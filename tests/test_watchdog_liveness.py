"""数据新鲜度看门狗 D1/D2 活性对账回归测试 (2026-08-28, 主动暴露机制)。

背景: WATCHLIST 是人工登记的清单 —— 登记不到的源死了永远无人知
(plan_feedback 三条输入死了 3 周 / daemon_heartbeat 空表 4 个月 / earnings_calendar
永久零行使财报 ATR 上限门从未生效)。D1 从 sqlite_master 自动枚举表载体,
D2 从 Hermes cron 表达式反推"本应跑"的时刻与 executions.db 实跑对账。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "data_freshness_watchdog", PROJECT_ROOT / "scripts" / "data_freshness_watchdog.py"
)
wf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wf)


# ── D1 表级载体活性 ────────────────────────────────────────────────────

_TIME_COLS = ("id INTEGER PRIMARY KEY, trade_date TEXT, created_at TEXT")


def _mk_db(tmp_path, spec):
    """spec: {表名: [行dict, ...]}"""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    for t, rows in spec.items():
        conn.execute(f"CREATE TABLE {t} ({_TIME_COLS})")
        for r in rows:
            conn.execute(
                f"INSERT INTO {t} (trade_date, created_at) VALUES (?,?)",
                (r.get("trade_date"), r.get("created_at")))
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def d1(tmp_path, monkeypatch):
    """把 D1 的两个外部依赖换成合成物 (库文件 + 仓内写入方普查)。"""
    today = datetime.now()
    db = _mk_db(tmp_path, {
        # 有写入方但零行 → never_fed (earnings_calendar 型)
        "empty_carrier": [],
        # 有写入方但停更 60 天 → stalled (生产者静默死亡)
        "stalled_carrier": [{"trade_date": (today - timedelta(days=60)).strftime("%Y-%m-%d")}],
        # 有写入方且新鲜 → 不告
        "live_carrier": [{"trade_date": today.strftime("%Y-%m-%d")}],
        # 无写入方 → 属人工裁决面, 即使零行也不自动告
        "no_writer_carrier": [],
    })
    monkeypatch.setattr(wf, "_main_db", lambda: db)
    monkeypatch.setattr(wf, "_repo_write_targets", lambda: {
        "empty_carrier": {"w.py"}, "stalled_carrier": {"w.py"}, "live_carrier": {"w.py"}})
    return wf, monkeypatch


def test_zero_row_table_with_writer_is_flagged(d1):
    findings, known, observed = d1[0]._check_table_liveness({})
    keys = {f["key"] for f in findings}
    assert "D1:empty_carrier:never_fed" in keys


def test_stalled_table_with_writer_is_flagged(d1):
    findings, _, _ = d1[0]._check_table_liveness({})
    assert any(f["key"] == "D1:stalled_carrier:stalled" for f in findings)


def test_fresh_table_not_flagged(d1):
    findings, _, _ = d1[0]._check_table_liveness({})
    assert not any("live_carrier" in f["key"] for f in findings)


def test_table_without_repo_writer_is_not_flagged(d1):
    """零行但不告: 无现役写入方 = 人工裁决面 (自动告会淹掉真信号)。"""
    findings, _, observed = d1[0]._check_table_liveness({})
    assert not any("no_writer_carrier" in f["key"] for f in findings)
    assert any(o["table"] == "no_writer_carrier" for o in observed), "仍须落观测 JSON"


def test_baseline_suppresses_known_findings(d1):
    """棘轮: 基线内的既有墓地静默计数, 不进告警 (否则首跑 15 条会把真新信号埋掉)。"""
    baseline = {f["key"]: f["detail"] for f in d1[0]._check_table_liveness({})[0]}
    findings, known, _ = d1[0]._check_table_liveness(baseline)
    assert findings == []
    assert known == 2


def test_missing_authoritative_db_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "_main_db", lambda: tmp_path / "nope.db")
    monkeypatch.setattr(wf, "_repo_write_targets", lambda: {})
    findings, _, _ = wf._check_table_liveness({})
    assert [f["key"] for f in findings] == ["D1:main_db_missing"]


# ── D2 Hermes 应跑未跑 ─────────────────────────────────────────────────

def _mk_hermes(tmp_path, jobs, execs):
    jj = tmp_path / "jobs.json"
    jj.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    edb = tmp_path / "exec.db"
    if edb.exists():
        edb.unlink()
    c = sqlite3.connect(str(edb))
    c.execute("CREATE TABLE executions (job_id TEXT, claimed_at TEXT)")
    c.executemany("INSERT INTO executions VALUES (?,?)", execs)
    c.commit()
    c.close()
    return jj, edb


def _job(jid, name, expr, created):
    return {"id": jid, "name": name, "enabled": True,
            "schedule": {"expr": expr}, "created_at": created}


def _slot0(expr, now=None):
    """最近一个已到期槽 (测试基准必须挂在槽上, 挂钟偏移会随时辰漂移出假失败)。"""
    return wf._cron_due_slots(expr, now or datetime.now())[0]


@pytest.fixture
def d2(tmp_path, monkeypatch):
    def run(jobs, execs, baseline=None, now=None):
        jj, edb = _mk_hermes(tmp_path, jobs, execs)
        monkeypatch.setattr(wf, "HERMES_JOBS_JSON", jj)
        monkeypatch.setattr(wf, "HERMES_EXEC_DB", edb)
        return wf._check_hermes_jobs(baseline or {}, _now=now)
    return run


def test_quiet_schedule_is_silent(d2):
    """全部按时跑 → 零告 (回归基准: 今晚 20 个作业实测 0 误报)。"""
    now = datetime.now()
    s0 = _slot0("0 4 * * *", now)
    jobs = [_job("j1", "daily-4am", "0 4 * * *", (s0 - timedelta(days=30)).isoformat())]
    execs = [("j1", (s0 + timedelta(seconds=40)).isoformat())]   # 最近槽已跑
    findings, known, judged = d2(jobs, execs)
    assert findings == [] and known == 0
    assert judged == 1, "必须真的判过一个作业 (judged=0 与检测器失明不可区分)"


def test_two_missed_slots_alarm(d2):
    now = datetime.now()
    s0 = _slot0("0 4 * * *", now)
    jobs = [_job("j1", "daily-4am", "0 4 * * *", (s0 - timedelta(days=30)).isoformat())]
    execs = [("j1", (s0 - timedelta(days=3)).isoformat())]       # 最近 3 槽无实跑
    findings, _, judged = d2(jobs, execs)
    assert judged == 1 and len(findings) == 1
    assert findings[0]["key"] == "D2:j1"
    assert "连续" in findings[0]["detail"]


def test_single_missed_slot_is_not_alarmed(d2):
    """只缺最近一槽 = 认领延迟/边界抖动, 不打扰 (阈值 HERMES_MISS_SLOTS=2)。"""
    s0 = _slot0("0 * * * *")
    jobs = [_job("j1", "hourly", "0 * * * *",
                 (s0 - timedelta(days=5)).isoformat())]
    # 上一槽跑了 (晚 5min 认领), 只有当前槽缺 → missed=1
    execs = [("j1", (s0 - timedelta(hours=1) + timedelta(minutes=5)).isoformat())]
    findings, _, judged = d2(jobs, execs)
    assert judged == 1 and findings == []


def test_not_yet_due_job_is_not_alarmed(d2):
    """新建作业 (created_at 晚于最近应跑槽) 不得算漏跑 —— 这是纯 last_run_at 判据的假阳性来源。"""
    now = datetime.now()
    s0 = _slot0("0 4 * * *", now)
    created = s0 + (now - s0) / 2      # 创建于最近槽之后、此刻之前
    jobs = [_job("jnew", "just-created", "0 4 * * *", created.isoformat())]
    assert d2(jobs, [])[2] == 0


def test_job_outside_execution_coverage_is_not_alarmed(d2):
    """槽位早于 executions.db 覆盖窗 (= 全库最旧实跑记录) 且查不到跑记录 → 不可判, 保守不计。

    覆盖窗按全库算而非按作业算: 环形缓冲里"该作业一条记录都没有"才是不可判,
    "该作业最近刚跑过"绝不能因此变成整段不可判 (那会让 D2 悄悄退化成永不调度对账)。
    """
    now = datetime.now()
    s0 = _slot0("0 9 * * 6", now)
    old = (now - timedelta(days=30)).isoformat()
    jobs = [_job("j1", "weekly", "0 9 * * 6", old)]           # 该作业缓冲里零记录
    # 别的作业的实跑落在 s0 之后 (必存在: s0 < now) → 覆盖下限晚于 s0, j1 全槽窗外
    execs = [("other", (s0 + (now - s0) / 2).isoformat())]
    findings, _, judged = d2(jobs, execs)
    assert judged == 0 and findings == []


_CLOCK_SWEEP = [(0, 0), (3, 59), (4, 0), (4, 1), (9, 0), (12, 30), (22, 13), (23, 59)]


@pytest.mark.parametrize("hh,mm", _CLOCK_SWEEP)
def test_healthy_jobs_verdict_is_clock_independent(d2, hh, mm):
    """D2 全在"槽 vs 此刻"上判定 → 结论不得随跑脚本的挂钟漂移 (原用例正是这样在 22:13 假失败)。"""
    now = datetime(2026, 8, 29, hh, mm)
    h0, d0 = _slot0("0 * * * *", now), _slot0("0 4 * * *", now)
    jobs = [_job("h", "hourly", "0 * * * *", (h0 - timedelta(days=30)).isoformat()),
            _job("d", "daily", "0 4 * * *", (d0 - timedelta(days=30)).isoformat())]
    execs = [("h", (h0 + timedelta(seconds=40)).isoformat()),
             ("d", (d0 + timedelta(seconds=40)).isoformat())]
    findings, _, judged = d2(jobs, execs, now=now)
    assert findings == [] and judged == 2, f"{hh:02d}:{mm:02d} → {findings} judged={judged}"


@pytest.mark.parametrize("hh,mm", _CLOCK_SWEEP)
def test_dead_job_alarms_at_any_clock_time(d2, hh, mm):
    """反向: 真停摆的作业在任何时辰都必须告 (防"某些时辰恰好判不到" = 间歇性失明)。"""
    now = datetime(2026, 8, 29, hh, mm)
    d0 = _slot0("0 4 * * *", now)
    jobs = [_job("d", "dead-daily", "0 4 * * *", (d0 - timedelta(days=30)).isoformat())]
    execs = [("d", (d0 - timedelta(days=5)).isoformat())]
    findings, _, judged = d2(jobs, execs, now=now)
    assert judged == 1 and [f["key"] for f in findings] == ["D2:d"], \
        f"{hh:02d}:{mm:02d} → {findings} judged={judged}"


def test_baseline_suppresses_known_dead_job(d2):
    now = datetime.now()
    jobs = [_job("j1", "dead", "0 4 * * *", (now - timedelta(days=30)).isoformat())]
    execs = [("j1", (now - timedelta(days=10)).isoformat())]
    findings, _, _ = d2(jobs, execs)
    assert len(findings) == 1
    suppressed, known, _ = d2(jobs, execs, baseline={findings[0]["key"]: "已知停摆"})
    assert suppressed == [] and known == 1


def test_unreadable_hermes_face_is_reported_once(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "HERMES_JOBS_JSON", tmp_path / "missing.json")
    monkeypatch.setattr(wf, "HERMES_EXEC_DB", tmp_path / "missing.db")
    findings, _, _ = wf._check_hermes_jobs({})
    assert [f["key"] for f in findings] == ["D2:hermes_missing"]
    assert wf._check_hermes_jobs({"D2:hermes_missing": "已知"})[0] == []


# ── cron 表达式本体 ────────────────────────────────────────────────────

@pytest.mark.parametrize("spec,lo,hi,expected", [
    ("*", 0, 59, 60),
    ("*/15", 0, 59, {0, 15, 30, 45}),
    ("2-6", 0, 6, {2, 3, 4, 5, 6}),
    ("1,15", 0, 59, {1, 15}),
    ("7", 0, 6, set()),          # 越界值裁剪, 不抛
])
def test_cron_field_parsing(spec, lo, hi, expected):
    got = wf._cron_field(spec, lo, hi)
    assert got == (set(range(lo, hi + 1)) if expected == 60 else expected)


def test_cron_due_slots_are_in_past_and_ordered():
    now = datetime(2026, 8, 28, 21, 15)
    slots = wf._cron_due_slots("0 4 * * 2-6", now, 4)
    assert slots and all(s < now for s in slots)
    assert slots == sorted(slots, reverse=True)
    assert all(s.minute == 0 and s.hour == 4 for s in slots)
    assert all(s.weekday() in (1, 2, 3, 4, 5) for s in slots)  # cron 2-6 = 周二..周六


def test_invalid_cron_expression_is_skipped_not_crashed():
    assert wf._cron_due_slots("not a cron", datetime.now()) == []
    assert wf._cron_due_slots("", datetime.now()) == []


# ── 告警文案 ───────────────────────────────────────────────────────────

def test_message_includes_liveness_section():
    live = {"alarms": [{"key": "D1:x:never_fed", "kind": "D1", "detail": "零行落库",
                        "action": "…"},
                       {"key": "D2:j1", "kind": "D2", "name": "dead-job",
                        "detail": "连续 3 槽未跑", "action": "…"}]}
    msg = wf._build_message([], [], live)
    assert "活性对账" in msg and "零行落库" in msg and "dead-job" in msg


def test_message_stays_silent_when_all_green():
    assert wf._build_message([], [], {"alarms": []}) is None
    assert wf._build_message([], [], None) is None


def test_colon_less_key_head_does_not_crash():
    """无冒号的键 (如 D1:main_db_missing 之外的历史形态) 取头名不得 IndexError。"""
    msg = wf._build_message([], [], {"alarms": [{"key": "bare_key", "kind": "D1",
                                                "detail": "d", "action": "a"}]})
    assert "bare_key" in msg
