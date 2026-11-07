"""Collection dashboard (P1): status of both hands, recording, rates and the event log.

The TUI is a thin layer over CollectorSession: it polls snapshot() at 10 Hz and drains
session events through a thread-safe queue, so nothing in the control loop waits on the UI.
The master-hand buttons keep working exactly as in the CLI; the keyboard only adds
connect / record / stop shortcuts.
"""
import datetime
import os
import queue
import time
from typing import Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, Static

from dobot_control.collector import CollectorSession, Event
from dobot_control.settings import Settings, save_settings
from dobot_control.tui.calibration_screen import CalibrationScreen
from dobot_control.tui.episodes_screen import EpisodesScreen
from dobot_control.tui.settings_screen import SettingsScreen

PHASE_STYLE = {
    "idle": ("IDLE", "bold white on grey30"),
    "connecting": ("CONNECTING", "bold black on yellow"),
    "ready": ("READY", "bold black on cyan"),
    "running": ("RUNNING", "bold black on green"),
    "stopped": ("STOPPED", "bold white on grey30"),
    "error": ("ERROR", "bold white on red"),
}
LIGHT_STYLE = {"dark": "grey50", "red": "bold red", "yellow": "bold yellow", "green": "bold green"}
LEVEL_STYLE = {"info": "white", "warn": "yellow", "error": "bold red"}


def _flag(on: bool, yes: str, no: str, style: str) -> Text:
    return Text(f" {yes} ", style=style) if on else Text(f" {no} ", style="grey50")


class Panel(Static):
    """A bordered Static whose title is set from CSS / constructor."""

    def __init__(self, title: str, **kw):
        super().__init__(**kw)
        self.border_title = title


class CollectorApp(App):
    TITLE = "Dobot X-Trainer"
    SUB_TITLE = "data collection"

    CSS = """
    Screen { layout: vertical; }
    #top { height: auto; }
    #top Horizontal { height: auto; }
    Panel { border: round $accent; padding: 0 1; height: auto; }
    #status { width: 1fr; }
    .hand { width: 1fr; }
    #recording { width: 1fr; }
    #rates { width: 1fr; }
    #log { border: round $accent; height: 1fr; }
    """

    BINDINGS = [
        Binding("c", "connect", "Connect/start"),
        Binding("r", "toggle_record", "Record on/off"),
        Binding("s", "stop_session", "Stop session"),
        Binding("e", "settings", "Settings"),
        Binding("l", "episodes", "Episodes"),
        Binding("k", "calibrate", "Calibrate"),
        Binding("o", "open_dir", "Show save dir", show=False),
        Binding("q", "quit_app", "Quit"),
    ]
    FAKE_BINDINGS = [
        ("1", "fake_press(0, 0, 0.1)", "LA"),
        ("2", "fake_press(0, 0, 1.3)", "LA hold"),
        ("3", "fake_press(1, 0, 0.1)", "RA"),
        ("4", "fake_press(1, 0, 1.3)", "RA hold"),
        ("5", "fake_press(0, 1, 0.1)", "B"),
    ]

    def __init__(self, settings: Settings, backend: str = "real", auto_connect: bool = False):
        super().__init__()
        self.settings = settings
        self.backend = backend
        self.auto_connect = auto_connect
        # the fake backend never writes calibration results unless a test turns this on
        self.calibration_writes = backend != "fake"
        self.session: Optional[CollectorSession] = None
        self._events: "queue.Queue[Event]" = queue.Queue()
        self._quit_armed = 0.0
        if backend == "fake":
            for key, action, desc in self.FAKE_BINDINGS:
                self.bind(key, action, description=desc)

    MAIN_ACTIONS = {"connect", "toggle_record", "stop_session", "settings", "episodes", "calibrate",
                    "open_dir", "quit_app", "fake_press"}

    def _on_main_screen(self) -> bool:
        return len(self.screen_stack) <= 1

    def check_action(self, action: str, parameters) -> Optional[bool]:
        # App bindings would otherwise also fire on the settings / episodes / calibration
        # screens (e.g. `c` connecting while calibration holds the hands).
        if action in self.MAIN_ACTIONS and not self._on_main_screen():
            return False
        return True

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="top"):
            yield Panel("Session", id="status")
            with Horizontal(id="hands"):
                yield Panel("Left hand", id="hand-left", classes="hand")
                yield Panel("Right hand", id="hand-right", classes="hand")
            with Horizontal():
                yield Panel("Recording", id="recording")
                yield Panel("Rates", id="rates")
        log = RichLog(id="log", wrap=True, markup=False, max_lines=2000)
        log.border_title = "Events"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "data collection" + ("  [FAKE HARDWARE]" if self.backend == "fake" else "")
        self._write_log(Event(time.time(), "info",
                              "Press c to connect." + ("" if self.backend == "fake" else
                                                       " Make sure experiments/launch_nodes.py is running.")))
        self.refresh_view()
        self.set_interval(0.1, self.refresh_view)
        if self.auto_connect:
            self.action_connect()

    # ------------------------------------------------------------------ rendering
    def _write_log(self, ev: Event) -> None:
        ts = datetime.datetime.fromtimestamp(ev.time).strftime("%H:%M:%S")
        line = Text(f"{ts} ", style="grey50")
        line.append(f"{ev.level.upper():5} ", style=LEVEL_STYLE.get(ev.level, "white"))
        line.append(ev.message, style=LEVEL_STYLE.get(ev.level, "white"))
        self.query_one("#log", RichLog).write(line)

    def refresh_view(self) -> None:
        while True:
            try:
                self._write_log(self._events.get_nowait())
            except queue.Empty:
                break

        s = self.session.snapshot() if self.session else None
        phase = s.phase if s else "idle"
        label, style = PHASE_STYLE.get(phase, (phase.upper(), "bold"))
        active = phase in ("connecting", "ready", "running")
        # an active session shows what it runs with; otherwise show what the next connect will use
        c = self.session.settings.collect if s and active else self.settings.collect
        save_dir = s.save_dir if s and active else os.path.join(c.resolved_save_root(), c.project_name,
                                                                 "collect_data")

        status = Text()
        status.append(f" {label} ", style=style)
        status.append(f"  backend: {self.backend}   robot: {(s and s.robot_type) or '-'}   "
                      f"project: {c.project_name}\n")
        status.append(f"save dir: {save_dir}", style="grey70")
        if s and s.error:
            status.append(f"\nerror: {s.error}", style="bold red")
        self.query_one("#status", Panel).update(status)

        for i, side in enumerate(("left", "right")):
            t = Text()
            unlocked = bool(s and s.unlocked[i])
            servo = bool(s and s.servo[i])
            t.append("lock  ")
            t.append_text(_flag(unlocked, "UNLOCKED", "locked", "bold black on yellow"))
            t.append("\nservo ")
            t.append_text(_flag(servo, "SERVO", "off", "bold black on green"))
            self.query_one(f"#hand-{side}", Panel).update(t)

        t = Text()
        recording = bool(s and s.recording)
        t.append_text(_flag(recording, "● REC", "not recording", "bold white on red"))
        light = s.light if s else "dark"
        t.append("   light: ")
        t.append(f"■ {light}", style=LIGHT_STYLE.get(light, "white"))
        t.append(f"\nepisode: {(s and s.episode) or '-'}   frames: {s.frames if s else 0}"
                 f"\nepisodes this session: {s.episodes_recorded if s else 0}")
        self.query_one("#recording", Panel).update(t)

        t = Text()
        hz = s.loop_hz if s else 0.0
        t.append(f"control loop: {hz:6.1f} Hz\n")
        fps = s.camera_fps if s else (0.0, 0.0, 0.0)
        t.append("cameras:     ")
        for name, f in zip(("top", "left", "right"), fps):
            t.append(f"{name} ")
            t.append(f"{f:.0f}", style="green" if f > 20 else ("yellow" if f > 0 else "grey50"))
            t.append("  ")
        t.append("fps")
        self.query_one("#rates", Panel).update(t)

    # ------------------------------------------------------------------ actions
    def action_connect(self) -> None:
        if self.session and self.session.state.phase in ("connecting", "ready", "running"):
            self.notify("Session already active", severity="warning")
            return
        self.session = CollectorSession(self.settings, backend=self.backend)
        self.session.add_listener(self._events.put)
        self._connect_worker(self.session)

    @work(thread=True, exclusive=True, group="connect")
    def _connect_worker(self, session: CollectorSession) -> None:
        try:
            session.connect()
        except Exception:
            session.stop()
            return  # the error is already in the event log / state
        session.start()

    def action_toggle_record(self) -> None:
        s = self.session
        if not s or s.state.phase != "running":
            self.notify("Not running", severity="warning")
            return
        if s.state.recording:
            s.stop_recording()
        elif not s.start_recording():
            self.notify("Recording needs at least one hand in servo (long-press A)", severity="warning")

    def action_stop_session(self) -> None:
        if self.session and self.session.state.phase in ("connecting", "ready", "running"):
            self._stop_worker(self.session)

    @work(thread=True, group="stop")
    def _stop_worker(self, session: CollectorSession) -> None:
        session.stop()

    def action_settings(self) -> None:
        active = bool(self.session and self.session.state.phase in ("connecting", "ready", "running"))
        default_root = type(self.settings.collect)().resolved_save_root()
        self.push_screen(SettingsScreen(self.settings, active, default_root), self._settings_done)

    def _settings_done(self, new: Optional[Settings]) -> None:
        if new is None:
            return
        try:
            save_settings(new)
        except Exception as e:
            self.notify(f"Could not write the ini: {e}", severity="error")
            return
        self.settings = new  # a running session keeps its own settings object
        active = self.session and self.session.state.phase in ("connecting", "ready", "running")
        self.notify("Settings saved" + (" - they apply on the next connect" if active else ""))
        self._events.put(Event(time.time(), "info", "Settings saved to the ini"))

    def _session_active(self) -> bool:
        return bool(self.session and self.session.state.phase in ("connecting", "ready", "running"))

    def action_episodes(self) -> None:
        c = self.session.settings.collect if self._session_active() else self.settings.collect

        def busy():
            s = self.session
            if s and s.state.recording and s.state.episode:
                return os.path.join(s.state.save_dir, s.state.episode)
            return None

        self.push_screen(EpisodesScreen(c.resolved_save_root(), c.project_name, busy))

    def action_calibrate(self) -> None:
        if self._session_active():
            self.notify("Stop the session first (s): calibration needs the hands and cameras",
                        severity="warning")
            return
        hooks = None
        if self.backend == "fake":
            from dobot_control.fake import fake_calibration_hooks
            hooks = fake_calibration_hooks()
        self.push_screen(CalibrationScreen(hooks, allow_write=self.calibration_writes))

    def action_open_dir(self) -> None:
        s = self.session.snapshot() if self.session else None
        if s:
            self.notify(s.save_dir)

    def action_quit_app(self) -> None:
        s = self.session
        if s and s.state.recording and time.time() - self._quit_armed > 3:
            self._quit_armed = time.time()
            self.notify("Recording in progress - press q again to stop and quit", severity="warning")
            return
        if s:
            s.stop()
        self.exit()

    def action_fake_press(self, hand: int, key: int, duration: float) -> None:
        if self.session and self.session.fake_hands:
            self.session.fake_hands[hand].press(key, duration)
