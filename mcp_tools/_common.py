# -*- coding: utf-8 -*-
"""
mcp_tools 共享基础设施
=====================
统一放置各 MCP 工具子模块共用的全局依赖（store / mcp 实例 / 序列化与截断工具、
知识库根目录等），避免各子模块重复初始化。
"""

import json
import os
import sys

from mcp.server.fastmcp import FastMCP  # noqa: E402

from config import Config  # noqa: E402
from core.secret_scan import SecretScanError  # noqa: E402
from core.trivium_store import TriviumStore, domain_in_block, node_domain  # noqa: E402

# 确保能 import 项目 core 模块（以项目根为基准；mcp_tools 位于项目根/包目录）
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
# 切换到项目根目录，保证 config 里的相对路径（data/mh_memory.db）解析正确
os.chdir(_SCRIPT_DIR)

# 知识库根目录（环境变量 KNOWLEDGE_DIR 优先）。
# 默认约定为项目根下的 ./knowledge（相对 _SCRIPT_DIR，即项目根）——这是「约定默认知识库位置」，
# 目录不存在时调用方（kb_index / kb_search）应给出清晰提示，而不要静默返回空结果。
KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "") or os.path.normpath(
    os.path.join(_SCRIPT_DIR, "knowledge")
)

# 全局存储实例（TriviumDB：向量 + 图 + 文档）+ MCP 实例（各子模块据此注册工具）
store = TriviumStore()

mcp = FastMCP("palimpsest")


def _to_json(data) -> str:
    """JSON 序列化（保留中文，不转义）"""
    return json.dumps(data, ensure_ascii=False)


def _shorten(text: str, length: int) -> str:
    """截取文本前 length 个字符"""
    text = text or ""
    return text[:length] if len(text) > length else text


def _kb_md_files() -> list:
    """遍历知识库目录，返回所有 .md 文件（绝对路径，排序稳定）"""
    files = []
    for root, _dirs, names in os.walk(KNOWLEDGE_DIR):
        for name in names:
            if name.lower().endswith(".md"):
                files.append(os.path.join(root, name))
    return sorted(files)
