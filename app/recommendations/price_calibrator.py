"""推荐价格校准器 (Task #37, 2026-05-28)

在推送推荐前，基于 MarketDataRouter 实时报价校准 entry_zone：
  - 常规日: ±2% 区间
  - 财报日: -3% / +5% 区间 (容忍跳空波动)

若获取行情失败或被禁用，保留原始 entry_zone 并打 calibration_failed 标记，
不阻塞下游推送链路。

公开 API:
    from app.recommendations.price_calibrator import PriceCalibrator
    calibrator = PriceCalibrator()
    recommendations = calibrator.calibrate(recommendations)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.utils.timezone import ET, now_et

logger = logging.getLogger(__name__)


class PriceCalibrator:
    """推荐价格校准器 - 基于最新行情校准入场区间"""

    REGULAR_LOW_PCT = 0.98
    REGULAR_HIGH_PCT = 1.02
    EARNINGS_LOW_PCT = 0.97
    EARNINGS_HIGH_PCT = 1.05

    DEFAULT_DEVIATION_ALERT_PCT = 5.0

    def __init__(self, router: Any = None, config: Optional[Dict[str, Any]] = None):
        """
        Args:
            router: 可注入的 MarketDataRouter 实例 (便于测试)；缺省时按需懒加载
            config: 可注入的配置字典 (便于测试)；缺省时从 unified_config 读取
        """
        self._router = router
        self._config_override = config

        cfg = self._load_config()
        self.enabled: bool = bool(cfg.get("enabled", True))
        try:
            self.deviation_alert_pct: float = float(
                cfg.get("deviation_alert_pct", self.DEFAULT_DEVIATION_ALERT_PCT)
            )
        except (TypeError, ValueError):
            self.deviation_alert_pct = self.DEFAULT_DEVIATION_ALERT_PCT

    # ── 配置 ──────────────────────────────────────────────────────────

    def _load_config(self) -> Dict[str, Any]:
        if self._config_override is not None:
            return dict(self._config_override)
        try:
            from app.core.config_container import get_config

            cfg = get_config()
            section = cfg.get_dotted("recommendations.price_calibration", {}) or {}
            if isinstance(section, dict):
                return section
        except Exception as e:  # pragma: no cover
            logger.debug(f"[PriceCalibrator] 配置读取失败，使用默认值: {e}")
        return {}

    # ── Router 懒加载 ─────────────────────────────────────────────────

    def _get_router(self) -> Any:
        if self._router is not None:
            return self._router
        try:
            from app.data.market_data_router import MarketDataRouter

            self._router = MarketDataRouter()
        except Exception as e:
            logger.warning(f"[PriceCalibrator] MarketDataRouter 初始化失败: {e}")
            self._router = None
        return self._router

    def _fetch_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        router = self._get_router()
        if router is None:
            return None
        try:
            quote = router.get_quote(symbol)
            if quote and float(quote.get("price", 0) or 0) > 0:
                return quote
        except Exception as e:
            logger.warning(f"[PriceCalibrator] 获取 {symbol} 报价失败: {e}")
        return None

    # ── 工具函数 ──────────────────────────────────────────────────────

    @staticmethod
    def _normalize_entry(entry: Any) -> Optional[Tuple[float, float]]:
        """同时支持 list [low, high] 与 dict {low, high} 两种格式"""
        try:
            if isinstance(entry, dict):
                low = entry.get("low")
                high = entry.get("high")
                if low is None or high is None:
                    return None
                lo, hi = float(low), float(high)
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                lo, hi = float(entry[0]), float(entry[1])
            else:
                return None
        except (TypeError, ValueError):
            return None
        if lo > hi:
            lo, hi = hi, lo
        return lo, hi

    def _calibrate_entry_zone(
        self,
        symbol: str,
        original_entry: Any,
        current_price: float,
        is_earnings_day: bool = False,
    ) -> Dict[str, Any]:
        if is_earnings_day:
            new_low = current_price * self.EARNINGS_LOW_PCT
            new_high = current_price * self.EARNINGS_HIGH_PCT
        else:
            new_low = current_price * self.REGULAR_LOW_PCT
            new_high = current_price * self.REGULAR_HIGH_PCT

        norm = self._normalize_entry(original_entry)
        original_low = norm[0] if norm else None
        original_high = norm[1] if norm else None

        return {
            "low": round(new_low, 2),
            "high": round(new_high, 2),
            "calibrated_at": now_et().isoformat(),
            "original_low": original_low,
            "original_high": original_high,
            "current_price": round(float(current_price), 4),
            "is_earnings_day": bool(is_earnings_day),
        }

    def _check_deviation(
        self, original_entry: Any, current_price: float
    ) -> Optional[str]:
        """若原始 entry_zone 中点与实时价偏离超过阈值，返回告警信息"""
        norm = self._normalize_entry(original_entry)
        if norm is None:
            return None
        lo, hi = norm
        mid = (lo + hi) / 2.0
        if mid <= 0:
            return None
        deviation = abs(current_price - mid) / mid * 100.0
        if deviation > self.deviation_alert_pct:
            return (
                f"价格偏离 {deviation:.1f}% "
                f"(原始中点 {mid:.2f}, 当前 {current_price:.2f})"
            )
        return None

    # ── 主入口 ────────────────────────────────────────────────────────

    def calibrate(
        self,
        recommendations: Dict[str, Any],
        market_data: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """在推送前校准所有推荐的 entry_zone

        Args:
            recommendations: RecommendationSynthesizer 输出的字典。
                顶层可以是:
                  - {"recommendations": [item, ...]}
                  - {"items": [...]} / {"targets": [...]} / {"results": [...]}
                  - {"AAPL": {...}, "NVDA": {...}}  (按 symbol 索引)
                每个 item 期望含 symbol/ticker 与 entry_zone (list 或 dict).
            market_data: 可选预取行情 {symbol: {"price": float, "change_pct": float}}.
                未命中时回退至 MarketDataRouter.get_quote().

        Returns:
            校准后的 recommendations (原地修改 + 添加校准信息).
        """
        if recommendations is None:
            return recommendations
        if not self.enabled:
            logger.info(
                "[PriceCalibrator] 已禁用 (recommendations.price_calibration.enabled=false)"
            )
            return recommendations

        items, _container = self._extract_items(recommendations)
        if not items:
            logger.debug("[PriceCalibrator] 未发现可校准的推荐项")
            return recommendations

        success_cnt = 0
        failed_cnt = 0
        alert_cnt = 0
        market_data = market_data or {}

        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = self._extract_symbol(item)
            if not symbol:
                continue

            try:
                quote = self._lookup_quote(symbol, market_data)
                if not quote or float(quote.get("price", 0) or 0) <= 0:
                    item["entry_zone_calibrated"] = {
                        "calibration_failed": True,
                        "reason": "quote_unavailable",
                        "calibrated_at": now_et().isoformat(),
                    }
                    failed_cnt += 1
                    continue

                current_price = float(quote["price"])
                original_entry = item.get("entry_zone")
                is_earnings_day = bool(
                    item.get("is_earnings_day")
                    or item.get("earnings_today")
                    or str(item.get("catalyst") or "").lower().startswith("earnings")
                )

                calibrated = self._calibrate_entry_zone(
                    symbol, original_entry, current_price, is_earnings_day
                )
                deviation_alert = self._check_deviation(original_entry, current_price)
                calibrated["deviation_alert"] = deviation_alert
                if deviation_alert:
                    alert_cnt += 1
                    logger.warning(f"[PriceCalibrator] {symbol} {deviation_alert}")

                item["entry_zone_calibrated"] = calibrated
                # 顶层显示用 entry_zone 字符串 (供推送渲染)
                item["entry_zone_display"] = (
                    f"${calibrated['low']:.2f} - ${calibrated['high']:.2f}"
                )
                # 同步覆盖列表/字典格式的 entry_zone, 保持下游一致
                if isinstance(original_entry, dict):
                    item["entry_zone"] = {
                        "low": calibrated["low"],
                        "high": calibrated["high"],
                    }
                else:
                    item["entry_zone"] = [calibrated["low"], calibrated["high"]]
                success_cnt += 1
            except Exception as e:
                logger.exception(f"[PriceCalibrator] {symbol} 校准异常: {e}")
                item["entry_zone_calibrated"] = {
                    "calibration_failed": True,
                    "reason": f"exception: {e.__class__.__name__}",
                    "calibrated_at": now_et().isoformat(),
                }
                failed_cnt += 1

        if isinstance(recommendations, dict):
            previous_meta = recommendations.get("calibration_meta")
            meta = {
                "calibrated_at": now_et().isoformat(),
                "success": success_cnt,
                "failed": failed_cnt,
                "deviation_alerts": alert_cnt,
                "total": success_cnt + failed_cnt,
                "deviation_alert_pct": self.deviation_alert_pct,
            }
            if previous_meta:
                meta["previous"] = previous_meta
            recommendations["calibration_meta"] = meta

        logger.info(
            f"[PriceCalibrator] 校准完成: 成功={success_cnt} 失败={failed_cnt} "
            f"偏离告警={alert_cnt} 阈值={self.deviation_alert_pct}%"
        )
        return recommendations

    # ── 解析推荐项 ────────────────────────────────────────────────────

    @staticmethod
    def _extract_items(
        recommendations: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        """从多种结构中提取出可迭代的 recommendation 项列表"""
        if not isinstance(recommendations, dict):
            return [], None

        for key in ("recommendations", "items", "targets", "results"):
            payload = recommendations.get(key)
            if isinstance(payload, list) and payload:
                return [p for p in payload if isinstance(p, dict)], payload

        symbol_items = [
            v
            for v in recommendations.values()
            if isinstance(v, dict)
            and ("symbol" in v or "ticker" in v or "entry_zone" in v)
        ]
        if symbol_items:
            return symbol_items, None

        return [], None

    @staticmethod
    def _extract_symbol(item: Dict[str, Any]) -> Optional[str]:
        for key in ("symbol", "ticker", "code"):
            val = item.get(key)
            if val:
                return str(val).strip()
        return None

    @staticmethod
    def _lookup_quote_from_external(
        symbol: str, market_data: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """优先精确匹配，其次匹配去前缀的 symbol (US.AAPL <-> AAPL)"""
        if not market_data:
            return None
        if symbol in market_data:
            return market_data[symbol]
        bare = symbol.split(".")[-1]
        if bare in market_data:
            return market_data[bare]
        prefixed = f"US.{bare}"
        if prefixed in market_data:
            return market_data[prefixed]
        return None

    def _lookup_quote(
        self, symbol: str, market_data: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        external = self._lookup_quote_from_external(symbol, market_data)
        if external and float(external.get("price", 0) or 0) > 0:
            return external
        # 退到 router 实时获取 (兼容带前缀 / 不带前缀两种格式)
        if symbol.startswith("US."):
            return self._fetch_quote(symbol)
        quote = self._fetch_quote(f"US.{symbol}")
        if quote:
            return quote
        return self._fetch_quote(symbol)


__all__ = ["PriceCalibrator"]
