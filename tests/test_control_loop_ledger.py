"""控制环路台账守卫 (2026-08-28, 工程控制论造型第 1 步).

起因: 系统实为分层控制系统 (快环=进程内/盘中诊断, 中环=日级测量, 慢环=周级裁决),
但环路清单只存在于散落的 cron 定义与代码线程里 — 新增/下线环路无人对账,
审计全靠救火 (8/28 两个提交修的正是"传感器静默零产出 8 周"这类环路失效)。

docs/CONTROL-LOOP-LEDGER.md 是台账本体。本文件钉住最小不变量
(仅在装有 hermes 的生产机生效, 其他环境整组 skip):
  1. 每个 enabled cron job 的 id 必须出现在台账里 (新环路必须入账);
  2. 台账里引用的 job_id 必须真实存在于 jobs.json (防台账记载幽灵环路);
  3. 台账必须存在且含造型陈述与包络/准入两节 (防台账被删空);
  4. 适应律文档存在且五段齐全 (改参数转移函数的单一真源, 防散回三处)。

回滚: 删本文件 + 删 docs/CONTROL-LOOP-LEDGER.md 即可, 零生产代码依赖。
"""
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JOBS_JSON = Path("/root/.hermes/cron/jobs.json")
LEDGER = REPO / "docs" / "CONTROL-LOOP-LEDGER.md"
ADAPT_LAW = REPO / "docs" / "ADAPTATION-CONTROL-LAW.md"

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


def test_adaptation_law_intact():
    assert ADAPT_LAW.exists(), (
        "适应律文档被删了 (docs/ADAPTATION-CONTROL-LAW.md) — "
        "改参数转移函数将散回宪章/OKR/DEB 三处, 无单一真源")
    text = ADAPT_LAW.read_text(encoding="utf-8")
    for section in ("触发段", "证据段", "增益上限段", "验证段", "回滚段"):
        assert section in text, f"适应律缺段: {section} (转移函数不完整)"
    # 增益上限的硬约束必须钉住, 否则适应律退化为噪声放大器 (DEB-20260823)
    assert "一次一变量" in text, "适应律丢了'一次一变量'增益上界"


def test_g6_earnings_blackout_gate_stays_online():
    """G6 契约 (2026-08-29 复核纠错后): 财报禁建门必须显式在线.

    原裁决 (下线) 依据"earnings_calendar 表 0 行 ⇒ 门从未拦截" — 该证据是死表
    (app/risk/earnings_guard.py SQLite 载体), 与本门实际读的活载体
    (earnings_risk_guard.py 内存态, AV 喂数) 同名不同物。生产日志实锤 8/19-8/26
    拦截 85 次 (BABA 36 / CRM 49) ⇒ 裁决撤销, 门恢复在线。

    本测试钉住"键存在且为 true": 代码缺省也是 true, 但显式声明防止 ①移位到
    PlaybookConfig 读不到的节 (只合并 execution_parameters + risk_parameters);
    ②未来再下线时绕过证据审查 (须先验活载体拦截历史 + 行为级日志证据,
    不能只看死表)。删键虽不改变行为 (缺省 true), 但删键 = 删除在线声明,
    同样红。

    键位: playbook.execution_parameters.* (PlaybookConfig._load 只合并
    risk_parameters + execution_parameters 两个子节, 放别处读不到)。
    """
    import yaml
    cfg = yaml.safe_load((REPO / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
    pb = cfg.get("playbook", {})
    for section in ("execution_parameters", "risk_parameters"):
        val = (pb.get(section) or {}).get("earnings_blackout_gate_enabled", "MISSING")
        if val != "MISSING":
            break
    assert val is True, (
        f"playbook.execution_parameters.earnings_blackout_gate_enabled 应为 true "
        f"(G6 活门在线, 8/19-8/26 拦截 85 次实证), 实际={val!r} — "
        "再下线须先验证活载体 (earnings_risk_guard._earnings_symbols) 拦截历史"
        "与生产日志行为级证据, 不能只看 SQLite 死表")
