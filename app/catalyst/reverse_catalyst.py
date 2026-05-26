"""
反向催化: 行情先行 → 新闻确认 → 快速辩论

当 emergency_interceptor 检测到价格异动时:
1. 搜索该标的相关新闻 (NewsNow + Finnhub, 最近30分钟)
2. 如果找到催化事件 → quick_debate(1轮, 60s)
3. 存储 CatalystVerdict → 供盘前计划/盘后归因使用

设计:
- 不触发交易 (盘中不介入, 用户要求)
- 仅存储结论到 _catalyst_verdicts 列表
- 盘前 run_premarket_planning 时消费这些结论
"""

import logging
import time
from typing import Dict, List, Optional

from app.catalyst.models import CatalystEvent, CatalystVerdict

logger = logging.getLogger("ftseek_us.catalyst.reverse")

# 全局结论存储 (盘中异动 → 盘前消费)
_reverse_catalyst_store: List[CatalystVerdict] = []


def get_reverse_verdicts() -> List[CatalystVerdict]:
    """获取反向催化积累的结论 (盘前调用后清空)"""
    global _reverse_catalyst_store
    verdicts = list(_reverse_catalyst_store)
    _reverse_catalyst_store.clear()
    return verdicts


def on_price_anomaly(symbol: str, change_pct: float,
                     scheduler=None) -> Optional[CatalystVerdict]:
    """价格异动回调 — 由 emergency_interceptor 触发
    
    Args:
        symbol: "US.FUTU" 等
        change_pct: 涨跌幅 (-0.05 = -5%)
        scheduler: TradingScheduler 实例
    
    Returns:
        CatalystVerdict 或 None
    """
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        cat_cfg = cfg.get_dotted("catalyst.reverse_catalyst", {}) or {}
        
        if not cat_cfg.get("enabled", False):
            return None
        
        min_change = float(cat_cfg.get("min_change_pct", 0.03))
        if abs(change_pct) < min_change:
            return None
        
        timeout = int(cat_cfg.get("quick_debate_timeout", 60))
        
    except Exception:
        if abs(change_pct) < 0.03:
            return None
        timeout = 60
    
    logger.info("[ReverseCatalyst] %s 异动 %.1f%%, 搜索新闻...", symbol, change_pct * 100)
    
    # Step 1: 搜索相关新闻
    news_items = _search_news_for_symbol(symbol, scheduler, minutes=30)
    
    if not news_items:
        logger.info("[ReverseCatalyst] %s 未找到相关新闻, 生成纯行情催化", symbol)
        # 无新闻 → 纯行情异动事件
        event = CatalystEvent(
            event_id=f"anomaly_{symbol}_{int(time.time())}",
            event_type="PRICE_ANOMALY",
            severity="HIGH" if abs(change_pct) >= 0.05 else "MEDIUM",
            tickers=[symbol],
            headline=f"{symbol} 价格异动 {change_pct:+.1%}",
            summary=f"无异样新闻, 纯行情异动 {change_pct:+.1%}",
            source="ticker_anomaly",
            source_language="en",
            published_at=time.time(),
        )
    else:
        # 有新闻 → 分类
        from app.catalyst.event_classifier import CatalystClassifier
        
        try:
            from app.core.config_container import get_config
            sector_mapping = get_config().get_dotted("catalyst.sector_mapping", {}) or {}
        except Exception:
            sector_mapping = {}
        
        classifier = CatalystClassifier(sector_mapping=sector_mapping)
        events = classifier.classify(news_items, use_llm=False)  # 快速模式: 无LLM
        
        if not events:
            logger.info("[ReverseCatalyst] %s 新闻无催化事件", symbol)
            return None
        
        event = events[0]  # 取最重要的一个
    
    # Step 2: 快速辩论
    from app.catalyst.catalyst_debate import CatalystDebate
    
    market_ctx = _get_quick_market_context(scheduler)
    debate = CatalystDebate(market_context=market_ctx)
    
    try:
        verdict = debate.quick_debate(event, timeout_seconds=timeout)
    except Exception as e:
        logger.warning("[ReverseCatalyst] 快速辩论失败: %s", e)
        verdict = debate._fallback_verdict(event, [])
    
    # Step 3: 存储 (不触发交易)
    _reverse_catalyst_store.append(verdict)
    
    logger.info(
        "[ReverseCatalyst] %s 结论: %s strength=%d consensus=%.2f",
        symbol, verdict.direction, verdict.strength, verdict.consensus_level
    )
    
    return verdict


def _search_news_for_symbol(symbol: str, scheduler=None,
                            minutes: int = 30) -> list:
    """搜索标的相关新闻 (最近N分钟)"""
    items = []
    
    # 从 NewsPipelineV2 获取
    try:
        pipeline = getattr(scheduler, 'news_pipeline_v2', None)
        if pipeline and hasattr(pipeline, 'get_recent_items'):
            all_items = pipeline.get_recent_items(max_age_minutes=minutes)
            for item in all_items:
                # 检查是否关联到此标的
                if symbol in (item.related_stocks or []):
                    items.append(item)
                elif symbol.replace("US.", "").lower() in item.title.lower():
                    items.append(item)
    except Exception:
        pass
    
    # Fallback: NewsNow DB
    if not items:
        try:
            import sqlite3
            from pathlib import Path
            db_path = Path(__file__).resolve().parent.parent.parent / "data" / "rsshub_us" / "news_us.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                cutoff = time.time() - minutes * 60
                ticker_short = symbol.replace("US.", "")
                rows = conn.execute(
                    "SELECT id, title, summary, source, source_name, "
                    "published_at, fetched_at FROM us_news "
                    "WHERE fetched_at >= ? AND (title LIKE ? OR symbols LIKE ?) "
                    "ORDER BY fetched_at DESC LIMIT 20",
                    (cutoff, f"%{ticker_short}%", f"%{ticker_short}%")
                ).fetchall()
                conn.close()
                
                from app.news_v2.models import UnifiedNewsItemV2
                for row in rows:
                    items.append(UnifiedNewsItemV2(
                        id=row[0], title=row[1] or "", content=row[2] or row[1] or "",
                        source=row[3] or "unknown", source_name=row[4] or "unknown",
                        timestamp=float(row[6] or 0),
                    ))
        except Exception:
            pass
    
    return items


def _get_quick_market_context(scheduler=None) -> Dict:
    """快速获取市场背景 (轻量版)"""
    ctx = {"vix": "N/A", "spy_trend": "N/A", "regime": "NEUTRAL"}
    try:
        if scheduler and hasattr(scheduler, 'vix_monitor') and scheduler.vix_monitor:
            vix = scheduler.vix_monitor.get_current_vix()
            if vix:
                ctx["vix"] = vix
    except Exception:
        pass
    return ctx
