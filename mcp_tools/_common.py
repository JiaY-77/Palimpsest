"""
mcp_tools 共享基础设施
=====================
统一放置各 MCP 工具子模块共用的全局依赖（store / mcp 实例 / 序列化与截断工具、
知识库根目录等），避免各子模块重复初始化。
"""

import json
import os
import sys

from mcp.server.fastmcp import FastMCP

from core.trivium_store import TriviumStore

# 确保能 import 项目 core 模块（以项目根为基准；mcp_tools 位于项目根/包目录）。
# 作为 pip 包安装后 core/config 本身即可按包名找到，此处注入只在直接跑仓库内脚本时
# 才起作用；不再 os.chdir（config 已改为绝对路径解析，无需也不应改动进程工作目录）。
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# 知识库根目录（环境变量 KNOWLEDGE_DIR 优先）。
# 默认约定为项目根下的 ./knowledge（相对 _SCRIPT_DIR，即项目根）——这是「约定默认知识库位置」，
# 目录不存在时调用方（kb_index / kb_search）应给出清晰提示，而不要静默返回空结果。
KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "") or os.path.normpath(os.path.join(_SCRIPT_DIR, "knowledge"))

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


# 句边界字符（excerpt 截断点优先级最高的边界）；换行单独处理（截断点留在行尾）
_SENT_END_CHARS = "。！？!?."
# 词边界字符（句边界缺失时的退化边界，截断点停在词尾、不留分隔符）
_WORD_SEP_CHARS = " \t，,、"


def _excerpt(text: str, length: int) -> str:
    """逐字截取检索 excerpt：不生成摘要，只挑截断点（verbatim，不改写原文）。

    目标长度 length（config.SEARCH_EXCERPT_CHARS，默认 400）：
      1. 截断点优先落在句边界（。！？.? 或换行），句读本身留在 excerpt 内
         （换行除外——截断停在行尾、不带尾随空白）；
      2. 最强句边界落在窗口前半段（< length/2）或缺失时，退化为词边界
         （空格/逗号，截断停在词尾）；
      3. 仍无 → 硬截断（与 _shorten 同形：原文前 length 个字符）。
    英文小数点两侧均为数字（如 3.14）不当句点。输出恒为原文前缀。
    """
    text = text or ""
    n = int(length)
    if n <= 0:
        return ""
    if len(text) <= n:
        return text
    window = text[:n]
    # 前半段门槛：最强边界比这还靠前时返回的 excerpt 会短得没有信息量，
    # 此时按「更长的降级边界」退一档（句边界 → 词边界 → 硬截断）
    floor = n // 2
    # 句边界：从窗口末尾往前找最后一个句界
    for i in range(n - 1, -1, -1):
        ch = window[i]
        if ch == "\n":
            if i >= floor:
                return window[:i]
            break
        if ch in _SENT_END_CHARS:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if ch == "." and window[i - 1].isdigit() and nxt.isdigit():
                continue  # 小数点（如 3.14）不当句点
            if i + 1 >= floor:
                return window[: i + 1]
            break
    # 词边界：最后一个空格/逗号处截断（停在词尾）
    for i in range(n - 1, -1, -1):
        if window[i] in _WORD_SEP_CHARS:
            if i >= floor:
                return window[:i]
            break
    return window  # 硬截断


def _kb_md_files() -> list:
    """遍历知识库目录，返回所有 .md 文件（绝对路径，排序稳定）"""
    files = []
    for root, _dirs, names in os.walk(KNOWLEDGE_DIR):
        for name in names:
            if name.lower().endswith(".md"):
                files.append(os.path.join(root, name))
    return sorted(files)
