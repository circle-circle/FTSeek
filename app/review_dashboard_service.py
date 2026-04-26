"""
复盘 Dashboard 独立服务
与 FTSeek 主交易服务隔离，避免 WebSocket 错误影响交易日志
"""
import asyncio
from app.storage.repository import get_db
import logging
from pathlib import Path
from fastapi import FastAPI, WebSocket, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Dict, Any, List, Optional
from datetime import datetime
import sqlite3
import json

# 独立日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ReviewDashboard] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler('/opt/ftseek/FTSeek/logs/review_dashboard.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="FTSeek 复盘 Dashboard", version="2.0")

# 数据库路径
DB_PATH = Path("/opt/ftseek/FTSeek/data/ftseek.db")
SIMULATION_DB_PATH = Path("/opt/ftseek/FTSeek/data/simulation.db")


def get_db_connection(db_path: Path):
    """获取数据库连接"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok", "service": "review-dashboard"}


@app.get("/api/summary")
async def get_summary(date: str = None):
    """获取交易摘要"""
    try:
        conn = get_db_connection(DB_PATH)
        cursor = conn.cursor()
        
        target_date = date or datetime.now().strftime('%Y-%m-%d')
        
        cursor.execute("""
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                COALESCE(SUM(pnl), 0) as total_pnl
            FROM trades
            WHERE date(entry_time) = ?
        """, (target_date,))
        
        row = cursor.fetchone()
        conn.close()
        
        return {
            "status": "ok",
            "data": dict(row) if row else {}
        }
    except Exception as e:
        logger.error(f"获取摘要失败：{e}")
        return {"status": "error", "message": str(e)}


@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket, task_id: str = Query(...)):
    """
    WebSocket 进度推送
    
    独立日志，不影响主交易服务
    """
    try:
        await websocket.accept()
        logger.info(f"WebSocket 连接：task_id={task_id}")
        
        # 模拟进度推送
        stages = [
            ("init", "初始化", 500),
            ("analyze", "分析中", 1000),
            ("review", "复盘中", 1500),
            ("complete", "完成", 200),
        ]
        
        for stage_id, stage_name, duration_ms in stages:
            try:
                await websocket.send_json({
                    "event": "progress_updated",
                    "task_id": task_id,
                    "stage": stage_id,
                    "progress": 50,
                })
                await asyncio.sleep(duration_ms / 1000)
            except Exception as e:
                logger.warning(f"发送进度失败：{e}")
                break
        
        # 完成
        try:
            await websocket.send_json({
                "event": "review_completed",
                "task_id": task_id,
            })
        except Exception as e:
            logger.debug(f"[WebSocket] 发送 review_completed 事件失败 (WebSocket 可能已断开): {e}")
            
    except Exception as e:
        logger.error(f"WebSocket 异常：{e}", exc_info=True)
    finally:
        # 安全关闭，不影响主服务
        try:
            if websocket.client_state.name not in ['DISCONNECTED', 'CLOSED']:
                await websocket.close()
        except Exception as e:
            logger.debug(f"关闭 WebSocket 异常 (可忽略): {e}")


@app.on_event("startup")
async def startup_event():
    """服务启动"""
    logger.info("=" * 50)
    logger.info("复盘 Dashboard 服务启动")
    logger.info(f"数据库：{DB_PATH}")
    logger.info("独立日志文件：/opt/ftseek/FTSeek/logs/review_dashboard.log")
    logger.info("=" * 50)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8898)
