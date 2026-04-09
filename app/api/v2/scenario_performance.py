"""
场景绩效 API (Scenario Performance API) - Phase 3

提供场景绩效分析接口：
- /api/v2/scenario/performance/win-rate - 场景胜率统计
- /api/v2/scenario/performance/accuracy - 场景准确率详情
- /api/v2/scenario/performance/matrix - 场景×策略矩阵
- /api/v2/scenario/performance/confidence - 置信度校准
- /api/v2/scenario/performance/recommendations - 权重建议
"""
from fastapi import APIRouter, Query, HTTPException
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

logger = logging.getLogger("ftseek.api.scenario_performance")

router = APIRouter(prefix="/api/v2/scenario/performance", tags=["Scenario Performance"])


def _get_analytics():
    """获取场景绩效分析器实例"""
    try:
        from app.analytics.scenario_performance import get_scenario_performance_analytics
        return get_scenario_performance_analytics()
    except Exception as e:
        logger.error(f"获取场景绩效分析器失败：{e}")
        return None


@router.get("/win-rate")
async def get_scenario_win_rate(
    days: int = Query(30, description="分析天数范围")
) -> Dict[str, Any]:
    """
    获取各场景的胜率统计

    Args:
        days: 分析天数范围

    Returns:
        {
            "days": 30,
            "scenarios": {
                "trend_strong": {
                    "total_trades": 50,
                    "wins": 32,
                    "losses": 18,
                    "win_rate": 0.64,
                    "avg_pnl_pct": 2.35
                },
                ...
            }
        }
    """
    analytics = _get_analytics()
    if not analytics:
        return {"error": "场景绩效分析器未初始化"}

    try:
        performance = analytics.analyze_scenario_win_rate(days=days)
        return {
            "days": days,
            "scenarios": performance,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"获取场景胜率失败：{e}")
        return {"error": str(e)}


@router.get("/accuracy")
async def get_scenario_accuracy(
    scenario: str = Query(..., description="场景名称"),
    days: int = Query(30, description="分析天数范围")
) -> Dict[str, Any]:
    """
    获取某场景的准确率详情

    Args:
        scenario: 场景名称
        days: 分析天数范围

    Returns:
        {
            "scenario": "trend_strong",
            "days": 30,
            "total_trades": 50,
            "accuracy": 0.64,
            "avg_pnl": 2.35,
            "best_trade": 8.5,
            "worst_trade": -4.2,
            "consecutive_wins": 5,
            "consecutive_losses": 2
        }
    """
    analytics = _get_analytics()
    if not analytics:
        return {"error": "场景绩效分析器未初始化"}

    try:
        accuracy = analytics.get_scenario_accuracy(scenario, days=days)
        return accuracy
    except Exception as e:
        logger.error(f"获取场景准确率失败：{e}")
        return {"error": str(e)}


@router.get("/matrix")
async def get_scenario_strategy_matrix(
    days: int = Query(30, description="分析天数范围")
) -> Dict[str, Any]:
    """
    获取场景×策略的二维表现矩阵

    Args:
        days: 分析天数范围

    Returns:
        {
            "days": 30,
            "matrix": {
                "trend_strong": {
                    "supertrend": {"trades": 15, "win_rate": 0.67, "avg_pnl": 2.8},
                    ...
                },
                ...
            }
        }
    """
    analytics = _get_analytics()
    if not analytics:
        return {"error": "场景绩效分析器未初始化"}

    try:
        matrix = analytics.analyze_scenario_strategy_matrix(days=days)
        return {
            "days": days,
            "matrix": matrix,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"获取场景策略矩阵失败：{e}")
        return {"error": str(e)}


@router.get("/best-strategies")
async def get_best_strategies(
    scenario: str = Query(..., description="场景名称"),
    days: int = Query(30, description="分析天数范围"),
    min_trades: int = Query(3, description="最小交易数过滤")
) -> Dict[str, Any]:
    """
    获取某场景下表现最好的策略

    Args:
        scenario: 场景名称
        days: 分析天数范围
        min_trades: 最小交易数过滤

    Returns:
        {
            "scenario": "trend_strong",
            "days": 30,
            "best_strategies": [
                {"strategy": "supertrend", "trades": 15, "win_rate": 0.67, "avg_pnl": 2.8},
                ...
            ]
        }
    """
    analytics = _get_analytics()
    if not analytics:
        return {"error": "场景绩效分析器未初始化"}

    try:
        strategies = analytics.get_best_strategies_for_scenario(scenario, days=days, min_trades=min_trades)
        return {
            "scenario": scenario,
            "days": days,
            "best_strategies": strategies,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"获取最佳策略失败：{e}")
        return {"error": str(e)}


@router.get("/confidence")
async def get_confidence_accuracy(
    days: int = Query(30, description="分析天数范围"),
    buckets: int = Query(5, description="置信度分桶数")
) -> Dict[str, Any]:
    """
    获取置信度区间的准确率 (置信度校准)

    Args:
        days: 分析天数范围
        buckets: 置信度分桶数

    Returns:
        {
            "days": 30,
            "buckets": {
                "0.5-0.6": {"trades": 20, "win_rate": 0.45, "avg_pnl": 1.2},
                "0.6-0.7": {"trades": 35, "win_rate": 0.52, "avg_pnl": 1.8},
                ...
            }
        }
    """
    analytics = _get_analytics()
    if not analytics:
        return {"error": "场景绩效分析器未初始化"}

    try:
        confidence_data = analytics.analyze_confidence_accuracy(days=days, confidence_buckets=buckets)
        return {
            "days": days,
            "buckets": confidence_data,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"获取置信度校准失败：{e}")
        return {"error": str(e)}


@router.get("/switch-performance")
async def get_switch_performance(
    code: Optional[str] = Query(None, description="股票代码"),
    days: int = Query(7, description="分析天数范围")
) -> Dict[str, Any]:
    """
    分析场景切换后的绩效表现

    Args:
        code: 股票代码 (不传则统计全部)
        days: 分析天数范围

    Returns:
        {
            "switches_analyzed": 50,
            "avg_performance_after_switch": 1.8,
            "by_switch_type": {
                "range_bound→trend_strong": {"count": 10, "avg_pnl": 2.5},
                ...
            }
        }
    """
    analytics = _get_analytics()
    if not analytics:
        return {"error": "场景绩效分析器未初始化"}

    try:
        perf = analytics.analyze_scenario_switch_performance(code=code, days=days)
        return {
            "code": code,
            "days": days,
            **perf,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"获取切换绩效失败：{e}")
        return {"error": str(e)}


@router.get("/recommendations")
async def get_weight_recommendations(
    days: int = Query(30, description="分析天数范围")
) -> Dict[str, Any]:
    """
    基于历史绩效获取场景权重建议

    Args:
        days: 分析天数范围

    Returns:
        {
            "days": 30,
            "recommendations": {
                "trend_strong": {
                    "current_weight": 1.5,
                    "recommended_weight": 1.7,
                    "reason": "win_rate=0.65, avg_pnl=2.8, trades=50"
                },
                ...
            }
        }
    """
    analytics = _get_analytics()
    if not analytics:
        return {"error": "场景绩效分析器未初始化"}

    try:
        recommendations = analytics.get_scenario_weight_recommendations(days=days)
        return {
            "days": days,
            "recommendations": recommendations,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.error(f"获取权重建议失败：{e}")
        return {"error": str(e)}
