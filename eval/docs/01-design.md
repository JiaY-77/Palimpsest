# 任务：为 Palimpsest 新建「检索质量离线评测」模块（eval/）

只做这一件事：在仓库根新建 `eval/` 目录，写 4 个代码文件 + 1 个测试文件。
不要修改 `eval/` 和 `tests/test_eval_metrics.py` 之外的任何文件。不要访问项目外目录。

## 背景

Palimpsest 是记忆 + 知识库的检索服务（Windows，项目即本目录）。检索有四条路径：

| 模式 | 入口 | 返回 |
|---|---|---|
| 纯 FTS5 | `core/fts_index.py::search_fts(query, limit=10)` | `[{"node_id": int, "content": str}]` |
| 纯语义 | `core/trivium_store.py::TriviumStore().search_similar(...)` | 见该文件实际签名（top_k 参数控制条数） |
| RRF 融合 | `mcp_tools/memory.py::_hybrid_rrf(query, scope, domain, domain_bias, top_k, fts_limit, block)` | `list[dict]`，每项含 `id` |
| 级联 | `mcp_tools/memory.py::_hybrid_cascade(同上签名)` | 同上 |

注意：`_hybrid_rrf` / `_hybrid_cascade` 内部会调 `_mem_search_impl`，那个会往库里 bump `hit_count`。所以评测**必须在库的副本上跑**。

## 硬约束（违反即返工）

1. **绝不修改真实库**：脚本启动时把库文件复制到 `eval/.tmp/` 下，把 `DB_PATH` 环境变量指向副本后，再 import / 初始化 `TriviumStore`。需要复制的东西：
   - `Config.DB_PATH` 指向的库文件（本地为 `data/mh_memory.db`）
   - 同目录下的 `.pld` sidecar 文件（若存在，triviumdb 0.8.6+ 的 payload sidecar）
   - FTS 索引库：由 `core/fts_index.py` 的库路径函数决定（自己读该文件确认函数名），同样复制到副本目录并让 `core/fts_index` 用到副本（通过它读取的环境变量/配置，自己确认）
2. **只读自证**：跑前跑后对真实库文件算 sha256，写进报告（一致 = ✅）。
3. **不硬编码密钥**：`DEEPSEEK_API_KEY` 从环境变量读（`Config.DEEPSEEK_API_KEY` 已封装）。任何日志/文件/异常信息里不得出现 key 内容。
4. Python 一律 3.11，Windows 路径用 `pathlib`。脚本顶部把项目根加进 `sys.path`：
   `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`
5. 不许吞异常假成功：失败要计数、要打印、要反映到退出码。

## 文件 1：`eval/metrics.py`（纯函数，不联网不连库）

- `recall_at_k(ranked_ids: list[int], gold: set[int], k: int) -> float`
- `mrr_at_k(ranked_ids, gold, k) -> float`（第一个命中的倒数排名，无命中 0.0）
- `ndcg_at_k(ranked_ids, gold, k, partial: set[int] | None = None) -> float`
  - 相关性：gold 命中 = 1.0；`partial`（同文档其他切片）命中 = 0.5；其余 0
  - 二值口径下（partial=None）就是标准 nDCG
- 全部对空输入安全（gold 为空、结果为空 → 返回 0.0，不抛异常）

## 文件 2：`eval/gen_eval_set.py`（命令行，argparse）

从库内真实节点反推评测题。

**抽样**（`--seed` 默认 `20260910`，用 `random.Random(seed)` 保证可重现）：
遍历全库 active 节点（`status != "outdated"`），分层随机抽 120 个正样本 target：
- hermes 域的 `memory` / `record` / `correction` / `plan` / `decision` 类：40
- `kb_chunk`：40
- novel 域（`novel_chunk` / `character_state` / `plot_plan`）：20
- `task` / `rule` / 其他：20
某层实际数量不足时从其他层补足，并在统计里注明实际分布。

**出题**：每 5 个节点打包成一次 DeepSeek chat completions 调用（`Config.DEEPSEEK_BASE_URL` + `/chat/completions`，model 用 `Config.DEEPSEEK_MODEL`，key 用 `Config.DEEPSEEK_API_KEY`，用标准库 `urllib.request` 或项目已有依赖，不要新增依赖）。对每批要求：
- 每个节点生成 1 条中文查询，模拟真实用户向助手提问的口吻，8–25 字
- **严禁复制原文中连续 ≥6 个字的短语**（这是评测有效性的关键，务必在 prompt 里写死并自查：生成后逐题检查与原文的公共连续子串长度，超限的重生成一次）
- 交替产出两类：`kind="semantic"`（口语化改写，不带专业黑话）与 `kind="entity"`（包含关键实体/术语）
- 要求返回严格 JSON：`{"items":[{"idx":0,"query":"..."},...]}`；解析失败重试 1 次，仍失败则该批标记失败并继续

**负样本**：20 条，写在本脚本的常量列表里（询问库中确实不存在的主题：外部时事、虚构项目、语料中从未出现过的事），`kind="negative"`，`gold_ids=[]`。这 20 条也要人工写得不含糊（是真不存在，而不是「可能没有」）。

**输出** `eval/eval_set.json`：
```json
{"version": 1, "created_at": "ISO8601", "seed": 20260910,
 "items": [{"qid": "q001", "query": "...", "kind": "semantic|entity|negative",
            "gold_ids": [123], "gold_type": "memory", "gold_domain": "hermes"}]}
```

**参数**：`--limit N`（只生成前 N 题）、`--resume`（已存在的 qid 跳过并续写）、`--dry-run`（只抽样不调 API，打印分层分布）。
**结束**：打印成功/失败题数 + 分层分布；有失败时退出码非 0。

## 文件 3：`eval/run_eval.py`（命令行，argparse）

- 读 `eval/eval_set.json`，在**库副本**上跑 4 种模式，每种取 top-10 排名：
  `fts` / `vec` / `rrf` / `cascade`
  （`vec` 用 `TriviumStore().search_similar`，按实际签名适配；不启用图扩散）
- 正样本指标：`Recall@1/3/5/10`、`MRR@10`、`nDCG@5`；kb_chunk 题额外报 **doc-level recall**（把同一 `source_path` 的其他 kb_chunk 视为相关，列 `Recall@5(doc)`）
- 负样本：记录 top1 分数；报告「负样本 top1 分数中位数 vs 正样本 top1 分数中位数」分离度（fts 无分数，用是否命中表示）
- 输出：
  - `eval/results_<UTC时间戳>.json`：逐题原始排名 + 汇总
  - `eval/report_<UTC时间戳>.md`：模式对比表、分层细分表、错例 top10（query + gold_id + 各模式排名）
  - 报告里必须有真实库 sha256 前后一致性检查结果
- 参数：`--modes rrf,cascade`、`--limit N`、`--top-k 10`

## 文件 4：`eval/README.md`

怎么跑（venv python 调用示例 + 需要设置的环境变量）、题集 schema、指标定义、库副本保护机制说明。

## 文件 5：`tests/test_eval_metrics.py`

为 `eval/metrics.py` 写单测：recall@k / MRR / nDCG 的手算用例 + 边界（gold 不在结果里、单 gold、空结果、partial 命中）。不联网、不连库。

## 交付前自查（必须做，并在回复里贴实测输出）

1. `venv/Scripts/python.exe -m py_compile eval/metrics.py eval/gen_eval_set.py eval/run_eval.py`
2. `venv/Scripts/python.exe -m pytest tests/test_eval_metrics.py -q`
3. `venv/Scripts/python.exe eval/gen_eval_set.py --dry-run`（只抽样，验证分层与副本机制）
4. 全量 `venv/Scripts/python.exe -m pytest -q`（确认没弄坏既有测试）
5. 报告真实库文件 sha256 在 dry-run 前后未变

回复里贴出：改了哪些文件、上面 5 条命令的实际输出、剩余风险。不要写「逻辑上正确但未验证」。
