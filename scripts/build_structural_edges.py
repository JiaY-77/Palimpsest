"""结构化建边器：从显式结构化字段推算/写入图谱边。

背景：生产库 88% 节点是孤岛。本脚本从 6 类显式关系补边：
  dry-run（默认）只统计不写库；--apply 才真正写入（带 batch_id 可回滚）。

6 类边源：
  1. source_path 文档枢纽边（PART_OF）—— 同文档节点的聚合
  2. chunk_index 文档内顺序边（PREV/NEXT）—— 同文档 chunk 的先后
  3. 技能 related_skills（SKILL_RELATED_TO）—— 从技能 md 的 YAML 头读
  4. _novel_relations.json 人物关系
  5. Obsidian [[双链]]（WIKI_LINK）
  6. 名称提及（MENTIONS）—— 仅统计

治本设计（防「重建后边丢失」）：
  - **绝不依赖节点 id**：一切按 source_path / 名称定位端点；
  - 幂等：建边前查 (src,dst,label) 是否已存在；
  - 可回滚：每条边写入时带上 `batch_id`，`--rollback <batch_id>` 一次撤销；
  - 重建后自动重跑：由 build_kb_index.py / build_skill_index.py 收尾调用。

用法：
    venv/Scripts/python.exe scripts/build_structural_edges.py                    # dry-run
    venv/Scripts/python.exe scripts/build_structural_edges.py --apply            # 真正写库
    venv/Scripts/python.exe scripts/build_structural_edges.py --rollback <batch> # 回滚
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import Config  # noqa: E402
from core.trivium_store import TriviumStore  # noqa: E402


def _set_db(path: str) -> str:
    """把库路径同时写入环境变量与 Config。

    Bug 1 根因：`Config.DB_PATH` 在 config 模块 **import 时** 就求值成类属性
    （_resolve_db_path() 默认返回 <root>/data/mh_memory.db）。TriviumStore.__init__
    读的是这个类属性，因此「import 之后再设 os.environ["DB_PATH"]」永远不生效。
    这里在 import Config 之后、实例化 TriviumStore 之前，就地把 Config.DB_PATH
    显式改掉——这是唯一可靠的覆盖点。
    """
    env_path = path
    if not os.path.isabs(env_path):
        env_path = os.path.join(_PROJECT_ROOT, env_path)
    os.environ["DB_PATH"] = env_path
    Config.DB_PATH = env_path
    return env_path


# ── 常量 ──────────────────────────────────────────────────────────────
# 技能目录 / 知识库目录不硬编码：默认从项目既有机制解析（--skills-dir / --vault-dir 可覆盖）
_DEFAULT_SKILLS_DIR = ""
_DEFAULT_VAULT_DIR = ""
try:
    from scripts.build_skill_index import resolve_skills_dir

    _DEFAULT_SKILLS_DIR = resolve_skills_dir()
except Exception:  # noqa: BLE001
    _DEFAULT_SKILLS_DIR = ""
try:
    from mcp_tools._common import KNOWLEDGE_DIR

    _DEFAULT_VAULT_DIR = KNOWLEDGE_DIR
except Exception:  # noqa: BLE001
    _DEFAULT_VAULT_DIR = ""

SKILLS_DIR = _DEFAULT_SKILLS_DIR
VAULT_DIR = _DEFAULT_VAULT_DIR
NOVEL_RELATIONS = os.path.join(_PROJECT_ROOT, "_novel_relations.json")
LOG_TYPES = {"record", "event", "git_commit", "review"}

W_PART_OF = 0.8
W_NEXT = 0.7
W_SKILL = 1.0
W_NOVEL = 0.8
W_WIKI = 1.0
W_MENTION = 0.6

# 边类型 -> 权重（写库时用）
W_OF = {
    "PART_OF": W_PART_OF,
    "NEXT": W_NEXT,
    "SKILL_RELATED_TO": W_SKILL,
    "WIKI_LINK": W_WIKI,
    "MENTIONS": W_MENTION,
}


# ── 工具 ──────────────────────────────────────────────────────────────
def _title_of(p: dict) -> str:
    for k in ("title", "name", "label", "canonical_name"):
        v = p.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _load_graph(store: TriviumStore):
    """单连接一次取回全部节点 payload 与全部出边。

    性能根因：`store.get_node()` / `store.get_edges()` 每次都 `_acquire()` 开/关
    一次库连接（含 generation 校验），逐节点调用是 N+1 次连接——1540 节点 ×3 轮
    ≈ 4600 次开关库，单次 ~50ms 就是 4 分钟（实测撞 --apply 超时）。
    这里在一个 `_acquire()` 内完成全部读取：实测 1540 节点取边 0.00s、取 payload 0.05s。

    返回 (ids, nodes, edges)：
      ids   : list[int] 全节点 id
      nodes : {nid: payload_dict}
      edges : {(src, dst, label_upper)} 全库出边集合（供查重与基线度统计）
    """
    ids: list[int] = []
    nodes: dict[int, dict] = {}
    edges: set[tuple[int, int, str]] = set()
    with store._acquire() as db:
        ids = list(db.all_node_ids())
        for nid in ids:
            node = db.get(nid)
            if node is not None:
                nodes[nid] = node.payload or {}
            for e in db.get_edges(nid) or []:
                tgt = getattr(e, "target_id", None)
                if tgt is None:
                    continue
                lab = str(getattr(e, "label", "") or "").upper()
                edges.add((int(nid), int(tgt), lab))
    return ids, nodes, edges


# ── 主流程 ────────────────────────────────────────────────────────────
def _rollback(batch_id: str) -> int:
    """按脚本侧批次清单回滚该批次写入的边（只删本批次记录的三元组）。"""
    manifest = os.path.join(_PROJECT_ROOT, "data", "edge_batches", f"{batch_id}.json")
    if not os.path.exists(manifest):
        print(f"[回滚] 找不到批次清单: {manifest}")
        return 1
    with open(manifest, encoding="utf-8") as fh:
        entries = json.load(fh)
    store = TriviumStore()
    removed = 0
    # 单连接一次删完（delete_edge 每条都开/关一次库，921 条 ≈ 失控的 N 次连接；
    # 这里直接在一个 _acquire() 内按三元组 unlink，语义与 delete_edge 一致）。
    with store._acquire() as db:
        for e in entries:
            try:
                edges = db.get_edges(e["src"]) or []
                if any(
                    getattr(x, "target_id", None) == e["dst"] and str(getattr(x, "label", "") or "") == e["type"]
                    for x in edges
                ):
                    db.unlink(e["src"], e["dst"], e["type"])
                    removed += 1
            except Exception as ex:  # noqa: BLE001
                print(f"  删除失败 {e}: {ex}")
    print(f"[回滚] batch={batch_id} 已删除 {removed}/{len(entries)} 条边")
    if removed == len(entries):
        os.remove(manifest)
        print("[回滚] 批次清单已移除")
    return 0


def build_edges(
    store,
    skills_dir: str | None = None,
    vault_dir: str | None = None,
    apply: bool = False,
    batch_id: str | None = None,
    report: bool = False,
) -> dict:
    """从显式结构字段推算并（可选）写入图谱边。

    返回 dict，至少含这些键：
        nodes            int   全库节点数
        candidates       int   去重后候选边数
        created          int   实际写入边数（apply=False 时为 0）
        skipped          int   已存在而跳过的边数
        base_with_edge   int   建边前「有边节点」数
        pred_with_edge   int   建边后预测/实际「有边节点」数
        batch_id         str|None  apply=True 时生成的批次 id
        manifest         str|None  apply=True 时批次清单路径

    store 由调用方传入：本函数不调用 _set_db、不写 os.environ["DB_PATH"]、
    不实例化 TriviumStore。skills_dir / vault_dir 为 None 时沿用模块级
    SKILLS_DIR / VAULT_DIR 的当前值（CLI 的 --skills-dir/--vault-dir 已在
    main 里覆盖过这两个全局变量）。report=False 只返回统计、不打印。
    """

    def out(msg: str) -> None:
        if report:
            print(msg)

    skills = SKILLS_DIR if skills_dir is None else skills_dir
    vault = VAULT_DIR if vault_dir is None else vault_dir

    if apply and batch_id is None:
        batch_id = f"structural-{time.strftime('%Y%m%d-%H%M%S')}"
    manifest: str | None = None

    ids, nodes, existing = _load_graph(store)

    # 基线（用已取回的边集，不再逐节点开连接）
    deg = collections.Counter()
    for s, d, _lab in existing:
        deg[s] += 1
        deg[d] += 1
    base_with_edge = {n for n, v in deg.items() if v > 0}
    base_out = len(existing)

    out("=== 结构化建边 dry-run 报告 ===")
    out(f"库: {Config.DB_PATH}   节点数: {len(ids)}")
    out(
        f"\n[基线] 有边节点: {len(base_with_edge)} "
        f"({len(base_with_edge) / len(ids) * 100:.1f}%)   总出边: {base_out}   "
        f"孤立: {len(ids) - len(base_with_edge)} "
        f"({(len(ids) - len(base_with_edge)) / len(ids) * 100:.1f}%)"
    )

    # 全部候选边（用于最终去重与孤立率预测）
    cand: list[tuple[int, int, str]] = []  # (src, dst, type)
    cand_nodes: set[int] = set()  # 涉及到的节点

    # ── 源 1：source_path 文档枢纽（PART_OF）──────────────────────────
    sp_groups: dict[str, list[int]] = collections.defaultdict(list)
    for nid, p in nodes.items():
        sp = p.get("source_path")
        if sp:
            sp_groups[str(sp)].append(nid)
    # 文档枢纽 = 该 source_path 的「代表节点」（组内第一个）
    sp_hub: dict[str, int] = {sp: members[0] for sp, members in sp_groups.items()}
    src1_edges = 0
    src1_nodes: set[int] = set()
    for sp, members in sp_groups.items():
        hub = sp_hub[sp]
        for nid in members:
            if nid != hub:
                cand.append((nid, hub, "PART_OF"))
                src1_nodes.add(nid)
                src1_nodes.add(hub)
                src1_edges += 1
    out(
        f"\n[源 1 source_path]     候选边: {src1_edges}   涉及节点: {len(src1_nodes)}"
        f"   分组: {len(sp_groups)} (多节点组 {sum(1 for v in sp_groups.values() if len(v) > 1)})"
    )

    # ── 源 2：chunk_index 顺序边（NEXT）──────────────────────────────
    src2_edges = 0
    for _sp, members in sp_groups.items():
        withci = [(n, nodes[n].get("chunk_index")) for n in members if isinstance(nodes[n].get("chunk_index"), int)]
        if len(withci) <= 1:
            continue
        withci.sort(key=lambda x: x[1])
        for i in range(len(withci) - 1):
            a, b = withci[i][0], withci[i + 1][0]
            cand.append((a, b, "NEXT"))
            cand_nodes.add(a)
            cand_nodes.add(b)
            src2_edges += 1
    out(
        f"[源 2 chunk_index]     候选边: {src2_edges}   涉及节点: "
        f"{len({s for s, _, t in cand if t == 'NEXT'}) + len({d for _, d, t in cand if t == 'NEXT'})}"
    )

    # ── 源 3：技能 related_skills ─────────────────────────────────────
    sk_nodes: dict[str, int] = {}  # name -> nid（排除 .archive）
    for nid, p in nodes.items():
        if p.get("type") != "skill_chunk":
            continue
        cat = str(p.get("category") or "")
        if cat.startswith(".archive/"):
            continue
        nm = p.get("name") or p.get("title")
        if nm:
            sk_nodes[str(nm).strip()] = nid

    skill_files = [
        f for f in glob.glob(skills + "/**/SKILL.md", recursive=True) if ".archive" not in f.replace("\\", "/")
    ]
    src3_edges = 0
    unresolved_skills: list[str] = []
    for f in skill_files:
        try:
            with open(f, encoding="utf-8") as fh:
                txt = fh.read()
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---", txt, re.DOTALL)
        if not m:
            continue
        fm = m.group(1)
        nm_m = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
        name = nm_m.group(1).strip().strip('"') if nm_m else None
        rs_m = re.search(r"related_skills:\s*\[([^\]]*)\]", fm)
        if not (name and rs_m and name in sk_nodes):
            continue
        for tgt_name in [t.strip().strip("\"'") for t in rs_m.group(1).split(",") if t.strip()]:
            if tgt_name in sk_nodes:
                cand.append((sk_nodes[name], sk_nodes[tgt_name], "SKILL_RELATED_TO"))
                cand.append((sk_nodes[tgt_name], sk_nodes[name], "SKILL_RELATED_TO"))
                src3_edges += 2
            else:
                unresolved_skills.append(f"{name}->{tgt_name}")
    out(
        f"[源 3 技能相关]         候选边: {src3_edges}   涉及技能: {len(sk_nodes)}   "
        f"未解析: {len(unresolved_skills)} {unresolved_skills[:5]}"
    )

    # ── 源 4：_novel_relations.json ───────────────────────────────────
    src4_edges = 0
    matched = 0
    unresolved_chars: list[str] = []
    # 角色节点 = domain=novel，kind=character，title 精确匹配
    char_nodes: dict[str, int] = {}
    for nid, p in nodes.items():
        if p.get("kind") == "character" or (p.get("domain") == "novel" and p.get("type") == "novel_chunk"):
            t = _title_of(p)
            if t:
                char_nodes[t] = nid
    if os.path.exists(NOVEL_RELATIONS):
        with open(NOVEL_RELATIONS, encoding="utf-8") as fh:
            rels = json.load(fh)
        for r in rels:
            s, t = str(r.get("source", "")).strip(), str(r.get("target", "")).strip()
            if s in char_nodes and t in char_nodes:
                matched += 1
                cand.append((char_nodes[s], char_nodes[t], str(r.get("type", "RELATED_TO"))))
                cand.append((char_nodes[t], char_nodes[s], str(r.get("type", "RELATED_TO"))))
                src4_edges += 2
            else:
                if s not in char_nodes:
                    unresolved_chars.append(s)
                if t not in char_nodes:
                    unresolved_chars.append(t)
        out(
            f"[源 4 小说关系]         候选边: {src4_edges}   解析成功: {matched}/{len(rels)}   "
            f"未解析: {len(set(unresolved_chars))} {sorted(set(unresolved_chars))[:6]}"
        )
    else:
        out("[源 4 小说关系]         (文件不存在，跳过)")

    # ── 源 5：Obsidian 双链 ───────────────────────────────────────────
    src5_edges = 0
    src5_ok = 0
    unresolved_links: list[str] = []
    _wiki_seen: set[tuple[int, int, str]] = set()
    # 文档节点映射：source_path(相对) -> 组内节点集合
    path_to_nodes: dict[str, list[int]] = {}
    for nid, p in nodes.items():
        sp = p.get("source_path")
        if sp:
            path_to_nodes.setdefault(str(sp).replace("\\", "/"), []).append(nid)

    if os.path.isdir(vault):
        mds = glob.glob(vault + "/**/*.md", recursive=True)
        link_re = re.compile(r"\[\[([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")
        total_links = 0
        # 文档级：源文档枢纽 -> 目标文档枢纽，每个 (源文档,目标文档) 只建 1 条
        for f in mds:
            rel = os.path.relpath(f, vault).replace("\\", "/")
            src_hub = sp_hub.get(rel)
            if src_hub is None:
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    txt = fh.read()
            except OSError:
                continue
            for m in link_re.finditer(txt):
                tgt_name = m.group(1).strip()
                if not tgt_name:
                    continue
                total_links += 1
                tgt_hub = None
                for path, hub in sp_hub.items():
                    if os.path.splitext(os.path.basename(path))[0] == tgt_name:
                        tgt_hub = hub
                        break
                if tgt_hub is not None and tgt_hub != src_hub:
                    key = (src_hub, tgt_hub, "WIKI_LINK")
                    if key not in _wiki_seen:
                        _wiki_seen.add(key)
                        cand.append(key)
                        src5_ok += 1
                else:
                    unresolved_links.append(tgt_name)
        src5_edges = src5_ok
        out(
            f"[源 5 双链]            候选边: {src5_edges}   解析成功: {src5_ok}/{total_links}   "
            f"未解析目标: {len(set(unresolved_links))} {sorted(set(unresolved_links))[:6]}"
        )

    # ── 源 6：名称提及（仅统计）───────────────────────────────────────
    mention_names = set(sk_nodes) | set(char_nodes)
    mention_names |= {_title_of(p) for p in nodes.values() if _title_of(p)}
    mention_names = {n for n in mention_names if n and len(n) >= 2}
    src6_est = 0
    for nid, p in nodes.items():
        if p.get("type") in LOG_TYPES or p.get("type") == "task":
            content = str(p.get("content") or "")
            for nm in mention_names:
                if nm in content and sk_nodes.get(nm) != nid and char_nodes.get(nm) != nid:
                    src6_est += 1
                    break  # 每节点最多计 1
    out(f"[源 6 名称提及]         候选边(估): {src6_est}   (词典 {len(mention_names)} 名称，仅统计)")

    # ── 汇总 ──────────────────────────────────────────────────────────
    # 源 6（名称提及）未进 cand；dry-run 的孤立率预测只算可写库的结构边，
    # 故按 uniq 推导涉及节点，避免高估。
    uniq = {(s, d, t) for s, d, t in cand}
    all_touched: set[int] = set()
    for s, d, _ in uniq:
        all_touched.add(s)
        all_touched.add(d)
    predicted = base_with_edge | all_touched
    iso = len(ids) - len(predicted)

    out(f"\n[合计] 候选边(去重): {len(uniq)}   (未去重 {len(cand)})")
    out(
        f"[预测] 有边节点: {len(predicted)} ({len(predicted) / len(ids) * 100:.1f}%)   "
        f"孤立率: {iso / len(ids) * 100:.1f}%   (基线 {len(base_with_edge) / len(ids) * 100:.1f}%"
        f" → 目标 <30~40%)"
    )

    # 各类型度 top10
    deg2 = collections.Counter()
    for s, d, _ in uniq:
        deg2[s] += 1
        deg2[d] += 1
    out("\n[按预测度 top10]")
    for nid, cnt in deg2.most_common(10):
        t = _title_of(nodes.get(nid, {})) or str(nodes.get(nid, {}).get("content", ""))[:30]
        out(f"  {cnt:4d}  {nodes.get(nid, {}).get('type', '?'):12s} {t}")

    # ── 写库（仅 --apply）─────────────────────────────────────────────
    # Bug 2：逐条 _edge_exists() 都要 get_edges(src) 全量遍历，915 条 × 全库边
    # 会跑到超时。改为一次性预取所有 (src,dst,label) 建成集合，之后 O(1) 查重。
    # （`existing` 已由 _load_graph 单连接取回，这里直接复用，不再重复开库。）
    # 性能：create_edge 每条也开/关一次库（921 条 ≈ 1 分钟），这里改为单连接内
    # 直接 db.link()，与 create_edge 语义一致（同三元组幂等）。

    created = skipped = 0
    written: list[dict] = []
    with store._acquire() as db:
        for s, d, t in sorted(uniq):
            if s == d:  # 自环跳过
                skipped += 1
                continue
            if (s, d, t.upper()) in existing:
                skipped += 1
                continue
            if not apply:  # dry-run：只统计，不写库
                continue
            w = W_OF.get(t, 0.8)
            try:
                db.link(s, d, label=t, weight=w)
                existing.add((s, d, t.upper()))  # 同批次内也防重
                created += 1
                written.append({"src": s, "dst": d, "type": t, "weight": w})
            except Exception as ex:  # noqa: BLE001
                out(f"  建边失败 {s}->{d} ({t}): {ex}")

    if apply:
        # 记录批次清单（供 --rollback）
        manifest_dir = os.path.join(_PROJECT_ROOT, "data", "edge_batches")
        os.makedirs(manifest_dir, exist_ok=True)
        manifest = os.path.join(manifest_dir, f"{batch_id}.json")
        with open(manifest, "w", encoding="utf-8") as fh:
            json.dump(written, fh, ensure_ascii=False)

        out(f"\n[apply] batch_id = {batch_id}")
        out(f"[apply] 已建: {created} 条 | 已存在跳过: {skipped} 条")
        out(f"[apply] 批次清单: {manifest}")
        out(f"[回滚] venv/Scripts/python.exe scripts/build_structural_edges.py --rollback {batch_id}")

    return {
        "nodes": len(ids),
        "candidates": len(uniq),
        "created": created,
        "skipped": skipped,
        "base_with_edge": len(base_with_edge),
        "pred_with_edge": len(predicted),
        "batch_id": batch_id if apply else None,
        "manifest": manifest,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/mh_memory.db")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--rollback", metavar="BATCH_ID", help="按 batch_id 回滚该批次写入的边")
    ap.add_argument("--batch-id", default=None, help="指定本批次 id（默认自动生成时间戳；供测试与自动化定位）")
    ap.add_argument("--skills-dir", default=None, help="技能目录（默认自动解析）")
    ap.add_argument("--vault-dir", default=None, help="Obsidian 知识库目录（默认 KNOWLEDGE_DIR）")
    args = ap.parse_args()

    global SKILLS_DIR, VAULT_DIR
    if args.skills_dir:
        SKILLS_DIR = os.path.abspath(args.skills_dir)
    if args.vault_dir:
        VAULT_DIR = os.path.abspath(args.vault_dir)

    if args.rollback:
        _set_db(args.db)
        return _rollback(args.rollback)

    _set_db(args.db)
    store = TriviumStore(read_only=not args.apply)

    build_edges(store, apply=args.apply, report=True, batch_id=args.batch_id)
    if not args.apply:
        print("\n[安全] dry-run 模式，未写入任何数据 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
