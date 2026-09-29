"""migrate_domain：迭代器耗尽前不得写库
=========================================

triviumdb 的 ``iter_payloads()`` 在迭代器耗尽前不释放连接；循环体内再调
``update_payload``（内部要开新连接）会报 ``Database locked``——``--apply``
大概率在第一个待迁移节点上就炸，而 dry-run 不触发写路径、掩盖了问题。

这里用一个「能感知自己是否还在被迭代」的假 store 把顺序钉死。
"""

from __future__ import annotations

import os
import sys

# migrate_domain 是「脚本式」导入（内部 `import _common` 依赖 scripts/ 在
# sys.path 上），因此测试也按脚本方式加载它。
_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import migrate_domain as md  # noqa: E402


class _IterationAwareStore:
    """假 store：记录「写是否发生在迭代未耗尽时」。"""

    def __init__(self, nodes):
        self._nodes = list(nodes)
        self.iter_exhausted = False
        self.writes = []
        self.writes_during_iteration = 0
        self.db_path = "test_migrate.db"  # 结果里会回显库路径

    def iter_payloads(self):
        self.iter_exhausted = False
        yield from self._nodes
        self.iter_exhausted = True

    def update_payload(self, node_id, new_payload):
        if not self.iter_exhausted:
            self.writes_during_iteration += 1
        self.writes.append((node_id, dict(new_payload)))


def _patch_store(monkeypatch, fake):
    monkeypatch.setattr(md, "TriviumStore", lambda *a, **k: fake)


def test_apply_consumes_iterator_before_writing(monkeypatch):
    fake = _IterationAwareStore([
        (1, {"character_name": "hero", "content": "甲"}),
        (2, {"character_name": "villain", "content": "乙"}),
        (3, {"domain": "already", "content": "丙"}),
    ])
    _patch_store(monkeypatch, fake)

    result = md.run(apply=True)

    assert fake.writes_during_iteration == 0, (
        f"写库发生在迭代器耗尽前（会 Database locked）："
        f"{fake.writes_during_iteration} 次"
    )
    assert result["migrated"] == 2, result
    assert result["skipped_already_have_domain"] == 1, result
    assert [nid for nid, _p in fake.writes] == [1, 2], fake.writes
    for _nid, payload in fake.writes:
        assert payload["domain"] == payload["character_name"]


def test_dry_run_does_not_write(monkeypatch):
    fake = _IterationAwareStore([
        (1, {"character_name": "hero", "content": "甲"}),
    ])
    _patch_store(monkeypatch, fake)

    result = md.run(apply=False)

    assert fake.writes == [], "dry-run 不得写库"
    assert result["dry_run"] is True
    assert result["migrated"] == 1
    assert result["preview"] == [{"node_id": 1, "character_name": "hero"}]
