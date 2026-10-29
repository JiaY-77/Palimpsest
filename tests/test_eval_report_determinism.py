"""eval 确定性回归测试：同输入 → 逐位相同输出（#221 §2.4）。

涵盖三处「数字逐位相同」契约（对齐 docs/RETRIEVAL_QUALITY.md §8.4 的
「同库同 manifest 连跑两次」实证）：

- run_eval._generate_report：同一份 results_data + eval_set 生成两次报告字符串相等
- eval.significance.per_query_scores：同一份输入两次逐题得分相等
- eval.split_eval_set.temporal_split：同一份输入两次切分的 qid 归属相等
- eval.run_baseline.build_cmds：同参数两次的命令序列相等（纯函数拼装）

配对 bootstrap 的确定性已由 tests/test_eval_significance.py 覆盖。
import run_eval 会触发 import 期 _copy_db_to_tmp（同 test_eval_tmp_copy_guard
的处理：保存/恢复 DB_PATH）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture()
def restore_db_path():
    saved = os.environ.get("DB_PATH")
    yield
    if saved is None:
        os.environ.pop("DB_PATH", None)
    else:
        os.environ["DB_PATH"] = saved


def _mk_eval_set() -> dict:
    """v2 风格小型题集：带 status、含一条多 gold 条目（#174 语义）。"""
    return {
        "version": 2,
        "seed": 7,
        "items": [
            # 判卷：reresolved 单 gold
            {
                "qid": "qa",
                "query": "qa?",
                "kind": "semantic",
                "gold_ids": [11],
                "status": "reresolved",
                "layer": "hermes",
            },
            # 判卷：live 人工确认条目，多 gold（命中任一即命中）
            {
                "qid": "qb",
                "query": "qb?",
                "kind": "semantic",
                "gold_ids": [22, 23],
                "status": "live",
                "layer": "hermes",
            },
            # 判卷：单 gold
            {"qid": "qc", "query": "qc?", "kind": "entity", "gold_ids": [31], "status": "reresolved", "layer": "kb"},
            # 不判卷：quarantined（gold 置空）
            {"qid": "qd", "query": "qd?", "kind": "semantic", "gold_ids": [], "status": "quarantined", "layer": "kb"},
            # 负样本
            {"qid": "qn", "query": "qn?", "kind": "negative", "gold_ids": [], "status": "live", "layer": "negative"},
        ],
    }


def _mk_results() -> dict:
    return {
        "qa": {
            "fts": {"ids": [11, 40], "scores": [None, None]},
            "vec": {"ids": [40, 11], "scores": [0.9, 0.8]},
            "rrf": {"ids": [11, 40], "scores": [0.03, 0.01]},
            "cascade": {"ids": [40, 11], "scores": [0.7, 0.6]},
        },
        "qb": {
            "fts": {"ids": [23], "scores": [None]},
            "vec": {"ids": [22], "scores": [0.95]},
            "rrf": {"ids": [22, 23], "scores": [0.04, 0.02]},
            "cascade": {"ids": [23, 22], "scores": [0.8, 0.7]},
        },
        "qc": {
            "fts": {"ids": [], "scores": []},
            "vec": {"ids": [31], "scores": [0.86]},
            "rrf": {"ids": [31], "scores": [0.02]},
            "cascade": {"ids": [], "scores": []},
        },
        "qd": {
            "fts": {"ids": [99], "scores": [None]},
            "vec": {"ids": [99], "scores": [0.4]},
            "rrf": {"ids": [99], "scores": [0.01]},
            "cascade": {"ids": [99], "scores": [0.3]},
        },
        "qn": {
            "fts": {"ids": [], "scores": []},
            "vec": {"ids": [], "scores": []},
            "rrf": {"ids": [], "scores": []},
            "cascade": {"ids": [], "scores": []},
        },
    }


class TestReportDeterminism:
    def test_report_generation_is_deterministic(self, restore_db_path):
        import eval.run_eval as re_mod

        eval_set = _mk_eval_set()
        results = _mk_results()
        src = {11: "a/11.md", 22: "a/22.md", 23: "a/22.md", 31: "a/31.md"}
        sha = "0" * 64
        r1 = re_mod._generate_report(results, eval_set, ["fts", "vec", "rrf", "cascade"], sha, sha, src)
        r2 = re_mod._generate_report(results, eval_set, ["fts", "vec", "rrf", "cascade"], sha, sha, src)
        assert r1 == r2

    def test_report_tags_subsets_with_counts(self, restore_db_path):
        """#221 §2.5：报告 printout 必须标注子集名与条数。"""
        import eval.run_eval as re_mod

        report = re_mod._generate_report(_mk_results(), _mk_eval_set(), ["vec"], "0" * 64, "0" * 64, {})
        # 题集状态标注
        assert "live" in report and "reresolved" in report and "quarantined" in report
        # 判卷子集与规模标注
        assert "(n=" in report
        assert "多 gold" in report and "单 gold" in report

    def test_scalar_gold_ids_backward_compatible(self, restore_db_path):
        """旧题集单值 gold_ids（标量）也要能判卷（#174 向后兼容）。"""
        import eval.run_eval as re_mod

        eval_set = {
            "version": 1,
            "items": [{"qid": "qs", "query": "s?", "kind": "semantic", "gold_ids": 11, "layer": "kb"}],
        }
        results = {"qs": {"vec": {"ids": [11], "scores": [0.9]}}}
        report = re_mod._generate_report(results, eval_set, ["vec"], "0" * 64, "0" * 64, {})
        assert "| recall@1 |" in report and "1.0000" in report


class TestPerQueryDeterminism:
    def test_per_query_scores_deterministic(self):
        from eval.significance import metric_fn, per_query_scores

        eval_set = _mk_eval_set()
        items = {it["qid"]: it for it in eval_set["items"]}
        results = _mk_results()
        fn = metric_fn("recall@5")
        s1 = per_query_scores(results, items, "vec", fn)
        s2 = per_query_scores(results, items, "vec", fn)
        # qb 的 vec top-5 命中 gold {22,23} 中的 22 → recall = 1/2（任一命中即有贡献）
        assert s1 == s2 == {"qa": 1.0, "qb": 0.5, "qc": 1.0}

    def test_multi_gold_hit_any_counts(self):
        from eval.significance import metric_fn, per_query_scores

        items = {"qb": _mk_eval_set()["items"][1]}
        results = _mk_results()
        # vec 只返回 22，fts 只返回 23 —— 任一 gold 进入 top-1 谁都得满分
        for mode in ["vec", "fts"]:
            assert per_query_scores(results, items, mode, metric_fn("mrr@1")) == {"qb": 1.0}


class TestSplitDeterminism:
    def test_temporal_split_is_deterministic(self):
        from eval.split_eval_set import temporal_split

        items = _mk_eval_set()["items"]
        created = {11: 100.0, 22: 200.0, 23: 900.0, 31: 50.0}
        _, _, m1 = temporal_split(items, created, tune_fraction=2 / 3)
        _, _, m2 = temporal_split(items, created, tune_fraction=2 / 3)
        assert m1["tune_qids"] == m2["tune_qids"]
        assert m1["held_out_qids"] == m2["held_out_qids"]
        # 时间戳齐全才可判：qc(50, tune 侧)、qa(100, tune 侧)、qb(最早 gold 200, held)
        assert m1["tune_qids"] == ["qc", "qa"]
        assert m1["held_out_qids"] == ["qb"]


class TestBaselineCmdComposition:
    def test_build_cmds_deterministic_and_shape(self):
        import eval.run_baseline as rb

        cmds1, labels1 = rb.build_cmds(
            "es.json", "mf.json", ["fts", "vec", "rrf", "cascade"], "recall@5", skip_eval=False
        )
        cmds2, labels2 = rb.build_cmds(
            "es.json", "mf.json", ["fts", "vec", "rrf", "cascade"], "recall@5", skip_eval=False
        )
        assert [len(c) for c in cmds1] == [len(c) for c in cmds2] and labels1 == labels2
        # 三步：run_eval → split(pinned) → 3 组 significance（模式全集两两配）
        assert any("run_eval" in label for label in labels1)
        assert any("split" in label for label in labels1)
        assert sum(1 for label in labels1 if "significance" in label) == 3
        # significance 命令带 manifest，pinned 复跑可复现
        sig_cmd = next(c for c, label in zip(cmds1, labels1, strict=True) if "significance" in label)
        assert "--split-manifest" in sig_cmd

    def test_build_cmds_skip_eval_requires_results(self, tmp_path, monkeypatch):
        import eval.run_baseline as rb

        monkeypatch.setattr(rb, "_EVAL_DIR", tmp_path)
        with pytest.raises(SystemExit):
            rb.build_cmds("es.json", "mf.json", ["vec", "rrf"], "recall@5", skip_eval=True)


class TestPinnedResplit:
    def _old_manifest(self) -> dict:
        return {
            "tune_qids": ["qa", "qc"],
            "held_out_qids": ["qb"],
            "unplaced_qids": {
                "negative_or_no_gold": ["qn", "qd"],
                "missing_timestamp": ["qx"],
                "new_qids_not_in_manifest": [],
            },
            "boundary_created_at": 100.0,
            "boundary_created_at_iso": "2026-09-05T00:00:00+00:00",
            "split_field": "created_at",
            "tune_fraction": 0.8,
            "notes": ["old note"],
        }

    def _items(self) -> list[dict]:
        items = _mk_eval_set()["items"]  # qa/qb/qc/qd/qn
        items.append({"qid": "qx", "kind": "semantic", "gold_ids": [55], "status": "dropped", "layer": "kb"})
        items.append({"qid": "qnew", "kind": "semantic", "gold_ids": [66], "status": "live", "layer": "hermes"})
        return items

    def test_pin_preserves_assignment(self):
        from eval.split_eval_set import pinned_resplit

        _, _, m = pinned_resplit(self._items(), self._old_manifest())
        assert m["tune_qids"] == ["qa", "qc"]
        assert m["held_out_qids"] == ["qb"]
        assert m["boundary_created_at"] == 100.0
        assert m["unplaced_qids"]["missing_timestamp"] == ["qx"]

    def test_pin_unplaced_lists_carry_so_unplaced_old_not_reported_new(self):
        from eval.split_eval_set import pinned_resplit

        _, _, m = pinned_resplit(self._items(), self._old_manifest())
        # qd/qn（负面与无 gold）直接重算；qx（旧 missing_timestamp）原样携带；
        # 只有 qnew 才是真正的「新增」——旧的 unplaced 不会被当成新增反复上报
        assert m["unplaced"]["new_qids_not_in_manifest"] == ["qnew"]
        assert set(m["unplaced_qids"]["negative_or_no_gold"]) == {"qn", "qd"}

    def test_pin_legacy_manifest_without_lists_reports_then_stabilizes(self):
        from eval.split_eval_set import pinned_resplit

        old = {k: v for k, v in self._old_manifest().items() if k != "unplaced_qids"}
        _, _, m1 = pinned_resplit(self._items(), old)
        # 旧格式没有未放置名单：未放置带 gold 正样本（qx / qnew）会被报一轮
        assert set(m1["unplaced"]["new_qids_not_in_manifest"]) == {"qx", "qnew"}
        # 第二轮：名单持久携带（不再当「新增」扩列，但也绝不缩水/消失）
        _, _, m2 = pinned_resplit(self._items(), m1)
        assert set(m2["unplaced"]["new_qids_not_in_manifest"]) == {"qx", "qnew"}
        assert m2["tune_qids"] == m1["tune_qids"] and m2["held_out_qids"] == m1["held_out_qids"]

    def test_pin_overlap_qids_raises(self):
        from eval.split_eval_set import pinned_resplit

        bad = self._old_manifest()
        bad["held_out_qids"] = ["qb", "qa"]
        with pytest.raises(ValueError, match="qids in both sides"):
            pinned_resplit(self._items(), bad)


class TestStatusFieldGuard:
    def test_normalize_rejects_bad_gold(self):
        from eval.metrics import normalize_gold_ids

        with pytest.raises(ValueError):
            normalize_gold_ids({"oops": 1})
        with pytest.raises(ValueError):
            normalize_gold_ids(["12", None])

    def test_normalize_accepts_legacy_scalar(self):
        from eval.metrics import normalize_gold_ids

        assert normalize_gold_ids(11) == [11]
        assert normalize_gold_ids("11") == [11]
        assert normalize_gold_ids([11, 12, 11]) == [11, 12]  # 去重保序
        assert normalize_gold_ids(None) == []
        assert normalize_gold_ids([]) == []
        assert normalize_gold_ids(["12"]) == [12]
