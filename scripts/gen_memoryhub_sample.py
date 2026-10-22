"""生成 MemoryHub 记忆包示例（纯虚构内容，用于对外文档）。

为什么用脚本生成而不是手写 JSON：manifest.fingerprint 是 nodes+edges 的稳定指纹，
手写必然对不上，示例包就不"真"了。用仓库自己的 pack_fingerprint 算，保证示例与
真实记忆包格式完全一致。

内容全部虚构（一个假想的小团队 AI 助手），不含任何真实个人信息。
其中第 5 个节点**故意**包含一个假的 API Key，用来演示敏感扫描——文档里会明确标注。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.memoryhub import MEMORYHUB_FORMAT, MEMORYHUB_VERSION, pack_fingerprint  # noqa: E402

# 故意写成分段的假 key，避免被任何工具误当真凭据使用
FAKE_KEY_CONTENT = (
    "部署备注：本地测试时曾把 key 写进代码里，形状像 "
    "sk-demo-" + "x" * 24 + " （示例占位，不是真凭据）。后来改成读环境变量。"
)

NODES = [
    {
        "node_id": 101,
        "payload": {
            "type": "memory",
            "content": "用户偏好：这个项目统一用 TypeScript，不允许 any 逃逸；遇到第三方库缺类型时自己写声明文件。",
            "importance": 0.7,
            "domain": "demo",
            "status": "active",
        },
    },
    {
        "node_id": 102,
        "payload": {
            "type": "correction",
            "content": "用户纠正过两次：测试必须覆盖失败路径，只测 happy path 不算完成。原话是「报错的那条路才是真实世界」。",
            "importance": 0.85,
            "domain": "demo",
            "status": "active",
        },
    },
    {
        "node_id": 103,
        "payload": {
            "type": "task",
            "content": "[完成 2026-03-14] 把日志模块从 console 换成 pino：分级 + 结构化字段，生产环境关掉 debug。",
            "importance": 0.5,
            "domain": "demo",
            "status": "active",
        },
    },
    {
        "node_id": 104,
        "payload": {
            "type": "record",
            "content": "[2026-03-12] 跑了一次全量 lint 修复，动了 12 个文件，全是格式问题。",
            "importance": 0.2,
            "domain": "demo",
            "status": "active",
        },
    },
    {
        "node_id": 105,
        "payload": {
            "type": "memory",
            "content": FAKE_KEY_CONTENT,
            "importance": 0.4,
            "domain": "demo",
            "status": "active",
        },
    },
    {
        "node_id": 106,
        "payload": {
            "type": "plan",
            "content": "下一阶段打算接入 CI：先跑 lint + 类型检查，再补单元测试，最后加覆盖率门槛。",
            "importance": 0.6,
            "domain": "demo",
            "status": "active",
        },
    },
]

EDGES = [
    {"source_id": 102, "target_id": 101, "label": "RELATED_TO", "weight": 1.0},
    {"source_id": 103, "target_id": 101, "label": "REFERS_TO", "weight": 1.0},
    {"source_id": 106, "target_id": 102, "label": "REFERS_TO", "weight": 1.0},
]

MANIFEST = {
    "name": "示例记忆包：一个虚构的小团队 AI 助手",
    "kind": "memory_pack",
    "created_at": 1773446400.0,  # 2026-03-14 固定时间，保证示例可复现
    "producer": "palimpsest (sample)",
    "origin_db": "sample.db",
    "fingerprint": pack_fingerprint({"nodes": NODES, "edges": EDGES}),
    "node_total": len(NODES),
    "edge_total": len(EDGES),
    "story": (
        "这是一个虚构的四人小团队的 AI 助手记忆。团队只有一条硬规矩——测试要覆盖失败路径；"
        "一条技术约定——只用 TypeScript；一次做过的技术升级——日志换成 pino；"
        "一次日常维护——批量 lint；一份下一阶段计划——接 CI。"
        "另外故意留了一条写错 key 的旧笔记，用来演示敏感扫描。"
    ),
}

pack = {
    "format": MEMORYHUB_FORMAT,
    "format_version": MEMORYHUB_VERSION,
    "manifest": MANIFEST,
    "nodes": NODES,
    "edges": EDGES,
}

out = ROOT / "docs" / "examples" / "memoryhub-sample.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"写入 {out}（{out.stat().st_size} bytes，指纹 {MANIFEST['fingerprint'][:16]}…）")
