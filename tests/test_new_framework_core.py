"""
新框架核心模块测试

测试范围：
1. config_loader - 统一配置加载器
2. trading_env - 交易环境抽象
3. stages - 三阶段抽象
4. container - DI 容器
5. event_bus - 事件总线
"""

import pytest
import os
import tempfile
import time
from unittest.mock import patch, MagicMock
from pathlib import Path


# ============================================================
# 1. ConfigLoader 测试
# ============================================================

class TestConfigLoader:
    """统一配置加载器测试"""
    
    def test_basic_get(self):
        """测试基本配置获取"""
        from app.core.config_loader import cfg
        
        # 测试点路径访问
        project_name = cfg.get("base.project_name")
        assert project_name is not None, "base.project_name 不应为 None"
        
        # 测试默认值
        missing_key = cfg.get("nonexistent.key", default="fallback")
        assert missing_key == "fallback"
    
    def test_section_get(self):
        """测试整段配置获取"""
        from app.core.config_loader import cfg
        
        stop_loss = cfg.get_section("stop_loss")
        assert isinstance(stop_loss, dict)
        assert "default_percent" in stop_loss
    
    def test_env_var_resolution(self):
        """测试环境变量解析"""
        from app.core.config_loader import ConfigLoader
        
        # 创建临时配置文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("""
test:
  host: ${TEST_HOST:localhost}
  port: ${TEST_PORT:8080}
""")
            f.flush()
            
            loader = ConfigLoader(f.name)
            assert loader.get("test.host") == "localhost"
            assert loader.get("test.port") == "8080"
            
            # 清理
            os.unlink(f.name)
    
    def test_singleton(self):
        """测试单例模式"""
        from app.core.config_loader import ConfigLoader
        
        instance1 = ConfigLoader.get_instance()
        instance2 = ConfigLoader.get_instance()
        assert instance1 is instance2


# ============================================================
# 2. TradingEnvironment 测试
# ============================================================

class TestTradingEnvironment:
    """交易环境抽象测试"""
    
    def test_simulate_mode(self):
        """测试模拟模式"""
        from app.core.trading_env import TradingEnvironment, TradingMode
        
        env = TradingEnvironment(mode=TradingMode.SIMULATE)
        
        assert env.is_simulate == True
        assert env.is_real == False
        assert env.mode == TradingMode.SIMULATE
    
    def test_real_mode(self):
        """测试实盘模式"""
        from app.core.trading_env import TradingEnvironment, TradingMode
        
        env = TradingEnvironment(mode=TradingMode.REAL)
        
        assert env.is_simulate == False
        assert env.is_real == True
        assert env.mode == TradingMode.REAL
    
    def test_capabilities(self):
        """测试环境能力差异"""
        from app.core.trading_env import TradingEnvironment, TradingMode
        
        sim_env = TradingEnvironment(mode=TradingMode.SIMULATE)
        real_env = TradingEnvironment(mode=TradingMode.REAL)
        
        # 模拟盘允许满仓
        assert sim_env.capabilities.max_positions_multiplier == 1.0
        # 实盘限制 50%
        assert real_env.capabilities.max_positions_multiplier == 0.5
        
        # 模拟盘跳过 Futu 验证
        assert sim_env.capabilities.skip_futu_position_check == True
        assert real_env.capabilities.skip_futu_position_check == False
    
    def test_max_positions(self):
        """测试最大持仓计算"""
        from app.core.trading_env import TradingEnvironment, TradingMode
        
        sim_env = TradingEnvironment(mode=TradingMode.SIMULATE)
        real_env = TradingEnvironment(mode=TradingMode.REAL)
        
        # 模拟盘：满仓
        assert sim_env.get_max_positions(10, 5) == 10
        # 实盘：50%
        assert real_env.get_max_positions(10, 5) == 5
    
    def test_notification_prefix(self):
        """测试通知前缀"""
        from app.core.trading_env import TradingEnvironment, TradingMode
        
        sim_env = TradingEnvironment(mode=TradingMode.SIMULATE)
        real_env = TradingEnvironment(mode=TradingMode.REAL)
        
        assert sim_env.get_notification_prefix() == "[SIM]"
        assert real_env.get_notification_prefix() == ""


# ============================================================
# 3. Stages 测试
# ============================================================

class TestStages:
    """三阶段抽象测试"""
    
    def test_trading_stage_enum(self):
        """测试阶段枚举"""
        from app.core.stages import TradingStage
        
        assert TradingStage.PRE_MARKET.value == "pre_market"
        assert TradingStage.INTRADAY.value == "intraday"
        assert TradingStage.POST_MARKET.value == "post_market"
        assert TradingStage.CLOSED.value == "closed"
    
    def test_battle_plan(self):
        """测试盘前计划"""
        from app.core.stages import BattlePlan
        
        plan = BattlePlan(
            date="2026-04-28",
            targets=[
                {"symbol": "HK.00700", "direction": "ANY", "status": "VALID"},
                {"symbol": "HK.09988", "direction": "BUY", "status": "VALID"},
            ],
            restricted=["HK.00005"],
        )
        
        assert plan.is_allowed("HK.00700") == True
        assert plan.is_allowed("HK.09988") == True
        assert plan.is_allowed("HK.00005") == False
        assert plan.is_allowed("HK.99999") == False
    
    def test_stage_manager(self):
        """测试阶段管理器"""
        from app.core.stages import StageManager, TradingStage
        
        manager = StageManager()
        assert manager.current_stage == TradingStage.CLOSED


# ============================================================
# 4. Container 测试
# ============================================================

class TestContainer:
    """DI 容器测试"""
    
    def test_basic_register_resolve(self):
        """测试基本注册和解析"""
        from app.core.container import Container
        
        container = Container()
        
        class TestService:
            def __init__(self):
                self.name = "test"
        
        container.register("test_service", TestService)
        service = container.resolve("test_service")
        
        assert isinstance(service, TestService)
        assert service.name == "test"
    
    def test_dependency_injection(self):
        """测试依赖注入"""
        from app.core.container import Container
        
        container = Container()
        
        class ConfigService:
            def __init__(self):
                self.value = "config"
        
        class DependentService:
            def __init__(self, config_service=None):
                self.config = config_service
        
        container.register("config_service", ConfigService)
        container.register("dependent", DependentService, depends=["config_service"])
        
        dep = container.resolve("dependent")
        assert isinstance(dep.config, ConfigService)
    
    def test_circular_dependency_detection(self):
        """测试循环依赖检测"""
        from app.core.container import Container, CircularDependencyError
        
        container = Container()
        
        container.register("a", lambda: None, depends=["b"])
        container.register("b", lambda: None, depends=["a"])
        
        with pytest.raises(CircularDependencyError):
            container.resolve("a")
    
    def test_singleton(self):
        """测试单例模式"""
        from app.core.container import Container
        
        container = Container()
        
        call_count = 0
        def factory():
            nonlocal call_count
            call_count += 1
            return {"count": call_count}
        
        container.register("counter", factory)
        
        obj1 = container.resolve("counter")
        obj2 = container.resolve("counter")
        
        assert obj1 is obj2
        assert call_count == 1


# ============================================================
# 5. EventBus 测试
# ============================================================

class TestEventBus:
    """事件总线测试"""
    
    def test_basic_publish_subscribe(self):
        """测试基本发布订阅"""
        from app.core.event_bus import EventBus
        
        received = []
        
        def handler(event):
            received.append(event.payload)
        
        EventBus.subscribe("test.event", handler)
        EventBus.publish("test.event", {"data": "test"})
        
        assert len(received) == 1
        assert received[0] == {"data": "test"}
        
        # 清理
        EventBus.clear()
    
    def test_multiple_handlers(self):
        """测试多个处理器"""
        from app.core.event_bus import EventBus
        
        results = []
        
        def handler1(event):
            results.append("h1")
        
        def handler2(event):
            results.append("h2")
        
        EventBus.subscribe("multi.event", handler1)
        EventBus.subscribe("multi.event", handler2)
        EventBus.publish("multi.event", {})
        
        assert "h1" in results
        assert "h2" in results
        
        EventBus.clear()
    
    def test_handler_exception_isolation(self):
        """测试处理器异常隔离"""
        from app.core.event_bus import EventBus
        
        results = []
        
        def failing_handler(event):
            raise ValueError("test error")
        
        def normal_handler(event):
            results.append("ok")
        
        EventBus.subscribe("error.event", failing_handler)
        EventBus.subscribe("error.event", normal_handler)
        
        errors = EventBus.publish("error.event", {})
        
        # 正常处理器仍然执行
        assert "ok" in results
        # 错误被捕获
        assert len(errors) == 1
        
        EventBus.clear()
    
    def test_event_constants(self):
        """测试事件常量"""
        from app.core.event_bus import TradeEvent, SystemEvent
        
        assert hasattr(TradeEvent, "POSITION_CLOSED")
        assert hasattr(TradeEvent, "ORDER_FILLED")
        assert hasattr(SystemEvent, "SERVICE_STARTED")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
