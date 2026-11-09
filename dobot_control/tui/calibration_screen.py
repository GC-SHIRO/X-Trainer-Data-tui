"""Calibration tools (P3): the four setup scripts as tabs.

    Ports      scripts/1_find_port.py     find master-hand / gripper serial ports
    Offsets    scripts/2_get_offset.py    master-hand joint offsets + trigger range
    Hand check scripts/3_just_buttonA.py  A short press locks/unlocks, live joint angles
    Cameras    scripts/5_camera_read.py   RealSense serials -> top / left / right

Results are shown first and only written to the ini with the Save buttons. With the fake
backend nothing is written (dry run) unless allow_write is set.
"""
import datetime
import time
from typing import Dict, Optional

import numpy as np
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Label, RichLog, Select, Static, TabbedContent, TabPane

from dobot_control import calibration as cal
from dobot_control.config import load_ini_data_camera, load_ini_data_gripper, load_ini_data_hands
from dobot_control.preview import get_preview
from dobot_control.tui.widgets import ConfirmScreen

LEVEL_STYLE = {"info": "white", "warn": "yellow", "error": "bold red"}
OFFSET_WARN_RAD = 0.5   # a bigger change usually means the hand was not in the start pose


class CalibrationScreen(Screen):
    BINDINGS = [Binding("escape", "back", "Back")]
    DEFAULT_CSS = """
    CalibrationScreen TabbedContent { height: 1fr; }
    CalibrationScreen ContentSwitcher { height: 1fr; }
    CalibrationScreen TabPane { height: 1fr; padding: 0 1; }
    CalibrationScreen .help { color: $text-muted; height: auto; margin-bottom: 1; }
    CalibrationScreen .buttons { height: auto; margin-bottom: 1; }
    CalibrationScreen .buttons Button { margin-right: 1; }
    CalibrationScreen .result { height: auto; }
    CalibrationScreen .cam-row { height: auto; }
    CalibrationScreen .cam-row Label { width: 8; padding: 1 0 0 0; }
    CalibrationScreen .cam-row Select { width: 30; }
    CalibrationScreen #cal-log { height: 10; border: round $accent; }
    """

    def __init__(self, hooks: Optional[Dict[str, dict]] = None, allow_write: bool = True):
        super().__init__()
        self.hooks = hooks or {}
        self.allow_write = allow_write
        self.port_result: Optional[cal.PortScanResult] = None
        self.offset_result: Optional[Dict[str, cal.HandOffset]] = None
        self.camera_result: Optional[cal.CameraCheck] = None
        self.hand_check: Optional[cal.HandCheck] = None
        self.preview_cams = None          # cameras opened for the preview window

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="cal-tabs"):
            with TabPane("Ports", id="tab-ports"):
                with VerticalScroll():
                    yield Static("Plug in both master hands and both grippers, then scan. Hands are probed "
                                 "first (Dynamixel, 2M / 1M baud), grippers on the remaining ports.",
                                 classes="help")
                    with Horizontal(classes="buttons"):
                        yield Button("Scan ports", id="ports-scan", variant="primary")
                        yield Button("Save ports", id="ports-save", disabled=True)
                    yield Static(self._ports_text(), id="ports-result", classes="result")
            with TabPane("Offsets", id="tab-offsets"):
                with VerticalScroll():
                    yield Static("Hold BOTH master hands in the start pose (start_joints in the ini, the same "
                                 "pose as the arms' home) with the trigger released, then read.",
                                 classes="help")
                    with Horizontal(classes="buttons"):
                        yield Button("Read offsets", id="offsets-read", variant="primary")
                        yield Button("Save offsets", id="offsets-save", disabled=True)
                    yield Static("", id="offsets-result", classes="result")
            with TabPane("Hand check", id="tab-hands"):
                with VerticalScroll():
                    yield Static("Short-press A on a hand to unlock it (torque off, free to move) and again to "
                                 "lock it. Use this to adjust the handles; angles are after calibration.",
                                 classes="help")
                    with Horizontal(classes="buttons"):
                        yield Button("Start", id="hands-start", variant="primary")
                        yield Button("Stop", id="hands-stop", disabled=True)
                    yield Static("", id="hands-result", classes="result")
            with TabPane("Cameras", id="tab-cameras"):
                with VerticalScroll():
                    yield Static("Detect connected RealSense cameras (they are reset, takes a few seconds) and "
                                 "assign serials to top / left / right. Close any program using them first.",
                                 classes="help")
                    with Horizontal(classes="buttons"):
                        yield Button("Detect", id="cams-detect", variant="primary")
                        yield Button("Save cameras", id="cams-save", disabled=True)
                        yield Button("Camera view", id="cams-view")
                    yield Static("", id="cams-result", classes="result")
                    with Vertical(id="cams-assign"):
                        for k in cal.CAMERA_KEYS:
                            with Horizontal(classes="cam-row"):
                                yield Label(k)
                                yield Select([], id=f"cam-{k}", prompt="(detect first)")
        log = RichLog(id="cal-log", wrap=True, markup=False, max_lines=1000)
        log.border_title = "Log"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "calibration" + ("" if self.allow_write else "  [FAKE - dry run, ini not written]")
        self.set_interval(0.1, self._refresh_hands)

    # ------------------------------------------------------------------ helpers
    def cal_log(self, level: str, msg: str) -> None:
        """Thread-safe log line (calibration functions call this from workers)."""
        def write():
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            line = Text(f"{ts} ", style="grey50")
            line.append(msg, style=LEVEL_STYLE.get(level, "white"))
            self.query_one("#cal-log", RichLog).write(line)
        try:
            self.app.call_from_thread(write)
        except RuntimeError:  # already on the UI thread
            write()

    def _ui(self, fn, *args) -> None:
        self.app.call_from_thread(fn, *args)

    def _write_guard(self, what: str) -> bool:
        if self.allow_write:
            return True
        self.notify(f"Fake backend: {what} not written (dry run)", severity="warning")
        self.cal_log("warn", f"Dry run: {what} not written to the ini")
        return False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handler = {
            "ports-scan": self._scan_ports, "ports-save": self._save_ports,
            "offsets-read": self._read_offsets, "offsets-save": self._save_offsets,
            "hands-start": self._start_hands, "hands-stop": self._stop_hands,
            "cams-detect": self._detect_cams, "cams-save": self._save_cams,
            "cams-view": self._toggle_view,
        }.get(event.button.id)
        if handler:
            handler()

    def action_back(self) -> None:
        self._stop_hands()
        self._close_view()
        self.app.pop_screen()

    def on_unmount(self) -> None:
        self._stop_hands()
        self._close_view()

    # ------------------------------------------------------------------ ports
    def _ports_text(self) -> Text:
        _, hands = load_ini_data_hands()
        _, grippers = load_ini_data_gripper()
        r = self.port_result
        t = Text()
        t.append(f"{'device':<16}{'current ini':<26}{'scan result'}\n", style="bold")
        for s in cal.HAND_SECTIONS:
            cur = f"{hands[s].port} @ {hands[s].baud_rate}"
            new = (f"{r.hands[s][0]} @ {r.hands[s][1]}" if s in r.hands else "NOT FOUND") if r else "-"
            t.append(f"{s:<16}{cur:<26}")
            t.append(new + "\n", style="red" if new == "NOT FOUND" else ("green" if r else ""))
        for s in cal.GRIPPER_SECTIONS:
            new = (r.grippers.get(s, "NOT FOUND")) if r else "-"
            t.append(f"{s:<16}{grippers[s].port:<26}")
            t.append(new + "\n", style="red" if new == "NOT FOUND" else ("green" if r else ""))
        if r:
            if r.leftover:
                t.append(f"unassigned ports: {', '.join(r.leftover)}\n", style="yellow")
            for e in r.errors:
                t.append(f"{e}\n", style="red")
            if r.complete:
                t.append("All four devices found.", style="green")
        return t

    def _scan_ports(self) -> None:
        self.query_one("#ports-scan", Button).disabled = True
        self.cal_log("info", "Scanning serial ports ...")
        self._scan_ports_worker()

    @work(thread=True, group="ports")
    def _scan_ports_worker(self) -> None:
        try:
            res = cal.find_ports(log=self.cal_log, **self.hooks.get("find_ports", {}))
        except Exception as e:
            self.cal_log("error", f"Port scan failed: {e}")
            res = None
        self._ui(self._ports_done, res)

    def _ports_done(self, res) -> None:
        self.query_one("#ports-scan", Button).disabled = False
        if res is None:
            return
        self.port_result = res
        self.query_one("#ports-result", Static).update(self._ports_text())
        self.query_one("#ports-save", Button).disabled = not (res.hands or res.grippers)

    def _save_ports(self) -> None:
        r = self.port_result
        if not r or not self._write_guard("ports"):
            return

        def done(yes: bool) -> None:
            if yes:
                cal.save_ports(r)
                self.cal_log("info", "Ports saved to the ini")
                self.notify("Ports saved")
                self.query_one("#ports-result", Static).update(self._ports_text())

        if r.complete:
            done(True)
        else:
            self.app.push_screen(ConfirmScreen("Not every device was found. Save only the ports that were "
                                               "found (the others keep their current entry)?"), done)

    # ------------------------------------------------------------------ offsets
    def _read_offsets(self) -> None:
        self.query_one("#offsets-read", Button).disabled = True
        self.cal_log("info", "Reading master-hand joints ...")
        self._read_offsets_worker()

    @work(thread=True, group="offsets")
    def _read_offsets_worker(self) -> None:
        try:
            res = cal.read_offsets(log=self.cal_log, **self.hooks.get("read_offsets", {}))
        except Exception as e:
            self.cal_log("error", f"Reading offsets failed: {e} (check the ports first)")
            res = None
        self._ui(self._offsets_done, res)

    def _offsets_done(self, res) -> None:
        self.query_one("#offsets-read", Button).disabled = False
        if res is None:
            return
        self.offset_result = res
        t = Text()
        for s, r in res.items():
            t.append(f"{s}\n", style="bold")
            t.append(f"  raw joints (deg) {np.round(np.rad2deg(r.raw_joints), 1).tolist()}\n")
            t.append(f"  offsets  now     {r.old_offsets}\n")
            t.append(f"           new     {r.offsets}\n", style="green")
            change = r.max_change
            t.append(f"           max change {change:.2f} rad\n",
                     style="bold yellow" if change > OFFSET_WARN_RAD else "grey70")
            t.append(f"  gripper  now {r.old_gripper_config}  new {r.gripper_config}\n")
        if any(r.max_change > OFFSET_WARN_RAD for r in res.values()):
            t.append(f"\nAn offset changes by more than {OFFSET_WARN_RAD} rad: check that the hand is really "
                     "in the start pose.", style="yellow")
        self.query_one("#offsets-result", Static).update(t)
        self.query_one("#offsets-save", Button).disabled = False

    def _save_offsets(self) -> None:
        r = self.offset_result
        if not r or not self._write_guard("offsets"):
            return

        def done(yes: bool) -> None:
            if yes:
                cal.save_offsets(r)
                self.cal_log("info", "Offsets and gripper config saved to the ini")
                self.notify("Offsets saved")

        if any(x.max_change > OFFSET_WARN_RAD for x in r.values()):
            self.app.push_screen(ConfirmScreen(f"An offset changes by more than {OFFSET_WARN_RAD} rad. "
                                               "Save anyway?"), done)
        else:
            done(True)

    # ------------------------------------------------------------------ hand check
    def _start_hands(self) -> None:
        if self.hand_check:
            return
        self.query_one("#hands-start", Button).disabled = True
        self._start_hands_worker()

    @work(thread=True, group="hands")
    def _start_hands_worker(self) -> None:
        hc = cal.HandCheck(log=self.cal_log, **self.hooks.get("HandCheck", {}))
        try:
            hc.start()
        except Exception as e:
            self.cal_log("error", f"Could not open the master hands: {e}")
            self._ui(self._hands_stopped)
            return
        self.hand_check = hc
        self._ui(self._hands_started)

    def _hands_started(self) -> None:
        self.query_one("#hands-stop", Button).disabled = False

    def _hands_stopped(self) -> None:
        self.query_one("#hands-start", Button).disabled = False
        self.query_one("#hands-stop", Button).disabled = True

    def _stop_hands(self) -> None:
        hc, self.hand_check = self.hand_check, None
        if hc:
            hc.stop()
            self.cal_log("info", "Hand check stopped, both hands locked")
            try:
                self._hands_stopped()
            except Exception:
                pass

    def _refresh_hands(self) -> None:
        hc = self.hand_check
        if not hc:
            return
        s = hc.snapshot()
        if s.error:
            self._stop_hands()
            return
        t = Text()
        for i, name in enumerate(("LEFT", "RIGHT")):
            t.append(f"{name:<6}")
            t.append(" UNLOCKED " if s.unlocked[i] else " locked ",
                     style="bold black on yellow" if s.unlocked[i] else "grey50")
            if s.keys is not None:
                a, b = s.keys[i][0], s.keys[i][1]
                t.append(f"   A {'down' if a == 0 else 'up  '}  B {'down' if b == 0 else 'up  '}")
            t.append("\n")
            if s.joints is not None:
                j = s.joints[7 * i:7 * i + 7]
                t.append("  joints (deg) " + "  ".join(f"{v:7.1f}" for v in np.rad2deg(j[:6])))
                t.append(f"   trigger {j[6]:.2f}\n")
        self.query_one("#hands-result", Static).update(t)

    # ------------------------------------------------------------------ cameras
    def _detect_cams(self) -> None:
        self._close_view()  # detection resets the devices
        self.query_one("#cams-detect", Button).disabled = True
        self.cal_log("info", "Detecting RealSense cameras ...")
        self._detect_worker()

    @work(thread=True, group="cams")
    def _detect_worker(self) -> None:
        try:
            res = cal.check_cameras(**self.hooks.get("check_cameras", {}))
        except Exception as e:
            self.cal_log("error", f"Camera detection failed: {e}")
            res = None
        self._ui(self._cams_done, res)

    def _cams_done(self, res) -> None:
        self.query_one("#cams-detect", Button).disabled = False
        if res is None:
            return
        self.camera_result = res
        t = Text(f"found {len(res.found)}: {', '.join(res.found) or 'none'}\n")
        for k, serial in res.assigned.items():
            ok = serial in res.found
            t.append(f"  {k:<6}{serial:<16}")
            t.append("connected\n" if ok else "NOT CONNECTED\n", style="green" if ok else "red")
        if res.unassigned:
            t.append(f"not assigned: {', '.join(res.unassigned)}\n", style="yellow")
        self.query_one("#cams-result", Static).update(t)
        options = sorted(set(res.found) | set(res.assigned.values()))
        for k in cal.CAMERA_KEYS:
            sel = self.query_one(f"#cam-{k}", Select)
            sel.set_options([(o + ("" if o in res.found else "  (not connected)"), o) for o in options])
            sel.value = res.assigned[k]
        self.query_one("#cams-save", Button).disabled = False
        self.cal_log("info", f"{len(res.found)} camera(s) found")

    def _save_cams(self) -> None:
        values = {k: self.query_one(f"#cam-{k}", Select).value for k in cal.CAMERA_KEYS}
        if any(not isinstance(v, str) for v in values.values()):
            self.notify("Assign all three cameras", severity="error")
            return
        if len(set(values.values())) != 3:
            self.notify("Each camera needs a different serial", severity="error")
            return
        if values == load_ini_data_camera():
            self.notify("No change")
            return
        if not self._write_guard("cameras"):
            return
        cal.save_cameras(values)
        self.cal_log("info", f"Cameras saved: {values}")
        self.notify("Cameras saved")

    # ------------------------------------------------------------------ camera window
    def _view_assignment(self) -> Dict[str, str]:
        """Serials chosen in the selects (after Detect), otherwise the ini."""
        assigned = load_ini_data_camera()
        for k in cal.CAMERA_KEYS:
            v = self.query_one(f"#cam-{k}", Select).value
            if isinstance(v, str):
                assigned[k] = v
        return assigned

    def _toggle_view(self) -> None:
        if self.preview_cams is not None:
            self._close_view()
            return
        button = self.query_one("#cams-view", Button)
        button.disabled = True
        assigned = self._view_assignment()
        self.cal_log("info", f"Opening cameras for the window: {assigned}")
        self._open_view_worker(assigned)

    @work(thread=True, group="cams-view")
    def _open_view_worker(self, assigned: Dict[str, str]) -> None:
        try:
            cams = cal.open_cameras(assigned, **self.hooks.get("open_cameras", {}))
        except Exception as e:
            self.cal_log("error", f"Could not open the cameras: {e}")
            self._ui(self._view_closed_ui)
            return
        try:
            get_preview().show(lambda: cal.camera_frames(cams), on_close=self._view_closed_by_window)
        except RuntimeError as e:
            cal.close_cameras(cams)
            self.cal_log("warn", str(e))
            self._ui(self._view_closed_ui)
            return
        self.preview_cams = cams
        self._ui(self._view_opened_ui)

    def _view_opened_ui(self) -> None:
        b = self.query_one("#cams-view", Button)
        b.disabled, b.label = False, "Close view"
        self.cal_log("info", "Camera window open (top | left | right)")

    def _view_closed_ui(self) -> None:
        b = self.query_one("#cams-view", Button)
        b.disabled, b.label = False, "Camera view"

    def _view_closed_by_window(self, error) -> None:
        # preview thread: X button or read error
        cams, self.preview_cams = self.preview_cams, None
        if cams:
            cal.close_cameras(cams)
        self.cal_log("warn" if error else "info",
                     f"Camera window failed: {error}" if error else "Camera window closed")
        try:
            self._ui(self._view_closed_ui)
        except Exception:
            pass

    def _close_view(self) -> None:
        cams, self.preview_cams = self.preview_cams, None
        if cams is None:
            return
        get_preview().hide()
        cal.close_cameras(cams)
        self.cal_log("info", "Camera window closed")
        try:
            self._view_closed_ui()
        except Exception:
            pass
