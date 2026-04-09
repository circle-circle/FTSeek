"""
FTSeek 新闻增强模块
集成新闻猎手 (News Hunter) 与 FTSeek 交易系统
"""

import os
import sys
import logging
from typing import Dict, Optional

# 导入新闻猎手
sys.path.insert(0, '/root/.openclaw/skills/news-hunter/scripts')

try:
    from ftseek_integration import FTSeekNewsEnhancer
    NEWS_ENHANCER_AVAILABLE = True
except ImportError as e:
    NEWS_ENHANCER_AVAILABLE = False
    print(f"[新闻增强] 模块加载失败: {e}")

logger = logging.getLogger("ftseek.trader.news_enhancer")


class NewsEnhancerWrapper:
    """新闻增强包装器，用于FTSeek集成"""
    
    def __init__(self):
        if NEWS_ENHANCER_AVAILABLE:
            self.enhancer = FTSeekNewsEnhancer()
            self.enabled = True
            logger.info("✅ 新闻增强模块已加载")
        else:
            self.enhancer = None
            self.enabled = False
            logger.warning("⚠️ 新闻增强模块未加载")
    
    def enhance_signal(self, signal_dict: Dict) -> Dict:
        """
        增强交易信号
        
        Args:
            signal_dict: 信号字典 {
                'stock_code': 'HK.00700',
                'action': 'buy',  # buy/sell
                'confidence': 0.65,
                ...
            }
        
        Returns:
            增强后的信号字典
        """
        if not self.enabled:
            return signal_dict
        
        try:
            enhanced = self.enhancer.enhance_signal(signal_dict)
            
            # 记录增强效果
            if enhanced.get('news_enhanced'):
                original_conf = signal_dict.get('confidence', 0.5)
                new_conf = enhanced.get('confidence', 0.5)
                
                logger.info(f"[新闻增强] {signal_dict['stock_code']} "
                          f"置信度: {original_conf:.2f} → {new_conf:.2f} "
                          f"(新闻: {enhanced.get('news_count', 0)}条)")
            
            return enhanced
            
        except Exception as e:
            logger.error(f"[新闻增强] 增强失败: {e}")
            return signal_dict
    
    def check_block(self, signal_dict: Dict) -> Optional[str]:
        """
        检查是否应该阻止交易
        
        Args:
            signal_dict: 信号字典
        
        Returns:
            None: 允许交易
            str: 阻止原因
        """
        if not self.enabled:
            return None
        
        try:
            block_reason = self.enhancer.should_block_trade(signal_dict)
            
            if block_reason:
                logger.warning(f"[新闻阻止] {signal_dict['stock_code']} "
                            f"阻止原因: {block_reason}")
            
            return block_reason
            
        except Exception as e:
            logger.error(f"[新闻阻止] 检查失败: {e}")
            return None
    
    def get_news_signals(self) -> list:
        """
        获取新闻驱动的交易信号
        
        Returns:
            信号列表
        """
        if not self.enabled:
            return []
        
        try:
            signals = self.enhancer.generate_news_signals()
            
            if signals:
                logger.info(f"[新闻信号] 生成 {len(signals)} 个新闻驱动信号")
            
            return signals
            
        except Exception as e:
            logger.error(f"[新闻信号] 获取失败: {e}")
            return []
    
    def get_market_sentiment(self) -> Dict:
        """
        获取市场整体情绪
        
        Returns:
            {
                'sentiment': 'positive/negative/neutral',
                'score': 0.5,
                'confidence': 0.8
            }
        """
        if not self.enabled:
            return {'sentiment': 'neutral', 'score': 0, 'confidence': 0}
        
        try:
            sentiment = self.enhancer.get_market_sentiment()
            return sentiment
            
        except Exception as e:
            logger.error(f"[市场情绪] 获取失败: {e}")
            return {'sentiment': 'neutral', 'score': 0, 'confidence': 0}


# 全局实例
news_enhancer_wrapper = NewsEnhancerWrapper()


def get_news_enhancer() -> NewsEnhancerWrapper:
    """获取新闻增强器实例"""
    return news_enhancer_wrapper


# 快捷函数
def enhance_signal(signal: Dict) -> Dict:
    """快捷函数：增强信号"""
    return news_enhancer_wrapper.enhance_signal(signal)


def check_news_block(signal: Dict) -> Optional[str]:
    """快捷函数：检查阻止"""
    return news_enhancer_wrapper.check_block(signal)


def get_news_signals() -> list:
    """快捷函数：获取新闻信号"""
    return news_enhancer_wrapper.get_news_signals()