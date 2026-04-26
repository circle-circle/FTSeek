"""
认证 API 模块

P1 修复 (2026-04-12): 占位模块
注：完整实现需要用户认证系统（JWT/OAuth）
"""
from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/")
async def get_auth_status():
    return {"status": "auth API placeholder"}