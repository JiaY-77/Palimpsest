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
from typing import Any

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
