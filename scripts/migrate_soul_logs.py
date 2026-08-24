"""SOUL.md 变更日志 → MemoryHub 迁移脚本（2026-08-24 建立）

用途：把 SOUL.md 的变更日志（历史事件）平移进 MemoryHub，SOUL 只保留最近几条 + 指针。
语义设计：
- 版本日志 = 历史事件（永远真实），所以全部 type=event、status=active，
  不用 mem_ingest 的自动冲突检测（会把旧版本误标 outdated）。
- 手动建 REVISED_BY 边：新版本 → 旧版本，形成版本演进图谱。
- importance 按新旧递增（0.5 → 0.9），最近的版本更相关。

用法：cd Memory_Hub && ./venv/Scripts/python.exe scripts/migrate_soul_logs.py
"""
import re
import sys
import time

sys.path.insert(0, "D:/HeJiaQi/Documents/Code/Python/Memory_Hub")
from core.trivium_store import TriviumStore  # noqa: E402

SOUL_PATH = r"C:/Users/七七/AppData/Local/hermes/SOUL.md"
SOURCE = "SOUL.md"
DOMAIN = "hermes"


def parse_logs(text: str):
    """解析变更日志，返回 [(date, version, content), ...]，按版本号排序（旧→新）"""
    m = re.search(r"## 变更日志\n(.*)", text, re.S)
    if not m:
        raise SystemExit("未找到 ## 变更日志 段落")
    raw = re.findall(
        r"- \[日志\] (\d{4}-\d{2}-\d{2}) 版本 (\d+\.\d+)(.*)", m.group(1)
    )
    logs = []
    for date, ver, content in raw:
        logs.append((date, ver, content.strip()))
    # 按版本号排序（旧 → 新）
    logs.sort(key=lambda x: [int(p) for p in x[1].split(".")])
    return logs


def main():
    soul = open(SOUL_PATH, encoding="utf-8").read()
    logs = parse_logs(soul)
    print(f"解析到 {len(logs)} 条日志，从 {logs[0][1]} 到 {logs[-1][1]}")

    store = TriviumStore()

    # ---- 增量模式：跳过已存在的版本号（防重复 ingest）----
    existing_map = {}  # version -> node_id
    for nid in store._get_all_node_ids():
        node = store.get_node(nid)
        payload = node.get("payload", {}) if node else {}
        if payload.get("type") == "event" and payload.get("character_name") == DOMAIN:
            m = re.search(r"版本\s*v?([\d.]+)", payload.get("content", ""))
            if m:
                existing_map[m.group(1)] = nid
    if existing_map:
        before = len(logs)
        logs = [l for l in logs if l[1] not in existing_map]
        print(f"增量模式：已有 {len(existing_map)} 个版本，本次新增 {len(logs)} 条（跳过 {before - len(logs)} 条）")
    if not logs:
        print("没有新增日志，无需迁移")
        return

    node_ids = {}
    for i, (date, ver, content) in enumerate(logs):
        text = f"[SOUL变更日志] {date} 版本 {ver}：{content}"
        emb = store.embed_text(text)
        # importance 按新旧递增 0.5 → 0.9
        importance = round(0.5 + 0.4 * i / max(len(logs) - 1, 1), 2)
        nid = store.insert_node(
            {
                "type": "event",
                "content": text,
                "importance": importance,
                "character_name": DOMAIN,
                "source": SOURCE,
            },
            emb,
        )
        # 补写真实时间戳（insert_node 基础 payload 固定 created_at=None）
        node = store.get_node(nid)
        payload = node.get("payload", {}) if node else {}
        if payload.get("created_at") is None:
            payload["created_at"] = time.time()
            store.update_payload(nid, payload)
        node_ids[ver] = nid
        # REVISED_BY 边：当前版本 → 上一版本（新修订旧）
        # 增量模式：第一个新增版本连到已有版本中最大的那个（防链断裂）
        if i == 0 and existing_map:
            prev_ver = sorted(existing_map.keys(), key=lambda v: [int(p) for p in v.split(".")])[-1]
            if prev_ver:
                store.create_edge(
                    nid,
                    existing_map[prev_ver],
                    "REVISED_BY",
                    content=f"{ver} 修订 {prev_ver}",
                    weight=0.9,
                )
        elif i > 0:
            prev_ver = logs[i - 1][1]
            store.create_edge(
                nid,
                node_ids[prev_ver],
                "REVISED_BY",
                content=f"{ver} 修订 {prev_ver}",
                weight=0.9,
            )
        print(f"  ✓ v{ver} → id={nid} (importance={importance})")

    print(f"\n完成：迁移 {len(node_ids)} 个节点，建立 {len(node_ids)-1} 条 REVISED_BY 边")
    print("节点映射：", node_ids)


if __name__ == "__main__":
    main()
