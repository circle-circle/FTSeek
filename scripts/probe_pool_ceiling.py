#!/usr/bin/env python3
"""候选池"物理上限"核查 (只读, 零下单) —— 回答: 现役异动榜里, 每轮**最多**能有几只票过闸?

2026-09-23 实测教训 (两条, 都会造出**假读数**):
  ① **限频假 0**: 连发 3 批快照会把后续批次全打成失败 ⇒ 第二/三轮读成"0 只过闸"。
     必须**跨轮去重**后一次性取数, 且批次间 sleep。
  ② **时段不代表执行窗**: 取 ET 03:4x 的榜 (70% 过闸) ≠ ET 04-09 执行窗的榜;
     执行窗实测 (price_gate 台账 389 轮/10887 被拒票次) 被拒票 **100% <$5、价中位 $1.4**
     ⇒ 现役异动榜在执行窗内就是微盘泵票榜。要量池子必须按**执行窗小时**切。

口径与生产**完全一致**: 价格取快照, 市值取快照 `total_market_val` (与 executor:714 同一字段),
闸值取 .env.us 实值 (MIN_PRICE / MIN_MARKET_CAP_USD)。不写任何生产目录。
"""
import json
import os
import sys
import time
from pathlib import Path

from futu import OpenQuoteContext, KLType  # noqa: F401  (保持与其它脚本同一导入面)

ROOT = Path(__file__).resolve().parents[1] if (Path(__file__).resolve().parents[1] / "scripts").exists() \
    else Path("/opt/ftseek/FTSeek-US")
LED = ROOT / "data" / "futu_top_movers_shadow.jsonl"


def envf(k, d):
    try:
        return float(os.environ.get(k, d))
    except Exception:
        return float(d)


MIN_PRICE = envf("EXT_ENTRY_MIN_PRICE", 5.0)
MIN_CAP = envf("EXT_ENTRY_MIN_MARKET_CAP_USD", 1_000_000_000)

_CACHE = {}


def snap_bisect(ctx, codes, sleep_s=1.6):
    """快照批混坏票 (OTC 等) 会让整批失败 ⇒ 二分隔离; 命中缓存避免重复调用。

    教训 (本轮实测): 连发 3 批会把后续批次全部打成失败 (限频) ⇒ 读数假 0;
    必须先**跨轮去重**再取数, 且批次之间 sleep。
    """
    out, stack = {}, [list(codes)]
    for c in codes:
        if c in _CACHE:
            out[c] = _CACHE[c]
    stack = [[c for c in codes if c not in _CACHE]]
    while stack:
        batch = stack.pop()
        if not batch:
            continue
        ret, df = ctx.get_market_snapshot(batch)
        time.sleep(sleep_s)
        if ret == 0:
            for _, row in df.iterrows():
                out[row["code"]] = row
                _CACHE[row["code"]] = row
            continue
        if len(batch) == 1:
            continue                                  # 单票也失败 ⇒ 丢弃 (不可交易票)
        mid = len(batch) // 2
        stack += [batch[:mid], batch[mid:]]
    return out


rounds = [json.loads(l) for l in LED.read_text().splitlines() if l.strip()][-3:]
ctx = OpenQuoteContext(host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
                       port=int(os.environ.get("FUTU_OPEND_PORT", 11111)))
try:
    print(f"闸值: 价 ≥${MIN_PRICE:g} | 市值 ≥${MIN_CAP/1e9:.1f}B  (口径: 快照 last_price / total_market_val)")
    uniq = sorted({m["symbol"] for r in rounds for m in (r.get("movers") or [])})
    print(f"跨 {len(rounds)} 轮去重后唯一票 {len(uniq)} 只 ⇒ 先取这一批 (避免重复调用触发限频)")
    got = snap_bisect(ctx, uniq)
    print(f"取到快照 {len(got)}/{len(uniq)} 只" + (f" (缺: {[c for c in uniq if c not in got][:6]}...)" if len(got) < len(uniq) else ""))
    agg = []
    for r in rounds:
        mv = [m for m in (r.get("movers") or [])]
        codes = [m["symbol"] for m in mv]
        snap = {c: _CACHE[c] for c in codes if c in _CACHE}
        ok_p = ok_c = ok_both = 0
        for m in mv:
            row = snap.get(m["symbol"])
            if row is None:
                continue
            px = float(row.get("last_price") or 0)
            cap = float(row.get("total_market_val") or 0)
            p = px >= MIN_PRICE
            c = cap >= MIN_CAP
            ok_p += p
            ok_c += c
            ok_both += (p and c)
        n = len(mv)
        print(f"  [{r['ts']}] 榜 {n} 只 → 价过 {ok_p} ({100*ok_p/n:.0f}%) | "
              f"市值过 {ok_c} ({100*ok_c/n:.0f}%) | **两者都过 {ok_both}** ⇒ 这是本轮候选的物理上限")
        agg.append((n, ok_p, ok_c, ok_both))
    if agg:
        tn = sum(a[0] for a in agg)
        tb = sum(a[3] for a in agg)
        print(f"\n小计: {tn} 只 / {len(agg)} 轮 → 过价+市值 {tb} 只 "
              f"({100*tb/max(1,tn):.1f}%) ⇒ 平均 {tb/len(agg):.1f} 只/轮")
        print("注: 过闸 ≠ 会下单 (后面还有涨幅带/量/价差/深度/财报门); 这里只量**供给天花板**。")
finally:
    ctx.close()