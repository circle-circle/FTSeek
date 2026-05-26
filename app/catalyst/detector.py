"""
催化事件检测调度器 — 盘前/盘后入口

职责:
- 盘前 (8:30-9:25 ET): 全量扫描过夜新闻 → 分类 → 辩论 → 返回 verdicts
- 盘后 (16:05-18:00 ET): 委托 postmarket_review.py 回看

集成点:
- 盘前: 在 run_premarket_planning 之前调用, verdicts 注入 _build_debate_context
- 盘中: 不介入 (仅 reverse_catalyst 存储结论, 不触发交易)
"""

import logging
import time
from typing import Dict, List, Optional

from app.catalyst.models import CatalystEvent, CatalystVerdict
from app.catalyst.event_classifier import CatalystClassifier
from app.catalyst.catalyst_debate import CatalystDebate

logger = logging.getLogger("ftseek_us.catalyst.detector")


def run_premarket_catalyst_scan(scheduler) -> List[CatalystVerdict]:
    """盘前催化扫描 — 在 run_premarket_planning 之前调用
    
    流程:
    1. 从 NewsPipelineV2 获取过夜新闻 (已去重+过滤)
    2. CatalystClassifier 分类 → CatalystEvent 列表
    3. 对 severity >= HIGH 的事件启动 CatalystDebate
    4. 返回 CatalystVerdict 列表
    
    Args:
        scheduler: TradingScheduler 实例
    
    Returns:
        CatalystVerdict 列表 (空列表 = 无催化事件, 不影响盘前计划)
    """
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        cat_cfg = cfg.get_dotted("catalyst", {}) or {}
        
        if not cat_cfg.get("enabled", False):
            return []
        
        premarket_cfg = cat_cfg.get("premarket", {})
        scan_hours = int(premarket_cfg.get("scan_news_hours", 12))
        min_severity = premarket_cfg.get("min_severity", "HIGH")
        debate_timeout = int(premarket_cfg.get("debate_timeout_seconds", 180))
        
        # 获取板块映射
        sector_mapping = cat_cfg.get("sector_mapping", {})
        
    except Exception as e:
        logger.warning("[Catalyst] 配置加载失败 (跳过): %s", e)
        return []
    
    # Step 1: 获取新闻
    news_items = _get_recent_news(scheduler, hours=scan_hours)
    if not news_items:
        logger.info("[Catalyst] 无过夜新闻, 跳过催化扫描")
        return []
    
    logger.info("[Catalyst] 盘前扫描: %d 条新闻", len(news_items))
    
    # Step 2: 分类
    classifier = CatalystClassifier(sector_mapping=sector_mapping)
    events = classifier.classify(news_items, use_llm=True)
    
    if not events:
        logger.info("[Catalyst] 无催化事件检出")
        return []
    
    logger.info("[Catalyst] 检出 %d 个催化事件", len(events))
    
    # Step 3: 过滤 + 辩论
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    min_sev_level = severity_order.get(min_severity, 1)
    
    debate_events = [
        e for e in events
        if severity_order.get(e.severity, 2) <= min_sev_level
    ]
    
    if not debate_events:
        logger.info("[Catalyst] 无 HIGH+ 事件需辩论 (%d 个 MEDIUM 跳过)", len(events))
        # MEDIUM 事件仍生成 fallback verdict
        verdicts = []
        debate_engine = CatalystDebate(market_context=_get_market_context(scheduler))
        for event in events:
            verdicts.append(debate_engine._fallback_verdict(event, []))
        return verdicts
    
    # Step 4: 辩论
    market_ctx = _get_market_context(scheduler)
    debate_engine = CatalystDebate(market_context=market_ctx)
    
    verdicts = []
    for event in debate_events:
        try:
            verdict = debate_engine.full_debate(event, timeout_seconds=debate_timeout)
            verdicts.append(verdict)
        except Exception as e:
            logger.warning("[Catalyst] 辩论失败 %s: %s", event.headline[:30], e)
            verdicts.append(debate_engine._fallback_verdict(event, []))
    
    # MEDIUM 事件用 fallback
    medium_events = [
        e for e in events
        if severity_order.get(e.severity, 2) > min_sev_level
    ]
    for event in medium_events:
        verdicts.append(debate_engine._fallback_verdict(event, []))
    
    logger.info("[Catalyst] 盘前催化完成: %d 个 verdict (HIGH+ %d, MEDIUM %d)",
                len(verdicts), len(debate_events), len(medium_events))
    
    return verdicts


def _get_recent_news(scheduler, hours: int = 12) -> list:
    """从 NewsPipelineV2 获取近期新闻"""
    try:
        pipeline = getattr(scheduler, 'news_pipeline_v2', None)
        if pipeline and hasattr(pipeline, 'get_recent_items'):
            cutoff = time.time() - hours * 3600
            items = pipeline.get_recent_items(max_age_minutes=hours * 60)
            return items if items else []
    except Exception as e:
        logger.debug("[Catalyst] NewsPipelineV2 获取失败: %s", e)
    
    # Fallback: 从 NewsNow DB 直接读取
    try:
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent.parent / "data" / "rsshub_us" / "news_us.db"
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cutoff = time.time() - hours * 3600
            rows = conn.execute(
                "SELECT id, title, summary, source, source_name, "
                "published_at, fetched_at, symbols, language "
                "FROM us_news WHERE fetched_at >= ? ORDER BY fetched_at DESC LIMIT 100",
                (cutoff,)
            ).fetchall()
            conn.close()
            
            from app.news_v2.models import UnifiedNewsItemV2
            items = []
            for row in rows:
                item = UnifiedNewsItemV2(
                    id=row[0],
                    title=row[1] or "",
                    content=row[2] or row[1] or "",
                    source=row[3] or "unknown",
                    source_name=row[4] or "unknown",
                    timestamp=float(row[6] or 0),
                )
                items.append(item)
            return items
    except Exception as e:
        logger.debug("[Catalyst] NewsNow DB 回退失败: %s", e)
    
    return []


def _get_market_context(scheduler) -> Dict:
    """获取市场背景上下文"""
    ctx = {"vix": "N/A", "spy_trend": "N/A", "regime": "NEUTRAL"}
    
    try:
        if hasattr(scheduler, 'vix_monitor') and scheduler.vix_monitor:
            vix = scheduler.vix_monitor.get_current_vix()
            if vix:
                ctx["vix"] = vix
    except Exception:
        pass
    
    try:
        if hasattr(scheduler, 'market_data') and scheduler.market_data:
            spy_ctx = scheduler.market_data.get_spy_context()
            if spy_ctx:
                ctx["spy_trend"] = spy_ctx.get("trend", "N/A")
                ctx["regime"] = spy_ctx.get("regime", "NEUTRAL")
    except Exception:
        pass
    
    return ctx
