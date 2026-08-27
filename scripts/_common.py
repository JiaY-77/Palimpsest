# -*- coding: utf-8 -*-
"""scripts 公共工具（P0 重构 2026-08-27）

统一「项目根路径注入」样板：各脚本 import 本项目 core/config 前，
先 from _common import ... 即可，不再各自手写 sys.path 注入。

用法：
    from _common import PROJECT_ROOT, SCRIPT_DIR
"""
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
