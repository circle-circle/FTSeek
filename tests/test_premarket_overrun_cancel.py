"""盘前轮次跨开盘的步骤级取消回归 (2026-09-03, DEB-20260903-intraday-chain-audit §六 D4(a))。

实况: 2026-09-03 盘前轮次 ET 09:01:09 进 `run_playbook_generate`, 到 09:31:38 仍未完成 →
开盘后 90 秒内 TRADING 分支的 `run_playbook_auto_execute`(入场触发/re-price/下单) **一次都没跑**;
当天是 watchdog 误重启意外打断盘前轮次才恢复了入场 (09:32:14 恢复 → 09:38 起逐笔成交)。

修法: 盘前分支每个非必需步骤前查阶段, 已开盘即 `continue` 回轮顶 → 重新取 phase 进 TRADING
分支。本文件钉住三条边界:
  1. 判据本身: 未开盘 False / 已开盘 True + 告警; 阶段探测异常 → False (保守, 不误跳步骤);
  2. 覆盖面: 四个非必需步骤都被守卫且都以 continue 收尾, **generate (供给) 不被守卫**;
  3. "跳过零损失"的机器判据: 被守卫的 playbook 步骤在 TRADING 分支都有同名 catch-up 调用
     (否则跳过=丢功能/丢记录, 那才是真的行为变更)。

回滚: 删本文件即可; 生产侧回滚 = 去掉四处 `if self._premarket_overrun(...): continue`。
"""
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from app.core.scheduler_core import TradingScheduler  # noqa: E402
from app.core.scheduler_triggers import MarketPhase  # noqa: E402

_SRC = (PROJECT_ROOT / "app" / "core" / "scheduler_core.py").read_text(encoding="utf-8")

# 盘前分支里被守卫的步骤 → 其函数名 (用于 catch-up 契约校验)
_GUARDED = {
    "topup_supplement": "run_playbook_topup_supplement",
    "normalize_vol_buckets": "run_playbook_normalize_vol_buckets",
    "morning_push": "run_playbook_morning_push",
    "macro_calendar_shadow": None,  # record-only 影子, 自带 06:00-09:29 ET 窗口门, 无 catch-up
}


def _branch(name):
    """取出 `_llm_strategy_loop` 里某个 phase 分支的源码 (到下一个 elif 为止)。"""
    loop = _SRC.split("def _llm_strategy_loop", 1)[1].split("\n    def ", 1)[0]
    seg = loop.split(f"phase == MarketPhase.{name}:", 1)[1]
    return re.split(r"\n\s*elif phase == MarketPhase\.", seg, 1)[0]


@pytest.fixture
def sched():
    obj = TradingScheduler.__new__(TradingScheduler)
    obj.running = True
    return obj


class TestOverrunPredicate:
    def _set_phase(self, monkeypatch, phase):
        import app.core.scheduler_triggers as st
        monkeypatch.setattr(st, "get_market_phase", lambda: phase)

    def test_false_before_open(self, sched, monkeypatch):
        """未开盘 (盘前) → 不跳步骤, 也不告警。"""
        self._set_phase(monkeypatch, MarketPhase.PRE_MARKET)
        sent = []
        import app.utils.critical_alerts as ca
        monkeypatch.setattr(ca, "send_critical", lambda m, async_mode=False: sent.append(m))
        assert sched._premarket_overrun("normalize_vol_buckets") is False
        assert sent == []

    def test_true_after_open_and_alerts(self, sched, monkeypatch):
        """已开盘 → True + 一条可操作的告警 (入场被阻塞必须可见)。"""
        self._set_phase(monkeypatch, MarketPhase.TRADING)
        sent = []
        import app.utils.critical_alerts as ca
        monkeypatch.setattr(ca, "send_critical", lambda m, async_mode=False: sent.append(m))
        assert sched._premarket_overrun("normalize_vol_buckets") is True
        assert len(sent) == 1
        assert "normalize_vol_buckets" in sent[0] and "入场" in sent[0]

    def test_phase_probe_failure_is_conservative(self, sched, monkeypatch):
        """阶段探测抛异常 → False (维持原步骤顺序, 不因守卫故障跳步骤)。"""
        import app.core.scheduler_triggers as st

        def _boom():
            raise RuntimeError("holiday calendar unavailable")

        monkeypatch.setattr(st, "get_market_phase", _boom)
        assert sched._premarket_overrun("morning_push") is False

    def test_never_raises(self, sched, monkeypatch):
        """推送通道故障也不能把守卫变成新的故障点。"""
        self._set_phase(monkeypatch, MarketPhase.TRADING)
        import app.utils.critical_alerts as ca

        def _boom(*a, **k):
            raise RuntimeError("dingtalk down")

        monkeypatch.setattr(ca, "send_critical", _boom)
        assert sched._premarket_overrun("macro_calendar_shadow") is True


class TestGuardCoverage:
    def test_four_non_essential_steps_guarded_with_continue(self):
        """盘前分支四处守卫, 每处都以 continue 收尾 (回轮顶重新取阶段)。"""
        pre = _branch("PRE_MARKET")
        for step in _GUARDED:
            m = re.search(
                rf'if self\._premarket_overrun\("{step}"\):\s*\n\s*continue', pre)
            assert m, f"盘前分支缺少 {step} 的步骤级取消守卫"

    def test_generate_is_not_guarded(self):
        """generate 是入场的供给前提 → 不跳 (跳了只是换到 TRADING 分支跑同一件事)。"""
        pre = _branch("PRE_MARKET")
        gen_idx = pre.index("run_playbook_generate(self)")
        first_guard = pre.index("_premarket_overrun(")
        assert gen_idx < first_guard, "generate 之前不应有跨界守卫"
        assert not re.search(r'_premarket_overrun\("generate', pre)

    def test_trading_branch_has_catchup_for_every_skipped_playbook_step(self):
        """零损失契约: 被跳过的 playbook 步骤在 TRADING 分支都有同名 catch-up 调用。"""
        trading = _branch("TRADING")
        for step, fn in _GUARDED.items():
            if fn is None:
                continue
            assert f"{fn}(self)" in trading, (
                f"{step} 被盘前跨界跳过后, TRADING 分支缺 catch-up 调用 {fn} → 会丢功能/丢记录")

    def test_trading_branch_runs_auto_execute(self):
        """跳过盘前步骤的**目的**: TRADING 分支里有入场执行 (否则取消毫无意义)。

        2026-11-03 (执行优先批): 内联调用被抽成 `_maybe_auto_execute(now)`
        (interval 门 + 异常隔离), 直接调用字面量随之消失 ⇒ 断言改为"分支内存在入场执行
        调用"(两种形态都算)。**意图不变且不削弱**: 包装形态下额外确认包装器里真的调了
        `run_playbook_auto_execute` (防"空壳包装"变成静默不执行)。
        """
        trading = _branch("TRADING")
        direct = "run_playbook_auto_execute(self)" in trading
        wrapped = "_maybe_auto_execute(" in trading
        assert direct or wrapped, (
            "TRADING 分支必须含入场执行调用 (直接调用或 _maybe_auto_execute 包装) "
            "—— 否则盘前取消守卫跳过步骤后, 入场就没人做了")
        if wrapped and not direct:
            assert "run_playbook_auto_execute(self)" in _SRC, (
                "_maybe_auto_execute 包装器内必须真的调用 run_playbook_auto_execute "
                "(空壳包装 = 静默不执行)")


def test_after_hours_steps_are_isolated():
    """AFTER_HOURS 分支的每一步都须自带 try/except。

    2026-11-03 (全链审计 P1-2): 该分支末尾含 **EOD 持仓平仓** (run_playbook_track)。
    此前 `_process_deferred_audit_queue()` 与 `run_after_hours_llm_scan()` **无调用点 try**
    ⇒ 任一异常直接落到最外层 except ⇒ **本分支剩余步骤整段跳过 (含 EOD 平仓)**
    ⇒ 持仓静默过夜、无 SL。隔离开后单步失败只丢该步, 不带走后面的平仓。

    本用例钉住"每一步自带 try/except"这条不变量 (源码级, 与本文件既有风格一致)。
    """
    ah = _branch("AFTER_HOURS")
    lines = ah.splitlines()
    for step in ("_process_deferred_audit_queue(", "run_after_hours_llm_scan("):
        idxs = [i for i, ln in enumerate(lines) if step in ln]
        assert idxs, f"AFTER_HOURS 分支找不到 {step}"
        i = idxs[0]
        before = lines[max(0, i - 2):i]
        after = lines[i + 1:i + 3]
        assert any("try:" in ln for ln in before), (
            f"{step} 之前没有 try: ⇒ 异常会跳到最外层, 连带跳过 EOD 平仓")
        assert any("except" in ln for ln in after), (
            f"{step} 之后没有 except ⇒ 异常会跳到最外层, 连带跳过 EOD 平仓")
