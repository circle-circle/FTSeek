"""E1 前瞻影子臂一次性重建回归测试 (DEB-20260815 §40.5, 承用户 2026-08-28 裁决 B).

重建语义 = 采集器阶段2 的窗口化重放 (§四十): 同查询过滤/同去重键 (首行优先)/
同记录格式/同借券费参数 + 阶段1 同款 next_open 回填, 只记价不结算
(结算判读归 GATE 1 裁决方, §16.4)。
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import scripts.e1_shadow_collector as e1
import scripts.e1_shadow_rebuild_once as rb

WIN = ("2026-08-24", "2026-08-26")


def _mk_db(path: Path):
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE signal_snapshots (
        symbol TEXT, trade_date TEXT, direction TEXT,
        strength TEXT, strategy_id TEXT)""")
    con.executemany(
        "INSERT INTO signal_snapshots VALUES (?,?,?,?,?)",
        [("US.AAA", "2026-08-24", "LONG", "4", "deep_analysis"),
         ("US.AAA", "2026-08-25", "LONG", "5", "deep_analysis"),
         ("US.BBB", "2026-08-25", "SHORT", "3", "universe_expansion"),
         ("US.CCC", "2026-08-26", "LONG", "4", "deep_analysis"),    # 窗口末日, 无次日bar → next_open 留空
         ("US.AAA", "2026-08-24", "LONG", "2", "universe_expansion"),  # 同键后行 → 首行优先
         ("US.OLD", "2026-08-21", "LONG", "5", "deep_analysis"),    # 窗口前 → 排除
         ("US.NEW", "2026-08-27", "LONG", "5", "deep_analysis"),    # 窗口后 → 排除
         ("US.NEU", "2026-08-25", "NEUTRAL", "5", "deep_analysis"),  # 非 LONG/SHORT → 排除
         ("US.VOT", "2026-08-25", "LONG", "5", "voting_llm")])      # 非 DA/UE → 排除
    con.commit()
    con.close()


def _patch(monkeypatch, tmp_path):
    db = tmp_path / "signal_snapshots.db"
    _mk_db(db)
    out = tmp_path / "e1.jsonl"
    monkeypatch.setattr(e1, "SNAP_DB", db)
    monkeypatch.setattr(e1, "OUT", out)
    monkeypatch.setattr(e1, "CACHE_DIR", tmp_path / "bars")
    # 免实盘 Futu: 合成日线 (08-24/25/26 open=100/110/120, 无 08-27)
    bars = {"2026-08-24": ["2026-08-24", 100.0, 101.0, 99.0, 100.5],
            "2026-08-25": ["2026-08-25", 110.0, 111.0, 109.0, 110.5],
            "2026-08-26": ["2026-08-26", 120.0, 121.0, 119.0, 120.5]}
    monkeypatch.setattr(e1, "get_daily", lambda sym, date: (bars, list(bars.keys())))
    return out


def test_rebuild_collects_window_with_dedup_and_filters(monkeypatch, tmp_path):
    """窗口内 4 键: 同键去重(首行优先)/排除窗口外·NEUTRAL·非DA-UE, 记录字段与采集器同构."""
    out = _patch(monkeypatch, tmp_path)
    stats = rb.rebuild(*WIN)
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert stats["added"] == 4 and stats["no_bar"] == 0 and len(rows) == 4
    by = {(r["symbol"], r["trade_date"], r["direction"]): r for r in rows}
    assert set(by) == {("US.AAA", "2026-08-24", "LONG"), ("US.AAA", "2026-08-25", "LONG"),
                       ("US.BBB", "2026-08-25", "SHORT"), ("US.CCC", "2026-08-26", "LONG")}
    assert by[("US.AAA", "2026-08-24", "LONG")]["strength"] == "4", "同键后行(UE strength=2)不得覆盖首行"
    assert by[("US.AAA", "2026-08-24", "LONG")]["signal_day_open"] == 100.0
    assert by[("US.BBB", "2026-08-25", "SHORT")]["borrow_fee_annual_pct"] == 0.05
    assert by[("US.AAA", "2026-08-24", "LONG")]["borrow_fee_annual_pct"] == 0.0
    assert all(datetime.fromisoformat(r["collected_at"]).tzinfo is not None for r in rows), \
        "采集戳须 tz-aware (naive 墙钟混写 = §四十.1 同族陷阱)"


def test_next_open_backfill_and_window_end_stays_none(monkeypatch, tmp_path):
    """阶段1 同款回填: 有次日bar的补齐; 窗口末日无次日bar留 None (交给采集器后续运行)."""
    out = _patch(monkeypatch, tmp_path)
    stats = rb.rebuild(*WIN)
    assert stats["filled"] == 3
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    by = {(r["symbol"], r["trade_date"]): r for r in rows}
    assert by[("US.AAA", "2026-08-24")]["next_open_price"] == 110.0
    assert by[("US.AAA", "2026-08-24")]["next_open_date"] == "2026-08-25"
    assert by[("US.BBB", "2026-08-25")]["next_open_price"] == 120.0
    assert by[("US.CCC", "2026-08-26")]["next_open_price"] is None


def test_idempotent_and_preserves_existing(monkeypatch, tmp_path):
    """与采集器共用键空间: 已存在键不重写不重复; 重跑零新增."""
    out = _patch(monkeypatch, tmp_path)
    seed = {"symbol": "US.AAA", "trade_date": "2026-08-24", "direction": "LONG",
            "strength": "9", "strategy_id": "deep_analysis", "signal_day_open": 77.0,
            "next_open_price": 999.0, "next_open_date": "2026-08-25",
            "borrow_fee_annual_pct": 0.0, "collected_at": "2026-08-28T03:00:00-04:00"}
    out.write_text(json.dumps(seed) + "\n")
    stats = rb.rebuild(*WIN)
    assert (stats["before"], stats["added"], stats["total"]) == (1, 3, 4)
    stats2 = rb.rebuild(*WIN)
    assert stats2["added"] == 0 and stats2["total"] == 4, "重跑必须幂等"
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert len(rows) == 4
    kept = next(r for r in rows if r["symbol"] == "US.AAA" and r["trade_date"] == "2026-08-24")
    assert kept["strength"] == "9" and kept["next_open_price"] == 999.0, "既有记录字段不被重建覆盖"
