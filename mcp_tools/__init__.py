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
本包向 mcp_server.py 汇聚全部工具与共享依赖，供其统一注册与 re-export。
"""

from mcp_tools._common import (  # noqa: E402
    KNOWLEDGE_DIR, _kb_md_files, _shorten, _to_json, mcp, store,
)
from mcp_tools.graph import (  # noqa: E402
    _BIDIRECTIONAL_RELATIONS, _collect_neighbors, _edge_exists, graph_neighbors,
    mem_link,
)
from mcp_tools.kb import kb_index, kb_search  # noqa: E402
from mcp_tools.memory import (  # noqa: E402
    _l1_sniff, _mem_search_impl, _parse_version_content, mem_get_full,
    mem_ingest, mem_recent, mem_retrieve, mem_review, mem_search,
    mem_version_history,
)
from mcp_tools.routing import _extract_recommendation, router_query  # noqa: E402

__all__ = [
    "store", "mcp", "_to_json", "_shorten", "_kb_md_files", "KNOWLEDGE_DIR",
    "mem_retrieve", "mem_get_full", "mem_ingest", "mem_recent", "mem_review",
    "mem_version_history", "mem_search", "_mem_search_impl", "_l1_sniff",
    "_parse_version_content",
    "kb_index", "kb_search",
    "graph_neighbors", "mem_link", "_collect_neighbors", "_edge_exists",
    "_BIDIRECTIONAL_RELATIONS",
    "router_query", "_extract_recommendation",
]
