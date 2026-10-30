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

# doctor 检查项数量：
#   ①~⑧ run_startup_check（关键文件 / Store 初始化 / FTS5 / FTS 索引完整性 / 依赖 / Embedding /
#      分块配置 / GPU 卸载配置）
#   ⑨ 向量维度一致性   ⑩ domain 字段迁移状态   ⑪ 运行时路径字符集
#   ⑫ 向量空间一致性（指纹守卫；startup_check 里同名那项是轻量 WARN 版，doctor 会跳过它）
# 新增检查项时只改这一处（原先三处硬编码 6，加第 ⑦ 项时三处齐红）。
_EXPECTED_CHECK_COUNT = 12


@pytest.fixture
def iso_db(tmp_path, monkeypatch):
    """隔离库夹具：主库与 FTS 索引都落在本次测试专用临时目录，并预置成「健康库」。

    两处都必须重定向：`TriviumStore()` 构造时读 `Config.DB_PATH`，而
    `core/fts_index._db_path()` 从同一个 Config 派生——只改 `store.db_path`
    两者都不会生效，测试会去读写 conftest 的 session 级共享临时库（顺序依赖）。

    「健康库」= 已写入当前向量空间指纹（否则指纹守卫按设计判 FAIL）+ 主库
    内容已同步进 fts 索引（否则「FTS 索引完整性」按设计判 FAIL）。这两项都是
    issue #241 引入/依赖的检查，测试库不预置就会凭空报红。
    """
    from config import Config
    from core import embedding_fingerprint as ef
    from core.fts_index import rebuild
    from core.trivium_store import TriviumStore

    db = tmp_path / "mh_test.db"
    monkeypatch.setattr(store, "db_path", str(db))
    monkeypatch.setattr(Config, "DB_PATH", str(db))

    healthy = TriviumStore()
    ef.write_fingerprint(healthy, ef.current_fingerprint(healthy))
    rebuild(healthy)
    return store, db


# ================================================================
# embedding 正常（fake embedder 自动生效）
# ================================================================


def test_doctor_all_pass(iso_db, monkeypatch):
    """fake embedder 正常：全部检查项通过，退出码 0。

    monkeypatch _check_embedding 跳过真实 HTTP 探测，
    仅验证 fake embedder 能产生非零向量——确保测试不依赖 Ollama 可达性
    （CI / OLLAMA_EMBEDDING_BASE_URL 指向死地址时仍全绿）。
    """
    import core.startup_check as sc

    def _fake_check_embedding():
        from core.startup_check import _all_zero
        from core.trivium_store import TriviumStore

        emb = TriviumStore().embed_text("ping")
        if _all_zero(emb):
            raise RuntimeError("Embedding 返回全零向量")
        return f"Embedding 服务可用 (fake, dim={len(emb)})"

    monkeypatch.setattr(sc, "_check_embedding", _fake_check_embedding)

    # 指纹守卫（⑨）：先写入当前向量空间指纹，模拟「已经重算对齐」的健康库。
    # 不写的话第 ⑨ 项按设计判 FAIL（存量库升级首见即红的语义），与本用例
    # 「全部检查项通过」的意图冲突。
    from core import embedding_fingerprint as ef
    from core.trivium_store import TriviumStore

    _store = TriviumStore()
    ef.write_fingerprint(_store, ef.current_fingerprint(_store))

    result = run_doctor()
    assert result["ok"]
    assert len(result["checks"]) == _EXPECTED_CHECK_COUNT
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
    assert len(result["checks"]) == _EXPECTED_CHECK_COUNT

    emb_check = next(c for c in result["checks"] if c["name"] == "Embedding 服务可用")
    assert not emb_check["ok"]
    assert "Ollama" in emb_check["detail"]
    assert "ollama pull" in emb_check["fix"]

    # 维度一致性检查：fake embedder 仍可用（class 级替换未动），probe 能跑，
    # DB dim == 1024 == fake dim → 该检查通过（embedding 不可用 ≠ 维度不一致）
    dim_check = next(c for c in result["checks"] if c["name"] == "向量维度一致性")
    assert dim_check["ok"]

    other = [c for c in result["checks"] if c["name"] not in ("Embedding 服务可用", "向量维度一致性")]
    assert all(c["ok"] for c in other)

    text = render_text(result)
    assert "\u274c" in text
    assert "体检未通过" in text


# ================================================================
# 维度不一致
# ================================================================


def test_doctor_dimension_mismatch(iso_db, monkeypatch):
    """实测 512 维 vs 库 1024 维：维度检查失败并给出新建库步骤。"""
    import hashlib
    import math

    from core.trivium_store import TriviumStore

    def _dim512_embed(text):
        vec = [0.0] * 512
        if text:
            padded = " " + text + " "
            for i in range(len(padded) - 1):
                gram = padded[i : i + 2]
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
        dim_check = next(c for c in result["checks"] if c["name"] == "向量维度一致性")
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

        monkeypatch.setattr(TriviumStore, "embed_text", staticmethod(_fake_embed))
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
    assert len(data["checks"]) == _EXPECTED_CHECK_COUNT
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

    with pytest.raises(FileNotFoundError, match=r"config\.py"):
        _check_key_files(root=str(root))


def test_key_files_data_already_exists(_fake_project_root):
    """data 目录已存在时：检查通过，detail 不误报「已自动创建」。"""
    root = _fake_project_root
    (root / "data").mkdir(exist_ok=True)
    assert (root / "data").is_dir()

    result = _check_key_files(root=str(root))
    assert "已存在" in result
    assert "已自动创建" not in result


# ================================================================
# ⑦ domain 字段迁移状态
# ================================================================


def test_legacy_domain_mirror_flags_node_without_domain():
    """只有 character_name 没有 domain 的节点 → 该项失败并给出迁移命令。

    用真库真节点（不经 mock）：插一个历史兼容镜像节点，检查必须发现它；
    删除后同一检查恢复通过 —— 证明它统计的确实是这类节点，而不是恒真/恒假。
    """
    from core.doctor import _check_legacy_domain_mirror

    emb = store.embed_text("legacy domain mirror probe")
    nid = store.insert_node(
        {
            "type": "memory",
            "character_name": "legacy_only",
            "content": "只有 character_name 没有 domain 的历史节点（doctor ⑦ 用例）",
        },
        emb,
    )
    try:
        ok, detail, fix = _check_legacy_domain_mirror()
        assert ok is False
        assert "character_name" in detail
        assert "migrate_domain.py" in fix
    finally:
        store.delete_node(nid)

    ok_after, detail_after, fix_after = _check_legacy_domain_mirror()
    assert ok_after is True, detail_after
    assert fix_after == ""


def test_legacy_domain_mirror_ignores_official_domain_node():
    """有正式 domain 字段的节点不算存量（即使同时带 character_name 镜像）。"""
    from core.doctor import _check_legacy_domain_mirror

    emb = store.embed_text("official domain probe")
    nid = store.insert_node(
        {
            "type": "memory",
            "domain": "hermes",
            "character_name": "hermes",
            "content": "domain 与 character_name 镜像一致的正式节点（doctor ⑦ 用例）",
        },
        emb,
    )
    try:
        ok, detail, _fix = _check_legacy_domain_mirror()
        assert ok is True, detail
    finally:
        store.delete_node(nid)


# ================================================================
# FTS 索引完整性（issue #241）
# 恢复后 fts.db 丢失 → _ensure_schema 会静默建空表 → 混合检索恒降级纯语义。
# 这组测试钉住：主库有节点而索引缺失/滞后时，必须有响亮的非 PASS 信号。
# ================================================================


@pytest.fixture
def cfg_iso_db(tmp_path, monkeypatch):
    """把 Config.DB_PATH 指向本次测试专用目录（TriviumStore 与 fts._db_path 都读它）。"""
    from config import Config

    db = str(tmp_path / "mh_fts.db")
    monkeypatch.setattr(Config, "DB_PATH", db)
    return db


def _insert_raw_nodes(db_path: str, count: int) -> None:
    """绕过 store 直插节点（模拟既有库/恢复库的初始状态）。"""
    import triviumdb

    dim = 16
    db = triviumdb.TriviumDB(db_path, dim=dim)
    db.batch_insert_with_ids(
        list(range(1, count + 1)),
        [[0.1] * dim] * count,
        [{"content": f"node {i} payload", "type": "memory"} for i in range(1, count + 1)],
    )
    db.flush()
    db.close()


def _startup_fts_item():
    from core.startup_check import run_startup_check

    result = run_startup_check()
    return next(c for c in result["checks"] if c["name"] == "FTS 索引完整性")


def test_startup_flags_missing_fts_db(cfg_iso_db):
    """主库有节点 + fts.db 缺失 → ok=False，detail 非空，fix 指向 rebuild 脚本。"""
    _insert_raw_nodes(cfg_iso_db, 2)
    item = _startup_fts_item()
    assert item["ok"] is False, item
    assert item["detail"], "缺失时必须给出可读原因，不能静默"
    assert item["fix"], "缺失时必须给出修复命令"
    assert "--repair" in item["fix"]


def test_startup_flags_empty_fts_table(cfg_iso_db):
    """主库有节点 + fts.db 存在但 mem_fts 0 行（静默空表的现场）→ 同样必须标红。"""
    import sqlite3

    _insert_raw_nodes(cfg_iso_db, 2)
    path = os.path.join(os.path.dirname(cfg_iso_db), "fts.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
            "content, node_id UNINDEXED, source_path UNINDEXED, tokenize='trigram')"
        )
        conn.commit()
    finally:
        conn.close()
    item = _startup_fts_item()
    assert item["ok"] is False, item
    assert item["detail"]
    assert item["fix"]


def test_startup_flags_lagging_fts_index(cfg_iso_db):
    """索引行数 < 节点数一半 → 「明显滞后」也要标红并给修复命令。"""
    from core.fts_index import index_node

    _insert_raw_nodes(cfg_iso_db, 4)
    index_node(1, "only one node got indexed")
    item = _startup_fts_item()
    assert item["ok"] is False, item
    assert "滞后" in item["detail"]
    assert "--repair" in item["fix"]


def test_startup_fts_ok_when_populated(cfg_iso_db):
    """索引行数正常 → ok=True，detail 带计数。"""
    from core.fts_index import index_node

    _insert_raw_nodes(cfg_iso_db, 2)
    index_node(1, "alpha content payload")
    index_node(2, "beta content payload")
    item = _startup_fts_item()
    assert item["ok"] is True, item
    assert "主库 2 / 索引 2" in item["detail"]


def test_startup_fts_ok_on_empty_store(cfg_iso_db):
    """库为空 → ok=True 且不要求索引存在（全新部署不是故障）。"""
    item = _startup_fts_item()
    assert item["ok"] is True, item


def test_doctor_reports_fts_fix_command(cfg_iso_db):
    """doctor 复用 startup 检查并显示修复命令（_suggest_fix 侧的显式分支）。"""
    _insert_raw_nodes(cfg_iso_db, 2)
    result = run_doctor()
    check = next(c for c in result["checks"] if c["name"] == "FTS 索引完整性")
    assert not check["ok"]
    assert "--repair" in check["fix"]
    text = render_text(result)
    assert "FTS 索引完整性" in text
    assert "--repair" in text
