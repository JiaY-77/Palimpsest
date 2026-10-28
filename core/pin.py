"""常驻域（pin domain）：**每轮无条件注入**的规则块（issue #176）。

## 为什么需要它

`MEMORY.md` 每轮全量注入，但有 2200 字硬顶（当时已 99% 满）——它是「agent 必须
永远知道的东西」的容量上限。小帕存得更多，但召回是 **relevance-gated** 的
（`prefetch_top_k` 默认 3、`prefetch_min_score` 默认 0.3）：一条与当前消息语义
不相似的常驻规则，永远进不了注入。

常驻域提供第三条路：某个 domain 的节点**无条件**拼进 pre-turn 的注入文本，不参与
相似度竞争；成本用**节点边界的字符预算**兜住，超预算如实报告而不是静默截半个节点。

## 纯度约定

本模块**不碰存储**：调用方把 `(id, payload)` 列表交进来，这里只做筛选、排序、
拼装、预算截断。这样可以直接对内存列表写单测，不需要库。
"""

from __future__ import annotations

from typing import Any

# 只注入仍在生效的节点。outdated（被新版取代）与 archived 一律跳过——常驻不等于
# 常驻旧版本：被取代的规则继续每轮注入，等于把作废的指令钉在 prompt 里。
PIN_STATUS_ACTIVE = "active"

# 单块默认预算（字符）。3000 是 issue #176 的起点值：与 MEMORY.md 的 2200 同一量级，
# 略大一些容纳条目标签与分隔符，仍远小于一次典型对话的上下文。
PIN_DEFAULT_BUDGET_CHARS = 3000

# 单条上限：防止一条超长记忆（如整篇文档切片误入常驻域）吃光整块预算。
PIN_PER_NODE_MAX_CHARS = 600


def select_pin_nodes(entries: list[tuple[int, dict[str, Any]]], *, domain: str) -> list[tuple[int, dict[str, Any]]]:
    """挑出常驻节点并按注入优先级排序。

    - 判据：``payload.domain == domain`` 且 ``payload.status == "active"``（或缺省
      status，视为 active —— 老节点没有这个字段）。
    - 排序：``importance`` 降序，同 importance 时 ``id`` 降序（新写的排前面）——
      确定性排序，保证同样的库每次注入同样的顺序，便于比对与排错。

    非字典 payload / 缺 content 的节点直接跳过（渲染阶段无从渲染）。
    """
    picked: list[tuple[int, dict[str, Any]]] = []
    for nid, payload in entries:
        if not isinstance(payload, dict):
            continue
        if str(payload.get("domain") or "") != domain:
            continue
        status = payload.get("status")
        if status is not None and str(status) != PIN_STATUS_ACTIVE:
            continue
        if not str(payload.get("content") or "").strip():
            continue
        picked.append((int(nid), payload))

    picked.sort(key=lambda item: (float(item[1].get("importance") or 0.0), item[0]), reverse=True)
    return picked


def compose_pin_block(
    nodes: list[tuple[int, dict[str, Any]]],
    *,
    domain: str,
    budget_chars: int = PIN_DEFAULT_BUDGET_CHARS,
) -> dict[str, Any]:
    """把常驻节点拼成可直接注入的一段文本，按**节点边界**截断。

    返回::

        {
          "text": str,          # 空串 = 不注入（无节点 / 预算连一条都放不下）
          "node_count": int,    # 实际注入的节点数
          "chars": int,         # text 的实际字符数
          "truncated": bool,    # 是否因预算丢掉了节点
          "dropped": int,       # 因预算未注入的节点数
          "ids": [int, ...],    # 实际注入的节点 id（调用方用于与语义召回去重）
        }

    截断策略：**不切节点**——下一个节点放不进预算就停，绝不截半个节点（半个规则的
    危害大于少一条规则）。``truncated``/``dropped`` 让「预算不够」变成可观测事实，
    而不是悄悄少注入。
    """
    budget = max(0, int(budget_chars))
    header = f"[Palimpsest 常驻规则 · domain={domain}]"
    # 头部本身也占预算；预算小于头部时直接不注入（宁可不注入，也不注入一条被截断的头部）
    if not nodes or budget <= len(header):
        return {
            "text": "",
            "node_count": 0,
            "chars": 0,
            "truncated": bool(nodes),
            "dropped": len(nodes),
            "ids": [],
        }

    lines = [header]
    used = len(header)
    injected: list[int] = []
    for nid, payload in nodes:
        body = str(payload.get("content") or "").strip()
        if not body:
            continue
        if len(body) > PIN_PER_NODE_MAX_CHARS:
            body = body[:PIN_PER_NODE_MAX_CHARS]
        line = f"- {body}"
        cost = len(line) + 1  # +1 = 换行
        if used + cost > budget:
            break
        lines.append(line)
        used += cost
        injected.append(nid)

    dropped = len(nodes) - len(injected)
    if injected and dropped:
        # 如实报告：这条尾巴本身也算预算（放得下才加，放不下就只保留正文）
        tail = f"（预算 {budget} 字符已满，另有 {dropped} 条未注入）"
        if used + len(tail) + 1 <= budget:
            lines.append(tail)
            used += len(tail) + 1

    text = "\n".join(lines) if injected else ""
    return {
        "text": text,
        "node_count": len(injected),
        "chars": len(text),
        "truncated": dropped > 0,
        "dropped": dropped,
        "ids": injected,
    }


def pin_diagnostics(block: dict[str, Any]) -> dict[str, Any]:
    """从 ``compose_pin_block`` 的结果里抽出**诊断字段**（不含正文，避免响应体膨胀）。"""
    return {
        "node_count": int(block.get("node_count") or 0),
        "chars": int(block.get("chars") or 0),
        "truncated": bool(block.get("truncated")),
        "dropped": int(block.get("dropped") or 0),
    }
