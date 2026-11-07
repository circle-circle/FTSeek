"""指标口径助手 —— 让"看起来变好"与"真的变好"在报告层可区分。

为什么需要 (DEB-20261106 §28/§29):
`mfe_pct` 是**持仓期间峰值**，而 BE/锁利类出场（SL_BREAKEVEN / SL_TRAIL_LOCK / SL_CERT_LOCK /
TAKE_PROFIT / EXIT_MONITOR_CLOSE）必须价格先走到武装线才可能发生 ⇒ 这些笔**定义上不可能** MFE≈0。
出场链一改（be 参数、限价保本、提前退出），"MFE=0 占比 / 死单率"会**自动变好**，与信号质量无关。
实测：DA 链 BE 出场占比 55% → 80%，raw 死单率 28.5% → 14.8%，而同构成校正后 20.3% → 23.2%（变差）。

因此报告层必须:
  1. 并列 BE 出场占比（分母膨胀的可见化）；
  2. 死单率只在**非 BE 人群**内计算（可比口径）；
  3. 需要跨期比较时用 `adjusted_dead_rate`（把非 BE 死单率套到参考期 BE 占比上）。

本模块纯计算、无 IO、无副作用；报告只调用，不改变任何交易逻辑。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

# 与 repair/统计脚本保持同一族定义 (metric_drift_audit.BE_REASONS)
BE_EXIT_REASONS = frozenset({
    "SL_BREAKEVEN",
    "SL_TRAIL_LOCK",
    "SL_CERT_LOCK",
    "TAKE_PROFIT",
    "EXIT_MONITOR_CLOSE",
})


def is_be_exit(exit_reason: Optional[str]) -> bool:
    return (exit_reason or "") in BE_EXIT_REASONS


def be_share(rows: Iterable[Dict[str, Any]]) -> Optional[float]:
    """BE/锁利类出场占比 (0-1)。空样本返回 None（不要用 0 冒充"没有 BE"）。"""
    rows = list(rows)
    if not rows:
        return None
    return sum(1 for r in rows if is_be_exit(r.get("exit_reason"))) / len(rows)


def dead_rate_caliber(rows: Iterable[Dict[str, Any]], threshold: float = 0.3) -> Dict[str, Any]:
    """死单率三口径 + 人群拆分。

    返回:
      n / be_n / be_share / nonbe_n / raw_dead (全样本) / nonbe_dead (非 BE 内) /
      mfe_missing (mfe_pct 缺失笔数, 不计入分子但计入分母)
    """
    rows = list(rows)
    if not rows:
        return {"n": 0, "be_n": 0, "be_share": None, "nonbe_n": 0,
                "raw_dead": None, "nonbe_dead": None, "mfe_missing": 0}
    be = [r for r in rows if is_be_exit(r.get("exit_reason"))]
    nonbe = [r for r in rows if not is_be_exit(r.get("exit_reason"))]

    def _dead(xs: list) -> tuple[int, int]:
        miss = sum(1 for r in xs if r.get("mfe_pct") is None)
        dead = sum(1 for r in xs if r.get("mfe_pct") is not None and float(r["mfe_pct"]) < threshold)
        return dead, miss

    dead_all, miss_all = _dead(rows)
    dead_nonbe, _ = _dead(nonbe)
    return {
        "n": len(rows),
        "be_n": len(be),
        "be_share": len(be) / len(rows),
        "nonbe_n": len(nonbe),
        "raw_dead": dead_all / len(rows),
        "nonbe_dead": (dead_nonbe / len(nonbe)) if nonbe else None,
        "mfe_missing": miss_all,
    }


def adjusted_dead_rate(nonbe_dead: Optional[float], ref_be_share: Optional[float]) -> Optional[float]:
    """把"非 BE 死单率"套到参考期 BE 占比上 —— 跨期比较用的同构成口径。"""
    if nonbe_dead is None or ref_be_share is None:
        return None
    if not 0.0 <= ref_be_share <= 1.0:
        return None
    return (1.0 - ref_be_share) * nonbe_dead


def caliber_note(nonbe_dead: Optional[float], nonbe_n: int, ref_be_share: Optional[float] = None) -> str:
    """一行口径脚注（报告用）。缺失时明确说"不可比"，不要静默省略。"""
    if nonbe_dead is None or nonbe_n == 0:
        return "死单口径: 非BE人群无样本，不可比"
    txt = f"死单口径: 只在非BE出场人群内计 = {nonbe_dead * 100:.0f}% (n={nonbe_n})"
    adj = adjusted_dead_rate(nonbe_dead, ref_be_share)
    if adj is not None:
        txt += f"；同构成校正(参考BE {ref_be_share * 100:.0f}%) = {adj * 100:.0f}%"
    txt += "；跨期比较禁用全样本口径"
    return txt