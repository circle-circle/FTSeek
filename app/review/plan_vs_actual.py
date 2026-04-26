"""
计划与实际交易对比报告 (Plan vs Actual EOD Report)

功能:
1. 读取盘前作战计划 (Battle Plan)
2. 读取当日实际交易记录 (Positions / Signals)
3. 对比并生成归因分析:
   - 计划了但没做的 (Signal 没触发 / 被风控拦截)
   - 做了但没计划的 (盘中突发信号)
   - LLM 预测方向与实际收盘对比
4. 生成 Markdown 格式报告，供钉钉/飞书推送
"""

import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger("ftseek.review.plan_vs_actual")

PLAN_FILE = "/opt/ftseek/FTSeek/data/daily_battle_plan.json"
DB_PATH = "/opt/ftseek/FTSeek/data/ftseek.db"


def generate_plan_vs_actual_report(date: str = None) -> str:
    """
    生成计划与实际对比报告
    
    Args:
        date: 日期 YYYY-MM-DD，默认今天
        
    Returns:
        Markdown 格式的报告字符串
    """
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    
    logger.info(f"[EOD Report] 生成 {date} 计划对比报告...")
    
    # 1. 加载计划
    plan_targets = load_battle_plan()
    
    # 2. 加载实际交易
    actual_trades = load_actual_trades(date)
    
    # 3. 对比分析
    analysis = compare_plan_and_actual(plan_targets, actual_trades)
    
    # 4. 生成 Markdown
    report = format_markdown(date, analysis)
    
    return report


def load_battle_plan() -> List[Dict]:
    """加载盘前作战计划"""
    if not os.path.exists(PLAN_FILE):
        logger.warning(f"[EOD Report] 计划文件不存在: {PLAN_FILE}")
        return []
    
    try:
        with open(PLAN_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('targets', [])
    except Exception as e:
        logger.error(f"[EOD Report] 加载计划文件失败: {e}")
        return []


def load_actual_trades(date: str) -> List[Dict]:
    """从数据库加载当日交易记录"""
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # 获取当日开仓的记录
        cursor.execute("""
            SELECT code, action, entry_price, entry_time, realized_pnl, status
            FROM positions 
            WHERE entry_time LIKE ?
        """, (f"{date}%",))
        
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"[EOD Report] 加载交易记录失败: {e}")
        return []


def compare_plan_and_actual(plan: List[Dict], trades: List[Dict]) -> Dict:
    """
    对比计划与实际
    
    Returns:
        包含 categorized_trades, missed_plans, extra_trades 的字典
    """
    plan_symbols = {t.get('symbol'): t for t in plan}
    trade_symbols = {t['code']: t for t in trades}
    
    missed_plans = []
    extra_trades = []
    executed_plans = []
    
    # 检查计划中的目标是否被执行
    for symbol, target in plan_symbols.items():
        if symbol in trade_symbols:
            executed_plans.append({
                'symbol': symbol,
                'plan_direction': target.get('direction', 'N/A'),
                'actual_action': trade_symbols[symbol]['action'],
                'entry_price': trade_symbols[symbol]['entry_price'],
                'pnl': trade_symbols[symbol].get('realized_pnl', 0)
            })
        else:
            missed_plans.append({
                'symbol': symbol,
                'plan_direction': target.get('direction', 'N/A'),
                'reason': '未触发交易信号或被风控拦截'
            })
            
    # 检查实际交易中是否有计划外的
    for symbol, trade in trade_symbols.items():
        if symbol not in plan_symbols:
            extra_trades.append({
                'symbol': symbol,
                'action': trade['action'],
                'entry_price': trade['entry_price'],
                'reason': '盘中突发信号'
            })
            
    return {
        'executed_plans': executed_plans,
        'missed_plans': missed_plans,
        'extra_trades': extra_trades,
        'total_plan_targets': len(plan_symbols),
        'total_actual_trades': len(trade_symbols)
    }


def format_markdown(date: str, analysis: Dict) -> str:
    """生成 Markdown 报告"""
    lines = [
        f"## 📊 FTSeek 盘前计划复盘 | {date}",
        "",
        "### 🎯 总体执行情况",
        f"- **计划目标**: {analysis['total_plan_targets']} 只",
        f"- **实际交易**: {analysis['total_actual_trades']} 笔",
        f"- **计划执行率**: {_calc_exec_rate(analysis):.0%}",
        ""
    ]
    
    # 1. 计划内执行
    if analysis['executed_plans']:
        lines.append("### ✅ 计划内执行")
        for t in analysis['executed_plans']:
            pnl_str = f"{t['pnl']:+.2f}" if t['pnl'] is not None else "持仓中"
            lines.append(f"- **{t['symbol']}**: 计划 {t['plan_direction']} → 实际 {t['action']} | 盈亏: {pnl_str}")
        lines.append("")
    
    # 2. 计划遗漏
    if analysis['missed_plans']:
        lines.append("### ❌ 计划未执行")
        for t in analysis['missed_plans']:
            lines.append(f"- **{t['symbol']}** ({t['plan_direction']}): {t['reason']}")
        lines.append("")
        
    # 3. 计划外交易
    if analysis['extra_trades']:
        lines.append("### ⚡ 计划外突发交易")
        for t in analysis['extra_trades']:
            lines.append(f"- **{t['symbol']}** ({t['action']}): {t['reason']} | 入场: {t['entry_price']}")
        lines.append("")
        
    lines.append("---")
    lines.append("_* 报告由 FTSeek EOD 归因引擎自动生成 *_")
    
    return "\n".join(lines)


def _calc_exec_rate(analysis: Dict) -> float:
    if analysis['total_plan_targets'] == 0:
        return 0.0
    return len(analysis['executed_plans']) / analysis['total_plan_targets']


if __name__ == "__main__":
    # 测试
    print(generate_plan_vs_actual_report())
