# Palimpsest Refactor 方案（P0/P1/P2 已完成 · 静态门禁已建立 · P3 待启动）

> 2026-08-27 立项：先做 P0，P1/P2 记录进任务清单。P0/P1 已完成并 commit。
> 2026-08-28 全身优化（TASK-20260828）：阶段 1/2 完成——core/ 遗留酒馆链路（importer/thinking_tracker/merger/extractor/pipeline/retriever）全部退役，冲突检测抽为 core/conflict.py 的 resolve_conflict（mem_ingest 复用），main.py 移除 /extract /import /retrieve 旧端点瘦身；scripts 清理 migrate_soul_logs/rebuild_db/bak_v2.1。
> 2026-09-11 状态核对（代码审阅 + 落地静态门禁）：P1 三项全部完成；P2 数据访问层补全完成、共享 service 层待评估；同日本文件新增「静态质量门禁」章节，剩余重构项归入 P3。

## 背景：体检结果（2026-08-27）

| 问题 | 证据 |
|---|---|
| 单体文件 | mcp_server.py 882 行 / 21 函数（工具注册+校验+逻辑混在一起） |
| 重复代码 | 36 处重复 5 行块：embed→merge 流水线两处手写；节点遍历样板 `_get_all_node_ids + get_node + if not node` 全库 18 处 |
| 大函数 | consolidate() 118 行、build() 164 行、graph_neighbors() 90 行、mem_ingest() 77 行 |
| 私有方法外泄 | 多处直接调 `store._get_all_node_ids()`（store 缺公共查询接口） |
| scripts 样板 | 8 个脚本各自手写 sys.path 注入 |

## P0（已完成，commit 见 git log）

1. `TriviumStore.iter_payloads()` 公共生成器（core/trivium_store.py）——替换 14 处遍历样板
2. `core/pipeline.py` 的 `ingest_many()` / `embed_and_merge()`——消除 main.py 两处 embed+merge 重复
3. `scripts/_common.py` 统一项目根路径注入——9 个脚本样板替换
4. 保留边界：export_all_data.py 的遍历保留（有读失败警告语义）；dashboard 的 id 列表操作改列表推导

回归：py_compile 全绿 + REST /export /summary /retrieve + CLI recent/search + mcp_server import 全通过。

## P1（已完成）

- ① mcp_server.py 单体拆分 ✅ —— 工具实现落到 `mcp_tools/`（memory / kb / graph / routing / stats_tool / consolidate_tool），入口只留工具注册，**882 → 49 行**
- ② `main.py` 的 `generate_report()` → `core/reporting.py` ✅
- ③ 大函数按步骤拆分 —— ⚠️ 部分完成：`consolidator.consolidate()`、`graph_neighbors()` 已拆；仍有 4 个 >100 行函数（见 P3）

## P2（已完成）

- ① REST/MCP 双入口共享 service 层 —— **未做，待评估**：现状是两个入口各自调用 `core/` 的公共接口（`core/` 实际充当 service 层），仅在出现真实重复逻辑时才需要再抽一层
- ② 数据访问层补全 —— ✅ 生产代码不再外部调用 `store._get_all_node_ids` 等私有方法（仅 `tests/test_smoke.py` 用其做前后快照断言）

## P3（待启动）

- ~~大函数拆分~~ ✅ 已完成（2026-09-11）：4 个 >100 行函数全部拆到 ≤100（AST 复测「>100 行函数数 = 0」）——`search_similar` 137→100（含 42 行 docstring，主体为编排）、`mem_ingest` 125→约 55、`compute_stats` 119→约 25、`_apply_merge` 100→约 25；抽出的 helper 各自单一职责（候选过滤 / 衰减重排 / block 过滤 / 累加器 / 事务写入 / 单对合并）
- `scripts/` 瘦身（13,487 行，一次性脚本靠 `.gitignore` 的 `scripts/_t[0-9]*.py` 排除；有复用价值的脚本去编号后入库）
- ~~类型检查（mypy）~~ ✅ 已建立：非严格起步、当前覆盖 `core/`（`python -m mypy`，CI 的 typecheck job），后续按同样方式纳入 `mcp_tools/`
- ~~覆盖率基线（pytest-cov）~~ ✅ 已建立：`core/` + `mcp_tools/` 合计 76%，CI 输出报告不设门槛；缺口集中在 `mcp_tools/routing.py`（22%）、`mcp_tools/kb.py`（28%）、`core/reporting.py`（5%）

## 静态质量门禁（2026-09-11 建立）

2026-09-11 代码审阅的 P0 结论是「数据正确性做得很细，静态质量门禁从缺」：CI 只跑 pytest，`pyproject.toml` 无 `[tool.ruff]`，`git log --all -S "tool.ruff"` 为空（从未配置过 lint）。

落地内容：

| 项 | 内容 |
|---|---|
| 规则配置 | `pyproject.toml` 的 `[tool.ruff]` / `[tool.ruff.lint]`：**显式写 select**（不依赖 ruff 默认值，防版本漂移）；`RUF001/002/003` 因中文全角标点误报过多而 ignore |
| 版本钉住 | `requirements-dev.txt` 与 CI 均钉 `ruff==0.16.7` / `mypy==2.3.1` / `pytest-cov==7.1.0` |
| CI 阻断 | `.github/workflows/ci.yml` 三个 job：`lint`（`ruff check .`）、`typecheck`（`mypy`，core/ 非严格）、`test`（三版本 pytest + 覆盖率报告） |
| 存量清理 | 全仓库 ruff 问题清零（自动修复 + 人工判断），`# noqa` 全部有效（无 RUF100） |

维护约定：新增代码必须 `ruff check` 干净、`core/` 改动必须 `mypy` 干净；扩大规则集（如开启 `RUF001-003`、`DTZ`、`PLW`、`check_untyped_defs`）需先批量处理存量，不得直接放开。

## 沉淀索引

- Palimpsest 记忆：P0 完成记录（node 待查）；方向定案 node 523；灵感卡片删除 node 524
- 待办清单：P3 两项（大函数拆分、scripts 瘦身）、类型检查与覆盖率基线
