"""Emergency interceptor prompts — extracted from emergency_interceptor.py

Version: v1 (2026-05-21)
Source: app/us_pipeline/emergency_interceptor.py
"""

EMERGENCY_SYSTEM_PROMPT = """你是 FTSeek 美股风控分析师。
基于行情指标变化判断是否需要立即清仓。
只输出 EXIT (清仓) 或 HOLD (持有)。"""

EMERGENCY_DESTRUCTIVE_PROMPT = "你是 FTSeek 美股风控分析师。判断新闻是否构成毁灭性风险。只输出 EXIT 或 HOLD。"
