"""
core.version —— 版本号动态获取
================================
版本不写死在代码里（自动化判断版本迭代）：
从 git 读取 —— 有 tag 用 tag（v1.0.0），无 tag 用短 hash + dirty 标记；
读取失败回退 "dev"（非 git 部署/打包场景）。
调用方：main.py 的根端点 /  README 版本描述等。
"""

import os
import subprocess

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# .git 目录（供测试 monkeypatch 指向假仓库）；进程内缓存据此追踪 git 元数据
_GIT_DIR = os.path.join(_PROJECT_ROOT, ".git")

_cache: dict = {"version": "", "fingerprint": ()}


def _mtime(path: str) -> float:
    """文件 mtime；不存在/不可读记 0.0，不抛异常。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _git_fingerprint() -> tuple:
    """git describe 结果是否可能变化的指纹（只 stat，不跑子进程）：
    返回固定 4 元组 —— (HEAD mtime, index mtime, packed-refs mtime,
    refs 目录下所有文件的最大 mtime)。仅用 mtime 判断是否重新取版本，
    不含具体版本内容，保证贴近真实 .git 元数据。
    """
    refs_dir = os.path.join(_GIT_DIR, "refs")
    refs_mtime = 0.0
    if os.path.isdir(refs_dir):
        for dirpath, _dirnames, filenames in os.walk(refs_dir):
            for filename in filenames:
                refs_mtime = max(refs_mtime, _mtime(os.path.join(dirpath, filename)))
    return (
        _mtime(os.path.join(_GIT_DIR, "HEAD")),
        _mtime(os.path.join(_GIT_DIR, "index")),
        _mtime(os.path.join(_GIT_DIR, "packed-refs")),
        refs_mtime,
    )


def get_version() -> str:
    """从 git 读取版本（tag 优先，无 tag 用短 hash + dirty 标记）；失败回退 dev。"""
    try:
        fingerprint = _git_fingerprint()
        if _cache["version"] and _cache["fingerprint"] == fingerprint:
            return _cache["version"]
    except Exception:  # noqa: BLE001 —— 读取 git 元数据失败回退空指纹继续走缓存判断
        fingerprint = ()
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=_PROJECT_ROOT, stderr=subprocess.DEVNULL,
            text=True, timeout=3,
        ).strip()
        version = out or "dev"
    except Exception:  # noqa: BLE001 —— git describe 失败回退 dev 非 git 部署可用
        version = "dev"
    _cache.update(version=version, fingerprint=fingerprint)
    return version


def version_bump(current: str, kind: str = "patch") -> str:
    """语义化版本递增（供版本迭代判断）：
    kind=breaking → 主版本 +1，次/补丁清零；feature → 次版本 +1，补丁清零；patch → 补丁 +1。
    输入/输出形如 '1.0.0'（不含 v 前缀，调用方自行加）。
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
