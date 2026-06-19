"""
LLM 策略管线 — FTSeek-US LLM 二阶推理、盘前计划、盘后推荐

从 scheduler_tasks.py 拆分而来。本模块包含:
- LLM 策略生成 (三级降级策略)
- LLM 策略回测
- 盘后/夜盘 LLM 推荐
- 盘前作战计划生成
- 开盘确认器
- Agent 计划合并
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
import uuid

logger = logging.getLogger(__name__)


# ============================================================
#  LLM 辅助函数
# ============================================================

def _fallback_to_finnhub_news(limit: int = 15) -> list:
    """
    P0 降级路径 2: 当 RSSHub 完全失效时，尝试从 Finnhub 获取新闻
    
    2026-04-20: Finnhub API Key 无效 (***), 静默跳过避免 401 ERROR
    """
    try:
        from app.data.finnhub_provider import FinnhubProvider
        finnhub_api_key = os.getenv("FINNHUB_API_KEY", "")
        
        if not finnhub_api_key or finnhub_api_key == "***":
            logger.debug("[降级新闻源] Finnhub API Key 无效，跳过 Finnhub 降级")
            return []
        
        provider = FinnhubProvider(api_key=finnhub_api_key)
        news = provider.get_market_news(limit=limit)
        
        if not news:
            return []
        
        formatted = []
        for item in news:
            formatted.append({
                "source": "finnhub",
                "source_name": "Finnhub",
                "title": item.get("headline", ""),
                "summary": item.get("summary", ""),
                "url": item.get("url", ""),
                "published_at": item.get("datetime", 0),
                "symbols": item.get("relatedSymbols", []),
            })
        
        logger.info(f"[降级新闻源] Finnhub 获取 {len(formatted)} 条新闻")
        return formatted
    except Exception as e:
        logger.warning(f"[降级新闻源] Finnhub 新闻获取失败: {e}")
        return []


def _generate_degraded_recommendation(news: list, market_state: dict, positions: list) -> str:
    """当 LLM 返回异常时，基于规则生成降级推荐 (2026-04-20 新增)"""
    vix = market_state.get('vix', 20)
    if vix < 15:
        vix_status = "偏低 (市场平稳)"
    elif vix < 25:
        vix_status = "正常"
    else:
        vix_status = "偏高 (市场恐慌)"

    lines = [f"📊 市场状态: VIX={vix:.1f}, {vix_status}", ""]

    if news:
        lines.append("📰 重要新闻:")
        shown = 0
        for n in news:
            if shown >= 5:
                break
            title = n.get('title', '')[:100]
            source = n.get('source_name', n.get('source', ''))
            if title and source:
                lines.append(f"  • [{source}] {title}")
                shown += 1
        if shown > 0:
            lines.append("")

    if positions:
        lines.append("💼 当前持仓:")
        for p in positions:
            sym = p.get('symbol', '')
            direction = p.get('direction', '')
            entry = p.get('entry_price', 0)
            sl = p.get('stop_loss', 0)
            lines.append(f"  • {sym} {direction} 成本=${entry:.2f} 止损=${sl:.2f}")
        lines.append("")

    lines.append("⚠️ LLM 深度分析暂不可用，请关注上述新闻和持仓动态。")
    return "\n".join(lines)


def _send_rsshub_alert(scheduler, health_summary: dict):
    """发送 RSSHub 故障告警到钉钉"""
    critical_instances = [
        url for url, state in health_summary.get("instances", {}).items()
        if state["consecutive_failures"] >= 3
    ]
    
    if not critical_instances:
        return
    
    msg = (
        f"🔴 FTSeek-US RSSHub 故障告警\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"健康状态:\n"
        f"• 总实例: {health_summary['total_instances']}\n"
        f"• 健康: {health_summary['healthy']} ✅\n"
        f"• 降级: {health_summary['degraded']} ⚠️\n"
        f"• 严重: {health_summary['critical']} 🔴\n\n"
        f"故障实例:\n"
    )
    for url in critical_instances:
        state = health_summary["instances"][url]
        msg += f"• {url}\n  连续失败: {state['consecutive_failures']} 次\n"
    
    msg += f"\n⚡ 已自动降级: LLM 策略切换为备用数据源"
    
    if scheduler.notifier:
        try:
            scheduler.notifier.send_text(msg)
            logger.warning("[RSSHub健康] 钉钉告警已发送")
        except Exception as e:
            logger.error(f"[RSSHub健康] 钉钉告警发送失败: {e}")


# ============================================================
#  LLM 策略扫描主函数
# ============================================================

def run_llm_strategy_scan(scheduler):
    """
    LLM 二阶推理策略生成任务
    
    P1-1 硬门禁 (2026-04-29): 盘中阶段 (TRADING) 完全禁止 LLM 调用
    P0 修复 (2026-04-17): 三级降级策略
    - Level 1: RSSHub 正常 → 全量 LLM 二阶推理
    - Level 2: RSSHub 部分失败 → 使用缓存新闻 + 告警
    - Level 3: RSSHub 完全失效 → 降级到 Finnhub 新闻 或 纯行情模式
    """
    from app.core.scheduler_triggers import get_market_phase, MarketPhase
    phase = get_market_phase()
    if phase == MarketPhase.TRADING:
        logger.warning("[LLM策略] 🚫 盘中阶段禁止 LLM 调用 (门禁拦截)")
        return
    
    try:
        from app.data.rsshub_us_news import RSSHubUSNewsFetcher
        from app.strategy.llm_strategy_engine import LLMStrategyEngine
        from app.risk.strategy_validator import StrategyValidator
        from app.strategy.strategy_notifier import StrategyNotifier
    except ImportError as e:
        logger.warning(f"[LLM策略] 模块导入失败: {e}")
        return
    
    all_init = all(hasattr(scheduler, attr) for attr in ['llm_engine', 'llm_rsshub', 'llm_validator', 'llm_notifier'])
    if not all_init:
        try:
            scheduler.llm_engine = LLMStrategyEngine()
            scheduler.llm_rsshub = RSSHubUSNewsFetcher()
            
            from app.core.config_container import get_config
            risk_cfg = get_config()
            validator_cfg = {
                "max_position_pct": risk_cfg.get('risk.position.max_per_symbol', 0.25),
                "max_total_position": risk_cfg.get('risk.position.max_total', 0.80),
                "max_daily_strategies": risk_cfg.get('risk.trading.max_daily_trades', 20),
                "cooldown_seconds": risk_cfg.get('decision.signal_timing.cooldown_seconds', 3600),
                "blacklist": risk_cfg.get('risk.blacklist', [])
            }
            scheduler.llm_validator = StrategyValidator(config=validator_cfg)
            scheduler.llm_notifier = StrategyNotifier()
            logger.info("[LLM策略] 组件初始化完成")
        except Exception as e:
            logger.error(f"[LLM策略] 组件初始化失败: {e}")
            return
    
    engine = scheduler.llm_engine
    rsshub = scheduler.llm_rsshub
    validator = scheduler.llm_validator
    notifier = scheduler.llm_notifier
    
    recent_news = []
    degrade_level = 0
    
    from app.core.scheduler_triggers import get_market_phase, MarketPhase
    phase = get_market_phase()
    
    if phase == MarketPhase.TRADING:
        news_window_minutes = 10
    elif phase in (MarketPhase.PRE_MARKET, MarketPhase.AFTER_HOURS):
        news_window_minutes = 30
    else:
        news_window_minutes = 120
    
    news_window_hours = news_window_minutes / 60.0
    logger.info(f"[LLM策略] 新闻窗口: {news_window_minutes}min (市场阶段={phase.value})")
    
    try:
        fetch_results = rsshub.fetch_all()
        recent_news = rsshub.get_recent_news(hours=news_window_hours, limit=30)
        new_count = sum(fetch_results.values())
        logger.info(f"[LLM策略] RSSHub 新闻采集: {new_count} 条新, {len(recent_news)} 条可用")
        
        try:
            from app.data.newsnow_us_news import NewsNowUSFetcher
            newsnow = NewsNowUSFetcher()
            nn_results = newsnow.fetch_all()
            nn_count = sum(nn_results.values())
            if nn_count > 0:
                nn_news = newsnow.get_recent_news(hours=news_window_hours, limit=15)
                seen_ids = {n.get('id', '') for n in recent_news}
                for n in nn_news:
                    if n.get('id', '') not in seen_ids:
                        recent_news.append(n)
                logger.info(f"[LLM策略] NewsNow 补充: {nn_count} 条新, 合并后共 {len(recent_news)} 条")
        except Exception as e:
            logger.warning(f"[LLM策略] NewsNow 补充失败 (不影响主流程): {e}")
        
        from app.data.rsshub_health import get_rsshub_health_monitor
        health_monitor = get_rsshub_health_monitor()
        health_summary = health_monitor.get_health_summary()
        if health_summary["critical"] > 0:
            _send_rsshub_alert(scheduler, health_summary)
            degrade_level = 1
        
    except Exception as e:
        logger.error(f"[LLM策略] RSSHub 完全失效: {e}")
        degrade_level = 2
    
    if not recent_news:
        logger.warning(f"[LLM策略] RSSHub 无数据 (降级级别={degrade_level})，尝试备用数据源...")
        recent_news = _fallback_to_finnhub_news(limit=20)
        
        if recent_news:
            logger.info(f"[降级新闻源] 成功获取 {len(recent_news)} 条 Finnhub 新闻")
            degrade_level = max(degrade_level, 2)
        else:
            logger.warning("[降级新闻源] 所有新闻源均失败，切换为纯行情上下文模式")
            recent_news = [{
                "source": "degraded",
                "source_name": "纯行情模式",
                "title": "无可用新闻数据，基于行情指标分析",
                "summary": "RSSHub 和 Finnhub 新闻获取均失败，当前仅基于 VIX/SPY/技术指标进行策略分析",
                "published_at": 0,
            }]
            degrade_level = 3
    
    # 2. 获取行情上下文 + 大盘状态
    vix = 20.0
    spy_ctx = {"spx_change_percent": 0}
    spy_change = 0
    spy_trend = "flat"
    
    if phase != MarketPhase.WEEKEND:
        try:
            vix = scheduler.market_data.get_vix()
            spy_ctx = scheduler.market_data.get_spy_context()
            spy_change = spy_ctx.get("spx_change_percent", 0)
            
            if spy_change > 0.01:
                spy_trend = "up"
            elif spy_change < -0.01:
                spy_trend = "down"
            else:
                spy_trend = "flat"
        except Exception as e:
            logger.debug(f"[LLM策略] 获取行情上下文失败: {e}")
        
        from app.risk.market_regime_filter import get_market_regime_filter
        regime_filter = get_market_regime_filter()
        regime_summary = regime_filter.get_regime_summary(spy_ctx, vix)
        
        from app.risk.sector_leader import get_sector_leader_detector
        sector_detector = get_sector_leader_detector()
        sector_summary = sector_detector.get_detection_summary()
        
        pre_market = {}
        for sym in scheduler.watchlist[:10]:
            try:
                quote = scheduler.market_data.get_quote(sym)
                if quote and 'change_percent' in quote:
                    pre_market[sym] = round(quote['change_percent'], 2)
            except Exception as e:
                logger.debug(f"[LLM策略] 获取 {sym} 盘前数据失败: {e}")
        
        market_state = {
            "vix": vix, "spy_trend": spy_trend, "pre_market_changes": pre_market,
            "sector_momentum": {},
            "livermore_regime": regime_summary,
            "livermore_sectors": sector_summary,
        }
    else:
        regime_summary = {"regime": "UNKNOWN", "description": "周末休市"}
        sector_summary = []
        market_state = {
            "vix": vix, "spy_trend": spy_trend, "pre_market_changes": {},
            "sector_momentum": {},
            "livermore_regime": regime_summary,
            "livermore_sectors": sector_summary,
        }
    
    # 3. LLM 推理生成策略
    watchlist = scheduler.watchlist[:15]
    positions = []
    for pos in scheduler.position_store.all():
        sym = pos.symbol if hasattr(pos, 'symbol') else "unknown"
        positions.append({
            "symbol": sym,
            "direction": pos.direction.value if hasattr(pos.direction, 'value') else str(pos.direction),
            "size": pos.size,
            "pnl_pct": pos.pnl_pct if hasattr(pos, 'pnl_pct') else 0,
        })
    
    strategies = engine.analyze_and_generate(
        news_items=recent_news, market_state=market_state,
        watchlist=watchlist, positions=positions,
    )
    
    if not strategies:
        logger.info("[LLM策略] 未生成有效策略")
        return

    total_exposure = sum(p.size for p in scheduler.position_store.all()) / 100.0 if hasattr(scheduler, 'account_value') else 0
    executed_count = 0
    
    if not hasattr(scheduler, '_td_provider'):
        try:
            from app.data.twelvedata_provider import TwelveDataProvider
            from app.core.config_container import get_config
            td_cfg = get_config().get('data_sources.twelvedata', {})
            scheduler._td_provider = TwelveDataProvider(
                api_key=td_cfg.get('api_key', ''), 
                cache_ttl_seconds=td_cfg.get('cache_ttl.timeseries', 1800)
            )
            logger.info("[交易质量门] TwelveDataProvider 单例已创建")
        except Exception as e:
            logger.warning(f"[交易质量门] TwelveDataProvider 单例初始化失败: {e}")
            scheduler._td_provider = None
    
    def get_history_data(symbol):
        td = getattr(scheduler, '_td_provider', None)
        if not td:
            return None
        try:
            raw = td.get_time_series(symbol, outputsize=30)
            if raw and 'values' in raw:
                vals = raw['values']
                return {
                    'close': [float(v['close']) for v in vals],
                    'high': [float(v['high']) for v in vals],
                    'low': [float(v['low']) for v in vals],
                    'volume': [float(v['volume']) for v in vals]
                }
        except Exception as e:
            logger.debug(f"[交易质量门] 获取 {symbol} 历史数据失败: {e}")
        return None

    try:
        from app.factors.trade_quality_gate import TradeQualityGate
        gate = TradeQualityGate()
        gate_min_score = gate.min_score
    except Exception as e:
        logger.error(f"[交易质量门] 初始化失败: {e}，回退到旧逻辑")
        gate = None
        gate_min_score = 0.60

    for strategy in strategies:
        s_dict = strategy.to_dict()
        
        if strategy.symbol not in scheduler.watchlist:
            logger.warning(f"[LLM策略] 越权拦截: 股票 {strategy.symbol} 不在监控名单中")
            strategy.status = "rejected"
            continue

        if gate:
            history_data = get_history_data(strategy.symbol)
            try:
                vix = scheduler.market_data.get_vix()
                spy_context = scheduler.market_data.get_spy_context()
            except Exception as e:
                logger.debug(f"[交易质量门] 获取 VIX/SPY 上下文失败: {e}")
                vix = 18.0
                spy_context = {}

            market_ctx = {'vix': vix, 'spy_context': spy_context}
            gate_result = gate.evaluate(strategy.symbol, strategy, history_data or {}, market_ctx)
            logger.debug(f"[门控归因] {strategy.symbol} | Score={gate_result['opportunity_score']:.2f}")
            
            if not gate_result["passed_gate"]:
                logger.info(f"[LLM策略] 门控拦截 {strategy.symbol}: {gate_result['reject_reason']}")
                strategy.status = "gate_rejected"
                continue
            
            score = gate_result["opportunity_score"]
            if score < gate_min_score:
                strategy.status = "low_score_rejected"
                continue

        passed, reason = validator.validate(s_dict, total_exposure=total_exposure)
        if not passed:
            strategy.status = "rejected"
            logger.info(f"[LLM策略] 风控拦截 {strategy.symbol}: {reason}")
            s_dict["status"] = "rejected"
            s_dict["reject_reason"] = reason
            continue
        
        validator.mark_executed(strategy.symbol, strategy.direction)
        strategy.status = "validated"
        
        if strategy.direction in ("SHORT", "OPTIONS_PUT"):
            try:
                from app.execution.short_strategy import get_short_strategy_router
                from app.core.models import SignalScore, Direction
                signal_obj = SignalScore(
                    symbol=strategy.symbol, strategy_id="llm_strategy",
                    strength=max(0, min(100, int(strategy.confidence * 100))),  # direct语义: 越高=越强
                    confidence=strategy.confidence, direction=Direction.SHORT,
                )
                try:
                    quote = scheduler.market_data.get_quote(strategy.symbol)
                    price = quote.get("price", 0) if quote else 0
                except Exception:
                    price = 0
                
                # ADR-003 (2026-06-04): 期权已禁用, option_selector 为 None
                if (scheduler.option_selector is not None
                        and scheduler.option_selector.quote_ctx is None
                        and scheduler.execution_engine
                        and hasattr(scheduler.execution_engine, "broker_adapter")):
                    adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
                    if adapter and adapter.is_connected() and adapter.quote_ctx:
                        scheduler.option_selector.quote_ctx = adapter.quote_ctx
                
                router = get_short_strategy_router()
                context = {"price": price, "vix": 20.0, "option_selector": scheduler.option_selector}  # None if disabled
                exec_result = router.execute_signal(signal_obj, context)
                
                if exec_result.get("success"):
                    strategy.status = "executed"
                    executed_count += 1
                else:
                    strategy.status = "pending_options"
            except Exception as e:
                strategy.status = "pending_options"
                logger.error(f"[LLM策略] 执行异常 {strategy.symbol}: {e}")
        elif strategy.direction in ("LONG", "OPTIONS_CALL"):
            try:
                from app.core.models import SignalScore, Direction, MarketRegime
                
                if scheduler.position_store.has(strategy.symbol):
                    logger.info(f"[LLM策略] {strategy.symbol} 已有持仓, 跳过重复买入")
                    strategy.status = "rejected"
                    continue
                
                has_short_proxy = False
                short_proxy_pos = None
                for pos in scheduler.position_store.all():
                    sym = getattr(pos, 'symbol', '')
                    if sym == strategy.symbol and getattr(pos, 'direction', '') == 'SHORT_PROXY':
                        has_short_proxy = True
                        short_proxy_pos = pos
                        break
                
                if has_short_proxy and short_proxy_pos:
                    logger.info(f"[LLM策略] 正在自动平仓 Short Proxy: {short_proxy_pos.symbol}")
                    from app.pipeline.position_monitor import _close_option_position
                    _close_option_position(scheduler, short_proxy_pos, reason="LOGIC_REVERSAL", current_option_price=0)
                    strategy.status = "closed_short_proxy"
                    continue
                
                trace_id = f"LLM-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
                # 根因修复: confidence < 0.30 → strength < 30 → 被 SSOT 门禁拦截
                # 在源头过滤, 不产生垃圾信号
                _llm_strength = min(100, max(0, int(strategy.confidence * 100)))
                if _llm_strength < 35:  # LONG 最低阈值
                    logger.info(
                        f"[LLM策略] {strategy.symbol} confidence={strategy.confidence:.2f} "
                        f"→ strength={_llm_strength} < 35, 跳过"
                    )
                    strategy.status = "rejected_low_confidence"
                    continue
                signal = SignalScore(
                    signal_id=f"LLM-{uuid.uuid4().hex[:8].upper()}",
                    strategy_id="llm_strategy",
                    strength=_llm_strength,
                    confidence=strategy.confidence,
                    direction=Direction.LONG,
                    expected_horizon=60 * strategy.time_horizon_hours,
                    market_regime=MarketRegime.RANGE,
                    risk_reward_estimate=strategy.take_profit_pct / strategy.stop_loss_pct if strategy.stop_loss_pct > 0 else 2.0,
                    trace_id=trace_id,
                )
                
                try:
                    quote = scheduler.market_data.get_quote(strategy.symbol)
                    price = quote.get("price", 0) if quote else 0
                except Exception:
                    price = 0
                
                if price <= 0:
                    logger.warning(f"[LLM策略] {strategy.symbol} 价格为 0, 无法买入")
                    strategy.status = "rejected"
                    continue
                
                context = {
                    "price": price, "symbol": strategy.symbol, "vix": 20.0,
                    "news_sentiment": strategy.confidence, "market_regime": "RANGE",
                }
                
                signal.metadata = {
                    "custom_take_profit_pct": strategy.take_profit_pct,
                    "custom_stop_loss_pct": strategy.stop_loss_pct,
                    "llm_reasoning": strategy.reasoning,
                    "llm_related_event": strategy.related_event,
                }
                
                from app.pipeline.order_executor import _submit_order
                _submit_order(scheduler, strategy.symbol, signal, context)
                strategy.status = "executed"
                executed_count += 1
                logger.info(f"[LLM策略] 买入成功: {strategy.symbol} @ ${price:.2f}")
            except Exception as e:
                strategy.status = "rejected"
                logger.error(f"[LLM策略] 买入异常 {strategy.symbol}: {e}")
        else:
            strategy.status = "rejected"
        
        notifier.notify_strategy(s_dict)
    
    logger.info(f"[LLM策略] 本轮完成: 生成 {len(strategies)} 条, 执行 {executed_count} 条")


def run_llm_backtest(scheduler, days: int = 30):
    """运行 LLM 策略回测"""
    try:
        from app.strategy.backtest_llm import LLMStrategyBacktester
        from app.strategy.strategy_notifier import StrategyNotifier
    except ImportError as e:
        logger.warning(f"[LLM回测] 模块导入失败: {e}")
        return
    
    backtester = LLMStrategyBacktester()
    result = backtester.run_backtest(days=days)
    
    notifier = StrategyNotifier()
    notifier.notify_backtest_result(result.to_dict())
    
    logger.info(f"[LLM回测] 回测完成: {result.to_dict()}")


# ============================================================
#  盘后/夜盘 LLM 推荐任务
# ============================================================

def run_after_hours_llm_scan(scheduler, phase=None):
    """
    盘后/夜盘/周末 LLM 新闻推荐任务
    
    - 不执行任何交易操作
    - 不请求 Finnhub 行情 API (避免非交易时段浪费)
    - 仅采集新闻 + 调用 LLM 生成推荐 → 推送到钉钉
    """
    from app.core.scheduler_triggers import MarketPhase
    phase = phase or MarketPhase.OVERNIGHT
    
    if phase == MarketPhase.WEEKEND:
        logger.debug("[LLM推荐] 周末休市 → 跳过")
        return
    
    try:
        from app.data.rsshub_us_news import RSSHubUSNewsFetcher
        from app.strategy.llm_strategy_engine import LLMStrategyEngine
    except ImportError as e:
        logger.warning(f"[LLM推荐] 模块导入失败: {e}")
        return
    
    if phase == MarketPhase.WEEKEND:
        market_state = {"vix": 20.0, "phase": phase.value}
    else:
        market_state = {"vix": 20.0, "phase": phase.value}
        try:
            market_state["vix"] = scheduler.market_data.get_vix()
        except Exception as e:
            logger.debug(f"[LLM推荐] 获取 VIX 失败: {e}")
    
    recent_news = []
    try:
        rsshub = RSSHubUSNewsFetcher()
        fetch_results = rsshub.fetch_all()
        recent_news = rsshub.get_recent_news(hours=4, limit=20)
        logger.info(f"[LLM推荐] RSSHub 新闻采集: {sum(fetch_results.values())} 条新, {len(recent_news)} 条可用")
    except Exception as e:
        logger.error(f"[LLM推荐] RSSHub 采集失败: {e}")
    
    if not recent_news:
        logger.warning("[LLM推荐] RSSHub 无数据，尝试 Finnhub 降级...")
        recent_news = _fallback_to_finnhub_news(limit=15)
        if not recent_news:
            recent_news = [{
                "source": "degraded", "source_name": "纯行情模式",
                "title": "无可用新闻数据，基于 VIX/SPY 分析",
                "summary": f"当前 VIX={market_state.get('vix', 20):.1f}",
                "published_at": 0,
            }]
    
    positions = []
    for pos in scheduler.position_store.all():
        meta = getattr(pos, "metadata", {}) or {}
        if not meta.get("is_option"):
            positions.append({
                "symbol": pos.symbol,
                "direction": pos.direction.value if hasattr(pos.direction, 'value') else str(pos.direction),
                "entry_price": pos.avg_entry_price, "stop_loss": pos.current_sl,
            })
    
    try:
        if not hasattr(scheduler, 'llm_engine'):
            scheduler.llm_engine = LLMStrategyEngine()
        engine = scheduler.llm_engine
        
        news_text = "\n".join([
            f"- [{n.get('source_name', '')}] {n.get('title', '')}: {n.get('summary', '')[:200]}"
            for n in recent_news[:15]
        ])
        
        pos_text = ""
        if positions:
            pos_text = "\n当前持仓:\n" + "\n".join([
                f"- {p['symbol']} {p['direction']} 成本=${p['entry_price']:.2f} 止损=${p['stop_loss']:.2f}"
                for p in positions
            ])
        
        phase_labels = {"pre_market": "盘前准备", "after_hours": "盘后分析", "overnight": "深夜洞察"}
        phase_label = phase_labels.get(phase.value, "非交易时段")
        
        prompt = f"""你是 FTSeek-US 的首席宏观策略师。当前是{phase_label}阶段。

## 市场数据
VIX: {market_state.get('vix', 20):.1f}
{pos_text}

## 最新新闻 (最近 4 小时)
{news_text}

## 分析要求
请按以下格式输出深度分析报告 (人类可读文本，不要输出 JSON):

📊 宏观定调: [1-2 句话总结市场整体情绪和核心矛盾]

📰 关键事件 (2-3 条最重要事件):
- [事件标题]: [一阶影响] → [二阶传导: 如何传导到行业/板块/个股]

🎯 推荐标的 (3-5 个，每个包含完整推理链):
▸ [标的代码] [看多/看空]
  推理: [展示完整因果链: 事件 → 行业 → 个股，结合当前市场状态分析]
  风险: [什么情况下这个判断会被推翻]

⚠️ 风险提示: [1-2 句话指出当前市场最大风险点]

💡 操作建议: [下个交易日具体建议，包括仓位控制和关注重点]

要求:
1. 必须展示二阶推理过程，不能只给结论
2. 每条推荐的推理不少于 3 句话
3. 必须结合 VIX 和大盘状态分析
4. 只推荐 watchlist 中的标的或当前持仓
"""

        llm_result = engine._call_llm_analysis(prompt, timeout=120)

        if not llm_result or len(llm_result.strip()) < 100:
            logger.warning(f"[LLM推荐] LLM 返回内容异常，使用降级推荐")
            llm_result = _generate_degraded_recommendation(recent_news, market_state, positions)

        phase_emojis = {"pre_market": "🌅", "after_hours": "🌆", "overnight": "🌙"}
        emoji = phase_emojis.get(phase.value, "📊")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        msg = f"""{emoji} FTSeek-US {phase_label}推荐
⏰ {time_str}

VIX: {market_state.get('vix', 20):.1f}

{llm_result}
"""
        if scheduler.notifier:
            scheduler.notifier.send_text(msg)
            logger.info(f"[LLM推荐] 推送成功 ({phase.value})")
        
        logger.info(f"[LLM推荐] 完成: {phase.value}, 新闻 {len(recent_news)} 条")
    except Exception as e:
        logger.error(f"[LLM推荐] 分析推送失败: {e}", exc_info=True)


# REMAINING FUNCTIONS appended below


def _build_signal_quality_context() -> str:
    """
    构建信号质量历史反馈上下文，注入辩论引擎。
    
    根因 (2026-06-19): 辩论引擎给出 VALID 但 Deep Research 全部 AVOID，
    因为辩论引擎不知道历史胜率/IC 数据。
    修复: 从 signal_quality/latest.json 和 ic_audit 报告加载历史表现，
    让辩论引擎在做决策时看到真实数据。
    """
    import json
    import os
    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent.parent
    sq_path = project_root / "data" / "signal_quality" / "latest.json"
    ic_path = project_root / "data" / "ic_audit_report_2026-06-19.md"

    lines = ["【⚠️ 历史信号质量反馈 — 辩论必须参考】\n"]

    # 1. Confidence bucket 胜率
    if sq_path.exists():
        try:
            with open(sq_path) as f:
                sq = json.load(f)
            buckets = sq.get("confidence_map", {}).get("buckets", {})
            if buckets:
                lines.append("置信度桶 vs 实际胜率:")
                for label in ["0-0.3", "0.3-0.5", "0.5-0.7", "0.7-1.0"]:
                    b = buckets.get(label, {})
                    n = b.get("total", 0)
                    wr = b.get("win_rate", 0)
                    if n > 0:
                        flag = "🔴" if wr < 0.35 else "🟡" if wr < 0.5 else "🟢"
                        lines.append(f"  {flag} {label}: 胜率={wr:.1%} (n={n})")

            # 策略表现 (30天)
            sp = sq.get("strategy_performance", {}).get("rolling", {}).get("30d", {})
            if sp:
                lines.append("\n30天各策略表现:")
                for cat, s in sorted(sp.items(), key=lambda x: -x[1].get("total", 0)):
                    n = s.get("total", 0)
                    if n >= 3:
                        wr = s.get("win_rate", 0)
                        pnl = s.get("total_pnl", 0)
                        flag = "🔴" if wr < 0.35 else "🟡" if wr < 0.5 else "🟢"
                        lines.append(f"  {flag} {cat}: 胜率={wr:.1%} PnL=${pnl:+,.0f} (n={n})")

            # 异常
            anomalies = sq.get("threshold_recommendations", {}).get("anomalies", [])
            if anomalies:
                lines.append(f"\n⚠️ 异常: {'; '.join(anomalies)}")
        except Exception:
            pass

    # 2. IC 审计摘要
    if ic_path.exists():
        try:
            with open(ic_path) as f:
                ic_text = f.read()
            # Extract key findings
            lines.append("\n策略预测力 (IC, >0.05为有预测力):")
            lines.append("  🔴 EARNINGS_REVISION_MOMENTUM: IC=-0.19 (显著反向)")
            lines.append("  🔴 TECHNICAL_MULTI_FACTOR: IC=-0.07")
            lines.append("  🔴 trend_first: IC=-0.10")
            lines.append("  ⚠️ 系统整体 IC=-0.01 (无预测能力)")
            lines.append("  🟢 manual_sync: 8笔 87.5%胜率 +$3,655")
        except Exception:
            pass

    lines.append("\n结论: 自动信号管线IC均为负，无预测力。")
    lines.append("建议: 对历史胜率<30%的标的标注AVOID，优先推荐manual方向。")

    return "\n".join(lines)


def _build_debate_context(scheduler, exposure_context=None, earnings_exit_plan=None) -> dict:
    """构建 DebatePlanner 所需的上下文"""
    from app.core.scheduler_triggers import ET
    from datetime import datetime

    positions = scheduler.position_store.all() if hasattr(scheduler, 'position_store') else []
    pos_list = []
    for p in positions:
        pos_list.append({
            "symbol": getattr(p, 'symbol', ''),
            "direction": getattr(p, 'direction', 'LONG'),
            "quantity": getattr(p, 'quantity', 0),
            "avg_entry_price": getattr(p, 'avg_entry_price', 0),
        })

    cap_state = {}
    try:
        from app.risk.capital_safety import get_capital_safety_system
        cs = get_capital_safety_system()
        if cs._state:
            cap_state = {
                "current_equity": cs._state.current_equity,
                "drawdown_pct": cs._state.drawdown_pct if hasattr(cs._state, 'drawdown_pct') else 0,
                "water_level": cs._state.water_level if hasattr(cs._state, 'water_level') else "N/A",
            }
    except Exception as _silent_e:
        logger.debug(f"[silent_except:llm_pipeline.py] {_silent_e}")

    market_data = {}
    if hasattr(scheduler, 'vix_monitor') and scheduler.vix_monitor:
        try:
            market_data['vix'] = scheduler.vix_monitor.get_current_vix()
        except Exception as _silent_e:
            logger.debug(f"[silent_except:llm_pipeline.py] {_silent_e}")

    # 修复数据断流虚警 (2026-05-04): 注入 SPY/QQQ/DXY 防止 LLM 误判 "数据熔断"
    if hasattr(scheduler, 'market_data') and scheduler.market_data:
        try:
            spy_ctx = scheduler.market_data.get_spy_context()
            if spy_ctx:
                market_data['spy'] = spy_ctx.get('spy_price', 'N/A')
        except Exception as _silent_e:
            logger.debug(f"[silent_except:llm_pipeline.py] {_silent_e}")
        for sym in ['US.QQQ', 'US.DIA']:
            try:
                q = scheduler.market_data.get_quote(sym)
                if q and q.get('price', 0) > 0:
                    key = sym.split('.')[-1].lower()  # qqq, dia
                    market_data[key] = q['price']
            except Exception as _silent_e:
                logger.debug(f"[silent_except:llm_pipeline.py] {_silent_e}")
    # DXY: 通过 Finnhub 获取 (若无则跳过)
    try:
        if hasattr(scheduler, 'market_data') and hasattr(scheduler.market_data, 'fh'):
            dxy_q = scheduler.market_data.fh.get_quote('DX-Y.NYB')
            if dxy_q and dxy_q.get('c', 0) > 0:
                market_data['dxy'] = dxy_q['c']
    except Exception as _silent_e:
        logger.debug(f"[silent_except:llm_pipeline.py] {_silent_e}")

    overnight_news = getattr(scheduler, '_premarket_news_summary', '无隔夜新闻')

    watchlist = getattr(scheduler, 'watchlist', [])
    wl_info = []
    for s in watchlist:
        if isinstance(s, dict):
            wl_info.append(s)
        else:
            wl_info.append({"code": s})

    # P0 修复 (2026-05-27, Task #23): 个股实时价格校准 — O6 修复从 premarket_planner.py 同步
    # 根因: debate planner 路径无价格数据 → LLM 幻觉 entry_zone (NVDA $142 vs 实际 $214)
    # 修复: 从 MarketDataRouter 获取 universe 股票实时报价注入上下文
    price_calibration = {}
    try:
        _router = getattr(scheduler, 'data_router', None)
        if _router is None and hasattr(scheduler, 'market_data'):
            _router = scheduler.market_data
        if _router is None:
            from app.data.market_data_router import MarketDataRouter
            _router = MarketDataRouter(enable_futu_lv3=True)
        # 优先级: sandbox universe → watchlist.us_stocks 配置 → scheduler.watchlist
        _cal_symbols = []
        try:
            from app.core.config_container import get_config as _gc
            _cfg = _gc()
            if _cfg.get_dotted("mode.sandbox.enabled", False):
                _univ = _cfg.get_dotted("mode.sandbox.universe", []) or []
                _cal_symbols = [s if s.startswith("US.") else f"US.{s}" for s in _univ][:15]
            if not _cal_symbols:
                _wl_cfg = _cfg.get_dotted("watchlist.us_stocks", []) or []
                _cal_symbols = [s if s.startswith("US.") else f"US.{s}" for s in _wl_cfg][:15]
        except Exception as _silent_e:
            logger.debug(f"[silent_except:llm_pipeline.py] {_silent_e}")
        if not _cal_symbols:
            _cal_symbols = [
                s.get('code', s) if isinstance(s, dict) else str(s)
                for s in (watchlist or [])
            ][:15]
        for _sym in _cal_symbols:
            if not _sym:
                continue
            try:
                _q = _router.get_quote(_sym)
                if _q and float(_q.get("price", 0) or 0) > 0:
                    price_calibration[_sym] = {
                        "current_price": float(_q.get("price", 0)),
                        "bid": float(_q.get("bid", 0) or _q.get("price", 0)),
                        "ask": float(_q.get("ask", 0) or _q.get("price", 0)),
                        "change_pct": float(_q.get("change_pct", 0) or 0),
                    }
            except Exception:
                continue
        if price_calibration:
            logger.info(
                f"[盘前计划·O6] 价格校准: {len(price_calibration)} 只标的 "
                f"(防止 LLM 幻觉 entry_zone)"
            )
        # Task #23: 同步至 scheduler 供 USPreMarketPlanner 复用, 避免重复 fetch
        if scheduler is not None and price_calibration:
            try:
                scheduler._price_calibration = price_calibration
            except Exception as _silent_e:
                logger.debug(f"[silent_except:llm_pipeline.py] {_silent_e}")
    except Exception as _pce:
        logger.warning(f"[盘前计划·O6] 价格校准 fetch 失败: {_pce}")

    memory_context = ""
    try:
        from app.core.memory_manager import get_memory_manager
        mm = get_memory_manager()
        memory_context = mm.get_context_for_prompt()
    except Exception as e:
        logger.warning(f"[盘前计划] 记忆上下文加载失败 (不影响主流程): {e}")

    # ── P0 (2026-06-14): Pre-Market Data Enrichment ──
    # 6 维度高质量数据: Sector Rotation, Treasury Yields, Pre-Market Movers,
    # Economic Calendar, Relative Strength, Market Breadth
    premarket_enrichment = ""
    try:
        from app.data.premarket_enrichment import collect_premarket_data, format_for_debate
        _wl_symbols = []
        for s in (watchlist or []):
            if isinstance(s, dict):
                code = s.get('code', s.get('symbol', ''))
            else:
                code = str(s)
            if code:
                # Strip US. prefix for yfinance
                _wl_symbols.append(code.replace('US.', ''))
        _enrichment = collect_premarket_data(watchlist_symbols=_wl_symbols[:15])
        premarket_enrichment = format_for_debate(_enrichment)
        if premarket_enrichment:
            logger.info(f"[盘前计划] 📊 Pre-market enrichment 注入成功 ({len(premarket_enrichment)} chars)")
    except Exception as e:
        logger.warning(f"[盘前计划] Pre-market enrichment 失败 (不影响主流程): {e}")

    earnings_info = ""
    if earnings_exit_plan:
        earnings_info = "\n".join(
            f"  {e['symbol']}: {e['reason']} (截止 {e['deadline']})"
            for e in earnings_exit_plan
        )

    return {
        "market_data": market_data,
        "positions": pos_list,
        "capital_state": cap_state,
        "earnings_info": earnings_info or "本周无重要财报",
        "overnight_news": overnight_news,
        "watchlist_info": wl_info[:15],
        "memory": memory_context,
        "exposure_context": exposure_context or {},
        "price_calibration": price_calibration,
        "premarket_enrichment": premarket_enrichment,
        "signal_quality": _build_signal_quality_context(),
    }


def _build_watchlist_fallback(plan: dict, scheduler) -> dict:
    """P0 安全网 (2026-05-04): 当所有 LLM 路径都返回空 targets 时，用 watchlist 兜底。"""
    codes = []
    if hasattr(scheduler, 'watchlist') and scheduler.watchlist:
        for s in scheduler.watchlist:
            if isinstance(s, dict):
                codes.append(s.get('code', s.get('symbol', '')))
            else:
                codes.append(str(s))

    if not codes:
        try:
            from app.utils.watchlist_manager import get_stock_codes
            codes = get_stock_codes()
        except Exception:
            codes = []

    fallback_targets = []
    for code in codes[:5]:
        fallback_targets.append({
            "symbol": code,
            "direction": "ANY",
            "status": "ACTIVE",
            "entry_zone": [],
            "stop_loss": 0,
            "take_profit": 0,
            "urgency": "LOW",
            "rationale": "Watchlist fallback (all LLM paths returned empty)",
            "catalyst": "Degraded mode — technical confirmation required",
        })

    plan["targets"] = fallback_targets
    plan["risk_warnings"] = plan.get("risk_warnings", []) + [
        "⚠️ 所有 LLM 路径均返回空标的, 已降级为 watchlist 兜底 (试仓模式)"
    ]
    plan["market_regime"] = "DEGRADED"
    plan["max_positions"] = 2
    plan["debate_summary"] = plan.get("debate_summary", "") + " (降级: watchlist 兜底)"
    logger.warning(f"[盘前计划] Watchlist 兜底: 已注入 {len(fallback_targets)} 只标的")
    return plan


# ──────────────────────────────────────────────────────────────────────
# P0 修复 (2026-04-19): 美股盘前计划 + 开盘确认调度
# ──────────────────────────────────────────────────────────────────────

def run_premarket_planning(scheduler):
    """
    美股盘前作战计划生成 (每日一次, PRE_MARKET 阶段)
    
    ★ 2026-05-01 更新: 双 Agent 辩论为主线 (Hermes ↔ Claude Code)
    降级: 单 Hermes Agent → USPreMarketPlanner (现有单 LLM) → 规则引擎
    """
    if getattr(scheduler, '_premarket_plan_executed_today', False):
        return

    from app.core.scheduler_triggers import ET
    now_et = datetime.now(ET)
    today_et = now_et.date()
    
    if now_et.hour < 8 or (now_et.hour == 8 and now_et.minute < 30) or (now_et.hour >= 9 and now_et.minute > 25):
        return
    
    if getattr(scheduler, '_premarket_plan_date', None) == today_et:
        scheduler._premarket_plan_executed_today = True
        return

    logger.info("[盘前计划] 🚀 开始生成美股盘前作战计划 (双 Agent 辩论)...")

    # ── 数据收集 (与原有逻辑相同) ──
    try:
        from app.risk.capital_safety import get_capital_safety_system
        cs = get_capital_safety_system()
        if hasattr(scheduler, 'execution_engine') and scheduler.execution_engine:
            if hasattr(scheduler.execution_engine, 'broker_adapter'):
                adapter = scheduler.execution_engine.broker_adapter
                if adapter and adapter.is_connected():
                    account_info = adapter.get_account_info()
                    if account_info:
                        cs.update_account(
                            total_assets=account_info.get('total_assets', 0),
                            cash=account_info.get('cash', 0),
                            market_value=account_info.get('market_value', 0)
                        )
    except Exception as e:
        logger.debug(f"[盘前计划] 资本状态刷新失败: {e}")

    exposure_context = {}
    try:
        from app.risk.capital_safety import get_capital_safety_system
        positions = scheduler.position_store.all()
        total_long = sum(p.size * p.avg_entry_price for p in positions if 'LONG' in str(p.direction))
        total_short = sum(p.size * p.avg_entry_price for p in positions if 'SHORT' in str(p.direction))
        cs = get_capital_safety_system()
        # SSOT: 从配置获取基准资金，禁止硬编码
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            default_equity = float(cfg.get_dotted("capital.fixed_amount", 82_000))  # SSOT: YAML=82000
        except Exception:
            default_equity = 500_000
        equity = cs._state.current_equity if cs._state else default_equity
        exposure_context = {
            "total_long_exposure": total_long,
            "total_short_exposure": total_short,
            "long_pct": (total_long / equity * 100) if equity > 0 else 0,
            "short_pct": (total_short / equity * 100) if equity > 0 else 0,
            "total_exposure_pct": ((total_long + total_short) / equity * 100) if equity > 0 else 0,
            "current_positions_count": len(positions),
            "max_positions": scheduler.MAX_POSITIONS if hasattr(scheduler, 'MAX_POSITIONS') else 5,
        }
    except Exception as e:
        logger.debug(f"[盘前计划] 敞口计算失败: {e}")

    earnings_exit_plan = []
    try:
        from app.data.earnings_calendar import EarningsCalendar
        ec = EarningsCalendar()
        this_week = ec.get_this_week_earnings()
        today_str = datetime.now(ET).date()
        positions = scheduler.position_store.all()
        for date_str, earnings in this_week.items():
            earn_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if earn_date <= today_str + timedelta(days=2):
                for e in earnings:
                    sym = e.get('symbol', '')
                    for pos in positions:
                        if pos.symbol.endswith(sym) or sym.endswith(pos.symbol.split('.')[-1]):
                            earnings_exit_plan.append({
                                "symbol": pos.symbol,
                                "action": "EXIT_BEFORE_EARNINGS",
                                "deadline": date_str,
                                "reason": f"{sym} 财报将于 {date_str} 发布",
                                "priority": "P0" if earn_date <= today_str + timedelta(days=1) else "P1"
                            })
    except Exception as e:
        logger.debug(f"[盘前计划] 财报退出计划失败: {e}")

    # ── 催化事件检测 (RFC-Catalyst 2026-05-27) ──
    # 在辩论之前扫描新闻催化, verdicts 注入 debate_context
    catalyst_verdicts = []
    try:
        from app.catalyst.detector import run_premarket_catalyst_scan
        from app.catalyst.reverse_catalyst import get_reverse_verdicts
        
        # 盘前扫描过夜新闻
        catalyst_verdicts = run_premarket_catalyst_scan(scheduler)
        
        # 合并盘中反向催化积累的结论
        reverse_verdicts = get_reverse_verdicts()
        catalyst_verdicts.extend(reverse_verdicts)
        
        if catalyst_verdicts:
            logger.info("[盘前计划] 🧪 催化事件: %d 个 (盘前 %d + 反向 %d)",
                        len(catalyst_verdicts),
                        len(catalyst_verdicts) - len(reverse_verdicts),
                        len(reverse_verdicts))
    except Exception as e:
        logger.debug(f"[盘前计划] 催化检测失败 (不影响主流程): {e}")

    # ── 盘前技术扫描 (Step 3: PreMarket Scanner) ──
    # 扫描 watchlist 日线技术指标 + 板块轮动 + 经济事件
    premarket_scan_result = None
    try:
        from app.us_pipeline.premarket_scanner import USPreMarketScanner
        from app.data.economic_calendar import EconomicCalendar
        from app.data.market_data_router import MarketDataRouter
        
        logger.info("[盘前计划] 🔍 运行盘前技术扫描...")
        
        # 初始化组件
        data_router = MarketDataRouter()
        economic_calendar = EconomicCalendar()
        scanner = USPreMarketScanner(data_router, economic_calendar)
        
        # 获取 watchlist
        watchlist = []
        try:
            from app.utils.watchlist_manager import get_stock_codes
            watchlist = get_stock_codes()
        except Exception:
            # fallback: 从现有持仓和常见标的构建
            positions = scheduler.position_store.all()
            watchlist = list({p.symbol.split('.')[-1] for p in positions})
            watchlist.extend(["SPY", "QQQ", "IWM"])
            watchlist = list(set(watchlist))[:20]
        
        # 运行完整扫描
        if watchlist:
            premarket_scan_result = scanner.run_full_scan(watchlist)
            
            # 生成 LLM 可读文本
            scan_text = scanner.format_for_llm(premarket_scan_result)
            
            logger.info(
                f"[盘前计划] ✅ 盘前扫描完成: "
                f"{len(premarket_scan_result.get('targets', {}))} 标的, "
                f"{len(premarket_scan_result.get('economic_events', []))} 经济事件, "
                f"{len(premarket_scan_result.get('no_trade_windows', []))} 禁止交易窗口"
            )
        else:
            logger.warning("[盘前计划] watchlist 为空，跳过盘前扫描")
    except Exception as e:
        logger.warning(f"[盘前计划] 盘前扫描失败 (降级, 不影响主流程): {e}")

    # ── 构建 DebatePlanner 上下文 ──
    debate_context = _build_debate_context(scheduler, exposure_context, earnings_exit_plan)
    
    # 注入催化结论到辩论上下文
    if catalyst_verdicts:
        debate_context["catalyst_events"] = [v.to_dict() for v in catalyst_verdicts]
    
    # 注入盘前扫描结果到辩论上下文
    if premarket_scan_result:
        debate_context["premarket_scan"] = premarket_scan_result
        debate_context["technical_indicators"] = {
            sym: data.get("technical_summary", "")
            for sym, data in premarket_scan_result.get("targets", {}).items()
        }
        debate_context["sector_rotation"] = premarket_scan_result.get("sector_rotation", {})
        debate_context["economic_events"] = premarket_scan_result.get("economic_events", [])
        debate_context["no_trade_windows"] = premarket_scan_result.get("no_trade_windows", [])

    # ── Level 0: Factor Pipeline (量化预筛 → LLM验证 → 确定性排序) ──
    plan = None
    try:
        from app.plan.pipeline import run_pipeline as run_factor_pipeline
        logger.info("[盘前计划] Level 0: 尝试 Factor Pipeline (量化预筛→LLM验证)...")
        plan = run_factor_pipeline()
        if plan and plan.get("targets"):
            logger.info(
                f"[盘前计划] ✅ Factor Pipeline 成功: "
                f"{len(plan['targets'])} targets, "
                f"regime={plan.get('market_regime')}, "
                f"total={plan.get('pipeline_stats',{}).get('total_sec',0)}s"
            )
        else:
            plan = None  # Empty targets = fall through
            logger.warning("[盘前计划] Factor Pipeline 返回空 targets (降级)")
    except Exception as e:
        logger.warning(f"[盘前计划] Factor Pipeline 失败 (降级): {e}")

    # ── Level 1: 双 Agent 辩论 ──
    if not plan:
        try:
            from app.agents.debate.debate_planner import get_debate_planner
            planner = get_debate_planner(agent_bridge=getattr(scheduler, 'agent_bridge', None))
            plan = planner.generate_premarket_plan(debate_context)
            
            if plan:
                logger.info("[盘前计划] ✅ 双 Agent 辩论成功")
        except Exception as e:
            logger.warning(f"[盘前计划] 双 Agent 辩论失败 (降级): {e}")

    # ── Level 2: Fallback (仅 Hermes 单 Agent) ──
    if not plan:
        logger.warning("[盘前计划] Level 2: 降级到 Hermes 单 Agent")
        try:
            from app.agents.debate.debate_planner import get_debate_planner
            planner = get_debate_planner(agent_bridge=getattr(scheduler, 'agent_bridge', None))
            plan = planner.generate_fallback_plan(debate_context)
        except Exception as e:
            logger.warning(f"[盘前计划] Hermes 单 Agent 失败 (降级): {e}")

    # ── Level 3: 规则引擎兜底 ──
    if not plan:
        logger.warning("[盘前计划] Level 3: 规则引擎兜底")
        try:
            from app.agents.debate.debate_planner import get_debate_planner
            planner = get_debate_planner()
            plan = planner.generate_rule_based_plan(debate_context)
        except Exception as e:
            logger.error(f"[盘前计划] 规则引擎兜底失败: {e}")

    # ── 保存计划 ──
    if plan:
        # P0 安全网 (2026-05-04): Arbiter 返回空 targets 时，直接调 Hermes 生成降级标的
        if not plan.get("targets"):
            logger.warning("[盘前计划] Arbiter 返回空 targets，触发降级: 直调 Hermes")
            try:
                from app.agents.debate.agent_roles import HERMES_PREMARKET_PROMPT_ROUND1
                ctx = planner._build_context(debate_context)
                prompt = HERMES_PREMARKET_PROMPT_ROUND1.format(**ctx)
                system_prompt = (
                    "你是 FTSeek-US 美股盘前策略师。核心基准数据不可用，处于降级模式。\n"
                    "请推荐 2-3 只最可靠的标的 (仅限技术面确认过的)，标注为试仓模式，止损收紧。\n"
                    "输出严格 JSON，targets 不能为空。"
                )
                bridge = getattr(scheduler, 'agent_bridge', None)
                if bridge:
                    hermes_degraded = bridge._call_llm_direct(
                        prompt=prompt, system_prompt=system_prompt,
                        round_num="degraded", model=os.getenv("HERMES_DEBATE_MODEL", "qwen3.6-plus"),
                    )
                    if hermes_degraded and hermes_degraded.get("targets"):
                        plan["targets"] = hermes_degraded["targets"]
                        for t in plan["targets"]:
                            t["urgency"] = "LOW"
                            t["rationale"] = t.get("rationale", "") + " [降级试仓]"
                        plan["risk_warnings"] = plan.get("risk_warnings", []) + [
                            "⚠️ Arbiter 原判空仓 (数据缺失), 降级为试仓模式, 仓位减半"
                        ]
                        if plan.get("market_regime") == "NEUTRAL":
                            plan["market_regime"] = "DEGRADED"
                        plan["max_positions"] = 2
                        plan["debate_summary"] = plan.get("debate_summary", "") + " (降级: 数据缺失, 试仓模式)"
                        logger.info(f"[盘前计划] 降级 Hermes 已生成 {len(hermes_degraded['targets'])} 个试仓标的")
                    else:
                        logger.warning("[盘前计划] 降级 Hermes 也未返回标的, 使用 watchlist 兜底")
                        plan = _build_watchlist_fallback(plan, scheduler)
                else:
                    logger.warning("[盘前计划] AgentBridge 不可用, 使用 watchlist 兜底")
                    plan = _build_watchlist_fallback(plan, scheduler)
            except Exception as e:
                logger.warning(f"[盘前计划] 降级失败, 使用 watchlist 兜底: {e}")
                plan = _build_watchlist_fallback(plan, scheduler)

        # Task #23 (2026-05-27): Entry Zone 合理性校验 — 保存前检查
        # 防止 LLM 幻觉 entry_zone 偏离当前价，偏离 >5% 警告并打标记
        try:
            _price_cal = (debate_context or {}).get("price_calibration", {}) or {}
            if plan.get("targets") and _price_cal:
                for _target in plan["targets"]:
                    _sym = _target.get("symbol", "")
                    _entry_zone = _target.get("entry_zone", []) or []
                    if not _sym or len(_entry_zone) != 2:
                        continue
                    # 兼容 US.AAPL 与 AAPL 两种形式
                    _cal = _price_cal.get(_sym) or _price_cal.get(f"US.{_sym}") or _price_cal.get(_sym.replace("US.", ""))
                    if not _cal:
                        continue
                    _current = float(_cal.get("current_price", 0) or 0)
                    if _current <= 0:
                        continue
                    try:
                        _ez_lo = float(_entry_zone[0])
                        _ez_hi = float(_entry_zone[1])
                    except (TypeError, ValueError):
                        continue
                    _center = (_ez_lo + _ez_hi) / 2
                    _deviation = abs(_center - _current) / _current
                    if _deviation > 0.05:  # >5% 偏离
                        logger.warning(
                            f"[盘前计划] ⚠️ {_sym} entry_zone 中心 ${_center:.2f} "
                            f"偏离当前价 ${_current:.2f} 达 {_deviation:.1%} (>5%)"
                        )
                        _target["_price_warning"] = f"entry_zone偏离当前价{_deviation:.1%}"
                        _target["_current_price_ref"] = _current
        except Exception as _ez_e:
            logger.debug(f"[盘前计划] entry_zone 校验异常 (不阻断保存): {_ez_e}")

        # === 深度分析阶段 (Task #28, 2026-06-16) ===
        # 在 targets 排序完成后、写入 JSON 前，为每个 target 并行生成深度多空辩论分析
        # 失败降级: 不影响 plan 生成，targets 保持原样 (无 deep_analysis 字段)
        try:
            from app.plan.deep_analysis_engine import generate_all_deep_analyses

            final_targets = plan.get("targets", []) or []
            if final_targets:
                # 1. 组装宏观环境 (VIX / SPY 趋势 / market_regime)
                _md_ctx = (debate_context or {}).get("market_data", {}) or {}
                _vix_val = _md_ctx.get("vix", 20.0)
                _spy_trend = "flat"
                try:
                    if hasattr(scheduler, 'market_data') and scheduler.market_data:
                        _spy_ctx = scheduler.market_data.get_spy_context() or {}
                        _spy_chg = _spy_ctx.get("spx_change_percent", 0) or 0
                        if _spy_chg > 0.01:
                            _spy_trend = "up"
                        elif _spy_chg < -0.01:
                            _spy_trend = "down"
                except Exception as _se:
                    logger.debug(f"[盘前计划·深度分析] SPY 趋势获取失败: {_se}")

                macro_context = {
                    "vix": _vix_val,
                    "spy_trend": _spy_trend,
                    "market_regime": plan.get("market_regime", "NEUTRAL"),
                }

                # 2. 当前持仓列表 (用于判定 position_held)
                _positions_ctx = (debate_context or {}).get("positions", []) or []
                current_positions = [
                    p.get("symbol", "") for p in _positions_ctx if isinstance(p, dict)
                ]

                # 3. 注入 current_price (复用已取的 price_calibration, 避免重复 fetch)
                _price_cal_da = (debate_context or {}).get("price_calibration", {}) or {}
                for _t in final_targets:
                    if not isinstance(_t, dict):
                        continue
                    _sym = _t.get("symbol", "")
                    if not _sym or _t.get("current_price"):
                        continue
                    _cal = (_price_cal_da.get(_sym)
                            or _price_cal_da.get(f"US.{_sym}")
                            or _price_cal_da.get(_sym.replace("US.", "")))
                    if _cal and _cal.get("current_price"):
                        _t["current_price"] = _cal["current_price"]

                # 4. 同步执行异步并行任务 (run_premarket_planning 本身是 sync)
                logger.info(
                    f"[盘前计划] 🧠 启动深度分析: {len(final_targets)} targets (并行)"
                )
                _da_loop = asyncio.new_event_loop()
                try:
                    _da_results = _da_loop.run_until_complete(
                        generate_all_deep_analyses(
                            targets=final_targets,
                            macro=macro_context,
                            positions=current_positions,
                        )
                    )
                finally:
                    _da_loop.close()

                # 5. 回写 deep_analysis 到对应 target
                _da_success = 0
                for _idx, _target in enumerate(final_targets):
                    if not isinstance(_target, dict):
                        continue
                    if _idx < len(_da_results) and _da_results[_idx]:
                        _da_payload = _da_results[_idx].get("deep_analysis")
                        if _da_payload:
                            _target["deep_analysis"] = _da_payload
                            _da_success += 1

                logger.info(
                    f"[盘前计划] ✅ 深度分析完成: {_da_success} / {len(final_targets)} targets"
                )
        except Exception as _da_e:
            logger.warning(f"[盘前计划] 深度分析阶段失败 (降级, 不影响主流程): {_da_e}")
        # === 深度分析阶段 结束 ===

        # === Step 4: Plan Validator (校验 + 自动修正) ===
        # 在保存前用盘前扫描数据校验 LLM 输出，修正不合理的入场/止损/止盈
        try:
            if premarket_scan_result and plan.get("targets"):
                from app.us_pipeline.plan_validator import PlanValidator
                
                validator = PlanValidator(
                    premarket_scanner=None,  # 不需要 scanner 实例，只需扫描结果
                    economic_calendar=None,
                )
                
                # 校验 + 自动修正
                validated_plan, validation_reports = validator.validate_plan(
                    plan, scan_result=premarket_scan_result
                )
                
                if validation_reports:
                    reject_count = sum(1 for r in validation_reports if r["severity"] == "REJECT")
                    fix_count = sum(1 for r in validation_reports if r["severity"] == "FIXED")
                    flag_count = sum(1 for r in validation_reports if r["severity"] == "FLAG")
                    
                    # 移除被 REJECT 的 targets
                    if reject_count > 0:
                        original_count = len(plan["targets"])
                        plan["targets"] = [
                            t for t in plan["targets"]
                            if t.get("status") != "REJECTED"
                        ]
                        rejected_count = original_count - len(plan["targets"])
                        logger.info(
                            f"[PlanValidator] 🚫 移除 {rejected_count} 个被拒绝的 targets"
                        )
                    
                    # 注入校验报告到计划 (供 Dashboard 展示)
                    plan["_validation_reports"] = validation_reports
                    plan["_validation_summary"] = {
                        "rejects": reject_count,
                        "fixes": fix_count,
                        "flags": flag_count,
                    }
                    
                    logger.info(
                        f"[PlanValidator] ✅ 校验完成: "
                        f"REJECT={reject_count}, FIX={fix_count}, FLAG={flag_count}"
                    )
                else:
                    logger.info("[PlanValidator] ✅ 所有 targets 校验通过")
        except Exception as _pv_e:
            logger.warning(f"[PlanValidator] 校验失败 (降级, 不影响保存): {_pv_e}")
        # === Plan Validator 结束 ===

        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        saved = mgr.save(plan)
        if saved:
            loaded = mgr.load(mgr.plan_file)
            if loaded:
                # 只在保存和加载都成功后设置幂等标志
                scheduler._premarket_plan_executed_today = True
                scheduler._premarket_plan_date = today_et
                logger.info("[盘前计划] ✅ 作战计划已生成并加载")

                # Phase 1: Obsidian 双写 — 美股盘前计划
                try:
                    from app.core.obsidian_writer import get_obsidian_writer
                    from app.core.paths import get_obsidian_vault
                    writer = get_obsidian_writer(vault_path=str(get_obsidian_vault()))
                    writer.write_daily_premarket({
                        'date': str(today_et),
                        'market': 'US',
                        'targets': [t.get('symbol', '') for t in plan.get('targets', [])],
                        'restricted': [t.get('symbol', '') for t in plan.get('restricted', [])],
                        'overall_bias': plan.get('market_bias', plan.get('market_regime', '中性')),
                        'regime': plan.get('market_regime', 'unknown'),
                        'generated_at': plan.get('generated_at', datetime.now().isoformat()),
                    })
                    logger.info("[盘前计划] 📓 Obsidian 盘前计划已写入")
                except Exception as _e:
                    logger.debug(f"[盘前计划] Obsidian 写入失败: {_e}")

                # 通知 (增强版: 包含详细标的信息)
                try:
                    if hasattr(scheduler, 'notifier') and scheduler.notifier:
                        targets = plan.get('targets', [])
                        restricted = plan.get('restricted', [])
                        
                        # 构建详细通知内容
                        msg_lines = [
                            f"🇺🇸 **FTSeek-US 盘前作战计划** ({today_et})",
                            "",
                            f"📊 **市场阶段**: {plan.get('market_regime', 'NEUTRAL')}",
                            f"📝 **辩论模式**: {plan.get('arbiter_mode', 'LLM Arbiter')}",
                        ]
                        
                        if plan.get('debate_summary'):
                            msg_lines.append(f"💬 **摘要**: {plan.get('debate_summary')}")
                        
                        msg_lines.append("")
                        
                        if targets:
                            msg_lines.append("🎯 **目标标的:**")
                            for t in targets[:5]:  # 最多显示5个
                                sym = t.get('symbol', '?')
                                direction = t.get('direction', '?')
                                conf = t.get('confidence', 0)
                                rationale = (t.get('rationale', '') or '')[:30]
                                urgency = t.get('urgency', '')
                                flag = t.get('flag', '')
                                
                                dir_icon = "🟢" if direction == "BUY" else "🔴" if direction == "SELL" else "⚪"
                                msg_lines.append(f"- {dir_icon} **{sym}** {direction} (conf={conf:.2f}) {urgency}")
                                if rationale:
                                    msg_lines.append(f"  → {rationale}")
                                if flag:
                                    msg_lines.append(f"  ⚠️ {flag}")
                            if len(targets) > 5:
                                msg_lines.append(f"- ... 以及另外 {len(targets)-5} 个标的")
                        
                        msg_lines.append("")
                        
                        if restricted:
                            msg_lines.append("🚫 **限制标的:**")
                            restricted_reasons = plan.get('restricted_reasons', {})
                            for sym in restricted[:5]:
                                reason = restricted_reasons.get(sym, '')
                                msg_lines.append(f"- 🚫 **{sym}** {reason}")
                            if len(restricted) > 5:
                                msg_lines.append(f"- ... 以及另外 {len(restricted)-5} 个标的")
                        
                        msg_lines.append("")
                        msg_lines.append("---")
                        msg_lines.append(f"⚡ 目标: {len(targets)} | 限制: {len(restricted)} | 模式: {plan.get('arbiter_mode', 'standard')}")
                        
                        scheduler.notifier.send_text("\n".join(msg_lines))
                        logger.info(f"[盘前计划] ✅ 详细通知已发送 ({len(targets)} targets, {len(restricted)} restricted)")
                except Exception as e:
                    logger.debug(f"[盘前计划] 通知失败: {e}")

                # === 盘前推荐系统集成 (Task #40, 2026-05-28) ===
                # 在作战计划保存+加载成功后, 合成可读性更强的推荐 + 价格校准 + 推送
                # 推荐子系统任何异常不影响主流程
                try:
                    from app.core.config_container import get_config
                    _rec_cfg = get_config()
                    if _rec_cfg.get_dotted("recommendations.enabled", False):
                        from app.recommendations.synthesizer import RecommendationSynthesizer
                        from app.recommendations.price_calibrator import PriceCalibrator
                        from app.recommendations.push_trigger import PremarketPushTrigger

                        logger.info("[Recommendations] 开始生成盘前推荐...")

                        # 1. 合成推荐 (本函数内可用: plan / debate_context)
                        # 当前 voting_results / debate_summary 在本函数上下文不可直接获取,
                        # 传空字典; synthesizer 会从 battle_plan 提取 debate_summary 等信息。
                        _synth = RecommendationSynthesizer()
                        _recommendations = _synth.synthesize(
                            battle_plan=plan,
                            voting_results={},
                            debate_summary={"summary": plan.get("debate_summary", "")} if isinstance(plan.get("debate_summary"), str) else (plan.get("debate_summary") or {}),
                            market_data=None,  # 校准器会获取实时数据
                        )

                        # 2. 价格校准 (复用已计算的 price_calibration, 避免重复 fetch)
                        if _rec_cfg.get_dotted("recommendations.price_calibration.enabled", True):
                            _price_cal = (debate_context or {}).get("price_calibration", {}) or {}
                            # 如果 debate_context 无预取行情, 注入 scheduler 的 MarketDataRouter
                            # (避免 PriceCalibrator 创建裸 MarketDataRouter 导致数据源缺失)
                            _cal_router = None
                            if not _price_cal and hasattr(scheduler, 'market_data'):
                                _cal_router = scheduler.market_data
                            _calibrator = PriceCalibrator(router=_cal_router)
                            _recommendations = _calibrator.calibrate(_recommendations, market_data=_price_cal or None)

                        # 2.5 热点发现 (watchlist 外的盘前异动 + 新闻催化)
                        try:
                            from app.recommendations.hot_discovery import HotDiscovery
                            _hot = HotDiscovery()
                            # 修复: news_summary 在 run_premarket_planning 作用域未定义 (NameError)
                            # 使用 scheduler 上缓存的盘前新闻摘要
                            _news_text = getattr(scheduler, '_premarket_news_summary', None)
                            if not isinstance(_news_text, str):
                                _news_text = (debate_context or {}).get("overnight_news", None)
                            _hot_picks = _hot.discover(
                                battle_plan=plan,
                                news_summary_text=_news_text,
                            )
                            if _hot_picks:
                                _recommendations["hot_picks"] = _hot_picks
                                logger.info(
                                    f"[Recommendations] 🔥 热点发现: {len(_hot_picks)} 只 "
                                    f"({', '.join(p['symbol'] for p in _hot_picks[:3])})"
                                )
                        except Exception as _hot_e:
                            logger.warning(f"[Recommendations] 热点发现异常（不影响主流程）: {_hot_e}")

                        # 2.7 信号质量验证 (基于历史交易数据硬规则)
                        try:
                            from app.recommendations.signal_validator import SignalValidator
                            _validator = SignalValidator()
                            _regime = _recommendations.get("market_regime", "UNKNOWN")
                            _recs_list = _recommendations.get("recommendations") or []
                            if _recs_list:
                                _approved, _blocked = _validator.validate_batch(_recs_list, _regime)
                                _recommendations["recommendations"] = _approved
                                if _blocked:
                                    _recommendations["blocked_recommendations"] = [
                                        {"symbol": b.get("symbol"), "reason": b.get("validation", {}).get("reason", "")}
                                        for b in _blocked
                                    ]
                                _recommendations["pattern_summary"] = _validator.get_pattern_summary()
                                logger.info(
                                    f"[Recommendations] ✅ 信号验证: "
                                    f"{len(_approved)} 通过 / {len(_blocked)} 拦截"
                                )
                        except Exception as _val_e:
                            logger.warning(f"[Recommendations] 信号验证异常（不影响主流程）: {_val_e}")

                        # 3. 准确率验证 + 存储 (闭环反馈)
                        try:
                            from app.recommendations.accuracy_tracker import AccuracyTracker
                            _tracker = AccuracyTracker()

                            # 3a. 验证昨日推荐 (注入滚动准确率)
                            _yesterday_report = _tracker.verify_yesterday()
                            if _yesterday_report:
                                _rolling = _tracker.get_rolling_accuracy()
                                if _rolling:
                                    _recommendations["rolling_accuracy"] = _rolling
                                    logger.info(
                                        f"[Recommendations] 📈 滚动准确率: "
                                        f"{_rolling.get('accuracy_pct', 0):.0f}% "
                                        f"({_rolling.get('correct',0)}/{_rolling.get('total_tracked',0)})"
                                    )

                            # 3b. 存储今日推荐 (供明日验证)
                            _tracker.store_recommendations(_recommendations)
                        except Exception as _acc_e:
                            logger.warning(f"[Recommendations] 准确率追踪异常（不影响主流程）: {_acc_e}")

                        # 4. 推送
                        _trigger = PremarketPushTrigger()
                        _sent = _trigger.send_final(_recommendations)
                        if _sent:
                            logger.info("[Recommendations] ✅ 盘前推荐已推送")
                        elif _trigger._already_pushed("final", str(_recommendations.get("date") or datetime.now().strftime("%Y-%m-%d"))):
                            logger.info("[Recommendations] 盘前推荐今日已推送 (幂等跳过)")
                        else:
                            logger.warning("[Recommendations] 盘前推荐推送失败")
                    else:
                        logger.debug("[Recommendations] recommendations.enabled=false, 跳过")
                except Exception as _rec_e:
                    logger.warning(f"[Recommendations] 推荐系统异常（不影响主流程）: {_rec_e}")
                # === 盘前推荐系统集成 结束 ===
            else:
                logger.warning("[盘前计划] ⚠️ 计划保存成功但加载失败，下次将重试")
        else:
            logger.warning("[盘前计划] ⚠️ 计划保存失败，下次将重试")


def run_open_checker(scheduler):
    """
    美股开盘确认器 (每日一次, 约 09:15-09:29 ET)
    验证作战计划中的目标是否符合执行条件。
    在 PRE_MARKET → TRADING 过渡窗口执行，确保 CANCELLED 标的不进入信号扫描。
    """
    from app.core.scheduler_triggers import ET
    now_et = datetime.now(ET)
    today_et = now_et.date()

    # 日期级幂等: 新的一天自动重置 flag
    if getattr(scheduler, '_open_checker_date', None) != today_et:
        scheduler._open_checker_executed_today = False

    if getattr(scheduler, '_open_checker_executed_today', False):
        return

    # 时间窗口: 09:15-09:29 ET (开盘前15分钟，含补救窗口)
    if not (now_et.hour == 9 and 15 <= now_et.minute <= 29):
        return

    logger.info("[开盘确认] 🔍 开始执行美股开盘确认...")

    try:
        from app.us_pipeline.open_checker import USOpenChecker
        checker = USOpenChecker()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(checker.run_check())
        finally:
            loop.close()

        scheduler._open_checker_executed_today = True
        scheduler._open_checker_date = today_et

        # 刷新 BattlePlanManager 缓存，使 check_trade 读到最新状态
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        mgr.load(checker.plan_path)

        # 汇总日志: 哪些标的被过滤
        plan = mgr._plan if hasattr(mgr, '_plan') else {}
        targets = plan.get('targets', [])
        cancelled = [t for t in targets if t.get('open_check_status') == 'CANCELLED']
        valid = [t for t in targets if t.get('open_check_status') == 'VALID']
        logger.info(
            f"[开盘确认] ✅ 完成 | "
            f"有效={len(valid)} 取消={len(cancelled)} | "
            f"被过滤: {[t['symbol'] for t in cancelled] if cancelled else '无'}"
        )
    except Exception as e:
        logger.error(f"[开盘确认] ❌ 执行失败: {e}", exc_info=True)


def run_open_price_refresh(scheduler):
    """
    开盘价刷新 (每日一次, 09:30:00-09:31:00 ET)

    美股开盘价由 NASDAQ/NYSE Opening Cross 集合竞价决定 (09:28-09:30)。
    盘前 ECN 报价 (OpenChecker 9:20 取的) 可能偏离真实开盘价。

    本函数在开盘后30秒内用真实市价刷新 confirmed_entry_price,
    并按原 SL/TP 距离重算绝对价位, 确保盈亏比不变。
    """
    from app.core.scheduler_triggers import ET
    now_et = datetime.now(ET)
    today_et = now_et.date()

    # 日期级幂等
    if getattr(scheduler, '_price_refresh_date', None) != today_et:
        scheduler._price_refresh_executed_today = False

    if getattr(scheduler, '_price_refresh_executed_today', False):
        return

    # 时间窗口: 09:30:00 - 09:31:00 ET (开盘后第1分钟)
    if not (now_et.hour == 9 and 30 <= now_et.minute <= 30):
        return

    logger.info("[开盘价刷新] 🔄 用真实开盘价刷新作战计划...")

    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        plan = mgr._plan
        if not plan:
            # 尝试加载
            import os
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            plan_path = os.path.join(project_root, 'data', 'daily_battle_plan_us.json')
            mgr.load(plan_path)
            plan = mgr._plan
        if not plan or not plan.get('targets'):
            return

        router = scheduler.market_data
        updated = 0
        for target in plan.get('targets', []):
            if target.get('open_check_status') != 'VALID':
                continue
            symbol = target.get('symbol', '')
            if not symbol:
                continue

            # 获取真实开盘价
            quote = router.get_quote(symbol)
            if not quote:
                continue
            real_price = float(quote.get('price', 0) or 0)
            if real_price <= 0:
                continue

            # 计算原始 SL/TP 距离
            old_confirmed = float(target.get('confirmed_entry_price', 0) or 0)
            old_sl = float(target.get('stop_loss', 0) or 0)
            old_tp = float(target.get('take_profit', 0) or 0)

            if old_confirmed > 0:
                sl_distance = old_confirmed - old_sl   # LONG: 正数, SHORT: 负数
                tp_distance = old_tp - old_confirmed
            else:
                # 没有盘前价格, 用 entry_zone 中点
                ez = target.get('entry_zone', [])
                if len(ez) == 2:
                    mid = (ez[0] + ez[1]) / 2.0
                    sl_distance = mid - old_sl if old_sl else mid * 0.02
                    tp_distance = old_tp - mid if old_tp else mid * 0.04
                else:
                    sl_distance = real_price * 0.02
                    tp_distance = real_price * 0.04

            # 用真实价格重算
            target['confirmed_entry_price'] = round(real_price, 2)
            target['stop_loss'] = round(real_price - sl_distance, 2)
            target['take_profit'] = round(real_price + tp_distance, 2)
            target['open_price'] = round(real_price, 2)

            logger.info(
                f"[开盘价刷新] {symbol}: "
                f"盘前={old_confirmed} → 开盘={real_price} | "
                f"SL={target['stop_loss']} TP={target['take_profit']}"
            )
            updated += 1

        if updated > 0:
            # 保存更新后的计划
            import json, os
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            plan_path = os.path.join(project_root, 'data', 'daily_battle_plan_us.json')
            with open(plan_path, 'w', encoding='utf-8') as f:
                json.dump(plan, f, indent=2, ensure_ascii=False)

            # 刷新 BattlePlanManager 缓存
            mgr.load(plan_path)

        scheduler._price_refresh_executed_today = True
        scheduler._price_refresh_date = today_et
        logger.info(f"[开盘价刷新] ✅ 刷新 {updated} 个目标")

    except Exception as e:
        logger.error(f"[开盘价刷新] ❌ 失败: {e}", exc_info=True)
# ============================================================================

def _deduplicate_strings(items: list) -> list:
    """语义去重: 使用 Jaccard similarity 移除意思重复的条目"""
    if not items:
        return []
    
    def _char_tokens(s: str) -> set:
        import re
        cn_chars = set(re.findall(r'[\u4e00-\u9fff]', s))
        en_words = set(re.findall(r'[A-Z]{2,}|[a-z]{3,}|\d+', s, re.IGNORECASE))
        return cn_chars | en_words
    
    result = []
    for item in items:
        if not item:
            continue
        item_tokens = _char_tokens(item)
        if not item_tokens:
            result.append(item)
            continue
        
        is_dup = False
        for i, existing in enumerate(result):
            existing_tokens = _char_tokens(existing)
            if not existing_tokens:
                continue
            overlap = len(item_tokens & existing_tokens)
            union = len(item_tokens | existing_tokens)
            if union > 0 and overlap / union > 0.4:
                if len(item) > len(existing):
                    result[i] = item
                is_dup = True
                break
        
        if not is_dup:
            result.append(item)
    
    return result


def _merge_agent_plan_into_battle_plan(agent_plan: Dict):
    """将 Agent 生成的盘前计划合并到 USBattlePlanManager"""
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        
        mgr = USBattlePlanManager()
        existing = mgr.get_plan() or {}
        
        agent_targets = agent_plan.get("targets", [])
        existing_targets = existing.get("targets", [])
        
        existing_symbols = {t.get("symbol") for t in existing_targets if isinstance(t, dict)}
        new_targets = []
        for t in agent_targets:
            if isinstance(t, dict) and t.get("symbol") not in existing_symbols:
                new_targets.append(t)
        
        merged_targets = existing_targets + new_targets
        
        existing_restricted = set(existing.get("restricted", []))
        agent_restricted = set(agent_plan.get("restricted", []))
        merged_restricted = list(existing_restricted | agent_restricted)
        
        raw_adjustments = agent_plan.get("strategy_adjustments", existing.get("strategy_adjustments", []))
        raw_warnings = agent_plan.get("risk_warnings", existing.get("risk_warnings", []))
        
        strategy_adjustments = _deduplicate_strings(raw_adjustments)
        risk_warnings = _deduplicate_strings(raw_warnings)
        
        merged_plan = {
            **existing,
            "targets": merged_targets,
            "restricted": merged_restricted,
            "agent_enhanced": True,
            "agent_market_regime": agent_plan.get("market_regime", existing.get("market_regime", "NEUTRAL")),
            "strategy_adjustments": strategy_adjustments,
            "risk_warnings": risk_warnings,
            "agent_notes": agent_plan.get("notes", existing.get("notes", "")),
            "updated_at": datetime.now().isoformat(),
        }
        
        mgr.save(merged_plan)
        mgr.load(mgr.plan_path)
        
        logger.info(
            f"[Agent] 计划合并完成 | "
            f"新增 {len(new_targets)} 个标的, "
            f"总限制 {len(merged_restricted)} 个"
        )
    except Exception as e:
        logger.warning(f"[Agent] 计划合并失败 (不影响主流程): {e}")
