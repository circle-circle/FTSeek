"""新闻催化辩论引擎 — 盘前/盘后催化事件检测 + 多模型辩论

模块结构:
- models.py: CatalystEvent + CatalystVerdict 数据模型
- event_classifier.py: 催化事件分类器 (关键词+LLM)
- catalyst_debate.py: 3模型并行辩论引擎
- detector.py: 盘前/盘后调度入口
- reverse_catalyst.py: 行情先行→新闻确认 (反向催化)
- postmarket_review.py: 盘后催化回看 + 教训提取
- sources/: 一手信息源 (HKEX, CSRC)

集成点:
- 盘前: 注入 _build_debate_context → DebatePlanner → 作战计划
- 盘后: 注入盘后归因辩论 → lessons DB
- 异动: emergency_interceptor 回调 → 快速辩论 → 存储结论

调度: 仅盘前(8:30-9:25 ET) + 盘后(16:05-18:00 ET), 盘中不介入
"""

__version__ = "1.0.0"
