"""
新闻策略信号桥接器 (News Signal Bridge)

功能:
1. 将 LLMNewsStrategyEngine 输出的 NewsStrategySignal 转换为 Signal 对象
2. 注入 SignalStore 供共识引擎和交易系统消费
3. 触发钉钉策略信号推送

设计原则:
- SignalSource.EVENT: 标记为事件驱动信号
- 优先级 CRITICAL (4): 新闻驱动信号高优先级
- 带 LLM 元数据: direction, confidence, position_tier, stop_loss, take_profit

集成位置:
- rsshub_multi_source_cron.py: push_event_impacts() 中调用
- 也可被 Harness 巡检手动触发

作者: FTSeek Team
日期: 2026-04-15
"""

import logging
import time
from datetime import datetime
from typing import List, Optional, Dict

logger = logging.getLogger("ftseek.factors.news_signal_bridge")


# ============================================================
# 方向/置信度映射
# ============================================================

DIRECTION_TO_ACTION = {
    "STRONG_BUY": "BUY",
    "BUY": "BUY",
    "SELL": "SELL",
    "STRONG_SELL": "SELL",
    "NEUTRAL": None,       # 中性 → 不生成信号
    "WATCH_ONLY": None,    # 仅关注 → 不生成信号
}

CONFIDENCE_TO_PRIORITY = {
    "HIGH": 4,      # CRITICAL
    "MEDIUM": 3,    # HIGH
    "LOW": 2,       # MEDIUM
}

URGENCY_TO_EXPIRE = {
    "IMMEDIATE": 600,       # 10分钟 (盘中立即执行)
    "NEXT_OPEN": 7200,      # 2小时 (等到开盘)
    "WAIT_CONFIRM": 1800,   # 30分钟 (等确认)
    "MONITOR_ONLY": 0,      # 不设置信号 (仅推送)
}


# ============================================================
# 桥接器
# ============================================================

class NewsSignalBridge:
    """
    新闻策略信号桥接器
    
    核心职责:
    1. 将 NewsStrategySignal → List[Signal]
    2. 注入 SignalStore
    3. 触发钉钉推送 (可选)
    """
    
    def __init__(self, signal_store=None, dingtalk_notifier=None):
        self._signal_store = signal_store
        self._dingtalk_notifier = dingtalk_notifier
        self._stats = {
            "total_bridged": 0,
            "signals_injected": 0,
            "signals_rejected": 0,
            "alerts_sent": 0,
            "signals_recorded": 0,
        }
        # 策略复盘引擎 (自动记录信号, 供后续评估)
        self._review_engine = None
        self._init_review_engine()
    
    def _init_review_engine(self):
        """初始化策略复盘引擎"""
        try:
            from app.factors.strategy_review import get_review_engine
            self._review_engine = get_review_engine()
            logger.info("策略复盘引擎已关联")
        except Exception as e:
            logger.debug(f"策略复盘引擎初始化失败 (非阻塞): {e}")
    
    def bridge_signal(
        self,
        news_strategy_signal,
        auto_inject: bool = True,
        auto_alert: bool = True,
        check_position_conflict: bool = True,  # 2026-04-15 新增: 仓位冲突检查
    ) -> List:
        """
        将新闻策略信号转换为 Signal 对象并注入
        
        Args:
            news_strategy_signal: NewsStrategySignal 对象
            auto_inject: 是否自动注入 SignalStore
            auto_alert: 是否自动发送钉钉推送
            check_position_conflict: 是否检查仓位冲突 (2026-04-15 新增)
            
        Returns:
            (injected_signals, rejected_signals) 列表
        """
        self._stats["total_bridged"] += 1
        
        if not news_strategy_signal or not news_strategy_signal.stock_strategies:
            logger.debug("无策略信号，跳过桥接")
            return [], []
        
        injected = []
        rejected = []
        
        # 2026-04-15: 仓位冲突检查 (仅 BUY 信号需要)
        conflict_results = []
        if check_position_conflict:
            conflict_results = self._check_position_conflicts(
                news_strategy_signal
            )
        
        for stock_strategy in news_strategy_signal.stock_strategies:
            # 转换 direction → action
            action = DIRECTION_TO_ACTION.get(stock_strategy.direction)
            
            if action is None:
                logger.debug(
                    "跳过中性/观望信号: %s %s",
                    stock_strategy.code, stock_strategy.direction
                )
                rejected.append(stock_strategy)
                continue
            
            # 2026-04-15: 检查是否有冲突决策
            if check_position_conflict and conflict_results:
                cr = conflict_results.get(stock_strategy.code)
                if cr and cr.decision.value in ("reject", "defer"):
                    logger.info(
                        "🚫 仓位冲突决策: %s %s → 原因: %s",
                        stock_strategy.code, cr.decision.value, cr.reason
                    )
                    rejected.append(stock_strategy)
                    self._stats["signals_rejected"] += 1
                    
                    # 发送冲突告警
                    if auto_alert and self._dingtalk_notifier:
                        self._send_conflict_alert(cr, stock_strategy)
                    continue
            
            # 构建 Signal 对象
            signal = self._build_signal(
                stock_strategy=stock_strategy,
                news_signal=news_strategy_signal,
                action=action,
            )
            
            # 注入 SignalStore
            if auto_inject and self._signal_store:
                success = self._signal_store.add(signal)
                if success:
                    injected.append(signal)
                    self._stats["signals_injected"] += 1
                    
                    # 记录到策略复盘引擎 (自动跟踪)
                    if self._review_engine:
                        try:
                            self._review_engine.record_signal(signal, news_strategy_signal)
                            self._stats["signals_recorded"] += 1
                        except Exception as e:
                            logger.debug(f"复盘记录失败 (非阻塞): {e}")
                    
                    logger.info(
                        "✅ 新闻信号注入成功: %s %s | %s | 仓位=%s",
                        stock_strategy.code, action,
                        stock_strategy.confidence,
                        stock_strategy.position_tier,
                    )
                else:
                    rejected.append(stock_strategy)
                    self._stats["signals_rejected"] += 1
                    logger.info(
                        "🚫 新闻信号被拦截: %s (冷却/风控)",
                        stock_strategy.code,
                    )
            else:
                # 不注入，仅返回
                injected.append(signal)
        
        # 钉钉推送
        if auto_alert and injected:
            self._send_strategy_alert(news_strategy_signal, injected)
        
        return injected, rejected
    
    def _build_signal(self, stock_strategy, news_signal, action: str):
        """构建 Signal 对象"""
        from app.strategy.base import Signal, SignalAction
        from app.strategy.consensus_types import SignalSource
        
        # 计算过期时间
        expire_seconds = URGENCY_TO_EXPIRE.get(
            stock_strategy.urgency, 1800
        )
        expire_at = time.time() + expire_seconds if expire_seconds > 0 else 0
        
        # 优先级
        priority = CONFIDENCE_TO_PRIORITY.get(
            stock_strategy.confidence, 2
        )
        
        # 构建 reason
        reason = (
            f"[新闻驱动] {news_signal.event_sector} | "
            f"LLM {stock_strategy.direction} | "
            f"置信度={stock_strategy.confidence} | "
            f"仓位建议={stock_strategy.position_tier}"
        )
        if stock_strategy.reasoning:
            reason += f"\n逻辑: {stock_strategy.reasoning}"
        
        # 构建 extra 元数据
        extra = {
            'source_type': 'NEWS_DRIVEN',
            'news_id': news_signal.news_id,
            'news_title': news_signal.news_title,
            'event_type': news_signal.event_type,
            'event_sector': news_signal.event_sector,
            'impact_chain': news_signal.impact_chain,
            'risk_bias': news_signal.risk_bias,
            'market_overview': news_signal.market_overview,
            'decay_logic': news_signal.decay_logic,
            'llm_direction': stock_strategy.direction,
            'llm_confidence': stock_strategy.confidence,
            'position_tier': stock_strategy.position_tier,
            'urgency': stock_strategy.urgency,
            'sentiment_expiry_minutes': stock_strategy.sentiment_expiry_minutes,
            'us_hk_mapping': stock_strategy.us_hk_mapping,
            'stop_loss_pct': stock_strategy.stop_loss,
            'take_profit_pct': stock_strategy.take_profit,
            'impact_score': stock_strategy.impact_score,
            'second_order_effects': stock_strategy.second_order_effects,
            'risk_flags': stock_strategy.risk_flags,
        }
        
        # Generate TraceID for News Strategy Signal
        import uuid
        from datetime import datetime as _dt
        trace_id = f"NEWS-{_dt.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

        signal = Signal(
            id=f"NEWS_{stock_strategy.code}_{int(time.time())}_{hash(stock_strategy.code) % 1000:03d}",
            code=stock_strategy.code,
            name=stock_strategy.name,
            action=SignalAction(action),
            price=stock_strategy.entry_price if stock_strategy.entry_price > 0 else 0.0,
            quantity=0,  # 由 executor._get_qty 动态计算整手数量
            trace_id=trace_id,
            source=SignalSource.EVENT,
            reason=reason,
            strategy='llm_news_strategy',
            priority=priority,
            expire_at=expire_at,
            extra=extra,
        )
        
        return signal
    
    def _check_position_conflicts(self, news_strategy_signal) -> Dict:
        """
        检查仓位冲突 — 2026-04-15 新增
        
        对比新信号和现有持仓，返回 {code: ConflictResult}
        """
        from app.factors.position_conflict_resolver import (
            get_resolver,
            NewSignalAssessment,
            PositionScore,
            score_new_signal_from_data,
            score_position_from_data,
        )
        
        resolver = get_resolver()
        results = {}
        
        try:
            # 1. 获取现有持仓 (从 Futu API)
            existing_positions = self._get_existing_positions()
            
            # 2. 对每个新信号做冲突检查
            for stock_strategy in news_strategy_signal.stock_strategies:
                direction = stock_strategy.direction
                if direction not in ("STRONG_BUY", "BUY"):
                    continue  # 只有买入信号需要检查仓位冲突
                
                # 构建新信号评估
                extra = {}
                if hasattr(news_strategy_signal, 'extra'):
                    extra = news_strategy_signal.extra
                elif hasattr(news_strategy_signal, '__dict__'):
                    extra = {k: v for k, v in vars(news_strategy_signal).items()
                             if not k.startswith('_')}
                
                new_signal = score_new_signal_from_data(
                    code=stock_strategy.code,
                    name=stock_strategy.name,
                    direction=stock_strategy.direction,
                    confidence=stock_strategy.confidence,
                    urgency=getattr(stock_strategy, 'urgency', 'WAIT_CONFIRM'),
                    event_type=getattr(news_strategy_signal, 'event_type', ''),
                    impact_score=getattr(stock_strategy, 'impact_score', 0.5),
                    sentiment_expiry_minutes=getattr(stock_strategy, 'sentiment_expiry_minutes', 120),
                    position_tier=getattr(stock_strategy, 'position_tier', 'satellite'),
                    us_hk_mapping=getattr(stock_strategy, 'us_hk_mapping', ''),
                    has_cross_market_validation=bool(getattr(stock_strategy, 'us_hk_mapping', '')),
                    news_age_minutes=0,  # 刚生成的信号
                )
                
                # 调用决策引擎
                result = resolver.resolve(new_signal, existing_positions)
                results[stock_strategy.code] = result
                
                logger.info(
                    "仓位冲突检查: %s → %s (新信号=%.1f, 最弱=%.1f)",
                    stock_strategy.code, result.decision.value,
                    result.new_signal_score, result.weakest_position_score,
                )
                
        except Exception as e:
            logger.error(f"仓位冲突检查失败 (非阻塞): {e}")
            # 失败时不阻拦信号，让后续管线正常处理
        
        return results
    
    def _get_existing_positions(self) -> list:
        """获取现有持仓列表，返回 PositionScore 列表"""
        from app.factors.position_conflict_resolver import score_position_from_data
        
        positions = []
        
        # 2026-04-15 修复: 优先使用富途服务端实时查询
        try:
            from app.factors.live_position_query import get_live_position_query
            live_query = get_live_position_query()
            active_positions = live_query.get_active_positions(market='HK')
            
            if active_positions:
                for pos in active_positions:
                    score = score_position_from_data(
                        code=pos.code,
                        name=pos.name,
                        pnl_pct=pos.pl_ratio,
                        current_price=pos.current_price,
                        entry_price=pos.cost_price,
                        holding_hours=(pos.query_time - 0) / 3600 if pos.query_time > 0 else 24,
                        original_confidence='MEDIUM',  # 从富途无法获取原始置信度
                        trend_strength=50,
                        momentum_1h=0,
                        stop_loss_pct=-8.0,
                        take_profit_pct=15.0,
                    )
                    positions.append(score)
                logger.info(f"仓位冲突检查: 从富途服务端获取 {len(positions)} 只持仓")
                return positions
                
        except Exception as e:
            logger.debug(f"富途实时查询失败，尝试降级方案: {e}")
        
        # 降级方案 1: 从 Futu API 获取持仓
        try:
            from app.simulation import api as futu_api
            portfolio = futu_api.get_portfolio()
            
            if portfolio and 'positions' in portfolio:
                for pos in portfolio['positions']:
                    code = pos.get('code', '')
                    if not code:
                        continue
                    
                    score = score_position_from_data(
                        code=code,
                        name=pos.get('name', ''),
                        pnl_pct=pos.get('pnl_pct', 0),
                        current_price=pos.get('current_price', 0),
                        entry_price=pos.get('cost_price', 0),
                        holding_hours=pos.get('holding_hours', 24),
                        original_confidence=pos.get('original_confidence', 'MEDIUM'),
                        trend_strength=pos.get('trend_strength', 50),
                        momentum_1h=pos.get('momentum_1h', 0),
                        stop_loss_pct=pos.get('stop_loss_pct', -8.0),
                        take_profit_pct=pos.get('take_profit_pct', 15.0),
                    )
                    positions.append(score)
                
                if positions:
                    return positions
                    
        except Exception as e:
            logger.debug(f"模拟盘 API 获取持仓失败: {e}")
        
        # 降级方案 2: 从 SignalStore 获取
        try:
            from app.signal.core import signal_store
            pos_signals = signal_store.get_positions()
            
            for sig in pos_signals:
                extra = sig.extra or {}
                score = score_position_from_data(
                    code=sig.code,
                    name=sig.name,
                    pnl_pct=extra.get('pnl_pct', 0),
                    current_price=extra.get('current_price', sig.price),
                    entry_price=sig.price,
                    holding_hours=(time.time() - sig.timestamp) / 3600 if sig.timestamp > 0 else 24,
                    original_confidence=extra.get('llm_confidence', 'MEDIUM'),
                    trend_strength=50,
                    momentum_1h=0,
                    stop_loss_pct=extra.get('stop_loss_pct', -8.0),
                    take_profit_pct=extra.get('take_profit_pct', 15.0),
                )
                positions.append(score)
                
        except Exception as e:
            logger.debug(f"SignalStore 获取持仓失败: {e}")
        
        return positions
    
    def _send_conflict_alert(self, result, stock_strategy):
        """发送仓位冲突告警"""
        if not self._dingtalk_notifier:
            return
        
        try:
            from app.factors.position_conflict_resolver import (
                format_conflict_alert,
                NewSignalAssessment,
            )
            
            new_signal = NewSignalAssessment(
                code=stock_strategy.code,
                name=stock_strategy.name,
                direction=stock_strategy.direction,
                confidence=stock_strategy.confidence,
                urgency=getattr(stock_strategy, 'urgency', 'WAIT_CONFIRM'),
                event_type='',
                sentiment_expiry_minutes=getattr(stock_strategy, 'sentiment_expiry_minutes', 120),
                position_tier=getattr(stock_strategy, 'position_tier', 'satellite'),
                us_hk_mapping=getattr(stock_strategy, 'us_hk_mapping', ''),
                total_score=result.new_signal_score,
            )
            
            text = format_conflict_alert(result, new_signal)
            self._dingtalk_notifier.send_text(text)
            
        except Exception as e:
            logger.error(f"冲突告警发送失败: {e}")

    def _send_strategy_alert(self, news_signal, signals: List):
        """发送策略信号钉钉推送"""
        if not self._dingtalk_notifier:
            return
        
        try:
            text = self._build_alert_text(news_signal, signals)
            result = self._dingtalk_notifier.send_text(text)
            if result:
                self._stats["alerts_sent"] += 1
                logger.info("✅ 策略信号钉钉推送成功")
            else:
                logger.warning("❌ 策略信号钉钉推送失败")
        except Exception as e:
            logger.error("策略信号钉钉推送异常: %s", e)
    
    def _build_alert_text(self, news_signal, signals: List) -> str:
        """构建钉钉推送文本"""
        # 判断整体方向
        bias_icon = {
            "Risk-On": "🐂",
            "Risk-Off": "🐻",
            "中性": "⚖️",
        }.get(news_signal.risk_bias, "📊")
        
        # 构建信号详情
        signal_lines = []
        for sig in signals:
            extra = sig.extra
            direction = extra.get('llm_direction', 'N/A')
            confidence = extra.get('llm_confidence', 'N/A')
            position = extra.get('position_tier', 'N/A')
            stop_loss = extra.get('stop_loss_pct', 0)
            take_profit = extra.get('take_profit_pct', 0)
            expiry = extra.get('sentiment_expiry_minutes', 120)
            us_hk = extra.get('us_hk_mapping', '')
            
            # 方向 emoji
            dir_emoji = {"BUY": "📈", "SELL": "📉"}.get(sig.action.value, "📊")
            
            signal_lines.append(
                f"### {dir_emoji} {sig.code} ({sig.name})\n"
                f"| 项目 | 内容 |\n"
                f"|------|------|\n"
                f"| **方向** | {direction} |\n"
                f"| **置信度** | {confidence} |\n"
                f"| **仓位** | {position} |\n"
                f"| **止盈** | +{take_profit}% |\n"
                f"| **止损** | {stop_loss}% |\n"
                f"| **情绪有效** | {expiry}分钟 |\n"
                f"\n**美股映射**: {us_hk or 'N/A'}\n"
            )
            if extra.get('second_order_effects'):
                effects = "、".join(extra['second_order_effects'][:2])
                signal_lines.append(f"🔄 **二阶效应**: {effects}")
        
        text = (
            f"### {bias_icon} LLM 新闻驱动策略信号 | {news_signal.risk_bias}\n\n"
            f"**触发新闻**: {news_signal.news_title}\n\n"
            f"**事件类型**: {news_signal.event_type}\n\n"
            f"**影响板块**: {news_signal.event_sector}\n\n"
            f"**市场判断**: {news_signal.market_overview or 'N/A'}\n\n"
            f"**情绪衰减**: {news_signal.decay_logic or 'N/A'}\n\n"
            f"---\n\n"
            f"## 📋 交易信号 ({len(signals)}只)\n\n"
            + "\n\n".join(signal_lines) +
            f"\n\n---\n"
            f"⚡ LLM 驱动 · 跨市场映射 · 时效管理 · 仅供参考不构成投资建议"
        )
        
        return text
    
    def get_stats(self) -> dict:
        return {**self._stats}


# ============================================================
# 全局单例
# ============================================================

_global_bridge = None

def get_signal_bridge(signal_store=None, dingtalk_notifier=None) -> NewsSignalBridge:
    """获取全局桥接器单例"""
    global _global_bridge
    if _global_bridge is None:
        _global_bridge = NewsSignalBridge(signal_store, dingtalk_notifier)
    return _global_bridge


def reset_bridge():
    """重置桥接器 (测试用)"""
    global _global_bridge
    _global_bridge = None
