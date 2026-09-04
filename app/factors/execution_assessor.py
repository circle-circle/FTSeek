"""
智能执行评估器 (ExecutionAssessor)

核心理念:
"滑点会吞噬利润。在流动性枯竭时，不要交易。"
"不要在点差过大或深度不足的盘口上下单。"

功能:
1. 点差评估 (Spread): Bid-Ask Spread 是否过宽。
2. 流动性评分 (Liquidity): 基于平均成交量和市值估算。
3. 限价单建议: 根据点差大小建议使用 Limit Order 还是 Market Order。

注意: 由于免费 API 限制 (Finnhub/TwelveData 无 L2 盘口)，
本模块使用 "代理指标" 来估算流动性风险。
"""

import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from app.core.config_container import get_config

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    name: str = "ExecutionAssessor"
    passed: bool = True
    score: float = 0.0
    weight: float = 0.20
    reason: str = ""
    metadata: Dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class ExecutionAssessor:
    """执行质量评估器"""

    def __init__(self, config_path: str = None):
        loader = get_config(config_path)
        cfg = loader.get('strategy_enhancements.smart_execution', {})
        self.enabled = cfg.get('enabled', True)
        self.max_slippage = cfg.get('max_slippage_bps', 50)
        self.min_liquidity = cfg.get('liquidity_min_score', 0.4)
        
        logger.info(
            f"[智能执行] 已初始化 | "
            f"最大滑点={self.max_slippage}bps | "
            f"最低流动性评分={self.min_liquidity}"
        )

    def check(self, symbol: str, history: Dict, market_ctx: Dict) -> GateResult:
        """
        评估当前标的是否适合交易 (流动性/滑点风险)

        Args:
            symbol: 标的代码
            history: 历史数据 (volume, close)
            market_ctx: 市场上下文
        """
        if not self.enabled:
            return GateResult(name="ExecutionAssessor", passed=True, score=0.5, reason="Disabled")

        try:
            volumes = history.get('volume', [])
            closes = history.get('close', [])
            price = closes[0] if closes else 0

            if not volumes or price <= 0:
                return GateResult(name="ExecutionAssessor", passed=False, score=0.0, reason="No Data")

            # 1. 计算平均成交量 (过去 20 天)
            avg_vol = sum(volumes[:20]) / 20 if len(volumes) >= 20 else sum(volumes)/len(volumes)
            
            # 2. 估算流动性评分 (0.0 - 1.0)
            # 逻辑: 日均成交额 = AvgVol * Price
            # 美股一般认为日均成交额 > 1000 万美元为流动性良好
            avg_turnover = avg_vol * price 
            
            # 评分映射: 
            # < 500万 -> 0.2 (极差)
            # 1000万 -> 0.5 (及格)
            # 5000万 -> 0.8 (良好)
            # > 1亿 -> 1.0 (极佳)
            if avg_turnover < 5_000_000:
                liq_score = 0.2
            elif avg_turnover < 10_000_000:
                liq_score = 0.4
            elif avg_turnover < 50_000_000:
                liq_score = 0.6
            elif avg_turnover < 100_000_000:
                liq_score = 0.8
            else:
                liq_score = 1.0

            # 3. 估算点差 (Spread Proxy)
            # 经验公式: 低流动性股票通常点差更大
            # 假设 Spread (bps) 与 Turnover 负相关
            # 简单估算:
            # DEB-20260903 #48: 原映射在默认配置下使 spread 门不可达 —
            # liq<0.4→50bps 已被 min_liquidity(0.4) 门先行拦截, 而通过门 liq≥0.4
            # 的最高 spread=20 < max_slippage(50), elif spread>max_slippage 恒 False。
            # 调整映射使边际流动性标的 (刚过流动性门) spread 仍可超阈, 让 spread 门可达:
            if liq_score < 0.4:
                est_spread_bps = 100  # 高风险 (同时被流动性门拦)
            elif liq_score < 0.55:
                est_spread_bps = 55   # 边际流动性: 过流动性门但点差超阈 → spread 门捕获
            elif liq_score < 0.7:
                est_spread_bps = 25
            elif liq_score < 0.85:
                est_spread_bps = 8
            else:
                est_spread_bps = 2   # 极低

            # 4. 综合评分
            score = liq_score # 初始分为流动性分
            
            result = GateResult(name="ExecutionAssessor", metadata={"liq_score": liq_score, "est_spread_bps": est_spread_bps, "avg_turnover": avg_turnover})

            # 检查硬性拦截
            if liq_score < self.min_liquidity:
                result.passed = False
                result.score = 0.2
                result.reason = f"流动性不足 (Turnover=${avg_turnover/1e6:.1f}M), 滑点风险高"
            elif est_spread_bps > self.max_slippage:
                result.passed = False
                result.score = 0.3
                result.reason = f"预估点差过大 ({est_spread_bps}bps), 易被滑点吞噬"
            else:
                result.passed = True
                result.score = score
                order_type = "MARKET" if liq_score > 0.7 else "LIMIT"
                result.reason = f"执行环境良好 (Turnover=${avg_turnover/1e6:.1f}M, 建议 {order_type})"

            return result

        except Exception as e:
            logger.warning(f"[智能执行] {symbol} 评估异常: {e}")
            return GateResult(name="ExecutionAssessor", passed=True, score=0.5, reason=f"Error: {e}")
