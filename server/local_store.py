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

from collections.abc import Iterator
from typing import Any

from core.trivium_store import TriviumStore, node_domain


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
        # 显式按关键字转发：TriviumStore.create_edge 的第 4 位是 content（历史
        # 遗留、db.link 用不到的死参），按位置传 weight 会把 float 塞进 content、
        # weight 静默回退默认值 0.9——之前 weight 从未生效过。
        self._store.create_edge(source_id, target_id, relation_type=relation, weight=weight)

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
    ) -> list[dict[str, Any]]:
        """语义检索：协议首参是 query **字符串**，本实现先 embed 成向量再转发。

        （修 bug 记录）原实现把 query 原样转发给
        ``TriviumStore.search_similar(query_embedding: list[float], ...)``——
        真调用会把字符串当向量喂进去（此前无人调用才没炸）。

        - 协议外参数（``expand_depth`` / ``apply_decay`` / ``include_outdated``）
          经 ``**kwargs`` 原样透传给 ``TriviumStore.search_similar``，
          ``apply_decay`` 等不丢失。
        - ``embed_text`` 的异常（``EmbeddingUnavailableError``）不吞、自然传播，
          与 ``TriviumStore`` 其它 embed 路径一致。
        - ``domain``：``TriviumStore.search_similar`` 没有该参数；远程语义由
          REST 检索视图（``mcp_tools.memory._mem_search_impl``）实现——
          「宽召回 → 过滤 → 截断」。为让 Local/Remote 对同一协议参数语义一致，
          这里用与远程**同一个** ``node_domain`` 函数复刻同一条规则：
          非 ``kb_chunk`` 且 ``node_domain(payload) != domain`` 的候选剔除。
          先宽召回 ``max(top_k * 3, 30)`` 再过滤截断，对齐远程的召回宽度，
          避免「过滤在截断之后」导致候选被非本域结果挤出窗口。
        """
        embedding = self._store.embed_text(query)
        domain_norm = (domain or "").strip().lower()
        wide_k = max(top_k * 3, 30) if domain_norm else top_k
        results = list(
            self._store.search_similar(
                embedding,
                wide_k,
                block=block,
                payload_filter=payload_filter,
                **kwargs,
            )
        )
        if domain_norm:
            results = [
                r
                for r in results
                if (r.get("payload") or {}).get("type") == "kb_chunk"
                or node_domain(r.get("payload") or {}) == domain_norm
            ]
            results = results[:top_k]
        return results


# 协议契合检查放在**测试**里做（tests/test_store_protocol.py），
# 不在导入期实例化——LocalStore() 会真的打开数据库连接，
# 模块级实例化会在 import 时产生副作用（可能撞锁/建空库）。
