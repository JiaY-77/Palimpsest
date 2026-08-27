# -*- coding: utf-8 -*-
"""
mcp_tools.routing —— 任务路由查询工具
====================================
router_query（从规则类知识切片提取推荐模型/配置）+ 关键词提取 _extract_recommendation。
"""

from mcp_tools._common import _shorten, _to_json, mcp  # noqa: E402
from mcp_tools.memory import _mem_search_impl  # noqa: E402

# ---- v2.0 统一语义层：任务路由查询 ----
# 模型/配置关键词（用于从规则切片中提取「推荐模型/配置」）
_MODEL_KEYWORDS = [
    "r1", "qwen9b", "qwen", "glm", "phi", "opencode", "deepseek", "kimi",
    "gpt", "claude", "gemini", "mistral", "llama", "minimax", "yi",
    "codestral", "qwen3",
]
_CONFIG_KEYWORDS = [
    "preset", "think", "token", "max_tokens", "temperature", "8k", "16k",
    "32k", "context", "stream", "tool",
]


def _extract_recommendation(text: str) -> tuple:
    """
    从规则片段文本中粗提取「推荐模型/配置」。
    返回 (recommended_dict|None, confidence, hit_model, hit_config)。
    confidence：命中模型关键词 → high；只命中部分（仅配置关键词）→ medium；无命中 → low。
    """
    text = text or ""
    low = text.lower()
    hit_models = [m for m in _MODEL_KEYWORDS if m in low]
    hit_configs = [c for c in _CONFIG_KEYWORDS if c in low]
    if hit_models:
        model = hit_models[0]
        config_hints = [c for c in hit_configs]
        recommended = {"model": model}
        if config_hints:
            recommended["config"] = ", ".join(config_hints[:4])
        return recommended, "high", bool(hit_models), bool(hit_configs)
    if hit_configs:
        return {"model": None, "config": ", ".join(hit_configs[:4])}, "medium", False, True
    return None, "low", False, False


@mcp.tool()
def router_query(task: str, top_k: int = 3) -> str:
    """
    任务路由查询（v2.0 统一语义层）：给定一个任务描述，查规则类知识切片
    （domain=rule，即 副官加班协议/宪法/模型军团管理办法/模型路由决策树 等），
    提取「推荐模型/配置」建议。
    返回 JSON：{"task", "recommended": {model, config}|null, "confidence": high|medium|low,
               "sources": [{path, score, snippet(前100字)}], "raw_hint": 规则原文摘要}
    """
    task = (task or "").strip()
    if not task:
        return _to_json({
            "task": task, "recommended": None, "confidence": "low",
            "sources": [], "raw_hint": "任务描述不能为空",
        })
    # 内部复用 mem_search 核心：只查规则切片（scope=kb + domain_bias=rule + 内置 rule ×1.3）
    raw = _mem_search_impl(task, scope="kb", domain="", domain_bias="rule", top_k=top_k)
    results = raw.get("results", [])
    sources = []
    for item in results:
        sources.append({
            "path": item["meta"].get("source_path", ""),
            "score": item["score"],
            "snippet": _shorten(item.get("summary", ""), 100),
        })
    # 拼接规则原文（前 800 字）做关键词提取
    raw_text = " ".join(item.get("summary", "") for item in results)[:800]
    recommended, confidence, _hit_model, _hit_cfg = _extract_recommendation(raw_text)
    return _to_json({
        "task": task,
        "recommended": recommended,
        "confidence": confidence,
        "sources": sources,
        "raw_hint": _shorten(raw_text, 300),
    })
