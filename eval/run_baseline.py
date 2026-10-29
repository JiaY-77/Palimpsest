"""重跑评测 + 生成基线报告——一条命令（#221 §2.8）。

#232（分块池化全量重嵌）落地后跑这条命令，就能从题集直通基线报告：

    python eval/run_baseline.py

它串行三步（都沿用既有脚本，不另开实现）：

  1. run_eval.py —— 库副本上跑全 4 模式，SHA256 跑前跑后自证，
     报告里每个数字标注子集与条数（#221 §2.5）；
  2. split_eval_set.py --pin-manifest —— 按 qid 钉住重切 split：
     已有 tune/held-out 归属一动不动，新增题只进
     unplaced.new_qids_not_in_manifest，等主人显式决定（#221 §2.4）；
  3. significance.py —— 对 vec vs rrf / vec vs cascade / fts vs vec
     做 paired bootstrap，输出逐集（all/tune/held_out）对比。

可复跑：同库同 manifest 连跑两次，numbers 逐位相同（确定性证明：
tests/test_eval_report_determinism.py；docs/RETRIEVAL_QUALITY.md §8.4）。

注意：
  - 基线数字意义依赖库状态——重嵌（#232）完成前的跑数都只是
    「重嵌前对照」，不要钉成基线（主人 2026-10-29 决定 #221 不出
    最终基线，等重嵌完再出）。
  - --skip-eval 复用 eval/ 下最新一份 results_*.json（跳过第 1 步）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent

COMPARISONS: tuple[tuple[str, str], ...] = (
    ("vec", "rrf"),
    ("vec", "cascade"),
    ("fts", "vec"),
)


def build_cmds(
    eval_set: str,
    manifest: str,
    modes: list[str],
    metric: str,
    skip_eval: bool,
) -> tuple[list[list[str]], list[str]]:
    """Compose the three step command lists + human-readable step labels (pure)."""
    scripts = [Path(__file__).parent / "run_eval.py", Path(__file__).parent / "split_eval_set.py"]

    # 第 0 步合法性检查：--skip-eval 时必须已有 results_*.json 可复用
    if skip_eval and not sorted(_EVAL_DIR.glob("results_*.json"), key=lambda p: p.stat().st_mtime):
        raise SystemExit("[baseline] --skip-eval set but no results_*.json found in eval/")

    cmds: list[list[str]] = []
    labels: list[str] = []
    if not skip_eval:
        cmds.append([sys.executable, str(scripts[0]), "--modes", ",".join(modes), "--eval-set", eval_set])
        labels.append("1/3 run_eval")
    cmds.append([sys.executable, str(scripts[1]), "--eval-set", eval_set, "--pin-manifest", manifest])
    labels.append("2/3 split (pinned by qid)")

    for mode_a, mode_b in COMPARISONS:
        if mode_a not in modes or mode_b not in modes:
            continue
        args = [
            "--results-a",
            "<results>",
            "--mode-a",
            mode_a,
            "--mode-b",
            mode_b,
            "--metric",
            metric,
            "--eval-set",
            eval_set,
            "--split-manifest",
            manifest,
        ]
        cmds.append([sys.executable, str(Path(__file__).parent / "significance.py"), *args])
        labels.append(f"3/3 significance {mode_a} vs {mode_b}")
    return cmds, labels


def _latest_results(eval_dir: Path) -> Path:
    existing = sorted(eval_dir.glob("results_*.json"), key=lambda p: p.stat().st_mtime)
    if not existing:
        raise SystemExit("[baseline] no results_*.json found in eval/ — run step 1 first")
    return existing[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full evaluation pipeline in one command (#221 2.8)")
    parser.add_argument("--eval-set", default=None, help="eval set JSON (default: eval/eval_set_v2.json)")
    parser.add_argument("--manifest", default=None, help="split manifest to pin (default: eval/split_manifest.json)")
    parser.add_argument("--modes", type=str, default="fts,vec,rrf,cascade", help="modes for step 1 (default: all)")
    parser.add_argument(
        "--metric", type=str, default="recall@5", help="metric for significance step (default: recall@5)"
    )
    parser.add_argument(
        "--skip-eval", action="store_true", help="reuse the newest results_*.json instead of re-running evaluation"
    )
    args = parser.parse_args()

    eval_set = args.eval_set or str(_EVAL_DIR / "eval_set_v2.json")
    manifest = args.manifest or str(_EVAL_DIR / "split_manifest.json")
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    cmds, labels = build_cmds(eval_set, manifest, modes, args.metric, skip_eval=args.skip_eval)
    results_path: str | None = str(_latest_results(_EVAL_DIR)) if args.skip_eval else None
    for label, cmd in zip(labels, cmds, strict=True):
        cmd_str = [str(c) for c in cmd]
        if "<results>" in cmd_str:
            if results_path is None:
                results_path = str(_latest_results(_EVAL_DIR))  # 第 1 步刚产出
            cmd_str[cmd_str.index("<results>")] = results_path
        print(f"[baseline] step {label}: {' '.join(cmd_str)}")
        proc = subprocess.run(cmd_str, check=False)
        if proc.returncode != 0:
            print(f"[baseline] step {label} FAILED (exit {proc.returncode}) — stopping")
            return proc.returncode
    print("[baseline] all steps done. Artifacts in eval/: results_*.json, report_*.md,")
    print("[baseline] eval_set_tune.json / eval_set_heldout.json / split_manifest.json, comparison_*.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
