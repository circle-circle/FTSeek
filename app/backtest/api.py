"""
回测 API 模块

P1 修复 (2026-04-12): 占位模块
注：完整实现需要集成 app/backtest/optimizer.py
"""
from fastapi import APIRouter

router = APIRouter(prefix="/backtest", tags=["backtest"])

@router.get("/")
async def get_backtest_status():
    return {"status": "backtest API placeholder"}