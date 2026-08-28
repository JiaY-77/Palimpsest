# -*- coding: utf-8 -*-
"""
core.version —— 版本号动态获取
================================
版本不写死在代码里（主人 2026-08-28 指令：小七自己判断版本迭代）：
从 git 读取 —— 有 tag 用 tag（v2.2.0），无 tag 用短 hash + dirty 标记；
读取失败回退 "dev"（非 git 部署/打包场景）。
调用方：main.py 的根端点 /  README 版本描述等。
"""

import os
import subprocess

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_cache: dict = {"version": "", "mtime": 0.0}


def get_version() -> str:
    """从 git 读取版本（tag 优先，无 tag 用短 hash + dirty 标记）；失败回退 dev。"""
    try:
        mtime = os.path.getmtime(os.path.join(_PROJECT_ROOT, ".git", "HEAD"))
        if _cache["version"] and _cache["mtime"] == mtime:
            return _cache["version"]
    except Exception:
        mtime = 0.0
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=_PROJECT_ROOT, stderr=subprocess.DEVNULL,
            text=True, timeout=3,
        ).strip()
        version = out or "dev"
    except Exception:
        version = "dev"
    _cache.update(version=version, mtime=mtime)
    return version


def version_bump(current: str, kind: str = "patch") -> str:
    """语义化版本递增（小七判断版本迭代用）：
    kind=breaking → 主版本 +1，次/补丁清零；feature → 次版本 +1，补丁清零；patch → 补丁 +1。
    输入/输出形如 '2.2.0'（不含 v 前缀，调用方自行加）。
    """
    parts = [int(x) for x in current.strip("v").split(".")]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    if kind == "breaking":
        major += 1
        minor = 0
        patch = 0
    elif kind == "feature":
        minor += 1
        patch = 0
    else:  # patch
        patch += 1
    return f"{major}.{minor}.{patch}"
