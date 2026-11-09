"""System helpers: serial port discovery/permissions, directories, busy-wait timing."""
import os
import subprocess
import time

import serial.tools.list_ports as serial_stl

UDEV_HINT = ("install the udev rule once: sudo cp scripts/99-xtrainer-serial.rules /etc/udev/rules.d/ "
             "&& sudo udevadm control --reload-rules && sudo udevadm trigger")


def _sudo_n(args, data: bytes = None) -> bool:
    """Run a command with sudo without asking for a password (works with NOPASSWD only)."""
    try:
        r = subprocess.run(["sudo", "-n"] + args, input=data, capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_port_access(path: str) -> bool:
    """Make sure a serial port is readable/writable; no password is ever asked."""
    if os.access(path, os.R_OK | os.W_OK):
        return True
    if _sudo_n(["chmod", "666", path]):
        return True
    print(f"[WARN] no permission for {path}; {UDEV_HINT}")
    return False


def free_limit_and_set_one(file_name: str) -> None:
    """Set a USB-serial latency_timer to 1 ms (best effort: warn instead of failing)."""
    try:
        with open(file_name, "w") as f:
            f.write("1")
        return
    except FileNotFoundError:
        print(f"[WARN] {file_name} not found; USB latency left at the default")
        return
    except PermissionError:
        pass
    if not _sudo_n(["tee", file_name], data=b"1"):
        print(f"[WARN] cannot set {file_name} to 1 ms; {UDEV_HINT}")


def scan_port() -> list:
    """List /dev/ttyUSB* and /dev/ttyACM* ports and make sure they are accessible."""
    com_list = [p.device for p in serial_stl.comports() if "USB" in p.device or "ACM" in p.device]
    for port in com_list:
        ensure_port_access(port)
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
