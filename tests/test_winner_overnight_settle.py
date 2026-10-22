# -*- coding: utf-8 -*-
"""RFC-074 赢家隔夜影子结算器契约钉 (2026-09-02 核查批).

锁住的语义:
  W1 悲观序 SL_gap: 次日开盘跳空越过 SL → 开盘价结算
  W2 悲观序 SL_touch: 盘中低/高点触 SL → SL 价结算 (不假设更优出场)
  W3 EOD_next_day: 未触 SL → 次日收盘结算, delta=(close-arm)×qty×方向
  W4 B16 判据样本口径: 汇总只按盈利臂 (arm_profit_pct>0, 毛口径) — arm 点挂
     PRE_CLOSE else 分支会捕到非豁免亏损臂, 照结算但仅留 loser_n 对照,
     不入 n/均值/胜率/by_outcome (近30日实测污染 ~8%)
  W5 原子写: 结算后无 .tmp 残留 (temp+rename)
"""
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "settle_wos", REPO / "scripts" / "settle_winner_overnight_shadow.py")
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)


@pytest.fixture(autouse=True)
def _stub_external_bar_fetch(monkeypatch):
    """RFC-161 挡板: 结算器现在会 fetch-on-miss。

    既有用例 mock 的是 `load_1m`, 一旦它返回 None 就会走到真补拉 (实测直连 OpenD)。
    测试与外部世界之间必须有挡板 — 默认挡成「空」, 需要验证补拉语义的用例自行覆盖。
    """
    monkeypatch.setattr(sw, "_fetch_bars", lambda symbol, day: (None, "empty"))


def _mk_bars(o=100.0, h=None, l=None, c=101.0, n=35):
    """n 根 1min bar [ts,o,h,l,c] (ts 不参与结算逻辑); load_1m 要求 ≥30 根."""
    h = o if h is None else h
    l = o if l is None else l
    return [[f"t{i}", o, h, l, c] for i in range(n)]


def _arm_row(**kw):
    row = {
        "shadow": "winner_overnight", "type": "arm",
        "symbol": "TEST", "direction": "LONG",
        "entry_price": 100.0, "quantity": 10,
        "arm_date": "2026-09-02", "arm_price": 100.0,
        "arm_profit_pct": 1.0, "settled": False,
    }
    row.update(kw)
    return row


class TestSettleRow:
    """W1-W3: 悲观序结算结局 (单位: 价格=USD, delta=USD)."""

    def test_w1_sl_gap_settles_at_open(self, monkeypatch):
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _mk_bars(o=94.0, c=94.0))
        ok, f = sw.settle_row(_arm_row(stop_loss_at_arm=95.0))
        assert ok and f["settle_outcome"] == "SL_gap"
        assert f["settle_price"] == 94.0  # 跳空越过 SL → 开盘价, 非 SL 价

    def test_w2_sl_touch_settles_at_sl(self, monkeypatch):
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _mk_bars(o=100.0, l=94.5))
        ok, f = sw.settle_row(_arm_row(stop_loss_at_arm=95.0))
        assert ok and f["settle_outcome"] == "SL_touch"
        assert f["settle_price"] == 95.0
        assert f["overnight_delta"] == -50.0  # (95-100)×10

    def test_w2_short_touch_uses_high(self, monkeypatch):
        """SHORT 对称: 盘中高点触 SL (上方) → SL 价结算."""
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _mk_bars(o=100.0, h=105.5, c=101.0))
        ok, f = sw.settle_row(_arm_row(direction="SHORT", stop_loss_at_arm=105.0))
        assert ok and f["settle_outcome"] == "SL_touch"
        assert f["settle_price"] == 105.0
        assert f["overnight_delta"] == -50.0  # (105-100)×10×(-1): SHORT 触上方SL=亏

    def test_w3_eod_settles_at_close(self, monkeypatch):
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _mk_bars(o=100.0, c=101.0))
        ok, f = sw.settle_row(_arm_row(stop_loss_at_arm=95.0))
        assert ok and f["settle_outcome"] == "EOD_next_day"
        assert f["settle_price"] == 101.0
        assert f["overnight_delta"] == 10.0  # (101-100)×10


class TestWinnerCaliberSummary:
    """W4-W5: B16 盈利臂口径 + 原子写."""

    def test_summary_filters_losers_and_atomic(self, monkeypatch, tmp_path):
        shadow = tmp_path / "wos.jsonl"
        out = tmp_path / "wos_latest.json"
        rows = [
            _arm_row(),                      # 盈利臂 → B16 样本, delta +10
            _arm_row(arm_profit_pct=-1.0),   # 亏损臂 → 仅对照, delta +10 不入
        ]
        shadow.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                          encoding="utf-8")
        monkeypatch.setattr(sw, "SHADOW", shadow)
        monkeypatch.setattr(sw, "OUT", out)
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _mk_bars(o=100.0, c=101.0))
        rc = sw.main(argv=[])
        summary = json.loads(out.read_text(encoding="utf-8"))
        assert rc == 0
        assert summary["settled_n"] == 2
        assert summary["winner_n"] == 1 and summary["loser_n"] == 1
        assert summary["sum_overnight_delta"] == 10.0  # 亏损臂 +10 不入判据合计
        assert summary["win_rate"] == 1.0
        assert summary["by_outcome"] == {"EOD_next_day": 1}
        # W5 原子写: 无 .tmp 残留; 两行均落 settled 标记
        assert list(tmp_path.glob("*.tmp")) == []
        settled_rows = [json.loads(l) for l in shadow.read_text(encoding="utf-8").splitlines()]
        assert all(r.get("settled") for r in settled_rows)


class TestArmHookIsolation:
    """W6: arm 埋点测试隔离 (2026-09-02 核查批发现) — pytest 环境下
    PRE_CLOSE_EXIT 不得写生产账本 data/shadow/winner_overnight_shadow.jsonl
    (埋点写死生产路径, 无守卫时一次全量测试跑 8 行 TEST 符号落账)."""

    def test_pre_close_scenario_does_not_touch_prod_ledger(self):
        from datetime import timedelta
        from app.playbook.pipeline import _check_playbook_exit_rules
        from app.utils.timezone import now_et

        ledger = REPO / "data" / "shadow" / "winner_overnight_shadow.jsonl"
        before = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
        meta = {"_log_tag": "test_iso", "_symbol": "TEST", "cohort": "A",
                "horizon": "short", "_source_type": "PLAYBOOK_SIM"}
        reason, *_ = _check_playbook_exit_rules(
            direction="LONG", entry_price=100.0, current_price=100.013,
            initial_sl=95.0, peak_price=100.1, meta=meta,
            held_minutes=300, minutes_to_close=5.0, minutes_from_open=385.0,
            entry_time_str=(now_et() - timedelta(minutes=300)).isoformat(),
        )
        assert reason == "PRE_CLOSE_EXIT"  # 场景自证: 埋点分支确被走到
        after = len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
        assert after == before, \
            f"pytest 环境下 arm 埋点不得写生产账本 ({before} → {after} 行)"


class TestPendingStaleAlert:
    """W7 积压棘轮 (2026-09-16): 超 N 天未结算 → app/push HIGH 告警 + exit 1.

    缘起: 9/4 META arm 因次日 1min bar 缺失卡死 12 天, cron 每天只打印
    "未结算积压 1" 而无主动通知 — B16 判据 (n≥20) 在积压清空前不可信。
    """

    def test_w7_stale_detection_threshold_and_malformed_date(self):
        rows = [
            _arm_row(arm_date="2026-09-01"),               # 超龄 → 告警
            _arm_row(arm_date="2026-09-07"),               # 3 天 → 未超龄
            _arm_row(arm_date="not-a-date"),               # 损坏 → 同罪告警
            _arm_row(arm_date="2026-01-01", settled=True), # 已结算 → 永不告警
        ]
        stale = sw._stale_pending(rows, alert_days=3, today=date(2026, 9, 10))
        assert [r["arm_date"] for r in stale] == ["2026-09-01", "not-a-date"]

    def test_w7_main_stale_backlog_alerts_and_exits_1(self, monkeypatch, tmp_path):
        shadow = tmp_path / "wos.jsonl"
        out = tmp_path / "wos_latest.json"
        row = _arm_row(arm_date="2020-01-01")  # 恒超龄 (与运行日期解耦)
        shadow.write_text(json.dumps(row) + "\n", encoding="utf-8")
        monkeypatch.setattr(sw, "SHADOW", shadow)
        monkeypatch.setattr(sw, "OUT", out)
        monkeypatch.setattr(sw, "load_1m", lambda s, d: None)  # bar 缺失 → 保持 pending
        alerted = []
        monkeypatch.setattr(sw, "_push_stale_alert",
                            lambda stale, pending_n, days: alerted.append((stale, pending_n, days)) or True)
        rc = sw.main(argv=[])
        assert rc == 1  # 棘轮: 违反状态对 cron 可见 (exit 1)
        assert len(alerted) == 1
        stale, pending_n, days = alerted[0]
        assert len(stale) == 1 and stale[0]["symbol"] == "TEST"
        assert pending_n == 1 and days == 3
        summary = json.loads(out.read_text(encoding="utf-8"))
        assert summary["pending_n"] == 1 and summary["settled_n"] == 0

    def test_w7_fresh_backlog_does_not_alert(self, monkeypatch, tmp_path):
        shadow = tmp_path / "wos.jsonl"
        out = tmp_path / "wos_latest.json"
        row = _arm_row(arm_date=date.today().isoformat())  # 当日 → 未超龄
        shadow.write_text(json.dumps(row) + "\n", encoding="utf-8")
        monkeypatch.setattr(sw, "SHADOW", shadow)
        monkeypatch.setattr(sw, "OUT", out)
        monkeypatch.setattr(sw, "load_1m", lambda s, d: None)
        alerted = []
        monkeypatch.setattr(sw, "_push_stale_alert",
                            lambda stale, pending_n, days: alerted.append((stale, pending_n, days)) or True)
        rc = sw.main(argv=[])
        assert rc == 0 and alerted == []  # 正常等待次日 bar, 不刷告警

    def test_w7_push_uses_hub_sync_true_and_stops(self, monkeypatch):
        calls = []

        class FakeHub:
            def push(self, msg, sync=False):
                calls.append(("push", msg, sync))
                return True

            def stop(self):
                calls.append(("stop", None, None))

        import app.push as push_mod
        monkeypatch.setattr(push_mod, "get_push_hub", lambda: FakeHub())
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-08")
        ok = sw._push_stale_alert(
            [{"symbol": "US.META", "arm_date": "2026-09-04"}], pending_n=1, alert_days=3)
        assert ok is True
        assert calls[0][0] == "push" and calls[0][2] is True  # sync=True: cron 短命进程必达
        assert calls[-1][0] == "stop"  # 关线程池防 cron 挂死 (error_alert 同课)
        msg = calls[0][1]
        assert "积压 1 条" in msg.title and msg.level is push_mod.Level.HIGH
        assert "US.META" in msg.body and "US_META_2026-09-08.json" in msg.body

    def test_w7_push_failure_still_exits_1(self, monkeypatch, tmp_path):
        shadow = tmp_path / "wos.jsonl"
        out = tmp_path / "wos_latest.json"
        row = _arm_row(arm_date="2020-01-01")
        shadow.write_text(json.dumps(row) + "\n", encoding="utf-8")
        monkeypatch.setattr(sw, "SHADOW", shadow)
        monkeypatch.setattr(sw, "OUT", out)
        monkeypatch.setattr(sw, "load_1m", lambda s, d: None)
        monkeypatch.setattr(sw, "_push_stale_alert", lambda stale, pending_n, days: False)
        rc = sw.main(argv=[])
        assert rc == 1  # 推送失败不吞掉违反状态 (下轮 cron 重试告警)


class TestW8EraFaithfulEffSl:
    """W8 (RFC-140, 2026-10-09): 生效 SL 口径 — arm 行带 era 字段时按 arm 时刻
    BE/锁盈线逐 bar 重放 (非初始 SL 冻结), 旧口径留档双列。

    缘起: 4 笔存量实测旧口径 +183.14 → era-faithful 生效 SL 口径 −142.24
    (4/4 SL 类出场), 初始 SL 口径对「带 SL 持一夜」判据结构性偏乐观。
    """

    def _era_row(self, **kw):
        """带 era 字段的 arm 行 (LONG, entry=100, arm=100.5, peak=1.0%, BE 已武装)."""
        row = _arm_row(arm_price=100.5, quantity=10, **kw)
        row.update({
            "eff_sl_at_arm": 100.6,          # BE+slip 触发线 (紧于初始 SL 95)
            "peak_profit_pct_at_arm": 1.0,
            "era_be_pct": 0.4,
            "era_trailing_enabled": True,
            "era_factor_dd_pct": 0.8,
            "era_fee_cushion_pct": 0.1,
            "era_slip_cushion_pct": 0.2,
            "era_adaptive_on": False,
            "era_cooldown_sl_pct": 4.0,
        })
        row.update(kw)
        return row

    def test_w8_eff_sl_touch_beats_init_sl_eod(self, monkeypatch):
        """BE 武装后价格回踩触发线 → SL_touch@eff, 而初始 SL 口径会给 EOD (偏乐观)."""
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        # 全天在 100.2-100.9 震荡, 收盘 100.8: 初始 SL 95 不触 → EOD +3.0;
        # 生效线 ~100.6 (BE armed, entry*(1+0.3%)) 被 100.2 低点击穿 → SL_touch
        bars = [[f"t{i}", 100.5, 100.9, 100.2, 100.8] for i in range(35)]
        monkeypatch.setattr(sw, "load_1m", lambda s, d: bars)
        ok, f = sw.settle_row(self._era_row(stop_loss_at_arm=95.0))
        assert ok and f["sl_caliber"] == "effective_sl"
        assert f["settle_outcome"] == "SL_touch"
        assert f["settle_outcome_init_sl"] == "EOD_next_day"  # 旧口径对照列留档
        assert f["overnight_delta"] < 0 < f["overnight_delta_init_sl"]  # 方向翻转实证

    def test_w8_no_era_fields_falls_back_legacy(self, monkeypatch):
        """era 字段缺失 (legacy arm 行) → 初始 SL 口径 + sl_caliber 标记 (fail-safe)."""
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _mk_bars(o=100.0, c=101.0))
        ok, f = sw.settle_row(_arm_row(stop_loss_at_arm=95.0))
        assert ok and f["sl_caliber"] == "init_sl_legacy"
        assert f["settle_outcome"] == "EOD_next_day"
        assert f["overnight_delta"] == f["overnight_delta_init_sl"] == 10.0

    def test_w8_peak_ratchet_raises_lock_line(self, monkeypatch):
        """日内峰值升高 → 锁盈线跟随 ratchet (生产每轮重算语义): 高开后深回落
        触发的应是抬高后的锁盈线, 不是 arm 时刻旧线."""
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        # bar0 冲到 103 (peak→3%), dd 0.8% 锁盈线 ≈ 102.18; bar1 跌到 101 触发
        bars = [["t0", 100.5, 103.0, 100.4, 102.8], ["t1", 102.8, 102.9, 101.0, 101.2]]
        bars += [[f"t{i}", 101.2, 101.3, 101.1, 101.2] for i in range(2, 35)]
        monkeypatch.setattr(sw, "load_1m", lambda s, d: bars)
        ok, f = sw.settle_row(self._era_row(stop_loss_at_arm=95.0))
        assert ok and f["settle_outcome"] == "SL_touch"
        # 锁盈出场价应远高于 BE 线 ~100.6 且高于 arm 价 (锁住利润)
        assert f["settle_price"] > 101.5
        assert f["overnight_delta"] > 0  # 锁盈臂也可以是正收益 — 语义正确性关键

    def test_w8_reset_settled_replays(self, monkeypatch, tmp_path):
        """--reset-settled: 已结算行清字段重放 (口径修复后重结算通道)."""
        shadow = tmp_path / "wos.jsonl"
        out = tmp_path / "wos_latest.json"
        row = self._era_row(stop_loss_at_arm=95.0, settled=True,
                            settle_outcome="EOD_next_day", overnight_delta=99.9)
        shadow.write_text(json.dumps(row) + "\n", encoding="utf-8")
        monkeypatch.setattr(sw, "SHADOW", shadow)
        monkeypatch.setattr(sw, "OUT", out)
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-09-03")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _mk_bars(o=100.0, c=101.0))
        rc = sw.main(argv=["--reset-settled"])
        assert rc == 0
        settled = json.loads(shadow.read_text(encoding="utf-8"))
        assert settled["overnight_delta"] != 99.9  # 旧值被重放覆盖
        assert settled["sl_caliber"] == "effective_sl"


# ═══════════════════════════════════════════════════════════════════
#  RFC-161 (2026-10-23): fetch-on-miss + 完整性判据 + 三因分离 + 迟到留痕
# ═══════════════════════════════════════════════════════════════════

def _full_bars(day: str = "2026-09-03", n: int = 390):
    """生成该日 09:31 起的 n 根 1min bar (n=390 → 末根 16:00, 全 RTH)."""
    from datetime import datetime as _dt, timedelta as _td
    t0 = _dt.fromisoformat("%s 09:31:00" % day)
    return [[(t0 + _td(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"), 100.0, 100.5, 99.5, 100.2]
            for i in range(n)]


class TestW9BarCompleteness:
    """W9a: 半日 bar 不得当完整 EOD (静默错值通道)。"""

    def test_partial_cache_treated_as_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sw, "BAR_DIR", tmp_path)
        day, p = "2026-08-21", tmp_path / "US_SPY_2026-08-21.json"
        p.write_text(json.dumps(_full_bars(day, 106)))          # 实测残档形态: 止于 11:16
        assert sw.load_1m("US.SPY", day) is None
        p.write_text(json.dumps(_full_bars(day)))
        assert len(sw.load_1m("US.SPY", day)) == 390            # 全 RTH 才认

    def test_early_close_day_is_complete_at_1259(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sw, "BAR_DIR", tmp_path)
        day = "2026-11-27"                                      # NYSE 提前收盘 13:00 ET
        (tmp_path / ("US_SPY_%s.json" % day)).write_text(json.dumps(_full_bars(day, 210)))
        assert len(sw.load_1m("US.SPY", day)) == 210            # 半日市不算残缺

    def test_short_file_still_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sw, "BAR_DIR", tmp_path)
        day = "2026-08-11"
        (tmp_path / ("US_CMG_%s.json" % day)).write_text(json.dumps(_full_bars(day, 29)))
        assert sw.load_1m("US.CMG", day) is None


class TestW9FetchOnMiss:
    """W9b: 只在该 bar 日收盘后补拉; 三因分离 (等待/无数据/失败)。"""

    def test_no_fetch_before_bar_day_closes(self, monkeypatch):
        called = []
        monkeypatch.setattr(sw, "load_1m", lambda s, d: None)
        monkeypatch.setattr(sw, "_fetch_bars",
                            lambda s, d: called.append((s, d)) or (_full_bars(), "fetched"))
        future = (date.today() + timedelta(days=3)).isoformat()
        bars, status = sw.ensure_bars("US.UNH", future)
        assert (bars, status) == (None, "too_early") and called == []  # 盘中不拉, 更不落半日档

    def test_fetch_used_when_day_closed(self, monkeypatch):
        import app.utils.holiday_calendar as hc
        monkeypatch.setattr(hc, "is_trading_day_str", lambda *a, **k: True)
        monkeypatch.setattr(sw, "load_1m", lambda s, d: None)
        monkeypatch.setattr(sw, "_fetch_bars", lambda s, d: (_full_bars("2026-10-15"), "fetched"))
        bars, status = sw.ensure_bars("US.UNH", "2026-10-15")
        assert status == "fetched" and len(bars) == 390

    def test_cache_hit_never_fetches(self, monkeypatch):
        called = []
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _full_bars())
        monkeypatch.setattr(sw, "_fetch_bars", lambda s, d: called.append(1) or (None, "error"))
        bars, status = sw.ensure_bars("US.UNH", "2026-10-15")
        assert status == "cache" and len(bars) == 390 and called == []

    def test_holiday_day_stays_pending(self, monkeypatch):
        import app.utils.holiday_calendar as hc
        called = []
        monkeypatch.setattr(hc, "is_trading_day_str", lambda *a, **k: False)
        monkeypatch.setattr(sw, "load_1m", lambda s, d: None)
        monkeypatch.setattr(sw, "_fetch_bars", lambda s, d: called.append(1) or (None, "empty"))
        bars, status = sw.ensure_bars("US.UNH", "2026-10-15")
        assert (bars, status) == (None, "not_trading_day") and called == []


class TestW9NoBarAndLag:
    """W9c: no_bar 只在「已收盘+交易日+拉取成功但无全 RTH」连续 2 周期后成立;
    额度/网络失败永不累计; 迟到结算留 settle_lag_days。"""

    def _setup(self, monkeypatch, tmp_path, arm_date="2026-10-15", nxt="2026-10-16"):
        shadow = tmp_path / "wos.jsonl"
        out = tmp_path / "wos_latest.json"
        shadow.write_text(json.dumps(_arm_row(arm_date=arm_date)) + "\n", encoding="utf-8")
        monkeypatch.setattr(sw, "SHADOW", shadow)
        monkeypatch.setattr(sw, "OUT", out)
        monkeypatch.setattr(sw, "next_trading_day", lambda d: nxt)
        monkeypatch.setattr(sw, "load_1m", lambda s, d: None)
        alerted = []
        monkeypatch.setattr(sw, "_push_stale_alert",
                            lambda stale, pending_n, days: alerted.append(len(stale)) or True)
        return shadow, out, alerted

    def test_no_bar_after_two_empty_cycles(self, monkeypatch, tmp_path):
        shadow, out, alerted = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(sw, "_fetch_bars", lambda s, d: (None, "empty"))
        assert sw.main(argv=[]) == 1                       # 第 1 周期: 仍 pending → 积压告警
        row = json.loads(shadow.read_text(encoding="utf-8"))
        assert row.get("bar_miss_n") == 1 and not row.get("bar_no_bar")
        assert sw.main(argv=[]) == 0                       # 第 2 周期: 转 no_bar, 不再告警
        row = json.loads(shadow.read_text(encoding="utf-8"))
        assert row["bar_no_bar"] is True and "empty x2" in row["bar_no_bar_reason"]
        summary = json.loads(out.read_text(encoding="utf-8"))
        assert summary["no_bar_n"] == 1 and summary["pending_n"] == 0
        assert summary["winner_n"] == 0                     # 排除行不进 B16 分母
        assert len(alerted) == 1

    def test_quota_error_never_becomes_no_bar(self, monkeypatch, tmp_path):
        shadow, out, _ = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(sw, "_fetch_bars", lambda s, d: (None, "error"))
        for _ in range(3):
            assert sw.main(argv=[]) == 1                    # 持续待补, 持续告警 (对 cron 可见)
        row = json.loads(shadow.read_text(encoding="utf-8"))
        assert not row.get("bar_no_bar") and row.get("bar_miss_n") is None
        summary = json.loads(out.read_text(encoding="utf-8"))
        assert summary["no_bar_n"] == 0 and summary["pending_n"] == 1

    def test_truncated_after_fetch_also_counts(self, monkeypatch, tmp_path):
        shadow, _, _ = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(sw, "_fetch_bars", lambda s, d: (None, "truncated"))
        sw.main(argv=[])
        sw.main(argv=[])
        row = json.loads(shadow.read_text(encoding="utf-8"))
        assert row["bar_no_bar"] is True and "truncated x2" in row["bar_no_bar_reason"]

    def test_reset_settled_reopens_no_bar(self, monkeypatch, tmp_path):
        shadow = tmp_path / "wos.jsonl"
        out = tmp_path / "wos_latest.json"
        row = _arm_row(arm_date="2026-10-15", bar_no_bar=True, bar_no_bar_reason="empty x2",
                       bar_miss_n=2)
        shadow.write_text(json.dumps(row) + "\n", encoding="utf-8")
        monkeypatch.setattr(sw, "SHADOW", shadow)
        monkeypatch.setattr(sw, "OUT", out)
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-10-16")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _full_bars("2026-10-16"))
        rc = sw.main(argv=["--reset-settled"])
        assert rc == 0
        got = json.loads(shadow.read_text(encoding="utf-8"))
        assert "bar_no_bar" not in got and got["settled"] is True   # 重开后正常结算

    def test_late_settle_records_lag_and_bar_source(self, monkeypatch):
        monkeypatch.setattr(sw, "next_trading_day", lambda d: "2026-10-15")
        monkeypatch.setattr(sw, "load_1m", lambda s, d: _mk_bars())
        ok, f = sw.settle_row(_arm_row(stop_loss_at_arm=95.0))
        assert ok and f["settle_lag_days"] >= 1 and f["bar_source"] == "cache"
        assert f["settle_date"] == "2026-10-15"


class TestW9FetchStatusSeparation:
    """W9d: exit_replay_backtest 层 — 「额度/网络失败」≠「无数据」, 残缓存自动重拉。"""

    @staticmethod
    def _erb():
        import sys
        if str(REPO / "scripts") not in sys.path:
            sys.path.insert(0, str(REPO / "scripts"))
        import exit_replay_backtest
        return exit_replay_backtest

    def test_limit_failure_is_error_not_empty(self, monkeypatch, tmp_path):
        import pandas as pd
        erb = self._erb()
        monkeypatch.setattr(erb, "BAR_DIR", tmp_path)
        monkeypatch.setattr(erb.time, "sleep", lambda *_: None)

        class FlakyCtx:  # RET_OK 但 data=None = 限流/额度耗尽的实测形态
            def request_history_kline(self, *a, **k):
                return 0, None, ""

        status, bars = erb.fetch_bars_with_status(FlakyCtx(), "US.KO", "2026-10-15")
        assert (status, bars) == ("error", None)

    def test_legal_empty_is_empty(self, monkeypatch, tmp_path):
        import pandas as pd
        erb = self._erb()
        monkeypatch.setattr(erb, "BAR_DIR", tmp_path)
        monkeypatch.setattr(erb.time, "sleep", lambda *_: None)

        class EmptyCtx:  # 休市日/停牌: ret=OK + 0 行
            def request_history_kline(self, *a, **k):
                return 0, pd.DataFrame(), ""

        status, bars = erb.fetch_bars_with_status(EmptyCtx(), "US.KO", "2026-10-15")
        assert (status, bars) == ("empty", None)

    def test_truncated_cache_is_refetched_and_repaired(self, monkeypatch, tmp_path):
        import pandas as pd
        erb = self._erb()
        monkeypatch.setattr(erb, "BAR_DIR", tmp_path)
        monkeypatch.setattr(erb.time, "sleep", lambda *_: None)
        day = "2026-08-21"
        p = tmp_path / ("US_SPY_%s.json" % day)
        p.write_text(json.dumps(_full_bars(day, 106)))
        rows = [{"time_key": b[0], "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2}
                for b in _full_bars(day)]

        class FullCtx:
            def request_history_kline(self, *a, **k):
                return 0, pd.DataFrame(rows), ""

        status, bars = erb.fetch_bars_with_status(FullCtx(), "US.SPY", day)
        assert status == "fetched" and len(bars) == 390
        assert len(json.loads(p.read_text())) == 390   # 残档被修成全天版
