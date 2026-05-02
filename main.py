"""
MemoryHub — FastAPI 主入口
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

app = FastAPI(title="MemoryHub")

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
        "service": "MemoryHub",
        "version": "0.9.0",
        "endpoints": ["/extract", "/retrieve", "/import", "/export"],
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
    for node in nodes:
        temp_vec = [0.1] * store.dim
        result = merger.merge(node, temp_vec)
        if result:
            new_count += 1
        else:
            skipped += 1

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

    if not results:
        return MemoryResponse(
            status="ok", injected_text="", debug_info={"message": "未找到相关记忆"}
        )

    # 组装注入文本
    lines = ["[MemoryHub 记忆注入]"]
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
async def import_chat(file: UploadFile = File(...)):
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
                temp_vec = [0.1] * store.dim
                result = merger.merge(node, temp_vec)
                if result:
                    new_count += 1
                else:
                    skipped += 1

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
    all_ids = store._get_all_node_ids()

    nodes = []
    for nid in all_ids:
        node_data = store.get_node(nid)
        if node_data:
            payload = node_data.get("payload", {})
            nodes.append(
                {
                    "id": node_data.get("id"),
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
