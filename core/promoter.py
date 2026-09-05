# -*- coding: utf-8 -*-
"""
core.promoter —— 高频记忆自动升级（记忆生命周期：promote）
=========================================================
让「反复被检索命中的记忆」自然浮出：hit_count 追踪（见 trivium_store 的
search_similar 回写）+ 候选推荐 + 升权打标，为人工升级知识库提供入口。

保守设计、可逆、人工兜底：
  - CLI 默认 dry-run 只打印候选，--apply 才写库（交互模式同 consolidate）。
  - 升权只打标（importance 微涨 + promoted 标记），【不】自动升级为 kb_chunk——
    真正升级知识库留给人工 review（有意为之的边界，不扩展）。
  - 幂等：已 promoted 且 hit_count 未超过上次的 promoted_hit_base 的节点跳过，
    再次 apply 输出 0 变更。
"""

import logging
import time

from core.trivium_store import TriviumStore
from core.utils import _to_float

logger = logging.getLogger(__name__)


def _to_int(value, default: int) -> int:
    """安全转 int，失败用默认值。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def find_promote_candidates(
    store: TriviumStore,
    days: int = 30,
    min_hits: int = 5,
) -> list[dict]:
    """扫描高频命中且未达高价值的 active 记忆节点，作为升级候选。

    候选定义（全部满足）：
      - hit_count >= min_hits（窗口门槛，用 days 过滤命中活跃度，近似即可）
      - importance < 0.8（已近高价值不重复升权）
      - status == "active"
      - type != "kb_chunk"（知识块不属于记忆升级候选）

    days 窗口：近似用 payload.last_hit_at 判定命中是否落在窗口内；
    缺失/非法 last_hit_at 视为「活跃度未知」，不据此排除（保守放行）。
    返回 [{'id', 'content', 'type', 'domain', 'importance', 'hit_count', 'suggested_action'}]。
    """
    now = time.time()
    window = max(1, int(days)) * 86400.0
    candidates: list[dict] = []
    for nid, payload in store.iter_payloads():
        if payload.get("type") == "kb_chunk":
            continue
        if payload.get("status") != "active":
            continue
        imp = _to_float(payload.get("importance"), 0.5)
        if imp >= 0.8:
            continue
        hit_count = _to_int(payload.get("hit_count"), 0)
        if hit_count < min_hits:
            continue
        # days 窗口：命中活跃度近似（last_hit_at 缺失/非法不排除）
        last_hit = payload.get("last_hit_at")
        if last_hit:
            try:
                if (now - float(last_hit)) > window:
                    continue
            except (TypeError, ValueError):
                pass
        candidates.append({
            "id": nid,
            "content": (payload.get("content") or "")[:60],
            "type": payload.get("type", ""),
            "domain": (payload.get("domain", "")
                       or payload.get("character_name", "") or ""),
            "importance": round(imp, 2),
            "hit_count": hit_count,
            "suggested_action": "升权打标（importance +0.1，供人工 review 升级知识库）",
        })
    candidates.sort(key=lambda c: c["hit_count"], reverse=True)
    return candidates


def _is_idempotent_skip(payload: dict, hit_count: int) -> bool:
    """幂等判定：已 promoted 且 hit_count 未超过 promoted_hit_base 的节点跳过。

    第二次 apply 时，上次写入的 promoted_hit_base 等于当时 hit_count；
    本轮 hit_count 未增长（<= base）则跳过，保证重复 apply 输出 0 变更。
    """
    if not payload.get("promoted"):
        return False
    base = payload.get("promoted_hit_base")
    if base is None:
        return False
    return hit_count <= _to_int(base, 0)


def promote(
    store: TriviumStore,
    dry_run: bool = True,
    days: int = 30,
    min_hits: int = 5,
) -> dict:
    """执行高频记忆升级。

    dry_run=True（默认）：只预览候选，不修改任何数据。
    dry_run=False：对非幂等跳过节点升权打标：
      - importance 提升为 min(原值+0.1, 0.9)，原值存 payload.prev_importance；
      - 打标 payload.promoted=true、payload.promoted_at=当前时间戳；
      - 记录 payload.promoted_hit_base=当前 hit_count（供幂等再次 apply）。

    返回 {'dry_run', 'days', 'min_hits', 'candidates', 'promoted_count',
             'skipped'（幂等跳过数），'changes'（dry_run=False 时的具体变更）}。
    """
    candidates = find_promote_candidates(store, days=days, min_hits=min_hits)
    if dry_run:
        return {
            "dry_run": True,
            "days": days,
            "min_hits": min_hits,
            "candidates": candidates,
            "candidate_count": len(candidates),
        }

    now = time.time()
    promoted_list: list[dict] = []
    skipped = 0
    for c in candidates:
        nid = c["id"]
        node = store.get_node(nid)
        if not node:
            skipped += 1
            continue
        payload = dict(node.get("payload", {}) or {})
        hit_count = _to_int(payload.get("hit_count"), 0)
        # 幂等：已升权且命中未增长 → 跳过，避免重复升权
        if _is_idempotent_skip(payload, hit_count):
            skipped += 1
            continue
        old_imp = _to_float(payload.get("importance"), 0.5)
        new_imp = round(min(old_imp + 0.1, 0.9), 2)
        payload["prev_importance"] = old_imp
        payload["importance"] = new_imp
        payload["promoted"] = True
        payload["promoted_at"] = now
        payload["promoted_hit_base"] = hit_count
        store.update_payload(nid, payload)
        promoted_list.append({
            "id": nid,
            "old_importance": round(old_imp, 2),
            "new_importance": new_imp,
            "hit_count": hit_count,
        })

    return {
        "dry_run": False,
        "days": days,
        "min_hits": min_hits,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "promoted_count": len(promoted_list),
        "skipped": skipped,
        "changes": promoted_list,
    }
