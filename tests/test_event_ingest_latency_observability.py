"""事件采集时延观测 (RFC-189 / DEB-20261106 §16.3 F1-b) 的承重测试。

守三件 (缺一件 = 观测仪会静默给出错误结论):
1. 诊断检测器必须**分开**政治 `rout` 与市场 `selloff` —— 11-05 的 3 条 "rout" 全是
   中期选举新闻, 裸词表会把它们报成"市场级风险";
2. ET 日归属 + as-of(08:00 ET) 可见性切分: 同一批 headline 里"已入库/晚到"必须按
   first_seen_at 与当日 08:00 ET 的比较来定, 而不是按发布时间;
3. 劣化判据必须两侧条件都满足才标红, 且小样本/无前日时不判 (防每日假警报)。

全部在临时库上跑, 不触碰生产 data/event_stream.db。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.event_stream.latency_report import (  # noqa: E402
    daily_latency_stats,
    degradation_reason,
    format_health_line,
    market_risk_hits,
)
from app.event_stream.store import EventStore  # noqa: E402

# 真实 headline (2026-11-05/11-06 event_stream 原样)
ELECTION_ROUT = [
    "After Republicans routed, Trump devotes first post-midterm event to White House ballroom - Reuters",
    "COMMENTARY: Middle East to pay for Trump's midterm rout - Reuters",
    "Here's what the Democratic election rout could mean for the U.S. economy",
]
MARKET_RISK = [
    "Chip selloff drags global stocks lower on AI spending concerns - Reuters",
    "Markets fret over growth and inflation, just as tech rout gains speed - Reuters",
    "Nasdaq drops into correction territory - here's what typically happens next",
]


def _make_store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "event_stream.db")


def _insert(store: EventStore, rows):
    conn = sqlite3.connect(store.path)
    try:
        for idx, (first_seen_at, latency_ms, headline, source_id) in enumerate(rows):
            conn.execute(
                """INSERT INTO canonical_events
                   (event_id, source_id, source_tier, source_url, event_type, event_time,
                    headline, summary, facts_json, status, confidence, reasoning_version,
                    first_seen_at, published_to_seen_ms, event_meta_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "e%d" % idx, source_id, "T1", "http://x/%d" % idx, "NEWS_WIRE",
                    first_seen_at, headline, None, "{}", "ACTIVE", 0.9, "v1",
                    first_seen_at, latency_ms, None,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_market_risk_detector_separates_election_from_selloff():
    assert market_risk_hits(ELECTION_ROUT) == []
    assert len(market_risk_hits(MARKET_RISK)) == 3
    # 共现要求: 只有危机词或只有范围词都不算
    assert market_risk_hits(["Tesla rout continues"]) == []
    assert market_risk_hits(["Global stocks mostly higher"]) == []


def test_daily_stats_et_day_and_asof_visibility(tmp_path):
    store = _make_store(tmp_path)
    # 2026-11-05 是 EST (UTC-5): 08:00 ET = 13:00Z
    _insert(store, [
        ("2026-11-05T12:09:05+00:00", 30 * 60000,
         "Asia fuel oil benchmarks slump as market prices in easing", "FINNHUB_GENERAL_NEWS"),
        ("2026-11-05T18:49:08+00:00", 89 * 60000,
         "Chip selloff drags global stocks lower", "FINNHUB_GENERAL_NEWS"),
        ("2026-11-05T22:09:07+00:00", 200 * 60000,
         "Nasdaq drops into correction territory", "FINNHUB_GENERAL_NEWS"),
        # UTC 11-06 但也属 ET 11-05 (22:09 ET = 03:09Z 次日) 的样本不存在, 这里另测边界:
        ("2026-11-06T02:00:00+00:00", 15 * 60000,
         "Overnight piece without market scope", "RSSHUB_YAHOO"),
    ])
    stats = daily_latency_stats(store.path, days=7)
    by_day = {row["day"]: row for row in stats}
    # 2026-11-06T02:00Z = 11-05 21:00 ET ⇒ 归 ET 11-05, 不是 11-06
    assert set(by_day) == {"2026-11-05"}
    day = by_day["2026-11-05"]
    assert day["n"] == 4
    assert day["n_news"] == 3
    assert day["risk_hits"] == 3          # fuel oil(slump∧market) + chip selloff + nasdaq correction
    assert day["risk_hits_visible"] == 1  # 只有 12:09Z 那条早于 13:00Z 截止
    assert day["over60_n"] == 2           # 89min / 200min
    assert day["p50_min"] == 30.0


def test_degradation_reason_requires_both_conditions_and_enough_samples():
    prev = {"p90_min": 100.0}
    assert degradation_reason({"over60_share": 0.6, "p90_min": 250.0, "latency_n": 100}, prev)
    # 只有超窗占比高, P90 没翻倍 → 不判
    assert degradation_reason({"over60_share": 0.6, "p90_min": 120.0, "latency_n": 100}, prev) is None
    # P90 翻倍但超窗占比低 → 不判
    assert degradation_reason({"over60_share": 0.2, "p90_min": 400.0, "latency_n": 100}, prev) is None
    # 小样本 → 不判
    assert degradation_reason({"over60_share": 0.9, "p90_min": 900.0, "latency_n": 5}, prev) is None
    # 无前日 → 只印数不判
    assert degradation_reason({"over60_share": 0.9, "p90_min": 900.0, "latency_n": 100}, None) is None


def test_health_line_uses_last_complete_et_day(tmp_path):
    stats = [
        {"day": "2026-11-07", "n": 11, "latency_n": 11, "p50_min": 59.1, "p90_min": 2358.0,
         "over60_n": 5, "over60_share": 0.45, "n_news": 3, "risk_hits": 0, "risk_hits_visible": 0},
        {"day": "2026-11-06", "n": 848, "latency_n": 848, "p50_min": 19.2, "p90_min": 487.3,
         "over60_n": 252, "over60_share": 0.30, "n_news": 48, "risk_hits": 3, "risk_hits_visible": 2},
    ]
    now = datetime(2026, 11, 7, 12, 45, tzinfo=timezone.utc)  # 07:45 ET
    line = format_health_line(stats, now=now)
    assert "2026-11-06" in line and "2026-11-05" not in line
    assert "晚到 1" in line
    assert "当日(部分) n=11" in line
    # 劣化判据用完整日, 不拿当日半天样本比 (45% 超窗 vs 30% 前日 ⇒ 不标红)
    assert line.startswith("✅")