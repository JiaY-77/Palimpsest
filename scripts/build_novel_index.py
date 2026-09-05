# -*- coding: utf-8 -*-
"""
小说设定库入库脚本
==================
扫描小说 vault 根目录下的所有 Obsidian 笔记（.md），每个 .md 文件作为一个
节点（整文件一块，不做子切片）向量化后以 type=novel_chunk 节点写入
TriviumDB，domain=novel，供小说创作辅助的设定检索使用。

设计要点：
  - 每个 .md 文件 = 一个节点：文件都 <7KB，语义一体；角色卡应整卡命中，
    不做 build_kb_index 那样的按标题子切片。
  - 节点字段：type=novel_chunk / domain=novel / kind（setting|overview|
    relation|character）/ title / source_path / source_mtime / importance=0.6
    / content（去 frontmatter 全文）/ status=active。
  - source_path 始终记录「相对 vault 根」的正斜杠路径（如
    "02_角色/01_中原正道/天剑阁/凌无咎.md"）。

v1.0 全量 / 增量两种模式：
  --full（全量）：先把库里所有 domain=novel 的旧节点删除（delete_node 会连带
      清理其图谱边），再全量重建所有文件。防止重复跑造成重复节点。
  默认（增量）：遍历现有 domain=novel 节点建立 {source_path: mtime} 映射，
      只处理「新文件」或「mtime 变化」的文件；同一文件更新时保持节点 id 不变
      （upsert：update_payload + update_vector），源文件中已不存在的旧节点
      （孤儿）一并删除。

向量化 / 写入失败：收集到 failed 列表，不中断整体，单个文件失败不影响其他。

方法参考 scripts/build_kb_index.py（TriviumStore / iter_payloads /
insert_node / update_payload / update_vector / delete_node 用例一致）。

可直接运行，也可 import 调用 build(source=..., full=...)。
"""
import argparse
import os
import time

# 确保能 import 项目 core 模块（以项目根为基准，_common 导入即把项目根注入 sys.path）
import _common  # noqa: E402,F401

from core.trivium_store import TriviumStore  # noqa: E402

# 节点类型与域（与 mcp_server 的 novel 区块检索条件保持一致）
CHUNK_TYPE = "novel_chunk"
DOMAIN = "novel"

# 默认数据源：无硬编码默认路径（个人 vault 路径不入开源仓库）。
# 通过环境变量 PALIMPSEST_NOVEL_DIR 或命令行 --source 提供。
DEFAULT_SOURCE_DIR = os.environ.get("PALIMPSEST_NOVEL_DIR", "") or None

# mtime 比较容差（秒）：浮点序列化/反序列化可能有微小误差，差值小于该值视为未变化
MTIME_TOLERANCE = 1e-3

# 固定 importance（小说设定统一权重）
IMPORTANCE = 0.6


def _strip_frontmatter(text: str) -> str:
    """去掉 YAML frontmatter（--- 包围的元数据区），返回正文文本。

    若文件不以 --- 开头则原样返回；frontmatter 不需要入库。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:])
    return text


def _kind_of(rel_path: str) -> str:
    """按相对 vault 根的路径判定节点 kind。

    - 01_世界观/ 下 → setting（世界观设定）
    - 文件名是 00_角色总览.md → overview（角色总览索引）
    - 01_人物关系/ 下 → relation（人物关系 / 家族血缘等）
    - 其余 02_角色 门派目录下的角色卡 → character
    """
    name = os.path.basename(rel_path)
    if name == "00_角色总览.md":
        return "overview"
    norm = rel_path.replace("\\", "/")
    if norm.startswith("01_世界观/"):
        return "setting"
    if "/01_人物关系/" in norm:
        return "relation"
    return "character"


def _extract_title(rel_path: str, content: str, kind: str) -> str:
    """提取节点标题。

    角色卡（character）取文件内第一个一级「# 」标题（即角色名，如「凌无咎」）；
    文档类（setting / relation / overview）取文件名去掉 .md 后缀。
    """
    if kind == "character":
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                return stripped[2:].strip() or os.path.splitext(os.path.basename(rel_path))[0]
    return os.path.splitext(os.path.basename(rel_path))[0]


def _md_files(source_dir: str) -> list:
    """遍历源目录，返回所有 .md 文件（递归，按路径排序）。

    排除非设定目录：03_章节/、04_草稿/、.obsidian/（只入定稿设定数据）。
    """
    SKIP_DIRS = {"03_章节", "04_草稿", ".obsidian"}
    files = []
    for root, dirs, names in os.walk(source_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if name.lower().endswith(".md"):
                files.append(os.path.join(root, name))
    return sorted(files)


def _rel_path(fp: str, source_dir: str) -> str:
    """源文件相对 vault 根的正斜杠路径（与 --source 指向哪个目录无关，
    二者顶层结构一致：01_世界观/ 02_角色/）。"""
    return os.path.relpath(fp, source_dir).replace("\\", "/")


def _load_existing_map(store) -> dict:
    """遍历现有 domain=novel 节点，建立 {source_path: {"node_id": int,
    "mtime": float|None}} 映射。老数据无 source_mtime 时为 None（视为未知）。
    """
    mapping = {}
    for nid, payload in store.iter_payloads():
        if payload.get("type") != CHUNK_TYPE:
            continue
        if payload.get("domain") != DOMAIN:
            continue
        rel = payload.get("source_path", "")
        if not rel:
            continue
        entry = mapping.setdefault(rel, {"node_id": nid, "mtime": None})
        if entry["mtime"] is None:
            entry["mtime"] = payload.get("source_mtime")
        entry["node_id"] = nid
    return mapping


def _count_by_kind(store) -> dict:
    """扫描库中全部 domain=novel 节点，按 kind 统计块数，返回 {kind: count}。"""
    counts = {}
    for nid, payload in store.iter_payloads():
        if payload.get("type") != CHUNK_TYPE:
            continue
        if payload.get("domain") != DOMAIN:
            continue
        k = payload.get("kind", "unknown")
        counts[k] = counts.get(k, 0) + 1
    return counts


def _count_novel_nodes(store) -> int:
    """统计库中全部 domain=novel 节点数。"""
    total = 0
    for nid, payload in store.iter_payloads():
        if payload.get("type") != CHUNK_TYPE:
            continue
        if payload.get("domain") == DOMAIN:
            total += 1
    return total


def _build_payload(rel_path: str, content: str, kind: str,
                   title: str, mtime: float) -> dict:
    """组装单个节点的 payload（type/domain/kind/title/source_path/
    source_mtime/importance/content/status）。"""
    return {
        "type": CHUNK_TYPE,
        "domain": DOMAIN,
        "kind": kind,
        "title": title,
        "source_path": rel_path,
        "source_mtime": mtime,
        "importance": IMPORTANCE,
        "content": content,
        "status": "active",
    }


def _upsert_node(store, payload: dict, content: str, existing: dict) -> str:
    """以 upsert 方式写入单个文件节点：已存在则保持 node id 不变
    （update_payload + update_vector），否则 insert_node。

    返回 "inserted" | "updated"。向量化失败时抛异常（由调用方计入 failed）。
    """
    rel = payload["source_path"]
    emb = store.embed_text(content)
    entry = existing.get(rel)
    if entry is not None:
        store.update_payload(entry["node_id"], payload)
        store.update_vector(entry["node_id"], emb)
        return "updated"
    store.insert_node(payload, emb)
    return "inserted"


def build(source: str = None, store=None, full: bool = False) -> dict:
    """构建小说设定库索引（v1.0）。

    full=True（--full）：先删除库里所有 domain=novel 旧节点（delete_node 连带
        清边）再全量重建；防止重复跑产生重复节点。
    full=False（默认）：增量——遍历已有节点建立 {source_path: mtime} 映射，
        只处理新文件或 mtime 变化的文件（upsert 保持 id），孤儿（源文件已删除）
        节点一并清理。

    单文件向量化 / 写入失败收集到 failed，不中断整体。
    返回统计 dict {mode, processed_files, inserted, updated, deleted, failed,
        total_novel_nodes, by_kind}。
    """
    if not source:
        raise ValueError("source 必填：小说 vault 根目录（--source 或 PALIMPSEST_NOVEL_DIR）")
    store = store or TriviumStore()
    md_files = _md_files(source)
    existing = _load_existing_map(store)

    inserted = 0
    updated = 0
    deleted = 0
    failed = 0
    failed_paths = []
    processed_files = 0

    if full:
        # 全量模式：先清空所有旧 domain=novel 节点（连带其图谱边），再全量重建
        for rel, entry in list(existing.items()):
            try:
                store.delete_node(entry["node_id"])
                deleted += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                failed_paths.append(rel)
        existing = {}
        processed_files = len(md_files)
        for fp in md_files:
            rel = _rel_path(fp, source)
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                mtime = os.path.getmtime(fp)
                content = _strip_frontmatter(text)
                if not content.strip():
                    continue  # 空文件/空正文跳过（如世界观占位文件），不计失败
                kind = _kind_of(rel)
                title = _extract_title(rel, content, kind)
                payload = _build_payload(rel, content, kind, title, mtime)
                result = _upsert_node(store, payload, content, existing)
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                failed_paths.append(rel)
    else:
        # 增量模式：处理新文件 / mtime 变化文件（upsert），跳过未变化文件
        known_paths = set()
        for fp in md_files:
            rel = _rel_path(fp, source)
            known_paths.add(rel)
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                mtime = os.path.getmtime(fp)
                content = _strip_frontmatter(text)
                if not content.strip():
                    continue  # 空文件/空正文跳过（如世界观占位文件），不计失败
                kind = _kind_of(rel)
                title = _extract_title(rel, content, kind)
                payload = _build_payload(rel, content, kind, title, mtime)
                entry = existing.get(rel)
                if entry is not None and entry["mtime"] is not None \
                        and abs(entry["mtime"] - mtime) <= MTIME_TOLERANCE:
                    continue  # mtime 未变，跳过
                processed_files += 1
                result = _upsert_node(store, payload, content, existing)
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                failed_paths.append(rel)
        # 孤儿清理：existing 中有但磁盘上已不存在的源文件旧节点删除
        for rel, entry in list(existing.items()):
            if rel in known_paths:
                continue
            try:
                store.delete_node(entry["node_id"])
                deleted += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                failed_paths.append(rel)

    total_novel_nodes = _count_novel_nodes(store)
    by_kind = _count_by_kind(store)

    return {
        "mode": "full" if full else "incremental",
        "processed_files": processed_files,
        "inserted": inserted,
        "updated": updated,
        "deleted": deleted,
        "failed": failed,
        "failed_paths": failed_paths,
        "total_novel_nodes": total_novel_nodes,
        "by_kind": by_kind,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="小说设定库索引构建（默认增量 mtime 对比，--full 全量重建）")
    parser.add_argument("--full", action="store_true",
                        help="全量重建：先删除所有 domain=novel 旧节点再重建全部文件")
    parser.add_argument("--source", default=DEFAULT_SOURCE_DIR,
                        help="novel vault 根目录（必填；或设环境变量 PALIMPSEST_NOVEL_DIR）")
    args = parser.parse_args()
    if not args.source:
        parser.error("--source 必填：本地小说 vault 根目录（个人路径不入仓库，请显式传入）")

    import json
    print(f"novel vault 根目录: {args.source}")
    print(f"模式: {'全量重建' if args.full else '增量更新（mtime 对比）'}")
    result = build(source=args.source, full=args.full)
    # 只输出统计 JSON，不打印小说正文内容（避免刷屏）
    print(json.dumps({k: v for k, v in result.items() if k != "failed_paths"},
                     ensure_ascii=False))
    if result["failed"]:
        print(f"失败文件 {result['failed']} 个: {result['failed_paths'][:10]}",
              file=__import__("sys").stderr)
