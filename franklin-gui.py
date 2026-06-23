#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false


"""
Franklin GTK GUI (Wayland-friendly) implementation.

This is a pure-renderer GUI: the headless recorder (franklin-race-recorder.py)
owns the authoritative race model and SQLite writes. The GUI:
- selects the next race's mode (Real/Fake/Training) and config
- publishes race-control commands (start/end/reset/penalty/DQ/remove-lap)
- renders the authoritative ``franklin:race_state`` snapshot it receives
- shows a lap/event log and the leaderboard from that snapshot
- manages drivers (add/rename/delete) via dialog

It never mutates race state locally. See docs/redis-message-reference.md for the
Redis channels/messages and the snapshot schema.
"""

import argparse
import json
import logging
import queue
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import gi
import redis

from gui_config import load_initial_config, write_config
from race.contestant import Contestant
from race.race import RaceEndMode
from race.race_contestants import RaceContestants
from race.race_mode import RaceMode
from race.race_snapshot import RaceSnapshot, idle_snapshot
from racer_colors import RacerColorScheme, assign_random_scheme
from redis_commands import build_command_envelope, parse_command_envelope

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]  # noqa: E402
    Gdk,
    Gio,
    GLib,
    Gtk,
    Pango,
)

# Reference layout the UI was designed against. Fonts and the initial window
# size scale relative to these so the GUI fits whatever screen it runs on.
# GTK4 CSS has no viewport-relative units, so we set a single root font size
# (in pt, which composes with the system text-scaling/DPI factor) and express
# every other font size as an ``em`` multiple of it in static CSS.
DESIGN_WIDTH = 1200
DESIGN_HEIGHT = 760
BASE_FONT_PT = 17  # root font size at scale 1.0; all other sizes are em multiples
UI_SCALE_MIN = 0.4
UI_SCALE_MAX = 1.6


class FranklinGuiApp(Gtk.Application):
    def __init__(
        self,
        *,
        initial_mode: RaceMode,
        total_laps: int,
        contestants_data: list[dict[str, Any]],
        race_end_mode: RaceEndMode,
        last_race_contestant_ids: list[int],
        racer_color_assignments: dict[int, RacerColorScheme],
        redis_socket: str = "./redis.sock",
    ) -> None:
        super().__init__(application_id="com.franklin.lapcounter.gui")

        # Logging
        logging.basicConfig(
            filename="gui.log",
            filemode="a",
            format="%(asctime)s %(levelname)s:%(message)s",
            level=logging.INFO,
            force=True,
        )
        logging.info("Franklin GUI initialized")

        # Next-race configuration (used only to build start commands and the
        # pre-race UI; the running race is owned by the recorder).
        self.total_laps = total_laps
        self.race_mode = initial_mode
        self.race_end_mode = race_end_mode
        self.global_contestants = RaceContestants(contestants_data)
        self.racer_color_assignments = dict(racer_color_assignments)
        self.last_race_contestant_ids: set[int] = set(last_race_contestant_ids)

        # Authoritative render state: the latest race snapshot from the recorder.
        # The GUI never mutates race state; it only renders this and publishes
        # commands (see docs/redis-message-reference.md, franklin:race_state).
        self.snapshot: RaceSnapshot = idle_snapshot()

        known_racer_ids = {
            c.transmitter_id for c in self.global_contestants.contestants
        }.union(last_race_contestant_ids)
        self._ensure_racer_color_assignments(known_racer_ids, persist=False)

        # Redis contract reference: docs/redis-message-reference.md
        self.redis_socket = redis_socket
        self.redis_in_channel = "hardware:in"
        self.redis_out_channel = "hardware:out"
        self.redis_events_channel = "franklin:events"
        self.redis_race_state_channel = "franklin:race_state"
        self.redis_race_state_latest_key = "franklin:race_state:latest"
        self._redis_client: redis.Redis | None = None
        self._redis_pubsub = None

        self.config_path = Path("franklin.config.json")

        self.lap_counter_detected = False
        self._last_lap_counter_signal_time: float | None = None

        # Threaded message processing. Each item is (channel, message) so the
        # drain loop can route snapshots vs hardware/event traffic.
        self._incoming_messages: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._shutdown = threading.Event()
        self._redis_thread: threading.Thread | None = None

        # UI refs
        self.window: Gtk.ApplicationWindow | None = None
        self._color_provider_counter = 0
        self.mode_combo: Gtk.DropDown | None = None
        self.start_btn: Gtk.Button | None = None
        self.stop_btn: Gtk.Button | None = None
        self.reset_btn: Gtk.Button | None = None
        self.time_label: Gtk.Label | None = None
        self.state_label: Gtk.Label | None = None
        self.detect_label: Gtk.Label | None = None
        self.laps_remaining_label: Gtk.Label | None = None
        self.ethernet_label: Gtk.Label | None = None
        self.wifi_label: Gtk.Label | None = None
        self.leaderboard_grid: Gtk.Grid | None = None
        self.leaderboard_title_label: Gtk.Label | None = None
        self.leaderboard_scroll: Gtk.ScrolledWindow | None = None
        self._initial_scale: float = 1.0
        self.events_view: Gtk.TextView | None = None
        self.panes: Gtk.Paned | None = None
        self.events_box: Gtk.Box | None = None
        self._events_visible = False

        self._system_status_thread: threading.Thread | None = None
        # Single CSS provider for the whole window. Only the root font size
        # changes with the window; everything else is static ``em``-based CSS.
        self._css_provider = Gtk.CssProvider()
        self._base_font_pt: int | None = None
        self._swatch_css_classes: dict[tuple[str, str], str] = {}

        # Start light UI (mirrored on both sides of the timer)
        self._start_light_count = 4
        self._start_light_left_areas: list[Gtk.Widget] = []
        self._start_light_right_areas: list[Gtk.Widget] = []
        self._start_light_classes: list[str] = [
            "start-light-red"
        ] * self._start_light_count
        self._start_light_spacing_px = 6
        self._start_light_diameter_px: int | None = None
        self._start_sequence_running = False
        self._start_sequence_phase: str | None = None

        self._register_actions_and_shortcuts()

    def _snapshot_racer_ids(self) -> set[int]:
        """Racer IDs present in the current snapshot (leaderboard or laps)."""
        ids = {row.racer_id for row in self.snapshot.leaderboard}
        if not ids:
            ids = {lap.racer_id for lap in self.snapshot.laps}
        return ids

    def _humanize_snapshot_state(self, state: str) -> str:
        if state == "not_started":
            return "Ready to start"
        return state.replace("_", " ").title()

    def handle_snapshot(self, data: dict[str, Any]) -> None:
        """Apply an authoritative race-state snapshot from the recorder."""
        try:
            snapshot = RaceSnapshot.from_dict(data)
        except Exception as exc:
            logging.error("Invalid race snapshot: %s", exc)
            self.append_event(f"Ignored invalid race snapshot: {exc}")
            return

        if not snapshot.supersedes(self.snapshot):
            return

        previous_state = self.snapshot.state
        self.snapshot = snapshot

        racer_ids = self._snapshot_racer_ids()
        if racer_ids:
            self.last_race_contestant_ids = set(racer_ids)
            self._ensure_racer_color_assignments(racer_ids, persist=True)

        # An authoritative running/finished state ends any local countdown visuals.
        if self.snapshot.is_going:
            self._start_sequence_running = False
            self._set_start_sequence_phase(None)
            self._set_start_lights("#2e7d32")
        elif self.snapshot.state in {"finished", "not_started"}:
            self._start_sequence_running = False
            self._set_start_sequence_phase(None)

        if previous_state != "finished" and self.snapshot.state == "finished":
            self.save_config()

        self.refresh_views()

    def _sync_controls_with_race_state(self) -> None:
        running = self.snapshot.is_going
        starting = self._start_sequence_running

        start_action = self.lookup_action("start_race")
        if start_action:
            start_action.set_enabled(not running and not starting)

        end_action = self.lookup_action("end_race")
        if end_action:
            end_action.set_enabled(running and not starting)

        reset_action = self.lookup_action("reset_race")
        if reset_action:
            reset_action.set_enabled(self.snapshot.state == "finished" and not starting)

        mode_action = self.lookup_action("mode")
        if mode_action:
            mode_action.set_enabled(not running and not starting)

            # Sync the checked state of GIO action "mode" with the actual race mode
            if self.snapshot.is_going and self.snapshot.race_mode:
                if self.snapshot.race_mode == RaceMode.REAL.value:
                    mode_str = "real"
                elif self.snapshot.race_mode == RaceMode.FAKE.value:
                    mode_str = "fake"
                else:
                    mode_str = "training"
            else:
                if self.race_mode == RaceMode.REAL:
                    mode_str = "real"
                elif self.race_mode == RaceMode.FAKE:
                    mode_str = "fake"
                else:
                    mode_str = "training"

            current_state = mode_action.get_state()
            if current_state and current_state.get_string() != mode_str:
                mode_action.set_state(GLib.Variant.new_string(mode_str))

    def _register_actions_and_shortcuts(self) -> None:
        action_defs: list[tuple[str, Any, list[str]]] = [
            ("start_race", self._action_start_race, ["<Primary>s"]),
            ("end_race", self._action_end_race, ["<Primary>e"]),
            ("reset_race", self._action_reset_race, ["<Primary><Shift>r"]),
            ("toggle_mode", self._action_toggle_mode, ["<Primary>t"]),
            ("toggle_event_log", self._action_toggle_event_log, ["<Primary>l"]),
            ("manage_drivers", self._action_manage_drivers, ["<Primary>r"]),
            ("preferences", self._action_preferences, ["<Primary>comma"]),
            ("show_keyboard_shortcuts", self._action_show_keyboard_shortcuts, ["<Shift>slash"]),
        ]

        for name, callback, accels in action_defs:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)

        # Register stateful "mode" action for the PopoverMenuBar checkmarks
        initial_mode_str = "real"
        if self.race_mode == RaceMode.FAKE:
            initial_mode_str = "fake"
        elif self.race_mode == RaceMode.TRAINING:
            initial_mode_str = "training"

        mode_action = Gio.SimpleAction.new_stateful(
            "mode",
            GLib.VariantType.new("s"),  # string parameter type
            GLib.Variant.new_string(initial_mode_str),  # initial state
        )
        mode_action.connect("activate", self._action_change_mode)
        self.add_action(mode_action)

    def _action_change_mode(
        self, action: Gio.SimpleAction, parameter: GLib.Variant
    ) -> None:
        if self.snapshot.is_going:
            self.append_event("Cannot change mode while race is running")
            return

        mode_str = parameter.get_string()
        action.set_state(parameter)

        if mode_str == "real":
            self.race_mode = RaceMode.REAL
        elif mode_str == "fake":
            self.race_mode = RaceMode.FAKE
        elif mode_str == "training":
            self.race_mode = RaceMode.TRAINING

        self.save_config()
        self.append_event(f"Mode changed to {self.race_mode}")
        self.refresh_views()

    def _action_start_race(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_start_clicked(None)

    def _action_end_race(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_end_clicked(None)

    def _action_reset_race(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_reset_clicked(None)

    def _action_toggle_mode(self, _action: Gio.SimpleAction, _param: Any) -> None:
        if self.snapshot.is_going:
            self.append_event("Cannot change mode while race is running")
            return
        modes = [RaceMode.REAL, RaceMode.FAKE, RaceMode.TRAINING]
        idx = modes.index(self.race_mode)
        next_mode = modes[(idx + 1) % len(modes)]
        self.race_mode = next_mode
        self.append_event(f"Mode changed to {self.race_mode}")
        self.refresh_views()

    def _action_toggle_event_log(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.toggle_event_log_visibility()

    def _action_manage_drivers(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_manage_drivers_clicked(None)

    def _action_preferences(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_preferences_clicked(None)

    def _action_show_keyboard_shortcuts(
        self, _action: Gio.SimpleAction, _param: Any
    ) -> None:
        self.show_keyboard_shortcuts_dialog()

    def do_activate(self) -> None:  # type: ignore[override]
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("Franklin Lap Counter (GTK)")
        win_w, win_h = self._fit_size_to_monitor()
        window.set_default_size(win_w, win_h)
        self.window = window

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.add_css_class("franklin-root")
        root.set_margin_top(0)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)

        # PopoverMenuBar setup at the top of the window
        menu_model = Gio.Menu()

        # Race Submenu
        race_menu = Gio.Menu()
        race_menu.append("Start Race", "app.start_race")
        race_menu.append("End Race", "app.end_race")
        race_menu.append("Reset Race", "app.reset_race")
        menu_model.append_submenu("Race", race_menu)

        # Mode Submenu
        mode_menu = Gio.Menu()
        mode_menu.append("Real Race Mode", "app.mode('real')")
        mode_menu.append("Fake Race Mode", "app.mode('fake')")
        mode_menu.append("Training Mode", "app.mode('training')")
        menu_model.append_submenu("Mode", mode_menu)

        # Tools Submenu
        tools_menu = Gio.Menu()
        tools_menu.append("Manage Drivers", "app.manage_drivers")
        tools_menu.append("Preferences", "app.preferences")
        tools_menu.append("Toggle Event Log", "app.toggle_event_log")
        menu_model.append_submenu("Tools", tools_menu)

        menu_bar = Gtk.PopoverMenuBar.new_from_model(menu_model)
        menu_bar.set_hexpand(True)

        time_label = Gtk.Label(label="00:00:00")
        time_label.add_css_class("race-clock")
        time_label.set_xalign(0.5)
        time_label.set_halign(Gtk.Align.CENTER)
        time_label.set_hexpand(False)
        self.time_label = time_label

        clock_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        clock_row.set_halign(Gtk.Align.CENTER)
        clock_row.set_hexpand(True)
        left_lights = self._create_start_light_stack()
        right_lights = self._create_start_light_stack()
        self._start_light_left_areas = left_lights
        self._start_light_right_areas = right_lights
        self._set_start_lights("#c62828")
        clock_row.append(self._wrap_start_light_stack(left_lights))
        clock_row.append(time_label)
        clock_row.append(self._wrap_start_light_stack(right_lights))

        # Keep the clock + start-light cluster centred rather than stretching
        # oddly on very wide windows.
        clock_frame = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        clock_frame.set_halign(Gtk.Align.CENTER)
        clock_frame.set_hexpand(True)
        clock_frame.set_margin_bottom(10)
        clock_frame.append(clock_row)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        state_label = Gtk.Label(
            label=f"State: {self._humanize_snapshot_state(self.snapshot.state)}"
        )
        laps_remaining_label = Gtk.Label(label=f"Laps Remaining: {self.total_laps}")
        self.state_label = state_label
        self.laps_remaining_label = laps_remaining_label

        state_label.set_xalign(0)
        laps_remaining_label.set_xalign(0)
        status.append(laps_remaining_label)

        panes = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        panes.set_vexpand(True)
        panes.set_hexpand(True)
        self.panes = panes

        leaderboard_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        leaderboard_label = Gtk.Label(label="Leaderboard")
        leaderboard_label.add_css_class("leaderboard-title")
        self.leaderboard_title_label = leaderboard_label
        leaderboard_box.append(leaderboard_label)

        leaderboard_grid = Gtk.Grid()
        leaderboard_grid.set_column_spacing(16)
        leaderboard_grid.set_row_spacing(4)
        leaderboard_grid.set_hexpand(True)
        leaderboard_grid.set_vexpand(True)
        leaderboard_grid.add_css_class("leaderboard-grid")
        self.leaderboard_grid = leaderboard_grid

        leaderboard_scroll = Gtk.ScrolledWindow()
        leaderboard_scroll.set_vexpand(True)
        leaderboard_scroll.set_hexpand(True)
        leaderboard_scroll.set_child(leaderboard_grid)
        leaderboard_box.append(leaderboard_scroll)
        self.leaderboard_scroll = leaderboard_scroll
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        events_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        events_box.append(Gtk.Label(label="Events"))
        events_view = Gtk.TextView()
        events_view.set_editable(False)
        events_view.set_monospace(True)
        self.events_view = events_view

        events_scroll = Gtk.ScrolledWindow()
        events_scroll.set_vexpand(True)
        events_scroll.set_hexpand(True)
        events_scroll.set_child(events_view)
        events_box.append(events_scroll)
        self.events_box = events_box

        panes.set_start_child(leaderboard_box)

        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=20)
        detect_label = Gtk.Label(label="HW: Waiting")
        detect_label.set_xalign(0)
        ethernet_label = Gtk.Label(label="Ethernet: checking...")
        ethernet_label.set_xalign(0)
        wifi_label = Gtk.Label(label="Wi-Fi: checking...")
        wifi_label.set_xalign(0)
        self.detect_label = detect_label
        self.ethernet_label = ethernet_label
        self.wifi_label = wifi_label

        status_bar.append(state_label)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(detect_label)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(ethernet_label)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(wifi_label)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(Gtk.Label(label="? for Help"))

        root.append(menu_bar)
        root.append(clock_frame)
        root.append(status)
        root.append(panes)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        root.append(status_bar)

        window.set_child(root)

        # Rescale fonts when the window size changes instead of polling on the
        # timer. ``default-width``/``default-height`` track the live size of a
        # resizable window in GTK4.
        window.connect("notify::default-width", self._on_window_resize)
        window.connect("notify::default-height", self._on_window_resize)
        window.connect("map", self._on_window_resize)

        window.present()
        self._apply_scale()

        self.connect_redis()
        GLib.timeout_add(100, self.update_time)
        GLib.timeout_add(50, self.drain_incoming_messages)

        self.toggle_event_log_visibility(show=False)
        self._start_system_status_updater()

        self._sync_controls_with_race_state()

        self.append_event("GUI ready")
        self.refresh_views()

    def do_shutdown(self) -> None:  # type: ignore[override]
        self._shutdown.set()
        try:
            if self._redis_pubsub:
                self._redis_pubsub.close()
        except Exception:
            pass
        try:
            if self._redis_client:
                self._redis_client.close()
        except Exception:
            pass
        if self._system_status_thread and self._system_status_thread.is_alive():
            self._system_status_thread.join(timeout=1.0)
        Gtk.Application.do_shutdown(self)

    def toggle_event_log_visibility(self, show: bool | None = None) -> None:
        if not self.panes or not self.events_box:
            return

        target = (not self._events_visible) if show is None else show
        if target:
            self.panes.set_end_child(self.events_box)
            self._events_visible = True
            self.append_event("Events log shown")
        else:
            self.panes.set_end_child(None)
            self._events_visible = False

    def _fit_size_to_monitor(self) -> tuple[int, int]:
        """Return an initial window size that fits the current monitor.

        Also records ``self._initial_scale`` so first-paint fonts match the
        chosen window size (GTK has no viewport-relative CSS units).
        """
        win_w, win_h = DESIGN_WIDTH, DESIGN_HEIGHT
        display = Gdk.Display.get_default()
        monitors = display.get_monitors() if display is not None else None
        if monitors is not None and monitors.get_n_items() > 0:
            monitor = monitors.get_item(0)
            geometry = monitor.get_geometry()  # type: ignore[union-attr]
            if geometry.width > 0 and geometry.height > 0:
                # Leave a margin so window decorations/panels still fit.
                win_w = min(DESIGN_WIDTH, int(geometry.width * 0.95))
                win_h = min(DESIGN_HEIGHT, int(geometry.height * 0.92))

        self._initial_scale = self._scale_for_size(win_w, win_h)
        return win_w, win_h

    def _scale_for_size(self, width: int, height: int) -> float:
        if width <= 0 or height <= 0:
            return 1.0
        scale = min(width / DESIGN_WIDTH, height / DESIGN_HEIGHT)
        return max(UI_SCALE_MIN, min(UI_SCALE_MAX, scale))

    def _ui_scale(self) -> float:
        """Current UI scale based on the live window size."""
        if not self.window:
            return self._initial_scale
        width = self.window.get_width()
        height = self.window.get_height()
        if width <= 0 or height <= 0:
            return self._initial_scale
        return self._scale_for_size(width, height)

    def _on_window_resize(self, *_args: Any) -> None:
        self._apply_scale()

    def _apply_scale(self) -> None:
        """Set the single root font size from the current window size.

        Every other font size is an ``em`` multiple of this in the CSS built by
        :meth:`_build_css`, so one provider reload rescales the whole window.
        """
        scale = self._ui_scale()
        base_pt = max(8, round(BASE_FONT_PT * scale))
        if base_pt == self._base_font_pt:
            return
        self._base_font_pt = base_pt
        self._css_provider.load_from_data(self._build_css(base_pt).encode("utf-8"))

    def _build_css(self, base_pt: int) -> str:
        """Whole-window stylesheet. Only the root font size is dynamic.

        ``pt`` units compose with the system text-scaling/DPI factor, and font
        size inherits down the widget tree, so the ``em`` multiples below all
        resolve against the root size.
        """
        return f"""
        .franklin-root {{ font-size: {base_pt}pt; }}

        .race-clock {{ font-size: 2.6em; font-weight: bold; }}
        .leaderboard-title {{ font-size: 2.3em; font-weight: bold; }}

        .leaderboard-cell, .leaderboard-header-cell {{
            font-size: 1em;
            font-family: monospace;
        }}
        .leaderboard-status-cell {{
            font-size: 1em;
            font-family: 'Noto Color Emoji', monospace;
        }}
        .leaderboard-header-cell {{
            font-weight: 700;
            background-color: #ececec;
        }}
        .leaderboard-best-col {{ background-color: #eaf4ff; }}
        .leaderboard-last-col {{ background-color: #fff9e8; }}
        .leaderboard-total-col {{ background-color: #ffecec; }}

        {self._build_swatch_css()}

        .start-light {{
            border-radius: 999px;
            border: 2px solid #141414;
            background-color: #c62828;
        }}
        .start-light-red {{ background-color: #c62828; }}
        .start-light-yellow {{ background-color: #f9a825; }}
        .start-light-green {{ background-color: #2e7d32; }}
        """

    def _build_swatch_css(self) -> str:
        return "\n".join(
            f".{class_name}-primary {{ background-color: {primary_hex}; }}\n"
            f".{class_name}-secondary {{ background-color: {secondary_hex}; }}"
            for (
                primary_hex,
                secondary_hex,
            ), class_name in self._swatch_css_classes.items()
        )

    def _create_start_light_stack(self) -> list[Gtk.Widget]:
        lights: list[Gtk.Widget] = []
        for idx in range(self._start_light_count):
            light = Gtk.Box()
            light.add_css_class("start-light")
            light.add_css_class(self._start_light_classes[idx])
            light.set_size_request(24, 24)
            light.set_hexpand(False)
            light.set_vexpand(False)
            lights.append(light)
        return lights

    def _wrap_start_light_stack(self, areas: list[Gtk.Widget]) -> Gtk.Box:
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=self._start_light_spacing_px
        )
        box.set_valign(Gtk.Align.CENTER)
        for area in areas:
            box.append(area)
        return box

    def _set_start_light_pattern(self, classes: list[str]) -> None:
        if len(classes) != self._start_light_count:
            return

        if classes == self._start_light_classes:
            return

        previous = self._start_light_classes
        self._start_light_classes = classes.copy()

        for idx, area in enumerate(self._start_light_left_areas):
            area.remove_css_class(previous[idx])
            area.add_css_class(classes[idx])

        for idx, area in enumerate(self._start_light_right_areas):
            area.remove_css_class(previous[idx])
            area.add_css_class(classes[idx])

    def _set_start_lights(self, color_hex: str) -> None:
        target_class = {
            "#c62828": "start-light-red",
            "#f9a825": "start-light-yellow",
            "#2e7d32": "start-light-green",
        }.get(color_hex, "start-light-red")
        self._set_start_light_pattern([target_class] * self._start_light_count)

    def _sync_start_lights_with_race_state(self) -> None:
        if self._start_sequence_running:
            return
        if self.snapshot.is_going:
            self._set_start_lights("#2e7d32")
        else:
            self._set_start_lights("#c62828")

    def _set_start_sequence_phase(self, phase: str | None) -> None:
        self._start_sequence_phase = phase

        if not self.state_label:
            return

        if phase is None:
            self.state_label.set_text(
                f"State: {self._humanize_snapshot_state(self.snapshot.state)}"
            )
            return

        self.state_label.set_text(f"State: Starting - {phase}")

    def _update_start_light_size(self) -> None:
        if not self.time_label or not self.window:
            return

        timer_height = self.time_label.get_height()
        timer_width = self.time_label.get_width()
        window_width = self.window.get_width()
        if timer_height <= 0 or timer_width <= 0 or window_width <= 0:
            return

        total_spacing = self._start_light_spacing_px * (self._start_light_count - 1)

        # Don't exceed timer text height.
        max_by_height = max(10, timer_height - 4)

        # Fit all 4 lights on each side of the timer.
        # clock_row spacing is 24 between [left-lights][timer][right-lights] => 48 total
        approx_side_width = max(40, int((window_width - timer_width - 48) / 2))
        max_by_width = max(
            10, int((approx_side_width - total_spacing) / self._start_light_count)
        )

        diameter = max(10, min(max_by_height, max_by_width))
        if self._start_light_diameter_px == diameter:
            return

        self._start_light_diameter_px = diameter
        for area in self._start_light_left_areas + self._start_light_right_areas:
            area.set_size_request(diameter, diameter)

    def _start_race_countdown(self) -> None:
        if self._start_sequence_running:
            return

        self._start_sequence_running = True
        self._set_start_sequence_phase("Starting")
        self._set_start_lights("#c62828")
        self.append_event("Start countdown")

        self._sync_controls_with_race_state()

        if not self._redis_client:
            self.append_event("Redis not connected; cannot schedule race start")
            self._start_sequence_running = False
            self._sync_controls_with_race_state()
            return

        base = time.time() + 0.25
        ready_at = base
        set_at = base + 1.0
        go_at = base + 2.0

        # The recorder owns the race; we only request a start (with config) and
        # render the snapshot it publishes. The countdown visuals below are a
        # local preview that the authoritative snapshot supersedes once running.
        self.publish_command(
            "start_race",
            ready_at=ready_at,
            set_at=set_at,
            go_at=go_at,
            start_at=go_at,
            # Race config for the headless recorder (ignored by the Rust owner).
            race_mode=self.race_mode.value,
            total_laps=self.total_laps,
            race_end_mode=self.race_end_mode.value,
        )
        self.append_event(f"Scheduled countdown (go at {go_at:.3f})")

        # Local visual countdown preview. If Redis timeline events arrive, those
        # handlers keep this in sync; when the running snapshot arrives,
        # handle_snapshot() clears the sequence.
        def show_ready_local() -> bool:
            if not self._start_sequence_running or self.snapshot.is_going:
                return False
            self._set_start_sequence_phase("Ready")
            self._set_start_light_pattern(
                [
                    "start-light-yellow",
                    "start-light-red",
                    "start-light-red",
                    "start-light-yellow",
                ]
            )
            self.refresh_views()
            return False

        def show_set_local() -> bool:
            if not self._start_sequence_running or self.snapshot.is_going:
                return False
            self._set_start_sequence_phase("Set")
            self._set_start_lights("#f9a825")
            self.refresh_views()
            return False

        def show_go_local() -> bool:
            if not self._start_sequence_running or self.snapshot.is_going:
                return False
            self._set_start_sequence_phase("Go")
            self._set_start_lights("#2e7d32")
            self.refresh_views()
            return False

        now_epoch = time.time()
        ready_delay_ms = max(0, int((ready_at - now_epoch) * 1000))
        set_delay_ms = max(0, int((set_at - now_epoch) * 1000))
        go_delay_ms = max(0, int((go_at - now_epoch) * 1000))

        GLib.timeout_add(ready_delay_ms, show_ready_local)
        GLib.timeout_add(set_delay_ms, show_set_local)
        GLib.timeout_add(go_delay_ms, show_go_local)

    def _run_command(self, args: list[str], timeout: float = 1.0) -> str:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            return ""
        return ""

    def _detect_ethernet_interface(self) -> str | None:
        try:
            names = sorted(p.name for p in Path("/sys/class/net").iterdir())
        except Exception:
            return None

        for prefix in ("eth", "en"):
            for name in names:
                if name.startswith(prefix):
                    return name
        return None

    def _get_ipv4_for_interface(self, interface: str) -> str | None:
        out = self._run_command(["ip", "-4", "-o", "addr", "show", "dev", interface])
        for part in out.split():
            if "/" in part and part.count(".") == 3:
                return part.split("/")[0]
        return None

    def _has_internet_access(self) -> bool:
        try:
            with socket.create_connection(("1.1.1.1", 53), timeout=0.7):
                return True
        except OSError:
            return False

    def _get_ethernet_status_text(self) -> str:
        iface = self._detect_ethernet_interface()
        if not iface:
            return "Ethernet: unavailable"

        ipv4 = self._get_ipv4_for_interface(iface)
        if not ipv4:
            return f"Ethernet ({iface}): disconnected"

        internet = "internet ok" if self._has_internet_access() else "no internet"
        return f"Ethernet ({iface}): {ipv4} | {internet}"

    def _get_wifi_ssid(self) -> str | None:
        ssid = self._run_command(["iwgetid", "-r"], timeout=0.8)
        return ssid if ssid else None

    def _get_wifi_password(self, ssid: str) -> str:
        psk = self._run_command(
            [
                "nmcli",
                "-s",
                "-g",
                "802-11-wireless-security.psk",
                "connection",
                "show",
                ssid,
            ],
            timeout=1.0,
        )
        return psk if psk else "Unavailable"

    def _get_wifi_status_text(self) -> str:
        ssid = self._get_wifi_ssid()
        if not ssid:
            return "Wi-Fi: disconnected"
        password = self._get_wifi_password(ssid)
        return f"Wi-Fi: {ssid} | PW: {password}"

    def _apply_system_status(self, ethernet_text: str, wifi_text: str) -> bool:
        if self.ethernet_label:
            self.ethernet_label.set_text(ethernet_text)
        if self.wifi_label:
            self.wifi_label.set_text(wifi_text)
        return False

    def _start_system_status_updater(self) -> None:
        def worker() -> None:
            while not self._shutdown.is_set():
                ethernet_text = self._get_ethernet_status_text()
                wifi_text = self._get_wifi_status_text()
                GLib.idle_add(self._apply_system_status, ethernet_text, wifi_text)
                if self._shutdown.wait(5.0):
                    break

        self._system_status_thread = threading.Thread(target=worker, daemon=True)
        self._system_status_thread.start()

    def append_event(self, text: str) -> None:
        if not self.events_view:
            return
        buf = self.events_view.get_buffer()
        end = buf.get_end_iter()
        timestamp = time.strftime("%H:%M:%S")
        buf.insert(end, f"[{timestamp}] {text}\n")

    def _format_time_cs(self, seconds_value: float | None) -> str:
        if seconds_value is None or seconds_value == float("inf"):
            return "00:00:00"

        total_cs = max(0, int(seconds_value * 100))
        minutes = min(99, total_cs // 6000)
        seconds = (total_cs // 100) % 60
        centiseconds = total_cs % 100
        return f"{minutes:02}:{seconds:02}:{centiseconds:02}"

    def _leaderboard_status_symbol(self, position: int, lap_count: int) -> str:
        if self.snapshot.state == "finished":
            if position == 1:
                return "🥇"
            if position == 2:
                return "🥈"
            if position == 3:
                return "🥉"
            return ""

        if self.snapshot.state == "winner_declared" and position == 1:
            return "🏁"

        target_laps = self.snapshot.effective_total_laps or self.total_laps
        if target_laps and lap_count == (target_laps - 1):
            return "🔔"

        if lap_count == 0:
            return "—"

        return ""

    def _ensure_racer_color_assignments(
        self, racer_ids: set[int], *, persist: bool
    ) -> None:
        changed = False
        for racer_id in racer_ids:
            if racer_id <= 0:
                continue
            if racer_id in self.racer_color_assignments:
                continue
            self.racer_color_assignments[racer_id] = assign_random_scheme(
                self.racer_color_assignments
            )
            changed = True

        if changed and persist:
            self.save_config()

    def _racer_color_scheme(self, racer_id: int) -> tuple[str, str]:
        self._ensure_racer_color_assignments({racer_id}, persist=False)
        return self.racer_color_assignments.get(racer_id, ("#777777", "#bbbbbb"))

    def _hex_to_rgb(self, color_hex: str) -> tuple[float, float, float]:
        raw = color_hex.strip().lstrip("#")
        if len(raw) != 6:
            return (0.5, 0.5, 0.5)
        try:
            r = int(raw[0:2], 16) / 255.0
            g = int(raw[2:4], 16) / 255.0
            b = int(raw[4:6], 16) / 255.0
            return (r, g, b)
        except ValueError:
            return (0.5, 0.5, 0.5)

    def _hex_to_rgba(self, color_hex: str) -> Gdk.RGBA:
        rgba = Gdk.RGBA()
        if not rgba.parse(color_hex):
            rgba.parse("#777777")
        return rgba

    def _rgba_to_hex(self, rgba: Gdk.RGBA) -> str:
        r = max(0, min(255, int(round(rgba.red * 255))))
        g = max(0, min(255, int(round(rgba.green * 255))))
        b = max(0, min(255, int(round(rgba.blue * 255))))
        return f"#{r:02x}{g:02x}{b:02x}"

    def _new_color_picker_button(
        self, initial_hex: str, on_hex_changed: Any
    ) -> Gtk.Widget:
        # Custom lightweight Popover-based color picker to completely bypass GSettings schema issues.
        button = Gtk.Button()

        swatch_box = Gtk.Box()
        swatch_box.set_size_request(24, 24)

        # Use modern GTK4 add_provider_for_display with an ever-incrementing counter to avoid collisions and deprecations
        self._color_provider_counter += 1
        class_id = f"swatch-box-p-{self._color_provider_counter}"
        swatch_box.add_css_class(class_id)

        provider = Gtk.CssProvider()
        provider.load_from_data(
            f".{class_id} {{ background-color: {initial_hex}; border-radius: 4px; border: 1px solid #777; }}".encode(
                "utf-8"
            )
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        button.set_child(swatch_box)

        popover = Gtk.Popover()
        popover.set_parent(button)

        # Unparent the popover when the button is destroyed to avoid finalized child warnings
        button.connect("destroy", lambda widget: popover.unparent())

        popover_root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        popover_root.set_margin_top(8)
        popover_root.set_margin_bottom(8)
        popover_root.set_margin_start(8)
        popover_root.set_margin_end(8)

        presets_grid = Gtk.Grid()
        presets_grid.set_column_spacing(4)
        presets_grid.set_row_spacing(4)

        presets = [
            "#e53935",
            "#d81b60",
            "#8e24aa",
            "#5e35b1",
            "#3949ab",
            "#1e88e5",
            "#039be5",
            "#00acc1",
            "#00897b",
            "#43a047",
            "#7cb342",
            "#c0ca33",
            "#fdd835",
            "#ffb300",
            "#fb8c00",
            "#f4511e",
            "#6d4c41",
            "#757575",
            "#546e7a",
            "#212121",
            "#ffffff",
        ]

        def on_preset_clicked(btn: Gtk.Button, hex_val: str) -> None:
            entry.set_text(hex_val)
            on_hex_changed(hex_val)
            popover.popdown()

        for i, hex_val in enumerate(presets):
            row = i // 7
            col = i % 7
            preset_btn = Gtk.Button()
            preset_btn.set_size_request(20, 24)

            preset_box = Gtk.Box()
            preset_box.set_size_request(16, 16)

            self._color_provider_counter += 1
            preset_class_id = f"preset-box-{self._color_provider_counter}"
            preset_box.add_css_class(preset_class_id)
            p_provider = Gtk.CssProvider()
            p_provider.load_from_data(
                f".{preset_class_id} {{ background-color: {hex_val}; border-radius: 3px; border: 1px solid #777; }}".encode(
                    "utf-8"
                )
            )
            if display is not None:
                Gtk.StyleContext.add_provider_for_display(
                    display, p_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )

            preset_btn.set_child(preset_box)

            preset_btn.connect("clicked", on_preset_clicked, hex_val)
            presets_grid.attach(preset_btn, col, row, 1, 1)

        popover_root.append(presets_grid)

        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.Entry()
        entry.set_width_chars(10)
        entry.set_text(initial_hex)
        entry_row.append(entry)

        apply_btn = Gtk.Button(label="Apply")

        def on_apply_clicked(btn: Gtk.Button) -> None:
            text = entry.get_text().strip()
            if text.startswith("#") and len(text) in (4, 7):
                on_hex_changed(text)
                popover.popdown()

        apply_btn.connect("clicked", on_apply_clicked)
        entry_row.append(apply_btn)

        popover_root.append(entry_row)
        popover.set_child(popover_root)

        button.connect("clicked", lambda b: popover.popup())
        return button

    def _new_color_swatch_for_colors(
        self, primary_hex: str, secondary_hex: str
    ) -> Gtk.Widget:
        color_key = (primary_hex.lower(), secondary_hex.lower())
        class_name = self._swatch_css_classes.get(color_key)
        if class_name is None:
            class_name = f"racer-swatch-{len(self._swatch_css_classes)}"
            self._swatch_css_classes[color_key] = class_name
            self._base_font_pt = None
            self._apply_scale()

        swatch = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        swatch.set_size_request(34, 20)
        swatch.set_hexpand(False)
        swatch.set_vexpand(False)
        swatch.set_halign(Gtk.Align.CENTER)
        swatch.set_valign(Gtk.Align.CENTER)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        top.add_css_class(f"{class_name}-primary")
        top.set_size_request(34, 7)
        middle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        middle.add_css_class(f"{class_name}-secondary")
        middle.set_size_request(34, 6)
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bottom.add_css_class(f"{class_name}-primary")
        bottom.set_size_request(34, 7)

        swatch.append(top)
        swatch.append(middle)
        swatch.append(bottom)

        frame = Gtk.Frame()
        frame.set_child(swatch)
        frame.set_size_request(36, 22)
        frame.set_hexpand(False)
        frame.set_vexpand(False)
        frame.set_halign(Gtk.Align.CENTER)
        frame.set_valign(Gtk.Align.CENTER)
        return frame

    def _new_color_swatch(self, racer_id: int) -> Gtk.Widget:
        primary_hex, secondary_hex = self._racer_color_scheme(racer_id)
        return self._new_color_swatch_for_colors(primary_hex, secondary_hex)

    def _new_leaderboard_label(
        self,
        text: str,
        *,
        xalign: float,
        css_class: str,
        hexpand: bool = False,
        extra_css_classes: list[str] | None = None,
        ellipsize: bool = False,
    ) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_xalign(xalign)
        label.add_css_class(css_class)
        if extra_css_classes:
            for extra_css_class in extra_css_classes:
                label.add_css_class(extra_css_class)
        label.set_hexpand(hexpand)
        if ellipsize:
            # Let the name column shrink and tail-truncate instead of forcing
            # the grid wider; GTK measures the rest of the columns to content.
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_width_chars(8)
        return label

    def _referee_adjusted_leaderboard(
        self,
    ) -> list[
        tuple[str, int, int | str, float | None, float | None, float | None, bool]
    ]:
        """Display rows from the authoritative snapshot.

        The recorder already applies penalties/DQ and orders the rows, so we
        just map them to the grid tuple shape. ``inf`` keeps missing best/last
        times rendering as ``00:00:00`` via ``_format_time_cs``.
        """
        rows: list[
            tuple[str, int, int | str, float | None, float | None, float | None, bool]
        ] = []
        if self.snapshot.state == "not_started":
            sorted_racer_ids = sorted(
                self.last_race_contestant_ids,
                key=lambda rid: self.global_contestants.get_contestant_name(
                    rid
                ).lower(),
            )
            for rid in sorted_racer_ids:
                rows.append(("", rid, "", None, None, None, False))
            return rows

        for row in self.snapshot.leaderboard:
            pos_label = "DQ" if row.disqualified else str(row.position)
            best = row.best_lap_time if row.best_lap_time is not None else float("inf")
            last = row.last_lap_time if row.last_lap_time is not None else float("inf")
            rows.append(
                (
                    pos_label,
                    row.racer_id,
                    row.lap_count,
                    best,
                    last,
                    row.adjusted_total_time,
                    row.disqualified,
                )
            )
        return rows

    def _render_leaderboard_grid(self) -> None:
        if not self.leaderboard_grid:
            return

        status_col_width_px = 48
        swatch_col_width_px = 56

        child = self.leaderboard_grid.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.leaderboard_grid.remove(child)
            child = next_child

        leaderboard_data = self._referee_adjusted_leaderboard()

        header_cells: list[tuple[str, float, bool]] = [
            ("Pos", 1.0, False),
            ("", 0.5, False),
            ("", 0.5, False),
            ("Racer", 0.0, True),
            ("Laps", 1.0, False),
            ("Best", 1.0, False),
            ("Last", 1.0, False),
            ("Total", 1.0, False),
        ]

        for col, (text, xalign, hexpand) in enumerate(header_cells):
            header_extra_classes: list[str] = []
            if col == 5:
                header_extra_classes.append("leaderboard-best-col")
            elif col == 6:
                header_extra_classes.append("leaderboard-last-col")
            elif col == 7:
                header_extra_classes.append("leaderboard-total-col")

            header_label = self._new_leaderboard_label(
                text,
                xalign=xalign,
                css_class="leaderboard-header-cell",
                hexpand=hexpand,
                extra_css_classes=header_extra_classes,
            )
            if col == 1:
                header_label.set_size_request(status_col_width_px, -1)
            elif col == 2:
                header_label.set_size_request(swatch_col_width_px, -1)
            self.leaderboard_grid.attach(header_label, col, 0, 1, 1)

        for row_index, (
            pos_label,
            racer_id,
            lap_count,
            best,
            last,
            total,
            is_dq,
        ) in enumerate(leaderboard_data, start=1):
            name = self.global_contestants.get_contestant_name(racer_id)
            if is_dq:
                status_symbol = "⛔"
                name = f"{name} (DQ)"
            elif not pos_label:
                status_symbol = ""
            else:
                status_symbol = self._leaderboard_status_symbol(
                    int(pos_label), lap_count if isinstance(lap_count, int) else 0
                )
            best_s = self._format_time_cs(best) if best is not None else ""
            last_s = self._format_time_cs(last) if last is not None else ""
            total_s = self._format_time_cs(total) if total is not None else ""

            row_values: list[tuple[str, float, bool, list[str]]] = [
                (pos_label, 1.0, False, []),
                (status_symbol, 0.5, False, ["leaderboard-status-cell"]),
                ("", 0.5, False, []),
                (name, 0.0, True, []),
                (f"{lap_count}", 1.0, False, []),
                (best_s, 1.0, False, ["leaderboard-best-col"]),
                (last_s, 1.0, False, ["leaderboard-last-col"]),
                (total_s, 1.0, False, ["leaderboard-total-col"]),
            ]

            for col, (text, xalign, hexpand, extra_classes) in enumerate(row_values):
                if col == 2:
                    swatch = self._new_color_swatch(racer_id)
                    swatch_cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                    swatch_cell.set_halign(Gtk.Align.FILL)
                    swatch_cell.set_hexpand(False)
                    swatch_cell.set_size_request(swatch_col_width_px, -1)
                    swatch_cell.append(swatch)
                    self.leaderboard_grid.attach(swatch_cell, col, row_index, 1, 1)
                    continue

                cell_label = self._new_leaderboard_label(
                    text,
                    xalign=xalign,
                    css_class="leaderboard-cell",
                    hexpand=hexpand,
                    extra_css_classes=extra_classes,
                    ellipsize=(col == 3),
                )
                if col == 1:
                    cell_label.set_size_request(status_col_width_px, -1)
                self.leaderboard_grid.attach(cell_label, col, row_index, 1, 1)

    def refresh_views(self) -> None:
        self._sync_start_lights_with_race_state()
        self._sync_controls_with_race_state()

        if self.state_label:
            if self._start_sequence_phase is not None:
                self.state_label.set_text(
                    f"State: Starting - {self._start_sequence_phase}"
                )
            else:
                self.state_label.set_text(
                    f"State: {self._humanize_snapshot_state(self.snapshot.state)}"
                )
        if self.detect_label:
            now = time.monotonic()
            connected = (
                self._last_lap_counter_signal_time is not None
                and (now - self._last_lap_counter_signal_time) < 5.0
            )
            status = "Connected" if connected else "Waiting"
            self.detect_label.set_text(f"HW: {status}")
        if self.laps_remaining_label:
            # Show the running race's mode if one is active, otherwise the mode
            # selected for the next race.
            if self.snapshot.is_going and self.snapshot.race_mode:
                is_training = self.snapshot.race_mode == RaceMode.TRAINING.value
            else:
                is_training = self.race_mode == RaceMode.TRAINING
            if is_training:
                self.laps_remaining_label.set_visible(False)
            else:
                self.laps_remaining_label.set_visible(True)
                if self._start_sequence_running or self.snapshot.race_id is None:
                    laps_remaining = self.total_laps
                else:
                    laps_remaining = self.snapshot.laps_remaining_leader
                self.laps_remaining_label.set_text(f"Laps Remaining: {laps_remaining}")

        self._render_leaderboard_grid()

    def update_time(self) -> bool:
        self._update_start_light_size()

        if self.time_label:
            self.time_label.set_text(
                self._format_time_cs(self.snapshot.current_elapsed())
            )

        return True

    def on_start_clicked(self, _button: Gtk.Button | None) -> None:
        if self._start_sequence_running or self.snapshot.is_going:
            return

        # Publishes a start_race command (with config) for the recorder; the
        # authoritative race appears via the snapshot.
        self._start_race_countdown()
        self.refresh_views()

    def on_end_clicked(self, _button: Gtk.Button | None) -> None:
        if not self.snapshot.is_going:
            return

        self.publish_command("end_race")
        self.append_event("Requested race end")

    def on_reset_clicked(self, _button: Gtk.Button | None) -> None:
        if self._start_sequence_running or self.snapshot.state != "finished":
            return

        self.publish_command("reset_race")
        self.append_event("Requested race reset")

    def on_preferences_clicked(self, _button: Gtk.Button | None) -> None:
        if not self.window:
            return

        dialog = Gtk.Dialog(title="Preferences", transient_for=self.window, modal=True)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(10)
        root.set_margin_end(10)

        help_text = Gtk.Label(
            label="Set regular race lap count and end condition. Changes are saved to config and apply to new races."
        )
        help_text.set_xalign(0)
        root.append(help_text)

        laps_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        laps_row.append(Gtk.Label(label="Regular Race Laps:"))
        laps_spin = Gtk.SpinButton.new_with_range(1, 500, 1)
        laps_spin.set_value(float(self.total_laps))
        laps_row.append(laps_spin)
        root.append(laps_row)

        end_mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        end_mode_row.append(Gtk.Label(label="Race Ends:"))
        end_mode_options: list[tuple[str, RaceEndMode]] = [
            ("When winner crosses finish line", RaceEndMode.WINNER),
            ("When last car crosses finish line", RaceEndMode.LAST_CAR),
            ("Only when user ends race", RaceEndMode.MANUAL),
        ]
        current_end_mode_index = next(
            (
                idx
                for idx, (_label, mode) in enumerate(end_mode_options)
                if mode == self.race_end_mode
            ),
            1,
        )
        end_mode_combo = Gtk.DropDown.new_from_strings(
            [label for label, _mode in end_mode_options]
        )
        end_mode_combo.set_selected(current_end_mode_index)
        end_mode_row.append(end_mode_combo)
        root.append(end_mode_row)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        button_row.append(cancel_btn)
        button_row.append(save_btn)
        root.append(button_row)
        dialog.set_child(root)

        def close_preferences(save: bool) -> None:
            if save:
                new_total_laps = int(laps_spin.get_value_as_int())
                selected_idx = end_mode_combo.get_selected()
                if selected_idx < 0 or selected_idx >= len(end_mode_options):
                    selected_idx = 1
                new_end_mode = end_mode_options[selected_idx][1]

                # Next-race config only; the running race is owned by the
                # recorder and unaffected until the next start.
                self.total_laps = new_total_laps
                self.race_end_mode = new_end_mode

                self.save_config()
                self.refresh_views()
                self.append_event(
                    f"Preferences saved: regular race laps = {new_total_laps}, end mode = {new_end_mode.value}"
                )
            dialog.destroy()

        cancel_btn.connect("clicked", lambda _button: close_preferences(False))
        save_btn.connect("clicked", lambda _button: close_preferences(True))
        dialog.present()

    def show_keyboard_shortcuts_dialog(self) -> None:
        if not self.window:
            return

        dialog = Gtk.Dialog(
            title="Keyboard Shortcuts", transient_for=self.window, modal=True
        )
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)

        shortcuts = [
            ("?", "Show keyboard shortcuts"),
            ("Ctrl+S", "Start race"),
            ("Ctrl+E", "End race"),
            ("Ctrl+Shift+R", "Reset race"),
            ("Ctrl+T", "Toggle race mode"),
            ("Ctrl+L", "Toggle event log"),
            ("Ctrl+R", "Manage drivers"),
            ("Ctrl+,", "Preferences"),
        ]

        grid = Gtk.Grid()
        grid.set_column_spacing(24)
        grid.set_row_spacing(8)
        for row, (shortcut, description) in enumerate(shortcuts):
            shortcut_label = Gtk.Label(label=shortcut)
            shortcut_label.set_xalign(0)
            description_label = Gtk.Label(label=description)
            description_label.set_xalign(0)
            grid.attach(shortcut_label, 0, row, 1, 1)
            grid.attach(description_label, 1, row, 1, 1)

        close_btn = Gtk.Button(label="Close")
        close_btn.set_halign(Gtk.Align.END)
        close_btn.connect("clicked", lambda _button: dialog.destroy())

        def on_key_pressed(
            _controller: Gtk.EventControllerKey,
            keyval: int,
            _keycode: int,
            _state: Gdk.ModifierType,
        ) -> bool:
            if keyval == Gdk.KEY_Escape:
                dialog.destroy()
                return True
            return False

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", on_key_pressed)
        dialog.add_controller(key_controller)

        root.append(grid)
        root.append(close_btn)
        dialog.set_child(root)
        dialog.present()

    def on_manage_drivers_clicked(self, _button: Gtk.Button | None) -> None:
        if not self.window:
            return

        dialog = Gtk.Dialog(
            title="Manage Drivers", transient_for=self.window, modal=True
        )
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(8)
        root.set_margin_end(8)

        help_text = Gtk.Label(
            label="Edit names/colors, add or delete drivers. Use the color pickers for primary/secondary colors. Enter=Add, Ctrl+Enter=Save, Ctrl+D=Delete focused driver, Esc=Cancel."
        )
        help_text.set_xalign(0)
        root.append(help_text)

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_id_entry = Gtk.Entry()
        add_id_entry.set_placeholder_text("Transmitter ID")
        add_id_entry.set_width_chars(12)
        add_name_entry = Gtk.Entry()
        add_name_entry.set_placeholder_text("Driver name")
        add_name_entry.set_hexpand(True)
        add_btn = Gtk.Button(label="Add")
        add_row.append(add_id_entry)
        add_row.append(add_name_entry)
        add_row.append(add_btn)
        root.append(add_row)

        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(280)
        scroll.set_child(list_box)
        root.append(scroll)

        status_label = Gtk.Label(label="")
        status_label.set_xalign(0)
        root.append(status_label)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        button_row.append(cancel_btn)
        button_row.append(save_btn)
        root.append(button_row)
        dialog.set_child(root)

        staged: dict[int, str] = {
            c.transmitter_id: c.name for c in self.global_contestants.contestants
        }
        self._ensure_racer_color_assignments(set(staged.keys()), persist=False)
        staged_colors: dict[int, RacerColorScheme] = {
            tid: self.racer_color_assignments[tid]
            for tid in staged.keys()
            if tid in self.racer_color_assignments
        }
        focused_driver_id: int | None = None

        def set_status(msg: str) -> None:
            status_label.set_text(msg)

        def set_focused_driver(tid: int | None) -> None:
            nonlocal focused_driver_id
            focused_driver_id = tid

        def delete_driver_by_id(tid: int) -> bool:
            if tid not in staged:
                return False
            staged.pop(tid, None)
            staged_colors.pop(tid, None)
            refresh_driver_rows()
            set_status(f"Deleted driver {tid}")
            return True

        def refresh_driver_rows() -> None:
            child = list_box.get_first_child()
            while child is not None:
                next_child = child.get_next_sibling()
                list_box.remove(child)
                child = next_child

            nonlocal focused_driver_id

            for transmitter_id in sorted(staged.keys()):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

                id_label = Gtk.Label(label=str(transmitter_id))
                id_label.set_width_chars(8)
                id_label.set_xalign(0)

                name_entry = Gtk.Entry()
                name_entry.set_hexpand(True)
                name_entry.set_text(staged[transmitter_id])

                def on_name_changed(
                    entry: Gtk.Entry, tid: int = transmitter_id
                ) -> None:
                    staged[tid] = entry.get_text().strip()

                name_entry.connect("changed", on_name_changed)

                primary_color, secondary_color = staged_colors.get(
                    transmitter_id, ("#777777", "#bbbbbb")
                )
                swatch = self._new_color_swatch_for_colors(
                    primary_color, secondary_color
                )

                def on_primary_changed(new_hex: str, tid: int = transmitter_id) -> None:
                    current_primary, current_secondary = staged_colors.get(
                        tid, ("#777777", "#bbbbbb")
                    )
                    staged_colors[tid] = (new_hex, current_secondary)
                    refresh_driver_rows()
                    set_focused_driver(tid)

                def on_secondary_changed(
                    new_hex: str, tid: int = transmitter_id
                ) -> None:
                    current_primary, current_secondary = staged_colors.get(
                        tid, ("#777777", "#bbbbbb")
                    )
                    staged_colors[tid] = (current_primary, new_hex)
                    refresh_driver_rows()
                    set_focused_driver(tid)

                primary_picker = self._new_color_picker_button(
                    primary_color,
                    on_primary_changed,
                )
                secondary_picker = self._new_color_picker_button(
                    secondary_color,
                    on_secondary_changed,
                )

                name_focus = Gtk.EventControllerFocus()
                name_focus.connect(
                    "enter", lambda _ctrl, tid: set_focused_driver(tid), transmitter_id
                )
                name_entry.add_controller(name_focus)

                delete_btn = Gtk.Button(label="Delete")

                delete_focus = Gtk.EventControllerFocus()
                delete_focus.connect(
                    "enter", lambda _ctrl, tid: set_focused_driver(tid), transmitter_id
                )
                delete_btn.add_controller(delete_focus)

                def on_delete_clicked(
                    _btn: Gtk.Button, tid: int = transmitter_id
                ) -> None:
                    set_focused_driver(tid)
                    delete_driver_by_id(tid)

                delete_btn.connect("clicked", on_delete_clicked)

                row.append(id_label)
                row.append(name_entry)
                row.append(swatch)
                row.append(primary_picker)
                row.append(secondary_picker)
                row.append(delete_btn)
                list_box.append(row)

            if focused_driver_id is not None and focused_driver_id not in staged:
                focused_driver_id = None

        def add_current_driver() -> None:
            raw_id = add_id_entry.get_text().strip()
            name = add_name_entry.get_text().strip()
            if not raw_id:
                set_status("Enter a transmitter ID")
                return
            if not name:
                set_status("Enter a driver name")
                return

            try:
                transmitter_id = int(raw_id)
            except ValueError:
                set_status("Transmitter ID must be a number")
                return

            if transmitter_id in staged:
                set_status(f"Driver {transmitter_id} already exists; name updated")
            else:
                set_status(f"Added driver {transmitter_id}")

            staged[transmitter_id] = name
            self._ensure_racer_color_assignments({transmitter_id}, persist=False)
            staged_colors[transmitter_id] = self.racer_color_assignments.get(
                transmitter_id, ("#777777", "#bbbbbb")
            )
            add_id_entry.set_text("")
            add_name_entry.set_text("")
            refresh_driver_rows()
            add_id_entry.grab_focus()

        def on_add_clicked(_btn: Gtk.Button | None) -> None:
            add_current_driver()

        add_btn.connect("clicked", on_add_clicked)
        add_id_entry.connect("activate", lambda _entry: on_add_clicked(None))
        add_name_entry.connect("activate", lambda _entry: on_add_clicked(None))

        def save_drivers() -> None:
            cleaned = {
                tid: name.strip() for tid, name in staged.items() if name.strip()
            }

            validated_colors: dict[int, RacerColorScheme] = {
                tid: staged_colors.get(tid, ("#777777", "#bbbbbb"))
                for tid in sorted(cleaned.keys())
            }

            self.global_contestants.contestants = [
                Contestant(transmitter_id=tid, name=cleaned[tid])
                for tid in sorted(cleaned.keys())
            ]

            # Keep colors for active/recent racers, and update edited driver colors.
            kept_ids = set(cleaned.keys())
            kept_ids.update(self._snapshot_racer_ids())
            kept_ids.update(self.last_race_contestant_ids)

            self.racer_color_assignments = {
                tid: colors
                for tid, colors in self.racer_color_assignments.items()
                if tid in kept_ids
            }
            self.racer_color_assignments.update(validated_colors)

            self.save_config()
            self.refresh_views()
            self.append_event(
                f"Saved driver updates ({len(self.global_contestants.contestants)} total)"
            )
            dialog.destroy()

        def on_key_pressed(
            _controller: Gtk.EventControllerKey,
            keyval: int,
            _keycode: int,
            state: Gdk.ModifierType,
        ) -> bool:
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and (
                state & Gdk.ModifierType.CONTROL_MASK
            ):
                save_drivers()
                return True
            if keyval in (Gdk.KEY_d, Gdk.KEY_D) and (
                state & Gdk.ModifierType.CONTROL_MASK
            ):
                if focused_driver_id is None:
                    set_status("Focus a driver row first, then press Ctrl+D")
                    return True
                if not delete_driver_by_id(focused_driver_id):
                    set_status("Focused driver is no longer available")
                return True
            if keyval == Gdk.KEY_Escape:
                dialog.destroy()
                return True
            return False

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", on_key_pressed)
        dialog.add_controller(key_controller)
        cancel_btn.connect("clicked", lambda _button: dialog.destroy())
        save_btn.connect("clicked", lambda _button: save_drivers())

        refresh_driver_rows()
        add_id_entry.grab_focus()

        dialog.present()

    def upsert_contestant(self, transmitter_id: int, name: str) -> None:
        for contestant in self.global_contestants.contestants:
            if contestant.transmitter_id == transmitter_id:
                contestant.name = name
                return
        self.global_contestants.contestants.append(
            Contestant(transmitter_id=transmitter_id, name=name)
        )

    def save_config(self) -> None:
        contestants = [
            {"transmitter_id": c.transmitter_id, "name": c.name}
            for c in self.global_contestants.contestants
        ]

        last_race_contestant_ids = sorted(self.last_race_contestant_ids)

        write_config(
            self.config_path,
            race_mode=self.race_mode,
            total_laps=self.total_laps,
            race_end_mode=self.race_end_mode,
            contestants_data=contestants,
            last_race_contestant_ids=last_race_contestant_ids,
            racer_color_assignments=self.racer_color_assignments,
        )

    def connect_redis(self) -> None:
        try:
            self._redis_client = redis.Redis(
                unix_socket_path=self.redis_socket, decode_responses=True
            )
            self._redis_client.ping()
            self._redis_pubsub = self._redis_client.pubsub()
            channels = (
                self.redis_out_channel,
                self.redis_events_channel,
                self.redis_race_state_channel,
            )
            self._redis_pubsub.subscribe(*channels)
            self.append_event("Connected to Redis")
            self.append_event(f"Subscribed to Redis channels: {', '.join(channels)}")
            logging.info("Connected to Redis")
            logging.info("Subscribed to Redis channels: %s", ", ".join(channels))
            self._load_latest_snapshot()
        except Exception as exc:
            self.append_event(f"Redis connect failed: {exc}")
            logging.error("Failed to connect to Redis: %s", exc)
            return

        def reader() -> None:
            assert self._redis_pubsub is not None
            while not self._shutdown.is_set():
                try:
                    msg = self._redis_pubsub.get_message(timeout=0.1)
                    if msg and msg.get("type") == "message":
                        channel = msg.get("channel")
                        data = msg.get("data")
                        parsed = json.loads(data) if isinstance(data, str) else {}
                        if isinstance(parsed, dict) and isinstance(channel, str):
                            self._incoming_messages.put((channel, parsed))
                except Exception as exc:
                    logging.error("Redis listener error: %s", exc)
                    time.sleep(0.2)

        self._redis_thread = threading.Thread(target=reader, daemon=True)
        self._redis_thread.start()

    def _load_latest_snapshot(self) -> None:
        """Fetch the retained snapshot so late joiners render current state."""
        if not self._redis_client:
            return
        try:
            payload = self._redis_client.get(self.redis_race_state_latest_key)
        except Exception as exc:
            logging.error("Failed to read latest snapshot: %s", exc)
            return
        if not isinstance(payload, str):
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logging.error("Retained snapshot is not valid JSON")
            return
        if isinstance(data, dict):
            self.handle_snapshot(data)

    def drain_incoming_messages(self) -> bool:
        while True:
            try:
                channel, msg = self._incoming_messages.get_nowait()
            except queue.Empty:
                break
            if channel == self.redis_race_state_channel:
                self.handle_snapshot(msg)
            else:
                self.handle_hardware_message(msg)
        return True

    def handle_hardware_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        simulated = bool(msg.get("simulated", False))

        if msg_type == "heartbeat":
            self.lap_counter_detected = True
            self._last_lap_counter_signal_time = time.monotonic()
            self.refresh_views()
            return

        if msg_type == "preferences_changed":
            logging.info("Preferences changed via Redis, reloading...")
            (
                configured_mode,
                total_laps,
                race_end_mode,
                contestants_data,
                last_race_contestant_ids,
                racer_color_assignments,
            ) = load_initial_config(self.config_path)

            self.total_laps = total_laps
            self.race_mode = configured_mode
            self.race_end_mode = race_end_mode
            self.global_contestants = RaceContestants(contestants_data)
            self.last_race_contestant_ids = set(last_race_contestant_ids)
            self.racer_color_assignments = dict(racer_color_assignments)

            known_racer_ids = {
                c.transmitter_id for c in self.global_contestants.contestants
            }.union(self.last_race_contestant_ids)
            self._ensure_racer_color_assignments(known_racer_ids, persist=False)
            self.refresh_views()
            self.append_event("Preferences reloaded from database")
            return

        if msg_type == "status":
            source = "SIM" if simulated else "HW"
            self.append_event(f"STATUS [{source}]: {msg.get('message', '')}")
            return

        if msg_type == "countdown_phase":
            phase = str(msg.get("phase", "")).lower()
            at_raw = msg.get("at")
            at_epoch = (
                float(at_raw) if isinstance(at_raw, (int, float)) else time.time()
            )
            delay_ms = max(0, int((at_epoch - time.time()) * 1000))

            def apply_phase() -> bool:
                if phase == "ready":
                    self._start_sequence_running = True
                    self._set_start_sequence_phase("Ready")
                    self._set_start_light_pattern(
                        [
                            "start-light-yellow",
                            "start-light-red",
                            "start-light-red",
                            "start-light-yellow",
                        ]
                    )
                    self.append_event("Ready")
                elif phase == "set":
                    self._start_sequence_running = True
                    self._set_start_sequence_phase("Set")
                    self._set_start_lights("#f9a825")
                    self.append_event("Set")
                elif phase == "go":
                    self._set_start_sequence_phase("Go")
                    self._set_start_lights("#2e7d32")
                    self.append_event("Go")
                self.refresh_views()
                return False

            GLib.timeout_add(delay_ms, apply_phase)
            return

        if msg_type == "start_race":
            at_raw = msg.get("at")
            at_epoch = (
                float(at_raw) if isinstance(at_raw, (int, float)) else time.time()
            )
            delay_ms = max(0, int((at_epoch - time.time()) * 1000))

            # Visual only: the authoritative running race arrives via the
            # snapshot, which clears the start sequence.
            def apply_start() -> bool:
                self._set_start_sequence_phase("Go")
                self._set_start_lights("#2e7d32")
                self.append_event("Go")
                self.refresh_views()
                return False

            GLib.timeout_add(delay_ms, apply_start)
            return

        if msg_type == "race_control":
            command = str(msg.get("command", ""))
            accepted = bool(msg.get("accepted", True))
            detail = str(msg.get("message", ""))
            racer_id_raw = msg.get("racer_id")
            racer_id_i = int(racer_id_raw) if racer_id_raw is not None else None

            # Display-only: the recorder applies race-control effects and the
            # results are reflected in the next snapshot.
            self.append_event(
                f"RACE_CONTROL: {command} accepted={accepted} racer={racer_id_i} {detail}"
            )
            return

        if msg_type == "lap":
            # Laps are applied by the recorder; we just log them for the operator.
            racer_id_raw = msg.get("racer_id")
            if racer_id_raw is None:
                return
            racer_id_i = int(racer_id_raw)
            name = self.global_contestants.get_contestant_name(racer_id_i)
            source = "SIM" if simulated else "HW"
            self.append_event(f"LAP [{source}]: {name} (ID {racer_id_i})")
            return

    def publish_command(self, command: str, **kwargs: Any) -> None:
        if not self._redis_client:
            self.append_event("Redis client not initialized")
            logging.error(
                "Cannot publish command '%s': redis client not initialized", command
            )
            return

        try:
            payload = build_command_envelope(command, source="franklin_gui", **kwargs)
            validated = parse_command_envelope(payload)
            published_to = self._redis_client.publish(
                self.redis_in_channel, json.dumps(validated)
            )
            logging.info(
                "Published command '%s' to %s (subscribers=%s)",
                command,
                self.redis_in_channel,
                published_to,
            )
            if published_to == 0:
                self.append_event(
                    f"Warning: command '{command}' had no hardware subscriber"
                )
        except Exception as exc:
            logging.error("Failed to publish command '%s': %s", command, exc)
            self.append_event(f"Publish failed for {command}: {exc}")


def parse_mode_override() -> RaceMode | None:
    parser = argparse.ArgumentParser(
        description="Start Franklin GTK GUI in chosen initial mode."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--race", action="store_true", help="Start in Race Mode")
    group.add_argument("--fake", action="store_true", help="Start in Fake Race Mode")
    group.add_argument("--training", action="store_true", help="Start in Training Mode")
    args = parser.parse_args()

    if args.race:
        return RaceMode.REAL
    if args.fake:
        return RaceMode.FAKE
    if args.training:
        return RaceMode.TRAINING
    return None


def main() -> None:
    (
        configured_mode,
        total_laps,
        race_end_mode,
        contestants_data,
        last_race_contestant_ids,
        racer_color_assignments,
    ) = load_initial_config(Path("franklin.config.json"))
    mode_override = parse_mode_override()
    initial_mode = mode_override or configured_mode
    app = FranklinGuiApp(
        initial_mode=initial_mode,
        total_laps=total_laps,
        contestants_data=contestants_data,
        race_end_mode=race_end_mode,
        last_race_contestant_ids=last_race_contestant_ids,
        racer_color_assignments=racer_color_assignments,
    )
    app.run([])


if __name__ == "__main__":
    main()
