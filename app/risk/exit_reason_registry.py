"""
exit_reason_registry.py - 退出原因注册表

Phase 2 (2026-05-31): 完整交易链路归因

目标:
- 标准化 exit_reason, 建立白名单, 便于复盘统计
- 提供 normalize / validate / category 三大能力
- 支持带后缀的格式 (如 "DAY_TRADE_TP@trail_suppressed")

使用:
    from app.risk.exit_reason_registry import (
        validate_exit_reason, get_exit_category, normalize_exit_reason,
        EXIT_REASON_REGISTRY, ExitCategory,
    )
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ExitCategory(Enum):
    PRICE_ACTION = "price_action"     # 价格行为触发
    TIME_BASED = "time_based"          # 时间触发
    RISK_CONTROL = "risk_control"      # 风控触发
    MANUAL = "manual"                  # 手动/异常触发


# ───────────────────────────────────────────────────────────────
# 白名单: 所有合法的 exit_reason 及其分类
# ───────────────────────────────────────────────────────────────
EXIT_REASON_REGISTRY: Dict[str, dict] = {
    # 价格行为
    "TP1_PRICE_ACTION":   {"category": ExitCategory.PRICE_ACTION, "description": "目标价止盈"},
    "TP2_SCALE_OUT":      {"category": ExitCategory.PRICE_ACTION, "description": "分批止盈"},
    "SL_ATR_STOP":        {"category": ExitCategory.PRICE_ACTION, "description": "ATR止损触发"},
    "TRAILING_EXIT":      {"category": ExitCategory.PRICE_ACTION, "description": "追踪止损触发"},
    "DAY_TRADE_TP":       {"category": ExitCategory.PRICE_ACTION, "description": "日内交易止盈"},

    # 时间触发
    "TIME_STOP_DECAY":    {"category": ExitCategory.TIME_BASED, "description": "持仓时间衰减止损"},
    "PRE_CLOSE_FORCED":   {"category": ExitCategory.TIME_BASED, "description": "收盘前强制平仓"},
    "SESSION_END":        {"category": ExitCategory.TIME_BASED, "description": "交易时段结束"},

    # 风控触发
    "RISK_CIRCUIT_BREAKER": {"category": ExitCategory.RISK_CONTROL, "description": "熔断器触发"},
    "MAX_LOSS_DAILY":       {"category": ExitCategory.RISK_CONTROL, "description": "日最大亏损"},
    "SECTOR_HALT":          {"category": ExitCategory.RISK_CONTROL, "description": "板块停牌"},
    "VIX_SPIKE_EXIT":       {"category": ExitCategory.RISK_CONTROL, "description": "VIX飙升强平"},
    "FLASH_EXIT":           {"category": ExitCategory.RISK_CONTROL, "description": "闪崩紧急退出"},

    # 手动/异常
    "MANUAL_OVERRIDE":         {"category": ExitCategory.MANUAL, "description": "手动干预平仓"},
    "POSITION_SYNC_MISMATCH":  {"category": ExitCategory.MANUAL, "description": "持仓同步异常"},
    "SYSTEM_ERROR":            {"category": ExitCategory.MANUAL, "description": "系统错误强平"},
}


# ───────────────────────────────────────────────────────────────
# 历史/非标准 reason → 标准 reason 映射
# ───────────────────────────────────────────────────────────────
LEGACY_MAPPINGS: Dict[str, str] = {
    "tp1":              "TP1_PRICE_ACTION",
    "tp2":              "TP2_SCALE_OUT",
    "tp3":              "TP2_SCALE_OUT",
    "take_profit":      "TP1_PRICE_ACTION",
    "stop_loss":        "SL_ATR_STOP",
    "sl":               "SL_ATR_STOP",
    "atr_stop":         "SL_ATR_STOP",
    "trailing":         "TRAILING_EXIT",
    "trailing_stop":    "TRAILING_EXIT",
    "trail_stop":       "TRAILING_EXIT",
    "time_stop":        "TIME_STOP_DECAY",
    "timeout":          "TIME_STOP_DECAY",
    "time_exit":        "TIME_STOP_DECAY",
    "pre_close":        "PRE_CLOSE_FORCED",
    "eod":              "PRE_CLOSE_FORCED",
    "force_close":      "PRE_CLOSE_FORCED",
    "session_end":      "SESSION_END",
    "circuit_breaker":  "RISK_CIRCUIT_BREAKER",
    "vix_spike":        "VIX_SPIKE_EXIT",
    "flash_crash":      "FLASH_EXIT",
    "flash":            "FLASH_EXIT",
    "manual":           "MANUAL_OVERRIDE",
    "manual_close":     "MANUAL_OVERRIDE",
    "sync_mismatch":    "POSITION_SYNC_MISMATCH",
    "system_error":     "SYSTEM_ERROR",
    "error":            "SYSTEM_ERROR",
}


def _strip_suffix(exit_reason: str) -> str:
    """去掉 '@xxx' 后缀, 返回基础 reason"""
    if not exit_reason:
        return ""
    return exit_reason.split("@", 1)[0]


def validate_exit_reason(exit_reason: str) -> bool:
    """校验 exit_reason 是否在白名单中

    支持 'TP1_PRICE_ACTION@trail_suppressed' 这种带后缀的格式。
    """
    base = _strip_suffix(exit_reason)
    return base in EXIT_REASON_REGISTRY


def get_exit_category(exit_reason: str) -> Optional[str]:
    """获取退出原因的分类 (返回 ExitCategory.value 字符串, 未注册返回 None)"""
    base = _strip_suffix(exit_reason)
    entry = EXIT_REASON_REGISTRY.get(base)
    if not entry:
        return None
    cat = entry["category"]
    return cat.value if isinstance(cat, ExitCategory) else str(cat)


def normalize_exit_reason(raw_reason: str) -> str:
    """尝试将非标准 exit_reason 映射到标准名称

    规则:
      1. 已是标准名 (含带 @ 后缀) → 原样返回
      2. 命中 LEGACY_MAPPINGS → 返回标准名
      3. 都不匹配 → 原样返回并记录 warning
    """
    if not raw_reason:
        return raw_reason

    if validate_exit_reason(raw_reason):
        return raw_reason

    base = _strip_suffix(raw_reason).lower().strip()
    suffix = raw_reason[len(_strip_suffix(raw_reason)):]  # 包含 '@xxx'

    if base in LEGACY_MAPPINGS:
        mapped = LEGACY_MAPPINGS[base]
        return mapped + suffix if suffix else mapped

    logger.warning(f"Unrecognized exit_reason: '{raw_reason}', keeping original")
    return raw_reason


def list_reasons_by_category(category: ExitCategory) -> Dict[str, dict]:
    """列出某分类下所有 reason"""
    return {
        code: meta
        for code, meta in EXIT_REASON_REGISTRY.items()
        if meta["category"] == category
    }


def sync_glossary_to_db(db_path: str) -> int:
    """将 EXIT_REASON_REGISTRY 同步写入 exit_reason_glossary 表 (UPSERT)

    用于初始化 / 升级时, 让 glossary 表自带描述信息。
    Returns: 写入条数
    """
    import sqlite3
    from datetime import datetime

    written = 0
    conn = sqlite3.connect(db_path)
    try:
        # 确保表存在 (与 trade_store._init_db 中的定义保持一致)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS exit_reason_glossary (
                reason_code        TEXT PRIMARY KEY,
                category           TEXT NOT NULL,
                description        TEXT,
                total_count        INTEGER DEFAULT 0,
                win_count          INTEGER DEFAULT 0,
                avg_pnl            REAL DEFAULT 0,
                avg_holding_hours  REAL DEFAULT 0,
                last_updated       TEXT DEFAULT (datetime('now'))
            )
        """)
        now = datetime.now().isoformat()
        for code, meta in EXIT_REASON_REGISTRY.items():
            cat = meta["category"]
            cat_value = cat.value if isinstance(cat, ExitCategory) else str(cat)
            conn.execute("""
                INSERT INTO exit_reason_glossary (reason_code, category, description, last_updated)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(reason_code) DO UPDATE SET
                    category = excluded.category,
                    description = excluded.description,
                    last_updated = excluded.last_updated
            """, (code, cat_value, meta.get("description", ""), now))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written
