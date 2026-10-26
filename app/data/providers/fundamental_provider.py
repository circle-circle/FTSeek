"""
基本面数据 Provider

聚合多个数据源，按优先级降级：
  1. Finnhub (主) - 财务指标 + 分析师评级 + 新闻 + 财报日历
  2. Yahoo Finance (备) - 补充财务数据
  3. Alpha Vantage (备) - 最终降级

对外接口统一，下游不关心具体数据源。
"""
import os
import logging
import requests
import urllib3
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

from app.data.providers.base import BaseDataProvider
from app.utils.symbol_utils import to_dotted_class_share
from app.utils.timezone import now_utc

# 抑制 TLS 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 加载环境变量
load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env.us")

logger = logging.getLogger(__name__)


class FundamentalProvider(BaseDataProvider):
    """基本面数据 Provider（多源聚合）"""

    def __init__(self):
        super().__init__(name="fundamental", cache_ttl_seconds=3600)
        self.api_key = os.getenv("FINNHUB_API_KEY", "")
        self.base_url = "https://finnhub.io/api/v1"

    def _finnhub_get(self, endpoint: str, params: Dict, timeout: int = 15) -> Optional[Any]:
        """低层 Finnhub GET"""
        if not self.api_key or self.api_key == "***":
            return None
        headers = {"X-Finnhub-Token": self.api_key}
        try:
            # C3 (2026-08-24, 体检 E2): verify=True 收口 (实测握手正常)
            resp = requests.get(
                f"{self.base_url}/{endpoint}",
                params=params, headers=headers, timeout=timeout, verify=True,
            )
            if resp.status_code == 429:
                logger.warning(f"[FundamentalProvider] Finnhub 429 on {endpoint}")
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug(f"[FundamentalProvider] Finnhub {endpoint} error: {e}")
            return None

    # 2026-08-22 (P1-3): 基本面数据 1h 缓存 — 降级链每次全量跑是浪费
    _fundamental_cache = {}
    _FUNDAMENTAL_CACHE_TTL = 43200  # [RFC-171] 1h→12h: 基本面(PE/市值/行业/ROE)季度级变化,
    # 盘前每个候选每天都重拉一次是纯浪费 (69 只 × 1 call 打 Finnhub 60/min)。

    def get_data(self, symbol: str, **kwargs) -> Dict[str, Any]:
        """获取基本面数据（自动降级: Futu → Finnhub → Yahoo → AlphaVantage）"""
        # 2026-08-22 (P1-3): 先查缓存
        import time as _time
        now_ts = _time.time()
        if symbol in self._fundamental_cache:
            cached_result, cached_ts = self._fundamental_cache[symbol]
            if now_ts - cached_ts < self._FUNDAMENTAL_CACHE_TTL:
                return cached_result

        now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

        # 尝试数据源 0: Futu (主数据源 — 真实数据，无需API key)
        result = self._fetch_futu(symbol)
        if result and result.get("status") == "ok":
            self._fundamental_cache[symbol] = (result, now_ts)  # P1-3 缓存写入
            return result

        # 降级 1: Finnhub
        logger.debug(f"[FundamentalProvider] Futu failed, trying Finnhub: {symbol}")
        result = self._fetch_finnhub(symbol)
        if result and result.get("status") == "ok":
            self._fundamental_cache[symbol] = (result, now_ts)  # P1-3 缓存写入
            return result

        # 降级 2: Yahoo Finance
        logger.info(f"[FundamentalProvider] Finnhub failed, trying Yahoo: {symbol}")
        result = self._fetch_yahoo(symbol)
        if result and result.get("status") == "ok":
            self._fundamental_cache[symbol] = (result, now_ts)  # P1-3 缓存写入
            return result

        # 降级 3: Alpha Vantage
        logger.info(f"[FundamentalProvider] Yahoo failed, trying AlphaVantage: {symbol}")
        result = self._fetch_alphavantage(symbol)
        if result and result.get("status") == "ok":
            self._fundamental_cache[symbol] = (result, now_ts)  # P1-3 缓存写入
            return result

        # 全部失败
        return {
            "status": "failed",
            "error": "All data sources failed",
            "data_source": "none",
            "fetched_at": now_iso,
        }

    def _fetch_futu(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 Futu get_market_snapshot 获取基本面（主数据源 — 真实数据）"""
        now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            # P0 事故修复 (2026-07-24 夜): 改用单例 + 不 disconnect — 原 "每次 new + connect + disconnect"
            #   在实盘模式下每次都触发一次实盘解锁 (connect 内无条件 unlock_trade)。
            #   topup 盘中窗每 ~2min 扫 11 只 → 解锁 spam 撞富途限频 (30s/10次) → ERROR 循环。
            #   本方法只需 quote_ctx 行情快照, 单例 _connected 短路后零额外解锁。
            #   同款反模式共 9 处 (thesis_challenge/plan/*), 本处为热路径先修, 余者周末收敛。
            from app.web_dashboard.manual_order_service import get_manual_order_service
            svc = get_manual_order_service()
            if not svc.connect():
                logger.warning("[FundamentalProvider] Futu connect failed")
                return None

            from futu import RET_OK
            # 2026-09-18 类股修正: Futu OpenD 只认点号写法 — 传 US.BRK-B 返回
            #   ret=-1 "未知股票 BRK-B" (实测), 改传 US.BRK.B 后 ret=0。
            _market, _, _base = str(symbol).partition(".")
            _snap_symbol = f"{_market}.{to_dotted_class_share(_base)}" if _base else symbol
            ret, data = svc._quote_ctx.get_market_snapshot([_snap_symbol])
            # 注意: 单例连接常驻, 绝不在此 disconnect (会断掉 dashboard 手动交易的共享连接)

            if ret != RET_OK or data.empty:
                logger.warning(f"[FundamentalProvider] Futu snapshot failed: {data}")
                return None

            row = data.iloc[0].to_dict()
            eps = row.get("earning_per_share", 0)
            net_asset = row.get("net_asset", 0)
            net_profit = row.get("net_profit", 0)
            pe_ttm = row.get("pe_ttm_ratio", 0)
            pe_ratio = row.get("pe_ratio", 0)
            pb_ratio = row.get("pb_ratio", 0)
            market_cap = row.get("total_market_val", 0)
            dividend_ttm = row.get("dividend_ttm", 0)
            dividend_yield = row.get("dividend_ratio_ttm", 0)
            volume_ratio = row.get("volume_ratio", 1.0)

            # ROE = net_profit / net_asset (if both available)
            roe = round(net_profit / net_asset * 100, 2) if net_asset and net_asset > 0 else 0

            financials = {
                "source": "futu/market_snapshot",
                "fetched_at": now_iso,
                "market_cap": market_cap,
                "pe_ttm": pe_ttm,
                "pe_ratio": pe_ratio,
                "forward_pe": 0,  # Futu doesn't provide forward PE
                "pb_ratio": pb_ratio,
                "roe": roe,
                "eps_ttm": eps,
                "net_asset_per_share": row.get("net_asset_per_share", 0),
                "revenue_yoy": 0,  # Not available from snapshot
                "beta": 0,  # Not available from snapshot
                "dividend_yield": dividend_yield,
                "dividend_ttm": dividend_ttm,
                "52w_high": row.get("highest52weeks_price", 0),
                "52w_low": row.get("lowest52weeks_price", 0),
                "volume_ratio": volume_ratio,
                "bid_ask_ratio": row.get("bid_ask_ratio", 0),
                "turnover_rate": row.get("turnover_rate", 0),
                "pre_price": row.get("pre_price", 0),
                "pre_change_rate": row.get("pre_change_rate", 0),
                "after_price": row.get("after_price", 0),
                "after_change_rate": row.get("after_change_rate", 0),
            }

            # 2026-06-29: Futu snapshot 无 industry 字段, 从 Finnhub profile2 补充
            bare = symbol.replace("US.", "").replace("HK.", "").upper()
            profile = self._finnhub_get("stock/profile2", {"symbol": bare})
            if profile and isinstance(profile, dict):
                financials["industry"] = profile.get("finnhubIndustry") or ""

            # Earnings calendar: not available from Futu, mark as unknown
            next_catalyst = {
                "source": "futu/market_snapshot",
                "fetched_at": now_iso,
                "event": "Unknown",
                "date": "",
                "eps_estimate": None,
                "revenue_estimate": None,
            }

            return {
                "status": "ok",
                "data": {
                    "financials": financials,
                    "analyst": {"source": "none", "fetched_at": now_iso},
                    "recent_news": [],  # News fetched separately in deep_research
                    "next_catalyst": next_catalyst,
                    "_sources": {
                        "financials": "ok",
                        "analyst": "none",
                        "news": "none",
                        "catalyst": "ok",
                    },
                },
                "data_source": "futu",
                "fetched_at": now_iso,
            }

        except Exception as e:
            logger.warning(f"[FundamentalProvider] Futu error: {e}")
            return None

    def _fetch_finnhub(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 Finnhub 获取（主数据源）"""
        bare = symbol.replace("US.", "").replace("HK.", "").upper()
        now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # 并行获取 4 个维度（串行实现，可优化为 ThreadPoolExecutor）
        metric = self._finnhub_get("stock/metric", {"symbol": bare, "metric": "all"})
        recommendations = self._finnhub_get("stock/recommendation", {"symbol": bare})
        profile = self._finnhub_get("stock/profile2", {"symbol": bare})
        
        end_d = now_utc().strftime("%Y-%m-%d")
        start_d = (now_utc() - timedelta(days=7)).strftime("%Y-%m-%d")
        news = self._finnhub_get("company-news", {
            "symbol": bare, "from": start_d, "to": end_d,
        })
        
        today = now_utc().date()
        end = today + timedelta(days=90)
        earnings = self._finnhub_get("calendar/earnings", {
            "symbol": bare,
            "from": today.strftime("%Y-%m-%d"),
            "to": end.strftime("%Y-%m-%d"),
        })

        # 解析财务指标
        financials = {"source": "finnhub/stock/metric", "fetched_at": now_iso}
        if metric and "metric" in metric:
            m = metric["metric"]
            financials.update({
                "market_cap": m.get("marketCapitalization", 0),
                "pe_ttm": m.get("peNormalizedAnnual", 0),
                "forward_pe": m.get("peExclExtraAnnual", 0),
                "pb": m.get("pbAnnual", 0),
                "roe": m.get("roeTTM", 0),
                "eps_ttm": m.get("epsNormalizedAnnual", 0),
                "revenue_yoy": m.get("revenueGrowth5Y", 0),
                "gross_margin": m.get("grossMarginTTM", 0),
                "net_margin": m.get("netProfitMarginTTM", 0),
                "beta": m.get("beta", 0),
                "52w_high": m.get("52WeekHigh", 0),
                "52w_low": m.get("52WeekLow", 0),
            })
        else:
            return None  # Finnhub 失败

        # 从 profile2 获取行业分类 (finnhubIndustry 字段)
        if profile and isinstance(profile, dict):
            financials["industry"] = profile.get("finnhubIndustry") or ""

        # 解析分析师评级
        analyst = {"source": "finnhub/recommendation_trends", "fetched_at": now_iso}
        if recommendations and len(recommendations) > 0:
            latest = recommendations[0]
            total = (latest.get("strongBuy", 0) + latest.get("buy", 0) 
                    + latest.get("hold", 0) + latest.get("sell", 0) 
                    + latest.get("strongSell", 0))
            if total > 0:
                buy_pct = (latest.get("strongBuy", 0) + latest.get("buy", 0)) / total * 100
                hold_pct = latest.get("hold", 0) / total * 100
                sell_pct = (latest.get("sell", 0) + latest.get("strongSell", 0)) / total * 100
                analyst.update({
                    "buy_pct": buy_pct,
                    "hold_pct": hold_pct,
                    "sell_pct": sell_pct,
                    "total_analysts": total,
                    "trend": "bullish" if buy_pct > 60 else "bearish" if sell_pct > 40 else "neutral",
                })

        # 解析新闻
        recent_news = []
        if news and isinstance(news, list):
            recent_news = [
                {
                    "headline": n.get("headline", ""),
                    "source": n.get("source", ""),
                    "datetime": n.get("datetime", 0),
                }
                for n in news[:5]
            ]

        # 解析财报日历
        next_catalyst = {"source": "finnhub/earnings_calendar", "fetched_at": now_iso}
        if earnings and "earningsCalendar" in earnings and len(earnings["earningsCalendar"]) > 0:
            next_earnings = earnings["earningsCalendar"][0]
            next_catalyst.update({
                "event": "Q Earnings",
                "date": next_earnings.get("date", ""),
            })

        # 统计成功/失败
        sources = {
            "financials": "ok" if metric else "failed",
            "analyst": "ok" if recommendations else "failed",
            "news": "ok" if news else "failed",
            "catalyst": "ok" if earnings else "failed",
        }

        return {
            "status": "ok",
            "data": {
                "financials": financials,
                "analyst": analyst,
                "recent_news": recent_news,
                "next_catalyst": next_catalyst,
                "_sources": sources,
            },
            "data_source": "finnhub",
            "fetched_at": now_iso,
        }

    def _fetch_yahoo(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 Yahoo Finance 获取（降级数据源）"""
        try:
            from app.data.yahoo_provider import YahooFinanceProvider
            yf = YahooFinanceProvider()
            bare = symbol.replace("US.", "").replace("HK.", "").upper()
            
            market_data = yf.get_market_data(bare)
            
            if market_data and market_data.get("fundamentals"):
                now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
                fund = market_data["fundamentals"]
                return {
                    "status": "ok",
                    "data": {
                        "financials": {
                            "market_cap": fund.get("market_cap", 0),
                            "pe_ttm": fund.get("pe_ratio", 0),
                            "eps_ttm": fund.get("eps", 0),
                            "source": "yahoo",
                            "fetched_at": now_iso,
                        },
                        "_sources": {"financials": "ok"},
                    },
                    "data_source": "yahoo",
                    "fetched_at": now_iso,
                }
        except Exception as e:
            logger.debug(f"[FundamentalProvider] Yahoo error: {e}")
        
        return None

    def _fetch_alphavantage(self, symbol: str) -> Optional[Dict[str, Any]]:
        """从 Alpha Vantage 获取（最终降级）"""
        try:
            from app.data.alphavantage_provider import AlphaVantageProvider
            av = AlphaVantageProvider()
            bare = symbol.replace("US.", "").replace("HK.", "").upper()
            
            # AlphaVantage 没有直接的基本面 API，跳过
            return None
        except Exception as e:
            logger.debug(f"[FundamentalProvider] AlphaVantage error: {e}")
        
        return None
