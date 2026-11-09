"""VIX 取数链的「输入准确」契约 (RFC-190 第三轮, 2026-11-09)。

守三件在这一轮审计里发现的事实:
  1. FRED 用 `.` 占位"当日未发布/假期" —— 末行是 `.` 时**不得**让整条 FRED 腿被吞;
  2. FRED 日频值必须带观测日 (`_vix_last_as_of`), 否则执行门日志无法区分 T-1 与实时;
  3. 退出监控的 ATR 波动放大不得用 `* 1.05` 这个与单一真源相反的方向系数,
     且不得把 AlphaVantage 的兜底 20.0 当真值缓存。
"""
from datetime import datetime, timedelta

import pytest
import requests as _requests

from app.analysis import market_state as ms


class _Resp:
    status_code = 200

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def _checker():
    c = ms.MarketStateChecker.__new__(ms.MarketStateChecker)  # 绕开 __init__ 的单例/连接副作用
    c._vix_cache = None
    c._vix_fred_cache = None
    c._vix_live_fail_ts = 0.0
    c._vix_last_source = "unknown"
    c._vix_last_as_of = None
    c._cache_ttl = timedelta(minutes=30)
    return c


def test_fred_dot_last_row_falls_back_to_last_valid_observation(monkeypatch):
    """末行 '.' (当日未发布) ⇒ 取最近有效观测, 而不是整条腿失败返回 None。"""
    csv_text = "DATE,VIXCLS\n2026-11-05,28.18\n2026-11-06,25.64\n2026-11-07,.\n"
    monkeypatch.setattr(_requests, "get", lambda *a, **k: _Resp(csv_text))
    monkeypatch.setattr("app.data.vix_source.is_vixy_proxy_window", lambda *a, **k: False)
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "")

    c = _checker()
    assert c._fetch_vix_data() == pytest.approx(25.64)
    assert c._vix_last_source == "fred_daily"
    assert c._vix_last_as_of == "2026-11-06"


def test_fred_daily_cache_keeps_as_of_and_accepts_legacy_two_tuple(monkeypatch):
    """日频槽位命中时 as_of 必须一起恢复; 旧的 2 元组缓存 (测试/部署残留) 不得崩。"""
    monkeypatch.setattr(_requests, "get", lambda *a, **k: _Resp("DATE,VIXCLS\n2026-11-06,25.64\n"))
    monkeypatch.setattr("app.data.vix_source.is_vixy_proxy_window", lambda *a, **k: False)

    c = _checker()
    c._vix_fred_cache = (25.64, datetime.now(), "2026-11-06")
    assert c._fetch_vix_data() == pytest.approx(25.64)
    assert c._vix_last_source == "fred_daily_cached"
    assert c._vix_last_as_of == "2026-11-06"

    c2 = _checker()
    c2._vix_fred_cache = (19.42, datetime.now())  # legacy 2-tuple
    assert c2._fetch_vix_data() == pytest.approx(19.42)
    assert c2._vix_last_as_of is None  # 不臆造观测日


# ── 退出监控的 VIX 口径 ───────────────────────────────────────────────────

def test_exit_monitor_vix_fallback_uses_single_source_factor(monkeypatch):
    """VIXY 降级腿必须走 vix_source (0.85), 而不是方向相反的 `* 1.05`。"""
    import pandas as pd
    from app.risk import position_exit_monitor as pem

    class _Ctx:
        def request_history_kline(self, *a, **k):
            return 0, pd.DataFrame({"close": [21.22]}), None

    from app.data import alphavantage_provider as avp
    monkeypatch.setattr(avp, "get_alpha_vantage_provider",
                        lambda: type("P", (), {"get_vix_or_none": lambda self: None})())

    m = pem.ExitMonitor.__new__(pem.ExitMonitor)
    m._vix_cached = None
    m._vix_ts = 0.0
    m._quote_ctx = _Ctx()

    assert m._get_vix() == pytest.approx(18.04, abs=0.01)   # 21.22 × 0.85
    assert m._vix_cached == pytest.approx(18.04, abs=0.01)


def test_exit_monitor_never_caches_provider_default(monkeypatch):
    """AV 兜底 20.0 不得被当作真值: 只接受 get_vix_or_none() 的真值。"""
    import pandas as pd
    from app.risk import position_exit_monitor as pem

    class _Ctx:
        def request_history_kline(self, *a, **k):
            return 0, pd.DataFrame({"close": [21.22]}), None

    calls = {"n": 0}

    class _P:
        def get_vix(self):
            calls["n"] += 1
            return 20.0          # 旧路径会把这个当真值

        def get_vix_or_none(self):
            calls["n"] += 1
            return None

    from app.data import alphavantage_provider as avp
    monkeypatch.setattr(avp, "get_alpha_vantage_provider", lambda: _P())

    m = pem.ExitMonitor.__new__(pem.ExitMonitor)
    m._vix_cached = None
    m._vix_ts = 0.0
    m._quote_ctx = _Ctx()

    assert m._get_vix() == pytest.approx(18.04, abs=0.01)  # 走了 VIXY 腿, 不是 20.0


def test_exit_monitor_source_has_no_105_factor():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "app" / "risk" / "position_exit_monitor.py"
    code = src.read_text(encoding="utf-8")
    block = code.split("def _get_vix", 1)[1].split("def _calc_atr_from_kline", 1)[0]
    # 剔注释: 修法说明里会引用旧系数 (tombstone), 那是证据不是复活
    block = "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))
    assert "* 1.05" not in block, "1.05 系数与 vix_source.VIXY_CONTANGO_FACTOR=0.85 方向相反"
    assert "vixy_to_vix" in block and "get_vix_or_none" in block

def test_pre_trade_vix_failure_is_visible_and_gate_stays_dormant():
    """两处死调用必须留痕 (一次性告警), 但其判定**不得**被本批擅自复活。

    `MarketStateChecker._fetch_vix()` 自始不存在 ⇒ P1-1 HIGH_VOL 警戒与 RFC-056
    分档削弱从未生效。复活 = 行为变更 (会削弱 strength), 须单独预注册。
    """
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "app" / "risk" / "pre_trade_check.py"
    code = "\n".join(ln for ln in src.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))
    assert code.count("._fetch_vix()") == 2, "死调用被悄悄改成别的名字 ⇒ 门复活, 属行为变更"
    assert code.count("_warn_vix_read_dead(") >= 3, "失败路径又变回静默"
