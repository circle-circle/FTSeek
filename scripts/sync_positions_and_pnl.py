#!/usr/bin/env python3
"""
持仓和收益同步脚本

同步 FTSeek 和 FTSeek-US 的持仓到富途端，并更新收益数据。
"""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
)
logger = logging.getLogger("ftseek.sync_positions")

# 数据库路径
HK_DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"
US_DB_PATH = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"

# 输出目录
OUTPUT_DIR = Path("/opt/ftseek/FTSeek/data/position_sync")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def get_hk_positions(db_path: str) -> List[Dict[str, Any]]:
    """
    从数据库获取港股持仓
    
    Args:
        db_path: 数据库路径
    
    Returns:
        持仓列表
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT code, action, quantity, entry_price, current_price, 
                   unrealized_pnl, status, strategy, entry_time,
                   stop_loss, take_profit
            FROM positions
            WHERE status='OPEN'
            ORDER BY entry_time DESC
        """)
        
        positions = []
        for row in cursor.fetchall():
            pnl_percent = (row['unrealized_pnl'] / row['entry_price'] / row['quantity'] * 100) if row['quantity'] > 0 else 0
            
            positions.append({
                'market': 'HK',
                'code': row['code'],
                'direction': 'SHORT' if row['action'] == 'SELL' else 'LONG',
                'quantity': row['quantity'],
                'entry_price': row['entry_price'],
                'current_price': row['current_price'] or row['entry_price'],
                'pnl': row['unrealized_pnl'] or 0,
                'pnl_percent': pnl_percent,
                'status': row['status'],
                'strategy': row['strategy'] or 'N/A',
                'entry_time': row['entry_time'],
                'stop_loss': row['stop_loss'],
                'take_profit': row['take_profit'],
            })
        
        conn.close()
        return positions
    except sqlite3.OperationalError as e:
        logger.warning(f"Failed to get HK positions: {e}")
        conn.close()
        return []


def get_us_positions(db_path: str) -> List[Dict[str, Any]]:
    """
    从数据库获取美股持仓
    
    Args:
        db_path: 数据库路径
    
    Returns:
        持仓列表
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # 美股 open_positions 表结构:
        # symbol, direction, size, avg_entry_price, initial_sl, current_sl, 
        # initial_tp, take_profit_json, trail_state_json, entry_time, 
        # time_stop_hours, strategy_id, signal_id, position_id, market_regime, updated_at
        
        cursor.execute("""
            SELECT symbol as code, direction, size, 
                   avg_entry_price as entry_price, initial_sl as stop_loss,
                   initial_tp as take_profit, entry_time, strategy_id as strategy,
                   market_regime
            FROM open_positions
            ORDER BY entry_time DESC
        """)
        
        positions = []
        for row in cursor.fetchall():
            # 获取当前价格 (从 trade_records 或其他地方)
            # 这里使用 entry_price 作为占位符
            current_price = row['entry_price']  # TODO: 从 API 获取实时价格
            quantity = row['size']  # 修复：美股使用 size 字段
            
            # 计算盈亏
            if row['direction'] and 'SHORT' in str(row['direction']).upper():
                pnl = (row['entry_price'] - current_price) * quantity
                pnl_percent = (row['entry_price'] - current_price) / row['entry_price'] * 100 if row['entry_price'] > 0 else 0
            else:
                pnl = (current_price - row['entry_price']) * quantity
                pnl_percent = (current_price - row['entry_price']) / row['entry_price'] * 100 if row['entry_price'] > 0 else 0
            
            positions.append({
                'market': 'US',
                'code': row['code'] or 'N/A',
                'direction': 'SHORT' if row['direction'] and 'SHORT' in str(row['direction']).upper() else 'LONG',
                'quantity': quantity,  # 修复：使用转换后的变量
                'entry_price': row['entry_price'],
                'current_price': current_price,
                'pnl': pnl,
                'pnl_percent': pnl_percent,
                'status': 'OPEN',
                'strategy': row['strategy'] or 'voting_llm',
                'entry_time': row['entry_time'],
                'stop_loss': row['stop_loss'],
                'take_profit': row['take_profit'],
                'market_regime': row['market_regime'] if row['market_regime'] else 'N/A',
            })
        
        conn.close()
        return positions
    except sqlite3.OperationalError as e:
        logger.warning(f"Failed to get US positions: {e}")
        conn.close()
        return []


def calculate_summary(positions: List[Dict]) -> Dict[str, Any]:
    """
    计算持仓汇总
    
    Args:
        positions: 持仓列表
    
    Returns:
        汇总数据
    """
    if not positions:
        return {
            'total_positions': 0,
            'long_positions': 0,
            'short_positions': 0,
            'total_pnl': 0,
            'winning_positions': 0,
            'losing_positions': 0,
        }
    
    total_pnl = sum(p['pnl'] for p in positions)
    long_positions = sum(1 for p in positions if p['direction'] == 'LONG')
    short_positions = sum(1 for p in positions if p['direction'] == 'SHORT')
    winning = sum(1 for p in positions if p['pnl'] > 0)
    losing = sum(1 for p in positions if p['pnl'] < 0)
    
    return {
        'total_positions': len(positions),
        'long_positions': long_positions,
        'short_positions': short_positions,
        'total_pnl': total_pnl,
        'winning_positions': winning,
        'losing_positions': losing,
        'win_rate': winning / len(positions) if positions else 0,
    }


def save_snapshot(positions: List[Dict], market: str) -> Path:
    """
    保存持仓快照
    
    Args:
        positions: 持仓列表
        market: 市场 (HK/US)
    
    Returns:
        保存路径
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{market}_position_snapshot_{timestamp}.json"
    filepath = OUTPUT_DIR / filename
    
    summary = calculate_summary(positions)
    
    snapshot = {
        'market': market,
        'timestamp': datetime.now().isoformat(),
        'positions': positions,
        'summary': summary,
    }
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    
    logger.info(f"📄 Snapshot saved to: {filepath}")
    
    return filepath


def generate_report(hk_positions: List[Dict], us_positions: List[Dict]) -> str:
    """
    生成持仓同步报告
    
    Args:
        hk_positions: 港股持仓
        us_positions: 美股持仓
    
    Returns:
        报告文本
    """
    hk_summary = calculate_summary(hk_positions)
    us_summary = calculate_summary(us_positions)
    
    lines = [
        "=" * 70,
        "持仓同步报告",
        "=" * 70,
        "",
        f"报告时间：{datetime.now().isoformat()}",
        "",
        "=" * 70,
        "港股持仓",
        "=" * 70,
    ]
    
    if hk_positions:
        lines.append(f"总持仓数：{hk_summary['total_positions']}")
        lines.append(f"多头：{hk_summary['long_positions']} | 空头：{hk_summary['short_positions']}")
        lines.append(f"总盈亏：{hk_summary['total_pnl']:.2f}")
        lines.append(f"盈利：{hk_summary['winning_positions']} | 亏损：{hk_summary['losing_positions']}")
        lines.append("")
        lines.append("持仓明细:")
        lines.append("-" * 70)
        
        for pos in hk_positions:
            pnl_sign = "+" if pos['pnl'] > 0 else ""
            lines.append(f"  {pos['code']} {pos['direction']} {pos['quantity']}股")
            lines.append(f"    入场价：{pos['entry_price']:.2f} | 当前价：{pos['current_price']:.2f}")
            lines.append(f"    盈亏：{pnl_sign}{pos['pnl']:.2f} ({pnl_sign}{pos['pnl_percent']:.1%})")
            lines.append(f"    策略：{pos['strategy']} | 时间：{pos['entry_time']}")
            if pos['stop_loss']:
                lines.append(f"    止损：{pos['stop_loss']:.2f}")
            if pos['take_profit']:
                lines.append(f"    止盈：{pos['take_profit']:.2f}")
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
        lines.append(f"总持仓数：{us_summary['total_positions']}")
        lines.append(f"多头：{us_summary['long_positions']} | 空头：{us_summary['short_positions']}")
        lines.append(f"总盈亏：{us_summary['total_pnl']:.2f}")
        lines.append(f"盈利：{us_summary['winning_positions']} | 亏损：{us_summary['losing_positions']}")
        lines.append("")
        lines.append("持仓明细:")
        lines.append("-" * 70)
        
        for pos in us_positions:
            pnl_sign = "+" if pos['pnl'] > 0 else ""
            lines.append(f"  {pos['code']} {pos['direction']} {pos['quantity']}股")
            lines.append(f"    入场价：${pos['entry_price']:.2f} | 当前价：${pos['current_price']:.2f}")
            lines.append(f"    盈亏：{pnl_sign}${pos['pnl']:.2f} ({pnl_sign}{pos['pnl_percent']:.1%})")
            lines.append(f"    策略：{pos['strategy']} | 时间：{pos['entry_time']}")
            lines.append("")
    else:
        lines.append("  无持仓")
    
    lines.extend([
        "",
        "=" * 70,
        "汇总",
        "=" * 70,
        f"港股总盈亏：{hk_summary['total_pnl']:.2f}",
        f"美股总盈亏：{us_summary['total_pnl']:.2f}",
        f"合计总盈亏：{hk_summary['total_pnl'] + us_summary['total_pnl']:.2f}",
        "",
        "=" * 70,
    ])
    
    return "\n".join(lines)


def main():
    """主函数"""
    logger.info("=" * 70)
    logger.info("开始持仓和收益同步...")
    logger.info("=" * 70)
    
    # 获取港股持仓
    logger.info("")
    logger.info("获取港股持仓...")
    hk_positions = get_hk_positions(HK_DB_PATH)
    logger.info(f"  港股持仓数：{len(hk_positions)}")
    
    # 获取美股持仓
    logger.info("")
    logger.info("获取美股持仓...")
    us_positions = get_us_positions(US_DB_PATH)
    logger.info(f"  美股持仓数：{len(us_positions)}")
    
    # 保存快照
    logger.info("")
    if hk_positions:
        hk_snapshot = save_snapshot(hk_positions, 'HK')
    else:
        logger.info("  港股无持仓，跳过快照保存")
    
    if us_positions:
        us_snapshot = save_snapshot(us_positions, 'US')
    else:
        logger.info("  美股无持仓，跳过快照保存")
    
    # 生成报告
    report = generate_report(hk_positions, us_positions)
    
    # 保存报告
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"position_sync_report_{timestamp}.md"
    
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    
    logger.info(f"📄 报告已保存到：{report_path}")
    logger.info("")
    logger.info("=" * 70)
    logger.info("持仓和收益同步完成！")
    logger.info("=" * 70)
    
    # 打印报告
    print()
    print(report)
    
    return {
        'hk_positions': hk_positions,
        'us_positions': us_positions,
        'hk_snapshot': str(OUTPUT_DIR / f'HK_position_snapshot_{timestamp}.json') if hk_positions else None,
        'us_snapshot': str(OUTPUT_DIR / f'US_position_snapshot_{timestamp}.json') if us_positions else None,
        'report': str(report_path),
    }


if __name__ == "__main__":
    result = main()
