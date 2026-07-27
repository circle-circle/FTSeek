#!/usr/bin/env python3
"""跨期稳健性验证 L3 (2026-07-26, 用户批"构建回测体系") — 只读, 零 live 影响。

目的: 用 5 年日 K 检验近期结构性发现是否只是当下市况的产物, 防止 8 月终裁
引用"6 周样本规律"当结构性规律。核心检验对象 = ATR 甜蜜区 (07-24 复盘:
3-4% 桶是唯一正期望桶, 14 天样本)。

方法 (诚实边界):
  - 不是策略回测 (LLM 信号端不可历史回放, 见 DEB-20260726-backtest-l2-l3 §L4);
  - 是"机会结构"统计: 按 ATR14% 分桶 × 年, 看 ①动量跟随收益 (昨日收-收方向
    今日开盘进场持有到收盘, 本系统日内动量做法的最近似日K代理) ②日内波幅
    (机会规模) 是否跨年稳定分层。
  - 判据 (预注册): 3-4% 桶动量跟随收益 ≥4 年同号优于 2.5-3% 桶 → 结构性;
    否则 ATR 门精调降级为"近期市况参数", 终裁不得引用为结构规律。

数据: Futu KL_DAY (分页拉取), 缓存 data/replay_bars_daily/{SYM}.json —
      同缓存被 entry_gate_replay.py (L2) 复用做 ATR 回算。
用法: PYTHONPATH=. ./venv/bin/python scripts/historical_robustness_check.py
        [--years 5] [--symbols US.NVDA,US.TSM] [--refresh]
落盘: data/historical_robustness_latest.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DB_PATH = REPO / "data" / "ftseek_us.db"
DAILY_DIR = REPO / "data" / "replay_bars_daily"
OUT_PATH = REPO / "data" / "historical_robustness_latest.json"

BUCKETS = [("<2.5", 0.0, 2.5), ("2.5-3.0", 2.5, 3.0), ("3.0-4.0", 3.0, 4.0), (">=4.0", 4.0, 99.0)]


def instruction_symbols() -> list[str]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM playbook_instructions WHERE direction IN ('BUY','SELL_SHORT')")]
    conn.close()
    return sorted(syms)


def _cache_path(symbol: str) -> Path:
    return DAILY_DIR / f"{symbol.replace('.', '_')}.json"


def fetch_daily(ctx, symbol: str, start: str, end: str, refresh: bool = False) -> list[list] | None:
    """[[date, o, h, l, c]] 升序; Futu KL_DAY 分页拉全. 失败返回 None."""
    cp = _cache_path(symbol)
    if cp.exists() and not refresh:
        try:
            rows = json.loads(cp.read_text())
            if rows and rows[0][0] <= start[:10] or len(rows) > 200:  # 缓存覆盖足够即用
                return rows
        except Exception:
            pass
    from futu import KLType, RET_OK, AuType
    rows: list[list] = []
    page_key = None
    for _page in range(8):  # 5y/1000 根 ≈ 2 页, 上限 8 页防死循环
        # 2026-07-28 (DEB-bar-ruler F2): 显式 QFQ — L3 五年跨度的连续性分析应用前复权
        #   (除息跳空会污染长周期 ATR 分桶/趋势); ATR% 为比值, 复权同时缩放分子分母,
        #   分桶口径不受影响 (复核待复核点④定性)。
        ret, data, page_key = ctx.request_history_kline(
            symbol, ktype=KLType.K_DAY, start=start, end=end,
            max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
        if ret != RET_OK or data is None:
            break
        for _, r in data.iterrows():
            rows.append([str(r.get("time_key", ""))[:10], float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])])
        if page_key is None:
            break
        time.sleep(1.2)  # kline 配额保护
    time.sleep(1.2)
    if not rows:
        return None
    rows.sort(key=lambda b: b[0])
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(rows))
    return rows


def atr14_pct_series(rows: list[list]) -> dict[str, float]:
    """date → ATR14/close*100 (TR 简单滚动均值, 与 pipeline 定桶同口径量级)."""
    out: dict[str, float] = {}
    trs: list[float] = []
    prev_c = None
    for d, o, h, l, c in rows:
        if prev_c is not None:
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            trs.append(tr)
            if len(trs) > 14:
                trs.pop(0)
            if len(trs) == 14 and c > 0:
                out[d] = sum(trs) / 14 / c * 100
        prev_c = c
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--symbols", default="", help="逗号分隔覆盖 (缺省=指令台账 78 标的)")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()] or instruction_symbols()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=365 * args.years)).strftime("%Y-%m-%d")
    print(f"L3 跨期稳健性: {len(syms)} 标的 × {start}~{end} 日K")

    from futu import OpenQuoteContext
    import os
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    # bucket → year → 累计器 {n, follow_sum, range_sum}
    agg: dict[str, dict[str, dict]] = {}
    fetched, failed = 0, []
    try:
        for i, sym in enumerate(syms):
            rows = fetch_daily(ctx, sym, start, end, args.refresh)
            if not rows or len(rows) < 30:
                failed.append(sym)
                continue
            fetched += 1
            atrp = atr14_pct_series(rows)
            for j in range(2, len(rows)):
                d, o, h, l, c = rows[j]
                pd_, po, ph, pl, pc = rows[j - 1]
                ppc = rows[j - 2][4]
                a = atrp.get(pd_)  # 用前一日 ATR 定桶 (交易时点可知, 无前视)
                if a is None or o <= 0 or ppc <= 0 or pc <= 0:
                    continue
                bucket = next((name for name, lo, hi in BUCKETS if lo <= a < hi), None)
                if bucket is None:
                    continue
                year = d[:4]
                cc_prev = (pc - ppc) / ppc          # 昨日收-收动量方向
                oc_today = (c - o) / o              # 今日开-收
                follow = (1 if cc_prev > 0 else -1) * oc_today * 100  # 动量跟随收益%
                rng = (h - l) / pc * 100
                slot = agg.setdefault(bucket, {}).setdefault(year, {"n": 0, "follow": 0.0, "rng": 0.0})
                slot["n"] += 1
                slot["follow"] += follow
                slot["rng"] += rng
            if (i + 1) % 15 == 0:
                print(f"  {i + 1}/{len(syms)} (fail {len(failed)})")
    finally:
        ctx.close()

    # 汇总 + 预注册判据
    years = sorted({y for b in agg.values() for y in b})
    report = {"generated_at": datetime.now().isoformat(), "symbols": len(syms),
              "fetched": fetched, "failed": failed, "start": start, "end": end,
              "buckets": {}, "verdict": {}}
    print(f"\n覆盖: {fetched}/{len(syms)} 标的 (失败 {len(failed)}: {failed[:6]}{'...' if len(failed) > 6 else ''})")
    print(f"\n{'桶':<8} {'年':<6} {'n':>7} {'动量跟随均值%':>12} {'日内波幅均值%':>12}")
    for name, _, _ in BUCKETS:
        by_year = agg.get(name, {})
        report["buckets"][name] = {}
        for y in years:
            s = by_year.get(y)
            if not s or s["n"] < 30:
                continue
            f_avg, r_avg = s["follow"] / s["n"], s["rng"] / s["n"]
            report["buckets"][name][y] = {"n": s["n"], "follow_avg_pct": round(f_avg, 4),
                                          "range_avg_pct": round(r_avg, 3)}
            print(f"{name:<8} {y:<6} {s['n']:>7} {f_avg:>12.4f} {r_avg:>12.3f}")

    # 判据: 3-4 桶 follow 是否逐年 ≥ 2.5-3 桶
    wins, total = 0, 0
    for y in years:
        a = report["buckets"].get("3.0-4.0", {}).get(y)
        b = report["buckets"].get("2.5-3.0", {}).get(y)
        if a and b:
            total += 1
            if a["follow_avg_pct"] > b["follow_avg_pct"]:
                wins += 1
    structural = total >= 4 and wins >= total - 1  # 允许 1 年例外
    report["verdict"] = {
        "criterion": "3.0-4.0 桶动量跟随 ≥4 年优于 2.5-3.0 桶 (容 1 年例外)",
        "years_compared": total, "years_won": wins, "structural": structural,
        "implication": ("ATR 甜蜜区可引用为结构性规律" if structural else
                        "ATR 门精调降级为近期市况参数, 终裁不得引用为结构规律"),
    }
    print(f"\n[预注册判据] 3-4桶 vs 2.5-3桶: {wins}/{total} 年占优 → "
          f"{'✅ 结构性' if structural else '❌ 非结构性 (近期市况参数)'}")
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"落盘: {OUT_PATH}")


if __name__ == "__main__":
    main()
