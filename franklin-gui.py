#!/usr/bin/env python3
# pyright: reportAttributeAccessIssue=false


"""
Franklin GTK GUI (Wayland-friendly) implementation.

This is the first pass of the GUI app that mirrors the core Franklin TUI flow:
- race mode selection (Real/Fake/Training)
- start/end race controls
- Redis pub/sub integration (channels/messages documented in docs/redis-message-reference.md)
- lap/event log and leaderboard rendering
- race persistence via SQLite
- basic driver management (add/rename/delete) via dialog
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

from database import LapDatabase
from gui_config import load_initial_config, write_config
from race.contestant import Contestant
from race.race import (
    Race,
    RaceEndMode,
    RaceState,
    generate_fake_race,
    is_race_going,
    make_fake_lap,
    make_lap_from_sensor_event_and_race,
    order_laps_by_occurrence,
)
from race.race_contestants import RaceContestants
from race.race_mode import RaceMode
from racer_colors import RacerColorScheme, assign_random_scheme
from redis_commands import build_command_envelope, parse_command_envelope

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import (  # pyright: ignore[reportAttributeAccessIssue]  # noqa: E402
    Gdk,
    Gio,
    GLib,
    Gtk,
)


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
        db_path: str = "lap_counter.db",
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

        # Core state
        self.total_laps = total_laps
        self.race_mode = initial_mode
        self.race_end_mode = race_end_mode
        self.global_contestants = RaceContestants(contestants_data)
        self.racer_color_assignments = dict(racer_color_assignments)
        self.previous_race: Race | None = None
        if last_race_contestant_ids:
            seeded_previous_race = Race(previous_race=None)
            seeded_previous_race.active_contestants = set(last_race_contestant_ids)
            seeded_previous_race.total_laps = total_laps
            seeded_previous_race.race_end_mode = race_end_mode
            self.previous_race = seeded_previous_race

        self.race = Race(previous_race=self.previous_race)
        self.race.total_laps = total_laps
        self.race.race_end_mode = race_end_mode

        # Referee adjustments (applied from franklin:events)
        self.racer_penalties_seconds: dict[int, int] = {}
        self.disqualified_racers: set[int] = set()

        known_racer_ids = {
            c.transmitter_id for c in self.global_contestants.contestants
        }.union(last_race_contestant_ids)
        self._ensure_racer_color_assignments(known_racer_ids, persist=False)

        # Redis contract reference: docs/redis-message-reference.md
        self.redis_socket = redis_socket
        self.redis_in_channel = "hardware:in"
        self.redis_out_channel = "hardware:out"
        self.redis_events_channel = "franklin:events"
        self._redis_client: redis.Redis | None = None
        self._redis_pubsub = None

        self.config_path = Path("franklin.config.json")
        self.db = LapDatabase(db_path)
        self.current_race_id: int | None = None

        self.lap_counter_detected = False
        self._last_lap_counter_signal_time: float | None = None

        # Threaded message processing
        self._incoming_messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._shutdown = threading.Event()
        self._redis_thread: threading.Thread | None = None
        self._fake_thread: threading.Thread | None = None

        # UI refs
        self.window: Gtk.ApplicationWindow | None = None
        self.mode_combo: Gtk.ComboBoxText | None = None
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
        self.leaderboard_scroll: Gtk.ScrolledWindow | None = None
        self.events_view: Gtk.TextView | None = None
        self.panes: Gtk.Paned | None = None
        self.events_box: Gtk.Box | None = None
        self._events_visible = False

        self._system_status_thread: threading.Thread | None = None
        self._leaderboard_css_provider = Gtk.CssProvider()
        self._leaderboard_font_pt: int | None = None

        # Start light UI (mirrored on both sides of the timer)
        self._start_lights_css_provider = Gtk.CssProvider()
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

        in_progress = self.db.get_in_progress_race()
        if in_progress:
            logging.info("Resuming in-progress race: %s", in_progress["id"])
            self.current_race_id = int(in_progress["id"])

    def _register_actions_and_shortcuts(self) -> None:
        action_defs: list[tuple[str, Any, list[str]]] = [
            ("start_race", self._action_start_race, ["<Primary>s"]),
            ("end_race", self._action_end_race, ["<Primary>e"]),
            ("reset_race", self._action_reset_race, ["<Primary><Shift>r"]),
            ("toggle_mode", self._action_toggle_mode, ["<Primary>t"]),
            ("toggle_event_log", self._action_toggle_event_log, ["<Primary>l"]),
            ("manage_drivers", self._action_manage_drivers, ["<Primary>r"]),
            ("preferences", self._action_preferences, ["<Primary>comma"]),
        ]

        for name, callback, accels in action_defs:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self.set_accels_for_action(f"app.{name}", accels)

    def _action_start_race(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_start_clicked(None)

    def _action_end_race(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_end_clicked(None)

    def _action_reset_race(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_reset_clicked(None)

    def _action_toggle_mode(self, _action: Gio.SimpleAction, _param: Any) -> None:
        if is_race_going(self.race):
            self.append_event("Cannot change mode while race is running")
            return
        modes = [RaceMode.REAL, RaceMode.FAKE, RaceMode.TRAINING]
        idx = modes.index(self.race_mode)
        next_mode = modes[(idx + 1) % len(modes)]
        self.race_mode = next_mode
        if self.mode_combo:
            self.mode_combo.set_active(modes.index(next_mode))
        self.append_event(f"Mode changed to {self.race_mode}")
        self.refresh_views()

    def _action_toggle_event_log(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.toggle_event_log_visibility()

    def _action_manage_drivers(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_manage_drivers_clicked(None)

    def _action_preferences(self, _action: Gio.SimpleAction, _param: Any) -> None:
        self.on_preferences_clicked(None)

    def do_activate(self) -> None:  # type: ignore[override]
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("Franklin Lap Counter (GTK)")
        window.set_default_size(1200, 760)
        self.window = window

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        root.set_margin_start(12)
        root.set_margin_end(12)

        time_label = Gtk.Label()
        time_label.set_markup('<span size="48000" weight="bold">00:00:00</span>')
        time_label.set_xalign(0.5)
        time_label.set_halign(Gtk.Align.CENTER)
        time_label.set_hexpand(False)
        self.time_label = time_label

        clock_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        clock_row.set_halign(Gtk.Align.CENTER)
        clock_row.set_hexpand(True)
        clock_row.set_margin_bottom(10)
        left_lights = self._create_start_light_stack()
        right_lights = self._create_start_light_stack()
        self._start_light_left_areas = left_lights
        self._start_light_right_areas = right_lights
        self._set_start_lights("#c62828")
        clock_row.append(self._wrap_start_light_stack(left_lights))
        clock_row.append(time_label)
        clock_row.append(self._wrap_start_light_stack(right_lights))

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_combo = Gtk.ComboBoxText()
        mode_options = [RaceMode.REAL, RaceMode.FAKE, RaceMode.TRAINING]
        for mode in mode_options:
            mode_combo.append_text(mode.value)
        mode_combo.set_active(mode_options.index(self.race_mode))
        mode_combo.connect("changed", self.on_mode_changed)
        self.mode_combo = mode_combo

        start_btn = Gtk.Button(label="Start Race (Ctrl+S)")
        start_btn.connect("clicked", self.on_start_clicked)
        self.start_btn = start_btn

        stop_btn = Gtk.Button(label="End Race (Ctrl+E)")
        stop_btn.set_sensitive(False)
        stop_btn.connect("clicked", self.on_end_clicked)
        self.stop_btn = stop_btn

        reset_btn = Gtk.Button(label="Reset (Ctrl+Shift+R)")
        reset_btn.set_sensitive(False)
        reset_btn.connect("clicked", self.on_reset_clicked)
        self.reset_btn = reset_btn

        manage_drivers_btn = Gtk.Button(label="Manage Drivers (Ctrl+R)")
        manage_drivers_btn.connect("clicked", self.on_manage_drivers_clicked)

        preferences_btn = Gtk.Button(label="Preferences (Ctrl+,)")
        preferences_btn.connect("clicked", self.on_preferences_clicked)

        controls.append(Gtk.Label(label="Mode (Ctrl+T):"))
        controls.append(mode_combo)
        controls.append(start_btn)
        controls.append(stop_btn)
        controls.append(reset_btn)
        controls.append(manage_drivers_btn)
        controls.append(preferences_btn)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        state_label = Gtk.Label(
            label=f"State: {self._humanize_race_state(self.race.state)}"
        )
        laps_remaining_label = Gtk.Label(label=f"Laps Remaining: {self.total_laps}")
        self.state_label = state_label
        self.laps_remaining_label = laps_remaining_label
        for lbl in [state_label, laps_remaining_label]:
            lbl.set_xalign(0)
            status.append(lbl)

        panes = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        panes.set_vexpand(True)
        panes.set_hexpand(True)
        self.panes = panes

        leaderboard_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        leaderboard_label = Gtk.Label()
        leaderboard_label.set_markup(
            '<span size="42000" weight="bold">Leaderboard</span>'
        )
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
                self._leaderboard_css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            Gtk.StyleContext.add_provider_for_display(
                display,
                self._start_lights_css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        self._start_lights_css_provider.load_from_data(
            b"""
            .start-light {
                border-radius: 999px;
                border: 2px solid #141414;
                background-color: #c62828;
            }
            .start-light-red {
                background-color: #c62828;
            }
            .start-light-yellow {
                background-color: #f9a825;
            }
            .start-light-green {
                background-color: #2e7d32;
            }
            """
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

        status_bar.append(detect_label)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(ethernet_label)
        status_bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        status_bar.append(wifi_label)

        root.append(clock_row)
        root.append(controls)
        root.append(status)
        root.append(panes)
        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        root.append(status_bar)

        window.set_child(root)
        window.present()

        self.connect_redis()
        GLib.timeout_add(100, self.update_time)
        GLib.timeout_add(50, self.drain_incoming_messages)

        self.toggle_event_log_visibility(show=False)
        self._start_system_status_updater()

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
        self.db.close()
        super().do_shutdown()

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
        if is_race_going(self.race):
            self._set_start_lights("#2e7d32")
        else:
            self._set_start_lights("#c62828")

    def _set_start_sequence_phase(self, phase: str | None) -> None:
        self._start_sequence_phase = phase

        if not self.state_label:
            return

        if phase is None:
            self.state_label.set_text(
                f"State: {self._humanize_race_state(self.race.state)}"
            )
            return

        self.state_label.set_text(f"State: Starting - {phase}")

    def _update_start_light_size(self) -> None:
        if not self.time_label or not self.window:
            return

        timer_height = self.time_label.get_allocated_height()
        timer_width = self.time_label.get_allocated_width()
        window_width = self.window.get_allocated_width()
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

        if self.start_btn:
            self.start_btn.set_sensitive(False)
        if self.stop_btn:
            self.stop_btn.set_sensitive(False)
        if self.reset_btn:
            self.reset_btn.set_sensitive(False)

        if self.race_mode == RaceMode.FAKE:

            def set_yellow() -> bool:
                if not self._start_sequence_running:
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
                self.append_event("Ready")
                return False

            def set_all_yellow() -> bool:
                if not self._start_sequence_running:
                    return False
                self._set_start_sequence_phase("Set")
                self._set_start_lights("#f9a825")
                self.append_event("Set")
                return False

            def set_green_and_start() -> bool:
                if not self._start_sequence_running:
                    return False
                self._set_start_sequence_phase("Go")
                self._set_start_lights("#2e7d32")
                self.append_event("Go")
                self._start_sequence_running = False
                self._start_race_now()
                return False

            GLib.timeout_add(1000, set_yellow)
            GLib.timeout_add(2000, set_all_yellow)
            GLib.timeout_add(3000, set_green_and_start)
            return

        if not self._redis_client:
            self.append_event("Redis not connected; cannot schedule race start")
            self._start_sequence_running = False
            if self.start_btn:
                self.start_btn.set_sensitive(True)
            return

        base = time.time() + 0.25
        ready_at = base
        set_at = base + 1.0
        go_at = base + 2.0

        self.publish_command(
            "start_race",
            ready_at=ready_at,
            set_at=set_at,
            go_at=go_at,
            start_at=go_at,
        )
        self.append_event(f"Scheduled countdown (go at {go_at:.3f})")

        # Local visual countdown preview for GUI reliability. If Redis timeline
        # events arrive, those handlers will keep this in sync.
        def show_ready_local() -> bool:
            if not self._start_sequence_running or is_race_going(self.race):
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
            if not self._start_sequence_running or is_race_going(self.race):
                return False
            self._set_start_sequence_phase("Set")
            self._set_start_lights("#f9a825")
            self.refresh_views()
            return False

        def show_go_local() -> bool:
            if not self._start_sequence_running or is_race_going(self.race):
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

        # Fallback: if no start signal arrives from Redis timeline, start locally
        # so GUI operation is resilient to command-path issues.
        fallback_delay_ms = max(0, go_delay_ms + 250)

        def fallback_start_if_missing() -> bool:
            if self._start_sequence_running and not is_race_going(self.race):
                logging.warning(
                    "Start fallback triggered: no start_race timeline received in time"
                )
                self.append_event("Start fallback triggered (local start)")
                self._start_sequence_running = False
                self._start_race_now()
            return False

        GLib.timeout_add(fallback_delay_ms, fallback_start_if_missing)

    def _start_race_now(self) -> None:
        if is_race_going(self.race):
            return

        self.racer_penalties_seconds.clear()
        self.disqualified_racers.clear()

        self.race = Race(previous_race=self.previous_race)
        self.race.total_laps = self.total_laps
        self.race.race_end_mode = self.race_end_mode
        self.race.start(start_time=time.monotonic())

        self.current_race_id = self.db.create_race(
            notes=f"Mode: {self.race_mode}, Total Laps: {self.total_laps}"
        )

        if self.stop_btn:
            self.stop_btn.set_sensitive(True)
        if self.reset_btn:
            self.reset_btn.set_sensitive(False)

        self._set_start_sequence_phase(None)
        self.append_event("Race started")
        if self.race_mode == RaceMode.FAKE:
            self.start_fake_playback()

        self.refresh_views()

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

    def _set_leaderboard_font_size(self, point_size: int) -> None:
        if point_size == self._leaderboard_font_pt:
            return

        css = (
            ".leaderboard-cell, .leaderboard-header-cell { "
            f"font-size: {point_size}pt; "
            "font-family: monospace; "
            "}"
            ".leaderboard-status-cell {"
            f"font-size: {point_size}pt; "
            "font-family: 'Noto Color Emoji', monospace;"
            "}"
            ".leaderboard-header-cell {"
            "font-weight: 700;"
            "background-color: #ececec;"
            "}"
            ".leaderboard-best-col { background-color: #eaf4ff; }"
            ".leaderboard-last-col { background-color: #fff9e8; }"
            ".leaderboard-total-col { background-color: #ffecec; }"
        )
        self._leaderboard_css_provider.load_from_data(css.encode("utf-8"))
        self._leaderboard_font_pt = point_size

    def _update_leaderboard_font_size(self, racer_count: int) -> None:
        if not self.leaderboard_scroll:
            return

        min_pt = 14
        max_pt = 28
        allocated_height = self.leaderboard_scroll.get_allocated_height()

        # First render pass can be 0; keep a reasonable default.
        if allocated_height <= 0:
            self._set_leaderboard_font_size(20)
            return

        rows_to_fit = max(10, racer_count) + 1  # +1 for header line
        estimated_pt = int(allocated_height / (rows_to_fit * 1.6))
        target_pt = max(min_pt, min(max_pt, estimated_pt))
        self._set_leaderboard_font_size(target_pt)

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

    def _leaderboard_name_col_width(self) -> int:
        # Non-name characters in:
        # {pos:>3}  {status:^6} {name:<N} {laps:>4}  {best:>8}  {last:>8}  {total:>8}
        non_name_chars = 48
        min_name_chars = 12
        max_name_chars = 48

        if not self.leaderboard_scroll:
            return 20

        width_px = self.leaderboard_scroll.get_allocated_width()
        if width_px <= 0:
            return 20

        # Approx monospace character width based on current leaderboard font size.
        pt = self._leaderboard_font_pt or 20
        approx_char_px = max(7.0, pt * 0.83)
        total_chars = int((width_px - 12) / approx_char_px)
        target = total_chars - non_name_chars
        return max(min_name_chars, min(max_name_chars, target))

    def _humanize_race_state(self, state: RaceState) -> str:
        return state.name.replace("_", " ").title()

    def _leaderboard_status_symbol(self, position: int, lap_count: int) -> str:
        if self.race.state == RaceState.FINISHED:
            if position == 1:
                return "🥇"
            if position == 2:
                return "🥈"
            if position == 3:
                return "🥉"
            return ""

        if self.race.state == RaceState.WINNER_DECLARED and position == 1:
            return "🏁"

        if lap_count == (self.total_laps - 1):
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
        rgba = self._hex_to_rgba(initial_hex)

        # Prefer GTK4 ColorDialogButton when available.
        color_dialog_cls = getattr(Gtk, "ColorDialog", None)
        color_dialog_button_cls = getattr(Gtk, "ColorDialogButton", None)
        if color_dialog_cls is not None and color_dialog_button_cls is not None:
            try:
                color_dialog = color_dialog_cls()
                button = color_dialog_button_cls.new(color_dialog)
                button.set_rgba(rgba)
                button.connect(
                    "notify::rgba",
                    lambda b, _pspec: on_hex_changed(self._rgba_to_hex(b.get_rgba())),
                )
                return button
            except Exception:
                pass

        # Fallback for environments exposing ColorButton API.
        color_button_cls = getattr(Gtk, "ColorButton", None)
        if color_button_cls is not None:
            try:
                button = color_button_cls.new()
                button.set_rgba(rgba)
                button.connect(
                    "color-set",
                    lambda b: on_hex_changed(self._rgba_to_hex(b.get_rgba())),
                )
                return button
            except Exception:
                pass

        # Generic fallback: open a color chooser dialog from a button.
        chooser_dialog_cls = getattr(Gtk, "ColorChooserDialog", None)
        if chooser_dialog_cls is not None and self.window is not None:
            button = Gtk.Button(label="Pick")

            def on_pick_clicked(_btn: Gtk.Button) -> None:
                dialog = chooser_dialog_cls(
                    title="Pick color",
                    transient_for=self.window,
                    modal=True,
                )
                if hasattr(dialog, "set_rgba"):
                    dialog.set_rgba(rgba)

                def on_response(d: Gtk.Dialog, response: int) -> None:
                    if response == Gtk.ResponseType.OK and hasattr(d, "get_rgba"):
                        new_rgba = d.get_rgba()
                        on_hex_changed(self._rgba_to_hex(new_rgba))
                    d.destroy()

                dialog.connect("response", on_response)
                dialog.present()

            button.connect("clicked", on_pick_clicked)
            return button

        # Last-resort fallback: keep a compact text indicator button.
        button = Gtk.Button(label=initial_hex.upper())
        button.set_sensitive(False)
        return button

    def _apply_widget_background(self, widget: Gtk.Widget, color_hex: str) -> None:
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(f"* {{ background-color: {color_hex}; }}".encode())
        widget.get_style_context().add_provider(
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _new_color_swatch_for_colors(
        self, primary_hex: str, secondary_hex: str
    ) -> Gtk.Widget:
        swatch = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        swatch.set_size_request(34, 20)
        swatch.set_hexpand(False)
        swatch.set_vexpand(False)
        swatch.set_halign(Gtk.Align.CENTER)
        swatch.set_valign(Gtk.Align.CENTER)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        top.set_size_request(34, 7)
        middle = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        middle.set_size_request(34, 6)
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bottom.set_size_request(34, 7)

        self._apply_widget_background(top, primary_hex)
        self._apply_widget_background(middle, secondary_hex)
        self._apply_widget_background(bottom, primary_hex)

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
    ) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_xalign(xalign)
        label.add_css_class(css_class)
        if extra_css_classes:
            for extra_css_class in extra_css_classes:
                label.add_css_class(extra_css_class)
        label.set_hexpand(hexpand)
        return label

    def _referee_adjusted_leaderboard(
        self,
    ) -> list[tuple[str, int, int, float, float, float, bool]]:
        base = self.race.leaderboard()
        active_rows: list[tuple[int, int, float, float, float]] = []
        dq_rows: list[tuple[int, int, float, float, float]] = []

        for _pos, racer_id, lap_count, best, last, total in base:
            adjusted_total = total + float(
                self.racer_penalties_seconds.get(racer_id, 0)
            )
            if racer_id in self.disqualified_racers:
                dq_rows.append((racer_id, lap_count, best, last, adjusted_total))
            else:
                active_rows.append((racer_id, lap_count, best, last, adjusted_total))

        active_rows.sort(
            key=lambda row: (
                -row[1],  # lap_count desc
                row[4],  # adjusted total asc
                row[2],  # best lap asc
                row[0],  # stable tie-breaker
            )
        )

        adjusted: list[tuple[str, int, int, float, float, float, bool]] = []
        for idx, (racer_id, lap_count, best, last, adjusted_total) in enumerate(
            active_rows, start=1
        ):
            adjusted.append(
                (str(idx), racer_id, lap_count, best, last, adjusted_total, False)
            )

        for racer_id, lap_count, best, last, adjusted_total in sorted(
            dq_rows, key=lambda row: row[0]
        ):
            adjusted.append(
                ("DQ", racer_id, lap_count, best, last, adjusted_total, True)
            )

        return adjusted

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
        name_w = self._leaderboard_name_col_width()

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
            else:
                status_symbol = self._leaderboard_status_symbol(
                    int(pos_label), lap_count
                )
            best_s = self._format_time_cs(best)
            last_s = self._format_time_cs(last)
            total_s = self._format_time_cs(total)

            row_values: list[tuple[str, float, bool, list[str]]] = [
                (pos_label, 1.0, False, []),
                (status_symbol, 0.5, False, ["leaderboard-status-cell"]),
                ("", 0.5, False, []),
                (name[:name_w], 0.0, True, []),
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
                )
                if col == 1:
                    cell_label.set_size_request(status_col_width_px, -1)
                self.leaderboard_grid.attach(cell_label, col, row_index, 1, 1)

        self._update_leaderboard_font_size(len(leaderboard_data))

    def refresh_views(self) -> None:
        self._sync_start_lights_with_race_state()

        if self.state_label:
            if self._start_sequence_phase is not None:
                self.state_label.set_text(
                    f"State: Starting - {self._start_sequence_phase}"
                )
            else:
                self.state_label.set_text(
                    f"State: {self._humanize_race_state(self.race.state)}"
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
            if self.race_mode == RaceMode.TRAINING:
                self.laps_remaining_label.set_visible(False)
            else:
                self.laps_remaining_label.set_visible(True)
                if self._start_sequence_running:
                    laps_remaining = self.total_laps
                else:
                    laps_remaining, _ = self.race.laps_remaining()
                self.laps_remaining_label.set_text(f"Laps Remaining: {laps_remaining}")

        self._render_leaderboard_grid()

    def update_time(self) -> bool:
        self._update_start_light_size()

        if is_race_going(self.race) and self.race.start_time is not None:
            self.race.elapsed_time = time.monotonic() - self.race.start_time
        if self.time_label:
            self.time_label.set_markup(
                f'<span size="48000" weight="bold">{self._format_time_cs(self.race.elapsed_time)}</span>'
            )

        return True

    def on_mode_changed(self, combo: Gtk.ComboBoxText) -> None:
        mode_options = [RaceMode.REAL, RaceMode.FAKE, RaceMode.TRAINING]
        selected_idx = combo.get_active()
        if selected_idx < 0 or selected_idx >= len(mode_options):
            selected_idx = mode_options.index(RaceMode.TRAINING)
        self.race_mode = mode_options[selected_idx]
        self.save_config()
        self.append_event(f"Mode changed to {self.race_mode}")

    def on_start_clicked(self, _button: Gtk.Button | None) -> None:
        if self._start_sequence_running or is_race_going(self.race):
            return

        # Build next-race lineup immediately from previous race contestants,
        # with fresh stats/time for pre-race display.
        self.race = Race(previous_race=self.previous_race)
        self.race.total_laps = self.total_laps
        self.race.race_end_mode = self.race_end_mode
        if self.time_label:
            self.time_label.set_markup(
                f'<span size="48000" weight="bold">{self._format_time_cs(self.race.elapsed_time)}</span>'
            )

        self._start_race_countdown()
        self.refresh_views()

    def _finalize_finished_race(
        self,
        *,
        message: str,
        publish_end_command: bool,
    ) -> None:
        self.previous_race = self.race

        if self.current_race_id:
            self.db.end_race(self.current_race_id)
            self.current_race_id = None

        if self.start_btn:
            self.start_btn.set_sensitive(True)
        if self.stop_btn:
            self.stop_btn.set_sensitive(False)
        if self.reset_btn:
            self.reset_btn.set_sensitive(True)

        if publish_end_command and self.race_mode != RaceMode.FAKE:
            self.publish_command("end_race")

        self.save_config()
        self.append_event(message)
        self.refresh_views()

    def on_end_clicked(self, _button: Gtk.Button | None) -> None:
        if not is_race_going(self.race):
            return

        self.race.state = RaceState.FINISHED
        self._finalize_finished_race(
            message="Race ended",
            publish_end_command=True,
        )

    def _apply_remove_lap_from_redis(
        self, racer_id: int, lap_number: int | None = None
    ) -> None:
        target_index: int | None = None
        target_lap_label = (
            f"lap {lap_number}" if lap_number is not None else "latest lap"
        )

        for idx in range(len(self.race.laps) - 1, -1, -1):
            lap = self.race.laps[idx]
            if lap.racer_id != racer_id or lap.lap_number <= 0:
                continue
            if lap_number is not None and lap.lap_number != lap_number:
                continue
            target_index = idx
            break

        if target_index is None:
            self.append_event(
                f"REMOVE LAP: racer {racer_id} {target_lap_label} not found"
            )
            return

        removed = self.race.laps.pop(target_index)

        if self.current_race_id:
            try:
                _ = self.db.remove_lap(self.current_race_id, racer_id, lap_number)
            except Exception as exc:
                logging.error("Failed to remove lap in DB: %s", exc)

        self.append_event(
            f"REMOVE LAP: racer {racer_id} removed lap {removed.lap_number}"
        )
        self.refresh_views()

    def _apply_race_reset(self, source: str = "local") -> None:
        self.racer_penalties_seconds.clear()
        self.disqualified_racers.clear()

        # Keep prior racers visible for the next race with reset stats.
        self.race = Race(previous_race=self.previous_race)
        self.race.total_laps = self.total_laps
        self.race.race_end_mode = self.race_end_mode

        if self.current_race_id:
            try:
                self.db.end_race(self.current_race_id)
            except Exception as exc:
                logging.error("Failed to end race during reset: %s", exc)
            self.current_race_id = None

        if self.start_btn:
            self.start_btn.set_sensitive(True)
        if self.stop_btn:
            self.stop_btn.set_sensitive(False)
        if self.reset_btn:
            self.reset_btn.set_sensitive(False)

        if self.time_label:
            self.time_label.set_markup(
                f'<span size="48000" weight="bold">{self._format_time_cs(self.race.elapsed_time)}</span>'
            )

        self.append_event(f"Race reset ({source})")
        self.refresh_views()

    def on_reset_clicked(self, _button: Gtk.Button | None) -> None:
        if self._start_sequence_running or self.race.state != RaceState.FINISHED:
            return

        if self.race_mode == RaceMode.FAKE:
            self._apply_race_reset(source="local")
            return

        self.publish_command("reset_race")
        self.append_event("Requested race reset")

    def on_preferences_clicked(self, _button: Gtk.Button | None) -> None:
        if not self.window:
            return

        dialog = Gtk.Dialog(title="Preferences", transient_for=self.window, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)

        content = dialog.get_content_area()
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
        end_mode_combo = Gtk.ComboBoxText()
        end_mode_options: list[tuple[str, RaceEndMode]] = [
            ("When winner crosses finish line", RaceEndMode.WINNER),
            ("When last car crosses finish line", RaceEndMode.LAST_CAR),
            ("Only when user ends race", RaceEndMode.MANUAL),
        ]
        for label, _mode in end_mode_options:
            end_mode_combo.append_text(label)
        current_end_mode_index = next(
            (
                idx
                for idx, (_label, mode) in enumerate(end_mode_options)
                if mode == self.race_end_mode
            ),
            1,
        )
        end_mode_combo.set_active(current_end_mode_index)
        end_mode_row.append(end_mode_combo)
        root.append(end_mode_row)

        content.append(root)

        def on_response(d: Gtk.Dialog, response: int) -> None:
            if response == Gtk.ResponseType.OK:
                new_total_laps = int(laps_spin.get_value_as_int())
                selected_idx = end_mode_combo.get_active()
                if selected_idx < 0 or selected_idx >= len(end_mode_options):
                    selected_idx = 1
                new_end_mode = end_mode_options[selected_idx][1]

                self.total_laps = new_total_laps
                self.race_end_mode = new_end_mode
                if not is_race_going(self.race):
                    self.race.total_laps = new_total_laps
                    self.race.race_end_mode = new_end_mode

                self.save_config()
                self.refresh_views()
                self.append_event(
                    f"Preferences saved: regular race laps = {new_total_laps}, end mode = {new_end_mode.value}"
                )
            d.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def on_manage_drivers_clicked(self, _button: Gtk.Button | None) -> None:
        if not self.window:
            return

        dialog = Gtk.Dialog(
            title="Manage Drivers", transient_for=self.window, modal=True
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save", Gtk.ResponseType.OK)

        content = dialog.get_content_area()
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

        content.append(root)

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

        def on_key_pressed(
            _controller: Gtk.EventControllerKey,
            keyval: int,
            _keycode: int,
            state: Gdk.ModifierType,
        ) -> bool:
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and (
                state & Gdk.ModifierType.CONTROL_MASK
            ):
                dialog.response(Gtk.ResponseType.OK)
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
                dialog.response(Gtk.ResponseType.CANCEL)
                return True
            return False

        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", on_key_pressed)
        dialog.add_controller(key_controller)

        refresh_driver_rows()
        add_id_entry.grab_focus()

        def on_response(d: Gtk.Dialog, response: int) -> None:
            if response == Gtk.ResponseType.OK:
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

                # Keep colors for active/previous racers, and update edited driver colors.
                kept_ids = set(cleaned.keys())
                kept_ids.update(self.race.active_contestants)
                if self.previous_race is not None:
                    kept_ids.update(self.previous_race.active_contestants)

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
            d.destroy()

        dialog.connect("response", on_response)
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

        if self.previous_race is not None:
            last_race_contestant_ids = sorted(self.previous_race.active_contestants)
        else:
            last_race_contestant_ids = sorted(self.race.active_contestants)

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
            self._redis_pubsub.subscribe(
                self.redis_out_channel, self.redis_events_channel
            )
            self.append_event("Connected to Redis")
            self.append_event(
                f"Subscribed to Redis channels: {self.redis_out_channel}, {self.redis_events_channel}"
            )
            logging.info("Connected to Redis")
            logging.info(
                "Subscribed to Redis channels: %s, %s",
                self.redis_out_channel,
                self.redis_events_channel,
            )
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
                        data = msg.get("data")
                        parsed = json.loads(data) if isinstance(data, str) else {}
                        if isinstance(parsed, dict):
                            self._incoming_messages.put(parsed)
                except Exception as exc:
                    logging.error("Redis listener error: %s", exc)
                    time.sleep(0.2)

        self._redis_thread = threading.Thread(target=reader, daemon=True)
        self._redis_thread.start()

    def drain_incoming_messages(self) -> bool:
        while True:
            try:
                msg = self._incoming_messages.get_nowait()
            except queue.Empty:
                break
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

            def apply_start() -> bool:
                self._start_sequence_running = False
                self._start_race_now()
                return False

            GLib.timeout_add(delay_ms, apply_start)
            return

        if msg_type == "race_control":
            command = str(msg.get("command", ""))
            accepted = bool(msg.get("accepted", True))
            detail = str(msg.get("message", ""))
            racer_id_raw = msg.get("racer_id")
            racer_id_i = int(racer_id_raw) if racer_id_raw is not None else None

            self.append_event(
                f"RACE_CONTROL: {command} accepted={accepted} racer={racer_id_i} {detail}"
            )

            if accepted and command == "reset_race":
                self._apply_race_reset(source="redis")
            elif accepted and command == "add_penalty" and racer_id_i is not None:
                penalty_seconds = int(msg.get("penalty_seconds", 0) or 0)
                if penalty_seconds > 0:
                    self.racer_penalties_seconds[racer_id_i] = (
                        self.racer_penalties_seconds.get(racer_id_i, 0)
                        + penalty_seconds
                    )
                    self.refresh_views()
            elif accepted and command == "disqualify_racer" and racer_id_i is not None:
                self.disqualified_racers.add(racer_id_i)
                self.refresh_views()
            elif accepted and command == "remove_lap" and racer_id_i is not None:
                lap_no_raw = msg.get("lap_number")
                lap_no = int(lap_no_raw) if lap_no_raw is not None else None
                self._apply_remove_lap_from_redis(racer_id_i, lap_no)
            return

        if msg_type != "lap":
            return

        if not is_race_going(self.race):
            logging.error("Cannot add lap - race is not running")
            self.append_event("Ignored lap: race is not running")
            return

        racer_id = msg.get("racer_id")
        lap_at = msg.get("lap_at")
        race_start_at = msg.get("race_start_at")
        recorded_at = msg.get("recorded_at")

        if racer_id is None:
            self.append_event("Invalid lap data received")
            return

        racer_id_i = int(racer_id)
        if racer_id_i in self.disqualified_racers:
            name = self.global_contestants.get_contestant_name(racer_id_i)
            self.append_event(f"Ignored lap: {name} (ID {racer_id_i}) is disqualified")
            return

        self._ensure_racer_color_assignments({racer_id_i}, persist=True)

        sensor_id_raw = msg.get("sensor_id", racer_id_i)
        sensor_id_i = int(sensor_id_raw)

        if not (
            isinstance(lap_at, (int, float)) and isinstance(race_start_at, (int, float))
        ):
            self.append_event("Invalid lap data received")
            return

        lap_at_epoch = float(lap_at)
        race_start_epoch = float(race_start_at)
        recorded_epoch = (
            float(recorded_at)
            if isinstance(recorded_at, (int, float))
            else lap_at_epoch
        )
        lap = make_lap_from_sensor_event_and_race(
            racer_id_i,
            race_start_at=race_start_epoch,
            lap_at=lap_at_epoch,
            recorded_at=recorded_epoch,
            race=self.race,
        )
        previous_state = self.race.state
        lap_accepted = self.race.add_lap(lap)
        if not lap_accepted:
            name = self.global_contestants.get_contestant_name(lap.racer_id)
            self.append_event(
                f"Ignored lap: {name} (ID {lap.racer_id}) already finished {self.total_laps} laps"
            )
            self.refresh_views()
            return

        finished_now = (
            previous_state != RaceState.FINISHED
            and self.race.state == RaceState.FINISHED
        )

        if self.current_race_id:
            lap_at_epoch = (
                float(lap_at) if isinstance(lap_at, (int, float)) else float(lap.lap_at)
            )
            race_start_epoch = (
                float(race_start_at)
                if isinstance(race_start_at, (int, float))
                else float(lap.race_start_at)
            )
            recorded_epoch = float(msg.get("recorded_at", lap_at_epoch))
            self.db.add_lap(
                race_id=self.current_race_id,
                racer_id=lap.racer_id,
                sensor_id=sensor_id_i,
                lap_number=lap.lap_number,
                lap_time=lap.lap_time if lap.lap_number > 0 else None,
                race_start_at=race_start_epoch,
                lap_at=lap_at_epoch,
                recorded_at=recorded_epoch,
            )

        if finished_now:
            self._finalize_finished_race(
                message="Race finished automatically",
                publish_end_command=True,
            )

        name = self.global_contestants.get_contestant_name(lap.racer_id)
        source = "SIM" if simulated else "HW"
        self.append_event(
            f"LAP [{source}]: {name} (ID {lap.racer_id}) lap {lap.lap_number} at {self._format_time_cs(lap.seconds_from_race_start)}"
        )
        self.refresh_views()

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

    def start_fake_playback(self) -> None:
        fake_race = generate_fake_race()
        sorted_laps = order_laps_by_occurrence(fake_race.laps)
        race_start = self.race.start_time or time.monotonic()

        def playback() -> None:
            try:
                for ts, lap in sorted_laps:
                    if self._shutdown.is_set() or not is_race_going(self.race):
                        return
                    elapsed = time.monotonic() - race_start
                    wait_time = ts - elapsed
                    if wait_time > 0:
                        time.sleep(wait_time)
                    lap_event = make_fake_lap(
                        lap.racer_id, lap.lap_number, lap.lap_time, ts
                    )
                    self._incoming_messages.put(
                        {
                            "type": "lap",
                            "racer_id": lap_event.racer_id,
                            "sensor_id": 1,
                            "race_start_at": float(lap_event.race_start_at),
                            "lap_at": float(lap_event.lap_at),
                            "recorded_at": float(lap_event.recorded_at),
                            "simulated": True,
                        }
                    )
            except Exception as exc:
                logging.error("Fake playback error: %s", exc)

        self._fake_thread = threading.Thread(target=playback, daemon=True)
        self._fake_thread.start()


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
