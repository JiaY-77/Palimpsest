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
# 已知差异（不在同组断言内，另行跟踪）：
#   * search_similar：协议声明首参是 query 字符串，TriviumStore 期望的是 embedding
#     向量（LocalStore 原样下传），远程实现则把 query 交给服务端重算向量——
#     语义未对齐，故不做同组断言；
#   * get_edges：本地实现返回 Edge 对象，远程实现返回序列化后的字典。


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
