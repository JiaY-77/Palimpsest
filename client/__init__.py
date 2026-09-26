"""客户端包（client）
===================

只读客户端入口使用——CLI、dashboard、各类维护脚本。

放在 ``client/`` 而非 ``core/`` 是**架构纪律**：``core/`` 不得依赖任何具体
存储实现（只依赖 ``protocols.Store``），否则 REST 服务（它也使用 ``core/``）
会与客户端形成循环依赖。详见 ``protocols.py`` 的依赖方向说明。
"""

from client.remote_store import RemoteStore

__all__ = ["RemoteStore"]
