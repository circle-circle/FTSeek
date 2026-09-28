#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trail-lock 赢家金字塔影子 — 每晚结算 (DEB-20260928 ①, 2026-09-28 用户裁决落地).

数据源: data/shadow/trail_lock_pyramid.jsonl (盘中 record-only hook,
app/playbook/pyramid_shadow.py 写入) + trade_records/capital_state (sqlite ro)
+ arm 日日K (仅母仓提前退出后的尾部段需要; Futu K_DAY autype=NONE, 1.2s 限流,
缓存 data/replay_bars/pyramid_*)。

结算口径 (DEB① §十三 裁决写死, 改=重裁):
  1. 虚拟腿入场 fill = arm_price ×(1+0.10%) LONG / ×(1−0.10%) SHORT (滑点垫方向感知)
  2. 武装前下行保护 = arm_price (继承母仓武装时刻净保本线, 误差 ≤ fee 量级)
  3. 自身 trail 复用现行参数: 腿浮盈(自 fill)≥0.8% 武装 → 峰值回撤 1.0% 锁利
  4. tick 段精确回放; 母仓退出 tick 之后 → arm 日日K 保守段 (peak 吸收 day high,
     day low ≤ stop 则判 stop 出场=不利优先, 否则 EOD 收盘); 腿不跨日 (EOD 兜底)
  5. 费用 0.15% 往返 × add_notional; 判据只用帽内可执行部分 (add_ratio≤40%),
     无帽 40% 反事实仅诊断列不进判据
  6. cap_usd = equity_at_arm × playbook.risk_parameters.max_position_pct;
     equity_at_arm = capital_state 最近 last_update ≤ arm ts (实测净值, 非固定系数)
  7. 判据 (DEB① §七预注册): n≥40 可执行 ∧ 腿净额中位>0 ∧ Σ>0 ∧
     腿回吐中位 ≤ 母仓回吐中位 ∧ 日聚类 t + MDE(80%) 并报 → 另立 DEB 提请上 sim
  8. 期望量级预注册声明 (§十一): 可执行口径单事件期望 ≈ $0.5–1.5, n=40 总期望
     $20–60, MDE 与可信效应同量级 ⇒ "测不出" ≠ 证伪; 本影子真正回答的决策问题
     是"武装赢家是否值得单名帽让路", 不是"开 40% 加仓"

幂等: 重跑覆盖 latest.json。回滚: 删 cron 行即可, 台账保留。
用法: PYTHONPATH=. venv/bin/python scripts/trail_lock_pyramid_settle.py
"""
import json
import math
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LEDGER = ROOT / "data" / "shadow" / "trail_lock_pyramid.jsonl"
OUT = ROOT / "data" / "shadow" / "trail_lock_pyramid_latest.json"
BAR_CACHE = ROOT / "data" / "replay_bars"
ET = ZoneInfo("America/New_York")

# ── 完整性哨兵 (RFC-107 §3E: 吞错=结论可能建立在残缺样本上) ──
BAD_JSONL: list[int] = []   # 行号
NO_EQUITY: list[str] = []   # symbol@ts
NO_BARS: list[str] = []     # symbol@date
BAD_META: list[int] = []    # trade id

SLIP = 0.001          # 0.10% 滑点垫 (方向感知)
FEE_RT = 0.0015       # 0.15% 往返费线
TRAIL_ARM = 0.8       # 腿自身锁利武装线 % (复用 profit_override_threshold_pct)
TRAIL_DD = 1.0        # 锁利回撤容忍 % (复用 profit_override_drawdown_pct)
ADD_CAP = 0.40        # 加仓上限 (禁改)
N_GATE = 40           # 判据 n 门 (预注册)


def _load_cfg() -> dict:
    import yaml
    uni = yaml.safe_load((ROOT / "config" / "unified_config.yaml").read_text()) or {}
    risk = ((uni.get("playbook") or {}).get("risk_parameters") or {})
    exe = ((uni.get("playbook") or {}).get("execution_parameters") or {})
    return {
        "max_position_pct": float(risk.get("max_position_pct", 0.06) or 0.06),
        "daily_notional_cap_pct": float(exe.get("daily_notional_cap_pct", 0.8) or 0.8),
    }


def _ro() -> sqlite3.Connection:
    from app.core.paths import get_trade_records_db
    con = sqlite3.connect(Path(get_trade_records_db()).as_uri() + "?mode=ro",
                          uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _equity_at(con: sqlite3.Connection, ts: str, arm: dict):
    """arm 事件记录的 equity_at_arm 优先 (hook 在武装时刻实读);
    缺失 → 回退最新单行镜像并打哨兵 (capital_state 无历史, 2026-09-28 实测)。"""
    eq = arm.get("equity_at_arm")
    if eq:
        return float(eq), arm["ts"]
    NO_EQUITY.append(f"{arm['symbol']}@{ts}(latest_fallback)")
    row = con.execute(
        "SELECT current_equity, last_update FROM capital_state "
        "ORDER BY last_update DESC LIMIT 1").fetchone()
    return (float(row["current_equity"]), row["last_update"]) if row else (None, None)


def _day_bar(symbol: str, day: str):
    """arm 日日K [date, open, high, low, close]; 缓存命中优先, 否则 Futu (1.2s 限流)。"""
    fp = BAR_CACHE / f"pyramid_{symbol.replace('.', '_')}_{day}.json"
    if fp.exists():
        try:
            b = json.loads(fp.read_text())
            if b and b[0][0] <= day <= b[-1][0]:
                for r in b:
                    if r[0] == day:
                        return r
        except Exception:
            NO_BARS.append(f"{symbol}@{day}(bad-cache)")
            return None
    try:
        from futu import AuType, KLType, RET_OK, OpenQuoteContext
        q = OpenQuoteContext(host="127.0.0.1", port=11111)
        try:
            ret, df, _ = q.request_history_kline(
                symbol, start=day, end=day, ktype=KLType.K_DAY,
                autype=AuType.NONE, max_count=5)
            if ret != RET_OK or df is None or len(df) == 0:
                NO_BARS.append(f"{symbol}@{day}(fetch)")
                return None
            bars = [[r["time_key"][:10], r["open"], r["high"], r["low"], r["close"]]
                    for _, r in df.iterrows()]
        finally:
            q.close()
            time.sleep(1.2)
        if bars:
            fp.write_text(json.dumps(bars))
            return bars[0]
    except Exception as e:
        NO_BARS.append(f"{symbol}@{day}({type(e).__name__})")
    return None


def _load_ledger() -> dict:
    """按 (symbol, entry_time) 聚合 arm+tick。"""
    events: dict = {}
    if not LEDGER.exists():
        return events
    for i, line in enumerate(LEDGER.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            BAD_JSONL.append(i)
            continue
        key = (row.get("symbol"), row.get("entry_time"))
        if row.get("t") == "arm":
            events.setdefault(key, {"arm": row, "ticks": []})["arm"] = row
        elif row.get("t") == "tick" and key in events:
            events[key]["ticks"].append(row)
    return events


def _settle_leg(arm: dict, ticks: list, day_bar) -> dict:
    """虚拟腿当日结算。返回腿记录 (口径见模块 docstring)。"""
    direction = arm["direction"]
    sgn = 1.0 if direction == "LONG" else -1.0
    fill = arm["arm_price"] * (1 + sgn * SLIP)
    stop = float(arm["arm_price"])      # 武装前保护 (裁决口径 2)
    peak = fill
    trail_on = False
    exit_px = exit_how = None
    tail_after_parent = False

    for tk in ticks:
        px = float(tk["price"])
        peak = max(peak, px) if sgn > 0 else min(peak, px)
        leg_pct = sgn * (px / fill - 1) * 100
        if not trail_on and leg_pct >= TRAIL_ARM:
            trail_on = True
        if trail_on:
            tstop = peak * (1 - TRAIL_DD / 100) if sgn > 0 else peak * (1 + TRAIL_DD / 100)
            stop = max(stop, tstop) if sgn > 0 else min(stop, tstop)
        if (sgn > 0 and px <= stop) or (sgn < 0 and px >= stop):
            exit_px, exit_how = stop, ("leg_trail" if trail_on else "leg_stop")
            break
        if tk.get("exiting"):
            tail_after_parent = True
            break

    if exit_px is None and tail_after_parent:
        # 母仓先退 → 当日剩余段用日K 保守界 (裁决口径 4)
        if day_bar:
            _, _, hi, lo, cl = day_bar
            peak = max(peak, hi) if sgn > 0 else min(peak, lo)
            if trail_on:
                tstop = peak * (1 - TRAIL_DD / 100) if sgn > 0 else peak * (1 + TRAIL_DD / 100)
                stop = max(stop, tstop) if sgn > 0 else min(stop, tstop)
            if (sgn > 0 and lo <= stop) or (sgn < 0 and hi >= stop):
                exit_px, exit_how = stop, "bar_conservative_stop"
            else:
                exit_px, exit_how = float(cl), "bar_eod"
        else:
            exit_px, exit_how = float(ticks[-1]["price"]), "tail_missing_nobars"
    elif exit_px is None:
        if ticks:
            exit_px, exit_how = float(ticks[-1]["price"]), "eod_tick"  # EOD 兜底 (tick 覆盖到收盘)
        elif day_bar:
            exit_px, exit_how = float(day_bar[4]), "bar_eod_noticks"
        # else: 无 tick 无 bar → 由调用方记 NO_BARS, 不结算

    if exit_px is None:
        return {}
    leg_pct_net_gross = sgn * (exit_px / fill - 1) * 100   # 毛 (费用单独扣)
    peak_pct = sgn * (peak / fill - 1) * 100
    return {
        "fill": round(fill, 4), "exit_px": round(exit_px, 4), "exit_how": exit_how,
        "leg_gross_pct": round(leg_pct_net_gross, 4),
        "leg_peak_pct": round(peak_pct, 4),
        "leg_giveback_pct": round(peak_pct - leg_pct_net_gross, 4),
    }


def main() -> None:
    cfg = _load_cfg()
    events = _load_ledger()
    today_et = datetime.now(ET).date().isoformat()
    con = _ro()

    settled, in_progress = [], 0
    for (symbol, entry_time), ev in sorted(events.items(), key=lambda kv: kv[1]["arm"]["ts"]):
        arm = ev["arm"]
        arm_day = arm["ts"][:10]
        if arm_day >= today_et:
            in_progress += 1
            continue
        ticks = ev["ticks"]
        need_bar = (not ticks) or any(t.get("exiting") for t in ticks)
        day_bar = _day_bar(symbol, arm_day) if need_bar else None
        leg = _settle_leg(arm, ticks, day_bar)
        if not leg:
            NO_BARS.append(f"{symbol}@{arm_day}(unsettled)")
            continue

        # 帽内可执行口径 (§15.6/§十二): equity 优先用 arm 事件实录, 回退最新镜像
        eq, eq_ts = _equity_at(con, arm["ts"], arm)
        if eq is None:
            NO_EQUITY.append(f"{symbol}@{arm['ts']}(unavailable)")
        arm_notional = arm["arm_price"] * float(arm["parent_qty"] or 0)
        cap_usd = (eq or 0) * cfg["max_position_pct"]
        add_ratio = min(ADD_CAP, max(0.0, (cap_usd - arm_notional) / arm_notional)) \
            if arm_notional > 0 and eq else None
        add_notional = (add_ratio or 0) * arm_notional

        # 母仓结局 join (回吐对照 + 背景)
        parent = con.execute(
            "SELECT id, exit_reason, pnl_net, pnl_pct, metadata_json FROM trade_records "
            "WHERE symbol=? AND entry_time=? LIMIT 1", (symbol, entry_time)).fetchone()
        parent_giveback = parent_reason = None
        if parent:
            parent_reason = parent["exit_reason"]
            try:
                m = json.loads(parent["metadata_json"] or "{}")
                mfe = float(m.get("mfe_pct", 0) or 0)
                parent_giveback = round(mfe - float(parent["pnl_pct"] or 0), 4)
            except Exception:
                BAD_META.append(parent["id"])

        leg_net = leg["leg_gross_pct"] / 100 * add_notional - FEE_RT * add_notional
        leg_net_uncap = leg["leg_gross_pct"] / 100 * (ADD_CAP * arm_notional) \
            - FEE_RT * (ADD_CAP * arm_notional)
        settled.append({
            "symbol": symbol, "direction": arm["direction"], "arm_ts": arm["ts"],
            "entry_time": entry_time, "arm_price": arm["arm_price"],
            "parent_qty": arm["parent_qty"], "arm_notional": round(arm_notional, 2),
            "equity_at_arm": eq, "equity_ts": eq_ts,
            "cap_usd": round(cap_usd, 2), "add_ratio": round(add_ratio, 4) if add_ratio is not None else None,
            "add_notional": round(add_notional, 2),
            "leg_net_usd": round(leg_net, 2),
            "leg_net_usd_uncapped40_diag": round(leg_net_uncap, 2),
            "parent_exit_reason": parent_reason, "parent_giveback_pct": parent_giveback,
            **leg,
        })

    ex = [s for s in settled if s["add_ratio"] and s["add_ratio"] > 0]
    nets = sorted(s["leg_net_usd"] for s in ex)
    med = nets[len(nets) // 2] if nets else None
    tot = round(sum(nets), 2)
    # 日聚类 t + MDE(80%, α=0.05 双侧 ⇒ 2.8×SE) — 宪章铁律9
    by_day: dict = {}
    for s in ex:
        by_day[s["arm_ts"][:10]] = by_day.get(s["arm_ts"][:10], 0) + s["leg_net_usd"]
    day_sums = list(by_day.values())
    k = len(day_sums)
    t_stat = mde80 = None
    if k >= 2:
        mean = sum(day_sums) / k
        sd = math.sqrt(sum((x - mean) ** 2 for x in day_sums) / (k - 1))
        se = sd / math.sqrt(k)
        t_stat = round(mean / se, 3) if se > 0 else None
        mde80 = round(2.8 * se, 2)

    leg_gb = sorted(s["leg_giveback_pct"] for s in ex)
    par_gb = sorted(s["parent_giveback_pct"] for s in ex if s["parent_giveback_pct"] is not None)
    gates = {
        "n_executable>=40": len(ex) >= N_GATE,
        "median_leg_net>0": (med is not None and med > 0),
        "sum_leg_net>0": tot > 0,
        "giveback_leg<=parent": bool(leg_gb and par_gb and
                                     leg_gb[len(leg_gb) // 2] <= par_gb[len(par_gb) // 2]),
    }
    verdict = "READY_FOR_USER_RULING" if all(gates.values()) else "KEEP_SAMPLING"

    out = {
        "script": "trail_lock_pyramid_settle", "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "deb": "DEB-20260928-trail-lock-pyramid-shadow",
        "config_snapshot": {"trail_arm_pct": TRAIL_ARM, "trail_dd_pct": TRAIL_DD,
                            "slip_pct": SLIP * 100, "fee_rt_pct": FEE_RT * 100,
                            "add_cap": ADD_CAP, **cfg},
        "n_armed_total": len(events), "n_settled": len(settled), "n_in_progress": in_progress,
        "n_executable": len(ex), "n_truncated_zero_headroom": len(settled) - len(ex),
        "leg_net_median_usd": med, "leg_net_sum_usd": tot,
        "leg_giveback_median_pct": leg_gb[len(leg_gb) // 2] if leg_gb else None,
        "parent_giveback_median_pct": par_gb[len(par_gb) // 2] if par_gb else None,
        "daily_cluster": {"k_days": k, "t": t_stat, "mde80_usd_per_day": mde80},
        "gates": gates, "verdict": verdict,
        "expectation_preregistered": "单事件期望≈$0.5–1.5 / n=40总期望$20–60 (§十一); 测不出≠证伪; 决策问题=武装赢家帽政策",
        "sentinels": {"bad_jsonl": BAD_JSONL, "no_equity": NO_EQUITY,
                      "no_bars": NO_BARS, "bad_meta": BAD_META},
        "events": settled,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"[pyramid-settle] armed={len(events)} settled={len(settled)} "
          f"executable={len(ex)} truncated={len(settled) - len(ex)} "
          f"median={med} sum={tot} verdict={verdict}")
    print(f"[pyramid-settle] gates={gates} daily_t={t_stat} mde80={mde80}")
    if BAD_JSONL or NO_EQUITY or NO_BARS or BAD_META:
        print(f"[pyramid-settle] ⚠️sentinels: bad_jsonl={len(BAD_JSONL)} "
              f"no_equity={len(NO_EQUITY)} no_bars={len(NO_BARS)} bad_meta={len(BAD_META)}")
    print(f"[saved] {OUT}")


if __name__ == "__main__":
    main()
