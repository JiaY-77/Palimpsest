# -*- coding: utf-8 -*-
"""共享冲突检测：mem_ingest 写入时的相似旧记忆分级处理（三层防误标）

三层防误标（v1.2）：
  0. 类型白名单：仅 memory/task/plan 参与冲突检测（record/event/correction/
     git_commit/review 等历史留痕类型完全跳过）；
  1. 分档：score > 0.75 判定同一事实被取代；0.4 < score <= 0.75 仅话题相关；
  2. type 隔离：旧节点 type 必须与新节点一致（跨 type 绝不互标，kb_chunk 依旧排除）；
  3. domain 隔离：新旧 character_name 都非空且不同则跳过（跨域绝不互标），
     有一方为空则不隔离（通用节点可被任域修订）。
"""


def resolve_conflict(store, embedding, node_id) -> dict:
    """查找与本次写入相似的旧记忆并分级处理，返回 {"outdated_ids": […], "related_ids": […]}

    逻辑与 mem_ingest 内联版一致（embedding 由调用方传入，此处不再 embed）：
    - outdated_ids：score > 0.75 且通过 type/domain 门的旧节点，标 outdated + 建 REVISED_BY 边；
    - related_ids：0.4 < score <= 0.75 且通过 type/domain 门的旧节点，只记入返回，
      不标 outdated、不建边。
    """
    new_node = store.get_node(node_id)
    new_payload = new_node.get("payload", {}) if new_node else {}
    new_type = new_payload.get("type")
    new_domain = new_payload.get("character_name")

    outdated_ids = []
    related_ids = []
    # 第 0 层：类型白名单——record/event/correction/git_commit/review 等
    # 历史留痕类型完全跳过冲突检测
    if new_type not in ("memory", "task", "plan"):
        return {"outdated_ids": outdated_ids, "related_ids": related_ids}

    similar = store.search_similar(embedding, top_k=3, expand_depth=0, apply_decay=False)
    for r in similar:
        old_id = r.get("id")
        score = float(r.get("score", 0.0))
        # 第 1 层：score <= 0.4 视为无关，直接跳过（不参与任何标记）
        if old_id is None or old_id == node_id or score <= 0.4:
            continue
        old_node = store.get_node(old_id)
        old_payload = old_node.get("payload", {}) if old_node else {}
        if not old_payload:
            continue
        # 第 2 层：type 隔离——跨 type 绝不互标
        old_type = old_payload.get("type")
        # 沿用原有排除：kb_chunk 只供 kb_search 检索，不参与记忆冲突检测
        if old_type != new_type or old_type == "kb_chunk":
            continue
        # 第 3 层：domain 隔离——双方都非空且不同则跨域绝不互标；
        # 有一方为空则不隔离（通用节点可以被任何域修订）
        old_domain = old_payload.get("character_name")
        if new_domain and old_domain and new_domain != old_domain:
            continue
        # 过完所有门后分档：
        if score > 0.75:
            # 判定同一事实被取代：保留原字段，仅把 status 标记为 outdated
            old_payload["status"] = "outdated"
            store.update_payload(old_id, old_payload)
            # 新记忆 -> 旧记忆 的修订关系
            store.create_edge(node_id, old_id, "REVISED_BY")
            outdated_ids.append(old_id)
        else:
            # 仅话题相关：只记入 related_ids，不标 outdated、不建边
            related_ids.append(old_id)
    return {"outdated_ids": outdated_ids, "related_ids": related_ids}