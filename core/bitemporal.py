"""事实时间字段（bi-temporal）—— 世界时间维度的读写契约。

Palimpsest 原本只记「记录时间」（payload ``created_at``，数值 ``time.time()``，
表示这条记录何时被写入系统）。但知识的时效性需要第二个维度：**这个事实在世界里
从何时为真、到何时不再为真**。两者正交：

    世界时间（新增）        系统时间（已有）
    valid_at   事实开始为真    created_at   记录何时写入
    invalid_at 事实不再为真    expired_at   记录何时被标为历史

字段语义（对齐 Graphiti 的 bi-temporal 设计，见
``docs/BITEMPORAL.md``）：

- ``valid_at``：事实开始为真的世界时间。缺省等于写入时刻（``created_at``）——
  「写下即认为当下为真」。未来时态的计划/承诺，调用方可显式传入。
- ``invalid_at``：事实停止为真的世界时间。由新事实取代旧事实时，
  置为新事实的 ``valid_at``。
- ``expired_at``：**系统**把这条记录标为历史（``outdated``）的时刻。
  与 ``invalid_at`` 的区别：``invalid_at`` 是世界层面「不再为真」，
  ``expired_at`` 是系统层面「这条记录不再是我当前采纳的版本」。
  两者在简单场景下常常同时发生，但语义不同（例如一条被撤销后又发现仍然成立的
  事实：``invalid_at`` 可能被修正，``expired_at`` 仍是当初标记的时刻）。

时间戳一律用**数值** ``time.time()``（与 ``created_at`` 一致）——写 ISO 字符串会让
``/mem/recent`` 的 ``created_at`` 排序在 float/str 之间抛 TypeError。

豁免：``kb_chunk``（知识库切片）是外来文档的语义索引，没有「事实何时为真」的概念，
本模块的函数对其返回空（不打时间字段）。``record`` / ``event`` / ``git_commit`` 等
历史留痕类型同理不参与（它们本身就是时间点日志）。
"""

from __future__ import annotations

import time

# 参与 bi-temporal 事实时间打标的类型（与冲突检测的白名单同源）：
# 只有这些类型表示「一条关于世界的事实」，才有「何时为真」的概念。
FACT_TYPES = frozenset({"memory", "task", "plan"})

# 四个时间字段名（三个新增 + 系统时间）。集中在此，避免各处硬编码字符串。
VALID_AT = "valid_at"
INVALID_AT = "invalid_at"
EXPIRED_AT = "expired_at"
RECORDED_AT = "created_at"


def is_fact_type(payload: dict) -> bool:
    """该节点是否为「事实类」节点（需要 bi-temporal 时间字段）。

    kb_chunk 与历史留痕类型（record / event / git_commit / review / correction）
    不属于事实类——它们不需要「何时开始为真」的世界时间语义。
    """
    return payload.get("type") in FACT_TYPES


def stamp_new_fact(payload: dict, now: float | None = None) -> dict:
    """为**新写入**的事实节点补 ``valid_at``。

    仅在 ``payload`` 尚未带 ``valid_at`` 时写入（幂等）：显式传入的时间
    （未来时态的计划/承诺）不被覆盖。

    就地修改并返回 ``payload``（与写入链其它字段的构造方式一致）。
    非事实类型原样返回。
    """
    if not is_fact_type(payload):
        return payload
    if payload.get(VALID_AT) is None:
        payload[VALID_AT] = now if now is not None else time.time()
    return payload


def mark_superseded(old_payload: dict, new_valid_at: float | None, now: float | None = None) -> dict:
    """为**被取代**的旧事实补 ``invalid_at`` / ``expired_at``。

    - ``invalid_at`` = 取代者的 ``valid_at``（旧事实从新事实为真之时起不再为真）；
      取代者无 ``valid_at`` 时退回用 ``now``。
    - ``expired_at`` = 本次系统标记的时刻（``now``）；若已有值则不覆盖
      （第一次被标为历史的时刻才是「进入历史」的时刻）。

    就地修改并返回 ``old_payload``。非事实类型原样返回。
    """
    if not is_fact_type(old_payload):
        return old_payload
    ts = now if now is not None else time.time()
    if old_payload.get(INVALID_AT) is None:
        old_payload[INVALID_AT] = new_valid_at if new_valid_at is not None else ts
    if old_payload.get(EXPIRED_AT) is None:
        old_payload[EXPIRED_AT] = ts
    return old_payload


def time_fields(payload: dict) -> dict:
    """抽取一条 payload 的 bi-temporal 时间字段（供检索结果 meta 使用）。

    只返回存在的键；全无时返回空 dict（调用方据此决定是否放进 meta）。
    """
    out = {}
    for key in (VALID_AT, INVALID_AT, EXPIRED_AT, RECORDED_AT):
        val = payload.get(key)
        if val is not None:
            out[key] = val
    return out


def is_valid_at(payload: dict, as_of: float | None) -> bool:
    """在时间点 ``as_of`` 回看时，该事实是否「当时为真」（as_of 历史视图判据）。

    一条事实在 ``as_of`` 时刻为真，当且仅当::

        valid_at <= as_of   AND   (invalid_at 缺失 或 invalid_at > as_of)

    - ``as_of`` 为 None → 恒返回 True（关闭 as_of，调用方按原行为处理）。
    - 非事实类型（kb_chunk / record 等）没有世界时间语义 → 恒 True（不受
      as_of 影响，避免静默丢弃）。
    - 字段缺失兜底（历史数据无时间字段）：
        * ``valid_at`` 缺失 → 视为「一直在为真」→ 不过滤（保守，宁多回不丢）；
        * ``invalid_at`` 缺失 → 视为「仍为真」→ 通过。
    """
    if as_of is None:
        return True
    if not is_fact_type(payload):
        return True
    valid_at = payload.get(VALID_AT)
    if valid_at is not None and valid_at > as_of:
        # as_of 早于事实开始为真之时 → 当时还不为真
        return False
    invalid_at = payload.get(INVALID_AT)
    # 事实在 as_of 之前（或恰在那一刻）已停止为真 → 当时不为真
    return not (invalid_at is not None and invalid_at <= as_of)
