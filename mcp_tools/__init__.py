# -*- coding: utf-8 -*-
"""
mcp_tools —— MCP 工具包
======================
将原 mcp_server.py 单体拆分为职责单一的子模块：
  memory.py   记忆读写与查询（mem_* 系列 + _mem_search_impl）
  kb.py       知识库（kb_index / kb_search）
  graph.py    图谱（graph_neighbors / mem_link / _collect_neighbors / _edge_exists）
  routing.py  任务路由（router_query / _extract_recommendation）
  _common.py  共享基础设施（store / mcp / 序列化工具 / 知识库根目录）
本包只导出公共工具符号，供 main.py / palimpsest_cli 等外部直接复用；
私有实现符号（_.* 前缀）由各子模块内部直连导入，不经过本包入口。
包被 import 时即触发全部 @mcp.tool() 注册（FastMCP 装饰器在 import 时执行）。
"""

from mcp_tools._common import mcp, store  # noqa: E402
from mcp_tools.consolidate_tool import mem_consolidate  # noqa: E402
from mcp_tools.graph import graph_neighbors, mem_communities, mem_link  # noqa: E402
from mcp_tools.kb import kb_index, kb_search  # noqa: E402
from mcp_tools.memory import (  # noqa: E402
    mem_get_full, mem_hybrid_search, mem_ingest, mem_recent, mem_retrieve,
    mem_review, mem_search, mem_version_history,
)
from mcp_tools.routing import router_query  # noqa: E402

__all__ = [
    "store", "mcp",
    "mem_retrieve", "mem_get_full", "mem_ingest", "mem_recent", "mem_review",
    "mem_version_history", "mem_search", "mem_hybrid_search", "mem_consolidate",
    "kb_index", "kb_search",
    "graph_neighbors", "mem_communities", "mem_link",
    "router_query",
]
