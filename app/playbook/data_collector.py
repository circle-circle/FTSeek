"""
Playbook 数据收集器编排层

组合调用现有 Provider，不重写数据获取逻辑。
收集 6 维度数据：fundamental, technical, insider, news, earnings, sentiment

设计原则：
  1. 委托现有 Provider，不重复实现
  2. 每个维度有 quality 字段 (ok/stale/partial/missing)
  3. 支持 batch 收集多只股票
  4. 非阻塞，失败静默，不影响主流程
"""
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


@dataclass
class FundamentalData:
    """基本面数据"""
    pe: Optional[float] = None
    market_cap: Optional[float] = None  # 百万美元
    industry: Optional[str] = None
    revenue_growth_yoy: Optional[float] = None
    roe: Optional[float] = None
    debt_to_equity: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    source: str = ""
    fetched_at: str = ""
    quality: str = "missing"  # ok/stale/partial/missing


@dataclass
class TechnicalData:
    """技术面数据"""
    price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    ma20: Optional[float] = None
    ma50: Optional[float] = None
    rsi_14: Optional[float] = None
    macd_signal: Optional[str] = None  # golden_cross/dead_cross/neutral
    atr_14: Optional[float] = None
    bollinger_position: Optional[str] = None  # upper/middle/lower
    volume_5d_avg_ratio: Optional[float] = None  # 近5日成交量 / 20日均量
    trend_5d: Optional[str] = None  # up/down/flat
    candles_5d: List[str] = field(default_factory=list)  # 阳线/阴线
    quality: str = "missing"


@dataclass
class InsiderData:
    """内部人交易数据"""
    buy_count_30d: int = 0
    sell_count_30d: int = 0
    net_shares_30d: int = 0
    cluster_score: float = 0.0  # 0-1, 来自 SECForm4Provider
    cluster_buyers: List[str] = field(default_factory=list)
    signal: str = "neutral"  # strong_buy/buy/neutral/sell
    transactions: List[Dict] = field(default_factory=list)
    quality: str = "missing"


@dataclass
class NewsData:
    """新闻数据"""
    headlines: List[str] = field(default_factory=list)
    sentiment_score: float = 0.0  # -1.0 ~ 1.0
    source: str = ""
    fetched_at: str = ""
    quality: str = "missing"


@dataclass
class EarningsData:
    """财报数据"""
    next_date: Optional[str] = None
    days_until: Optional[int] = None
    history: List[Dict] = field(default_factory=list)  # 最近4季
    beat_rate: float = 0.0  # 超预期比例
    sector_atr_multiplier: float = 1.0  # 行业 ATR 乘数
    quality: str = "missing"


@dataclass
class SentimentData:
    """情绪数据"""
    news_sentiment: float = 0.0  # -1.0 ~ 1.0, Finnhub 原生
    articles_count: int = 0
    vix: Optional[float] = None  # 当前 VIX; None = 取不到 (2026-09-14: 原为 20.0 哨兵,
    #   使"缺失"与"真值恰为 20.0"不可区分。消费方 instruction_generator 的
    #   market_state_overlay 已按 `or 20.0` 显式兜底 (fail-open), 故语义不变。
    quality: str = "missing"


@dataclass
class PlaybookData:
    """单只股票的完整 6 维度数据"""
    symbol: str
    fundamental: FundamentalData = field(default_factory=FundamentalData)
    technical: TechnicalData = field(default_factory=TechnicalData)
    insider: InsiderData = field(default_factory=InsiderData)
    news: NewsData = field(default_factory=NewsData)
    earnings: EarningsData = field(default_factory=EarningsData)
    sentiment: SentimentData = field(default_factory=SentimentData)
    
    # 交叉一致性校验警告 (由 data_collector._check_consistency 填充)
    consistency_warnings: List[str] = field(default_factory=list)
    
    # ========== 便捷访问方法 ==========
    
    def get_sector(self) -> Optional[str]:
        """获取行业分类"""
        return self.fundamental.industry
    
    def get_price(self) -> Optional[float]:
        """获取当前价格"""
        return self.technical.price
    
    def get_ma20(self) -> Optional[float]:
        """获取 20 日均线"""
        return self.technical.ma20
    
    def get_rsi(self) -> Optional[float]:
        """获取 RSI(14)"""
        return self.technical.rsi_14
    
    def get_macd_signal(self) -> Optional[str]:
        """获取 MACD 信号 (golden_cross/dead_cross/neutral)"""
        return self.technical.macd_signal
    
    def get_volume_ratio(self) -> Optional[float]:
        """获取成交量比率 (近5日成交量 / 20日均量)"""
        return self.technical.volume_5d_avg_ratio
    
    def get_sentiment_score(self) -> float:
        """获取新闻情绪分数"""
        return self.news.sentiment_score
    
    def get_insider_cluster_score(self) -> float:
        """获取内部人集群买入分数 (0-1)"""
        return self.insider.cluster_score
    
    def get_insider_net_buy(self) -> int:
        """获取内部人净买入股数 (正=净买入, 负=净卖出)"""
        return self.insider.net_shares_30d
    
    def get_earnings_beat_rate(self) -> float:
        """获取财报超预期率"""
        return self.earnings.beat_rate
    
    def get_days_until_earnings(self) -> Optional[int]:
        """获取距离下次财报的天数"""
        return self.earnings.days_until
    
    def get_pe_below_industry(self) -> Optional[bool]:
        """检查 PE 是否低于行业平均.

        当前 Fallback: 由于尚未接入行业平均 PE 数据源, 使用绝对阈值 30
        作为简化判断 (pe 存在且 < 30 视为相对低估).
        TODO (P2): 接入行业/sector 平均 PE 后, 改用 个股PE < 行业平均PE 判断.
        """
        pe = self.fundamental.pe
        if pe is None:
            return None
        # Fallback: 绝对阈值, 因行业平均 PE 数据暂未接入
        return pe < 30 if pe > 0 else None


class PlaybookDataCollector:
    """Playbook 数据收集器编排层"""

    def __init__(
        self,
        fundamental_provider,
        finnhub_provider,
        insider_provider,
        earnings_calendar,
        futu_extended=None,
    ):
        """
        Args:
            fundamental_provider: FundamentalProvider 实例
            finnhub_provider: FinnhubProvider 实例
            insider_provider: InsiderProvider 实例 (SEC EDGAR Form 4)
            earnings_calendar: EarningsCalendar 实例
            futu_extended: FutuExtendedProvider 实例 (空头/分析师/内部人, 可选)
        """
        self.fundamental = fundamental_provider
        self.finnhub = finnhub_provider
        self.sec = insider_provider
        self.earnings = earnings_calendar
        self.futu_ext = futu_extended

    # 2026-08-22 (P1-2): VIX 全市场指标单次拉取缓存 — 逐标的实例化是浪费
    # 2026-09-14 (P2, 用户裁「全部处理」): 改走**执行门同源**的 market_state 检查器。
    #   原实现走 AlphaVantageProvider.get_vix() —— 两个病灶:
    #   ① 失败兜底 `return 20.0` 假值 (调用方用 `vix != 20.0` 猜"缺失", 真值等于
    #      20.0 时反被误判缺失; 20.0 又恰好卡在 overlay 档位边界上);
    #   ② 盘前/盘后走 FRED 日频 (T-1/T-2) ⇒ 与执行门/regime 侧的 VIXY 实时代理
    #      不同口径 (盘前恰恰是本链路的运行时段)。
    #   现改为 get_checker().get_vix_cached() (盘前/盘中优先 VIXY 实时代理, 30min
    #   缓存) —— Playbook 看到的就是 VIX 硬门看到的那个数; 取不到如实 None。
    #   缓存语义: 只要 ts>0 即视为"已有结果", **None 也缓存 5min** —— 否则每只标的
    #   都要重连一次 OpenD (连接风暴, 见 data_collector 顶部限流注释)。
    _vix_cache = {"value": None, "ts": 0}
    _VIX_CACHE_TTL = 300  # 5 分钟

    def _get_vix_cached(self):
        """VIX 全市场指标 (执行门同源), 5分钟缓存; 取不到返回 None (不造 20.0)。"""
        import time as _time
        now = _time.time()
        if self._vix_cache["ts"] > 0 and now - self._vix_cache["ts"] < self._VIX_CACHE_TTL:
            return self._vix_cache["value"]
        try:
            from app.analysis.market_state import get_checker
            vix = get_checker().get_vix_cached()
        except Exception as e:
            logger.debug(f"Failed to get VIX: {e}")
            vix = None
        self._vix_cache["value"] = vix
        self._vix_cache["ts"] = now
        return vix

    def collect(self, symbol: str) -> PlaybookData:
        """收集单只股票的 6 维度数据"""
        return PlaybookData(
            symbol=symbol,
            fundamental=self._collect_fundamental(symbol),
            technical=self._collect_technical(symbol),
            insider=self._collect_insider(symbol),
            news=self._collect_news(symbol),
            earnings=self._collect_earnings(symbol),
            sentiment=self._collect_sentiment(symbol),
        )

    def collect_batch(self, symbols: List[str], max_workers: int = 5) -> List[PlaybookData]:
        """批量收集，使用 ThreadPoolExecutor 并行"""
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_symbol = {
                pool.submit(self.collect, symbol): symbol for symbol in symbols
            }
            for future in as_completed(future_to_symbol):
                symbol = future_to_symbol[future]
                try:
                    data = future.result()
                    # 交叉一致性校验
                    warnings = self._check_consistency(data)
                    if warnings:
                        data.consistency_warnings = warnings
                    results.append(data)
                except Exception as e:
                    logger.error(f"Failed to collect data for {symbol}: {e}")
                    # 返回空数据，不中断流程
                    results.append(PlaybookData(symbol=symbol))
        return results

    def _collect_fundamental(self, symbol: str) -> FundamentalData:
        """
        委托 FundamentalProvider.get_data(symbol)

        Provider 返回 {"status": "ok", "financials": {...}, "next_catalyst": {...}, ...}
        """
        try:
            raw = self.fundamental.get_data(symbol)
            if raw.get("status") != "ok":
                return FundamentalData(quality="missing")

            # 根因修复 (2026-06-22): Provider 返回 {"data": {"financials": {...}}}
            # 2026-06-29 修复: 原代码 raw.get("financials") or raw.get("data") 拿到的是
            # 外层 wrapper dict (含 financials/analyst/news/catalyst 键), 不是 financials 本身
            data_wrapper = raw.get("data") or {}
            fin = data_wrapper.get("financials") or raw.get("financials") or {}

            # 字段映射: Provider 用 snake_case, 原代码期望 Finnhub camelCase
            pe = fin.get("pe_ttm") or fin.get("peTTM") or fin.get("pe_ratio") or fin.get("pe")
            market_cap = fin.get("market_cap") or fin.get("marketCapitalization")
            # industry 不在 financials 里, 从 next_catalyst 或 Futu snapshot 取
            industry = fin.get("industry") or fin.get("sector") or ""
            revenue_growth = fin.get("revenue_yoy") or fin.get("revenueGrowthTTMYoy") or fin.get("revenueGrowth")
            roe = fin.get("roe") or fin.get("roeTTM") or fin.get("returnOnEquity")
            debt_to_equity = fin.get("debt_to_equity") or fin.get("debtToEquity")
            high_52w = fin.get("52w_high") or fin.get("52WeekHigh") or fin.get("high52w")
            low_52w = fin.get("52w_low") or fin.get("52WeekLow") or fin.get("low52w")

            # 如果 financials 没有 industry, 尝试从 Futu snapshot 补充
            if not industry:
                nc = raw.get("next_catalyst", {})
                industry = nc.get("industry") or nc.get("sector") or ""

            # 判断 quality
            key_fields = [pe, market_cap, industry]
            if all(f is not None and f != 0 for f in key_fields):
                quality = "ok"
            elif any(f is not None and f != 0 for f in key_fields):
                quality = "partial"
            else:
                quality = "missing"

            return FundamentalData(
                pe=pe,
                market_cap=market_cap,
                industry=industry,
                revenue_growth_yoy=revenue_growth,
                roe=roe,
                debt_to_equity=debt_to_equity,
                high_52w=high_52w,
                low_52w=low_52w,
                source=raw.get("data_source", ""),
                fetched_at=raw.get("fetched_at", ""),
                quality=quality,
            )
        except Exception as e:
            logger.warning(f"Failed to collect fundamental for {symbol}: {e}")
            return FundamentalData(quality="missing")

    def _collect_technical(self, symbol: str) -> TechnicalData:
        """
        委托 MarketDataRouter → Futu LV3 K线 → LV3TechnicalCalculator
        """
        try:
            from app.data.market_data_router import MarketDataRouter
            
            # 获取全局 router 实例 (懒加载，复用连接)
            router = self._get_market_data_router()
            if router is None:
                logger.warning(f"MarketDataRouter not available for {symbol}")
                return TechnicalData(quality="missing")
            
            ctx = router.get_full_market_context(symbol)
            if not ctx or ctx.get("price", 0) <= 0:
                return TechnicalData(quality="missing")
            
            price = ctx.get("price", 0)
            bid = ctx.get("lv3_bid") or ctx.get("bid") or price
            ask = ctx.get("lv3_ask") or ctx.get("ask") or price
            ma20 = ctx.get("ma20")
            ma50 = ctx.get("ma50")
            rsi = ctx.get("rsi")
            macd = ctx.get("macd", 0)
            macd_signal = ctx.get("macd_signal", 0)
            atr = ctx.get("atr")
            vol_ratio = ctx.get("volume_ratio")
            trend = ctx.get("trend", "NEUTRAL")
            
            # MACD 信号判断
            macd_sig = "neutral"
            macd_hist = ctx.get("macd_hist", 0)
            if macd > 0 and macd_hist > 0:
                macd_sig = "golden_cross"
            elif macd < 0 and macd_hist < 0:
                macd_sig = "dead_cross"
            
            # 布林带位置
            bb_pos = None
            bb_upper = ctx.get("bb_upper") or ctx.get("boll_upper")
            bb_lower = ctx.get("bb_lower") or ctx.get("boll_lower")
            bb_middle = ctx.get("bb_middle") or ctx.get("boll_middle")
            if bb_upper and bb_lower and price > 0:
                if price >= bb_upper * 0.98:
                    bb_pos = "upper"
                elif price <= bb_lower * 1.02:
                    bb_pos = "lower"
                else:
                    bb_pos = "middle"
            
            # 近5日K线
            recent_closes = ctx.get("recent_closes", [])
            candles_5d = []
            for i in range(len(recent_closes) - 1):
                candles_5d.append("阳线" if recent_closes[i+1] > recent_closes[i] else "阴线")
            
            # 5日趋势
            trend_5d = "flat"
            change_5d = ctx.get("change_5d_pct", 0)
            if change_5d > 1:
                trend_5d = "up"
            elif change_5d < -1:
                trend_5d = "down"
            
            # 质量判断
            key_fields = [price, ma20, rsi]
            ok_count = sum(1 for f in key_fields if f is not None and f > 0)
            if ok_count >= 3:
                quality = "ok"
            elif ok_count >= 1:
                quality = "partial"
            else:
                quality = "missing"

            # 根因修复 (2026-06-22): RSI 缺失时用日K线补算
            # 盘后/非交易时段 K_1M 分钟线获取失败 → RSI=None → technical quality=partial
            # 修复: 从 日K线 计算 RSI(14)
            # 2026-06-28: 走Router.get_daily_bars()而非直接调futu(富途历史K线可能过期)
            if rsi is None and router:
                try:
                    dklines = router.get_daily_bars(symbol, count=30)
                    if dklines and len(dklines) >= 15:
                        closes = [k['close'] for k in dklines]
                        gains = []
                        losses = []
                        for i in range(1, len(closes)):
                            change = closes[i] - closes[i-1]
                            gains.append(max(0, change))
                            losses.append(max(0, -change))
                        if len(gains) >= 14:
                            avg_gain = sum(gains[:14]) / 14
                            avg_loss = sum(losses[:14]) / 14
                            for i in range(14, len(gains)):
                                avg_gain = (avg_gain * 13 + gains[i]) / 14
                                avg_loss = (avg_loss * 13 + losses[i]) / 14
                            if avg_loss == 0:
                                rsi = 100.0
                            else:
                                rs = avg_gain / avg_loss
                                rsi = round(100 - (100 / (1 + rs)), 2)
                            # 也从日K线补 MA20/MA50
                            if ma20 is None and len(closes) >= 20:
                                ma20 = round(sum(closes[-20:]) / 20, 2)
                            if ma50 is None and len(closes) >= 50:
                                ma50 = round(sum(closes[-50:]) / 50, 2)
                            # 重新判断质量
                            ok_count = sum(1 for f in [price, ma20, rsi] if f is not None and f > 0)
                            quality = "ok" if ok_count >= 3 else "partial" if ok_count >= 1 else "missing"
                            logger.debug(f"[data_collector] {symbol} RSI from daily kline: {rsi:.1f}")
                except Exception as dk_err:
                    logger.debug(f"[data_collector] {symbol} daily kline RSI fallback failed: {dk_err}")

            return TechnicalData(
                price=price,
                bid=bid,
                ask=ask,
                ma20=ma20,
                ma50=ma50,
                rsi_14=rsi,
                macd_signal=macd_sig,
                atr_14=atr,
                bollinger_position=bb_pos,
                volume_5d_avg_ratio=vol_ratio,
                trend_5d=trend_5d,
                candles_5d=candles_5d,
                quality=quality,
            )
        except Exception as e:
            logger.warning(f"Failed to collect technical for {symbol}: {e}")
            return TechnicalData(quality="missing")
    
    # 全局 MarketDataRouter 单例 (进程级复用)
    _router_instance = None
    _router_init_failed = False
    
    def _get_market_data_router(self):
        """获取或创建 MarketDataRouter 单例"""
        if PlaybookDataCollector._router_init_failed:
            return None
        if PlaybookDataCollector._router_instance is not None:
            return PlaybookDataCollector._router_instance
        try:
            import os
            from app.data.market_data_router import MarketDataRouter
            PlaybookDataCollector._router_instance = MarketDataRouter(
                twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY"),
                finnhub_api_key=os.environ.get("FINNHUB_API_KEY"),
                alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY"),
                futu_opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
                futu_opend_port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
                enable_futu_lv3=os.environ.get("ENABLE_FUTU_LV3", "true").lower() == "true",
            )
            return PlaybookDataCollector._router_instance
        except Exception as e:
            logger.error(f"Failed to init MarketDataRouter: {e}")
            PlaybookDataCollector._router_init_failed = True
            return None

    def _check_news_freshness(self, news_list: List[Dict]) -> str:
        """检查新闻新鲜度: 最新一条距今>6h → stale, 否则 ok."""
        try:
            if not news_list:
                return "missing"
            # Finnhub news 有 timestamp (Unix秒, UTC epoch)
            import time as _time
            latest_ts = 0
            for n in news_list:
                ts = n.get("timestamp") or n.get("time") or 0
                if isinstance(ts, (int, float)) and ts > latest_ts:
                    latest_ts = ts
            if latest_ts == 0:
                return "ok"  # 无法判断, 不降级
            age_hours = (_time.time() - latest_ts) / 3600
            if age_hours > 6:
                return "stale"
            return "ok"
        except Exception:
            return "ok"

    def _collect_insider(self, symbol: str) -> InsiderData:
        """
        委托 SECForm4Provider: fetch_company_form4s + detect_cluster_buys
        兼容 dataclass 和 dict 两种返回格式 (缓存=dict, 新鲜=dataclass)
        """
        try:
            bare = symbol.replace("US.", "").upper()
            
            # 获取 30 天内的 Form 4 交易 (返回 List, 元素可能是 dict 或 dataclass)
            txns = self.sec.fetch_company_form4s(bare, max_age_days=30)
            
            def _get(obj, key, default=None):
                """兼容 dict 和 dataclass 属性访问"""
                if isinstance(obj, dict):
                    return obj.get(key, default)
                return getattr(obj, key, default)
            
            # 统计买卖
            buy_count = sum(1 for t in txns if _get(t, "transaction_code") == "P")
            sell_count = sum(1 for t in txns if _get(t, "transaction_code") == "S")
            net_shares = sum(
                _get(t, "shares", 0) if _get(t, "transaction_code") == "P" else -_get(t, "shares", 0)
                for t in txns
            )
            
            # 集群买入检测 (内部方法兼容两种格式)
            try:
                cluster = self.sec.detect_cluster_buys(bare, window_days=30)
            except Exception:
                cluster = None
            cluster_score = cluster.score if cluster else 0.0
            cluster_buyers = []
            if cluster:
                cluster_buyers = [
                    f"{b.get('name', '?')} ({b.get('role', '?')})"
                    for b in cluster.buyers
                ]
            
            # 判断 signal
            if cluster_score > 0.5:
                signal = "strong_buy"
            elif buy_count > sell_count and net_shares > 0:
                signal = "buy"
            elif sell_count > buy_count and net_shares < 0:
                signal = "sell"
            else:
                signal = "neutral"
            
            # 转为 dict 列表
            transactions = [
                {
                    "insider": _get(t, "insider_name", ""),
                    "code": _get(t, "transaction_code", ""),
                    "shares": _get(t, "shares", 0),
                    "price": _get(t, "price_per_share", 0),
                    "date": _get(t, "transaction_date", ""),
                }
                for t in txns[:10]
            ]

            return InsiderData(
                buy_count_30d=buy_count,
                sell_count_30d=sell_count,
                net_shares_30d=net_shares,
                cluster_score=cluster_score,
                cluster_buyers=cluster_buyers,
                signal=signal,
                transactions=transactions,
                quality="ok",
            )
        except Exception as e:
            logger.warning(f"Failed to collect insider for {symbol}: {e}")
            return InsiderData(quality="missing")

    def _collect_news(self, symbol: str) -> NewsData:
        """
        委托 FinnhubProvider.get_company_news(symbol, days=3)
        """
        try:
            from app.utils.timezone import now_utc
            news_list = self.finnhub.get_company_news(symbol, days=3)
            if not news_list:
                return NewsData(quality="missing")

            headlines = [n.get("headline", "") for n in news_list[:5]]
            
            # 简单情绪分析：正面/负面关键词计数
            positive_keywords = ["surge", "beat", "growth", "positive", "upgrade", "strong"]
            negative_keywords = ["drop", "miss", "decline", "negative", "downgrade", "weak"]
            
            pos_count = 0
            neg_count = 0
            for headline in headlines:
                h_lower = headline.lower()
                pos_count += sum(1 for kw in positive_keywords if kw in h_lower)
                neg_count += sum(1 for kw in negative_keywords if kw in h_lower)
            
            total = pos_count + neg_count
            sentiment_score = (pos_count - neg_count) / total if total > 0 else 0.0

            return NewsData(
                headlines=headlines,
                sentiment_score=sentiment_score,
                source="finnhub",
                fetched_at=now_utc().isoformat(),
                quality=self._check_news_freshness(news_list),
            )
        except Exception as e:
            logger.warning(f"Failed to collect news for {symbol}: {e}")
            return NewsData(quality="missing")

    def _collect_earnings(self, symbol: str) -> EarningsData:
        """
        Finnhub 主数据源 + AlphaVantage fallback

        根因修复 (2026-06-22):
        1. AlphaVantage API 25次/天额度耗尽 → get_earnings_history 返回空 → earnings 永久 missing
        2. AlphaVantage 返回 surprise="0.45" (str), data_collector 做 > 0 比较 TypeError
        修复: Finnhub get_company_earnings (24h缓存, 无额度限制) 作为主源,
              surprise 做 float() 安全转换
        """
        try:
            bare = symbol.replace("US.", "").upper()

            # 源 1: Finnhub earnings calendar (未来财报日期)
            next_date = None
            days_until = None
            quality = "missing"

            try:
                # 用 Finnhub calendar/earnings 查该标的未来财报
                # TZ-2 (2026-08-30): 锚 ET 日历日 — 原服务器 CST date.today() 在 ET 午后
                #   领先一天, days_until 整体偏移 → screener "财报前3天"排除门边界漂移。
                from app.utils.timezone import now_et as _now_et
                import datetime as _dt
                today = _now_et().date()
                # [RFC-171] 先查市场级日历 (1 call/日, 6h 缓存); 取不到再回退逐标查询。
                #   原实现每标的 1 次 → 69 只候选 69 calls, 与 news/其它维度一起
                #   打穿 Finnhub 免费档 60/min, 每命中一次 429 就盲睡 5s (实测单轮 62 次)。
                cal = None
                _idx = {}
                try:
                    _idx = self.finnhub.get_earnings_calendar_index(days_ahead=90)
                except Exception:
                    _idx = {}
                if isinstance(_idx, dict) and _idx:
                    cal = _idx.get(bare.upper()) or []
                else:
                    end = today + _dt.timedelta(days=90)
                    fh_data = self.finnhub._get("calendar/earnings", {
                        "symbol": bare,
                        "from": today.isoformat(),
                        "to": end.isoformat(),
                    })
                    if fh_data and isinstance(fh_data, dict):
                        cal = fh_data.get("earningsCalendar", [])
                if cal:
                    next_date = cal[0].get("date", "")
                    if next_date:
                        try:
                            next_dt = _dt.datetime.strptime(next_date, "%Y-%m-%d")
                            days_until = (next_dt.date() - today).days
                        except Exception:
                            pass
                    quality = "ok"
            except Exception as e:
                logger.debug(f"Finnhub earnings calendar failed for {symbol}: {e}")

            # 源 2: Finnhub stock/earnings (历史财报 + surprise)
            history = []
            beat_rate = 0.0
            try:
                history = self.finnhub.get_company_earnings(symbol, quarters=4)

                # 安全转换 surprise (可能为 str 或 None)
                beat_count = 0
                valid_count = 0
                for h in history:
                    actual = h.get("epsActual")
                    estimate = h.get("epsEstimate")
                    if actual is not None and estimate is not None:
                        try:
                            surprise = float(actual) - float(estimate)
                        except (TypeError, ValueError):
                            surprise = 0
                        if surprise > 0:
                            beat_count += 1
                            valid_count += 1
                        else:
                            valid_count += 1
                beat_rate = beat_count / valid_count if valid_count > 0 else 0.0

                if history and quality != "ok":
                    quality = "partial"
            except Exception as e:
                logger.debug(f"Finnhub earnings history failed for {symbol}: {e}")

            # 源 3: AlphaVantage fallback (仅当 Finnhub 全失败)
            if quality == "missing":
                try:
                    av_history = self.earnings.get_earnings_history(bare, limit=4)
                    if av_history:
                        history = av_history
                        beat_count = 0
                        valid_count = 0
                        for h in history:
                            surprise = h.get("surprise", 0)
                            try:
                                surprise = float(surprise) if surprise else 0
                            except (TypeError, ValueError):
                                surprise = 0
                            if surprise > 0:
                                beat_count += 1
                                valid_count += 1
                            elif surprise < 0:
                                valid_count += 1
                        beat_rate = beat_count / valid_count if valid_count > 0 else 0.0
                        quality = "partial"
                except Exception as e:
                    logger.debug(f"AlphaVantage earnings fallback failed for {symbol}: {e}")

            return EarningsData(
                next_date=next_date,
                days_until=days_until,
                history=history,
                beat_rate=beat_rate,
                sector_atr_multiplier=1.0,
                quality=quality,
            )
        except Exception as e:
            logger.warning(f"Failed to collect earnings for {symbol}: {e}")
            return EarningsData(quality="missing")

    def _collect_sentiment(self, symbol: str) -> SentimentData:
        """
        三源融合情绪：Finnhub新闻情绪 + Futu分析师评级 + VIX

        2026-06-21: 新增 Futu 分析师评级共识 (get_research_analyst_consensus)
        """
        try:
            # 1. Finnhub 新闻情绪 [-1, 1]
            news_sentiment = 0.0
            articles_count = 0
            sentiment_quality = "missing"
            try:
                news_sentiment = self.finnhub.get_sentiment_score(symbol)
                if news_sentiment != 0.0:
                    articles_count = 1
                    sentiment_quality = "ok"
                else:
                    sentiment_quality = "partial"
            except Exception as e:
                logger.debug(f"Finnhub sentiment failed for {symbol}: {e}")

            # 2. Futu 分析师评级共识 (如果有 futu_ext)
            analyst_rating = None
            analyst_target_avg = None
            analyst_buy_pct = None
            try:
                if self.futu_ext:
                    ac = self.futu_ext.get_analyst_consensus(symbol)
                    if ac.get("status") == "ok":
                        d = ac.get("data", {})
                        analyst_rating = d.get("rating")  # 1-5
                        analyst_target_avg = d.get("target_avg")
                        analyst_buy_pct = d.get("buy_pct")
                        if analyst_rating is not None:
                            # 分析师评级转情绪分数: rating 4-5 → 正面, 3 → 中性, 1-2 → 负面
                            analyst_sentiment = (analyst_rating - 3) / 2.0  # 1→-1, 3→0, 5→1
                            # 如果新闻情绪为空，用分析师评级替代
                            if sentiment_quality == "partial" or sentiment_quality == "missing":
                                news_sentiment = analyst_sentiment
                                articles_count = d.get("total_analysts", 0)
                                sentiment_quality = "ok"
                            else:
                                # 两者都有，取加权平均
                                news_sentiment = (news_sentiment * 0.4 + analyst_sentiment * 0.6)
            except Exception as e:
                logger.debug(f"Futu analyst consensus failed for {symbol}: {e}")

            # 3. VIX (2026-08-22 P1-2: 全市场指标走缓存, 不逐标的实例化)
            vix = self._get_vix_cached()
            # 2026-09-14 (P2): 原为 `vix != 20.0` —— 用"值等于哨兵"猜缺失, 真值恰为
            #   20.0 时反被判缺失 (且 20.0 正好卡在 overlay 档位边界)。现 None 即缺失。
            vix_quality = "ok" if vix is not None else "missing"

            # 综合质量
            if sentiment_quality == "ok" and vix_quality == "ok":
                quality = "ok"
            elif sentiment_quality in ("ok", "partial") or vix_quality == "ok":
                quality = "partial"
            else:
                quality = "missing"

            return SentimentData(
                news_sentiment=news_sentiment,
                articles_count=articles_count,
                vix=vix,
                quality=quality,
            )
        except Exception as e:
            logger.warning(f"Failed to collect sentiment for {symbol}: {e}")
            return SentimentData(quality="missing")
    
    def _check_consistency(self, data: PlaybookData) -> List[str]:
        """
        交叉一致性校验：检查不同维度数据之间的逻辑一致性
        
        Returns:
            警告信息列表 (空列表表示无问题)
        
        检查项:
        1. 新闻情绪 vs 技术面趋势矛盾
        2. 内部人信号 vs 技术面信号矛盾
        3. 基本面估值 vs 技术面位置矛盾
        4. 数据时间戳不一致 (fetched_at 差异过大)
        """
        warnings = []
        
        # 1. 新闻情绪 vs 技术面趋势
        news_sentiment = data.get_sentiment_score()
        technical_trend = data.technical.trend_5d
        
        if news_sentiment is not None and technical_trend:
            # 强烈矛盾：新闻极度正面但技术面下跌
            if news_sentiment > 0.5 and technical_trend == "down":
                warnings.append(
                    f"矛盾: 新闻情绪正面({news_sentiment:.2f}) 但技术面下跌({technical_trend})"
                )
            # 强烈矛盾：新闻极度负面但技术面上涨
            elif news_sentiment < -0.5 and technical_trend == "up":
                warnings.append(
                    f"矛盾: 新闻情绪负面({news_sentiment:.2f}) 但技术面上涨({technical_trend})"
                )
        
        # 2. 内部人信号 vs 技术面信号
        insider_signal = data.insider.signal
        rsi = data.get_rsi()
        
        if insider_signal and rsi:
            # 内部人大量买入但 RSI 超买 (可能是滞后信号)
            if insider_signal == "strong_buy" and rsi > 70:
                warnings.append(
                    f"潜在滞后: 内部人强烈买入但 RSI={rsi:.1f}(超买)，可能是滞后信号"
                )
            # 内部人大量卖出但 RSI 超卖
            elif insider_signal == "strong_sell" and rsi < 30:
                warnings.append(
                    f"潜在滞后: 内部人强烈卖出但 RSI={rsi:.1f}(超卖)，可能是滞后信号"
                )
        
        # 3. 基本面估值 vs 技术面位置
        pe_ratio = data.fundamental.pe
        price = data.get_price()
        ma50 = data.technical.ma50
        
        if pe_ratio and price and ma50:
            # 高估值 (PE > 50) 但价格远低于 MA50 (可能是价值陷阱)
            if pe_ratio > 50 and price < ma50 * 0.9:
                warnings.append(
                    f"价值陷阱风险: PE={pe_ratio:.1f}(高估值) 但价格低于MA50 10%+"
                )
        
        # 4. 财报临近但技术面波动异常
        days_until_earnings = data.get_days_until_earnings()
        atr = data.technical.atr_14
        
        if days_until_earnings is not None and atr and price:
            # 财报 < 7天但 ATR 异常高 (>5% 价格)
            if days_until_earnings < 7 and atr > price * 0.05:
                warnings.append(
                    f"财报前波动: 距财报{days_until_earnings}天，ATR={atr:.2f}(>{price*0.05:.2f})"
                )
        
        # 5. 数据时间戳一致性检查
        timestamps = []
        if data.fundamental.fetched_at:
            try:
                timestamps.append(datetime.fromisoformat(data.fundamental.fetched_at.replace('Z', '+00:00')))
            except Exception:
                pass
        if data.news.fetched_at:
            try:
                timestamps.append(datetime.fromisoformat(data.news.fetched_at.replace('Z', '+00:00')))
            except Exception:
                pass
        
        if len(timestamps) >= 2:
            max_diff = max(timestamps) - min(timestamps)
            # 不同维度数据时间差 > 24小时
            if max_diff.total_seconds() > 86400:
                warnings.append(
                    f"数据时间不一致: 各维度数据时间差 {max_diff.total_seconds()/3600:.1f}小时"
                )
        
        return warnings
