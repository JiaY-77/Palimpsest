"""Gate 0：实体锚定层的图谱覆盖审计（PR0）。

回答一个证伪问题：**图里到底有没有「实体锚定 + 多跳扩散」能吃到的增量？**

做法（只读，不动任何产品代码/数据）：
  1. 用最小 EntityIndex 原型（词典 = 节点标题/别名 + 有边节点名 + kb/skill 标题），
     对评测集每条 query 做词面锚点识别（离线、零依赖、不用 embedding）。
  2. 对每个锚点沿现有图做 1-2 跳 BFS 扩散，收集可达节点集合 R。
  3. 对比「金标节点」是否落在 R 内，以及是否**超出**现有 sem∪fts 的 top30（真实增量上限）。

输出四个数：
  trigger_rate   识别出 ≥1 锚点的查询占比
  reach@2        金标节点落在锚点 2 跳内的查询占比
  novel_reach@2  金标落在 2 跳内、且不在 sem∪fts top30 的查询占比（真实增量）
  anchor_precision 抽样的锚点识别是否合理（人工看，脚本只导出）

判定（对齐规划 §7.2）：
  novel_reach@2 < 5%  或  reach@2 < 15%  → 停止 M1（图太稀疏，应先做写边）
  trigger_rate < 30% → 继续做，但把 embedding 兜底列入 M1.5

用法：
  venv/Scripts/python.exe scripts/entity_gate0.py [--eval-set eval/manual_set.json] [--top-k 30]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import deque
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _PROJECT_ROOT)

# ── 停用表：高频泛词，做了锚点只会污染（对齐规划 §2.3）──────────────────────
STOPLIST = frozenset(
    [
        "小帕",
        "记忆",
        "系统",
        "方案",
        "问题",
        "配置",
        "升级",
        "结果",
        "那个",
        "这个",
        "什么",
        "怎么",
        "为什么",
        "如何",
        "今天",
        "昨天",
        "现在",
        "之前",
        "以后",
        "然后",
        "所以",
        "但是",
        "因为",
        "我们",
        "你们",
        "他们",
        "自己",
        "一下",
        "一下",
        "memory",
        "system",
        "config",
        "plan",
        "issue",
        "result",
        "update",
        "version",
        "docs",
        "test",
        "eval",
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "have",
        "will",
    ]
)

# 事实层 / 索引层的 type（可当锚点；日志层排除）
ANCHOR_TYPES = {"memory", "correction", "decision", "plan", "task", "kb_chunk", "skill_chunk"}
LOG_TYPES = {"record", "event", "git_commit"}


# ── 文本规范化（NFKC + 全角→半角 + casefold）──────────────────────────────
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return s.casefold().strip()


def extract_terms(text: str, min_len_cjk: int = 3, min_len_latin: int = 3) -> set[str]:
    """从一段文本里抽候选词面（中文 3-12 字子串 + 拉丁**完整词**）。

    2026-10-11 修正：原实现对拉丁词做 1-4 gram 切分，导致「memoryhub」被切成
    「mory」「hub」这类**伪词面**——锚点匹配到碎片、指不到真节点（实测 h001
    「为什么从 MemoryHub 改名」的锚点变成 ['mory','hub']，完全失准）。改为
    **只收完整拉丁词**；中文下限由 2 提到 3（2 字子串噪声极大，如「主人」「纠正」
    这类也会挂满节点）。
    """
    t = norm(text)
    terms: set[str] = set()
    # 中文子串（≥3 字）
    cjk = "".join(ch for ch in t if "\u4e00" <= ch <= "\u9fff")
    for n in range(min_len_cjk, min(12, len(cjk)) + 1):
        for i in range(len(cjk) - n + 1):
            terms.add(cjk[i : i + n])
    # 拉丁完整词（不再切 n-gram）
    for w in re.findall(r"[a-z0-9_]+", t):
        if len(w) >= min_len_latin:
            terms.add(w)
    return terms - STOPLIST


class EntityIndexProto:
    """最小实体索引原型：surface -> set[node_id]。零依赖、离线构建。"""

    def __init__(self, store, max_df: int = 8):
        self.store = store
        self.max_df = max_df
        self.surface2nodes: dict[str, set[int]] = {}
        self.node_title: dict[int, str] = {}

    def _title_of(self, nid: int, payload: dict) -> str:
        for k in ("canonical_name", "name", "title", "label", "task_key"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        # 退化：content 前 20 字
        c = payload.get("content") or ""
        return c[:20].strip()

    def build(self) -> None:
        ids = self.store._get_all_node_ids()
        for nid in ids:
            wrap = self.store.get_node(nid)
            if not wrap:
                continue
            p = wrap.get("payload", wrap) if isinstance(wrap, dict) else {}
            t = p.get("type", "")
            if t in LOG_TYPES:
                continue
            title = self._title_of(nid, p)
            self.node_title[nid] = title
            # 来源 1：标题/名字全串；来源 2：拆出的词面（标题内的实体词）
            for surf in {norm(title)} | extract_terms(title):
                if not surf or surf in STOPLIST:
                    continue
                self.surface2nodes.setdefault(surf, set()).add(nid)
        # df 过滤：一个词面挂太多节点 = 泛词，整条丢弃
        before = len(self.surface2nodes)
        self.surface2nodes = {s: v for s, v in self.surface2nodes.items() if len(v) <= self.max_df}
        self._df_removed = before - len(self.surface2nodes)

    def match(self, query: str, max_anchors: int = 3) -> list[tuple[str, list[int]]]:
        """返回 [(surface, [node_id, ...]), ...]，最长匹配优先、去重叠。"""
        t = norm(query)[:64]
        hits: list[tuple[int, int, str, list[int]]] = []  # (start, len, surface, nodes)
        for surf, nodes in self.surface2nodes.items():
            pos = t.find(surf)
            if pos >= 0:
                hits.append((pos, len(surf), surf, sorted(nodes)))
        # 长匹配优先，同长取 node 少的（更精确）
        hits.sort(key=lambda h: (-h[1], len(h[3])))
        chosen: list[tuple[str, list[int]]] = []
        used: list[tuple[int, int]] = []
        for start, ln, surf, nodes in hits:
            end = start + ln
            if any(not (end <= s or start >= e) for s, e in used):
                continue  # 与已选重叠
            used.append((start, end))
            chosen.append((surf, nodes))
            if len(chosen) >= max_anchors:
                break
        return chosen


def bfs_reachable(store, seeds: list[int], depth: int, node_filter=None) -> dict[int, int]:
    """从多个种子做 1-depth 跳 BFS，返回 {node_id: min_hop}。"""
    dist: dict[int, int] = dict.fromkeys(seeds, 0)
    frontier = deque((s, 0) for s in seeds)
    while frontier:
        nid, d = frontier.popleft()
        if d >= depth:
            continue
        try:
            edges = list(store.get_edges(nid)) + list(store.get_incoming_edges(nid))
        except Exception:  # noqa: BLE001 —— 只读审计：个别节点取边失败按「无边」处理，避免中断全图遍历
            edges = []
        for e in edges:
            # Edge / IncomingEdge 对象：出边取 target_id，入边取 source_id
            tgt = getattr(e, "target_id", None)
            if tgt is None and isinstance(e, dict):
                tgt = e.get("target_id")
            src = getattr(e, "source_id", None)
            if src is None and isinstance(e, dict):
                src = e.get("source_id")
            # 若 target 就是当前节点（入边），另一端是 source
            if tgt == nid and src is not None:
                tgt = src
            if tgt is None or tgt == nid:
                continue
            if tgt not in dist or dist[tgt] > d + 1:
                dist[tgt] = d + 1
                frontier.append((tgt, d + 1))
    return dist


def sem_fts_topk(store, query: str, top_k: int) -> set[int]:
    """现有检索（语义 + FTS）的 top-k node 集合，用于算 novel_reach。

    注意：只读库上 search_similar 会尝试回写 hit_count 而报错——这里直接用
    底层向量检索拿 id，避免触发写路径。
    """
    got: set[int] = set()
    db = None
    try:
        db = store._acquire()
        emb = store.embed_text(query)
        hits = db.search_advanced(
            emb,
            top_k=top_k,
            recall_k=top_k,
            min_score=0.0,
            expand_depth=1,
            teleport_alpha=0.0,
            enable_advanced_pipeline=True,
        )
        for h in hits or []:
            got.add(h.id)
    except Exception:  # noqa: S110, BLE001 —— 审计脚本：单通道检索失败不中断整体统计
        pass
    finally:
        if db is not None:
            import contextlib

            with contextlib.suppress(Exception):
                db.close()
    # FTS
    try:
        from core.fts_index import search_fts

        for r in search_fts(query, limit=top_k):
            nid = r.get("node_id") if isinstance(r, dict) else None
            if nid is not None:
                got.add(nid)
    except Exception:  # noqa: S110, BLE001 —— 审计脚本：单通道检索失败不中断整体统计
        pass
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-set", default="eval/manual_set.json")
    ap.add_argument("--top-k", type=int, default=30)
    ap.add_argument("--max-df", type=int, default=8)
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    os.environ.setdefault("DB_PATH", "data/mh_memory.db")
    from core.trivium_store import TriviumStore

    eval_path = Path(args.eval_set)
    if not eval_path.is_absolute():
        eval_path = Path(_PROJECT_ROOT) / eval_path
    data = json.loads(eval_path.read_text(encoding="utf-8"))
    items = data.get("items", data) if isinstance(data, dict) else data

    print(f"评测集: {eval_path}  ({len(items)} 条)")
    t0 = time.time()
    store = TriviumStore(read_only=True)
    idx = EntityIndexProto(store, max_df=args.max_df)
    idx.build()
    print(
        f"实体索引: {len(idx.surface2nodes)} 个词面（df 过滤掉 {idx._df_removed} 个泛词），耗时 {time.time() - t0:.1f}s"
    )

    n_total = n_trigger = n_reach = n_novel = 0
    rows = []
    for it in items:
        q = it.get("query", "")
        gold = set(it.get("gold_ids") or [])
        n_total += 1
        anchors = idx.match(q)
        if anchors:
            n_trigger += 1
        seed_ids: list[int] = []
        for _surf, nodes in anchors:
            seed_ids.extend(nodes)
        reach: set[int] = set()
        if seed_ids:
            reach = set(bfs_reachable(store, sorted(set(seed_ids)), args.depth))
        sem_fts = sem_fts_topk(store, q, args.top_k) if gold else set()
        in_reach = gold & reach
        novel = in_reach - sem_fts
        if in_reach:
            n_reach += 1
        if novel:
            n_novel += 1
        rows.append(
            {
                "qid": it.get("qid"),
                "kind": it.get("kind"),
                "query": q,
                "anchors": [{"surface": s, "n_nodes": len(ns), "nodes": ns} for s, ns in anchors],
                "gold": sorted(gold),
                "gold_in_reach": sorted(in_reach),
                "gold_novel": sorted(novel),
                "reach_size": len(reach),
                "sem_fts_topk_size": len(sem_fts),
            }
        )

    trig = n_trigger / n_total if n_total else 0
    reach = n_reach / n_total if n_total else 0
    novel = n_novel / n_total if n_total else 0

    print("\n" + "=" * 56)
    print(f"  样本数            : {n_total}")
    print(f"  trigger_rate      : {trig:.1%}   （识别出 ≥1 锚点的查询占比）")
    print(f"  reach@{args.depth}          : {reach:.1%}   （金标落在锚点 {args.depth} 跳内）")
    print(f"  novel_reach@{args.depth}    : {novel:.1%}   （真实增量：金标在跳内且不在 sem∪fts top{args.top_k}）")
    print("=" * 56)

    print("\n判定：")
    if novel < 0.05 or reach < 0.15:
        print("  ⛔ 停止 M1 —— 图太稀疏，实体锚定吃不到增量。应先做「写边」（M2）。")
    elif trig < 0.30:
        print("  ⚠️  继续，但把 embedding 兜底列入 M1.5（锚点识别覆盖不足）。")
    else:
        print("  ✅ 继续 —— 图谱有可召回增量，值得做 M1。")

    # 明细
    print("\n明细（gold_novel 非空 = 这条是实体路的真实增量）：")
    for r in rows:
        if r["gold_novel"] or (r["anchors"] and not r["gold_in_reach"]):
            print(f"  [{r['qid']}] {r['query'][:38]}")
            print(f"       锚点: {[a['surface'] for a in r['anchors']] or '无'}")
            print(f"       gold={r['gold']} in_reach={r['gold_in_reach']} novel={r['gold_novel']}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "eval_set": str(eval_path),
                    "n": n_total,
                    "trigger_rate": trig,
                    "reach": reach,
                    "novel_reach": novel,
                    "depth": args.depth,
                    "top_k": args.top_k,
                    "max_df": args.max_df,
                    "rows": rows,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n明细已写: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
