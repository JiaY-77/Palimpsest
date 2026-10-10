"""
检索通道降级留痕 + 运行时路径预检测试。

覆盖本次改动的失败路径（「写代码先想失败路径」纪律）：

A. core.fts_index.search_fts_status 的三态契约
   - "empty"    空查询
   - "degraded" 索引文件不存在 / 查询抛异常
   - "ok"       正常执行（有无命中都算 ok）
   并验证 search_fts() 保持向后兼容（只返回 rows）。

B. mcp_tools.memory 混合检索把通道状态透出
   - FTS 通道 degraded 时仍返回结果，但 channels["fts"] == "degraded" 且 logger 有 warning
   - 正常时 channels["fts"] == "ok"

C. core.doctor._check_path_ascii 的判定
   - 全 ASCII 路径 → ok
   - 非 ASCII 路径但可写 → ok（只提示，不报错）
   - 非 ASCII 路径不可写 → 失败并给修复方向

隔离：沿用 conftest 的临时库与 fake embedder，不碰生产库。
"""

import logging

# ================================================================
# A. search_fts_status 三态
# ================================================================


def test_search_fts_status_empty_query():
    """空查询/None → ("empty")，且不执行检索。"""
    from core.fts_index import search_fts, search_fts_status

    for q in ("", "   ", None):
        rows, status = search_fts_status(q)
        assert rows == []
        assert status == "empty"
    # 向后兼容包装：空查询返回空列表
    assert search_fts("") == []
    assert search_fts(None) == []


def test_search_fts_status_degraded_when_index_missing(tmp_path, monkeypatch):
    """索引文件不存在 → ("degraded")：这是「通道不可用」，与「无命中」不同。"""
    from config import Config
    from core import fts_index

    # 指向一个不存在索引文件的目录
    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "sub" / "mh.db"))
    rows, status = fts_index.search_fts_status("中文查询")
    assert rows == []
    assert status == "degraded"
    # 向后兼容包装仍返回空列表
    assert fts_index.search_fts("中文查询") == []


def test_search_fts_status_ok_with_hits(tmp_path, monkeypatch):
    """索引可查且有命中 → ("ok")，rows 非空。"""
    from config import Config
    from core import fts_index

    db = tmp_path / "mh.db"
    monkeypatch.setattr(Config, "DB_PATH", str(db))
    # 写入一条索引（写入口建表）
    fts_index.index_node(1, "主人喜欢喝美式咖啡不加糖", "")
    rows, status = fts_index.search_fts_status("美式咖啡")
    assert status == "ok"
    assert any(r.get("node_id") == 1 for r in rows)


def test_search_fts_status_ok_when_no_hit(tmp_path, monkeypatch):
    """索引可查但无命中 → ("ok") + 空 rows（关键：不得误判为 degraded）。"""
    from config import Config
    from core import fts_index

    db = tmp_path / "mh.db"
    monkeypatch.setattr(Config, "DB_PATH", str(db))
    fts_index.index_node(1, "完全无关的内容", "")
    rows, status = fts_index.search_fts_status("量子纠缠拓扑绝缘体")
    assert rows == []
    assert status == "ok"


def test_search_fts_status_degraded_on_exception(tmp_path, monkeypatch):
    """查询抛异常 → ("degraded")，不向上抛（契约：检索侧天然降级）。"""
    from config import Config
    from core import fts_index

    db = tmp_path / "mh.db"
    db.write_text("")  # 建一个非法/空文件，触发连接或查询异常
    monkeypatch.setattr(Config, "DB_PATH", str(db))

    # 强制连接直接抛异常，验证降级而非崩溃
    def _boom(*a, **kw):
        raise sqlite3_error()

    def sqlite3_error():
        import sqlite3

        return sqlite3.OperationalError("simulated failure")

    monkeypatch.setattr(fts_index.sqlite3, "connect", _boom)
    rows, status = fts_index.search_fts_status("任意查询")
    assert rows == []
    assert status == "degraded"


# ================================================================
# B. 混合检索透出通道状态
# ================================================================


def _no_op_fts_status(q, limit=10):
    return [], "degraded"


def test_hybrid_rrf_marks_fts_degraded(monkeypatch, caplog):
    """FTS 通道降级时：RRF 仍返回（来自语义通道），channels 标 degraded 并留 warning。"""
    import mcp_tools.memory as mem

    monkeypatch.setattr(mem, "search_fts_status", _no_op_fts_status)
    # 语义侧返回空，避免依赖真实库
    monkeypatch.setattr(mem, "_sem_candidate_items", lambda *a, **kw: [])

    with caplog.at_level(logging.WARNING, logger="mcp_tools.memory"):
        items, channels = mem._hybrid_rrf(
            "查询", "all", "", "", top_k=5, fts_limit=10, block="", include_outdated=False
        )

    assert items == []
    assert channels["fts"] == "degraded"
    assert any("FTS 通道不可用" in r.message for r in caplog.records)


def test_hybrid_rrf_channels_ok_when_fts_ok(monkeypatch):
    """FTS 通道正常：channels["fts"] == "ok"，无降级 warning。"""
    import mcp_tools.memory as mem

    monkeypatch.setattr(mem, "search_fts_status", lambda q, limit=10: ([], "ok"))
    monkeypatch.setattr(mem, "_sem_candidate_items", lambda *a, **kw: [])

    _items, channels = mem._hybrid_rrf("查询", "all", "", "", top_k=5, fts_limit=10, block="", include_outdated=False)
    assert channels["fts"] == "ok"
    assert channels["semantic"] == "empty"


def test_hybrid_cascade_marks_fts_degraded(monkeypatch, caplog):
    """级联模式下 FTS 降级同样留痕并退化为纯语义。"""
    import mcp_tools.memory as mem

    monkeypatch.setattr(mem, "search_fts_status", _no_op_fts_status)
    monkeypatch.setattr(mem, "_sem_candidate_items", lambda *a, **kw: [])

    with caplog.at_level(logging.WARNING, logger="mcp_tools.memory"):
        _items, channels = mem._hybrid_cascade(
            "查询", "all", "", "", top_k=5, fts_limit=10, block="", include_outdated=False
        )

    assert channels["fts"] == "degraded"
    assert any("FTS 通道不可用" in r.message for r in caplog.records)


def test_hybrid_search_impl_exposes_channels(monkeypatch):
    """对外结果 dict 含 channels（MCP 客户端可据此判断检索质量是否降级）。"""
    import mcp_tools.memory as mem

    monkeypatch.setattr(mem, "search_fts_status", lambda q, limit=10: ([], "degraded"))
    monkeypatch.setattr(mem, "_sem_candidate_items", lambda *a, **kw: [])

    result = mem._hybrid_search_impl("查询", top_k=5, mode="rrf")
    assert "channels" in result
    assert result["channels"]["fts"] == "degraded"


# ================================================================
# C. doctor 路径预检
# ================================================================


def test_check_path_ascii_ascii_ok(tmp_path, monkeypatch):
    """全 ASCII 路径 → ok=True，detail 提示均为 ASCII。"""
    import core.doctor as doctor
    from config import Config

    monkeypatch.setattr(Config, "DB_PATH", str(tmp_path / "data" / "mh.db"))
    ok, detail, fix = doctor._check_path_ascii()
    assert ok
    assert "ASCII" in detail
    assert fix == ""


def test_check_path_ascii_non_ascii_writable_ok(tmp_path, monkeypatch):
    """非 ASCII 路径但可正常读写 → ok=True（只提示，不误报为故障）。"""
    import core.doctor as doctor
    from config import Config

    zh_dir = tmp_path / "中文目录"
    zh_dir.mkdir(parents=True, exist_ok=True)
    # _check_path_ascii 内部经 TriviumStore() 读 Config.DB_PATH，故 patch 该源
    monkeypatch.setattr(Config, "DB_PATH", str(zh_dir / "mh.db"))

    ok, detail, fix = doctor._check_path_ascii()
    assert ok, f"非 ASCII 但可写应通过，实际 detail={detail}"
    assert "非 ASCII" in detail
    assert fix == ""


def test_check_path_ascii_non_ascii_unwritable_fails(tmp_path, monkeypatch):
    """非 ASCII 路径不可写 → ok=False 并给出改英文目录的修复方向。"""
    import core.doctor as doctor
    from config import Config

    zh_dir = tmp_path / "中文目录"
    zh_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Config, "DB_PATH", str(zh_dir / "mh.db"))

    real_open = open

    def _deny(path, *a, **kw):
        # 只对写探针失败，不影响 pytest 自身 IO
        if str(path).endswith(".palimpsest_write_probe"):
            raise PermissionError("Access is denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _deny)

    ok, detail, fix = doctor._check_path_ascii()
    assert not ok
    assert "不可写" in detail
    assert "英文" in fix
