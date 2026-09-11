"""
tests/test_retrieval_probe.py —— 检索体检探针单元测试

覆盖：
  - evaluate_probe 纯函数判定逻辑（注入假检索结果）
  - --probe-file 非法输入 → 退出码 2
  - mem_search 调用可 monkeypatch（不依赖 Ollama）
"""

import json
import os
import sys

import pytest

# 确保 scripts 在 path 中（_common 导入依赖）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from scripts.retrieval_probe import (  # noqa: E402
    DEFAULT_PROBES,
    build_json,
    evaluate_probe,
    load_probe_file,
    run_one_probe,
)

# ---------------------------------------------------------------------------
# evaluate_probe 纯函数单测
# ---------------------------------------------------------------------------

class TestEvaluateProbe:
    """注入假 top1 结果，验证各 expect 条件的判定逻辑。"""

    def _make_top1(self, type_="memory", source_path="", title="", summary="",
                   score=0.9, node_id=1):
        return {
            "id": node_id,
            "type": type_,
            "score": score,
            "summary": summary,
            "meta": {
                "type": type_,
                "importance": 0.5,
                "status": "",
                "domain": "",
                "source_path": source_path,
                "title": title,
            },
        }

    # ---- expect_source_contains ----

    def test_source_contains_hit(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_source_contains": "0.8.6"}
        top1 = self._make_top1(source_path="03_技术学习/TriviumDB 0.8.6 压测报告.md")
        assert evaluate_probe(probe, top1) is True

    def test_source_contains_miss(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_source_contains": "0.8.6"}
        top1 = self._make_top1(source_path="some/other/file.md")
        assert evaluate_probe(probe, top1) is False

    # ---- expect_type ----

    def test_type_hit(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_type": "novel_chunk"}
        top1 = self._make_top1(type_="novel_chunk")
        assert evaluate_probe(probe, top1) is True

    def test_type_miss(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_type": "novel_chunk"}
        top1 = self._make_top1(type_="memory")
        assert evaluate_probe(probe, top1) is False

    # ---- expect_type_any ----

    def test_type_any_hit(self):
        probe = {"name": "t", "query": "q", "scope": "memory",
                 "expect_type_any": ["memory", "correction"]}
        top1 = self._make_top1(type_="correction")
        assert evaluate_probe(probe, top1) is True

    def test_type_any_miss(self):
        probe = {"name": "t", "query": "q", "scope": "memory",
                 "expect_type_any": ["memory", "correction"]}
        top1 = self._make_top1(type_="kb_chunk")
        assert evaluate_probe(probe, top1) is False

    # ---- expect_text_contains ----

    def test_text_contains_hit_in_summary(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_text_contains": "凌无咎"}
        top1 = self._make_top1(summary="凌无咎与某人关系密切")
        assert evaluate_probe(probe, top1) is True

    def test_text_contains_hit_in_title(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_text_contains": "凌无咎"}
        top1 = self._make_top1(title="凌无咎人物志", summary="无关内容")
        assert evaluate_probe(probe, top1) is True

    def test_text_contains_miss(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_text_contains": "凌无咎"}
        top1 = self._make_top1(summary="完全无关的内容", title="另一个标题")
        assert evaluate_probe(probe, top1) is False

    # ---- 组合条件 ----

    def test_combined_conditions_hit(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_type": "novel_chunk",
                 "expect_text_contains": "凌无咎"}
        top1 = self._make_top1(type_="novel_chunk",
                               summary="凌无咎与谁的关系")
        assert evaluate_probe(probe, top1) is True

    def test_combined_conditions_partial_miss(self):
        """type 对但 text 不含 → 未命中"""
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_type": "novel_chunk",
                 "expect_text_contains": "凌无咎"}
        top1 = self._make_top1(type_="novel_chunk",
                               summary="完全不同的内容")
        assert evaluate_probe(probe, top1) is False

    def test_combined_source_and_type(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_source_contains": "融合",
                 "expect_type": "kb_chunk"}
        top1 = self._make_top1(type_="kb_chunk",
                               source_path="03_技术学习/记忆服务融合方案.md")
        assert evaluate_probe(probe, top1) is True

    def test_combined_source_and_type_type_miss(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_source_contains": "融合",
                 "expect_type": "kb_chunk"}
        top1 = self._make_top1(type_="memory",
                               source_path="03_技术学习/记忆服务融合方案.md")
        assert evaluate_probe(probe, top1) is False

    # ---- 无约束条件 ----

    def test_no_expect_always_hit(self):
        probe = {"name": "t", "query": "q", "scope": "all"}
        top1 = self._make_top1()
        assert evaluate_probe(probe, top1) is True

    # ---- top1 is None ----

    def test_top1_none_miss(self):
        probe = {"name": "t", "query": "q", "scope": "all"}
        assert evaluate_probe(probe, None) is False

    def test_top1_none_with_expect_miss(self):
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_type": "memory"}
        assert evaluate_probe(probe, None) is False

    # ---- meta 字段缺失 ----

    def test_meta_missing_source_path(self):
        """meta 无 source_path 时 expect_source_contains 应 miss"""
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_source_contains": "foo"}
        top1 = {"id": 1, "type": "memory", "score": 0.5,
                "summary": "x", "meta": {"type": "memory"}}
        assert evaluate_probe(probe, top1) is False


# ---------------------------------------------------------------------------
# DEFAULT_PROBES 结构验证
# ---------------------------------------------------------------------------

class TestDefaultProbes:
    def test_all_have_required_fields(self):
        for p in DEFAULT_PROBES:
            assert "name" in p, p
            assert "query" in p, p
            assert "scope" in p, p

    def test_count(self):
        assert len(DEFAULT_PROBES) == 4


# ---------------------------------------------------------------------------
# build_json 结构验证
# ---------------------------------------------------------------------------

class TestBuildJson:
    def test_structure(self):
        results = [
            {"name": "a", "query": "q", "hit": True,
             "top1": {"id": 1, "type": "memory", "score": 0.9,
                       "summary": "s", "meta": {"type": "memory"}},
             "latency_ms": 100.0},
            {"name": "b", "query": "q2", "hit": False,
             "top1": None, "latency_ms": 50.0},
        ]
        out = build_json(results)
        assert "probes" in out
        assert "summary" in out
        assert len(out["probes"]) == 2
        s = out["summary"]
        assert s["total"] == 2
        assert s["hits"] == 1
        assert s["hit_rate"] == "1/2"
        assert "provider" in s
        assert "dim" in s
        assert "latency_min_ms" in s
        assert "latency_avg_ms" in s
        assert "latency_max_ms" in s

    def test_empty_results(self):
        out = build_json([])
        assert out["probes"] == []
        assert out["summary"]["total"] == 0
        assert out["summary"]["hits"] == 0


# ---------------------------------------------------------------------------
# --probe-file 错误处理（退出码 2）
# ---------------------------------------------------------------------------

class TestProbeFileErrors:
    def test_file_not_found(self, tmp_path):
        p = str(tmp_path / "nonexistent.json")
        with pytest.raises(SystemExit) as exc_info:
            load_probe_file(p)
        assert exc_info.value.code == 2

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json {{{", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            load_probe_file(str(p))
        assert exc_info.value.code == 2

    def test_not_array(self, tmp_path):
        p = tmp_path / "obj.json"
        p.write_text('{"name": "x"}', encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            load_probe_file(str(p))
        assert exc_info.value.code == 2

    def test_missing_name_field(self, tmp_path):
        p = tmp_path / "noname.json"
        p.write_text('[{"query": "q"}]', encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            load_probe_file(str(p))
        assert exc_info.value.code == 2

    def test_missing_query_field(self, tmp_path):
        p = tmp_path / "noquery.json"
        p.write_text('[{"name": "n"}]', encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            load_probe_file(str(p))
        assert exc_info.value.code == 2

    def test_valid_file_loads(self, tmp_path):
        p = tmp_path / "ok.json"
        data = [{"name": "test", "query": "q", "scope": "all"}]
        p.write_text(json.dumps(data), encoding="utf-8")
        result = load_probe_file(str(p))
        assert result == data


# ---------------------------------------------------------------------------
# run_one_probe monkeypatch（不连 Ollama）
# ---------------------------------------------------------------------------

class TestRunOneProbe:
    def test_hit(self, monkeypatch):
        fake_result = {
            "results": [{
                "id": 1, "type": "memory", "score": 0.9,
                "summary": "凌无咎与某人",
                "meta": {"type": "memory", "source_path": "", "title": ""},
            }]
        }
        monkeypatch.setattr(
            "scripts.retrieval_probe.mem_search",
            lambda query, scope, top_k: json.dumps(fake_result),
        )
        probe = {"name": "t", "query": "凌无咎", "scope": "memory",
                 "expect_type": "memory"}
        r = run_one_probe(probe, top_k=1)
        assert r["hit"] is True
        assert r["top1"]["id"] == 1
        assert r["latency_ms"] >= 0

    def test_miss_no_results(self, monkeypatch):
        empty = {"results": []}
        monkeypatch.setattr(
            "scripts.retrieval_probe.mem_search",
            lambda query, scope, top_k: json.dumps(empty),
        )
        probe = {"name": "t", "query": "q", "scope": "all",
                 "expect_type": "memory"}
        r = run_one_probe(probe, top_k=1)
        assert r["hit"] is False
        assert r["top1"] is None
