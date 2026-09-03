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
  5. **系统 crontab** 调用的仓内脚本必须在台账或存量棘轮基线里 (2026-09-03 复盘批补:
     原 1/2 只核 Hermes jobs.json, 系统 crontab 是盲区 — 实测 68 个脚本仅 2 个在台账,
     穿透影子两环 9/3 挂 cron 时零守卫拦截即首例实证)。

回滚: 删本文件 + 删 docs/CONTROL-LOOP-LEDGER.md 即可, 零生产代码依赖。
"""
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
JOBS_JSON = Path("/root/.hermes/cron/jobs.json")
LEDGER = REPO / "docs" / "CONTROL-LOOP-LEDGER.md"
ADAPT_LAW = REPO / "docs" / "ADAPTATION-CONTROL-LAW.md"
CRONTAB_BASELINE = REPO / "docs" / "references" / "crontab-loop-baseline.txt"
_SCRIPT_REF = re.compile(r"scripts/([A-Za-z0-9_]+\.py)")

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
    # 台账显式声明的"留痕不入守卫"行豁免 (一次性已销 job 的历史留痕, 非活体环路)。
    # 豁免以行标记为准, 活体环路写入豁免行 = 人工审计面 (同台账 §2.4 入账提醒纪律)。
    guarded = "\n".join(l for l in ledger_text.splitlines() if "留痕不入守卫" not in l)
    phantom = sorted({tok for tok in _JOB_ID.findall(guarded) if tok not in real})
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


def test_early_tight_sl_graduated_variant_stays_on():
    """early_tight_sl 影子毕业契约 (2026-08-29 用户裁决"1批").

    胜出变体 early_tight_30m_mfe0.3: 影子 290 样本/5.5 周净反事实 +27.58pp,
    误杀最少 (20/47)。直攻 SL_INITIAL 主漏 (30 天 11 笔 -$2315, 该批 avg_MFE
    仅 0.068% = 方向从入场即错)。钉住: enabled=true + mfe_gate=0.3 显式在位。
    再下线须先看毕业后的 exit_scorecard 判分 (SL_INITIAL 早期笔数/损失 vs
    基线), 不能凭单日体感。回滚路径: 配置改 false 即恢复, 本测试随之改判。
    """
    import yaml
    cfg = yaml.safe_load((REPO / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
    ep = (cfg.get("playbook", {}).get("execution_parameters") or {})
    assert ep.get("early_tight_sl_enabled") is True, (
        f"early_tight_sl_enabled 应为 true (影子 290 样本净+27.58pp 毕业), "
        f"实际={ep.get('early_tight_sl_enabled')!r}")
    assert ep.get("early_tight_sl_mfe_gate") == 0.3, (
        f"early_tight_sl_mfe_gate 应为 0.3 (胜出变体口径, 无门变体误杀更多), "
        f"实际={ep.get('early_tight_sl_mfe_gate')!r}")


# ── 系统 crontab 面 (2026-09-03 复盘批): 台账守卫原先只看 Hermes, crontab 是盲区 ──
def _cron_scripts() -> set:
    """cron 全解析面 (用户 crontab + /etc/crontab + /etc/cron.d, 注释行不计) 调用的仓内脚本。

    复用 shadow_registry._cron_surface_texts — 与挂载核对哨兵同一解析面 (401bedcb 教训:
    Alinux8 cronie 连含点号文件名也执行, 核面不能只看 crontab -l)。
    """
    from app.playbook.shadow_registry import _cron_surface_texts
    names = set()
    for text in _cron_surface_texts():
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            names.update(_SCRIPT_REF.findall(line))
    return names


def _baseline_entries() -> dict:
    """{脚本 basename: 理由}; 文件缺失返回空 (由下面第一个测试报红)。"""
    if not CRONTAB_BASELINE.exists():
        return {}
    out = {}
    for line in CRONTAB_BASELINE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, _, reason = line.partition("#")
        out[name.strip()] = reason.strip()
    return out


def test_crontab_loops_are_ledgered_or_baselined(ledger_text):
    """新环路无处可藏: crontab 调的仓内脚本必须在台账挂行, 或在存量棘轮基线里。"""
    assert CRONTAB_BASELINE.exists(), (
        f"棘轮基线被删了 ({CRONTAB_BASELINE.relative_to(REPO)}) — 删基线=把 66 个存量环"
        "一次性变成守卫红, 或让新环路重新无处登记; 要收窄请逐行删 (每删一行=该环已入台账)")
    scripts = _cron_scripts()
    assert scripts, "cron 解析面读到 0 个仓内脚本 — 解析面本身坏了 (守卫失明比漏报更糟)"
    base = _baseline_entries()
    unaccounted = sorted(s for s in scripts if s not in ledger_text and s not in base)
    assert not unaccounted, (
        "以下系统 crontab 环路既未在控制环路台账 (docs/CONTROL-LOOP-LEDGER.md §2) 挂行, "
        "也未登记存量棘轮基线 — 台账 §6.1 '新增 cron job 必须入账' 对 crontab 同样生效 "
        "(首列用 crontab-<名> 并写明脚本路径, 先例 crontab-oneshot-0831f / crontab-pen-*):\n  "
        + "\n  ".join(unaccounted))


def test_crontab_baseline_entries_carry_reason():
    """往基线加行必须带理由 (空白 = 把墓地重新藏起来, 同 §6.5 棘轮纪律)。"""
    empty = sorted(k for k, v in _baseline_entries().items() if len(v) < 4)
    assert not empty, f"棘轮基线条目缺理由: {empty} — 存量豁免也必须写明出处/待办"


def test_crontab_baseline_has_no_ghost_entries():
    """反向核对: 基线里的脚本若已不在 crontab 上 = 陈旧豁免 (同台账幽灵环路纪律)。"""
    ghosts = sorted(set(_baseline_entries()) - _cron_scripts())
    assert not ghosts, (
        "棘轮基线记载了已不在 cron 面上的脚本 (陈旧豁免, 会掩盖真实覆盖进度) — "
        "环下线请同步删基线行:\n  " + "\n  ".join(ghosts))


def test_ledger_names_carrier_script_for_crontab_rows(ledger_text):
    """台账里的 crontab-* 行必须写出载体脚本路径 (否则守卫按 basename 匹配不到 = 假入账)。"""
    rows = [l for l in ledger_text.splitlines() if l.startswith("| crontab-")]
    assert rows, "台账已无 crontab-* 行? (先例 crontab-oneshot-0831f / crontab-pen-*)"
    nameless = [l.split("|")[1].strip() for l in rows if not _SCRIPT_REF.search(l)]
    assert not nameless, (
        f"以下 crontab 台账行未写载体脚本路径, 守卫无法据此认定入账: {nameless} — "
        "一次性 job 可写只读脚本名 (如 verify_da_ue_batch_20260831.py)")
