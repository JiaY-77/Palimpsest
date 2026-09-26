"""RemoteStore —— 通过 REST 访问记忆库的客户端
============================================

实现 ``protocols.Store``，接口与 ``core.trivium_store.TriviumStore`` 对齐，
但内部**不发直接打开数据库**，而是走 REST HTTP。

这是「一份数据、一个写者、多条接入方式」架构的客户端半边：

- 服务端（REST 进程）``LocalStore`` —— 唯一持有库连接的写者
- 客户端（CLI / dashboard / 脚本）``RemoteStore`` —— 一律经 REST 读写

不覆盖的能力
------------
本类**只覆盖 core 需要的最小方法集**，不逐一镜像 ``TriviumStore``。
复合操作（合并 consolidate、归档 task_archive、全量重建索引等）应由服务端
暴露**业务级端点**，客户端一次调用即可——不要让客户端逐方法发 HTTP，
否则会破坏事务边界并产生 N+1 请求。见决策 3（``plans/gateway-fusion-strategy.md``）。

尚未有对应 REST 端点的方法会抛出 ``NotImplementedError`` 并说明原因，
而不是静默返回空值——静默降级是历史上库损坏的起点。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from typing import Any

import httpx

# 默认 REST 地址，与 config.Config.REST_PORT 对齐。
_DEFAULT_BASE_URL = "http://127.0.0.1:8090"


class RemoteStoreError(RuntimeError):
    """REST 访问失败（不可达 / 非 2xx / 响应格式不符）。

    异常消息含目标 URL 与底层错误，便于定位是「服务没起」还是「接口出错」。
    """


class RemoteStore:
    """``protocols.Store`` 的 HTTP 实现。

    参数
    ----
    base_url:
        REST 服务地址。默认取环境变量 ``PALIMPSEST_BASE_URL``，
        再退回 ``http://127.0.0.1:8090``。
    timeout:
        单请求超时（秒）。本地回环调用，默认 10s 足够。
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("PALIMPSEST_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    # ---- 内部 ----

    def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        """发一个请求并返回解析后的 JSON；失败抛 ``RemoteStoreError``。"""
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteStoreError(
                f"无法访问 Palimpsest REST：{self.base_url}{path}\n"
                f"底层错误：{exc}\n"
                f"处理：确认 REST 服务在运行（uvicorn main:app --port 8090），"
                f"或设置 PALIMPSEST_BASE_URL 指向已运行的实例。"
            ) from exc
        if resp.status_code >= 400:
            raise RemoteStoreError(
                f"REST 返回 {resp.status_code}：{method} {self.base_url}{path}\n"
                f"响应：{resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ---- 读 ----

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """取单个节点。REST ``GET /memory/{id}`` 返回 404 时返回 ``None``。"""
        try:
            return self._req("GET", f"/memory/{node_id}")
        except RemoteStoreError as exc:
            if "返回 404" in str(exc):
                return None
            raise

    def get_edges(self, node_id: int) -> list:
        """取某节点邻边——经 ``POST /graph/neighbors``。

        REST 返回结构为 ``{"node_id":…, "relations":[…]}"``（键名 ``relations``，
        非 ``neighbors``/``edges``）——按实际响应解析。
        """
        data = self._req(
            "POST", "/graph/neighbors", json={"node_id": node_id, "depth": 1}
        )
        if isinstance(data, dict):
            return (
                data.get("relations")
                or data.get("neighbors")
                or data.get("edges")
                or []
            )
        return data or []

    def iter_payloads(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """遍历全部节点 ``(node_id, payload)``。

        经 ``GET /export`` 分页拉取，避免一次性载入全库。

        注意：确有全量遍历需求时才用；日常统计请优先 ``count_by_type`` /
        ``recent_ids``，它们各自只走一个端点。
        """
        page = 1
        page_size = 500
        while True:
            data = self._req(
                "GET", "/export", params={"page": page, "page_size": page_size}
            )
            items = []
            if isinstance(data, dict):
                items = data.get("memories", data.get("items", [])) or []
            elif isinstance(data, list):
                items = data
            if not items:
                return
            for item in items:
                nid = item.get("id") if isinstance(item, dict) else None
                payload = item.get("payload", item) if isinstance(item, dict) else {}
                if nid is not None:
                    yield int(nid), payload
            if len(items) < page_size:
                return
            page += 1

    def iter_nodes(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """遍历全部节点 ``(node_id, node)``。

        REST 没有「含完整节点结构」的批量端点，此处退回 ``iter_payloads``
        的等价语义（payload 即节点主体）。如后续需要完整结构，应由服务端
        增加批量端点，而不是在客户端拼装。
        """
        yield from self.iter_payloads()

    def count_by_type(self) -> dict[str, int]:
        """按类型统计——经 ``POST /mem/stats``。"""
        data = self._req("POST", "/mem/stats")
        if isinstance(data, dict):
            for key in ("by_type", "type_counts", "counts"):
                if isinstance(data.get(key), dict):
                    return data[key]
            # stats 可能嵌套在 totals 下
            totals = data.get("totals")
            if isinstance(totals, dict) and isinstance(totals.get("by_type"), dict):
                return totals["by_type"]
        return {}

    def recent_ids(self, limit: int = 20) -> list[int]:
        """最近节点 id —— 由 ``GET /export`` 首页（按 id 倒序）取前 limit 个。

        服务端没有专门的 /recent 端点，此处用 export 首页近似；
        若将来服务端补了 narrow 端点，改走它更好。
        """
        data = self._req("GET", "/export", params={"page": 1, "page_size": limit})
        items = []
        if isinstance(data, dict):
            items = data.get("memories", data.get("items", [])) or []
        elif isinstance(data, list):
            items = data
        ids = [int(it["id"]) for it in items if isinstance(it, dict) and "id" in it]
        ids.sort(reverse=True)
        return ids[:limit]

    # ---- 写 ----

    def insert_node(self, node_data: dict[str, Any], embedding: list[float]) -> int:
        """插入节点 —— 经 ``POST /mem/ingest``。

        REST 的 ingest 由服务端自行生成向量（内容优先），``embedding`` 参数
        在远程路径下不使用——保留它是为了接口一致。
        """
        payload = {
            "content": node_data.get("content", ""),
            "type": node_data.get("type", "memory"),
            "importance": node_data.get("importance", 0.5),
            "domain": node_data.get("domain", ""),
            "source": node_data.get("source", ""),
        }
        data = self._req("POST", "/mem/ingest", json=payload)
        if isinstance(data, dict) and "node_id" in data:
            return int(data["node_id"])
        raise RemoteStoreError(f"ingest 响应缺少 node_id：{data!r}")

    def update_payload(self, node_id: int, new_payload: dict[str, Any]) -> None:
        """整体替换 payload —— 经 ``PUT /memory/{id}``。"""
        self._req("PUT", f"/memory/{node_id}", json=new_payload)

    def update_vector(self, node_id: int, new_vector: list[float]) -> None:
        """整体替换向量 —— 经 ``PUT /memory/{id}/vector``。

        注意：REST 端点路径需在服务端确认；若不存在会抛 RemoteStoreError。
        """
        self._req("PUT", f"/memory/{node_id}/vector", json={"vector": new_vector})

    def delete_node(self, node_id: int) -> None:
        """删除节点 —— 经 ``DELETE /memory/{id}``。"""
        self._req("DELETE", f"/memory/{node_id}")

    def create_edge(
        self,
        source_id: int,
        target_id: int,
        relation: str = "RELATED_TO",
        weight: float = 1.0,
    ) -> None:
        """建边 —— 经 ``POST /mem/link``。"""
        self._req(
            "POST",
            "/mem/link",
            json={
                "source_id": source_id,
                "target_id": target_id,
                "relation": relation,
                "weight": weight,
            },
        )

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
        """语义检索 —— 经 ``POST /mem/search``。

        ``payload_filter`` 用于把类型过滤下推到检索层。REST 的 search 端点
        接受 ``scope`` 等参数；若需要按类型精确过滤，应扩展该端点
        （服务端检索层原生支持 payload_filter）。
        """
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if domain:
            body["domain"] = domain
        if block:
            body["block"] = block
        # 透传其余与 REST 端点同名的参数（scope / tier / include_neighbors ...）
        for key in ("scope", "domain_bias", "include_neighbors",
                    "include_outdated", "tier", "domain_boost"):
            if key in kwargs:
                body[key] = kwargs[key]
        data = self._req("POST", "/mem/search", json=body)
        if isinstance(data, dict):
            return data.get("results", [])
        return data or []

    # ---- 收尾 ----

    def close(self) -> None:
        """关闭底层 HTTP 连接池。"""
        self._client.close()

    def __enter__(self) -> RemoteStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
