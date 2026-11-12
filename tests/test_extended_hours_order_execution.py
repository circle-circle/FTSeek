import json
import contextlib
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.extended_hours_entry_executor as entry_executor
import scripts.extended_hours_exit_executor as exit_executor
import scripts.extended_hours_order_lifecycle as lifecycle


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows
        self.empty = not rows

    def iterrows(self):
        return iter(enumerate(self.rows))

    @property
    def iloc(self):
        return self

    def __getitem__(self, index):
        return self.rows[index]


class FakeOrderResponse:
    def __init__(self, order_id):
        self._order_id = FakeSeries(order_id)

    def __getitem__(self, key):
        if key != "order_id":
            raise KeyError(key)
        return self._order_id


class FakeSeries:
    def __init__(self, value):
        self.values = (value,)


class FakeTradeContext:
    def __init__(self, rows=None, place_order_id="900001", modify_ok=True):
        self.rows = rows or []
        self.place_order_id = place_order_id
        self.modify_ok = modify_ok
        self.placed = []

    def order_list_query(self, **kwargs):
        order_id = kwargs.get("order_id")
        rows = self.rows
        if order_id:
            rows = [row for row in rows if str(row.get("order_id")) == str(order_id)]
            rows = rows[-1:]
        return 0, FakeFrame(rows)

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return 0, FakeOrderResponse(self.place_order_id)

    def modify_order(self, *args, **kwargs):
        return (0 if self.modify_ok else -1), "ok" if self.modify_ok else "cancel failed"

    def close(self):
        pass


def _create_db(path):
    connection = sqlite3.connect(path)
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
            config_version TEXT, entry_session TEXT, decision_id TEXT
        );
        """
    )
    connection.commit()
    connection.close()


@pytest.fixture(autouse=True)
def _research_arm_pinned_off(monkeypatch):
    """Hermetic 钉 (2026-09-22, DEB-20260920 §12): .env.us 已带 EXT_ENTRY_RESEARCH_MOMENTUM=true,
    executor import 期读 env ⇒ 本文件 (严格臂订单执行面) 显式钉关; 研究臂用例见
    tests/test_ext_entry_research_arm.py (自行钉开)。"""
    monkeypatch.setattr(entry_executor, "RESEARCH_ARM_ON", False)
    # 同因 hermetic 钉 (2026-09-23 复查批): .env.us 已带 EXT_ENTRY_ALLOW_SHORT=true,
    # executor import 期读 env ⇒ 本文件显式钉回多侧 —— 否则 chg≥3% 的 fixture 票
    # (如 BIGCO +6.1%) 在 dry 观测臂被判 SHORT 方向, 触达未打桩的 `_earnings_calendar_event`
    # 被事件门拒 (实测 KeyError: 'symbol')。空侧用例见 tests/test_ext_entry_short_arm.py。
    monkeypatch.setattr(entry_executor, "SHORT_ALLOW", False)
    # 同因 hermetic 钉 (2026-09-23 RFC-101 取证批): `EXT_ENTRY_ALLOW_SIM=false` 时
    # collect() 会 fail_closed (2026-09-22 安全门), 而本文件的订单执行面用例期望
    # position_limit 分支 ⇒ 断言随操作者 env 漂移。取证: env EXT_ENTRY_ALLOW_SIM=false
    # 时 test_collect_blocks_extended_open_position_count 由绿转红 (实测)。
    monkeypatch.setattr(entry_executor, "ALLOW_SIM_ENTRY", True)


@pytest.fixture()
def entry_environment(tmp_path, monkeypatch):
    db_path = tmp_path / "entry.db"
    _create_db(db_path)
    monkeypatch.setattr(entry_executor, "DB", db_path)
    monkeypatch.setattr(entry_executor, "AUDIT", tmp_path / "entry-audit.jsonl")
    monkeypatch.setattr(entry_executor, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(entry_executor, "LOCK", tmp_path / "state.lock")
    monkeypatch.setattr(entry_executor, "_push", lambda text: None)
    monkeypatch.setattr(
        entry_executor, "_now_et",
        lambda: datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(entry_executor, "_open_trade_context", lambda: FakeTradeContext())
    monkeypatch.setattr(
        entry_executor, "_reconcile_broker_entry_orders", lambda *args, **kwargs: ([], [])
    )
    return db_path


def test_entry_full_fill_writes_position_and_trade_record(
    entry_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(
        entry_executor,
        "wait_order_fill",
        lambda *args, **kwargs: {
            "filled": True, "filled_qty": 100, "avg_fill_price": 10.0,
            "status": "FILLED_ALL", "remaining_qty": 0,
            "cancel_ok": True, "error": "",
        },
    )

    result = entry_executor._execute(
        "US.FAKE", 100, 10.0, "premarket", 3.0, 300000.0,
        {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
    )

    assert result["ok"] is True
    connection = sqlite3.connect(entry_environment)
    position = connection.execute(
        "SELECT position_id, size, avg_entry_price, initial_sl FROM open_positions"
    ).fetchone()
    trade = connection.execute(
        "SELECT quantity, entry_price, futu_order_id, exit_price, config_version, "
        "entry_session FROM trade_records"
    ).fetchone()
    connection.close()
    assert position[1:] == (100, 10.0, 9.5)
    assert trade == (100, 10.0, "900001", None, "v2", "premarket")


def test_entry_grade_controls_certainty_lock_metadata(
    entry_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(
        entry_executor,
        "wait_order_fill",
        lambda *args, **kwargs: {
            "filled": True, "filled_qty": 50, "avg_fill_price": 10.0,
            "status": "FILLED_ALL", "remaining_qty": 0,
            "cancel_ok": True, "error": "",
        },
    )

    result = entry_executor._execute(
        "US.FAKEA", 50, 10.0, "premarket", 3.0, 300000.0,
        {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
        position_usd=5000.0, grade="A", structure={"vwap": 10.0},
    )

    assert result["ok"] is True
    connection = sqlite3.connect(entry_environment)
    metadata = json.loads(connection.execute(
        "SELECT metadata_json FROM trade_records WHERE symbol='US.FAKEA'"
    ).fetchone()[0])
    connection.close()
    assert metadata["entry_grade"] == "A"
    assert metadata["entry_position_usd"] == 5000.0
    assert metadata["cert_lock_pct"] == 0.0
    assert metadata["entry_structure"] == {"vwap": 10.0}


def test_entry_partial_fill_writes_actual_filled_quantity(
    entry_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(
        entry_executor,
        "wait_order_fill",
        lambda *args, **kwargs: {
            "filled": True, "filled_qty": 40, "avg_fill_price": 10.2,
            "status": "CANCELLED_PART", "remaining_qty": 60,
            "cancel_ok": True, "error": "",
        },
    )

    result = entry_executor._execute(
        "US.FAKE", 100, 10.0, "premarket", 3.0, 300000.0,
        {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
    )

    assert result["ok"] is True
    connection = sqlite3.connect(entry_environment)
    size = connection.execute("SELECT size FROM open_positions").fetchone()[0]
    connection.close()
    assert size == 40


def test_entry_db_write_is_idempotent_and_never_replaces_position(
    entry_environment,
):
    first_id = entry_executor._write_open_position(
        "US.TEST", 100, 10.0, 10.0, "premarket", {}, 3.0, 300000.0, "900001"
    )
    repeat_id = entry_executor._write_open_position(
        "US.TEST", 100, 10.0, 10.0, "premarket", {}, 3.0, 300000.0, "900001"
    )
    assert repeat_id == first_id

    with pytest.raises(RuntimeError, match="already exists"):
        entry_executor._write_open_position(
            "US.TEST", 100, 10.0, 10.0, "premarket", {}, 3.0, 300000.0, "900002"
        )


def test_wait_fill_cancels_partial_and_confirms_terminal_state():
    context = FakeTradeContext(
        rows=[{"order_id": "900001", "code": "US.TEST", "order_status": "FILLED_PART",
               "qty": 100, "dealt_qty": 40, "dealt_avg_price": 10.2, "price": 10.0}]
    )
    context.rows.append(
        {"order_id": "900001", "code": "US.TEST", "order_status": "CANCELLED_PART",
         "qty": 100, "dealt_qty": 40, "dealt_avg_price": 10.2, "price": 10.0}
    )

    result = lifecycle.wait_order_fill(
        context, "SIMULATE", 19930823, "900001", 100, 10.0,
        timeout_s=0.1, poll_s=0,
    )

    assert result["filled"] is True
    assert result["filled_qty"] == 40
    assert result["cancel_ok"] is True
    assert result["status"] == "CANCELLED_PART"


def test_wait_fill_reports_cancel_failure():
    context = FakeTradeContext(
        rows=[{"order_id": "900001", "code": "US.TEST", "order_status": "SUBMITTED",
               "qty": 100, "dealt_qty": 0, "dealt_avg_price": 0, "price": 10.0}],
        modify_ok=False,
    )

    result = lifecycle.wait_order_fill(
        context, "SIMULATE", 19930823, "900001", 100, 10.0,
        timeout_s=0, poll_s=0,
    )

    assert result["filled"] is False
    assert result["cancel_ok"] is False
    assert "cancel_request_failed" in result["error"]


def test_entry_reconciliation_restores_attempt_state_from_active_broker_order():
    context = FakeTradeContext(
        rows=[{"order_id": "8185070", "code": "US.FAKE", "order_status": "SUBMITTED",
               "qty": 100, "dealt_qty": 0, "dealt_avg_price": 0, "price": 10.0,
               "remark": "ext_entry", "create_time": "2026-09-17 08:00:00"}]
    )
    state = {"version": 2, "opened_today": {}, "attempts": {}}

    active_orders, conflicts = entry_executor._reconcile_broker_entry_orders(
        context, "SIMULATE", 19930823, "premarket",
        state=state, today="2026-09-17",
    )

    assert conflicts == []
    assert active_orders[0]["order_id"] == "8185070"
    attempt = state["attempts"]["2026-09-17:US.FAKE"]
    assert attempt["status"] == "broker_active"
    assert attempt["order_id"] == "8185070"


def test_entry_reconciliation_ignores_prior_date_terminal_order():
    """回归 (2026-09-17 Hermes 复查实证): 跨日已撤旧单**不得**写 {today}:{symbol} attempt.

    病灶: 曾把 ET 9/16 23:14 的旧单记进 9/17 → 采集循环 `if attempt_key in attempts: continue`
    使该标的被静默永久跳过 (实测 US.NOK)。create_time 不可解析时同理 (宁可漏记不可误封锁)。
    """
    context = FakeTradeContext(rows=[
        {"order_id": "8184987", "code": "US.NOK", "order_status": "CANCELLED_ALL",
         "qty": 1, "dealt_qty": 0, "dealt_avg_price": 0, "price": 10.4,
         "remark": "ext_entry", "create_time": "2026-09-16 23:14:45"},
        {"order_id": "8184000", "code": "US.FOO", "order_status": "CANCELLED_ALL",
         "qty": 1, "dealt_qty": 0, "dealt_avg_price": 0, "price": 5.0,
         "remark": "ext_entry", "create_time": ""},
    ])
    state = {"version": 2, "opened_today": {}, "attempts": {}}

    active_orders, conflicts = entry_executor._reconcile_broker_entry_orders(
        context, "SIMULATE", 19930823, "premarket",
        state=state, today="2026-09-17",
    )

    assert active_orders == []
    assert conflicts == []
    assert state["attempts"] == {}


def test_entry_reconciliation_keeps_same_date_terminal_attempt():
    """同一意图的另一面: **当日**已撤单仍要记 attempt (Codex §13 崩溃恢复意图不得被削弱)。"""
    context = FakeTradeContext(rows=[
        {"order_id": "8185069", "code": "US.MEDS", "order_status": "CANCELLED_ALL",
         "qty": 1237, "dealt_qty": 0, "dealt_avg_price": 0, "price": 4.04,
         "remark": "ext_entry", "create_time": "2026-09-17 00:06:31"},
    ])
    state = {"version": 2, "opened_today": {}, "attempts": {}}

    entry_executor._reconcile_broker_entry_orders(
        context, "SIMULATE", 19930823, "premarket",
        state=state, today="2026-09-17",
    )

    assert state["attempts"]["2026-09-17:US.MEDS"]["status"] == "failed"


def test_entry_reconciliation_active_order_still_blocks_across_dates():
    """活单**无条件**计入 active_orders (拦住新开仓) — 与订单日期无关 (孤儿活单先例)。"""
    context = FakeTradeContext(rows=[
        {"order_id": "8185070", "code": "US.FAKE", "order_status": "SUBMITTED",
         "qty": 100, "dealt_qty": 0, "dealt_avg_price": 0, "price": 10.0,
         "remark": "ext_entry", "create_time": "2026-09-16 23:00:00"},
    ])
    state = {"version": 2, "opened_today": {}, "attempts": {}}

    active_orders, _ = entry_executor._reconcile_broker_entry_orders(
        context, "SIMULATE", 19930823, "premarket",
        state=state, today="2026-09-17",
    )

    assert [o["order_id"] for o in active_orders] == ["8185070"]
    assert state["attempts"] == {}


def test_reconciliation_without_today_keeps_legacy_full_scan(tmp_path, monkeypatch):
    """回归 (2026-09-17 实盘 16:15 发现): `_execute` 的下单前安全闸调用对账时**不传 today/state**。

    若此时仍按"日期过滤"处理, `_order_et_date(...) == None` 恒 False ⇒ **当天单也被判跨日全部跳过**
    (当日已成交无主单的建仓回补 + 冲突检测失效, 且日志把它误报成"跨日旧单").
    约定: today 为空 ⇒ 调用方未要求按日过滤 ⇒ 保持全量处理(旧行为).
    """
    db_path = tmp_path / "rec.db"
    _create_db(db_path)
    monkeypatch.setattr(entry_executor, "DB", db_path)
    calls = []
    monkeypatch.setattr(entry_executor, "_write_open_position",
                        lambda **kw: calls.append(kw) or "EXT-test")

    context = FakeTradeContext(rows=[
        {"order_id": "8185449", "code": "US.CTNT", "order_status": "FILLED_ALL",
         "qty": 68493, "dealt_qty": 68493, "dealt_avg_price": 0.072, "price": 0.073,
         "remark": "ext_entry", "create_time": "2026-09-17 04:05:11"},
    ])

    active_orders, conflicts = entry_executor._reconcile_broker_entry_orders(
        context, "SIMULATE", 19930823, "premarket",  # 不传 state/today (同 _execute 安全闸)
    )

    assert active_orders == [] and conflicts == []
    assert len(calls) == 1                      # 仍须尝试回补建仓, 未被误判为跨日
    assert calls[0]["symbol"] == "US.CTNT"
    assert calls[0]["qty"] == 68493


def test_opened_today_total_merges_sessions_and_ignores_other_dates():
    """用户 9/17 令"配额提升到 5" = 每日**合计**(盘前+盘后共享同一上限), 不是单时段各 5。"""
    opened = {"2026-09-17:premarket": 2, "2026-09-17:afterhours": 3, "2026-09-16:premarket": 4}
    assert entry_executor._opened_today_total(opened, "2026-09-17") == 5
    assert entry_executor._opened_today_total({}, "2026-09-17") == 0
    assert entry_executor._opened_today_total(opened, "2026-09-16") == 4   # 跨日残留 key 不计


def test_daily_quota_env_override_enables_small_batch_observation():
    """观察期可把日配额压小；测试用子进程隔离，避免本机 .env.us 漂移成假红。"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import scripts.extended_hours_entry_executor as e; print(e.MAX_PER_DAY)",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            "EXT_ENTRY_MAX_PER_DAY": "3",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "3"


def test_new_sources_default_off_until_replay_evidence():
    """新源默认关(待回放证据), 现役异动榜保持开 —— 用户 9/17「需要」的落地口径, 可 env 开。"""
    assert entry_executor.SOURCE_RANK_BOARD is True
    assert entry_executor.SOURCE_BEAT_RANK is False
    assert entry_executor.SOURCE_WATCHLIST is False


def test_beat_rank_symbols_filters_stale_and_microcap(monkeypatch):
    """事件源: 必须过滤 ①过期披露(不加就是跟 1-2 个月前的事件交易) ②微盘 ③非美股。"""
    import pandas as pd

    class _Ctx:
        def get_earnings_beat_rank(self, market, beat_type, count=None):
            return 0, (1455, pd.DataFrame([
                {"security": "US.FRESH", "released_date": "2026-09-15", "market_cap": 5e9},
                {"security": "US.STALE", "released_date": "2026-07-30", "market_cap": 5e9},
                {"security": "US.TINY", "released_date": "2026-09-15", "market_cap": 5e7},
                {"security": "HK.00700", "released_date": "2026-09-15", "market_cap": 5e11},
            ]))

    monkeypatch.setattr(entry_executor, "_now_et",
                        lambda: datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc))
    assert entry_executor._beat_rank_symbols(_Ctx()) == ["US.FRESH"]


def test_watchlist_symbols_reads_configured_group_and_keeps_us_only():
    import pandas as pd

    class _Ctx:
        def __init__(self):
            self.group = None

        def get_user_security(self, group_name):
            self.group = group_name
            return 0, pd.DataFrame([{"code": "US.AMD"}, {"code": "HK.00700"}, {"code": "US.NVDA"}])

    ctx = _Ctx()
    assert entry_executor._watchlist_symbols(ctx) == ["US.AMD", "US.NVDA"]
    assert ctx.group == entry_executor.WATCHLIST_GROUP


def test_scan_candidates_merges_sources_in_priority_order_and_dedupes(monkeypatch):
    """合并器: 源顺序=优先级, 重复标的保留先出现的口径, 已合并的不再补快照 (省接口调用)。"""
    monkeypatch.setattr(entry_executor, "SOURCE_RANK_BOARD", True)
    monkeypatch.setattr(entry_executor, "SOURCE_BEAT_RANK", True)
    monkeypatch.setattr(entry_executor, "SOURCE_WATCHLIST", True)
    # 隔离日历源 (2026-09-23 生产 .env.us 已开 calendar/finnhub_calendar 两源):
    # 本用例只测合并器优先级/去重 —— 不关闸则真日历票(US.CBRL 等)漏进桩池, 隔日必红。
    monkeypatch.setattr(entry_executor, "SOURCE_CALENDAR", False)
    monkeypatch.setattr(entry_executor, "SOURCE_FINNHUB_CALENDAR", False)
    monkeypatch.setattr(entry_executor, "_scan_movers", lambda s, q: [
        {"symbol": "US.A", "px": 10.0, "chg": 3.0, "vol": 3e5, "prev_close": 9.7}])
    monkeypatch.setattr(entry_executor, "_beat_rank_symbols", lambda q: ["US.B", "US.A"])
    monkeypatch.setattr(entry_executor, "_watchlist_symbols", lambda q: ["US.C"])

    seen = {}

    def fake_snap(session, qctx, syms):
        seen["syms"] = list(syms)
        return [{"symbol": s, "px": 20.0, "chg": 5.0, "vol": 5e5, "prev_close": 19.0} for s in syms]

    monkeypatch.setattr(entry_executor, "_snapshot_candidates", fake_snap)

    out = entry_executor._scan_candidates("premarket", object())

    assert [c["symbol"] for c in out] == ["US.A", "US.B", "US.C"]
    assert seen["syms"] == ["US.B", "US.C"]      # US.A 已由异动榜提供, 不重复快照
    assert out[0]["px"] == 10.0                  # 保留先到源(异动榜)的口径


def test_price_gate_blocks_penny_stocks_and_over_budget():
    """2026-09-17 用户裁方案A: 价格下限闸 $5 (挡仙股) + 上限 = 单笔仓位 $5000。"""
    assert entry_executor._price_in_range(0.073) is False   # US.CTNT 实盘样例
    assert entry_executor._price_in_range(0.435) is False   # US.GIPR 实盘样例
    assert entry_executor._price_in_range(4.99) is False
    assert entry_executor._price_in_range(5.0) is True      # 边界含等号
    assert entry_executor._price_in_range(42.0) is True
    assert entry_executor._price_in_range(5000.0) is True
    assert entry_executor._price_in_range(0) is False       # 无效价
    assert entry_executor._price_in_range(-1.0) is False
    assert entry_executor._price_in_range(5000.01) is False  # 超过单笔仓位, 连 1 股都买不起


def test_price_gate_reject_recording_is_per_run_aggregate(tmp_path, monkeypatch, capsys):
    """打点一行 = 一次运行 (n + 明细), 供按日聚合"$5 闸挡掉多少噪声票" (用户 9/17 令)。"""
    log = tmp_path / "rejects.jsonl"
    monkeypatch.setattr(entry_executor, "PRICE_GATE_REJECTS", log)

    entry_executor._record_price_gate_rejects("premarket", [])
    assert not log.exists()  # 没有拦下 = 不打点, 不留噪声

    entry_executor._record_price_gate_rejects("premarket", [
        {"symbol": "US.CTNT", "price": 0.073, "change_pct": 75.06, "volume": 22167799.0},
        {"symbol": "US.GIPR", "price": 0.435, "change_pct": 19.75, "volume": 318404.0},
    ])

    rec = json.loads(log.read_text().strip())
    assert rec["n"] == 2 and rec["session"] == "premarket"
    assert [r["symbol"] for r in rec["rejects"]] == ["US.CTNT", "US.GIPR"]
    assert "价格闸" in capsys.readouterr().out


def test_collect_dry_records_penny_candidates_blocked_by_price_gate(tmp_path, monkeypatch):
    """方案A 端到端: 扫描出的仙股必须被价格闸挡掉, 不得进入候选, 且要留下打点 (实盘 CTNT/GIPR 教训)。"""

    class _FakeQuote:
        """快照桩 (2026-09-18 §23.5 顺带修): 原桩只有 close(), 盘前 pretrade 闸取不到报价
        ⇒ BIGCO 被判 quote_missing, 用例自 2026-09-18 起恒红。这里补上行情快照。"""

        def close(self):
            pass

        def get_market_snapshot(self, symbols):
            class Row:
                @staticmethod
                def get(key, default=0):
                    return {
                        "bid_price": 41.99,
                        "ask_price": 42.01,
                        "ask_vol": 100_000,
                        "last_price": 42.0,
                        "prev_close_price": 42.0,
                        "total_market_val": 5_000_000_000,
                    }.get(key, default)

            class Iloc:
                def __getitem__(self, _index):
                    return Row()

            class Snapshot:
                empty = False
                iloc = Iloc()

            return 0, Snapshot()

    rejects_path = tmp_path / "rejects.jsonl"
    monkeypatch.setattr(entry_executor, "PRICE_GATE_REJECTS", rejects_path)
    monkeypatch.setattr(entry_executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(entry_executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(entry_executor, "_held_symbols", lambda: [])
    # 同上: 生产 .env.us 开了日历双源 ⇒ 不关闸则真财报票绕过 _scan_movers 桩直灌候选
    # (实测 Finnhub 4 只 + 静态日历 5 只, 且其中部分行缺 symbol 键致断言 KeyError)。
    monkeypatch.setattr(entry_executor, "SOURCE_CALENDAR", False)
    monkeypatch.setattr(entry_executor, "SOURCE_FINNHUB_CALENDAR", False)
    # 同因第三处: 生产 .env.us 现设 EXT_ENTRY_CHG_BAND=0.5,2.0 ⇒ BIGCO(+6.10%) 会落带外
    # 被 momentum_volume 拒 (ordered=0)。本用例锚的是"仙股被价格闸挡、正常票进执行",
    # 与涨幅带无关 ⇒ 钉回旧下限口径 (空带 → ≥ ENTRY_CHANGE_PCT)。
    monkeypatch.setattr(entry_executor, "ENTRY_CHG_BAND", "")
    monkeypatch.setattr(entry_executor, "ENTRY_CHANGE_PCT", 2.0)
    monkeypatch.setattr(entry_executor, "_scan_movers", lambda session, qctx: [
        {"symbol": "US.CTNT", "px": 0.073, "chg": 75.06, "vol": 22167799.0},
        {"symbol": "US.GIPR", "px": 0.435, "chg": 19.75, "vol": 318404.0},
        {"symbol": "US.BIGCO", "px": 42.0, "chg": 6.10, "vol": 900000.0},
    ])
    monkeypatch.setattr(entry_executor, "_earnings_surprise",
                        lambda symbol, session: (
                            {"surprise_pct": 10.0, "period": "2026-06-30"}, None))
    monkeypatch.setattr(entry_executor, "_entry_window_reason", lambda session, grade: None)
    monkeypatch.setattr(
        entry_executor,
        "_entry_structure_gate",
        lambda qctx, symbol, session, price: (
            {"vwap": 42.0, "last_close": 42.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0}, None,
            {"vwap": 42.0, "last_close": 42.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0},
        ),
    )
    import futu
    monkeypatch.setattr(futu, "OpenQuoteContext", lambda **kw: _FakeQuote())

    results = entry_executor.collect("premarket", dry=True)

    assert [r["symbol"] for r in results] == ["US.BIGCO"]
    assert results[0]["qty"] == int(entry_executor.POSITION_USD / 42.0)
    rec = json.loads(rejects_path.read_text().strip())
    assert [r["symbol"] for r in rec["rejects"]] == ["US.CTNT", "US.GIPR"]  # 含原因明细


@pytest.fixture()
def exit_environment(tmp_path, monkeypatch):
    db_path = tmp_path / "exit.db"
    _create_db(db_path)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO open_positions (symbol, position_id, direction, size, "
        "avg_entry_price, entry_time, strategy_id, source, owner_strategy, owner, "
        "mode, account_type, trader_type, is_filled, version, updated_at, "
        "sync_status) VALUES ('US.TEST', 'EXT-1', 'LONG', 100, 10.0, "
        "'2026-09-17T08:00:00-04:00', 'extended_hours_entry', 'test', 'test', "
        "'test', 'normal', 'simulate', 'AUTO', 1, 1, '2026-09-17', 'pending')"
    )
    connection.execute(
        "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, "
        "entry_price, quantity, source_type, trade_mode, mode, owner_strategy, "
        "strategy_id, futu_order_id, created_at) VALUES ('2026-09-17', 'US.TEST', "
        "'LONG', '2026-09-17T08:00:00-04:00', 10.0, 100, 'PLAYBOOK_SIM', "
        "'simulate', 'futu_simulate', 'extended_hours_entry', "
        "'extended_hours_entry', '900001', '2026-09-17')"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(exit_executor, "DB", db_path)
    monkeypatch.setattr(exit_executor, "_AUDIT_LOG", tmp_path / "exit-audit.jsonl")
    monkeypatch.setattr(exit_executor, "_push", lambda text: None)
    monkeypatch.setattr(exit_executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(exit_executor, "_in_session_window", lambda session: True)

    def fail_global_broker():
        raise AssertionError("exit executor tests must inject a durable broker")

    monkeypatch.setattr(exit_executor, "_get_dual_broker", fail_global_broker)
    return db_path


class FakeDurableBroker:
    def __init__(self):
        self.calls = []

    def close_position(self, symbol, qty, direction, **kwargs):
        self.calls.append({"symbol": symbol, "qty": qty, "direction": direction, **kwargs})
        owner = kwargs["exit_owner"]
        dealt_qty = float(kwargs.pop("_fake_dealt_qty", qty))
        success = dealt_qty == float(qty)
        return {
            "success": success,
            "order_filled": success,
            "error": "" if success else "exit_eth_partial_fill_held",
            "exit_state": "FILLED_UNBOOKED" if success else "HELD",
            "exit_intent_id": "fake-intent",
            "futu_order_id": "900003",
            "filled_price": 11.0,
            "dealt_qty": dealt_qty,
            "price_source": "broker_limit_deal",
            "confirmed_at": 1789238400.0,
            "broker_qty_before": float(qty),
            "broker_qty_after": float(qty) - dealt_qty,
            "owner": owner,
            "decision": kwargs["exit_decision"],
        }

    def acknowledge_close(self, intent_id, receipt):
        return intent_id == "fake-intent" and bool(receipt)


def _install_durable_broker(monkeypatch, dealt_qty=None):
    broker = FakeDurableBroker()
    if dealt_qty is not None:
        original_close = broker.close_position

        def close_position(*args, **kwargs):
            kwargs["_fake_dealt_qty"] = dealt_qty
            return original_close(*args, **kwargs)

        broker.close_position = close_position
    monkeypatch.setattr(exit_executor, "_get_dual_broker", lambda: broker)
    return broker


def test_exit_full_fill_updates_records_and_removes_position(
    exit_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    broker = _install_durable_broker(monkeypatch)

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["ok"] is True
    assert result["fully_filled"] is True
    connection = sqlite3.connect(exit_environment)
    positions = connection.execute("SELECT COUNT(*) FROM open_positions").fetchone()[0]
    trade = connection.execute(
        "SELECT exit_price, exit_reason, close_futu_order_id, realized_pnl "
        "FROM trade_records WHERE futu_order_id='900001'"
    ).fetchone()
    connection.close()
    assert positions == 0
    # RFC-142 exit_reason 归一化层 (app/risk/exit_reasons.py): lock_in → SL_TRAIL_LOCK 入库
    assert trade == (11.0, "SL_TRAIL_LOCK", "900003", 100.0)
    with exit_executor._AUDIT_LOG.open() as handle:
        audit = json.loads(handle.read().splitlines()[-1])
    assert audit["fully_filled"] is True
    assert audit["db_updated"] is True
    assert broker.calls[0]["session"] == "ETH"
    assert broker.calls[0]["target_env"] == "sim"
    assert broker.calls[0]["limit_timeout_s"] == 20.0
    assert broker.calls[0]["exit_decision"]["metadata"]["extended_hours_exit"] == {
        "session": "premarket",
        "requested_limit_price": 11.0,
        "broker_remark_prefix": "ext_exit_lock_in",
    }
    # RFC-107 §2.2: 顶层键是 exit_lifecycle.reserve_leg **唯一认**的位置 (它只读
    # decision.metadata 顶层, 读不到嵌套的 extended_hours_exit.*)。少了它 ⇒ remark 回退
    # pbe_* ⇒ reconcile_extended_hours_exits.py 与 orphan_ext_order_watch.py (均按
    # remark.startswith("ext_exit_") 过滤) 对 ETH 单**失明**。MU 9/25 04:04 盘前 BE 案实测:
    # metadata 只有嵌套键 ⇒ 券商单 remark=pbe_, 对账环看不见它。
    # 契约侧三向钉在 tests/execution/test_exit_lifecycle.py::test_reserve_leg_remark_prefix_*
    assert broker.calls[0]["exit_decision"]["metadata"]["broker_remark_prefix"] == "ext_exit_lock_in"


def test_exit_partial_fill_stays_durable_pending_without_direct_db_write(
    exit_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    _install_durable_broker(monkeypatch, dealt_qty=40)

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["ok"] is False
    assert result["fully_filled"] is False
    assert result["remaining_qty"] == 60
    assert result["error"] == "exit_eth_partial_fill_held"
    connection = sqlite3.connect(exit_environment)
    position_size = connection.execute("SELECT size FROM open_positions").fetchone()[0]
    open_trade = connection.execute(
        "SELECT quantity, exit_price, close_futu_order_id "
        "FROM trade_records WHERE futu_order_id='900001'"
    ).fetchone()
    connection.close()
    assert position_size == 100
    assert open_trade == (100, None, None)


def test_exit_blocks_when_broker_exit_order_is_active(
    exit_environment, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    broker = _install_durable_broker(monkeypatch, dealt_qty=0)

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["ok"] is False
    assert result["error"] == "exit_eth_partial_fill_held"
    assert len(broker.calls) == 1


@pytest.mark.parametrize("error", [
    "exit_evidence_pending",
    "exit_legacy_order_evidence_pending",
    "exit_order_evidence_pending",
    "exit_order_pending",
    "exit_position_evidence_pending",
    "exit_fill_price_pending",
    "exit_cancel_pending",
    "exit_reconciliation_pending",
])
def test_exit_pending_evidence_is_awaiting_confirmation_not_failure(
    exit_environment, monkeypatch, error
):
    class PendingBroker:
        def close_position(self, *args, **kwargs):
            return {
                "success": False,
                "order_filled": False,
                "error": error,
                "exit_state": "HELD",
                "exit_intent_id": "pending-intent",
                "futu_order_id": "900004",
                "filled_price": 0.0,
                "dealt_qty": 0.0,
                "price_source": "exit_evidence_pending",
            }

    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(exit_executor, "_get_dual_broker", lambda: PendingBroker())

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["ok"] is False
    assert result["status"] == "awaiting_confirmation"
    assert result["awaiting_confirmation"] is True
    assert result["error"] == error
    with exit_executor._AUDIT_LOG.open() as handle:
        audit = json.loads(handle.read().splitlines()[-1])
    assert audit["awaiting_confirmation"] is True
    assert audit["status"] == "awaiting_confirmation"


def test_exit_executor_exception_has_failure_status(exit_environment, monkeypatch):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(
        exit_executor,
        "_get_dual_broker",
        lambda: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["ok"] is False
    assert result["status"] == "failed_or_held"
    assert result["awaiting_confirmation"] is False
    assert result["error"] == "durable_exit_failed:ledger unavailable"


def test_collect_blocks_extended_open_position_count(monkeypatch):
    @contextlib.contextmanager
    def state_lock():
        yield True

    class TradeContext:
        def close(self):
            pass

    monkeypatch.setattr(entry_executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(entry_executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(entry_executor, "MAX_OPEN", 3)
    monkeypatch.setattr(entry_executor, "MAX_OPEN_NOTIONAL_USD", 15000)
    monkeypatch.setattr(entry_executor, "_state_lock", state_lock)
    monkeypatch.setattr(entry_executor, "_load_state", lambda: {"version": 2})
    monkeypatch.setattr(entry_executor, "_save_state", lambda state: None)
    monkeypatch.setattr(entry_executor, "_open_trade_context", lambda: TradeContext())
    monkeypatch.setattr(
        entry_executor,
        "_reconcile_broker_entry_orders",
        lambda *args, **kwargs: ([], []),
    )
    monkeypatch.setattr(
        entry_executor,
        "_extended_open_exposure",
        lambda: ([("US.A", 10, 100), ("US.B", 10, 100), ("US.C", 10, 100)], 3000),
    )

    result = entry_executor.collect("afterhours")

    assert result == [{
        "skip": "ext_open_position_limit",
        "open_positions": 3,
        "limit": entry_executor.MAX_OPEN,
    }]


def test_open_exposure_gate_blocks_notional_projection(monkeypatch):
    monkeypatch.setattr(entry_executor, "MAX_OPEN_NOTIONAL_USD", 15000)
    monkeypatch.setattr(
        entry_executor,
        "_extended_open_exposure",
        lambda: ([("US.A", 100, 110), ("US.B", 100, 100)], 11000),
    )

    result = entry_executor._open_exposure_gate()

    assert result == {
        "skip": "ext_open_notional_limit",
        "open_notional_usd": 11000,
        "next_position_usd": entry_executor.POSITION_USD,
        "limit_usd": entry_executor.MAX_OPEN_NOTIONAL_USD,
    }


def test_open_exposure_gate_allows_four_positions_under_current_ext_cap(monkeypatch):
    monkeypatch.setattr(entry_executor, "MAX_OPEN", 5)
    monkeypatch.setattr(entry_executor, "MAX_OPEN_NOTIONAL_USD", 25000)
    monkeypatch.setattr(
        entry_executor,
        "_extended_open_exposure",
        lambda: (
            [
                ("US.A", 10, 100),
                ("US.B", 10, 100),
                ("US.C", 10, 100),
                ("US.D", 10, 100),
            ],
            20000,
        ),
    )

    assert entry_executor._open_exposure_gate() is None


def test_extended_open_exposure_counts_only_extended_positions(tmp_path, monkeypatch):
    db_path = tmp_path / "open.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE open_positions (symbol TEXT, size REAL, avg_entry_price REAL, "
        "owner_strategy TEXT, strategy_id TEXT, position_id TEXT)"
    )
    connection.executemany(
        "INSERT INTO open_positions VALUES (?,?,?,?,?,?)",
        [
            ("US.EXT", 10, 100, "extended_hours_entry", "other", "normal"),
            ("US.REGULAR", 20, 200, "playbook_sim", "playbook_sim", "normal"),
        ],
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(entry_executor, "DB", db_path)

    rows, exposure = entry_executor._extended_open_exposure()

    assert rows == [("US.EXT", 10, 100)]
    assert exposure == 1000


def test_exit_next_open_path_uses_rth_session(exit_environment, monkeypatch):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    broker = _install_durable_broker(monkeypatch)

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "next_open", "next_open_exit", dry=False,
    )

    assert result["ok"] is True
    assert result["fully_filled"] is True
    assert broker.calls[0]["session"] == "RTH"


def test_entry_change_pct_env_override(monkeypatch):
    """涨幅闸门可用 env 覆写, 默认 2.0 不变 (2026-09-17 回放: <3% 边际跳空档是唯一负档).

    证据: 现役口径 n=46 → <3% 档 -0.65%/WR42.9%; ≥3% 档 +1.41%。
    放宽口径 n=161 → <3% 档 -0.33%/WR39.4%; ≥3% 档 +1.32%。
    """
    import importlib
    assert entry_executor.ENTRY_CHANGE_PCT == 2.0          # 默认不变
    monkeypatch.setenv("EXT_ENTRY_CHANGE_PCT", "3.0")
    m = importlib.reload(entry_executor)
    assert m.ENTRY_CHANGE_PCT == 3.0                       # env 可覆写
    monkeypatch.delenv("EXT_ENTRY_CHANGE_PCT")
    m = importlib.reload(entry_executor)
    assert m.ENTRY_CHANGE_PCT == 2.0

def test_entry_reconciliation_drops_placeholder_orders():
    """券商账户里的报废占位单 (US.TEST order 8185070) 不得进入对账产物 (2026-09-18 §23.5)。

    真实事故: 该单长期躺在 SIM 账户里, 每轮对账都碰到它 ⇒ 写 attempts / 进 active_orders,
    并沿订单口触发 REAL 拦截推送 (用户反馈"一直持续推送")。
    """
    context = FakeTradeContext(rows=[
        {"order_id": "8185070", "code": "US.TEST", "order_status": "SUBMITTED",
         "qty": 100, "dealt_qty": 0, "dealt_avg_price": 0, "price": 10.0,
         "remark": "ext_entry", "create_time": "2026-09-17 08:00:00"},
        {"order_id": "8185071", "code": "US.TEST", "order_status": "FILLED_ALL",
         "qty": 100, "dealt_qty": 100, "dealt_avg_price": 10.0, "price": 10.0,
         "remark": "ext_entry", "create_time": "2026-09-17 08:05:00"},
    ])
    state = {"version": 2, "opened_today": {}, "attempts": {}}

    active_orders, conflicts = entry_executor._reconcile_broker_entry_orders(
        context, "SIMULATE", 19930823, "premarket",
        state=state, today="2026-09-17",
    )

    assert active_orders == []
    assert conflicts == []
    assert state["attempts"] == {}


# ---------------------------------------------------------------------------
# P1-3 (INCIDENT-002): 推送文案真实性 + 跨进程节流
# 病灶: 10/2 盘前 ACN 每分钟重放陈旧 BOOKED 结果, 推「锁盈待确认」— 实际无任何
# 单待确认 (账本已终结而仓位仍在 = 零保护), 且无节流 (台账 32 条同文推送)。
# ---------------------------------------------------------------------------

class _BookedStaleBroker:
    """复现 10/2 ACN 形态: request 命中旧 BOOKED 行, result() 原样返回陈旧存量。"""

    def close_position(self, *args, **kwargs):
        return {
            "success": False, "order_filled": False,
            "error": "exit_order_pending", "exit_state": "BOOKED",
            "exit_intent_id": "7b149410fb274548b209659b822b9ba7",
            "futu_order_id": "8219159", "filled_price": 0.0, "dealt_qty": 0.0,
            "price_source": "exit_evidence_pending",
        }


def _capture_push(monkeypatch):
    sent = []
    monkeypatch.setattr(exit_executor, "_push",
                        lambda text, level=None, category=None: sent.append(text) or True)
    return sent


def test_booked_stale_replay_push_says_no_exit_path_not_pending_confirmation(
    exit_environment, monkeypatch
):
    """exit_state=BOOKED 且未成交 ⇒ 文案必须直说「无可用退出路径」+ 释放命令。"""
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(exit_executor, "_get_dual_broker", lambda: _BookedStaleBroker())
    sent = _capture_push(monkeypatch)

    result = exit_executor.execute(
        "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
    )

    assert result["ok"] is False and result["exit_state"] == "BOOKED"
    assert len(sent) == 1
    text = sent[0]
    assert "锁盈待确认" not in text, "陈旧 BOOKED 重放不得再冒充「待确认」"
    assert "无可用退出路径" in text
    assert "release_stuck_exit_intent.py" in text, "必须给出可粘贴的释放命令"
    assert "7b149410fb274548b209659b822b9ba7" in text


def test_repeated_identical_failure_push_is_throttled(exit_environment, monkeypatch):
    """同态连续重放: 首行必推, 之后 30 分钟内静默 (每分钟 cron = 每行 ~60s)。"""
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(exit_executor, "_get_dual_broker", lambda: _BookedStaleBroker())
    sent = _capture_push(monkeypatch)

    for _ in range(10):
        exit_executor.execute(
            "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
        )

    assert len(sent) == 1, f"10 次同态重放只应推首行, 实际 {len(sent)}"


def test_awaiting_confirmation_repeats_never_push_again(exit_environment, monkeypatch):
    """等券商回报是瞬态: 首推后同态重复不再推。"""
    class PendingBroker:
        def close_position(self, *args, **kwargs):
            return {
                "success": False, "order_filled": False,
                "error": "exit_order_evidence_pending", "exit_state": "HELD",
                "exit_intent_id": "pending-intent", "futu_order_id": "900004",
                "filled_price": 0.0, "dealt_qty": 0.0,
                "price_source": "exit_evidence_pending",
            }

    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(exit_executor, "_get_dual_broker", lambda: PendingBroker())
    sent = _capture_push(monkeypatch)

    for _ in range(5):
        exit_executor.execute(
            "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
        )

    assert len(sent) == 1 and "等待券商回报" in sent[0]


def test_throttle_reminds_every_30_runs(exit_environment, monkeypatch):
    """长阻塞不能永久静默: 第 30 行同态必须再提醒一次。"""
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(exit_executor, "_get_dual_broker", lambda: _BookedStaleBroker())
    sent = _capture_push(monkeypatch)

    for i in range(30):
        exit_executor.execute(
            "US.TEST", "LONG", 100, 11.0, "premarket", "lock_in", dry=False,
        )

    assert len(sent) == 2, f"第 1 与第 30 行各推一次, 实际 {len(sent)}"


def test_should_push_unit_semantics():
    """节流闸纯函数语义: 首行推 / awaiting 重复不推 / 每 30 行提醒。"""
    assert exit_executor._should_push("failed_or_held", 1) is True
    assert exit_executor._should_push("awaiting_confirmation", 1) is True
    assert exit_executor._should_push("awaiting_confirmation", 2) is False
    assert exit_executor._should_push("failed_or_held", 2) is False
    assert exit_executor._should_push("failed_or_held", 29) is False
    assert exit_executor._should_push("failed_or_held", 30) is True
    assert exit_executor._should_push("failed_or_held", 60) is True


def test_repeat_run_len_reads_bounded_tail(tmp_path, monkeypatch):
    """节流取证只看尾部窗口: 审计文件增长后不得退化为每分钟整读。"""
    record = json.dumps({
        "symbol": "US.TEST", "status": "failed_or_held", "error": "same",
    })
    audit = tmp_path / "audit.jsonl"
    audit.write_text((record + "\n") * 20)
    monkeypatch.setattr(exit_executor, "_AUDIT_LOG", audit)
    monkeypatch.setattr(exit_executor, "_TAIL_BYTES", 6 * (len(record) + 1))

    assert exit_executor._repeat_run_len(
        "US.TEST", "failed_or_held", "same"
    ) == 5
