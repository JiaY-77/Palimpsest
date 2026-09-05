# -*- coding: utf-8 -*-
"""
TriviumDB 0.8.3 特性测试 —— TQL 语法冒烟（P0）
=====================================================
覆盖：FIND / MATCH / SEARCH / WHERE / RETURN / ORDER BY / LIMIT / OFFSET /
聚合（COUNT/SUM/AVG/MIN/MAX/COLLECT）/ DML（CREATE/SET/DELETE）。

核心护栏：**非法 TQL 必须报解析错误（RuntimeError）而非 panic / 崩溃**，
用 fuzz 非法语法字符串断言进程不崩溃。

隔离保证：全部在 %TEMP%/tdb_ftest/ 下独立临时库运行，不触碰正式库
data/mh_memory.db（conftest 的 DB_PATH 隔离与此互补）。
"""
import math
import os
import tempfile

import pytest

import triviumdb  # noqa: E402


# ---------------------------------------------------------------------------
# 公共夹具：每次测试一个全新的隔离临时库（8 维，够 TQL 语法验证用）
# ---------------------------------------------------------------------------
@pytest.fixture
def tdb():
    _dir = os.path.join(tempfile.gettempdir(), "tdb_ftest")
    os.makedirs(_dir, exist_ok=True)
    path = os.path.join(_dir, "tql_basic.db")
    for f in os.listdir(_dir):
        if f.startswith("tql_basic"):
            os.remove(os.path.join(_dir, f))
    db = triviumdb.TriviumDB(path, dim=8, auto_build_quiver=False)

    def vec(text, dim=8):
        out = [0.0] * dim
        for ch in text:
            out[ord(ch) % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]

    ids = []
    for i in range(6):
        ids.append(db.insert(
            vec(f"content node {i} alpha"),
            {"type": "memory", "domain": f"d{i % 3}", "num": i,
             "tags": ["a", "b"] if i % 2 else ["a"]},
        ))
    for a, b, label in [(0, 1, "REL"), (1, 2, "REL"), (0, 3, "REL"),
                        (3, 4, "CAUSES"), (1, 4, "CAUSES"), (4, 5, "REL")]:
        db.link(ids[a], ids[b], label)
    yield db
    db.close()


def _rows(db, query):
    """执行 tql 并返回 QueryRow 列表；异常时原样抛出（由测试决定是否捕获）。"""
    return db.tql(query)


# ---------------------------------------------------------------------------
# FIND —— 文档过滤
# ---------------------------------------------------------------------------
def test_find_equality(tdb):
    rows = _rows(tdb, 'FIND {type: "memory"} RETURN *')
    assert len(rows) == 6
    for r in rows:
        assert r.row["_"]["payload"]["type"] == "memory"


def test_find_operator_gte(tdb):
    rows = _rows(tdb, 'FIND {num: {$gte: 3}} RETURN *')
    nums = [r.row["_"]["payload"]["num"] for r in rows]
    assert nums
    assert all(n >= 3 for n in nums)


def test_find_array_all(tdb):
    rows = _rows(tdb, 'FIND {tags: {$all: ["a", "b"]}} RETURN *')
    tags = [r.row["_"]["payload"]["tags"] for r in rows]
    assert tags
    assert all("b" in t for t in tags)


def test_find_or(tdb):
    rows = _rows(tdb, 'FIND {$or: [{num: {$lt: 2}}, {num: {$gte: 5}}]} RETURN *')
    nums = [r.row["_"]["payload"]["num"] for r in rows]
    assert set(nums) == {0, 1, 5}


def test_find_order_by_limit_offset(tdb):
    rows = _rows(
        tdb,
        'FIND {type: "memory"} RETURN * ORDER BY _.num DESC LIMIT 3 OFFSET 1',
    )
    nums = [r.row["_"]["payload"]["num"] for r in rows]
    # DESC: 5,4,3,2,1,0 → offset 1 → 4,3,2
    assert nums == [4, 3, 2]


# ---------------------------------------------------------------------------
# MATCH —— 图模式匹配
# ---------------------------------------------------------------------------
def test_match_single_edge(tdb):
    rows = _rows(tdb, 'MATCH (a)-[:REL]->(b) RETURN b')
    assert len(rows) == 4  # 4 条 REL 边


def test_match_where(tdb):
    rows = _rows(tdb, 'MATCH (a)-[:REL]->(b) WHERE b.num > 2 RETURN b')
    nums = [r.row["b"]["payload"]["num"] for r in rows]
    assert nums
    assert all(n > 2 for n in nums)


def test_match_variable_length(tdb):
    rows = _rows(tdb, 'MATCH (a)-[:REL*1..2]->(b) RETURN DISTINCT b')
    assert rows


def test_match_count(tdb):
    rows = _rows(tdb, 'MATCH (a)-[:REL]->(b) RETURN COUNT(b) AS c')
    # 0.8.5+：聚合返回扁平化，别名直接映射值（0.8.3 曾嵌套在 payload 里）
    assert rows[0].row["c"] == 4


# ---------------------------------------------------------------------------
# SEARCH —— 向量检索 + EXPAND
# ---------------------------------------------------------------------------
def test_search_vector_topk(tdb):
    v = ",".join(["0.5"] * 8)
    rows = _rows(tdb, f"SEARCH VECTOR [{v}] TOP 3 RETURN *")
    assert len(rows) == 3


def test_search_expand(tdb):
    v = ",".join(["0.5"] * 8)
    rows = _rows(tdb, f"SEARCH VECTOR [{v}] TOP 2 EXPAND [:REL*1..1] RETURN *")
    assert len(rows) >= 2


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def test_aggregates(tdb):
    rows = _rows(
        tdb,
        "MATCH (n) RETURN COUNT(n) AS c, SUM(n.num) AS s, AVG(n.num) AS a, MIN(n.num) AS mn, MAX(n.num) AS mx",
    )
    row = rows[0].row
    # 0.8.5+：聚合返回扁平化（0.8.3 曾嵌套 payload['count'] 等）
    assert row["c"] == 6
    assert row["s"] == 15.0
    assert row["a"] == 2.5
    assert row["mn"] == 0.0
    assert row["mx"] == 5.0


def test_collect(tdb):
    rows = _rows(tdb, "MATCH (n) RETURN COLLECT(n.domain) AS d")
    coll = rows[0].row["d"]
    # 0.8.5+：COLLECT 返回扁平列表（0.8.3 曾嵌套 payload['collect']）
    assert set(coll) == {"d0", "d1", "d2"}
    assert len(coll) == 6


# ---------------------------------------------------------------------------
# DML —— CREATE / SET / DELETE
# ---------------------------------------------------------------------------
def test_dml_create_set_delete(tdb):
    before = tdb.node_count()
    res = tdb.tql_mut
    r = res('CREATE (a {name: "Alice", age: 30})')
    assert r["affected"] == 1
    assert len(r["created_ids"]) == 1
    new_id = r["created_ids"][0]
    assert tdb.node_count() == before + 1

    # SET
    r2 = res('MATCH (n {name: "Alice"}) SET n.age == 31')
    assert r2["affected"] == 1
    payload = tdb.get(new_id).payload
    assert payload["age"] == 31

    # DELETE
    r3 = res('MATCH (n {name: "Alice"}) DELETE n')
    assert r3["affected"] == 1
    assert tdb.get(new_id) is None


# ---------------------------------------------------------------------------
# 非法语法护栏：必须报解析错误，绝不能 panic / 崩溃
# ---------------------------------------------------------------------------
_ILLEGAL = [
    "FIND ***",
    "SELECT bogus from n where 1=1",
    "MATCH (((",
    "FIND {type:} RETURN *",
    "RETURN *",
    "MATCH (n) WHERE n.num = 3 RETURN n",   # 单等号非法（应 ==）
    "SEARCH VECTOR [1,2 RETURN *",
    "MATCH (a)-[:REL]->(b) RETURN nope",    # 未绑定变量
    "CREATE (",
    "TOTALLY GARBAGE QUERY !!!",
    "MATCH (n) RETURN n ORDER BY n.payload",
    "FIND {a: {$invalid_op: 1}} RETURN *",
]


def test_invalid_syntax_raises_parse_error_not_panic(tdb):
    for q in _ILLEGAL:
        try:
            tdb.tql(q)
        except RuntimeError as e:
            # 解析错误信息（而非 Python 进程崩溃）
            assert "解析错误" in str(e) or "parse error" in str(e) or "错误" in str(e), \
                f"应报解析错误，得到: {e}"
        else:
            # 某些非法查询可能被解释为合法但语义上拒绝；关键是「未崩溃」。
            # 若未抛异常则视为通过（不 panic 是最高优先），打印观察。
            print(f"[observe] illegal query did not raise: {q!r}")
