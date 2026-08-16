"""scripts/long_expectancy_ledger.py 契约测试 — L-E1 主实验 (DEB-20260816-long-expectancy §四)

锁定预注册判据的可计算性 (改判据参数=改实验, 必须过 DEB):
1. 统计数学: t / 95%CI 上界 (样本标准差)
2. 判据1: 供给辩论触发 = n≥300 **且** CI上界<0 (两条件缺一不可)
3. 判据2: 正桶保护 = n≥60 且 t≥1.5
4. 单位: mfe_pct/mae_pct 列为百分数, cell 输出为分数
5. 红线⑤: 多空同台账
"""
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.long_expectancy_ledger import (  # noqa: E402
    CI_Z, N_TRIGGER, PROTECT_N, PROTECT_T, _cell, _stats, build_ledger,
)


def _tr(direction="LONG", net=-10.0, gross=-10.0, exit_reason="TIME_EXIT",
        conf=0.5, session="regular", regime="RANGE", month="2026-08", n=1):
    return [{"trade_date": f"{month}-15", "direction": direction, "exit_reason": exit_reason,
             "entry_session": session, "market_regime": regime, "signal_confidence": conf,
             "gross": gross, "net": net, "mfe_pct": 100.0, "mae_pct": 100.0} for _ in range(n)]


class TestStatsMath:
    def test_t_and_ci_known_values(self):
        s = _stats([1.0, 2.0, 3.0])
        assert s["n"] == 3 and s["mean"] == 2.0
        assert abs(s["t"] - 2.0 / math.sqrt(1.0 / 3.0)) < 0.01  # t = mean/se, var=1
        assert abs(s["ci_upper"] - (2.0 + CI_Z * math.sqrt(1.0 / 3.0))) < 0.01

    def test_zero_variance_and_small_n(self):
        assert _stats([5.0])["t"] is None
        assert _stats([5.0, 5.0])["t"] is None  # se=0 → None, 不除零

    def test_preregistered_constants_frozen(self):
        """判据参数 = 预注册内容, 变更须过 DEB 辩论"""
        assert (N_TRIGGER, CI_Z, PROTECT_N, PROTECT_T) == (300, 1.96, 60, 1.5)


class TestSupplyDebateTrigger:
    def test_both_conditions_required(self):
        # 300 笔稳定小亏 (se 小 → CI 上界 < 0) → 触发
        yes = build_ledger(_tr(net=-10.0, gross=-10.0, n=N_TRIGGER))
        assert yes["supply_debate_trigger"]["triggered"] is True
        # 299 笔同款 → n 未达 → 不触发
        no_n = build_ledger(_tr(net=-10.0, gross=-10.0, n=N_TRIGGER - 1))
        assert no_n["supply_debate_trigger"]["triggered"] is False
        # 300 笔高波动 (mean -10 但 std≈90 → CI 上界 > 0) → 不触发
        wide = [80.0 if i % 2 else -100.0 for i in range(N_TRIGGER)]
        rows = _tr(n=0) + [{"trade_date": "2026-08-15", "direction": "LONG",
                            "exit_reason": "TIME_EXIT", "entry_session": "regular",
                            "market_regime": "RANGE", "signal_confidence": 0.5,
                            "gross": v, "net": v, "mfe_pct": 0, "mae_pct": 0} for v in wide]
        no_ci = build_ledger(rows)
        assert no_ci["supply_debate_trigger"]["triggered"] is False

    def test_short_never_triggers_supply_debate(self):
        """判据1 只判 LONG — SHORT 亏损再多也不在此路径触发 (另案辩论)"""
        only_short = build_ledger(_tr(direction="SHORT", net=-10.0, gross=-10.0, n=400))
        assert only_short["supply_debate_trigger"]["triggered"] is False


class TestBucketProtection:
    def _rows(self, nets, reason="SL_TRAIL_LOCK"):
        return [{"trade_date": "2026-08-15", "direction": "LONG", "exit_reason": reason,
                 "entry_session": "regular", "market_regime": "RANGE", "signal_confidence": 0.5,
                 "gross": v + 1.0, "net": v, "mfe_pct": 0, "mae_pct": 0} for v in nets]

    def test_protected_bucket_flagged(self):
        # 60 笔均盈 (4/5/6 循环, t≈38) → 保护
        led = build_ledger(self._rows([4.0, 5.0, 6.0] * 20))
        prot = [p for p in led["protected_buckets"] if p["bucket"] == "SL_TRAIL_LOCK"]
        assert prot and prot[0]["n"] == PROTECT_N and prot[0]["t"] >= PROTECT_T

    def test_below_n_or_negative_t_not_protected(self):
        # n=59 → 不保护 (当前 0.5 conf 桶 n=55 正在此线之下)
        assert build_ledger(self._rows([4.0, 5.0, 6.0] * 19))["protected_buckets"] == []
        # 亏损桶 t 为负 → 永不保护
        assert build_ledger(self._rows([-4.0, -5.0, -6.0] * 30))["protected_buckets"] == []


class TestUnitsAndSymmetry:
    def test_mfe_mae_percent_to_fraction(self):
        cell = _cell(_tr(net=-5.0, gross=-5.0, n=2))
        assert cell["avg_mfe_pct"] == 1.0  # 列值 100 (百分数) → 分数 1.0
        assert cell["avg_mae_pct"] == 1.0

    def test_both_directions_in_ledger(self):
        led = build_ledger(_tr(net=-1.0, gross=-1.0, n=3) +
                           _tr(direction="SHORT", net=1.0, gross=1.0, n=3))
        assert set(led["overall"].keys()) == {"LONG", "SHORT"}
