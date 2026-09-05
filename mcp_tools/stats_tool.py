# -*- coding: utf-8 -*-
"""
mcp_tools.stats_tool —— 库级盘点统计工具（mem_stats）
=====================================================
mem_stats：统一盘点「库里有什么 / 领域密度 / 图谱状态」。
复用 core.stats.compute_stats 单次全遍历结果，供 MCP / REST / CLI 三入口共用。
"""

from core.stats import compute_stats  # noqa: E402
from mcp_tools._common import _to_json, mcp, store  # noqa: E402


@mcp.tool()
def mem_stats() -> str:
    """
    库级盘点统计：回答「库里有什么 / 领域密度 / 图谱状态」。
    返回分节 JSON：
      - totals：节点总数 / active / outdated / 按 type 分布 / 按 domain 分布
      - kinds：novel_chunk 按 kind 统计（character/setting/relation/overview）
      - importance：按区间计数（<0.4 / 0.4-0.6 / 0.6-0.8 / >=0.8）
      - time：按 created_at 月份分布
      - graph：有边节点数 / 总边数 / 边 label 分布 top10 / 平均出度 / hit_count 统计
    只读操作，不修改任何节点数据。
    """
    return _to_json(compute_stats(store))
