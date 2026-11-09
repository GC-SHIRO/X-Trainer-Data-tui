"""Collection / network / safety settings stored in dobot_settings.ini.

Hardware calibration sections ([HAND_*], [GRIPPER_*], [CAMERA]) are handled by
dobot_control.config and written by the calibration scripts. The sections here
([COLLECT], [NETWORK], [SAFETY]) hold run-time settings; every key has a default
equal to the value previously hard-coded in run_control.py / launch_nodes.py, so
an ini without these sections behaves exactly like before.
"""
import configparser
from dataclasses import asdict, dataclass, field, fields
from typing import Tuple

from dobot_control.config import REPO_ROOT, load_ini, save_ini


@dataclass
class CollectSettings:
    save_root: str = ""   # empty = <repo parent>/datasets (see resolved_save_root)
    project_name: str = "Cup"
    show_img: bool = True
    jpeg_quality: int = 95
    # top camera crop as fractions of height/width: top, bottom, left, right
    top_crop: Tuple[float, float, float, float] = (0.2, 0.2, 0.2, 0.2)
    # 0 = run the control loop as fast as possible (original behaviour)
    control_hz: float = 0.0

    def resolved_save_root(self) -> str:
        return self.save_root or str(REPO_ROOT.parent / "datasets")


@dataclass
class NetworkSettings:
    left_ip: str = "192.168.5.1"
    right_ip: str = "192.168.5.2"
    zmq_host: str = "127.0.0.1"
    zmq_port: int = 6001
    check_firmware: bool = True
    # "auto" queries the controller; otherwise "Nova 2" or "Nova 5"
    robot_type: str = "auto"
    # TUI: start experiments/launch_nodes.py automatically on connect when none is running locally
    auto_start_node: bool = True


@dataclass
class SafetySettings:
    enabled: bool = True
    # gripper-tip workspace in mm (robot base frame)
    x_range_left: Tuple[float, float] = (-850.0, 700.0)
    x_range_right: Tuple[float, float] = (-650.0, 850.0)
    y_range: Tuple[float, float] = (-1900.0, 1100.0)
    z_min_left: float = 0.0
    z_min_right: float = 5.0
    # maximum downward TCP speed, m/s
    max_z_down_speed: float = 1.0
    # max per-step joint jump between leader commands, rad
    servo_step_limit: float = 0.9
    check_j3: bool = True


@dataclass
class Settings:
    collect: CollectSettings = field(default_factory=CollectSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    safety: SafetySettings = field(default_factory=SafetySettings)


_SECTIONS = {"collect": "COLLECT", "network": "NETWORK", "safety": "SAFETY"}


def _parse(raw: str, default):
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, tuple):
        parts = [x for x in raw.split(",")]
        if len(parts) != len(default):
            raise ValueError(f"需要 {len(default)} 个逗号分隔的值，实际 {len(parts)} 个")
        return tuple(type(d)(x) for d, x in zip(default, parts))
    return raw.strip()


def _format(value) -> str:
    if isinstance(value, tuple):
        return ", ".join(str(v) for v in value)
    return str(value)


def _load_section(ini: configparser.ConfigParser, section: str, cls):
    obj = cls()
    if not ini.has_section(section):
        return obj
    for f in fields(cls):
        if ini.has_option(section, f.name):
            default = getattr(obj, f.name)
            try:
                setattr(obj, f.name, _parse(ini.get(section, f.name), default))
            except ValueError as e:
                raise ValueError(f"[{section}] {f.name}: {e}") from e
    return obj


def load_settings() -> Settings:
    ini = load_ini()
    return Settings(
        collect=_load_section(ini, "COLLECT", CollectSettings),
        network=_load_section(ini, "NETWORK", NetworkSettings),
        safety=_load_section(ini, "SAFETY", SafetySettings),
    )


ROBOT_TYPES = ("auto", "Nova 2", "Nova 5")

# Labels and one-line descriptions shown by the settings UIs, keyed by (section attr, field).
FIELD_LABEL = {
    ("collect", "save_root"): "数据根目录",
    ("collect", "project_name"): "项目名",
    ("collect", "show_img"): "相机窗口",
    ("collect", "jpeg_quality"): "JPEG 质量",
    ("collect", "top_crop"): "顶部相机裁剪",
    ("collect", "control_hz"): "控制频率",
    ("network", "left_ip"): "左臂 IP",
    ("network", "right_ip"): "右臂 IP",
    ("network", "zmq_host"): "从臂服务地址",
    ("network", "zmq_port"): "从臂服务端口",
    ("network", "check_firmware"): "检查固件",
    ("network", "robot_type"): "机型",
    ("network", "auto_start_node"): "自动启动从臂服务",
    ("safety", "enabled"): "启用安全检查",
    ("safety", "x_range_left"): "左臂 X 范围",
    ("safety", "x_range_right"): "右臂 X 范围",
    ("safety", "y_range"): "Y 范围",
    ("safety", "z_min_left"): "左臂 Z 下限",
    ("safety", "z_min_right"): "右臂 Z 下限",
    ("safety", "max_z_down_speed"): "最大下降速度",
    ("safety", "servo_step_limit"): "单步关节跳变上限",
    ("safety", "check_j3"): "检查 J3",
}
FIELD_HELP = {
    ("collect", "save_root"): "留空 = <仓库上一级>/datasets",
    ("collect", "project_name"): "数据存到 <根目录>/<项目名>/collect_data",
    ("collect", "show_img"): "连接后自动弹出相机窗口（TUI 中按 v 切换）",
    ("collect", "jpeg_quality"): "1-100",
    ("collect", "top_crop"): "按比例裁掉：上, 下, 左, 右",
    ("collect", "control_hz"): "0 = 不限速（与原版一致）",
    ("network", "left_ip"): "左臂控制器 IP",
    ("network", "right_ip"): "右臂控制器 IP",
    ("network", "zmq_host"): "launch_nodes.py 所在主机",
    ("network", "zmq_port"): "launch_nodes.py 端口",
    ("network", "check_firmware"): "固件低于 V3.5.8.1 时拒绝连接",
    ("network", "robot_type"): "auto = 向左臂控制器查询",
    ("network", "auto_start_node"): "TUI 连接时本机没有 launch_nodes 就自动启动，退出 TUI 时一并关闭",
    ("safety", "enabled"): "工作空间 / 下降速度 / J3 检查（关节跳变检查始终开启）",
    ("safety", "x_range_left"): "左夹爪尖 X 最小, 最大（mm）",
    ("safety", "x_range_right"): "右夹爪尖 X 最小, 最大（mm）",
    ("safety", "y_range"): "夹爪尖 Y 最小, 最大（mm）",
    ("safety", "z_min_left"): "左夹爪尖 Z 须高于（mm）",
    ("safety", "z_min_right"): "右夹爪尖 Z 须高于（mm）",
    ("safety", "max_z_down_speed"): "末端最大下降速度（m/s）",
    ("safety", "servo_step_limit"): "相邻两次主手指令的最大关节跳变（rad）",
    ("safety", "check_j3"): "J3 变号时停止",
}


def parse_field(section_attr: str, name: str, raw: str):
    """Parse a text value for one field the same way the ini loader does."""
    default = getattr(getattr(Settings(), section_attr), name)
    return _parse(raw, default)


def format_field(value) -> str:
    return _format(value)


def validate(settings: Settings) -> list:
    """Return a list of (section_attr, field, message) for out-of-range values."""
    errs = []
    c, n, s = settings.collect, settings.network, settings.safety

    def bad(sec, f, msg):
        errs.append((sec, f, msg))

    if not c.project_name or any(ch in c.project_name for ch in "/\\:"):
        bad("collect", "project_name", "不能为空，且不能包含 / \\ :")
    if not 1 <= c.jpeg_quality <= 100:
        bad("collect", "jpeg_quality", "须在 1-100 之间")
    if any(not 0 <= v < 0.5 for v in c.top_crop):
        bad("collect", "top_crop", "每个值须在 [0, 0.5) 之间")
    if c.control_hz < 0:
        bad("collect", "control_hz", "须 >= 0")
    if not 1 <= n.zmq_port <= 65535:
        bad("network", "zmq_port", "须在 1-65535 之间")
    for f in ("left_ip", "right_ip", "zmq_host"):
        if not getattr(n, f):
            bad("network", f, "不能为空")
    if n.robot_type not in ROBOT_TYPES:
        bad("network", "robot_type", f"须为 {', '.join(ROBOT_TYPES)} 之一")
    for f in ("x_range_left", "x_range_right", "y_range"):
        lo, hi = getattr(s, f)
        if lo >= hi:
            bad("safety", f, "最小值须小于最大值")
    if s.max_z_down_speed <= 0:
        bad("safety", "max_z_down_speed", "须 > 0")
    if s.servo_step_limit <= 0:
        bad("safety", "servo_step_limit", "须 > 0")
    return errs


def save_settings(settings: Settings) -> None:
    """Write the three run-time sections back, leaving calibration sections untouched."""
    ini = load_ini()
    for attr, section in _SECTIONS.items():
        if not ini.has_section(section):
            ini.add_section(section)
        for key, value in asdict(getattr(settings, attr)).items():
            ini.set(section, key, _format(value))
    save_ini(ini)
