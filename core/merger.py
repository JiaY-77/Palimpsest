"""记忆合并器 - 基于向量相似度的智能合并去重"""

import logging
from typing import Any, Tuple, Optional
from core.trivium_store import TriviumStore

logger = logging.getLogger(__name__)


class Merger:
    """合并新提取的记忆节点到已有数据库中"""

    def __init__(self, store: TriviumStore):
        self.store = store

    def merge(self, new_node: dict[str, Any], embedding: list[float]) -> Optional[int]:
        """
        合并一个记忆节点。
        返回新节点的 ID（如果是新插入），或者被更新的已有节点的 ID。
        如果判断为完全重复，返回 None。
        """
        SIMILAR_HIGH_THRESHOLD = 0.85
        SIMILAR_MID_THRESHOLD = 0.4

        # 查找最相似的已有节点
        similar = self._find_similar(embedding)
        if similar is None:
            # 没有相似节点，直接插入
            node_id = self.store.insert_node(new_node, embedding)
            logger.info(f"新增节点 ID={node_id}: {new_node.get('label', '')[:30]}")
            return node_id

        old_id, score = similar

        # 完全重复
        if score > SIMILAR_HIGH_THRESHOLD:
            logger.debug(f"完全重复，跳过。已有节点 ID={old_id}, 相似度={score:.3f}")
            return None

        # 高度相似，需要更新
        if score > SIMILAR_MID_THRESHOLD:
            logger.info(f"检测到高度相似节点 ID={old_id}, 相似度={score:.3f}, 执行更新")
            self._mark_outdated(old_id)
            new_id = self.store.insert_node(new_node, embedding)
            self.store.create_edge(
                old_id, new_id, relation_type="REVISED_BY", weight=score
            )
            logger.info(
                f"更新节点 ID={old_id} → 新节点 ID={new_id}: {new_node.get('label', '')[:30]}"
            )
            return new_id

        # 相似度低，直接插入
        node_id = self.store.insert_node(new_node, embedding)
        logger.info(f"新增节点 ID={node_id}: {new_node.get('label', '')[:30]}")
        return node_id

    def _find_similar(self, embedding: list[float]) -> Optional[Tuple[int, float]]:
        """
        查找与 embedding 最相似的已有节点。
        返回 (node_id, similarity_score)，如果没有则返回 None。
        """
        results = self.store.search_similar(embedding, top_k=1, expand_depth=0)
        if not results:
            return None
        first = results[0]
        node_id = first.get("id")
        score = first.get("score", 0.0)
        # 防御：确保 id 是整数且 score 是数字
        if node_id is not None and isinstance(node_id, int):
            return (node_id, float(score))
        return None

    def _mark_outdated(self, node_id: int) -> None:
        """将节点标记为 outdated"""
        old_node = self.store.get_node(node_id)
        if old_node:
            old_payload = old_node.get("payload", {})
            old_payload["status"] = "outdated"
            with self.store._acquire() as db:
                db.update_payload(node_id, old_payload)
