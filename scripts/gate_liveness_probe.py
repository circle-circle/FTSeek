#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
证伪门活性探针 (2026-08-22c, 用户裁"按建议执行")
==================================================
对 intraday_reentry 的 9 道证伪门逐门跑**真实采集路径 + 门侧消费函数**,
报 ok / empty / stale / dead, 落盘 telemetry. 只读, 零交易路径改动.

背景 (为什么需要它): ADR-032 抓出 4 个死门, 2026-08-22b 复查又抓出 1 个
(SEC 门 universe 格式死门) — "provider 返回了数据但门读错键/传错参数 →
恒不触发但无异常" 这类失败**没有系统性检测**, 只能靠人翻代码.
本探针把检测自动化: 门侧消费链跑通了才算 alive, 否则当场暴露.

状态语义:
  ok     = 采集路径通 + 门侧消费函数吃得到合法 schema
  empty  = 路径通但无数据 (可能是合法空: 无财报/无近期8-K/盘外无逐笔)
  stale  = 数据存在但陈旧 (资金流最新记录不是最近交易日)
  dead   = 异常 / schema 不符 / 恒空列表类死门 ← 本探针要抓的东西
  off    = 设计性关断 (social 门: 数据源无极性分数, ADR-032 诚实降级)

输出: data/gate_liveness_latest.json + data/gate_liveness_history.jsonl
cron: 15 7 * * 2-6 (07:15 CST = 19:15 ET, 当日 ET 会话已收完整资金流)
回滚: 删 cron 行 + 本文件 + data/gate_liveness_*
"""
import json
import sys
import traceback
from datetime import timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

LATEST = BASE / "data" / "gate_liveness_latest.json"
HISTORY = BASE / "data" / "gate_liveness_history.jsonl"

SAMPLE_SYMBOLS = ["US.NVDA", "US.AAPL"]  # 高流动性, 所有 provider 都覆盖


def _most_recent_et_trading_day():
    from app.utils.timezone import now_et
    d = now_et().date()
    while d.weekday() >= 5:  # 5=Sat 6=Sun (节假日不识别, 假日次日会误报一天 stale)
        d -= timedelta(days=1)
    return d.isoformat()


def probe_capital_flow(trade_day: str) -> dict:
    """资金流门: 复刻 run_scan 的当日过滤读取, 报最新记录日期覆盖面."""
    path = BASE / "data" / "futu_capital_flow.jsonl"
    if not path.exists():
        return {"status": "dead", "detail": "futu_capital_flow.jsonl 不存在 — 门恒不触发"}
    newest = {}
    try:
        for line in path.read_text().strip().split("\n"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            s = d.get("symbol")
            t = str(d.get("item_time") or "")
            if s and t and t > newest.get(s, ""):
                newest[s] = t
    except Exception as e:
        return {"status": "dead", "detail": f"读取异常: {e}"}
    if not newest:
        return {"status": "dead", "detail": "文件无有效记录"}
    fresh = [s for s, t in newest.items() if t[:10] == trade_day]
    if not fresh:
        oldest = min(t[:10] for t in newest.values())
        return {"status": "stale",
                "detail": f"0/{len(newest)} 标的有最近交易日({trade_day})记录, "
                          f"最新仅至 {max(t[:10] for t in newest.values())} — "
                          f"采集掉线或订阅全换 (旧记录最旧 {oldest})"}
    return {"status": "ok",
            "detail": f"{len(fresh)}/{len(newest)} 标的当日有记录 "
                      f"(订阅上限20, 其余为历史残留, 门按当日过滤不受影响)"}


def probe_futu_ext_gate(sym: str, method: str, consume) -> dict:
    """FutuExtendedProvider 系 (卖空量/内部人): 跑真实调用 + 门侧消费."""
    from app.playbook.intraday_reentry import _futu_ext
    prov = _futu_ext()
    if prov is None:
        return {"status": "dead", "detail": "FutuExtendedProvider 初始化失败 (OpenD 不可达?)"}
    try:
        raw = getattr(prov, method)(sym)
    except Exception as e:
        return {"status": "dead", "detail": f"{method} 异常: {e}"}
    if not isinstance(raw, dict) or raw.get("status") != "ok":
        return {"status": "dead", "detail": f"{method} status!=ok: {str(raw)[:120]}"}
    data = raw.get("data")
    if not data:
        return {"status": "empty", "detail": f"{sym} 无覆盖 (data 空)"}
    try:
        consumed = consume(data)
    except Exception as e:
        return {"status": "dead", "detail": f"门侧消费异常 (schema 不符?): {e}"}
    return {"status": "ok", "detail": f"{sym} → {consumed}"}


def probe_analyst(sym: str) -> dict:
    """分析师门: 真实 periods schema + _derive_analyst_trend 门侧消费."""
    from app.data.analyst_revision_provider import get_analyst_provider
    from app.playbook.intraday_reentry import _derive_analyst_trend
    try:
        rt = get_analyst_provider().get_recommendation_trend(sym)
    except Exception as e:
        return {"status": "dead", "detail": f"get_recommendation_trend 异常: {e}"}
    if not isinstance(rt, dict) or not rt.get("periods"):
        return {"status": "dead", "detail": f"返回非 {periods_schema()} 或为空: {str(rt)[:120]}"}
    n = len(rt["periods"])
    if n < 2:
        return {"status": "empty", "detail": f"periods 仅 {n} 期 (<2, 门按死区返回 None)"}
    trend = _derive_analyst_trend(rt)
    return {"status": "ok", "detail": f"{sym} periods={n} 期, 修订动量={trend} (None=死区内, 合法)"}


def periods_schema():
    return "{periods:[{strong_buy,buy,hold,sell,strong_sell}]}"


def probe_earnings(sym: str) -> dict:
    """财报门: 全量日历源活性 + 行级 symbol/days_until schema + 门侧查找."""
    from app.playbook.intraday_reentry import _earnings_cal, _find_days_to_earnings
    ec = _earnings_cal()
    if ec is None:
        return {"status": "dead", "detail": "EarningsCalendar 初始化失败"}
    try:
        full = ec.get_watchlist_earnings()
        rows = ec.get_watchlist_earnings(watchlist_symbols=[sym])
    except Exception as e:
        return {"status": "dead", "detail": f"get_watchlist_earnings 异常: {e}"}
    if not full:
        return {"status": "dead", "detail": "全量财报日历为空 — 数据源断供"}
    days = _find_days_to_earnings(rows, sym)
    if rows and not any(isinstance(r, dict) and "symbol" in r and "days_until" in r for r in rows):
        return {"status": "dead", "detail": f"行 schema 不含 symbol/days_until: {str(rows[0])[:120]}"}
    return {"status": "ok",
            "detail": f"日历源 {len(full)} 条活跃; {sym} 7日内财报 days_until={days} (None=无, 合法)"}


def probe_finnhub_sentiment(sym: str) -> dict:
    """情绪极端门: provider 活性 (enabled) + 新闻流真实存在 + 分数口径."""
    from app.playbook.intraday_reentry import _finnhub
    fp = _finnhub()
    if fp is None:
        return {"status": "dead", "detail": "FinnhubProvider 初始化失败"}
    if not getattr(fp, "enabled", False):
        return {"status": "dead", "detail": "finnhub disabled (无 API key?) — 门恒吃 0.0"}
    try:
        news = fp.get_company_news(sym, days=2)
        score = fp.get_sentiment_score(sym)
    except Exception as e:
        return {"status": "dead", "detail": f"finnhub 调用异常: {e}"}
    if not news:
        return {"status": "empty", "detail": f"{sym} 近2日无公司新闻 (score 恒 0, 门合法不触发)"}
    return {"status": "ok", "detail": f"{sym} 新闻 {len(news)} 条, 情绪分={score:.2f} (±0.8 死区外才触发)"}


def probe_sec(trade_day: str) -> dict:
    """SEC 8-K/13D-G 门: 源活性 (非空/缓存新) + 门侧消费字段 (items/ticker/form)."""
    from app.data.sec_8k_provider import CACHE_FILE, CACHE_13DG_FILE, fetch_recent_8k, fetch_recent_13dg
    import time
    try:
        rows8k = fetch_recent_8k()
        rowsdg = fetch_recent_13dg()
    except Exception as e:
        return {"status": "dead", "detail": f"EDGAR 拉取异常: {e}"}
    age = (time.time() - CACHE_FILE.stat().st_mtime) if CACHE_FILE.exists() else -1
    if not rows8k and not rowsdg:
        if age < 0 or age > 86400:
            return {"status": "dead",
                    "detail": f"8-K/13D-G 双空且缓存年龄 {age/3600:.1f}h — EDGAR 断供且降级为空 (静默死门风险)"}
        return {"status": "empty", "detail": "近24h 无命中白名单的 8-K (合法空)"}
    schema_ok = all(("items" in r and "ticker" in r) for r in rows8k[:5])
    bearish = {"1.02", "1.03", "2.05", "2.06", "4.02", "5.02"}
    n_bear = sum(1 for r in rows8k if bearish.intersection(r.get("items") or []))
    if rows8k and not schema_ok:
        return {"status": "dead", "detail": f"8-K 行缺 items/ticker 键: {str(rows8k[0])[:120]}"}
    return {"status": "ok",
            "detail": f"8-K {len(rows8k)} 条 (利空item命中 {n_bear}) / 13D-G {len(rowsdg)} 条, "
                      f"缓存 {age/3600:.1f}h 前; 门按裸 ticker 过滤+item码集消费"}


def probe_ticker_sentiment(sym: str) -> dict:
    """逐笔情绪门: LV3 真实调用 + _derive_ticker_direction 门侧消费."""
    from app.data.futu_lv3_provider import get_futu_lv3_provider
    from app.playbook.intraday_reentry import _derive_ticker_direction
    try:
        ts = get_futu_lv3_provider().get_ticker_sentiment(sym, window=30)
    except Exception as e:
        return {"status": "dead", "detail": f"get_ticker_sentiment 异常: {e}"}
    if not isinstance(ts, dict) or "confidence" not in ts or "sentiment" not in ts:
        return {"status": "dead", "detail": f"返回 schema 不含 sentiment/confidence: {str(ts)[:120]}"}
    direction = _derive_ticker_direction(ts)
    if float(ts.get("confidence") or 0) <= 0:
        return {"status": "empty", "detail": f"{sym} 无逐笔数据 (confidence=0, 盘外合法; 盘中=推送断)"}
    return {"status": "ok", "detail": f"{sym} sentiment={ts['sentiment']}, conf={ts['confidence']}, 方向={direction}"}


def run_probe() -> dict:
    trade_day = _most_recent_et_trading_day()
    gates = {"capital_flow": probe_capital_flow(trade_day)}

    def _short_consume(data):
        return f"short_percent={data.get('short_percent')}"

    def _insider_consume(data):
        return f"signal={data.get('signal')}"

    gates["short_squeeze"] = probe_futu_ext_gate(SAMPLE_SYMBOLS[0], "get_daily_short_volume", _short_consume)
    gates["insider_divergence"] = probe_futu_ext_gate(SAMPLE_SYMBOLS[0], "get_insider_trades", _insider_consume)
    gates["analyst_divergence"] = probe_analyst(SAMPLE_SYMBOLS[0])
    gates["earnings_proximity"] = probe_earnings(SAMPLE_SYMBOLS[0])
    gates["sentiment_extreme"] = probe_finnhub_sentiment(SAMPLE_SYMBOLS[0])
    gates["sec_event"] = probe_sec(trade_day)
    gates["ticker_sentiment"] = probe_ticker_sentiment(SAMPLE_SYMBOLS[0])
    gates["social_sentiment"] = {
        "status": "off",
        "detail": "设计性关断 (ADR-032): 数据源无极性分数, 诚实降级待有符号源",
    }

    from app.utils.timezone import now_et
    result = {
        "generated_at_et": now_et().isoformat(timespec="seconds"),
        "most_recent_et_trading_day": trade_day,
        "sample_symbols": SAMPLE_SYMBOLS,
        "gates": gates,
        "n_dead": sum(1 for g in gates.values() if g["status"] == "dead"),
        "n_stale": sum(1 for g in gates.values() if g["status"] == "stale"),
    }
    LATEST.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")

    for name, g in gates.items():
        print(f"[gate_probe] {name:<22} {g['status']:<6} {g['detail']}", flush=True)
    print(f"[gate_probe] 汇总: dead={result['n_dead']} stale={result['n_stale']} "
          f"(交易日基准 {trade_day})", flush=True)
    return result


def _cleanup():
    """关闭 Futu 连接 — OpenQuoteContext/LV3 的后台线程不关则进程挂着不退出."""
    try:
        from app.playbook.intraday_reentry import _futu_ext
        p = _futu_ext()
        if p is not None:
            p.close()
    except Exception:
        pass
    try:
        from app.data.futu_lv3_provider import get_futu_lv3_provider
        get_futu_lv3_provider().close()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        run_probe()
    finally:
        _cleanup()
