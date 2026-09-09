#!/usr/bin/env python3
"""EOD 深水闸影子结算器 (RFC-079, 2026-09-09, record-only 影子的判分侧).

对 data/shadow/eod_depth_gate_shadow.jsonl 的 arm 行 (EOD 豁免成立时打点):
  - 关联实际结局: trade_records 同 symbol + entry_date_et 的已平仓行 (exit_time ≥ arm_date)
  - Δ = 反事实"EOD 按 decision_price 平" vs 实际结局 的净额差。
    费结构两边同为一次往返 → Δ = sign × (decision_price − exit_price) × qty (纯价差, 精确)。
  - would_revoke 行的 Δ>0 = 深水闸若生效可省下的钱; Δ<0 = 误杀 (豁免仓隔夜修复)。

判据 (判据字典 B19, 预注册 2026-09-09):
  触发结算 n≥8 ∧ ΣΔ>0 ∧ 误杀赢家(Δ<0 且 |Δ|>费用线)≤1 → 提请转硬闸 (另立裁决, 不自动生效)。
  回溯证据 (8/1-9/8 回放): -2.5% 阈值 3/3 命中灾难, ΣΔ=+$410, 误杀 0。

挂载: crontab 30 6 * * 2-6 (与 winner settle 同窗, 复用 data/replay_bars 落盘后的时段)。
数据源: trade_records (ftseek_us.db, 只读)。arm 行未平仓 → 保留 unsettled 待下次。

用法: venv/bin/python scripts/settle_eod_depth_gate_shadow.py [--days N]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "ftseek_us.db"
SHADOW = ROOT / "data" / "shadow" / "eod_depth_gate_shadow.jsonl"
OUT = ROOT / "data" / "shadow" / "eod_depth_gate_report_latest.json"

MIN_N = 8          # B19 触发结算样本下限
MAX_MISKILL = 1    # B19 误杀赢家上限
FEE_LINE = 5.0     # Δ<0 且 |Δ|>费地板 才算"误杀赢家" (微负=费差噪声)


def _atomic_write(path: Path, text: str):
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


def settle_row(row, conn):
    """返回 (settled: bool, fields dict). 未平仓/找不到结局 → (False, {})."""
    sym = row.get("symbol")
    entry_date = row.get("entry_date_et")
    arm_date = row.get("arm_date")
    if not sym or not arm_date:
        return True, {"status": "bad_row"}
    q = """
        SELECT id, exit_time, exit_price, pnl_net, realized_pnl, exit_reason, quantity
        FROM trade_records
        WHERE symbol=? AND substr(trade_date,1,10)=? AND exit_price > 0
          AND substr(exit_time,1,10) >= ?
        ORDER BY exit_time DESC LIMIT 1
    """
    r = conn.execute(q, (sym, entry_date or "", arm_date)).fetchone()
    if not r:
        # 可能仍持仓 (豁免继续多夜) 或已平但 trade_date 标签不同 — 保留 unsettled
        return False, {}
    exit_price = float(r["exit_price"] or 0)
    net = r["pnl_net"] if r["pnl_net"] is not None else (r["realized_pnl"] or 0)
    qty = float(row.get("qty") or r["quantity"] or 0)
    dec_price = float(row.get("decision_price") or 0)
    sign = 1.0 if str(row.get("direction", "")).upper() in ("LONG", "BUY") else -1.0
    if dec_price <= 0 or exit_price <= 0 or qty <= 0:
        return True, {"status": "bad_price", "trade_id": r["id"]}
    delta = sign * (dec_price - exit_price) * qty   # cf(EOD平) − actual, 费两边抵消
    return True, {
        "status": "settled",
        "trade_id": r["id"],
        "exit_time": r["exit_time"],
        "exit_price": exit_price,
        "actual_net": round(float(net or 0), 2),
        "exit_reason": r["exit_reason"],
        "cf_delta": round(delta, 2),
        "settled_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
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

    n_settled = n_unsettled = 0
    trig_rows = []   # would_revoke 且已结算
    for row in rows:
        if row.get("settled"):
            if row.get("would_revoke"):
                trig_rows.append(row)
            continue
        ok, fields = settle_row(row, conn)
        if not ok:
            n_unsettled += 1
            continue
        row.update(fields)
        row["settled"] = True
        n_settled += 1
        if row.get("would_revoke") and fields.get("status") == "settled":
            trig_rows.append(row)
    conn.close()

    # 回写 jsonl (全量重写 — 行级幂等: settled 标志)
    if n_settled:
        SHADOW.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")

    # B19 判据机械判定 (仅 would_revoke 触发组)
    sum_delta = sum(float(r.get("cf_delta") or 0) for r in trig_rows)
    miskill = sum(1 for r in trig_rows if float(r.get("cf_delta") or 0) < -FEE_LINE)
    n_trig = len(trig_rows)
    if n_trig >= MIN_N and sum_delta > 0 and miskill <= MAX_MISKILL:
        verdict = "GRADUATION_LINE_HIT"
    elif n_trig >= MIN_N and (sum_delta <= 0 or miskill > MAX_MISKILL):
        verdict = "GATE_NOT_WORTH"
    else:
        verdict = "KEEP_SAMPLING"

    report = {
        "rows_total": len(rows),
        "newly_settled": n_settled,
        "unsettled_backlog": n_unsettled,
        "trigger_settled_n": n_trig,
        "trigger_sum_delta": round(sum_delta, 2),
        "trigger_miskill_n": miskill,
        "min_n": MIN_N, "max_miskill": MAX_MISKILL,
        "verdict": verdict,
    }
    _atomic_write(OUT, json.dumps(report, ensure_ascii=False, indent=1))

    print(f"═══ RFC-079 EOD深水闸影子结算 ═══")
    print(f"arm行 总={len(rows)} 新结算={n_settled} 未结算积压={n_unsettled}")
    print(f"触发组(would_revoke已结算) n={n_trig}/{MIN_N} ΣΔ={sum_delta:+.2f} 误杀={miskill}/{MAX_MISKILL}")
    for r in trig_rows:
        print(f"  {r.get('arm_date')} {r.get('symbol'):9s} depth={r.get('depth_net_pct')}% "
              f"dec={r.get('decision_price')} → 实际 {r.get('exit_price')} ({r.get('exit_reason')}) "
              f"Δ={r.get('cf_delta'):+.2f}")
    print(f"verdict={verdict} → {OUT.relative_to(ROOT)}")
    if verdict == "GRADUATION_LINE_HIT":
        print("⚠️ 已达毕业线: 按 B19 提请转硬闸裁决 (另立 DEB + 用户裁, 本脚本不自动生效)")


if __name__ == "__main__":
    main()
