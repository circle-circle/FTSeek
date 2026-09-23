#!/usr/bin/env python3
"""隔夜财报携带影子的量闸口径对齐 (2026-09-23)。

病: 影子固定用 `EXEC.MIN_VOLUME` (20 万) 判"能否过闸", 而生产入场闸是**按时段分档**的
(`EXEC._min_volume_now`: 盘前 04-07 → 2 万, 08-09 → 20 万) ⇒ 影子在早段把生产会放行的票
读成 `volume_below_min` (假红), 观测量与它要影子的那道闸**不同口径**。

契约 (本测试钉死):
 ① 影子实际生效的量门槛 == `EXEC._min_volume_now(session, hour)` 逐小时相等;
 ② 两个小时的生效门槛不同时, **同一份报价**的过闸结论必须随小时而变 (证明它真跟着分档走);
 ③ 静态纪律: 不得再出现扁平 `EXEC.MIN_VOLUME` 判闸 (防未来回退)。
"""
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.extended_hours_entry_executor as EXEC        # noqa: E402
import scripts.overnight_earnings_carry_shadow as SHADOW    # noqa: E402

COHORT = [{"symbol": "TESTX", "report_date": "2026-09-22", "hour": "amc",
           "eps_actual": 1.0, "eps_estimate": 0.5}]


class _FakeQuoteCtx:
    """最小券商桩: 只实现 get_market_snapshot, 永不触网络、永不触下单口。"""

    def __init__(self, rows):
        self._rows = rows

    def get_market_snapshot(self, codes):
        from futu import RET_OK
        want = set(codes)
        return RET_OK, pd.DataFrame([r for r in self._rows if r["code"] in want])


def _row(code="US.TESTX", px=50.0, prev=48.0, vol=25000, mc=2e9, today="2026-09-23"):
    return {"code": code, "prev_close_price": prev, "pre_price": px, "last_price": px,
            "pre_change_rate": round((px / prev - 1) * 100, 3), "pre_volume": vol,
            "after_price": px, "after_change_rate": 0.0, "after_volume": vol,
            "total_market_val": mc, "update_time": f"{today} 08:10:00"}


def _run(hour, vol=25000):
    items, stats = SHADOW.evaluate("premarket", COHORT, _FakeQuoteCtx([_row(vol=vol)]),
                                  "2026-09-23", hour)
    return items[0], stats


def test_applied_floor_equals_production_bucket_floor():
    for h in (4, 5, 6, 7, 8, 9):
        _, stats = _run(h)
        assert stats["et_hour"] == h
        assert stats["min_volume_applied"] == EXEC._min_volume_now("premarket", h), f"hour={h}"


def test_conclusion_tracks_bucket_when_buckets_differ():
    lo = EXEC._min_volume_now("premarket", 5)
    hi = EXEC._min_volume_now("premarket", 9)
    if lo == hi:
        return  # 未配置分档 ⇒ 无口径差, 契约 ① 已覆盖
    assert lo < hi, f"分档配置异常: 早段 {lo} 不严于晚段 {hi}"
    vol = (lo + hi) // 2
    early, _ = _run(5, vol=vol)
    late, _ = _run(9, vol=vol)
    assert early["passes_floors"] is True and late["passes_floors"] is False
    assert "volume_below_min" in late["reject"]


def test_no_flat_min_volume_usage_left():
    src = (ROOT / "scripts" / "overnight_earnings_carry_shadow.py").read_text()
    assert "vol < EXEC.MIN_VOLUME" not in src
    assert "vol >= EXEC.MIN_VOLUME" not in src
    assert "EXEC._min_volume_now" in src

def test_floors_block_does_not_self_reference_summary():
    """回归钉 (实跑 --dry 抓到): `summary` 字典构造中不得引用 `summary` (UnboundLocalError)。"""
    src = (ROOT / "scripts" / "overnight_earnings_carry_shadow.py").read_text()
    start = src.index('"floors": {')
    block = src[start:start + 400]
    assert 'summary.get(' not in block, "floors 块内自引用 summary ⇒ 构造期 UnboundLocalError"
    assert 'stats["min_volume_applied"]' in block
