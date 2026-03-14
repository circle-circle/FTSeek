"""
动态仓位管理模块

根据信号质量、市场环境和近期表现动态调整仓位
"""
import logging
from typing import Dict, Optional
from datetime import datetime, timedelta

logger = logging.getLogger("ftseek.risk.dynamic_position_sizer")


class DynamicPositionSizer:
    """动态仓位计算器"""
    
    def __init__(self):
        # 基础配置
        self.base_position = 0.10  # 基础仓位 10%
        self.min_position = 0.05   # 最小仓位 5%
        self.max_position = 0.30   # 最大仓位 30%
        
        # 近期交易记录 (用于计算胜率)
        self.recent_trades = []
    
    def add_trade_result(self, pnl: float, timestamp: str):
        """添加交易结果"""
        self.recent_trades.append({
            'pnl': pnl,
            'timestamp': timestamp
        })
        
        # 只保留最近 30 天的交易
        cutoff = datetime.now() - timedelta(days=30)
        self.recent_trades = [
            t for t in self.recent_trades
            if datetime.fromisoformat(t['timestamp']) > cutoff
        ]
    
    def calculate_position(self,
                          signal_confidence: float,
                          market_regime: str = 'sideways',
                          ai_score: float = 0.0,
                          volatility: float = 0.0) -> float:
        """
        计算动态仓位
        
        Args:
            signal_confidence: 信号置信度 (0-1)
            market_regime: 市场环境 (uptrend/downtrend/sideways)
            ai_score: AI 预测分数 (-1 到 1)
            volatility: 波动率 (0-1)
        
        Returns:
            float: 建议仓位 (0.05-0.30)
        """
        position = self.base_position
        
        # 1. 信号置信度调整 (0.5-1.5x)
        confidence_multiplier = 0.5 + signal_confidence
        position *= confidence_multiplier
        
        # 2. 市场环境调整
        regime_multipliers = {
            'uptrend': 1.2,      # 上涨市场加仓
            'sideways': 1.0,     # 震荡市场正常
            'downtrend': 0.5     # 下跌市场减仓
        }
        regime_multiplier = regime_multipliers.get(market_regime, 1.0)
        position *= regime_multiplier
        
        # 3. AI 预测调整
        if ai_score > 0.5:
            position *= 1.2  # AI 强烈看好
        elif ai_score < -0.5:
            position *= 0.5  # AI 强烈看空
        
        # 4. 近期表现调整
        recent_win_rate = self._get_recent_win_rate()
        if recent_win_rate > 0.60:
            position *= 1.2  # 近期表现好
        elif recent_win_rate < 0.40:
            position *= 0.5  # 近期表现差
        
        # 5. 波动率调整 (高波动降低仓位)
        if volatility > 0.05:  # 日波动 > 5%
            position *= 0.7
        elif volatility > 0.03:  # 日波动 > 3%
            position *= 0.85
        
        # 限制在最小和最大仓位之间
        position = max(self.min_position, min(self.max_position, position))
        
        logger.info(
            f"动态仓位计算：置信度={signal_confidence:.2f}, "
            f"市场={market_regime}, AI={ai_score:.2f}, "
            f"近期胜率={recent_win_rate:.2f}, 波动率={volatility:.2f}"
        )
        logger.info(f"建议仓位：{position:.2%}")
        
        return position
    
    def _get_recent_win_rate(self) -> float:
        """计算近期胜率"""
        if not self.recent_trades:
            return 0.5  # 默认 50%
        
        winning_trades = [t for t in self.recent_trades if t['pnl'] > 0]
        return len(winning_trades) / len(self.recent_trades)
    
    def get_position_explanation(self,
                                signal_confidence: float,
                                market_regime: str,
                                ai_score: float,
                                volatility: float) -> Dict:
        """获取仓位计算详细说明"""
        position = self.base_position
        
        explanations = {
            'base_position': f'{self.base_position:.2%}',
            'adjustments': []
        }
        
        # 置信度调整
        confidence_multiplier = 0.5 + signal_confidence
        position *= confidence_multiplier
        explanations['adjustments'].append({
            'factor': '信号置信度',
            'multiplier': f'{confidence_multiplier:.2f}x',
            'reason': f'置信度 {signal_confidence:.2f}'
        })
        
        # 市场环境调整
        regime_multipliers = {
            'uptrend': 1.2,
            'sideways': 1.0,
            'downtrend': 0.5
        }
        regime_multiplier = regime_multipliers.get(market_regime, 1.0)
        position *= regime_multiplier
        explanations['adjustments'].append({
            'factor': '市场环境',
            'multiplier': f'{regime_multiplier:.2f}x',
            'reason': market_regime
        })
        
        # AI 预测调整
        if ai_score > 0.5:
            ai_multiplier = 1.2
        elif ai_score < -0.5:
            ai_multiplier = 0.5
        else:
            ai_multiplier = 1.0
        
        position *= ai_multiplier
        explanations['adjustments'].append({
            'factor': 'AI 预测',
            'multiplier': f'{ai_multiplier:.2f}x',
            'reason': f'AI 分数 {ai_score:.2f}'
        })
        
        # 近期表现调整
        recent_win_rate = self._get_recent_win_rate()
        if recent_win_rate > 0.60:
            perf_multiplier = 1.2
        elif recent_win_rate < 0.40:
            perf_multiplier = 0.5
        else:
            perf_multiplier = 1.0
        
        position *= perf_multiplier
        explanations['adjustments'].append({
            'factor': '近期表现',
            'multiplier': f'{perf_multiplier:.2f}x',
            'reason': f'胜率 {recent_win_rate:.2%}'
        })
        
        # 波动率调整
        if volatility > 0.05:
            vol_multiplier = 0.7
        elif volatility > 0.03:
            vol_multiplier = 0.85
        else:
            vol_multiplier = 1.0
        
        position *= vol_multiplier
        explanations['adjustments'].append({
            'factor': '波动率',
            'multiplier': f'{vol_multiplier:.2f}x',
            'reason': f'波动率 {volatility:.2%}'
        })
        
        # 最终仓位
        final_position = max(self.min_position, min(self.max_position, position))
        explanations['final_position'] = f'{final_position:.2%}'
        explanations['before_limits'] = f'{position:.2%}'
        
        if final_position != position:
            explanations['limited_by'] = 'min' if final_position == self.min_position else 'max'
        
        return explanations


# 全局单例
_sizer = None

def get_dynamic_position_sizer() -> DynamicPositionSizer:
    """获取动态仓位计算器单例"""
    global _sizer
    if _sizer is None:
        _sizer = DynamicPositionSizer()
    return _sizer
