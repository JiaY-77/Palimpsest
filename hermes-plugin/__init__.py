"""Palimpsest × Hermes 适配器（薄）
========================================

**本文件是宿主适配器，不是策略层。** 所有「什么值得记、记哪层、什么时候召回、
怎么去重提炼」的判定都在 Palimpsest 本体的策略引擎里（``core/strategy.py``）；
Hermes 侧只做三件事：

1. **钩子注册**——告诉 Hermes 在哪些生命周期点调用我们；
2. **字段映射**——把 Hermes 的 hook payload 转成本体 lifecycle 协议的请求
   （读环境变量配置、拼 JSON）；
3. **结果注入**——把本体返回的 ``inject_text`` 原样拼进 prompt。

为什么这样拆：记忆智能是 Palimpsest 的产品核心资产，必须住在本体，否则换一个
宿主（Claude Code / Cursor / 任意 agent）智能就丢了。适配器越薄，本体通用性越强。

双插件：Memory Provider（本文件）+ Context Engine（``context_engine.py``）。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

from agent.memory_provider import MemoryProvider, RecallStatus

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:8090"


def _http_post(url: str, payload: dict, timeout: float = 5.0) -> dict:
    """REST POST 到 Palimpsest :8090，返回解析后的 JSON；失败返回 {"error": ...}。

    fail-open：记忆后端不可用时静默降级（返回空、不阻塞宿主）。
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — 记忆后端必须 fail-open
        logger.debug("Palimpsest REST %s failed: %s", url, exc)
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool schemas（面向模型：模型决定何时主动用；冷路径，与 lifecycle 热路径分工）
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
            "scope": {
                "type": "string",
                "enum": ["all", "memory", "kb"],
                "description": "all=记忆+知识库(默认)；memory=只记忆；kb=只知识库",
            },
            "top_k": {"type": "integer", "description": "返回条数（默认 5）"},
            "include_neighbors": {"type": "boolean", "description": "是否附带图谱邻居（默认 false）"},
            "tier": {
                "type": "string",
                "description": "记忆分层：facts(默认，只回事实层) / logs(只回日志层) / 空串(不过滤)",
            },
            "domain": {"type": "string", "description": "域（默认 hermes）"},
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
        "在两条记忆节点之间建图谱边（默认 RELATED_TO）。用于把相关事实显式关联起来，后续图谱检索/邻居扩散可用。"
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
        "查某个记忆节点的图谱邻居（沿出边 BFS，depth 1-3）。用于看一条记忆关联了哪些其他记忆/知识，发现隐藏关系。"
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


class PalimpsestMemoryProvider(MemoryProvider):
    """Hermes 记忆后端适配器：把生命周期事件转发到本体 lifecycle 协议。

    本类只做传输（读环境变量配置、拼 payload、发 POST、把返回文本交给宿主）。
    「召不召 / 抽不抽 / 记哪层」的判定全部在 Palimpsest 本体的 ``core/strategy.py``。
    """

    # Providers that durably checkpoint every successful on_pre_compress() set this to
    # PRE_COMPRESS_CHECKPOINT_API_VERSION; 1 = best-effort legacy.
    pre_compress_checkpoint_api_version = 1

    def __init__(self) -> None:
        self._base_url = os.environ.get("PALIMPSEST_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        self._domain = os.environ.get("PALIMPSEST_DOMAIN", "hermes")
        # 召回参数（传输类配置，随请求传给本体策略引擎，由本体决策如何使用）
        self._top_k = int(os.environ.get("PALIMPSEST_PREFETCH_TOP_K", "3"))
        # 注入降噪（T081 配套）：图邻居默认关（记忆域图近乎无边，纯空转）；
        # 注入最低相关度门槛可配，默认与旧硬编码一致 0.3，可调高再砍噪音。
        self._include_neighbors = os.environ.get("PALIMPSEST_PREFETCH_NEIGHBORS", "false").lower() == "true"
        self._min_score = float(os.environ.get("PALIMPSEST_PREFETCH_MIN_SCORE", "0.3"))
        # 记忆分层：默认只注入事实层，日志层（record/event/git_commit）不进上下文。
        self._tier = os.environ.get("PALIMPSEST_PREFETCH_TIER", "facts")
        # 召回范围：默认 memory（只召回记忆节点）。修 #46——旧默认 all 会让 kb_chunk
        # 混进注入池并挤占 top_k 名额（kb_chunk 的 domain 恒为 "kb"，domain 过滤对它
        # 无效，只有 scope=memory 能挡住）。需要把知识库切片一并注入时显式设为 all。
        self._scope = os.environ.get("PALIMPSEST_PREFETCH_SCOPE", "memory")
        self._auto_ingest = os.environ.get("PALIMPSEST_AUTO_INGEST", "true").lower() != "false"
        self._enabled = False
        self._cron_skipped = False
        self._session_id = ""
        self._last_recall: RecallStatus | None = None

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
            self._enabled = False
            return
        self._session_id = session_id
        self._enabled = True
        logger.info(
            "Palimpsest memory provider initialized (domain=%s, base=%s, auto_ingest=%s)",
            self._domain,
            self._base_url,
            self._auto_ingest,
        )

    def system_prompt_block(self) -> str:
        if not self._enabled:
            return ""
        return (
            "\n[Palimpsest 记忆层] 本会话已接入 Palimpsest 语义记忆。"
            "相关历史记忆会自动注入；可用 palimpsest_* 工具主动检索/写入/建图谱边。"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮召回：转发 ``/lifecycle/pre-turn``，原样返回本体给的注入文本。

        策略（trivial 判定 / 长度门槛 / score 过滤 / 条数截断 / 格式化）全在本体。
        """
        self._last_recall = None
        if not self._enabled:
            return ""
        resp = _http_post(
            f"{self._base_url}/lifecycle/pre-turn",
            {
                "session_id": session_id or self._session_id,
                "user_message": query,
                "domain": self._domain,
                "scope": self._scope,
                "top_k": self._top_k,
                "include_neighbors": self._include_neighbors,
                "tier": self._tier,
                "min_score": self._min_score,
            },
        )
        if "error" in resp:
            return ""
        text = resp.get("inject_text", "")
        if text:
            count = len(resp.get("inject_blocks", []))
            self._last_recall = RecallStatus(provider_label="palimpsest", count=count)
        return text

    def recall_status(self) -> RecallStatus | None:
        return self._last_recall

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """每轮沉淀：转发 ``/lifecycle/post-turn``（命中判定 / importance / 分层在本体）。"""
        if not self._enabled or not self._auto_ingest:
            return
        _http_post(
            f"{self._base_url}/lifecycle/post-turn",
            {
                "session_id": session_id or self._session_id,
                "user_message": user_content,
                "assistant_message": assistant_content or "",
                "auto_ingest": self._auto_ingest,
            },
        )

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """会话结束：转发 ``/lifecycle/session-end``（要点提炼 / 分层 / 去重在本体）。"""
        if not self._enabled or not self._auto_ingest:
            return
        _http_post(
            f"{self._base_url}/lifecycle/session-end",
            {
                "session_id": self._session_id,
                "messages": messages or [],
                "auto_ingest": self._auto_ingest,
            },
        )

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """压缩前抽取：转发 ``/lifecycle/pre-compress``（不写库，只回文本）。"""
        if not self._enabled:
            return ""
        resp = _http_post(
            f"{self._base_url}/lifecycle/pre-compress",
            {"session_id": self._session_id, "messages": messages or []},
        )
        if "error" in resp:
            return ""
        return resp.get("points_text", "")

    def shutdown(self) -> None:
        self._enabled = False

    # -- 工具（冷路径：模型主动调用）--------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [SEARCH_SCHEMA, INGEST_SCHEMA, LINK_SCHEMA, GRAPH_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        handlers = {
            "palimpsest_search": self._tool_search,
            "palimpsest_ingest": self._tool_ingest,
            "palimpsest_link": self._tool_link,
            "palimpsest_graph": self._tool_graph,
        }
        fn = handlers.get(tool_name)
        if fn is None:
            return json.dumps({"error": f"unknown tool {tool_name}"}, ensure_ascii=False)
        try:
            return json.dumps(fn(args), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 —— 工具调用异常转 JSON 错误，不崩调用方
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    def _tool_search(self, args: dict[str, Any]) -> dict:
        return _http_post(
            f"{self._base_url}/mem/search",
            {
                "query": args.get("query", ""),
                "scope": args.get("scope", "all"),
                "domain": args.get("domain", self._domain),
                "top_k": int(args.get("top_k", 5)),
                "include_neighbors": (str(args.get("include_neighbors", False)).lower() == "true"),
                "tier": args.get("tier", self._tier),
            },
        )

    def _tool_ingest(self, args: dict[str, Any]) -> dict:
        return _http_post(
            f"{self._base_url}/mem/ingest",
            {
                "content": args.get("content", ""),
                "type": args.get("type", "memory"),
                "importance": float(args.get("importance", 0.5)),
                "domain": args.get("domain", self._domain),
                "source": "hermes-tool",
            },
        )

    def _tool_link(self, args: dict[str, Any]) -> dict:
        return _http_post(
            f"{self._base_url}/mem/link",
            {
                "source_id": int(args.get("source_id", 0)),
                "target_id": int(args.get("target_id", 0)),
                "relation": args.get("relation", "RELATED_TO"),
                "weight": float(args.get("weight", 0.9)),
                "bidirectional": (str(args.get("bidirectional", True)).lower() == "true"),
            },
        )

    def _tool_graph(self, args: dict[str, Any]) -> dict:
        return _http_post(
            f"{self._base_url}/graph/neighbors",
            {
                "node_id": int(args.get("node_id", 0)),
                "relation": args.get("relation", ""),
                "depth": int(args.get("depth", 1)),
                "limit": int(args.get("limit", 20)),
            },
        )


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
