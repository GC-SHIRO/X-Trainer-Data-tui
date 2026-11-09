"""Teleoperation + recording core, shared by the CLI (experiments/run_control.py), TUI and GUI.

The control logic is a port of the original run_control.py loop:
  * master-hand buttons -> lock/unlock (A short), servo on/off (A long), record start/stop (B)
  * when a hand starts servoing the follower is interpolated to the leader pose first
  * every servo step is jump-checked and safety-checked before being sent
  * while recording, each step saves topImg/leftImg/rightImg/{idx}.jpg and observation/{idx}.pkl

Differences from the original script (intentional):
  * frame index restarts at 1 for every episode (was: kept counting across episodes)
  * a new episode never reuses an existing directory (same-second starts get stamp+1)
  * errors (jump check, safety, camera death) stop the session with a red light and an
    `error` state instead of assert/exit(); the process is not killed
"""
import collections
import datetime
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Deque, List, Optional, Tuple

import cv2
import numpy as np

from dobot_control.settings import Settings
from dobot_control.utils import mk_dir, wait_period
from dobot_control.format_obs import save_frame
from dobot_control.motion import dynamic_approach, robot_pose_init, servo_action_check, set_light
from dobot_control.safety import check_command

CAMERA_NAMES = ("top", "left", "right")
IMAGE_DIRS = ("topImg", "leftImg", "rightImg")
HANDS = ("left", "right")
HAND_ZH = ("左手", "右手")


class CollectorError(RuntimeError):
    pass


@dataclass
class Event:
    time: float
    level: str        # "info" | "warn" | "error"
    message: str


@dataclass
class CollectorState:
    phase: str = "idle"                    # idle | connecting | ready | running | stopped | error
    unlocked: Tuple[bool, bool] = (False, False)
    servo: Tuple[bool, bool] = (False, False)
    recording: bool = False
    light: str = "dark"
    robot_type: Optional[str] = None
    episode: Optional[str] = None          # current/last episode directory name
    frames: int = 0                        # frames saved in the current/last episode
    episodes_recorded: int = 0
    loop_hz: float = 0.0
    camera_fps: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    error: Optional[str] = None
    save_dir: str = ""


# --------------------------------------------------------------------------- cameras
class CameraWorker:
    """Continuously reads one camera and keeps the latest BGR frame."""

    def __init__(self, camera, index: int, top_crop: Tuple[float, float, float, float]):
        self._camera = camera
        self.index = index
        self.name = CAMERA_NAMES[index]
        self._crop = top_crop
        self._lock = threading.Lock()
        self._frame = np.zeros((480, 640, 3), dtype=np.uint8)
        self._stop = threading.Event()
        self._times: Deque[float] = collections.deque(maxlen=30)
        self.error: Optional[str] = None
        self._thread = threading.Thread(target=self._run, name=f"cam-{self.name}", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def alive(self) -> bool:
        return self._thread.is_alive()

    def _process(self, rgb: np.ndarray) -> np.ndarray:
        if self.index != 0:
            return rgb[:, :, ::-1]
        # top camera: crop each side by a fraction, then scale back to the source size
        h, w, _ = rgb.shape
        top_pct, bottom_pct, left_pct, right_pct = self._crop
        top_px = max(0, min(int(top_pct * h), h - 1))
        bottom_px = max(0, min(int(bottom_pct * h), h - 1 - top_px))
        left_px = max(0, min(int(left_pct * w), w - 1))
        right_px = max(0, min(int(right_pct * w), w - 1 - left_px))
        cropped = rgb[top_px:h - bottom_px, left_px:w - right_px, ::-1]
        return cv2.resize(cropped, (w, h))

    def _run(self):
        try:
            while not self._stop.is_set():
                rgb, _ = self._camera.read()
                frame = self._process(rgb)
                with self._lock:
                    self._frame = frame
                self._times.append(time.time())
        except Exception as e:  # camera unplugged, librealsense timeout, ...
            self.error = f"{self.name} 相机：{e}"

    def latest(self) -> np.ndarray:
        with self._lock:
            return self._frame

    def fps(self) -> float:
        t = list(self._times)
        if len(t) < 2 or time.time() - t[-1] > 1.0:
            return 0.0
        return (len(t) - 1) / (t[-1] - t[0])


# --------------------------------------------------------------------------- buttons
class ButtonMonitor:
    """Translates master-hand button edges into the what_to_do[2, 3] state.

    what_to_do[i] = [unlocked, servo, recording]; recording is only used in row 0.
    Logic is identical to button_monitor_realtime() in the original run_control.py:
    B (either hand) toggles recording, but only starts it while at least one hand servos.
    """

    def __init__(self, agent, on_new_episode: Callable[[], None], log: Callable[[str, str], None],
                 sensor_protection: bool = False):
        self._agent = agent
        self._on_new_episode = on_new_episode
        self._log = log
        self._sensor_protection = sensor_protection
        self.lock = threading.Lock()
        self.what_to_do = np.zeros((2, 3), dtype=int)
        self.keys_press_count = np.zeros((2, 3), dtype=int)
        self.falling = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="buttons", daemon=True)

    def start(self):
        # The original compared the first read against all-zeros; with idle buttons (=1) that
        # produced a phantom B release which armed recording (count 0 -> 1). Take the first real
        # read as the baseline (synchronously, so no press can slip in before it) and arm B
        # explicitly: same behaviour, without phantom edges.
        self._baseline = self._agent.get_keys()
        self.keys_press_count[0, 2] = 1
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> np.ndarray:
        with self.lock:
            return self.what_to_do.copy()

    # Programmatic equivalents of a B press (for keyboard / UI control).
    def request_recording(self, on: bool) -> bool:
        with self.lock:
            servo_any = self.what_to_do[0, 1] or self.what_to_do[1, 1]
            if on and not self.what_to_do[0, 2] and servo_any:
                self._start_recording_locked()
                self.keys_press_count[0, 2] = (self.keys_press_count[0, 2] | 1) + 1  # even: next B stops
                return True
            if not on and self.what_to_do[0, 2]:
                self.what_to_do[0, 2] = 0
                self.keys_press_count[0, 2] = self.keys_press_count[0, 2] | 1        # odd: next B starts
                return True
        return False

    def _start_recording_locked(self):
        self.what_to_do[0, 2] = 1
        self._on_new_episode()

    def _run(self):
        last_keys = self._baseline
        start_press = np.zeros((2, 2), dtype=int)
        tic = [0.0, 0.0]
        while not self._stop.is_set() and not self.falling:
            now_keys = self._agent.get_keys()
            dev_keys = now_keys - last_keys
            with self.lock:
                # button A: short press -> lock/unlock, long press -> servo on/off
                for i in range(2):
                    if dev_keys[i, 0] == -1:
                        tic[i] = time.time()
                        start_press[i, 0] = 1
                    if dev_keys[i, 0] == 1 and start_press[i, 0]:
                        start_press[i, 0] = 0
                        held = time.time() - tic[i]
                        if held < 0.5:
                            self.keys_press_count[i, 0] += 1
                            self.what_to_do[i, 0] = self.keys_press_count[i, 0] % 2
                            self._log("info", f"{HAND_ZH[i]} A 键："
                                              f"{'解锁' if self.what_to_do[i, 0] else '锁定'}")
                        elif held > 1:
                            self.keys_press_count[i, 1] += 1
                            self.what_to_do[i, 1] = self.keys_press_count[i, 1] % 2
                            self._log("info", f"{HAND_ZH[i]} A 键："
                                              f"{'开始同步' if self.what_to_do[i, 1] else '停止同步'}")
                # button B (either hand): toggle recording while at least one hand servos
                for i in range(2):
                    if dev_keys[i, 1] == -1:
                        start_press[i, 1] = 1
                    if dev_keys[i, 1] == 1:
                        start_press[i, 1] = 0
                        if self.keys_press_count[0, 2] % 2 == 1:
                            if self.keys_press_count[0, 1] % 2 == 1 or self.keys_press_count[1, 1] % 2 == 1:
                                self._start_recording_locked()
                                self.keys_press_count[0, 2] += 1
                        else:
                            self.what_to_do[0, 2] = 0
                            self.keys_press_count[0, 2] += 1
                if self._sensor_protection:
                    for i in range(2):
                        if now_keys[i, 2] and self.what_to_do[i, 0]:
                            self._agent.set_torque(2, True)
                            self.falling = True
            last_keys = now_keys


# --------------------------------------------------------------------------- session
class CollectorSession:
    def __init__(self, settings: Settings, backend: str = "real"):
        assert backend in ("real", "fake")
        self.settings = settings
        self.backend = backend
        self.state = CollectorState(save_dir=self._save_dir())
        self.events: Deque[Event] = collections.deque(maxlen=500)
        self._listeners: List[Callable[[Event], None]] = []
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self.fake_hands = None  # FakeLeaderHand pair when backend == "fake" (for tests / UI)
        self.cameras: List[CameraWorker] = []
        self.buttons: Optional[ButtonMonitor] = None
        self.env = None
        self.agent = None
        # loop variables (same names as the original script)
        self._last_status = np.zeros((2, 3), dtype=int)
        self._start_servo = False
        self._flag_in = np.array([0, 0])
        self._last_action = None
        self._obs = None
        self._safe_limit = 0
        self._curr_light = "dark"
        self._total_time = 0.04
        self._episode_stamp = 0
        self._idx = 0

    # ------------------------------------------------------------- events / state
    def add_listener(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def log(self, level: str, message: str) -> None:
        ev = Event(time.time(), level, message)
        self.events.append(ev)
        for fn in self._listeners:
            try:
                fn(ev)
            except Exception:
                pass

    def _set(self, **kw) -> None:
        with self._state_lock:
            self.state = replace(self.state, **kw)

    def snapshot(self) -> CollectorState:
        with self._state_lock:
            st = self.state
        cams = tuple(round(c.fps(), 1) for c in self.cameras) if self.cameras else (0.0, 0.0, 0.0)
        return replace(st, camera_fps=cams)

    def latest_images(self) -> List[np.ndarray]:
        return [c.latest() for c in self.cameras]

    def _save_dir(self) -> str:
        c = self.settings.collect
        return os.path.join(c.resolved_save_root(), c.project_name, "collect_data")

    # ------------------------------------------------------------- lifecycle
    def connect(self) -> None:
        """Open cameras, master hands and follower arms, then move the arms home."""
        self._set(phase="connecting", error=None)
        try:
            self._connect()
        except Exception as e:
            self._fail(str(e), light=False)
            raise
        self._set(phase="ready")
        self.log("info", "就绪：长按 A 开始同步，按 B 开始录制")

    def _connect(self) -> None:
        net = self.settings.network
        mk_dir(self.state.save_dir)

        if self.backend == "fake":
            from dobot_control.agents.agent import BimanualAgent
            from dobot_control.fake import build_fake_backend
            cams, hands, robot_client = build_fake_backend()
            self.fake_hands = hands
            self.agent = BimanualAgent(hands[0], hands[1])
            robot_type = "Nova 2" if net.robot_type == "auto" else net.robot_type
        else:
            from dobot_control.agents.agent import BimanualAgent
            from dobot_control.agents.dobot_agent import DobotAgent
            from dobot_control.cameras.realsense_camera import RealSenseCamera
            from dobot_control.config import load_ini_data_camera, load_ini_data_hands
            from dobot_control.robots.controller_info import firmware_error, get_robot_type
            from dobot_control.robots.robot_node import ZMQClientRobot

            camera_ids = load_ini_data_camera()
            self.log("info", "正在打开相机...")
            cams = [RealSenseCamera(flip=True, device_id=camera_ids["top"]),
                    RealSenseCamera(flip=False, device_id=camera_ids["left"]),
                    RealSenseCamera(flip=True, device_id=camera_ids["right"])]
            self.log("info", "正在打开主手...")
            _, hands_dict = load_ini_data_hands()
            self.agent = BimanualAgent(DobotAgent("LEFT", hands_dict["HAND_LEFT"]),
                                       DobotAgent("RIGHT", hands_dict["HAND_RIGHT"]))
            self.log("info", f"正在连接从臂服务 {net.zmq_host}:{net.zmq_port}...")
            robot_client = ZMQClientRobot(port=net.zmq_port, host=net.zmq_host)
            if net.check_firmware:
                for ip, name in ((net.left_ip, "左臂"), (net.right_ip, "右臂")):
                    err = firmware_error(ip, name)
                    if err:
                        raise CollectorError(err)
            robot_type = get_robot_type(net.left_ip) if net.robot_type == "auto" else net.robot_type
            if robot_type is None:
                raise CollectorError(f"无法从 {net.left_ip} 获取机型")

        self.cameras = [CameraWorker(cam, i, self.settings.collect.top_crop) for i, cam in enumerate(cams)]
        for c in self.cameras:
            c.start()
        time.sleep(2)  # let the camera threads fill their first frames (as before)
        self.log("info", "相机线程已启动")

        from dobot_control.env import RobotEnv
        self.env = RobotEnv(robot_client)
        for do in (1, 2, 3):
            self.env.set_do_status([do, 0])
        self.log("info", f"机型：{robot_type}，从臂正在回到初始位...")
        robot_pose_init(self.env)
        self._set(robot_type=robot_type, light="dark")

        self.buttons = ButtonMonitor(self.agent, self._new_episode, self.log)
        self.buttons.start()

    def start(self) -> None:
        """Run the control loop in a background thread."""
        if self.state.phase != "ready":
            raise CollectorError(f"当前状态 {self.state.phase!r} 下无法启动")
        self._stop.clear()
        self._loop_thread = threading.Thread(target=self.run, name="collector-loop", daemon=True)
        self._loop_thread.start()

    def run(self) -> None:
        """Run the control loop in the calling thread until stop() or an error."""
        self._set(phase="running")
        try:
            while not self._stop.is_set():
                self._step()
        except Exception as e:
            self._fail(str(e) or type(e).__name__)
            return
        if self.state.recording:
            self._set(episodes_recorded=self.state.episodes_recorded + 1)
            self.log("info", f"已停止录制 {self.state.episode}（{self.state.frames} 帧）")
        self._set(phase="stopped", recording=False)
        self.log("info", "会话已停止")

    def stop(self) -> None:
        self._stop.set()
        if self._loop_thread and self._loop_thread is not threading.current_thread():
            self._loop_thread.join(timeout=5)
        if self.buttons:
            self.buttons.stop()
        for c in self.cameras:
            c.stop()
        if self.state.phase in ("ready", "connecting"):
            self._set(phase="stopped")

    def _fail(self, message: str, light: bool = True) -> None:
        self.log("error", message)
        if light and self.env is not None:
            try:
                set_light(self.env, "red", 1)
            except Exception:
                pass
        self._set(phase="error", error=message, recording=False, light="red" if light else self.state.light)
        if self.buttons:
            self.buttons.stop()
        for c in self.cameras:
            c.stop()

    # ------------------------------------------------------------- recording API
    def start_recording(self) -> bool:
        """Same as pressing B to start; only works while at least one hand servos."""
        return bool(self.buttons and self.buttons.request_recording(True))

    def stop_recording(self) -> bool:
        return bool(self.buttons and self.buttons.request_recording(False))

    def _new_episode(self) -> None:
        # called by ButtonMonitor (under its lock) when recording starts
        stamp = int(datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
        while stamp <= self._episode_stamp or os.path.exists(os.path.join(self.state.save_dir, str(stamp))):
            stamp += 1
        self._episode_stamp = stamp
        self._idx = 0
        self._set(episode=str(stamp), frames=0)

    def _light(self, color: str, status: int, track: bool = True) -> None:
        """Switch the tower light. `track=False` reproduces the original script, which turned
        the light off on servo stop without updating its `curr_light` variable."""
        set_light(self.env, color, status)
        if track:
            self._curr_light = color
        self._set(light=color if status else "dark")

    # ------------------------------------------------------------- one control step
    def _step(self) -> None:
        tic = time.time()
        for c in self.cameras:
            if c.error or not c.alive():
                raise CollectorError(c.error or f"{c.name} 相机线程退出，请检查相机")
        if self.buttons.falling:
            raise CollectorError("主手跌落传感器触发")

        agent, env = self.agent, self.env
        what_to_do = self.buttons.snapshot()
        dev = what_to_do - self._last_status
        self._last_status = what_to_do

        # button A short press: lock / unlock
        for i in range(2):
            if dev[i, 0] != 0:
                agent.set_torque(i, not what_to_do[i, 0])

        # button A long press: a hand started servoing -> approach the leader pose first
        if dev[0, 1] == 1 or dev[1, 1] == 1:
            self.log("info", "从臂正在插值靠近主手姿态")
            for i in range(2):
                if what_to_do[i, 1]:
                    agent.set_torque(i, True)
            self._flag_in = np.array([what_to_do[0, 1], what_to_do[1, 1]])
            try:
                self._last_action = dynamic_approach(env, agent, self._flag_in)
            except AssertionError:
                raise CollectorError("主手与从臂姿态相差过大，"
                                     "请把主手摆到接近从臂的姿态后重试")
            for i in range(2):
                if what_to_do[i, 0] and what_to_do[i, 1]:
                    agent.set_torque(i, False)
            self._start_servo = True
            self._obs = env.get_obs()
            if self._curr_light != "green":
                self._light("yellow", 1)

        # servo stopped on a hand
        if dev[0, 1] == -1 or dev[1, 1] == -1:
            self._flag_in = np.array([what_to_do[0, 1], what_to_do[1, 1]])
            if what_to_do[0, 1] == 0 and what_to_do[1, 1] == 0:
                self._light("green", 0, track=False)

        # recording transitions (bookkeeping only; the light is switched inside the servo branch)
        if dev[0, 2] == 1:
            self.log("info", f"开始录制 {self.state.episode}")
        elif dev[0, 2] == -1:
            self._set(episodes_recorded=self.state.episodes_recorded + 1)
            self.log("info", f"已停止录制 {self.state.episode}（{self.state.frames} 帧）")

        if (what_to_do[0, 1] or what_to_do[1, 1]) and self._start_servo:
            action = agent.act({})
            ok, action = servo_action_check(action, self._last_action, self._flag_in,
                                            self.settings.safety.servo_step_limit)
            if not ok:
                raise CollectorError("关节跳变过大（关节详情见终端输出）")

            if self._safe_limit < 1:
                self._safe_limit += 1
            else:
                warnings = check_command(action, self._last_action, self._total_time, self.state.robot_type,
                                         (bool(what_to_do[0, 1]), bool(what_to_do[1, 1])), self.settings.safety)
                if warnings:
                    for w in warnings:
                        self.log("warn", w)
                    raise CollectorError("安全保护：" + "；".join(warnings))

            # button B: recording indicator
            if dev[0, 2] == 1:
                self._light("green", 1)
            elif dev[0, 2] == -1:
                self._light("yellow", 1)

            if what_to_do[0, 2] == 1:
                self._save(action)

            self._obs = env.step(action, self._flag_in)
            self._obs["joint_positions"][6] = action[6]
            self._obs["joint_positions"][13] = action[13]
            self._last_action = action
        else:
            self._start_servo = False
            self._safe_limit = 0
            time.sleep(0.001)  # idle: the old loop yielded ~1 ms in cv2.waitKey

        hz = self.settings.collect.control_hz
        if hz > 0:
            wait_period(1000.0 / hz, tic)
        self._total_time = time.time() - tic
        self._set(unlocked=(bool(what_to_do[0, 0]), bool(what_to_do[1, 0])),
                  servo=(bool(what_to_do[0, 1]), bool(what_to_do[1, 1])),
                  recording=bool(what_to_do[0, 2]),
                  loop_hz=round(1.0 / self._total_time, 1) if self._total_time > 0 else 0.0)

    def _save(self, action: np.ndarray) -> None:
        self._idx += 1
        episode_dir = os.path.join(self.state.save_dir, str(self._episode_stamp))
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(self.settings.collect.jpeg_quality)]
        for cam, sub in zip(self.cameras, IMAGE_DIRS):
            d = os.path.join(episode_dir, sub)
            mk_dir(d)
            cv2.imwrite(os.path.join(d, f"{self._idx}.jpg"), cam.latest(), params)
        obs_dir = os.path.join(episode_dir, "observation") + os.sep
        mk_dir(obs_dir)
        save_frame(obs_dir, self._idx, self._obs, action)
        self._set(frames=self._idx)
