"""
FTSeek-US LLM 策略运行模块 (P2-2 委托到 app.pipeline.llm_pipeline)

职责:
- LLM 二阶推理策略生成和执行 (run_llm_strategy_scan)
- 盘后/夜盘 LLM 新闻推荐 (run_after_hours_llm_scan)
- 盘前作战计划生成 (run_premarket_planning)
- 开盘确认器 (run_open_checker)
- LLM 策略回测 (run_llm_backtest)
- 降级新闻源和告警

函数列表:
  run_llm_strategy_scan         - LLM 策略生成任务 (三级降级)
  run_llm_backtest              - LLM 策略回测
  run_after_hours_llm_scan      - 盘后/夜盘推荐
  run_premarket_planning        - 盘前作战计划生成
  run_open_checker              - 开盘确认器
  _fallback_to_finnhub_news     - Finnhub 降级新闻源
  _generate_degraded_recommendation - 规则降级推荐
  _send_rsshub_alert            - RSSHub 故障告警
"""

# P2-2: 委托到 pipeline 模块 (实现已迁移)
from app.pipeline.llm_pipeline import (
    run_llm_strategy_scan,
    run_llm_backtest,
    run_after_hours_llm_scan,
    run_premarket_planning,
    run_open_checker,
    _fallback_to_finnhub_news,
    _generate_degraded_recommendation,
    _send_rsshub_alert,
)

__all__ = [
    "run_llm_strategy_scan",
    "run_llm_backtest",
    "run_after_hours_llm_scan",
    "run_premarket_planning",
    "run_open_checker",
    "_fallback_to_finnhub_news",
    "_generate_degraded_recommendation",
    "_send_rsshub_alert",
]
