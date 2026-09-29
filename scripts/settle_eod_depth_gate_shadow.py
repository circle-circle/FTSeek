#!/usr/bin/env python3
"""EOD 深水闸影子结算器 (RFC-079, 2026-09-09; **RFC-095 身份锚定 v2, 2026-09-22**).

对 data/shadow/eod_depth_gate_shadow.jsonl 的 arm 行 (EOD 豁免成立时打点):
  - 关联实际结局: trade_records 同**身份**的仓 (不是"同标的同日最新已平")
  - Δ = 反事实"EOD 按 decision_price 平" vs 实际结局 的净额差。
    费结构两边同为一次往返 → Δ = sign × (decision_price − exit_price) × qty (纯价差, 精确)。
  - would_revoke 行的 Δ>0 = 深水闸若生效可省下的钱; Δ<0 = 误杀 (豁免仓隔夜修复)。

判据 (判据字典 B19, 预注册 2026-09-09): 触发结算 n≥8 ∧ ΣΔ>0 ∧ 误杀赢家(Δ<0 且 |Δ|>费用线)≤1
→ 提请转硬闸 (另立裁决, 不自动生效)。n 轴 = **去重后的仓数** (distinct trade_id, 见病灶③)。
  回溯证据 (8/1-9/8 回放): -2.5% 阈值 3/3 命中灾难, ΣΔ=+$410, 误杀 0。

── RFC-095 修复的三个病灶 (2026-09-22, 用户裁「同意」建议A/B) ─────────────
病灶① **身份失配冻结**: 旧实现 `WHERE symbol=? AND trade_date=entry_date AND exit_time>=arm_date
  ORDER BY exit_time DESC LIMIT 1` —— 结算跑在 arm 次日 06:30 CST, 此时真正的豁免仓**可能还没平**
  (实测 2026-09-15 US.MU: 真仓 1503 于 9/16 09:31 ET 才平, 而 9/16 06:30 CST 的结算已跑) ⇒ 命中
  **同日另一笔已平仓** 1501 (9/15 10:03 ET 就平了, 早于 15:52 的决策时刻, 物理上不可能是被豁免的仓)
  并把该行写成 settled=true 永久冻结 ⇒ Δ 记成错仓价差 (−73.23 vs 真值 −36.51)。
  修法 = **身份锚定**: trade_ref (trade_records.id) 精确 > entry_time 精确 > (entry_price±0.5% ∧ qty,
  且出场晚于 arm 决策时刻); 都中不了 → 保持 unsettled **不再猜**。存量已 settled 行重算一次, 旧值写
  superseded 留痕。
病灶② **未持夜行计入 Δ**: arm 行只证明"决策时刻豁免成立", 不保证仓真过夜 —— 实测 2026-09-17
  US.NOK: 15:52:04 记豁免成立, 同一分钟 15:52:51 实际按规则4 PRE_CLOSE_EXIT 平仓 (净盈亏 47 秒内
  由 −0.11% 翻到 +0.03%, 跨过 net<0 判定线) ⇒ 该行结局不是隔夜结局。修法 = 出场 ET 日 ≤ arm_date
  的行标 `status=not_held` 且**不进 Δ 聚合** (行保留呈报)。
病灶③ **判据 n 轴被多夜重复计数**: 同一仓多夜各落一行 (实测 UBER 1485 落 4 行), 若多夜都触发
  would_revoke, ΣΔ/n 会重复计数。修法 = 触发组按 trade_id 去重, **取最早 arm_date 那夜**
  (闸首次可拦截的机会); 报告同时给 raw 行数 (仅诊断)。

挂载: crontab 30 6 * * 2-6 (与 winner settle 同窗, 复用 data/replay_bars 落盘后的时段)。
数据源: trade_records (ftseek_us.db, 只读 mode=ro)。arm 行未平仓/无身份锚 → 保留 unsettled 待下次。

用法: venv/bin/python scripts/settle_eod_depth_gate_shadow.py [--days N]
"""
import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "ftseek_us.db"
SHADOW = ROOT / "data" / "shadow" / "eod_depth_gate_shadow.jsonl"
OUT = ROOT / "data" / "shadow" / "eod_depth_gate_report_latest.json"

MIN_N = 8          # B19 触发结算样本下限 (仓数)
MAX_MISKILL = 1    # B19 误杀赢家上限
FEE_LINE = 5.0     # Δ<0 且 |Δ|>费地板 才算"误杀赢家" (微负=费差噪声)
PRICE_TOL = 0.005  # 身份锚定: entry_price 相对容差 (0.5%, 覆盖浮点/成交价微差)

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:   # zoneinfo 不可用 → 固定 EDT 近似 (只影响 00:00-01:00 ET 窗的日期归属)
    ET = timezone(timedelta(hours=-4))

_TRADE_COLS = ("id, symbol, direction, entry_time, exit_time, exit_price, pnl_net, "
               "realized_pnl, exit_reason, quantity, entry_price")


def _et_date(ts):
    """时间戳 → ET 日期串 (trade_records 三格式混存: 带 -04:00 / 带 +08:00 / naive=CST)。"""
    if not ts:
        return ""
    s = str(ts)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.astimezone(ET).date().isoformat()
    except Exception:
        return s[:10]


def _atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_rows():
    if not SHADOW.exists():
        return []
    rows = []
    for ln in SHADOW.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except Exception as _parse_err:
            # R5 (2026-09-09 棘轮清剿): 坏行跳过但必须留痕 — 静默坏行会让结算集悄悄缩小
            print(f"[settle_eod_depth_gate] 影子档坏行跳过: {_parse_err} | {ln[:120]!r}",
                  file=sys.stderr)
    return rows


def match_trade(conn, row):
    """身份锚定匹配 → (row | None, match_by, note)。

    ① trade_ref 精确匹配 (2026-09-22 起打点行自带 = trade_records.id, 最强锚)
    ② entry_time 精确匹配
    ③ (symbol + direction + entry_price±0.5% + qty) 且出场时刻晚于 arm 决策时刻
    都中不了 → (None, "no_anchor"|"still_open", note), 调用方保持 unsettled。**不再按"最新已平"猜**。
    """
    sym = row.get("symbol")
    if not sym:
        return None, "bad_row", ""
    direction = row.get("direction")
    ts = str(row.get("ts") or "")

    ref = row.get("trade_ref")
    if ref not in (None, ""):
        r = conn.execute(
            f"SELECT {_TRADE_COLS} FROM trade_records WHERE id=? AND exit_price > 0",
            (int(ref),)).fetchone()
        if r:
            return r, "trade_ref", ""
        still_open = conn.execute(
            "SELECT id FROM trade_records WHERE id=? AND exit_price IS NULL", (int(ref),)).fetchone()
        if still_open:
            return None, "still_open", ""
        return None, "no_anchor", "trade_ref 在 trade_records 无对应已平仓"

    et_in = str(row.get("entry_time") or "")
    if et_in:
        r = conn.execute(
            f"SELECT {_TRADE_COLS} FROM trade_records WHERE symbol=? AND direction=? "
            f"AND entry_time=? AND exit_price > 0", (sym, direction, et_in)).fetchone()
        if r:
            return r, "entry_time", ""
        still_open = conn.execute(
            "SELECT id FROM trade_records WHERE symbol=? AND entry_time=? AND exit_price IS NULL",
            (sym, et_in)).fetchone()
        if still_open:
            return None, "still_open", ""
        return None, "no_anchor", "entry_time 锚在 trade_records 无对应已平仓"

    px = float(row.get("entry_price") or 0)
    qty = float(row.get("qty") or 0)
    if px <= 0 or qty <= 0:
        return None, "no_anchor", "缺 entry_price/qty, 无法锚定"
    cands = conn.execute(
        f"SELECT {_TRADE_COLS} FROM trade_records WHERE symbol=? AND direction=? AND quantity=? "
        f"AND exit_price > 0 AND ABS(entry_price - ?) <= ?",
        (sym, direction, qty, px, px * PRICE_TOL)).fetchall()
    if not cands:
        return None, "no_anchor", f"(px≈{px} qty={qty}) 无匹配仓"
    best = min(abs(float(c["entry_price"] or 0) - px) for c in cands)
    cands = [c for c in cands if abs(abs(float(c["entry_price"] or 0) - px) - best) < 1e-9]
    # 排除"出场早于 arm 决策时刻"的候选 (物理上不是同一仓)
    live = [c for c in cands if not (ts and str(c["exit_time"] or "")[:19] <= ts[:19])]
    if not live:
        return None, "no_candidate_after_arm", "候选均早于 arm 决策时刻出场"
    live.sort(key=lambda c: str(c["exit_time"] or ""))
    return live[0], "entry_price_qty", ""


def settle_row(row, conn):
    """返回 (settled: bool, fields dict)。未平仓/无身份锚 → (False, {}) 保持待结算。"""
    sym = row.get("symbol")
    arm_date = str(row.get("arm_date") or "")
    if not sym or not arm_date:
        return True, {"status": "bad_row", "match_by": "bad_row", "cf_delta": None}
    r, match_by, note = match_trade(conn, row)
    if r is None:
        if match_by == "bad_row":
            return True, {"status": "bad_row", "match_by": "bad_row", "cf_delta": None}
        # still_open / no_candidate_after_arm / no_anchor(仓未出现) → 下轮再结算 (不猜)
        return False, {}
    exit_price = float(r["exit_price"] or 0)
    net = r["pnl_net"] if r["pnl_net"] is not None else (r["realized_pnl"] or 0)
    qty = float(row.get("qty") or r["quantity"] or 0)
    dec_price = float(row.get("decision_price") or 0)
    sign = 1.0 if str(row.get("direction", "")).upper() in ("LONG", "BUY") else -1.0
    fields = {
        "status": "settled", "trade_id": r["id"], "match_by": match_by,
        "exit_time": r["exit_time"], "exit_price": exit_price,
        "actual_net": round(float(net or 0), 2), "exit_reason": r["exit_reason"],
        "settled_at": datetime.now().isoformat(timespec="seconds"),
    }
    if note:
        fields["anchor_note"] = note
    # 病灶②: 出场 ET 日 ≤ arm_date ⇒ 该行结局不是隔夜结局, 不进 Δ 聚合
    if _et_date(r["exit_time"]) and _et_date(r["exit_time"]) <= arm_date:
        fields.update({"status": "not_held", "held_overnight": False, "cf_delta": None,
                       "anchor_note": "出场 ET 日 ≤ arm_date: 决策时刻豁免成立但未过夜"})
        return True, fields
    if dec_price <= 0 or exit_price <= 0 or qty <= 0:
        fields.update({"status": "bad_price", "held_overnight": None, "cf_delta": None})
        return True, fields
    fields.update({"held_overnight": True,
                   "cf_delta": round(sign * (dec_price - exit_price) * qty, 2)})
    return True, fields


def trigger_rows(rows):
    """B19 触发组 (病灶③去重): would_revoke ∧ settled ∧ 真持夜 ∧ 有 Δ, 同 trade_id 取最早 arm_date。"""
    by_tid = {}
    for r in rows:
        if not r.get("would_revoke") or r.get("status") != "settled":
            continue
        if not r.get("held_overnight") or r.get("cf_delta") is None or r.get("trade_id") is None:
            continue
        cur = by_tid.get(r["trade_id"])
        if cur is None or str(r.get("arm_date")) < str(cur.get("arm_date")):
            by_tid[r["trade_id"]] = r
    return sorted(by_tid.values(), key=lambda r: str(r.get("arm_date")))


def _verdict(n_trig, sum_delta, miskill):
    """B19 机械判定 (与 2026-09-09 预注册逐字一致, RFC-095 未改任何阈值/方向)。"""
    if n_trig >= MIN_N and sum_delta > 0 and miskill <= MAX_MISKILL:
        return "GRADUATION_LINE_HIT"
    if n_trig >= MIN_N and (sum_delta <= 0 or miskill > MAX_MISKILL):
        return "GATE_NOT_WORTH"
    return "KEEP_SAMPLING"


def earnings_night_rows(rows):
    """Event-axis subset: settled, held overnight, is_earnings_night=True; dedupe by trade."""
    by_trade = {}
    for row in rows:
        if row.get("is_earnings_night") is not True or row.get("status") != "settled":
            continue
        if not row.get("held_overnight") or row.get("cf_delta") is None:
            continue
        key = row.get("trade_id") or (row.get("symbol"), row.get("arm_date"))
        current = by_trade.get(key)
        if current is None or str(row.get("arm_date")) < str(current.get("arm_date")):
            by_trade[key] = row
    return sorted(by_trade.values(), key=lambda row: str(row.get("arm_date")))


def earnings_night_summary(rows):
    subset = earnings_night_rows(rows)
    deltas = [float(row.get("cf_delta") or 0) for row in subset]
    unknown = sum(1 for row in rows if row.get("is_earnings_night") is None)
    return {
        "n": len(subset),
        "min_n": 10,
        "sum_delta": round(sum(deltas), 2) if deltas else None,
        "median_delta": round(statistics.median(deltas), 2) if deltas else None,
        "eod_better_n": sum(1 for value in deltas if value > 0),
        "hold_better_n": sum(1 for value in deltas if value < 0),
        "unknown_n": unknown,
        "verdict": "READY_FOR_REVIEW" if len(subset) >= 10 else "KEEP_SAMPLING",
        "sign_convention": "cf_delta>0 = EOD close better than holding; cf_delta<0 = holding better",
        "behavior": "record-only; never mixed into B19 trigger criteria",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="只处理 arm_date 在最近 N 天内的行 (0=全部)")
    args = ap.parse_args()

    rows = load_rows()
    if not rows:
        print("[eod_depth_gate] 影子文件为空 — 打点尚未产生 arm 行 (或开关未启用)")
        return

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    n_settled = n_unsettled = n_resettled = n_legacy_unverified = 0
    for row in rows:
        legacy = bool(row.get("settled")) and not row.get("match_by")
        if row.get("settled") and not legacy:
            continue
        ok, fields = settle_row(row, conn)
        if legacy:
            if not ok:
                # 存量行重算无锚: 原值保留但**不进 Δ 聚合** (标 legacy_unverified 可见)
                row["match_by"] = "legacy_unverified"
                row["rfc095_note"] = "身份锚定重算未中, 旧值保留仅呈报"
                n_legacy_unverified += 1
                continue
            if row.get("trade_id") != fields.get("trade_id") or \
                    abs(float(row.get("cf_delta") or 0) - float(fields.get("cf_delta") or 0)) > 0.005:
                # 旧值留痕 (不静默改口): 口径变更必须可见
                row["superseded"] = {
                    "trade_id": row.get("trade_id"), "exit_price": row.get("exit_price"),
                    "actual_net": row.get("actual_net"), "cf_delta": row.get("cf_delta"),
                    "match_rule": "legacy: same symbol + trade_date=entry_date + exit>=arm_date "
                                  "ORDER BY exit_time DESC LIMIT 1",
                    "note": "RFC-095 身份锚定重算前的旧值",
                }
                n_resettled += 1
                print(f"[RFC-095 重算] {row.get('arm_date')} {row.get('symbol')} "
                      f"trade_id {row.get('trade_id')}→{fields.get('trade_id')} "
                      f"Δ {row.get('cf_delta')}→{fields.get('cf_delta')} "
                      f"(match_by={fields.get('match_by')})")
            fields["resettled_by_rfc095"] = True
        elif not ok:
            # 无身份锚 / 仓未平: 保持 unsettled 待下轮 (绝不当成已结算)
            n_unsettled += 1
            continue
        row.update(fields)
        row["settled"] = True
        n_settled += 1
    conn.close()

    # 回写 jsonl (全量重写 — 行级幂等: settled 标志)
    if n_settled:
        SHADOW.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

    trig = trigger_rows(rows)
    sum_delta = sum(float(r.get("cf_delta") or 0) for r in trig)
    miskill = sum(1 for r in trig if float(r.get("cf_delta") or 0) < -FEE_LINE)
    n_trig = len(trig)
    raw_trig = sum(1 for r in rows if r.get("would_revoke") and r.get("status") == "settled"
                   and r.get("held_overnight") and r.get("cf_delta") is not None)
    not_held = sum(1 for r in rows if r.get("status") == "not_held")
    legacy_unverified = sum(1 for r in rows if r.get("match_by") == "legacy_unverified"
                            or (r.get("status") == "settled" and not r.get("match_by")))
    match_by = {}
    for r in rows:
        if r.get("status") in ("settled", "not_held"):
            k = str(r.get("match_by") or "legacy_unverified")
            match_by[k] = match_by.get(k, 0) + 1
    verdict = _verdict(n_trig, sum_delta, miskill)
    earnings_night = earnings_night_summary(rows)

    report = {
        # "n" = 注册表计数锚 (shadow_registry._count_samples 读 .json 时只认
        # n/n_total/n_events/stats 四键)。必须指向 **B19 判据 n 轴 = 触发组去重仓数**
        # (n_trig), 不是 arm 行数 — 同仓多夜会落多行, 用行数当计数锚会让 current_n
        # 虚假达标迁 READY_TO_EVALUATE (gap_chase_gate 9/21 踩过的同型假象:
        # data_path 指向无 n 键的产物 ⇒ current_n 恒 0, 被清账误读成"断供")。
        "n": n_trig,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows_total": len(rows),
        "newly_settled": n_settled,
        "unsettled_backlog": n_unsettled,
        "not_held_n": not_held,
        "resettled_n": n_resettled,
        "legacy_unverified_n": legacy_unverified,
        "match_by": match_by,
        "trigger_settled_n": n_trig,          # n 轴 = 去重仓数 (B19 判据轴)
        "trigger_settled_rows": raw_trig,     # 原始行数 (多夜重复计数, 仅诊断)
        "trigger_sum_delta": round(sum_delta, 2),
        "trigger_miskill_n": miskill,
        "min_n": MIN_N, "max_miskill": MAX_MISKILL,
        "verdict": verdict,
        "earnings_night": earnings_night,
        "criteria_ref": "B19 (docs/ADJUDICATED-CRITERIA.md)",
        "settle_rule": "RFC-095 身份锚定 v2: trade_ref(trade_records.id) > entry_time 精确 > "
                       "entry_price±0.5%+qty(出场晚于决策时刻); "
                       "无锚=unsettled 不猜; 未持夜(exit ET 日≤arm_date)不进 Δ; 触发组按 trade_id "
                       "取最早夜去重",
    }
    _atomic_write(OUT, json.dumps(report, ensure_ascii=False, indent=1))

    print("═══ RFC-079 EOD深水闸影子结算 (RFC-095 身份锚定 v2) ═══")
    print(f"arm行 总={len(rows)} 新结算={n_settled} 重算={n_resettled} 未结算积压={n_unsettled} "
          f"| 未持夜={not_held} 旧口径无法重算={n_legacy_unverified}")
    print(f"匹配依据分布={match_by}")
    print(f"触发组 n={n_trig}/{MIN_N} (原始行={raw_trig}) ΣΔ={sum_delta:+.2f} "
          f"误杀={miskill}/{MAX_MISKILL}")
    for r in trig:
        print(f"  {r.get('arm_date')} {str(r.get('symbol')):9s} depth={r.get('depth_net_pct')}% "
              f"dec={r.get('decision_price')} → 实际 {r.get('exit_price')} ({r.get('exit_reason')}) "
              f"Δ={r.get('cf_delta'):+.2f}")
    for r in rows:
        if r.get("status") == "not_held":
            print(f"  [未过夜] {r.get('arm_date')} {r.get('symbol')} {r.get('direction')} "
                  f"trade_id={r.get('trade_id')} depth={r.get('depth_net_pct')}% — "
                  f"{r.get('anchor_note')}")
    print(f"verdict={verdict} → {OUT.relative_to(ROOT)}")
    if verdict == "GRADUATION_LINE_HIT":
        print("⚠️ 已达毕业线: 按 B19 提请转硬闸裁决 (另立 DEB + 用户裁, 本脚本不自动生效)")


if __name__ == "__main__":
    main()
