"""Pytest 配置文件"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# futu SDK 线程退出挂死修复 (2026-08-27, 根因定位): callback_executor 线程
# 默认非 daemon (SysConfig.get_all_thread_daemon()=False) 且 queue.get() 永久
# 等待; 测试触达 lazy 连接路径后, 解释器退出时 threading._shutdown 逐锁等它
# — 全量套件 141s 跑完但退出挂死 1h+ 实证 (07-30/31 同症的最久 11h)。
# 必须在任何测试创建 futu 连接之前设置 (network_manager 主循环本就 setDaemon)。
try:
    from futu.common.sys_config import SysConfig
    SysConfig.set_all_thread_daemon(True)
except Exception:
    pass


import pytest


@pytest.fixture(autouse=True)
def _reset_market_regime_gate_cache():
    # MarketRegimeGate._assess_cache 是进程级类属性 (2026-07-31 为省 AlphaVantage
    # 付费调用加的 TTL 缓存) — 测试间不清会串上下文 (全量跑 test_risk_on_normal_vix
    # 挂、单跑过的实证)。
    try:
        from app.playbook.market_regime import MarketRegimeGate
        MarketRegimeGate._assess_cache = None
    except Exception:
        pass
    yield


def pytest_sessionfinish(session, exitstatus):
    # futu SDK 线程退出兜底 (2026-08-27 修正): 事后置 t._daemonic 对 3.8+ 无效 —
    # threading._shutdown 按线程【启动时】注册的 _shutdown_locks 逐锁 acquire 等
    # 线程退出, 不复查 t.daemon (机制实证: 旧 hack 挂死/清锁干净退出)。
    # 清空 _shutdown_locks = 把已启动的非 daemon 线程事后 daemon 化: 解释器
    # finalize 阶段照常击杀这些空闲循环线程, 正常退出路径不受影响。
    import threading
    for t in threading.enumerate():
        if t is not threading.main_thread():
            try:
                t._daemonic = True
            except Exception:
                pass
    try:
        with threading._shutdown_locks_lock:
            threading._shutdown_locks.clear()
    except Exception:
        pass
