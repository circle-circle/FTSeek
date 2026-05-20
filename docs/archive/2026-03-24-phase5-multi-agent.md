# Phase 5: 多 Agent 集成实现计划

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal**: 实现多 Agent 协作系统，包括 Main/Decision/Risk/Execution/Observability 五个 Agent 角色，完成完整协作流程

**Architecture**: 基于 OpenClaw sessions_spawn，实现 agent_coordinator 统一调度，每个引擎对应一个 Agent 角色

**Tech Stack**: Python 3.8+, OpenClaw sessions_spawn, pytest

**预计时间**: 1-2 天  
**任务数**: 6 个

---

## Task 1: Agent 角色定义与配置

**Files:**
- Create: `app/agents/agent_config.py`
- Test: `tests/agents/test_agent_config.py`

**Step 1: Write the failing test**
```python
# tests/agents/test_agent_config.py
import pytest
from app.agents.agent_config import AgentRole, AgentConfig

def test_agent_role_definition():
    """测试 Agent 角色定义"""
    assert AgentRole.MAIN.value == "main"
    assert AgentRole.DECISION.value == "decision"
    assert AgentRole.RISK.value == "risk"
    assert AgentRole.EXECUTION.value == "execution"
    assert AgentRole.OBSERVABILITY.value == "observability"

def test_agent_config_initialization():
    """测试 Agent 配置初始化"""
    config = AgentConfig.get_config(AgentRole.MAIN)
    
    assert config.role == AgentRole.MAIN
    assert config.model != ""
    assert config.description != ""
```

**Step 2: Run test — confirm it fails**
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
pytest tests/agents/test_agent_config.py::test_agent_role_definition -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/agent_config.py
from enum import Enum
from dataclasses import dataclass
from typing import Dict

class AgentRole(str, Enum):
    """Agent 角色定义"""
    MAIN = "main"
    DECISION = "decision"
    RISK = "risk"
    EXECUTION = "execution"
    OBSERVABILITY = "observability"

@dataclass
class AgentConfig:
    """Agent 配置"""
    role: AgentRole
    model: str
    description: str
    thinking: str = "high"
    timeout_seconds: int = 300
    
    @classmethod
    def get_config(cls, role: AgentRole) -> "AgentConfig":
        """获取 Agent 配置"""
        configs: Dict[AgentRole, "AgentConfig"] = {
            AgentRole.MAIN: AgentConfig(
                role=AgentRole.MAIN,
                model="bailian/qwen3.5-plus",
                description="总协调器，接收信号→分发到各引擎",
                thinking="high",
                timeout_seconds=300,
            ),
            AgentRole.DECISION: AgentConfig(
                role=AgentRole.DECISION,
                model="deepseek/deepseek-chat",
                description="信号生成 + 评分 + 市场状态识别",
                thinking="high",
                timeout_seconds=300,
            ),
            AgentRole.RISK: AgentConfig(
                role=AgentRole.RISK,
                model="bailian/glm-5",
                description="风控检查 + 熔断判定 + 退出决策",
                thinking="high",
                timeout_seconds=300,
            ),
            AgentRole.EXECUTION: AgentConfig(
                role=AgentRole.EXECUTION,
                model="bailian/qwen3.5-plus",
                description="订单路由 + 执行优化 + 状态追踪",
                thinking="medium",
                timeout_seconds=300,
            ),
            AgentRole.OBSERVABILITY: AgentConfig(
                role=AgentRole.OBSERVABILITY,
                model="bailian/qwen3.5-plus",
                description="监控告警 + 失败分析 + 审计回放",
                thinking="medium",
                timeout_seconds=300,
            ),
        }
        return configs.get(role, configs[AgentRole.MAIN])
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/test_agent_config.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/agent_config.py tests/agents/test_agent_config.py
git commit -m "feat(agents): 定义 Agent 角色与配置 (Task 1/6)"
```

---

## Task 2: Agent 消息传递协议

**Files:**
- Create: `app/agents/message_protocol.py`
- Test: `tests/agents/test_message_protocol.py`

**Step 1: Write the failing test**
```python
# tests/agents/test_message_protocol.py
import pytest
from app.agents.message_protocol import AgentMessage, MessageType

def test_agent_message_creation():
    """测试 Agent 消息创建"""
    msg = AgentMessage(
        msg_type=MessageType.SIGNAL,
        from_agent="decision",
        to_agent="risk",
        payload={"signal_id": "123", "strength": 75}
    )
    
    assert msg.msg_type == MessageType.SIGNAL
    assert msg.from_agent == "decision"
    assert msg.payload["signal_id"] == "123"

def test_message_serialization():
    """测试消息序列化"""
    msg = AgentMessage(
        msg_type=MessageType.ORDER,
        from_agent="risk",
        to_agent="execution",
        payload={"order_id": "456"}
    )
    
    serialized = msg.to_dict()
    assert "msg_type" in serialized
    assert "payload" in serialized
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/agents/test_message_protocol.py::test_agent_message_creation -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/message_protocol.py
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional
import uuid

class MessageType(str, Enum):
    """消息类型"""
    SIGNAL = "signal"           # 决策→风险：信号
    RISK_CHECK = "risk_check"   # 风险→执行：风控通过
    ORDER = "order"             # 执行→观测：订单
    ALERT = "alert"             # 观测→所有：告警
    STATUS = "status"           # 状态更新
    ERROR = "error"             # 错误消息

@dataclass
class AgentMessage:
    """Agent 消息"""
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    msg_type: MessageType = MessageType.STATUS
    from_agent: str = ""
    to_agent: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    correlation_id: Optional[str] = None  # 关联消息 ID
    
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type.value,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentMessage":
        """从字典反序列化"""
        return cls(
            msg_id=data.get("msg_id", str(uuid.uuid4())),
            msg_type=MessageType(data.get("msg_type", "status")),
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            payload=data.get("payload", {}),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
            correlation_id=data.get("correlation_id"),
        )
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/test_message_protocol.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/message_protocol.py tests/agents/test_message_protocol.py
git commit -m "feat(agents): 实现 Agent 消息传递协议 (Task 2/6)"
```

---

## Task 3: Agent 协调器

**Files:**
- Create: `app/agents/coordinator.py`
- Test: `tests/agents/test_coordinator.py`

**Step 1: Write the failing test**
```python
# tests/agents/test_coordinator.py
import pytest
from app.agents.coordinator import AgentCoordinator

def test_coordinator_initialization():
    """测试协调器初始化"""
    coordinator = AgentCoordinator()
    
    assert coordinator is not None
    assert hasattr(coordinator, 'dispatch_to_agent')

def test_coordinator_dispatch_workflow():
    """测试协调器分发工作流"""
    coordinator = AgentCoordinator()
    
    # 模拟工作流：decision → risk → execution
    workflow = ["decision", "risk", "execution"]
    
    # 验证工作流定义
    assert workflow == coordinator.get_default_workflow()
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/agents/test_coordinator.py::test_coordinator_initialization -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/coordinator.py
from typing import List, Dict, Any, Optional
from .agent_config import AgentRole, AgentConfig
from .message_protocol import AgentMessage, MessageType

class AgentCoordinator:
    """Agent 协调器"""
    
    def __init__(self):
        self.workflow: List[str] = [
            AgentRole.DECISION.value,
            AgentRole.RISK.value,
            AgentRole.EXECUTION.value,
            AgentRole.OBSERVABILITY.value,
        ]
        self.message_history: List[AgentMessage] = []
    
    def get_default_workflow(self) -> List[str]:
        """获取默认工作流"""
        return self.workflow.copy()
    
    def dispatch_to_agent(
        self,
        role: AgentRole,
        task: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> AgentMessage:
        """
        分发任务到 Agent
        
        实际实现需要调用 OpenClaw sessions_spawn
        这里提供接口定义
        """
        config = AgentConfig.get_config(role)
        
        message = AgentMessage(
            msg_type=MessageType.STATUS,
            from_agent="coordinator",
            to_agent=role.value,
            payload={
                "task": task,
                "context": context or {},
                "model": config.model,
            }
        )
        
        self.message_history.append(message)
        return message
    
    def process_signal(self, symbol: str, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理信号完整工作流
        
        1. Decision: 生成信号评分
        2. Risk: 预交易风控检查
        3. Execution: 执行订单
        4. Observability: 记录事件
        """
        result = {
            "symbol": symbol,
            "stages": {},
        }
        
        # Stage 1: Decision
        decision_msg = self.dispatch_to_agent(
            AgentRole.DECISION,
            f"为 {symbol} 生成信号评分",
            {"market_data": market_data}
        )
        result["stages"]["decision"] = decision_msg.to_dict()
        
        # Stage 2: Risk
        risk_msg = self.dispatch_to_agent(
            AgentRole.RISK,
            f"检查 {symbol} 的风控",
            {"signal": decision_msg.payload}
        )
        result["stages"]["risk"] = risk_msg.to_dict()
        
        # Stage 3: Execution
        exec_msg = self.dispatch_to_agent(
            AgentRole.EXECUTION,
            f"执行 {symbol} 订单",
            {"risk_approved": True}
        )
        result["stages"]["execution"] = exec_msg.to_dict()
        
        # Stage 4: Observability
        obs_msg = self.dispatch_to_agent(
            AgentRole.OBSERVABILITY,
            f"记录 {symbol} 交易事件",
            {"order": exec_msg.payload}
        )
        result["stages"]["observability"] = obs_msg.to_dict()
        
        return result
    
    def get_message_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取消息历史"""
        return [msg.to_dict() for msg in self.message_history[-limit:]]
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/test_coordinator.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/coordinator.py tests/agents/test_coordinator.py
git commit -m "feat(agents): 实现 Agent 协调器 (Task 3/6)"
```

---

## Task 4: Decision Agent 实现

**Files:**
- Create: `app/agents/decision_agent.py`
- Test: `tests/agents/test_decision_agent.py`

**Step 1: Write the failing test**
```python
# tests/agents/test_decision_agent.py
import pytest
from app.agents.decision_agent import DecisionAgent

def test_decision_agent_generates_signal():
    """测试决策 Agent 生成信号"""
    agent = DecisionAgent()
    
    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume_ratio": 1.5,
    }
    
    signal = agent.generate_signal(market_data)
    
    assert signal is not None
    assert "signal_id" in signal
    assert "strength" in signal
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/agents/test_decision_agent.py::test_decision_agent_generates_signal -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/decision_agent.py
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import uuid

class DecisionAgent:
    """决策 Agent - 信号生成 + 评分"""
    
    def __init__(self):
        self.signal_history = []
    
    def generate_signal(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """生成信号评分"""
        symbol = market_data.get("symbol", "UNKNOWN")
        
        # 简单评分逻辑 (实际应集成 decision 引擎)
        strength = self._calculate_strength(market_data)
        confidence = self._calculate_confidence(market_data)
        
        signal = {
            "signal_id": str(uuid.uuid4()),
            "symbol": symbol,
            "direction": "LONG" if strength > 50 else "SHORT",
            "strength": strength,
            "confidence": confidence,
            "expected_horizon": 60,
            "market_regime": "RANGE",
            "risk_reward_estimate": 2.0,
            "created_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(minutes=5)).isoformat(),
        }
        
        self.signal_history.append(signal)
        return signal
    
    def _calculate_strength(self, data: Dict[str, Any]) -> int:
        """计算信号强度 (0-100)"""
        base_strength = 50
        
        # 成交量因子
        volume_ratio = data.get("volume_ratio", 1.0)
        if volume_ratio > 2.0:
            base_strength += 20
        elif volume_ratio > 1.5:
            base_strength += 10
        
        # 价格动量因子
        momentum = data.get("momentum", 0)
        base_strength += min(20, max(-20, momentum * 10))
        
        return max(0, min(100, base_strength))
    
    def _calculate_confidence(self, data: Dict[str, Any]) -> float:
        """计算置信度 (0.0-1.0)"""
        base_confidence = 0.5
        
        # 数据完整性
        if "price" in data and "volume" in data:
            base_confidence += 0.2
        
        # 历史数据
        if data.get("history_available", False):
            base_confidence += 0.2
        
        return min(1.0, base_confidence)
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/test_decision_agent.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/decision_agent.py tests/agents/test_decision_agent.py
git commit -m "feat(agents): 实现 Decision Agent (Task 4/6)"
```

---

## Task 5: Risk/Execution/Observability Agent 实现

**Files:**
- Create: `app/agents/risk_agent.py`
- Create: `app/agents/execution_agent.py`
- Create: `app/agents/observability_agent.py`
- Test: `tests/agents/test_agents.py`

**Step 1: Write the failing test**
```python
# tests/agents/test_agents.py
import pytest
from app.agents.risk_agent import RiskAgent
from app.agents.execution_agent import ExecutionAgent
from app.agents.observability_agent import ObservabilityAgent

def test_risk_agent_pre_trade_check():
    """测试风险 Agent 预交易检查"""
    agent = RiskAgent()
    
    signal = {
        "strength": 75,
        "confidence": 0.8,
    }
    
    result = agent.pre_trade_check(signal)
    assert "approved" in result

def test_execution_agent_submit_order():
    """测试执行 Agent 提交订单"""
    agent = ExecutionAgent()
    
    order = {
        "symbol": "AAPL",
        "quantity": 100,
        "side": "BUY",
    }
    
    result = agent.submit_order(order)
    assert "order_id" in result

def test_observability_agent_record_event():
    """测试观测 Agent 记录事件"""
    agent = ObservabilityAgent()
    
    event = {
        "event_type": "order_submitted",
        "symbol": "AAPL",
    }
    
    result = agent.record_event(event)
    assert result["success"] == True
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/agents/test_agents.py -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/risk_agent.py
from typing import Dict, Any

class RiskAgent:
    """风险 Agent - 风控检查 + 熔断判定"""
    
    def __init__(self):
        self.circuit_breakers = {}
    
    def pre_trade_check(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """预交易风控检查"""
        strength = signal.get("strength", 0)
        confidence = signal.get("confidence", 0)
        
        approved = strength >= 50 and confidence >= 0.6
        
        return {
            "approved": approved,
            "reason": "PASS" if approved else "LOW_CONFIDENCE",
            "checks": {
                "strength_check": strength >= 50,
                "confidence_check": confidence >= 0.6,
            }
        }
    
    def check_circuit_breakers(self, symbol: str) -> Dict[str, bool]:
        """检查熔断器"""
        return {
            "triggered": False,
            "symbol": symbol,
        }

# app/agents/execution_agent.py
from typing import Dict, Any
import uuid

class ExecutionAgent:
    """执行 Agent - 订单路由 + 执行"""
    
    def __init__(self):
        self.orders = {}
    
    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """提交订单"""
        order_id = str(uuid.uuid4())
        
        self.orders[order_id] = {
            **order,
            "status": "submitted",
            "order_id": order_id,
        }
        
        return {
            "order_id": order_id,
            "status": "submitted",
            "symbol": order.get("symbol"),
        }
    
    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        if order_id in self.orders:
            self.orders[order_id]["status"] = "canceled"
            return True
        return False

# app/agents/observability_agent.py
from typing import Dict, Any
from datetime import datetime

class ObservabilityAgent:
    """观测 Agent - 监控告警 + 审计"""
    
    def __init__(self):
        self.events = []
    
    def record_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """记录事件"""
        event_record = {
            **event,
            "event_id": f"evt_{len(self.events)}",
            "timestamp": datetime.now().isoformat(),
        }
        
        self.events.append(event_record)
        
        return {
            "success": True,
            "event_id": event_record["event_id"],
        }
    
    def send_alert(self, level: str, title: str, content: str) -> bool:
        """发送告警"""
        # 实际实现应调用 alert_manager
        print(f"[ALERT {level}] {title}: {content}")
        return True
    
    def get_events(self, limit: int = 100) -> list:
        """获取事件历史"""
        return self.events[-limit:]
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/test_agents.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/risk_agent.py app/agents/execution_agent.py app/agents/observability_agent.py tests/agents/test_agents.py
git commit -m "feat(agents): 实现 Risk/Execution/Observability Agent (Task 5/6)"
```

---

## Task 6: 端到端集成测试

**Files:**
- Create: `tests/agents/test_multi_agent_integration.py`

**Step 1: Write integration test**
```python
# tests/agents/test_multi_agent_integration.py
"""多 Agent 端到端集成测试"""
import pytest
from app.agents.coordinator import AgentCoordinator
from app.agents.decision_agent import DecisionAgent
from app.agents.risk_agent import RiskAgent
from app.agents.execution_agent import ExecutionAgent
from app.agents.observability_agent import ObservabilityAgent

def test_multi_agent_full_workflow():
    """测试多 Agent 完整工作流"""
    coordinator = AgentCoordinator()
    
    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume": 1000000,
        "volume_ratio": 1.5,
        "momentum": 0.02,
    }
    
    # 完整工作流
    result = coordinator.process_signal("AAPL", market_data)
    
    # 验证所有阶段都执行了
    assert "decision" in result["stages"]
    assert "risk" in result["stages"]
    assert "execution" in result["stages"]
    assert "observability" in result["stages"]
    
    # 验证消息历史
    history = coordinator.get_message_history()
    assert len(history) >= 4  # 至少 4 条消息
```

**Step 2: Run test — confirm it passes**
```bash
pytest tests/agents/test_multi_agent_integration.py -v
```
Expected: PASS

**Step 3: Commit**
```bash
git add tests/agents/test_multi_agent_integration.py
git commit -m "test(agents): 添加多 Agent 端到端测试 (Task 6/6)"
```

---

## 完成验证

所有任务完成后运行：

```bash
# 运行所有 Agent 测试
pytest tests/agents/ -v --tb=short

# 验证模块导入
python -c "from app.agents.coordinator import AgentCoordinator; c = AgentCoordinator(); print('✅ AgentCoordinator 导入成功')"
```

**预期结果**:
- ✅ 6/6 任务完成
- ✅ 所有测试通过
- ✅ 模块导入无错误

---

*计划文档版本：1.0 | 创建日期：2026-03-24*
