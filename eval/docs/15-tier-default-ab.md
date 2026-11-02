# 15 · 检索默认 tier 放宽 A/B（#266）

> 2.21.0 的行为变更依据。同库同题集双口径 A/B，全部数字可由文中命令逐位复现。

## 结论

**检索默认 `tier` 从 `facts` 放宽为 `""`（不过滤）。** `tier=facts` 会把日志层（`record` / `event` / `git_commit` / `thought` / `session`）整体排除在候选之外——「上次压测结果怎么样」这类问题的答案天然落在 `record` / `thought` 层，默认口径下检索服务看不到自己的历史。回退开关：环境变量 `DEFAULT_TIER=facts`，重启即恢复 2.20 行为。

## 双口径数字（62 条判卷题，题级 hit，gold 命中任一即命中）

| 口径 | hit@5 | hit@10 |
|---|---|---|
| `tier=""`（2.21 起默认，不过滤） | **0.8871** | **0.9032** |
| `tier=facts`（2.20 旧默认，回退口径） | 0.6129 | 0.6452 |

**放宽的净收益：hit@5 +0.274 / hit@10 +0.258。** `""` 口径与 issue #266 的表（0.887 / 0.903）逐位一致；`facts` 回退口径与 issue 的 0.613 / 0.645 逐位一致。

## 逐题归因：gold 被 tier 过滤直接吃掉的题

判据：gold 在 `""` 口径候选中可达、在 `facts` 口径中不可达（不看排名，纯可达性）。

- **15 / 62（24.2%）**，题号：`q002 q011 q023 q026 q029 q035 q043 q044 q047 q069 q083 q091 q093 q104 q111`
- gold_type 分布：`record` 5 · `git_commit` 5 · `event` 3 · `kb_chunk` 1 · `memory` 1
- issue #266 记 17/62（27%）：多出的 2 条为 `thought` 类 gold，在 2026-10-30 的 reresolve（人工重指认）中已改指新 gold，本口径下不再计入；`record` 5/`git_commit` 5/`event` 3 与 issue 逐案完全一致。

注：多 gold 题 `q001` 的旧案例（`thought` gold 排第 2 被过滤，只剩排 122 的 `kb_chunk`）在当前题集版本中 gold 已重指为 `[118, 3659]`，不再是本口径下的过滤受害者——但机制相同：`_mem_search_impl` 先取候选、后置 `_tier_matches` 硬过滤，日志层答案在过滤阶段整体不可达。

## 回退验证（三条路径等价）

| 验证方式 | 命令 | hit@5 / hit@10 |
|---|---|---|
| 显式传参 | `eval/run_eval.py --modes vec --top-k 200 --tier facts` | 0.6129 / 0.6452 |
| **env 一键回退** | `DEFAULT_TIER=facts eval/run_eval.py --modes vec --top-k 200`（不传 `--tier`） | **0.6129 / 0.6452（逐位一致）** |
| 历史锚点 | issue #266 的 facts 列 | 0.613 / 0.645 |

env 路径实测：`DEFAULT_TIER=facts python -c "import config; print(config.Config.DEFAULT_TIER)"` → `'facts'`。

## 复现命令与库完整性

```bash
# 新默认口径（不传 tier，服务端默认 "" 生效）
./venv/Scripts/python.exe eval/run_baseline.py --modes vec
./venv/Scripts/python.exe eval/run_eval.py --modes vec --top-k 200 --eval-set eval/eval_set_v2.json
# 回退口径
DEFAULT_TIER=facts ./venv/Scripts/python.exe eval/run_eval.py --modes vec --top-k 200 --eval-set eval/eval_set_v2.json
```

- 题集：`eval/eval_set_v2.json`（148 条，其中判卷正样本 62）；库副本自动创建于 `eval/.tmp/`
- 每次 run_eval 报告的库副本 SHA256 **跑前 = 跑后**（`5f14af89…`，一致性 ✅ PASS）——评测只读，不碰真库
- `--top-k 200` 是 issue #266 表的口径（窗口开满再判卷）；标准口径 `run_baseline.py`（top-10）下双数字为 0.8468/0.8629（`""`）vs 0.5806/0.5968（`facts`），报告的 recall@k 为 (题, gold) 对级（分母 124），与本文题级口径（分母 62）不同
