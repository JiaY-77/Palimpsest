# 弃用 / 退役计划（DEPRECATIONS）

本文件登记字段、配置项、命令、接口的弃用与退役计划。每个条目写明：**背景**（为什么会有新旧两套）、
**当前状态**（哪些触点还在生效、对应真实行号/函数名）、**分阶段退役步骤**（先停写 → 再停读 →
最后删检查/索引，避免一次性移除把历史数据读挂）、**deadline / 目标版本**（占位，未锁定）与**验证命令**。
退役动作必须等「存量归零」确认后再推进，禁止静默删除兼容路径。

---

## character_name（domain 的历史兼容镜像）

### 背景

`domain` 是正式的区块/域字段（2026-08-29 起，commit `8e0f55f`）。`character_name` 是
2026-08-29 之前的旧字段，历史节点用 `character_name` 表达区块归属。读侧
`node_domain()`（`core/trivium_store.py` 的 `node_domain`，L50-60）已统一为
「`domain` 优先、`character_name` 回退、再缺省 `general`」；迁移期原计划写侧继续写镜像，
使两字段始终一致。

### 当前状态

写侧镜像已在本分支提前退役（commit `4df318f`「Retired compatibility mirror」）：
`domain` 是唯一写字段。以下 3 处原本写「domain + character_name 双字段」，现在只写 `domain`：

- `mcp_tools/memory.py` 的 `mem_ingest` 写入路径（`node_data`，约 L209-217）
- `core/consolidator.py` 的 `_merge_one_pair`（合并节点 `new_node_data`，约 L107-115）
- `core/trivium_store.py` 的插入路径（`insert_node` L250 / `insert_node_tx` L285：
  通用 payload 组装，按传入字段原样写入，不再生成 `character_name`）

仍在生效、尚未移除的 `character_name` 触点：

- **读侧回退**：`core/trivium_store.py::node_domain`（L59-60，`character_name` 兜底）
- **迁移脚本**：`scripts/migrate_domain.py`（把「无 domain 但有 character_name」的历史节点补齐 `domain`）
- **doctor 检查项**：`core/doctor.py::_check_legacy_domain_mirror`（L94-122，即 doctor 第 7 项
  「domain 字段迁移状态」）
- **索引字段列表**：`core/trivium_store.py::_init_indexes`（L154）、
  `scripts/rebuild_db.py::INDEX_FIELDS`（L79）

### 退役步骤（分阶段）

- **阶段一（已完成，commit `8e0f55f`）**：读侧统一走 `node_domain()`。
- **阶段二（本分支已提前落地，commit `4df318f`）**：停止写侧镜像——上述 3 处的
  `character_name` 写入已删除，`domain` 为唯一写字段；保留读侧回退至少一个版本作为缓冲。
- **阶段三（待办）**：运行 `python scripts/migrate_domain.py --apply` 把历史节点补齐 `domain`；
  用 doctor 第 7 项（`_check_legacy_domain_mirror`）确认存量归零。
- **阶段四（存量归零后进行，未开始）**：移除读侧回退（`node_domain` 的 `character_name` 兜底）
  与 doctor 第 7 项检查（`_check_legacy_domain_mirror`）；同步删除
  `core/trivium_store.py::_init_indexes`（L154）与 `scripts/rebuild_db.py::INDEX_FIELDS`（L79）
  里的 `character_name` 索引字段。

> 阶段顺序说明：原计划是「先迁移存量（阶段三）→ 再停写镜像」；本分支在审计整改中把停写镜像
> 提前到了阶段二。这不影响读侧正确性：尚未补齐 `domain` 的历史节点仍能经 `node_domain()`
> 回退命中。因此阶段四（删读侧回退 / 检查 / 索引）必须以阶段三做完、存量归零为前提，不得提前。

### deadline / 目标版本

占位目标，版本号未最终锁定（以发版计划为准，若有变动同步更新本文档与 CHANGELOG）：
**2.1.0 完成阶段三（存量迁移）；2.2.0 完成阶段四（移除读侧回退与索引）。**
阶段二（停写镜像）已在当前分支落地。

### 如何验证

```bash
# 1. dry-run 应报 migrated: 0（0 个待迁移）
python scripts/migrate_domain.py

# 2. doctor 第 7 项（domain 字段迁移状态）应为 ok
python scripts/palimpsest_cli.py doctor
```