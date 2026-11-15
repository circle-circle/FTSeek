"""
策略模块 — FTSeek-US

存活内容: 本包其余模块 (exit_policies / regime_adapter / regime_filter /
signal_quality_scorer / llm_strategy_engine / strategy_notifier / pure)。

2026-11-15 减法批 (R-2a, 用户令「继续」):
  `base.py` (BaseStrategy/StrategyContext, 263 行) 与 `registry.py`
  (StrategyRegistry/get_strategy_registry, 222 行) 已删除 —— 唯一消费方是
  `app/core/di_container.py` 的 `strategy_registry()` 工厂, 而它装配出的
  `scheduler.strategy_registry` 全仓零读取; 且 `app/core/strategy_registry.py`
  (配置驱动的同名不同物) 才是现役真源 (下单执行器 / 指令构建 / 信号 IC 追踪都读它)。
  同批删除 `app/decision/strategies/{base,ma_cross,price_break,rsi_rebound}.py`:
  仅被上面那份 registry 用于"发现内置策略", 零测试、零 cron/Hermes 载体、零看板读取。
  回滚: `git revert <本批 sha>` 一次性恢复 (无数据/配置依赖)。
"""