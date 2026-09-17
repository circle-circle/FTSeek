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
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "settle_wos", REPO / "scripts" / "settle_winner_overnight_shadow.py")
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)


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
