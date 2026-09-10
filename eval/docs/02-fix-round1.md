# 返工单 1：eval 模块验收发现的问题（逐条修，不要漏）

只允许修改：`eval/gen_eval_set.py`、`eval/run_eval.py`、`eval/README.md`、`tests/test_eval_metrics.py`（如需）。
不要动其他任何文件。修改前先读现状，改动要最小化。

## 问题 1（严重）纯向量基线被图扩散污染

`run_eval.py` 的 `_run_vec` 调用 `store.search_similar(..., expand_depth=1, ...)`，而规格要求「不启用图扩散」。
`search_similar` 的 `expand_depth` 默认就是 1，必须显式传 `expand_depth=0`。

理由：vec 模式是干净基线。带着图扩散的话，rrf/cascade 与它的对比里混进了图谱成分，就无法判断「融合有没有增益」。

## 问题 2（严重）kb 文档级 recall 未实现

`_doc_recall` 已定义但从未被调用，规格要求的 `Recall@5(doc)` 在报告里完全缺失。要实现：

- 对 `gold_type == "kb_chunk"` 的题额外算 **doc-level** 指标：
  - `doc_recall@5`：top-5 里是否命中**同一 `source_path` 的任一节点**（二值 0/1，不是比例——比例会让多切片文档永远低分）
  - `ndcg@5` 计算时把「同一 source_path 的其他 chunk」作为 `partial` 传入（相关性 0.5），复用 `ndcg_at_k(..., partial=...)`
- 报告新增一张表（或一列）：kb_chunk 题的 `Recall@5(doc)` + `nDCG@5(partial)`
- source_path 用已有的 `_collect_source_paths` 取值

## 问题 3（严重）分层细分表分组错位

报告按 `item["gold_domain"]` 分组，但 kb_chunk 节点的 payload `domain` 实际是 `"kb"` / `"rule"`，
而分组表键是 `["hermes","kb_chunk","novel","other"]` → kb 题全掉进 `other`，分层表失真。

修法：
- `gen_eval_set.py` 每题写入 `layer` 字段（取值 `hermes` / `kb` / `novel` / `other`，沿用抽样分层逻辑）
- `run_eval.py` 报告按 `layer` 分组；题集里没有 `layer` 字段时回退用 `gold_type`/`gold_domain` 推断（兼容旧题集）

## 问题 4（中）--resume 会重复追加负样本

负样本是无条件 append。改为：resume 时若已有 `kind == "negative"` 的题（按 query 文本去重）则跳过，只补足到 20 条。

## 问题 5（中）重复检索取分数，浪费约 2x 时间

主循环里为取 score 把 vec/rrf/cascade 的检索又跑了一遍。改为：模式函数一次返回 `(ids, scores)`，
主循环直接用；结果 JSON 结构改为每题 `{mode: {"ids": [...], "scores": [...]}}`，报告读取同步调整（README 更新 schema）。

## 问题 6（小）fts 在负样本分析里显示 N/A

fts 无分数。负样本分析表的 fts 列改为输出「负样本中 fts 有返回结果的比例」（并在表头/脚注注明含义），不要 N/A。

## 问题 7（小）新增 `--eval-set PATH` 参数

`run_eval.py` 支持 `--eval-set` 指定题集路径（默认 `eval/eval_set.json`），便于用最小题集做验收。

## 回归要求（全部重跑并贴实测输出，不要贴"逻辑正确"）

1. `venv/Scripts/python.exe -m py_compile eval/metrics.py eval/gen_eval_set.py eval/run_eval.py`
2. `venv/Scripts/python.exe -m pytest tests/test_eval_metrics.py -q`
3. `venv/Scripts/python.exe -m pytest -q`（不得少于 215 passed）
4. `venv/Scripts/python.exe eval/gen_eval_set.py --dry-run`（打印里应含 layer 分布）
5. `sha256sum data/mh_memory.db` 前后一致
6. **自建 3 题最小题集**（1 条 kb_chunk 正样本 + 1 条普通正样本 + 1 条 negative，你自己从库里挑，写进 `eval/.tmp/mini_set.json`），
   跑 `venv/Scripts/python.exe eval/run_eval.py --eval-set eval/.tmp/mini_set.json --limit 3`，确认：
   - 报告出现 kb 的 `Recall@5(doc)` 行
   - vec 模式确实 `expand_depth=0`
   - 真库 sha256 前后一致
   - 报告能正常生成
   （注意：验证完把 mini_set.json 保留在 eval/.tmp/ 即可，不要提交）
