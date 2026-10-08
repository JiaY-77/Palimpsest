"""任务节点状态机：状态解析、统一写路径、活跃任务查询、旧节点回填。

背景（为什么要这层）
====================
Palimpsest 里的任务节点（``type=task``）长期把「任务当前处于哪个阶段」写在
``content`` 首行的方括号标记里（如 ``[doing] T-123 写注册表``）。这个约定的问题：

  - 读的人（人 / agent）得逐条读正文才知道状态，无法按状态过滤或排序；
  - 写的人各写各的，没有单一入口保证「状态变了要留痕」。

本模块把状态提成**结构化字段** ``payload.task_state``，并规定：

  1. ``parse_task_state`` —— 从 content 首行解析状态（唯一解析规则，不猜）；
  2. ``apply_task_patch`` —— ``type=task`` 节点的**唯一写入口**：状态变化时同步
     写 ``last_touched_at``、生成 ``type=record`` 日志节点并建 ``LOGS`` 边；
  3. ``list_active_tasks`` —— 按状态/项目筛选 + 优先级排序的活跃任务查询；
  4. ``backfill_task_state`` —— 存量老节点一次性回填（默认 dry-run）。

状态取值与 ``content`` 首行标记的映射见 ``_STATE_KEYWORDS``。解析不出时返回 ``None``
（``unknown`` 只在回填兜底时使用），绝不猜测——猜错的状态比没有状态更危险。

解析是「关键词包含式」（不是精确相等）：存量语料的真实标记五花八门——
``[待办，2026-09-10 主人指令「计入小帕」]``（含空格、超长）、``[完成 2026-09-05]``
（带后缀）、``【任务·…（进行中）…】``（全角括号、状态词嵌在中间）。
因此规则是：把 content **首行**里所有括号内容块拆出来，逐块做关键词包含匹配
（**块内以最先出现的状态词为准**，见 ``_match_keyword``），命中即返回；
匹配范围到首行末尾（真实语料里状态词常常在正文中间，如 ``T073：… → [待办，…]``）。
"""

import logging
import re
import time
from datetime import datetime, timezone

from core.utils import _to_float

logger = logging.getLogger(__name__)

# 任务的全部合法状态（白名单）。显式传入 payload.task_state 时必须命中其一。
TASK_STATES = frozenset({"todo", "doing", "blocked", "done", "canceled", "unknown"})

# 回填时解析不出状态 → unknown（并打 needs_review 标记，交给人工复核）。
UNKNOWN_STATE = "unknown"

# 状态关键词表：**(关键词, 规范化状态)**，元组顺序仅作**同位置并列时的兜底**
# （见 ``_match_keyword``：块内实际以「最先出现的状态词」胜出，顺序只在两个
# 关键词起点相同时用来打破平局）。
#
# 为什么用「包含」而不是「精确相等」：真实存量语料的状态标记带后缀、带日期、
# 带说明（``[完成 2026-09-05]`` / ``[挂账持续]`` / ``[已开工 2026-09-14 晚，主人拍板]``），
# 精确匹配只能解析 3/18。改为「块内含关键词即命中」。
#
# 为什么块内按「最先出现」而不是「全局优先级」取胜出（2026-10-08 复核修正）：
#   一个标记块里可能同时出现多个状态词，全局优先级会误判——
#     · ``[挂账，T055 换脑落地后实施]``：挂账在前（本任务已挂账），
#       「落地」是**别人**（T055 换脑）的完成条件 → 应判 blocked；
#       若「落地」(doing) 排在「挂账」(blocked) 前就误判成 doing。
#     · ``【技能库瘦身（进行中）…挂起待续】``：进行中在前（本任务在做），
#       全局 blocked-优先排序会把「挂起」抢前面 → 误判 blocked。
#   两类都指向同一条规则：**块内最先出现的状态词代表本块语义**，
#   与人类读标记的习惯（首现即主状态）一致。
#
# 关键词内含/嵌套仍需顺序兜底（起点相同）：
#   · ``未完成`` 由否定词单独判定（``_NEGATION_HINTS``），不靠顺序；
#   · ``部分完成`` 起点（0）早于 ``完成``（2），按位置自然胜出，无需再靠顺序。
_STATE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("部分完成", "doing"),
    ("已拍板·待执行", "todo"),
    ("进行中", "doing"),
    ("已开工", "doing"),
    ("开工", "doing"),
    ("挂起", "blocked"),
    ("挂账", "blocked"),
    ("阻塞", "blocked"),
    # 「落地」是弱语义词：真实语料里从不作独立状态标记（``[落地]`` 出现 0 次），
    # 只作描述词（``换脑落地后实施``=别人的条件 / ``第二步入落地``=本任务进展）。
    # 保留它给「第二步入落地」这类本任务进展句兜底，但排在所有强语义词之后，
    # 且位置判据已保证「挂账…落地」（挂账在前）判 blocked。
    ("落地", "doing"),
    ("待办", "todo"),
    ("待批准", "todo"),
    ("待执行", "todo"),
    ("待启动", "todo"),
    ("计划中", "todo"),
    ("待决策", "todo"),
    ("已完成", "done"),
    ("完成", "done"),
    ("取消", "canceled"),
    ("todo", "todo"),
    ("doing", "doing"),
    ("blocked", "blocked"),
    ("done", "done"),
    ("canceled", "canceled"),
)

# content 首行所有 ``[...]`` 半角块。``[^\[\]\n]*`` 允许含空格 / 任意长度 / 中文，
# 因此 ``[待办，2026-09-10 主人指令「计入小帕」]`` 这类超长带空格标记也能整块取出。
_BRACKET_RE = re.compile(r"\[([^\[\]\n]*)\]")

# 首行所有 ``【...】`` 全角内容块（真实语料 ``【任务·…（进行中）…】``）。
# ``[^【】\n]*`` 允许内部嵌套全角圆括号 ``（...）``——「跨界嵌套」在整块文本上
# 做关键词匹配，所以 ``（进行中）`` 落在块内即可命中，无需拆嵌套。
_FULL_BRACKET_RE = re.compile(r"【([^【】\n]*)】")

# 旧接口兼容：``PARSE_MAP`` 由 ``_STATE_KEYWORDS`` 派生（其余模块可能仍 import 它）。
PARSE_MAP = dict(_STATE_KEYWORDS)

# 否定/未完成提示：命中即不解析（如「还没完成」「未完成」——
# 不能因为句子里出现「完成」就判成 done）。
_NEGATION_HINTS = ("未完成", "还没", "尚未", "未开始", "未能", "暂未", "没有完成")
DEFAULT_ACTIVE_STATES = ("todo", "doing", "blocked")

# 排序优先级：doing 最紧（正在做）→ blocked（卡住要救）→ todo（排队）。
_STATE_PRIORITY = {"doing": 0, "blocked": 1, "todo": 2}


def _normalize(value: str) -> str:
    """标记词归一：去首尾空白 + 折叠内部空白 + 小写。

    大小写/空格容错：``[ Doing ]`` / ``[DOING]`` / ``[doing]`` 都得到 ``doing``。
    """
    return re.sub(r"\s+", "", value or "").strip().lower()


def _match_keyword(text: str) -> str | None:
    """在文本里找**最先出现**的状态关键词，返回规范化状态或 ``None``。

    规则（2026-10-08 复核修正）：块内**起点最靠前**的状态词胜出——
    一个标记块常同时含多个状态词，最先出现的那个代表本块主状态：

      - ``挂账，T055 换脑落地后实施`` → 挂账(pos 0) 早于 落地(pos 9) → blocked
        （「落地」是别人 T055 的完成条件，非本任务状态）；
      - ``技能库瘦身（进行中）…挂起待续`` → 进行中 早于 挂起 → doing
        （本任务在做，「挂起待续」指别的事项）。

    ``_STATE_KEYWORDS`` 的顺序只在**两个关键词起点相同**时用作兜底
    （``部分完成`` 起点天然早于 ``完成``，故实际很少触发）。

    与旧实现（全局优先级先命中者胜）的差别：旧版把 blocked 词一律排在 doing
    词前，导致 ``[挂账，…落地…]`` 被「落地」误判、或 ``（进行中）…挂起待续``
    被「挂起」误判；按位置判定对两类同时正确。
    """
    if not text:
        return None
    best_pos = -1
    best_state: str | None = None
    for keyword, state in _STATE_KEYWORDS:
        pos = text.find(keyword)
        if pos < 0:
            continue
        # 严格更靠前才替换 → 起点相同时保留元组中靠前的关键词（兜底优先级）
        if best_state is None or pos < best_pos:
            best_pos = pos
            best_state = state
    return best_state


def parse_task_state(content: str) -> str | None:
    """从 ``content`` **首行**解析任务状态（关键词包含式，不猜）。

    规则：
      - 只看**首行**（``\\n`` 前）；第二行及以后不参与；
      - 取出首行里**所有**括号内容块：半角 ``[...]``（可含空格、任意长度）、
        全角 ``【...】``（可嵌套全角 ``（...）``），按出现顺序依次尝试；
      - 对每个块做**关键词包含匹配**（见 ``_STATE_KEYWORDS`` / ``_match_keyword``）：
        块内**最先出现的状态词**决定该块状态，命中即返回；
      - 否定提示（``未完成``/``还没`` 等）命中的块直接跳过——防止
        「未完成」被 ``完成`` 命中成 done；
      - 全部块都不命中 / 首行没有括号 → 返回 ``None``（不猜）。

    例：``[部分完成]`` → doing、``[完成 2026-09-05]`` → done、
    ``【任务·…（进行中）…】`` → doing、``T073：… → [待办，…]`` → todo。

    返回规范化状态字符串或 ``None``。
    """
    text = content or ""
    first_line = text.split("\n", 1)[0]

    # 依次尝试首行里的每个括号块；第一个匹配到状态的块决定结果。
    # 注意：**不做跨块合并**——真实语料里「部分完成」与「完成」可能分别出现在
    # 不同块，逐块匹配 + 每块内部按优先级取词，能正确处理这种组合。
    for match in _iter_mark_blocks(first_line):
        block = match.group(1)
        # 否定提示：块本身或**块之前的文本**命中即跳过该块
        # （「还没完成 [done]」的否定词在括号外，必须看前缀才拦得住）
        prefix = first_line[: match.start()]
        if any(hint in block or hint in prefix for hint in _NEGATION_HINTS):
            continue
        state = _match_keyword(_normalize(block))
        if state:
            return state
    return None


def _iter_mark_blocks(first_line: str):
    """按出现顺序产出首行里的括号内容块（半角 ``[...]`` + 全角 ``【...】``）。

    半角块 ``[待办，2026-09-10 …]`` 与全角块 ``【…（进行中）…】`` 统一处理：
    统一用 ``re.Match``（``group(1)`` = 块内容），调用方无需区分括号形态。
    """
    spans: list[tuple[int, re.Match]] = []
    for pattern in (_BRACKET_RE, _FULL_BRACKET_RE):
        for match in pattern.finditer(first_line):
            spans.append((match.start(), match))
    spans.sort(key=lambda item: item[0])
    seen: set[tuple[int, int]] = set()
    for _start, match in spans:
        key = match.span()
        if key in seen:
            continue
        seen.add(key)
        yield match


def _now_iso() -> str:
    """当前时间（本地时区的 ISO8601 字符串，秒精度，带偏移）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _date_str() -> str:
    """当前日期 ``YYYY-MM-DD``（日志节点 content 用）。"""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _resolve_effective_state(payload: dict) -> tuple[str | None, bool]:
    """解析节点当前生效状态，返回 ``(state, needs_write)``。

    - ``payload.task_state`` 是合法白名单值 → 直接采用，无需重算；
    - 缺失 / 为空 / 非法 → 从 ``content`` 首行现算（存量老节点还没回填）；
    - 仍解析不出 → ``(None, False)``，保持原值不动。

    ``needs_write`` 表示「该状态是现算出来的、尚未落库」，调用方据此决定是否回填。
    """
    explicit = payload.get("task_state")
    if isinstance(explicit, str) and explicit in TASK_STATES:
        return explicit, False
    parsed = parse_task_state(payload.get("content") or "")
    if parsed is None:
        return None, False
    return parsed, True


def apply_task_patch(store, node_id: int, payload_patch: dict | None = None) -> dict:
    """``type=task`` 节点的统一写入入口。

    流程：
      1. 合并 ``payload_patch`` 进现有 payload（浅合并，只改传入键）；
      2. 定状态，按优先级：
         a. 显式 ``payload.task_state`` → 白名单校验（非法直接 ``ValueError``、不落库）；
         b. 本次改动了 ``content`` → 从**新** content 首行重新解析（content 是状态的
            来源，改了正文标记却不跟状态，就会留下「正文说 doing、字段说 todo」的
            静默漂移）；解析不出则保持原值；
         c. 否则 → 从现有 content 首行解析，解析不出保持原值；
      3. 状态发生变化 → 更新 ``task_state`` + ``last_touched_at``，写一条
         ``type=record`` 日志节点并建 ``record -[LOGS]-> task`` 边；
         状态没变（含无状态可解析）→ 只更新 ``last_touched_at``。

    **原子性**：状态更新与日志写入不是一个事务（triviumdb 的连接模型如此，
    见 core/trivium_store.py 的单进程写约束）。因此日志节点**先写**——日志写入
    失败时状态保持原值不变，不会出现「状态已变、没有留痕」的半状态；
    最坏情况是「日志已写、状态未变」（一次多余日志，可人工清理，无损）。

    返回 ``{node_id, state, previous_state, changed, last_touched_at}``。
    """
    node = store.get_node(node_id)
    if not node:
        raise ValueError(f"节点 ID={node_id} 不存在，无法更新任务状态")
    payload = dict(node.get("payload") or {})

    patch = payload_patch or {}
    if "task_state" in patch:
        explicit = patch["task_state"]
        if explicit not in TASK_STATES:
            raise ValueError(f"非法 task_state: {explicit!r}；合法取值 {sorted(TASK_STATES)}")
        new_state = explicit
    elif "content" in patch:
        # content 是状态的来源：改了正文标记就按新正文重解析，避免字段与正文漂移
        new_state = parse_task_state(patch.get("content") or "")
    else:
        new_state, _ = _resolve_effective_state(payload)

    current = payload.get("task_state")
    previous_state = current if isinstance(current, str) and current in TASK_STATES else None
    merged = {**payload, **patch}
    if new_state is not None:
        merged["task_state"] = new_state

    # 状态变化先留痕：日志成功后才落状态，避免「状态已变、无留痕」。
    changed = new_state is not None and new_state != previous_state
    now_iso = _now_iso()
    if changed:
        _write_state_log(store, node_id, merged, previous_state, new_state, time.time())

    merged["last_touched_at"] = now_iso
    store.update_payload(node_id, merged)

    logger.info(
        "任务状态更新 node=%s %s -> %s（changed=%s）",
        node_id,
        previous_state,
        new_state,
        changed,
    )
    return {
        "node_id": node_id,
        "state": new_state,
        "previous_state": previous_state,
        "changed": changed,
        "last_touched_at": now_iso,
    }


def _task_label(payload: dict) -> str:
    """任务节点的展示名：优先 ``task_key``，其次 ``title``，最后取 content 首行。"""
    label = (payload.get("task_key") or payload.get("title") or "").strip()
    if label:
        return label
    first_line = ((payload.get("content") or "").split("\n", 1)[0]).strip()
    # 去掉首行里的状态标记（半角/全角都去），只留正文，便于日志可读
    cleaned = _BRACKET_RE.sub("", first_line)
    cleaned = _FULL_BRACKET_RE.sub("", cleaned)
    return cleaned.strip()


def _write_state_log(store, node_id: int, payload: dict, previous_state, new_state: str, now_ts: float) -> None:
    """写一条状态变更日志节点并建 ``record -[LOGS]-> task`` 边。

    content 格式：``[{new_state}] {task_key} {任务名} · {YYYY-MM-DD}``
    （previous_state 一并写进 payload，便于回溯「从哪来」）。

    ``created_at`` 走**插入后补写**：``insert_node`` 的基础 payload 里已含
    ``created_at``（值 None），其 ``extra_fields`` 过滤会把传入的同名键剔掉
    （``k not in payload``），直接传会被静默吞掉 → 日志节点时间戳恒为 None。
    因此插入后按 ``mem_ingest`` 的同款做法（``mcp_tools/memory.py``）读回补写：
    ``created_at`` 为空才写，避免覆盖既有值。

    时间戳**必须是数值**（``time.time()``），与库内既有节点一致——写 ISO 字符串
    会让 ``/mem/recent`` 的 ``created_at`` 排序在 float/str 之间抛 TypeError。
    """
    label = _task_label(payload)
    content = f"[{new_state}] {label} · {_date_str()}".strip()
    log_payload = {
        "type": "record",
        "content": content,
        "importance": 0.4,
        "status": "active",
        "domain": payload.get("domain") or payload.get("character_name") or "task",
        "log_kind": "task_state",
        "task_id": node_id,
        "task_state": new_state,
        "previous_state": previous_state,
    }
    log_id = store.insert_node(log_payload, store.embed_text(content))
    # insert_node 吞掉 created_at（基础 payload 占位 None）→ 读回补写真实时间戳
    node = store.get_node(log_id) or {}
    log_stored = dict(node.get("payload") or {})
    if log_stored.get("created_at") is None:
        log_stored["created_at"] = now_ts
        store.update_payload(log_id, log_stored)
    store.create_edge(log_id, node_id, "LOGS")
    logger.info("任务状态日志已写入 log_node=%s -> task=%s（%s）", log_id, node_id, new_state)


def _matches_filters(payload: dict, project: str, states: set, include_legacy: bool) -> bool:
    """活跃任务过滤：type / status / legacy + 状态集 + 项目。"""
    if payload.get("type") != "task":
        return False
    if payload.get("status") != "active":
        return False
    if not include_legacy and payload.get("legacy") is True:
        return False
    state, _ = _resolve_effective_state(payload)
    if state not in states:
        return False
    return not (project and str(payload.get("project") or "") != project)


def _sort_key(item: dict) -> tuple:
    """排序键：``doing > blocked > todo`` → ``last_touched_at`` 倒序 → ``importance`` 倒序 → id 倒序。

    ``last_touched_at`` 缺失时按空串处理（ISO8601 字符串可直接比较，
    无时间戳的排在有时间戳之后）；``importance`` 缺失按 0.0。
    """
    state = item.get("state") or ""
    priority = _STATE_PRIORITY.get(state, len(_STATE_PRIORITY))
    touched = item.get("last_touched_at") or ""
    importance = _to_float(item.get("importance"), 0.0) or 0.0
    # 前两个键升序，后两个键降序 → 统一取负号，整体升序排一遍
    return (priority, _neg_str(touched), -importance, -(item.get("id") or 0))


def _neg_str(value: str):
    """字符串降序的排序辅助：用一个可比较的包装类（取反不适用于字符串）。

    实现方式是「按位取反」的整型编码——把字符串编码成字节能唯一比较的
    元组，再用其元素取负。简单起见直接返回 ``_DescStr`` 包装，见下。
    """
    return _DescStr(value)


class _DescStr:
    """字符串倒序包装：比较时反转结果（``sorted`` 全升序即可实现降序）。"""

    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = value

    def __eq__(self, other):
        return self.value == other.value

    def __lt__(self, other):
        return self.value > other.value


def list_active_tasks(
    store,
    project: str = "",
    states=(),
    limit: int = 15,
    include_legacy: bool = False,
) -> dict:
    """列出活跃任务（``type=task`` & ``status=active`` & 状态命中）。

    - ``states``：可传字符串（逗号分隔，如 ``"todo,doing,blocked"``）或序列；
      空 / 全为非法值 → 回退默认 ``DEFAULT_ACTIVE_STATES``。
    - ``project``：非空时只保留 ``payload.project`` 相等的节点。
    - ``include_legacy=False``（默认）排除 ``payload.legacy is True`` 的老节点。
    - 排序：状态优先级 → ``last_touched_at`` 倒序 → ``importance`` 倒序。

    返回 ``{"results": [...], "total": int, "states": [...]}``；
    ``total`` 是过滤后的总数（不受 ``limit`` 影响）。
    """
    state_set = _parse_states(states)
    items: list[dict] = []
    for nid, node in store.iter_nodes():
        payload = node.get("payload") or {}
        if not _matches_filters(payload, project, state_set, include_legacy):
            continue
        state, _ = _resolve_effective_state(payload)
        items.append(_brief(nid, payload, state))
    items.sort(key=_sort_key)
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 15
    return {"results": items[:cap], "total": len(items), "states": sorted(state_set)}


def _parse_states(states) -> set:
    """把 ``states`` 参数归一成合法状态集合；非法值过滤，空则回退默认集。"""
    raw = [s.strip() for s in states.split(",")] if isinstance(states, str) else [str(s).strip() for s in states or []]
    picked = {s for s in raw if s in TASK_STATES}
    return picked or set(DEFAULT_ACTIVE_STATES)


def _brief(nid: int, payload: dict, state: str | None) -> dict:
    """任务节点 → 精简条目（只给跟进要用的字段，不复制整包 payload）。"""
    return {
        "id": nid,
        "task_key": payload.get("task_key") or "",
        "title": payload.get("title") or _task_label(payload),
        "state": state,
        "project": payload.get("project") or "",
        "next_action": payload.get("next_action") or "",
        "aliases": payload.get("aliases") or [],
        "last_touched_at": payload.get("last_touched_at"),
        "importance": payload.get("importance", 0.5),
    }


def backfill_task_state(store, dry_run: bool = True) -> dict:
    """存量 ``type=task`` 节点回填 ``task_state``。

    对每个 ``type=task``（且未回填过）的节点：
      - 从 content 首行解析状态写入 ``payload.task_state``；
      - 解析失败 → ``task_state="unknown"`` + ``needs_review=True``；
      - **按状态决定是否打 ``legacy``**：只有不再需要跟进的状态（``done`` /
        ``canceled`` / ``unknown``）才标 ``payload.legacy = True``（历史留痕，
        ``/tasks/active`` 默认不列出）；仍在跟进的状态（``todo`` / ``doing`` /
        ``blocked``）保持 legacy 缺失，**回填后即可出现在活跃任务视图里**。

    为什么这样分：``legacy`` 的语义是「历史节点，不必再看」，不是「回填过的节点」。
    把仍在做的任务一并标成 legacy，会让回填后的工作集立刻变空——恰好废掉这套
    改造的目的。

    ``dry_run=True``（默认）只统计不写库；``--apply`` 才落盘。

    **两段式（先快照后写）**：``iter_nodes`` 在整个迭代期间持有库连接
    （进程级 DB 访问锁），迭代中调 ``update_payload`` 会在同进程内重入取锁而自锁
    ——因此先把候选节点整体快照出来、迭代结束（连接释放）后再逐个写回。

    返回 ``{dry_run, scanned, already, parsed, unknown, needs_review, legacy, changes}``；
    ``changes`` 为每个将变更 / 已变更节点的摘要（dry-run 预览 = 执行决策）。
    """
    scanned = 0
    already = 0
    parsed = 0
    unknown = 0
    legacy_count = 0
    changes: list[dict] = []
    pending: list[tuple[int, dict]] = []  # (node_id, 待写 payload)，迭代结束再写

    for nid, node in store.iter_nodes():
        payload = node.get("payload") or {}
        if payload.get("type") != "task":
            continue
        scanned += 1
        state = parse_task_state(payload.get("content") or "")
        if state is None:
            state = UNKNOWN_STATE
            unknown += 1
        else:
            parsed += 1
        # 该节点**应该**是什么 legacy 值：不活跃状态才标 legacy
        want_legacy = state not in DEFAULT_ACTIVE_STATES
        # 幂等：已是目标形态（task_state 相符且 legacy 相符）→ 跳过，重跑输出 0 变更
        if payload.get("task_state") == state and payload.get("legacy") is want_legacy:
            already += 1
            continue
        changes.append(
            {
                "id": nid,
                "task_state": state,
                "needs_review": state == UNKNOWN_STATE,
                "legacy": want_legacy,
                "title": _task_label(payload),
            }
        )
        new_payload = {**payload, "task_state": state}
        if want_legacy:
            new_payload["legacy"] = True
            legacy_count += 1
        else:
            new_payload["legacy"] = False
        if state == UNKNOWN_STATE:
            new_payload["needs_review"] = True
        pending.append((nid, new_payload))

    if not dry_run:
        for nid, new_payload in pending:
            store.update_payload(nid, new_payload)

    return {
        "dry_run": dry_run,
        "scanned": scanned,
        "already": already,
        "parsed": parsed,
        "unknown": unknown,
        "needs_review": unknown,
        "legacy": legacy_count,
        "changes": changes,
    }
