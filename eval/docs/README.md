# 检索与排序实验记录（ADR 索引）

这里存放 Palimpsest 检索质量迭代过程中的**实验结论**，一篇对应一个可复现的技术判断。写法是「问题 → 判据 → 实测数字 → 结论」，每条结论都能由文中命令复现。

**为什么保留「否证」的篇目**：知道哪些方向试过、为什么不行，比只留下成功案例更有价值——它防止后来的人重复投入同一条死路。标记为「否证」的路线已关闭，不要再开。

> 入口文档：[`docs/RETRIEVAL_QUALITY.md`](../../docs/RETRIEVAL_QUALITY.md)（当前检索质量的完整说明与最新基线）。
> 评测代码与题集：[`eval/README.md`](../README.md)。

## 索引

| # | 主题 | 结论 | 落地 |
|---|---|---|---|
| 01 | [建 `eval/` 离线评测模块](01-design.md) | 采纳：搭建题集生成 + 四模式对比 + 指标纯函数 | `eval/` |
| 02 | [eval 模块验收返工](02-fix-round1.md) | 采纳：修正首轮验收问题 | `eval/` |
| 03 | [移除 L1 嗅探通道](03-remove-L1.md) | 采纳：判据「能优化则删，有作用则留」→ 移除 | 检索链路瘦身 |
| 04 | [修复 FTS 整句匹配零召回](04-fts-rewrite.md) | 采纳：长短语整串 MATCH 几乎 0 命中，改为词项拆分 | `core/fts_index.py` |
| 05 | [修复元数据硬加权压过语义](05-ranking-reweight.md) | 采纳：乘性加权跨度 > 3 倍而余弦分差通常 < 0.1 | `core/trivium_store.py` |
| 06 | [语义主序与图扩散解耦](06-expand-depth.md) | 采纳：图扩散拖累检索，生产默认 `expand_depth=0` | `RETRIEVAL_EXPAND_DEPTH` |
| 07 | [embedding 模型 A/B 复核](07-embedding-ab-v2.md) | bge-m3 优于 qwen3-embedding:0.6b（R@5 +3.4pp） | 默认 embedding |
| 08 | [分块重打分预演](08-chunk-ranking-preview.md) | **否证** | — |
| 09 | [长度归一化与分层分块](09-chunk-norm-variants.md) | **否证** | — |
| 10 | [跨域分数校准](10-chunk-calibration.md) | 首个通过判据，但留有隐忧 | 见 #11 复测 |
| 11 | [分数校准三条细化路线](11-score-calibration-round3.md) | 无增益：`calib_s80` 与 base 持平 | — |
| 12 | [索引期 small-to-big](12-chunk-index-small-to-big.md) | **否证**：召回非瓶颈、排序纯损失（−3.39pp R@5）；分块整条路线四轮关闭 | — |
| 13 | [查询改写 / 扩展](13-query-expansion.md) | **否证**：本地生成模型改写在本题集无净收益 | — |
| 14 | [查询侧域路由](14-domain-routing.md) | 收益真实，但域不可自动可靠推断 | 仅显式传 `domain` |
| 15 | [检索默认 `tier` 放宽 A/B](15-tier-default-ab.md) | 采纳：`facts` → `""`（不过滤），调研历史落在 `record`/`thought` 层 | 2.21.0 行为变更 |

## 约定

- 新开一篇用两位数序号，文件名 `NN-短主题.md`（短横线分隔的小写英文）。
- 结论段必须先写「采纳 / 否证 / 有条件采纳」，再写判据与数字。
- 否证的路线在本文表格里标 **否证**，并在对应篇首行注明「整条路线关闭」。
