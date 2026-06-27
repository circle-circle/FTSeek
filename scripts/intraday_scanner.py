#!/usr/bin/env python3
"""盘中异动扫描器 (Intraday Scanner)

每 15 分钟扫描一次，检测:
1. 突发新闻 (Finnhub market_news / company_news)
2. 价格异动 (涨跌 > 3%)
3. 对命中标的做快速因子评分

命中后:
- 注入 daily_battle_plan_us.json (discovery_source=INTRADAY_ALERT)
- 飞书推送紧急通知

API 预算: Finnhub 免费 60 次/分钟, 本脚本每次 ~15-25 次
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env.us")

import urllib3
urllib3.disable_warnings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("intraday_scanner")

PLAN_PATH = Path(__file__).parent.parent / "data" / "daily_battle_plan_us.json"
EXCLUDE_PREFIXES = ("US.SPY", "US.QQQ", "US.IWM", "US.DIA", "US.XL")
ALERT_THRESHOLD_SCORE = 0.55   # 最低推送阈值
PRICE_MOVE_THRESHOLD = 3.0     # 价格异动阈值 (%)
NEWS_LOOKBACK_HOURS = 2        # 新闻回看小时
MAX_INTRADAY_TARGETS = 5       # 最多注入 5 个盘中发现


def is_market_hours() -> bool:
    """检查是否在美股交易时间 (9:15 AM - 4:05 PM ET, 含盘前)"""
    # ET = UTC-4 (EDT) or UTC-5 (EST). 简化: UTC-4
    et_now = datetime.now(timezone.utc) - timedelta(hours=4)
    weekday = et_now.weekday()  # 0=Mon, 6=Sun
    if weekday >= 5:  # 周末
        return False
    hour_min = et_now.hour * 100 + et_now.minute
    return 915 <= hour_min <= 1605


def get_futu_quote_ctx():
    """获取 Futu quote context (替代 Finnhub client)"""
    try:
        from app.web_dashboard.manual_order_service import ManualOrderService
        svc = ManualOrderService()
        if svc.connect():
            return svc
        logger.error("[Futu] connect failed")
        return None
    except Exception as e:
        logger.error(f"[Futu] error: {e}")
        return None


def futu_disconnect(svc):
    """安全断开 Futu 连接"""
    try:
        if svc:
            svc.disconnect()
    except Exception:
        pass


def scan_price_moves_futu(svc, pool: list) -> dict:
    """用 Futu get_market_snapshot 批量扫描价格异动 (1次调用扫全部)

    Returns: {symbol: {change_pct, price, prev_close, pre_change_rate, volume_ratio}}
    """
    from futu import RET_OK
    moves = {}

    # 批量查询 (Futu snapshot 支持一次查多只)
    batch_size = 20
    for i in range(0, len(pool), batch_size):
        batch = pool[i:i + batch_size]
        try:
            ret, data = svc._quote_ctx.get_market_snapshot(batch)
            if ret != RET_OK or data.empty:
                continue
            for _, row in data.iterrows():
                code = row.get("code", "")
                prev_close = row.get("prev_close_price", 0)
                last = row.get("last_price", 0)
                if not prev_close or prev_close <= 0 or not last:
                    continue
                change = round((last - prev_close) / prev_close * 100, 2)
                pre_change = row.get("pre_change_rate", 0)
                if abs(change) >= PRICE_MOVE_THRESHOLD or abs(pre_change) >= PRICE_MOVE_THRESHOLD:
                    moves[code] = {
                        "change_pct": change,
                        "price": last,
                        "prev_close": prev_close,
                        "pre_change_rate": pre_change,
                        "volume_ratio": row.get("volume_ratio", 1.0),
                    }
        except Exception as e:
            logger.debug(f"[PriceScan] batch error: {e}")

    logger.info(f"[PriceScan] Futu snapshot: {len(pool)} stocks scanned, {len(moves)} movers (>={PRICE_MOVE_THRESHOLD}%)")
    return moves


def quick_factor_score_futu(svc, code: str, price_info: dict) -> float:
    """快速因子评分 (用 Futu snapshot, 无需 Finnhub)

    因子:
    - 日内动量: 涨跌幅度 vs 前收
    - 量比: 放量确认
    - 买卖盘: bid_ask_ratio
    """
    from futu import RET_OK
    scores = []

    change = price_info.get("change_pct", 0)
    vol_ratio = price_info.get("volume_ratio", 1.0)

    # Factor 1: 日内动量
    if 2 <= change <= 7:
        scores.append(0.9)
    elif 7 < change <= 12:
        scores.append(0.6)
    elif change > 12:
        scores.append(0.2)
    elif -3 <= change < 2:
        scores.append(0.5)
    elif -7 <= change < -3:
        scores.append(0.4)
    else:
        scores.append(0.1)

    # Factor 2: 量比 (放量 = 高关注度)
    if vol_ratio >= 2.0:
        scores.append(0.9)
    elif vol_ratio >= 1.5:
        scores.append(0.7)
    elif vol_ratio >= 1.0:
        scores.append(0.5)
    else:
        scores.append(0.3)

    # Factor 3: 盘前异动 (如果有盘前数据)
    pre_change = price_info.get("pre_change_rate", 0)
    if abs(pre_change) >= 2:
        scores.append(0.8)  # 盘前已有异动
    elif abs(pre_change) >= 1:
        scores.append(0.6)
    else:
        scores.append(0.4)

    return round(sum(scores) / len(scores), 3) if scores else 0.0


def get_finnhub_client():
    """获取 Finnhub 客户端"""
    from app.data.premarket_enrichment import _get_finnhub_client
    return _get_finnhub_client()


def get_scan_pool() -> list:
    """获取扫描池: dragonhead + daily_active_pool"""
    pool = set()

    # Dragonhead US stocks
    try:
        from app.utils.dragonhead_pool import US_DRAGON_HEADS
        for sector_data in US_DRAGON_HEADS.values():
            if isinstance(sector_data, dict):
                for stock in sector_data.get("stocks", []):
                    code = stock.get("code", "")
                    if code.startswith("US."):
                        pool.add(code)
    except Exception as e:
        logger.debug(f"dragonhead import error: {e}")

    # Daily active pool
    try:
        active_path = Path(__file__).parent.parent / "data" / "daily_active_pool.json"
        if active_path.exists():
            data = json.loads(active_path.read_text())
            for e in data.get("entries", []):
                code = e.get("code", "")
                if code.startswith("US."):
                    pool.add(code)
    except Exception:
        pass

    # Remove ETFs (handled by ORB)
    pool = {c for c in pool if not any(c.startswith(p) for p in EXCLUDE_PREFIXES)}
    return sorted(pool)


def get_existing_targets() -> set:
    """获取当前 plan 中已有的 targets (避免重复)"""
    if not PLAN_PATH.exists():
        return set()
    try:
        plan = json.loads(PLAN_PATH.read_text())
        return {t.get("symbol", "") for t in plan.get("targets", [])}
    except Exception:
        return set()


def scan_price_moves(client, pool: list) -> dict:
    """扫描价格异动 (Finnhub quote, ~0.05s/call)
    
    Returns: {symbol: {change_pct, price, prev_close}}
    """
    moves = {}
    api_calls = 0
    t0 = time.time()

    for code in pool:
        if api_calls >= 35 or (time.time() - t0 > 20):
            break
        sym = code.replace("US.", "")
        try:
            q = client.quote(sym)
            api_calls += 1
            pc = q.get("pc", 0)
            c = q.get("c", 0)
            if pc and c and pc > 0:
                change = (c - pc) / pc * 100
                if abs(change) >= PRICE_MOVE_THRESHOLD:
                    moves[code] = {
                        "change_pct": round(change, 2),
                        "price": c,
                        "prev_close": pc,
                    }
        except Exception:
            pass
        time.sleep(0.03)

    logger.info(f"[PriceScan] {api_calls} calls, {len(moves)} movers (>={PRICE_MOVE_THRESHOLD}%)")
    return moves


def scan_breaking_news(client) -> dict:
    """扫描突发新闻 (Finnhub market_news)
    
    Returns: {symbol: {headline, datetime, sentiment}}
    """
    news_hits = {}
    now_ts = time.time()
    lookback_sec = NEWS_LOOKBACK_HOURS * 3600

    try:
        news = client.general_news("general", min_id=0)
        if not news:
            return news_hits

        for article in news[:50]:
            article_ts = article.get("datetime", 0)
            if article_ts and (now_ts - article_ts) > lookback_sec:
                continue  # 太旧

            related = article.get("related", "")
            if not related:
                continue

            headline = article.get("headline", "")
            sentiment = article.get("sentiment", "")
            source = article.get("source", "")

            for sym in related.split(","):
                sym = sym.strip().upper()
                if not sym or len(sym) > 5:
                    continue
                code = f"US.{sym}"
                if any(code.startswith(p) for p in EXCLUDE_PREFIXES):
                    continue

                if code not in news_hits:
                    news_hits[code] = {
                        "headline": headline[:80],
                        "datetime": article_ts,
                        "sentiment": sentiment,
                        "source": source,
                    }

    except Exception as e:
        logger.debug(f"[NewsScan] error: {e}")

    logger.info(f"[NewsScan] {len(news_hits)} symbols with recent news")
    return news_hits


def quick_factor_score(client, code: str, price_info: dict = None) -> float:
    """快速因子评分 (无 K 线, 仅用 Finnhub quote 数据)
    
    因子:
    - 日内涨幅 vs 前收 (momentum): 3-8% = 高分, >8% = 追高风险
    - 买卖价差 (liquidity proxy): h-l spread
    - 成交量 (从 quote 的 t 字段): 正常 = 中分
    
    Returns: 0-1 composite score
    """
    sym = code.replace("US.", "")
    scores = []

    try:
        q = client.quote(sym)
        c = q.get("c", 0)  # current
        pc = q.get("pc", 0)  # prev close
        h = q.get("h", 0)  # day high
        l = q.get("l", 0)  # day low
        o = q.get("o", 0)  # open

        if not all([c, pc, h, l]) or pc <= 0:
            return 0.0

        # Factor 1: 日内动量 (change from prev close)
        change = (c - pc) / pc * 100
        if 2 <= change <= 7:
            scores.append(0.9)  # 强势但不过热
        elif 7 < change <= 12:
            scores.append(0.6)  # 偏热, 可能追高
        elif change > 12:
            scores.append(0.2)  # 过热, 高风险
        elif -3 <= change < 2:
            scores.append(0.5)  # 震荡
        elif -7 <= change < -3:
            scores.append(0.4)  # 回调可能是机会
        else:
            scores.append(0.1)  # 大跌, 可能有问题

        # Factor 2: 日内强度 (close vs high-low range)
        day_range = h - l
        if day_range > 0:
            intraday_strength = (c - l) / day_range
            scores.append(intraday_strength)  # 0-1, 越接近 high 越强
        else:
            scores.append(0.5)

        # Factor 3: 开盘跳空 (open vs prev_close)
        if o and pc > 0:
            gap = (o - pc) / pc * 100
            if 1 <= gap <= 5:
                scores.append(0.8)  # 正向跳空
            elif gap > 5:
                scores.append(0.4)  # 跳空过大
            elif -1 < gap < 1:
                scores.append(0.5)  # 平开
            else:
                scores.append(0.3)  # 负跳空

    except Exception:
        return 0.0

    return round(sum(scores) / len(scores), 3) if scores else 0.0


def build_intraday_target(code: str, score: float, move_info: dict, news_info: dict) -> dict:
    """构建盘中发现的 target 条目"""
    price = move_info.get("price", 0)
    change = move_info.get("change_pct", 0)
    direction = "LONG" if change > 0 else "SHORT"

    # Entry zone: 当前价 ±1%
    entry_low = round(price * 0.99, 2)
    entry_high = round(price * 1.01, 2)

    # Stop loss: 2.5% from entry
    stop_loss = round(price * 0.975, 2) if direction == "LONG" else round(price * 1.025, 2)

    # Take profit: 4% from entry (R:R ≈ 1.6)
    take_profit = round(price * 1.04, 2) if direction == "LONG" else round(price * 0.96, 2)

    # Catalyst info
    catalyst = {"type": "NONE", "headline": ""}
    if news_info:
        catalyst = {
            "type": "NEWS",
            "headline": news_info.get("headline", ""),
            "source": news_info.get("source", ""),
            "sentiment": news_info.get("sentiment", ""),
        }

    urgency = "HIGH" if score >= 0.7 else "MEDIUM" if score >= ALERT_THRESHOLD_SCORE else "LOW"

    return {
        "symbol": code,
        "direction": direction,
        "entry_zone": [entry_low, entry_high],
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "urgency": urgency,
        "verdict": "CONDITIONAL",
        "confidence": min(70, int(score * 100)),
        "factor_score": score,
        "composite_rank": round(score * 0.6 + 0.3, 3),  # 盘中简化排名
        "discovery_source": "INTRADAY_ALERT",
        "discovery_time": datetime.now(timezone.utc).isoformat(),
        "catalyst": catalyst,
        "rationale": f"盘中异动: {change:+.1f}% | 因子{score:.0%} | {news_info.get('headline','无新闻')[:50]}",
        "execution_rules": {"type": "INTRADAY", "max_position_pct": 3},
        "factor_validation": {},
        "data_cited": [],
        "model_used": "quick_factor",
    }


def inject_targets(new_targets: list) -> int:
    """将盘中发现注入 battle plan JSON, 返回注入数量"""
    if not PLAN_PATH.exists():
        logger.warning("Plan file not found, cannot inject")
        return 0

    plan = json.loads(PLAN_PATH.read_text())
    existing = {t.get("symbol", "") for t in plan.get("targets", [])}

    injected = 0
    for t in new_targets:
        sym = t["symbol"]
        if sym in existing:
            continue
        if injected >= MAX_INTRADAY_TARGETS:
            break

        plan["targets"].append(t)
        existing.add(sym)
        injected += 1
        logger.info(f"[Inject] {sym} score={t['factor_score']:.3f} → plan")

    if injected > 0:
        plan["pipeline_stats"] = plan.get("pipeline_stats", {})
        plan["pipeline_stats"]["intraday_injected"] = injected
        plan["pipeline_stats"]["last_intraday_scan"] = datetime.now(timezone.utc).isoformat()
        PLAN_PATH.write_text(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
        logger.info(f"[Inject] Saved {injected} targets to plan")

    return injected


def push_alert(targets: list):
    """飞书推送盘中告警"""
    if not targets:
        return

    try:
        from app.notifier.feishu import notify_top_opportunities
        pool_meta = {
            "core_pool_size": 0,
            "event_discovered": len(targets),
            "final_pool_size": len(targets),
        }
        ok = notify_top_opportunities(targets, pool_meta)
        if ok:
            logger.info(f"[Alert] 📢 Feishu push: {len(targets)} intraday targets")
    except Exception as e:
        logger.warning(f"[Alert] Feishu error: {e}")


def check_plan_health(svc) -> list:
    """方案2: 计划健康检查 — 对每个target检查当前价vs入场区间/止损

    Returns: list of dicts with status changes that need alerting
    """
    from futu import RET_OK
    if not PLAN_PATH.exists():
        return []

    plan = json.loads(PLAN_PATH.read_text())
    targets = plan.get("targets", [])
    if not targets:
        return []

    symbols = [t.get("symbol", "") for t in targets if t.get("symbol")]
    if not symbols:
        return []

    alerts = []

    # Batch snapshot
    batch_size = 20
    prices = {}
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            ret, data = svc._quote_ctx.get_market_snapshot(batch)
            if ret != RET_OK or data.empty:
                continue
            for _, row in data.iterrows():
                code = row.get("code", "")
                prices[code] = {
                    "last": row.get("last_price", 0),
                    "prev_close": row.get("prev_close_price", 0),
                }
        except Exception:
            pass

    for t in targets:
        sym = t.get("symbol", "")
        direction = t.get("direction", "LONG")
        entry = t.get("entry_zone", [0, 0])
        sl = t.get("stop_loss", 0)
        tp = t.get("take_profit", 0)
        cur = prices.get(sym, {}).get("last", 0)
        old_status = t.get("_intraday_status", "")

        if not cur or not entry or len(entry) < 2:
            continue

        # Determine status
        if direction == "LONG":
            if cur <= sl and sl > 0:
                new_status = "BREACHED_STOP"
            elif cur >= tp and tp > 0:
                new_status = "HIT_TARGET"
            elif entry[0] <= cur <= entry[1]:
                new_status = "IN_ENTRY_ZONE"
            elif cur > entry[1]:
                new_status = "ABOVE_ENTRY"
            else:
                new_status = "BELOW_ENTRY"
        else:  # SHORT
            if cur >= sl and sl > 0:
                new_status = "BREACHED_STOP"
            elif cur <= tp and tp > 0:
                new_status = "HIT_TARGET"
            elif entry[0] <= cur <= entry[1]:
                new_status = "IN_ENTRY_ZONE"
            elif cur < entry[0]:
                new_status = "BELOW_ENTRY"
            else:
                new_status = "ABOVE_ENTRY"

        # Status change → alert
        if new_status != old_status and new_status in ("IN_ENTRY_ZONE", "BREACHED_STOP", "HIT_TARGET"):
            alerts.append({
                "symbol": sym,
                "direction": direction,
                "status": new_status,
                "price": cur,
                "entry_zone": entry,
                "stop_loss": sl,
                "take_profit": tp,
            })

        # Update target status in plan
        t["_intraday_status"] = new_status
        t["_intraday_price"] = cur
        t["_intraday_checked_at"] = datetime.now(timezone.utc).isoformat()

    # Save updated statuses
    plan["targets"] = targets
    plan["_last_health_check"] = datetime.now(timezone.utc).isoformat()
    with open(PLAN_PATH, "w") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2, default=str)

    return alerts


def push_health_alerts(alerts: list):
    """方案6: 飞书推送计划状态变化"""
    if not alerts:
        return

    from app.notifier.feishu import send_text

    lines = ["⚡ 计划状态变化\n"]
    for a in alerts:
        sym = a["symbol"].replace("US.", "")
        status = a["status"]
        direction = a["direction"]
        price = a["price"]
        entry = a["entry_zone"]

        icon = {"IN_ENTRY_ZONE": "🎯", "BREACHED_STOP": "🛑", "HIT_TARGET": "✅"}.get(status, "⚠️")
        dir_icon = "🔴" if direction == "SHORT" else "🟢"

        if status == "IN_ENTRY_ZONE":
            lines.append(f"{icon} {dir_icon} {sym} 进入入场区间 ${entry[0]}-{entry[1]} (当前${price:.2f})")
        elif status == "BREACHED_STOP":
            lines.append(f"{icon} {dir_icon} {sym} 突破止损 ${a['stop_loss']} (当前${price:.2f})")
        elif status == "HIT_TARGET":
            lines.append(f"{icon} {dir_icon} {sym} 触达目标 ${a['take_profit']} (当前${price:.2f})")

    msg = "\n".join(lines)
    try:
        send_text(msg)
        logger.info(f"[HealthAlert] 📢 Pushed {len(alerts)} status changes")
    except Exception as e:
        logger.warning(f"[HealthAlert] Feishu error: {e}")


def run_scan():
    """主扫描流程 (Futu主数据源, Finnhub降级)"""
    if not is_market_hours():
        logger.info("[Scan] Outside market hours, skip")
        return

    logger.info("[Scan] === Intraday scan start ===")
    t0 = time.time()

    # 方案1: 优先用 Futu, 降级用 Finnhub
    use_futu = True
    svc = get_futu_quote_ctx()
    if not svc:
        logger.warning("[Scan] Futu unavailable, falling back to Finnhub")
        use_futu = False
        client = get_finnhub_client()
        if not client:
            logger.error("[Scan] No data source (Futu + Finnhub both failed)")
            return

    # 1. 获取扫描池 & 已有 targets
    pool = get_scan_pool()
    existing = get_existing_targets()
    logger.info(f"[Scan] Pool: {len(pool)} stocks, existing targets: {len(existing)}")

    # 2. 价格扫描
    if use_futu:
        price_moves = scan_price_moves_futu(svc, pool)
        news_hits = {}  # Futu无新闻API, 降级为空 (等Finnhub key修复)
    else:
        price_moves = scan_price_moves(client, pool)
        news_hits = scan_breaking_news(client)

    # 3. 合并: 价格异动 OR 突发新闻 → 候选
    candidates = set()
    candidates.update(price_moves.keys())
    candidates.update(news_hits.keys())
    candidates -= existing  # 去掉已有的
    logger.info(f"[Scan] Candidates: {len(candidates)} (after dedup)")

    # 4. 快速因子评分
    scored = []
    for code in candidates:
        move_info = price_moves.get(code, {"change_pct": 0, "price": 0, "prev_close": 0, "volume_ratio": 1.0, "pre_change_rate": 0})
        news_info = news_hits.get(code, {})

        if use_futu:
            score = quick_factor_score_futu(svc, code, move_info)
        else:
            score = quick_factor_score(client, code, move_info)

        if score >= ALERT_THRESHOLD_SCORE:
            target = build_intraday_target(code, score, move_info, news_info)
            scored.append((score, target))

    scored.sort(key=lambda x: x[0], reverse=True)
    logger.info(f"[Scan] Scored: {len(scored)} above threshold")

    # 5. 注入 + 推送
    if scored:
        new_targets = [t for _, t in scored[:MAX_INTRADAY_TARGETS]]
        injected = inject_targets(new_targets)
        if injected > 0:
            urgent = [t for t in new_targets if t.get("urgency") == "HIGH"]
            if urgent:
                push_alert(urgent)
            else:
                push_alert(new_targets[:3])

    # 6. 方案2: 计划健康检查 (Futu only)
    if use_futu:
        alerts = check_plan_health(svc)
        if alerts:
            push_health_alerts(alerts)

    if use_futu:
        futu_disconnect(svc)

    elapsed = time.time() - t0
    logger.info(f"[Scan] === Done in {elapsed:.1f}s ===")


if __name__ == "__main__":
    run_scan()
