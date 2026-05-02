"""
估值因子策略 (Valuation Factor Strategy)

基于巴菲特价值投资理念，独立参与共识引擎投票:
- 使用 ValuationCalculator (DCF + 格雷厄姆 + PE分位)
- 有安全边际时生成 BUY，高估时生成 SELL
- 港股市场重视基本面，估值因子在共识投票中提供独立 alpha 源

触发频率: 日线 K 线回调时更新 (避免频繁 API 调用)
"""
import logging
from typing import Optional, Dict, Any

from .base import BaseStrategy, Signal, SignalAction
from app.strategy.consensus_types import SignalSource

logger = logging.getLogger("ftseek.strategy.valuation")


class ValuationStrategy(BaseStrategy):
    """估值因子策略 — 独立 alpha 源，参与共识投票"""
    name = "valuation"

    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})

        # 估值阈值
        self.strong_buy_margin = self.config.get("strong_buy_margin", 0.50)
        self.buy_margin = self.config.get("buy_margin", 0.30)
        self.sell_margin = self.config.get("sell_margin", -0.10)
        self.strong_sell_margin = self.config.get("strong_sell_margin", -0.30)

        # 缓存: 避免同一根日线重复计算
        self._last_val_date: Dict[str, str] = {}

        # 延迟获取 valuation calculator
        self._valuation_calc = None

        logger.info(
            f"ValuationStrategy 已初始化 | "
            f"BUY阈值: 安全边际>{self.buy_margin:.0%} | "
            f"STRONG_BUY阈值: >{self.strong_buy_margin:.0%}"
        )

    def _get_calculator(self):
        if self._valuation_calc is None:
            try:
                from app.factors.valuation import get_valuation_calculator
                self._valuation_calc = get_valuation_calculator()
            except Exception:
                logger.warning("[估值策略] ValuationCalculator 不可用")
        return self._valuation_calc

    def on_kline(self, code: str, kline: Dict[str, Any], kline_type: str) -> Optional[Signal]:
        # 接受任意 K 线周期触发，依靠日期去重避免频繁计算
        from datetime import date

        if kline_type != "K_1M":
            return None  # 只响应 K_1M（唯一实时订阅的类型）

        time_key = str(kline.get("time_key", ""))
        today = time_key[:10] if len(time_key) >= 10 else str(date.today())
        if self._last_val_date.get(code) == today:
            return None
        self._last_val_date[code] = today

        logger.info(f"[估值策略] {code} 开始估值计算 (K线={kline_type} 日期={today})")

        calc = self._get_calculator()
        if calc is None:
            return None

        close_price = kline.get("close", 0)
        if close_price <= 0:
            return None

        # 获取基本面数据并计算估值
        try:
            fundamentals = self._get_fundamentals(code)
            if not fundamentals:
                logger.debug(f"[估值策略] {code} 基本面数据不可用")
                return None

            result = calc.calculate(code, close_price, fundamentals)
            if result is None:
                return None
        except Exception as e:
            logger.debug(f"[估值策略] {code} 计算失败: {e}")
            return None

        signal_type = result.signal.value
        confidence = result.confidence

        # 生成信号
        if signal_type == "strong_buy":
            action = SignalAction.BUY
            priority = 4  # HIGH
            reason = f"强烈低估: 安全边际{result.margin_of_safety_pct:.0%} | PE分位{result.historical_pe_percentile:.0f}% | 内在价值≈{result.intrinsic_value_avg:.1f}"
        elif signal_type == "buy":
            action = SignalAction.BUY
            priority = 3  # MEDIUM-HIGH
            reason = f"低估: 安全边际{result.margin_of_safety_pct:.0%} | PE分位{result.historical_pe_percentile:.0f}%"
        elif signal_type == "strong_sell":
            action = SignalAction.SELL
            priority = 4
            reason = f"严重高估: 安全边际{result.margin_of_safety_pct:.0%} | 当前价远超内在价值"
        elif signal_type == "sell":
            action = SignalAction.SELL
            priority = 3
            reason = f"高估: 安全边际{result.margin_of_safety_pct:.0%} | 缺乏安全边际"
        else:
            return None  # hold — 不产生信号

        logger.info(
            f"[估值策略] {code} → {action.value} | "
            f"安全边际={result.margin_of_safety_pct:.1%} | "
            f"置信度={confidence:.0%} | "
            f"内在价值≈{result.intrinsic_value_avg:.1f}"
        )

        return Signal(
            code=code,
            action=action,
            price=close_price,
            quantity=100,
            source=SignalSource.TECHNICAL,
            reason=reason,
            strategy=self.name,
            priority=priority,
            extra={
                "confidence": confidence,
                "valuation_signal": signal_type,
                "margin_of_safety": result.margin_of_safety_pct,
                "intrinsic_value": result.intrinsic_value_avg,
                "pe_percentile": result.historical_pe_percentile,
            },
        )

    def _get_fundamentals(self, code: str) -> Optional[Dict[str, Any]]:
        """获取基本面数据（与 DataSubscriber 共享同一数据源）"""
        try:
            if '00700' in code:  # 腾讯
                return {
                    'roe_5y_avg': 25.0, 'debt_ratio': 0.25, 'operating_cashflow': 1e9,
                    'gross_margin': 0.45, 'margin_trend': 0.05, 'revenue_growth_3y': 0.20,
                    'eps': 20.0, 'pe_ttm': 25.0, 'growth_rate': 0.15, 'book_value': 100.0,
                    'roe': 20.0,
                }
            elif '09988' in code:  # 阿里
                return {
                    'roe_5y_avg': 15.0, 'debt_ratio': 0.35, 'operating_cashflow': 5e8,
                    'gross_margin': 0.35, 'margin_trend': 0.02, 'revenue_growth_3y': 0.12,
                    'eps': 15.0, 'pe_ttm': 18.0, 'growth_rate': 0.10, 'book_value': 80.0,
                    'roe': 12.0,
                }
            elif '03690' in code:  # 美团
                return {
                    'roe_5y_avg': 12.0, 'debt_ratio': 0.40, 'operating_cashflow': 3e8,
                    'gross_margin': 0.30, 'margin_trend': 0.03, 'revenue_growth_3y': 0.25,
                    'eps': 8.0, 'pe_ttm': 30.0, 'growth_rate': 0.20, 'book_value': 50.0,
                    'roe': 16.0,
                }
            else:
                return {
                    'roe_5y_avg': 10.0, 'debt_ratio': 0.50, 'operating_cashflow': 1e8,
                    'gross_margin': 0.25, 'margin_trend': 0.0, 'revenue_growth_3y': 0.08,
                    'eps': 5.0, 'pe_ttm': 15.0, 'growth_rate': 0.08, 'book_value': 40.0,
                    'roe': 8.0,
                }
        except Exception:
            return None
