#!/usr/bin/env python3
"""被 bridge/门掐掉的「看空评级」反事实评估 (short_funnel_shadow 裁决辅助, 2026-10-31).

回答的问题 (用户 2026-10-31 裁「不立门, 先做一次回测」):
    看空评级却**没出空头指令**的那批标的, 若当日开盘就放行做空, T+1/T+3/T+5 是赚是亏?
    即: bridge/通道候选层掐掉的空头是"拦对了"还是"误杀"。

为什么需要它: `scripts/short_funnel_shadow_eval.py` 只给**计数** (61 只被掐 / 原因分布),
给不出**期望**。计数大 ≠ 该开门 — 开门前必须先有"放行会赚"的读数 (用户 8/14 口径:
历史可行直接回测, 不默认攒样)。

数据源 (全部已存在, 零新采集线):
  - logs/da_pipeline_stderr.log[-YYYYMMDD][.gz]  → 当日 DA 评级 (与 short_funnel_shadow_tag 同解析式)
    ⚠️ 该 jsonl 的 `rated_bearish_syms` 只存前 10 个 ⇒ **不能用 jsonl 做 symbol 级回测**, 必须重解析日志。
  - data/ftseek_us.db::playbook_instructions      → 当日该标的的指令/skip_reason (为何没出空头)
  - data/replay_bars_daily/US_<SYM>.json          → 日K [date, open, high, low, close] (只读缓存)

口径 (沿用既有尺子, 不新造约定):
  锚 = 当日开盘价 (DA 盘前评级, 若放行入场发生在开盘段; 与 da_blocked_signal_cf_eval.py 同锚)。
  方向带符号收益: SHORT ⇒ signed = -(exit/entry - 1) (正 = 做空会赚)。
  前瞻窗: T+1 (主) / T+3 / T+5 (辅, 只作一致性披露, 不参与判读, 防多窗拣选)。
  (date, symbol) 去重; 右缘无 bar 的显式计 censored, 不静默丢弃。
  费用线 **沿用** B14 预注册的 ±0.15% (DEB-20260901 Phase C), 本脚本不自创阈值。

判据 (本批只出读数, 不改生产):
  n<30                                        ⇒ INSUFFICIENT_N (不下结论)
  mean_signed(T+1) ≥ +0.15%                   ⇒ OVER_BLOCKING_SUSPECT (放行有正期望, 误杀嫌疑)
  mean_signed(T+1) ≤ -0.15%                   ⇒ BLOCK_CORRECT      (拦对了)
  其余                                         ⇒ UNRESOLVED        (费用线内, 不可分辨)
  ⚠️ 只读零副作用: 不改交易行为/不写库/不推送/不下单。产物 = data/shadow/short_blocked_cf_latest.json。
用法: PYTHONPATH=. venv/bin/python scripts/short_blocked_cf_eval.py [--since 2026-08-14]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
DAILY_DIR = ROOT / "data" / "replay_bars_daily"
LOG_DIR = ROOT / "logs"
OUT = ROOT / "data" / "shadow" / "short_blocked_cf_latest.json"

FEE_LINE_PCT = 0.15          # 沿用 B14 预注册费用线 (不新造)
MIN_N = 30                   # 预注册样本门 (同族惯例)
WINDOWS = (1, 3, 5)          # T+1 主窗, T+3/T+5 只作一致性

BEARISH = ("看空", "强烈看空")

_RE_SYM = re.compile(r"深度分析 \d+/\d+: (US\.\w+)")
_RE_RATING = re.compile(r"评级: (\S+) \(([\d.]+)/10\)")


def _open_text(p: Path):
    return gzip.open(p, "rt", errors="ignore") if p.suffix == ".gz" else p.open(errors="ignore")


def parse_da_log(path: Path) -> dict[str, str]:
    """DA stderr → {symbol: rating} (与 short_funnel_shadow_tag._parse_da_log 同解析式)。"""
    ratings: dict[str, str] = {}
    cur = None
    with _open_text(path) as fh:
        for ln in fh:
            m = _RE_SYM.search(ln)
            if m:
                cur = m.group(1)
                continue
            m2 = _RE_RATING.search(ln)
            if m2 and cur:
                ratings[cur] = m2.group(1)
                cur = None
    return ratings


def log_date(path: Path) -> str | None:
    """rotation 文件名日期 = 次日 (装的是前一日那轮); 无后缀 = 当天。"""
    m = re.search(r"log-(\d{8})", path.name)
    if m:
        return (datetime.strptime(m.group(1), "%Y%m%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def load_bars(symbol: str):
    p = DAILY_DIR / f"{symbol.replace('.', '_')}.json"
    if not p.exists():
        return None
    try:
        rows = json.loads(p.read_text())
    except Exception:                                             # noqa: BLE001
        return None
    out = {}
    for r in rows:
        try:
            out[str(r[0])] = (float(r[1]), float(r[4]))           # (open, close)
        except (TypeError, ValueError, IndexError):
            continue
    return out or None


def classify(reason: str) -> str:
    r = (reason or "").strip()
    if not r:
        return "无指令(skip_reason 空)"
    if "非当前通道候选" in r or "bridge" in r.lower():
        return "bridge/通道候选未提名"
    if "entry_confirm_reversed" in r:
        return "entry_confirm 2min门弃单"
    if "admission" in r:
        return "admission 价量门"
    if "sector" in r:
        return "sector 集中度"
    return "其他: " + r[:40]


def sign_test_p(k: int, n: int) -> float:
    """单侧符号检验 P(X>=k | p=0.5)。"""
    if n <= 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def collect(since: str) -> list[dict]:
    """→ 每笔 (date, symbol, reason_bucket, bars) 的反事实样本。"""
    conn = sqlite3.connect(str(DB), timeout=15)
    conn.row_factory = sqlite3.Row
    samples: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for lf in sorted(LOG_DIR.glob("da_pipeline_stderr.log*")):
        d = log_date(lf)
        if d is None:
            continue
        if d < since:
            continue
        ratings = parse_da_log(lf)
        bearish = sorted({s for s, r in ratings.items() if r in BEARISH})
        if not bearish:
            continue
        rows = conn.execute(
            "SELECT symbol, direction, executed, skip_reason FROM playbook_instructions "
            "WHERE substr(created_at,1,10)=?", (d,)).fetchall()
        short_syms = {r["symbol"] for r in rows if r["direction"] == "SELL_SHORT"}
        by_sym: dict[str, list] = defaultdict(list)
        for r in rows:
            by_sym[r["symbol"]].append(r)
        for sym in bearish:
            if sym in short_syms:
                continue                     # 出了空头指令 ⇒ 不在"被掐"人群
            if (d, sym) in seen:
                continue
            seen.add((d, sym))
            reasons = [classify(r["skip_reason"]) for r in by_sym.get(sym, [])]
            bucket = reasons[0] if reasons else "无指令(skip_reason 空)"
            bars = load_bars(sym)
            samples.append({"date": d, "symbol": sym, "bucket": bucket, "bars": bars,
                            "raw_reasons": [r["skip_reason"] for r in by_sym.get(sym, [])]})
    return samples


def cf_stats(samples: list[dict]) -> dict:
    """按桶 × 窗 算带符号收益 (SHORT ⇒ 正=放行会赚)。"""
    res: dict[str, dict] = {}
    for h in WINDOWS:
        agg: dict[str, list[float]] = defaultdict(list)
        censored = Counter()
        for s in samples:
            bars = s["bars"]
            if not bars:
                censored[s["bucket"]] += 1
                continue
            dates = sorted(bars)
            try:
                i = dates.index(s["date"])
            except ValueError:
                censored[s["bucket"]] += 1      # 当日无 bar (停牌/未缓村)
                continue
            if i + h >= len(dates):
                censored[s["bucket"]] += 1      # 右缘删失
                continue
            entry = bars[s["date"]][0]
            exit_ = bars[dates[i + h]][1]
            if not entry or entry <= 0:
                censored[s["bucket"]] += 1
                continue
            agg[s["bucket"]].append(-(exit_ / entry - 1.0) * 100.0)
        per = {}
        for b, vals in agg.items():
            n = len(vals)
            mean = sum(vals) / n if n else 0.0
            sv = sorted(vals)
            wins = sum(1 for v in vals if v > 0)
            per[b] = {
                "n": n,
                "mean_pct": round(mean, 4),
                "median_pct": round(sv[n // 2], 4) if n else 0.0,
                "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
                "sign_p_pos": round(sign_test_p(wins, n), 6) if n else 1.0,
                "censored": censored.get(b, 0),
                "verdict": _verdict(n, mean),
            }
        res[f"T+{h}"] = per
    return res


def _verdict(n: int, mean: float) -> str:
    if n < MIN_N:
        return "INSUFFICIENT_N"
    if mean >= FEE_LINE_PCT:
        return "OVER_BLOCKING_SUSPECT"
    if mean <= -FEE_LINE_PCT:
        return "BLOCK_CORRECT"
    return "UNRESOLVED"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-08-14")
    a = ap.parse_args()

    samples = collect(a.since)
    stats = cf_stats(samples)
    buckets = sorted({s["bucket"] for s in samples})

    print("=" * 78)
    print("被掐「看空评级」反事实 (SHORT @ 当日开盘, 正=放行会赚; 费用线 ±%.2f%%, n门 %d)"
          % (FEE_LINE_PCT, MIN_N))
    print(f"样本: {len(samples)} 笔 (date×symbol 去重), 窗口 since {a.since}")
    print("=" * 78)
    for b in buckets:
        print(f"\n── {b} ──")
        for h in WINDOWS:
            s = stats[f"T+{h}"].get(b)
            if not s or not s["n"]:
                continue
            tag = "主窗" if h == 1 else "披露"
            print(f"  T+{h}({tag}) n={s['n']:3d} 均{s['mean_pct']:+7.3f}% 中位{s['median_pct']:+7.3f}%"
                  f" WR={s['win_rate_pct']:5.1f}% p_pos={s['sign_p_pos']:.4f}"
                  f" 删失{s['censored']} ⇒ {s['verdict']}")

    # 全人群 (所有桶合并) 主窗读数 — 开门问题的正面回答
    merged = []
    for s in samples:
        bars = s.get("bars")
        if not bars:
            continue
        dates = sorted(bars)
        if s["date"] not in dates:
            continue
        i = dates.index(s["date"])
        if i + 1 >= len(dates):
            continue
        entry = bars[s["date"]][0]
        if entry and entry > 0:
            merged.append(-(bars[dates[i + 1]][1] / entry - 1.0) * 100.0)
    print("\n" + "─" * 78)
    if merged:
        n = len(merged)
        mean = sum(merged) / n
        wins = sum(1 for v in merged if v > 0)
        print(f"全人群 T+1: n={n} 均{mean:+.3f}% WR={wins / n * 100:.1f}% "
              f"p_pos={sign_test_p(wins, n):.4f} ⇒ {_verdict(n, mean)}")
    else:
        print("全人群 T+1: 无可算样本")
    print("⚠️ 只印不判: 本脚本不改生产行为; 结论供用户裁「要不要为被掐掉的看空单开门」。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "since": a.since, "n_samples": len(samples),
        "fee_line_pct": FEE_LINE_PCT, "min_n": MIN_N,
        "stats": stats,
        "buckets": {b: sum(1 for s in samples if s["bucket"] == b) for b in buckets},
        "note": "SHORT @ 当日 open 锚, T+1 主窗; 费用线沿用 B14 预注册 ±0.15%; 只印不判",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"结果落盘: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())