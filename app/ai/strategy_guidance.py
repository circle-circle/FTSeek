#!/usr/bin/env python3
"""
策略指导引擎 (Phase 4)

功能: 将LLM分析结果转化为可执行的策略建议

用法:
    from app.ai.strategy_guidance import StrategyGuidanceEngine
    engine = StrategyGuidanceEngine()
    guidance = engine.generate_from_llm(assessment, context)
"""
import logging
import json
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger("ftseek.ai.strategy_guidance")
logger.propagate = False


@dataclass
class StrategyGuidance:
    """策略指导建议"""
    timestamp: str  # 生成时间
    overall_stance: str  # 看多/看空/中性
    confidence: float  # 置信度 0-1
    sector_allocation: List[Dict]  # 板块配置建议 [{sector, weight, reason}]
    stock_picks: List[Dict]  # 个股推荐 [{code, action, target_price, stop_loss, reason}]
    risk_warnings: List[str]  # 风险提示
    key_catalysts: List[str]  # 关键催化剂
    invalidation_conditions: List[str]  # 策略失效条件
    update_frequency: str  # 更新频率
    valid_until: str  # 有效期
    reasoning: str = ""  # 推理过程
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class StrategyGuidanceEngine:
    """策略指导引擎"""
    
    def __init__(self):
        logger.info("StrategyGuidanceEngine 初始化完成")
    
    def generate_from_llm(self, assessment, context: Dict) -> StrategyGuidance:
        """
        基于LLM分析生成策略指导
        
        Args:
            assessment: MarketAssessment实例（来自LLMMarketAnalyst）
            context: MarketContextBuilder生成的上下文
        
        Returns:
            StrategyGuidance实例
        """
        logger.info("开始生成策略指导")
        
        # 1. 解析LLM输出
        overall_stance = self._determine_stance(assessment)
        confidence = assessment.confidence_value if hasattr(assessment, 'confidence_value') else 0.5
        
        # 2. 生成板块配置建议
        sector_allocation = self._generate_sector_allocation(context, overall_stance)
        
        # 3. 生成个股推荐
        stock_picks = self._generate_stock_picks(context, assessment)
        
        # 4. 提取风险提示
        risk_warnings = self._extract_risk_warnings(context, assessment)
        
        # 5. 识别关键催化剂
        key_catalysts = self._identify_catalysts(context)
        
        # 6. 设置失效条件
        invalidation_conditions = self._set_invalidation_conditions(context, overall_stance)
        
        # 7. 生成推理过程
        reasoning = self._build_reasoning(assessment, context)
        
        # 8. 设置有效期
        now = datetime.now()
        valid_until = now + timedelta(hours=8)  # 当日有效
        
        guidance = StrategyGuidance(
            timestamp=now.isoformat(),
            overall_stance=overall_stance,
            confidence=confidence,
            sector_allocation=sector_allocation,
            stock_picks=stock_picks,
            risk_warnings=risk_warnings,
            key_catalysts=key_catalysts,
            invalidation_conditions=invalidation_conditions,
            update_frequency="盘中每30分钟或重大新闻触发",
            valid_until=valid_until.isoformat(),
            reasoning=reasoning,
        )
        
        logger.info(
            f"策略指导生成完成: {overall_stance}, "
            f"置信度{confidence:.2f}, {len(stock_picks)}只个股推荐"
        )
        
        return guidance
    
    def _determine_stance(self, assessment) -> str:
        """确定整体立场"""
        if hasattr(assessment, 'direction'):
            direction_map = {
                'STRONG_BUY': '强烈看多',
                'BUY': '看多',
                'NEUTRAL': '中性',
                'SELL': '看空',
                'STRONG_SELL': '强烈看空',
            }
            return direction_map.get(assessment.direction, '中性')
        return '中性'
    
    def _generate_sector_allocation(self, context: Dict, stance: str) -> List[Dict]:
        """
        生成板块配置建议
        
        基于:
        - 板块资金流向
        - 板块涨跌排名
        - 新闻主题情绪
        """
        allocation = []
        
        sector_data = context.get('sector_data', {})
        capital_flow = sector_data.get('capital_flow', [])
        ranking = sector_data.get('ranking', [])
        
        news_summary = context.get('news_summary', {})
        topics = news_summary.get('topics', {})
        
        # 根据板块表现分配权重
        if capital_flow:
            # 资金流入且上涨的板块优先
            for sector in capital_flow[:5]:
                change_pct = sector.get('change_pct', 0)
                turnover = sector.get('turnover', 0)
                advance_count = sector.get('advance_count', 0)
                decline_count = sector.get('decline_count', 0)
                
                # 计算板块强度分数
                strength = 0
                if change_pct > 0:
                    strength += change_pct * 10
                if turnover > 50:  # 成交额>50亿
                    strength += 1
                if advance_count > decline_count:
                    strength += (advance_count - decline_count) / max(advance_count + decline_count, 1) * 2
                
                # 根据整体立场调整权重
                if '看多' in stance and strength > 0:
                    weight = min(0.30, 0.10 + strength * 0.05)
                elif '看空' in stance:
                    weight = max(0.05, 0.10 - strength * 0.03)
                else:
                    weight = 0.10
                
                reason = f"资金流入{turnover:.1f}亿, 涨{advance_count}/跌{decline_count}"
                
                allocation.append({
                    'sector': sector.get('sector_name', ''),
                    'weight': round(weight, 2),
                    'strength': round(strength, 2),
                    'reason': reason,
                })
        
        # 按权重排序
        allocation.sort(key=lambda x: x['weight'], reverse=True)
        
        # 归一化权重（总和=1）
        total_weight = sum(a['weight'] for a in allocation)
        if total_weight > 0:
            for a in allocation:
                a['weight'] = round(a['weight'] / total_weight, 2)
        
        return allocation[:5]  # 最多5个板块
    
    def _generate_stock_picks(self, context: Dict, assessment) -> List[Dict]:
        """
        生成个股推荐
        
        基于:
        - 个股新闻情绪
        - 技术面快照
        - LLM评估
        """
        picks = []
        
        stock_sentiment = context.get('stock_sentiment', {})
        technical = context.get('technical_snapshot', {})
        
        # 合并所有股票
        all_codes = set(list(stock_sentiment.keys()) + list(technical.keys()))
        
        for code in all_codes:
            sent = stock_sentiment.get(code, {})
            tech = technical.get(code, {})
            
            # 计算综合评分
            sentiment_score = sent.get('decayed_score', sent.get('sentiment_score', 0))
            news_count = sent.get('news_count', 0)
            trend = sent.get('trend', 'stable')
            
            price = tech.get('price', 0)
            change_pct = tech.get('change_pct', 0)
            rsi = tech.get('rsi_14', None)
            
            # 评分逻辑
            score = 0
            
            # 情绪得分权重高
            score += sentiment_score * 40
            
            # 新闻数量（关注度）
            if news_count >= 5:
                score += 10
            elif news_count >= 3:
                score += 5
            
            # 趋势
            if trend == 'rising':
                score += 15
            elif trend == 'falling':
                score -= 15
            
            # 技术面
            if rsi is not None:
                if 30 <= rsi <= 70:  # 正常区间
                    score += 5
                elif rsi < 30:  # 超卖
                    score += 10
                elif rsi > 70:  # 超买
                    score -= 10
            
            # 确定操作建议
            if score >= 60:
                action = 'BUY'
                action_name = '买入'
            elif score >= 30:
                action = 'HOLD'
                action_name = '持有'
            elif score >= 0:
                action = 'WATCH'
                action_name = '关注'
            else:
                action = 'AVOID'
                action_name = '回避'
            
            # 仅推荐BUY和WATCH
            if action not in ['BUY', 'WATCH']:
                continue
            
            # 计算目标价和止损价
            if price > 0:
                if action == 'BUY':
                    target_price = round(price * 1.05, 2)  # 5%目标
                    stop_loss = round(price * 0.97, 2)  # 3%止损
                else:
                    target_price = round(price * 1.03, 2)
                    stop_loss = round(price * 0.95, 2)
            else:
                target_price = 0
                stop_loss = 0
            
            reason = f"情绪{sentiment_score:+.2f}({trend}), {news_count}条新闻"
            if rsi is not None:
                reason += f", RSI {rsi:.0f}"
            
            picks.append({
                'code': code,
                'action': action,
                'action_name': action_name,
                'price': price,
                'target_price': target_price,
                'stop_loss': stop_loss,
                'score': round(score, 2),
                'reason': reason,
            })
        
        # 按评分排序
        picks.sort(key=lambda x: x['score'], reverse=True)
        
        return picks[:5]  # 最多5只
    
    def _extract_risk_warnings(self, context: Dict, assessment) -> List[str]:
        """提取风险提示"""
        warnings = []
        
        # 1. VIX风险
        cross_market = context.get('cross_market', {})
        vix = cross_market.get('vix', 0)
        if vix > 30:
            warnings.append(f"⚠️ VIX指数{vix:.1f}处于高位，市场恐慌情绪浓厚")
        elif vix > 25:
            warnings.append(f"⚠️ VIX指数{vix:.1f}偏高，注意控制仓位")
        
        # 2. 美股风险
        spy_change = cross_market.get('spy_change', 0)
        if spy_change < -2:
            warnings.append(f"⚠️ 美股隔夜大跌SPY {spy_change:+.2f}%，可能拖累港股")
        
        # 3. 市场宽度风险
        breadth = context.get('market_breadth', {})
        ratio = breadth.get('ratio', 0.5)
        if ratio < 0.3:
            warnings.append(f"⚠️ 市场宽度差，涨跌比{ratio:.2f}，仅{ratio*100:.0f}%股票上涨")
        
        # 4. 新闻情绪风险
        news_summary = context.get('news_summary', {})
        sentiment = news_summary.get('overall_sentiment', 0)
        trend = news_summary.get('sentiment_trend', 'stable')
        if sentiment < -0.5 and trend == 'falling':
            warnings.append(f"⚠️ 新闻情绪恶化（{sentiment:+.2f}且下降），警惕黑天鹅")
        
        # 5. LLM风险提示
        if hasattr(assessment, 'risk_flags') and assessment.risk_flags:
            warnings.extend(assessment.risk_flags[:3])
        
        # 6. 通用风险
        warnings.append("⚠️ 以上建议仅供参考，投资有风险，入市需谨慎")
        
        return warnings
    
    def _identify_catalysts(self, context: Dict) -> List[str]:
        """识别关键催化剂"""
        catalysts = []
        
        # 1. 突发新闻
        news_summary = context.get('news_summary', {})
        breaking = news_summary.get('breaking_news', [])
        if breaking:
            for bnews in breaking[:2]:
                catalysts.append(f"突发: {bnews.get('title', '')[:50]}")
        
        # 2. 政策主题
        topics = news_summary.get('topics', {})
        if 'policy' in topics and topics['policy']['count'] > 5:
            catalysts.append(f"政策关注度高: {topics['policy']['count']}条相关新闻")
        
        # 3. 板块异动
        sector_data = context.get('sector_data', {})
        ranking = sector_data.get('ranking', [])
        if ranking:
            top_sector = ranking[0]
            if top_sector.get('change_pct', 0) > 2:
                catalysts.append(f"板块强势: {top_sector['sector_name']} +{top_sector['change_pct']:.2f}%")
        
        return catalysts
    
    def _set_invalidation_conditions(self, context: Dict, stance: str) -> List[str]:
        """设置策略失效条件"""
        conditions = []
        
        # 根据立场设置不同条件
        if '看多' in stance:
            conditions.append("恒指跌幅>2%，止损观望")
            conditions.append("VIX指数突破35，市场恐慌")
            conditions.append("突发重大利空新闻（制裁、战争等）")
        elif '看空' in stance:
            conditions.append("恒指涨幅>2%，趋势反转")
            conditions.append("VIX指数跌破15，市场过度乐观")
            conditions.append("政策面出现重大利好")
        else:
            conditions.append("市场出现明确方向性突破（±2%）")
            conditions.append("重大新闻事件改变市场格局")
        
        # 通用条件
        conditions.append("个股触发止损价，严格执行纪律")
        
        return conditions
    
    def _build_reasoning(self, assessment, context: Dict) -> str:
        """构建推理过程"""
        lines = []
        
        # LLM推理
        if hasattr(assessment, 'reasoning') and assessment.reasoning:
            lines.append(assessment.reasoning[:500])
        
        # 关键因素
        if hasattr(assessment, 'key_factors') and assessment.key_factors:
            lines.append("关键因素:")
            for factor in assessment.key_factors[:5]:
                lines.append(f"- {factor}")
        
        return '\n'.join(lines)
    
    def update_with_breaking_news(self, current_guidance: StrategyGuidance, news, context: Dict) -> Optional[StrategyGuidance]:
        """
        突发新闻更新策略指导
        
        Args:
            current_guidance: 当前策略指导
            news: 突发新闻
            context: 最新上下文
        
        Returns:
            更新后的策略指导，如无重大变化返回None
        """
        # 判断新闻重要性
        priority = getattr(news, 'priority', 5)
        is_breaking = getattr(news, 'is_breaking', False)
        
        if priority > 2 and not is_breaking:
            return None  # 非重大新闻，不更新
        
        logger.info(f"突发新闻触发策略更新: {getattr(news, 'title', '')[:50]}")
        
        # 这里简化处理，实际应该调用LLM快速分析
        # 暂时返回原指导，标记需要更新
        updated = StrategyGuidance(
            timestamp=datetime.now().isoformat(),
            overall_stance=current_guidance.overall_stance,
            confidence=current_guidance.confidence,
            sector_allocation=current_guidance.sector_allocation,
            stock_picks=current_guidance.stock_picks,
            risk_warnings=current_guidance.risk_warnings + [f"【突发】{getattr(news, 'title', '')}"],
            key_catalysts=current_guidance.key_catalysts,
            invalidation_conditions=current_guidance.invalidation_conditions,
            update_frequency="突发新闻触发",
            valid_until=(datetime.now() + timedelta(hours=4)).isoformat(),
            reasoning=f"突发新闻: {getattr(news, 'title', '')}",
        )
        
        return updated
    
    def format_for_dingtalk(self, guidance: StrategyGuidance) -> str:
        """
        格式化为钉钉Markdown消息
        
        Args:
            guidance: StrategyGuidance实例
        
        Returns:
            Markdown格式字符串
        """
        lines = [
            "### 🎯 交易策略指导",
            "",
            f"**生成时间**: {guidance.timestamp[:19]}",
            f"**有效期至**: {guidance.valid_until[:19]}",
            f"**整体立场**: {guidance.overall_stance}",
            f"**置信度**: {guidance.confidence:.0%}",
            "",
        ]
        
        # 板块配置
        if guidance.sector_allocation:
            lines.append("#### 📊 板块配置建议")
            lines.append("| 板块 | 权重 | 理由 |")
            lines.append("|------|------|------|")
            for sector in guidance.sector_allocation:
                lines.append(
                    f"| {sector['sector']} | {sector['weight']:.0%} | {sector['reason']} |"
                )
            lines.append("")
        
        # 个股推荐
        if guidance.stock_picks:
            lines.append("#### 🎯 个股推荐")
            lines.append("| 代码 | 操作 | 目标价 | 止损价 | 理由 |")
            lines.append("|------|------|--------|--------|------|")
            for stock in guidance.stock_picks:
                action_emoji = {'BUY': '🔴买入', 'WATCH': '👀关注'}.get(stock['action'], stock['action_name'])
                lines.append(
                    f"| {stock['code']} | {action_emoji} | {stock['target_price']} | "
                    f"{stock['stop_loss']} | {stock['reason']} |"
                )
            lines.append("")
        
        # 关键催化剂
        if guidance.key_catalysts:
            lines.append("#### ⚡ 关键催化剂")
            for catalyst in guidance.key_catalysts:
                lines.append(f"- {catalyst}")
            lines.append("")
        
        # 风险提示
        if guidance.risk_warnings:
            lines.append("#### ⚠️ 风险提示")
            for warning in guidance.risk_warnings:
                lines.append(f"- {warning}")
            lines.append("")
        
        # 失效条件
        if guidance.invalidation_conditions:
            lines.append("#### 🚫 策略失效条件")
            for condition in guidance.invalidation_conditions:
                lines.append(f"- {condition}")
            lines.append("")
        
        return '\n'.join(lines)


# 单例模式
_instance = None

def get_guidance_engine() -> StrategyGuidanceEngine:
    """获取StrategyGuidanceEngine单例"""
    global _instance
    if _instance is None:
        _instance = StrategyGuidanceEngine()
    return _instance


if __name__ == '__main__':
    # 测试
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    engine = get_guidance_engine()
    
    print("\n" + "="*80)
    print("策略指导引擎测试")
    print("="*80)
    
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
    
    # 模拟上下文
    mock_context = {
        'news_summary': {
            'total_news': 45,
            'overall_sentiment': 0.65,
            'sentiment_trend': 'rising',
            'topics': {
                'policy': {'count': 8, 'avg_score': 0.7, 'sentiment_trend': 'rising'},
            },
            'breaking_news': [
                {'title': '央行宣布降准0.5%', 'source': '富途快讯', 'priority': 1}
            ],
        },
        'sector_data': {
            'capital_flow': [
                {'sector_name': '科技', 'change_pct': 2.5, 'turnover': 120.5, 'advance_count': 40, 'decline_count': 10},
                {'sector_name': '金融', 'change_pct': 1.2, 'turnover': 85.3, 'advance_count': 25, 'decline_count': 15},
            ],
            'ranking': [
                {'sector_name': '科技', 'change_pct': 2.5},
            ],
        },
        'market_breadth': {
            'advancing': 280,
            'declining': 150,
            'unchanged': 20,
            'ratio': 0.62,
        },
        'cross_market': {
            'spy_change': 0.8,
            'qqq_change': 1.2,
            'vix': 18.5,
        },
        'stock_sentiment': {
            'HK.00700': {'decayed_score': 0.72, 'news_count': 8, 'trend': 'rising'},
            'HK.09988': {'decayed_score': 0.45, 'news_count': 5, 'trend': 'stable'},
        },
        'technical_snapshot': {
            'HK.00700': {'price': 300, 'change_pct': 1.5, 'rsi_14': 65},
            'HK.09988': {'price': 180, 'change_pct': 0.8, 'rsi_14': 55},
        },
    }
    
    try:
        assessment = MockAssessment()
        guidance = engine.generate_from_llm(assessment, mock_context)
        
        print("\n策略指导生成成功:")
        print(f"整体立场: {guidance.overall_stance}")
        print(f"置信度: {guidance.confidence:.0%}")
        print(f"板块配置: {len(guidance.sector_allocation)}个")
        print(f"个股推荐: {len(guidance.stock_picks)}只")
        print(f"风险提示: {len(guidance.risk_warnings)}条")
        
        print("\n" + "="*80)
        print("钉钉格式输出:")
        print("="*80)
        print(engine.format_for_dingtalk(guidance))
        
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
