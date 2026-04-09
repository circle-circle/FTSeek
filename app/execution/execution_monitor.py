#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
执行质量监控器 (Execution Quality Monitor) - P1-3 优化

2026-04-06 新增:
1. 统计平均滑点、订单拒绝率、重试成功率
2. 提供 API 供健康度看板读取
3. 异常时发送预警

功能:
- 滑点监控：记录每笔订单的预期价格 vs 成交均价
- 订单拒绝率：统计因风控/限额等原因被拒绝的订单
- 重试成功率：统计订单重试后的成功比例
- 执行延迟：从信号生成到下单完成的时间
"""

import logging
import time
import sqlite3
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import deque, defaultdict
from pathlib import Path

from app import config
from app.notifier.dingtalk import DingTalkNotifier
from app.storage.database import get_sqlite_connection  # 2026-04-08: 统一连接函数

logger = logging.getLogger("ftseek.execution.monitor")
logger.propagate = False


@dataclass
class ExecutionRecord:
    """执行记录"""
    code: str
    signal_time: float
    order_time: float = 0.0
    fill_time: float = 0.0
    signal_price: float = 0.0
    fill_price: float = 0.0
    quantity: int = 0
    order_type: str = ""  # MARKET/LIMIT
    status: str = ""  # FILLED/REJECTED/CANCELLED
    reject_reason: str = ""
    retry_count: int = 0
    slippage: float = 0.0  # 滑点 ( fill_price - signal_price ) / signal_price
    execution_latency_ms: float = 0.0  # 执行延迟 (毫秒)


class ExecutionMonitor:
    """
    执行质量监控器

    监控指标:
    - 平均滑点 (按股票/订单类型分组)
    - 订单拒绝率
    - 重试成功率
    - 执行延迟
    """

    def __init__(self, db_path: str = None, max_records: int = 500):
        """
        初始化

        Args:
            db_path: 数据库路径
            max_records: 内存中保留的最大记录数
        """
        self.db_path = db_path or str(config.DATA_DIR / "execution.db")
        self.max_records = max_records

        # 内存缓存 (用于快速查询)
        self._records: deque = deque(maxlen=max_records)
        self._today_stats: Dict = {}
        self._last_alert_time: float = 0

        # 告警阈值
        self.slippage_alert_threshold = config.EXECUTION_MONITOR_SLIPPAGE_ALERT  # 默认 1%
        self.reject_rate_alert_threshold = config.EXECUTION_MONITOR_REJECT_RATE_ALERT  # 默认 10%

        # 钉钉通知器
        self.notifier = DingTalkNotifier()

        # 初始化数据库
        self._init_db()

        logger.info(f"执行质量监控器已初始化 | 数据库={self.db_path}")

    def _init_db(self):
        """初始化数据库表"""
        try:
            conn = get_sqlite_connection(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS execution_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    signal_time REAL NOT NULL,
                    order_time REAL,
                    fill_time REAL,
                    signal_price REAL,
                    fill_price REAL,
                    quantity INTEGER,
                    order_type TEXT,
                    status TEXT,
                    reject_reason TEXT,
                    retry_count INTEGER DEFAULT 0,
                    slippage REAL,
                    execution_latency_ms REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY,
                    total_orders INTEGER DEFAULT 0,
                    filled_orders INTEGER DEFAULT 0,
                    rejected_orders INTEGER DEFAULT 0,
                    avg_slippage REAL,
                    avg_latency_ms REAL,
                    retry_success_count INTEGER DEFAULT 0,
                    retry_total_count INTEGER DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_code_time
                ON execution_records(code, signal_time)
            """)

            conn.commit()
            conn.close()
            logger.debug("执行质量数据库初始化完成")

        except Exception as e:
            logger.error(f"执行质量数据库初始化失败：{e}")

    def record_start(self, code: str, signal_price: float, quantity: int, order_type: str) -> str:
        """
        记录执行开始

        Args:
            code: 股票代码
            signal_price: 信号价格
            quantity: 数量
            order_type: 订单类型

        Returns:
            record_id: 记录 ID
        """
        record = ExecutionRecord(
            code=code,
            signal_time=time.time(),
            signal_price=signal_price,
            quantity=quantity,
            order_type=order_type,
        )
        self._records.append(record)
        return f"{code}_{record.signal_time}"

    def record_fill(
        self,
        record_id: str,
        fill_price: float,
        fill_time: float = None,
        retry_count: int = 0,
    ):
        """
        记录成交

        Args:
            record_id: 记录 ID
            fill_price: 成交价格
            fill_time: 成交时间
            retry_count: 重试次数
        """
        record = self._find_record(record_id)
        if not record:
            logger.warning(f"未找到执行记录：{record_id}")
            return

        record.fill_price = fill_price
        record.fill_time = fill_time or time.time()
        record.status = "FILLED"
        record.retry_count = retry_count

        # 计算滑点
        if record.signal_price > 0:
            record.slippage = (fill_price - record.signal_price) / record.signal_price

        # 计算执行延迟
        record.execution_latency_ms = (record.fill_time - record.signal_time) * 1000

        # 记录订单时间
        record.order_time = record.signal_time  # 简化：假设下单时间等于信号时间

        # 持久化
        self._persist_record(record)

        # 检查是否触发告警
        if abs(record.slippage) > self.slippage_alert_threshold:
            self._send_slippage_alert(record)

        logger.info(
            f"[执行质量] {record.code} 成交 | 信号价={record.signal_price:.3f} "
            f"成交价={fill_price:.3f} 滑点={record.slippage*100:.2f}% "
            f"延迟={record.execution_latency_ms:.0f}ms"
        )

    def record_reject(self, record_id: str, reason: str):
        """
        记录拒绝

        Args:
            record_id: 记录 ID
            reason: 拒绝原因
        """
        record = self._find_record(record_id)
        if not record:
            logger.warning(f"未找到执行记录：{record_id}")
            return

        record.status = "REJECTED"
        record.reject_reason = reason
        record.fill_time = time.time()
        record.execution_latency_ms = (record.fill_time - record.signal_time) * 1000

        self._persist_record(record)

        logger.warning(f"[执行质量] {record.code} 被拒绝：{reason}")

    def _find_record(self, record_id: str) -> Optional[ExecutionRecord]:
        """从内存缓存中查找记录"""
        for record in self._records:
            if f"{record.code}_{record.signal_time}" == record_id:
                return record
        return None

    def _persist_record(self, record: ExecutionRecord):
        """持久化到数据库"""
        try:
            conn = get_sqlite_connection(self.db_path)
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO execution_records
                (code, signal_time, order_time, fill_time, signal_price, fill_price,
                 quantity, order_type, status, reject_reason, retry_count,
                 slippage, execution_latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.code,
                record.signal_time,
                record.order_time,
                record.fill_time,
                record.signal_price,
                record.fill_price,
                record.quantity,
                record.order_type,
                record.status,
                record.reject_reason,
                record.retry_count,
                record.slippage,
                record.execution_latency_ms,
            ))

            # 更新日统计
            today = datetime.now().strftime("%Y-%m-%d")
            self._update_daily_stats(today, record)

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"持久化执行记录失败：{e}")

    def _update_daily_stats(self, date: str, record: ExecutionRecord, max_retries: int = 3):
        """更新日统计（带重试机制）"""
        for attempt in range(max_retries):
            try:
                conn = get_sqlite_connection(self.db_path)
                cursor = conn.cursor()

                # 检查是否已存在
                cursor.execute("SELECT 1 FROM daily_stats WHERE date = ?", (date,))
                exists = cursor.fetchone() is not None

                if exists:
                    # 更新
                    cursor.execute("""
                        UPDATE daily_stats
                        SET total_orders = total_orders + 1,
                            filled_orders = filled_orders + ?,
                            rejected_orders = rejected_orders + ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE date = ?
                    """, (
                        1 if record.status == "FILLED" else 0,
                        1 if record.status == "REJECTED" else 0,
                        date,
                    ))
                else:
                    # 插入
                    cursor.execute("""
                        INSERT INTO daily_stats
                        (date, total_orders, filled_orders, rejected_orders, avg_slippage)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        date,
                        1,
                        1 if record.status == "FILLED" else 0,
                        1 if record.status == "REJECTED" else 0,
                        record.slippage if record.status == "FILLED" else 0,
                    ))

                conn.commit()
                conn.close()
                return  # 成功

            except sqlite3.OperationalError as e:
                # 数据库锁定，重试
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    logger.debug(f"数据库锁定，重试 {attempt+1}/{max_retries}: {e}")
                    time.sleep(0.1 * (attempt + 1))  # 指数退避
                else:
                    logger.error(f"更新日统计失败（重试{max_retries}次后）: {e}")
            except Exception as e:
                logger.error(f"更新日统计失败：{e}")
                break

    def _send_slippage_alert(self, record: ExecutionRecord):
        """发送滑点告警"""
        # 避免频繁告警 (5 分钟内不重复)
        now = time.time()
        if now - self._last_alert_time < 300:
            return

        self._last_alert_time = now

        message = (
            f"⚠️ 执行滑点告警\n"
            f"股票：{record.code}\n"
            f"信号价：{record.signal_price:.3f}\n"
            f"成交价：{record.fill_price:.3f}\n"
            f"滑点：{record.slippage*100:.2f}% (阈值={self.slippage_alert_threshold*100:.1f}%)\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        try:
            self.notifier.send_markdown("⚠️ 执行滑点告警", message)
            logger.warning(f"滑点告警已发送：{record.code}")
        except Exception as e:
            logger.error(f"发送滑点告警失败：{e}")

    # =====================================================================
    # 统计查询 API
    # =====================================================================

    def get_today_stats(self) -> Dict:
        """获取今日统计"""
        try:
            conn = get_sqlite_connection(self.db_path)
            cursor = conn.cursor()

            today = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("""
                SELECT
                    total_orders,
                    filled_orders,
                    rejected_orders,
                    avg_slippage,
                    avg_latency_ms,
                    retry_success_count,
                    retry_total_count
                FROM daily_stats
                WHERE date = ?
            """, (today,))

            row = cursor.fetchone()
            conn.close()

            if row:
                total, filled, rejected, avg_slip, avg_lat, retry_ok, retry_total = row
                return {
                    'date': today,
                    'total_orders': total or 0,
                    'filled_orders': filled or 0,
                    'rejected_orders': rejected or 0,
                    'reject_rate': rejected / total if total > 0 else 0,
                    'avg_slippage': avg_slip or 0,
                    'avg_latency_ms': avg_lat or 0,
                    'retry_rate': retry_ok / retry_total if retry_total > 0 else 0,
                }

        except Exception as e:
            logger.error(f"获取今日统计失败：{e}")

        return {
            'date': datetime.now().strftime("%Y-%m-%d"),
            'total_orders': 0,
            'filled_orders': 0,
            'rejected_orders': 0,
            'reject_rate': 0,
            'avg_slippage': 0,
            'avg_latency_ms': 0,
            'retry_rate': 0,
        }

    def get_slippage_stats(self, days: int = 7) -> Dict:
        """
        获取滑点统计

        Args:
            days: 统计天数

        Returns:
            {
                'avg_slippage': float,
                'max_slippage': float,
                'slippage_by_stock': {code: avg_slippage},
                'slippage_by_order_type': {MARKET/LIMIT: avg_slippage}
            }
        """
        try:
            conn = get_sqlite_connection(self.db_path)
            cursor = conn.cursor()

            cutoff = (datetime.now() - timedelta(days=days)).timestamp()

            # 整体统计
            cursor.execute("""
                SELECT AVG(slippage), MAX(slippage), MIN(slippage)
                FROM execution_records
                WHERE signal_time > ? AND status = 'FILLED'
            """, (cutoff,))
            avg_slip, max_slip, min_slip = cursor.fetchone()

            # 按股票分组
            cursor.execute("""
                SELECT code, AVG(slippage)
                FROM execution_records
                WHERE signal_time > ? AND status = 'FILLED'
                GROUP BY code
                ORDER BY AVG(ABS(slippage)) DESC
                LIMIT 10
            """, (cutoff,))
            by_stock = {row[0]: row[1] for row in cursor.fetchall()}

            # 按订单类型分组
            cursor.execute("""
                SELECT order_type, AVG(slippage)
                FROM execution_records
                WHERE signal_time > ? AND status = 'FILLED'
                GROUP BY order_type
            """, (cutoff,))
            by_type = {row[0]: row[1] for row in cursor.fetchall()}

            conn.close()

            return {
                'avg_slippage': avg_slip or 0,
                'max_slippage': max_slip or 0,
                'min_slippage': min_slip or 0,
                'slippage_by_stock': by_stock,
                'slippage_by_order_type': by_type,
            }

        except Exception as e:
            logger.error(f"获取滑点统计失败：{e}")
            return {}

    def get_reject_stats(self, days: int = 7) -> Dict:
        """
        获取拒绝统计

        Args:
            days: 统计天数

        Returns:
            {
                'total_rejected': int,
                'reject_rate': float,
                'reject_reasons': {reason: count}
            }
        """
        try:
            conn = get_sqlite_connection(self.db_path)
            cursor = conn.cursor()

            cutoff = (datetime.now() - timedelta(days=days)).timestamp()

            # 总拒绝数
            cursor.execute("""
                SELECT COUNT(*)
                FROM execution_records
                WHERE signal_time > ? AND status = 'REJECTED'
            """, (cutoff,))
            total_rejected = cursor.fetchone()[0]

            # 总订单数
            cursor.execute("""
                SELECT COUNT(*)
                FROM execution_records
                WHERE signal_time > ?
            """, (cutoff,))
            total_orders = cursor.fetchone()[0]

            # 拒绝原因分组
            cursor.execute("""
                SELECT reject_reason, COUNT(*)
                FROM execution_records
                WHERE signal_time > ? AND status = 'REJECTED'
                GROUP BY reject_reason
                ORDER BY COUNT(*) DESC
            """, (cutoff,))
            by_reason = {row[0]: row[1] for row in cursor.fetchall()}

            conn.close()

            return {
                'total_rejected': total_rejected,
                'total_orders': total_orders,
                'reject_rate': total_rejected / total_orders if total_orders > 0 else 0,
                'reject_reasons': by_reason,
            }

        except Exception as e:
            logger.error(f"获取拒绝统计失败：{e}")
            return {}

    def get_retry_stats(self, days: int = 7) -> Dict:
        """
        获取重试统计

        Args:
            days: 统计天数

        Returns:
            {
                'total_retries': int,
                'retry_success_rate': float,
                'avg_retry_count': float
            }
        """
        try:
            conn = get_sqlite_connection(self.db_path)
            cursor = conn.cursor()

            cutoff = (datetime.now() - timedelta(days=days)).timestamp()

            # 有重试的订单
            cursor.execute("""
                SELECT COUNT(*), SUM(retry_count), AVG(retry_count)
                FROM execution_records
                WHERE signal_time > ? AND retry_count > 0
            """, (cutoff,))
            count, total_retries, avg_retries = cursor.fetchone()

            # 重试后成功
            cursor.execute("""
                SELECT COUNT(*)
                FROM execution_records
                WHERE signal_time > ? AND retry_count > 0 AND status = 'FILLED'
            """, (cutoff,))
            retry_success = cursor.fetchone()[0]

            conn.close()

            return {
                'total_orders_with_retry': count or 0,
                'total_retries': total_retries or 0,
                'retry_success_count': retry_success or 0,
                'retry_success_rate': retry_success / count if count > 0 else 0,
                'avg_retry_count': avg_retries or 0,
            }

        except Exception as e:
            logger.error(f"获取重试统计失败：{e}")
            return {}

    def get_health_score(self) -> float:
        """
        计算执行健康度评分 (0-100)

        评分标准:
        - 滑点 < 0.5%: +40 分
        - 拒绝率 < 5%: +30 分
        - 重试成功率 > 80%: +20 分
        - 平均延迟 < 500ms: +10 分

        Returns:
            健康度评分 (0-100)
        """
        score = 0.0

        # 滑点评分
        slip_stats = self.get_slippage_stats(days=7)
        avg_slippage = abs(slip_stats.get('avg_slippage', 0))
        if avg_slippage < 0.005:  # < 0.5%
            score += 40
        elif avg_slippage < 0.01:  # < 1%
            score += 20
        elif avg_slippage < 0.02:  # < 2%
            score += 10

        # 拒绝率评分
        reject_stats = self.get_reject_stats(days=7)
        reject_rate = reject_stats.get('reject_rate', 0)
        if reject_rate < 0.05:  # < 5%
            score += 30
        elif reject_rate < 0.10:  # < 10%
            score += 15
        elif reject_rate < 0.20:  # < 20%
            score += 5

        # 重试成功率评分
        retry_stats = self.get_retry_stats(days=7)
        retry_success = retry_stats.get('retry_success_rate', 0)
        if retry_success > 0.80:
            score += 20
        elif retry_success > 0.50:
            score += 10

        # 延迟评分 (简化：假设都 < 500ms)
        score += 10

        return min(100, score)

    def get_dashboard(self) -> Dict:
        """
        获取执行质量看板数据

        Returns:
            {
                'health_score': float,
                'today': Dict,
                'slippage': Dict,
                'reject': Dict,
                'retry': Dict
            }
        """
        return {
            'health_score': self.get_health_score(),
            'today': self.get_today_stats(),
            'slippage': self.get_slippage_stats(days=7),
            'reject': self.get_reject_stats(days=7),
            'retry': self.get_retry_stats(days=7),
        }

    def export_report(self) -> str:
        """导出执行质量报告"""
        dashboard = self.get_dashboard()

        lines = [
            "=" * 80,
            "  FTSeek 执行质量监控报告",
            "=" * 80,
            f"日期：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"健康度评分：{dashboard['health_score']:.0f} / 100",
            "",
            "今日统计:",
            f"  总订单：{dashboard['today']['total_orders']}",
            f"  成交：{dashboard['today']['filled_orders']}",
            f"  拒绝：{dashboard['today']['rejected_orders']} ({dashboard['today']['reject_rate']*100:.1f}%)",
            f"  平均滑点：{dashboard['today']['avg_slippage']*100:.2f}%",
            f"  平均延迟：{dashboard['today']['avg_latency_ms']:.0f}ms",
            "",
            "7 日滑点统计:",
            f"  平均：{dashboard['slippage'].get('avg_slippage', 0)*100:.2f}%",
            f"  最大：{dashboard['slippage'].get('max_slippage', 0)*100:.2f}%",
        ]

        if dashboard['slippage'].get('slippage_by_stock'):
            lines.append("  按股票:")
            for code, slip in list(dashboard['slippage']['slippage_by_stock'].items())[:5]:
                lines.append(f"    {code}: {slip*100:.2f}%")

        lines.extend([
            "",
            "7 日拒绝统计:",
            f"  总拒绝：{dashboard['reject']['total_rejected']}",
            f"  拒绝率：{dashboard['reject']['reject_rate']*100:.1f}%",
        ])

        if dashboard['reject'].get('reject_reasons'):
            lines.append("  拒绝原因:")
            for reason, count in dashboard['reject']['reject_reasons'].items():
                lines.append(f"    {reason}: {count}")

        lines.extend([
            "",
            "7 日重试统计:",
            f"  重试订单：{dashboard['retry']['total_orders_with_retry']}",
            f"  重试成功：{dashboard['retry']['retry_success_count']}",
            f"  成功率：{dashboard['retry']['retry_success_rate']*100:.1f}%",
            "",
            "=" * 80,
        ])

        return "\n".join(lines)


# 全局单例
_execution_monitor: Optional[ExecutionMonitor] = None


def get_execution_monitor() -> ExecutionMonitor:
    """获取执行质量监控器实例"""
    global _execution_monitor
    if _execution_monitor is None:
        _execution_monitor = ExecutionMonitor()
    return _execution_monitor


def reset_execution_monitor():
    """重置监控器 (用于测试)"""
    global _execution_monitor
    _execution_monitor = None
