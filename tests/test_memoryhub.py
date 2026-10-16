"""MemoryHub 记忆包读侧测试
==========================

覆盖 core.memoryhub 的读侧契约：
  1. 往返零丢失：mem_ingest 写 3 条 + 建 1 条边 → build_pack → read_pack；
  2. 非 JSON：code == "not_json"；
  3. 非记忆包：合法 JSON 但缺 format → code == "not_memoryhub"；
  4. 版本不支持：format_version=99 → code == "unsupported_version"；
  5. 只读：read_pack 前后库内节点数不变；
  6. 密钥告警：content 含 sk- 强规则 → issues 命中 secret/openai_key；
  7. 摘要截断：story 透传、preview 长度 ≤ preview_chars；
  8. 指纹稳定：同包两次相同、改 content 后不同。

隔离保证：照 tests/test_promote.py 的做法，用独立临时库（自建 TriviumStore +
临时 Config.DB_PATH + conftest 的确定性 fake embedder），并把 mcp_tools.memory
的模块级 store 指向该临时库（mem_ingest 走它写入）。绝不触碰正式库、绝不连 Ollama。
"""

import contextlib
import json
import os
import shutil
import tempfile

import pytest
from conftest import _fake_embed

import mcp_tools.memory as memory_tool
from core.memoryhub import (
    build_pack,
    pack_fingerprint,
    read_pack,
    summarize_pack,
)
from core.trivium_store import TriviumStore


@pytest.fixture
def iso_store():
    """全新临时库上的独立 TriviumStore，并把 memory 模块的 store 指向它。"""
    tmp = tempfile.mkdtemp(prefix="palimpsest_memoryhub_iso_")
    from config import Config

    old_db = Config.DB_PATH
    old_store = memory_tool.store
    Config.DB_PATH = os.path.join(tmp, "memoryhub.db")
    s = TriviumStore()
    s.embed_text = _fake_embed
    memory_tool.store = s
    try:
        yield s
    finally:
        memory_tool.store = old_store
        Config.DB_PATH = old_db
        with contextlib.suppress(Exception):
            s._acquire().close()
        shutil.rmtree(tmp, ignore_errors=True)


def _ingest(content: str, **kw) -> int:
    """经 mem_ingest 写入，返回 node_id。"""
    res = json.loads(memory_tool.mem_ingest(content, **kw))
    assert res.get("stored") is True, res
    return res["node_id"]


def _write_pack(pack: dict) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="palimpsest_memoryhub_pack_"), "pack.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False)
    return path


def _valid_pack(nodes=None, edges=None, story="") -> dict:
    return {
        "format": "memoryhub",
        "format_version": 1,
        "manifest": {"kind": "memory_pack", "story": story},
        "nodes": nodes if nodes is not None else [],
        "edges": edges if edges is not None else [],
    }


def test_roundtrip_zero_loss(iso_store):
    """往返零丢失：3 节点 + 1 边全部读回。"""
    s = iso_store
    a = _ingest("记忆甲：MemoryHub 往返测试的第一条独立内容", type="memory", domain="mh_test")
    b = _ingest("记忆乙：完全不同的第二条内容用于验证", type="memory", domain="mh_test")
    c = _ingest("记忆丙：第三条互不相关的记忆节点", type="memory", domain="mh_test")
    s.create_edge(a, b, "RELATED_TO")

    pack = build_pack(s, name="往返包", story="从前有座山")
    path = _write_pack(pack)
    result = read_pack(path)

    assert result["ok"] is True
    assert len(result["nodes"]) == 3
    assert {n["node_id"] for n in result["nodes"]} == {a, b, c}
    assert len(result["edges"]) == 1
    assert result["edges"][0]["source_id"] == a
    assert result["edges"][0]["target_id"] == b
    assert result["edges"][0]["label"] == "RELATED_TO"
    assert result["stats"]["node_total"] == 3
    assert result["stats"]["type_counts"].get("memory") == 3


def test_read_non_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json at all", encoding="utf-8")
    result = read_pack(str(path))
    assert result["ok"] is False
    assert result["code"] == "not_json"


def test_read_not_memoryhub(tmp_path):
    path = tmp_path / "plain.json"
    path.write_text(json.dumps({"hello": 1}), encoding="utf-8")
    result = read_pack(str(path))
    assert result["ok"] is False
    assert result["code"] == "not_memoryhub"


def test_read_unsupported_version(tmp_path):
    pack = _valid_pack()
    pack["format_version"] = 99
    path = tmp_path / "v99.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = read_pack(path)
    assert result["ok"] is False
    assert result["code"] == "unsupported_version"


def test_read_pack_is_read_only(iso_store):
    """read_pack 只读：前后库内节点数不变。"""
    s = iso_store
    _ingest("只读校验节点一", type="memory", domain="mh_ro")
    _ingest("只读校验节点二", type="memory", domain="mh_ro")

    before = len(list(s.iter_payloads()))
    pack = build_pack(s)
    result = read_pack(_write_pack(pack))
    after = len(list(s.iter_payloads()))

    assert result["ok"] is True
    assert before == after == 2


def test_secret_issue_detected():
    """包内 content 命中 sk- 强规则 → secret issue（openai_key）。"""
    nodes = [
        {"node_id": 1, "payload": {"type": "memory", "content": "sk-" + "a" * 30}},
        {"node_id": 2, "payload": {"type": "memory", "content": "干净的记忆内容"}},
    ]
    result = read_pack(_write_pack(_valid_pack(nodes=nodes)))

    assert result["ok"] is True
    assert result["issues"], result
    secret = [i for i in result["issues"] if i["kind"] == "secret"]
    assert secret, result["issues"]
    assert "openai_key" in secret[0]["rules"]


def test_summarize_preview_truncation():
    long_content = "这是一段很长的记忆内容" * 20
    nodes = [
        {
            "node_id": 7,
            "payload": {"type": "memory", "content": long_content, "importance": "0.8", "domain": "mh"},
        }
    ]
    summary = summarize_pack(_valid_pack(nodes=nodes, story="故事透传"), preview_chars=20)

    assert summary["ok"] is True
    assert summary["story"] == "故事透传"
    assert len(summary["nodes"]) == 1
    assert len(summary["nodes"][0]["preview"]) <= 20
    assert summary["nodes"][0]["importance"] == pytest.approx(0.8)


def test_fingerprint_stable_and_sensitive():
    pack = _valid_pack(nodes=[{"node_id": 1, "payload": {"type": "memory", "content": "原始内容"}}])
    fp1 = pack_fingerprint(pack)
    fp2 = pack_fingerprint(pack)
    assert fp1 == fp2
    assert len(fp1) == 16
    assert int(fp1, 16) == int(fp1, 16)

    pack["nodes"][0]["payload"]["content"] = "改动后的内容"
    assert pack_fingerprint(pack) != fp1
