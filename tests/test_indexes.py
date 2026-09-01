# -*- coding: utf-8 -*-
"""
TriviumDB 0.8.3 特性测试 —— 持久化属性索引（P1）
======================================================
覆盖：create_index(Hash) / create_ordered_index(Ordered) /
create_composite_index(Composite) / create_bitmap_index(Bitmap) 四类：
  · 正确性（建索引后查询结果与无索引一致）
  · index_info() 反映
  · drop / 重建
  · 字段边界：嵌套字段 / 数组字段 / null 值 / 复合索引字段顺序

隔离保证：%TEMP%/tdb_ftest/ 独立临时库。
"""
import math
import os
import tempfile

import pytest

import triviumdb  # noqa: E402


@pytest.fixture
def tdb():
    _dir = os.path.join(tempfile.gettempdir(), "tdb_ftest")
    os.makedirs(_dir, exist_ok=True)
    path = os.path.join(_dir, "indexes.db")
    for f in os.listdir(_dir):
        if f.startswith("indexes"):
            os.remove(os.path.join(_dir, f))
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)

    def vec(text, dim=8):
        out = [0.0] * dim
        for ch in text:
            out[ord(ch) % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]

    nodes = [
        {"type": "memory", "num": 1, "folder": "work", "tags": ["a", "b"],
         "meta": {"score": 0.9}},
        {"type": "memory", "num": 2, "folder": "work", "tags": ["a"],
         "meta": {"score": 0.5}},
        {"type": "memory", "num": 3, "folder": "home", "tags": ["b", "c"],
         "meta": {"score": 0.7}},
        {"type": "event", "num": 4, "folder": "work", "tags": [],
         "meta": None},
        {"type": "event", "num": 5, "folder": "home", "tags": ["a", "b", "c"],
         "meta": {"score": 0.1}},
    ]
    for p in nodes:
        db.insert(vec(p["folder"] + str(p["num"])), p)
    yield db
    db.close()


def _ids(rows):
    return [r.row["_"]["id"] for r in rows]


def test_no_index_consistency_baseline(tdb):
    """无索引时的 FIND 结果（作为对照基线）。"""
    rows = tdb.tql('FIND {type: "memory"} RETURN *')
    assert len(rows) == 3


def test_index_does_not_change_results(tdb):
    """建索引前后同一 FIND 结果一致。"""
    before = _ids(tdb.tql('FIND {type: "memory"} RETURN *'))
    tdb.create_index("type")
    after = _ids(tdb.tql('FIND {type: "memory"} RETURN *'))
    assert sorted(before) == sorted(after)
    tdb.drop_index("type")
    after_drop = _ids(tdb.tql('FIND {type: "memory"} RETURN *'))
    assert sorted(before) == sorted(after_drop)


def test_hash_ordered_consistent(tdb):
    tdb.create_index("num")
    tdb.create_ordered_index("num")
    h = _ids(tdb.tql('FIND {num: {$gte: 3}} RETURN *'))
    o = _ids(tdb.tql('FIND {num: {$gte: 3}} RETURN *'))
    assert h and sorted(h) == sorted(o)


def test_bitmap_consistent(tdb):
    tdb.create_bitmap_index("type")
    rows = tdb.tql('FIND {type: "event"} RETURN *')
    assert len(rows) == 2
    for r in rows:
        assert r.row["_"]["payload"]["type"] == "event"


def test_composite_consistent(tdb):
    tdb.create_composite_index(["type", "num"])
    rows = tdb.tql('FIND {type: "memory", num: 2} RETURN *')
    assert len(rows) == 1
    assert rows[0].row["_"]["payload"]["num"] == 2


def test_index_info_reflection(tdb):
    tdb.create_index("num")
    tdb.create_ordered_index("folder")
    tdb.create_composite_index(["type", "num"])
    tdb.create_bitmap_index("type")
    info = tdb.index_info()
    by_field = {}
    for e in info:
        by_field.setdefault(e["kind"], []).append(e)
    assert any(k == "hash" for k in by_field), info
    assert any(k == "ordered" for k in by_field), info
    assert any(k == "composite" for k in by_field), info
    assert any(k == "bitmap" for k in by_field), info
    # composite 的 fields 顺序保留
    comp = by_field["composite"][0]
    assert comp["fields"] == ["type", "num"], comp
    assert comp["field"] == "type,num", comp


def test_drop_recreate(tdb):
    tdb.create_index("num")
    assert any(e["kind"] == "hash" for e in tdb.index_info())
    tdb.drop_index("num")
    assert tdb.index_info() == []
    tdb.create_index("num")
    assert any(e["kind"] == "hash" for e in tdb.index_info())


def test_drop_bitmap_composite_ordered(tdb):
    tdb.create_bitmap_index("type")
    tdb.create_composite_index(["type", "num"])
    tdb.create_ordered_index("num")
    assert tdb.index_info()
    tdb.drop_bitmap_index("type")
    tdb.drop_composite_index(["type", "num"])
    tdb.drop_ordered_index("num")
    assert tdb.index_info() == []


# ---------------------------------------------------------------------------
# 字段边界
# ---------------------------------------------------------------------------
def test_nested_field_dotted_query(tdb):
    """嵌套字段点号路径 {“meta.score”: ...} 可解析，但 0.8.3 不作多层下钻
    （返回空）。属顶层字段索引边界——记录该行为，不判定实现错误。"""
    rows = tdb.tql('FIND {"meta.score": {$gte: 0.7}} RETURN *')
    # 无嵌套下钻 → 空；见 test_nested_field_nested_object_not_supported 的解析错误边界
    assert rows == []


def test_nested_field_nested_object_not_supported(tdb):
    """{meta: {score: ...}} 嵌套对象简写不被 Finder 支持 → 应报解析错误（边界）。"""
    with pytest.raises(RuntimeError):
        tdb.tql('FIND {meta: {score: {$gte: 0.7}}} RETURN *')


def test_array_field_all(tdb):
    rows = tdb.tql('FIND {tags: {$all: ["a", "b"]}} RETURN *')
    names = {r.row["_"]["payload"]["num"] for r in rows}
    assert names == {1, 5}


def test_null_field_exists_true_includes_null(tdb):
    """$exists:true 会把「存在但值为 null」的字段也算作存在（0.8.3 语义）。"""
    rows = tdb.tql('FIND {meta: {$exists: true}} RETURN *')
    a_meta = [r.row["_"]["payload"].get("meta") for r in rows]
    # 所有 5 个节点都有 meta 键（含 None）→ 全部命中
    assert len(rows) == 5
    # null 值节点（num=4）被 $exists:true 命中就是此语义的体现
    assert None in a_meta


def test_field_not_exists(tdb):
    """没有某字段的节点用 $exists:false 过滤。所有节点都带 folder，应命中 0。"""
    rows = tdb.tql('FIND {folder: {$exists: false}} RETURN *')
    assert rows == []


def test_composite_equality_consistent(tdb):
    """复合索引 [type, num] 的双等值查询与无索引一致。"""
    before = _ids(tdb.tql('FIND {type: "memory", num: 2} RETURN *'))
    tdb.create_composite_index(["type", "num"])
    after = _ids(tdb.tql('FIND {type: "memory", num: 2} RETURN *'))
    assert before == after == [2]


@pytest.mark.xfail(
    reason="复合索引 [type, num] 对辅助字段 num 的范围查询（$lt/$gte）在 0.8.3 "
           "返回空集，而无索引时正常 → 疑似 composite 索引范围下推 bug，需上报作者",
    strict=False,
)
def test_composite_secondary_range_bug(tdb):
    """复合索引对次要字段做范围过滤：无索引[1,2]，建复合索引后应为[1,2]。"""
    tdb.create_composite_index(["type", "num"])
    rows = tdb.tql('FIND {type: "memory", num: {$lt: 3}} RETURN *')
    assert {r.row["_"]["payload"]["num"] for r in rows} == {1, 2}

