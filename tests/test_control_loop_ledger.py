"""控制环路台账守卫 (2026-08-28, 工程控制论造型第 1 步).

起因: 系统实为分层控制系统 (快环=进程内/盘中诊断, 中环=日级测量, 慢环=周级裁决),
但环路清单只存在于散落的 cron 定义与代码线程里 — 新增/下线环路无人对账,
审计全靠救火 (8/28 两个提交修的正是"传感器静默零产出 8 周"这类环路失效)。

docs/CONTROL-LOOP-LEDGER.md 是台账本体。本文件钉住最小不变量
(仅在装有 hermes 的生产机生效, 其他环境整组 skip):
  1. 每个 enabled cron job 的 id 必须出现在台账里 (新环路必须入账);
  2. 台账里引用的 job_id 必须真实存在于 jobs.json (防台账记载幽灵环路);
  3. 台账必须存在且含造型陈述与包络/准入两节 (防台账被删空)。

回滚: 删本文件 + 删 docs/CONTROL-LOOP-LEDGER.md 即可, 零生产代码依赖。
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JOBS_JSON = Path("/root/.hermes/cron/jobs.json")
LEDGER = REPO / "docs" / "CONTROL-LOOP-LEDGER.md"

pytestmark = pytest.mark.skipif(
    not JOBS_JSON.exists(), reason="非生产机 (无 /root/.hermes/cron/jobs.json)")

_JOB_ID = re.compile(r"[0-9a-f]{12}")


@pytest.fixture(scope="module")
def jobs():
    return json.loads(JOBS_JSON.read_text(encoding="utf-8"))["jobs"]


@pytest.fixture(scope="module")
def ledger_text():
    assert LEDGER.exists(), "控制环路台账被删了 (docs/CONTROL-LOOP-LEDGER.md)"
    return LEDGER.read_text(encoding="utf-8")


def test_enabled_job_is_in_ledger(jobs, ledger_text):
    unlisted = []
    for j in jobs:
        if not j.get("enabled"):
            continue
        if j["id"] not in ledger_text:
            unlisted.append(f"{j['id']} ({j.get('name')})")
    assert not unlisted, (
        "以下 enabled cron job 未在控制环路台账 (docs/CONTROL-LOOP-LEDGER.md §2) 入账 — "
        "新环路必须声明它属于哪一层、测什么、失效模式:\n  " + "\n  ".join(unlisted))


def test_ledger_job_ids_are_real(jobs, ledger_text):
    real = {j["id"] for j in jobs}
    phantom = sorted({tok for tok in _JOB_ID.findall(ledger_text) if tok not in real})
    assert not phantom, (
        "台账引用了不存在的 job_id (幽灵环路, 记载与活体调度脱钩):\n  "
        + "\n  ".join(phantom))


def test_ledger_has_core_sections(ledger_text):
    for section in ("造型陈述", "稳定包络", "统计准入条件", "开环缺口"):
        assert section in ledger_text, f"台账缺核心节: {section} (台账被删空了?)"
