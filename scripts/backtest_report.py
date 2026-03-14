#!/usr/bin/env python3
"""
FTSeek 策略回测和收益率报告脚本

功能:
- 从数据库读取历史信号
- 计算策略收益率
- 生成回测报告
- 推送到钉钉

使用方式:
    python3 scripts/backtest_report.py --days 30
    python3 scripts/backtest_report.py --monthly

定时任务 (crontab):
    0 9 * * 1 cd /opt/ftseek/FTSeek && python3 scripts/backtest_report.py --weekly >> logs/backtest.log 2>&1
"""
import sys
import os
import logging
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage.database import Database
from app.notifier.dingtalk import DingTalkNotifier
from app import config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger("ftseek.scripts.backtest")


def get_signals(db: Database, days: int = 30) -> List[Dict]:
    """获取指定天数内的信号"""
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, code, name, action, price, quantity, strategy, 
                   reason, created_at, executed_at, status
            FROM signals
            WHERE created_at >= datetime('now', ?)
            ORDER BY created_at DESC
        """, (f'-{days} days',))
        
        return [dict(row) for row in cursor.fetchall()]


def calculate_metrics(signals: List[Dict]) -> Dict:
    """
    计算策略指标
    
    Returns:
        指标字典
    """
    if not signals:
        return {
            'total_signals': 0,
            'buy_signals': 0,
            'sell_signals': 0,
            'executed_signals': 0,
            'win_rate': 0,
            'total_return': 0,
            'avg_return': 0,
            'max_drawdown': 0,
            'sharpe_ratio': 0,
        }
    
    buys = [s for s in signals if s['action'] == 'BUY']
    sells = [s for s in signals if s['action'] == 'SELL']
    executed = [s for s in signals if s['status'] == 'executed']
    
    # 按策略分组
    by_strategy = {}
    for s in signals:
        strategy = s.get('strategy', 'unknown')
        if strategy not in by_strategy:
            by_strategy[strategy] = {'buy': 0, 'sell': 0, 'executed': 0}
        by_strategy[strategy][s['action'].lower()] = by_strategy[strategy].get(s['action'].lower(), 0) + 1
        if s['status'] == 'executed':
            by_strategy[strategy]['executed'] = by_strategy[strategy].get('executed', 0) + 1
    
    # 简化收益率计算 (配对买卖)
    buys_sorted = sorted(buys, key=lambda x: x['created_at'])
    sells_sorted = sorted(sells, key=lambda x: x['created_at'])
    
    total_return = 0
    trades = 0
    wins = 0
    
    for i in range(min(len(buys_sorted), len(sells_sorted))):
        buy = buys_sorted[i]
        sell = sells_sorted[i]
        
        if sell['price'] > buy['price']:
            wins += 1
        
        ret = (sell['price'] - buy['price']) / buy['price']
        total_return += ret
        trades += 1
    
    win_rate = (wins / trades * 100) if trades > 0 else 0
    avg_return = (total_return / trades * 100) if trades > 0 else 0
    
    return {
        'total_signals': len(signals),
        'buy_signals': len(buys),
        'sell_signals': len(sells),
        'executed_signals': len(executed),
        'win_rate': win_rate,
        'total_return': total_return * 100,
        'avg_return': avg_return,
        'total_trades': trades,
        'by_strategy': by_strategy,
    }


def generate_report(metrics: Dict, days: int) -> str:
    """生成报告文本"""
    lines = [
        f"### 📊 FTSeek 策略回测报告\n\n",
        f"**统计周期**: 过去 {days} 天\n\n",
        f"**更新时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n",
        f"---\n\n",
        f"### 📈 核心指标\n\n",
        f"| 指标 | 数值 |\n",
        f"|------|------|\n",
        f"| 总信号数 | {metrics['total_signals']} 条 |\n",
        f"| 买入信号 | {metrics['buy_signals']} 条 |\n",
        f"| 卖出信号 | {metrics['sell_signals']} 条 |\n",
        f"| 已执行 | {metrics['executed_signals']} 条 |\n",
        f"| 交易次数 | {metrics['total_trades']} 笔 |\n",
        f"| **胜率** | **{metrics['win_rate']:.1f}%** |\n",
        f"| **总收益** | **{metrics['total_return']:+.2f}%** |\n",
        f"| 平均收益 | {metrics['avg_return']:+.2f}% |\n",
        f"\n",
    ]
    
    # 按策略统计
    if metrics.get('by_strategy'):
        lines.extend([
            f"---\n\n",
            f"### 🎯 策略表现\n\n",
            f"| 策略 | 买入 | 卖出 | 执行 |\n",
            f"|------|------|------|------|\n",
        ])
        
        for strategy, stats in metrics['by_strategy'].items():
            lines.append(
                f"| {strategy} | {stats.get('buy', 0)} | {stats.get('sell', 0)} | {stats.get('executed', 0)} |\n"
            )
    
    lines.extend([
        f"\n---\n\n",
        f"*FTSeek V3 - 智能量化交易系统*\n\n",
    ])
    
    return "".join(lines)


def send_report(notifier: DingTalkNotifier, report_text: str, metrics: Dict):
    """发送报告到钉钉"""
    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"📊 FTSeek 回测报告 (收益{metrics['total_return']:+.2f}%)",
                "text": report_text,
            },
            "at": {"isAtAll": False},
        }
        
        import requests
        import time
        import hmac
        import hashlib
        import base64
        import urllib.parse
        
        # 加签
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{notifier.secret}"
        hmac_code = hmac.new(
            notifier.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url = f"{notifier.webhook_url}&timestamp={timestamp}&sign={sign}"
        
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        
        if result.get("errcode") == 0:
            logger.info("钉钉报告推送成功")
            return True
        else:
            logger.error(f"钉钉推送失败：{result}")
            return False
            
    except Exception as e:
        logger.error(f"发送报告失败：{e}")
        return False


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='FTSeek 策略回测报告')
    parser.add_argument('--days', type=int, default=30, help='统计天数')
    parser.add_argument('--weekly', action='store_true', help='周报模式 (7 天)')
    parser.add_argument('--monthly', action='store_true', help='月报模式 (30 天)')
    args = parser.parse_args()
    
    if args.weekly:
        days = 7
    elif args.monthly:
        days = 30
    else:
        days = args.days
    
    logger.info("=" * 60)
    logger.info(f"📊 FTSeek 策略回测报告 (过去 {days} 天)")
    logger.info("=" * 60)
    
    # 初始化
    try:
        db = Database()
        notifier = DingTalkNotifier(
            webhook_url=config.DINGTALK_WEBHOOK_URL,
            secret=config.DINGTALK_SECRET,
        )
    except Exception as e:
        logger.error(f"初始化失败：{e}")
        return 1
    
    # 获取信号
    logger.info(f"读取历史信号...")
    signals = get_signals(db, days)
    logger.info(f"找到 {len(signals)} 条信号")
    
    if not signals:
        logger.info("无信号数据，跳过报告生成")
        return 0
    
    # 计算指标
    logger.info("计算策略指标...")
    metrics = calculate_metrics(signals)
    
    # 生成报告
    logger.info("生成报告...")
    report_text = generate_report(metrics, days)
    
    # 发送报告
    logger.info("发送报告到钉钉...")
    success = send_report(notifier, report_text, metrics)
    
    # 打印摘要
    print("\n" + "=" * 60)
    print("📊 回测摘要")
    print("=" * 60)
    print(f"统计周期：{days} 天")
    print(f"总信号数：{metrics['total_signals']} 条")
    print(f"交易次数：{metrics['total_trades']} 笔")
    print(f"胜率：{metrics['win_rate']:.1f}%")
    print(f"总收益：{metrics['total_return']:+.2f}%")
    print(f"平均收益：{metrics['avg_return']:+.2f}%")
    print("=" * 60)
    
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
