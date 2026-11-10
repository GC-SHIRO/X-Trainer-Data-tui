"""Calibration steps from scripts/1_find_port, 2_get_offset, 3_just_buttonA and 5_camera_read,
as functions that the TUI (and later the GUI) can call.

Every step that touches hardware takes an optional factory so a fake backend can be injected;
the defaults open the real Dynamixel / Feetech / RealSense devices. Nothing is written to the
ini until the matching save_*() function is called, so the UI can show results first.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from dobot_control.config import (load_ini, load_ini_data_camera, load_ini_data_gripper,
                                  load_ini_data_hands, save_ini)

HAND_SECTIONS = ("HAND_LEFT", "HAND_RIGHT")
GRIPPER_SECTIONS = ("GRIPPER_LEFT", "GRIPPER_RIGHT")
CAMERA_KEYS = ("top", "left", "right")
HAND_BAUD_RATES = (2000000, 1000000)
GRIPPER_IDS = {"HAND_LEFT": 8, "HAND_RIGHT": 18}   # Dynamixel id of the master-hand trigger

Log = Callable[[str, str], None]


def _nolog(level: str, msg: str) -> None:
    pass


def _close(dev) -> None:
    for name in ("close", "disconnect"):
        fn = getattr(dev, name, None)
        if fn:
            try:
                fn()
            except Exception:
                pass
            return


def _fmt_list(values) -> str:
    # same text format the original scripts wrote: str(list) without brackets
    return str(list(values)).replace("[", "").replace("]", "")


# --------------------------------------------------------------------------- 1. find ports
@dataclass
class PortScanResult:
    ports: List[str]
    hands: Dict[str, Tuple[str, int]] = field(default_factory=dict)   # section -> (port, baud)
    grippers: Dict[str, str] = field(default_factory=dict)            # section -> port
    leftover: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return (len(self.hands) == 2 and len(self.grippers) == 2 and not self.leftover
                and not self.errors)


def _open_hand(cfg, port: str, baud: int):
    from dobot_control.dynamixel.driver import DynamixelDriver
    return DynamixelDriver(ids=cfg.joint_ids, append_id=cfg.append_id, port=port, baudrate=baud)


def _open_gripper(cfg, port: str):
    from dobot_control.gripper.dobot_gripper import DobotGripper
    return DobotGripper(port=port, servo_pos=cfg.pos, id_name=cfg.id_name)


def _scan_ports() -> List[str]:
    from dobot_control.utils import scan_port
    return scan_port()


def find_ports(log: Log = _nolog, list_ports: Callable[[], List[str]] = _scan_ports,
               open_hand=_open_hand, open_gripper=_open_gripper) -> PortScanResult:
    """Probe every serial port: master hands (Dynamixel, 2M or 1M baud), then grippers (Feetech).

    Unlike the original script this stops at the first match per device and closes every
    probe connection; the assignment rule (hands first, grippers on the remaining ports) is
    the same.
    """
    ports = list(list_ports())
    res = PortScanResult(ports=list(ports))
    log("info", f"串口：{', '.join(ports) or '无'}")
    if len(ports) < 4:
        res.errors.append(f"应至少有 4 个串口（2 个主手 + 2 个夹爪），只找到 {len(ports)} 个")

    _, hands = load_ini_data_hands()
    remaining = list(ports)
    for section in HAND_SECTIONS:
        found = None
        for port in remaining:
            for baud in HAND_BAUD_RATES:
                try:
                    dev = open_hand(hands[section], port, baud)
                except Exception:
                    continue
                _close(dev)
                found = (port, baud)
                break
            if found:
                break
        if found:
            remaining.remove(found[0])
            res.hands[section] = found
            log("info", f"{section}: {found[0]} @ {found[1]}")
        else:
            res.errors.append(f"{section}：没有串口响应")
            log("warn", f"{section}：未找到")

    _, grippers = load_ini_data_gripper()
    for section in GRIPPER_SECTIONS:
        found = None
        for port in remaining:
            try:
                dev = open_gripper(grippers[section], port)
            except Exception as e:
                log("info", f"{section}：{port} 无响应（{e or type(e).__name__}）")
                continue
            _close(dev)
            found = port
            break
        if found:
            remaining.remove(found)
            res.grippers[section] = found
            log("info", f"{section}: {found}")
        else:
            res.errors.append(f"{section}：没有串口响应")
            log("warn", f"{section}：未找到")

    res.leftover = remaining
    if remaining:
        log("warn", f"未分配的串口：{', '.join(remaining)}")
    return res


def save_ports(res: PortScanResult) -> None:
    """Write the ports that were found (missing devices keep their old entry)."""
    ini = load_ini()
    for section, (port, baud) in res.hands.items():
        ini.set(section, "port", port)
        ini.set(section, "baud_rate", str(baud))
    for section, port in res.grippers.items():
        ini.set(section, "port", port)
    save_ini(ini)


# --------------------------------------------------------------------------- 2. joint offsets
@dataclass
class HandOffset:
    section: str
    raw_joints: List[float]          # 6 raw joint angles (rad) read from the driver
    start_joints: List[float]
    joint_signs: List[int]
    offsets: List[float]             # new joint_offsets
    gripper_config: List[int]        # new [id, close_deg, open_deg]
    old_offsets: List[float]
    old_gripper_config: List[int]

    @property
    def max_change(self) -> float:
        return float(np.max(np.abs(np.array(self.offsets) - np.array(self.old_offsets))))


def _open_offset_driver(section: str, cfg):
    from dobot_control.dynamixel.driver import DynamixelDriver
    return DynamixelDriver(ids=list(cfg.joint_ids) + [GRIPPER_IDS[section]], append_id=cfg.append_id,
                           port=cfg.port, baudrate=cfg.baud_rate)


def compute_offsets(section: str, cfg, pos_joint: Sequence[float]) -> HandOffset:
    """Same formulas as scripts/2_get_offset.py; pos_joint = 6 joints + trigger (rad)."""
    curr = list(pos_joint[:6])
    offsets = [float("%.2f" % (curr[i] - cfg.start_joints[i] * cfg.joint_signs[i])) for i in range(6)]
    gripper_open = int(np.rad2deg(pos_joint[-1]) - 0.2)
    gripper_close = int(np.rad2deg(pos_joint[-1]) + 30)
    return HandOffset(section=section, raw_joints=[float(x) for x in curr],
                      start_joints=list(cfg.start_joints), joint_signs=list(cfg.joint_signs),
                      offsets=offsets, gripper_config=[GRIPPER_IDS[section], gripper_close, gripper_open],
                      old_offsets=list(cfg.joint_offsets), old_gripper_config=list(cfg.gripper_config))


def read_offsets(log: Log = _nolog, open_driver=_open_offset_driver,
                 prepare_ports: Optional[Callable[[], object]] = _scan_ports) -> Dict[str, HandOffset]:
    """Read both master hands (held in the start_joints pose) and compute new offsets."""
    if prepare_ports:
        prepare_ports()  # chmod the serial ports, as the original script did
    _, hands = load_ini_data_hands()
    out = {}
    for section in HAND_SECTIONS:
        cfg = hands[section]
        dev = open_driver(section, cfg)
        try:
            pos = dev.get_joints()
        finally:
            _close(dev)
        out[section] = compute_offsets(section, cfg, pos)
        log("info", f"{section}：零位 {out[section].offsets}，夹爪 {out[section].gripper_config}")
    return out


def save_offsets(results: Dict[str, HandOffset]) -> None:
    ini = load_ini()
    for section, r in results.items():
        ini.set(section, "joint_offsets", _fmt_list(r.offsets))
        ini.set(section, "gripper_config", _fmt_list(r.gripper_config))
    save_ini(ini)


# --------------------------------------------------------------------------- 3. hand check
@dataclass
class HandCheckState:
    running: bool = False
    joints: Optional[np.ndarray] = None     # 14: left 6 + trigger, right 6 + trigger (calibrated)
    keys: Optional[np.ndarray] = None       # 2x3 raw key states (1 = idle)
    unlocked: Tuple[bool, bool] = (False, False)
    error: Optional[str] = None


def _open_hands_agent():
    from dobot_control.agents.agent import BimanualAgent
    from dobot_control.agents.dobot_agent import DobotAgent
    _, hands = load_ini_data_hands()
    return BimanualAgent(DobotAgent("LEFT", hands["HAND_LEFT"]), DobotAgent("RIGHT", hands["HAND_RIGHT"]))


class HandCheck:
    """scripts/3_just_buttonA.py: short-press A to lock/unlock a master hand (torque off =
    free to move) and watch its calibrated joint angles, e.g. to adjust the handles."""

    def __init__(self, log: Log = _nolog, open_agent=_open_hands_agent, period_s: float = 0.035):
        self._log = log
        self._open_agent = open_agent
        self._period = period_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = HandCheckState()
        self._thread: Optional[threading.Thread] = None
        self.agent = None
        self.buttons = None

    def start(self) -> None:
        from dobot_control.collector import ButtonMonitor
        self.agent = self._open_agent()
        self.buttons = ButtonMonitor(self.agent, lambda: None, self._log)
        self.buttons.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hand-check", daemon=True)
        self._thread.start()
        self._log("info", "手柄检查已开始：短按 A 解锁/锁定主手")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self.buttons:
            self.buttons.stop()
        if self.agent is not None:
            for i in range(2):  # leave both hands locked (torque on), like a fresh start
                try:
                    self.agent.set_torque(i, True)
                except Exception:
                    pass
        with self._lock:
            self._state.running = False

    def snapshot(self) -> HandCheckState:
        with self._lock:
            return HandCheckState(**self._state.__dict__)

    def _run(self) -> None:
        last = np.zeros((2, 3), dtype=int)
        with self._lock:
            self._state.running = True
        try:
            while not self._stop.is_set():
                tic = time.time()
                joints = np.asarray(self.agent.act({}), dtype=float)
                what = self.buttons.snapshot()
                for i in range(2):
                    if what[i, 0] != last[i, 0]:
                        self.agent.set_torque(i, not what[i, 0])
                last = what
                keys = np.asarray(self.agent.get_keys())
                with self._lock:
                    self._state.joints = joints
                    self._state.keys = keys
                    self._state.unlocked = (bool(what[0, 0]), bool(what[1, 0]))
                time.sleep(max(0.0, self._period - (time.time() - tic)))
        except Exception as e:
            with self._lock:
                self._state.error = str(e) or type(e).__name__
            self._log("error", f"手柄检查中止：{self._state.error}")
        finally:
            with self._lock:
                self._state.running = False


# --------------------------------------------------------------------------- 4. cameras
@dataclass
class CameraCheck:
    found: List[str]
    assigned: Dict[str, str]          # top/left/right -> serial from the ini

    @property
    def missing(self) -> Dict[str, str]:
        return {k: v for k, v in self.assigned.items() if v not in self.found}

    @property
    def unassigned(self) -> List[str]:
        return [s for s in self.found if s not in self.assigned.values()]


def _realsense_ids() -> List[str]:
    from dobot_control.cameras.realsense_camera import get_device_ids
    return get_device_ids()


def check_cameras(list_ids: Callable[[], List[str]] = _realsense_ids) -> CameraCheck:
    """List connected RealSense serials and compare with [CAMERA] (resets the devices, ~2 s)."""
    return CameraCheck(found=list(list_ids()), assigned=load_ini_data_camera())


def save_cameras(assigned: Dict[str, str]) -> None:
    values = [assigned[k] for k in CAMERA_KEYS]
    if len(set(values)) != len(values):
        raise ValueError("三个相机必须是不同的序列号")
    ini = load_ini()
    for k in CAMERA_KEYS:
        ini.set("CAMERA", k, assigned[k])
    save_ini(ini)


CAMERA_FLIPS = {"top": True, "left": False, "right": True}   # as in collector / 5_camera_read


def _open_realsense(key: str, serial: str):
    from dobot_control.cameras.realsense_camera import RealSenseCamera
    return RealSenseCamera(flip=CAMERA_FLIPS[key], device_id=serial)


def open_cameras(assigned: Dict[str, str], open_camera=_open_realsense) -> List[object]:
    """Open top/left/right for the preview window; closes what was opened if one fails."""
    cams = []
    try:
        for k in CAMERA_KEYS:
            cams.append(open_camera(k, assigned[k]))
    except Exception:
        close_cameras(cams)
        raise
    return cams


def close_cameras(cams) -> None:
    for c in cams:
        fn = getattr(c, "stop", None)
        if fn:
            try:
                fn()
            except Exception:
                pass


def camera_frames(cams) -> List[np.ndarray]:
    """One BGR frame per camera (read() returns RGB), like scripts/5_camera_read.py."""
    return [c.read()[0][:, :, ::-1] for c in cams]


# --------------------------------------------------------------------------- follower home pose
def move_followers_home(host: str, port: int, log: Log = _nolog, timeout_ms: int = 10000) -> None:
    """Drive both follower arms to the home pose (same motion as at connect) through the robot
    server, e.g. as a visual reference before reading the master-hand offsets."""
    from dobot_control.env import RobotEnv
    from dobot_control.motion import robot_pose_init
    from dobot_control.robots.robot_node import ZMQClientRobot
    client = ZMQClientRobot(port=port, host=host, timeout_ms=timeout_ms)
    try:
        env = RobotEnv(client)
        log("info", "从臂正在回到初始位...")
        robot_pose_init(env)
        log("info", "从臂已到初始位")
    finally:
        client.close()
