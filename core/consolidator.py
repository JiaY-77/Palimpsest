"""容量自动合并：扫描记忆库，找相似度过高的 memory 节点对，dry-run 先预览、apply 才合并。"""

import logging
from typing import Any

from core.trivium_store import TriviumStore

logger = logging.getLogger(__name__)


def find_candidates(
    store: TriviumStore,
    sim_threshold: float = 0.85,
) -> list[dict]:
    """遍历所有 active+memory 节点，用原始向量 search 找相似度超阈值的节点对。

    返回 [{'a': id_a, 'b': id_b, 'score': float,
             'a_imp': importance_a, 'b_imp': importance_b,
             'a_content': 前80字, 'b_content': 前80字}]
    去重：a<b 按 id 排序，避免重复对。
    """
    # 收集所有 active + type=memory 节点的 id 和向量
    memory_nodes: list[dict] = []
    db = None
    try:
        db = store._acquire()
        for nid in db.all_node_ids():
            node = db.get(nid)
            if not node:
                continue
            payload = node.payload or {}
            if payload.get("type") != "memory":
                continue
            if payload.get("status") != "active":
                continue
            vec = node.vector
            if not vec:
                continue
            memory_nodes.append({
                "id": nid,
                "vector": vec,
                "payload": payload,
            })
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    # 对每个节点做向量 search 找相似
    seen_pairs: set[tuple[int, int]] = set()
    candidates: list[dict] = []

    db2 = None
    try:
        db2 = store._acquire()
        for mn in memory_nodes:
            nid = mn["id"]
            vec = mn["vector"]
            hits = db2.search(vec, top_k=6, min_score=0.0, expand_depth=0)
            for hit in hits or []:
                hid = hit.id
                score = float(hit.score)
                if hid == nid:
                    continue
                if score < sim_threshold:
                    continue
                # 只接受对方也是 active + memory
                hnode = db2.get(hid)
                if not hnode:
                    continue
                hpayload = hnode.payload or {}
                if hpayload.get("type") != "memory":
                    continue
                if hpayload.get("status") != "active":
                    continue
                # 去重：a<b 按 id 排序
                a, b = (nid, hid) if nid < hid else (hid, nid)
                pair = (a, b)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                # 取双方 payload
                a_payload = mn["payload"] if a == nid else hpayload
                b_payload = hpayload if b == hid else mn["payload"]
                a_imp = _to_float(a_payload.get("importance"), 0.5)
                b_imp = _to_float(b_payload.get("importance"), 0.5)
                candidates.append({
                    "a": a,
                    "b": b,
                    "score": round(score, 4),
                    "a_imp": round(a_imp, 2),
                    "b_imp": round(b_imp, 2),
                    "a_content": (a_payload.get("content") or "")[:80],
                    "b_content": (b_payload.get("content") or "")[:80],
                })
    finally:
        if db2 is not None:
            try:
                db2.close()
            except Exception:
                pass

    # 按 score 降序
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def consolidate(
    store: TriviumStore,
    dry_run: bool = True,
    sim_threshold: float = 0.85,
    max_importance: float = 0.8,
) -> dict:
    """扫描并合并高相似度 memory 节点。

    dry_run=True  只预览候选，不修改任何数据。
    dry_run=False 执行合并：新建合并节点，旧节点标 outdated，建 REVISED_BY 边。
    """
    candidates = find_candidates(store, sim_threshold=sim_threshold)

    # 过滤保护规则
    merged = 0
    skipped_high_value = 0
    skipped_both_important = 0
    skipped_ids: list[dict] = []
    will_merge: list[dict] = []

    for c in candidates:
        a_imp = c["a_imp"]
        b_imp = c["b_imp"]
        # 任一方 importance >= max_importance → 跳过（保护高价值）
        if a_imp >= max_importance or b_imp >= max_importance:
            skipped_high_value += 1
            skipped_ids.append({**c, "reason": "high_value"})
            continue
        # 双方 importance 都 >= 0.4 → 跳过（都算重要）
        if a_imp >= 0.4 and b_imp >= 0.4:
            skipped_both_important += 1
            skipped_ids.append({**c, "reason": "both_important"})
            continue
        will_merge.append(c)

    if dry_run:
        return {
            "candidates": candidates,
            "will_merge": will_merge,
            "skipped_high_value": skipped_high_value,
            "skipped_both_important": skipped_both_important,
            "dry_run": True,
        }

    # ---- 执行合并 ----
    merged_ids: list[dict] = []
    db = None
    try:
        db = store._acquire()
        for c in will_merge:
            a_id, b_id = c["a"], c["b"]
            a_node = db.get(a_id)
            b_node = db.get(b_id)
            if not a_node or not b_node:
                continue
            a_payload = a_node.payload or {}
            b_payload = b_node.payload or {}

            # 高 importance 方保留内容
            a_imp = c["a_imp"]
            b_imp = c["b_imp"]
            if a_imp >= b_imp:
                high_payload, low_payload = a_payload, b_payload
                low_id = b_id
            else:
                high_payload, low_payload = b_payload, a_payload
                low_id = a_id

            high_content = high_payload.get("content") or ""
            merge_content = (
                high_content
                + f"\n\n（由 Palimpsest 自动合并自节点 {low_id}，原内容见 REVISED_BY 链）"
            )
            merge_importance = max(c["a_imp"], c["b_imp"])

            new_node_data = {
                "type": "memory",
                "content": merge_content,
                "importance": merge_importance,
                "character_name": high_payload.get("character_name", ""),
                "label": high_payload.get("label", ""),
                "source": "consolidate",
            }
            # 用高 importance 方的向量作为合并节点向量
            high_vec = a_node.vector if (a_imp >= b_imp) else b_node.vector
            new_id = db.insert(high_vec, new_node_data)

            # 旧节点标 outdated
            a_payload["status"] = "outdated"
            db.update_payload(a_id, a_payload)
            b_payload["status"] = "outdated"
            db.update_payload(b_id, b_payload)

            # 建边：新节点 REVISED_BY -> 两个旧节点
            db.link(new_id, a_id, label="REVISED_BY", weight=c["score"])
            db.link(new_id, b_id, label="REVISED_BY", weight=c["score"])

            merged += 1
            merged_ids.append({
                "new_id": new_id,
                "old_a": a_id,
                "old_b": b_id,
                "score": c["score"],
            })
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass

    return {
        "candidates": candidates,
        "merged": merged,
        "merged_ids": merged_ids,
        "skipped_high_value": skipped_high_value,
        "skipped_both_important": skipped_both_important,
        "dry_run": False,
    }


def _to_float(value: Any, default: float) -> float:
    """安全转 float，失败用默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
