# Phase 2: 风险引擎实现计划

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal**: 实现完整的风险引擎，包括四级熔断、三段式风控、组合式止盈止损

**Architecture**: 基于 engine.py 基类，实现 RiskEngine 接口，包含 circuit_breaker/position_limits/drawdown_control/exit_engine 模块

**Tech Stack**: Python 3.8+, pytest, SQLite, FastAPI

**预计时间**: 2-3 天  
**任务数**: 10 个

---

## Task 1: 四级熔断系统 - 策略级熔断

**Files:**
- Create: `app/risk/circuit_breaker.py`
- Test: `tests/risk/test_circuit_breaker.py`

**Step 1: Write the failing test**
```python
# tests/risk/test_circuit_breaker.py
import pytest
from datetime import datetime, timedelta
from app.risk.circuit_breaker import StrategyCircuitBreaker
from app.core.models import SignalScore, Direction

def test_strategy_circuit_breaker_triggers_after_consecutive_losses():
    """测试策略级熔断：连续 5 笔亏损后触发"""
    breaker = StrategyCircuitBreaker(
        consecutive_losses_threshold=5,
        pause_duration_seconds=1800,  # 30 分钟
    )
    
    # 模拟连续 5 笔亏损
    for i in range(5):
        breaker.record_loss("strategy_1")
    
    # 应该触发熔断
    assert breaker.is_triggered("strategy_1") == True
    assert breaker.get_remaining_pause("strategy_1") > 0

def test_strategy_circuit_breaker_resets_after_pause():
    """测试熔断在暂停期后自动重置"""
    breaker = StrategyCircuitBreaker(
        consecutive_losses_threshold=3,
        pause_duration_seconds=60,  # 1 分钟 (测试用)
    )
    
    # 触发熔断
    for i in range(3):
        breaker.record_loss("strategy_1")
    
    assert breaker.is_triggered("strategy_1") == True
    
    # 等待 1 分钟后
    import time
    time.sleep(61)
    
    # 应该自动重置
    assert breaker.is_triggered("strategy_1") == False
```

**Step 2: Run test — confirm it fails**
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
pytest tests/risk/test_circuit_breaker.py::test_strategy_circuit_breaker_triggers_after_consecutive_losses -v
```
Expected: FAIL — "ModuleNotFoundError: No module named 'app.risk.circuit_breaker'"

**Step 3: Write minimal implementation**
```python
# app/risk/circuit_breaker.py
from datetime import datetime, timedelta
from typing import Dict, Optional
from dataclasses import dataclass

@dataclass
class CircuitBreakerState:
    consecutive_losses: int = 0
    last_loss_time: Optional[datetime] = None
    triggered_at: Optional[datetime] = None
    pause_until: Optional[datetime] = None

class StrategyCircuitBreaker:
    """策略级熔断器"""
    
    def __init__(self, consecutive_losses_threshold: int = 5, pause_duration_seconds: int = 1800):
        self.threshold = consecutive_losses_threshold
        self.pause_duration = timedelta(seconds=pause_duration_seconds)
        self.states: Dict[str, CircuitBreakerState] = {}
    
    def record_loss(self, strategy_id: str):
        """记录亏损"""
        if strategy_id not in self.states:
            self.states[strategy_id] = CircuitBreakerState()
        
        state = self.states[strategy_id]
        state.consecutive_losses += 1
        state.last_loss_time = datetime.now()
        
        # 检查是否触发熔断
        if state.consecutive_losses >= self.threshold:
            state.triggered_at = datetime.now()
            state.pause_until = datetime.now() + self.pause_duration
    
    def is_triggered(self, strategy_id: str) -> bool:
        """检查是否触发熔断"""
        if strategy_id not in self.states:
            return False
        
        state = self.states[strategy_id]
        
        if not state.triggered_at:
            return False
        
        # 检查是否已过暂停期
        if state.pause_until and datetime.now() > state.pause_until:
            # 重置状态
            state.consecutive_losses = 0
            state.triggered_at = None
            state.pause_until = None
            return False
        
        return True
    
    def get_remaining_pause(self, strategy_id: str) -> int:
        """获取剩余暂停时间 (秒)"""
        if strategy_id not in self.states:
            return 0
        
        state = self.states[strategy_id]
        if not state.pause_until:
            return 0
        
        remaining = (state.pause_until - datetime.now()).total_seconds()
        return max(0, int(remaining))
    
    def reset(self, strategy_id: str):
        """重置策略状态"""
        if strategy_id in self.states:
            self.states[strategy_id] = CircuitBreakerState()
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/risk/test_circuit_breaker.py::test_strategy_circuit_breaker_triggers_after_consecutive_losses -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/risk/circuit_breaker.py tests/risk/test_circuit_breaker.py
git commit -m "feat(risk): 实现策略级熔断器 (Task 1/10)"
```

---

## Task 2: 品种级熔断 + 系统级熔断

**Files:**
- Modify: `app/risk/circuit_breaker.py`
- Test: `tests/risk/test_circuit_breaker.py`

**Step 1: Write the failing test**
```python
# tests/risk/test_circuit_breaker.py (追加)

def test_symbol_circuit_breaker_on_high_volatility():
    """测试品种级熔断：高波动率触发"""
    from app.risk.circuit_breaker import SymbolCircuitBreaker
    
    breaker = SymbolCircuitBreaker(
        volatility_threshold=0.05,  # 5% 波动率
    )
    
    # 正常波动率
    assert breaker.check_volatility("AAPL", 0.03) == True  # 可交易
    
    # 高波动率
    assert breaker.check_volatility("TSLA", 0.06) == False  # 触发熔断

def test_system_circuit_breaker_on_market_data_down():
    """测试系统级熔断：行情断流"""
    from app.risk.circuit_breaker import SystemCircuitBreaker
    
    breaker = SystemCircuitBreaker(
        market_data_timeout_seconds=60,
    )
    
    # 正常
    breaker.update_market_data_status(True)
    assert breaker.is_triggered() == False
    
    # 模拟断流超过 60 秒
    import time
    time.sleep(61)
    assert breaker.is_triggered() == True
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/risk/test_circuit_breaker.py::test_symbol_circuit_breaker_on_high_volatility -v
```
Expected: FAIL — "ImportError: cannot import name 'SymbolCircuitBreaker'"

**Step 3: Write minimal implementation**
```python
# app/risk/circuit_breaker.py (追加)

class SymbolCircuitBreaker:
    """品种级熔断器"""
    
    def __init__(self, volatility_threshold: float = 0.05):
        self.volatility_threshold = volatility_threshold
        self.frozen_symbols: Dict[str, datetime] = {}
    
    def check_volatility(self, symbol: str, volatility: float) -> bool:
        """检查波动率，返回是否可交易"""
        if volatility > self.volatility_threshold:
            self.frozen_symbols[symbol] = datetime.now()
            return False
        return True
    
    def is_frozen(self, symbol: str) -> bool:
        """检查品种是否被冻结"""
        return symbol in self.frozen_symbols

class SystemCircuitBreaker:
    """系统级熔断器"""
    
    def __init__(self, market_data_timeout_seconds: int = 60):
        self.timeout = timedelta(seconds=market_data_timeout_seconds)
        self.last_market_data_time: Optional[datetime] = None
        self.triggered = False
    
    def update_market_data_status(self, is_alive: bool):
        """更新行情状态"""
        if is_alive:
            self.last_market_data_time = datetime.now()
            self.triggered = False
    
    def is_triggered(self) -> bool:
        """检查是否触发系统熔断"""
        if not self.last_market_data_time:
            return True
        
        if datetime.now() - self.last_market_data_time > self.timeout:
            self.triggered = True
        
        return self.triggered
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/risk/test_circuit_breaker.py -v
```
Expected: PASS (所有熔断测试)

**Step 5: Commit**
```bash
git add app/risk/circuit_breaker.py tests/risk/test_circuit_breaker.py
git commit -m "feat(risk): 实现品种级和系统级熔断器 (Task 2/10)"
```

---

## Task 3: 账户级熔断 - 回撤控制

**Files:**
- Create: `app/risk/drawdown_control.py`
- Test: `tests/risk/test_drawdown_control.py`

**Step 1: Write the failing test**
```python
# tests/risk/test_drawdown_control.py
import pytest
from app.risk.drawdown_control import DrawdownController

def test_daily_drawdown_limit():
    """测试日内回撤限制"""
    controller = DrawdownController(
        max_daily_drawdown=0.03,  # 3%
        max_total_drawdown=0.08,  # 8%
    )
    
    # 初始状态
    assert controller.is_trading_allowed() == True
    
    # 模拟日内回撤 2%
    controller.update_drawdown(daily_dd=0.02, total_dd=0.05)
    assert controller.is_trading_allowed() == True
    
    # 模拟日内回撤 4% (超限)
    controller.update_drawdown(daily_dd=0.04, total_dd=0.05)
    assert controller.is_trading_allowed() == False
    assert controller.get_block_reason() == "DAILY_DRAWDOWN"
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/risk/test_drawdown_control.py::test_daily_drawdown_limit -v
```
Expected: FAIL — "ModuleNotFoundError"

**Step 3: Write minimal implementation**
```python
# app/risk/drawdown_control.py
from datetime import datetime, date
from typing import Optional

class DrawdownController:
    """回撤控制器"""
    
    def __init__(self, max_daily_drawdown: float = 0.03, max_total_drawdown: float = 0.08):
        self.max_daily = max_daily_drawdown
        self.max_total = max_total_drawdown
        
        self.current_daily_dd = 0.0
        self.current_total_dd = 0.0
        self.last_reset_date: Optional[date] = None
        self.block_reason: Optional[str] = None
    
    def update_drawdown(self, daily_dd: float, total_dd: float):
        """更新回撤数据"""
        # 检查是否需要重置日内回撤 (新的一天)
        today = date.today()
        if self.last_reset_date != today:
            self.current_daily_dd = 0.0
            self.last_reset_date = today
        
        self.current_daily_dd = daily_dd
        self.current_total_dd = total_dd
        
        # 检查是否超限
        self._check_limits()
    
    def _check_limits(self):
        """检查回撤限制"""
        self.block_reason = None
        
        if self.current_daily_dd > self.max_daily:
            self.block_reason = "DAILY_DRAWDOWN"
        elif self.current_total_dd > self.max_total:
            self.block_reason = "TOTAL_DRAWDOWN"
    
    def is_trading_allowed(self) -> bool:
        """检查是否允许交易"""
        return self.block_reason is None
    
    def get_block_reason(self) -> Optional[str]:
        """获取阻止原因"""
        return self.block_reason
    
    def reset(self):
        """重置控制器"""
        self.current_daily_dd = 0.0
        self.current_total_dd = 0.0
        self.block_reason = None
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/risk/test_drawdown_control.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/risk/drawdown_control.py tests/risk/test_drawdown_control.py
git commit -m "feat(risk): 实现回撤控制器 (Task 3/10)"
```

---

## Task 4: 仓位限制检查器

**Files:**
- Create: `app/risk/position_limits.py`
- Test: `tests/risk/test_position_limits.py`

**Step 1: Write the failing test**
```python
# tests/risk/test_position_limits.py
import pytest
from app.risk.position_limits import PositionLimitsChecker

def test_single_position_limit():
    """测试单品种仓位限制"""
    checker = PositionLimitsChecker(
        max_position_per_symbol=0.25,  # 25%
        max_position_per_direction=0.50,  # 50%
        max_total_position=0.80,  # 80%
    )
    
    # 正常仓位
    assert checker.check_position("AAPL", 0.20, 0.60, 0.60) == (True, None)
    
    # 单品种超限
    result, reason = checker.check_position("AAPL", 0.30, 0.60, 0.60)
    assert result == False
    assert reason == "SINGLE_SYMBOL_LIMIT"
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/risk/test_position_limits.py::test_single_position_limit -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/risk/position_limits.py
from typing import Tuple, Optional, Dict

class PositionLimitsChecker:
    """仓位限制检查器"""
    
    def __init__(
        self,
        max_position_per_symbol: float = 0.25,
        max_position_per_direction: float = 0.50,
        max_total_position: float = 0.80,
    ):
        self.max_symbol = max_position_per_symbol
        self.max_direction = max_position_per_direction
        self.max_total = max_total_position
    
    def check_position(
        self,
        symbol: str,
        proposed_position: float,
        current_direction_exposure: float,
        current_total_exposure: float,
    ) -> Tuple[bool, Optional[str]]:
        """
        检查仓位是否超限
        
        Returns:
            (是否允许，拒绝原因)
        """
        # 单品种限制
        if proposed_position > self.max_symbol:
            return False, "SINGLE_SYMBOL_LIMIT"
        
        # 单方向限制
        if current_direction_exposure + proposed_position > self.max_direction:
            return False, "DIRECTION_LIMIT"
        
        # 总仓位限制
        if current_total_exposure + proposed_position > self.max_total:
            return False, "TOTAL_POSITION_LIMIT"
        
        return True, None
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/risk/test_position_limits.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/risk/position_limits.py tests/risk/test_position_limits.py
git commit -m "feat(risk): 实现仓位限制检查器 (Task 4/10)"
```

---

## Task 5: 预交易风控检查器

**Files:**
- Create: `app/risk/pre_trade_check.py`
- Test: `tests/risk/test_pre_trade_check.py`

**Step 1: Write the failing test**
```python
# tests/risk/test_pre_trade_check.py
import pytest
from app.risk.pre_trade_check import PreTradeChecker
from app.core.models import SignalScore, Direction

def test_pre_trade_check_allows_good_signal():
    """测试预交易检查：好信号应该通过"""
    checker = PreTradeChecker()
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=75,
        confidence=0.8,
    )
    
    result = checker.check(signal)
    assert result.allowed == True
    assert result.reason is None

def test_pre_trade_check_rejects_low_confidence():
    """测试预交易检查：低置信度应该拒绝"""
    checker = PreTradeChecker(min_confidence=0.60)
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=50,
        confidence=0.40,  # 低于阈值
    )
    
    result = checker.check(signal)
    assert result.allowed == False
    assert "LOW_CONFIDENCE" in result.reason
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/risk/test_pre_trade_check.py::test_pre_trade_check_allows_good_signal -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/risk/pre_trade_check.py
from dataclasses import dataclass
from typing import Optional, List
from app.core.models import SignalScore

@dataclass
class PreTradeResult:
    allowed: bool
    reason: Optional[str]
    checks_passed: List[str]

class PreTradeChecker:
    """预交易风控检查器"""
    
    def __init__(
        self,
        min_confidence: float = 0.60,
        min_signal_strength: int = 50,
    ):
        self.min_confidence = min_confidence
        self.min_strength = min_signal_strength
    
    def check(self, signal: SignalScore) -> PreTradeResult:
        """执行预交易检查"""
        checks_passed = []
        
        # 1. 信号强度检查
        if signal.strength < self.min_strength:
            return PreTradeResult(
                allowed=False,
                reason="SIGNAL_TOO_WEAK",
                checks_passed=checks_passed,
            )
        checks_passed.append("SIGNAL_STRENGTH")
        
        # 2. 置信度检查
        if signal.confidence < self.min_confidence:
            return PreTradeResult(
                allowed=False,
                reason="LOW_CONFIDENCE",
                checks_passed=checks_passed,
            )
        checks_passed.append("CONFIDENCE")
        
        # 3. 信号未过期
        if signal.is_expired():
            return PreTradeResult(
                allowed=False,
                reason="SIGNAL_EXPIRED",
                checks_passed=checks_passed,
            )
        checks_passed.append("NOT_EXPIRED")
        
        # 所有检查通过
        return PreTradeResult(
            allowed=True,
            reason=None,
            checks_passed=checks_passed,
        )
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/risk/test_pre_trade_check.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/risk/pre_trade_check.py tests/risk/test_pre_trade_check.py
git commit -m "feat(risk): 实现预交易风控检查器 (Task 5/10)"
```

---

## Task 6: 组合式止盈止损配置

**Files:**
- Create: `app/risk/exit_config.py`
- Test: `tests/risk/test_exit_config.py`

**Step 1: Write the failing test**
```python
# tests/risk/test_exit_config.py
import pytest
from app.risk.exit_config import ExitConfig

def test_exit_config_default_values():
    """测试退出配置默认值"""
    config = ExitConfig()
    
    assert config.stop_loss == -0.06  # -6%
    assert config.take_profit_1 == 0.08  # +8%
    assert config.take_profit_2 == 0.15  # +15%
    assert config.take_profit_3 == 0.25  # +25%
    assert config.trailing_stop_atr_mult == 2.0
    assert config.time_stop_hours == 72

def test_exit_config_get_levels():
    """测试获取退出价位"""
    config = ExitConfig()
    entry_price = 100.0
    
    levels = config.get_exit_levels(entry_price)
    
    assert levels["stop_loss"] == 94.0  # 100 * (1 - 0.06)
    assert levels["take_profit_1"] == 108.0
    assert levels["take_profit_2"] == 115.0
    assert levels["take_profit_3"] == 125.0
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/risk/test_exit_config.py::test_exit_config_default_values -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/risk/exit_config.py
from dataclasses import dataclass
from typing import Dict

@dataclass
class ExitConfig:
    """组合式止盈止损配置"""
    
    # 固定止损
    stop_loss: float = -0.06  # -6%
    
    # 分批止盈 (价格目标 + 平仓比例)
    take_profit_1: float = 0.08   # +8%
    take_profit_ratio_1: float = 0.30  # 平 30%
    
    take_profit_2: float = 0.15   # +15%
    take_profit_ratio_2: float = 0.30  # 平 30%
    
    take_profit_3: float = 0.25   # +25%
    take_profit_ratio_3: float = 0.40  # 平 40%
    
    # 移动止损
    trailing_stop_atr_mult: float = 2.0  # ATR 倍数
    
    # 时间止损
    time_stop_hours: int = 72
    
    def get_exit_levels(self, entry_price: float) -> Dict[str, float]:
        """获取退出价位"""
        return {
            "stop_loss": entry_price * (1 + self.stop_loss),
            "take_profit_1": entry_price * (1 + self.take_profit_1),
            "take_profit_2": entry_price * (1 + self.take_profit_2),
            "take_profit_3": entry_price * (1 + self.take_profit_3),
        }
    
    def get_exit_ratios(self) -> Dict[str, float]:
        """获取平仓比例"""
        return {
            "take_profit_1": self.take_profit_ratio_1,
            "take_profit_2": self.take_profit_ratio_2,
            "take_profit_3": self.take_profit_ratio_3,
        }
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/risk/test_exit_config.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/risk/exit_config.py tests/risk/test_exit_config.py
git commit -m "feat(risk): 实现组合式止盈止损配置 (Task 6/10)"
```

---

## Task 7: 退出引擎 - 判断退出条件

**Files:**
- Create: `app/risk/exit_engine.py`
- Test: `tests/risk/test_exit_engine.py`

**Step 1: Write the failing test**
```python
# tests/risk/test_exit_engine.py
import pytest
from datetime import datetime, timedelta
from app.risk.exit_engine import ExitEngine, ExitReason
from app.core.models import Position, Direction

def test_exit_engine_triggers_stop_loss():
    """测试退出引擎：触发止损"""
    engine = ExitEngine()
    
    position = Position(
        symbol="AAPL",
        direction=Direction.LONG,
        avg_entry_price=100.0,
        size=100,
        initial_sl=94.0,  # -6%
    )
    
    # 当前价格跌破止损
    should_exit, reason = engine.should_exit(position, current_price=93.0)
    
    assert should_exit == True
    assert reason == ExitReason.STOP_LOSS

def test_exit_engine_triggers_take_profit():
    """测试退出引擎：触发止盈"""
    engine = ExitEngine()
    
    position = Position(
        symbol="AAPL",
        direction=Direction.LONG,
        avg_entry_price=100.0,
        size=100,
        initial_tp=108.0,  # +8%
    )
    
    # 当前价格突破止盈
    should_exit, reason = engine.should_exit(position, current_price=110.0)
    
    assert should_exit == True
    assert reason == ExitReason.TAKE_PROFIT
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/risk/test_exit_engine.py::test_exit_engine_triggers_stop_loss -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/risk/exit_engine.py
from enum import Enum
from typing import Tuple, Optional, List
from app.core.models import Position, ExitReason

class ExitEngine:
    """退出引擎"""
    
    def should_exit(
        self,
        position: Position,
        current_price: float,
    ) -> Tuple[bool, Optional[ExitReason]]:
        """
        判断是否应该退出
        
        Returns:
            (是否退出，退出原因)
        """
        # 优先级 1: 硬止损 (必须立即执行)
        if self._check_stop_loss(position, current_price):
            return True, ExitReason.STOP_LOSS
        
        # 优先级 2: 止盈目标
        if self._check_take_profit(position, current_price):
            return True, ExitReason.TAKE_PROFIT
        
        # 优先级 3: 时间止损
        if self._check_time_stop(position):
            return True, ExitReason.TIME_STOP
        
        # 不退出
        return False, None
    
    def _check_stop_loss(self, position: Position, price: float) -> bool:
        """检查止损"""
        if position.direction == Direction.LONG:
            return price <= position.initial_sl
        else:  # SHORT
            return price >= position.initial_sl
    
    def _check_take_profit(self, position: Position, price: float) -> bool:
        """检查止盈"""
        if position.direction == Direction.LONG:
            return price >= position.initial_tp
        else:  # SHORT
            return price <= position.initial_tp
    
    def _check_time_stop(self, position: Position) -> bool:
        """检查时间止损"""
        from datetime import datetime
        holding_hours = (datetime.now() - position.entry_time).total_seconds() / 3600
        return holding_hours >= position.time_stop_hours
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/risk/test_exit_engine.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/risk/exit_engine.py tests/risk/test_exit_engine.py
git commit -m "feat(risk): 实现退出引擎基础逻辑 (Task 7/10)"
```

---

## Task 8: RiskEngine 主类集成

**Files:**
- Create: `app/risk/engine.py`
- Test: `tests/risk/test_risk_engine.py`

**Step 1: Write the failing test**
```python
# tests/risk/test_risk_engine.py
import pytest
from app.risk.engine import RiskEngine
from app.core.events import EventStore
from app.core.models import SignalScore, Direction

def test_risk_engine_initialization():
    """测试风险引擎初始化"""
    event_store = EventStore(":memory:")
    engine = RiskEngine(config={}, event_store=event_store)
    engine.initialize()
    
    assert engine.is_healthy() == True

def test_risk_engine_pre_trade_check():
    """测试风险引擎预交易检查"""
    event_store = EventStore(":memory:")
    engine = RiskEngine(config={}, event_store=event_store)
    engine.initialize()
    
    signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=75,
        confidence=0.8,
    )
    
    result = engine.pre_trade_check(signal)
    assert result == True
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/risk/test_risk_engine.py::test_risk_engine_initialization -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/risk/engine.py
from typing import List, Optional, Dict, Any
from app.core.engine import RiskEngine as BaseRiskEngine
from app.core.events import EventStore
from app.core.models import SignalScore, Order, Position, Alert
from .circuit_breaker import StrategyCircuitBreaker, SymbolCircuitBreaker, SystemCircuitBreaker
from .drawdown_control import DrawdownController
from .position_limits import PositionLimitsChecker
from .pre_trade_check import PreTradeChecker, PreTradeResult
from .exit_engine import ExitEngine

class RiskEngine(BaseRiskEngine):
    """风险引擎主类"""
    
    def initialize(self):
        """初始化风险引擎"""
        # 熔断器
        self.strategy_breaker = StrategyCircuitBreaker()
        self.symbol_breaker = SymbolCircuitBreaker()
        self.system_breaker = SystemCircuitBreaker()
        
        # 回撤控制
        self.drawdown_controller = DrawdownController()
        
        # 仓位限制
        self.position_checker = PositionLimitsChecker()
        
        # 预交易检查
        self.pre_trade_checker = PreTradeChecker()
        
        # 退出引擎
        self.exit_engine = ExitEngine()
        
        self.enabled = True
    
    def shutdown(self):
        """关闭风险引擎"""
        self.enabled = False
    
    def pre_trade_check(
        self,
        signal: SignalScore,
        position: Optional[Position] = None,
    ) -> bool:
        """预交易风控检查"""
        # 1. 检查熔断器
        if self.system_breaker.is_triggered():
            return False
        
        if self.strategy_breaker.is_triggered(signal.strategy_id):
            return False
        
        if self.symbol_breaker.is_frozen(signal.symbol):
            return False
        
        # 2. 预交易检查器
        result = self.pre_trade_checker.check(signal)
        if not result.allowed:
            self._record_rejection(signal, result.reason)
            return False
        
        # 3. 仓位限制 (需要当前持仓数据)
        # TODO: 集成 position_checker
        
        return True
    
    def intra_trade_check(self, positions: List[Position]) -> List[Alert]:
        """持仓中风控检查"""
        alerts = []
        
        for position in positions:
            # TODO: 检查浮盈回撤、波动率上升等
            pass
        
        return alerts
    
    def post_trade_check(self, order: Order, position: Position) -> bool:
        """交易后风控检查"""
        # TODO: 验证成交价、更新风险限额
        return True
    
    def check_circuit_breakers(self) -> Dict[str, bool]:
        """检查熔断器状态"""
        return {
            "strategy": self.strategy_breaker.is_triggered(""),
            "symbol": False,
            "system": self.system_breaker.is_triggered(),
        }
    
    def should_exit(self, position: Position, current_price: float) -> Optional[str]:
        """判断是否应该退出持仓"""
        should_exit, reason = self.exit_engine.should_exit(position, current_price)
        if should_exit:
            return reason.value
        return None
    
    def _record_rejection(self, signal: SignalScore, reason: str):
        """记录风控拒绝"""
        self.event_store.append(
            TradeEvent(
                event_type="risk_blocked",
                signal_id=signal.signal_id,
                data={"reason": reason},
            )
        )
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/risk/test_risk_engine.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/risk/engine.py tests/risk/test_risk_engine.py
git commit -m "feat(risk): 实现 RiskEngine 主类 (Task 8/10)"
```

---

## Task 9: 风险引擎配置集成

**Files:**
- Modify: `config/config_us.py`
- Modify: `app/risk/engine.py`

**Step 1: Add risk config to config_us.py**
```python
# config/config_us.py (已有，验证配置完整性)
RISK_CONFIG = {
    # 账户级风控
    "MAX_DAILY_DRAWDOWN": 0.03,
    "MAX_TOTAL_DRAWDOWN": 0.08,
    
    # 仓位限制
    "MAX_POSITION_PER_SYMBOL": 0.25,
    "MAX_POSITION_PER_DIRECTION": 0.50,
    "MAX_TOTAL_POSITION": 0.80,
    
    # 止损止盈
    "DEFAULT_STOP_LOSS": 0.06,
    "DEFAULT_TAKE_PROFIT_1": 0.08,
    "DEFAULT_TAKE_PROFIT_2": 0.15,
    "DEFAULT_TAKE_PROFIT_3": 0.25,
    
    # 熔断配置
    "CIRCUIT_BREAKER": {
        "strategy": {
            "consecutive_losses": 5,
            "pause_duration": 1800,
        },
        # ...
    }
}
```

**Step 2: Update RiskEngine to use config**
```python
# app/risk/engine.py (update initialize method)
def initialize(self):
    """初始化风险引擎"""
    # 从配置加载参数
    risk_config = self.config.get("risk", {})
    
    # 熔断器
    strategy_cfg = risk_config.get("CIRCUIT_BREAKER", {}).get("strategy", {})
    self.strategy_breaker = StrategyCircuitBreaker(
        consecutive_losses_threshold=strategy_cfg.get("consecutive_losses", 5),
        pause_duration_seconds=strategy_cfg.get("pause_duration", 1800),
    )
    
    # 回撤控制
    self.drawdown_controller = DrawdownController(
        max_daily_drawdown=risk_config.get("MAX_DAILY_DRAWDOWN", 0.03),
        max_total_drawdown=risk_config.get("MAX_TOTAL_DRAWDOWN", 0.08),
    )
    
    # ... (类似更新其他组件)
```

**Step 3: Commit**
```bash
git add config/config_us.py app/risk/engine.py
git commit -m "feat(risk): 集成风险引擎配置 (Task 9/10)"
```

---

## Task 10: 风险引擎端到端测试

**Files:**
- Create: `tests/risk/test_risk_integration.py`

**Step 1: Write integration test**
```python
# tests/risk/test_risk_integration.py
"""风险引擎端到端集成测试"""
import pytest
from app.risk.engine import RiskEngine
from app.core.events import EventStore
from app.core.models import SignalScore, Direction, Position

def test_risk_engine_full_workflow():
    """测试风险引擎完整工作流"""
    event_store = EventStore(":memory:")
    engine = RiskEngine(config={}, event_store=event_store)
    engine.initialize()
    
    # 1. 好信号应该通过
    good_signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=75,
        confidence=0.8,
    )
    assert engine.pre_trade_check(good_signal) == True
    
    # 2. 低置信度信号应该拒绝
    bad_signal = SignalScore(
        symbol="AAPL",
        direction=Direction.LONG,
        strength=50,
        confidence=0.3,
    )
    assert engine.pre_trade_check(bad_signal) == False
    
    # 3. 止损退出判断
    position = Position(
        symbol="AAPL",
        direction=Direction.LONG,
        avg_entry_price=100.0,
        initial_sl=94.0,
    )
    should_exit, reason = engine.should_exit(position, 93.0)
    assert should_exit == True
```

**Step 2: Run test — confirm it passes**
```bash
pytest tests/risk/test_risk_integration.py -v
```
Expected: PASS

**Step 3: Commit**
```bash
git add tests/risk/test_risk_integration.py
git commit -m "test(risk): 添加风险引擎端到端测试 (Task 10/10)"
```

---

## 完成验证

所有任务完成后运行：

```bash
# 运行所有风险引擎测试
pytest tests/risk/ -v --tb=short

# 检查测试覆盖率
pytest tests/risk/ --cov=app/risk --cov-report=term-missing

# 验证模块导入
python -c "from app.risk.engine import RiskEngine; print('✅ RiskEngine 导入成功')"
```

**预期结果**:
- ✅ 10/10 任务完成
- ✅ 所有测试通过
- ✅ 测试覆盖率 > 80%
- ✅ 模块导入无错误

---

*计划文档版本：1.0 | 创建日期：2026-03-24*
