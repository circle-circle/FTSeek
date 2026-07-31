"""Pytest 配置文件"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def pytest_sessionfinish(session, exitstatus):
    # futu SDK 线程非 daemon: 测试触达 lazy 连接的 app 代码路径后,
    # network_manager/callback_executor 会永久卡住解释器退出
    # (07-30/31 三具挂死 pytest 实证, 最久 11h 白占 170MB)。
    # 线程已 start 无法改 daemon 属性, 直接置私有标记绕过 join。
    import threading
    for t in threading.enumerate():
        if t is not threading.main_thread():
            try:
                t._daemonic = True
            except Exception:
                pass
