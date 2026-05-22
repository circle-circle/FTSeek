"""
场景识别 API (Scenario API)

提供场景识别相关接口：
- /api/v2/scenario/current - 当前场景状态
- /api/v2/scenario/history - 场景历史
- /api/v2/scenario/performance - 场景表现统计
- /api/v2/scenario/strategies - 场景策略映射
"""
from fastapi import APIRouter, Query, HTTPException
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

logger = logging.getLogger("ftseek.api.scenario")

router = APIRouter(prefix="/api/v2/scenario", tags=["Scenario Recognition"])


def _get_scenario_classifier():
    """获取场景分类器实例"""
    try:
        from app.scenario import get_scenario_classifier
        return get_scenario_classifier()
    except Exception as e:
        logger.error(f"获取场景分类器失败：{e}")
        return None


def _get_scenario_router():
    """获取场景路由器实例"""
    try:
        from app.scenario import get_scenario_router
        return get_scenario_router()
    except Exception as e:
        logger.error(f"获取场景路由器失败：{e}")
        return None


def _get_scenario_memory():
    """获取场景记忆器实例"""
    try:
        from app.scenario import get_scenario_memory
        return get_scenario_memory()
    except Exception as e:
        logger.error(f"获取场景记忆器失败：{e}")
        return None


def _get_kline_data(code: str, limit: int = 100) -> List[Dict]:
    """获取 K 线数据用于场景分类"""
    try:
        from app.data_subscriber import get_subscriber
        subscriber = get_subscriber()

        if code in subscriber._kline_buffers:
            buf = subscriber._kline_buffers[code]
            klines = []
            for i in range(min(limit, buf.size)):
                idx = -limit + i
                if abs(idx) <= buf.size:
                    klines.append({
                        'open': buf.open[idx] if hasattr(buf, 'open') else 0,
                        'high': buf.high[idx] if hasattr(buf, 'high') else 0,
                        'low': buf.low[idx] if hasattr(buf, 'low') else 0,
                        'close': buf.close[idx] if hasattr(buf, 'close') else 0,
                        'volume': buf.volume[idx] if hasattr(buf, 'volume') else 0,
                    })
            return klines
    except Exception as e:
        logger.debug(f"获取 K 线数据失败：{e}")

    # 返回模拟数据用于测试
    base_price = 100.0
    return [
        {
            'open': base_price + i * 0.5,
            'high': base_price + i * 0.5 + 1,
            'low': base_price + i * 0.5 - 1,
            'close': base_price + i * 0.5 + 0.5,
            'volume': 1000000,
        }
        for i in range(limit)
    ]


@router.get("/current")
async def get_current_scenario(
    code: str = Query(..., description="股票代码")
) -> Dict[str, Any]:
    """
    获取某股票的当前场景状态

    Args:
        code: 股票代码

    Returns:
        {
            "scenario": "trend_strong",
            "confidence": 0.85,
            "macro_regime": "strong_trend_up",
            "adx": 35.2,
            "strategies": {...}
        }
    """
    classifier = _get_scenario_classifier()
    if not classifier:
        return {"error": "场景分类器未初始化"}

    try:
        # 获取 K 线数据
        klines = _get_kline_data(code, limit=100)

        # 执行场景分类
        result = classifier.classify(code, klines)

        # 获取策略路由
        router = _get_scenario_router()
        strategies = {}
        if router:
            route_result = router.route(result.scenario_type)
            strategies = route_result.to_dict()

        return {
            "code": code,
            "scenario": result.scenario_type.value,
            "confidence": round(result.confidence, 2),
            "macro_regime": result.macro_regime.value,
            "adx": round(result.adx_value, 1),
            "atr_percentile": round(result.atr_percentile, 1),
            "timestamp": datetime.now().isoformat(),
            "strategies": strategies,
        }

    except Exception as e:
        logger.error(f"获取场景状态失败：{e}")
        return {"error": str(e)}


@router.get("/history")
async def get_scenario_history(
    code: str = Query(..., description="股票代码"),
    limit: int = Query(20, description="返回记录数")
) -> Dict[str, Any]:
    """
    获取某股票的历史场景切换记录

    Args:
        code: 股票代码
        limit: 返回记录数

    Returns:
        {
            "code": "HK.00700",
            "history": [
                {"scenario": "trend_strong", "timestamp": "...", "confidence": 0.85},
                ...
            ]
        }
    """
    memory = _get_scenario_memory()
    if not memory:
        return {"error": "场景记忆器未初始化"}

    try:
        history = memory.get_history(code, limit=limit)
        return {
            "code": code,
            "history": history,
            "current_scenario": memory.get_current_scenario(code).value,
            "confirm_count": memory.get_confirm_count(code),
        }

    except Exception as e:
        logger.error(f"获取场景历史失败：{e}")
        return {"error": str(e)}


@router.get("/performance")
async def get_scenario_performance(
    days: int = Query(30, description="分析天数范围")
) -> Dict[str, Any]:
    """
    获取各场景的表现统计

    Args:
        days: 分析天数范围

    Returns:
        {
            "days": 30,
            "scenarios": {
                "trend_strong": {
                    "total_trades": 15,
                    "wins": 10,
                    "losses": 5,
                    "win_rate": 0.67,
                    "avg_pnl_pct": 2.5,
                    "total_pnl_pct": 37.5
                },
                ...
            }
        }
    """
    try:
        from app.signal.analytics import get_analytics
        analytics = get_analytics()

        # 分析各场景表现
        perf = analytics.analyze_scenario_performance(days=days)

        # 格式化输出
        scenarios = {}
        for scenario, stats in perf.items():
            scenarios[scenario] = {
                "total_trades": stats.get('total_trades', 0),
                "wins": stats.get('winning_trades', 0),
                "losses": stats.get('losing_trades', 0),
                "win_rate": round(stats.get('win_rate', 0), 3),
                "avg_pnl_pct": round(stats.get('avg_pnl_pct', 0), 2),
                "total_pnl_pct": round(stats.get('total_pnl_pct', 0), 2),
            }

        return {
            "days": days,
            "scenarios": scenarios,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"获取场景表现失败：{e}")
        return {"error": str(e)}


@router.get("/strategies")
async def get_scenario_strategies(
    scenario: Optional[str] = Query(None, description="场景名称，不传则返回所有")
) -> Dict[str, Any]:
    """
    获取场景的策略映射配置

    Args:
        scenario: 场景名称（可选，不传则返回所有场景）

    Returns:
        {
            "scenarios": {
                "trend_strong": {
                    "primary": ["supertrend", "confluence_v2"],
                    "secondary": ["enhanced_confluence", "macd"],
                    "suppressed": ["rsi", "bollinger"],
                    "risk_template": "trend_following",
                    "weights": {...}
                },
                ...
            }
        }
    """
    from app.scenario import get_all_scenarios, get_scenario_config

    try:
        if scenario:
            # 返回单个场景配置
            config = get_scenario_config(scenario)
            return {
                "scenario": scenario,
                "config": config,
            }
        else:
            # 返回所有场景配置
            all_scenarios = get_all_scenarios()
            configs = {}
            for s in all_scenarios:
                configs[s] = get_scenario_config(s)

            return {
                "scenarios": configs,
                "total": len(configs),
            }

    except Exception as e:
        logger.error(f"获取场景策略配置失败：{e}")
        return {"error": str(e)}


@router.get("/list")
async def list_all_scenarios() -> Dict[str, Any]:
    """
    列出所有已配置的场景

    Returns:
        {
            "scenarios": [
                {"name": "TREND_STRONG", "description": "强趋势"},
                ...
            ]
        }
    """
    from app.scenario import get_all_scenarios, CORE_SCENARIO_ROUTING

    try:
        all_scenarios = get_all_scenarios()
        result = []
        for s in all_scenarios:
            config = CORE_SCENARIO_ROUTING.get(s, {})
            result.append({
                "name": s,
                "description": config.get('description', ''),
                "risk_template": config.get('risk_template', 'default'),
            })

        return {
            "scenarios": result,
            "total": len(result),
        }

    except Exception as e:
        logger.error(f"列出场景失败：{e}")
        return {"error": str(e)}


@router.get("/classifier/status")
async def get_classifier_status() -> Dict[str, Any]:
    """
    获取场景分类器状态

    Returns:
        {
            "initialized": true,
            "config": {
                "confirm_window": 3,
                "cool_down_minutes": 2,
                "confidence_threshold": 0.65
            },
            "active_codes": ["HK.00700", "HK.09988"],
            "scenarios_count": 5
        }
    """
    from app.scenario import SCENARIO_CLASSIFIER_CONFIG, get_all_scenarios

    classifier = _get_scenario_classifier()
    memory = _get_scenario_memory()

    try:
        active_codes = []
        if memory:
            states = memory.get_all_states()
            active_codes = list(states.keys())

        return {
            "initialized": classifier is not None,
            "config": SCENARIO_CLASSIFIER_CONFIG,
            "active_codes": active_codes,
            "scenarios_count": len(get_all_scenarios()),
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"获取分类器状态失败：{e}")
        return {"error": str(e)}
