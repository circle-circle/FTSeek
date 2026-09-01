#!/usr/bin/env python3
"""DA 门被拦方向性信号前瞻评估 (DEB-20260901 Phase C 观察点③, 判据字典 B14).

回答一个问题: DA 质量门拦掉的方向性候选 (direction∈BUY/SELL_SHORT),
若放行入场, 前瞻是赚是亏? — 即门是"有效拦截"还是"过度拦截"。
导火索: 8/31 DA 门拦 22 信号中 19 个 (86%), "拦太狠"自警 + BABA/BWXT
被 $3000 地板误杀实锤 → 需要系统性反事实证据而非个案叙事。

数据源 (全部已存在, 零新采集线):
  - data/da_gate_decisions.jsonl (7/13 起; 只取 decision=BLOCK 且
    direction∈{BUY,SELL_SHORT} 行 — direction=SKIP 的 858 行非方向性, 排除)
  - data/replay_bars_daily/US_<SYM>.json 日K (refresh_daily_bars AFTER_HOURS
    增量刷新, 只读缓存不补拉 — 补拉是 scheduler AFTER_HOURS 的职责, 免重复打 Futu)

口径 (对齐 gate_counterfactual_ruler.py, 不新造约定):
  锚日 = trade_date; 锚价 = 锚日开盘价 (门在盘前 ~09:00 ET 裁决, 若放行
  入场发生在当日开盘段, open 是最忠实代理; 无价字段可用, 不虚构)。
  方向带符号收益: sign=+1 BUY / -1 SELL_SHORT (ic_tracker 同约定, 正=该方向会赚)。
  前瞻窗: T+1 (主) / T+3 / T+5 (辅, 一致性参考); 右缘删失显式计数不静默丢弃。
  (trade_date, symbol, direction) 去重 — 同日同标的重复拦只计 1 样本。

预注册判据 (B14, 2026-09-01 立项, 主窗 T+1, n_eval≥50 才出裁决):
  mean_signed ≤ -0.15% (费用线) ∧ p_under ≤ 0.05 → GATE_EFFECTIVE (拦得对, 维持)
  mean_signed ≥ +0.15% ∧ p_over ≤ 0.05            → OVER_BLOCKING_SUSPECT (误杀, 提案另立DEB诊断)
  其余 → KEEP_SAMPLING (继续攒样不重置); n_eval<50 一律 KEEP_SAMPLING。
  T+3/T+5 仅作一致性披露, 不参与裁决 (防多窗拣选)。

只读输入, 幂等 (同输入同输出); 自身产物 = latest 快照 + history 追加。
挂载: cron 15 6 * * 2-6 (盘后日K刷新完成)。回滚 = 删 cron 行 + 本脚本 + data/shadow/da_blocked_cf_*。
"""
from __future__ import annotations

import json
from datetime import datetime
from math import comb
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE_JSONL = REPO / "data" / "da_gate_decisions.jsonl"
DAILY_DIR = REPO / "data" / "replay_bars_daily"
OUT_LATEST = REPO / "data" / "shadow" / "da_blocked_cf_latest.json"
OUT_HISTORY = REPO / "data" / "shadow" / "da_blocked_cf_history.jsonl"

HORIZONS = (1, 3, 5)
PRIMARY_HORIZON = 1
MIN_N = 50
FEE_LINE_PCT = 0.15
SIGN_P = 0.05


def sign_test_one_sided(k: int, n: int) -> float:
    """P(X >= k | Binomial(n, 0.5)) — 单侧 (k 为正向笔数)。"""
    if n <= 0:
        return 1.0
    return sum(comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def _sign(direction: str) -> int | None:
    d = (direction or "").upper()
    if d == "BUY":
        return 1
    if d == "SELL_SHORT":
        return -1
    return None


def _load_bars(symbol: str):
    """[[date,o,h,l,c]] 升序; 只读缓存, 缺失返回 None (计删失)。"""
    cp = DAILY_DIR / f"{symbol.replace('.', '_')}.json"
    if not cp.exists():
        return None
    try:
        rows = json.loads(cp.read_text())
        return rows or None
    except Exception:
        return None


def _loads(line: str):
    """jsonl 行解析: 失败返回 None (留痕口径同 gap_alignment_shadow, 不静默吞)。"""
    try:
        return json.loads(line)
    except Exception:
        return None


def load_blocked_signals() -> list[dict]:
    """方向性 BLOCK 行, (trade_date, symbol, direction) 去重 (保留最早裁决)。"""
    if not GATE_JSONL.exists():
        return []
    seen: set[tuple] = set()
    out = []
    bad_lines = 0
    for line in GATE_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = _loads(line)
        if r is None:
            bad_lines += 1
            continue
        if r.get("decision") != "BLOCK":
            continue
        if _sign(r.get("direction", "")) is None:  # SKIP/空方向行排除
            continue
        key = (r.get("trade_date"), r.get("symbol"), r.get("direction"))
        if key in seen or not all(key):
            continue
        seen.add(key)
        out.append(r)
    if bad_lines:
        print(f"[da_blocked_cf] WARNING: da_gate_decisions.jsonl 解析失败 {bad_lines} 行已跳过")
    out.sort(key=lambda r: r.get("datetime", ""))
    return out


def evaluate(signals: list[dict]) -> dict:
    stats = {h: {"n_eval": 0, "wins": 0, "signed": []} for h in HORIZONS}
    censored = {"no_bars": 0, "no_anchor": 0, "window": {h: 0 for h in HORIZONS}}
    by_quality = {"le30": [], "31to60": [], "gt60": []}
    by_direction = {"BUY": [], "SELL_SHORT": []}
    detail = []

    for r in signals:
        sym, date, direction = r["symbol"], r["trade_date"], r["direction"]
        sign = _sign(direction)
        bars = _load_bars(sym)
        if not bars:
            censored["no_bars"] += 1
            continue
        idx = next((i for i, b in enumerate(bars) if b[0] == date), None)
        if idx is None:
            censored["no_anchor"] += 1
            continue
        anchor_open = float(bars[idx][1])
        if anchor_open <= 0:
            censored["no_anchor"] += 1
            continue

        row_cf = {"date": date, "symbol": sym, "direction": direction,
                  "quality_score": r.get("quality_score"), "anchor_open": anchor_open}
        any_eval = False
        for h in HORIZONS:
            j = idx + h
            if j >= len(bars):
                censored["window"][h] += 1
                row_cf[f"cf_t{h}_pct"] = None
                continue
            cf = sign * (float(bars[j][4]) / anchor_open - 1) * 100
            s = stats[h]
            s["n_eval"] += 1
            s["wins"] += 1 if cf > 0 else 0
            s["signed"].append(cf)
            row_cf[f"cf_t{h}_pct"] = round(cf, 3)
            any_eval = True
        if any_eval:
            detail.append(row_cf)
            q = r.get("quality_score")
            qb = ("le30" if (q or 0) <= 30 else "31to60" if (q or 0) <= 60 else "gt60")
            by_quality[qb].append(row_cf.get(f"cf_t{PRIMARY_HORIZON}_pct"))
            by_direction[direction].append(row_cf.get(f"cf_t{PRIMARY_HORIZON}_pct"))

    horizons_report = {}
    for h in HORIZONS:
        s = stats[h]
        n = s["n_eval"]
        entry = {"n_eval": n, "censored_window": censored["window"][h]}
        if n:
            signed = s["signed"]
            mean = sum(signed) / n
            entry.update({
                "wins": s["wins"],
                "win_rate": round(s["wins"] / n, 3),
                "mean_signed_pct": round(mean, 3),
                "median_signed_pct": round(sorted(signed)[n // 2], 3),
                # p_over: 正向笔数显著多 → 放行会赚 → 过度拦截嫌疑
                "p_over": round(sign_test_one_sided(s["wins"], n), 4),
                # p_under: 负向笔数显著多 → 放行会亏 → 门拦得对
                "p_under": round(sign_test_one_sided(n - s["wins"], n), 4),
            })
        horizons_report[f"T+{h}"] = entry

    # 预注册裁决 — 仅主窗 T+1, 防多窗拣选
    prim = horizons_report[f"T+{PRIMARY_HORIZON}"]
    n_eval = prim.get("n_eval", 0)
    if n_eval < MIN_N:
        verdict, reason = "KEEP_SAMPLING", f"n_eval={n_eval}<{MIN_N} 攒样中"
    else:
        mean = prim["mean_signed_pct"]
        if mean <= -FEE_LINE_PCT and prim["p_under"] <= SIGN_P:
            verdict, reason = "GATE_EFFECTIVE", (
                f"T+1 mean={mean:+.3f}%≤-{FEE_LINE_PCT}% ∧ p_under={prim['p_under']}≤{SIGN_P}"
                " → 被拦信号放行总体亏, 门拦得对")
        elif mean >= FEE_LINE_PCT and prim["p_over"] <= SIGN_P:
            verdict, reason = "OVER_BLOCKING_SUSPECT", (
                f"T+1 mean={mean:+.3f}%≥+{FEE_LINE_PCT}% ∧ p_over={prim['p_over']}≤{SIGN_P}"
                " → 被拦信号放行总体赚, 误杀嫌疑, 提案另立DEB诊断")
        else:
            verdict, reason = "KEEP_SAMPLING", (
                f"n_eval={n_eval}≥{MIN_N} 但 T+1 mean={mean:+.3f}% 未越±{FEE_LINE_PCT}%费用线"
                " 或符号检验不显著 → 继续攒样不重置")

    def _qstat(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"n": 0}
        return {"n": len(vals), "mean_signed_pct": round(sum(vals) / len(vals), 3),
                "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 3)}

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_signals": len(signals),
        "n": n_eval,  # 注册表计数锚 (主窗可评估样本)
        "censored": {"no_bars": censored["no_bars"], "no_anchor": censored["no_anchor"],
                     "window": censored["window"]},
        "preregistered": f"B14: 主窗T+1, n_eval≥{MIN_N}, ±{FEE_LINE_PCT}%费用线, "
                         f"单侧符号检验p≤{SIGN_P}; T+3/T+5仅一致性披露",
        "verdict": verdict,
        "verdict_reason": reason,
        "horizons": horizons_report,
        "by_quality_t1": {k: _qstat(v) for k, v in by_quality.items()},
        "by_direction_t1": {k: _qstat(v) for k, v in by_direction.items()},
        "detail": detail,
    }


def main() -> int:
    signals = load_blocked_signals()
    report = evaluate(signals)

    OUT_LATEST.parent.mkdir(parents=True, exist_ok=True)
    OUT_LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    with OUT_HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")

    print("═══ DA 门被拦方向性信号前瞻评估 (B14, 判据字典) ═══")
    print("方向性 BLOCK 信号: n=%d (去重后), 删失: no_bars=%d no_anchor=%d" % (
        report["n_signals"], report["censored"]["no_bars"], report["censored"]["no_anchor"]))
    for hz, e in report["horizons"].items():
        if not e.get("n_eval"):
            print("  %s: n_eval=0 (删失=%d)" % (hz, e.get("censored_window", 0)))
            continue
        print("  %s: n=%-3d WR=%5.1f%% mean=%+6.3f%% p_over=%.4f p_under=%.4f (窗口删失=%d)" % (
            hz, e["n_eval"], e["win_rate"] * 100, e["mean_signed_pct"],
            e["p_over"], e["p_under"], e["censored_window"]))
    print("裁决: %s — %s" % (report["verdict"], report["verdict_reason"]))
    print("→ %s" % OUT_LATEST.relative_to(REPO))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
