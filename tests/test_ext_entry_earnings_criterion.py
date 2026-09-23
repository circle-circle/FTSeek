#!/usr/bin/env python3
"""判据②口径修正 (RFC-097) 回归测试: 当日 actual vs est + 三态 fail-close + 涨幅带 + 对照台账。

背景: 旧判据②把 company_earnings[0].period (Finnhub 归一化的**日历**季度末) 当"披露新鲜度"
锚 ⇒ off-calendar 财年票被判 age_days < -7 = **当日真 beat 被拒**。
生产活例: US.KBH (actual 1.05 / est 0.9022 / +16.38%, period='2026-09-30' age_days=-8)
于 2026-09-22 盘后被闸②拒 (logs/ext_entry.log:220)。DEB-20260923 §2.1/§14.3, RFC-097。
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = "2026-09-22"          # 固定 ET 日, 避免跨日 flake
CAL_ROWS = [{"symbol": "KBH", "reportDate": TODAY, "timeOfTheDay": "post-market"},
            {"symbol": "AZO", "reportDate": TODAY, "timeOfTheDay": "pre-market"}]
KBH_COMPANY = [{"symbol": "KBH", "period": "2026-09-30", "surprisePercent": 16.3822,
                "actual": 1.05, "estimate": 0.9022}]


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "ext_entry_exec_criterion", ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeClient(object):
    def __init__(self, cal_rows, company_rows):
        self._cal, self._company = cal_rows, company_rows

    def earnings_calendar(self, _from, to, symbol, international=False):
        return {"earningsCalendar": self._cal, "to": to}

    def company_earnings(self, symbol, limit=None):
        return self._company


def _wire(mod, monkeypatch, cal_rows, company_rows=None, tmp_path=None, shadow=True):
    import app.data.premarket_enrichment as pe
    monkeypatch.setattr(pe, "_get_finnhub_client",
                        lambda: _FakeClient(cal_rows, company_rows or KBH_COMPANY))
    monkeypatch.setattr(mod, "_earnings_calendar_rows", lambda: (CAL_ROWS, None))
    monkeypatch.setattr(mod, "_today_et", lambda: TODAY)
    monkeypatch.setattr(mod, "_FINNHUB_TODAY_CAL_CACHE", {})
    monkeypatch.setattr(mod, "EARNINGS_CRITERION", "calendar_actual")
    monkeypatch.setattr(mod, "EARNINGS_CRITERION_SHADOW", shadow)
    if tmp_path is not None:
        monkeypatch.setattr(mod, "EARNINGS_CRITERION_SHADOW_PATH", tmp_path / "shadow.jsonl")
    monkeypatch.setattr(mod, "_EARNINGS_CRITERION_SHADOW_KEYS", None)


def _today_rows(sym, actual, est):
    row = {"symbol": sym, "hour": "amc"}
    if actual is not None:
        row["epsActual"] = actual
    if est is not None:
        row["epsEstimate"] = est
    return [row]


def test_kbh_future_period_now_passes(monkeypatch):
    """P0 回归锚: period 在未来 (旧判据必拦) + 当日 actual 打赢预期 ⇒ 新判据放行。"""
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 1.05, 0.9022))
    got, reason = mod._earnings_surprise("US.KBH", "afterhours")
    assert reason is None and got is not None, (got, reason)
    assert abs(got["surprise_pct"] - 16.3822) < 0.01
    assert got["actual"] == 1.05 and got["estimate"] == 0.9022
    assert got["report_date"] == TODAY and got["report_phase"] == "amc"


def test_legacy_path_still_blocks_kbh(monkeypatch):
    """回滚路径语义未变: legacy_period 下同一票仍被 period 龄窗拦 (surprise_stale)。"""
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 1.05, 0.9022))
    monkeypatch.setattr(mod, "EARNINGS_CRITERION", "legacy_period")
    got, reason = mod._earnings_surprise("US.KBH", "afterhours")
    assert got is None and reason == "surprise_stale", (got, reason)


def test_miss_is_rejected(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 0.80, 0.9022))
    got, reason = mod._earnings_surprise("US.KBH", "afterhours")
    assert got is None and reason == "surprise_nonpositive", (got, reason)


def test_estimate_zero_and_missing_fail_close(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 1.05, 0))
    assert mod._earnings_surprise("US.KBH", "afterhours")[1] == "estimate_zero"
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 1.05, None))
    assert mod._earnings_surprise("US.KBH", "afterhours")[1] == "estimate_missing"
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 1.05, "0.9"))
    assert mod._earnings_surprise("US.KBH", "afterhours")[1] == "estimate_missing"


def test_actual_missing_fail_close(monkeypatch):
    """当日日历无此票 / 今日 actual 尚未入库 ⇒ fail-closed (不得放行)。"""
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", None, 0.9022))
    assert mod._earnings_surprise("US.KBH", "afterhours")[1] == "actual_missing"
    mod = _load()
    _wire(mod, monkeypatch, [{"symbol": "OTHER", "epsActual": 1.0, "epsEstimate": 0.5}])
    assert mod._earnings_surprise("US.KBH", "afterhours")[1] == "actual_missing"


def test_calendar_source_failure_still_fail_closed(monkeypatch):
    mod = _load()
    _wire(mod, monkeypatch, [])
    monkeypatch.setattr(mod, "_earnings_calendar_rows", lambda: ([], "boom"))
    assert mod._earnings_surprise("US.KBH", "afterhours")[1] == "calendar_source_error"


def test_phase_gate_unchanged(monkeypatch):
    """判据① (同日同相位) 语义零变更: 盘前 session 对盘后财报仍拒。"""
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 1.05, 0.9022))
    assert mod._earnings_surprise("US.KBH", "premarket")[1] == "calendar_phase_mismatch"


def test_entry_chg_band(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "ENTRY_CHG_BAND", "")
    monkeypatch.setattr(mod, "ENTRY_CHANGE_PCT", 2.0)
    assert [mod._entry_chg_ok(x) for x in (1.0, 2.0, 3.0)] == [False, True, True]
    monkeypatch.setattr(mod, "ENTRY_CHG_BAND", "0.5,2.0")
    assert [mod._entry_chg_ok(x) for x in (0.4, 0.5, 2.0, 2.1)] == [False, True, True, False]
    monkeypatch.setattr(mod, "ENTRY_CHG_BAND", "bad-config")
    assert [mod._entry_chg_ok(x) for x in (1.0, 2.0)] == [False, True]  # fail-safe 回旧口径


def test_shadow_ledger_dedupes_per_symbol_day(monkeypatch, tmp_path):
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 1.05, 0.9022), tmp_path=tmp_path)
    mod._earnings_surprise("US.KBH", "afterhours")
    mod._earnings_surprise("US.KBH", "afterhours")
    rows = [json.loads(line) for line in (tmp_path / "shadow.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) == 1, rows
    assert rows[0]["legacy_blocked_new_pass"] is True
    assert rows[0]["legacy_reason"] == "surprise_stale"
    assert rows[0]["new_pass"] is True


def test_no_shadow_write_when_disabled(monkeypatch, tmp_path):
    mod = _load()
    _wire(mod, monkeypatch, _today_rows("KBH", 1.05, 0.9022), tmp_path=tmp_path, shadow=False)
    mod._earnings_surprise("US.KBH", "afterhours")
    assert not (tmp_path / "shadow.jsonl").exists()