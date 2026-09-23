#!/usr/bin/env python3
"""盘前「昨日 AMC」配对 (RFC-102) 的契约测试。

预注册要点: 盘前链原先只认**当日 BMO** 事件, 而当日 BMO 的 actual 在盘前窗内结构性不可得
(2026-09-23 实测: 6 只事件票 5 只到 08:10 ET 仍 actual_missing)。开
`EXT_ENTRY_PREMARKET_AMC_PAIRING` 后盘前同时接受**昨日 AMC** (actual 次日早晨已入库)。

本测试钉住的契约:
 ① 开关默认关 ⇒ 配对表 = [(0,"before")] (生产行为不变);
 ② 开关开 ⇒ 表追加 (-1,"after"), 且**昨日 AMC 行被接受 / 当日 BMO 仍被接受 / 两日前 AMC 被拒**;
 ③ 判据② 读**事件所属日**的 Finnhub 行 (不是今日行) —— 串日是最危险的失败模式;
 ④ 昨日行缺 actual ⇒ fail-closed (绝不因"换了一天"就放松三态)。
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.extended_hours_entry_executor as EXEC  # noqa: E402

TODAY = EXEC._today_et()
YDAY = EXEC._shift_et_day(TODAY, -1)
DBYDAY = EXEC._shift_et_day(TODAY, -2)


@pytest.fixture
def pairing_on(monkeypatch):
    monkeypatch.setattr(EXEC, "PREMARKET_AMC_PAIRING", True)


@pytest.fixture
def pairing_off(monkeypatch):
    monkeypatch.setattr(EXEC, "PREMARKET_AMC_PAIRING", False)


def _rows(*spec):
    """spec: (symbol, reportDate, phase_string)"""
    # 注意: 日历行的 symbol 是**裸 ticker** (生产契约: futu `security[3:]`, AV 同形) ——
    # 带 "US." 前缀会让匹配落空 (本测试初版就踩了这个, 实跑复现后才改对)。
    return [{"symbol": s, "reportDate": d, "timeOfTheDay": p} for s, d, p in spec]


def _stub_rows(monkeypatch, rows):
    monkeypatch.setattr(EXEC, "_earnings_calendar_rows", lambda: (rows, None))


def _stub_cal(monkeypatch, by_day):
    monkeypatch.setattr(EXEC, "_finnhub_today_calendar",
                        lambda day=None: (by_day.get(day or TODAY, {}), None))


def test_default_off_and_pairing_tables():
    import os
    assert EXEC.PREMARKET_AMC_PAIRING is False, "开关必须默认关 (生产行为不变)"
    assert os.getenv("EXT_ENTRY_PREMARKET_AMC_PAIRING") in (None, "", "false")
    assert EXEC._session_event_pairs("premarket") == [(0, "before")]
    assert EXEC._session_event_pairs("afterhours") == [(0, "after")]


def test_pairs_include_yesterday_amc_when_on(pairing_on):
    assert EXEC._session_event_pairs("premarket") == [(0, "before"), (-1, "after")]
    assert EXEC._session_event_pairs("afterhours") == [(0, "after")], "盘后不得被本开关影响"


def test_shift_et_day_is_failsafe():
    assert EXEC._shift_et_day(TODAY, -1) == YDAY
    assert EXEC._shift_et_day("not-a-date", -1) == "not-a-date"


def test_event_matching_matrix(monkeypatch):
    rows = _rows(("BMOT", TODAY, "pre-market"),
                 ("AMCY", YDAY, "post-market"),
                 ("AMC2", DBYDAY, "post-market"),
                 ("MISM", TODAY, "post-market"))
    _stub_rows(monkeypatch, rows)
    monkeypatch.setattr(EXEC, "EARNINGS_EVENT_SOURCE", "futu_only")

    # 注: **日期不在配对表内** 的行走日历"缺失"归因 (老行为, 未被本 RFC 改变) ——
    # 只有"同日期 + 相位不符"才是 phase_mismatch。台账口径据此不变。
    monkeypatch.setattr(EXEC, "PREMARKET_AMC_PAIRING", False)
    assert EXEC._earnings_calendar_event("US.BMOT", "premarket")[0] is not None
    assert EXEC._earnings_calendar_event("US.AMCY", "premarket")[1] == "calendar_missing", \
        "关: 昨日 AMC 行对盘前不可见 (与线上一致)"
    assert EXEC._earnings_calendar_event("US.MISM", "premarket")[1] == "calendar_phase_mismatch", \
        "同日期但相位不符 ⇒ mismatch"

    monkeypatch.setattr(EXEC, "PREMARKET_AMC_PAIRING", True)
    assert EXEC._earnings_calendar_event("US.BMOT", "premarket")[0] is not None, "当日 BMO 不得失效"
    ev, reason = EXEC._earnings_calendar_event("US.AMCY", "premarket")
    assert reason is None and ev["reportDate"] == YDAY, "昨日 AMC 应被接受且带自身日期"
    assert EXEC._earnings_calendar_event("US.AMC2", "premarket")[1] == "calendar_missing", \
        "两日前的 AMC 不得被接受 (只认昨日)"
    assert EXEC._earnings_calendar_event("US.MISM", "premarket")[1] == "calendar_phase_mismatch"


def test_actual_is_read_from_event_day(monkeypatch, pairing_on):
    _stub_rows(monkeypatch, _rows(("AMCY", YDAY, "post-market")))
    monkeypatch.setattr(EXEC, "EARNINGS_EVENT_SOURCE", "futu_only")
    seen = []

    def _cal(day=None):
        seen.append(day)
        if day == YDAY:
            return {"AMCY": {"symbol": "AMCY", "hour": "amc", "epsActual": 1.0, "epsEstimate": 0.5,
                             "quarter": 4, "year": 2026}}, None
        return {"AMCY": {"symbol": "AMCY", "hour": "amc", "epsActual": None, "epsEstimate": 0.5}}, None

    monkeypatch.setattr(EXEC, "_finnhub_today_calendar", _cal)
    monkeypatch.setattr(EXEC, "EARNINGS_CRITERION", "calendar_actual")
    monkeypatch.setattr(EXEC, "EARNINGS_CRITERION_SHADOW", False)
    res, reason = EXEC._earnings_surprise("US.AMCY", "premarket")
    assert reason is None and res and res["surprise_pct"] == pytest.approx(100.0)
    assert YDAY in seen, f"actual 必须读事件日 ({YDAY}) 的行; 实读 {seen}"


def test_no_actual_on_any_pairing_day_fails_closed(monkeypatch, pairing_on):
    """真 fail-closed: 配对表内**所有**日期都没有 actual ⇒ 必须拒 (不得凭空放行)。"""
    _stub_rows(monkeypatch, _rows(("AMCY", YDAY, "post-market")))
    monkeypatch.setattr(EXEC, "EARNINGS_EVENT_SOURCE", "futu_only")
    _stub_cal(monkeypatch, {TODAY: {"AMCY": {"hour": "bmo", "epsActual": None, "epsEstimate": 0.5}},
                            YDAY: {"AMCY": {"hour": "amc", "epsActual": None, "epsEstimate": 0.5}}})
    monkeypatch.setattr(EXEC, "EARNINGS_CRITERION", "calendar_actual")
    monkeypatch.setattr(EXEC, "EARNINGS_CRITERION_SHADOW", False)
    res, reason = EXEC._earnings_surprise("US.AMCY", "premarket")
    assert res is None and reason == "actual_missing"


def test_union_is_symmetric_and_auditable(monkeypatch, pairing_on):
    """源间日期分歧的另一方向 (futu 记昨日 AMC, Finnhub 只在今日行有 actual):
    按 §2.6 的并源语义应当放行, 但**必须**在结果里留下 actual_day 供审计。"""
    _stub_rows(monkeypatch, _rows(("AMCY", YDAY, "post-market")))
    monkeypatch.setattr(EXEC, "EARNINGS_EVENT_SOURCE", "futu_only")
    _stub_cal(monkeypatch, {TODAY: {"AMCY": {"hour": "bmo", "epsActual": 9.9, "epsEstimate": 0.5}},
                            YDAY: {"AMCY": {"hour": "amc", "epsActual": None, "epsEstimate": 0.5}}})
    monkeypatch.setattr(EXEC, "EARNINGS_CRITERION", "calendar_actual")
    monkeypatch.setattr(EXEC, "EARNINGS_CRITERION_SHADOW", False)
    res, reason = EXEC._earnings_surprise("US.AMCY", "premarket")
    assert reason is None and res["actual_day"] == TODAY and res["event_date"] == YDAY


def test_finnhub_fallback_walks_pair_table(monkeypatch, pairing_on):
    _stub_rows(monkeypatch, [])  # futu 无行 ⇒ 走 union 回落
    monkeypatch.setattr(EXEC, "EARNINGS_EVENT_SOURCE", "calendar_finnhub_union")
    _stub_cal(monkeypatch, {YDAY: {"AMCY": {"hour": "amc"}}, TODAY: {"BMOT": {"hour": "bmo"}}})
    ev, reason = EXEC._earnings_calendar_event("US.AMCY", "premarket")
    assert reason is None and ev["reportDate"] == YDAY and ev["_source"] == "finnhub"
    ev2, reason2 = EXEC._earnings_calendar_event("US.BMOT", "premarket")
    assert reason2 is None and ev2["reportDate"] == TODAY
    # 相位不符 ⇒ 仍按 mismatch (且不得因遍历多日而变成 unknown/missing)
    _stub_cal(monkeypatch, {TODAY: {"X": {"hour": "amc"}}})
    assert EXEC._earnings_calendar_event("US.X", "premarket")[1] == "calendar_phase_mismatch"

# ── C 段: 判据② 的「取数日并源」(RFC-102 §2.6) ──────────────────────────────
# 真实案例 (2026-09-23): futu 把 WOR 记成"当日 BMO", 而 Finnhub(9/22 行) 与 Nasdaq(9/22 日历)
# 一致记为 9/22 AMC (actual 0.82 vs est 0.7575 ≈ +8.3%) ⇒ 老逻辑取"事件日=今日"的 actual 落空,
# 把 "+8% beat + 盘前 +19% 跳空" 的票读成 actual_missing 拦掉。


def _cal_with_actual(day, ticker, actual, est=0.7575, hour="amc"):
    return {day: {ticker: {"symbol": ticker, "hour": hour, "epsActual": actual,
                           "epsEstimate": est, "quarter": 1, "year": 2027}}}


def test_actual_union_falls_back_within_pairing_days(monkeypatch, pairing_on):
    _stub_rows(monkeypatch, _rows(("WOR", TODAY, "pre-market")))     # futu: 当日 BMO (源间分歧)
    monkeypatch.setattr(EXEC, "EARNINGS_EVENT_SOURCE", "futu_only")
    _stub_cal(monkeypatch, _cal_with_actual(YDAY, "WOR", 0.82))      # 昨日行才有 actual
    res, reason = EXEC._actual_with_day_union("WOR", "premarket", TODAY)
    assert res is not None and reason is None
    assert res["actual_day"] == YDAY and res["event_date"] == TODAY
    assert res["surprise_pct"] == pytest.approx((0.82 - 0.7575) / 0.7575 * 100, rel=1e-6)


def test_union_does_not_leak_outside_pairing_days(monkeypatch, pairing_on):
    _stub_cal(monkeypatch, _cal_with_actual(DBYDAY, "WOR", 9.9))     # 两日前有 actual ⇒ 不得采用
    res, reason = EXEC._actual_with_day_union("WOR", "premarket", TODAY)
    assert res is None


def test_union_off_is_single_day(monkeypatch, pairing_off):
    _stub_cal(monkeypatch, _cal_with_actual(YDAY, "WOR", 0.82))
    res, _reason = EXEC._actual_with_day_union("WOR", "premarket", TODAY)
    assert res is None, "关: 昨日 actual 不得被采用 (单日 ⇒ 老行为等价)"


def test_union_prefers_event_day_first(monkeypatch, pairing_on):
    cal = _cal_with_actual(TODAY, "WOR", 0.90)
    cal[YDAY] = {"WOR": {"hour": "amc", "epsActual": 0.10, "epsEstimate": 0.75}}
    _stub_cal(monkeypatch, cal)
    res, _reason = EXEC._actual_with_day_union("WOR", "premarket", TODAY)
    assert res["actual_day"] == TODAY, "事件日优先, 不得被兜底日覆盖"


def test_shadow_ledger_records_attribution_fields():
    src = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()
    assert '"report_date": event_date' in src, "台账必须记录事件日 (跨日错位审计的前提)"
    assert '"actual_day": (new_res or {}).get("actual_day")' in src, "台账必须记录 actual 取数日"
