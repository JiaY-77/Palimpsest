"""Palimpsest Dashboard — 独立进程（**可选**，端口 8010）。

## 首选：直接用 REST 自带的面板

面板已融合进 REST：**`http://127.0.0.1:8090/dashboard`**。只跑 REST 就够了，
不用另起进程——这是大众部署的默认姿势。

本脚本保留给「想把面板单独暴露在别的端口 / 单独反代」的场景，行为与 REST 内的
面板完全一致：路由本体是同一份 `server/dashboard_api.build_router`，这里只是换了
一套数据源（经 REST 代理，而不是进程内直读）。

## 架构约束（务必保留）

本服务是**纯客户端**：绝不自己打开数据库。TriviumDB 对库是连接级排他的——面板
一旦自己 `TriviumStore()` 开库，就等于在 REST 之外多出一个写者，两者互相抢库，
表现为间歇性写失败并最终损坏文件组（历史上「库老是坏」的根因正在于此）。
"""

import os
import sys

import httpx
import uvicorn

# 项目根注入：必须在任何本项目 import 之前。
#
# 少了这一步时 `python scripts/dashboard.py`（文档给的启动方式）会直接崩在
# `from config import Config` 上——`ModuleNotFoundError: No module named 'config'`
# ——因为脚本模式下 sys.path[0] 是 scripts/ 而不是仓库根。
#
# 这里**不用** `from _common import ...` 那个统一约定：本模块还会被当作包 import
# （`import scripts.dashboard`，测试就是这么用的），而 `_common` 只在 scripts/ 目录
# 处在 sys.path 上时才可导入。自己算一次项目根对两种模式都成立，且幂等。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI, HTTPException  # noqa: E402

from config import Config  # noqa: E402
from server.dashboard_api import DashboardBackend, build_router  # noqa: E402

app = FastAPI(title="Palimpsest Dashboard")

REST_BASE_URL = (os.getenv("PALIMPSEST_BASE_URL") or f"http://127.0.0.1:{Config.REST_PORT}").rstrip("/")

DASHBOARD_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard.html")


def _rest(method: str, path: str, **kwargs) -> object:
    """代理一次 REST 调用；失败转成 HTTPException，明确说明是后端问题。"""
    url = f"{REST_BASE_URL}{path}"
    try:
        resp = httpx.request(method, url, timeout=30.0, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"主 REST 服务不可达：{url}\n"
                f"底层错误：{exc}\n"
                f"处理：确认 Palimpsest REST 在运行（端口 {Config.REST_PORT}），"
                f"或设置 PALIMPSEST_BASE_URL 指向已运行的实例。"
            ),
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"REST 返回 {resp.status_code}：{resp.text[:300]}")
    if not resp.content:
        return None
    return resp.json()


def _scan_nodes() -> list[dict]:
    """经 REST `/export` 分页拉全库节点（带 payload）。只读。"""
    nodes: list[dict] = []
    page = 1
    while True:
        data = _rest("GET", "/export", params={"page": page, "page_size": 500, "include_payload": "true"})
        if not isinstance(data, dict):
            break
        nodes.extend(data.get("memories") or [])
        total_pages = data.get("total_pages") or 1
        if page >= total_pages:
            break
        page += 1
        if page > 200:  # 防御：异常情况下不死循环
            break
    return nodes


def _stats() -> dict:
    data = _rest("POST", "/mem/stats")
    return data if isinstance(data, dict) else {}


def _search(query: str, limit: int) -> dict:
    data = _rest("POST", "/mem/search", json={"query": query, "top_k": limit})
    return data if isinstance(data, dict) else {}


def _fact_history(node_id: int) -> dict:
    data = _rest("POST", "/mem/fact-history", json={"node_id": node_id})
    return data if isinstance(data, dict) else {}


def _consolidate_preview() -> dict:
    data = _rest("POST", "/mem/consolidate", json={})
    return data if isinstance(data, dict) else {}


app.include_router(
    build_router(
        DashboardBackend(
            scan_nodes=_scan_nodes,
            stats=_stats,
            search=_search,
            fact_history=_fact_history,
            consolidate_preview=_consolidate_preview,
        ),
        page_route="/",
        page_path=DASHBOARD_PATH,
    )
)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=Config.DASHBOARD_PORT)
