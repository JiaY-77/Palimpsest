# 测试与开发（Testing & Development）

> 怎么跑测试、怎么读评测结果、仓库长什么样、加代码要过哪些门禁。
> 提交 PR 的完整流程见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

---

## 测试与评测

```bash
python -m pytest tests/ -v                                 # 在仓库根执行
DB_PATH=/tmp/stress.db python -m uvicorn main:app --port 8091
python scripts/rest_stress.py --base http://127.0.0.1:8091 --seeds 200 --out report.json
venv/Scripts/python.exe eval/gen_eval_set.py --dry-run     # 需要 DEEPSEEK_API_KEY
venv/Scripts/python.exe eval/run_eval.py
```

测试套件覆盖核心闭环：写入 → `mem_search` 命中 → `mem_get_full` 全文往返；图谱建边 → `graph_neighbors` / `mem_communities`；敏感扫描拒绝含密钥内容；混合检索 FTS 侧命中标记；冲突检测 / 版本链与 outdated 检索语义；`consolidate` / `promote` 干跑与幂等；PUT/PATCH 部分更新保留字段；并发与失败路径。`tests/conftest.py` 在导入前把 `DB_PATH` 重定向到临时库，套件永不碰生产库，用确定性 fake embedder，无需在线 Ollama 即可全绿。

`scripts/rest_stress.py` 应用层压测覆盖 6 类真实场景（高频检索 / 批量写入 / 图谱建边与扩散 / 边界输入 / 读写混合长压 / 写入后召回正确性），输出 qps 与 p50/p95/p99。`eval/` 离线评测从库内真实节点反推题集，对 `fts` / `vec` / `rrf` / `cascade` 算 Recall@K、MRR@K、nDCG@K；脚本先把真库复制到 `eval/.tmp/` 只在副本上读写、并对真库前后算 SHA256 自证。配套工具：`scripts/retrieval_probe.py` / `scripts/prod_entrypoint_check.py` / `scripts/ab_snapshot_*.py`；题集与指标定义见 [`eval/README.md`](../eval/README.md)；**完整评测体系（题集怎么造 · 指标怎么定义 · 结果怎么读）与汇总结论**——含一次消融实验：把排序从「乘性硬加权」改为「ε 级加性软加权」后，记忆层 Recall@5 由 0.263 提升到 0.684——见 [`docs/RETRIEVAL_QUALITY.md`](RETRIEVAL_QUALITY.md)。

## 项目结构

`main.py`（REST 唯一写者）/ `mcp_server.py`（stdio 逃生梯）/ `config.py` → `core/`（共享引擎，无框架依赖）→ `mcp_tools/`（19 个工具，MCP/REST/CLI 共用）；另有 `scripts/` 运维脚本、`eval/` 离线评测、`hermes-plugin/` 双插件、`tests/`、`docs/`、`data/`。分层约定与完整树见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

**图谱建边：** `scripts/build_structural_edges.py` 从七个来源为图谱补边（同源分组 `PART_OF` / 切片顺序 `NEXT` / 技能相关 `SKILL_RELATED_TO` / 小说关系 `RELATED_TO` / Markdown 双链 `WIKI_LINK` / 会话时序链 `FOLLOWS` 与名称提及 `MENTIONS` / **知识关联 `REFERENCES`**——把记忆的 `linked_from` 落成「记忆 → 知识切片」的边）。**默认只读 dry-run** 并打印影响面预测，确认后加 `--apply` 落库（批次清单可 `--rollback` 精确回滚）。`build_kb_index.py` / `build_skill_index.py` 重建索引后会自动补边，可用 `PALIMPSEST_BUILD_EDGES=0` 关闭。

**治理可视化：** `scripts/dashboard.py` 除检索面板外，另提供三块只读视图——冲突链（被取代节点与修订链）、遗忘曲线（按时间衰减 × 重要度排序）、图谱规模（孤立率与边标签分布）。

## 开发指南

- **虚拟环境：** 每个 checkout 单独建一个（`python -m venv venv`）并 `pip install -r requirements.txt`。
- **新增工具：** 在 `mcp_tools/` 内用共享的 `@mcp.tool()` 装饰器注册——它会立即同时出现在 MCP 服务、REST 层与 CLI 中。
- **新增核心模块：** 保持 `core/` 不引入 FastAPI/MCP；经由 `mcp_tools/` 与 `main.py` 消费。
- **改了 schema？** 重建 FTS 索引（`fts-rebuild`）与知识库索引（`build_kb_index.py`）。
- **测试：** 保持隔离——绝不让测试指向生产数据库。

提交 PR 前请先过质量门禁（与 CI 的 `lint` / `typecheck` / `test` 三个 job 一一对应）：

```bash
python -m pytest tests/ -q                         # 测试（CI 上跑 3.10 / 3.11 / 3.12）
ruff check .                                       # 静态检查（规则集钉在 pyproject.toml）
mypy                                               # 类型检查（当前覆盖 core/）
python -m pytest --cov=core --cov=mcp_tools -q     # 覆盖率基线
```

文档与代码的一致性由 `scripts/readme_check.py` 检查（MCP 工具清单 / CLI 子命令 / REST 路由 / 配置项键名与默认值 / 文件引用 / 行内代码配对），CI 的 `docs` job 以 `--strict` 跑：`python scripts/readme_check.py --strict`。

版本发布遵循 [语义化版本](https://semver.org/lang/zh-CN/)，流程见 [RELEASING.md](RELEASING.md)，历史见 [CHANGELOG.md](../CHANGELOG.md)，弃用 / 退役计划见 [DEPRECATIONS.md](DEPRECATIONS.md)。
