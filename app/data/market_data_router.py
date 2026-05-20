"""
市场数据路由器

数据源架构（2026-04-20 升级为 LV3 主通道）：
  ★★★ Primary:   Futu LV3    — 实时报价 + K线 → 本地计算技术指标 (无 API 限制, 延迟 <10ms)
  Secondary: TwelveData  — 技术指标降级 (仅当 LV3 不可用时)
  Tertiary:  Alpha Vantage — 最终备用数据源，并提供真实 VIX
  Auxiliary: Finnhub     — 新闻情绪分析

调用策略（2026-04-20 升级）：
  - 实时价格 + 技术指标 → Futu LV3 快照 + K线 → 本地计算 (MA/RSI/MACD/ATR/BOLL/KDJ)
  - SPY 大盘 → LV3 快照 → TwelveData → Alpha Vantage
  - VIX → Alpha Vantage（真实数据，缓存 10min）→ 默认 20.0
  - 新闻情绪 → Finnhub + RSS (USNewsProvider)
  - 降级链路 → TwelveData → Alpha Vantage

LV3 降级策略：
  Level 1: Futu OpenD 实时行情 + K线 (LV3, 延迟 < 10ms) ← 主通道
  Level 2: TwelveData HTTP API (LV3 不可用时降级)
  Level 3: Alpha Vantage (最终备用)

限速小结：
  - Futu LV3:    本地连接，无调用限制 (快照 + K线)
  - TwelveData:   仅在降级时使用 (8 credits/min)
  - Alpha Vantage: 仅在降级时使用 (5 calls/min)
  - Finnhub:     仅用于新闻情绪 (60 calls/min)

错误处理：
  - 统一异常类
  - 详细错误日志
  - 优雅降级 fallback
  - LV3 连接健康监控
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

from app.data.futu_lv3_provider import FutuLV3Provider
from app.data.twelvedata_provider import TwelveDataProvider
from app.data.finnhub_provider import FinnhubProvider
from app.data.alphavantage_provider import AlphaVantageProvider

logger = logging.getLogger(__name__)


class MarketDataRouterError(Exception):
    """市场数据路由器错误基类"""
    pass


class DataSourceError(MarketDataRouterError):
    """数据源错误"""
    pass


class AllSourcesFailedError(MarketDataRouterError):
    """所有数据源均失败"""
    pass


class MarketDataRouter:
    """
    统一行情数据接入层（Futu LV3 + TwelveData + Finnhub）。
    
    数据源优先级:
    1. Futu LV3 - 实时报价 + K线 (主通道, 无 API 限制)
    2. TwelveData - 技术指标降级 (仅当 LV3 不可用时)
    3. Finnhub - 实时报价降级
    4. AlphaVantage - 仅 VIX 数据源 (2026-04-24 起不再用于个股行情)
    """

    def __init__(
        self,
        twelvedata_api_key: Optional[str] = None,
        finnhub_api_key: Optional[str] = None,
        alphavantage_api_key: Optional[str] = None,
        cache_ttl_seconds: int = 900,
        futu_opend_host: str = "127.0.0.1",
        futu_opend_port: int = 11111,
        enable_futu_lv3: bool = True,
    ):
        """
        初始化数据路由器

        Args:
            twelvedata_api_key: TwelveData API Key
            finnhub_api_key: Finnhub API Key
            alphavantage_api_key: Alpha Vantage API Key
            cache_ttl_seconds: 默认缓存 TTL
            futu_opend_host: Futu OpenD 主机地址
            futu_opend_port: Futu OpenD 端口
            enable_futu_lv3: 是否启用 LV3 实时行情 (默认启用)
        """
        # 初始化数据提供商
        self.td = TwelveDataProvider(
            api_key=twelvedata_api_key,
            cache_ttl_seconds=cache_ttl_seconds,
        ) if twelvedata_api_key else None

        self.fh = FinnhubProvider(
            api_key=finnhub_api_key,
            cache_ttl_seconds=min(300, cache_ttl_seconds),
        ) if finnhub_api_key else None

        # 2026-04-24 修复: AlphaVantage 仅作为 VIX 数据源，
        # 不再用于个股行情轮询（盘前/盘后轮询无意义，且浪费 API 配额）
        # 保留实例用于 get_vix() 调用
        self.av = AlphaVantageProvider(
            api_key=alphavantage_api_key,
            cache_ttl_seconds=cache_ttl_seconds,
        ) if alphavantage_api_key else None

        # ── Futu LV3 实时行情 (2026-04-17 新增) ──
        self.futu: Optional[FutuLV3Provider] = None
        if enable_futu_lv3:
            try:
                # 将 Finnhub 作为 LV3 的降级数据源
                self.futu = FutuLV3Provider(
                    host=futu_opend_host,
                    port=futu_opend_port,
                    fallback_provider=self.fh,
                    cache_ttl_seconds=5,  # 5 秒缓存 (实时行情)
                )
                logger.info("[MarketDataRouter] Futu LV3 提供商已初始化")
            except Exception as e:
                logger.warning(f"[MarketDataRouter] Futu LV3 初始化失败: {e}")

        # 记录启用的数据源
        enabled_sources = []
        if self.futu and self.futu.enabled:
            enabled_sources.append("FutuLV3")
        if self.td and self.td.enabled:
            enabled_sources.append("TwelveData")
        if self.fh and self.fh.enabled:
            enabled_sources.append("Finnhub")
        if self.av and self.av.enabled:
            enabled_sources.append("AlphaVantage")

        logger.info(
            f"[MarketDataRouter] 初始化完成 | "
            f"启用的数据源：{enabled_sources if enabled_sources else ['无']} | "
            f"Cache TTL={cache_ttl_seconds}s"
        )

        if not enabled_sources:
            logger.error("[MarketDataRouter] ⚠️ 未配置任何数据源，服务将不可用!")
    
    # ── 报价 ──────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        实时报价：Futu LV3 → Finnhub → TwelveData → Alpha Vantage

        Args:
            symbol: 股票代码 (US.AAPL 格式)

        Returns:
            报价字典或 None
        """
        errors = []

        # 0. 尝试 Futu LV3 (实时, 毫秒级延迟) — 2026-04-17 新增
        if self.futu:
            try:
                quote = self.futu.get_quote(symbol)
                if quote and quote.get("price", 0) > 0:
                    logger.debug(
                        f"[Router] {symbol} 从 Futu LV3 获取报价 ${quote['price']:.2f} "
                        f"(延迟 {quote.get('delay_ms', '?')})"
                    )
                    return quote
            except Exception as e:
                errors.append(f"FutuLV3: {e}")
                logger.warning(f"[Router] Futu LV3 获取报价失败 {symbol}: {e}")

        # P0 修复 (2026-05-21): 期权代码仅 Futu 支持，Finnhub/TwelveData 不支持期权格式
        from app.data.futu_lv3_provider import FutuLV3Provider
        if FutuLV3Provider._is_option_code(symbol):
            logger.debug(f"[Router] 期权代码 {symbol} Futu 失败，跳过 Finnhub/TwelveData")
            return None

        # 1. 尝试 Finnhub
        if self.fh:
            try:
                quote = self.fh.get_quote(symbol)
                if quote and quote.get("price", 0) > 0:
                    logger.debug(f"[Router] {symbol} 从 Finnhub 获取报价 ${quote['price']}")
                    return quote
            except Exception as e:
                errors.append(f"Finnhub: {e}")
                logger.warning(f"[Router] Finnhub 获取报价失败 {symbol}: {e}")

        # 2. 尝试 TwelveData
        if self.td:
            try:
                quote = self.td.get_quote(symbol)
                if quote and quote.get("price", 0) > 0:
                    logger.debug(f"[Router] {symbol} 从 TwelveData 获取报价 ${quote['price']}")
                    return quote
            except Exception as e:
                errors.append(f"TwelveData: {e}")
                logger.warning(f"[Router] TwelveData 获取报价失败 {symbol}: {e}")

        # 3. AlphaVantage 已禁用 (2026-04-24)
        # 盘前/盘后无实时行情，API 配额浪费，仅保留 VIX 数据源功能

        # 全部失败
        error_msg = f"[Router] {symbol} 所有数据源均失败: {'; '.join(errors)}"
        logger.error(error_msg)
        return None

    def get_batch_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量报价：Futu LV3 快照 → Finnhub → TwelveData → Alpha Vantage

        Args:
            symbols: 股票代码列表

        Returns:
            报价字典
        """
        results: Dict[str, Dict] = {}
        failed: List[str] = []
        errors = []

        # 0. Futu LV3 批量快照 (一次调用获取所有股票) — 2026-04-17 新增
        if self.futu:
            try:
                futu_results = self.futu.get_batch_quote(symbols)
                if futu_results:
                    for sym, quote in futu_results.items():
                        results[sym] = quote
                    failed = [s for s in symbols if s not in results]
                    logger.debug(
                        f"[Router] Futu LV3 批量快照: "
                        f"{len(results)}/{len(symbols)} 成功"
                    )
                else:
                    failed = symbols.copy()
            except Exception as e:
                errors.append(f"FutuLV3: {e}")
                logger.warning(f"[Router] Futu LV3 批量获取失败：{e}")
                failed = symbols.copy()

        # 1. Finnhub 获取失败的
        if failed and self.fh:
            still_failed = []
            try:
                for sym in failed:
                    q = self.fh.get_quote(sym)
                    if q and q.get("price", 0) > 0:
                        results[sym] = q
                    else:
                        still_failed.append(sym)
            except Exception as e:
                errors.append(f"Finnhub: {e}")
                logger.warning(f"[Router] Finnhub 批量获取失败：{e}")
                still_failed = failed.copy()

            failed = still_failed

        # 2. TwelveData 获取失败的
        if failed and self.td:
            still_failed = []
            try:
                for sym in failed:
                    q = self.td.get_quote(sym)
                    if q and q.get("price", 0) > 0:
                        results[sym] = q
                    else:
                        still_failed.append(sym)
            except Exception as e:
                errors.append(f"TwelveData: {e}")
                logger.warning(f"[Router] TwelveData 批量获取失败：{e}")
                still_failed = failed.copy()
            
            failed = still_failed
        
        # 3. AlphaVantage 已禁用 (2026-04-24)
        # 不再用于个股行情轮询，仅保留 VIX 数据源功能
        
        if failed:
            logger.error(f"[Router] 以下股票获取报价失败：{failed} | 错误：{'; '.join(errors)}")
        
        return results

    # ── 技术指标 / 上下文 ─────────────────────────────────────────

    def get_full_market_context(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        综合行情上下文（供 VotingCoordinator 使用）。

        数据融合（2026-04-20 升级为 LV3 主通道，2026-04-24 移除 AlphaVantage 个股轮询）：
          ★ 1. Futu LV3 → 实时报价 + K 线 → 本地计算技术指标 (主通道, 无 API 限制)
          2. TwelveData → 技术指标降级 (仅当 LV3 不可用时)
          3. AlphaVantage → 已禁用 (仅 VIX 数据源)
          4. Finnhub → 新闻情绪 + 价格降级

        Args:
            symbol: 股票代码
        
        Returns:
            完整市场上下文字典或 None
        """
        errors = []
        ts_ctx = None
        
        # ★★★ Step 1: Futu LV3 主通道 — 实时价格 + K线 → 技术指标计算
        if self.futu:
            try:
                # 获取 LV3 实时报价
                futu_quote = self.futu.get_quote(symbol)
                if futu_quote and futu_quote.get("price", 0) > 0:
                    # 获取 K 线数据用于技术指标计算
                    klines = self.futu.get_kline(symbol, ktype="K_1M", count=200)
                    
                    if klines and len(klines) >= 30:
                        # 从 LV3 K 线计算技术指标
                        from app.data.lv3_technical_calculator import LV3TechnicalCalculator
                        calc = LV3TechnicalCalculator()
                        tech = calc.calculate_from_klines(klines)
                        
                        if tech:
                            # 合并技术指标 + LV3 报价
                            ts_ctx = tech
                            ts_ctx["data_source"] = "futu_lv3"
                            
                            # ★★★ 关键修复: 使用 LV3 实时快照价格覆盖 K 线收盘价
                            # K 线数据可能存在延迟或复权差异，快照价格是最新的真实成交价。
                            if futu_quote and futu_quote.get("price", 0) > 0:
                                ts_ctx["price"] = futu_quote["price"]
                                logger.debug(f"[Router] {symbol} 使用快照价格覆盖 K 线价格: ${futu_quote['price']:.2f}")
                            
                            # 注入 LV3 深度数据
                            ts_ctx["lv3_bid"] = futu_quote.get("bid", ts_ctx["price"])
                            ts_ctx["lv3_ask"] = futu_quote.get("ask", ts_ctx["price"])
                            ts_ctx["lv3_bid_vol"] = futu_quote.get("bid_vol", 0)
                            ts_ctx["lv3_ask_vol"] = futu_quote.get("ask_vol", 0)
                            ts_ctx["lv3_delay_ms"] = futu_quote.get("delay_ms", "<10")
                            # 买卖力量比
                            bid_vol = futu_quote.get("bid_vol", 0)
                            ask_vol = futu_quote.get("ask_vol", 0)
                            total_vol = bid_vol + ask_vol
                            ts_ctx["order_flow_imbalance"] = (
                                (bid_vol - ask_vol) / total_vol if total_vol > 0 else 0.0
                            )
                            # P1 2026-05-12 审计修复: 注入短期价格斜率 (替代日累计 change_percent)
                            # 计算最近 5 根 1-min K 线的价格变化率 (slope_5m)
                            # 对日内入场决策更有意义: 反映"当下"的动量方向
                            if len(klines) >= 5:
                                recent_closes = [float(k.get('close', 0)) for k in klines[-5:]]
                                if recent_closes[0] > 0:
                                    slope_5m = (recent_closes[-1] - recent_closes[0]) / recent_closes[0] * 100
                                    ts_ctx["slope_5m"] = round(slope_5m, 3)
                                    ts_ctx["recent_closes"] = recent_closes
                                else:
                                    ts_ctx["slope_5m"] = 0.0
                            # 距日内高点距离 (补充信号)
                            day_high = float(futu_quote.get("high_price", 0))
                            if day_high > 0 and ts_ctx["price"] > 0:
                                ts_ctx["high_distance_pct"] = round(
                                    (ts_ctx["price"] - day_high) / day_high * 100, 3
                                )

                            # ★★★ 2026-05-20 增强: Push Ticker + OrderBook 实时因子注入
                            # 利用已订阅但未处理的 TICKER/ORDER_BOOK 数据 (之前浪费槽位)
                            current_price = ts_ctx.get("price", 0)
                            try:
                                # 逐笔量能因子
                                from app.data.ticker_analyzer import analyze_tickers
                                push_tickers = self.futu.get_push_tickers(symbol)
                                if push_tickers and len(push_tickers) >= 10:
                                    ticker_factors = analyze_tickers(push_tickers, current_price=current_price)
                                    if ticker_factors:
                                        ts_ctx["ticker_buy_sell_ratio"] = ticker_factors["buy_sell_ratio_short"]
                                        ts_ctx["ticker_buy_sell_ratio_medium"] = ticker_factors["buy_sell_ratio_medium"]
                                        ts_ctx["ticker_volume_surge"] = ticker_factors["volume_surge"]
                                        ts_ctx["ticker_large_order_ratio"] = ticker_factors["large_order_volume_ratio"]
                                        ts_ctx["ticker_vwap_deviation"] = ticker_factors["vwap_deviation"]
                                        ts_ctx["ticker_velocity"] = ticker_factors["ticker_velocity"]
                                        ts_ctx["ticker_score"] = ticker_factors["score"]
                                        ts_ctx["ticker_signal"] = ticker_factors["signal_label"]
                                        # 增强 order_flow_imbalance: 用逐笔数据的买卖比替代纯快照
                                        if ticker_factors["buy_sell_ratio_short"] > 0:
                                            bs = ticker_factors["buy_sell_ratio_short"]
                                            ts_ctx["order_flow_imbalance"] = round((bs - 1.0) / (bs + 1.0), 4)
                            except Exception as te:
                                logger.debug(f"[Router] {symbol} Ticker 因子注入跳过: {te}")

                            try:
                                # 买卖盘深度因子
                                from app.data.orderbook_analyzer import analyze_orderbook
                                push_ob = self.futu.get_push_orderbook(symbol)
                                if push_ob:
                                    ob_factors = analyze_orderbook(push_ob, current_price=current_price)
                                    if ob_factors:
                                        ts_ctx["ob_spread_bps"] = ob_factors["spread_bps"]
                                        ts_ctx["ob_depth_imbalance"] = ob_factors["depth_imbalance"]
                                        ts_ctx["ob_order_flow_imbalance"] = ob_factors["order_flow_imbalance"]
                                        ts_ctx["ob_liquidity_score"] = ob_factors["liquidity_score"]
                                        ts_ctx["ob_direction_score"] = ob_factors["direction_score"]
                                        ts_ctx["ob_signal"] = ob_factors["signal_label"]
                                        ts_ctx["ob_bid_ask_ratio"] = ob_factors["bid_ask_ratio"]
                                        # 增强 LV3 数据: 用 Push 实时数据覆盖快照数据
                                        if ob_factors["bid_price"] > 0:
                                            ts_ctx["lv3_bid"] = ob_factors["bid_price"]
                                            ts_ctx["lv3_ask"] = ob_factors["ask_price"]
                                            ts_ctx["lv3_bid_vol"] = ob_factors["bid_vol"]
                                            ts_ctx["lv3_ask_vol"] = ob_factors["ask_vol"]
                            except Exception as obe:
                                logger.debug(f"[Router] {symbol} OrderBook 因子注入跳过: {obe}")

                            logger.info(
                                f"[Router] {symbol} ★★★ LV3 主通道 | "
                                f"price=${ts_ctx['price']:.2f} RSI={ts_ctx.get('rsi', 0):.1f} "
                                f"MACD={ts_ctx.get('macd', 0):.4f} ATR={ts_ctx.get('atr', 0):.2f} "
                                f"trend={ts_ctx.get('trend', 'N/A')}"
                                f" | ticker={ts_ctx.get('ticker_signal', 'N/A')}"
                                f" | ob={ts_ctx.get('ob_signal', 'N/A')}"
                            )
                        else:
                            # K 线有但指标计算失败，使用基础报价
                            ts_ctx = {
                                "price": futu_quote["price"],
                                "change_percent": futu_quote.get("change_percent", 0.0),
                                "volume": futu_quote.get("volume", 0),
                                "data_source": "futu_lv3_quote_only",
                            }
                            logger.warning(f"[Router] {symbol} LV3 K 线技术指标计算失败，仅使用报价")
                    else:
                        # K 线数据不足，使用基础报价
                        ts_ctx = {
                            "price": futu_quote["price"],
                            "change_percent": futu_quote.get("change_percent", 0.0),
                            "volume": futu_quote.get("volume", 0),
                            "data_source": "futu_lv3_quote_only",
                        }
                        logger.warning(f"[Router] {symbol} LV3 K 线数据不足 ({len(klines) if klines else 0} 条)")
            except Exception as e:
                errors.append(f"FutuLV3: {e}")
                logger.warning(f"[Router] Futu LV3 主通道失败 {symbol}: {e}")
        
        # Step 2: TwelveData 降级 (仅当 LV3 不可用时)
        if not ts_ctx or ts_ctx.get("data_source") not in ("futu_lv3", "futu_lv3_quote_only"):
            if self.td:
                try:
                    ts_ctx = self.td.get_full_market_context(symbol)
                    if ts_ctx and ts_ctx.get("price", 0) > 0:
                        logger.debug(f"[Router] {symbol} 从 TwelveData 获取技术指标 (LV3 降级)")
                except Exception as e:
                    errors.append(f"TwelveData: {e}")
                    logger.warning(f"[Router] TwelveData 获取技术指标失败 {symbol}: {e}")
        
        # Step 3: Alpha Vantage 已禁用 (2026-04-24)
        # AlphaVantage 仅作为 VIX 数据源 (Step 5)，不再用于个股行情。
        # 原因: 盘前/盘后无实时行情，API 配额浪费，数据全部 stale。
        # 如果 LV3 和 TwelveData 都失败，说明非交易时段无数据可用，直接返回 None。
        # 2026-04-24 之前此处调用 self.av.get_full_market_context(symbol) 导致
        # 每日 2000+ 次无效 AlphaVantage 调用。
        
        if not ts_ctx or ts_ctx.get("price", 0) <= 0:
            logger.error(f"[Router] {symbol} 所有数据源均失败：{'; '.join(errors)}")
            return None

        # Step 4: 新闻情绪 — Finnhub 已跳过 (API Key 无效), 由 app.pipeline 注入 RSS+Qwen
        ts_ctx.setdefault("news_sentiment", 0.0)

        # Step 5: VIX (Alpha Vantage 真实数据)
        if self.av:
            try:
                ts_ctx["vix"] = self.av.get_vix()
            except Exception as e:
                logger.warning(f"[Router] AlphaVantage 获取 VIX 失败：{e}")
                ts_ctx.setdefault("vix", 20.0)
        else:
            ts_ctx.setdefault("vix", 20.0)

        # Step 6: SPY 大盘 (LV3 快照优先 → TwelveData → Alpha Vantage)
        spy_ctx = None
        if self.futu:
            try:
                spy_quote = self.futu.get_quote("US.SPY")
                if spy_quote and spy_quote.get("price", 0) > 0:
                    spy_ctx = {"spy_price": spy_quote["price"]}
                    logger.debug(f"[Router] SPY 从 LV3 获取: ${spy_quote['price']:.2f}")
            except Exception as e:
                logger.debug(f"[Router] LV3 获取 SPY 失败: {e}")
        
        if not spy_ctx and self.td:
            try:
                spy_ctx = self.td.get_spy_context()
            except Exception as e:
                logger.warning(f"[Router] TwelveData 获取 SPY 失败：{e}")
        
        # 2026-04-24: AlphaVantage SPY 已禁用 (原因同上)
        
        if spy_ctx:
            ts_ctx.update({
                "spy_price": spy_ctx.get("spy_price", 0.0),
                "spy_ma20": spy_ctx.get("spy_ma20", 0.0),
                "spy_ma50": spy_ctx.get("spy_ma50", 0.0),
                "spx_change_percent": spy_ctx.get("spx_change_percent", 0.0),
                "qqq_change_percent": spy_ctx.get("qqq_change_percent", 0.0),
                "price_change_5d": spy_ctx.get("price_change_5d", 0.0),
            })

        # Step 7: 市场状态判断
        ts_ctx["market_regime"] = self._calc_market_regime(ts_ctx)
        
        # 添加时间戳
        ts_ctx["timestamp"] = datetime.now().isoformat()

        logger.info(
            f"[Router] {symbol} price=${ts_ctx['price']:.2f} "
            f"sentiment={ts_ctx.get('news_sentiment', 0):.2f} "
            f"vix={ts_ctx.get('vix', 20.0):.2f} "
            f"regime={ts_ctx['market_regime']} "
            f"source={ts_ctx.get('data_source', 'unknown')}"
        )
        return ts_ctx

    def _calc_market_regime(self, ctx: Dict[str, Any]) -> str:
        """
        根据 VIX / 价格相对均线判断市场状态
        
        P2 修复 (2026-04-16): 使用 VIX 作为主要指标，个股波动率作为辅助
        VIX > 25: 高波动 (市场恐慌)
        VIX < 15: 低波动 (市场平静)
        趋势向上/向下: 趋势市
        其他: 震荡市
        """
        vix = ctx.get("vix", 20.0)
        vol = ctx.get("volatility", 0)
        avg_vol = ctx.get("avg_volatility", 0.001)
        trend = ctx.get("trend", "NEUTRAL")

        # 高波动: VIX > 25 (市场恐慌) 或 个股波动率显著高于均值 (3x, 提高阈值避免误判)
        if vix > 25 or (avg_vol > 0 and vol > avg_vol * 3.0):
            return "HIGH_VOL"
        # 趋势市场
        if trend == "UP":
            return "TREND_UP"
        if trend == "DOWN":
            return "TREND_DOWN"
        # 低波动: VIX < 15 (市场平静)
        if vix < 15:
            return "LOW_VOL"
        # 默认: 震荡市
        return "RANGE"

    # ── 新闻情绪 ──────────────────────────────────────────────────

    def get_sentiment_score(self, symbol: str) -> float:
        """单标的新闻情绪分 — 仅 RSS+Qwen, Finnhub 已移除 (API Key 无效)"""
        # Finnhub API Key 为 *** 无效值, 所有调用都返回 401 ERROR
        # 新闻情绪由 app.pipeline 中的 RSS+Qwen 管线提供, 不需要 Finnhub
        return 0.0

    def get_market_news_summary(self, hours: int = 6) -> Dict[str, Any]:
        """大盘整体新闻情绪 — 仅 RSS+Qwen, Finnhub 已移除 (API Key 无效)"""
        return {"sentiment": 0.0, "news_count": 0}

    # ── VIX / SPY ─────────────────────────────────────────────────

    def get_vix(self) -> float:
        """VIX: Alpha Vantage 真实数据 → 默认 20.0"""
        if self.av:
            try:
                return self.av.get_vix()
            except Exception as e:
                logger.warning(f"[Router] AlphaVantage 获取 VIX 失败：{e}")
        return 20.0

    def get_spy_context(self) -> Dict[str, Any]:
        """SPY 大盘上下文：仅 LV3 快照 (2026-04-24 移除 AlphaVantage SPY 轮询)"""
        # ★ 优先: LV3 快照获取 SPY 实时价格
        if self.futu:
            try:
                spy_quote = self.futu.get_quote("US.SPY")
                if spy_quote and spy_quote.get("price", 0) > 0:
                    return {"spy_price": spy_quote["price"]}
            except Exception as e:
                logger.debug(f"[Router] LV3 获取 SPY 失败: {e}")

        # 2026-04-24: AlphaVantage SPY 已禁用，盘前/盘后无新增技术指标
        # TwelveData key 无效，跳过
        return {}

    def get_technical_indicators(
        self,
        symbol: str,
        indicators: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        获取技术指标 (供 Scalping Engine 使用)

        P0 修复 (2026-05-02): 原方法不存在导致 scalping 信号链路完全断裂。
        内部复用 get_full_market_context() 的 LV3 K 线→技术指标计算链路。

        Args:
            symbol: 股票代码 (US.AAPL 格式)
            indicators: 请求的指标列表 ["rsi", "vwap", "bollinger", ...]

        Returns:
            技术指标字典，包含:
              rsi_14, vwap, bb_upper, bb_lower, macd, atr, volume, avg_volume 等
        """
        result = {}

        ctx = self.get_full_market_context(symbol)
        if not ctx:
            logger.warning(f"[Router] {symbol} 技术指标获取失败: 无行情数据")
            return result

        # 映射: full context 字段 → scalping 期望字段
        field_map = {
            "rsi": "rsi_14",
            "vwap": "vwap",
            "bb_upper": "bb_upper",
            "bb_lower": "bb_lower",
            "bb_middle": "bb_middle",
            # 兼容旧字段名 (LV3 calculator 同时输出 boll_* 和 bb_*)
            "boll_upper": "bb_upper",
            "boll_lower": "bb_lower",
            "boll_middle": "bb_middle",
            "macd": "macd",
            "macd_signal": "macd_signal",
            "macd_hist": "macd_hist",
            "atr": "atr",
            "volume": "volume",
            "avg_volume": "avg_volume",
            "ma5": "ma5",
            "ma10": "ma10",
            "ma20": "ma20",
        }

        for src_key, dst_key in field_map.items():
            if src_key in ctx:
                result[dst_key] = ctx[src_key]

        # 如果 indicators 列表指定了，只返回请求的
        if indicators:
            filtered = {}
            for ind in indicators:
                ind_map = {
                    "rsi": "rsi_14",
                    "vwap": "vwap",
                    "bollinger": ["bb_upper", "bb_lower", "bb_middle"],
                    "macd": ["macd", "macd_signal", "macd_hist"],
                    "atr": "atr",
                    "volume": ["volume", "avg_volume"],
                    "ma": ["ma5", "ma10", "ma20"],
                }
                keys = ind_map.get(ind, [ind])
                if isinstance(keys, list):
                    for k in keys:
                        if k in result:
                            filtered[k] = result[k]
                elif keys in result:
                    filtered[keys] = result[keys]
            return filtered

        return result

    # ── 统计 ──────────────────────────────────────────────────────

    def get_rate_limit_status(self) -> Dict[str, Any]:
        """返回各数据源当前剩余令牌数"""
        status = {}
        
        if self.td:
            try:
                status["twelvedata"] = self.td.get_rate_limit_status()
            except Exception as e:
                status["twelvedata"] = {"error": str(e)}
        
        if self.fh:
            try:
                status["finnhub"] = self.fh.get_rate_limit_status()
            except Exception as e:
                status["finnhub"] = {"error": str(e)}
        
        if self.av:
            try:
                status["alphavantage"] = self.av.get_rate_limit_status()
            except Exception as e:
                status["alphavantage"] = {"error": str(e)}
        
        return status
    
    def get_enabled_sources(self) -> List[str]:
        """获取启用的数据源列表"""
        sources = []
        if self.td and self.td.enabled:
            sources.append("TwelveData")
        if self.fh and self.fh.enabled:
            sources.append("Finnhub")
        if self.av and self.av.enabled:
            sources.append("AlphaVantage")
        return sources
    
    def health_check(self) -> Dict[str, Any]:
        """
        健康检查
        
        Returns:
            健康状态字典
        """
        status = {
            "healthy": False,
            "timestamp": datetime.now().isoformat(),
            "sources": {},
        }
        
        sources_healthy = 0
        
        if self.td:
            try:
                td_status = self.td.get_rate_limit_status()
                status["sources"]["twelvedata"] = {
                    "healthy": td_status.get("tokens_remaining", 0) > 0,
                    "tokens": td_status.get("tokens_remaining", 0),
                }
                if status["sources"]["twelvedata"]["healthy"]:
                    sources_healthy += 1
            except Exception as e:
                status["sources"]["twelvedata"] = {"healthy": False, "error": str(e)}
        
        if self.fh:
            try:
                fh_status = self.fh.get_rate_limit_status()
                status["sources"]["finnhub"] = {
                    "healthy": fh_status.get("tokens_remaining", 0) > 0,
                    "tokens": fh_status.get("tokens_remaining", 0),
                }
                if status["sources"]["finnhub"]["healthy"]:
                    sources_healthy += 1
            except Exception as e:
                status["sources"]["finnhub"] = {"healthy": False, "error": str(e)}
        
        if self.av:
            try:
                av_status = self.av.get_rate_limit_status()
                status["sources"]["alphavantage"] = {
                    "healthy": av_status.get("tokens_remaining", 0) > 0,
                    "tokens": av_status.get("tokens_remaining", 0),
                    "daily_remaining": av_status.get("daily_remaining", 0),
                }
                if status["sources"]["alphavantage"]["healthy"]:
                    sources_healthy += 1
            except Exception as e:
                status["sources"]["alphavantage"] = {"healthy": False, "error": str(e)}
        
        # 至少一个数据源健康即认为整体健康
        status["healthy"] = sources_healthy > 0
        status["sources_healthy"] = sources_healthy
        status["sources_total"] = len([s for s in [self.td, self.fh, self.av] if s])
        
        return status
    
    def close(self):
        """关闭所有连接"""
        if self.td and hasattr(self.td, 'close'):
            self.td.close()
        if self.fh and hasattr(self.fh, 'close'):
            self.fh.close()
        if self.av and hasattr(self.av, 'close'):
            self.av.close()
        # P1 修复 (2026-05-09): 关闭 Futu LV3 连接，释放 OpenD 槽位
        if self.futu_provider and hasattr(self.futu_provider, 'disconnect'):
            try:
                self.futu_provider.disconnect()
                logger.info("[MarketDataRouter] Futu LV3 连接已断开")
            except Exception as e:
                logger.warning(f"[MarketDataRouter] Futu 断开失败: {e}")
        logger.info("[MarketDataRouter] 所有连接已关闭")


# 模块级导出
__all__ = [
    "MarketDataRouter",
    "MarketDataRouterError",
    "DataSourceError",
    "AllSourcesFailedError",
]
