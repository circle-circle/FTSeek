#!/usr/bin/env python3
"""
FTSeek 每小时策略检查推送

功能:
- 检查当前监控标的行情
- 检查策略信号状态
- 检查持仓盈亏
- 推送策略状态到钉钉

使用方式:
    python3 scripts/hourly_check.py

定时任务 (crontab):
    0 * * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/hourly_check.py
"""
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import config
from app.notifier.dingtalk import DingTalkNotifier
from app.storage.database import get_database
from app.risk.dynamic_exit_manager import DynamicExitManager

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/FTSeek/logs/hourly_check.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("ftseek.hourly_check")


def get_current_prices(codes: list) -> dict:
    """获取股票当前价格"""
    prices = {}
    
    try:
        from futu import OpenQuoteContext
        quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
        
        ret, data = quote_ctx.get_market_snapshot(codes)
        quote_ctx.close()
        
        if ret == 0:
            for _, row in data.iterrows():
                code = row['code']
                prices[code] = {
                    'name': row.get('stock_name', code),
                    'price': float(row['last_price']),
                    'change_pct': float(row.get('change_rate', 0)),
                    'volume': int(row.get('volume', 0)),
                    'turnover': float(row.get('turnover', 0)),
                }
    except Exception as e:
        logger.warning(f"获取实时行情失败: {e}")
    
    return prices


def get_position_pnl(positions: list, prices: dict) -> dict:
    """计算持仓盈亏"""
    total_pnl = 0
    total_value = 0
    total_cost = 0
    position_details = []
    
    for pos in positions:
        code = pos['code']
        entry_price = pos['entry_price']
        quantity = pos['quantity']
        action = pos['action']
        
        # 获取当前价格
        current_price = prices.get(code, {}).get('price', entry_price)
        
        # 计算盈亏
        if action == 'BUY':
            pnl_pct = (current_price - entry_price) / entry_price
            pnl_amount = (current_price - entry_price) * quantity
        else:  # SELL
            pnl_pct = (entry_price - current_price) / entry_price
            pnl_amount = (entry_price - current_price) * quantity
        
        cost = entry_price * quantity
        value = current_price * quantity
        
        total_pnl += pnl_amount
        total_value += value
        total_cost += cost
        
        position_details.append({
            'code': code,
            'quantity': quantity,
            'entry_price': entry_price,
            'current_price': current_price,
            'action': action,
            'pnl_pct': pnl_pct,
            'pnl_amount': pnl_amount,
            'value': value,
            'stop_loss': pos.get('stop_loss'),
            'take_profit': pos.get('take_profit')
        })
    
    return {
        'total_pnl': total_pnl,
        'total_value': total_value,
        'total_cost': total_cost,
        'total_pnl_pct': total_pnl / total_cost if total_cost > 0 else 0,
        'position_details': position_details
    }


def get_signal_status(db) -> dict:
    """获取最近信号状态"""
    status = {
        'total_signals': 0,
        'confirmed': 0,
        'executed': 0,
        'ignored': 0,
        'failed': 0,
        'recent_signals': []
    }
    
    # 从信号记录文件读取
    import json
    from datetime import datetime
    
    today = datetime.now().strftime('%Y-%m-%d')
    signal_file = f'/opt/ftseek/FTSeek/data/signal_records/signals_{today}.json'
    
    try:
        if os.path.exists(signal_file):
            with open(signal_file, 'r') as f:
                signals = json.load(f)
            
            status['total_signals'] = len(signals)
            
            for sig in signals:
                executed = sig.get('executed', False)
                reject_reason = sig.get('reject_reason')
                
                if executed and not reject_reason:
                    status['executed'] += 1
                elif reject_reason:
                    status['failed'] += 1
                else:
                    status['confirmed'] += 1
                
                if len(status['recent_signals']) < 10:
                    status['recent_signals'].append({
                        'code': sig.get('code', ''),
                        'action': sig.get('action', ''),
                        'price': sig.get('signal_price', 0),
                        'strategy': sig.get('strategy', ''),
                        'time': sig.get('timestamp', ''),
                        'status': 'executed' if executed and not reject_reason else ('failed' if reject_reason else 'pending')
                    })
                    
    except Exception as e:
        logger.warning(f"获取信号状态失败: {e}")
    
    return status


def get_position_status(db) -> dict:
    """获取持仓状态"""
    status = {
        'total_positions': 0,
        'total_pnl': 0,
        'positions': []
    }
    
    try:
        with db.get_connection() as conn:
            cursor = conn.execute("""
                SELECT code, quantity, entry_price, action, stop_loss, take_profit
                FROM positions 
                WHERE status = 'OPEN'
            """)
            rows = cursor.fetchall()
        
        status['total_positions'] = len(rows)
        
        for row in rows:
            status['positions'].append({
                'code': row[0],
                'quantity': row[1],
                'entry_price': row[2],
                'action': row[3],
                'stop_loss': row[4],
                'take_profit': row[5]
            })
            
    except Exception as e:
        logger.warning(f"获取持仓状态失败: {e}")
    
    return status


def main():
    """主函数"""
    now = datetime.now()
    current_time = now.strftime("%Y-%m-%d %H:%M:%S")
    
    logger.info(f"{'='*60}")
    logger.info(f"FTSeek 每小时策略检查 - {current_time}")
    logger.info(f"{'='*60}")
    
    # 2026-03-27: 每小时推送前先同步持仓
    logger.info("同步持仓...")
    try:
        import subprocess
        result = subprocess.run(
            ['python3', '/opt/ftseek/FTSeek/scripts/position_sync_v2.py', '--sync'],
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            logger.info("持仓同步完成")
        else:
            logger.warning(f"持仓同步失败: {result.stderr}")
    except Exception as e:
        logger.warning(f"持仓同步异常: {e}")
    
    # 初始化组件
    notifier = DingTalkNotifier()
    db = get_database()
    exit_manager = DynamicExitManager()
    
    # 获取监控标的
    watchlist = config.WATCH_LIST
    logger.info(f"监控标的: {watchlist}")
    
    # 获取当前行情
    prices = get_current_prices(watchlist)
    
    # 获取信号状态
    signal_status = get_signal_status(db)
    
    # 获取持仓状态
    position_status = get_position_status(db)
    
    # 计算持仓盈亏
    pnl_info = get_position_pnl(position_status['positions'], prices)
    
    # 获取止盈止损参数
    take_profit_pct = exit_manager.get_take_profit()
    stop_loss_pct = exit_manager.get_stop_loss()
    
    # 构建推送消息
    pnl_emoji = "🟢" if pnl_info['total_pnl'] >= 0 else "🔴"
    
    message = f"""
📊 **FTSeek 每小时策略检查**

🕐 检查时间: {current_time}

---

### 📈 监控标的行情
"""
    
    for code, info in prices.items():
        change_emoji = "🟢" if info['change_pct'] >= 0 else "🔴"
        message += f"- **{info['name']}** ({code}): {info['price']:.2f} {change_emoji} {info['change_pct']:+.2f}%\n"
    
    if not prices:
        message += "- ⚠️ 无法获取实时行情\n"
    
    message += f"""
---

### 📊 今日信号统计
- 总信号数: {signal_status['total_signals']}
- 已执行: {signal_status['executed']}
- 待处理: {signal_status['confirmed']}
- 执行失败: {signal_status['failed']}
"""
    
    if signal_status['recent_signals']:
        message += "\n**最近信号:**\n"
        for sig in signal_status['recent_signals']:
            action_emoji = "🟢" if sig['action'] == 'BUY' else "🔴"
            status_emoji = "✅" if sig['status'] == 'executed' else ("❌" if sig['status'] == 'failed' else "⏳")
            message += f"- {status_emoji} {action_emoji} {sig['code']}: {sig['action']} @ {sig['price']:.2f}\n"
    
    message += f"""
---

### 💼 持仓状态
- 持仓数量: {position_status['total_positions']} 笔
- 持仓市值: HK${pnl_info['total_value']:,.0f}
- 持仓成本: HK${pnl_info['total_cost']:,.0f}
- 浮动盈亏: {pnl_emoji} HK${pnl_info['total_pnl']:+,.0f} ({pnl_info['total_pnl_pct']:+.2%})
"""
    
    if position_status['positions']:
        message += "\n**持仓明细:**\n"
        for pos in pnl_info['position_details']:
            pos_pnl_emoji = "🟢" if pos['pnl_amount'] >= 0 else "🔴"
            message += f"- {pos['code']}: {pos['action']} {pos['quantity']}股 @ {pos['entry_price']:.2f}\n"
            message += f"  现价: {pos['current_price']:.2f} | {pos_pnl_emoji} {pos['pnl_pct']:+.2%}\n"
            if pos['take_profit']:
                message += f"  止盈: {pos['take_profit']:.2f} | 止损: {pos['stop_loss']:.2f}\n"
    else:
        message += "- 无持仓\n"
    
    message += f"""
---

### ⚙️ 当前策略参数
- 共识阈值: 0.4
- 止盈目标: {take_profit_pct:.1%}
- 止损: {stop_loss_pct:.1%}
- 交易模式: 模拟盘

---

💡 **提示**: 策略信号触发后会自动推送到钉钉，请及时确认。
"""
    
    # 发送钉钉推送
    try:
        notifier.send_text(message)  # 使用 send_text 方法
        logger.info("✅ 钉钉推送成功")
    except Exception as e:
        logger.error(f"❌ 钉钉推送失败: {e}")
    
    # 输出到控制台
    print(message)
    
    logger.info(f"{'='*60}")
    logger.info("检查完成")
    
    return {
        'prices': prices,
        'signals': signal_status,
        'positions': position_status,
        'pnl': pnl_info
    }


if __name__ == "__main__":
    main()