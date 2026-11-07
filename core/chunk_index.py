"""chunk 级向量索引存储层（独立 sqlite：node_chunks.db）。

设计要点：
- 路径动态解析：CHUNK_DB_PATH 环境变量优先，否则 dirname(Config.DB_PATH)/node_chunks.db
- 每次操作短连接，用完即 close；写操作 BEGIN IMMEDIATE
- 向量 float32 BLOB（array('f').tobytes()），纯 python 余弦，不引入 numpy
"""

import logging
import math
import os
import re
import sqlite3
import time
from array import array

from config import Config

logger = logging.getLogger(__name__)

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
    """同一 node_id 先删后插（幂等）。返回写入的 chunk 行数。

    原子性（缺陷 1 修复）：``_ensure_schema`` 挪到 ``BEGIN IMMEDIATE`` 之前。
    sqlite3 的 ``executescript`` 会先隐式提交 pending 事务——旧代码在显式事务
    内调用它，建表那一刻就把事务吞掉了（实测 in_transaction True→False）。
    虽然当前 Python（legacy 隔离模式）随后会为 DELETE 自动重开一个隐式事务、
    回滚仍能保住旧数据，但显式事务被吞本身就是隐患：靠「恰好有隐式事务兜底」
    不应成为设计。建表幂等且不需要事务，放事务外直取，事务内只剩
    「DELETE + executemany」整体提交/回滚。
    """
    if len(chunks) != len(texts):
        raise ValueError(f"chunks/texts 长度不一致: chunks={len(chunks)}, texts={len(texts)}")
    now = time.time()
    conn = _connect()
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
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
    """删除该 node 的全部 chunk 行，返回删除行数。原子性处理与 sync_node_chunks 同款。"""
    conn = _connect()
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM node_chunks WHERE node_id = ?", (node_id,))
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def search_chunks(query_vec: list[float], top_m: int, model: str) -> list[tuple[int, int, float]]:
    """按 model + 维度过滤，暴力余弦，返回按 cos 降序的 (node_id, chunk_idx, cos)。

    维度校验（缺陷 2 修复）：SQL 直接过滤 ``dim = len(query_vec)``——不同
    embedding 模型/维度的脏行在 SQL 层就不进候选；读出后再比对一次
    ``len(v) != len(q)`` 兜底（防 store 与行内 dim 字段不一致的脏数据），
    命不一致的行跳过并告警，绝不静默按短的截断算余弦。
    """
    q = array("f", query_vec)
    q_dim = len(query_vec)
    q_norm = math.sqrt(sum(x * x for x in q))
    results: list[tuple[int, int, float]] = []
    conn = _connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT node_id, chunk_idx, vector FROM node_chunks WHERE model = ? AND dim = ?",
            (model, q_dim),
        ).fetchall()
        for node_id, chunk_idx, blob in rows:
            v = _blob_to_vec(blob)
            if len(v) != q_dim:
                logger.warning(
                    "chunk 向量维度与行内 dim 不一致，跳过 node=%s idx=%s (len=%s, 期望 %s)",
                    node_id,
                    chunk_idx,
                    len(v),
                    q_dim,
                )
                continue
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
    """统计：总 chunk 数、涉及 node 数、按 model 分组计数、按维度分布计数。

    ``by_dim``（缺陷 2 配套）：维度分布暴露少——换 embedding 模型后残存的
    旧维度行一眼可查（正常同 model 下只应有一个维度）。
    """
    conn = _connect()
    try:
        _ensure_schema(conn)
        total, nodes = conn.execute("SELECT COUNT(*), COUNT(DISTINCT node_id) FROM node_chunks").fetchone()
        by_model = dict(conn.execute("SELECT model, COUNT(*) FROM node_chunks GROUP BY model"))
        by_dim = {str(dim): cnt for dim, cnt in conn.execute("SELECT dim, COUNT(*) FROM node_chunks GROUP BY dim")}
        return {"total_chunks": total, "total_nodes": nodes, "by_model": by_model, "by_dim": by_dim}
    finally:
        conn.close()


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。；;])")

_CHUNK_MIN_CHARS = 200
_CHUNK_MAX_CHARS = 1500
_TITLE_MIN_CHARS = 8
# 标题长度上限：真标题（「接续点 …」「学习计划 …」）都在这一量级；
# 首行超过它几乎必是段落正文（如 700 字长段），不当标题拆。
_TITLE_MAX_CHARS = 64


def _is_title_line(lines: list[str], first_line: str) -> bool:
    return len(lines) > 1 and _TITLE_MIN_CHARS <= len(first_line) <= _TITLE_MAX_CHARS


def _paragraph_chunks(content: str) -> list[str]:
    """按空行分段 → 过短段向后合并 → 超长段按句号/分号二次切。

    单段内容（无空行且 < 1500 字，≈512 token 内）原样返回 [content]。
    """
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


def split_chunks(content: str) -> list[str]:
    """按空行分段切块；首行 ≥ 8 字时先把标题单独成第 0 个 chunk。

    标题块（issue #295 实测：标题与段落语义强互补，只存一种覆盖一半）：
    - 首行 ``content.split("\\n")[0].strip()`` 长度 ≥ 8 字时作为第 0 个 chunk 输出，
      段落切分只作用于其余行（标题不得在段落块里重复出现，避免同一段文本被 embed 两次）
    - 短内容（无空行且 < 1500 字）仍返回单元素、不拆标题——保持既有行为；
      标题 < 8 字时不单列，随首段一起切（改动前行为）
    """
    if not content or not content.strip():
        return []
    if "\n\n" not in content and len(content) < _CHUNK_MAX_CHARS:
        return [content]

    # 标题块：先于段落切分提取（标题独立成 chunk 0，其余行进入段落管线）
    lines = content.split("\n")
    title = lines[0].strip()
    if _is_title_line(lines, title):
        rest = "\n".join(lines[1:])
        return [title, *_paragraph_chunks(rest)]
    return _paragraph_chunks(content)


# ---- 三处写入接入的统一入口（mem_ingest / reembed / PUT-PATCH 改 content）----


def embedding_model_id(store=None) -> str:
    """当前 embedding 模型的稳定标识（写入 node_chunks.model，检索按它过滤）。

    从 Config 取实际模型名，不硬编码：local 取 GGUF 文件名（与
    core/embedding_fingerprint.current_fingerprint 同口径）、openai 取
    ``EMBEDDING_MODEL``、ollama 取 ``OLLAMA_EMBEDDING_MODEL``；均带 provider
    前缀（``{provider}:{model}``）防不同 provider 下同名模型串空间。取不到
    模型名时退化为 ``{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL}`` 这个稳定标识。
    """
    provider = (getattr(store, "provider", None) or Config.EMBEDDING_PROVIDER or "local").strip() or "local"
    if provider == "local":
        # 只取文件名：模型缓存目录搬家（EMBEDDING_MODEL_PATH 换盘）不算换空间
        model_id = os.path.basename(str(Config.EMBEDDING_MODEL_FILE))
    elif provider == "openai":
        model_id = str(Config.EMBEDDING_MODEL)
    else:  # ollama
        model_id = str(Config.OLLAMA_EMBEDDING_MODEL)
    if not model_id:
        # 稳定兜底标识（不做硬编码）：provider + Config 里现成的模型名字段
        model_id = f"{Config.EMBEDDING_PROVIDER}:{Config.EMBEDDING_MODEL}"
    return f"{provider}:{model_id}"


def index_node_chunks(store, node_id: int, content: str) -> int:
    """切分 + 逐段 embed + 落 chunk 表。单段内容则清掉已有 chunk 行（不写）。

    三处写入路径（mem_ingest / POST reembed / PUT-PATCH 改 content）共用：

    - ``Config.CHUNK_INDEX_ENABLED`` 关闭时直接返回 0（三处即时生效，不写不删）
    - 只有 > 1 段才写；单段（< 1500 字无空行）调 ``remove_node_chunks`` 清旧行，
      避免内容改短后被旧长内容 chunk 残留占检索
    - model 取 ``embedding_model_id(store)``（从 Config 取实际模型名，不硬编码）
    - **失败绝不阻塞主写路径**：整段 try/except，失败只 warning（含 node_id 与
      异常），主写入照常返回。embed 到一半失败时旧 chunk 行原样保留（此函数
      不先删后插：嵌入全部成功后才整事务替换）。
    """
    if not getattr(Config, "CHUNK_INDEX_ENABLED", True):
        return 0
    try:
        content = content or ""
        if not content.strip():
            return 0
        chunks = split_chunks(content)
        if len(chunks) <= 1:
            remove_node_chunks(node_id)
            return 0
        model = embedding_model_id(store)
        vectors = [store.embed_text(seg) for seg in chunks]
        return sync_node_chunks(node_id, vectors, chunks, model)
    except Exception as e:  # noqa: BLE001 —— chunk 索引失败只告警，绝不阻塞主写入
        logger.warning("chunk 索引写入失败（已跳过，主写入不受影响）node=%s: %s", node_id, e)
        return 0
