"""存储协议契合测试
==================

校验 ``server.LocalStore`` 与 ``client.RemoteStore`` 都满足
``protocols.Store`` 协议——这是「core 面向接口」架构的契约保障。

设计要点：
- **不在导入期实例化** LocalStore/RemoteStore（会真开库/真连网）
- 用 ``Store`` 协议做结构检查，不实例化即可验证接口是否漂移
"""

from __future__ import annotations

import inspect

import pytest

from protocols import Store

# ---- 协议本身的完整性 ----


def test_protocol_defines_expected_methods():
    """Store 协议必须包含 core 依赖的那批方法。"""
    required = {
        "get_node",
        "get_edges",
        "iter_payloads",
        "iter_nodes",
        "count_by_type",
        "recent_ids",
        "insert_node",
        "update_payload",
        "update_vector",
        "delete_node",
        "create_edge",
        "search_similar",
    }
    defined = {name for name in dir(Store) if not name.startswith("_")}
    missing = required - defined
    assert not missing, f"Store 协议缺少方法：{sorted(missing)}"


# ---- 实现类的结构契合（不实例化） ----


@pytest.mark.parametrize(
    "module_path,class_name",
    [
        ("server.local_store", "LocalStore"),
        ("client.remote_store", "RemoteStore"),
    ],
)
def test_implementation_declares_protocol_methods(module_path: str, class_name: str):
    """实现类必须声明协议要求的方法（不实例化，避免副作用）。"""
    mod = pytest.importorskip(module_path)
    cls = getattr(mod, class_name)

    required = [
        "get_node",
        "get_edges",
        "iter_payloads",
        "iter_nodes",
        "count_by_type",
        "recent_ids",
        "insert_node",
        "update_payload",
        "update_vector",
        "delete_node",
        "create_edge",
        "search_similar",
    ]
    missing = [m for m in required if not callable(getattr(cls, m, None))]
    assert not missing, f"{class_name} 未实现协议方法：{missing}"


def test_remote_store_satisfies_protocol(monkeypatch):
    """RemoteStore 实例应满足 Store 协议（构造不发网络请求，可直接实例化）。"""
    from client.remote_store import RemoteStore

    rs = RemoteStore(base_url="http://127.0.0.1:9")  # 端口 9 不可达，但构造不连接
    try:
        assert isinstance(rs, Store), "RemoteStore 实例不满足 protocols.Store"
    finally:
        rs.close()


def test_local_store_signature_matches_protocol():
    """LocalStore 的方法签名应与协议一致（按参数名核对核心方法）。"""
    from server.local_store import LocalStore

    for name in ("count_by_type", "recent_ids", "get_node", "delete_node"):
        proto_sig = inspect.signature(getattr(Store, name))
        impl_sig = inspect.signature(getattr(LocalStore, name))
        assert list(proto_sig.parameters) == list(impl_sig.parameters), (
            f"{name} 签名与协议不一致：协议={list(proto_sig.parameters)} 实现={list(impl_sig.parameters)}"
        )


# ---------------------------------------------------------------------------
# 行为契约：同一组断言同时跑 LocalStore 与 RemoteStore
# ---------------------------------------------------------------------------
#
# 结构检查（上面几条）只能证明「方法名在」；真正的偏离都是**行为**上的：
# update_vector 用错 HTTP 方法、recent_ids 走按 importance 排序的端点、
# 404 靠错误消息字符串判定……这些只有把同一个断言对两个实现各跑一遍才能钉住。
#
# 已知差异（同组对拍落地后仍保留的**已文档化偏离**，每种一个偏离钉）：
#   * insert_node 的 embedding：Remote 有意忽略入参向量、服务端按 content 重算
#     —— 本文件 test_insert_node_embedding_recompute_parity 钉住（行为不改）；
#   * RemoteStore.search_similar 的 payload_filter：不做下推，非空时显式抛
#     NotImplementedError —— tests/test_remote_store_contract.py 钉住；
#   * RemoteStore.iter_nodes：无完整结构端点，等价 iter_payloads 语义
#     —— tests/test_remote_store_contract.py 钉住；
#   * RemoteStore.recent_ids：经 /mem/recent 按 created_at 倒序（时间戳缺失
#     按 id 兜底）；本地按 id 倒序。走 /export 会错（importance 降序）
#     —— tests/test_remote_store_contract.py 钉住。
# （以上任一偏离若被「静默收敛」到另一边，对应钉会先红，逼着先改文档与实现。）


@pytest.fixture
def protocol_stores(monkeypatch):
    """LocalStore 与 RemoteStore 指向同一份库、同一份实现。

    RemoteStore 走**真实 ASGI 应用**（进程内、不起服务、不走网络）：用
    ``starlette.testclient.TestClient`` 替换 ``httpx.Client``——它本身就是
    ``httpx.Client`` 子类，因此 RemoteStore 的代码一行不改。不进入它的上下文
    管理器，避免触发应用启动自检（那会真去探 embedding 服务）。

    ``/memory/{id}`` 这类原生端点读 ``main._store``，``/mem/*`` 读
    ``mcp_tools.store``；两者指向同一实例，两条入口才是同一份数据。
    """
    import httpx
    from starlette.testclient import TestClient

    import main as main_mod
    from client.remote_store import RemoteStore
    from mcp_tools import store as shared_store
    from server.local_store import LocalStore

    monkeypatch.setattr(main_mod, "_store", shared_store)
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: TestClient(main_mod.app))

    remote = RemoteStore(base_url="http://testserver")
    try:
        yield LocalStore(shared_store), remote
    finally:
        remote.close()


def _assert_store_contract(s, impl: str) -> None:
    """行为契约本体：两种实现都必须逐条满足。"""
    from conftest import _fake_embed

    content = f"存储契约测试节点（{impl}）"
    nid = s.insert_node(
        {"type": "memory", "content": content, "importance": 0.5, "domain": "contract", "source": "pytest"},
        _fake_embed(content),
    )
    assert isinstance(nid, int), f"{impl}: insert_node 应返回节点 id，得到 {nid!r}"

    node = s.get_node(nid)
    assert node is not None, f"{impl}: 刚写入的节点读不到"
    payload = node["payload"]
    assert payload["content"] == content, f"{impl}: content 不一致：{payload}"
    assert payload.get("domain") == "contract", f"{impl}: domain 丢失：{payload}"

    # 部分更新必须是**合并**语义：未提到的字段保留
    s.update_payload(nid, {"importance": 0.9})
    updated = s.get_node(nid)["payload"]
    assert updated["importance"] == 0.9, f"{impl}: importance 未更新：{updated}"
    assert updated["content"] == content, f"{impl}: 部分更新冲掉了 content：{updated}"
    assert updated.get("domain") == "contract", f"{impl}: 部分更新冲掉了 domain"

    # 全量遍历必须拿到**完整** payload：远程实现曾只拿到 5 个摘要字段，
    # domain / created_at 恒空，凡经它跑 consolidate / stats / review 都是错的却不报错
    from_iter = [pl for nid_, pl in s.iter_payloads() if nid_ == nid]
    assert from_iter, f"{impl}: iter_payloads 未包含刚写入的节点"
    payload_iter = from_iter[0]
    assert payload_iter.get("content") == content, f"{impl}: iter_payloads 的 payload 残缺：{payload_iter}"
    assert payload_iter.get("domain") == "contract", f"{impl}: iter_payloads 丢了 domain（残缺摘要）：{payload_iter}"
    assert "importance" in payload_iter, f"{impl}: iter_payloads 的 payload 残缺：{payload_iter}"

    # 向量整体替换：远程实现曾用 PUT + {"vector": [...]}，在任何服务端版本都不可用
    s.update_vector(nid, [0.25] * 1024)

    # 最近列表必须包含刚写入的节点（远程实现曾走按 importance 排序的 /export）
    assert nid in s.recent_ids(20), f"{impl}: recent_ids 未包含刚写入的节点"

    # 删除后读取必须表现为「不存在」（远程实现曾用错误消息子串判 404）
    s.delete_node(nid)
    assert s.get_node(nid) is None, f"{impl}: 删除后仍能读到节点"


@pytest.mark.parametrize("impl", ["local", "remote"])
def test_store_contract_roundtrip(protocol_stores, impl):
    local, remote = protocol_stores
    _assert_store_contract(local if impl == "local" else remote, impl)


# ---------------------------------------------------------------------------
# search_similar / get_edges / insert_node：两种实现逐位对拍 + 偏离钉（PR 契约）
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_pair(protocol_stores):
    """共享库上的种子节点组（供 Local/Remote 同参对拍）。

    说明：该夹具下 LocalStore 与 RemoteStore 指向**同一份库**（同一
    TriviumStore 实例）——种子只插一次、插入顺序由列表固定，两边读到的 id
    天然对齐，等价于「同一份种子库、同一插入顺序」。种子用小众内容防跨测试
    残留误命中；finally 反序删除还原共享库。
    """
    from conftest import _fake_embed

    local, remote = protocol_stores
    seeds = [
        ("香草苹果派烤制步骤好记", {"type": "memory", "domain": "hero"}),
        ("高铁地铁通勤换乘攻略", {"type": "memory", "domain": "beta"}),
        ("钢琴键盘和弦指法乐谱", {"type": "memory", "domain": "beta"}),
        ("知识条目：香草苹果派的历史起源", {"type": "kb_chunk", "domain": "kb"}),
    ]
    inserted: list[int] = []
    try:
        for content, extra in seeds:
            data = dict(extra, content=content, importance=0.5, source="pytest")
            inserted.append(local.insert_node(data, _fake_embed(content)))
        yield local, remote, inserted
    finally:
        for nid in reversed(inserted):
            local.delete_node(nid)


def _ids(results) -> list:
    return [r["id"] for r in results]


def test_search_similar_topk_order_parity(seeded_pair):
    """同参数下，Local 与 Remote 的 top-k 结果 id 与顺序逐位相等。

    参数对齐说明：Remote 经 /mem/search，内部用 Config.RETRIEVAL_EXPAND_DEPTH
    （默认 0）作 expand_depth 且无法从请求传入；Local 默认走 TriviumStore 的
    expand_depth=1。故 Local 侧显式传同值同深，tier/scope 双方都取「不过滤」。
    """
    from config import Config

    local, remote, inserted = seeded_pair
    q = "香草苹果派"
    loc = list(local.search_similar(q, 5, expand_depth=Config.RETRIEVAL_EXPAND_DEPTH))
    rem = list(remote.search_similar(q, 5))
    assert _ids(loc) == _ids(rem), f"对拍失败 local={_ids(loc)} remote={_ids(rem)}"
    assert len(loc) >= 1, "种子应至少命中一条"
    assert _ids(loc)[0] == inserted[0], "查询主题的种子应排第一"

    for l_, r_ in zip(loc, rem, strict=True):
        assert l_["score"] == pytest.approx(r_["score"], abs=1e-3), f"分数不一致 id={l_['id']}"


def test_search_similar_block_filter_parity(seeded_pair):
    """block 过滤行为一致：只留 block 内节点，且两边逐位相等。"""
    from config import Config

    local, remote, inserted = seeded_pair
    q = "高铁地铁通勤"
    loc = list(local.search_similar(q, 5, expand_depth=Config.RETRIEVAL_EXPAND_DEPTH, block="beta"))
    rem = list(remote.search_similar(q, 5, block="beta"))
    assert _ids(loc) == _ids(rem)
    assert inserted[1] in _ids(rem), "beta 域种子应被 block=beta 命中"
    assert inserted[0] not in _ids(rem), "hero 域种子不应进入 beta 分区块"


def test_search_similar_domain_filter_parity(seeded_pair):
    """domain 过滤语义一致：记忆节点按 node_domain 剔除，kb_chunk 不受影响。"""
    local, remote, inserted = seeded_pair
    q = "钢琴键盘和弦"
    loc = list(local.search_similar(q, 5, expand_depth=0, domain="beta"))
    rem = list(remote.search_similar(q, 5, domain="beta"))
    assert _ids(loc) == _ids(rem)
    assert inserted[2] in _ids(rem), "beta 域记忆种子应被保留"
    assert inserted[0] not in _ids(rem), "hero 域记忆种子应被 domain=beta 过滤"
    assert inserted[3] in _ids(rem), "kb_chunk 应不受 domain 过滤影响（两种实现同条规则）"


def test_search_similar_payload_filter_local_effective(seeded_pair):
    """payload_filter 在 Local 侧真实下推生效（Remote 侧不下推，偏离已钉）。"""
    from conftest import _fake_embed

    local, _remote, inserted = seeded_pair
    content_rec = "香草苹果派烤箱型号记录"
    nid_rec = local.insert_node(
        {"type": "record", "content": content_rec, "domain": "hero"},
        _fake_embed(content_rec),
    )
    try:
        q = "香草苹果派"
        hits = list(local.search_similar(q, 5, expand_depth=0, payload_filter={"type": "record"}))
        assert nid_rec in _ids(hits), "同主题 record 种子应被 payload_filter 命中"
        for r in hits:
            assert (r.get("payload") or {}).get("type") == "record", r
        assert inserted[0] not in _ids(hits), "payload_filter 未生效：memory 种子仍被返回"
        assert inserted[3] not in _ids(hits), "payload_filter 未生效：kb_chunk 种子仍被返回"
    finally:
        local.delete_node(nid_rec)


def test_get_edges_shape_and_value_parity(seeded_pair):
    """get_edges 两种实现都返回属性兼容对象，值逐边相等。

    同时钉 create_edge 的 weight 真生效（LocalStore 曾把 weight 按位置
    塞进 TriviumStore.create_edge 的 content 形参，weight 静默回退默认值）。
    """
    local, remote, inserted = seeded_pair
    a, b = inserted[0], inserted[1]
    local.create_edge(a, b, "RELATED_TO", 0.7)

    edges_l = local.get_edges(a)
    edges_r = remote.get_edges(a)
    assert len(edges_l) == 1 and len(edges_r) == 1
    el, er = edges_l[0], edges_r[0]
    assert el.target_id == er.target_id == b
    assert el.label == er.label == "RELATED_TO"
    assert el.weight == pytest.approx(0.7), f"create_edge 的 weight 未生效：{el.weight}"
    assert er.weight == pytest.approx(0.7)
    # 属性访问是全仓库消费方的契约（mcp_tools/graph.py：edge.target_id 等）
    assert er.label == er.relation  # RemoteEdge 保留 relation 别名


def test_insert_node_embedding_recompute_parity(seeded_pair):
    """两边 insert_node(相同 node_data, 相同向量) 后，检索都能命中该节点。

    Remote 有意忽略入参向量、由服务端按 content 重算（行为不改）；另用
    一份**明显不对**向量经 Remote 插入，检索仍按 content 命中——真 embedder
    换掉或服务端改为信任客户端向量时，本测试会红。
    """
    from conftest import _fake_embed

    local, remote, inserted = seeded_pair
    del inserted
    content_a = "黑胶唱片收藏防潮小窍门"
    vec = _fake_embed(content_a)
    nid_l = local.insert_node({"type": "memory", "content": content_a, "domain": "parity_a"}, vec)
    try:
        nid_r = remote.insert_node({"type": "memory", "content": content_a, "domain": "parity_a"}, vec)
        try:
            # Remote 一份「假向量」插入：服务端若开始信任客户端向量，这里会失联
            content_b = "黑胶唱片避光的正确方式"
            nid_r_bad_vec = remote.insert_node(
                {"type": "memory", "content": content_b, "domain": "parity_a"},
                [0.5] * 1024,
            )
            try:
                q = "黑胶唱片"
                ids_l = _ids(list(local.search_similar(q, 5, expand_depth=0)))
                ids_r = _ids(list(remote.search_similar(q, 5)))
                assert nid_l in ids_l and nid_l in ids_r
                assert nid_r in ids_l and nid_r in ids_r
                assert nid_r_bad_vec in ids_l and nid_r_bad_vec in ids_r
            finally:
                local.delete_node(nid_r_bad_vec)
        finally:
            remote.delete_node(nid_r)
    finally:
        local.delete_node(nid_l)


def test_resolve_conflict_uses_protocol_text_search(seeded_pair):
    """resolve_conflict 注入 protocols.Store 实现（Local/Remote）时走协议文本检索。

    （修 bug 记录）此前非事务分支总是把向量塞进 search_similar 的 query 形参——
    LocalStore 会把向量再 embed、RemoteStore 给服务端发数组，都是协议外破坏。
    TriviumStore 形实现（自带 embed_text）保持原向量直查（历史行为字节不动）。
    """
    from conftest import _fake_embed

    from core.conflict import resolve_conflict

    local, remote, inserted = seeded_pair
    old = inserted[0]
    for store_ in (local, remote):
        content_new = "香草苹果派烤制温度细节"
        nid_new = local.insert_node(
            {"type": "memory", "content": content_new, "domain": "hero"},
            _fake_embed(content_new),
        )
        try:
            result = resolve_conflict(
                store_,
                _fake_embed(content_new),
                nid_new,
                new_payload={"type": "memory", "content": content_new, "domain": "hero"},
            )
            # 自动取代默认关闭：相似只记 related，不标 outdated
            assert old in result["related_ids"], (store_, result)
        finally:
            local.delete_node(nid_new)
