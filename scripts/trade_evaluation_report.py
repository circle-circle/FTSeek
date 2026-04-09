#!/usr/bin/env python3
"""
FTSeek 交易评估报告生成器

生成周度/月度交易评估报告，包含:
1. 交易决策质量分析
2. 入场/出场时机评估
3. 盈亏分析
4. 优化建议

使用方式:
    python3 scripts/trade_evaluation_report.py --weekly
    python3 scripts/trade_evaluation_report.py --monthly --month 2026-03
"""

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.analytics.trade_evaluation import get_evaluator
from app.notifier.dingtalk import DingTalkNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("ftseek.evaluation_report")


def generate_weekly_report(week_start: str = None, push: bool = True) -> dict:
    """
    生成周度交易评估报告
    
    Args:
        week_start: 周开始日期 (YYYY-MM-DD)
        push: 是否推送钉钉
        
    Returns:
        报告数据
    """
    evaluator = get_evaluator()
    
    # 获取汇总数据
    summary = evaluator.get_weekly_summary(week_start)
    
    if not summary or summary.get('total_trades', 0) == 0:
        logger.info("本周无交易记录")
        return {"message": "本周无交易记录"}
    
    # 获取优化建议
    suggestions = evaluator.get_optimization_suggestions(days=7)
    
    # 获取详细评估记录
    evaluations = evaluator.get_recent_evaluations(days=7)
    
    # 构建报告
    report = {
        "period": summary.get('period', ''),
        "summary": {
            "总交易数": summary.get('total_trades', 0),
            "盈利次数": summary.get('winning_trades', 0),
            "胜率": f"{summary.get('win_rate', 0):.1f}%",
            "总盈亏": f"HK$ {summary.get('total_pnl', 0):+,.2f}",
            "平均盈亏": f"{summary.get('avg_pnl_pct', 0):+.2f}%",
        },
        "scores": {
            "入场平均分": f"{summary.get('avg_entry_score', 0):.0f}",
            "出场平均分": f"{summary.get('avg_exit_score', 0):.0f}",
            "综合平均分": f"{summary.get('avg_overall_score', 0):.0f}",
        },
        "quality_distribution": summary.get('quality_distribution', {}),
        "by_code": summary.get('by_code', []),
        "suggestions": suggestions,
        "top_trades": _get_top_trades(evaluations, 5),
        "improvement_trades": _get_improvement_trades(evaluations, 3)
    }
    
    # 推送钉钉
    if push:
        _push_weekly_report(report)
    
    return report


def generate_monthly_report(month: str = None, push: bool = True) -> dict:
    """
    生成月度交易评估报告
    
    Args:
        month: 月份 (YYYY-MM)
        push: 是否推送钉钉
        
    Returns:
        报告数据
    """
    evaluator = get_evaluator()
    
    # 获取汇总数据
    summary = evaluator.get_monthly_summary(month)
    
    if not summary or summary.get('total_trades', 0) == 0:
        logger.info("本月无交易记录")
        return {"message": "本月无交易记录"}
    
    # 获取优化建议
    suggestions = evaluator.get_optimization_suggestions(days=30)
    
    # 构建报告
    report = {
        "period": summary.get('period', ''),
        "summary": {
            "总交易数": summary.get('total_trades', 0),
            "盈利次数": summary.get('winning_trades', 0),
            "胜率": f"{summary.get('win_rate', 0):.1f}%",
            "总盈亏": f"HK$ {summary.get('total_pnl', 0):+,.2f}",
            "平均盈亏": f"{summary.get('avg_pnl_pct', 0):+.2f}%",
            "最佳交易": f"HK$ {summary.get('best_trade', 0):+,.2f}",
            "最差交易": f"HK$ {summary.get('worst_trade', 0):+,.2f}",
        },
        "scores": {
            "综合平均分": f"{summary.get('avg_score', 0):.0f}",
        },
        "weekly_trend": summary.get('weekly_trend', []),
        "by_strategy": summary.get('by_strategy', []),
        "suggestions": suggestions
    }
    
    # 推送钉钉
    if push:
        _push_monthly_report(report)
    
    return report


def _get_top_trades(evaluations: list, limit: int = 5) -> list:
    """获取最佳交易"""
    sorted_trades = sorted(
        evaluations, 
        key=lambda x: x.get('overall_score', 0),
        reverse=True
    )
    return sorted_trades[:limit]


def _get_improvement_trades(evaluations: list, limit: int = 3) -> list:
    """获取需要改进的交易"""
    sorted_trades = sorted(
        evaluations, 
        key=lambda x: x.get('overall_score', 0)
    )
    return sorted_trades[:limit]


def _push_weekly_report(report: dict):
    """推送周度报告到钉钉"""
    try:
        notifier = DingTalkNotifier()
        
        # 格式化质量分布
        quality_dist = report.get('quality_distribution', {})
        quality_str = "\n".join([
            f"  {'✅' if q == 'excellent' else '✅' if q == 'good' else '⚠️' if q == 'fair' else '🔴'} {q}: {c}笔"
            for q, c in quality_dist.items()
        ]) or "暂无数据"
        
        # 格式化股票统计
        by_code = report.get('by_code', [])
        by_code_str = "\n".join([
            f"  {c['code']}: {c['trades']}笔, 盈亏 HK${c['pnl']:+,.0f}"
            for c in by_code[:5]
        ]) or "暂无数据"
        
        # 格式化建议
        suggestions = report.get('suggestions', [])
        suggestions_str = "\n".join([
            f"  {'🔴' if s.get('priority') == 'high' else '⚠️' if s.get('priority') == 'medium' else '💡'} {s.get('message', '')}"
            for s in suggestions[:5]
        ]) or "暂无建议"
        
        message = f"""📊 周度交易评估报告
━━━━━━━━━━━━━━━━━━━━
📅 报告周期: {report.get('period', '')}
━━━━━━━━━━━━━━━━━━━━
📈 **交易统计**
{json.dumps(report.get('summary', {}), ensure_ascii=False, indent=2)}
━━━━━━━━━━━━━━━━━━━━
🎯 **决策评分**
{json.dumps(report.get('scores', {}), ensure_ascii=False, indent=2)}
━━━━━━━━━━━━━━━━━━━━
📊 **质量分布**
{quality_str}
━━━━━━━━━━━━━━━━━━━━
📋 **按股票统计**
{by_code_str}
━━━━━━━━━━━━━━━━━━━━
💡 **优化建议**
{suggestions_str}
"""
        
        notifier._send_markdown(message)
        logger.info("周度评估报告推送成功")
        
    except Exception as e:
        logger.error(f"推送周度报告失败: {e}")


def _push_monthly_report(report: dict):
    """推送月度报告到钉钉"""
    try:
        notifier = DingTalkNotifier()
        
        # 格式化周趋势
        weekly_trend = report.get('weekly_trend', [])
        weekly_str = "\n".join([
            f"  {w['week']}: {w['trades']}笔, 盈亏 HK${w['pnl']:+,.0f}"
            for w in weekly_trend
        ]) or "暂无数据"
        
        # 格式化策略统计
        by_strategy = report.get('by_strategy', [])
        strategy_str = "\n".join([
            f"  {s['strategy']}: {s['trades']}笔, 盈亏 HK${s['pnl']:+,.0f}, 得分 {s['avg_score']:.0f}"
            for s in by_strategy[:5]
        ]) or "暂无数据"
        
        # 格式化建议
        suggestions = report.get('suggestions', [])
        suggestions_str = "\n".join([
            f"  {'🔴' if s.get('priority') == 'high' else '⚠️' if s.get('priority') == 'medium' else '💡'} {s.get('message', '')}"
            for s in suggestions[:5]
        ]) or "暂无建议"
        
        message = f"""📊 月度交易评估报告
━━━━━━━━━━━━━━━━━━━━
📅 报告周期: {report.get('period', '')}
━━━━━━━━━━━━━━━━━━━━
📈 **月度统计**
{json.dumps(report.get('summary', {}), ensure_ascii=False, indent=2)}
━━━━━━━━━━━━━━━━━━━━
📊 **周趋势**
{weekly_str}
━━━━━━━━━━━━━━━━━━━━
🎯 **策略表现**
{strategy_str}
━━━━━━━━━━━━━━━━━━━━
💡 **优化建议**
{suggestions_str}
"""
        
        notifier._send_markdown(message)
        logger.info("月度评估报告推送成功")
        
    except Exception as e:
        logger.error(f"推送月度报告失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="FTSeek 交易评估报告")
    parser.add_argument("--weekly", action="store_true", help="生成周度报告")
    parser.add_argument("--monthly", action="store_true", help="生成月度报告")
    parser.add_argument("--month", type=str, help="指定月份 (YYYY-MM)")
    parser.add_argument("--no-push", action="store_true", help="不推送钉钉")
    
    args = parser.parse_args()
    
    if args.weekly:
        report = generate_weekly_report(push=not args.no_push)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    elif args.monthly:
        report = generate_monthly_report(month=args.month, push=not args.no_push)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()