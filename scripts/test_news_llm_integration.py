#!/usr/bin/env python3
"""
新闻LLM深度分析集成 - 端到端测试

测试流程:
1. 新闻汇总与主题聚类
2. 富途板块数据获取
3. 市场上下文构建
4. 策略指导生成
5. 钉钉推送测试

用法:
    cd /opt/ftseek/FTSeek
    python3 scripts/test_news_llm_integration.py
"""
import sys
import logging
import time
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/opt/ftseek/logs/news_llm_test.log', encoding='utf-8')
    ]
)

logger = logging.getLogger("test.news_llm_integration")


def test_news_summarizer():
    """测试1: 新闻汇总与主题聚类"""
    print("\n" + "="*80)
    print("测试1: 新闻汇总与主题聚类")
    print("="*80)
    
    try:
        from app.news_push.news_summarizer import get_summarizer
        
        summarizer = get_summarizer()
        summary = summarizer.summarize(hours=12, max_news=50)
        
        print(f"✅ 新闻汇总成功")
        print(f"   新闻总数: {summary['total_news']}")
        print(f"   整体情绪: {summary['overall_sentiment']:+.2f} ({summary['sentiment_trend']})")
        print(f"   主题数: {len(summary['topics'])}")
        print(f"   焦点股: {len(summary['stock_focus'])}")
        print(f"   突发新闻: {len(summary['breaking_news'])}")
        print(f"   生成耗时: {summary['generation_time_ms']}ms")
        
        if summary['topics']:
            print(f"\n   主题分布:")
            for topic_id, info in list(summary['topics'].items())[:3]:
                print(f"     - {info['name']}: {info['count']}条, 情绪{info['avg_score']:+.2f}")
        
        return summary
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_sector_data():
    """测试2: 富途板块数据获取"""
    print("\n" + "="*80)
    print("测试2: 富途板块数据获取")
    print("="*80)
    
    try:
        from app.data.futu_sector_data import get_sector_data
        
        sector_data = get_sector_data()
        
        # 测试板块排名
        ranking = sector_data.get_sector_ranking(limit=5)
        print(f"✅ 板块排名获取成功: {len(ranking)}个板块")
        
        if ranking:
            print(f"\n   Top 3 板块:")
            for i, sector in enumerate(ranking[:3], 1):
                print(f"     {i}. {sector['sector_name']}: {sector['change_pct']:+.2f}% "
                      f"(成交额{sector['turnover']}亿)")
        
        # 测试概念板块
        concepts = sector_data.get_concept_hot()
        print(f"\n✅ 概念板块获取成功: {len(concepts)}个概念")
        
        if concepts:
            print(f"\n   Top 3 概念:")
            for i, concept in enumerate(concepts[:3], 1):
                print(f"     {i}. {concept['concept_name']}: {concept['change_pct']:+.2f}% "
                      f"(成交额{concept['turnover']}亿)")
        
        sector_data.close()
        return {'ranking': ranking, 'concepts': concepts}
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_context_builder():
    """测试3: 市场上下文构建器"""
    print("\n" + "="*80)
    print("测试3: 市场上下文构建器")
    print("="*80)
    
    try:
        from app.ai.market_context_builder import get_context_builder
        
        builder = get_context_builder()
        target_codes = ['HK.00700', 'HK.09988', 'HK.03690']
        
        start = time.time()
        context = builder.build_pre_market_context(target_codes)
        elapsed = time.time() - start
        
        print(f"✅ 上下文构建成功")
        print(f"   生成耗时: {context['generation_time_ms']}ms (实际{elapsed:.1f}s)")
        print(f"   新闻总数: {context['news_summary']['total_news']}")
        print(f"   板块数: {len(context['sector_data']['capital_flow'])}")
        print(f"   市场宽度: {context['market_breadth']}")
        print(f"   跨市场数据: {list(context['cross_market'].keys())}")
        print(f"   个股情绪: {len(context['stock_sentiment'])}只")
        
        # 测试LLM格式化
        formatted = builder.format_for_llm(context)
        print(f"\n✅ LLM格式化成功: {len(formatted)}字符")
        print(f"\n   前500字符:")
        print("   " + "-"*76)
        for line in formatted[:500].split('\n'):
            print(f"   {line}")
        print("   " + "-"*76)
        
        return context
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_strategy_guidance():
    """测试4: 策略指导引擎"""
    print("\n" + "="*80)
    print("测试4: 策略指导引擎")
    print("="*80)
    
    try:
        from app.ai.strategy_guidance import get_guidance_engine
        
        engine = get_guidance_engine()
        
        # 模拟LLM评估
        class MockAssessment:
            def __init__(self):
                self.direction = 'BUY'
                self.confidence_level = 'HIGH'
                self.key_factors = ['科技板块强势', '资金流入明显', '新闻情绪正面']
                self.risk_flags = ['美股波动风险']
                self.reasoning = "基于隔夜新闻和板块资金流向，科技板块表现强势，建议重点关注。"
            
            @property
            def confidence_value(self):
                return 0.75
        
        # 简化的上下文
        mock_context = {
            'news_summary': {
                'total_news': 45,
                'overall_sentiment': 0.65,
                'sentiment_trend': 'rising',
                'topics': {},
                'breaking_news': [],
            },
            'sector_data': {
                'capital_flow': [
                    {'sector_name': '科技', 'change_pct': 2.5, 'turnover': 120.5, 
                     'advance_count': 40, 'decline_count': 10, 'top_stocks': []},
                ],
                'ranking': [],
            },
            'market_breadth': {'ratio': 0.62},
            'cross_market': {'vix': 18.5},
            'stock_sentiment': {
                'HK.00700': {'decayed_score': 0.72, 'news_count': 8, 'trend': 'rising'},
            },
            'technical_snapshot': {
                'HK.00700': {'price': 300, 'change_pct': 1.5, 'rsi_14': 65},
            },
        }
        
        assessment = MockAssessment()
        guidance = engine.generate_from_llm(assessment, mock_context)
        
        print(f"✅ 策略指导生成成功")
        print(f"   整体立场: {guidance.overall_stance}")
        print(f"   置信度: {guidance.confidence:.0%}")
        print(f"   板块配置: {len(guidance.sector_allocation)}个")
        print(f"   个股推荐: {len(guidance.stock_picks)}只")
        print(f"   风险提示: {len(guidance.risk_warnings)}条")
        print(f"   催化剂: {len(guidance.key_catalysts)}个")
        
        if guidance.sector_allocation:
            print(f"\n   板块配置:")
            for sector in guidance.sector_allocation:
                print(f"     - {sector['sector']}: {sector['weight']:.0%} ({sector['reason']})")
        
        if guidance.stock_picks:
            print(f"\n   个股推荐:")
            for stock in guidance.stock_picks:
                print(f"     - {stock['code']}: {stock['action_name']} "
                      f"(目标{stock['target_price']}, 止损{stock['stop_loss']})")
        
        # 测试钉钉格式化
        message = engine.format_for_dingtalk(guidance)
        print(f"\n✅ 钉钉格式化成功: {len(message)}字符")
        
        return guidance
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_dingtalk_push(summary, guidance):
    """测试5: 钉钉推送"""
    print("\n" + "="*80)
    print("测试5: 钉钉推送")
    print("="*80)
    
    try:
        from app.news_push.news_summarizer import get_summarizer
        from app.ai.strategy_guidance import get_guidance_engine
        from app.notifier.dingtalk import DingTalkNotifier
        
        notifier = DingTalkNotifier()
        summarizer = get_summarizer()
        engine = get_guidance_engine()
        
        # 构建消息
        lines = [
            "### 📰 新闻LLM深度分析集成测试",
            "",
            f"**测试时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        
        # 新闻汇总
        if summary:
            lines.append("#### 新闻汇总")
            lines.append(summarizer.format_for_dingtalk(summary))
            lines.append("")
        
        # 策略指导
        if guidance:
            lines.append("#### 策略指导")
            lines.append(engine.format_for_dingtalk(guidance))
        
        message = '\n'.join(lines)
        
        # 推送
        success = notifier.send_markdown(message, title="新闻LLM深度分析测试")
        
        if success:
            print(f"✅ 钉钉推送成功")
        else:
            print(f"⚠️  钉钉推送失败（可能未配置webhook）")
        
        return success
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """主测试流程"""
    print("\n" + "="*80)
    print("新闻LLM深度分析集成 - 端到端测试")
    print("="*80)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    start_time = time.time()
    
    # 测试1: 新闻汇总
    summary = test_news_summarizer()
    time.sleep(0.5)
    
    # 测试2: 板块数据
    sector_data = test_sector_data()
    time.sleep(0.5)
    
    # 测试3: 上下文构建
    context = test_context_builder()
    time.sleep(0.5)
    
    # 测试4: 策略指导
    guidance = test_strategy_guidance()
    time.sleep(0.5)
    
    # 测试5: 钉钉推送
    push_success = test_dingtalk_push(summary, guidance)
    
    # 总结
    elapsed = time.time() - start_time
    print("\n" + "="*80)
    print("测试总结")
    print("="*80)
    print(f"总耗时: {elapsed:.1f}秒")
    print(f"新闻汇总: {'✅' if summary else '❌'}")
    print(f"板块数据: {'✅' if sector_data else '❌'}")
    print(f"上下文构建: {'✅' if context else '❌'}")
    print(f"策略指导: {'✅' if guidance else '❌'}")
    print(f"钉钉推送: {'✅' if push_success else '⚠️'}")
    
    all_pass = all([summary, sector_data, context, guidance])
    print(f"\n{'✅ 所有核心测试通过！' if all_pass else '⚠️  部分测试失败，请检查日志'}")
    print("="*80)


if __name__ == '__main__':
    main()
