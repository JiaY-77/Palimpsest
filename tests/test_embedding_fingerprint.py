"""向量空间指纹守卫测试（T2）
================================

全部走临时库（conftest 已把 DB_PATH 指向 session 级临时目录）+ fake embedder，
不连 Ollama、不加载模型。doctor 相关用例 monkeypatch 指纹读取/计算函数，
确定性构造 PASS / WARN / FAIL / SKIP 四种判定。
"""

import importlib.metadata
import types

import pytest

from core import embedding_fingerprint as ef

# 固定指纹（与真实环境无关，保证用例确定性）
_FP_LOCAL = {
    "space_id": "abcd1234abcd1234",
    "provider": "local",
    "model": "Qwen3-Embedding-0.6B-Q8_0.gguf",
    "dim": 1024,
    "pooling": "last",
    "engine": "llama_cpp:0.4.0",
}


@pytest.fixture(autouse=True)
def _clean_fingerprint_nodes():
    """每条用例前后清掉指纹 record 节点：用例互不污染、顺序无关。"""
    from mcp_tools import store

    def _clean():
        for nid, payload in list(store.iter_payloads()):
            if isinstance(payload, dict) and payload.get("source") == ef.FINGERPRINT_SOURCE:
                store.delete_node(nid)

    _clean()
    yield
    _clean()


# ---- 1. compute_space_id ----


def test_compute_space_id_stable_and_sensitive():
    base = ef.compute_space_id("local", "m.gguf", 1024, "last")
    assert base == ef.compute_space_id("local", "m.gguf", 1024, "last")  # 同输入同输出
    assert len(base) == 16
    assert ef.compute_space_id("ollama", "m.gguf", 1024, "last") != base  # provider 变 → 变
    assert ef.compute_space_id("local", "other.gguf", 1024, "last") != base  # model 变 → 变
    assert ef.compute_space_id("local", "m.gguf", 768, "last") != base  # dim 变 → 变


# ---- 3. read_fingerprint 无记录（放在写库用例之前，靠 fixture 保证干净） ----


def test_read_fingerprint_none_when_absent():
    from mcp_tools import store

    assert ef.read_fingerprint(store) is None


# ---- 2. write_fingerprint 是 upsert ----


def test_write_fingerprint_is_upsert():
    from mcp_tools import store

    fp1 = dict(_FP_LOCAL)
    ef.write_fingerprint(store, fp1)
    fp2 = dict(_FP_LOCAL, space_id="bbbb2222bbbb2222")
    ef.write_fingerprint(store, fp2)

    hits = [
        (nid, p) for nid, p in store.iter_payloads() if isinstance(p, dict) and p.get("source") == ef.FINGERPRINT_SOURCE
    ]
    assert len(hits) == 1  # 连续写两次仍只有 1 条
    _, payload = hits[0]
    assert payload["space_id"] == "bbbb2222bbbb2222"  # 内容是第二次的
    read_back = ef.read_fingerprint(store)
    assert read_back is not None
    assert read_back["space_id"] == "bbbb2222bbbb2222"


# ---- 4. compare：无记录 ----


def test_compare_missing_stored_gives_fix_hint():
    verdict = ef.compare(None, dict(_FP_LOCAL))
    assert verdict["ok"] is False
    assert "未记录" in verdict["reason"]
    assert "reindex --force" in verdict["hint"]


# ---- 5. compare：space_id 相同 ----


def test_compare_same_space_ok():
    stored = dict(_FP_LOCAL, recorded_at="2026-01-01T00:00:00")
    verdict = ef.compare(stored, dict(_FP_LOCAL))
    assert verdict["ok"] is True
    assert verdict["soft_warnings"] == []


# ---- 6. compare：只有 provider 不同 ----


def test_compare_provider_mismatch_named_in_reason():
    stored = dict(_FP_LOCAL, provider="ollama")
    stored["space_id"] = ef.compute_space_id("ollama", _FP_LOCAL["model"], _FP_LOCAL["dim"], _FP_LOCAL["pooling"])
    verdict = ef.compare(stored, dict(_FP_LOCAL))
    assert verdict["ok"] is False
    assert "provider" in verdict["reason"]  # reason 点名差异字段
    assert "reindex --force" in verdict["hint"]


# ---- 7. reindex --force 的 argparse 解析（不跑真重嵌） ----


def test_cli_reindex_force_flag_parses():
    from scripts.palimpsest_cli import build_parser

    p = build_parser()
    args = p.parse_args(["reindex", "--force"])
    assert args.force is True  # force 蕴含 restart 语义（cmd_reindex 内 if force: restart=True）
    assert p.parse_args(["reindex"]).force is False  # 不传 --force：行为完全不变
    both = p.parse_args(["reindex", "--force", "--restart"])  # --restart 与 --force 并存
    assert both.force is True and both.restart is True


# ---- 8. doctor：指纹不一致 → FAIL + 修复命令 ----


def test_doctor_fingerprint_mismatch_fails_with_fix(monkeypatch):
    import core.doctor as doctor_mod

    stored = dict(_FP_LOCAL, provider="openai", model="voyage-3", pooling="n/a")
    stored["space_id"] = ef.compute_space_id("openai", "voyage-3", 1024, "n/a")
    monkeypatch.setattr(ef, "read_fingerprint", lambda s: stored)
    monkeypatch.setattr(ef, "current_fingerprint", lambda s: dict(_FP_LOCAL))

    ok, detail, fix, warn = doctor_mod._check_embedding_space()
    assert ok is False
    assert "reindex --force" in fix
    assert warn is False

    # 渲染层也带出修复命令（人类可读输出）
    rendered = doctor_mod.render_text(
        {"ok": False, "checks": [{"name": "向量空间一致性", "ok": False, "detail": detail, "fix": fix}]}
    )
    assert "reindex --force" in rendered


# ---- 9. 引擎版本分级：space_id 同 engine 不同 → ok=True + 软告警，doctor WARN 不 FAIL ----


def test_engine_change_is_soft_warning(monkeypatch):
    import core.doctor as doctor_mod

    stored = dict(_FP_LOCAL, engine="llama_cpp:0.3.36")
    current = dict(_FP_LOCAL)  # space_id 相同，engine=llama_cpp:0.4.0

    verdict = ef.compare(stored, current)
    assert verdict["ok"] is True
    assert verdict["soft_warnings"]  # 非空
    assert "0.3.36" in verdict["soft_warnings"][0] and "0.4.0" in verdict["soft_warnings"][0]

    monkeypatch.setattr(ef, "read_fingerprint", lambda s: stored)
    monkeypatch.setattr(ef, "current_fingerprint", lambda s: current)
    ok, detail, _fix, warn = doctor_mod._check_embedding_space()
    assert ok is True  # WARN 而非 FAIL
    assert warn is True
    assert "0.4.0" in detail


# ---- 10. detect_engine_version 取不到 → "unknown"，且 unknown 参与比对不告警 ----


def test_detect_engine_version_unknown_and_no_noise(monkeypatch):
    def _no_pkg(name, **kwargs):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _no_pkg)
    local_stub = types.SimpleNamespace(provider="local", dim=1024)
    assert ef.detect_engine_version(local_stub) == "unknown"

    import requests as requests_mod

    def _refused(url, **kwargs):
        raise requests_mod.ConnectionError("refused")

    monkeypatch.setattr(requests_mod, "get", _refused)
    ollama_stub = types.SimpleNamespace(provider="ollama", dim=1024)
    assert ef.detect_engine_version(ollama_stub) == "unknown"

    # unknown 参与比对：不产生告警（避免每次升级 llama-cpp-python 前后探测抖动成噪音）
    old = dict(_FP_LOCAL, engine="llama_cpp:0.3.36")
    cur_unknown = dict(_FP_LOCAL, engine="unknown")
    verdict = ef.compare(old, cur_unknown)
    assert verdict["ok"] is True
    assert verdict["soft_warnings"] == []
