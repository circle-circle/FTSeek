"""
场景日志 API (Scenario Log API)

提供场景日志查询接口：
- /api/v2/scenario/log/history - 场景历史
- /api/v2/scenario/log/switches - 场景切换记录
- /api/v2/scenario/log/statistics - 场景统计
- /api/v2/scenario/log/frequency - 切换频率
"""
from fastapi import APIRouter, Query, HTTPException
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

logger = logging.getLogger("ftseek.api.scenario_log")

router = APIRouter(prefix="/api/v2/scenario/log", tags=["Scenario Log"])


def _get_scenario_log_manager():
    """获取场景日志管理器实例"""
    try:
        from app.scenario import get_scenario_log_manager
        return get_scenario_log_manager()
    except Exception as e:
        logger.error(f"获取场景日志管理器失败：{e}")
        return None


@router.get("/history")
async def get_scenario_history(
    code: str = Query(..., description="股票代码"),
    limit: int = Query(50, description="返回记录数"),
    days: int = Query(7, description="查询天数范围")
) -> Dict[str, Any]:
    """
    获取某股票的场景历史

    Args:
        code: 股票代码
        limit: 返回记录数
        days: 查询天数范围

    Returns:
        {
            "code": "HK.00700",
            "history": [
                {"scenario": "trend_strong", "confidence": 0.85, "timestamp": "..."},
                ...
            ],
            "total": 50
        }
    """
    log_manager = _get_scenario_log_manager()
    if not log_manager:
        return {"error": "场景日志管理器未初始化"}

    try:
        history = log_manager.get_scenario_history(code, limit=limit, days=days)
        return {
            "code": code,
            "history": history,
            "total": len(history),
        }
    except Exception as e:
        logger.error(f"获取场景历史失败：{e}")
        return {"error": str(e)}


@router.get("/current")
async def get_current_scenario(
    code: str = Query(..., description="股票代码")
) -> Dict[str, Any]:
    """
    获取某股票的当前场景

    Args:
        code: 股票代码

    Returns:
        {
            "code": "HK.00700",
            "scenario": "trend_strong",
            "timestamp": "..."
        }
    """
    log_manager = _get_scenario_log_manager()
    if not log_manager:
        return {"error": "场景日志管理器未初始化"}

    try:
        scenario = log_manager.get_current_scenario(code)
        return {
            "code": code,
            "scenario": scenario,
        }
    except Exception as e:
        logger.error(f"获取当前场景失败：{e}")
        return {"error": str(e)}


@router.get("/switches")
async def get_scenario_switches(
    code: str = Query(..., description="股票代码"),
    days: int = Query(1, description="查询天数")
) -> Dict[str, Any]:
    """
    获取某股票的场景切换记录

    Args:
        code: 股票代码
        days: 查询天数

    Returns:
        {
            "code": "HK.00700",
            "switches": [
                {
                    "from_scenario": "range_bound",
                    "to_scenario": "trend_strong",
                    "confidence": 0.85,
                    "timestamp": "..."
                },
                ...
            ],
            "total": 5
        }
    """
    log_manager = _get_scenario_log_manager()
    if not log_manager:
        return {"error": "场景日志管理器未初始化"}

    try:
        switches = log_manager.get_scenario_switches(code, days=days)
        return {
            "code": code,
            "switches": switches,
            "total": len(switches),
        }
    except Exception as e:
        logger.error(f"获取场景切换记录失败：{e}")
        return {"error": str(e)}


@router.get("/statistics")
async def get_scenario_statistics(
    days: int = Query(7, description="统计天数"),
    group_by: str = Query("scenario", description="分组方式：scenario/code")
) -> Dict[str, Any]:
    """
    获取场景统计数据

    Args:
        days: 统计天数
        group_by: 分组方式 (scenario/code)

    Returns:
        {
            "days": 7,
            "group_by": "scenario",
            "data": [
                {
                    "scenario": "trend_strong",
                    "total_count": 150,
                    "unique_codes": 12,
                    "avg_confidence": 0.78
                },
                ...
            ],
            "total_records": 500
        }
    """
    log_manager = _get_scenario_log_manager()
    if not log_manager:
        return {"error": "场景日志管理器未初始化"}

    try:
        stats = log_manager.get_scenario_statistics(days=days, group_by=group_by)
        return stats
    except Exception as e:
        logger.error(f"获取场景统计失败：{e}")
        return {"error": str(e)}


@router.get("/frequency")
async def get_scenario_frequency(
    code: Optional[str] = Query(None, description="股票代码，不传则统计全部"),
    hours: int = Query(24, description="统计小时数")
) -> Dict[str, Any]:
    """
    获取场景切换频率

    Args:
        code: 股票代码 (不传则统计全部)
        hours: 统计小时数

    Returns:
        {
            "hours": 24,
            "unique_codes": 15,
            "unique_scenarios": 8,
            "total_logs": 250,
            "logs_per_hour": 10.4
        }
    """
    log_manager = _get_scenario_log_manager()
    if not log_manager:
        return {"error": "场景日志管理器未初始化"}

    try:
        freq = log_manager.get_switch_frequency(code=code, hours=hours)
        return freq
    except Exception as e:
        logger.error(f"获取场景频率失败：{e}")
        return {"error": str(e)}


@router.get("/codes")
async def get_active_codes() -> Dict[str, Any]:
    """
    获取所有有场景记录的股票

    Returns:
        {
            "codes": ["HK.00700", "HK.09988", ...],
            "total": 20
        }
    """
    log_manager = _get_scenario_log_manager()
    if not log_manager:
        return {"error": "场景日志管理器未初始化"}

    try:
        codes = log_manager.get_all_active_codes()
        return {
            "codes": codes,
            "total": len(codes),
        }
    except Exception as e:
        logger.error(f"获取活跃股票失败：{e}")
        return {"error": str(e)}
