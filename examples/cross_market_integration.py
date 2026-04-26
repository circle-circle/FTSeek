#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨市场套利模块集成演示脚本

展示:
1. Lead-Lag 追踪器记录价格变动
2. 动态仓位计算器调整参数
3. 新闻映射器分析新闻
4. 持续时间预测器预测影响时间
5. 事件绩效追踪器记录结果

使用示例:
    python examples/cross_market_integration.py
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategy.cross_market import get_cross_market_strategy, OvernightChange
from app.factors.news_mapper import get_news_mapper
from app.factors.duration_predictor import get_duration_predictor


def main():
    print("=" * 70)
    print("跨市场套利模块集成演示")
    print("=" * 70)

    # ============================================================
    # 1. 初始化策略 (已集成 5 个模块)
    # ============================================================
    print("\n1. 初始化跨市场策略...")
    strategy = get_cross_market_strategy()

    print(f"   Lead-Lag 追踪器：✓")
    print(f"   动态仓位计算器：✓")
    print(f"   新闻映射器：✓")
    print(f"   持续时间预测器：✓")
    print(f"   事件绩效追踪器：✓")

    # ============================================================
    # 2. 模拟美股隔夜数据
    # ============================================================
    print("\n2. 模拟美股隔夜数据...")
    us_data = [
        OvernightChange(
            code='BABA',
            name='Alibaba',
            close_price=85.5,
            change_pct=0.045,  # +4.5%
            volume=15000000,
            turnover=1280000000,
            market='US',
            timestamp=datetime.now()
        ),
        OvernightChange(
            code='TSLA',
            name='Tesla',
            close_price=250.0,
            change_pct=0.032,  # +3.2%
            volume=25000000,
            turnover=6250000000,
            market='US',
            timestamp=datetime.now()
        ),
    ]

    for item in us_data:
        print(f"   {item.code}: {item.change_pct:+.1%}")

    # ============================================================
    # 3. 记录价格变动到 Lead-Lag 追踪器
    # ============================================================
    print("\n3. 记录价格变动到 Lead-Lag 追踪器...")
    current_time = datetime.now().timestamp()
    for us_change in us_data:
        strategy.lead_lag.record_price_move(
            code=us_change.code,
            timestamp=current_time,
            price_change_pct=us_change.change_pct * 100,
            volume=us_change.volume
        )
        print(f"   记录 {us_change.code}: {us_change.change_pct * 100:+.1f}%")

    # 模拟港股跟随数据
    print("\n   模拟港股跟随数据...")
    hk_pairs = {'BABA': 'HK.09988', 'TSLA': 'HK.01211'}
    for us_code, hk_code in hk_pairs.items():
        # 滞后 180 秒，跟随 70% 的变动
        strategy.lead_lag.record_price_move(
            code=hk_code,
            timestamp=current_time + 180,
            price_change_pct=us_data[0].change_pct * 100 * 0.7,
            volume=5000000
        )
        print(f"   记录 {hk_code}: {us_data[0].change_pct * 100 * 0.7:+.1f}% (滞后 180 秒)")

    # ============================================================
    # 4. 检测 Lead-Lag 关系
    # ============================================================
    print("\n4. 检测 Lead-Lag 关系...")
    metrics = strategy.lead_lag.detect_lag('US.BABA', 'HK.09988', force_refresh=True)
    print(f"   US.BABA → HK.09988:")
    print(f"     滞后时间：{metrics.lag_seconds:.0f} 秒")
    print(f"     相关性：{metrics.correlation:.3f}")
    print(f"     响应强度：{metrics.strength:.3f}")
    print(f"     置信度：{metrics.confidence:.2f}")

    # ============================================================
    # 5. 生成交易信号
    # ============================================================
    print("\n5. 生成交易信号...")
    signals = strategy.analyze_us_to_hk(us_data)

    if signals:
        for signal in signals:
            print(f"   信号：{signal.code} {signal.action.value}")
            print(f"     理由：{signal.reason[:60]}...")
            if signal.extra:
                extra = signal.extra
                print(f"     套利评分：{extra.get('arb_total_score', 'N/A'):.3f}")
                print(f"     决策：{extra.get('arb_decision', 'N/A')}")
                print(f"     仓位：{extra.get('risk_params', {}).get('max_position_pct', 'N/A'):.1%}")
                print(f"     止损：{extra.get('risk_params', {}).get('stop_loss_pct', 'N/A'):.1%}")
                print(f"     止盈：{extra.get('risk_params', {}).get('take_profit_pct', 'N/A'):.1%}")
    else:
        print("   无信号生成 (可能是评分过低或冷却期)")

    # ============================================================
    # 6. 新闻映射演示
    # ============================================================
    print("\n6. 新闻映射演示...")
    news_mapper = get_news_mapper(use_ai=False)  # 规则模式

    news_result = news_mapper.map_news_to_stocks(
        news_id='demo_news_001',
        title='阿里巴巴财报超预期，净利润同比增长 30%',
        content='阿里巴巴集团发布季度财报，营收和净利润均超出市场预期...'
    )

    print(f"   新闻标题：阿里巴巴财报超预期...")
    print(f"   直接匹配：{len(news_result.direct_matches)} 只股票")
    for match in news_result.direct_matches:
        print(f"     - {match.get('code', 'N/A')}: {match.get('name', 'N/A')}")
    print(f"   行业匹配：{len(news_result.industry_matches)} 只股票")
    print(f"   置信度：{news_result.confidence:.2f}")

    # ============================================================
    # 7. 持续时间预测演示
    # ============================================================
    print("\n7. 持续时间预测演示...")
    duration_predictor = get_duration_predictor()

    duration_result = duration_predictor.predict(
        event_type='earnings',
        sentiment_score=0.7,
        news_count=5,
        market_cap='large',
        volatility=0.25
    )

    print(f"   事件类型：earnings (财报)")
    print(f"   情绪评分：+0.7 (正面)")
    print(f"   预测持续时间：{duration_result.expected_duration_minutes} 分钟")
    print(f"   退出建议：{duration_result.exit_signal}")
    print(f"   置信度：{duration_result.confidence:.2f}")

    # ============================================================
    # 8. 动态仓位计算演示
    # ============================================================
    print("\n8. 动态仓位计算演示...")
    sizer_params = strategy.sizer.calculate(
        arb_score=0.75,  # 高质量信号
        news_sentiment=0.5,
        volatility=0.25
    )

    print(f"   输入：arb_score=0.75, sentiment=0.5, volatility=0.25")
    print(f"   仓位：{sizer_params.position_pct:.1%}")
    print(f"   止损：{sizer_params.stop_loss_pct:.1%}")
    print(f"   止盈：{sizer_params.take_profit_pct:.1%}")
    print(f"   信号质量：{sizer_params.signal_quality}")

    # ============================================================
    # 9. 事件绩效追踪状态
    # ============================================================
    print("\n9. 事件绩效追踪状态...")
    stats = strategy.perf_tracker.get_performance_stats('cross_market_arbitrage', days=30)
    print(f"   总事件数：{stats.total_events}")
    print(f"   已完成：{stats.completed_events}")
    print(f"   胜率：{stats.win_rate:.1%}")
    print(f"   平均收益：{stats.avg_return:.2%}")

    # ============================================================
    # 总结
    # ============================================================
    print("\n" + "=" * 70)
    print("集成演示完成")
    print("=" * 70)
    print("""
核心工作流:
1. 美股隔夜价格变动 → Lead-Lag 追踪器记录
2. 检测跨市场滞后关系 → 计算套利评分
3. 动态仓位计算器 → 调整仓位/止损/止盈
4. 新闻映射器 → 识别新闻影响股票
5. 持续时间预测器 → 预测退出时机
6. 事件绩效追踪器 → 记录结果并优化策略

下一步:
- 在 data_subscriber.py 中接入实时行情
- 在 news_push/collector.py 中接入实时新闻
- 使用真实数据进行回测验证
""")


if __name__ == "__main__":
    main()
