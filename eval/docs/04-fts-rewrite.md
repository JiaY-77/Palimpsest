# 任务：修复 FTS 通道「整句匹配」导致的零召回（实测数据已完备，不用再调研）

## 背景（均已实测，直接采信）

`core/fts_index.py::search_fts` 目前用**整句**做 trigram MATCH（`MATCH '"整句"'`），
而 trigram 子串匹配要求整句连续出现 → 对中文自然语言问句实测 **recall@10 = 0.0000**（129 条评测正样本，一条未中）。

**方案对比实测（同 129 题，recall@10）**：
| 策略 | recall@10 |
|---|---|
| 整句（现状） | 0.0000 |
| 标点切分 OR | 0.0930 |
| 4-gram OR | 0.3721 |
| **3-gram OR（选定）** | **0.5116** |

**端到端实测（把 3-gram OR 接进融合链路）**：
- rrf：R@1 0.1008→**0.2558**，R@5 0.3333→**0.4884**，MRR 0.1977→**0.3570**
- cascade：R@5 0.3333→**0.4109**，MRR 0.1977→**0.3134**
- rrf 首次超过纯向量基线（0.4109），即融合从「负资产」变回「正贡献」

## 改动清单

### 1. `core/fts_index.py`

新增模块级常量与纯函数：

```python
_SPLIT_RE = re.compile(r"[\s,，。；;、/（）()？?！!：:+【】\[\]「」『』\-—·…]+")

def build_fts_query(query: str, n: int = 3, max_grams: int = 12) -> str:
    """把自然语言查询改写为 FTS5 trigram OR 查询（长查询专用）。

    - 按中英文标点/空白切段
    - 每段做 n-gram 滑窗，**均匀采样**（step = max(1, total // max_grams)），
      保证覆盖句尾而不是只取开头
    - 段长 < n 且 < 3 字符的丢弃（trigram 至少 3 字符）
    - 全局片段上限 max_grams（12），用 " OR " 连接
    - 无有效片段返回 ""
    """
```

`search_fts(query, limit=10)` 逻辑改为（**短查询行为必须保持不变**）：
- 空 query → `[]`
- `len(query) <= 8` 或 query 含双引号 → **走现状路径**（整句 `MATCH '"query"'` / `LIKE` 兜底）——短查询（如 `T041`）正是 trigram 的强项，不许动
- `len(query) > 8` 且 `build_fts_query(query)` 非空 → 用改写后的 OR 串 `MATCH`，仍 `ORDER BY rank LIMIT ?`
- 任何异常 → `[]`（保持现状契约）

### 2. `tests/test_fts_query_rewrite.py`（新建）

- `build_fts_query` 纯函数单测：
  - 无标点长句（如「之前压测用的那些脚本都放在哪了」）能产出多个 3 字片段
  - 片段总数 ≤ 12，且**采样覆盖句尾**（最后一段的末尾 3 字应出现在结果中）
  - 带标点/空格的句子按段切分
  - 短串（< 3 字符）返回 `""`
  - 空/None 输入返回 `""`
- 集成测试：用临时 fts 库索引 3~5 条中文节点，验证
  - 长自然语言查询**能命中**（现状会 miss）
  - 短查询（`T041` 之类）仍精确匹配

### 3. `CHANGELOG.md`

在 `## [Unreleased]` 下追加 `### 修复` 条目：FTS 长查询改写（整句 trigram → 3-gram OR），并附一句话实测收益。

## 硬约束

- **不引入任何新依赖**（明确不要 jieba）
- 不改 `index_node` / `remove_node` / `sync_node` / `rebuild` 的行为
- 不动 `eval/` 目录
- Python 一律 `venv/Scripts/python.exe`

## 回归要求（贴实测命令与输出，不要写「逻辑上正确」）

1. `venv/Scripts/python.exe -m py_compile core/fts_index.py tests/test_fts_query_rewrite.py`
2. `venv/Scripts/python.exe -m pytest tests/test_fts_query_rewrite.py -q`
3. 全量 `venv/Scripts/python.exe -m pytest -q`（不得有 failure/error；当前基线 253 passed）
4. 短查询路径不变：贴出你验证 `search_fts("T041")` / 短查询仍走整句路径的证据
5. `sha256sum data/mh_memory.db` 前后一致
6. **端到端指标**：跑 `venv/Scripts/python.exe eval/run_eval.py`（149 题，约 1 分钟），
   贴出报告里 `rrf` 与 `cascade` 的 `recall@5` —— **目标 rrf R@5 ≥ 0.45**（当前 0.3333）
