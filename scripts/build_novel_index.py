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
import dataclasses
import os

# 确保能 import 项目 core 模块（以项目根为基准，_common 导入即把项目根注入 sys.path）
import _common  # noqa: F401

from core.index_rules import IndexRules, is_included, load_rules, match_kind
from core.trivium_store import TriviumStore

# 节点类型与域（与 mcp_server 的 novel 区块检索条件保持一致）
CHUNK_TYPE = "novel_chunk"
DOMAIN = "novel"

# 库根约定文件名：通用名优先，创作 vault 专用的旧名作为兼容回退。
# 优先级：--rules > <vault>/.palimpsest-novel-index.json > <vault>/.palimpsest-index.json > 内置默认
NOVEL_RULES_FILENAME = ".palimpsest-novel-index.json"

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


def _has_frontmatter_id(text: str) -> bool:
    """检查 frontmatter 中是否存在 id: 键。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return False
        stripped = lines[i].strip()
        if stripped.startswith(("id:", "id :")):
            return True
    return False


def _kind_of(rel_path: str, rules: IndexRules | None = None) -> str:
    """按相对库根路径判定节点 kind（委托给规则引擎）。

    rel_path 一律是「相对库根」的正斜杠路径。

    契约：**解析不出 kind 就返回 default_kind**（默认 "default"），绝不伪装成某个
    具体业务 kind。真实的误判事故（角色卡从 02_角色/ 迁到 01_世界观/03_角色/ 后 77 张卡
    全被归类为 setting、脚本照常退出无任何提示）就源于旧实现的 `return "character"`
    兜底——先把「不知道」写成「知道」，再被静默接受。未匹配路径由调用方收集并显式列出。
    """
    if rules is None:
        rules = load_rules()
    return match_kind(rel_path, rules)


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


def _md_files(source_dir: str, rules: IndexRules | None = None) -> list:
    """遍历源目录，返回所有 .md 文件（递归，按路径排序）。

    使用规则的 exclude/include 过滤路径。
    """
    if rules is None:
        rules = load_rules()
    files = []
    for root, dirs, names in os.walk(source_dir):
        # 过滤目录：把目录名转为相对路径后用 is_included 检查
        filtered_dirs = []
        for d in dirs:
            abs_dir = os.path.join(root, d)
            rel_dir = os.path.relpath(abs_dir, source_dir).replace("\\", "/")
            if is_included(rel_dir, True, rules):
                filtered_dirs.append(d)
        dirs[:] = filtered_dirs
        for name in names:
            if name.lower().endswith(".md"):
                abs_fp = os.path.join(root, name)
                rel_fp = os.path.relpath(abs_fp, source_dir).replace("\\", "/")
                if is_included(rel_fp, False, rules):
                    files.append(abs_fp)
    return sorted(files)


def _rel_path(fp: str, source_dir: str) -> str:
    """源文件相对 vault 根的正斜杠路径。"""
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
    for _nid, payload in store.iter_payloads():
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
    for _nid, payload in store.iter_payloads():
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


def build(source: str | None = None, store=None, full: bool = False,
          rules: IndexRules | str | None = None) -> dict:
    """构建小说设定库索引（v1.0）。

    full=True（--full）：先删除库里所有 domain=novel 旧节点（delete_node 连带
        清边）再全量重建；防止重复跑产生重复节点。
    full=False（默认）：增量——遍历已有节点建立 {source_path: mtime} 映射，
        只处理新文件或 mtime 变化的文件（upsert 保持 id），孤儿（源文件已删除）
        节点一并清理。

    rules: IndexRules 对象、规则文件路径字符串、或 None（按库根约定文件 + 内置默认）。
    单文件向量化 / 写入失败收集到 failed，不中断整体。
    返回统计 dict {mode, processed_files, inserted, updated, deleted, failed,
        total_novel_nodes, by_kind, unmatched_paths, rules_source, rules_warnings}。

    unmatched_paths：kind 落到 default_kind（规则未匹配）的文件路径，显式列出而不是
    静默归并——旧实现把「不知道」兜底成 character，误判时全程无声。
    """
    if not source:
        raise ValueError("source 必填：小说 vault 根目录（--source 或 PALIMPSEST_NOVEL_DIR）")

    # 加载规则
    if rules is None:
        rules_obj = load_rules(root=source, legacy_filename=NOVEL_RULES_FILENAME)
    elif isinstance(rules, str):
        rules_obj = load_rules(root=source, explicit=rules)
    else:
        rules_obj = rules

    store = store or TriviumStore()
    md_files = _md_files(source, rules_obj)
    existing = _load_existing_map(store)

    inserted = 0
    updated = 0
    deleted = 0
    failed = 0
    failed_paths = []
    processed_files = 0
    unmatched_paths: list[str] = []

    if full:
        # 全量模式：先清空所有旧 domain=novel 节点（连带其图谱边），再全量重建
        for rel, entry in list(existing.items()):
            try:
                store.delete_node(entry["node_id"])
                deleted += 1
            except Exception:  # noqa: BLE001 —— 旧节点删除失败计数后跳过继续清库
                failed += 1
                failed_paths.append(rel)
        existing = {}
        processed_files = len(md_files)
        for fp in md_files:
            rel = _rel_path(fp, source)
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                mtime = os.path.getmtime(fp)
                content = _strip_frontmatter(text)
                if not content.strip():
                    continue  # 空文件/空正文跳过（如世界观占位文件），不计失败
                # require_frontmatter_id 检查
                if rules_obj.require_frontmatter_id and not _has_frontmatter_id(text):
                    unmatched_paths.append(rel)
                    continue
                kind = _kind_of(rel, rules_obj)
                title = _extract_title(rel, content, kind)
                payload = _build_payload(rel, content, kind, title, mtime)
                result = _upsert_node(store, payload, content, existing)
                if result == "inserted":
                    inserted += 1
                else:
                    updated += 1
                if kind == rules_obj.default_kind:
                    unmatched_paths.append(rel)
            except Exception:  # noqa: BLE001 —— 单文件处理失败计数后跳过继续其余文件
                failed += 1
                failed_paths.append(rel)
    else:
        # 增量模式：处理新文件 / mtime 变化文件（upsert），跳过未变化文件
        known_paths = set()
        for fp in md_files:
            rel = _rel_path(fp, source)
            known_paths.add(rel)
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                mtime = os.path.getmtime(fp)
                content = _strip_frontmatter(text)
                if not content.strip():
                    continue  # 空文件/空正文跳过（如世界观占位文件），不计失败
                # require_frontmatter_id 检查
                if rules_obj.require_frontmatter_id and not _has_frontmatter_id(text):
                    unmatched_paths.append(rel)
                    continue
                kind = _kind_of(rel, rules_obj)
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
                if kind == rules_obj.default_kind:
                    unmatched_paths.append(rel)
            except Exception:  # noqa: BLE001 —— 增量处理失败计数后跳过继续其余文件
                failed += 1
                failed_paths.append(rel)
        # 孤儿清理：existing 中有但磁盘上已不存在的源文件旧节点删除
        for rel, entry in list(existing.items()):
            if rel in known_paths:
                continue
            try:
                store.delete_node(entry["node_id"])
                deleted += 1
            except Exception:  # noqa: BLE001 —— 孤儿节点删除失败计数后跳过继续清理
                failed += 1
                failed_paths.append(rel)

    total_novel_nodes = _count_novel_nodes(store)
    by_kind = _count_by_kind(store)
    unmatched_paths.sort()

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
        "unmatched_paths": unmatched_paths,
        "rules_source": rules_obj.source,
        "rules_warnings": list(rules_obj.warnings),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="小说设定库索引构建（默认增量 mtime 对比，--full 全量重建）")
    parser.add_argument("--full", action="store_true",
                        help="全量重建：先删除所有 domain=novel 旧节点再重建全部文件")
    parser.add_argument("--source", default=DEFAULT_SOURCE_DIR,
                        help="novel vault 根目录（必填；或设环境变量 PALIMPSEST_NOVEL_DIR）")
    parser.add_argument("--rules", default=None,
                        help="索引规则 JSON 文件路径（不传则使用 vault 根 .palimpsest-index.json 或内置默认）")
    parser.add_argument("--require-frontmatter-id", action="store_true",
                        help="仅索引 frontmatter 含 id: 键的文件，其余跳过并计入未匹配")
    args = parser.parse_args()
    if not args.source:
        parser.error("--source 必填：本地小说 vault 根目录（个人路径不入仓库，请显式传入）")

    import json
    rules_arg = (load_rules(root=args.source, explicit=args.rules)
                 if args.rules
                 else load_rules(root=args.source, legacy_filename=NOVEL_RULES_FILENAME))
    if args.require_frontmatter_id:
        # 用户显式 --require-frontmatter-id 时强制开启
        rules_arg = dataclasses.replace(rules_arg, require_frontmatter_id=True)
    for w in rules_arg.warnings:
        print(f"[警告] {w}")
    print(f"novel vault 根目录: {args.source}")
    print(f"规则来源: {rules_arg.source}")
    print(f"模式: {'全量重建' if args.full else '增量更新（mtime 对比）'}")
    result = build(source=args.source, full=args.full, rules=rules_arg)
    # 只输出统计 JSON，不打印小说正文内容（避免刷屏）
    print(json.dumps({k: v for k, v in result.items() if k != "failed_paths"},
                     ensure_ascii=False))
    if result["failed"]:
        print(f"失败文件 {result['failed']} 个: {result['failed_paths'][:10]}",
              file=__import__("sys").stderr)
    if result["unmatched_paths"]:
        shown = result["unmatched_paths"]
        print(f"未匹配 kind 的文件 {len(shown)} 个（default_kind 兜底）:")
        for p in shown[:10]:
            print(f"  {p}")
