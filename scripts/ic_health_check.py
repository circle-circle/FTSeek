#!/usr/bin/env python3
"""
IC Health Check — detect dead-strategy pollution & anti-predictive IC
====================================================================

Cron job: runs after the nightly backfill (forward returns) completes.
Checks the predictive health of the signal store (signal_snapshots.db):

1. Overall IC (Spearman rank correlation between *signed* signal_strength
   and fwd_ret_t1) — across all signals with a realized fwd_ret_t1.
2. IC by strategy_id.
3. IC by trade_mode (live / simulate).
4. Dead-strategy pollution: a strategy disabled in
   config/unified_config.yaml (strategies.<name>.enabled: False) that
   still has signals in signal_snapshots.  Those stale signals pollute
   the headline IC number and can trip downstream IC gates even though
   every *active* strategy is healthy.
5. IC re-computed EXCLUDING disabled-strategy signals (the "true" IC).
6. Alerts (see THRESHOLDS) — overall IC < -0.05, any active strategy
   IC < -0.10, or dead signals > 20% of total.
7. Summary with ✅/⚠️/🔴 markers.  On RED, prints '🚨 IC_HEALTH_ALERT'
   as the final line and exits 1 so cron / monitoring picks it up.

Bug this prevents
-----------------
IC was reported as -0.097 (anti-predictive), triggering IC gates that
blocked all trades.  The real cause was a disabled strategy
(EARNINGS_REVISION_MOMENTUM, disabled after 6/18) whose ~441 stale
signals were still in signal_snapshots and dragged the headline IC
negative.  Excluding them, the true IC was +0.007.  Nobody investigated
*why* the IC was negative — they just stacked more gates and patches.
This script surfaces the dead-pollution root cause directly.

# Crontab: 30 5 * * 2-6 (after backfill runs at 5:30)
# /opt/ftseek/FTSeek-US/venv/bin/python /opt/ftseek/FTSeek-US/scripts/ic_health_check.py

Usage:
    /opt/ftseek/FTSeek-US/venv/bin/python scripts/ic_health_check.py [--days 90] [--no-config-skip]
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ── FTSeek venv layout (set shebang / cron to use venv/bin/python) ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "signal_snapshots.db"
CONFIG_PATH = PROJECT_ROOT / "config" / "unified_config.yaml"

# ── strategy_id (in DB) → config name (under strategies: in yaml) ──
# Anything not found here is treated as "unknown config mapping" and is
# assumed ENABLED (we only block on strategies we can prove are disabled).
STRATEGY_ID_TO_CONFIG = {
    "EARNINGS_REVISION_MOMENTUM": "earnings_revision",
    "TECHNICAL_MULTI_FACTOR": "technical",
    "technical_intraday": "technical",      # intraday variant → same config family
    "voting_llm": "voting_llm",
    "BATTLE_PLAN_FAST_PATH": "fast_lane",
    "trend_first": "trend_first",
    "orb": "orb",
    "BATTLE_PLAN": "fast_lane",
}

# ── alert thresholds ──
THRESH_OVERALL_IC = -0.05        # overall IC below this = anti-predictive (RED)
THRESH_ACTIVE_STRAT_IC = -0.10   # any active strategy below this = hard block (RED)
THRESH_DEAD_POLLUTION_PCT = 0.20 # dead signals > 20% of total = pollution (RED)

MIN_N_FOR_IC = 5                 # fewer than this many pairs → skip IC (n too small)


# ════════════════════════════════════════════════════════════════
# IC calculation
# ════════════════════════════════════════════════════════════════
def _signed_strength(direction, strength):
    """
    Convert raw (direction, strength) → a *signed* scalar whose sign
    encodes the predicted direction and whose magnitude encodes
    conviction.  LONG → +strength, SHORT → -strength, NEUTRAL → 0.

    Forward returns are signed the same way (LONG profits when fwd_ret>0,
    SHORT profits when fwd_ret<0).  Spearman(signed_strength, fwd_ret_t1)
    is therefore a *direction-aware* IC: a positive IC means stronger
    conviction in a direction predicts larger returns *in that direction*.
    """
    d = (direction or "").strip().upper()
    s = float(strength) if strength is not None else 0.0
    if d == "LONG":
        return s
    if d == "SHORT":
        return -s
    return 0.0  # NEUTRAL / unknown → 0 (no directional view)


def compute_ic(rows):
    """
    Spearman rank IC between signed signal_strength and fwd_ret_t1.

    Returns (ic, n) where ic is None if n < MIN_N_FOR_IC or undefined.
    Falls back to Pearson correlation if scipy is unavailable.
    """
    pairs = [
        (_signed_strength(r["direction"], r["strength"]), r["fwd_ret_t1"])
        for r in rows
        if r.get("fwd_ret_t1") is not None and r.get("strength") is not None
    ]
    if len(pairs) < MIN_N_FOR_IC:
        return None, len(pairs)
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    # constant series → correlation undefined
    if len(set(xs)) <= 1 or len(set(ys)) <= 1:
        return None, len(pairs)

    try:
        from scipy.stats import spearmanr
        res = spearmanr(xs, ys)
        ic = float(res.correlation if hasattr(res, "correlation") else res[0])
        return round(ic, 4), len(pairs)
    except ImportError:
        # ── Pearson fallback (manual, no scipy) ──
        n = len(pairs)
        mx = sum(xs) / n
        my = sum(ys) / n
        sx = sum((x - mx) ** 2 for x in xs)
        sy = sum((y - my) ** 2 for y in ys)
        if sx == 0 or sy == 0:
            return None, len(pairs)
        cov = sum((x - mx) * (y - my) for x, y in pairs)
        ic = cov / (sx * sy) ** 0.5
        return round(float(ic), 4), len(pairs)


# ════════════════════════════════════════════════════════════════
# Data loading
# ════════════════════════════════════════════════════════════════
def load_signals(days):
    """Load signal_snapshots rows with realized fwd_ret_t1 within `days`."""
    if not DB_PATH.exists():
        print(f"❌ DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(2)

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT strategy_id, direction, strength, fwd_ret_t1,
                   trade_mode, trade_date
            FROM signal_snapshots
            WHERE fwd_ret_t1 IS NOT NULL
              AND (trade_date IS NULL OR trade_date >= ?)
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════
# Config: which strategies are disabled?
# ════════════════════════════════════════════════════════════════
def load_disabled_strategies():
    """
    Parse config/unified_config.yaml, return:
        {config_name: enabled_bool} for every entry under `strategies:`.

    A strategy is "disabled" when its `enabled` key is explicitly False
    (or absent-and-defaulted — but we only treat explicit False as
    disabled, to avoid false positives on config entries that simply
    don't carry an enabled flag).
    """
    if not CONFIG_PATH.exists():
        return {}, {}

    try:
        import yaml
    except ImportError:
        print("⚠️  PyYAML not installed — cannot detect disabled strategies.", file=sys.stderr)
        return {}, {}

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    strategies = cfg.get("strategies") or {}
    enabled_map = {}   # name -> bool
    disabled_set = set()
    for name, body in strategies.items():
        if isinstance(body, dict) and "enabled" in body:
            is_on = bool(body["enabled"])
            enabled_map[name] = is_on
            if not is_on:
                disabled_set.add(name)
        else:
            # No explicit enabled flag — treat as enabled (don't block).
            enabled_map[name] = True
    return enabled_map, disabled_set


def strategy_is_disabled(strategy_id, disabled_set):
    """True if the strategy_id maps to a config name that is disabled."""
    cfg_name = STRATEGY_ID_TO_CONFIG.get(strategy_id)
    if cfg_name is None:
        return False, None
    return cfg_name in disabled_set, cfg_name


# ════════════════════════════════════════════════════════════════
# Main health check
# ════════════════════════════════════════════════════════════════
def run_health_check(days):
    lines = []
    red_flags = []   # list of (severity, message)
    yellow_flags = []

    def out(s=""):
        lines.append(s)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out("=" * 64)
    out("  FTSeek-US IC Health Check")
    out(f"  Run: {now} | window: {days}d")
    out("=" * 64)

    signals = load_signals(days)
    enabled_map, disabled_set = load_disabled_strategies()
    out(f"DB: {DB_PATH}")
    out(f"Config: {CONFIG_PATH}")
    out(f"Signals (fwd_ret_t1 populated): {len(signals)}")
    out(f"Disabled strategies in config: "
        f"{sorted(disabled_set) if disabled_set else '(none)'}")
    out("")

    if not signals:
        out("⚠️  No signals with fwd_ret_t1 in window — nothing to evaluate.")
        out("✅ IC_HEALTH_OK")
        return lines, False

    # ── 1. Overall IC ─────────────────────────────────────────────
    overall_ic, overall_n = compute_ic(signals)
    out("─" * 64)
    out("1. Overall IC (signed strength vs fwd_ret_t1)")
    out("─" * 64)
    if overall_ic is None:
        out(f"  ⚠️  n={overall_n} < {MIN_N_FOR_IC} (too few for IC)")
    else:
        marker = "🔴" if overall_ic < THRESH_OVERALL_IC else "✅"
        out(f"  {marker} Overall IC = {overall_ic:+.4f}  (n={overall_n})")
        if overall_ic < THRESH_OVERALL_IC:
            red_flags.append(
                f"Overall IC {overall_ic:+.4f} < {THRESH_OVERALL_IC} "
                f"(anti-predictive)"
            )
    out("")

    # ── 2. IC by strategy_id ──────────────────────────────────────
    by_strat = defaultdict(list)
    for s in signals:
        by_strat[s.get("strategy_id") or "unknown"].append(s)

    out("─" * 64)
    out("2. IC by strategy_id")
    out("─" * 64)
    strat_ic = {}
    for sid, grp in sorted(by_strat.items(), key=lambda kv: -(len(kv[1]))):
        is_disabled, cfg_name = strategy_is_disabled(sid, disabled_set)
        ic, n = compute_ic(grp)
        strat_ic[sid] = {"ic": ic, "n": n, "disabled": is_disabled,
                         "cfg_name": cfg_name}
        tag = ""
        if is_disabled:
            tag = f"  [DISABLED in config → {cfg_name}]"
        if ic is None:
            out(f"  {sid:32s} n={n:<5} IC=N/A (n<{MIN_N_FOR_IC}){tag}")
        else:
            if is_disabled:
                marker = "⚪"  # disabled — informational, not an alert target
            elif ic < THRESH_ACTIVE_STRAT_IC:
                marker = "🔴"
                red_flags.append(
                    f"Active strategy {sid} IC {ic:+.4f} < "
                    f"{THRESH_ACTIVE_STRAT_IC} (hard block)"
                )
            elif ic < 0:
                marker = "⚠️"
                yellow_flags.append(
                    f"Active strategy {sid} IC {ic:+.4f} (negative but above block)"
                )
            else:
                marker = "✅"
            out(f"  {marker} {sid:32s} n={n:<5} IC={ic:+.4f}{tag}")
    out("")

    # ── 3. IC by trade_mode ───────────────────────────────────────
    by_mode = defaultdict(list)
    for s in signals:
        by_mode[s.get("trade_mode") or "unknown"].append(s)

    out("─" * 64)
    out("3. IC by trade_mode")
    out("─" * 64)
    for mode, grp in sorted(by_mode.items(), key=lambda kv: -(len(kv[1]))):
        ic, n = compute_ic(grp)
        if ic is None:
            out(f"  ⚠️  {mode:10s} n={n:<5} IC=N/A (n<{MIN_N_FOR_IC})")
        else:
            if ic < THRESH_OVERALL_IC:
                marker = "🔴"
            elif ic < 0:
                marker = "⚠️"
            else:
                marker = "✅"
            out(f"  {marker} {mode:10s} n={n:<5} IC={ic:+.4f}")
    out("")

    # ── 4. Dead strategy pollution ────────────────────────────────
    out("─" * 64)
    out("4. Dead strategy pollution (disabled strategy still emitting signals)")
    out("─" * 64)
    dead_signals = 0
    dead_breakdown = []
    for sid, grp in by_strat.items():
        is_disabled, cfg_name = strategy_is_disabled(sid, disabled_set)
        if not is_disabled:
            continue
        dead_signals += len(grp)
        ic, n = strat_ic[sid]["ic"], strat_ic[sid]["n"]
        ic_str = f"{ic:+.4f}" if ic is not None else "N/A"
        dead_breakdown.append((sid, cfg_name, len(grp), ic_str))

    total = len(signals)
    dead_pct = (dead_signals / total) if total else 0.0
    if dead_signals == 0:
        out("  ✅ No signals from disabled strategies.")
    else:
        marker = "🔴" if dead_pct > THRESH_DEAD_POLLUTION_PCT else "⚠️"
        out(f"  {marker} Dead signals: {dead_signals}/{total} "
            f"({dead_pct:.1%}) of total")
        out(f"      threshold: > {THRESH_DEAD_POLLUTION_PCT:.0%} = RED (pollution)")
        for sid, cfg, n, ic_str in dead_breakdown:
            out(f"      • {sid:32s} → config '{cfg}' : {n} signals, IC={ic_str}")
        if dead_pct > THRESH_DEAD_POLLUTION_PCT:
            red_flags.append(
                f"Dead-strategy pollution: {dead_signals}/{total} "
                f"({dead_pct:.1%}) > {THRESH_DEAD_POLLUTION_PCT:.0%}"
            )
        else:
            yellow_flags.append(
                f"Dead-strategy signals present: {dead_signals} "
                f"({dead_pct:.1%}) — purge or exclude from IC gates"
            )
    out("")

    # ── 5. IC excluding disabled strategies (the "true" IC) ───────
    out("─" * 64)
    out("5. IC EXCLUDING disabled-strategy signals (true active IC)")
    out("─" * 64)
    active_signals = [
        s for s in signals
        if not strategy_is_disabled(s.get("strategy_id") or "unknown",
                                    disabled_set)[0]
    ]
    active_ic, active_n = compute_ic(active_signals)
    if active_ic is None:
        out(f"  ⚠️  n={active_n} < {MIN_N_FOR_IC} (too few for IC after exclusion)")
    else:
        out(f"  ✅ Active-only IC = {active_ic:+.4f}  (n={active_n})")
        if overall_ic is not None and active_ic is not None:
            delta = active_ic - overall_ic
            out(f"      vs overall {overall_ic:+.4f}  →  ΔIC = {delta:+.4f}")
            if dead_signals and abs(delta) > 0.02:
                out(f"      ⚠️  Excluding dead strategies shifts IC by {delta:+.4f};")
                out(f"          headline IC is being distorted by stale signals.")
    out("")

    # ── 6. Verdict ────────────────────────────────────────────────
    out("=" * 64)
    out("VERDICT")
    out("=" * 64)
    if red_flags:
        out("🔴 RED — action required:")
        for f in red_flags:
            out(f"   • {f}")
        if yellow_flags:
            out("⚠️  Warnings:")
            for f in yellow_flags:
                out(f"   • {f}")
        out("")
        out("🚨 IC_HEALTH_ALERT")
        return lines, True
    elif yellow_flags:
        out("⚠️  YELLOW — investigate but not blocking:")
        for f in yellow_flags:
            out(f"   • {f}")
        out("")
        out("✅ IC_HEALTH_OK")
        return lines, False
    else:
        out("✅ All clear — IC healthy, no dead-strategy pollution.")
        out("")
        out("✅ IC_HEALTH_OK")
        return lines, False


def main():
    parser = argparse.ArgumentParser(description="FTSeek IC health check")
    parser.add_argument("--days", type=int, default=90,
                        help="Lookback window in days (default 90)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress stdout (still sets exit code)")
    args = parser.parse_args()

    lines, is_red = run_health_check(args.days)
    if not args.quiet:
        print("\n".join(lines))
    sys.exit(1 if is_red else 0)


if __name__ == "__main__":
    main()
