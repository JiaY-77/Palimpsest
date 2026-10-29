"""
mcp_tools.memory —— 记忆读写与查询工具
=====================================
mem_retrieve / mem_get_full / mem_ingest / mem_recent / mem_review /
mem_version_history / mem_search（及核心 _mem_search_impl）/ mem_hybrid_search。
"""

import contextlib
import logging
import re
import threading
import time
from typing import Any

from config import Config
from core import tdb_text_index as _tdb_text
from core.bitemporal import freshness_decay, is_valid_at, stamp_new_fact, time_fields
from core.capsule_guard import validate_capsule_fields
from core.conflict import resolve_conflict
from core.db_health import check_db_health, health_hint
from core.fts_index import index_node, search_fts_status
from core.judgment_metrics import record_ingest
from core.memoryhub import apply_import, read_pack, summarize_pack
from core.meta_guard import validate_meta
from core.policy import check_ingest
from core.search import is_exact_identifier_query
from core.secret_scan import SecretScanError
from core.strategy import THOUGHT_TYPE, decide_import, detect_thought_origin, normalize_skeleton_kind
from core.task_state import HANDOFF_KIND, _handoff_title, _is_handoff, _iso_from_epoch
from core.trivium_store import EmbeddingInputTooLongError, EmbeddingUnavailableError, domain_in_block, node_domain
from core.utils import _to_float
from mcp_tools._common import _shorten, _to_json, mcp, store
from mcp_tools.graph import _collect_neighbors

logger = logging.getLogger(__name__)

# mem_ingest 并发 id 分配锁：多请求同时算 next_id 可能得到相同 id，
# 加锁保护「id 分配 + 插入」的临界区（只锁分配段，不锁整个 ingest 流程）
_INGEST_ID_LOCK = threading.Lock()

# ---- 记忆分层（tier）：检索侧视图，不改存储、不迁数据 ----
# 定义已移到 config.py（可经环境变量 TIER_FACTS / TIER_LOGS / DEFAULT_TIER 调整），
# 此处仅从配置读取，避免同一份清单在两处漂移。
# facts = 事实层（默认检索与注入池）；logs = 日志层（从默认池摘出）；
# ""（空串）= 不过滤，回到改动前的全量行为（显式历史通道）。
# kb_chunk / novel_chunk 不入本体系，走既有 scope 隔离；
# 未登记的 type 一律归 facts（保守兜底，避免静默丢结果）。
# 常量在模块级定义（mem_review 等默认参数在函数定义期求值，须先于此可用）。
TIER_FACTS = Config.TIER_FACTS
TIER_LOGS = Config.TIER_LOGS
DEFAULT_TIER = Config.DEFAULT_TIER


@mcp.tool()
def mem_retrieve(
    query: str, domain: str = "", top_k: int = 5, include_outdated: bool = False, as_of: float | None = None
) -> str:
    """
    语义检索记忆：返回 150 字摘要 + meta（绝不返回全文，省 token 的关键设计）。
    全文请用 mem_get_full 按 id 单独取。
    v4.0 outdated 语义：默认只回当前有效节点（status != "outdated"）；
    include_outdated=True 时返回全部（历史可追溯通道）。
    as_of 非空时按 bi-temporal 时间窗回看（valid_at <= as_of 且未在 as_of 前
    invalid_at），以时间窗为准、不看当前 status；为空 = 原行为不变。
    """
    emb = store.embed_text(query)
    # v1.1 拉宽召回：与 mem_search 一致，top_k*3 召回再过滤，避免 kb_chunk 挤占名额导致记忆条数凑不满
    # as_of 历史视图：底层拉全量（含 outdated），时间窗过滤决定去留
    results = store.search_similar(
        emb,
        top_k=max(top_k * 3, 30),
        expand_depth=getattr(Config, "RETRIEVAL_EXPAND_DEPTH", 0),
        include_outdated=include_outdated or (as_of is not None),
    )
    items = []
    for r in results:
        payload = r.get("payload", {}) or {}
        # v4.0 outdated 语义：默认跳过被取代的旧版本，include_outdated=True 保留；
        # as_of 非空时以时间窗为准（不看 status）——判据集中在 _visible()，多路共用。
        if not _visible(payload, include_outdated=include_outdated, as_of=as_of):
            continue
        # domain 过滤：只保留指定角色/域的记忆（domain 正式字段，character_name 兼容镜像已退役）
        if domain and node_domain(payload) != (domain or "").strip().lower():
            continue
        # 排除知识库块：kb_chunk 只供 kb_search / mem_search(scope=kb) 检索，不混入记忆
        if payload.get("type") == "kb_chunk":
            continue
        items.append(
            {
                "id": r.get("id"),
                "score": round(_to_float(r.get("score"), 0.0), 4),
                "summary": _shorten(payload.get("content", ""), 150),
                "meta": {
                    "type": payload.get("type", ""),
                    "importance": payload.get("importance", 0.5),
                    "status": payload.get("status", ""),
                    "domain": node_domain(payload),
                },
            }
        )
        if len(items) >= top_k:
            break
    if not items:
        return _to_json({"results": []})
    return _to_json({"results": items})


@mcp.tool()
def mem_get_full(node_id: int) -> str:
    """按节点 id 取完整记忆（含全部 payload 字段；不含向量，避免返回 1024 维浮点数组）"""
    node = store.get_node(node_id)
    if not node:
        return _to_json({"found": False, "node_id": node_id})
    return _to_json(
        {
            "found": True,
            "id": node.get("id"),
            "payload": node.get("payload", {}),
            "num_edges": node.get("num_edges", 0),
        }
    )


def _find_linked_kb_ids(emb: list[float]) -> list[int]:
    """v1.1 知识关联检测：找 score > 0.35 的 kb_chunk 节点 id（写入 payload.linked_from）。

    只读查询，必须在事务外做（事务内不重开连接）。

    RP 专用实例（config.RP_MODE=1）整体关掉：RP 独立库里没有 kb_chunk，
    检测与 REFERENCES 挂边纯属浪费（开关在调用时读取，默认关闭完全不变）。
    """
    if Config.RP_MODE:
        return []
    linked: list[int] = []
    similar = store.search_similar(emb, top_k=3, expand_depth=0, apply_decay=False)
    for r in similar:
        r_payload = r.get("payload", {}) or {}
        if r_payload.get("type") == "kb_chunk" and _to_float(r.get("score"), 0.0) > 0.35:
            rid = r.get("id")
            if rid is not None:
                linked.append(rid)
    return linked


def _insert_with_conflict(
    store, node_data: dict, emb: list[float], replaces: list[int] | None = None, embedding_audit: dict | None = None
) -> tuple[int, dict]:
    """事务化写入 + 冲突检测标脏，返回 (node_id, conflict)。

    「insert（含 created_at）+ resolve_conflict 标脏」整体包进单事务：任一步抛异常
    自动 rollback，不会出现「新节点已写入、旧记忆未标 outdated」的半状态。
    id 分配走模块级锁保护临界区：同一进程内多线程并发写入时，「读 max(id)」
    与「按该 id 写入」必须原子，否则两个线程会挑到同一个 next_id。跨进程不必
    靠这把锁 —— triviumdb 以独占写模式打开库文件，第二个写进程在构造时即抛
    `Database locked`，走不到分配 ID 这一步（详见 core/trivium_store.py）。

    replaces：显式取代的旧节点 id 列表（透传给 resolve_conflict）——占位符，
    细节见 core.conflict.resolve_conflict。

    embedding_audit（issue #232）：调用方经 ``embed_text_with_audit`` 得到的
    嵌入审计 dict，透传给 insert_node_tx 沉淀进 payload["embedding"]。

    返回前显式关闭连接释放库锁：后续 FTS 同步 / 读回节点都经 store._acquire()
    重开，若不关闭会触发 "Database locked"（同库双连接）。
    """
    db = None
    try:
        db = store._acquire()
        with _INGEST_ID_LOCK:
            existing = db.all_node_ids()
            next_id = (max(existing) + 1) if existing else 1
            with db.transaction() as tx:
                # insert_node_tx 已把 created_at 透传进 payload（事务模式下新节点不可
                # 读回，避免 tx.update_payload 整包覆盖）；此处仅做冲突检测标脏。
                node_id = store.insert_node_tx(tx, node_data, emb, next_id=next_id, embedding_audit=embedding_audit)
                conflict = resolve_conflict(store, emb, node_id, tx=tx, db=db, new_payload=node_data, replaces=replaces)
        with contextlib.suppress(Exception):
            db.close()
        db = None
        return node_id, conflict
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()


def _link_kb_references(store, node_id: int, kb_ids: list[int]) -> None:
    """为「记忆 → 知识切片」建 REFERENCES 图边（best-effort，同三元组幂等）。

    与 scripts/build_structural_edges.py 的「源 7 知识关联」同源：写入时即时建边，
    历史数据回填 / 重建后修复由建边器负责，两条路径都幂等，不会重复建边。
    此前 `linked_from` 只落在 payload、**从不建边**，导致图谱上「记忆 → 知识切片」
    完全不可达（知识切片不会被记忆引用）。
    """
    for kid in kb_ids:
        try:
            store.create_edge(node_id, kid, "REFERENCES")
        except Exception as e:  # noqa: BLE001 —— 建边失败仅告警，主写入已提交
            logger.warning("知识关联建边失败 %s->%s: %s", node_id, kid, e)


def _post_ingest_side_effects(
    store, node_id: int, content: str, now: float, linked_kb_ids: list[int] | None = None
) -> tuple[list, str]:
    """事务提交后的收尾，返回 (secret_hint, domain)。

    - FTS 全文索引同步（混合检索依赖；失败仅 warning，主写入已提交，可 fts-rebuild 兜底）
    - 知识关联建边：把 linked_from 的切片 id 落成 REFERENCES 图边（同样 best-effort）
    - 读回已提交节点取 secret_hint（弱规则命中标记），并补写缺失的 created_at；
      ``type=task`` 再补写缺失的 ``last_touched_at``（= created_at 同一刻，issue #128 C1）
    """
    try:
        index_node(node_id, content)
    except Exception as e:  # noqa: BLE001 —— 写入后 FTS 索引失败仅告警主链已提交
        logger.warning("FTS 索引同步失败 node=%s: %s", node_id, e)
    # TDB 文本通道（默认关闭零成本；失败 warning 不阻断，可 rebuild 兜底）
    try:
        _tdb_text.sync_node(node_id, content)
    except Exception as e:  # noqa: BLE001 —— TDB 同步失败仅告警主链已提交
        logger.warning("TDB 文本索引同步失败 node=%s: %s", node_id, e)

    _link_kb_references(store, node_id, linked_kb_ids or [])

    node = store.get_node(node_id)
    payload = node.get("payload", {}) if node else {}
    secret_hint = payload.get("secret_hint", [])
    changed = False
    if payload.get("created_at") is None:
        payload["created_at"] = now
        changed = True
    # issue #128 C1：type=task 的 last_touched_at 初始化为 created_at 同一刻
    # （ISO，与 apply_task_patch._now_iso 同格式）。此前只在状态变更时才写，
    # 从未更新过的任务恒为 null，列表新鲜度判据失去回退依据。只在缺失时补——
    # 已有值（状态机写的）绝不覆盖。
    if payload.get("type") == "task" and not payload.get("last_touched_at"):
        created = payload.get("created_at")
        if isinstance(created, (int, float)):
            payload["last_touched_at"] = _iso_from_epoch(created)
            changed = True
    if changed:
        store.update_payload(node_id, payload)
    return secret_hint, node_domain(payload)


def _build_ingest_result(
    node_id,
    domain_out: str,
    conflict: dict,
    linked_kb_ids: list[int],
    secret_hint: list,
    policy_warnings: list | None = None,
) -> str:
    """组装 mem_ingest 的返回 JSON（含 outdated 修订链提示 + 策略告警）。"""
    outdated_ids = conflict["outdated_ids"]
    related_ids = conflict["related_ids"]
    # 冲突检测阶段（core.conflict）产生的策略告警 + 入口阶段的告警合并
    all_warnings = list(conflict.get("policy_warnings", [])) + list(policy_warnings or [])

    suggestion = ""
    if outdated_ids:
        ids = ", ".join(str(i) for i in outdated_ids)
        suggestion = f"旧记忆 id={ids} 已标记 outdated（REVISED_BY 链），若涉及固定记忆（MEMORY.md）请同步更新"
    payload = {
        "stored": True,
        "node_id": node_id,
        "domain": domain_out,
        "conflict_found": bool(outdated_ids),
        "outdated_ids": outdated_ids,
        "related_ids": related_ids,
        "linked_kb_ids": linked_kb_ids,
        "secret_hint": secret_hint,
        "suggestion": suggestion,
    }
    # 仅在有告警时加字段，避免污染既有严格断言
    if all_warnings:
        payload["policy_warnings"] = all_warnings
    return _to_json(payload)


@mcp.tool()
def mem_ingest(
    content: str,
    type: str = "memory",
    importance: float = 0.5,
    domain: str = "",
    source: str = "",
    replaces: str = "",
    skeleton_kind: str = "",
    turn_index: int | None = None,
    meta: dict[str, str] | None = None,
    sections: dict[str, str] | None = None,
    pending: list[str] | None = None,
) -> str:
    """
    写入新记忆。

    接续点自动打标：``type=task`` 且 content 首行以「接续点」标题词开头
    （``core.task_state._is_handoff`` 判据）→ 自动写 ``kind=handoff``（幂等），
    并在 ``title`` 为空时用 ``core.task_state._handoff_title`` 自动补一行摘要
    （已有非空值不覆盖），跨会话交接记录由此默认从活跃任务列表排除、
    ``tasks_active(include_handoff=true)`` 可显式取回，且交接列表里读得出是哪条。

    ★ 写入前请先判定（判定归调用方，本工具不代判、不额外接 LLM）：
      1) 先 ``mem_search`` 查同主题的现有记忆（``scope="memory"``，top_k=3~5）；
      2) 按结果分三种情况：
         - **全新事实** → 直接写入（不传 ``replaces``）
         - **旧事实被新值更新/取代** → 写入时传 ``replaces=<旧节点 id>``
         - **同主题的补充 / 不同角度** → 直接写入（不传 ``replaces``；相似内容会自动
           记入 ``related_ids`` 作为话题关联提示）
      3) **拿不准就按「全新事实」写入** —— 宁可多留一条，也不要误标 outdated
         （误标会让旧事实从默认检索中消失；多留只是候选多一条）

    **为什么判定归调用方**：判断「值不值得记 / 是否取代旧事实」需要完整对话上下文，
    而调用方（agent 自身、自带模型）恰好拥有它 —— 记忆层只提供机制（版本链 / 关联
    提示 / 冲突治理），不代判。

    取代机制（默认不自动取代）：

      - ``replaces``（可选）：逗号分隔的旧节点 id（兼容中文逗号）。列出的节点若存在
        且通过 type/domain 门，即无条件标 outdated 并建立
        ``新记忆 --REVISED_BY--> 旧记忆`` 的修订链。**语义判断由调用方给出**，不依赖阈值。
      - 未声明 replaces 时：与库中相似度 >0.4 的旧记忆只记入 ``related_ids``（话题相关
        提示），**不标 outdated**——「相似」不等于「同一事实被取代」，避免同主题互补
        内容被误吞。
      - 旧「相似度 >0.75 自动取代」行为仅在 ``CONFLICT_AUTO_SUPERSEDE=true`` 时启用
        （默认关闭，过渡兼容开关）。
    自定义元数据打标（``meta``，可选）：键值均为字符串的字典，写入节点
    payload 的 ``meta`` 子字典（如 ``{"chat_id": "..."}``），供外部宿主按
    「域名 + meta 键值」精确定位并批量删除（REST ``/mem/purge-by-meta``）。
    结构约束（超出即拒绝写入）：≤8 个键，键名限 ``^[a-zA-Z0-9_]{1,32}$``，
    值必须是字符串且 ≤256 字符。不传（None）或空字典时 payload **不含**
    ``meta`` 键，payload 形状与旧行为完全一致。
    v1.1：新增知识关联检测——score > 0.35 的 kb_chunk 记入 payload.linked_from，
    返回 linked_kb_ids 便于追溯关联的知识库块。

    手写胶囊结构字段（可选 ``sections`` / ``pending``）：与自动胶囊
    （``core.strategy.compose_session_capsule`` 产出，POST /lifecycle/session-end
    写入）形状一致——``sections`` 为四节文本字典（键限 thought / outcome /
    next / pitfalls，值为字符串），``pending`` 为待补节名列表。未传
    ``pending`` 而传了 ``sections`` 时自动算（缺失或值为空白的节，固定
    顺序）；显式传入原样使用、不做自动推断覆盖。校验见 ``core.capsule_guard``
    （与 REST ``/mem/ingest`` 同一份实现）：键 / 元素出白名单、值非字符串、
    pending 列了 sections 中非空的节，一律拒绝写入（stored:false，不落库）。
    两个键均不传（None）时 payload 不含这两个键，形状与旧行为完全一致。

    思维底片来源标记（``type="thought"`` 专属）：``skeleton_kind`` 为给侧上报的
    **事实字段**（本轮注入的思考骨架类型，``""`` / code / research / default，
    白名单外归空串）；``thought_origin`` 为本工具按 content 开头形态自动判定的
    **启发式字段**（skeleton / native / unknown，判据与诚实边界见
    ``core.strategy.detect_thought_origin``）。任何通道写 thought 节点都自动带
    ``thought_origin``，新节点无需回填。
    """
    now = time.time()
    # ---- 入口校验（write-guard policy）：空内容 / 超长 / 分级上限 ----
    # 契约见 core/policy.py；空内容与超长无论何种模式都硬拒（沿用原语义），
    # 分级上限在 warn 模式只记 warning 不拒。
    policy_res = check_ingest(content, type, domain)
    if not policy_res["ok"]:
        return _to_json({"stored": False, "node_id": None, "error": policy_res["error"]})
    policy_warnings: list[str] = list(policy_res["warnings"])
    # issue #232：写入路径的向量同根生成，但要把嵌入审计一起拿到——
    # 沉淀进 payload["embedding"]，长文本不再静默只嵌前缀而不留痕。
    emb, embed_audit = store.embed_text_with_audit(content)

    # 显式取代声明：解析逗号分隔的 id 串（兼容中文逗号 / 空白），非法片段忽略。
    replace_ids: list[int] = []
    for token in (replaces or "").replace("，", ",").split(","):
        token = token.strip()
        if token.isdigit():
            replace_ids.append(int(token))

    # ---- v1.1 知识关联检测：只读查询，留在事务外做 ----
    linked_kb_ids = _find_linked_kb_ids(emb)

    node_data = {
        "type": type,
        "content": content,
        "importance": importance,
        "domain": domain,
        "source": source,
        "created_at": now,
        "linked_from": linked_kb_ids,
    }
    # 自定义元数据打标（可选 meta）：结构校验见 core.meta_guard——超出键数 /
    # 键名 / 值长度的约束即拒绝写入（stored:False）。不传或空字典不写 meta
    # 键，payload 形状与旧行为一致；合法时随节点同事务落库（避免「先写后补」
    # 的两步非原子写入留下无 meta 半状态）。
    if meta:
        meta_ok, meta_err = validate_meta(meta)
        if not meta_ok:
            return _to_json({"stored": False, "error": meta_err})
        node_data["meta"] = dict(meta)
    # 手写胶囊结构字段（可选 sections / pending）：校验见 core.capsule_guard——
    # 与自动胶囊（compose_session_capsule）形状同源；键 / 元素出白名单、值
    # 非字符串、pending 列了非空节，一律拒绝写入（stored:False，不落库，
    # 走下方事务异常前的既有拒绝路径）。不传（None）或空字典时不写对应键，
    # payload 形状与旧行为一致；pending 未传而 sections 非空时由校验函数
    # 自动算（缺失或空白节，固定顺序），显式传入原样落库。
    if sections is not None or pending is not None:
        cap_ok, cap_err, pending_resolved = validate_capsule_fields(sections, pending)
        if not cap_ok:
            return _to_json({"stored": False, "error": cap_err})
        if sections:
            node_data["sections"] = dict(sections)
        if pending_resolved is not None:
            node_data["pending"] = list(pending_resolved)
    # 会话接续点写入层打标（幂等）：type=task 且 content 首行以「接续点」标题词
    # 开头 → 自动写 kind=handoff。判定复用 core.task_state._is_handoff 的首行判据
    # （PR #112 收紧过的「标题词开头」，与查询 / 回填同源，不另立识别规则）——
    # 此前 kind 只有存量回填会打，回填之后纯 ingest 进来的接续点一律缺 kind，
    # include_handoff=True 也捞不回（生产 2363/2393 实测根因之一）。已是接续点
    # 的写入不重复打、不报错。
    # title 同步补齐（幂等、不覆盖）：title 此前只有回填会补，新写入的接续点在
    # 交接列表里 title 为空、读不出是哪条。已有非空值不覆盖（尊重人工与既有值，
    # 与回填「title 非空即就绪」判据一致）；为空才补，提取复用 _handoff_title
    # （PR #112 验收过的逻辑，不改动）。
    if type == "task" and _is_handoff({"content": content}):
        node_data["kind"] = HANDOFF_KIND
        if not (node_data.get("title") or "").strip():
            # created_at 一并带入（issue #227）：hash-only 首行（补产胶囊形态）
            # 退化出的可读 stub 需要日期段；其余 title 提取路径不读 created_at，
            # 对正常手写接续点 / session-end 胶囊的既有 title 零行为变化。
            node_data["title"] = _handoff_title({"content": content, "created_at": now})
    # 思维底片来源标记（type=thought 专属，见 mem_ingest docstring）：skeleton_kind
    # 是给侧上报的事实字段（白名单外归空串，脏值不进库）；thought_origin 是按
    # content 形态判定的启发式字段——落库路径统一打标，任何通道写 thought 节点
    # 都自动带值，新节点无需回填。
    if type == THOUGHT_TYPE:
        node_data["skeleton_kind"] = normalize_skeleton_kind(skeleton_kind)
        node_data["thought_origin"] = detect_thought_origin(content)
        # issue #125：lifecycle 通道写入时落 turn_index（宿主上报的轮次序号），
        # 作为同轮重复提交的持久判重依据；不传（None）不落。需与 source
        # （lifecycle:post-turn[:session_id]）配合判重——见 main.lifecycle_post_turn。
        if turn_index is not None:
            node_data["turn_index"] = int(turn_index)
    # bi-temporal：新事实补 valid_at（世界时间 = 事实开始为真之时；缺省=写入时刻）。
    # 非事实类型（kb_chunk / record 等）自动跳过，见 core.bitemporal。
    stamp_new_fact(node_data, now)

    # ---- 事务化写入链路（2026-08-29 止血加事务）----
    # 「insert（含 created_at）+ resolve_conflict 标脏」整体包进单事务，任一步
    # 抛异常 → 自动 rollback → 不留半状态；连接管理细节见 _insert_with_conflict。
    node_id = None
    conflict = {"outdated_ids": [], "related_ids": []}
    secret_hint: list = []
    domain_out = ""
    try:
        node_id, conflict = _insert_with_conflict(store, node_data, emb, replace_ids, embedding_audit=embed_audit)
        # 事务已提交，连接已在 helper 内释放（避免后续重开撞 Database locked）
        secret_hint, domain_out = _post_ingest_side_effects(store, node_id, content, now, linked_kb_ids)
    except SecretScanError as e:
        # 强规则拒绝：事务回滚（未写任何节点），返回 stored:False
        return _to_json({"stored": False, "error": str(e), "rules": e.rules})
    except Exception as e:  # noqa: BLE001 —— 事务异常回滚返回存储失败不留半状态
        # 事务内其他异常：整条写入链路回滚，无半状态；返回 stored:False + hint。
        # 写入失败可能已污染文件组（残留 .tmp/.wal → generation 校验失败 → 库
        # 从可读写退化为读不动），故失败后立即做一次健康探测并明确回报，避免
        # 后续重试在坏库上继续加重污染（见 issue #32）。
        logger.error("mem_ingest 写入失败（事务已回滚）: %s", e)
        health = check_db_health()
        payload = {
            "stored": False,
            "error": "写入事务失败，已回滚，无残留节点",
            "hint": str(e),
            "db_healthy": health.get("ok"),
        }
        if not health.get("ok"):
            payload["db_health_error"] = health.get("error", "")
            payload["recovery_hint"] = health_hint(health)
            logger.error("写入失败后健康探测不通过：%s", health.get("error"))
        return _to_json(payload)

    result = _build_ingest_result(node_id, domain_out, conflict, linked_kb_ids, secret_hint, policy_warnings)

    # 写入判定观测（write_judgment，core/judgment_metrics.py）：仅成功路径记录；
    # 函数内部静默降级，观测写失败绝不影响本返回值。
    record_ingest(
        node_id,
        node_data["type"],
        domain_out,
        related_count=len(conflict.get("related_ids", [])),
        replaced_count=len(replace_ids),
        outdated_count=len(conflict.get("outdated_ids", [])),
    )
    return result


@mcp.tool()
def mem_recent(domain: str = "", limit: int = 10, as_of: float | None = None) -> str:
    """最近记忆列表：按 created_at 倒序（时间戳缺失时退化为按 id 倒序，即插入顺序）

    A3 升级：带 domain 时 TQL FIND 替代 iter_payloads 全遍历（O(logN) vs O(N)）。
    TQL 结果回 Python 排序兜底 created_at 缺失场景，保证行为与旧版一致。
    空 domain 是全量枚举：0.8.3 时 MATCH (n) 硬截断 5000 条故绕道 iter_payloads；
    0.8.5 修复 #32 后 MATCH 可全量返回（含 LIMIT pushdown），故统一走 TQL 获取，
    排序仍留 Python——TQL ORDER BY 会把缺失 created_at 排最前，与
    (created_at or 0, id) 双键倒序语义不一致，不可下推。
    as_of 非空时按 bi-temporal 时间窗回看（valid_at <= as_of 且未在 as_of 前
    invalid_at）；为空 = 原行为不变。
    """
    domain_val = (domain or "").strip().lower()
    raw = []  # list[(nid, payload)]
    # 防注入白名单：domain 仅允许 [a-z0-9_-]。含其他字符（引号/花括号等）
    # 不走 TQL FIND（否则可能注入/破坏查询），退化为 iter_payloads 全遍历过滤——
    # 非法 domain 安全降级、不崩溃（不抛异常）。此校验必须在 _acquire() 之前，
    # 避免 open 的 db 与 iter_payloads 内部 _acquire 双开同一库文件报 Database locked。
    domain_ok = bool(re.fullmatch(r"[a-z0-9_-]+", domain_val or ""))
    # issue #123：「general」是 node_domain() 对空/隐式 domain 的归一别名——
    # TQL FIND {domain: "general"} 只匹配**显式** payload 字段，会把
    # domain 缺省为空串的隐式 general 节点漏掉（与 /mem/search 的 node_domain
    # 口径相反）。改走 iter_payloads + node_domain 过滤兜底，保证隐式 general
    # 可达、口径一致；非法 domain 同样走该兜底（原防注入路径）。其余 domain
    # 保持 TQL 快路径不变。
    if domain_val and (not domain_ok or domain_val == "general"):
        logger.warning(
            "mem_recent domain=%r 走 iter_payloads 兜底（%s）",
            domain_val,
            "非法字符" if not domain_ok else "隐式 general 需 node_domain 口径",
        )
        raw = [(nid, pl) for nid, pl in store.iter_payloads() if node_domain(pl) == domain_val]
    else:
        db = None
        try:
            db = store._acquire()
            if domain_val:
                # FIND 过滤走 Hash 索引；row key 为 "_"
                tql_q = f'FIND {{domain: "{domain_val}"}} RETURN *'
                node_key = "_"
            else:
                # 全量枚举：MATCH 0.8.5 不再截断；row key 为绑定名 "n"
                tql_q = "MATCH (n) RETURN n"
                node_key = "n"
            rows = db.tql(tql_q)
            for r in rows:
                nd = r.row.get(node_key, {})
                pl = nd.get("payload", {}) or {}
                nid = nd.get("id")
                if nid is not None:
                    raw.append((nid, pl))
        except Exception as e:  # noqa: BLE001 —— TQL 失败退化为全遍历保证不丢结果
            # TQL 失败（语法/引擎异常）退化为 iter_payloads 全遍历，保证不丢结果。
            # 须先 close db 释放锁，否则 iter_payloads 内部 _acquire 双开报 Database locked。
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()
                db = None
            logger.warning(f"mem_recent TQL 失败，退化为 iter_payloads: {e}")
            raw = [(nid, pl) for nid, pl in store.iter_payloads() if not domain_val or node_domain(pl) == domain_val]
        finally:
            # 0.7.6 的 with 退出不释放锁，必须显式 close；0.8.2+ 兼容（close 幂等）
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()
    # as_of 历史视图：按时间窗过滤（as_of 为空时 is_valid_at 恒 True，无副作用）
    if as_of is not None:
        raw = [(nid, pl) for nid, pl in raw if is_valid_at(pl, as_of)]
    items = [
        {
            "id": nid,
            "type": pl.get("type", ""),
            "content": _shorten(pl.get("content", ""), 100),
            "importance": pl.get("importance", 0.5),
            "status": pl.get("status", ""),
            "domain": node_domain(pl),
            "created_at": pl.get("created_at"),
        }
        for nid, pl in raw
    ]
    # (created_at, id) 双键倒序：时间戳缺失（None→0）时按 id 倒序兜底
    items.sort(key=lambda x: (x["created_at"] or 0, x["id"]), reverse=True)
    return _to_json({"results": items[:limit], "total": len(items)})


def _task_state_date_str() -> str:
    """当前日期 YYYY-MM-DD（任务状态查询的展示辅助）。"""
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")


@mcp.tool()
def tasks_active(
    project: str = "",
    states: str = "todo,doing,blocked",
    limit: int = 15,
    include_handoff: bool = False,
    fresh_days: int = 30,
) -> str:
    """活跃任务列表：``type=task`` & ``status=active`` & 状态命中，按优先级排序。

    参数：
      - ``project``：非空时只保留该项目（``payload.project``）的任务；
      - ``states``：逗号分隔的状态集（``todo`` / ``doing`` / ``blocked`` /
        ``done`` / ``canceled``）；非法值被忽略，全非法则回退默认三态；
      - ``limit``：返回条数上限（``total`` 仍为过滤后总数）；
      - ``include_handoff``：``true`` 时同时列出会话接续点
        （``kind=handoff`` 的跨会话交接记录），默认排除。显式列出时**不受状态
        过滤限制**——接续点没有「待办状态」语义，``task_state`` 为 ``None`` 也能
        完整取回（``kind`` 由写入层自动打标，见 ``core.task_state._is_handoff``），
        且置顶按 ``importance`` 倒序；
      - ``fresh_days``：新鲜度窗口（天，默认 30）。超期未动的条目不再逐条返回，
        汇总进返回体 ``stale``（``count`` / ``oldest`` / ``hint``）——旧任务由此
        退场，不再污染会话开场工作集（issue #128）；``0`` = 展开全部。

    排序：``doing > blocked > todo`` → ``last_touched_at`` 倒序 → ``importance`` 倒序。
    默认排除 ``payload.legacy = true`` 的老节点（见 CLI ``tasks backfill``）。

    接手留痕（issue #208）：条目若被其他会话接手过（POST /memory/{id}/open
    记录的胶囊接手痕迹），带 ``opened_at``（首次被接手时间）和 ``open_count``
    （接手过的新会话个数，同会话重复接手不重复计数）——胶囊「被几个会话
    接着用 / 何时第一次被接手」由此可读；从未被接手过的条目**不带**这两个
    键（靠键缺席区分「从未接手」与「接过手」）。

    核心逻辑见 core.task_state.list_active_tasks（REST 端点复用同一实现，不复制）。
    """
    from core.task_state import list_active_tasks

    return _to_json(
        list_active_tasks(
            store,
            project=project,
            states=states,
            limit=limit,
            include_handoff=include_handoff,
            fresh_days=fresh_days,
        )
    )


def _mem_review_impl(store, days: int = 7, domain: str = "", tier: str = DEFAULT_TIER) -> str:
    """mem_review 的核心实现（不含 @mcp.tool 装饰），供 REST 端点同款复用。

    ``store`` 由调用方传入（MCP 路径传 _common.store，REST 路径传服务端 store），
    逻辑本身不感知存储来源。issue #168。
    """
    now = time.time()
    window = max(1, int(days)) * 86400.0
    domain_val = (domain or "").strip().lower()

    # 单次全遍历：stats 与四类候选共用同一数据源（与旧实现逐字段一致）
    items = []
    for nid, payload in store.iter_payloads():
        if domain_val and node_domain(payload) != domain_val:
            continue
        items.append(
            {
                "id": nid,
                "type": payload.get("type", ""),
                "content": _shorten(payload.get("content", ""), 100),
                "importance": _to_float(payload.get("importance"), 0.5),
                "status": payload.get("status", ""),
                "domain": node_domain(payload),
                "created_at": payload.get("created_at"),
            }
        )

    total = len(items)
    active = sum(1 for x in items if x["status"] != "outdated")
    outdated = sum(1 for x in items if x["status"] == "outdated")
    kb_chunks = sum(1 for x in items if x["type"] == "kb_chunk")
    memory_nodes = sum(1 for x in items if x["type"] == "memory")

    def _imp(x) -> float:
        return _to_float(x.get("importance"), 0)

    # 四类候选（与旧实现同一构造规则 + 同一排序）
    # recent_ingests：窗口内 type=memory，created_at 倒序；tier 视图过滤
    # （_tier_matches 语义与检索一致：facts=默认/ logs=日志层/ ""=不过滤）
    recent = [
        {k: x[k] for k in ("id", "type", "content", "importance", "status", "domain", "created_at")}
        for x in items
        if x["type"] == "memory"
        and _tier_matches(x["type"], tier)
        and isinstance(x["created_at"], (int, float))
        and x["created_at"]
        and (now - x["created_at"]) <= window
    ]
    recent.sort(key=lambda y: (y["created_at"] or 0, y["id"]), reverse=True)

    # high_value_candidates：importance>=0.6 + active
    high_value = [
        {k: x[k] for k in ("id", "type", "content", "importance", "status", "domain", "created_at")}
        for x in items
        if x["status"] != "outdated" and _imp(x) >= 0.6
    ]
    high_value.sort(key=_imp, reverse=True)

    # stale_outdated：status=outdated（保持遍历序，与旧实现一致）
    stale = [
        {k: x[k] for k in ("id", "type", "content", "importance", "status", "domain", "created_at")}
        for x in items
        if x["status"] == "outdated"
    ]

    # low_value_candidates：importance<=0.4 + active + 非 kb_chunk（保持遍历序）
    low_value = [
        {k: x[k] for k in ("id", "type", "content", "importance", "status", "domain", "created_at")}
        for x in items
        if x["status"] != "outdated" and x["type"] != "kb_chunk" and _imp(x) <= 0.4
    ]

    return _to_json(
        {
            "review_window_days": days,
            "stats": {
                "total": total,
                "active": active,
                "outdated": outdated,
                "memory": memory_nodes,
                "kb_chunk": kb_chunks,
            },
            "recent_ingests": recent[:30],
            "high_value_candidates": high_value[:20],
            "stale_outdated": stale[:20],
            "low_value_candidates": low_value[:20],
        }
    )


@mcp.tool()
def mem_review(days: int = 7, domain: str = "", tier: str = DEFAULT_TIER) -> str:
    """复盘盘点（复盘机制融入系统，复盘=记忆治理）。

    统计全库节点 + 盘点最近 days 天 ingest 的记忆，输出复盘草稿：
      - recent_ingests：窗口内新增记忆（type=memory，按 created_at 倒序）
      - high_value_candidates：importance>=0.6 且 active（升级知识库候选）
      - stale_outdated：outdated 节点（版本链历史，可清理候选）
      - low_value_candidates：importance<=0.4 且 active 的非知识节点（清理候选）
    供每日复盘使用：维护者裁决后升级/清理，再存 type=review 节点。

    tier：记忆分层视图，仅作用于 recent_ingests（与 mem_search 同款判定，
    复用 _tier_matches）——"facts"（默认）只回事实层 / "logs" 只回日志层 /
    ""（空串）不过滤（等价改动前行为）。kb_chunk / novel_chunk 不入本体系，
    走既有 scope 隔离；未登记的 type 一律归 facts。

    A3 候选集回退全遍历：triviumdb 0.8.3 的 TQL FIND/MATCH 硬截断 5000 条，且
    TQL 返回顺序为 id 升序、不同于 iter_payloads 的 all_node_ids 顺序——对
    stale/low_value 这类旧实现「不排序、保持遍历序」的候选集，走 TQL 无法与旧实现
    逐字段一致。故四类候选与 stats 统一来自单次全遍历（与旧实现同一数据源）。
    """
    return _mem_review_impl(store, days=days, domain=domain, tier=tier)


# ---- 版本历史查询（REVISED_BY 修订链）----
_VERSION_RE = re.compile(r"\[SOUL变更日志\]\s*(\d{4}-\d{2}-\d{2})\s*版本\s*v?([\d.]+)")
_TITLE_RE = re.compile(r"\*\*(.+?)\*\*")


def _parse_version_content(content: str) -> tuple:
    """解析版本日志 content → (date, version, title)"""
    content = content or ""
    m = _VERSION_RE.search(content)
    date = m.group(1) if m else ""
    version = m.group(2) if m else ""
    t = _TITLE_RE.search(content)
    # 无 **标题** 时取「：」后的前 50 字
    title = t.group(1).strip() if t else content.split("：", 1)[-1].strip()[:50]
    return date, version, title


@mcp.tool()
def mem_version_history(domain: str = "hermes", full_content: bool = False, offset: int = 0, limit: int = 20) -> str:
    """
    版本历史查询：沿 REVISED_BY 修订链（新版本 → 旧版本）返回版本演进摘要，
    用于查 SOUL 版本日志等历史事件链。
    参数：domain 按节点域过滤（默认 hermes）；full_content=True 时
    每条含完整 content 原文；offset/limit 分页（链长 > limit 时分页）。
    返回 JSON：{"found", "start_id", "chain_length", "versions": [...]}，
    每条版本为 {id, version, date, title, importance}。
    """
    # 1. 找 domain 下最新的事件节点（created_at 最大，缺失时按 id 最大兜底）
    candidates = []
    for nid, payload in store.iter_payloads():
        if payload.get("type") != "event" or node_domain(payload) != (domain or "").strip().lower():
            continue
        try:
            ts = float(payload.get("created_at") or 0)
        except (TypeError, ValueError):
            ts = 0
        candidates.append((ts, nid))
    if not candidates:
        return _to_json({"found": False, "start_id": None, "chain_length": 0, "versions": []})
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    start_id = candidates[0][1]

    # 2. 沿 REVISED_BY 出边往回走（新 → 旧），收集版本链（防环）
    chain = []
    seen = set()
    cur = start_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        node = store.get_node(cur)
        if not node:
            break
        chain.append(node)
        nxt = None
        for edge in store.get_edges(cur):
            if edge.label == "REVISED_BY":
                nxt = edge.target_id
                break
        cur = nxt

    # 3. 组装版本摘要 + 分页
    versions = []
    for node in chain[offset : offset + limit]:
        payload = node.get("payload", {}) or {}
        content = payload.get("content", "")
        date, version, title = _parse_version_content(content)
        item = {
            "id": node.get("id"),
            "version": version,
            "date": date,
            "title": title,
            "importance": payload.get("importance", 0.5),
        }
        if full_content:
            item["content"] = content
        versions.append(item)

    return _to_json(
        {
            "found": True,
            "start_id": start_id,
            "chain_length": len(chain),
            "versions": versions,
        }
    )


@mcp.tool()
def mem_fact_history(node_id: int) -> str:
    """
    单条事实的时间线：返回该节点的 bi-temporal 时间字段，以及
    「它取代了谁」（沿 REVISED_BY 出边）与「谁取代了它」（沿反向边）。

    与 mem_version_history 的区别：后者按 domain 找最新 event 并整条链列出，
    面向「版本日志」；本工具面向**任意一条事实**，回答「这条记忆的演变」——
    这正是 bi-temporal 的反向索引（谁失效了谁）的检索入口。

    返回 JSON：{found, node_id, times:{valid_at,invalid_at,expired_at,created_at},
    superseded:[…], superseded_by:[…]}。每项为 {id, content_summary, times}。
    """
    node = store.get_node(node_id)
    if not node:
        return _to_json({"found": False, "node_id": node_id, "error": "节点不存在"})
    payload = node.get("payload", {}) or {}

    def _brief(nid: int) -> dict | None:
        n = store.get_node(nid)
        if not n:
            return None
        p = n.get("payload", {}) or {}
        return {
            "id": nid,
            "content_summary": _shorten(p.get("content", ""), 80),
            "times": time_fields(p),
        }

    # 出边 REVISED_BY：本节点 → 被它取代的旧节点
    superseded = []
    for edge in store.get_edges(node_id):
        if (getattr(edge, "label", "") or "").upper() == "REVISED_BY":
            brief = _brief(edge.target_id)
            if brief:
                superseded.append(brief)

    # 反向边：谁指向本节点（新版本 → 本节点）＝ 取代了本节点的更新事实
    superseded_by = []
    for edge in store.get_incoming_edges(node_id):
        if (getattr(edge, "label", "") or "").upper() == "REVISED_BY":
            brief = _brief(edge.source_id)
            if brief:
                superseded_by.append(brief)

    return _to_json(
        {
            "found": True,
            "node_id": node_id,
            "type": payload.get("type", ""),
            "status": payload.get("status", ""),
            "times": time_fields(payload),
            "superseded": superseded,
            "superseded_by": superseded_by,
        }
    )


# ---- 记忆分层（tier）：检索侧视图，不改存储、不迁数据 ----
# 常量（TIER_FACTS / TIER_LOGS / DEFAULT_TIER）已在文件顶部从 config.py 读取；
# 此处仅保留判定函数。facts = 事实层（默认检索与注入池）；logs = 日志层；
# ""（空串）= 不过滤（显式历史通道）；未登记 type 一律归 facts。


def _tier_matches(ptype: str, tier: str) -> bool:
    """type 是否落在 tier 层。tier 为空串 = 不过滤（恒 True）；未知 tier 值 = 退回不过滤。"""
    t = (tier or "").strip().lower()
    if t not in ("facts", "logs"):
        return True
    if t == "logs":
        return (ptype or "") in TIER_LOGS
    # facts：日志层排除，其余（含未登记 type 与 kb_chunk/novel_chunk）保留
    return (ptype or "") not in TIER_LOGS


def _mem_search_impl(
    query: str,
    scope: str = "all",
    domain: str = "",
    domain_bias: str = "",
    top_k: int = 5,
    include_neighbors: bool = False,
    neighbor_limit: int = 5,
    block: str = "",
    include_outdated: bool = False,
    domain_boost: str = "",
    tier: str = DEFAULT_TIER,
    as_of: float | None = None,
) -> dict:
    """
    mem_search 的核心实现（返回 dict，供 mem_search 工具复用）。
    v2.0 统一语义层：
      - domain_bias："" 不额外 bias；"memory" 非 kb_chunk ×DOMAIN_BIAS_WEIGHT；
        "kb" 对 kb_chunk ×DOMAIN_BIAS_WEIGHT。
      - 最终权重 = base_score × (bias 系数)，权重来自 config（DOMAIN_BIAS_WEIGHT，可配），
        在过滤之后、排序之前应用。
      - domain_boost："" 不额外加分；非空时对 node_domain(payload) == domain_boost 的
        候选在语义分上「加」Config.DOMAIN_BOOST_EPS（加性软加权，不同于 domain 的硬过滤、
        也不同于 domain_bias 的乘性加权）；默认空 = 不改变任何现有行为。
      - v2.x 新鲜度加权：Config.MEM_SEARCH_FRESHNESS_WEIGHT 非零时对候选加
        score += WEIGHT × decay(节点年龄)（加性时效信号，见 core/bitemporal.
        freshness_decay）；默认 0 = 关闭（逐位一致）。as_of 历史视图下不生效。
      - v4.0 outdated 语义：默认过滤 status=="outdated" 的旧版本（只回当前有效节点），
        include_outdated=True 时不过滤，返回全部（显式历史可追溯通道）。
    """
    if scope not in ("memory", "kb", "all"):
        scope = "all"
    if domain_bias not in ("memory", "kb"):
        domain_bias = ""
    query = (query or "").strip()
    if not query:
        return {"results": [], "scope": scope, "hint": "查询内容不能为空"}
    emb = store.embed_text(query)
    # 一次向量检索，拉宽召回再按 scope 过滤截断，保证过滤后仍有足够结果
    # as_of 历史视图：底层必须拉全量（含 outdated），再由时间窗过滤决定去留——
    # 否则「当时为真、现在 outdated」的节点在检索上游就被滤掉，as_of 看不见它们。
    _fetch_outdated = include_outdated or (as_of is not None)
    results = store.search_similar(
        emb,
        top_k=max(top_k * 3, 30),
        expand_depth=getattr(Config, "RETRIEVAL_EXPAND_DEPTH", 0),
        block=block,
        include_outdated=_fetch_outdated,
    )
    items = []
    for r in results:
        payload = r.get("payload", {}) or {}
        ptype = payload.get("type", "")
        is_kb = ptype == "kb_chunk"
        # v4.0 outdated 语义：默认跳过被取代的旧版本（不区分类型；kb_chunk 若被标
        # outdated 同样过滤），显式 include_outdated=True 时保留（历史可追溯）。
        # bi-temporal as_of 历史视图：as_of 非空时以「时间窗」为准决定去留——
        # 当时为真的节点应返回，哪怕它现在 status=outdated（这正是一期
        # 「按时间点回看」的价值）。故 as_of 模式下跳过基于当前 status 的过滤。
        if as_of is None:
            if not include_outdated and payload.get("status") == "outdated":
                continue
        elif not is_valid_at(payload, as_of):
            continue
        # 记忆分层（tier）：后置视图过滤，与 scope/domain/block 并列。
        # tier="" 时 _tier_matches 恒 True，逐条等价于改动前行为（回归红线）。
        if not _tier_matches(ptype, tier):
            continue
        if scope == "memory" and is_kb:
            continue
        if scope == "kb" and not is_kb:
            continue
        # domain 过滤仅作用于记忆节点（kb_chunk 无 character_name 概念，domain 为正式字段）
        if domain and not is_kb and node_domain(payload) != (domain or "").strip().lower():
            continue
        # 分区块（审查补丁）：block 非空时主结果也按区块过滤——
        # 带 --block hermes 的搜索只返回 hermes 区块内容，语义命中的跨区块节点丢弃
        if block and not domain_in_block(node_domain(payload), block):
            continue
        # v2.0 统一语义层加权：过滤之后、排序之前应用（只乘系数，不改变过滤逻辑）
        score = _to_float(r.get("score"), 0.0)
        if (domain_bias == "memory" and not is_kb) or (domain_bias == "kb" and is_kb):
            score *= Config.DOMAIN_BIAS_WEIGHT
        if domain_boost and node_domain(payload) == domain_boost.strip().lower():
            score += Config.DOMAIN_BOOST_EPS
        # v2.x 新鲜度加权（issue #175，数据优先）：score += WEIGHT × decay(节点年龄)。
        # as_of 历史视图下不生效——双时间线回看按「当时为真」排序，与今天的
        # 记忆新鲜度无关（验收红线：as_of 路径不受影响）。默认 WEIGHT=0 时
        # 完全跳过（零开销，行为与改动前逐位一致，有回归测试保证）。
        if as_of is None:
            _fw = Config.MEM_SEARCH_FRESHNESS_WEIGHT
            if _fw:
                _decay = freshness_decay(
                    payload,
                    time.time(),
                    Config.MEM_SEARCH_FRESHNESS_SCALE_DAYS,
                    Config.MEM_SEARCH_FRESHNESS_DECAY,
                )
                if _decay is not None:
                    score += _fw * _decay
        meta = {
            "type": ptype,
            "importance": payload.get("importance", 0.5),
            "status": payload.get("status", ""),
            "domain": node_domain(payload),
        }
        # bi-temporal：世界时间字段随结果透出（无则不加，保持 meta 精简）
        _times = time_fields(payload)
        if _times:
            meta["times"] = _times
        if is_kb:
            meta["source_path"] = payload.get("source_path", "")
            meta["title"] = payload.get("title", "")
        items.append(
            {
                "id": r.get("id"),
                "type": ptype,
                "score": round(score, 4),
                "summary": _shorten(payload.get("content", ""), 150),
                "meta": meta,
            }
        )
    # bias 后按最终 score 降序排序再截断（bias 需影响排序，不能按原始顺序收集后 break）
    items.sort(key=lambda x: x["score"], reverse=True)
    top_items = items[:top_k]
    result = {"results": top_items, "scope": scope}
    if domain_bias:
        result["bias"] = domain_bias
    # 阶段三：分区返回——图关联区（语义区原样，邻居独立展示，不参与语义排序）
    if include_neighbors:
        neighbors = _collect_neighbors(top_items, max(1, min(int(neighbor_limit), 20)))
        result["neighbors"] = neighbors
        result["neighbor_count"] = len(neighbors)
    return result


@mcp.tool()
def mem_search(
    query: str,
    scope: str = "all",
    domain: str = "",
    domain_bias: str = "",
    top_k: int = 5,
    include_neighbors: bool = False,
    neighbor_limit: int = 5,
    block: str = "",
    include_outdated: bool = False,
    domain_boost: str = "",
    tier: str = DEFAULT_TIER,
    as_of: float | None = None,
) -> str:
    """
    统一检索入口：记忆 + 知识库混合检索。
    scope 取值：memory（只查记忆节点，排除 kb_chunk）/ kb（只查知识库块）/ all（都查）。
    domain 非空时按 node_domain 过滤记忆（kb_chunk 的 domain 是 "kb"，不受影响）。
    domain_bias 取值 "" / "memory" / "kb"：为空不额外 bias；"memory" 记忆优先
        （非 kb_chunk 的 score *= DOMAIN_BIAS_WEIGHT）；"kb" 知识优先（kb_chunk 的 score
        *= DOMAIN_BIAS_WEIGHT）。
        权重来自 config（DOMAIN_BIAS_WEIGHT，可配）。
        bias 在过滤之后、排序之前应用（先 bias 再按最终 score 排序截断）。
    domain_boost 非空时对 node_domain(payload) == domain_boost 的候选在语义分上
        「加」Config.DOMAIN_BOOST_EPS（加性软加权，不同于 domain 的硬过滤、也不同于
        domain_bias 的乘性加权）；默认为空 = 不改变任何现有行为。
    双层返回原则不变：只返回 150 字摘要 + meta，不返回全文（全文请用 mem_get_full）。
    v3.0 分区返回：include_neighbors=True 时，语义区（results）原样返回，
        额外附 neighbors 图关联区（语义命中节点的一跳邻居，score = via_score × weight，
        去重、relation 大写、按 score 降序）。include_neighbors=False 时输出与旧版完全一致。
        neighbor_limit 钳制 1-20（默认 5）。
    v4.0 outdated 语义：默认只回当前有效节点（status != "outdated"）——被新版本取代的
        旧版保留在库中可追溯但不参与普通检索；include_outdated=True 时返回全部
        （历史可追溯通道）。
    v5.0 记忆分层（tier）：tier="facts"（默认）只回事实层，把日志层（record/event/
        git_commit，约占活跃节点四成）从检索池摘出；tier="logs" 只回日志层；
        tier=""（空串）不过滤，等价于改动前行为（显式历史通道）。
        kb_chunk / novel_chunk 不入本体系，走 scope 隔离；未登记 type 一律归 facts。
    v6.0 bi-temporal as_of 历史视图：as_of（数值时间戳）非空时，按「该时刻事实是否
        为真」（valid_at <= as_of 且未在 as_of 前 invalid_at）过滤，以时间窗为准、
        不看当前 status——当时为真的节点即使现已被标 outdated 也会返回。as_of 为空
        = 原行为不变。
    """
    return _to_json(
        _mem_search_impl(
            query,
            scope=scope,
            domain=domain,
            domain_bias=domain_bias,
            top_k=top_k,
            include_neighbors=include_neighbors,
            neighbor_limit=neighbor_limit,
            block=block,
            include_outdated=include_outdated,
            domain_boost=domain_boost,
            tier=tier,
            as_of=as_of,
        )
    )


# ---- 混合检索（FTS5 精确 + 语义向量 的 RRF 融合 / 级联策略）----
# 只新增，不改动 mem_search / _mem_search_impl 现有行为。
# RRF 标准 k（见 Config.RRF_K）：单侧命中也算贡献；每项 meta 标出 fts_hit / sem_hit 来源。


def _visible(payload: dict, include_outdated: bool = False, as_of: float | None = None) -> bool:
    """节点可见性判据（语义路 / 实体路共用，保证多路过滤口径一致）。

    v4.0 outdated 语义：默认跳过被取代的旧版本（status == "outdated"）；
    include_outdated=True 时保留；as_of 非空时以时间窗为准（不看 status），
    见 core.bitemporal.is_valid_at。内联版本散落在若干处，此处集中一处，
    行为与既有内联判断逐位一致。
    """
    if as_of is None:
        return not (not include_outdated and payload.get("status") == "outdated")
    return is_valid_at(payload, as_of)


def _sem_candidate_items(
    query: str,
    scope: str,
    domain: str,
    domain_bias: str,
    top_k: int,
    block: str,
    include_outdated: bool = False,
    tier: str = DEFAULT_TIER,
    as_of: float | None = None,
) -> list:
    """语义候选：复用 _mem_search_impl 宽松召回（top_k*3），按 score 降序排名。"""
    sem = _mem_search_impl(
        query,
        scope,
        domain,
        domain_bias,
        top_k=max(top_k * 3, 30),
        include_neighbors=False,
        block=block,
        include_outdated=include_outdated,
        tier=tier,
        as_of=as_of,
    )
    items = sem.get("results", [])
    return sorted(items, key=lambda it: it.get("score", 0.0), reverse=True)


def _fts_only_item(
    node_id: int,
    scope: str,
    domain: str,
    block: str,
    include_outdated: bool = False,
    tier: str = DEFAULT_TIER,
    as_of: float | None = None,
):
    """FTS 命中但语义未命中的节点：按 payload 补全 mem_search 同构条目。

    复用 _mem_search_impl 的 scope/domain/block/tier 过滤语义，
    避免不同 scope 下 FTS 侧混入越界结果。
    """
    node = store.get_node(node_id)
    if not node:
        return None
    payload = node.get("payload", {}) or {}
    ptype = payload.get("type", "")
    is_kb = ptype == "kb_chunk"
    # v4.0 outdated 语义：FTS-only 侧与语义侧保持一致，默认过滤旧版本
    # as_of 非空时以时间窗为准（不看 status），与语义侧口径一致
    if as_of is None:
        if not include_outdated and payload.get("status") == "outdated":
            return None
    elif not is_valid_at(payload, as_of):
        return None
    # v5.0 tier：FTS-only 侧同样受分层约束，否则日志层会从 FTS 路漏回结果集
    if not _tier_matches(ptype, tier):
        return None
    if scope == "memory" and is_kb:
        return None
    if scope == "kb" and not is_kb:
        return None
    if domain and not is_kb and node_domain(payload) != (domain or "").strip().lower():
        return None
    if block and not domain_in_block(node_domain(payload), block):
        return None
    meta = {
        "type": ptype,
        "importance": payload.get("importance", 0.5),
        "status": payload.get("status", ""),
        "domain": node_domain(payload),
    }
    # bi-temporal：世界时间字段随结果透出（无则不加，保持 meta 精简）
    _times = time_fields(payload)
    if _times:
        meta["times"] = _times
    if is_kb:
        meta["source_path"] = payload.get("source_path", "")
        meta["title"] = payload.get("title", "")
    return {
        "id": node_id,
        "type": ptype,
        "score": 0.0,
        "summary": _shorten(payload.get("content", ""), 150),
        "meta": meta,
    }


def _rrf_fuse(
    sem_ids: list,
    fts_ids: list,
    top_k: int,
    k: float = 60.0,
    w_sem: float = 1.0,
    w_fts: float = 1.0,
    ent_ids: list | None = None,
    w_ent: float = 0.0,
    extra_channels: list | None = None,
) -> list:
    """纯 RRF 融合打分（不碰库，供单测与 _hybrid_rrf 复用）。

    sem_ids：语义侧排名（按 rank 顺序的 node_id 列表）；fts_ids：FTS 侧排名
    （按 rank 顺序的 node_id 列表）。0-based rank；单侧命中同样计入；
    k 为标准 reciprocal rank 常数（默认 60）。
    w_sem / w_fts：两侧权重，语义侧累加 w_sem/(k+rank)，FTS 侧累加
    w_fts/(k+rank)；默认 1.0 / 1.0 与原等权行为逐位一致。

    ent_ids / w_ent：可选的**第三路（实体锚定路）**排名与权重。
    ent_ids=None（默认）时行为与两路版本逐位一致；w_ent=0 时该路不计分。
    实体路的命中标记单独给出（meta.hit 由调用方自行组装），此处保持
    返回结构不变以免破坏既有调用方。

    extra_channels：第四路及以后的**泛化可选通道**，形如
    `[(ids, weight), ...]`——例如 TDB BM25 文本通道（TDB_TEXT_ENABLED=1 时
    由 _hybrid_rrf 传入）。与 ent_ids 同规则计分（0-based rank、单侧命中
    也计入），不影响 fts_hit/sem_hit 命中标记；默认 None，不传时行为与
    改动前逐位一致。

    返回按 RRF 分降序的 [(node_id, rrf_score, fts_hit, sem_hit), ...] 直至 top_k。
    """
    rrf: dict = {}
    sem_hit: set = set()
    fts_hit: set = set()
    for rank, nid in enumerate(sem_ids):
        if nid is None:
            continue
        rrf[nid] = rrf.get(nid, 0.0) + w_sem / (k + rank)
        sem_hit.add(nid)
    for rank, nid in enumerate(fts_ids):
        if nid is None:
            continue
        rrf[nid] = rrf.get(nid, 0.0) + w_fts / (k + rank)
        fts_hit.add(nid)
    if ent_ids and w_ent:
        for rank, nid in enumerate(ent_ids):
            if nid is None:
                continue
            rrf[nid] = rrf.get(nid, 0.0) + w_ent / (k + rank)
    for ch_ids, ch_weight in extra_channels or []:
        if not ch_ids or not ch_weight:
            continue
        for rank, nid in enumerate(ch_ids):
            if nid is None:
                continue
            rrf[nid] = rrf.get(nid, 0.0) + ch_weight / (k + rank)
    return [
        (nid, score, nid in fts_hit, nid in sem_hit)
        for nid, score in sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)
    ][:top_k]


def _hybrid_rrf(
    query: str,
    scope: str,
    domain: str,
    domain_bias: str,
    top_k: int,
    fts_limit: int,
    block: str,
    include_outdated: bool = False,
    tier: str = DEFAULT_TIER,
    as_of: float | None = None,
) -> tuple[list, dict]:
    """RRF 融合：语义排名 + FTS 排名的 reciprocal rank 求和（k=60）。

    两个排名都是 0-based；单侧命中也计入 rrf；按 rrf 降序取 top_k。

    返回 `(items, channels)`，其中 channels 记录各检索通道状态
    （`fts` / `semantic`），FTS 通道降级时如实标出并在日志留痕，
    避免「通道挂了」被误读成「确实没命中」；`exact_query` / `fts_weight`
    透出精确标识符判定与本次实际使用的 FTS 权重（线上排查走没走抬高路）。
    """
    sem_items = _sem_candidate_items(
        query, scope, domain, domain_bias, top_k, block, include_outdated=include_outdated, tier=tier, as_of=as_of
    )
    fts, fts_status = search_fts_status(query, limit=fts_limit)
    if fts_status == "degraded":
        logger.warning("混合检索降级：FTS 通道不可用（query=%r），结果仅来自语义通道", query[:60])

    # 精确标识符查询（commit hash / PR 号 / 工单号）专用 FTS 权重：
    # 此类 query 语义通道天然失灵，FTS 是唯一能救场的通道，改用更高的
    # RRF_FTS_EXACT_WEIGHT 保证 FTS-only 命中能进 top_k；普通 query 走原权重。
    exact_query = is_exact_identifier_query(query)
    w_fts = Config.RRF_FTS_EXACT_WEIGHT if exact_query else Config.RRF_FTS_WEIGHT

    # TDB BM25 文本通道（第四路，默认关闭）：TDB_TEXT_ENABLED=0 时**零成本**，
    # 连延迟导入都不做，保证默认路径与引入前逐位一致。
    extra_channels: list = []
    tdb_status: str = ""
    tdb_hit_ids: list = []  # 保序（RRF 需要 rank 顺序）
    tdb_hit_set: set = set()  # 无序（per-item 命中标记用）
    if getattr(Config, "TDB_TEXT_ENABLED", False):
        # 条件触发：FTS 命中数 ≥ 下限说明 FTS 已够好 → 跳过 TDB 路
        # （不 import、不查询、不开连接）；0 = 永不跳过（等价旧行为）。
        tdb_floor = getattr(Config, "TDB_TEXT_FTS_FLOOR", 3)
        if tdb_floor > 0 and len(fts) >= tdb_floor:
            tdb_status = "skipped"
        else:
            from core.tdb_text_index import search as _tdb_text_search  # 可选通道，失败即降级

            tdb_limit = Config.TDB_TEXT_LIMIT or fts_limit  # 0 = 沿用 FTS 的 limit
            tdb_rows, tdb_status = _tdb_text_search(query, limit=tdb_limit)
            if tdb_status == "degraded":
                logger.warning("混合检索降级：TDB 文本通道不可用（query=%r），该路不计入融合", query[:60])
            tdb_hit_ids = [r.get("node_id") for r in tdb_rows if r.get("node_id") is not None]
            tdb_hit_set = set(tdb_hit_ids)
            extra_channels.append((tdb_hit_ids, Config.TDB_TEXT_WEIGHT))

    ranked = _rrf_fuse(
        [it.get("id") for it in sem_items if it.get("id") is not None],
        [r.get("node_id") for r in fts if r.get("node_id") is not None],
        top_k,
        Config.RRF_K,
        w_sem=Config.RRF_SEM_WEIGHT,
        w_fts=w_fts,
        extra_channels=extra_channels or None,
    )

    # 语义条目按 id 建表，便于合并命中来源标记；FTS-only 节点按 payload 补全
    by_id = {item.get("id"): item for item in sem_items if item.get("id") is not None}
    merged = []
    for nid, score, fts_hit_flag, sem_hit_flag in ranked:
        item = by_id.get(nid)
        if item is None:
            item = _fts_only_item(nid, scope, domain, block, include_outdated=include_outdated, tier=tier, as_of=as_of)
            if item is None:
                continue
        item = dict(item)
        meta = dict(item.get("meta", {}) or {})
        meta["fts_hit"] = fts_hit_flag
        meta["sem_hit"] = sem_hit_flag
        if tdb_status:
            # 仅启用 TDB 文本通道时才出现该键（默认关闭的输出逐位不变）；
            # skipped（FTS 够好跳过）与真正未命中靠此处 False + channels 里的
            # "tdb_text": "skipped" 区分开。
            meta["tdb_text_hit"] = nid in tdb_hit_set
        item["meta"] = meta
        item["score"] = round(score, 4)
        merged.append(item)
        if len(merged) >= top_k:
            break
    channels = {
        "semantic": "ok" if sem_items else "empty",
        "fts": fts_status,
        "exact_query": exact_query,
        "fts_weight": w_fts,
    }
    if tdb_status:
        # 仅启用 TDB 文本通道时才出现该键（默认关闭的输出逐位不变）。
        channels["tdb_text"] = tdb_status
    return merged, channels


def _hybrid_cascade(
    query: str,
    scope: str,
    domain: str,
    domain_bias: str,
    top_k: int,
    fts_limit: int,
    block: str,
    include_outdated: bool = False,
    tier: str = DEFAULT_TIER,
    as_of: float | None = None,
) -> tuple[list, dict]:
    """级联：FTS 粗筛候选集 → 向量精排（只留交集）→ 不足 top_k 从剩余语义补足。

    候选集为空时退化为纯语义结果；兜底条目 fts_hit=False 如实标记未过 FTS 粗筛。

    返回 `(items, channels)`；FTS 通道降级（索引缺失/查询异常）与
    「粗筛确实无命中」是两种不同情况，后者才是设计内的退化，前者需要留痕。
    """
    fts, fts_status = search_fts_status(query, limit=fts_limit)
    if fts_status == "degraded":
        logger.warning("混合检索降级：FTS 通道不可用（query=%r），级联退化为纯语义", query[:60])
    fts_ids = {r.get("node_id") for r in fts if r.get("node_id") is not None}
    sem_items = _sem_candidate_items(
        query, scope, domain, domain_bias, top_k, block, include_outdated=include_outdated, tier=tier, as_of=as_of
    )

    in_candidate = [it for it in sem_items if it.get("id") in fts_ids]
    rest = [it for it in sem_items if it.get("id") not in fts_ids]
    picked = in_candidate[:top_k]
    if len(picked) < top_k:
        picked = picked + rest[: top_k - len(picked)]

    items = []
    for it in picked:
        it = dict(it)
        meta = dict(it.get("meta", {}) or {})
        meta["fts_hit"] = it.get("id") in fts_ids
        meta["sem_hit"] = True
        it["meta"] = meta
        it["score"] = round(_to_float(it.get("score"), 0.0), 4)
        items.append(it)
    channels = {"semantic": "ok" if sem_items else "empty", "fts": fts_status}
    return items, channels


def _hybrid_search_impl(
    query: str,
    scope: str = "all",
    domain: str = "",
    domain_bias: str = "",
    top_k: int = 5,
    mode: str = "rrf",
    fts_limit: int = 50,
    include_neighbors: bool = False,
    neighbor_limit: int = 5,
    block: str = "",
    include_outdated: bool = False,
    tier: str = DEFAULT_TIER,
    as_of: float | None = None,
) -> dict:
    """
    mem_hybrid_search 的核心实现（返回 dict，供 mem_hybrid_search 工具复用）。
    混合检索增强：
      - mode="rrf"（默认）：Reciprocal Rank Fusion（k=60），FTS5 精确 + 语义向量
        双路排名求和，单侧命中也算贡献；score = RRF 分。
      - mode="cascade"：FTS 粗筛候选集 → 向量精排只保留交集 → 交集不足 top_k
        时从剩余语义结果按 score 补足（候选集为空退化为纯语义）；score = 语义分。
    返回结构与 mem_search 一致：{"results", "scope", "bias"(可选), "mode",
    "neighbors"/"neighbor_count"(include_neighbors=True 时)}。
    空 query 返回 hint；任何异常吞掉返回 hint，不抛出。
    v4.0 outdated 语义：默认过滤 status=="outdated" 旧版本；include_outdated=True 时返回全部。
    """
    if scope not in ("memory", "kb", "all"):
        scope = "all"
    if domain_bias not in ("memory", "kb"):
        domain_bias = ""
    if mode not in ("rrf", "cascade"):
        mode = "rrf"
    query = (query or "").strip()
    if not query:
        return {"results": [], "scope": scope, "hint": "查询内容不能为空"}
    top_k = max(1, int(top_k or 0))
    fts_limit = max(1, int(fts_limit or 0))
    try:
        if mode == "cascade":
            items, channels = _hybrid_cascade(
                query,
                scope,
                domain,
                domain_bias,
                top_k,
                fts_limit,
                block,
                include_outdated=include_outdated,
                tier=tier,
                as_of=as_of,
            )
        else:
            items, channels = _hybrid_rrf(
                query,
                scope,
                domain,
                domain_bias,
                top_k,
                fts_limit,
                block,
                include_outdated=include_outdated,
                tier=tier,
                as_of=as_of,
            )
        result: dict[str, Any] = {"results": items, "scope": scope, "mode": mode, "channels": channels}
        if channels.get("fts") == "degraded":
            # issue #122：FTS 通道真降级（向量侧仍可用）→ 保留结果 + 程序可读
            # 契约字段，不只靠 channels 字符串。
            result["degraded"] = True
            result["errors"] = ["fts channel degraded: 结果仅来自语义通道"]
        if channels.get("tdb_text") == "degraded":
            # TDB 文本通道真降级（可选通道，仅启用时存在）：同类契约留痕。
            result["degraded"] = True
            result.setdefault("errors", [])
            result["errors"] = [*result["errors"], "tdb_text channel degraded: 该路不计入融合"]
        if domain_bias:
            result["bias"] = domain_bias
        if include_neighbors:
            neighbors = _collect_neighbors(items, max(1, min(int(neighbor_limit), 20)))
            result["neighbors"] = neighbors
            result["neighbor_count"] = len(neighbors)
        return result
    except (EmbeddingInputTooLongError, EmbeddingUnavailableError):
        # issue #122：embedding 类失败向上抛（REST 由 main.py 统一映射，与
        # /mem/search 同状态码同语义），不再伪装成 200 + 空结果。
        raise
    except Exception as e:  # noqa: BLE001 —— 真·局部降级保留结果并结构化留痕
        # 非 embedding 的失败（如 FTS 子路径问题）不再只靠 hint 字符串告知：
        # degraded / errors 是程序可读的契约字段。
        logger.warning("混合检索局部降级（query=%r）: %s", query[:60], e)
        return {
            "results": [],
            "scope": scope,
            "mode": mode,
            "degraded": True,
            "errors": [str(e)],
            "hint": f"混合检索失败: {e}",
        }


@mcp.tool()
def mem_hybrid_search(
    query: str,
    scope: str = "all",
    domain: str = "",
    domain_bias: str = "",
    top_k: int = 5,
    mode: str = "rrf",
    fts_limit: int = 50,
    include_neighbors: bool = False,
    neighbor_limit: int = 5,
    block: str = "",
    include_outdated: bool = False,
    tier: str = DEFAULT_TIER,
    as_of: float | None = None,
) -> str:
    """
    混合检索：FTS5 精确检索 + 语义向量检索的融合排序。
    mode 取值："rrf"（默认，Reciprocal Rank Fusion，k=60，单侧命中也算）/
              "cascade"（FTS 粗筛候选集 → 向量精排交集，交集不足从剩余语义补足）。
    scope 同 mem_search（memory/kb/all）；fts_limit 控制 FTS 侧候选量；
    每项返回 {id, type, score, summary, meta}，meta 含 fts_hit/sem_hit 命中来源标记；
    include_neighbors=True 时附 neighbors 图关联区（复用 _collect_neighbors）。
    其余参数（domain/domain_bias/neighbor_limit/block）语义同 mem_search。
    v4.0 outdated 语义：默认过滤 status=="outdated" 旧版本；include_outdated=True 时返回全部
        （历史可追溯通道）。
    v5.0 记忆分层（tier）：同 mem_search——"facts"（默认）只回事实层 / "logs" 只回日志层 /
        ""（空串）不过滤（等价改动前行为）。语义侧与 FTS-only 侧同时受约束。
    v6.0 bi-temporal as_of：非空时按时间窗回看（语义侧与 FTS-only 侧同口径），
        以时间窗为准、不看当前 status；为空 = 原行为不变。
    """
    return _to_json(
        _hybrid_search_impl(
            query,
            scope,
            domain,
            domain_bias,
            top_k,
            mode,
            fts_limit,
            include_neighbors,
            neighbor_limit,
            block,
            include_outdated,
            tier,
            as_of,
        )
    )


@mcp.tool()
def mem_unpack(path: str, preview_chars: int = 80) -> str:
    """只读解开一个 MemoryHub 记忆包（.json）：校验格式/版本、扫描敏感信息、给人读摘要。
    不写入任何东西。path 为记忆包文件的绝对路径或相对当前工作目录的路径。"""
    try:
        result = read_pack(path)
        if not result.get("ok"):
            return _to_json(result)
        summary = summarize_pack(result, preview_chars=preview_chars)
        issues = result.get("issues") or []
        summary["issue_counts"] = {
            "secret": sum(1 for i in issues if i.get("kind") == "secret"),
            "empty_content": sum(1 for i in issues if i.get("kind") == "empty_content"),
            "malformed_node": sum(1 for i in issues if i.get("kind") == "malformed_node"),
        }
        summary["stats"] = result.get("stats", {})
        return _to_json(summary)
    except Exception as e:  # noqa: BLE001 —— 统一转成可读错误对象
        return _to_json({"ok": False, "code": "unreadable", "error": str(e)[:200]})


@mcp.tool()
def mem_import(path: str, select: str = "", include_secrets: bool = False) -> str:
    """把记忆包里的节点「入籍」（写回本库）。默认只读预览：select 为空时只返回可选的
    节点清单，不写任何东西。select 为逗号分隔的原包 node_id（如 "12,15,31"，兼容中文逗号），
    显式选中的节点才会入籍。入籍不沿用原 node_id（记入 payload.origin_id），边只保留两端
    都入籍的。include_secrets=True 时才允许入籍命中强敏感规则的节点。"""
    try:
        result = read_pack(path)
        if result.get("ok") is not True:
            return _to_json(result)

        ids: list[int] = []
        for token in (select or "").replace("，", ",").split(","):
            token = token.strip()
            if token.isdigit():
                ids.append(int(token))

        plan = decide_import(result, select=ids or None, include_secrets=include_secrets)
        manifest = result.get("manifest", {})

        if plan["decision_log"]["mode"] == "preview":
            return _to_json(
                {
                    "ok": True,
                    "mode": "preview",
                    "manifest": manifest,
                    "nodes": summarize_pack(result)["nodes"],
                    "decision_log": plan["decision_log"],
                    "hint": "在 select 里填入要入籍的 node_id（逗号分隔）后重新调用",
                }
            )

        res = apply_import(store, plan)
        return _to_json(
            {
                "ok": res["ok"],
                "mode": "import",
                "manifest": manifest,
                "imported": res["imported"],
                "failed": res["failed"],
                "edges_created": res["edges_created"],
                "edges_failed": res["edges_failed"],
                "skipped": plan["skipped"],
                "decision_log": plan["decision_log"],
            }
        )
    except Exception as e:  # noqa: BLE001 —— 统一转成可读错误对象
        return _to_json({"ok": False, "code": "import_failed", "error": str(e)[:200]})
