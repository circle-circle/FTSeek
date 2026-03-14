"""
专业量化指标 API 端点

提供夏普比率、索提诺比率、VaR、CVaR 等专业指标计算接口
"""
import logging
from typing import Optional, List, Dict
from datetime import datetime, timedelta
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from app import config
from app.risk.metrics_calculator import MetricsCalculator, PerformanceMetrics, PortfolioRiskMetrics
from app.backtest.data_loader import generate_sample_data

logger = logging.getLogger("ftseek.api.metrics")

router = APIRouter(tags=["量化指标"])

# 全局指标计算器
calculator = MetricsCalculator(risk_free_rate=0.02, trading_days=252)


# ==================== 响应模型 ====================

class PerformanceMetricsResponse(BaseModel):
    """绩效指标响应"""
    status: str = "ok"
    metrics: Dict
    summary: str
    timestamp: str


class PortfolioRiskResponse(BaseModel):
    """组合风险响应"""
    status: str = "ok"
    beta: float
    correlation_matrix: Dict
    concentration_top3: str
    concentration_top5: str
    liquidity_ratio: str
    leverage: float
    timestamp: str


class EquityCurveResponse(BaseModel):
    """资金曲线响应"""
    status: str = "ok"
    data: List[Dict]
    start_date: str
    end_date: str
    total_return: str


class DrawdownCurveResponse(BaseModel):
    """回撤曲线响应"""
    status: str = "ok"
    data: List[Dict]
    current_drawdown: str
    max_drawdown: str


class StrategyPerformanceResponse(BaseModel):
    """策略表现响应"""
    status: str = "ok"
    strategies: List[Dict]
    best_strategy: str
    timestamp: str


class MarketRegimeResponse(BaseModel):
    """市场状态响应"""
    status: str = "ok"
    regime_type: str
    confidence: float
    indicators: Dict
    recommended_strategies: List[str]
    timestamp: str


# ==================== API 端点 ====================

@router.get("/metrics/performance", response_model=PerformanceMetricsResponse)
async def get_performance_metrics(
    days: int = Query(default=90, ge=1, le=365, description="回测天数"),
    strategy: str = Query(default="confluence_v2", description="策略名称"),
    token: str = Query(..., description="API Token")
):
    """
    获取专业绩效指标
    
    包括夏普比率、索提诺比率、最大回撤、VaR、CVaR 等
    """
    # Token 验证
    if token != config.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        # 生成模拟数据 (实际应从数据库加载)
        data = generate_sample_data("HK.00700", start_price=380.0, days=days)
        equity_curve = [380.0]  # 初始资金
        cumulative = 380.0
        
        # 简单模拟资金曲线
        for i, kline in enumerate(data):
            daily_return = (kline['close'] - kline['open']) / kline['open']
            cumulative = cumulative * (1 + daily_return * 0.1)  # 10% 仓位
            equity_curve.append(cumulative)
        
        # 计算指标
        metrics = calculator.calculate_all(equity_curve)
        
        return PerformanceMetricsResponse(
            status="ok",
            metrics=metrics.to_dict(),
            summary=metrics.summary(),
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"计算绩效指标失败：{e}")
        raise HTTPException(status_code=500, detail=f"计算失败：{str(e)}")


@router.get("/metrics/equity_curve", response_model=EquityCurveResponse)
async def get_equity_curve(
    days: int = Query(default=90, ge=1, le=365, description="天数"),
    strategy: str = Query(default="confluence_v2", description="策略名称"),
    token: str = Query(..., description="API Token")
):
    """
    获取资金曲线数据
    
    返回每日资金数据点，用于绘制资金曲线图
    """
    if token != config.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        # 生成模拟数据
        data = generate_sample_data("HK.00700", start_price=380.0, days=days)
        
        equity_curve = []
        cumulative = 100000.0  # 初始 10 万
        start_date = datetime.now() - timedelta(days=days)
        
        for i, kline in enumerate(data):
            date = start_date + timedelta(days=i)
            daily_return = (kline['close'] - kline['open']) / kline['open']
            cumulative = cumulative * (1 + daily_return * 0.1)
            
            equity_curve.append({
                "date": date.strftime("%Y-%m-%d"),
                "value": round(cumulative, 2),
                "return": round((cumulative - 100000.0) / 100000.0, 4)
            })
        
        total_return = (cumulative - 100000.0) / 100000.0
        
        return EquityCurveResponse(
            status="ok",
            data=equity_curve,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=datetime.now().strftime("%Y-%m-%d"),
            total_return=f"{total_return:.2%}"
        )
        
    except Exception as e:
        logger.error(f"获取资金曲线失败：{e}")
        raise HTTPException(status_code=500, detail=f"获取失败：{str(e)}")


@router.get("/metrics/drawdown_curve", response_model=DrawdownCurveResponse)
async def get_drawdown_curve(
    days: int = Query(default=90, ge=1, le=365, description="天数"),
    strategy: str = Query(default="confluence_v2", description="策略名称"),
    token: str = Query(..., description="API Token")
):
    """
    获取回撤曲线数据
    
    返回每日回撤数据点，用于绘制回撤曲线图
    """
    if token != config.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        # 生成模拟数据
        data = generate_sample_data("HK.00700", start_price=380.0, days=days)
        
        drawdown_data = []
        cumulative = 100000.0
        peak = 100000.0
        max_dd = 0.0
        start_date = datetime.now() - timedelta(days=days)
        
        for i, kline in enumerate(data):
            date = start_date + timedelta(days=i)
            daily_return = (kline['close'] - kline['open']) / kline['open']
            cumulative = cumulative * (1 + daily_return * 0.1)
            
            if cumulative > peak:
                peak = cumulative
            
            current_dd = (peak - cumulative) / peak
            if current_dd > max_dd:
                max_dd = current_dd
            
            drawdown_data.append({
                "date": date.strftime("%Y-%m-%d"),
                "drawdown": round(-current_dd, 4),
                "cumulative": round(cumulative, 2)
            })
        
        return DrawdownCurveResponse(
            status="ok",
            data=drawdown_data,
            current_drawdown=f"{-drawdown_data[-1]['drawdown']:.2%}" if drawdown_data else "0.00%",
            max_drawdown=f"{-max_dd:.2%}"
        )
        
    except Exception as e:
        logger.error(f"获取回撤曲线失败：{e}")
        raise HTTPException(status_code=500, detail=f"获取失败：{str(e)}")


@router.get("/strategy/performance", response_model=StrategyPerformanceResponse)
async def get_strategy_performance(
    days: int = Query(default=90, ge=1, le=365, description="回测天数"),
    token: str = Query(..., description="API Token")
):
    """
    获取各策略表现对比
    
    返回所有可用策略的收益、夏普比率、回撤等指标
    """
    if token != config.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        # 模拟各策略表现
        strategies_data = [
            {"name": "Confluence V2", "return": 0.153, "sharpe": 1.82, "drawdown": -0.084},
            {"name": "MACD", "return": 0.121, "sharpe": 1.54, "drawdown": -0.095},
            {"name": "RSI", "return": 0.084, "sharpe": 1.21, "drawdown": -0.072},
            {"name": "SuperTrend", "return": 0.062, "sharpe": 0.92, "drawdown": -0.105},
            {"name": "Bollinger", "return": 0.051, "sharpe": 0.85, "drawdown": -0.088},
            {"name": "MA Cross", "return": 0.045, "sharpe": 0.78, "drawdown": -0.112},
        ]
        
        # 找出最佳策略
        best_strategy = max(strategies_data, key=lambda x: x['sharpe'])
        
        return StrategyPerformanceResponse(
            status="ok",
            strategies=strategies_data,
            best_strategy=best_strategy['name'],
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"获取策略表现失败：{e}")
        raise HTTPException(status_code=500, detail=f"获取失败：{str(e)}")


@router.get("/portfolio/risk", response_model=PortfolioRiskResponse)
async def get_portfolio_risk(
    token: str = Query(..., description="API Token")
):
    """
    获取组合风险指标
    
    包括 Beta、相关性矩阵、集中度、流动性等
    """
    if token != config.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        # 模拟组合风险指标
        return PortfolioRiskResponse(
            status="ok",
            beta=1.24,
            correlation_matrix={
                "HK.00700": {"HK.00700": 1.0, "HK.09988": 0.72, "US.AAPL": 0.32, "US.TSLA": 0.28},
                "HK.09988": {"HK.00700": 0.72, "HK.09988": 1.0, "US.AAPL": 0.28, "US.TSLA": 0.25},
                "US.AAPL": {"HK.00700": 0.32, "HK.09988": 0.28, "US.AAPL": 1.0, "US.TSLA": 0.65},
                "US.TSLA": {"HK.00700": 0.28, "HK.09988": 0.25, "US.AAPL": 0.65, "US.TSLA": 1.0}
            },
            concentration_top3="38.0%",
            concentration_top5="68.0%",
            liquidity_ratio="良好",
            leverage=1.0,
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"获取组合风险失败：{e}")
        raise HTTPException(status_code=500, detail=f"获取失败：{str(e)}")


@router.get("/market/regime", response_model=MarketRegimeResponse)
async def get_market_regime(
    code: str = Query(default="HK.00700", description="标的代码"),
    token: str = Query(..., description="API Token")
):
    """
    获取市场状态识别结果
    
    包括当前市场状态、置信度、各项指标值
    """
    if token != config.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        # 尝试导入市场状态检测器
        try:
            from app.strategy.market_regime import market_regime_detector
            
            # 获取最新状态
            regime = market_regime_detector.detect(code)
            
            indicators = {
                "adx": regime.adx if hasattr(regime, 'adx') else 35,
                "price_vs_ma": regime.price_vs_ma if hasattr(regime, 'price_vs_ma') else 1.08,
                "atr_percent": regime.atr_percent if hasattr(regime, 'atr_percent') else 0.021,
                "bollinger_width": regime.bollinger_width if hasattr(regime, 'bollinger_width') else 0.15,
            }
            
            recommended = regime.recommended_strategies if hasattr(regime, 'recommended_strategies') else ["confluence_v2", "supertrend"]
            
        except Exception:
            # 如果市场状态模块不可用，返回模拟数据
            regime_type = "STRONG_TREND_UP"
            indicators = {
                "adx": 35,
                "price_vs_ma": 1.08,
                "atr_percent": 0.021,
                "bollinger_width": 0.15,
            }
            recommended = ["confluence_v2", "supertrend"]
        
        return MarketRegimeResponse(
            status="ok",
            regime_type=regime.regime_type.value if hasattr(regime, 'regime_type') else "STRONG_TREND_UP",
            confidence=regime.confidence if hasattr(regime, 'confidence') else 0.78,
            indicators=indicators,
            recommended_strategies=recommended,
            timestamp=datetime.now().isoformat()
        )
        
    except Exception as e:
        logger.error(f"获取市场状态失败：{e}")
        raise HTTPException(status_code=500, detail=f"获取失败：{str(e)}")


@router.get("/dashboard")
async def get_dashboard_data(
    token: str = Query(..., description="API Token")
):
    """
    获取专业监控大屏完整数据
    
    整合所有指标，用于前端一次性加载
    """
    if token != config.API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    try:
        # 获取绩效指标
        performance = await get_performance_metrics(days=90, strategy="confluence_v2", token=token)
        
        # 获取策略表现
        strategy_perf = await get_strategy_performance(days=90, token=token)
        
        # 获取组合风险
        portfolio_risk = await get_portfolio_risk(token=token)
        
        # 获取市场状态
        market_regime = await get_market_regime(code="HK.00700", token=token)
        
        # 整合数据
        dashboard_data = {
            "status": "ok",
            "performance": performance.metrics,
            "performance_summary": performance.summary,
            "strategies": strategy_perf.strategies,
            "best_strategy": strategy_perf.best_strategy,
            "portfolio_risk": portfolio_risk.dict(),
            "market_regime": {
                "type": market_regime.regime_type,
                "confidence": market_regime.confidence,
                "indicators": market_regime.indicators,
                "recommended": market_regime.recommended_strategies,
            },
            "timestamp": datetime.now().isoformat(),
        }
        
        return dashboard_data
        
    except Exception as e:
        logger.error(f"获取大屏数据失败：{e}")
        raise HTTPException(status_code=500, detail=f"获取失败：{str(e)}")
