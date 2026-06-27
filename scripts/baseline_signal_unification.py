#!/usr/bin/env python3
"""信号源统一(改动1 修正版) baseline 采集 + A/B 对照.

用途 (对应 docs/analysis/signal-source-unification-review-20260624.md §3.3 步骤1/3):
  - 步骤1 基线采集: 对候选标的落盘 legacy(10字段) vs 新路径(SymbolContext 50+字段) 的 market_data
  - 步骤3 shadow 对照: 比较字段维度数、缓存哈希、(可选) vote 方向 delta

默认模式 (不调 LLM, 快): 只比较 market_data 字段集 + vote 缓存哈希.
  python scripts/baseline_signal_unification.py --symbols US.AAPL,US.TSLA
  python scripts/baseline_signal_unification.py --symbols US.AAPL,US.TSLA --vote

回滚阈值 (§3.3 步骤4): --vote 模式下若 SHORT 占比偏移 >15pp / SKIP 率 >20% / 方向一致率 <70% → 警告.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import concurrent.futures
from datetime import datetime
from pathlib import Path

# 确保项目根在 sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _bootstrap():
    """构造 router / regime / voting_coordinator / collector (镜像 scheduler_core.py:220)."""
    from app.data.market_data_router import MarketDataRouter
    from app.playbook.market_regime import MarketRegimeGate
    from app.core.di_container import get_di_container
    from app.playbook.pipeline import _build_data_collector

    router = MarketDataRouter(
        twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY"),
        finnhub_api_key=os.getenv("FINNHUB_API_KEY"),
        alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
        cache_ttl_seconds=900,
        futu_opend_host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
        futu_opend_port=int(os.getenv("FUTU_OPEND_PORT", "11111")),
        enable_futu_lv3=True,
    )
    regime_ctx = MarketRegimeGate().assess()
    vc = get_di_container().voting_coordinator()
    collector = _build_data_collector()
    return router, regime_ctx, vc, collector


def _build_new_ctx(symbol, raw, regime_ctx):
    """新路径: 与 _build_vote_market_data 同构 (不读 config flag, 强制新路径)."""
    from app.core.pipeline_contracts import SymbolContext
    from app.playbook.pipeline import _normalize_regime_str
    ctx = SymbolContext.from_raw(symbol, raw)
    ctx.vix = float(getattr(regime_ctx, "vix", 20.0) or 20.0)
    ctx.market_regime = _normalize_regime_str(regime_ctx)
    return ctx


def _vote_with_timeout(vc, symbol, market_data, timeout=30.0):
    """带超时地调 vote (镜像 signal_stage.py:394 的 ThreadPoolExecutor 模式).

    注: 首次 LLM 调用冷启动可能 10-20s, 默认 30s; 实盘热缓存通常 <5s.
    """
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(vc.vote, symbol, market_data)
            return fut.result(timeout=timeout)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def _cache_hash_keys(market_data):
    """复刻 voting_coordinator._compute_market_hash 的 key_fields (line 120-124)."""
    return {
        "symbol": market_data.get("symbol", "") if isinstance(market_data, dict)
        else getattr(market_data, "symbol", ""),
        "rsi": market_data.get("rsi") if isinstance(market_data, dict)
        else getattr(market_data, "rsi", None),
        "macd_signal": market_data.get("macd_signal") if isinstance(market_data, dict)
        else getattr(market_data, "macd_signal", None),
        "regime": market_data.get("regime") if isinstance(market_data, dict)
        else getattr(market_data, "regime", None),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="US.AAPL,US.MSFT,US.TSLA,US.NVDA,US.AMZN",
                    help="逗号分隔标的列表")
    ap.add_argument("--out", default=str(_ROOT / "docs" / "analysis" /
                                         "signal_unification_baseline.json"),
                    help="输出 JSON 路径")
    ap.add_argument("--vote", action="store_true", help="额外调 VotingCoordinator vote 做方向 delta (慢, 调 LLM)")
    args = ap.parse_args()

    import dataclasses
    from app.playbook.pipeline import _build_vote_market_data

    router, regime_ctx, vc, collector = _bootstrap()
    if vc is None:
        print("[FATAL] VotingCoordinator 不可用, 无法 --vote; 仍可做字段对比", file=sys.stderr)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    regime_str = getattr(regime_ctx.regime, "name", str(getattr(regime_ctx, "regime", "?")))
    # os._exit 不会 flush stdout, 强制行缓冲
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    print(f"[baseline] regime={regime_str} vix={getattr(regime_ctx, 'vix', '?')} symbols={len(symbols)} vote={args.vote}")

    records = []
    dir_count = {"agree": 0, "disagree": 0, "error": 0}
    dist_legacy = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0, "SKIP": 0, "ERROR": 0}
    dist_new = {"LONG": 0, "SHORT": 0, "NEUTRAL": 0, "SKIP": 0, "ERROR": 0}

    for sym in symbols:
        rec = {"symbol": sym}
        try:
            raw = router.get_full_market_context(sym)
            if not raw or float(raw.get("price", 0) or 0) <= 0:
                rec["status"] = "no_market_data"
                records.append(rec)
                print(f"  {sym}: SKIP (router 无数据)")
                continue
            # legacy 适配器已删除, 只跑新路径
            legacy_md = None

            new_ctx = _build_new_ctx(sym, raw, regime_ctx)
            new_md = dataclasses.asdict(new_ctx)

            legacy_keys = sorted((legacy_md or {}).keys()) if legacy_md else []
            new_keys = sorted(new_md.keys())
            new_nonzero = {k: v for k, v in new_md.items()
                           if v not in (0, 0.0, "", None, False, [])}

            rec["legacy_field_count"] = len(legacy_keys)
            rec["legacy_fields"] = legacy_keys
            rec["new_field_count"] = len(new_keys)
            rec["new_nonzero_count"] = len(new_nonzero)
            rec["new_only_keys"] = sorted(set(new_keys) - set(legacy_keys))
            rec["legacy_cache_hash_keys"] = _cache_hash_keys(legacy_md) if legacy_md else None
            rec["new_cache_hash_keys"] = _cache_hash_keys(new_ctx)
            rec["price"] = raw.get("price")

            if args.vote and vc is not None:
                v_legacy = _vote_with_timeout(vc, sym, legacy_md) if legacy_md else {"_error": "no_legacy"}
                v_new = _vote_with_timeout(vc, sym, new_ctx)
                d_legacy = (v_legacy or {}).get("direction", "ERROR") if isinstance(v_legacy, dict) else "ERROR"
                d_new = (v_new or {}).get("direction", "ERROR") if isinstance(v_new, dict) else "ERROR"
                if "_error" in (v_legacy or {}):
                    d_legacy = "ERROR"
                if "_error" in (v_new or {}):
                    d_new = "ERROR"
                rec["vote_legacy"] = {"direction": d_legacy, "decision_id": (v_legacy or {}).get("decision_id")}
                rec["vote_new"] = {"direction": d_new, "decision_id": (v_new or {}).get("decision_id")}
                rec["vote_agree"] = (d_legacy == d_new and d_legacy not in ("ERROR",))
                dist_legacy[d_legacy] = dist_legacy.get(d_legacy, 0) + 1
                dist_new[d_new] = dist_new.get(d_new, 0) + 1
                if d_legacy == "ERROR" or d_new == "ERROR":
                    dir_count["error"] += 1
                elif d_legacy == d_new:
                    dir_count["agree"] += 1
                else:
                    dir_count["disagree"] += 1
                print(f"  {sym}: legacy={d_legacy} new={d_new} agree={rec['vote_agree']} "
                      f"fields {len(legacy_keys)}→{len(new_keys)}({len(new_nonzero)} nonzero)")
            else:
                print(f"  {sym}: fields {len(legacy_keys)}→{len(new_keys)}({len(new_nonzero)} nonzero) "
                      f"price={raw.get('price')}")
        except Exception as e:
            rec["status"] = f"error: {type(e).__name__}: {e}"
            print(f"  {sym}: ERROR {e}")
        records.append(rec)

    summary = {
        "generated_at": datetime.now().isoformat(),
        "regime": regime_str,
        "vix": getattr(regime_ctx, "vix", None),
        "symbols_total": len(symbols),
        "symbols_ok": sum(1 for r in records if r.get("new_field_count")),
        "vote_mode": args.vote,
        "direction": dir_count if args.vote else None,
        "distribution_legacy": dist_legacy if args.vote else None,
        "distribution_new": dist_new if args.vote else None,
        "records": records,
    }

    if args.vote and dir_count:
        total = dir_count["agree"] + dir_count["disagree"]
        agree_rate = (dir_count["agree"] / total * 100) if total else 0
        summary["agree_rate_pct"] = round(agree_rate, 1)
        # §3.3 步骤4 回滚阈值
        alerts = []
        if total and agree_rate < 70:
            alerts.append(f"方向一致率 {agree_rate:.1f}% < 70% 阈值")
        if dist_new.get("SKIP", 0) / max(1, len(symbols)) > 0.20:
            alerts.append(f"新路径 SKIP 率 {dist_new.get('SKIP',0)}/{len(symbols)} > 20%")
        legacy_short = dist_legacy.get("SHORT", 0) / max(1, len(symbols))
        new_short = dist_new.get("SHORT", 0) / max(1, len(symbols))
        if abs(new_short - legacy_short) > 0.15:
            alerts.append(f"SHORT 占比偏移 {legacy_short:.0%}→{new_short:.0%} > 15pp")
        summary["rollback_alerts"] = alerts
        if alerts:
            print("\n[ROLLBACK ALERT]")
            for a in alerts:
                print(f"  ⚠ {a}")
        else:
            print(f"\n[OK] 方向一致率 {agree_rate:.1f}%, 无回滚阈值触发")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, default=str, ensure_ascii=False))
    print(f"\n[baseline] 已写入 {out} ({len(records)} records)", flush=True)

    # Futu OpenQuoteContext / 后台线程在解释器退出时挂起 (非 daemon), 诊断脚本直接硬退出
    try:
        sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
