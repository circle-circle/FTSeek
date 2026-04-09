#!/usr/bin/env python3
"""
修复 OpenD 连接和异步事件循环问题
"""
import sys
import os
import asyncio
import threading
import time
import socket

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_opend_connection():
    """测试 OpenD 连接"""
    print("🔍 测试 OpenD 连接...")
    
    host = '127.0.0.1'
    port = 11111
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        
        if result == 0:
            print(f"✅ OpenD 连接成功: {host}:{port}")
            return True
        else:
            print(f"❌ OpenD 连接失败: {host}:{port} (错误码: {result})")
            return False
        
        sock.close()
    except Exception as e:
        print(f"❌ OpenD 连接异常: {e}")
        return False

def check_event_loop():
    """检查事件循环"""
    print("\n🔍 检查事件循环状态...")
    
    try:
        loop = asyncio.get_event_loop()
        print(f"✅ 当前线程有事件循环: {loop}")
        return True
    except RuntimeError as e:
        print(f"⚠️ 当前线程没有事件循环: {e}")
        
        # 尝试创建新的事件循环
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            print(f"✅ 已创建新事件循环: {loop}")
            return True
        except Exception as e2:
            print(f"❌ 创建事件循环失败: {e2}")
            return False

def test_futu_import():
    """测试 futu 模块导入"""
    print("\n🔍 测试 futu 模块导入...")
    
    # 设置环境变量（必须在导入 futu 之前）
    os.environ["FUTU_LOG_LEVEL"] = "ERROR"
    os.environ["FUTU_LOG_TO_FILE"] = "0"
    os.environ["FUTU_LOG_PATH"] = "/tmp/futu_logs"
    
    try:
        # 确保目录存在
        os.makedirs("/tmp/futu_logs", exist_ok=True)
        
        # 在导入前配置日志
        import logging
        futu_logger = logging.getLogger("futu")
        futu_logger.setLevel(logging.CRITICAL)
        futu_logger.handlers = []
        futu_logger.propagate = False
        
        # 导入 futu
        from futu import OpenQuoteContext
        
        print("✅ futu 模块导入成功")
        
        # 测试连接
        print("\n🔍 测试 OpenQuoteContext 连接...")
        try:
            # 创建事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # 测试连接
            quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
            
            # 获取市场状态
            ret, data = quote_ctx.get_global_state()
            if ret == 0:
                print(f"✅ OpenQuoteContext 连接成功")
                print(f"   市场状态: {data}")
            else:
                print(f"❌ OpenQuoteContext 连接失败: ret={ret}")
            
            quote_ctx.close()
            return True
            
        except Exception as e:
            print(f"❌ OpenQuoteContext 连接异常: {e}")
            return False
            
    except ImportError as e:
        print(f"❌ 无法导入 futu 模块: {e}")
        print("   请检查是否安装了 futu-api: pip install futu-api")
        return False
    except Exception as e:
        print(f"❌ futu 模块导入异常: {e}")
        return False

def check_python_version():
    """检查 Python 版本"""
    print("\n🔍 检查 Python 版本...")
    print(f"Python 版本: {sys.version}")
    print(f"Python 路径: {sys.executable}")
    
    # 检查 asyncio 版本
    import asyncio
    print(f"asyncio 版本: {asyncio.__version__ if hasattr(asyncio, '__version__') else '未知'}")

def create_fixed_main():
    """创建修复后的主程序入口"""
    print("\n🔧 创建修复后的主程序入口...")
    
    fixed_code = '''#!/usr/bin/env python3
"""
FTSeek 修复版主入口
修复异步事件循环问题
"""
import sys
import os
import asyncio
import threading
import signal
import logging
import time

# 设置环境变量（必须在导入 futu 之前）
os.environ["FUTU_LOG_LEVEL"] = "ERROR"
os.environ["FUTU_LOG_TO_FILE"] = "0"
os.environ["FUTU_LOG_PATH"] = "/tmp/futu_logs"

# 确保 app 包可被 import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 创建临时日志目录
try:
    os.makedirs("/tmp/futu_logs", exist_ok=True)
except:
    pass

# 配置 futu 日志
import logging as stdlib_logging
futu_logger = stdlib_logging.getLogger("futu")
futu_logger.setLevel(stdlib_logging.CRITICAL)
futu_logger.handlers = []
futu_logger.propagate = False

def ensure_event_loop():
    """确保当前线程有事件循环"""
    try:
        loop = asyncio.get_event_loop()
        print(f"✅ 当前线程已有事件循环: {loop}")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        print(f"✅ 已创建新事件循环: {loop}")
    return loop

def main():
    """主函数"""
    print("🚀 FTSeek 修复版启动...")
    
    # 确保事件循环
    loop = ensure_event_loop()
    
    # 导入配置
    from app import config
    
    # 配置日志
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_FILE)
        ]
    )
    
    logger = logging.getLogger("ftseek.main")
    
    try:
        # 导入其他模块
        from app.strategy import STRATEGY_REGISTRY
        from app.strategy.strategy_manager import strategy_manager
        from app.notifier.dingtalk import DingTalkNotifier
        from app.trader.executor import TradeExecutor
        from app.data_subscriber import DataSubscriber
        from app.server import app as fastapi_app, set_executor, set_notifier, set_strategy_manager
        from app.risk import get_risk_manager, init_realtime_monitor
        
        logger.info("模块导入成功")
        
        # 初始化组件
        risk_manager = get_risk_manager()
        notifier = DingTalkNotifier()
        executor = TradeExecutor()
        
        # 注册策略
        for strategy_cls in STRATEGY_REGISTRY.values():
            strategy_manager.register_strategy(strategy_cls())
        
        logger.info(f"已注册 {len(STRATEGY_REGISTRY)} 个策略")
        
        # 设置全局单例
        set_executor(executor)
        set_notifier(notifier)
        set_strategy_manager(strategy_manager)
        
        # 初始化实时监控
        init_realtime_monitor()
        
        # 创建数据订阅器
        strategies = list(strategy_manager.get_strategies().values())
        subscriber = DataSubscriber(strategies, notifier)
        
        # 启动数据订阅
        logger.info("启动数据订阅...")
        subscriber.start()
        
        # 启动 FastAPI 服务器
        import uvicorn
        
        def run_fastapi():
            uvicorn.run(
                fastapi_app,
                host=config.API_HOST,
                port=config.API_PORT,
                log_level="info"
            )
        
        # 在子线程中运行 FastAPI
        api_thread = threading.Thread(target=run_fastapi, daemon=True)
        api_thread.start()
        
        logger.info(f"API 服务器启动: http://{config.API_HOST}:{config.API_PORT}")
        
        # 主循环
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在关闭...")
            subscriber.stop()
            logger.info("FTSeek 已关闭")
            
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
'''
    
    fixed_file = "app/main_fixed.py"
    with open(fixed_file, 'w', encoding='utf-8') as f:
        f.write(fixed_code)
    
    print(f"✅ 修复后的主程序已保存到: {fixed_file}")
    
    # 设置执行权限
    os.chmod(fixed_file, 0o755)
    
    return fixed_file

def main():
    """主函数"""
    print("=" * 60)
    print("FTSeek OpenD 连接修复工具")
    print("=" * 60)
    
    # 检查 Python 版本
    check_python_version()
    
    # 测试 OpenD 连接
    if not test_opend_connection():
        print("\n⚠️ OpenD 连接测试失败，请检查:")
        print("   1. OpenD 是否运行: ps aux | grep FutuOpenD")
        print("   2. 端口是否监听: netstat -tlnp | grep 11111")
        print("   3. 防火墙设置: sudo ufw allow 11111")
        return False
    
    # 检查事件循环
    if not check_event_loop():
        print("\n⚠️ 事件循环检查失败")
        return False
    
    # 测试 futu 导入
    if not test_futu_import():
        print("\n⚠️ futu 模块测试失败")
        return False
    
    # 创建修复后的主程序
    fixed_file = create_fixed_main()
    
    print("\n" + "=" * 60)
    print("✅ 修复完成！")
    print("=" * 60)
    print("\n🎯 下一步操作:")
    print(f"1. 运行修复后的主程序: python {fixed_file}")
    print("2. 或者手动修复原文件: app/main.py")
    print("\n📋 修复内容:")
    print("   • 确保事件循环在导入 futu 前已创建")
    print("   • 提前配置 futu 日志环境")
    print("   • 修复异步线程安全问题")
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)