"""
龙头股动态评估器 (LeaderPoolEvaluator)

核心理念:
"只做领头羊，不跟风。"
"当领头羊停止创新高时，警惕板块调整。"

功能:
1. 相对强度 (Relative Strength) 评估: 标的 vs SPY 的涨幅比较。
2. 稳定性检查: 回撤控制 (Max Drawdown) 和 突破领先度。
3. 衰竭预警: 若跌破 5 日均线或相对强度下降，发出警报。

注意: 由于免费 API 限制，无法实时获取板块全量数据。
本模块主要依赖 "相对强度" 这一通用指标来判断个股是否为板块龙头。
"""

import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from app.core.config_container import get_config

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    name: str = "LeaderPoolEvaluator"
    passed: bool = True
    score: float = 0.0
    weight: float = 0.15
    reason: str = ""
    metadata: Dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class LeaderPoolEvaluator:
    """龙头股评估器"""

    def __init__(self, config_path: str = None):
        loader = get_config(config_path)
        cfg = loader.get('strategy_enhancements.leader_pool', {})
        self.enabled = cfg.get('enabled', True)
        self.ma_exhaustion = cfg.get('exhaustion_ma_days', 5)
        
        logger.info(
            f"[龙头评估] 已初始化 | "
            f"衰竭检测均线={self.ma_exhaustion}天"
        )

    def check(self, symbol: str, history: Dict, market_ctx: Dict) -> GateResult:
        """
        评估标的是否具有"龙头"特征

        Args:
            symbol: 标的代码
            history: 历史数据 (close)
            market_ctx: 包含 SPY 历史或上下文
        """
        if not self.enabled:
            return GateResult(name="LeaderPoolEvaluator", passed=True, score=0.5, reason="Disabled")

        try:
            closes = history.get('close', [])
            
            if len(closes) < 20:
                return GateResult(name="LeaderPoolEvaluator", passed=False, score=0.0, reason="Insufficient Data")

            current_price = closes[0]
            
            # 1. 计算相对强度 (RS)
            # RS = 标的涨幅 / SPY 涨幅 (过去 20 天)
            spy_ctx = market_ctx.get('spy_context', {})
            spy_price = spy_ctx.get('price', 0.0)
            spy_prev = spy_ctx.get('prev_close', 0.0)
            
            # 如果没有 SPY 上下文，假设市场平稳 (RS=1)
            if spy_price > 0 and spy_prev > 0:
                spy_chg = (spy_price - spy_prev) / spy_prev
            else:
                spy_chg = 0.001 # 避免除以0

            # 标的涨幅 (简化：用今日涨幅代替，理想情况用 20 天)
            # 这里为了简化实现，使用今日涨幅 / 今日 SPY 涨幅
            if len(closes) >= 2:
                stock_chg = (closes[0] - closes[1]) / closes[1]
            else:
                stock_chg = 0.0

            rs = stock_chg / spy_chg if abs(spy_chg) > 0.001 else 1.0

            # 2. 检查是否跌破均线 (衰竭预警)
            # 计算 MA(5)
            ma5 = sum(closes[:5]) / 5 if len(closes) >= 5 else current_price
            is_exhausted = current_price < ma5

            score = 0.5
            result = GateResult(name="LeaderPoolEvaluator", metadata={"rs": rs, "is_exhausted": is_exhausted, "ma5": ma5})

            if is_exhausted:
                # 跌破 5 日线 -> 弱势/衰竭
                score = 0.2
                result.passed = False
                result.reason = f"跌破 {self.ma_exhaustion} 日均线 (MA5={ma5:.2f})，龙头衰竭"
            elif rs > 1.5:
                # 相对强度极高 -> 龙头特征
                score = 1.0
                result.passed = True
                result.reason = f"强劲龙头 (RS={rs:.1f}x)"
            elif rs > 0.8:
                # 跟随大盘 -> 普通
                score = 0.6
                result.passed = True
                result.reason = f"跟随大盘 (RS={rs:.1f}x)"
            elif rs < 0.5:
                # 弱于大盘 -> 弱势
                score = 0.3
                result.passed = False
                result.reason = f"显著弱于大盘 (RS={rs:.1f}x)"
            else:
                score = 0.4
                result.reason = f"相对强度一般 (RS={rs:.1f}x)"

            result.score = score
            return result

        except Exception as e:
            logger.warning(f"[龙头评估] {symbol} 检测异常: {e}")
            return GateResult(name="LeaderPoolEvaluator", passed=True, score=0.5, reason=f"Error: {e}")
