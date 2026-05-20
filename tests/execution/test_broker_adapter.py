# tests/execution/test_broker_adapter.py
import pytest
from app.execution.broker_adapter import FutuBrokerAdapter

def test_futu_adapter_initialization():
    """测试富途适配器初始化"""
    adapter = FutuBrokerAdapter(
        host="127.0.0.1",
        port=11111,
        trade_env=1,  # 模拟环境
    )
    
    assert adapter.is_connected() == False  # 初始未连接
    
    # 注意：实际连接测试需要 FutuOpenD 运行
    # 这里只测试接口
