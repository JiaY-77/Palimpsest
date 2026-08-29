# -*- coding: utf-8 -*-
"""
核心算法单元测试（纯函数为主，不碰真实库）
=========================================
覆盖 5 组算法，供他人 fork 重构后防回归：
  1. 时间衰减  _days_since_created          （core.trivium_store）
  2. RRF 融合  _rrf_fuse                     （mcp_tools.memory）
  3. 冲突分级  resolve_conflict              （core.conflict，用轻量 fake store）
  4. 图谱邻居  _collect_neighbors            （mcp_tools.graph，用轻量 fake store）
  5. secret_scan 分级                       （core.secret_scan）

隔离原则：纯函数直接测；需要 store 的用「手写假对象」记录调用，不新增依赖。
"""

# ---------------------------------------------------------------------------
# 1. 时间衰减：_days_since_created（纯函数）
# ---------------------------------------------------------------------------
def test_days_since_created_missing_returns_0():
    from core.trivium_store import _days_since_created

    now = 1_700_000_000.0
    assert _days_since_created(None, now) == 0.0


def test_days_since_created_past_30_days():
    from core.trivium_store import _days_since_created

    now = 1_700_000_000.0
    created = now - 30 * 86400.0
    assert abs(_days_since_created(created, now) - 30.0) < 1e-6


def test_days_since_created_future_returns_0():
    from core.trivium_store import _days_since_created

    now = 1_700_000_000.0
    created = now + 5 * 86400.0
    assert _days_since_created(created, now) == 0.0


def test_days_since_created_invalid_returns_0():
    from core.trivium_store import _days_since_created

    now = 1_700_000_000.0
    for bad in ("abc", "not-a-time", "", {}, [], -1, 0):
        assert _days_since_created(bad, now) == 0.0, bad


# ---------------------------------------------------------------------------
# 2. RRF 融合：_rrf_fuse（纯函数）
# ---------------------------------------------------------------------------
def _rrf():
    from mcp_tools.memory import _rrf_fuse
    return _rrf_fuse


def test_rrf_fts_only():
    f = _rrf()
    # 仅 FTS 侧命中：ids [10, 11] 第 0、1 位
    ranked = f([], [10, 11], top_k=10, k=60.0)
    assert len(ranked) == 2
    nid, score, fts_hit, sem_hit = ranked[0]
    assert nid == 10
    assert fts_hit is True and sem_hit is False
    # rank0: 1/60
    assert abs(score - (1.0 / 60.0)) < 1e-9
    assert ranked[1][0] == 11
    assert abs(ranked[1][1] - (1.0 / 61.0)) < 1e-9


def test_rrf_sem_only():
    f = _rrf()
    ranked = f([1, 2, 3], [], top_k=10, k=60.0)
    assert len(ranked) == 3
    assert [r[0] for r in ranked] == [1, 2, 3]
    assert all(r[3] is True and r[2] is False for r in ranked)


def test_rrf_dual_hit_ranks_first():
    f = _rrf()
    # 节点 7 双侧命中（sem rank0 + fts rank0），score = 2/60；节点 9 仅 sem rank1
    ranked = f([7, 9, 8], [7, 5], top_k=10, k=60.0)
    assert ranked[0][0] == 7, ranked
    assert ranked[0][2] is True and ranked[0][3] is True  # fts+sem 都命中
    assert abs(ranked[0][1] - (2.0 / 60.0)) < 1e-9
    # 其余按分降序：9 (1/61) 与 5 (1/61) 并列，8 (1/62) 最后
    nids = [r[0] for r in ranked]
    assert nids[0] == 7
    assert nids[-1] == 8


def test_rrf_top_k_truncation():
    f = _rrf()
    ranked = f([1, 2, 3, 4], [], top_k=2, k=60.0)
    assert len(ranked) == 2
    assert [r[0] for r in ranked] == [1, 2]


def test_rrf_skips_none_ids():
    f = _rrf()
    ranked = f([None, 1], [None, 2], top_k=10, k=60.0)
    nids = [r[0] for r in ranked]
    assert None not in nids
    assert 1 in nids and 2 in nids


# ---------------------------------------------------------------------------
# 3. 冲突检测分级：resolve_conflict（轻量 fake store）
# ---------------------------------------------------------------------------
class _RecordingStore:
    """手写假 store：记录 update_payload / create_edge 调用，search 结果可注入。"""

    def __init__(self, nodes, similar):
        # nodes: {id: payload}；similar: 预设的 search_similar 返回列表
        self.nodes = dict(nodes)
        self.similar = list(similar)
        self.updated = []      # [(id, payload)]
        self.edges = []        # [(source, target, label)]

    def get_node(self, node_id):
        payload = self.nodes.get(node_id)
        return {"id": node_id, "payload": payload} if payload is not None else None

    def search_similar(self, embedding, top_k=3, expand_depth=0, apply_decay=True):
        return self.similar

    def update_payload(self, node_id, new_payload):
        self.updated.append((node_id, dict(new_payload)))
        self.nodes[node_id] = dict(new_payload)

    def create_edge(self, source_id, target_id, relation_type, content="", weight=0.9):
        self.edges.append((source_id, target_id, relation_type))


def _resolve():
    from core.conflict import resolve_conflict
    return resolve_conflict


def test_conflict_high_score_marks_outdated():
    resolve_conflict = _resolve()
    store = _RecordingStore(
        nodes={1: {"type": "memory", "domain": "hero", "status": "active",
                   "content": "旧"}},
        similar=[{"id": 1, "score": 0.9, "payload": {}}],
    )
    new_payload = {"type": "memory", "domain": "hero", "status": "active",
                   "content": "新"}
    store.nodes[99] = new_payload
    result = resolve_conflict(store, [0.5] * 8, 99)
    assert result["outdated_ids"] == [1]
    assert result["related_ids"] == []
    # 旧节点被标 outdated + 建 REVISED_BY 边
    assert store.nodes[1]["status"] == "outdated"
    assert (99, 1, "REVISED_BY") in store.edges


def test_conflict_mid_score_only_related():
    resolve_conflict = _resolve()
    store = _RecordingStore(
        nodes={1: {"type": "memory", "domain": "hero", "status": "active",
                   "content": "旧"}},
        similar=[{"id": 1, "score": 0.6, "payload": {}}],
    )
    new_payload = {"type": "memory", "domain": "hero", "status": "active",
                   "content": "新"}
    store.nodes[99] = new_payload
    result = resolve_conflict(store, [0.5] * 8, 99)
    assert result["related_ids"] == [1]
    assert result["outdated_ids"] == []
    # 不标 outdated、不建边
    assert store.nodes[1]["status"] == "active"
    assert store.edges == []


def test_conflict_low_score_ignored():
    resolve_conflict = _resolve()
    store = _RecordingStore(
        nodes={1: {"type": "memory", "domain": "hero", "status": "active"}},
        similar=[{"id": 1, "score": 0.3, "payload": {}}],
    )
    store.nodes[99] = {"type": "memory", "domain": "hero", "status": "active"}
    result = resolve_conflict(store, [0.5] * 8, 99)
    assert result["outdated_ids"] == [] and result["related_ids"] == []
    assert store.nodes[1]["status"] == "active"
    assert store.edges == []


def test_conflict_type_isolation():
    resolve_conflict = _resolve()
    # 新节点 task，旧节点 memory（跨 type）→ 绝不互标
    store = _RecordingStore(
        nodes={1: {"type": "memory", "domain": "hero", "status": "active"}},
        similar=[{"id": 1, "score": 0.9, "payload": {}}],
    )
    store.nodes[99] = {"type": "task", "domain": "hero", "status": "active"}
    result = resolve_conflict(store, [0.5] * 8, 99)
    assert result["outdated_ids"] == [] and result["related_ids"] == []
    assert store.nodes[1]["status"] == "active"
    assert store.edges == []


def test_conflict_kb_chunk_excluded():
    resolve_conflict = _resolve()
    store = _RecordingStore(
        nodes={1: {"type": "kb_chunk", "domain": "kb", "status": "active"}},
        similar=[{"id": 1, "score": 0.95, "payload": {}}],
    )
    store.nodes[99] = {"type": "memory", "domain": "kb", "status": "active"}
    result = resolve_conflict(store, [0.5] * 8, 99)
    assert result["outdated_ids"] == [] and result["related_ids"] == []
    assert store.nodes[1]["status"] == "active"


def test_conflict_domain_isolation_both_non_general():
    resolve_conflict = _resolve()
    # 双方 domain 都非 general 且不同 → 跨域绝不互标
    store = _RecordingStore(
        nodes={1: {"type": "memory", "domain": "hero", "status": "active"}},
        similar=[{"id": 1, "score": 0.9, "payload": {}}],
    )
    store.nodes[99] = {"type": "memory", "domain": "work", "status": "active"}
    result = resolve_conflict(store, [0.5] * 8, 99)
    assert result["outdated_ids"] == [] and result["related_ids"] == []
    assert store.nodes[1]["status"] == "active"


def test_conflict_general_not_isolated():
    resolve_conflict = _resolve()
    # general 不隔离：新节点 general 可修订 hero 域旧记忆
    store = _RecordingStore(
        nodes={1: {"type": "memory", "domain": "hero", "status": "active"}},
        similar=[{"id": 1, "score": 0.9, "payload": {}}],
    )
    store.nodes[99] = {"type": "memory", "status": "active"}  # 无 domain → general
    result = resolve_conflict(store, [0.5] * 8, 99)
    assert result["outdated_ids"] == [1]


def test_conflict_white_list_type_records_skipped():
    resolve_conflict = _resolve()
    store = _RecordingStore(
        nodes={1: {"type": "record", "domain": "hero", "status": "active"}},
        similar=[{"id": 1, "score": 0.99, "payload": {}}],
    )
    store.nodes[99] = {"type": "record", "domain": "hero", "status": "active"}
    # record 类型不进白名单 → 完全不参与冲突检测（search 不该被调用也无妨）
    result = resolve_conflict(store, [0.5] * 8, 99)
    assert result["outdated_ids"] == [] and result["related_ids"] == []
    assert store.nodes[1]["status"] == "active"


# ---------------------------------------------------------------------------
# 4. 图谱邻居收集：_collect_neighbors（轻量 fake store，monkeypatch 注入）
# ---------------------------------------------------------------------------
class _Edge:
    def __init__(self, target_id, label, weight=1.0):
        self.target_id = target_id
        self.label = label
        self.weight = weight


class _GraphStore:
    """手写假 store：get_edges / get_node，供 _collect_neighbors 使用。"""

    def __init__(self, edges, nodes):
        # edges: {id: [_Edge, ...]}；nodes: {id: {"payload": {...}}}
        self.edges = edges
        self.nodes = nodes

    def get_edges(self, node_id):
        return self.edges.get(node_id, [])

    def get_node(self, node_id):
        n = self.nodes.get(node_id)
        return {"id": node_id, "payload": n.get("payload", {})} if n else None


def _collect(items, limit=5, edges=None, nodes=None):
    from mcp_tools import graph as graph_mod
    original = graph_mod.store
    fake = _GraphStore(edges or {}, nodes or {})
    graph_mod.store = fake
    try:
        return graph_mod._collect_neighbors(items, limit)
    finally:
        graph_mod.store = original  # 不污染全局 store（后续冒烟测试依赖真实 store）


def test_neighbors_dedup_keep_highest_score():
    # 邻居 50 同时被命中节点 1（via=0.9, weight=0.7 → 0.63）和 2（via=1.0, weight=0.7 → 0.7）到达
    items = [{"id": 1, "score": 0.9}, {"id": 2, "score": 1.0}]
    edges = {
        1: [_Edge(50, "related", 0.7)],
        2: [_Edge(50, "related", 0.7)],
    }
    nodes = {50: {"payload": {"type": "memory", "content": "邻居节点内容"}}}
    out = _collect(items, 5, edges, nodes)
    assert len(out) == 1
    assert out[0]["id"] == 50
    # 去重保留最高分：via=1.0 × weight=0.7 = 0.7
    assert abs(out[0]["score"] - 0.7) < 1e-6
    assert out[0]["via_id"] == 2


def test_neighbors_score_is_via_times_weight():
    items = [{"id": 1, "score": 0.5}]
    edges = {1: [_Edge(60, "related", 0.4)]}
    nodes = {60: {"payload": {"type": "memory", "content": "x"}}}
    out = _collect(items, 5, edges, nodes)
    assert abs(out[0]["score"] - (0.5 * 0.4)) < 1e-6
    assert out[0]["via_score"] == 0.5
    assert out[0]["weight"] == 0.4


def test_neighbors_filter_self_loop_and_semantic_zone():
    # 命中节点 1 的自环（target=1）与已在语义区的节点 2 都要被过滤
    items = [{"id": 1, "score": 0.8}, {"id": 2, "score": 0.7}]
    edges = {
        1: [_Edge(1, "related", 0.9), _Edge(2, "related", 0.9), _Edge(70, "related", 0.9)],
    }
    nodes = {70: {"payload": {"type": "memory", "content": "x"}}}
    out = _collect(items, 5, edges, nodes)
    assert [o["id"] for o in out] == [70]
    assert 1 not in [o["id"] for o in out]
    assert 2 not in [o["id"] for o in out]


def test_neighbors_relation_uppercase():
    items = [{"id": 1, "score": 0.8}]
    edges = {1: [_Edge(80, "related_to", 0.9)]}
    nodes = {80: {"payload": {"type": "memory", "content": "x"}}}
    out = _collect(items, 5, edges, nodes)
    assert out[0]["relation"] == "RELATED_TO"


def test_neighbors_empty_label_default_linked():
    items = [{"id": 1, "score": 0.8}]
    edges = {1: [_Edge(80, "", 0.9)]}
    nodes = {80: {"payload": {"type": "memory", "content": "x"}}}
    out = _collect(items, 5, edges, nodes)
    assert out[0]["relation"] == "LINKED"


def test_neighbors_missing_node_skipped():
    items = [{"id": 1, "score": 0.8}]
    edges = {1: [_Edge(90, "related", 0.9)]}
    out = _collect(items, 5, edges, {})  # 节点 90 缺失
    assert out == []


def test_neighbors_limit_applied_and_sorted():
    items = [{"id": 1, "score": 0.9}]
    edges = {
        1: [_Edge(101, "related", 1.0), _Edge(102, "related", 0.9),
            _Edge(103, "related", 0.8)],
    }
    nodes = {101: {"payload": {"type": "memory", "content": "a"}},
             102: {"payload": {"type": "memory", "content": "b"}},
             103: {"payload": {"type": "memory", "content": "c"}}}
    out = _collect(items, 2, edges, nodes)
    assert len(out) == 2
    # 按 score 降序：0.9, 0.81, 0.72 → 取前二
    assert out[0]["id"] == 101
    assert out[1]["id"] == 102


# ---------------------------------------------------------------------------
# 5. secret_scan 强/弱分级
# ---------------------------------------------------------------------------
def test_secret_scan_strong_openai_key():
    from core.secret_scan import scan_secret, scan_secret_classified

    text = "api key 是 sk-abcdefghijklmnopqrstuvwxyz123 请勿外泄"
    assert "openai_key" in scan_secret(text)
    cls = scan_secret_classified(text)
    assert "openai_key" in cls["strong"]
    assert cls["weak"] == []


def test_secret_scan_strong_private_key():
    from core.secret_scan import scan_secret_classified

    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA"
    cls = scan_secret_classified(text)
    assert any("private_key" in r for r in cls["strong"])


def test_secret_scan_strong_bearer():
    from core.secret_scan import scan_secret_classified

    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    cls = scan_secret_classified(text)
    assert "bearer" in cls["strong"]


def test_secret_scan_strong_github_token():
    from core.secret_scan import scan_secret_classified

    text = "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghij"
    cls = scan_secret_classified(text)
    assert "github_token" in cls["strong"]


def test_secret_scan_weak_id_card():
    from core.secret_scan import scan_secret_classified

    text = "身份证号 11010119900307749X"
    cls = scan_secret_classified(text)
    assert cls["strong"] == []
    assert "id_card" in cls["weak"]


def test_secret_scan_weak_phone():
    from core.secret_scan import scan_secret_classified

    text = "联系电话 13800138000"
    cls = scan_secret_classified(text)
    assert cls["strong"] == []
    assert "phone" in cls["weak"]


def test_secret_scan_clean():
    from core.secret_scan import scan_secret, scan_secret_classified

    text = "这是一段完全干净的记忆内容，没有任何敏感信息。"
    assert scan_secret(text) == []
    cls = scan_secret_classified(text)
    assert cls["strong"] == [] and cls["weak"] == []


def test_scan_secret_classified_empty_text():
    from core.secret_scan import scan_secret_classified

    cls = scan_secret_classified("")
    assert cls == {"strong": [], "weak": []}
