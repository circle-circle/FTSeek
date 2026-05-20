"""
简化版 Dashboard Web 服务
"""
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from datetime import datetime
from typing import Dict, Any

from app.simple_dashboard import SimpleDashboard
from app.storage.metrics_tables import MetricsTables
from app.observability.metrics_api import MetricsAPI
from app.observability.dingtalk_alert import DingTalkAlerter

logger = logging.getLogger("ftseek_us.dashboard")

dashboard = SimpleDashboard()

# 初始化指标 API
from app.core.paths import get_trade_records_db
db_path = str(get_trade_records_db())
metrics_tables = MetricsTables(db_path)
metrics_api = MetricsAPI(metrics_tables)

# 钉钉告警配置 (从环境变量或配置读取)
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN"
DINGTALK_SECRET = ""
dingtalk_alerter = DingTalkAlerter(DINGTALK_WEBHOOK, DINGTALK_SECRET) if DINGTALK_WEBHOOK else None

def create_app() -> FastAPI:
    """创建 FastAPI 应用"""
    app = FastAPI(
        title="FTSeek-US Dashboard",
        description="美股交易 Dashboard",
        version="1.0.0",
    )
    
    # 配置模板
    base_dir = Path(__file__).parent
    templates_dir = base_dir / "templates"
    templates_dir.mkdir(exist_ok=True)
    
    templates = Jinja2Templates(directory=str(templates_dir))
    
    # 路由
    @app.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request):
        """复盘报告页面"""
        return templates.TemplateResponse("review.html", {
            "request": request,
            "title": "复盘报告 - FTSeek Dashboard",
        })

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request):
        """Dashboard 首页"""
        stats = dashboard.get_stats()
        positions = dashboard.get_positions()
        trades = dashboard.get_trades(limit=20)
        
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "title": "FTSeek-US Dashboard",
            "stats": stats,
            "positions": positions,
            "trades": trades,
            "current_time": datetime.now().isoformat(),
        })
    
    @app.get("/api/stats")
    async def get_stats():
        """获取统计信息"""
        return dashboard.get_stats()
    
    @app.get("/api/positions")
    async def get_positions():
        """获取持仓列表"""
        return {"positions": dashboard.get_positions()}
    
    @app.get("/api/trades")
    async def get_trades(limit: int = 50):
        """获取交易记录"""
        return {"trades": dashboard.get_trades(limit)}
    
    @app.get("/api/account")
    async def get_account():
        """获取账户信息"""
        return dashboard.get_account_summary()
    
    @app.get("/health", response_class=HTMLResponse)
    async def health_page(request: Request):
        """健康监控页面"""
        return templates.TemplateResponse("health.html", {
            "request": request,
            "title": "系统健康监控 - FTSeek-US",
        })
    
    @app.get("/api/metrics/system")
    async def get_system_metrics():
        """获取系统指标"""
        try:
            status = metrics_api.get_latest_system_status()
            return {
                "cpu_percent": status.get("cpu_percent", {}).get("value", 0),
                "memory_percent": status.get("memory_percent", {}).get("value", 0),
                "disk_percent": status.get("disk_percent", {}).get("value", 0),
            }
        except Exception as e:
            logger.error(f"获取系统指标失败：{e}")
            return {"cpu_percent": 0, "memory_percent": 0, "disk_percent": 0}
    
    @app.get("/api/metrics/trading")
    async def get_trading_metrics():
        """获取交易指标"""
        try:
            status = metrics_api.get_latest_trading_status()
            return {
                "signals_count": int(status.get("signals_count", {}).get("value", 0)),
                "orders_count": int(status.get("orders_count", {}).get("value", 0)),
                "fills_count": int(status.get("fills_count", {}).get("value", 0)),
                "pending_orders": int(status.get("pending_orders", {}).get("value", 0)),
            }
        except Exception as e:
            logger.error(f"获取交易指标失败：{e}")
            return {"signals_count": 0, "orders_count": 0, "fills_count": 0, "pending_orders": 0}
    
    @app.get("/api/metrics/api")
    async def get_api_metrics():
        """获取 API 调用指标"""
        try:
            # 从数据源指标中获取
            status = metrics_api.get_latest_system_status()
            # 默认返回模拟数据 (实际应从 metrics 表读取)
            return {
                "total_requests": int(status.get("opendata_total", {}).get("value", 0)),
                "success_requests": int(status.get("opendata_success", {}).get("value", 0)),
                "failed_requests": int(status.get("opendata_failed", {}).get("value", 0)),
                "avg_latency_ms": status.get("opendata_latency", {}).get("value", 0),
            }
        except Exception as e:
            logger.error(f"获取 API 指标失败：{e}")
            return {"total_requests": 0, "success_requests": 0, "failed_requests": 0, "avg_latency_ms": 0}
    
    @app.get("/api/metrics/alert")
    async def check_and_send_alerts():
        """检查指标并发送告警"""
        try:
            alerts_sent = []
            
            # 获取系统指标
            system_status = metrics_api.get_latest_system_status()
            cpu = system_status.get("cpu_percent", {}).get("value", 0)
            memory = system_status.get("memory_percent", {}).get("value", 0)
            
            # 检查 CPU
            if cpu > 90:
                if dingtalk_alerter:
                    dingtalk_alerter.send_alert(
                        level="CRITICAL",
                        title="⚠️ CPU 使用率告警",
                        content=f"CPU 使用率达到 {cpu:.1f}%，超过阈值 90%"
                    )
                alerts_sent.append("CPU_CRITICAL")
            elif cpu > 70:
                if dingtalk_alerter:
                    dingtalk_alerter.send_alert(
                        level="WARN",
                        title="⚡ CPU 使用率警告",
                        content=f"CPU 使用率达到 {cpu:.1f}%，超过阈值 70%"
                    )
                alerts_sent.append("CPU_WARN")
            
            # 检查内存
            if memory > 90:
                if dingtalk_alerter:
                    dingtalk_alerter.send_alert(
                        level="CRITICAL",
                        title="⚠️ 内存使用率告警",
                        content=f"内存使用率达到 {memory:.1f}%，超过阈值 90%"
                    )
                alerts_sent.append("MEMORY_CRITICAL")
            elif memory > 70:
                if dingtalk_alerter:
                    dingtalk_alerter.send_alert(
                        level="WARN",
                        title="⚡ 内存使用率警告",
                        content=f"内存使用率达到 {memory:.1f}%，超过阈值 70%"
                    )
                alerts_sent.append("MEMORY_WARN")
            
            return {
                "status": "checked",
                "alerts_sent": alerts_sent,
                "timestamp": datetime.now().isoformat(),
            }
        except Exception as e:
            logger.error(f"告警检查失败：{e}")
            return {"status": "error", "error": str(e)}
    
    @app.get("/health")
    async def health_check_api():
        """健康检查 API"""
        return {"status": "ok", "timestamp": datetime.now().isoformat()}
    
    logger.info("简化版 Dashboard Web 服务已创建")
    
    return app
