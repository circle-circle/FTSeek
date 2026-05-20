"""
Scalping 标的筛选器 (Scalping Stock Selector)

核心理念: Scalping 快进快出, 需要高流动性 + 高日内振幅的标的。
财报禁建期的标的排除, 避免在关键时间窗口内持仓。

四步筛选 (2026-05-04 升级):
  Step 1 — 流动性门槛: 日均量 >= min_daily_volume
  Step 2 — 日内振幅门槛: (high-low)/prev_close >= min_amplitude
  Step 3 — 财报禁建排除: 排除财报前 24h 禁建期标的
  Step 4 — 动态池扩展: 盘前动量 + 新闻关联股扩展 (新增)

作者: FTSeek Team
日期: 2026-05-04 (v2.0 — 动态股池扩展)
"""

import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger("ftseek.scalping.selector")


# 默认配置
DEFAULT_MIN_DAILY_VOLUME = 500_000     # 50万股 (2026-05-04 从 100万 降低)
DEFAULT_MIN_AMPLITUDE = 0.015          # 1.5% 日内振幅 (2026-05-04 从 2% 降低)
DEFAULT_MAX_TARGETS = 25               # 最大标的总数 (2026-05-04 从 15 增加)


class ScalpingStockSelector:
    """
    Scalping 标的筛选器

    设计原则:
      - 不依赖 LLM, 纯数据驱动
      - 优先用 Futu 批量快照, 降级到 HTTP API
      - 财报守卫必须已更新财报日历后再调用
    """

    def __init__(
        self,
        data_router=None,
        earnings_guard=None,
        config: Optional[Dict] = None,
        watchlist_codes: Optional[List[str]] = None,
    ):
        """
        Args:
            data_router: MarketDataRouter 实例 (批量报价)
            earnings_guard: EarningsRiskGuard 实例 (财报黑名单)
            config: 选股配置 dict (min_daily_volume, min_amplitude, max_targets, exclude_earnings_blackout)
            watchlist_codes: 候选股票代码列表 (None = 从 watchlist_manager 自动加载)
        """
        self.data_router = data_router
        self.earnings_guard = earnings_guard
        cfg = config or {}
        self.config = config  # P0 保存完整配置供动态扩展使用
        self.min_daily_volume = cfg.get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME)
        self.min_amplitude = cfg.get("min_amplitude", DEFAULT_MIN_AMPLITUDE)
        self.max_targets = cfg.get("max_targets", DEFAULT_MAX_TARGETS)
        self.exclude_earnings_blackout = cfg.get("exclude_earnings_blackout", True)
        self._watchlist_codes = watchlist_codes

    # ── 公开 API ──────────────────────────────────────────────────

    def populate_pool(self, daily_active_pool) -> int:
        """
        执行完整选股流程并将结果注入 SOURCE_SCALPING 池。
        P0 升级 (2026-05-04): 增加 Step 4 动态股池扩展 (盘前动量 + 新闻关联)

        Args:
            daily_active_pool: DailyActivePool 实例

        Returns:
            成功注入的标的数量
        """
        # Step 1-3: 基础筛选
        targets = self.select_targets()
        if not targets:
            logger.warning("[ScalpingSelector] 筛选结果为空 — 无标的满足条件, 将走降级路径")
            return 0

        count = 0
        for code in targets:
            try:
                daily_active_pool.add_scalping_target(code, metadata={
                    "selector": "scalping_stock_selector",
                    "min_amplitude": self.min_amplitude,
                    "min_volume": self.min_daily_volume,
                })
                count += 1
            except Exception as e:
                logger.error(f"[ScalpingSelector] 注入 {code} 失败: {e}")

        # Step 4: 动态股池扩展 (P0 新增)
        expansion_count = self._expand_dynamic_pool(daily_active_pool, set(targets))
        count += expansion_count

        logger.info(
            f"[ScalpingSelector] 筛选完成: {count} 只入选 SOURCE_SCALPING "
            f"(基础={len(targets)} + 扩展={expansion_count}) "
            f"| 振幅>={self.min_amplitude:.0%} | 量>={self.min_daily_volume:,} | "
            f"财报排除={self.exclude_earnings_blackout}"
        )
        return count

    def _expand_dynamic_pool(self, daily_active_pool, existing_targets: Set[str]) -> int:
        """
        Step 4: 动态股池扩展 — 通过盘前行情和新闻关联扩展标的池。

        扩展策略:
          1. 盘前动量扩展: 盘前涨幅/跌幅最大的标的 (高波动机会)
          2. 新闻关联扩展: 从新闻中提取相关股票代码 (事件驱动机会)

        Args:
            daily_active_pool: DailyActivePool 实例
            existing_targets: 已入选的标的集合 (避免重复)

        Returns:
            成功扩展的标的数量
        """
        count = 0
        expansion_cfg = self.config.get("dynamic_pool_expansion", {})
        if not expansion_cfg.get("enabled", False):
            return 0

        max_expansion = expansion_cfg.get("max_expansion_targets", 10)

        # 1. 盘前动量扩展
        if expansion_cfg.get("premarket_momentum", False):
            premarket_count = self._expand_by_premarket_momentum(
                daily_active_pool, existing_targets, max_expansion - count
            )
            count += premarket_count
            logger.info(f"[ScalpingSelector] 盘前动量扩展: +{premarket_count} 只")

        # 2. 新闻关联扩展
        if expansion_cfg.get("news_related_expansion", False) and count < max_expansion:
            news_count = self._expand_by_news_related(
                daily_active_pool, existing_targets, max_expansion - count
            )
            count += news_count
            logger.info(f"[ScalpingSelector] 新闻关联扩展: +{news_count} 只")

        return count

    def _expand_by_premarket_momentum(
        self, daily_active_pool, existing_targets: Set[str], max_count: int
    ) -> int:
        """
        盘前动量扩展: 找出盘前涨幅/跌幅最大的标的。

        逻辑:
          - 获取所有 watchlist 标的的盘前价格变动
          - 筛选变动幅度 > 1% 的标的
          - 排除已入选的标的
          - 取前 N 只
        """
        if not self.data_router:
            return 0

        try:
            # 获取候选池
            candidates = self._get_candidate_pool()
            if not candidates:
                return 0

            # 获取盘前数据
            premarket_movers = []
            for sym in candidates:
                if sym in existing_targets:
                    continue
                try:
                    quote = self.data_router.get_realtime_quote(sym)
                    if quote:
                        price = quote.get("last_price", 0)
                        prev_close = quote.get("prev_close", 0)
                        if price > 0 and prev_close > 0:
                            change_pct = (price - prev_close) / prev_close
                            if abs(change_pct) >= 0.01:  # 1% 变动
                                premarket_movers.append((sym, abs(change_pct), change_pct))
                except Exception:
                    pass

            # 按变动幅度排序，取前 N
            premarket_movers.sort(key=lambda x: x[1], reverse=True)
            added = 0
            for sym, abs_change, direction in premarket_movers[:max_count]:
                try:
                    daily_active_pool.add_scalping_target(sym, metadata={
                        "selector": "premarket_momentum",
                        "change_pct": direction,
                        "abs_change": abs_change,
                    })
                    added += 1
                    logger.info(
                        f"[ScalpingSelector] 盘前动量扩展: {sym} "
                        f"({'+' if direction > 0 else ''}{direction:.1%})"
                    )
                except Exception as e:
                    logger.debug(f"[ScalpingSelector] 盘前扩展 {sym} 失败: {e}")

            return added

        except Exception as e:
            logger.debug(f"[ScalpingSelector] 盘前动量扩展失败: {e}")
            return 0

    def _expand_by_news_related(
        self, daily_active_pool, existing_targets: Set[str], max_count: int
    ) -> int:
        """
        新闻关联扩展: 从最近新闻中提取相关股票代码。

        逻辑:
          - 读取最近 30 分钟的新闻
          - 从新闻标题/内容中提取股票代码 (正则匹配 US.XXX)
          - 排除已入选的标的和 ETF
          - 验证标的流动性 (有行情数据)
          - 取前 N 只
        """
        try:
            # 从新闻数据库获取最近新闻
            from app.core.paths import get_news_us_db
            news_db_path = str(get_news_us_db())
            import sqlite3
            import re
            from datetime import datetime, timedelta

            if not self.data_router:
                return 0

            conn = sqlite3.connect(news_db_path)
            c = conn.cursor()

            # 获取最近 30 分钟的新闻
            cutoff = (datetime.now() - timedelta(minutes=30)).isoformat()
            c.execute("""
                SELECT title, content FROM news 
                WHERE created_at > ? 
                ORDER BY created_at DESC 
                LIMIT 50
            """, (cutoff,))

            news_items = c.fetchall()
            conn.close()

            if not news_items:
                return 0

            # 提取股票代码
            extracted_symbols = set()
            symbol_pattern = re.compile(r'US\.([A-Z]{1,5})\b')
            for title, content in news_items:
                text = (title or "") + " " + (content or "")
                matches = symbol_pattern.findall(text)
                for match in matches:
                    sym = f"US.{match}"
                    if sym not in existing_targets and sym not in extracted_symbols:
                        extracted_symbols.add(sym)

            # 验证流动性并添加
            added = 0
            etf_patterns = ("US.SPY", "US.QQQ", "US.DIA", "US.IWM", "US.UVXY")
            for sym in extracted_symbols:
                if added >= max_count:
                    break
                if any(sym.startswith(p) for p in etf_patterns):
                    continue

                try:
                    # 验证是否有行情数据
                    quote = self.data_router.get_realtime_quote(sym)
                    if quote and quote.get("last_price", 0) > 0:
                        daily_active_pool.add_scalping_target(sym, metadata={
                            "selector": "news_related",
                            "news_count": len([s for s in extracted_symbols if s == sym]),
                        })
                        added += 1
                        logger.info(f"[ScalpingSelector] 新闻关联扩展: {sym}")
                except Exception:
                    pass

            return added

        except Exception as e:
            logger.debug(f"[ScalpingSelector] 新闻关联扩展失败: {e}")
            return 0

    def select_targets(self) -> List[str]:
        """
        执行三步筛选, 返回合格标的列表 (按振幅降序)。

        Returns:
            排序后的股票代码列表 (最多 max_targets 只)
        """
        candidates = self._get_candidate_pool()
        if not candidates:
            logger.warning("[ScalpingSelector] 候选池为空")
            return []

        logger.info(f"[ScalpingSelector] 候选池: {len(candidates)} 只, 开始筛选...")

        # 批量拉取行情数据
        quotes = self._fetch_batch_data(candidates)

        # Step 1: 流动性过滤
        liquid = self._filter_by_liquidity(candidates, quotes)
        logger.info(f"[ScalpingSelector] Step1 流动性: {len(liquid)}/{len(candidates)} 通过 (量>={self.min_daily_volume:,})")

        # Step 2: 振幅过滤
        high_amp, amp_scores = self._filter_by_amplitude(liquid, quotes)
        logger.info(f"[ScalpingSelector] Step2 振幅: {len(high_amp)}/{len(liquid)} 通过 (振幅>={self.min_amplitude:.0%})")

        # Step 3: 财报排除
        if self.exclude_earnings_blackout and self.earnings_guard:
            clean = self._filter_by_earnings(high_amp)
            logger.info(f"[ScalpingSelector] Step3 财报: {len(clean)}/{len(high_amp)} 通过 (排除禁建)")
        else:
            clean = high_amp

        # 按振幅降序排序, 取前 N
        sorted_targets = sorted(clean, key=lambda s: amp_scores.get(s, 0), reverse=True)
        result = sorted_targets[:self.max_targets]

        if result:
            logger.info(
                f"[ScalpingSelector] 最终入选: {len(result)} 只 | "
                f"Top5: {[s.replace('US.', '') for s in result[:5]]}"
            )

        # 筛掉名单
        rejected_liquid = set(candidates) - set(liquid)
        rejected_amp = set(liquid) - set(high_amp)
        rejected_earnings = set(high_amp) - set(clean)
        if rejected_earnings:
            logger.info(f"[ScalpingSelector] 财报排除: {[s.replace('US.', '') for s in rejected_earnings]}")

        return result

    # ── 候选池 ────────────────────────────────────────────────────

    def _get_candidate_pool(self) -> List[str]:
        """
        构建候选池:
          watchlist_us.json 全部已启用股票 (不含 ETF)
        """
        codes = self._watchlist_codes
        if codes is None:
            try:
                from app.utils.watchlist_manager import get_stock_codes
                codes = get_stock_codes()
            except Exception as e:
                logger.warning(f"[ScalpingSelector] 加载 watchlist 失败: {e}")
                codes = []

        if not codes:
            return []

        # 排除 ETF (SPY/QQQ/DIA/IWM/UVXY 不适合 scalping)
        etf_patterns = ("US.SPY", "US.QQQ", "US.DIA", "US.IWM", "US.UVXY")
        result = [c for c in codes if not any(c.startswith(p) for p in etf_patterns)]
        return result

    # ── 数据获取 ──────────────────────────────────────────────────

    def _fetch_batch_data(self, candidates: List[str]) -> Dict[str, Dict]:
        """
        批量获取行情数据。

        优先: data_router.get_batch_quotes() → Futu LV3 快照
        降级: data_router.get_realtime_price() 逐个查询
        """
        if not self.data_router:
            return {}

        try:
            if hasattr(self.data_router, 'get_batch_quotes'):
                return self.data_router.get_batch_quotes(candidates)
        except Exception as e:
            logger.debug(f"[ScalpingSelector] 批量报价失败, 降至逐股: {e}")

        # 降级: 逐个股获取
        results = {}
        for sym in candidates:
            try:
                # P0 修复 (2026-05-05): get_realtime_price 不存在, 用 get_quote 降级
                quote = self.data_router.get_quote(sym) if hasattr(self.data_router, 'get_quote') else None
                if quote and quote.get("price", 0) > 0:
                    results[sym] = {"price": quote["price"], "volume": quote.get("volume", 0)}
            except Exception:
                pass
        return results

    # ── Step 1: 流动性 ─────────────────────────────────────────────

    def _filter_by_liquidity(self, candidates: List[str], quotes: Dict[str, Dict]) -> List[str]:
        """
        过滤低流动性标的。

        条件: quote["volume"] >= min_daily_volume (当日实时量已达标)
              若 volume 不可用, 不淘汰 (保守放行)
        """
        passed = []
        for sym in candidates:
            q = quotes.get(sym, {})
            volume = q.get("volume", 0)
            # volume=0 可能是数据不可用, 保守放行, 让振幅和财报过滤兜底
            if volume <= 0 or volume >= self.min_daily_volume:
                passed.append(sym)
        return passed

    # ── Step 2: 日内振幅 ──────────────────────────────────────────

    def _filter_by_amplitude(
        self, candidates: List[str], quotes: Dict[str, Dict]
    ) -> tuple:
        """
        过滤低振幅标的。

        条件: (high - low) / prev_close >= min_amplitude
              所有值必须 > 0

        Returns:
            (passed_codes, {code: amplitude_score})
        """
        passed = []
        scores = {}
        for sym in candidates:
            q = quotes.get(sym, {})
            high = q.get("high", 0)
            low = q.get("low", 0)
            prev_close = q.get("prev_close", 0)

            if high <= 0 or low <= 0 or prev_close <= 0 or high <= low:
                # 数据不完整, 保守放行 (可能盘前阶段, Futu 未连接)
                passed.append(sym)
                scores[sym] = 0.0
                continue

            amplitude = (high - low) / prev_close
            scores[sym] = amplitude
            if amplitude >= self.min_amplitude:
                passed.append(sym)

        return passed, scores

    # ── Step 3: 财报禁建排除 ───────────────────────────────────────

    def _filter_by_earnings(self, candidates: List[str]) -> List[str]:
        """
        排除财报禁建期标的。

        条件: earnings_guard.is_earnings_blackout(symbol) == False

        财报日前 24h → 禁建 (Scalping 快进快出, 不能卡在财报窗口)
        财报日前后 3 天 → 标记但不排除 (波动增大反而是 Scalping 机会)
        """
        if not self.earnings_guard:
            return candidates

        passed = []
        for sym in candidates:
            try:
                is_blackout, reason = self.earnings_guard.is_earnings_blackout(sym)
                if is_blackout:
                    logger.info(f"[ScalpingSelector] 🚫 {sym} 财报禁建: {reason}")
                    continue
            except Exception as e:
                logger.debug(f"[ScalpingSelector] 财报检查异常 {sym} (放行): {e}")
            passed.append(sym)
        return passed


# ── 工厂函数 ──────────────────────────────────────────────────────

def create_stock_selector(
    data_router=None,
    earnings_guard=None,
    scalping_config: Optional[Dict] = None,
    watchlist_codes: Optional[List[str]] = None,
) -> ScalpingStockSelector:
    """
    创建 ScalpingStockSelector 实例。

    Args:
        data_router: MarketDataRouter
        earnings_guard: EarningsRiskGuard
        scalping_config: scalping 完整配置 dict
        watchlist_codes: 候选代码列表

    Returns:
        ScalpingStockSelector 实例 (数据源不可用时返回降级实例)
    """
    # P0 升级 (2026-05-04): 合并 stock_selector 和 dynamic_pool_expansion 配置
    # 注意: unified_config.yaml 中使用 stock_selector 而不是 stock_selection
    selection_cfg = {}
    if scalping_config:
        # 基础选股配置 (兼容 stock_selection 和 stock_selector 两种键名)
        selection_cfg = scalping_config.get("stock_selection", scalping_config.get("stock_selector", {})).copy()
        # 动态扩展配置
        expansion_cfg = scalping_config.get("dynamic_pool_expansion", {})
        if expansion_cfg:
            selection_cfg["dynamic_pool_expansion"] = expansion_cfg

    selector = ScalpingStockSelector(
        data_router=data_router,
        earnings_guard=earnings_guard,
        config=selection_cfg,
        watchlist_codes=watchlist_codes,
    )
    logger.info(
        f"[ScalpingSelector] 已创建 | 振幅>={selector.min_amplitude:.0%} | "
        f"量>={selector.min_daily_volume:,} | 最大{selector.max_targets}只 | "
        f"财报排除={selector.exclude_earnings_blackout} | "
        f"动态扩展={'✅' if selection_cfg.get('dynamic_pool_expansion', {}).get('enabled') else '❌'}"
    )
    return selector
