"""eval/check_eval_set.py 的题集自洽校验回归测试（issue #240）。

背景：eval_set_v2.json 曾静默漂移成 138 题 / 判卷 27（README 与
docs/RETRIEVAL_QUALITY.md §8.3/§8.4 记的是 148 / 31），而题集是
git-ignored 本地数据，CI 看不到，此前没有任何东西能发现漂移。
本文件只建临时 JSON 断不变量，不读 eval/ 下任何真实题集。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _mk_set() -> dict:
    """干净小型题集：live=2 / reresolved=1 / quarantined=1 / dropped=1，判卷 3。"""
    return {
        "version": 2,
        "seed": 7,
        "status_counts": {"live": 2, "reresolved": 1, "quarantined": 1, "dropped": 1},
        "items": [
            {
                "qid": "q1",
                "query": "a?",
                "kind": "semantic",
                "gold_ids": [11],
                "gold_type": "node",
                "gold_domain": "hermes",
                "layer": "hermes",
                "status": "live",
            },
            {
                "qid": "q2",
                "query": "b?",
                "kind": "semantic",
                "gold_ids": [12],
                "gold_type": "node",
                "gold_domain": "hermes",
                "layer": "hermes",
                "status": "live",
            },
            {
                "qid": "q3",
                "query": "c?",
                "kind": "entity",
                "gold_ids": [13],
                "gold_type": "node",
                "gold_domain": "kb",
                "layer": "kb",
                "status": "reresolved",
            },
            {
                "qid": "q4",
                "query": "d?",
                "kind": "semantic",
                "gold_ids": [],
                "gold_type": "node",
                "gold_domain": "kb",
                "layer": "kb",
                "status": "quarantined",
            },
            {
                "qid": "q5",
                "query": "e?",
                "kind": "semantic",
                "gold_ids": [],
                "gold_type": "node",
                "gold_domain": "kb",
                "layer": "kb",
                "status": "dropped",
            },
        ],
    }


def _write(tmp_path: Path, data: object, name: str = "es.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _run_cli(py_args: list[str], script: Path | None = None) -> subprocess.CompletedProcess[str]:
    """subprocess 跑 CLI（仓库既有 CLI 测试习惯）。"""
    exe = Path(__file__).resolve().parent.parent / "eval" / "check_eval_set.py"
    target = script if script is not None else exe
    return subprocess.run([sys.executable, str(target), *py_args], capture_output=True, text=True, timeout=120)


class TestCheckClean:
    def test_clean_set_passes(self, tmp_path):
        from eval.check_eval_set import check

        assert check(_write(tmp_path, _mk_set())) == []

    def test_cli_exit_zero(self, tmp_path):
        p = _write(tmp_path, _mk_set())
        r = _run_cli(["--eval-set", str(p)])
        assert r.returncode == 0, r.stdout + r.stderr
        assert "items=5" in r.stdout and "judged=3" in r.stdout and "live=2" in r.stdout


class TestInvariantsBroken:
    """每个不变量各一条破坏用例，用关键词断言（不锁全文文案）。"""

    def test_missing_file(self, tmp_path):
        from eval.check_eval_set import check

        msgs = check(tmp_path / "nope.json")
        assert msgs and "文件不存在" in msgs[0]

    def test_bad_json(self, tmp_path):
        from eval.check_eval_set import check

        p = tmp_path / "es.json"
        p.write_text("{oops", encoding="utf-8")
        msgs = check(p)
        assert "无法解析 JSON" in " | ".join(msgs)

    def test_missing_items(self, tmp_path):
        from eval.check_eval_set import check

        data = _mk_set()
        del data["items"]
        msgs = check(_write(tmp_path, data))
        assert "缺 items" in " | ".join(msgs)

    def test_missing_status_counts(self, tmp_path):
        from eval.check_eval_set import check

        data = _mk_set()
        del data["status_counts"]
        msgs = check(_write(tmp_path, data))
        assert "缺 status_counts" in " | ".join(msgs)

    def test_items_vs_status_counts_sum_mismatch(self, tmp_path):
        """不变量 4：len(items) != sum(status_counts.values())。"""
        from eval.check_eval_set import check

        data = _mk_set()
        data["items"] = data["items"][:-1]  # 少 1 条
        msgs = check(_write(tmp_path, data))
        text = " | ".join(msgs)
        assert "items 数与 status_counts 之和 不一致" in text
        assert "len(items)=4" in text and "sum(status_counts)=5" in text

    def test_recomputed_counts_mismatch(self, tmp_path):
        """不变量 5：存储 status_counts != 实算计数，逐 key 报差异。"""
        from eval.check_eval_set import check

        data = _mk_set()
        data["items"][3]["status"] = "live"  # q4: quarantined -> live，但不改 status_counts
        msgs = check(_write(tmp_path, data))
        text = " | ".join(msgs)
        assert "status_counts 与实算不符" in text
        assert "quarantined: 存储 1 / 实算 0" in text

    def test_duplicate_qid(self, tmp_path):
        """不变量 6：qid 唯一。"""
        from eval.check_eval_set import check

        data = _mk_set()
        data["items"][4]["qid"] = "q4"
        msgs = check(_write(tmp_path, data))
        text = " | ".join(msgs)
        assert "qid 重复" in text and "q4" in text

    def test_quarantined_with_gold(self, tmp_path):
        """不变量 7：quarantined / dropped 不得带非空 gold_ids。"""
        from eval.check_eval_set import check

        data = _mk_set()
        data["items"][3]["gold_ids"] = [44]
        data["items"][4]["gold_ids"] = [55]
        msgs = check(_write(tmp_path, data))
        text = " | ".join(msgs)
        assert "quarantined/dropped 不得带 gold_ids" in text
        assert "q4" in text and "q5" in text

    def test_invalid_status_value(self, tmp_path):
        """不变量 3：status 必须在四值枚举内。"""
        from eval.check_eval_set import check

        data = _mk_set()
        data["items"][0]["status"] = "frozen"
        msgs = check(_write(tmp_path, data))
        text = " | ".join(msgs)
        assert "status 枚举违规" in text and "frozen" in text and "q1" in text


class TestCliExpectOptions:
    def test_expect_mismatch_fails(self, tmp_path):
        p = _write(tmp_path, _mk_set())
        r = _run_cli(["--eval-set", str(p), "--expect", "99"])
        assert r.returncode == 1
        assert "--expect 不符" in r.stdout and "实际 5" in r.stdout

    def test_expect_judged_mismatch_fails(self, tmp_path):
        p = _write(tmp_path, _mk_set())
        r = _run_cli(["--eval-set", str(p), "--expect-judged", "99"])
        assert r.returncode == 1
        assert "--expect-judged 不符" in r.stdout and "实际 3" in r.stdout

    def test_expect_match_ok(self, tmp_path):
        p = _write(tmp_path, _mk_set())
        r = _run_cli(["--eval-set", str(p), "--expect", "5", "--expect-judged", "3"])
        assert r.returncode == 0, r.stdout + r.stderr


class TestRunBaselineStep0:
    def test_first_cmd_is_eval_set_check(self):
        import eval.run_baseline as rb

        cmds, labels = rb.build_cmds("es.json", "mf.json", ["vec", "rrf"], "recall@5", skip_eval=False)
        assert "check_eval_set.py" in cmds[0][1]
        assert "--eval-set" in cmds[0] and "es.json" in cmds[0]
        assert labels[0].startswith("0/")

    def test_check_survives_skip_eval(self, tmp_path, monkeypatch):
        """--skip-eval 时 check 仍是第一步（不随 skip 一起被跳过）。"""
        import eval.run_baseline as rb

        (tmp_path / "results_x.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(rb, "_EVAL_DIR", tmp_path)
        cmds, labels = rb.build_cmds("es.json", "mf.json", ["vec", "rrf"], "recall@5", skip_eval=True)
        assert "check_eval_set.py" in cmds[0][1]
        assert labels[0].startswith("0/")
        assert all("run_eval" not in label for label in labels)
