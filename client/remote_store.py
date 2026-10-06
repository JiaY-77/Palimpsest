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


class RemoteStoreNotFoundError(RemoteStoreError):
    """目标资源不存在（REST 返回 404）。

    单独成类而不是让调用方从消息串里抠状态码：404 是「可能正常」的语义
    （如 ``get_node`` 查不存在的 id 返回 ``None``），必须可被精确捕获。
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
        self.base_url = (base_url or os.getenv("PALIMPSEST_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    # ---- 内部 ----

    def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        """发一个请求并返回解析后的 JSON；失败抛 ``RemoteStoreError``
        （404 单独抛 ``RemoteStoreNotFoundError``，是其子类）。
        """
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteStoreError(
                f"无法访问 Palimpsest REST：{self.base_url}{path}\n"
                f"底层错误：{exc}\n"
                f"处理：确认 REST 服务在运行（uvicorn main:app --port 8090），"
                f"或设置 PALIMPSEST_BASE_URL 指向已运行的实例。"
            ) from exc
        # 404 必须先于 >=400 分支判定，否则会被统一吞掉、调用方无法精确捕获
        if resp.status_code == 404:
            raise RemoteStoreNotFoundError(f"REST 返回 404：{method} {self.base_url}{path}\n响应：{resp.text[:500]}")
        if resp.status_code >= 400:
            raise RemoteStoreError(
                f"REST 返回 {resp.status_code}：{method} {self.base_url}{path}\n响应：{resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ---- 读 ----

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """取单个节点。REST ``GET /memory/{id}`` 返回 404 时返回 ``None``。"""
        try:
            return self._req("GET", f"/memory/{node_id}")
        except RemoteStoreNotFoundError:
            return None

    def get_edges(self, node_id: int) -> list:
        """取某节点邻边——经 ``POST /graph/neighbors``。

        REST 返回结构为 ``{"node_id":…, "relations":[…]}"``（键名 ``relations``，
        非 ``neighbors``/``edges``）——按实际响应解析。
        """
        data = self._req("POST", "/graph/neighbors", json={"node_id": node_id, "depth": 1})
        if isinstance(data, dict):
            return data.get("relations") or data.get("neighbors") or data.get("edges") or []
        return data or []

    def iter_payloads(self) -> Iterator[tuple[int, dict[str, Any]]]:
        """遍历全部节点 ``(node_id, payload)``。

        经 ``GET /export`` 分页拉取，避免一次性载入全库；
        ``include_payload=true`` 要求服务端附带完整 payload——没有它，
        ``domain`` / ``created_at`` 这类字段会恒为空，凡经 REST 走
        consolidate / compute_stats / mem_review 的结果都是错的却不报错。
        服务端漏了这个字段属于契约违约，这里显式抛错而不是退回残缺摘要。

        注意：确有全量遍历需求时才用；日常统计请优先 ``count_by_type`` /
        ``recent_ids``，它们各自只走一个端点。
        """
        page = 1
        page_size = 500
        while True:
            data = self._req(
                "GET",
                "/export",
                params={"page": page, "page_size": page_size, "include_payload": "true"},
            )
            items = []
            if isinstance(data, dict):
                items = data.get("memories", data.get("items", [])) or []
            elif isinstance(data, list):
                items = data
            if not items:
                return
            for item in items:
                if not isinstance(item, dict):
                    raise RemoteStoreError(f"/export 返回了非对象条目：{item!r}")
                nid = item.get("id")
                if nid is None:
                    continue
                if "payload" not in item:
                    raise RemoteStoreError(
                        "GET /export 未返回 payload 字段（include_payload=true 被忽略）"
                        "——契约违约：残缺摘要会让 domain/created_at 静默变空。"
                    )
                yield int(nid), item["payload"]
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
        """最近节点 id —— 经 ``POST /mem/recent``（body ``{"limit": limit}``）。

        服务端 ``mcp_tools.memory.mem_recent`` 按 ``created_at`` 倒序（时间戳缺失
        时按 id 倒序兜底）返回 ``{"results": [{id, type, content, ...}, ...],
        "total": N}``，``results`` 已截到 limit——按实际响应解析，只抽 id。
        不走 ``GET /export``：export 内部按 importance 降序，是「最重要前 N」
        而非「最近 N」，语义不符。
        """
        data = self._req("POST", "/mem/recent", json={"limit": limit})
        items = []
        if isinstance(data, dict):
            items = data.get("results") or []
        elif isinstance(data, list):
            items = data
        return [int(it["id"]) for it in items if isinstance(it, dict) and "id" in it]

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
        """整体替换向量 —— 经 ``PATCH /memory/{id}/vector``。

        服务端形参是顶层 ``vector: list[float]``（FastAPI 直接把 body 解析成
        数组），故 body 直接是向量数组，不能再包 ``{"vector": ...}``。
        """
        self._req("PATCH", f"/memory/{node_id}/vector", json=new_vector)

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

        ``payload_filter`` 服务端**尚不支持**：``main.MemSearchRequest`` 与
        ``mcp_tools.memory.mem_search`` 都没有该字段（即便塞进 body 也会被
        FastAPI 模型静默丢弃），故收到非空过滤时直接抛 ``NotImplementedError``——
        静默忽略会让调用方以为过滤生效了。
        """
        if payload_filter:
            raise NotImplementedError(
                "RemoteStore.search_similar 暂不支持 payload_filter 下推，请先补服务端支持或改用 LocalStore"
            )
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if domain:
            body["domain"] = domain
        if block:
            body["block"] = block
        # 透传其余与 REST 端点同名的参数（scope / tier / include_neighbors ...）
        for key in ("scope", "domain_bias", "include_neighbors", "include_outdated", "tier", "domain_boost"):
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
