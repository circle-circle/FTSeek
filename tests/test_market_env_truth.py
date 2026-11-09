"""RFC-190 第三轮: 市场环境真值口径/as-of 契约 (只读, 零行为变更)。

守三条不变量:
  1. 账本缺失/过期**不得编数值** —— truth=None 或 stale 标记, 文本如实说明;
  2. 偏离 sign = (proxy/truth-1)*100, **负数 = 代理低估** (与 ledger 同公式);
  3. VIX 缓存必须带 as_of —— 写入时刻 (timestamp) 不等于数据观测日。
"""
import json
import os
import threading
import time

import pytest

from app.data import market_env_truth as met


def _write_ledger(path, rows, age_hours=0.0):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "market_env_daily.jsonl"
    monkeypatch.setattr(met, "LEDGER", p)
    return p


def test_load_latest_returns_last_row_with_age(ledger):
    _write_ledger(ledger, [
        {"date": "2026-11-05", "vix_close": 28.18},
        {"date": "2026-11-06", "vix_close": 25.64, "term_slope": 0.1049,
         "rv20_spy": 19.71, "vix_pct252": 98.9},
    ])
    row = met.load_latest()
    assert row["date"] == "2026-11-06"
    assert row["vix_close"] == 25.64
    assert row["_stale"] is False
    assert row["_age_hours"] < 1


def test_stale_ledger_is_flagged_not_dropped(ledger):
    _write_ledger(ledger, [{"date": "2026-11-06", "vix_close": 25.64}], age_hours=40)
    row = met.load_latest(max_age_hours=30)
    assert row is not None and row["_stale"] is True
    payload = met.describe(max_age_hours=30)
    assert payload["truth"]["stale"] is True
    assert "账本过期" in payload["line"]


def test_missing_ledger_returns_no_truth(ledger):
    assert met.load_latest() is None
    payload = met.describe(proxy_value=18.04, proxy_source="finnhub_vixy_proxy",
                           proxy_as_of="2026-11-06")
    assert payload["truth"] is None
    assert payload["gap_pct"] is None
    assert "环境真值账本不可用" in payload["line"]
    # 有代理值时仍要标出口径, 不能因真值缺失就退回裸数字
    assert "VIXY×0.85" in payload["line"]


def test_gap_sign_negative_means_proxy_underestimates(ledger):
    _write_ledger(ledger, [{"date": "2026-11-06", "vix_close": 25.64,
                            "term_slope": 0.1049, "rv20_spy": 19.71,
                            "vix_pct252": 98.9}])
    payload = met.describe(proxy_value=18.04, proxy_source="finnhub_vixy_proxy",
                           proxy_as_of="2026-11-06")
    assert payload["gap_pct"] == pytest.approx(-29.64, abs=0.01)
    assert "低估" in payload["line"] and "高估" not in payload["line"]
    # 口径 + as-of 必须出现在同一行里, 人不用回查缓存
    assert "代理" in payload["line"] and "2026-11-06" in payload["line"]
    assert "真值 25.64" in payload["line"] and "倒挂" in payload["line"]


def test_gap_sign_positive_means_proxy_overestimates(ledger):
    _write_ledger(ledger, [{"date": "2025-12-01", "vix_close": 20.0}])
    assert met.gap_pct(26.0, 20.0) == pytest.approx(30.0)
    assert "代理高估" in met.line(proxy_value=26.0, proxy_source="finnhub_vixy_proxy")
    # 现货源与真值同口径 ⇒ 偏离只能来自滞后, 不得写成"高估/低估"
    spot_line = met.line(proxy_value=26.0, proxy_source="fred")
    assert "高估" not in spot_line and "相对真值" in spot_line


def test_unknown_source_is_named_not_silently_spot():
    assert met.caliber_label("brand_new_source") == "未知口径(brand_new_source)"
    assert met.caliber_short(None) == "未知"
    assert met.caliber_short("fred") == "现货收盘"
    assert met.caliber_short("finnhub_vixy_proxy") == "代理"


def test_gap_pct_invalid_inputs_return_none():
    assert met.gap_pct(None, 20.0) is None
    assert met.gap_pct(18.0, 0) is None
    assert met.gap_pct("x", 20.0) is None


# ── VIX 缓存 as-of 契约 (write path) ──────────────────────────────────────

def test_vix_cache_records_observation_date(monkeypatch):
    """FRED 腿必须把 CSV 首列日期写进缓存 as_of, 且 '.' 占位行被跳过。"""
    from app.risk import vix_monitor as vm

    class _Resp:
        text = "DATE,VIXCLS\n2026-11-05,28.18\n2026-11-06,25.64\n2026-11-07,.\n"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(vm.requests, "get", lambda *a, **k: _Resp())

    # 绕过 __init__ (它会读写生产缓存文件): 只测取数 + 落缓存逻辑
    m = vm.VIXMonitor.__new__(vm.VIXMonitor)
    m._cache = {}
    m._cache_lock = threading.Lock()
    m._consecutive_failures = 0
    m._next_retry_at = 0.0
    m._last_success_time = 0.0
    m._last_as_of = None
    m.api_key = ""
    captured = {}
    m._save_cache = lambda data: captured.update(data)

    # 强制走 fred_first: 视为非代理窗口; 代理腿返回 None
    monkeypatch.setattr("app.core.scheduler_triggers.get_market_phase", lambda: "OVERNIGHT")
    monkeypatch.setattr(m, "_fetch_vix_finnhub", lambda: None)

    assert m.get_vix() == pytest.approx(25.64)   # '.' 行被跳过, 取最近有效观测
    assert captured["source"] == "fred"
    assert captured["as_of"] == "2026-11-06"
    assert captured["source_chain"] == "fred_first"