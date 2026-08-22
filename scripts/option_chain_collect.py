#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期权链数据采集 (2026-08-22)
============================
采集 Futu 期权链数据 (put/call ratio, IV skew, unusual flow), 攒样供方向确认.

数据源: Futu OpenD get_option_chain / get_option_events / get_option_rank
输出: data/option_chain.jsonl
"""

import json
import os
from pathlib import Path
from datetime import datetime

BASE = Path("/opt/ftseek/FTSeek-US")
OUT = BASE / "data" / "option_chain.jsonl"
LATEST = BASE / "data" / "option_chain_latest.json"

# 采集标的 (watchlist + 高流动性)
SYMBOLS = ["US.SPY", "US.QQQ", "US.AAPL", "US.TSLA", "US.NVDA", "US.AMD", "US.MU"]


def collect_option_chain():
    """采集期权链数据"""
    try:
        from app.data.providers.futu_extended_provider import FutuExtendedProvider
        futu_ext = FutuExtendedProvider()
        if not futu_ext._get_ctx():
            print("[option] Futu context unavailable")
            return 0

        ctx = futu_ext._get_ctx()
        records = []

        for symbol in SYMBOLS:
            try:
                # 获取期权链 (最近到期日)
                ret, chain = ctx.get_option_chain(symbol)
                if ret != 0 or chain is None or len(chain) == 0:
                    print(f"[option] {symbol} 期权链拉取失败或无数据")
                    continue

                # 聚合: put/call ratio, IV skew
                puts = chain[chain["option_type"] == "P"]
                calls = chain[chain["option_type"] == "C"]

                if len(puts) > 0 and len(calls) > 0:
                    pc_ratio = len(puts) / len(calls)
                    avg_iv_put = puts["implied_volatility"].mean() if "implied_volatility" in puts else None
                    avg_iv_call = calls["implied_volatility"].mean() if "implied_volatility" in calls else None
                    iv_skew = (avg_iv_put - avg_iv_call) if (avg_iv_put and avg_iv_call) else None

                    records.append({
                        "symbol": symbol,
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "pc_ratio": round(pc_ratio, 3),
                        "avg_iv_put": round(avg_iv_put, 4) if avg_iv_put else None,
                        "avg_iv_call": round(avg_iv_call, 4) if avg_iv_call else None,
                        "iv_skew": round(iv_skew, 4) if iv_skew else None,
                        "n_puts": len(puts),
                        "n_calls": len(calls),
                        "fetched_at": datetime.now().isoformat(),
                    })
                    print(f"[option] {symbol}: pc_ratio={pc_ratio:.2f} iv_skew={iv_skew:.4f if iv_skew else 'N/A'}")
            except Exception as e:
                print(f"[option] {symbol} 异常: {e}")

        # 追加写入
        if records:
            with open(OUT, "a") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

            # 写 latest 摘要
            latest = {
                "generated_at": datetime.now().isoformat(),
                "n_records": len(records),
                "symbols": SYMBOLS,
            }
            LATEST.write_text(json.dumps(latest, ensure_ascii=False, indent=2))

        print(f"[option] 采集完成: {len(records)} 条")
        return len(records)

    except Exception as e:
        print(f"[option] 采集失败: {e}")
        return 0


if __name__ == "__main__":
    collect_option_chain()
