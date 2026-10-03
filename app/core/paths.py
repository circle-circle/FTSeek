#!/usr/bin/env python3
"""
FTSeek-US 路径解析单一入口 (F18 2026-05-12)

所有路径必须通过此模块获取, 禁止硬编码 /opt/ftseek/ 绝对路径。
优先级: YAML 配置 > 环境变量 > 项目根相对路径
"""
import os
from pathlib import Path
from typing import Optional

# 项目根目录 (app/core/paths.py → app/ → ../ → project root)
_PROJECT_ROOT: Optional[Path] = None


def _get_project_root() -> Path:
    """获取项目根目录 (lazy init)"""
    global _PROJECT_ROOT
    if _PROJECT_ROOT is None:
        _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    return _PROJECT_ROOT


def _get_config():
    """Lazy import config (avoid circular imports)"""
    from app.core.config_container import get_config
    return get_config()


def get_data_dir() -> Path:
    """获取 data/ 目录"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.data_dir", "data")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_exit_ledger_db() -> Path:
    """Playbook exit evidence under the existing data root; no new config key."""
    return get_data_dir() / "playbook_exits.db"


def get_reviews_dir() -> Path:
    """获取 daily_reviews/ 目录"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.reviews_dir", "data/daily_reviews")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_decisions_db() -> Path:
    """获取 decisions.db 路径"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.decisions_db", "data/decisions.db")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_rejections_db() -> Path:
    """获取 rejections.db 路径"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.rejections_db", "data/rejections.db")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def _resolve_sim_trade_records_db() -> Path:
    """Resolve the SIM trade ledger from override/config without TRADE_ENV routing."""
    override = os.getenv("FTSEEK_DB_FILE", "").strip()
    if override:
        path = Path(override)
        return path if path.is_absolute() else _get_project_root() / path
    cfg = _get_config()
    relative = cfg.get_dotted("paths.trade_records_db", "data/ftseek_us.db")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_trade_records_db() -> Path:
    """获取 ftseek_us.db 路径 (实盘: ftseek_us_live.db)"""
    # 实盘隔离 (2026-06-09): TRADE_ENV=0 时使用独立数据库文件
    if os.getenv("TRADE_ENV", "1").strip() == "0":
        return _get_project_root() / "data" / "ftseek_us_live.db"
    return _resolve_sim_trade_records_db()


def get_trade_records_db_for_arm(target_env: str) -> Path:
    """Resolve the execution ledger for an explicit playbook arm.

    The SIM arm must remain the research ledger even if legacy TRADE_ENV=0 is set.
    The LIVE arm is always the isolated real ledger and never follows TRADE_ENV.
    """
    normalized = str(target_env or "").strip().lower()
    if normalized in {"real", "live"}:
        return _get_project_root() / "data" / "ftseek_us_live.db"

    # In normal operation (TRADE_ENV=1) this preserves the historical resolver so
    # callers/tests that override get_trade_records_db() keep working. If legacy
    # TRADE_ENV=0 is ever set, SIM is pinned explicitly and cannot drift to LIVE.
    if os.getenv("TRADE_ENV", "1").strip() != "0":
        return get_trade_records_db()

    return _resolve_sim_trade_records_db()


def get_exit_quality_db() -> Path:
    """获取 exit_quality.db 路径"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.exit_quality_db", "data/exit_quality.db")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_battle_plan_path() -> Path:
    """获取 daily_battle_plan_us.json 路径"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.battle_plan", "data/daily_battle_plan_us.json")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_logs_dir() -> Path:
    """获取 logs/ 目录"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.logs_dir", "logs")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_config_path() -> Path:
    """获取 unified_config.yaml 路径"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.config_path", "config/unified_config.yaml")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_exit_policy_path() -> Path:
    """获取 exit_policy.yaml 路径"""
    cfg = _get_config()
    relative = cfg.get_dotted("paths.exit_policy", "app/risk/exit_policy.yaml")
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_project_root() -> Path:
    """获取项目根目录"""
    return _get_project_root()


def _resolve(key: str, default: str) -> Path:
    cfg = _get_config()
    relative = cfg.get_dotted(f"paths.{key}", default)
    if os.path.isabs(relative):
        return Path(relative)
    return _get_project_root() / relative


def get_weekly_reviews_dir() -> Path:
    return _resolve("weekly_reviews_dir", "data/weekly_reviews")


def get_monthly_reviews_dir() -> Path:
    return _resolve("monthly_reviews_dir", "data/monthly_reviews")


def get_improvement_plans_dir() -> Path:
    return _resolve("improvement_plans_dir", "data/improvement_plans")


def get_review_db() -> Path:
    return _resolve("review_db", "data/ftseek_us_review.db")


def get_trade_records_legacy_db() -> Path:
    """旧版 trade_records.db (legacy, 部分 module 仍读), 与主 ftseek_us.db 区分"""
    return _resolve("trade_records_legacy_db", "data/trade_records.db")


# get_trade_records_json 已删 (2026-07-26 扫尾): trade_records.json 07-15 deprecated, getter 零调用方


def get_obsidian_vault() -> Path:
    return _resolve("obsidian_vault", "obsidian_vault")


def get_vix_cache_dir() -> Path:
    return _resolve("vix_cache_dir", "data/vix_cache")


def get_news_us_db() -> Path:
    return _resolve("news_us_db", "data/rsshub_us/news_us.db")


def get_debate_history_db() -> Path:
    return _resolve("debate_history_db", "data/debate_framework/debate_history.db")


def get_watchlist_path() -> Path:
    return _resolve("watchlist", "config/watchlist_us.json")


def get_us_stock_config_path() -> Path:
    return _resolve("us_stock_config", "config/us_stock_config.yaml")
