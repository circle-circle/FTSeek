#!/usr/bin/env python3
"""
持仓同步检查服务

功能:
1. 定时检查数据库与富途账户持仓一致性
2. 发现差异自动告警（仅交易时间）
3. 记录详细日志
4. 可选自动同步
"""

import os
import sys
import json
import sqlite3
import logging
from datetime import datetime, date, time
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))

from futu import OpenSecTradeContext, TrdEnv
from app import config
from app.notifier.dingtalk import DingTalkNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.position_sync")


# 香港节假日列表 (2026年)
HK_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 2, 17), date(2026, 2, 18), date(2026, 2, 19),
    date(2026, 4, 4), date(2026, 4, 6),
    date(2026, 5, 1),
    date(2026, 5, 25),
    date(2026, 6, 22),
    date(2026, 7, 1),
    date(2026, 9, 26),
    date(2026, 10, 1), date(2026, 10, 2),
    date(2026, 10, 26),
    date(2026, 12, 25),
}
HK_HOLIDAYS = HK_HOLIDAYS_2026.copy()


def is_trading_day(check_date: date = None) -> bool:
    """检查是否为交易日"""
    if check_date is None:
        check_date = date.today()
    if check_date.weekday() >= 5:
        return False
    if check_date in HK_HOLIDAYS:
        return False
    return True


def is_trading_time(check_datetime: datetime = None) -> bool:
    """
    检查是否为交易时间（含收盘后30分钟窗口用于检查）
    - 上午: 09:30-12:15
    - 下午: 13:00-16:30
    """
    if check_datetime is None:
        check_datetime = datetime.now()
    
    check_date = check_datetime.date()
    check_time = check_datetime.time()
    
    if not is_trading_day(check_date):
        return False
    
    if time(9, 30) <= check_time <= time(12, 15):
        return True
    if time(13, 0) <= check_time <= time(16, 30):
        return True
    
    return False


def get_trading_status() -> dict:
    """获取当前交易状态"""
    now = datetime.now()
    today = now.date()
    current_time = now.time()
    
    is_trading_day_val = is_trading_day(today)
    is_trading_time_val = is_trading_time(now)
    
    if not is_trading_day_val:
        period = "非交易日"
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


class PositionSyncService:
    """持仓同步服务"""
    
    def __init__(self, auto_sync: bool = False):
        self.db_path = '/opt/ftseek/FTSeek/data/ftseek.db'
        self.notifier = DingTalkNotifier()
        self.auto_sync = auto_sync
    
    def get_futu_positions(self) -> Dict:
        """获取富途实际持仓"""
        ctx = OpenSecTradeContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
        ret, data = ctx.position_list_query(trd_env=TrdEnv.SIMULATE)
        ctx.close()
        
        positions = {}
        if ret == 0 and len(data) > 0:
            # 过滤掉 qty=0 的历史记录，只保留实际持仓
            for idx, row in data.iterrows():
                qty = float(row['qty'])
                if qty > 0:  # 只保留实际持仓
                    positions[row['code']] = {
                        'qty': qty,
                        'can_sell': float(row['can_sell_qty']),
                        'cost_price': float(row['cost_price']),
                        'market_val': float(row['market_val']),
                        'pl_val': float(row['pl_val'])
                    }
        
        return positions
    
    def get_db_positions(self) -> Tuple[Dict, Dict]:
        """获取数据库持仓"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 当前持仓
        cursor.execute("""
            SELECT code, SUM(quantity) as qty, AVG(entry_price) as avg_price
            FROM positions WHERE status = 'OPEN'
            GROUP BY code
        """)
        open_positions = {}
        for row in cursor.fetchall():
            open_positions[row[0]] = {
                'qty': float(row[1]),
                'avg_price': float(row[2])
            }
        
        # 今日交易
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute("""
            SELECT code, action, quantity, entry_price, exit_price, realized_pnl, status
            FROM positions 
            WHERE date(entry_time) = ?
            ORDER BY entry_time
        """, (today,))
        today_trades = cursor.fetchall()
        
        conn.close()
        
        return open_positions, today_trades
    
    def check_sync(self) -> Dict:
        """检查同步状态"""
        result = {
            'timestamp': datetime.now().isoformat(),
            'futu_positions': {},
            'db_positions': {},
            'diff': [],
            'is_sync': True,
            'message': ''
        }
        
        # 获取两边数据
        futu_pos = self.get_futu_positions()
        db_pos, today_trades = self.get_db_positions()
        
        result['futu_positions'] = futu_pos
        result['db_positions'] = db_pos
        
        # 对比差异
        all_codes = set(futu_pos.keys()) | set(db_pos.keys())
        
        for code in all_codes:
            futu_qty = futu_pos.get(code, {}).get('qty', 0)
            db_qty = db_pos.get(code, {}).get('qty', 0)
            
            if abs(futu_qty - db_qty) > 0.01:  # 允许小数点误差
                result['is_sync'] = False
                result['diff'].append({
                    'code': code,
                    'futu_qty': futu_qty,
                    'db_qty': db_qty,
                    'diff': futu_qty - db_qty
                })
        
        if result['is_sync']:
            result['message'] = '✅ 数据库与富途持仓一致'
        else:
            result['message'] = f'⚠️ 发现 {len(result["diff"])} 处差异'
        
        # 保存检查记录
        self._save_check_log(result)
        
        return result
    
    def _save_check_log(self, result: Dict):
        """保存检查日志"""
        log_dir = Path('/opt/ftseek/FTSeek/data/sync_logs')
        log_dir.mkdir(parents=True, exist_ok=True)
        
        today = datetime.now().strftime('%Y-%m-%d')
        log_file = log_dir / f"sync_check_{today}.json"
        
        # 追加模式
        logs = []
        if log_file.exists():
            try:
                with open(log_file, 'r') as f:
                    logs = json.load(f)
            except:
                pass
        
        logs.append(result)
        
        with open(log_file, 'w') as f:
            json.dump(logs, f, indent=2, ensure_ascii=False, default=str)
    
    def send_alert(self, result: Dict) -> bool:
        """发送差异告警（仅交易时间）"""
        trading = get_trading_status()
        
        # 非交易时间跳过告警
        if not trading['is_trading_time']:
            logger.info(f"非交易时间 ({trading['period']})，跳过告警发送")
            return False
        
        if result['is_sync']:
            logger.info("持仓同步，无需告警")
            return False
        
        message = f"""⚠️ **持仓同步异常告警**

📅 **时间**: {result['timestamp']}
🕐 **时段**: {trading['period']}

---

### 差异详情

| 股票 | 富途持仓 | 数据库持仓 | 差异 |
|------|----------|------------|------|
"""
        for diff in result['diff']:
            message += f"| {diff['code']} | {diff['futu_qty']:.0f} 股 | {diff['db_qty']:.0f} 股 | {diff['diff']:+.0f} 股 |\n"
        
        message += f"""
---

**建议操作**:
- 检查平仓指令是否正确执行
- 运行同步脚本: `python3 scripts/position_sync_service.py --sync`
"""
        
        try:
            self.notifier.send_text(message)
            logger.info("差异告警已发送")
            return True
        except Exception as e:
            logger.error(f"发送告警失败: {e}")
            return False
    
    def sync(self, dry_run: bool = True) -> Dict:
        """
        同步持仓
        
        Args:
            dry_run: 仅检测不修改
        """
        result = self.check_sync()
        
        if result['is_sync']:
            logger.info("数据一致，无需同步")
            return result
        
        if dry_run:
            logger.info("检测模式，未执行修改")
            return result
        
        # 执行同步
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for diff in result['diff']:
            code = diff['code']
            futu_qty = diff['futu_qty']
            db_qty = diff['db_qty']
            
            if futu_qty > 0 and db_qty == 0:
                # 富途有持仓，数据库没有 - 创建记录
                cost_price = result['futu_positions'][code]['cost_price']
                entry_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                cursor.execute("""
                    INSERT INTO positions 
                    (code, action, quantity, entry_price, entry_time, status)
                    VALUES (?, 'BUY', ?, ?, ?, 'OPEN')
                """, (code, int(futu_qty), cost_price, entry_time))
                
                logger.info(f"创建持仓记录: {code} {int(futu_qty)} 股")
            
            elif futu_qty == 0 and db_qty > 0:
                # 数据库有持仓，富途没有 - 更新为已平仓
                exit_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
                cursor.execute("""
                    UPDATE positions 
                    SET status = 'CLOSED', exit_time = ?, exit_price = entry_price
                    WHERE code = ? AND status = 'OPEN'
                """, (exit_time, code))
                
                logger.info(f"更新为已平仓: {code}")
            
            elif futu_qty != db_qty:
                # 数量不一致 - 调整
                qty_diff = db_qty - futu_qty
                
                if qty_diff > 0:
                    # 数据库多了，需要删除
                    cursor.execute("""
                        SELECT id, quantity FROM positions 
                        WHERE code = ? AND status = 'OPEN'
                        ORDER BY entry_time ASC
                    """, (code,))
                    records = cursor.fetchall()
                    
                    to_remove = qty_diff
                    for pos_id, qty in records:
                        if to_remove <= 0:
                            break
                        if qty <= to_remove:
                            cursor.execute("DELETE FROM positions WHERE id = ?", (pos_id,))
                            to_remove -= qty
                        else:
                            cursor.execute("UPDATE positions SET quantity = ? WHERE id = ?", (qty - to_remove, pos_id))
                            to_remove = 0
                
                logger.info(f"调整数量: {code} {db_qty:.0f} → {futu_qty:.0f}")
        
        conn.commit()
        conn.close()
        
        logger.info("同步完成")
        
        # 重新检查
        return self.check_sync()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='持仓同步检查')
    parser.add_argument('--sync', action='store_true', help='执行同步')
    parser.add_argument('--alert', action='store_true', help='发送告警')
    args = parser.parse_args()
    
    service = PositionSyncService()
    trading = get_trading_status()
    
    if args.sync:
        result = service.sync(dry_run=False)
    else:
        result = service.check_sync()
    
    print(f"\n{'='*60}")
    print(f"📊 持仓同步检查")
    print(f"{'='*60}")
    print(f"时间: {result['timestamp']}")
    print(f"交易状态: {'✅ ' + trading['period'] if trading['is_trading_time'] else '⛔ ' + trading['period']}")
    print(f"同步状态: {result['message']}")
    
    if result['diff']:
        print(f"\n差异详情:")
        for diff in result['diff']:
            print(f"  {diff['code']}: 富途 {diff['futu_qty']:.0f} 股, 数据库 {diff['db_qty']:.0f} 股")
    
    if args.alert:
        alert_sent = service.send_alert(result)
        if alert_sent:
            print(f"\n📱 告警已发送")
        elif not trading['is_trading_time']:
            print(f"\n🔇 非交易时间，跳过告警")
    
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()