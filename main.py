"""
Palimpsest — FastAPI 主入口
提供记忆提取、检索、导入、导出的完整 API 服务

运行约束（单进程写入）
=====================
库文件由 triviumdb 以独占写模式打开：第二个写连接（同进程或跨进程）会在
构造 TriviumDB 时直接失败并报 `Database locked`；节点 ID 由应用层按
「当前已提交最大 id + 1」分配（见 core/trivium_store.py 的 insert_node_tx）。

因此本服务必须单进程运行：

  - 不要用 `uvicorn --workers N` / gunicorn 多 worker，也不要在同一 `DB_PATH`
    上跑多个 REST 实例——写请求会直接报错；
  - MCP 服务 / CLI / dashboard 与 REST 同时指向同一 `DB_PATH` 时，写操作互斥；
    需要并行写请各自指向不同的 `DB_PATH`。

这一约束是 fail-fast 的：不会静默产生重复 ID 或损坏数据。详见 README「启动」。
"""

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import Config
from core.fts_index import sync_node
from core.reporting import generate_report
from core.startup_check import run_startup_check
from core.strategy import (
    GRAPH_ENHANCE_TOP_K,
)
from core.strategy import (
    clamp_max_topics as _strategy_clamp_max_topics,
)
from core.strategy import (
    decide_post_turn as _strategy_post_turn,
)
from core.strategy import (
    decide_pre_compress as _strategy_pre_compress,
)
from core.strategy import (
    decide_pre_turn as _strategy_pre_turn,
)
from core.strategy import (
    decide_session_end as _strategy_session_end,
)
from core.strategy import (
    extract_graph_topics as _strategy_extract_graph_topics,
)
from core.strategy import (
    format_graph_enhancement as _strategy_format_graph_enhancement,
)
from core.strategy import (
    is_near_duplicate as _strategy_is_near_duplicate,
)
from core.task_state import apply_task_patch
from core.trivium_store import DatabaseBusyError, EmbeddingUnavailableError, TriviumStore
from core.version import get_version
from mcp_tools import (
    graph_neighbors as _mcp_graph_neighbors,
)
from mcp_tools import mcp as _mcp_server
from mcp_tools import (
    mem_communities as _mcp_mem_communities,
)
from mcp_tools import (
    mem_fact_history as _mcp_mem_fact_history,
)
from mcp_tools import (
    mem_hybrid_search as _mcp_mem_hybrid_search,
)
from mcp_tools import (
    mem_ingest as _mcp_mem_ingest,
)
from mcp_tools import (
    mem_link as _mcp_mem_link,
)
from mcp_tools import (
    mem_recent as _mcp_mem_recent,
)
from mcp_tools import (
    mem_search as _mcp_mem_search,
)
from mcp_tools import (
    skill_search as _mcp_skill_search,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期：启动自检 + 托管 MCP-over-HTTP 会话管理器。

    自检是独立协程（与 store 懒加载解耦）：首次请求才建连库，进程启动只做
    只读自检；阻塞文件 IO 经 to_thread 下放，不占用事件循环。

    MCP-over-HTTP（/mcp）让 MCP 客户端复用本进程的 store，避免第二个进程
    并发打开同一个 TriviumDB —— TriviumDB 对库文件是严格排他的，跨进程并发
    会导致写入失败，并在文件组留下残留进而污染整库。见 issue #32。
    """
    async with _mcp_server.session_manager.run():
        await _startup_self_check()
        yield


app = FastAPI(title="Palimpsest", lifespan=_lifespan)

# MCP-over-HTTP：把 MCP 工具挂到同一进程（/mcp，streamable-http 传输）。
# 这是「单写者」约束的落点：REST 与 MCP 共用本进程的 store，杜绝两个进程
# 并发打开同一个 TriviumDB（严格排他会让写入方失败并污染文件组，见 issue #32）。
# MCP 客户端接入方式：url = http://127.0.0.1:8090/mcp
# FastMCP 默认把 streamable-http 端点放在 /mcp，挂到 /mcp 会变成 /mcp/mcp；
# 置为 "/" 后挂载路径即对外路径。
_mcp_server.settings.streamable_http_path = "/"


class _NormalizeMcpPath:
    """把 `/mcp`（无尾斜杠）规范化为 `/mcp/`。

    Starlette 的 Mount 在收到不带尾斜杠的路径时会返回 307 重定向到带斜杠版本；
    多数 MCP 客户端（含 Hermes）不跟随重定向，于是按文档接入方式
    （url = http://127.0.0.1:8090/mcp）连接会直接失败。这里在路由匹配前
    就地改写 path，使两种写法都能直达 MCP 子应用。

    作为 ASGI 中间件加在 `app` 上（`app.add_middleware`），不替换 `app`
    对象本身——否则其后的 `@app.exception_handler` 等装饰器会全部失效。
    """

    def __init__(self, app, prefix="/mcp"):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == self.prefix:
            scope = dict(scope)
            scope["path"] = self.prefix + "/"
            scope["raw_path"] = (self.prefix + "/").encode()
        await self.app(scope, receive, send)


app.mount("/mcp", _mcp_server.streamable_http_app())
# 见 _NormalizeMcpPath 文档字符串：消除 307，兼容无尾斜杠接入。
app.add_middleware(_NormalizeMcpPath, prefix="/mcp")

# API Key 鉴权开关：PALIMPSEST_API_KEY 默认空 = 不启用（localhost 本机直连）。
# 设置后除 / 健康检查外所有请求须带 Bearer 或 X-API-Key，否则 401。
# API Key 仅做校验，不做加密传输；公网部署必须配 HTTPS 反向代理。
API_KEY = Config.API_KEY


@app.exception_handler(EmbeddingUnavailableError)
async def _embedding_unavailable_handler(request, exc: EmbeddingUnavailableError):
    """Embedding 服务不可用 → 503 fail-fast，不静默降级为全零向量。"""
    logger.warning("Embedding 服务不可用: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "embedding 服务不可用",
            "hint": "embedding 服务不可用，检查 Ollama 是否启动或 EMBEDDING_* 配置",
        },
    )


@app.exception_handler(DatabaseBusyError)
async def _database_busy_handler(request, exc: DatabaseBusyError):
    """库被其他进程占用 → 503 fail-fast，明确告知冲突而不是含糊报错。

    与 EmbeddingUnavailableError 的处理同构：异常消息本身含库路径与处理指引，
    经 `hint` 原样回给调用方，便于直接定位是谁在抢库。
    """
    logger.warning("记忆库被其他进程占用: %s", exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "记忆库被其他进程占用",
            "hint": str(exc),
        },
    )


@app.middleware("http")
async def _api_key_middleware(request: Request, call_next):
    """可选 API Key 鉴权中间件。

    未配置 PALIMPSEST_API_KEY 时放行所有请求（不启用鉴权）。
    已配置时，除 / 健康检查外的所有请求必须带
    Authorization: Bearer <key> 或 X-API-Key: <key>，否则 401。
    /docs 等调试路径同样受保护（与业务端点一致的安全边界）。
    """
    if not API_KEY:
        return await call_next(request)

    path = request.url.path
    if path == "/":
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        provided = auth[len("Bearer ") :].strip()
    else:
        provided = request.headers.get("x-api-key", "").strip()

    if not provided or not secrets.compare_digest(provided, API_KEY):
        return JSONResponse(status_code=401, content={"detail": "未授权：缺少或无效的 API Key"})

    return await call_next(request)


# ---- 全局服务实例（首次访问时惰性初始化） ----
# 不在 import 期构造 TriviumStore：避免 import main 即连库、建索引等副作用
# （测试、CLI、打包扫描导入本模块时不应触碰数据库）。
_store: TriviumStore | None = None


def _get_store() -> TriviumStore:
    """取全局 TriviumStore，首次调用时构造（惰性单例）。"""
    global _store
    if _store is None:
        _store = TriviumStore()
    return _store


async def _startup_self_check():
    """启动自检（工程护栏）：只记录，不阻断 —— 避免自检失败拖垮服务可用性。

    结果可通过 CLI 子命令 `palimpsest_cli.py startup-check` 手动触发查看完整明细。
    """
    import json

    result = await asyncio.to_thread(run_startup_check)
    if result["ok"]:
        logger.info("启动自检全部通过（%s 项）", len(result["checks"]))
    else:
        failed = [c for c in result["checks"] if not c["ok"]]
        logger.error(
            "启动自检存在失败项（%s/%s）：%s",
            len(failed),
            len(result["checks"]),
            json.dumps({c["name"]: c["detail"] for c in failed}, ensure_ascii=False),
        )


# ---- API 端点 ----
@app.get("/")
def root():
    return {
        "service": "Palimpsest",
        "version": get_version(),
        "endpoints": [
            "/",
            "/export",
            "/summary",
            "/report",
            "/memory/{node_id}",
            "/memory/{node_id}/vector",
            "/memory/{node_id}/reembed",
            "/mem/search",
            "/skill/search",
            "/mem/hybrid-search",
            "/mem/ingest",
            "/mem/link",
            "/mem/edge",
            "/mem/fact-history",
            "/mem/recent",
            "/tasks/active",
            "/graph/neighbors",
            "/graph/communities",
            "/mem/stats",
            "/lifecycle/pre-turn",
            "/lifecycle/post-turn",
            "/lifecycle/session-end",
            "/lifecycle/pre-compress",
            "/lifecycle/context-enhance",
        ],
    }


@app.get("/export")
def export_memories(page: int = 1, page_size: int = 100, include_payload: bool = False):
    """导出记忆为精简摘要（分页：默认第一页 100 条，page_size 上限 500）。

    ``include_payload=true`` 时每条附带完整 payload —— 供需要 ``(id, payload)``
    全量遍历的调用方（如 RemoteStore.iter_payloads 走 REST 复现本地语义）。
    默认关闭：摘要页面的响应体不该为 dashboard 之类只看摘要的调用方膨胀。
    """
    if page_size > 500:
        page_size = 500
    if page_size < 1:
        page_size = 100
    if page < 1:
        page = 1

    nodes = []
    for nid, payload in _get_store().iter_payloads():
        item = {
            "id": nid,
            "type": payload.get("type", ""),
            "content": payload.get("content", ""),
            "importance": payload.get("importance", 0),
            "status": payload.get("status", ""),
        }
        if include_payload:
            item["payload"] = payload
        nodes.append(item)

    # 按重要性降序排列（importance 可能为脏字符串，用 _to_float 兜底防排序类型错误）
    from core.utils import _to_float

    nodes.sort(key=lambda n: _to_float(n.get("importance", 0), 0.0), reverse=True)

    total_nodes = len(nodes)
    total_pages = (total_nodes + page_size - 1) // page_size
    start = (page - 1) * page_size
    end = start + page_size
    page_nodes = nodes[start:end]

    return {
        "status": "ok",
        "total_nodes": total_nodes,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "memories": page_nodes,
    }


@app.get("/summary")
def summary():
    """生成一份人类可读的记忆摘要"""
    events = []
    characters = []
    plots = []
    total = 0

    for _nid, payload in _get_store().iter_payloads():
        total += 1
        t = payload.get("type", "")
        content = payload.get("content", "")
        if t == "event":
            events.append(content)
        elif t == "character_state":
            characters.append(content)
        elif t == "plot_plan":
            plots.append(content)

    return {
        "status": "ok",
        "total_memories": total,
        "summary": {
            "剧情事件": events[:5],
            "角色状态": characters[:5],
            "剧情计划": plots[:5],
        },
    }


@app.post("/report")
async def report_endpoint():
    """
    基于当前数据库中的所有记忆，调用 LLM 生成一份角色灵魂分析报告。
    核心逻辑见 core/reporting.py 的 generate_report（函数式拆分，行为不变）。
    Prompt 面向小说创作 / 角色扮演场景（角色心理分析），不是通用记忆摘要。
    """
    return await generate_report(_get_store())


@app.get("/memory/{node_id}")
def get_memory(node_id: int):
    """获取指定 ID 的记忆节点 payload（剥掉内部字段 secret_hint / linked_from / linked_kb_ids / superseded）"""
    node = _get_store().get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在")
    payload = node.get("payload", {})
    stripped = {
        k: v for k, v in payload.items() if k not in ("secret_hint", "linked_from", "linked_kb_ids", "superseded")
    }
    return {"id": node_id, "payload": stripped}


@app.delete("/memory/{node_id}")
def delete_memory(node_id: int):
    """删除指定 ID 的记忆节点"""
    try:
        _get_store().delete_node(node_id)
        # FTS 全文索引同步（失败不阻塞主删除，可手动 fts-rebuild 兜底）
        sync_node(node_id, "")
        return {"status": "ok", "message": f"节点 {node_id} 已删除"}
    except Exception as e:
        logger.info("删除节点失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="删除失败：节点不存在或已被删除") from e


def _sync_fts_after_update(node_id: int) -> None:
    """更新节点后同步 FTS 全文索引（失败不阻塞主更新，可手动 fts-rebuild 兜底）。"""
    try:
        node = _get_store().get_node(node_id)
        content = ((node or {}).get("payload") or {}).get("content", "")
        sync_node(node_id, content)
    except Exception as e:  # noqa: BLE001 —— FTS 同步失败仅告警不阻塞节点更新
        logger.warning("FTS 索引同步失败 node=%s: %s", node_id, e)


@app.put("/memory/{node_id}")
def update_memory_payload(node_id: int, payload: dict):
    """更新指定 ID 的记忆 payload（部分更新合并语义：只改传入字段，其余保留）。

    ``type=task`` 节点走任务写路径（``core.task_state.apply_task_patch``）：状态变化时
    自动补 ``last_touched_at``、写 ``type=record`` 日志节点并建 ``record -[LOGS]-> task``
    边，保证任务状态「变了必留痕」；其余类型节点行为不变。
    """
    try:
        store = _get_store()
        node = store.get_node(node_id)
        if node is None:
            # 404 + 固定提示（不泄漏内部异常，安全审计要求）
            raise HTTPException(status_code=404, detail="更新失败：节点不存在或数据格式错误")
        if (node.get("payload") or {}).get("type") == "task":
            result = apply_task_patch(store, node_id, payload)
            _sync_fts_after_update(node_id)
            response = {
                "status": "ok",
                "message": f"节点 {node_id} payload 已更新",
                "task_state": result["state"],
                "state_changed": result["changed"],
            }
            if result["changed"]:
                response["previous_state"] = result["previous_state"]
            return response
        store.update_payload(node_id, payload)
        _sync_fts_after_update(node_id)
        result = {"status": "ok", "message": f"节点 {node_id} payload 已更新"}
        # issue #52：改 content 不会自动重算向量（避免在写路径里塞网络调用），
        # 但必须让调用方知道——否则语义检索静默按旧文本漂移。
        if "content" in payload:
            result["warning"] = (
                "content 已更新，但向量未重算——语义检索与冲突检测仍按旧文本执行。"
                f"如需同步，调用 POST /memory/{node_id}/reembed。"
            )
        return result
    except HTTPException:
        raise
    except ValueError as e:
        # 仅「任务状态非法」这类校验拒绝才映射 400（任务节点自身存在性已在上面判过）
        logger.info("更新节点 payload 失败（校验拒绝） node=%s: %s", node_id, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.info("更新节点 payload 失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="更新失败：节点不存在或数据格式错误") from e


@app.patch("/memory/{node_id}")
def patch_memory_payload(node_id: int, payload: dict):
    """PATCH：部分更新指定 ID 的记忆 payload（与 PUT 同逻辑，但语义上更精确）"""
    try:
        _get_store().update_payload(node_id, payload)
        _sync_fts_after_update(node_id)
        result = {"status": "ok", "message": f"节点 {node_id} payload 已更新"}
        if "content" in payload:
            result["warning"] = (
                "content 已更新，但向量未重算——语义检索与冲突检测仍按旧文本执行。"
                f"如需同步，调用 POST /memory/{node_id}/reembed。"
            )
        return result
    except Exception as e:
        logger.info("更新节点 payload 失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="更新失败：节点不存在或数据格式错误") from e


@app.post("/memory/{node_id}/reembed")
def reembed_memory(node_id: int):
    """按当前 content 重算并写回该节点的向量（issue #52）。

    改 content 后语义漂移的补救入口：服务端自己生成向量，调用方不必在
    外部两步走（POST ollama embeddings → PATCH /memory/{id}/vector）。
    显式端点而非 PUT/PATCH 自动重算——重嵌要花算力且是网络调用，不该在
    写路径里静默执行（会阻塞、超时、503）。
    """
    try:
        ok = _get_store().reembed_node(node_id)
    except EmbeddingUnavailableError:
        raise
    except Exception as e:
        logger.info("重算节点向量失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="重算失败：节点不存在或数据格式错误") from e
    if not ok:
        raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在")
    return {"status": "ok", "message": f"节点 {node_id} 向量已按当前 content 重算"}


@app.patch("/memory/{node_id}/vector")
def update_memory_vector(node_id: int, vector: list[float]):
    """更新指定 ID 的记忆向量（维度必须匹配）"""
    try:
        if len(vector) != _get_store().dim:
            raise HTTPException(
                status_code=400,
                detail=f"向量维度应为 {_get_store().dim}，实际为 {len(vector)}",
            )
        _get_store().update_vector(node_id, vector)
        return {"status": "ok", "message": f"节点 {node_id} 向量已更新"}
    except HTTPException:
        raise
    except Exception as e:
        logger.info("更新节点向量失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="向量更新失败：节点不存在或维度不匹配") from e


# ---- 统一语义层端点（2026-08-27 换脑插件通道）----
# 对齐 mcp_server 工具（mem_search / mem_ingest / mem_link / graph_neighbors），
# 供 Hermes memory provider 插件（plugins/palimpsest/）通过 REST :8090 调用。
# 返回解析后的 JSON（FastAPI 自动序列化），客户端无需再 parse 字符串。


class MemSearchRequest(BaseModel):
    query: str
    scope: str = "all"  # memory | kb | all
    domain: str = ""
    domain_bias: str = ""
    top_k: int = 5
    include_neighbors: bool = False
    include_outdated: bool = False
    block: str = ""
    domain_boost: str = ""  # 加性软加权：对 node_domain == domain_boost 的候选加分
    tier: str = "facts"  # 记忆分层：facts(默认) | logs | ""(不过滤，改动前行为)
    as_of: float | None = None  # bi-temporal 历史视图：按该时间点回看事实是否为真


class SkillSearchRequest(BaseModel):
    query: str
    top_k: int = 5


class MemIngestRequest(BaseModel):
    content: str
    type: str = "memory"  # memory | plan | record | correction | event | kb_chunk ...
    importance: float = 0.5
    domain: str = ""
    source: str = ""


class MemHybridSearchRequest(BaseModel):
    query: str
    scope: str = "all"  # memory | kb | all
    domain: str = ""
    domain_bias: str = ""
    top_k: int = 5
    mode: str = "rrf"  # rrf | cascade
    fts_limit: int = 50
    include_neighbors: bool = False
    neighbor_limit: int = 5
    include_outdated: bool = False
    block: str = ""
    tier: str = "facts"  # 记忆分层：facts(默认) | logs | ""(不过滤，改动前行为)
    as_of: float | None = None  # bi-temporal 历史视图


class MemLinkRequest(BaseModel):
    source_id: int
    target_id: int
    relation: str = "RELATED_TO"
    weight: float = 0.9
    bidirectional: bool = True


class MemEdgeDeleteRequest(BaseModel):
    """删除一条边（issue #51）。"""

    source_id: int
    target_id: int
    relation: str = "RELATED_TO"


class GraphNeighborsRequest(BaseModel):
    node_id: int
    relation: str = ""
    depth: int = 1
    limit: int = 20
    min_weight: float = 0.0
    block: str = ""


class MemRecentRequest(BaseModel):
    domain: str = ""
    limit: int = 10
    as_of: float | None = None  # bi-temporal 历史视图：按该时间点回看事实是否为真


class GraphCommunitiesRequest(BaseModel):
    min_community_size: int = 2
    top_k: int = 20
    with_summary: bool = True


class MemFactHistoryRequest(BaseModel):
    node_id: int


def _as_json(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 —— JSON 解析失败回退原始文本保持 JSON 结构稳定
        return {"raw": text}


@app.post("/mem/search")
def mem_search(req: MemSearchRequest):
    return _as_json(
        _mcp_mem_search(
            req.query,
            scope=req.scope,
            domain=req.domain,
            domain_bias=req.domain_bias,
            top_k=req.top_k,
            include_neighbors=req.include_neighbors,
            block=req.block,
            include_outdated=req.include_outdated,
            domain_boost=req.domain_boost,
            tier=req.tier,
            as_of=req.as_of,
        )
    )


@app.post("/skill/search")
def skill_search(req: SkillSearchRequest):
    return _as_json(_mcp_skill_search(req.query, top_k=req.top_k))


@app.post("/mem/hybrid-search")
def mem_hybrid_search(req: MemHybridSearchRequest):
    return _as_json(
        _mcp_mem_hybrid_search(
            req.query,
            scope=req.scope,
            domain=req.domain,
            domain_bias=req.domain_bias,
            top_k=req.top_k,
            mode=req.mode,
            fts_limit=req.fts_limit,
            include_neighbors=req.include_neighbors,
            neighbor_limit=req.neighbor_limit,
            block=req.block,
            include_outdated=req.include_outdated,
            tier=req.tier,
            as_of=req.as_of,
        )
    )


@app.post("/mem/ingest")
def mem_ingest(req: MemIngestRequest):
    result = _mcp_mem_ingest(
        req.content,
        type=req.type,
        importance=req.importance,
        domain=req.domain,
        source=req.source,
    )
    # 校验类拒绝（空内容 / 超长）：REST 侧映射为 422，返回友好 message；
    # 其余 stored:false（如事务失败/secret 强拒，不携带 node_id 键）仍按 JSON 原样返回，不改语义。
    try:
        payload = json.loads(result)
    except Exception:  # noqa: BLE001 —— 解析失败以空字典兜底不影响后续校验
        payload = {}
    if not payload.get("stored") and "node_id" in payload and payload.get("node_id") is None:
        return JSONResponse(status_code=422, content={"detail": payload.get("error", "内容校验失败")})
    return _as_json(result)


@app.post("/mem/link")
def mem_link(req: MemLinkRequest):
    return _as_json(
        _mcp_mem_link(
            req.source_id,
            req.target_id,
            relation=req.relation,
            weight=req.weight,
            bidirectional=req.bidirectional,
        )
    )


@app.delete("/mem/edge")
def delete_mem_edge(req: MemEdgeDeleteRequest):
    """删除一条边（issue #51）。

    建边早有 /mem/link，删边一直没有公开途径——冲突检测误标产生的 REVISED_BY
    边只能停机直连库手删。幂等：边不存在时 deleted=False，不报错。
    用 DELETE + body（与 issue 建议一致）；FastAPI 允许 DELETE 带 body。
    """
    label = (req.relation or "").strip().upper() or "RELATED_TO"
    deleted = _get_store().delete_edge(req.source_id, req.target_id, label)
    return {"deleted": deleted, "source_id": req.source_id, "target_id": req.target_id, "relation": label}


@app.post("/graph/neighbors")
def graph_neighbors(req: GraphNeighborsRequest):
    return _as_json(
        _mcp_graph_neighbors(
            req.node_id,
            relation=req.relation,
            depth=req.depth,
            limit=req.limit,
            min_weight=req.min_weight,
            block=req.block,
        )
    )


@app.post("/graph/communities")
def graph_communities(req: GraphCommunitiesRequest):
    return _as_json(
        _mcp_mem_communities(
            min_community_size=req.min_community_size,
            top_k=req.top_k,
            with_summary=req.with_summary,
        )
    )


@app.post("/mem/fact-history")
def mem_fact_history(req: MemFactHistoryRequest):
    """单条事实的时间线（只读）：bi-temporal 时间字段 + 取代关系（双向）。

    核心逻辑见 mcp_tools.memory.mem_fact_history（复用同一实现，不复制）。
    """
    return _as_json(_mcp_mem_fact_history(req.node_id))


@app.post("/mem/recent")
def mem_recent(req: MemRecentRequest):
    """最近记忆列表（只读）：按 created_at 倒序，时间戳缺失时按 id 倒序兜底。

    核心逻辑见 mcp_tools.memory.mem_recent（复用同一实现，不复制）。
    """
    return _as_json(_mcp_mem_recent(domain=req.domain, limit=req.limit, as_of=req.as_of))


@app.post("/mem/stats")
def mem_stats():
    """库级盘点统计（只读）：返回 totals / kinds / importance / time / graph 分节。

    核心逻辑见 core/stats.py compute_stats（单次全遍历，不修改任何节点）。
    """
    from core.stats import compute_stats

    stats = compute_stats(_get_store())
    stats.pop("elapsed_ms", None)
    return stats


@app.get("/tasks/active")
def tasks_active(
    project: str = "",
    states: str = "todo,doing,blocked",
    limit: int = 15,
):
    """活跃任务列表：``type=task`` & ``status=active`` & 状态命中，按优先级排序。

    只读。参数走 query（``?project=X&states=todo,doing&limit=15``）；核心逻辑见
    ``core.task_state.list_active_tasks``，与 MCP 工具 ``tasks_active`` 同一实现。
    默认排除 ``payload.legacy = true`` 的老节点（老节点请先跑 CLI ``tasks backfill``）。
    """
    from core.task_state import list_active_tasks

    return list_active_tasks(_get_store(), project=project, states=states, limit=limit)


# ---------------------------------------------------------------------------
# Lifecycle protocol —— 记忆策略引擎的宿主无关接口
# ---------------------------------------------------------------------------
# 设计：宿主（Hermes / Claude Code / 任意 agent）通过这组端点转发「原始事件」，
# 由本体的策略引擎（core/strategy.py）决定「召不召、抽不抽、记哪层」。
# 宿主适配器只做「钩子注册 + 字段映射 + 结果注入」，不含任何策略。
#
# 与 MCP 的分工：
#   - MCP 工具        = 冷路径，「模型主动想用记忆时怎么用」
#   - lifecycle 协议  = 热路径，「模型没想用记忆时记忆怎么找到模型」
# 两者共享同一策略引擎与存储层。


class PreTurnRequest(BaseModel):
    session_id: str = ""
    turn_index: int = 0
    user_message: str
    recent_context_summary: str = ""
    scope: str = "all"
    domain: str = ""
    top_k: int = 5
    include_neighbors: bool = False
    tier: str = "facts"
    min_score: float = 0.0


class PostTurnRequest(BaseModel):
    session_id: str = ""
    turn_index: int = 0
    user_message: str
    assistant_message: str = ""
    auto_ingest: bool = True


class SessionEndRequest(BaseModel):
    session_id: str = ""
    messages: list[dict] = []
    auto_ingest: bool = True


class PreCompressRequest(BaseModel):
    session_id: str = ""
    messages: list[dict] = []


@app.post("/lifecycle/pre-turn")
def lifecycle_pre_turn(req: PreTurnRequest):
    """每轮模型调用前：本体决定召回什么，返回可注入 prompt 的文本。

    宿主适配器拿到 ``inject_text`` 后原样拼进 prompt（放 system / user 前缀由
    适配器按自己的上下文组装方式决定——策略只管内容与优先级）。
    """
    probe = _strategy_pre_turn(req.user_message, hits=None, top_k=req.top_k, min_score=req.min_score)
    if probe["skip"] and probe["skip_reason"] in ("trivial", "too_short"):
        return probe

    raw = _as_json(
        _mcp_mem_search(
            req.user_message,
            scope=req.scope,
            domain=req.domain,
            top_k=req.top_k,
            include_neighbors=req.include_neighbors,
            tier=req.tier,
        )
    )
    hits = raw.get("results", []) if isinstance(raw, dict) else []
    result = _strategy_pre_turn(req.user_message, hits=hits, top_k=req.top_k, min_score=req.min_score)
    result["session_id"] = req.session_id
    result["turn_index"] = req.turn_index
    return result


@app.post("/lifecycle/post-turn")
def lifecycle_post_turn(req: PostTurnRequest):
    """每轮回复后：本体决定这轮要不要沉淀、写什么、写哪层。"""
    decision = _strategy_post_turn(req.user_message, auto_ingest=req.auto_ingest)
    decision["session_id"] = req.session_id
    decision["turn_index"] = req.turn_index
    if not decision.get("store"):
        return decision

    ingest = _as_json(
        _mcp_mem_ingest(
            decision["content"],
            type=decision["type"],
            importance=decision["importance"],
        )
    )
    decision["stored"] = bool(ingest.get("stored"))
    decision["node_id"] = ingest.get("node_id")
    decision["tier_distribution"] = {decision["tier"]: 1} if ingest.get("stored") else {}
    return decision


@app.post("/lifecycle/session-end")
def lifecycle_session_end(req: SessionEndRequest):
    """会话结束：本体提炼要点、去重、写 facts 层。"""
    decision = _strategy_session_end(req.messages, auto_ingest=req.auto_ingest)
    decision["session_id"] = req.session_id
    if not decision.get("store"):
        return decision

    probe = _as_json(_mcp_mem_search(decision["content"], scope="memory", top_k=1))
    dup_hits = probe.get("results", []) if isinstance(probe, dict) else []
    if _strategy_is_near_duplicate(dup_hits):
        return {
            "store": False,
            "points": decision["points"],
            "stored": False,
            "tier_distribution": {},
            "skip_reason": "near_duplicate",
            "session_id": req.session_id,
        }

    ingest = _as_json(
        _mcp_mem_ingest(
            decision["content"],
            type=decision["type"],
            importance=decision["importance"],
        )
    )
    return {
        "store": True,
        "stored": bool(ingest.get("stored")),
        "node_id": ingest.get("node_id"),
        "points": decision["points"],
        "tier_distribution": {"facts": 1} if ingest.get("stored") else {},
        "session_id": req.session_id,
    }


@app.post("/lifecycle/pre-compress")
def lifecycle_pre_compress(req: PreCompressRequest):
    """压缩前抽取要点，贡献给压缩 prompt（**不写库**，只保上下文）。"""
    return _strategy_pre_compress(req.messages)


class ContextEnhanceRequest(BaseModel):
    messages: list[dict] = []
    focus_topic: str = ""
    domain: str = ""
    protect_first_n: int = 3
    protect_last_n: int = 6
    max_topics: int = 3
    timeout_budget: float = 8.0


@app.post("/lifecycle/context-enhance")
def lifecycle_context_enhance(req: ContextEnhanceRequest):
    """压缩前图谱增强：本体挑主题、进程内查图谱、组装注入文本（**不写库**）。

    宿主（压缩引擎适配器）只提供「将要压缩的消息 + 保护段参数 + 耗时预算」，
    「挑哪些主题、每主题取几条、怎么组装」的判定全在本体（core/strategy.py）。
    与原适配器相比少了 N 次 HTTP 往返——检索在本进程内完成，预算在此统一约束。
    """
    max_topics = _strategy_clamp_max_topics(req.max_topics)
    topics = _strategy_extract_graph_topics(
        req.messages,
        req.focus_topic or None,
        protect_first_n=req.protect_first_n,
        protect_last_n=req.protect_last_n,
        max_topics=max_topics,
    )
    if not topics:
        return {
            "enhancement_text": "",
            "topics": [],
            "hits": 0,
            "decision_log": {"reason": "no_topics"},
        }

    deadline = time.monotonic() + max(0.1, req.timeout_budget)
    budget_exhausted = False
    entries: list[tuple[str, dict, list]] = []
    for topic in topics:
        if time.monotonic() >= deadline:
            budget_exhausted = True
            break
        raw = _as_json(
            _mcp_mem_search(
                topic,
                scope="all",
                domain=req.domain,
                top_k=GRAPH_ENHANCE_TOP_K,
                include_neighbors=True,
            )
        )
        hits = raw.get("results", []) if isinstance(raw, dict) else []
        if not hits:
            continue
        entries.append((topic, hits[0], raw.get("neighbors") or []))
        if len(entries) >= max_topics:
            break

    return {
        "enhancement_text": _strategy_format_graph_enhancement(entries, max_topics=max_topics),
        "topics": topics,
        "hits": len(entries),
        "decision_log": {
            "topics": len(topics),
            "hits": len(entries),
            "budget_exhausted": budget_exhausted,
            "max_topics": max_topics,
        },
    }
