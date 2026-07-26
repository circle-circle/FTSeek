#!/usr/bin/env python3
"""
FTSeek-US 轻量 Watchdog (替代 stoploss_daemon)

职责：
1. 检查主进程是否在运行
2. 检查是否有活跃持仓
3. 如果主进程挂了 + 有持仓 → 钉钉告警

不做：
- 不连接 Futu
- 不执行交易
- 不写 DB
- 不读行情

50 行代码，零状态同步问题。
"""
import os
import sys
import subprocess
import logging
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "watchdog.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("watchdog")


def check_main_process() -> bool:
    """检查 ftseek-us 主进程是否存活"""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "ftseek-us.service"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == "active"
    except Exception as e:
        logger.warning(f"systemctl 检查失败: {e}")
        return False


def check_positions() -> list:
    """检查是否有活跃持仓 (直接读 DB，不依赖进程)"""
    import sqlite3
    db_path = BASE_DIR / "data" / "ftseek_us.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, direction, size, avg_entry_price FROM open_positions")
        positions = cursor.fetchall()
        conn.close()
        return positions
    except Exception as e:
        logger.warning(f"DB 查询失败: {e}")
        return []


def send_alert(positions: list):
    """发送钉钉告警"""
    try:
        from dotenv import load_dotenv
        load_dotenv(BASE_DIR / ".env.us")
        webhook = os.getenv("DINGTALK_WEBHOOK_URL_US", "")
        if not webhook:
            logger.warning("钉钉 Webhook 未配置")
            return

        import requests
        pos_lines = "\n".join(
            f"- {p[0]} ({p[1]}) {p[2]}股 @ ${p[3]:.2f}"
            for p in positions
        )
        msg = (
            f"🚨 FTSeek-US Watchdog 告警\n\n"
            f"主进程已停止，但仍有 {len(positions)} 个活跃持仓:\n"
            f"{pos_lines}\n\n"
            f"⚠️ 请在 Futu APP 中手动管理止损，或重启服务"
        )
        resp = requests.post(webhook, json={"msgtype": "text", "text": {"content": msg}}, timeout=10)
        if resp.status_code == 200:
            logger.info("钉钉告警已发送")
        else:
            logger.warning(f"钉钉告警发送失败: {resp.status_code}")
    except Exception as e:
        logger.error(f"告警发送失败: {e}")


if __name__ == "__main__":
    alive = check_main_process()
    positions = check_positions()

    if alive:
        logger.info(f"主进程运行中 | 持仓: {len(positions)} 个")
    elif positions:
        logger.critical(f"🚨 主进程已停止！{len(positions)} 个持仓失去保护")
        send_alert(positions)
    else:
        logger.info("主进程已停止 | 无持仓 (安全)")
