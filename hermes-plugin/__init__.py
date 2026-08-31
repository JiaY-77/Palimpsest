"""Palimpsest memory plugin — MemoryProvider backed by Palimpsest REST :8090.

把 Hermes 的记忆层换成 Palimpsest（Memory Provider 插件）。

能力：
  - prefetch(): 每轮自动召回 Palimpsest 语义记忆（含图谱邻居）注入上下文
  - sync_turn(): 检测重要信号（纠正/偏好/决策/规则）自动沉淀，避免垃圾写入
  - on_session_end(): 会话末提炼要点（含强信号的消息）
  - on_pre_compress(): 压缩前抽取要点，贡献给压缩 prompt（不写入）
  - 5 个工具: palimpsest_search / palimpsest_ingest / palimpsest_link /
    palimpsest_graph / palimpsest_router —— 模型可主动检索/写入/建边/路由

配置（环境变量，可选；默认即指向本机 Palimpsest）:
  PALIMPSEST_BASE_URL       默认 http://127.0.0.1:8090
  PALIMPSEST_DOMAIN         默认 hermes
  PALIMPSEST_PREFETCH_TOP_K 默认 5
  PALIMPSEST_AUTO_INGEST    默认 true；false 关闭自动沉淀（只用工具）
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt

logger = logging.getLogger(__name__)

# 强信号：命中即触发 sync_turn 自动沉淀 / on_session_end 提炼 / on_pre_compress 抽取。
# 保守锚定中文语料：纠正、偏好、决策、规则、启动类动词。
_IMPORTANT_RE = re.compile(
    r"(记住|记好|以后|从今|别忘|不要忘|我的偏好|我更喜欢|我习惯|"
    r"不对|不是|错了|纠正|更正|改成|改为|"
    r"批准|决定|拍板|定案|方案|规则|规矩|红线|"
    r"开始做|启动|立项|安排|计划|下一步|优先)"
)


def _http_post(url: str, payload: dict, timeout: float = 5.0) -> dict:
    """REST POST 到 Palimpsest :8090，返回解析后的 JSON；失败返回 {"error": ...}。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — 记忆后端必须 fail-open
        logger.debug("Palimpsest REST %s failed: %s", url, exc)
        return {"error": str(exc)}


def _msg_text(msg: Dict[str, Any]) -> str:
    return str(msg.get("content") or "")


# ---------------------------------------------------------------------------
# Tool schemas（面向模型：模型决定何时主动用）
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "palimpsest_search",
    "description": (
        "语义检索 Palimpsest 记忆库：跨会话历史记忆 + 知识库切片，"
        "可选图谱邻居。返回 150 字摘要 + 相关度分。用于回忆具体历史事实、"
        "查知识、找相关规则。比内置记忆更深（含语义向量 + 图谱）。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "想查的内容（自然语言）"},
            "scope": {"type": "string", "enum": ["all", "memory", "kb"], "description": "all=记忆+知识库(默认)；memory=只记忆；kb=只知识库"},
            "top_k": {"type": "integer", "description": "返回条数（默认 5）"},
            "include_neighbors": {"type": "boolean", "description": "是否附带图谱邻居（默认 false）"},
        },
        "required": ["query"],
    },
}

INGEST_SCHEMA = {
    "name": "palimpsest_ingest",
    "description": (
        "向 Palimpsest 写入一条记忆。自动冲突检测：与库中相似旧记忆会标记 outdated 并挂 REVISED_BY 链。"
        "type 常用：memory(默认)/record(运维记录)/plan(方案)/correction(纠正——importance 建议 0.85)/event。"
        "importance 0-1，纠正和规则类用高值（0.7+），普通观察 0.5。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "记忆内容"},
            "type": {"type": "string", "description": "memory/record/plan/correction/event（默认 memory）"},
            "importance": {"type": "number", "description": "重要性 0-1（默认 0.5）"},
            "domain": {"type": "string", "description": "域（默认 hermes）"},
        },
        "required": ["content"],
    },
}

LINK_SCHEMA = {
    "name": "palimpsest_link",
    "description": (
        "在两条记忆节点之间建图谱边（默认 RELATED_TO）。"
        "用于把相关事实显式关联起来，后续图谱检索/邻居扩散可用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {"type": "integer", "description": "源节点 id"},
            "target_id": {"type": "integer", "description": "目标节点 id"},
            "relation": {"type": "string", "description": "RELATED_TO(默认)/CAUSES/REFERS_TO/REVISED_BY"},
        },
        "required": ["source_id", "target_id"],
    },
}

GRAPH_SCHEMA = {
    "name": "palimpsest_graph",
    "description": (
        "查某个记忆节点的图谱邻居（沿出边 BFS，depth 1-3）。"
        "用于看一条记忆关联了哪些其他记忆/知识，发现隐藏关系。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "node_id": {"type": "integer", "description": "起始节点 id"},
            "relation": {"type": "string", "description": "只沿该关系边扩散（空=全部）"},
            "depth": {"type": "integer", "description": "扩散深度 1-3（默认 1）"},
        },
        "required": ["node_id"],
    },
}

ROUTER_SCHEMA = {
    "name": "palimpsest_router",
    "description": (
        "任务路由查询：给 Palimpsest 一个任务描述，返回规则类知识（模型路由决策树）"
        "推荐的模型与配置。用于派活/选模型时快速对齐纪律。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "任务描述"},
            "top_k": {"type": "integer", "description": "返回条数（默认 3）"},
        },
        "required": ["task"],
    },
}


class PalimpsestMemoryProvider(MemoryProvider):
    """Palimpsest 记忆后端：语义召回 + 自动沉淀 + 图谱 + 规则路由。"""

    pre_compress_checkpoint_api_version = 1

    def __init__(self) -> None:
        self._base_url = os.environ.get(
            "PALIMPSEST_BASE_URL", "http://127.0.0.1:8090"
        ).rstrip("/")
        self._domain = os.environ.get("PALIMPSEST_DOMAIN", "hermes")
        self._top_k = int(os.environ.get("PALIMPSEST_PREFETCH_TOP_K", "5"))
        self._auto_ingest = (
            os.environ.get("PALIMPSEST_AUTO_INGEST", "true").lower() != "false"
        )
        self._enabled = False
        self._cron_skipped = False
        self._session_id = ""
        self._last_recall: Optional[RecallStatus] = None

    # -- 核心生命周期 ------------------------------------------------

    @property
    def name(self) -> str:
        return "palimpsest"

    def is_available(self) -> bool:
        # 契约：只查配置可达性，不做网络调用
        return bool(self._base_url)

    def unavailable_reason(self) -> str:
        return "未配置 PALIMPSEST_BASE_URL（默认 http://127.0.0.1:8090 即可）"

    def initialize(self, session_id: str, **kwargs) -> None:
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in {"cron", "flush"} or platform == "cron":
            logger.debug("Palimpsest skipped: cron/flush context")
            self._cron_skipped = True
            return
        self._session_id = session_id
        self._enabled = True
        logger.info(
            "Palimpsest memory provider initialized (domain=%s, base=%s, auto_ingest=%s)",
            self._domain, self._base_url, self._auto_ingest,
        )

    def system_prompt_block(self) -> str:
        if not self._enabled:
            return ""
        return (
            "\n[Palimpsest 记忆层] 本会话已接入 Palimpsest 语义记忆。"
            "相关历史记忆会自动注入；可用 palimpsest_* 工具主动检索/写入/建图谱边/规则路由。"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮召回相关记忆注入上下文；trivial 输入跳过（省一次 HTTP）。"""
        self._last_recall = None
        if not self._enabled or is_trivial_prompt(query):
            return ""
        if len((query or "").strip()) < 4:
            return ""
        resp = _http_post(f"{self._base_url}/mem/search", {
            "query": query, "scope": "all", "domain": self._domain,
            "top_k": self._top_k, "include_neighbors": True,
        })
        if "error" in resp or not resp.get("results"):
            return ""
        hits = [r for r in resp["results"] if r.get("score", 0) >= 0.3]
        if not hits:
            return ""
        lines = ["[Palimpsest 记忆注入]"]
        for r in hits[: self._top_k]:
            lines.append(f"- ({r.get('score', 0):.2f}) {r.get('summary', '')[:150]}")
        self._last_recall = RecallStatus(provider_label="palimpsest", count=len(hits))
        return "\n".join(lines)

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """每轮沉淀：只在命中强信号时写入，避免库被低价值轮次污染。"""
        if not self._enabled or not self._auto_ingest:
            return
        if is_trivial_prompt(user_content) or not user_content:
            return
        if not _IMPORTANT_RE.search(user_content):
            return
        importance = (
            0.7
            if any(k in user_content for k in ("不对", "不是", "错了", "纠正", "更正"))
            else 0.6
        )
        _http_post(f"{self._base_url}/mem/ingest", {
            "content": f"[对话沉淀] 用户: {user_content[:300]}",
            "type": "memory", "importance": importance,
            "domain": self._domain, "source": "hermes-sync_turn",
        })

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """会话结束：把含强信号的消息提炼成一条要点。"""
        if not self._enabled or not self._auto_ingest:
            return
        points: List[str] = []
        seen: set = set()
        for msg in messages:
            text = _msg_text(msg)
            if not text or text in seen:
                continue
            if _IMPORTANT_RE.search(text):
                seen.add(text)
                points.append(f"[{msg.get('role', '?')}] {text[:150]}")
        if not points:
            return
        content = "会话要点（Palimpsest 插件提炼）：\n" + "\n".join(points[:8])
        _http_post(f"{self._base_url}/mem/ingest", {
            "content": content, "type": "record", "importance": 0.55,
            "domain": self._domain, "source": "hermes-session_end",
        })

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """压缩前抽取要点，贡献给压缩 prompt（不写入 Palimpsest，只保上下文）。"""
        points: List[str] = []
        for msg in messages:
            text = _msg_text(msg)
            if not text:
                continue
            if _IMPORTANT_RE.search(text):
                points.append(f"[{msg.get('role', '?')}] {text[:200]}")
        return "\n".join(points[:10])

    def shutdown(self) -> None:
        self._enabled = False

    # -- 工具 --------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, INGEST_SCHEMA, LINK_SCHEMA, GRAPH_SCHEMA, ROUTER_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        handlers = {
            "palimpsest_search": self._tool_search,
            "palimpsest_ingest": self._tool_ingest,
            "palimpsest_link": self._tool_link,
            "palimpsest_graph": self._tool_graph,
            "palimpsest_router": self._tool_router,
        }
        fn = handlers.get(tool_name)
        if fn is None:
            return json.dumps({"error": f"unknown tool {tool_name}"}, ensure_ascii=False)
        try:
            return json.dumps(fn(args), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _tool_search(self, args: Dict[str, Any]) -> dict:
        return _http_post(f"{self._base_url}/mem/search", {
            "query": args.get("query", ""), "scope": args.get("scope", "all"),
            "domain": args.get("domain", self._domain),
            "top_k": int(args.get("top_k", 5)),
            "include_neighbors": bool(args.get("include_neighbors", False)),
        })

    def _tool_ingest(self, args: Dict[str, Any]) -> dict:
        return _http_post(f"{self._base_url}/mem/ingest", {
            "content": args.get("content", ""), "type": args.get("type", "memory"),
            "importance": float(args.get("importance", 0.5)),
            "domain": args.get("domain", self._domain), "source": "hermes-tool",
        })

    def _tool_link(self, args: Dict[str, Any]) -> dict:
        return _http_post(f"{self._base_url}/mem/link", {
            "source_id": int(args.get("source_id", 0)),
            "target_id": int(args.get("target_id", 0)),
            "relation": args.get("relation", "RELATED_TO"),
            "weight": float(args.get("weight", 0.9)),
            "bidirectional": bool(args.get("bidirectional", True)),
        })

    def _tool_graph(self, args: Dict[str, Any]) -> dict:
        return _http_post(f"{self._base_url}/graph/neighbors", {
            "node_id": int(args.get("node_id", 0)),
            "relation": args.get("relation", ""),
            "depth": int(args.get("depth", 1)),
            "limit": int(args.get("limit", 20)),
        })

    def _tool_router(self, args: Dict[str, Any]) -> dict:
        return _http_post(f"{self._base_url}/mem/router", {
            "task": args.get("task", ""), "top_k": int(args.get("top_k", 3)),
        })


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """注册 Palimpsest 为 Hermes memory provider + context engine 插件（双插件换脑）。"""
    ctx.register_memory_provider(PalimpsestMemoryProvider())
    try:
        from .context_engine import PalimpsestContextEngine

        ctx.register_context_engine(PalimpsestContextEngine(model="__pending__"))
    except Exception as exc:  # noqa: BLE001 — context engine 注册失败不影响 memory provider
        logger.debug("Context engine registration failed: %s", exc)
