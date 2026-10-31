"""
TriviumDB 0.8.3 特性测试 —— Prepared TQL（P0）
======================================================
覆盖：prepare_tql / execute_prepared_tql / PreparedTql.parameter_names；
缺参 / 多参 / 非法值 fail-closed。

已知观察：非有限数值（inf）在 0.8.3 中不触发报错（见 test 注释，属待核实行为）。

隔离保证：%TEMP%/tdb_ftest/ 独立临时库。
"""

import math
import os
import tempfile

import pytest
import triviumdb


@pytest.fixture
def tdb():
    _dir = os.path.join(tempfile.gettempdir(), "tdb_ftest")
    os.makedirs(_dir, exist_ok=True)
    path = os.path.join(_dir, "prepared.db")
    for f in os.listdir(_dir):
        if f.startswith("prepared"):
            os.remove(os.path.join(_dir, f))
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)

    def vec(text, dim=8):
        out = [0.0] * dim
        for ch in text:
            out[ord(ch) % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]

    for i in range(4):
        db.insert(vec(f"note {i}"), {"type": "note", "num": i})
    yield db
    db.close()


def test_parameter_names(tdb):
    prepared = tdb.prepare_tql('FIND {type: "note"} RETURN $bonus + 1 AS score')
    names = prepared.parameter_names()
    assert isinstance(names, list)
    assert "bonus" in names


def test_execute_prepared_ok(tdb):
    prepared = tdb.prepare_tql('FIND {type: "note"} RETURN $bonus AS score')
    rows = tdb.execute_prepared_tql(prepared, {"bonus": 4})
    assert len(rows) == 4
    # 0.8.5+：标量别名列直接映射值，不再自动附加节点 _ 绑定（节点绑定只在 RETURN * / RETURN n 时出现）
    for r in rows:
        assert r.row["score"] == 4


def test_prepared_repeatable(tdb):
    """同一 Prepared 对象可重复绑定执行。"""
    prepared = tdb.prepare_tql('FIND {type: "note"} RETURN $n AS score')
    r1 = tdb.execute_prepared_tql(prepared, {"n": 1})
    r2 = tdb.execute_prepared_tql(prepared, {"n": 2})
    assert len(r1) == len(r2) == 4


def test_missing_param_fails(tdb):
    prepared = tdb.prepare_tql('FIND {type: "note"} RETURN $bonus AS score')
    with pytest.raises(RuntimeError):
        tdb.execute_prepared_tql(prepared, {})


def test_extra_param_fails(tdb):
    prepared = tdb.prepare_tql('FIND {type: "note"} RETURN $bonus AS score')
    with pytest.raises(RuntimeError):
        tdb.execute_prepared_tql(prepared, {"bonus": 4, "extra": 1})


def test_wrong_param_name_fails(tdb):
    prepared = tdb.prepare_tql('FIND {type: "note"} RETURN $bonus AS score')
    # 提供同参数名外的缺失名 → 缺参，应报错
    with pytest.raises(RuntimeError):
        tdb.execute_prepared_tql(prepared, {"other": 3})


def test_non_finite_value(tdb):
    """非有限数值必须 fail-closed（0.9.0 起在 JSON 编码阶段即拒）。

    依据 API 参考「Prepared 参数只接受 null/bool/string/number；缺参、额外参数、
    数组/对象和非有限数值明确失败」。0.8.3 实测宽松（inf 不报错），0.9.0 起
    改为抛 ValueError「JSON 浮点数必须有限」——断言「必须失败」而非绑定异常类型。
    """
    prepared = tdb.prepare_tql('FIND {type: "note"} RETURN $bonus AS score')
    with pytest.raises((RuntimeError, ValueError)):
        tdb.execute_prepared_tql(prepared, {"bonus": float("inf")})
