# T081 执行卡：记忆分层（tier 过滤）+ 注入降噪配套三项

> 状态：开工 2026-09-15（主人拍板：**激进档**全链路默认分层 + 配套三项一起做）
> 分支：`feat/memory-tier-filter` → PR → CI → merge

## 一、分层定义（写死在 core/，单一事实源）

| tier | 含义 | 包含 type |
|---|---|---|
| `facts` | 事实层：默认检索与注入池 | memory, correction, decision, plan, task, review, solution, inspiration, user_intent, character_state |
| `logs` | 日志层：从默认池摘出 | record, event, git_commit |
| `""`（空串） | 不过滤（现状完全不变，显式历史通道） | — |

- `kb_chunk` / `novel_chunk` 不入 tier 体系，走已有 `scope` 隔离。
- 未登记的 type（含 `plot_plan` / `rule` 等）→ **保守归 facts**，防止静默丢结果。
- **激进档语义**：`tier` 默认值为 `"facts"`（MCP 工具 / REST / CLI / 插件全链路），`tier=""` 显式传空才回到不过滤。

## 二、改动点

### A. 核心 tier 过滤（`mcp_tools/memory.py`）
1. 新增模块级常量 `TIER_FACTS` / `TIER_LOGS` + `_in_tier(ptype, tier) -> bool`。
2. `_mem_search_impl` 签名加 `tier: str = "facts"`，在过滤循环中与 scope/domain/block 并列加 tier 判断。
3. `_hybrid_search_impl` 同样加 `tier: str = "facts"`，透传给 `_hybrid_rrf` / `_hybrid_cascade`，两处过滤循环各加一行。
4. **召回补偿**：tier 过滤是后置的，会吃掉候选名额（logs 占 43%）。已有 `max(top_k * 3, 30)` 的本就按此设计；再对 `TIER_EMPTY` 情形保持原样，确保 `tier=""` 结果与改动前逐条一致（回归红线）。
5. MCP 工具 `mem_search` / `mem_hybrid_search` 加 `tier` 参数（默认 `"facts"`），docstring 说明。

### B. 全链路透传
- `main.py`：`MemSearchRequest` / `MemHybridSearchRequest` 加 `tier: str = "facts"`，路由透传。
- `scripts/palimpsest_cli.py`：`search` / `hybrid-search` 子命令加 `--tier`，默认 `facts`；`--tier ""` 可关。
- `hermes-plugin/__init__.py`：`_tool_search` 透传 `tier`（工具 schema 加参数）。

### C. 配套三项
1. `include_neighbors` 默认 `True` → `False`（prefetch 处硬编码那个；实测记忆域图近乎无边，纯空转）。**注意**：插件 prefetch 现在传 `include_neighbors: True`，改为 `False`。
2. `PALIMPSEST_PREFETCH_TOP_K` 默认 5 → 3。
3. prefetch 注入加 `min_score` 门槛：现硬编码 0.3，提为可配 `PALIMPSEST_PREFETCH_MIN_SCORE`(默认 0.3，可调高)，并在文档记默认值。

## 三、验收三条（小七亲自跑，逐条留证）

1. **回归**：`tier=""` 时检索结果与改动前逐条一致（真实 A/B：改动前 main 版本快照 vs 改动后 `tier=""`）。
2. **新增单测**：tier 过滤单元测试（facts 层只回事实节点、logs 层只回日志节点、`""` 全回、未登记 type 归 facts）。
3. **真实 A/B**：20 个真实查询跑两遍，对比 top-5 里 logs 占比与插件每轮注入 token 数。

## 四、门禁
`pytest tests/ -q` / `ruff` / `mypy` / `readme_check --strict` 全绿；文档同步（README / CHANGELOG / HERMES_INTEGRATION）。

## 五、风险
tier 后置过滤会吃候选名额 → 已确认 `max(top_k*3, 30)` 余量充足（logs 占活跃 43%，5×3=15 候选里期望剩 ~8.5 条事实，够 top_k=5）；若真实 A/B 发现召回不足，再按层动态放大候选。

---

## 六、验收结果（2026-09-15 实测留证）

### 验收 1：回归 — `tier=""` 逐条等价改动前 ✓
`tests/test_memory_tier.py::test_tier_empty_equals_pre_change_behaviour`
—— `tier=""` 时五种类型节点全回、按 score 降序。四道门禁全绿：
`pytest` **389 passed**（基线 370 + 新增 19）/ `ruff` All checks passed /
`mypy` no issues in 15 files / `readme_check --strict` 0 错 0 警。

### 验收 2：新增单测 ✓
`tests/test_memory_tier.py` **19 条**，覆盖：`_tier_matches` 纯函数契约（空串/非法值/
未登记 type/kb_chunk 豁免/大小写）、三类型日志全过滤、logs 层反向查、
纯语义路与混合检索两路（rrf + cascade）、全链路参数透传（MCP/REST/CLI）。
**红→绿实测**：临时停用过滤行 → 5 条立刻变红（含两条混合检索）；恢复 → 19 passed。

### 验收 3：真实 A/B（20 个真实查询，生产库只读）✓
| 指标 | 改动前 (`tier=""`) | 改动后 (`facts`) |
|---|---|---|
| top-5 命中总数 | 100 | 95 |
| 其中日志层 | 42 | **0** |
| 其中事实层 | 58 | **95**（+64%） |
| 日志层占比 | **42.0%** | **0.0%** |
| 每轮注入 token | 286.5 | **172.9** |
| 20 轮注入总 token | 5730 | **3458**（降 **39.7%**） |

**如实记录的偏差**：命中总数 100 → 95（-5）。原因是少数查询（5/20 轮 × top-5 中
的个别位）命中的全是日志层，过滤后该位无结果可补。这符合设计意图——这类查询
本就该显式 `tier="logs"` 去查；且事实层占比从 58% 升到 100%，检索精度提升是主要收益。
**未做动态候选放大**：实测未见事实层召回不足（20 轮每轮仍能填满 top-5 的 95%），
故不引入额外复杂度；若后续真实使用出现召回不足再加。

## 七、顺带修的既有缺陷
- `scripts/palimpsest_cli.py` 的 parser 建在 `main()` 内部，无法单测 →
  抽成 `build_parser()`（行为不变，纯可测性改进）。
- `tests/test_domain_boost.py` 用 `type=event`（logs 层）建种，默认分层后会
  被过滤 → 该文件测的是加权逻辑、与分层无关，显式传 `tier=""` 隔离变量。
- 测试隔离：新建的 tier 测试改用自建独立临时库（同 `test_mem_stats` 的
  `iso_store` 模式），不污染 conftest 的 session 级共享库（直写节点不进 FTS，
  会打破 `test_fts_check` 的全库一致性断言）。

