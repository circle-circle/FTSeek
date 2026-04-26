"""
FTSeek LLM Agent 桥接模块

实现与本地 Hermes Agent 的轻量级通信:
- 盘前: 发送昨日复盘 + 新闻 → 生成作战计划
- 盘后: 发送全天交易记录 → 生成归因报告 + 改进建议
- 记忆: 通过 Hermes 的 memory 系统维持交易上下文
"""

from .agent_bridge import get_ftseek_agent_bridge

__all__ = ["get_ftseek_agent_bridge"]
