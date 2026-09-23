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

# ── 覆盖补强源: 当日 Finnhub 日历 → 候选池 (2026-09-23 供给归因) ──────────────
executor = _load()


def _fh_cal(monkeypatch, by_ticker):
    monkeypatch.setattr(executor, "_finnhub_today_calendar", lambda: (by_ticker, None))
    # 日期锚定 (与上方 _wire 用例同法): `_finnhub_calendar_symbols` 内部会实读
    # `_today_et()` 与行内 date 双保险比对 —— 不钉死则隔日跑即把"当日行"判成跨日行 (2026-09-23 实测 2 红)。
    monkeypatch.setattr(executor, "_today_et", lambda: TODAY)


def test_finnhub_source_phase_mapping(monkeypatch):
    """bmo → 盘前窗; amc → 盘后窗; dmh(盘中) 与未标定不进延长时段池。"""
    _fh_cal(monkeypatch, {
        "CTAS": {"hour": "bmo", "date": TODAY, "epsEstimate": 1.38},
        "FUL": {"hour": "amc", "date": TODAY, "epsEstimate": 1.47},
        "DMH1": {"hour": "dmh", "date": TODAY, "epsEstimate": 1.0},
        "NONE": {"hour": None, "date": TODAY, "epsEstimate": 1.0},
    })
    assert executor._finnhub_calendar_symbols("premarket") == ["US.CTAS"]
    assert executor._finnhub_calendar_symbols("afterhours") == ["US.FUL"]


def test_finnhub_source_skips_other_dates(monkeypatch):
    """端点契约可能漂 (返回跨日行) ⇒ 非当日行必须挡掉。"""
    _fh_cal(monkeypatch, {"CTAS": {"hour": "bmo", "date": "2026-09-24"}})
    assert executor._finnhub_calendar_symbols("premarket") == []


def test_finnhub_source_fails_open_on_error(monkeypatch, capsys):
    """源故障 ⇒ 跳过本源 (不影响其他源), 且留显式日志 (禁静默)。"""
    monkeypatch.setattr(executor, "_finnhub_today_calendar", lambda: (None, "finnhub_error"))
    assert executor._finnhub_calendar_symbols("premarket") == []
    assert "不可用" in capsys.readouterr().out


def test_finnhub_source_does_not_widen_eligibility(monkeypatch):
    """**候选源零资格变更**的机械保证: 只产候选代码, 判据① 仍只认 futu/AV 日历。
    注: 必须显式钉 `EARNINGS_EVENT_SOURCE=calendar` —— 生产 `.env.us` 已开并源(union),
    那会**故意**让 CTAS 类票过判据① (见下方 union 用例组); 本用例只验候选源那一层的独立性。"""
    _fh_cal(monkeypatch, {"CTAS": {"hour": "bmo", "date": TODAY}})
    monkeypatch.setattr(executor, "EARNINGS_EVENT_SOURCE", "calendar")
    assert executor._finnhub_calendar_symbols("premarket") == ["US.CTAS"]
    monkeypatch.setattr(executor, "_earnings_calendar_rows", lambda: ([], None))   # futu/AV 无 CTAS
    assert executor._earnings_surprise("US.CTAS", "premarket") == (None, "calendar_missing")


# ── 判据① 事件源并源 (union 模式, 2026-09-23「先可见再打磨」) ──────────────────
def _wire_union(mod, monkeypatch, futu_rows, fh_rows, tmp_path, session_case="premarket"):
    _wire(mod, monkeypatch, fh_rows, tmp_path=tmp_path)
    monkeypatch.setattr(mod, "_earnings_calendar_rows", lambda: (futu_rows, None))
    monkeypatch.setattr(mod, "EARNINGS_EVENT_SOURCE", "calendar_finnhub_union")
    return session_case


def test_union_accepts_finnhub_only_event(monkeypatch, tmp_path):
    """futu 日历**根本没有这只票** + Finnhub 当日标 bmo ⇒ 盘前窗放行 (并源的核心价值)。"""
    mod = _load()
    sess = _wire_union(mod, monkeypatch, [], [{"symbol": "CTAS", "hour": "bmo",
                                               "epsActual": 2.1, "epsEstimate": 1.38}], tmp_path)
    got, reason = mod._earnings_surprise("US.CTAS", sess)
    assert reason is None and got is not None, (got, reason)
    assert got["report_date"] == TODAY and got["report_phase"] in ("bmo", "pre-market")
    assert "CTAS" in mod._EVENT_SRC_OBS["finnhub"]


def test_union_accepts_when_futu_phase_unlabeled(monkeypatch, tmp_path):
    """病灶原型 US.CTAS: futu 标 REGULAR (相位空) 而 Finnhub 标 bmo ⇒ 并源放行。"""
    mod = _load()
    futu = [{"symbol": "CTAS", "reportDate": TODAY, "timeOfTheDay": "", "pub_type": "REGULAR"}]
    sess = _wire_union(mod, monkeypatch, futu, [{"symbol": "CTAS", "hour": "bmo",
                                                 "epsActual": 2.1, "epsEstimate": 1.38}], tmp_path)
    got, reason = mod._earnings_surprise("US.CTAS", sess)
    assert reason is None and got is not None, (got, reason)


def test_union_does_not_override_futu_mismatch(monkeypatch, tmp_path):
    """futu **明确**标了不同相位 ⇒ 不覆盖 (保守): afterhours 票在盘前窗仍拒。"""
    mod = _load()
    futu = [{"symbol": "CBRL", "reportDate": TODAY, "timeOfTheDay": "post-market"}]
    sess = _wire_union(mod, monkeypatch, futu, [{"symbol": "CBRL", "hour": "bmo",
                                                 "epsActual": 2.1, "epsEstimate": 1.38}], tmp_path)
    got, reason = mod._earnings_surprise("US.CBRL", sess)
    assert got is None and reason == "calendar_phase_mismatch", (got, reason)
    assert not mod._EVENT_SRC_OBS["finnhub"]


def test_union_off_keeps_fail_close(monkeypatch):
    """开关关闭 (默认) ⇒ 逐字保留旧语义 (futu 无行 = calendar_missing)。"""
    mod = _load()
    _wire(mod, monkeypatch, [{"symbol": "CTAS", "hour": "bmo",
                              "epsActual": 2.1, "epsEstimate": 1.38}])
    monkeypatch.setattr(mod, "_earnings_calendar_rows", lambda: ([], None))
    monkeypatch.setattr(mod, "EARNINGS_EVENT_SOURCE", "calendar")
    got, reason = mod._earnings_surprise("US.CTAS", "premarket")
    assert got is None and reason == "calendar_missing", (got, reason)


def test_union_finnhub_down_fails_closed(monkeypatch, tmp_path):
    """并源的兜底源故障 ⇒ fail-close (calendar_source_error), 不回退成放行。"""
    mod = _load()
    _wire_union(mod, monkeypatch, [], [], tmp_path)
    import app.data.premarket_enrichment as pe
    monkeypatch.setattr(pe, "_get_finnhub_client", lambda: None)
    monkeypatch.setattr(mod, "_FINNHUB_TODAY_CAL_CACHE", {})
    got, reason = mod._earnings_surprise("US.CTAS", "premarket")
    assert got is None and reason == "calendar_source_error", (got, reason)
