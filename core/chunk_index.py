"""chunk 级向量索引存储层（独立 sqlite：node_chunks.db）。

设计要点：
- 路径动态解析：CHUNK_DB_PATH 环境变量优先，否则 dirname(Config.DB_PATH)/node_chunks.db
- 每次操作短连接，用完即 close；写操作 BEGIN IMMEDIATE
- 向量 float32 BLOB（array('f').tobytes()），纯 python 余弦，不引入 numpy
"""

import math
import os
import re
import sqlite3
import time
from array import array

from config import Config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node_chunks (
  node_id INTEGER NOT NULL,
  chunk_idx INTEGER NOT NULL,
  chunk_text TEXT NOT NULL,
  vector BLOB NOT NULL,
  dim INTEGER NOT NULL,
  model TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (node_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS idx_chunks_model ON node_chunks(model);
"""


def _db_path() -> str:
    env = os.environ.get("CHUNK_DB_PATH")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(Config.DB_PATH)), "node_chunks.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _vec_to_blob(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _blob_to_vec(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return arr.tolist()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def sync_node_chunks(node_id: int, chunks: list[list[float]], texts: list[str], model: str) -> int:
    """同一 node_id 先删后插（幂等）。返回写入的 chunk 行数。"""
    if len(chunks) != len(texts):
        raise ValueError(f"chunks/texts 长度不一致: chunks={len(chunks)}, texts={len(texts)}")
    now = time.time()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(_SCHEMA)
        conn.execute("DELETE FROM node_chunks WHERE node_id = ?", (node_id,))
        conn.executemany(
            "INSERT INTO node_chunks (node_id, chunk_idx, chunk_text, vector, dim, model, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(node_id, i, texts[i], _vec_to_blob(chunks[i]), len(chunks[i]), model, now) for i in range(len(chunks))],
        )
        conn.commit()
        return len(chunks)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def remove_node_chunks(node_id: int) -> int:
    """删除该 node 的全部 chunk 行，返回删除行数。"""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(_SCHEMA)
        cur = conn.execute("DELETE FROM node_chunks WHERE node_id = ?", (node_id,))
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def search_chunks(query_vec: list[float], top_m: int, model: str) -> list[tuple[int, int, float]]:
    """按 model 过滤，暴力余弦，返回按 cos 降序的 (node_id, chunk_idx, cos)。"""
    q = array("f", query_vec)
    q_norm = math.sqrt(sum(x * x for x in q))
    results: list[tuple[int, int, float]] = []
    conn = _connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT node_id, chunk_idx, vector FROM node_chunks WHERE model = ?",
            (model,),
        ).fetchall()
        for node_id, chunk_idx, blob in rows:
            v = _blob_to_vec(blob)
            v_norm = math.sqrt(sum(x * x for x in v))
            if q_norm == 0.0 or v_norm == 0.0:
                cos = 0.0
            else:
                dot = sum(a * b for a, b in zip(q, v, strict=False))
                cos = dot / (q_norm * v_norm)
            results.append((node_id, chunk_idx, cos))
        results.sort(key=lambda r: r[2], reverse=True)
        return results[:top_m]
    finally:
        conn.close()


def chunk_stats() -> dict:
    """统计：总 chunk 数、涉及 node 数、按 model 分组计数。"""
    conn = _connect()
    try:
        _ensure_schema(conn)
        total, nodes = conn.execute("SELECT COUNT(*), COUNT(DISTINCT node_id) FROM node_chunks").fetchone()
        by_model = dict(conn.execute("SELECT model, COUNT(*) FROM node_chunks GROUP BY model"))
        return {"total_chunks": total, "total_nodes": nodes, "by_model": by_model}
    finally:
        conn.close()


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。；;])")

_CHUNK_MIN_CHARS = 200
_CHUNK_MAX_CHARS = 1500


def split_chunks(content: str) -> list[str]:
    """按空行分段 → 过短段向后合并 → 超长段按句号/分号二次切。

    单段内容（无空行且 < 1500 字，≈512 token 内）原样返回 [content]。
    """
    if not content or not content.strip():
        return []
    if "\n\n" not in content and len(content) < _CHUNK_MAX_CHARS:
        return [content]

    # 按空行分段
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content)]
    paragraphs = [p for p in paragraphs if p]

    # 过短段向后合并
    merged: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) < _CHUNK_MIN_CHARS or len(buf) < _CHUNK_MIN_CHARS:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            merged.append(buf)
            buf = p
    if buf:
        merged.append(buf)

    # 超长段按句号/分号二次切
    final: list[str] = []
    for seg in merged:
        if len(seg) <= _CHUNK_MAX_CHARS:
            final.append(seg)
            continue
        parts: list[str] = []
        buf2 = ""
        for sentence in _SENTENCE_SPLIT_RE.split(seg):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(buf2) + len(sentence) > _CHUNK_MAX_CHARS and buf2:
                parts.append(buf2)
                buf2 = sentence
            else:
                buf2 = f"{buf2}{sentence}" if buf2 else sentence
        if buf2:
            if len(buf2) < _CHUNK_MIN_CHARS and parts:
                parts[-1] = f"{parts[-1]}\n\n{buf2}"
            else:
                parts.append(buf2)
        final.extend(parts)

    return final or [content]
