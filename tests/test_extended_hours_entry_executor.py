import contextlib
import fcntl
import json

import pytest

import scripts.extended_hours_entry_executor as executor


class FakeQuoteContext:
    def __init__(self, *args, **kwargs):
        pass

    def close(self):
        pass

    def get_market_snapshot(self, symbols):
        class Row:
            @staticmethod
            def get(key, default=0):
                return {
                    "bid_price": 9.99,
                    "ask_price": 10.01,
                    "ask_vol": 1_000,
                }.get(key, default)

        class Iloc:
            def __getitem__(self, _index):
                return Row()

        class Snapshot:
            empty = False
            iloc = Iloc()

        return 0, Snapshot()


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(executor, "LOCK", tmp_path / "state.lock")
    monkeypatch.setattr(executor, "PRICE_GATE_REJECTS", tmp_path / "price-rejects.jsonl")
    monkeypatch.setattr(executor, "PRETRADE_GATE_REJECTS", tmp_path / "pretrade-rejects.jsonl")


def test_collect_refuses_real_order_when_sim_disabled_via_switch(
    isolated_state, monkeypatch
):
    """2026-09-17 方案1: ALLOW_SIM_ENTRY 默认 true 恢复下单; 开关=false 时 SIM 仍 fail-closed."""
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setenv("EXT_ENTRY_ALLOW_SIM", "false")  # 关开关
    monkeypatch.setattr(executor, "ALLOW_SIM_ENTRY", False)
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)

    result = executor.collect("premarket")

    assert result == [{"ok": False, "skip": "simulate_stop_unsupported_fail_closed"}]
    assert not executor.STATE.exists()


def test_collect_rejects_outside_session_window(monkeypatch):
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: False)

    result = executor.collect("premarket")

    assert result == [{"ok": False, "skip": "outside_trading_window"}]


def test_dry_scan_does_not_write_state_and_caps_position_notional(
    isolated_state, monkeypatch
):
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_held_symbols", lambda: set())
    monkeypatch.setattr(
        executor,
        "_scan_movers",
        lambda session, qctx: [
            {"symbol": "US.FAKE", "px": 10.0, "chg": 3.0, "vol": 300000.0},
            {"symbol": "US.EXPENSIVE", "px": 6000.0, "chg": 3.0, "vol": 300000.0},
        ],
    )
    monkeypatch.setattr(
        executor,
        "_earnings_surprise",
        lambda symbol: {"surprise_pct": 10.0, "period": "2026-06-30"},
    )
    monkeypatch.setattr("futu.OpenQuoteContext", FakeQuoteContext)

    result = executor.collect("premarket", dry=True)

    assert len(result) == 1
    assert result[0]["symbol"] == "US.FAKE"
    assert result[0]["qty"] == 499
    assert result[0]["price"] == 10.01
    assert result[0]["decision_price"] == 10.0
    assert result[0]["pretrade"]["spread_pct"] == 0.2
    assert result[0]["dry"] is True
    assert not executor.STATE.exists()


def test_pretrade_gate_blocks_missing_quote_spread_depth_and_drift():
    trade_date = "2026-09-18"

    assert executor._pretrade_gate(10.0, 500, None, trade_date)[1] == "quote_missing"

    wide = {"bid": 9.0, "ask": 11.0, "ask_volume": 1_000, "spread_pct": 10.0}
    assert executor._pretrade_gate(10.0, 500, wide, trade_date)[1] == "spread_too_wide"

    quote = {"bid": 9.99, "ask": 10.01, "ask_volume": 100, "spread_pct": 0.1999}
    assert executor._pretrade_gate(10.0, 500, quote, trade_date)[1] == "insufficient_ask_depth"

    quote = {"bid": 10.49, "ask": 10.51, "ask_volume": 1_000, "spread_pct": 0.1903}
    assert executor._pretrade_gate(10.0, 500, quote, trade_date)[1] == "price_drift_too_high"


def test_pretrade_gate_accepts_liquid_quote_and_rounds_price():
    quote = {"bid": 9.99, "ask": 10.01, "ask_volume": 1_000, "spread_pct": 0.1999}
    gate, reason = executor._pretrade_gate(10.0, 500, quote, "2026-09-18")

    assert reason is None
    assert gate["order_price"] == 10.01
    assert gate["round_trip_fee_pct"] < executor.MAX_ROUND_TRIP_FEE_PCT


def test_pretrade_gate_blocks_excessive_round_trip_fee(monkeypatch):
    monkeypatch.setattr(
        "app.execution.fee_model.estimate_round_trip",
        lambda *_args, **_kwargs: {"total": 100.0},
    )
    quote = {"bid": 9.99, "ask": 10.01, "ask_volume": 1_000, "spread_pct": 0.1999}

    gate, reason = executor._pretrade_gate(10.0, 500, quote, "2026-09-18")

    assert gate is None
    assert reason == "round_trip_fee_too_high"


def test_state_is_atomic_and_unreadable_state_fails_closed(
    isolated_state, monkeypatch
):
    state = {"version": 2, "opened_today": {}, "attempts": {}}
    executor._save_state(state)

    assert executor._load_state() == state
    assert not any(path.name.endswith(".tmp") for path in executor.STATE.parent.iterdir())

    executor.STATE.write_text("{broken")
    with pytest.raises(RuntimeError, match="unreadable"):
        executor._load_state()


def test_state_lock_detects_concurrent_executor(isolated_state):
    with executor.LOCK.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with executor._state_lock() as locked:
            assert locked is False

    with executor._state_lock() as locked:
        assert locked is True


def test_execute_sim_env_is_not_blocked_by_real_guard(isolated_state, monkeypatch):
    """SIM/REAL 不混淆 (2026-09-18 用户令): 环境为 SIMULATE 时**不得**走 REAL 拦截分支。

    本测试原先把 SIMULATE 与 REAL 混写在一个用例里 (先 setenv SIMULATE 再覆写 REAL),
    既没验到 SIM 分支、又把"配置态"说成"标的拦截"。现拆清: SIM ⇒ 不返 real_not_authorized。
    """
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(executor, "_push", lambda text: pytest.fail(f"SIM 环境不得推送: {text}"))
    called = {}

    def _fake_open_trade_context():
        called["hit"] = True
        raise RuntimeError("SIM 分支已越过 REAL 守卫 (本测试只验守卫位置, 不真下单)")

    monkeypatch.setattr(executor, "_open_trade_context", _fake_open_trade_context)
    with pytest.raises(RuntimeError, match="越过 REAL 守卫"):
        executor._execute(
            "US.FAKE", 100, 10.0, "premarket", 3.0, 300000.0,
            {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
        )
    assert called.get("hit") is True


def test_execute_rejects_real_environment(isolated_state, tmp_path, monkeypatch):
    """REAL 环境 = 配置态问题: 拦截 + **环境级一次提示** (不再逐标的发"某标的被拦")。"""
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.setattr(executor, "_PUSH_DEDUP", tmp_path / "dedup.json")
    # 2026-09-19: 本用例验的是"环境级一次提示"的**去重与文案**, 必须在非探针分支上跑 ——
    #   `_probe_mode()` 现对 pytest 进程默认 True (pytest→静默), 不关掉就只留一行静默日志、无真推送可断言。
    monkeypatch.setattr(executor, "_probe_mode", lambda: False)
    pushed = []
    monkeypatch.setattr(executor, "_push", lambda text: pushed.append(text))

    result = executor._execute(
        "US.FAKE", 100, 10.0, "premarket", 3.0, 300000.0,
        {"surprise_pct": 10.0, "period": "2026-06-30"}, dry=False,
    )

    assert result["ok"] is False
    assert result["skip"] == "real_not_authorized"
    assert len(pushed) == 1 and "FUTU_TRD_ENV" in pushed[0]      # 环境级, 不点标的
    assert "US.FAKE" not in pushed[0]


def test_entry_switch_default_is_fail_closed():
    """代码默认必须是 fail-closed (2026-09-18 §23 R8).

    原默认 `"true"` = fail-open: `.env.us` 缺失/未加载/键名写错 ⇒ **静默开仓**。
    开关现在真的在下单, 默认方向必须偏安全侧。此测试钉住源码里的默认字面量。
    """
    import inspect

    src = inspect.getsource(executor)
    assert 'os.getenv("EXT_ENTRY_ALLOW_SIM", "false")' in src, (
        "ALLOW_SIM_ENTRY 默认值被改回 fail-open: env 缺失时会静默开仓"
    )

# ---------------------------------------------------------------------------
# 2026-09-18 §23.5 占位标的源头修 (用户: "[FTSeek-US] 🚫 延长时段开仓拦截: US.TEST
# REAL未授权" 持续推送 / 含 US.TEST 的假消息流 → 从源头解决)
# ---------------------------------------------------------------------------


def test_execute_rejects_placeholder_symbol_before_env_checks(isolated_state, monkeypatch):
    """占位标的 (TEST/US.TEST/*.TEST) 必须在**任何环境判定之前**返回 test_symbol。

    优先级钉住: 若后人把 env 判定挪到占位守卫之前, REAL 通道会重新对占位标的推告警。
    """
    monkeypatch.setenv("FUTU_TRD_ENV", "REAL")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.setattr(
        executor, "_push", lambda text: pytest.fail(f"占位标的不得推送: {text}")
    )

    result = executor._execute(
        "US.TEST", 100, 10.0, "premarket", 3.0, 300000.0, None, dry=False,
    )

    assert result == {"ok": False, "skip": "test_symbol"}


def test_scan_candidates_drops_placeholder_symbols(monkeypatch):
    """占位标的在**候选源汇合口**就被丢弃 —— 不再走完四闸 / 占 attempt 键 / 进推送路径。

    实测 US.TEST 曾整条穿过候选→四闸→attempt→REAL 拦截推送 (用户看到"持续推送");
    根因是仓库测试普遍把 US.TEST 当 fixture, 而生产路径此前无源头过滤。
    """
    monkeypatch.setattr(
        executor,
        "_scan_movers",
        lambda session, qctx: [
            {"symbol": "US.TEST", "px": 10.0, "chg": 3.0, "vol": 300000.0},
            {"symbol": "US.FAKE", "px": 10.0, "chg": 3.0, "vol": 300000.0},
        ],
    )

    candidates = executor._scan_candidates("premarket", FakeQuoteContext())

    assert [c["symbol"] for c in candidates] == ["US.FAKE"]


def _gate_count_line(capsys):
    lines = [l for l in capsys.readouterr().out.splitlines() if "闸计数" in l]
    assert lines, "缺少每轮 per-gate 计数行"
    return lines[-1]


def test_gate_counts_line_attributes_every_candidate(isolated_state, monkeypatch, capsys):
    """每轮一行 per-gate 计数 (2026-09-18 用户令「需要」): 候选必须被逐闸归因, 合计不丢项。

    动机 (实测): 动量/量能门与财报门是静默 `continue`, 且 pretrade 台账历史 0 行 ⇒
    日志里"连续多轮 0 笔"与"某一闸卡死"无法区分 —— 盘前链路归因一度只能靠外部只读探针
    (探针读数 50 = 价格闸 34 + 动量量能 15 + 财报门 1)。
    """
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_held_symbols", lambda: {"US.HELD"})
    monkeypatch.setattr(
        executor,
        "_scan_movers",
        lambda session, qctx: [
            {"symbol": "US.HELD", "px": 10.0, "chg": 5.0, "vol": 1_000_000.0},    # 已持仓
            {"symbol": "US.PENNY", "px": 2.0, "chg": 5.0, "vol": 1_000_000.0},    # 价格闸
            {"symbol": "US.SLOW", "px": 10.0, "chg": 1.0, "vol": 1_000_000.0},    # 动量门
            {"symbol": "US.THIN", "px": 10.0, "chg": 5.0, "vol": 10.0},           # 量能门
            {"symbol": "US.NOEARN", "px": 10.0, "chg": 5.0, "vol": 1_000_000.0},  # 财报门
            {"symbol": "US.ILQ", "px": 10.0, "chg": 5.0, "vol": 1_000_000.0},     # 流动性闸
            {"symbol": "US.WIN", "px": 10.0, "chg": 5.0, "vol": 1_000_000.0},     # 过闸
        ],
    )
    monkeypatch.setattr(
        executor,
        "_earnings_surprise",
        lambda symbol: None if symbol == "US.NOEARN" else {"surprise_pct": 10.0, "period": "2026-06-30"},
    )
    monkeypatch.setattr(
        executor,
        "_liquidity_quote",
        lambda qctx, symbol: (
            {"bid": 9.99, "ask": 10.01, "ask_volume": 1_000, "spread_pct": 0.2}
            if symbol == "US.WIN"
            else {"bid": 9.0, "ask": 11.0, "ask_volume": 1, "spread_pct": 20.0}  # 20% 价差 ⇒ 真闸应拒
        ),
    )
    monkeypatch.setattr("futu.OpenQuoteContext", FakeQuoteContext)

    result = executor.collect("premarket", dry=True)
    line = _gate_count_line(capsys)

    assert [r["symbol"] for r in result] == ["US.WIN"]
    assert "候选=7" in line
    for expect in ("held=1", "price_gate=1", "momentum_volume=2", "earnings=1",
                   "liquidity=1", "ordered=1", "attempt_reserved=0"):
        assert expect in line, (expect, line)
    counts = {kv.split("=")[0]: int(kv.split("=")[1])
              for kv in line.split("候选=")[1].split()[1:]}
    assert sum(counts.values()) == 7, f"有未归因候选: {line}"


def test_live_path_prints_gate_counts_and_reserves_attempt(isolated_state, monkeypatch, capsys):
    """下单路径 (cron 每轮走的那条) 同样打计数行, 且 attempt 已占被单独计数。"""
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(executor, "ALLOW_SIM_ENTRY", True)
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_held_symbols", lambda: set())
    today = executor._today_et()
    monkeypatch.setattr(
        executor,
        "_scan_movers",
        lambda session, qctx: [
            {"symbol": "US.PENNY", "px": 1.0, "chg": 9.0, "vol": 9_000_000.0},
            {"symbol": "US.WIN", "px": 10.0, "chg": 9.0, "vol": 9_000_000.0},
            {"symbol": "US.WIN2", "px": 10.0, "chg": 9.0, "vol": 9_000_000.0},
        ],
    )
    monkeypatch.setattr(
        executor, "_earnings_surprise",
        lambda symbol: {"surprise_pct": 30.0, "period": "2026-06-30"},
    )
    monkeypatch.setattr(
        executor, "_liquidity_quote",
        lambda qctx, symbol: {"bid": 9.99, "ask": 10.01, "ask_volume": 1_000, "spread_pct": 0.2},
    )
    monkeypatch.setattr("futu.OpenQuoteContext", FakeQuoteContext)
    monkeypatch.setattr(executor, "_execute",
                        lambda *a, **k: {"ok": False, "skip": "stub_no_order"})
    monkeypatch.setattr(executor, "_audit", lambda result: None)
    monkeypatch.setattr(executor, "_push", lambda text: None)

    @contextlib.contextmanager
    def _fake_lock():
        yield True

    monkeypatch.setattr(executor, "_state_lock", _fake_lock)
    # US.WIN 已占 attempt ⇒ 本轮应计入 attempt_reserved 而非再下单
    executor.STATE.write_text(json.dumps(
        {"version": 2, "opened_today": {}, "attempts": {f"{today}:US.WIN": {"status": "opened"}}}))
    executor.LOCK.write_text("")

    results = executor.collect("premarket")
    line = _gate_count_line(capsys)

    assert "候选=3" in line and "attempt_reserved=1" in line and "ordered=1" in line
    assert len(results) == 1
    state = json.loads(executor.STATE.read_text())
    assert f"{today}:US.WIN2" in state["attempts"]
