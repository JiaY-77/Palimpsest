"""存储协议（Store Protocol）
===========================

本模块**只定义接口**，不含任何实现——它是「一份数据、一个写者、多条接入方式」
架构的依赖倒置点。

背景
----
Palimpsest 的库由多个入口访问：REST 服务、CLI、dashboard、各类脚本。
历史上各入口**各自直接打开数据库文件**，而 TriviumDB 对库是连接级排他的，
第二个进程连打开都会失败——表现为间歇性写失败，最终导致文件组损坏。

根治办法：**全库唯一写者 = REST 进程**，其他入口一律通过 REST 访问。

依赖方向（关键——不得出现环）
----------------------------
::

    server( LocalStore ) ──┐
                            ├──> core ──> protocols.Store
    client( RemoteStore ) ──┘         ▲
              │                        │
              └── HTTP ──> server ─────┘

- ``core/`` 只依赖本模块的 ``Store`` 协议，**不 import server / client**
- ``server.LocalStore`` 与 ``client.RemoteStore`` 都实现 ``Store``
- REST 进程只创建 ``LocalStore``；CLI / dashboard / 脚本只创建 ``RemoteStore``

本模块不得 import ``core.trivium_store``、``server.*``、``client.*``，
以保持依赖图无环。

用法
----
core 函数继续接收 ``store`` 参数，但类型标注从具体实现改为 ``Store``::

    def consolidate(store: Store, *, dry_run: bool = False) -> dict: ...

由调用方决定注入哪个实现：

- REST 内部：``consolidate(LocalStore(...))``
- CLI / dashboard：``consolidate(RemoteStore(...))``

设计取舍
--------
本协议**故意只覆盖 core 需要的最小方法集**，不追求逐一镜像
``TriviumStore`` 的全部公开方法。复合操作（如「合并」）应作为**业务级端点**
暴露，而不是让客户端逐个调用底层方法——否则一次复合操作会退化成多次 HTTP
往返，破坏事务边界。详见 ``plans/gateway-fusion-strategy.md`` 决策 3。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """存储访问协议——core 层依赖的唯一抽象。

    实现者：

    - ``server.local_store.LocalStore``：服务端进程内使用，直接操作 TriviumDB
    - ``client.remote_store.RemoteStore``：客户端使用，内部走 REST HTTP

    方法语义与 ``core.trivium_store.TriviumStore`` 对应方法一致；
    异常语义也保持一致——具体实现应让底层错误自然传播，
    不要静默降级（静默降级正是历史上库损坏的起点）。
    """

    # ---- 读 ----

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """取单个节点（含 payload）。不存在返回 ``None``。"""
        ...

    def get_edges(self, node_id: int) -> list:
        """取某节点的出边列表。"""
        ...

    def iter_payloads(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """遍历全部节点的 ``(node_id, payload)``。

        注意：全量遍历。既有调用点应优先改用 ``count_by_type`` / ``recent_ids``
        等窄接口；确需全量时再使用。
        """
        ...

    def iter_nodes(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """遍历全部节点的 ``(node_id, node)``（含向量之外的完整结构）。"""
        ...

    def count_by_type(self) -> dict[str, int]:
        """按 ``payload.type`` 统计节点数。"""
        ...

    def recent_ids(self, limit: int = 20) -> list[int]:
        """返回最近（按 id 倒序）的 ``limit`` 个节点 id。"""
        ...

    # ---- 写 ----

    def insert_node(self, node_data: dict[str, Any], embedding: list[float]) -> int:
        """插入一个节点，返回新 ``node_id``。"""
        ...

    def update_payload(self, node_id: int, new_payload: dict[str, Any]) -> None:
        """整体替换节点的 payload。"""
        ...

    def update_vector(self, node_id: int, new_vector: list[float]) -> None:
        """整体替换节点的向量。"""
        ...

    def delete_node(self, node_id: int) -> None:
        """删除节点。"""
        ...

    def create_edge(
        self,
        source_id: int,
        target_id: int,
        relation: str = "RELATED_TO",
        weight: float = 1.0,
    ) -> None:
        """建立一条有向边。"""
        ...

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
        """语义检索。

        参数与 ``TriviumStore.search_similar`` 对齐；``payload_filter`` 用于把
        类型过滤下推到检索层（避免全局 top_k 后再过滤导致目标被挤出窗口）。
        """
        ...
