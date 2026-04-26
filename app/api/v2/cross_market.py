#!/usr/bin/env python3
"""
跨市场套利 API 路由

提供:
1. 否决信号接口
2. 今日信号查询
3. 调度器状态查询
"""

import logging
from typing import Optional
from fastapi import APIRouter, Query, HTTPException

logger = logging.getLogger("ftseek.api.cross_market")

router = APIRouter(prefix="/api/v2/cross_market", tags=["Cross Market Arbitrage"])


@router.post("/reject")
async def reject_cross_market_signal(
    signal_id: str = Query(..., description="信号ID"),
    token: str = Query(..., description="鉴权Token"),
    user_id: str = Query(None, description="操作人"),
    comment: str = Query("", description="否决理由"),
):
    """
    否决跨市场套利信号 (阻止自动执行)
    
    调用方式:
    - 钉钉卡片按钮点击
    - 手动调用 API
    
    返回示例:
    {
        "status": "success",
        "message": "信号 xxx 已阻止执行"
    }
    """
    try:
        from app.strategy.cross_market_scheduler import get_scheduler
        from app.config import API_TOKEN
        
        # 验证 Token
        if token != API_TOKEN:
            raise HTTPException(status_code=401, detail="Token 无效")
        
        scheduler = get_scheduler()
        if not scheduler:
            raise HTTPException(status_code=503, detail="调度器未启动")
        
        success = scheduler.handle_reject(
            signal_id=signal_id,
            user_id=user_id,
            comment=comment,
        )
        
        if not success:
            raise HTTPException(status_code=400, detail="否决失败 (信号已执行或不存在)")
        
        return {
            "status": "success",
            "message": f"信号 {signal_id} 已阻止执行",
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"否决信号失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/today_signals")
async def get_today_signals(
    token: str = Query(..., description="鉴权Token"),
):
    """
    获取今日跨市场信号记录
    
    返回示例:
    {
        "signals": [
            {
                "signal_id": "xxx",
                "code": "00700.HK",
                "action": "BUY",
                "status": "auto_executed",
                "score": 0.82,
                "executed": true,
                "review_comment": ""
            }
        ]
    }
    """
    try:
        from app.strategy.cross_market_scheduler import get_scheduler
        from app.config import API_TOKEN
        
        # 验证 Token
        if token != API_TOKEN:
            raise HTTPException(status_code=401, detail="Token 无效")
        
        scheduler = get_scheduler()
        if not scheduler:
            return {"signals": []}
        
        signals = scheduler.get_today_signals()
        
        return {
            "signals": [
                {
                    "signal_id": s.signal_id,
                    "code": s.code,
                    "action": s.action,
                    "status": s.review_status,
                    "score": s.metadata.get('arb_total_score', 0),
                    "executed": s.executed,
                    "review_comment": s.review_comment,
                }
                for s in signals
            ]
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取信号失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scheduler_status")
async def get_scheduler_status(
    token: str = Query(..., description="鉴权Token"),
):
    """
    获取调度器状态
    
    返回示例:
    {
        "running": true,
        "signal_count": 3,
        "transition_stats": {
            "current_phase": "arb_dominant",
            "arb_signals_count": 3,
            "arb_executed_count": 2
        }
    }
    """
    try:
        from app.strategy.cross_market_scheduler import get_scheduler
        from app.config import API_TOKEN
        
        # 验证 Token
        if token != API_TOKEN:
            raise HTTPException(status_code=401, detail="Token 无效")
        
        scheduler = get_scheduler()
        if not scheduler:
            return {"running": False}
        
        stats = scheduler.transition_mgr.get_stats()
        
        return {
            "running": scheduler._running,
            "signal_count": len(scheduler._signal_records),
            "transition_stats": stats,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取调度器状态失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
