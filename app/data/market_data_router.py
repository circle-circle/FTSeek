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
import threading
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

import pandas as pd

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
        # 2026-06-28 修复: twelvedata_api_key可能为None(systemd无env), 
        # 但TwelveDataProvider内部会load_dotenv(.env.us)自行加载key
        # 所以始终创建实例, 让Provider自己处理key
        self.td = TwelveDataProvider(
            api_key=twelvedata_api_key,
            cache_ttl_seconds=cache_ttl_seconds,
        )

        # 2026-06-28 修复: 同TwelveData, Finnhub也通过.env.us自行加载key
        self.fh = FinnhubProvider(
            api_key=finnhub_api_key,
            cache_ttl_seconds=min(300, cache_ttl_seconds),
        )

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

        # ── SPY MA200 缓存 (2026-06-16 修复: spy_ma200=0 bug) ──
        # MA200 变化极慢 (每日仅增 1 个数据点), 30 分钟缓存足够
        self._spy_ma200_cache: Optional[float] = None
        self._spy_ma200_cache_time: Optional[datetime] = None
        self._spy_ma200_cache_ttl = timedelta(minutes=30)
        # P0 (2026-07-03): MA20/MA50 缓存 (与 MA200 同步, 避免依赖 AlphaVantage)
        self._spy_ma20_cache: Optional[float] = None
        self._spy_ma50_cache: Optional[float] = None

        # ── 日K线共享缓存 (2026-06-29: 消除双backfill重复API调用) ──
        # key = f"{symbol}:{date_str}", TTL=当日有效 (跨日自动miss)
        # ic_tracker.backfill 和 scripts/backfill_forward_returns 共享此缓存
        self._daily_bars_cache: Dict[str, tuple] = {}
        self._daily_bars_cache_ttl = 300  # 5 分钟 (与 TwelveData provider 对齐)

        if not enabled_sources:
            logger.error("[MarketDataRouter] ⚠️ 未配置任何数据源，服务将不可用!")
    
    # ── 报价 ──────────────────────────────────────────────────────

    def get_daily_bars(self, symbol: str, count: int = 30) -> Optional[List[Dict[str, Any]]]:
        """获取日K线历史 (供 IC 前瞻收益回填 / shadow tracker MFE-MAE 使用).

        Fallback 链: Futu LV3 get_daily_kline (历史K线, 盘后可用) → Finnhub get_kline(D).
        2026-06-23 修复 (OKR P0): 本方法此前缺失, 导致 ic_tracker.backfill_forward_returns
        每次 AttributeError 被吞 → 前瞻收益永不回填 → IC 永远=None → 毕业门禁永不通过;
        同时 shadow_tracker 的 hasattr 检查 False → MFE/MAE 永远 0.

        2026-06-29: 新增 _daily_bars_cache, 消除双backfill重复API调用.
        ic_tracker 和 scripts/backfill_forward_returns 共享同一router实例的缓存.

        Returns:
            List[{time, open, high, low, close, volume, turnover}] 升序; time 归一化为
            "YYYY-MM-DD" (兼容 ic_tracker 精确匹配 + shadow_tracker [:10]). 失败返回 None.
        """
        if not symbol:
            return None

        # P1 (2026-06-29): 日K线共享缓存 — 同标的5分钟内不重复调API
        import time as _time
        cache_key = f"{symbol}:{count}"
        cached = self._daily_bars_cache.get(cache_key)
        if cached:
            bars, ts = cached
            if (_time.time() - ts) < self._daily_bars_cache_ttl:
                return bars

        # 1. Futu (主): 历史日K, 盘后也可用 (不依赖实时连接)
        if self.futu:
            try:
                bars = self.futu.get_daily_kline(symbol, count=count)
                bars = self._normalize_bar_times(bars)
                if bars:
                    self._daily_bars_cache[cache_key] = (bars, _time.time())
                    return bars
            except Exception as e:
                logger.warning(f"[Router] get_daily_bars {symbol} Futu 失败, 尝试 TwelveData: {e}")

        # 2. TwelveData (fallback): HTTP time_series interval=1day, 免费版可用.
        #    Finnhub /stock/candle 免费版 403, 不能作日线 fallback.
        if self.td and self.td.enabled:
            try:
                bars = self.td.get_daily_bars(symbol, count=count)
                if bars:
                    logger.info(f"[Router] get_daily_bars {symbol} 走 TwelveData fallback: {len(bars)} bars")
                    self._daily_bars_cache[cache_key] = (bars, _time.time())
                    return bars
            except Exception as e:
                logger.warning(f"[Router] get_daily_bars {symbol} TwelveData fallback 失败: {e}")

        logger.warning(f"[Router] get_daily_bars {symbol}: 所有数据源均失败")
        return None

    @staticmethod
    def _normalize_bar_times(bars: Optional[List[Dict]]) -> Optional[List[Dict]]:
        """把 bar 的 time 归一化为 "YYYY-MM-DD".

        Futu: "2026-06-23 00:00:00" → 截断; Finnhub: "1719100800" (Unix 秒) → 解析.
        保证 ic_tracker (精确匹配 "YYYY-MM-DD") 与 shadow_tracker ([:10]) 都能用.
        """
        if not bars:
            return None
        for b in bars:
            t = str(b.get("time", ""))
            if not t:
                continue
            if t.isdigit():
                # Unix 秒 → 日期
                try:
                    b["time"] = datetime.utcfromtimestamp(int(t)).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    pass
            elif len(t) > 10:
                b["time"] = t[:10]
        return bars

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        实时报价：Futu LV3 → Finnhub → TwelveData → Alpha Vantage

        Args:
            symbol: 股票代码 (US.AAPL 格式)

        Returns:
            报价字典或 None
        """
        # 2026-06-11: 跳过非美股标的，避免无效 API 调用和错误日志
        if not symbol.startswith('US.') or symbol.count('.') > 1:
            return None

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
        
        # P1 修复 (2026-05-21): 期权代码跳过技术指标计算
        # 期权定价基于 Black-Scholes 等模型（Greeks），RSI/MACD/BB 对期权无意义
        # 期权 K 线数据稀疏且不连续，计算技术指标会返回误导性数值
        from app.data.futu_lv3_provider import FutuLV3Provider
        is_option = FutuLV3Provider._is_option_code(symbol)
        if is_option:
            logger.debug(f"[Router] {symbol} 期权代码，跳过技术指标计算，仅获取报价数据")
        
        # ★★★ Step 1: Futu LV3 主通道 — 实时价格 + K线 → 技术指标计算
        klines = None  # FIX (2026-05-22): Prevent UnboundLocalError if quote fails or price=0
        if self.futu:
            try:
                # 获取 LV3 实时报价
                futu_quote = self.futu.get_quote(symbol)
                if futu_quote and futu_quote.get("price", 0) > 0:
                    # 期权跳过 K 线技术指标计算 (P1 2026-05-21)
                    if is_option:
                        klines = None  # 期权不需要 K 线技术指标
                        ts_ctx = {
                            "price": futu_quote["price"],
                            "change_percent": futu_quote.get("change_percent", 0.0),
                            "volume": futu_quote.get("volume", 0),
                            "data_source": "futu_lv3_quote_only",
                            "is_option": True,
                        }
                        logger.info(f"[Router] {symbol} ★ 期权报价 | price=${futu_quote['price']:.2f} vol={futu_quote.get('volume', 0)}")
                    else:
                        # 获取 1分钟K线 (盘中短周期信号: slope_5m/recent_closes)
                        klines = self.futu.get_kline(symbol, ktype="K_1M", count=200)
                        # 获取 日K线 (盘前技术指标: MA/MACD/RSI/Boll — 日线级别信号过夜不失效)
                        daily_klines = self.get_daily_bars(symbol, count=60)

                    # 2026-07-09: 技术指标改用日K线计算 (盘前信号时间框架对齐)
                    # 根因: 原用K_1M算MACD/MA20 → 1分钟级别信号持续几分钟 → 盘前生成到开盘
                    # 已过5小时 → 信号失效. 日线MACD金叉/MA20持续1-3天, 过夜不失效.
                    # 1分钟K线仍用于slope_5m/recent_closes等盘中实时短周期信号.
                    if daily_klines and len(daily_klines) >= 30:
                        from app.data.lv3_technical_calculator import LV3TechnicalCalculator
                        calc = LV3TechnicalCalculator()
                        tech = calc.calculate_from_klines(daily_klines)

                        if tech:
                            # 合并技术指标 + LV3 报价
                            ts_ctx = tech
                            ts_ctx["data_source"] = "futu_lv3_daily"
                            ts_ctx["indicator_timeframe"] = "daily"
                        else:
                            ts_ctx = None  # 日K线计算失败, 下面fallback到1分钟
                    else:
                        ts_ctx = None  # 日K线不足, fallback

                    # Fallback: 日K线不可用时, 回退到1分钟K线 (保持原有行为)
                    if ts_ctx is None and klines and len(klines) >= 30:
                        from app.data.lv3_technical_calculator import LV3TechnicalCalculator
                        calc = LV3TechnicalCalculator()
                        tech = calc.calculate_from_klines(klines)
                        if tech:
                            ts_ctx = tech
                            ts_ctx["data_source"] = "futu_lv3_1m_fallback"
                            ts_ctx["indicator_timeframe"] = "1m"
                            logger.debug(f"[Router] {symbol} 日K线不足, 回退到1分钟技术指标")

                    # ATR覆盖 + 快照价格注入 (对所有ts_ctx生效, 不限来源)
                    if ts_ctx is not None:
                        # 2026-06-26: 用日K线ATR覆盖 — atr_pct < 0.5% 门禁
                        # 日K线主路径下ATR已是日级别, fallback到1分钟时仍需覆盖
                        try:
                            daily_bars = self.get_daily_bars(symbol, count=30)
                            if daily_bars and len(daily_bars) >= 15:
                                from app.data.lv3_technical_calculator import LV3TechnicalCalculator as _Calc
                                _df = _Calc()._klines_to_dataframe(daily_bars)
                                if _df is not None and len(_df) >= 15:
                                    _h = _df["high"]; _l = _df["low"]; _pc = _df["close"].shift(1)
                                    _tr = pd.concat([_h - _l, (_h - _pc).abs(), (_l - _pc).abs()], axis=1).max(axis=1)
                                    _daily_atr = float(_tr.rolling(window=14).mean().iloc[-1])
                                    if _daily_atr > 0:
                                        ts_ctx["atr"] = round(_daily_atr, 2)
                                        _price = ts_ctx.get("price", 0)
                                        ts_ctx["atr_percent"] = round(_daily_atr / _price * 100, 2) if _price > 0 else 0.0
                            # 兜底: 如果ATR%仍 < 0.5%, 用 price*2% 作为最小日波动率
                            _atr_val = float(ts_ctx.get("atr", 0) or 0)
                            _price_val = float(ts_ctx.get("price", 0) or 0)
                            if _price_val > 0 and _atr_val > 0:
                                _atr_pct_check = _atr_val / _price_val * 100
                                if _atr_pct_check < 0.5:
                                    _floor_atr = _price_val * 0.02  # 2% 日波动率兜底
                                    ts_ctx["atr"] = round(_floor_atr, 2)
                                    ts_ctx["atr_percent"] = 2.0
                                    logger.debug(f"[Router] {symbol} ATR兜底: {_atr_val:.2f}({_atr_pct_check:.2f}%) → {_floor_atr:.2f}(2.0%)")
                        except Exception as _atr_err:
                            logger.debug(f"[Router] {symbol} 日ATR覆盖失败 (用1分钟ATR): {_atr_err}")

                        # ★★★ FIX 2026-07-11: 富因子注入原被误缩进进 except 分支, 移回正常路径
                        # 用此 if 承载块内 28 缩进 (恒真: 外层已判 ts_ctx 非 None), 避免大范围重排
                        if ts_ctx is not None:
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
                            if klines and len(klines) >= 5:
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

                            # ── ORB: 注入日内高/低/开盘价 + 开盘区间 ──
                            ts_ctx["day_high"] = day_high
                            ts_ctx["day_low"] = float(futu_quote.get("low_price", 0))
                            ts_ctx["open_price"] = float(futu_quote.get("open_price", 0))

                            # 计算开盘区间 (Opening Range): 开盘后前15根1分钟K线的高低点
                            if klines and len(klines) >= 15:
                                try:
                                    import pytz
                                    from datetime import datetime as _dt
                                    now_et = _dt.now(pytz.timezone('US/Eastern'))
                                    # 筛选今日开盘后的K线 (09:30 ET 起)
                                    market_open_str = now_et.strftime("%Y-%m-%d") + " 09:30"
                                    today_bars = []
                                    for k in klines:
                                        tk = str(k.get("time_key", ""))
                                        if tk >= market_open_str:
                                            today_bars.append(k)
                                    # 取前15根 (09:30-09:44)
                                    orb_bars = today_bars[:15]
                                    if len(orb_bars) >= 5:  # 至少5根才算有效
                                        orb_high = max(float(b.get("high", 0)) for b in orb_bars)
                                        orb_low = min(float(b.get("low", 0)) for b in orb_bars)
                                        if orb_high > 0 and orb_low > 0 and orb_high > orb_low:
                                            ts_ctx["opening_high"] = orb_high
                                            ts_ctx["opening_low"] = orb_low
                                            ts_ctx["opening_range_ready"] = len(today_bars) >= 15
                                except Exception as orb_e:
                                    logger.debug(f"[Router] {symbol} ORB区间计算异常: {orb_e}")

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

                            logger.debug(
                                f"[Router] {symbol} ★★★ LV3 主通道 | "
                                f"price=${ts_ctx['price']:.2f} RSI={ts_ctx.get('rsi', 0):.1f} "
                                f"MACD={ts_ctx.get('macd', 0):.4f} ATR={ts_ctx.get('atr', 0):.2f} "
                                f"trend={ts_ctx.get('trend', 'N/A')}"
                                f" | ticker={ts_ctx.get('ticker_signal', 'N/A')}"
                                f" | ob={ts_ctx.get('ob_signal', 'N/A')}"
                            )
                    else:
                        # K 线数据不足，使用基础报价
                        ts_ctx = {
                            "price": futu_quote["price"],
                            "change_percent": futu_quote.get("change_percent", 0.0),
                            "volume": futu_quote.get("volume", 0),
                            "data_source": "futu_lv3_quote_only",
                        }
                        logger.warning(f"[Router] {symbol} LV3 K 线数据不足 ({len(klines) if klines else 0} 条)")

                    # 日K线ATR fallback: K_1M失败/不足时, ts_ctx无atr → 从日K线计算
                    # 根因修复: 原日K线ATR覆盖在 if klines>=30 → if tech: 块内,
                    # K_1M失败时永远不执行 → 盘前ATR=0 → vol_buckets误匹配low桶
                    if ts_ctx and ts_ctx.get("price", 0) > 0 and not ts_ctx.get("atr"):
                        try:
                            daily_bars = self.get_daily_bars(symbol, count=30)
                            if daily_bars and len(daily_bars) >= 15:
                                from app.data.lv3_technical_calculator import LV3TechnicalCalculator as _Calc
                                _df = _Calc()._klines_to_dataframe(daily_bars)
                                if _df is not None and len(_df) >= 15:
                                    _h = _df["high"]; _l = _df["low"]; _pc = _df["close"].shift(1)
                                    _tr = pd.concat([_h - _l, (_h - _pc).abs(), (_l - _pc).abs()], axis=1).max(axis=1)
                                    _daily_atr = float(_tr.rolling(window=14).mean().iloc[-1])
                                    if _daily_atr > 0:
                                        ts_ctx["atr"] = round(_daily_atr, 2)
                                        _p = ts_ctx.get("price", 0)
                                        ts_ctx["atr_percent"] = round(_daily_atr / _p * 100, 2) if _p > 0 else 0.0
                                        logger.debug(f"[Router] {symbol} 日K线ATR fallback: {_daily_atr:.2f} ({_daily_atr/_p*100:.2f}%)")
                        except Exception as _atr_fb_err:
                            logger.debug(f"[Router] {symbol} 日K线ATR fallback 失败: {_atr_fb_err}")

            except Exception as e:
                errors.append(f"FutuLV3: {e}")
                logger.warning(f"[Router] Futu LV3 主通道失败 {symbol}: {e}")
        
        # Step 2: TwelveData 降级 (LV3 不可用 或 K线不足时)
        # P0 修复 (2026-05-23): 期权代码仅 Futu 支持，跳过 TwelveData/Finnhub
        # 根因: TwelveData 不支持期权符号格式，每次调用浪费 API 配额并产生 ERROR
        if is_option and (not ts_ctx or ts_ctx.get("price", 0) <= 0):
            logger.debug(f"[Router] {symbol} 期权代码 Futu 无数据 (非交易时段/已过期)，跳过 TwelveData")
            return None
        # P1 修复 (2026-06-17): quote_only 也允许 TwelveData 降级获取技术指标
        # 根因: K线<30条时 data_source=quote_only (无 RSI/MACD/ATR),
        #   但原代码将 quote_only 视为"已完成"跳过 TwelveData → IC 死锁
        _needs_td_fallback = (
            not ts_ctx
            or ts_ctx.get("data_source") not in ("futu_lv3", "futu_lv3_quote_only")
            or ts_ctx.get("data_source") == "futu_lv3_quote_only"
        )
        if _needs_td_fallback:
            if self.td:
                try:
                    td_ctx = self.td.get_full_market_context(symbol)
                    if td_ctx and td_ctx.get("price", 0) > 0:
                        if ts_ctx and ts_ctx.get("data_source") == "futu_lv3_quote_only":
                            # 合并: 保留 Futu LV3 实时报价, 补充 TwelveData 技术指标
                            _futu_price_keys = {"price", "change_percent", "volume",
                                                "lv3_bid", "lv3_ask", "lv3_bid_vol",
                                                "lv3_ask_vol", "lv3_delay_ms"}
                            for _k, _v in td_ctx.items():
                                if _k not in _futu_price_keys:
                                    ts_ctx[_k] = _v
                            ts_ctx["data_source"] = "futu_lv3+td"
                            logger.debug(f"[Router] {symbol} LV3 报价 + TwelveData 技术指标合并成功")
                        else:
                            ts_ctx = td_ctx
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

        # Step 6: SPY 大盘 — 复用 get_spy_context() (含 MA200 缓存逻辑)
        # 2026-06-16 修复: 原来内联构建 spy_ctx 缺少 spy_ma200,
        # 导致 per-symbol context 的 spy_ma200 永远为 0
        spy_ctx = None
        try:
            spy_ctx = self.get_spy_context()
            if spy_ctx:
                logger.debug(
                    f"[Router] SPY 上下文: price=${spy_ctx.get('spy_price', 0):.2f} "
                    f"ma200=${spy_ctx.get('spy_ma200', 0):.2f}"
                )
        except Exception as e:
            logger.debug(f"[Router] get_spy_context() 失败: {e}")

        if not spy_ctx and self.td:
            try:
                spy_ctx = self.td.get_spy_context()
            except Exception as e:
                logger.warning(f"[Router] TwelveData 获取 SPY 失败：{e}")
        
        # 2026-04-24: AlphaVantage SPY 已禁用 (原因同上)
        
        if spy_ctx:
            ts_ctx.update({
                "spy_price": spy_ctx.get("spy_price", 0.0),
                "spy_ma200": spy_ctx.get("spy_ma200", 0.0),
                "spy_ma20": spy_ctx.get("spy_ma20", 0.0),
                "spy_ma50": spy_ctx.get("spy_ma50", 0.0),
                "spx_change_percent": spy_ctx.get("spx_change_percent", 0.0),
                "qqq_change_percent": spy_ctx.get("qqq_change_percent", 0.0),
                "price_change_5d": spy_ctx.get("price_change_5d", 0.0),
            })

        # Step 7: 市场状态判断
        ts_ctx["market_regime"] = self._calc_market_regime(ts_ctx)

        # Step 8: RFC-054 字段默认值补全
        # 确保所有 RFC-054 必需字段在任何数据路径下都有值
        # (LV3 主通道已计算这些字段，此处为 fallback/降级 路径兜底)
        ts_ctx.setdefault("slope_5m", 0.0)
        ts_ctx.setdefault("volume_ratio", 1.0)
        ts_ctx.setdefault("high_distance_pct", 0.0)
        ts_ctx.setdefault("trend", "NEUTRAL")
        ts_ctx.setdefault("order_flow_imbalance", 0.0)
        ts_ctx.setdefault("ticker_buy_sell_ratio", 1.0)

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
        """VIX: Alpha Vantage 真实数据 → 默认 20.0 (向后兼容)"""
        vix = self.get_vix_or_none()
        return vix if vix is not None else 20.0

    def get_vix_or_none(self) -> Optional[float]:
        """VIX 真实值; 拿不到返回 None (不兜底 20.0).

        RFC-060 (2026-06-22): market_regime 等对 VIX 敏感的场景需区分真实值与兜底,
        20.0 卡在 NORMAL/CAUTIOUS 边界, 兜底会误判 regime.
        """
        if self.av:
            try:
                return self.av.get_vix_or_none()
            except Exception as e:
                logger.warning(f"[Router] AlphaVantage 获取 VIX 失败：{e}")
        return None

    def get_spy_context(self) -> Dict[str, Any]:
        """SPY 大盘上下文：LV3 快照 + MA200 (2026-06-16 修复: 补充 spy_ma200 计算)

        P0 修复: 原来仅返回 {"spy_price": ...}, 导致 spy_ma200 永远为 0,
        regime_filter 始终 FALLBACK→CAUTION (position_scale=0.50, min_strength=55)。

        修复: 从 Futu LV3 获取 250 根日 K 线计算 MA200, 结果缓存 30 分钟。
        """
        result: Dict[str, Any] = {}

        # ★ Step 1: LV3 快照获取 SPY 实时价格
        if self.futu:
            try:
                spy_quote = self.futu.get_quote("US.SPY")
                if spy_quote and spy_quote.get("price", 0) > 0:
                    result["spy_price"] = spy_quote["price"]
            except Exception as e:
                logger.debug(f"[Router] LV3 获取 SPY 报价失败: {e}")

        # ★ Step 2: MA200 计算 (30 分钟缓存, 避免每次扫描都拉 250 根日 K)
        # MA200 每日仅变化 1 个数据点, 30 分钟刷新完全足够
        now = datetime.now()
        cache_valid = (
            self._spy_ma200_cache is not None
            and self._spy_ma200_cache_time is not None
            and (now - self._spy_ma200_cache_time) < self._spy_ma200_cache_ttl
        )
        if cache_valid:
            result["spy_ma200"] = self._spy_ma200_cache
            # P0 (2026-07-03): 缓存命中时也返回 MA20/MA50
            if self._spy_ma20_cache is not None:
                result["spy_ma20"] = self._spy_ma20_cache
            if self._spy_ma50_cache is not None:
                result["spy_ma50"] = self._spy_ma50_cache
            logger.debug(f"[Router] SPY MA200 使用缓存: {self._spy_ma200_cache:.2f}")
        elif self.futu:
            try:
                # 2026-06-16 修复: get_kline() 对 K_DAY 过滤到仅当日, 只剩 1 根
                # 改用 Futu request_history_kline 直接拉取完整日 K
                # 2026-06-30 根因修复: 加start/end + max_count>=范围交易日数
                from futu import KLType
                from datetime import date as _d, timedelta as _td
                _end = _d.today().isoformat()
                _start = (_d.today() - _td(days=400)).isoformat()
                if self.futu._ensure_connected() and self.futu._quote_ctx:
                    ret, data, _ = self.futu._quote_ctx.request_history_kline(
                        code="US.SPY", ktype=KLType.K_DAY, max_count=320,
                        start=_start, end=_end
                    )
                    if ret == 0 and data is not None and len(data) >= 50:
                        closes = [float(row["close"]) for _, row in data.iterrows() if float(row["close"]) > 0]
                        if len(closes) >= 50:
                            # 使用全部可用日 K 计算 MA (理想 200, 最少 50)
                            ma200 = sum(closes[-200:]) / min(len(closes), 200)
                            self._spy_ma200_cache = ma200
                            self._spy_ma200_cache_time = now
                            result["spy_ma200"] = ma200
                            # P0 (2026-07-03): 顺带计算 MA20/MA50, 避免依赖 AlphaVantage (免费 API 25次/天限流)
                            if len(closes) >= 20:
                                ma20 = sum(closes[-20:]) / 20
                                self._spy_ma20_cache = ma20
                                result["spy_ma20"] = ma20
                            if len(closes) >= 50:
                                ma50 = sum(closes[-50:]) / 50
                                self._spy_ma50_cache = ma50
                                result["spy_ma50"] = ma50
                            logger.info(
                                f"[Router] SPY MA200 已计算: {ma200:.2f} "
                                f"(从 {len(closes)} 根日 K, 缓存 30min)"
                            )
                        else:
                            logger.debug("[Router] SPY MA200 收盘价不足")
                    else:
                        logger.debug(f"[Router] SPY MA200 history_kline 失败: ret={ret}")
            except Exception as e:
                logger.warning(f"[Router] SPY MA200 计算失败: {e}")

        # 2026-04-24: AlphaVantage SPY 已禁用，盘前/盘后无新增技术指标
        # TwelveData key 无效，跳过
        return result

    def get_technical_indicators(
        self,
        symbol: str,
        indicators: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        获取技术指标

        P0 修复 (2026-05-02): 原方法不存在导致信号链路完全断裂。
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

        # 映射: full context 字段
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
        if self.futu and hasattr(self.futu, 'disconnect'):
            try:
                self.futu.disconnect()
                logger.info("[MarketDataRouter] Futu LV3 连接已断开")
            except Exception as e:
                logger.warning(f"[MarketDataRouter] Futu 断开失败: {e}")
        logger.info("[MarketDataRouter] 所有连接已关闭")


# ── 进程级共享单例 (2026-07-11) ────────────────────────
# 根因修复: 此前 market_regime.MarketRegimeGate() / factor_engine 等处直接
# new MarketDataRouter(), 导致每个 playbook 周期都新建一个 Futu LV3 连接 +
# 30s watchdog 线程 (旧实例被线程引用无法回收 → 线程泄漏 + OpenD 连接churn).
# 所有非启动路径应通过 get_market_data_router() 复用同一实例.
_shared_router: Optional["MarketDataRouter"] = None
_shared_router_lock = threading.Lock()


def get_market_data_router(**kwargs) -> "MarketDataRouter":
    """返回进程级共享的 MarketDataRouter 单例 (懒创建, 线程安全, 双重检查锁).

    启动时 scheduler 会通过 set_market_data_router() 注册主 router,
    因此常规运行期此函数直接返回那个已连接 Futu 的实例, 不会二次建连.
    仅在无人注册的独立上下文 (CLI/测试) 才懒创建一个.
    """
    global _shared_router
    if _shared_router is None:
        with _shared_router_lock:
            if _shared_router is None:
                _shared_router = MarketDataRouter(**kwargs)
    return _shared_router


def set_market_data_router(router: "MarketDataRouter") -> None:
    """注册已创建的 router 为进程级共享单例 (启动时由 scheduler 调用).

    让全进程共用 scheduler 已建立的 Futu 连接, 避免任何二次创建.
    """
    global _shared_router
    with _shared_router_lock:
        _shared_router = router


# 模块级导出
__all__ = [
    "MarketDataRouter",
    "MarketDataRouterError",
    "DataSourceError",
    "AllSourcesFailedError",
    "get_market_data_router",
    "set_market_data_router",
]
