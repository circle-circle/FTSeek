"""盘前 Pipeline API 路由

提供手动触发、查看评估结果、审批/拒绝等接口。

Endpoints:
  GET  /api/v2/premarket/status       - 调度器状态
  GET  /api/v2/premarket/assessments  - 今日评估列表
  GET  /api/v2/premarket/assessments/{id} - 单个评估详情
  POST /api/v2/premarket/run          - 手动触发 Pipeline
  POST /api/v2/premarket/assessments/{id}/approve - 审批确认
  POST /api/v2/premarket/assessments/{id}/reject  - 拒绝
"""

import logging
import time
from typing import Optional
from fastapi import APIRouter, HTTPException

logger = logging.getLogger("ftseek.api.premarket")

router = APIRouter(prefix="/api/v2/premarket", tags=["盘前Pipeline"])

# 内存存储待确认的评估结果
_pending_reviews: dict = {}  # assessment_id -> assessment_data


@router.get("/status")
async def premarket_status():
    """获取调度器状态"""
    try:
        from app.main import premarket_scheduler
        if premarket_scheduler is None:
            return {"enabled": False, "message": "调度器未初始化"}
        return premarket_scheduler.get_status()
    except Exception as e:
        logger.error(f"获取调度器状态失败: {e}")
        return {"enabled": False, "error": str(e)}


@router.get("/assessments")
async def list_assessments(limit: int = 20):
    """获取最近的评估结果列表"""
    try:
        from app.main import premarket_scheduler
        if premarket_scheduler is None:
            return {"assessments": [], "message": "调度器未初始化"}

        result = premarket_scheduler.get_last_result()
        if result is None:
            return {"assessments": [], "message": "暂无执行结果"}

        assessments = []
        for a in getattr(result, "assessments", [])[:limit]:
            assessments.append(a.to_dict() if hasattr(a, "to_dict") else a)

        return {
            "trace_id": getattr(result, "trace_id", ""),
            "assessment_count": len(assessments),
            "assessments": assessments,
        }
    except Exception as e:
        logger.error(f"获取评估列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/assessments/{assessment_id}")
async def get_assessment_detail(assessment_id: str):
    """获取单个评估详情"""
    try:
        from app.main import premarket_scheduler
        if premarket_scheduler is None:
            raise HTTPException(status_code=404, detail="调度器未初始化")

        result = premarket_scheduler.get_last_result()
        if result is None:
            raise HTTPException(status_code=404, detail="暂无执行结果")

        for a in getattr(result, "assessments", []):
            a_id = getattr(a, "event_pack_id", "")
            if a_id == assessment_id or getattr(a, "id", "") == assessment_id:
                return a.to_dict() if hasattr(a, "to_dict") else a

        # 检查待确认列表
        if assessment_id in _pending_reviews:
            return _pending_reviews[assessment_id]

        raise HTTPException(status_code=404, detail=f"评估 {assessment_id} 未找到")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取评估详情失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run")
async def trigger_premarket_pipeline():
    """手动触发盘前 Pipeline 执行"""
    try:
        from app.main import premarket_scheduler
        if premarket_scheduler is None:
            raise HTTPException(status_code=503, detail="调度器未初始化")

        result = premarket_scheduler.run_once()
        if result is None:
            return {"status": "error", "message": "执行失败，请查看日志"}

        return {
            "status": "success",
            "trace_id": getattr(result, "trace_id", ""),
            "duration_seconds": getattr(result, "duration_seconds", 0),
            "summary": result.to_dict() if hasattr(result, "to_dict") else {},
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"手动触发 Pipeline 失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/assessments/{assessment_id}/approve")
async def approve_assessment(assessment_id: str):
    """审批确认执行 (人工确认通道)"""
    try:
        if assessment_id not in _pending_reviews:
            raise HTTPException(
                status_code=404, detail=f"待确认评估 {assessment_id} 未找到"
            )

        assessment_data = _pending_reviews.pop(assessment_id)
        logger.info(f"[盘前API] 评估已审批: {assessment_id}")

        # 转换为 Signal 并发送到 DecisionBuffer
        from app.pipeline.strategy_router import PreMarketSignalConverter
        from app.pipeline.premarket_analyst import PreMarketAssessment

        # 从 dict 重建 Assessment 对象 (简化处理)
        assessment = PreMarketAssessment(**assessment_data)
        converter = PreMarketSignalConverter()
        signal = converter.convert(assessment)

        if signal:
            from app.signal_store import signal_store
            success = signal_store.add(signal)
            if success:
                return {
                    "status": "approved",
                    "signal_id": signal.id,
                    "message": "信号已生成并入库",
                }
            else:
                return {
                    "status": "approved",
                    "message": "信号被冷却拦截",
                }
        else:
            return {
                "status": "approved",
                "message": "方向为观望，不生成交易信号",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"审批评估失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/assessments/{assessment_id}/reject")
async def reject_assessment(assessment_id: str):
    """拒绝执行 (人工否决通道)"""
    try:
        if assessment_id in _pending_reviews:
            assessment_data = _pending_reviews.pop(assessment_id)
            logger.info(
                f"[盘前API] 评估已拒绝: {assessment_id} "
                f"direction={assessment_data.get('direction', '?')}"
            )

        return {"status": "rejected", "message": "评估已丢弃"}

    except Exception as e:
        logger.error(f"拒绝评估失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
