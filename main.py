"""
Palimpsest — FastAPI 主入口
提供记忆提取、检索、导入、导出的完整 API 服务
"""

import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from core.fts_index import remove_node
from core.startup_check import run_startup_check
from core.trivium_store import TriviumStore
from core.reporting import generate_report
from core.version import get_version

logger = logging.getLogger(__name__)

app = FastAPI(title="Palimpsest")


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
async def export_memories():
    """导出所有记忆为精简摘要"""
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

    # 按重要性降序排列
    nodes.sort(key=lambda n: n.get("importance", 0), reverse=True)

    return {
        "status": "ok",
        "total_nodes": len(nodes),
        "memories": nodes,
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



@app.delete("/memory/{node_id}")
async def delete_memory(node_id: int):
    """删除指定 ID 的记忆节点"""
    try:
        store.delete_node(node_id)
        # FTS 全文索引同步（失败不阻塞主删除，可手动 fts-rebuild 兜底）
        try:
            remove_node(node_id)
        except Exception:
            pass
        return {"status": "ok", "message": f"节点 {node_id} 已删除"}
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"删除失败: {str(e)}")


@app.put("/memory/{node_id}")
async def update_memory_payload(node_id: int, payload: dict):
    """更新指定 ID 的记忆 payload（元数据）"""
    try:
        store.update_payload(node_id, payload)
        return {"status": "ok", "message": f"节点 {node_id} payload 已更新"}
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"更新失败: {str(e)}")


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
        raise HTTPException(status_code=404, detail=f"更新失败: {str(e)}")


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
    ))


@app.post("/mem/hybrid-search")
async def mem_hybrid_search(req: MemHybridSearchRequest):
    return _as_json(_mcp_mem_hybrid_search(
        req.query, scope=req.scope, domain=req.domain,
        domain_bias=req.domain_bias, top_k=req.top_k, mode=req.mode,
        fts_limit=req.fts_limit, include_neighbors=req.include_neighbors,
        neighbor_limit=req.neighbor_limit, block=req.block,
    ))


@app.post("/mem/ingest")
async def mem_ingest(req: MemIngestRequest):
    return _as_json(_mcp_mem_ingest(
        req.content, type=req.type, importance=req.importance,
        domain=req.domain, source=req.source,
    ))


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


@app.post("/mem/router")
async def router_query(req: RouterQueryRequest):
    return _as_json(_mcp_router_query(req.task, top_k=req.top_k))


