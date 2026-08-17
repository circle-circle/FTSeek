"""scripts/long_expectancy_ledger.py 契约测试 — L-E1 主实验 (DEB-20260816-long-expectancy §四)

锁定预注册判据的可计算性 (改判据参数=改实验, 必须过 DEB):
1. 统计数学: t / 95%CI 上界 (样本标准差)
2. 判据1 v2 (§十一A2, 触发前修订): 供给辩论触发 = n≥300 **且** 日级聚类t<-2.0
   (日级=当日净额SUM; 交易级CI仅共报诊断 — 防伪重复偏窄触发)
3. 判据2: 正桶保护 = n≥60 且 t≥1.5
4. 单位: mfe_pct/mae_pct 列为百分数, cell 输出为分数
5. 红线⑤: 多空同台账; §十一A3: policy_period 维度; B1: conf×source; B3: 毛额指标
"""
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.long_expectancy_ledger import (  # noqa: E402
    CI_Z, DAY_T_TRIGGER, N_TRIGGER, PROTECT_N, PROTECT_T, _cell, _policy_period,
    _stats, build_ledger,
)


def _tr(direction="LONG", net=-10.0, gross=-10.0, exit_reason="TIME_EXIT",
        conf=0.5, session="regular", regime="RANGE", month="2026-08", n=1):
    return [{"trade_date": f"{month}-15", "direction": direction, "exit_reason": exit_reason,
             "entry_session": session, "market_regime": regime, "signal_confidence": conf,
             "source": "deep_analysis", "entry_time": f"{month}-15T09:40:00-04:00",
             "gross": gross, "net": net, "mfe_pct": 100.0, "mae_pct": 100.0} for _ in range(n)]


def _days_rows(day_sums):
    """day_sums: [(date, 每笔net, 笔数)] — 构造跨日分布的交易行"""
    rows = []
    for date, net, cnt in day_sums:
        rows += [{"trade_date": date, "direction": "LONG", "exit_reason": "TIME_EXIT",
                  "entry_session": "regular", "market_regime": "RANGE",
                  "signal_confidence": 0.5, "source": "deep_analysis",
                  "gross": net, "net": net, "mfe_pct": 0, "mae_pct": 0} for _ in range(cnt)]
    return rows


def _clustered_days(per_day_sum, n_days=30, per_day=10):
    return [(f"2026-08-{d + 1:02d}", per_day_sum / per_day, per_day) for d in range(n_days)]


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
        """判据参数 = 预注册内容, 变更须过 DEB 辩论 (v2: §十一A2 日级聚类, 触发前修订)"""
        assert (N_TRIGGER, DAY_T_TRIGGER, CI_Z, PROTECT_N, PROTECT_T) == (300, -2.0, 1.96, 60, 1.5)


class TestSupplyDebateTrigger:
    def test_trigger_requires_n_and_day_cluster_t(self):
        # 30天×10笔, 日净额SUM -60/-80/-100/-120/-140 循环 (日级t≈-6.9) → 触发
        days = []
        pattern = [-60.0, -80.0, -100.0, -120.0, -140.0]
        for d in range(30):
            days.append((f"2026-08-{d + 1:02d}", pattern[d % 5] / 10, 10))
        led = build_ledger(_days_rows(days))
        assert led["supply_debate_trigger"]["day_cluster_t"] < DAY_T_TRIGGER
        assert led["supply_debate_trigger"]["triggered"] is True

    def test_n_below_threshold_blocks_trigger(self):
        # 同上但总笔数 299 (最后一天 9 笔) → 不触发
        days = _clustered_days(-100.0, n_days=30)
        days[-1] = (days[-1][0], days[-1][1], 9)
        led = build_ledger(_days_rows(days))
        assert led["supply_debate_trigger"]["triggered"] is False

    def test_pseudo_replication_guard(self):
        """A2 核心: 交易级 CI 上界<0 但日级聚类 t≥-2.0 → 不触发 (防伪重复偏窄)"""
        # 29天共亏 -100/天 + 1天大赚 +2000: 交易级净期望负且方差小, 日级 t≈-0.4
        days = [(f"2026-08-{d + 1:02d}", -10.0, 10) for d in range(29)]
        days.append(("2026-08-30", 200.0, 10))
        led = build_ledger(_days_rows(days))
        trig = led["supply_debate_trigger"]
        assert trig["n"] == 300
        assert trig["trade_level_ci_upper"] < 0 or trig["trade_level_t"] < 0
        assert trig["day_cluster_t"] > DAY_T_TRIGGER
        assert trig["triggered"] is False

    def test_day_t_high_variance_not_triggered(self):
        # 日净额SUM交替 -100/+60 (mean -20, t≈-1.4) → 不触发
        days = []
        for d in range(30):
            days.append((f"2026-08-{d + 1:02d}", (-100.0 if d % 2 == 0 else 60.0) / 10, 10))
        led = build_ledger(_days_rows(days))
        assert led["supply_debate_trigger"]["day_t_met"] is False

    def test_short_never_triggers_supply_debate(self):
        """判据1 只判 LONG — SHORT 亏损再多也不在此路径触发 (另案辩论)"""
        only_short = build_ledger(_tr(direction="SHORT", net=-10.0, gross=-10.0, n=400))
        assert only_short["supply_debate_trigger"]["triggered"] is False


class TestBucketProtection:
    def _rows(self, nets, reason="SL_TRAIL_LOCK"):
        return [{"trade_date": "2026-08-15", "direction": "LONG", "exit_reason": reason,
                 "entry_session": "regular", "market_regime": "RANGE", "signal_confidence": 0.5,
                 "source": "deep_analysis",
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

    def test_gross_metric_present(self):
        """B3: 桶级毛额指标 (判别读毛额, net 仅总账)"""
        cell = _cell(_tr(net=-5.0, gross=3.0, n=4))
        assert cell["avg_gross_per_trade"] == 3.0
        assert cell["win_rate"] == 1.0  # 胜率按毛额>0


class TestPolicyPeriodAndSource:
    def test_policy_period_boundaries(self):
        """A3: 三个政策断裂点 8/12 禁触窗 / 8/15 W_WEEK / 8/17 术后"""
        assert _policy_period("2026-08-11") == "P0_base"
        assert _policy_period("2026-08-12") == "P1_no_trigger_window"
        assert _policy_period("2026-08-14") == "P1_no_trigger_window"
        assert _policy_period("2026-08-15") == "P2_w_week"
        assert _policy_period("2026-08-16") == "P2_w_week"
        assert _policy_period("2026-08-17") == "P3_post_exit_fix"

    def test_policy_period_dimension_in_ledger(self):
        rows = _tr(net=-5.0, gross=-5.0, month="2026-08", n=1)
        rows[0]["trade_date"] = "2026-08-13"
        led = build_ledger(rows)
        assert "policy_period" in led["dimensions"]["LONG"]
        assert "P1_no_trigger_window" in led["dimensions"]["LONG"]["policy_period"]

    def test_conf_x_source_dimension(self):
        """B1: conf×source 交叉分桶 (反向校准的来源剥离)"""
        rows = _tr(net=-5.0, gross=-5.0, conf=0.7, n=2)
        led = build_ledger(rows)
        keys = led["dimensions"]["LONG"]["conf_x_source"]
        assert "0.7|deep_analysis" in keys

    def test_entry_hour_policy_dimension(self):
        """§十四14.4: 入场时点维度必须带政策期前缀, 禁触窗两侧不合并"""
        rows = _tr(net=-5.0, gross=-5.0, n=2)
        rows[0]["trade_date"] = "2026-08-11"
        rows[0]["entry_time"] = "2026-08-11T09:40:00-04:00"
        rows[1]["trade_date"] = "2026-08-14"
        rows[1]["entry_time"] = "2026-08-14T10:20:00-04:00"
        led = build_ledger(rows)
        keys = led["dimensions"]["LONG"]["entry_hour_policy"]
        assert "P0_base|09h" in keys and "P1_no_trigger_window|10h" in keys
        assert all(k.startswith(("P0_", "P1_", "P2_", "P3_")) for k in keys)

    def test_overall_has_day_cluster_t(self):
        led = build_ledger(_tr(net=-5.0, gross=-5.0, n=2))
        assert led["overall"]["LONG"]["day_cluster_t"]["n"] == 1  # 同日聚为一天
