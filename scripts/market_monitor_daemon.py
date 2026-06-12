#!/usr/bin/env python3
"""
盘中持续监控守护进程
- 每5分钟检查一次交易状态
- 交易时段自动激活
- 非交易时段休眠
"""
import sqlite3
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('logs/market_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB = "data/ftseek_us.db"
ET = timezone(timedelta(hours=-4))

def is_market_hours():
    now_et = datetime.now(ET)
    weekday = now_et.weekday()
    hour_min = now_et.hour * 100 + now_et.minute
    if weekday >= 5:
        return False
    return 925 <= hour_min <= 1605

def get_today_trades():
    conn = sqlite3.connect(DB)
    cursor = conn.cursor()
    now_et = datetime.now(ET)
    today = now_et.strftime('%Y-%m-%d')
    
    cursor.execute("""
        SELECT symbol, direction, entry_time, exit_time,
               ROUND(realized_pnl, 2), signal_confidence,
               market_regime, exit_reason
        FROM trade_records
        WHERE date(entry_time, 'unixepoch', 'localtime') = ?
          AND realized_pnl IS NOT NULL
        ORDER BY entry_time DESC
    """, (today,))
    
    trades = cursor.fetchall()
    conn.close()
    return trades

def analyze_and_alert():
    trades = get_today_trades()
    now_et = datetime.now(ET)
    
    if not trades:
        logger.info(f"[{now_et.strftime('%H:%M ET')}] 今日暂无交易")
        return
    
    wins = sum(1 for t in trades if t[4] > 0)
    losses = len(trades) - wins
    total_pnl = sum(t[4] for t in trades)
    wr = (wins / len(trades) * 100) if trades else 0
    
    # 每5笔交易或每小时记录一次
    logger.info(f"[{now_et.strftime('%H:%M ET')}] 📊 {len(trades)}笔 | "
                f"WR:{wr:.0f}% ({wins}W/{losses}L) | PnL:${total_pnl:.2f}")
    
    # 警报条件
    alerts = []
    if wr < 40 and len(trades) >= 3:
        alerts.append(f"⚠️ 胜率过低: {wr:.0f}%")
    if total_pnl < -500:
        alerts.append(f"⚠️ 亏损过大: ${total_pnl:.2f}")
    if len(trades) >= 10:
        alerts.append(f"ℹ️ 交易频率高: {len(trades)}笔")
    
    for alert in alerts:
        logger.warning(alert)
    
    # 记录每笔交易
    for t in trades[:3]:  # 最新3笔
        symbol, direction, entry, exit_time, pnl, conf, regime, reason = t
        status = "✅" if pnl > 0 else "❌"
        logger.info(f"  {status} {symbol:10} {direction:5} ${pnl:8.2f} | {reason}")

def main():
    logger.info("="*60)
    logger.info("市场监控守护进程启动")
    logger.info("="*60)
    
    while True:
        try:
            if is_market_hours():
                analyze_and_alert()
                time.sleep(300)  # 5分钟检查一次
            else:
                now_et = datetime.now(ET)
                hour_min = now_et.hour * 100 + now_et.minute
                
                # 计算距离开盘的时间
                if hour_min < 925:
                    # 盘前
                    time.sleep(600)  # 10分钟检查一次
                else:
                    # 盘后
                    time.sleep(3600)  # 1小时检查一次
                    
        except KeyboardInterrupt:
            logger.info("监控进程停止")
            break
        except Exception as e:
            logger.error(f"监控异常: {e}", exc_info=True)
            time.sleep(60)

def _acquire_lock():
    """PID 锁：防止 cron 重复启动多个实例"""
    import fcntl, os, sys
    lock_path = "/tmp/market_monitor_daemon.pid"
    try:
        lock_file = open(lock_path, 'w')
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        return lock_file  # 保持文件句柄，进程退出时自动释放
    except (IOError, OSError):
        return None  # 已有实例在运行

if __name__ == "__main__":
    lock = _acquire_lock()
    if lock is None:
        # 已有实例运行，静默退出
        sys.exit(0)
    main()
