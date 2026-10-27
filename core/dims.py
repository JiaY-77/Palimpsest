"""向量维度探测。自 scripts/reindex.py 下沉到 core，消除 core→scripts 反向依赖。"""

import contextlib


def get_db_dim(store):
    """读取库实际维度（从 DB 的 storage_info）。

    返回 (dim: int, error: str | None)。
    """
    import triviumdb

    from core.trivium_store import try_acquire_existing

    db = None
    try:
        # 单例进程内优先复用共享连接（issue #167）：本进程已持有该库时，裸开
        # 第二条连接必被连接级排他拒绝。尚无共享连接时才裸开（独立 CLI 场景不变）。
        # 这里必须传 dim=store.dim：TriviumDB 构造函数要求该参数。
        # 依赖 triviumdb 一个未文档化行为：打开【已存在】的库时此参数被忽略，
        # 实际维度以库内 storage_info() 为准——这正是本函数随后读
        # storage_info()["dim"] 而不是信任入参的原因。
        # 风险提示：若 triviumdb 将来改为校验 dim 与库内实际维度的一致性，
        # 此处会开始抛错，届时需改为「先探测维度再开库」的两段式。
        # auto_build_quiver=False：只探测维度，不触发 QuIVer 构图（见 trivium_store 同处注释）。
        db = try_acquire_existing(store.db_path)
        if db is None:
            db = triviumdb.TriviumDB(store.db_path, dim=store.dim, auto_build_quiver=False)
        info = db.storage_info()
        return info.get("dim"), None
    except Exception as e:  # noqa: BLE001 —— 读维度失败返回错误信息交由体检汇总提示
        return None, f"无法读取数据库维度: {e}"
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()
