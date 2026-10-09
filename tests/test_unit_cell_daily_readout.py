#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""unit_cell_daily_readout 契约钉 — 读数面口径与裁决触发器判据.

钉住:
  1. 桶/窗口白名单与 gap_alignment_shadow.bucket_gap 输出集合一致 (单一真源, 防各自漂移)
  2. _agg 聚合口径 (pnl_net None→0, wins 计 >0)
  3. 触发器判据: 双窗同向出血→SHUTDOWN; 当月反阳→降级 CONFLICT_WATCH; 样本不足→不触发
  4. 幂等: 同 --date 重跑覆写同一报告, 不堆叠
  5. 只读纪律: 脚本不 import 任何 db 写路径
"""
from pathlib import Path

from scripts import unit_cell_daily_readout as ucr
from scripts.gap_alignment_shadow import bucket_gap


def _row(tid, direction, bucket, window, net, date="2026-09-08"):
    return {"trade_id": tid, "date": date, "direction": direction,
            "gap_bucket": bucket, "window": window, "pnl_net": net}


class TestBucketWhitelistPinned:
    def test_long_buckets_match_bucket_gap(self):
        produced = {bucket_gap(g, "LONG") for g in (-3, 0, 2, 3.99, 4, 10)}
        assert produced <= ucr.KNOWN_BUCKETS

    def test_short_buckets_match_bucket_gap(self):
        produced = {bucket_gap(g, "SHORT") for g in (-10, -2, -1, 0, 2, 4, 10)}
        assert produced <= ucr.KNOWN_BUCKETS

    def test_no_extra_windows(self):
        assert ucr.KNOWN_WINDOWS == {"open_0930_1000", "after_1000"}


class TestAgg:
    def test_none_net_counts_zero_not_skip(self):
        agg = ucr._agg([_row(1, "LONG", "gap_up_0_4", "open_0930_1000", None)])
        a = agg[("LONG", "gap_up_0_4", "open_0930_1000")]
        assert a["n"] == 1 and a["net"] == 0.0 and a["wins"] == 0

    def test_unknown_bucket_excluded(self, capsys):
        agg = ucr._agg([_row(1, "LONG", "invented_bucket", "open_0930_1000", 100)])
        assert len(agg) == 0
        assert "未知桶/窗口" in capsys.readouterr().out

    def test_wins_strictly_positive(self):
        agg = ucr._agg([_row(1, "LONG", "gap_up_0_4", "open_0930_1000", 0.0)])
        assert agg[("LONG", "gap_up_0_4", "open_0930_1000")]["wins"] == 0


def _mk_agg(cell, n, net):
    return {cell: {"n": n, "net": net, "wins": 0}} if cell else {}


CELL = ("LONG", "flat_or_down_open", "open_0930_1000")


class TestTriggers:
    def test_double_window_bleed_shutdown(self):
        out = ucr._check_triggers(
            _mk_agg(CELL, 110, -872.87), _mk_agg(CELL, 54, -322.28), {})
        assert any("SHUTDOWN_PROPOSE" in t and "|".join(CELL) in t for t in out)

    def test_month_rebound_demotes_to_conflict(self):
        """9月实证情形: 滚动出血但当月 n>=10 净>0 ⇒ 严禁关停, 降级 CONFLICT_WATCH."""
        out = ucr._check_triggers(
            _mk_agg(CELL, 110, -872.87), _mk_agg(CELL, 54, -322.28),
            _mk_agg(CELL, 10, 219.06))
        assert any("CONFLICT_WATCH" in t and "regime" in t for t in out)
        assert not any("SHUTDOWN_PROPOSE" in t for t in out)

    def test_month_rebound_needs_min_n(self):
        """当月 n<10 的反阳不构成豁免 (噪音)."""
        out = ucr._check_triggers(
            _mk_agg(CELL, 110, -872.87), _mk_agg(CELL, 54, -322.28),
            _mk_agg(CELL, 3, 219.06))
        assert any("SHUTDOWN_PROPOSE" in t for t in out)

    def test_hist_positive_roll_negative_conflict(self):
        out = ucr._check_triggers(
            _mk_agg(CELL, 60, 500.0), _mk_agg(CELL, 12, -200.0), {})
        assert any("CONFLICT_WATCH" in t for t in out)

    def test_small_sample_no_trigger(self):
        """全历史 n<30 或滚动 n<10 ⇒ 不触发 (gap_up_4plus n=15 情形)."""
        out = ucr._check_triggers(
            _mk_agg(CELL, 15, -546.18), _mk_agg(CELL, 14, -602.27), {})
        assert out == []


class TestIdempotency:
    def test_same_date_overwrites_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ucr, "REPORT_DIR", tmp_path)
        monkeypatch.setattr(ucr, "JSONL", tmp_path / "nonexistent.jsonl")
        monkeypatch.setattr("sys.argv", ["ucr", "--date", "2026-09-09"])
        ucr.main()
        ucr.main()
        assert [p.name for p in tmp_path.iterdir() if p.suffix == ".md"] == ["unit_cells_2026-09-09.md"]
        # 只过滤 .md: conftest._isolate_live_probe_waiver_file (autouse) 会向 tmp_path
        # 写 live_probe_lock_waiver.json 做四锁破例隔离 — 与本幂等断言无关。


class TestReadOnlyDiscipline:
    def test_no_db_write_imports(self):
        src = Path(ucr.__file__).read_text(encoding="utf-8")
        for banned in ("playbook.db", "trade_store", "db_pool", "sqlite3"):
            assert banned not in src, f"读数脚本不得引入写库路径: {banned}"


# ── D1' 极端跳空禁入影子 (DEB-20260909 §5.2) ──

def _d1_row(tid, ag, net, src="DA", date="2026-09-08"):
    return {"trade_id": tid, "date": date, "direction": "LONG",
            "aligned_gap_pct": ag, "pnl_net": net, "source": src,
            "gap_bucket": "gap_up_0_4", "window": "open_0930_1000"}


class TestD1BannedPredicate:
    def test_boundary_against_leg(self):
        assert ucr._d1_banned(-3.0, 3.0) is True      # 贴线即拦
        assert ucr._d1_banned(-3.0, 3.5) is False     # 更严腿不拦
        assert ucr._d1_banned(-3.5, 3.5) is True
        assert ucr._d1_banned(-2.99, 3.0) is False

    def test_chase_leg_shared(self):
        assert ucr._d1_banned(4.0, 3.0) is True       # 顺向追高 +4 两腿同拦
        assert ucr._d1_banned(4.0, 3.5) is True
        assert ucr._d1_banned(3.99, 3.0) is False
        assert ucr._d1_banned(0.0, 3.0) is False


class TestD1Collect:
    def test_all_equals_sum_of_sources(self):
        rows = [_d1_row(1, -4.0, -100, "DA"), _d1_row(2, -4.0, -50, "UE"),
                _d1_row(3, 1.0, 999, "DA")]
        legs, n_no_gap = ucr._d1_collect(rows)
        a = legs[3.0]
        assert a["ALL"]["n"] == 2 and a["ALL"]["net"] == -150
        assert a["DA"]["n"] == 1 and a["UE"]["n"] == 1
        assert a["ALL"]["n"] == a["DA"]["n"] + a["UE"]["n"]
        assert n_no_gap == 0

    def test_kill_counts_strictly_positive_net(self):
        legs, _ = ucr._d1_collect([_d1_row(1, -4.0, 0.0), _d1_row(2, -4.0, 5.0)])
        assert legs[3.0]["ALL"]["kills"] == 1   # net=0 不算误杀

    def test_none_gap_skipped_and_counted(self):
        r = _d1_row(1, None, -100)
        legs, n_no_gap = ucr._d1_collect([r])
        assert n_no_gap == 1 and legs[3.0] == {}

    def test_leg_35_is_strict_subset_of_leg_30(self):
        rows = [_d1_row(1, -3.2, -10), _d1_row(2, -3.6, -10), _d1_row(3, 5.0, -10)]
        legs, _ = ucr._d1_collect(rows)
        assert legs[3.0]["ALL"]["n"] == 3
        assert legs[3.5]["ALL"]["n"] == 2       # −3.2 只被 −3 腿拦


class TestD1LegVerdict:
    def test_n_gate(self):
        assert ucr._d1_leg_verdict(29, -100, 0)[0] is False
        assert ucr._d1_leg_verdict(30, -100, 0)[0] is True

    def test_net_strictly_negative(self):
        assert ucr._d1_leg_verdict(30, 0.0, 0)[1] is False
        assert ucr._d1_leg_verdict(30, -0.01, 0)[1] is True

    def test_kill_rate_boundary(self):
        assert ucr._d1_leg_verdict(30, -100, 12)[2] is True    # 12/30 = 40% 贴线过
        assert ucr._d1_leg_verdict(30, -100, 13)[2] is False   # 43% 不过
        assert ucr._d1_leg_verdict(0, -100, 0)[2] is False     # 空篮不算过


class TestD1Report:
    def test_leg_lines_and_agreement(self):
        rows = [_d1_row(i, -4.0, -10) for i in range(30)]          # −3腿: n30 净-300 误杀0
        rows += [_d1_row(100 + i, -3.2, -10) for i in range(5)]    # 只进 −3腿
        lines = ucr._fmt_d1(rows, [])
        text = "\n".join(lines)
        assert "ag≤−3.0 或 ≥+4" in text and "ag≤−3.5 或 ≥+4" in text
        assert "✅ 同向" in text   # 两腿都被拦净<0 且 n 门: −3.5腿 n=30 也过

    def test_legs_disagree_flagged(self):
        rows = [_d1_row(i, -3.2, -10) for i in range(30)]   # −3腿过, −3.5腿 n=0 不过
        text = "\n".join(ucr._fmt_d1(rows, []))
        assert "❌ 分歧" in text


# ── D3' 按源监控 (DEB-20260909 §5.2①) ──

class TestBySource:
    def test_sl_initial_rate_and_mfe0(self):
        rows = [
            dict(_d1_row(1, 1.0, -50, "UE"), exit_reason="SL_INITIAL", mfe_pct=0.0),
            dict(_d1_row(2, 1.0, 30, "UE"), exit_reason="SL_TRAIL_LOCK", mfe_pct=1.2),
            dict(_d1_row(3, 1.0, 10, "DA"), exit_reason="SL_BREAKEVEN", mfe_pct=0.0),
        ]
        text = "\n".join(ucr._fmt_by_source(rows, "测试窗"))
        assert "| UE | 2 | 50% | -20.00 | 50.0%(1) | 50.0%(1) |" in text
        assert "| DA | 1 | 100% | +10.00 | 0.0%(0) | 100.0%(1) |" in text

    def test_mfe_none_excluded_from_denominator(self):
        rows = [dict(_d1_row(1, 1.0, 5, "DA"), exit_reason="X", mfe_pct=None)]
        text = "\n".join(ucr._fmt_by_source(rows, "测试窗"))
        assert "| DA | 1 | 100% | +5.00 | 0.0%(0) | — |" in text


class TestReportIncludesNewSections:
    def test_main_renders_d1_and_by_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ucr, "REPORT_DIR", tmp_path)
        monkeypatch.setattr(ucr, "JSONL", tmp_path / "nonexistent.jsonl")
        monkeypatch.setattr("sys.argv", ["ucr", "--date", "2026-09-09"])
        ucr.main()
        text = (tmp_path / "unit_cells_2026-09-09.md").read_text(encoding="utf-8")
        assert "D1' 极端跳空禁入影子" in text
        assert "D3' 按源监控" in text
