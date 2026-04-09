#!/usr/bin/env python3
"""
FTSeek 每日交易复盘脚本

功能：
1. 收集当日交易数据
2. 分析盈亏情况
3. 识别核心问题
4. 提出优化建议
5. 生成复盘报告

使用方式：
    python3 scripts/run_review.py                    # 今日复盘
    python3 scripts/run_review.py --date 2026-03-24  # 指定日期
    python3 scripts/run_review.py --output markdown  # 输出格式
"""

import os
import sys
import json
import sqlite3
import logging
import argparse
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Any, Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("ftseek.review")

# 默认项目路径
DEFAULT_PROJECT_PATH = "/opt/ftseek/FTSeek"


class DailyReviewer:
    """每日复盘分析器"""
    
    def __init__(self, project_path: str = DEFAULT_PROJECT_PATH, review_date: str = None):
        self.project_path = Path(project_path)
        self.review_date = review_date or date.today().isoformat()
        self.db_path = self.project_path / "data" / "ftseek.db"
        self.log_path = self.project_path / "logs" / "ftseek.log"
        self.signal_path = self.project_path / "data" / "signal_records" / f"signals_{self.review_date}.json"
        
    def collect_trades(self) -> List[Dict]:
        """收集当日交易记录"""
        trades = []
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT id, code, action, quantity, entry_price, entry_time,
                       exit_price, exit_time, realized_pnl, stop_loss, take_profit, status, strategy
                FROM positions 
                WHERE date(entry_time) = ?
                ORDER BY entry_time
            ''', (self.review_date,))
            
            rows = cursor.fetchall()
            
            for row in rows:
                id_, code, action, qty, entry_price, entry_time, exit_price, exit_time, pnl, sl, tp, status, strategy = row
                
                pnl_pct = 0
                if exit_price and entry_price:
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                    if action == 'SELL':
                        pnl_pct = -pnl_pct
                
                trades.append({
                    'id': id_,
                    'code': code,
                    'action': action,
                    'quantity': qty,
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'entry_time': entry_time,
                    'exit_time': exit_time,
                    'pnl': pnl or 0,
                    'pnl_pct': pnl_pct,
                    'stop_loss': sl,
                    'take_profit': tp,
                    'status': status,
                    'strategy': strategy
                })
            
            conn.close()
        except Exception as e:
            logger.error(f"收集交易记录失败: {e}")
            
        return trades
    
    def collect_signals(self) -> List[Dict]:
        """收集当日信号记录"""
        signals = []
        
        try:
            if self.signal_path.exists():
                with open(self.signal_path, 'r') as f:
                    signals = json.load(f)
        except Exception as e:
            logger.error(f"收集信号记录失败: {e}")
            
        return signals
    
    def analyze_pnl(self, trades: List[Dict]) -> Dict:
        """分析盈亏情况"""
        total_pnl = 0
        wins = 0
        losses = 0
        by_code = defaultdict(lambda: {'pnl': 0, 'trades': 0, 'wins': 0})
        
        for trade in trades:
            if trade['status'] == 'CLOSED' and trade['pnl'] is not None:
                pnl = trade['pnl']
                total_pnl += pnl
                
                if pnl > 0:
                    wins += 1
                    by_code[trade['code']]['wins'] += 1
                else:
                    losses += 1
                
                by_code[trade['code']]['pnl'] += pnl
                by_code[trade['code']]['trades'] += 1
        
        closed = wins + losses
        win_rate = (wins / closed * 100) if closed > 0 else 0
        
        return {
            'total_pnl': total_pnl,
            'wins': wins,
            'losses': losses,
            'closed': closed,
            'win_rate': win_rate,
            'by_code': dict(by_code)
        }
    
    def analyze_reject_reasons(self, signals: List[Dict]) -> Dict:
        """分析拒绝原因"""
        reasons = defaultdict(int)
        
        for signal in signals:
            if signal.get('reject_reason'):
                r = signal['reject_reason']
                if 'existing_position' in r:
                    reasons['已有持仓'] += 1
                elif '碎股' in r or '不足1手' in r:
                    reasons['碎股限制'] += 1
                elif 'weak_signal' in r.lower():
                    reasons['弱信号'] += 1
                elif 'trend_filter' in r.lower() or '趋势过滤' in r:
                    reasons['趋势过滤'] += 1
                elif '价格检查' in r:
                    reasons['价格检查'] += 1
                elif '持仓数' in r or '最大持仓' in r:
                    reasons['持仓限制'] += 1
                elif 'cooldown' in r.lower():
                    reasons['冷却中'] += 1
                else:
                    reasons['其他'] += 1
        
        return dict(reasons)
    
    def identify_issues(self, trades: List[Dict], signals: List[Dict]) -> List[Dict]:
        """识别核心问题"""
        issues = []
        
        # 检查大额亏损
        for trade in trades:
            if trade['pnl'] < -1000:
                pnl_pct = abs(trade['pnl_pct'])
                if pnl_pct > 5:
                    issues.append({
                        'severity': 'critical',
                        'type': '大额亏损',
                        'code': trade['code'],
                        'description': f"{trade['code']} 亏损 {trade['pnl']:.0f} HKD ({pnl_pct:.1f}%)，远超止损阈值",
                        'trade_id': trade['id']
                    })
        
        # 检查止损未触发
        for trade in trades:
            if trade['status'] == 'CLOSED' and trade['stop_loss'] and trade['exit_price']:
                if trade['action'] == 'BUY' and trade['exit_price'] < trade['stop_loss']:
                    issues.append({
                        'severity': 'high',
                        'type': '止损未触发',
                        'code': trade['code'],
                        'description': f"{trade['code']} 出场价 {trade['exit_price']:.2f} < 止损价 {trade['stop_loss']:.2f}",
                        'trade_id': trade['id']
                    })
        
        # 检查重复开仓
        entries_by_code = defaultdict(list)
        for trade in trades:
            if trade['status'] == 'OPEN' or trade['entry_time']:
                entries_by_code[trade['code']].append(trade['entry_time'])
        
        for code, times in entries_by_code.items():
            if len(times) > 1:
                # 检查是否有同一秒的开仓
                times_sorted = sorted(times)
                for i in range(len(times_sorted) - 1):
                    if times_sorted[i] == times_sorted[i+1]:
                        issues.append({
                            'severity': 'high',
                            'type': '重复开仓',
                            'code': code,
                            'description': f"{code} 在 {times_sorted[i][:19]} 同一秒内多次开仓",
                            'trade_id': None
                        })
        
        return issues
    
    def generate_recommendations(self, issues: List[Dict], pnl_analysis: Dict) -> List[Dict]:
        """生成优化建议"""
        recommendations = []
        
        for issue in issues:
            if issue['type'] == '大额亏损':
                recommendations.append({
                    'priority': 'P0',
                    'title': f"检查 {issue['code']} 止损逻辑",
                    'action': '核实止损是否正确触发，检查是否存在系统重启导致止损失效',
                    'expected_impact': '避免单笔大额亏损'
                })
            
            if issue['type'] == '重复开仓':
                recommendations.append({
                    'priority': 'P0',
                    'title': '修复重复开仓Bug',
                    'action': '添加信号去重锁，防止同一标的同一秒重复开仓',
                    'expected_impact': '避免重复开仓导致的敞口扩大'
                })
            
            if issue['type'] == '止损未触发':
                recommendations.append({
                    'priority': 'P1',
                    'title': '优化止损检查机制',
                    'action': '确保止损检查在系统重启后也能正常运行',
                    'expected_impact': '确保止损及时触发'
                })
        
        # 添加通用建议
        if pnl_analysis['win_rate'] < 50:
            recommendations.append({
                'priority': 'P1',
                'title': '提高信号质量',
                'action': '提高最低置信度阈值，优化策略参数',
                'expected_impact': f"提升胜率从 {pnl_analysis['win_rate']:.1f}% 到 55%+"
            })
        
        return recommendations
    
    def run(self) -> Dict:
        """执行复盘分析"""
        logger.info(f"开始复盘分析: {self.review_date}")
        
        trades = self.collect_trades()
        signals = self.collect_signals()
        pnl_analysis = self.analyze_pnl(trades)
        reject_reasons = self.analyze_reject_reasons(signals)
        issues = self.identify_issues(trades, signals)
        recommendations = self.generate_recommendations(issues, pnl_analysis)
        
        return {
            'date': self.review_date,
            'trades': trades,
            'signals': signals,
            'pnl_analysis': pnl_analysis,
            'reject_reasons': reject_reasons,
            'issues': issues,
            'recommendations': recommendations,
            'generated_at': datetime.now().isoformat()
        }


def format_markdown_report(result: Dict) -> str:
    """生成Markdown格式报告"""
    lines = []
    
    # 标题
    lines.append(f"# FTSeek 今日复盘报告（{result['date']}）")
    lines.append("")
    
    # 一、今日盈亏汇总
    lines.append("## 一、今日盈亏汇总")
    lines.append("")
    lines.append("| 标的 | 方向 | 数量 | 入场价 | 出场价 | 已实现盈亏 | 备注 |")
    lines.append("|------|------|------|--------|--------|------------|------|")
    
    for trade in result['trades']:
        if trade['status'] == 'CLOSED' and trade['exit_price']:
            pnl_str = f"{trade['pnl']:+,.0f}"
            pnl_pct = trade['pnl_pct']
            remark = f"{'盈利' if trade['pnl'] > 0 else '止损'} {pnl_pct:+.2f}%"
            if abs(pnl_pct) > 10:
                remark = f"⚠️ {remark}"
            entry = trade['entry_price'] if trade['entry_price'] else 0
            exit_p = trade['exit_price'] if trade['exit_price'] else 0
            lines.append(f"| {trade['code']} | {trade['action']} | {trade['quantity']} | {entry:.1f} | {exit_p:.1f} | {pnl_str} | {remark} |")
    
    lines.append("")
    pnl = result['pnl_analysis']
    lines.append(f"**今日已实现盈亏：{pnl['total_pnl']:+,.0f} 港元**")
    lines.append(f"**胜率：{pnl['win_rate']:.1f}% ({pnl['wins']}胜/{pnl['losses']}负)**")
    lines.append("")
    
    # 二、核心问题分析
    lines.append("## 二、核心问题分析")
    lines.append("")
    
    if result['issues']:
        for i, issue in enumerate(result['issues'], 1):
            severity = "🚨" if issue['severity'] == 'critical' else "⚠️"
            lines.append(f"### {severity} 问题{i}：{issue['description']}")
            lines.append("")
    else:
        lines.append("无明显问题")
        lines.append("")
    
    # 三、优化建议
    lines.append("## 三、优化建议")
    lines.append("")
    
    if result['recommendations']:
        for rec in result['recommendations']:
            lines.append(f"- **[{rec['priority']}] {rec['title']}**")
            lines.append(f"  - 行动：{rec['action']}")
            lines.append(f"  - 预期效果：{rec['expected_impact']}")
            lines.append("")
    
    # 四、待办事项
    lines.append("## 四、待办事项")
    lines.append("")
    lines.append("```")
    for rec in result['recommendations']:
        if rec['priority'] == 'P0':
            lines.append(f"[ ] {rec['title']}")
    lines.append("```")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='FTSeek 每日交易复盘')
    parser.add_argument('--date', type=str, help='复盘日期 (YYYY-MM-DD)')
    parser.add_argument('--output', type=str, default='markdown', choices=['markdown', 'json'], help='输出格式')
    parser.add_argument('--project', type=str, default=DEFAULT_PROJECT_PATH, help='项目路径')
    
    args = parser.parse_args()
    
    reviewer = DailyReviewer(args.project, args.date)
    result = reviewer.run()
    
    if args.output == 'json':
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_markdown_report(result))


if __name__ == '__main__':
    main()