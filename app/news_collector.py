#!/usr/bin/env python3
"""
富途市场情绪数据 (Futu Market Sentiment)

2026-04-26 收敛说明：
----------------------------------------------------------------
本文件原名为 "新闻采集模块"，实际包含三部分：
  1. FutuMarketData         -- 富途资金流/快照情绪数据（仍在使用）
  2. RSSNewsCollector       -- 东财/WSJ/CNBC RSS（已由 app.news_push.collector 取代）
  3. Jin10NewsCollector     -- 金十快讯（已由 app.news_push.collector 取代）
  4. NewsItem dataclass     -- 与 app.news_push.collector.NewsItem 字段冗余

外部仅有 3 处调用方在使用 FutuMarketData：
  - app/strategy/consensus_engine.py:1190
  - scripts/rsshub_multi_source_cron.py:922
  - scripts/us_market_simulator.py:169

因此收敛策略：
  - 保留 FutuMarketData 类（其职责是情绪数据而非新闻采集）
  - 删除 RSS / Jin10 / Composite / NewsItem / fetch_news / __main__
  - 文件名暂保留以兼容既有 import；后续如需要可再重命名为 app/data/futu_market_sentiment.py
----------------------------------------------------------------
"""
import logging
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger("ftseek.news_collector")
logger.propagate = False


class FutuMarketData:
    """富途市场数据获取（资金流 + 市场快照 作为情绪指标） — 单例长连接"""

    _instance = None
    _quote_ctx = None

    def __new__(cls, host: str = '127.0.0.1', port: int = 11111):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.host = host
            cls._instance.port = port
        return cls._instance

    def __init__(self, host: str = '127.0.0.1', port: int = 11111):
        # __new__ 已赋值，避免重复设置
        pass

    def _get_ctx(self):
        """获取或重建复用连接"""
        if FutuMarketData._quote_ctx is None:
            try:
                from futu import OpenQuoteContext
                FutuMarketData._quote_ctx = OpenQuoteContext(
                    host=self.host, port=self.port
                )
                logger.debug("[FutuMarketData] 长连接已建立")
            except Exception as e:
                logger.warning(f"[FutuMarketData] 建立连接失败: {e}")
                raise
        return FutuMarketData._quote_ctx

    def _reset_ctx(self):
        """连接异常时销毁，下次调用重建"""
        try:
            if FutuMarketData._quote_ctx is not None:
                FutuMarketData._quote_ctx.close()
        except Exception:
            pass
        finally:
            FutuMarketData._quote_ctx = None
            logger.debug("[FutuMarketData] 连接已重置，下次调用重建")

    def get_capital_flow(self, code: str) -> Dict:
        """获取资金流向（作为情绪指标）— 复用长连接，失败时重建一次"""
        for attempt in range(2):
            try:
                ctx = self._get_ctx()
                ret, data = ctx.get_capital_flow(code)

                if ret == 0 and len(data) > 0:
                    latest = data.iloc[-1]
                    in_flow = float(latest.get('in_flow', 0))
                    main_in_flow = latest.get('main_in_flow', 0)
                    if main_in_flow == 'N/A' or main_in_flow is None:
                        main_in_flow = in_flow
                    else:
                        main_in_flow = float(main_in_flow)
                    if in_flow > 0:
                        sentiment = min(1.0, in_flow / 100000000)
                    else:
                        sentiment = max(-1.0, in_flow / 100000000)
                    return {
                        'code': code,
                        'in_flow': in_flow,
                        'main_inflow': main_in_flow,
                        'sentiment': round(sentiment, 3),
                        'timestamp': latest.get('capital_flow_item_time', datetime.now().isoformat()),
                    }
                return {'code': code, 'sentiment': 0, 'error': '无数据'}
            except Exception as e:
                logger.warning(f"获取资金流向失败 {code} (attempt {attempt+1}): {e}")
                self._reset_ctx()

        return {'code': code, 'sentiment': 0, 'error': '获取失败'}

    def get_market_snapshot(self, codes: List[str]) -> List[Dict]:
        """获取市场快照 — 复用长连接，失败时重建一次（2026-04-20: 接入全局限流器）"""
        from app.utils.futu_limiter import get_global_limiter
        limiter = get_global_limiter()

        for attempt in range(2):
            try:
                if not limiter.acquire(timeout=10):
                    logger.warning("[限流] 市场快照被拒绝")
                    return []

                ctx = self._get_ctx()
                ret, data = ctx.get_market_snapshot(codes)
                limiter.record()

                if ret == 0:
                    result = []
                    for _, row in data.iterrows():
                        result.append({
                            'code': row['code'],
                            'name': row.get('stock_name', ''),
                            'price': float(row.get('last_price', 0)),
                            'change_pct': float(row.get('change_rate', 0)),
                            'volume': int(row.get('volume', 0)),
                            'turnover': float(row.get('turnover', 0)),
                        })
                    return result
                return []
            except Exception as e:
                logger.warning(f"获取市场快照失败 (attempt {attempt+1}): {e}")
                self._reset_ctx()

        return []
