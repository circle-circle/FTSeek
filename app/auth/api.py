"""
认证 API 模块（占位）
TODO: 实现认证 API
"""
from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/")
async def get_auth_status():
    return {"status": "auth API placeholder"}