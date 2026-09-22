#!/usr/bin/env python3
"""earnings 闸台账的 `coverage` 块 (2026-09-22 零成交归因缺口, 见 DEB-20260920 §10.10)。

背景(实测): 2026-09-21/22 两个交易日延长时段 0 成交, 台账 421 条**全部**是
`calendar_missing`。该桶同时装着三件不同的事:
  (a) 财报季空窗 (当日全市场仅 6-18 家披露);
  (b) 日历源当日覆盖过窄 (主源 futu 当日 6 只);
  (c) 候选池与事件池**不相交** (异动榜 50 只 ∩ futu∩AV 日历 = ∅)。
不补这组读数, 三者不可区分 —— 当天必须手工跑三方交集探针才能定性, 而探针不落盘,
下次归零又要重跑。本文件钉住 coverage 的字段语义与异常安全。
"""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(
        "ext_entry_exec_cov", ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CAL_ROWS = [
    # 主源 futu: 今日 3 只 (2 盘前 1 盘后)
    {"symbol": "AZO", "reportDate": "2026-09-22", "timeOfTheDay": "pre-market", "source": "futu"},
    {"symbol": "THO", "reportDate": "2026-09-22", "timeOfTheDay": "pre-market", "source": "futu"},
    {"symbol": "KBH", "reportDate": "2026-09-22", "timeOfTheDay": "post-market", "source": "futu"},
    # 备源 AV: 今日 1 只盘前 + 1 只相位空 (不得计入同相位)
    {"symbol": "BRTX", "reportDate": "2026-09-22", "timeOfTheDay": "pre-market", "source": "av"},
    {"symbol": "NAII", "reportDate": "2026-09-22", "timeOfTheDay": "", "source": "av"},
    # 非今日行不得计入
    {"symbol": "XXXX", "reportDate": "2026-09-23", "timeOfTheDay": "pre-market", "source": "futu"},
]


def _arm(mod, tmp_path, monkeypatch, rows=CAL_ROWS, error=None):
    ledger = tmp_path / "earnings_gate_rejects.jsonl"
    monkeypatch.setattr(mod, "EARNINGS_GATE_REJECTS", ledger)
    monkeypatch.setattr(mod, "_earnings_calendar_rows", lambda: (rows, error))
    return ledger


def _last_row(ledger):
    return json.loads(ledger.read_text().strip().splitlines()[-1])


def test_coverage_reports_calendar_size_and_intersection(tmp_path, monkeypatch):
    """coverage 必须分辨"日历规模"与"候选∩日历交集" —— 这正是 calendar_missing 归不了因的那层。"""
    mod = _load()
    ledger = _arm(mod, tmp_path, monkeypatch)

    mod._record_earnings_gate_rejects(
        "premarket", "2026-09-22", {"calendar_missing": 3}, [],
        candidates=["US.IMCC", "US.GRML", "US.AZO", "US.XXXXX"],
    )
    row = _last_row(ledger)
    assert row["counts"] == {"calendar_missing": 3}          # 原有字段一字不变
    cov = row["coverage"]
    assert cov["calendar_rows_today"] == 5                    # 今日 5 行 (非今日行不计)
    assert cov["calendar_by_source"] == {"futu": 3, "av": 2}
    assert cov["calendar_same_phase"] == 3                    # 盘前 2 + AV 盘前 1 (空相位不计)
    assert cov["candidates"] == 4
    assert cov["candidates_in_calendar"] == 1                 # 只有 AZO 在册
    assert cov["candidates_in_calendar_same_phase"] == 1      # 且是盘前相位
    assert cov["calendar_source_error"] is None


def test_coverage_distinguishes_seasonal_drought_from_pool_mismatch(tmp_path, monkeypatch):
    """两种"归零"必须读数可分: 空日历 (季空窗) vs 有日历但交集为 0 (口径互斥)。"""
    mod = _load()
    # 情形一: 季空窗 —— 今日日历为空
    ledger = _arm(mod, tmp_path, monkeypatch, rows=[])
    mod._record_earnings_gate_rejects("premarket", "2026-09-22", {"calendar_missing": 20}, [],
                                      candidates=["US.IMCC"])
    cov = _last_row(ledger)["coverage"]
    assert (cov["calendar_rows_today"], cov["candidates_in_calendar"]) == (0, 0)

    # 情形二: 日历非空但与候选不相交 (2026-09-22 实况)
    _arm(mod, tmp_path, monkeypatch)          # 换回非空日历 (同一 ledger 追加一行)
    mod._record_earnings_gate_rejects("premarket", "2026-09-22", {"calendar_missing": 20}, [],
                                      candidates=["US.IMCC"])
    cov = _last_row(ledger)["coverage"]
    assert cov["calendar_rows_today"] == 5 and cov["candidates_in_calendar"] == 0


def test_coverage_is_optional_and_never_breaks_the_ledger(tmp_path, monkeypatch):
    """candidates 缺席 / 日历源抛异常 ⇒ 台账照写 (观测面不得影响交易主流程)。"""
    mod = _load()
    ledger = _arm(mod, tmp_path, monkeypatch)
    mod._record_earnings_gate_rejects("premarket", "2026-09-22", {"calendar_missing": 1}, [])
    assert _last_row(ledger)["coverage"]["candidates"] == 0     # 不传 = 交集记 0

    def _boom():
        raise RuntimeError("calendar down")

    monkeypatch.setattr(mod, "_earnings_calendar_rows", _boom)
    mod._record_earnings_gate_rejects("premarket", "2026-09-22", {"calendar_missing": 1}, [],
                                      candidates=["US.IMCC"])
    cov = _last_row(ledger)["coverage"]
    assert "coverage_error" in cov and "calendar down" in cov["coverage_error"]


def test_empty_counts_still_writes_nothing(tmp_path, monkeypatch):
    """闸未触发时不落行 (与原有行为一致, 不因新增 coverage 改变落行条件)。"""
    mod = _load()
    ledger = _arm(mod, tmp_path, monkeypatch)
    mod._record_earnings_gate_rejects("premarket", "2026-09-22", {}, [], candidates=["US.AZO"])
    assert not ledger.exists()


def test_callers_pass_candidates_from_movers():
    """源码契约: 两个 collect 调用点都必须把候选池传进台账 (否则交集读数恒 0 = 假读数)。"""
    src = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()
    assert src.count('candidates=[str(m.get("symbol") or "") for m in movers]') == 2, (
        "collect 的两个调用点都要传候选池; 漏一处会让 coverage 的交集读数是假的 0"
    )