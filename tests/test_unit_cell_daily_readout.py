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
        assert [p.name for p in tmp_path.iterdir()] == ["unit_cells_2026-09-09.md"]


class TestReadOnlyDiscipline:
    def test_no_db_write_imports(self):
        src = Path(ucr.__file__).read_text(encoding="utf-8")
        for banned in ("playbook.db", "trade_store", "db_pool", "sqlite3"):
            assert banned not in src, f"读数脚本不得引入写库路径: {banned}"
