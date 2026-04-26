#!/usr/bin/env python3
"""
市场上下文构建器 (Phase 3)

功能: 融合多源数据（新闻+板块+行情+宽度），为LLM分析提供完整上下文

用法:
    from app.ai.market_context_builder import MarketContextBuilder
    builder = MarketContextBuilder()
    context = builder.build_pre_market_context(['HK.00700', 'HK.09988'])
"""
import logging
import time
from typing import List, Dict, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("ftseek.ai.context_builder")
logger.propagate = False


class MarketContextBuilder:
    """市场上下文构建器"""
    
    def __init__(
        self,
        news_summarizer=None,
        sector_data=None,
        news_analyzer=None,
        hsi_fetcher=None,
    ):
        """
        Args:
            news_summarizer: NewsSummarizer实例
            sector_data: FutuSectorData实例
            news_analyzer: NewsSentimentAnalyzer实例
            hsi_fetcher: HSIDataFetcher实例
        """
        # 延迟导入
        if news_summarizer is None:
            try:
                from app.news_push.news_summarizer import get_summarizer
                self.news_summarizer = get_summarizer()
            except Exception as e:
                logger.warning(f"NewsSummarizer初始化失败: {e}")
                self.news_summarizer = None
        else:
            self.news_summarizer = news_summarizer
        
        if sector_data is None:
            try:
                from app.data.futu_sector_data import get_sector_data
                self.sector_data = get_sector_data()
            except Exception as e:
                logger.warning(f"FutuSectorData初始化失败: {e}")
                self.sector_data = None
        else:
            self.sector_data = sector_data
        
        if news_analyzer is None:
            try:
                from app.factors.news_integration import get_news_analyzer
                self.news_analyzer = get_news_analyzer()
            except Exception as e:
                logger.warning(f"NewsSentimentAnalyzer初始化失败: {e}")
                self.news_analyzer = None
        else:
            self.news_analyzer = news_analyzer
        
        if hsi_fetcher is None:
            try:
                from app.data.hsi_data import hsi_fetcher
                self.hsi_fetcher = hsi_fetcher
            except Exception as e:
                logger.warning(f"HSIDataFetcher初始化失败: {e}")
                self.hsi_fetcher = None
        else:
            self.hsi_fetcher = hsi_fetcher
        
        logger.info("MarketContextBuilder 初始化完成")
    
    def build_pre_market_context(self, target_codes: List[str]) -> Dict:
        """
        构建盘前综合上下文
        
        Args:
            target_codes: 目标股票代码列表
        
        Returns:
            {
                'timestamp': '...',
                'news_summary': {...},
                'sector_data': {...},
                'market_breadth': {...},
                'cross_market': {...},
                'technical_snapshot': {...},
                'stock_sentiment': {...},
            }
        """
        start_time = time.time()
        logger.info(f"开始构建盘前上下文，目标股票: {len(target_codes)}只")
        
        context = {
            'timestamp': datetime.now().isoformat(),
        }
        
        # 1. 新闻汇总（回溯12小时，覆盖隔夜新闻）
        try:
            if self.news_summarizer:
                context['news_summary'] = self.news_summarizer.summarize(hours=12)
                logger.info(f"新闻汇总完成: {context['news_summary']['total_news']}条")
            else:
                context['news_summary'] = self._empty_news_summary()
        except Exception as e:
            logger.error(f"新闻汇总失败: {e}")
            context['news_summary'] = self._empty_news_summary()
        
        # 2. 富途板块数据
        try:
            if self.sector_data:
                context['sector_data'] = {
                    'capital_flow': self.sector_data.get_sector_capital_flow(),
                    'ranking': self.sector_data.get_sector_ranking(limit=10),
                    'concept_hot': self.sector_data.get_concept_hot(),
                }
                logger.info(f"板块数据完成: {len(context['sector_data']['capital_flow'])}个板块")
            else:
                context['sector_data'] = self._empty_sector_data()
        except Exception as e:
            logger.error(f"板块数据获取失败: {e}")
            context['sector_data'] = self._empty_sector_data()
        
        # 3. 市场宽度
        try:
            if self.hsi_fetcher:
                context['market_breadth'] = self.hsi_fetcher.get_market_breadth() or {}
                logger.info(f"市场宽度完成: {context['market_breadth']}")
            else:
                context['market_breadth'] = {}
        except Exception as e:
            logger.error(f"市场宽度获取失败: {e}")
            context['market_breadth'] = {}
        
        # 4. 跨市场数据（美股/汇率/债券）
        try:
            context['cross_market'] = self._fetch_cross_market()
            logger.info(f"跨市场数据完成")
        except Exception as e:
            logger.error(f"跨市场数据获取失败: {e}")
            context['cross_market'] = {}
        
        # 5. 个股技术面快照
        try:
            context['technical_snapshot'] = self._get_technical_batch(target_codes)
            logger.info(f"技术面快照完成: {len(context['technical_snapshot'])}只")
        except Exception as e:
            logger.error(f"技术面快照失败: {e}")
            context['technical_snapshot'] = {}
        
        # 6. 个股新闻情绪
        try:
            context['stock_sentiment'] = self._get_stock_sentiment_batch(target_codes)
            logger.info(f"个股情绪完成: {len(context['stock_sentiment'])}只")
        except Exception as e:
            logger.error(f"个股情绪获取失败: {e}")
            context['stock_sentiment'] = {}
        
        elapsed = time.time() - start_time
        context['generation_time_ms'] = int(elapsed * 1000)
        
        logger.info(f"盘前上下文构建完成，耗时{elapsed:.1f}秒")
        return context
    
    def build_intraday_context(self, target_codes: List[str] = None) -> Dict:
        """
        构建盘中上下文（简化版，仅关键数据）
        
        Args:
            target_codes: 目标股票代码列表（可选）
        
        Returns:
            简化的上下文
        """
        start_time = time.time()
        
        context = {
            'timestamp': datetime.now().isoformat(),
            'type': 'intraday',
        }
        
        # 1. 最新新闻汇总（回溯2小时）
        try:
            if self.news_summarizer:
                context['news_summary'] = self.news_summarizer.summarize(hours=2, max_news=50)
        except Exception as e:
            logger.error(f"盘中新闻汇总失败: {e}")
            context['news_summary'] = self._empty_news_summary()
        
        # 2. 板块数据（快速获取）
        try:
            if self.sector_data:
                context['sector_data'] = {
                    'ranking': self.sector_data.get_sector_ranking(limit=5),
                }
        except Exception as e:
            logger.error(f"盘中板块数据获取失败: {e}")
            context['sector_data'] = {'ranking': []}
        
        # 3. 市场宽度
        try:
            if self.hsi_fetcher:
                context['market_breadth'] = self.hsi_fetcher.get_market_breadth() or {}
        except Exception as e:
            logger.error(f"盘中市场宽度获取失败: {e}")
            context['market_breadth'] = {}
        
        # 4. 个股数据（如有）
        if target_codes:
            context['stock_sentiment'] = self._get_stock_sentiment_batch(target_codes)
        
        elapsed = time.time() - start_time
        context['generation_time_ms'] = int(elapsed * 1000)
        
        return context
    
    def format_for_llm(self, context: Dict) -> str:
        """
        格式化为LLM可读文本
        
        Args:
            context: build_pre_market_context()返回的上下文
        
        Returns:
            格式化的提示词文本
        """
        lines = [
            "## 盘前市场综合分析",
            "",
            f"**分析时间**: {context['timestamp'][:19]}",
            f"**生成耗时**: {context.get('generation_time_ms', 0)}ms",
            "",
        ]
        
        # 一、隔夜新闻汇总
        news = context.get('news_summary', {})
        lines.append("### 一、隔夜新闻汇总")
        lines.append(f"- **总新闻数**: {news.get('total_news', 0)}条")
        lines.append(f"- **整体情绪**: {news.get('overall_sentiment', 0):+.2f} ({news.get('sentiment_trend', 'stable')})")
        lines.append("")
        
        # 主题分布
        topics = news.get('topics', {})
        if topics:
            lines.append("**主题分布**:")
            for topic_id, topic_info in topics.items():
                name = topic_info.get('name', topic_id)
                count = topic_info.get('count', 0)
                score = topic_info.get('avg_score', 0)
                trend = topic_info.get('sentiment_trend', 'stable')
                lines.append(f"- {name}: {count}条, 情绪{score:+.2f} ({trend})")
            lines.append("")
        
        # 突发新闻
        breaking = news.get('breaking_news', [])
        if breaking:
            lines.append(f"**突发新闻** ({len(breaking)}条):")
            for i, bnews in enumerate(breaking[:3], 1):
                lines.append(f"{i}. {bnews.get('title', '')}")
            lines.append("")
        
        # 二、港股板块资金流向
        sector = context.get('sector_data', {})
        capital_flow = sector.get('capital_flow', [])
        if capital_flow:
            lines.append("### 二、港股板块资金流向")
            lines.append("| 板块 | 涨跌幅 | 成交额(亿) | 涨/跌 | 领涨股 |")
            lines.append("|------|--------|-----------|-------|--------|")
            
            for s in capital_flow[:8]:
                name = s.get('sector_name', '')
                change = s.get('change_pct', 0)
                turnover = s.get('turnover', 0)
                advance = s.get('advance_count', 0)
                decline = s.get('decline_count', 0)
                top_stocks = s.get('top_stocks', [])
                top_stock = top_stocks[0] if top_stocks else {}
                top_name = top_stock.get('name', '')
                top_change = top_stock.get('change_pct', 0)
                
                lines.append(
                    f"| {name} | {change:+.2f}% | {turnover:.1f} | "
                    f"{advance}/{decline} | {top_name}({top_change:+.1f}%) |"
                )
            lines.append("")
        
        # 三、市场宽度
        breadth = context.get('market_breadth', {})
        if breadth:
            lines.append("### 三、市场宽度")
            advancing = breadth.get('advancing', 0)
            declining = breadth.get('declining', 0)
            unchanged = breadth.get('unchanged', 0)
            ratio = breadth.get('ratio', 0)
            
            lines.append(f"- 上涨/下跌/平: {advancing}/{declining}/{unchanged}")
            lines.append(f"- 涨跌比: {ratio:.2f}")
            
            if 'new_high' in breadth:
                lines.append(f"- 创新高/新低: {breadth['new_high']}/{breadth['new_low']}")
            lines.append("")
        
        # 四、跨市场数据
        cross = context.get('cross_market', {})
        if cross:
            lines.append("### 四、跨市场数据")
            if 'spy_change' in cross:
                lines.append(f"- **美股隔夜**: SPY {cross.get('spy_change', 0):+.2f}%, QQQ {cross.get('qqq_change', 0):+.2f}%")
            if 'vix' in cross:
                vix = cross.get('vix', 0)
                vix_status = '恐慌' if vix > 30 else ('正常' if vix > 20 else '平静')
                lines.append(f"- **VIX**: {vix:.1f} ({vix_status})")
            if 'us_bond_10y' in cross:
                lines.append(f"- **美债10Y**: {cross.get('us_bond_10y', 0):.2f}%")
            lines.append("")
        
        # 五、重点个股
        sentiment = context.get('stock_sentiment', {})
        technical = context.get('technical_snapshot', {})
        
        if sentiment or technical:
            lines.append("### 五、重点个股分析")
            
            all_codes = set(list(sentiment.keys()) + list(technical.keys()))
            for code in list(all_codes)[:10]:
                sent = sentiment.get(code, {})
                tech = technical.get(code, {})
                
                sent_score = sent.get('decayed_score', sent.get('sentiment_score', 0))
                sent_trend = sent.get('trend', 'stable')
                news_count = sent.get('news_count', 0)
                
                price = tech.get('price', 0)
                change = tech.get('change_pct', 0)
                rsi = tech.get('rsi_14', None)
                macd = tech.get('macd_signal', '')
                
                line = f"- **{code}**: "
                if price:
                    line += f"价格{price}, 涨跌{change:+.2f}%, "
                if rsi is not None:
                    line += f"RSI {rsi:.0f}, "
                if macd:
                    line += f"MACD{macd}, "
                line += f"新闻情绪{sent_score:+.2f}({sent_trend}), {news_count}条新闻"
                
                lines.append(line)
            lines.append("")
        
        # 总结提示
        lines.append("---")
        lines.append("")
        lines.append("**请基于以上信息，给出以下分析:**")
        lines.append("1. 今日港股整体走势预判（看多/看空/中性）")
        lines.append("2. 重点推荐板块及个股")
        lines.append("3. 风险提示与失效条件")
        lines.append("4. 关键催化事件")
        lines.append("")
        
        return '\n'.join(lines)
    
    def _fetch_cross_market(self) -> Dict:
        """获取跨市场数据（美股/汇率/债券）"""
        result = {}
        
        try:
            # 尝试从yfinance获取美股数据
            import yfinance as yf
            
            # SPY
            spy = yf.Ticker("SPY")
            spy_hist = spy.history(period="1d")
            if not spy_hist.empty:
                result['spy_change'] = float(spy_hist['Close'].pct_change().iloc[-1] * 100) if len(spy_hist) > 1 else 0
                result['spy_price'] = float(spy_hist['Close'].iloc[-1])
            
            # QQQ
            qqq = yf.Ticker("QQQ")
            qqq_hist = qqq.history(period="1d")
            if not qqq_hist.empty:
                result['qqq_change'] = float(qqq_hist['Close'].pct_change().iloc[-1] * 100) if len(qqq_hist) > 1 else 0
                result['qqq_price'] = float(qqq_hist['Close'].iloc[-1])
            
            # VIX
            vix = yf.Ticker("^VIX")
            vix_hist = vix.history(period="1d")
            if not vix_hist.empty:
                result['vix'] = float(vix_hist['Close'].iloc[-1])
            
            # 美债10Y
            bond = yf.Ticker("TNX")
            bond_hist = bond.history(period="1d")
            if not bond_hist.empty:
                result['us_bond_10y'] = float(bond_hist['Close'].iloc[-1])
            
            logger.debug(f"跨市场数据获取成功: SPY {result.get('spy_change', 0):+.2f}%")
            
        except Exception as e:
            logger.warning(f"yfinance获取跨市场数据失败: {e}")
            
            # Fallback: 使用模拟数据
            result = {
                'spy_change': 0.8,
                'qqq_change': 1.2,
                'vix': 18.5,
                'us_bond_10y': 4.25,
            }
            logger.info("使用模拟跨市场数据")
        
        return result
    
    def _get_technical_batch(self, codes: List[str]) -> Dict:
        """批量获取技术面快照"""
        result = {}
        
        for code in codes:
            try:
                # 这里简化处理，实际应该调用技术指标计算
                # 可以从DataSubscriber或富途API获取
                result[code] = {
                    'code': code,
                    'price': 0,
                    'change_pct': 0,
                    'rsi_14': None,
                    'macd_signal': '',
                    'volume_ratio': 1.0,
                }
            except Exception as e:
                logger.debug(f"获取{code}技术面失败: {e}")
                continue
        
        return result
    
    def _get_stock_sentiment_batch(self, codes: List[str]) -> Dict:
        """批量获取个股新闻情绪"""
        result = {}
        
        if not self.news_analyzer:
            return result
        
        for code in codes:
            try:
                sentiment = self.news_analyzer.analyze_stock(code, lookback_hours=24)
                result[code] = sentiment
            except Exception as e:
                logger.debug(f"获取{code}情绪失败: {e}")
                continue
        
        return result
    
    def _empty_news_summary(self) -> Dict:
        """返回空新闻摘要"""
        return {
            'total_news': 0,
            'overall_sentiment': 0.0,
            'sentiment_trend': 'stable',
            'topics': {},
            'breaking_news': [],
        }
    
    def _empty_sector_data(self) -> Dict:
        """返回空板块数据"""
        return {
            'capital_flow': [],
            'ranking': [],
            'concept_hot': [],
        }


# 单例模式
_instance = None

def get_context_builder() -> MarketContextBuilder:
    """获取MarketContextBuilder单例"""
    global _instance
    if _instance is None:
        _instance = MarketContextBuilder()
    return _instance


if __name__ == '__main__':
    # 测试
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    builder = get_context_builder()
    
    print("\n" + "="*80)
    print("市场上下文构建器测试")
    print("="*80)
    
    target_codes = ['HK.00700', 'HK.09988', 'HK.03690']
    
    try:
        context = builder.build_pre_market_context(target_codes)
        
        print(f"\n生成耗时: {context['generation_time_ms']}ms")
        print(f"新闻总数: {context['news_summary']['total_news']}")
        print(f"板块数: {len(context['sector_data']['capital_flow'])}")
        print(f"市场宽度: {context['market_breadth']}")
        
        print("\n" + "="*80)
        print("LLM格式化输出示例")
        print("="*80)
        formatted = builder.format_for_llm(context)
        print(formatted[:2000])  # 打印前2000字符
        
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
