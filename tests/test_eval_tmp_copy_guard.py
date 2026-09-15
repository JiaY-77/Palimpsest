"""eval 副本自拷贝防护回归测试

触发场景（Windows 特有）：``DB_PATH`` 已经指向 ``eval/.tmp`` 里的库副本时，
再次 import run_eval / gen_eval_set 会让 ``_copy_db_to_tmp`` 把文件复制到自身，
抛 ``PermissionError: [WinError 32]``。

根因：同一 pytest 会话里先后 import 两个 eval 模块（各自的 import 期都会调用
_copy_db_to_tmp），第二次的 src 与 dst 是同一个文件。

契约：src 与 dst 指向同一文件时跳过复制，import 幂等、不抛异常。
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


def _reimport(module_name: str):
    """强制重新导入模块，触发其 import 期的 _copy_db_to_tmp。"""
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", ["eval.run_eval", "eval.gen_eval_set"])
def test_second_import_does_not_self_copy(module_name: str, monkeypatch):
    """连续 import 两次不应因 self-copy 抛 PermissionError。"""
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    saved = os.environ.get("DB_PATH")
    try:
        _reimport(module_name)          # 第一次：把 DB_PATH 指到 .tmp 副本
        _reimport(module_name)          # 第二次：DB_PATH 已指向副本，触发 self-copy 场景
    finally:
        if saved is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = saved


def test_is_same_file_helper():
    """_is_same_file 对同一路径返回 True，对不同文件返回 False。"""
    import eval.run_eval as re_mod

    p = Path(__file__).resolve()
    assert re_mod._is_same_file(p, p) is True
    assert re_mod._is_same_file(p, p.parent / "definitely_not_here.xyz") is False
