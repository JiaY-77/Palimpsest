from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from config import Config

app = FastAPI(title="MemoryHub")


# ---------- 请求/响应模型 ----------
class ExtractRequest(BaseModel):
    character_name: str  # 当前角色名
    user_name: str  # 用户名
    new_message: str  # 新产生的消息（用户或角色）
    recent_messages: list[str] = []  # 最近的几条消息（可选）


class RetrieveRequest(BaseModel):
    current_message: str  # 用户当前输入
    character_name: str
    user_name: str
    max_tokens: int = 300  # 预留注入文本的最大 token 数


class MemoryResponse(BaseModel):
    status: str
    injected_text: str = ""
    debug_info: dict = {}


# ---------- API 端点 ----------
@app.post("/extract")
async def extract_memory(req: ExtractRequest):
    """从新消息中提取并存储记忆"""
    # TODO: 实现提取逻辑
    return MemoryResponse(
        status="ok", debug_info={"message": "extract endpoint placeholder"}
    )


@app.post("/retrieve")
async def retrieve_memory(req: RetrieveRequest):
    """检索与当前消息相关的记忆，返回要注入的上下文文本"""
    # TODO: 实现检索逻辑（硬关联+语义+叙事弧光）
    return MemoryResponse(
        status="ok",
        injected_text="",
        debug_info={"message": "retrieve endpoint placeholder"},
    )


@app.get("/")
async def root():
    return {"message": "MemoryHub is running."}
