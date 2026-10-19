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
``[待办，2026-09-10 用户指令「计入 Palimpsest」]``（含空格、超长）、``[完成 2026-09-05]``
（带后缀）、``【任务·…（进行中）…】``（全角括号、状态词嵌在中间）。
因此规则是：把 content **首行**里所有括号内容块拆出来，逐块做关键词包含匹配
（**块内以最先出现的状态词为准**，见 ``_match_keyword``），命中即返回；
匹配范围到首行末尾（真实语料里状态词常常在正文中间，如 ``T-73：… → [待办，…]``）。
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

# 会话接续点的身份标记：``payload.kind == "handoff"`` 的 task 节点是「跨会话交接
# 记录」而非待办事项。接续点必须写成 ``type=task``（写成 ``type=record`` 会落
# logs 层，``tier=facts`` 的默认检索搜不到），但它们的状态标记常命中 doing、
# 又多数没有 title——混进 ``list_active_tasks`` 的工作集会长期占着前排。因此
# 查询层默认排除它们（``include_handoff=False``），显式开关可取回（显式取回
# 不受状态过滤限制——接续点没有「待办状态」语义）。``kind`` 由**写入层**自动打：
# ``mem_ingest`` / ``apply_task_patch`` 在写入/更新时凡 content 首行符合
# ``_is_handoff`` 判据即打标，存量缺标由回填补——不依赖任何一方单独补齐。
# 只支持这一个 kind 取值，不做别的分类。
HANDOFF_KIND = "handoff"

# 会话接续点识别：``type=task`` 且 content **首行以「接续点」作为标题词开头**
# （跳过前导空白与若干 ``[...]`` 状态块后必须紧跟「接续点」，允许 ``【`` / ``★``
# 装饰，见 ``_is_handoff``）。真实存量语料两种形态都命中：
#   · ``【★接续点·2026-10-16 任务治理整理 + 下一步外包 · 新会话先读这条】``
#   · ``接续点 / 会话接续 / 新聊天先读 —— 2026-10-13~14 大修（……）``
# 不能只做「首行包含『接续点』」——句中提到「接续点」的真待办会被误吃
# （生产实测两条反例，见 ``_is_handoff`` 的 docstring）。
_HANDOFF_HINT = "接续点"

# 状态关键词表：**(关键词, 规范化状态)**，元组顺序仅作**同位置并列时的兜底**
# （见 ``_match_keyword``：块内实际以「最先出现的状态词」胜出，顺序只在两个
# 关键词起点相同时用来打破平局）。
#
# 为什么用「包含」而不是「精确相等」：真实存量语料的状态标记带后缀、带日期、
# 带说明（``[完成 2026-09-05]`` / ``[挂账持续]`` / ``[已开工 2026-09-14 晚，用户拍板]``），
# 精确匹配只能解析 3/18。改为「块内含关键词即命中」。
#
# 为什么块内按「最先出现」而不是「全局优先级」取胜出（2026-10-08 复核修正）：
#   一个标记块里可能同时出现多个状态词，全局优先级会误判——
#     · ``[挂账，T-55 换脑落地后实施]``：挂账在前（本任务已挂账），
#       「落地」是**别人**（T-55 换脑）的完成条件 → 应判 blocked；
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
# 因此 ``[待办，2026-09-10 用户指令「计入 Palimpsest」]`` 这类超长带空格标记也能整块取出。
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

      - ``挂账，T-55 换脑落地后实施`` → 挂账(pos 0) 早于 落地(pos 9) → blocked
        （「落地」是别人 T-55 的完成条件，非本任务状态）；
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
    ``【任务·…（进行中）…】`` → doing、``T-73：… → [待办，…]`` → todo。

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


def _iso_from_epoch(ts: float) -> str:
    """数值时间戳 → ``_now_iso()`` 同格式（本地时区、秒精度、带偏移）的 ISO 字符串。

    issue #128 C1：``mem_ingest`` 写 ``type=task`` 时把 ``last_touched_at`` 初始化为
    ``created_at`` 同一刻。``created_at`` 是数值时间戳（``time.time()``），转换统一走
    这里，两个时间字段格式一致，列表新鲜度判据（``_freshness_epoch``）可直接比较。
    """
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat(timespec="seconds")


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
         状态没变（含无状态可解析）→ 只更新 ``last_touched_at``；

      4. **接续点写入层打标（幂等）**：合并后的 ``type=task`` content 若首行以
         「接续点」标题词开头（``_is_handoff``，与 ``mem_ingest`` / 回填同源的
         唯一识别规则）→ 自动补 ``kind=handoff``，并在 ``title`` 为空时用
         ``_handoff_title`` 补摘要（``title`` 已有非空值不覆盖，提取逻辑与回填
         同源）。content 改成接续点形态同样打标；已是接续点的不重复写、不报错。

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
    # 接续点写入层打标（幂等）：合并后的 content 首行以「接续点」标题词开头
    # （``_is_handoff``——与 ``mem_ingest`` / 回填同源的唯一识别规则，PR #112
    # 收紧过的首行判据不放宽）→ 自动补 ``kind=handoff``；已是接续点的不重复写。
    # title 同步补齐（幂等、不覆盖）：``title`` 此前只有回填会补，更新路径改出
    # 的接续点在交接列表里 title 为空、读不出是哪条。``title`` 已有非空值就保持
    # 原值（尊重人工与既有值，与回填「title 非空即就绪」判据一致）；为空才用
    # ``_handoff_title`` 补（PR #112 验收过的提取逻辑，直接复用不改动）。
    if merged.get("type") == "task" and _is_handoff(merged):
        merged["kind"] = HANDOFF_KIND
        if not (merged.get("title") or "").strip():
            merged["title"] = _handoff_title(merged)

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


def _matches_filters(
    payload: dict, project: str, states: set, include_legacy: bool, include_handoff: bool = False
) -> bool:
    """活跃任务过滤：type / status / legacy / handoff + 状态集 + 项目。"""
    if payload.get("type") != "task":
        return False
    if payload.get("status") != "active":
        return False
    if not include_legacy and payload.get("legacy") is True:
        return False
    is_handoff = payload.get("kind") == HANDOFF_KIND
    if not include_handoff and is_handoff:
        return False
    # 接续点豁免状态判定：它没有「待办状态」语义——正文首行是接续点标记块，
    # ``parse_task_state`` 解析不出 → 生效状态 None → 按 {todo, doing, blocked}
    # 过滤会把 ``include_handoff=True`` 的显式取回也一并挡掉（生产 2363/2393
    # 实测，三条列表全隐形）。豁免**不是**塞假状态：``task_state`` 仍如实反映
    # 正文解析结果（解析不出就是 None），只是不再用它决定接续点的可见性。
    if not is_handoff:
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


def _iso_to_epoch(value: str) -> float | None:
    """ISO8601 字符串 → epoch 秒；解析不出 → ``None``（不猜）。

    naive（无时区）按 UTC 处理——与治理脚本既有约定一致：``_now_iso`` 的产物都带
    偏移，naive 只出现在手工写入的旧数据里，偏差至多是时区差，不影响 30 天级窗口。
    """
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _freshness_epoch(payload: dict) -> float | None:
    """新鲜度判据时间（epoch 秒，issue #128 A 项）。

    **``last_touched_at`` 与 ``created_at`` 取更早者**（2026-10-19 主人拍板）：
    存量任务的 ``last_touched_at`` 会被批量回填刷成同一天（实测生产库 9 条
    52 天前建的老任务全是 ``2026-10-08``），单看它会把「建了很久的老任务」
    显示成「11 天前动过」而永不退场；取更早者 = 「建得久 或 久未动，都算旧」。
    两个时间戳都缺 / 都解析不出 → ``None``，调用方视为新鲜（保留、不猜、不折叠）。
    """
    candidates: list[float] = []
    touched = payload.get("last_touched_at")
    if isinstance(touched, str) and touched.strip():
        epoch = _iso_to_epoch(touched)
        if epoch is not None:
            candidates.append(epoch)
    created = payload.get("created_at")
    if isinstance(created, (int, float)) and created > 0:
        candidates.append(float(created))
    elif isinstance(created, str) and created.strip():
        epoch = _iso_to_epoch(created)
        if epoch is not None:
            candidates.append(epoch)
    return min(candidates) if candidates else None


def _stale_age_days(payload: dict, now: float) -> float | None:
    """条目年龄（天，浮点）；判据缺失 → ``None``（不猜）。"""
    epoch = _freshness_epoch(payload)
    if epoch is None:
        return None
    return max(0.0, (now - epoch) / 86400.0)


def _order_handoff_first(items: list[dict], include_handoff: bool) -> list[dict]:
    """排序（issue #128 A2）：``include_handoff=True`` 时接续点**置顶**。

    接续点组内 ``importance`` 倒序、``id`` 倒序兜底；普通任务保持原排序
    （状态优先级 → ``last_touched_at`` 倒序 → ``importance`` 倒序）。取回接续点
    的目的就是「先读这条」，混排在旧实现里会被状态优先级压到后面。
    """
    if not include_handoff:
        items.sort(key=_sort_key)
        return items
    handoffs = [it for it in items if it.get("kind") == HANDOFF_KIND]
    others = [it for it in items if it.get("kind") != HANDOFF_KIND]
    handoffs.sort(key=lambda it: (-(_to_float(it.get("importance"), 0.0) or 0.0), -(it.get("id") or 0)))
    others.sort(key=_sort_key)
    return handoffs + others


def _fold_stale(items: list[dict], ages: dict[int, float | None], fresh_days: int) -> tuple[list[dict], dict | None]:
    """新鲜度折叠（issue #128 A1）：超期条目移出 ``results``，汇总成 ``stale`` 摘要。

    - ``fresh_days <= 0`` → 关闭折叠（原样返回全部、``stale=None``，兼容旧行为）；
    - ``kind=handoff`` 豁免折叠（跨会话交接记录天然可能很旧，进 results 不进汇总）；
    - ``stale.oldest.age_days`` 为整数天（向下取整，与治理脚本的 ``.days`` 口径一致）。
    """
    try:
        threshold = float(fresh_days)
    except (TypeError, ValueError):
        threshold = 30.0
    if threshold <= 0:
        return items, None
    visible: list[dict] = []
    stale: list[tuple[float, dict]] = []
    for it in items:
        age = ages.get(it["id"])
        if age is not None and age > threshold and it.get("kind") != HANDOFF_KIND:
            stale.append((age, it))
        else:
            visible.append(it)
    if not stale:
        return visible, None
    oldest_age, oldest = max(stale, key=lambda pair: pair[0])
    return visible, {
        "count": len(stale),
        "oldest": {
            "id": oldest.get("id"),
            "title": oldest.get("title") or "",
            "state": oldest.get("state") or "",
            "age_days": int(oldest_age),
        },
        "hint": "显式传 fresh_days=0 可展开全部",
    }


def list_active_tasks(
    store,
    project: str = "",
    states=(),
    limit: int = 15,
    include_legacy: bool = False,
    include_handoff: bool = False,
    fresh_days: int = 30,
) -> dict:
    """列出活跃任务（``type=task`` & ``status=active`` & 状态命中）。

    - ``states``：可传字符串（逗号分隔，如 ``"todo,doing,blocked"``）或序列；
      空 / 全为非法值 → 回退默认 ``DEFAULT_ACTIVE_STATES``。
    - ``project``：非空时只保留 ``payload.project`` 相等的节点。
    - ``include_legacy=False``（默认）排除 ``payload.legacy is True`` 的老节点。
    - ``include_handoff=False``（默认）排除 ``payload.kind == "handoff"`` 的会话
      接续点（跨会话交接记录而非待办；显式传 ``True`` 可取回，此时接续点置顶，
      见 ``_order_handoff_first``）。
    - ``fresh_days``（issue #128 A 项）：新鲜度窗口（天）。判据时间 =
      ``last_touched_at``（ISO），为空回退 ``created_at``（数值时间戳），两者都缺
      视为新鲜（保留、不猜、不折叠）。超过 ``fresh_days`` 天未触碰的条目不再逐条
      返回，汇总进返回体的 ``stale``（``count`` / ``oldest`` / ``hint``）；
      ``kind=handoff`` 豁免折叠；``fresh_days <= 0`` 关闭折叠（兼容旧行为）。

    返回 ``{"results": [...], "total": int, "states": [...], "stale": dict | None}``；
    ``total`` 是过滤后的总数（不受 ``limit`` 影响，**含被折叠的超期条目**——
    可见数 = ``len(results)``，折叠数 = ``stale.count``）；``stale`` 无超期项时为 ``None``。
    """
    state_set = _parse_states(states)
    now = time.time()
    items: list[dict] = []
    ages: dict[int, float | None] = {}
    for nid, node in store.iter_nodes():
        payload = node.get("payload") or {}
        if not _matches_filters(payload, project, state_set, include_legacy, include_handoff):
            continue
        state, _ = _resolve_effective_state(payload)
        items.append(_brief(nid, payload, state))
        ages[nid] = _stale_age_days(payload, now)

    items = _order_handoff_first(items, include_handoff)
    visible, stale = _fold_stale(items, ages, fresh_days)
    try:
        cap = max(1, int(limit))
    except (TypeError, ValueError):
        cap = 15
    return {"results": visible[:cap], "total": len(items), "states": sorted(state_set), "stale": stale}


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
        "created_at": payload.get("created_at"),
        "importance": payload.get("importance", 0.5),
        "kind": payload.get("kind") or "",
    }


# 接续点首行判据（2026-10-16 收紧）：``^`` 锚定行首，跳过前导空白与若干
# ``[...]`` 状态块（如 ``[完成] `` / ``[待办] ``），其后必须紧跟「接续点」
# 标题词（允许 ``【`` / ``★`` 前缀装饰）。「接续点」只出现在**句中**的不命中。
_HANDOFF_FIRST_LINE_RE = re.compile(r"^\s*(?:\[[^\[\]\n]*\]\s*)*【?★?接续点")


def _is_handoff(payload: dict) -> bool:
    """task 节点是否为会话接续点：content **首行以「接续点」作为标题词开头**。

    判据 ``_HANDOFF_FIRST_LINE_RE``：跳过首行前导的空白与若干 ``[...]`` 状态
    块（如 ``[完成] 【★接续点…】``）后，必须紧跟「接续点」标题词（允许 ``【``
    / ``★`` 前缀装饰）；块外正文直接以「接续点」开头（无 ``【】``）同样命中。
    **不看「接续点」出现在句中的任何位置。**

    旧判据（PR #111 初版）是「首行**包含** ``_HANDOFF_HINT``」，生产库实测
    两条误判（都是 type=task 的活跃真待办，逐字首行）：

    · ``[待办] Palimpsest「接续点 × 任务列表」解耦（走 PR，2026-10-16 立）``
      ——「接续点」只出现在**句中**：本条在**讨论**接续点机制，本身是真
      待办；包含匹配会把它打成 handoff，一条真待办就此从活跃列表隐形。
    · ``【历史留痕·记忆同行实测 2026-10-10 第一步】（⚠️ 本条为中途状态，不是当前接续点）``
      ——同为句中命中，且本条**自述**不是接续点。

    生产实测「包含」判据在 99 条 task 里命中 60 条 handoff——过宽；收紧为
    「标题词开头」后，只认真正的接续点形态（见 ``test_is_handoff_real_shapes``
    的 7 条逐字用例）。
    """
    first_line = (payload.get("content") or "").split("\n", 1)[0]
    return _HANDOFF_FIRST_LINE_RE.match(first_line) is not None


# title 摘要段落的分隔符：中点 ``·``、中文破折号 ``——``、斜杠与竖线的全/半角
# 形态。接续点标记的「日期+主题」段与锚词段之间几乎总由这些符号隔开。
_TITLE_SEGMENT_SPLIT_RE = re.compile(r"——|[·／/｜|]")

# 判「纯锚词段」前先剥掉的装饰：空白与常见纯符号（切段后残留的单个 ``—`` 等符号
# 段剥完即空 → 按空段丢弃）。
_TITLE_ANCHOR_STRIP_RE = re.compile(r"[\s·—|｜/／]+")

# 接续点标记的固定锚词：切段后整段只是这些词（含 ``★`` 前缀变体）的没有信息量，
# 一律丢弃——它们出现在**每一条**接续点上，不携带「这条是哪条」的区分度。
_TITLE_ANCHOR_STUBS = ("★接续点", "接续点", "会话接续", "新聊天先读", "新会话先读这条")

# title 摘要的截断上限。
_TITLE_MAX_LEN = 120


def _iter_title_fragments(first_line: str):
    """按出现顺序交错产出首行的文本片段：括号块内容 + 块外正文。

    块内（标记块的内容）与块外（正文）都可能是信息的载体（见 ``_handoff_title``
    的「为什么」）；片段边界同时是切段边界——两个相邻块不会被空白粘连成一段
    （``【接续点 / …新聊天先读】【2026-10-12 …主题…】`` 的锚词块与主题块各自成段）。
    """
    pos = 0
    for match in _iter_mark_blocks(first_line):
        if match.start() > pos:
            yield first_line[pos : match.start()]
        yield match.group(1)
        pos = match.end()
    if pos < len(first_line):
        yield first_line[pos:]


def _handoff_title(payload: dict) -> str:
    """task 节点的 title 摘要：块内 + 块外文本切段，取信息量最大的一段。

    **为什么「取信息量最大的段」而不是「先删块再取剩余」**（2026-10-16 验收
    实测反例）：旧规则把首行里的括号块整块删掉再取剩余文本，而生产上接续点的
    主流形态就是**整行一个块**（``【★接续点·2026-10-16 … · 新会话先读这条】``），
    删完只剩空串 → 回退常量「接续点」，等于没补（实测 ``tasks backfill
    --apply`` 后 4 条生产接续点的 title 仍无法互相区分）。反过来，若**只**取
    块内容，``接续点 / … —— 2026-10-13~14 大修 · [进行中] 细节`` 这类「块 +
    块外正文」形态又会把正文丢光、退化成 ``进行中`` 这类状态词。因此块内与块外
    文本**都**参与切段，哪段信息量最大（最长）取哪段。

    规则：
      1. 首行按括号块切成片段（块内容 + 块外正文，按出现顺序，见
         ``_iter_title_fragments``）；一个块都没有 → 整行就是一个片段；
      2. 每个片段按分隔符切段（``_TITLE_SEGMENT_SPLIT_RE``：``·`` ``——``
         ``/`` ``／`` ``｜`` ``|``）；
      3. 丢弃空段 / 纯符号段 / 纯锚词段（``_TITLE_ANCHOR_STUBS``）；
      4. 剩余段取**最长**的一段（长度并列取最先出现），截断到 120 字符；
      5. 仍为空（整行只有锚词，如 ``【★接续点】``）→ 退回 ``payload.task_key``
         → 再退回 ``"接续点"``。

    回填对 ``title`` 为空的 task 节点**统一**用它补摘要（不限于接续点）：非接续
    点没有锚词段，同一套规则自然退化成「首行原文 / 最长段」。
    """
    first_line = (payload.get("content") or "").split("\n", 1)[0]
    segments: list[str] = []
    for fragment in _iter_title_fragments(first_line):
        for seg in _TITLE_SEGMENT_SPLIT_RE.split(fragment):
            if not seg.strip():
                continue
            core = _TITLE_ANCHOR_STRIP_RE.sub("", seg)
            if not core or core in _TITLE_ANCHOR_STUBS:
                continue
            segments.append(seg.strip())
    # max 并列时保留最先出现的那段（严格大于才替换）
    best = max(segments, key=len) if segments else ""
    if len(best) > _TITLE_MAX_LEN:
        best = best[:_TITLE_MAX_LEN]
    return best or (payload.get("task_key") or _HANDOFF_HINT)


def backfill_task_state(store, dry_run: bool = True) -> dict:
    """存量 ``type=task`` 节点回填 ``task_state``。

    对每个 ``type=task``（且未回填过）的节点：
      - 从 content 首行解析状态写入 ``payload.task_state``；
      - 解析失败 → ``task_state="unknown"`` + ``needs_review=True``；
      - **按状态决定是否打 ``legacy``**：只有不再需要跟进的状态（``done`` /
        ``canceled`` / ``unknown``）才标 ``payload.legacy = True``（历史留痕，
        ``/tasks/active`` 默认不列出）；仍在跟进的状态（``todo`` / ``doing`` /
        ``blocked``）保持 legacy 缺失，**回填后即可出现在活跃任务视图里**。
        **接续点豁免**：``kind=handoff`` 的节点不打 ``legacy``（统一写 ``False``）——
        状态解析不出来自接续点标记块、不代表任务完结，显式 ``include_handoff=True``
        应能完整取回（见 ``_matches_filters`` 的状态豁免）。
      - **会话接续点**（content 首行**以「接续点」作为标题词开头**，见
        ``_is_handoff``）：打
        ``payload.kind = "handoff"`` 身份标记——``list_active_tasks`` 默认排除
        接续点（``include_handoff=True`` 可取回），排除前先补 title 便于显式
        取回时识别。
      - **``title`` 为空/缺失的 task 节点**（含接续点与普通任务）：补一行摘要
        （见 ``_handoff_title``）——活跃列表里空 title 同样读不出是哪条任务；
        幂等判据相应放宽为「title 非空即就绪」。

    为什么这样分：``legacy`` 的语义是「历史节点，不必再看」，不是「回填过的节点」。
    把仍在做的任务一并标成 legacy，会让回填后的工作集立刻变空——恰好废掉这套
    改造的目的。

    ``dry_run=True``（默认）只统计不写库；``--apply`` 才落盘。

    **两段式（先快照后写）**：``iter_nodes`` 在整个迭代期间持有库连接
    （进程级 DB 访问锁），迭代中调 ``update_payload`` 会在同进程内重入取锁而自锁
    ——因此先把候选节点整体快照出来、迭代结束（连接释放）后再逐个写回。

    返回 ``{dry_run, scanned, already, parsed, unknown, needs_review, legacy, handoff, changes}``；
    ``changes`` 为每个将变更 / 已变更节点的摘要（dry-run 预览 = 执行决策）。
    """
    scanned = 0
    already = 0
    parsed = 0
    unknown = 0
    legacy_count = 0
    handoff_count = 0
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
        # 接续点判定 + title 是否已补（目标判据：title 非空即就绪——不要求它一定
        # 出自本函数，人工改过的 title 同样算就绪）
        handoff = _is_handoff(payload)
        # 该节点**应该**是什么 legacy 值：不活跃状态才标 legacy。**接续点豁免**：
        # 接续点没有「待办状态」语义，状态解析不出（unknown）不等于「任务已完结、
        # 不必再看」；打了 kind=handoff 后默认视图已由 kind 排除，若再按 unknown
        # 打 legacy=True，会把 include_handoff=True 的显式取回也一并挡掉——显式
        # 开关应当能完整取回全部接续点（生产 2363/2393 回归根因之二）。
        want_legacy = (state not in DEFAULT_ACTIVE_STATES) and not handoff
        title_done = bool((payload.get("title") or "").strip())
        # 幂等：已是目标形态（task_state 相符、legacy 相符、title 非空即就绪；
        # 接续点还要求 kind 已打）→ 跳过，重跑输出 0 变更。接续点的 legacy 判据
        # 相应放宽为「非 True 即就绪」：豁免后 legacy 不再是接续点的语义字段，
        # 存量若被旧逻辑打了 True 才回写 False。
        legacy_done = payload.get("legacy") is not True if handoff else payload.get("legacy") is want_legacy
        if (
            payload.get("task_state") == state
            and legacy_done
            and title_done
            and (not handoff or payload.get("kind") == HANDOFF_KIND)
        ):
            already += 1
            continue
        changes.append(
            {
                "id": nid,
                "task_state": state,
                "needs_review": state == UNKNOWN_STATE,
                "legacy": want_legacy,
                "title": _task_label(payload),
                "handoff": handoff,
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
        # title 为空一律补摘要（接续点与普通任务同一套规则，见 ``_handoff_title``）
        if not title_done:
            new_payload["title"] = _handoff_title(payload)
        if handoff:
            new_payload["kind"] = HANDOFF_KIND
            handoff_count += 1
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
        "handoff": handoff_count,
        "changes": changes,
    }
