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
        "get_node", "get_edges", "iter_payloads", "iter_nodes",
        "count_by_type", "recent_ids",
        "insert_node", "update_payload", "update_vector", "delete_node",
        "create_edge", "search_similar",
    }
    defined = {name for name in dir(Store) if not name.startswith("_")}
    missing = required - defined
    assert not missing, f"Store 协议缺少方法：{sorted(missing)}"


# ---- 实现类的结构契合（不实例化） ----

@pytest.mark.parametrize("module_path,class_name", [
    ("server.local_store", "LocalStore"),
    ("client.remote_store", "RemoteStore"),
])
def test_implementation_declares_protocol_methods(module_path: str, class_name: str):
    """实现类必须声明协议要求的方法（不实例化，避免副作用）。"""
    mod = pytest.importorskip(module_path)
    cls = getattr(mod, class_name)

    required = [
        "get_node", "get_edges", "iter_payloads", "iter_nodes",
        "count_by_type", "recent_ids",
        "insert_node", "update_payload", "update_vector", "delete_node",
        "create_edge", "search_similar",
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
            f"{name} 签名与协议不一致："
            f"协议={list(proto_sig.parameters)} 实现={list(impl_sig.parameters)}"
        )
