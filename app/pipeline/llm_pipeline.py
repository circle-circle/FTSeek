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


def _get_et_time_str(fmt: str = '%Y-%m-%d %H:%M') -> str:
    """获取 ET 时间字符串 (避免在各函数内重复 import ET)"""
    try:
        from app.utils.timezone import now_et
        return now_et().strftime(fmt)
    except Exception:
        return datetime.now().strftime(fmt)


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
        f"⏰ {_get_et_time_str()} ET\n\n"
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

# TOMBSTONE 2026-07-31 (DEB-fullchain-cleanup 第5轮终裁): 全仓零调用 (仅 app/pipeline/__init__
#   re-export), 函数体是 NewsNowUSFetcher 与 app.risk.market_regime_filter 的唯一实例化点 — 三者同死。
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
                "max_position_pct": risk_cfg.get('risk.position.max_per_symbol', 0.08),  # [DEB-fullchain #19] 回退值与 yaml SSOT 同步
                "max_total_position": risk_cfg.get('risk.position.max_total', 0.80),
                "max_daily_strategies": risk_cfg.get('risk.trading.max_daily_trades', 500),
                "cooldown_seconds": risk_cfg.get('decision.signal_timing.cooldown_seconds', 120),
                # 2026-07-26 扫尾: 原键 risk.blacklist 不存在恒空, 真键 global_blacklist
                "blacklist": risk_cfg.get('risk.global_blacklist', [])
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
        
        if strategy.direction == "SHORT":
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
                    if quote:
                        _bid = float(quote.get("bid", 0) or 0)
                        _ask = float(quote.get("ask", 0) or 0)
                        price = round((_bid + _ask) / 2.0, 2) if _bid > 0 and _ask > 0 else float(quote.get("price", 0) or 0)
                    else:
                        price = 0
                except Exception:
                    price = 0
                router = get_short_strategy_router()
                context = {"price": price, "vix": 20.0}
                exec_result = router.execute_signal(signal_obj, context)
                
                if exec_result.get("success"):
                    strategy.status = "executed"
                    executed_count += 1
                else:
                    strategy.status = "pending_options"
            except Exception as e:
                strategy.status = "pending_options"
                logger.error(f"[LLM策略] 执行异常 {strategy.symbol}: {e}")
        elif strategy.direction == "LONG":
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
                    from app.pipeline.order_executor import _close_position, _get_current_price
                    from app.core.models import ExitReason
                    _cp = _get_current_price(scheduler, short_proxy_pos.symbol)
                    if _cp and _cp > 0:
                        _close_position(scheduler, short_proxy_pos, _cp, ExitReason.STRUCTURE_EXIT)
                    else:
                        logger.warning(f"[LLM策略] 无法获取 {short_proxy_pos.symbol} 当前价格, 跳过平仓")
                    strategy.status = "closed_short_proxy"
                    continue
                
                trace_id = f"LLM-{_get_et_time_str('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
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
                    if quote:
                        _bid = float(quote.get("bid", 0) or 0)
                        _ask = float(quote.get("ask", 0) or 0)
                        price = round((_bid + _ask) / 2.0, 2) if _bid > 0 and _ask > 0 else float(quote.get("price", 0) or 0)
                    else:
                        price = 0
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


# run_llm_backtest 已删 (2026-07-26 扫尾): 全仓零调用点, 产出 backtest_snapshots.json
# 只写不读且 04-16 起冻结 (DEB-20260720 已记死开关)。回滚: git revert。


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

    # 2026-07-31 议题⑧ (用户裁"不看"): 默认关停 — 钉钉 LLM 新闻推荐无人看, 纯 LLM 成本
    #   (每日最多 3 次: 盘前/盘后/深夜)。入口单点守卫覆盖 scheduler_core 三个调用点。
    try:
        from app.core.config_container import get_config as _ah_gc
        if not bool(_ah_gc().get_dotted("llm_pushes.after_hours_scan_enabled", False)):
            logger.debug("[LLM推荐] 已关停 (llm_pushes.after_hours_scan_enabled=false), 跳过")
            return
    except Exception:
        pass  # 配置读取失败 → 维持原行为 (不因守卫故障断掉既有链路)

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
        time_str = _get_et_time_str()

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


def _build_signal_quality_context(path=None) -> str:
    """
    构建信号质量历史反馈上下文，注入辩论引擎。

    根因 (2026-06-19): 辩论引擎给出 VALID 但 Deep Research 全部 AVOID，
    因为辩论引擎不知道历史胜率/IC 数据。
    修复: 从 signal_quality/latest.json 加载历史表现，让辩论引擎在做决策时看到真实数据。

    2026-08-22 重写 (用户裁"prompt修改不影响交易链路逻辑, 基于发现修复", DEB-20260822 §九)。
    旧版三处坏损:
      ① 读 confidence_map 但文件键为 confidence_winrate_map → 置信度桶段恒空;
      ② 依赖已删除的 ic_audit_report_2026-06-19.md → fallback 到硬编码 6/19 旧引擎数字;
      ③ 硬编码结论"优先推荐manual方向"每日注入辩论引擎 (误导)。
    新版纪律: 内容全部来自 data/signal_quality/latest.json 实时数据, 无硬编码结论;
    文件缺失/解析失败时输出"反馈不可用", 不编造。胜率旁必列期望值 (桶期望非单调时
    胜率单独呈现有误导, 见 anomalies 自述)。
    回滚: git revert 本函数 (调用方 :982 签名不变)。
    """
    sq_path = Path(path) if path else (
        Path(__file__).resolve().parent.parent.parent / "data" / "signal_quality" / "latest.json")

    if not sq_path.exists():
        return ("【⚠️ 历史信号质量反馈不可用】(data/signal_quality/latest.json 缺失) — "
                "本次判断无历史校准数据可参考, 请按保守口径输出。")

    try:
        with open(sq_path) as f:
            sq = json.load(f)
    except Exception as e:
        logger.warning(f"[signal_quality_context] 解析失败: {e}")
        return ("【⚠️ 历史信号质量反馈不可用】(latest.json 解析失败) — "
                "本次判断无历史校准数据可参考, 请按保守口径输出。")

    lines = [f"【⚠️ 历史信号质量反馈 — 辩论必须参考】(数据截至 {sq.get('as_of', '未知')}, "
             f"近{sq.get('history_days', '?')}天, 已平仓 {sq.get('total_closed_trades', '?')} 笔)\n"]

    # 1. Confidence bucket: 胜率 + 期望值 (键为 confidence_winrate_map, 旧代码错读 confidence_map)
    buckets = (sq.get("confidence_winrate_map") or {}).get("buckets", {})
    if buckets:
        lines.append("置信度桶 vs 实际表现 (胜率 | 期望值/笔 — 期望非单调时置信度不可作权重):")
        for label in ["0-0.3", "0.3-0.5", "0.5-0.7", "0.7-1.0"]:
            b = buckets.get(label, {})
            n = b.get("total", 0)
            if n <= 0:
                continue
            wr = b.get("win_rate", 0)
            exp_pct = b.get("avg_pnl_pct", 0) or 0
            flag = "🔴" if exp_pct < 0 else "🟢"
            lines.append(f"  {flag} conf {label}: 胜率={wr:.1%} | 期望={exp_pct:+.2%} (n={n})")

    # 2. 策略/来源表现 (优先近30日, 无则全史)
    sp = (sq.get("strategy_performance") or {}).get("rolling", {}).get("30d", {}) \
        or (sq.get("strategy_performance") or {}).get("all_time", {})
    if sp:
        lines.append("\n各策略/来源表现:")
        for cat, s in sorted(sp.items(), key=lambda x: -x[1].get("total", 0)):
            n = s.get("total", 0)
            if n < 3:
                continue
            wr = s.get("win_rate", 0)
            pnl = s.get("total_pnl", 0)
            flag = "🔴" if pnl < 0 else "🟢"
            lines.append(f"  {flag} {cat}: 胜率={wr:.1%} PnL=${pnl:+,.0f} (n={n})")

    # 3. 异常 (来自 threshold_recommendations, 系统计自产, 原样透传)
    anomalies = (sq.get("threshold_recommendations") or {}).get("anomalies", [])
    if anomalies:
        lines.append(f"\n⚠️ 异常: {'; '.join(anomalies)}")

    # 收尾 = 使用指令 (非数据结论): 要求差异化理由, 不替模型下方向判断
    lines.append("\n使用要求: 以上为系统实测统计。若本次判断与上方历史模式一致"
                 "（如同桶期望为负仍给同方向），必须在推理中给出差异化理由，否则降级 NO_TRADE。")
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

    # TOMBSTONE 2026-07-31 (DEB 第5轮): 全仓无写者 — 本行恒取默认值 '无隔夜新闻', 死读。
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
            from app.data.market_data_router import get_market_data_router
            _router = get_market_data_router(enable_futu_lv3=True)
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

def run_open_checker(scheduler):
    """
    美股开盘确认器 (每日一次, 约 09:15-09:29 ET)
    验证作战计划中的目标是否符合执行条件。
    在 PRE_MARKET → TRADING 过渡窗口执行，确保 CANCELLED 标的不进入信号扫描。
    """
    # [ADR-012] 旧作战计划轨道 (daily_battle_plan_us.json) 已废弃冻结 (2026-07-13 discovery管线停用)。
    # 开盘 gap 校验 + 钉钉推送默认下线, 避免推送过时标的; 真实轨道为 playbook_instructions。
    # 复活整条旧轨道: feature_flags.legacy_battle_plan_enabled=true + 重启。config 不可用时保持下线。
    try:
        from app.core.config_container import get_config as _gc
        if not _gc().get_dotted("feature_flags.legacy_battle_plan_enabled", False):
            return
    except Exception:
        return

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
            "updated_at": _get_et_time_str('%Y-%m-%dT%H:%M:%S'),
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
