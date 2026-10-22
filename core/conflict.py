"""共享冲突检测：mem_ingest 写入时的相似旧记忆分级处理（三层防误标）

三层防误标（2026-08-29 domain 统一）：
  0. 类型白名单：仅 memory/task/plan 参与冲突检测（record/event/correction/
     git_commit/review 等历史留痕类型完全跳过）；
  1. 分档：score > 0.75 判定同一事实被取代；0.4 < score <= 0.75 仅话题相关；
  2. type 隔离：旧节点 type 必须与新节点一致（跨 type 绝不互标，kb_chunk 依旧排除）；
  3. domain 隔离：统一走 node_domain（domain 正式 / character_name 兼容回退）。
     双方 domain 都非 "general" 且不同则跳过（跨域绝不互标）；
     general（含未分类空域）不隔离——通用节点可被任域修订。
"""

import logging

from config import Config
from core.bitemporal import mark_superseded, windows_overlap
from core.policy import check_protected_overwrite, get_policy_mode, is_protected
from core.trivium_store import node_domain

logger = logging.getLogger(__name__)


def resolve_conflict(
    store, embedding, node_id, tx=None, db=None, new_payload=None, replaces: list[int] | None = None
) -> dict:
    """查找与本次写入相似的旧记忆并分级处理，返回 {"outdated_ids": […], "related_ids": […]}

    逻辑与 mem_ingest 内联版一致（embedding 由调用方传入，此处不再 embed）：
    - outdated_ids：被判定「同一事实被取代」的旧节点，标 outdated + 建 REVISED_BY 边；
    - related_ids：话题相关但不算取代的旧节点，只记入返回，不标 outdated、不建边。

    **取代的两个来源**（取代不再仅凭相似度自动发生）：
    1. **显式声明**（`replaces`）：调用方列出要取代的旧节点 id，只要存在且通过
       type/domain 门即无条件标 outdated——语义判断由调用方（LLM/agent）给出，
       不依赖阈值。显式意图优先于自动策略（在 CONFLICT_SKIP_TYPES 之前处理）。
    2. **自动相似判定**（仅当 `Config.CONFLICT_AUTO_SUPERSEDE=true`）：相似度 >0.75
       且过门 → 标 outdated（旧行为，作为过渡兼容开关）。默认关闭——此时 >0.4 的
       相似候选一律记入 related_ids，不再自动标 outdated，避免同主题互补内容误吞。

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

    outdated_ids: list[int] = []
    related_ids: list[int] = []
    policy_warnings: list[str] = []

    def _load_old(old_id: int) -> dict:
        """取旧节点 payload（事务模式走已打开连接，非事务走 store）。"""
        if tx is not None:
            old_node = db.get(old_id)
            return dict(old_node.payload or {}) if old_node else {}
        old_node = store.get_node(old_id)
        return old_node.get("payload", {}) if old_node else {}

    def _passes_gates(old_payload: dict) -> bool:
        """第 2/3 层门：type 隔离（同类、非 kb_chunk）+ domain 隔离（跨域不互标）。"""
        old_type = old_payload.get("type")
        # 沿用原有排除：kb_chunk 只供 kb_search 检索，不参与记忆冲突检测
        if old_type != new_type or old_type == "kb_chunk":
            return False
        old_domain = node_domain(old_payload)
        # general（含未分类空域，node_domain 兜底）不隔离，通用节点可被任何域修订
        return not (new_domain != old_domain and new_domain != "general" and old_domain != "general")

    def _supersede(old_id: int, old_payload: dict) -> None:
        """判定同一事实被取代：保留原字段，仅把 status 标记为 outdated + 建 REVISED_BY。"""
        # 只读保护门（write-guard policy，core/policy.py）：
        # 受保护节点（type=rule 或 payload.protected=True）不参与自动覆盖。
        # warn 模式：照常标 outdated + 建边，但记 policy_warnings（攒数据）；
        # enforce 模式：跳过该节点，不标 outdated、不建边。
        if is_protected(old_payload):
            warn_msg = check_protected_overwrite(old_payload)
            if warn_msg:
                policy_warnings.append(warn_msg)
                logger.warning("policy: %s (node_id=%s)", warn_msg, old_id)
            if get_policy_mode() == "enforce":
                return
        old_payload["status"] = "outdated"
        # bi-temporal（世界时间）：旧事实自新事实的 valid_at 起不再为真，
        # 并在本次系统标记时刻进入历史。非事实类型自动跳过，见 core.bitemporal。
        mark_superseded(old_payload, new_payload.get("valid_at"))
        if tx is not None:
            tx.update_payload(old_id, old_payload)
            # 新记忆 -> 旧记忆 的修订关系
            tx.link(node_id, old_id, "REVISED_BY")
        else:
            store.update_payload(old_id, old_payload)
            store.create_edge(node_id, old_id, "REVISED_BY")
        outdated_ids.append(old_id)

    # ---- 来源一：显式取代（replaces）——不论相似度，显式声明即取代 ----
    # 显式意图优先于自动策略：本段置于「类型白名单」与「CONFLICT_SKIP_TYPES」之前——
    # 白名单 / skip 只约束**自动相似判定**，不该吃掉调用方的显式指令，否则
    # record / correction / event 等非白名单类型永远无法被显式取代。
    explicit_ids = {int(r) for r in (replaces or []) if r is not None}
    explicit_ids.discard(node_id)
    for rid in sorted(explicit_ids):
        old_payload = _load_old(rid)
        if not old_payload:
            continue
        if not _passes_gates(old_payload):
            continue
        _supersede(rid, old_payload)

    # 第 0 层：类型白名单——record/event/correction/git_commit/review 等历史留痕
    # 类型跳过**自动**冲突检测（上面的显式 replaces 不受此限）。
    if new_type not in ("memory", "task", "plan"):
        return {"outdated_ids": outdated_ids, "related_ids": related_ids, "policy_warnings": policy_warnings}

    # 第 0b 层：按 type 显式豁免（CONFLICT_SKIP_TYPES，issue #50）——只约束自动相似判定。
    # task 这类「累积关系」不是「同一事实被取代」：连续写入相似措辞的独立任务时，
    # 旧任务会被误标 outdated 并从默认检索中消失。默认空集 = 行为完全不变。
    if new_type in Config.CONFLICT_SKIP_TYPES:
        return {"outdated_ids": outdated_ids, "related_ids": related_ids, "policy_warnings": policy_warnings}

    if tx is not None:
        similar = _similar_hits(db, embedding)
    elif hasattr(store, "embed_text"):
        # TriviumStore 形实现（自带 embed_text）：保持原向量直查——嵌入由
        # mem_ingest 调用方传入复用，零额外嵌入开销（历史行为，字节不动）。
        similar = store.search_similar(embedding, top_k=3, expand_depth=0, apply_decay=False)
    else:
        # protocols.Store 实现（LocalStore / RemoteStore）：search_similar 首参
        # 是 query **文本**、各实现自行 embed。此前把向量当文本传，会让
        # LocalStore 把向量再 embed、RemoteStore 给服务端发数组——协议外的
        # 静默破坏。按协议传文本：query 取新节点 content（与 embedding 同源，
        # embed 后同向量，结果排序不变）。
        query = str((new_payload or {}).get("content") or "")
        similar = store.search_similar(query, top_k=3, expand_depth=0, apply_decay=False)

    auto_supersede = bool(getattr(Config, "CONFLICT_AUTO_SUPERSEDE", False))
    for r in similar:
        old_id = r.get("id")
        score = float(r.get("score", 0.0))
        # 第 1 层：score <= 0.4 视为无关，直接跳过（不参与任何标记）
        if old_id is None or old_id == node_id or score <= 0.4:
            continue
        # 已由显式取代处理过的节点不再重复判定
        if old_id in explicit_ids:
            continue
        old_payload = _load_old(old_id)
        if not old_payload:
            continue
        if not _passes_gates(old_payload):
            continue
        # 自动取代关闭（默认）：相似只提示相关，不标 outdated
        if not auto_supersede:
            related_ids.append(old_id)
            continue
        # 自动取代开启：仅此路径保留「相似度 >0.75 = 同一事实被取代」的旧行为
        if score > 0.75:
            # 时间窗判定（bi-temporal 三期，core/bitemporal.windows_overlap）：
            # 相似但不重叠的两条事实是「不同时期的真相」（如「十年前住北京」≠
            # 「现在住上海」），不判为「同一事实被取代」→ 不标 outdated，归入
            # related_ids。默认关闭（CONFLICT_TIME_WINDOW=false = 行为不变）。
            if Config.CONFLICT_TIME_WINDOW and not windows_overlap(new_payload, old_payload):
                related_ids.append(old_id)
                continue
            _supersede(old_id, old_payload)
        else:
            # 仅话题相关：只记入 related_ids，不标 outdated、不建边
            related_ids.append(old_id)
    return {"outdated_ids": outdated_ids, "related_ids": related_ids, "policy_warnings": policy_warnings}


def _similar_hits(db, embedding: list[float]) -> list[dict]:
    """事务内查找相似旧记忆（对已打开连接 db 用原生 search：只返回已提交节点）。

    复用 store.search_similar 的候选语义（不衰减、不扩散）：召回 top_k=max(9,10)=10
    个候选，再截取前 3 个返回。但使用已打开的事务连接，避免事务期间二次
    _acquire 触发 "Database locked"。
    """
    try:
        hits = db.search(embedding, top_k=max(3 * 3, 10), min_score=0.0, expand_depth=0)
        return [{"id": hit.id, "score": float(hit.score), "payload": hit.payload} for hit in (hits or [])][:3]
    except Exception as e:  # noqa: BLE001 —— 事务内相似检索失败降级空结果不阻断冲突检测
        logger.warning(f"事务内相似检索失败，返回空结果: {e}")
        return []
