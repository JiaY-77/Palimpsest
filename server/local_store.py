"""LocalStore —— 服务端进程内的本地存储实现
==========================================

``protocols.Store`` 的本地实现：包一层 ``core.trivium_store.TriviumStore``，
把方法签名对齐到协议。

定位：**只有 REST 服务进程会创建它**。它是「唯一写者」原则的落点——
进程内的 REST 通过 ``LocalStore`` 直接读写 TriviumDB；
进程外的 CLI / dashboard / 脚本一律走 ``client.RemoteStore``。

为什么还要包一层？
------------------
``core/`` 里的消费方（consolidator / promoter / stats 等）只依赖
``protocols.Store``。有了 ``LocalStore``，这些消费方在服务端**继续用本地库**
而无需感知协议之外的东西；同时客户端可以注入 ``RemoteStore`` 复用同一批函数。

这不是「多此一举的转发」——它是依赖倒置的支点：core 面向接口，而非具体实现。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from core.trivium_store import TriviumStore


class LocalStore:
    """``protocols.Store`` 的服务端实现，直接操作 TriviumDB。

    参数
    ----
    store:
        可选的既有 ``TriviumStore`` 实例（测试或复用场景）。
        缺省时自行创建一个。
    """

    def __init__(self, store: TriviumStore | None = None) -> None:
        self._store = store if store is not None else TriviumStore()

    @property
    def inner(self) -> TriviumStore:
        """暴露底层 ``TriviumStore``（服务端确有需要直接访问的场景）。"""
        return self._store

    # ---- 读 ----

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        return self._store.get_node(node_id)

    def get_edges(self, node_id: int) -> list:
        return self._store.get_edges(node_id)

    def iter_payloads(self) -> Iterator[tuple[int, dict[str, Any]]]:
        return self._store.iter_payloads()

    def iter_nodes(self) -> Iterator[tuple[int, dict[str, Any]]]:
        return self._store.iter_nodes()

    def count_by_type(self) -> dict[str, int]:
        return self._store.count_by_type()

    def recent_ids(self, limit: int = 20) -> list[int]:
        return self._store.recent_ids(limit)

    # ---- 写 ----

    def insert_node(self, node_data: dict[str, Any], embedding: list[float]) -> int:
        return self._store.insert_node(node_data, embedding)

    def update_payload(self, node_id: int, new_payload: dict[str, Any]) -> None:
        self._store.update_payload(node_id, new_payload)

    def update_vector(self, node_id: int, new_vector: list[float]) -> None:
        self._store.update_vector(node_id, new_vector)

    def delete_node(self, node_id: int) -> None:
        self._store.delete_node(node_id)

    def create_edge(
        self,
        source_id: int,
        target_id: int,
        relation: str = "RELATED_TO",
        weight: float = 1.0,
    ) -> None:
        self._store.create_edge(source_id, target_id, relation, weight)

    # ---- 检索 ----

    def search_similar(
        self,
        query: str,
        top_k: int = 5,
        *,
        domain: str = "",
        block: str = "",
        payload_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Iterable:
        return self._store.search_similar(
            query,
            top_k,
            domain=domain,
            block=block,
            payload_filter=payload_filter,
            **kwargs,
        )


# 协议契合检查放在**测试**里做（tests/test_store_protocol.py），
# 不在导入期实例化——LocalStore() 会真的打开数据库连接，
# 模块级实例化会在 import 时产生副作用（可能撞锁/建空库）。
