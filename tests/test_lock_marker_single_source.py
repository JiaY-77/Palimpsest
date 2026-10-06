"""锁措辞单一真相护栏
====================

``core/db_health.py`` 与 ``core/trivium_store.py`` 必须共用同一份「库被别的
进程占用」措辞表。两份不一致时，「库被占用」（重试即可）会被误报成「库损坏、
需备份恢复」，这是运维误导性最强的一类信号。

覆盖：
  - 四种已知措辞（database locked / already opened / 数据库已锁定 /
    incompatible access mode）一律判为 busy=True；
  - 真正的 generation 损坏措辞判为 busy=False；
  - 标记表只有一份（两个模块共用）。
"""

from __future__ import annotations

import pytest
import triviumdb

from core import db_health, trivium_store


def _fake_db_raising(msg: str):
    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError(msg)

    return _Boom


@pytest.mark.parametrize(
    "msg",
    [
        "RuntimeError: database locked",
        "RuntimeError: already opened",
        "RuntimeError: 数据库已锁定",
        "RuntimeError: incompatible access mode",
    ],
)
def test_busy_phrasings_are_detected(tmp_path, monkeypatch, msg):
    """四种「被占用」措辞都必须判为 busy。"""
    db_file = tmp_path / "busy.db"
    db_file.write_bytes(b"")  # 存在性检查要能过

    monkeypatch.setattr(triviumdb, "TriviumDB", _fake_db_raising(msg))
    health = db_health.check_db_health(str(db_file))

    assert health["ok"] is False
    assert health["busy"] is True, health
    assert "占用" in db_health.health_hint(health), health


def test_corruption_is_not_reported_as_busy(tmp_path, monkeypatch):
    """generation 损坏不是 busy —— 必须走恢复指引而不是「重试」。"""
    db_file = tmp_path / "corrupt.db"
    db_file.write_bytes(b"")

    monkeypatch.setattr(
        triviumdb,
        "TriviumDB",
        _fake_db_raising("拒绝不完整的 .tdb/.vec generation：.flush_ok 缺失或不匹配"),
    )
    health = db_health.check_db_health(str(db_file))

    assert health["ok"] is False
    assert health["busy"] is False, health
    assert "恢复" in db_health.health_hint(health), health


def test_marker_table_single_source_of_truth():
    """标记表只有一份；store 侧不得再自维护第二份残缺表。"""
    from core import utils

    markers = set(utils._DB_LOCK_MARKERS)
    assert markers == set(trivium_store._DB_LOCK_MARKERS), "db_health / trivium_store 共用的标记表被复制成两份"
    for phrase in ("database locked", "already opened", "数据库已锁定", "incompatible access mode"):
        assert phrase in markers, f"标记表缺 {phrase!r}: {sorted(markers)}"
    assert trivium_store._is_db_locked_error is utils._is_db_locked_error
