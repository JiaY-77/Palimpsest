"""
Palimpsest — FastAPI 主入口
提供记忆提取、检索、导入、导出的完整 API 服务
"""

from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
from typing import Optional
import tempfile
import os

from core.thinking_tracker import ThinkingTracker
from core.trivium_store import TriviumStore
from core.merger import Merger
from core.retriever import Retriever
from core.importer import ChatImporter
from core.pipeline import ingest_many
from core.reporting import generate_report

app = FastAPI(title="Palimpsest")

# ---- 全局服务实例（启动时初始化一次） ----
store = TriviumStore()
tracker = ThinkingTracker()
merger = Merger(store)
retriever = Retriever(store)
importer = ChatImporter()


# ---- 请求/响应模型 ----
class ExtractRequest(BaseModel):
    character_name: str
    user_name: str
    new_message: str
    thinking_text: Optional[str] = None
    recent_messages: list[str] = []


class RetrieveRequest(BaseModel):
    current_message: str
    character_name: str
    user_name: str
    max_tokens: int = 300


class MemoryResponse(BaseModel):
    status: str
    injected_text: str = ""
    debug_info: dict = {}


# ---- API 端点 ----
@app.get("/")
async def root():
    return {
        "service": "Palimpsest",
        "version": "2.1.0",
        "endpoints": ["/extract", "/retrieve", "/import", "/export", "/memory/{id}",
                      "/mem/search", "/mem/hybrid-search", "/mem/ingest", "/mem/link",
                      "/graph/neighbors", "/mem/router"],
    }


@app.post("/extract")
async def extract_memory(req: ExtractRequest):
    """从 AI 回复中提取并合并记忆"""
    if not req.thinking_text or "<thinking>" not in req.thinking_text:
        return MemoryResponse(
            status="skipped",
            debug_info={"message": "消息中未包含 thinking 标签，跳过提取"},
        )

    parsed = tracker.parse_thinking(req.thinking_text)
    nodes = tracker.to_memory_nodes(parsed)

    new_count = 0
    skipped = 0
    # 添加角色隔离字段
    for node in nodes:
        node["character_name"] = req.character_name
        node["user_name"] = req.user_name
    new_count, skipped = ingest_many(store, merger, nodes)

    return MemoryResponse(
        status="ok",
        debug_info={
            "nodes_extracted": len(nodes),
            "new": new_count,
            "skipped": skipped,
            "character": req.character_name,
        },
    )


@app.post("/retrieve")
async def retrieve_memory(req: RetrieveRequest):
    """检索与当前消息相关的记忆"""
    results = retriever.retrieve(req.current_message, top_k=5)
    print("=== 检索结果 ===", results)
    # 过滤只保留当前角色的记忆
    results = [r for r in results if r.get("character_name") == req.character_name]

    if not results:
        return MemoryResponse(
            status="ok", injected_text="", debug_info={"message": "未找到相关记忆"}
        )

    # 组装注入文本
    lines = ["[Palimpsest 记忆注入]"]
    for r in results:
        lines.append(f"- [{r['type']}] {r['content']}")
    injected = "\n".join(lines)

    return MemoryResponse(
        status="ok",
        injected_text=injected,
        debug_info={
            "results_count": len(results),
            "top_score": results[0]["score"] if results else 0,
        },
    )


@app.post("/import")
async def import_chat(
    file: UploadFile = File(...),
    character_name: str = "unknown",
    user_name: str = "unknown",
):
    """导入酒馆聊天文件，批量提取记忆"""
    # 保存上传文件到临时目录
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="wb") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        all_messages = importer.load_file(tmp_path)
        ai_messages = importer.filter_ai_messages(all_messages)

        new_count = 0
        skipped = 0
        for msg in ai_messages:
            parsed = tracker.parse_thinking(msg["reasoning"])
            nodes = tracker.to_memory_nodes(parsed)
            for node in nodes:
                node["character_name"] = character_name
                node["user_name"] = user_name
            n, s = ingest_many(store, merger, nodes)
            new_count += n
            skipped += s

        return {
            "status": "ok",
            "total_messages": len(all_messages),
            "ai_replies_with_thinking": len(ai_messages),
            "new_nodes": new_count,
            "skipped_nodes": skipped,
        }
    finally:
        os.unlink(tmp_path)


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

from mcp_server import (
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


