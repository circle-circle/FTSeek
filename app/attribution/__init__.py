"""交易归因链包（app.attribution）

2026-04-26 收敛：
- 合并原 app/attribution.py 单文件与 app/attribution/ 包（两者同名曾相互遮蔽）。
- 统一由本文件显式导出全部归因写入/查询接口，避免 from .chain import * 遗漏。
- 同时保留 calculator 子模块（供 post_market_audit 等批量计算使用）。
"""
from app.attribution.chain import (
    ensure_attribution_schema,
    record_rejection,
    record_approval,
    record_entry,
    record_exit,
    update_execution_result,
    update_trade_attribution_result,
    get_attribution_report,
)

__all__ = [
    "ensure_attribution_schema",
    "record_rejection",
    "record_approval",
    "record_entry",
    "record_exit",
    "update_execution_result",
    "update_trade_attribution_result",
    "get_attribution_report",
]
