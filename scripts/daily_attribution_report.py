#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日归因报告推送脚本

功能：
1. 生成交易归因报告 (7 日/30 日)
2. 生成策略健康度报告
3. 推送 DingTalk 通知
4. 可选：自动应用权重建议

使用方式:
- 手动运行：python scripts/daily_attribution_report.py
- 定时任务：添加到 crontab 或使用 CronCreate
"""
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek')

import logging
from datetime import datetime
from pathlib import Path

from app.config import DATA_DIR
from app.notifier.dingtalk import DingTalkNotifier

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger("ftseek.scripts.attribution")


def generate_attribution_report(days: int = 7):
    """
    生成归因报告

    Args:
        days: 报告天数范围

    Returns:
        报告字典
    """
    from app.analytics.attribution_engine import get_attribution_engine

    engine = get_attribution_engine()
    report = engine.generate_attribution_report(period=f"{days}d")

    return report


def generate_health_report():
    """生成策略健康度报告"""
    from app.analytics.strategy_health import get_strategy_health_tracker

    tracker = get_strategy_health_tracker()
    return tracker.get_health_report()


def format_dingtalk_message(report: dict, health_report: dict) -> str:
    """
    格式化 DingTalk 消息

    Args:
        report: 归因报告
        health_report: 健康度报告

    Returns:
        Markdown 格式消息
    """
    lines = []

    # 标题
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 FTSeek 交易归因日报")
    lines.append(f"🕐 生成时间：{datetime.now().strftime('%m-%d %H:%M')}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    # 关键发现
    if 'key_insights' in report and report['key_insights']:
        lines.append("🔑 *关键发现*")
        for insight in report['key_insights'][:5]:  # 最多 5 条
            lines.append(f"  • {insight}")
        lines.append("")

    # 策略表现
    if 'strategy_attribution' in report and report['strategy_attribution']:
        lines.append("📈 *策略表现*")
        strategies = sorted(report['strategy_attribution'],
                          key=lambda x: x.get('win_rate', 0), reverse=True)

        for i, s in enumerate(strategies[:4], 1):
            win_icon = "🟢" if s.get('win_rate', 0) >= 0.6 else "🟡" if s.get('win_rate', 0) >= 0.4 else "🔴"
            lines.append(
                f"  {i}.{win_icon} {s['strategy_name']}: "
                f"胜率{s.get('win_rate', 0):.0%} | "
                f"盈亏比{s.get('profit_factor', 0):.2f} | "
                f"评分{s.get('quality_score', 0):.0f}"
            )
        lines.append("")

    # 市场状态
    if 'market_regime_attribution' in report and report['market_regime_attribution']:
        lines.append("🌍 *市场状态适配*")
        for regime in report['market_regime_attribution'][:3]:
            lines.append(f"  {regime['regime_name']}: {regime.get('best_strategy', 'N/A')}")
        lines.append("")

    # 策略健康度
    if 'strategies' in health_report:
        lines.append("💚 *策略健康度*")
        for strategy, metrics in health_report['strategies'].items():
            status_icon = "🟢" if metrics['health_level'] == 'EXCELLENT' else \
                         "🟡" if metrics['health_level'] == 'GOOD' else \
                         "🟠" if metrics['health_level'] == 'NORMAL' else "🔴"

            lines.append(
                f"  {status_icon} {strategy}: {metrics['health_level']} "
                f"({metrics['overall_health_score']:.0f}分)"
            )
        lines.append("")

    # 权重建议
    if 'best_strategy' in report:
        lines.append(f"🏆 最佳策略：*{report['best_strategy']}*")
    if 'worst_strategy' in report:
        lines.append(f"📉 需优化：*{report['worst_strategy']}*")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"数据来源：signal_outcomes (最近{report.get('period', '7d')})")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    return "\n".join(lines)


def apply_weight_recommendations(auto_apply: bool = False) -> dict:
    """
    应用权重建议

    Args:
        auto_apply: 是否自动应用

    Returns:
        权重调整结果
    """
    from app.analytics.attribution_engine import get_attribution_engine
    from app.consensus.weight_calculator import WeightCalculator

    engine = get_attribution_engine()
    recommendations = engine.get_weight_recommendations(days=30)

    if not recommendations:
        return {'success': False, 'message': '无数据'}

    # 如果不自动应用，仅返回建议
    if not auto_apply:
        return {
            'success': True,
            'recommendations': recommendations,
            'message': '建议权重已生成，需手动应用'
        }

    # 自动应用权重建议
    calculator = WeightCalculator()
    updated = {}

    for strategy, rec_weight in recommendations.items():
        old_weight = calculator.base_weights.get(strategy, 1.0)

        # 平滑过渡：新旧权重加权平均
        new_weight = old_weight * 0.7 + rec_weight * 0.3

        # 限制调整幅度
        max_change = 0.3
        if abs(new_weight - old_weight) > max_change:
            if new_weight > old_weight:
                new_weight = old_weight + max_change
            else:
                new_weight = old_weight - max_change

        calculator.base_weights[strategy] = round(new_weight, 3)
        updated[strategy] = {'old': old_weight, 'new': new_weight}

    return {
        'success': True,
        'updated': updated,
        'message': f'已更新 {len(updated)} 个策略权重'
    }


def main():
    """主函数"""
    logger.info("=" * 60)
    logger.info("FTSeek 每日归因报告")
    logger.info(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    # 1. 生成归因报告
    logger.info("生成归因报告...")
    report_7d = generate_attribution_report(days=7)
    report_30d = generate_attribution_report(days=30)

    logger.info(f"归因报告生成完成：{len(report_7d.get('strategy_attribution', []))} 个策略")

    # 2. 生成健康度报告
    logger.info("生成策略健康度报告...")
    health_report = generate_health_report()

    logger.info(
        f"健康度报告生成完成：{len(health_report.get('strategies', {}))} 个策略"
    )

    # 3. 格式化 DingTalk 消息
    message = format_dingtalk_message(report_7d, health_report)

    # 4. 推送 DingTalk
    logger.info("推送 DingTalk 通知...")

    try:
        from app.notifier.dingtalk import DingTalkNotifier

        # 从环境变量或配置获取 webhook
        webhook_url = os.environ.get('DINGTALK_WEBHOOK_URL', '')
        secret = os.environ.get('DINGTALK_SECRET', '')

        if not webhook_url:
            logger.warning("DINGTALK_WEBHOOK_URL 未设置，跳过推送")
            print("\n" + message + "\n")  # 打印到控制台
        else:
            notifier = DingTalkNotifier(webhook_url=webhook_url, secret=secret)
            result = notifier.send_markdown(title="📊 FTSeek 交易归因日报", text=message)

            if result.get('success', False):
                logger.info(f"✅ DingTalk 推送成功：{result}")
            else:
                logger.error(f"❌ DingTalk 推送失败：{result}")

    except Exception as e:
        logger.error(f"DingTalk 推送异常：{e}", exc_info=True)
        print("\n" + message + "\n")  # 打印到控制台

    # 5. 生成权重建议
    logger.info("生成权重建议...")
    weight_result = apply_weight_recommendations(auto_apply=False)

    if weight_result.get('success'):
        logger.info("权重建议:")
        for strategy, weight in sorted(weight_result.get('recommendations', {}).items(),
                                       key=lambda x: x[1], reverse=True):
            logger.info(f"  {strategy}: {weight:.3f}")

    # 6. 保存报告到文件
    output_dir = DATA_DIR / "attribution_reports"
    output_dir.mkdir(parents=True, exist_ok=True)

    report_file = output_dir / f"attribution_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    import json
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump({
            'report_7d': report_7d,
            'report_30d': report_30d,
            'health_report': health_report,
            'weight_recommendations': weight_result.get('recommendations', {}),
            'generated_at': datetime.now().isoformat(),
        }, f, indent=2, ensure_ascii=False)

    logger.info(f"✅ 报告已保存：{report_file}")

    logger.info("=" * 60)
    logger.info("每日归因报告完成")
    logger.info("=" * 60)

    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
