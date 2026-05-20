"""
信号生成器

从"布尔值"升级为"评分值"的起点

P1 模块化重构 (2026-04-06):
- 支持从新策略模块加载策略
- 保留原有接口以确保向后兼容
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path

# 添加父目录到路径
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.models import SignalScore, Direction, MarketRegime, TradeEvent
from app.core.events import EventStore

# P1 新增：导入策略模块
try:
    from app.decision.strategies import get_strategy, list_strategies, BaseStrategy
    STRATEGIES_MODULE_AVAILABLE = True
except ImportError:
    STRATEGIES_MODULE_AVAILABLE = False

logger = logging.getLogger(__name__)


class SignalGenerator:
    """
    信号生成器
    
    接收行情数据，生成原始信号
    """
    
    def __init__(self, config: Dict[str, Any], event_store: EventStore):
        self.config = config
        self.event_store = event_store
        self.strategies = {}
        self.signal_history: Dict[str, List[SignalScore]] = {}
    
    def register_strategy(self, strategy_id: str, strategy_func):
        """注册策略（支持两种方式）"""
        self.strategies[strategy_id] = strategy_func
        logger.info(f"注册策略：{strategy_id}")

    def load_builtin_strategies(self):
        """
        P1 新增：加载内置策略模块中的所有策略

        自动发现并注册所有使用 @register_strategy 装饰器的策略
        """
        if not STRATEGIES_MODULE_AVAILABLE:
            logger.warning("策略模块不可用，跳过自动加载")
            return

        strategies = list_strategies()
        for strategy_info in strategies:
            strategy_name = strategy_info["name"]
            if strategy_name not in self.strategies:
                strategy = get_strategy(strategy_name)
                if strategy:
                    # 包装策略 execute 方法以适配原有接口
                    def make_wrapper(s):
                        def wrapper(symbol, data):
                            return s.execute(symbol, data)
                        return wrapper
                    self.strategies[strategy_name] = make_wrapper(strategy)
                    logger.info(f"[策略模块] 加载策略：{strategy_name} v{strategy_info['version']}")
    
    def generate_signal(
        self,
        symbol: str,
        strategy_id: str,
        market_data: Dict[str, Any],
    ) -> Optional[SignalScore]:
        """
        生成信号
        
        Args:
            symbol: 股票代码
            strategy_id: 策略 ID
            market_data: 市场数据 (价格/成交量/指标等)
        
        Returns:
            SignalScore 或 None (无信号)
        """
        if strategy_id not in self.strategies:
            logger.error(f"未知策略：{strategy_id}")
            return None
        
        try:
            # 调用策略生成信号
            strategy_func = self.strategies[strategy_id]
            signal_data = strategy_func(symbol, market_data)
            
            if signal_data is None:
                return None
            
            # 创建信号对象
            signal = SignalScore(
                strategy_id=strategy_id,
                symbol=symbol,
                direction=Direction(signal_data.get("direction", "LONG")),
                strength=signal_data.get("strength", 50),
                confidence=signal_data.get("confidence", 0.5),
                expected_horizon=signal_data.get("expected_horizon", 60),
                volatility_fit=signal_data.get("volatility_fit", True),
                market_regime=MarketRegime(signal_data.get("market_regime", "RANGE")),
                risk_reward_estimate=signal_data.get("risk_reward", 2.0),
                score_breakdown=signal_data.get("score_breakdown", {}),
            )
            
            # 设置过期时间
            expire_seconds = self.config.get("SIGNAL_EXPIRE_SECONDS", 300)
            signal.expires_at = datetime.now() + timedelta(seconds=expire_seconds)
            
            # 记录信号生成事件
            self.event_store.append(
                TradeEvent(
                    event_type="signal_generated",
                    signal_id=signal.signal_id,
                    data={
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "direction": signal.direction.value,
                        "strength": signal.strength,
                    },
                )
            )
            
            # 保存到历史
            if symbol not in self.signal_history:
                self.signal_history[symbol] = []
            self.signal_history[symbol].append(signal)
            
            logger.info(f"生成信号：{symbol} {signal.direction.value} strength={signal.strength}")
            return signal
            
        except Exception as e:
            logger.error(f"信号生成失败：{e}")
            return None
    
    def get_signal_history(self, symbol: str, limit: int = 20) -> List[SignalScore]:
        """获取信号历史"""
        if symbol not in self.signal_history:
            return []
        return self.signal_history[symbol][-limit:]
    
    def get_strategy_performance(
        self,
        strategy_id: str,
        symbol: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        获取策略历史表现
        
        用于信号评分时参考历史胜率
        """
        signals = []
        
        if symbol:
            # 特定股票的策略表现
            if symbol in self.signal_history:
                signals = [
                    s for s in self.signal_history[symbol]
                    if s.strategy_id == strategy_id
                ][-limit:]
        else:
            # 所有股票的策略表现
            for sym_signals in self.signal_history.values():
                signals.extend([
                    s for s in sym_signals if s.strategy_id == strategy_id
                ][-limit:])
        
        if not signals:
            return {
                "total_signals": 0,
                "win_rate": 0.0,
                "avg_return": 0.0,
                "max_drawdown": 0.0,
            }
        
        # 简单统计 (实际需要结合成交和盈亏数据)
        total = len(signals)
        avg_strength = sum(s.strength for s in signals) / total
        avg_confidence = sum(s.confidence for s in signals) / total
        
        return {
            "total_signals": total,
            "avg_strength": avg_strength,
            "avg_confidence": avg_confidence,
            "recent_signals": signals[-5:],
        }


# ============================================================
# P1 迁移提示：示例策略已移至 app/decision/strategies/price_break.py
# ============================================================
# 旧代码保留以确保向后兼容，建议使用新策略模块
def example_price_break_strategy(symbol: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    示例：价格突破策略（已弃用）

    ⚠️ P1 提示：请使用 app.decision.strategies.PriceBreakStrategy
    """
    from app.decision.strategies import get_strategy

    strategy = get_strategy("price_break")
    if strategy:
        return strategy.execute(symbol, data)

    # Fallback 到旧实现
    current_price = data.get("current_price", 0)
    resistance = data.get("resistance", 0)
    volume_ratio = data.get("volume_ratio", 1.0)

    if current_price > resistance and volume_ratio > 1.5:
        return {
            "direction": "LONG",
            "strength": 70,
            "confidence": 0.7,
            "expected_horizon": 120,
            "volatility_fit": True,
            "market_regime": "TREND_UP",
            "risk_reward": 2.5,
            "score_breakdown": {
                "breakout": 30,
                "volume_confirm": 20,
                "trend_align": 20,
            }
        }

    return None
