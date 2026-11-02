#!/usr/bin/env python3
"""Trail/BE 的 ATR 缩放 floor 只读回放 (DEB-20261028 第2轮 P1, 用户 2026-10-28 批准执行).

⚠️ 设计更正所谓 (执行中发现, 已在 DEB §12 第2轮补记):
  原批口径「往 exit_param_shadow 加变体」不可行 —— 该影子有硬不变量
  「只能评估比现行更紧的变体, 更松的会骑过实盘退出点, 路径不可观测」
  (exit_shadow.py:15)。ATR 缩放 floor 对高波标的是【更宽】变体, 必须用
  分钟 bar 全路径回放。本脚本即替代载体: 零生产变更、零新影子, 且证据立即
  可得 (回看历史), 不必等前瞻攒样。

回放语义 (忠实复刻 exit_engine._compute_dynamic_effective_sl + adaptive_trailing):
  - peak_profit_pct ≥ be 才武装保本线 entry×(1+cushion/100)
  - 武装后 dd(mfe) = clamp(0.4×mfe, floor, 1.2) (target_capture 60%, SIM-only)
  - effective_sl = max(initial_sl, be_line, peak×(1−dd/100)) (LONG; SHORT 镜像)
  - initial_sl 取该笔 exit_telemetry.initial_sl (含 tighten_sl_by_beta 后的实测值)
  - cushion 取该笔 _be_cushion_fee_aware (fee-aware), 缺失回退 config 值

变体 (恒不松于…不, 恒【不更紧】: 只放宽, 与现行对照):
  live_repl : be=0.4,        floor=0.4                (保真度基准: 应复现实际出场)
  v_floor   : be=0.4,        floor=max(0.4, 0.10×ATR%)  (只修 min 饱和, DYN 型)
  v_be      : be=max(0.4, 0.10×ATR%), floor=0.4          (只修 BE 武装点, 今日 BE 群型)
  v_both    : 两者同调

口径与已知偏差 (全部声明, 不静默):
  - bar 内时序未知: 保守口径 = 先检 stop(用旧 peak) 后更新 peak(bar high);
    乐观口径 = 先更新 peak 后检 stop。主表报保守, 附乐观 Δ 作区间。
  - 触发即按 stop 价成交 (理想化); 实盘 BE 存在 limit→fallback_market 滑点
    (DYN 实测 fill_vs_limit 33bps) ⇒ 对「变体 vs 实际出场价」的 Δ 略偏乐观;
    故同报「变体 vs live_repl 重现价」的 Δ 隔离参数效应 (同理想化口径, 干净对照)。
  - 两腿费用各路径均只出场一次 ⇒ Δnet = Δgross (fee-linearity 声明, 与
    runner_exit_counterfactual.py 同款)。
  - live 隔夜仓 (exit 日期 > entry 日期): 变体只在入场日走 bar; 变体首日未触发则
    16:00 强制平仓并标 live_overnight=True, 与主表分列 (隔夜豁免逻辑不在本变体面)。
  - 仅用本地 replay_bars 缓存, 不拉 futu (盘中运行避免 OpenD 槽位争抢);
    缺 bar / 截断 (末根<15:55) 计数披露。

运行: ./venv/bin/python scripts/trail_floor_atr_cf_20261028.py
输出: stdout 报告 + data/reports/trail_floor_atr_cf_20261028.json
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DB = REPO / "data" / "ftseek_us.db"
BAR_DIR = REPO / "data" / "replay_bars"
OUT_JSON = REPO / "data" / "reports" / "trail_floor_atr_cf_20261028.json"

ADAPTIVE_FACTOR = 0.4   # 1 − target_capture 60% (unified_config adaptive_trailing_*)
DD_CAP = 1.2            # adaptive_trailing_max_drawdown_pct
DD_FLOOR_LIVE = 0.4     # adaptive_trailing_min_drawdown_pct
BE_LIVE = 0.4           # 16 条 trailing=true 规则全部 breakeven_at_pct=0.4 (DEB §5.5)
ATR_FLOOR_K = 0.10      # 变体: floor/be = max(live, k×ATR%)
EOD_HHMM = "15:55"      # 截断判定与 MFE 右端 (与 post_exit_path 同约定)

EXIT_REASONS = ("SL_BREAKEVEN", "SL_TRAIL_LOCK")


def _load_bars(symbol: str, day: str):
    fp = BAR_DIR / f"{symbol.replace('.', '_')}_{day}.json"
    if not fp.exists():
        return None, "no_bars"
    try:
        bars = json.loads(fp.read_text())
    except Exception:
        return None, "bad_bars"
    if not isinstance(bars, list) or not bars:
        return None, "bad_bars"
    return bars, "ok"


def _walk(bars, entry_price: float, direction: str, initial_sl: float,
          cushion_pct: float, be_pct: float, floor_pct: float,
          optimistic: bool, bad_bars: Optional[Counter] = None) -> tuple[float | None, str, int]:
    """分钟 bar 状态机回放。返回 (exit_price, exit_reason, exit_bar_idx)。"""
    is_long = direction.upper() == "LONG"
    stop = initial_sl
    peak = entry_price
    for i, b in enumerate(bars):
        try:
            hi, lo = float(b[2]), float(b[3])
        except Exception:
            if bad_bars is not None:
                bad_bars["malformed_bar"] += 1
            continue

        def _stop_hit() -> bool:
            return (lo <= stop) if is_long else (hi >= stop)

        if not optimistic and _stop_hit():
            return stop, "STOP", i
        # 更新 peak / 武装 / 重算 stop
        peak = (max(peak, hi) if is_long else min(peak, lo))
        mfe = ((peak - entry_price) if is_long else (entry_price - peak)) / entry_price * 100
        if mfe >= be_pct:
            be_line = (entry_price * (1 + cushion_pct / 100.0) if is_long
                       else entry_price * (1 - cushion_pct / 100.0))
            stop = (max(stop, be_line) if is_long else min(stop, be_line))
            if mfe > be_pct:
                dd = min(max(ADAPTIVE_FACTOR * mfe, floor_pct), DD_CAP)
                dd_line = (peak * (1 - dd / 100.0) if is_long
                           else peak * (1 + dd / 100.0))
                stop = (max(stop, dd_line) if is_long else min(stop, dd_line))
        if optimistic and _stop_hit():
            return stop, "STOP", i
    # 未触发 → 末根 close 强平
    try:
        return float(bars[-1][4]), "EOD", len(bars) - 1
    except Exception:
        return None, "no_bars", -1


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price,
                  quantity, COALESCE(pnl_net, realized_pnl) pnl_net, exit_reason, metadata_json
           FROM trade_records
           WHERE source_type='PLAYBOOK_SIM' AND exit_time IS NOT NULL
             AND entry_time >= '2026-06-26'
             AND json_extract(metadata_json,'$._adaptive_trailing.drawdown_pct') IS NOT NULL
             AND exit_reason IN ('SL_BREAKEVEN','SL_TRAIL_LOCK')
           ORDER BY entry_time"""
    ).fetchall()

    try:
        from app.playbook.config import get_playbook_config, BE_FALLBACK_FEE_CUSHION_PCT
        _cfg_cushion = float(get_playbook_config().get("breakeven_fee_cushion_pct",
                                                       BE_FALLBACK_FEE_CUSHION_PCT))
    except Exception:
        _cfg_cushion = 0.15

    n_bad_meta = 0
    n_skip = {"no_bars": 0, "bad_bars": 0, "no_initial_sl": 0, "no_atr": 0}
    n_truncated = 0
    walk_bad = Counter()
    details = []
    for r in rows:
        try:
            mj = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        except Exception:
            n_bad_meta += 1
            continue
        et = mj.get("exit_telemetry") or {}
        bl = mj.get("bucket_lineage") or {}
        initial_sl = et.get("initial_sl") or bl.get("stop_loss")
        if not initial_sl:
            n_skip["no_initial_sl"] += 1
            continue
        atr_frac = bl.get("atr_pct_repriced") or bl.get("atr_pct_generated")
        if not atr_frac:
            n_skip["no_atr"] += 1
            continue
        atr_pct = float(atr_frac) * 100.0  # 存的是分数 (0.05598) → %
        cushion = mj.get("_be_cushion_fee_aware")
        cushion = float(cushion) if cushion else _cfg_cushion

        day = str(r["entry_time"])[:10]
        bars, st = _load_bars(r["symbol"], day)
        if bars is None:
            n_skip[st] += 1
            continue
        entry_hhmm = str(r["entry_time"])[11:16]
        bars = [b for b in bars if b[0][11:16] >= entry_hhmm]
        if not bars:
            n_skip["no_bars"] += 1
            continue
        truncated = bars[-1][0][11:16] < EOD_HHMM
        if truncated:
            n_truncated += 1

        ep = float(r["entry_price"])
        qty = float(r["quantity"] or 0)
        is_long = r["direction"].upper() == "LONG"
        live_overnight = str(r["exit_time"])[:10] > day
        atr_floor = max(DD_FLOOR_LIVE, ATR_FLOOR_K * atr_pct)
        atr_be = max(BE_LIVE, ATR_FLOOR_K * atr_pct)

        variants = {
            "live_repl": dict(be=BE_LIVE, floor=DD_FLOOR_LIVE),
            "v_floor": dict(be=BE_LIVE, floor=atr_floor),
            "v_be": dict(be=atr_be, floor=DD_FLOOR_LIVE),
            "v_both": dict(be=atr_be, floor=atr_floor),
        }
        out = {}
        for vn, vp in variants.items():
            px, reason, _bi = _walk(bars, ep, r["direction"], float(initial_sl),
                                    cushion, vp["be"], vp["floor"], optimistic=False,
                                    bad_bars=walk_bad)
            px_o, _r_o, _ = _walk(bars, ep, r["direction"], float(initial_sl),
                                  cushion, vp["be"], vp["floor"], optimistic=True,
                                  bad_bars=walk_bad)
            out[vn] = (px, px_o, reason)
        rec = dict(
            tid=r["id"], sym=r["symbol"], dir=r["direction"], day=day,
            bucket=bl.get("bucket_effective"), atr_pct=round(atr_pct, 3),
            entry=ep, qty=qty, actual_exit=float(r["exit_price"]),
            actual_reason=r["exit_reason"], live_overnight=live_overnight,
            truncated=truncated,
            live_repl=out["live_repl"][0], live_repl_reason=out["live_repl"][2],
            v_floor=out["v_floor"][0], v_floor_opt=out["v_floor"][1],
            v_be=out["v_be"][0], v_be_opt=out["v_be"][1],
            v_both=out["v_both"][0], v_both_opt=out["v_both"][1],
        )
        details.append(rec)

    print(f"人群: adaptive-metadata ∧ BE/TRAIL_LOCK 已平 n={len(rows)} | "
          f"进入回放 n={len(details)} | 坏meta {n_bad_meta} | 跳过 {n_skip} | "
          f"截断日 {n_truncated} (计入回放, 非跳过) | 坏bar {dict(walk_bad)}")

    def _delta(rec, variant_px, base_px):
        if variant_px is None or base_px is None:
            return None
        d = (variant_px - base_px) if rec["dir"] == "LONG" else (base_px - variant_px)
        return d * rec["qty"]

    # 保真度: live_repl vs 实际出场价 (±0.1% 或 ±$0.02 容差)
    fid_n = fid_ok = 0
    for rec in details:
        if rec["live_repl"] is None:
            continue
        fid_n += 1
        tol = max(0.001 * rec["actual_exit"], 0.02)
        if abs(rec["live_repl"] - rec["actual_exit"]) <= tol:
            fid_ok += 1
    fidelity = (fid_ok / fid_n) if fid_n else 0.0
    print(f"保真度: live_repl 复现实际出场 {fid_ok}/{fid_n} = {fidelity:.3f} "
          f"(RFC-154 冻结线 0.80; <0.80 读数仅作描述)")

    print(f"\n=== 变体对比 (保守口径; Δ vs 实际出场价, $; 正=变体更优) ===")
    hdr = f"{'子集':22s} {'n':>3s} " + " ".join(f"{v:>18s}" for v in ("v_floor", "v_be", "v_both"))
    print(hdr)

    def _report_group(label, grp):
        line = f"{label:22s} {len(grp):3d} "
        for vn in ("v_floor", "v_be", "v_both"):
            ds = [_delta(rec, rec[vn], rec["actual_exit"]) for rec in grp]
            ds = [d for d in ds if d is not None]
            if not ds:
                line += f"{'—':>18s}"
                continue
            s = sum(ds)
            w = sum(1 for d in ds if d > 0)
            line += f"{s:>+10.2f}({w}/{len(ds)})  "
        print(line)

    groups = [("全部", details),
              ("日内 (live未隔夜)", [d for d in details if not d["live_overnight"]]),
              ("live隔夜", [d for d in details if d["live_overnight"]])]
    for b in ("medium", "high", "extreme", None):
        grp = [d for d in details if d["bucket"] == b]
        if grp:
            groups.append((f"桶={b}", grp))
    for label, grp in groups:
        _report_group(label, grp)

    print(f"\n=== v_both 逐笔 |Δ|≥$20 (保守口径, Δ vs 实际) ===")
    rows_out = []
    for rec in details:
        d = _delta(rec, rec["v_both"], rec["actual_exit"])
        if d is None or abs(d) < 20:
            continue
        d_opt = _delta(rec, rec["v_both_opt"], rec["actual_exit"])
        d_repl = _delta(rec, rec["v_both"], rec["live_repl"])
        rows_out.append((d, rec, d_opt, d_repl))
    for d, rec, d_opt, d_repl in sorted(rows_out, key=lambda x: -abs(x[0])):
        print(f"  {rec['sym']:9s} {rec['dir']:5s} {rec['day']} 桶={rec['bucket'] or '?':6s} "
              f"ATR={rec['atr_pct']:.2f}% 实际出={rec['actual_exit']:.2f}({rec['actual_reason']}) "
              f"v_both={rec['v_both']:.2f} Δ${d:+8.2f} [乐观{d_opt:+8.2f}] "
              f"[隔离参数Δ{d_repl:+8.2f}]{' [live隔夜]' if rec['live_overnight'] else ''}")

    out = dict(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        population=f"adaptive-metadata ∧ {EXIT_REASONS} ∧ entry>=2026-06-26",
        n_total=len(rows), n_replayed=len(details), n_bad_meta=n_bad_meta, n_skip=n_skip,
        n_truncated=n_truncated, n_malformed_bar=walk_bad.get("malformed_bar", 0),
        fidelity=dict(n=fid_n, ok=fid_ok, ratio=round(fidelity, 3),
                      note="RFC-154 冻结线 0.80; 容差 ±0.1%或±$0.02"),
        variant_spec=dict(v_floor=f"floor=max(0.4,{ATR_FLOOR_K}×ATR%)",
                          v_be=f"be=max(0.4,{ATR_FLOOR_K}×ATR%)", v_both="两者同调"),
        fee_model="Δnet=Δgross (各路径均单次出场; fee-linearity 声明)",
        details=details,
    )
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n落盘: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
