"""服务端存储包（server）
=====================

仅 REST 服务进程使用。``LocalStore`` 是**唯一持有数据库连接的写者**。

与 ``client/`` 的分工是架构核心：服务端走本地库，客户端走 HTTP，
两边都实现 ``protocols.Store``，于是 ``core/`` 可以完全不知道谁在背后。
"""

from server.local_store import LocalStore

__all__ = ["LocalStore"]
