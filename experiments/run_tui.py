"""Terminal UI for teleoperation + recording (start experiments/launch_nodes.py first).

    python experiments/run_tui.py                   # real hardware, press c to connect
    python experiments/run_tui.py --fake            # simulated hardware (keys 1-5 press buttons)
    python experiments/run_tui.py --project-name Cup --connect
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

from dataclasses import dataclass
from typing import Optional

import tyro

from dobot_control.settings import load_settings
from dobot_control.tui.app import CollectorApp


@dataclass
class Args:
    project_name: Optional[str] = None    # default: [COLLECT] project_name
    save_data_path: Optional[str] = None  # default: [COLLECT] save_root
    robot_port: Optional[int] = None      # default: [NETWORK] zmq_port
    hostname: Optional[str] = None        # default: [NETWORK] zmq_host
    fake: bool = False                    # use simulated hardware
    connect: bool = False                 # connect immediately instead of waiting for c


def main(args: Args) -> int:
    settings = load_settings()
    if args.project_name is not None:
        settings.collect.project_name = args.project_name
    if args.save_data_path is not None:
        settings.collect.save_root = args.save_data_path
    if args.robot_port is not None:
        settings.network.zmq_port = args.robot_port
    if args.hostname is not None:
        settings.network.zmq_host = args.hostname
    app = CollectorApp(settings, backend="fake" if args.fake else "real", auto_connect=args.connect)
    app.run()
    s = app.session
    return 1 if s is not None and s.state.phase == "error" else 0


if __name__ == "__main__":
    sys.exit(main(tyro.cli(Args)))
