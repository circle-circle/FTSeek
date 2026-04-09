"""
回测 API 模块（占位）
TODO: 实现完整的回测 API
"""
from fastapi import APIRouter

router = APIRouter(prefix="/backtest", tags=["backtest"])

@router.get("/")
async def get_backtest_status():
    return {"status": "backtest API placeholder"}