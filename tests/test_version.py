"""
核心.version —— 版本缓存时效性测试（P2）
================================================
背景：get_version() 曾只用 .git/HEAD 的 mtime 当缓存 key；打 tag（写
refs/tags / packed-refs）、提交（写 index）、切分支（改写 HEAD 内容且 mtime
可能不变）都可能不触碰 HEAD 的 mtime → 进程内缓存一直返回旧版本。
本文档用假 .git 目录 + 假 subprocess，验证新的指纹缓存：
  · 指纹不变 → git describe 只执行一次
  · 指纹变化 → 重新取版本
  · 真实 _git_fingerprint 能感知 tag 文件 mtime 的变化
隔离保证：tmp_path 假仓库，monkeypatch _GIT_DIR / subprocess.check_output /
_cache，不依赖真实 git 仓库、不 sleep、不联网。
"""
import os
import subprocess

import pytest

import core.version


@pytest.fixture(autouse=True)
def _reset_version_cache(monkeypatch):
    """每个用例开始前重置进程内版本缓存，避免用例间互相污染。"""
    monkeypatch.setattr(core.version, "_cache", {"version": "", "fingerprint": ()})


def test_fingerprint_unchanged_keeps_cached_version(monkeypatch):
    """指纹不变 → 只取一次版本：连续两次 get_version() 只跑一次 check_output。"""
    monkeypatch.setattr(core.version, "_git_fingerprint", lambda: (1.0, 2.0, 3.0, 4.0))
    calls: list = []

    def fake_check_output(*_args, **_kwargs):
        calls.append(1)
        return "v1.0.0"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert core.version.get_version() == "v1.0.0"
    assert core.version.get_version() == "v1.0.0"
    assert len(calls) == 1


def test_fingerprint_change_refetches_version(monkeypatch):
    """指纹变化 → 重新取版本：第二次 get_version() 拿到新版本并再次跑 describe。"""
    fingerprints = iter([(1.0, 1.0, 1.0, 1.0), (2.0, 2.0, 2.0, 2.0)])
    outputs = iter(["v1.0.0", "v1.1.0"])
    calls: list = []

    def fake_fingerprint():
        return next(fingerprints)

    def fake_check_output(*_args, **_kwargs):
        calls.append(1)
        return next(outputs)

    monkeypatch.setattr(core.version, "_git_fingerprint", fake_fingerprint)
    monkeypatch.setattr(subprocess, "check_output", fake_check_output)
    assert core.version.get_version() == "v1.0.0"
    assert core.version.get_version() == "v1.1.0"
    assert len(calls) == 2


def test_real_fingerprint_detects_tag_file_change(tmp_path, monkeypatch):
    """真实指纹能感知 tag 变化：改 refs/tags/v1.0.0 的 mtime → 指纹不同。

    本用例必须调真实现 _git_fingerprint（不 monkeypatch），只重定向 _GIT_DIR
    到 tmp_path 下造的假 .git 目录。
    """
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "index").write_bytes(b"")
    (git_dir / "packed-refs").write_text("")
    tags = git_dir / "refs" / "tags"
    tags.mkdir(parents=True)
    tag_file = tags / "v1.0.0"
    tag_file.write_text("0123456789abcdef\n")

    monkeypatch.setattr(core.version, "_GIT_DIR", str(git_dir))
    fp_before = core.version._git_fingerprint()

    old = os.path.getmtime(tag_file)
    os.utime(tag_file, (old + 100, old + 100))
    fp_after = core.version._git_fingerprint()

    assert fp_before != fp_after