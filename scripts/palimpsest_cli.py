# -*- coding: utf-8 -*-
"""
Palimpsest CLI —— 本地小兵调用小帕（Palimpsest）的薄封装（2026-08-25 小帕操作员岗试点）。

设计：
  - 直接复用 mcp_server.py 的工具函数（mem_search / mem_ingest / mem_link 等），
    不复制冲突检测/规则加权逻辑，避免漂移。
  - 输出 = MCP 工具原生返回（JSON 字符串），已按省 token 设计（摘要 150 字等）。
  - 供本地小兵（qwen 工具桥 run_command）调用：小七只下「派活一句话 + 读结果」，
    机械调用全在本地免费侧。

用法：
  python palimpsest_cli.py search "关键词" [--scope all|memory|kb] [--domain X] [--top-k 5] [--neighbors]
  python palimpsest_cli.py ingest "记忆内容" [--domain X] [--importance 0.5] [--type memory]
  python palimpsest_cli.py link --source N --target N [--relation RELATED_TO] [--one-way]
  python palimpsest_cli.py index
  python palimpsest_cli.py graph --id N [--depth 1] [--relation X]
  python palimpsest_cli.py recent [--limit 10] [--domain X]
  python palimpsest_cli.py kb "关键词" [--top-k 5]

边界（指挥官铁律，CLI 不越权）：
  - importance 分级 / 记忆内容撰写 / 建边决策 = 指挥官（小七）负责；
    小兵只做机械执行与结果回传。
"""
import argparse
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)  # Palimpsest 项目根
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 复用 mcp_server 的工具函数（mcp_server 内部会 chdir 到项目根）
from mcp_server import (  # noqa: E402
    graph_neighbors, kb_index, kb_search, mem_ingest, mem_link, mem_recent,
    mem_review, mem_search,
)


def cmd_search(args):
    print(mem_search(
        query=args.query, scope=args.scope, domain=args.domain,
        top_k=args.top_k, include_neighbors=args.neighbors, block=args.block,
    ))


def cmd_ingest(args):
    print(mem_ingest(
        content=args.content, domain=args.domain,
        importance=args.importance, type=args.type,
    ))


def cmd_link(args):
    print(mem_link(
        source_id=args.source, target_id=args.target,
        relation=args.relation, bidirectional=not args.one_way,
    ))


def cmd_index(args):
    print(kb_index())


def cmd_graph(args):
    print(graph_neighbors(
        node_id=args.id, relation=args.relation, depth=args.depth,
        limit=args.limit, min_weight=args.min_weight, block=args.block,
    ))


def cmd_recent(args):
    print(mem_recent(domain=args.domain, limit=args.limit))


def cmd_review(args):
    print(mem_review(days=args.days, domain=args.domain))


def cmd_kb(args):
    print(kb_search(query=args.query, top_k=args.top_k))


def main():
    p = argparse.ArgumentParser(
        prog="palimpsest_cli",
        description="Palimpsest 本地 CLI（供本地小兵调用，指挥官只消费结果）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="统一检索（记忆+知识库）")
    sp.add_argument("query")
    sp.add_argument("--scope", default="all", choices=["all", "memory", "kb"])
    sp.add_argument("--domain", default="")
    sp.add_argument("--top-k", type=int, default=5)
    sp.add_argument("--neighbors", action="store_true", help="返回图关联区")
    sp.add_argument("--block", default="", help="图谱扩散只走同区块边（hermes/work/novel/kb/general）")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("ingest", help="写入记忆（内容/分级由指挥官定）")
    sp.add_argument("content")
    sp.add_argument("--domain", default="")
    sp.add_argument("--importance", type=float, default=0.5)
    sp.add_argument("--type", default="memory")
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("link", help="手动建边（边决策由指挥官定）")
    sp.add_argument("--source", type=int, required=True)
    sp.add_argument("--target", type=int, required=True)
    sp.add_argument("--relation", default="RELATED_TO")
    sp.add_argument("--one-way", action="store_true", help="不补反向边")
    sp.set_defaults(fn=cmd_link)

    sp = sub.add_parser("index", help="知识库文件索引扫描")
    sp.set_defaults(fn=cmd_index)

    sp = sub.add_parser("graph", help="图谱邻居查询")
    sp.add_argument("--id", type=int, required=True)
    sp.add_argument("--depth", type=int, default=1)
    sp.add_argument("--relation", default="")
    sp.add_argument("--min-weight", type=float, default=0.0, help="精馏：只保留 weight 不低于此值的边")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--block", default="", help="只沿 target 节点 domain 匹配区块的边扩散（hermes/work/novel/kb/general）")
    sp.set_defaults(fn=cmd_graph)

    sp = sub.add_parser("recent", help="最近记忆列表")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--domain", default="")
    sp.set_defaults(fn=cmd_recent)

    sp = sub.add_parser("review", help="复盘盘点（近 N 天记忆 + 治理候选）")
    sp.add_argument("--days", type=int, default=7)
    sp.add_argument("--domain", default="")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("kb", help="知识库语义检索")
    sp.add_argument("query")
    sp.add_argument("--top-k", type=int, default=5)
    sp.set_defaults(fn=cmd_kb)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
