"""币种换算回归 (2026-09-21 切实盘修正): 非 USD 账户的 equity/cash/market_val 折算。

背景 (实测取证, 见 docs/analysis/2026-09-20-live-vs-sim-execution-readiness.md §6):
  REAL 账户 (281756480166019917) 是 HKD 计价且带本币负腿 (hkd_assets < 0)。
  旧实现的 fx_rate = usd_assets/total_assets 不是汇率 → equity 高估 +9.68%, cash 高估 8.6×。
  本测试用**录下来的券商原始行**做桩, 锁死修正后的口径 (不连券商, 只验算术)。

SIM (currency='N/A') 必须逐点不变: 它根本不进本分支。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import time

import pandas as pd
import pytest
from fastapi import FastAPI

from app.execution.broker_adapter import FutuBrokerAdapter
from app.risk.futu_risk import calc_futu_risk
from app.web_dashboard.data_service import DataService
from app.web_dashboard.routes.dashboard import _parse_account
from futu import RET_OK

# ── 券商原始行 (2026-09-21 实盘只读 accinfo_query 实录, 字符串保留原样) ──
RAW_SIM = {
    "total_assets": "99392.093", "cash": "99392.093", "market_val": "0.0",
    "power": "198784.186", "max_power_short": "N/A", "currency": "N/A",
    "usd_assets": "N/A", "hkd_assets": "N/A", "us_cash": "99392.093",
    "us_avl_withdrawal_cash": "0.0", "usd_net_cash_power": "N/A",
    "avl_withdrawal_cash": "0.0", "frozen_cash": "0.0",
    "securities_assets": "99392.093",
}
RAW_REAL_HKD = {
    "total_assets": "660473.79", "cash": "84173.79", "market_val": "576300.0",
    "power": "494483.03673", "max_power_short": "494483.03673", "currency": "HKD",
    "usd_assets": "92339.62", "hkd_assets": "-63947.15", "us_cash": "92339.62",
    "us_avl_withdrawal_cash": "31513.55", "usd_net_cash_power": "31513.55919218",
    "avl_withdrawal_cash": "247229.47", "frozen_cash": "2832.9141666",
    "securities_assets": "660473.7899999999",
    "debt": "10000.0", "interest_debt": "500.0",
    "short_sell_power": "100000.0", "net_assets_power": "600000.0",
    "risk_status": "LEVEL3", "risk_level": "N/A", "exposure_level": "SAFE",
    "exposure_limit": "N/A", "used_limit": "N/A", "remaining_limit": "N/A",
    "is_pdt": "N/A", "pdt_seq": "N/A", "beginning_dtbp": "N/A",
    "dt_status": "N/A", "remaining_dtbp": "N/A", "dt_call_amount": "N/A",
}


class _StubCtx:
    def __init__(self, row: dict):
        self._row = row

    def accinfo_query(self, **kwargs):
        return RET_OK, pd.DataFrame([self._row])


def _adapter(row: dict, trade_env: int = 1) -> FutuBrokerAdapter:
    a = FutuBrokerAdapter(host="127.0.0.1", port=11111, trade_env=trade_env, account_id="0")
    a.connected = True          # 绕过真实连接 (只测算术)
    a.trade_ctx = _StubCtx(row)
    a._last_heartbeat_time = time.time()   # 阻止 is_connected() 去真探活 (桩无该接口)
    return a


def test_sim_account_path_unchanged():
    """SIM (currency='N/A') 不进换算分支 → 值必须与修正前逐点一致。"""
    info = _adapter(RAW_SIM, trade_env=1).get_account_info()
    assert info["total_assets"] == 99392.093
    assert info["cash"] == 99392.093
    assert info["market_val"] == 0.0
    assert info["buying_power"] == 198784.186
    assert "_fx_rate" not in info and "_cash_source" not in info


def test_real_hkd_equity_is_net_of_local_currency_leg():
    """净值 = 本币总资产折算 (含本币负腿), 不是 USD 毛侧资产。"""
    info = _adapter(RAW_REAL_HKD, trade_env=0).get_account_info()

    fx = 92339.62 / (660473.79 + 63947.15)          # = 0.1274675 (隐含 7.8452 HKD/USD)
    assert info["_fx_rate"] == pytest.approx(fx, rel=1e-6)
    assert info["total_assets"] == pytest.approx(660473.79 * fx, abs=0.02)

    # 旧实现会给出 usd_assets=92339.62 (高估 +9.68%) — 明确禁止回归
    assert info["total_assets"] < 90000
    assert info["_usd_side_assets"] == 92339.62


def test_real_hkd_cash_ignores_inconsistent_us_cash_field():
    """us_cash(92339.62) 与折算值(10730) 差 8.6× → 弃用券商字段, 用折算值。"""
    info = _adapter(RAW_REAL_HKD, trade_env=0).get_account_info()
    fx = info["_fx_rate"]
    assert info["cash"] == pytest.approx(84173.79 * fx, abs=0.02)
    assert "不一致" in info["_cash_source"]
    assert info["_usd_cash_field"] == 92339.62


def test_real_hkd_equity_identity_cash_plus_market_value():
    """净值 ≈ 现金 + 持仓市值 (折算后) — 口径自洽的硬约束。"""
    info = _adapter(RAW_REAL_HKD, trade_env=0).get_account_info()
    lhs = info["cash"] + info["market_val"]
    assert lhs == pytest.approx(info["total_assets"], rel=0.001)


def test_real_hkd_buying_power_prefers_broker_usd_field():
    """usd_net_cash_power 与 avl_withdrawal_cash/FX 精确互证 → 采信券商字段。"""
    info = _adapter(RAW_REAL_HKD, trade_env=0).get_account_info()
    assert info["buying_power"] == pytest.approx(31513.55919218, abs=0.01)
    assert info["_buying_power_source"] == "usd_net_cash_power"
    assert info["avl_withdrawal_cash"] == pytest.approx(31513.55, abs=0.01)


def test_real_hkd_risk_fields_are_usd_and_survive_futu_risk():
    """FutuRisk 消费的债务/利息/功率字段不得遗留本币单位。"""
    info = _adapter(RAW_REAL_HKD, trade_env=0).get_account_info()
    fx = info["_fx_rate"]
    for field, native in (
        ("debt", 10000.0),
        ("interest_debt", 500.0),
        ("short_sell_power", 100000.0),
        ("net_assets_power", 600000.0),
    ):
        assert info[field] == pytest.approx(native * fx, abs=0.02)

    risk = calc_futu_risk(info)
    assert risk.net_liquidation == pytest.approx(
        info["total_assets"] - info["debt"], abs=0.02
    )


def test_dashboard_account_paths_share_broker_hkd_caliber():
    """DataService/dashboard route 不得重新引入 usd_assets 毛侧净值或 us_cash 毛现金。"""
    info = _adapter(RAW_REAL_HKD, trade_env=0).get_account_info()
    rows = [info, DataService.to_usd(RAW_REAL_HKD), _parse_account(RAW_REAL_HKD)]

    for row in rows:
        assert row["account_scope"] == "global"
        assert row["total_assets"] == pytest.approx(84188.48, abs=0.02)
        assert row["cash"] == pytest.approx(10729.36, abs=0.02)
        assert row["market_val"] == pytest.approx(73459.12, abs=0.02)

    assert DataService.to_usd(RAW_REAL_HKD)["power"] == pytest.approx(
        31513.55919218, abs=0.01
    )
    assert _parse_account(RAW_REAL_HKD)["avl_cash"] == pytest.approx(
        31513.55, abs=0.01
    )


def test_broker_risk_fields_pass_through_without_fake_usd_values():
    """P1 DEB-20261025: risk fields survive both normalization and Dashboard."""
    parsed = _parse_account(RAW_REAL_HKD)
    assert parsed["risk_status"] == "LEVEL3"
    assert parsed["risk_level"] == "N/A"
    assert parsed["exposure_level"] == "SAFE"
    assert parsed["remaining_limit"] == "N/A"
    assert parsed["is_pdt"] == "N/A"
    assert parsed["pdt_seq"] == "N/A"
    assert parsed["remaining_dtbp"] == "N/A"
    assert parsed["risk_fields_currency"] == "broker_native"

    converted = DataService.to_usd(RAW_REAL_HKD)
    for field in (
        "risk_status", "risk_level", "exposure_level", "exposure_limit",
        "used_limit", "remaining_limit", "is_pdt", "pdt_seq", "beginning_dtbp",
        "dt_status", "remaining_dtbp", "dt_call_amount",
    ):
        assert converted[field] == parsed[field]
    assert converted["risk_fields_currency"] == "broker_native"


def test_order_capacity_route_uses_shared_hkd_caliber(monkeypatch):
    """order-capacity 端点不得残留旧的 hkd_assets/usd_assets 汇率口径。"""
    import app.web_dashboard.manual_order_service as manual_order_service
    from app.web_dashboard.routes import trade

    class FakeBrokerCtx:
        def accinfo_query(self, **kwargs):
            return RET_OK, pd.DataFrame([RAW_REAL_HKD])

        def position_list_query(self, **kwargs):
            return RET_OK, pd.DataFrame()

    fake_service = SimpleNamespace(
        connect=lambda: True,
        _is_live=lambda: False,
        _read_env=lambda key, default: "0",
        get_quote=lambda symbol: {"cur_price": 100.0},
        _broker_ctx=FakeBrokerCtx(),
    )
    monkeypatch.setattr(
        manual_order_service, "get_manual_order_service", lambda: fake_service
    )

    app = FastAPI()
    trade.register(app, object())
    route = next(
        item for item in app.routes
        if getattr(item, "path", "") == "/api/order-capacity/{symbol}"
    )
    result = asyncio.run(route.endpoint("AAPL"))

    assert "error" not in result
    assert result["account_currency"] == "HKD"
    assert result["cash"] == pytest.approx(31513.55, abs=0.01)
    assert result["power"] == pytest.approx(31513.56, abs=0.01)
    assert result["cash_can_buy"] == 315
