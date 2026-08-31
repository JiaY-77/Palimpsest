# -*- coding: utf-8 -*-
"""
FTS5 全文搜索索引（trigram 分词器，支持中文任意子串匹配）。
独立 SQLite 索引文件（fts.db），丢了可 rebuild，不是主库。

用法：
    from core.fts_index import index_node, remove_node, search_fts, rebuild, sync_node
"""

import logging
import os
import sqlite3

from config import Config

logger = logging.getLogger(__name__)


def _db_path() -> str:
    return os.path.join(os.path.dirname(Config.DB_PATH), "fts.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
        "content, node_id UNINDEXED, source_path UNINDEXED, "
        "tokenize='trigram')"
    )
    conn.commit()
    return conn


def index_node(node_id: int, content: str, source_path: str = "") -> None:
    """索引单个节点（INSERT OR REPLACE）"""
    if not content:
        return
    conn = _connect()
    try:
        conn.execute("DELETE FROM mem_fts WHERE node_id = ?", (int(node_id),))
        conn.execute(
            "INSERT INTO mem_fts(content, node_id, source_path) VALUES(?, ?, ?)",
            (content, int(node_id), source_path or ""),
        )
        conn.commit()
    finally:
        conn.close()


def remove_node(node_id: int) -> None:
    """从索引中移除节点"""
    conn = _connect()
    try:
        conn.execute("DELETE FROM mem_fts WHERE node_id = ?", (int(node_id),))
        conn.commit()
    finally:
        conn.close()


def sync_node(node_id: int, content: str, source_path: str = "") -> None:
    """统一 FTS 同步入口：非空 content 写入索引，空内容移除索引。"""
    try:
        if content:
            index_node(node_id, content, source_path)
        else:
            remove_node(node_id)
    except Exception as e:
        logger.warning("FTS 索引同步失败 node=%s: %s", node_id, e)


def search_fts(query: str, limit: int = 10) -> list[dict]:
    """
    全文搜索。trigram 分词器（>=3字符且不含双引号）+ LIKE 兜底。
    返回 [{'node_id': int, 'content': str}]；异常/空查询返回空列表。
    """
    query = (query or "").strip()
    if not query:
        return []
    conn = _connect()
    try:
        if len(query) >= 3 and '"' not in query:
            # trigram 查询：带引号做精确子串匹配（query 不含双引号才安全）
            fts_query = f'"{query}"'
            rows = conn.execute(
                "SELECT node_id, content FROM mem_fts WHERE mem_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, int(limit)),
            ).fetchall()
        else:
            # 短查询（<3字符）或含双引号的查询：退化为 LIKE 子串匹配
            pattern = f"%{query}%"
            rows = conn.execute(
                "SELECT node_id, content FROM mem_fts WHERE content LIKE ? "
                "LIMIT ?",
                (pattern, int(limit)),
            ).fetchall()
        return [{"node_id": r[0], "content": (r[1] or "")[:120]} for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def rebuild(store) -> int:
    """
    全量重建 FTS 索引。store 为 TriviumStore 实例。
    返回索引节点数。
    """
    conn = _connect()
    try:
        conn.execute("DELETE FROM mem_fts")
        conn.commit()
        count = 0
        for nid, payload in store.iter_payloads():
            content = payload.get("content", "")
            if not content:
                continue
            source_path = payload.get("source_path", "")
            conn.execute(
                "INSERT INTO mem_fts(content, node_id, source_path) VALUES(?, ?, ?)",
                (content, int(nid), source_path),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()
