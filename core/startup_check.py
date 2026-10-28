"""
启动自检模块 —— 工程护栏
========================
服务/CLI 启动前快速体检核心依赖，返回结构化结果：

    {"ok": bool, "checks": [{"name": str, "ok": bool, "detail": str}, ...]}

检查项：
  ① 关键文件存在（config.py / requirements.txt 必须存在；data 目录缺失时自动创建）
  ② TriviumStore 能初始化（无需 embedding 在线，初始化失败才报告）
  ③ FTS5 全文索引能连接
  ④ 核心依赖可导入（fastapi / triviumdb / dotenv / requests 等）
  ⑤ Embedding 服务可用（HTTP 探测 Ollama + 实际 embed_text 验证向量非全零）
  ⑥（可选）TDB 文本通道预热（仅 TDB_TEXT_ENABLED=1 时注册）
  ⑦ 向量空间一致性（指纹守卫，轻量：不一致只 WARN 一行 + 修复命令，不改 ok/退出码）

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


def _check_key_files(root: str | None = None) -> str:
    """检查①：config.py、requirements.txt 必须存在；data 目录缺失时自动创建。

    root 缺省时使用包所在的项目根（生产路径），仅供测试注入隔离目录使用。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if root is None else str(root)
    missing = []
    auto_created = []
    for rel in ("config.py", "requirements.txt", "data"):
        path = os.path.join(root, rel)
        if os.path.exists(path):
            continue
        if rel == "data":
            os.makedirs(path, exist_ok=True)
            auto_created.append(rel)
        else:
            missing.append(rel)
    if missing:
        raise FileNotFoundError(f"缺失关键文件/目录: {', '.join(missing)}")
    parts = ["config.py / requirements.txt 均存在"]
    if auto_created:
        parts.append("data 目录已自动创建")
    else:
        parts.append("data 目录已存在")
    return "; ".join(parts)


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

    按 ``EMBEDDING_PROVIDER`` 分派——三个后端的探测方式不一样，不能一刀切：

    - ``local``  ：内置 llama.cpp，没有外部服务可探，直接实算一条向量。
      依赖未装 / 模型文件缺失 / 维度配置错都会在这里如实暴露。
    - ``ollama`` ：先 HTTP 探测 ``/api/tags``（2s 短超时，快、准），可达后再实算。
      先探是为了「端口不通时不必等满 embed 超时」，不是为了替代实算。
    - ``openai`` ：云端无法本地探测，直接实算。

    三条路最后**都必须实算一次**：HTTP 可达 ≠ 能用（模型没拉取时照样返回全零，
    那正是「自检全绿、检索全废」的静默失败来源）。
    """
    from config import Config, embedding_provider_source  # 延迟导入，避免重 import 副作用
    from core.trivium_store import EmbeddingUnavailableError, TriviumStore

    provider = getattr(Config, "EMBEDDING_PROVIDER", "local") or "local"
    source = embedding_provider_source()
    store = TriviumStore()

    if provider == "local":
        fail_detail = (
            "内置 embedding 不可用：需安装 llama-cpp-python"
            '（pip install "llama-cpp-python>=0.3.36,<0.4" '
            "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu）"
            "，并确保模型文件可用（EMBEDDING_MODEL_PATH 指向本地 GGUF，或允许自动下载）"
        )
    elif provider == "openai":
        fail_detail = "云端 embedding 不可用，请确认 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL"
    else:  # ollama
        fail_detail = "Ollama embedding 服务不可用，请确认已启动 Ollama 并 ollama pull qwen3-embedding:0.6b"

    # 只有 ollama 需要先探端口：端口不通时 embed_text 会白等一轮超时
    if provider == "ollama":
        try:
            import requests

            resp = requests.get(Config.OLLAMA_EMBEDDING_BASE_URL.rstrip("/") + "/api/tags", timeout=2)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"{fail_detail}（探测失败: {e}）") from e

    # 三条路都要实算一次：可达 ≠ 能用，模型未就绪时可能返回全零向量
    try:
        emb = store.embed_text("ping")
    except EmbeddingUnavailableError as e:
        raise RuntimeError(f"{fail_detail}（实际错误: {e}）") from e
    if _all_zero(emb):
        raise RuntimeError(f"{fail_detail}（返回全零向量）")
    # 回显来源（issue #192）：只看 ``provider=local`` 分不清是「用户显式配的」
    # 还是「.env 没写、自动落到默认值」——后者正是升级静默换后端的入口。
    source_note = "" if source == "env" else "，.env 未显式设置 → 自动探测"
    detail = f"Embedding 服务可用 (provider={provider}{source_note}, dim={len(emb)})"
    if source == "auto:fallback" and _has_existing_nodes(store):
        # 老库 + 靠默认值落到 local：升级风险最高的一种组合（2.16.0 把默认从
        # ollama 改成了 local，老 .env 通常没写 PROVIDER）。
        detail += (
            "；⚠ 未显式设置 EMBEDDING_PROVIDER，当前按默认 local 运行。"
            "2.16.0 起默认值已从 ollama 改为 local——若你原本用 Ollama / 云端，"
            "请在 .env 显式写上 EMBEDDING_PROVIDER"
        )
    return detail


def _has_existing_nodes(store: object) -> bool:
    """库里有没有节点（只看第一条就够）。异常一律当作「没有」，绝不影响自检。"""
    try:
        for _ in store.iter_payloads():  # type: ignore[attr-defined]
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _check_tdb_text() -> str:
    """检查⑥（可选，仅 TDB_TEXT_ENABLED=1 时注册）：TDB BM25 文本通道预热。

    预热失败不阻塞启动（_probe 吃掉异常只标红），可之后手动调
    `core.tdb_text_index.warmup()` 补预热——通道降级会被混合检索如实
    留痕（channels["tdb_text"]=="degraded"），不会静默吃查询。
    """
    from config import Config  # 延迟导入，避免重 import 副作用
    from core import tdb_text_index

    if not getattr(Config, "TDB_TEXT_ENABLED", False):
        return "未启用（TDB_TEXT_ENABLED=0）"
    if not tdb_text_index.warmup():
        raise RuntimeError("TDB 文本索引预热失败（build_text_index/flush 异常）")
    rows, status = tdb_text_index.search("启动自检")  # 容错查询，降级即判定失败
    if status == "degraded":
        raise RuntimeError("TDB 文本索引预热后查询仍降级（侧车缺失/加载失败）")
    return f"TDB 文本通道已预热 (rows={len(rows)}, status={status})"


def _check_embedding_space() -> str:
    """检查⑦（轻量）：向量空间指纹一致性 —— 只告警，绝不阻断启动。

    与 doctor 第 9 项的区别：doctor 里指纹不一致要 FAIL（部署体检拦下来），
    启动自检里恒 ok=True——指纹不一致只说明库里向量是旧空间算的，服务本身
    照常可用；若这里改退出码，升级后服务会起不来，比问题本身更糟。
    指纹检查自身任何异常（含读不到库）也降级为一句跳过说明。
    """
    try:
        from core import embedding_fingerprint as ef
        from core.trivium_store import TriviumStore

        store = TriviumStore()
        verdict = ef.compare(ef.read_fingerprint(store), ef.current_fingerprint(store))
        if verdict["ok"]:
            if verdict["soft_warnings"]:
                return "WARN: " + "；".join(verdict["soft_warnings"])
            return "向量空间指纹一致"
        return f"WARN: {verdict['reason']} 修复: {verdict['hint']}"
    except Exception as e:  # noqa: BLE001 —— 指纹检查任何异常都不影响启动判定
        return f"已跳过（指纹检查不可用: {e}）"


def run_startup_check() -> dict:
    """执行全部自检，返回结构化结果（永不抛异常）。"""
    checks = [
        _probe("关键文件存在", _check_key_files),
        _probe("TriviumStore 初始化", _check_store_init),
        _probe("FTS5 索引连接", _check_fts),
        _probe("依赖可导入", _check_dependencies),
        _probe("Embedding 服务可用", _check_embedding),
    ]
    from config import Config  # 延迟导入

    # 可选通道自检：仅启用 TDB 文本通道时注册，默认关闭时输出与改动前一致
    if getattr(Config, "TDB_TEXT_ENABLED", False):
        checks.append(_probe("TDB 文本通道预热", _check_tdb_text))

    # 指纹守卫（轻量）：恒 ok=True，不一致只在 detail 里 WARN + 给修复命令，
    # 不改变 result["ok"] / 退出码（否则升级后服务起不来）
    checks.append({"name": "向量空间一致性", "ok": True, "detail": _check_embedding_space()})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
