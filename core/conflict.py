# -*- coding: utf-8 -*-
"""共享冲突检测：mem_ingest 写入时的相似旧记忆分级处理（三层防误标）

三层防误标（v1.2，2026-08-29 domain 统一）：
  0. 类型白名单：仅 memory/task/plan 参与冲突检测（record/event/correction/
     git_commit/review 等历史留痕类型完全跳过）；
  1. 分档：score > 0.75 判定同一事实被取代；0.4 < score <= 0.75 仅话题相关；
  2. type 隔离：旧节点 type 必须与新节点一致（跨 type 绝不互标，kb_chunk 依旧排除）；
  3. domain 隔离：统一走 node_domain（domain 正式 / character_name 兼容回退）。
     双方 domain 都非 "general" 且不同则跳过（跨域绝不互标）；
     general（含未分类空域）不隔离——通用节点可被任域修订。
"""

import logging

from core.trivium_store import node_domain

logger = logging.getLogger(__name__)


def resolve_conflict(store, embedding, node_id, tx=None, db=None,
                     new_payload=None) -> dict:
    """查找与本次写入相似的旧记忆并分级处理，返回 {"outdated_ids": […], "related_ids": […]}

    逻辑与 mem_ingest 内联版一致（embedding 由调用方传入，此处不再 embed）：
    - outdated_ids：score > 0.75 且通过 type/domain 门的旧节点，标 outdated + 建 REVISED_BY 边；
    - related_ids：0.4 < score <= 0.75 且通过 type/domain 门的旧节点，只记入返回，
      不标 outdated、不建边。

    事务模式（tx 非空）：供 mem_ingest 事务化链路使用，写操作（标脏/建边）落在
    传入的事务句柄 tx 上（tx.update_payload / tx.link），读操作（查找相似、取旧
    节点 payload）走传入的同一已打开连接 db（db.search / db.get）——不二次
    _acquire（事务期间重开连接会 "Database locked"，正是此前的移植半写风险点）。
    new_payload：事务模式下新节点尚未提交、db.get 不可见，故由调用方把新节点
    payload 传入以取新节点的 type/domain；非事务模式忽略（仍读 store.get_node）。

    非事务模式（tx 为 None）：保持原有行为，走 store.search_similar /
    store.get_node / store.update_payload / store.create_edge（各自独立 _acquire）。
    """
    if new_payload is not None:
        new_type = new_payload.get("type")
        new_domain = node_domain(new_payload)
    else:
        new_node = store.get_node(node_id)
        new_payload = new_node.get("payload", {}) if new_node else {}
        new_type = new_payload.get("type")
        new_domain = node_domain(new_payload)

    outdated_ids = []
    related_ids = []
    # 第 0 层：类型白名单——record/event/correction/git_commit/review 等
    # 历史留痕类型完全跳过冲突检测
    if new_type not in ("memory", "task", "plan"):
        return {"outdated_ids": outdated_ids, "related_ids": related_ids}

    if tx is not None:
        similar = _similar_hits(db, embedding)
    else:
        similar = store.search_similar(embedding, top_k=3, expand_depth=0,
                                       apply_decay=False)
    for r in similar:
        old_id = r.get("id")
        score = float(r.get("score", 0.0))
        # 第 1 层：score <= 0.4 视为无关，直接跳过（不参与任何标记）
        if old_id is None or old_id == node_id or score <= 0.4:
            continue
        if tx is not None:
            old_node = db.get(old_id)
            old_payload = dict(old_node.payload or {}) if old_node else {}
        else:
            old_node = store.get_node(old_id)
            old_payload = old_node.get("payload", {}) if old_node else {}
        if not old_payload:
            continue
        # 第 2 层：type 隔离——跨 type 绝不互标
        old_type = old_payload.get("type")
        # 沿用原有排除：kb_chunk 只供 kb_search 检索，不参与记忆冲突检测
        if old_type != new_type or old_type == "kb_chunk":
            continue
        # 第 3 层：domain 隔离——双方都非 general 且不同则跨域绝不互标；
        # general（含未分类空域，node_domain 兜底）不隔离，通用节点可被任何域修订
        old_domain = node_domain(old_payload)
        if (new_domain != old_domain
                and new_domain != "general" and old_domain != "general"):
            continue
        # 过完所有门后分档：
        if score > 0.75:
            # 判定同一事实被取代：保留原字段，仅把 status 标记为 outdated
            old_payload["status"] = "outdated"
            if tx is not None:
                tx.update_payload(old_id, old_payload)
                # 新记忆 -> 旧记忆 的修订关系
                tx.link(node_id, old_id, "REVISED_BY")
            else:
                store.update_payload(old_id, old_payload)
                store.create_edge(node_id, old_id, "REVISED_BY")
            outdated_ids.append(old_id)
        else:
            # 仅话题相关：只记入 related_ids，不标 outdated、不建边
            related_ids.append(old_id)
    return {"outdated_ids": outdated_ids, "related_ids": related_ids}


def _similar_hits(db, embedding: list[float]) -> list[dict]:
    """事务内查找相似旧记忆（对已打开连接 db 用原生 search：只返回已提交节点）。

    复用 store.search_similar 的候选语义（top_k=3、不衰减、不扩散），但使用
    已打开的事务连接，避免事务期间二次 _acquire 触发 "Database locked"。
    """
    try:
        hits = db.search(embedding, top_k=max(3 * 3, 10), min_score=0.0,
                         expand_depth=0)
        return [
            {"id": hit.id, "score": float(hit.score), "payload": hit.payload}
            for hit in (hits or [])
        ][:3]
    except Exception as e:
        logger.warning(f"事务内相似检索失败，返回空结果: {e}")
        return []