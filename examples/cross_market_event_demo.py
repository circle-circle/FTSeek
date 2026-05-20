#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨市场套利与事件驱动策略集成示例

演示如何集成使用:
1. Lead-Lag 追踪器
2. 新闻 - 股票映射器
3. 动态仓位计算器
4. 影响持续时间预测器
5. 事件绩效追踪器

使用场景:
- 美股夜间大涨→港股开盘套利
- 突发新闻→事件驱动交易
- ADR 溢价/折价套利
"""

import sys
import time
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategy.lead_lag_tracker import get_lead_lag_tracker, LagMetrics
from app.factors.news_mapper import get_news_mapper, NewsMappingResult
from app.risk.dynamic_sizer import get_dynamic_positionSizer, DynamicRiskParams
from app.factors.duration_predictor import get_duration_predictor, DurationPrediction
from app.consensus.event_performance_tracker import get_event_performance_tracker
from app.strategy.base import Signal, SignalAction


def demo_lead_lag_tracker():
    """演示 Lead-Lag 追踪器"""
    print("\n" + "="*60)
    print("1. Lead-Lag 追踪器演示")
    print("="*60)

    tracker = get_lead_lag_tracker(window_size=300, max_lag_seconds=120)

    # 模拟数据：美股 TSLA 在 t=0 时涨 5%
    base_time = time.time()
    tracker.record_price_move("US.TSLA", base_time, 5.0, volume=1000000)

    # 港股 00700 在 t+180 秒时开始跟涨 3%
    tracker.record_price_move("HK.00700", base_time + 180, 3.0, volume=500000)

    # 继续模拟更多数据点
    for i in range(1, 50):
        tracker.record_price_move("US.TSLA", base_time + i*60, 0.5, volume=100000)
        tracker.record_price_move("HK.00700", base_time + i*60 + 180, 0.3, volume=50000)

    # 检测滞后关系
    metrics = tracker.detect_lag("US.TSLA", "HK.00700", force_refresh=True)

    print(f"Lead: US.TSLA → Lag: HK.00700")
    print(f"  滞后时间：{metrics.lag_seconds:.1f} 秒")
    print(f"  相关性：{metrics.correlation:.3f}")
    print(f"  响应强度：{metrics.strength:.3f}")
    print(f"  置信度：{metrics.confidence:.2f}")

    return metrics


def demo_news_mapper():
    """演示新闻 - 股票映射器"""
    print("\n" + "="*60)
    print("2. 新闻 - 股票映射器演示")
    print("="*60)

    mapper = get_news_mapper()  # 演示模式

    # 测试新闻 1: 明确提及公司
    result1 = mapper.map_news_to_stocks(
        news_id="news_001",
        title="腾讯控股发布 2025 年 Q4 财报，净利润同比增长 30%",
        content="腾讯控股有限公司今日发布财报..."
    )

    print(f"新闻 1: 腾讯财报")
    print(f"  直接匹配：{result1.direct_matches}")
    print(f"  置信度：{result1.confidence:.2f}")

    # 测试新闻 2: 行业新闻
    result2 = mapper.map_news_to_stocks(
        news_id="news_002",
        title="电动车补贴退坡，行业面临洗牌",
        content="工信部发布新政策，电动车购置补贴将退坡..."
    )

    print(f"\n新闻 2: 电动车补贴")
    print(f"  行业匹配：{result2.industry_matches[:5]}")  # 显示前 5 个
    print(f"  置信度：{result2.confidence:.2f}")

    return result1, result2


def demo_dynamic_position_sizer():
    """演示动态仓位计算器"""
    print("\n" + "="*60)
    print("3. 动态仓位计算器演示")
    print("="*60)

    sizer = get_dynamic_positionSizer()

    # 场景 1: 高质量信号
    params1 = sizer.calculate(
        arb_score=0.85,
        news_sentiment=0.7,
        volatility=0.2
    )
    print(f"场景 1: 高质量信号 (arb=0.85, sentiment=0.7)")
    print(f"  仓位：{params1.position_pct:.1%}")
    print(f"  止损：{params1.stop_loss_pct:.1%}")
    print(f"  止盈：{params1.take_profit_pct:.1%}")
    print(f"  质量等级：{params1.signal_quality.value}")
    print(f"  置信度：{params1.confidence:.2f}")

    # 场景 2: 中等质量信号
    params2 = sizer.calculate(
        arb_score=0.65,
        news_sentiment=0.2,
        volatility=0.35
    )
    print(f"\n场景 2: 中等质量信号 (arb=0.65, sentiment=0.2, vol=0.35)")
    print(f"  仓位：{params2.position_pct:.1%}")
    print(f"  止损：{params2.stop_loss_pct:.1%}")
    print(f"  止盈：{params2.take_profit_pct:.1%}")
    print(f"  质量等级：{params2.signal_quality.value}")
    print(f"  置信度：{params2.confidence:.2f}")

    # 场景 3: 低质量信号
    params3 = sizer.calculate(
        arb_score=0.35,
        news_sentiment=-0.3,
        volatility=0.4
    )
    print(f"\n场景 3: 低质量信号 (arb=0.35, sentiment=-0.3, vol=0.4)")
    print(f"  仓位：{params3.position_pct:.1%}")
    print(f"  止损：{params3.stop_loss_pct:.1%}")
    print(f"  止盈：{params3.take_profit_pct:.1%}")
    print(f"  质量等级：{params3.signal_quality.value}")
    print(f"  置信度：{params3.confidence:.2f}")

    return params1, params2, params3


def demo_duration_predictor():
    """演示影响持续时间预测器"""
    print("\n" + "="*60)
    print("4. 影响持续时间预测器演示")
    print("="*60)

    predictor = get_duration_predictor()

    # 场景 1: 财报事件 (负面)
    pred1 = predictor.predict(
        event_type="earnings",
        sentiment_score=-0.7,
        news_count=15,
        market_cap="large",
        volatility=0.25
    )
    print(f"场景 1: 负面财报 ( sentiment=-0.7, news=15)")
    print(f"  预期持续时间：{pred1.expected_duration_minutes} 分钟")
    print(f"  退出建议：{pred1.exit_signal}")
    print(f"  置信度：{pred1.confidence:.2f}")
    print(f"  原因：{pred1.exit_reason}")

    # 场景 2: 并购事件 (正面)
    pred2 = predictor.predict(
        event_type="ma",
        sentiment_score=0.8,
        news_count=25,
        market_cap="mid",
        volatility=0.3
    )
    print(f"\n场景 2: 并购事件 (sentiment=0.8, news=25)")
    print(f"  预期持续时间：{pred2.expected_duration_minutes} 分钟")
    print(f"  退出建议：{pred2.exit_signal}")
    print(f"  置信度：{pred2.confidence:.2f}")

    # 场景 3: 分析师评级 (轻微负面)
    pred3 = predictor.predict(
        event_type="analyst",
        sentiment_score=-0.3,
        news_count=3,
        market_cap="large",
        volatility=0.2
    )
    print(f"\n场景 3: 分析师评级 (sentiment=-0.3, news=3)")
    print(f"  预期持续时间：{pred3.expected_duration_minutes} 分钟")
    print(f"  退出建议：{pred3.exit_signal}")

    return pred1, pred2, pred3


def demo_event_performance_tracker():
    """演示事件绩效追踪器"""
    print("\n" + "="*60)
    print("5. 事件绩效追踪器演示")
    print("="*60)

    tracker = get_event_performance_tracker()

    # 记录事件生成
    tracker.record_event(
        event_id="evt_demo_001",
        event_type="earnings",
        predicted_direction="sell",
        target_stocks=[{"code": "HK.00700", "direction": "short", "weight": 1.0}],
        event_title="腾讯财报不及预期",
        predicted_duration_minutes=90,
        predicted_return=0.03,
        confidence=0.75,
        arb_score=0.8,
        news_sentiment=-0.6
    )
    print(f"记录事件：evt_demo_001 (腾讯财报)")

    # 模拟交易退出
    tracker.record_outcome(
        event_id="evt_demo_001",
        trade_id="trade_demo_001",
        code="HK.00700",
        direction="short",
        entry_price=520.0,
        exit_price=505.0,
        quantity=100,
        actual_return=0.029,  # +2.9%
        max_drawdown=-0.008,
        max_profit=0.035,
        holding_minutes=75,
        exit_reason="take_profit",
        predicted_duration=90,
        predicted_direction="sell"
    )
    print(f"记录结果：收益 +2.9%, 持仓 75 分钟，止盈退出")

    # 获取绩效统计
    stats = tracker.get_performance_stats("earnings", days=30)
    print(f"\n财报事件绩效统计 (30 天):")
    print(f"  总事件数：{stats.total_events}")
    print(f"  胜率：{stats.win_rate:.1%}")
    print(f"  平均收益：{stats.avg_return:.2%}")
    print(f"  方向预测准确率：{stats.direction_accuracy:.1%}")

    # 获取优化建议
    suggestions = tracker.get_optimization_suggestions("earnings")
    if suggestions:
        print(f"\n优化建议:")
        for sug in suggestions:
            print(f"  [{sug['type']}] {sug['message']}")
            if 'suggestion' in sug:
                print(f"    → {sug['suggestion']}")

    return stats


def demo_integrated_workflow():
    """演示完整集成工作流"""
    print("\n" + "="*60)
    print("6. 完整集成工作流演示")
    print("="*60)
    print("场景：美股 TSLA 夜间大涨 5%，预测港股 00700 开盘套利")

    # 1. Lead-Lag 分析
    tracker = get_lead_lag_tracker()
    metrics = tracker.detect_lag("US.TSLA", "HK.00700")

    print(f"\n步骤 1: Lead-Lag 分析")
    print(f"  US.TSLA → HK.00700: lag={metrics.lag_seconds:.0f}s, corr={metrics.correlation:.2f}")

    # 2. 新闻分析
    mapper = get_news_mapper()
    news_result = mapper.map_news_to_stocks(
        news_id="news_tsla_surge",
        title="特斯拉夜间大涨 5%，财报超预期",
        content="特斯拉发布 Q4 财报..."
    )

    print(f"\n步骤 2: 新闻映射")
    print(f"  直接匹配：{news_result.direct_matches}")

    # 3. 动态仓位计算
    arb_score = 0.75  # 假设的套利评分
    sizer = get_dynamic_positionSizer()
    params = sizer.calculate(
        arb_score=arb_score,
        news_sentiment=0.6,
        volatility=0.25
    )

    print(f"\n步骤 3: 动态仓位")
    print(f"  建议仓位：{params.position_pct:.1%}")
    print(f"  止损：{params.stop_loss_pct:.1%}")
    print(f"  止盈：{params.take_profit_pct:.1%}")

    # 4. 持续时间预测
    predictor = get_duration_predictor()
    duration = predictor.predict(
        event_type="earnings",
        sentiment_score=0.6,
        news_count=10
    )

    print(f"\n步骤 4: 持续时间预测")
    print(f"  预期：{duration.expected_duration_minutes} 分钟")
    print(f"  退出：{duration.exit_signal}")

    # 5. 记录事件 (用于后续绩效分析)
    perf_tracker = get_event_performance_tracker()
    perf_tracker.record_event(
        event_id="evt_tsla_earnings",
        event_type="earnings",
        predicted_direction="buy",
        target_stocks=[{"code": "HK.00700", "direction": "long"}],
        predicted_duration_minutes=duration.expected_duration_minutes,
        arb_score=arb_score,
        news_sentiment=0.6
    )

    print(f"\n步骤 5: 事件已记录 (evt_tsla_earnings)")
    print(f"  等待交易执行后记录结果...")

    print(f"\n" + "="*60)
    print("集成工作流完成!")
    print("="*60)


def main():
    """主函数"""
    print("\n" + "#"*60)
    print("# FTSeek 跨市场套利与事件驱动策略集成演示")
    print("#"*60)

    # 单独模块演示
    demo_lead_lag_tracker()
    demo_news_mapper()
    demo_dynamic_position_sizer()
    demo_duration_predictor()
    demo_event_performance_tracker()

    # 完整工作流演示
    demo_integrated_workflow()

    print("\n" + "#"*60)
    print("# 演示完成!")
    print("#"*60 + "\n")


if __name__ == "__main__":
    main()
