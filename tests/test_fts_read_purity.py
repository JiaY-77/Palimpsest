"""FTS 只读路径不得建表 / 拿写锁
=================================

``search_fts`` 是只读语义：不得创建索引文件、不得执行 DDL、不得拿写锁。
建表只在写入口（index_node / remove_node / rebuild）做一次。

隔离：把 ``core.fts_index._db_path`` 指向 tmp_path，不碰会话共享索引。
"""

from __future__ import annotations

import sqlite3

from core import fts_index


def test_search_on_missing_index_returns_empty_without_creating(tmp_path,
                                                                monkeypatch):
    """索引文件不存在 → 返回空且不创建文件（只读查询无副作用）。"""
    target = tmp_path / "fts.db"
    monkeypatch.setattr(fts_index, "_db_path", lambda: str(target))

    assert fts_index.search_fts("中文查询") == []
    assert not target.exists(), "只读检索不得创建 FTS 索引文件"


def test_search_does_not_create_schema(tmp_path, monkeypatch):
    """只有空库（无 mem_fts 表）时，只读检索不得建表。"""
    target = tmp_path / "fts.db"
    sqlite3.connect(str(target)).close()  # 造一个空 SQLite 文件

    monkeypatch.setattr(fts_index, "_db_path", lambda: str(target))
    assert fts_index.search_fts("中文查询") == []

    con = sqlite3.connect(str(target))
    try:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master")}
    finally:
        con.close()
    assert "mem_fts" not in names, f"只读检索建了表: {sorted(names)}"


def test_write_path_creates_schema_and_reads_back(tmp_path, monkeypatch):
    """写入口仍负责建表，且建完能读回来（行为未被破坏）。"""
    target = tmp_path / "fts.db"
    monkeypatch.setattr(fts_index, "_db_path", lambda: str(target))

    fts_index.index_node(1, "中文内容测试唯一标记词", "")

    rows = fts_index.search_fts("中文内容测试")
    assert any(r["node_id"] == 1 for r in rows), rows


def test_write_path_survives_when_schema_flag_reset(tmp_path, monkeypatch):
    """同一进程内重复写入口调用不报错（标记复用/复位都要安全）。"""
    target = tmp_path / "fts.db"
    monkeypatch.setattr(fts_index, "_db_path", lambda: str(target))

    fts_index.index_node(11, "重复写入唯一标记词", "")
    fts_index.index_node(12, "重复写入唯一标记词", "")
    fts_index.remove_node(11)

    rows = fts_index.search_fts("重复写入唯一标记词")
    ids = {r["node_id"] for r in rows}
    assert ids == {12}, ids
