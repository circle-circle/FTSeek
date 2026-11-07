"""指标漂移体检的承重测试。

守三件:
1. `adjusted_dead_rate` 的标准化语义 —— 死单只可能出现在非 BE 人群, 所以把当前非 BE 死单率
   套到参考期 BE 占比上; 若这里的数学写反, "同构成比较"会给出反向结论 (假改善/假恶化);
2. `verdict` 必须区分 DRIFT / REAL —— 单位混用 (0-1 比率 vs 百分点) 会让 fwd 永远判"变好",
   把 DRIFT 误判成 MIXED (本脚本第一版就踩过);
3. `is_be` 的出场族定义必须与生产 BE 语义一致 —— 漏一个族会把 BE 笔算进非 BE 分母。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from metric_drift_audit import adjusted_dead_rate, is_be, verdict  # noqa: E402


def test_is_be_covers_production_be_families():
    for reason in ("SL_BREAKEVEN", "SL_TRAIL_LOCK", "SL_CERT_LOCK", "TAKE_PROFIT", "EXIT_MONITOR_CLOSE"):
        assert is_be(reason), reason
    for reason in ("SL_INITIAL", "TIME_EXIT", "OVERNIGHT_GAP_STOP", "DEAD_SIGNAL_EXIT", None, ""):
        assert not is_be(reason), reason


def test_adjusted_dead_rate_applies_nonbe_rate_to_reference_mix():
    # 参考期 BE 占比 50% ⇒ 非 BE 占 50%; 非 BE 死单率 60% ⇒ 调整后 30%
    assert adjusted_dead_rate(be_share=0.8, nonbe_dead=0.6, ref_be_share=0.5) == 0.3
    # 参考构成全 BE ⇒ 没人可能成死单
    assert adjusted_dead_rate(be_share=0.8, nonbe_dead=0.6, ref_be_share=1.0) == 0.0
    assert adjusted_dead_rate(be_share=0.8, nonbe_dead=None, ref_be_share=0.5) is None
    assert adjusted_dead_rate(be_share=0.8, nonbe_dead=0.6, ref_be_share=1.5) is None


def test_verdict_flags_drift_when_only_raw_improves():
    # raw 改善 -13.7pp, 同构成恶化 +2.9pp, fwd 基本不变(4bps, 低于 5bps 门)
    assert verdict(-0.137, +0.029, +0.041) == "DRIFT"


def test_verdict_requires_all_three_for_real():
    assert verdict(-0.10, -0.05, +0.20) == "REAL"      # raw↓ adj↓ fwd↑
    assert verdict(-0.10, -0.05, +0.02) == "MIXED"     # fwd 改善不足门
    assert verdict(+0.05, +0.05, -0.20) == "NO_CHANGE"


def test_verdict_does_not_treat_pp_as_fraction():
    # 0.20pp 的 fwd 改善不该通过 5bps 门(0.05pp)以外的判断逻辑 —— 单位一致性回归
    assert verdict(-0.10, +0.05, +0.20) == "MIXED"