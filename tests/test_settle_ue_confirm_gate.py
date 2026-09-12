# -*- coding: utf-8 -*-
"""settle_ue_confirm_gate 行为钉 (2026-09-12 断头传感器修复配套).

钉三件事:
1. 判据机械性: 构造数据必须给出预注册一致结论 (防见数后挪门柱)。
2. fwd 方向语义: 直接消费 signal_snapshots.fwd_ret_t1 (已签名), 不再二次乘方向。
3. 仙股尾部警示: mean 被单笔尾部主导时 median 必须同时呈报 (台账留痕的判读警示)。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.settle_ue_confirm_gate as m  # noqa: E402


def _write_shadow(path: Path, rows: list) -> None:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")


def _confirm_rec(date: str, verdicts: list) -> dict:
    return {"type": "confirm", "date": date, "verdicts": verdicts,
            "counts": {}, "phase": "shadow"}


def _v(sym: str, verdict: str) -> dict:
    return {"symbol": sym, "direction": "LONG", "verdict": verdict,
            "signed_pre_move": 0.01}


def _make_snap_db(path: Path, rows: list) -> None:
    cc = sqlite3.connect(str(path))
    cc.execute("CREATE TABLE signal_snapshots (decision_id TEXT, fwd_ret_t1 REAL)")
    cc.executemany("INSERT INTO signal_snapshots VALUES (?,?)", rows)
    cc.commit()
    cc.close()


def _dates(n):
    return [f"2026-09-{d:02d}" for d in range(1, n + 1)]


def test_ready_to_adjudicate_when_criteria_met(tmp_path, monkeypatch):
    """≥10日 ∧ n≥30 ∧ PRICE_IN≤0 ∧ CONFIRM>0 → READY_TO_ADJUDICATE."""
    days = _dates(10)
    recs, snaps = [], []
    for i, d in enumerate(days):
        # 每日 2 PRICE_IN (fwd 负) + 2 CONFIRM (fwd 正) → 各桶 n=20+... 凑 n≥30 需 15/2
        recs.append(_confirm_rec(d, [
            _v(f"PI{i}A", "PRICE_IN"), _v(f"PI{i}B", "PRICE_IN"),
            _v(f"CF{i}A", "CONFIRM"), _v(f"CF{i}B", "CONFIRM"),
        ]))
        for s in (f"PI{i}A", f"PI{i}B"):
            snaps.append((f"universe_expansion:{d}:{s}", -0.01))
        for s in (f"CF{i}A", f"CF{i}B"):
            snaps.append((f"universe_expansion:{d}:{s}", 0.01))
    # n=20 < 30 → 补 5 天
    for i, d in enumerate(_dates(15)[10:]):
        d = f"2026-09-{11+i:02d}"
        recs.append(_confirm_rec(d, [
            _v(f"PI2{i}A", "PRICE_IN"), _v(f"PI2{i}B", "PRICE_IN"),
            _v(f"CF2{i}A", "CONFIRM"), _v(f"CF2{i}B", "CONFIRM"),
        ]))
        for s in (f"PI2{i}A", f"PI2{i}B"):
            snaps.append((f"universe_expansion:{d}:{s}", -0.01))
        for s in (f"CF2{i}A", f"CF2{i}B"):
            snaps.append((f"universe_expansion:{d}:{s}", 0.01))
    _write_shadow(tmp_path / "ue_confirm_gate.jsonl", recs)
    _make_snap_db(tmp_path / "signal_snapshots.db", snaps)
    monkeypatch.setattr(m, "SHADOW", tmp_path / "ue_confirm_gate.jsonl")
    monkeypatch.setattr(m, "SNAP_DB", tmp_path / "signal_snapshots.db")
    monkeypatch.setattr(m, "OUT", tmp_path / "out.json")
    sys.argv = ["x"]
    rc = m.main()
    assert rc == 0
    rep = json.loads((tmp_path / "out.json").read_text())
    assert rep["verdict"] == "READY_TO_ADJUDICATE"
    assert rep["buckets"]["PRICE_IN"]["fwd_ret_t1_mean_pct"] < 0
    assert rep["buckets"]["CONFIRM"]["fwd_ret_t1_mean_pct"] > 0


def test_keep_sampling_when_price_in_positive(tmp_path, monkeypatch):
    """PRICE_IN 前瞻 >0 → 判据失败 → KEEP_SAMPLING (符号不得翻转)."""
    days = _dates(12)
    recs, snaps = [], []
    for i, d in enumerate(days):
        recs.append(_confirm_rec(d, [_v(f"PI{i}", "PRICE_IN"), _v(f"CF{i}", "CONFIRM"),
                                     _v(f"PI{i}B", "PRICE_IN"), _v(f"CF{i}B", "CONFIRM")]))
        snaps.append((f"universe_expansion:{d}:PI{i}", 0.02))    # 正 → 违反 ≤0
        snaps.append((f"universe_expansion:{d}:PI{i}B", 0.02))
        snaps.append((f"universe_expansion:{d}:CF{i}", 0.01))
        snaps.append((f"universe_expansion:{d}:CF{i}B", 0.01))
    _write_shadow(tmp_path / "ue_confirm_gate.jsonl", recs)
    _make_snap_db(tmp_path / "signal_snapshots.db", snaps)
    monkeypatch.setattr(m, "SHADOW", tmp_path / "ue_confirm_gate.jsonl")
    monkeypatch.setattr(m, "SNAP_DB", tmp_path / "signal_snapshots.db")
    monkeypatch.setattr(m, "OUT", tmp_path / "out.json")
    sys.argv = ["x"]
    m.main()
    rep = json.loads((tmp_path / "out.json").read_text())
    assert rep["verdict"] == "KEEP_SAMPLING"
    assert any("PRICE_IN" in r for r in rep["reasons"])


def test_censored_no_silent_drop(tmp_path, monkeypatch):
    """缺 fwd 的行计 censored, 不静默丢 (分母诚实)."""
    recs = [_confirm_rec("2026-09-01", [_v("AAA", "CONFIRM"), _v("BBB", "CONFIRM")])]
    _write_shadow(tmp_path / "ue_confirm_gate.jsonl", recs)
    _make_snap_db(tmp_path / "signal_snapshots.db",
                  [("universe_expansion:2026-09-01:AAA", 0.01)])  # BBB 缺
    monkeypatch.setattr(m, "SHADOW", tmp_path / "ue_confirm_gate.jsonl")
    monkeypatch.setattr(m, "SNAP_DB", tmp_path / "signal_snapshots.db")
    monkeypatch.setattr(m, "OUT", tmp_path / "out.json")
    sys.argv = ["x"]
    m.main()
    rep = json.loads((tmp_path / "out.json").read_text())
    assert rep["buckets"]["CONFIRM"]["n_verdicts"] == 2
    assert rep["buckets"]["CONFIRM"]["n_with_fwd"] == 1
    assert rep["buckets"]["CONFIRM"]["censored_no_fwd"] == 1


def test_median_reported_with_mean(tmp_path, monkeypatch):
    """仙股尾部警示: mean 与 median 必须同时呈报 (判读不得只看 mean)."""
    recs, snaps = [], []
    days = _dates(11)
    for i, d in enumerate(days):
        recs.append(_confirm_rec(d, [_v(f"P{i}", "PRICE_IN"), _v(f"P{i}B", "PRICE_IN"),
                                     _v(f"P{i}C", "PRICE_IN")]))
        snaps.append((f"universe_expansion:{d}:P{i}", 4.0))      # 一笔仙股尾部 +400%
        snaps.append((f"universe_expansion:{d}:P{i}B", 0.001))
        snaps.append((f"universe_expansion:{d}:P{i}C", -0.001))
    _write_shadow(tmp_path / "ue_confirm_gate.jsonl", recs)
    _make_snap_db(tmp_path / "signal_snapshots.db", snaps)
    monkeypatch.setattr(m, "SHADOW", tmp_path / "ue_confirm_gate.jsonl")
    monkeypatch.setattr(m, "SNAP_DB", tmp_path / "signal_snapshots.db")
    monkeypatch.setattr(m, "OUT", tmp_path / "out.json")
    sys.argv = ["x"]
    m.main()
    rep = json.loads((tmp_path / "out.json").read_text())
    pi = rep["buckets"]["PRICE_IN"]
    assert pi["fwd_ret_t1_mean_pct"] is not None
    assert pi["fwd_ret_t1_median_pct"] is not None
    # mean 被尾部拉高, median 应远小于 mean
    assert pi["fwd_ret_t1_mean_pct"] > pi["fwd_ret_t1_median_pct"] * 10


def test_preregistered_constants_pinned():
    """预注册判据常量钉死 — 改这里=改判据, 必须留痕."""
    assert m.MIN_DAYS == 10
    assert m.MIN_N == 30
    assert m.JUDGED_BUCKETS == ("PRICE_IN", "CONFIRM")
