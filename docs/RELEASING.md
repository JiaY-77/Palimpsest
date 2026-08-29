# Release 流程（发布规范）

> 目标：让每次发布都可预期、可追溯、可回滚。发布是严肃动作，不是随手打 tag。

## 版本号规则（语义化版本）

格式 `主.次.修订`（如 `1.0.0`、`1.1.0`、`1.0.1`）：

| 变更类型 | 示例 | 版本 | 必须 |
|---|---|---|---|
| **破坏性变更**（不兼容 API/存储格式/行为） | 改了 MCP 工具签名、库格式不兼容 | 主版本 +1 | 迁移指南 |
| **新增功能**（向后兼容） | 新 MCP 工具、新 CLI 命令、新能力 | 次版本 +1 | 测试通过 |
| **修复**（bug 修复，无新功能） | 修 bug、性能优化、文档 | 修订号 +1 | 测试通过 |

- 1.0.0 = 第一个正式稳定基线（2026-08-29）
- **未发布前**：主版本 0.x 可随意破坏（但本项目已定 1.0 为对外起点，不再用 0.x）
- 预发布标签：`1.1.0-rc.1`（Release Candidate，正式发版前验证用）

## 发布清单（每次发版逐项打勾）

### 1. 前置检查（发版前必须全过）

```bash
# 测试全绿
python -m pytest tests/ -v

# CI 全绿（GitHub Actions 三个 Python 版本）
# 查看：https://github.com/JiaY-77/Palimpsest/actions

# 启动自检
venv\Scripts\python.exe scripts\palimpsest_cli.py startup-check
```

### 2. 版本号决策

- 对照上表确定 `主.次.修订`
- 用 `core/version.py` 的 `version_bump(current, kind)` 计算新版本号（或手动）

### 3. 更新 CHANGELOG.md

- 把 `[Unreleased]` 下的条目移动到新版本小节
- 补发布日期、版本链接
- 重要变更写清「迁移指南」（若有）

### 4. 打 tag + 发 Release

```bash
# 打 tag（v 前缀）
git tag -a v1.0.0 -m "Palimpsest 1.0.0"

# 推送 tag
git push origin v1.0.0

# 创建 GitHub Release（标题 = 版本号，正文 = CHANGELOG 对应小节）
gh release create v1.0.0 \
  --title "Palimpsest 1.0.0" \
  --notes "$(sed -n '/## \[1.0.0\]/,/^## \[/p' CHANGELOG.md)"
```

### 5. 发布后确认

```bash
# REST 版本号正确
curl http://127.0.0.1:8090/ | grep version

# Release 页面存在
gh release view v1.0.0
```

## 谁负责

- **版本号决策**：项目维护者（作者）拍板
- **发版执行**：维护者或维护者授权的助手
- **发布说明**：从 CHANGELOG 对应小节复制，不额外编造

## 红线

1. **测试不过不发版**（pytest 必须全绿，CI 必须绿）
2. **脱敏审计不过不发版**（push 前 secret_scan 扫一遍，公开仓库历史无隐私文件）
3. **不重复打同一版本号**（tag 已存在则用更高版本号）
4. **破坏性变更必须有迁移指南**，否则不发
