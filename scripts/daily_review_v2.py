#!/usr/bin/env python3
"""
FTSeek 每日交易复盘脚本 V2

增强功能:
1. 信号统计 (生成/执行/忽略)
2. 已实现盈亏计算
3. 已平仓交易明细
4. 策略表现归因
5. 交易-信号关联分析

使用方式:
    python3 scripts/daily_review_v2.py           # 今日复盘
    python3 scripts/daily_review_v2.py --date 2026-03-19
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.database import get_database
from app.notifier.dingtalk import DingTalkNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.daily_review_v2")


class DailyReviewerV2:
    """每日复盘分析器 V2 - 增强版"""
    
    def __init__(self):
        self.db = get_database()
        self.notifier = DingTalkNotifier()
    
    def get_trades(self, review_date: str) -> Tuple[List[Dict], List[Dict]]:
        """
        获取指定日期的交易记录
        
        Returns:
            Tuple[List[Dict], List[Dict]]: (今日新开仓交易, 今日平仓交易)
        """
        opened_trades = []
        closed_trades = []
        
        try:
            with self.db.get_connection() as conn:
                # 今日开仓的交易
                cursor = conn.execute("""
                    SELECT id, code, action, quantity, entry_price, entry_time,
                           stop_loss, take_profit, status, exit_price, realized_pnl,
                           exit_time, highest_price, lowest_price
                    FROM positions 
                    WHERE date(entry_time) = ?
                    ORDER BY entry_time
                """, (review_date,))
                rows = cursor.fetchall()
                
                for row in rows:
                    trade = {
                        'id': row[0],
                        'code': row[1],
                        'action': row[2],
                        'quantity': row[3],
                        'entry_price': row[4],
                        'entry_time': row[5],
                        'stop_loss': row[6],
                        'take_profit': row[7],
                        'status': row[8],
                        'exit_price': row[9],
                        'realized_pnl': row[10],
                        'exit_time': row[11],
                        'highest_price': row[12],
                        'lowest_price': row[13]
                    }
                    if trade['status'] == 'CLOSED':
                        closed_trades.append(trade)
                    else:
                        opened_trades.append(trade)
                        
        except Exception as e:
            logger.error(f"获取交易记录失败: {e}")
        
        return opened_trades, closed_trades
    
    def get_all_trades_today(self, review_date: str) -> List[Dict]:
        """获取今日所有交易记录 (含信号关联)"""
        trades = []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, code, action, quantity, entry_price, entry_time,
                           stop_loss, take_profit, status, exit_price, realized_pnl,
                           exit_time, highest_price, lowest_price, signal_id, strategy
                    FROM positions 
                    WHERE date(entry_time) = ?
                    ORDER BY entry_time
                """, (review_date,))
                rows = cursor.fetchall()
                
                for row in rows:
                    trades.append({
                        'id': row[0],
                        'code': row[1],
                        'action': row[2],
                        'quantity': row[3],
                        'entry_price': row[4],
                        'entry_time': row[5],
                        'stop_loss': row[6],
                        'take_profit': row[7],
                        'status': row[8],
                        'exit_price': row[9],
                        'realized_pnl': row[10],
                        'exit_time': row[11],
                        'highest_price': row[12],
                        'lowest_price': row[13],
                        'signal_id': row[14],
                        'strategy': row[15]
                    })
        except Exception as e:
            logger.error(f"获取交易记录失败: {e}")
        
        return trades
    
    def get_signals(self, review_date: str) -> Tuple[List[Dict], Dict]:
        """
        获取指定日期的信号记录 (优先从 signal_tracker JSON 文件读取)
        
        Returns:
            Tuple[List[Dict], Dict]: (信号列表, 信号统计)
        """
        signals = []
        stats = {
            'total': 0,
            'executed': 0,
            'rejected': 0,
            'pending': 0,
            'by_strategy': defaultdict(int),
            'by_code': defaultdict(int),
            'by_confidence': {'high': 0, 'medium': 0, 'low': 0},
            'reject_reasons': defaultdict(int)
        }
        
        # 1. 优先从 signal_tracker JSON 文件读取
        signal_file = Path(f'/opt/ftseek/FTSeek/data/signal_records/signals_{review_date}.json')
        if signal_file.exists():
            try:
                with open(signal_file, 'r', encoding='utf-8') as f:
                    signal_data = json.load(f)
                    
                for record in signal_data:
                    signal = {
                        'id': record['signal_id'],
                        'code': record['code'],
                        'strategy': record['strategy'],
                        'action': record['action'],
                        'price': record['signal_price'],
                        'quantity': record['quantity'],
                        'confidence': record['confidence'],
                        'consensus_score': record['consensus_score'],
                        'strategies': record.get('strategies', []),
                        'executed': record['executed'],
                        'execution_price': record.get('execution_price'),
                        'reject_reason': record.get('reject_reason'),
                        'pnl': record.get('pnl'),
                        'market_regime': record.get('market_regime'),
                        'created_at': record['timestamp']
                    }
                    signals.append(signal)
                    
                    # 统计
                    stats['total'] += 1
                    if record['executed']:
                        stats['executed'] += 1
                    elif record.get('reject_reason'):
                        stats['rejected'] += 1
                        # 记录拒绝原因
                        reason = record['reject_reason']
                        if 'cooldown' in reason.lower():
                            stats['reject_reasons']['冷却中'] += 1
                        elif '碎股' in reason:
                            stats['reject_reasons']['碎股限制'] += 1
                        elif 'value' in reason:
                            stats['reject_reasons']['代码错误'] += 1
                        else:
                            stats['reject_reasons']['其他'] += 1
                    else:
                        stats['pending'] += 1
                    
                    # 按策略统计
                    strategy = record['strategy'] or 'unknown'
                    stats['by_strategy'][strategy] += 1
                    
                    # 按标的统计
                    stats['by_code'][record['code']] += 1
                    
                    # 按置信度统计
                    conf = record.get('confidence', 0)
                    if conf >= 0.7:
                        stats['by_confidence']['high'] += 1
                    elif conf >= 0.5:
                        stats['by_confidence']['medium'] += 1
                    else:
                        stats['by_confidence']['low'] += 1
                        
                logger.info(f"从 signal_tracker 加载 {len(signals)} 条信号记录")
                return signals, stats
                    
            except Exception as e:
                logger.warning(f"读取 signal_tracker 失败: {e}")
        
        # 2. 回退到数据库查询
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, code, name, strategy, action, price, quantity,
                           technical_score, sentiment_score, confidence, reason,
                           status, created_at, confirmed_at, executed_at
                    FROM signals 
                    WHERE date(created_at) = ?
                    ORDER BY created_at
                """, (review_date,))
                rows = cursor.fetchall()
                
                for row in rows:
                    signal = {
                        'id': row[0],
                        'code': row[1],
                        'name': row[2],
                        'strategy': row[3],
                        'action': row[4],
                        'price': row[5],
                        'quantity': row[6],
                        'technical_score': row[7],
                        'sentiment_score': row[8],
                        'confidence': row[9],
                        'reason': row[10],
                        'status': row[11],
                        'created_at': row[12],
                        'confirmed_at': row[13],
                        'executed_at': row[14]
                    }
                    signals.append(signal)
                    
                    # 统计
                    stats['total'] += 1
                    status = signal['status']
                    if status == 'executed':
                        stats['executed'] += 1
                    elif status == 'pending':
                        stats['pending'] += 1
                    elif status in ['ignored', 'rejected']:
                        stats['rejected'] += 1
                    
                    # 按策略统计
                    strategy = signal['strategy'] or 'unknown'
                    stats['by_strategy'][strategy] += 1
                    
                    # 按标的统计
                    stats['by_code'][signal['code']] += 1
                    
        except Exception as e:
            logger.warning(f"获取信号记录失败: {e}")
        
        return signals, stats
    
    def get_open_positions(self) -> List[Dict]:
        """获取当前持仓"""
        positions = []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, code, action, quantity, entry_price, entry_time,
                           stop_loss, take_profit, status, highest_price, lowest_price,
                           unrealized_pnl
                    FROM positions 
                    WHERE status = 'OPEN'
                """)
                rows = cursor.fetchall()
                
                for row in rows:
                    positions.append({
                        'id': row[0],
                        'code': row[1],
                        'action': row[2],
                        'quantity': row[3],
                        'entry_price': row[4],
                        'entry_time': row[5],
                        'stop_loss': row[6],
                        'take_profit': row[7],
                        'status': row[8],
                        'highest_price': row[9],
                        'lowest_price': row[10],
                        'unrealized_pnl': row[11]
                    })
        except Exception as e:
            logger.warning(f"获取持仓失败: {e}")
        
        return positions
    
    def calculate_pnl_metrics(self, trades: List[Dict], closed_trades: List[Dict], open_positions: List[Dict] = None) -> Dict:
        """计算盈亏指标"""
        open_positions = open_positions or []
        
        # 今日交易统计
        total_trades = len(trades)
        buy_count = sum(1 for t in trades if t['action'] == 'BUY')
        sell_count = sum(1 for t in trades if t['action'] == 'SELL')
        total_volume = sum(t['quantity'] * t['entry_price'] for t in trades)
        
        # 当前持仓市值
        position_value = sum(p['quantity'] * p['entry_price'] for p in open_positions)
        unrealized_pnl = sum(p.get('unrealized_pnl', 0) or 0 for p in open_positions)
        
        # 今日已实现盈亏
        total_realized_pnl = sum(t.get('realized_pnl', 0) or 0 for t in closed_trades)
        win_trades = [t for t in closed_trades if (t.get('realized_pnl', 0) or 0) > 0]
        loss_trades = [t for t in closed_trades if (t.get('realized_pnl', 0) or 0) < 0]
        
        win_count = len(win_trades)
        loss_count = len(loss_trades)
        win_rate = win_count / len(closed_trades) * 100 if closed_trades else 0
        
        total_win = sum(t.get('realized_pnl', 0) or 0 for t in win_trades)
        total_loss = sum(t.get('realized_pnl', 0) or 0 for t in loss_trades)
        
        # 盈亏比
        avg_win = total_win / win_count if win_count else 0
        avg_loss = abs(total_loss / loss_count) if loss_count else 0
        profit_factor = total_win / abs(total_loss) if total_loss != 0 else float('inf') if total_win > 0 else 0
        
        # 按股票统计
        by_code = defaultdict(lambda: {
            'count': 0, 'volume': 0, 'buy': 0, 'sell': 0,
            'realized_pnl': 0, 'win_count': 0, 'loss_count': 0
        })
        for t in trades:
            code = t['code']
            by_code[code]['count'] += 1
            by_code[code]['volume'] += t['quantity'] * t['entry_price']
            if t['action'] == 'BUY':
                by_code[code]['buy'] += 1
            else:
                by_code[code]['sell'] += 1
        
        for t in closed_trades:
            code = t['code']
            pnl = t.get('realized_pnl', 0) or 0
            by_code[code]['realized_pnl'] += pnl
            if pnl > 0:
                by_code[code]['win_count'] += 1
            elif pnl < 0:
                by_code[code]['loss_count'] += 1
        
        return {
            'total_trades': total_trades,
            'total_volume': total_volume,
            'buy_count': buy_count,
            'sell_count': sell_count,
            'closed_trades': len(closed_trades),
            'open_positions': len(open_positions),
            'position_value': position_value,
            'unrealized_pnl': unrealized_pnl,
            'total_realized_pnl': total_realized_pnl,
            'win_count': win_count,
            'loss_count': loss_count,
            'win_rate': win_rate,
            'total_win': total_win,
            'total_loss': total_loss,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor,
            'by_code': dict(by_code)
        }
    
    def get_strategy_config(self) -> Dict:
        """获取当前策略配置"""
        try:
            config_path = Path('/opt/ftseek/FTSeek/config/strategy_params.json')
            if config_path.exists():
                with open(config_path, 'r') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"读取策略配置失败: {e}")
        
        # 默认配置
        return {
            'stop_loss_pct': -3.0,
            'take_profit_pct': 4.0,
            'partial_tp1_pct': 2.0,
            'partial_tp2_pct': 4.0,
            'trailing_tp_trigger_pct': 2.5,
            'intraday_stop_pct': -1.5,
            'max_holding_hours': 24
        }
    
    def generate_report(self, review_date: str) -> Dict:
        """生成完整复盘报告"""
        # 获取数据
        all_trades = self.get_all_trades_today(review_date)
        opened_trades, closed_trades = self.get_trades(review_date)
        signals, signal_stats = self.get_signals(review_date)
        open_positions = self.get_open_positions()
        pnl_metrics = self.calculate_pnl_metrics(all_trades, closed_trades, open_positions)
        strategy_config = self.get_strategy_config()
        
        report = {
            'date': review_date,
            'generated_at': datetime.now().isoformat(),
            'trades': {
                'all': all_trades,
                'opened': opened_trades,
                'closed': closed_trades
            },
            'signals': {
                'list': signals,
                'stats': signal_stats
            },
            'positions': open_positions,
            'pnl_metrics': pnl_metrics,
            'strategy_config': strategy_config
        }
        
        return report
    
    def save_report(self, report: Dict):
        """保存报告到文件"""
        report_dir = Path('/opt/ftseek/FTSeek/data/reviews')
        report_dir.mkdir(parents=True, exist_ok=True)
        
        date_str = report['date']
        
        # JSON 格式
        json_path = report_dir / f"{date_str}_review_v2.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        
        # Markdown 格式
        md_path = report_dir / f"{date_str}_eod_review_v2.md"
        md_content = self._format_markdown(report)
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        logger.info(f"报告已保存: {json_path}, {md_path}")
    
    def _format_markdown(self, report: Dict) -> str:
        """格式化 Markdown 报告"""
        date_str = report['date']
        gen_time = report['generated_at']

        # 安全获取数据，防止类型错误
        pnl = report.get('pnl_metrics', {})
        if not isinstance(pnl, dict):
            pnl = {}

        sig = report.get('signals', {}).get('stats', {})
        if not isinstance(sig, dict):
            sig = {}

        config = report.get('strategy_config', {})
        if not isinstance(config, dict):
            config = {}

        # 盈亏状态图标
        pnl_total = pnl.get('total_realized_pnl', 0) or 0
        pnl_icon = "📈" if pnl_total >= 0 else "📉"
        pnl_color = "green" if pnl_total >= 0 else "red"
        
        md = f"""# FTSeek 每日交易复盘 V2

**日期**: {date_str}
**生成时间**: {gen_time}

---

## 📊 今日盈亏总览

| 指标 | 数值 |
|------|------|
| **今日已实现盈亏** | {pnl_icon} HK${abs(pnl_total):,.0f} |
| **浮动盈亏** | HK${pnl.get('unrealized_pnl', 0):,.0f} |
| 总交易次数 | {pnl.get('total_trades', 0)} |
| 平仓交易 | {pnl.get('closed_trades', 0)} |
| **当前持仓** | {pnl.get('open_positions', 0)} 笔 |
| 持仓市值 | HK${pnl.get('position_value', 0):,.0f} |
| 交易金额 | HK${pnl.get('total_volume', 0):,.0f} |
| 买入/卖出 | {pnl.get('buy_count', 0)} / {pnl.get('sell_count', 0)} |

### 盈亏分解

| 指标 | 数值 |
|------|------|
| 盈利交易 | {pnl.get('win_count', 0)} 笔 |
| 亏损交易 | {pnl.get('loss_count', 0)} 笔 |
| **胜率** | {pnl.get('win_rate', 0):.1f}% |
| 总盈利 | HK${pnl.get('total_win', 0):,.0f} |
| 总亏损 | HK${abs(pnl.get('total_loss', 0)):.0f} |
| 平均盈利 | HK${pnl.get('avg_win', 0):,.0f} |
| 平均亏损 | HK${abs(pnl.get('avg_loss', 0)):.0f} |
| **盈亏比** | {pnl.get('profit_factor', 0):.2f} |

---

## 📈 分股票统计

| 股票 | 交易次数 | 买入 | 卖出 | 金额 | 已实现盈亏 | 盈/亏 |
|------|----------|------|------|------|------------|-------|
"""
        for code, data in pnl['by_code'].items():
            pnl_val = data['realized_pnl']
            pnl_str = f"HK${pnl_val:,.0f}"
            status = "✅" if pnl_val >= 0 else "❌"
            md += f"| {code} | {data['count']} | {data['buy']} | {data['sell']} | HK${data['volume']:,.0f} | {pnl_str} | {status} |\n"
        
        # 已平仓交易明细
        md += f"""
---

## 📋 已平仓交易明细

"""
        closed_trades = report['trades']['closed']
        if closed_trades:
            md += "| 时间 | 股票 | 策略 | 方向 | 数量 | 入场价 | 出场价 | 盈亏 | 盈亏% |\n"
            md += "|------|------|------|------|------|--------|--------|------|-------|\n"
            for t in closed_trades:
                pnl_val = t.get('realized_pnl', 0) or 0
                pnl_pct = (pnl_val / (t['entry_price'] * t['quantity'])) * 100 if t['entry_price'] and t['quantity'] else 0
                pnl_str = f"HK${pnl_val:,.0f}"
                status = "✅" if pnl_val >= 0 else "❌"
                entry_time = t['entry_time'].split('T')[1][:5] if 'T' in str(t['entry_time']) else str(t['entry_time'])[11:16]
                strategy = t.get('strategy', '-') or '-'
                md += f"| {entry_time} | {t['code']} | {strategy} | {t['action']} | {t['quantity']} | {t['entry_price']:.2f} | {t['exit_price']:.2f} | {pnl_str} | {pnl_pct:+.2f}% {status} |\n"
        else:
            md += "*今日无平仓交易*\n"
        
        # 信号-交易关联分析
        signals = report['signals']['list']
        trades = report['trades']['all']
        
        md += f"""
---

## 🔗 信号-交易关联分析

"""
        # 统计有信号关联的交易
        linked_trades = [t for t in trades if t.get('signal_id')]
        unlinked_trades = [t for t in trades if not t.get('signal_id')]
        
        md += f"| 指标 | 数值 |\n|------|------|\n"
        md += f"| 总交易 | {len(trades)} |\n"
        md += f"| 有信号关联 | {len(linked_trades)} |\n"
        md += f"| 无信号关联 | {len(unlinked_trades)} |\n\n"
        
        if linked_trades:
            md += "### 已关联交易\n\n"
            md += "| 股票 | 策略 | 信号ID | 盈亏 |\n|------|------|--------|------|\n"
            for t in linked_trades[:10]:  # 最多展示10条
                pnl = t.get('realized_pnl', 0) or 0
                signal_id = t.get('signal_id', '')[:8]
                md += f"| {t['code']} | {t.get('strategy', '-')} | {signal_id}... | HK${pnl:,.0f} |\n"
        
        if unlinked_trades:
            md += "\n### ⚠️ 未关联交易 (需检查)\n\n"
            md += "| 时间 | 股票 | 方向 | 数量 |\n|------|------|------|------|\n"
            for t in unlinked_trades[:10]:
                entry_time = t['entry_time'].split('T')[1][:5] if 'T' in str(t['entry_time']) else str(t['entry_time'])[11:16]
                md += f"| {entry_time} | {t['code']} | {t['action']} | {t['quantity']} |\n"
        
        # 信号统计
        md += f"""
---

## 📶 信号统计

| 指标 | 数值 |
|------|------|
| 总信号数 | {sig['total']} |
| 已执行 | {sig['executed']} |
| 被拒绝 | {sig['rejected']} |
| 待处理 | {sig['pending']} |
| 执行率 | {(sig['executed'] / sig['total'] * 100) if sig['total'] > 0 else 0:.1f}% |

"""
        if sig['by_confidence']['high'] > 0 or sig['by_confidence']['medium'] > 0:
            md += "### 置信度分布\n\n"
            md += "| 等级 | 数量 |\n|------|------|\n"
            md += f"| 高 (≥70%) | {sig['by_confidence']['high']} |\n"
            md += f"| 中 (50-70%) | {sig['by_confidence']['medium']} |\n"
            md += f"| 低 (<50%) | {sig['by_confidence']['low']} |\n\n"
        
        if sig['reject_reasons']:
            md += "### 拒绝原因\n\n"
            md += "| 原因 | 数量 |\n|------|------|\n"
            for reason, count in sig['reject_reasons'].items():
                md += f"| {reason} | {count} |\n"
            md += "\n"
        
        if sig['by_strategy']:
            md += "### 按策略统计\n\n"
            md += "| 策略 | 数量 | 执行 |\n|------|------|------|\n"
            for strategy, count in sig['by_strategy'].items():
                # 统计该策略的执行数
                exec_count = sum(1 for s in report['signals']['list'] if s.get('strategy') == strategy and s.get('executed'))
                md += f"| {strategy} | {count} | {exec_count} |\n"
        
        if sig['by_code']:
            md += "\n### 按标的统计\n\n"
            md += "| 标的 | 数量 |\n|------|------|\n"
            for code, count in sig['by_code'].items():
                md += f"| {code} | {count} |\n"
        
        # 当前持仓
        md += f"""
---

## 💼 当前持仓

"""
        positions = report.get('positions', [])
        if not isinstance(positions, list):
            positions = []

        if positions:
            md += "| 股票 | 方向 | 数量 | 入场价 | 入场时间 | 最高价 | 最低价 |\n"
            md += "|------|------|------|--------|----------|--------|--------|\n"
            for p in positions:
                entry_time = p['entry_time'].split('T')[1][:5] if 'T' in str(p['entry_time']) else str(p['entry_time'])[11:16]
                high = p.get('highest_price', p['entry_price']) or p['entry_price']
                low = p.get('lowest_price', p['entry_price']) or p['entry_price']
                md += f"| {p['code']} | {p['action']} | {p['quantity']} | {p['entry_price']:.2f} | {entry_time} | {high:.2f} | {low:.2f} |\n"
        else:
            md += "*无持仓*\n"
        
        # 策略配置
        md += f"""
---

## ⚙️ 策略配置

| 参数 | 值 |
|------|-----|
| 止损 | {config.get('stop_loss_pct', -3.0)}% |
| 止盈 | +{config.get('take_profit_pct', 4.0)}% |
| 分批止盈 | +{config.get('partial_tp1_pct', 2.0)}% / +{config.get('partial_tp2_pct', 4.0)}% |
| 移动止盈触发 | +{config.get('trailing_tp_trigger_pct', 2.5)}% |
| 日内止损 | {config.get('intraday_stop_pct', -1.5)}% |
| 最大持仓时间 | {config.get('max_holding_hours', 24)} 小时 |

---

## 📝 复盘总结

### 今日表现
- **盈亏**: {"盈利" if pnl_total >= 0 else "亏损"} HK${abs(pnl_total):,.0f}
- **胜率**: {pnl.get('win_rate', 0):.1f}% ({pnl.get('win_count', 0)}胜 / {pnl.get('loss_count', 0)}负)
- **信号执行率**: {(sig.get('executed', 0) / sig.get('total', 1) * 100) if sig.get('total', 0) > 0 else 0:.1f}%

### 改进建议

"""
        # 根据表现生成建议
        if pnl.get('win_rate', 0) < 50 and pnl.get('closed_trades', 0) >= 3:
            md += "⚠️ **胜率偏低** (<50%)，建议检查:\n"
            md += "- 入场时机是否合理\n"
            md += "- 止损是否严格执行\n"
            md += "- 策略参数是否需要调整\n\n"

        if pnl.get('profit_factor', 0) < 1.0 and pnl.get('closed_trades', 0) >= 3:
            md += "⚠️ **盈亏比不佳** (<1.0)，建议:\n"
            md += "- 扩大止盈目标\n"
            md += "- 缩小止损范围\n"
            md += "- 选择波动更大的标的\n\n"

        if sig.get('total', 0) > 0 and sig.get('executed', 0) == 0:
            md += "⚠️ **信号未执行**，建议检查:\n"
            md += "- 钉钉推送是否正常\n"
            md += "- 用户确认流程是否顺畅\n"
            md += "- 信号冷却时间是否过长\n\n"

        if pnl.get('closed_trades', 0) == 0:
            md += "📌 **今日无平仓**，建议:\n"
            md += "- 检查是否有持仓需要管理\n"
            md += "- 确认出场策略是否触发\n\n"
        
        md += """
---

*自动生成 by FTSeek V2*
"""
        return md
    
    def send_notification(self, report: Dict):
        """发送钉钉通知"""
        pnl = report.get('pnl_metrics', {})
        if not isinstance(pnl, dict):
            pnl = {}

        sig = report.get('signals', {}).get('stats', {})
        if not isinstance(sig, dict):
            sig = {}

        pnl_total = pnl.get('total_realized_pnl', 0) or 0
        pnl_icon = "📈" if pnl_total >= 0 else "📉"
        exec_rate = (sig.get('executed', 0) / sig.get('total', 1) * 100) if sig.get('total', 0) > 0 else 0
        open_positions = pnl.get('open_positions', 0)
        unrealized_pnl = pnl.get('unrealized_pnl', 0)
        
        message = f"""📊 **FTSeek 每日复盘 V2**

📅 **日期**: {report.get('date', 'unknown')}

---

### 💰 今日盈亏

{pnl_icon} **已实现盈亏**: HK${pnl_total:,.0f}
📉 **浮动盈亏**: HK${unrealized_pnl:,.0f}

| 指标 | 数值 |
|------|------|
| 平仓交易 | {pnl.get('closed_trades', 0)} 笔 |
| **当前持仓** | {open_positions} 笔 |
| 胜率 | {pnl.get('win_rate', 0):.1f}% |
| 盈亏比 | {pnl.get('profit_factor', 0):.2f} |

---

### 📶 信号统计

| 指标 | 数值 |
|------|------|
| 总信号 | {sig.get('total', 0)} |
| 已执行 | {sig.get('executed', 0)} |
| 被拒绝 | {sig.get('rejected', 0)} |
| 执行率 | {exec_rate:.1f}% |

---

详细报告: `data/reviews/{report['date']}_eod_review_v2.md`
"""
        
        try:
            self.notifier.send_text(message)
            logger.info("钉钉通知已发送")
        except Exception as e:
            logger.error(f"发送钉钉通知失败: {e}")


def main():
    parser = argparse.ArgumentParser(description='FTSeek 每日交易复盘 V2')
    parser.add_argument('--date', type=str, default=None, help='复盘日期 (YYYY-MM-DD)')
    parser.add_argument('--no-notify', action='store_true', help='不发送钉钉通知')
    args = parser.parse_args()
    
    review_date = args.date or date.today().isoformat()
    
    logger.info(f"开始生成复盘报告: {review_date}")
    
    reviewer = DailyReviewerV2()
    report = reviewer.generate_report(review_date)
    
    # 保存报告
    reviewer.save_report(report)
    
    # 发送通知
    if not args.no_notify:
        reviewer.send_notification(report)
    
    # 打印摘要
    pnl = report['pnl_metrics']
    sig = report['signals']['stats']
    
    print(f"\n{'='*50}")
    print(f"📊 FTSeek 每日复盘 V2 - {review_date}")
    print(f"{'='*50}")
    print(f"💰 已实现盈亏: HK${pnl['total_realized_pnl']:,.0f}")
    print(f"📈 平仓交易: {pnl['closed_trades']} 笔")
    print(f"🎯 胜率: {pnl['win_rate']:.1f}%")
    print(f"📊 盈亏比: {pnl['profit_factor']:.2f}")
    print(f"📶 信号: {sig['total']} 生成 / {sig['executed']} 执行")
    print(f"{'='*50}\n")


if __name__ == '__main__':
    main()