"""Run experiments/launch_nodes.py (the follower-arm ZMQ server) as a child process.

The TUI uses this so launch_nodes no longer needs its own terminal. The server code itself is
unchanged; it just runs in a subprocess whose output is forwarded to a log callback, with the
server's per-millisecond idle "Timeout in Robot Server" lines and similar noise dropped.
"""
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional

from dobot_control.config import REPO_ROOT

NODE_SCRIPT = REPO_ROOT / "experiments" / "launch_nodes.py"
READY_MARK = "Starting robot server on"
_NOISE_PREFIXES = ("Timeout in Robot Server", "in dobot robot")
_NUMBER_LIST = re.compile(r"^\[[-+0-9.,eE\s]*\]$")      # moveJ prints its joint list
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1")

Log = Callable[[str, str], None]


def is_local(host: str) -> bool:
    return host in LOCAL_HOSTS


def port_in_use(host: str, port: int, timeout: float = 0.3) -> bool:
    """True if something already listens on host:port (e.g. a launch_nodes started by hand)."""
    try:
        with socket.create_connection(("127.0.0.1" if host == "0.0.0.0" else host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_noise(line: str) -> bool:
    s = line.strip()
    return (not s or s.startswith(_NOISE_PREFIXES) or set(s) == {"*"} or bool(_NUMBER_LIST.match(s)))


class NodeProcess:
    """One launch_nodes.py child process. States: stopped, starting, running, exited."""

    def __init__(self, host: str, port: int, log: Log, fake: bool = False):
        self.host = host
        self.port = port
        self.fake = fake
        self._log = log
        self._proc: Optional[subprocess.Popen] = None
        self._ready = threading.Event()
        self._exited = threading.Event()
        self.returncode: Optional[int] = None
        self.tail: List[str] = []          # last output lines, for error messages
        self._stopping = False

    # ------------------------------------------------------------------ state
    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    @property
    def state(self) -> str:
        if self._proc is None:
            return "stopped"
        if self._exited.is_set():
            return "exited"
        return "running" if self._ready.is_set() else "starting"

    @property
    def alive(self) -> bool:
        return self.state in ("starting", "running")

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        if self.alive:
            return
        cmd = [sys.executable, "-u", str(NODE_SCRIPT), "--robot-port", str(self.port), "--hostname", self.host]
        if self.fake:
            cmd.append("--fake")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True   # not hit by the terminal's Ctrl-C
        self._ready.clear()
        self._exited.clear()
        self._stopping = False
        self.returncode = None
        self.tail = []
        self._proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      encoding="utf-8", errors="replace", bufsize=1, **kw)
        self._log("info", f"[从臂服务] 已启动 launch_nodes.py（pid {self._proc.pid}）")
        threading.Thread(target=self._read, args=(self._proc,), name="node-output", daemon=True).start()

    def wait_ready(self, timeout: float) -> bool:
        """Wait until the server is listening; False on timeout or if it exited."""
        deadline = time.time() + timeout
        while time.time() < deadline and not (self._ready.is_set() or self._exited.is_set()):
            time.sleep(0.05)
        return self._ready.is_set() and not self._exited.is_set()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop like Ctrl-C in its terminal would; escalate to terminate/kill if needed."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._stopping = True
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGINT)
            proc.wait(timeout=timeout / 2)
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout / 2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        self._exited.wait(2)

    # ------------------------------------------------------------------ output
    def _read(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            line = raw.rstrip()
            if _is_noise(line):
                continue
            self.tail = (self.tail + [line])[-20:]
            if READY_MARK in line:
                self._ready.set()
                self._log("info", f"[从臂服务] 就绪：{self.host}:{self.port}")
                continue
            self._log("info", f"[从臂服务] {line}")
        self.returncode = proc.wait()
        self._exited.set()
        if self.returncode == 0 or self._stopping:
            self._log("info", f"[从臂服务] 已退出（代码 {self.returncode}）")
        else:
            self._log("error", f"[从臂服务] 异常退出（代码 {self.returncode}）")
