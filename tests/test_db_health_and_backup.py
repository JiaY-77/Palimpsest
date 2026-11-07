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
    # 0.9.0 起 TriviumDB 构造时严格校验维度：副本校验用 Config 的维度打开，
    # 夹具库是 dim=DIM 的小库，必须让配置与夹具一致，否则报「维度不匹配」。
    monkeypatch.setattr(Config, "OLLAMA_EMBEDDING_DIM", DIM, raising=False)
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


# ================================================================
# issue #301：备份必须整组覆盖数据目录下的独立 *.db 侧车库
# #295 引入 node_chunks.db（chunk 级向量索引的独立 SQLite）后，只认主库
# 后缀清单的备份把它结构性漏掉——恢复后 chunk 表静默为空、长文档语义检索
# 悄悄退化（无报错，最难归因）。收集改按目录 glob：数据目录每个 *.db 都
# 自动进组，未来新增侧车库无需改代码。
# ================================================================


def _build_chunk_db(db_directory: str, rows: list[tuple[int, int, str]]) -> None:
    """造一个与 core.chunk_index 同 schema 的 node_chunks.db（字段同 _SCHEMA）。"""
    import sqlite3

    path = os.path.join(db_directory, "node_chunks.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS node_chunks ("
            "node_id INTEGER NOT NULL, chunk_idx INTEGER NOT NULL, chunk_text TEXT NOT NULL, "
            "vector BLOB NOT NULL, dim INTEGER NOT NULL, model TEXT NOT NULL, updated_at REAL NOT NULL, "
            "PRIMARY KEY (node_id, chunk_idx))"
        )
        for node_id, chunk_idx, chunk_text in rows:
            conn.execute(
                "INSERT INTO node_chunks(node_id, chunk_idx, chunk_text, vector, dim, model, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (int(node_id), int(chunk_idx), chunk_text, b"\x00\x00", 4, "fake-model", 0.0),
            )
        conn.commit()
    finally:
        conn.close()


def _db_group_names(directory: str) -> set[str]:
    """目录下 *.db 及其 -wal / -shm 侧车的名字集合（glob 期望集合的共用实现）。"""
    names = set()
    for name in os.listdir(directory):
        if not os.path.isfile(os.path.join(directory, name)):
            continue
        if name.endswith(".db") or (name.endswith(("-wal", "-shm")) and ".db" in name):
            names.add(name)
    return names


def test_backup_covers_every_data_dir_db(tiny_db, tmp_path, monkeypatch, capsys):
    """数据目录下每个 *.db（含从没听过名字的假侧车库）都出现在备份产物里。

    期望集合用目录生成，不硬编码文件名清单；造两个脚本没听过的 db 即反向
    用例：新增 *.db 不改代码就被备份覆盖。
    """
    directory = os.path.dirname(tiny_db)
    for name in ("alpha_mood.db", "beta_stats.db"):
        with open(os.path.join(directory, name), "wb") as fh:
            fh.write(b"fake side db")
    _build_chunk_db(directory, [(1, 0, "a"), (2, 0, "b"), (2, 1, "c")])
    # chunk 库的 -wal 侧车：存在才拷，备份必须跟着带走
    with open(os.path.join(directory, "node_chunks.db-wal"), "wb") as fh:
        fh.write(b"")

    code, dest, output = _run_backup(tiny_db, tmp_path, monkeypatch, capsys)
    assert code == 0

    expected = _db_group_names(directory)
    produced = _db_group_names(dest)
    assert produced == expected, f"备份产物必须与数据目录 *.db 组完全一致: 产物={produced} 期望={expected}"
    # 侧车库组有日志说明
    assert "node_chunks.db" in output


def test_backup_reports_chunk_rows_and_nodes(tiny_db, tmp_path, monkeypatch, capsys):
    """node_chunks.db 存在 → 回读报告 chunk 行数与被覆盖 node 数。"""
    _build_chunk_db(os.path.dirname(tiny_db), [(1, 0, "a"), (2, 0, "b"), (2, 1, "c")])
    code, _dest, output = _run_backup(tiny_db, tmp_path, monkeypatch, capsys)
    assert code == 0
    # 3 行 chunk / 覆盖 2 个 node —— 回读的是副本，不是源库数字
    assert "3 行 / 2 个 node" in output, f"回读报告缺失或数字不对: {output}"


def test_backup_skips_missing_chunk_db(tiny_db, tmp_path, monkeypatch, capsys):
    """源库没有 node_chunks.db（老库）→ 跳过并说明，不算失败。"""
    code, dest, output = _run_backup(tiny_db, tmp_path, monkeypatch, capsys)
    assert code == 0
    assert not os.path.exists(os.path.join(dest, "node_chunks.db"))
    assert "chunk 索引" in output and "跳过" in output, f"应对缺席给出说明行: {output}"
