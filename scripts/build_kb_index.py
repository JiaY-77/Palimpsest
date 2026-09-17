"""
知识库索引构建脚本
==================
扫描知识库根目录（KNOWLEDGE_DIR，可用环境变量覆盖）下的所有 Obsidian 笔记（.md），按 Markdown
标题（## / ###）切片，每块 300~800 字符，向量化后以 type=kb_chunk 节点写入
TriviumDB，供 kb_search / mem_search(scope=kb) 做语义检索。

v1.1 增量更新（默认模式）：
    python scripts/build_kb_index.py
    每个源文件的 mtime 记录在 kb_chunk payload.source_mtime；重建前遍历现有
    kb_chunk 节点建立 {source_path: (mtime, node_ids)} 映射，只重建「新文件」
    或「mtime 变化」的文件（先删旧块再重新切片插入），mtime 未变的文件跳过。
    老数据（无 source_mtime 字段）视为「未知」，一律重建（保险起见）。

v2.1 退役文档排除：
    文档引言区（frontmatter 之后、首个 ## 节标题之前）出现「⛔ 已退役」或
    「已退役（」横幅标记 → 判定为已退役文档，不进入索引（不切片、不向量化），
    并清理其在库中的旧 kb_chunk 节点（否则旧块仍会被语义检索命中）。
    只检测引言区，避免误伤正文提到「已退役」的文档（如知识库首页的导航列表）。
    幂等：退役文档第二次运行时库中已无其旧节点，重复执行无副作用。

v1.0 Upsert 重建策略：
    重建不再删除旧块再重新插入（导致节点 id 全部变化、图谱边丢失），改为
    upsert 策略保持节点 id 不变：
    - 对每个待重建文件，读取旧块 payload.chunk_index 建立 {chunk_index: node_id} 映射；
    - 遍历新切片：新块 i 对应旧 id 存在 → update_payload + update_vector（保持 id）；
      不存在 → insert_node（文件变长新增块）；
      旧 chunk_index >= 新块数 → delete_nodes（文件变短多余块）。
    - 全量模式（--full）不再删除所有旧 kb_chunk 节点，改为强制所有 active 文件
      走 upsert，并统一执行孤儿/退役文档旧块清理。
    - 增量/全量模式均保留孤儿清理与退役文档旧块清理。
    图谱边（RELATED_TO）不再因重建丢失。

可直接运行，也可 import 调用 build(full=...)。
"""

import argparse
import os
import re
import time

# 确保能 import 项目 core 模块（以项目根为基准，_common 导入即把项目根注入 sys.path）
import _common  # noqa: F401

from config import Config
from core.fts_index import rebuild as fts_rebuild
from core.index_rules import IndexRules, chunk_markdown, is_included, load_rules, match_kind
from core.trivium_store import TriviumStore

# 知识库根目录统一由 mcp_tools._common 提供（环境变量 KNOWLEDGE_DIR 优先；
# 默认约定为项目根下 ./knowledge），避免脚本各自推导本机路径造成分叉
from mcp_tools._common import KNOWLEDGE_DIR

# 每块字符数目标区间：默认值与 IndexRules 默认保持一致。
# 具体运行时的区间由加载的索引规则决定（chunk_markdown 取 rules.min/max_chunk_len）。
MIN_CHUNK_LEN = 300
MAX_CHUNK_LEN = 800

# 知识库内置默认规则（无 .palimpsest-index.json 时）：跳过 .obsidian，
# 其余与改动前行为逐位一致。default_kind="default" —— 知识库不做 kind 归类，
# 没有匹配上的路径就是「未匹配」，不伪装成任何业务 kind。
KB_BUILTIN_RULES = IndexRules(
    chunk_strategy="heading",
    min_chunk_len=MIN_CHUNK_LEN,
    max_chunk_len=MAX_CHUNK_LEN,
    exclude=(".obsidian",),
    source="builtin",
)

# split_markdown 的向后兼容默认（按标题切 300~800），与 KB_BUILTIN_RULES 同参数
DEFAULT_CHUNK_RULES = KB_BUILTIN_RULES

# 规则来源：--rules > <知识库根>/.palimpsest-index.json > 内置默认
RULES_FILENAME = ".palimpsest-index.json"

# 目标块类型（与 mcp_server.py 的 kb_search / mem_search 过滤条件保持一致）
CHUNK_TYPE = "kb_chunk"
KB_DOMAIN = "kb"

# mtime 比较容差（秒）：浮点序列化/反序列化可能有微小误差，差值小于该值视为未变化
MTIME_TOLERANCE = 1e-3

# ---- v2.1 退役文档排除 ----
# 退役横幅标记（文档引言区出现任一即视为退役）。样例：
#   > ⛔ **已退役（2026-08-24 规则收敛，维护者批准）**：本文档不再维护...
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


def _kb_md_files(knowledge_dir: str, rules: IndexRules | None = None) -> list:
    """遍历知识库目录，返回所有 .md 文件（递归，按规则过滤，路径排序）"""
    if rules is None:
        rules = KB_BUILTIN_RULES
    files = []
    for root, dirs, names in os.walk(knowledge_dir):
        # 按规则过滤目录（默认跳过 .obsidian）
        filtered_dirs = []
        for d in dirs:
            abs_dir = os.path.join(root, d)
            rel_dir = os.path.relpath(abs_dir, knowledge_dir).replace("\\", "/")
            if is_included(rel_dir, True, rules):
                filtered_dirs.append(d)
        dirs[:] = filtered_dirs
        for name in names:
            if name.lower().endswith(".md"):
                abs_fp = os.path.join(root, name)
                rel_fp = os.path.relpath(abs_fp, knowledge_dir).replace("\\", "/")
                if is_included(rel_fp, False, rules):
                    files.append(abs_fp)
    return sorted(files)


def split_markdown(text: str, rules: IndexRules | None = None) -> list:
    """
    按 Markdown 标题（## / ###）分段切片（算法迁移自 core.index_rules.chunk_markdown）。
    1. 先跳过 YAML frontmatter（--- 包围的元数据区）
    2. 以 ## 或 ### 行为段首切分；文件开头（含 # 主标题）作为第 0 段
    3. 超过 max_chunk_len 的段按行累积切块（每块尽量 min~max 字符）
    4. 相邻过小的块合并（< min_chunk_len 且合并后不超上限）
    返回非空文本块列表。

    签名向后兼容：不传 rules 时使用内置默认（heading / 300~800），
    与改动前行为逐位一致。
    """
    if rules is None:
        rules = DEFAULT_CHUNK_RULES
    return chunk_markdown(text, rules)


def _load_existing_index(store) -> dict:
    """
    遍历现有 kb_chunk 节点，建立 {source_path: {"mtime": float|None, "ids": [node_id,...],
    "domain": str}} 映射。
    mtime 取该文件任一块的 source_mtime；老数据无该字段时为 None（视为「未知」）。
    domain 取该文件任一块的 payload.domain（老数据无该字段时为空串）。
    """
    mapping = {}
    for nid, payload in store.iter_payloads():
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
    扫描库中全部 kb_chunk 节点，统计 domain=kb 的块数（按 payload.domain 区分）。
    """
    counts = {KB_DOMAIN: 0, "other": 0}
    for _nid, payload in store.iter_payloads():
        if payload.get("type") != CHUNK_TYPE:
            continue
        dom = payload.get("domain", "")
        counts[dom if dom in counts else "other"] += 1
    return counts


def _detect_retired(md_files: list, knowledge_dir: str) -> tuple:
    """退役文档预检：只读文件头部检测退役横幅，退役文档不进入索引。

    返回 (retired_rels, active_files)。
    """
    retired_rels = set()
    active_files = []
    for fp in md_files:
        rel = os.path.relpath(fp, knowledge_dir).replace("\\", "/")
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                head = f.read(RETIRED_HEAD_CHARS)
        except OSError:
            head = ""  # 读取失败不判退役，留给后续逻辑记录跳过
        if head and _is_retired_doc(head):
            retired_rels.add(rel)
            print(f"[退役] 跳过（顶部已退役横幅）: {rel}")
        else:
            active_files.append(fp)
    return retired_rels, active_files


def _determine_pending(full: bool, active_files: list, existing: dict,
                       knowledge_dir: str) -> tuple:
    """确定待重建文件列表（v1.0）。

    全量模式：所有 active 文件强制重建；
    增量模式：新文件 / 老数据无 mtime / mtime 变化 → 重建，其余跳过。
    返回 (pending, skipped)。
    """
    if full:
        # 全量模式：所有 active 文件强制重建（upsert），不再删除旧 kb_chunk 节点
        return list(active_files), 0

    # ---- 增量模式：筛选需要重建的文件 ----
    pending = []
    skipped = 0
    for fp in active_files:
        rel = os.path.relpath(fp, knowledge_dir).replace("\\", "/")
        cur_mtime = os.path.getmtime(fp)
        entry = existing.get(rel)
        if entry is None:
            pending.append(fp)  # 新文件（索引里没有）
        elif entry["mtime"] is None \
                or abs(entry["mtime"] - cur_mtime) > MTIME_TOLERANCE:
            pending.append(fp)  # 老数据无 mtime（未知）或 mtime 变化
        else:
            skipped += 1  # mtime 未变，跳过
    return pending, skipped


def _cleanup_orphans_and_retired(store, existing: dict, known_paths: set,
                                 retired_rels: set) -> int:
    """孤儿与退役文档旧块清理。

    孤儿：existing 中有但磁盘上已不存在的源文件旧块删除；
    退役：退役文档不进入索引，旧块须一并移除。
    返回删除节点数 deleted_old。
    """
    deleted_old = 0

    # 孤儿清理：existing 中有但磁盘上已不存在的源文件旧块删除
    orphan_ids = []
    for rel, entry in existing.items():
        if rel not in known_paths:
            orphan_ids.extend(entry["ids"])
    if orphan_ids:
        deleted_old += _delete_nodes(store, orphan_ids)
        print(f"[清理] 孤儿：源文件已删除，清理旧 kb_chunk 节点 {len(orphan_ids)} 个")

    # v2.1 退役文档旧节点清理
    retired_ids = []
    for rel in sorted(retired_rels):
        entry = existing.get(rel)
        if entry and entry["ids"]:
            retired_ids.extend(entry["ids"])
    if retired_ids:
        deleted_old += _delete_nodes(store, retired_ids)
        print(f"[清理] 退役文档旧 kb_chunk 节点 {len(retired_ids)} 个已删除: "
              f"{sorted(retired_rels)}")

    return deleted_old


def _rebuild_file(store, fp: str, knowledge_dir: str, existing: dict,
                  rules: IndexRules) -> tuple:
    """切片 -> upsert 入库单个文件（v1.0：保持旧节点 id）。

    读取失败返回 (None, 0)（由调用方计入跳过）；成功返回
    ({"file","chunks","char_lens"}, deleted_old)。
    """
    try:
        with open(fp, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError as e:
        print(f"[跳过] 读取失败: {fp}: {e}")
        return None, 0

    rel = os.path.relpath(fp, knowledge_dir).replace("\\", "/")
    title = os.path.splitext(os.path.basename(fp))[0]
    cur_mtime = os.path.getmtime(fp)
    chunks = split_markdown(text, rules)

    # v1.0 构建旧块 {chunk_index: node_id} 映射
    entry = existing.get(rel)
    old_index_map = {}
    if entry and entry["ids"]:
        for nid in entry["ids"]:
            node = store.get_node(nid)
            payload = node.get("payload", {}) if node else {}
            ci = payload.get("chunk_index")
            if ci is not None:
                old_index_map[int(ci)] = nid

    char_lens = []
    doc_domain = rules.domain or KB_DOMAIN

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
        old_nid = old_index_map.get(i)
        if old_nid is not None:
            store.update_vector(old_nid, emb)
            store.update_payload(old_nid, payload)
        else:
            store.insert_node(payload, emb)
        char_lens.append(len(chunk))

    # 删除多余旧块（旧 chunk_index >= 新块数）
    deleted_old = 0
    excess_ids = [nid for ci, nid in old_index_map.items() if ci >= len(chunks)]
    if excess_ids:
        deleted_old += _delete_nodes(store, excess_ids)

    return {"file": rel, "chunks": len(chunks), "char_lens": char_lens}, deleted_old


def build(knowledge_dir: str = KNOWLEDGE_DIR, store=None, full: bool = False,
          rules: IndexRules | str | None = None) -> dict:
    """
    构建知识库向量索引（v1.0 upsert 策略）。
    full=True：全量模式——所有 active 文件强制 upsert 重建（不删除旧节点，保持 id），
        并统一执行孤儿/退役文档旧块清理。
    full=False（默认）：增量更新——对比 mtime，只重建新增/变化的文件；老数据
        （无 source_mtime）视为未知一律重建；源文件已删除的孤儿块顺带清理。
    重建时通过 upsert 保持旧节点 id 不变，图谱边（RELATED_TO）不再因重建丢失。

    rules: IndexRules 对象、规则文件路径字符串、或 None（自动从 knowledge_dir 根
        加载 .palimpsest-index.json；无配置时使用内置默认——与改动前行为一致）。
    规则里的 domain 覆盖 payload domain（默认仍为 kb）。

    返回统计信息 {files, total_chunks, elapsed, mode, rebuilt, skipped, deleted_old,
        domain_counts, unmatched_paths}。
    （结构已拆分：_detect_retired / _determine_pending / _cleanup_orphans_and_retired
     / _rebuild_file，行为不变。）
    """
    # 加载规则
    if rules is None:
        # 无配置 → 知识库内置默认（按标题切 300~800、只排除 .obsidian）
        rules_obj = load_rules(root=knowledge_dir, builtin=KB_BUILTIN_RULES)
    elif isinstance(rules, str):
        rules_obj = load_rules(root=knowledge_dir, explicit=rules)
    else:
        rules_obj = rules

    store = store or TriviumStore()
    md_files = _kb_md_files(knowledge_dir, rules_obj)
    existing = _load_existing_index(store)

    # ---- v2.1 退役文档预检 ----
    retired_rels, active_files = _detect_retired(md_files, knowledge_dir)

    t0 = time.time()
    file_stats = []
    total_chunks = 0
    deleted_old = 0

    # ---- v1.0 确定待重建文件列表 ----
    pending, skipped = _determine_pending(full, active_files, existing, knowledge_dir)

    # ---- 两个模式共用：known_paths 构造 + 孤儿/退役清理 ----
    known_paths = set()
    for fp in active_files:
        known_paths.add(os.path.relpath(fp, knowledge_dir).replace("\\", "/"))
    known_paths |= retired_rels  # 退役文档仍在磁盘上（不算孤儿）

    deleted_old += _cleanup_orphans_and_retired(store, existing, known_paths,
                                                retired_rels)

    # ---- 切片 -> upsert 入库（v1.0：保持旧节点 id） ----
    rebuilt = 0
    for fp in pending:
        stats_entry, delta = _rebuild_file(store, fp, knowledge_dir, existing,
                                           rules_obj)
        if stats_entry is None:
            skipped += 1
            continue
        file_stats.append(stats_entry)
        total_chunks += stats_entry["chunks"]
        deleted_old += delta
        rebuilt += 1
        print(f"  {stats_entry['file']}: {stats_entry['chunks']} 块 "
              f"{stats_entry['char_lens']}")

    elapsed = round(time.time() - t0, 2)
    mode = "full" if full else "incremental"
    domain_counts = _count_domain_chunks(store)

    # ---- 未匹配路径显式化（仅当规则提供 kind_map 时才有意义） ----
    unmatched_paths: list[str] = []
    if rules_obj.kind_map:
        for fp in active_files:
            rel = os.path.relpath(fp, knowledge_dir).replace("\\", "/")
            if match_kind(rel, rules_obj) == rules_obj.default_kind:
                unmatched_paths.append(rel)
    unmatched_paths.sort()

    print(f"\n=== 知识库索引构建完成（{'全量' if full else '增量'}模式）v1.0 upsert ===")
    print(f"总块数: {total_chunks} | 新增/重建: {rebuilt} 篇 | 跳过: {skipped} 篇 | 耗时: {elapsed} 秒")
    print(f"[域统计] domain=kb（知识块）: {domain_counts.get(KB_DOMAIN, 0)} 块 | "
          f"其他: {domain_counts.get('other', 0)} 块")
    print(f"[规则] 来源: {rules_obj.source} | kind_map {len(rules_obj.kind_map)} 条 | "
          f"默认 kind: {rules_obj.default_kind}")
    for w in rules_obj.warnings:
        print(f"[警告] {w}")
    if unmatched_paths:
        # 未匹配路径必须可见：不静默归并到某个 kind（旧实现把「不知道」兜底成
        # character，误判时全程无声）
        print(f"[未匹配] {len(unmatched_paths)} 个文件未命中任何 kind_map 规则"
              f"（kind={rules_obj.default_kind}）:")
        for rel in unmatched_paths[:10]:
            print(f"  {rel}")
        if len(unmatched_paths) > 10:
            print(f"  ... 其余 {len(unmatched_paths) - 10} 个见 unmatched_paths 字段")
    if deleted_old:
        print(f"（删除旧 kb_chunk 节点 {deleted_old} 个）")
    # v1.0 FTS 联动（混合检索依赖）：知识库重建后同步全文索引；
    # 失败不阻塞主流程（可手动 palimpsest_cli.py fts-rebuild 兜底）
    fts_count = -1
    try:
        fts_count = fts_rebuild(store)
        print(f"[FTS] 全文索引已同步: {fts_count} 个节点")
    except Exception as e:  # noqa: BLE001 —— FTS 重建失败仅提示可手动 fts-rebuild 兜底
        print(f"[FTS] 全文索引同步失败（可手动 fts-rebuild）: {e}")
    return {
        "files": file_stats,
        "total_chunks": total_chunks,
        "elapsed": elapsed,
        "mode": mode,
        "rebuilt": rebuilt,
        "skipped": skipped,
        "deleted_old": deleted_old,
        "domain_counts": domain_counts,  # v1.0: {kb: n, other: n}
        "fts_count": fts_count,  # v1.0: FTS 同步节点数（-1=失败）
        "unmatched_paths": unmatched_paths,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="知识库向量索引构建（默认增量，--full 全量）")
    parser.add_argument("--full", action="store_true",
                        help="全量重建：所有 active 文件强制 upsert 重建（保持节点 id）")
    parser.add_argument("--rules", default=None,
                        help="索引规则 JSON 文件路径（不传则使用知识库根 .palimpsest-index.json 或内置默认）")
    args = parser.parse_args()
    rules_arg = None
    if args.rules:
        # 先解析并打印规则告警，再传给 build（避免 build 内部二次解析路径字符串）
        rules_arg = load_rules(root=KNOWLEDGE_DIR, explicit=args.rules)
        for w in rules_arg.warnings:
            print(f"[警告] {w}")
    else:
        loaded = load_rules(root=KNOWLEDGE_DIR)
        if loaded.source != "builtin":
            rules_arg = loaded
        for w in loaded.warnings:
            print(f"[警告] {w}")
    print(f"知识库根目录: {KNOWLEDGE_DIR}")
    print(f"数据库路径: {Config.DB_PATH}")
    print(f"模式: {'全量重建' if args.full else '增量更新（mtime 对比）'}")
    result = build(full=args.full, rules=rules_arg)
    if result["unmatched_paths"]:
        print(f"未匹配 kind 的文件 {len(result['unmatched_paths'])} 个（default_kind 兜底）:")
        for p in result["unmatched_paths"][:10]:
            print(f"  {p}")
