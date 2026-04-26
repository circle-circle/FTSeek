#!/usr/bin/env python3
"""
盘中新闻动态更新器 (Phase 6)

功能:
- 盘中每30分钟定期更新策略指导
- 重大新闻（P1/P2）实时触发更新
- LLM快速研判（30秒超时）
- 策略变化显著时推送钉钉

用法:
    from app.news_push.intraday_updater import IntradayNewsUpdater
    updater = IntradayNewsUpdater()
    updated_guidance = updater.on_breaking_news(news_item)
"""
import logging
import time
from typing import Optional, Dict
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftseek.news_push.intraday_updater")
logger.propagate = False


class IntradayNewsUpdater:
    """盘中新闻动态更新器"""
    
    def __init__(
        self,
        context_builder=None,
        guidance_engine=None,
        llm_analyst=None,
        notifier=None,
    ):
        """
        Args:
            context_builder: MarketContextBuilder实例
            guidance_engine: StrategyGuidanceEngine实例
            llm_analyst: LLMMarketAnalyst实例
            notifier: DingTalkNotifier实例
        """
        # 延迟导入
        if context_builder is None:
            try:
                from app.ai.market_context_builder import get_context_builder
                self.context_builder = get_context_builder()
            except Exception as e:
                logger.warning(f"MarketContextBuilder初始化失败: {e}")
                self.context_builder = None
        else:
            self.context_builder = context_builder
        
        if guidance_engine is None:
            try:
                from app.ai.strategy_guidance import get_guidance_engine
                self.guidance_engine = get_guidance_engine()
            except Exception as e:
                logger.warning(f"StrategyGuidanceEngine初始化失败: {e}")
                self.guidance_engine = None
        else:
            self.guidance_engine = guidance_engine
        
        if llm_analyst is None:
            try:
                from app.ai.llm_market_analyst import LLMMarketAnalyst
                self.llm_analyst = LLMMarketAnalyst()
            except Exception as e:
                logger.warning(f"LLMMarketAnalyst初始化失败: {e}")
                self.llm_analyst = None
        else:
            self.llm_analyst = llm_analyst
        
        if notifier is None:
            try:
                from app.notifier import get_dingtalk_notifier
                self.notifier = get_dingtalk_notifier()
            except Exception as e:
                logger.warning(f"DingTalkNotifier初始化失败: {e}")
                self.notifier = None
        else:
            self.notifier = notifier
        
        # 状态
        self.current_guidance: Optional[Dict] = None
        self.last_update_time: float = 0
        self.update_interval = 1800  # 30分钟基础更新间隔
        
        logger.info("IntradayNewsUpdater 初始化完成")
    
    def on_breaking_news(self, news) -> Optional[Dict]:
        """
        突发新闻触发更新
        
        Args:
            news: NewsItem实例
        
        Returns:
            更新后的策略指导（Dict格式），如无重大变化返回None
        """
        priority = getattr(news, 'priority', 5)
        is_breaking = getattr(news, 'is_breaking', False)
        
        # 仅P1/P2优先级或突发新闻触发
        if priority > 2 and not is_breaking:
            logger.debug(f"新闻优先级{priority}，不触发更新")
            return None
        
        title = getattr(news, 'title', '')
        logger.info(f"突发新闻触发策略更新: {title[:50]}")
        
        # 触发更新
        return self._trigger_update(trigger_news=news)
    
    def periodic_update(self, target_codes: list = None) -> Optional[Dict]:
        """
        定期更新（盘中每30分钟）
        
        Args:
            target_codes: 目标股票代码列表
        
        Returns:
            更新后的策略指导，如无变化返回None
        """
        now = time.time()
        
        # 检查是否到达更新间隔
        if now - self.last_update_time < self.update_interval:
            logger.debug(f"距上次更新{now - self.last_update_time:.0f}秒，未到更新间隔")
            return None
        
        logger.info("盘中定期更新触发")
        return self._trigger_update(target_codes=target_codes)
    
    def _trigger_update(self, trigger_news=None, target_codes: list = None) -> Optional[Dict]:
        """
        触发更新
        
        Args:
            trigger_news: 触发更新的新闻（可选）
            target_codes: 目标股票代码列表
        
        Returns:
            更新后的策略指导，如无显著变化返回None
        """
        try:
            # 1. 获取最新市场上下文
            if target_codes is None:
                target_codes = ['HK.00700', 'HK.09988', 'HK.03690']  # 默认监控股票
            
            if self.context_builder:
                context = self.context_builder.build_intraday_context(target_codes)
            else:
                logger.warning("上下文构建器不可用")
                return None
            
            # 2. LLM快速研判
            assessment = self._quick_llm_analysis(context)
            if not assessment:
                logger.warning("LLM快速研判失败")
                return None
            
            # 3. 生成/更新策略指导
            if self.guidance_engine:
                new_guidance = self.guidance_engine.generate_from_llm(assessment, context)
            else:
                logger.warning("策略指导引擎不可用")
                return None
            
            # 4. 对比旧指导，判断是否显著变化
            if self.current_guidance:
                if not self._is_significant_change(self.current_guidance, new_guidance.to_dict()):
                    logger.info("策略指导无显著变化，不推送")
                    return None
            
            # 5. 更新当前指导
            self.current_guidance = new_guidance.to_dict()
            self.last_update_time = time.time()
            
            # 6. 推送钉钉
            if self.notifier:
                self._push_update_to_dingtalk(new_guidance, trigger_news)
            
            logger.info(f"策略指导更新完成，已推送钉钉")
            return self.current_guidance
            
        except Exception as e:
            logger.error(f"策略更新失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _quick_llm_analysis(self, context: Dict):
        """
        LLM快速研判（30秒超时）
        
        Args:
            context: 市场上下文
        
        Returns:
            MarketAssessment实例
        """
        if not self.llm_analyst:
            logger.warning("LLM分析器不可用")
            return None
        
        import asyncio
        
        try:
            # 创建新事件循环
            loop = asyncio.new_event_loop()
            
            try:
                # 调用LLM分析（urgent=True使用30秒超时）
                assessment = loop.run_until_complete(
                    self.llm_analyst.analyze(
                        code="MARKET_WIDE",
                        news_context=context.get('news_summary', {}),
                        cross_market_context=context.get('cross_market', {}),
                        trigger_type="COMBINED",
                        urgent=True,  # 30秒超时
                    )
                )
                return assessment
            finally:
                loop.close()
                
        except Exception as e:
            logger.error(f"LLM快速研判失败: {e}")
            return None
    
    def _is_significant_change(self, old_guidance: Dict, new_guidance: Dict) -> bool:
        """
        判断策略指导是否有显著变化
        
        Args:
            old_guidance: 旧策略指导
            new_guidance: 新策略指导
        
        Returns:
            是否显著变化
        """
        # 1. 整体立场变化
        old_stance = old_guidance.get('overall_stance', '')
        new_stance = new_guidance.get('overall_stance', '')
        if old_stance != new_stance:
            logger.info(f"立场变化: {old_stance} → {new_stance}")
            return True
        
        # 2. 置信度变化>20%
        old_confidence = old_guidance.get('confidence', 0.5)
        new_confidence = new_guidance.get('confidence', 0.5)
        if abs(new_confidence - old_confidence) > 0.2:
            logger.info(f"置信度变化: {old_confidence:.0%} → {new_confidence:.0%}")
            return True
        
        # 3. 个股推荐变化>50%
        old_stocks = set(s['code'] for s in old_guidance.get('stock_picks', []))
        new_stocks = set(s['code'] for s in new_guidance.get('stock_picks', []))
        
        if old_stocks and new_stocks:
            overlap = len(old_stocks & new_stocks)
            change_ratio = 1 - overlap / max(len(old_stocks), len(new_stocks))
            if change_ratio > 0.5:
                logger.info(f"个股推荐变化{change_ratio:.0%}")
                return True
        
        return False
    
    def _push_update_to_dingtalk(self, guidance, trigger_news=None):
        """
        推送策略更新到钉钉
        
        Args:
            guidance: StrategyGuidance实例
            trigger_news: 触发更新的新闻（可选）
        """
        if not self.notifier:
            logger.warning("钉钉通知器不可用")
            return
        
        try:
            # 格式化策略指导
            if self.guidance_engine:
                message = self.guidance_engine.format_for_dingtalk(guidance)
            else:
                message = str(guidance.to_dict())
            
            # 如果是突发新闻触发，添加新闻信息
            if trigger_news:
                news_title = getattr(trigger_news, 'title', '')
                news_source = getattr(trigger_news, 'source', '')
                
                prefix_lines = [
                    "### 🚨 突发新闻触发策略更新",
                    "",
                    f"**新闻**: {news_title}",
                    f"**来源**: {news_source}",
                    f"**时间**: {datetime.now().strftime('%H:%M:%S')}",
                    "",
                    "---",
                    "",
                ]
                message = '\n'.join(prefix_lines) + message
            
            # 推送
            self.notifier.send_text(message, title="策略指导更新")
            logger.info("策略更新已推送钉钉")
            
        except Exception as e:
            logger.error(f"钉钉推送失败: {e}")
    
    def set_update_interval(self, seconds: int):
        """
        设置更新间隔
        
        Args:
            seconds: 更新间隔（秒）
        """
        self.update_interval = seconds
        logger.info(f"更新间隔已设置为{seconds}秒")
    
    def clear_current_guidance(self):
        """清空当前策略指导（用于盘前重置）"""
        self.current_guidance = None
        self.last_update_time = 0
        logger.info("当前策略指导已清空")


# 单例模式
_instance = None

def get_intraday_updater() -> IntradayNewsUpdater:
    """获取IntradayNewsUpdater单例"""
    global _instance
    if _instance is None:
        _instance = IntradayNewsUpdater()
    return _instance


if __name__ == '__main__':
    # 测试
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    updater = get_intraday_updater()
    
    print("\n" + "="*80)
    print("盘中动态更新器测试")
    print("="*80)
    
    # 模拟突发新闻
    class MockNews:
        def __init__(self):
            self.title = "央行宣布降准0.5%，释放流动性"
            self.source = "富途快讯"
            self.priority = 1
            self.is_breaking = True
    
    try:
        news = MockNews()
        print(f"\n模拟突发新闻: {news.title}")
        print(f"优先级: {news.priority}")
        
        # 触发更新
        result = updater.on_breaking_news(news)
        
        if result:
            print(f"\n策略更新成功:")
            print(f"  立场: {result.get('overall_stance', '')}")
            print(f"  置信度: {result.get('confidence', 0):.0%}")
            print(f"  个股推荐: {len(result.get('stock_picks', []))}只")
        else:
            print("\n策略无显著变化，未更新")
        
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
