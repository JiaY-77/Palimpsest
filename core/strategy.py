"""记忆策略引擎（strategy engine）
========================================

Palimpsest 的**记忆智能**住在这一层：判断「什么值得记、记哪层、什么时候召回、
怎么去重提炼」。它原先散落在 Hermes 接入插件（``hermes-plugin/__init__.py``）里，
被宿主插件的接口肢解——换宿主智能全丢。本模块把它收回本体。

设计原则
--------
- **策略引擎在本体，适配器薄到没有智能**：宿主适配器只做事件转发 + 结果注入；
  「召不召/抽不抽/记哪层」的判定全部在这里发生。
- **不依赖任何宿主**：本模块**不得** import ``agent.*`` 或任何 Hermes 专有模块。
  原本借自宿主的 ``is_trivial_prompt`` 已在此重实现（``_TRIVIAL_PROMPT_RE``）。
- **纯决策，不碰传输**：本模块接受「原始事件」，返回「决策结果」（可以含要注入的
  文本/要写入的内容），但**不自己发 HTTP、不开库**。存储访问由调用方（main.py 的
  端点）通过既有的 mcp_tools 函数完成。

三个决策入口（对应 lifecycle 协议的三端点）
-------------------------------------------
- :func:`decide_pre_turn`   —— 要不要召回、召回哪些、怎么排序（给 pre-turn）
- :func:`decide_post_turn`  —— 这轮要不要沉淀、写什么内容、写哪层（给 post-turn）
- :func:`decide_session_end`—— 会话要点提炼 + 分层（给 session-end）

分层归属（tier semantics）
--------------------------
- ``logs`` 层：逐轮抓到的**用户原话片段**（未加工），来自 post-turn 的强信号命中。
- ``facts`` 层：**提炼后的结论**（多轮压缩成要点），来自 session-end。
  这与「facts 是事实层、logs 是日志层」的既有约定一致，也修了历史 bug：会话要点
  曾误写 logs 层，导致 prefetch 默认 tier=facts 时「只写不读」而静默失效。
"""

from __future__ import annotations

import re
import time
from typing import Any

from core.secret_scan import scan_secret_classified

# ---------------------------------------------------------------------------
# 常量与规则（原 hermes-plugin/__init__.py L42-L59 下沉而来，语义不变）
# ---------------------------------------------------------------------------

# 强信号：命中即触发会话要点提炼（session-end）/ 压缩前抽取。
# 保守锚定中文语料：纠正、偏好、决策、规则、启动类动词。
# 注意：此正则偏「宽」——它的产出是「会话要点」（人工可读的提炼），不是逐条自动
# 落库的事实，所以容忍一定噪音。post-turn 的自动落库另用 _EXPLICIT_INSTRUCTION_RE，
# 两者刻意解耦。
_IMPORTANT_RE = re.compile(
    r"(记住|记好|以后|从今|别忘|不要忘|我的偏好|我更喜欢|我习惯|"
    r"不对|不是|错了|纠正|更正|改成|改为|"
    r"批准|决定|拍板|定案|方案|规则|规矩|红线|"
    r"开始做|启动|立项|安排|计划|下一步|优先)"
)

# post-turn 自动落库专用：只认「对助手的明确指令 / 长期偏好 / 纠正」，不认「操作动词」。
# 原因：`启动|安排|计划|方案|优先|决定|立项` 这类词在命令输出、构建日志、后台进程
# 通知里高频出现，宽正则会把噪音写进库；而 post-turn 是「关键词命中即把用户原话
# 落库」，误触发直接污染检索池。剔除操作词后，命中即可解释为「用户在下指令」，可审计。
_EXPLICIT_INSTRUCTION_RE = re.compile(
    r"(记住|记好|以后|从今|别忘|不要忘|别再|"
    r"我的偏好|我更喜欢|我习惯|我一般|我通常|"
    r"不对|不是这样|错了|纠正|更正|改成|改为|"
    r"规则|规矩|红线)"
)

# 纠正类词：命中则 importance 抬到 0.7（原插件 sync_turn 的 0.7/0.6 分档）。
_CORRECTION_WORDS = ("不对", "不是这样", "错了", "纠正", "更正")

# 会话要点（facts 层）的 importance。
_SESSION_FACTS_IMPORTANCE = 0.55

# 近似重复阈值：/mem/search top-1 与它的语义匹配分比较，≥ 视为已存在。
# fail-open：HTTP 错误 / 空结果 / 缺 score 一律视为「不重复」。
# 高阈值避免误杀「不同但相似」的记忆。
NEAR_DUP_THRESHOLD = 0.95

# 允许进入要点提炼的消息角色。
_ALLOWED_ROLES = ("user", "assistant")

# 默认参数（原插件各处的字面量，集中于此便于调参）
PRE_TURN_TOP_K = 5
PRE_TURN_MIN_SCORE = 0.0  # 原插件 _min_score 默认；0 = 不额外过滤
PRE_TURN_MIN_LEN = 4
POST_TURN_TRUNCATE = 300
SESSION_END_LIMIT = 8
SESSION_END_PER_MSG_CHARS = 150
PRE_COMPRESS_LIMIT = 10
PRE_COMPRESS_PER_MSG_CHARS = 200

# 本体的 trivial 判定（原借用宿主的 agent.memory_provider.is_trivial_prompt）。
# 语义对齐：空输入、slash 命令、裸寒暄/确认 → True。
_TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"hi|hey|hello|yo|sup|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)"
    r'[\s!?.:;,"\'~\u2018\u2019\u201c\u201d\u2014\u2013\u2026()\[\]{}<>*&^%$#@!+=`\u00a0]*$',
    re.IGNORECASE,
)


def is_trivial_prompt(text: str | None) -> bool:
    """空输入、slash 命令、裸寒暄/确认 → True（跳过召回省一次往返）。"""
    stripped = (text or "").strip()
    if not stripped or stripped.startswith("/"):
        return True
    return bool(_TRIVIAL_PROMPT_RE.match(stripped))


def extract_points(messages: list[dict[str, Any]], limit: int, per_message_chars: int) -> list[str]:
    """从消息列表提炼要点行。只接受 user/assistant 角色，去重，命中 _IMPORTANT_RE。"""
    points: list[str] = []
    seen: set = set()
    for msg in messages:
        if msg.get("role") not in _ALLOWED_ROLES:
            continue
        text = str(msg.get("content") or "")
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


# ---------------------------------------------------------------------------
# 决策一：pre-turn（召回）
# ---------------------------------------------------------------------------


def decide_pre_turn(
    user_message: str,
    *,
    hits: list[dict[str, Any]] | None = None,
    min_score: float = PRE_TURN_MIN_SCORE,
    top_k: int = PRE_TURN_TOP_K,
) -> dict[str, Any]:
    """判断本轮回什么、注入什么。

    ``hits`` 由调用方（端点）用既有的 /mem/search 逻辑取得——本函数只做**决策**：
    trivial 跳过、长度门槛、score 过滤、条数截断、注入文本格式化、优先级。

    返回::

        {
          "skip": bool,
          "skip_reason": str,          # skip=True 时有值
          "inject_text": str,          # 可直接拼进 prompt 的文本（空=不注入）
          "inject_blocks": [...],      # 结构化（content/priority/source）
          "decision_log": {...},       # 为什么召这些（可观测性）
        }
    """
    if is_trivial_prompt(user_message):
        return {
            "skip": True,
            "skip_reason": "trivial",
            "inject_text": "",
            "inject_blocks": [],
            "decision_log": {"reason": "trivial"},
        }
    if len((user_message or "").strip()) < PRE_TURN_MIN_LEN:
        return {
            "skip": True,
            "skip_reason": "too_short",
            "inject_text": "",
            "inject_blocks": [],
            "decision_log": {"reason": "too_short"},
        }

    candidates = hits or []
    filtered = [r for r in candidates if r.get("score", 0) >= min_score]
    kept = filtered[:top_k]
    if not kept:
        return {
            "skip": True,
            "skip_reason": "no_hit",
            "inject_text": "",
            "inject_blocks": [],
            "decision_log": {"candidates": len(candidates), "kept": 0},
        }

    lines = ["[Palimpsest 记忆注入]"]
    blocks: list[dict[str, Any]] = []
    for r in kept:
        score = r.get("score", 0)
        summary = r.get("summary", "")
        lines.append(f"- ({score:.2f}) {summary[:150]}")
        blocks.append(
            {
                "content": summary[:150],
                "priority": round(float(score), 4),
                "source": f"mem#{r.get('id')}" if r.get("id") is not None else "mem",
            }
        )
    return {
        "skip": False,
        "skip_reason": "",
        "inject_text": "\n".join(lines),
        "inject_blocks": blocks,
        "decision_log": {
            "candidates": len(candidates),
            "kept": len(kept),
            "min_score": min_score,
            "top_k": top_k,
        },
    }


# ---------------------------------------------------------------------------
# 决策二：post-turn（沉淀）
# ---------------------------------------------------------------------------


def decide_post_turn(user_message: str, *, auto_ingest: bool = True) -> dict[str, Any]:
    """判断这一轮要不要沉淀、写什么、写哪层。

    命中 ``_EXPLICIT_INSTRUCTION_RE`` 才写（保守，避免低价值轮次污染检索池）。
    分层：写 ``logs`` 层（type="record"）——抓到的是**用户原话片段**，未加工，
    不是事实；facts 层留给 session-end 的提炼结论与人工写入。

    返回::

        {
          "store": bool,
          "content": str,              # store=True 时 = 要写入的正文
          "type": str, "importance": float, "tier": str,
          "decision_log": {...},
        }
    """
    if not auto_ingest:
        return {"store": False, "decision_log": {"reason": "auto_ingest_off"}}
    if not user_message or is_trivial_prompt(user_message):
        return {"store": False, "decision_log": {"reason": "trivial_or_empty"}}
    m = _EXPLICIT_INSTRUCTION_RE.search(user_message)
    if not m:
        return {"store": False, "decision_log": {"reason": "no_strong_signal"}}

    importance = 0.7 if any(k in user_message for k in _CORRECTION_WORDS) else 0.6
    truncated = user_message[:POST_TURN_TRUNCATE]
    note = "…[截断]" if len(user_message) > POST_TURN_TRUNCATE else ""
    return {
        "store": True,
        "content": f"[对话沉淀] 用户: {truncated}{note}",
        "type": "record",
        "importance": importance,
        "tier": "logs",
        "decision_log": {
            "matched_keyword": m.group(0),
            "match_pos": m.start(),
            "importance": importance,
        },
    }


# ---------------------------------------------------------------------------
# 决策三：session-end（会话要点）
# ---------------------------------------------------------------------------


def decide_session_end(messages: list[dict[str, Any]], *, auto_ingest: bool = True) -> dict[str, Any]:
    """会话结束：把含强信号的消息提炼成一条要点，写 facts 层。

    分层修正（历史 bug #47）：旧实现写 type="record"（logs 层），而 prefetch
    默认 tier="facts"，导致会话要点「只写不读」而静默失效。本函数写 type="memory"
    （facts 层），让提炼结论回到后续上下文。

    返回::

        {
          "store": bool,
          "content": str, "type": str, "importance": float, "tier": str,
          "points": list[str],         # 供调用方做去重判断
          "decision_log": {...},
        }
    """
    if not auto_ingest:
        return {"store": False, "points": [], "decision_log": {"reason": "auto_ingest_off"}}
    points = extract_points(messages, limit=SESSION_END_LIMIT, per_message_chars=SESSION_END_PER_MSG_CHARS)
    if not points:
        return {"store": False, "points": [], "decision_log": {"reason": "no_points"}}
    content = "会话要点（Palimpsest 策略引擎提炼）：\n" + "\n".join(points)
    return {
        "store": True,
        "content": content,
        "type": "memory",
        "importance": _SESSION_FACTS_IMPORTANCE,
        "tier": "facts",
        "points": points,
        "decision_log": {"points": len(points), "limit": SESSION_END_LIMIT},
    }


# ---------------------------------------------------------------------------
# 决策四：pre-compress（压缩前抽取，不写库）
# ---------------------------------------------------------------------------


def decide_pre_compress(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """压缩前抽取要点，贡献给压缩 prompt（**不写入** Palimpsest，只保上下文）。"""
    points = extract_points(messages, limit=PRE_COMPRESS_LIMIT, per_message_chars=PRE_COMPRESS_PER_MSG_CHARS)
    return {"points": points, "points_text": "\n".join(points)}


def is_near_duplicate(hits: list[dict[str, Any]] | None, threshold: float = NEAR_DUP_THRESHOLD) -> bool:
    """给定 /mem/search(scope=memory, top_k=1) 的结果，判断是否已存在近似内容。

    fail-open：空结果 / 缺 score 一律 False。查询动作由调用方完成——本函数只做判断。
    """
    if not hits:
        return False
    score = hits[0].get("score", 0)
    return score >= threshold


# ---------------------------------------------------------------------------
# 决策五：context-enhance（压缩前图谱增强）
# 原 contextual 压缩引擎（hermes-plugin/context_engine.py）的 _extract_topics /
# _graph_enhancement 下沉而来。宿主只提供「将要压缩的消息 + 保护段参数」，
# 「挑哪些主题、查几条、怎么组装注入文本」的判定全部在这里。
# ---------------------------------------------------------------------------

# 注入段标题与格式常量（原 context_engine 内联字面量）。
GRAPH_ENHANCE_HEADER = "[Palimpsest 图谱要点（压缩前提炼）]"
GRAPH_TOPIC_MIN_CHARS = 8  # 短于此的寒暄/确认不作为图谱查询主题
GRAPH_TOPIC_TRUNCATE = 120  # 主题送检索前的截断长度
GRAPH_TOPIC_DISPLAY = 50  # 注入文本里主题展示截断
GRAPH_HIT_SUMMARY_DISPLAY = 120  # 注入文本里摘要展示截断
GRAPH_NEIGHBOR_TITLE_DISPLAY = 80  # 注入文本里邻居标题展示截断
GRAPH_ENHANCE_TOP_K = 2  # 每个主题取前几条记忆
GRAPH_ENHANCE_NEIGHBORS = 3  # 每个主题最多附几条图谱邻居
GRAPH_ENHANCE_MAX_TOPICS = 3  # 最多查询几个主题（宿主可用配置覆盖，上限 5）
GRAPH_ENHANCE_MAX_TOPICS_CAP = 5
GRAPH_ENHANCE_DEFAULT_BUDGET = 8.0  # 整条增强链路的默认耗时预算（秒）


def clamp_max_topics(value: Any) -> int:
    """把宿主的 ``PALIMPSEST_GRAPH_TOPICS`` 收敛到 [1, 5]（原插件行为）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = GRAPH_ENHANCE_MAX_TOPICS
    return max(1, min(n, GRAPH_ENHANCE_MAX_TOPICS_CAP))


def extract_graph_topics(
    messages: list[dict[str, Any]],
    focus_topic: str | None = None,
    *,
    protect_first_n: int = 3,
    protect_last_n: int = 6,
    max_topics: int = GRAPH_ENHANCE_MAX_TOPICS,
) -> list[str]:
    """从将被压缩的消息中挑出图谱查询主题。

    规则（与原 ``_extract_topics`` 逐条对齐，行为保持一致）：
    focus_topic 优先；否则取**中间段**（跳过 protect_first_n / protect_last_n
    保护段——那些不参与压缩）的 user 消息；内容短于 8 字符的寒暄跳过；
    靠后的意图优先；每条截断 120 字符；主题数封顶 ``max_topics``。
    消息太短（没有真正的中间段）→ 返回空列表（与原行为一致）。
    """
    topics: list[str] = []
    if focus_topic and str(focus_topic).strip():
        topics.append(str(focus_topic).strip())

    head = protect_first_n or 3
    tail = protect_last_n or 6
    if len(messages) - tail <= head:
        return []
    boundary = max(0, len(messages) - tail)
    candidates: list[str] = []
    for idx, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        if idx < head or idx >= boundary:
            continue  # 保护段不参与图谱提炼
        content = str(msg.get("content") or "").strip()
        if len(content) >= GRAPH_TOPIC_MIN_CHARS:
            candidates.append(content)
    for content in reversed(candidates):  # 靠后的意图优先
        if len(topics) >= max_topics:
            break
        topics.append(content[:GRAPH_TOPIC_TRUNCATE])
    return topics[:max_topics]


def format_graph_enhancement(
    entries: list[tuple[str, dict[str, Any], list[dict[str, Any]]]],
    *,
    max_topics: int = GRAPH_ENHANCE_MAX_TOPICS,
) -> str:
    """把「主题 → 关键记忆 + 图谱邻居」组装成注入文本；无命中返回空串。

    ``entries`` 由调用方（main.py 端点）用**进程内**检索装配，每条为
    ``(topic, top_hit, neighbors)``——本函数只管格式，不发请求。
    """
    lines: list[str] = []
    added = 0
    for topic, top, neighbors in entries:
        if added >= max_topics:
            break
        try:
            score = float(top.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        summary = str(top.get("summary", "") or "")[:GRAPH_HIT_SUMMARY_DISPLAY]
        lines.append(f"- 主题「{str(topic)[:GRAPH_TOPIC_DISPLAY]}」→ 关键记忆({score:.2f}): {summary}")
        for nb in (neighbors or [])[:GRAPH_ENHANCE_NEIGHBORS]:
            relation = nb.get("relation", "LINKED")
            title = str(nb.get("title", "") or "")[:GRAPH_NEIGHBOR_TITLE_DISPLAY]
            lines.append(f"  · 图谱关联: {relation} → {title}")
        added += 1
    if not added:
        return ""
    return "\n".join([GRAPH_ENHANCE_HEADER, *lines])


# MemoryHub 入籍时从 payload 剔除的本库运行时字段（保真优先，只剔这些）：
# 命中计数 / 时间戳 / 图谱度数 / 关联来源 / 敏感标记（新库 sweep 时重算）/
# 包内边列表 / 旧的 origin 字段（origin_id/origin_pack 由本函数重写）。
_IMPORT_DROP_FIELDS = (
    "hit_count",
    "last_hit_at",
    "num_edges",
    "linked_from",
    "secret_hint",
    "edges",
    "origin_id",
    "origin_pack",
)


def decide_import(
    pack: dict,
    *,
    select: list[int] | None = None,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """判定「把记忆包里的哪些节点入籍本库」（纯决策，不写库、不开库）。

    入参 ``pack`` = ``core.memoryhub.read_pack()`` 成功返回的 dict（含
    ``manifest`` / ``nodes`` / ``edges`` / ``issues`` / ``stats``）。

    设计要点（与读侧对称）：
      - **默认只读预览**：``select`` 为空 → 一个字节都不写，逐条记
        ``not_selected``，调用方据此给用户看可选清单。
      - **入籍须显式选中**：``select`` 非空时只处理被选中的原包 ``node_id``。
      - **不沿用原 node_id**：新库自己分配 id；原 id 记进 ``payload.origin_id``，
        来源包指纹/名字记进 ``payload.origin_pack``。
      - **边只保留两端都入籍的**。
      - **不做冲突检测 / 不取代本库已有记忆**——纯新增，避免误标 outdated。

    逐条判定顺序（命中即停）：``not_found`` → ``empty_content`` →
    ``secret_requires_confirm`` → ``not_active`` → 入籍。强敏感判定复用
    ``core.secret_scan.scan_secret_classified`` 的 ``strong`` 列表，与写入路径
    ``core.trivium_store.insert_node`` 的强拒判定保持一致。

    ``select`` 里的重复 id 只处理一次（保持调用方给的顺序）；非 int 元素静默跳过
    （既不处理也不进 ``skipped``）。
    """
    raw_nodes = pack.get("nodes", []) or []
    all_nodes = [item for item in raw_nodes if isinstance(item, dict)]
    node_index: dict[Any, dict] = {}
    for item in all_nodes:
        node_index.setdefault(item.get("node_id"), item)

    type_counts: dict = {}
    for item in all_nodes:
        payload = item.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        ntype = payload.get("type", "unknown")
        type_counts[ntype] = type_counts.get(ntype, 0) + 1

    nodes: list[dict] = []
    skipped: list[dict] = []
    imported_origin_ids: set = set()
    skip_reasons: dict = {}
    mode = "preview"
    selected = 0

    def _skip(origin_id, reason: str) -> None:
        skipped.append({"origin_id": origin_id, "reason": reason})
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    if not select:
        for item in all_nodes:
            skipped.append({"origin_id": item.get("node_id"), "reason": "not_selected"})
        skip_reasons["not_selected"] = len(all_nodes)
    else:
        mode = "import"
        seen: set = set()
        for origin_id in select:
            if not isinstance(origin_id, int) or isinstance(origin_id, bool):
                continue  # 非 int 静默跳过
            if origin_id in seen:
                continue
            seen.add(origin_id)
            selected += 1

            matched = node_index.get(origin_id)
            if matched is None:
                _skip(origin_id, "not_found")
                continue

            item_payload = matched.get("payload") or {}
            if not isinstance(item_payload, dict):
                item_payload = {}

            content = item_payload.get("content")
            if content is None or not str(content).strip():
                _skip(origin_id, "empty_content")
                continue

            if not include_secrets and scan_secret_classified(str(content))["strong"]:
                _skip(origin_id, "secret_requires_confirm")
                continue

            status = item_payload.get("status")
            if status is not None and status != "active":
                _skip(origin_id, "not_active")
                continue

            # 入籍：payload 浅拷贝 → 剔运行时字段 → 写 origin 字段
            new_payload = dict(item_payload)
            for key in _IMPORT_DROP_FIELDS:
                new_payload.pop(key, None)
            new_payload["origin_id"] = origin_id
            manifest = pack.get("manifest", {}) or {}
            new_payload["origin_pack"] = manifest.get("fingerprint") or manifest.get("name") or ""
            if new_payload.get("created_at") is None:
                new_payload["created_at"] = time.time()
            nodes.append({"origin_id": origin_id, "payload": new_payload})
            imported_origin_ids.add(origin_id)

    edges: list[dict] = []
    for edge in pack.get("edges", []) or []:
        if not isinstance(edge, dict):
            continue
        src = edge.get("source_id")
        tgt = edge.get("target_id")
        if src in imported_origin_ids and tgt in imported_origin_ids:
            edges.append(
                {
                    "source_origin_id": src,
                    "target_origin_id": tgt,
                    "label": edge.get("label", ""),
                    "weight": edge.get("weight"),
                }
            )

    return {
        "nodes": nodes,
        "skipped": skipped,
        "edges": edges,
        "decision_log": {
            "mode": mode,
            "node_total": len(all_nodes),
            "selected": selected,
            "to_import": len(nodes),
            "skipped_total": len(skipped),
            "skip_reasons": skip_reasons,
            "type_counts": type_counts,
        },
    }


# ---------------------------------------------------------------------------
# 决策六：thought skeleton（pre-turn 给侧——思考骨架）
#
# 「思维预设层」的给侧：宿主每轮模型调用前来取一份思考骨架拼进 prompt，
# 引导模型把结构化思考写在正文里（写在正文 → 可解析 → post-turn 可抽取）。
# 比静态预设高一级的地方：骨架按当前轮的**情境**动态挑选——写代码给失败
# 路径骨架、调研给证据/反例骨架、普通轮给通用骨架、闲聊不给。判定全在这里；
# 骨架生成不接 LLM（那是宿主的活），只在固定模板里挑。
# ---------------------------------------------------------------------------

# 情境分类正则（关键词启发式）。对自由文本做情境归类没有结构位置可用，
# 关键词是唯一判据；这里刻意收窄到「基本只出现在对应情境」的词，且分类
# 结果只影响**骨架文案**（不碰召回 / 不碰写入），误分类的代价只是骨架
# 不那么贴切，不污染任何存储。顺序：code 先于 research（调试型调研按
# 代码情境给失败路径骨架更贴切）。
_THOUGHT_CODE_RE = re.compile(
    r"(代码|函数|脚本|接口|端点|模块|重构|修复|报错|堆栈|"
    r"traceback|exception|bug|编译|部署|单元测试|测试用例)",
    re.IGNORECASE,
)
_THOUGHT_RESEARCH_RE = re.compile(r"(调研|评估|对比|选型|可行性|证据|论文|文献|竞品|查证|核实)")

# 骨架模板：``<thought>`` 起始标记 + 编号引导行，宿主原样拼进 prompt。
# 模型在骨架行后续写，写出的思考天然带标记 → post-turn 的 extract_thought
# 可解析（与 decide_thought_store 的正则对称）。
#
# 「骨架即胶囊」（2026-10-27）：每一问重写成**会话胶囊的一节**——四个编号答案
# 由 post-turn 的 parse_thought_sections 按编号映射成结构化字段（见
# _SKELETON_FIELD_MAP），累进会话缓冲，session-end 合并成一条
# type=task kind=handoff 胶囊。文案同时保持对「决策四问」的可读性。
# 统一骨架（2026-10-28 设计定案，issue #188）：**四问固定对应胶囊四节**。
#
# 历史包袱：原分 code / research / default 三套骨架，字段名各不相同
# （change/failure_path/verify/deliverable、question/evidence/counterexample/
# conclusion、goal/known_unknown/judgement/output），而胶囊四节是
# thought/outcome/next/pitfalls —— 任何单骨架轮都填不满四节（default 没有
# change/failure_path，code 没有 judgement/goal），实测全库自动胶囊只有
# 【思路】有正文、另三节恒为「（待补）」。
#
# 设计意图（主人原话）：「每轮对话……都是顺手的事情，填一次呗」。填写成本
# 必须极低、且每轮都该填得满，所以不再按场景换字段名。`kind` 保留为**归因
# 标记**（thought_origin 仍能区分当时判成哪一类），骨架正文三套共用一份。
_CAPSULE_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("thought", "这一轮的核心判断 / 思路（一句话）"),
    ("outcome", "做了什么（改了什么 / 查清了什么，带可核验句柄：PR 号 / commit / 路径）"),
    ("next", "下一步 / 待办"),
    ("pitfalls", "踩的坑 / 风险"),
)

_SKELETON_BODY: str = (
    "<thought>\n"
    + "".join(f"{idx}. {question}：\n" for idx, (_, question) in enumerate(_CAPSULE_QUESTIONS, 1))
    + "</thought>"
)

_THOUGHT_SKELETONS: dict[str, str] = {
    "code": _SKELETON_BODY,
    "research": _SKELETON_BODY,
    "default": _SKELETON_BODY,
}

# 每类骨架的编号问题 → 胶囊字段名。统一后三套相同；保留 dict 形状是为了
# `normalize_skeleton_kind` 的历史 kind 仍能解析（老缓冲节点里存着旧字段名的
# 轮次，compose_session_capsule 另有兼容取值，见该函数）。
_SKELETON_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "code": tuple(field for field, _ in _CAPSULE_QUESTIONS),
    "research": tuple(field for field, _ in _CAPSULE_QUESTIONS),
    "default": tuple(field for field, _ in _CAPSULE_QUESTIONS),
}

THOUGHT_SKELETON_MIN_LEN = 4  # 与召回门槛一致：过短输入没有思考可引导，不给骨架


def decide_thought_skeleton(user_message: str, *, enabled: bool = True) -> dict[str, Any]:
    """按当前轮情境挑一份思考骨架（纯决策，不接 LLM、不发请求）。

    返回::

        {"skeleton": str, "kind": str, "reason": str}

    - ``kind``：``"" / "code" / "research" / "default"`` —— skeleton 为空时
      kind 也为空（宿主可观测「为什么没给」）。
    - trivial / 空 / 过短 / 宿主关闸（``enabled=False``）→ ``skeleton=""``：
      闲聊轮没有思考可引导，注入骨架只是噪音。
    """
    if not enabled:
        return {"skeleton": "", "kind": "", "reason": "disabled"}
    stripped = (user_message or "").strip()
    if not stripped:
        return {"skeleton": "", "kind": "", "reason": "empty"}
    if is_trivial_prompt(stripped):
        return {"skeleton": "", "kind": "", "reason": "trivial"}
    if len(stripped) < THOUGHT_SKELETON_MIN_LEN:
        return {"skeleton": "", "kind": "", "reason": "too_short"}
    if _THOUGHT_CODE_RE.search(stripped):
        return {"skeleton": _THOUGHT_SKELETONS["code"], "kind": "code", "reason": "matched:code"}
    if _THOUGHT_RESEARCH_RE.search(stripped):
        return {"skeleton": _THOUGHT_SKELETONS["research"], "kind": "research", "reason": "matched:research"}
    return {"skeleton": _THOUGHT_SKELETONS["default"], "kind": "default", "reason": "default_skeleton"}


# ---------------------------------------------------------------------------
# 决策七：thought 抽取（post-turn 收侧——思维底片落库）
#
# 「思维预设层」的收侧：assistant 消息里的 ``<thought>...</thought>`` 段是
# 模型被骨架引导出的结构化思考（写在正文 → 宿主无需暴露原生 CoT，所有
# 模型通用）。抽出来落库 ``type="thought"``、``tier="logs"``——过程记录，
# 不是提炼事实；升格 facts/rule 是将来的显式治理动作，因此 thought 刻意
# **不**进 POLICY_PROTECTED_TYPES（保持可修可删）。
# ---------------------------------------------------------------------------

THOUGHT_TYPE = "thought"
THOUGHT_TIER = "logs"
THOUGHT_MIN_CHARS = 20  # 短于此 = 空壳思考（模型只回吐了骨架没续写），不落库
THOUGHT_MAX_CHARS = 4000  # 底片截断上限（过程快照不是长文，远低于通用写入上限）
THOUGHT_IMPORTANCE = 0.5  # 基础档：值得翻回来，但未经验证
THOUGHT_IMPORTANCE_DECISIVE = 0.7  # 决断档：思考里写下了被定下的判断 / 根因

# ---------------------------------------------------------------------------
# 来源标记（skeleton_kind / thought_origin）—— 思维底片「从哪来」的两个字段
#
# 落库的 thought 节点带两个互补字段：
#   - ``skeleton_kind``（事实字段）：本轮给侧**实际注入**的骨架类型（"" / code /
#     research / default），由宿主随 post-turn 上报——只有宿主知道注入了什么。
#   - ``thought_origin``（启发式字段）：按落库文本的**开头形态**判定
#     （skeleton / native / unknown）——本体只看得到文本。
#
# 诚实边界：宿主侧无法区分「骨架引导出的产物」与「模型原生思考」（思考原文
# 同走一个字段，两者不可辨）。字段并存的贡献在于**交叉核对**：kind 说「注入了
# 什么」，origin 说「文本长得像什么」，两者不一致即可查出「注入了骨架但模型
# 没照写」；单独任何一个都不声称精确归因。
# ---------------------------------------------------------------------------

THOUGHT_NOTE_PREFIX = "[思维底片]"  # 落库 content 的包裹前缀（比对前先剥掉）

ORIGIN_SKELETON = "skeleton"
ORIGIN_NATIVE = "native"
ORIGIN_UNKNOWN = "unknown"


def _fold_punctuation(text: str) -> str:
    """全/半角标点与全角空格归一（形态启发式的合理鲁棒性，不追求逐字相等）。"""
    return text.replace("（", "(").replace("）", ")").replace("：", ":").replace("\u3000", " ")


# skeleton_kind 的合法取值 = 骨架模板的 kind 集合；白名单之外（含未知值）归空串。
THOUGHT_SKELETON_KINDS: tuple[str, ...] = tuple(_THOUGHT_SKELETONS)

# 各骨架的起手式 = 模板第二行（编号引导行），全/半角归一后缓存：与
# _THOUGHT_SKELETONS 同源派生——模板改了判据自动跟随，不另立一套。
_SKELETON_OPENERS_FOLDED: dict[str, str] = {
    kind: _fold_punctuation(tpl.splitlines()[1].strip()) for kind, tpl in _THOUGHT_SKELETONS.items()
}

# 统一骨架之前的起手式（三套旧模板的首行），**只用于历史归因**。
#
# `detect_thought_origin` 是形态启发式：拿文本开头比对骨架起手式。2026-10-28
# 统一骨架后，库里既有的 thought 节点（旧模板产物）开头对不上新起手式，会被误
# 判成 `native`。这里把旧文案并进判据，向后兼容历史节点——新节点用新起手式，
# 老节点仍能识别成 `skeleton`。旧文案只读不写（不再注入），可随历史节点老化后移除。
_LEGACY_SKELETON_OPENERS: tuple[str, ...] = (
    "1. 本轮改什么 / 新增什么（一句话）：",
    "1. 要查清的问题（一句话）：",
    "1. 要的是什么（一句话）：",
)

_LEGACY_SKELETON_OPENERS_FOLDED: tuple[str, ...] = tuple(
    _fold_punctuation(line.strip()) for line in _LEGACY_SKELETON_OPENERS
)


def normalize_skeleton_kind(value: str) -> str:
    """归一 ``skeleton_kind`` 事实字段：合法 kind 之外（含空白 / 未知值）一律空串。"""
    v = str(value or "").strip()
    return v if v in THOUGHT_SKELETON_KINDS else ""


def detect_thought_origin(text: str) -> str:
    """形态启发式：按文本开头判断这条思考「长得像」哪种骨架的产物。

    返回 ``"skeleton"`` / ``"native"`` / ``"unknown"``。

    判据：剥掉可选的 ``[思维底片]`` 包裹前缀与前导空白后，开头是否前缀匹配
    某个骨架模板的引导行（起手式，直接从 ``_THOUGHT_SKELETONS`` 派生，不另立
    一套；全/半角标点与空白轻度归一后比对）。空文本 / 只有前缀 / 非字符串等
    异常输入 → ``"unknown"``（判定所需信息不存在，不猜测）。

    **诚实边界（必读）**：这是**形态启发式**，不是精确归因——宿主侧无法区分
    「骨架引导出的产物」与「模型恰好写出同构开头的原生思考」。与事实字段
    ``skeleton_kind``（本轮实际注入了哪类骨架）并存：kind 说「注入了什么」，
    origin 说「文本长得像什么」，两者交叉可查「注入了骨架但模型没照写」；
    单独任何一个都不声称精确归因。
    """
    try:
        stripped = str(text or "").strip()
    except Exception:  # noqa: BLE001 —— 异常输入按「无法判定」处理
        return ORIGIN_UNKNOWN
    if stripped.startswith(THOUGHT_NOTE_PREFIX):
        stripped = stripped[len(THOUGHT_NOTE_PREFIX) :].strip()
    if not stripped:
        return ORIGIN_UNKNOWN
    folded = _fold_punctuation(stripped)
    # 新骨架起手式 + 历史旧文案（向后兼容库里 2026-10-28 之前的 thought 节点）
    for opener in (*_SKELETON_OPENERS_FOLDED.values(), *_LEGACY_SKELETON_OPENERS_FOLDED):
        if folded.startswith(opener):
            return ORIGIN_SKELETON
    return ORIGIN_NATIVE


# 抽取正则：非贪婪 + DOTALL（思考常跨行）。非递归——嵌套时取第一个闭合段
# （内层标记原样保留），不做深度解析。
_THOUGHT_BLOCK_RE = re.compile(r"<thought>(.*?)</thought>", re.DOTALL)
_THOUGHT_OPEN_RE = re.compile(r"<thought>")

# 决断档信号：这条思考里含「被修正 / 被定下的判断 / 根因」——将来翻回来
# 最有价值的那类（与 decide_post_turn 的纠正分档同一思路，词表按思考文本
# 的语言习惯另配）。
_THOUGHT_DECISIVE_RE = re.compile(r"(不对|不是这样|错了|纠正|更正|改成|改为|拍板|定案|决定了|结论是|根因)")


def extract_thought(message: str) -> dict[str, Any]:
    """从 assistant 消息解析 ``<thought>...</thought>`` 段（纯解析，不判价值）。

    行为定义（边界样例均有测试钉住）：

    - 段出现在正文**任意位置**（不要求开头）都抽得到；
    - 多段：取**第一段**完整闭合段（一轮一条底片），``segments`` 记总数；
    - 嵌套（段内还有 ``<thought>``）：不递归解析，取第一个闭合段原样，
      ``nested=True`` 供观测；
    - 未闭合（有开标记无闭标记）：``found=False, unclosed=True``——被截断的
      思考内容不完整，保守不落库（decide_thought_store 据此拒绝）。
    """
    text = message or ""
    match = _THOUGHT_BLOCK_RE.search(text)
    if match is None:
        return {
            "found": False,
            "text": "",
            "segments": 0,
            "nested": False,
            "position": -1,
            "unclosed": bool(_THOUGHT_OPEN_RE.search(text)),
        }
    first = match.group(1).strip()
    return {
        "found": True,
        "text": first,
        "segments": len(_THOUGHT_BLOCK_RE.findall(text)),
        "nested": "<thought>" in first,
        "position": match.start(),
        "unclosed": False,
    }


def decide_thought_store(
    assistant_message: str,
    *,
    auto_ingest: bool = True,
    session_id: str = "",
    skeleton_kind: str = "",
) -> dict[str, Any]:
    """判定这轮的思考底片要不要落库、怎么落（post-turn 收侧，纯决策）。

    落库约定：``type="thought"``、``tier="logs"``、``source`` 带生命周期来源
    标识（``lifecycle:post-turn[:session_id]``）。importance 分两档可解释：

    - 基础 ``0.5``——未经验证的过程思考；
    - 决断档 ``0.7``——思考里写下决断信号（被定下的判断 / 根因 / 纠正），
      将来翻回来最值钱的那类。

    来源标记（随返回值透出，由执行层写进 payload）：

    - ``skeleton_kind``——事实字段：本轮给侧注入的骨架类型（宿主上报；
      白名单外归空串，见 :func:`normalize_skeleton_kind`）；
    - ``thought_origin``——启发式字段：按抽出的思考文本开头判定
      （:func:`detect_thought_origin`，诚实边界见其 docstring）；无文本
      （未命中 / 未闭合 / 空段 / 关闸）时为 ``"unknown"``。

    返回::

        {
          "store": bool, "found": bool, "content": str, "type": str,
          "tier": str, "importance": float, "source": str,
          "skeleton_kind": str, "thought_origin": str,
          "skip_reason": str,   # "" / not_found / unclosed / empty /
                                # too_thin / auto_ingest_off
          "decision_log": {...},
        }

    ``found`` 只反映「解析到完整闭合段」（含未落库的情形，如空段 / 过短 /
    关闸），供宿主观测「模型到底写没写思考」。
    """
    kind = normalize_skeleton_kind(skeleton_kind)
    if not auto_ingest:
        return {
            "store": False,
            "found": False,
            "content": "",
            "type": THOUGHT_TYPE,
            "tier": THOUGHT_TIER,
            "importance": 0.0,
            "source": "",
            "skeleton_kind": kind,
            "thought_origin": ORIGIN_UNKNOWN,
            "skip_reason": "auto_ingest_off",
            "decision_log": {"reason": "auto_ingest_off"},
        }

    parsed = extract_thought(assistant_message)
    log: dict[str, Any] = {
        "segments": parsed["segments"],
        "nested": parsed["nested"],
        "position": parsed["position"],
    }
    if not parsed["found"]:
        reason = "unclosed" if parsed["unclosed"] else "not_found"
        return {
            "store": False,
            "found": False,
            "content": "",
            "type": THOUGHT_TYPE,
            "tier": THOUGHT_TIER,
            "importance": 0.0,
            "source": "",
            "skeleton_kind": kind,
            "thought_origin": ORIGIN_UNKNOWN,
            "skip_reason": reason,
            "decision_log": log,
        }

    text = parsed["text"]
    if not text:
        log["reason"] = "empty"
        return {
            "store": False,
            "found": True,
            "content": "",
            "type": THOUGHT_TYPE,
            "tier": THOUGHT_TIER,
            "importance": 0.0,
            "source": "",
            "skeleton_kind": kind,
            "thought_origin": ORIGIN_UNKNOWN,
            "skip_reason": "empty",
            "decision_log": log,
        }
    if len(text) < THOUGHT_MIN_CHARS:
        log["reason"] = "too_thin"
        return {
            "store": False,
            "found": True,
            "content": "",
            "type": THOUGHT_TYPE,
            "tier": THOUGHT_TIER,
            "importance": 0.0,
            "source": "",
            "skeleton_kind": kind,
            "thought_origin": detect_thought_origin(text),
            "skip_reason": "too_thin",
            "decision_log": log,
        }

    importance = THOUGHT_IMPORTANCE_DECISIVE if _THOUGHT_DECISIVE_RE.search(text) else THOUGHT_IMPORTANCE
    truncated = text[:THOUGHT_MAX_CHARS]
    note = "…[截断]" if len(text) > THOUGHT_MAX_CHARS else ""
    source = f"lifecycle:post-turn:{session_id}" if session_id else "lifecycle:post-turn"
    origin = detect_thought_origin(text)
    log["chars"] = len(text)
    log["importance"] = importance
    log["thought_origin"] = origin
    return {
        "store": True,
        "found": True,
        "content": f"{THOUGHT_NOTE_PREFIX} {truncated}{note}",
        "type": THOUGHT_TYPE,
        "tier": THOUGHT_TIER,
        "importance": importance,
        "source": source,
        "skeleton_kind": kind,
        "thought_origin": origin,
        "skip_reason": "",
        "decision_log": log,
    }


def backfill_thought_origin(store, dry_run: bool = True) -> dict:
    """存量 thought 节点回填 ``thought_origin``（同一判据，不另立一套）。

    只处理 ``type=thought`` 且**缺** ``thought_origin`` 键的节点：

    - 幂等判据 = 键存在即视为已处理，**已有值一律不覆盖**（包括人工改过的值），
      ``--apply`` 后复跑 0 变更；
    - ``skeleton_kind`` 是给侧上报的事实字段，存量节点**无从得知**——不回填，
      缺着比编造诚实；
    - ``thought_origin`` 的判定与诚实边界见 :func:`detect_thought_origin`
      （形态启发式，非精确归因）。

    ``dry_run=True``（默认）只统计不写库；``dry_run=False`` 才落盘。

    **两段式（先快照后写）**：``iter_nodes`` 在整个迭代期间持有库连接，
    迭代中调 ``update_payload`` 会在同进程内重入取锁而自锁——因此先把候选
    节点整体快照出来、迭代结束（连接释放）后再逐个写回（与
    ``core.task_state.backfill_task_state`` 同款约束）。

    返回 ``{dry_run, scanned, already, changed, distribution, changes}``；
    ``distribution`` 为扫描到的全部 thought 节点按 origin 的分布（已带值的
    按现有值计入，缺省的按将写入的值计入），复跑时分布依旧完整。
    """
    scanned = 0
    already = 0
    distribution: dict[str, int] = {
        ORIGIN_SKELETON: 0,
        ORIGIN_NATIVE: 0,
        ORIGIN_UNKNOWN: 0,
    }
    pending: list[tuple[int, str]] = []  # (node_id, 将写入的 origin)，迭代结束再写

    for nid, node in store.iter_nodes():
        payload = node.get("payload") or {}
        if payload.get("type") != THOUGHT_TYPE:
            continue
        scanned += 1
        if "thought_origin" in payload:
            already += 1
            existing = str(payload.get("thought_origin") or ORIGIN_UNKNOWN)
            distribution[existing] = distribution.get(existing, 0) + 1
            continue
        origin = detect_thought_origin(payload.get("content") or "")
        distribution[origin] = distribution.get(origin, 0) + 1
        pending.append((nid, origin))

    if not dry_run:
        for nid, origin in pending:
            store.update_payload(nid, {"thought_origin": origin})

    return {
        "dry_run": dry_run,
        "scanned": scanned,
        "already": already,
        "changed": len(pending),
        "distribution": distribution,
        "changes": [{"id": nid, "thought_origin": origin} for nid, origin in pending],
    }


# ---------------------------------------------------------------------------
# 会话胶囊（「骨架即胶囊」收口）：post-turn 累进缓冲 → session-end 合并成胶囊
#
# 骨架问的四个问题本身就是一轮的决策记录，答案不该被扔掉。post-turn 把每轮
# <thought> 的编号答案解析成结构化字段（parse_thought_sections），经
# decide_session_buffer 判定后累进该 session 的**缓冲节点**（type="session"，
# 同一 session_id 只维护一条；落库由执行层 main.py 完成——本模块纯决策不碰
# 存储）。会话结束时 compose_session_capsule 把缓冲里的 turns 合并成一条
# type=task + kind=handoff 的「会话胶囊」，由既有交接通道
# （list_active_tasks(include_handoff=True)）供新会话先读。
# 全部纯函数：不接库、不接 LLM；解析失败一律返回空、绝不抛异常。
# ---------------------------------------------------------------------------

# 编号行：前导空白 + 数字 + 分隔符（半角点 / 顿号 / 全角点）+ 同行剩余内容。
# 数字超出字段映射范围（如 5+）只作边界（该段内容不归属任何字段）。
_THOUGHT_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\s*[.、．]\s*(.*)$")

# 会话缓冲节点（type="session"）的约定字段与档位：过程簿记，logs 层语义
# （不进 facts 默认注入池，见 config.TIER_LOGS 的 session 登记）。
SESSION_BUFFER_TYPE = "session"
SESSION_BUFFER_STATUS_ACTIVE = "active"  # 会话中；session-end 合并后改 closed
SESSION_BUFFER_STATUS_CLOSED = "closed"
SESSION_BUFFER_IMPORTANCE = 0.3  # 簿记节点，低于普通过程记录
SESSION_BUFFER_RAW_MAX_CHARS = 4000  # 缓冲里每轮 thought 原文的截断上限

CAPSULE_THOUGHT_MAX_CHARS = 300  # 胶囊「思路」节总长上限（纯截断，超出部分丢弃）
CAPSULE_SECTION_TBD = "（待补）"  # 空节占位正文（不猜、不编，如实标待补）

# 胶囊四节固定顺序（content 节序与 pending 列表都按它）。
_CAPSULE_SECTION_ORDER = ("thought", "outcome", "next", "pitfalls")

# next 节的「下一步」信号词：句子里带这些字样才算交接需要的事（任务书口径）。
_CAPSULE_NEXT_MARKERS = ("下一步", "待", "需", "再")

# ---------------------------------------------------------------------------
# 实质轮筛选（issue #170，胶囊第二刀）：会话缓冲只收「实质轮」
#
# 为什么是这三条判据（任一满足即实质轮）：三条分别覆盖「干活动作」「思考
# 深度」「任务指派」三种实质性的来源，互为兜底——宿主可能拿不到工具信号
# （老宿主，判据 1 失效），模型可能没照骨架写长思考（判据 2 失效），用户可
# 能只丢一句话就指望动手（判据 3 失效）。短轮（打招呼 / 确认 / 一句话回复）
# 三条全不中，被挡在缓冲外，胶囊不再被噪声塞满：
#
#   1. ~~该轮有工具调用~~——**已删除**（2026-10-28，issue #194）：宿主侧从不填
#      ``tool_calls_count``（恒 0），而判据 2 放宽后空 ``sections`` 在更上游就被
#      ``no_sections`` 挡掉，这条判据根本到不了。字段与代码一起删掉，不留装饰。
#   2. 填了胶囊格子（``sections`` 任一非空）——统一骨架（issue #188）后四问固定
#      对应四节，填一格就是一次有效填写。**2026-10-28 放宽**：旧规则是「正文
#      ≥ ``SESSION_BUFFER_MIN_THOUGHT_CHARS`` 字 **且** 落在结论字段上」，与
#      「每轮顺手填一次」的设计意图冲突——实测全库缓冲 turns 恒为 1、自动胶囊
#      只产 3 条，模型顺手填的短轮全被挡在门外；
#   3. 用户消息含指令动词（词表见 ``_SUBSTANTIVE_ZH_VERBS`` / 正则）——
#      「做/改/查/写/跑/验证/修/提/生成/整理/分析/对比」等执行类请求。指令
#      ≠ 问句：整句是疑问形态（「要怎么改这个配置？」）不影响判定，问句里
#      带指令动词也算指派了任务。
#
# 三条判据的现实分工（2026-10-28 复核）：判据 2 放宽后，「sections 非空」是
# 缓冲的主要入口；`decide_session_buffer` 对**空 sections 的轮**更早就返回
# ``no_sections``（缓冲只存结构化思考），所以判据 1/3 在 REST 链路里到不了——
# 它们只剩直接调用 `is_substantive_turn` 时的意义（测试与外部调用方），以及
# 将来允许「无结构化思考但有工具调用」的轮进缓冲时的扩展位。
# ---------------------------------------------------------------------------

# **已废弃**（2026-10-28）：判据 2 不再看正文长度。保留常量只是为了让
# `is_substantive_turn` / `decide_session_buffer` 的签名与 Config 项继续可用，
# 下个大版本连同 `Config.SESSION_BUFFER_MIN_THOUGHT_CHARS` 一起删除。
SESSION_BUFFER_MIN_THOUGHT_CHARS = 400

# 判据 3 的指令动词词表（任务书口径，不做同义词扩表）。中文按子串匹配
# （单字动词无法用词边界），因此**偏宽松**：「我做了个梦」也会命中「做」。
# 这是有意的取向——判据 3 的作用是补上宿主没有工具信号时的缺口，宁可多收
# 一条，也不漏掉真正的任务指派；拦寒暄实际靠更上游的骨架注入（trivial 轮
# 拿不到骨架，模型就不会按编号作答 → sections 空 → no_sections）。英文用词边界正则（``\bdo\b`` 不会误伤 docker / doing /
# dot）。
_SUBSTANTIVE_ZH_VERBS: tuple[str, ...] = (
    "做",
    "改",
    "查",
    "写",
    "跑",
    "验证",
    "修",
    "提",
    "生成",
    "整理",
    "分析",
    "对比",
)
_SUBSTANTIVE_EN_VERB_RE = re.compile(
    r"\b(?:do|make|fix|add|check|run|verify|write|build|create|review|analyze)\b",
    re.IGNORECASE,
)


def _question_stem(question: str) -> str:
    """取问句主干：第一个括号之前的部分（``做了什么（改了什么 / 查清了什么）`` → ``做了什么``）。"""
    return re.split(r"[（(]", question, maxsplit=1)[0].strip().rstrip("：: ")


def _strip_question_echo(value: str, question: str) -> str:
    """剥掉答案开头对问句的**回显**（``做了什么：新增…`` → ``新增…``）。

    骨架问句是固定的四句（``_CAPSULE_QUESTIONS``），所以这是**模板匹配**而非猜测：
    只有行首命中问句主干 + 分隔冒号才剥，其余一律原样返回。少剥只是难看，多剥会
    吃内容，因此这里刻意保守——括号里的补充说明不参与匹配（填表人通常只回显主干）。
    """
    stem = _question_stem(question)
    if not stem:
        return value
    matched = re.match(rf"^\s*{re.escape(stem)}\s*(?:[（(][^）)\n]*[）)])?\s*[：:]\s*", value)
    return value[matched.end() :].strip() if matched else value


def parse_thought_sections(text: str, skeleton_kind: str) -> dict[str, str]:
    """把 ``<thought>`` 段里的编号答案解析成「胶囊字段 → 文本」（纯解析）。

    - 按 ``_SKELETON_FIELD_MAP[skeleton_kind]`` 的字段名顺序映射编号 1..N；
      skeleton_kind 为空或不在映射表 → 用 ``default`` 的映射。
    - 每个字段值 = 「该编号行之后、下一个编号行之前」的文本（strip 后）；编号行
      形如 ``1. 标签：答案``——编号之后同行的内容取到，但**开头的问句回显会被剥掉**
      （2026-10-28：原先原样保留，导致胶囊每节都是「【做了什么】做了什么：…」的
      双重前缀；问句固定四句，故按 `_CAPSULE_QUESTIONS` 精确匹配，剥不到就不剥，
      绝不改写答案正文）。编号行本身的分隔符认 ``.`` / ``、`` / ``．``，
      容忍前导空白；行内冒号全/半角均常见，但不作为编号分隔符。
    - 缺编号 / 空值 → 该字段不出现（不猜、不补默认文案）；同编号重复出现 →
      后见覆盖先见。
    - 输入无 ``<thought>`` 段或解析不出任何字段 → 返回 ``{}``（非字符串同样
      ``{}``，绝不抛异常）。
    """
    if not isinstance(text, str):
        return {}
    match = _THOUGHT_BLOCK_RE.search(text)
    if match is None:
        return {}
    fields = _SKELETON_FIELD_MAP.get(
        normalize_skeleton_kind(skeleton_kind) or "default",
        _SKELETON_FIELD_MAP["default"],
    )
    sections: dict[str, str] = {}
    current: str | None = None
    parts: list[str] = []

    def _flush() -> None:
        if current is not None:
            value = "\n".join(parts).strip()
            if value:
                sections[current] = value

    for line in match.group(1).splitlines():
        numbered = _THOUGHT_NUMBERED_LINE_RE.match(line)
        if numbered is None:
            if current is not None:
                parts.append(line)
            continue
        _flush()
        num = int(numbered.group(1))
        current = fields[num - 1] if 1 <= num <= len(fields) else None
        same_line = numbered.group(2).strip()
        if same_line and 1 <= num <= len(_CAPSULE_QUESTIONS):
            same_line = _strip_question_echo(same_line, _CAPSULE_QUESTIONS[num - 1][1])
        parts = [same_line] if same_line else []
    _flush()
    return sections


def is_substantive_turn(
    thought_text: str,
    sections: dict[str, str],
    *,
    user_message: str = "",
    min_thought_chars: int = SESSION_BUFFER_MIN_THOUGHT_CHARS,
) -> bool:
    """判一轮是不是「实质轮」（纯函数，二条判据任一满足即 True，依据见上方判据块）。

    - ``thought_text``：该轮 ``<thought>`` 段正文。**不再看长度**（2026-10-28，
      issue #188），参数保留只为签名兼容；
    - ``sections``：:func:`parse_thought_sections` 的解析结果（判据 2：**任一
      非空格子**即算实质轮——四问已固定对应胶囊四节）；
    - ``user_message``：本轮用户消息（判据 3；空串不命中）；
    - ``min_thought_chars``：**已废弃**（判据 2 不再使用），保留签名以免破坏调用方，
      下个大版本连同 ``Config.SESSION_BUFFER_MIN_THOUGHT_CHARS`` 一起删除。

    ``tool_calls_count`` 判据已按 issue #194 删除：宿主从不填该字段（恒 0），且
    空 ``sections`` 在 ``decide_session_buffer`` 更上游就被 ``no_sections`` 挡掉，
    判据 1 在任何真实链路里都到不了。
    """
    # 判据 2：填了胶囊格子就算（「每轮顺手填一次」的设计意图，issue #188）。
    # 统一骨架后四问直接对应四节，任一非空格子都是有效填写。旧的「正文 ≥400 字
    # 且落在结论字段上」把模型顺手填的短轮全挡在缓冲外——实测全库缓冲 turns
    # 恒为 1、自动胶囊只产 3 条，正是这条门槛造成的。
    if any(isinstance(v, str) and v.strip() for v in (sections or {}).values()):
        return True
    # 判据 3：用户消息指派了任务（指令 ≠ 问句：疑问形态不影响判定）
    message = (user_message or "").strip()
    return bool(
        message and (any(verb in message for verb in _SUBSTANTIVE_ZH_VERBS) or _SUBSTANTIVE_EN_VERB_RE.search(message))
    )


def decide_session_buffer(
    text: str,
    *,
    skeleton_kind: str,
    session_id: str,
    turn_index: int,
    user_message: str = "",
    min_thought_chars: int = SESSION_BUFFER_MIN_THOUGHT_CHARS,
) -> dict[str, Any]:
    """判定本轮思考要不要进「会话缓冲」、进什么（post-turn 第三条路径，纯决策）。

    返回 ``{"buffer": bool, "reason": str, "skeleton_kind": str,
    "sections": dict, "raw": str}``：

    - session_id 为空 → ``buffer=False, reason="no_session_id"``（老宿主兼容，
      不落缓冲，行为与改动前一致）；
    - 解析不出任何字段 → ``buffer=False, reason="no_sections"``（保留原 thought
      落库路径兜底，本判定只管缓冲）；
    - 解析出了字段但不是实质轮 → ``buffer=False, reason="non_substantive"``
      （issue #170 实质轮筛选：短轮不进缓冲，但**不静默丢弃**——reason 留痕、
      sections 随诊断返回，与另两种 skip_reason 同一可观测约定）；
    - 否则 ``buffer=True``（reason 为空串）：``sections`` 为解析结果
      （:func:`parse_thought_sections`），``raw`` 为 ``<thought>`` 段原文截断到
      ``SESSION_BUFFER_RAW_MAX_CHARS`` 字；``skeleton_kind`` 为归一后的骨架类型
      （:func:`normalize_skeleton_kind`）。

    「实质轮」判定委托 :func:`is_substantive_turn`（三条判据：工具调用 / 长思考
    含结论字段 / 用户消息指令动词，依据见其上方判据块）。老宿主不传
    ``tool_calls_count`` / 不传 ``user_message`` → 缺省 0 / 空串，判据 1/3 永不
    命中，行为退化到只用判据 2——缺字段绝不当成实质轮。

    ``turn_index`` 不参与判定（要不要进缓冲只看 session_id、解析结果与实质
    性）——它随调用方语义留给执行层做缓冲内幂等覆盖的键。
    ``min_thought_chars`` 由执行层从 ``Config.SESSION_BUFFER_MIN_THOUGHT_CHARS``
    注入（环境变量可调），本函数不接配置——纯决策不碰环境。
    """
    kind = normalize_skeleton_kind(skeleton_kind)
    if not (session_id or "").strip():
        return {"buffer": False, "reason": "no_session_id", "skeleton_kind": kind, "sections": {}, "raw": ""}
    sections = parse_thought_sections(text, kind)
    if not sections:
        return {"buffer": False, "reason": "no_sections", "skeleton_kind": kind, "sections": {}, "raw": ""}
    parsed = extract_thought(text or "")
    raw = parsed["text"][:SESSION_BUFFER_RAW_MAX_CHARS] if parsed["found"] else ""
    if not is_substantive_turn(
        parsed["text"] if parsed["found"] else "",
        sections,
        user_message=user_message,
        min_thought_chars=min_thought_chars,
    ):
        # 带上 sections 便于排查「解析出了什么却被判非实质」（可观测，不静默）。
        return {"buffer": False, "reason": "non_substantive", "skeleton_kind": kind, "sections": sections, "raw": ""}
    return {"buffer": True, "reason": "", "skeleton_kind": kind, "sections": sections, "raw": raw}


# ---------------------------------------------------------------------------
# 可核验句柄抽取（M2）：自动胶囊的 outcome 节此前没有任何句柄（handles 参数
# 早就支持但调用方没传），对比手写接续点（有 PR 号 / commit / 路径）少了对账
# 依据。抽取是纯函数（不 import agent.*、不碰网络、不读环境），按固定正则的
# 模板匹配——与 _strip_question_echo 同一取向：命中才算数，绝不猜。
# ---------------------------------------------------------------------------

CAPSULE_HANDLE_LIMIT = 12  # 胶囊 outcome 里的句柄条数上限（超出按首次出现顺序截断）

# 形态表：**优先级序**（先出现的形态先认领文本区间）。表序兼作重叠消解依据——
# 同一段文本被多个形态命中时留高优先级形态，如 ``D:/a/b`` 整体按 Windows 路径
# 收，不再让 unix_path 把里面的 ``/a/b`` 碎片重复产出；``http://127.0.0.1:8090``
# 留 ``127.0.0.1:8090`` 而不是 ``//127.0.0.1`` 碎片。所以 unix_path 这种最宽泛
# 的形态排最后，向其余所有形态让路。
_HANDLE_EXTRACTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Windows 绝对路径：盘符 + 斜杠起头，吃到空白 / 引号 / 括号为止。lookbehind
    # 挡掉「前面紧跟字母的盘符」——``http://`` 不得从 ``p:/`` 起头被咬成路径。
    # 在任务书正则基础上额外排除**全角标点**（，。、；：！？等）：中文正文里路径
    # 后面紧跟全角标点是常态，不排除会把后半句整句吞进句柄；CJK 汉字本身保留
    # （路径里有中文目录名是真实形态，如 Novel/乾元风云录）。
    ("win_path", re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s`\"'）)，。、；：！？【】《》「」『』]+")),
    ("sha_full", re.compile(r"\b[0-9a-f]{40}\b")),  # 完整 commit sha
    ("sha_short", re.compile(r"(?<=`)[0-9a-f]{7,12}(?=`)")),  # 短 sha 必须被反引号包住（裸短串不认）
    ("addr", re.compile(r"\b(?:127\.0\.0\.1|localhost):\d{2,5}\b")),  # 服务地址
    ("pid", re.compile(r"\bPID[= ]\d+\b", re.IGNORECASE)),  # 进程号（PID 123 / pid=123）
    ("node", re.compile(r"(?:节点|node)\s?\d+", re.IGNORECASE)),  # 节点号（节点 3564 / node 42）
    ("pr_issue", re.compile(r"#\d+")),  # PR / issue 号
    # Unix 绝对路径：最宽泛（\w 含 CJK），排最后给上面所有形态让路。lookbehind
    # 挡掉「前面紧跟 ASCII 词字符的斜杠」——``core/strategy.py`` 里的相对路径
    # 不得咬出 ``/strategy.py`` 这种不存在的碎片句柄；中文紧贴路径（「检查/var/log」）
    # 不受影响（CJK 不在挡除表里）。
    ("unix_path", re.compile(r"(?<![a-zA-Z0-9_.-])/[\w./-]{6,}")),
)

# 路径类句柄尾部的句读标点是行文带的、不是路径的一部分（「见 /a/b.py。」），
# 剥掉；路径合法尾字符（字母数字 / - _）不在表里，不受影响。
_HANDLE_TRAILING_PUNCT = ".,;:!?，。、；：！？…）】》"


def extract_handles(text_blobs: list[str], *, limit: int = CAPSULE_HANDLE_LIMIT) -> list[str]:
    """从若干段文本里抽取可核验句柄（纯函数，见上方形态表；M2）。

    - 输出**按首次出现位置**排序、跨段去重，超过 ``limit`` 截断；
    - 无命中返回 ``[]``（绝不返回 ``["无"]`` 之类的噪音占位）；
    - 非字符串元素按字符串处理（调用方传 raw / sections 值的混拼，容忍脏值），
      空白输入同样安全。
    """
    out: list[str] = []
    seen: set[str] = set()
    for blob in text_blobs:
        text = blob if isinstance(blob, str) else str(blob or "")
        if not text:
            continue
        # 先按优先级认领区间（同段内被高优先级形态占住的区间，低优先级不再产
        # 碎片），再把幸存者按出现位置排序——输出序 = 首次出现序。
        candidates: list[tuple[int, int, int, str]] = []  # (优先级, start, end, 句柄)
        for priority, (_name, pattern) in enumerate(_HANDLE_EXTRACTORS):
            for match in pattern.finditer(text):
                handle = _trim_trailing_punct(match.group(0))
                if handle:
                    candidates.append((priority, match.start(), match.end(), handle))
        kept: list[tuple[int, str]] = []
        claimed: list[tuple[int, int]] = []
        for _priority, start, end, handle in sorted(candidates, key=lambda c: (c[0], c[1], -c[2])):
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue
            claimed.append((start, end))
            kept.append((start, handle))
        for _start, handle in sorted(kept, key=lambda k: k[0]):
            if handle in seen:
                continue
            seen.add(handle)
            out.append(handle)
            if len(out) >= limit:
                return out
    return out


def _trim_trailing_punct(handle: str) -> str:
    """剥掉句柄尾部行文带的句读标点（路径后紧跟句号 / 逗号是正文常态）。"""
    return handle.rstrip(_HANDLE_TRAILING_PUNCT)


def format_capsule_content(session_id: str, sections: dict[str, str]) -> str:
    """按固定四节格式拼胶囊正文（首行「接续点 / 会话接续 <session>」与
    ``core.task_state._is_handoff`` 的首行判据对齐）。

    抽成函数是因为正文有两个产出点：纯合成（``compose_session_capsule``）与
    「已有胶囊 + 新轮次」的合并（``merge_capsule_with_existing``）——两处各拼一遍
    格式必然漂移，四节标题又是逐字判据。
    """
    return (
        f"接续点 / 会话接续 {session_id}\n"
        f"【思路】{sections['thought']}\n"
        f"【做了什么】{sections['outcome']}\n"
        f"【下一步】{sections['next']}\n"
        f"【踩的坑】{sections['pitfalls']}"
    )


def compose_session_capsule(turns: list[dict], *, session_id: str, handles: list[str] | None = None) -> dict:
    """把会话缓冲的 turns 合并成胶囊内容（纯合成，不写库）。

    返回 ``{"content": str, "sections": dict, "pending": list[str], "turns": int}``。
    sections 四节（字段名见 ``_SKELETON_FIELD_MAP``，逐轮按缓冲顺序合并）：

    - thought（【思路】）：从各轮 judgement / conclusion / goal 里按顺序取
      （每轮取第一个非空字段），总长 ≤ ``CAPSULE_THOUGHT_MAX_CHARS``（纯截断）；
    - outcome（【做了什么】）：合并各轮 change / deliverable，去重，每项一行；
      ``handles``（PR / 文件路径 / 节点号等交付物句柄）按序拼在末尾（并入统一去重）；
    - next（【下一步】）：合并各轮 verify / output 里带「下一步 / 待 / 需 / 再」
      的行，去重；
    - pitfalls（【踩的坑】）：合并各轮 failure_path，去重。

    任一节为空 → 该节写「（待补）」（``CAPSULE_SECTION_TBD``）并把它记进
    ``pending``（按 thought / outcome / next / pitfalls 顺序）。

    content 固定四节格式，首行「接续点 / 会话接续 <session_id>」与
    ``core.task_state._is_handoff`` 的首行判据对齐（显式 ``payload.kind="handoff"``
    之外的形态层识别路径）；四节标题【思路】/【做了什么】/【下一步】/【踩的坑】
    逐字固定。
    """
    thought_parts: list[str] = []
    outcome_parts: list[str] = []
    next_parts: list[str] = []
    pitfall_parts: list[str] = []
    merged = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        s = turn.get("sections")
        if not isinstance(s, dict):
            continue
        merged += 1
        # 新字段（统一骨架，2026-10-28）优先；旧字段名兜住历史缓冲里的轮次
        # （老库的 turns 存的是 judgement/change/verify/failure_path 那批名字）。
        for key in ("thought", "judgement", "conclusion", "goal"):
            v = s.get(key)
            if isinstance(v, str) and v.strip():
                thought_parts.append(v.strip())
                break
        for key in ("outcome", "change", "deliverable"):
            v = s.get(key)
            if isinstance(v, str) and v.strip():
                outcome_parts.append(v.strip())
        # next 是「下一步 / 待办」专用格子，整段照取；旧字段 verify/output 是
        # 「怎么验」「产出」，还得靠信号词筛出真正交接要的事。
        nv = s.get("next")
        if isinstance(nv, str) and nv.strip():
            next_parts.append(nv.strip())
        for key in ("verify", "output"):
            v = s.get(key)
            if isinstance(v, str) and v.strip():
                for line in v.splitlines():
                    line = line.strip()
                    if line and any(mk in line for mk in _CAPSULE_NEXT_MARKERS):
                        next_parts.append(line)
        v = s.get("pitfalls")
        if isinstance(v, str) and v.strip():
            pitfall_parts.append(v.strip())
        else:
            v = s.get("failure_path")
            if isinstance(v, str) and v.strip():
                pitfall_parts.append(v.strip())

    def _dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    for handle in handles or []:
        text = str(handle).strip()
        if text:
            outcome_parts.append(text)  # handles 拼在 outcome 末尾（并入统一去重）

    thought_body = "\n".join(thought_parts)
    if len(thought_body) > CAPSULE_THOUGHT_MAX_CHARS:
        thought_body = thought_body[:CAPSULE_THOUGHT_MAX_CHARS]
    raw = {
        "thought": thought_body,
        "outcome": "\n".join(_dedupe(outcome_parts)),
        "next": "\n".join(_dedupe(next_parts)),
        "pitfalls": "\n".join(_dedupe(pitfall_parts)),
    }
    sections = {name: raw[name] or CAPSULE_SECTION_TBD for name in _CAPSULE_SECTION_ORDER}
    pending = [name for name in _CAPSULE_SECTION_ORDER if not raw[name]]
    content = format_capsule_content(session_id, sections)
    return {"content": content, "sections": sections, "pending": pending, "turns": merged}


def _merge_capsule_section_lines(old: str, new: str) -> str:
    """两段节正文按**行**合并：追加去重，保序（旧在前）。

    返回空串表示「这一节最终什么都没有」，由调用方决定是否落
    ``CAPSULE_SECTION_TBD``。为什么按行而不是按字符 / 整段替换：节的正文本来就是
    「每项一行」或「每轮一段」的形态（outcome / next 逐项一行，thought 逐轮一段），
    行级去重既能挡住重复项，又不会把两轮的内容粘连成一句。
    """
    if not old or old == CAPSULE_SECTION_TBD:
        return new if new and new != CAPSULE_SECTION_TBD else ""
    if not new or new == CAPSULE_SECTION_TBD:
        return old
    seen: set[str] = set()
    out: list[str] = []
    for line in f"{old}\n{new}".splitlines():
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(line)
    return "\n".join(out)


def merge_capsule_with_existing(composed: dict, existing_sections: dict | None, *, session_id: str) -> dict:
    """把新合成的胶囊与**已有**胶囊的四节按行合并，返回新的 composed。

    为什么必须有这一步（2026-10-28 实测事故）：``_finalize_session_capsule`` 的更新
    路径原先无条件用新合成覆盖正文。而胶囊正文里可能含**人补写的内容**——收口时按
    接续点口径补的 outcome / next / pitfalls 并不在缓冲 turns 里，机器合成不出来。
    一次正常的 session-end、或断线自愈的补产，都会把那些手写内容静默盖掉（实测：
    节点 3665 的 1761 字手写接续点，会在一次 1 轮缓冲的补产下被覆盖）。

    规则（取向 = 什么都不丢，宁可节变长）：
    - 新节为空（``（待补）``）→ 保留旧节；
    - 旧节为空 / 缺失 → 用新节；
    - 两者都有 → 按行追加去重（旧在前，保序）；
    - 合并结果为空 → 该节落 ``（待补）`` 并进 ``pending``。

    ``turns`` 沿用新合成的值（它是「缓冲里的轮数」这一事实，不是内容并集）。
    """
    incoming = composed.get("sections") if isinstance(composed, dict) else None
    if not isinstance(incoming, dict):
        return composed
    previous = existing_sections if isinstance(existing_sections, dict) else {}
    merged: dict[str, str] = {}
    for name in _CAPSULE_SECTION_ORDER:
        body = _merge_capsule_section_lines(str(previous.get(name) or ""), str(incoming.get(name) or ""))
        merged[name] = body or CAPSULE_SECTION_TBD
    return {
        "content": format_capsule_content(session_id, merged),
        "sections": merged,
        "pending": [name for name in _CAPSULE_SECTION_ORDER if merged[name] == CAPSULE_SECTION_TBD],
        "turns": composed.get("turns"),
    }
