"""
复盘大盘 API v2 (Review Dashboard API v2)

提供深度复盘分析数据，支持可视化展示：
- /api/v2/review/dashboard - KPI 概览 + 收益曲线
- /api/v2/review/attribution - 策略归因分析
- /api/v2/review/correlation - 信号相关性分析
- /api/v2/review/recommendations - 优化建议
"""
from fastapi import APIRouter, Query, HTTPException
from typing import Optional, Dict, Any, List
from datetime import datetime
import logging

logger = logging.getLogger("ftseek.api.review_v2")

router = APIRouter(prefix="/api/v2/review", tags=["Review Dashboard v2"])


def _calculate_equity_curve(chains: List) -> List[Dict]:
    """计算收益曲线数据"""
    from app.storage.trade_recorder import get_trade_chain_recorder

    recorder = get_trade_chain_recorder()
    all_chains = recorder.get_all_chains()

    # 按创建时间排序
    sorted_chains = sorted(all_chains, key=lambda c: c.created_at)

    equity_data = []
    cumulative_pnl = 0.0

    for chain in sorted_chains:
        if chain.is_closed and chain.result:
            cumulative_pnl += chain.result.pnl
            equity_data.append({
                'date': chain.result.exit_time[:16] if chain.result.exit_time else chain.created_at[:16],
                'cumulative_pnl': round(cumulative_pnl, 2),
                'trade_id': chain.trade_id,
                'code': chain.code,
                'pnl': round(chain.result.pnl, 2),
            })

    return equity_data


def _calculate_drawdown_curve(chains: List) -> List[Dict]:
    """计算回撤曲线数据"""
    from app.storage.trade_recorder import get_trade_chain_recorder

    recorder = get_trade_chain_recorder()
    all_chains = recorder.get_all_chains()

    # 按创建时间排序
    sorted_chains = sorted(all_chains, key=lambda c: c.created_at)

    drawdown_data = []
    cumulative_pnl = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for chain in sorted_chains:
        if chain.is_closed and chain.result:
            cumulative_pnl += chain.result.pnl
            if cumulative_pnl > peak:
                peak = cumulative_pnl

            drawdown = (peak - cumulative_pnl) / (peak + 0.001)
            if drawdown > max_drawdown:
                max_drawdown = drawdown

            drawdown_data.append({
                'date': chain.result.exit_time[:16] if chain.result.exit_time else chain.created_at[:16],
                'cumulative_pnl': round(cumulative_pnl, 2),
                'peak': round(peak, 2),
                'drawdown': round(drawdown, 4),
                'max_drawdown': round(max_drawdown, 4),
            })

    return drawdown_data


def _calculate_sharpe_ratio(chains: List) -> float:
    """计算 Sharpe 比率"""
    import statistics

    closed_chains = [c for c in chains if c.is_closed and c.result]
    if len(closed_chains) < 2:
        return 0.0

    pnls = [c.result.pnl for c in closed_chains if c.result]

    try:
        mean_pnl = statistics.mean(pnls)
        std_pnl = statistics.stdev(pnls)
        if std_pnl == 0:
            return 0.0
        # 年化 Sharpe (假设日均交易)
        sharpe = mean_pnl / std_pnl
        return round(sharpe, 2)
    except Exception:
        return 0.0


def _calculate_max_drawdown(chains: List) -> float:
    """计算最大回撤"""
    closed_chains = [c for c in chains if c.is_closed and c.result]
    if not closed_chains:
        return 0.0

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0

    for chain in closed_chains:
        cumulative += chain.result.pnl
        if cumulative > peak:
            peak = cumulative
        if peak > 0:
            dd = (peak - cumulative) / peak
            if dd > max_dd:
                max_dd = dd

    return round(max_dd, 4)


@router.get("/dashboard")
async def get_dashboard_data(
    start_date: Optional[str] = Query(None, description="开始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="结束日期 (YYYY-MM-DD)"),
) -> Dict[str, Any]:
    """
    获取大盘概览数据

    包含:
    - KPI 指标：总交易数、胜率、总盈亏、Sharpe、最大回撤等
    - 收益曲线数据
    - 回撤分析数据
    """
    from app.storage.trade_recorder import get_trade_chain_recorder

    recorder = get_trade_chain_recorder()
    chains = recorder.get_all_chains(start_date, end_date)
    closed_chains = [c for c in chains if c.is_closed]

    # 计算 KPI 指标
    total_trades = len(chains)
    closed_trades = len(closed_chains)

    if closed_trades > 0:
        winning_trades = sum(1 for c in closed_chains if c.is_profitable)
        win_rate = winning_trades / closed_trades
        total_pnl = sum(c.result.pnl for c in closed_chains if c.result)
        avg_pnl = total_pnl / closed_trades
        avg_holding_minutes = int(sum(c.result.holding_period_minutes for c in closed_chains if c.result) / closed_trades)
        avg_slippage_bps = sum(c.execution.slippage_bps for c in closed_chains if c.execution) / closed_trades
    else:
        win_rate = 0.0
        total_pnl = 0.0
        avg_pnl = 0.0
        avg_holding_minutes = 0
        avg_slippage_bps = 0.0
        winning_trades = 0

    sharpe = _calculate_sharpe_ratio(chains)
    max_drawdown = _calculate_max_drawdown(chains)

    # 收益曲线和回撤分析
    equity_curve = _calculate_equity_curve(chains)
    drawdown_data = _calculate_drawdown_curve(chains)

    return {
        "generated_at": datetime.now().isoformat(),
        "filters": {
            "start_date": start_date,
            "end_date": end_date,
        },
        "kpi": {
            "total_trades": total_trades,
            "closed_trades": closed_trades,
            "winning_trades": winning_trades,
            "losing_trades": closed_trades - winning_trades,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(avg_pnl, 2),
            "sharpe_ratio": sharpe,
            "max_drawdown": max_drawdown,
            "avg_holding_minutes": avg_holding_minutes,
            "avg_slippage_bps": round(avg_slippage_bps, 2),
        },
        "equity_curve": equity_curve,
        "drawdown_analysis": {
            "max_drawdown": max_drawdown,
            "curve": drawdown_data,
        },
    }


@router.get("/attribution")
async def get_attribution_data(
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
) -> Dict[str, Any]:
    """
    获取策略归因分析数据

    包含:
    - 按策略分组统计
    - 按市场状态分组统计
    - 按信号强度分组统计
    - 策略×市场状态交叉分析
    - 噪声信号识别
    - 过拟合信号识别
    """
    from app.analytics.strategy_attribution import get_strategy_attribution

    attribution = get_strategy_attribution()
    report = attribution.get_full_report()

    # 应用日期过滤（在归因分析内部处理）
    # 注意：当前实现加载所有数据，日期过滤可在后续优化

    return {
        "generated_at": datetime.now().isoformat(),
        "filters": {
            "start_date": start_date,
            "end_date": end_date,
        },
        "summary": {
            "total_chains": report['total_chains'],
            "closed_chains": report['closed_chains'],
        },
        "by_strategy": report['by_strategy'],
        "by_market_regime": report['by_market_regime'],
        "by_signal_strength": report['by_signal_strength'],
        "strategy_regime_matrix": report['strategy_regime_cross'],
        "noise_signals": report['noise_signals'],
        "overfit_signals": report['overfit_signals'],
    }


@router.get("/correlation")
async def get_correlation_data(
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
) -> Dict[str, Any]:
    """
    获取信号相关性分析数据

    包含:
    - 信号分数 vs 盈亏相关性 (Pearson r)
    - 置信度 vs 胜率关系
    - 按策略分组的相关性
    - 噪声信号列表
    - 过拟合信号列表
    """
    from app.analytics.signal_correlation import get_signal_quality_correlation

    correlation = get_signal_quality_correlation()
    report = correlation.get_full_report()

    return {
        "generated_at": datetime.now().isoformat(),
        "filters": {
            "start_date": start_date,
            "end_date": end_date,
        },
        "summary": {
            "total_chains": report['total_chains'],
            "closed_chains": report['closed_chains'],
        },
        "score_pnl_correlation": report['score_pnl_correlation'],
        "confidence_vs_winrate": report['confidence_vs_winrate'],
        "by_strategy": report['by_strategy'],
        "noise_signals": report['noise_signals'],
        "overfit_signals": report['overfit_signals'],
    }


@router.get("/recommendations")
async def get_recommendations(
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
) -> Dict[str, Any]:
    """
    获取优化建议

    基于归因分析和相关性分析结果，自动生成优化建议：
    - 降权/加权建议
    - 重新校准建议
    - 黑名单建议
    - 执行策略调整建议
    """
    from app.analytics.strategy_attribution import get_strategy_attribution
    from app.analytics.signal_correlation import get_signal_quality_correlation
    from app.analytics.optimization_advisor import generate_optimization_recommendations
    from app.storage.trade_recorder import get_trade_chain_recorder

    # 获取分析数据
    attribution = get_strategy_attribution()
    correlation = get_signal_quality_correlation()
    recorder = get_trade_chain_recorder()

    attribution_report = attribution.get_full_report()
    correlation_report = correlation.get_full_report()

    # 获取执行统计数据
    stats = recorder.get_statistics()
    execution_stats = {
        'avg_slippage_bps': stats.get('avg_slippage_bps', 0),
        'avg_latency_ms': stats.get('avg_latency_ms', 0),
    }

    # 生成建议
    recommendations = generate_optimization_recommendations(
        strategy_attribution=attribution_report,
        signal_correlation=correlation_report,
        execution_stats=execution_stats,
    )

    return {
        "generated_at": datetime.now().isoformat(),
        "filters": {
            "start_date": start_date,
            "end_date": end_date,
        },
        "summary": {
            "total_recommendations": len(recommendations),
        },
        "recommendations": recommendations,
    }


@router.get("/trades/detail")
async def get_trade_details(
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    strategy: Optional[str] = Query(None, description="策略过滤"),
    regime: Optional[str] = Query(None, description="市场状态过滤"),
) -> Dict[str, Any]:
    """
    获取交易明细列表

    支持按日期、策略、市场状态过滤
    """
    from app.storage.trade_recorder import get_trade_chain_recorder

    recorder = get_trade_chain_recorder()
    chains = recorder.get_all_chains(start_date, end_date)

    # 过滤
    if strategy:
        chains = [c for c in chains if c.decision and c.decision.decision_reason == strategy]
    if regime:
        chains = [c for c in chains if c.signal and c.signal.market_regime == regime]

    # 转换为字典
    trade_details = []
    for chain in chains:
        detail = {
            'trade_id': chain.trade_id,
            'code': chain.code,
            'created_at': chain.created_at,
            'is_closed': chain.is_closed,
            'is_profitable': chain.is_profitable,
        }

        if chain.signal:
            detail['signal_score'] = chain.signal.signal_score
            detail['signal_confidence'] = chain.signal.signal_confidence
            detail['market_regime'] = chain.signal.market_regime
            detail['decision_reason'] = chain.decision.decision_reason if chain.decision else ''

        if chain.execution:
            detail['slippage_bps'] = chain.execution.slippage_bps
            detail['execution_latency_ms'] = chain.execution.execution_latency_ms

        if chain.result:
            detail['pnl'] = chain.result.pnl
            detail['pnl_percent'] = chain.result.pnl_percent
            detail['exit_reason'] = chain.result.exit_reason
            detail['holding_period_minutes'] = chain.result.holding_period_minutes

        trade_details.append(detail)

    return {
        "generated_at": datetime.now().isoformat(),
        "filters": {
            "start_date": start_date,
            "end_date": end_date,
            "strategy": strategy,
            "regime": regime,
        },
        "total": len(trade_details),
        "trades": trade_details[:100],  # 限制返回数量
    }
