"""[DEB-20260901 选项1② · ADR-035] 入场 archetype 路由契约测试.

锁住的语义 (开关默认关闭; 启用前提=影子判据全过, 见 ADR-035):
  R1  resolve_route 纯函数判定 (开关关/无archetype/BREAKOUT/矛盾价位/偏离过大/TTL取短)
  R2  路由指令下 LIMIT 单@确认位 (市价路径零变化对照)
  R3  TTL 内全成 → 按 dealt_avg_price 真价 proceed
  R4  TTL 到期零成交 → 撤单 + skip_reason=archetype_ttl_expired 终态 (绝不市价追)
  R5  TTL 到期部分成交 → 撤余单按已成交量 proceed
  R6  撤单-成交竞态 → 复查到成交按成交 proceed
  R7  订单被拒/撤 → skip_reason=archetype_order_dead 终态
  R8  配置默认值: 路由关 / TTL=300 / 终态跳过集合并入 INVALIDATED_SKIP_REASONS
"""
import json
import sqlite3
import threading
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from futu import RET_OK

from app.playbook.db import PlaybookDB
from app.playbook.playbook_service import PlaybookService
from app.playbook.entry_routing import resolve_route, routing_enabled, parse_raw_semantics

_ROUTE_CFG = {"entry_archetype_routing_enabled": True, "entry_archetype_ttl_seconds": 300}
_PB_RAW = {"entry_archetype": "PULLBACK_LIMIT",
           "confirmation": {"price_level": 140.5, "side": "below", "ttl_min": 15},
           "chase_cap_price": None}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


@pytest.fixture
def svc(db):
    return PlaybookService(playbook_db=db)


def _save_inst(db, **kwargs):
    payload = {
        "date": "2026-09-01",
        "symbol": "US.TEST_ARCH",  # TEST 守卫: 影子遥测不落生产文件
        "direction": "BUY",
        "entry_price": 145.0,
        "stop_loss": 137.0,
        "take_profit": 152.0,
        "position_size": 40,
        "confidence": 70,
        "conditions": ["pullback_to_confirmation"],
        "raw_instruction_json": json.dumps(_PB_RAW, ensure_ascii=False),
    }
    payload.update(kwargs)
    return db.save_instruction(payload)


def _mk_dual(order_rows, submit_result=None):
    """构造 mock dual_broker: submit_order 捕获订单; order_list_query 按序返回状态行."""
    dual = MagicMock()
    ctx = MagicMock()
    lock = threading.Lock()
    dual._ctx_and_acc.return_value = (ctx, "12345", "sim", lock)
    captured = {}

    def _submit(order, target_env="sim"):
        captured["order"] = order
        captured["target_env"] = target_env
        return submit_result or {"success": True, "futu_order_id": "F1",
                                 "filled_price": 0, "filled_qty": 0, "error": ""}
    dual.submit_order.side_effect = _submit

    _rows = list(order_rows)

    def _olq(order_id=None, trd_env=None):
        if not _rows:
            return RET_OK, pd.DataFrame([{"order_status": "SUBMITTED", "dealt_qty": 0,
                                           "dealt_avg_price": 0}])
        row = _rows.pop(0) if len(_rows) > 1 else _rows[0]
        return RET_OK, pd.DataFrame([row])
    ctx.order_list_query.side_effect = _olq
    ctx.modify_order.return_value = (RET_OK, None)
    dual.cancel_order.return_value = {"success": True, "error": ""}
    return dual, captured


# ── R1: resolve_route 纯函数 ─────────────────────────────────────────────

class TestResolveRoute:
    def test_switch_off_returns_none(self):
        assert resolve_route(_PB_RAW, "BUY", 145.0,
                             {"entry_archetype_routing_enabled": False}) is None

    def test_pullback_limit_routes_limit(self):
        r = resolve_route(_PB_RAW, "BUY", 145.0, _ROUTE_CFG)
        assert r and r["action"] == "limit"
        assert r["limit_price"] == 140.5
        assert r["ttl_seconds"] == 300.0  # DA ttl_min=15 > 配置 300s → 配置生效
        assert r["archetype"] == "PULLBACK_LIMIT"

    def test_breakout_not_routed(self):
        raw = {"entry_archetype": "BREAKOUT", "confirmation": {"price_level": 200}}
        assert resolve_route(raw, "BUY", 198.0, _ROUTE_CFG) is None

    def test_no_archetype_not_routed(self):
        assert resolve_route({"thesis": "x"}, "BUY", 100.0, _ROUTE_CFG) is None

    def test_missing_confirmation_not_routed(self):
        raw = {"entry_archetype": "PULLBACK_LIMIT"}
        assert resolve_route(raw, "BUY", 145.0, _ROUTE_CFG) is None

    def test_level_conflict_buy(self):
        raw = {"entry_archetype": "PULLBACK_LIMIT",
               "confirmation": {"price_level": 150}, "chase_cap_price": 145}
        r = resolve_route(raw, "BUY", 144.0, _ROUTE_CFG)
        assert r and r["action"] == "skip" and r["reason"] == "archetype_level_conflict"

    def test_level_conflict_short(self):
        raw = {"entry_archetype": "FADE_SHORT",
               "confirmation": {"price_level": 60}, "chase_cap_price": 62}
        r = resolve_route(raw, "SHORT", 63.0, _ROUTE_CFG)
        assert r and r["action"] == "skip" and r["reason"] == "archetype_level_conflict"

    def test_deviation_too_large_not_routed(self):
        raw = {"entry_archetype": "PULLBACK_LIMIT", "confirmation": {"price_level": 100}}
        assert resolve_route(raw, "BUY", 145.0, _ROUTE_CFG) is None  # 偏离 31%

    def test_ttl_takes_shorter_of_da_and_config(self):
        raw = {"entry_archetype": "FADE_SHORT",
               "confirmation": {"price_level": 63.27, "ttl_min": 2}}
        r = resolve_route(raw, "SHORT", 61.0, _ROUTE_CFG)
        assert r["ttl_seconds"] == 120.0

    def test_json_string_input_and_garbage(self):
        r = resolve_route(json.dumps(_PB_RAW), "BUY", 145.0, _ROUTE_CFG)
        assert r and r["action"] == "limit"
        assert resolve_route("{not json", "BUY", 145.0, _ROUTE_CFG) is None
        assert parse_raw_semantics(None)["entry_archetype"] is None

    def test_fade_short_routes_limit(self):
        raw = {"entry_archetype": "FADE_SHORT",
               "confirmation": {"price_level": 63.27, "side": "above"}}
        r = resolve_route(raw, "SHORT", 61.0, _ROUTE_CFG)
        assert r and r["action"] == "limit" and r["limit_price"] == 63.27


# ── R2: 下单构造路由 (inner 层, 绕过上游闸门) ─────────────────────────────

def _call_inner(svc, db, iid, route):
    inst = svc._find_instruction_by_id(iid)
    return svc._execute_via_dual_broker_inner(
        inst, iid, inst["symbol"], inst["direction"],
        float(inst["entry_price"]), float(inst["stop_loss"]), float(inst["take_profit"]),
        int(inst["position_size"]), "LONG", "", "sim", archetype_route=route)


class TestOrderConstruction:
    def test_routed_order_is_limit_at_confirmation(self, svc, db):
        """路由指令 → OrderType.LIMIT @ 确认位; 订单被拒 → 终态跳过不记账."""
        iid = _save_inst(db)
        # 订单状态直接 CANCELLED → 等待循环走 archetype_order_dead 终态 (不进记账)
        dual, captured = _mk_dual([{"order_status": "CANCELLED", "dealt_qty": 0,
                                    "dealt_avg_price": 0}])
        route = {"action": "limit", "limit_price": 140.5, "ttl_seconds": 5,
                 "archetype": "PULLBACK_LIMIT", "chase_cap_price": None}
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), \
             patch("app.data.market_data_router.MarketDataRouter") as _mdr:
            _mdr.return_value.get_quote.return_value = None
            res = _call_inner(svc, db, iid, route)
        order = captured["order"]
        from app.core.models import OrderType
        assert order.order_type == OrderType.LIMIT
        assert order.price == 140.5
        assert order.quantity == 40
        assert res["success"] is False
        row = svc._find_instruction_by_id(iid)
        assert row["skip_reason"] == "archetype_order_dead"
        assert not row["executed"]  # 执行占位已释放

    def test_market_path_unchanged_without_route(self, svc, db):
        """无路由 = 市价现状 (零行为变更对照). 成交即时返回 → 不进确认窗."""
        iid = _save_inst(db)
        dual, captured = _mk_dual(
            [], submit_result={"success": True, "futu_order_id": "F2",
                               "filled_price": 145.0, "filled_qty": 40, "error": ""})
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual), \
             patch("app.data.market_data_router.MarketDataRouter") as _mdr, \
             patch.object(PlaybookService, "_execute_post_fill_side_effects",
                          create=True, return_value=None):
            _mdr.return_value.get_quote.return_value = None
            try:
                _call_inner(svc, db, iid, None)
            except Exception:
                pass  # 记账后段依赖 (非本测试范围); 订单构造断言不受影响
        from app.core.models import OrderType
        order = captured["order"]
        assert order.order_type == OrderType.MARKET
        assert order.price == 0.0


# ── R3-R7: _handle_routed_fill_wait 状态机 ────────────────────────────────

def _wait_kwargs(svc, db, iid, ttl=1):
    inst = svc._find_instruction_by_id(iid)
    route = {"action": "limit", "limit_price": 140.5, "ttl_seconds": ttl,
             "archetype": "PULLBACK_LIMIT", "chase_cap_price": None}
    return route, iid, inst["symbol"]


class TestFillWaitStateMachine:
    def test_filled_all_proceeds_with_deal_price(self, svc, db):
        iid = _save_inst(db)
        route, iid, sym = _wait_kwargs(svc, db, iid, ttl=30)
        dual, _ = _mk_dual([{"order_status": "FILLED_ALL", "dealt_qty": 40,
                             "dealt_avg_price": 140.3}])
        db.try_claim_execution(iid)
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual):
            rt = svc._handle_routed_fill_wait("F1", "sim", route, iid, sym, 40, "futu_simulate", "BUY")
        assert rt["proceed"] is True
        assert rt["fill_price"] == 140.3  # 真成交均价, 非限价名义
        assert rt["dealt_qty"] == 40
        assert rt["price_source"] == "routed_limit_deal"

    def test_ttl_expired_cancels_and_skips(self, svc, db):
        """TTL 内零成交 → 撤单 + skip_reason=archetype_ttl_expired + 释放占位."""
        iid = _save_inst(db)
        route, iid, sym = _wait_kwargs(svc, db, iid, ttl=1)
        dual, _ = _mk_dual([{"order_status": "SUBMITTED", "dealt_qty": 0,
                             "dealt_avg_price": 0}])
        db.try_claim_execution(iid)
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual):
            rt = svc._handle_routed_fill_wait("F1", "sim", route, iid, sym, 40, "futu_simulate", "BUY")
        assert rt["proceed"] is False
        assert rt["result"]["ttl_expired"] is True
        dual.cancel_order.assert_called_once()
        row = svc._find_instruction_by_id(iid)
        assert row["skip_reason"] == "archetype_ttl_expired"
        assert not row["executed"]

    def test_partial_at_deadline_books_dealt_cancels_rest(self, svc, db):
        iid = _save_inst(db)
        route, iid, sym = _wait_kwargs(svc, db, iid, ttl=1)
        dual, _ = _mk_dual([
            {"order_status": "SUBMITTED", "dealt_qty": 0, "dealt_avg_price": 0},   # 窗口内
            {"order_status": "FILLED_PART", "dealt_qty": 30, "dealt_avg_price": 140.5},  # 到期查
            {"order_status": "FILLED_PART", "dealt_qty": 30, "dealt_avg_price": 140.5},  # 竞态复查
        ])
        db.try_claim_execution(iid)
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual):
            rt = svc._handle_routed_fill_wait("F1", "sim", route, iid, sym, 40, "futu_simulate", "BUY")
        assert rt["proceed"] is True
        assert rt["dealt_qty"] == 30  # 按已成交量, 不追余量
        dual.cancel_order.assert_called_once()

    def test_race_fill_after_cancel_proceeds(self, svc, db):
        """撤单请求与成交交叉 — 复查到 FILLED_ALL 按成交记账."""
        iid = _save_inst(db)
        route, iid, sym = _wait_kwargs(svc, db, iid, ttl=1)
        dual, _ = _mk_dual([
            {"order_status": "SUBMITTED", "dealt_qty": 0, "dealt_avg_price": 0},
            {"order_status": "SUBMITTED", "dealt_qty": 0, "dealt_avg_price": 0},
            {"order_status": "FILLED_ALL", "dealt_qty": 40, "dealt_avg_price": 140.5},
        ])
        db.try_claim_execution(iid)
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual):
            rt = svc._handle_routed_fill_wait("F1", "sim", route, iid, sym, 40, "futu_simulate", "BUY")
        assert rt["proceed"] is True and rt["dealt_qty"] == 40

    def test_order_dead_status_terminal(self, svc, db):
        iid = _save_inst(db)
        route, iid, sym = _wait_kwargs(svc, db, iid, ttl=30)
        dual, _ = _mk_dual([{"order_status": "REJECTED", "dealt_qty": 0,
                             "dealt_avg_price": 0}])
        db.try_claim_execution(iid)
        with patch("app.execution.dual_broker.get_dual_broker", return_value=dual):
            rt = svc._handle_routed_fill_wait("F1", "sim", route, iid, sym, 40, "futu_simulate", "BUY")
        assert rt["proceed"] is False
        row = svc._find_instruction_by_id(iid)
        assert row["skip_reason"] == "archetype_order_dead"
        assert not row["executed"]


# ── R8: 配置与终态集合默认值 ──────────────────────────────────────────────

class TestDefaults:
    def test_routing_disabled_by_default(self):
        assert routing_enabled() is False  # 读真实配置: 默认关闭

    def test_yaml_defaults(self):
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load((Path(__file__).resolve().parents[1]
                              / "config" / "unified_config.yaml").read_text())
        pb = cfg["playbook"]
        assert pb["entry_archetype_routing_enabled"] is False
        assert pb["entry_archetype_ttl_seconds"] == 300

    def test_terminal_skip_reasons_invalidated(self):
        from app.playbook.pipeline import INVALIDATED_SKIP_REASONS
        for reason in ("archetype_ttl_expired", "archetype_level_conflict",
                       "archetype_order_dead"):
            assert reason in INVALIDATED_SKIP_REASONS
