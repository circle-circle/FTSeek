import contextlib
import fcntl
import json
from datetime import datetime, timezone

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
                    "total_market_val": 5_000_000_000,
                }.get(key, default)

        class Iloc:
            def __getitem__(self, _index):
                return Row()

        class Snapshot:
            empty = False
            iloc = Iloc()

        return 0, Snapshot()


class FakeKlineFrame:
    def __init__(self, rows):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def iterrows(self):
        return iter(enumerate(self.rows))


class FakeKlineContext:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def request_history_kline(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return 0, FakeKlineFrame(self.rows), None


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(executor, "LOCK", tmp_path / "state.lock")
    monkeypatch.setattr(executor, "PRICE_GATE_REJECTS", tmp_path / "price-rejects.jsonl")
    monkeypatch.setattr(executor, "PRETRADE_GATE_REJECTS", tmp_path / "pretrade-rejects.jsonl")
    monkeypatch.setattr(executor, "EARNINGS_GATE_REJECTS", tmp_path / "earnings-rejects.jsonl")
    monkeypatch.setattr(
        executor, "EARNINGS_NEAR_MISSES", tmp_path / "earnings-near-misses.jsonl"
    )


@pytest.fixture(autouse=True)
def _chg_band_pinned_off(monkeypatch):
    """Hermetic 钉 (2026-09-23, RFC-097): `.env.us` 已带 `EXT_ENTRY_CHG_BAND=0.5,2.0`
    (入场温和票带), executor 模块 import 期读 env ⇒ **本文件**(闸/计数/近失行为面)一律显式钉空,
    回到"旧阈值 chg>=2.0"语义, 防生产配置漂移进断言 (实测: 不钉则 8 个用例因带把 chg≥2 的
    fixture 票全挡掉而红)。涨幅带自身行为见 tests/test_ext_entry_observation_decoupling.py
    与 tests/test_ext_entry_earnings_criterion.py (那里显式钉带)。"""
    monkeypatch.setattr(executor, "ENTRY_CHG_BAND", "")
    # 同因 (2026-09-23 空侧开启): fixture 票 chg=9% 在 SHORT_ALLOW=true 下会走空侧路径
    # (事件门/SELL 下单), 与本文件的 LONG 语义断言冲突 ⇒ 钉回多侧。空侧行为见
    # tests/test_ext_entry_short_arm.py (那里显式开)。
    monkeypatch.setattr(executor, "SHORT_ALLOW", False)


@pytest.fixture(autouse=True)
def _research_arm_pinned_off(monkeypatch):
    """Hermetic 钉 (2026-09-22, DEB-20260920 §12): .env.us 已带 EXT_ENTRY_RESEARCH_MOMENTUM=true
    (研究臂生产启用), executor 模块 import 期读 env ⇒ 本文件 (严格臂行为面) 一律显式钉关,
    防生产 env 漂移进断言 (实测: 钉前 test_gate_counts_line_attributes_every_candidate 因
    research_pool 子集标签破坏归因求和而红)。研究臂行为用例见 tests/test_ext_entry_research_arm.py
    (自行显式钉开, 双向 hermetic)。"""
    monkeypatch.setattr(executor, "RESEARCH_ARM_ON", False)


def _seed_earnings_calendar(monkeypatch, rows, error=None, today=None):
    """把日历缓存换成测试内容 (新格式 {"rows": [...], "error": ...}).

    所有会走 premarket collect 的用例都必须 seed — 否则 P1-4 的
    `_afterhours_earnings_expected()` 会尝试真实 EarningsCalendar (触网/开 OpenD)。
    """
    day = today or executor._today_et()
    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {day: {"rows": rows, "error": error}})


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
            {"symbol": "US.FAKE", "px": 10.0, "chg": 3.0, "vol": 2_000_000.0},
            {"symbol": "US.EXPENSIVE", "px": 6000.0, "chg": 3.0, "vol": 300000.0},
        ],
    )
    monkeypatch.setattr(
        executor,
        "_earnings_surprise",
        lambda symbol, session: ({"surprise_pct": 10.0, "period": "2026-06-30"}, None),
    )
    monkeypatch.setattr(executor, "_entry_window_reason", lambda session, grade: None)
    monkeypatch.setattr(
        executor,
        "_entry_structure_gate",
        lambda qctx, symbol, session, price: (
            {"vwap": 10.0, "last_close": 10.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0}, None,
            {"vwap": 10.0, "last_close": 10.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0},
        ),
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


def test_entry_grade_requires_market_cap_and_turnover_then_scales_position():
    assert executor._entry_grade(10.0, 2_000_000, 5_000_000_000) == ("A", None)
    assert executor._entry_grade(10.0, 1_200_000, 5_000_000_000) == ("B", None)
    assert executor._entry_grade(10.0, 2_000_000, 0) == (None, "market_cap_missing")
    assert executor._entry_grade(10.0, 2_000_000, 500_000_000) == (None, "market_cap_too_low")
    assert executor._entry_grade(10.0, 900_000, 5_000_000_000) == (None, "turnover_too_low")


def test_entry_window_reason_uses_grade_maturity(monkeypatch):
    monkeypatch.setattr(
        executor, "_now_et", lambda: datetime(2026, 9, 18, 7, 45, tzinfo=timezone.utc)
    )
    assert executor._entry_window_reason("premarket", "A") is None
    assert executor._entry_window_reason("premarket", "B") == "entry_window_not_mature"

    monkeypatch.setattr(
        executor, "_now_et", lambda: datetime(2026, 9, 18, 17, 45, tzinfo=timezone.utc)
    )
    assert executor._entry_window_reason("afterhours", "A") is None
    assert executor._entry_window_reason("afterhours", "B") == "entry_window_not_mature"


def test_pretrade_gate_applies_grade_a_spread_and_depth():
    quote = {"bid": 9.98, "ask": 10.02, "ask_volume": 999, "spread_pct": 0.3999}

    gate, reason = executor._pretrade_gate(
        10.0, 500, quote, "2026-09-18",
        max_spread_pct=0.5, min_ask_depth_ratio=2.0,
    )

    assert reason == "insufficient_ask_depth"
    assert gate is None

    quote.update(ask_volume=2_000)
    tight = dict(quote, spread_pct=0.51)
    assert executor._pretrade_gate(
        10.0, 500, tight, "2026-09-18",
        max_spread_pct=0.5, min_ask_depth_ratio=2.0,
    )[1] == "spread_too_wide"


def test_extended_vwap_metrics_uses_same_session_bars(monkeypatch):
    rows = [
        {"time_key": "2026-09-18 07:30:00", "close": 10.25, "volume": 10_000},
        {"time_key": "2026-09-18 07:35:00", "close": 10.25, "volume": 10_000},
        {"time_key": "2026-09-18 07:40:00", "close": 10.25, "volume": 10_000},
        {"time_key": "2026-09-18 07:45:00", "close": 10.30, "volume": 10_000},
    ]
    monkeypatch.setattr(
        executor, "_today_et", lambda: "2026-09-18"
    )
    context = FakeKlineContext(rows)

    metrics = executor._extended_vwap_metrics(context, "US.FAKE", "premarket")

    assert metrics["vwap"] == 10.2625
    assert metrics["volume_ratio"] == 1.0
    assert metrics["bars"] == 4
    assert metrics["anchor"] == "maturity"
    assert context.calls[0][1]["extended_time"] is True


def test_extended_vwap_maturity_anchor_excludes_predawn_bars(monkeypatch):
    """P1-3: 凌晨死区 (4:00-7:00) bar 不得拖锚; 7:00+ 事件量 bar 必须参与 (§10.6 边界)."""
    rows = [
        {"time_key": "2026-09-18 04:05:00", "close": 9.00, "volume": 100},      # 凌晨死区
        {"time_key": "2026-09-18 05:00:00", "close": 9.00, "volume": 100},
        {"time_key": "2026-09-18 06:55:00", "close": 9.00, "volume": 100},      # 边界外
        {"time_key": "2026-09-18 07:05:00", "close": 10.25, "volume": 10_000},  # 边界内
        {"time_key": "2026-09-18 07:30:00", "close": 10.25, "volume": 10_000},
        {"time_key": "2026-09-18 07:35:00", "close": 10.25, "volume": 10_000},
        {"time_key": "2026-09-18 07:40:00", "close": 10.25, "volume": 10_000},
        {"time_key": "2026-09-18 07:45:00", "close": 10.30, "volume": 10_000},
    ]
    monkeypatch.setattr(executor, "_today_et", lambda: "2026-09-18")
    context = FakeKlineContext(rows)

    metrics = executor._extended_vwap_metrics(context, "US.FAKE", "premarket")
    assert metrics["bars"] == 5             # 07:05 起, 3 根 9.00 的死区 bar 未参与
    assert metrics["vwap"] == 10.26
    assert metrics["volume_ratio"] == 1.0

    monkeypatch.setattr(executor, "VWAP_ANCHOR_MATURITY", False)  # 回滚开关 = 旧口径
    legacy = executor._extended_vwap_metrics(context, "US.FAKE", "premarket")
    assert legacy["bars"] == 8
    assert legacy["anchor"] == "full_session"
    assert legacy["vwap"] < metrics["vwap"]   # 死区 bar 把旧口径的锚拖低


def test_extended_vwap_afterhours_anchor_starts_at_session_open(monkeypatch):
    """§10.6: 盘后锚 = 16:00 (全时段起点, 无凌晨病灶; 16:00-16:20 是发布后量最重的
    价格发现段, 排除它 VWAP 失代表性且会把 A/B 窗开头打成 bars<4 预热拒绝)."""
    rows = [
        {"time_key": "2026-09-18 16:05:00", "close": 42.0, "volume": 50_000},
        {"time_key": "2026-09-18 16:10:00", "close": 42.1, "volume": 40_000},
        {"time_key": "2026-09-18 16:15:00", "close": 42.2, "volume": 30_000},
        {"time_key": "2026-09-18 16:20:00", "close": 42.3, "volume": 20_000},
    ]
    monkeypatch.setattr(executor, "_today_et", lambda: "2026-09-18")
    context = FakeKlineContext(rows)

    metrics = executor._extended_vwap_metrics(context, "US.FAKE", "afterhours")

    assert metrics is not None              # 16:20 (A/B 入场窗起点) 即有 4 根 bar, 无预热拒绝
    assert metrics["bars"] == 4
    assert metrics["anchor"] == "maturity"


def test_entry_structure_gate_requires_vwap_reclaim_and_volume(monkeypatch):
    metrics = {"vwap": 10.0, "vwap_dev_pct": 0.0, "volume_ratio": 1.0}
    monkeypatch.setattr(
        executor, "_extended_vwap_metrics", lambda qctx, symbol, session: metrics
    )

    passed = executor._entry_structure_gate(object(), "US.FAKE", "premarket", 10.01)
    assert passed[1] is None and passed[0] == metrics
    rejected = executor._entry_structure_gate(object(), "US.FAKE", "premarket", 9.99)
    assert rejected[1] == "below_vwap"
    assert rejected[2] == metrics   # P1-3: 被拒时也携带实测 metrics 供台账记录

    monkeypatch.setattr(
        executor, "_extended_vwap_metrics", lambda qctx, symbol, session: None
    )
    missing = executor._entry_structure_gate(object(), "US.FAKE", "premarket", 10.01)
    assert missing[1] == "structure_data_missing" and missing[2] is None

    extended = dict(metrics, vwap_dev_pct=1.0)
    monkeypatch.setattr(
        executor, "_extended_vwap_metrics", lambda qctx, symbol, session: extended
    )
    assert executor._entry_structure_gate(object(), "US.FAKE", "premarket", 10.15)[1] == "vwap_extended"

    quiet = dict(metrics, volume_ratio=0.1)
    monkeypatch.setattr(
        executor, "_extended_vwap_metrics", lambda qctx, symbol, session: quiet
    )
    assert executor._entry_structure_gate(object(), "US.FAKE", "premarket", 10.01)[1] == "volume_confirmation_missing"


def test_earnings_calendar_event_requires_today_and_matching_phase(monkeypatch):
    # 钉死配对开关 (RFC-102): 本用例验的是**默认单日配对**语义; 不钉则生产 .env.us 一开
    #   `EXT_ENTRY_PREMARKET_AMC_PAIRING=true` 就把它改名成 phase_mismatch (N4 类环境耦合)。
    monkeypatch.setattr(executor, "PREMARKET_AMC_PAIRING", False)
    monkeypatch.setattr(executor, "_today_et", lambda: "2026-09-18")
    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-18": {"rows": [
            {"symbol": "FAKE", "reportDate": "2026-09-18", "timeOfTheDay": "pre-market"},
            {"symbol": "OLD", "reportDate": "2026-09-17", "timeOfTheDay": "pre-market"},
        ], "error": None},
    })

    assert executor._earnings_calendar_event("US.FAKE", "premarket")[0] is not None
    assert executor._earnings_calendar_event("US.FAKE", "afterhours") == (None, "calendar_phase_mismatch")
    assert executor._earnings_calendar_event("US.OLD", "premarket") == (None, "calendar_missing")


def test_earnings_calendar_event_sub_reasons_distinguish_source_and_phase(monkeypatch):
    """P0-2: "源挂了" / "无事件" / "相位不符" / "相位字段不可识别" 必须可区分.

    §9 观察期第 10 日判断"市场无事件 vs 硬闸过窄/字段错配"依赖这些子原因;
    旧实现全部塌缩成 None, 归因不可能。
    """
    monkeypatch.setattr(executor, "_today_et", lambda: "2026-09-18")

    # 源故障 (不得静默当"今日无财报")
    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-18": {"rows": [], "error": "Timeout: av"},
    })
    assert executor._earnings_calendar_event("US.FAKE", "premarket") == (None, "calendar_source_error")

    # 相位字段为空/不可识别 → fail-closed 且单列子原因 (数据源契约漂移的信号)
    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-18": {"rows": [
            {"symbol": "FAKE", "reportDate": "2026-09-18", "timeOfTheDay": ""},
        ], "error": None},
    })
    assert executor._earnings_calendar_event("US.FAKE", "premarket") == (None, "calendar_phase_unknown")

    # 同 symbol 多行: 首行相位不符不得终判, 须扫完所有匹配行 (旧版首行即拒)
    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-18": {"rows": [
            {"symbol": "FAKE", "reportDate": "2026-09-18", "timeOfTheDay": "post-market"},
            {"symbol": "FAKE", "reportDate": "2026-09-18", "timeOfTheDay": "pre-market"},
        ], "error": None},
    })
    row, reason = executor._earnings_calendar_event("US.FAKE", "premarket")
    assert reason is None and row["timeOfTheDay"] == "pre-market"


def test_earnings_calendar_rows_propagate_normalized_empty_source_error(monkeypatch):
    """入口不得把底层双源空误归因为 calendar_missing."""
    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {})

    class Calendar:
        def get_earnings_calendar_with_status(self):
            return [], "futu_and_av_empty"

    monkeypatch.setattr("app.data.earnings_calendar.EarningsCalendar", lambda: Calendar())

    assert executor._earnings_calendar_rows() == ([], "futu_and_av_empty")


def test_earnings_surprise_rejects_stale_prior_quarter_period(monkeypatch):
    """P0-1 新鲜度守卫: 今日 actual 未入库时 e[0] 是上一季记录 — 上季 beat 不得放行今日入场.

    病灶同型: 9/17 首跑 4 笔"财报高开→回落" (miss + 盘前先涨 2% 透过旧闸)。

    ⚠️ 2026-09-23 (RFC-097): 本用例测的是**旧口径 (period 龄窗)**, 现已是回滚路径
    (`EXT_EARNINGS_CRITERION=legacy_period`); 生产默认走 calendar_actual ⇒ 此处必须
    显式钉住判据, 否则它测的东西随默认值漂移 (2026-09-23 实测: 不钉则红)。
    """
    monkeypatch.setattr(executor, "EARNINGS_CRITERION", "legacy_period")
    monkeypatch.setattr(executor, "_today_et", lambda: "2026-09-20")
    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-20": {"rows": [
            {"symbol": "FAKE", "reportDate": "2026-09-20", "timeOfTheDay": "pre-market"},
        ], "error": None},
    })

    class FakeClient:
        def __init__(self, period):
            self._period = period

        def company_earnings(self, tick, limit=1):
            return [{"surprisePercent": 12.0, "period": self._period}]

    def _patch_client(period):
        import app.data.premarket_enrichment as enrichment
        monkeypatch.setattr(
            enrichment, "_get_finnhub_client", lambda: FakeClient(period)
        )

    _patch_client("2026-03-31")   # 距今 173 天 = 上一季记录 → stale
    assert executor._earnings_surprise("US.FAKE", "premarket") == (None, "surprise_stale")

    _patch_client("2026-06-30")   # 距今 82 天 = 本季 (Q2 晚披露) → 放行
    earn, reason = executor._earnings_surprise("US.FAKE", "premarket")
    assert reason is None
    assert earn["surprise_pct"] == 12.0
    assert earn["period_age_days"] == 82
    assert earn["report_date"] == "2026-09-20"
    assert earn["report_phase"] == "pre-market"

    _patch_client(None)           # period 缺失 → fail-closed
    assert executor._earnings_surprise("US.FAKE", "premarket") == (None, "surprise_stale")


def test_afterhours_earnings_expected_reads_today_calendar(monkeypatch):
    """P1-4 依据函数: 源故障/空日历保守视为"有盘后财报" (继续保留额度)."""
    monkeypatch.setattr(executor, "_today_et", lambda: "2026-09-18")

    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-18": {"rows": [
            {"symbol": "A", "reportDate": "2026-09-18", "timeOfTheDay": "post-market"},
        ], "error": None},
    })
    assert executor._afterhours_earnings_expected() is True

    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-18": {"rows": [
            {"symbol": "A", "reportDate": "2026-09-18", "timeOfTheDay": "pre-market"},
            {"symbol": "B", "reportDate": "2026-09-19", "timeOfTheDay": "post-market"},  # 非今日
        ], "error": None},
    })
    assert executor._afterhours_earnings_expected() is False

    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-18": {"rows": [], "error": "Timeout: av"},
    })
    assert executor._afterhours_earnings_expected() is True

    monkeypatch.setattr(executor, "_EARNINGS_CALENDAR_CACHE", {
        "2026-09-18": {"rows": [], "error": None},
    })
    assert executor._afterhours_earnings_expected() is True


def test_record_earnings_gate_rejects_writes_one_aggregated_line(tmp_path, monkeypatch):
    path = tmp_path / "earnings-rejects.jsonl"
    monkeypatch.setattr(executor, "EARNINGS_GATE_REJECTS", path)

    executor._record_earnings_gate_rejects(
        "premarket", "2026-09-21",
        {"calendar_missing": 40, "surprise_stale": 1},
        [{"symbol": "US.FAKE", "sub_reason": "surprise_stale"}],
    )
    executor._record_earnings_gate_rejects("premarket", "2026-09-21", {}, [])  # 空轮不写

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["counts"] == {"calendar_missing": 40, "surprise_stale": 1}
    assert rec["details"] == [{"symbol": "US.FAKE", "sub_reason": "surprise_stale"}]


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


def _quota_test_environment(monkeypatch, opened_today):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(executor, "ALLOW_SIM_ENTRY", True)
    monkeypatch.setattr(executor, "MAX_PER_DAY", 3)
    monkeypatch.setattr(executor, "PREMARKET_MAX_PER_DAY", 2)
    monkeypatch.setattr(executor, "MAX_OPEN", 3)
    monkeypatch.setattr(executor, "MAX_OPEN_NOTIONAL_USD", 15000)
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_state_lock", contextlib.contextmanager(lambda: (yield True)))
    monkeypatch.setattr(
        executor, "_load_state",
        lambda: {"version": 2, "opened_today": opened_today, "attempts": {}},
    )
    monkeypatch.setattr(executor, "_save_state", lambda state: None)
    # P1-4 默认场景: 当日**有**盘后财报 ⇒ 保留额度维持旧行为 (盘前限 2)。
    # 不 seed 会让 `_afterhours_earnings_expected()` 触真实日历源 (触网/开 OpenD)。
    _seed_earnings_calendar(monkeypatch, [
        {"symbol": "AFTER", "reportDate": executor._today_et(), "timeOfTheDay": "post-market"},
    ])

    class TradeContext:
        def close(self):
            pass

    monkeypatch.setattr(executor, "_open_trade_context", lambda: TradeContext())
    monkeypatch.setattr(
        executor, "_reconcile_broker_entry_orders",
        lambda *args, **kwargs: ([], []),
    )


def test_earnings_near_miss_shadow_dedupes_by_day_session_symbol(
        tmp_path, monkeypatch
):
    path = tmp_path / "earnings-near-misses.jsonl"
    monkeypatch.setattr(executor, "EARNINGS_NEAR_MISSES", path)
    monkeypatch.setattr(executor, "EARNINGS_NEAR_MISS_ON", True)
    monkeypatch.setattr(executor, "MIN_EXT_TURNOVER_USD", 10_000_000.0)
    legacy_row = {
        "trade_date": "2026-09-21",
        "session": "afterhours",
        "symbol": "US.AAA",
        "base_gate": "price_momentum_volume_passed",
    }
    path.write_text(json.dumps(legacy_row) + "\n")
    candidates = [
        {"symbol": "US.LOW_TURNOVER", "price": 10.2, "change_pct": 6.1, "volume": 300000},
        {"symbol": "US.AAA", "price": 10.2, "change_pct": 6.1, "volume": 1_000_000},
        {"symbol": "US.BBB", "price": 12.4, "change_pct": 4.3, "volume": 900000},
    ]

    executor._record_earnings_near_misses("afterhours", "2026-09-21", candidates)
    executor._record_earnings_near_misses(
        "afterhours", "2026-09-21", [
            candidates[0],
            {"symbol": "US.CCC", "price": 9.8, "change_pct": 3.2, "volume": 1_100_000},
        ]
    )

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["symbol"] for row in rows] == [
        "US.AAA", "US.AAA", "US.BBB", "US.CCC"
    ]
    assert rows[0]["base_gate"] == "price_momentum_volume_passed"
    assert all(row["reject_reason"] == "calendar_missing" for row in rows[1:])
    assert all(
        row["base_gate"] == "price_momentum_volume_turnover_passed"
        for row in rows[1:]
    )
    assert all(row["turnover_usd"] >= 10_000_000 for row in rows[1:])


def test_collect_dry_scan_records_turnover_qualified_earnings_near_miss(
        isolated_state, monkeypatch
):
    near_miss_path = executor.EARNINGS_NEAR_MISSES
    monkeypatch.setattr(executor, "EARNINGS_NEAR_MISS_ON", True)
    monkeypatch.setattr(executor, "MIN_EXT_TURNOVER_USD", 10_000_000.0)
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_held_symbols", lambda: set())
    monkeypatch.setattr(
        executor,
        "_scan_candidates",
        lambda session, qctx: [
            {"symbol": "US.NEAR", "px": 10.0, "chg": 5.0, "vol": 1_200_000.0},
            {"symbol": "US.LOW_TURNOVER", "px": 10.0, "chg": 5.0, "vol": 300_000.0},
        ],
    )
    monkeypatch.setattr(
        executor,
        "_earnings_surprise",
        lambda symbol, session: (None, "calendar_missing"),
    )
    monkeypatch.setattr("futu.OpenQuoteContext", FakeQuoteContext)

    result = executor.collect("premarket", dry=True)

    assert result == [{"skip": "no_signal_passed_all_gates"}]
    rows = [json.loads(line) for line in near_miss_path.read_text().splitlines()]
    assert [row["symbol"] for row in rows] == ["US.NEAR"]
    assert rows[0]["base_gate"] == "price_momentum_volume_turnover_passed"
    assert rows[0]["turnover_usd"] == 12_000_000.0


def test_collect_blocks_premarket_after_two_positions(isolated_state, monkeypatch):
    today = executor._today_et()
    _quota_test_environment(monkeypatch, {f"{today}:premarket": 2})
    monkeypatch.setattr(
        executor, "_open_exposure_gate",
        lambda: pytest.fail("盘前段内配额已达 2, 不应继续进入暴露度闸"),
    )

    result = executor.collect("premarket")

    assert result == [{
        "skip": "premarket_session_limit",
        "session_today": 2,
        "limit": executor.PREMARKET_MAX_PER_DAY,
        "reserved_for_afterhours": True,
    }]


def test_collect_releases_premarket_third_slot_without_afterhours_earnings(
    isolated_state, monkeypatch
):
    """P1-4: 当日日历无盘后相位财报 ⇒ 保留是纯机会成本, 盘前第 3 笔放行."""
    today = executor._today_et()
    _quota_test_environment(monkeypatch, {f"{today}:premarket": 2})
    _seed_earnings_calendar(monkeypatch, [
        {"symbol": "PRE", "reportDate": today, "timeOfTheDay": "pre-market"},
    ])
    exposure_gate = {"skip": "next_gate_reached"}
    monkeypatch.setattr(executor, "_open_exposure_gate", lambda: exposure_gate)

    result = executor.collect("premarket")

    assert result == [exposure_gate]   # 未被 premarket_session_limit 拦下


def test_collect_keeps_premarket_reserve_when_calendar_source_fails(
    isolated_state, monkeypatch
):
    """P1-4 保守方向: 日历源故障时**保留**额度 (宁机会成本, 不改暴露节奏)."""
    today = executor._today_et()
    _quota_test_environment(monkeypatch, {f"{today}:premarket": 2})
    _seed_earnings_calendar(monkeypatch, [], error="Timeout: av")
    monkeypatch.setattr(
        executor, "_open_exposure_gate",
        lambda: pytest.fail("源故障时盘前第 3 笔不得放行"),
    )

    result = executor.collect("premarket")

    assert result == [{
        "skip": "premarket_session_limit",
        "session_today": 2,
        "limit": executor.PREMARKET_MAX_PER_DAY,
        "reserved_for_afterhours": True,
    }]


def test_collect_keeps_afterhours_slot_after_two_premarket_positions(
    isolated_state, monkeypatch
):
    today = executor._today_et()
    _quota_test_environment(monkeypatch, {f"{today}:premarket": 2})
    exposure_gate = {"skip": "next_gate_reached"}
    monkeypatch.setattr(executor, "_open_exposure_gate", lambda: exposure_gate)

    result = executor.collect("afterhours")

    assert result == [exposure_gate]


def test_collect_allows_third_afterhours_position_when_premarket_unused(
    isolated_state, monkeypatch
):
    today = executor._today_et()
    _quota_test_environment(monkeypatch, {f"{today}:afterhours": 2})
    exposure_gate = {"skip": "next_gate_reached"}
    monkeypatch.setattr(executor, "_open_exposure_gate", lambda: exposure_gate)

    result = executor.collect("afterhours")

    assert result == [exposure_gate]


def test_collect_daily_total_limit_still_blocks_three_opened_positions(
    isolated_state, monkeypatch
):
    today = executor._today_et()
    _quota_test_environment(
        monkeypatch,
        {f"{today}:premarket": 2, f"{today}:afterhours": 1},
    )
    monkeypatch.setattr(
        executor, "_open_exposure_gate",
        lambda: pytest.fail("全天合计配额已达 3, 不应继续进入暴露度闸"),
    )

    result = executor.collect("afterhours")

    assert result == [{"skip": "daily_limit", "total_today": 3}]


def test_collect_stops_premarket_loop_when_second_position_fills(
    isolated_state, monkeypatch
):
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(executor, "ALLOW_SIM_ENTRY", True)
    monkeypatch.setattr(executor, "MAX_PER_DAY", 3)
    monkeypatch.setattr(executor, "PREMARKET_MAX_PER_DAY", 2)
    monkeypatch.setattr(executor, "MAX_OPEN", 3)
    monkeypatch.setattr(executor, "MAX_OPEN_NOTIONAL_USD", 15000)
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_state_lock", contextlib.contextmanager(lambda: (yield True)))
    monkeypatch.setattr(executor, "_open_trade_context", lambda: type("T", (), {"close": lambda self: None})())
    monkeypatch.setattr(
        executor, "_reconcile_broker_entry_orders",
        lambda *args, **kwargs: ([], []),
    )
    monkeypatch.setattr(executor, "_open_exposure_gate", lambda: None)
    monkeypatch.setattr(executor, "_held_symbols", lambda: set())
    today = executor._today_et()
    day_key = f"{today}:premarket"
    state = {"version": 2, "opened_today": {day_key: 1}, "attempts": {}}
    monkeypatch.setattr(executor, "_load_state", lambda: state)
    _seed_earnings_calendar(monkeypatch, [
        {"symbol": "AFTER", "reportDate": today, "timeOfTheDay": "post-market"},
    ])
    monkeypatch.setattr(executor, "_scan_movers", lambda session, qctx: [
        {"symbol": "US.FIRST", "px": 10.0, "chg": 9.0, "vol": 9_000_000.0},
        {"symbol": "US.SECOND", "px": 10.0, "chg": 9.0, "vol": 9_000_000.0},
    ])
    monkeypatch.setattr(
        executor, "_earnings_surprise",
        lambda symbol, session: ({"surprise_pct": 30.0, "period": "2026-06-30"}, None),
    )
    monkeypatch.setattr(
        executor, "_liquidity_quote",
        lambda qctx, symbol: {"bid": 9.99, "ask": 10.01, "ask_volume": 1_000,
                              "spread_pct": 0.2, "market_cap": 5_000_000_000},
    )
    monkeypatch.setattr(executor, "_entry_window_reason", lambda session, grade: None)
    monkeypatch.setattr(
        executor, "_entry_structure_gate",
        lambda qctx, symbol, session, price: (
            {"vwap": 10.0, "last_close": 10.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0}, None,
            {"vwap": 10.0, "last_close": 10.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0},
        ),
    )
    monkeypatch.setattr("futu.OpenQuoteContext", FakeQuoteContext)
    executed = []
    monkeypatch.setattr(
        executor, "_execute",
        lambda *args, **kwargs: executed.append(args) or {"ok": True, "buy_order_id": "SIM-1"},
    )
    monkeypatch.setattr(executor, "_audit", lambda result: None)
    monkeypatch.setattr(executor, "_push", lambda text: None)

    results = executor.collect("premarket")

    assert len(executed) == 1
    assert len(results) == 1
    assert state["opened_today"][day_key] == 2
    assert f"{today}:US.FIRST" in state["attempts"]
    assert f"{today}:US.SECOND" not in state["attempts"]


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

    class LiveMode:
        is_live = True

        def account_id(self):
            return 1

    monkeypatch.setattr(executor, "_mode", lambda: LiveMode())
    monkeypatch.setattr(executor, "_ext_real_authorized", lambda: False)
    monkeypatch.setattr(executor, "AUDIT", tmp_path / "entry-audit.jsonl")
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
    assert len(pushed) == 1 and "EXT_REAL_AUTHORIZED" in pushed[0]  # 环境级, 不点标的
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
            {"symbol": "US.ILQ", "px": 10.0, "chg": 5.0, "vol": 2_000_000.0},     # 流动性闸
            {"symbol": "US.WIN", "px": 10.0, "chg": 5.0, "vol": 2_000_000.0},     # 过闸
        ],
    )
    monkeypatch.setattr(
        executor,
        "_earnings_surprise",
        lambda symbol, session: (
            (None, "calendar_missing") if symbol == "US.NOEARN"
            else ({"surprise_pct": 10.0, "period": "2026-06-30"}, None)
        ),
    )
    monkeypatch.setattr(
        executor,
        "_liquidity_quote",
        lambda qctx, symbol: (
            {"bid": 9.99, "ask": 10.01, "ask_volume": 1_000, "spread_pct": 0.2,
             "market_cap": 5_000_000_000}
            if symbol == "US.WIN"
            else {"bid": 9.0, "ask": 11.0, "ask_volume": 1, "spread_pct": 20.0,
                  "market_cap": 5_000_000_000}  # 20% 价差 ⇒ 真闸应拒
        ),
    )
    monkeypatch.setattr(executor, "_entry_window_reason", lambda session, grade: None)
    monkeypatch.setattr(
        executor,
        "_entry_structure_gate",
        lambda qctx, symbol, session, price: (
            {"vwap": 10.0, "last_close": 10.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0}, None,
            {"vwap": 10.0, "last_close": 10.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0},
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
    # earnings_phase/stale/src_err 是 earnings 的子集聚合 (P0-2 观测列), 不参与归因求和
    derived = ("earnings_phase", "earnings_stale", "earnings_src_err")
    attributed = sum(v for k, v in counts.items() if k not in derived)
    assert attributed == 7, f"有未归因候选: {line}"


def test_live_path_prints_gate_counts_and_reserves_attempt(isolated_state, monkeypatch, capsys):
    """下单路径 (cron 每轮走的那条) 同样打计数行, 且 attempt 已占被单独计数。"""
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    monkeypatch.setattr(executor, "ALLOW_SIM_ENTRY", True)
    monkeypatch.setattr(executor, "_is_trading_day", lambda: True)
    monkeypatch.setattr(executor, "_in_session_window", lambda session: True)
    monkeypatch.setattr(executor, "_held_symbols", lambda: set())
    today = executor._today_et()
    _seed_earnings_calendar(monkeypatch, [
        {"symbol": "AFTER", "reportDate": today, "timeOfTheDay": "post-market"},
    ])
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
        lambda symbol, session: ({"surprise_pct": 30.0, "period": "2026-06-30"}, None),
    )
    monkeypatch.setattr(
        executor, "_liquidity_quote",
        lambda qctx, symbol: {"bid": 9.99, "ask": 10.01, "ask_volume": 1_000,
                              "spread_pct": 0.2, "market_cap": 5_000_000_000},
    )
    monkeypatch.setattr(executor, "_entry_window_reason", lambda session, grade: None)
    monkeypatch.setattr(
        executor,
        "_entry_structure_gate",
        lambda qctx, symbol, session, price: (
            {"vwap": 10.0, "last_close": 10.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0}, None,
            {"vwap": 10.0, "last_close": 10.0, "volume_ratio": 1.0,
             "vwap_dev_pct": 0.0},
        ),
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
