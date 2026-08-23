# TOMBSTONE 2026-07-31 (DEB-fullchain-cleanup 第5轮终裁): 唯一调用方 scripts/post_market_audit.py
#   无 cron; 且 battle plan catalyst_events 键缺失 (生产端 detector 已死) → 即便手动跑也在 :117 早退。
"""
盘后催化回看 — 预测 vs 实际 + 教训提取

流程:
1. 读取今日催化结论 (从 daily_battle_plan_us.json 的 catalyst_events)
2. 获取各标的当日实际涨跌 (从 Futu 行情 / trade_records)
3. 计算命中率: 预测方向 vs 实际方向
4. 对未命中事件, call_llm(feature="catalyst_review") 分析原因
5. 生成教训 → LessonInjector.add_lesson()

调用时机: 盘后归因辩论 cron (c09e8387406b, 04:05 CST = 16:05 ET)
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.catalyst.models import CatalystVerdict

logger = logging.getLogger("ftseek_us.catalyst.postmarket")

# 项目路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PLAN_PATH = PROJECT_ROOT / "data" / "daily_battle_plan_us.json"


def run_postmarket_catalyst_review(scheduler=None) -> Dict:
    """盘后催化回看主入口
    
    Returns:
        回看结果摘要 {"total": N, "hits": N, "misses": N, "accuracy": float, "lessons": [...]}
    """
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        pm_cfg = cfg.get_dotted("catalyst.postmarket", {}) or {}
        if not pm_cfg.get("enabled", False):
            return {"skipped": True, "reason": "postmarket review disabled"}
    except Exception:
        pass
    
    # Step 1: 读取今日催化结论
    verdicts = _load_today_verdicts()
    if not verdicts:
        logger.info("[CatalystReview] 今日无催化事件, 跳过回看")
        return {"total": 0, "skipped": True}
    
    logger.info("[CatalystReview] 今日 %d 个催化事件待回看", len(verdicts))
    
    # Step 2: 获取实际涨跌
    actual_returns = _get_actual_returns(verdicts, scheduler)
    
    # Step 3: 计算命中率
    hits, misses, details = _evaluate_predictions(verdicts, actual_returns)
    
    total = len(verdicts)
    accuracy = len(hits) / total if total > 0 else 0.0
    
    logger.info("[CatalystReview] 命中率: %d/%d (%.0f%%)", len(hits), total, accuracy * 100)
    
    # Step 4: LLM 分析未命中事件
    lessons = []
    generate_lessons = True
    try:
        from app.core.config_container import get_config
        generate_lessons = get_config().get_dotted("catalyst.postmarket.generate_lessons", True)
    except Exception:
        pass
    
    if generate_lessons and misses:
        lessons = _analyze_misses(misses, details)
    
    # Step 5: 写入 lessons DB
    if lessons:
        _inject_lessons(lessons)
    
    # 持久化回看结果
    _save_review_result(verdicts, actual_returns, hits, misses, accuracy, lessons)
    
    # 构建完整结果 (含 details 供通知格式化)
    result = {
        "total": total,
        "hits": len(hits),
        "misses": len(misses),
        "accuracy": accuracy,
        "lessons_count": len(lessons),
        "details": [d for d in (hits + misses)],
        "lessons": lessons,
    }
    
    # ── 推送盘后催化回看报告 ──
    try:
        from app.catalyst.notifier import push_postmarket_report
        push_postmarket_report(result)
    except Exception as e:
        logger.warning("[CatalystReview] 推送失败 (不影响主流程): %s", e)
    
    return result


def _load_today_verdicts() -> List[Dict]:
    """从 battle_plan JSON 读取今日催化事件"""
    try:
        if not PLAN_PATH.exists():
            return []
        
        plan = json.loads(PLAN_PATH.read_text())
        
        # 检查日期
        from datetime import datetime, timezone, timedelta
        plan_date = plan.get("date", "")
        # 催化事件可能存在多个字段
        catalyst_events = plan.get("catalyst_events", [])
        
        if not catalyst_events:
            # 也检查 overnight_news 中嵌入的催化信息 (backwards compat)
            return []
        
        return catalyst_events
        
    except Exception as e:
        logger.warning("[CatalystReview] 读取计划失败: %s", e)
        return []


def _get_actual_returns(verdicts: List[Dict], scheduler=None) -> Dict[str, float]:
    """获取各标的当日实际收益率
    
    Returns:
        {"US.FUTU": -0.085, "US.BABA": -0.032, ...}
    """
    returns = {}
    symbols = set(v.get("symbol", "") for v in verdicts if v.get("symbol"))
    
    # 方式 1: Futu 行情 (优先)
    for symbol in symbols:
        try:
            if scheduler and hasattr(scheduler, 'market_data') and scheduler.market_data:
                ctx = scheduler.market_data.get_symbol_context(symbol)
                if ctx:
                    change = ctx.get("change_pct", 0)
                    if change:
                        returns[symbol] = change
                        continue
        except Exception:
            pass
        
        # 方式 2: trade_records DB
        try:
            import sqlite3
            db_path = PROJECT_ROOT / "data" / "ftseek_us.db"
            if db_path.exists():
                conn = sqlite3.connect(str(db_path))
                # 2026-07-27 修复 (时区批同款): date('now')=UTC 日 vs exit_time=ET/CST 墙钟时间 —
                #   盘后复盘跑在 CST 早晨 (UTC 前一日晚间) 时错位。改 ET 交易日参数。
                from app.utils.timezone import get_trade_date
                row = conn.execute(
                    "SELECT pnl_pct FROM trade_records "
                    "WHERE symbol = ? AND date(exit_time) = ? "
                    "ORDER BY exit_time DESC LIMIT 1",
                    (symbol, get_trade_date())
                ).fetchone()
                conn.close()
                if row and row[0]:
                    returns[symbol] = float(row[0])
                    continue
        except Exception:
            pass
    
    return returns


def _evaluate_predictions(verdicts: List[Dict],
                          actual_returns: Dict[str, float]) -> Tuple[list, list, list]:
    """评估预测 vs 实际
    
    Returns:
        (hits, misses, details)
    """
    hits = []
    misses = []
    details = []
    
    for v in verdicts:
        symbol = v.get("symbol", "")
        predicted_dir = v.get("direction", "AVOID")
        strength = v.get("strength", 0)
        actual_ret = actual_returns.get(symbol)
        
        if actual_ret is None:
            details.append({
                "symbol": symbol, "predicted": predicted_dir,
                "actual": "N/A", "status": "no_data"
            })
            continue
        
        # 判断命中
        if predicted_dir == "AVOID":
            # AVOID: 如果波动 < 2% 算命中 (确实没什么行情)
            is_hit = abs(actual_ret) < 0.02
        elif predicted_dir == "SHORT":
            # SHORT: 实际下跌 > 1% 算命中
            is_hit = actual_ret < -0.01
        elif predicted_dir == "LONG":
            # LONG: 实际上涨 > 1% 算命中
            is_hit = actual_ret > 0.01
        else:
            is_hit = False
        
        detail = {
            "symbol": symbol,
            "predicted": predicted_dir,
            "strength": strength,
            "actual_return": actual_ret,
            "status": "hit" if is_hit else "miss",
            "reasoning": v.get("reasoning", ""),
            "event_type": v.get("event_type", "UNKNOWN"),
        }
        details.append(detail)
        
        if is_hit:
            hits.append(detail)
        else:
            misses.append(detail)
    
    return hits, misses, details


def _analyze_misses(misses: List[Dict], details: List[Dict]) -> List[str]:
    """LLM 分析未命中事件, 生成教训"""
    if not misses:
        return []
    
    lessons = []
    
    for miss in misses[:3]:  # 最多分析3个 (控制成本)
        try:
            from app.llm.model_call import call_llm
            
            prompt = (
                f"以下催化事件预测失败, 请分析原因并给出教训:\n\n"
                f"标的: {miss['symbol']}\n"
                f"预测方向: {miss['predicted']} (strength={miss.get('strength', '?')})\n"
                f"实际收益: {miss.get('actual_return', '?'):.1%}\n"
                f"事件类型: {miss.get('event_type', '?')}\n"
                f"推理: {miss.get('reasoning', 'N/A')[:100]}\n\n"
                f"请回答:\n"
                f"1. 预测失败的主要原因是什么?\n"
                f"2. 下次遇到类似事件应该怎么做?\n"
                f"返回JSON: {{\"root_cause\": \"...\", \"lesson\": \"一句话教训\"}}"
            )
            
            result = call_llm(
                feature="catalyst_review",
                prompt=prompt,
                json_mode=True,
                timeout=30,
            )
            
            if result.success and result.response:
                lesson_text = result.response.get("lesson", "")
                if lesson_text:
                    lessons.append(
                        f"[催化回看] {miss['symbol']} {miss['predicted']}→"
                        f"实际{miss.get('actual_return', 0):+.1%}: {lesson_text}"
                    )
        except Exception as e:
            logger.debug("[CatalystReview] LLM 分析失败: %s", e)
    
    return lessons


def _inject_lessons(lessons: List[str]):
    """将教训写入 LessonInjector"""
    try:
        from app.review.lesson_injector import LessonInjector
        li = LessonInjector()
        for lesson in lessons:
            li.save_lesson(
                category="catalyst_review",
                title=lesson[:80],
                description=lesson,
                impact_score=0.6,
                source="catalyst_postmarket",
            )
        logger.info("[CatalystReview] 注入 %d 条教训到 LessonInjector", len(lessons))
    except Exception as e:
        logger.warning("[CatalystReview] 教训注入失败: %s", e)


def _save_review_result(verdicts, actual_returns, hits, misses, accuracy, lessons):
    """持久化回看结果"""
    try:
        from datetime import datetime
        result_path = PROJECT_ROOT / "data" / "catalyst_review"
        result_path.mkdir(exist_ok=True)

        # ── R-8a (2026-08-23 DEB-20260823): 复盘日期改 ET ──
        # 盘后复盘在 ET 收盘后跑 (ET 16:00+ = CST 次日 04:00+), 原用裸 datetime.now()
        # (宿主 Asia/Shanghai) → ET 交易日 D 的复盘被标成 D+1, 文件名同样错一天。
        try:
            from app.utils.timezone import now_et as _now_et_pm
            _review_dt = _now_et_pm()
        except Exception:
            _review_dt = datetime.now()

        filename = f"review_{_review_dt.strftime('%Y%m%d')}.json"
        filepath = result_path / filename

        data = {
            "date": _review_dt.strftime("%Y-%m-%d"),
            "total_verdicts": len(verdicts),
            "hits": len(hits),
            "misses": len(misses),
            "accuracy": accuracy,
            "details": hits + misses,
            "lessons": lessons,
            "actual_returns": actual_returns,
        }
        
        filepath.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        logger.info("[CatalystReview] 回看结果已保存: %s", filepath)
    except Exception as e:
        logger.debug("[CatalystReview] 保存失败: %s", e)
