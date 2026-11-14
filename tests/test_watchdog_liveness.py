"""数据新鲜度看门狗 D1/D2/D3/D4 活性对账回归测试 (2026-08-28 主动暴露机制; 2026-09-14 补 D3/D4)。

背景: WATCHLIST 是人工登记的清单 —— 登记不到的源死了永远无人知
(plan_feedback 三条输入死了 3 周 / daemon_heartbeat 空表 4 个月 / earnings_calendar
永久零行使财报 ATR 上限门从未生效)。D1 从 sqlite_master 自动枚举表载体,
D2 从 Hermes cron 表达式反推"本应跑"的时刻与 executions.db 实跑对账。
D3 (2026-09-14) 补 D2 的另一半: 一条 failed 执行记录同样"占位", 于是 DA 主作业
9/11 被 shutdown 打断后 last_status='error' 挂了 3 天而 D2 判它"跑过了"。
D4 (2026-09-14) 补 D3 的另一半: 那次挂死的 DA 进程被 reparent 后**又活了 2天16小时**,
握着生产库 3 个句柄 + 1 条 FutuOpenD 连接 —— "作业失败"报得出, "进程还活着占资源"报不出。

回滚: 删本文件即可, 零生产代码依赖。
"""
import importlib.util
import logging
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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


# ── D1 锁存表类型化豁免 (DEB-20260910 §11.8 选项(b), 2026-09-14) ──────────
# playbook_risk_state 是 RiskCircuitBreaker 的熔断锁存表: 只在熔断真触发时写行,
# 零行 = 从未熔断 = 健康。旧 never_fed 判据把它当"生产者从未喂数", 每轮实跑都推
# 一条假阳性到飞书 (9/12 06:25 `[pushed] 飞书` 实证), 真告警会被常态噪音掩掉。

def test_declared_latch_table_zero_rows_is_healthy(tmp_path, monkeypatch):
    """声明过的锁存表零行不告 —— 判据本身要能区分"没跑"与"跑了但条件没成立"。"""
    db = _mk_db(tmp_path, {"playbook_risk_state": []})
    monkeypatch.setattr(wf, "_main_db", lambda: db)
    monkeypatch.setattr(wf, "_repo_write_targets",
                        lambda: {"playbook_risk_state": {"db.py"}})
    findings, known, observed = wf._check_table_liveness({})
    assert findings == [] and known == 0
    assert any(o["table"] == "playbook_risk_state" for o in observed), "仍须落观测 JSON"


def test_open_positions_zero_rows_is_flat_book_healthy(tmp_path, monkeypatch):
    """open_positions 零行 = 账面全平 = 健康 (2026-10-16 A-1: 清仓后曾被 D1 误报
    never_fed 并经 meta_audit 二次放大成 P0)。"""
    db = _mk_db(tmp_path, {"open_positions": []})
    monkeypatch.setattr(wf, "_main_db", lambda: db)
    monkeypatch.setattr(wf, "_repo_write_targets",
                        lambda: {"open_positions": {"db.py"}})
    findings, known, observed = wf._check_table_liveness({})
    assert findings == [] and known == 0
    assert any(o["table"] == "open_positions" for o in observed), "仍须落观测 JSON"


def test_latch_exemption_does_not_leak_to_other_tables(tmp_path, monkeypatch):
    """防过修: 豁免只覆盖声明过的表名, 未声明的零行表必须照告 (否则 D1 整体失效)。"""
    db = _mk_db(tmp_path, {"playbook_risk_state": [], "earnings_calendar": []})
    monkeypatch.setattr(wf, "_main_db", lambda: db)
    monkeypatch.setattr(wf, "_repo_write_targets",
                        lambda: {"playbook_risk_state": {"db.py"},
                                 "earnings_calendar": {"earnings_guard.py"}})
    keys = {f["key"] for f in wf._check_table_liveness({})[0]}
    assert "D1:earnings_calendar:never_fed" in keys
    assert not any("playbook_risk_state" in k for k in keys)


def test_latch_table_entries_are_all_declared_with_reason():
    """登记纪律: 每条豁免必须带非空理由 (空理由 = 无声扩大豁免面, 等于关掉 D1)。"""
    assert wf.LATCH_TABLES, "豁免清单不应为空 (空 = 本组测试失去意义)"
    for table, reason in wf.LATCH_TABLES.items():
        assert table == table.lower(), f"{table}: 键必须小写 (判据按 t.lower() 匹配)"
        assert isinstance(reason, str) and len(reason.strip()) >= 10, f"{table}: 理由过短"


def test_event_driven_table_long_gap_is_healthy(tmp_path, monkeypatch):
    """manual_order_audit 无新行 = 无人手动下单, 不是生产器死亡。"""
    today = datetime.now()
    db = _mk_db(tmp_path, {
        "manual_order_audit": [
            {"trade_date": (today - timedelta(days=60)).strftime("%Y-%m-%d")}
        ]
    })
    monkeypatch.setattr(wf, "_main_db", lambda: db)
    monkeypatch.setattr(wf, "_repo_write_targets",
                        lambda: {"manual_order_audit": {"manual_order_service.py"}})
    findings, _, observed = wf._check_table_liveness({})
    assert not any("manual_order_audit" in f["key"] for f in findings)
    assert any(o["table"] == "manual_order_audit" for o in observed)


def test_event_driven_exemption_does_not_leak_to_other_tables(tmp_path, monkeypatch):
    """防过修: 未声明的事件态表仍照告, 豁免只覆盖显式登记表。"""
    today = datetime.now()
    db = _mk_db(tmp_path, {
        "manual_order_audit": [{"trade_date": (today - timedelta(days=60)).strftime("%Y-%m-%d")}],
        "stalled_carrier": [{"trade_date": (today - timedelta(days=60)).strftime("%Y-%m-%d")}],
    })
    monkeypatch.setattr(wf, "_main_db", lambda: db)
    monkeypatch.setattr(wf, "_repo_write_targets", lambda: {
        "manual_order_audit": {"manual_order_service.py"},
        "stalled_carrier": {"writer.py"},
    })
    findings, _, _ = wf._check_table_liveness({})
    keys = {f["key"] for f in findings}
    assert "D1:stalled_carrier:stalled" in keys
    assert not any("manual_order_audit" in key for key in keys)


def test_event_driven_table_entries_are_all_declared_with_reason():
    assert wf.EVENT_DRIVEN_TABLES
    for table, reason in wf.EVENT_DRIVEN_TABLES.items():
        assert table == table.lower()
        assert isinstance(reason, str) and len(reason.strip()) >= 10, f"{table}: 理由过短"


def test_age_days_parses_valid_formats_without_intermediate_warnings(caplog):
    with caplog.at_level(logging.WARNING, logger=wf._log.name):
        assert wf._age_days(datetime.now().strftime("%Y-%m-%d")) >= 0
        assert wf._age_days(str(datetime.now().timestamp())) >= 0
        assert wf._age_days("not-a-timestamp") == -1
    assert len(caplog.records) == 1
    assert "timestamp unparseable" in caplog.records[0].getMessage()


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
    # started_at (2026-09-14): D2 的"实跑"判据从 claimed_at 收紧为 started_at —
    #   派发失败行 claimed_at 有值而 started_at IS NULL, 业务脚本从未启动。
    #   2 元组 = 该槽真的跑了 (started_at 落 claimed_at, 保持既有用例语义不变);
    #   3 元组第 3 位可显式传 None 复现派发失败。
    # status/finished_at/error (2026-09-14, D3): 位置 4/5/6, 缺省 = 跑成功。
    #   元组按位补齐, 既有 2/3 元组用例语义完全不变。
    c.execute("CREATE TABLE executions (job_id TEXT, claimed_at TEXT, started_at TEXT,"
              " status TEXT, finished_at TEXT, error TEXT)")
    rows = []
    for e in execs:
        claimed = e[1]
        rows.append((e[0], claimed,
                     e[2] if len(e) > 2 else claimed,
                     e[3] if len(e) > 3 else "completed",
                     e[4] if len(e) > 4 else claimed,
                     e[5] if len(e) > 5 else ""))
    c.executemany("INSERT INTO executions VALUES (?,?,?,?,?,?)", rows)
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


def test_et_pinned_evening_job_not_alarmed_before_real_slot_due(d2):
    """job.timezone=ET 的晚班作业: host CST 幻影槽不得抢跑假阳性 (2026-11-14 D2 修复)。

    now = 2026-11-17 06:25 CST = 11-16 17:25 ET; expr 21:00 ET 周一至周五。
    真实 ET 最近已到期槽 = 周五 11-13 21:00 ET (缺), 周四 11-12 21:00 ET 已跑。
    旧口径按 CST 幻影槽把周一 21:00 CST (真实 ET 槽尚未到期) 计为缺席 → 假阳性。
    """
    now = datetime(2026, 11, 17, 6, 25, tzinfo=timezone(timedelta(hours=8)))
    job = _job("jet", "evening-et", "0 21 * * 1-5", "2026-10-01T00:00:00")
    job["timezone"] = "America/New_York"
    execs = [("jet", "2026-11-13T10:00:00+08:00")]
    findings, _, judged = d2([job], execs, now=now)
    assert findings == []
    assert judged == 1


def test_et_pinned_job_missed_slots_alarm_displayed_in_et(d2):
    """连续缺真实 ET 槽 → 告警, 且「最近应跑」按 ET 挂钟显示而非 host CST。"""
    now = datetime(2026, 11, 17, 6, 25, tzinfo=timezone(timedelta(hours=8)))
    job = _job("jet", "evening-et", "0 21 * * 1-5", "2026-10-01T00:00:00")
    job["timezone"] = "America/New_York"
    execs = [("jet", "2026-11-12T10:00:00+08:00")]
    findings, _, judged = d2([job], execs, now=now)
    assert judged == 1 and len(findings) == 1
    assert "11-13 21:00" in findings[0]["detail"]


def test_et_pinned_morning_job_healthy_on_et_slot(d2):
    """DA main carrier 同型: 08:00 ET 准点跑 → 健康 (周六 CST 晨 = ET 周五盘后)。"""
    now = datetime(2026, 11, 14, 6, 25, tzinfo=timezone(timedelta(hours=8)))
    job = _job("jda", "da-main-et", "0 8 * * 1-5", "2026-10-01T00:00:00")
    job["timezone"] = "America/New_York"
    execs = [("jda", "2026-11-13T21:00:00+08:00")]
    findings, _, judged = d2([job], execs, now=now)
    assert findings == [] and judged == 1


def test_unreadable_hermes_face_is_reported_once(tmp_path, monkeypatch):
    monkeypatch.setattr(wf, "HERMES_JOBS_JSON", tmp_path / "missing.json")
    monkeypatch.setattr(wf, "HERMES_EXEC_DB", tmp_path / "missing.db")
    findings, _, _ = wf._check_hermes_jobs({})
    assert [f["key"] for f in findings] == ["D2:hermes_missing"]
    assert wf._check_hermes_jobs({"D2:hermes_missing": "已知"})[0] == []


# ── D2 "实跑" 判据: 派发失败 (started_at IS NULL) 不算跑过 ────────────────
# 生产实证 2026-09-06 10:00 → 09-07 22:30: `systemd-run --user --scope` 不可用,
# Hermes 写下 12 行 claimed_at 有值 / started_at IS NULL 的派发失败记录。旧判据
# MAX(claimed_at) 把它们当"已跑" ⇒ 盘中「硬止损监控·上午段」连缺 4 槽零告警,
# DA 主作业 9/7 全日零分析 (da_gate_decisions.jsonl 无 9/7 行) 也无人知。

def test_dispatch_failure_slots_count_as_missed(d2):
    """认领了但 worker 从未启动 = 该槽没跑 (9/7 硬止损监控连缺 4 槽的复现)。"""
    now = datetime(2026, 9, 7, 23, 15)
    slots = wf._cron_due_slots("*/30 21-23 * * 1-5", now)[:4]   # 23:00/22:30/22:00/21:30
    jobs = [_job("sl", "硬止损监控·上午段", "*/30 21-23 * * 1-5",
                 (slots[-1] - timedelta(days=30)).isoformat())]
    execs = [("sl", (s + timedelta(seconds=30)).isoformat(), None) for s in slots]
    findings, _, judged = d2(jobs, execs, now=now)
    assert judged == 1, "派发失败的槽必须被判定, 否则与检测器失明不可区分"
    assert [f["key"] for f in findings] == ["D2:sl"]
    assert "从无实跑" in findings[0]["detail"]


def test_started_then_failed_execution_still_counts_as_run(d2):
    """反向防过修: 真启动了但业务失败 (如 alpha-backfill rc=124 超时) 不算停摆。

    这类作业产出了部分数据、也有自己的告警面; 若把它们判成"未跑", D2 会把
    "跑了但失败" 误报成 "静默停摆" — 8 月 alpha-backfill 连续 8 次 rc=124 会刷屏。
    """
    now = datetime(2026, 9, 7, 23, 15)
    slots = wf._cron_due_slots("*/30 21-23 * * 1-5", now)[:4]
    jobs = [_job("ab", "alpha-backfill", "*/30 21-23 * * 1-5",
                 (slots[-1] - timedelta(days=30)).isoformat())]
    execs = [("ab", (slots[0] + timedelta(seconds=30)).isoformat(),
              (slots[0] + timedelta(seconds=45)).isoformat())]
    findings, _, judged = d2(jobs, execs, now=now)
    assert judged == 1 and findings == []


def test_run_column_falls_back_when_started_at_absent(tmp_path, monkeypatch):
    """Hermes 旧 schema (无 started_at 列) → 回落 claimed_at, D2 不得整体炸掉。

    不回落的后果是查询抛 OperationalError 被外层 except 吞成 D2:detector_error,
    调度对账从"部分失明"退化成"全盲"。
    """
    now = datetime(2026, 9, 7, 23, 15)
    slots = wf._cron_due_slots("*/30 21-23 * * 1-5", now)[:4]
    jj = tmp_path / "jobs.json"
    jj.write_text(json.dumps({"jobs": [
        _job("old", "legacy-schema", "*/30 21-23 * * 1-5",
             (slots[-1] - timedelta(days=30)).isoformat())]}), encoding="utf-8")
    edb = tmp_path / "legacy.db"
    c = sqlite3.connect(str(edb))
    c.execute("CREATE TABLE executions (job_id TEXT, claimed_at TEXT)")   # 无 started_at
    c.execute("INSERT INTO executions VALUES (?,?)",
              ("old", (slots[0] + timedelta(seconds=30)).isoformat()))
    c.commit()
    c.close()
    monkeypatch.setattr(wf, "HERMES_JOBS_JSON", jj)
    monkeypatch.setattr(wf, "HERMES_EXEC_DB", edb)
    findings, _, judged = wf._check_hermes_jobs({}, _now=now)
    assert judged == 1 and findings == []


# ── D3 跑了但失败且未恢复 ────────────────────────────────────────────────
# 生产实证: DA 主作业 9/11 20:45 `Interrupted by shutdown before terminal
# completion`, 其后周末无执行 ⇒ last_status='error' 挂 3 天零告警 (D2 判"跑过了")。

@pytest.fixture
def d3(tmp_path, monkeypatch):
    def run(jobs, execs, baseline=None):
        jj, edb = _mk_hermes(tmp_path, jobs, execs)
        monkeypatch.setattr(wf, "HERMES_JOBS_JSON", jj)
        monkeypatch.setattr(wf, "HERMES_EXEC_DB", edb)
        return wf._check_hermes_failures(baseline or {})
    return run


def test_failed_last_execution_alarms(d3):
    """最新一条执行 failed 且 started_at 非空 = 跑了但没跑成, 且此后无成功覆盖 → 告。"""
    jobs = [_job("da", "deep-stock-analysis 每日盘前扫描", "0 20 * * 1-5",
                 "2026-08-01T20:00:00+08:00")]
    execs = [("da", "2026-09-10T20:00:17+08:00", "2026-09-10T20:00:19+08:00",
              "completed", "2026-09-10T20:51:26+08:00", ""),
             ("da", "2026-09-11T20:00:18+08:00", "2026-09-11T20:00:20+08:00",
              "failed", "2026-09-11T20:45:06+08:00",
              "Interrupted by shutdown before terminal completion.")]
    findings, _, judged = d3(jobs, execs)
    assert judged == 1 and [f["key"] for f in findings] == ["D3:da"]
    assert "Interrupted by shutdown" in findings[0]["detail"]
    assert findings[0]["name"] == "deep-stock-analysis 每日盘前扫描"[:36]


def test_recovered_after_failure_is_silent(d3):
    """失败之后有成功执行覆盖 = 已自愈, 不告 (否则每个曾失败过的作业永久告警)。"""
    jobs = [_job("da", "da", "0 20 * * 1-5", "2026-08-01T20:00:00+08:00")]
    execs = [("da", "2026-09-11T20:00:18+08:00", "2026-09-11T20:00:20+08:00",
              "failed", "2026-09-11T20:45:06+08:00", "Interrupted by shutdown"),
             ("da", "2026-09-14T20:00:11+08:00", "2026-09-14T20:00:13+08:00",
              "completed", "2026-09-14T20:48:02+08:00", "")]
    findings, _, judged = d3(jobs, execs)
    assert judged == 1 and findings == []


def test_dispatch_failure_belongs_to_d2_not_d3(d3):
    """分工不重叠: started_at IS NULL (worker 从未启动) 归 D2, D3 不得重复告。

    9/7 那 12 行派发失败若被两面各告一次, 值班人会把同一个根因读成两个故障。
    """
    jobs = [_job("sl", "硬止损监控·上午段", "*/30 21-23 * * 1-5",
                 "2026-08-01T21:00:00+08:00")]
    execs = [("sl", "2026-09-07T21:00:31+08:00", None, "failed",
              "2026-09-07T21:00:31+08:00",
              "Restart-safe cron worker dispatch failed: cannot create restart-safe "
              "systemd scope for gateway child")]
    findings, _, judged = d3(jobs, execs)
    assert judged == 1 and findings == []


def test_disabled_job_failure_is_not_alarmed(d3):
    """已停用/一次性作业不再期望它跑, 末次失败不构成缺口 (否则退役作业永久告警)。"""
    j = _job("old", "退役作业", "0 4 * * *", "2026-08-01T04:00:00+08:00")
    j["enabled"] = False
    execs = [("old", "2026-09-11T04:00:00+08:00", "2026-09-11T04:00:02+08:00",
              "failed", "2026-09-11T04:08:52+08:00", "Interrupted by shutdown")]
    assert d3([j], execs) == ([], 0, 0)


def test_d3_baseline_suppresses_known_flaky_job(d3):
    """棘轮: 已知长期失败面可显式登记静默 (与 D1/D2 同一套基线, 只许减少不许新增)。"""
    jobs = [_job("ab", "alpha-backfill", "30 6 * * 2-6", "2026-08-01T06:30:00+08:00")]
    execs = [("ab", "2026-08-26T06:30:20+08:00", "2026-08-26T06:30:22+08:00",
              "failed", "2026-08-26T07:10:00+08:00", "backfill rc=124 TIMEOUT")]
    findings, _, _ = d3(jobs, execs)
    assert [f["key"] for f in findings] == ["D3:ab"]
    suppressed, known, _ = d3(jobs, execs, baseline={"D3:ab": "已知超时面"})
    assert suppressed == [] and known == 1


def test_d3_degrades_quietly_on_old_schema(tmp_path, monkeypatch):
    """Hermes 旧 schema (无 status 列) → 本维不可判, 静默退出而非抛成 detector_error。

    抛错的后果是外层 except 把它变成 "D3 检测器自身失明" 告警 —— 那会把
    "对方换了 schema" 误报成 "我们的检测器坏了", 且每轮都告。
    """
    jj = tmp_path / "jobs.json"
    jj.write_text(json.dumps({"jobs": [
        _job("j1", "legacy", "0 4 * * *", "2026-08-01T04:00:00+08:00")]}),
        encoding="utf-8")
    edb = tmp_path / "legacy.db"
    c = sqlite3.connect(str(edb))
    c.execute("CREATE TABLE executions (job_id TEXT, claimed_at TEXT)")   # 无 status/started_at
    c.execute("INSERT INTO executions VALUES (?,?)", ("j1", "2026-09-11T04:00:00+08:00"))
    c.commit()
    c.close()
    monkeypatch.setattr(wf, "HERMES_JOBS_JSON", jj)
    monkeypatch.setattr(wf, "HERMES_EXEC_DB", edb)
    assert wf._check_hermes_failures({}) == ([], 0, 0)


def test_d3_unreadable_hermes_face_is_silent(d3, tmp_path, monkeypatch):
    """调度面不可读时 D3 静默 —— 该故障由 D2:hermes_missing 独家负责吼, 不重复告。"""
    monkeypatch.setattr(wf, "HERMES_JOBS_JSON", tmp_path / "missing.json")
    monkeypatch.setattr(wf, "HERMES_EXEC_DB", tmp_path / "missing.db")
    assert wf._check_hermes_failures({}) == ([], 0, 0)


# ── D4 孤儿进程 (调度面已判死 / 进程仍活) ──────────────────────────────
# 生产实证 (DEB-20260910 §16.10(a)): DA cron 的 pipeline 调用没套 timeout, 9/11 20:03
# 那次挂死在对 FutuOpenD 的 socket 读上; hermes 20:45 判 "Interrupted by shutdown" 后
# shell(1075503)+python(1075525) 被 reparent 到 systemd --user 仍存活 2天16小时。

# 9/11 实锤的两个进程原样 (取证 /tmp/orphan_forensics_20260914.txt + DEB §16.10(a)):
#   包装 shell  —— argv 里有 skill 路径 (cd 写在命令行上), cwd 故意给不匹配值;
#   python 子进程 —— argv 里**没有** skill 路径 (cd 发生在父 shell), 只有 cwd 匹配。
# 两者各证一路信号: 只按 cmdline 匹配会漏掉真正握着库句柄/行情连接的子进程。
_SKILL_CWD = "/root/.hermes/skills/deep-stock-analysis/scripts"
_DA_CHILD_CMD = ("/opt/ftseek/FTSeek-US/venv/bin/python pipeline.py --mode full --top 5 "
                 "--symbols  --format json")   # ← $SYMBOLS 展开为空串
_DA_SHELL_CMD = ("/bin/sh -lic set +m; cd " + _SKILL_CWD + " && "
                 "/opt/ftseek/FTSeek-US/venv/bin/python pipeline.py --mode full --top 5 "
                 '--symbols "$SYMBOLS" --format json > /tmp/da_pipeline_out_x.json')
# (pid, ppid, elapsed_hours, cmdline, cwd)
_CHILD = (1075525, 1075503, 64.6, _DA_CHILD_CMD, _SKILL_CWD)
_SHELL = (1075503, 995, 64.6, _DA_SHELL_CMD, "/root")


def _procs(*rows):
    """构造可注入的进程表 (pid, ppid, elapsed_hours, cmdline, cwd), 不碰真 /proc。"""
    return lambda: iter(rows)


def _boom_procs():
    """枚举即抛 —— 用来走各检测器的 detector_error 出口。"""
    raise OSError("/proc unreadable")
    yield    # pragma: no cover — 让它成为生成器函数


def test_orphan_over_threshold_alarms():
    findings, known, judged = wf._check_orphan_processes(
        {}, procs=_procs(_CHILD))
    assert judged == 1 and known == 0
    assert [f["key"] for f in findings] == ["D4:1075525"]
    f = findings[0]
    assert f["kind"] == "D4" and f["name"] == "pid 1075525"
    assert "2.7天" in f["detail"], "存活时长必须可读 (值班人据此判紧急度)"
    assert "ppid=1075503" in f["detail"], "父进程要留在文案里 (判 reparent 用)"
    assert "timeout" in f["action"], "处置动作须写明根因侧补法"


def test_python_child_is_caught_by_cwd_alone():
    """回归钉 (2026-09-14 自测抓到的真缺陷): argv 无 skill 路径的子进程必须靠 cwd 命中。

    漏掉它的后果不是"少一条告警"——它正是**持有 data/ftseek_us.db(+wal/shm) 与
    FutuOpenD ESTAB 连接**的那一个, 包装 shell 反而无害。
    """
    assert _SKILL_CWD not in _DA_CHILD_CMD, "前提: 子进程 argv 里确实没有 skill 路径"
    findings, _, judged = wf._check_orphan_processes({}, procs=_procs(_CHILD))
    assert judged == 1 and [f["key"] for f in findings] == ["D4:1075525"]
    assert "命中 cwd" in findings[0]["detail"], "文案须说明是哪一路信号命中 (便于核误伤)"


def test_wrapper_shell_is_caught_by_cmdline_alone():
    """另一路信号独立成立: cwd 不匹配时, argv 里的 skill 路径仍须命中。"""
    assert _SKILL_CWD in _DA_SHELL_CMD and _SHELL[4] == "/root"
    findings, _, _ = wf._check_orphan_processes({}, procs=_procs(_SHELL))
    assert [f["key"] for f in findings] == ["D4:1075503"]
    assert "命中 cmdline" in findings[0]["detail"]


def test_real_0911_orphan_pair_is_both_caught():
    """9/11 现场两个进程一起喂 → 两个都要抓到 (缺一个 = 泄漏面看不全)。"""
    findings, _, judged = wf._check_orphan_processes({}, procs=_procs(_SHELL, _CHILD))
    assert judged == 2
    assert {f["key"] for f in findings} == {"D4:1075503", "D4:1075525"}


def test_running_job_within_threshold_is_silent():
    """正常在跑的 DA (历史最长 51min) 不得被打成孤儿 —— 否则每晚 20:00 必假告。"""
    fresh = (1075525, 1075503, 0.85, _DA_CHILD_CMD, _SKILL_CWD)
    assert wf._check_orphan_processes({}, procs=_procs(fresh)) == ([], 0, 1)


def test_threshold_boundary_is_silent():
    """判据是 `hours <= ORPHAN_MAX_HOURS → 不告`, 钉住边界方向 (恰等于阈值不告)。"""
    at = wf.ORPHAN_MAX_HOURS
    assert wf._check_orphan_processes(
        {}, procs=_procs((1, 995, at, _DA_CHILD_CMD, _SKILL_CWD))) == ([], 0, 1)
    over, _, _ = wf._check_orphan_processes(
        {}, procs=_procs((1, 995, at + 0.01, _DA_CHILD_CMD, _SKILL_CWD)))
    assert [f["key"] for f in over] == ["D4:1"]


@pytest.mark.parametrize("cmd,cwd", [
    # (cmdline, cwd) 均取 2026-09-14 生产实测值 —— 常驻进程两路信号都不得命中
    ("/opt/ftseek/FTSeek-US/venv/bin/python main.py --start", "/opt/ftseek/FTSeek-US"),
    ("/root/.hermes/hermes-agent/venv/bin/python3 "
     "/opt/ftseek/FTSeek-US/scripts/hermes_worker.py", "/opt/ftseek/FTSeek-US"),
    ("/root/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run",
     "/root/.hermes"),
    ("/usr/lib/systemd/systemd --user", "/root"),      # reparent 落点本身
    ("crond -n", "/"),
])
def test_resident_processes_are_never_flagged(cmd, cwd):
    """防过修: 常驻服务/系统进程活多久都不算孤儿 (模式只认 skill 一次性批处理)。"""
    assert wf._check_orphan_processes(
        {}, procs=_procs((4242, 1, 2000.0, cmd, cwd))) == ([], 0, 0)


def test_d4_baseline_suppresses_known_orphan():
    """棘轮: 确需留守取证的孤儿可显式登记静默 (与 D1/D2/D3 同一套基线, 只减不增)。"""
    rows = (_CHILD,)
    assert [f["key"] for f in wf._check_orphan_processes({}, procs=_procs(*rows))[0]] == ["D4:1075525"]
    assert wf._check_orphan_processes({"D4:1075525": "取证留守"},
                                      procs=_procs(*rows)) == ([], 1, 1)


def test_d4_detector_error_is_surfaced_not_swallowed():
    """枚举异常必须显性化 (返回 [] 会被读成"没有孤儿" = 二次失明, 同 D3 纪律)。"""
    findings, _, _ = wf._check_orphan_processes({}, procs=_boom_procs)
    assert [f["key"] for f in findings] == ["D4:detector_error"]
    assert "OSError" in findings[0]["detail"]
    # 已登记过 detector_error → 静默计数, 不每轮重复告
    assert wf._check_orphan_processes({"D4:detector_error": "已知"},
                                      procs=_boom_procs) == ([], 0, 0)


def _assert_finding_shape(res, dim, third_is_list):
    """各检测器的返回契约: (list, int, int|list) —— 发现位必须是 list。"""
    assert isinstance(res, tuple) and len(res) == 3, f"{dim}: 必须是 3 元组, 实得 {res!r}"
    findings, known, judged = res
    assert isinstance(findings, list), (
        f"{dim}: 发现位必须是 list (下游 collect_liveness 做 list+list 拼接), "
        f"实得 {type(findings).__name__} → `return A if c else [...], 0, 0` 优先级陷阱复发")
    assert isinstance(known, int), f"{dim}: 静默计数必须是 int"
    assert isinstance(judged, list if third_is_list else int), f"{dim}: 第三位类型漂移"


def test_all_detectors_keep_shape_when_own_error_is_baselined(tmp_path, monkeypatch):
    """四维同款钉子 (2026-09-14 真实回归): 自身异常且该异常键**已在基线**时, 仍须守形状契约。

    病根: `return ([], k, j) if baseline.get("Dn:detector_error") else [ {...} ], 0, 0`
    —— 条件表达式比元组构造结合得更紧, 静默分支实际返回 `(([], k, j), 0, 0)`,
    于是 collect_liveness 的 `t_find + h_find + f_find + o_find` 变成 tuple + list
    → TypeError → **整个看门狗崩掉** (四维一起失明, 只剩 cron 的钉钉兜底吼一声)。
    触发条件恰好是"上一轮检测器坏过并被冻结进基线"—— 即最需要它继续工作的时刻。
    """
    boom_dir = tmp_path / "jobs.json"     # 目录: .exists() 为真而 .read_text() 抛
    boom_dir.mkdir()
    monkeypatch.setattr(wf, "HERMES_JOBS_JSON", boom_dir)
    monkeypatch.setattr(wf, "HERMES_EXEC_DB", boom_dir)

    def _db_boom():
        raise OSError("权威库不可达")
    monkeypatch.setattr(wf, "_main_db", _db_boom)

    cases = [
        ("D1", lambda: wf._check_table_liveness({"D1:detector_error": "已知"}), True),
        ("D2", lambda: wf._check_hermes_jobs({"D2:detector_error": "已知"}), False),
        ("D3", lambda: wf._check_hermes_failures({"D3:detector_error": "已知"}), False),
        ("D4", lambda: wf._check_orphan_processes({"D4:detector_error": "已知"},
                                                  procs=_boom_procs), False),
    ]
    for dim, call, third_is_list in cases:
        res = call()
        _assert_finding_shape(res, dim, third_is_list)
        assert res[0] == [], f"{dim}: 已基线化的自身异常应静默, 不得再产告警"


def test_unbaselined_detector_error_still_alarms_for_all_dims(tmp_path, monkeypatch):
    """另一半: 未基线化时四维都必须**吼出来** (显性化纪律, 与上一例互为原子翻容忍)。"""
    boom_dir = tmp_path / "jobs.json"
    boom_dir.mkdir()
    monkeypatch.setattr(wf, "HERMES_JOBS_JSON", boom_dir)
    monkeypatch.setattr(wf, "HERMES_EXEC_DB", boom_dir)

    def _db_boom():
        raise OSError("权威库不可达")
    monkeypatch.setattr(wf, "_main_db", _db_boom)

    for dim, call in [
        ("D1", lambda: wf._check_table_liveness({})),
        ("D2", lambda: wf._check_hermes_jobs({})),
        ("D3", lambda: wf._check_hermes_failures({})),
        ("D4", lambda: wf._check_orphan_processes({}, procs=_boom_procs)),
    ]:
        findings, _, _ = call()
        assert [f["key"] for f in findings] == [f"{dim}:detector_error"], f"{dim} 未显性化"
        assert "OSError" in findings[0]["detail"] or "IsADirectoryError" in findings[0]["detail"]


def test_d4_rollback_disables_dimension_quietly(monkeypatch):
    """回滚 = ORPHAN_PATTERNS 置空: 本维静默不判, 且不得伪造成 detector_error。"""
    monkeypatch.setattr(wf, "ORPHAN_PATTERNS", {})
    assert wf._check_orphan_processes({}, procs=_procs(_SHELL, _CHILD)) == ([], 0, 0)


def test_orphan_pattern_entries_all_declare_reason():
    """登记纪律 (同 LATCH_TABLES): 每条模式必须写明"它挂死为什么是风险"。"""
    assert wf.ORPHAN_PATTERNS, "模式清单不应为空 (空 = 本组测试失去意义)"
    for pat, reason in wf.ORPHAN_PATTERNS.items():
        assert isinstance(pat, str) and len(pat.strip()) >= 8, f"{pat}: 模式过短易误伤"
        assert isinstance(reason, str) and len(reason.strip()) >= 10, f"{pat}: 理由过短"
    assert wf.ORPHAN_MAX_HOURS >= 1.0, "阈值低于 DA 历史最长实跑 (51min) 会天天假告"


def _fake_scan(seen, skipped, rows=()):
    """伪造 /proc 扫描: 回填采样统计并产出给定行 (用来验降级可见化)。"""
    def _it(stats=None):
        if stats is not None:
            stats["seen"], stats["skipped"] = seen, skipped
        return iter(rows)
    return _it


def test_mass_proc_scan_loss_is_surfaced_not_false_green(monkeypatch):
    """/proc 大面积读不到时"0 孤儿"是**假绿** → 必须显性化 (采样降级也是失明的一种)。

    与本文件立项初衷同源: "降级不报警"正是 shadow 墓地/空表 4 个月的根因。
    """
    monkeypatch.setattr(wf, "_iter_processes", _fake_scan(seen=100, skipped=90))
    findings, _, judged = wf._check_orphan_processes({})
    assert judged == 0, "一个样本都没读到, 判定数必须是 0"
    assert [f["key"] for f in findings] == ["D4:proc_scan_degraded"]
    assert "90/100" in findings[0]["detail"]
    assert "假绿" in findings[0]["action"], "文案要说清后果, 否则值班人不知道要重跑"


def test_minor_race_loss_is_not_alarmed(monkeypatch):
    """竞态退出 (listdir 与 read 之间进程消失) 是常态 → 低占比不得告, 否则天天噪音。"""
    monkeypatch.setattr(wf, "_iter_processes", _fake_scan(seen=400, skipped=3))
    assert wf._check_orphan_processes({}) == ([], 0, 0)


def test_injected_procs_path_never_fires_degradation():
    """注入式 procs (单测/回放) 不回填 stats ⇒ 降级维必须自动静默, 不得误报。"""
    findings, _, judged = wf._check_orphan_processes({}, procs=_procs(_CHILD))
    assert judged == 1
    assert all(f["key"] != "D4:proc_scan_degraded" for f in findings)


def test_real_proc_scan_skip_ratio_is_healthy():
    """活体体检: 真机上 /proc 采样降级占比必须远低于阈值 (否则 D4 常态假绿)。"""
    stats = {}
    rows = list(wf._iter_processes(stats))
    seen, skipped = stats.get("seen", 0), stats.get("skipped", 0)
    assert seen > 10, f"枚举到的进程数异常少 ({seen}) → /proc 不可读"
    ratio = skipped / seen
    assert ratio <= wf.PROC_SCAN_SKIP_RATIO, (
        f"本机采样降级 {skipped}/{seen} = {ratio:.0%} 已超阈值 → D4 会常态报假绿")
    assert rows, "必须至少产出可用样本"
def test_iter_processes_field_offsets_are_correct():
    """/proc 解析活体实证: stat 第 4 字段=ppid, 第 22 字段=starttime, 外加 cwd 一路。

    comm 可含空格与括号, 解析必须从**最后一个** ')' 之后切 —— 偏一位就把 ppid 读成
    pgrp、把 starttime 读成 utime, 存活时长随之荒谬。这类错只在真机上炸, 故拿自身
    进程当活体样本, 而不是喂假 /proc (假 /proc 只会证明"我的解析符合我的假设")。
    """
    import os
    me = os.getpid()
    rows = {pid: (ppid, hours, cmd, cwd)
            for pid, ppid, hours, cmd, cwd in wf._iter_processes()}
    assert me in rows, "枚举必须能看到自己"
    ppid, hours, cmd, cwd = rows[me]
    assert ppid == os.getppid(), "ppid 偏移错位 (第4字段)"
    assert -0.1 <= hours < 24, f"本进程刚起, 存活时长应接近 0, 实得 {hours}h → starttime 偏移错位"
    assert "pytest" in cmd or "python" in cmd
    assert cwd == os.getcwd(), "cwd 一路读错会让子进程漏检 (见 test_python_child_is_caught_by_cwd_alone)"


def test_collect_liveness_aggregates_d4(monkeypatch):
    """接线钉: D4 必须真进 collect_liveness 的告警面与计数 (否则写了等于没写)。"""
    monkeypatch.setattr(wf, "_load_liveness_baseline", lambda: {})
    monkeypatch.setattr(wf, "_check_table_liveness", lambda b: ([], 0, []))
    monkeypatch.setattr(wf, "_check_hermes_jobs", lambda b: ([], 0, 0))
    monkeypatch.setattr(wf, "_check_hermes_failures", lambda b: ([], 0, 0))
    monkeypatch.setattr(wf, "_check_orphan_processes",
                        lambda b: ([{"key": "D4:1", "kind": "D4", "name": "pid 1",
                                     "detail": "d", "action": "a"}], 2, 3))
    live = wf.collect_liveness()
    assert [f["key"] for f in live["alarms"]] == ["D4:1"]
    assert live["orphan_procs_judged"] == 3
    assert live["silenced_known"] == 2


def test_message_renders_d4_section():
    """告警文案必须能区分 D4 (值班人看 tag 就知道是进程泄漏, 不是数据停更)。"""
    msg = wf._build_message([], [], {"alarms": [
        {"key": "D4:1075525", "kind": "D4", "name": "pid 1075525",
         "detail": "已存活 2.7天", "action": "kill 并给 cron 套 timeout"}]})
    assert "活性对账" in msg and "孤儿进程" in msg
    assert "[D4]" in msg and "pid 1075525" in msg and "2.7天" in msg


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
