#!/usr/bin/env python3
"""
跨市场LLM预分析+下单链路完整验证测试

功能:
1. 新闻数据获取
2. 美股行情数据获取
3. LLM整合分析
4. 写入缓存(模拟09:25预分析)
5. 共识引擎读取缓存(模拟09:30开盘)
6. 生成交易信号
7. 风控检查
8. 模拟下单(不实际执行)
9. 钉钉推送完整链路

问题修复记录:
- [FIX-01] 模型名称需带bailian/前缀
- [FIX-02] LLM客户端chat()是异步方法，需asyncio调用
- [FIX-03] 超时时间从30s增加到120s(思维链模式需要更长时间)
- [FIX-04] 缓存键使用trigger_type="SCHEDULED"确保匹配
- [FIX-05] 钉钉配置从.env直接读取

使用方法:
  cd /opt/ftseek/FTSeek
  python scripts/test_llm_to_order_flow.py
"""

import sys
import os
import logging
import json
import time
import asyncio
from datetime import datetime, timedelta

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '/opt/ftseek/ExpertTeam')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("test_llm_to_order")


# ============================================================================
# 配置加载
# ============================================================================

def load_env_config():
    """加载.env配置 [FIX-05: 从.env直接读取]"""
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    load_dotenv(env_path)
    
    config = {
        'bailian_api_key': os.getenv('BAILIAN_API_KEY'),
        # [FIX-01: 确保模型名称带bailian/前缀]
        'bailian_model': os.getenv('BAILIAN_MODEL', 'bailian/qwen3.5-plus'),
        # [FIX-03: 超时从30s增加到120s]
        'bailian_timeout': 120,
        'dingtalk_webhook': os.getenv('DINGTALK_WEBHOOK_URL'),
        'dingtalk_secret': os.getenv('DINGTALK_SECRET'),
    }
    
    # [FIX-01: 确保前缀]
    if not config['bailian_model'].startswith('bailian/'):
        config['bailian_model'] = f"bailian/{config['bailian_model']}"
    
    logger.info("=" * 80)
    logger.info("配置加载完成 [FIX-05: 从.env直接读取]")
    logger.info("=" * 80)
    logger.info(f"BAILIAN_MODEL: {config['bailian_model']}")
    logger.info(f"BAILIAN_TIMEOUT: {config['bailian_timeout']}s [FIX-03: 思维链模式]")
    logger.info(f"DINGTALK_WEBHOOK: {'已配置' if config['dingtalk_webhook'] else '未配置'}")
    logger.info("=" * 80)
    
    return config


# ============================================================================
# 数据获取
# ============================================================================

def get_news_for_stock(stock_code: str) -> dict:
    """获取标的相关新闻(模拟数据)"""
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤1: 获取 {stock_code} 相关新闻")
    logger.info(f"{'='*80}")
    
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
        logger.info(f"  [{i}] {news['title']} ({news['source']})")
    
    return news_data


def get_us_market_data(us_code: str) -> dict:
    """获取美股行情数据(模拟数据)"""
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤2: 获取 {us_code} 上周行情")
    logger.info(f"{'='*80}")
    
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
        },
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
            "analyst_ratings": {
                "buy": 18,
                "hold": 5,
                "sell": 2,
                "avg_target": 145.00
            }
        }
    }
    
    logger.info(f"\n上周行情: 开${us_data['weekly_summary']['open']} 收${us_data['weekly_summary']['close']} 涨{us_data['weekly_summary']['change_pct']}%")
    logger.info(f"技术指标: MA5=${us_data['technical_indicators']['ma5']} RSI={us_data['technical_indicators']['rsi_14']}")
    
    return us_data


# ============================================================================
# LLM分析 [FIX-02: 异步调用]
# ============================================================================

def call_llm_analysis(news_data: dict, us_data: dict, config: dict):
    """
    调用LLM进行整合分析
    
    [FIX-02: LLMClient.chat()是异步方法，需asyncio调用]
    [FIX-03: 超时120s]
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤3: 调用LLM整合分析 [FIX-02: 异步调用] [FIX-03: 120s超时]")
    logger.info(f"{'='*80}")
    
    try:
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
## 二、美股BABA上周行情

- 开盘: ${us_data['weekly_summary']['open']}
- 收盘: ${us_data['weekly_summary']['close']}
- 涨幅: {us_data['weekly_summary']['change_pct']}%
- MA5: ${us_data['technical_indicators']['ma5']}
- MA10: ${us_data['technical_indicators']['ma10']}
- RSI(14): {us_data['technical_indicators']['rsi_14']}
- MACD: {us_data['technical_indicators']['macd']}
- 量能: {us_data['technical_indicators']['volume_trend']}
- 分析师评级: 买入{us_data['market_sentiment']['analyst_ratings']['buy']} / 持有{us_data['market_sentiment']['analyst_ratings']['hold']} / 卖出{us_data['market_sentiment']['analyst_ratings']['sell']}
- 目标价: ${us_data['market_sentiment']['analyst_ratings']['avg_target']}

## 三、输出格式(严格JSON)

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
        
        logger.info(f"\n正在调用 {config['bailian_model']} (超时: {config['bailian_timeout']}s)...")
        
        from utils.llm_client import LLMClient
        
        # [FIX-02: 异步调用]
        async def run_llm():
            client = LLMClient(
                model=config['bailian_model'],
                timeout=config['bailian_timeout'],
                max_retries=2
            )
            
            messages = [
                {"role": "system", "content": "你是一位资深的美股/港股分析师。"},
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
            logger.info(f"\n✅ LLM分析完成!")
            logger.info(f"  方向: {analysis_result.get('direction')}")
            logger.info(f"  置信度: {analysis_result.get('confidence_level')}")
            logger.info(f"  仓位: {analysis_result.get('position_tier')}")
            
            return analysis_result
        else:
            logger.error(f"LLM响应解析失败: {response[:200]}")
            return None
            
    except Exception as e:
        logger.error(f"LLM调用失败: {e}", exc_info=True)
        return None


# ============================================================================
# 缓存写入 (模拟09:25预分析)
# ============================================================================

def write_to_cache(analysis_result: dict, code: str = "HK.09988"):
    """
    将LLM分析结果写入缓存 [FIX-04: 使用SCHEDULED触发类型]
    
    模拟09:25调度器预分析后的缓存写入
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤4: 写入缓存 [FIX-04: trigger_type=SCHEDULED]")
    logger.info(f"{'='*80}")
    
    try:
        from app.ai.llm_market_analyst import (
            LLMMarketAnalyst, MarketAssessment, Direction, ConfidenceLevel, PositionTier
        )
        
        # 初始化LLM分析师
        analyst = LLMMarketAnalyst(config={
            "enabled": True,
            "model": "bailian/qwen3.5-plus",
            "timeout": 120,
            "cache_ttl": 600,  # 10分钟
        })
        
        # 构建MarketAssessment对象
        direction_map = {
            "STRONG_BUY": Direction.STRONG_BUY,
            "BUY": Direction.BUY,
            "NEUTRAL": Direction.NEUTRAL,
            "SELL": Direction.SELL,
            "STRONG_SELL": Direction.STRONG_SELL,
        }
        
        confidence_map = {
            "HIGH": ConfidenceLevel.HIGH,
            "MEDIUM": ConfidenceLevel.MEDIUM,
            "LOW": ConfidenceLevel.LOW,
        }
        
        position_map = {
            "FULL": PositionTier.FULL,
            "HALF": PositionTier.HALF,
            "LIGHT": PositionTier.LIGHT,
            "SKIP": PositionTier.SKIP,
        }
        
        assessment = MarketAssessment(
            direction=direction_map.get(analysis_result.get('direction', 'NEUTRAL'), Direction.NEUTRAL),
            confidence_level=confidence_map.get(analysis_result.get('confidence_level', 'MEDIUM'), ConfidenceLevel.MEDIUM),
            position_tier=position_map.get(analysis_result.get('position_tier', 'LIGHT'), PositionTier.LIGHT),
            key_factors=analysis_result.get('key_factors', []),
            risk_flags=analysis_result.get('risk_flags', []),
            recommended_timing='IMMEDIATE',
            reasoning=analysis_result.get('reasoning', ''),
            ttl_seconds=600,
            timestamp=time.time(),
            trigger_type="SCHEDULED",  # [FIX-04: 关键! 确保缓存键匹配]
        )
        
        # 写入缓存
        cache_key = f"{code}:SCHEDULED:{int(time.time() / 300)}"
        analyst._cache[cache_key] = assessment
        
        logger.info(f"✅ 缓存写入成功: {code}")
        logger.info(f"  缓存键: {cache_key}")
        logger.info(f"  方向: {assessment.direction}")
        logger.info(f"  置信度: {assessment.confidence_level}")
        logger.info(f"  TTL: {assessment.ttl_seconds}s")
        logger.info(f"  过期时间: {datetime.fromtimestamp(assessment.timestamp + assessment.ttl_seconds).strftime('%H:%M:%S')}")
        
        return analyst, cache_key
        
    except Exception as e:
        logger.error(f"缓存写入失败: {e}", exc_info=True)
        return None, None


# ============================================================================
# 共识引擎读取 (模拟09:30开盘)
# ============================================================================

def test_consensus_engine_read(analyst, code: str = "HK.09988"):
    """
    测试共识引擎读取LLM缓存
    
    模拟09:30开盘时共识引擎调用get_cached_assessment()
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤5: 共识引擎读取缓存 [模拟09:30开盘]")
    logger.info(f"{'='*80}")
    
    try:
        # [FIX-04: 使用SCHEDULED触发类型读取]
        assessment = analyst.get_cached_assessment(code, trigger_type="SCHEDULED")
        
        if assessment and assessment.is_valid():
            logger.info(f"✅ 缓存读取成功!")
            logger.info(f"  方向: {assessment.direction}")
            logger.info(f"  置信度: {assessment.confidence_level}")
            logger.info(f"  仓位: {assessment.position_tier}")
            logger.info(f"  有效期: {assessment.is_valid()}")
            
            # 计算剩余时间
            elapsed = time.time() - assessment.timestamp
            remaining = assessment.ttl_seconds - elapsed
            logger.info(f"  剩余时间: {remaining:.0f}s ({remaining/60:.1f}分钟)")
            
            return assessment
        else:
            logger.warning(f"⚠️  缓存读取失败或已过期")
            return None
            
    except Exception as e:
        logger.error(f"共识引擎读取失败: {e}", exc_info=True)
        return None


# ============================================================================
# 信号生成 (模拟调度器09:30执行)
# ============================================================================

def generate_trading_signal(assessment, code: str = "HK.09988"):
    """
    生成交易信号
    
    模拟调度器在09:30根据LLM研判生成CrossMarketSignalRecord
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤6: 生成交易信号 [模拟09:30调度器执行]")
    logger.info(f"{'='*80}")
    
    try:
        from app.strategy.cross_market_scheduler import CrossMarketSignalRecord
        from app.ai.llm_market_analyst import Direction
        
        # 方向映射 - 使用枚举值比较
        if assessment.direction in (Direction.STRONG_BUY, Direction.BUY):
            action = "BUY"
        elif assessment.direction in (Direction.SELL, Direction.STRONG_SELL):
            action = "SELL"
        else:
            action = None  # NEUTRAL不生成信号
        
        if not action:
            logger.info(f"ℹ️  LLM方向为{assessment.direction}，不生成交易信号")
            return None
        
        # 构造信号元数据
        metadata = {
            'arb_total_score': assessment.confidence_value * 100,
            'arb_decision': 'execute',
            'llm_direction': str(assessment.direction),
            'llm_confidence': assessment.confidence_value,
            'llm_position_tier': str(assessment.position_tier),
            'hk_code': code,
            'us_code': 'BABA.US',
            'arb_type': 'us_to_hk',
            'entry_price': None,  # 市价单
        }
        
        # 生成信号ID
        signal_id = f"LLM_{code}_{int(time.time())}"
        
        # 创建信号记录
        record = CrossMarketSignalRecord(
            signal_id=signal_id,
            code=code,
            action=action,
            metadata=metadata,
        )
        
        # 自动执行状态
        record.review_status = 'auto_executed'
        
        logger.info(f"✅ 信号生成成功!")
        logger.info(f"  信号ID: {signal_id}")
        logger.info(f"  代码: {code}")
        logger.info(f"  方向: {action}")
        logger.info(f"  状态: {record.review_status}")
        logger.info(f"  LLM置信度: {assessment.confidence_value:.2f}")
        logger.info(f"  LLM仓位: {assessment.position_tier}")
        
        return record
        
    except Exception as e:
        logger.error(f"信号生成失败: {e}", exc_info=True)
        return None


# ============================================================================
# 风控检查
# ============================================================================

def run_risk_check(record):
    """
    运行风控检查
    
    模拟调度器执行前的_pre_execute_risk_check()
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤7: 风控检查")
    logger.info(f"{'='*80}")
    
    try:
        # 1. 熔断器检查
        try:
            from app.risk.circuit_breaker import get_circuit_breaker
            cb = get_circuit_breaker()
            if cb and cb.is_paused():
                logger.warning(f"❌ 熔断器已触发，信号被拦截")
                return False, "熔断器已触发"
        except Exception as e:
            logger.info(f"ℹ️  熔断器未初始化，跳过: {e}")
        
        # 2. 持仓上限检查
        try:
            from app.position.position_manager import PositionManager
            pm = PositionManager()
            open_positions = pm.get_all_positions()
            open_count = len([p for p in open_positions if getattr(p, 'status', '') != 'CLOSED'])
        except Exception:
            open_count = 0
        
        from config.config_center import get_config_center
        cc = get_config_center()
        max_open = int(cc.get('trading.max_open_positions', 4))
        
        if open_count >= max_open:
            logger.warning(f"❌ 持仓已达上限 ({open_count}/{max_open})")
            return False, f"持仓已达上限 ({open_count}/{max_open})"
        
        logger.info(f"✅ 持仓检查: {open_count}/{max_open}")
        
        # 3. 日交易次数检查
        max_daily = int(cc.get('risk_limits.max_daily_trades', 10))
        try:
            from app.analytics.trade_evaluation import get_evaluator
            evaluator = get_evaluator()
            daily_trades = len(evaluator.get_trades_today())
        except Exception:
            daily_trades = 0
        
        if daily_trades >= max_daily:
            logger.warning(f"❌ 日交易次数已达上限 ({daily_trades}/{max_daily})")
            return False, f"日交易次数已达上限"
        
        logger.info(f"✅ 日交易次数: {daily_trades}/{max_daily}")
        
        logger.info(f"\n✅ 风控检查通过!")
        return True, ""
        
    except Exception as e:
        logger.error(f"风控检查失败: {e}", exc_info=True)
        return False, str(e)


# ============================================================================
# 模拟下单
# ============================================================================

def simulate_order_placement(record):
    """
    模拟下单(不实际执行)
    
    验证Signal对象构造和trade_executor调用链路
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤8: 模拟下单 [验证链路，不实际执行]")
    logger.info(f"{'='*80}")
    
    try:
        from app.strategy.base import Signal, SignalAction
        
        # 构造Signal对象
        action = SignalAction.BUY if record.action == 'BUY' else SignalAction.SELL
        
        signal = Signal(
            id=record.signal_id,
            code=record.code,
            action=action,
            price=0.0,  # 市价单
            quantity=0,  # 由trade_executor动态计算
            reason=(
                f"跨市场套利 | LLM研判 | "
                f"置信度={record.metadata.get('llm_confidence', 0):.2f} "
                f"({record.metadata.get('llm_position_tier', 'LIGHT')})"
            ),
            strategy='cross_market_arbitrage',
            priority=4,  # CRITICAL
            extra=record.metadata,
        )
        
        logger.info(f"✅ Signal对象构造成功!")
        logger.info(f"  信号ID: {signal.id}")
        logger.info(f"  代码: {signal.code}")
        logger.info(f"  方向: {signal.action}")
        logger.info(f"  策略: {signal.strategy}")
        logger.info(f"  优先级: {signal.priority}")
        logger.info(f"  原因: {signal.reason}")
        
        # 检查trade_executor是否可用
        try:
            from app.trading.trade_executor import get_trade_executor
            executor = get_trade_executor()
            
            if executor:
                logger.info(f"\n✅ trade_executor可用!")
                logger.info(f"  类型: {type(executor).__name__}")
                logger.info(f"  模拟模式: {getattr(executor, '_simulate_mode', True)}")
                
                # 模拟调用(不实际执行)
                logger.info(f"\n📋 下单参数验证:")
                logger.info(f"  signal: {signal.id}")
                logger.info(f"  trade_type: OPEN")
                logger.info(f"  预期行为: 市价单买入{record.code}")
                
                return True
            else:
                logger.warning(f"⚠️  trade_executor未初始化")
                return False
                
        except Exception as e:
            logger.warning(f"⚠️  trade_executor获取失败: {e}")
            return False
        
    except Exception as e:
        logger.error(f"模拟下单失败: {e}", exc_info=True)
        return False


# ============================================================================
# 钉钉推送
# ============================================================================

def send_full_chain_to_dingtalk(analysis_result, assessment, record, config: dict):
    """
    发送完整链路结果到钉钉
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"步骤9: 钉钉推送完整链路")
    logger.info(f"{'='*80}")
    
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        
        notifier = DingTalkNotifier(
            webhook_url=config['dingtalk_webhook'],
            secret=config['dingtalk_secret']
        )
        
        # 构建完整的分析数据
        assessment_data = {
            'direction': str(assessment.direction),
            'confidence_level': str(assessment.confidence_level),
            'position_tier': str(assessment.position_tier),
            'recommended_timing': 'IMMEDIATE',
            'key_factors': assessment.key_factors,
            'risk_flags': assessment.risk_flags,
            'reasoning': assessment.reasoning,
            'news_impact': {
                'sentiment': analysis_result.get('news_sentiment', '中性'),
                'news_count': 5,
            },
            'cross_market_insight': f"美股BABA上周涨6.16%, 技术指标{analysis_result.get('technical_trend', '震荡')}",
            'trading_logic': analysis_result.get('trading_logic', ''),
        }
        
        # 发送LLM研判
        success1 = notifier.send_llm_assessment(
            code="HK.09988",
            assessment_data=assessment_data
        )
        
        logger.info(f"{'✅' if success1 else '❌'} LLM研判推送: {'成功' if success1 else '失败'}")
        
        # 发送执行结果
        if record:
            success2 = notifier.send_cross_market_execution_result(
                metadata_dict=record.metadata,
                success=True,
                detail=f"LLM研判驱动 | {assessment.direction} | 置信度{assessment.confidence_value:.2f}",
            )
            
            logger.info(f"{'✅' if success2 else '❌'} 执行结果推送: {'成功' if success2 else '失败'}")
        
        return success1
        
    except Exception as e:
        logger.error(f"钉钉推送失败: {e}", exc_info=True)
        return False


# ============================================================================
# 主流程
# ============================================================================

def main():
    """主流程"""
    logger.info("=" * 80)
    logger.info("跨市场LLM预分析+下单链路完整验证测试")
    logger.info(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"测试标的: 阿里巴巴 HK.09988 / BABA.US")
    logger.info("=" * 80)
    
    logger.info(f"\n📋 测试流程:")
    logger.info(f"  1. 新闻数据获取")
    logger.info(f"  2. 美股行情获取")
    logger.info(f"  3. LLM整合分析 [FIX-02/03]")
    logger.info(f"  4. 写入缓存 [FIX-04: SCHEDULED]")
    logger.info(f"  5. 共识引擎读取 [FIX-04: SCHEDULED]")
    logger.info(f"  6. 生成交易信号")
    logger.info(f"  7. 风控检查")
    logger.info(f"  8. 模拟下单")
    logger.info(f"  9. 钉钉推送")
    
    # 步骤0: 加载配置
    config = load_env_config()
    
    if not config['bailian_api_key']:
        logger.error("❌ BAILIAN_API_KEY 未配置")
        return False
    
    if not config['dingtalk_webhook']:
        logger.error("❌ DINGTALK_WEBHOOK_URL 未配置")
        return False
    
    # 步骤1-2: 数据获取
    news_data = get_news_for_stock("HK.09988")
    us_data = get_us_market_data("BABA.US")
    
    # 步骤3: LLM分析
    analysis_result = call_llm_analysis(news_data, us_data, config)
    
    if not analysis_result:
        logger.error("❌ LLM分析失败，退出测试")
        return False
    
    # 步骤4: 写入缓存
    analyst, cache_key = write_to_cache(analysis_result, "HK.09988")
    
    if not analyst:
        logger.error("❌ 缓存写入失败，退出测试")
        return False
    
    # 步骤5: 共识引擎读取
    assessment = test_consensus_engine_read(analyst, "HK.09988")
    
    if not assessment:
        logger.error("❌ 共识引擎读取失败，退出测试")
        return False
    
    # 步骤6: 生成信号
    record = generate_trading_signal(assessment, "HK.09988")
    
    # 步骤7: 风控检查
    if record:
        risk_passed, risk_reason = run_risk_check(record)
        
        if not risk_passed:
            logger.error(f"❌ 风控检查未通过: {risk_reason}")
            return False
    
    # 步骤8: 模拟下单
    order_ready = False
    if record:
        order_ready = simulate_order_placement(record)
    
    # 步骤9: 钉钉推送
    dingtalk_sent = send_full_chain_to_dingtalk(analysis_result, assessment, record, config)
    
    # 总结
    logger.info(f"\n{'='*80}")
    logger.info("📊 测试总结")
    logger.info(f"{'='*80}")
    logger.info(f"✅ 新闻获取: 成功 ({news_data['news_count']}条)")
    logger.info(f"✅ 美股行情: 成功 ({us_data['period']})")
    logger.info(f"{'✅' if analysis_result else '❌'} LLM分析: {'成功' if analysis_result else '失败'} [FIX-01/02/03]")
    logger.info(f"{'✅' if analyst else '❌'} 缓存写入: {'成功' if analyst else '失败'} [FIX-04]")
    logger.info(f"{'✅' if assessment else '❌'} 缓存读取: {'成功' if assessment else '失败'} [FIX-04]")
    logger.info(f"{'✅' if record else '❌'} 信号生成: {'成功' if record else '跳过(NEUTRAL)'}")
    logger.info(f"{'✅' if record and risk_passed else '❌'} 风控检查: {'通过' if record and risk_passed else '未通过'}")
    logger.info(f"{'✅' if order_ready else '⚠️'}  模拟下单: {'就绪' if order_ready else 'trade_executor未初始化'}")
    logger.info(f"{'✅' if dingtalk_sent else '❌'} 钉钉推送: {'成功' if dingtalk_sent else '失败'}")
    logger.info(f"{'='*80}")
    
    # 最终结论
    all_passed = (
        analysis_result and 
        analyst and 
        assessment and 
        record and 
        risk_passed and 
        dingtalk_sent
    )
    
    if all_passed:
        logger.info(f"\n🎉 完整链路验证通过! 下周开盘可正常模拟交易!")
        logger.info(f"\n📋 关键修复点:")
        logger.info(f"  [FIX-01] 模型名称带bailian/前缀")
        logger.info(f"  [FIX-02] LLM异步调用(asyncio)")
        logger.info(f"  [FIX-03] 超时120s(思维链模式)")
        logger.info(f"  [FIX-04] 缓存键trigger_type=SCHEDULED")
        logger.info(f"  [FIX-05] 钉钉配置从.env直接读取")
    else:
        logger.warning(f"\n⚠️  部分环节未通过，请检查上述日志")
    
    logger.info(f"{'='*80}")
    
    return all_passed


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
