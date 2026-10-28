"""
基本面数据 Provider

聚合多个数据源，按优先级降级：
  1. Futu market_snapshot (主) - 财务指标 (pe/市值/52w/ROE/股息)
  2. Finnhub (备) - 财务指标 + 分析师评级 + 新闻 + 财报日历

2026-10-27: Yahoo / AlphaVantage 两腿已删除 —— 均为恒不产出分支
(Yahoo 的取值条件 market_data["fundamentals"] 全历史从未存在; AlphaVantage 是 return None 桩)。
真实降级链 = Futu → Finnhub, 两者皆失败时返回 status=failed (下游 → data_quality=missing)。

对外接口统一，下游不关心具体数据源。
"""
import os
import json
import time
import logging
import requests
import urllib3
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

from app.data.providers.base import BaseDataProvider
from app.data import finnhub_limiter as fh_quota
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

    # ── T1 (2026-10-28): industry 落盘缓存 ────────────────────────────────
    # `stock/profile2` 只用到一个字段 finnhubIndustry —— 行业分类是**静态**的, 永不 daily 变化,
    # 却原来在 Futu happy path 上逐只拉一次 (盘前 76 只/天; 今天这一项独占 28 次 429)。
    # 改为进程共享 + 落盘缓存(跨重启), 默认 30 天。
    _PROFILE_CACHE_PATH = Path(__file__).resolve().parents[3] / "data" / "finnhub_profile_cache.json"
    _PROFILE_TTL_SEC = 30 * 86400
    _profile_cache: Dict[str, Dict[str, Any]] = {}
    _profile_cache_loaded = False

    def _load_profile_cache(self) -> None:
        if FundamentalProvider._profile_cache_loaded:
            return
        FundamentalProvider._profile_cache_loaded = True
        try:
            if self._PROFILE_CACHE_PATH.exists():
                raw = json.loads(self._PROFILE_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    FundamentalProvider._profile_cache = raw
        except Exception as e:
            logger.debug(f"[FundamentalProvider] profile 缓存读取失败(忽略): {e}")

    def _save_profile_cache(self) -> None:
        try:
            self._PROFILE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._PROFILE_CACHE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._profile_cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._PROFILE_CACHE_PATH)
        except Exception as e:
            logger.debug(f"[FundamentalProvider] profile 缓存写入失败(忽略): {e}")

    def _get_profile(self, bare: str) -> Optional[Dict[str, Any]]:
        """industry 静态字段: 落盘缓存(30d) 优先, 未命中才请求 profile2。"""
        self._load_profile_cache()
        ent = self._profile_cache.get(bare)
        if ent and (time.time() - float(ent.get("ts", 0))) < self._PROFILE_TTL_SEC:
            return ent.get("data") or {}
        profile = self._finnhub_get("stock/profile2", {"symbol": bare})
        if profile and isinstance(profile, dict):
            self._profile_cache[bare] = {"ts": time.time(), "data": profile}
            self._save_profile_cache()
            return profile
        return None

    def _finnhub_get(self, endpoint: str, params: Dict, timeout: int = 15) -> Optional[Any]:
        """低层 Finnhub GET（2026-10-28: 走进程级共享配额桶）"""
        if not self.api_key or self.api_key == "***":
            return None
        headers = {"X-Finnhub-Token": self.api_key}
        # 原实现裸 requests 无限速: 盘前 5 并发 × 逐只降级 5 连击 = 429 主来源之一
        if not fh_quota.acquire(n=1, timeout=20.0):
            logger.warning(f"[FundamentalProvider] Finnhub 配额排队超时, 跳过 {endpoint}")
            return None
        try:
            # C3 (2026-08-24, 体检 E2): verify=True 收口 (实测握手正常)
            resp = requests.get(
                f"{self.base_url}/{endpoint}",
                params=params, headers=headers, timeout=timeout, verify=True,
            )
            # 校准: 服务端自报剩余额度（跨进程共用 key 时唯一可见的真值）
            fh_quota.observe(getattr(resp, "headers", None))
            if resp.status_code == 429:
                fh_quota.on_429(endpoint)
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

        # 2026-10-27 [RFC-XXX 待补]: 删除两条恒不产出的死腿 (Yahoo / AlphaVantage)
        #   Yahoo: _fetch_yahoo 的成立条件 market_data["fundamentals"] 在
        #     yahoo_provider.get_market_data() 里从未存在 (git log -S 全历史 0 命中)
        #     ⇒ 即便 yfinance 可用也恒返回 None; 45 天日志 5 次触达 0 次成功,
        #     且每次空转 6-11s (yfinance 库 crumb 链路 429/空体)。
        #   AlphaVantage: 该腿本身即 `return None` 桩。
        #   ⇒ 真实降级链只剩 Futu → Finnhub (A10 期 fundamental 质量 ok 135/135)。

        # 全部失败
        return {
            "status": "failed",
            "error": "All data sources failed",
            "data_source": "none",
            "fetched_at": now_iso,
        }

    # ── T2 (2026-10-28): 批量快照预取 ──────────────────────────────────────
    # Futu get_market_snapshot 限 60 次/30 秒。原链路逐只 1 次 × 5 并发 ⇒ 今天实测
    # `获取市场快照频率太高` 9 次, 且撞了之后该只**立刻向 Finnhub 连发 5 次**(降级腿),
    # 把 Futu 的限流放大成 Finnhub 风暴(96 次 429 的入口)。
    # 同仓 `premarket_enrichment._fetch_mcap_map` 已证明 `get_market_snapshot(list)` 可用,
    # 这里复用同一手法(含整批失败时二分隔离坏票), 76 只 → 1 批。
    PREFETCH_TTL_SEC = 180
    SNAPSHOT_BATCH_SIZE = 200
    _prefetched: Dict[str, Dict[str, Any]] = {}
    _prefetch_ts = 0.0

    @staticmethod
    def _snap_key(symbol: str) -> str:
        """US.BRK-B → US.BRK.B（Futu OpenD 只认点号写法）"""
        _market, _, _base = str(symbol).partition(".")
        return f"{_market}.{to_dotted_class_share(_base)}" if _base else str(symbol)

    @staticmethod
    def _is_throttle(msg: Any) -> bool:
        s = str(msg)
        return ("频率" in s) or ("最多" in s) or ("too frequent" in s.lower())

    def _snapshot_bisect(self, ctx, batch: List[str], out: Dict[str, Dict[str, Any]], depth: int = 0) -> None:
        """整批失败时二分隔离坏票（与 premarket_enrichment 同手法）；单只失败即放弃该只。"""
        from futu import RET_OK
        if not batch or depth > 8:
            return
        try:
            ret, df = ctx.get_market_snapshot(batch)
        except Exception:
            ret, df = -1, None
        if ret == RET_OK and df is not None and not df.empty:
            for _, r in df.iterrows():
                out[str(r.get("code", ""))] = r.to_dict()
            return
        if len(batch) == 1:
            logger.debug(f"[FundamentalProvider] 批量快照单只失败: {batch[0]} — 该只回退 Finnhub")
            return
        mid = len(batch) // 2
        self._snapshot_bisect(ctx, batch[:mid], out, depth + 1)
        self._snapshot_bisect(ctx, batch[mid:], out, depth + 1)

    def prefetch_snapshots(self, symbols: List[str]) -> int:
        """一批拉全部候选的快照, 供 _fetch_futu 复用。返回命中只数（失败返回 0，调用方回退逐只）。"""
        symbols = [s for s in (symbols or []) if s]
        if not symbols:
            return 0
        try:
            from app.web_dashboard.manual_order_service import get_manual_order_service
            svc = get_manual_order_service()
            if not svc.connect():
                logger.warning("[FundamentalProvider] 批量快照预取跳过: Futu connect failed")
                return 0
            codes = list(dict.fromkeys(self._snap_key(s) for s in symbols))
            got: Dict[str, Dict[str, Any]] = {}
            for i in range(0, len(codes), self.SNAPSHOT_BATCH_SIZE):
                if i > 0:
                    time.sleep(0.3)  # 批次间隔流保护（与 premarket_enrichment 一致）
                self._snapshot_bisect(svc._quote_ctx, codes[i:i + self.SNAPSHOT_BATCH_SIZE], got)
            if got:
                FundamentalProvider._prefetched = got
                FundamentalProvider._prefetch_ts = time.time()
            logger.info(
                f"[FundamentalProvider] 批量快照预取 {len(got)}/{len(codes)} 只 "
                f"({len(codes)} 只逐只调用 → {-(-len(codes) // self.SNAPSHOT_BATCH_SIZE)} 批)"
            )
            return len(got)
        except Exception as e:
            logger.warning(f"[FundamentalProvider] 批量快照预取失败, 回退逐只: {e}", exc_info=True)
            return 0

    def _prefetched_row(self, snap_symbol: str) -> Optional[Dict[str, Any]]:
        if not self._prefetched:
            return None
        if (time.time() - self._prefetch_ts) > self.PREFETCH_TTL_SEC:
            return None
        return self._prefetched.get(snap_symbol)

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
            _snap_symbol = self._snap_key(symbol)
            # T2: 优先用批量预取的行（76 只逐只调用 → 1 批）
            row = self._prefetched_row(_snap_symbol)
            if row is None:
                ret, data = svc._quote_ctx.get_market_snapshot([_snap_symbol])
                # 注意: 单例连接常驻, 绝不在此 disconnect (会断掉 dashboard 手动交易的共享连接)
                # T4: 上游限流时单只延迟重试一次, 不要立刻降级到 Finnhub 5 连击（跨源放大链）
                if ret != RET_OK and self._is_throttle(data):
                    time.sleep(1.2)
                    ret, data = svc._quote_ctx.get_market_snapshot([_snap_symbol])
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
            # T1 (2026-10-28): 走 30 天落盘缓存（industry 静态，逐只每日拉 = 纯浪费 + 429 大户）
            bare = symbol.replace("US.", "").replace("HK.", "").upper()
            profile = self._get_profile(bare)
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
        # 2026-10-28 (T4): metric 是这条腿的"总闸"—— 它失败(429/空)整只即失败,
        #   原来仍继续请求其余 4 个端点, 在限流时把 1 次失败放大成 5 次。现先取 metric 并早退。
        metric = self._finnhub_get("stock/metric", {"symbol": bare, "metric": "all"})
        if not metric or "metric" not in metric:
            return None  # Finnhub 失败（429/无数据）⇒ 不再追加 4 个请求

        recommendations = self._finnhub_get("stock/recommendation", {"symbol": bare})
        profile = self._get_profile(bare)   # T1: 30 天落盘缓存

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
