#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宏观指标采集 (2026-08-22)
==========================
采集 Futu 宏观指标 (CPI/PMI/利率等), 攒样供 regime 判断.

数据源: Futu OpenD get_macro_indicator_list / get_macro_indicator_history
输出: data/macro_indicators.jsonl
"""

import json
import os
from pathlib import Path
from datetime import datetime

BASE = Path("/opt/ftseek/FTSeek-US")
OUT = BASE / "data" / "macro_indicators.jsonl"
LATEST = BASE / "data" / "macro_indicators_latest.json"

# 关键宏观指标 (美国)
INDICATORS = {
    "US.CPI": "CPI 消费者物价指数",
    "US.PMI": "PMI 采购经理人指数",
    "US.FED_RATE": "联邦基金利率",
    "US.UNEMPLOYMENT": "失业率",
    "US.GDP": "GDP 增长率",
}


def collect_macro():
    """采集宏观指标"""
    try:
        from app.data.providers.futu_extended_provider import FutuExtendedProvider
        futu_ext = FutuExtendedProvider()
        if not futu_ext._get_ctx():
            print("[macro] Futu context unavailable")
            return 0

        ctx = futu_ext._get_ctx()
        records = []

        for code, name in INDICATORS.items():
            try:
                # 获取指标历史 (最近 10 条)
                ret, df = ctx.get_macro_indicator_history(code, num=10)
                if ret != 0 or df is None or len(df) == 0:
                    print(f"[macro] {code} 拉取失败或无数据")
                    continue

                for _, row in df.iterrows():
                    records.append({
                        "indicator": code,
                        "name": name,
                        "date": str(row.get("date", "")),
                        "value": float(row.get("value", 0)) if row.get("value") else None,
                        "fetched_at": datetime.now().isoformat(),
                    })
                print(f"[macro] {code} ({name}): {len(df)} 条")
            except Exception as e:
                print(f"[macro] {code} 异常: {e}")

        # 追加写入
        if records:
            with open(OUT, "a") as f:
                for r in records:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

            # 写 latest 摘要
            latest = {
                "generated_at": datetime.now().isoformat(),
                "n_records": len(records),
                "indicators": list(INDICATORS.keys()),
            }
            LATEST.write_text(json.dumps(latest, ensure_ascii=False, indent=2))

        print(f"[macro] 采集完成: {len(records)} 条")
        return len(records)

    except Exception as e:
        print(f"[macro] 采集失败: {e}")
        return 0


if __name__ == "__main__":
    collect_macro()
