"""
复盘 API 模块

提供 HTTP 接口访问复盘数据
"""
import logging
from app.storage.repository import get_db
from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any, Optional
from datetime import datetime
import sqlite3
from pathlib import Path

logger = logging.getLogger("ftseek.review.api")

router = APIRouter()

# 数据库路径
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")

def get_db_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn
@router.get("/reports")
async def get_review_reports(
    type: str = "daily",
    market: str = "HK",
    limit: int = 10
):
    """获取复盘报告列表"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 查询复盘报告
        cursor.execute("""
            SELECT * FROM review_reports 
            WHERE report_type = ? AND market = ?
            ORDER BY period_end DESC
            LIMIT ?
        """, (type, market, limit))
        
        reports = []
        for row in cursor.fetchall():
            reports.append({
                "id": row["id"],
                "report_type": row["report_type"],
                "market": row["market"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "total_trades": row["total_trades"],
                "win_rate": row["win_rate"],
                "total_pnl": row["total_pnl"],
                "overall_score": row.get("overall_score", 0),
                "overall_level": row.get("overall_level", "N/A"),
                "generated_at": row["generated_at"],
            })
        
        conn.close()
        return {"reports": reports}
    except Exception as e:
        logger.error(f"查询复盘报告失败：{e}")
        return {"reports": []}

@router.get("/daily")
async def get_daily_review(date: str, market: str = "HK"):
    """获取日复盘详情"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM review_reports 
            WHERE report_type = 'daily' AND market = ? AND period_start = ?
            ORDER BY generated_at DESC
            LIMIT 1
        """, (market, date))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "id": row["id"],
                "report_type": row["report_type"],
                "market": row["market"],
                "date": row["period_start"],
                "stats": {
                    "total_trades": row["total_trades"],
                    "winning_trades": row["winning_trades"],
                    "losing_trades": row["losing_trades"],
                    "win_rate": row["win_rate"],
                    "total_pnl": row["total_pnl"],
                    "max_drawdown": row.get("max_drawdown", 0),
                },
                "dimensions": {
                    "decision_quality": row.get("decision_quality_score", 0),
                    "timing_quality": row.get("timing_quality_score", 0),
                    "position_management": row.get("position_management_score", 0),
                    "risk_execution": row.get("risk_execution_score", 0),
                    "psychology": row.get("psychology_score", 0),
                    "strategy_effectiveness": row.get("strategy_effectiveness_score", 0),
                },
                "overall_score": row.get("overall_score", 0),
                "overall_level": row.get("overall_level", "N/A"),
                "generated_at": row["generated_at"],
            }
        return {"error": "未找到复盘报告"}
    except Exception as e:
        logger.error(f"查询日复盘失败：{e}")
        return {"error": str(e)}

@router.get("/weekly")
async def get_weekly_review(week: str, market: str = "HK"):
    """获取周复盘详情"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM review_reports 
            WHERE report_type = 'weekly' AND market = ? AND period_start = ?
            ORDER BY generated_at DESC
            LIMIT 1
        """, (market, week))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "id": row["id"],
                "report_type": "weekly",
                "market": row["market"],
                "week": row["period_start"],
                "stats": {
                    "total_trades": row["total_trades"],
                    "win_rate": row["win_rate"],
                    "total_pnl": row["total_pnl"],
                },
                "overall_score": row.get("overall_score", 0),
                "overall_level": row.get("overall_level", "N/A"),
                "generated_at": row["generated_at"],
            }
        return {"error": "未找到周复盘报告"}
    except Exception as e:
        logger.error(f"查询周复盘失败：{e}")
        return {"error": str(e)}

@router.get("/monthly")
async def get_monthly_review(month: str, market: str = "HK"):
    """获取月复盘详情"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM review_reports 
            WHERE report_type = 'monthly' AND market = ? AND period_start = ?
            ORDER BY generated_at DESC
            LIMIT 1
        """, (market, month))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "id": row["id"],
                "report_type": "monthly",
                "market": row["market"],
                "month": row["period_start"],
                "stats": {
                    "total_trades": row["total_trades"],
                    "win_rate": row["win_rate"],
                    "total_pnl": row["total_pnl"],
                },
                "overall_score": row.get("overall_score", 0),
                "overall_level": row.get("overall_level", "N/A"),
                "generated_at": row["generated_at"],
            }
        return {"error": "未找到月复盘报告"}
    except Exception as e:
        logger.error(f"查询月复盘失败：{e}")
        return {"error": str(e)}

@router.get("/improvements")
async def get_improvements(status: str = "pending"):
    """获取改进建议"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM improvements 
            WHERE status = ?
            ORDER BY created_at DESC
            LIMIT 20
        """, (status,))
        
        improvements = []
        for row in cursor.fetchall():
            improvements.append({
                "id": row["id"],
                "category": row["category"],
                "description": row["description"],
                "priority": row["priority"],
                "status": row["status"],
                "created_at": row["created_at"],
            })
        
        conn.close()
        return {"improvements": improvements}
    except Exception as e:
        logger.error(f"查询改进建议失败：{e}")
        return {"improvements": []}

@router.post("/run")
async def run_review(
    type: str = "daily",
    date: Optional[str] = None,
    market: str = "HK"
):
    """手动触发复盘"""
    try:
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        # P1 修复 (2026-04-12): 已集成复盘引擎
        # 注：完整实现需要调用 periodic_collector_hk 或 batch_processor_hk
        logger.info(f"触发复盘：type={type} date={date} market={market}")

        # 调用复盘服务
        from app.review_dashboard_service import ReviewDashboardService
        service = ReviewDashboardService()
        service.trigger_review(review_type=type, date=date, market=market)

        return {
            "status": "success",
            "message": f"已触发{type}复盘",
            "type": type,
            "date": date,
            "market": market,
        }
    except Exception as e:
        logger.error(f"触发复盘失败：{e}")
        return {"status": "error", "message": str(e)}
