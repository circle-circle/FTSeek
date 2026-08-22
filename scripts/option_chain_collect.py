#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期权链数据采集 (2026-08-22)
============================
采集富途期权市场统计数据 (市场级 Put/Call Ratio), 攒样供方向确认.

数据源: Futu OpenD get_option_market_statistic(OptionMarket.US_SECURITY, OptionStatisticDataType.VOLUME/OPEN_INTEREST)
  实测返回列: time, timestamp, call_value, put_value, total_value, ratio
  (ratio = Put/Call 比值, 市场级情绪指标)

注: 2026-08-22 复查发现原脚本假设的 get_option_chain 返回列含
    implied_volatility/option_type='P'/'C' 字段不存在 (死数据), 已改用真实
    get_option_market_statistic API.

输出: data/option_market_stat.jsonl
"""

import json
from pathlib import Path
from datetime import datetime, timedelta

BASE = Path("/opt/ftseek/FTSeek-US")
OUT = BASE / "data" / "option_market_stat.jsonl"
LATEST = BASE / "data" / "option_market_stat_latest.json"


def collect_option_market_stat():
    """采集期权市场统计 (市场级 Put/Call Ratio)"""
    try:
        from futu import OpenQuoteContext, RET_OK, OptionMarket, OptionStatisticDataType
    except Exception as e:
        print(f"[option] futu import 失败: {e}")
        return 0

    ctx = None
    try:
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)

        # 最近 7 个交易日的成交量/持仓量统计
        end = datetime.now().strftime("%Y-%m-%d")
        begin = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        records = []

        for data_type, type_name in [
            (OptionStatisticDataType.VOLUME, "volume"),
            (OptionStatisticDataType.OPEN_INTEREST, "open_interest"),
        ]:
            try:
                ret, data, page_req_key = ctx.get_option_market_statistic(
                    OptionMarket.US_SECURITY, data_type, begin_time=begin, end_time=end)
                if ret != RET_OK or data is None or len(data) == 0:
                    print(f"[option] {type_name} 拉取失败: {data}")
                    continue
                for _, row in data.iterrows():
                    records.append({
                        "date": str(row.get("time", "")),
                        "type": type_name,
                        "call_value": int(row.get("call_value", 0)),
                        "put_value": int(row.get("put_value", 0)),
                        "total_value": int(row.get("total_value", 0)),
                        "pc_ratio": float(row.get("ratio", 0)) if row.get("ratio") else None,
                        "market": "US_SECURITY",
                        "fetched_at": datetime.now().isoformat(),
                    })
                print(f"[option] {type_name}: {len(data)} 条")
            except Exception as e:
                print(f"[option] {type_name} 异常: {e}")

        # 追加写入 (去重: 同 date+type 覆盖)
        if records:
            existing = {}
            if OUT.exists():
                for line in open(OUT):
                    try:
                        r = json.loads(line)
                        existing[(r["date"], r["type"])] = r
                    except json.JSONDecodeError:
                        continue
            for r in records:
                existing[(r["date"], r["type"])] = r
            with open(OUT, "w") as f:
                for r in sorted(existing.values(), key=lambda x: (x["date"], x["type"])):
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")

            LATEST.write_text(json.dumps({
                "generated_at": datetime.now().isoformat(),
                "n_records": len(existing),
            }, ensure_ascii=False, indent=2))

        print(f"[option] 采集完成: {len(records)} 条")
        return len(records)

    except Exception as e:
        print(f"[option] 采集失败: {e}")
        return 0
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    collect_option_market_stat()
