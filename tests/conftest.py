# -*- coding: utf-8 -*-
"""
pytest 公共夹具 —— 冒烟测试隔离
==============================
核心原则：所有测试一律使用【临时数据库】，绝不触碰正式库 data/mh_memory.db。

实现要点：
  1. 在 import 任何本项目模块（config / mcp_tools / core）【之前】，先把
     DB_PATH 环境变量指向临时目录下的 mh_test.db。config.py 的 load_dotenv()
     默认不覆盖已存在的环境变量，因此临时路径优先于 .env 里的正式库路径。
  2. FTS5 全文索引随 DB_PATH 的 dirname 落在同一临时目录（fts.db），随临时库
     一起被隔离与清理。
  3. 测试结束（session 结束）后递归删除临时目录。
  4. 知识库（kb_chunk / kb_search）测试不在此做——不依赖真实 Knowledge 目录，
     冒烟只覆盖记忆主链路。
"""

import os
import shutil
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 临时库路径（必须在任何本项目 import 前设定）
_TMP_DIR = tempfile.mkdtemp(prefix="palimpsest_test_")
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "mh_test.db")
# 屏蔽知识库根，避免冒烟测试意外触碰真实知识目录
os.environ.setdefault("KNOWLEDGE_DIR", os.path.join(_TMP_DIR, "knowledge"))
os.environ.setdefault("HERMES_MEMORY_FILE", "")

import hashlib

import pytest  # noqa: E402

# 在 DB_PATH 就绪后导入全局 store（mcp_tools 内部据此实例化 TriviumStore）
from mcp_tools import store  # noqa: E402  # noqa: F401

# ---------------------------------------------------------------------------
# 确定性 fake embedder —— 测试全部走此实现（不依赖 Ollama）
# ---------------------------------------------------------------------------

_FAKE_DIM = 1024


def _fake_embed(text: str) -> list[float]:
    """基于字符 2-gram 的确定性向量：相似文本 → 相似向量。"""
    import math

    vec = [0.0] * _FAKE_DIM
    if not text:
        return vec
    padded = " " + text + " "
    for i in range(len(padded) - 1):
        gram = padded[i : i + 2]
        h = int(hashlib.md5(gram.encode("utf-8")).hexdigest()[:4], 16)
        vec[h % _FAKE_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


@pytest.fixture(scope="session", autouse=True)
def fake_embedder():
    """替换全局 store.embed_text 为确定性 fake embedding（n-gram 向量）。

    测试全部走 fake embedding（确定性 n-gram 向量），真实 embedding 由
    startup-check / 生产环境验证。session 级别不恢复。
    """
    store.embed_text = _fake_embed
    yield


@pytest.fixture(scope="session", autouse=True)
def smoke_env():
    """占位夹具：确保临时环境已建立；session 结束统一清理临时目录。"""
    yield
    # triviumdb 连接在 with 块退出/显式 close 后已释放；失败忽略（Windows 文件锁偶发）
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


@pytest.fixture
def db_path():
    """暴露当前测试库路径，便于断言用临时库而非正式库。"""
    return os.environ["DB_PATH"]
