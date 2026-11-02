"""「止损执行率」伪指标全线拆除契约钉 (2026-11-01).

同一根因在 5 处复现, 全部源自"判据读一个做不到/没填充的列 + 默认值兜底":

| # | 位置 | 读的列 | 旧读数 |
|---|------|--------|--------|
| 1 | app/review/monthly_review.py `_calculate_risk_metrics` | trade_records.stop_loss_planned/executed | 恒 **100%** (默认 1.0) |
| 2 | app/review/monthly_review.py `_identify_error_patterns` | 同上 | 恒 **0 个违规** (判据恒空) |
| 3 | app/review/weekly_review.py `_analyze_risk` + `_generate_plan` | 同上 (活跃库) | 恒 **100%**, 建议永不出 |
| 4 | app/review/suggestion_engine.py RULES + `_calculate_stats` | 同上 | 恒 **100%**, critical 建议永不触发 → **该模块 2026-11-01 整体退役** |
| 5 | app/storage/trade_recorder.py / review_tables.py `get_statistics` | 缺列 / 零填充列 | 恒 **0%** (反向假数) |

根源 (退役前): `suggestion_engine._ensure_stop_loss_columns()` 的 ALTER 打在**遗留库**
`data/trade_records.db` (0 行), 活跃库 `ftseek_us.db` 永远没有这两列。

纪律: 指标不可测就删掉, 不留一个恒定的"看起来没问题"的数字; 想恢复必须先在写入侧真实落库。
本文件只做**契约**(源码不得再读/再渲染), 前提实证见 test_monthly_net_basis.py。
"""
import inspect

import pytest

from app.review import monthly_review as mr_mod
from app.review import weekly_review as wr_mod
from app.review.weekly_review import WeeklyReviewGenerator
from app.storage import review_tables as rt_mod
from app.storage import trade_recorder as tr_mod

PHANTOM_COLS = ('stop_loss_planned', 'stop_loss_executed')


def _code_only(src: str) -> str:
    """去掉整行注释 (拆除说明里会复述字段名, 不算引用)。"""
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


def _gen(cls):
    """绕过 __init__ 的 DB 初始化 (conftest 禁止测试写生产库); 被测方法只读入参。"""
    return cls.__new__(cls)


class TestWeeklyRiskNoPhantomRate:
    def test_analyze_risk_drops_rate(self):
        risk = _gen(WeeklyReviewGenerator)._analyze_risk([{'max_drawdown': 0.02}])
        assert 'stop_loss_execution_rate' not in risk
        assert 'max_drawdown' in risk

    def test_generate_plan_has_no_dead_stop_loss_branch(self):
        src = _code_only(inspect.getsource(WeeklyReviewGenerator._generate_plan))
        assert 'stop_loss_execution_rate' not in src, (
            "`rate < 0.8` 分支读恒 1.0 的伪指标 ⇒ 永不触发, 属静默死亡的守卫")


class TestSuggestionEngineRetired:
    """2026-11-01 退役批: 该模块零调用方 + 数据源 0 行遗留库 ⇒ 整体删除, 不得复活。

    判据详见 app/review/__init__.py 的退役说明。若将来要恢复"建议引擎"能力,
    必须走新模块 + 真实数据源 (活跃库) + 有调用方, 不得把旧模块从 git 里 `checkout` 回来
    (它读的 data/trade_records.db 仍是 0 行)。
    """

    MODULES = ('suggestion_engine', 'strategy_analysis',
               'psychology_analyzer', 'improvement_generator')

    @pytest.mark.parametrize('name', MODULES)
    def test_module_deleted(self, name):
        import importlib
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module('app.review.%s' % name)

    def test_package_still_imports_cleanly(self):
        """删文件必须同批删 __init__ 导出, 否则任何 app.review.* 导入都会连带崩。"""
        import app.review as rv
        assert rv.__all__ == ['DailyReviewGenerator', 'WeeklyReviewGenerator',
                              'ErrorPatternDetector']

    @pytest.mark.parametrize('cls_name', ('SuggestionEngine', 'StrategyAnalyzer',
                                          'PsychologyAnalyzer', 'ImprovementGenerator'))
    def test_no_export(self, cls_name):
        import app.review as rv
        assert not hasattr(rv, cls_name), f"{cls_name} 已退役, 不得再被导出"

    def test_live_sibling_kept(self):
        """反向: 活的 error_pattern_detector 不能被连带删掉 (daily_review 有 3 处真实调用)。"""
        from app.review.error_pattern_detector import detect_error_patterns
        assert callable(detect_error_patterns)


class TestStorageStatsNoPhantomRate:
    @pytest.mark.parametrize('mod,cls_name,fn', [
        (tr_mod, 'TradeRecorder', 'get_review_statistics'),
        (rt_mod, 'ReviewDatabase', 'get_review_statistics'),
    ])
    def test_no_rate_in_stats(self, mod, cls_name, fn):
        cls = getattr(mod, cls_name)
        src = _code_only(inspect.getsource(getattr(cls, fn)))
        assert "'stop_loss_execution_rate'" not in src


class TestNoModuleReadsPhantomColumns:
    """全表扫描: 这 5 个模块都不得再用取值表达式读幽灵列 (注释里的拆除说明不算)。"""

    @pytest.mark.parametrize('mod', [mr_mod, wr_mod])
    @pytest.mark.parametrize('col', PHANTOM_COLS)
    def test_no_read(self, mod, col):
        src = _code_only(inspect.getsource(mod))
        assert '.get(%r' % col not in src
        assert '.get("%s"' % col not in src