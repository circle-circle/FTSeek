#!/usr/bin/env python3
"""ORB 触发诊断脚本 (2026-06-01)
=====================================
用途: 美股开盘后任意时刻跑, 模拟 evaluate("orb", ctx) 调用,
     打印每个标的在每个防御层的状态, 定位为什么没触发.

运行时机:
  - 09:45-11:00 ET (= 21:45-23:00 CST 北京时间) 是 ORB 入场窗口, 此时跑最有意义
  - 之外时间也能跑, 但会被时间窗防御层拦下

用法:
  python3 scripts/diagnose_orb.py
  python3 scripts/diagnose_orb.py --symbol US.TSLA
  python3 scripts/diagnose_orb.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 把项目根加入 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def diagnose_one_symbol(symbol: str) -> dict:
    """模拟 ORB evaluate, 返回每个防御层的状态"""
    result = {"symbol": symbol, "stages": {}}

    try:
        from app.core.config_container import get_config
        from app.core.pipeline_contracts import SymbolContext
        cfg = get_config()
        orb_cfg = cfg.get_dotted("strategies.orb", {}) or {}
        result["orb_config"] = orb_cfg
    except Exception as e:
        result["error"] = f"config 读取失败: {e}"
        return result

    # 拉行情
    try:
        from app.data.market_data_router import get_data_router
        router = get_data_router()
        ctx_data = router.get_full_market_context(symbol) or {}
    except Exception as e:
        result["error"] = f"行情拉取失败: {e}"
        return result

    # 关键字段
    fields = {
        "price": ctx_data.get("price", 0),
        "atr": ctx_data.get("atr", 0),
        "open_price": ctx_data.get("open_price", 0),
        "opening_high": ctx_data.get("opening_high", 0),
        "opening_low": ctx_data.get("opening_low", 0),
        "opening_range_ready": ctx_data.get("opening_range_ready", False),
        "market_regime": ctx_data.get("market_regime", ""),
    }
    result["fields"] = fields

    # 逐层模拟防御
    stages = result["stages"]

    if fields["price"] <= 0 or fields["atr"] <= 0:
        stages["1_data_completeness"] = f"❌ FAIL: price={fields['price']} atr={fields['atr']}"
        return result
    stages["1_data_completeness"] = "✅ PASS"

    if not fields["opening_range_ready"]:
        stages["2_opening_range_ready"] = "❌ FAIL: opening_range_ready=False (15 根 1min K 线未到)"
        return result
    stages["2_opening_range_ready"] = "✅ PASS"

    if fields["opening_high"] <= 0 or fields["opening_low"] <= 0 or \
       fields["opening_high"] <= fields["opening_low"]:
        stages["3_range_validity"] = f"❌ FAIL: high={fields['opening_high']} low={fields['opening_low']}"
        return result
    stages["3_range_validity"] = "✅ PASS"

    base_price = fields["open_price"] if fields["open_price"] > 0 else fields["price"]
    width_pct = (fields["opening_high"] - fields["opening_low"]) / base_price * 100
    min_w = float(orb_cfg.get("min_range_width_pct", 0.3))
    max_w = float(orb_cfg.get("max_range_width_pct", 2.5))
    stages["4_range_width"] = f"width={width_pct:.2f}% (要求 {min_w}-{max_w}%)"
    if width_pct < min_w:
        stages["4_range_width"] += " ❌ 太窄"
        return result
    if width_pct > max_w:
        stages["4_range_width"] += " ❌ 太宽 — 高波动股常见!"
        return result
    stages["4_range_width"] += " ✅"

    # 时间窗
    from datetime import datetime as _dt, time as _dtime
    import pytz
    now_et = _dt.now(pytz.timezone('US/Eastern'))
    end_str = orb_cfg.get("entry_end_et", "11:00").split(":")
    entry_end = _dtime(int(end_str[0]), int(end_str[1]))
    entry_start = _dtime(9, 45)
    in_window = entry_start <= now_et.time() <= entry_end
    stages["5_time_window"] = f"now_et={now_et.time().strftime('%H:%M')} (要求 09:45-{end_str[0]}:{end_str[1]}) {'✅' if in_window else '❌ 不在窗口内'}"
    if not in_window:
        return result

    # 突破判断
    buf = fields["price"] * float(orb_cfg.get("breakout_buffer_pct", 0.1)) / 100
    if fields["price"] > fields["opening_high"] + buf:
        direction = "LONG"
    elif fields["price"] < fields["opening_low"] - buf:
        direction = "SHORT"
    else:
        stages["6_breakout"] = f"❌ 价格 {fields['price']} 在区间 [{fields['opening_low']}, {fields['opening_high']}] 内"
        return result
    stages["6_breakout"] = f"✅ {direction} 突破"

    # 不追高
    max_chase = float(orb_cfg.get("max_chase_pct", 1.5)) / 100
    if direction == "LONG":
        chase = (fields["price"] - fields["opening_high"]) / fields["opening_high"]
    else:
        chase = (fields["opening_low"] - fields["price"]) / fields["opening_low"]
    stages["7_no_chase"] = f"chase={chase*100:.2f}% (max={max_chase*100:.1f}%) {'✅' if chase <= max_chase else '❌ 已追高'}"
    if chase > max_chase:
        return result

    # 大盘趋势
    regime = (fields["market_regime"] or "").upper()
    if direction == "LONG" and regime == "TREND_DOWN":
        stages["8_regime_align"] = "❌ LONG 但大盘 TREND_DOWN"
        return result
    if direction == "SHORT" and regime == "TREND_UP":
        stages["8_regime_align"] = "❌ SHORT 但大盘 TREND_UP"
        return result
    stages["8_regime_align"] = f"✅ regime={regime}"

    stages["FINAL"] = f"✅ ORB 应触发: direction={direction}"
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", help="单个标的 (默认遍历 sandbox.universe 或 watchlist)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    symbols = []
    if args.symbol:
        symbols = [args.symbol if args.symbol.startswith("US.") else f"US.{args.symbol}"]
    else:
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            uni = cfg.get_dotted("mode.sandbox.universe", []) or []
            symbols = [s if s.startswith("US.") else f"US.{s}" for s in uni]
            if not symbols:
                # fallback 用主流标的
                symbols = ["US.TSLA", "US.AMD", "US.SPY", "US.QQQ", "US.NVDA"]
        except Exception:
            symbols = ["US.TSLA", "US.AMD", "US.SPY", "US.QQQ"]

    results = [diagnose_one_symbol(s) for s in symbols]

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    print("=" * 70)
    print(f"ORB 触发诊断 ({len(results)} 标的)")
    print("=" * 70)
    for r in results:
        print(f"\n📍 {r['symbol']}")
        if "error" in r:
            print(f"   ❌ {r['error']}")
            continue
        f = r.get("fields", {})
        print(f"   price={f.get('price'):.2f} atr={f.get('atr'):.2f} "
              f"high={f.get('opening_high'):.2f} low={f.get('opening_low'):.2f} "
              f"ready={f.get('opening_range_ready')}")
        for stage, status in r["stages"].items():
            print(f"   [{stage}] {status}")


if __name__ == "__main__":
    main()
