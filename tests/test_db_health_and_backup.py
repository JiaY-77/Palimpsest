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


# ================================================================
# issue #241：备份必须带上 fts.db
# 丢 fts 备份的后果：恢复后 fts.db 不存在 → _ensure_schema 静默建空表 →
# 混合检索恒降级纯语义，服务看起来健康。备份脚本必须拷走 fts 组。
# ================================================================


def _build_fts(db_directory: str, rows: list[tuple[int, str]]) -> None:
    """在主库同目录造一个有内容的 fts.db（schema 与 core.fts_index 一致）。"""
    import sqlite3

    path = os.path.join(db_directory, "fts.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
            "content, node_id UNINDEXED, source_path UNINDEXED, tokenize='trigram')"
        )
        for node_id, content in rows:
            conn.execute(
                "INSERT INTO mem_fts(content, node_id, source_path) VALUES(?, ?, ?)",
                (content, int(node_id), ""),
            )
        conn.commit()
    finally:
        conn.close()


def _run_backup(db_path: str, tmp_path, monkeypatch, capsys):
    """把 Config.DB_PATH 指到指定库后跑一次完整备份，返回 (退出码, 备份目录, 输出)。"""
    from config import Config
    from scripts import backup_db

    monkeypatch.setattr(Config, "DB_PATH", db_path)
    target = str(tmp_path / "backups")
    monkeypatch.setattr(sys, "argv", ["backup_db.py", "--target", target])
    code = backup_db.main()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    dest = os.path.join(target, max(os.listdir(target)))
    return code, dest, output


def test_backup_includes_fts_db(tiny_db, tmp_path, monkeypatch, capsys):
    """主库同目录有 fts.db → 备份带走得，行数与源库一致。"""
    import sqlite3

    _build_fts(os.path.dirname(tiny_db), [(1, "first node content"), (2, "second node content")])
    code, dest, _ = _run_backup(tiny_db, tmp_path, monkeypatch, capsys)
    assert code == 0
    fts_copy = os.path.join(dest, "fts.db")
    assert os.path.isfile(fts_copy)
    conn = sqlite3.connect(fts_copy)
    try:
        rows = conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0]
    finally:
        conn.close()
    assert rows == 2, "备份出的 fts.db 行数必须与源库一致"


def test_backup_skips_missing_fts_db(tiny_db, tmp_path, monkeypatch, capsys):
    """源库没有 fts.db（索引从未建立）→ 跳过并说明，不算失败，退出码仍按主库组。"""
    code, dest, output = _run_backup(tiny_db, tmp_path, monkeypatch, capsys)
    assert code == 0
    assert not os.path.exists(os.path.join(dest, "fts.db"))
    assert "fts.db" in output and "跳过" in output, f"应对缺席给出说明行: {output}"
