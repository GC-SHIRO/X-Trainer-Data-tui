"""Follower-arm server: connects both Dobot arms and serves them over ZMQ.

Arm IPs and the ZMQ endpoint come from [NETWORK] in dobot_settings.ini;
command-line flags override them for one run.
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

from dataclasses import dataclass
from typing import Optional

import tyro

from dobot_control.robots.dobot import DobotRobot
from dobot_control.robots.robot import BimanualRobot
from dobot_control.robots.robot_node import ZMQServerRobot
from dobot_control.settings import load_settings


@dataclass
class Args:
    robot_port: Optional[int] = None   # default: [NETWORK] zmq_port
    hostname: Optional[str] = None     # default: [NETWORK] zmq_host
    fake: bool = False                 # serve simulated arms (no hardware; for testing the TUI)


def main(args: Args) -> None:
    net = load_settings().network
    port = args.robot_port if args.robot_port is not None else net.zmq_port
    host = args.hostname if args.hostname is not None else net.zmq_host
    if args.fake:
        from dobot_control.fake import FakeRobot
        robot = FakeRobot()
    else:
        robot_l = DobotRobot(robot_ip=net.left_ip, robot_number=2, gripper_section="GRIPPER_LEFT")
        robot_r = DobotRobot(robot_ip=net.right_ip, robot_number=2, gripper_section="GRIPPER_RIGHT")
        robot = BimanualRobot(robot_l, robot_r)
    server = ZMQServerRobot(robot, port=port, host=host)
    # "Starting robot server on" is the ready marker watched by dobot_control.nodes
    print(f"Starting robot server on {host}:{port}", flush=True)
    try:
        server.serve()
    except KeyboardInterrupt:
        print("Robot server stopped", flush=True)


if __name__ == "__main__":
    main(tyro.cli(Args))
