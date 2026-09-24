#!/usr/bin/env python3
"""DEB-20260923 裁决 A 契约测试: 严格超集 + 不追高 + 四态 stale 分类。

§3.2 实证 (190 只): 新判据 120 只 / 旧判据 18 只 / 两个都放 18 只 / 仅旧放 0 只 ⇒
  新判据是旧判据的**严格超集**: 换口径零回归, 只把 102 只真 beat 从误拦里放出来。
§3.3 入场变体⑤ (温和票 +0.5~2%): −$0.41≈打平 (胜 15/19), 是唯一非负期望的多头变体。
§13.3.2 / §14.2: 旧判据的 surprise_stale 混装三态 → 拆四态 (endpoint_empty /
  period_unparseable / stale_gt_150 / future_lt_-7), 10 月对照裁决必需。

本文件只测**契约** (新⊇旧 / 不追高带 / 四态分类), 不测具体回放读数 (那些在 DEB §3/§13 里)。
"""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = "2026-09-22"


def _load():
    spec = importlib.util.spec_from_file_location(
        "ext_entry_exec_deb23_contract",
        ROOT / "scripts" / "extended_hours_entry_executor.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeClient:
    """双端点桩: company_earnings (旧路径) + earnings_calendar (新路径)。"""

    def __init__(self, company_rows, cal_rows):
        self._company = company_rows
        self._cal = cal_rows

    def company_earnings(self, symbol, limit=None):
        return self._company

    def earnings_calendar(self, _from, _to, symbol, international=False):
        return {"earningsCalendar": self._cal, "to": _to}


def _wire(mod, monkeypatch, futu_cal_rows, finnhub_cal_rows, company_rows, tmp_path):
    """同时桩住两条数据路径 (futu 日历 + Finnhub 双端点), 让 legacy 与 new 都走到真逻辑。"""
    import app.data.premarket_enrichment as pe

    monkeypatch.setattr(
        pe, "_get_finnhub_client",
        lambda: _FakeClient(company_rows, finnhub_cal_rows),
    )
    monkeypatch.setattr(mod, "_earnings_calendar_rows", lambda: (futu_cal_rows, None))
    monkeypatch.setattr(mod, "_today_et", lambda: TODAY)
    monkeypatch.setattr(mod, "_FINNHUB_TODAY_CAL_CACHE", {})
    monkeypatch.setattr(mod, "EARNINGS_CRITERION", "calendar_actual")
    monkeypatch.setattr(mod, "EARNINGS_CRITERION_SHADOW", True)
    monkeypatch.setattr(mod, "EARNINGS_CRITERION_SHADOW_PATH", tmp_path / "shadow.jsonl")
    monkeypatch.setattr(mod, "_EARNINGS_CRITERION_SHADOW_KEYS", None)


# ── §3.2 严格超集 ────────────────────────────────────────────────────────────


def test_new_criterion_is_strict_superset_of_legacy(monkeypatch, tmp_path):
    """§3.2 核心契约: 旧判据放行的票, 新判据必须也放行 (严格超集的机械保证)。

    场景: 票同时满足 (a) futu 日历有当日同相位事件 (判据①) (b) Finnhub company_earnings
    有正 surprise 且 period 在 [-7,150] 窗内 (旧判据②) (c) Finnhub 日历行有正 actual beat
    (新判据②)。契约: legacy pass ⇒ new pass。
    """
    mod = _load()
    _wire(
        mod, monkeypatch,
        futu_cal_rows=[{"symbol": "BOTH", "reportDate": TODAY, "timeOfTheDay": "post-market"}],
        finnhub_cal_rows=[{"symbol": "BOTH", "hour": "amc", "epsActual": 1.5, "epsEstimate": 1.0}],
        company_rows=[{"surprisePercent": 15.0, "period": "2026-06-30"}],  # age=84, in [-7,150]
        tmp_path=tmp_path,
    )

    legacy_res, legacy_reason = mod._earnings_surprise_legacy_finnhub("BOTH")
    assert legacy_res is not None and legacy_reason is None, (
        f"fixture 必须让旧判据通过, 实际: {(legacy_res, legacy_reason)}"
    )
    new_res, new_reason = mod._earnings_surprise_calendar_actual("BOTH", TODAY)
    assert new_res is not None and new_reason is None, (
        f"旧放行 ⇒ 新必须放行 (严格超集违反), 实际: {(new_res, new_reason)}"
    )


def test_new_accepts_when_legacy_rejects_future_period(monkeypatch, tmp_path):
    """§2.1 主病灶: period 在未来 (age<-7) 但 actual 已在库且 beat ⇒ 旧拒新放。

    生产活例 US.KBH (actual 1.05 / est 0.9022 / +16.38%, period='2026-09-30' age=-8)。
    这是"仅新放行"的 102 只里的典型形态。
    """
    mod = _load()
    _wire(
        mod, monkeypatch,
        futu_cal_rows=[{"symbol": "KBH", "reportDate": TODAY, "timeOfTheDay": "post-market"}],
        finnhub_cal_rows=[{"symbol": "KBH", "hour": "amc", "epsActual": 1.05, "epsEstimate": 0.9022}],
        company_rows=[{"surprisePercent": 16.38, "period": "2026-09-30"}],  # age=-8
        tmp_path=tmp_path,
    )
    legacy_res, legacy_reason = mod._earnings_surprise_legacy_finnhub("KBH")
    assert legacy_res is None and legacy_reason == "surprise_stale"
    new_res, new_reason = mod._earnings_surprise_calendar_actual("KBH", TODAY)
    assert new_res is not None and new_reason is None
    assert abs(new_res["surprise_pct"] - 16.38) < 0.1


def test_both_reject_miss_ticket(monkeypatch, tmp_path):
    """miss 票两条路径都拒 (新判据不是无条件放行, 它仍要求 actual > est)。"""
    mod = _load()
    _wire(
        mod, monkeypatch,
        futu_cal_rows=[{"symbol": "MISS", "reportDate": TODAY, "timeOfTheDay": "post-market"}],
        finnhub_cal_rows=[{"symbol": "MISS", "hour": "amc", "epsActual": 0.80, "epsEstimate": 0.90}],
        company_rows=[{"surprisePercent": -11.0, "period": "2026-06-30"}],
        tmp_path=tmp_path,
    )
    legacy_res, _ = mod._earnings_surprise_legacy_finnhub("MISS")
    new_res, new_reason = mod._earnings_surprise_calendar_actual("MISS", TODAY)
    assert legacy_res is None
    assert new_res is None and new_reason == "surprise_nonpositive"


# ── §3.3 不追高约束 ──────────────────────────────────────────────────────────


def test_mild_population_band_rejects_chasers(monkeypatch):
    """§3.3 入场变体⑤: 温和票带 0.5~2.0% 是唯一非负期望的多头变体。

    带外 (>2%) = 追高人群 (入场时涨幅中位 +5.24%, MFE +0.70% ≪ MAE −3.85%)。
    带内 (0.5~2%) = 温和人群 (回放 19 笔, −$0.41/笔≈打平, 胜 15/19)。
    """
    mod = _load()
    monkeypatch.setattr(mod, "ENTRY_CHG_BAND", "0.5,2.0")
    monkeypatch.setattr(mod, "ENTRY_CHANGE_PCT", 2.0)

    # 带内: 温和票放行
    assert mod._entry_chg_ok(0.5) is True
    assert mod._entry_chg_ok(1.0) is True
    assert mod._entry_chg_ok(2.0) is True

    # 带外: 追高拒绝
    assert mod._entry_chg_ok(2.1) is False   # 追高 (回放 −$81.26/笔)
    assert mod._entry_chg_ok(5.0) is False   # 大幅追高 (入场涨幅中位附近)
    assert mod._entry_chg_ok(0.4) is False   # 过低 (流动性/信号不足)


def test_empty_band_keeps_legacy_threshold(monkeypatch):
    """空带 (默认/回滚): 沿用 ENTRY_CHANGE_PCT 下限 (旧行为, 含追高人群)。"""
    mod = _load()
    monkeypatch.setattr(mod, "ENTRY_CHG_BAND", "")
    monkeypatch.setattr(mod, "ENTRY_CHANGE_PCT", 2.0)

    assert mod._entry_chg_ok(1.9) is False
    assert mod._entry_chg_ok(2.0) is True
    assert mod._entry_chg_ok(5.0) is True   # 旧口径不挡追高 (A 方案的核心修复之一)


# ── §13.3.2 / §14.2 四态 stale 分类 ──────────────────────────────────────────


def test_legacy_stale_four_state_classification():
    """surprise_stale 四态必须可区分 (10 月对照台账归因必需)。

    §14.2: 旧分类把 Finnhub 端点不稳定 (空返回) 与口径误拦 (age<-7) 混为同一 sub_reason
    ⇒ 10 月 `surprise_stale` 计数上升时无法区分"源坏了"与"口径在误拦"。
    """
    mod = _load()

    # endpoint_empty: company_earnings 空返回 → period 和 age 都 None
    assert mod._classify_legacy_stale_state("surprise_stale", {"period": None, "period_age_days": None}) == "endpoint_empty"

    # period_unparseable: period 存在但不可解析 → age None
    assert mod._classify_legacy_stale_state("surprise_stale", {"period": "not-a-date", "period_age_days": None}) == "period_unparseable"

    # stale_gt_150: 真陈旧 (上季记录, 守卫原始目标)
    assert mod._classify_legacy_stale_state("surprise_stale", {"period": "2026-03-31", "period_age_days": 175}) == "stale_gt_150"

    # future_lt_-7: 口径误拦的主体 (未来财季被当"新鲜度"锚)
    assert mod._classify_legacy_stale_state("surprise_stale", {"period": "2026-09-30", "period_age_days": -8}) == "future_lt_-7"
    assert mod._classify_legacy_stale_state("surprise_stale", {"period": "2027-03-31", "period_age_days": -190}) == "future_lt_-7"

    # 非 stale 原因 → None (不分类)
    assert mod._classify_legacy_stale_state("surprise_nonpositive", {}) is None
    assert mod._classify_legacy_stale_state("calendar_missing", {}) is None
    assert mod._classify_legacy_stale_state(None, {}) is None


def test_shadow_ledger_records_four_state_split(monkeypatch, tmp_path):
    """§7.1 对照台账: legacy_stale_state 字段必须落入 shadow jsonl (10 月裁决的输入)。

    KBH 活例: period='2026-09-30' age=-8 ⇒ future_lt_-7 (口径误拦)。
    """
    mod = _load()
    _wire(
        mod, monkeypatch,
        futu_cal_rows=[{"symbol": "KBH", "reportDate": TODAY, "timeOfTheDay": "post-market"}],
        finnhub_cal_rows=[{"symbol": "KBH", "hour": "amc", "epsActual": 1.05, "epsEstimate": 0.9022}],
        company_rows=[{"surprisePercent": 16.38, "period": "2026-09-30"}],  # age=-8
        tmp_path=tmp_path,
    )
    mod._earnings_surprise("US.KBH", "afterhours")
    shadow_path = tmp_path / "shadow.jsonl"
    assert shadow_path.exists()
    rows = [json.loads(l) for l in shadow_path.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["legacy_stale_state"] == "future_lt_-7"
    assert rows[0]["legacy_blocked_new_pass"] is True
    assert rows[0]["legacy_reason"] == "surprise_stale"
    assert rows[0]["new_pass"] is True


def test_shadow_ledger_stale_gt_150_state(monkeypatch, tmp_path):
    """真陈旧票 (上季记录) 必须分到 stale_gt_150 (与口径误拦区分)。"""
    mod = _load()
    _wire(
        mod, monkeypatch,
        futu_cal_rows=[{"symbol": "OLD", "reportDate": TODAY, "timeOfTheDay": "pre-market"}],
        finnhub_cal_rows=[{"symbol": "OLD", "hour": "bmo", "epsActual": 2.0, "epsEstimate": 1.5}],
        company_rows=[{"surprisePercent": 10.0, "period": "2026-03-31"}],  # age=175
        tmp_path=tmp_path,
    )
    mod._earnings_surprise("US.OLD", "premarket")
    rows = [json.loads(l) for l in (tmp_path / "shadow.jsonl").read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["legacy_stale_state"] == "stale_gt_150"
    assert rows[0]["new_pass"] is True


def test_shadow_ledger_endpoint_empty_state(monkeypatch, tmp_path):
    """Finnhub 端点空返回 (company_earnings=[]) → endpoint_empty (§14.2 KBH 二次查询实测)。"""
    mod = _load()
    _wire(
        mod, monkeypatch,
        futu_cal_rows=[{"symbol": "EMPTY", "reportDate": TODAY, "timeOfTheDay": "post-market"}],
        finnhub_cal_rows=[{"symbol": "EMPTY", "hour": "amc", "epsActual": 1.0, "epsEstimate": 0.8}],
        company_rows=[],  # 端点空返回 → finnhub_no_data
        tmp_path=tmp_path,
    )
    mod._earnings_surprise("US.EMPTY", "afterhours")
    rows = [json.loads(l) for l in (tmp_path / "shadow.jsonl").read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    # company_earnings=[] → legacy 返回 finnhub_no_data (非 surprise_stale)
    # ⇒ legacy_stale_state 为 None (只有 surprise_stale 才分类)
    assert rows[0]["legacy_stale_state"] is None
    assert rows[0]["legacy_reason"] == "finnhub_no_data"
    assert rows[0]["new_pass"] is True
