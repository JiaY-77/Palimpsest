"""Palimpsest memory plugin — MemoryProvider backed by Palimpsest REST :8090.

把 Hermes 的记忆层换成 Palimpsest（Memory Provider 插件）。

能力：
  - prefetch(): 每轮自动召回 Palimpsest 语义记忆（含图谱邻居）注入上下文
  - sync_turn(): 检测「明确指令」信号（纠正/偏好/规则）自动沉淀到 logs 层
  - on_session_end(): 会话末提炼要点写入 facts 层
  - on_pre_compress(): 压缩前抽取要点，贡献给压缩 prompt（不写入）
  - 4 个工具: palimpsest_search / palimpsest_ingest / palimpsest_link /
    palimpsest_graph —— 模型可主动检索/写入/建边

配置（环境变量，可选；默认即指向本机 Palimpsest）:
  PALIMPSEST_BASE_URL       默认 http://127.0.0.1:8090
  PALIMPSEST_DOMAIN         默认 hermes
  PALIMPSEST_PREFETCH_TOP_K 默认 3（注入降噪：5→3）
  PALIMPSEST_PREFETCH_NEIGHBORS   默认 false（图邻居不入注入；true 打开）
  PALIMPSEST_PREFETCH_MIN_SCORE   默认 0.3（注入最低相关度门槛）
  PALIMPSEST_PREFETCH_TIER        默认 facts（只注入事实层；空串=不过滤）
  PALIMPSEST_PREFETCH_SCOPE       默认 memory（只召回记忆；all=含知识库切片）
  PALIMPSEST_AUTO_INGEST    默认 true；false 关闭自动沉淀（只用工具）
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from typing import Any

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt

logger = logging.getLogger(__name__)

# 强信号：命中即触发 on_session_end 提炼 / on_pre_compress 抽取。
# 保守锚定中文语料：纠正、偏好、决策、规则、启动类动词。
# 注意：此正则偏「宽」——它的产出是「会话要点」（人工可读的提炼），不是
# 逐条自动落库的事实，所以容忍一定噪音。sync_turn 的自动落库另用
# _EXPLICIT_INSTRUCTION_RE（见下），两者刻意解耦。
_IMPORTANT_RE = re.compile(
    r"(记住|记好|以后|从今|别忘|不要忘|我的偏好|我更喜欢|我习惯|"
    r"不对|不是|错了|纠正|更正|改成|改为|"
    r"批准|决定|拍板|定案|方案|规则|规矩|红线|"
    r"开始做|启动|立项|安排|计划|下一步|优先)"
)

# sync_turn 自动落库专用：只认「对助手的明确指令 / 长期偏好 / 纠正」，
# 不认「操作动词」。原因：`启动|安排|计划|方案|优先|决定|立项` 这类词在
# 命令输出、构建日志、后台进程通知里高频出现，宽正则会把噪音写进库；
# 而 sync_turn 是「关键词命中即把用户原话落库」，误触发直接污染检索池。
# 剔除操作词后，命中即可解释为「用户在下指令」，事后可审计。
_EXPLICIT_INSTRUCTION_RE = re.compile(
    r"(记住|记好|以后|从今|别忘|不要忘|别再|"
    r"我的偏好|我更喜欢|我习惯|我一般|我通常|"
    r"不对|不是这样|错了|纠正|更正|改成|改为|"
    r"规则|规矩|红线)"
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


def _msg_text(msg: dict[str, Any]) -> str:
    return str(msg.get("content") or "")


def _extract_points(messages: list[dict[str, Any]], limit: int, per_message_chars: int) -> list[str]:
    """从消息列表中提炼要点行。只接受 user/assistant 角色，去重，命中 _IMPORTANT_RE。"""
    _ALLOWED_ROLES = ("user", "assistant")
    points: list[str] = []
    seen: set = set()
    for msg in messages:
        if msg.get("role") not in _ALLOWED_ROLES:
            continue
        text = _msg_text(msg)
        if not text or not text.strip():
            continue
        if text in seen:
            continue
        if not _IMPORTANT_RE.search(text):
            continue
        seen.add(text)
        points.append(f"[{msg.get('role', '?')}] {text[:per_message_chars]}")
        if len(points) >= limit:
            break
    return points


# Semantic-score threshold for near-duplicate detection.  The top-1 result from
# ``/mem/search`` is compared against this value:
#   - This is the "content already exists" semantic-match threshold (0–1 scale).
#   - The function is *fail-open*: HTTP errors, empty results, or missing scores
#     are all treated as "not duplicate".  A high threshold here avoids false
#     positives that would silently discard distinct-but-similar memories.
# If you change this value, update the docstring and the boundary test in
# ``tests/test_hermes_plugin_extraction.py`` accordingly.
_NEAR_DUP_THRESHOLD = 0.95


def _is_near_duplicate(content: str, base_url: str, domain: str, threshold: float = _NEAR_DUP_THRESHOLD) -> bool:
    """查询 Palimpsest 是否已存在近似内容。任何异常均返回 False（fail-open）。"""
    try:
        resp = _http_post(f"{base_url}/mem/search", {
            "query": content, "scope": "memory", "domain": domain, "top_k": 1,
        })
        if "error" in resp:
            return False
        results = resp.get("results", [])
        if not results:
            return False
        score = results[0].get("score", 0)
        return score >= threshold
    except Exception:  # noqa: BLE001 —— 近似查询失败视为不重复，fail-open
        return False


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
            "tier": {"type": "string", "description": "记忆分层：facts(默认，只回事实层) / logs(只回日志层) / 空串(不过滤)"},
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

class PalimpsestMemoryProvider(MemoryProvider):
    """Palimpsest 记忆后端：语义召回 + 自动沉淀 + 图谱。"""

    pre_compress_checkpoint_api_version = 1

    def __init__(self) -> None:
        self._base_url = os.environ.get(
            "PALIMPSEST_BASE_URL", "http://127.0.0.1:8090"
        ).rstrip("/")
        self._domain = os.environ.get("PALIMPSEST_DOMAIN", "hermes")
        self._top_k = int(os.environ.get("PALIMPSEST_PREFETCH_TOP_K", "3"))
        # 注入降噪（T081 配套）：图邻居默认关（记忆域图近乎无边，纯空转）；
        # 注入最低相关度门槛可配，默认与旧硬编码一致 0.3，可调高再砍噪音。
        self._include_neighbors = (
            os.environ.get("PALIMPSEST_PREFETCH_NEIGHBORS", "false").lower() == "true"
        )
        self._min_score = float(os.environ.get("PALIMPSEST_PREFETCH_MIN_SCORE", "0.3"))
        # 记忆分层：默认只注入事实层，日志层（record/event/git_commit）不进上下文。
        self._tier = os.environ.get("PALIMPSEST_PREFETCH_TIER", "facts")
        # 召回范围：默认 memory（只召回记忆节点）。修 #46——旧默认 all 会让 kb_chunk
        # 混进注入池并挤占 top_k 名额（kb_chunk 的 domain 恒为 "kb"，domain 过滤对它
        # 无效，只有 scope=memory 能挡住）。需要把知识库切片一并注入时显式设为 all。
        self._scope = os.environ.get("PALIMPSEST_PREFETCH_SCOPE", "memory")
        self._auto_ingest = (
            os.environ.get("PALIMPSEST_AUTO_INGEST", "true").lower() != "false"
        )
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
            self._domain, self._base_url, self._auto_ingest,
        )

    def system_prompt_block(self) -> str:
        if not self._enabled:
            return ""
        return (
            "\n[Palimpsest 记忆层] 本会话已接入 Palimpsest 语义记忆。"
            "相关历史记忆会自动注入；可用 palimpsest_* 工具主动检索/写入/建图谱边。"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮召回相关记忆注入上下文；trivial 输入跳过（省一次 HTTP）。"""
        self._last_recall = None
        if not self._enabled or is_trivial_prompt(query):
            return ""
        if len((query or "").strip()) < 4:
            return ""
        resp = _http_post(f"{self._base_url}/mem/search", {
            "query": query, "scope": self._scope, "domain": self._domain,
            "top_k": self._top_k, "include_neighbors": self._include_neighbors,
            "tier": self._tier,
        })
        if "error" in resp or not resp.get("results"):
            return ""
        hits = [r for r in resp["results"] if r.get("score", 0) >= self._min_score]
        if not hits:
            return ""
        lines = ["[Palimpsest 记忆注入]"]
        for r in hits[: self._top_k]:
            lines.append(f"- ({r.get('score', 0):.2f}) {r.get('summary', '')[:150]}")
        self._last_recall = RecallStatus(provider_label="palimpsest", count=len(hits))
        return "\n".join(lines)

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
        """每轮沉淀：只在命中「明确指令」强信号时写入，避免库被低价值轮次污染。

        落库层归属：写入 ``type="record"``（logs 层），不是 facts 层。理由——这里抓到
        的是用户原话片段，属未经加工的对话记录，本就不是事实；facts 层留给人工/工具
        显式写入与提炼后的结论（见 ``on_session_end``）。与 #12 的分层意图一致。

        可审计性：命中词与命中位置写进 payload，事后可从节点内容解释它为何入库；
        同时落库正文带 300 字截断标记，避免「触发了但正文看不到证据」。
        """
        if not self._enabled or not self._auto_ingest:
            return
        if is_trivial_prompt(user_content) or not user_content:
            return
        m = _EXPLICIT_INSTRUCTION_RE.search(user_content)
        if not m:
            return
        importance = (
            0.7
            if any(k in user_content for k in ("不对", "不是这样", "错了", "纠正", "更正"))
            else 0.6
        )
        truncated = user_content[:300]
        note = "…[截断]" if len(user_content) > 300 else ""
        _http_post(f"{self._base_url}/mem/ingest", {
            "content": f"[对话沉淀] 用户: {truncated}{note}",
            "type": "record", "importance": importance,
            "domain": self._domain, "source": "hermes-sync_turn",
            "matched_keyword": m.group(0), "match_pos": m.start(),
        })

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """会话结束：把含强信号的消息提炼成一条要点。

        落库层归属：写入 ``type="memory"``（facts 层）。理由是这里产出的是**提炼后的
        结论**（多轮消息压缩成要点行），不是逐轮原始片段——它应当回到后续上下文，
        而非像 sync_turn 的原始对话片段那样沉进 logs 层。
        修 #47：旧实现写 ``type="record"``（logs 层），而 prefetch 默认 ``tier="facts"``，
        导致会话要点默认只写不读、静默失效。
        """
        if not self._enabled or not self._auto_ingest:
            return
        points = _extract_points(messages, limit=8, per_message_chars=150)
        if not points:
            return
        content = "会话要点（Palimpsest 插件提炼）：\n" + "\n".join(points)
        if _is_near_duplicate(content, self._base_url, self._domain):
            logger.info("Palimpsest: 跳过近似重复的会话要点")
            return
        _http_post(f"{self._base_url}/mem/ingest", {
            "content": content, "type": "memory", "importance": 0.55,
            "domain": self._domain, "source": "hermes-session_end",
        })

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """压缩前抽取要点，贡献给压缩 prompt（不写入 Palimpsest，只保上下文）。"""
        points = _extract_points(messages, limit=10, per_message_chars=200)
        return "\n".join(points)

    def shutdown(self) -> None:
        self._enabled = False

    # -- 工具 --------------------------------------------------------

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
        return _http_post(f"{self._base_url}/mem/search", {
            "query": args.get("query", ""), "scope": args.get("scope", "all"),
            "domain": args.get("domain", self._domain),
            "top_k": int(args.get("top_k", 5)),
            "include_neighbors": (
                str(args.get("include_neighbors", False)).lower() == "true"
            ),
            "tier": args.get("tier", self._tier),
        })

    def _tool_ingest(self, args: dict[str, Any]) -> dict:
        return _http_post(f"{self._base_url}/mem/ingest", {
            "content": args.get("content", ""), "type": args.get("type", "memory"),
            "importance": float(args.get("importance", 0.5)),
            "domain": args.get("domain", self._domain), "source": "hermes-tool",
        })

    def _tool_link(self, args: dict[str, Any]) -> dict:
        return _http_post(f"{self._base_url}/mem/link", {
            "source_id": int(args.get("source_id", 0)),
            "target_id": int(args.get("target_id", 0)),
            "relation": args.get("relation", "RELATED_TO"),
            "weight": float(args.get("weight", 0.9)),
            "bidirectional": (
                str(args.get("bidirectional", True)).lower() == "true"
            ),
        })

    def _tool_graph(self, args: dict[str, Any]) -> dict:
        return _http_post(f"{self._base_url}/graph/neighbors", {
            "node_id": int(args.get("node_id", 0)),
            "relation": args.get("relation", ""),
            "depth": int(args.get("depth", 1)),
            "limit": int(args.get("limit", 20)),
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
