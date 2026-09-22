"""db_health 探针与 backup_db 整组收集的回归测试。

对应 issue #32：写入失败可能污染文件组，需可判定的健康信号 + 可独立打开的
整组备份。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db_health import check_db_health, health_hint
from scripts.backup_db import collect_group, prune_backups

DIM = 16


@pytest.fixture
def tiny_db(tmp_path):
    """造一个小库（含 sidecar），返回主文件路径。"""
    import triviumdb

    path = str(tmp_path / "tiny.db")
    db = triviumdb.TriviumDB(path, dim=DIM)
    db.batch_insert_with_ids(
        [1, 2],
        [[0.1] * DIM, [0.2] * DIM],
        [{"content": "first", "type": "memory"}, {"content": "second", "type": "memory"}],
    )
    db.flush()
    db.close()
    return path


def test_health_ok_on_valid_db(tiny_db):
    health = check_db_health(tiny_db, dim=DIM)
    assert health["ok"] is True
    assert health["node_count"] == 2
    assert health_hint(health) == ""


def test_health_reports_missing_db(tmp_path):
    health = check_db_health(str(tmp_path / "nope" / "missing.db"), dim=DIM)
    assert health["ok"] is False
    assert health["error"]
    # 文件缺失不是「被占用」，应归到需要人工恢复的一类
    assert health["busy"] is False
    assert "恢复" in health_hint(health)
    # 探针必须无副作用：不能顺手建出一个空库
    assert not os.path.exists(str(tmp_path / "nope" / "missing.db"))


def test_health_reports_corrupt_db_as_non_busy(tiny_db, tmp_path):
    """sidecar 缺失（generation 不完整）→ ok=False 且 busy=False（需人工恢复）。"""
    import shutil

    broken = str(tmp_path / "broken.db")
    shutil.copy2(tiny_db, broken)  # 只拷主文件，丢掉 .vec/.flush_ok 等 sidecar
    health = check_db_health(broken, dim=DIM)
    assert health["ok"] is False
    assert health["busy"] is False
    assert "恢复" in health_hint(health)


def test_collect_group_excludes_junk(tiny_db):
    directory = os.path.dirname(tiny_db)
    stem = os.path.basename(tiny_db)
    for junk in (f"{stem}.bak-20260101", f"{stem}.tmp", f"{stem}.pre-0.8.2"):
        with open(os.path.join(directory, junk), "w", encoding="utf-8"):
            pass

    group = {os.path.basename(p) for p in collect_group(tiny_db)}
    assert stem in group
    assert not any("bak-" in name or ".tmp" in name or "pre-" in name for name in group)
    # sidecar 必须一并进组（TriviumDB 要求整组同 generation）
    assert any(name.endswith(".vec") for name in group)
    assert any(name.endswith(".flush_ok") for name in group)


def test_prune_backups_keeps_newest(tmp_path):
    target = str(tmp_path / "backups")
    for stamp in ("20260101_000000", "20260102_000000", "20260103_000000"):
        os.makedirs(os.path.join(target, f"backup_{stamp}"))

    removed = prune_backups(target, keep=2)
    assert removed == ["backup_20260101_000000"]
    remaining = sorted(os.listdir(target))
    assert remaining == ["backup_20260102_000000", "backup_20260103_000000"]
