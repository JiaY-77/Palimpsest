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
    """
    placed: list[tuple[float, str, dict]] = []
    n_negative_or_no_gold = 0
    n_missing_timestamp = 0
    for it in items:
        if it.get("kind") == "negative" or not it.get("gold_ids"):
            n_negative_or_no_gold += 1
            continue
        ts_known = [created_by_gold[g] for g in it["gold_ids"] if g in created_by_gold]
        if not ts_known:
            n_missing_timestamp += 1
            continue
        placed.append((min(ts_known), str(it.get("qid", "")), it))

    placed.sort(key=lambda t: (t[0], t[1]))
    n_tune = int(len(placed) * tune_fraction) if placed else 0
    tune = [it for _, _, it in placed[:n_tune]]
    held_out = [it for _, _, it in placed[n_tune:]]
    boundary = placed[n_tune - 1][0] if n_tune > 0 else None

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
    args = parser.parse_args()

    eval_dir = Path(__file__).parent
    out_dir = Path(args.out_dir) if args.out_dir else eval_dir
    eval_set_path = Path(args.eval_set) if args.eval_set else eval_dir / "eval_set.json"
    if not eval_set_path.exists():
        print(f"[split] ERROR: eval set not found: {eval_set_path}")
        return 1
    with open(eval_set_path, encoding="utf-8") as f:
        eval_set = json.load(f)

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

    print("[split] boundary created_at:", manifest["boundary_created_at_iso"])
    print(f"[split] tune set: {manifest['n_tune']} items -> eval_set_tune.json")
    print(f"[split] held-out set: {manifest['n_held_out']} items -> eval_set_heldout.json")
    print(f"[split] unplaced (negative / no gold / missing timestamp): {manifest['unplaced']}")
    print(f"[split] manifest written to: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
