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

状态取值与 ``content`` 首行标记的映射见 ``PARSE_MAP``。解析不出时返回 ``None``
（``unknown`` 只在回填兜底时使用），绝不猜测——猜错的状态比没有状态更危险。
"""

import logging
import re
from datetime import datetime, timezone

from core.utils import _to_float

logger = logging.getLogger(__name__)

# 任务的全部合法状态（白名单）。显式传入 payload.task_state 时必须命中其一。
TASK_STATES = frozenset({"todo", "doing", "blocked", "done", "canceled", "unknown"})

# 回填时解析不出状态 → unknown（并打 needs_review 标记，交给人工复核）。
UNKNOWN_STATE = "unknown"

# content 首行第一个方括号词 → 规范化状态。
# 只认首行第一个 [xxx]；同名标记（如「部分完成」）按语义映射，不按字面猜。
PARSE_MAP = {
    "todo": "todo",
    "待办": "todo",
    "待启动": "todo",
    "已拍板·待执行": "todo",
    "doing": "doing",
    "进行中": "doing",
    "部分完成": "doing",
    "blocked": "blocked",
    "挂账": "blocked",
    "阻塞": "blocked",
    "done": "done",
    "完成": "done",
    "已完成": "done",
    "canceled": "canceled",
    "取消": "canceled",
}

# 活跃任务默认状态集（done / canceled 不再需要跟进）。
DEFAULT_ACTIVE_STATES = ("todo", "doing", "blocked")

# 排序优先级：doing 最紧（正在做）→ blocked（卡住要救）→ todo（排队）。
_STATE_PRIORITY = {"doing": 0, "blocked": 1, "todo": 2}

# 首行第一个方括号：``[`` 后跟 1~20 个非括号字符再 ``]``。
# 限定长度既避免把正文里的长括号（如引用块）误当状态标记，也顺带挡掉
# 跨行贪婪匹配——``.`` 不匹配 ``\n``，所以永远是「首行内」。
_LEAD_MARK_RE = re.compile(r"\[\s*([^\[\]\n]{1,20}?)\s*\]")

# 否定/未完成提示：命中即不解析（如「还没完成」「未完成」——
# 不能因为句子里出现「完成」就判成 done）。
_NEGATION_HINTS = ("未完成", "还没", "尚未", "未开始", "未能", "暂未", "没有完成")


def _normalize(value: str) -> str:
    """标记词归一：去首尾空白 + 折叠内部空白 + 小写。

    大小写/空格容错：``[ Doing ]`` / ``[DOING]`` / ``[doing]`` 都得到 ``doing``。
    """
    return re.sub(r"\s+", "", value or "").strip().lower()


def parse_task_state(content: str) -> str | None:
    """从 ``content`` 首行第一个 ``[xxx]`` 解析任务状态。

    规则（与 ``PARSE_MAP`` 对应）：
      - 只解析**首行第一个**方括号，正文里后续括号一律忽略；
      - 标记词大小写 / 首尾空格 / 内部空格容错；
      - ``[部分完成]`` → ``doing``、``[已拍板·待执行]`` → ``todo``（语义映射）；
      - 无法识别、无括号、或命中否定提示（「未完成」等）→ 返回 ``None``（不猜）。

    返回规范化状态字符串或 ``None``。
    """
    text = content or ""
    first_line = text.split("\n", 1)[0]
    # 否定判定只看第一个标记之前的文本（标记之后的正文不参与否定判定，
    # 否则「[done] 未完成的收尾」会被误判）。为覆盖「还没 [done]」这种
    # 标记本身带否定前缀的写法，把标记内容也纳入否定检查。
    match = _LEAD_MARK_RE.search(first_line)
    prefix = first_line[: match.start()] if match else first_line
    if match:
        prefix += match.group(1)
    for hint in _NEGATION_HINTS:
        if hint in prefix:
            return None
    if not match:
        return None
    return PARSE_MAP.get(_normalize(match.group(1)))


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
        _write_state_log(store, node_id, merged, previous_state, new_state, now_iso)

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
    # 去掉首行开头的状态标记，只留正文，便于日志可读
    return _LEAD_MARK_RE.sub("", first_line, count=1).strip()


def _write_state_log(store, node_id: int, payload: dict, previous_state, new_state: str, now_iso: str) -> None:
    """写一条状态变更日志节点并建 ``record -[LOGS]-> task`` 边。

    content 格式：``[{new_state}] {task_key} {任务名} · {YYYY-MM-DD}``
    （previous_state 一并写进 payload，便于回溯「从哪来」）。
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
        "created_at": now_iso,
    }
    log_id = store.insert_node(log_payload, store.embed_text(content))
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
      - 打 ``payload.legacy = True``（标记「老节点」，``/tasks/active`` 默认不再列出）；
      - 从 content 首行解析状态写入 ``payload.task_state``；
      - 解析失败 → ``task_state="unknown"`` + ``needs_review=True``。

    ``dry_run=True``（默认）只统计不写库；``--apply`` 才落盘。

    **两段式（先快照后写）**：``iter_nodes`` 在整个迭代期间持有库连接
    （进程级 DB 访问锁），迭代中调 ``update_payload`` 会在同进程内重入取锁而自锁
    ——因此先把候选节点整体快照出来、迭代结束（连接释放）后再逐个写回。

    返回 ``{dry_run, scanned, already, parsed, unknown, needs_review, changes}``；
    ``changes`` 为每个将变更 / 已变更节点的摘要（dry-run 预览 = 执行决策）。
    """
    scanned = 0
    already = 0
    parsed = 0
    unknown = 0
    changes: list[dict] = []
    pending: list[tuple[int, dict]] = []  # (node_id, 待写 payload)，迭代结束再写

    for nid, node in store.iter_nodes():
        payload = node.get("payload") or {}
        if payload.get("type") != "task":
            continue
        scanned += 1
        # 幂等：已回填（legacy + 合法 task_state）的节点跳过，重跑输出 0 变更
        if payload.get("legacy") is True and payload.get("task_state") in TASK_STATES:
            already += 1
            continue
        state = parse_task_state(payload.get("content") or "")
        if state is None:
            state = UNKNOWN_STATE
            unknown += 1
        else:
            parsed += 1
        changes.append(
            {
                "id": nid,
                "task_state": state,
                "needs_review": state == UNKNOWN_STATE,
                "title": _task_label(payload),
            }
        )
        new_payload = {**payload, "legacy": True, "task_state": state}
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
        "changes": changes,
    }
