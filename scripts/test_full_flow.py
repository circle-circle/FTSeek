#!/usr/bin/env python3
"""
端到端测试脚本

测试完整交易流程：
1. 模拟行情推送
2. 因子计算
3. 策略生成信号
4. 风控检查
5. 模拟执行
6. 记录验证
7. 报告生成
"""
import os
import sys
import logging
from datetime import datetime
from pathlib import Path

# 设置环境变量
os.environ['API_TOKEN'] = 'test_token'
os.environ['BAILIAN_API_KEY'] = 'sk-8f8a02822c1b46b4bd43b422b9196ec6'
os.environ['FTSEEK_ENABLE_AI'] = 'true'

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s: %(message)s'
)
logger = logging.getLogger("ftseek.test.e2e")


def test_step_1_data_subscription():
    """Step 1: 测试数据订阅"""
    logger.info("="*60)
    logger.info("Step 1: 测试数据订阅")
    logger.info("="*60)
    
    from app.data.realtime_subscriber import get_realtime_subscriber
    
    subscriber = get_realtime_subscriber()
    status = subscriber.get_subscription_status()
    
    assert status['connected'], "行情订阅未连接"
    logger.info(f"✅ 行情订阅已连接")
    logger.info(f"   已订阅：{len(status['subscribed_codes'])} 只股票")
    
    return True


def test_step_2_factor_calculation():
    """Step 2: 测试因子计算"""
    logger.info("="*60)
    logger.info("Step 2: 测试因子计算")
    logger.info("="*60)
    
    from app.factors import (
        CapitalFlowFactors,
        SentimentFactors,
        AISentimentFactor,
        AIPredictionFactor
    )
    
    # 模拟 K 线数据
    klines = [
        {'close': 380+i*2, 'high': 385+i*2, 'low': 378+i*2, 
         'volume': 1000000, 'date': f'2026-03-{i%31:02d}'}
        for i in range(50)
    ]
    
    data = {'klines': klines}
    
    # 测试资金流因子
    cf_factor = CapitalFlowFactors()
    cf_result = cf_factor.calculate('HK.00700', data)
    assert cf_result is not None, "资金流因子计算失败"
    logger.info(f"✅ 资金流因子：{cf_result.value:.2f}")
    
    # 测试情绪因子
    sf_factor = SentimentFactors()
    sf_result = sf_factor.calculate('HK.00700', {
        'news': [{'title': '腾讯业绩超预期', 'content': ''}]
    })
    assert sf_result is not None, "情绪因子计算失败"
    logger.info(f"✅ 情绪因子：{sf_result.value:.2f}")
    
    # 测试 AI 情绪因子
    ai_sf_factor = AISentimentFactor()
    ai_sf_result = ai_sf_factor.calculate('HK.00700', {
        'news': [{'title': '腾讯发布利好消息', 'content': ''}]
    })
    logger.info(f"✅ AI 情绪因子：{ai_sf_result.value if ai_sf_result else 'N/A'}")
    
    # 测试 AI 预测因子
    ai_pf_factor = AIPredictionFactor()
    ai_pf_result = ai_pf_factor.calculate('HK.00700', data)
    logger.info(f"✅ AI 预测因子：{ai_pf_result.value if ai_pf_result else 'N/A'}")
    
    return True


def test_step_3_strategy_fusion():
    """Step 3: 测试策略融合"""
    logger.info("="*60)
    logger.info("Step 3: 测试策略融合")
    logger.info("="*60)
    
    from app.strategy.fusion_v3 import FusionV3Strategy
    
    strategy = FusionV3Strategy({
        'use_ai': True,
        'ai_weight': 0.3
    })
    
    # 模拟报价
    quote = {
        'last_done': 390.0,
        'time': '2026-03-14 21:50:00'
    }
    
    # 生成信号
    signal = strategy.on_quote('HK.00700', quote)
    
    logger.info(f"✅ 策略融合完成")
    logger.info(f"   信号：{signal.action if signal else '无信号'}")
    logger.info(f"   置信度：{signal.confidence if signal else 0:.2f}")
    
    return True


def test_step_4_risk_check():
    """Step 4: 测试风控检查"""
    logger.info("="*60)
    logger.info("Step 4: 测试风控检查")
    logger.info("="*60)
    
    from app.risk.dynamic_position_sizer import get_dynamic_position_sizer
    from app.risk.realtime_alert import get_realtime_risk_monitor
    
    # 测试动态仓位
    sizer = get_dynamic_position_sizer()
    position = sizer.calculate_position(
        signal_confidence=0.70,
        market_regime='uptrend',
        ai_score=0.85,
        volatility=0.02
    )
    logger.info(f"✅ 动态仓位：{position:.2%}")
    
    # 测试风险监控
    monitor = get_realtime_risk_monitor()
    status = monitor.get_current_status()
    logger.info(f"✅ 风险状态:")
    logger.info(f"   连续亏损：{status['consecutive_losses']}")
    logger.info(f"   回撤：{status['drawdown']:.2%}")
    
    return True


def test_step_5_trade_execution():
    """Step 5: 测试交易执行"""
    logger.info("="*60)
    logger.info("Step 5: 测试交易执行")
    logger.info("="*60)
    
    from app.execution.auto_executor import get_auto_trade_executor
    
    executor = get_auto_trade_executor(simulate=True)
    
    # 模拟执行交易
    result = executor.execute_signal(
        code='HK.00700',
        action='BUY',
        quantity=100,
        price=390.0,
        strategy='fusion_v3',
        signal_confidence=0.67,
        ai_score=0.85
    )
    
    assert result.success, "交易执行失败"
    logger.info(f"✅ 交易执行成功")
    logger.info(f"   订单 ID: {result.order_id}")
    logger.info(f"   成交价：HK${result.filled_price:.2f}")
    
    return True


def test_step_6_record_verification():
    """Step 6: 测试记录验证"""
    logger.info("="*60)
    logger.info("Step 6: 测试记录验证")
    logger.info("="*60)
    
    from app.storage.trade_recorder import get_trade_recorder
    
    recorder = get_trade_recorder()
    records = recorder.get_all_records()
    
    assert len(records) > 0, "交易记录为空"
    logger.info(f"✅ 交易记录：{len(records)} 条")
    
    # 验证最新记录
    latest = records[-1]
    logger.info(f"   最新交易：{latest.code} {latest.action} @ {latest.entry_price}")
    
    # 测试今日汇总
    summary = recorder.get_today_summary()
    logger.info(f"✅ 今日汇总:")
    logger.info(f"   交易数：{summary['closed_trades']}")
    logger.info(f"   胜率：{summary['win_rate']:.1%}")
    
    return True


def test_step_7_report_generation():
    """Step 7: 测试报告生成"""
    logger.info("="*60)
    logger.info("Step 7: 测试报告生成")
    logger.info("="*60)
    
    from app.reports.weekly_review import get_weekly_review_generator
    
    reviewer = get_weekly_review_generator()
    
    # 生成测试报告
    report = reviewer.generate_report(
        start_date='2026-03-10',
        end_date='2026-03-14',
        trade_records=[
            {'strategy': 'fusion_v3', 'pnl': 1500, 'exit_price': 405},
            {'strategy': 'fusion_v3', 'pnl': -500, 'exit_price': 385}
        ],
        prediction_stats={'accuracy': 0.67},
        attribution_report={'ai_contribution': {'ai_contribution_percent': 15.0}}
    )
    
    logger.info(f"✅ 报告生成成功")
    logger.info(f"   总交易：{report['summary']['total_trades']}")
    logger.info(f"   建议：{len(report['recommendations'])} 条")
    
    return True


def main():
    """主测试流程"""
    logger.info("╔" + "="*58 + "╗")
    logger.info("║" + " "*10 + "FTSeek 端到端测试" + " "*29 + "║")
    logger.info("╚" + "="*58 + "╝")
    logger.info("")
    
    tests = [
        ("Step 1: 数据订阅", test_step_1_data_subscription),
        ("Step 2: 因子计算", test_step_2_factor_calculation),
        ("Step 3: 策略融合", test_step_3_strategy_fusion),
        ("Step 4: 风控检查", test_step_4_risk_check),
        ("Step 5: 交易执行", test_step_5_trade_execution),
        ("Step 6: 记录验证", test_step_6_record_verification),
        ("Step 7: 报告生成", test_step_7_report_generation),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            logger.info("")
            if test_func():
                passed += 1
                logger.info(f"✅ {name}: 通过")
            else:
                failed += 1
                logger.error(f"❌ {name}: 失败")
        except Exception as e:
            failed += 1
            logger.error(f"❌ {name}: 异常 - {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        logger.info("")
    
    # 总结
    logger.info("="*60)
    logger.info(f"测试结果：{passed}/{len(tests)} 通过")
    logger.info("="*60)
    
    if failed == 0:
        logger.info("")
        logger.info("🎉 所有测试通过！系统已就绪，可以开市！")
        logger.info("")
        return 0
    else:
        logger.info("")
        logger.info(f"⚠️ {failed} 个测试失败，请检查系统配置！")
        logger.info("")
        return 1


if __name__ == '__main__':
    sys.exit(main())
