"""System helpers: serial port discovery/permissions, directories, busy-wait timing."""
import os
import subprocess
import time

import serial.tools.list_ports as serial_stl

from dobot_control.config import get_passcode


def _sudo_chmod_777(path: str) -> None:
    subprocess.run(f"echo {get_passcode()} | sudo -S chmod 777 {path}", shell=True)


def free_limit_and_set_one(file_name: str) -> None:
    """Make a USB-serial latency_timer writable and set it to 1 ms."""
    _sudo_chmod_777(file_name)
    with open(file_name, "w+") as f:
        f.write("1")


def scan_port() -> list:
    """List /dev/ttyUSB* and /dev/ttyACM* ports and make them accessible."""
    com_list = [p.device for p in serial_stl.comports() if "USB" in p.device or "ACM" in p.device]
    for port in com_list:
        _sudo_chmod_777(port)
    return com_list


def mk_dir(path_dir: str) -> bool:
    if not os.path.isdir(path_dir):
        os.makedirs(path_dir, exist_ok=True)
        return True
    return False


def wait_period(delay_time_ms: float, start_t: float) -> None:
    """Busy-wait until delay_time_ms has elapsed since start_t."""
    deadline = start_t + delay_time_ms / 1000
    while time.time() < deadline:
        pass
