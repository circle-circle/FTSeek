"""
FTSeek Phase 3: 影子执行器 (Shadow Executor)

设计原则:
- 零风险: 绝不连接 Futu OpenD 交易接口，只读行情数据库。
- 独立存储: 影子盘交易记录写入独立的 SQLite DB (`data/shadow_trades.db`)。
- 异步模拟: 每 30 秒拉取最新成交价，计算盈亏，判断是否触发止损/止盈/超时。
- 监控推送: 通过 DingTalk 推送影子盘执行结果，供人工复盘。

功能:
1. 接收 Tier2 信号 (SHORT_ALPHA)。
2. 记录入场价格。
3. 定期轮询 (Poll) 检查出场条件。
4. 统计胜率、盈亏比、最大回撤。

作者: FTSeek Team
日期: 2026-04-16
"""

import sqlite3
import time
import logging
import threading
from typing import Dict, Any, Optional
from pathlib import Path
from datetime import datetime

logger = logging.getLogger("ftseek.shadow_executor")

# 影子盘独立数据库路径
SHADOW_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "shadow_trades.db")

class ShadowTradeRecord:
    def __init__(self, trace_id, code, price, qty, tier_stop_loss, tier_take_profit, max_hold_min):
        self.trace_id = trace_id
        self.code = code
        self.entry_price = price
        self.qty = qty
        self.entry_time = time.time()
        
        # 独立风控参数
        self.stop_loss_price = price * (1 - tier_stop_loss)
        self.take_profit_price = price * (1 + tier_take_profit)
        self.max_hold_seconds = max_hold_min * 60
        
        self.status = "PENDING" # PENDING, WIN, LOSS, TIMEOUT
        self.exit_price = 0.0
        self.pnl = 0.0
        self.pnl_pct = 0.0

class ShadowExecutor:
    """影子执行器 (单例)"""
    
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> 'ShadowExecutor':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._trades: Dict[str, ShadowTradeRecord] = {}
        self._lock = threading.Lock()
        self._poller_thread = None
        self._running = False
        
        self._init_db()
        self._load_pending_trades()
        
        logger.info("✅ 影子执行器已初始化 (零风险模式)")

    def _init_db(self):
        """初始化影子盘数据库"""
        conn = sqlite3.connect(SHADOW_DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS shadow_trades (
                trace_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                pnl REAL,
                pnl_pct REAL,
                status TEXT,
                reason TEXT,
                entry_time TEXT,
                exit_time TEXT
            )
        ''')
        conn.commit()
        conn.close()

    def add_trade(self, record: ShadowTradeRecord):
        """添加一笔影子交易"""
        with self._lock:
            self._trades[record.trace_id] = record
            logger.info(f"[影子盘] 模拟入场: {record.code} @ {record.entry_price:.2f}")
            
        # 启动轮询器 (如果未运行)
        if not self._running:
            self._start_poller()

    def _start_poller(self):
        """启动后台轮询线程"""
        self._running = True
        self._poller_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller_thread.start()

    def _poll_loop(self):
        """轮询检查出场条件"""
        while self._running:
            time.sleep(30) # 每 30 秒检查一次
            try:
                self._check_exits()
            except Exception as e:
                logger.warning(f"[影子盘] 轮询异常: {e}")

    def _check_exits(self):
        """检查所有进行中的交易"""
        # 这里需要获取最新行情，为简单起见，假设通过 DataSubscriber 的 Quote 缓存获取
        # 实际生产中应通过 DataSubscriber 推送或查询接口获取
        from app.data_subscriber import get_global_subscriber
        subscriber = get_global_subscriber()
        if not subscriber:
            return

        now = time.time()
        with self._lock:
            to_remove = []
            for tid, trade in self._trades.items():
                if trade.status != "PENDING":
                    to_remove.append(tid)
                    continue
                    
                # 获取最新价
                current_price = subscriber.get_latest_price(trade.code)
                if current_price <= 0:
                    continue
                
                # 判断出场条件
                reason = ""
                if current_price <= trade.stop_loss_price:
                    reason = "STOP_LOSS_HIT"
                elif current_price >= trade.take_profit_price:
                    reason = "TAKE_PROFIT_HIT"
                elif (now - trade.entry_time) > trade.max_hold_seconds:
                    reason = "TIMEOUT"
                
                if reason:
                    trade.exit_price = current_price
                    trade.pnl = (current_price - trade.entry_price) * trade.qty
                    trade.pnl_pct = (current_price - trade.entry_price) / trade.entry_price
                    trade.status = "WIN" if trade.pnl > 0 else "LOSS"
                    trade.reason = reason
                    
                    self._save_trade(trade)
                    self._notify_exit(trade)
                    to_remove.append(tid)

            for tid in to_remove:
                self._trades.pop(tid, None)

    def _save_trade(self, trade: ShadowTradeRecord):
        """保存交易记录到 DB"""
        try:
            conn = sqlite3.connect(SHADOW_DB_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('''
                INSERT INTO shadow_trades 
                (trace_id, code, entry_price, exit_price, pnl, pnl_pct, status, reason, entry_time, exit_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade.trace_id, trade.code, trade.entry_price, trade.exit_price,
                trade.pnl, trade.pnl_pct, trade.status, trade.reason,
                datetime.now().isoformat(), datetime.now().isoformat()
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[影子盘] 保存记录失败: {e}")

    def _notify_exit(self, trade: ShadowTradeRecord):
        """推送影子盘结果到钉钉"""
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            notifier = DingTalkNotifier()
            
            emoji = "🟢" if trade.pnl > 0 else "🔴"
            direction = "买入"
            msg = (
                f"{emoji} **[影子盘] 模拟平仓**\n"
                f"标的: {trade.code}\n"
                f"方向: {direction}\n"
                f"原因: {trade.reason}\n"
                f"盈亏: {trade.pnl:.2f} ({trade.pnl_pct:+.2%})\n"
                f"持仓时长: {(time.time()-trade.entry_time)/60:.1f} 分钟"
            )
            notifier.send_text(msg)
        except Exception as e:
            logger.debug(f"[影子盘] 推送失败: {e}")

    def _load_pending_trades(self):
        """加载 DB 中未完成的交易 (重启恢复)"""
        # 略，可根据需要实现
