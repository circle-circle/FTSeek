# 策略因子可插拔模块化 + 多模型辩论验证体系

> **日期**: 2026-05-23  
> **范围**: F1 (交易规则) + F3 (架构清晰)  
> **决策级别**: 🔴 (跨文件重构 + 交易主路径)  
> **状态**: DRAFT — 等待审批

---

## 一、问题诊断

### 1.1 当前架构的核心缺陷

| 缺陷 | 现状 | 影响 |
|------|------|------|
| **因子不可插拔** | `app/factors/` 是 8 个独立的 gate 模块（regime_filter、capital_flow 等），没有统一的 Factor 接口 | 无法像 SSS/GDIAN/MFFF 那样自由组合、网格扫描 |
| **因子与策略耦合** | signal_pipeline 硬编码调用因子，新因子需要改 pipeline 代码 | 无法独立回测单个因子的 alpha 贡献 |
| **止损止盈单体化** | exit_policy.yaml 按 regime 分组（normal/trending/volatile），不按策略变体 | 无法为 9 条通过门禁的策略各自配独立 exit profile |
| **验证缺乏闭环** | shadow_tracker + promotion_engine 有框架但未与回测管线联通 | 新策略无法从回测→影子→转正自动化流转 |
| **辩论系统未接入策略优化** | debate 模块仅用于盘前计划/盘后归因 | 策略参数调优依赖人肉判断，无多模型对抗 |

### 1.2 数据基线（来自本次分析）

```
纯股票 296 笔 | 胜率 28.4% | PnL +$13,954 | 利润因子 1.54
主引擎: Main SHORT + TRAILING_STOP = 59.5% WR, +$3,637
出血点: Scalping LONG = 6.5% WR, -$1,601
利润集中度: 12笔(4%)贡献 198% 总利润
近7天: 0 胜（信号严重退化）
```

### 1.3 9 条门禁策略对系统的启示

用户提供的回测分析揭示了三类策略族群：

| 族群 | 核心因子组合 | 对现有系统的映射 |
|------|-------------|----------------|
| 5min 高动量 | SSS=-1, GDIAN=35, MFFF=2.0 | → 需要新建 **超卖深度因子** + **趋势方向因子** + **动量过滤因子** |
| 15min 超卖反转 | SSS∈[-1.5,1], GDIAN≤35, MFFF≥1.0 | → 需要因子可参数化扫描 + 组合回测 |
| 日线深度反转 | SSS=-2, GDIAN≤25, MFFF≥1.5 | → 需要多时间框架因子计算 + 低频交易统计验证 |

**关键发现：当前系统没有 SSS/GDIAN/MFFF 这类量化因子，只有 ad-hoc 的 gate 判断。这是无法做参数扫描和策略进化的根本原因。**

---

## 二、目标架构

```
┌─────────────────────────────────────────────────────────┐
│                  策略辩论验证闭环                          │
│                                                         │
│  ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌───────┐│
│  │ 因子库   │──▶│ 策略组合  │──▶│ 回测引擎  │──▶│ 辩论  ││
│  │(Plug-in)│   │(Factor   │   │(Walk-    │   │(多模型││
│  │         │   │ Assembly)│   │ Forward)  │   │ 对抗) ││
│  └─────────┘   └──────────┘   └──────────┘   └───────┘│
│       ▲              │              │             │     │
│       │              ▼              ▼             ▼     │
│  ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌───────┐│
│  │ 因子注册 │   │ Exit模块  │   │ 统计门禁  │   │ 影子  ││
│  │ Registry│   │(Plug-in) │   │(Bonferroni│   │ 追踪  ││
│  │         │   │          │   │ +MonteCarlo│  │       ││
│  └─────────┘   └──────────┘   └──────────┘   └───────┘│
└─────────────────────────────────────────────────────────┘
```

---

## 三、Phase 规划

### Phase 1: 因子系统重构（Factor Plug-in Framework）

**目标**：建立可插拔、可组合、可参数化的因子库

#### 3.1.1 因子接口定义

```python
# app/factors/base.py (新建)
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from enum import Enum

class FactorTimeframe(Enum):
    TICK = "tick"
    MIN_1 = "1min"
    MIN_5 = "5min"
    MIN_15 = "15min"
    DAILY = "daily"

@dataclass
class FactorValue:
    """因子输出 — 标准化结构"""
    name: str                    # 因子名称 (e.g., "SSS")
    value: float                 # 当前值
    score: float                 # 归一化评分 [-1, 1]
    direction_hint: str          # "LONG" | "SHORT" | "NEUTRAL"
    confidence: float            # 0-1 自评置信度
    metadata: Dict[str, Any]     # 额外信息 (e.g., lookback, params)

@dataclass
class FactorParams:
    """因子参数 — 可扫描"""
    params: Dict[str, Any]       # 参数键值对
    version: str = "1.0"
    description: str = ""

class BaseFactor(ABC):
    """因子基类 — 所有因子必须实现"""
    
    name: str = ""
    timeframe: FactorTimeframe = FactorTimeframe.MIN_5
    version: str = "1.0"
    
    # 参数空间 (用于网格扫描)
    param_space: Dict[str, List[Any]] = {}
    
    def __init__(self, params: Optional[FactorParams] = None):
        self.params = params or FactorParams(params={})
    
    @abstractmethod
    def compute(self, symbol: str, market_data: Dict, ctx: Dict) -> FactorValue:
        """计算因子值 — 纯函数，无副作用"""
        ...
    
    def validate_params(self) -> bool:
        """参数合法性检查"""
        return True
    
    def describe(self) -> str:
        """人类可读的因子描述"""
        return f"{self.name} v{self.version}"
```

#### 3.1.2 因子注册中心

```python
# app/factors/registry.py (新建)
class FactorRegistry:
    """因子注册中心 — 单例"""
    _instance = None
    _factors: Dict[str, Type[BaseFactor]] = {}
    
    def register(self, factor_cls: Type[BaseFactor]):
        """注册因子类"""
        ...
    
    def create(self, name: str, params: FactorParams = None) -> BaseFactor:
        """实例化因子"""
        ...
    
    def scan_params(self, name: str, market_data: Dict) -> List[FactorValue]:
        """网格扫描 — 遍历 param_space 所有组合"""
        ...
    
    def list_available(self) -> List[str]:
        """列出所有已注册因子"""
        ...
```

#### 3.1.3 首批因子实现（对应 9 条门禁策略的核心因子）

| 因子名 | 对应原始 | 计算逻辑 | 参数空间 |
|--------|---------|---------|---------|
| **SSS** (SuperSoldScore) | 超卖深度 | 基于 RSI + Bollinger + 成交量综合评估超卖程度 | `rsi_period: [7,14,21]`, `bb_std: [1.5,2.0,2.5]`, `threshold: [-2,-1.5,-1,0,1]` |
| **GDIAN** (GradientDirectionIndex) | 趋势方向 | MA 斜率 + ADX 综合判断趋势方向强度 | `ma_period: [10,20,50]`, `adx_period: [14]`, `tight_threshold: [20,25,35,45]` |
| **MFFF** (MomentumFlowFilterFactor) | 动量过滤 | MACD + 资金流 + 成交量比综合动量 | `macd_fast: [8,12]`, `macd_slow: [26]`, `volume_weight: [0.3,0.5,0.7]`, `threshold: [0.5,1.0,1.5,2.0]` |
| **VolRegime** | 波动率状态 | ATR percentile + VIX 分位 | `atr_period: [14]`, `lookback: [20,50]` |
| **CapitalFlow** | 资金流 | 复用现有 `capital_flow.py`，适配新接口 | 保持现有参数 |

#### 3.1.4 因子组合器 (Factor Assembly)

```python
# app/factors/assembly.py (新建)
@dataclass
class FactorAssembly:
    """因子组合 — 可序列化为 YAML"""
    name: str                         # "stable_15m_relaxed_oversold_tight_trend"
    factors: List[Dict[str, Any]]     # [{name: "SSS", params: {...}, weight: 0.4}, ...]
    direction_gate: str               # "ALL_LONG" | "ALL_SHORT" | "CONSENSUS" | "ANY"
    min_composite_score: float        # 最低综合评分门槛
    timeframe: str                    # 组合的工作时间框架
    
    # 入场条件 (因子组合的逻辑表达式)
    entry_conditions: List[str]       # ["SSS.score >= -1.5", "GDIAN.value <= 35", "MFFF.score >= 1.0"]
    
    def evaluate(self, factor_values: Dict[str, FactorValue]) -> Tuple[bool, float, str]:
        """评估是否满足入场条件"""
        ...
```

**文件变更：**
- 新建: `app/factors/base.py`, `app/factors/registry.py`, `app/factors/assembly.py`
- 新建: `app/factors/sss.py`, `app/factors/gdian.py`, `app/factors/mfff.py`
- 改造: `app/factors/regime_filter.py` → 适配 BaseFactor 接口
- 改造: `app/factors/capital_flow.py` → 适配 BaseFactor 接口
- 新建: `config/factor_profiles.yaml` — 9 条门禁策略的因子参数配置

---

### Phase 2: 止损止盈可插拔模块化

**目标**：将 exit_policy.yaml 从 monolithic 改造为 per-strategy-variant 的可插拔模块

#### 3.2.1 Exit Profile 接口

```python
# app/exit/base.py (新建)
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class ExitProfile:
    """退出配置剖面 — 可序列化"""
    name: str
    stop_loss_type: str          # "fixed_pct" | "atr_multi" | "chandelier"
    stop_loss_params: Dict       # 具体参数
    take_profit_type: str        # "fixed_pct" | "scale_out" | "trailing"
    take_profit_params: Dict
    time_stop_days: int = 0      # 0 = 无时间止损
    max_holding_minutes: int = 0 # 0 = 无持仓上限
    pre_close_behavior: str      # "force_close" | "reduce" | "hold"

class BaseExitModule(ABC):
    """退出模块基类"""
    name: str = ""
    profile: ExitProfile = None
    
    @abstractmethod
    def evaluate(self, position, ctx) -> ExitDecision:
        """评估是否应该退出"""
        ...
    
    def get_stop_price(self, entry_price: float, atr: float, direction: str) -> float:
        """计算止损价"""
        ...
    
    def get_take_profit_price(self, entry_price: float, atr: float, direction: str) -> float:
        """计算止盈价"""
        ...
```

#### 3.2.2 9 条策略对应的 Exit Profile

```yaml
# config/exit_profiles.yaml (新建)
exit_profiles:
  # 15分钟族群 — 紧密止损 + 适度止盈
  stable_15m_base:
    stop_loss_type: atr_multi
    stop_loss_params:
      multiplier: 1.5
      max_loss_pct: 2.0
    take_profit_type: fixed_pct
    take_profit_params:
      target_pct: 4.0
    max_holding_minutes: 120
    
  stable_15m_relaxed_oversold:
    stop_loss_type: atr_multi
    stop_loss_params:
      multiplier: 1.5
      max_loss_pct: 2.0
    take_profit_type: scale_out
    take_profit_params:
      tiers:
        - trigger_pct: 3.0
          close_pct: 50
        - trigger_pct: 6.0
          close_pct: 50
    max_holding_minutes: 180
    
  # 5分钟高动量
  aggressive_5m_high_momentum:
    stop_loss_type: fixed_pct
    stop_loss_params:
      pct: -1.5
    take_profit_type: trailing
    take_profit_params:
      activation_pct: 2.0
      trail_pct: 1.0
    max_holding_minutes: 30
    
  # 日线深度反转
  radical_daily_deep_oversold:
    stop_loss_type: fixed_pct
    stop_loss_params:
      pct: -7.0          # $7 止损
    take_profit_type: fixed_pct
    take_profit_params:
      target_pct: 14.0    # $14 止盈 (2:1 ratio)
    time_stop_days: 10
    pre_close_behavior: hold
```

#### 3.2.3 Exit 模块注册与装配

```python
# app/exit/registry.py (新建)
class ExitModuleRegistry:
    """退出模块注册中心"""
    _modules: Dict[str, BaseExitModule] = {}
    
    def load_profiles(self, yaml_path: str):
        """从 YAML 加载所有 exit profile"""
        ...
    
    def get_module(self, profile_name: str) -> BaseExitModule:
        """按名称获取退出模块"""
        ...
    
    def assemble(self, factor_assembly: FactorAssembly) -> BaseExitModule:
        """根据因子组合自动匹配最佳 exit profile"""
        ...
```

**文件变更：**
- 新建: `app/exit/base.py`, `app/exit/registry.py`, `app/exit/atr_exit.py`, `app/exit/trailing_exit.py`, `app/exit/scale_out_exit.py`
- 新建: `config/exit_profiles.yaml`
- 改造: `app/strategies/exit_policies.py` → 委托到新的 ExitModuleRegistry
- exit_policy.yaml 保持不变（作为 legacy fallback）

---

### Phase 3: 独立辩论引擎 (Debate Engine)

**设计哲学**: 辩论引擎是独立外挂模块，不绑定任何市场，不限定轮次，不限定触发方式。
它只做一件事：**给定议题 → 多模型充分对抗 → 结构化结论 → 可注入因子/退出模块**。

```
┌──────────────────────────────────────────────────────────┐
│              独立辩论引擎 (debate_engine/)                  │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌────────┐ │
│  │ 议题解析  │  │ 动态轮次  │  │ 知识库    │  │ 结论   │ │
│  │ (Topic   │  │ (收敛驱动│  │ (历史辩论  │  │ 注入   │ │
│  │  Parser) │  │  非固定) │  │  教训积累) │  │ 通道   │ │
│  └──────────┘  └──────────┘  └───────────┘  └────────┘ │
│       ▲              ▲             ▲             │       │
│       │              │             │             ▼       │
│  议题来源:       轮次控制:     自动注入:     注入目标:    │
│  - 回测完成      min=3        历史教训      - Factor     │
│  - 策略退化      max=7        历史结论      - Exit       │
│  - 手动触发      收敛停止     历史分歧      - Filter     │
│  - 定时巡检      分歧终止     反模式        - Strategy   │
│  - 跨市场迁移                                              │
└──────────────────────────────────────────────────────────┘
         ▲                    ▲                    │
         │                    │                    ▼
    ┌────┴─────┐       ┌─────┴────┐        ┌──────────┐
    │ FTSeek   │       │ FTSeek   │        │ 因子/退出 │
    │ (港股)    │       │ -US(美股) │        │ 配置覆盖  │
    └──────────┘       └──────────┘        └──────────┘
```

**跨市场通用**: 议题协议中 `market` 字段标识适用范围 (`US`/`HK`/`BOTH`)，
同一个辩论引擎可同时服务两个项目。港股通过 shared lib import 或 HTTP API 接入。

#### 3.3.1 议题驱动协议 (Topic-Driven Protocol)

不再"Phase C 到了所以辩论"，而是"有一个具体议题需要辩论"。

```python
# debate_engine/topic.py (新建 — 独立于 app/)
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum

class TopicType(Enum):
    FACTOR_PARAM = "factor_param"           # 因子参数调优
    EXIT_PROFILE = "exit_profile"           # 止损止盈调整
    FILTER_ADDITION = "filter_addition"     # 新增过滤条件
    FILTER_REMOVAL = "filter_removal"       # 移除过滤条件
    STRATEGY_VIABILITY = "strategy_viability"  # 策略存废
    CROSS_MARKET = "cross_market"           # 跨市场迁移
    FAILURE_DIAGNOSIS = "failure_diagnosis" # 失败归因
    CUSTOM = "custom"                       # 自定义议题

@dataclass
class DebateTopic:
    """辩议论题 — 驱动整个辩论的结构化输入"""
    topic_id: str
    topic_type: TopicType
    market: str                          # "US" | "HK" | "BOTH"
    title: str                           # 人类可读标题
    description: str                     # 详细背景
    evidence: Dict[str, Any]             # 支撑数据 (回测统计/交易明细/日志)
    affected_components: List[str]       # 受影响的组件 ["factor:SSS", "exit:stable_15m"]
    context: Dict[str, Any] = field(default_factory=dict)  # 额外上下文
    
    # 注入目标声明 — 辩论结论应该注入到哪里
    injection_targets: List[Dict] = field(default_factory=list)
    # 例: [{"target": "factor:SSS", "param": "threshold", "current_value": -1}]
```

#### 3.3.2 动态轮次调度器 (Convergence-Driven Rounds)

**核心改进**: 轮次不硬编码为 3，而是由收敛度驱动。

```python
# debate_engine/rounds.py (新建)
@dataclass
class DebateConfig:
    min_rounds: int = 3              # 最少 3 轮 (保证最低证据收集)
    max_rounds: int = 7              # 最多 7 轮 (防止无限辩论)
    convergence_threshold: float = 0.8  # 共识度 ≥ 80% 提前终止
    divergence_limit: int = 2        # 连续 2 轮分歧扩大则强制终止
    models: List[str] = field(default_factory=lambda: [
        "qwen3.7-max",               # 阿里 Qwen 体系
        "deepseek-v4-pro",            # 深度求索体系
    ])
    # 其他模型 (glm-5/minimax) 按需启用，见 config.yaml
    # Claude Code 作为可选第 4 视角 (代码级深度审计时按需加入)

class DynamicRoundScheduler:
    """
    动态轮次调度器
    
    轮次流程:
    - Round 1: 发散 — 各模型独立分析 (必须执行)
    - Round 2: 交叉质疑 — 互相挑战 (必须执行)
    - Round 3: 初步收敛 — 尝试共识 (必须执行)
    - Round 4+: 深度收敛 — 仅当 Round 3 共识度 < threshold 时继续
    - 终止条件: 共识度达标 OR 达到 max_rounds OR 分歧连续扩大
    """
    
    def __init__(self, config: DebateConfig):
        self.config = config
    
    async def run(self, topic: DebateTopic, knowledge_ctx: Dict) -> DebateResult:
        """执行完整辩论，返回结构化结果"""
        all_rounds = []
        consensus_scores = []
        
        for round_num in range(1, self.config.max_rounds + 1):
            round_result = await self._execute_round(round_num, topic, all_rounds, knowledge_ctx)
            all_rounds.append(round_result)
            
            # Round 3+ 检测收敛
            if round_num >= self.config.min_rounds:
                score = self._compute_consensus_score(round_result)
                consensus_scores.append(score)
                
                # 收敛达标 → 提前终止
                if score >= self.config.convergence_threshold:
                    break
                
                # 分歧扩大 → 强制终止
                if len(consensus_scores) >= self.config.divergence_limit:
                    if all(consensus_scores[-i-1] < consensus_scores[-i-2] 
                           for i in range(self.config.divergence_limit - 1)):
                        round_result.forced_stop = True
                        round_result.stop_reason = "divergence_expanding"
                        break
            
            # 达到上限
            if round_num == self.config.max_rounds:
                round_result.forced_stop = True
                round_result.stop_reason = "max_rounds_reached"
        
        return self._compile_result(topic, all_rounds)
    
    def _compute_consensus_score(self, round_result: RoundResult) -> float:
        """
        计算共识度 (0-1)
        - 所有模型同意的 action items / 总 action items
        - 加权: 高置信度共识权重更高
        """
        ...
    
    async def _execute_round(self, round_num: int, topic: DebateTopic, 
                             previous_rounds: List, knowledge_ctx: Dict) -> RoundResult:
        """执行单轮辩论"""
        ...
```

#### 3.3.3 辩论知识库 (Debate Knowledge Base)

**核心价值**: 每次辩论的结论不是一次性报告，而是持久化的知识资产。
后续辩论自动注入相关历史，避免重复踩坑。

```
debate_engine/
  knowledge/
    debates/              # 完整辩论记录 (JSON)
      debate_20260523_sss_threshold.json
      debate_20260530_exit_15m.json
    lessons/              # 提炼的教训 (人类可读 + 机器可检索)
      lessons_index.json
    outcomes/             # 结论回填 — 注入后的实际效果
      outcomes_index.json
    patterns/             # 跨辩论模式检测
      patterns_index.json
```

```python
# debate_engine/knowledge.py (新建)
@dataclass
class DebateRecord:
    """单次辩论的完整记录"""
    debate_id: str
    topic: DebateTopic
    timestamp: str
    config: DebateConfig
    rounds: List[Dict]                  # 每轮的完整输出
    verdict: 'DebateVerdict'            # 最终结论
    total_rounds: int                   # 实际执行了几轮
    stop_reason: str                    # "converged" | "divergence" | "max_rounds"
    consensus_score: float              # 最终共识度

@dataclass
class Lesson:
    """从辩论中提炼的教训 — 可跨辩论复用"""
    lesson_id: str
    source_debate_id: str               # 来自哪次辩论
    category: str                       # 分类 (见下)
    statement: str                      # 教训陈述
    evidence: str                       # 数据支撑
    confidence: float                   # 初始置信度 (辩论共识度)
    validated: bool = False             # 是否已被实际数据验证
    validation_result: str = ""         # 验证结果描述
    applicable_markets: List[str] = field(default_factory=list)  # ["US", "HK", "BOTH"]
    applicable_factors: List[str] = field(default_factory=list)  # ["SSS", "GDIAN"]
    applicable_exits: List[str] = field(default_factory=list)    # ["stable_15m"]
    created_at: str = ""
    updated_at: str = ""

# 教训分类
LESSON_CATEGORIES = {
    "factor_decay":         "因子失效 — 参数有效性随市场变化衰减",
    "exit_mismatch":        "止损不匹配 — ATR 倍数/止盈层级与策略时间框架不匹配",
    "filter_false_positive":"过滤假阳性 — 过滤条件误杀有效信号",
    "filter_false_negative":"过滤假阴性 — 过滤条件未能拦截噪声",
    "sample_size":          "样本量不足 — 统计结论不可靠",
    "overfitting":          "过拟合 — 参数在历史数据上有效但泛化差",
    "regime_sensitivity":   "市场状态敏感 — 策略在特定 regime 下有效",
    "cross_market_gap":     "跨市场差异 — 因子/策略在不同市场表现不同",
    "execution_reality":    "执行偏差 — 滑点/延迟/流动性导致实际表现偏离回测",
    "concentration_risk":   "集中度风险 — 利润集中在少数交易",
}

@dataclass
class Outcome:
    """结论注入后的实际效果追踪"""
    outcome_id: str
    debate_id: str
    injection_id: str                   # 对应的注入指令
    metric_name: str                    # "win_rate" | "pnl" | "sharpe" | "max_dd"
    value_before: float                 # 注入前的指标值
    value_after: float                  # 注入后的指标值
    observation_days: int               # 观察天数
    observation_trades: int             # 观察期间的交易数
    verdict: str                        # "confirmed" | "refuted" | "inconclusive"
    note: str = ""
    recorded_at: str = ""

class DebateKnowledgeBase:
    """辩论知识库 — 持久化 + 检索 + 学习"""
    
    def __init__(self, knowledge_dir: str = "debate_engine/knowledge"):
        self.knowledge_dir = knowledge_dir
    
    # ── 记录 ──
    
    def save_debate(self, record: DebateRecord):
        """保存完整辩论记录"""
        ...
    
    def extract_lessons(self, record: DebateRecord) -> List[Lesson]:
        """从辩论结论中自动提炼教训"""
        ...
    
    # ── 检索 ──
    
    def find_related_lessons(self, topic: DebateTopic, top_k: int = 10) -> List[Lesson]:
        """
        检索与新议题相关的历史教训
        匹配维度: topic_type + affected_components + market + factors
        """
        ...
    
    def find_related_debates(self, topic: DebateTopic, top_k: int = 5) -> List[DebateRecord]:
        """检索相关历史辩论"""
        ...
    
    def get_outcome_for_lesson(self, lesson_id: str) -> Optional[Outcome]:
        """查询某个教训是否有回填结果"""
        ...
    
    # ── 学习 ──
    
    def record_outcome(self, outcome: Outcome):
        """记录注入后的实际效果，更新相关 lesson 的置信度"""
        ...
    
    def update_lesson_confidence(self, lesson_id: str, outcome: Outcome):
        """
        根据回填结果更新 lesson 置信度:
        - confirmed: confidence += 0.1 (上限 1.0), validated=True
        - refuted: confidence -= 0.3, validated=True
        - inconclusive: 不变
        """
        ...
    
    def detect_patterns(self) -> List[Dict]:
        """
        跨辩论模式检测:
        1. 反复出现的分歧 → 可能需要更多数据而非更多辩论
        2. 被证伪的共识 → 修正辩论质量 (哪些类型的共识不可靠)
        3. 高成功率结论 → 加权未来类似议题
        4. 特定模型总是偏乐观/悲观 → 校准模型权重
        """
        ...
    
    def build_knowledge_context(self, topic: DebateTopic) -> Dict:
        """
        为新辩论构建知识上下文 — 注入到 Prompt 中
        包含: 相关历史教训 + 相关历史辩论 + 已知反模式 + 已验证结论
        """
        ...
```

#### 3.3.4 结论注入通道 (Verdict → Injection)

辩论输出不是纯报告，而是 **DebateVerdict** — 带类型化的可执行注入指令。

```python
# debate_engine/verdict.py (新建)
@dataclass
class Injection:
    """可执行的配置注入指令"""
    injection_id: str
    target: str                  # "factor:SSS" | "exit:stable_15m" | "filter:global"
    action: str                  # "override_param" | "add_filter" | "remove_filter"
                                # | "enable_strategy" | "disable_strategy"
    params: Dict[str, Any]       # 具体参数变更
    confidence: float            # 辩论共识置信度
    market: str                  # "US" | "HK" | "BOTH"
    source_debate_id: str        # 来自哪次辩论
    status: str = "pending"      # "pending" | "applied" | "reverted"

@dataclass
class DebateVerdict:
    """辩论最终结论 — 结构化 + 可执行"""
    debate_id: str
    topic: DebateTopic
    total_rounds: int
    consensus_score: float
    stop_reason: str
    
    # 共识项 — 所有模型同意，可直接注入
    consensus_items: List[Dict]
    # 例: [{"statement": "增加 VIX<25 过滤", "confidence": 0.92, "evidence": "..."}]
    
    # 分歧项 — 模型意见不同，标记为 shadow test
    disagreement_items: List[Dict]
    # 例: [{"statement": "午餐时段排除", "model_a": "支持", "model_b": "反对", "resolution": "shadow"}]
    
    # 盲区 — 所有模型都未考虑，标记为研究项
    blind_spots: List[str]
    
    # 可执行的注入指令
    injections: List[Injection]
    
    # 提炼的教训
    lessons: List[Lesson]

class InjectionApplier:
    """
    注入执行器 — Overlay 模式
    
    不直接修改基线配置文件，而是写入 override 文件。
    Registry 在加载配置时自动 merge baseline + overrides。
    
    好处:
    - 基线配置永远干净 (git diff 可读)
    - override 可追溯 (每个文件对应一次辩论)
    - 可一键回滚 (删除 override 目录即可)
    """
    
    def apply(self, injection: Injection):
        """将注入写入 override 文件"""
        override_path = f"config/overrides/{injection.source_debate_id}_{injection.target}.yaml"
        # 写入 YAML，包含 metadata (辩论ID/时间/置信度)
        ...
    
    def revert(self, injection_id: str):
        """回滚指定注入 (删除 override 文件)"""
        ...
    
    def revert_all(self, debate_id: str):
        """回滚某次辩论的所有注入"""
        ...
    
    def list_active_overrides(self) -> List[Dict]:
        """列出当前生效的所有 override"""
        ...
```

**Override 文件结构示例**:
```yaml
# config/overrides/debate_20260523_factor_SSS.yaml
# Generated by: DebateEngine
# Debate ID: debate_20260523_sss_threshold
# Timestamp: 2026-05-23T14:30:00Z
# Consensus Score: 0.85
# Confidence: 0.92
#
# 辩论结论: SSS 阈值从 -1 调整到 -1.5 可减少假信号 30%，胜率提升 3%

override:
  target: factor:SSS
  action: override_param
  params:
    threshold: -1.5       # 原值: -1
  source:
    debate_id: debate_20260523_sss_threshold
    round_count: 4
    models: [qwen3.7-max, deepseek-v4-pro]
    consensus_statement: "SSS threshold -1.5 reduces false signals while maintaining sample size"
  rollback:
    original_value: -1
    method: "delete this file"
```

#### 3.3.5 历史教训注入到 Prompt

每次新辩论开始时，自动从知识库检索相关历史，注入到 Round 1 Prompt：

```
[Round 1 Prompt 结构]
├── 议题定义: 类型 + 背景 + 受影响组件
├── 证据数据: 回测统计 + 交易明细 + 日志
├── 统计门禁: Bonferroni p, Monte Carlo pctl, top-N robustness (如适用)
│
├── ─── 历史知识注入 ───
├── 相关教训 (Top 5):
│   ├── [2026-05-15] SSS threshold -1→-2: 交易数-40%, 胜率+3% ✅已验证
│   ├── [2026-05-10] VIX<25 过滤: 样本量不足 ⚠️教训: 过滤叠加时注意样本量
│   ├── [2026-05-18] 午餐排除: 共识但未执行 ⏳待验证
│   ├── [2026-05-20] ATR×1.5 止损: 15min策略太紧, 频繁被扫 ❌已证伪
│   └── [2026-05-22] MFFF 波动自适应: 低波动期假信号-25% ✅已验证
│
├── 已知反模式:
│   ├── "共识度高但样本量<50的结论，历史证伪率 60%"
│   └── "两个模型都建议收紧参数时，实际效果往往过度收紧"
│
├── 要求:
│   ├── 你的分析是否与历史教训矛盾？如果是，说明为什么这次不同
│   ├── 已验证的教训可以作为前提使用，无需重新论证
│   └── 待验证的教训请提出验证方案
```

#### 3.3.6 结论回填与持续学习 (Outcome Tracking)

```python
# debate_engine/outcome_tracker.py (新建)
class OutcomeTracker:
    """
    追踪辩论结论注入后的实际效果
    
    触发时机:
    1. 注入后 7 天 — 首次检查
    2. 注入后 20 天 — 正式评估
    3. 注入后 60 天 — 长期验证
    
    评估方法:
    - 对比注入前 N 天 vs 注入后 N 天的核心指标
    - 控制变量: 同市场、同 regime、同策略
    """
    
    def check_outcomes(self, days_threshold: int = 7) -> List[Outcome]:
        """检查所有已应用但未评估的注入"""
        ...
    
    def evaluate_injection(self, injection: Injection, observation_days: int) -> Outcome:
        """评估单个注入的效果"""
        ...
    
    def generate_learning_report(self) -> Dict:
        """
        生成学习报告 — 辩论引擎的自我进化
        包含:
        - 历史辩论胜率 (结论被验证的比例)
        - 各模型的预测准确率
        - 高效结论的特征 (哪些类型的议题/参数/市场组合容易得到正确结论)
        - 建议的辩论策略调整
        """
        ...
```

#### 3.3.7 统计验证门禁 (Statistical Gate)

```python
# debate_engine/statistical_gate.py (新建 — 独立模块)
class StatisticalGate:
    """
    策略回测的统计学门禁 — 辩论前的数据质量检查
    
    必须通过以下所有检验才能进入辩论环节:
    1. 最小样本量 (基于策略时间框架)
    2. Bonferroni 校正后 p < 0.05
    3. 修正 Sharpe (按实际交易频率年化)
    4. 头部交易移除后仍显著
    5. 蒙特卡洛置换检验 (1000 次)
    """
    
    MIN_SAMPLES = {"5min": 200, "15min": 100, "daily": 50}
    
    def evaluate(self, trades: List[Dict], n_total_tests: int) -> GateResult:
        ...
    
    def _corrected_sharpe(self, pnl_list, trades_per_year) -> float: ...
    def _bonferroni_p(self, p_raw, n_tests) -> float: ...
    def _monte_carlo_permutation(self, pnl_list, n_permutations=1000) -> float: ...
    def _top_n_robustness(self, pnl_list, max_n=10) -> int: ...
```

#### 3.3.8 模型配置 (统一百炼平台 + 可切换)

```yaml
# debate_engine/config.yaml
platform:
  name: bailian                     # 统一平台
  api_key_env: BAILIAN_API_KEY      # 环境变量名 (所有模型共用)
  base_url: https://dashscope.aliyuncs.com/compatible-mode/v1  # 百炼 OpenAI 兼容接口

# 默认模型池 — 辩论时从中选择 N 个
# 铁律: 同 family 的模型不能同时参与同一场辩论
models:
  # ── 阿里 Qwen 体系 ──
  - name: qwen3.7-max
    family: qwen
    label: "Qwen"
    strengths: ["数据分析", "中文理解"]
    max_tokens: 8000
    temperature: 0.3
    enabled: true                   # 当前启用

  # ── 深度求索体系 ──
  - name: deepseek-v4-pro
    family: deepseek
    label: "DeepSeek"
    strengths: ["代码分析", "数学推理", "逻辑严谨"]
    max_tokens: 8000
    temperature: 0.3
    enabled: true                   # 当前启用

  # ── 以下模型当前未启用，按需开启 ──

  - name: qwen-plus
    family: qwen
    label: "Qwen-Plus (轻量)"
    strengths: ["快速响应", "成本低"]
    max_tokens: 4000
    temperature: 0.3
    enabled: false

  - name: glm-5
    family: glm
    label: "GLM"
    strengths: ["逻辑推理", "风险评估"]
    max_tokens: 8000
    temperature: 0.3
    enabled: false

  - name: minimax-m2.5
    family: minimax
    label: "MiniMax"
    strengths: ["创意方案", "长上下文"]
    max_tokens: 8000
    temperature: 0.3
    enabled: false

  # ── Claude Code (可选第 4 视角, subprocess 模式) ──
  # - name: claude-code
  #   family: anthropic
  #   mode: subprocess
  #   command: "claude"
  #   args: ["--acp", "--stdio", "--dangerously-skip-permissions"]
  #   run_as: claude-runner
  #   timeout: 180
  #   strengths: ["代码扫描", "架构分析"]
  #   enabled: false

# 辩论默认配置
debate_defaults:
  min_rounds: 3
  max_rounds: 7
  convergence_threshold: 0.8
  divergence_limit: 2
  # 当前默认: qwen3.7-max + deepseek-v4-pro (2 模型, 跨体系)
  default_selection:
    models: ["qwen3.7-max", "deepseek-v4-pro"]
    # 未来扩展到 3 模型时:
    # models: ["qwen3.7-max", "deepseek-v4-pro", "glm-5"]
    # 或自动选:
    # strategy: "cross_family"
    # count: 3
```

```python
# debate_engine/models/bailian.py (新建)
import os
import json
import requests
from typing import Dict, List, Optional

class BailianClient:
    """
    百炼平台统一客户端
    
    所有模型 (qwen/deepseek/...) 共用一个 API Key，
    通过 OpenAI 兼容接口调用，只需切换 model 参数。
    """
    
    def __init__(self, config: Dict):
        self.api_key = os.environ[config['platform']['api_key_env']]
        self.base_url = config['platform']['base_url'].rstrip('/')
        self.models = {m['name']: m for m in config['models'] 
                       if m.get('mode') != 'subprocess'}
    
    def select_models(self, count: int = 3, strategy: str = "cross_family",
                      manual: List[str] = None) -> List[str]:
        """
        选择辩论模型组合
        
        cross_family: 自动选 N 个不同 family 的模型
        manual: 手动指定模型列表
        """
        if manual:
            return manual
        
        if strategy == "cross_family":
            # 每个 family 选一个，按 strength 多样性优先
            families_seen = set()
            selected = []
            for model_name, model_cfg in self.models.items():
                if model_cfg['family'] not in families_seen:
                    families_seen.add(model_cfg['family'])
                    selected.append(model_name)
                    if len(selected) >= count:
                        break
            return selected
        
        return list(self.models.keys())[:count]
    
    async def call(self, model_name: str, prompt: str, 
                   system: str = "", max_tokens: int = None) -> Dict:
        """
        调用百炼模型 — 统一接口
        
        底层走 OpenAI 兼容格式:
        POST {base_url}/chat/completions
        Header: Authorization: Bearer {BAILIAN_API_KEY}
        """
        model_cfg = self.models[model_name]
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": model_name,
                "messages": messages,
                "temperature": model_cfg.get('temperature', 0.3),
                "max_tokens": max_tokens or model_cfg.get('max_tokens', 8000),
            },
            timeout=120
        )
        resp.raise_for_status()
        
        data = resp.json()
        content = data['choices'][0]['message']['content']
        
        # 尝试解析 JSON (辩论输出要求结构化)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # 尝试提取 ```json ... ``` 代码块
            parsed = self._extract_json_from_markdown(content)
        
        return {
            "model": model_name,
            "family": model_cfg['family'],
            "content": content,
            "parsed": parsed,
            "usage": data.get("usage", {}),
        }
    
    def _extract_json_from_markdown(self, text: str) -> Optional[Dict]:
        """从 markdown 代码块中提取 JSON"""
        import re
        match = re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        return None
```

#### 3.3.9 辩论引擎完整文件结构

```
debate_engine/                    # 独立模块，不绑定任何市场
├── __init__.py
├── config.yaml                   # 模型配置 + 轮次参数
├── topic.py                      # DebateTopic + TopicType
├── rounds.py                     # DynamicRoundScheduler
├── verdict.py                    # DebateVerdict + Injection + InjectionApplier
├── knowledge.py                  # DebateKnowledgeBase + Lesson + DebateRecord
├── outcome_tracker.py            # OutcomeTracker
├── statistical_gate.py           # StatisticalGate
├── models/                       # 模型调用适配层
│   ├── base.py                   # BaseLLMAdapter
│   ├── bailian.py                # 百炼平台 (qwen/deepseek + 可扩展)
│   └── claude_code.py            # Claude Code subprocess
├── prompts/                      # Prompt 模板 (按 round 类型)
│   ├── round1_diverge.md
│   ├── round2_challenge.md
│   ├── round3_converge.md
│   └── round_deep.md             # Round 4+ 深度收敛
├── knowledge/                    # 知识数据 (git tracked)
│   ├── debates/                  # 完整辩论记录
│   ├── lessons/                  # 提炼的教训
│   │   └── lessons_index.json
│   ├── outcomes/                 # 回填结果
│   │   └── outcomes_index.json
│   └── patterns/                 # 跨辩论模式
│       └── patterns_index.json
└── cli.py                        # 命令行入口: python -m debate_engine --topic ...
```

---

### Phase 4: 自动化验证管线（Debate → Backtest → Validate → Shadow）

#### 3.4.1 完整管线 (辩论引擎作为独立外挂)

```
触发源 (任一):
├── 飞书 /debate 命令
├── Cron 定时巡检
├── 策略退化告警
├── 回测完成事件
└── 跨市场迁移需求
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│            独立辩论引擎 (debate_engine/)                     │
│                                                          │
│  ┌─────────┐  ┌──────────┐  ┌─────────┐  ┌───────────┐ │
│  │ 议题解析 │→│ 知识库检索│→│ 动态轮次 │→│ 结论+注入  │ │
│  │         │  │ 历史教训  │  │ 收敛驱动 │  │ 教训提炼   │ │
│  └─────────┘  └──────────┘  └─────────┘  └─────┬─────┘ │
└─────────────────────────────────────────────────┼────────┘
                                                  │
                              ┌───────────────────┼───────────────┐
                              │                   │               │
                              ▼                   ▼               ▼
                    ┌──────────────┐   ┌──────────────┐  ┌────────────┐
                    │ 注入因子参数  │   │ 注入 Exit    │  │ 保存知识库  │
                    │ (Overlay)    │   │ Profile      │  │ + 回填追踪  │
                    └──────────────┘   └──────────────┘  └────────────┘
                              │                   │
                              ▼                   ▼
                    ┌──────────────────────────────────────┐
                    │  现有系统 (旁路验证，不直接替换)         │
                    │  因子 Registry + Exit Registry        │
                    │  → 回测/影子 → 回填 → 学习闭环         │
                    └──────────────────────────────────────┘

并行管线 (可选，回测驱动):
┌──────────────────┐
│ Phase A: 回测    │  ← Walk-Forward (如需要)
│ Phase B: 统计门禁 │  ← StatisticalGate
└────────┬─────────┘
         │ evidence → 作为辩论议题的输入数据
         ▼
   [辩论引擎接收议题]
```

**关键区别**: 辩论引擎不再嵌在 A→B→C→D→E 管线中间，而是：
- **独立接收议题** (不依赖回测管线，也可以直接对现有策略辩论)
- **输出可注入** (因子参数 / Exit Profile / 过滤器 / 策略开关)
- **有记忆** (每次辩论记录 → 知识库 → 下次辩论自动注入)
- **有闭环** (注入后追踪实际效果 → 更新教训置信度)

#### 3.4.2 Walk-Forward 回测引擎

```python
# app/backtest/walk_forward.py (新建)
class WalkForwardEngine:
    """
    Walk-Forward 回测引擎
    
    解决回测过拟合的核心方法:
    1. 滚动窗口: 前 N% 训练，后 (100-N)% 测试
    2. 多窗口平均: 重复 K 次，取统计平均
    3. 参数稳定性: 不同窗口的最优参数应该相近
    """
    
    def run(self, 
            factor_assembly: FactorAssembly,
            exit_profile: ExitProfile,
            market_data: pd.DataFrame,
            train_pct: float = 0.7,
            n_windows: int = 3) -> WalkForwardResult:
        """
        执行 walk-forward 回测
        
        Returns:
            WalkForwardResult:
                in_sample_stats: Dict      # 训练集统计
                out_sample_stats: Dict     # 测试集统计
                degradation_ratio: float   # OOS/IS Sharpe ratio
                param_stability: float     # 参数跨窗口一致性
                combined_pnl_curve: List   # 合并 PnL 曲线
        """
        ...
```

#### 3.4.3 Cron Job 定时辩论

```yaml
# 自动化辩论调度 (Hermes cron)
triggers:
  # 每周一开盘前: 自动对当前活跃策略做一次辩论
  weekly_strategy_review:
    schedule: "0 21 * * 0"    # 周日 21:00 UTC (美东周一 17:00, 盘前)
    action: run_debate
    target: active_strategies
    
  # 策略退化告警: 连续 3 天亏损触发紧急辩论
  degradation_alert:
    condition: "consecutive_loss_days >= 3"
    action: run_debate
    target: degraded_strategy
    
  # 新因子组合: 回测完成后自动辩论
  post_backtest:
    condition: "backtest_completed"
    action: run_debate
    target: new_variant
```

---

## 四、实施路线图

### Step 1: 因子基础设施 (2-3天)
- [ ] 实现 `app/factors/base.py` — Factor 接口 + FactorValue + FactorParams
- [ ] 实现 `app/factors/registry.py` — 注册中心
- [ ] 实现 SSS/GDIAN/MFFF 三个核心因子
- [ ] 将现有 `regime_filter.py` + `capital_flow.py` 适配新接口
- [ ] 编写因子单元测试 (至少 30 个 test case)

### Step 2: Exit 模块化 (1-2天)
- [ ] 实现 `app/exit/base.py` — ExitProfile + BaseExitModule
- [ ] 实现 ATR/trailing/scale_out 三种退出模块
- [ ] 创建 `config/exit_profiles.yaml` — 9 条策略的退出配置
- [ ] 将 `app/strategies/exit_policies.py` 桥接到新系统

### Step 3: 因子组合 + 回测集成 (2-3天)
- [ ] 实现 `app/factors/assembly.py` — FactorAssembly
- [ ] 实现 `app/backtest/walk_forward.py` — Walk-Forward 引擎
- [ ] 集成 StatisticalGate — 自动判断是否通过门禁
- [ ] 编写 9 条策略的 FactorAssembly + ExitProfile 配置

### Step 4: 独立辩论引擎 (3-4天)
- [ ] 实现 `debate_engine/topic.py` — DebateTopic + TopicType 议题协议
- [ ] 实现 `debate_engine/rounds.py` — DynamicRoundScheduler 动态轮次
- [ ] 实现 `debate_engine/verdict.py` — DebateVerdict + Injection + InjectionApplier
- [ ] 实现 `debate_engine/knowledge.py` — DebateKnowledgeBase + Lesson + Outcome
- [ ] 实现 `debate_engine/outcome_tracker.py` — 结论回填与持续学习
- [ ] 实现 `debate_engine/models/` — 百炼平台模型适配 (qwen3.7-max + deepseek-v4-pro)
- [ ] 编写 Prompt 模板 (round1_diverge / round2_challenge / round3_converge / round_deep)
- [ ] 实现 `debate_engine/statistical_gate.py` — 统计门禁
- [ ] 创建 `debate_engine/knowledge/` 知识库目录结构
- [ ] 实现 `debate_engine/cli.py` — 命令行入口

### Step 5: 端到端集成 + Cron 自动化 (1-2天)
- [ ] 连接因子系统 + Exit 模块 + 辩论引擎
- [ ] 配置 Cron Job 定时辩论 (周度巡检 + 退化告警 + 回填检查)
- [ ] 飞书通知集成 (辩论结论推送 + 教训摘要)
- [ ] 编写集成测试 (完整议题→辩论→注入→回填链路)

### 总工时: 10-15 天

---

## 五、验证计划

### 5.1 因子系统验证
```bash
# 单元测试: 每个因子 ≥ 10 case
pytest tests/factors/test_sss.py -v
pytest tests/factors/test_gdian.py -v
pytest tests/factors/test_mfff.py -v

# 参数扫描验证
python scripts/factor_param_scan.py --factor SSS --data data/historical/
# 预期: 扫描结果与用户提供的 9 条策略参数一致
```

### 5.2 Exit 模块验证
```bash
# 回放历史交易，对比新旧 exit 差异
python scripts/backtest_config_change.py --exit-profile stable_15m_base --days 30
# 预期: 新 exit profile 在历史数据上 PnL 优于或等于现有系统
```

### 5.3 辩论引擎验证
```bash
# 用已知策略做一次完整辩论 (验证端到端链路)
python -m debate_engine --topic-type factor_param --market US \
  --evidence '{"strategy": "main_short_trailing", "trades": 296, "win_rate": 0.595}'

# 预期:
#   - 知识库检索: 返回相关历史教训 (首次运行为空)
#   - Round 1: 两模型独立分析 (qwen3.7-max + deepseek-v4-pro)
#   - Round 2: 有效交叉质疑 (非表面附和)
#   - Round 3+: 共识收敛 OR 分歧终止
#   - 输出: DebateVerdict (含 injections + lessons)
#   - 知识库: 自动保存辩论记录 + 提炼教训

# 验证知识库持久化
cat debate_engine/knowledge/lessons/lessons_index.json
# 预期: 至少 1 条 lesson 被记录

# 验证注入通道
python -m debate_engine --apply-injections --debate-id debate_xxx
ls config/overrides/
# 预期: 生成 override YAML 文件

# 验证回填追踪 (7天后)
python -m debate_engine --check-outcomes --days 7
# 预期: 报告注入后的指标变化
```

### 5.4 端到端验证
```bash
# 从因子组合 → 回测 → 门禁 → 辩论 → 影子 完整跑一遍
python scripts/run_full_pipeline.py --assembly stable_15m_relaxed_oversold_tight_trend
# 预期: 产出包含统计门禁结果 + 辩论共识 + 影子追踪启动
```

---

## 六、风险与对策

| 风险 | 概率 | 影响 | 对策 |
|------|------|------|------|
| 因子计算引入 bug 影响主交易路径 | 中 | 高 | 新因子系统仅用于回测/辩论，不直接替换现有 signal_pipeline |
| LLM 辩论产生"虚假共识"（两模型都说好话） | 高 | 中 | Prompt 中注入对抗性引导 + 强制要求 Round 2 找出漏洞 + 知识库反模式检测 |
| 回测过拟合（9 条策略已存在此风险） | 高 | 高 | Walk-Forward + Monte Carlo 双重验证 + Bonferroni 校正 |
| 新系统与现有代码冲突 | 中 | 中 | 渐进式集成: 先旁路运行，确认无冲突后再桥接 |
| Cron 辩论消耗大量 LLM token | 低 | 低 | 设置辩论频率上限 (每周 ≤ 3 次) + 使用轻量模型做初筛 |
| 知识库膨胀 — 历史教训过多导致 Prompt 过长 | 中 | 低 | 按相关度 Top-K 检索 (默认 5 条) + 定期清理低置信度教训 |
| 辩论结论注入后系统表现变差 | 中 | 高 | Overlay 模式一键回滚 + OutcomeTracker 7天自动检查 + 置信度<0.5的教训不参与 Prompt 注入 |
| 知识库数据丢失 (文件损坏/误删) | 低 | 中 | Git tracked + 定期备份 + lessons_index.json 可重建 |

---

## 七、决策自检 (5 问)

1. **为何**: 服务 F1 (更有效的交易规则) + F3 (架构清晰，模块职责单一)
2. **预期**: 胜率从 28.4% → 45%+ (15min 族群验证), 利润因子从 1.54 → 2.0+
3. **回滚**: 新系统旁路运行，`git revert` 即可恢复
4. **最简**: 没有不改代码的方案 — 当前因子不可参数化是根本障碍
5. **边界**: 🔴 级 — 跨文件重构 + 影响交易逻辑（但分阶段，每阶段独立可回滚）

---

## 八、与现有系统的桥接策略

**原则：Overlay，不是 Rewrite。**

```
现有系统 (保持不变):
  signal_pipeline.py → 继续生成信号
  exit_policy.yaml → 继续控制退出
  StrategyAdapter → 继续协调策略

新系统 (旁路并行):
  FactorRegistry → 因子计算 (仅回测/分析用)
  FactorAssembly → 策略组合定义
  ExitProfileRegistry → 退出配置 (仅回测/分析用)
  WalkForward → 回测验证

独立辩论引擎 (跨市场通用):
  debate_engine/ → 独立模块，不绑定任何市场
  DebateKnowledgeBase → 历史教训持久化 + 检索
  OutcomeTracker → 注入效果追踪 + 学习闭环
  InjectionApplier → Overlay 模式注入因子/退出参数

桥接 (Phase 5):
  当新系统通过 20+ 天影子验证后:
    signal_pipeline 新增 "factor_enhanced" 模式
    exit_policy.yaml 新增 exit_profile 引用
    新旧模式可通过 config 开关切换
  辩论引擎通过 Override 文件与两个市场交互:
    FTSeek-US 读 config/overrides/ (US 专属)
    FTSeek-HK 读 config/overrides/ (HK 专属)
    market=BOTH 的注入同时写入两个项目
```

---

## 附录 A: 9 条门禁策略的完整配置

```yaml
# config/strategy_variants.yaml (新建)
strategy_variants:
  # ── 5 分钟族群 ──
  aggressive_5m_high_momentum:
    timeframe: 5min
    factors:
      - name: SSS
        params: {threshold: -1, rsi_period: 14}
        condition: "score >= -1"
      - name: GDIAN
        params: {ma_period: 20, tight_threshold: 35}
        condition: "value <= 35"
      - name: MFFF
        params: {threshold: 2.0}
        condition: "score >= 2.0"
    exit_profile: aggressive_5m_high_momentum
    direction: LONG
    
  # ── 15 分钟族群 ──
  stable_15m_long:
    timeframe: 15min
    factors:
      - name: SSS
        params: {threshold: -1}
        condition: "score >= -1"
      - name: GDIAN
        params: {tight_threshold: 40}
        condition: "value <= 40"
      - name: MFFF
        params: {threshold: 0.5}
        condition: "score >= 0.5"
    exit_profile: stable_15m_base
    direction: LONG
    
  stable_15m_relaxed_oversold_tight_trend:
    timeframe: 15min
    factors:
      - name: SSS
        params: {threshold: -1.5}
        condition: "score >= -1.5"
      - name: GDIAN
        params: {tight_threshold: 35}
        condition: "value <= 35"
      - name: MFFF
        params: {threshold: 1.0}
        condition: "score >= 1.0"
    exit_profile: stable_15m_relaxed_oversold
    direction: LONG
    
  # ... (其余 6 条类似结构)
  
  # ── 日线族群 ──
  radical_daily_deep_oversold:
    timeframe: daily
    factors:
      - name: SSS
        params: {threshold: -2, rsi_period: 14}
        condition: "score <= -2"
      - name: GDIAN
        params: {tight_threshold: 25}
        condition: "value <= 25"
      - name: MFFF
        params: {threshold: 1.5}
        condition: "score >= 1.5"
    exit_profile: radical_daily_deep_oversold
    direction: LONG
```

---

## 附录 B: 辩论输出示例 (DebateVerdict 完整结构)

```json
{
  "debate_id": "debate_20260523_stable_15m_relaxed",
  "topic": {
    "topic_type": "factor_param",
    "market": "US",
    "title": "stable_15m_relaxed_oversold_tight_trend 策略参数优化",
    "affected_components": ["factor:SSS", "factor:GDIAN", "factor:MFFF", "exit:stable_15m"]
  },
  "total_rounds": 4,
  "stop_reason": "converged",
  "consensus_score": 0.85,
  
  "knowledge_injected": {
    "related_lessons": [
      {"id": "L001", "statement": "SSS threshold -1→-2 交易数-40%", "validated": true, "confidence": 0.9},
      {"id": "L002", "statement": "过滤叠加时注意样本量", "validated": true, "confidence": 0.85}
    ],
    "anti_patterns": ["共识度高但样本量<50的结论，历史证伪率 60%"]
  },

  "rounds_summary": {
    "round1": {
      "qwen37max": {
        "assessment": "修正 Sharpe=3.7，896 笔交易，统计基础扎实",
        "failure_modes": ["滑点敏感", "参数邻域窄"],
        "suggestions": ["VIX<25 过滤", "持仓放宽到 180min"]
      },
      "deepseek_v4_pro": {
        "assessment": "策略逻辑合理但滑点风险被低估",
        "failure_modes": ["15min K线延迟", "午餐流动性差"],
        "suggestions": ["排除 12:00-14:00 ET", "MFFF 波动自适应"]
      }
    },
    "round2": {
      "key_challenges": [
        "qwen 的 ADV 过滤忽略了小盘股反转 alpha",
        "deepseek 的午餐排除减少 30% 样本 (触发 L002 教训)"
      ]
    },
    "round3": {
      "consensus_score": 0.72,
      "note": "未达 0.8 阈值，进入 Round 4"
    },
    "round4": {
      "consensus_score": 0.85,
      "note": "收敛达标，终止"
    }
  },

  "consensus_items": [
    {"statement": "VIX < 25 前置过滤", "confidence": 0.92, "models_agree": 2},
    {"statement": "bid-ask spread < 0.05% 过滤", "confidence": 0.88, "models_agree": 2},
    {"statement": "持仓时间 120-180min (折中)", "confidence": 0.78, "models_agree": 2}
  ],

  "disagreement_items": [
    {"statement": "午餐时段排除", "resolution": "shadow_test", "note": "qwen支持 vs deepseek反对，样本量风险"},
    {"statement": "ADV 门槛 5M vs 2M", "resolution": "shadow_test", "note": "需更多数据"}
  ],

  "blind_spots": [
    "未讨论美股盘前盘后的信号质量差异",
    "未考虑 ETF vs 个股的因子表现差异"
  ],

  "injections": [
    {
      "injection_id": "inj_20260523_001",
      "target": "filter:global",
      "action": "add_filter",
      "params": {"condition": "vix < 25", "name": "vix_ceiling"},
      "confidence": 0.92,
      "market": "US",
      "status": "applied"
    },
    {
      "injection_id": "inj_20260523_002",
      "target": "filter:global",
      "action": "add_filter",
      "params": {"condition": "bid_ask_spread < 0.0005", "name": "spread_ceiling"},
      "confidence": 0.88,
      "market": "US",
      "status": "applied"
    },
    {
      "injection_id": "inj_20260523_003",
      "target": "exit:stable_15m_relaxed_oversold",
      "action": "override_param",
      "params": {"max_holding_minutes": 180},
      "confidence": 0.78,
      "market": "US",
      "status": "pending"
    }
  ],

  "lessons": [
    {
      "lesson_id": "L015",
      "category": "filter_false_positive",
      "statement": "VIX<25 + spread<0.05% 双过滤可将 15min 策略假信号减少约 25%，同时保留 >80% 有效信号",
      "confidence": 0.90,
      "validated": false,
      "applicable_markets": ["US"],
      "applicable_factors": ["SSS", "GDIAN", "MFFF"]
    },
    {
      "lesson_id": "L016",
      "category": "sample_size",
      "statement": "午餐时段排除虽有理论依据，但会减少 30% 样本量，在小样本策略中慎用",
      "confidence": 0.65,
      "validated": false,
      "applicable_markets": ["US"],
      "applicable_factors": []
    }
  ],

  "action_items": [
    {"action": "apply_injection", "id": "inj_20260523_001", "priority": "P0"},
    {"action": "apply_injection", "id": "inj_20260523_002", "priority": "P0"},
    {"action": "shadow_test", "variant": "lunch_excluded", "priority": "P1"},
    {"action": "shadow_test", "variant": "adv_2m_vs_5m", "priority": "P2"},
    {"action": "research", "topic": "盘前盘后信号质量差异", "priority": "P2"}
  ]
}
```
