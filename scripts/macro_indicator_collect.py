#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宏观指标采集 (2026-08-22)
==========================
采集富途宏观指标 (CPI/失业率/GDP/利率), 攒样供 regime 判断.

数据源: Futu OpenD
  - get_macro_indicator_list(region='US') 获取指标列表
  - get_macro_indicator_history(indicator_id, max_count=N) 获取历史
  实测返回列: data_time, release_time, value, predict_value, previous_value, unit_type

注: 2026-08-22 复查发现原脚本假设的 get_macro_indicator_history(code, num) 签名
    错误 (真实签名是 (indicator_id, time, max_count)), 且 indicator 用 'US.CPI'
    字符串格式错误 (真实是整数 indicator_id). 已用实测 API 重写.

输出: data/macro_indicators.jsonl
"""

import json
from pathlib import Path
from datetime import datetime

BASE = Path("/opt/ftseek/FTSeek-US")
OUT = BASE / "data" / "macro_indicators.jsonl"
LATEST = BASE / "data" / "macro_indicators_latest.json"

# 关键宏观指标 (实测真实 indicator_id, 2026-08-22 从 get_macro_indicator_list 取得)
INDICATORS = {
    1003000002: "美国CPI同比",
    1003000001: "美国核心CPI同比",
    1003000007: "美国失业率",
    1003000008: "美国初次申请失业救济金人数",
    1003000019: "美国实际国内生产总值(GDP)",
    1003000026: "美国联邦基金利率",
    1003000013: "美联储目标利率上限",
}


def collect_macro():
    """采集宏观指标"""
    try:
        from futu import OpenQuoteContext, RET_OK
    except Exception as e:
        print(f"[macro] futu import 失败: {e}")
        return 0

    ctx = None
    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        records = []

        for indicator_id, name in INDICATORS.items():
            try:
                ret, df = ctx.get_macro_indicator_history(indicator_id, max_count=5)
                if ret != RET_OK or df is None or len(df) == 0:
                    print(f"[macro] {indicator_id} ({name}) 拉取失败或无数据")
                    continue

                def _safe_float(v):
                    """N/A / None / 非数字 → None (实测 Futu 返回 'N/A' 字符串)"""
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None

                for _, row in df.iterrows():
                    records.append({
                        "indicator_id": indicator_id,
                        "name": name,
                        "data_time": str(row.get("data_time", "")),
                        "release_time": str(row.get("release_time", "")),
                        "value": _safe_float(row.get("value")),
                        "predict_value": _safe_float(row.get("predict_value")),
                        "previous_value": _safe_float(row.get("previous_value")),
                        "unit_type": str(row.get("unit_type", "")),
                        "fetched_at": datetime.now().isoformat(),
                    })
                print(f"[macro] {name}: {len(df)} 条")
            except Exception as e:
                print(f"[macro] {name} 异常: {e}")

        # 追加写入 (去重: 同 indicator_id+data_time 覆盖)
        if records:
            existing = {}
            if OUT.exists():
                for line in open(OUT):
                    try:
                        r = json.loads(line)
                        existing[(r["indicator_id"], r["data_time"])] = r
                    except json.JSONDecodeError:
                        continue
            for r in records:
                existing[(r["indicator_id"], r["data_time"])] = r
            with open(OUT, "w") as f:
                for r in sorted(existing.values(), key=lambda x: (x["indicator_id"], x["data_time"])):
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

            LATEST.write_text(json.dumps({
                "generated_at": datetime.now().isoformat(),
                "n_records": len(existing),
                "indicators": list(INDICATORS.values()),
            }, ensure_ascii=False, indent=2))

        print(f"[macro] 采集完成: {len(records)} 条")
        return len(records)

    except Exception as e:
        print(f"[macro] 采集失败: {e}")
        return 0
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    collect_macro()
