# Palimpsest Refactor 方案（P0/P1 已完成 · P2 待启动）

> 2026-08-27 主人批准：先做 P0，P1/P2 记录进任务清单。P0/P1 已完成并 commit（T053）。
> 2026-08-28 全身优化（TASK-20260828）：阶段 1/2 完成——core/ 遗留酒馆链路（importer/thinking_tracker/merger/extractor/pipeline/retriever）全部退役，冲突检测抽为 core/conflict.py 的 resolve_conflict（mem_ingest 复用），main.py 移除 /extract /import /retrieve 旧端点瘦身至 255 行；scripts 清理 migrate_soul_logs/rebuild_db/bak_v2.1。

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

## P1（待启动，中风险：文件级拆分，行为不变 + 全量回归）

- 目标：消除 mcp_server.py 单体（882 行）
- ① mcp_server.py → `mcp_tools/` 包：memory.py（mem_* 系列）/ kb.py（kb_*）/ graph.py（graph_neighbors/mem_link）/ routing.py（router_query），入口只留工具注册
- ② main.py 的 generate_report()（62 行）→ `core/reporting.py`
- ③ 大函数按步骤拆分：consolidator.consolidate()、build_kb_index.build()、graph_neighbors()
- 验证：每步 py_compile + 功能实测（mem_search/mem_ingest/mem_review/graph_neighbors）+ 分步 commit
- 成本预估：80-150K token

## P2（待启动，高风险：架构级，需单独讨论）

- ① REST/MCP 双入口共享 service 层（现 main.py 与 mcp_server.py 各调各的 core，有重复）
- ② 数据访问层补全：消灭所有外部调私有方法（`_get_all_node_ids` 等），补 `count_by_type()` / `recent_ids()` 等公共接口
- 验证：全量回归 + 性能对比（遍历次数下降）
- 成本预估：另议

## 沉淀索引

- Palimpsest 记忆：P0 完成记录（node 待查）；方向定案 node 523；灵感卡片删除 node 524
- 任务清单：T053（P1）、T054（P2）
