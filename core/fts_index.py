"""
FTS5 全文搜索索引（trigram 分词器，支持中文任意子串匹配）。
独立 SQLite 索引文件（fts.db），丢了可 rebuild，不是主库。

用法：
    from core.fts_index import index_node, remove_node, search_fts, rebuild, sync_node, build_fts_query
"""

import logging
import os
import re
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


def sync_node(node_id: int, content: str, source_path: str = "") -> bool:
    """统一 FTS 同步入口：非空 content 写入索引，空内容移除索引。

    返回 bool：True=同步成功；False=同步失败（失败仅 warning 不抛出，
    不破坏调用契约，调用方可据此感知 FTS 失败——如返回 False 打印告警）。
    """
    try:
        if content:
            index_node(node_id, content, source_path)
        else:
            remove_node(node_id)
        return True
    except Exception as e:  # noqa: BLE001 —— FTS 索引失败仅告警返回 False 不破坏契约
        logger.warning("FTS 索引同步失败 node=%s: %s", node_id, e)
        return False


_SPLIT_RE = re.compile(r"[\s,，。；;、/（）()？?！!：:+【】\[\]「」『』\-—·…]+")


def _even_indices(total: int, k: int) -> list[int]:
    """从 [0, total) 均匀取 k 个下标，首尾必含（k >= 2 时）。"""
    if total <= k:
        return list(range(total))
    return [round(i * (total - 1) / (k - 1)) for i in range(k)]


def build_fts_query(query: str, n: int = 3, max_grams: int = 12) -> str:
    """把自然语言查询改写为 FTS5 trigram OR 查询（长查询专用）。

    - 按中英文标点/空白切段
    - 每段做 n-gram 滑窗，**均匀采样**（step = max(1, total // max_grams)），
      保证覆盖句尾而不是只取开头
    - 段长 < n 且 < 3 字符的丢弃（trigram 至少 3 字符）
    - 全局片段上限 max_grams（12），用 " OR " 连接
    - 无有效片段返回 ""
    """
    if not query:
        return ""
    parts = _SPLIT_RE.split(query)
    grams: list[str] = []
    for part in parts:
        if len(part) < n:
            continue
        total = len(part) - n + 1
        step = max(1, total // max_grams)
        indices = list(range(0, total, step))
        if indices[-1] != total - 1:
            indices.append(total - 1)
        grams.extend(part[i : i + n] for i in indices)
    if not grams:
        return ""
    if len(grams) > max_grams:
        grams = [grams[i] for i in _even_indices(len(grams), max_grams)]
    return " OR ".join(grams)


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
        if len(query) > 8 and '"' not in query:
            fts_query = build_fts_query(query)
            if fts_query:
                rows = conn.execute(
                    "SELECT node_id, content FROM mem_fts WHERE mem_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (fts_query, int(limit)),
                ).fetchall()
            else:
                pattern = f"%{query}%"
                rows = conn.execute(
                    "SELECT node_id, content FROM mem_fts WHERE content LIKE ? "
                    "LIMIT ?",
                    (pattern, int(limit)),
                ).fetchall()
        elif len(query) >= 3 and '"' not in query:
            fts_query = f'"{query}"'
            rows = conn.execute(
                "SELECT node_id, content FROM mem_fts WHERE mem_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, int(limit)),
            ).fetchall()
        else:
            pattern = f"%{query}%"
            rows = conn.execute(
                "SELECT node_id, content FROM mem_fts WHERE content LIKE ? "
                "LIMIT ?",
                (pattern, int(limit)),
            ).fetchall()
        return [{"node_id": r[0], "content": (r[1] or "")[:120]} for r in rows]
    except Exception:  # noqa: BLE001 —— 全文检索失败返回空列表查询侧天然降级
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
