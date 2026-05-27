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
        # 用真实的文本内容生成向量
        content_to_embed = node.get("content", "") or node.get("label", "")
        real_vec = store.embed_text(content_to_embed)
        result = merger.merge(node, real_vec)
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
                # 用真实的文本内容生成向量
                content_to_embed = node.get("content", "") or node.get("label", "")
                real_vec = store.embed_text(content_to_embed)
                result = merger.merge(node, real_vec)
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


@app.get("/summary")
async def summary():
    """生成一份人类可读的记忆摘要"""
    all_ids = store._get_all_node_ids()

    events = []
    characters = []
    plots = []

    for nid in all_ids:
        node = store.get_node(nid)
        if not node:
            continue
        payload = node.get("payload", {})
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
        "total_memories": len(all_ids),
        "summary": {
            "剧情事件": events[:5],
            "角色状态": characters[:5],
            "剧情计划": plots[:5],
        },
    }


@app.post("/report")
async def generate_report():
    """
    基于当前数据库中的所有记忆，调用 LLM 生成一份角色灵魂分析报告。
    这不再是简单的摘要，而是对角色命运的洞察。
    """
    # 1. 先从数据库获取所有记忆
    all_ids = store._get_all_node_ids()
    memories = []
    for nid in all_ids:
        node = store.get_node(nid)
        if node:
            payload = node.get("payload", {})
            if payload.get("content"):
                memories.append(f"[{payload.get('type', '')}] {payload['content']}")

    if not memories:
        return {
            "status": "error",
            "message": "当前数据库中没有记忆，请先导入聊天记录。",
        }

    # 2. 把所有记忆文本组装成一个大的上下文
    memory_text = "\n".join(memories)

    # 3. 这是整个 MemoryHub 最核心的 Prompt 之一
    # 它定义了我们的系统如何从一个数据仓库，变成一个灵魂洞察师
    report_prompt = f"""你是一位极其敏锐的角色扮演心理分析师。你的任务不是复述剧情，而是洞察角色的灵魂。

下面是从一段漫长的角色扮演对话中，提取出的关键记忆碎片。这些碎片记录了角色的行为、状态、计划和内心独白。

请你根据这些碎片，生成一份深刻的角色分析报告。报告必须包含以下几个维度：

1.  **人格光谱**：分析该角色展现出的核心人格特质。不要只贴标签，要说明这些特质是如何通过具体行为体现的。特别要指出其人格中存在的“矛盾”或“复杂性”（例如，“优雅的残忍”、“伪装成粗心的占有欲”）。

2.  **命运时刻**：从记忆碎片中，找出 3 个最关键的事件转折点。这些时刻必须深刻影响了故事走向或角色关系。请说明为什么这些是转折点。

3.  **内心戏剧场**：深入分析角色的内心世界。他/她的伪装、压抑的渴望、内心的恐惧、对自己或他人的谎言是什么？他/她嘴上说的和心里想的，可能有什么不同？

4.  **未完成的交响曲**：基于已有的线索和角色的行为模式，大胆预测 2-3 条故事未来可能的发展方向，或者指出那些尚未被提及、但隐隐存在的“伏笔”。

请用优美、流畅、充满洞察力的中文撰写这份报告。你要像一位资深文学评论家在分析他最爱的角色一样，充满热情和深度。不要使用 markdown 格式的标题，用优雅的自然段落来分隔各个部分。

=== 记忆碎片 ===
{memory_text}
"""
    # 4. 调用 DeepSeek
    try:
        store._acquire()  # 这是一个临时连接，我们只需要用它来调一次 API
        # 注意：这里我们直接使用 openai 库，因为 store 里没有封装 chat.completions
        from openai import OpenAI
        from config import Config

        llm_cfg = Config.get_llm_config()
        client = OpenAI(api_key=llm_cfg["api_key"], base_url=llm_cfg["base_url"])

        completion = client.chat.completions.create(
            model=llm_cfg["model"],
            messages=[{"role": "user", "content": report_prompt}],
            temperature=0.8,  # 给报告一点文学性的创造力
            max_tokens=4000,
        )
        report = completion.choices[0].message.content
        return {"status": "ok", "report": report}
    except Exception as e:
        return {"status": "error", "message": f"报告生成失败: {str(e)}"}
