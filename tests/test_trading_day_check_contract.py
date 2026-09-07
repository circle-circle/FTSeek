"""`scripts/trading_day_check.py` 输出契约 + 全仓消费方回归测试 (2026-09-07).

根因 (2026-09-07 全仓 grep 实证, 与 DA skill pitfall `-6` 同一族病灶)
--------------------------------------------------------------------
旧实现的 docstring 写着「输出 TRADING_DAY/NON_TRADING_DAY, **exit code 始终 0**,
调用方按 stdout 内容判断, 不按 exit code」, 但实测**全仓 9 个消费方无一按 stdout 判断**:
  - 4 个 hermes cron prompt 的 Step 0: `echo "EXIT=$?"` +「如果 EXIT=1(非交易日)回复
    [SILENT] 并停止」(`7a40dd7c9e94` 盘后综合复盘 / `2d69370d5c91` 盘前综合预检 /
    `9ac3efd8c5e7` Deepulse 日报 / `3a6c83bbd84c` deep-analysis 盘后复盘+记忆更新)
  - `scripts/da_supply_reconcile_check.py:99`     `return r.returncode == 0`
  - `scripts/premarket_reprice_shadow.py:73`      `return r.returncode == 0`
  - `scripts/business_heartbeat.py:95`            `if r.returncode != 0: return 0`
  - `ops/hermes/intraday_diagnose.py:45`          `if r.returncode != 0: sys.exit(0)`
  - `ops/hermes/intraday_diagnose_late.py:53`     `if result.returncode != 0: ...`
⇒ exit code 恒 0 使**这 9 处的假日分支全是死代码**: 全系统交易日守卫实际退化为「只排周末」,
  而 cron 表达式本就排周末 ⇒ **等于零守卫**。美国假日照跑盘后复盘/盘前预检/盘中诊断/
  shadow 记账, 并向飞书推送基于陈旧数据的报告。

修法选择: 改**工具**(exit 1 = 非交易日) 而非逐个改 9 个消费方 —— 一处修复同时救活
  4 段 prompt 文本(改动面大、易错、无测试保护) + 5 处代码。stdout token 逐字不变,
  故按 stdout 判断的调用方(本批新改的两条 intraday 脚本)同样正确。

本测试钉住:
  1. **exit code 与 stdout 一致且都等于 SSOT 判定** (契约本体; 日期无关, CI 天天有效)
  2. **劳动节 2026-09-07 → exit 1** (死分支复活的正面证明; 用替身日期, 不依赖 CI 当天)
  3. 周末判断**零依赖**(SSOT 挂掉仍拦得住) + 工作日 SSOT 挂掉 **fail-open**(照报不误拦)
  4. 两条 intraday 脚本改按 stdout 判定, 且三份副本(上游 skill / 仓内 ops / cron 活体)字节一致
  5. 两个仓内 gate 函数现在真的会随 SSOT 变动(此前恒 True = 死门)
  6. cron prompt 的 `EXIT=1` 惯用法仍在(工具契约与 prompt 的耦合点, 防单边改动再次脱钩)
"""
import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TDC = ROOT / "scripts" / "trading_day_check.py"
PY = sys.executable

if not TDC.exists():  # pragma: no cover
    pytest.skip("scripts/trading_day_check.py 不存在", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location("_tdc_under_test", TDC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(*args):
    return subprocess.run([PY, str(TDC), *args], capture_output=True, text=True,
                          timeout=60, cwd=str(ROOT))


def _ssot_verdict() -> bool:
    from app.utils.holiday_calendar import get_holiday_calendar
    from app.utils.timezone import now_et
    return bool(get_holiday_calendar().is_trading_day("US", now_et().date()))


LABOR_DAY_2026 = date(2026, 9, 7)     # 周一, NYSE 全日休市
TUESDAY = date(2026, 9, 8)            # 劳动节后首个交易日
SATURDAY = date(2026, 9, 5)


@pytest.fixture
def cli_argv(monkeypatch):
    """`main()` 内是 argparse, 会读 pytest 自己的 sys.argv ⇒ 必须替身。"""
    monkeypatch.setattr(sys, "argv", ["trading_day_check.py", "--market", "US"])


# ══════════════════════════════════════════════════════════════════════════
# 1. 契约本体
# ══════════════════════════════════════════════════════════════════════════
def test_stdout_token_and_exit_code_agree():
    """stdout 只允许两个逐字 token, 且 exit code 必须与它同义 (0=交易/1=非交易)。"""
    r = _run("--market", "US")
    token = r.stdout.strip()
    assert token in ("TRADING_DAY", "NON_TRADING_DAY"), f"意外 stdout: {r.stdout!r}"
    assert (r.returncode == 0) == (token == "TRADING_DAY"), (
        f"exit code {r.returncode} 与 stdout {token} 不同义 —— 消费方会读错")
    assert r.returncode in (0, 1), "exit code 只允许 0/1 (其它值会被消费方误读成假日)"


def test_verdict_matches_repo_ssot():
    """工具结论必须等于仓内 SSOT `is_trading_day()` (日期无关, CI 天天有效)。"""
    r = _run("--market", "US")
    assert (r.stdout.strip() == "TRADING_DAY") is _ssot_verdict()


def test_exit_code_one_on_labor_day(monkeypatch, cli_argv):
    """**死分支复活的正面证明**: 劳动节必须 exit 1, 否则 4 个 cron 的 `EXIT=1` 永不触发。"""
    mod = _load()
    monkeypatch.setattr(mod, "_today", lambda market: LABOR_DAY_2026)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main()
    assert buf.getvalue().strip() == "NON_TRADING_DAY"
    assert rc == 1, "劳动节未返回 exit 1 ⇒ cron prompt 的「EXIT=1 → [SILENT]」仍是死代码"


def test_exit_code_zero_on_trading_day(monkeypatch, cli_argv):
    """反向: 交易日必须 exit 0 —— 误拦比白跑代价大 (整条链集体静默)。"""
    mod = _load()
    monkeypatch.setattr(mod, "_today", lambda market: TUESDAY)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main()
    assert buf.getvalue().strip() == "TRADING_DAY" and rc == 0


# ══════════════════════════════════════════════════════════════════════════
# 2. 判定正确性 (替身日期, 不依赖 CI 当天)
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("d,expected,why", [
    (LABOR_DAY_2026, False, "劳动节 (旧实现的平行假日表漏了它)"),
    (TUESDAY, True, "假日后首个交易日"),
    (SATURDAY, False, "周六"),
    (date(2026, 9, 6), False, "周日"),
    (date(2026, 11, 26), False, "感恩节"),
    (date(2026, 11, 27), True, "感恩节次日 = 半日, 仍是交易日"),
    (date(2026, 10, 12), True, "哥伦布日: 联邦假日但 NYSE 照常开盘"),
    (date(2026, 11, 11), True, "退伍军人日: 同上 (别拿债市日历套股市)"),
    (date(2026, 12, 25), False, "圣诞"),
    (date(2027, 12, 24), False, "圣诞落周六 → 观察在周五 12/24, 整休"),
    (date(2027, 12, 31), True, "元旦 2028 落周六 NYSE 不补休 → 周五完整交易日"),
])
def test_us_verdict_per_date(monkeypatch, d, expected, why):
    mod = _load()
    monkeypatch.setattr(mod, "_today", lambda market: d)
    assert mod._is_trading("US") is expected, f"{d} {why}"


def test_hk_path_is_weekend_only(monkeypatch):
    """港股维持原简化口径 (仅周末) — 本仓无港股假日表, 不假装有。"""
    mod = _load()
    monkeypatch.setattr(mod, "_today", lambda market: SATURDAY)
    assert mod._is_trading("HK") is False
    monkeypatch.setattr(mod, "_today", lambda market: TUESDAY)
    assert mod._is_trading("HK") is True


# ══════════════════════════════════════════════════════════════════════════
# 3. 失败语义: 周末零依赖 + 工作日 fail-open
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture
def break_ssot(monkeypatch):
    """让 `app.utils.holiday_calendar` 导入即炸 (模拟仓内 SSOT 不可用)。"""
    monkeypatch.setitem(sys.modules, "app.utils.holiday_calendar", None)


def test_weekend_guard_survives_ssot_outage(monkeypatch, break_ssot):
    """周末判断必须零依赖: SSOT 挂掉时周六/周日仍然拦得住 (旧实现的最终兜底口径)。"""
    mod = _load()
    monkeypatch.setattr(mod, "_today", lambda market: SATURDAY)
    assert mod._is_trading("US") is False


def test_fail_open_on_weekday_when_ssot_down(monkeypatch, break_ssot):
    """工作日 + SSOT 挂掉 → **fail-open 视为交易日**。

    9 个消费方里多数是「非交易日即静默」, fail-close 会让它们在依赖故障日集体哑掉;
    宁可假日白跑一轮 (与 DA skill `trading_calendar` / UE / playbook 同口径)。
    """
    mod = _load()
    monkeypatch.setattr(mod, "_today", lambda market: LABOR_DAY_2026)
    assert mod._is_trading("US") is True


def test_exit_code_never_leaks_crash_as_holiday(monkeypatch, cli_argv):
    """崩溃不得伪装成假日: 内部异常 → exit 0 + TRADING_DAY (否则 9 个消费方全静默)。"""
    mod = _load()

    def _boom(market):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(mod, "_is_trading", _boom)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main()
    assert buf.getvalue().strip() == "TRADING_DAY" and rc == 0


# ══════════════════════════════════════════════════════════════════════════
# 4. 仓内 gate 函数: 此前恒 True = 死门, 现在必须随 SSOT 变动
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("rel", ["scripts/da_supply_reconcile_check.py",
                                 "scripts/premarket_reprice_shadow.py"])
def test_repo_gate_functions_follow_the_tool(rel):
    """两个 gate 用 `returncode == 0` 判定 ⇒ 修好 exit code 后它们才第一次真正生效。"""
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location("_gate_" + path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._is_trading_day() is _ssot_verdict(), (
        f"{rel} 的交易日 gate 与 SSOT 不一致 (修前恒 True = 死门)")


def test_business_heartbeat_guard_reads_returncode():
    """心跳守卫的判据是 `returncode != 0 → 静默`; 契约变更后它才会在假日真的静默。"""
    src = (ROOT / "scripts" / "business_heartbeat.py").read_text(encoding="utf-8")
    assert "trading_day_check.py" in src
    assert "if r.returncode != 0:" in src
    assert "return 0  # 守卫自身故障 → 静默" in src


# ══════════════════════════════════════════════════════════════════════════
# 5. 两条 intraday 脚本: 改按 stdout 判定 + 三份副本字节一致
# ══════════════════════════════════════════════════════════════════════════
INTRADAY = ["intraday_diagnose.py", "intraday_diagnose_late.py"]
COPIES = [ROOT / "ops" / "hermes",
          Path("/root/.hermes/scripts"),
          Path("/root/.hermes/skills/devops/ftseek-intraday-cron-diagnosis/scripts")]


@pytest.mark.parametrize("name", INTRADAY)
def test_intraday_scripts_decide_by_stdout(name):
    """按 stdout 判定比按 returncode 更严格: 子进程崩溃/超时时 fail-open 照报而非静默。"""
    src = (ROOT / "ops" / "hermes" / name).read_text(encoding="utf-8")
    assert '"NON_TRADING_DAY" in' in src, f"{name} 未改为按 stdout 判定"
    assert "returncode != 0" not in src, (
        f"{name} 仍在用 returncode 判假日 —— 该判据在崩溃时会 fail-close 静默")


@pytest.mark.parametrize("name", INTRADAY)
def test_intraday_script_copies_are_identical(name):
    """快照纪律: 上游 skill 副本 / 仓内 ops 副本 / cron 活体副本必须字节一致。

    `intraday_diagnose.py` docstring 明文「上游编辑请改 skill 副本, 本文件为 cron
    直接调用的稳定快照」—— 三份漂移过就会只修一半 (本批之前正是三份同时带着同一个死判据)。
    """
    present = [base / name for base in COPIES if (base / name).exists()]
    if len(present) < 2:
        pytest.skip(f"仅找到 {len(present)} 份副本, 无法比对")
    blobs = {p.read_bytes() for p in present}
    assert len(blobs) == 1, f"{name} 的 {len(present)} 份副本不一致: {[str(p) for p in present]}"


# ══════════════════════════════════════════════════════════════════════════
# 6. cron prompt 耦合点
# ══════════════════════════════════════════════════════════════════════════
def test_cron_prompts_still_use_the_exit1_idiom():
    """工具契约 (exit 1 = 非交易日) 与 cron prompt 的 `EXIT=1` 惯用法是一对耦合。

    钉住它, 防再次单边改动脱钩 —— 本批的病灶正是「工具改契约 / prompt 按想象写」两头对不上。
    prompt 若改走 stdout 判据, 本测试需同步更新 (那是有意的契约变更, 应当被看见)。
    """
    jobs_path = Path("/root/.hermes/cron/jobs.json")
    if not jobs_path.exists():
        pytest.skip("hermes cron 配置不可读")
    import json
    jobs = json.loads(jobs_path.read_text(encoding="utf-8"))["jobs"]
    users = [j for j in jobs if "trading_day_check.py" in json.dumps(j, ensure_ascii=False)]
    assert users, "没有任何 cron 引用 trading_day_check.py — 消费方清单已过期?"
    for j in users:
        prompt = j.get("prompt") or ""
        assert "EXIT=1" in prompt or "NON_TRADING_DAY" in prompt, (
            f"cron {j['id']}({j.get('name')}) 引用了工具但既不按 EXIT=1 也不按 stdout 判定")
