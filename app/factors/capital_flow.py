"""
资金流监控模块 (CapitalFlowMonitor) - 量价相关性增强版

核心理念:
"价格走势必须得到资金流向的确认。"
"关注聪明钱 (Smart Money) 的持续推动，而非单笔大单。"

功能:
1. 量价相关性 (Price-Volume Correlation): 
   使用 Pearson 相关系数判断上涨是否伴随放量 (正相关 -> 健康)。
2. 资金流趋势: EMA(Volume * PriceChange) 过滤单日噪音。
3. 量能激增检测: 突破日的量是否显著放大。

配置来源: unified_config.yaml -> strategy_enhancements.capital_flow
"""

import logging
import math
from typing import Dict, Any, Optional
from dataclasses import dataclass
from app.core.config_container import get_config

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    name: str = "CapitalFlowMonitor"
    passed: bool = True
    score: float = 0.0
    weight: float = 0.20
    reason: str = ""
    metadata: Dict = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class CapitalFlowMonitor:
    """资金流监控器"""

    def __init__(self, config_path: str = None):
        loader = get_config(config_path)
        cfg = loader.get('strategy_enhancements.capital_flow', {})
        self.enabled = cfg.get('enabled', True)
        self.vol_surge = cfg.get('volume_surge_factor', 2.0)
        self.corr_window = cfg.get('pv_correlation_window', 5)
        
        logger.info(
            f"[资金流监控] 已初始化 | "
            f"放量倍数={self.vol_surge}x | "
            f"相关性窗口={self.corr_window}天"
        )

    def check(self, symbol: str, history: Dict) -> GateResult:
        """
        检查资金流状态

        Args:
            symbol: 股票代码
            history: 历史数据 (close, volume) -> TwelveData 倒序

        Returns:
            GateResult 检测结果
        """
        if not self.enabled:
            return GateResult(name="CapitalFlowMonitor", passed=True, score=0.5, reason="Disabled")

        try:
            closes = history.get('close', [])
            volumes = history.get('volume', [])

            # DEB-20260903 #46: closes 与 volumes 均需校验窗口长度 —
            # 原仅校验 closes, volumes 较短时窗口切片错配 → 循环 volumes[i] 越界 IndexError
            if len(closes) < self.corr_window + 2 or len(volumes) < self.corr_window + 2:
                return GateResult(name="CapitalFlowMonitor", passed=False, score=0.0, reason="Insufficient Data")

            # 1. 计算量价相关性 (Pearson Correlation)
            # 我们需要 Price Change (%) 和 Volume Change (or just Volume) 的相关性
            # TwelveData 倒序: [Today, Yesterday, ...]
            
            # 取窗口数据
            window_closes = closes[:self.corr_window+1] # Need N+1 for N changes
            window_vols = volumes[:self.corr_window+1]

            # 计算变化率
            # Today (idx 0) change requires idx 1 (yesterday) close
            price_changes = []
            vol_changes = []
            
            for i in range(len(window_closes)-1):
                pc = (window_closes[i] - window_closes[i+1]) / window_closes[i+1] if window_closes[i+1] != 0 else 0
                vc = window_vols[i] # 直接用 Volume 绝对值，或者相对于均量的比
                
                price_changes.append(pc)
                vol_changes.append(vc)

            corr = self._pearson_corr(price_changes, vol_changes)

            # 2. 检查今日量能激增
            # DEB-20260903 #46: 均量不足时不得 fallback=1 — curr_vol/1 使 vol_ratio 虚增数百万倍
            if len(volumes) > 20:
                avg_vol = sum(volumes[1:21]) / 20
            elif len(volumes) > 1:
                avg_vol = sum(volumes[1:]) / (len(volumes) - 1)  # 可用窗口均值
            else:
                avg_vol = 0.0  # 均量不可用 → vol_ratio=0 (不放大)
            curr_vol = volumes[0]
            vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0

            result = GateResult(name="CapitalFlowMonitor", metadata={"pv_corr": corr, "vol_ratio": vol_ratio})

            # 3. 评分逻辑
            # 理想状态：上涨 (PriceChange > 0) 且 放量 (Vol > Avg) 且 正相关 (Corr > 0)
            # 注意：我们的 price_changes[0] 是 (Today - Yesterday) / Yesterday
            
            today_pc = price_changes[0] if price_changes else 0
            
            score = 0.5 # 中性基础分

            if corr > 0.5 and today_pc > 0:
                # 强正相关且上涨 -> 聪明钱买入
                score = min(1.0, 0.6 + (corr * 0.4))
                result.passed = True
                result.reason = f"资金推动上涨 (Corr={corr:.2f}, Vol={vol_ratio:.1f}x)"
            elif corr > 0.2 and vol_ratio >= self.vol_surge:
                # 量很大，相关性一般 -> 可能是消息刺激，给中等分
                score = 0.7
                result.passed = True
                result.reason = f"放量但相关性一般 (Vol={vol_ratio:.1f}x)"
            elif today_pc > 0 and vol_ratio < 0.8:
                # 缩量上涨 -> 动能不足，警惕
                score = 0.3
                result.passed = False
                result.reason = f"缩量上涨 (Vol={vol_ratio:.1f}x)，动能存疑"
            elif corr < -0.5 and today_pc > 0:
                # 负相关且上涨 (价涨量缩，或价跌量涨) -> 背离
                # 如果今天是涨的，但相关性是负的，说明之前的下跌伴随放量 -> 危险
                score = 0.2
                result.passed = False
                result.reason = f"量价背离 (Corr={corr:.2f})，警惕出货"
            else:
                # 其他情况
                score = 0.4
                result.reason = f"资金信号不明显 (Corr={corr:.2f})"

            result.score = score
            return result

        except Exception as e:
            logger.warning(f"[资金流监控] {symbol} 检测异常: {e}")
            # DEB-20260903 #47: 与其他四门对齐 fail-open —
            # 原 fail-closed 导致资金流门单点异常即阻断所有交易
            return GateResult(name="CapitalFlowMonitor", passed=True, score=0.5, reason=f"Error(降级放行): {e}")

    def _pearson_corr(self, x, y):
        """计算皮尔逊相关系数"""
        n = len(x)
        if n == 0: return 0.0
        
        sum_x = sum(x)
        sum_y = sum(y)
        sum_xy = sum(a*b for a, b in zip(x, y))
        sum_x2 = sum(a*a for a in x)
        sum_y2 = sum(b*b for b in y)
        
        num = (n * sum_xy) - (sum_x * sum_y)
        den = math.sqrt((n * sum_x2 - sum_x**2) * (n * sum_y2 - sum_y**2))
        
        return num / den if den != 0 else 0.0
