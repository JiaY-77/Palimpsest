# -*- coding: utf-8 -*-
"""
Palimpsest CLI —— 本地 CLI 薄封装（2026-08-25）。

设计：
  - 直接复用 mcp_server.py 的工具函数（mem_search / mem_ingest / mem_link 等），
    不复制冲突检测/规则加权逻辑，避免漂移。
  - 输出 = MCP 工具原生返回（JSON 字符串），已按省 token 设计（摘要 150 字等）。
  - 供本地调用：用户只下「派活一句话 + 读结果」，
    机械调用全在本地免费侧。

用法：
  python palimpsest_cli.py search "关键词" [--scope all|memory|kb] [--domain X] [--top-k 5] [--neighbors]
  python palimpsest_cli.py hybrid-search "关键词" [--scope all] [--domain X] [--top-k 5] [--mode rrf|cascade] [--fts-limit 50]
  python palimpsest_cli.py ingest "记忆内容" [--domain X] [--importance 0.5] [--type memory]
  python palimpsest_cli.py link --source N --target N [--relation RELATED_TO] [--one-way]
  python palimpsest_cli.py index
  python palimpsest_cli.py graph --id N [--depth 1] [--relation X]
  python palimpsest_cli.py recent [--limit 10] [--domain X]
  python palimpsest_cli.py kb "关键词" [--top-k 5]
  python palimpsest_cli.py consolidate [--apply] [--threshold 0.85] [--max-importance 0.8]
  python palimpsest_cli.py ingest-git [--repo PATH] [--since N]
  python palimpsest_cli.py startup-check

边界（CLI 不越权）：
  - importance 分级 / 记忆内容撰写 / 建边决策 = 用户负责；
    CLI 只做机械执行与结果回传。
"""
import argparse
import json
import os
import subprocess
import sys

# 依赖守卫：以下 import 依赖第三方包（fastapi/dotenv/triviumdb/requests 等），
# 未激活 venv 或依赖没装全时会抛 ModuleNotFoundError——给新手友好引导而非裸 traceback。
try:
    try:
        # 包形式（import scripts.palimpsest_cli，palimpsest-cli 控制台入口）
        from ._common import SCRIPT_DIR as _SCRIPT_DIR, PROJECT_ROOT as _PROJECT_ROOT
    except ImportError:  # 直接运行 scripts/palimpsest_cli.py 时退化为同目录导入
        from _common import SCRIPT_DIR as _SCRIPT_DIR, PROJECT_ROOT as _PROJECT_ROOT

    # 复用 mcp_tools 的工具函数（config 已基于项目根解析绝对路径，与 cwd 无关）
    from mcp_tools import (  # noqa: E402
        graph_neighbors, kb_index, kb_search, mem_hybrid_search, mem_ingest,
        mem_link, mem_recent, mem_review, mem_search,
    )
    from core.consolidator import consolidate  # noqa: E402
    from core.fts_index import rebuild as fts_rebuild, search_fts  # noqa: E402
    from core.startup_check import run_startup_check  # noqa: E402
    from core.trivium_store import TriviumStore, is_valid_block  # noqa: E402
except ImportError as _import_err:
    _hint = (
        "\n"
        "未检测到依赖 / Missing dependencies\n"
        "\n"
        "请先安装项目依赖再运行，参考 README（Install dependencies, see README）:\n"
        "    python -m venv venv\n"
        "    venv\\Scripts\\activate              # Windows\n"
        "    source venv/bin/activate            # macOS / Linux\n"
        "    pip install -r requirements.txt\n"
        "\n"
        "未检测到依赖。请先安装：python -m venv venv && venv\\Scripts\\activate && pip install -r requirements.txt\n"
        "Dependencies not found. Please install: python -m venv venv && venv\\Scripts\\activate && pip install -r requirements.txt\n"
        "\n"
        f"详情（details）: {_import_err}\n"
    )
    print(_hint, file=sys.stderr)
    sys.exit(1)


def _validate_block(block: str) -> str:
    """校验 --block 参数：出厂通用区块直接通过；自定义 domain 放行但给提示（防拼写错误，不拦截）。"""
    if block and not is_valid_block(block):
        hint = (
            f"提示：{block} 不是出厂内置区块（task / kb / hermes / general，rule 归入 kb）。"
            "若为自定义 domain 可正常使用，若为拼写错误请检查。\n"
            f"Note: {block} is not a built-in block (task / kb / hermes / general, "
            "rule is part of kb). Custom domains work as blocks; check for typos."
        )
        print(hint, file=sys.stderr)
    return block


def cmd_search(args):
    _validate_block(args.block)
    print(mem_search(
        query=args.query, scope=args.scope, domain=args.domain,
        top_k=args.top_k, include_neighbors=args.neighbors, block=args.block,
    ))


def cmd_hybrid_search(args):
    print(mem_hybrid_search(
        query=args.query, scope=args.scope, domain=args.domain,
        top_k=args.top_k, mode=args.mode, fts_limit=args.fts_limit,
    ))


def cmd_ingest(args):
    result = mem_ingest(
        content=args.content, domain=args.domain,
        importance=args.importance, type=args.type,
    )
    print(result)
    data = json.loads(result)
    if not data.get("stored"):
        sys.exit(1)


def cmd_link(args):
    print(mem_link(
        source_id=args.source, target_id=args.target,
        relation=args.relation, bidirectional=not args.one_way,
    ))


def cmd_index(args):
    print(kb_index())


def cmd_graph(args):
    _validate_block(args.block)
    print(graph_neighbors(
        node_id=args.id, relation=args.relation, depth=args.depth,
        limit=args.limit, min_weight=args.min_weight, block=args.block,
    ))


def cmd_recent(args):
    print(mem_recent(domain=args.domain, limit=args.limit))


def cmd_review(args):
    raw = mem_review(days=args.days, domain=args.domain)
    try:
        data = json.loads(raw)
        # 统计 decision 节点数
        from core.trivium_store import TriviumStore
        _store = TriviumStore()
        decision_count = 0
        for nid, payload in _store.iter_payloads():
            if payload.get("type") == "decision":
                    decision_count += 1
        stats = data.get("stats", {})
        stats["decision"] = decision_count
        data["stats"] = stats
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except (json.JSONDecodeError, KeyError):
        print(raw)


def cmd_kb(args):
    print(kb_search(query=args.query, top_k=args.top_k))


def cmd_consolidate(args):
    from core.trivium_store import TriviumStore
    store = TriviumStore()
    result = consolidate(
        store,
        dry_run=not args.apply,
        sim_threshold=args.threshold,
        max_importance=args.max_importance,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_stats(args):
    """库级盘点统计（只读）：--section 可独立开关分节，缺省全给。"""
    from core.stats import compute_stats
    from core.trivium_store import TriviumStore

    store = TriviumStore()
    result = compute_stats(store)
    if args.section:
        wanted = set(args.section)
        picked = {}
        # "domains" 别名 = totals 里的按 domain 分布（单独列出该分节）
        if "domains" in wanted:
            picked["by_domain"] = result["totals"].get("by_domain", {})
        for key in result:
            if key == "elapsed_ms" or key not in wanted:
                continue
            picked[key] = result[key]
        result = picked
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_promote(args):
    """高频记忆自动升级：默认 dry-run 只打印候选，--apply 才升权打标。"""
    from core.promoter import promote
    from core.trivium_store import TriviumStore

    store = TriviumStore()
    result = promote(
        store,
        dry_run=not args.apply,
        days=args.days,
        min_hits=args.min_hits,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_ingest_git(args):
    """将 git commit 作为 git_commit 类型节点入库（幂等：已有则跳过）。"""
    repo = args.repo or _PROJECT_ROOT
    since = args.since

    # 运行 git log 获取最近 N 天的 commit
    fmt = "%H|%ad|%s"
    cmd = [
        "git", "-C", repo, "log",
        f'--since={since} days ago',
        f"--pretty=format:{fmt}",
        "--date=short",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(f"git log 失败：{result.stderr.strip()}")
        sys.exit(1)

    # 解析 commit 行
    lines = result.stdout.strip().splitlines()
    commits = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({
                "commit_hash": parts[0],
                "commit_time": parts[1],
                "subject": parts[2],
            })

    # 幂等检查：遍历库中已有节点，收集已有的 commit_hash
    store = TriviumStore()
    existing_hashes = set()
    for nid, payload in store.iter_payloads():
        ch = payload.get("commit_hash")
        if ch:
            existing_hashes.add(ch)

    added = 0
    skipped = 0
    for c in commits:
        if c["commit_hash"] in existing_hashes:
            skipped += 1
            continue
        node_data = {
            "type": "git_commit",
            "content": c["subject"],
            "importance": 0.5,
            "commit_hash": c["commit_hash"],
            "commit_time": c["commit_time"],
            "repo": repo,
            "source": "git_memory",
        }
        emb = store.embed_text(c["subject"])
        store.insert_node(node_data, emb)
        added += 1

    print(f"ingest-git 完成：新增 {added} 条，跳过 {skipped} 条（已存在）")


def cmd_fts_rebuild(args):
    store = TriviumStore()
    count = fts_rebuild(store)
    print(f"FTS 索引重建完成，已索引 {count} 个节点")


def cmd_fts_search(args):
    results = search_fts(args.query, limit=args.limit)
    if not results:
        print(f"FTS 搜索「{args.query}」：无命中")
        return
    from core.trivium_store import TriviumStore
    from mcp_tools._common import _shorten

    store = TriviumStore()
    for r in results:
        nid = r["node_id"]
        # 摘要设计：只输出 node_id + 截断摘要（不复读全文 content）；
        # 带 secret_hint 标记的条目打 [secret 已隐藏]，不展示内容明文。
        node = store.get_node(nid)
        payload = (node or {}).get("payload", {}) or {}
        if payload.get("secret_hint"):
            print(f"  node_id={nid}  [secret 已隐藏]")
        else:
            print(f"  node_id={nid}  {_shorten(r.get('content', ''), 120)}")
    print(f"共 {len(results)} 条命中")


def cmd_startup_check(args):
    """启动自检（工程护栏）：输出结构化结果，失败退出码 1。"""
    result = run_startup_check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("ok") else 1)


def cmd_task_archive(args):
    """已完成任务节点自动归档：默认 dry-run 预览（JSON），--apply 真正执行归档+删节点。"""
    from core.task_archive import archive_tasks
    from mcp_tools._common import KNOWLEDGE_DIR

    store = TriviumStore()
    kb_dir = args.knowledge_dir or KNOWLEDGE_DIR
    result = archive_tasks(store, dry_run=not args.apply, knowledge_dir=kb_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(
        prog="palimpsest_cli",
        description="Palimpsest 本地 CLI",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="统一检索（记忆+知识库）")
    sp.add_argument("query")
    sp.add_argument("--scope", default="all", choices=["all", "memory", "kb"])
    sp.add_argument("--domain", default="")
    sp.add_argument("--top-k", type=int, default=5)
    sp.add_argument("--neighbors", action="store_true", help="返回图关联区")
    sp.add_argument("--block", default="", help="图谱扩散只走同区块边（内置：task/kb/hermes/general；可传自定义 domain）")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("hybrid-search", help="混合检索（FTS5 精确 + 语义向量：RRF 融合 / 级联）")
    sp.add_argument("query")
    sp.add_argument("--scope", default="all", choices=["all", "memory", "kb"])
    sp.add_argument("--domain", default="")
    sp.add_argument("--top-k", type=int, default=5)
    sp.add_argument("--mode", default="rrf", choices=["rrf", "cascade"], help="rrf=倒数排名融合；cascade=FTS 粗筛→向量精排")
    sp.add_argument("--fts-limit", type=int, default=50, help="FTS 侧候选量（粗筛/桶大小）")
    sp.set_defaults(fn=cmd_hybrid_search)

    sp = sub.add_parser("ingest", help="写入记忆（内容/分级由用户定）")
    sp.add_argument("content")
    sp.add_argument("--domain", default="")
    sp.add_argument("--importance", type=float, default=0.5)
    sp.add_argument("--type", default="memory")
    sp.set_defaults(fn=cmd_ingest)

    sp = sub.add_parser("link", help="手动建边（边决策由用户定）")
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
    sp.add_argument("--block", default="", help="只沿 target 节点 domain 匹配区块的边扩散（内置：task/kb/hermes/general；可传自定义 domain）")
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

    sp = sub.add_parser("consolidate", help="容量自动合并（预览/合并高相似度 memory 节点对）")
    sp.add_argument("--apply", action="store_true", help="执行合并（默认 dry-run 只预览）")
    sp.add_argument("--threshold", type=float, default=0.85, help="相似度阈值（默认 0.85）")
    sp.add_argument("--max-importance", type=float, default=0.8, help="高价值保护阈值（>= 此值不合并，默认 0.8）")
    sp.set_defaults(fn=cmd_consolidate)

    sp = sub.add_parser("stats", help="库级盘点统计（totals/kinds/importance/time/graph/domains，只读）")
    sp.add_argument("--section", nargs="+",
                    choices=["totals", "kinds", "importance", "time", "graph", "domains"],
                    help="只输出指定分节（可多个）；缺省全给（domains=totals 的按 domain 分布）")
    sp.set_defaults(fn=cmd_stats)

    sp = sub.add_parser("promote", help="高频记忆自动升级（默认 dry-run 预览，--apply 才升权打标）")
    sp.add_argument("--apply", action="store_true", help="执行升权打标（默认 dry-run 只预览候选）")
    sp.add_argument("--days", type=int, default=30, help="命中活跃窗口（天，默认 30）")
    sp.add_argument("--min-hits", type=int, default=5, help="候选 hit_count 门槛（默认 5）")
    sp.set_defaults(fn=cmd_promote)

    sp = sub.add_parser("ingest-git", help="将 git commit 入库为 git_commit 节点（幂等）")
    sp.add_argument("--repo", default="", help="git 仓库路径（默认项目根）")
    sp.add_argument("--since", type=int, default=7, help="入库最近 N 天的 commit（默认 7）")
    sp.set_defaults(fn=cmd_ingest_git)

    sp = sub.add_parser("fts-rebuild", help="全量重建 FTS5 全文索引")
    sp.set_defaults(fn=cmd_fts_rebuild)

    sp = sub.add_parser("fts-search", help="FTS5 全文搜索（trigram 中文子串匹配）")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(fn=cmd_fts_search)

    sp = sub.add_parser("startup-check", help="启动自检（文件/存储/FTS/依赖），失败退出码 1")
    sp.set_defaults(fn=cmd_startup_check)

    sp = sub.add_parser("task-archive", help="已完成任务节点自动归档到知识库（05_任务归档/），默认 dry-run 预览")
    sp.add_argument("--apply", action="store_true", help="真正执行：写归档 md + 删节点（默认只预览不落盘）")
    sp.add_argument("--knowledge-dir", default="", help="知识库根目录（默认 KNOWLEDGE_DIR 环境变量对应的知识库根）")
    sp.set_defaults(fn=cmd_task_archive)

    try:
        args = p.parse_args()
        args.fn(args)
    except Exception as e:  # noqa: BLE001 — CLI 顶层统一兜底，友好报错而非裸 traceback
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
