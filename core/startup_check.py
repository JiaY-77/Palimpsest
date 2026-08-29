# -*- coding: utf-8 -*-
"""
启动自检模块 —— 工程护栏
========================
服务/CLI 启动前快速体检核心依赖，返回结构化结果：

    {"ok": bool, "checks": [{"name": str, "ok": bool, "detail": str}, ...]}

检查项：
  ① 关键文件存在（config.py / requirements.txt / data 目录）
  ② TriviumStore 能初始化（无需 embedding 在线，初始化失败才报告）
  ③ FTS5 全文索引能连接
  ④ 核心依赖可导入（fastapi / triviumdb / dotenv / requests 等）
  ⑤ Embedding 服务可用（HTTP 探测 Ollama + 实际 embed_text 验证向量非全零）

设计原则：任何单项失败只记录，不抛异常，不阻断服务启动（可用性优先）。
"""

import importlib
import importlib.util
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)


def _probe(name: str, fn) -> dict:
    """执行单项检查：捕获异常转为结构化结果，内部永不抛异常。"""
    try:
        detail = fn()
        return {"name": name, "ok": True, "detail": str(detail)}
    except Exception as e:  # noqa: BLE001 —— 护栏要吞掉所有单项异常
        logger.warning("启动自检项「%s」失败: %s", name, e)
        return {"name": name, "ok": False, "detail": str(e)}


def _check_key_files() -> str:
    """检查①：config.py、requirements.txt、data 目录是否存在。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    missing = []
    for rel in ("config.py", "requirements.txt", "data"):
        if not os.path.exists(os.path.join(root, rel)):
            missing.append(rel)
    if missing:
        raise FileNotFoundError(f"缺失关键文件/目录: {', '.join(missing)}")
    return "config.py / requirements.txt / data 均存在"


def _check_store_init() -> str:
    """检查②：TriviumStore 能初始化。

    仅验证构造与取库路径不抛错；embedding 服务（ollama/云端）是否在线
    属于运行时能力，不要求此时在线。
    """
    from core.trivium_store import TriviumStore  # 延迟导入，避免重 import 副作用

    store = TriviumStore()
    db_path = store.db_path
    return f"TriviumStore 初始化成功 (DB={db_path}, dim={store.dim})"


def _check_fts() -> str:
    """检查③：FTS5 索引能连接（能否建/查虚拟表）。"""
    from core.fts_index import _db_path, search_fts  # 延迟导入

    path = _db_path()
    # 连接并触发建表（trigram 分词），随后做一次空查询验证可用
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
            "content, node_id UNINDEXED, source_path UNINDEXED, "
            "tokenize='trigram')"
        )
        conn.commit()
    finally:
        conn.close()
    search_fts("护栏")  # 容错空查询，抛异常即判定失败
    return f"FTS5 索引可用 (fts.db={path})"


def _check_dependencies() -> str:
    """检查④：核心依赖可导入。"""
    required = ("fastapi", "triviumdb", "pydantic", "dotenv", "requests")
    missing = [m for m in required if importlib.util.find_spec(m) is None]
    if missing:
        raise ImportError(f"缺失依赖包: {', '.join(missing)}")
    return "依赖可导入: " + ", ".join(required)


def _all_zero(vec) -> bool:
    """向量是否全零（Ollama/云端 embedding 失败时返回 [0.0]*dim）。"""
    try:
        return sum(abs(float(v)) for v in vec) == 0
    except (TypeError, ValueError):
        return True


def _check_embedding() -> str:
    """检查⑤：Embedding 服务可用（向量非全零）。

    策略：先用 HTTP 探测 Ollama 端点（快、准），可达后再实际调 embed_text
    验证返回向量非全零——避免「自检通过但检索时向量全零」的静默失败。
    """
    from config import Config  # 延迟导入，避免重 import 副作用
    from core.trivium_store import TriviumStore

    provider = getattr(Config, "EMBEDDING_PROVIDER", "ollama") or "ollama"
    store = TriviumStore()
    fail_detail = (
        "Ollama embedding 服务不可用，请确认已启动 Ollama 并 "
        "ollama pull qwen3-embedding:0.6b；"
        "若使用云端 EMBEDDING_PROVIDER=openai 请确认 EMBEDDING_API_KEY"
    )

    if provider == "openai":
        # 云端：无法本地探测，直接实际调 embed_text 验证非全零
        emb = store.embed_text("ping")
        if _all_zero(emb):
            raise RuntimeError(fail_detail)
        return f"Embedding 服务可用 (provider=openai, dim={len(emb)})"

    # 本地 ollama：先 HTTP 探测（2s 短超时，快、准）
    try:
        import requests

        resp = requests.get("http://localhost:11434/api/tags", timeout=2)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"{fail_detail}（探测失败: {e}）")

    # HTTP 可达再实际 embed_text 验证非全零（模型未拉取时仍会返回全零）
    emb = store.embed_text("ping")
    if _all_zero(emb):
        raise RuntimeError(fail_detail)
    return f"Embedding 服务可用 (provider=ollama, dim={len(emb)})"


def run_startup_check() -> dict:
    """执行全部自检，返回结构化结果（永不抛异常）。"""
    checks = [
        _probe("关键文件存在", _check_key_files),
        _probe("TriviumStore 初始化", _check_store_init),
        _probe("FTS5 索引连接", _check_fts),
        _probe("依赖可导入", _check_dependencies),
        _probe("Embedding 服务可用", _check_embedding),
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
