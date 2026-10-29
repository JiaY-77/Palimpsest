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
import contextlib
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import Config
from core import pin as _pin
from core import tdb_text_index as _tdb_text
from core.capsule_guard import validate_capsule_fields
from core.fts_index import sync_node
from core.meta_guard import validate_meta
from core.policy import check_search_query
from core.reporting import generate_report
from core.startup_check import run_startup_check
from core.strategy import (
    GRAPH_ENHANCE_TOP_K,
    SESSION_BUFFER_IMPORTANCE,
    SESSION_BUFFER_STATUS_ACTIVE,
    SESSION_BUFFER_STATUS_CLOSED,
    SESSION_BUFFER_TYPE,
)
from core.strategy import (
    capsule_thickness as _strategy_capsule_thickness,
)
from core.strategy import (
    clamp_max_topics as _strategy_clamp_max_topics,
)
from core.strategy import (
    compose_session_capsule as _strategy_compose_session_capsule,
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
    decide_session_buffer as _strategy_decide_session_buffer,
)
from core.strategy import (
    decide_session_end as _strategy_session_end,
)
from core.strategy import (
    decide_thought_skeleton as _strategy_decide_thought_skeleton,
)
from core.strategy import (
    decide_thought_store as _strategy_decide_thought_store,
)
from core.strategy import (
    extract_graph_topics as _strategy_extract_graph_topics,
)
from core.strategy import (
    extract_handles as _strategy_extract_handles,
)
from core.strategy import (
    format_graph_enhancement as _strategy_format_graph_enhancement,
)
from core.strategy import (
    is_near_duplicate as _strategy_is_near_duplicate,
)
from core.strategy import (
    merge_capsule_with_existing as _strategy_merge_capsule_with_existing,
)
from core.task_state import HANDOFF_KIND, _now_iso, apply_task_patch
from core.trivium_store import DatabaseBusyError, EmbeddingInputTooLongError, EmbeddingUnavailableError, TriviumStore
from core.trivium_store import shutdown as _shutdown_shared_db
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
    mem_import as _mcp_mem_import,
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
    mem_unpack as _mcp_mem_unpack,
)
from mcp_tools import (
    skill_search as _mcp_skill_search,
)
from server.dashboard_api import DashboardBackend as _DashboardBackend
from server.dashboard_api import build_router as _build_dashboard_router

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
        # 断线自愈（孤儿缓冲补产）：常驻服务的后台周期扫描，Config 可关。
        recover_task: asyncio.Task[None] | None = None
        if Config.SESSION_CAPSULE_RECOVER_ENABLED:
            recover_task = asyncio.create_task(_orphan_recovery_loop())
        try:
            yield
        finally:
            if recover_task is not None:
                # cancel + 吞 CancelledError：lifespan 退出的正常路径，不算错误。
                recover_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recover_task
            # ASGI shutdown：进程级共享连接（issue #167 单例长连接）flush 全量
            # 落盘后关闭。flush 失败只记日志、不阻塞退出（WAL 模式下已提交事务
            # 不丢）；atexit 已注册兜底，覆盖脚本/CLI 等非 lifespan 退出路径。
            _shutdown_shared_db()


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

# ---- CORS：供本机浏览器工具直连 REST（本地 HTML 面板、插件前端等）----
# 这类工具多以 file:// 打开，浏览器发出的 Origin 是字面量 "null"（不会算作
# localhost 同源），因此必须显式放行；同时放行酒馆前端（127.0.0.1:8000）。
# 只列本机来源，不开放公网；需要别的来源时用 CORS_ALLOW_ORIGINS 覆盖（逗号分隔）。
_DEFAULT_CORS_ORIGINS = "null,http://127.0.0.1:8000,http://localhost:8000"
CORS_ALLOW_ORIGINS = [
    origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",") if origin.strip()
]
# 注意：Starlette 中后 add 的中间件在外层，预检请求由 CORS 先应答。
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Key 鉴权开关：PALIMPSEST_API_KEY 默认空 = 不启用（localhost 本机直连）。
# 设置后除 / 健康检查外所有请求须带 Bearer 或 X-API-Key，否则 401。
# API Key 仅做校验，不做加密传输；公网部署必须配 HTTPS 反向代理。
API_KEY = Config.API_KEY


@app.exception_handler(EmbeddingInputTooLongError)
async def _embedding_input_too_long_handler(request, exc: EmbeddingInputTooLongError):
    """输入超出 embedding 模型可处理长度 → 422（issue #121）。

    与 EmbeddingUnavailableError（503，服务真挂了）语义拆开：模型侧明确报
    「exceeds the context length」时服务是活的，503 的「检查 Ollama 是否启动」
    会把排查方向带偏。
    """
    logger.warning("检索输入超出 embedding 模型可处理长度: %s", exc)
    return JSONResponse(
        status_code=422,
        content={
            "detail": "输入超出 embedding 模型可处理长度",
            "hint": str(exc),
        },
    )


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
            "/memory/{node_id}/open",
            "/mem/search",
            "/skill/search",
            "/mem/hybrid-search",
            "/mem/ingest",
            "/mem/purge-by-meta",
            "/mem/retag-by-meta",
            "/mem/link",
            "/memoryhub/unpack",
            "/memoryhub/import",
            "/mem/edge",
            "/mem/fact-history",
            "/mem/recent",
            "/tasks/active",
            "/tasks/backfill",
            "/tasks/archive",
            "/fts/rebuild",
            "/fts/search",
            "/mem/review",
            "/mem/promote",
            "/mem/ingest-git",
            "/mem/reindex",
            "/graph/neighbors",
            "/graph/communities",
            "/mem/stats",
            "/mem/consolidate",
            "/doctor",
            "/lifecycle/pre-turn",
            "/lifecycle/post-turn",
            "/lifecycle/session-end",
            "/lifecycle/pre-compress",
            "/lifecycle/context-enhance",
            "/lifecycle/recover-orphan-capsules",
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
        # TDB 文本通道（默认关闭零成本）：删除节点同步清空正文文本
        _tdb_text.sync_node(node_id, "")
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
    # TDB 文本通道（默认关闭零成本；失败 warning 不阻断，可 rebuild 兜底）：
    # 与 FTS 同一份 content——立即读仓库当前正文，update_content 已落库。
    try:
        node = _get_store().get_node(node_id)
        content = ((node or {}).get("payload") or {}).get("content", "")
        _tdb_text.sync_node(node_id, content)
    except Exception as e:  # noqa: BLE001 —— TDB 同步失败仅告警不阻塞节点更新
        logger.warning("TDB 文本索引同步失败 node=%s: %s", node_id, e)


def _apply_payload_update(node_id: int, payload: dict) -> dict:
    """PUT/PATCH 共用的 update 写路径（唯一实现，避免两份逻辑漂移）。

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


@app.put("/memory/{node_id}")
def update_memory_payload(node_id: int, payload: dict):
    """更新指定 ID 的记忆 payload（部分更新合并语义：只改传入字段，其余保留）。

    ``type=task`` 节点走任务写路径（``core.task_state.apply_task_patch``）：状态变化时
    自动补 ``last_touched_at``、写 ``type=record`` 日志节点并建 ``record -[LOGS]-> task``
    边，保证任务状态「变了必留痕」；其余类型节点行为不变。
    """
    return _apply_payload_update(node_id, payload)


@app.patch("/memory/{node_id}")
def patch_memory_payload(node_id: int, payload: dict):
    """PATCH：部分更新指定 ID 的记忆 payload，与 PUT 走**同一条写路径**。

    ``type=task`` 节点同样经过 ``apply_task_patch``（状态变化留痕：补
    ``last_touched_at``、写日志节点、建 ``LOGS`` 边）——本端点历史上曾
    直连 ``store.update_payload``，绕过任务写路径导致状态无痕变更，
    现已与 PUT 合并到 ``_apply_payload_update``。响应字段与 PUT 完全一致。
    """
    return _apply_payload_update(node_id, payload)


# 胶囊接手留痕（issue #208）：opened_by 最多记多少个接手会话。留足够追溯线索
# 又不让 payload 无界膨胀；超限丢最老（保留最近接手的会话）。
CAPSULE_OPENED_BY_LIMIT = 10


class CapsuleOpenRequest(BaseModel):
    """POST /memory/{id}/open 请求体：接手会话的 session_id。

    ``None`` / 缺省 / 空串一律按「未知会话」处理（宿主侧拿不到 id 也得能留痕）。
    """

    session_id: str | None = None


def _open_capsule(node_id: int, session_id: str) -> dict:
    """记录「胶囊被会话接手」（issue #208）：opened_at / open_count / opened_by。

    为什么不走 PUT/PATCH 的任务写路径：``apply_task_patch`` 会补
    ``last_touched_at``——那会让胶囊看起来「刚动过」，污染新鲜度判据
    （``fresh_days`` 折叠会把刚被接手的老胶囊误判成活跃）。接手留痕只需要
    三个 payload 字段，直连 ``store.update_payload`` 合并，content /
    ``last_touched_at`` 零改动；FTS/TDB 索引的是 content，本次不碰 content，
    也无需同步索引。

    语义：
    - ``opened_at`` 只在首次写入，之后**永不刷新**——「首次被接手」是事实，
      不随后续接手变化（payload 里已有值就保留，含手工预置的历史值）；
    - ``open_count`` 只在**新**会话首调时 +1（与 ``opened_by`` 去重同判据）；
    - ``opened_by`` 按首次接手顺序去重，超 :data:`CAPSULE_OPENED_BY_LIMIT`
      丢最老；
    - 幂等：同一 ``session_id`` 重复调用不重复计数；空串 ``session_id``
      按「未知会话」走同一套去重 / 计数逻辑（空名会话），不做特判分支。
    """
    store = _get_store()
    node = store.get_node(node_id)
    if node is None:
        # 404 + 固定提示（不泄漏内部异常，安全审计要求；与 PUT 端点同一口径）
        raise HTTPException(status_code=404, detail="更新失败：节点不存在或数据格式错误")
    payload = node.get("payload") or {}
    opened_by = [str(sid) for sid in (payload.get("opened_by") or [])]
    open_count = int(payload.get("open_count") or 0)
    if session_id not in opened_by:
        # 新会话首调：计数 +1 并登记（append 后截尾 = 丢最老）
        open_count += 1
        opened_by = [*opened_by, session_id][-CAPSULE_OPENED_BY_LIMIT:]
    update = {
        # opened_at 只在首次写：payload 里已有（含手工预置的历史值）就不覆盖
        "opened_at": payload.get("opened_at") or _now_iso(),
        "open_count": open_count,
        "opened_by": opened_by,
    }
    store.update_payload(node_id, update)
    return {"status": "ok", **update}


@app.post("/memory/{node_id}/open")
def open_memory_node(node_id: int, req: CapsuleOpenRequest | None = None):
    """记录胶囊被当前会话接手（issue #208）。

    胶囊（``type=task`` + ``kind=handoff`` 的会话交接节点）被新会话接手时调用：
    首次写 ``opened_at``，每个**新**会话首调 ``open_count`` +1，``opened_by``
    记接手过的 session_id（去重、按首次顺序、上限 10、超限丢最老）。
    同一 ``session_id`` 重复调用幂等不重复计数。不动 content /
    ``last_touched_at``——那两处一个是要紧正文、一个是新鲜度判据，接手不该
    碰（检索命中计数 ``hit_count`` / ``last_hit_at`` 是 ``mem_search`` 自动
    +1 的另一回事，与本端点的「接手」语义互不代替）。
    """
    body = req or CapsuleOpenRequest()
    return _open_capsule(node_id, (body.session_id or "").strip())


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
    # 显式取代：逗号分隔的旧节点 id。取代默认不自动发生，仅显式声明才标 outdated。
    replaces: str = ""
    # 可选自定义元数据（如 {"chat_id": "..."}）：结构校验见 core.meta_guard，
    # 通过后原样写入 payload 的 meta 子字典；不传 / 空字典时 payload 不含 meta 键。
    meta: dict[str, str] | None = None
    # 可选手写胶囊结构字段：形状与自动胶囊（core.strategy.compose_session_capsule）
    # 一致，校验见 core.capsule_guard（pending 未传时自动算空节）；不传时
    # payload 不含这两个键，行为与旧版完全一致。
    sections: dict[str, str] | None = None
    pending: list[str] | None = None


class MemPurgeByMetaRequest(BaseModel):
    # 按「域名 + 自定义元数据」批量删除（酒馆记忆桥：用户删聊天 → 同步清记忆）。
    # domain 是防误删的硬闸门：缺失或空串一律 400，无法针对全库执行。
    domain: str = ""
    meta_key: str
    meta_value: str
    dry_run: bool = True  # 默认干跑：只预览不删除
    max_delete: int = 50  # 单次真实删除上限：匹配数超过即 409 拒绝执行


class MemRetagByMetaRequest(BaseModel):
    # 按「域名 + 自定义元数据」批量改写 meta 值（酒馆记忆桥：用户重命名聊天 →
    # 同步改标，防止记忆按旧 chat_id 检索不到而变孤儿）。
    # domain 是防误改的硬闸门：RP_MODE 关闭时缺失 / 空串一律 400。
    # 必填项给空串默认值：缺失统一走端点自己的 400 护栏（而非 422），错误信息
    # 能说明缺的是哪个字段，与 purge-by-meta 的护栏口径对齐。
    domain: str = ""
    meta_key: str = ""
    old_value: str = ""
    new_value: str = ""
    dry_run: bool = True  # 默认干跑：只预览不改写
    max_update: int = 500  # 单次真实改写上限：匹配数超过即 409 拒绝执行


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


class MemConsolidateRequest(BaseModel):
    threshold: float = 0.85
    max_importance: float = 0.8
    # apply=true 才执行合并；缺省保持既有「只读预览」语义（dry-run 不推进 generation / 不写库）
    apply: bool = False


class MemPromoteRequest(BaseModel):
    apply: bool = False
    days: int = 30
    min_hits: int = 5


class MemReviewRequest(BaseModel):
    days: int = 7
    domain: str = ""
    tier: str = "facts"


class MemIngestGitRequest(BaseModel):
    repo: str = ""  # 空 = 服务端项目根
    since: int = 7


class FtsSearchRequest(BaseModel):
    query: str
    limit: int = 10


class TaskBackfillRequest(BaseModel):
    apply: bool = False


class TaskArchiveRequest(BaseModel):
    apply: bool = False
    knowledge_dir: str = ""  # 空 = 服务端 KNOWLEDGE_DIR 约定位置


class MemReindexRequest(BaseModel):
    check: bool = False
    dry_run: bool = False
    restart: bool = False
    force: bool = False
    resume: bool = True
    only: str = ""
    skip: str = ""
    batch: int = 64
    yes: bool = False


class MemoryHubUnpackRequest(BaseModel):
    path: str
    preview_chars: int = 80


class MemoryHubImportRequest(BaseModel):
    path: str
    select: str = ""
    include_secrets: bool = False


def _as_json(text: str):
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 —— JSON 解析失败回退原始文本保持 JSON 结构稳定
        return {"raw": text}


def _query_limit_rejected(query: str) -> bool:
    """检索入口 query 上限校验（issue #121）：超长 → 422，不去打 embedding。

    阈值只从 policy/Config 取（MEM_SEARCH_MAX_QUERY_CHARS），不在端点写魔数。
    """
    result = check_search_query(query)
    if result["ok"]:
        return False
    raise HTTPException(status_code=422, detail=result["error"])


def _resolve_domain(requested: str) -> str:
    """解析 domain 参数：RP_MODE 关闭时原样返回（旧行为不变）；开启时锁死 RP_DOMAIN。

    RP 专用实例（config.RP_MODE=1）的域规则：
      - 不传 / 空串 → 自动补 RP_DOMAIN（RP 实例下没有「跨域」语义，一切落 RP 域）；
      - 传了别的 domain → 400 拒绝——域锁死，防止技术记忆误写进 RP 库，
        也防止 RP 实例被当成通用记忆库查询旧域数据。
    Config 的值在调用时读取（不在 import 期固化），切换 RP_MODE 无需重建应用。
    """
    if not Config.RP_MODE:
        return requested
    req_domain = (requested or "").strip()
    if req_domain and req_domain.lower() != Config.RP_DOMAIN.lower():
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    f"RP 专用实例：域已锁死为 {Config.RP_DOMAIN!r}，不接收其他 domain（请求传入 {req_domain!r}）"
                ),
                "requested_domain": req_domain,
                "allowed_domain": Config.RP_DOMAIN,
            },
        )
    return Config.RP_DOMAIN


@app.post("/mem/search")
def mem_search(req: MemSearchRequest):
    _query_limit_rejected(req.query)
    return _as_json(
        _mcp_mem_search(
            req.query,
            scope=req.scope,
            domain=_resolve_domain(req.domain),
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
    _query_limit_rejected(req.query)
    return _as_json(
        _mcp_mem_hybrid_search(
            req.query,
            scope=req.scope,
            domain=_resolve_domain(req.domain),
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
    # meta 结构校验（core.meta_guard）：不合法直接 422，不去打 embedding。
    if req.meta is not None:
        meta_ok, meta_err = validate_meta(req.meta)
        if not meta_ok:
            raise HTTPException(status_code=422, detail=meta_err)
    # 手写胶囊结构校验（core.capsule_guard）：不合法直接 422，不去打 embedding。
    # 裁决仍在 MCP 写入路径（转调时原样透传，由同一校验函数再兜一道），这里
    # 提前拦只为把拒绝映射成 422——stored:false 响应不带 node_id 键，下方
    # 的 422 兜底不会触发，与 meta 校验的既有范式同构。
    if req.sections is not None or req.pending is not None:
        cap_ok, cap_err, _ = validate_capsule_fields(req.sections, req.pending)
        if not cap_ok:
            raise HTTPException(status_code=422, detail=cap_err)
    result = _mcp_mem_ingest(
        req.content,
        type=req.type,
        importance=req.importance,
        domain=_resolve_domain(req.domain),
        source=req.source,
        replaces=req.replaces,
        meta=req.meta,
        sections=req.sections,
        pending=req.pending,
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


@app.post("/mem/purge-by-meta")
def mem_purge_by_meta(req: MemPurgeByMetaRequest):
    """按「域名 + 自定义元数据」精确批量删除记忆节点。

    外置宿主（酒馆记忆桥）在用户删除聊天记录时，需要把该聊天的记忆一并
    清空：写入端在 ingest 时打 ``meta`` 标（如 ``{"chat_id": "..."}``），
    删除端在这里按「``payload.domain == domain`` 且 ``payload.meta[meta_key]
    == meta_value``」匹配后逐个删除（走 store.delete_node，级联清理关联边
    与 FTS/TDB 文本通道）。

    破坏性操作的三道护栏：
      1. ``domain`` 必填非空（RP_MODE 关闭时，缺失 / 空串 400）——不允许针对
         全库删除；RP_MODE 开启时空 domain 自动补 RP_DOMAIN（域锁死语义）；
      2. ``dry_run`` 默认 true——缺省只预览（matched_count / ids / previews），
         不删任何节点；
      3. 匹配数 > ``max_delete`` 时 409 拒绝执行（附实际匹配数 + 预览），
         防止一次删爆。
    每次真实删除写一条 ``type=record`` 审计节点（domain 同解析后的请求域、
    importance=0.5、content 含 meta 键值 / deleted_ids / 时间戳）；审计节点
    本身不带 meta，不会被后续同条件的 purge 连坐删除。
    """
    if not Config.RP_MODE and not (req.domain or "").strip():
        raise HTTPException(status_code=400, detail="domain 必填且非空：本端点不允许针对全库删除")
    domain = _resolve_domain(req.domain)
    if not req.meta_key or not req.meta_value:
        raise HTTPException(status_code=400, detail="meta_key 与 meta_value 必填且非空")

    # 先物化匹配结果再写库：iter_payloads 的生成器遍历期间占用连接，遍历中
    # 写入必报 Database locked（先 list() 物化后再批量删）。
    matched: list[tuple[int, str]] = []
    for node_id, payload in _get_store().iter_payloads():
        if payload.get("domain") == domain and (payload.get("meta") or {}).get(req.meta_key) == req.meta_value:
            matched.append((int(node_id), (payload.get("content") or "")[:80]))
    matched.sort()
    matched_ids = [nid for nid, _ in matched]
    previews = [snippet for _, snippet in matched]

    if req.dry_run:
        return {"dry_run": True, "matched_count": len(matched_ids), "ids": matched_ids, "previews": previews}

    if len(matched_ids) > req.max_delete:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"匹配 {len(matched_ids)} 个节点，超过 max_delete={req.max_delete}，拒绝执行",
                "matched_count": len(matched_ids),
                "ids": matched_ids,
                "previews": previews,
            },
        )

    deleted_ids: list[int] = []
    failed_ids: list[int] = []
    store = _get_store()
    for node_id in matched_ids:
        try:
            store.delete_node(node_id)
            deleted_ids.append(node_id)
        except Exception as e:  # noqa: BLE001 —— 单条失败不中断（记入 failed 继续）
            logger.warning("purge-by-meta 删除节点 %s 失败: %s", node_id, e)
            failed_ids.append(node_id)

    audit_node_id = _write_purge_audit_record(domain, req, deleted_ids, failed_ids)
    return {
        "dry_run": False,
        "matched_count": len(matched_ids),
        "deleted_count": len(deleted_ids),
        "deleted_ids": deleted_ids,
        "failed": failed_ids,
        "audit_node_id": audit_node_id,
    }


def _write_purge_audit_record(
    domain: str, req: MemPurgeByMetaRequest, deleted_ids: list[int], failed_ids: list[int]
) -> int | None:
    """真实删除后写一条 type=record 审计节点，返回其 node_id（写不出则 None）。

    审计节点不带 meta——自身不会被同条件的 purge 连坐删除。审计失败不回滚
    删除（节点已删、不可逆），仅审计链断档。
    """
    content = (
        f"[purge-by-meta] domain={domain} meta.{req.meta_key}={req.meta_value} "
        f"deleted_ids={deleted_ids} failed={failed_ids} "
        f"at={datetime.now(timezone.utc).isoformat()}"
    )
    result = _mcp_mem_ingest(content, type="record", importance=0.5, domain=domain)
    try:
        payload = json.loads(result)
    except Exception:  # noqa: BLE001 —— 解析失败按审计未写入处理
        payload = {}
    return payload.get("node_id") if payload.get("stored") else None


def _write_retag_audit_record(
    domain: str,
    req: MemRetagByMetaRequest,
    updated_ids: list[int],
    failed_ids: list[int],
) -> int | None:
    """真实改写后写一条 type=record 审计节点，返回其 node_id（写不出则 None）。

    审计节点不带 meta（见 purge 审计的同理由），改写失败的部分随 failed 记录。
    """
    content = (
        f"[retag-by-meta] domain={domain} meta.{req.meta_key}={req.old_value}"
        f"->{req.new_value} updated_ids={updated_ids} failed={failed_ids} "
        f"at={datetime.now(timezone.utc).isoformat()}"
    )
    result = _mcp_mem_ingest(content, type="record", importance=0.5, domain=domain)
    try:
        payload = json.loads(result)
    except Exception:  # noqa: BLE001 —— 解析失败按审计未写入处理
        payload = {}
    return payload.get("node_id") if payload.get("stored") else None


@app.post("/mem/retag-by-meta")
def mem_retag_by_meta(req: MemRetagByMetaRequest):
    """按「域名 + 自定义元数据」批量改写 meta 值（不打向量、不重建索引）。

    外置宿主（酒馆记忆桥）在用户重命名聊天后调用：写入端 ingest 时打了
    ``{"chat_id": "..."}`` 标，重命名后把旧值统一改写为新值，同步改标。
    匹配口径与 purge-by-meta 完全一致（``payload.domain == domain`` 且
    ``payload.meta[meta_key] == old_value``）。

    破坏性/误改防护的四道护栏：
      1. ``domain`` 必填非空（RP_MODE 关闭时，缺失 / 空串 400）——不允许针对
         全库改标；RP_MODE 开启时空 domain 自动补 RP_DOMAIN；
      2. ``old_value == new_value`` → 400（无改写意义，防误触）；
      3. ``dry_run`` 默认 true——缺省只预览（matched_count / ids / previews），
         不改任何节点；
      4. 匹配数 > ``max_update`` 时 409 拒绝执行（附实际匹配数 + 预览）。

    meta 是合并式改写：先读回节点现有 meta 再浅合并
    ``{**cur_meta, meta_key: new_value}``，其他 meta 键一律不冲掉；改写目标
    {meta_key: new_value} 过 core.meta_guard 结构校验（非法键名 / 超长 → 400）。
    每次真实改写写一条 ``type=record`` 审计节点（domain 同解析后的请求域、
    importance=0.5、content 含 meta 键 / 新旧值 / updated_ids / 时间戳）。
    """
    if not Config.RP_MODE and not (req.domain or "").strip():
        raise HTTPException(status_code=400, detail="domain 必填且非空：本端点不允许针对全库改标")
    domain = _resolve_domain(req.domain)
    if not req.meta_key or not req.old_value or not req.new_value:
        raise HTTPException(status_code=400, detail="meta_key、old_value 与 new_value 必填且非空")
    if req.old_value == req.new_value:
        raise HTTPException(status_code=400, detail="old_value 与 new_value 相同：无改写意义")
    meta_ok, meta_err = validate_meta({req.meta_key: req.new_value})
    if not meta_ok:
        raise HTTPException(status_code=400, detail=f"meta 结构不合法：{meta_err}")

    # 先物化匹配结果再写库（同 purge：遍历期间占用连接，遍历中写入必报
    # Database locked）。
    matched: list[tuple[int, str]] = []
    for node_id, payload in _get_store().iter_payloads():
        if payload.get("domain") == domain and (payload.get("meta") or {}).get(req.meta_key) == req.old_value:
            matched.append((int(node_id), (payload.get("content") or "")[:80]))
    matched.sort()
    matched_ids = [nid for nid, _ in matched]
    previews = [snippet for _, snippet in matched]

    if req.dry_run:
        return {
            "dry_run": True,
            "matched_count": len(matched_ids),
            "updated_count": 0,
            "updated_ids": [],
            "failed": [],
            "ids": matched_ids,
            "previews": previews,
        }

    if len(matched_ids) > req.max_update:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"匹配 {len(matched_ids)} 个节点，超过 max_update={req.max_update}，拒绝执行",
                "matched_count": len(matched_ids),
                "ids": matched_ids,
                "previews": previews,
            },
        )

    updated_ids: list[int] = []
    failed_ids: list[int] = []
    store = _get_store()
    for node_id in matched_ids:
        try:
            # 改写前读回节点复核匹配态 + 合并 meta：更新间可能穿插其他写请求
            # （FastAPI 线程池），条件复核防「匹配过但 meta 已被改写」的重叠失效。
            node = store.get_node(node_id)
            payload = (node or {}).get("payload") or {}
            cur_meta = payload.get("meta") or {}
            if payload.get("domain") != domain or cur_meta.get(req.meta_key) != req.old_value:
                continue  # 复核失败不计 failed（节点本就不再匹配）
            store.update_payload(node_id, {"meta": {**cur_meta, req.meta_key: req.new_value}})
            updated_ids.append(node_id)
        except Exception as e:  # noqa: BLE001 —— 单条失败不中断（记入 failed 继续）
            logger.warning("retag-by-meta 改写节点 %s 失败: %s", node_id, e)
            failed_ids.append(node_id)

    audit_node_id = _write_retag_audit_record(domain, req, updated_ids, failed_ids)
    return {
        "dry_run": False,
        "matched_count": len(matched_ids),
        "updated_count": len(updated_ids),
        "updated_ids": updated_ids,
        "failed": failed_ids,
        "audit_node_id": audit_node_id,
    }


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


@app.post("/memoryhub/unpack")
def memoryhub_unpack(req: MemoryHubUnpackRequest):
    return _as_json(_mcp_mem_unpack(req.path, preview_chars=req.preview_chars))


@app.post("/memoryhub/import")
def memoryhub_import(req: MemoryHubImportRequest):
    return _as_json(_mcp_mem_import(req.path, select=req.select, include_secrets=req.include_secrets))


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
    return _as_json(_mcp_mem_recent(domain=_resolve_domain(req.domain), limit=req.limit, as_of=req.as_of))


@app.post("/mem/stats")
def mem_stats():
    """库级盘点统计（只读）：返回 totals / kinds / importance / time / graph 分节。

    核心逻辑见 core/stats.py compute_stats（单次全遍历，不修改任何节点）。
    """
    from core.stats import compute_stats

    stats = compute_stats(_get_store())
    stats.pop("elapsed_ms", None)
    return stats


# ---------------------------------------------------------------------------
# Dashboard（融合进 REST：一个进程就把面板带上，不用另起 8010）
#
# 路由本体在 `server/dashboard_api.py`（一份实现，独立进程也能挂）。这里只注入
# **直读版**数据源——REST 不能 HTTP 调自己（多占一个线程、平白多一跳，线程池紧张
# 时还可能自锁），所以四个数据源全部走进程内的 store / 既有工具函数。
# ---------------------------------------------------------------------------


def _dashboard_scan_nodes() -> list[dict]:
    """全库节点（带 payload）——面板的冲突链 / 遗忘曲线 / 图谱都吃这一份。"""
    return [{"id": nid, "payload": pl} for nid, pl in _get_store().iter_payloads() if isinstance(pl, dict)]


def _dashboard_stats() -> dict:
    from core.stats import compute_stats

    return compute_stats(_get_store())


def _dashboard_search(query: str, limit: int) -> dict:
    return _as_json(_mcp_mem_search(query, top_k=limit))


def _dashboard_fact_history(node_id: int) -> dict:
    return _as_json(_mcp_mem_fact_history(node_id))


def _dashboard_consolidate_preview() -> dict:
    """合并候选预览（dry-run）——REST 的 ``/mem/consolidate`` 写死 dry_run=True。"""
    from core.consolidator import consolidate

    return consolidate(_get_store(), dry_run=True)


_DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")

app.include_router(
    _build_dashboard_router(
        _DashboardBackend(
            scan_nodes=_dashboard_scan_nodes,
            stats=_dashboard_stats,
            search=_dashboard_search,
            fact_history=_dashboard_fact_history,
            consolidate_preview=_dashboard_consolidate_preview,
        ),
        page_route="/dashboard",
        page_path=_DASHBOARD_HTML,
    )
)


@app.post("/mem/consolidate")
def mem_consolidate(req: MemConsolidateRequest):
    """合并候选报告。dry-run（缺省）只读预览；``apply=true`` 才真正执行合并。

    核心逻辑见 core.consolidator.consolidate。
    """
    from core.consolidator import consolidate

    return consolidate(
        _get_store(),
        dry_run=not req.apply,
        sim_threshold=req.threshold,
        max_importance=req.max_importance,
    )


@app.post("/mem/promote")
def mem_promote(req: MemPromoteRequest):
    """高频记忆自动升级。dry-run（缺省）只预览候选；``apply=true`` 才升权打标。"""
    from core.promoter import promote

    return promote(
        _get_store(),
        dry_run=not req.apply,
        days=req.days,
        min_hits=req.min_hits,
    )


@app.post("/tasks/backfill")
def tasks_backfill(req: TaskBackfillRequest):
    """存量 task 节点回填 task_state。dry-run（缺省）只读预览；``apply=true`` 才写库。"""
    from core.task_state import backfill_task_state

    return backfill_task_state(_get_store(), dry_run=not req.apply)


@app.post("/tasks/archive")
def tasks_archive(req: TaskArchiveRequest):
    """已完成任务节点归档到知识库。dry-run（缺省）只预览；``apply=true`` 才写文件+删节点。

    knowledge_dir 空串 = 服务端 ``KNOWLEDGE_DIR``（core.task_archive 自行解析）。
    """
    from core.task_archive import archive_tasks

    return archive_tasks(
        _get_store(),
        dry_run=not req.apply,
        knowledge_dir=req.knowledge_dir or None,
    )


@app.post("/mem/review")
def mem_review(req: MemReviewRequest):
    """复盘盘点（近 N 天记忆 + 治理候选 + 全库 decision 计数）。

    复用 ``mcp_tools.memory.mem_review`` 的同一实现（_mem_review_impl），
    外加经同一份服务端 store 统计 ``type=decision`` 节点数——与 CLI 旧行为一致。
    """
    import json

    from mcp_tools.memory import _mem_review_impl

    data = json.loads(_mem_review_impl(_get_store(), days=req.days, domain=req.domain, tier=req.tier))
    decision_count = 0
    for _nid, payload in _get_store().iter_payloads():
        if isinstance(payload, dict) and payload.get("type") == "decision":
            decision_count += 1
    stats = data.get("stats", {})
    stats["decision"] = decision_count
    data["stats"] = stats
    return data


@app.post("/mem/ingest-git")
def mem_ingest_git(req: MemIngestGitRequest):
    """将 git commit 入库为 ``git_commit`` 节点（幂等）。git log 在服务端执行。

    repo 空串 = 服务端项目根（``scripts._common.PROJECT_ROOT``）。
    """
    from core.git_ingest import ingest_git_commits

    repo = req.repo
    if not repo:
        from scripts._common import PROJECT_ROOT as _PROJECT_ROOT

        repo = _PROJECT_ROOT
    return ingest_git_commits(_get_store(), repo, req.since)


@app.post("/fts/rebuild")
def fts_rebuild_endpoint():
    """全量重建 FTS5 全文索引（服务端执行，返回索引节点数）。"""
    from core.fts_index import rebuild as fts_rebuild_impl

    return {"indexed": fts_rebuild_impl(_get_store())}


@app.post("/fts/search")
def fts_search_endpoint(req: FtsSearchRequest):
    """FTS5 全文搜索（trigram 中文子串匹配）。只读，不在服务端拿写锁。"""
    from core.fts_index import search_fts

    return {"results": search_fts(req.query, limit=req.limit)}


@app.post("/mem/reindex")
def mem_reindex(req: MemReindexRequest):
    """全库向量重嵌入（换 embedding 模型后使用）。

    复用 ``scripts.reindex`` 的既有实现（含指纹比对 / 断点进度 / 退出码语义），
    stdout 捕获进 ``output`` 返回。``check`` / ``dry_run`` 路径不写库；
    非交互环境未传 ``yes`` 时交互确认会拒绝执行（与脚本旧行为一致）。
    """
    import contextlib
    import io

    from scripts import reindex as reindex_mod

    store = _get_store()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if req.check:
            code = reindex_mod.cmd_check(store)
        else:
            code = reindex_mod.cmd_reindex(
                store,
                only=[s.strip() for s in req.only.split(",") if s.strip()] or None,
                skip=[s.strip() for s in req.skip.split(",") if s.strip()] or None,
                batch=req.batch,
                restart=req.restart,
                dry_run=req.dry_run,
                yes=req.yes,
                force=req.force,
            )
    return {"exit_code": code, "output": buf.getvalue()}


@app.get("/doctor")
def doctor():
    """只读体检。

    已知说明：run_doctor() 内部会走一次 run_startup_check()，后者会自己开一次库连接；
    在同一进程内串行调用是安全的（store 是「每操作开-关库」不常驻），
    但若将来 _get_store() 改成常驻持锁，必须重新评估。
    """
    from core.doctor import run_doctor

    return run_doctor()


@app.get("/tasks/active")
def tasks_active(
    project: str = "",
    states: str = "todo,doing,blocked",
    limit: int = 15,
    include_handoff: bool = False,
    fresh_days: int = 30,
):
    """活跃任务列表：``type=task`` & ``status=active`` & 状态命中，按优先级排序。

    只读。参数走 query（``?project=X&states=todo,doing&limit=15&include_handoff=true``）；
    核心逻辑见 ``core.task_state.list_active_tasks``，与 MCP 工具 ``tasks_active``
    同一实现。默认排除 ``payload.legacy = true`` 的老节点（老节点请先跑 CLI
    ``tasks backfill``）；``include_handoff=true`` 才列出会话接续点
    （``kind=handoff`` 的跨会话交接记录，而非待办事项；显式列出时不受状态
    过滤限制——接续点没有「待办状态」语义，``task_state`` 为 None 也能取回，
    且置顶按 ``importance`` 倒序）；``fresh_days`` 是新鲜度窗口（默认 30 天）：
    超期未动的条目折叠进 ``stale`` 汇总、不再逐条返回，传 ``0`` 展开全部。
    """
    from core.task_state import list_active_tasks

    return list_active_tasks(
        _get_store(),
        project=project,
        states=states,
        limit=limit,
        include_handoff=include_handoff,
        fresh_days=fresh_days,
    )


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
    # 思维预设层给侧开关（宿主自己另有预设时可关掉本体的思考骨架）。
    thought_skeleton: bool = True


class PostTurnRequest(BaseModel):
    session_id: str = ""
    # 轮次序号：显式 >0 → 同序号重复提交幂等覆盖（防重）；<=0（宿主未提供真实
    # 序号，含老宿主缺省 0）→ 服务端按缓冲 last_turn_index+1 追加，绝不把 0 当
    # 「同一轮」（issue #216：每轮恒 0 互相覆盖曾让缓冲 turns 恒为 1）。
    turn_index: int = 0
    user_message: str
    assistant_message: str = ""
    auto_ingest: bool = True
    # 思维预设层事实字段：本轮给侧注入的思考骨架类型（"" / code / research /
    # default）。宿主不传 → 空串（老宿主兼容）；白名单外的值落库时归一为空串。
    skeleton_kind: str = ""
    # 本轮思考底片的域归属（issue #123，可选）：宿主不传 → 空串（老宿主行为
    # 不变，隐式 general）；传了则落进 payload.domain，宿主可按自己的域检索
    # 底片（否则固定 domain=hermes 的宿主永远查不到自己的底片）。
    domain: str = ""


class SessionEndRequest(BaseModel):
    session_id: str = ""
    messages: list[dict] = []
    auto_ingest: bool = True


class PreCompressRequest(BaseModel):
    session_id: str = ""
    messages: list[dict] = []


# ---------------------------------------------------------------------------
# 常驻域注入（issue #176）：某 domain 的 active 节点**每轮无条件**拼在语义召回
# 之前。语义召回是 relevance-gated 的——与当前消息无关的常驻规则永远进不了注入，
# 这里补上「不参与相似度竞争」的第三条路。配置 `PIN_DOMAIN` 为空即完全关闭。
# ---------------------------------------------------------------------------


def _pin_entries() -> list[tuple[int, dict]]:
    """取常驻域候选节点。TQL 快路径优先，失败/不可用时退化为全遍历（不漏判）。"""
    domain = (getattr(Config, "PIN_DOMAIN", "") or "").strip()
    if not domain:
        return []
    store = _get_store()
    db = None
    try:
        if re.fullmatch(r"[A-Za-z0-9_:-]+", domain):
            db = store._acquire()
            rows: list[tuple[int, dict]] = []
            for r in db.tql(f'FIND {{domain: "{domain}"}} RETURN *'):
                nd = r.row.get("_", {})
                nid = nd.get("id")
                if nid is not None:
                    rows.append((int(nid), nd.get("payload", {}) or {}))
            return rows
    except DatabaseBusyError:
        raise
    except Exception as e:  # noqa: BLE001 —— TQL 失败退化为全遍历保证不漏判
        logger.warning("常驻域 TQL 查询失败，退化为 iter_payloads: %s", e)
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()
    return [(nid, pl) for nid, pl in store.iter_payloads()]


def _collect_pin() -> dict:
    """收集并拼装常驻块；**fail-open**——任何异常都退化为「无常驻块」，绝不拖垮整轮。"""
    domain = (getattr(Config, "PIN_DOMAIN", "") or "").strip()
    empty = {"text": "", "node_count": 0, "chars": 0, "truncated": False, "dropped": 0, "ids": []}
    if not domain:
        return empty
    try:
        nodes = _pin.select_pin_nodes(_pin_entries(), domain=domain)
        return _pin.compose_pin_block(
            nodes,
            domain=domain,
            budget_chars=int(getattr(Config, "PIN_BUDGET_CHARS", _pin.PIN_DEFAULT_BUDGET_CHARS)),
        )
    except DatabaseBusyError:
        raise
    except Exception as e:  # noqa: BLE001 —— 常驻注入失败跳过即可，召回照常
        logger.warning("常驻域注入失败（已跳过，不影响语义召回）: %s", e)
        return empty


def _attach_pin(probe: dict, pin_block: dict) -> dict:
    """把常驻块拼到注入文本**最前面**（优先级高于语义召回），并写诊断字段。"""
    probe["pin"] = _pin.pin_diagnostics(pin_block)
    text = str(pin_block.get("text") or "")
    if not text:
        return probe
    existing = str(probe.get("inject_text") or "")
    probe["inject_text"] = f"{text}\n\n{existing}" if existing else text
    blocks = list(probe.get("inject_blocks") or [])
    blocks.insert(0, {"content": text, "priority": 1.0, "source": "pin"})
    probe["inject_blocks"] = blocks
    return probe


@app.post("/lifecycle/pre-turn")
def lifecycle_pre_turn(req: PreTurnRequest):
    """每轮模型调用前：本体决定召回什么，返回可注入 prompt 的文本。

    宿主适配器拿到 ``inject_text`` 后原样拼进 prompt（放 system / user 前缀由
    适配器按自己的上下文组装方式决定——策略只管内容与优先级）。

    另返回 ``thought_skeleton`` / ``thought_skeleton_kind``（思维预设层给侧）：
    按当前轮情境动态挑的思考骨架，宿主原样拼进 prompt 即可引导模型把结构化
    思考写进正文（post-turn 会抽取落库）；闲聊/过短轮骨架为空串。骨架判定
    全在本体（core/strategy.py），宿主零判定。
    """
    skeleton = _strategy_decide_thought_skeleton(req.user_message, enabled=req.thought_skeleton)
    # 常驻块与 query 无关：每轮都取（trivial / 超长 query 也一样——「无条件」就是无条件）
    pin_block = _collect_pin()
    probe = _strategy_pre_turn(req.user_message, hits=None, top_k=req.top_k, min_score=req.min_score)
    # issue #124：早退分支同样回显请求的 session_id / turn_index（此前只在正常分支回显）。
    probe["session_id"] = req.session_id
    probe["turn_index"] = req.turn_index
    if probe["skip"] and probe["skip_reason"] in ("trivial", "too_short"):
        probe["thought_skeleton"] = skeleton["skeleton"]
        probe["thought_skeleton_kind"] = skeleton["kind"]
        return _attach_pin(probe, pin_block)

    # issue #121：内部召回那一路同样受 query 上限约束——fail-open 但可观测。
    # 超长 query 不送 embedding（模型侧会报 exceeds the context length 并被
    # 误译成「服务不可用」），跳过召回但明确标 skip_reason，不静默吞。
    qcheck = check_search_query(req.user_message)
    if not qcheck["ok"]:
        probe["skip"] = True
        probe["skip_reason"] = "query_too_long"
        probe["inject_text"] = ""
        probe["inject_blocks"] = []
        probe["decision_log"] = {"reason": "query_too_long", "detail": qcheck["error"]}
        probe["thought_skeleton"] = skeleton["skeleton"]
        probe["thought_skeleton_kind"] = skeleton["kind"]
        return _attach_pin(probe, pin_block)

    raw = _as_json(
        _mcp_mem_search(
            req.user_message,
            scope=req.scope,
            domain=_resolve_domain(req.domain),
            top_k=req.top_k,
            include_neighbors=req.include_neighbors,
            tier=req.tier,
        )
    )
    hits = raw.get("results", []) if isinstance(raw, dict) else []
    # 去重：常驻块已经无条件注入过的节点，不再在语义召回里重复出现（省 token）
    pinned_ids = set(pin_block.get("ids") or [])
    if pinned_ids:
        hits = [h for h in hits if h.get("id") not in pinned_ids]
    result = _strategy_pre_turn(req.user_message, hits=hits, top_k=req.top_k, min_score=req.min_score)
    result["session_id"] = req.session_id
    result["turn_index"] = req.turn_index
    result["thought_skeleton"] = skeleton["skeleton"]
    result["thought_skeleton_kind"] = skeleton["kind"]
    return _attach_pin(result, pin_block)


def _thought_already_stored(source: str, turn_index: int) -> bool:
    """同一 lifecycle 轮次的 thought 查重（issue #125）。

    判据：既有 ``type="thought"`` 节点 payload 的 ``source`` 与 ``turn_index``
    都与本轮相同。判定**在库内**——跨进程 / 重启窗口依然有效，不依赖内存集合。

    双路径（照 ``mcp_tools.memory.mem_recent`` 的既有写法）：TQL
    ``FIND {source: ...}`` 快路径优先；source 含可疑字符（防 TQL 注入）或 TQL
    失败时退化为 ``iter_payloads`` 全遍历过滤。``DatabaseBusyError`` 不吞——
    fail-fast，与写路径的占用语义一致。
    """
    store = _get_store()
    rows: list[tuple[int, dict]] = []
    db = None
    try:
        if re.fullmatch(r"[A-Za-z0-9_:-]+", source or ""):
            db = store._acquire()
            for r in db.tql(f'FIND {{source: "{source}"}} RETURN *'):
                nd = r.row.get("_", {})
                nid = nd.get("id")
                if nid is not None:
                    rows.append((nid, nd.get("payload", {}) or {}))
        else:
            rows = [(nid, pl) for nid, pl in store.iter_payloads()]
    except DatabaseBusyError:
        raise
    except Exception as e:  # noqa: BLE001 —— TQL 失败退化为全遍历保证不漏判
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()
            db = None
        logger.warning("thought 查重 TQL 失败，退化为 iter_payloads: %s", e)
        rows = [(nid, pl) for nid, pl in store.iter_payloads()]
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()
    return any(
        pl.get("type") == "thought" and (pl.get("source") or "") == source and pl.get("turn_index") == turn_index
        for _nid, pl in rows
    )


# ---------------------------------------------------------------------------
# 会话缓冲（「骨架即胶囊」第二/三刀的执行层）：post-turn 把每轮编号思考解析成
# 字段，经实质轮筛选（is_substantive_turn，issue #170）后累进该 session 的缓冲
# 节点（type="session"，同一 session_id 只一条）；session-end 由
# compose_session_capsule 合并成 type=task + kind=handoff 胶囊。
# 判定全在 core/strategy.py（parse_thought_sections / is_substantive_turn /
# decide_session_buffer / compose_session_capsule 纯函数），这里只做落库执行。
# ---------------------------------------------------------------------------

# 会话胶囊节点的固定档位（任务书口径）：接续点形态的任务卡，写进 hermes 域。
SESSION_CAPSULE_TYPE = "task"
SESSION_CAPSULE_IMPORTANCE = 0.85
SESSION_CAPSULE_DOMAIN = "hermes"
SESSION_CAPSULE_EDGE_RELATION = "CONSOLIDATED_INTO"  # 缓冲节点 → 胶囊节点

# 厚度门槛（issue #219，口径主人定的，先用常量不加 env 配置项）：胶囊只在会话
# 真干了活时才值得产。薄会话（一两句闲聊）合成出的四节几乎全「（待补）」，只会
# 污染「新会话先读」的列表。
CAPSULE_MIN_SUBSTANTIVE_TURNS = 2
CAPSULE_MIN_FILLED_SECTIONS = 2


def _find_active_session_buffer(session_id: str) -> tuple[int, dict] | None:
    """按 payload 定位该 session 的**活跃**缓冲节点。

    判据：``type=session`` + ``payload.session_id`` 相等 + ``status=active``。
    缓冲按 payload 字段定位（不走向量 / FTS 检索），单用户库 iter_payloads
    全遍历足够；中途 session-end（issue #187）后同 session 可能有多条缓冲，
    这里只要**活跃**那条（追加轮次的目标），全部缓冲的合并在胶囊落地侧
    （``_find_session_buffers``）。
    """
    for nid, payload in _get_store().iter_payloads():
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("type") == SESSION_BUFFER_TYPE
            and payload.get("session_id") == session_id
            and payload.get("status") == SESSION_BUFFER_STATUS_ACTIVE
        ):
            return nid, payload
    return None


def _append_session_buffer_turn(session_id: str, turn_index: int, buf: dict) -> tuple[int, int]:
    """把本轮解析出的 sections 累进该 session 的缓冲节点，返回 ``(节点 id, 解析序号)``。

    - 无缓冲节点 → 新建（``store.insert_node`` 一次原子写入，payload 一次成形）：
      不走 ``mem_ingest``——它的固定入参写不了 ``payload.turns`` 这类结构化字段，
      且机器簿记不该参与冲突检测 / 记忆检索池。
    - 已有 → 追加本轮 turn；**显式非 0 序号同序重复提交幂等覆盖**（原位替换，
      不追加第二条）。``turn_index <= 0``（宿主未提供真实序号，老宿主恒 0）不当
      「同一轮」：按 ``last_turn_index + 1`` 追加（新建缓冲从 1 起）——否则每轮
      恒 0 互相覆盖，缓冲只剩最后一轮（issue #216，全库实测 turns 恒为 1）。
    - ``last_turn_index`` 单调不回退（显式低序号重试不把游标拉回旧值，兜底
      追加的序号才不会与存量轮次相撞）。
    - ``started_at`` 记缓冲创建时刻（首个 turn 的落库时刻，数值）；更新时保留。
    - ``updated_at`` 记最后活动时刻（新建 = started_at，每次追加都刷新）：孤儿
      恢复扫描（``_recover_orphan_session_buffers``）靠它判断缓冲静默多久——
      此前 payload 只有 started_at、追加也不刷新任何时间戳，断线自愈无从判定。
    - 解析序号随 ``(nid, index)`` 一并返回（issue #219）：``turn_index <= 0`` 的
      宿主在请求里本来就没有真序号，端点要把落库用的真实序号回传，插件重载后才
      能从缓冲离点续接，而不是从 1 重计覆盖第 1 轮。
    """
    store = _get_store()
    existing = _find_active_session_buffer(session_id)
    index = int(turn_index)
    if index <= 0:
        # 宿主未提供真实序号 → 解析出追加序号（新建缓冲从 1 起），不拿 0 当同一轮。
        index = (int(existing[1].get("last_turn_index") or 0) + 1) if existing else 1
    turn_entry = {
        "turn_index": index,
        "skeleton_kind": str(buf.get("skeleton_kind") or ""),
        "sections": dict(buf.get("sections") or {}),
        "raw": str(buf.get("raw") or ""),
    }
    if existing is None:
        now = time.time()
        content = f"[会话缓冲] session={session_id}（进行中，session-end 合并为会话胶囊）"
        vector, embed_audit = store.embed_text_with_audit(content)  # issue #232
        nid = store.insert_node(
            {
                "type": SESSION_BUFFER_TYPE,
                "content": content,
                "importance": SESSION_BUFFER_IMPORTANCE,
                "session_id": session_id,
                "status": SESSION_BUFFER_STATUS_ACTIVE,
                "started_at": now,
                "updated_at": now,  # 与 started_at 同值：新建这一刻即最后一次活动
                "turns": [turn_entry],
                "last_turn_index": index,
            },
            vector,
            embedding_audit=embed_audit,
        )
        return nid, index
    nid, payload = existing
    prev_last = int(payload.get("last_turn_index") or 0)
    turns = [t for t in (payload.get("turns") or []) if isinstance(t, dict)]
    for i, turn in enumerate(turns):
        if turn.get("turn_index") == index:
            turns[i] = turn_entry  # 幂等：同轮重复提交覆盖，不追加第二条
            break
    else:
        turns.append(turn_entry)
    store.update_payload(nid, {"turns": turns, "last_turn_index": max(prev_last, index), "updated_at": time.time()})
    _sync_fts_after_update(nid)
    return nid, index


@app.post("/lifecycle/post-turn")
def lifecycle_post_turn(req: PostTurnRequest):
    """每轮回复后：本体决定这轮要不要沉淀、写什么、写哪层；并抽取思考底片。

    两条独立的落库路径，互不阻断（判定都在 core/strategy.py，端点只执行）：

    - 用户原话强信号 → logs 层（type="record"，既有行为）；
    - assistant 消息里的 ``<thought>`` 段 → logs 层（type="thought"，思维底片）；
      不命中 / 未闭合 / 空段 / 过短时**不落库但可观测**（thought.skip_reason
      区分各情形），入库失败同理（ingest_failed + error 透传），不静默吞。

    ``tier_distribution`` 汇总本轮**全部**写入的分层计数（此前只数用户原话
    一条；混合轮两条都计入，thought-only 轮也能从顶层看到真实写入）。

    来源标记（写进 thought 节点 payload，随 ``thought`` 分节可观测）：
    ``skeleton_kind`` 为宿主上报的**事实字段**（本轮注入的骨架类型，缺省空串）；
    ``thought_origin`` 为本体按文本开头形态的**启发式**判定（skeleton / native /
    unknown，非精确归因）——两者交叉可查「注入了骨架但模型没照写」。

    会话缓冲（第三条路径，issue #170 实质轮筛选）：``<thought>`` 编号答案解析后
    经 :func:`core.strategy.is_substantive_turn` 判实质——工具调用 / 长思考含
    结论字段 / 用户消息指令动词，任一满足才累进缓冲；短轮不进缓冲但可观测
    （``session_buffer.reason="non_substantive"``，不静默丢弃）。
    """
    decision = _strategy_post_turn(req.user_message, auto_ingest=req.auto_ingest)
    decision["session_id"] = req.session_id
    decision["turn_index"] = req.turn_index
    domain = _resolve_domain(req.domain)

    tier_dist: dict[str, int] = {}
    if decision.get("store"):
        ingest = _as_json(
            _mcp_mem_ingest(
                decision["content"],
                type=decision["type"],
                importance=decision["importance"],
                domain=domain,
            )
        )
        decision["stored"] = bool(ingest.get("stored"))
        decision["node_id"] = ingest.get("node_id")
        if ingest.get("stored"):
            tier_dist[decision["tier"]] = tier_dist.get(decision["tier"], 0) + 1

    # 思维底片：assistant 消息里的 <thought> 段 → type="thought" / logs 层。
    # 与用户原话路径互不阻断：任一路径的入库失败不影响另一条落库。
    thought = _strategy_decide_thought_store(
        req.assistant_message,
        auto_ingest=req.auto_ingest,
        session_id=req.session_id,
        skeleton_kind=req.skeleton_kind,
    )
    thought_out: dict = {
        "found": thought["found"],
        "stored": False,
        "node_id": None,
        "skip_reason": thought["skip_reason"],
        "type": thought["type"],
        "tier": thought["tier"],
        "importance": thought["importance"],
        "skeleton_kind": thought["skeleton_kind"],
        "thought_origin": thought["thought_origin"],
        "decision_log": thought["decision_log"],
    }
    if thought["store"]:
        # issue #125：同 (session, turn_index) 的重复提交不落第二条 thought。
        # turn_index==0（宿主未提供真实序号）不判重——与「宿主不传字段 = 老
        # 行为不变」的兼容约定一致，避免把退化序号误吞成重复。
        if req.turn_index > 0 and _thought_already_stored(thought["source"], req.turn_index):
            thought_out["stored"] = False
            thought_out["skip_reason"] = "duplicate"
        else:
            tingest = _as_json(
                _mcp_mem_ingest(
                    thought["content"],
                    type=thought["type"],
                    importance=thought["importance"],
                    source=thought["source"],
                    skeleton_kind=thought["skeleton_kind"],
                    turn_index=req.turn_index,
                    domain=domain,
                )
            )
            thought_out["stored"] = bool(tingest.get("stored"))
            thought_out["node_id"] = tingest.get("node_id")
            if not tingest.get("stored"):
                thought_out["skip_reason"] = "ingest_failed"
                if tingest.get("error"):
                    thought_out["error"] = tingest["error"]
            else:
                tier_dist[thought["tier"]] = tier_dist.get(thought["tier"], 0) + 1
    decision["thought"] = thought_out
    decision["tier_distribution"] = tier_dist

    # 会话缓冲（第三条独立路径，「骨架即胶囊」）：解析编号思考 → 实质轮筛选
    # （issue #170）→ 累进缓冲节点。实质判定在 strategy.is_substantive_turn
    # （工具调用 / 长思考含结论字段 / 指令动词，任一满足即实质）；阈值从
    # Config 注入（环境变量可调），端点只取值与执行。与前两条落库路径互不
    # 阻断：独立 try/except，失败透传 session_buffer.error。
    session_buffer_out: dict = {"buffered": False, "node_id": None, "reason": "", "error": None}
    try:
        buf = _strategy_decide_session_buffer(
            req.assistant_message,
            skeleton_kind=req.skeleton_kind,
            session_id=req.session_id,
            turn_index=req.turn_index,
            user_message=req.user_message,
            min_thought_chars=Config.SESSION_BUFFER_MIN_THOUGHT_CHARS,
        )
        session_buffer_out["reason"] = buf["reason"]
        if buf["buffer"]:
            buf_nid, resolved = _append_session_buffer_turn(req.session_id, req.turn_index, buf)
            session_buffer_out["node_id"] = buf_nid
            session_buffer_out["buffered"] = True
            session_buffer_out["turn_index_resolved"] = resolved
    except Exception as e:  # noqa: BLE001 —— 缓冲失败绝不拖垮既有两条落库路径
        logger.warning("session 缓冲累计失败 session=%s turn=%s: %s", req.session_id, req.turn_index, e)
        session_buffer_out["error"] = str(e)
    decision["session_buffer"] = session_buffer_out
    # 解析后的真实序号（issue #219）：缓冲路径里**落库用**的那个序号。
    # 加字段、不改 ``turn_index``（那仍是请求值，老消费者零感知）——宿主不发
    # 序号（恒 0）时它在请求里本来就没有真序号，只有这里能学到该接几号。
    decision["turn_index_resolved"] = session_buffer_out.get("turn_index_resolved", req.turn_index)
    return decision


def _find_session_buffers(session_id: str) -> list[tuple[int, dict]]:
    """定位该 session 的**全部**缓冲节点（active + closed，按节点 id 升序）。

    issue #187：session-end 可能在会话中途发生过一次（缓冲被 closed），之后又
    有实质轮 → ``_append_session_buffer_turn`` 找不到活跃缓冲会另建新缓冲。
    合成胶囊必须取**全部**缓冲的轮次，否则中途 closed 那批轮会被丢出胶囊；
    节点 id 升序即创建顺序，供轮次合并保持稳定。
    """
    out = [
        (nid, payload)
        for nid, payload in _get_store().iter_payloads()
        if isinstance(payload, dict)
        and payload.get("type") == SESSION_BUFFER_TYPE
        and payload.get("session_id") == session_id
    ]
    out.sort(key=lambda item: item[0])
    return out


def _find_session_capsule(session_id: str) -> tuple[int, dict] | None:
    """定位该 session 已有的会话胶囊（issue #187「先查后建」）。

    判据：``type=task`` + ``payload.kind="handoff"`` + ``payload.session_id`` 相等。
    查的是**本链路自己产出的**胶囊——kind / session_id 是本链路显式写入的
    payload 字段，必然齐全，比 content 首行判据更精确（首行判据是给手写接续点
    的形态兜底，两者不混用）。
    """
    for nid, payload in _get_store().iter_payloads():
        if (
            isinstance(payload, dict)
            and payload.get("type") == SESSION_CAPSULE_TYPE
            and payload.get("kind") == HANDOFF_KIND
            and payload.get("session_id") == session_id
        ):
            return nid, payload
    return None


def _merge_session_turns(buffers: list[tuple[int, dict]]) -> list[dict]:
    """合并该 session 全部缓冲里的 turns（按轮次升序，跨缓冲逐字去重）。

    去重键是整条轮次（turn_index + skeleton_kind + sections + raw）的逐字相等：
    同一轮同内容在多个缓冲里重复出现只留一条；**同 turn_index 不同内容的轮两
    条都留**——宿主可能不传真实序号（恒 0），按 turn_index 判重会误吞不同的
    轮，宁可重复不可丢轮。排序稳定，turn_index 相同时保持缓冲创建顺序。
    """
    seen: dict[tuple, dict] = {}
    for _nid, payload in buffers:
        for turn in payload.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            key = (
                turn.get("turn_index"),
                turn.get("skeleton_kind"),
                json.dumps(turn.get("sections") or {}, sort_keys=True, ensure_ascii=False),
                turn.get("raw"),
            )
            if key not in seen:
                seen[key] = turn
    turns = list(seen.values())
    turns.sort(key=lambda t: t.get("turn_index") if isinstance(t.get("turn_index"), int) else 0)
    return turns


def _finalize_session_capsule(session_id: str) -> dict | None:
    """会话胶囊落地（session-end 路径）：全部缓冲 → 合成 → 写胶囊 + 关缓冲 + 建边。

    - 无缓冲（老宿主 / 空 session_id）→ ``None``；缓冲全部 closed 且胶囊已存在
      → 同样 ``None``（重复 session-end 幂等空操作：不重写、不新建、返回体不加
      ``capsule`` 字段，与改动前一致）。
    - 有活跃缓冲（或缓冲在而胶囊缺失——历史写入失败残留的自愈）→ 合成。轮次取
      **全部缓冲**合并（issue #187：中途 session-end 把缓冲切成了多条，只取活跃
      那条会把前半段轮丢出胶囊）；该 session 已有胶囊 → **原节点更新**（正文重
      合成 + payload 覆盖，node_id 不变），没有 → 才新建。
    - 新建走 ``mem_ingest`` **单次写**（issue #198）：sections / pending 随入参
      一次落库，消掉「先建节点再补写结构化字段」两步之间留下半状态胶囊的窗口；
      ``turns`` / ``session_id`` 这类 mem_ingest 固定入参不支持的字段随后补写，
      失败只记日志（正文与四节此时已可用）。更新路径不走 mem_ingest（它只会
      新建节点）：``update_content``（同步 FTS / TDB，向量标 stale）+ 尽力重嵌 +
      ``update_payload``，node_id 稳定是硬要求。
    - handles（可核验句柄，M2）：各轮 sections 值 + raw 混拼后经
      :func:`core.strategy.extract_handles` 抽取，经 compose 拼进 outcome 节。
    - **全部**缓冲标 ``closed`` + 建 ``CONSOLIDATED_INTO`` 边（幂等：已建的边不
      重建）——失败只记日志不阻断（胶囊正文此时已可用）。

    胶囊写入失败 → 抛出（调用方 catch 后只记日志，不影响旧要点路径）。
    """
    store = _get_store()
    buffers = _find_session_buffers(session_id)
    if not buffers:
        return None
    has_active = any(payload.get("status") == SESSION_BUFFER_STATUS_ACTIVE for _nid, payload in buffers)
    existing = _find_session_capsule(session_id)
    if not has_active and existing is not None:
        return None  # 已合并过且无新轮：重复 session-end 幂等空操作
    turns = _merge_session_turns(buffers)
    # 句柄文本源：各轮 sections 的值 + thought 原文（raw）混拼（M2 任务书口径）。
    handle_blobs: list[str] = []
    for turn in turns:
        sections = turn.get("sections")
        if isinstance(sections, dict):
            handle_blobs.extend(value for value in sections.values() if isinstance(value, str))
        raw = turn.get("raw")
        if isinstance(raw, str) and raw:
            handle_blobs.append(raw)
    composed = _strategy_compose_session_capsule(
        turns, session_id=session_id, handles=_strategy_extract_handles(handle_blobs)
    )
    if existing is not None:
        # 更新路径的正文与四节**按行合并**，不覆盖（2026-10-28 实测）：胶囊里可能
        # 含人补写的内容（收口时按接续点口径补的 outcome / next / pitfalls），它们
        # 不在缓冲 turns 里、机器合成不出来；一次 1 轮缓冲的 session-end 或断线自愈
        # 补产就会把 1761 字手写接续点静默盖掉。规则见 merge_capsule_with_existing。
        composed = _strategy_merge_capsule_with_existing(composed, existing[1].get("sections"), session_id=session_id)
    # 厚度门槛（issue #219）：只判这次**要写出的**合成结果（含与既有胶囊合并后
    # 的四节——既有正文是真内容时不该被薄缓冲拖成跳过）。门槛不过 → **不创建也
    # 不更新胶囊节点**；但必须把缓冲关掉（status=closed）：否则断线自愈扫描器
    # （``_recover_orphan_session_buffers``）会认为它还悬空，每 5 分钟反复重试，
    # 永远产不出胶囊、日志刷屏。要点/摘要路径不在这里，完全不受影响。
    thickness = _strategy_capsule_thickness(turns, composed["sections"])
    if (
        thickness["substantive_turns"] < CAPSULE_MIN_SUBSTANTIVE_TURNS
        or thickness["filled_sections"] < CAPSULE_MIN_FILLED_SECTIONS
    ):
        for buf_nid, _buf_payload in buffers:
            try:
                store.update_payload(buf_nid, {"status": SESSION_BUFFER_STATUS_CLOSED})
            except Exception as e:  # noqa: BLE001 —— 关缓冲失败只记日志不阻断
                logger.warning("薄会话缓冲关闭失败 buffer=%s: %s", buf_nid, e)
        logger.info(
            "会话胶囊跳过（太薄）session=%s substantive_turns=%s filled_sections=%s",
            session_id,
            thickness["substantive_turns"],
            thickness["filled_sections"],
        )
        return {
            "skipped": "too_thin",
            "substantive_turns": thickness["substantive_turns"],
            "filled_sections": thickness["filled_sections"],
        }
    structured = {
        "kind": HANDOFF_KIND,  # 显式写进 payload（_is_handoff 首行判据之外的显式路）
        "session_id": session_id,
        "sections": composed["sections"],
        "pending": composed["pending"],
        "turns": composed["turns"],
    }
    if existing is not None:
        # 更新路径（issue #187）：node_id 不变，正文重合成 + payload 覆盖。
        # 收口时打 closed_at（#208 配套）：一场会话的接受到此为止。新建路径
        # 刻意不写——刚产出的胶囊此刻还没有任何会话接手过它，写 closed_at
        # 会自相矛盾（opened_at 为空却有 closed_at）。
        structured["closed_at"] = _now_iso()
        capsule_id = existing[0]
        store.update_content(capsule_id, composed["content"])  # 内部同步 FTS / TDB，向量标 stale
        try:
            store.reembed_node(capsule_id)  # 尽力重嵌：失败保持 stale，由 reembed 通道兜底
        except Exception as e:  # noqa: BLE001 —— 重嵌失败不阻断胶囊更新
            logger.warning("胶囊重嵌失败 capsule=%s: %s", capsule_id, e)
        try:
            store.update_payload(capsule_id, structured)
        except Exception as e:  # noqa: BLE001 —— 结构化字段覆盖失败不影响正文已更新
            logger.warning("胶囊 payload 覆盖失败 capsule=%s: %s", capsule_id, e)
    else:
        # 新建路径（issue #198 单次写）：sections / pending 随 mem_ingest 一次落库。
        ingest = _as_json(
            _mcp_mem_ingest(
                composed["content"],
                type=SESSION_CAPSULE_TYPE,
                importance=SESSION_CAPSULE_IMPORTANCE,
                domain=SESSION_CAPSULE_DOMAIN,
                sections=composed["sections"],
                pending=composed["pending"],
            )
        )
        if not (isinstance(ingest, dict) and ingest.get("stored") and ingest.get("node_id") is not None):
            error = (
                str(ingest.get("error") or "capsule_ingest_failed")
                if isinstance(ingest, dict)
                else "capsule_ingest_failed"
            )
            raise RuntimeError(f"会话胶囊写入失败: {error}")
        capsule_id = int(ingest["node_id"])
        try:
            store.update_payload(capsule_id, structured)
        except Exception as e:  # noqa: BLE001 —— 簿记字段补写失败不影响胶囊正文/四节可用
            logger.warning("胶囊 payload 补写失败 capsule=%s: %s", capsule_id, e)
    for buf_nid, _buf_payload in buffers:
        try:
            store.update_payload(buf_nid, {"status": SESSION_BUFFER_STATUS_CLOSED})
            # 幂等建边：更新路径下老缓冲的边第一次 session-end 就建过了，不重建。
            linked = {(getattr(e, "label", ""), getattr(e, "target_id", None)) for e in store.get_edges(buf_nid)}
            if (SESSION_CAPSULE_EDGE_RELATION, capsule_id) not in linked:
                store.create_edge(buf_nid, capsule_id, SESSION_CAPSULE_EDGE_RELATION)
        except Exception as e:  # noqa: BLE001 —— 关缓冲 / 建边失败只记日志不阻断
            logger.warning("缓冲关闭/建边失败 buffer=%s capsule=%s: %s", buf_nid, capsule_id, e)
    return {
        "node_id": capsule_id,
        "sections": composed["sections"],
        "pending": composed["pending"],
        "turns": composed["turns"],
    }


def _session_buffer_activity_ts(payload: dict) -> float:
    """缓冲的最后活动时间（epoch 秒）：updated_at 优先，缺失回退 started_at，都缺失返回 0.0（视为最老，可补）。

    0.0 兜底的取向：连 started_at 都没有的缓冲只可能是更老的数据形态或脏数据，
    宁可多补不可漏补——补产不丢数据（session-end 语义幂等），漏补才真悬空。
    """
    for key in ("updated_at", "started_at"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _recover_orphan_session_buffers(*, dry_run: bool = False, stale_after: float | None = None) -> dict:
    """孤儿会话缓冲扫描与补产：进程硬砍（session-end 没跑）后把悬空缓冲补成胶囊。

    为什么需要它：胶囊只在 session-end 落地，post-turn 累进的缓冲若赶上进程被硬砍
    （计划内关机 / 崩溃）就永远 status=active 悬空，胶囊不产。这里把「静默超过
    stale_after 秒」的活跃缓冲按 session 逐个调 ``_finalize_session_capsule`` 补产
    ——该函数幂等（无缓冲 / 已合并过且无新轮都返回 None），晚到的正常 session-end
    与补产结果天然合流，不丢轮次。

    - ``dry_run=True``：一个字节都不写库，只报告将要处理哪些 session（recovered
      条目 node_id=None、turns 为合并轮数预览）。
    - 单 session 补产失败只记 warning 并计入 failed，绝不中断其余 session——
      单点失败不能拖垮整批。
    - 返回键名固定（端点与测试断言依赖）：ok / dry_run / stale_after / scanned /
      recovered / skipped / failed；``scanned`` 为本轮扫到的 type=session 缓冲
      节点总数（含 active / closed）。
    """
    threshold = float(Config.SESSION_CAPSULE_RECOVER_AFTER_SECONDS) if stale_after is None else stale_after
    store = _get_store()
    now = time.time()
    scanned = 0
    stale_by_session: dict[str, tuple[int, dict]] = {}
    for nid, payload in store.iter_payloads():
        if not isinstance(payload, dict) or payload.get("type") != SESSION_BUFFER_TYPE:
            continue
        scanned += 1
        if payload.get("status") != SESSION_BUFFER_STATUS_ACTIVE:
            continue
        if now - _session_buffer_activity_ts(payload) < threshold:
            continue
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            continue  # 无主缓冲没有合并归属（写入侧不会产生），防御脏数据
        # 同一 session 理论只一条 active；多条时留活动时间最新的一条占位即可——
        # 合并走 _find_session_buffers 取全部缓冲，轮次不会丢。
        prev = stale_by_session.get(session_id)
        if prev is None or _session_buffer_activity_ts(payload) > _session_buffer_activity_ts(prev[1]):
            stale_by_session[session_id] = (nid, payload)

    result: dict = {
        "ok": True,
        "dry_run": dry_run,
        "stale_after": threshold,
        "scanned": scanned,
        "recovered": [],
        "skipped": [],
        "failed": [],
    }
    for session_id in stale_by_session:
        if dry_run:
            merged = _merge_session_turns(_find_session_buffers(session_id))
            result["recovered"].append({"session_id": session_id, "node_id": None, "turns": len(merged)})
            continue
        try:
            capsule = _finalize_session_capsule(session_id)
        except Exception as e:  # noqa: BLE001 —— 单 session 失败不拖垮整批
            logger.warning("孤儿会话缓冲补产失败 session=%s: %s", session_id, e)
            result["failed"].append({"session_id": session_id, "error": str(e)})
            continue
        if capsule is None:
            # 无缓冲 / 已合并过且无新轮的幂等空操作
            result["skipped"].append(session_id)
            continue
        if isinstance(capsule, dict) and capsule.get("skipped"):
            # 厚度门槛跳过（issue #219）：``_finalize_session_capsule`` 已经把
            # 缓冲关掉了——下一轮扫描不会再命中这条（status≠active），不会
            # 每 5 分钟反复刷 skip。带 session 与厚度数字留在 skipped 里可观测。
            result["skipped"].append(
                {
                    "session_id": session_id,
                    "skipped": capsule["skipped"],
                    "substantive_turns": capsule.get("substantive_turns"),
                    "filled_sections": capsule.get("filled_sections"),
                }
            )
            continue
        capsule_id = int(capsule["node_id"])
        try:
            # 留痕：这条胶囊是断线补产的，不是正常 session-end 产的——检索侧据此
            # 能区分「半截产物」，排查时不用猜它是怎么来的。
            store.update_payload(capsule_id, {"recovered": True, "recovered_at": _now_iso()})
        except Exception as e:  # noqa: BLE001 —— 留痕失败不影响胶囊已产出（尽力而为）
            logger.warning("孤儿补产留痕失败 capsule=%s: %s", capsule_id, e)
        logger.info("孤儿会话缓冲已补产成胶囊 session=%s capsule=%s turns=%s", session_id, capsule_id, capsule["turns"])
        result["recovered"].append({"session_id": session_id, "node_id": capsule_id, "turns": capsule["turns"]})
    return result


async def _orphan_recovery_loop() -> None:
    """后台周期扫描：服务常驻时的断线自愈（Config.SESSION_CAPSULE_RECOVER_ENABLED 才启动）。

    - 启动先 sleep 5 秒再首轮扫描：等 store 懒加载就绪，也避免与启动后的首批
      请求挤在同一时刻做全库扫描。
    - 同步扫描经 to_thread 下放，不阻塞事件循环（同 ``_startup_self_check``）。
    - 单轮异常只记 warning 继续循环，绝不退出——自愈循环自己挂了，孤儿缓冲就
      永远没人补了。CancelledError 不在 Exception 体系内，cancel 照常穿透。
    """
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(_recover_orphan_session_buffers)
        except Exception as e:  # noqa: BLE001 —— 扫描失败只记日志，下轮重试
            logger.warning("孤儿会话缓冲周期扫描失败（下轮重试）: %s", e)
        await asyncio.sleep(Config.SESSION_CAPSULE_RECOVER_INTERVAL_SECONDS)


@app.post("/lifecycle/session-end")
def lifecycle_session_end(req: SessionEndRequest):
    """会话结束：要点路径（type=memory，既有行为）+ 会话胶囊路径（kind=handoff）。

    两条独立路径，互不阻断：

    - 旧「会话要点」：messages 强信号提炼 → type=memory / facts 层。由
      ``Config.SESSION_MEMORY_INGEST`` 开关控制，**默认开 = 行为完全不变**；
      关闭后本路径可观测地跳过（skip_reason=session_memory_disabled）。
    - 会话胶囊（新增）：session_id 命中活跃缓冲节点 → compose_session_capsule
      → type=task + kind=handoff 胶囊 + 缓冲节点 closed + CONSOLIDATED_INTO 边。
      返回体新增 ``capsule: {node_id, sections, pending, turns}``——仅在胶囊
      **实际写出**时携带；无缓冲（老宿主 / 空 session_id）→ 不加该字段，
      既有返回体逐字段不变。
    - 太薄的会话（issue #219 厚度门槛）不产胶囊：返回体 ``capsule_skipped`` 带
      原因与两个厚度数字（``{"skipped": "too_thin", ...}``），不静默；缓冲
      **仍然关闭**（否则自愈扫描器会永远重试）。
    """
    decision = _strategy_session_end(req.messages, auto_ingest=req.auto_ingest)
    decision["session_id"] = req.session_id

    # 胶囊路径（新增）：与要点路径互不阻断，失败只记日志（capsule 字段缺席）。
    capsule_out: dict | None = None
    capsule_skipped_out: dict | None = None
    if req.session_id:
        try:
            capsule_out = _finalize_session_capsule(req.session_id)
        except Exception as e:  # noqa: BLE001 —— 胶囊失败不阻断旧要点路径
            logger.warning("会话胶囊生成失败 session=%s: %s", req.session_id, e)
        # 厚度门槛跳过（issue #219）不伪装成「胶囊已产出」：把 skip 返回体单独
        # 透传 ``capsule_skipped``，``capsule`` 仍只在真写出时携带。
        if isinstance(capsule_out, dict) and capsule_out.get("skipped"):
            capsule_skipped_out = capsule_out
            capsule_out = None
        if capsule_skipped_out is not None:
            decision["capsule_skipped"] = capsule_skipped_out
        if capsule_out is not None:
            decision["capsule"] = capsule_out

    if not decision.get("store"):
        return decision

    if not Config.SESSION_MEMORY_INGEST:
        # 兼容开关关闭：旧要点路径停用（胶囊路径不受影响），可观测地跳过。
        decision["store"] = False
        decision["skip_reason"] = "session_memory_disabled"
        return decision

    probe = _as_json(_mcp_mem_search(decision["content"], scope="memory", top_k=1))
    dup_hits = probe.get("results", []) if isinstance(probe, dict) else []
    if _strategy_is_near_duplicate(dup_hits):
        out = {
            "store": False,
            "points": decision["points"],
            "stored": False,
            "tier_distribution": {},
            "skip_reason": "near_duplicate",
            "session_id": req.session_id,
        }
        if capsule_out is not None:
            out["capsule"] = capsule_out
        return out

    ingest = _as_json(
        _mcp_mem_ingest(
            decision["content"],
            type=decision["type"],
            importance=decision["importance"],
            domain=_resolve_domain(""),
        )
    )
    out = {
        "store": True,
        "stored": bool(ingest.get("stored")),
        "node_id": ingest.get("node_id"),
        "points": decision["points"],
        "tier_distribution": {"facts": 1} if ingest.get("stored") else {},
        "session_id": req.session_id,
    }
    if capsule_out is not None:
        out["capsule"] = capsule_out
    return out


@app.post("/lifecycle/pre-compress")
def lifecycle_pre_compress(req: PreCompressRequest):
    """压缩前抽取要点，贡献给压缩 prompt（**不写库**，只保上下文）。"""
    return _strategy_pre_compress(req.messages)


class RecoverCapsulesRequest(BaseModel):
    """POST /lifecycle/recover-orphan-capsules 请求体：dry_run 只报告不写库。"""

    dry_run: bool = False


@app.post("/lifecycle/recover-orphan-capsules")
def lifecycle_recover_orphan_capsules(req: RecoverCapsulesRequest) -> dict:
    """手动触发孤儿缓冲补产（验收与人工补救；与后台循环共用同一套扫描实现）。

    同步 def：FastAPI 自动放线程池，不阻塞事件循环（与其他 lifecycle 端点一致）。
    """
    return _recover_orphan_session_buffers(dry_run=req.dry_run)


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
