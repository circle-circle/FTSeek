"""
数据订阅模块

注意：实际实现位于 app/data_subscriber.py
本模块仅提供兼容性导入
"""
from app.data_subscriber import (
    DataSubscriber,
    create_subscriber,
    # 其他需要导出的内容
)

__all__ = [
    'DataSubscriber',
    'create_subscriber',
]
