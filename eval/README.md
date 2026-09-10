# 检索质量离线评测模块 (eval/)

本模块对 Palimpsest 的四条检索路径进行离线质量评测，使用库内真实节点反推查询，对比不同检索模式的召回率、排名质量和融合策略效果。

## 快速开始

```bash
# 前提：已激活 venv，且设置了 DEEPSEEK_API_KEY 环境变量
cd Palimpsest

# 1. 只抽样查看分层分布（不调 API）
venv/Scripts/python.exe eval/gen_eval_set.py --dry-run

# 2. 生成评测题集（需要 DeepSeek API）
venv/Scripts/python.exe eval/gen_eval_set.py

# 3. 运行评测（4 种模式，默认 top-10）
venv/Scripts/python.exe eval/run_eval.py

# 4. 运行指定模式
venv/Scripts/python.exe eval/run_eval.py --modes rrf,cascade

# 5. 只评测前 20 题
venv/Scripts/python.exe eval/run_eval.py --limit 20

# 6. 用自定义题集评测
venv/Scripts/python.exe eval/run_eval.py --eval-set eval/.tmp/mini_set.json --limit 3
```

## 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 是（gen 阶段） | DeepSeek API 密钥，用于生成查询 |
| `DEEPSEEK_BASE_URL` | 否 | 默认 `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 否 | 默认 `deepseek-v4-flash` |
| `DB_PATH` | 否 | 覆盖默认库路径（默认 `data/mh_memory.db`） |

## 题集 Schema (`eval/eval_set.json`)

```json
{
  "version": 1,
  "created_at": "2026-09-10T12:00:00+00:00",
  "seed": 20260910,
  "items": [
    {
      "qid": "q001",
      "query": "关于某个话题的查询",
      "kind": "semantic|entity|negative",
      "gold_ids": [123],
      "gold_type": "memory",
      "gold_domain": "hermes",
      "layer": "hermes|kb|novel|other"
    }
  ]
}
```

**字段说明：**
- `kind`：`semantic`（口语化改写）/ `entity`（含术语）/ `negative`（不存在的主题）
- `gold_ids`：该查询对应的正确节点 ID 列表
- `gold_type`：节点类型（memory / kb_chunk / novel_chunk 等）
- `gold_domain`：节点域（hermes / kb / novel 等）
- `layer`：分层标签（`hermes` / `kb` / `novel` / `other`），用于分层报告分组

## 结果 Schema (`eval/results_*.json`)

```json
{
  "q001": {
    "fts": {"ids": [123, 456], "scores": [null, null]},
    "vec": {"ids": [123, 789], "scores": [0.92, 0.85]},
    "rrf": {"ids": [123, 456], "scores": [0.032, 0.018]},
    "cascade": {"ids": [123, 456], "scores": [0.91, 0.84]}
  }
}
```

每个模式包含 `ids`（排序后的节点 ID）和 `scores`（对应分数，fts 为 null）。

## 指标定义

| 指标 | 定义 |
|---|---|
| Recall@K | 前 K 个结果中命中 gold 集合的比例 |
| MRR@K | 第一个命中结果的倒数排名，无命中为 0 |
| nDCG@K | 标准归一化折损累积增益，gold=1.0，partial=0.5 |
| Recall@5(doc) | kb_chunk 题专用：同一 source_path 的其他 chunk 也视为相关 |

## 检索模式

| 模式 | 说明 |
|---|---|
| `fts` | 纯 FTS5 全文检索（trigram tokenizer） |
| `vec` | 纯语义向量检索（SA-PPR，无图扩散 expand_depth=0 + 时间衰减） |
| `rrf` | RRF 融合（语义排名 + FTS 排名的 reciprocal rank 求和，k=60） |
| `cascade` | 级联策略（FTS 粗筛 → 语义精排） |

## 库副本保护机制

为避免评测过程对真实数据库产生写入（如 `hit_count` 递增），所有脚本在启动时会：

1. 将 `data/mh_memory.db` 及所有 sidecar 文件（`.vec`, `.pld.*`, `.gidx`, `.pidx`, `.wal`）复制到 `eval/.tmp/`
2. 将 `data/fts.db` 复制到 `eval/.tmp/`
3. 设置 `DB_PATH` 环境变量指向副本
4. 后续所有 import 和初始化都使用副本

**完整性自证：** 跑前跑后对真实库文件计算 SHA256，写入评测报告，一致则标记 ✅。

## 文件结构

```
eval/
├── metrics.py           # 纯函数指标（recall, MRR, nDCG），不联网不连库
├── gen_eval_set.py      # 生成评测题集（抽样 + DeepSeek API）
├── run_eval.py          # 运行评测，输出结果 JSON + 报告 Markdown
├── README.md            # 本文件
├── eval_set.json        # 生成的评测题集（由 gen_eval_set.py 产出）
├── results_*.json       # 逐题原始排名（由 run_eval.py 产出）
├── report_*.md          # 模式对比报告（由 run_eval.py 产出）
└── .tmp/                # 库副本（自动创建，.gitignore）
```

## 单元测试

```bash
# 测试指标函数（不联网不连库）
venv/Scripts/python.exe -m pytest tests/test_eval_metrics.py -v

# 全量测试（确认未破坏现有测试）
venv/Scripts/python.exe -m pytest -q
```
