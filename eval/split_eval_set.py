"""Temporal held-out split for the retrieval evaluation query set.

Splits eval-set items into a tuning set (queries pointing at older nodes) and
a held-out reporting set (newer ones), so tuning decisions are never evaluated
on the data they were tuned on. The split point is recorded in a manifest that
``eval/significance.py`` consumes to tag each number with its subset.

Split field: the eval-set items carry no timestamp of their own, so the
``created_at`` (epoch seconds) of each item's gold node(s) is used as a proxy,
read from DB payloads on a DB copy (same isolation scheme as ``run_eval.py``).
Negative samples and items whose gold node is missing or has no timestamp are
excluded from both sides and counted in the manifest — see the limitations
section in ``docs/RETRIEVAL_QUALITY.md``.

Usage:
    python eval/split_eval_set.py [--eval-set eval/eval_set.json] [--tune-fraction 0.8]

Outputs (under eval/, gitignored like the other eval/*.json):
    eval_set_tune.json / eval_set_heldout.json / split_manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


from eval.metrics import normalize_gold_ids  # noqa: E402


def temporal_split(
    items: list[dict],
    created_by_gold: dict[int, float],
    tune_fraction: float = 0.8,
    source_eval_set: str = "",
) -> tuple[list[dict], list[dict], dict]:
    """Split items by gold-node creation time. Pure function, deterministic.

    Order: ascending (created_at, qid); the oldest ``floor(n * tune_fraction)``
    items form the tuning set, the rest the held-out set. Items without a
    usable timestamp (negative samples, no gold ids, gold node missing or
    timestamp-less) are excluded from both sides and counted in the manifest.
    For multi-gold items the earliest gold timestamp is used (conservative:
    the item is more likely to end up in the tuning set).
    ``gold_ids`` is accepted as a scalar legacy id or a list (#174 multi-gold).
    """
    placed: list[tuple[float, str, dict]] = []
    n_negative_or_no_gold = 0
    n_missing_timestamp = 0
    for it in items:
        gold_ids = normalize_gold_ids(it.get("gold_ids"))
        if it.get("kind") == "negative" or not gold_ids:
            n_negative_or_no_gold += 1
            continue
        ts_known = [created_by_gold[g] for g in gold_ids if g in created_by_gold]
        if not ts_known:
            n_missing_timestamp += 1
            continue
        placed.append((min(ts_known), str(it.get("qid", "")), it))

    placed.sort(key=lambda t: (t[0], t[1]))
    n_tune = int(len(placed) * tune_fraction) if placed else 0
    tune = [it for _, _, it in placed[:n_tune]]
    held_out = [it for _, _, it in placed[n_tune:]]
    boundary = placed[n_tune - 1][0] if n_tune > 0 else None

    # unplaced 也要钉 qid：pin 复跑时区分「本来就不在两侧」和「真正的新增」
    neg_qids = [str(it.get("qid", "")) for it in items if it.get("kind") == "negative" or not it.get("gold_ids")]
    no_ts_qids = []
    for it in items:
        gold_ids2 = normalize_gold_ids(it.get("gold_ids"))
        if it.get("kind") != "negative" and gold_ids2 and not [g for g in gold_ids2 if g in created_by_gold]:
            no_ts_qids.append(str(it.get("qid", "")))

    manifest: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_eval_set": source_eval_set,
        "split_field": "created_at (epoch seconds) of the item's gold node(s), read from DB payloads",
        "tune_fraction": tune_fraction,
        "n_placed": len(placed),
        "n_tune": len(tune),
        "n_held_out": len(held_out),
        "unplaced": {
            "negative_or_no_gold": n_negative_or_no_gold,
            "missing_timestamp": n_missing_timestamp,
        },
        "unplaced_qids": {
            "negative_or_no_gold": neg_qids,
            "missing_timestamp": no_ts_qids,
        },
        "boundary_created_at": boundary,
        "boundary_created_at_iso": (
            datetime.fromtimestamp(boundary, tz=timezone.utc).isoformat() if boundary is not None else None
        ),
        "tune_qids": [str(it.get("qid", "")) for it in tune],
        "held_out_qids": [str(it.get("qid", "")) for it in held_out],
        "notes": [
            "Older gold nodes -> tuning set; newer -> held-out set. Tuning runs must not touch the held-out set.",
            "Negative samples and items without a timestamped gold node are excluded from both sides.",
            "Protocol and limitations are documented in docs/RETRIEVAL_QUALITY.md.",
        ],
    }
    return tune, held_out, manifest


def _write_subset(path: Path, source: dict, items: list[dict], role: str) -> None:
    out = {
        "version": source.get("version", 1),
        "created_at": source.get("created_at"),
        "seed": source.get("seed"),
        "split": {"role": role},
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


def _copy_db_to_tmp(project_root: Path) -> Path:
    """Copy DB + sidecars + fts.db into eval/.tmp/ and point DB_PATH there.

    Same isolation scheme as run_eval.py, but invoked from main() only so that
    importing this module (in tests) never touches the DB.
    """
    db_path_env = os.getenv("DB_PATH", "")
    if db_path_env:
        db = Path(db_path_env) if Path(db_path_env).is_absolute() else project_root / db_path_env
    else:
        db = project_root / "data" / "mh_memory.db"
    fts = db.parent / "fts.db"
    if not db.exists():
        print(
            f"[split] ERROR: DB not found at {db} — set DB_PATH to the DB you want to evaluate against "
            f"(e.g. the production copy under <repo>/data/)"
        )
        sys.exit(1)
    tmp = (Path(__file__).parent / ".tmp").resolve()
    tmp.mkdir(parents=True, exist_ok=True)
    for p in db.parent.iterdir():
        if p.name.startswith(db.name) and p.name != db.name + ".lock" and p.is_file():
            shutil.copy2(p, tmp / p.name)
    if fts.exists():
        shutil.copy2(fts, tmp / fts.name)
    tmp_db = tmp / db.name
    os.environ["DB_PATH"] = str(tmp_db)
    return tmp_db


def load_gold_timestamps(db_path: Path) -> dict[int, float]:
    """node_id -> created_at (epoch seconds) for all timestamped nodes in the DB."""
    os.environ["DB_PATH"] = str(db_path)
    from core.trivium_store import TriviumStore  # needs DB_PATH set first

    store = TriviumStore()
    out: dict[int, float] = {}
    for nid, payload in store.iter_payloads():
        if payload and payload.get("created_at") is not None:
            out[nid] = float(payload["created_at"])
    return out


def pinned_resplit(items: list[dict], old_manifest: dict) -> tuple[list[dict], list[dict], dict]:
    """Re-split BY QID frozen by ``old_manifest``. Pure (no DB, no file IO).

    为什么钉 qid：按 (created_at, qid) 排序切分时，任何新增题目都可能改变
    已有题目相对位置 -> 已有 qid 在 tune/held-out 之间悄悄搬家，held-out
    不再「未被调参触碰」。钉住 qid 后新增题只进 unplaced，等待显式重切。

    写出的 manifest 带 ``unplaced_qids`` 逐条名单（negative_or_no_gold /
    missing_timestamp / new_qids_not_in_manifest），此名单随 qid 一起被下一轮
    pin 读取，所以连续 pin 不会把旧 unplaced 反复当成「新增」；读侧兼容旧
    格式 manifest（只有 unplaced 计数、无名单）——该情况下未列入名单的未放置
    正样本会被报为新增一轮，第二轮起自然收敛。
    """
    pinned_tune = {str(q) for q in old_manifest.get("tune_qids", [])}
    pinned_held = {str(q) for q in old_manifest.get("held_out_qids", [])}
    overlap = pinned_tune & pinned_held
    if overlap:
        raise ValueError(f"manifest has qids in both sides: {sorted(overlap)}")
    tune = [it for it in items if str(it.get("qid")) in pinned_tune]
    held_out = [it for it in items if str(it.get("qid")) in pinned_held]
    placed_qids = pinned_tune | pinned_held

    old_unplaced_qids = old_manifest.get("unplaced_qids", {})
    old_unplaced = {str(q) for q in old_unplaced_qids.get("missing_timestamp", [])}
    old_unplaced |= {str(q) for q in old_unplaced_qids.get("negative_or_no_gold", [])}
    old_unplaced |= {str(q) for q in old_unplaced_qids.get("new_qids_not_in_manifest", [])}

    set_qids = {str(it.get("qid", "")) for it in items}
    # negative / no-gold 分类不需要 DB，可直接在当前题集上重算
    current_neg_or_no_gold = [
        str(it.get("qid", ""))
        for it in items
        if it.get("kind") == "negative" or not normalize_gold_ids(it.get("gold_ids"))
    ]
    # 真正的新增 = 带 gold 的正样本 qid 不在两侧、也不在旧 unplaced 名单里
    # （gold 已置空的 quarantine/dropped 归 negative_or_no_gold 名单，不算新增）
    fresh_new = [
        str(it.get("qid"))
        for it in items
        if it.get("kind") != "negative"
        and normalize_gold_ids(it.get("gold_ids"))
        and str(it.get("qid")) not in placed_qids
        and str(it.get("qid")) not in old_unplaced
    ]
    # 旧名单的 new_qids 原样携带：「未放置池」只增不减，等主人显式重切决定，
    # 不允许在连续 pin 时悄悄缩水或复活
    carried_new = sorted(
        str(q)
        for q in old_unplaced_qids.get("new_qids_not_in_manifest", [])
        if str(q) in set_qids and str(q) not in placed_qids
    )
    current_new = sorted(set(carried_new) | set(fresh_new))
    # 旧名单里的 missing_timestamp 原样携带（判「无时间戳」需要 DB，不在纯函数里做）
    carried_missing_ts = sorted(
        str(q)
        for q in old_unplaced_qids.get("missing_timestamp", [])
        if str(q) in set_qids and str(q) not in placed_qids
    )

    manifest: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_eval_set": "",
        "pinned": True,
        "split_field": old_manifest.get("split_field", ""),
        "tune_fraction": old_manifest.get("tune_fraction"),
        "n_placed": len(tune) + len(held_out),
        "n_tune": len(tune),
        "n_held_out": len(held_out),
        "unplaced": {
            "negative_or_no_gold": len(current_neg_or_no_gold),
            "missing_timestamp": len(carried_missing_ts),
            "new_qids_not_in_manifest": current_new,
        },
        "unplaced_qids": {
            "negative_or_no_gold": current_neg_or_no_gold,
            "missing_timestamp": carried_missing_ts,
            "new_qids_not_in_manifest": current_new,
        },
        "boundary_created_at": old_manifest.get("boundary_created_at"),
        "boundary_created_at_iso": old_manifest.get("boundary_created_at_iso"),
        "tune_qids": [str(it.get("qid", "")) for it in tune],
        "held_out_qids": [str(it.get("qid", "")) for it in held_out],
        "notes": [
            "Pinned re-split: qid assignments are frozen from the pinned manifest; new qids are NOT",
            "auto-assigned (unplaced.new_qids_not_in_manifest) so existing splits never move silently.",
            "unplaced_qids carries the per-qid unplaced lists so repeated pinning converges.",
            *(old_manifest.get("notes", []) or []),
        ],
    }
    return tune, held_out, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Temporal held-out split of the eval query set")
    parser.add_argument("--eval-set", default=None, help="eval set JSON (default: eval/eval_set.json)")
    parser.add_argument(
        "--tune-fraction",
        type=float,
        default=0.8,
        help="fraction of oldest items assigned to the tuning set (default: 0.8)",
    )
    parser.add_argument("--out-dir", default=None, help="output directory (default: eval/)")
    parser.add_argument(
        "--skip-db", action="store_true", help="skip reading the DB (timestamps must be provided via --timestamps-json)"
    )
    parser.add_argument(
        "--timestamps-json", default=None, help='JSON file {"<node_id>": created_at} to use instead of reading the DB'
    )
    parser.add_argument(
        "--pin-manifest",
        default=None,
        help=(
            "reuse an existing split_manifest.json: items are assigned BY QID exactly as pinned there, "
            "so adding items later never silently reshuffles the existing split. QIDs not present in "
            "the manifest are reported as unplaced_new and require an explicit re-split decision."
        ),
    )
    args = parser.parse_args()

    eval_dir = Path(__file__).parent
    out_dir = Path(args.out_dir) if args.out_dir else eval_dir
    eval_set_path = Path(args.eval_set) if args.eval_set else eval_dir / "eval_set.json"
    if not eval_set_path.exists():
        print(f"[split] ERROR: eval set not found: {eval_set_path}")
        return 1
    with open(eval_set_path, encoding="utf-8") as f:
        eval_set = json.load(f)

    if args.pin_manifest:
        # ── Pinned re-split: qid assignment is frozen by the old manifest ──
        with open(args.pin_manifest, encoding="utf-8") as f:
            old_manifest = json.load(f)
        try:
            tune, held_out, manifest = pinned_resplit(eval_set["items"], old_manifest)
        except ValueError as e:
            print(f"[split] ERROR: {e}")
            return 1
        manifest["source_eval_set"] = str(eval_set_path)
        manifest["pinned_from"] = str(args.pin_manifest)
        new_qids = manifest["unplaced"]["new_qids_not_in_manifest"]
        print(
            f"[split] pinned from {args.pin_manifest}: tune={len(tune)}, "
            f"held-out={len(held_out)}, new unassigned qids={len(new_qids)}"
        )
    else:
        if args.timestamps_json:
            with open(args.timestamps_json, encoding="utf-8") as f:
                created_by_gold = {int(k): float(v) for k, v in json.load(f).items()}
        elif args.skip_db:
            created_by_gold = {}
        else:
            print("[split] Copying DB to a safe location (the original production DB is never touched)")
            tmp_db = _copy_db_to_tmp(Path(_PROJECT_ROOT))
            print(f"[split] DB copy at: {tmp_db}")
            created_by_gold = load_gold_timestamps(tmp_db)
        print(f"[split] timestamped nodes available: {len(created_by_gold)}")

        tune, held_out, manifest = temporal_split(
            eval_set["items"],
            created_by_gold,
            tune_fraction=args.tune_fraction,
            source_eval_set=str(eval_set_path),
        )

    manifest_path = out_dir / "split_manifest.json"
    _write_subset(out_dir / "eval_set_tune.json", eval_set, tune, "tune")
    _write_subset(out_dir / "eval_set_heldout.json", eval_set, held_out, "held_out")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("[split] boundary created_at:", manifest.get("boundary_created_at_iso"))
    print(f"[split] tune set: {manifest['n_tune']} items -> eval_set_tune.json")
    print(f"[split] held-out set: {manifest['n_held_out']} items -> eval_set_heldout.json")
    print(f"[split] unplaced: {manifest.get('unplaced')}")
    print(f"[split] manifest written to: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
