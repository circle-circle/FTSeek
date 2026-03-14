#!/usr/bin/env python3
"""
FTSeek 每日绩效汇总定时脚本

每个交易日 16:30 自动执行，生成并推送当日绩效到钉钉群

使用方法:
    python3 scripts/daily_performance_summary.py --date today
    python3 scripts/daily_performance_summary.py --date today --push

定时任务 (crontab):
    30 16 * * 1-5 cd /opt/ftseek/FTSeek && export $(cat .env | grep -v '^#' | xargs) && python3 scripts/daily_performance_summary.py --push >> logs/daily_performance.log 2>&1

环境变量:
    DINGTALK_WEBHOOK_URL: 钉钉机器人 Webhook URL
    DINGTALK_SECRET: 钉钉机器人加签 Secret
"""
import sys
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta

# 添加项目根目录到 Python 路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from app.performance.tracker import PerformanceTracker


# 配置日志
log_file = project_root / "logs" / "daily_performance.log"
log_file.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("ftseek.scripts.daily_performance_summary")


def format_performance_report(performance_data: dict, date: str) -> str:
    """
    格式化绩效报告为文本
    
    Args:
        performance_data: 绩效数据
        date: 报告日期
    
    Returns:
        格式化后的文本报告
    """
    lines = []
    lines.append("=" * 60)
    lines.append("📊 FTSeek 每日绩效汇总")
    lines.append("=" * 60)
    lines.append(f"日期：{date}")
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 60)
    
    # 总览
    if 'summary' in performance_data:
        summary = performance_data['summary']
        lines.append("\n📈 总览")
        lines.append("-" * 60)
        
        if 'total_trades' in summary:
            lines.append(f"  总交易次数：{summary['total_trades']} 笔")
        if 'total_profit' in summary:
            lines.append(f"  总盈亏：{summary['total_profit']:.2f} HKD")
        if 'win_rate' in summary:
            lines.append(f"  胜率：{summary['win_rate']:.1%}")
        if 'profit_rate' in summary:
            lines.append(f"  收益率：{summary['profit_rate']:.2%}")
    
    # 按策略统计
    if 'by_strategy' in performance_data and performance_data['by_strategy']:
        lines.append("\n🎯 按策略统计")
        lines.append("-" * 60)
        for strategy_name, stats in performance_data['by_strategy'].items():
            lines.append(f"  【{strategy_name}】")
            if 'trades' in stats:
                lines.append(f"    交易次数：{stats['trades']} 笔")
            if 'profit' in stats:
                lines.append(f"    盈亏：{stats['profit']:.2f} HKD")
            if 'win_rate' in stats:
                lines.append(f"    胜率：{stats['win_rate']:.1%}")
    
    # 按股票统计
    if 'by_stock' in performance_data and performance_data['by_stock']:
        lines.append("\n💼 按股票统计")
        lines.append("-" * 60)
        for stock_code, stats in performance_data['by_stock'].items():
            lines.append(f"  【{stock_code}】")
            if 'trades' in stats:
                lines.append(f"    交易次数：{stats['trades']} 笔")
            if 'profit' in stats:
                lines.append(f"    盈亏：{stats['profit']:.2f} HKD")
    
    # 最佳交易
    if 'best_trades' in performance_data and performance_data['best_trades']:
        lines.append("\n🏆 最佳交易")
        lines.append("-" * 60)
        for i, trade in enumerate(performance_data['best_trades'][:3], 1):
            if 'profit' in trade and 'stock_code' in trade:
                lines.append(f"  {i}. {trade['stock_code']}: {trade['profit']:.2f} HKD")
    
    lines.append("\n" + "=" * 60)
    
    return "\n".join(lines)


def get_daily_performance(tracker: PerformanceTracker, date: str) -> dict:
    """
    获取指定日期的绩效数据
    
    Args:
        tracker: 绩效追踪器
        date: 日期 (YYYY-MM-DD 或 'today')
    
    Returns:
        绩效数据字典
    """
    if date == 'today':
        target_date = datetime.now().strftime('%Y-%m-%d')
    else:
        target_date = date
    
    # 获取所有配对的交易
    all_trades = tracker.match_trades()
    
    # 过滤出指定日期的交易（按平仓时间）
    trades = []
    for trade in all_trades:
        close_time = trade.get('close_time', '')
        if close_time and close_time.startswith(target_date):
            trades.append(trade)
    
    performance_data = {
        'summary': {
            'total_trades': len(trades),
            'total_profit': 0.0,
            'win_count': 0,
            'loss_count': 0,
            'win_rate': 0.0,
            'profit_rate': 0.0
        },
        'by_strategy': {},
        'by_stock': {},
        'best_trades': []
    }
    
    if not trades:
        logger.info(f"当日 ({target_date}) 无交易记录")
        return performance_data
    
    # 计算总盈亏
    total_profit = sum(t.get('profit', 0) for t in trades)
    win_trades = [t for t in trades if t.get('profit', 0) > 0]
    loss_trades = [t for t in trades if t.get('profit', 0) < 0]
    
    performance_data['summary']['total_profit'] = total_profit
    performance_data['summary']['win_count'] = len(win_trades)
    performance_data['summary']['loss_count'] = len(loss_trades)
    
    if trades:
        performance_data['summary']['win_rate'] = len(win_trades) / len(trades)
    
    # 按策略统计
    strategy_stats = {}
    for trade in trades:
        strategy = trade.get('strategy', trade.get('name', 'unknown'))
        if strategy not in strategy_stats:
            strategy_stats[strategy] = {'trades': 0, 'profit': 0.0, 'wins': 0}
        strategy_stats[strategy]['trades'] += 1
        strategy_stats[strategy]['profit'] += trade.get('profit', 0)
        if trade.get('profit', 0) > 0:
            strategy_stats[strategy]['wins'] += 1
    
    for strategy, stats in strategy_stats.items():
        performance_data['by_strategy'][strategy] = {
            'trades': stats['trades'],
            'profit': stats['profit'],
            'win_rate': stats['wins'] / stats['trades'] if stats['trades'] > 0 else 0
        }
    
    # 按股票统计
    stock_stats = {}
    for trade in trades:
        stock_code = trade.get('code', 'unknown')
        if stock_code not in stock_stats:
            stock_stats[stock_code] = {'trades': 0, 'profit': 0.0}
        stock_stats[stock_code]['trades'] += 1
        stock_stats[stock_code]['profit'] += trade.get('profit', 0)
    
    performance_data['by_stock'] = stock_stats
    
    # 最佳交易（按盈亏排序）
    sorted_trades = sorted(trades, key=lambda x: x.get('profit', 0), reverse=True)
    performance_data['best_trades'] = sorted_trades[:5]
    
    return performance_data


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='FTSeek 每日绩效汇总定时脚本')
    parser.add_argument(
        '--date',
        type=str,
        default='today',
        help='报告日期 (YYYY-MM-DD 或 today), 默认：today'
    )
    parser.add_argument(
        '--push',
        action='store_true',
        help='推送到钉钉'
    )
    parser.add_argument(
        '--db',
        type=str,
        default=None,
        help='数据库路径'
    )
    
    args = parser.parse_args()
    
    logger.info("=" * 50)
    logger.info("开始执行每日绩效汇总")
    logger.info(f"报告日期：{args.date}")
    logger.info(f"执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 初始化绩效追踪器
    db_path = args.db or '/opt/ftseek/FTSeek/data/ftseek.db'
    
    try:
        tracker = PerformanceTracker(db_path=db_path)
        logger.info("绩效追踪器初始化成功")
    except Exception as e:
        logger.error(f"初始化绩效追踪器失败：{e}", exc_info=True)
        return 1
    
    # 获取当日绩效数据
    try:
        performance_data = get_daily_performance(tracker, args.date)
        logger.info("绩效数据获取成功")
    except Exception as e:
        logger.error(f"获取绩效数据失败：{e}", exc_info=True)
        return 1
    
    # 格式化报告
    try:
        report_text = format_performance_report(performance_data, args.date)
        logger.info("报告格式化成功")
    except Exception as e:
        logger.error(f"格式化报告失败：{e}", exc_info=True)
        return 1
    
    # 输出报告到日志
    logger.info("\n" + report_text)
    
    # 推送到钉钉
    if args.push:
        from app import config
        import hashlib
        import hmac
        import base64
        import urllib.parse
        import requests
        
        webhook_url = config.DINGTALK_WEBHOOK_URL
        secret = config.DINGTALK_SECRET
        
        if not webhook_url:
            logger.warning("⚠️  DINGTALK_WEBHOOK_URL 未配置，跳过推送")
            return 0
        
        try:
            # 生成签名
            timestamp = str(round(datetime.now().timestamp() * 1000))
            sign_str = f"{timestamp}\n{secret}"
            sign = base64.b64encode(
                hmac.new(
                    secret.encode('utf-8'),
                    sign_str.encode('utf-8'),
                    digestmod=hashlib.sha256
                ).digest()
            ).decode()
            sign = urllib.parse.quote(sign, safe='')
            
            # 发送消息
            full_url = f"{webhook_url}&timestamp={timestamp}&sign={sign}"
            
            headers = {'Content-Type': 'application/json'}
            data = {
                "msgtype": "markdown",
                "markdown": {
                    "title": "FTSeek 每日绩效汇总",
                    "text": report_text.replace("\n", "\n\n")  # Markdown 格式优化
                }
            }
            
            response = requests.post(full_url, json=data, headers=headers, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('errcode') == 0:
                    logger.info("✅ 绩效汇总已推送到钉钉")
                    return 0
                else:
                    logger.error(f"❌ 钉钉推送失败：{result}")
                    return 1
            else:
                logger.error(f"❌ 钉钉推送失败：HTTP {response.status_code}")
                return 1
                
        except Exception as e:
            logger.error(f"钉钉推送异常：{e}", exc_info=True)
            return 1
    
    logger.info("绩效汇总生成完成（未推送）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
