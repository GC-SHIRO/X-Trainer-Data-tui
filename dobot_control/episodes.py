"""Browse, check and discard recorded episodes.

Layout (written by CollectorSession):
    <save_root>/<project>/collect_data/<stamp>/{topImg,leftImg,rightImg}/<idx>.jpg
                                              /observation/<idx>.pkl
Discarded episodes are moved to <save_root>/<project>/trash/<stamp> (never deleted here),
so they can be restored by moving the directory back.
"""
import datetime
import os
import pickle
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

SUBDIRS = ("topImg", "leftImg", "rightImg", "observation")
EXT = {"topImg": ".jpg", "leftImg": ".jpg", "rightImg": ".jpg", "observation": ".pkl"}
PKL_KEYS = {"joint_positions", "joint_velocities", "ee_pos_quat", "gripper_position", "control"}


@dataclass
class EpisodeInfo:
    name: str
    path: str
    counts: Dict[str, int] = field(default_factory=dict)
    first_idx: Optional[int] = None
    last_idx: Optional[int] = None
    size_bytes: int = 0
    issues: List[str] = field(default_factory=list)
    verified: Optional[bool] = None       # None = not deep-checked yet

    @property
    def frames(self) -> int:
        return self.counts.get("observation", 0)

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def start_time(self) -> Optional[datetime.datetime]:
        try:
            return datetime.datetime.strptime(self.name, "%Y%m%d%H%M%S")
        except ValueError:
            return None


def _indices(d: str, ext: str) -> List[int]:
    out = []
    try:
        entries = list(os.scandir(d))
    except FileNotFoundError:
        return out
    for e in entries:
        stem, e_ext = os.path.splitext(e.name)
        if e_ext == ext and stem.isdigit():
            out.append(int(stem))
    return sorted(out)


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def scan_episode(path: str) -> EpisodeInfo:
    """Fast structural check: every stream present, same frame indices, no gaps."""
    info = EpisodeInfo(name=os.path.basename(path.rstrip("/\\")), path=path)
    idx_sets = {}
    for sub in SUBDIRS:
        d = os.path.join(path, sub)
        if not os.path.isdir(d):
            info.issues.append(f"missing {sub}/")
            info.counts[sub] = 0
            continue
        idx = _indices(d, EXT[sub])
        idx_sets[sub] = idx
        info.counts[sub] = len(idx)
    obs = idx_sets.get("observation", [])
    if obs:
        info.first_idx, info.last_idx = obs[0], obs[-1]
        if obs[-1] - obs[0] + 1 != len(obs):
            info.issues.append(f"frame gaps ({len(obs)} frames in {obs[0]}..{obs[-1]})")
    elif "observation" in idx_sets:
        info.issues.append("empty")
    ref = set(obs)
    for sub, idx in idx_sets.items():
        if sub != "observation" and set(idx) != ref:
            info.issues.append(f"{sub}: {len(idx)} files vs {len(obs)} observations")
    info.size_bytes = _dir_size(path)
    return info


def verify_episode(info: EpisodeInfo, max_frames: Optional[int] = None) -> EpisodeInfo:
    """Deep check: every pkl loads with the expected keys/shapes and every jpg is non-empty."""
    issues = []
    obs_dir = os.path.join(info.path, "observation")
    idx = _indices(obs_dir, ".pkl")
    if max_frames is not None:
        idx = idx[:max_frames]
    for i in idx:
        p = os.path.join(obs_dir, f"{i}.pkl")
        try:
            with open(p, "rb") as f:
                obs = pickle.load(f)
            missing = PKL_KEYS - set(obs)
            if missing:
                issues.append(f"{i}.pkl missing {sorted(missing)}")
            elif np.asarray(obs["joint_positions"]).shape != (14,) or np.asarray(obs["control"]).shape != (14,):
                issues.append(f"{i}.pkl wrong shape")
        except Exception as e:
            issues.append(f"{i}.pkl unreadable ({type(e).__name__})")
        for sub in SUBDIRS[:3]:
            jp = os.path.join(info.path, sub, f"{i}.jpg")
            if os.path.exists(jp) and os.path.getsize(jp) == 0:
                issues.append(f"{sub}/{i}.jpg empty")
        if len(issues) > 20:
            issues.append("... (stopped after 20 problems)")
            break
    info.issues = [x for x in info.issues if not x.startswith("deep: ")] + [f"deep: {x}" for x in issues]
    info.verified = not issues
    return info


def list_episodes(save_dir: str) -> List[EpisodeInfo]:
    """All episodes in a collect_data directory, newest first."""
    if not os.path.isdir(save_dir):
        return []
    eps = [scan_episode(e.path) for e in os.scandir(save_dir) if e.is_dir()]
    return sorted(eps, key=lambda e: e.name, reverse=True)


def list_projects(save_root: str) -> List[str]:
    if not os.path.isdir(save_root):
        return []
    return sorted(e.name for e in os.scandir(save_root)
                  if e.is_dir() and os.path.isdir(os.path.join(e.path, "collect_data")))


def trash_dir_for(save_dir: str) -> str:
    """<project>/collect_data -> <project>/trash"""
    return os.path.join(os.path.dirname(os.path.normpath(save_dir)), "trash")


def trash_episode(info: EpisodeInfo, trash_dir: str) -> str:
    """Move an episode directory into trash_dir; returns the new path."""
    os.makedirs(trash_dir, exist_ok=True)
    dest = os.path.join(trash_dir, info.name)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(trash_dir, f"{info.name}_{n}")
        n += 1
    shutil.move(info.path, dest)
    return dest


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
