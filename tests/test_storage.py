# -*- coding: utf-8 -*-
"""
T069 · TriviumDB 0.8.3 特性测试 —— 存储层（P2）
=================================================
覆盖：
  · storage_info()：库格式/版本/访问模式元信息
  · validate_graph()：图完整性校验
  · repair_graph_indexes()：索引修复
  · 撕裂恢复（crash-safety）：子进程硬杀（os._exit 绕过干净 close）后，
    重新打开库仍可恢复且数据完整（WAL 回放）
  · read_only / immutable 打开语义见 test_concurrency.py

隔离保证：%TEMP%/tdb_ftest/ 独立临时库；
子进程脚本带 __main__ 守卫 + 进程计数守卫（压测工具管理铁律）。
"""
import math
import os
import subprocess
import sys
import tempfile

import pytest

import triviumdb  # noqa: E402

_HARDKILL_CHILD = os.path.join(os.path.dirname(__file__), "_tdb_hardkill_child.py")


@pytest.fixture
def _dir():
    d = os.path.join(tempfile.gettempdir(), "tdb_ftest")
    os.makedirs(d, exist_ok=True)
    yield d


@pytest.fixture
def tdb(_dir):
    path = os.path.join(_dir, "storage.db")
    for f in os.listdir(_dir):
        if f.startswith("storage"):
            os.remove(os.path.join(_dir, f))
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)
    for i in range(10):
        db.insert([0.01 * i + 0.1] * 8, {"num": i})
    yield db
    db.close()


# ---------------------------------------------------------------------------
# storage_info / validate / repair
# ---------------------------------------------------------------------------
def test_storage_info_metadata(tdb):
    info = tdb.storage_info()
    assert isinstance(info, dict)
    assert info["package_version"] == "0.8.3"
    assert info["database_format_current"] == 7
    assert info["database_format_minimum"] <= info["database_format_current"]
    assert info["access_mode"] in ("read_write", "read_only", "immutable")
    assert "sidecars" in info and isinstance(info["sidecars"], dict)
    assert any(k.endswith("wal") for k in info["sidecars"]), info["sidecars"]


def test_validate_graph_integrity(tdb):
    res = tdb.validate_graph()
    assert res["valid"] is True
    for k in ("dangling_edges", "duplicate_edges", "degree_index_mismatches",
              "incoming_index_mismatches", "label_index_mismatches"):
        assert k in res
        assert res[k] == 0, res  # 未破坏的图应无任何不一致


def test_validate_graph_detects_unreachable_after_ok(tdb):
    # 正常图上先联动一条边再关闭，仍应 valid
    ids = tdb.all_node_ids()
    tdb.link(ids[0], ids[1], "REL")
    res = tdb.validate_graph()
    assert res["valid"] is True
    assert res["dangling_edges"] == 0


def test_repair_graph_indexes_idempotent(tdb):
    first = tdb.repair_graph_indexes()
    assert isinstance(first, dict)
    assert set(first.keys()) >= {"rebuilt_indexes", "removed_dangling_edges",
                                 "removed_duplicate_edges"}
    second = tdb.repair_graph_indexes()
    assert second["removed_dangling_edges"] == 0
    # 修复后图仍有效
    assert tdb.validate_graph()["valid"] is True


def test_repair_heals_corrupted_index(tdb):
    """模拟索引损坏：人为制造重复/悬挂边后 repair 应能清除或至少不崩溃，
    且 validate 结果受限（不因修复抛异常）。"""
    ids = tdb.all_node_ids()
    tdb.link(ids[0], ids[1], "REL")
    tdb.link(ids[0], ids[1], "REL")  # 重复边
    # 修复动作本身可正常执行（具体去重与否由引擎决定，不崩溃即可）
    report = tdb.repair_graph_indexes()
    assert isinstance(report, dict)
    assert tdb.validate_graph()["valid"] is True


# ---------------------------------------------------------------------------
# crash-safety：子进程硬杀后恢复
# ---------------------------------------------------------------------------
def test_hard_kill_recovery(_dir):
    """子进程插 100 条后用 os._exit 硬杀（绕过干净 close/WAL 刷盘），
    重新打开库应能恢复（WAL 回放）且数据完整。"""
    path = os.path.join(_dir, "crash.db")
    for f in os.listdir(_dir):
        if f.startswith("crash"):
            os.remove(os.path.join(_dir, f))
    r = subprocess.run(
        [sys.executable, _HARDKILL_CHILD, "--child-hardkill", path, "100"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 23  # 硬杀码（见子脚本）
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)
    try:
        assert db.validate_graph()["valid"] is True
        assert db.node_count() == 100  # WAL 回放完整恢复
    finally:
        db.close()


def test_hard_kill_then_clean_close_double(_dir):
    """硬杀恢复后再次正常写、正常关，形成完整闭环（不重复崩溃）。"""
    path = os.path.join(_dir, "crash2.db")
    for f in os.listdir(_dir):
        if f.startswith("crash2"):
            os.remove(os.path.join(_dir, f))
    r = subprocess.run(
        [sys.executable, _HARDKILL_CHILD, "--child-hardkill", path, "100"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 23
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)
    db.insert([0.5] * 8, {"num": "recovered"})
    assert db.node_count() == 101
    db.close()
    # 再次干净打开，一切正常
    db2 = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)
    assert db2.node_count() == 101
    db2.close()
