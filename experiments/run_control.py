"""Command-line teleoperation + recording (start experiments/launch_nodes.py first).

All logic lives in dobot_control.collector.CollectorSession; this script only wires
settings, prints events and shows the optional three-camera preview window.

    python experiments/run_control.py                    # real hardware
    python experiments/run_control.py --fake             # no hardware (fake hands/arms/cameras)
    python experiments/run_control.py --project-name Cup --no-show-img
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import tyro

from dobot_control.collector import CollectorSession
from dobot_control.settings import load_settings


@dataclass
class Args:
    robot_port: Optional[int] = None      # default: [NETWORK] zmq_port
    hostname: Optional[str] = None        # default: [NETWORK] zmq_host
    show_img: Optional[bool] = None       # default: [COLLECT] show_img
    save_data_path: Optional[str] = None  # default: [COLLECT] save_root
    project_name: Optional[str] = None    # default: [COLLECT] project_name
    fake: bool = False                    # use simulated hardware


def main(args: Args) -> int:
    settings = load_settings()
    if args.robot_port is not None:
        settings.network.zmq_port = args.robot_port
    if args.hostname is not None:
        settings.network.zmq_host = args.hostname
    if args.show_img is not None:
        settings.collect.show_img = args.show_img
    if args.save_data_path is not None:
        settings.collect.save_root = args.save_data_path
    if args.project_name is not None:
        settings.collect.project_name = args.project_name

    session = CollectorSession(settings, backend="fake" if args.fake else "real")
    session.add_listener(lambda ev: print(f"[{ev.level.upper()}] {ev.message}", flush=True))
    print(f"Saving to {session.state.save_dir}")
    try:
        session.connect()
    except Exception:
        session.stop()
        return 1

    print("-------------------------Ok, let's start------------------------")
    session.start()
    canvas = np.zeros((480, 640 * 3, 3), dtype=np.uint8)
    try:
        while session.state.phase == "running":
            if settings.collect.show_img:
                for i, img in enumerate(session.latest_images()):
                    canvas[:, 640 * i:640 * (i + 1)] = img
                cv2.imshow("0", canvas)
                cv2.waitKey(30)
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("Interrupted")
    finally:
        session.stop()
        cv2.destroyAllWindows()
    return 1 if session.state.phase == "error" else 0


if __name__ == "__main__":
    sys.exit(main(tyro.cli(Args)))
