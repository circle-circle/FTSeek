"""
交易归因 API (Trade Attribution API)

提供交易归因系统的 HTTP API 接口，支持：
1. 策略表现分析
2. 市场状态归因
3. 执行质量分析
4. 策略健康度监控
5. 权重调整建议
"""
import logging
from datetime import datetime
from typing import Dict, Any

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger("ftseek.api.attribution")
logger.propagate = False

router = APIRouter(prefix="/api/v2/attribution", tags=["attribution"])


@router.get("/strategies")
def get_strategy_attribution(
    days: int = Query(default=30, ge=1, le=365, description="查询天数范围"),
) -> Dict[str, Any]:
    """
    获取策略表现归因分析

    返回各策略的：
    - 交易统计 (总数/胜率/盈亏)
    - 质量评分
    - 市场状态适配度
    - 置信度校准
    """
    from app.analytics.attribution_engine import get_attribution_engine

    engine = get_attribution_engine()

    try:
        results = engine.analyze_strategy_performance(days=days)
        return {
            "success": True,
            "period_days": days,
            "strategies": [s.to_dict() for s in results],
            "best_strategy": max(results, key=lambda x: x.quality_score).strategy_name if results else None,
            "worst_strategy": min(results, key=lambda x: x.quality_score).strategy_name if results else None,
        }
    except Exception as e:
        logger.error(f"策略归因分析失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"分析失败：{str(e)}")


@router.get("/regimes")
def get_market_regime_attribution(
    days: int = Query(default=30, ge=1, le=365, description="查询天数范围"),
) -> Dict[str, Any]:
    """
    获取市场状态归因分析

    返回各市场状态 (trending/ranging/neutral) 下:
    - 交易统计
    - 各策略表现
    - 最佳/最差策略
    """
    from app.analytics.attribution_engine import get_attribution_engine

    engine = get_attribution_engine()

    try:
        results = engine.analyze_market_regime_performance(days=days)
        return {
            "success": True,
            "period_days": days,
            "regimes": [r.to_dict() for r in results],
        }
    except Exception as e:
        logger.error(f"市场状态归因分析失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"分析失败：{str(e)}")


@router.get("/execution")
def get_execution_attribution(
    days: int = Query(default=30, ge=1, le=365, description="查询天数范围"),
) -> Dict[str, Any]:
    """
    获取执行质量归因分析

    返回：
    - 平均滑点 (bps)
    - 平均延迟 (ms)
    - 总成本
    - 成本拖累比率
    """
    from app.analytics.attribution_engine import get_attribution_engine

    engine = get_attribution_engine()

    try:
        result = engine.analyze_execution_quality(days=days)
        return {
            "success": True,
            "period_days": days,
            "execution_quality": result.to_dict(),
        }
    except Exception as e:
        logger.error(f"执行质量分析失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"分析失败：{str(e)}")


@router.get("/news")
def get_news_attribution(
    days: int = Query(default=30, ge=1, le=365, description="查询天数范围"),
) -> Dict[str, Any]:
    """
    获取新闻影响归因分析

    返回：
    - 有新闻 vs 无新闻的交易表现对比
    - 新闻情绪影响
    - 新闻溢价
    """
    from app.analytics.attribution_engine import get_attribution_engine

    engine = get_attribution_engine()

    try:
        result = engine.analyze_news_impact(days=days)
        return {
            "success": True,
            "period_days": days,
            "news_impact": result.to_dict(),
        }
    except Exception as e:
        logger.error(f"新闻影响分析失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"分析失败：{str(e)}")


@router.get("/report")
def get_full_attribution_report(
    period: str = Query(default="30d", pattern="^(7d|30d|90d)$", description="查询周期"),
) -> Dict[str, Any]:
    """
    获取完整归因报告

    包含：
    - 策略归因
    - 市场状态归因
    - 执行质量归因
    - 新闻影响归因
    - 关键发现
    """
    from app.analytics.attribution_engine import get_attribution_engine

    engine = get_attribution_engine()

    try:
        report = engine.generate_attribution_report(period=period)
        return {
            "success": True,
            "report": report,
        }
    except Exception as e:
        logger.error(f"归因报告生成失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"报告生成失败：{str(e)}")


@router.get("/weights/recommend")
def get_weight_recommendations(
    days: int = Query(default=30, ge=1, le=365, description="分析天数范围"),
) -> Dict[str, Any]:
    """
    获取策略权重建议

    基于历史表现计算各策略的建议权重
    """
    from app.analytics.attribution_engine import get_attribution_engine

    engine = get_attribution_engine()

    try:
        recommendations = engine.get_weight_recommendations(days=days)
        return {
            "success": True,
            "period_days": days,
            "recommendations": recommendations,
        }
    except Exception as e:
        logger.error(f"权重建议生成失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成失败：{str(e)}")


# ==================== 策略健康度 API ====================

@router.get("/health")
def get_strategy_health() -> Dict[str, Any]:
    """
    获取所有策略的健康度指标

    返回各策略的：
    - 最近胜率
    - 盈亏比
    - 连续亏损
    - 市场适配度
    - 健康等级
    """
    from app.analytics.strategy_health import get_strategy_health_tracker

    tracker = get_strategy_health_tracker()

    try:
        report = tracker.get_health_report()
        return {
            "success": True,
            "report": report,
        }
    except Exception as e:
        logger.error(f"策略健康度获取失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取失败：{str(e)}")


@router.get("/health/{strategy_name}")
def get_strategy_health_detail(
    strategy_name: str,
    current_regime: str = Query(default=None, description="当前市场状态"),
) -> Dict[str, Any]:
    """
    获取指定策略的健康度详情

    Args:
        strategy_name: 策略名称
        current_regime: 当前市场状态
    """
    from app.analytics.strategy_health import get_strategy_health_tracker

    tracker = get_strategy_health_tracker()

    try:
        metrics = tracker.get_health_metrics(strategy_name, current_regime)
        return {
            "success": True,
            "strategy": strategy_name,
            "metrics": metrics.to_dict(),
        }
    except Exception as e:
        logger.error(f"策略健康度详情获取失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取失败：{str(e)}")


@router.post("/health/trade")
def record_trade_health(
    trade_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    记录交易结果到健康度追踪器

    用于实时更新策略健康度

    Request Body:
    - trade_id: 交易 ID
    - strategy: 策略名称
    - code: 股票代码
    - action: BUY/SELL
    - pnl_percent: 盈亏百分比
    - entry_time: 入场时间
    - exit_time: 出场时间
    - market_regime: 市场状态
    """
    from app.analytics.strategy_health import get_strategy_health_tracker, TradeRecord

    tracker = get_strategy_health_tracker()

    try:
        trade = TradeRecord(
            trade_id=trade_data.get('trade_id', ''),
            strategy=trade_data.get('strategy', ''),
            code=trade_data.get('code', ''),
            action=trade_data.get('action', ''),
            entry_price=trade_data.get('entry_price', 0.0),
            exit_price=trade_data.get('exit_price', 0.0),
            pnl=trade_data.get('pnl', 0.0),
            pnl_percent=trade_data.get('pnl_percent', 0.0),
            entry_time=trade_data.get('entry_time', ''),
            exit_time=trade_data.get('exit_time', ''),
            market_regime=trade_data.get('market_regime', 'neutral'),
        )

        tracker.record_trade(trade)

        # 返回更新后的健康度
        metrics = tracker.get_health_metrics(trade.strategy)

        return {
            "success": True,
            "message": "交易已记录",
            "updated_health": metrics.to_dict(),
        }
    except Exception as e:
        logger.error(f"交易健康度记录失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"记录失败：{str(e)}")


@router.post("/health/signal")
def record_signal_health(
    strategy: str,
) -> Dict[str, Any]:
    """
    记录信号产生 (用于频率统计)

    Args:
        strategy: 策略名称
    """
    from app.analytics.strategy_health import get_strategy_health_tracker

    tracker = get_strategy_health_tracker()

    try:
        tracker.record_signal(strategy)
        return {
            "success": True,
            "message": f"策略 {strategy} 信号已记录",
        }
    except Exception as e:
        logger.error(f"信号记录失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"记录失败：{str(e)}")


@router.post("/health/regime/update")
def update_market_regime(
    regime: str,
) -> Dict[str, Any]:
    """
    更新当前市场状态

    Args:
        regime: 市场状态 (trending/ranging/neutral)
    """
    from app.analytics.strategy_health import get_strategy_health_tracker

    tracker = get_strategy_health_tracker()

    try:
        tracker.update_current_regime(regime)
        return {
            "success": True,
            "message": f"市场状态已更新：{regime}",
        }
    except Exception as e:
        logger.error(f"市场状态更新失败：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"更新失败：{str(e)}")
