# -*- coding: utf-8 -*-
"""交易界面闸门呈现层契约 (2026-09-08, REVIEW-20260908 批1 B1/A2/A3).

钉三件事:
  1. **字段契约**: 前端 (trade.js renderGates/orderGateLines、hk_trade.html renderPrecheck)
     消费的每个键都必须存在且量纲明确 —— 改名/改单位会静默让预检条变成摆设。
  2. **PDT 不得出现**: 2026-08-02 用户裁决关闭 (富途证券(香港)账户不适用 FINRA PDT,
     app/risk/pdt_guard.py:347 + config risk.pdt_guard.enabled=false), 2026-09-08 用户复述
     "美股 PDT 限制条件在当前富途中不存在"。在 UI 呈现一个不存在的约束 = 纯噪声。
  3. **fail-open**: 呈现层读不到依赖时不得谎报"被阻断" (否则依赖故障 = 假拦截,
     与 scripts/trading_day_check.py 的 fail-open 纪律同源)。

以及港股侧 A3: 查询失败不得再伪装成空列表 (实盘页把 OpenD 掉线显示成"无港股持仓")。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.web_dashboard.order import gates as gates_mod
from app.web_dashboard.hk_order_service import HKOrderService


# ── 前端实际消费的键 (trade.js) ────────────────────────────────
FRONTEND_KEYS = [
    "kill_switch_active", "water_level", "water_blocks_manual", "water_msg",
    "drawdown_halt", "drawdown_reason", "manual_budget_usd",
    "max_order_value_usd", "today_manual_orders", "max_daily_manual_orders",
    "price_deviation_warn_pct", "price_deviation_hard_pct",
    "blocks_now", "warnings_now",
]


class FakeManualService:
    MAX_ORDER_VALUE_USD = 50_000
    MAX_DAILY_MANUAL_ORDERS = 20
    PRICE_DEVIATION_WARN_PCT = 5.0
    PRICE_DEVIATION_HARD_PCT = 10.0


@pytest.fixture
def clean_deps(monkeypatch):
    """把所有依赖钉成"健康且无阻断"的基线, 各测试再按需覆盖单项。"""
    import app.us_pipeline.emergency_interceptor as ei
    import app.risk.capital_safety as cs_mod
    import app.core.scheduler_core as sc
    import app.web_dashboard.order.validator as validator

    monkeypatch.setattr(ei, "check_kill_switch", lambda: False, raising=False)

    class _State:
        water_level = cs_mod.WaterLevel.GREEN
        is_locked = False

    class _CS:
        _state = _State()

        def can_open_position(self):
            return True, ""

        def get_available_budget_for_manual(self):
            return 123456.0

    monkeypatch.setattr(cs_mod, "get_capital_safety_system", lambda: _CS(), raising=False)
    monkeypatch.setattr(sc, "_scheduler_instance", None, raising=False)
    monkeypatch.setattr(validator, "count_today_manual_orders", lambda svc: 0, raising=False)
    return {"cs_mod": cs_mod, "ei": ei, "sc": sc, "validator": validator}


# ══════════════════════════════════════════════════════════════
#  1. 字段契约 + 量纲
# ══════════════════════════════════════════════════════════════

def test_all_frontend_consumed_keys_present(clean_deps):
    payload = gates_mod.collect_manual_gates(FakeManualService())
    missing = [k for k in FRONTEND_KEYS if k not in payload]
    assert not missing, f"前端消费的键缺失 (预检条会静默失效): {missing}"


def test_no_write_only_keys(clean_deps):
    """反向契约: 不得下发前端不消费的字段 (write-only = prompt/接口膨胀主因)。"""
    payload = gates_mod.collect_manual_gates(FakeManualService())
    extra = [k for k in payload if k not in FRONTEND_KEYS and k != "kill_switch_known"]
    assert not extra, f"未被前端消费的字段: {extra}"


def test_units_are_absolute_money_and_percentage_not_fraction(clean_deps):
    """量纲钉子: 金额=绝对美元, 阈值=百分数 (5.0 而非 0.05), 次数=计数。"""
    payload = gates_mod.collect_manual_gates(FakeManualService())
    assert payload["max_order_value_usd"] == 50_000          # 绝对 USD, 非占比
    assert isinstance(payload["max_order_value_usd"], int)   # 计数/金额走 int, 不下发 50000.0
    assert isinstance(payload["max_daily_manual_orders"], int)
    assert payload["manual_budget_usd"] == 123456.0          # 绝对 USD
    assert payload["price_deviation_warn_pct"] == 5.0        # 百分数, 不是 0.05
    assert payload["price_deviation_hard_pct"] == 10.0       # 百分数, 不是 0.10
    assert payload["max_daily_manual_orders"] == 20          # 计数
    assert payload["today_manual_orders"] == 0               # 计数


def test_blocks_and_warnings_are_string_lists(clean_deps):
    payload = gates_mod.collect_manual_gates(FakeManualService())
    assert isinstance(payload["blocks_now"], list)
    assert isinstance(payload["warnings_now"], list)
    assert all(isinstance(x, str) for x in payload["blocks_now"] + payload["warnings_now"])


# ══════════════════════════════════════════════════════════════
#  2. PDT 不得出现 (2026-08-02 / 2026-09-08 用户裁决)
# ══════════════════════════════════════════════════════════════

def test_pdt_never_surfaced_in_gates(clean_deps):
    payload = gates_mod.collect_manual_gates(FakeManualService())
    blob = repr(payload).lower()
    assert "pdt" not in blob, "PDT 已于 2026-08-02 裁决关闭 (富途香港不适用), 不得在 UI 呈现"
    assert not [k for k in payload if "pdt" in k.lower()]


def test_gates_module_does_not_import_pdt_guard():
    """代码层面零 PDT (docstring 里保留裁决出处是刻意的, 不算引用)。"""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "..",
                        "app", "web_dashboard", "order", "gates.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # ast.Import 无 .module (只有 ImportFrom 有), 故用 getattr
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            assert not any("pdt" in (n or "").lower() for n in names), \
                "gates.py 不得 import PDT 相关模块 (裁决: 富途香港不适用 FINRA PDT)"
    docstring = ast.get_docstring(tree) or ""
    code_only = src.replace(docstring, "", 1)
    assert "pdt" not in code_only.lower(), "gates.py 代码体内不得出现 PDT 逻辑"


# ══════════════════════════════════════════════════════════════
#  3. fail-open: 依赖故障 ≠ 阻断
# ══════════════════════════════════════════════════════════════

def test_dependency_failure_does_not_claim_block(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("dependency down")

    import app.us_pipeline.emergency_interceptor as ei
    import app.risk.capital_safety as cs_mod
    monkeypatch.setattr(ei, "check_kill_switch", boom, raising=False)
    monkeypatch.setattr(cs_mod, "get_capital_safety_system", boom, raising=False)

    payload = gates_mod.collect_manual_gates(FakeManualService())
    assert payload["blocks_now"] == [], "依赖故障时谎报阻断 = 把故障变成假拦截"
    assert payload["kill_switch_active"] is False
    assert payload["water_level"] == "unknown"
    assert payload["water_blocks_manual"] is False


def test_missing_service_still_returns_full_shape():
    """service=None 且拿不到单例时, 键仍齐全 (前端不至于 undefined 崩)。"""
    payload = gates_mod.collect_manual_gates(service=object())
    for key in FRONTEND_KEYS:
        assert key in payload


# ══════════════════════════════════════════════════════════════
#  4. 各闸门语义与 validator 对齐
# ══════════════════════════════════════════════════════════════

def test_kill_switch_active_blocks(clean_deps, monkeypatch):
    monkeypatch.setattr(clean_deps["ei"], "check_kill_switch", lambda: True, raising=False)
    payload = gates_mod.collect_manual_gates(FakeManualService())
    assert payload["kill_switch_active"] is True
    assert any("紧急停止" in b for b in payload["blocks_now"])


def test_blackout_blocks_manual(clean_deps, monkeypatch):
    cs_mod = clean_deps["cs_mod"]

    class _State:
        water_level = cs_mod.WaterLevel.BLACKOUT
        is_locked = False

    class _CS:
        _state = _State()

        def can_open_position(self):
            return False, "熔断: 全部平仓, 禁止一切交易"

        def get_available_budget_for_manual(self):
            return 0.0

    monkeypatch.setattr(cs_mod, "get_capital_safety_system", lambda: _CS(), raising=False)
    payload = gates_mod.collect_manual_gates(FakeManualService())
    assert payload["water_blocks_manual"] is True
    assert any("BLACKOUT" in b for b in payload["blocks_now"])


def test_red_water_warns_but_does_not_block_manual(clean_deps, monkeypatch):
    """validator §9 对手动单只在 BLACKOUT 硬阻断, RED 仅 logger.warning — 呈现层须同口径。"""
    cs_mod = clean_deps["cs_mod"]

    class _State:
        water_level = cs_mod.WaterLevel.RED
        is_locked = False

    class _CS:
        _state = _State()

        def can_open_position(self):
            return False, "危险水位: 禁止开新仓, 请减仓"

        def get_available_budget_for_manual(self):
            return 1000.0

    monkeypatch.setattr(cs_mod, "get_capital_safety_system", lambda: _CS(), raising=False)
    payload = gates_mod.collect_manual_gates(FakeManualService())
    assert payload["water_blocks_manual"] is False
    assert payload["blocks_now"] == []
    assert any("RED" in w or "资本安全" in w for w in payload["warnings_now"])


def test_drawdown_halt_blocks(clean_deps, monkeypatch):
    class _DD:
        def is_trading_allowed(self):
            return False

        def get_block_reason(self):
            return "已实现亏损 3.2% > 3%"

    class _Risk:
        drawdown_controller = _DD()

    class _Sched:
        risk_engine = _Risk()

    monkeypatch.setattr(clean_deps["sc"], "_scheduler_instance", _Sched(), raising=False)
    payload = gates_mod.collect_manual_gates(FakeManualService())
    assert payload["drawdown_halt"] is True
    assert any("回撤熔断" in b for b in payload["blocks_now"])


def test_daily_quota_exhausted_blocks(clean_deps, monkeypatch):
    monkeypatch.setattr(clean_deps["validator"], "count_today_manual_orders",
                        lambda svc: 20, raising=False)
    payload = gates_mod.collect_manual_gates(FakeManualService())
    assert any("上限 20" in b for b in payload["blocks_now"])


# ══════════════════════════════════════════════════════════════
#  5. 港股侧 A3 (失败 ≠ 空) + A2 (限额快照)
# ══════════════════════════════════════════════════════════════

def test_hk_positions_failure_is_not_silent_empty(monkeypatch):
    svc = HKOrderService()
    monkeypatch.setattr(svc, "connect", lambda: False)
    result = svc.get_positions_with_status()
    assert result["rows"] == []
    assert result["error"] == "broker_not_connected", "掉线必须可辨, 不得伪装成无持仓"


def test_hk_orders_failure_is_not_silent_empty(monkeypatch):
    svc = HKOrderService()
    monkeypatch.setattr(svc, "connect", lambda: False)
    result = svc.get_today_orders_with_status()
    assert result["rows"] == []
    assert result["error"] == "broker_not_connected"


def test_hk_legacy_wrappers_still_return_list(monkeypatch):
    """旧签名兼容: get_positions()/get_today_orders() 仍返回 list (失败为空)。"""
    svc = HKOrderService()
    monkeypatch.setattr(svc, "connect", lambda: False)
    assert svc.get_positions() == []
    assert svc.get_today_orders() == []


def test_hk_order_limits_contract(monkeypatch):
    """A2 预检条数据源: 键齐全 + 量纲明确 (HKD 绝对值 / 百分数 / 计数)。"""
    svc = HKOrderService()
    monkeypatch.setattr(svc, "_count_today_orders", lambda: 3)
    limits = svc.get_order_limits()
    assert set(limits) == {
        "max_order_value_hkd", "max_daily_manual_orders", "today_manual_orders",
        "price_deviation_warn_pct", "price_deviation_hard_pct",
    }
    assert limits["max_order_value_hkd"] == float(svc.MAX_ORDER_VALUE_HKD)   # 绝对 HKD
    assert limits["price_deviation_warn_pct"] == 5.0                        # 百分数, 非 0.05
    assert limits["price_deviation_hard_pct"] == 10.0
    assert limits["today_manual_orders"] == 3                               # 计数
    assert limits["max_daily_manual_orders"] == svc.MAX_DAILY_MANUAL_ORDERS
