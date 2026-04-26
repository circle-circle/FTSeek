#!/usr/bin/env python3
"""
跨市场LLM预分析完整流程测试

功能:
1. 获取阿里巴巴(HK.09988)相关新闻
2. 获取美股上周行情(BABA.US)
3. 调用LLM整合分析
4. 推送到钉钉
5. 验证完整链路

使用方法:
  cd /opt/ftseek/FTSeek
  python scripts/test_llm_cross_market_flow.py
"""

import sys
import os
import logging
import json
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("test_llm_flow")


def load_env_config():
    """加载.env配置"""
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    load_dotenv(env_path)
    
    config = {
        'bailian_api_key': os.getenv('BAILIAN_API_KEY'),
        'bailian_model': os.getenv('BAILIAN_MODEL', 'bailian/qwen3.5-plus'),
        'bailian_timeout': 120,  # 思维链模式需要更长时间，至少120秒
        'dingtalk_webhook': os.getenv('DINGTALK_WEBHOOK_URL'),
        'dingtalk_secret': os.getenv('DINGTALK_SECRET'),
    }
    
    # 确保模型名称包含前缀
    if not config['bailian_model'].startswith('bailian/'):
        config['bailian_model'] = f"bailian/{config['bailian_model']}"
    
    logger.info("=" * 80)
    logger.info("配置加载完成")
    logger.info("=" * 80)
    logger.info(f"BAILIAN_MODEL: {config['bailian_model']}")
    logger.info(f"BAILIAN_TIMEOUT: {config['bailian_timeout']}s")
    logger.info(f"DINGTALK_WEBHOOK: {'已配置' if config['dingtalk_webhook'] else '未配置'}")
    logger.info("=" * 80)
    
    return config


def get_news_for_stock(stock_code: str) -> dict:
    """获取标的相关新闻(模拟数据)"""
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤1: 获取 {stock_code} 相关新闻")
    logger.info(f"{'='*80}")
    
    # 模拟新闻数据(实际应用中应从新闻API或数据库获取)
    news_data = {
        "stock_code": stock_code,
        "news_count": 5,
        "news_items": [
            {
                "title": "阿里巴巴发布最新季报，云业务增长强劲",
                "source": "财联社",
                "published": "2026-04-11 14:30",
                "summary": "阿里巴巴集团发布2026财年Q4财报，云智能集团收入同比增长25%，AI相关产品收入连续六个季度三位数增长。"
            },
            {
                "title": "阿里云推出新一代大模型，对标GPT-5",
                "source": "36氪",
                "published": "2026-04-10 09:15",
                "summary": "阿里云在北京发布会上推出通义千问5.0模型，在多项基准测试中超越GPT-4，接近GPT-5水平。"
            },
            {
                "title": "港股科技板块持续走强，阿里巴巴获外资增持",
                "source": "东方财富",
                "published": "2026-04-09 16:20",
                "summary": "近期港股科技板块持续走强，摩根士丹利增持阿里巴巴港股约2.3亿港元，看好其AI业务长期发展。"
            },
            {
                "title": "中美贸易关系缓和，中概股普遍受益",
                "source": "华尔街见闻",
                "published": "2026-04-08 21:00",
                "summary": "中美就部分关税问题达成初步共识，市场风险偏好提升，中概股普遍上涨。"
            },
            {
                "title": "阿里巴巴电商业务恢复增长，拼多多竞争压力加大",
                "source": "晚点LatePost",
                "published": "2026-04-07 10:45",
                "summary": "淘天集团CEO透露，2026年Q4 GMV同比增长12%，扭转连续三个季度下滑趋势，但拼多多仍在价格战中保持优势。"
            }
        ]
    }
    
    logger.info(f"获取到 {news_data['news_count']} 条新闻:")
    for i, news in enumerate(news_data['news_items'], 1):
        logger.info(f"  [{i}] {news['title']} ({news['source']}, {news['published']})")
    
    return news_data


def get_us_market_data(us_code: str) -> dict:
    """获取美股行情数据(模拟数据)"""
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤2: 获取 {us_code} 上周行情")
    logger.info(f"{'='*80}")
    
    # 模拟美股数据
    us_data = {
        "stock_code": us_code,
        "period": "上周(2026-04-04 至 2026-04-11)",
        "weekly_summary": {
            "open": 118.50,
            "close": 125.80,
            "high": 127.20,
            "low": 117.90,
            "change_pct": 6.16,
            "volume_avg": "28.5M",
            "turnover": "3.58B"
        },
        "daily_klines": [
            {"date": "2026-04-04", "open": 118.50, "close": 119.80, "high": 120.50, "low": 117.90, "volume": "25.2M", "change": 1.10},
            {"date": "2026-04-07", "open": 120.00, "close": 122.30, "high": 123.10, "low": 119.50, "volume": "27.8M", "change": 2.09},
            {"date": "2026-04-08", "open": 122.50, "close": 124.60, "high": 125.80, "low": 121.90, "volume": "30.1M", "change": 1.88},
            {"date": "2026-04-09", "open": 124.80, "close": 123.50, "high": 126.20, "low": 123.10, "volume": "26.5M", "change": -0.88},
            {"date": "2026-04-10", "open": 123.80, "close": 125.80, "high": 127.20, "low": 123.40, "volume": "32.9M", "change": 1.86},
        ],
        "technical_indicators": {
            "ma5": 123.40,
            "ma10": 121.80,
            "rsi_14": 68.5,
            "macd": 1.25,
            "signal": 0.85,
            "volume_trend": "放量上涨"
        },
        "market_sentiment": {
            "sector": "中概股/科技",
            "sector_performance": "+3.8%",
            "spy_correlation": 0.72,
            "analyst_ratings": {
                "buy": 18,
                "hold": 5,
                "sell": 2,
                "avg_target": 145.00
            }
        }
    }
    
    logger.info(f"\n上周行情摘要:")
    logger.info(f"  开盘: ${us_data['weekly_summary']['open']}")
    logger.info(f"  收盘: ${us_data['weekly_summary']['close']}")
    logger.info(f"  最高: ${us_data['weekly_summary']['high']}")
    logger.info(f"  最低: ${us_data['weekly_summary']['low']}")
    logger.info(f"  周涨幅: {us_data['weekly_summary']['change_pct']}%")
    logger.info(f"  日均成交量: {us_data['weekly_summary']['volume_avg']}")
    
    logger.info(f"\n技术指标:")
    logger.info(f"  MA5: ${us_data['technical_indicators']['ma5']}")
    logger.info(f"  MA10: ${us_data['technical_indicators']['ma10']}")
    logger.info(f"  RSI(14): {us_data['technical_indicators']['rsi_14']}")
    logger.info(f"  MACD: {us_data['technical_indicators']['macd']}")
    logger.info(f"  量能趋势: {us_data['technical_indicators']['volume_trend']}")
    
    logger.info(f"\n分析师评级:")
    ratings = us_data['market_sentiment']['analyst_ratings']
    logger.info(f"  买入: {ratings['buy']} | 持有: {ratings['hold']} | 卖出: {ratings['sell']}")
    logger.info(f"  平均目标价: ${ratings['avg_target']}")
    
    return us_data


def call_llm_analysis(news_data: dict, us_data: dict, config: dict):
    """调用LLM进行整合分析"""
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤3: 调用LLM整合分析(模型: {config['bailian_model']})")
    logger.info(f"{'='*80}")
    
    try:
        import asyncio
        
        # 构建分析提示词
        prompt = f"""你是一位资深的美股/港股分析师，请根据以下信息对阿里巴巴(BABA/HK.09988)进行综合分析:

## 一、相关新闻(最近一周)
"""
        for i, news in enumerate(news_data['news_items'], 1):
            prompt += f"""
{i}. 【{news['title']}】
   来源: {news['source']} | 时间: {news['published']}
   摘要: {news['summary']}
"""
        
        prompt += f"""
## 二、美股BABA上周行情(2026-04-04 至 2026-04-11)

**周度摘要:**
- 开盘: ${us_data['weekly_summary']['open']}
- 收盘: ${us_data['weekly_summary']['close']}
- 最高: ${us_data['weekly_summary']['high']}
- 最低: ${us_data['weekly_summary']['low']}
- 周涨幅: {us_data['weekly_summary']['change_pct']}%
- 日均成交量: {us_data['weekly_summary']['volume_avg']}

**日线数据:**
"""
        for kline in us_data['daily_klines']:
            prompt += f"- {kline['date']}: 开{kline['open']} 收{kline['close']} 高{kline['high']} 低{kline['low']} 量{kline['volume']} 涨跌{kline['change']:+.2f}%\n"
        
        prompt += f"""
**技术指标:**
- MA5: ${us_data['technical_indicators']['ma5']}
- MA10: ${us_data['technical_indicators']['ma10']}
- RSI(14): {us_data['technical_indicators']['rsi_14']}
- MACD: {us_data['technical_indicators']['macd']} / Signal: {us_data['technical_indicators']['signal']}
- 量能趋势: {us_data['technical_indicators']['volume_trend']}

**市场情绪:**
- 所属板块: {us_data['market_sentiment']['sector']}
- 板块表现: {us_data['market_sentiment']['sector_performance']}
- 分析师评级: 买入{us_data['market_sentiment']['analyst_ratings']['buy']} / 持有{us_data['market_sentiment']['analyst_ratings']['hold']} / 卖出{us_data['market_sentiment']['analyst_ratings']['sell']}
- 平均目标价: ${us_data['market_sentiment']['analyst_ratings']['avg_target']}

## 三、分析要求

请从以下维度进行深度分析:

1. **消息面分析**: 新闻对股价的影响(利多/利空/中性)，市场情绪倾向
2. **技术面分析**: 趋势判断、关键支撑/阻力位、量价关系
3. **资金面分析**: 成交量变化、外资动向、机构评级
4. **风险提示**: 短期风险因素、中长期不确定性
5. **交易建议**: 针对下周港股开盘(HK.09988)，给出明确的多空方向、置信度、仓位建议

## 四、输出格式

请严格按照以下JSON格式输出(不要输出其他内容):

```json
{{
  "direction": "STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL",
  "confidence_level": "HIGH/MEDIUM/LOW",
  "position_tier": "FULL/HALF/LIGHT/SKIP",
  "key_factors": ["因素1", "因素2", "因素3"],
  "risk_flags": ["风险1", "风险2"],
  "reasoning": "综合分析逻辑(200字以内)",
  "target_price": 目标价(数字),
  "stop_loss": 止损价(数字),
  "news_sentiment": "利多/利空/中性",
  "technical_trend": "上涨趋势/震荡/下跌趋势",
  "trading_logic": "详细的买卖逻辑说明(300字以内)"
}}
```
"""
        
        # 调用LLM(异步)
        logger.info(f"\n正在调用 {config['bailian_model']} (超时: {config['bailian_timeout']}s)...")
        
        # 使用项目中的LLM客户端
        sys.path.insert(0, '/opt/ftseek/ExpertTeam')
        from utils.llm_client import LLMClient
        
        async def run_llm():
            client = LLMClient(
                model=config['bailian_model'],
                timeout=config['bailian_timeout'],
                max_retries=2
            )
            
            messages = [
                {"role": "system", "content": "你是一位资深的美股/港股分析师，擅长技术分析和基本面分析。"},
                {"role": "user", "content": prompt}
            ]
            
            response = await client.chat(messages)
            return response.content
        
        # 运行异步调用
        response = asyncio.get_event_loop().run_until_complete(run_llm())
        
        # 解析JSON响应
        import re
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            analysis_result = json.loads(json_match.group())
            logger.info(f"\nLLM分析完成!")
            logger.info(f"  方向: {analysis_result.get('direction')}")
            logger.info(f"  置信度: {analysis_result.get('confidence_level')}")
            logger.info(f"  仓位: {analysis_result.get('position_tier')}")
            logger.info(f"  消息面: {analysis_result.get('news_sentiment')}")
            logger.info(f"  技术面: {analysis_result.get('technical_trend')}")
            logger.info(f"  目标价: ${analysis_result.get('target_price')}")
            logger.info(f"  止损价: ${analysis_result.get('stop_loss')}")
            
            return analysis_result
        else:
            logger.error(f"LLM响应解析失败: {response[:200]}")
            return None
            
    except Exception as e:
        logger.error(f"LLM调用失败: {e}", exc_info=True)
        return None


def send_to_dingtalk(analysis_result: dict, news_data: dict, us_data: dict, config: dict):
    """发送分析结果到钉钉"""
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤4: 发送分析结果到钉钉")
    logger.info(f"{'='*80}")
    
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        
        # 初始化钉钉推送
        notifier = DingTalkNotifier(
            webhook_url=config['dingtalk_webhook'],
            secret=config['dingtalk_secret']
        )
        
        # 构建完整的分析数据
        assessment_data = {
            'direction': analysis_result.get('direction', 'NEUTRAL'),
            'confidence_level': analysis_result.get('confidence_level', 'MEDIUM'),
            'position_tier': analysis_result.get('position_tier', 'LIGHT'),
            'recommended_timing': 'IMMEDIATE' if analysis_result.get('direction') in ['STRONG_BUY', 'BUY'] else 'WAIT_CONFIRM',
            'key_factors': analysis_result.get('key_factors', []),
            'risk_flags': analysis_result.get('risk_flags', []),
            'reasoning': analysis_result.get('reasoning', ''),
            'news_impact': {
                'sentiment': analysis_result.get('news_sentiment', '中性'),
                'news_count': news_data['news_count'],
                'top_news': [n['title'] for n in news_data['news_items'][:3]]
            },
            'cross_market_insight': f"美股BABA上周涨{us_data['weekly_summary']['change_pct']}%, 技术指标{analysis_result.get('technical_trend', '震荡')}, 目标价${analysis_result.get('target_price', 'N/A')}",
            'trading_logic': analysis_result.get('trading_logic', ''),
            'technical_summary': {
                'ma5': us_data['technical_indicators']['ma5'],
                'ma10': us_data['technical_indicators']['ma10'],
                'rsi': us_data['technical_indicators']['rsi_14'],
                'volume_trend': us_data['technical_indicators']['volume_trend']
            }
        }
        
        # 发送到钉钉
        success = notifier.send_llm_assessment(
            code="HK.09988",
            assessment_data=assessment_data
        )
        
        if success:
            logger.info(f"\n✅ 钉钉推送成功!")
        else:
            logger.error(f"\n❌ 钉钉推送失败")
        
        return success
        
    except Exception as e:
        logger.error(f"钉钉推送异常: {e}", exc_info=True)
        return False


def main():
    """主流程"""
    logger.info("=" * 80)
    logger.info("跨市场LLM预分析完整流程测试")
    logger.info(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"测试标的: 阿里巴巴 HK.09988 / BABA.US")
    logger.info("=" * 80)
    
    # 步骤0: 加载配置
    config = load_env_config()
    
    if not config['bailian_api_key']:
        logger.error("BAILIAN_API_KEY 未配置，退出测试")
        return False
    
    if not config['dingtalk_webhook']:
        logger.error("DINGTALK_WEBHOOK_URL 未配置，退出测试")
        return False
    
    # 步骤1: 获取新闻
    news_data = get_news_for_stock("HK.09988")
    
    # 步骤2: 获取美股行情
    us_data = get_us_market_data("BABA.US")
    
    # 步骤3: 调用LLM分析
    analysis_result = call_llm_analysis(news_data, us_data, config)
    
    if not analysis_result:
        logger.error("LLM分析失败，跳过钉钉推送")
        return False
    
    # 步骤4: 发送到钉钉
    success = send_to_dingtalk(analysis_result, news_data, us_data, config)
    
    # 总结
    logger.info(f"\n{'='*80}")
    logger.info("测试总结")
    logger.info(f"{'='*80}")
    logger.info(f"✅ 新闻获取: 成功 ({news_data['news_count']}条)")
    logger.info(f"✅ 美股行情: 成功 ({us_data['period']})")
    logger.info(f"{'✅' if analysis_result else '❌'} LLM分析: {'成功' if analysis_result else '失败'}")
    logger.info(f"{'✅' if success else '❌'} 钉钉推送: {'成功' if success else '失败'}")
    logger.info(f"{'='*80}")
    
    return success


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        logger.info("\n测试被用户中断")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\n测试异常: {e}", exc_info=True)
        sys.exit(1)
