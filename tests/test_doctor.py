# -*- coding: utf-8 -*-
"""
doctor 命令测试 —— 覆盖 embedding 正常/不可用、维度不一致、CLI 层。

隔离策略：
  - 所有测试使用 conftest.py 的 fake embedder（autouse session fixture），
    不依赖真实 Ollama。
  - 维度不一致测试通过 monkeypatch 替换 embed_text 为 512 维版本实现，
    DB 仍为 config 默认 1024 维，自然触发不匹配。
  - Embedding 不可用测试通过 monkeypatch 替换 startup_check._check_embedding
    为始终返回失败的实现，避免真连网络。
"""

import os
import subprocess
import sys

import pytest

from core.doctor import render_text, run_doctor
from core.startup_check import _check_key_files
from mcp_tools import store


@pytest.fixture
def iso_db(tmp_path, monkeypatch):
    """隔离库夹具：把 store.db_path 指向本次测试专用临时库。"""
    db = tmp_path / "mh_test.db"
    monkeypatch.setattr(store, "db_path", str(db))
    return store, db


# ================================================================
# embedding 正常（fake embedder 自动生效）
# ================================================================

def test_doctor_all_pass(iso_db, monkeypatch):
    """fake embedder 正常：6 项全部通过，退出码 0。

    monkeypatch _check_embedding 跳过真实 HTTP 探测，
    仅验证 fake embedder 能产生非零向量——确保测试不依赖 Ollama 可达性
    （CI / OLLAMA_EMBEDDING_BASE_URL 指向死地址时仍全绿）。
    """
    import core.startup_check as sc

    def _fake_check_embedding():
        from core.trivium_store import TriviumStore
        from core.startup_check import _all_zero
        emb = TriviumStore().embed_text("ping")
        if _all_zero(emb):
            raise RuntimeError("Embedding 返回全零向量")
        return f"Embedding 服务可用 (fake, dim={len(emb)})"

    monkeypatch.setattr(sc, "_check_embedding", _fake_check_embedding)

    result = run_doctor()
    assert result["ok"]
    assert len(result["checks"]) == 6
    for c in result["checks"]:
        assert c["ok"], f"检查项 {c['name']} 不应失败"
        assert c["fix"] == ""

    text = render_text(result)
    assert "体检通过" in text
    assert "\u2705" in text
    assert "\u274c" not in text


# ================================================================
# embedding 不可用
# ================================================================

def test_doctor_embedding_unavailable(iso_db, monkeypatch):
    """Embedding 不可用时：doctor 不崩，其余项照常返回，退出码 1。"""
    import core.startup_check as sc

    def _fake_check_embedding():
        raise RuntimeError(
            "Ollama embedding 服务不可用，请确认已启动 Ollama 并 "
            "ollama pull qwen3-embedding:0.6b；"
            "若使用云端 EMBEDDING_PROVIDER=openai 请确认 EMBEDDING_API_KEY"
        )

    monkeypatch.setattr(sc, "_check_embedding", _fake_check_embedding)

    result = run_doctor()
    assert not result["ok"]
    assert len(result["checks"]) == 6

    emb_check = next(c for c in result["checks"] if c["name"] == "Embedding 服务可用")
    assert not emb_check["ok"]
    assert "Ollama" in emb_check["detail"]
    assert "ollama pull" in emb_check["fix"]

    # 维度一致性检查：fake embedder 仍可用（class 级替换未动），probe 能跑，
    # DB dim == 1024 == fake dim → 该检查通过（embedding 不可用 ≠ 维度不一致）
    dim_check = next(c for c in result["checks"] if c["name"] == "向量维度一致性")
    assert dim_check["ok"]

    other = [c for c in result["checks"]
             if c["name"] not in ("Embedding 服务可用", "向量维度一致性")]
    assert all(c["ok"] for c in other)

    text = render_text(result)
    assert "\u274c" in text
    assert "体检未通过" in text


# ================================================================
# 维度不一致
# ================================================================

def test_doctor_dimension_mismatch(iso_db, monkeypatch):
    """实测 512 维 vs 库 1024 维：维度检查失败并给出新建库步骤。"""
    import math
    import hashlib
    from core.trivium_store import TriviumStore

    def _dim512_embed(text):
        vec = [0.0] * 512
        if text:
            padded = " " + text + " "
            for i in range(len(padded) - 1):
                gram = padded[i:i + 2]
                h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:4], 16)
                vec[h % 512] += 1.0
            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]
        return vec

    monkeypatch.setattr(TriviumStore, "embed_text", staticmethod(_dim512_embed))
    s, _ = iso_db
    s.embed_text = _dim512_embed

    try:
        result = run_doctor()
        dim_check = next(
            c for c in result["checks"] if c["name"] == "向量维度一致性")
        assert not dim_check["ok"]
        assert "512" in dim_check["detail"]
        assert "1024" in dim_check["detail"]
        assert "export_all_data" in dim_check["fix"]
        assert "rebuild_db" in dim_check["fix"]
        assert "build_kb_index.py --full" in dim_check["fix"]

        text = render_text(result)
        assert "向量维度一致性" in text
        assert "\u274c" in text
    finally:
        from tests.conftest import _fake_embed
        monkeypatch.setattr(
            TriviumStore, "embed_text", staticmethod(_fake_embed))
        s.embed_text = _fake_embed


# ================================================================
# CLI 层
# ================================================================

def test_doctor_cli_subprocess():
    """通过 subprocess 调用 doctor 子命令（无 Ollama 环境下不真连）。"""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.palimpsest_cli", "doctor", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    import json
    data = json.loads(result.stdout)
    assert "ok" in data
    assert "checks" in data
    assert len(data["checks"]) == 6
    for c in data["checks"]:
        assert "name" in c
        assert "ok" in c
        assert "detail" in c
        assert "fix" in c


def test_doctor_cli_human_text(iso_db):
    """human-readable 模式输出包含 emoji 和状态。"""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.palimpsest_cli", "doctor"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = result.stdout
    assert "Palimpsest doctor" in output
    assert "体检通过" in output or "体检未通过" in output


# ================================================================
# _check_key_files: data 目录自动创建
# ================================================================

@pytest.fixture()
def _fake_project_root(tmp_path):
    """在 tmp_path 下放 config.py 和 requirements.txt（不放 data/），供测试断言。"""
    (tmp_path / "config.py").write_text("# fake config", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi", encoding="utf-8")
    return tmp_path


def test_key_files_data_auto_created(_fake_project_root):
    """data 目录缺失时：检查通过，目录被自动创建。"""
    root = _fake_project_root
    assert not (root / "data").exists()

    result = _check_key_files(root=str(root))
    assert "已自动创建" in result
    assert (root / "data").is_dir()


def test_key_files_config_missing(_fake_project_root):
    """config.py 缺失时：仍应抛出 FileNotFoundError。"""
    root = _fake_project_root
    os.remove(root / "config.py")

    with pytest.raises(FileNotFoundError, match="config.py"):
        _check_key_files(root=str(root))


def test_key_files_data_already_exists(_fake_project_root):
    """data 目录已存在时：检查通过，detail 不误报「已自动创建」。"""
    root = _fake_project_root
    (root / "data").mkdir(exist_ok=True)
    assert (root / "data").is_dir()

    result = _check_key_files(root=str(root))
    assert "已存在" in result
    assert "已自动创建" not in result
