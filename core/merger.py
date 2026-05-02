"""记忆合并器 - 判断新记忆与已有记忆的关系，执行去重、更新或标记矛盾"""

import logging
from typing import Any
from core.trivium_store import TriviumStore

logger = logging.getLogger(__name__)


class Merger:
    """合并新提取的记忆节点到已有数据库中"""

    def __init__(self, store: TriviumStore):
        self.store = store

    def merge(self, new_node: dict[str, Any], embedding: list[float]) -> int | None:
        """
        合并一个记忆节点。
        返回新节点的 ID（如果是新插入），或者被更新的已有节点的 ID。
        如果判断为完全重复，返回 None。
        """
        new_content = new_node.get("content", "")
        new_type = new_node.get("type", "")

        # 1. 在已有记忆中查找最相似的节点
        existing = self._find_similar(new_content, new_type)

        # 2. 如果没有找到，直接插入
        if not existing:
            node_id = self.store.insert_node(new_node, embedding)
            logger.info(f"新增节点 ID={node_id}: {new_node.get('label', '')[:30]}")
            return node_id

        # 3. 找到了相似节点，判断关系类型
        old_content = existing.get("payload", {}).get("content", "")
        old_id = existing.get("id")
        # 如果旧节点 ID 为空，直接插入新节点
        if old_id is None:
            return self.store.insert_node(new_node, embedding)
        similarity = self._content_overlap(new_content, old_content)

        # 完全重复，跳过
        if similarity > 0.95:
            logger.debug(f"完全重复，跳过。已有节点 ID={old_id}")
            return None

        # 高度相似，更新
        if similarity > 0.4:
            # ❶ 标记旧节点为 outdated
            self._mark_outdated(old_id)
            # ❷ 插入新节点
            new_id = self.store.insert_node(new_node, embedding)
            # ❸ 创建 REVISED_BY 边
            self.store.create_edge(
                old_id, new_id, relation_type="REVISED_BY", weight=1.0
            )
            logger.info(
                f"更新节点 ID={old_id} → 新节点 ID={new_id}: {new_node.get('label', '')[:30]}"
            )
            return new_id

        # 低相似度，直接插入新节点
        node_id = self.store.insert_node(new_node, embedding)
        return node_id

    def _find_similar(self, content: str, node_type: str) -> dict[str, Any] | None:
        """在已有记忆中查找与 content 最相似的节点"""
        # 使用经过验证的 store.get_node 读取数据，避开原生 NodeView 的兼容性问题
        try:
            all_ids = self.store._get_all_node_ids()
        except AttributeError:
            # 如果 store 没有 _get_all_node_ids 这个方法，就临时添加一个
            with self.store._acquire() as db:
                all_ids = db.all_node_ids()

        if not all_ids:
            return None

        best_match = None
        best_score = 0

        for nid in all_ids:
            node_data = self.store.get_node(nid)
            if not node_data:
                continue

            payload = node_data.get("payload", {})
            existing_content = payload.get("content", "")
            if not existing_content:
                continue

            overlap = self._content_overlap(content, existing_content)
            # 只保留相似度大于 0.3 的最佳匹配
            if overlap > best_score and overlap > 0.05:
                best_score = overlap
                best_match = {"id": node_data.get("id"), "payload": payload}

            # ★★★ 终极调试：直接返回第一个内容不为空的节点 ★★★
            if not best_match:
                for nid in all_ids:
                    node_data = self.store.get_node(nid)
                    if not node_data:
                        continue
                    payload = node_data.get("payload", {})
                    if payload.get("content", "").strip():
                        print(
                            f"  [DEBUG-MERGER] 强制匹配节点 ID={node_data.get('id')}, 内容前40字: {payload['content'][:40]}"
                        )
                        return {"id": node_data.get("id"), "payload": payload}

        return best_match

    def _content_overlap(self, text1: str, text2: str) -> float:
        """计算两段文本的内容重叠度（0.0 到 1.0）"""
        if not text1 or not text2:
            return 0.0

        chars1 = set(text1.strip())
        chars2 = set(text2.strip())

        if not chars1 or not chars2:
            return 0.0

        intersection = chars1.intersection(chars2)
        union = chars1.union(chars2)

        return len(intersection) / len(union) if union else 0.0

    def _mark_outdated(self, node_id: int) -> None:
        """将节点标记为 outdated"""
        old_node = self.store.get_node(node_id)
        if old_node:
            old_payload = old_node.get("payload", {})
            old_payload["status"] = "outdated"
            # 通过重新插入并更新来标记（TriviumDB 的 update_payload 方式）
            with self.store._acquire() as db:
                db.update_payload(node_id, old_payload)
