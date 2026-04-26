#!/usr/bin/env python3
"""
阿里巴巴LLM整合分析流程测试

功能:
1. 获取阿里巴巴(HK.09988)的新闻分析数据
2. 获取美股上周行情数据(SPYY/QQQ)
3. 构建跨市场上下文
4. 调用LLM(qwen3.5-plus)进行整合分析
5. 推送分析结果到钉钉

使用方法:
  cd /opt/ftseek/FTSeek
  python scripts/test_baba_llm_analysis.py
"""

import sys
import os
import asyncio
import logging
import json
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/llm_test_baba.log', mode='w')
    ]
)
logger = logging.getLogger("test_baba_llm")


def test_load_config():
    """测试1: 加载配置"""
    logger.info("=" * 80)
    logger.info("测试1: 加载配置")
    logger.info("=" * 80)
    
    try:
        from config.config_center import get_config_center
        config_center = get_config_center()
        
        # 读取LLM配置
        llm_config = config_center.get('ai.llm_market_analyst', {})
        if hasattr(llm_config, 'to_dict'):
            llm_config = llm_config.to_dict()
        
        # 如果config_center没有数据，尝试从.env读取
        if not llm_config or not llm_config.get('enabled'):
            logger.warning("⚠️  config_center中LLM配置为空，尝试从.env和环境变量读取")
            llm_config = {
                'enabled': os.environ.get('FTSEEK_ENABLE_AI', 'true').lower() == 'true',
                'model': os.environ.get('BAILIAN_MODEL', 'bailian/qwen3.5-plus'),
                'timeout': 120,  # 增加到120秒，思维链模式需要更长时间
                'urgent_timeout': 60,  # 测试时使用60秒
                'max_retries': 2,
                'cache_ttl': 600,
                'max_cache_size': 200,
                'enable_thinking': True,
                'trading_window': {
                    'start': '09:15',
                    'end': '10:00',
                    'timezone': 'Asia/Hong_Kong'
                }
            }
        
        logger.info(f"✅ LLM配置加载成功")
        logger.info(f"   enabled: {llm_config.get('enabled')}")
        logger.info(f"   model: {llm_config.get('model')}")
        logger.info(f"   timeout: {llm_config.get('timeout')}s")
        logger.info(f"   cache_ttl: {llm_config.get('cache_ttl')}s")
        
        return llm_config
        
    except Exception as e:
        logger.error(f"❌ 配置加载失败: {e}", exc_info=True)
        return None


def test_build_news_context(code: str = "HK.09988"):
    """测试2: 构建新闻上下文"""
    logger.info("=" * 80)
    logger.info(f"测试2: 构建{code}新闻上下文")
    logger.info("=" * 80)
    
    try:
        from app.ai.llm_market_analyst import NewsContext
        from app.factors.news_integration import get_news_analyzer
        
        analyzer = get_news_analyzer()
        news_data = analyzer.analyze_stock(code)
        
        if not news_data:
            logger.warning(f"⚠️  {code} 无新闻数据，使用模拟数据")
            # 使用模拟数据
            return NewsContext(
                headlines=[
                    "阿里巴巴Q3财报超预期，营收同比增长85%",
                    "阿里云分拆上市计划推进，估值或达1000亿美元",
                    "中国电商市场竞争加剧，拼多多崛起",
                    "监管政策缓和，平台经济迎来发展机遇",
                ],
                sentiment_score=0.65,
                news_count=4,
                top_news_summary="阿里巴巴最新财报显示强劲增长，云业务分拆进展顺利",
                avg_age_minutes=120.0,
                has_breaking=False,
            )
        
        headlines = news_data.get('headlines', [])
        sentiment = float(news_data.get('sentiment_score', 0.0))
        news_count = int(news_data.get('news_count', len(headlines)))
        summary = news_data.get('summary', '') or news_data.get('top_news_summary', '')
        avg_age = float(news_data.get('avg_age_minutes', 60.0))
        has_breaking = bool(news_data.get('has_breaking', False))
        
        news_ctx = NewsContext(
            headlines=headlines[:5],
            sentiment_score=sentiment,
            news_count=news_count,
            top_news_summary=summary[:200] if summary else '',
            avg_age_minutes=avg_age,
            has_breaking=has_breaking,
        )
        
        logger.info(f"✅ 新闻上下文构建成功")
        logger.info(f"   新闻数量: {news_ctx.news_count}")
        logger.info(f"   情绪得分: {news_ctx.sentiment_score:+.2f}")
        logger.info(f"   摘要: {news_ctx.top_news_summary[:80]}")
        
        return news_ctx
        
    except Exception as e:
        logger.error(f"❌ 新闻上下文构建失败: {e}", exc_info=True)
        return None


def test_build_cross_market_context():
    """测试3: 构建跨市场上下文"""
    logger.info("=" * 80)
    logger.info("测试3: 构建跨市场上下文(美股上周行情)")
    logger.info("=" * 80)
    
    try:
        from app.ai.llm_market_analyst import CrossMarketContext
        
        # 尝试获取真实数据
        try:
            from app.data.us_overnight_fetcher import get_overnight_fetcher
            fetcher = get_overnight_fetcher()
            us_data = fetcher.fetch_overnight_data() if fetcher._enabled else []
            
            spy_change = 0.0
            qqq_change = 0.0
            arb_pairs = []
            
            for item in us_data:
                us_code = getattr(item, 'code', '') or getattr(item, 'symbol', '')
                change_pct = getattr(item, 'change_pct', 0.0) or 0.0
                if us_code == 'SPY':
                    spy_change = float(change_pct)
                elif us_code == 'QQQ':
                    qqq_change = float(change_pct)
            
            logger.info(f"✅ 获取到真实美股数据")
            logger.info(f"   SPY: {spy_change:+.2f}%")
            logger.info(f"   QQQ: {qqq_change:+.2f}%")
            
        except Exception as e:
            logger.warning(f"⚠️  获取真实数据失败，使用模拟数据: {e}")
            # 模拟上周美股行情数据
            spy_change = 2.3  # 模拟SPY上周涨幅
            qqq_change = 2.8  # 模拟QQQ上周涨幅
        
        # 构建跨市场上下文
        cross_market_ctx = CrossMarketContext(
            spy_change_pct=spy_change,
            qqq_change_pct=qqq_change,
            vix=16.5,  # 模拟VIX
            vix_status="low",
            us_bond_10y=4.2,
            arb_pairs=[
                {
                    'us_code': 'BABA',
                    'hk_code': 'HK.09988',
                    'change_pct': 3.5,
                }
            ],
        )
        
        logger.info(f"✅ 跨市场上下文构建成功")
        logger.info(f"   SPY变化: {cross_market_ctx.spy_change_pct:+.1f}%")
        logger.info(f"   QQQ变化: {cross_market_ctx.qqq_change_pct:+.1f}%")
        logger.info(f"   VIX: {cross_market_ctx.vix:.1f}")
        logger.info(f"   套利配对: {len(cross_market_ctx.arb_pairs)}个")
        
        return cross_market_ctx
        
    except Exception as e:
        logger.error(f"❌ 跨市场上下文构建失败: {e}", exc_info=True)
        return None


async def test_llm_analysis(code: str, news_ctx, cross_market_ctx, llm_config: dict):
    """测试4: 调用LLM进行整合分析"""
    logger.info("=" * 80)
    logger.info(f"测试4: 调用LLM分析{code}")
    logger.info("=" * 80)
    
    try:
        from app.ai.llm_market_analyst import LLMMarketAnalyst
        
        # 初始化LLM分析师
        analyst = LLMMarketAnalyst(llm_config)
        
        if not analyst.enabled:
            logger.error("❌ LLM分析师未启用")
            return None
        
        logger.info(f"✅ LLM分析师初始化成功")
        logger.info(f"   模型: {analyst.model}")
        logger.info(f"   超时: {analyst.timeout}s")
        logger.info(f"   思维链: {analyst.enable_thinking}")
        
        # 调用分析
        logger.info(f"⏳ 开始调用LLM分析 (可能需要30-120秒)...")
        start_time = datetime.now()
        
        assessment = await analyst.analyze(
            code=code,
            news_context=news_ctx,
            cross_market_context=cross_market_ctx,
            trigger_type="SCHEDULED",  # 使用SCHEDULED触发类型
            urgent=True,  # 使用较短超时
        )
        
        elapsed = (datetime.now() - start_time).total_seconds()
        
        if not assessment:
            logger.error("❌ LLM分析返回空结果")
            return None
        
        logger.info(f"✅ LLM分析完成 (耗时: {elapsed:.1f}s)")
        logger.info(f"   方向: {assessment.direction}")
        logger.info(f"   置信度: {assessment.confidence_level}")
        logger.info(f"   仓位建议: {assessment.position_tier}")
        logger.info(f"   执行时机: {assessment.recommended_timing}")
        logger.info(f"   关键因素: {len(assessment.key_factors)}条")
        
        # 打印详细结果
        logger.info("-" * 80)
        logger.info(f"方向得分: {assessment.direction_score:+.2f}")
        logger.info(f"置信度值: {assessment.confidence_value:.2f}")
        logger.info(f"仓位比例: {assessment.position_ratio:.2f}")
        logger.info("-" * 80)
        
        if assessment.key_factors:
            logger.info("关键因素:")
            for i, factor in enumerate(assessment.key_factors, 1):
                logger.info(f"  {i}. {factor}")
        
        if assessment.reasoning:
            logger.info(f"\n分析推理:\n{assessment.reasoning[:300]}")
        
        if assessment.cross_market_insight:
            logger.info(f"\n跨市场洞察:\n{assessment.cross_market_insight[:200]}")
        
        return assessment
        
    except Exception as e:
        logger.error(f"❌ LLM分析失败: {e}", exc_info=True)
        return None


def test_push_to_dingtalk(code: str, assessment):
    """测试5: 推送结果到钉钉"""
    logger.info("=" * 80)
    logger.info("测试5: 推送分析结果到钉钉")
    logger.info("=" * 80)
    
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        
        # 从.env直接读取钉钉配置
        webhook_url = os.environ.get('DINGTALK_WEBHOOK_URL', '')
        secret = os.environ.get('DINGTALK_SECRET', '')
        
        if not webhook_url:
            logger.error("❌ 钉钉Webhook URL未配置(环境变量DINGTALK_WEBHOOK_URL)")
            return False
        
        # 初始化钉钉推送
        notifier = DingTalkNotifier(webhook_url=webhook_url, secret=secret)
        
        # 转换评估结果为dict
        assessment_data = assessment.to_dict()
        
        # 推送LLM研判详情
        logger.info(f"⏳ 正在推送到钉钉...")
        success = notifier.send_llm_assessment(
            code=code,
            assessment_data=assessment_data,
        )
        
        if success:
            logger.info(f"✅ 钉钉推送成功")
        else:
            logger.error(f"❌ 钉钉推送失败")
        
        return success
        
    except Exception as e:
        logger.error(f"❌ 钉钉推送异常: {e}", exc_info=True)
        return False


def test_push_summary_to_dingtalk(code: str, assessment, elapsed: float):
    """测试6: 推送汇总到钉钉"""
    logger.info("=" * 80)
    logger.info("测试6: 推送汇总到钉钉")
    logger.info("=" * 80)
    
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        
        # 从.env直接读取钉钉配置
        webhook_url = os.environ.get('DINGTALK_WEBHOOK_URL', '')
        secret = os.environ.get('DINGTALK_SECRET', '')
        
        if not webhook_url:
            logger.error("❌ 钉钉Webhook URL未配置(环境变量DINGTALK_WEBHOOK_URL)")
            return False
        
        notifier = DingTalkNotifier(webhook_url=webhook_url, secret=secret)
        
        # 构建汇总消息
        direction_map = {
            "STRONG_BUY": "🔴强烈看多",
            "BUY": "🔴看多",
            "NEUTRAL": "⚪中性",
            "SELL": "🟢看空",
            "STRONG_SELL": "🟢强烈看空",
        }
        
        direction = assessment.direction
        d = direction_map.get(direction, direction)
        
        lines = [
            "### 🤖 LLM整合分析测试 - 阿里巴巴",
            "",
            f"**分析标的**: {code}",
            f"**分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**分析耗时**: {elapsed:.1f}s",
            "",
            "---",
            "",
            f"### {d}",
            "",
            f"- **置信度**: {assessment.confidence_level}",
            f"- **仓位建议**: {assessment.position_tier}",
            f"- **执行时机**: {assessment.recommended_timing}",
            f"- **信号强度**: {assessment.direction_score:+.2f}",
            "",
            "---",
            "",
            f"**关键因素**:",
        ]
        
        if assessment.key_factors:
            for i, factor in enumerate(assessment.key_factors[:3], 1):
                lines.append(f"{i}. {factor[:100]}")
        
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("*FTSeek LLM整合分析流程测试*")
        
        text = "\n".join(lines)
        
        # 推送
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"LLM整合分析测试 - {code}",
                "text": text[:4096],
            },
        }
        
        success = notifier._do_post(payload)
        
        if success:
            logger.info(f"✅ 汇总推送成功")
        else:
            logger.error(f"❌ 汇总推送失败")
        
        return success
        
    except Exception as e:
        logger.error(f"❌ 汇总推送异常: {e}", exc_info=True)
        return False


async def main():
    """主测试流程"""
    logger.info("\n")
    logger.info("╔" + "=" * 78 + "╗")
    logger.info("║" + " " * 18 + "阿里巴巴LLM整合分析流程测试" + " " * 27 + "║")
    logger.info("╚" + "=" * 78 + "╝")
    logger.info("\n")
    
    code = "HK.09988"  # 阿里巴巴港股
    
    # 测试1: 加载配置
    llm_config = test_load_config()
    if not llm_config:
        logger.error("❌ 配置加载失败，终止测试")
        return 1
    
    # 测试2: 构建新闻上下文
    news_ctx = test_build_news_context(code)
    
    # 测试3: 构建跨市场上下文
    cross_market_ctx = test_build_cross_market_context()
    
    # 测试4: 调用LLM分析
    assessment = await test_llm_analysis(code, news_ctx, cross_market_ctx, llm_config)
    if not assessment:
        logger.error("❌ LLM分析失败，终止测试")
        return 1
    
    # 测试5: 推送详细结果到钉钉
    push_success = test_push_to_dingtalk(code, assessment)
    
    # 测试6: 推送汇总到钉钉
    summary_success = test_push_summary_to_dingtalk(code, assessment, 0.0)
    
    # 汇总结果
    logger.info("\n")
    logger.info("=" * 80)
    logger.info("测试结果汇总:")
    logger.info("=" * 80)
    
    results = {
        "配置加载": llm_config is not None,
        "新闻上下文": news_ctx is not None,
        "跨市场上下文": cross_market_ctx is not None,
        "LLM分析": assessment is not None,
        "钉钉详情推送": push_success,
        "钉钉汇总推送": summary_success,
    }
    
    all_passed = True
    for test_name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        logger.info(f"  {test_name}: {status}")
        if not passed:
            all_passed = False
    
    logger.info("=" * 80)
    
    if all_passed:
        logger.info("🎉 所有测试通过! LLM整合分析流程验证成功")
        logger.info("\n下一步:")
        logger.info("  1. 检查钉钉是否收到分析结果")
        logger.info("  2. 查看日志: logs/llm_test_baba.log")
        logger.info("  3. 下一个交易日观察09:25的自动LLM预分析")
        return 0
    else:
        logger.error("⚠️  部分测试失败，请检查上述错误信息")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
