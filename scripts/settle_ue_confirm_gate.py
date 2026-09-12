#!/usr/bin/env python3
"""UE 开盘确认门 (U11) 影子结算器 (2026-09-12, 用户裁「按建议执行」— 断头传感器修复).

背景: `scripts/llm_universe_scan.py cmd_confirm` (U11, 2026-08-26 用户裁) 每交易日
开盘+10min 对当日盘前浮现候选裁决 PRICE_IN / CONFIRM / REVERSED / NO_QUOTE,
落 `data/shadow/ue_confirm_gate.jsonl` (Phase 1 = 影子, 不拦 21:10 inject 链)。
8/26-9/11 共 11 个交易日 78 条裁决 — **台账零前瞻收益**: verdict 行只有
signed_pre_move (盘前动量), 无 fwd 字段, 而预注册判据要的是"前瞻" ⇒ 判据
永远算不出 = 死影子 (与 8/28 "传感器静默零产出" / 9/11 premarket_invalidation
"31天310行零结算" 同款病灶)。本脚本是缺失的判分侧: 纯读, 零写入影子文件,
零交易行为变更。

前瞻收益源 (不重复造轮子): `data/signal_snapshots.db signal_snapshots` 表,
join 键 = decision_id = `universe_expansion:{date}:{symbol}` (UE 注入行同键,
llm_universe_scan.py:1880 契约)。`fwd_ret_t1` 各列**已经是方向签名后**的指令
收益 (U1 契约 2026-09-11, llm_universe_scan.py:1131; signal_ic_tracker.py:340
LONG=(t1/t0)-1, SHORT=(t0/t1)-1) — 直接按桶求均值即"该桶方向性前瞻",
正=该桶方向对了。回填由既有 run_signal_ic_backfill (pipeline.py:3116, T+1 收盘
后自动) 负责, 本器只读不算价, 缺 fwd 的行计入 censored 不静默丢。

判据 (预注册, 逐字抄自 config/unified_config.yaml universe_expansion.confirm_gate
注释 + jsonl note 字段, 2026-08-26 见数前写死):
    ≥10 交易日 ∧ 被裁决 n≥30 ∧ PRICE_IN 桶前瞻 ≤ 0 ∧ CONFIRM 桶前瞻 > 0
    → READY_TO_ADJUDICATE (Phase 2 接线为 inject 前置闸; **提请另裁, 不自动生效**)
    任一不满足 → KEEP_SAMPLING。
NO_QUOTE / REVERSED 桶只计数呈报, 不参与判据 (预注册未指定)。

强制披露 (DEB-20260909 §5.2 同族先验, 随每次汇总输出):
  ① confirm_gate 反向先验: D2' 影子披露的"等+10/+30min再确认"8轮实证方向92%不变
     但代价+0.50%/笔 (t=5.67) — 延迟确认机制在本系统有成本前科。转正裁决时须
     对照该先验, 不因单桶符号翻正就跳过成本核算。
  ② n<30 或交易日<10 的桶读数一律标注 n_lt_30_flag, 只作方向参考 (宪章七律⑤)。

挂载: crontab 4 6 * * 2-6 (premarket_invalidation 2 6 结算后同班; 本器只依赖
signal_snapshots 已回填行, T+1 fwd 在 05:32 批次后可用)。
产物: data/shadow/ue_confirm_gate_report_latest.json (atomic write)。
回滚: 删本脚本 + cron 行 + 产物文件。

用法: venv/bin/python scripts/settle_ue_confirm_gate.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "data" / "shadow" / "ue_confirm_gate.jsonl"
SNAP_DB = ROOT / "data" / "signal_snapshots.db"
OUT = ROOT / "data" / "shadow" / "ue_confirm_gate_report_latest.json"

# 预注册判据常量 (见文件头, 改这里=改判据=须先改 config 注释/jsonl note 留痕)
MIN_DAYS = 10
MIN_N = 30
JUDGED_BUCKETS = ("PRICE_IN", "CONFIRM")   # 参与判据的两桶
REPORTED_BUCKETS = ("PRICE_IN", "CONFIRM", "REVERSED", "NO_QUOTE")

_DISCLOSURES = (
    "①confirm_gate 反向先验: D2'影子披露 8轮实证「等+10/+30min再确认」方向92%不变"
    "但代价+0.50%/笔(t=5.67) — 延迟确认在本系统有成本前科, 转正须核算成本不只符号; "
    "②n<30/交易日<10 的桶读数标 n_lt_30_flag, 只作方向参考(宪章七律⑤); "
    "③fwd_ret_t1 为方向签名后指令收益(signal_snapshots U1契约), 正=该桶方向对; "
    "缺 fwd 行计 censored 不静默丢。"
)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_verdicts() -> list:
    """展开 jsonl 每行 verdicts → 逐候选行 (带 date)。"""
    out = []
    if not SHADOW.exists():
        return out
    for line in SHADOW.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "confirm":
            continue
        d = rec.get("date")
        for v in rec.get("verdicts") or []:
            row = dict(v)
            row["date"] = d
            out.append(row)
    return out


def _load_fwd() -> dict:
    """decision_id → fwd_ret_t1 (只取 UE 注入行, ro 连接)。"""
    fwd: dict = {}
    if not SNAP_DB.exists():
        return fwd
    conn = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        for did, f in conn.execute(
                "SELECT decision_id, fwd_ret_t1 FROM signal_snapshots "
                "WHERE decision_id LIKE 'universe_expansion:%'"):
            fwd[did] = f
    finally:
        conn.close()
    return fwd


def _mean(xs):
    return (sum(xs) / len(xs)) if xs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="只打 JSON 不写文件")
    args = ap.parse_args()

    vs = _load_verdicts()
    fwd = _load_fwd()

    days = sorted({v.get("date") for v in vs if v.get("date")})
    buckets = {b: {"fwd": [], "censored": 0, "n": 0} for b in REPORTED_BUCKETS}
    joined = 0
    for v in vs:
        b = v.get("verdict")
        if b not in buckets:
            continue
        buckets[b]["n"] += 1
        did = f"universe_expansion:{v.get('date')}:{v.get('symbol')}"
        f = fwd.get(did)
        if f is None:
            buckets[b]["censored"] += 1
        else:
            buckets[b]["fwd"].append(float(f))
            joined += 1

    stats = {}
    for b, agg in buckets.items():
        m = _mean(agg["fwd"])
        med = None
        if agg["fwd"]:
            _s = sorted(agg["fwd"])
            _n = len(_s)
            med = (_s[_n // 2] if _n % 2 else (_s[_n // 2 - 1] + _s[_n // 2]) / 2) * 100
        stats[b] = {
            "n_verdicts": agg["n"],
            "n_with_fwd": len(agg["fwd"]),
            "censored_no_fwd": agg["censored"],
            "fwd_ret_t1_mean_pct": round(m * 100, 4) if m is not None else None,
            "fwd_ret_t1_median_pct": round(med, 4) if med is not None else None,
            "n_lt_30_flag": len(agg["fwd"]) < MIN_N,
        }

    n_days = len(days)
    pi = stats["PRICE_IN"]
    cf = stats["CONFIRM"]
    pi_m = pi["fwd_ret_t1_mean_pct"]
    cf_m = cf["fwd_ret_t1_mean_pct"]

    reasons = []
    gate_days = n_days >= MIN_DAYS
    gate_n = (pi["n_with_fwd"] + cf["n_with_fwd"]) >= MIN_N
    if not gate_days:
        reasons.append(f"交易日 {n_days} < {MIN_DAYS}")
    if not gate_n:
        reasons.append(f"被裁决(有fwd) n={pi['n_with_fwd']+cf['n_with_fwd']} < {MIN_N}")
    gate_pi = (pi_m is not None and pi_m <= 0)
    gate_cf = (cf_m is not None and cf_m > 0)
    if pi_m is None:
        reasons.append("PRICE_IN 桶无 fwd 读数")
    elif not gate_pi:
        reasons.append(f"PRICE_IN 前瞻 {pi_m:+.4f}% > 0 (判据要 ≤0)")
    if cf_m is None:
        reasons.append("CONFIRM 桶无 fwd 读数")
    elif not gate_cf:
        reasons.append(f"CONFIRM 前瞻 {cf_m:+.4f}% ≤ 0 (判据要 >0)")

    verdict = "READY_TO_ADJUDICATE" if (
        gate_days and gate_n and gate_pi and gate_cf) else "KEEP_SAMPLING"

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "gate": "ue_confirm_gate (U11)",
        "phase": "shadow (Phase 1, 不拦 inject 链)",
        "preregistered": ("≥10交易日 ∧ n≥30 ∧ PRICE_IN前瞻≤0 ∧ CONFIRM前瞻>0 → "
                          "READY_TO_ADJUDICATE (另裁不自动生效); 见 unified_config.yaml "
                          "universe_expansion.confirm_gate + jsonl note"),
        "fwd_source": ("signal_snapshots.fwd_ret_t1 (方向签名后指令收益, "
                       "decision_id=universe_expansion:{date}:{symbol} join)"),
        "n_trading_days": n_days,
        "date_range": [days[0], days[-1]] if days else [],
        "n_verdicts_total": len(vs),
        "n_joined_fwd": joined,
        "buckets": stats,
        "gate_checks": {
            "days_ok": gate_days, "n_ok": gate_n,
            "price_in_le_0": gate_pi, "confirm_gt_0": gate_cf,
        },
        "verdict": verdict,
        "reasons": reasons,
        "disclosures": _DISCLOSURES,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if not args.json:
        _atomic_write(OUT, text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
