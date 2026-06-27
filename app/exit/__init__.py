"""
退出模块 — 仅保留 option_quick_exit (期权快速退出)。

其余可插拔退出框架已于 2026-06-25 审计后删除 (仅被已删除的 harness/ 引用, 1786行死代码)。
生产退出逻辑由 app/risk/exit_rule_dispatcher.py + app/risk/exit_rules/ 管理。
"""
from app.exit.option_quick_exit import get_quick_exit_engine
