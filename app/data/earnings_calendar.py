"""
美股财报日历模块 (Earnings Calendar)

功能:
1. 通过 Finnhub API 获取美股财报日历
2. 按 watchlist 过滤，只关注持仓和监控名单中的股票
3. 缓存结果避免频繁 API 调用
4. 为盘前 Pipeline 提供财报上下文

数据源: Finnhub Earnings Calendar API (免费，60 calls/min)
更新时间: 2026-04-23
"""

import os
import time
import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path

logger = logging.getLogger("ftseek.earnings_calendar")

# 缓存路径
CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "earnings_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "earnings_calendar.json"
CACHE_TTL = 3600 * 4  # 4 小时缓存 (财报数据一天更新一次足够)


class EarningsCalendar:
    """
    美股财报日历管理器

    使用 Finnhub Earnings Calendar API 获取财报数据，
    并过滤出我们关注的股票。
    """

    def __init__(self, api_key: str = None, cache_ttl: int = CACHE_TTL):
        self.api_key = api_key or self._load_api_key()
        self.base_url = "https://finnhub.io/api/v1/calendar/earnings"
        self.cache_ttl = cache_ttl
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def _load_api_key(self) -> str:
        """加载 Finnhub API Key (多级降级)"""
        # 1. 环境变量
        key = os.environ.get("FINNHUB_API_KEY", "")
        if key and not key.startswith("***"):
            return key

        # 2. .env.us 共享文件
        for path in ["/opt/ftseek/FTSeek-US/.env.us", "/opt/ftseek/FTSeek/.env.us"]:
            try:
                with open(path) as f:
                    for line in f:
                        if line.startswith("FINNHUB_API_KEY="):
                            key = line.split("=", 1)[1].strip()
                            if key and not key.startswith("***"):
                                return key
            except Exception:
                pass

        logger.warning("[EarningsCalendar] Finnhub API Key 未配置")
        return ""

    def _is_cached(self) -> bool:
        """检查缓存是否有效"""
        if self._cache and (time.time() - self._cache_time) < self.cache_ttl:
            return True

        # 检查磁盘缓存
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                if (time.time() - data.get("fetched_at", 0)) < self.cache_ttl:
                    self._cache = data
                    self._cache_time = data.get("fetched_at", 0)
                    return True
        except Exception:
            pass

        return False

    def _save_cache(self, data: Dict):
        """保存缓存到内存和磁盘"""
        self._cache = data
        self._cache_time = time.time()

        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[EarningsCalendar] 保存缓存失败: {e}")

    def fetch_earnings(
        self,
        days_ahead: int = 7,
        symbols: List[str] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        获取财报日历

        Args:
            days_ahead: 向前看几天 (默认 7)
            symbols: 过滤特定股票代码 (默认 None = 不过滤)
            force_refresh: 强制刷新缓存

        Returns:
            {
                "fetched_at": timestamp,
                "period": "YYYY-MM-DD to YYYY-MM-DD",
                "total_count": int,
                "watchlist_count": int,
                "earnings": [  # 过滤后的财报列表
                    {
                        "symbol": "AAPL",
                        "date": "2026-04-25",
                        "hour": "AMC",  # AMC=盘后, BMO=盘前
                        "eps_estimate": float,
                        "eps_actual": float,
                        "revenue_estimate": float,
                        "year_ago_eps": float,
                        "number_of_estimates": int,
                        "in_watchlist": bool,
                    }
                ],
            }
        """
        # 检查缓存
        if not force_refresh and self._is_cached():
            cached = self._cache
            if symbols:
                # 如果传入了 symbols 过滤，从缓存中过滤
                filtered = [
                    e for e in cached.get("earnings_all", []) if e["symbol"] in symbols
                ]
                return {
                    "fetched_at": cached["fetched_at"],
                    "period": cached["period"],
                    "total_count": len(filtered),
                    "watchlist_count": len([e for e in filtered if e.get("in_watchlist")]),
                    "earnings": filtered,
                    "from_cache": True,
                }
            return {
                "fetched_at": cached["fetched_at"],
                "period": cached["period"],
                "total_count": cached.get("total_count", 0),
                "watchlist_count": cached.get("watchlist_count", 0),
                "earnings": cached.get("earnings", []),
                "from_cache": True,
            }

        if not self.api_key:
            return {
                "fetched_at": time.time(),
                "period": "",
                "total_count": 0,
                "watchlist_count": 0,
                "earnings": [],
                "error": "Finnhub API Key 未配置",
            }

        try:
            today = datetime.now().strftime("%Y-%m-%d")
            future = (datetime.now() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

            resp = requests.get(
                self.base_url,
                params={"token": self.api_key, "from": today, "to": future},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            raw_earnings = data.get("earningsCalendar", [])
            logger.info(
                f"[EarningsCalendar] 获取到 {len(raw_earnings)} 家公司财报 "
                f"({today} ~ {future})"
            )

            # 获取 watchlist 用于标记
            watchlist_symbols = self._get_watchlist_symbols()
            watchlist_set = set(s.upper() for s in watchlist_symbols)

            # 处理财报数据
            processed = []
            for e in raw_earnings:
                symbol = e.get("symbol", "").upper()
                if not symbol:
                    continue

                entry = {
                    "symbol": symbol,
                    "date": e.get("date", ""),
                    "hour": e.get("hour", ""),  # AMC / BMO
                    "eps_estimate": e.get("epsEstimate"),
                    "eps_actual": e.get("epsActual"),
                    "revenue_estimate": e.get("revenueEstimate"),
                    "year_ago_eps": e.get("yearAgoEps"),
                    "number_of_estimates": e.get("numberOfEstimates", 0),
                    "in_watchlist": symbol in watchlist_set,
                }
                processed.append(entry)

            # 如果传入了 symbols 过滤
            if symbols:
                symbols_set = set(s.upper().replace("US.", "") for s in symbols)
                filtered = [e for e in processed if e["symbol"] in symbols_set]
            else:
                filtered = [e for e in processed if e["in_watchlist"]]

            result = {
                "fetched_at": time.time(),
                "period": f"{today} ~ {future}",
                "total_count": len(processed),
                "watchlist_count": len([e for e in processed if e["in_watchlist"]]),
                "earnings_all": processed,  # 全部 (用于缓存)
                "earnings": filtered,  # 过滤后的
                "from_cache": False,
            }

            self._save_cache(result)
            return result

        except Exception as e:
            logger.error(f"[EarningsCalendar] 获取财报日历失败: {e}")
            return {
                "fetched_at": time.time(),
                "period": "",
                "total_count": 0,
                "watchlist_count": 0,
                "earnings": [],
                "error": str(e),
            }

    def _get_watchlist_symbols(self) -> List[str]:
        """获取监控名单中的美股代码"""
        try:
            from app.config import WATCH_LIST_US

            # 去掉 "US." 前缀
            symbols = []
            for code in WATCH_LIST_US:
                sym = code.replace("US.", "").upper()
                symbols.append(sym)
            return symbols
        except Exception:
            # Fallback: 核心美股
            return ["AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "TSLA", "META", "BABA", "JD", "BIDU", "PDD", "NIO", "XPEV", "LI"]

    def format_context(self, earnings_data: Dict = None) -> str:
        """
        格式化财报日历为文本上下文 (用于 LLM 盘前分析)

        Args:
            earnings_data: fetch_earnings() 返回的数据 (为 None 时自动获取)

        Returns:
            格式化的文本
        """
        if earnings_data is None:
            earnings_data = self.fetch_earnings()

        if earnings_data.get("error"):
            return f"⚠️ 财报日历获取失败: {earnings_data.get('error', '未知错误')}"

        earnings = earnings_data.get("earnings", [])
        if not earnings:
            return "📅 财报日历: 未来 7 天无监控名单中的股票发布财报"

        # 按日期分组
        by_date = {}
        for e in earnings:
            date = e.get("date", "未知")
            if date not in by_date:
                by_date[date] = []
            by_date[date].append(e)

        lines = [f"## 📅 财报日历 ({earnings_data.get('period', '')})"]
        lines.append(f"共 {len(earnings)} 只监控股有财报\n")

        for date in sorted(by_date.keys()):
            items = by_date[date]
            day_name = self._get_day_name(date)
            lines.append(f"### {date} ({day_name}) — {len(items)} 只")

            for e in items:
                wl_tag = "📌" if e.get("in_watchlist") else ""
                hour_tag = "🌅盘前" if e.get("hour") == "BMO" else ("🌙盘后" if e.get("hour") == "AMC" else "")
                eps_est = f"EPS≈${e['eps_estimate']:.2f}" if e.get("eps_estimate") else "EPS=N/A"
                lines.append(
                    f"  {wl_tag} **{e['symbol']}** {hour_tag} {eps_est}"
                )

            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _get_day_name(date_str: str) -> str:
        """获取日期对应的星期"""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            return days[dt.weekday()]
        except Exception:
            return ""

    def get_status(self) -> Dict:
        """获取模块状态"""
        return {
            "api_key_configured": bool(self.api_key and not self.api_key.startswith("***")),
            "cache_valid": self._is_cached(),
            "cache_file": str(CACHE_FILE),
        }


# ============================================================
#  全局单例
# ============================================================
_instance: Optional[EarningsCalendar] = None


def get_earnings_calendar() -> EarningsCalendar:
    """获取全局单例"""
    global _instance
    if _instance is None:
        _instance = EarningsCalendar()
    return _instance


def reset_earnings_calendar():
    """重置单例 (用于测试)"""
    global _instance
    _instance = None
