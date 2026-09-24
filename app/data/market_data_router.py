"""
市场数据路由器

数据源架构（2026-04-20 升级为 LV3 主通道）：
  ★★★ Primary:   Futu LV3    — 实时报价 + K线 → 本地计算技术指标 (无 API 限制, 延迟 <10ms)
  Secondary: TwelveData  — 技术指标降级 (仅当 LV3 不可用时)
  Tertiary:  Alpha Vantage — 最终备用数据源，并提供真实 VIX
  Auxiliary: Finnhub     — 新闻情绪分析

调用策略（2026-04-20 升级）：
  - 实时价格 + 技术指标 → Futu LV3 快照 + K线 → 本地计算 (MA/RSI/MACD/ATR/VWAP/BOLL/SuperTrend)
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
        # DEB-20260903 #20: 期权分支从未赋值 daily_klines, 后续引用抛 UnboundLocalError
        daily_klines = None
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
                        # 2026-07-20 修复: count 200→800 — get_kline HTTP 冷路径 start=T-1,
                        #   范围含 T-1+今日 ~780 根 1min bar; OpenD 对 max_count<范围总数返回
                        #   "最旧的N根" (见 futu_lv3_provider 2026-06-30 注释), count=200 时
                        #   冷缓存拿到的是纯 T-1 旧 bar → slope_5m 算的是昨天的斜率,
                        #   当日 VWAP/ORB/今日过滤全部落空. 800 覆盖两日全时段.
                        klines = self.futu.get_kline(symbol, ktype="K_1M", count=800)
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
                            logger.debug(f"[Router] 日ATR覆盖失败 (用1分钟ATR): {_atr_err}")

                        # ── RFC-068 审计修复 (2026-08-29): prev_close 注入 ──
                        # 本函数此前从不输出 prev_close 键 → pipeline.py B门硬门读取恒 0
                        # → "px>0 and pc>0" 第一步即短路 → 门结构性永不触发 (8/29 复查实测;
                        # RFC-068 文档曾声称该键存在, 为错误声称, 见其勘误节)。
                        # 昨收口径: 日K自算真昨收 (承 de13b8d0 教训; 不用快照 prev_close_price —
                        # 盘前概率性返回两天前收盘, 62% 实测)。优先复用主路径已拉的
                        # daily_klines/count=60 (零新增API调用), 缺失时才走缓存的
                        # get_daily_bars; 日K全不可得回落 quote.prev_close。
                        # 异常 fail-open (缺键 = B门按其"数据缺失放行"语义放行)。
                        try:
                            if not ts_ctx.get("prev_close"):
                                import pytz as _pz_pc
                                from datetime import datetime as _dt_pc
                                _today_pc = _dt_pc.now(_pz_pc.timezone("US/Eastern")).strftime("%Y-%m-%d")
                                # daily_klines 仅在非期权主路径赋值 (期权/报价路径无此局部) → locals 守卫
                                _bars_pc = locals().get("daily_klines") or self.get_daily_bars(symbol, count=60)
                                _closes_pc = [
                                    (str(b.get("time_key") or b.get("time") or "")[:10],
                                     float(b.get("close", 0) or 0))
                                    for b in (_bars_pc or [])
                                ]
                                _prior_pc = [c for d, c in _closes_pc if d and d < _today_pc and c > 0]
                                if _prior_pc:
                                    ts_ctx["prev_close"] = _prior_pc[-1]
                                elif futu_quote and float(futu_quote.get("prev_close", 0) or 0) > 0:
                                    ts_ctx["prev_close"] = float(futu_quote["prev_close"])
                        except Exception as _pc_err:
                            logger.debug(f"[Router] prev_close注入跳过 {symbol}: {_pc_err}")

                        # ★★★ FIX 2026-07-11: 富因子注入原被误缩进进 except 分支, 移回正常路径
                        # 用此 if 承载块内 28 缩进 (恒真: 外层已判 ts_ctx 非 None), 避免大范围重排
                        if ts_ctx is not None:
                            # ★★★ 关键修复: 使用 LV3 实时快照价格覆盖 K 线收盘价
                            # K 线数据可能存在延迟或复权差异，快照价格是最新的真实成交价。
                            if futu_quote and futu_quote.get("price", 0) > 0:
                                ts_ctx["price"] = futu_quote["price"]
                                logger.debug(f"[Router] {symbol} 使用快照价格覆盖 K 线价格: ${futu_quote['price']:.2f}")
                            
                            # ★★★ 2026-07-20 修复: 当日累计 VWAP — 07-16 遗留语义错配
                            # 根因: ts_ctx["vwap"] 主路径来自日K calculate_from_klines(daily_klines),
                            #   _calc_vwap 对 60 根日K 全量 cumsum → 实为"60日量加权均价"而非当日 VWAP;
                            #   VWAP 入场硬门 (entry_vwap_gate, 实盘启用) 误用该值 → 近期上涨股
                            #   SHORT 恒被拦 / 下跌股 LONG 恒被拦 (方向性系统误拦).
                            # 修复: 用 1min K 线过滤今日 bar 单独算 cum(tp×vol)/cum(vol),
                            #   写 vwap_intraday (门专用) 并覆盖 vwap (07-16 修复的原始意图).
                            #   今日 bar<5 (盘前/开盘头几分钟) → vwap_intraday=0, 门侧视缺失 fail-open.
                            ts_ctx["vwap_intraday"] = 0.0
                            try:
                                if klines:
                                    import pytz as _pz_vw
                                    from datetime import datetime as _dt_vw
                                    _today_vw = _dt_vw.now(_pz_vw.timezone('US/Eastern')).strftime("%Y-%m-%d")
                                    _cum_tpv = 0.0
                                    _cum_v = 0.0
                                    _n_today = 0
                                    for _k in klines:
                                        _tk = str(_k.get("time_key") or _k.get("time") or "")
                                        if _tk[:10] != _today_vw:
                                            continue
                                        _kh = float(_k.get("high", 0) or 0)
                                        _kl = float(_k.get("low", 0) or 0)
                                        _kc = float(_k.get("close", 0) or 0)
                                        _kv = float(_k.get("volume", 0) or 0)
                                        if _kh <= 0 or _kl <= 0 or _kc <= 0 or _kv <= 0:
                                            continue
                                        _cum_tpv += (_kh + _kl + _kc) / 3.0 * _kv
                                        _cum_v += _kv
                                        _n_today += 1
                                    if _n_today >= 5 and _cum_v > 0:
                                        _vwap_id = round(_cum_tpv / _cum_v, 4)
                                        ts_ctx["vwap_intraday"] = _vwap_id
                                        ts_ctx["vwap"] = _vwap_id
                            except Exception as _vw_err:
                                logger.debug(f"[Router] {symbol} 当日VWAP计算跳过: {_vw_err}")

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
                            # DEB-ruler §7.5-C/D (2026-08-04): push 路径无真盘口 →
                            # bid_vol=ask_vol=0 (futu_lv3_provider.py:791-795)。此时 ofi 无定义,
                            # 置 None 让消费端按"不可用"跳过, 不再以 0.0 假装有数据
                            # (0.0 曾使 D4 评分恒 +0.1/LONG、ensemble agree 恒 -1 偏压)。
                            # 哨兵 lv3_book_is_proxy 供 S2 窄校准/遥测识别退化样本。
                            ts_ctx["lv3_book_is_proxy"] = not (total_vol > 0)
                            ts_ctx["order_flow_imbalance"] = (
                                (bid_vol - ask_vol) / total_vol if total_vol > 0 else None
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
                            # DEB-20260903 #21: provider get_quote 产出键为 high/low/open
                            # (futu_lv3_provider.py:885-887), 原读 high_price 等不存在的键 → 恒 0
                            day_high = float(futu_quote.get("high", 0) or futu_quote.get("high_price", 0) or 0)
                            if day_high > 0 and ts_ctx["price"] > 0:
                                ts_ctx["high_distance_pct"] = round(
                                    (ts_ctx["price"] - day_high) / day_high * 100, 3
                                )

                            # ── ORB: 注入日内高/低/开盘价 + 开盘区间 ──
                            ts_ctx["day_high"] = day_high
                            ts_ctx["day_low"] = float(futu_quote.get("low", 0) or futu_quote.get("low_price", 0) or 0)
                            ts_ctx["open_price"] = float(futu_quote.get("open", 0) or futu_quote.get("open_price", 0) or 0)

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
                                        # 2026-07-20 修复: HTTP 路径 bar 键是 "time", push 路径是 "time_key"
                                        #   — 原只认 time_key, HTTP 冷路径下 today_bars 恒空 → ORB 区间永不注入
                                        tk = str(k.get("time_key") or k.get("time") or "")
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
                                            # DEB-20260903 #21: 真实 OrderBook 覆盖后重算哨兵/OFI —
                                            # 原实现保留 quote 路径的 proxy 标记, 把已有真实盘口的样本误标为 proxy
                                            _ob_bv = float(ob_factors.get("bid_vol", 0) or 0)
                                            _ob_av = float(ob_factors.get("ask_vol", 0) or 0)
                                            _ob_tv = _ob_bv + _ob_av
                                            ts_ctx["lv3_book_is_proxy"] = not (_ob_tv > 0)
                                            if _ob_tv > 0:
                                                ts_ctx["order_flow_imbalance"] = (_ob_bv - _ob_av) / _ob_tv
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
        # 2026-07-16 根因修复: 原白名单 ("futu_lv3","futu_lv3_quote_only") 漏了
        #   "futu_lv3_daily"/"futu_lv3_1m_fallback" (两者均含完整技术指标) → 被判"需 TD" →
        #   TD 可用时下方用 TD 覆盖掉本来更好的 LV3 日线数据 (source=twelvedata),
        #   TD 限流时反而保留 LV3 → 白白浪费 TD 调用打爆 429。
        #   正确语义: 仅当 LV3 完全无数据(None) 或 仅有报价无指标(quote_only) 才需 TD。
        _lv3_full = bool(ts_ctx) and ts_ctx.get("data_source") in (
            "futu_lv3", "futu_lv3_daily", "futu_lv3_1m_fallback"
        )
        # DEB-20260904 #146: 期权永不降级 TwelveData (TD 不支持期权符号格式) —
        # 原期权 quote_only 上下文不在 _lv3_full 白名单 → 仍触发 TD 调用,
        # 浪费 API 配额且垃圾数据可能被 merge 进期权上下文; 上方 L757 已处理
        # 期权无数据场景, 能走到这里的期权必有有效报价 → 直接跳过 TD
        _needs_td_fallback = (not _lv3_full) and (not is_option)
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
                            # 2026-07-16 修复: 补 data_source 标签。td_ctx 自身不带标签,
                            #   直接赋值会导致日志 source=unknown (掩盖了"已降级到 TwelveData"这一真实信号)。
                            if isinstance(ts_ctx, dict):
                                ts_ctx.setdefault("data_source", "twelvedata")
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
            logger.warning(f"[Router] get_spy_context() 失败: {e}", exc_info=True)

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
        # §7.5-C/D: None=盘口不可用 (与主路径退化语义一致), 不再以 0.0 假装有数据
        ts_ctx.setdefault("order_flow_imbalance", None)
        ts_ctx.setdefault("ticker_buy_sell_ratio", 1.0)
        # RFC-068 审计修复补全: TD 降级路径的昨收键名是 previous_close → 归一为
        # prev_close (LV3 全挂时门仍有数据; TD 缺该字段回落现价 → chg=0 自然放行)。
        if not ts_ctx.get("prev_close") and float(ts_ctx.get("previous_close", 0) or 0) > 0:
            ts_ctx["prev_close"] = float(ts_ctx["previous_close"])

        # ── RFC-091 / DEB-20260922 §4.4 修复 (2026-09-22, 用户裁「先修分母」) ──
        # 病灶 (行级): ts_ctx["change_percent"] 由 LV3TechnicalCalculator._calc_change_percent
        #   从**日K序列**算出 = (最后完整bar − 其前一根bar)/前一根bar ⇒ 在当日盘前/盘中它恒等于
        #   **前一交易日的涨跌幅**; 而上方"用快照价覆盖 K 线价"只换了 price、没重算 change_percent
        #   ⇒ ctx 输出"实时价 + 昨日涨跌幅"混装。
        #   实证 (DEB-20260922 §4): RIVN 9/21 ctx −2.66% == 9/18 涨跌幅 (14.99 vs 15.40),
        #   NOK 9/15 −13.30% == 9/14 涨跌幅, RUM 9/14 −2.65% == 9/11 涨跌幅, 差 ≤0.003pp;
        #   且 W7 不变量 (chg 分母分叉) 自 9/11 起每个交易日报 38 键基线外新增。
        # 修法: 真昨收 (RFC-068 日K自算) 与实时价同时可得时重算, 并写溯源键; 缺任一则保留原值
        #   (fail-open, 与 RFC-068 prev_close 注入同语义, 不打新守卫)。
        # 影响面: intraday_scorer:494/610 (slope_5m 不可用时的 fallback 路径), pipeline.py:5830,
        #   market_data_stage:168, entry_timing_shadow 的 features.change_percent。
        try:
            _pc_rf = float(ts_ctx.get("prev_close") or 0)
            _px_rf = float(ts_ctx.get("price") or 0)
            if _pc_rf > 0 and _px_rf > 0:
                ts_ctx["change_percent"] = round((_px_rf - _pc_rf) / _pc_rf * 100.0, 3)
                ts_ctx["change_percent_source"] = "router_recomputed_daily_close"
        except Exception as _cp_rf_err:
            logger.debug(f"[Router] change_percent重算跳过 {symbol}: {_cp_rf_err}")

        # 添加时间戳
        # DEB-20260903 #22: 用 ET naive 替代服务器本地 datetime.now() —
        # 同函数 L522/L560/L633 均按 US/Eastern 推"今日", timestamp 须同口径,
        # 否则非 ET 服务器上 ts_ctx 新鲜度/日期归因与 ET 交易日错位
        try:
            from app.utils.timezone import now_et as _now_et_ts
            ts_ctx["timestamp"] = _now_et_ts().replace(tzinfo=None).isoformat()
        except Exception:
            ts_ctx["timestamp"] = datetime.now().isoformat()

        logger.debug(
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
        # 2026-07-16 修复: 默认值 0 (原 0.001)。0.001 使下方 `avg_vol > 0` 恒真且分母极小,
        #   任何真实个股波动率(~0.02) > 0.001*3=0.003 → regime 永远误判 HIGH_VOL, 无视真实 VIX。
        #   avg_volatility 仅由 LV3 日线计算器填充; 缺失(降级路径)时应跳过个股波动率分支。
        #   与 signal_scorer.py 的默认值 0 保持一致。
        avg_vol = ctx.get("avg_volatility", 0)
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

        2026-08-03 修复 (regime 精准性验证, 见 obsidian 2026-08-03_regime_validation):
        根因: 非交易时段 (盘前生成指令) 用 FRED VIXCLS 日频 — FRED 更新滞后 2-3 天
        (8/3 仅到 7/30), 7/31 盘前生成 23 条指令全用 7/29 的 20.66 (与非交易时段
        FRED 优先的 AlphaVantage 链一致), 与 SPY 实际波动 12.4% 脱节, 污染 regime.
        修复: 盘前/交易时段优先用 Futu VIXY 实时报价 × 0.85 contango 校准 (与
        AlphaVantage _fetch_finnhub_vixy 同系数; Futu 已连接且有 LV3 行情, US.VIXY
        实测可取, 盘前亦有报价), 失败再走 AlphaVantage (Finnhub→FRED) 链.
        """
        if self.futu:
            try:
                # 延迟导入避免循环依赖; 盘前/交易时段用实时代理 (OVERNIGHT/WEEKEND 用 FRED 日频可接受)
                # 2026-09-14: 窗口判据收敛到单一真源 app/data/vix_source.py::is_vixy_proxy_window
                #   (修前这条规则在 4 个 VIX 生产者里各写一份且分成两派, 盘前会同时存在两个
                #    VIX: 执行门 14.71 vs 看板 vix_cache 17.84 — 见 DEB-20260914 §八)。
                from app.data.vix_source import is_vixy_proxy_window
                if is_vixy_proxy_window():
                    quote = self.futu.get_quote("US.VIXY")
                    if quote and quote.get("price", 0) > 0:
                        # 2026-09-14 (P0-1): 换算系数收敛到单一真源 app/data/vix_source.py
                        from app.data.vix_source import vixy_to_vix
                        vix_est = vixy_to_vix(quote["price"])
                        if vix_est is not None:
                            logger.info(f"[Router] VIX (Futu VIXY实时): VIXY=${quote['price']:.2f} → VIX≈{vix_est:.2f}")
                            return vix_est
            except Exception as e:
                logger.warning(f"[Router] Futu VIXY 获取失败: {e}")
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
                # DEB-20260903 #23: SPY 历史K线 end/start 边界用 ET 日期 —
                # 原 _d.today() 取服务器本地日期, 非 ET 时区 end 可能超前/落后一日
                try:
                    from app.utils.timezone import now_et as _now_et_spy
                    _today_spy = _now_et_spy().date()
                except Exception:
                    _today_spy = _d.today()
                _end = _today_spy.isoformat()
                _start = (_today_spy - _td(days=400)).isoformat()
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

    # 2026-08-29 减法: get_technical_indicators() 已删除 —
    # 2026-05-02 为信号链断裂补的 LV3 指标桥, 后调用方迁走, 全仓零调用
    # (rg 验证仅定义处自引用). 键对齐职责归消费者 (如 sss.py 双键读取模式).

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

        # DEB-20260903 #24: 主通道 Futu LV3 纳入健康口径 —
        # 原仅统计 td/fh/av 三个备用源, 主通道宕掉时只要任一备用有 token
        # 仍报 healthy=True, 监控对主通道失能无感知
        if self.futu:
            try:
                _futu_ok = bool(self.futu._ensure_connected())
                status["sources"]["futu_lv3"] = {"healthy": _futu_ok}
                if _futu_ok:
                    sources_healthy += 1
            except Exception as e:
                status["sources"]["futu_lv3"] = {"healthy": False, "error": str(e)}

        # 至少一个数据源健康即认为整体健康
        status["healthy"] = sources_healthy > 0
        status["sources_healthy"] = sources_healthy
        status["sources_total"] = len([s for s in [self.td, self.fh, self.av, self.futu] if s])

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
