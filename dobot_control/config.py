"""Loading of dobot_settings.ini (serial ports, master-hand calibration, cameras, grippers)."""
import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from dobot_control.agents.dobot_agent import DobotRobotConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
INI_PATH = REPO_ROOT / "scripts" / "dobot_config" / "dobot_settings.ini"


@dataclass
class GripperConfig:
    id_name: int
    pos: List[int]
    port: str


def load_ini() -> configparser.ConfigParser:
    ini_file = configparser.ConfigParser()
    ini_file.read(INI_PATH)
    return ini_file


def save_ini(ini_file: configparser.ConfigParser) -> None:
    with open(INI_PATH, "w", newline="\n") as f:
        ini_file.write(f)


def get_passcode() -> str:
    return load_ini().get("COMPUTER", "passcode")


def load_ini_data_camera() -> Dict[str, str]:
    ini_file = load_ini()
    return {cam: ini_file.get("CAMERA", cam) for cam in ("top", "left", "right")}


def load_ini_data_hands() -> Tuple[configparser.ConfigParser, Dict[str, DobotRobotConfig]]:
    ini_file = load_ini()
    hands_dict = {}
    for hand in ("HAND_LEFT", "HAND_RIGHT"):
        hands_dict[hand] = DobotRobotConfig(
            joint_ids=[int(i) for i in ini_file.get(hand, "joint_ids").split(",")],
            append_id=int(ini_file.get(hand, "append_id")),
            port=ini_file.get(hand, "port"),
            joint_offsets=[float(i) for i in ini_file.get(hand, "joint_offsets").split(",")],
            joint_signs=[int(i) for i in ini_file.get(hand, "joint_signs").split(",")],
            gripper_config=[int(i) for i in ini_file.get(hand, "gripper_config").split(",")],
            start_joints=[float(i) for i in ini_file.get(hand, "start_joints").split(",")],
            baud_rate=int(ini_file.get(hand, "baud_rate")),
            using_sensor=int(ini_file.get(hand, "using_sensor")))
    return ini_file, hands_dict


def load_ini_data_gripper() -> Tuple[configparser.ConfigParser, Dict[str, GripperConfig]]:
    ini_file = load_ini()
    gripper_dict = {}
    for gripper in ("GRIPPER_LEFT", "GRIPPER_RIGHT"):
        gripper_dict[gripper] = GripperConfig(
            id_name=int(ini_file.get(gripper, "id")),
            pos=[int(i) for i in ini_file.get(gripper, "pos").split(",")],
            port=ini_file.get(gripper, "port"))
    return ini_file, gripper_dict
