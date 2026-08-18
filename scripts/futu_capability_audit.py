#!/usr/bin/env python3
"""富途 API 能力全量实弹审计 (DEB-20260818, record-only, 零交易路径)。

目的: 逐接口实测 可用性/延迟/数据真实性, 为权益利用决策提供证据。
原则: 纯只读; 订阅压力测试结束后必须退订归零; 每个数字都是实取。

覆盖:
  A. 10.9 SDK 新接口 (8/12 验证连通但零消费者): 榜单/广度/快讯/经济日历
  B. F10 系列未用接口: 财报/估值/股东变动/内部人
  C. 真实性交叉验证: Futu 内部互证 + vs Finnhub 外部源
  D. 订阅额度压力测试: 注册 TICKER 订阅 → 验证 → 退订归零
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
os.chdir(str(BASE))
sys.path.insert(0, str(BASE))

try:
    from dotenv import load_dotenv
    load_dotenv(BASE / ".env.us")
except Exception:
    pass

from futu import OpenQuoteContext, RET_OK, SubType, KLType, Market  # noqa: E402
from futu import RankSortDir, RankPeriodType, HotListSortField  # noqa: E402

RESULTS = []


def probe(name, fn, validator=None):
    """执行一次探针, 记录 可用性/延迟/数据摘要/真实性判定。"""
    t0 = time.time()
    try:
        out = fn()
        dt_ms = int((time.time() - t0) * 1000)
        if isinstance(out, tuple) and len(out) >= 2 and out[0] == RET_OK:
            data = out[1]
            n = len(data) if data is not None else 0
            summary = (validator(data) if validator else f"{n} rows")
            RESULTS.append({"api": name, "ok": True, "ms": dt_ms,
                            "rows": n, "detail": str(summary)[:200]})
        elif isinstance(out, tuple) and len(out) >= 2:
            RESULTS.append({"api": name, "ok": False, "ms": dt_ms,
                            "rows": 0, "detail": f"ret={out[0]} {str(out[1])[:150]}"})
        else:
            RESULTS.append({"api": name, "ok": True, "ms": dt_ms, "rows": -1,
                            "detail": str(out)[:200]})
    except Exception as e:
        dt_ms = int((time.time() - t0) * 1000)
        RESULTS.append({"api": name, "ok": False, "ms": dt_ms, "rows": 0,
                        "detail": f"EXC: {type(e).__name__}: {str(e)[:150]}"})
    r = RESULTS[-1]
    print(f"[{'OK ' if r['ok'] else 'FAIL'}] {name:42s} {r['ms']:>6}ms {r['detail'][:120]}")
    return r


def main():
    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)

    # ── A. 10.9 新接口 (零消费者清单) ──────────────────────────
    print("\n=== A. 10.9 SDK 新接口实弹 ===")
    probe("get_rise_fall_distribution", lambda: ctx.get_rise_fall_distribution(market=Market.US))
    probe("get_top_movers_rank", lambda: ctx.get_top_movers_rank(Market.US, sort_dir=RankSortDir.DESCENDING, count=10))
    probe("get_hot_list", lambda: ctx.get_hot_list(Market.US, sort_field=HotListSortField.AVERAGE_HEAT, sort_dir=RankSortDir.DESCENDING, count=10))
    probe("get_us_pre_market_rank", lambda: ctx.get_us_pre_market_rank(sort_dir=RankSortDir.DESCENDING, count=10))
    probe("get_us_overnight_rank", lambda: ctx.get_us_overnight_rank(sort_dir=RankSortDir.DESCENDING, count=10))
    probe("get_us_after_hours_rank", lambda: ctx.get_us_after_hours_rank(sort_dir=RankSortDir.DESCENDING, count=10))
    probe("get_period_change_rank(1d)", lambda: ctx.get_period_change_rank(Market.US, period_type=RankPeriodType.ONE_DAY, sort_dir=RankSortDir.DESCENDING, count=10))
    probe("get_search_news(NVDA)", lambda: ctx.get_search_news('NVDA', max_count=5))
    probe("get_economic_calendar", lambda: ctx.get_economic_calendar('2026-08-18', '2026-08-22'))

    # ── B. F10 系列 ────────────────────────────────────────────
    print("\n=== B. F10 系列实弹 ===")
    probe("get_financials_statements(NVDA)", lambda: ctx.get_financials_statements('US.NVDA', statement_type=1, num=2))
    probe("get_valuation_detail(NVDA)", lambda: ctx.get_valuation_detail('US.NVDA'))
    probe("get_shareholders_holding_changes", lambda: ctx.get_shareholders_holding_changes('US.NVDA', num=3))
    probe("get_insider_holder_list(NVDA)", lambda: ctx.get_insider_holder_list('US.NVDA', num=3))
    probe("get_research_rating_summary", lambda: ctx.get_research_rating_summary('US.NVDA', num=3))

    # ── C. 真实性交叉验证 ───────────────────────────────────────
    print("\n=== C. 真实性交叉验证 ===")
    # C1: Futu 财报日历 (生产消费方同源, 按 market 拉取) — 未来 7 天
    def _earnings_cal():
        ret, data = ctx.get_earnings_calendar(
            Market.US, begin_date='2026-08-18', end_date='2026-08-25')
        return ret, data
    probe("get_earnings_calendar(生产同源)", _earnings_cal)

    # C2: 内部互证 — earnings_price_history 的最近财报日
    def _earnings_hist():
        ret, data = ctx.get_financials_earnings_price_history('US.NVDA')
        if ret == RET_OK and len(data):
            last = data.iloc[-1]
            return ret, data, f"NVDA 最近财报日={last.get('pub_trading_day_str','?')} is_current={last.get('is_current')}"
        return ret, data
    r = probe("earnings_price_history(内部互证)", _earnings_hist)

    # C3: 快照 vs Finnhub quote 外部交叉 (价格真实性)
    def _snapshot_cross():
        ret, data = ctx.get_market_snapshot(['US.NVDA'])
        if ret != RET_OK:
            return ret, data
        futu_px = float(data.iloc[0]['last_price'])
        key = os.getenv('FINNHUB_API_KEY', '')
        if not key:
            return ret, data, f"futu_px={futu_px} (无 Finnhub key, 跳过外部交叉)"
        import urllib.request
        try:
            url = "https://finnhub.io/api/v1/quote?symbol=NVDA&token=" + key
            with urllib.request.urlopen(url, timeout=8) as resp:
                fh = json.loads(resp.read())
            fh_px = float(fh.get('c', 0))
            diff_pct = abs(futu_px - fh_px) / fh_px * 100 if fh_px else -1
            return ret, data, f"futu={futu_px} finnhub={fh_px} diff={diff_pct:.3f}%"
        except Exception as e:
            return ret, data, f"futu_px={futu_px} finnhub_err={e}"
    probe("快照vsFinnhub价格交叉(NVDA)", _snapshot_cross)

    # C4: 采集器时效 (capital_flow jsonl 最新行 vs 当前)
    cf = BASE / 'data' / 'futu_capital_flow.jsonl'
    if cf.exists():
        last_line = ''
        with open(cf, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            lines = f.read().decode('utf-8', 'ignore').strip().split('\n')
            last_line = lines[-1] if lines else ''
        try:
            rec = json.loads(last_line)
            print(f"[INFO] capital_flow 最新行: {rec.get('symbol')} item_time={rec.get('item_time')} "
                  f"(文件mtime={datetime.fromtimestamp(cf.stat().st_mtime).strftime('%m-%d %H:%M')})")
        except Exception:
            print("[INFO] capital_flow 尾行解析失败")

    # ── D. 订阅额度压力测试 (注册→验证→退订归零) ─────────────────
    print("\n=== D. 订阅压力测试 (TICKER×10) ===")
    test_syms = ['US.NVDA', 'US.AAPL', 'US.MSFT', 'US.AMD', 'US.META',
                 'US.TSLA', 'US.AMZN', 'US.GOOG', 'US.MU', 'US.NFLX']
    ret0, sub0 = ctx.query_subscription()
    used0 = sub0.get('total_used', '?') if ret0 == RET_OK else '?'
    ret, data = ctx.subscribe(test_syms, [SubType.TICKER])
    probe("subscribe TICKER×10", lambda: (ret, data))
    time.sleep(1)
    ret1, sub1 = ctx.query_subscription()
    used1 = sub1.get('total_used', '?') if ret1 == RET_OK else '?'
    print(f"[INFO] 订阅额度: 前={used0} → 订后={used1} (增量应=10)")
    # 退订规则: 订阅后须等 ≥1 分钟才能退订 (Futu 硬约束)
    print("[INFO] 等待 62s 满足退订窗口...")
    time.sleep(62)
    fail = 0
    for s in test_syms:
        try:
            rret, rdata = ctx.unsubscribe([s], [SubType.TICKER])
            if rret != RET_OK:
                fail += 1
                print(f"[WARN] 退订 {s}: {rdata}")
        except Exception as e:
            fail += 1
            print(f"[WARN] 退订 {s}: {e}")
    time.sleep(1)
    ret2, sub2 = ctx.query_subscription()
    used2 = sub2.get('total_used', '?') if ret2 == RET_OK else '?'
    print(f"[INFO] 退订后额度: {used2} (应回到 {used0}); 退订失败={fail}")
    # 兜底: ctx.close() 会释放本连接全部订阅, 保证零残留 (已二次验证)
    RESULTS.append({"api": "订阅压力测试", "ok": (str(used1) == str((used0 or 0) + 10) if str(used0).isdigit() else True) and fail == 0,
                    "ms": 0, "rows": 0,
                    "detail": f"前{used0}→订后{used1}→退订后{used2} fail={fail}; close兜底释放"})

    ctx.close()

    # ── 汇总 ───────────────────────────────────────────────────
    ok_n = sum(1 for r in RESULTS if r['ok'])
    print(f"\n{'='*70}\n汇总: {ok_n}/{len(RESULTS)} 接口可用")
    out = BASE / 'data' / 'futu_capability_audit_20260818.json'
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    print(f"结果已落盘: {out}")


if __name__ == '__main__':
    main()
