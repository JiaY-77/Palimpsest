"""
检索 excerpt 逐字截取回归测试
============================

核心契约（issue：summary 是硬截断不是摘要）：

  1. `_excerpt` 纯函数：逐字截取不改写原文，截断点优先落句边界
     （。！？.? / 换行）；无句读退化为词边界（空格/逗号）；仍无硬截断
     —— 覆盖四种输入：中英混合 / 无标点长句 / 短于阈值 / 含换行
  2. `summary` 150 字硬截断行为不变（既有宿主与 UI 的兼容面）
  3. 检索工具（mem_search / mem_hybrid_search / mem_retrieve）每条结果
     都带 `excerpt` 字段
  4. excerpt 长度随 config.SEARCH_EXCERPT_CHARS（默认 400）走

隔离说明：端到端用例用【自建独立临时库】（iso_store 模式，同
test_memory_tier），不经由 conftest 的 session 级共享库。
"""

import contextlib
import json
import os
import shutil
import tempfile

import pytest

from config import Config
from mcp_tools._common import _excerpt, _shorten
from mcp_tools.memory import _fts_only_item, _mem_search_impl, mem_retrieve

EXCERPT_DEFAULT = 400


# ---------------------------------------------------------------------------
# 1. _excerpt 纯函数契约（不碰库）
# ---------------------------------------------------------------------------
class TestExcerptFunction:
    def test_mixed_cn_en_sentence_boundary(self):
        """中英混合：截断落在窗口内最后一个句号后，且句号保留。"""
        segments = [f"这是第{i}条结论句。" for i in range(30)]  # 中文句号结尾
        segments.append("An English sentence ends here.")
        text = "".join(segments) * 3  # 远超 400 字符
        out = _excerpt(text, EXCERPT_DEFAULT)
        assert len(out) <= EXCERPT_DEFAULT
        assert out.startswith("这是第0条结论句。")
        # 截断点必须是原文窗口内的一个句号（verbatim：不改写）
        assert out[-1] == "。"
        assert text.startswith(out)
        # 不能短于前半段门槛（句边界在前半段时不算合理截断点）
        assert len(out) >= EXCERPT_DEFAULT // 2

    def test_cn_en_mixed_keeps_english_punctuation(self):
        """英文句点也在句界集合里：结尾可落在英文句点。"""
        first = "中文先来一句。"
        english = "English clause. " * 30  # 16 字符/段，共 480
        text = first + english
        out = _excerpt(text, 400)
        assert len(out) <= 400
        assert text.startswith(out)
        # 最后一个保留字符是某个英文句点（或句号）
        assert out[-1] in ".。"

    def test_no_punctuation_long_hard_truncate(self):
        """无句读、无词边界（无空格无逗号）→ 硬截断，与窗口逐字一致。"""
        text = "无标点长串" * 120  # 600 字符
        out = _excerpt(text, EXCERPT_DEFAULT)
        assert out == text[:EXCERPT_DEFAULT]
        assert len(out) == EXCERPT_DEFAULT

    def test_no_sentence_but_spaces_falls_to_word_boundary(self):
        """无句读但有空格 → 退化为词边界（截在词尾，不留在前半段）。"""
        filler = "x" * 20
        text = " ".join([filler] * 40)  # 40 个词，839 字符
        out = _excerpt(text, EXCERPT_DEFAULT)
        assert len(out) <= EXCERPT_DEFAULT
        assert len(out) >= EXCERPT_DEFAULT // 2
        assert text.startswith(out.rstrip())
        assert out[-1] != "，"

    def test_shorter_than_limit_verbatim(self):
        """短于阈值：原文原样返回，一字不动。"""
        text = "一句话就完了。"
        assert _excerpt(text, EXCERPT_DEFAULT) == text

    def test_empty_and_edge_inputs(self):
        assert _excerpt("", EXCERPT_DEFAULT) == ""
        assert _excerpt(None, EXCERPT_DEFAULT) == ""  # 兼容 _shorten 同款空值入参
        assert _excerpt("一字", 1) == "一"
        assert _excerpt("一字", 0) == ""  # 长度 0 不炸、不无限

    def test_newline_is_boundary(self):
        """换行按句边界处理：截断留在行尾，不加尾随空白。"""
        lines = [f"第{i}行结论甲乙丙丁" for i in range(40)]  # 每行 11 字 + 换行
        text = "\n".join(lines)
        out = _excerpt(text, EXCERPT_DEFAULT)
        assert len(out) <= EXCERPT_DEFAULT
        assert text.startswith(out)
        # 截断停在一行末尾：下一个原文字符是换行或已到原文结尾
        nxt = text[len(out)] if len(out) < len(text) else ""
        assert nxt == "\n"
        assert out[-1] != "\n"

    def test_decimal_point_not_sentence_boundary(self):
        """英文小数点（两侧均为数字）不当句点：不把 3.14 拦腰截断。"""
        head = "pi approx " + ("数值批注句子。" * 3)
        text = head + "value is 3." + "1" * 300  # 长数字串带小数点，且窗口内靠后有另一句界
        out = _excerpt(text, 60)
        # 若断在句界，绝不停在数字中间的点后
        assert not out.endswith("3.")

    def test_final_excerpt_never_exceeds_limit_and_verbatim(self):
        """任何输入：输出是原文前缀、不超过上限（不变式）。"""
        for text in ("。" * 5, "x.y.z" * 200, "字" * 700, "a b c d" * 150, "㊙" + "文" * 399):
            out = _excerpt(text, EXCERPT_DEFAULT)
            assert len(out) <= EXCERPT_DEFAULT
            assert text.startswith(out)


# ---------------------------------------------------------------------------
# 2. summary 兼容契约：150 字硬截断行为不变
# ---------------------------------------------------------------------------
class TestSummaryUnchanged:
    def test_shorten_signature_and_behaviour(self):
        assert _shorten("字" * 500, 150) == "字" * 150
        assert _shorten("短", 150) == "短"
        assert _shorten("", 150) == ""

    def test_config_default_is_400(self):
        assert Config.SEARCH_EXCERPT_CHARS == EXCERPT_DEFAULT


# ---------------------------------------------------------------------------
# 3. 端到端：检索结果带 excerpt（独立临时库）
# ---------------------------------------------------------------------------
@pytest.fixture
def iso_store():
    tmp = tempfile.mkdtemp(prefix="palimpsest_excerpt_iso_")
    old = Config.DB_PATH
    Config.DB_PATH = os.path.join(tmp, "iso.db")
    from core.trivium_store import TriviumStore

    s = TriviumStore()
    try:
        yield s
    finally:
        Config.DB_PATH = old
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def iso(iso_store, monkeypatch):
    """把 memory 模块与 _common 单例的 store 都指向隔离库。"""
    from mcp_tools import _common, memory

    monkeypatch.setattr(memory, "store", iso_store)
    monkeypatch.setattr(_common, "store", iso_store)
    return iso_store


def _insert(store, payload: dict, content: str) -> int:
    node_payload = dict(payload)
    node_payload["content"] = content
    return store.insert_node(node_payload, store.embed_text(content))


def test_mem_search_results_have_excerpt(iso):
    q = "excerpt 回归甲：北极航道破冰船柴油机维护周期"
    long_content = ("普查结论句。" * 60) + q  # 360 字（短于 400，原样）与长文两种
    _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, long_content)
    long_content2 = ("普查结论句。" * 400) + q  # 超长
    _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, long_content2)

    out = _mem_search_impl(q, scope="all", top_k=5)
    results = out.get("results", [])
    assert results
    for r in results:
        assert "summary" in r, "summary 字段必须保留"
        assert "excerpt" in r, "检索结果必须新增 excerpt 字段"
        assert r["excerpt"].startswith(r["summary"] or ""), "excerpt 前缀必须包含 summary"
        assert len(r["excerpt"]) <= Config.SEARCH_EXCERPT_CHARS
    # 超长节点：summary 仍被硬截到 150 字；excerpt 截在句边界（400 字窗口内
    # 最后一个 。 在第 66 句末尾，恰好 396 字）
    longest = max(results, key=lambda r: len(r["excerpt"]))
    assert longest["excerpt"] == "普查结论句。" * 66
    assert longest["summary"] == ("普查结论句。" * 400)[:150]


def test_mem_search_empty_results_shape(iso):
    out = _mem_search_impl("完全不相干的查询没有命中内容", scope="all", top_k=5)
    assert out.get("results") == []


def test_fts_only_item_has_excerpt(iso):
    node_id = _insert(iso, {"type": "memory", "importance": 0.5}, "字" * 500)
    item = _fts_only_item(node_id, "all", "", "")
    assert item is not None
    assert "excerpt" in item
    assert item["excerpt"] == "字" * Config.SEARCH_EXCERPT_CHARS
    assert item["summary"] == "字" * 150


def test_mem_retrieve_has_excerpt(iso):
    q = "excerpt 回归乙：马达加斯加狐猴声学监测周期"
    _insert(iso, {"type": "memory", "domain": "hermes", "importance": 0.5}, q + "。" + "结论句。" * 80)
    out_str = mem_retrieve(q, top_k=3)
    data = json.loads(out_str) if isinstance(out_str, str) else out_str
    results = data.get("results", [])
    assert results
    for r in results:
        assert "excerpt" in r
        assert len(r["excerpt"]) <= Config.SEARCH_EXCERPT_CHARS
