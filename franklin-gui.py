#!/usr/bin/env python3

"""
Franklin GTK GUI (Wayland-friendly) implementation.

This is the first pass of the GUI app that mirrors the core Franklin TUI flow:
- race mode selection (Real/Fake/Training)
- start/end race controls
- Redis hardware subscription (hardware:out)
- Redis command publish (hardware:in)
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
from gui_config import load_initial_config
from race.contestant import Contestant
from race.race import (
    Race,
    RaceEndMode,
    RaceState,
    generate_fake_race,
    is_race_going,
    make_fake_lap,
    make_lap_from_sensor_data_and_race,
    order_laps_by_occurrence,
)
from race.race_contestants import RaceContestants
from race.race_mode import RaceMode

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # pyright: ignore[reportAttributeAccessIssue]  # noqa: E402


class FranklinGuiApp(Gtk.Application):
    def __init__(
        self,
        *,
        initial_mode: RaceMode,
        total_laps: int,
        contestants_data: list[dict[str, Any]],
        race_end_mode: RaceEndMode,
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
        self.previous_race: Race | None = None
        self.race = Race(previous_race=None)
        self.race.total_laps = total_laps
        self.race.race_end_mode = race_end_mode

        self.redis_socket = redis_socket
        self.redis_in_channel = "hardware:in"
        self.redis_out_channel = "hardware:out"
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
        self.time_label: Gtk.Label | None = None
        self.state_label: Gtk.Label | None = None
        self.detect_label: Gtk.Label | None = None
        self.laps_remaining_label: Gtk.Label | None = None
        self.ethernet_label: Gtk.Label | None = None
        self.wifi_label: Gtk.Label | None = None
        self.leaderboard_view: Gtk.TextView | None = None
        self.leaderboard_scroll: Gtk.ScrolledWindow | None = None
        self.events_view: Gtk.TextView | None = None
        self.panes: Gtk.Paned | None = None
        self.events_box: Gtk.Box | None = None
        self._events_visible = False

        self._last_race_state_publish = 0.0
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

    def _action_toggle_mode(self, _action: Gio.SimpleAction, _param: Any) -> None:
        if self.race.state == RaceState.RUNNING:
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
        mode_combo.append_text(RaceMode.REAL.name)
        mode_combo.append_text(RaceMode.FAKE.name)
        mode_combo.append_text(RaceMode.TRAINING.name)
        mode_combo.set_active(
            [RaceMode.REAL, RaceMode.FAKE, RaceMode.TRAINING].index(self.race_mode)
        )
        mode_combo.connect("changed", self.on_mode_changed)
        self.mode_combo = mode_combo

        start_btn = Gtk.Button(label="Start Race (Ctrl+S)")
        start_btn.connect("clicked", self.on_start_clicked)
        self.start_btn = start_btn

        stop_btn = Gtk.Button(label="End Race (Ctrl+E)")
        stop_btn.set_sensitive(False)
        stop_btn.connect("clicked", self.on_end_clicked)
        self.stop_btn = stop_btn

        manage_drivers_btn = Gtk.Button(label="Manage Drivers (Ctrl+R)")
        manage_drivers_btn.connect("clicked", self.on_manage_drivers_clicked)

        preferences_btn = Gtk.Button(label="Preferences (Ctrl+,)")
        preferences_btn.connect("clicked", self.on_preferences_clicked)

        controls.append(Gtk.Label(label="Mode (Ctrl+T):"))
        controls.append(mode_combo)
        controls.append(start_btn)
        controls.append(stop_btn)
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

        leaderboard_view = Gtk.TextView()
        leaderboard_view.set_editable(False)
        leaderboard_view.set_monospace(True)
        self.leaderboard_view = leaderboard_view

        leaderboard_scroll = Gtk.ScrolledWindow()
        leaderboard_scroll.set_vexpand(True)
        leaderboard_scroll.set_hexpand(True)
        leaderboard_scroll.set_child(leaderboard_view)
        leaderboard_box.append(leaderboard_scroll)
        self.leaderboard_scroll = leaderboard_scroll

        leaderboard_view.add_css_class("leaderboard-view")
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
        if self.race.state == RaceState.RUNNING:
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

    def _publish_countdown_phase(self, phase: str) -> None:
        # Countdown cues are intended for real hardware races.
        if self.race_mode == RaceMode.FAKE:
            return
        if not self._redis_client:
            return

        self.publish_command("countdown_phase", phase=phase, message=phase.upper())

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
            self._publish_countdown_phase("ready")
            return False

        def set_all_yellow() -> bool:
            if not self._start_sequence_running:
                return False
            self._set_start_sequence_phase("Set")
            self._set_start_lights("#f9a825")
            self.append_event("Set")
            self._publish_countdown_phase("set")
            return False

        def set_green_and_start() -> bool:
            if not self._start_sequence_running:
                return False
            self._set_start_sequence_phase("Go")
            self._set_start_lights("#2e7d32")
            self.append_event("Go")
            self._publish_countdown_phase("go")
            self._start_sequence_running = False
            self._start_race_now()
            return False

        GLib.timeout_add(1000, set_yellow)
        GLib.timeout_add(2000, set_all_yellow)
        GLib.timeout_add(3000, set_green_and_start)

    def _start_race_now(self) -> None:
        self.race = Race(previous_race=self.previous_race)
        self.race.total_laps = self.total_laps
        self.race.race_end_mode = self.race_end_mode
        self.race.start(start_time=time.monotonic())

        self.current_race_id = self.db.create_race(
            notes=f"Mode: {self.race_mode}, Total Laps: {self.total_laps}"
        )

        if self.stop_btn:
            self.stop_btn.set_sensitive(True)

        self._set_start_sequence_phase(None)
        self.append_event("Race started")
        if self.race_mode == RaceMode.FAKE:
            self.start_fake_playback()
        else:
            self.publish_command("start_race")

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

        css = f".leaderboard-view {{ font-size: {point_size}pt; }}"
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
        # {pos:>3}  {name:<N} {laps:>4}  {best:>8}  {last:>8}  {total:>8}
        non_name_chars = 40
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
            leader_remaining, _ = self.race.laps_remaining()
            self.laps_remaining_label.set_text(f"Laps Remaining: {leader_remaining}")

        if self.leaderboard_view:
            leaderboard_data = self.race.leaderboard()
            name_w = self._leaderboard_name_col_width()
            header_line = f"{'Pos':>3}  {'Racer':<{name_w}} {'Laps':>4}  {'Best':>8}  {'Last':>8}  {'Total':>8}"

            rows: list[str] = []
            for pos, racer_id, lap_count, best, last, total in leaderboard_data:
                name = self.global_contestants.get_contestant_name(racer_id)
                best_s = self._format_time_cs(best)
                last_s = self._format_time_cs(last)
                total_s = self._format_time_cs(total)
                rows.append(
                    f"{pos:>3}  {name[:name_w]:<{name_w}} {lap_count:>4}  {best_s:>8}  {last_s:>8}  {total_s:>8}"
                )

            buffer = self.leaderboard_view.get_buffer()
            tag_table = buffer.get_tag_table()
            header_tag = tag_table.lookup("leaderboard-header")
            if header_tag is None:
                header_tag = buffer.create_tag("leaderboard-header")

            # Keep header aligned with monospaced row data by using same scale,
            # and style it via weight + paragraph background.
            header_tag.set_property("weight", 700)
            header_tag.set_property("scale", 1.0)
            header_tag.set_property("paragraph-background", "#ececec")

            buffer.set_text("")
            end = buffer.get_end_iter()
            buffer.insert_with_tags(end, header_line + "\n", header_tag)
            if rows:
                end = buffer.get_end_iter()
                buffer.insert(end, "\n".join(rows))

            self._update_leaderboard_font_size(len(leaderboard_data))

    def update_time(self) -> bool:
        self._update_start_light_size()

        if self.race.state == RaceState.RUNNING and self.race.start_time is not None:
            self.race.elapsed_time = time.monotonic() - self.race.start_time
        if self.time_label:
            self.time_label.set_markup(
                f'<span size="48000" weight="bold">{self._format_time_cs(self.race.elapsed_time)}</span>'
            )

        now = time.monotonic()
        if now - self._last_race_state_publish > 1.0:
            self.publish_race_state()
            self._last_race_state_publish = now
        return True

    def on_mode_changed(self, combo: Gtk.ComboBoxText) -> None:
        text = combo.get_active_text() or RaceMode.TRAINING.name
        self.race_mode = RaceMode[text]
        self.append_event(f"Mode changed to {self.race_mode}")

    def on_start_clicked(self, _button: Gtk.Button | None) -> None:
        if self._start_sequence_running or is_race_going(self.race):
            return

        # Reset visible timer immediately when user starts a new race countdown.
        self.race.elapsed_time = 0.0
        self.race.start_time = None

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

        if publish_end_command and self.race_mode != RaceMode.FAKE:
            self.publish_command("end_race")

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
                if self.race.state != RaceState.RUNNING:
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
            label="Edit names, add or delete drivers. Enter=Add, Ctrl+Enter=Save, Ctrl+D=Delete focused driver, Esc=Cancel."
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
                id_label.set_width_chars(12)
                id_label.set_xalign(0)

                name_entry = Gtk.Entry()
                name_entry.set_hexpand(True)
                name_entry.set_text(staged[transmitter_id])

                def on_name_changed(
                    entry: Gtk.Entry, tid: int = transmitter_id
                ) -> None:
                    staged[tid] = entry.get_text().strip()

                name_entry.connect("changed", on_name_changed)

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
                self.global_contestants.contestants = [
                    Contestant(transmitter_id=tid, name=cleaned[tid])
                    for tid in sorted(cleaned.keys())
                ]
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
        data = {
            "total_laps": self.total_laps,
            "race_end_mode": self.race_end_mode.value,
            "contestants": contestants,
        }
        self.config_path.write_text(json.dumps(data, indent=2))

    def connect_redis(self) -> None:
        try:
            self._redis_client = redis.Redis(
                unix_socket_path=self.redis_socket, decode_responses=True
            )
            self._redis_client.ping()
            self._redis_pubsub = self._redis_client.pubsub()
            self._redis_pubsub.subscribe(self.redis_out_channel)
            self.append_event("Connected to Redis")
            self.append_event(f"Subscribed to Redis channel: {self.redis_out_channel}")
            logging.info("Connected to Redis")
            logging.info("Subscribed to Redis channel: %s", self.redis_out_channel)
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

        if msg_type == "heartbeat":
            self.lap_counter_detected = True
            self._last_lap_counter_signal_time = time.monotonic()
            self.refresh_views()
            return

        if msg_type == "status":
            self.append_event(f"STATUS: {msg.get('message', '')}")
            return

        if msg_type != "lap":
            return

        if self.race.state != RaceState.RUNNING:
            logging.error("Cannot add lap - race is not running")
            self.append_event("Ignored lap: race is not running")
            return

        racer_id = msg.get("racer_id")
        hardware_race_time = msg.get("race_time")

        if racer_id is None or hardware_race_time is None:
            self.append_event("Invalid lap data received")
            return

        racer_id_i = int(racer_id)
        sensor_id_raw = msg.get("sensor_id", racer_id_i)
        sensor_id_i = int(sensor_id_raw)

        lap = make_lap_from_sensor_data_and_race(
            racer_id_i, float(hardware_race_time), time.monotonic(), self.race
        )
        previous_state = self.race.state
        self.race.add_lap(lap)
        finished_now = (
            previous_state != RaceState.FINISHED
            and self.race.state == RaceState.FINISHED
        )

        if self.current_race_id:
            self.db.add_lap(
                race_id=self.current_race_id,
                racer_id=lap.racer_id,
                sensor_id=sensor_id_i,
                race_time=lap.seconds_from_race_start,
                lap_number=lap.lap_number,
                lap_time=lap.lap_time if lap.lap_number > 0 else None,
            )

        if finished_now:
            self._finalize_finished_race(
                message="Race finished automatically",
                publish_end_command=True,
            )

        name = self.global_contestants.get_contestant_name(lap.racer_id)
        self.append_event(
            f"LAP: {name} (ID {lap.racer_id}) lap {lap.lap_number} at {self._format_time_cs(lap.seconds_from_race_start)}"
        )
        self.refresh_views()

    def publish_command(self, command: str, **kwargs: Any) -> None:
        if not self._redis_client:
            self.append_event("Redis client not initialized")
            return
        payload: dict[str, Any] = {"type": "command", "command": command}
        payload.update(kwargs)
        self._redis_client.publish(self.redis_in_channel, json.dumps(payload))

    def publish_race_state(self) -> None:
        if not self._redis_client:
            return
        try:
            race_data = {
                "type": "race_state",
                "timestamp": time.monotonic(),
                "race_state": self.race.state.name,
                "elapsed_time": round(self.race.elapsed_time, 2),
                "race_mode": self.race_mode.name,
                "total_laps": self.total_laps,
            }
            self._redis_client.publish("franklin:race_state", json.dumps(race_data))
        except Exception as exc:
            logging.error("Failed to publish race state: %s", exc)

    def start_fake_playback(self) -> None:
        fake_race = generate_fake_race()
        sorted_laps = order_laps_by_occurrence(fake_race.laps)
        race_start = self.race.start_time or time.monotonic()

        def playback() -> None:
            try:
                for ts, lap in sorted_laps:
                    if self._shutdown.is_set() or self.race.state != RaceState.RUNNING:
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
                            "race_time": float(lap_event.seconds_from_race_start),
                        }
                    )
            except Exception as exc:
                logging.error("Fake playback error: %s", exc)

        self._fake_thread = threading.Thread(target=playback, daemon=True)
        self._fake_thread.start()


def parse_mode() -> RaceMode:
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
    return RaceMode.TRAINING


def main() -> None:
    initial_mode = parse_mode()
    total_laps, race_end_mode, contestants_data = load_initial_config(
        Path("franklin.config.json")
    )
    app = FranklinGuiApp(
        initial_mode=initial_mode,
        total_laps=total_laps,
        contestants_data=contestants_data,
        race_end_mode=race_end_mode,
    )
    app.run([])


if __name__ == "__main__":
    main()
