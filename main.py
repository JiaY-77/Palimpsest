"""
Palimpsest — FastAPI 主入口
提供记忆提取、检索、导入、导出的完整 API 服务
"""

import logging

import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import Config
from core.fts_index import sync_node
from core.startup_check import run_startup_check
from core.trivium_store import EmbeddingUnavailableError, TriviumStore
from core.reporting import generate_report
from core.version import get_version

logger = logging.getLogger(__name__)

app = FastAPI(title="Palimpsest")

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
        provided = auth[len("Bearer "):].strip()
    else:
        provided = request.headers.get("x-api-key", "").strip()

    if not provided or not secrets.compare_digest(provided, API_KEY):
        return JSONResponse(status_code=401, content={"detail": "未授权：缺少或无效的 API Key"})

    return await call_next(request)


@app.on_event("startup")
async def _startup_self_check():
    """启动自检（工程护栏）：只记录，不阻断 —— 避免自检失败拖垮服务可用性。

    结果可通过 CLI 子命令 `palimpsest_cli.py startup-check` 手动触发查看完整明细。
    """
    import json

    result = run_startup_check()
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

# ---- 全局服务实例（启动时初始化一次） ----
store = TriviumStore()


# ---- API 端点 ----
@app.get("/")
async def root():
    return {
        "service": "Palimpsest",
        "version": get_version(),
        "endpoints": ["/export", "/memory/{id}",
                      "/mem/search", "/mem/hybrid-search", "/mem/ingest", "/mem/link",
                      "/graph/neighbors", "/mem/router"],
    }


@app.get("/export")
async def export_memories(page: int = 1, page_size: int = 100):
    """导出记忆为精简摘要（分页：默认第一页 100 条，page_size 上限 500）。"""
    if page_size > 500:
        page_size = 500
    if page_size < 1:
        page_size = 100
    if page < 1:
        page = 1

    nodes = []
    for nid, payload in store.iter_payloads():
        nodes.append(
            {
                "id": nid,
                "type": payload.get("type", ""),
                "content": payload.get("content", ""),
                "importance": payload.get("importance", 0),
                "status": payload.get("status", ""),
            }
        )

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
async def summary():
    """生成一份人类可读的记忆摘要"""
    events = []
    characters = []
    plots = []
    total = 0

    for nid, payload in store.iter_payloads():
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
    """
    return await generate_report(store)



@app.get("/memory/{node_id}")
async def get_memory(node_id: int):
    """获取指定 ID 的记忆节点 payload（剥掉内部字段 secret_hint / linked_from / linked_kb_ids / superseded）"""
    node = store.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"节点 {node_id} 不存在")
    payload = node.get("payload", {})
    stripped = {
        k: v for k, v in payload.items()
        if k not in ("secret_hint", "linked_from", "linked_kb_ids", "superseded")
    }
    return {"id": node_id, "payload": stripped}


@app.delete("/memory/{node_id}")
async def delete_memory(node_id: int):
    """删除指定 ID 的记忆节点"""
    try:
        store.delete_node(node_id)
        # FTS 全文索引同步（失败不阻塞主删除，可手动 fts-rebuild 兜底）
        sync_node(node_id, "")
        return {"status": "ok", "message": f"节点 {node_id} 已删除"}
    except Exception as e:
        logger.info("删除节点失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="删除失败：节点不存在或已被删除")


def _sync_fts_after_update(node_id: int) -> None:
    """更新节点后同步 FTS 全文索引（失败不阻塞主更新，可手动 fts-rebuild 兜底）。"""
    try:
        node = store.get_node(node_id)
        content = ((node or {}).get("payload") or {}).get("content", "")
        sync_node(node_id, content)
    except Exception as e:
        logger.warning("FTS 索引同步失败 node=%s: %s", node_id, e)


@app.put("/memory/{node_id}")
async def update_memory_payload(node_id: int, payload: dict):
    """更新指定 ID 的记忆 payload（部分更新合并语义：只改传入字段，其余保留）"""
    try:
        store.update_payload(node_id, payload)
        _sync_fts_after_update(node_id)
        return {"status": "ok", "message": f"节点 {node_id} payload 已更新"}
    except Exception as e:
        logger.info("更新节点 payload 失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="更新失败：节点不存在或数据格式错误")


@app.patch("/memory/{node_id}")
async def patch_memory_payload(node_id: int, payload: dict):
    """PATCH：部分更新指定 ID 的记忆 payload（与 PUT 同逻辑，但语义上更精确）"""
    try:
        store.update_payload(node_id, payload)
        _sync_fts_after_update(node_id)
        return {"status": "ok", "message": f"节点 {node_id} payload 已更新"}
    except Exception as e:
        logger.info("更新节点 payload 失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="更新失败：节点不存在或数据格式错误")


@app.patch("/memory/{node_id}/vector")
async def update_memory_vector(node_id: int, vector: list[float]):
    """更新指定 ID 的记忆向量（维度必须匹配）"""
    try:
        if len(vector) != store.dim:
            raise HTTPException(
                status_code=400,
                detail=f"向量维度应为 {store.dim}，实际为 {len(vector)}",
            )
        store.update_vector(node_id, vector)
        return {"status": "ok", "message": f"节点 {node_id} 向量已更新"}
    except HTTPException:
        raise
    except Exception as e:
        logger.info("更新节点向量失败 node=%s: %s", node_id, e)
        raise HTTPException(status_code=404, detail="向量更新失败：节点不存在或维度不匹配")


# ---- 统一语义层端点（2026-08-27 换脑插件通道）----
# 对齐 mcp_server 工具（mem_search / mem_ingest / mem_link / graph_neighbors / router_query），
# 供 Hermes memory provider 插件（plugins/palimpsest/）通过 REST :8090 调用。
# 返回解析后的 JSON（FastAPI 自动序列化），客户端无需再 parse 字符串。
import json as _json

from mcp_tools import (
    mem_search as _mcp_mem_search,
    mem_ingest as _mcp_mem_ingest,
    mem_link as _mcp_mem_link,
    graph_neighbors as _mcp_graph_neighbors,
    mem_communities as _mcp_mem_communities,
    router_query as _mcp_router_query,
    mem_hybrid_search as _mcp_mem_hybrid_search,
)


class MemSearchRequest(BaseModel):
    query: str
    scope: str = "all"        # memory | kb | all
    domain: str = ""
    domain_bias: str = ""     # rule 等
    top_k: int = 5
    include_neighbors: bool = False
    include_outdated: bool = False
    block: str = ""


class MemIngestRequest(BaseModel):
    content: str
    type: str = "memory"      # memory | plan | record | correction | event | kb_chunk ...
    importance: float = 0.5
    domain: str = ""
    source: str = ""


class MemHybridSearchRequest(BaseModel):
    query: str
    scope: str = "all"        # memory | kb | all
    domain: str = ""
    domain_bias: str = ""     # rule 等
    top_k: int = 5
    mode: str = "rrf"         # rrf | cascade
    fts_limit: int = 50
    include_neighbors: bool = False
    neighbor_limit: int = 5
    include_outdated: bool = False
    block: str = ""


class MemLinkRequest(BaseModel):
    source_id: int
    target_id: int
    relation: str = "RELATED_TO"
    weight: float = 0.9
    bidirectional: bool = True


class GraphNeighborsRequest(BaseModel):
    node_id: int
    relation: str = ""
    depth: int = 1
    limit: int = 20
    min_weight: float = 0.0
    block: str = ""


class GraphCommunitiesRequest(BaseModel):
    min_community_size: int = 2
    top_k: int = 20
    with_summary: bool = True


class RouterQueryRequest(BaseModel):
    task: str
    top_k: int = 3


def _as_json(text: str):
    try:
        return _json.loads(text)
    except Exception:
        return {"raw": text}


@app.post("/mem/search")
async def mem_search(req: MemSearchRequest):
    return _as_json(_mcp_mem_search(
        req.query, scope=req.scope, domain=req.domain,
        domain_bias=req.domain_bias, top_k=req.top_k,
        include_neighbors=req.include_neighbors, block=req.block,
        include_outdated=req.include_outdated,
    ))


@app.post("/mem/hybrid-search")
async def mem_hybrid_search(req: MemHybridSearchRequest):
    return _as_json(_mcp_mem_hybrid_search(
        req.query, scope=req.scope, domain=req.domain,
        domain_bias=req.domain_bias, top_k=req.top_k, mode=req.mode,
        fts_limit=req.fts_limit, include_neighbors=req.include_neighbors,
        neighbor_limit=req.neighbor_limit, block=req.block,
        include_outdated=req.include_outdated,
    ))


@app.post("/mem/ingest")
async def mem_ingest(req: MemIngestRequest):
    import json as _json_mod

    result = _mcp_mem_ingest(
        req.content, type=req.type, importance=req.importance,
        domain=req.domain, source=req.source,
    )
    # 校验类拒绝（空内容 / 超长）：REST 侧映射为 422，返回友好 message；
    # 其余 stored:false（如事务失败/secret 强拒，不携带 node_id 键）仍按 JSON 原样返回，不改语义。
    try:
        payload = _json_mod.loads(result)
    except Exception:
        payload = {}
    if (not payload.get("stored") and "node_id" in payload
            and payload.get("node_id") is None):
        return JSONResponse(status_code=422,
                            content={"detail": payload.get("error", "内容校验失败")})
    return _as_json(result)


@app.post("/mem/link")
async def mem_link(req: MemLinkRequest):
    return _as_json(_mcp_mem_link(
        req.source_id, req.target_id, relation=req.relation,
        weight=req.weight, bidirectional=req.bidirectional,
    ))


@app.post("/graph/neighbors")
async def graph_neighbors(req: GraphNeighborsRequest):
    return _as_json(_mcp_graph_neighbors(
        req.node_id, relation=req.relation, depth=req.depth,
        limit=req.limit, min_weight=req.min_weight, block=req.block,
    ))


@app.post("/graph/communities")
async def graph_communities(req: GraphCommunitiesRequest):
    return _as_json(_mcp_mem_communities(
        min_community_size=req.min_community_size,
        top_k=req.top_k, with_summary=req.with_summary,
    ))


@app.post("/mem/router")
async def router_query(req: RouterQueryRequest):
    return _as_json(_mcp_router_query(req.task, top_k=req.top_k))


