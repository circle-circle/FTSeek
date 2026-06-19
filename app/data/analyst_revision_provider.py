"""
Yahoo Finance v10 分析师修正数据提供商 (ADR-007: 2026-06-16)

数据来源: Yahoo Finance REST API (v10/finance/quoteSummary)
模块: earningsTrend, upgradeDowngradeHistory, recommendationTrend

核心功能:
  1. 获取 EPS 估计共识 (当前季度 + 下季度 + 当前年度 + 下年度)
  2. 获取分析师升级/降级历史
  3. 获取推荐趋势 (strongBuy/buy/hold/sell/strongSell 月度变化)
  4. 检测向上修正信号 (PEAD + 估计上调动量)

不依赖 yfinance 库 (被限速), 直接使用 REST API + cookie/crumb 认证。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.cookiejar import MozillaCookieJar

logger = logging.getLogger("ftseek.analyst_revision")

# ── 常量 ──
CACHE_DIR = Path(os.getenv("FTSEEK_CACHE_DIR", "/opt/ftseek/FTSeek-US/data/yahoo_cache"))
CACHE_TTL_ESTIMATES = 86400     # 24h (估计数据变化慢)
CACHE_TTL_UPGRADES = 43200      # 12h
YAHOO_BASE_URL = "https://query1.finance.yahoo.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


@dataclass
class EarningsEstimate:
    """单个时期的 EPS 估计"""
    period: str             # "0q", "+1q", "0y", "+1y"
    period_label: str       # "当前季度", "下季度", etc.
    end_date: str           # 预计结束日期
    eps_avg: float          # 共识 EPS
    eps_low: float          # 最低估计
    eps_high: float         # 最高估计
    num_analysts: int       # 分析师数量
    growth: float           # YoY 增长率
    year_ago_eps: float     # 去年同期 EPS
    revenue_avg: float      # 共识营收
    revenue_growth: float   # 营收 YoY 增长


@dataclass
class AnalystRevision:
    """分析师修正汇总"""
    symbol: str
    estimates: List[EarningsEstimate]
    recent_upgrades: int       # 近30天升级数
    recent_downgrades: int     # 近30天降级数
    net_revisions: int         # 净修正 (upgrades - downgrades)
    consensus_shift: float     # 共识变化 (当前月 vs 上月, buy_ratio 变化)
    revision_score: float      # 综合修正分数 (-1 to +1)
    rationale: str


class YahooAnalystProvider:
    """Yahoo Finance 分析师数据提供商"""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cookie = None
        self._crumb = None
        self._crumb_time = 0

    def _get_session(self) -> tuple:
        """获取 Yahoo Finance cookie + crumb"""
        # Cache crumb for 30 minutes
        if self._crumb and (time.time() - self._crumb_time) < 1800:
            return self._cookie, self._crumb

        try:
            # Step 1: Get cookie from consent page
            cookie_header = ""
            req = Request(
                "https://fc.yahoo.com/",
                headers={"User-Agent": USER_AGENT},
            )
            try:
                urlopen(req, timeout=10)
            except HTTPError as e:
                # Expected: 404 or 401, but sets cookies
                cookie_header = e.headers.get("Set-Cookie", "")

            # Step 2: Get crumb
            req2 = Request(
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
                headers={
                    "User-Agent": USER_AGENT,
                    "Cookie": cookie_header,
                },
            )
            try:
                with urlopen(req2, timeout=10) as resp:
                    self._crumb = resp.read().decode("utf-8").strip()
                    self._cookie = cookie_header
                    self._crumb_time = time.time()
                    logger.debug(f"[Yahoo] Got crumb: {self._crumb[:8]}...")
            except Exception as e:
                logger.debug(f"[Yahoo] Crumb fetch failed: {e}")
                # Try without crumb (some endpoints work)
                self._crumb = ""
                self._cookie = ""

        except Exception as e:
            logger.debug(f"[Yahoo] Session setup failed: {e}")
            self._crumb = ""
            self._cookie = ""

        return self._cookie, self._crumb

    def _yahoo_request(self, url: str) -> Optional[Dict]:
        """发送 Yahoo Finance API 请求"""
        cookie, crumb = self._get_session()
        if crumb:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}crumb={crumb}"

        req = Request(url, headers={
            "User-Agent": USER_AGENT,
            "Cookie": cookie or "",
        })
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except (HTTPError, URLError) as e:
            logger.debug(f"[Yahoo] Request failed: {url[:80]}... -> {e}")
            return None

    def _cache_path(self, key: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "_", key)
        return CACHE_DIR / f"{safe}.json"

    def _read_cache(self, key: str, max_age: int) -> Optional[Dict]:
        path = self._cache_path(key)
        if not path.exists():
            return None
        age = time.time() - path.stat().st_mtime
        if age > max_age:
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _write_cache(self, key: str, data: Dict):
        path = self._cache_path(key)
        try:
            path.write_text(json.dumps(data, default=str))
        except Exception:
            pass

    def get_earnings_estimates(self, symbol: str) -> List[EarningsEstimate]:
        """
        获取 EPS 估计数据 (4 个时期)

        Args:
            symbol: US.AAPL 或 AAPL

        Returns:
            EarningsEstimate 列表 (最多4个时期)
        """
        ticker = symbol.replace("US.", "").upper()
        cache_key = f"estimates_{ticker}"

        cached = self._read_cache(cache_key, CACHE_TTL_ESTIMATES)
        if cached and "estimates" in cached:
            return [EarningsEstimate(**e) for e in cached["estimates"]]

        url = f"{YAHOO_BASE_URL}/v10/finance/quoteSummary/{ticker}?modules=earningsTrend"
        data = self._yahoo_request(url)
        if not data:
            return []

        try:
            result = data["quoteSummary"]["result"][0]
            trend_data = result.get("earningsTrend", {}).get("trend", [])
        except (KeyError, IndexError):
            return []

        estimates = []
        period_labels = {
            "0q": "当前季度", "+1q": "下季度",
            "0y": "当前年度", "+1y": "下年度",
        }

        for t in trend_data:
            period = t.get("period", "")
            ee = t.get("earningsEstimate", {})
            re_data = t.get("revenueEstimate", {})

            if not ee:
                continue

            try:
                est = EarningsEstimate(
                    period=period,
                    period_label=period_labels.get(period, period),
                    end_date=t.get("endDate", ""),
                    eps_avg=_safe_float(ee.get("avg", {}).get("raw", 0)),
                    eps_low=_safe_float(ee.get("low", {}).get("raw", 0)),
                    eps_high=_safe_float(ee.get("high", {}).get("raw", 0)),
                    num_analysts=int(ee.get("numberOfAnalysts", {}).get("raw", 0)),
                    growth=_safe_float(ee.get("growth", {}).get("raw", 0)),
                    year_ago_eps=_safe_float(ee.get("yearAgoEps", {}).get("raw", 0)),
                    revenue_avg=_safe_float(re_data.get("avg", {}).get("raw", 0)) if re_data else 0,
                    revenue_growth=_safe_float(re_data.get("growth", {}).get("raw", 0)) if re_data else 0,
                )
                estimates.append(est)
            except Exception as e:
                logger.debug(f"[Yahoo] Parse estimate error: {e}")

        # Cache
        self._write_cache(cache_key, {
            "estimates": [
                {
                    "period": e.period, "period_label": e.period_label,
                    "end_date": e.end_date, "eps_avg": e.eps_avg,
                    "eps_low": e.eps_low, "eps_high": e.eps_high,
                    "num_analysts": e.num_analysts, "growth": e.growth,
                    "year_ago_eps": e.year_ago_eps,
                    "revenue_avg": e.revenue_avg, "revenue_growth": e.revenue_growth,
                }
                for e in estimates
            ],
            "fetched_at": datetime.now().isoformat(),
        })

        return estimates

    def get_upgrade_downgrade_history(self, symbol: str) -> List[Dict]:
        """
        获取分析师升级/降级历史

        Returns:
            [{firm, fromGrade, toGrade, action, date}, ...]
        """
        ticker = symbol.replace("US.", "").upper()
        cache_key = f"upgrades_{ticker}"

        cached = self._read_cache(cache_key, CACHE_TTL_UPGRADES)
        if cached and "history" in cached:
            return cached["history"]

        url = f"{YAHOO_BASE_URL}/v10/finance/quoteSummary/{ticker}?modules=upgradeDowngradeHistory"
        data = self._yahoo_request(url)
        if not data:
            return []

        try:
            result = data["quoteSummary"]["result"][0]
            raw_history = result.get("upgradeDowngradeHistory", {}).get("history", [])
        except (KeyError, IndexError):
            return []

        history = []
        for item in raw_history:
            epoch = item.get("epochGradeDate", 0)
            history.append({
                "firm": item.get("firm", ""),
                "from_grade": item.get("fromGrade", ""),
                "to_grade": item.get("toGrade", ""),
                "action": item.get("action", ""),
                "date": datetime.fromtimestamp(epoch).strftime("%Y-%m-%d") if epoch else "",
                "epoch": epoch,
            })

        self._write_cache(cache_key, {"history": history})
        return history

    def get_recommendation_trend(self, symbol: str) -> Dict[str, Any]:
        """
        获取推荐趋势 (buy/hold/sell 月度变化)

        Returns:
            {periods: [{period, strongBuy, buy, hold, sell, strongSell}, ...]}
        """
        ticker = symbol.replace("US.", "").upper()
        cache_key = f"recommend_{ticker}"

        cached = self._read_cache(cache_key, CACHE_TTL_ESTIMATES)
        if cached:
            return cached

        url = f"{YAHOO_BASE_URL}/v10/finance/quoteSummary/{ticker}?modules=recommendationTrend"
        data = self._yahoo_request(url)
        if not data:
            return {}

        try:
            result = data["quoteSummary"]["result"][0]
            trend = result.get("recommendationTrend", {}).get("trend", [])
        except (KeyError, IndexError):
            return {}

        output = {"periods": []}
        for t in trend:
            output["periods"].append({
                "period": t.get("period", ""),
                "strong_buy": t.get("strongBuy", 0),
                "buy": t.get("buy", 0),
                "hold": t.get("hold", 0),
                "sell": t.get("sell", 0),
                "strong_sell": t.get("strongSell", 0),
            })

        self._write_cache(cache_key, output)
        return output

    def compute_revision_score(self, symbol: str) -> Optional[AnalystRevision]:
        """
        计算综合分析师修正分数

        评分维度:
          1. 升级/降级比 (近30天): net_revisions / max(upgrades+downgrades, 1)
          2. 共识变化 (本月 vs 上月): buy_ratio 变化
          3. EPS 增长预期: 当前季度 growth > 0 加分
          4. 分析师覆盖: ≥20 位分析师覆盖加分

        Returns:
            AnalystRevision 或 None
        """
        estimates = self.get_earnings_estimates(symbol)
        history = self.get_upgrade_downgrade_history(symbol)
        recommend = self.get_recommendation_trend(symbol)

        if not estimates:
            return None

        ticker = symbol.replace("US.", "")

        # ── 1. 近30天升级/降级 ──
        cutoff_epoch = time.time() - 30 * 86400
        recent_upgrades = 0
        recent_downgrades = 0
        for h in history:
            if h.get("epoch", 0) >= cutoff_epoch:
                action = h.get("action", "")
                if action in ("up", "main"):
                    recent_upgrades += 1
                elif action == "down":
                    recent_downgrades += 1
        net_revisions = recent_upgrades - recent_downgrades
        total_revisions = max(recent_upgrades + recent_downgrades, 1)
        revision_ratio = net_revisions / total_revisions  # -1 to +1

        # ── 2. 共识变化 ──
        consensus_shift = 0.0
        periods = recommend.get("periods", [])
        if len(periods) >= 2:
            current = periods[0]
            previous = periods[1]
            curr_total = sum(current.get(k, 0) for k in ["strong_buy", "buy", "hold", "sell", "strong_sell"])
            prev_total = sum(previous.get(k, 0) for k in ["strong_buy", "buy", "hold", "sell", "strong_sell"])
            if curr_total > 0 and prev_total > 0:
                curr_buy_ratio = (current.get("strong_buy", 0) + current.get("buy", 0)) / curr_total
                prev_buy_ratio = (previous.get("strong_buy", 0) + previous.get("buy", 0)) / prev_total
                consensus_shift = curr_buy_ratio - prev_buy_ratio  # positive = improving

        # ── 3. EPS 增长预期 ──
        eps_growth_score = 0.0
        for est in estimates:
            if est.period == "0q" and est.growth > 0:
                eps_growth_score = min(0.3, est.growth * 0.5)  # 60% growth = max 0.3
                break

        # ── 4. 分析师覆盖 ──
        coverage_score = 0.0
        for est in estimates:
            if est.period == "0q":
                if est.num_analysts >= 30:
                    coverage_score = 0.1
                elif est.num_analysts >= 20:
                    coverage_score = 0.05
                break

        # ── 综合分数 ──
        # revision_ratio: 40% weight, consensus_shift: 25%, eps_growth: 25%, coverage: 10%
        revision_score = (
            0.40 * revision_ratio +
            0.25 * min(1.0, max(-1.0, consensus_shift * 5)) +
            0.25 * eps_growth_score +
            0.10 * coverage_score
        )
        revision_score = round(max(-1.0, min(1.0, revision_score)), 3)

        # Rationale
        parts = []
        if recent_upgrades or recent_downgrades:
            parts.append(f"30d: {recent_upgrades}up/{recent_downgrades}dn")
        for est in estimates:
            if est.period == "0q":
                parts.append(f"EPS g={est.growth*100:.1f}%")
                parts.append(f"analysts={est.num_analysts}")
                break
        if consensus_shift != 0:
            parts.append(f"consensus Δ={consensus_shift:+.2f}")

        return AnalystRevision(
            symbol=symbol,
            estimates=estimates,
            recent_upgrades=recent_upgrades,
            recent_downgrades=recent_downgrades,
            net_revisions=net_revisions,
            consensus_shift=round(consensus_shift, 4),
            revision_score=revision_score,
            rationale=" | ".join(parts),
        )


def _safe_float(val, default=0.0) -> float:
    """安全转换浮点数"""
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


# Singleton
_instance: Optional[YahooAnalystProvider] = None


def get_analyst_provider() -> YahooAnalystProvider:
    global _instance
    if _instance is None:
        _instance = YahooAnalystProvider()
    return _instance
