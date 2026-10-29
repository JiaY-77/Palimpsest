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
  ⑥ Embedding 分块配置自洽（issue #232：chunk ≤ n_batch，仅 provider=local）
  ⑦（可选）TDB 文本通道预热（仅 TDB_TEXT_ENABLED=1 时注册）
  ⑧ 向量空间一致性（指纹守卫，轻量：不一致只 WARN 一行 + 修复命令，不改 ok/退出码）
  ⑨ FTS 索引完整性（issue #241：主库节点数 vs mem_fts 行数；注册在 ③ 之后，
     恢复备份后能直接暴露「fts.db 没恢复 → 混合检索悄悄降级纯语义」，失败时
     附带 fix=重建命令，doctor 侧经 _suggest_fix 复用）

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


def _mem_fts_count(path: str) -> int:
    """只读统计 mem_fts 行数；文件不存在 / 表缺失 / 查询失败一律返回 0（不抛异常）。"""
    if not os.path.isfile(path):
        return 0
    conn = None
    try:
        import pathlib

        uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        return int(conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        if conn is not None:
            conn.close()


def _check_fts_integrity() -> str:
    """检查⑨（issue #241）：FTS 索引完整性 —— 主库节点数 vs mem_fts 行数。

    fts.db 丢了会被 `_ensure_schema` 的 `CREATE ... IF NOT EXISTS` 静默建出
    空表，混合检索（rrf/cascade）随之悄悄退化为纯语义——检查③「能连接」
    看不出来，本项专门量「索引覆盖了多少」：

    - 主库节点数走 store 的连接（`store._acquire()` 拿 db 再 `node_count()`，
      与 scripts/backup_db.py::verify_group 同款用法），不做全量 iter_payloads
      —— doctor 已有两项全量遍历，能靠 node_count 的就别再加一项；
    - 三档判定：空库(节点 0) → ok；索引 0 行/缺失而节点 >0 → FAIL（缺失）；
      行数不足节点数一半 → FAIL（明显滞后）；其余 → ok 带计数。
    """
    from core.fts_index import _db_path  # 延迟导入
    from core.trivium_store import TriviumStore

    store = TriviumStore()
    db = store._acquire()
    try:
        node_count = int(db.node_count())
    finally:
        db.close()

    if node_count == 0:
        return "库为空，无需索引"

    fts_rows = _mem_fts_count(_db_path())
    if fts_rows == 0:
        raise RuntimeError(f"FTS 索引缺失：主库 {node_count} 个节点，索引 0 行；混合检索将退化为纯语义")
    if fts_rows < node_count * 0.5:
        raise RuntimeError(
            f"FTS 索引明显滞后：主库 {node_count} 个节点，索引仅 {fts_rows} 行（不足一半）；"
            "长文本的 FTS 通道覆盖不全，建议重建"
        )
    return f"FTS 索引完整 (主库 {node_count} / 索引 {fts_rows})"


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
        #
        # 除了写进 detail（doctor / 自检输出里看得到），还必须**打一条 WARNING**：
        # 这是「用户没选过，却被默认值换了后端」的情形，只在 detail 里当脚注太轻，
        # 启动日志里扫不到就等于没通知（issue #192）。
        message = (
            "EMBEDDING_PROVIDER 未显式设置，当前按默认 local（内置 llama.cpp）运行。"
            "2.16.0 起默认值已从 ollama 改为 local——若你原本用 Ollama / 云端，"
            "请在 .env 显式写 EMBEDDING_PROVIDER，否则升级后可能悄悄换了后端而不自知。"
        )
        logger.warning("⚠ %s", message)
        detail += f"；⚠ {message}"
    return detail


def _check_embedding_chunk_config() -> str:
    """检查⑥（issue #232）：分块池化块长自洽 —— chunk 必须 ≤ n_batch。

    块最终也要过 ``embed()``，而 embed() 内部有 ``tokens = tokens[:n_batch]``
    静默截断：``EMBEDDING_LOCAL_CHUNK_TOKENS`` 配得比 ``EMBEDDING_LOCAL_N_BATCH``
    大，块自己就被截断，分块池化整条机制失效（长节点向量仍只覆盖前缀）。
    报 error（raise RuntimeError，由 _probe 标红），不降级为告警——带病配置
    必须在启动时挡下来，不能等用户发现召回质量莫名变差才排查。
    """
    from config import Config  # 延迟导入，避免重 import 副作用

    provider = getattr(Config, "EMBEDDING_PROVIDER", "local") or "local"
    if provider != "local":
        return f"不适用（provider={provider}，分块池化仅 provider=local 生效）"
    chunk = int(getattr(Config, "EMBEDDING_LOCAL_CHUNK_TOKENS", 512))
    n_batch = int(getattr(Config, "EMBEDDING_LOCAL_N_BATCH", 512))
    if chunk > n_batch:
        raise RuntimeError(
            f"EMBEDDING_LOCAL_CHUNK_TOKENS={chunk} > EMBEDDING_LOCAL_N_BATCH={n_batch}："
            "chunk 必须 ≤ n_batch——每个块本身也要过 embed()，而 embed() 超过 n_batch "
            "的 token 会被静默截断，块自己被截断时分块池化失效（向量仍只覆盖前缀）。"
        )
    return f"分块配置自洽 (chunk={chunk} ≤ n_batch={n_batch})"


def _has_existing_nodes(store: object) -> bool:
    """库里有没有节点（只看第一条就够）。异常一律当作「没有」，绝不影响自检。"""
    try:
        for _ in store.iter_payloads():  # type: ignore[attr-defined]
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _check_tdb_text() -> str:
    """检查⑦（可选，仅 TDB_TEXT_ENABLED=1 时注册）：TDB BM25 文本通道预热。

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
    """检查⑧（轻量）：向量空间指纹一致性 —— 只告警，绝不阻断启动。

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
        _probe("Embedding 分块配置", _check_embedding_chunk_config),
    ]
    # FTS 索引完整性（issue #241）：注册在「FTS5 索引连接」之后。失败时附带
    # 可执行修复命令（startup_check 其他项不含 fix 字段；doctor 复用时会经
    # _suggest_fix 生成同样的命令，保持两处口径一致）。
    fts_integrity = _probe("FTS 索引完整性", _check_fts_integrity)
    fts_integrity.setdefault("fix", "")
    if not fts_integrity["ok"]:
        fts_integrity["fix"] = "python scripts/check_fts_consistency.py --repair"
    checks.insert(3, fts_integrity)
    from config import Config  # 延迟导入

    # 可选通道自检：仅启用 TDB 文本通道时注册，默认关闭时输出与改动前一致
    if getattr(Config, "TDB_TEXT_ENABLED", False):
        checks.append(_probe("TDB 文本通道预热", _check_tdb_text))

    # 指纹守卫（轻量）：恒 ok=True，不一致只在 detail 里 WARN + 给修复命令，
    # 不改变 result["ok"] / 退出码（否则升级后服务起不来）
    checks.append({"name": "向量空间一致性", "ok": True, "detail": _check_embedding_space()})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
