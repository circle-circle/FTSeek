"""
实时风险监控 API 接口
"""
import logging
from fastapi import APIRouter, Query, HTTPException
from typing import Optional

from app import config
from app.risk import risk_manager

# 延迟导入，避免循环依赖
def get_realtime_monitor():
    from app.risk.realtime_monitor import realtime_monitor
    return realtime_monitor

logger = logging.getLogger("ftseek.risk.api")
logger.propagate = False  # 防止日志重复

router = APIRouter(prefix="/api/risk", tags=["risk"])


def _check_token(token: str):
    """Token 校验"""
    import secrets
    if not secrets.compare_digest(token, config.API_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")


@router.get("/dashboard")
async def get_risk_dashboard(token: str = Query(...)):
    """
    获取风险仪表盘
    
    返回：
    - 风险评分 (0-100)
    - 风险等级 (LOW/MEDIUM/HIGH/CRITICAL)
    - 核心指标
    - 持仓风险
    - 最近警报
    """
    _check_token(token)
    
    monitor = get_realtime_monitor()
    if monitor is None:
        return {
            "status": "error",
            "message": "风险监控器未初始化",
        }
    
    dashboard = monitor.get_risk_dashboard()
    
    return {
        "status": "ok",
        "dashboard": dashboard,
    }


@router.get("/alerts")
async def get_risk_alerts(
    limit: int = Query(20, description="返回警报数量"),
    token: str = Query(...),
):
    """获取最近风险警报"""
    _check_token(token)
    
    monitor = get_realtime_monitor()
    if monitor is None:
        return {"status": "error", "message": "风险监控器未初始化"}
    
    return {"status": "ok", "alerts": monitor.get_alerts(limit)}


@router.get("/positions")
async def get_position_risks(token: str = Query(...)):
    """获取持仓风险"""
    _check_token(token)
    
    monitor = get_realtime_monitor()
    if monitor is None:
        return {"status": "error", "message": "风险监控器未初始化"}
    
    return {"status": "ok", "positions": monitor.get_position_risks()}


@router.get("/report")
async def export_risk_report(token: str = Query(...)):
    """导出风险报告"""
    _check_token(token)
    
    monitor = get_realtime_monitor()
    if monitor is None:
        return {"status": "error", "message": "风险监控器未初始化"}
    
    return {"status": "ok", "report": monitor.export_report()}


@router.get("/status")
async def get_monitor_status(token: str = Query(...)):
    """获取监控器状态"""
    _check_token(token)
    
    monitor = get_realtime_monitor()
    if monitor is None:
        return {"status": "ok", "running": False, "message": "监控器未初始化"}
    
    return {
        "status": "ok",
        "running": monitor._running,
        "check_interval": monitor.check_interval,
        "total_alerts": len(monitor._alerts),
    }
