# Phase 6: 多 Agent 投票决策系统实现计划

> **For implementer:** Use TDD throughout. Write failing test first. Watch it fail. Then implement.

**Goal**: 实现多 LLM Agent 并行投票决策系统，支持 DeepSeek/MiniMax/GLM-5/Qwen3.5 四个模型独立评分，加权聚合生成最终决策

**Architecture**: 基于现有 agents 模块，创建 voting 子模块，实现投票聚合器、多模型适配器、一致性检查

**Tech Stack**: Python 3.8+, pytest, OpenClaw sessions_spawn, 多个 LLM API

**预计时间**: 1-2 小时  
**任务数**: 6 个

---

## Task 1: 投票配置与数据模型

**Files:**
- Create: `app/agents/voting/voting_config.py`
- Test: `tests/agents/voting/test_voting_config.py`

**Step 1: Write the failing test**
```python
# tests/agents/voting/test_voting_config.py
import pytest
from app.agents.voting.voting_config import VotingConfig, ModelRole

def test_model_role_definition():
    """测试模型角色定义"""
    assert ModelRole.DEEPSEEK.value == "deepseek"
    assert ModelRole.MINIMAX.value == "minimax"
    assert ModelRole.GLM5.value == "glm5"
    assert ModelRole.QWEN35.value == "qwen35"

def test_voting_config_default_weights():
    """测试投票配置默认权重"""
    config = VotingConfig.get_default_config()
    
    assert config.model_weights[ModelRole.DEEPSEEK] == 0.30
    assert config.model_weights[ModelRole.MINIMAX] == 0.25
    assert config.model_weights[ModelRole.GLM5] == 0.25
    assert config.model_weights[ModelRole.QWEN35] == 0.20

def test_voting_config_model_mapping():
    """测试模型映射"""
    config = VotingConfig.get_default_config()
    
    assert config.model_mapping[ModelRole.DEEPSEEK] == "deepseek/deepseek-chat"
    assert config.model_mapping[ModelRole.GLM5] == "bailian/glm-5"
    assert config.model_mapping[ModelRole.QWEN35] == "bailian/qwen3.5-plus"
```

**Step 2: Run test — confirm it fails**
```bash
cd /opt/ftseek/FTSeek-US
source venv/bin/activate
pytest tests/agents/voting/test_voting_config.py::test_model_role_definition -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/voting/voting_config.py
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List

class ModelRole(str, Enum):
    """模型角色"""
    DEEPSEEK = "deepseek"
    MINIMAX = "minimax"
    GLM5 = "glm5"
    QWEN35 = "qwen35"

@dataclass
class VotingConfig:
    """投票配置"""
    model_weights: Dict[ModelRole, float]
    model_mapping: Dict[ModelRole, str]
    model_descriptions: Dict[ModelRole, str]
    min_consensus_threshold: float = 0.60  # 最小一致性阈值
    max_std_dev: float = 15.0  # 最大标准差 (超过需人工审核)
    
    @classmethod
    def get_default_config(cls) -> "VotingConfig":
        """获取默认配置"""
        return cls(
            model_weights={
                ModelRole.DEEPSEEK: 0.30,
                ModelRole.MINIMAX: 0.25,
                ModelRole.GLM5: 0.25,
                ModelRole.QWEN35: 0.20,
            },
            model_mapping={
                ModelRole.DEEPSEEK: "deepseek/deepseek-chat",
                ModelRole.MINIMAX: "minimax/minimax-chat",
                ModelRole.GLM5: "bailian/glm-5",
                ModelRole.QWEN35: "bailian/qwen3.5-plus",
            },
            model_descriptions={
                ModelRole.DEEPSEEK: "趋势/动量分析",
                ModelRole.MINIMAX: "新闻情绪分析",
                ModelRole.GLM5: "技术指标分析",
                ModelRole.QWEN35: "基本面/估值分析",
            }
        )
    
    def get_enabled_models(self) -> List[ModelRole]:
        """获取启用的模型列表"""
        return [
            role for role, weight in self.model_weights.items()
            if weight > 0
        ]
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/voting/test_voting_config.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/voting/voting_config.py tests/agents/voting/test_voting_config.py
git commit -m "feat(voting): 定义投票配置与数据模型 (Task 1/6)"
```

---

## Task 2: 单模型评分 Agent

**Files:**
- Create: `app/agents/voting/model_agent.py`
- Test: `tests/agents/voting/test_model_agent.py`

**Step 1: Write the failing test**
```python
# tests/agents/voting/test_model_agent.py
import pytest
from app.agents.voting.model_agent import ModelAgent, ModelScore

def test_model_agent_generates_score():
    """测试模型 Agent 生成评分"""
    agent = ModelAgent("deepseek", "deepseek/deepseek-chat")
    
    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume_ratio": 1.5,
        "momentum": 0.02,
    }
    
    score = agent.generate_score(market_data)
    
    assert score is not None
    assert 0 <= score.strength <= 100
    assert 0.0 <= score.confidence <= 1.0
    assert score.model_id == "deepseek"

def test_model_score_serialization():
    """测试模型评分序列化"""
    score = ModelScore(
        model_id="deepseek",
        strength=75,
        confidence=0.8,
        reasoning="趋势强劲，成交量放大"
    )
    
    data = score.to_dict()
    assert "model_id" in data
    assert "strength" in data
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/agents/voting/test_model_agent.py::test_model_agent_generates_score -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/voting/model_agent.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, Optional
import uuid

@dataclass
class ModelScore:
    """模型评分"""
    model_id: str
    strength: int  # 0-100
    confidence: float  # 0.0-1.0
    direction: str = "LONG"
    reasoning: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    score_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "score_id": self.score_id,
            "model_id": self.model_id,
            "strength": self.strength,
            "confidence": self.confidence,
            "direction": self.direction,
            "reasoning": self.reasoning,
            "timestamp": self.timestamp,
        }

class ModelAgent:
    """单模型评分 Agent"""
    
    def __init__(self, role: str, model_name: str, system_prompt: str = ""):
        self.role = role
        self.model_name = model_name
        self.system_prompt = system_prompt or self._default_prompt()
    
    def _default_prompt(self) -> str:
        """默认提示词"""
        return """你是一个专业的股票分析师。请根据提供的市场数据，给出 0-100 的评分。

评分标准:
- 0-30: 强烈看空
- 31-45: 看空
- 46-55: 中性
- 56-70: 看多
- 71-100: 强烈看多

请同时提供:
1. 评分 (0-100)
2. 置信度 (0.0-1.0)
3. 方向 (LONG/SHORT)
4. 理由 (简短说明)"""
    
    def generate_score(self, market_data: Dict[str, Any]) -> ModelScore:
        """
        生成评分
        
        实际实现需要调用 LLM API
        这里提供简化版本
        """
        # 简化实现：基于市场数据计算评分
        strength = self._calculate_strength(market_data)
        confidence = self._calculate_confidence(market_data)
        direction = "LONG" if strength > 50 else "SHORT"
        reasoning = self._generate_reasoning(market_data, strength)
        
        return ModelScore(
            model_id=self.role,
            strength=strength,
            confidence=confidence,
            direction=direction,
            reasoning=reasoning,
        )
    
    def _calculate_strength(self, data: Dict[str, Any]) -> int:
        """计算评分强度"""
        base = 50
        
        # 成交量因子
        volume_ratio = data.get("volume_ratio", 1.0)
        if volume_ratio > 2.0:
            base += 20
        elif volume_ratio > 1.5:
            base += 10
        
        # 动量因子
        momentum = data.get("momentum", 0)
        base += min(20, max(-20, int(momentum * 1000)))
        
        return max(0, min(100, base))
    
    def _calculate_confidence(self, data: Dict[str, Any]) -> float:
        """计算置信度"""
        base = 0.5
        
        if "price" in data and "volume" in data:
            base += 0.2
        if data.get("history_available", False):
            base += 0.2
        
        return min(1.0, base)
    
    def _generate_reasoning(self, data: Dict[str, Any], strength: int) -> str:
        """生成理由"""
        reasons = []
        
        volume_ratio = data.get("volume_ratio", 1.0)
        if volume_ratio > 1.5:
            reasons.append(f"成交量放大{volume_ratio:.1f}倍")
        
        momentum = data.get("momentum", 0)
        if momentum > 0.01:
            reasons.append(f"正向动量{momentum:.2%}")
        elif momentum < -0.01:
            reasons.append(f"负向动量{momentum:.2%}")
        
        return "; ".join(reasons) if reasons else "基于当前市场数据"
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/voting/test_model_agent.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/voting/model_agent.py tests/agents/voting/test_model_agent.py
git commit -m "feat(voting): 实现单模型评分 Agent (Task 2/6)"
```

---

## Task 3: 投票聚合器

**Files:**
- Create: `app/agents/voting/voting_aggregator.py`
- Test: `tests/agents/voting/test_voting_aggregator.py`

**Step 1: Write the failing test**
```python
# tests/agents/voting/test_voting_aggregator.py
import pytest
from app.agents.voting.voting_aggregator import VotingAggregator
from app.agents.voting.model_agent import ModelScore
from app.agents.voting.voting_config import VotingConfig, ModelRole

def test_voting_aggregator_calculates_weighted_average():
    """测试投票聚合器计算加权平均"""
    config = VotingConfig.get_default_config()
    aggregator = VotingAggregator(config)
    
    scores = [
        ModelScore(model_id="deepseek", strength=80, confidence=0.8),
        ModelScore(model_id="minimax", strength=70, confidence=0.7),
        ModelScore(model_id="glm5", strength=75, confidence=0.75),
        ModelScore(model_id="qwen35", strength=65, confidence=0.65),
    ]
    
    result = aggregator.aggregate(scores)
    
    # 加权平均：80*0.3 + 70*0.25 + 75*0.25 + 65*0.2 = 73.25
    assert 73 <= result["final_strength"] <= 74
    assert result["direction"] == "LONG"

def test_voting_aggregator_calculates_consensus():
    """测试一致性计算"""
    config = VotingConfig.get_default_config()
    aggregator = VotingAggregator(config)
    
    # 高度一致的情况
    scores_consensus = [
        ModelScore(model_id="deepseek", strength=75, confidence=0.8),
        ModelScore(model_id="minimax", strength=76, confidence=0.8),
        ModelScore(model_id="glm5", strength=74, confidence=0.8),
        ModelScore(model_id="qwen35", strength=75, confidence=0.8),
    ]
    
    result = aggregator.aggregate(scores_consensus)
    assert result["consensus_level"] > 0.8
    assert result["needs_manual_review"] == False
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/agents/voting/test_voting_aggregator.py::test_voting_aggregator_calculates_weighted_average -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/voting/voting_aggregator.py
from typing import Dict, Any, List
import statistics
from .voting_config import VotingConfig, ModelRole
from .model_agent import ModelScore

class VotingAggregator:
    """投票聚合器"""
    
    def __init__(self, config: VotingConfig):
        self.config = config
    
    def aggregate(self, scores: List[ModelScore]) -> Dict[str, Any]:
        """
        聚合多个模型评分
        
        Returns:
            {
                "final_strength": int,  # 最终评分
                "final_confidence": float,  # 最终置信度
                "direction": str,  # 方向
                "consensus_level": float,  # 一致性水平 (0-1)
                "std_dev": float,  # 标准差
                "needs_manual_review": bool,  # 是否需要人工审核
                "model_scores": List[Dict],  # 各模型评分详情
            }
        """
        if not scores:
            return self._empty_result()
        
        # 1. 计算加权平均
        weighted_sum = 0
        total_weight = 0
        
        for score in scores:
            role = self._get_model_role(score.model_id)
            weight = self.config.model_weights.get(role, 0.25)
            weighted_sum += score.strength * weight
            total_weight += weight
        
        final_strength = int(weighted_sum / total_weight) if total_weight > 0 else 50
        
        # 2. 计算平均置信度
        avg_confidence = sum(s.confidence for s in scores) / len(scores)
        
        # 3. 确定方向
        direction = "LONG" if final_strength > 50 else "SHORT"
        
        # 4. 计算一致性 (标准差)
        strengths = [s.strength for s in scores]
        std_dev = statistics.stdev(strengths) if len(strengths) > 1 else 0
        
        # 一致性水平 (标准差越小，一致性越高)
        consensus_level = max(0, 1 - (std_dev / self.config.max_std_dev))
        
        # 5. 判断是否需要人工审核
        needs_review = (
            std_dev > self.config.max_std_dev or
            consensus_level < self.config.min_consensus_threshold
        )
        
        return {
            "final_strength": final_strength,
            "final_confidence": round(avg_confidence, 2),
            "direction": direction,
            "consensus_level": round(consensus_level, 2),
            "std_dev": round(std_dev, 2),
            "needs_manual_review": needs_review,
            "model_scores": [s.to_dict() for s in scores],
        }
    
    def _get_model_role(self, model_id: str) -> ModelRole:
        """获取模型角色"""
        mapping = {
            "deepseek": ModelRole.DEEPSEEK,
            "minimax": ModelRole.MINIMAX,
            "glm5": ModelRole.GLM5,
            "qwen35": ModelRole.QWEN35,
        }
        return mapping.get(model_id.lower(), ModelRole.DEEPSEEK)
    
    def _empty_result(self) -> Dict[str, Any]:
        """空结果"""
        return {
            "final_strength": 50,
            "final_confidence": 0.0,
            "direction": "NEUTRAL",
            "consensus_level": 0.0,
            "std_dev": 0.0,
            "needs_manual_review": True,
            "model_scores": [],
        }
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/voting/test_voting_aggregator.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/voting/voting_aggregator.py tests/agents/voting/test_voting_aggregator.py
git commit -m "feat(voting): 实现投票聚合器 (Task 3/6)"
```

---

## Task 4: 多 Agent 投票协调器

**Files:**
- Create: `app/agents/voting/voting_coordinator.py`
- Test: `tests/agents/voting/test_voting_coordinator.py`

**Step 1: Write the failing test**
```python
# tests/agents/voting/test_voting_coordinator.py
import pytest
from app.agents.voting.voting_coordinator import VotingCoordinator

def test_voting_coordinator_collects_all_scores():
    """测试投票协调器收集所有模型评分"""
    coordinator = VotingCoordinator()
    
    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume_ratio": 1.5,
        "momentum": 0.02,
    }
    
    result = coordinator.run_voting(market_data)
    
    assert "final_strength" in result
    assert "model_scores" in result
    assert len(result["model_scores"]) == 4  # 4 个模型

def test_voting_coordinator_handles_timeout():
    """测试超时处理"""
    coordinator = VotingCoordinator(timeout_seconds=1)
    
    market_data = {"symbol": "AAPL"}
    
    # 即使有模型超时，也应该返回已有结果
    result = coordinator.run_voting(market_data)
    assert result is not None
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/agents/voting/test_voting_coordinator.py::test_voting_coordinator_collects_all_scores -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/voting/voting_coordinator.py
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import time

from .voting_config import VotingConfig, ModelRole
from .model_agent import ModelAgent, ModelScore
from .voting_aggregator import VotingAggregator

class VotingCoordinator:
    """投票协调器"""
    
    def __init__(
        self,
        config: Optional[VotingConfig] = None,
        timeout_seconds: int = 30,
    ):
        self.config = config or VotingConfig.get_default_config()
        self.timeout = timeout_seconds
        self.aggregator = VotingAggregator(self.config)
        self.model_agents = self._create_model_agents()
    
    def _create_model_agents(self) -> Dict[ModelRole, ModelAgent]:
        """创建模型 Agent"""
        agents = {}
        
        for role, model_name in self.config.model_mapping.items():
            description = self.config.model_descriptions.get(role, "")
            prompt = self._create_role_prompt(role, description)
            agents[role] = ModelAgent(role.value, model_name, prompt)
        
        return agents
    
    def _create_role_prompt(self, role: ModelRole, description: str) -> str:
        """为每个角色创建专用提示词"""
        base_prompt = f"""你是一个专业的股票分析师，专注于{description}。

请根据提供的市场数据，给出 0-100 的评分。

评分标准:
- 0-30: 强烈看空
- 31-45: 看空
- 46-55: 中性
- 56-70: 看多
- 71-100: 强烈看多"""
        
        return base_prompt
    
    def run_voting(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        运行投票决策流程
        
        1. 并行调用所有模型 Agent
        2. 收集评分
        3. 聚合结果
        """
        scores = self._collect_scores_parallel(market_data)
        result = self.aggregator.aggregate(scores)
        
        # 添加元数据
        result["symbol"] = market_data.get("symbol", "UNKNOWN")
        result["timestamp"] = time.time()
        
        return result
    
    def _collect_scores_parallel(
        self,
        market_data: Dict[str, Any],
    ) -> List[ModelScore]:
        """并行收集所有模型评分"""
        scores = []
        
        with ThreadPoolExecutor(max_workers=4) as executor:
            # 提交所有模型评分任务
            futures = {
                executor.submit(agent.generate_score, market_data): role
                for role, agent in self.model_agents.items()
            }
            
            # 收集结果
            for future in futures:
                try:
                    score = future.result(timeout=self.timeout)
                    scores.append(score)
                except FuturesTimeoutError:
                    # 超时，跳过该模型
                    role = futures[future]
                    print(f"警告：{role.value} 模型评分超时")
                except Exception as e:
                    # 其他错误，跳过该模型
                    role = futures[future]
                    print(f"警告：{role.value} 模型评分失败：{e}")
        
        return scores
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/voting/test_voting_coordinator.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/voting/voting_coordinator.py tests/agents/voting/test_voting_coordinator.py
git commit -m "feat(voting): 实现多 Agent 投票协调器 (Task 4/6)"
```

---

## Task 5: 集成到现有 Decision Agent

**Files:**
- Modify: `app/agents/decision_agent.py`
- Test: `tests/agents/voting/test_decision_integration.py`

**Step 1: Write the failing test**
```python
# tests/agents/voting/test_decision_integration.py
import pytest
from app.agents.decision_agent import DecisionAgent

def test_decision_agent_uses_voting():
    """测试 Decision Agent 使用投票系统"""
    agent = DecisionAgent(use_voting=True)
    
    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume_ratio": 1.5,
        "momentum": 0.02,
    }
    
    signal = agent.generate_signal(market_data)
    
    assert signal is not None
    assert "voting_result" in signal
    assert "final_strength" in signal["voting_result"]
```

**Step 2: Run test — confirm it fails**
```bash
pytest tests/agents/voting/test_decision_integration.py -v
```
Expected: FAIL

**Step 3: Write minimal implementation**
```python
# app/agents/decision_agent.py (修改)
from typing import Dict, Any, Optional
from .voting.voting_coordinator import VotingCoordinator

class DecisionAgent:
    """决策 Agent - 支持单模型或多 Agent 投票"""
    
    def __init__(self, use_voting: bool = True):
        self.use_voting = use_voting
        self.voting_coordinator = VotingCoordinator() if use_voting else None
        self.signal_history = []
    
    def generate_signal(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """生成信号评分"""
        symbol = market_data.get("symbol", "UNKNOWN")
        
        if self.use_voting and self.voting_coordinator:
            # 使用多 Agent 投票
            voting_result = self.voting_coordinator.run_voting(market_data)
            strength = voting_result["final_strength"]
            confidence = voting_result["final_confidence"]
        else:
            # 使用单模型
            voting_result = None
            score = self._generate_single_score(market_data)
            strength = score["strength"]
            confidence = score["confidence"]
        
        direction = "LONG" if strength > 50 else "SHORT"
        
        signal = {
            "signal_id": self._generate_id(),
            "symbol": symbol,
            "direction": direction,
            "strength": strength,
            "confidence": confidence,
            "expected_horizon": 60,
            "market_regime": "RANGE",
            "risk_reward_estimate": 2.0,
            "voting_result": voting_result,  # 投票详情
            "created_at": self._get_timestamp(),
            "expires_at": self._get_timestamp(minutes=5),
        }
        
        self.signal_history.append(signal)
        return signal
    
    def _generate_single_score(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """单模型评分 (向后兼容)"""
        # ... 原有实现
        return {
            "strength": 50,
            "confidence": 0.5,
        }
    
    def _generate_id(self) -> str:
        import uuid
        return str(uuid.uuid4())
    
    def _get_timestamp(self, minutes: int = 0) -> str:
        from datetime import datetime, timedelta
        dt = datetime.now() + timedelta(minutes=minutes)
        return dt.isoformat()
```

**Step 4: Run test — confirm it passes**
```bash
pytest tests/agents/voting/test_decision_integration.py -v
```
Expected: PASS

**Step 5: Commit**
```bash
git add app/agents/decision_agent.py tests/agents/voting/test_decision_integration.py
git commit -m "feat(voting): 集成投票系统到 Decision Agent (Task 5/6)"
```

---

## Task 6: 端到端测试与配置

**Files:**
- Create: `tests/agents/voting/test_voting_e2e.py`
- Create: `config/voting_config.yaml` (可选配置文件)

**Step 1: Write integration test**
```python
# tests/agents/voting/test_voting_e2e.py
"""多 Agent 投票系统端到端测试"""
import pytest
from app.agents.voting.voting_coordinator import VotingCoordinator

def test_voting_system_full_workflow():
    """测试投票系统完整工作流"""
    coordinator = VotingCoordinator()
    
    market_data = {
        "symbol": "AAPL",
        "price": 150.0,
        "volume": 1000000,
        "volume_ratio": 1.5,
        "momentum": 0.02,
        "history_available": True,
    }
    
    result = coordinator.run_voting(market_data)
    
    # 验证结果结构
    assert "final_strength" in result
    assert "final_confidence" in result
    assert "direction" in result
    assert "consensus_level" in result
    assert "model_scores" in result
    
    # 验证有 4 个模型评分
    assert len(result["model_scores"]) == 4
    
    # 验证评分范围
    assert 0 <= result["final_strength"] <= 100
    assert 0.0 <= result["final_confidence"] <= 1.0
    assert 0.0 <= result["consensus_level"] <= 1.0

def test_voting_system_low_consensus():
    """测试低一致性场景"""
    coordinator = VotingCoordinator()
    
    # 模拟分歧很大的情况
    market_data = {
        "symbol": "TSLA",
        "price": 200.0,
        "volume_ratio": 3.0,  # 异常放量
        "momentum": 0.05,
    }
    
    result = coordinator.run_voting(market_data)
    
    # 低一致性应该触发人工审核标记
    # (实际取决于模型输出，这里只验证结构)
    assert "needs_manual_review" in result
```

**Step 2: Run test — confirm it passes**
```bash
pytest tests/agents/voting/test_voting_e2e.py -v
```
Expected: PASS

**Step 3: Commit**
```bash
git add tests/agents/voting/test_voting_e2e.py
git commit -m "test(voting): 添加投票系统端到端测试 (Task 6/6)"
```

---

## 完成验证

所有任务完成后运行：

```bash
# 运行所有投票系统测试
pytest tests/agents/voting/ -v --tb=short

# 验证模块导入
python -c "from app.agents.voting.voting_coordinator import VotingCoordinator; c = VotingCoordinator(); print('✅ VotingCoordinator 导入成功')"
```

**预期结果**:
- ✅ 6/6 任务完成
- ✅ 所有测试通过
- ✅ 模块导入无错误

---

*计划文档版本：1.0 | 创建日期: 2026-03-24*
