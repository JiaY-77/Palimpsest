# -*- coding: utf-8 -*-
"""
知识库索引构建脚本
==================
扫描 D:/HeJiaQi/Documents/Knowledge 下的所有 Obsidian 笔记（.md），按 Markdown
标题（## / ###）切片，每块 300~800 字符，向量化后以 type=kb_chunk 节点写入
TriviumDB，供 kb_search / mem_search(scope=kb) 做语义检索。

v1.1 增量更新（默认模式）：
    python scripts/build_kb_index.py
    每个源文件的 mtime 记录在 kb_chunk payload.source_mtime；重建前遍历现有
    kb_chunk 节点建立 {source_path: (mtime, node_ids)} 映射，只重建「新文件」
    或「mtime 变化」的文件（先删旧块再重新切片插入），mtime 未变的文件跳过。
    老数据（无 source_mtime 字段）视为「未知」，一律重建（保险起见）。

v2.0 统一语义层（规则类标记）：
    规则类文档（文件名/路径含 副官加班协议/宪法/模型军团管理办法/模型路由决策树）
    的切片 domain 打 "rule"（type 仍为 kb_chunk，是知识的子集），其余保持 "kb"。
    增量模式下若文件已有索引的 domain 与当前判定不一致（如 kb→rule），也会触发重建。
    构建完成后输出 domain=rule / domain=kb 块数统计（返回 dict 含 domain_counts）。

v2.1 退役文档排除：
    文档引言区（frontmatter 之后、首个 ## 节标题之前）出现「⛔ 已退役」或
    「已退役（」横幅标记 → 判定为已退役文档，不进入索引（不切片、不向量化），
    并清理其在库中的旧 kb_chunk 节点（否则旧块仍会被语义检索命中）。
    只检测引言区，避免误伤正文提到「已退役」的文档（如模型军团管理办法的
    模型退役表格、模型路由决策树的「宪法已退役」说明、知识库首页的导航列表）。
    幂等：退役文档第二次运行时库中已无其旧节点，重复执行无副作用。

全量重建（v1.0 行为）：
    python scripts/build_kb_index.py --full
    删除所有 type==kb_chunk 旧节点后全量插入。

可直接运行，也可 import 调用 build(full=...)。
"""

import argparse
import os
import re
import sys
import time

# 确保能 import 项目 core 模块（以项目根为基准）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# 切换到项目根目录，保证 config 里的相对路径（data/mh_memory.db）解析正确
os.chdir(_PROJECT_ROOT)

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402

# 知识库根目录（小七的知识库）
KNOWLEDGE_DIR = r"D:/HeJiaQi/Documents/Knowledge"

# 每块字符数目标区间（简单实现：超长段按行切，尽量落在区间内）
MIN_CHUNK_LEN = 300
MAX_CHUNK_LEN = 800

# 目标块类型（与 mcp_server.py 的 kb_search / mem_search 过滤条件保持一致）
CHUNK_TYPE = "kb_chunk"

# mtime 比较容差（秒）：浮点序列化/反序列化可能有微小误差，差值小于该值视为未变化
MTIME_TOLERANCE = 1e-3

# ---- v2.0 统一语义层：规则类文档标记 ----
# 规则类文档（文件名/相对路径包含以下任一关键词）的切片 domain 打 "rule"，
# 其余笔记保持 "kb"。rule 是知识的子集（type 仍为 kb_chunk），mem_search 会对
# rule 节点内置 ×1.3 加权，router_query 只查 rule 切片做任务路由。
RULE_KEYWORDS = ["副官加班协议", "宪法", "模型军团管理办法", "模型路由决策树"]
RULE_DOMAIN = "rule"
KB_DOMAIN = "kb"

# ---- v2.1 退役文档排除 ----
# 退役横幅标记（文档引言区出现任一即视为退役）。样例：
#   > ⛔ **已退役（2026-08-24 规则收敛，主人批准）**：本文档不再维护...
# 只检测引言区（frontmatter 之后、首个 ## 节标题之前），避免误伤正文提及
# 「已退役」的文档（模型军团管理办法的模型退役表格、模型路由决策树的
# 「宪法已退役」说明、知识库首页的退役导航列表等）。
RETIRED_MARKERS = ("⛔ 已退役", "已退役（")

# 退役预检只读取文件头部这么多字符（frontmatter + 引言区横幅绰绰有余）
RETIRED_HEAD_CHARS = 4096


def _is_retired_doc(text: str) -> bool:
    """
    检测文档是否已退役：跳过 YAML frontmatter 后，检查首个 ## / ### 节标题
    之前的引言区是否出现「⛔ 已退役」或「已退役（」横幅标记。
    退役横幅语义上必然位于文档顶部引言区，此策略精确且不误伤正文提及。
    """
    lines = text.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    head = []
    for line in lines[start:]:
        if re.match(r"^#{2,3}\s", line):  # 首个 ## / ### 节标题，引言区到此为止
            break
        head.append(line)
    intro = "\n".join(head)
    return any(m in intro for m in RETIRED_MARKERS)


def _doc_domain(rel_path: str) -> str:
    """
    判断文档所属域：文件名/相对路径包含任一规则类关键词 → "rule"，
    否则 → "kb"。与 mcp_server 的 rule 加权、router_query、check_kb_consistency 保持一致。
    """
    for kw in RULE_KEYWORDS:
        if kw in rel_path:
            return RULE_DOMAIN
    return KB_DOMAIN


def _kb_md_files(knowledge_dir: str) -> list:
    """遍历知识库目录，返回所有 .md 文件（递归，跳过 .obsidian，按路径排序）"""
    files = []
    for root, dirs, names in os.walk(knowledge_dir):
        # 跳过 Obsidian 配置目录
        dirs[:] = [d for d in dirs if d != ".obsidian"]
        for name in names:
            if name.lower().endswith(".md"):
                files.append(os.path.join(root, name))
    return sorted(files)


def split_markdown(text: str) -> list:
    """
    按 Markdown 标题（## / ###）分段切片：
    1. 先跳过 YAML frontmatter（--- 包围的元数据区）
    2. 以 ## 或 ### 行为段首切分；文件开头（含 # 主标题）作为第 0 段
    3. 超过 MAX_CHUNK_LEN 的段按行累积切块（每块尽量 300~800 字符）
    4. 相邻过小的块合并（< MIN_CHUNK_LEN 且合并后不超上限）
    返回非空文本块列表。
    """
    lines = text.splitlines()
    # ---- 跳过 YAML frontmatter ----
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break
    # ---- 按标题切段 ----
    sections = []
    cur = []
    for line in lines:
        if re.match(r"^#{2,3}\s", line):  # ## 或 ### 标题
            if cur:
                sections.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        sections.append("\n".join(cur))

    # ---- 超长段按行切块 ----
    chunks = []
    for sec in sections:
        if len(sec) <= MAX_CHUNK_LEN:
            chunks.append(sec)
            continue
        buf = []
        buf_len = 0
        for line in sec.splitlines():
            buf.append(line)
            buf_len += len(line) + 1
            if buf_len >= MAX_CHUNK_LEN:
                chunks.append("\n".join(buf))
                buf = []
                buf_len = 0
        if buf:
            chunks.append("\n".join(buf))

    # ---- 过小的块与后一块合并（保持 300~800 的粗略区间） ----
    merged = []
    for c in chunks:
        if merged and len(merged[-1]) < MIN_CHUNK_LEN \
                and len(merged[-1]) + len(c) <= MAX_CHUNK_LEN:
            merged[-1] = merged[-1] + "\n" + c
        else:
            merged.append(c)

    return [c.strip() for c in merged if c and c.strip()]


def _load_existing_index(store) -> dict:
    """
    遍历现有 kb_chunk 节点，建立 {source_path: {"mtime": float|None, "ids": [node_id,...],
    "domain": str}} 映射。
    mtime 取该文件任一块的 source_mtime；老数据无该字段时为 None（视为「未知」）。
    domain 取该文件任一块的 payload.domain（老数据无该字段时为空串）。
    """
    mapping = {}
    for nid in store._get_all_node_ids():
        node = store.get_node(nid)
        payload = node.get("payload", {}) if node else {}
        if payload.get("type") != CHUNK_TYPE:
            continue
        rel = payload.get("source_path", "")
        if not rel:
            continue
        entry = mapping.setdefault(rel, {"mtime": None, "ids": [], "domain": ""})
        if entry["mtime"] is None:
            entry["mtime"] = payload.get("source_mtime")
        if not entry["domain"]:
            entry["domain"] = payload.get("domain", "")
        entry["ids"].append(nid)
    return mapping


def _delete_nodes(store, node_ids: list) -> int:
    """批量删除节点（同时删除关联边），返回删除数量"""
    for nid in node_ids:
        store.delete_node(nid)
    return len(node_ids)


def _count_domain_chunks(store) -> dict:
    """
    扫描库中全部 kb_chunk 节点，统计 domain=rule / domain=kb 的块数
    （rule 是 kb_chunk 的子集，此处按 payload.domain 区分）。
    """
    counts = {RULE_DOMAIN: 0, KB_DOMAIN: 0, "other": 0}
    for nid in store._get_all_node_ids():
        node = store.get_node(nid)
        payload = node.get("payload", {}) if node else {}
        if payload.get("type") != CHUNK_TYPE:
            continue
        dom = payload.get("domain", "")
        counts[dom if dom in counts else "other"] += 1
    return counts


def build(knowledge_dir: str = KNOWLEDGE_DIR, store=None, full: bool = False) -> dict:
    """
    构建知识库向量索引。
    full=True：全量重建（删除所有 type=kb_chunk 节点后全量插入，等价 v1.0 行为）。
    full=False（默认）：增量更新——对比 mtime，只重建新增/变化的文件；老数据
        （无 source_mtime）视为未知一律重建；源文件已删除的孤儿块顺带清理。
    返回统计信息 {files, total_chunks, elapsed, mode, rebuilt, skipped, deleted_old}。
    """
    store = store or TriviumStore()
    md_files = _kb_md_files(knowledge_dir)
    existing = _load_existing_index(store)

    # ---- v2.1 退役文档预检：只读文件头部检测退役横幅，退役文档不进入索引 ----
    retired_rels = set()
    active_files = []
    for fp in md_files:
        rel = os.path.relpath(fp, knowledge_dir).replace("\\", "/")
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(RETIRED_HEAD_CHARS)
        except OSError:
            head = ""  # 读取失败不判退役，留给后续逻辑记录跳过
        if head and _is_retired_doc(head):
            retired_rels.add(rel)
            print(f"[退役] 跳过（顶部已退役横幅）: {rel}")
        else:
            active_files.append(fp)

    t0 = time.time()
    file_stats = []
    total_chunks = 0
    deleted_old = 0

    if full:
        # ---- 全量模式：删除所有旧 kb_chunk 节点 ----
        all_old_ids = [nid for entry in existing.values() for nid in entry["ids"]]
        deleted_old = _delete_nodes(store, all_old_ids)
        if deleted_old:
            print(f"[清理] 全量模式：已删除旧 kb_chunk 节点 {deleted_old} 个")
        pending = active_files
        skipped = 0
    else:
        # ---- 增量模式：筛选需要重建的文件 ----
        pending = []
        skipped = 0
        known_paths = set()
        for fp in active_files:
            rel = os.path.relpath(fp, knowledge_dir).replace("\\", "/")
            known_paths.add(rel)
            cur_mtime = os.path.getmtime(fp)
            entry = existing.get(rel)
            if entry is None:
                pending.append(fp)  # 新文件（索引里没有）
            elif entry["mtime"] is None \
                    or abs(entry["mtime"] - cur_mtime) > MTIME_TOLERANCE:
                pending.append(fp)  # 老数据无 mtime（未知）或 mtime 变化
            elif entry["domain"] != _doc_domain(rel):
                pending.append(fp)  # v2.0 domain 变化（如 kb→rule），需重建
            else:
                skipped += 1  # mtime 未变，跳过
        # 退役文档仍在磁盘上（不算孤儿），并入 known_paths 防止被误判清理
        known_paths |= retired_rels
        # 顺带清理：源文件已不存在的孤儿块（增量模式下的残留）
        orphan_ids = []
        for rel, entry in existing.items():
            if rel not in known_paths:
                orphan_ids.extend(entry["ids"])
        if orphan_ids:
            deleted_old += _delete_nodes(store, orphan_ids)
            print(f"[清理] 增量模式：源文件已删除，清理孤儿 kb_chunk 节点 {len(orphan_ids)} 个")
        # v2.1 退役文档旧节点清理：退役文档不进入索引，旧块须一并移除，
        # 否则旧块仍会被语义检索命中（退役 ≠ 文件删除，不能走孤儿逻辑）
        retired_ids = []
        for rel in sorted(retired_rels):
            entry = existing.get(rel)
            if entry and entry["ids"]:
                retired_ids.extend(entry["ids"])
        if retired_ids:
            deleted_old += _delete_nodes(store, retired_ids)
            print(f"[清理] 增量模式：退役文档旧 kb_chunk 节点 {len(retired_ids)} 个已删除: "
                  f"{sorted(retired_rels)}")

    # ---- 切片 -> 向量化 -> 入库（增量模式先删该文件的旧块再重建） ----
    rebuilt = 0
    for fp in pending:
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError as e:
            # 读取失败跳过并记录，不中断整体构建
            print(f"[跳过] 读取失败: {fp}: {e}")
            skipped += 1
            continue

        rel = os.path.relpath(fp, knowledge_dir).replace("\\", "/")
        title = os.path.splitext(os.path.basename(fp))[0]
        cur_mtime = os.path.getmtime(fp)
        chunks = split_markdown(text)

        # 增量模式：先删除该文件的旧块再重建（全量模式上面已全删，此处不重复）
        entry = existing.get(rel)
        if entry and entry["ids"] and not full:
            deleted_old += _delete_nodes(store, entry["ids"])

        char_lens = []
        # v2.0 统一语义层：规则类文档切片 domain 打 "rule"，其余保持 "kb"
        doc_domain = _doc_domain(rel)
        for i, chunk in enumerate(chunks):
            emb = store.embed_text(chunk)
            payload = {
                "type": CHUNK_TYPE,
                "content": chunk,
                "source_path": rel,
                "title": title,
                "domain": doc_domain,
                "chunk_index": i,
                "importance": 0.6,
                "source_mtime": cur_mtime,
            }
            store.insert_node(payload, emb)
            char_lens.append(len(chunk))
        file_stats.append({"file": rel, "chunks": len(chunks), "char_lens": char_lens})
        total_chunks += len(chunks)
        rebuilt += 1
        print(f"  {rel}: {len(chunks)} 块 {char_lens}")

    elapsed = round(time.time() - t0, 2)
    mode = "full" if full else "incremental"
    # v2.0 统一语义层：重建完成后统计 rule / kb 块数
    domain_counts = _count_domain_chunks(store)
    print(f"\n=== 知识库索引构建完成（{'全量' if full else '增量'}模式）===")
    print(f"总块数: {total_chunks} | 新增/重建: {rebuilt} 篇 | 跳过: {skipped} 篇 | 耗时: {elapsed} 秒")
    print(f"[v2.0 域统计] domain=rule（规则类）: {domain_counts.get(RULE_DOMAIN, 0)} 块 | "
          f"domain=kb（普通知识）: {domain_counts.get(KB_DOMAIN, 0)} 块")
    if deleted_old:
        print(f"（删除旧 kb_chunk 节点 {deleted_old} 个）")
    return {
        "files": file_stats,
        "total_chunks": total_chunks,
        "elapsed": elapsed,
        "mode": mode,
        "rebuilt": rebuilt,
        "skipped": skipped,
        "deleted_old": deleted_old,
        "domain_counts": domain_counts,  # v2.0: {rule: n, kb: n}
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="知识库向量索引构建（默认增量，--full 全量）")
    parser.add_argument("--full", action="store_true",
                        help="全量重建：删除所有 kb_chunk 节点后重新索引")
    args = parser.parse_args()
    print(f"知识库根目录: {KNOWLEDGE_DIR}")
    print(f"数据库路径: {Config.DB_PATH}")
    print(f"模式: {'全量重建' if args.full else '增量更新（mtime 对比）'}")
    build(full=args.full)
