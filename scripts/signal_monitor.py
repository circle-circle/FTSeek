#!/usr/bin/env python3
"""
FTSeek 信号监控脚本

功能:
1. 监控信号执行率
2. 检测异常信号
3. 记录关键指标
4. 发送告警通知（仅交易时间）

运行: 每小时执行一次
"""

import os
import sys
import json
import logging
from datetime import datetime, date, time
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.signal_tracker import signal_tracker
from app.storage.database import get_database
from app.notifier.dingtalk import DingTalkNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.signal_monitor")


# 香港节假日列表 (2026年)
HK_HOLIDAYS_2026 = {
    # 元旦
    date(2026, 1, 1),
    # 农历新年
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),
    # 清明节
    date(2026, 4, 4), date(2026, 4, 6),
    # 劳动节
    date(2026, 5, 1),
    # 佛诞
    date(2026, 5, 25),
    # 端午节
    date(2026, 6, 22),
    # 香港回归纪念日
    date(2026, 7, 1),
    # 中秋节翌日
    date(2026, 9, 26),
    # 国庆节
    date(2026, 10, 1), date(2026, 10, 2),
    # 重阳节
    date(2026, 10, 26),
    # 圣诞节
    date(2026, 12, 25),
}

# 合并所有年份的节假日
HK_HOLIDAYS = HK_HOLIDAYS_2026.copy()


def is_trading_day(check_date: date = None) -> bool:
    """检查是否为交易日（排除周末和节假日）"""
    if check_date is None:
        check_date = date.today()
    
    # 周末不交易
    if check_date.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False
    
    # 节假日不交易
    if check_date in HK_HOLIDAYS:
        return False
    
    return True


def is_trading_time(check_datetime: datetime = None) -> bool:
    """
    检查是否为交易时间
    
    港股交易时间:
    - 上午: 09:30-12:00
    - 下午: 13:00-16:00
    
    告警允许时间（扩展15分钟用于收盘后检查）:
    - 上午: 09:30-12:15
    - 下午: 13:00-16:30
    """
    if check_datetime is None:
        check_datetime = datetime.now()
    
    check_date = check_datetime.date()
    check_time = check_datetime.time()
    
    # 非交易日
    if not is_trading_day(check_date):
        return False
    
    # 上午盘 09:30-12:15（含收盘后15分钟）
    morning_start = time(9, 30)
    morning_end = time(12, 15)
    
    # 下午盘 13:00-16:30（含收盘后30分钟）
    afternoon_start = time(13, 0)
    afternoon_end = time(16, 30)
    
    if morning_start <= check_time <= morning_end:
        return True
    if afternoon_start <= check_time <= afternoon_end:
        return True
    
    return False


def get_trading_status() -> dict:
    """获取当前交易状态"""
    now = datetime.now()
    today = now.date()
    current_time = now.time()
    
    is_trading_day_val = is_trading_day(today)
    is_trading_time_val = is_trading_time(now)
    
    # 判断具体时段
    if not is_trading_day_val:
        period = "非交易日（周末或假期）"
    elif current_time < time(9, 30):
        period = "开盘前"
    elif time(9, 30) <= current_time < time(12, 0):
        period = "上午盘"
    elif time(12, 0) <= current_time < time(13, 0):
        period = "午休"
    elif time(13, 0) <= current_time < time(16, 0):
        period = "下午盘"
    elif time(16, 0) <= current_time < time(16, 30):
        period = "收盘后检查窗口"
    else:
        period = "收盘后"
    
    return {
        'is_trading_day': is_trading_day_val,
        'is_trading_time': is_trading_time_val,
        'period': period,
        'current_time': now.strftime('%H:%M:%S'),
        'date': today.isoformat()
    }


class SignalMonitor:
    """信号监控器"""
    
    def __init__(self):
        self.db = get_database()
        self.notifier = DingTalkNotifier()
        self.today = date.today().isoformat()
        self.trading_status = get_trading_status()
    
    def check_execution_rate(self) -> dict:
        """检查信号执行率"""
        signals = signal_tracker.get_today_signals()
        
        if not signals:
            return {'status': 'no_signals', 'message': '今日无信号'}
        
        total = len(signals)
        executed = sum(1 for s in signals if s.executed)
        rejected = sum(1 for s in signals if not s.executed and s.reject_reason)
        pending = total - executed - rejected
        
        execution_rate = executed / total * 100 if total > 0 else 0
        
        status = 'ok' if execution_rate >= 80 else ('warning' if execution_rate >= 50 else 'alert')
        
        return {
            'status': status,
            'total': total,
            'executed': executed,
            'rejected': rejected,
            'pending': pending,
            'execution_rate': execution_rate
        }
    
    def check_reject_reasons(self) -> dict:
        """检查拒绝原因"""
        signals = signal_tracker.get_today_signals()
        rejected = [s for s in signals if not s.executed and s.reject_reason]
        
        if not rejected:
            return {'status': 'ok', 'reasons': {}}
        
        reasons = Counter()
        for s in rejected:
            reason = s.reject_reason
            # 分类
            if 'TrdSide' in reason:
                reasons['TrdSide未定义'] += 1
            elif 'RetryReason' in reason or 'OrderRetryManager' in reason:
                reasons['RetryReason错误'] += 1
            elif '精度' in reason or '价格' in reason:
                reasons['价格精度'] += 1
            elif '碎股' in reason:
                reasons['碎股限制'] += 1
            elif 'value' in reason:
                reasons['代码错误'] += 1
            elif '冷却' in reason:
                reasons['冷却中'] += 1
            elif '置信度' in reason:
                reasons['置信度不足'] += 1
            else:
                reasons['其他'] += 1
        
        # 判断状态
        code_errors = reasons.get('TrdSide未定义', 0) + reasons.get('RetryReason错误', 0) + reasons.get('代码错误', 0)
        status = 'alert' if code_errors > 5 else ('warning' if code_errors > 0 else 'ok')
        
        return {
            'status': status,
            'reasons': dict(reasons),
            'code_errors': code_errors
        }
    
    def check_positions(self) -> dict:
        """检查持仓状态"""
        positions = []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT code, action, quantity, entry_price, entry_time,
                           stop_loss, take_profit, status
                    FROM positions WHERE status = 'OPEN'
                """)
                for row in cursor.fetchall():
                    positions.append({
                        'code': row[0],
                        'action': row[1],
                        'quantity': row[2],
                        'entry_price': row[3],
                        'entry_time': row[4],
                        'stop_loss': row[5],
                        'take_profit': row[6]
                    })
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
        
        return {
            'status': 'ok' if len(positions) <= 3 else 'warning',
            'count': len(positions),
            'positions': positions
        }
    
    def check_circuit_breaker(self) -> dict:
        """检查熔断器状态"""
        from app.risk.circuit_breaker import circuit_breaker
        
        return {
            'status': 'alert' if circuit_breaker.is_paused() else 'ok',
            'is_paused': circuit_breaker.is_paused(),
            'reject_count': getattr(circuit_breaker, '_reject_count', 0)
        }
    
    def run_monitoring(self) -> dict:
        """运行完整监控"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'date': self.today,
            'trading_status': self.trading_status,
            'checks': {}
        }
        
        # 1. 执行率检查
        results['checks']['execution_rate'] = self.check_execution_rate()
        
        # 2. 拒绝原因检查
        results['checks']['reject_reasons'] = self.check_reject_reasons()
        
        # 3. 持仓检查
        results['checks']['positions'] = self.check_positions()
        
        # 4. 熔断器检查
        results['checks']['circuit_breaker'] = self.check_circuit_breaker()
        
        # 汇总状态
        statuses = [c['status'] for c in results['checks'].values()]
        if 'alert' in statuses:
            results['overall_status'] = 'alert'
        elif 'warning' in statuses:
            results['overall_status'] = 'warning'
        else:
            results['overall_status'] = 'ok'
        
        return results
    
    def save_results(self, results: dict):
        """保存监控结果"""
        monitor_dir = Path('/opt/ftseek/FTSeek/data/monitoring')
        monitor_dir.mkdir(parents=True, exist_ok=True)
        
        monitor_file = monitor_dir / f"monitor_{self.today}.json"
        
        # 追加模式
        existing = []
        if monitor_file.exists():
            try:
                with open(monitor_file, 'r') as f:
                    existing = json.load(f)
            except:
                pass
        
        existing.append(results)
        
        with open(monitor_file, 'w') as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
    
    def send_alert(self, results: dict) -> bool:
        """发送告警（仅交易时间）"""
        # 检查交易时间
        if not self.trading_status['is_trading_time']:
            logger.info(f"非交易时间 ({self.trading_status['period']})，跳过告警发送")
            return False
        
        if results['overall_status'] == 'ok':
            logger.info("状态正常，无需告警")
            return False
        
        message = f"""⚠️ **FTSeek 信号监控告警**

📅 **时间**: {results['timestamp']}
🕐 **时段**: {self.trading_status['period']}

---
"""
        
        # 执行率告警
        exec_check = results['checks']['execution_rate']
        if exec_check['status'] != 'ok':
            message += f"""### 执行率异常

- 总信号: {exec_check['total']}
- 已执行: {exec_check['executed']}
- 执行率: {exec_check['execution_rate']:.1f}%

"""
        
        # 拒绝原因告警
        reject_check = results['checks']['reject_reasons']
        if reject_check['status'] == 'alert':
            message += f"""### 代码错误过多

- 错误数: {reject_check['code_errors']}
- 详情: {reject_check['reasons']}

"""
        
        # 熔断器告警
        cb_check = results['checks']['circuit_breaker']
        if cb_check['is_paused']:
            message += """### 熔断器已触发

交易已暂停，请检查系统状态。

"""
        
        try:
            self.notifier.send_text(message)
            logger.info("告警已发送")
            return True
        except Exception as e:
            logger.error(f"发送告警失败: {e}")
            return False


def main():
    logger.info("开始信号监控...")
    
    monitor = SignalMonitor()
    results = monitor.run_monitoring()
    
    # 保存结果（总是保存）
    monitor.save_results(results)
    
    # 打印摘要
    trading = results['trading_status']
    print(f"\n{'='*60}")
    print(f"📊 FTSeek 信号监控 - {results['date']}")
    print(f"{'='*60}")
    print(f"交易状态: {'✅ 交易时间' if trading['is_trading_time'] else '⛔ ' + trading['period']}")
    print(f"整体状态: {results['overall_status'].upper()}")
    
    for name, check in results['checks'].items():
        status_icon = '✅' if check['status'] == 'ok' else ('⚠️' if check['status'] == 'warning' else '❌')
        print(f"  {status_icon} {name}: {check['status']}")
    
    # 发送告警（仅交易时间）
    alert_sent = monitor.send_alert(results)
    if alert_sent:
        print(f"\n📱 告警已发送")
    elif not trading['is_trading_time']:
        print(f"\n🔇 非交易时间，跳过告警")
    
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()