#!/usr/bin/env python3
"""「弃单再生」可行性只读回放 (2026-10-26, RFC-171 提案证据).

回答三个问题, 全部只用**弃单时点可得**的信息:
  Q1 同向再生 (延迟后按市价/回退限价再进) 是否为正?
     → 由 scripts/entry_second_chance_replay.py 回答 (已跑; 结论负), 本脚本不重复。
  Q2 反向 (把弃单当"趋势确认") 的 drift 是否足以覆盖费用?
  Q3 弃单集合的 forward drift 能否被某个时点特征分层 (archetype / 逆向幅度 /
     方向 / 开盘窗 / 计划锚偏离 / 决策置信) —— 即"真正的机会"是否可识别?

预注册判据 (读数前写死, 只在实现完成后才看输出):
  P1 反向可用: 反向 60min 净 drift ≥ +0.30%/笔 ∧ 顺向占比 ≥ 55% ∧ n ≥ 40
  P2 可分层: 存在一个子集 (n≥15) 其同向净 drift ≥ +0.15%/笔 且比补集高 ≥0.50pp
             ⇒ 该子集可作为"再生白名单"候选 (仍需影子前向验)
  P3 否则: 弃单集合 = 纯负信号 ⇒ 只进 veto/质量特征, 不建交易通道

口径与局限 (如实):
  - bar = 单日档 data/replay_bars/US_<SYM>_<date>.json (390 根 RTH) 优先, 多日档兜底;
    **必须同日** (日期不符标 UNMEASURABLE 排除)。
  - 入场价 = 弃单时点的 bar close (近似 px_at_confirm); 出场 = 固定 horizon 末根 close
    (TIME 口径, 不建模 BE/trailing —— 与 sibling 脚本同尺, 便于横向比较)。
  - 费用 10bps 往返 (5bps/腿, 与生产 fees_est 同量级)。
  - 不做 SL/TP 建模: 目的是看 drift 符号与分层, 不是复刻生产退出。
纯只读: 不写 playbook_instructions、不下单、不改门。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
FWD = ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl"
DAY_BAR_DIR = ROOT / "data" / "replay_bars"
BAR_DIRS = (ROOT / "data" / "replay_bars_1m", ROOT / "data" / "replay_bars_1m_extra")
FEE_ROUND_TRIP_BPS = 10.0
CALIB_WARN = 0.02


def _bars(symbol: str, date_str: str) -> Optional[list[tuple]]:
    stem = symbol.replace(".", "_")
    cands = [DAY_BAR_DIR / f"{stem}_{date_str}.json"]
    cands += [d / f"{stem}.json" for d in BAR_DIRS]
    for path in cands:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        out = []
        for row in raw:
            try:
                out.append((datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S"),
                            float(row[1]), float(row[2]), float(row[3]), float(row[4])))
            except Exception:
                continue
        if out:
            return out
    return None


def _row_at_or_before(bars, stamp):
    found = None
    for b in bars:
        if b[0] <= stamp:
            found = b
        else:
            break
    return found


def _bar_at_or_after(bars, stamp_offset_min: int, base_ts):
    target = base_ts.timestamp() + stamp_offset_min * 60
    for b in bars:
        if b[0].timestamp() >= target:
            return b
    return None


def _inst_meta() -> dict:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, symbol, direction, stop_loss, take_profit, atr_pct, vol_bucket,"
            " decision_conf, raw_instruction_json FROM playbook_instructions"
        ).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        d = dict(r)
        try:
            raw = json.loads(d.get("raw_instruction_json") or "{}")
        except Exception:
            raw = {}
        d["planned_entry"] = raw.get("planned_entry")
        d["entry_archetype"] = raw.get("entry_archetype") or ""
        out[int(r["id"])] = d
    return out


def _load_abandoned() -> list:
    rows = []
    for line in FWD.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("type") == "entry_confirm" and r.get("outcome") == "ABANDONED":
            rows.append(r)
    return rows


def _drift_pct(direction: str, entry: float, exit_: float) -> float:
    if direction == "LONG":
        return (exit_ - entry) / entry * 100.0
    return (entry - exit_) / entry * 100.0


def _stats(vals: list) -> dict:
    if not vals:
        return {"n": 0}
    m = st.mean(vals)
    sd = st.pstdev(vals) if len(vals) > 1 else 0.0
    t = m / (sd / (len(vals) ** 0.5)) if sd > 0 and len(vals) > 1 else None
    return {"n": len(vals), "mean": round(m, 4), "median": round(st.median(vals), 4),
            "win_pct": round(sum(1 for x in vals if x > 0) / len(vals) * 100, 1),
            "t": None if t is None else round(t, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", default="15,30,60")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]

    insts = _inst_meta()
    episodes, skip = [], {"no_bars": 0, "stale_bars": 0, "caliber_bad": 0, "no_inst": 0}
    for row in _load_abandoned():
        inst = insts.get(int(row.get("inst_id") or 0))
        if not inst:
            skip["no_inst"] += 1
            continue
        symbol = str(row.get("symbol") or "")
        ts = datetime.fromisoformat(str(row["ts"])).replace(tzinfo=None)
        bars = _bars(symbol, ts.strftime("%Y-%m-%d"))
        if not bars:
            skip["no_bars"] += 1
            continue
        bar = _row_at_or_before(bars, ts)
        ref = float(row.get("px_at_confirm") or 0)
        if bar is None or ref <= 0:
            skip["caliber_bad"] += 1
            continue
        if bar[0].date() != ts.date():
            skip["stale_bars"] += 1
            continue
        if abs(bar[4] - ref) / ref > CALIB_WARN:
            skip["caliber_bad"] += 1
            continue
        direction = "SHORT" if str(row.get("direction") or "").upper().startswith("S") else "LONG"
        fwd = {}
        for h in horizons:
            b = _bar_at_or_after(bars, h, bar[0])
            fwd[h] = None if b is None else _drift_pct(direction, bar[4], b[4])
        planned = inst.get("planned_entry")
        try:
            planned = float(planned) if planned else None
        except (TypeError, ValueError):
            planned = None
        episodes.append({
            "date": row["date"], "symbol": symbol, "direction": direction,
            "session_min": row.get("session_min"), "opp": float(row.get("opp_cost_pct") or 0),
            "archetype": inst.get("entry_archetype") or "未标注",
            "conf": inst.get("decision_conf"), "atr_pct": inst.get("atr_pct"),
            "vol_bucket": inst.get("vol_bucket"),
            "plan_gap_pct": (None if not planned else round((ref - planned) / planned * 100, 3)),
            "fwd": fwd,
        })

    res = {"generated_at": datetime.now().isoformat(timespec="seconds"),
           "cohort": {"abandoned": len(episodes) + sum(skip.values()), "usable": len(episodes),
                      **skip},
           "fee_round_trip_bps": FEE_ROUND_TRIP_BPS,
           "preregistered": {
               "P1_flip_usable": "reverse 60m net >= +0.30%/trade AND win>=55% AND n>=40",
               "P2_subgroup": "same-dir net >= +0.15%/trade AND >= +0.50pp over complement (n>=15)",
               "P3_else": "no channel; abandonment is a negative signal -> veto/quality feature only"}}

    def net(direction_sign: float, h: int) -> list:
        fee = FEE_ROUND_TRIP_BPS / 100.0
        return [direction_sign * e["fwd"][h] - fee for e in episodes if e["fwd"][h] is not None]

    res["horizons"] = {}
    for h in horizons:
        res["horizons"][h] = {"same_dir_net": _stats(net(1.0, h)),
                              "reverse_net": _stats(net(-1.0, h))}

    # Q3 分层 (只看同向净 drift —— "再生"意味着同向再进)
    def bucket(fn, h=60):
        groups = {}
        for e in episodes:
            if e["fwd"][h] is None:
                continue
            k = fn(e)
            if k is None:
                continue
            groups.setdefault(k, []).append(e["fwd"][h] - FEE_ROUND_TRIP_BPS / 100.0)
        return {str(k): _stats(v) for k, v in sorted(groups.items(), key=lambda x: str(x[0]))}

    by_mag = bucket(lambda e: ("<0.15%" if abs(e["opp"]) < 0.15 else
                              "0.15-0.5%" if abs(e["opp"]) < 0.5 else ">=0.5%"))
    by_arch = bucket(lambda e: e["archetype"])
    by_side = bucket(lambda e: e["direction"])
    by_min = bucket(lambda e: ("开盘窗<15m" if (e["session_min"] or 0) < 15 else "之后"))
    by_gap = bucket(lambda e: None if e["plan_gap_pct"] is None else
                    ("gap<1%" if abs(e["plan_gap_pct"]) < 1 else "gap>=1%"))
    by_conf = bucket(lambda e: None if e["conf"] is None else
                     ("conf<50" if float(e["conf"]) < 50 else "conf>=50"))
    res["strata"] = {"by_adverse_magnitude": by_mag, "by_archetype": by_arch,
                     "by_direction": by_side, "by_session": by_min,
                     "by_plan_gap": by_gap, "by_confidence": by_conf}

    # P1/P2 判定
    rev60 = res["horizons"].get(60, {}).get("reverse_net", {})
    p1 = bool(rev60.get("n", 0) >= 40 and rev60.get("mean", -9) >= 0.30
              and rev60.get("win_pct", 0) >= 55)
    best = None
    for name, strata in res["strata"].items():
        for k, s in strata.items():
            if s.get("n", 0) >= 15:
                others = [v["mean"] for kk, v in strata.items() if kk != k and v.get("n", 0) >= 5]
                comp = st.mean(others) if others else None
                if comp is not None and s["mean"] >= 0.15 and (s["mean"] - comp) >= 0.50:
                    if best is None or s["mean"] > best["mean"]:
                        best = {"stratum": name, "bucket": k, **s, "complement_mean": round(comp, 4)}
    res["verdict"] = {"P1_flip_usable": p1, "P2_subgroup_found": best,
                      "P3_no_channel": (not p1 and best is None)}

    report = ROOT / "data" / "reports" / "entry_confirm_abandon_reuse.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        c = res["cohort"]
        print(f"cohort: abandoned={c['abandoned']} usable={c['usable']} "
              f"no_bars={c['no_bars']} stale={c['stale_bars']} bad={c['caliber_bad']}")
        for h in horizons:
            s = res["horizons"][h]
            print(f"  {h}min 同向净 {s['same_dir_net']}  |  反向净 {s['reverse_net']}")
        for name, strata in res["strata"].items():
            print(f"  [{name}]")
            for k, s in strata.items():
                print(f"     {k:12s} {s}")
        print("verdict:", json.dumps(res["verdict"], ensure_ascii=False))
        print("report:", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())