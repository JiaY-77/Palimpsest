"""思维底片来源标记（``thought_origin`` / ``skeleton_kind``）的单元与回填测试。

覆盖三块：

  1. ``detect_thought_origin``   —— 形态启发式：骨架起手式 / 原生思考 / 异常输入
  2. ``decide_thought_store``    —— ``skeleton_kind`` 透传与白名单归一、
     ``thought_origin`` 判定随返回值透出
  3. ``backfill_thought_origin`` —— 存量回填：默认 dry-run 不写库、
     ``dry_run=False`` 才写、重跑幂等 0 变更

诚实边界（与 ``core.strategy.detect_thought_origin`` 的 docstring 同源）：
``thought_origin`` 是**形态启发式**——只声称「文本长得像骨架产物」，不是精确
归因；``skeleton_kind`` 是事实字段——「本轮实际注入了什么」。两者并存交叉可查
「注入了骨架但模型没照写」，宿主侧无法区分「骨架引导出的产物」与「原生思考」。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import uuid

import pytest

from core.strategy import (
    THOUGHT_TYPE,
    backfill_thought_origin,
    decide_thought_store,
    detect_thought_origin,
)

# ---------------------------------------------------------------------------
# 测试样本
# ---------------------------------------------------------------------------

# 骨架照写样本：按「模型照着骨架逐项续写」的真实形态构造（引导行起手 + 内容）。
# 引导行与 core/strategy._THOUGHT_SKELETONS 的模板一一对应。
_SKELETON_TEXTS = {
    "default": (
        "1. 要的是什么（一句话）：确认列表页空态有没有兜住\n"
        "2. 已知 / 未知：已核实入口存在，还没看空态分支\n"
        "3. 值不值得记：值得\n"
        "4. 该不该动手：先对齐"
    ),
    "code": (
        "1. 要改什么 / 新增什么（一句话）：给 thought 落库补来源标记\n"
        "2. 现状核实了吗（哪些已核实、哪些还没看）：已核实 payload 现状\n"
        "3. 失败路径（哪里会坏、坏了什么表现、怎么兜住）：判定函数抛异常会拖垮落库\n"
        "4. 怎么验（跑什么、看到什么算数）：单测覆盖三种起手式与异常输入"
    ),
    "research": (
        "1. 问题是什么（一句话）：两个回填方案选哪个\n"
        "2. 已有证据 / 还缺什么证据：已有任务回填先例，还缺锁行为证据\n"
        "3. 反例找了吗（相反结论成立会是什么样）：迭代中直接写库若不锁会自锁\n"
        "4. 结论的证据等级（实测 / 引用来源 / 推测）：实测"
    ),
}

# 原生思考样本：模型自带的推理文本，与任何骨架起手式无关（真实观测样本）。
_NATIVE_TEXTS = [
    "All done. Now report to the user.",
    "Oops, that read was unnecessary. Let me not waste more.",
    "这个报错先看堆栈第二帧，别急着改代码。",
]


# ---------------------------------------------------------------------------
# 1. detect_thought_origin —— 形态启发式
# ---------------------------------------------------------------------------


class TestDetectThoughtOrigin:
    @pytest.mark.parametrize("kind", ["default", "code", "research"])
    def test_skeleton_opener_maps_to_skeleton(self, kind):
        assert detect_thought_origin(_SKELETON_TEXTS[kind]) == "skeleton"

    def test_note_prefix_is_stripped_before_matching(self):
        """落库 content 带「底片」包裹前缀，判定前先剥掉（回填脚本处理的就是这种形态）。"""
        assert detect_thought_origin(f"[思维底片] {_SKELETON_TEXTS['default']}") == "skeleton"

    def test_native_reasoning_is_not_skeleton(self):
        for text in _NATIVE_TEXTS:
            assert detect_thought_origin(text) == "native"

    def test_empty_or_missing_text_is_unknown(self):
        assert detect_thought_origin("") == "unknown"
        assert detect_thought_origin("   ") == "unknown"
        assert detect_thought_origin(None) == "unknown"

    def test_prefix_only_is_unknown(self):
        """只有包裹前缀没有正文 → 无判定依据，unknown 而非 native。"""
        assert detect_thought_origin("[思维底片]") == "unknown"

    def test_halfwidth_punctuation_variants_still_match(self):
        """全/半角标点与空白的轻微出入不影响判定（形态启发式的合理鲁棒性）。"""
        assert detect_thought_origin("1. 要的是什么(一句话): 看看空态分支") == "skeleton"

    def test_leading_whitespace_tolerated(self):
        assert detect_thought_origin(f"\n  {_SKELETON_TEXTS['research']}") == "skeleton"


# ---------------------------------------------------------------------------
# 2. decide_thought_store —— 两个字段随落库判定透出
# ---------------------------------------------------------------------------


class TestDecideThoughtStoreOriginFields:
    def test_skeleton_kind_passthrough_and_origin_skeleton(self):
        r = decide_thought_store(f"<thought>\n{_SKELETON_TEXTS['default']}\n</thought>", skeleton_kind="default")
        assert r["store"] is True
        assert r["skeleton_kind"] == "default"
        assert r["thought_origin"] == "skeleton"

    def test_skeleton_kind_defaults_to_empty(self):
        r = decide_thought_store(f"<thought>\n{_SKELETON_TEXTS['code']}\n</thought>")
        assert r["store"] is True
        assert r["skeleton_kind"] == ""
        assert r["thought_origin"] == "skeleton"

    def test_cross_check_exposes_unfollowed_skeleton(self):
        """注入了骨架但模型没照写：事实字段与启发式字段交叉暴露差异（并存的意义）。"""
        r = decide_thought_store(f"<thought>{_NATIVE_TEXTS[0]} 值得记下来</thought>", skeleton_kind="code")
        assert r["store"] is True
        assert r["skeleton_kind"] == "code"
        assert r["thought_origin"] == "native"

    def test_skeleton_kind_out_of_whitelist_normalized_to_empty(self):
        """事实字段只接受合法 kind；未知值一律归空串，脏值不进库。"""
        r = decide_thought_store(f"<thought>\n{_SKELETON_TEXTS['code']}\n</thought>", skeleton_kind="bogus")
        assert r["skeleton_kind"] == ""

    def test_no_text_block_keeps_fact_field_but_origin_unknown(self):
        """没抽到思考段：事实字段照常透传，启发式字段因无文本而 unknown。"""
        r = decide_thought_store("普通回复，没有思考段", skeleton_kind="code")
        assert r["store"] is False
        assert r["skip_reason"] == "not_found"
        assert r["skeleton_kind"] == "code"
        assert r["thought_origin"] == "unknown"

    def test_auto_ingest_off_fields_still_present(self):
        """关闸分支同样带两个字段（返回结构一致，宿主观测不缺键）。"""
        r = decide_thought_store(f"<thought>\n{_SKELETON_TEXTS['default']}\n</thought>", auto_ingest=False)
        assert r["skip_reason"] == "auto_ingest_off"
        assert r["skeleton_kind"] == ""
        assert r["thought_origin"] == "unknown"


# ---------------------------------------------------------------------------
# 3. backfill_thought_origin —— 存量回填（iso 隔离库，回填是全库扫描型操作）
# ---------------------------------------------------------------------------


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore（照 test_task_state 的 iso_store 模式）。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_origin_iso_")
    from config import Config
    from core.trivium_store import TriviumStore
    from mcp_tools import store as shared_store

    old_db = Config.DB_PATH
    old_shared = shared_store.db_path
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    shared_store.db_path = Config.DB_PATH
    s = TriviumStore()
    try:
        yield s
    finally:
        Config.DB_PATH = old_db
        shared_store.db_path = old_shared
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def _insert_thought(s, content: str, **extra) -> int:
    payload = {"type": THOUGHT_TYPE, "content": content, "importance": 0.5, **extra}
    return s.insert_node(payload, s.embed_text(content))


class TestBackfillThoughtOrigin:
    def test_dry_run_does_not_write(self, iso_store):
        nid = _insert_thought(iso_store, f"[思维底片] {_SKELETON_TEXTS['default']}")

        result = backfill_thought_origin(iso_store, dry_run=True)

        assert result["dry_run"] is True
        assert any(c["id"] == nid for c in result["changes"])
        assert "thought_origin" not in iso_store.get_node(nid)["payload"]

    def test_apply_writes_origin_and_reports_distribution(self, iso_store):
        skel = _insert_thought(iso_store, f"[思维底片] {_SKELETON_TEXTS['code']}")
        native = _insert_thought(iso_store, f"[思维底片] {_NATIVE_TEXTS[0]}")

        result = backfill_thought_origin(iso_store, dry_run=False)

        assert result["dry_run"] is False
        assert result["scanned"] == 2
        assert result["changed"] == 2
        assert iso_store.get_node(skel)["payload"]["thought_origin"] == "skeleton"
        assert iso_store.get_node(native)["payload"]["thought_origin"] == "native"
        assert result["distribution"]["skeleton"] == 1
        assert result["distribution"]["native"] == 1

    def test_is_idempotent_on_rerun(self, iso_store):
        """--apply 后复跑：0 变更，已处理节点计入 already。"""
        nid = _insert_thought(iso_store, f"[思维底片] {_SKELETON_TEXTS['research']}")

        backfill_thought_origin(iso_store, dry_run=False)
        second = backfill_thought_origin(iso_store, dry_run=False)

        assert second["changed"] == 0
        assert second["already"] == 1
        assert nid not in [c["id"] for c in second["changes"]]
        assert iso_store.get_node(nid)["payload"]["thought_origin"] == "skeleton"

    def test_ignores_non_thought_and_never_overwrites_existing(self, iso_store):
        """非 thought 节点不碰；已带 thought_origin 的节点不覆盖（含人工改过的值）。"""
        mem_id = iso_store.insert_node(
            {"type": "memory", "content": f"普通记忆 {uuid.uuid4().hex[:8]}"},
            iso_store.embed_text("普通记忆"),
        )
        _insert_thought(iso_store, f"[思维底片] {_NATIVE_TEXTS[1]}", thought_origin="native")

        result = backfill_thought_origin(iso_store, dry_run=False)

        assert "thought_origin" not in iso_store.get_node(mem_id)["payload"]
        assert result["changed"] == 0
        assert result["already"] == 1

    def test_does_not_backfill_skeleton_kind(self, iso_store):
        """``skeleton_kind`` 是给侧上报的事实字段，存量无从得知——不编造，留缺。"""
        nid = _insert_thought(iso_store, f"[思维底片] {_SKELETON_TEXTS['default']}")

        backfill_thought_origin(iso_store, dry_run=False)

        assert "skeleton_kind" not in iso_store.get_node(nid)["payload"]
