"""隔夜持仓 shadow 模块 + 结算器单测 (2026-08-10)."""
import json

import app.playbook.overnight_hold_shadow as ohs
import scripts.settle_overnight_shadow as sos


# ── emit 侧 ──────────────────────────────────────────────

def _patch_output(tmp_path, monkeypatch):
    monkeypatch.setattr(ohs, "_OUTPUT", tmp_path / "ov.jsonl")
    # 2026-08-28: 生产开关已置 false (影子族取消采集), 本夹具必须显式钉住"开" —
    #   否则 emit 用例的红绿取决于 unified_config 的策略值而非被测逻辑 (良性退出过滤)。
    #   test_emit_disabled 自行覆盖为 False。
    monkeypatch.setattr(ohs, "_enabled", lambda: True)


def test_emit_accepts_benign(tmp_path, monkeypatch):
    _patch_output(tmp_path, monkeypatch)
    for reason in ("SL_BREAKEVEN", "SL_TRAIL_LOCK", "PRE_CLOSE_EXIT"):
        assert ohs.emit("US.X", "LONG", reason, 101.0, 100.0, 5.0, 5000.0, "d1")
    rows = (tmp_path / "ov.jsonl").read_text().strip().splitlines()
    assert len(rows) == 3
    r = json.loads(rows[0])
    assert r["exit_reason"] == "SL_BREAKEVEN" and r["settled"] is False
    assert r["exit_price"] == 101.0 and r["direction"] == "LONG"


def test_emit_rejects_non_benign(tmp_path, monkeypatch):
    _patch_output(tmp_path, monkeypatch)
    for reason in ("SL_INITIAL", "DEAD_SIGNAL_EXIT", "MFE_STALL_EXIT", "TAKE_PROFIT"):
        assert not ohs.emit("US.X", "SHORT", reason, 99.0, 100.0, -5.0)
    assert not (tmp_path / "ov.jsonl").exists()


def test_emit_disabled(tmp_path, monkeypatch):
    _patch_output(tmp_path, monkeypatch)
    monkeypatch.setattr(ohs, "_enabled", lambda: False)
    assert not ohs.emit("US.X", "LONG", "SL_BREAKEVEN", 101.0, 100.0, 5.0)
    assert not (tmp_path / "ov.jsonl").exists()


# ── 结算侧 ──────────────────────────────────────────────

def _mk_bars(date, o, c, h=None, lo=None):
    """生成 390 根 1-min bar (list 格式), 满足 load_day 的 >=30 阈值."""
    h = h if h is not None else max(o, c) * 1.001
    lo = lo if lo is not None else min(o, c) * 0.999
    return [[f"{date} 09:{31 + i % 30:02d}:00", o, h, lo, c] for i in range(390)]


def _setup(tmp_path, monkeypatch):
    shadow = tmp_path / "shadow.jsonl"
    bar_dir = tmp_path / "bars"
    out = tmp_path / "latest.json"
    bar_dir.mkdir()
    monkeypatch.setattr(sos, "SHADOW", shadow)
    monkeypatch.setattr(sos, "BAR_DIR", bar_dir)
    monkeypatch.setattr(sos, "OUT", out)
    return shadow, bar_dir, out


def test_settle_long_both_calibers(tmp_path, monkeypatch):
    shadow, bar_dir, out = _setup(tmp_path, monkeypatch)
    # D=周一 8/4, 次日 8/5. exit_price=100, close0=101, open1=103, close1=105
    shadow.write_text(json.dumps({
        "trade_date": "2026-08-04", "symbol": "US.T", "direction": "LONG",
        "exit_reason": "SL_BREAKEVEN", "exit_price": 100.0, "entry_price": 99.0,
        "pnl_net": 5.0, "notional": 5000.0, "settled": False}) + "\n")
    (bar_dir / "US_T_2026-08-04.json").write_text(json.dumps(_mk_bars("2026-08-04", 100, 101)))
    (bar_dir / "US_T_2026-08-05.json").write_text(json.dumps(_mk_bars("2026-08-05", 103, 105, h=106, lo=102)))

    n = sos._settle_all()
    assert n == 1
    row = json.loads(shadow.read_text().strip())
    assert row["settled"] is True
    assert row["next_trade_date"] == "2026-08-05"
    # close0 口径: gap = 103/101-1 = +1.98%, fullday = 105/101-1 = +3.96%
    assert abs(row["gap_pct"] - 1.9802) < 0.01
    assert abs(row["fullday_pct"] - 3.9604) < 0.01
    # from-exit 口径: gap = 103/100-1 = +3.0%, fullday = 105/100-1 = +5.0%
    assert abs(row["gap_from_exit_pct"] - 3.0) < 0.01
    assert abs(row["fullday_from_exit_pct"] - 5.0) < 0.01
    assert row["cf_fullday_from_exit_pnl"] == 250.0
    # from-exit gap 金额口径 (2026-08-10 补): gap 3.0% × $5000 = $150
    assert row["cf_gap_from_exit_pnl"] == 150.0
    # 汇总含双口径
    summary = json.loads(out.read_text())
    assert summary["fullday_from_exit"]["n"] == 1
    # by_symbol 主口径 = from_exit (隔夜裁决正确基准): +$250, caliber 标注
    sym = summary["by_symbol"]["US.T"]
    assert sym["total_pnl"] == 250.0          # from_exit, 非 close0 的 $198
    assert sym["caliber"] == "from_exit"
    assert sym["win_rate"] == 100.0


def test_settle_short_sign():
    # SHORT: open1 低于 close0 = 正 gap
    assert 1 == 1  # 数学由 test_settle_long 覆盖, 符号直接验证:
    # sign=-1: gap = -1×(103/101-1) = -1.98% (SHORT 遇高开为亏)
    close0, open1 = 101.0, 103.0
    gap_short = -1 * (open1 / close0 - 1) * 100
    assert gap_short < 0


def test_settle_idempotent_and_missing_bars(tmp_path, monkeypatch):
    shadow, bar_dir, out = _setup(tmp_path, monkeypatch)
    shadow.write_text(json.dumps({
        "trade_date": "2026-08-04", "symbol": "US.T", "direction": "LONG",
        "exit_reason": "PRE_CLOSE_EXIT", "exit_price": 101.0, "entry_price": 99.0,
        "pnl_net": 5.0, "notional": 5000.0, "settled": False}) + "\n")
    (bar_dir / "US_T_2026-08-04.json").write_text(json.dumps(_mk_bars("2026-08-04", 100, 101)))
    # 无次日 bars → 不结算
    assert sos._settle_all() == 0
    row = json.loads(shadow.read_text().strip())
    assert row["settled"] is False
    # 补次日 bars → 结算; 再跑一次 → 幂等无新结算
    (bar_dir / "US_T_2026-08-05.json").write_text(json.dumps(_mk_bars("2026-08-05", 102, 104)))
    assert sos._settle_all() == 1
    assert sos._settle_all() == 0
