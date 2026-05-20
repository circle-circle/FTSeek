# Phase 3: 执行引擎实现计划

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal**: 实现完整的执行引擎，包括智能订单路由、订单状态机、幂等机制、富途券商适配器

**Architecture**: 基于 engine.py 基类，实现 ExecutionEngine 接口，包含 smart_router/order_state_machine/idempotency/broker_adapter 模块

**Tech Stack**: Python 3.8+, pytest, SQLite, Futu OpenD API

**预计时间**: 2-3 天  
**任务数**: 12 个

---

## Task 1: 订单状态机

**Files:**
- Create: `app/execution/order_state_machine.py`
- Test: `tests/execution/test_order_state_machine.py`

**Step 1: Write the failing test**
```python
# tests/execution/test_order_state_machine.py
import pytest
from app.execution.order_state_machine import OrderStateMachine
from app.core.models import OrderState

def test_order_state_transitions():
    """测试订单状态转换"""
    machine = OrderStateMachine()
    
    # 初始状态
    assert machine.current_state == OrderState.CREATED
    
    # CREATED → VALIDATED
    machine.transition(OrderState.VALIDATED)
    assert machine.current_state == OrderState.VALIDATED
    
    # VALIDATED → SENT
    machine.transition(OrderState.SENT)
    assert machine.current_state == OrderState.SENT
    
    # SENT → ACKED
    machine.transition(OrderState.ACKED)
    assert machine.current_state == OrderState.ACKED
    
    # ACKED → FILLED
    machine.transition(OrderState.FILLED)
    assert machine.current_state == OrderState.FILLED
    assert machine.is_complete() == True

def test_invalid_state_transition():
    """测试无效状态转换"""
    machine = OrderStateMachine()
    
    # CREATED 不能直接到 FILLED
    with pytest.raises(ValueError, match="Invalid transition"):
        machine.transition(OrderState.FILLED)
```

**Step 2: Run test — confirm it fails**
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
pytest tests/execution/test_order_state_machine.py::test_order_state_transitions -v
```
Expected: FAIL — "ModuleNotFoundError: No module named 'app.execution.order_state_machine'"

**Step 3: Write minimal implementation**
```python
# app/execution/order_state_machine.py
from typing import Set, Dict, List
from app.core.models import OrderState

class OrderStateMachine:
    """订单状态机"""
    
    # 定义有效的状态转换
    VALID_TRANSITIONS: Dict[OrderState, Set[OrderState]] = {
        OrderState.CREATED: {OrderState.VALIDATED, OrderState.CANCELED},
        OrderState.VALIDATED: {OrderState.SENT, OrderState.CANCELED},
        OrderState.SENT: {OrderState.ACKED, OrderState.REJECTED, OrderState.CANCELED},
        OrderState.ACKED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED},
        OrderState.PARTIALLY_FILLED: {OrderState.FILLED, OrderState.CANCELED},
        OrderState.FILLED: set(),  # 终态
        OrderState.CANCELED: set(),  # 终态
        OrderState.REJECTED: set(),  # 终态
        OrderState.EXPIRED: set(),  # 终态
        OrderState.UNKNOWN: set(),  # 终态
    }
    
    def __init__(self):
        self.current_state = OrderState.CREATED
        self.history: List[OrderState] = [OrderState.CREATED]
    
    def transition(self, new_state: OrderState):
        """转换到新状态"""
        valid_next_states = self.VALID_TRANSITIONS.get(self.current_state, set())
        
        if new_state not in valid_next_states:
            raise ValueError(
                f"Invalid transition from {self.current_state.value} to {new_state.value}. "
                f"Valid transitions: {[s.value for s in valid_next_states]}"
            )
        
        self.current_state = new_state
        self.history.append(new_state)
    
    def is_complete(self) -> bool:
        """订单是否已完成"""
        return self.current_state in {
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
    
    def is_active(self) -> bool:
        """订单是否仍活跃"""
        return self.current_state in {
            OrderState.CREATED,
            OrderState.VALIDATED,
            OrderState.SENT,
            OrderState.ACKED,
            OrderState.PARTIALLY_FILLED,
        }
    
    def reset(self):
        """重置状态机"""
        self.current_state = OrderState.CREATED
        self.history = [OrderState.CREATED]
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/execution/test_order_state_machine.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/execution/order_state_machine.py tests/execution/test_order_state_machine.py
git commit -m "feat(execution): 实现订单状态机 (Task 1/12)"
```

---

## Task 2: 幂等机制

**Files:**
- Create: `app/execution/idempotency.py`
- Test: `tests/execution/test_idempotency.py`

**Step 1: Write the failing test**
```python
# tests/execution/test_idempotency.py
import pytest
from datetime import datetime, timedelta
from app.execution.idempotency import IdempotencyManager

def test_idempotency_prevents_duplicate():
    """测试幂等机制防止重复下单"""
    manager = IdempotencyManager(window_seconds=3600)
    
    client_order_id = "FTUS_AAPL_1234567890"
    
    # 第一次记录
    assert manager.is_duplicate(client_order_id) == False
    manager.record_order(client_order_id, "order_1")
    
    # 第二次检查应该是重复
    assert manager.is_duplicate(client_order_id) == True

def test_idempotency_window_expires():
    """测试幂等窗口过期"""
    manager = IdempotencyManager(window_seconds=1)  # 1 秒窗口 (测试用)
    
    client_order_id = "FTUS_TEST_1"
    manager.record_order(client_order_id, "order_1")
    
    import time
    time.sleep(1.1)
    
    # 过期后应该不再是重复
    assert manager.is_duplicate(client_order_id) == False
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/execution/test_idempotency.py::test_idempotency_prevents_duplicate -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/execution/idempotency.py
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

class IdempotencyManager:
    """幂等管理器"""
    
    def __init__(self, window_seconds: int = 3600):
        self.window = timedelta(seconds=window_seconds)
        self.orders: Dict[str, Tuple[str, datetime]] = {}  # client_order_id -> (order_id, timestamp)
    
    def is_duplicate(self, client_order_id: str) -> bool:
        """检查是否是重复订单"""
        if client_order_id not in self.orders:
            return False
        
        _, recorded_at = self.orders[client_order_id]
        
        # 检查是否在窗口期内
        if datetime.now() - recorded_at > self.window:
            # 过期，清理
            del self.orders[client_order_id]
            return False
        
        return True
    
    def record_order(self, client_order_id: str, order_id: str):
        """记录订单"""
        self.orders[client_order_id] = (order_id, datetime.now())
    
    def get_order_id(self, client_order_id: str) -> Optional[str]:
        """获取已记录的订单 ID"""
        if client_order_id in self.orders:
            order_id, recorded_at = self.orders[client_order_id]
            if datetime.now() - recorded_at <= self.window:
                return order_id
        return None
    
    def cleanup_expired(self):
        """清理过期记录"""
        now = datetime.now()
        expired = [
            cid for cid, (_, recorded_at) in self.orders.items()
            if now - recorded_at > self.window
        ]
        for cid in expired:
            del self.orders[cid]
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/execution/test_idempotency.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/execution/idempotency.py tests/execution/test_idempotency.py
git commit -m "feat(execution): 实现幂等机制 (Task 2/12)"
```

---

## Task 3: 订单类型选择器

**Files:**
- Create: `app/execution/order_type_selector.py`
- Test: `tests/execution/test_order_type_selector.py`

**Step 1: Write the failing test**
```python
# tests/execution/test_order_type_selector.py
import pytest
from app.execution.order_type_selector import OrderTypeSelector
from app.core.models import SignalScore, Direction, OrderType

def test_select_market_order_for_high_strength():
    """测试高评分信号选择市价单"""
    selector = OrderTypeSelector()
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=85,  # > 80
        confidence=0.9,
    )
    
    order_type = selector.select(signal, urgency="HIGH")
    assert order_type == OrderType.MARKET

def test_select_limit_order_for_low_strength():
    """测试低评分信号选择限价单"""
    selector = OrderTypeSelector()
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=55,  # < 60
        confidence=0.6,
    )
    
    order_type = selector.select(signal)
    assert order_type == OrderType.LIMIT
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/execution/test_order_type_selector.py::test_select_market_order_for_high_strength -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/execution/order_type_selector.py
from typing import Optional
from app.core.models import SignalScore, OrderType

class OrderTypeSelector:
    """订单类型选择器"""
    
    def __init__(
        self,
        market_order_min_score: int = 80,
        limit_order_max_score: int = 60,
        twap_order_min_value: float = 100000,
    ):
        self.market_min_score = market_order_min_score
        self.limit_max_score = limit_order_max_score
        self.twap_min_value = twap_order_min_value
    
    def select(
        self,
        signal: SignalScore,
        urgency: str = "NORMAL",
        order_value: Optional[float] = None,
    ) -> OrderType:
        """选择订单类型"""
        # 大单使用 TWAP
        if order_value and order_value > self.twap_min_value:
            return OrderType.TWAP
        
        # 高紧急度 + 高评分 → 市价单
        if urgency == "HIGH" and signal.strength >= self.market_min_score:
            return OrderType.MARKET
        
        # 高评分 → 市价单
        if signal.strength >= self.market_min_score:
            return OrderType.MARKET
        
        # 低评分 → 限价单
        if signal.strength <= self.limit_max_score:
            return OrderType.LIMIT
        
        # 中等评分 → IOC (立即成交或取消)
        return OrderType.IOC
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/execution/test_order_type_selector.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/execution/order_type_selector.py tests/execution/test_order_type_selector.py
git commit -m "feat(execution): 实现订单类型选择器 (Task 3/12)"
```

---

## Task 4: 智能订单路由器

**Files:**
- Create: `app/execution/smart_router.py`
- Test: `tests/execution/test_smart_router.py`

**Step 1: Write the failing test**
```python
# tests/execution/test_smart_router.py
import pytest
from app.execution.smart_router import SmartRouter
from app.core.models import Order, Direction, OrderType

def test_smart_router_selects_broker():
    """测试智能路由器选择券商"""
    router = SmartRouter()
    
    # 添加模拟券商
    router.add_broker("futuu", priority=1, available=True)
    router.add_broker("ibkr", priority=2, available=True)
    
    # 应该选择优先级最高的可用券商
    broker = router.select_broker("AAPL")
    assert broker == "futuu"

def test_smart_router_fallback():
    """测试路由器故障转移"""
    router = SmartRouter()
    
    router.add_broker("futuu", priority=1, available=False)  # 不可用
    router.add_broker("ibkr", priority=2, available=True)
    
    broker = router.select_broker("AAPL")
    assert broker == "ibkr"
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/execution/test_smart_router.py::test_smart_router_selects_broker -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/execution/smart_router.py
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

@dataclass
class BrokerInfo:
    name: str
    priority: int
    available: bool
    latency_ms: float = 0.0
    fee_rate: float = 0.0

class SmartRouter:
    """智能订单路由器"""
    
    def __init__(self):
        self.brokers: Dict[str, BrokerInfo] = {}
    
    def add_broker(self, name: str, priority: int, available: bool, **kwargs):
        """添加券商"""
        self.brokers[name] = BrokerInfo(
            name=name,
            priority=priority,
            available=available,
            **kwargs
        )
    
    def select_broker(self, symbol: str) -> Optional[str]:
        """选择最佳券商"""
        available_brokers = [
            (name, info) for name, info in self.brokers.items()
            if info.available
        ]
        
        if not available_brokers:
            return None
        
        # 按优先级排序
        available_brokers.sort(key=lambda x: x[1].priority)
        
        return available_brokers[0][0]
    
    def mark_unavailable(self, broker_name: str):
        """标记券商不可用"""
        if broker_name in self.brokers:
            self.brokers[broker_name].available = False
    
    def mark_available(self, broker_name: str):
        """标记券商可用"""
        if broker_name in self.brokers:
            self.brokers[broker_name].available = True
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/execution/test_smart_router.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/execution/smart_router.py tests/execution/test_smart_router.py
git commit -m "feat(execution): 实现智能订单路由器 (Task 4/12)"
```

---

## Task 5: 订单重试机制

**Files:**
- Create: `app/execution/order_retry.py`
- Test: `tests/execution/test_order_retry.py`

**Step 1: Write the failing test**
```python
# tests/execution/test_order_retry.py
import pytest
from app.execution.order_retry import OrderRetryManager

def test_retry_manager_allows_retry():
    """测试重试管理器允许重试"""
    manager = OrderRetryManager(max_attempts=3, delay_seconds=5)
    
    order_id = "order_1"
    
    # 前 3 次应该允许重试
    assert manager.should_retry(order_id) == True
    manager.record_attempt(order_id)
    
    assert manager.should_retry(order_id) == True
    manager.record_attempt(order_id)
    
    assert manager.should_retry(order_id) == True
    manager.record_attempt(order_id)
    
    # 第 4 次应该拒绝
    assert manager.should_retry(order_id) == False

def test_retry_manager_get_delay():
    """测试获取重试延迟"""
    manager = OrderRetryManager(max_attempts=3, delay_seconds=5)
    
    order_id = "order_1"
    manager.record_attempt(order_id)
    
    # 第一次重试延迟 5 秒
    assert manager.get_delay(order_id) == 5
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/execution/test_order_retry.py::test_retry_manager_allows_retry -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/execution/order_retry.py
from typing import Dict
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class RetryState:
    attempts: int = 0
    last_attempt: datetime = field(default_factory=datetime.now)

class OrderRetryManager:
    """订单重试管理器"""
    
    def __init__(self, max_attempts: int = 3, delay_seconds: int = 5):
        self.max_attempts = max_attempts
        self.delay_seconds = delay_seconds
        self.states: Dict[str, RetryState] = {}
    
    def should_retry(self, order_id: str) -> bool:
        """是否应该重试"""
        if order_id not in self.states:
            return True
        
        state = self.states[order_id]
        return state.attempts < self.max_attempts
    
    def record_attempt(self, order_id: str):
        """记录重试尝试"""
        if order_id not in self.states:
            self.states[order_id] = RetryState()
        
        self.states[order_id].attempts += 1
        self.states[order_id].last_attempt = datetime.now()
    
    def get_delay(self, order_id: str) -> int:
        """获取重试延迟 (秒)"""
        return self.delay_seconds
    
    def get_attempts(self, order_id: str) -> int:
        """获取已重试次数"""
        if order_id not in self.states:
            return 0
        return self.states[order_id].attempts
    
    def reset(self, order_id: str):
        """重置重试状态"""
        if order_id in self.states:
            del self.states[order_id]
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/execution/test_order_retry.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/execution/order_retry.py tests/execution/test_order_retry.py
git commit -m "feat(execution): 实现订单重试机制 (Task 5/12)"
```

---

## Task 6: 滑点控制

**Files:**
- Create: `app/execution/slippage_control.py`
- Test: `tests/execution/test_slippage_control.py`

**Step 1: Write the failing test**
```python
# tests/execution/test_slippage_control.py
import pytest
from app.execution.slippage_control import SlippageController

def test_slippage_within_tolerance():
    """测试滑点在容忍范围内"""
    controller = SlippageController(max_slippage_ratio=0.005)  # 0.5%
    
    expected_price = 100.0
    actual_price = 100.3  # 0.3% 滑点
    
    assert controller.is_acceptable(expected_price, actual_price, "BUY") == True

def test_slippage_exceeds_tolerance():
    """测试滑点超出容忍范围"""
    controller = SlippageController(max_slippage_ratio=0.005)
    
    expected_price = 100.0
    actual_price = 101.0  # 1% 滑点
    
    assert controller.is_acceptable(expected_price, actual_price, "BUY") == False
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/execution/test_slippage_control.py::test_slippage_within_tolerance -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/execution/slippage_control.py
class SlippageController:
    """滑点控制器"""
    
    def __init__(self, max_slippage_ratio: float = 0.005):
        self.max_slippage = max_slippage_ratio
    
    def is_acceptable(
        self,
        expected_price: float,
        actual_price: float,
        side: str,  # "BUY" or "SELL"
    ) -> bool:
        """检查滑点是否可接受"""
        if side == "BUY":
            slippage = (actual_price - expected_price) / expected_price
        else:  # SELL
            slippage = (expected_price - actual_price) / expected_price
        
        return slippage <= self.max_slippage
    
    def calculate_slippage(
        self,
        expected_price: float,
        actual_price: float,
        side: str,
    ) -> float:
        """计算滑点比例"""
        if side == "BUY":
            return (actual_price - expected_price) / expected_price
        else:
            return (expected_price - actual_price) / expected_price
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/execution/test_slippage_control.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/execution/slippage_control.py tests/execution/test_slippage_control.py
git commit -m "feat(execution): 实现滑点控制 (Task 6/12)"
```

---

## Task 7: 富途券商适配器 (基础)

**Files:**
- Create: `app/execution/broker_adapter.py`
- Test: `tests/execution/test_broker_adapter.py`

**Step 1: Write the failing test**
```python
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
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/execution/test_broker_adapter.py::test_futu_adapter_initialization -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/execution/broker_adapter.py
from typing import Optional, Dict, Any
from app.core.models import Order, OrderState

class FutuBrokerAdapter:
    """富途券商适配器"""
    
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        trade_env: int = 1,  # 0=真实，1=模拟
    ):
        self.host = host
        self.port = port
        self.trade_env = trade_env
        self.connected = False
        self.quote_ctx = None
        self.trading_ctx = None
    
    def connect(self) -> bool:
        """连接到富途 OpenD"""
        try:
            # TODO: 实际实现需要导入 futu API
            # from futu import OpenQuoteContext, OpenTradingContext
            # self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
            # self.trading_ctx = OpenTradingContext(...)
            self.connected = True
            return True
        except Exception as e:
            print(f"连接失败：{e}")
            return False
    
    def disconnect(self):
        """断开连接"""
        if self.quote_ctx:
            self.quote_ctx.close()
        if self.trading_ctx:
            self.trading_ctx.close()
        self.connected = False
    
    def is_connected(self) -> bool:
        """检查连接状态"""
        return self.connected
    
    def submit_order(self, order: Order) -> Dict[str, Any]:
        """提交订单"""
        if not self.connected:
            raise RuntimeError("未连接到券商")
        
        # TODO: 实际实现
        return {
            "order_id": order.order_id,
            "status": "submitted",
        }
    
    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        if not self.connected:
            raise RuntimeError("未连接到券商")
        
        # TODO: 实际实现
        return True
    
    def query_order(self, order_id: str) -> Optional[Order]:
        """查询订单状态"""
        if not self.connected:
            raise RuntimeError("未连接到券商")
        
        # TODO: 实际实现
        return None
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/execution/test_broker_adapter.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/execution/broker_adapter.py tests/execution/test_broker_adapter.py
git commit -m "feat(execution): 实现富途券商适配器基础 (Task 7/12)"
```

---

## Task 8-12: ExecutionEngine 主类 + 集成测试

*(由于篇幅限制，Task 8-12 的详细代码略，遵循相同模式)*

**Task 8**: 创建 `app/execution/engine.py` - ExecutionEngine 主类  
**Task 9**: 集成订单状态机、幂等机制、智能路由  
**Task 10**: 集成重试机制、滑点控制、券商适配器  
**Task 11**: 配置集成  
**Task 12**: 端到端测试

---

## 完成验证

所有任务完成后运行：

```bash
# 运行所有执行引擎测试
pytest tests/execution/ -v --tb=short

# 检查测试覆盖率
pytest tests/execution/ --cov=app/execution --cov-report=term-missing

# 验证模块导入
python -c "from app.execution.engine import ExecutionEngine; print('✅ ExecutionEngine 导入成功')"
```

**预期结果**:
- ✅ 12/12 任务完成
- ✅ 所有测试通过
- ✅ 测试覆盖率 > 80%
- ✅ 模块导入无错误

---

*计划文档版本：1.0 | 创建日期：2026-03-24*
