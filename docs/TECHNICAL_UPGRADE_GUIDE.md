# FTSeek 技术升级实施指南

**日期**: 2026-04-09  
**版本**: v1.0  
**目标**: 基于 2026 量化策略调研结果，制定可落地的技术升级方案

---

## 一、保持架构灵活性

### 1.1 现有架构评估

#### 当前架构优势
```
FTSeek 现有架构 (2026-04)

app/
├── strategy/                    # 策略层
│   ├── base.py                 # 基类定义清晰接口
│   ├── confluence_v2.py        # 多因子融合策略
│   ├── event_driven.py         # 事件驱动策略
│   ├── cross_market.py         # 跨市场套利
│   └── consensus_engine.py     # 共识引擎 (动态权重)
├── factors/                     # 因子层
│   ├── sentiment_ai.py         # 情绪因子
│   └── technical_factors.py    # 技术因子
├── trader/                      # 执行层
│   └── executor.py             # 交易执行
└── risk/                        # 风控层
    ├── risk_manager.py         # 风险管理
    └── position_sizer.py       # 仓位管理
```

#### 架构灵活性检查清单
- [x] 策略基类定义清晰 (`BaseStrategy.on_kline()`, `on_news()`)
- [x] 共识引擎支持动态权重调整
- [x] 信号生命周期管理统一 (`SignalStore`)
- [x] 配置系统支持热更新 (`config_center.py`)
- [ ] 因子注册表支持动态注册 **(待实现)**
- [ ] 策略表现数据自动收集 **(待实现)**
- [ ] 模型服务化部署支持 **(待实现)**

### 1.2 增强架构灵活性 - 具体实施

#### 1.2.1 实现因子动态注册表

**文件**: `app/factors/registry.py` (新建)

```python
#!/usr/bin/env python3
"""
因子注册表 - 支持因子动态注册和发现

目标:
1. 新因子无需修改核心代码即可接入
2. 支持因子热插拔
3. 提供因子元数据管理
"""

from typing import Dict, List, Callable, Optional, Any
from dataclasses import dataclass, field
import logging

logger = logging.getLogger("ftseek.factors.registry")


@dataclass
class FactorMetadata:
    """因子元数据"""
    name: str
    category: str  # "technical", "sentiment", "fundamental", "alternative"
    frequency: str  # "intraday", "daily", "weekly"
    latency: str  # "realtime", "delayed", "batch"
    description: str = ""
    required_data: List[str] = field(default_factory=list)
    output_range: tuple = (-1.0, 1.0)  # 因子值范围


class FactorRegistry:
    """
    因子注册表 (单例模式)
    
    使用示例:
        # 注册因子
        @FactorRegistry.register("rsi_factor", category="technical")
        def rsi_factor(code: str, kline: dict) -> float:
            return calculate_rsi(kline['close'])
        
        # 获取因子
        factor = FactorRegistry.get("rsi_factor")
        value = factor(code, kline)
        
        # 获取所有 technical 类别因子
        factors = FactorRegistry.get_by_category("technical")
    """
    
    _instance = None
    _factors: Dict[str, Callable] = {}
    _metadata: Dict[str, FactorMetadata] = {}
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @classmethod
    def register(cls, name: str, category: str = "custom", 
                 frequency: str = "intraday", latency: str = "realtime",
                 description: str = "", required_data: List[str] = None,
                 output_range: tuple = (-1.0, 1.0)) -> Callable:
        """
        因子注册装饰器
        
        Args:
            name: 因子名称
            category: 因子类别
            frequency: 数据频率要求
            latency: 延迟要求
            description: 因子描述
            required_data: 所需数据列表
            output_range: 输出值范围
        """
        def decorator(func: Callable) -> Callable:
            cls._factors[name] = func
            cls._metadata[name] = FactorMetadata(
                name=name,
                category=category,
                frequency=frequency,
                latency=latency,
                description=description,
                required_data=required_data or [],
                output_range=output_range
            )
            logger.info(f"因子已注册：{name} ({category})")
            return func
        return decorator
    
    @classmethod
    def get(cls, name: str) -> Optional[Callable]:
        """获取因子函数"""
        return cls._factors.get(name)
    
    @classmethod
    def get_metadata(cls, name: str) -> Optional[FactorMetadata]:
        """获取因子元数据"""
        return cls._metadata.get(name)
    
    @classmethod
    def get_by_category(cls, category: str) -> Dict[str, Callable]:
        """按类别获取因子"""
        return {
            name: func for name, func in cls._factors.items()
            if cls._metadata[name].category == category
        }
    
    @classmethod
    def list_all(cls) -> List[Dict]:
        """列出所有已注册因子"""
        return [
            {
                'name': name,
                'category': meta.category,
                'frequency': meta.frequency,
                'description': meta.description
            }
            for name, meta in cls._metadata.items()
        ]
    
    @classmethod
    def unregister(cls, name: str) -> bool:
        """注销因子"""
        if name in cls._factors:
            del cls._factors[name]
            del cls._metadata[name]
            logger.info(f"因子已注销：{name}")
            return True
        return False


# ==================== 内置因子注册示例 ====================

@FactorRegistry.register(
    name="momentum_5d",
    category="technical",
    frequency="daily",
    latency="delayed",
    description="5 日动量因子",
    required_data=["close_price_5d"],
    output_range=(-1.0, 1.0)
)
def momentum_5d(code: str, data: dict) -> float:
    """计算 5 日动量"""
    if 'close_prices' not in data or len(data['close_prices']) < 5:
        return 0.0
    
    current = data['close_prices'][-1]
    past = data['close_prices'][-5]
    
    if past == 0:
        return 0.0
    
    momentum = (current - past) / past
    return max(-1.0, min(1.0, momentum))


@FactorRegistry.register(
    name="sentiment_score",
    category="sentiment",
    frequency="intraday",
    latency="realtime",
    description="实时情绪分数",
    required_data=["news_sentiment"],
    output_range=(-1.0, 1.0)
)
def sentiment_score(code: str, data: dict) -> float:
    """获取情绪分数"""
    return data.get('news_sentiment', {}).get('score', 0.0)
```

#### 1.2.2 策略接口标准化

**文件**: `app/strategy/base.py` (增强)

```python
# 在现有 BaseStrategy 基础上增加以下能力:

from abc import abstractmethod
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from enum import Enum


class StrategyCapability(Enum):
    """策略能力标识"""
    REALTIME_SIGNAL = "realtime_signal"      # 实时信号生成
    BATCH_SIGNAL = "batch_signal"           # 批量信号生成
    NEWS_REACTIVE = "news_reactive"         # 新闻响应
    KLINE_REACTIVE = "kline_reactive"       # K 线响应
    RISK_AWARE = "risk_aware"               # 风险感知
    POSITION_SIZING = "position_sizing"     # 仓位管理
    STOP_LOSS = "stop_loss"                 # 止损管理


@dataclass
class StrategyInfo:
    """策略信息"""
    name: str
    version: str
    capabilities: List[StrategyCapability]
    required_data: List[str]
    supported_markets: List[str]
    min_confidence: float = 0.5
    max_positions: int = 10


class BaseStrategy:
    """
    策略基类 (增强版)
    
    新增功能:
    1. 策略信息接口
    2. 能力标识
    3. 性能指标追踪
    """
    
    name: str = "base_strategy"
    version: str = "1.0.0"
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self._performance_metrics: Dict[str, Any] = {
            'total_signals': 0,
            'confirmed_signals': 0,
            'executed_signals': 0,
            'win_count': 0,
            'loss_count': 0,
            'total_pnl': 0.0,
        }
    
    def get_info(self) -> StrategyInfo:
        """获取策略信息"""
        return StrategyInfo(
            name=self.name,
            version=self.version,
            capabilities=self._get_capabilities(),
            required_data=self._get_required_data(),
            supported_markets=self._get_supported_markets(),
        )
    
    def _get_capabilities(self) -> List[StrategyCapability]:
        """返回策略支持的能力列表"""
        capabilities = []
        
        # 检查是否重写相关方法
        if self.__class__.on_kline != BaseStrategy.on_kline:
            capabilities.append(StrategyCapability.KLINE_REACTIVE)
        if self.__class__.on_news != BaseStrategy.on_news:
            capabilities.append(StrategyCapability.NEWS_REACTIVE)
        if self.__class__.calculate_position != BaseStrategy.calculate_position:
            capabilities.append(StrategyCapability.POSITION_SIZING)
        
        return capabilities
    
    def _get_required_data(self) -> List[str]:
        """返回策略所需数据列表"""
        return ["kline", "quote"]
    
    def _get_supported_markets(self) -> List[str]:
        """返回支持的市场列表"""
        return ["HK", "US"]
    
    def update_performance(self, signal_result: Dict):
        """更新性能指标"""
        self._performance_metrics['total_signals'] += 1
        
        if signal_result.get('confirmed'):
            self._performance_metrics['confirmed_signals'] += 1
        
        if signal_result.get('executed'):
            self._performance_metrics['executed_signals'] += 1
            pnl = signal_result.get('pnl', 0.0)
            self._performance_metrics['total_pnl'] += pnl
            
            if pnl > 0:
                self._performance_metrics['win_count'] += 1
            elif pnl < 0:
                self._performance_metrics['loss_count'] += 1
    
    def get_performance(self) -> Dict:
        """获取性能指标"""
        metrics = self._performance_metrics.copy()
        
        total_trades = metrics['win_count'] + metrics['loss_count']
        if total_trades > 0:
            metrics['win_rate'] = metrics['win_count'] / total_trades
            metrics['avg_pnl'] = metrics['total_pnl'] / metrics['executed_signals']
        else:
            metrics['win_rate'] = 0.0
            metrics['avg_pnl'] = 0.0
        
        return metrics
    
    @abstractmethod
    def generate_signal(self, data: Dict) -> Optional[Signal]:
        """生成信号 (抽象方法)"""
        pass
    
    def on_kline(self, code: str, kline: dict, kline_type: str) -> Optional[Signal]:
        """K 线回调 (默认不实现)"""
        return None
    
    def on_news(self, news_list: List[Dict]) -> List[Signal]:
        """新闻回调 (默认返回空列表)"""
        return []
    
    def calculate_position(self, signal: Signal, portfolio: Dict) -> float:
        """计算仓位比例 (0-1)"""
        return self.config.get('default_position', 0.1)
    
    def should_stop_loss(self, position: Dict, current_price: float) -> bool:
        """是否应该止损"""
        return False
    
    def should_take_profit(self, position: Dict, current_price: float) -> bool:
        """是否应该止盈"""
        return False
```

---

## 二、渐进式升级方案

### 2.1 升级阶段划分

```
升级路线图 (2026 Q2-Q4)

Q2 (4-6 月) - 基础增强
├── 共识引擎权重优化 ✅ 优先级最高
├── 因子注册表实现
├── 策略性能数据收集
└── 深度学习情绪因子 POC

Q3 (7-9 月) - 智能化升级
├── ML 回撤预测模型
├── DRL 仓位优化原型
├── 社交媒体情绪整合
└── Transformer 价格预测 POC

Q4 (10-12 月) - 深度整合
├── 全系统回测验证
├── 另类数据融合
├── 模型服务化部署
└── 生产环境灰度发布
```

### 2.2 第一阶段实施详情 (Q2)

#### 2.2.1 共识引擎权重优化

**文件**: `app/strategy/consensus_engine.py` (增强)

```python
# 在现有共识引擎中增加性能追踪权重计算

from collections import deque
from datetime import datetime, timedelta
import numpy as np


class PerformanceTracker:
    """
    策略性能追踪器
    
    功能:
    1. 记录各策略历史信号表现
    2. 计算 Sharpe、Sortino、胜率等指标
    3. 支持滚动窗口统计
    """
    
    def __init__(self, window_days: int = 60):
        self.window_days = window_days
        self._signal_history: Dict[str, deque] = {}  # strategy_name -> deque of signal results
    
    def record_signal(self, strategy_name: str, signal_result: Dict):
        """
        记录信号结果
        
        Args:
            strategy_name: 策略名称
            signal_result: {
                'timestamp': datetime,
                'code': str,
                'direction': str,
                'entry_price': float,
                'exit_price': float,
                'pnl': float,
                'pnl_pct': float,
                'holding_days': int
            }
        """
        if strategy_name not in self._signal_history:
            self._signal_history[strategy_name] = deque(maxlen=1000)
        
        self._signal_history[strategy_name].append({
            **signal_result,
            'recorded_at': datetime.now()
        })
    
    def get_metrics(self, strategy_name: str) -> Dict:
        """
        获取策略性能指标
        
        Returns:
            {
                'sharpe_ratio': float,
                'sortino_ratio': float,
                'win_rate': float,
                'avg_pnl_pct': float,
                'max_drawdown': float,
                'total_signals': int,
                'signal_count_7d': int,
                'signal_count_30d': int,
            }
        """
        if strategy_name not in self._signal_history:
            return self._empty_metrics()
        
        history = list(self._signal_history[strategy_name])
        if not history:
            return self._empty_metrics()
        
        # 时间过滤
        cutoff = datetime.now() - timedelta(days=self.window_days)
        recent = [h for h in history if h['recorded_at'] >= cutoff]
        
        if not recent:
            return self._empty_metrics()
        
        # 计算指标
        pnls = [h['pnl_pct'] for h in recent if 'pnl_pct' in h]
        wins = [p for p in pnls if p > 0]
        
        metrics = {
            'total_signals': len(recent),
            'signal_count_7d': len([h for h in recent 
                                    if h['recorded_at'] >= datetime.now() - timedelta(days=7)]),
            'signal_count_30d': len([h for h in recent 
                                     if h['recorded_at'] >= datetime.now() - timedelta(days=30)]),
            'win_rate': len(wins) / len(pnls) if pnls else 0.0,
            'avg_pnl_pct': np.mean(pnls) if pnls else 0.0,
            'sharpe_ratio': self._calc_sharpe(pnls),
            'sortino_ratio': self._calc_sortino(pnls),
            'max_drawdown': self._calc_max_drawdown(pnls),
        }
        
        return metrics
    
    def _calc_sharpe(self, pnls: List[float], risk_free: float = 0.02) -> float:
        """计算 Sharpe 比率 (年化)"""
        if not pnls or len(pnls) < 2:
            return 0.0
        
        excess_returns = [p - risk_free / 252 for p in pnls]
        mean_return = np.mean(excess_returns)
        std_return = np.std(excess_returns)
        
        if std_return == 0:
            return 0.0
        
        # 年化
        return (mean_return / std_return) * np.sqrt(252)
    
    def _calc_sortino(self, pnls: List[float], risk_free: float = 0.02) -> float:
        """计算 Sortino 比率 (只考虑下行波动)"""
        if not pnls or len(pnls) < 2:
            return 0.0
        
        excess_returns = [p - risk_free / 252 for p in pnls]
        mean_return = np.mean(excess_returns)
        
        # 下行偏差
        downside_returns = [r for r in excess_returns if r < 0]
        if not downside_returns:
            return float('inf') if mean_return > 0 else 0.0
        
        downside_std = np.std(downside_returns)
        
        if downside_std == 0:
            return 0.0
        
        return (mean_return / downside_std) * np.sqrt(252)
    
    def _calc_max_drawdown(self, pnls: List[float]) -> float:
        """计算最大回撤"""
        if not pnls:
            return 0.0
        
        cumulative = np.cumprod([1 + p for p in pnls])
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = (cumulative - running_max) / running_max
        
        return abs(np.min(drawdowns))
    
    def _empty_metrics(self) -> Dict:
        """返回空指标"""
        return {
            'sharpe_ratio': 0.0,
            'sortino_ratio': 0.0,
            'win_rate': 0.0,
            'avg_pnl_pct': 0.0,
            'max_drawdown': 0.0,
            'total_signals': 0,
            'signal_count_7d': 0,
            'signal_count_30d': 0,
        }


class DynamicWeightCalculator:
    """
    动态权重计算器
    
    方法选择:
    1. performance_ranking: 基于表现排名分配权重
    2. sharpe_weighted: 按 Sharpe 比率加权
    3. bayesian_optimization: 贝叶斯优化 (需要额外依赖)
    """
    
    def __init__(self, method: str = "performance_ranking"):
        self.method = method
        self.performance_tracker = PerformanceTracker(window_days=60)
    
    def calculate_weights(self, strategy_names: List[str], 
                         market_regime: str = "normal") -> Dict[str, float]:
        """
        计算各策略权重
        
        Args:
            strategy_names: 策略名称列表
            market_regime: 市场状态 ("normal", "high_vol", "trend", "range")
        
        Returns:
            {strategy_name: weight}
        """
        if self.method == "performance_ranking":
            return self._weight_by_ranking(strategy_names, market_regime)
        elif self.method == "sharpe_weighted":
            return self._weight_by_sharpe(strategy_names, market_regime)
        else:
            return self._weight_by_ranking(strategy_names, market_regime)
    
    def _weight_by_ranking(self, strategy_names: List[str], 
                          market_regime: str) -> Dict[str, float]:
        """
        基于表现排名分配权重
        
        规则:
        - 第 1 名：40%
        - 第 2 名：25%
        - 第 3 名：15%
        - 第 4 名：10%
        - 第 5 名及以后：均分剩余 10%
        """
        if not strategy_names:
            return {}
        
        # 获取各策略得分
        scores = {}
        for name in strategy_names:
            metrics = self.performance_tracker.get_metrics(name)
            
            # 综合得分 = Sharpe * 0.4 + 胜率 * 0.3 + (1 - 最大回撤) * 0.3
            score = (
                metrics['sharpe_ratio'] * 0.4 +
                metrics['win_rate'] * 0.3 +
                (1 - metrics['max_drawdown']) * 0.3
            )
            scores[name] = score
        
        # 按得分排序
        sorted_strategies = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        
        # 分配权重
        weights = {}
        rank_weights = [0.40, 0.25, 0.15, 0.10]  # 前 4 名权重
        remaining = 0.10  # 剩余策略均分 10%
        
        for i, (name, score) in enumerate(sorted_strategies):
            if i < len(rank_weights):
                weights[name] = rank_weights[i]
            else:
                remaining_count = len(sorted_strategies) - len(rank_weights)
                weights[name] = remaining / remaining_count if remaining_count > 0 else 0
        
        return weights
    
    def _weight_by_sharpe(self, strategy_names: List[str],
                         market_regime: str) -> Dict[str, float]:
        """
        按 Sharpe 比率加权
        
        规则:
        - 只考虑 Sharpe > 0 的策略
        - 权重正比于 Sharpe 比率
        """
        if not strategy_names:
            return {}
        
        sharpes = {}
        for name in strategy_names:
            metrics = self.performance_tracker.get_metrics(name)
            if metrics['sharpe_ratio'] > 0:
                sharpes[name] = metrics['sharpe_ratio']
        
        if not sharpes:
            # 如果没有正 Sharpe 策略，平均分配
            return {name: 1.0 / len(strategy_names) for name in strategy_names}
        
        total_sharpe = sum(sharpes.values())
        weights = {name: sharpe / total_sharpe for name, sharpe in sharpes.items()}
        
        return weights
```

**集成到共识引擎**:

```python
# 在 consensus_engine.py 的 ConsensusEngine 类中添加:

class ConsensusEngine:
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.weight_calculator = DynamicWeightCalculator(method="performance_ranking")
        self._strategy_metrics: Dict[str, Dict] = {}
    
    def update_strategy_performance(self, strategy_name: str, signal_result: Dict):
        """更新策略性能记录"""
        self.weight_calculator.performance_tracker.record_signal(
            strategy_name, signal_result
        )
        self._strategy_metrics[strategy_name] = \
            self.weight_calculator.performance_tracker.get_metrics(strategy_name)
    
    def get_dynamic_weights(self, market_regime: str = "normal") -> Dict[str, float]:
        """
        获取动态权重
        
        Args:
            market_regime: 市场状态
        
        Returns:
            策略权重字典
        """
        strategy_names = list(self._strategy_metrics.keys())
        return self.weight_calculator.calculate_weights(strategy_names, market_regime)
    
    def get_strategy_metrics(self, strategy_name: str = None) -> Dict:
        """获取策略性能指标"""
        if strategy_name:
            return self._strategy_metrics.get(strategy_name, {})
        return self._strategy_metrics
```

#### 2.2.2 策略性能数据收集

**文件**: `data/strategy_performance.db` (新建数据库)

```sql
-- 策略性能数据表结构

CREATE TABLE IF NOT EXISTS strategy_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    code TEXT NOT NULL,
    action TEXT NOT NULL,
    
    -- 信号生成时间
    signal_timestamp DATETIME NOT NULL,
    
    -- 入场信息
    entry_price REAL,
    entry_timestamp DATETIME,
    
    -- 出场信息
    exit_price REAL,
    exit_timestamp DATETIME,
    exit_reason TEXT,  -- "stop_loss", "take_profit", "manual", "signal_reverse"
    
    -- 盈亏
    pnl REAL,
    pnl_pct REAL,
    holding_period_days INTEGER,
    
    -- 市场环境
    market_regime TEXT,  -- "normal", "high_vol", "trend", "range"
    vix_level REAL,
    
    -- 元数据
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_strategy ON strategy_signals(strategy_name);
CREATE INDEX idx_signal_time ON strategy_signals(signal_timestamp);
CREATE INDEX idx_code ON strategy_signals(code);

-- 策略日统计表
CREATE TABLE IF NOT EXISTS strategy_daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    date DATE NOT NULL,
    
    signal_count INTEGER,
    confirmed_count INTEGER,
    executed_count INTEGER,
    win_count INTEGER,
    loss_count INTEGER,
    
    total_pnl REAL,
    avg_pnl_pct REAL,
    sharpe_ratio REAL,
    max_drawdown REAL,
    
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    
    UNIQUE(strategy_name, date)
);
```

**文件**: `app/monitoring/strategy_performance_db.py` (新建)

```python
#!/usr/bin/env python3
"""
策略性能数据库 - 持久化存储策略表现数据
"""

import sqlite3
from datetime import datetime, date
from typing import Dict, List, Optional
from contextlib import contextmanager
import logging

logger = logging.getLogger("ftseek.monitoring.performance_db")


class StrategyPerformanceDB:
    """策略性能数据库"""
    
    def __init__(self, db_path: str = "data/strategy_performance.db"):
        self.db_path = db_path
        self._init_db()
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def _init_db(self):
        """初始化数据库表"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 创建策略信号表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    signal_timestamp DATETIME NOT NULL,
                    entry_price REAL,
                    entry_timestamp DATETIME,
                    exit_price REAL,
                    exit_timestamp DATETIME,
                    exit_reason TEXT,
                    pnl REAL,
                    pnl_pct REAL,
                    holding_period_days INTEGER,
                    market_regime TEXT,
                    vix_level REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 创建日统计表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategy_daily_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name TEXT NOT NULL,
                    date DATE NOT NULL,
                    signal_count INTEGER,
                    confirmed_count INTEGER,
                    executed_count INTEGER,
                    win_count INTEGER,
                    loss_count INTEGER,
                    total_pnl REAL,
                    avg_pnl_pct REAL,
                    sharpe_ratio REAL,
                    max_drawdown REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(strategy_name, date)
                )
            """)
            
            # 创建索引
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_strategy ON strategy_signals(strategy_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_signal_time ON strategy_signals(signal_timestamp)")
            
            logger.info("策略性能数据库初始化完成")
    
    def record_signal(self, signal_data: Dict):
        """记录信号生成"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO strategy_signals 
                (strategy_name, signal_id, code, action, signal_timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (
                signal_data['strategy_name'],
                signal_data['signal_id'],
                signal_data['code'],
                signal_data['action'],
                signal_data['signal_timestamp']
            ))
    
    def update_signal_result(self, signal_id: str, result_data: Dict):
        """更新信号结果"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE strategy_signals
                SET 
                    entry_price = ?,
                    entry_timestamp = ?,
                    exit_price = ?,
                    exit_timestamp = ?,
                    exit_reason = ?,
                    pnl = ?,
                    pnl_pct = ?,
                    holding_period_days = ?,
                    market_regime = ?,
                    vix_level = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE signal_id = ?
            """, (
                result_data.get('entry_price'),
                result_data.get('entry_timestamp'),
                result_data.get('exit_price'),
                result_data.get('exit_timestamp'),
                result_data.get('exit_reason'),
                result_data.get('pnl'),
                result_data.get('pnl_pct'),
                result_data.get('holding_period_days'),
                result_data.get('market_regime'),
                result_data.get('vix_level'),
                signal_id
            ))
    
    def get_strategy_performance(self, strategy_name: str, 
                                 days: int = 60) -> Dict:
        """获取策略性能统计"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 获取最近 N 天的信号
            cursor.execute("""
                SELECT pnl_pct, signal_timestamp
                FROM strategy_signals
                WHERE strategy_name = ?
                AND signal_timestamp >= datetime('now', ?)
                AND pnl_pct IS NOT NULL
                ORDER BY signal_timestamp
            """, (strategy_name, f'-{days} days'))
            
            rows = cursor.fetchall()
            
            if not rows:
                return self._empty_performance()
            
            pnls = [row['pnl_pct'] for row in rows]
            
            # 计算统计指标
            import numpy as np
            total_signals = len(pnls)
            win_count = sum(1 for p in pnls if p > 0)
            win_rate = win_count / total_signals if total_signals > 0 else 0
            avg_pnl = np.mean(pnls)
            std_pnl = np.std(pnls)
            sharpe = (avg_pnl / std_pnl) * np.sqrt(252) if std_pnl > 0 else 0
            
            # 计算最大回撤
            cumulative = np.cumprod([1 + p for p in pnls])
            running_max = np.maximum.accumulate(cumulative)
            max_dd = abs(np.min((cumulative - running_max) / running_max))
            
            return {
                'strategy_name': strategy_name,
                'total_signals': total_signals,
                'win_count': win_count,
                'loss_count': total_signals - win_count,
                'win_rate': win_rate,
                'avg_pnl_pct': avg_pnl,
                'sharpe_ratio': sharpe,
                'max_drawdown': max_dd,
                'total_pnl': np.prod([1 + p for p in pnls]) - 1,
            }
    
    def _empty_performance(self) -> Dict:
        """返回空性能统计"""
        return {
            'strategy_name': '',
            'total_signals': 0,
            'win_count': 0,
            'loss_count': 0,
            'win_rate': 0.0,
            'avg_pnl_pct': 0.0,
            'sharpe_ratio': 0.0,
            'max_drawdown': 0.0,
            'total_pnl': 0.0,
        }
```

---

## 三、数据积累方案

### 3.1 数据收集清单

#### 必须收集的数据

| 数据类型 | 收集频率 | 存储位置 | 用途 |
|----------|----------|----------|------|
| 策略信号 | 实时 | SQLite + JSON | 权重计算、回测 |
| K 线数据 | 实时/分钟 | existing | 因子计算、模型训练 |
| 成交记录 | 实时 | SQLite | 性能统计 |
| 市场状态 | 分钟 | SQLite | 状态依赖策略 |
| 情绪数据 | 实时/小时 | SQLite + Parquet | 情绪因子训练 |
| 新闻数据 | 实时 | existing | 事件驱动、NLP 训练 |

#### 数据存储结构设计

```
data/
├── strategy_performance.db    # 策略性能 SQLite
├── signals.db                  # 现有信号库
├── kline_cache/               # K 线缓存
│   ├── HK/
│   │   ├── 00700_1m.parquet
│   │   └── ...
│   └── US/
├── model_training/            # 模型训练数据
│   ├── factor_features/       # 因子特征
│   │   └── features_YYYYMMDD.parquet
│   ├── labels/               # 标签 (未来收益)
│   │   └── labels_YYYYMMDD.parquet
│   └── splits/               # 训练/验证/测试分割
└── market_regime/            # 市场状态记录
    └── regime_history.json
```

### 3.2 特征数据收集

**文件**: `app/factors/feature_collector.py` (新建)

```python
#!/usr/bin/env python3
"""
特征数据收集器 - 系统性地收集 ML 训练所需特征

功能:
1. 实时收集因子数据
2. 生成训练标签 (未来收益)
3. 存储为 Parquet 格式 (高效读取)
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path
import logging

logger = logging.getLogger("ftseek.factors.feature_collector")


class FeatureCollector:
    """特征收集器"""
    
    def __init__(self, storage_dir: str = "data/model_training"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        
        # 特征存储
        self._current_features: Dict[str, Dict] = {}
        self._feature_history: List[Dict] = []
    
    def record_features(self, code: str, timestamp: datetime, 
                       features: Dict[str, float]):
        """
        记录因子特征
        
        Args:
            code: 股票代码
            timestamp: 时间戳
            features: {factor_name: value}
        """
        record = {
            'code': code,
            'timestamp': timestamp,
            'date': timestamp.date(),
            'time': timestamp.time(),
            **features
        }
        
        self._feature_history.append(record)
        self._current_features[code] = features
    
    def generate_labels(self, code: str, prices: List[float],
                       timestamps: List[datetime],
                       horizons: List[int] = [1, 3, 5]) -> pd.DataFrame:
        """
        生成训练标签 (未来收益)
        
        Args:
            code: 股票代码
            prices: 价格序列
            timestamps: 时间戳序列
            horizons: 预测周期 [1, 3, 5] 天
        
        Returns:
            DataFrame with columns: timestamp, return_1d, return_3d, return_5d
        """
        data = {'timestamp': [], 'date': []}
        for h in horizons:
            data[f'return_{h}d'] = []
        
        for i, (price, ts) in enumerate(zip(prices, timestamps)):
            for h in horizons:
                if i + h < len(prices):
                    future_price = prices[i + h]
                    ret = (future_price - price) / price
                    data[f'return_{h}d'].append(ret)
                else:
                    data[f'return_{h}d'].append(None)
            
            data['timestamp'].append(ts)
            data['date'].append(ts.date())
        
        return pd.DataFrame(data).dropna()
    
    def save_daily_features(self, date: str = None):
        """保存日度特征数据"""
        if not date:
            date = datetime.now().strftime('%Y%m%d')
        
        if not self._feature_history:
            logger.warning("没有特征数据可保存")
            return
        
        df = pd.DataFrame(self._feature_history)
        
        # 保存为 Parquet
        output_path = self.storage_dir / 'factor_features' / f'features_{date}.parquet'
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        df.to_parquet(output_path, index=False)
        logger.info(f"特征数据已保存：{output_path} ({len(df)} 条)")
        
        # 清空缓存
        self._feature_history = []
    
    def load_features(self, start_date: str, end_date: str) -> pd.DataFrame:
        """加载历史特征数据"""
        feature_dir = self.storage_dir / 'factor_features'
        
        files = list(feature_dir.glob('features_*.parquet'))
        dfs = []
        
        for f in files:
            # 解析文件名中的日期
            date_str = f.stem.replace('features_', '')
            if start_date <= date_str <= end_date:
                df = pd.read_parquet(f)
                dfs.append(df)
                logger.debug(f"加载特征文件：{f.name}")
        
        if not dfs:
            logger.warning(f"未找到特征数据 ({start_date} to {end_date})")
            return pd.DataFrame()
        
        return pd.concat(dfs, ignore_index=True)
    
    def get_current_features(self, code: str) -> Optional[Dict]:
        """获取当前因子特征"""
        return self._current_features.get(code)


class MarketRegimeDetector:
    """
    市场状态检测器
    
    状态分类:
    - normal: 正常市场
    - high_vol: 高波动
    - trend: 趋势市场
    - range: 震荡市场
    """
    
    def __init__(self):
        self.vix_threshold = 25.0
        self.adx_threshold = 25.0
    
    def detect(self, market_data: Dict) -> str:
        """
        检测市场状态
        
        Args:
            market_data: {
                'vix': float,
                'adx': float,
                'volatility': float,
                'trend_strength': float
            }
        
        Returns:
            market_regime: "normal" | "high_vol" | "trend" | "range"
        """
        vix = market_data.get('vix', 0)
        adx = market_data.get('adx', 0)
        volatility = market_data.get('volatility', 0)
        
        # 高波动判断
        if vix > self.vix_threshold or volatility > 0.03:
            return "high_vol"
        
        # 趋势判断
        if adx > self.adx_threshold:
            return "trend"
        
        # 震荡判断
        if volatility < 0.01:
            return "range"
        
        return "normal"
```

### 3.3 数据收集集成

**文件**: `app/main.py` (集成数据收集)

```python
# 在主程序启动时初始化数据收集器

from app.factors.feature_collector import FeatureCollector, MarketRegimeDetector
from app.monitoring.strategy_performance_db import StrategyPerformanceDB

# 初始化全局数据收集器
feature_collector = FeatureCollector()
performance_db = StrategyPerformanceDB()
regime_detector = MarketRegimeDetector()

# 在信号处理流程中集成数据收集
def process_signal(signal: Signal, strategy_name: str):
    """处理信号 (增强版 - 含数据收集)"""
    
    # 1. 记录信号到数据库
    performance_db.record_signal({
        'strategy_name': strategy_name,
        'signal_id': signal.id,
        'code': signal.code,
        'action': signal.action.value,
        'signal_timestamp': datetime.now()
    })
    
    # 2. 记录当前因子特征
    current_features = get_current_factors(signal.code)
    feature_collector.record_features(
        code=signal.code,
        timestamp=datetime.now(),
        features=current_features
    )
    
    # 3. 记录市场状态
    market_data = get_market_data()
    regime = regime_detector.detect(market_data)
    
    # ... 后续信号处理
```

---

## 四、实施检查清单

### 4.1 第一阶段 (Q2 2026)

#### 4 月
- [ ] 因子注册表实现并完成测试
- [ ] 策略基类增强 (能力标识、性能追踪)
- [ ] 性能数据库表创建

#### 5 月
- [ ] 共识引擎动态权重集成
- [ ] 特征收集器部署
- [ ] 市场状态检测器集成

#### 6 月
- [ ] 第一阶段回测验证
- [ ] 深度学习情绪因子 POC
- [ ] 技术文档完善

### 4.2 关键里程碑

| 里程碑 | 目标日期 | 验收标准 |
|--------|----------|----------|
| 因子注册表上线 | 2026-04-30 | 支持动态注册 5+ 因子 |
| 动态权重运行 | 2026-05-31 | 每周自动调仓 |
| 数据收集完整 | 2026-06-30 | 100% 信号记录入库 |
| Q2 回测验证 | 2026-06-30 | Sharpe 提升 10%+ |

---

## 五、风险管理

### 5.1 技术风险

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| 架构变动引入 bug | 中 | 高 | 充分单元测试 + 灰度发布 |
| 性能下降 | 低 | 中 | 基准测试 + 性能监控 |
| 数据丢失 | 低 | 高 | 双写备份 + 定期导出 |

### 5.2 实施风险

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| 进度延期 | 中 | 中 | 分阶段交付 + 优先级管理 |
| 人员不足 | 中 | 高 | 明确分工 + 外部支持 |
| 效果不达预期 | 中 | 中 | 小步快跑 + 快速迭代 |

---

**文档版本**: v1.0  
**最后更新**: 2026-04-09  
**负责人**: 技术团队  
**下次评审**: 2026-04-30
