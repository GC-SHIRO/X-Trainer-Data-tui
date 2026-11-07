"""Queries to the Dobot controller's HTTP service (port 22000): firmware version and robot model."""
import ast
import json
from typing import Optional

import requests

MIN_FIRMWARE = 3581   # V3.5.8.1
MAX_FIRMWARE = 4000   # must stay on V3


def get_firmware_version(robot_ip: str, timeout: float = 3.0) -> Optional[int]:
    """Return the firmware version as an int (e.g. 3581 for 3.5.8.1), or None on failure."""
    try:
        response = requests.post(f"http://{robot_ip}:22000/settings/version", timeout=timeout)
    except requests.RequestException as e:
        print(f"[{robot_ip}] firmware query failed: {e}")
        return None
    if response.status_code != 200:
        print(f"[{robot_ip}] failed to retrieve the version page")
        return None
    rt_version = (response.text.split("{")[1].split("\n\t")[3].split(":")[1]
                  .split("\"")[1].split("-")[0].split("."))
    return int("".join(rt_version))


def firmware_error(robot_ip: str, name: str) -> Optional[str]:
    """None when the firmware is acceptable, otherwise a human-readable error."""
    version = get_firmware_version(robot_ip)
    if version is None or not (MIN_FIRMWARE <= version < MAX_FIRMWARE):
        return (f"{name} [{robot_ip}]: firmware must be V3 and >= 3.5.8.1 "
                f"(found: {version}), please check and update")
    return None


def get_robot_type(robot_ip: str, timeout: float = 3.0) -> Optional[str]:
    """Return the controller model name, e.g. "Nova 2" or "Nova 5"."""
    try:
        response = requests.post(f"http://{robot_ip}:22000/properties/controllerType", timeout=timeout)
    except requests.RequestException as e:
        print(f"[{robot_ip}] robot type query failed: {e}")
        return None
    if response.status_code != 200:
        print(f"[{robot_ip}] failed to obtain the type of robot")
        return None
    try:
        return json.loads(response.text)["name"]
    except ValueError:  # original code used eval(); accept Python-literal replies too
        return ast.literal_eval(response.text)["name"]
