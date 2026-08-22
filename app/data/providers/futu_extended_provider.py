"""
Futu 扩展数据 Provider

利用 Futu OpenD 10.7+ 新增 API，提供：
  1. 空头持仓 (short_interest) — 断裂点2修复
  2. 每日卖空成交量 (daily_short_volume)
  3. 分析师评级共识 (research_analyst_consensus) — 替代 Finnhub 付费 API
  4. 内部人交易 (insider_trade_list) — 补充 SEC EDGAR
  5. 分析师评级摘要 (research_rating_summary)

数据来源：Futu OpenD (本地，无需外部 API key)
限制：每 30 秒最多 30 次请求
"""
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

from app.data.providers.base import BaseDataProvider
from app.utils.timezone import now_utc

logger = logging.getLogger(__name__)


class FutuExtendedProvider(BaseDataProvider):
    """Futu 扩展数据 Provider（空头/分析师/内部人）"""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        super().__init__(name="futu_extended", cache_ttl_seconds=3600)  # 1h 缓存
        self._host = host
        self._port = port
        self._ctx = None

    def _get_ctx(self):
        """懒初始化 Futu Context"""
        if self._ctx is not None:
            return self._ctx
        try:
            from futu import OpenQuoteContext
            self._ctx = OpenQuoteContext(host=self._host, port=self._port)
            logger.info(f"[FutuExtended] 连接成功 {self._host}:{self._port}")
        except Exception as e:
            logger.error(f"[FutuExtended] 连接失败: {e}")
            self._ctx = None
        return self._ctx

    def get_data(self, symbol: str, **kwargs) -> Dict[str, Any]:
        """统一入口：返回该 symbol 的全部扩展数据"""
        now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        result: Dict[str, Any] = {
            "status": "ok",
            "data_source": "futu_opend",
            "fetched_at": now_iso,
            "data": {},
        }

        result["data"]["short_interest"] = self.get_short_interest(symbol)
        # 2026-08-21: 复活调用 — 6/29 删除是 SDK 10.03 时代判断, 10.09.6908 已提供
        # get_research_analyst_consensus (实测 MU/WEAV/BULL/AAPL 全覆盖, 含目标价)
        result["data"]["analyst_consensus"] = self.get_analyst_consensus(symbol)
        result["data"]["insider_trades"] = self.get_insider_trades(symbol)

        # 如果全部子项都失败，标记整体失败
        if not any(v.get("status") == "ok" for v in result["data"].values()):
            result["status"] = "failed"
            result["error"] = "All sub-queries failed"

        return result

    def get_short_interest(self, symbol: str, num: int = 10) -> Dict[str, Any]:
        """
        获取空头持仓数据

        Returns:
            {
                "status": "ok",
                "data": {
                    "latest_date": "2026-05-29",
                    "shares_short": 155886024,
                    "short_percent": 1.061,  # 占流通股比例 (%)
                    "days_to_cover": 3.38,
                    "avg_daily_volume": 46064146,
                }
            }
        """
        now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        cache_key = f"si_{symbol}"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        ctx = self._get_ctx()
        if not ctx:
            return {"status": "failed", "error": "Futu context unavailable", "fetched_at": now_iso}

        try:
            ret, us_df, err = ctx.get_short_interest(symbol, num=num)
            if ret != 0:
                return {"status": "failed", "error": f"get_short_interest failed: {err}", "fetched_at": now_iso}

            if us_df is None or len(us_df) == 0:
                return {"status": "ok", "data": {}, "data_source": "futu_opend", "fetched_at": now_iso}

            latest = us_df.iloc[0].to_dict()
            data = {
                "latest_date": latest.get("timestamp_str", ""),
                "shares_short": int(latest.get("shares_short", 0)),
                "short_percent": float(latest.get("short_percent", 0)),
                "days_to_cover": float(latest.get("days_to_cover", 0)),
                "avg_daily_volume": int(latest.get("avg_daily_share_volume", 0)),
                "close_price": float(latest.get("close_price", 0)),
            }
            result = {"status": "ok", "data": data, "data_source": "futu_opend", "fetched_at": now_iso}
            self._set_cache(cache_key, result)
            logger.debug(f"[FutuExtended] {symbol} short_interest: {data['short_percent']}% ({data['shares_short']:,} shares)")
            return result
        except Exception as e:
            logger.warning(f"[FutuExtended] get_short_interest error {symbol}: {e}")
            return {"status": "failed", "error": str(e), "fetched_at": now_iso}

    def get_daily_short_volume(self, symbol: str, num: int = 5) -> Dict[str, Any]:
        """获取每日卖空成交量"""
        now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        cache_key = f"dsv_{symbol}"
        # 2026-08-22c 死门修复 (门活性探针首跑抓出): 原传 ttl_override=3600, 但
        # BaseDataProvider._is_cache_valid 不接受该参数 → 每次 TypeError → 调用方
        # fail-open 吞掉 → 卖空拥挤门 (short_squeeze) 自上线起恒不触发。
        # 实例 TTL 本就是 3600s, 该参数纯冗余, 删除即修复。
        if self._is_cache_valid(cache_key):
            return self._cache.get(cache_key, {"status": "failed", "error": "cache miss"})

        ctx = self._get_ctx()
        if not ctx:
            return {"status": "failed", "error": "Futu context unavailable", "fetched_at": now_iso}

        try:
            ret, us_df, err = ctx.get_daily_short_volume(symbol, num=num)
            if ret != 0:
                return {"status": "failed", "error": f"get_daily_short_volume failed: {err}", "fetched_at": now_iso}

            if us_df is None or len(us_df) == 0:
                return {"status": "ok", "data": {}, "data_source": "futu_opend", "fetched_at": now_iso}

            latest = us_df.iloc[0].to_dict()
            data = {
                "latest_date": latest.get("timestamp_str", ""),
                "total_short": int(latest.get("total_shares_short", 0)),
                "short_percent": float(latest.get("short_percent", 0)),
                "volume": int(latest.get("volume", 0)),
                "daily_trade_avg_ratio": float(latest.get("daily_trade_avg_ratio", 0)),
            }
            result = {"status": "ok", "data": data, "data_source": "futu_opend", "fetched_at": now_iso}
            self._set_cache(cache_key, result)
            return result
        except Exception as e:
            logger.warning(f"[FutuExtended] get_daily_short_volume error {symbol}: {e}")
            return {"status": "failed", "error": str(e), "fetched_at": now_iso}

    def get_analyst_consensus(self, symbol: str) -> Dict[str, Any]:
        """
        获取分析师评级共识（近3个月）

        2026-08-21: 复活 — 6/29/7/2 删除系 SDK 10.03 时代判断;
        SDK 10.09.6908 提供 get_research_analyst_consensus, 实测返回 dict:
          highest/average/lowest (目标价), rating (1-5), total (分析师数),
          update_time_str, buy/hold/sell (百分比)
        失败时优雅降级 (status=failed), 调用方已按此契约处理。
        """
        now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        cache_key = f"ac_{symbol}"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        ctx = self._get_ctx()
        if not ctx:
            return {"status": "failed", "error": "Futu context unavailable", "fetched_at": now_iso}

        try:
            if not hasattr(ctx, "get_research_analyst_consensus"):
                return {"status": "failed",
                        "error": "SDK lacks get_research_analyst_consensus",
                        "fetched_at": now_iso}
            result = ctx.get_research_analyst_consensus(symbol)
            ret = result[0]
            d = result[1] if len(result) > 1 else None
            err = result[2] if len(result) > 2 else ""
            if ret != 0:
                return {"status": "failed",
                        "error": f"analyst_consensus failed: {str(d)[:120]} {err}",
                        "fetched_at": now_iso}
            if not isinstance(d, dict) or d.get("total") in (None, 0):
                return {"status": "ok", "data": {}, "data_source": "futu_opend", "fetched_at": now_iso}

            rating = d.get("rating")
            rating_label = {5: "strong_buy", 4: "buy", 3: "hold", 2: "underperform", 1: "sell"}.get(rating, "unknown")
            data = {
                "rating": rating,
                "rating_label": rating_label,
                "total_analysts": int(d.get("total") or 0),
                "target_avg": float(d.get("average") or 0),
                "target_high": float(d.get("highest") or 0),
                "target_low": float(d.get("lowest") or 0),
                "buy_pct": float(d.get("buy") or 0),
                "hold_pct": float(d.get("hold") or 0),
                "sell_pct": float(d.get("sell") or 0),
                "update_date": str(d.get("update_time_str") or ""),
            }
            result = {"status": "ok", "data": data, "data_source": "futu_opend", "fetched_at": now_iso}
            self._set_cache(cache_key, result)
            logger.debug(f"[FutuExtended] {symbol} consensus: rating={rating} avg_target={data['target_avg']} n={data['total_analysts']}")
            return result
        except Exception as e:
            logger.warning(f"[FutuExtended] get_analyst_consensus error {symbol}: {e}")
            return {"status": "failed", "error": str(e), "fetched_at": now_iso}

    def get_insider_trades(self, symbol: str, num: int = 20) -> Dict[str, Any]:
        """
        获取内部人交易列表

        Returns:
            {
                "status": "ok",
                "data": {
                    "transactions": [...],
                    "buy_count": 5,
                    "sell_count": 3,
                    "net_shares": 12345,
                    "signal": "buy",
                }
            }
        """
        now_iso = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        cache_key = f"it_{symbol}"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        ctx = self._get_ctx()
        if not ctx:
            return {"status": "failed", "error": "Futu context unavailable", "fetched_at": now_iso}

        try:
            result = ctx.get_insider_trade_list(symbol, num=num)
            ret = result[0]
            df = result[1] if len(result) > 1 else None
            err = result[2] if len(result) > 2 else ""
            if ret != 0:
                return {"status": "failed", "error": f"insider_trade_list failed: {err}", "fetched_at": now_iso}

            if df is None or len(df) == 0:
                return {"status": "ok", "data": {"transactions": [], "buy_count": 0, "sell_count": 0, "net_shares": 0, "signal": "neutral"}, "data_source": "futu_opend", "fetched_at": now_iso}

            transactions = []
            buy_count = 0
            sell_count = 0
            net_shares = 0

            for _, row in df.iterrows():
                row_dict = row.to_dict()
                trade_shares = int(row_dict.get("trade_shares", 0))
                trans_type = str(row_dict.get("transaction_type", ""))

                is_buy = "买" in trans_type or "获得" in trans_type
                is_sell = "卖" in trans_type

                if is_buy and trade_shares > 0:
                    buy_count += 1
                    net_shares += trade_shares
                elif is_sell and trade_shares < 0:
                    sell_count += 1
                    net_shares += trade_shares  # negative

                transactions.append({
                    "insider": str(row_dict.get("name", "")),
                    "title": str(row_dict.get("title", "")),
                    "action": trans_type,
                    "shares": trade_shares,
                    "date": str(row_dict.get("min_trade_date_str", "")),
                    "source": str(row_dict.get("source_group_name", "")),
                })

            if buy_count >= 3 and net_shares > 10000:
                signal = "strong_buy"
            elif buy_count > sell_count and net_shares > 0:
                signal = "buy"
            elif sell_count > buy_count and net_shares < 0:
                signal = "sell"
            else:
                signal = "neutral"

            data = {
                "transactions": transactions[:10],
                "buy_count": buy_count,
                "sell_count": sell_count,
                "net_shares": net_shares,
                "signal": signal,
            }
            result = {"status": "ok", "data": data, "data_source": "futu_opend", "fetched_at": now_iso}
            self._set_cache(cache_key, result)
            logger.debug(f"[FutuExtended] {symbol} insider: buy={buy_count} sell={sell_count} net={net_shares} → {signal}")
            return result
        except Exception as e:
            logger.warning(f"[FutuExtended] get_insider_trades error {symbol}: {e}")
            return {"status": "failed", "error": str(e), "fetched_at": now_iso}

    def close(self):
        """关闭 Futu Context"""
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None

    def __del__(self):
        self.close()
