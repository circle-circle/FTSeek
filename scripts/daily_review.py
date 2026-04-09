#!/usr/bin/env python3
"""
FTSeek 每日交易复盘脚本

功能:
1. 统计今日交易数据
2. 分析策略表现
3. 计算绩效指标
4. 生成复盘报告
5. 推送钉钉通知

使用方式:
    python3 scripts/daily_review.py           # 今日复盘
    python3 scripts/daily_review.py --date 2026-03-19

定时任务 (crontab):
    30 16 * * 1-5 cd /opt/ftseek/FTSeek && python3 scripts/daily_review.py
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.storage.database import get_database
from app.notifier.dingtalk import DingTalkNotifier
from app.risk.dynamic_exit_manager import DynamicExitManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.daily_review")


class DailyReviewer:
    """每日复盘分析器"""
    
    def __init__(self):
        self.db = get_database()
        self.notifier = DingTalkNotifier()
        self.exit_manager = DynamicExitManager()
    
    def get_trades(self, review_date: str) -> List[Dict]:
        """获取指定日期的交易记录"""
        trades = []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, code, action, quantity, entry_price, entry_time,
                           stop_loss, take_profit, status, exit_price, realized_pnl
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
                        'realized_pnl': row[10]
                    })
        except Exception as e:
            logger.warning(f"获取交易记录失败: {e}")
        
        return trades
    
    def get_signals(self, review_date: str) -> List[Dict]:
        """获取指定日期的信号记录"""
        signals = []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, code, strategy, action, price, status, created_at
                    FROM signals 
                    WHERE date(created_at) = ?
                    ORDER BY created_at
                """, (review_date,))
                rows = cursor.fetchall()
                
                for row in rows:
                    signals.append({
                        'id': row[0],
                        'code': row[1],
                        'strategy': row[2],
                        'action': row[3],
                        'price': row[4],
                        'status': row[5],
                        'created_at': row[6]
                    })
        except Exception as e:
            logger.warning(f"获取信号记录失败: {e}")
        
        return signals
    
    def get_open_positions(self) -> List[Dict]:
        """获取当前持仓"""
        positions = []
        try:
            with self.db.get_connection() as conn:
                cursor = conn.execute("""
                    SELECT id, code, action, quantity, entry_price, entry_time,
                           stop_loss, take_profit, status
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
                        'take_profit': row[7]
                    })
        except Exception as e:
            logger.warning(f"获取持仓失败: {e}")
        
        return positions
    
    def calculate_metrics(self, trades: List[Dict], positions: List[Dict]) -> Dict:
        """计算绩效指标"""
        total_trades = len(trades)
        total_volume = sum(t['quantity'] * t['entry_price'] for t in trades)
        
        # 统计方向
        buy_count = sum(1 for t in trades if t['action'] == 'BUY')
        sell_count = sum(1 for t in trades if t['action'] == 'SELL')
        
        # 持仓分析
        total_position_value = sum(p['quantity'] * p['entry_price'] for p in positions)
        
        # 按股票统计
        by_code = {}
        for t in trades:
            code = t['code']
            if code not in by_code:
                by_code[code] = {'count': 0, 'volume': 0, 'buy': 0, 'sell': 0}
            by_code[code]['count'] += 1
            by_code[code]['volume'] += t['quantity'] * t['entry_price']
            if t['action'] == 'BUY':
                by_code[code]['buy'] += 1
            else:
                by_code[code]['sell'] += 1
        
        return {
            'total_trades': total_trades,
            'total_volume': total_volume,
            'buy_count': buy_count,
            'sell_count': sell_count,
            'open_positions': len(positions),
            'position_value': total_position_value,
            'by_code': by_code
        }
    
    def generate_report(self, review_date: str) -> Dict:
        """生成复盘报告"""
        trades = self.get_trades(review_date)
        signals = self.get_signals(review_date)
        positions = self.get_open_positions()
        metrics = self.calculate_metrics(trades, positions)
        
        report = {
            'date': review_date,
            'generated_at': datetime.now().isoformat(),
            'trades': trades,
            'signals': signals,
            'positions': positions,
            'metrics': metrics
        }
        
        return report
    
    def save_report(self, report: Dict):
        """保存报告到文件"""
        report_dir = Path('/opt/ftseek/FTSeek/data/reviews')
        report_dir.mkdir(parents=True, exist_ok=True)
        
        date_str = report['date']
        
        # JSON 格式
        json_path = report_dir / f"{date_str}_review.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        # Markdown 格式
        md_path = report_dir / f"{date_str}_eod_review.md"
        md_content = self._format_markdown(report)
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        
        logger.info(f"报告已保存: {json_path}, {md_path}")
    
    def _format_markdown(self, report: Dict) -> str:
        """格式化 Markdown 报告"""
        m = report['metrics']
        
        md = f"""# FTSeek 每日交易复盘

**日期**: {report['date']}
**生成时间**: {report['generated_at']}

---

## 📊 交易统计

| 指标 | 数值 |
|------|------|
| 总交易次数 | {m['total_trades']} |
| 买入次数 | {m['buy_count']} |
| 卖出次数 | {m['sell_count']} |
| 交易金额 | HK${m['total_volume']:,.0f} |
| 当前持仓 | {m['open_positions']} 笔 |
| 持仓市值 | HK${m['position_value']:,.0f} |

---

## 📈 分股票统计

| 股票 | 交易次数 | 买入 | 卖出 | 金额 |
|------|----------|------|------|------|
"""
        for code, data in m['by_code'].items():
            md += f"| {code} | {data['count']} | {data['buy']} | {data['sell']} | HK${data['volume']:,.0f} |\n"
        
        md += f"""
---

## 💼 当前持仓

"""
        if report['positions']:
            for p in report['positions']:
                md += f"- **{p['code']}**: {p['action']} {p['quantity']}股 @ {p['entry_price']:.2f}\n"
        else:
            md += "无持仓\n"
        
        md += f"""
---

## 📝 策略配置

- 止损: -3%
- 止盈: +4%
- 分批止盈: +2%/+4%
- 移动止盈: +2.5% 触发
- 日内止损: -1.5%
- 最大持仓: 24小时

---

*自动生成 by FTSeek*
"""
        return md
    
    def send_notification(self, report: Dict):
        """发送钉钉通知"""
        m = report['metrics']
        
        message = f"""
📊 **FTSeek 每日交易复盘**

📅 **日期**: {report['date']}

---

### 交易统计

| 指标 | 数值 |
|------|------|
| 总交易次数 | {m['total_trades']} |
| 买入/卖出 | {m['buy_count']} / {m['sell_count']} |
| 交易金额 | HK${m['total_volume']:,.0f} |
| 当前持仓 | {m['open_positions']} 笔 |
| 持仓市值 | HK${m['position_value']:,.0f} |

---

### 分股票统计

"""
        for code, data in m['by_code'].items():
            message += f"- **{code}**: {data['count']} 笔 ({data['buy']}买/{data['sell']}卖)\n"
        
        message += """
---

💡 **策略配置**: 止损-3% / 止盈+4%
"""
        
        try:
            self.notifier.send_text(message)
            logger.info("钉钉通知已发送")
        except Exception as e:
            logger.error(f"发送钉钉通知失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="FTSeek 每日交易复盘")
    parser.add_argument("--date", type=str, default=None, help="复盘日期 (YYYY-MM-DD)")
    parser.add_argument("--no-notify", action="store_true", help="不发送钉钉通知")
    args = parser.parse_args()
    
    review_date = args.date or date.today().isoformat()
    
    logger.info(f"开始复盘: {review_date}")
    
    reviewer = DailyReviewer()
    report = reviewer.generate_report(review_date)
    reviewer.save_report(report)
    
    if not args.no_notify:
        reviewer.send_notification(report)
    
    # 打印摘要
    m = report['metrics']
    print(f"\n{'='*50}")
    print(f"FTSeek 每日复盘 - {review_date}")
    print(f"{'='*50}")
    print(f"总交易: {m['total_trades']} 笔")
    print(f"买入: {m['buy_count']} / 卖出: {m['sell_count']}")
    print(f"交易金额: HK${m['total_volume']:,.0f}")
    print(f"当前持仓: {m['open_positions']} 笔")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()