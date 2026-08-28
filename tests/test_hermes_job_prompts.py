"""Hermes cron 作业定义守卫 (2026-08-28, Task #15).

起因: 周日综合复盘 job 的 Step 5 把 SQL 直接写在 prompt 里 (SUM(pnl) / date(entry_time)),
trade_records 无 pnl 列 → 该步骤自建 job 以来每次运行必然 OperationalError。prompt 不受
版本控制、不被 pytest 覆盖、改错了没人知道 —— 与 DEB-20260726 "5 个 job 引用被归档脚本"
是同一类失效: 活体调度引用了仓库里的东西, 仓库却对它零约束。

本文件把最小不变量钉住 (仅在装有 hermes 的生产机生效, 其他环境整组 skip):
  1. 任何 job prompt 不得内嵌 SQL / 直连 sqlite (可执行查询必须在 scripts/ 里受测试);
  2. prompt 里以仓库相对路径引用的 scripts/*.py|sh 必须真实存在 (归档/改名即红);
  3. 任何 enabled job 必须有可跑的东西 (agent prompt 或 no_agent script), 不得空转;
  4. 周日复盘 Step 5 必须调 scripts/weekly_trade_summary.py。

回滚: 删本文件即可, 零生产代码依赖。
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JOBS_JSON = Path("/root/.hermes/cron/jobs.json")
LIVE_SCRIPT_DIR = Path("/root/.hermes/scripts")

pytestmark = pytest.mark.skipif(
    not JOBS_JSON.exists(), reason="非生产机 (无 /root/.hermes/cron/jobs.json)")

WEEKLY_JOB_ID = "1e9c597b0262"

# 仓库相对引用: 前面不是 '/' 或单词字符 (排除 /root/.hermes/skills/.../scripts/x.py)
_REPO_SCRIPT = re.compile(r"(?<![/\w])scripts/([A-Za-z0-9_\-]+\.(?:py|sh))")
_SQL_SELECT = re.compile(r"\bSELECT\b.{0,400}?\bFROM\b", re.S | re.I)


@pytest.fixture(scope="module")
def jobs():
    return json.loads(JOBS_JSON.read_text(encoding="utf-8"))["jobs"]


def _prompts(jobs):
    return [(j["id"], j["name"], j.get("prompt") or "") for j in jobs]


def test_no_prompt_embeds_sql(jobs):
    offenders = [f"{jid} ({name})"
                 for jid, name, pr in _prompts(jobs)
                 if _SQL_SELECT.search(pr) or "sqlite3.connect" in pr]
    assert not offenders, (
        "cron prompt 内嵌可执行 SQL (无版本控制/无测试覆盖, 列名漂移只能在生产报错), "
        "请抽为 scripts/ 下脚本并补测试:\n  " + "\n  ".join(offenders))


def test_referenced_repo_scripts_exist(jobs):
    missing = []
    for jid, name, pr in _prompts(jobs):
        for tok in _REPO_SCRIPT.findall(pr):
            if not (REPO / "scripts" / tok).exists():
                missing.append(f"{jid} ({name}) → scripts/{tok}")
    assert not missing, (
        "cron 引用了仓库里不存在的脚本 (被删/被归档/改名 — DEB-20260726 同类复发): \n  "
        + "\n  ".join(missing))


def test_enabled_job_has_something_to_run(jobs):
    empty = []
    for j in jobs:
        if not j.get("enabled"):
            continue
        pr = (j.get("prompt") or "").strip()
        script = j.get("script")
        if len(pr) >= 200:
            continue                      # agent 驱动
        if script:
            assert (LIVE_SCRIPT_DIR / script).exists(), (
                f"{j['id']} ({j['name']}) 的 no_agent 脚本 {script} 在线上不存 — 空转")
            continue
        empty.append(f"{j['id']} ({j['name']})")
    assert not empty, "enabled job 既无 prompt 也无 script, 每次触发都在空转:\n  " + "\n  ".join(empty)


def test_weekly_review_step5_uses_repo_script(jobs):
    job = [j for j in jobs if j["id"] == WEEKLY_JOB_ID]
    assert job, f"周日复盘 job {WEEKLY_JOB_ID} 不见了 — 若已下线请同步删本测试"
    pr = job[0]["prompt"]
    assert "scripts/weekly_trade_summary.py" in pr
    assert "trade_records" not in pr, "Step 5 又把表名写回 prompt 了"


def test_weekly_review_sections_do_not_self_push(jobs):
    """本 job 的最终消息即交付物; 章节脚本若自己再推一次飞书 = 同一内容每周两遍."""
    pr = next(j for j in jobs if j["id"] == WEEKLY_JOB_ID)["prompt"]
    for name in ("ic_weekly_report.py", "symbol_performance_report.py"):
        calls = [l for l in pr.splitlines() if name in l and "python3" in l]
        assert calls, f"prompt 里找不到 {name} 的调用 (章节被删? 同步本测试)"
        for line in calls:
            assert "--no-push" in line, f"{name} 缺 --no-push, 会与本消息重复推送: {line.strip()}"
