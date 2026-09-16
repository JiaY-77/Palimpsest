"""向量维度探测。自 scripts/reindex.py 下沉到 core，消除 core→scripts 反向依赖。"""

import contextlib


def get_db_dim(store):
    """读取库实际维度（从 DB 的 storage_info）。

    返回 (dim: int, error: str | None)。
    """
    import triviumdb
    db = None
    try:
        # 这里必须传 dim=store.dim：TriviumDB 构造函数要求该参数。
        # 依赖 triviumdb 一个未文档化行为：打开【已存在】的库时此参数被忽略，
        # 实际维度以库内 storage_info() 为准——这正是本函数随后读
        # storage_info()["dim"] 而不是信任入参的原因。
        # 风险提示：若 triviumdb 将来改为校验 dim 与库内实际维度的一致性，
        # 此处会开始抛错，届时需改为「先探测维度再开库」的两段式。
        db = triviumdb.TriviumDB(store.db_path, dim=store.dim)
        info = db.storage_info()
        return info.get("dim"), None
    except Exception as e:  # noqa: BLE001 —— 读维度失败返回错误信息交由体检汇总提示
        return None, f"无法读取数据库维度: {e}"
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()