"""Hardware-free stand-ins for the leader hands, follower arms and cameras.

Used by `CollectorSession(backend="fake")` so the collection core, TUI and GUI can be
developed and tested without robots attached. Interfaces mirror the real classes:
  FakeLeaderHand  ~ DobotAgent       (act / set_torque / get_keys)
  FakeRobot       ~ ZMQClientRobot   (num_dofs / get_observations / command_joint_state / set_do_status)
  FakeCamera      ~ RealSenseCamera  (read -> (rgb, depth))
"""
import threading
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

# Same poses robot_pose_init() drives to; the fake leader hovers around them.
HOME_LEFT = np.deg2rad([-90, 0, -90, 0, 90, 90])
HOME_RIGHT = np.deg2rad([90, 0, 90, 0, -90, -90])

KEY_A, KEY_B, KEY_SENSOR = 0, 1, 2


class FakeLeaderHand:
    """One master hand: slowly oscillating joints and pressable A/B buttons."""

    def __init__(self, which_hand: str, amplitude: float = 0.05, period_s: float = 6.0):
        self.which_hand = which_hand
        self._home = HOME_LEFT if which_hand == "LEFT" else HOME_RIGHT
        self._amplitude = amplitude
        self._period = period_s
        self._t0 = time.time()
        self.torque_enable = True
        # idle state of the real buttons is 1, pressed is 0; index 2 is the fall sensor
        self._keys = np.array([1, 1, 0])
        self._lock = threading.Lock()

    def act(self, obs: Dict[str, np.ndarray]) -> np.ndarray:
        phase = 2 * np.pi * (time.time() - self._t0) / self._period
        joints = self._home + self._amplitude * np.sin(phase + np.arange(6))
        gripper = 0.5 + 0.4 * np.sin(phase)
        return np.append(joints, gripper)

    def set_torque(self, _flag=False):
        self.torque_enable = _flag

    def get_keys(self) -> np.ndarray:
        time.sleep(0.002)  # the real driver polls the bus; avoid a hot spin
        with self._lock:
            return self._keys.copy()

    def press(self, key: int, duration_s: float = 0.1) -> None:
        """Press and release a button in the background (A short <0.5s, A long >1s)."""
        def _run():
            with self._lock:
                self._keys[key] = 0
            time.sleep(duration_s)
            with self._lock:
                self._keys[key] = 1
        threading.Thread(target=_run, daemon=True).start()


class FakeRobot:
    """Both follower arms (14 dof: 6 joints + gripper per arm)."""

    def __init__(self, latency_s: float = 0.004):
        self._joints = np.concatenate([HOME_LEFT + 0.3, [1.0], HOME_RIGHT - 0.3, [1.0]])
        self._latency = latency_s
        self._lock = threading.Lock()
        self.do_status: Dict[int, int] = {1: 0, 2: 0, 3: 0}

    def num_dofs(self) -> int:
        return 14

    def get_joint_state(self) -> np.ndarray:
        with self._lock:
            return self._joints.copy()

    def command_joint_state(self, joint_state: np.ndarray, flag_in) -> None:
        time.sleep(self._latency)
        with self._lock:
            if flag_in[0]:
                self._joints[:7] = joint_state[:7]
            if flag_in[1]:
                self._joints[7:] = joint_state[7:]
        return 1

    def get_observations(self) -> Dict[str, np.ndarray]:
        joints = self.get_joint_state()
        # Mirror DobotRobot: follower gripper always reads 1.0, velocities == positions
        joints[6] = joints[13] = 1.0
        return {
            "joint_positions": joints,
            "joint_velocities": joints.copy(),
            "ee_pos_quat": np.zeros(14),
            "gripper_position": np.array([1.0, 1.0]),
        }

    def set_do_status(self, which_do) -> None:
        self.do_status[int(which_do[0])] = int(which_do[1])

    def get_XYZrxryrz_state(self) -> np.ndarray:
        return np.zeros(12)


class FakeCamera:
    """Synthetic 640x480 RGB frames at ~30 fps with the camera name and a moving bar."""

    def __init__(self, name: str, fps: float = 30.0):
        self.name = name
        self._period = 1.0 / fps
        self._next = time.time()
        self._count = 0

    def read(self, img_size: Optional[Tuple[int, int]] = None) -> Tuple[np.ndarray, np.ndarray]:
        delay = self._next - time.time()
        if delay > 0:
            time.sleep(delay)
        self._next = max(self._next + self._period, time.time())
        self._count += 1
        img = np.full((480, 640, 3), 40, dtype=np.uint8)
        x = (self._count * 8) % 640
        img[:, x:x + 20] = (200, 120, 40)
        cv2.putText(img, f"{self.name} #{self._count}", (20, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3)
        return img, np.zeros((480, 640, 1), dtype=np.uint16)


def build_fake_backend() -> Tuple[List[FakeCamera], List[FakeLeaderHand], FakeRobot]:
    cameras = [FakeCamera("top"), FakeCamera("left"), FakeCamera("right")]
    hands = [FakeLeaderHand("LEFT"), FakeLeaderHand("RIGHT")]
    return cameras, hands, FakeRobot()


# --------------------------------------------------------------------------- calibration fakes
FAKE_PORTS = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyUSB0", "/dev/ttyUSB1"]


class _FakeDevice:
    def __init__(self, joints: Optional[np.ndarray] = None):
        self._joints = joints

    def get_joints(self) -> np.ndarray:
        return self._joints

    def close(self) -> None:
        pass


def fake_calibration_hooks() -> Dict[str, object]:
    """Keyword arguments for dobot_control.calibration functions, keyed by function name.

    Left hand answers on ttyACM1 @ 1M baud, right hand on ttyACM0 @ 2M, grippers on the
    USB ports (by id); offsets read back as the ini offsets plus a little noise; cameras are the ini serials.
    """
    from dobot_control.config import load_ini_data_camera, load_ini_data_gripper, load_ini_data_hands
    _, hands = load_ini_data_hands()
    hand_at = {(tuple(hands["HAND_LEFT"].joint_ids), "/dev/ttyACM1", 1000000),
               (tuple(hands["HAND_RIGHT"].joint_ids), "/dev/ttyACM0", 2000000)}

    def open_hand(cfg, port, baud):
        if (tuple(cfg.joint_ids), port, baud) not in hand_at:
            raise RuntimeError("no answer")
        return _FakeDevice()

    _, grippers = load_ini_data_gripper()
    gripper_at = {(grippers["GRIPPER_LEFT"].id_name, "/dev/ttyUSB1"),
                  (grippers["GRIPPER_RIGHT"].id_name, "/dev/ttyUSB0")}

    def open_gripper(cfg, port):
        if (cfg.id_name, port) not in gripper_at:
            raise RuntimeError("ping failed...")
        return _FakeDevice()

    rng = np.random.default_rng()

    def open_offset_driver(section, cfg):
        raw = (np.array(cfg.start_joints) * np.array(cfg.joint_signs) + np.array(cfg.joint_offsets)
               + rng.normal(0, 0.01, 6))
        return _FakeDevice(np.append(raw, np.deg2rad(171.5)))

    def open_agent():
        from dobot_control.agents.agent import BimanualAgent
        return BimanualAgent(FakeLeaderHand("LEFT"), FakeLeaderHand("RIGHT"))

    serials = list(load_ini_data_camera().values())
    return {
        "find_ports": dict(list_ports=lambda: list(FAKE_PORTS), open_hand=open_hand, open_gripper=open_gripper),
        "read_offsets": dict(open_driver=open_offset_driver, prepare_ports=None),
        "HandCheck": dict(open_agent=open_agent),
        "check_cameras": dict(list_ids=lambda: list(serials)),
        "open_cameras": dict(open_camera=lambda key, serial: FakeCamera(f"{key} {serial}")),
    }
