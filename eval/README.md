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

## 题集版本

- **当前基准**：`fusion_set_v1.json`，由 `gen_fusion_set.py` 产出，供 `fusion_ab.py` 使用。gold 口径 = FTS 字面枚举 ∩ 当前可见（`status != outdated`）。
- **`eval_set.json`（138 题，已失效）**：由 `gen_eval_set.py` 生成的历史题集。⚠️ **2026-10-22 实测其 gold 已整体不可信**——抽查 5 条，1 条的 gold 节点已不存在，存活的也语义错配（问「知识库版本号」，gold 是「压测方法学复证」片段；问「RAG 是什么」，gold 是「各阶段详细说明」）。推测是库重建后 id 漂移。**不要再用它作评测基准**，用它跑出的历史数字都应视为不可信。
- **历史题集**：`eval_set_v1_149.json`（149 题版本）与 `manual_set.json`（手工题）保存在本地评测数据目录，同样不入库；前者是历史版本、后者是手工补充集。
- 新增评测一律以 `fusion_set_v1.json` 这套生成流程为准。

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

## 融合权重 A/B 评测（`fusion_ab.py`）

`run_eval.py` 比的是**检索模式**（fts / vec / rrf / cascade）。如果要调的是 **RRF 内部的融合权重**，就不能每个方案各走一遍线上检索——语义 embedding、FTS 降级、库的瞬时状态都会引入噪声，A/B 的差值就不再只归因于融合逻辑。

`fusion_ab.py` 的做法：**每题只采集一次两路原始排名**（语义 top-N 带分数、FTS top-N 带 bm25 rank），多套融合方案在内存里对同一份排名重排，指标在重排后的顺序上计算。

```bash
# 1) 生成题集（gold 口径 = FTS 字面枚举 ∩ 当前可见）
venv/Scripts/python.exe eval/gen_fusion_set.py --out eval/fusion_set_v1.json

# 2) 跑 A/B（默认 5 套方案：基线 / 分档 / 归一化 / 纯候选补偿 / 分档+补偿）
venv/Scripts/python.exe eval/fusion_ab.py --eval-set eval/fusion_set_v1.json

# 3) 检查精确标识符判定的误判面
venv/Scripts/python.exe eval/probe_exact_misjudge.py
```

题集分四类，两类要点：

- **`exact` / `mixed` 共用同一 gold 集**（mixed 只是 query 里多了自然语言）——这样「加自然语言会不会把噪声带进语义通道」是单变量对照。
- **`mixed` 的 gold 由 FTS 字面枚举得到**，不用语义检索反查（否则就是「用检索结果证明检索结果」）。

题集的**人工部分不入库**（含私有记忆内容与具体 node id），放在 `eval/fusion_set_manual.json`（已被 `.gitignore` 忽略）；仓库里只保留脱敏模板 `eval/fusion_set_manual.example.json`。换库后照模板重抽节点、重写 query 即可。

> ⚠️ 主判据用 `hit@k`（前 k 条能否命中至少一条 gold），不要只看 `recall@k`：标识符类的 gold 是「库里所有提到该标识符的可见节点」，规模差异很大（2 条 ~ 60 条）。gold 36 条时 top-5 最多覆盖 5/36≈0.14，这个上限由 gold 规模决定，不是检索质量。

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
├── fusion_ab.py         # 融合权重 A/B：一次采集、多方案内存重排
├── gen_fusion_set.py    # 生成融合评测题集（gold = FTS 字面枚举 ∩ 可见）
├── probe_exact_misjudge.py  # 精确标识符判定的误判探针
├── fusion_set_manual.example.json  # 题集人工部分的脱敏模板（真实文件不入库）
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
