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
            raise ValueError(f"expected {len(default)} comma-separated values, got {len(parts)}")
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

# One-line descriptions shown by the settings UIs, keyed by (section attr, field).
FIELD_HELP = {
    ("collect", "save_root"): "dataset root; empty = <repo parent>/datasets",
    ("collect", "project_name"): "data goes to <save_root>/<project>/collect_data",
    ("collect", "show_img"): "camera preview window (run_control.py only)",
    ("collect", "jpeg_quality"): "JPEG quality 1-100",
    ("collect", "top_crop"): "top camera crop fractions: top, bottom, left, right",
    ("collect", "control_hz"): "0 = as fast as possible (original behaviour)",
    ("network", "left_ip"): "left arm controller IP",
    ("network", "right_ip"): "right arm controller IP",
    ("network", "zmq_host"): "robot server (launch_nodes.py) host",
    ("network", "zmq_port"): "robot server port",
    ("network", "check_firmware"): "refuse controllers with firmware < V3.5.8.1",
    ("network", "robot_type"): "auto = ask the left controller",
    ("safety", "enabled"): "workspace / Z speed / J3 checks (jump check always on)",
    ("safety", "x_range_left"): "left gripper-tip X min, max (mm)",
    ("safety", "x_range_right"): "right gripper-tip X min, max (mm)",
    ("safety", "y_range"): "gripper-tip Y min, max (mm)",
    ("safety", "z_min_left"): "left gripper-tip Z must stay above (mm)",
    ("safety", "z_min_right"): "right gripper-tip Z must stay above (mm)",
    ("safety", "max_z_down_speed"): "max downward TCP speed (m/s)",
    ("safety", "servo_step_limit"): "max joint jump between leader commands (rad)",
    ("safety", "check_j3"): "stop when J3 becomes positive",
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
        bad("collect", "project_name", "must be non-empty and contain no / \\ :")
    if not 1 <= c.jpeg_quality <= 100:
        bad("collect", "jpeg_quality", "must be 1-100")
    if any(not 0 <= v < 0.5 for v in c.top_crop):
        bad("collect", "top_crop", "each value must be in [0, 0.5)")
    if c.control_hz < 0:
        bad("collect", "control_hz", "must be >= 0")
    if not 1 <= n.zmq_port <= 65535:
        bad("network", "zmq_port", "must be 1-65535")
    for f in ("left_ip", "right_ip", "zmq_host"):
        if not getattr(n, f):
            bad("network", f, "must not be empty")
    if n.robot_type not in ROBOT_TYPES:
        bad("network", "robot_type", f"must be one of {', '.join(ROBOT_TYPES)}")
    for f in ("x_range_left", "x_range_right", "y_range"):
        lo, hi = getattr(s, f)
        if lo >= hi:
            bad("safety", f, "min must be < max")
    if s.max_z_down_speed <= 0:
        bad("safety", "max_z_down_speed", "must be > 0")
    if s.servo_step_limit <= 0:
        bad("safety", "servo_step_limit", "must be > 0")
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
