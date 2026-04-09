#!/usr/bin/env python3
"""
获取初始持仓和资金状态

用于记录服务启动时的初始状态，为后续复盘和改进提供基准。
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

# 输出目录
OUTPUT_DIR = Path("/opt/ftseek/FTSeek/data/initial_state")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 数据库路径
HK_DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"
US_DB_PATH = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"


def get_hk_positions(db_path: str) -> list:
    """获取港股持仓"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT code, action, quantity, entry_price, current_price, 
                   unrealized_pnl, status, strategy, entry_time
            FROM positions
            WHERE status='OPEN'
            ORDER BY entry_time DESC
        """)
        
        positions = []
        for row in cursor.fetchall():
            positions.append({
                'code': row['code'],
                'direction': 'SHORT' if row['action'] == 'SELL' else 'LONG',
                'quantity': row['quantity'],
                'entry_price': row['entry_price'],
                'current_price': row['current_price'] or row['entry_price'],
                'unrealized_pnl': row['unrealized_pnl'] or 0,
                'strategy': row['strategy'] or 'N/A',
                'entry_time': row['entry_time'],
            })
        
        conn.close()
        return positions
    except Exception as e:
        print(f"获取港股持仓失败：{e}")
        return []


def get_us_positions(db_path: str) -> list:
    """获取美股持仓"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT symbol, direction, size, avg_entry_price, current_sl, 
                   initial_tp, entry_time, strategy_id, market_regime
            FROM open_positions
            ORDER BY entry_time DESC
        """)
        
        positions = []
        for row in cursor.fetchall():
            positions.append({
                'symbol': row['symbol'],
                'direction': row['direction'],
                'size': row['size'],
                'entry_price': row['avg_entry_price'],
                'stop_loss': row['current_sl'],
                'take_profit': row['initial_tp'],
                'strategy': row['strategy_id'] or 'N/A',
                'market_regime': row['market_regime'] or 'N/A',
                'entry_time': row['entry_time'],
            })
        
        conn.close()
        return positions
    except Exception as e:
        print(f"获取美股持仓失败：{e}")
        return []


def get_hk_account(db_path: str) -> dict:
    """获取港股账户信息"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM account_info
            ORDER BY timestamp DESC
            LIMIT 1
        """)
        
        row = cursor.fetchone()
        if row:
            account = dict(row)
        else:
            account = {}
        
        conn.close()
        return account
    except Exception as e:
        print(f"获取港股账户失败：{e}")
        return {}


def get_us_account(db_path: str) -> dict:
    """获取美股账户信息"""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM account_info
            ORDER BY timestamp DESC
            LIMIT 1
        """)
        
        row = cursor.fetchone()
        if row:
            account = dict(row)
        else:
            account = {}
        
        conn.close()
        return account
    except Exception as e:
        print(f"获取美股账户失败：{e}")
        return {}


def generate_report(hk_positions, us_positions, hk_account, us_account) -> str:
    """生成初始状态报告"""
    timestamp = datetime.now().isoformat()
    
    lines = [
        "=" * 70,
        "初始持仓和资金状态报告",
        "=" * 70,
        "",
        f"报告时间：{timestamp}",
        "",
        "=" * 70,
        "港股持仓",
        "=" * 70,
    ]
    
    if hk_positions:
        total_pnl = sum(p['unrealized_pnl'] for p in hk_positions)
        lines.append(f"总持仓数：{len(hk_positions)}")
        lines.append(f"总盈亏：{total_pnl:.2f}")
        lines.append("")
        
        for pos in hk_positions:
            pnl_sign = "+" if pos['unrealized_pnl'] > 0 else ""
            lines.append(f"  {pos['code']} {pos['direction']} {pos['quantity']}股")
            lines.append(f"    入场价：{pos['entry_price']:.2f} | 当前价：{pos['current_price']:.2f}")
            lines.append(f"    盈亏：{pnl_sign}{pos['unrealized_pnl']:.2f}")
            lines.append(f"    策略：{pos['strategy']} | 时间：{pos['entry_time']}")
            lines.append("")
    else:
        lines.append("  无持仓")
    
    lines.extend([
        "",
        "=" * 70,
        "美股持仓",
        "=" * 70,
    ])
    
    if us_positions:
        lines.append(f"总持仓数：{len(us_positions)}")
        lines.append("")
        
        for pos in us_positions:
            lines.append(f"  {pos['symbol']} {pos['direction']} {pos['size']}股")
            lines.append(f"    入场价：${pos['entry_price']:.2f}")
            lines.append(f"    止损：${pos['stop_loss']:.2f} | 止盈：${pos['take_profit']:.2f}")
            lines.append(f"    策略：{pos['strategy']} | 市场状态：{pos['market_regime']}")
            lines.append(f"    时间：{pos['entry_time']}")
            lines.append("")
    else:
        lines.append("  无持仓")
    
    lines.extend([
        "",
        "=" * 70,
        "港股账户",
        "=" * 70,
    ])
    
    if hk_account:
        for key, value in hk_account.items():
            lines.append(f"  {key}: {value}")
    else:
        lines.append("  无账户信息")
    
    lines.extend([
        "",
        "=" * 70,
        "美股账户",
        "=" * 70,
    ])
    
    if us_account:
        for key, value in us_account.items():
            lines.append(f"  {key}: {value}")
    else:
        lines.append("  无账户信息")
    
    lines.extend([
        "",
        "=" * 70,
        "下周计划",
        "=" * 70,
        "1. 监控持仓变化",
        "2. 记录交易操作",
        "3. 同步富途服务端数据",
        "4. 收盘后复盘",
        "",
        "=" * 70,
    ])
    
    return "\n".join(lines)


def main():
    """主函数"""
    print("=" * 70)
    print("获取初始持仓和资金状态...")
    print("=" * 70)
    print()
    
    # 获取持仓
    print("获取港股持仓...")
    hk_positions = get_hk_positions(HK_DB_PATH)
    print(f"  港股持仓数：{len(hk_positions)}")
    
    print()
    print("获取美股持仓...")
    us_positions = get_us_positions(US_DB_PATH)
    print(f"  美股持仓数：{len(us_positions)}")
    
    print()
    print("获取港股账户...")
    hk_account = get_hk_account(HK_DB_PATH)
    print(f"  港股账户：{'有' if hk_account else '无'}信息")
    
    print()
    print("获取美股账户...")
    us_account = get_us_account(US_DB_PATH)
    print(f"  美股账户：{'有' if us_account else '无'}信息")
    
    # 生成报告
    print()
    report = generate_report(hk_positions, us_positions, hk_account, us_account)
    
    # 保存报告
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"initial_state_{timestamp}.md"
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"📄 报告已保存到：{report_path}")
    
    # 保存 JSON 数据
    json_path = OUTPUT_DIR / f"initial_state_{timestamp}.json"
    
    data = {
        'timestamp': datetime.now().isoformat(),
        'hk_positions': hk_positions,
        'us_positions': us_positions,
        'hk_account': hk_account,
        'us_account': us_account,
    }
    
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"📄 JSON 数据已保存到：{json_path}")
    
    print()
    print("=" * 70)
    print("初始状态获取完成！")
    print("=" * 70)
    
    # 打印报告
    print()
    print(report)
    
    return {
        'hk_positions': hk_positions,
        'us_positions': us_positions,
        'hk_account': hk_account,
        'us_account': us_account,
        'report_path': str(report_path),
        'json_path': str(json_path),
    }


if __name__ == "__main__":
    result = main()
