# -*- coding: utf-8 -*-
"""
mcp_tools.consolidate_tool —— 容量自动合并工具
=============================================
mem_consolidate：扫描高相似度 memory 节点对，dry_run 预览、apply 才真正合并。
"""

from mcp_tools._common import _to_json, mcp, store  # noqa: E402
from core.consolidator import consolidate  # noqa: E402


@mcp.tool()
def mem_consolidate(dry_run: bool = True, sim_threshold: float = 0.85,
                    max_importance: float = 0.8) -> str:
    """
    容量自动合并：扫描高相似度 memory 节点对。
    dry_run=True（默认，安全）只预览候选不修改数据；
    dry_run=False 才真正合并（新建合并节点、旧节点标 outdated、建 REVISED_BY 边）。
    供 AI 主动触发记忆整理。
    """
    result = consolidate(store, dry_run=dry_run,
                         sim_threshold=sim_threshold,
                         max_importance=max_importance)
    return _to_json(result)
