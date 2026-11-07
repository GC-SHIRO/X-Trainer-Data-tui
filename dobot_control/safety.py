"""Workspace / speed / joint checks applied to every teleop command (moved from run_control.py)."""
from typing import Dict, List, Tuple

import numpy as np

from dobot_control.settings import SafetySettings


def dh_transformation_matrix(theta, d, a, alpha):
    cos_theta, sin_theta = np.cos(theta), np.sin(theta)
    cos_alpha, sin_alpha = np.cos(alpha), np.sin(alpha)
    return np.array([
        [cos_theta, -sin_theta * cos_alpha, sin_theta * sin_alpha, a * cos_theta],
        [sin_theta, cos_theta * cos_alpha, -cos_theta * sin_alpha, a * sin_theta],
        [0, sin_alpha, cos_alpha, d],
        [0, 0, 0, 1],
    ])


def claw_width(coef):
    claw_servo = 2.3818 - coef * 1.5401
    cos_claw_servo = np.cos(claw_servo)
    return 0.03 * cos_claw_servo + 0.5 * np.sqrt(0.0036 * cos_claw_servo ** 2 + 0.0028)


_DH = {
    "Nova 2": [(0.2234, 0, np.pi / 2), (0, -0.280, 0), (0, -0.225, 0),
               (0.1175, 0, np.pi / 2), (0.120, 0, -np.pi / 2), (0.088, 0, 0)],
    "Nova 5": [(0.240, 0, np.pi / 2), (0, -0.400, 0), (0, -0.330, 0),
               (0.135, 0, np.pi / 2), (0.120, 0, -np.pi / 2), (0.088, 0, 0)],
}
_THETA_OFFSET = [0, -np.pi / 2, 0, -np.pi / 2, 0, 0]


def forward_kinematics(q0, q1, q2, q3, q4, q5, y, r_type):
    """Gripper-finger tip position (m) for one arm."""
    if r_type not in _DH:
        raise ValueError(f"Unsupported robot type for FK: {r_type!r}")
    t = np.eye(4)
    for q, off, (d, a, alpha) in zip((q0, q1, q2, q3, q4, q5), _THETA_OFFSET, _DH[r_type]):
        t = t @ dh_transformation_matrix(q + off, d, a, alpha)
    t_tool = np.eye(4)
    t_tool[:3, 3] = np.array([0, y, 0.2])
    return (t @ t_tool)[:3, 3]


def calculate_vel_pos(action, last_action, total_time, r_type):
    claw_left = claw_width(action[6])
    claw_right = claw_width(action[13])
    positions, vel = {}, {}
    for side in ("left", "right"):
        for paw in ("left", "right"):
            claw = (claw_left if side == "left" else claw_right) * (1 if paw == "left" else -1)
            joints = slice(0, 6) if side == "left" else slice(7, 13)
            current_fk = forward_kinematics(*action[joints], claw, r_type)
            last_fk = forward_kinematics(*last_action[joints], claw, r_type)
            positions[f"{side}_{paw}"] = current_fk
            vel[f"{side}_{paw}"] = (current_fk - last_fk) / total_time
    return positions, vel


def _in_zone(p, x_range, y_range, z_min):
    return x_range[0] <= p[0] <= x_range[1] and y_range[0] <= p[1] <= y_range[1] and p[2] > z_min


def check_pose_protection(positions: Dict, vel: Dict, servo_on: Tuple[bool, bool],
                          cfg: SafetySettings) -> List[str]:
    """Return a list of warnings; empty means safe."""
    warnings = []
    positions_mm = {k: v * 1000 for k, v in positions.items()}
    for i, side in enumerate(("left", "right")):
        if not servo_on[i]:
            continue
        vz = [vel[f"{side}_left"][2], vel[f"{side}_right"][2]]
        if min(vz) < -cfg.max_z_down_speed:
            warnings.append(f"The {side} robot TCP is moving down too fast ({min(vz):.2f} m/s)")
        x_range = cfg.x_range_left if side == "left" else cfg.x_range_right
        z_min = cfg.z_min_left if side == "left" else cfg.z_min_right
        for paw in ("left", "right"):
            if not _in_zone(positions_mm[f"{side}_{paw}"], x_range, cfg.y_range, z_min):
                warnings.append(f"The {side} arm is out of the safe zone "
                                f"(tip {np.round(positions_mm[f'{side}_{paw}'], 1)} mm)")
                break
    return warnings


def check_joint_safety(action) -> List[str]:
    warnings = []
    if not action[2] < 0:
        warnings.append(f"Left J3 out of the safe range ({action[2]:.3f} rad, must be < 0)")
    if not action[9] > 0:
        warnings.append(f"Right J3 out of the safe range ({action[9]:.3f} rad, must be > 0)")
    return warnings


def check_command(action, last_action, dt, robot_type, servo_on, cfg: SafetySettings) -> List[str]:
    if not cfg.enabled:
        return []
    positions, vel = calculate_vel_pos(action, last_action, dt, robot_type)
    warnings = check_pose_protection(positions, vel, servo_on, cfg)
    if cfg.check_j3:
        warnings += check_joint_safety(action)
    return warnings
