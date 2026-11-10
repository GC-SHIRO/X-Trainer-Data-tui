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
from rich.cells import cell_len
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
NOT_FOUND = "未找到"


def _pad(text: str, width: int) -> str:
    """Left-align to a terminal cell width (CJK characters take two cells)."""
    return text + " " * max(1, width - cell_len(text))


class CalibrationScreen(Screen):
    BINDINGS = [Binding("escape", "back", "返回")]
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
            with TabPane("串口", id="tab-ports"):
                with VerticalScroll():
                    yield Static("插好两只主臂和两个夹爪后扫描。先探测主臂（Dynamixel，2M / 1M 波特率），"
                                 "再在剩下的串口里找夹爪。",
                                 classes="help")
                    with Horizontal(classes="buttons"):
                        yield Button("扫描串口", id="ports-scan", variant="primary")
                        yield Button("保存串口", id="ports-save", disabled=True)
                    yield Static(self._ports_text(), id="ports-result", classes="result")
            with TabPane("零位", id="tab-offsets"):
                with VerticalScroll():
                    yield Static("作用：把主臂读数和从臂关节零位对齐。标定后，主臂处于读取时的姿态，"
                                 "从臂就对应 start_joints（从臂初始位，左臂 -90,0,-90,0,90,90 度，右臂镜像）。\n"
                                 "操作：可先点「从臂回初始位」让从臂作参照，再把两只主臂摆成和从臂相同的姿态，"
                                 "松开扳机，然后读取；读取和保存零位本身不会让任何一只臂动。",
                                 classes="help")
                    with Horizontal(classes="buttons"):
                        yield Button("从臂回初始位", id="offsets-home")
                        yield Button("读取零位", id="offsets-read", variant="primary")
                        yield Button("保存零位", id="offsets-save", disabled=True)
                    yield Static("", id="offsets-result", classes="result")
            with TabPane("手柄检查", id="tab-hands"):
                with VerticalScroll():
                    yield Static("短按主臂 A 键解锁（关闭力矩，可自由转动），再按一次锁定。"
                                 "用来调整手柄；显示的是标定后的角度。",
                                 classes="help")
                    with Horizontal(classes="buttons"):
                        yield Button("开始", id="hands-start", variant="primary")
                        yield Button("停止", id="hands-stop", disabled=True)
                    yield Static("", id="hands-result", classes="result")
            with TabPane("相机", id="tab-cameras"):
                with VerticalScroll():
                    yield Static("检测已连接的 RealSense 相机（会重置相机，需要几秒），把序列号分配给 "
                                 "top / left / right。检测前先关闭其他占用相机的程序。",
                                 classes="help")
                    with Horizontal(classes="buttons"):
                        yield Button("检测", id="cams-detect", variant="primary")
                        yield Button("保存相机", id="cams-save", disabled=True)
                        yield Button("相机窗口", id="cams-view")
                    yield Static("", id="cams-result", classes="result")
                    with Vertical(id="cams-assign"):
                        for k in cal.CAMERA_KEYS:
                            with Horizontal(classes="cam-row"):
                                yield Label(k)
                                yield Select([], id=f"cam-{k}", prompt="（请先检测）")
        log = RichLog(id="cal-log", wrap=True, markup=False, max_lines=1000)
        log.border_title = "日志"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "标定" + ("" if self.allow_write else "  [模拟硬件：演示模式，不写 ini]")
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
        self.notify(f"模拟硬件：{what}未写入（演示模式）", severity="warning")
        self.cal_log("warn", f"演示模式：{what}未写入 ini")
        return False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handler = {
            "ports-scan": self._scan_ports, "ports-save": self._save_ports,
            "offsets-read": self._read_offsets, "offsets-save": self._save_offsets,
            "offsets-home": self._home_followers,
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
        t.append(_pad("设备", 16) + _pad("ini 当前值", 26) + "扫描结果\n", style="bold")
        for s in cal.HAND_SECTIONS:
            cur = f"{hands[s].port} @ {hands[s].baud_rate}"
            new = (f"{r.hands[s][0]} @ {r.hands[s][1]}" if s in r.hands else NOT_FOUND) if r else "-"
            t.append(f"{s:<16}{cur:<26}")
            t.append(new + "\n", style="red" if new == NOT_FOUND else ("green" if r else ""))
        for s in cal.GRIPPER_SECTIONS:
            new = (r.grippers.get(s, NOT_FOUND)) if r else "-"
            t.append(f"{s:<16}{grippers[s].port:<26}")
            t.append(new + "\n", style="red" if new == NOT_FOUND else ("green" if r else ""))
        if r:
            if r.leftover:
                t.append(f"未分配的串口：{', '.join(r.leftover)}\n", style="yellow")
            for e in r.errors:
                t.append(f"{e}\n", style="red")
            if r.complete:
                t.append("四个设备全部找到。", style="green")
        return t

    def _scan_ports(self) -> None:
        self.query_one("#ports-scan", Button).disabled = True
        self.cal_log("info", "正在扫描串口 ...")
        self._scan_ports_worker()

    @work(thread=True, group="ports")
    def _scan_ports_worker(self) -> None:
        try:
            res = cal.find_ports(log=self.cal_log, **self.hooks.get("find_ports", {}))
        except Exception as e:
            self.cal_log("error", f"串口扫描失败：{e}")
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
        if not r or not self._write_guard("串口"):
            return

        def done(yes: bool) -> None:
            if yes:
                cal.save_ports(r)
                self.cal_log("info", "串口已写入 ini")
                self.notify("串口已保存")
                self.query_one("#ports-result", Static).update(self._ports_text())

        if r.complete:
            done(True)
        else:
            self.app.push_screen(ConfirmScreen("没有找全所有设备。只保存找到的串口吗？"
                                               "（没找到的保持 ini 原值）"), done)

    # ------------------------------------------------------------------ offsets
    def _home_followers(self) -> None:
        def done(yes: bool) -> None:
            if yes:
                self.query_one("#offsets-home", Button).disabled = True
                self._home_worker()

        self.app.push_screen(ConfirmScreen(
            "两台从臂将运动到初始位（左臂 -90,0,-90,0,90,90 度，右臂镜像）。\n"
            "请确认从臂周围没有人和障碍物。"), done)

    @work(thread=True, group="home")
    def _home_worker(self) -> None:
        try:
            ensure = getattr(self.app, "ensure_robot_server", None)
            if ensure is None:
                raise RuntimeError("当前界面无法启动从臂服务")
            node = self.app.call_from_thread(ensure)
            if node is not None and node.state == "starting":
                self.cal_log("info", "等待从臂服务就绪（最长 60 秒）...")
                if not node.wait_ready(60):
                    raise RuntimeError("从臂服务启动失败：" + (node.tail[-1] if node.tail else "无输出"))
            net = self.app.settings.network
            cal.move_followers_home(net.zmq_host, net.zmq_port, log=self.cal_log)
            self._ui(self.notify, "从臂已到初始位，现在把主臂摆成相同姿态后读取零位")
        except Exception as e:
            from dobot_control.collector import describe_error
            self.cal_log("error", f"从臂回初始位失败：{describe_error(e)}")
        finally:
            self._ui(self._home_done)

    def _home_done(self) -> None:
        try:
            self.query_one("#offsets-home", Button).disabled = False
        except Exception:
            pass

    def _read_offsets(self) -> None:
        self.query_one("#offsets-read", Button).disabled = True
        self.cal_log("info", "正在读取主臂关节 ...")
        self._read_offsets_worker()

    @work(thread=True, group="offsets")
    def _read_offsets_worker(self) -> None:
        try:
            res = cal.read_offsets(log=self.cal_log, **self.hooks.get("read_offsets", {}))
        except Exception as e:
            self.cal_log("error", f"读取零位失败：{e}（请先检查串口）")
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
            t.append(f"  原始关节角（度） {np.round(np.rad2deg(r.raw_joints), 1).tolist()}\n")
            t.append(f"  零位  当前 {r.old_offsets}\n")
            t.append(f"        新值 {r.offsets}\n", style="green")
            change = r.max_change
            t.append(f"        最大变化 {change:.2f} rad\n",
                     style="bold yellow" if change > OFFSET_WARN_RAD else "grey70")
            t.append(f"  夹爪  当前 {r.old_gripper_config}  新值 {r.gripper_config}\n")
        if any(r.max_change > OFFSET_WARN_RAD for r in res.values()):
            t.append(f"\n有零位变化超过 {OFFSET_WARN_RAD} rad：请确认主臂确实在初始姿态。", style="yellow")
        self.query_one("#offsets-result", Static).update(t)
        self.query_one("#offsets-save", Button).disabled = False

    def _save_offsets(self) -> None:
        r = self.offset_result
        if not r or not self._write_guard("零位"):
            return

        def done(yes: bool) -> None:
            if yes:
                cal.save_offsets(r)
                self.cal_log("info", "零位和夹爪参数已写入 ini")
                self.notify("零位已保存")

        if any(x.max_change > OFFSET_WARN_RAD for x in r.values()):
            self.app.push_screen(ConfirmScreen(f"有零位变化超过 {OFFSET_WARN_RAD} rad，仍然保存吗？"), done)
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
            self.cal_log("error", f"无法打开主臂：{e}")
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
            self.cal_log("info", "手柄检查已停止，两只主臂已锁定")
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
        for i, name in enumerate(("左主臂", "右主臂")):
            t.append(f"{name}  ")
            t.append(" 已解锁 " if s.unlocked[i] else " 已锁定 ",
                     style="bold black on yellow" if s.unlocked[i] else "grey50")
            if s.keys is not None:
                a, b = s.keys[i][0], s.keys[i][1]
                t.append(f"   A {'按下' if a == 0 else '松开'}  B {'按下' if b == 0 else '松开'}")
            t.append("\n")
            if s.joints is not None:
                j = s.joints[7 * i:7 * i + 7]
                t.append("  关节角（度） " + "  ".join(f"{v:7.1f}" for v in np.rad2deg(j[:6])))
                t.append(f"   扳机 {j[6]:.2f}\n")
        self.query_one("#hands-result", Static).update(t)

    # ------------------------------------------------------------------ cameras
    def _detect_cams(self) -> None:
        self._close_view()  # detection resets the devices
        self.query_one("#cams-detect", Button).disabled = True
        self.cal_log("info", "正在检测 RealSense 相机 ...")
        self._detect_worker()

    @work(thread=True, group="cams")
    def _detect_worker(self) -> None:
        try:
            res = cal.check_cameras(**self.hooks.get("check_cameras", {}))
        except Exception as e:
            self.cal_log("error", f"相机检测失败：{e}")
            res = None
        self._ui(self._cams_done, res)

    def _cams_done(self, res) -> None:
        self.query_one("#cams-detect", Button).disabled = False
        if res is None:
            return
        self.camera_result = res
        t = Text(f"找到 {len(res.found)} 个：{', '.join(res.found) or '无'}\n")
        for k, serial in res.assigned.items():
            ok = serial in res.found
            t.append(f"  {k:<6}{serial:<16}")
            t.append("已连接\n" if ok else "未连接\n", style="green" if ok else "red")
        if res.unassigned:
            t.append(f"未分配：{', '.join(res.unassigned)}\n", style="yellow")
        self.query_one("#cams-result", Static).update(t)
        options = sorted(set(res.found) | set(res.assigned.values()))
        for k in cal.CAMERA_KEYS:
            sel = self.query_one(f"#cam-{k}", Select)
            sel.set_options([(o + ("" if o in res.found else "（未连接）"), o) for o in options])
            sel.value = res.assigned[k]
        self.query_one("#cams-save", Button).disabled = False
        self.cal_log("info", f"找到 {len(res.found)} 个相机")

    def _save_cams(self) -> None:
        values = {k: self.query_one(f"#cam-{k}", Select).value for k in cal.CAMERA_KEYS}
        if any(not isinstance(v, str) for v in values.values()):
            self.notify("请为三个位置都分配相机", severity="error")
            return
        if len(set(values.values())) != 3:
            self.notify("三个相机必须是不同的序列号", severity="error")
            return
        if values == load_ini_data_camera():
            self.notify("没有改动")
            return
        if not self._write_guard("相机"):
            return
        cal.save_cameras(values)
        self.cal_log("info", f"相机已写入 ini：{values}")
        self.notify("相机已保存")

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
        self.cal_log("info", f"正在打开相机：{assigned}")
        self._open_view_worker(assigned)

    @work(thread=True, group="cams-view")
    def _open_view_worker(self, assigned: Dict[str, str]) -> None:
        try:
            cams = cal.open_cameras(assigned, **self.hooks.get("open_cameras", {}))
        except Exception as e:
            self.cal_log("error", f"无法打开相机：{e}")
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
        b.disabled, b.label = False, "关闭窗口"
        self.cal_log("info", "相机窗口已打开（top | left | right）")

    def _view_closed_ui(self) -> None:
        b = self.query_one("#cams-view", Button)
        b.disabled, b.label = False, "相机窗口"

    def _view_closed_by_window(self, error) -> None:
        # preview thread: X button or read error
        cams, self.preview_cams = self.preview_cams, None
        if cams:
            cal.close_cameras(cams)
        self.cal_log("warn" if error else "info",
                     f"相机窗口出错：{error}" if error else "相机窗口已关闭")
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
        self.cal_log("info", "相机窗口已关闭")
        try:
            self._view_closed_ui()
        except Exception:
            pass
