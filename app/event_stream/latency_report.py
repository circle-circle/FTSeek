"""事件采集时延的日观测 (只读, 零交易行为变更)。

背景 (DEB-20261106-market-risk-switch-review §16.3 / §十八, 2026-11-07 落地):
盘前批次能看到的永远是"那一秒已入库"的事件。11-05 的真崩盘新闻
(`Chip selloff drags global stocks lower`) 发布时间 02:35Z, 但 `first_seen_at`
= 18:49Z —— 晚于 DA 批次 (13:35Z) 约 5 小时。所以"模型只看见平静"的根因不是
渲染层缺一行, 而是**晚到**: 当日 FINNHUB 新闻发布→入库中位 89.8 分钟, 47/68 超 60 分钟。

本模块只回答三个问题, 不回答"要不要加门":
1. 每天发布→入库的 P50/P90 与"超 60 分钟"占比 (原 get_latency_stats 只有全历史 avg/min/max,
   无法区分"今天特别慢"与"一直慢");
2. 每天有几条**市场级风险**新闻, 其中几条在 as-of 时刻 (默认 08:00 ET) 前已入库
   —— 把"无事件"与"晚到"分开, 这是 D1 类改动的必要前提;
3. 与前一日对比是否劣化 (P90 翻倍 ∧ 超窗占比 >=50%) —— 只印判断, 不触发任何交易动作。

纪律:
- 只读 URI 打开 DB (mode=ro), 不建表、不回填、不写库;
- `market_risk_hits` 是**诊断用**检测器 (市场范围词 ∧ 危机词 共现), **不是** D1 的生产词表:
  生产词表必须另行裁决 (裸 `rout` 在 11-05 命中的 3 条全是中期选举政治新闻, 见 §二十二)。
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

ET = None  # lazily bound ZoneInfo("America/New_York")

# 诊断用词表 (非生产门禁): 必须同时命中"市场范围"与"危机"两类词才算市场级风险。
_CRISIS_TOKENS: Sequence[str] = (
    "selloff", "sell-off", "sell off", "plunge", "plunges", "plunged",
    "rout", "routed", "crash", "crashes", "slump", "slumps", "tumble", "tumbles",
    "circuit breaker", "risk-off", "bear market", "correction",
)
_SCOPE_TOKENS: Sequence[str] = (
    "stock", "stocks", "share", "shares", "equit", "market", "markets", "index",
    "indexes", "indices", "futures", "wall street", "nasdaq", "s&p", "dow",
    "global", "s&p 500",
)
_OVER_WINDOW_MS = 60 * 60 * 1000


def _et_zone():
    global ET
    if ET is None:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    return ET


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def percentile(values: Sequence[float], q: float) -> float:
    """nearest-rank 分位数 (小样本安全, 不插值)。"""
    if not values:
        raise ValueError("percentile of empty sequence")
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def market_risk_hits(headlines: Iterable[str]) -> List[str]:
    """诊断用: 返回命中"市场级风险"的 headline (市场范围词 ∧ 危机词共现)。"""
    hits: List[str] = []
    for headline in headlines:
        text = (headline or "").lower()
        if any(tok in text for tok in _CRISIS_TOKENS) and any(tok in text for tok in _SCOPE_TOKENS):
            hits.append(headline)
    return hits


def daily_latency_stats(
    db_path: Path,
    days: int = 7,
    asof_hour_et: int = 8,
    now: Optional[datetime] = None,
) -> List[dict]:
    """按 ET 日聚合发布→入库时延与 as-of 可见性 (只读)。

    返回按日降序的列表, 每项:
      day, n, p50_min, p90_min, over60_n, over60_share,
      n_news (FINNHUB_GENERAL_NEWS 条数), risk_hits, risk_hits_visible
    """
    path = Path(db_path)
    if not path.exists():
        return []
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days + 2)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        rows = conn.execute(
            """SELECT first_seen_at, source_id, headline, published_to_seen_ms
               FROM canonical_events
               WHERE first_seen_at IS NOT NULL AND first_seen_at >= ?""",
            (since.strftime("%Y-%m-%dT%H:%M:%S+00:00"),),
        ).fetchall()
    finally:
        conn.close()

    buckets: Dict[str, dict] = {}
    for first_seen_at, source_id, headline, latency_ms in rows:
        seen = _parse_iso(first_seen_at)
        if seen is None:
            continue
        day = seen.astimezone(_et_zone()).date().isoformat()
        bucket = buckets.setdefault(
            day,
            {"day": day, "latencies": [], "n_news": 0, "headlines": [], "visible": []},
        )
        if latency_ms is not None:
            bucket["latencies"].append(int(latency_ms))
        if source_id == "FINNHUB_GENERAL_NEWS":
            bucket["n_news"] += 1
        bucket["headlines"].append(headline)
        cutoff = datetime.fromisoformat(day + "T00:00:00").replace(
            hour=asof_hour_et, tzinfo=_et_zone()
        ).astimezone(timezone.utc)
        if seen <= cutoff:
            bucket["visible"].append(headline)

    out: List[dict] = []
    for day in sorted(buckets, reverse=True):
        bucket = buckets[day]
        lat = bucket["latencies"]
        risk = market_risk_hits(bucket["headlines"])
        risk_visible = market_risk_hits(bucket["visible"])
        out.append({
            "day": day,
            "n": len(bucket["headlines"]),
            "latency_n": len(lat),
            "p50_min": round(percentile(lat, 0.50) / 60000.0, 1) if lat else None,
            "p90_min": round(percentile(lat, 0.90) / 60000.0, 1) if lat else None,
            "over60_n": sum(1 for ms in lat if ms > _OVER_WINDOW_MS),
            "over60_share": (sum(1 for ms in lat if ms > _OVER_WINDOW_MS) / len(lat)) if lat else None,
            "n_news": bucket["n_news"],
            "risk_hits": len(risk),
            "risk_hits_visible": len(risk_visible),
        })
    return out


def degradation_reason(today: Optional[dict], prev: Optional[dict]) -> Optional[str]:
    """时延劣化判据 (只用于把当天的读数标红, 不触发任何动作)。

    条件 (两个都要满足, 防单点抖动误报):
      ① 超窗占比 >= 50% (且当日有 >=20 条带时延样本, 小样本不判)
      ② 前一日存在且当日 P90 >= 前一日 P90 的 2 倍
    前一日缺失时返回 None (首日只印数, 不判)。
    """
    if not today or not prev:
        return None
    share = today.get("over60_share")
    p90 = today.get("p90_min")
    prev_p90 = prev.get("p90_min")
    if share is None or p90 is None or prev_p90 is None:
        return None
    if today.get("latency_n", 0) < 20:
        return None
    if share >= 0.5 and prev_p90 > 0 and p90 >= 2 * prev_p90:
        return "超窗占比%.0f%% 且 P90 %.1fmin >= 2x前日 %.1fmin" % (share * 100, p90, prev_p90)
    return None


def format_health_line(
    stats: List[dict],
    asof_hour_et: int = 8,
    now: Optional[datetime] = None,
) -> str:
    """盘前健康检查用的一行读数 (含前日对比与 as-of 可见性)。

    头条用**上一个完整 ET 日**: 盘前 07:45 跑时当天才刚开始采, 拿半天样本
    比整日必假劣化; 当天部分样本只作为尾注印条数。
    """
    if not stats:
        return "🟡 近窗无事件 (event_stream 无带时延样本)"
    current_day = (now or datetime.now(timezone.utc)).astimezone(_et_zone()).date().isoformat()
    complete = [row for row in stats if row["day"] < current_day]
    if not complete:
        return "🟡 仅有当日部分样本 %s n=%d (无可判完整日)" % (stats[0]["day"], stats[0]["n"])
    today = complete[0]
    prev = complete[1] if len(complete) > 1 else None
    parts = ["%s n=%d" % (today["day"], today["n"])]
    if today["p50_min"] is not None:
        parts.append("P50=%.0fmin P90=%.0fmin >60min=%d/%d"
                     % (today["p50_min"], today["p90_min"], today["over60_n"], today["latency_n"]))
    late = today["risk_hits"] - today["risk_hits_visible"]
    parts.append(
        "市场级风险 %d 条: as-of %02d:00 ET 前可见 %d / 晚到 %d"
        % (today["risk_hits"], asof_hour_et, today["risk_hits_visible"], late)
    )
    if prev and prev["p90_min"] is not None:
        parts.append("前日 %s P90=%.0fmin" % (prev["day"], prev["p90_min"]))
    if stats[0]["day"] == current_day:
        parts.append("当日(部分) n=%d" % stats[0]["n"])
    reason = degradation_reason(today, prev)
    head = "🔴 时延劣化: " + reason if reason else "✅ 事件采集"
    return head + " | " + " | ".join(parts)