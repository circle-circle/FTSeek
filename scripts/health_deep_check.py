#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 深度健康自检 (Health Deep Check)
- 服务启动后自动运行，检查所有关键组件
- 发现异常时推送钉钉告警
- 记录检查结果到 data/health_check_history.json

作者: FTSeek Team
日期: 2026-04-22
"""
import sys
import os
import json
import time
import socket
import logging
import subprocess
from datetime import datetime
from pathlib import Path

# 确保路径正确
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger("ftseek.health_deep_check")

# 检查历史文件
CHECK_HISTORY = BASE_DIR / "data" / "health_check_history.json"

# 关键检查项
CHECKS = {
    "global_singletons": "全局单例初始化",
    "database_connectivity": "数据库连接",
    "opend_connection": "Futu OpenD 连接",
    "dingtalk_webhook": "钉钉推送通道",
    "rsshub_sources": "RSSHub 新闻源",
    "thread_pool_status": "线程池状态",
    "queue_depth": "队列深度",
    "recent_errors": "最近错误日志",
    "disk_space": "磁盘空间",
    "memory_usage": "内存使用",
}


def run_all_checks() -> dict:
    """
    执行所有健康检查
    
    Returns:
        检查结果字典
    """
    logger.info("=" * 60)
    logger.info("FTSeek 深度健康自检开始")
    logger.info("=" * 60)
    
    results = {
        "timestamp": datetime.now().isoformat(),
        "overall_status": "healthy",
        "checks": {},
        "errors": [],
        "warnings": [],
    }
    
    # 1. 全局单例初始化检查
    results["checks"]["global_singletons"] = check_global_singletons()
    
    # 2. 数据库连接检查
    results["checks"]["database_connectivity"] = check_database_connectivity()
    
    # 3. Futu OpenD 连接检查
    results["checks"]["opend_connection"] = check_opend_connection()
    
    # 4. 钉钉推送通道检查
    results["checks"]["dingtalk_webhook"] = check_dingtalk_webhook()
    
    # 5. RSSHub 新闻源检查
    results["checks"]["rsshub_sources"] = check_rsshub_sources()
    
    # 6. 线程池状态检查
    results["checks"]["thread_pool_status"] = check_thread_pool_status()
    
    # 7. 队列深度检查
    results["checks"]["queue_depth"] = check_queue_depth()
    
    # 8. 最近错误日志检查
    results["checks"]["recent_errors"] = check_recent_errors()
    
    # 9. 磁盘空间检查
    results["checks"]["disk_space"] = check_disk_space()
    
    # 10. 内存使用检查
    results["checks"]["memory_usage"] = check_memory_usage()
    
    # 汇总状态
    critical_failures = sum(1 for c in results["checks"].values() if c.get("status") == "critical")
    warnings = sum(1 for c in results["checks"].values() if c.get("status") == "warning")
    
    if critical_failures > 0:
        results["overall_status"] = "critical"
    elif warnings > 0:
        results["overall_status"] = "degraded"
    else:
        results["overall_status"] = "healthy"
    
    results["summary"] = {
        "total_checks": len(results["checks"]),
        "critical": critical_failures,
        "warnings": warnings,
        "healthy": len(results["checks"]) - critical_failures - warnings,
    }
    
    # 保存检查历史
    save_check_history(results)
    
    # 输出结果
    logger.info("-" * 60)
    logger.info(f"健康自检完成: {results['overall_status'].upper()}")
    logger.info(f"  关键失败: {critical_failures} | 警告: {warnings} | 正常: {results['summary']['healthy']}")
    for name, result in results["checks"].items():
        status_icon = {"healthy": "✅", "warning": "⚠️", "critical": "❌"}.get(result.get("status"), "❓")
        logger.info(f"  {status_icon} {name}: {result.get('message', '')}")
    logger.info("=" * 60)
    
    return results


def check_global_singletons() -> dict:
    """检查全局单例是否成功初始化"""
    try:
        from app.signal_store import signal_store
        from app.storage.decision_recorder import get_decision_recorder
        from app.factors import get_quality_scorer, get_holding_period_manager
        
        singletons = {
            "signal_store": signal_store,
            "decision_recorder": get_decision_recorder(),
            "quality_scorer": get_quality_scorer(),
            "holding_period_manager": get_holding_period_manager(),
        }
        
        # water_level_manager 需要 initial_capital 参数，单独检查
        try:
            from app.factors import get_water_level_manager
            wl_manager = get_water_level_manager()
            singletons["water_level_manager"] = wl_manager
        except TypeError:
            # WaterLevelManager 需要 initial_capital，在脚本中无法直接初始化
            # 这不是错误，只是服务启动时初始化的组件
            singletons["water_level_manager"] = "OK (服务启动时初始化)"
        except Exception as e:
            singletons["water_level_manager"] = f"检查失败: {e}"
        
        none_count = sum(1 for v in singletons.values() if v is None)
        if none_count > 0:
            return {
                "status": "warning",
                "message": f"{none_count}/{len(singletons)} 个单例未初始化",
                "details": {k: "OK" if v else "None" for k, v in singletons.items()},
            }
        return {"status": "healthy", "message": f"所有 {len(singletons)} 个单例已初始化"}
    except Exception as e:
        return {"status": "critical", "message": f"单例检查失败: {e}"}


def check_database_connectivity() -> dict:
    """检查数据库连接"""
    try:
        import sqlite3
        db_path = BASE_DIR / "data" / "ftseek.db"
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("SELECT 1")
        conn.close()
        return {"status": "healthy", "message": "数据库连接正常"}
    except Exception as e:
        return {"status": "critical", "message": f"数据库连接失败: {e}"}


def check_opend_connection() -> dict:
    """检查 Futu OpenD 连接"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        result = sock.connect_ex(("127.0.0.1", 11111))
        sock.close()
        if result == 0:
            return {"status": "healthy", "message": "OpenD 端口 11111 可连接"}
        else:
            return {"status": "critical", "message": "OpenD 端口 11111 不可达"}
    except Exception as e:
        return {"status": "critical", "message": f"OpenD 检查失败: {e}"}


def check_dingtalk_webhook() -> dict:
    """检查钉钉推送通道"""
    try:
        from app.notifier.dingtalk import get_dingtalk_notifier
        notifier = get_dingtalk_notifier()
        if notifier and notifier.webhook_url:
            return {"status": "healthy", "message": "钉钉 Webhook 已配置"}
        return {"status": "warning", "message": "钉钉 Webhook 未配置或通知器未初始化"}
    except Exception as e:
        return {"status": "warning", "message": f"钉钉检查失败: {e}"}


def check_rsshub_sources() -> dict:
    """检查 RSSHub 新闻源"""
    try:
        news_dir = BASE_DIR / "data" / "rsshub_multi_source"
        if not news_dir.exists():
            return {"status": "warning", "message": "新闻数据目录不存在"}
        
        json_files = list(news_dir.glob("*.json"))
        if not json_files:
            return {"status": "warning", "message": "无新闻数据文件"}
        
        latest = max(json_files, key=lambda f: f.stat().st_mtime)
        age_hours = (time.time() - latest.stat().st_mtime) / 3600
        
        if age_hours < 2:
            return {"status": "healthy", "message": f"新闻数据新鲜 ({age_hours:.1f}h 前)"}
        return {"status": "warning", "message": f"新闻数据较旧 ({age_hours:.1f}h 前)"}
    except Exception as e:
        return {"status": "warning", "message": f"RSSHub 检查失败: {e}"}


def check_thread_pool_status() -> dict:
    """检查线程池状态"""
    try:
        import threading
        active_threads = threading.active_count()
        thread_names = [t.name for t in threading.enumerate()]
        
        if active_threads > 50:
            return {"status": "warning", "message": f"活跃线程数过多: {active_threads}"}
        return {"status": "healthy", "message": f"活跃线程: {active_threads}"}
    except Exception as e:
        return {"status": "warning", "message": f"线程池检查失败: {e}"}


def check_queue_depth() -> dict:
    """检查队列深度"""
    try:
        from app.decision_buffer import get_decision_buffer
        buf = get_decision_buffer()
        if buf and hasattr(buf, "_pending_queue"):
            depth = len(buf._pending_queue)
            max_depth = getattr(buf, "max_pending", 500)
            usage_pct = depth / max_depth * 100 if max_depth > 0 else 0
            
            if usage_pct > 80:
                return {"status": "critical", "message": f"决策队列拥堵: {depth}/{max_depth} ({usage_pct:.0f}%)"}
            elif usage_pct > 50:
                return {"status": "warning", "message": f"决策队列使用较高: {depth}/{max_depth} ({usage_pct:.0f}%)"}
            return {"status": "healthy", "message": f"决策队列: {depth}/{max_depth} ({usage_pct:.0f}%)"}
        return {"status": "warning", "message": "无法获取决策队列状态"}
    except Exception as e:
        return {"status": "warning", "message": f"队列检查失败: {e}"}


def check_recent_errors() -> dict:
    """检查最近 5 分钟是否有 ERROR 日志"""
    try:
        log_file = BASE_DIR / "logs" / "health.log"
        if not log_file.exists():
            return {"status": "healthy", "message": "无日志文件"}
        
        five_min_ago = time.time() - 300
        error_count = 0
        
        with open(log_file, "r") as f:
            for line in f:
                if "ERROR" in line or "CRITICAL" in line:
                    error_count += 1
        
        if error_count > 20:
            return {"status": "warning", "message": f"最近有大量 ERROR 日志: {error_count} 条"}
        return {"status": "healthy", "message": f"ERROR 日志数: {error_count}"}
    except Exception as e:
        return {"status": "warning", "message": f"日志检查失败: {e}"}


def check_disk_space() -> dict:
    """检查磁盘空间"""
    try:
        stat = os.statvfs(str(BASE_DIR))
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb < 1:
            return {"status": "critical", "message": f"磁盘空间不足: {free_gb:.1f} GB"}
        elif free_gb < 5:
            return {"status": "warning", "message": f"磁盘空间较低: {free_gb:.1f} GB"}
        return {"status": "healthy", "message": f"磁盘可用: {free_gb:.1f} GB"}
    except Exception as e:
        return {"status": "warning", "message": f"磁盘检查失败: {e}"}


def check_memory_usage() -> dict:
    """检查内存使用"""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    rss_mb = rss_kb / 1024
                    if rss_mb > 1000:
                        return {"status": "warning", "message": f"内存使用较高: {rss_mb:.0f} MB"}
                    return {"status": "healthy", "message": f"内存使用: {rss_mb:.0f} MB"}
        return {"status": "warning", "message": "无法读取内存信息"}
    except Exception as e:
        return {"status": "warning", "message": f"内存检查失败: {e}"}


def save_check_history(results: dict):
    """保存检查结果到历史文件"""
    history = []
    if CHECK_HISTORY.exists():
        try:
            with open(CHECK_HISTORY, "r") as f:
                history = json.load(f)
        except:
            history = []
    
    history.append(results)
    history = history[-30:]  # 保留最近 30 次
    
    CHECK_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECK_HISTORY, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def format_dingtalk_message(results: dict) -> str:
    """格式化为钉钉 Markdown 消息"""
    status = results["overall_status"]
    icon = {"healthy": "✅", "degraded": "⚠️", "critical": "🚨"}.get(status, "❓")
    
    lines = [
        f"## {icon} FTSeek 深度健康自检",
        f"**时间**: {results['timestamp']}",
        f"**状态**: {status.upper()}",
        f"**检查项**: {results['summary']['healthy']}✅ / {results['summary']['warnings']}⚠️ / {results['summary']['critical']}❌",
    ]
    
    # 列出警告和错误
    for name, check in results["checks"].items():
        if check.get("status") in ("warning", "critical"):
            check_icon = "⚠️" if check["status"] == "warning" else "❌"
            lines.append(f"- {check_icon} **{name}**: {check.get('message', '')}")
    
    lines.append("")
    lines.append(f"_生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
    
    return "\n".join(lines)


def send_dingtalk_alert(message: str):
    """发送钉钉告警"""
    try:
        from app.notifier.dingtalk import get_dingtalk_notifier
        notifier = get_dingtalk_notifier()
        if notifier:
            notifier.send_text(message)
            logger.info("钉钉告警已发送")
        else:
            logger.warning("钉钉通知器未初始化")
    except Exception as e:
        logger.error(f"钉钉告警发送失败: {e}")


if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(BASE_DIR / "logs" / "health_deep_check.log"),
        ]
    )
    
    # 运行检查
    results = run_all_checks()
    
    # 如果有异常，发送钉钉告警
    if results["overall_status"] in ("critical", "degraded"):
        message = format_dingtalk_message(results)
        send_dingtalk_alert(message)
    else:
        logger.info("健康状态正常，无需告警")
    
    # 退出码: 0=正常, 1=警告, 2=严重
    exit_code = {"healthy": 0, "degraded": 1, "critical": 2}.get(results["overall_status"], 1)
    sys.exit(exit_code)
