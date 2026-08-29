"""容量自动合并：扫描记忆库，找相似度过高的 memory 节点对，dry-run 先预览、apply 才合并。"""

import logging

from core.trivium_store import TriviumStore, node_domain

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

    P2 重构（T054）：改为调用 store.find_similar_pairs(sim_threshold)，
    由 store 负责原始扫描（单连接、a<b 去重、双方须 active+memory、按 score 降序）；
    此处保留 sim_threshold 阈值过滤语义（find_similar_pairs 内部已按该阈值过滤+排序）。
    """
    return store.find_similar_pairs(sim_threshold=sim_threshold)


def _filter_candidates(
    candidates: list[dict],
    max_importance: float,
) -> tuple[list[dict], int, int, list[dict]]:
    """按保护规则过滤候选对，拆分为 will_merge 与各类 skipped。

    返回 (will_merge, skipped_high_value, skipped_both_important, skipped_ids)。
    """
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

    return will_merge, skipped_high_value, skipped_both_important, skipped_ids


def _apply_merge(store: TriviumStore, will_merge: list[dict]) -> tuple[int, list[dict]]:
    """真正执行合并：新建合并节点、旧节点标 outdated、建 REVISED_BY 边。

    返回 (merged, merged_ids)。

    P2 重构（T054）：改用 store 公共方法——store.insert_node / store.update_payload /
    store.create_edge，不再直接 _acquire。注意 store.insert_node 会触发 secret_scan
    （原 db.insert 不会）：合并节点内容来自库内已有记忆（尾部含「由 Palimpsest 自动
    合并自节点」字样，不含敏感信息），正常能通过。
    """
    merged = 0
    merged_ids: list[dict] = []
    for c in will_merge:
        a_id, b_id = c["a"], c["b"]
        a_node = store.get_node(a_id)
        b_node = store.get_node(b_id)
        if not a_node or not b_node:
            continue
        a_payload = a_node.get("payload") or {}
        b_payload = b_node.get("payload") or {}

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
        # domain 统一（2026-08-29）：合并节点以 node_domain 为准，domain 与
        # character_name 镜像同值（消除二义性）。general 为未分类兜底。
        merge_domain = node_domain(high_payload)

        new_node_data = {
            "type": "memory",
            "content": merge_content,
            "importance": merge_importance,
            "domain": merge_domain,
            "character_name": merge_domain,
            "label": high_payload.get("label", ""),
            "source": "consolidate",
        }
        # 用高 importance 方的向量作为合并节点向量
        high_vec = (a_node.get("vector")
                    if a_imp >= b_imp else b_node.get("vector"))
        new_id = store.insert_node(new_node_data, high_vec)

        # 崩溃安全合并顺序（T060 审计整改）：先建边、后标脏。
        # 破坏性操作（update_payload 标 outdated 旧节点）放最后——若建边失败，
        # 旧节点仍 active，不会出现「旧已脏、新没连」的半合并状态；新建节点即使
        # 孤立也比数据损坏好（可由 consolidate 重跑或手动清理）。
        store.create_edge(new_id, a_id, "REVISED_BY", weight=c["score"])
        store.create_edge(new_id, b_id, "REVISED_BY", weight=c["score"])

        # 旧节点标 outdated
        a_payload["status"] = "outdated"
        store.update_payload(a_id, a_payload)
        b_payload["status"] = "outdated"
        store.update_payload(b_id, b_payload)

        merged += 1
        merged_ids.append({
            "new_id": new_id,
            "old_a": a_id,
            "old_b": b_id,
            "score": c["score"],
        })

    return merged, merged_ids


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
    will_merge, skipped_high_value, skipped_both_important, _skipped_ids = (
        _filter_candidates(candidates, max_importance))

    if dry_run:
        return {
            "candidates": candidates,
            "will_merge": will_merge,
            "skipped_high_value": skipped_high_value,
            "skipped_both_important": skipped_both_important,
            "dry_run": True,
        }

    # ---- 执行合并 ----
    merged, merged_ids = _apply_merge(store, will_merge)

    return {
        "candidates": candidates,
        "merged": merged,
        "merged_ids": merged_ids,
        "skipped_high_value": skipped_high_value,
        "skipped_both_important": skipped_both_important,
        "dry_run": False,
    }
