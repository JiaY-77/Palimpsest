# -*- coding: utf-8 -*-
"""
T069 · TriviumDB 0.8.3 特性测试 —— 并发与安全（P2）
===================================================
覆盖：
  · 访问模式：read_only 只读语义（写拒绝）、immutable fail-closed
  · 锁语义：同路径共享读锁（多 reader）、写锁互斥（writer+reader、
    writer+writer 均拒绝）
  · 跨进程锁：子进程 writer 与父进程 writer/reader 互斥（压测铁律：
    __main__ 守卫 + 进程计数守卫）

隔离保证：%TEMP%/tdb_ftest/ 独立临时库；
跨进程通过 subprocess 启动子脚本（见 _tdb_lock_child.py）。
"""
import os
import subprocess
import sys
import tempfile

import pytest

import triviumdb  # noqa: E402

_LOCK_CHILD = os.path.join(os.path.dirname(__file__), "_tdb_lock_child.py")


@pytest.fixture
def _dir():
    d = os.path.join(tempfile.gettempdir(), "tdb_ftest")
    os.makedirs(d, exist_ok=True)
    yield d


@pytest.fixture
def base(_dir, request):
    """构建一个含 1 节点的既有库，返回其路径。"""
    name = request.node.name + ".db"
    path = os.path.join(_dir, name)
    for f in os.listdir(_dir):
        if f.startswith(request.node.name):
            os.remove(os.path.join(_dir, f))
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)
    db.insert([0.1] * 8, {"num": 1})
    db.close()
    return path


# ---------------------------------------------------------------------------
# access_mode 语义
# ---------------------------------------------------------------------------
def test_read_only_denies_mutation(base):
    ro = triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False,
                             access_mode="read_only")
    try:
        assert ro.node_count() == 1  # 可读
        for op in (
            lambda: ro.insert([0.2] * 8, {"num": 2}),
            lambda: ro.link(ro.all_node_ids()[0], ro.all_node_ids()[0], "X"),
        ):
            with pytest.raises(RuntimeError) as ei:
                op()
            assert "只读" in str(ei.value)
    finally:
        ro.close()


def test_immutable_without_manifest_fails_closed(base):
    """immutable 模式必须配合完整 generation manifest；
    缺失时 fail-closed（抛 ImmutableArtifactError），而非静默回退到可写。"""
    with pytest.raises(triviumdb.ImmutableArtifactError):
        triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False,
                            access_mode="immutable")


# ---------------------------------------------------------------------------
# 共享读锁 / 写锁互斥（同进程）
# ---------------------------------------------------------------------------
def test_shared_read_lock_multiple_readers(base):
    a = triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False,
                            access_mode="read_only")
    try:
        b = triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False,
                                access_mode="read_only")
        b.close()
    finally:
        a.close()
    # 两个 reader 未触发任何异常 → 共享读锁


def test_writer_excludes_reader_same_process(base):
    w = triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False)
    try:
        with pytest.raises(RuntimeError) as ei:
            triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False,
                                access_mode="read_only")
        assert "锁" in str(ei.value)
    finally:
        w.close()


def test_second_writer_rejected_same_process(base):
    w = triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False)
    try:
        with pytest.raises(RuntimeError) as ei:
            triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False)
        assert "锁" in str(ei.value)
    finally:
        w.close()


# ---------------------------------------------------------------------------
# 跨进程锁（subprocess）
# ---------------------------------------------------------------------------
def test_cross_process_writer_lock(base):
    """父进程持写锁时，子进程 writer 应被拒绝打开（锁跨进程生效）。"""
    w = triviumdb.TriviumDB(base, dim=8, auto_build_quiver=False)
    try:
        r = subprocess.run([sys.executable, _LOCK_CHILD, "--writer", base],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode != 0
        assert "OPEN" not in r.stdout
    finally:
        w.close()
    # 释放写锁后，子进程应可正常写入
    r2 = subprocess.run([sys.executable, _LOCK_CHILD, "--writer", base],
                        capture_output=True, text=True, timeout=60)
    assert r2.returncode == 0
    assert "OPEN" in r2.stdout and "WRITE_OK" in r2.stdout


def test_cross_process_reader_shared(base):
    """多个进程可同时以 read_only 打开（读锁可共享）。"""
    r1 = subprocess.Popen([sys.executable, _LOCK_CHILD, "--reader", base],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        r2 = subprocess.Popen([sys.executable, _LOCK_CHILD, "--reader", base],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out2, _ = r2.communicate(timeout=60)
        assert "OPEN_READ" in out2, out2
        r2.wait(timeout=60)
    finally:
        r1.terminate()
        try:
            r1.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            r1.kill()
