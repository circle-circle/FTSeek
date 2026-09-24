#!/usr/bin/env python3
"""隔夜已知事件标记影子 (ETH 微观结构影子 ⑦, 2026-09-24 用户裁「立项」) —— **record-only, 零干预**.

## 运行时刻语义

cron 运行时刻: **CST 08:45 * * 2-6** (即 ET 20:45 Mon-Fri, 前一日)。
  此时 et_today() 返回 T (ET 当日), 其 premarket/afterhours 两个 session 均已收盘,
  join T 计划对「隔夜跳空风险」无意义 — 风险是持仓穿越**次日 (T+1) 盘前**的已知事件。
  T+1 的盘前计划在 CST 08:30 (ET 20:30 T) 刚生成, T+1 的盘后计划要 CST 03:45 T+2 才生成
  — 在 08:45 时点盘后文件合法不存在。

`--next-day` 模式 (cron 专用):
  et_date = et_today() + 1 天 (ZoneInfo ET 纯日期加法);
  **只扫 premarket 计划文件** (盘后文件此时合法不存在, 不算 missing, 不触发 plan_missing);
  plan_missing 仅当 premarket 文件缺失才记 (CST 08:30 生成轮失败 = 真断供);
  行内字段 plan_scope = "premarket"。
  标记目标: T+1 盘前窗 (ET 04:00-09:30 T+1 = CST 16:00-21:30 T+1, 提前 ~7h 标记)。

`--date` 手工模式: 语义不变, 扫 premarket + afterhours 双文件, plan_scope = "both"。

## 预注册判据 (读前写死; 读数出来前不得改本段)

规则: 每日 join open_positions (playbook_sim + extended_hours_entry) 与 et_date 的 RFC-104
  计划 ({et_date}_premarket.json [+ {et_date}_afterhours.json, 非 --next-day 时])。
  持仓票命中任一计划行 ⇒ flag:"overnight_into_known_event"；
  未命中 ⇒ flag:"overnight_no_known_event" (cohort 对照面)。
  计划文件缺失 (语义见上) ⇒ flag:"plan_missing" 留痕 (计划断供本身是要观测的事件)。

判据 (攒样 → 提请 DEB):
  - 采样门: **≥ 20 个隔夜持仓观察** (flagged + unflagged 合计, flagged ≥ 8)
  - 提请 DEB 条件: flagged cohort 单笔 p10 < −2% (尾部显著劣于 unflagged 同分位)
    → 提请「事件穿越隔夜仓风险处置」DEB (选项面: 收紧/减仓/显式 carry 理由留痕, 届时另裁)
  - 否则维持现状并留档结题
  - **死线**: 2026-12-31 (隔夜样本天然低频, 死线放宽一档; 到期自动停采留档)

## 口径
- 持仓源: sqlite data/ftseek_us.db **只读** (URI mode=ro), open_positions 表
  owner_strategy IN ('playbook_sim', 'extended_hours_entry')
- 计划源: data/loops/ext_entry_plan/{et_date}_{premarket,afterhours}.json
- 输出: data/shadow/overnight_known_event_flags.jsonl (唯一产物)
- 幂等键: (et_date, symbol) — 同日重跑不重复行 (参照 rth_fade_shadow.py 写入侧去重)
  --next-day 下 et_date 是 T+1, 与手工 --date T+1 同日重跑天然去重 ✓
- 净额铁律: --report 模式算 PnL 时 pnl_net 优先否则 realized_pnl − fees_est

## 零干预
- 不写 DB、不推送、不触碰退出链/隔夜臂判据/风险预算
- 不连 futu/OpenD (纯本地文件 + sqlite ro)

跑法: cron CST 08:45 * * 2-6 `--next-day`, timeout 120s。
     `--date` 手工指定 ET 日期; `--report` 算两 cohort PnL 分布 + 采样进度; `--dry` 只打印不落盘。
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_log = logging.getLogger(__name__)

OUT = ROOT / "data" / "shadow" / "overnight_known_event_flags.jsonl"
DB_PATH = ROOT / "data" / "ftseek_us.db"
PLAN_DIR = ROOT / "data" / "loops" / "ext_entry_plan"
DEADLINE = date(2026, 12, 31)
ET = ZoneInfo("America/New_York")

# 持仓 owner_strategy 白名单 (playbook sim + EXT 研究臂)
OWNER_STRATEGIES = ("playbook_sim", "extended_hours_entry")


def et_today() -> str:
    return datetime.now(ET).date().isoformat()


def et_next_day() -> str:
    """ET today + 1 day (cron --next-day target: T+1 premarket window)."""
    return (datetime.now(ET).date() + timedelta(days=1)).isoformat()


def _db_ro() -> sqlite3.Connection:
    """Open sqlite in read-only URI mode (不写 DB, 不锁)."""
    uri = f"file:{DB_PATH}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def load_open_positions() -> list[dict]:
    """Read open_positions filtered to playbook_sim + extended_hours_entry."""
    conn = _db_ro()
    try:
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in OWNER_STRATEGIES)
        rows = conn.execute(
            f"SELECT symbol, direction, size, avg_entry_price, entry_time, "
            f"owner_strategy, mode, metadata_json "
            f"FROM open_positions WHERE owner_strategy IN ({placeholders})",
            list(OWNER_STRATEGIES),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_plan_rows(et_date: str, premarket_only: bool = False) -> tuple[list[dict], list[str]]:
    """Load plan rows for an ET date from plan files.

    premarket_only=True: only scan premarket file (cron --next-day mode, afterhours
        legitimately not yet generated → not counted as missing).
    premarket_only=False: scan both premarket + afterhours (manual --date mode).

    Returns (plan_rows, missing_files).
    missing_files lists which *expected* files were absent (for plan_missing flag).
    """
    plan_rows: list[dict] = []
    missing_files: list[str] = []
    sessions = ("premarket",) if premarket_only else ("premarket", "afterhours")
    for session in sessions:
        p = PLAN_DIR / f"{et_date}_{session}.json"
        if not p.exists():
            missing_files.append(str(p.name))
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("plan file read failed %s: %s", p, exc, exc_info=True)
            missing_files.append(str(p.name))
            continue
        if not isinstance(data, list):
            _log.warning("plan file %s is not a list, skipping", p.name)
            missing_files.append(str(p.name))
            continue
        for row in data:
            if isinstance(row, dict) and row.get("symbol"):
                plan_rows.append({
                    "symbol": str(row["symbol"]).upper(),
                    "session": session,
                    "phase": row.get("phase"),
                    "state": row.get("state"),
                    "habit_hint": row.get("habit_hint"),
                    "plan_hash": row.get("plan_hash"),
                })
    return plan_rows, missing_files


def _strip_us_prefix(sym: str) -> str:
    """Strip US. prefix from open_positions symbol for plan join."""
    s = str(sym or "").strip().upper()
    if s.startswith("US."):
        s = s[3:]
    return s


def build_flags(et_date: str, dry: bool = False, premarket_only: bool = False) -> list[dict]:
    """Build flag rows for a given ET date.

    premarket_only=True: cron --next-day mode, only premarket plan scanned,
        plan_missing triggers only when premarket file absent.
    Returns list of flag dicts ready to append to jsonl.
    """
    now_iso = datetime.now(ET).isoformat(timespec="seconds")
    plan_scope = "premarket" if premarket_only else "both"
    positions = load_open_positions()
    plan_rows, missing_files = load_plan_rows(et_date, premarket_only=premarket_only)

    # Index plan by symbol (upper, no prefix)
    plan_by_sym: dict[str, list[dict]] = {}
    for pr in plan_rows:
        plan_by_sym.setdefault(pr["symbol"], []).append(pr)

    # plan_missing: all *expected* files are missing AND no plan rows loaded.
    #   premarket_only=True: only premarket expected → 1 missing = plan_missing
    #   premarket_only=False: both expected → 2 missing = plan_missing
    expected_count = 1 if premarket_only else 2
    all_missing = len(missing_files) == expected_count and not plan_rows
    if all_missing:
        return [{
            "et_date": et_date,
            "symbol": None,
            "position_direction": None,
            "position_qty": None,
            "event_phase": None,
            "event_session": None,
            "plan_state": None,
            "habit_hint": None,
            "flag": "plan_missing",
            "plan_scope": plan_scope,
            "missing_files": missing_files,
            "recorded_at": now_iso,
        }]

    # Load existing keys for idempotency
    seen = _load_seen_keys()

    flags: list[dict] = []
    for pos in positions:
        raw_sym = pos["symbol"]
        bare_sym = _strip_us_prefix(raw_sym)
        key = (et_date, bare_sym)
        if key in seen:
            continue

        hits = plan_by_sym.get(bare_sym, [])
        if hits:
            # One row per position-plan hit (use first hit for metadata)
            h = hits[0]
            flags.append({
                "et_date": et_date,
                "symbol": bare_sym,
                "position_direction": str(pos.get("direction") or "").upper(),
                "position_qty": pos.get("size"),
                "event_phase": h.get("phase"),
                "event_session": h.get("session"),
                "plan_state": h.get("state"),
                "habit_hint": h.get("habit_hint"),
                "flag": "overnight_into_known_event",
                "plan_scope": plan_scope,
                "owner_strategy": pos.get("owner_strategy"),
                "plan_hits": len(hits),
                "recorded_at": now_iso,
            })
        else:
            flags.append({
                "et_date": et_date,
                "symbol": bare_sym,
                "position_direction": str(pos.get("direction") or "").upper(),
                "position_qty": pos.get("size"),
                "event_phase": None,
                "event_session": None,
                "plan_state": None,
                "habit_hint": None,
                "flag": "overnight_no_known_event",
                "plan_scope": plan_scope,
                "owner_strategy": pos.get("owner_strategy"),
                "recorded_at": now_iso,
            })
    return flags


def _load_seen_keys() -> set[tuple[str, str]]:
    """Load existing (et_date, symbol) keys from jsonl for idempotency."""
    seen: set[tuple[str, str]] = set()
    if not OUT.exists():
        return seen
    try:
        with OUT.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    ed = row.get("et_date")
                    sym = row.get("symbol")
                    if ed and sym:
                        seen.add((str(ed), str(sym).upper()))
                except json.JSONDecodeError:
                    _log.warning("jsonl parse failed", exc_info=True)
                    continue
    except OSError as exc:
        _log.warning("jsonl read failed for idempotency: %s", exc, exc_info=True)
    return seen


def append_flags(flags: list[dict]) -> int:
    """Append flag rows to jsonl (idempotent via seen-keys). Returns count appended."""
    if not flags:
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as f:
        for r in flags:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(flags)


def _net_pnl(row: dict) -> float | None:
    """净额铁律: pnl_net 优先否则 realized_pnl − fees_est."""
    pn = row.get("pnl_net")
    if pn is not None:
        try:
            return float(pn)
        except (ValueError, TypeError):
            pass
    rp = row.get("realized_pnl")
    fe = row.get("fees_est")
    if rp is not None:
        try:
            return float(rp) - float(fe or 0)
        except (ValueError, TypeError):
            pass
    return None


def _percentile(vals: list[float], p: float) -> float | None:
    """Simple percentile (linear interpolation). p in [0,100]."""
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    k = (p / 100.0) * (n - 1)
    f = int(k)
    c = min(f + 1, n - 1)
    d = k - f
    return s[f] + d * (s[c] - s[f])


def report(et_date: str | None = None) -> int:
    """--report mode: compute cohort PnL distribution + sampling progress."""
    if not OUT.exists():
        print("[overnight_known_event_flag] 台账不存在, 无读数")
        return 0

    # Load all flag rows
    flagged_syms: dict[str, list[str]] = {}   # et_date → [bare_sym]
    unflagged_syms: dict[str, list[str]] = {}
    plan_missing_dates: list[str] = []
    total_flagged = 0
    total_unflagged = 0

    try:
        with OUT.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    _log.warning("jsonl parse failed", exc_info=True)
                    continue
                ed = row.get("et_date")
                sym = row.get("symbol")
                flag = row.get("flag")
                if flag == "plan_missing":
                    if ed:
                        plan_missing_dates.append(str(ed))
                    continue
                if not ed or not sym:
                    continue
                bare = str(sym).upper()
                if flag == "overnight_into_known_event":
                    flagged_syms.setdefault(str(ed), []).append(bare)
                    total_flagged += 1
                elif flag == "overnight_no_known_event":
                    unflagged_syms.setdefault(str(ed), []).append(bare)
                    total_unflagged += 1
    except OSError as exc:
        _log.warning("jsonl read failed: %s", exc, exc_info=True)
        return 1

    total_obs = total_flagged + total_unflagged
    print(f"[overnight_known_event_flag] 采样进度: {total_obs}/20 "
          f"(flagged={total_flagged}/≥8, unflagged={total_unflagged})")
    if plan_missing_dates:
        print(f"   计划断供日: {plan_missing_dates}")

    # Load trade_records for cohort PnL
    # We look at trades whose entry_time falls on the et_date+1 (the "overnight into" day)
    # and whose owner_strategy matches our filter.
    # Actually: the flag marks a position held overnight FROM et_date INTO et_date+1.
    # The PnL we want is the overnight PnL — approximated by the trade_record that
    # closes the position (exit_time on et_date or et_date+1).
    # Simplification: join flagged/unflagged symbols to trade_records by symbol,
    # where trade_date matches the flag et_date or et_date+1.

    conn = _db_ro()
    try:
        conn.row_factory = sqlite3.Row
        # Get all relevant trade_records
        placeholders = ",".join("?" for _ in OWNER_STRATEGIES)
        rows = conn.execute(
            f"SELECT trade_date, symbol, owner_strategy, realized_pnl, pnl_net, fees_est, "
            f"entry_time, exit_time "
            f"FROM trade_records WHERE owner_strategy IN ({placeholders}) "
            f"ORDER BY trade_date",
            list(OWNER_STRATEGIES),
        ).fetchall()
        trades = [dict(r) for r in rows]
    finally:
        conn.close()

    # Build date→symbol→flag lookup
    all_flagged_keys = set()
    for ed, syms in flagged_syms.items():
        for s in syms:
            all_flagged_keys.add((ed, s))
    all_unflagged_keys = set()
    for ed, syms in unflagged_syms.items():
        for s in syms:
            all_unflagged_keys.add((ed, s))

    # Match trades to cohorts: a trade on date D for symbol S matches the flag on date D-1
    # (overnight from D-1 into D). Also match same-day for simplicity.
    flagged_pnls: list[float] = []
    unflagged_pnls: list[float] = []

    for tr in trades:
        td = str(tr.get("trade_date") or "")[:10]
        raw_sym = str(tr.get("symbol") or "")
        bare = _strip_us_prefix(raw_sym)
        net = _net_pnl(tr)
        if net is None:
            continue

        # Check if this trade's symbol was flagged/unflagged on td or td-1
        try:
            td_date = datetime.strptime(td, "%Y-%m-%d").date()
        except ValueError:
            continue
        prev_d = (td_date - timedelta(days=1)).isoformat()

        if (prev_d, bare) in all_flagged_keys or (td, bare) in all_flagged_keys:
            flagged_pnls.append(net)
        elif (prev_d, bare) in all_unflagged_keys or (td, bare) in all_unflagged_keys:
            unflagged_pnls.append(net)

    def _stats(vals: list[float], label: str) -> None:
        if not vals:
            print(f"   {label}: n=0 (无数据)")
            return
        med = _percentile(vals, 50)
        p10 = _percentile(vals, 10)
        print(f"   {label}: n={len(vals)} median=${med:+.2f} p10=${p10:+.2f}")

    print(f"\n[overnight_known_event_flag] cohort PnL 分布 (净额):")
    _stats(flagged_pnls, "flagged  ")
    _stats(unflagged_pnls, "unflagged")

    #判据 check
    if total_obs >= 20 and total_flagged >= 8:
        p10_f = _percentile(flagged_pnls, 10) if flagged_pnls else None
        if p10_f is not None and p10_f < -2.0:
            pct_str = f"${p10_f:+.2f}"
            print(f"\n⚠ 判据触发: flagged p10 = {pct_str} < −$2.00 → 提请「事件穿越隔夜仓风险处置」DEB")
        else:
            p10_str = f"${p10_f:+.2f}" if p10_f is not None else "N/A"
            print(f"\n✓ 采样达标但判据未触发: flagged p10 = {p10_str} ≥ −$2.00 → 维持现状留档")
    else:
        print(f"\n⏳ 采样未达标: {total_obs}/20 (flagged {total_flagged}/≥8) → 继续攒样")

    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="隔夜已知事件标记影子 (record-only, 零干预)")
    ap.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD (默认当日 ET)")
    ap.add_argument("--next-day", action="store_true",
                    help="cron 模式: et_date = ET today + 1, 只扫 premarket 计划")
    ap.add_argument("--dry", action="store_true", help="只打印不落盘")
    ap.add_argument("--report", action="store_true", help="算 cohort PnL 分布 + 采样进度")
    args = ap.parse_args(argv)

    premarket_only = args.next_day
    if args.date:
        et_date = args.date
    elif args.next_day:
        et_date = et_next_day()
    else:
        et_date = et_today()

    # 死线闸: 到期自动停采留档
    try:
        d = datetime.strptime(et_date, "%Y-%m-%d").date()
    except ValueError:
        print(f"[overnight_known_event_flag] 日期格式错误: {et_date}", file=sys.stderr)
        return 1
    if d > DEADLINE:
        print(f"[overnight_known_event_flag] 死线已过 ({DEADLINE}), 自动停采留档。"
              f" 台账保留于 {OUT}")
        return 0

    if args.report:
        return report(et_date)

    flags = build_flags(et_date, dry=args.dry, premarket_only=premarket_only)
    if not flags:
        scope_note = " (premarket-only)" if premarket_only else ""
        print(f"[overnight_known_event_flag] {et_date}{scope_note} "
              f"无持仓 (owner_strategy IN {OWNER_STRATEGIES})")
        return 0

    for f in flags:
        fl = f["flag"]
        sym = f.get("symbol") or "—"
        qty = f.get("position_qty") or 0
        phase = f.get("event_phase") or "—"
        sess = f.get("event_session") or "—"
        scope = f.get("plan_scope", "both")
        print(f"   {sym:<8} {fl:<35} qty={qty:<6} phase={phase} session={sess} scope={scope}")

    if args.dry:
        print(f"[overnight_known_event_flag] --dry: 不落盘, {len(flags)} 行")
        return 0

    n = append_flags(flags)
    print(f"[overnight_known_event_flag] 已追加 {n} 行 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
