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
from dobot_control.nodes import NodeProcess, is_local, port_in_use
from dobot_control.preview import get_preview
from dobot_control.settings import Settings, save_settings
from dobot_control.tui.calibration_screen import CalibrationScreen
from dobot_control.tui.episodes_screen import EpisodesScreen
from dobot_control.tui.settings_screen import SettingsScreen

PHASE_STYLE = {
    "idle": ("未连接", "bold white on grey30"),
    "connecting": ("连接中", "bold black on yellow"),
    "ready": ("就绪", "bold black on cyan"),
    "running": ("运行中", "bold black on green"),
    "stopped": ("已停止", "bold white on grey30"),
    "error": ("出错", "bold white on red"),
}
NODE_READY_TIMEOUT_S = 60   # connecting both arms + enabling them takes a while
LIGHT_ZH = {"dark": "灭", "red": "红", "yellow": "黄", "green": "绿"}
LEVEL_ZH = {"info": "信息", "warn": "警告", "error": "错误"}
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
    SUB_TITLE = "数据采集"

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
        Binding("c", "connect", "连接"),
        Binding("r", "restart_all", "重启服务"),
        Binding("s", "stop_session", "停止"),
        Binding("n", "toggle_node", "从臂服务"),
        Binding("v", "toggle_preview", "相机窗口"),
        Binding("e", "settings", "设置"),
        Binding("l", "episodes", "数据管理"),
        Binding("k", "calibrate", "标定"),
        Binding("o", "open_dir", "保存目录", show=False),
        Binding("q", "quit_app", "退出"),
    ]
    FAKE_BINDINGS = [
        ("1", "fake_press(0, 0, 0.1)", "左A短"),
        ("2", "fake_press(0, 0, 1.3)", "左A长"),
        ("3", "fake_press(1, 0, 0.1)", "右A短"),
        ("4", "fake_press(1, 0, 1.3)", "右A长"),
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
        self._restart_armed = 0.0
        self._restarting = False
        self._preview_owner = None      # session whose frames the camera window shows
        self.node: Optional[NodeProcess] = None   # launch_nodes.py started by this TUI
        self._external_node = (0.0, False)        # (checked at, a server already listens)
        self._auto_previewed = None     # session for which show_img already opened the window
        if backend == "fake":
            for key, action, desc in self.FAKE_BINDINGS:
                self.bind(key, action, description=desc)

    MAIN_ACTIONS = {"connect", "restart_all", "stop_session", "settings", "episodes", "calibrate",
                    "toggle_node",
                    "open_dir", "quit_app", "fake_press", "toggle_preview"}

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
            yield Panel("会话", id="status")
            with Horizontal(id="hands"):
                yield Panel("左主臂", id="hand-left", classes="hand")
                yield Panel("右主臂", id="hand-right", classes="hand")
            with Horizontal():
                yield Panel("录制", id="recording")
                yield Panel("频率", id="rates")
        log = RichLog(id="log", wrap=True, markup=False, max_lines=2000)
        log.border_title = "事件"
        yield log
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "数据采集" + ("  [模拟硬件]" if self.backend == "fake" else "")
        self._write_log(Event(time.time(), "info",
                              "按 c 连接。" + ("" if self.backend == "fake" else
                                                ("连接时会自动启动从臂服务（launch_nodes）。"
                                                 if self.settings.network.auto_start_node else
                                                 "按 n 启动从臂服务，或在另一个终端运行 launch_nodes.py。"))))
        self.refresh_view()
        self.set_interval(0.1, self.refresh_view)
        if self.auto_connect:
            self.action_connect()

    # ------------------------------------------------------------------ rendering
    def _write_log(self, ev: Event) -> None:
        ts = datetime.datetime.fromtimestamp(ev.time).strftime("%H:%M:%S")
        line = Text(f"{ts} ", style="grey50")
        line.append(f"{LEVEL_ZH.get(ev.level, ev.level)} ", style=LEVEL_STYLE.get(ev.level, "white"))
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
        self._sync_preview(phase)
        active = phase in ("connecting", "ready", "running")
        # an active session shows what it runs with; otherwise show what the next connect will use
        c = self.session.settings.collect if s and active else self.settings.collect
        save_dir = s.save_dir if s and active else os.path.join(c.resolved_save_root(), c.project_name,
                                                                 "collect_data")

        status = Text()
        status.append(f" {label} ", style=style)
        backend = "模拟硬件" if self.backend == "fake" else "真机"
        status.append(f"  后端：{backend}   机型：{(s and s.robot_type) or '-'}   "
                      f"项目：{c.project_name}\n")
        status.append(f"保存目录：{save_dir}", style="grey70")
        node_text, node_style = self._node_status()
        status.append("\n从臂服务：")
        status.append(node_text, style=node_style)
        if s and s.error:
            status.append(f"\n错误：{s.error}", style="bold red")
            if phase == "error":
                status.append("   按 r 重启全部服务", style="bold yellow")
        self.query_one("#status", Panel).update(status)

        for i, side in enumerate(("left", "right")):
            t = Text()
            unlocked = bool(s and s.unlocked[i])
            servo = bool(s and s.servo[i])
            t.append("锁定 ")
            t.append_text(_flag(unlocked, "已解锁", "已锁定", "bold black on yellow"))
            t.append("\n同步 ")
            t.append_text(_flag(servo, "同步中", "未同步", "bold black on green"))
            self.query_one(f"#hand-{side}", Panel).update(t)

        t = Text()
        recording = bool(s and s.recording)
        t.append_text(_flag(recording, "● 录制中", "未录制", "bold white on red"))
        light = s.light if s else "dark"
        t.append("   指示灯：")
        t.append(f"■ {LIGHT_ZH.get(light, light)}", style=LIGHT_STYLE.get(light, "white"))
        t.append(f"\nepisode：{(s and s.episode) or '-'}   帧数：{s.frames if s else 0}"
                 f"\n本次会话已录：{s.episodes_recorded if s else 0} 条")
        self.query_one("#recording", Panel).update(t)

        t = Text()
        hz = s.loop_hz if s else 0.0
        t.append(f"控制循环：{hz:6.1f} Hz\n")
        fps = s.camera_fps if s else (0.0, 0.0, 0.0)
        t.append("相机：    ")
        for name, f in zip(("top", "left", "right"), fps):
            t.append(f"{name} ")
            t.append(f"{f:.0f}", style="green" if f > 20 else ("yellow" if f > 0 else "grey50"))
            t.append("  ")
        t.append("fps")
        self.query_one("#rates", Panel).update(t)

    # ------------------------------------------------------------------ actions
    def action_connect(self) -> None:
        if self.session and self.session.state.phase in ("connecting", "ready", "running"):
            self.notify("会话已在运行", severity="warning")
            return
        net = self.settings.network
        if (self.backend == "real" and net.auto_start_node and is_local(net.zmq_host)
                and not (self.node and self.node.alive) and not port_in_use(net.zmq_host, net.zmq_port)):
            self._start_node()
        self._begin_session()

    def _begin_session(self) -> None:
        old = self.session
        self.session = CollectorSession(self.settings, backend=self.backend)
        self.session.add_listener(self._events.put)
        self._connect_worker(self.session, self.node, old)

    @work(thread=True, exclusive=True, group="connect")
    def _connect_worker(self, session: CollectorSession, node: Optional[NodeProcess],
                        old: Optional[CollectorSession] = None) -> None:
        if old is not None:
            old.stop()   # release the previous session's hands / cameras before reopening them
        if self.backend == "real" and node is not None and node.state == "starting":
            session.log("info", "等待从臂服务就绪（连接从臂、使能，最长 60 秒）...")
            if not node.wait_ready(NODE_READY_TIMEOUT_S):
                reason = node.tail[-1] if node.tail else "无输出"
                if node.alive:
                    node.stop()
                    reason = f"{NODE_READY_TIMEOUT_S} 秒内未就绪"
                session.fail(f"从臂服务启动失败：{reason}")
                return
        try:
            session.connect()
        except Exception:
            session.stop()
            return  # the error is already in the event log / state
        session.start()

    # ------------------------------------------------------------------ restart everything
    def action_restart_all(self) -> None:
        """Stop the session, restart the robot server with its alarms cleared, reconnect."""
        if self._restarting:
            self.notify("正在重启，请稍候", severity="warning")
            return
        s = self.session
        if s and s.state.recording and time.time() - self._restart_armed > 3:
            self._restart_armed = time.time()
            self.notify("正在录制，再按一次 r 停止录制并重启全部服务", severity="warning")
            return
        self._restarting = True
        self._hide_preview()
        self._events.put(Event(time.time(), "warn", "重启全部服务：停止会话 → 重启从臂服务并清除报警 → 重新连接"))
        self._restart_worker(s, self.node)

    @work(thread=True, group="restart")
    def _restart_worker(self, session: Optional[CollectorSession], node: Optional[NodeProcess]) -> None:
        had_node = node is not None and node.state != "stopped"   # ours, running or crashed
        try:
            if session is not None:
                session.stop()
            if node is not None and node.alive:
                node.stop()
        finally:
            self.call_from_thread(self._restart_connect, had_node)

    def _restart_connect(self, had_node: bool) -> None:
        try:
            net = self.settings.network
            self._external_node = (0.0, False)
            if is_local(net.zmq_host) and port_in_use(net.zmq_host, net.zmq_port):
                self._events.put(Event(time.time(), "warn", "从臂服务是外部启动的，TUI 无法重启它；"
                                                            "如有报警，请在那个终端重启 launch_nodes"))
            elif is_local(net.zmq_host) and (had_node or (self.backend == "real" and net.auto_start_node)):
                self._start_node(clear_error=True)
            self._begin_session()
        finally:
            self._restarting = False

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
            self.notify(f"写入 ini 失败：{e}", severity="error")
            return
        self.settings = new  # a running session keeps its own settings object
        active = self.session and self.session.state.phase in ("connecting", "ready", "running")
        self.notify("设置已保存" + ("，下次连接时生效" if active else ""))
        self._events.put(Event(time.time(), "info", "设置已写入 ini"))

    # ------------------------------------------------------------------ camera window
    def _sync_preview(self, phase: str) -> None:
        if phase == "running" and self.session is not self._auto_previewed:
            self._auto_previewed = self.session
            if self.session.settings.collect.show_img:
                self._show_preview()
        if self._preview_owner is not None and (self._preview_owner is not self.session or phase != "running"):
            self._hide_preview()

    def _show_preview(self) -> None:
        s = self.session
        if not s or s.state.phase != "running":
            self.notify("请先连接（c）", severity="warning")
            return
        try:
            get_preview().show(s.latest_images, on_close=self._preview_closed)
        except RuntimeError as e:
            self.notify(f"无法打开相机窗口：{e}", severity="warning")
            return
        self._preview_owner = s
        self._events.put(Event(time.time(), "info", "相机窗口已打开（按 v 关闭）"))

    def _hide_preview(self) -> None:
        if self._preview_owner is not None:
            get_preview().hide()
            self._preview_owner = None

    def _preview_closed(self, error) -> None:
        # preview thread: the window was closed with its X button or failed
        self._preview_owner = None
        self._events.put(Event(time.time(), "warn" if error else "info",
                               f"相机窗口出错：{error}" if error else "相机窗口已关闭"))

    def action_toggle_preview(self) -> None:
        if self._preview_owner is not None and get_preview().visible:
            self._hide_preview()
            self._events.put(Event(time.time(), "info", "相机窗口已关闭"))
        else:
            self._show_preview()

    # ------------------------------------------------------------------ robot server (launch_nodes)
    def _node_log(self, level: str, message: str) -> None:
        self._events.put(Event(time.time(), level, message))

    def _node_status(self):
        n, net = self.node, self.settings.network
        if n and n.state == "running":
            return f"运行中（本界面启动，pid {n.pid}）", "green"
        if n and n.state == "starting":
            return "启动中...", "yellow"
        if not is_local(net.zmq_host):
            return f"远程 {net.zmq_host}:{net.zmq_port}（需在那台机器上启动）", "grey70"
        now = time.time()
        if now - self._external_node[0] > 2.0:
            self._external_node = (now, port_in_use(net.zmq_host, net.zmq_port))
        if self._external_node[1]:
            return f"运行中（外部启动，端口 {net.zmq_port}）", "green"
        if n and n.state == "exited":
            return f"已退出（代码 {n.returncode}），按 n 重新启动", "red"
        if self.backend == "fake":
            return "未启动（模拟硬件不需要）", "grey50"
        return ("未启动（连接时自动启动）" if net.auto_start_node else "未启动（按 n 启动）"), "grey50"

    def _start_node(self, clear_error: bool = False) -> None:
        net = self.settings.network
        self.node = NodeProcess(net.zmq_host, net.zmq_port, self._node_log, fake=self.backend == "fake",
                                clear_error=clear_error)
        try:
            self.node.start()
        except OSError as e:
            self.notify(f"无法启动从臂服务：{e}", severity="error")
        self._external_node = (0.0, False)

    def ensure_robot_server(self) -> Optional[NodeProcess]:
        """Main thread. Returns the node to wait for, or None if a server already listens.
        Raises RuntimeError when the server is remote and not reachable."""
        net = self.settings.network
        if self.node and self.node.alive:
            return self.node
        if port_in_use(net.zmq_host, net.zmq_port):
            return None
        if not is_local(net.zmq_host):
            raise RuntimeError(f"远程从臂服务 {net.zmq_host}:{net.zmq_port} 未运行")
        self._start_node()
        return self.node

    def action_toggle_node(self) -> None:
        net = self.settings.network
        if self.node and self.node.alive:
            if self._session_active():
                self.notify("请先停止会话（s），再关闭从臂服务", severity="warning")
                return
            self._stop_node_worker(self.node)
            return
        if not is_local(net.zmq_host):
            self.notify(f"从臂服务地址是 {net.zmq_host}，需要在那台机器上启动", severity="warning")
            return
        if port_in_use(net.zmq_host, net.zmq_port):
            self.notify(f"端口 {net.zmq_port} 上已有从臂服务在运行（外部启动）", severity="warning")
            return
        self._start_node()

    @work(thread=True, group="node")
    def _stop_node_worker(self, node: NodeProcess) -> None:
        self._node_log("info", "[从臂服务] 正在关闭...")
        node.stop()
        self._external_node = (0.0, False)

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
            self.notify("请先停止会话（s）：标定需要占用主臂和相机",
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
            self.notify("正在录制，再按一次 q 停止并退出", severity="warning")
            return
        self._hide_preview()
        if s:
            s.stop()
        if self.node and self.node.alive:
            self.node.stop()   # only the server this TUI started
        self.exit()

    def action_fake_press(self, hand: int, key: int, duration: float) -> None:
        if self.session and self.session.fake_hands:
            self.session.fake_hands[hand].press(key, duration)
