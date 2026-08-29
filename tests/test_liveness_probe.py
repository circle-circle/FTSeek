"""liveness_probe 活性探针测试 — 行为级: 超龄/缺失/正常三态必须可区分。

守护的退防: 探针本身若"永远全绿"就是新的失明族成员 — 这些测试用
临时库/临时文件构造 STALE 场景, 断言探针确实会红 (红绿反证)。
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import liveness_probe as lp  # noqa: E402


def _mk_db(path: Path, max_date: str | None):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (trade_date TEXT)")
    if max_date is not None:
        conn.execute("INSERT INTO t VALUES (?)", (max_date,))
    conn.commit()
    conn.close()


def _probe_db(db_rel: str, lag: int = 0) -> lp.Probe:
    return lp.Probe(name="t", kind="db_max", description="t",
                    db=db_rel, sql="SELECT MAX(trade_date) FROM t",
                    lag_trading_days=lag)


def test_db_max_fresh_ok(tmp_path, monkeypatch):
    today = lp._recent_trading_days(1)[0]
    _mk_db(tmp_path / "t.db", today)
    monkeypatch.setattr(lp, "REPO", tmp_path)
    status, _ = lp._run_db_max(_probe_db("t.db"))
    assert status == "OK"


def test_db_max_stale_when_behind_frontier(tmp_path, monkeypatch):
    _mk_db(tmp_path / "t.db", "2026-01-05")  # 远早于任何交易日
    monkeypatch.setattr(lp, "REPO", tmp_path)
    status, detail = lp._run_db_max(_probe_db("t.db"))
    assert status == "STALE"
    assert "落后" in detail


def test_db_max_empty_table_is_stale_not_ok(tmp_path, monkeypatch):
    """空表 = 从未产出, 必须红 (诚实报空, 不得静默当正常)。"""
    _mk_db(tmp_path / "t.db", None)
    monkeypatch.setattr(lp, "REPO", tmp_path)
    status, _ = lp._run_db_max(_probe_db("t.db"))
    assert status == "STALE"


def test_json_ts_missing_file_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "REPO", tmp_path)
    p = lp.Probe(name="t", kind="json_ts", description="t",
                 path="data/liveness/nope.json", ts_field="last_update")
    status, detail = lp._run_json_ts(p)
    assert status == "STALE"
    assert "从未产出" in detail


def test_json_ts_age_threshold(tmp_path, monkeypatch):
    (tmp_path / "st.json").write_text(json.dumps({
        "fresh": datetime.now().isoformat(),
        "old": (datetime.now() - timedelta(hours=30)).isoformat(),
        "epoch": datetime.now().timestamp(),
    }))
    monkeypatch.setattr(lp, "REPO", tmp_path)
    fresh = lp.Probe(name="t", kind="json_ts", description="t",
                     path="st.json", ts_field="fresh", max_age_hours=26)
    old = lp.Probe(name="t", kind="json_ts", description="t",
                   path="st.json", ts_field="old", max_age_hours=26)
    epoch = lp.Probe(name="t", kind="json_ts", description="t",
                     path="st.json", ts_field="epoch", max_age_hours=26)
    assert lp._run_json_ts(fresh)[0] == "OK"
    assert lp._run_json_ts(old)[0] == "STALE"
    assert lp._run_json_ts(epoch)[0] == "OK"


def test_behavioral_fail_marker_detected(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text("print('No data found')")
    p = lp.Probe(name="t", kind="behavioral", description="t",
                 cmd=[sys.executable, str(script)], fail_marker="No data found")
    status, _ = lp._run_behavioral(p)
    assert status == "STALE"


def test_behavioral_clean_passes(tmp_path):
    script = tmp_path / "fake.py"
    script.write_text("print('all good')")
    p = lp.Probe(name="t", kind="behavioral", description="t",
                 cmd=[sys.executable, str(script)], fail_marker="No data found")
    status, _ = lp._run_behavioral(p)
    assert status == "OK"


@pytest.mark.live
def test_real_repo_probes_run_without_error():
    """冒烟: 真实仓库上跑全量探针, 不允许 ERROR (STALE 允许 — 那是探针的职责)。"""
    for p in lp.PROBES:
        if p.kind == "db_max":
            status, detail = lp._run_db_max(p)
        elif p.kind == "json_ts":
            status, detail = lp._run_json_ts(p)
        else:
            continue  # behavioral 冒烟由生产 cron 执行, 测试不重复跑
        assert status != "ERROR", f"{p.name}: {detail}"
