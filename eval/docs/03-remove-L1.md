# 移除 L1 嗅探通道（判定标准：能优化则删，有作用则留）

## 判定依据（已核实，不用再调研）

L1 嗅探 = `mem_search` 顺带读 MEMORY.md（`HERMES_MEMORY_FILE`），命中塞进附加字段 `memory_file_hits`。
判定为「无作用，可删」的三条证据：

1. **零消费方**：`hermes-plugin/__init__.py` 的 `prefetch()` 只读 `resp["results"]`（第 222 行）；
   `hermes-plugin/context_engine.py` 只读 `results` / `neighbors`。全仓没有任何代码读 `memory_file_hits`。
2. **重复**：MEMORY.md 本身每轮已注入上下文，检索再返回一份属于重复开销。
3. **与既定设计一致**：MEMORY.md 是静态身份区，不做检索目标（「假门卫」那条纪律）。

## 删除清单（只删，不新增任何功能）

1. `mcp_tools/memory.py`
   - 删除 `_l1_sniff` 函数及其上方的 L1 注释块（约 485–520 行整段）
   - `_mem_search_impl` 内：删除 L1 前置嗅探调用（`l1_hits = (... _l1_sniff(query))` 那几行，含 scope==kb / block 判断的注释）
   - 删除该函数 docstring 里「v3.8 L1 独立附加区」那段描述
   - 删除结果组装处的 `result["memory_file_hits"] = l1_hits["hits"]` 及相邻注释
2. `config.py`：删除 `L1_MAX_SIZE`
3. `.env.example`：删除 `L1_MAX_SIZE` 与 `HERMES_MEMORY_FILE` 两行（含各自注释行）
4. `README.md` / `README_EN.md`：删除配置表里的 `L1_MAX_SIZE`、`HERMES_MEMORY_FILE` 两行
5. `tests/conftest.py`：删除 `os.environ.setdefault("HERMES_MEMORY_FILE", "")`
6. `tests/test_smoke.py`：删除 L1 嗅探的整段测试函数（含 docstring）
7. `CHANGELOG.md`：**不要修改任何历史条目**；在最新的未发布段追加一条 Removed 记录（若无 `## [Unreleased]` 段就新增之）：
   「移除 L1 嗅探通道（`memory_file_hits`）——零消费方，MEMORY.md 每轮已注入上下文，属重复开销」

## 硬约束

- 不新增功能、不改无关逻辑、不删与 L1 无关的任何代码
- Python 一律 `venv/Scripts/python.exe`
- 不改 `eval/` 目录（另有任务在用）

## 回归要求（贴实测命令与输出）

1. `venv/Scripts/python.exe -m py_compile mcp_tools/memory.py config.py tests/test_smoke.py`
2. `venv/Scripts/python.exe -m pytest -q`（数量会因删测试而减少，报告实际数字；不得有 failure/error）
3. 残留检查：`grep -rn "l1_sniff\|memory_file_hits\|HERMES_MEMORY_FILE\|L1_MAX_SIZE" --include=*.py --include=*.md --include=*.example .`（排除 venv/.git/eval/.tmp）——只应剩 CHANGELOG 的历史条目与本次新增的 Removed 条目，其余必须为空，贴输出
4. `venv/Scripts/python.exe -m pytest tests/test_smoke.py -q`
5. `sha256sum data/mh_memory.db` 前后一致
