"""生成融合评测题集（可复现，不依赖一次性快照）。

为什么需要生成脚本而不是手写 JSON
--------------------------------
精确标识符类的 gold 必须是「库里所有提到该标识符、且当前可见」的节点。手写快照
会过期（库在长），而且容易混进已被取代的旧版本——旧版本在生产检索里被
`status == "outdated"` 过滤掉，永远召不回，算进 gold 只会系统性压低所有方案的指标。

所以 gold 的口径固定为：**FTS 字面枚举 ∩ 当前可见（status != outdated）**。

- 字面枚举 → 与任何排序无关（不用语义检索反查 gold，避免循环论证）
- 可见口径 → 与生产检索的过滤链一致

三类题
------
exact     纯标识符 query（commit hash / PR 号），gold 全自动生成
semantic  自然语言 query，沿用 eval_set.json 的 gold（生成时从节点反推），再按可见口径过滤
mixed     自然语言 + 标识符混排，gold = 标识符枚举 ∩ 可见，语义部分人工指定

用法
----
    venv/Scripts/python.exe eval/gen_fusion_set.py --out eval/fusion_set_v1.json
    venv/Scripts/python.exe eval/gen_fusion_set.py --exact-only    # 只出精确标识符类
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_EVAL_DIR = Path(__file__).resolve().parent
_TMP_DIR = _EVAL_DIR / ".tmp_fusion"
sys.path.insert(0, _PROJECT_ROOT)


def _locate_originals() -> tuple[Path, Path]:
    db_env = os.getenv("DB_PATH", "")
    if db_env and os.path.isabs(db_env):
        db = Path(db_env)
    elif db_env:
        db = Path(_PROJECT_ROOT) / db_env
    else:
        db = Path(_PROJECT_ROOT) / "data" / "mh_memory.db"
    return db, db.parent / "fts.db"


def _copy_if_missing() -> Path:
    """复用 fusion_ab 的副本目录；不存在才复制（避免覆盖正在被读的副本）。"""
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    orig_db, orig_fts = _locate_originals()
    tmp = _TMP_DIR.resolve()
    target = tmp / orig_db.name
    if not target.exists():
        for p in orig_db.parent.iterdir():
            if p.name.startswith(orig_db.name) and p.is_file():
                shutil.copy2(p, tmp / p.name)
        if orig_fts.exists():
            shutil.copy2(orig_fts, tmp / orig_fts.name)
    os.environ["DB_PATH"] = str(target)
    return target


_DB_COPY = _copy_if_missing()
from core.trivium_store import TriviumStore  # noqa: E402

# ── 题集人工部分：从外部 JSON 读（不入库，含私有记忆内容与具体 node id）──────
# 为什么不写在代码里：这批 query 是为「本机库里的具体节点」写的，既含私有记忆内容，
# 又绑死具体 node id。写进仓库等于把私有数据和只对某台机器成立的样本一起发布。
# 所以代码只留读取逻辑，数据放 eval/fusion_set_manual.json（已被 .gitignore 忽略）。
# 换库后按 eval/fusion_set_manual.example.json 的结构重写一份即可。
_MANUAL_PATH = _EVAL_DIR / "fusion_set_manual.json"


def _load_manual() -> dict:
    if not _MANUAL_PATH.exists():
        raise SystemExit(
            f"[gen_fusion_set] 缺少题集数据文件: {_MANUAL_PATH}\n"
            f"  请照 eval/fusion_set_manual.example.json 的结构写一份"
            f"（exact_tokens / mixed / date / semantic 四段）。"
        )
    with open(_MANUAL_PATH, encoding="utf-8") as f:
        return json.load(f)


_MANUAL = _load_manual()
# 精确标识符：纯 token，gold 由 FTS 字面枚举自动生成
EXACT_TOKENS: list[tuple[str, str]] = [(t, k) for t, k in _MANUAL["exact_tokens"]]
# 混合类：**同一组标识符** + 自然语言意图。gold 与对应 exact 题完全相同，
# 唯一变量是 query 形式——用来回答「加自然语言会不会把语义通道的噪声带进来」。
MIXED_MANUAL: list[tuple[str, str]] = [(t, q) for t, q in _MANUAL["mixed"]]
# 日期类：query 含 8 位日期，会被 is_exact_identifier_query 判成标识符查询
# （8 位纯数字命中 [0-9a-f]{7,} 且含数字）。误判是否真伤取决于日期是否字面相关：
# 日期在 FTS 里能精确匹配，抬 FTS 可能是好事——所以单列一类，用数据判断。
DATE_MANUAL: list[tuple[int, str]] = [(int(n), q) for n, q in _MANUAL["date"]]
# 自然语言类：query-from-passage（人工读节点内容后写问题，不看检索结果）。
SEMANTIC_MANUAL: list[tuple[int, str]] = [(int(n), q) for n, q in _MANUAL["semantic"]]


def fts_enum(token: str, store: TriviumStore, limit: int = 200) -> list[int]:
    """FTS 字面枚举某个标识符命中的全部节点 id（按 FTS 顺序，不去重口径由调用方决定）。"""
    fts_path = Path(os.environ["DB_PATH"]).parent / "fts.db"
    conn = sqlite3.connect(fts_path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT node_id FROM mem_fts WHERE mem_fts MATCH ? ORDER BY rank LIMIT ?",
            (f'"{token}"', int(limit)),
        ).fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows if r[0] is not None]


def visible(store: TriviumStore, nid: int) -> bool:
    p = (store.get_node(nid) or {}).get("payload", {}) or {}
    return p.get("status") != "outdated"


def build_exact(store: TriviumStore) -> list[dict]:
    items = []
    for i, (token, subtype) in enumerate(EXACT_TOKENS, 1):
        all_hits = fts_enum(token, store)
        gold = [n for n in all_hits if visible(store, n)]
        dropped = len(all_hits) - len(gold)
        if not gold:
            print(f"  ⚠ {token}: 可见 gold 为空，跳过")
            continue
        items.append(
            {
                "qid": f"x{i:03d}",
                "query": token,
                "kind": "exact",
                "exact_subtype": subtype,
                "gold_ids": gold,
                "gold_note": (
                    f"FTS 字面枚举 {len(all_hits)} 条，剔除非可见（outdated）{dropped} 条 → gold {len(gold)} 条"
                ),
            }
        )
        print(f"  exact {token}: 命中 {len(all_hits)} → 可见 gold {len(gold)}（剔除 {dropped}）")
    return items


def build_semantic(store: TriviumStore) -> list[dict]:
    """人工 query-from-passage：校验节点存在且可见，再收入题集。"""
    items = []
    for i, (nid, query) in enumerate(SEMANTIC_MANUAL, 1):
        node = store.get_node(nid)
        if not node:
            print(f"  ⚠ s{i:03d}（节点 {nid}）不存在，跳过——题集需要按当前库重抽")
            continue
        p = node.get("payload", {}) or {}
        if p.get("status") == "outdated":
            print(f"  ⚠ s{i:03d}（节点 {nid}）已 outdated，跳过")
            continue
        items.append(
            {
                "qid": f"s{i:03d}",
                "query": query,
                "kind": "semantic",
                "gold_ids": [nid],
                "layer": p.get("type", ""),
                "gold_note": f"query-from-passage：人工读节点 {nid}（{p.get('type')}/{p.get('domain')}）内容后写的问题",
            }
        )
    print(f"  semantic: {len(items)} 题")
    return items


def build_mixed(store: TriviumStore) -> list[dict]:
    """混合类：标识符 + 自然语言意图。gold 与 exact 类同口径（标识符枚举 ∩ 可见）。"""
    items = []
    for i, (token, query) in enumerate(MIXED_MANUAL, 1):
        all_hits = fts_enum(token, store)
        gold = [n for n in all_hits if visible(store, n)]
        if not gold:
            print(f"  ⚠ mixed {token}: 可见 gold 为空，跳过")
            continue
        items.append(
            {
                "qid": f"m{i:03d}",
                "query": query,
                "kind": "mixed",
                "token": token,
                "gold_ids": gold,
                "gold_note": f"标识符 {token} FTS 枚举 ∩ 可见 → {len(gold)} 条（与对应 exact 题同一 gold 集）",
            }
        )
    print(f"  mixed: {len(items)} 题")
    return items


def build_date(store: TriviumStore) -> list[dict]:
    """日期类：gold 由人工读节点确定；校验节点可见后收入。"""
    items = []
    for i, (nid, query) in enumerate(DATE_MANUAL, 1):
        node = store.get_node(nid)
        if not node or (node.get("payload", {}) or {}).get("status") == "outdated":
            print(f"  ⚠ date {nid} 不存在或已 outdated，跳过")
            continue
        items.append(
            {
                "qid": f"d{i:03d}",
                "query": query,
                "kind": "date",
                "gold_ids": [nid],
                "gold_note": f"query 含 8 位日期，会触发 is_exact 判定；gold 为人工确认的节点 {nid}",
            }
        )
    print(f"  date: {len(items)} 题")
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="生成融合评测题集")
    ap.add_argument("--out", default=str(_EVAL_DIR / "fusion_set_v1.json"))
    ap.add_argument("--exact-only", action="store_true")
    args = ap.parse_args()

    store = TriviumStore()
    print("[gen_fusion_set] 枚举精确标识符 gold")
    items = build_exact(store)
    if not args.exact_only:
        print("[gen_fusion_set] 构造混合类题")
        items += build_mixed(store)
        print("[gen_fusion_set] 构造日期类题")
        items += build_date(store)
        print("[gen_fusion_set] 收集自然语言题")
        items += build_semantic(store)

    out = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "融合权重 A/B 评测题集。gold 口径：精确标识符类由 FTS 字面枚举 ∩ 当前可见；"
            "自然语言类沿用 eval_set.json 反推的 gold 并做可见过滤。"
        ),
        "items": items,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[gen_fusion_set] {len(items)} 题 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
