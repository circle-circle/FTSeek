"""月报净口径 + 伪止损执行率拆除 (2026-11-01).

钉住两条已实证的病灶, 防回归:

  A. 月报只能报毛额 ⇒ 「总盈亏 $+1097.53」读起来像到手 1097.53。
     实测 2026-10: 毛 +1097.53 / 费 794.64 / **净 +302.89** (费吃毛利 72%), 净胜率 79.2% vs 毛 85.9%,
     假盈利 12 笔。收益段必须给「毛 | 费 | 净」三列 + 平均每笔双列, 口径与周报
     weekly_review._generate_summary (DEB-20260730 D1) 同源: net = COALESCE(pnl_net, realized_pnl),
     分母只算已平仓子集 (未平仓 realized_pnl/pnl_net 皆 NULL)。

  B. 「止损执行率」是伪影, 不是测量值:
     trade_records 无 stop_loss_planned / stop_loss_executed 列 (PRAGMA 52 列实测),
     旧实现 `t.get('stop_loss_planned')` 恒空 ⇒ sl_trades=[] ⇒ 恒落默认 1.0 ⇒ 月报恒印 100.0%。
     2026-11-01 拆除 (同根因的死判据 stop_loss_violation 一并拆除); 不得以任何默认值复活。

处置范式: 不可测的指标就删掉, 不留下一个恒定的「看起来没问题」的数字 (daily_review 2026 年同因处置)。
"""
import inspect
import re
import sqlite3

import pytest

from app.core.scheduler_triggers import _format_monthly_review
from app.review import monthly_review as mr_mod
from app.review.monthly_review import MonthlyReviewGenerator


def _gen():
    """纯计算方法的被测对象: 绕过 __init__ 的 DB 初始化 (conftest 禁止测试写生产库)。

    被测的 _calculate_performance / _calculate_risk_metrics / _identify_error_patterns
    只读入参, 不读 self 状态, 故 __new__ 构造等价且不触发 get_trade_recorder() 建表。
    """
    return MonthlyReviewGenerator.__new__(MonthlyReviewGenerator)

PROD_DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"


def _t(pnl, net, fee, **kw):
    row = {'realized_pnl': pnl, 'pnl_net': net, 'fees_est': fee, 'pnl_pct': 0.01}
    row.update(kw)
    return row


# 生产形态样本: 2 毛赚 (其中 1 笔净亏 = 假盈利) + 1 毛亏 + 1 未平仓 (全 NULL)
TRADES = [
    _t(100.0, 88.0, 12.0),
    _t(10.0, -2.0, 12.0),
    _t(-30.0, -42.0, 12.0),
    _t(None, None, None),          # 未平仓: 不得进净口径分母
]


class TestNetBasisComputed:
    """A. 净口径字段必须真的算出来, 且分母只数已平仓。"""

    def test_net_fields(self):
        perf = _gen()._calculate_performance(TRADES)
        assert perf['total_trades'] == 4
        assert perf['closed_trades'] == 3, "未平仓不得计入已平仓分母"
        assert perf['total_pnl'] == pytest.approx(80.0)      # 毛: 100+10-30
        assert perf['total_pnl_net'] == pytest.approx(44.0)  # 净: 88-2-42
        assert perf['fees_total'] == pytest.approx(36.0)
        assert perf['fake_wins'] == 1, "毛赚净亏的笔数必须单独计数"
        assert perf['win_rate'] == pytest.approx(0.5)        # 毛口径分母 = 全部 4 笔 (含未平)
        assert perf['win_rate_net'] == pytest.approx(1 / 3, abs=1e-4)  # 净口径分母 = 已平仓 3 笔 (入库保留 4 位)
        assert 'net=SUM(COALESCE(pnl_net,realized_pnl))' in perf['pnl_basis']

    def test_missing_pnl_net_falls_back_to_gross(self):
        """旧行无 pnl_net 时回退毛额, 不得当 0 丢掉。"""
        rows = [_t(50.0, None, 5.0), _t(-20.0, None, 5.0)]
        perf = _gen()._calculate_performance(rows)
        assert perf['total_pnl_net'] == pytest.approx(30.0)

    def test_empty_trades_has_net_keys(self):
        """空集也必须有净字段 (格式化层/消费者不 KeyError)。"""
        perf = _gen()._calculate_performance([])
        for k in ('closed_trades', 'total_pnl_net', 'fees_total',
                  'win_rate_net', 'winning_trades_net', 'fake_wins'):
            assert k in perf


class TestFormatter:
    """A/B. 渲染层: 三列必须出现, 伪止损执行率必须不出现。"""

    def _report(self, **perf_over):
        perf = {'total_trades': 4, 'closed_trades': 3, 'total_pnl': 80.0,
                'total_pnl_net': 44.0, 'fees_total': 36.0, 'win_rate': 0.5,
                'win_rate_net': 1 / 3, 'fake_wins': 1, 'sharpe_ratio': 0.35,
                'profit_factor': 1.41}
        perf.update(perf_over)
        return {'year': 2026, 'month': 10, 'performance': perf,
                'risk_metrics': {'max_single_loss': -353.34, 'max_drawdown': 621.09},
                'voting_analysis': {'avg_signal_strength': 47.18,
                                    'avg_signal_confidence': 0.472},
                'next_month_plan': []}

    def test_three_columns_rendered(self):
        text = _format_monthly_review(self._report())
        assert '毛 $+80.00 | 费 $36.00 | 净 $+44.00' in text
        assert '假盈利 1 笔' in text
        assert '平均每笔' in text and '净 $+14.67' in text
        assert '已平仓 3' in text

    def test_header_follows_net_not_gross(self):
        """毛赚净亏 ⇒ 头部必须是 🔴 —— 这正是税前视觉「假盈利」要防的。"""
        text = _format_monthly_review(self._report(total_pnl=80.0, total_pnl_net=-5.0))
        assert text.splitlines()[4].startswith('🔴'), text
        assert '净 $-5.00' in text

    def test_old_json_falls_back_to_gross_without_faking_net(self):
        """净口径上线前落盘的旧 JSON: 只报毛额 + 明确标注, 不得编造净额。"""
        report = self._report()
        for k in ('total_pnl_net', 'fees_total', 'win_rate_net', 'fake_wins', 'closed_trades'):
            report['performance'].pop(k, None)
        text = _format_monthly_review(report)
        assert '净 $' not in text
        assert '毛口径' in text
        assert '毛 $+1' not in text.replace('毛口径', '')

    def test_stop_loss_execution_rate_not_rendered(self):
        text = _format_monthly_review(
            self._report(), )
        assert '止损执行率' not in text, (
            "止损执行率是默认值伪影 (trade_records 无对应列), 渲染它等于宣布一个从未测量过的 100%")


class TestPhantomMetricRemoved:
    """B. 源码契约: 伪列不得被重新引用。"""

    def test_risk_metrics_has_no_stop_loss_rate(self):
        risk = _gen()._calculate_risk_metrics(TRADES)
        assert 'stop_loss_execution_rate' not in risk
        assert set(risk) == {'max_single_loss', 'max_drawdown', 'avg_holding_hours'}

    @pytest.mark.parametrize('col', ['stop_loss_planned', 'stop_loss_executed'])
    def test_module_does_not_read_phantom_columns(self, col):
        """只看取值表达式 (注释里的拆除说明不算引用)。"""
        src = inspect.getsource(mr_mod)
        reads = re.findall(r"\.get\(['\"]%s['\"]" % col, src)
        assert not reads, (
            f"monthly_review 又读取幽灵列 {col} (trade_records 无此列 ⇒ 判据恒空/恒默认); "
            "真实接线前不得重新判读它")

    def test_stop_loss_violation_pattern_dead_and_gone(self):
        patterns = _gen()._identify_error_patterns(TRADES)
        assert 'stop_loss_violation' not in [p['pattern'] for p in patterns]


class TestPrerequisiteStillHolds:
    """前提实证: 生产库确实没有这两列 (否则拆除就是误删, 前提变了需重评)。"""

    @pytest.mark.parametrize('col', ['stop_loss_planned', 'stop_loss_executed'])
    def test_column_absent_in_production(self, col):
        con = sqlite3.connect(PROD_DB)
        cols = [r[1] for r in con.execute("PRAGMA table_info(trade_records)")]
        con.close()
        assert len(cols) > 40, f"trade_records 列数异常 ({len(cols)}), 前提未实证"
        assert col not in cols, (
            f"{col} 已出现在生产库 ⇒ 拆除伪止损执行率的前提变了, 需重评: "
            "此时应改为真实接线读取该列, 而不是保持删除")

    def test_pnl_net_and_fees_are_filled_in_production(self):
        """净口径前提: 已平仓行必须有 pnl_net/fees_est (否则三列会大面积回退毛额)。"""
        con = sqlite3.connect(PROD_DB)
        row = con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN pnl_net IS NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN fees_est IS NULL THEN 1 ELSE 0 END) "
            "FROM trade_records WHERE realized_pnl IS NOT NULL "
            "AND trade_date >= '2026-09-01'").fetchone()
        con.close()
        total, null_net, null_fee = row
        assert total and total > 100, f"已平仓样本不足 ({total}), 前提未实证"
        assert (null_net or 0) == 0, f"{null_net} 笔已平仓缺 pnl_net ⇒ 净口径失真"
        assert (null_fee or 0) == 0, f"{null_fee} 笔已平仓缺 fees_est ⇒ 费用列失真"