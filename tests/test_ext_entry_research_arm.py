"""SIM 研究臂四纪律用例 (2026-09-22 用户裁「SIM 研究臂」; DEB-20260920 §12 预注册).

纪律面 (每条至少一钉):
  ① 严格臂优先 — 严格候选吃满配额后研究臂零下单 (不挤占);
  ② 全链打标 — attempts / audit / open_positions+trade_records.metadata_json / 推送
     均带 gate_mode=research_momentum; 严格臂缺省 gate_mode=strict;
  ③ SIM-only 硬连线 — REAL (_mode().is_simulate=False) 下研究臂物理不可达 (fail-closed);
  ④ 预注册退场线 — 过 EXT_RESEARCH_ARM_UNTIL 日期自动停用 (代码内日期闸)。
人群定义: 仅 calendar_missing 入池; calendar_phase_mismatch 等其余子原因仍 fail-closed。
在仓帽: 研究臂每单前复查 _open_exposure_gate (严于严格臂的每轮一次)。

打桩纪律 (2026-09-22 NVDA 真下单事故后固化): 本文件**永不**触达真券商 —
`_open_trade_context` 一律显式钉 Fake; conftest `_block_live_broker_orders` 兜底。
"""
import contextlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

import scripts.extended_hours_entry_executor as executor


# ── 最小桩件 ──────────────────────────────────────────────────────────────────


class FakeMode:
    def __init__(self, is_simulate=True, is_live=False):
        self.is_simulate = is_simulate
        self.is_live = is_live

    def account_id(self):
        return 1

    def futu_trd_env(self):
        return "SIMULATE" if self.is_simulate else "REAL"


class DummyQuoteContext:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass


class FakeSeries:
    def __init__(self, value):
        self.values = (value,)


class FakeOrderResponse:
    def __init__(self, order_id):
        self._order_id = FakeSeries(order_id)

    def __getitem__(self, key):
        if key != "order_id":
            raise KeyError(key)
        return self._order_id


class FakeTradeContext:
    """显式打桩的券商口 (纪律: 不打真券商; 先例 test_extended_hours_order_execution)."""

    def __init__(self, place_order_id="910001"):
        self.place_order_id = place_order_id
        self.placed = []

    def order_list_query(self, **kwargs):
        class _Empty:
            empty = True

            def iterrows(self):
                return iter(())

        return 0, _Empty()

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return 0, FakeOrderResponse(self.place_order_id)

    def modify_order(self, *args, **kwargs):
        return 0, "ok"

    def close(self):
        pass


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(executor, "LOCK", tmp_path / "state.lock")
    monkeypatch.setattr(executor, "AUDIT", tmp_path / "entry-audit.jsonl")
    monkeypatch.setattr(executor, "PRICE_GATE_REJECTS", tmp_path / "price-rejects.jsonl")
    monkeypatch.setattr(executor, "PRETRADE_GATE_REJECTS", tmp_path / "pretrade-rejects.jsonl")
    monkeypatch.setattr(executor, "EARNINGS_GATE_REJECTS", tmp_path / "earnings-rejects.jsonl")
    monkeypatch.setattr(
        executor, "EARNINGS_NEAR_MISSES", tmp_path / "earnings-near-misses.jsonl"
    )
    monkeypatch.setattr(executor, "_PUSH_DEDUP", tmp_path / "dedup.json")


def _seed_earnings_calendar(monkeypatch, rows, error=None, today=None):
    day = today or executor._today_et()
    monkeypatch.setattr(
        executor, "_EARNINGS_CALENDAR_CACHE", {day: {"rows": rows, "error": error}}
    )


QUOTE = {
    "bid": 9.99, "ask": 10.02, "ask_volume": 100_000.0,
    "spread_pct": 0.3, "market_cap": 5_000_000_000.0,
}
STRUCTURE = {"vwap": 9.9, "volume_ratio": 1.2, "bars": 6}
# price 10.0 × vol 2M = $20M 成交额 ⇒ A 级 (≥A_GRADE_TURNOVER_USD); chg 5% ≥ 2%; vol ≥ 20万
CANDIDATE = {"symbol": "US.RSH", "px": 10.0, "chg": 5.0, "vol": 2_000_000.0,
             "prev_close": 9.52, "name": "Research Test Co"}


def _research_environment(monkeypatch, movers=None, earnings=None,
                          opened_today=None, mode=None, execute_impl=None):
    """collect 级研究臂公共桩: SIM 模式 + 下游闸放行 + _execute 捕获 (默认假成交)。

    返回 executed 列表 (每笔下单的 kwargs 快照)。所有闸常量显式钉死, 不随 .env.us 漂移。
    """
    executed = []

    def default_execute(symbol, qty, price, session, chg, vol, earn, dry,
                        position_usd=None, grade="B", structure=None,
                        gate_mode="strict"):
        executed.append({
            "symbol": symbol, "qty": qty, "price": price, "session": session,
            "earn": earn, "grade": grade, "gate_mode": gate_mode,
        })
        return {"ok": True, "buy_order_id": f"OID-{symbol}",
                "filled_qty": qty, "avg_fill_price": price}

    monkeypatch.setattr(executor, "ALLOW_SIM_ENTRY", True)
    monkeypatch.setattr(executor, "RESEARCH_ARM_ON", True)
    monkeypatch.setattr(executor, "RESEARCH_ARM_UNTIL", "2026-10-31")
    monkeypatch.setattr(executor, "ENTRY_CHANGE_PCT", 2.0)
    monkeypatch.setattr(executor, "MIN_VOLUME", 200_000)
    monkeypatch.setattr(executor, "MIN_PRICE", 5.0)
    monkeypatch.setattr(executor, "MIN_EXT_TURNOVER_USD", 10_000_000.0)
    monkeypatch.setattr(executor, "A_GRADE_TURNOVER_USD", 20_000_000.0)
    monkeypatch.setattr(executor, "MIN_MARKET_CAP_USD", 1_000_000_000.0)
    monkeypatch.setattr(executor, "MAX_PER_DAY", 3)
    monkeypatch.setattr(executor, "PREMARKET_MAX_PER_DAY", 3)
    monkeypatch.setattr(executor, "MAX_OPEN", 5)
    monkeypatch.setattr(executor, "MAX_OPEN_NOTIONAL_USD", 25_000.0)
    fake_mode = mode if mode is not None else FakeMode()
    monkeypatch.setattr(executor, "_mode", lambda: fake_mode)
    monkeypatch.setattr(executor, "_ext_real_authorized", lambda: False)
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_entry_window_reason", lambda session, grade: None)
    monkeypatch.setattr(executor, "_held_symbols", lambda: set())
    monkeypatch.setattr(executor, "_open_exposure_gate", lambda: None)
    monkeypatch.setattr(executor, "_state_lock",
                        contextlib.contextmanager(lambda: (yield True)))
    monkeypatch.setattr(
        executor, "_load_state",
        lambda: {"version": 2, "opened_today": dict(opened_today or {}),
                 "attempts": {}},
    )
    saved = []
    monkeypatch.setattr(executor, "_save_state",
                        lambda state, state_path=None: saved.append(state))
    monkeypatch.setattr(
        executor, "_scan_candidates",
        lambda session, qctx: list(movers if movers is not None else [CANDIDATE]),
    )
    monkeypatch.setattr(executor, "_liquidity_quote", lambda qctx, symbol: dict(QUOTE))
    monkeypatch.setattr(
        executor, "_entry_structure_gate",
        lambda qctx, symbol, session, price: (dict(STRUCTURE), None, dict(STRUCTURE)),
    )
    if earnings is not None:
        monkeypatch.setattr(executor, "_earnings_surprise", earnings)
    else:
        # 空日历 ⇒ 全候选 calendar_missing (研究臂目标人群)
        _seed_earnings_calendar(monkeypatch, [])
    monkeypatch.setattr(executor, "_open_trade_context", lambda: FakeTradeContext())
    monkeypatch.setattr(
        executor, "_reconcile_broker_entry_orders", lambda *a, **k: ([], [])
    )
    monkeypatch.setattr(executor, "_execute", execute_impl or default_execute)
    monkeypatch.setattr("futu.OpenQuoteContext", DummyQuoteContext)
    executor._saved_states = saved   # 供断言 (tests 内部使用)
    return executed


# ── 纪律③/④ + 默认关: 启用判据 ────────────────────────────────────────────────


def test_research_arm_env_default_is_fail_closed():
    """源码契约钉: env 缺省必须是 false (fail-closed), 死线缺省 2026-10-31."""
    src = (executor.ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()
    assert 'os.getenv("EXT_ENTRY_RESEARCH_MOMENTUM", "false")' in src
    assert 'os.getenv("EXT_RESEARCH_ARM_UNTIL", "2026-10-31")' in src


def test_research_arm_active_requires_env_sim_and_deadline(monkeypatch):
    monkeypatch.setattr(executor, "RESEARCH_ARM_UNTIL", "2026-10-31")
    monkeypatch.setattr(executor, "_mode", lambda: FakeMode())

    monkeypatch.setattr(executor, "RESEARCH_ARM_ON", False)
    assert executor._research_arm_active() is False

    monkeypatch.setattr(executor, "RESEARCH_ARM_ON", True)
    assert executor._research_arm_active() is True

    # 纪律③: REAL / 非 SIM ⇒ fail-closed
    monkeypatch.setattr(executor, "_mode",
                        lambda: FakeMode(is_simulate=False, is_live=True))
    assert executor._research_arm_active() is False

    # 纪律③加强: _mode() 判定抛异常也 fail-closed
    def _boom():
        raise RuntimeError("mode unresolved")
    monkeypatch.setattr(executor, "_mode", _boom)
    assert executor._research_arm_active() is False

    # 纪律④: 过死线自动停用
    monkeypatch.setattr(executor, "_mode", lambda: FakeMode())
    monkeypatch.setattr(executor, "RESEARCH_ARM_UNTIL", "2026-09-21")
    assert executor._research_arm_active() is False


# ── 纪律②: 研究臂下单 + 全链打标 ─────────────────────────────────────────────


def test_collect_real_orders_calendar_missing_candidate_with_tag(
    isolated_state, monkeypatch
):
    executed = _research_environment(monkeypatch)
    today = executor._today_et()

    results = executor.collect("premarket")

    assert len(executed) == 1
    assert executed[0]["symbol"] == "US.RSH"
    assert executed[0]["gate_mode"] == "research_momentum"
    assert executed[0]["earn"]["surprise_pct"] is None          # 无财报事件存根
    assert executed[0]["earn"]["gate_mode"] == "research_momentum"
    ok_rows = [r for r in results if r.get("ok")]
    assert ok_rows and ok_rows[0]["gate_mode"] == "research_momentum"
    # attempts 打标 + opened_today 计数
    state = executor._saved_states[-1]
    attempt = state["attempts"][f"{today}:US.RSH"]
    assert attempt["gate_mode"] == "research_momentum"
    assert attempt["status"] == "opened"
    assert state["opened_today"][f"{today}:premarket"] == 1


def test_collect_dry_lists_research_candidates_without_state(
    isolated_state, monkeypatch
):
    _research_environment(monkeypatch)

    results = executor.collect("premarket", dry=True)

    research_rows = [r for r in results if r.get("gate_mode") == "research_momentum"]
    assert len(research_rows) == 1
    assert research_rows[0]["dry"] is True
    assert research_rows[0]["symbol"] == "US.RSH"
    assert not executor.STATE.exists()


def test_execute_tags_audit_and_db_metadata(isolated_state, tmp_path, monkeypatch):
    """_execute 级: gate_mode 贯通 audit jsonl + open_positions/trade_records metadata."""
    db_path = tmp_path / "entry.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE open_positions (
            symbol TEXT PRIMARY KEY, position_id TEXT, direction TEXT, size REAL,
            avg_entry_price REAL, entry_time TEXT, strategy_id TEXT, source TEXT,
            owner_strategy TEXT, owner TEXT, mode TEXT, account_type TEXT,
            trader_type TEXT, is_filled INTEGER, version INTEGER, updated_at TEXT,
            metadata_json TEXT, sync_status TEXT, initial_sl REAL, current_sl REAL,
            stop_loss_price REAL
        );
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, symbol TEXT,
            direction TEXT, entry_time TEXT, exit_time TEXT, entry_price REAL,
            exit_price REAL, quantity REAL, realized_pnl REAL, pnl_pct REAL,
            exit_reason TEXT, holding_hours REAL, source_type TEXT,
            trade_mode TEXT, mode TEXT, owner_strategy TEXT, strategy_id TEXT,
            futu_order_id TEXT, close_futu_order_id TEXT, metadata_json TEXT,
            mfe_pct REAL, mae_pct REAL, max_drawdown REAL, created_at TEXT,
            config_version TEXT, entry_session TEXT
        );
        """
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(executor, "DB", db_path)
    monkeypatch.setattr(executor, "_mode", lambda: FakeMode())
    monkeypatch.setattr(executor, "_open_trade_context", lambda: FakeTradeContext())
    monkeypatch.setattr(
        executor, "_reconcile_broker_entry_orders", lambda *a, **k: ([], [])
    )
    monkeypatch.setattr(
        executor, "wait_order_fill",
        lambda *a, **k: {"filled": True, "filled_qty": 100, "avg_fill_price": 10.0,
                         "status": "FILLED_ALL", "remaining_qty": 0,
                         "cancel_ok": True, "error": ""},
    )

    rec = executor._execute(
        "US.RSH", 100, 10.0, "premarket", 5.0, 2_000_000.0,
        {"gate_mode": "research_momentum", "surprise_pct": None}, False,
        gate_mode="research_momentum",
    )

    assert rec["ok"] is True and rec["gate_mode"] == "research_momentum"
    audit_rows = [
        json.loads(line)
        for line in executor.AUDIT.read_text().splitlines()
    ]
    assert audit_rows[-1]["gate_mode"] == "research_momentum"
    conn = sqlite3.connect(db_path)
    try:
        pos_meta = json.loads(conn.execute(
            "SELECT metadata_json FROM open_positions WHERE symbol='US.RSH'"
        ).fetchone()[0])
        trade_meta = json.loads(conn.execute(
            "SELECT metadata_json FROM trade_records WHERE symbol='US.RSH'"
        ).fetchone()[0])
    finally:
        conn.close()
    assert pos_meta["gate_mode"] == "research_momentum"
    assert trade_meta["gate_mode"] == "research_momentum"
    # 退出链关键键不受打标影响
    assert trade_meta["stop_loss"] == pos_meta["stop_loss"] == 9.5
    assert pos_meta["earnings_surprise_pct"] is None


def test_execute_strict_default_gate_mode(isolated_state, monkeypatch):
    """严格臂缺省 gate_mode=strict — 既有调用方零改动, 打标不缺位."""
    captured = {}

    def fake_write(*args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return "EXT-test"

    monkeypatch.setattr(executor, "_mode", lambda: FakeMode())
    monkeypatch.setattr(executor, "_open_trade_context", lambda: FakeTradeContext())
    monkeypatch.setattr(
        executor, "_reconcile_broker_entry_orders", lambda *a, **k: ([], [])
    )
    monkeypatch.setattr(
        executor, "wait_order_fill",
        lambda *a, **k: {"filled": True, "filled_qty": 10, "avg_fill_price": 10.0,
                         "status": "FILLED_ALL", "remaining_qty": 0,
                         "cancel_ok": True, "error": ""},
    )
    monkeypatch.setattr(executor, "_write_open_position", fake_write)

    rec = executor._execute(
        "US.STRICTX", 10, 10.0, "premarket", 5.0, 1_000_000.0,
        {"surprise_pct": 8.0}, False,
    )

    assert rec["ok"] is True and rec["gate_mode"] == "strict"
    assert captured["gate_mode"] == "strict"


# ── 纪律①: 严格臂优先, 研究臂只吃剩余配额 ────────────────────────────────────


def test_strict_arm_consumes_quota_before_research(isolated_state, monkeypatch):
    executed = _research_environment(
        monkeypatch,
        movers=[
            {"symbol": "US.STRICT", "px": 10.0, "chg": 5.0, "vol": 2_000_000.0,
             "prev_close": 9.52, "name": "Strict Co"},
            CANDIDATE,
        ],
        earnings=lambda symbol, session: (
            ({"surprise_pct": 8.0, "period": executor._today_et(),
              "period_age_days": 0, "report_date": executor._today_et(),
              "report_phase": "pre-market"}, None)
            if symbol == "US.STRICT" else (None, "calendar_missing")
        ),
    )
    monkeypatch.setattr(executor, "MAX_PER_DAY", 1)
    monkeypatch.setattr(executor, "PREMARKET_MAX_PER_DAY", 1)
    today = executor._today_et()
    _seed_earnings_calendar(monkeypatch, [
        {"symbol": "STRICT", "reportDate": today, "timeOfTheDay": "pre-market"},
    ])

    results = executor.collect("premarket")

    assert [e["symbol"] for e in executed] == ["US.STRICT"]      # 研究臂零挤占
    assert executed[0]["gate_mode"] == "strict"
    ok_rows = [r for r in results if r.get("ok")]
    assert len(ok_rows) == 1 and ok_rows[0]["symbol"] == "US.STRICT"


def test_research_arm_fills_remaining_quota_only(isolated_state, monkeypatch):
    """配额 2, 研究候选 3 ⇒ 只下 2 笔 (剩余配额口径)."""
    movers = [
        {"symbol": f"US.R{i}", "px": 10.0, "chg": 5.0, "vol": 2_000_000.0,
         "prev_close": 9.52, "name": f"Research {i}"}
        for i in range(3)
    ]
    executed = _research_environment(monkeypatch, movers=movers)
    monkeypatch.setattr(executor, "MAX_PER_DAY", 2)
    monkeypatch.setattr(executor, "PREMARKET_MAX_PER_DAY", 2)

    executor.collect("premarket")

    assert [e["symbol"] for e in executed] == ["US.R0", "US.R1"]
    assert all(e["gate_mode"] == "research_momentum" for e in executed)


# ── 人群定义: 仅 calendar_missing 入池 ───────────────────────────────────────


@pytest.mark.parametrize("sub_reason", [
    "calendar_phase_mismatch", "calendar_phase_unknown",
    "calendar_source_error", "surprise_nonpositive", "surprise_stale",
    "finnhub_error",
])
def test_non_calendar_missing_reasons_stay_fail_closed(
    isolated_state, monkeypatch, sub_reason
):
    executed = _research_environment(
        monkeypatch,
        earnings=lambda symbol, session: (None, sub_reason),
    )

    results = executor.collect("premarket")

    assert executed == []
    assert results == [{"skip": "no_signal_passed_all_gates"}]


def test_research_arm_off_by_env_blocks_orders(isolated_state, monkeypatch):
    """回滚开关: RESEARCH_ARM_ON=False ⇒ 行为与现状完全一致 (零下单)."""
    executed = _research_environment(monkeypatch)
    monkeypatch.setattr(executor, "RESEARCH_ARM_ON", False)

    results = executor.collect("premarket")

    assert executed == []
    assert results == [{"skip": "no_signal_passed_all_gates"}]


# ── 纪律③: REAL 下研究臂物理不可达 ───────────────────────────────────────────


def test_real_mode_collect_returns_before_research(isolated_state, monkeypatch):
    executed = _research_environment(
        monkeypatch, mode=FakeMode(is_simulate=False, is_live=True)
    )

    results = executor.collect("premarket")

    assert executed == []
    assert results == [{"ok": False, "skip": "real_not_authorized"}]


# ── 纪律④: 死线后自动停用 ────────────────────────────────────────────────────


def test_past_deadline_disables_research_orders(isolated_state, monkeypatch):
    executed = _research_environment(monkeypatch)
    monkeypatch.setattr(executor, "RESEARCH_ARM_UNTIL", "2026-09-21")   # 昨日

    results = executor.collect("premarket")

    assert executed == []
    assert results == [{"skip": "no_signal_passed_all_gates"}]


# ── 观测面: 闸计数行携带研究臂摘要 ───────────────────────────────────────────


def test_gate_counts_line_carries_research_summary(isolated_state, monkeypatch, capsys):
    """research_pool/research_ordered 是 earnings 拒者的子集标签 — 严格臂归因求和不受影响."""
    _research_environment(monkeypatch)

    executor.collect("premarket", dry=True)

    captured = capsys.readouterr().out
    lines = [l for l in captured.splitlines() if "闸计数" in l]
    assert lines, captured
    line = lines[-1]
    assert "research_pool=1" in line and "research_ordered=1" in line
    counts = {kv.split("=")[0]: int(kv.split("=")[1])
              for kv in line.split("候选=")[1].split()[1:]}
    derived = ("earnings_phase", "earnings_stale", "earnings_src_err")
    strict_attributed = sum(
        v for k, v in counts.items()
        if k not in derived and not k.startswith("research_")
    )
    assert strict_attributed == 1 == counts["earnings"]   # 单候选, 严格归因不变


# ── 在仓帽: 研究臂每单前复查 ─────────────────────────────────────────────────


def test_research_pass_rechecks_exposure_per_order(isolated_state, monkeypatch):
    """严格臂轮首检查放行后, 研究臂每单前复查; 复查拦截 ⇒ 零下单 + 打标 skip 落结果."""
    calls = {"n": 0}

    def gated_exposure():
        calls["n"] += 1
        if calls["n"] == 1:            # collect 轮首检查 (严格臂口径) → 放行
            return None
        return {"skip": "ext_open_position_limit", "open_positions": 5, "limit": 5}

    executed = _research_environment(monkeypatch)
    monkeypatch.setattr(executor, "_open_exposure_gate", gated_exposure)

    results = executor.collect("premarket")

    assert executed == []
    blocked = [r for r in results if r.get("skip") == "ext_open_position_limit"]
    assert blocked and blocked[0]["gate_mode"] == "research_momentum"
