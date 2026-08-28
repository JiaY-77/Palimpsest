# -*- coding: utf-8 -*-
"""共享冲突检测：mem_ingest 写入时的相似旧记忆标 outdated + 建 REVISED_BY 链"""


def resolve_conflict(store, embedding, node_id) -> list[int]:
    """查找与本次写入相似的旧记忆：标记 outdated、建立 REVISED_BY 边，返回旧 id 列表。

    逻辑与 mem_ingest 内联版一致（embedding 由调用方传入，此处不再 embed）：
    score > 0.4 且非自身、payload 非空、非 kb_chunk 的旧节点标 outdated。
    """
    outdated_ids = []
    similar = store.search_similar(embedding, top_k=3, expand_depth=0, apply_decay=False)
    for r in similar:
        old_id = r.get("id")
        score = float(r.get("score", 0.0))
        if old_id is None or old_id == node_id or score <= 0.4:
            continue
        old_node = store.get_node(old_id)
        old_payload = old_node.get("payload", {}) if old_node else {}
        if not old_payload:
            continue
        # 排除知识库块：kb_chunk 只供 kb_search 检索，不参与记忆冲突检测（不标 outdated、不建 REVISED_BY 边）
        if old_payload.get("type") == "kb_chunk":
            continue
        # 保留原字段，仅把 status 标记为 outdated
        old_payload["status"] = "outdated"
        store.update_payload(old_id, old_payload)
        # 新记忆 -> 旧记忆 的修订关系
        store.create_edge(node_id, old_id, "REVISED_BY")
        outdated_ids.append(old_id)
    return outdated_ids