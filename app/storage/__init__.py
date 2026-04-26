"""
FTSeek 数据存储模块
"""
from .database import Database, init_database, get_database
from .repository import get_db, get_pool, shutdown_all, DatabasePoolManager

__all__ = ['Database', 'init_database', 'get_database', 'get_db', 'get_pool', 'shutdown_all', 'DatabasePoolManager']
