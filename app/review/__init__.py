"""
深度复盘分析模块 (美股版)

支持决策/时机/持仓/风控/心理/策略 6 大维度复盘
适配美股多 Agent 架构

2026-11-01 退役批 (记录于 commit + results/explore_20260905/ssot_findings.md):
  删除 SuggestionEngine / StrategyAnalyzer / PsychologyAnalyzer / ImprovementGenerator。
  判据 (实测): ①全仓零生产调用方 (唯一入口是本文件 re-export) ②数据源均为 0 行遗留库
  data/trade_records.db ③最后实质改动 2026-05-20 ④无 cron / 无动态导入 / 无台账登记
  ⑤能力已被替代: daily_review._generate_suggestions_v2 / weekly_review._analyze_strategies。
  注意: 任何 `from app.review.X import Y` 都会执行本文件 ⇒ 删模块必须同批删掉这里的导入与 __all__ 项。
"""
from .daily_review import DailyReviewGenerator
from .weekly_review import WeeklyReviewGenerator
from .error_pattern_detector import ErrorPatternDetector

__all__ = [
    'DailyReviewGenerator',
    'WeeklyReviewGenerator',
    'ErrorPatternDetector',
]