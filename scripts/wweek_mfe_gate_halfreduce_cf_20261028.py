#!/usr/bin/env python3
"""W_WEEK 隔夜门三方案只读反事实 (DEB-20261028 第2轮 P2, 用户 2026-10-28 批准执行).

三方案对比 (全部 record-only, 零行为变更, 不新开影子):
  维持   = 现状 W_WEEK 豁免 (实际净额 A)
  二元门 = RFC-160 预注册谓词「EOD-MFE<0.5% ∧ 水下 → 不过夜」: 触发者按 arm 时刻
           水深价 EOD 平仓 (复用 wweek_exempt_counterfactual_20260929 口径)
  减半门 = 触发者 EOD 平一半、留一半持夜 (DEB-20261028 §7-B 动作变体; 兼容
           DEB-20260806「给翻盘机会」: 留半仓参与修复, 砍半跳空敞口; 误杀天然减半)

输入 (全部既有采集):
  - trade_records.metadata_json.overnight_arm {source, underwater_pct_net, marked_at_et, night_n}
  - trade_records.metadata_json.fee_breakdown (per-share 费率; 缺失回退 $4.2 平头/腿)
  - data/replay_bars/<SYM>_<DATE>.json 本地 1 分钟 bar (仅本地缓存, 不拉 futu ——
    盘中运行避免 OpenD 槽位争抢; 缺 bar 的样本退回全程 mfe_pct 代理并标 proxy=True)

EOD-MFE 口径 (RFC-160 闸的真实输入):
  入场日 [entry_time, 15:55 ET] 本地 bar 方向感知重建。
  代理偏差方向: 全程MFE ≥ EOD-MFE ⇒ 代理口径把部分「首日 EOD-MFE<0.5% 但次日冲高」
  的样本放进放行组 ⇒ 拦截组误杀被低估 (与 DEB-20261028 §5.16 声明一致)。

费用推导 (减半门):
  per-share 线性下 减半净额 = 0.5×(cf_close + actual) —— 两半腿费合计=一整腿费。
  另报「每额外腿 +$1 平头罚」稳健列 (覆盖费用里的平头成分)。
  与 20260929 脚本对账: 二元门 Δ(省) = cf_close − actual (正=门省钱)。

纪律:
  - 阈值 0.5% 是 RFC-160 预注册值, 本脚本只在注册阈值上复算, 不改阈值;
  - 输出描述性+反事实对比, 不构成转硬闸依据 (RFC-160 前瞻影子继续攒样);
  - 人群口径写明 (join/过滤), 与 DEB §5.9 (73 join) / §5.16 (79 未join) 分开列示。

运行: ./venv/bin/python scripts/wweek_mfe_gate_halfreduce_cf_20261028.py
输出: stdout 报告 + data/reports/wweek_mfe_gate_halfreduce_cf_20261028.json
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
BAR_DIR = REPO / "data" / "replay_bars"
OUT_JSON = REPO / "data" / "reports" / "wweek_mfe_gate_halfreduce_cf_20261028.json"

MFE_GATE_PCT = 0.5          # RFC-160 预注册阈值 (窗内禁改)
EOD_BAR_HHMM = (15, 55)     # 入场日 MFE 重建右端
FLAT_FEE_FALLBACK = 4.2     # 20260929 脚本口径回退 (单边出场腿)
FLAT_LEG_PENALTY = 1.0      # 减半门稳健列: 每额外腿平头罚

# 美股假日 2026 (与 wweek_exempt_counterfactual_20260929.py 同表)
HOL = {'2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19',
       '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25'}


def _next_trading_day(d: date) -> date:
    d = d + timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in HOL:
        d += timedelta(days=1)
    return d


def _bar_file(symbol: str, day: str) -> Path:
    return BAR_DIR / f"{symbol.replace('.', '_')}_{day}.json"


def _eod_mfe_from_bars(symbol: str, entry_time: str, entry_price: float,
                       direction: str) -> tuple[float | None, str]:
    """入场日 [entry, 15:55] 方向感知 MFE%。返回 (mfe_pct, 来源标记)。"""
    day = entry_time[:10]
    fp = _bar_file(symbol, day)
    if not fp.exists():
        return None, "no_bars"
    n_bad = 0
    try:
        bars = json.loads(fp.read_text())
    except Exception:
        return None, "bad_bars"
    if not isinstance(bars, list) or not bars:
        return None, "bad_bars"
    entry_hhmm = entry_time[11:16]
    peak = None
    is_long = direction.upper() == "LONG"
    for b in bars:
        try:
            hhmm = b[0][11:16]
            if hhmm < entry_hhmm or hhmm > "15:55":
                continue
            px = float(b[2]) if is_long else float(b[3])  # LONG 看 high, SHORT 看 low
            peak = px if peak is None else (max(peak, px) if is_long else min(peak, px))
        except Exception:
            n_bad += 1
    if n_bad:
        pass  # 计数披露于汇总 (坏 bar 行), 不静默
    if peak is None:
        return None, "no_bars"
    if is_long:
        mfe = (peak - entry_price) / entry_price * 100
    else:
        mfe = (entry_price - peak) / entry_price * 100
    return round(mfe, 4), f"bars_ok(bad={n_bad})" if n_bad else "bars_ok"


def _exit_leg_fee(qty: float, fee_bd: dict | None) -> float:
    """出场腿费用: 有 breakdown 按 per-share 费率重算该腿, 否则 $4.2 平头。"""
    if fee_bd and qty:
        rate_keys = ("commission", "settlement", "sec", "taf", "cat")
        total = sum(float(fee_bd.get(k) or 0) for k in rate_keys)
        # breakdown 是该腿实际费用; 减半时按 qty 比例线性缩放
        return total
    return FLAT_FEE_FALLBACK


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price,
                  quantity, COALESCE(pnl_net, realized_pnl) pnl, exit_reason, mfe_pct,
                  metadata_json
           FROM trade_records
           WHERE source_type='PLAYBOOK_SIM' AND entry_time>='2026-08-16'
           ORDER BY entry_time"""
    ).fetchall()

    n_bad_meta = 0
    family = []
    for r in rows:
        try:
            mj = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        except Exception:
            n_bad_meta += 1
            continue
        oa = mj.get("overnight_arm") or {}
        if oa.get("source") != "w_week":
            continue
        # 真跨日过滤 (DEB §5.9 口径: ET 日期字符串, 剔同日 after-hours)
        if not (r["exit_time"] and str(r["exit_time"])[:10] > str(r["entry_time"])[:10]):
            continue
        family.append((r, oa, mj))

    print(f"W_WEEK 真跨日已结算族 n={len(family)} | 坏 metadata 跳过 n={n_bad_meta}")

    detail = []
    n_no_uw = 0
    n_mfe_proxy = 0
    n_mfe_none = 0
    for r, oa, mj in family:
        uw = oa.get("underwater_pct_net")
        qty = float(r["quantity"] or 0)
        ep = float(r["entry_price"] or 0)
        actual = float(r["pnl"] or 0)
        marked = (oa.get("marked_at_et") or str(r["entry_time"]))[:10]
        span = (_next_trading_day(date.fromisoformat(marked)) - date.fromisoformat(marked)).days

        # EOD-MFE: 本地 bar 重建, 缺则全程 mfe_pct 代理
        eod_mfe, mfe_src = _eod_mfe_from_bars(r["symbol"], str(r["entry_time"]), ep,
                                              r["direction"])
        if eod_mfe is None:
            eod_mfe = r["mfe_pct"]
            mfe_src = f"proxy_whole_trade({mfe_src})"
            n_mfe_proxy += 1
            if eod_mfe is None:
                n_mfe_none += 1
                continue

        fee_bd = (mj.get("fee_breakdown") or {}).get("exit")
        fee_out = _exit_leg_fee(qty, fee_bd)

        if uw is None or not qty or not ep:
            n_no_uw += 1
            cf_close = None
        else:
            cf_close = ep * qty * (float(uw) / 100.0) - fee_out

        detail.append(dict(
            tid=r["id"], sym=r["symbol"], dir=r["direction"],
            entry_d=str(r["entry_time"])[:10], marked=marked,
            night_n=oa.get("night_n"), span=span,
            uw=uw, eod_mfe=eod_mfe, mfe_src=mfe_src, mfe_whole=r["mfe_pct"],
            actual=round(actual, 2), cf_close=round(cf_close, 2) if cf_close is not None else None,
            er=r["exit_reason"], qty=qty, ep=ep,
            fee_out=round(fee_out, 2),
        ))

    print(f"EOD-MFE 来源: bar 重建 {sum(1 for d in detail if d['mfe_src'].startswith('bars_ok'))} | "
          f"全程代理 {n_mfe_proxy} | 无 MFE 剔除 {n_mfe_none} | 无水深数据 {n_no_uw}")

    # ── 三方案对比: 门 = EOD-MFE<0.5% ∧ 水下(uw<0) ──
    def _report(gate_label: str, gated: list[dict], ungated: list[dict]) -> dict:
        rep = {"gate": gate_label, "n_gated": len(gated), "n_ungated": len(ungated)}
        for name, grp in (("gated", gated), ("ungated", ungated)):
            hold = sum(d["actual"] for d in grp)
            cfable = [d for d in grp if d["cf_close"] is not None]
            close = sum(d["cf_close"] for d in cfable) if cfable else None
            # 减半门: per-share 线性 → 0.5×(cf+actual); 稳健列每条额外腿 +$1 平头
            half = (sum(0.5 * (d["cf_close"] + d["actual"]) for d in cfable)
                    if cfable else None)
            half_robust = (sum(0.5 * (d["cf_close"] + d["actual"]) - FLAT_LEG_PENALTY
                               for d in cfable) if cfable else None)
            # 误杀: 二元门使该笔变差 >$5 (RFC-160 费地板)
            n_killed = sum(1 for d in cfable if (d["cf_close"] - d["actual"]) < -5)
            rep[name] = {
                "n": len(grp), "hold_net": round(hold, 2),
                "n_cf": len(cfable),
                "close_net": round(close, 2) if close is not None else None,
                "half_net": round(half, 2) if half is not None else None,
                "half_net_robust": round(half_robust, 2) if half_robust is not None else None,
                "binary_benefit": round((close - sum(d["actual"] for d in cfable)), 2) if close is not None else None,
                "half_benefit": round((half - sum(d["actual"] for d in cfable)), 2) if half is not None else None,
                "n_killed_binary": n_killed,
            }
        return rep

    gated = [d for d in detail if (d["uw"] or 0) < 0 and d["eod_mfe"] < MFE_GATE_PCT]
    ungated = [d for d in detail if not ((d["uw"] or 0) < 0 and d["eod_mfe"] < MFE_GATE_PCT)]
    rep_main = _report("EOD-MFE<0.5% ∧ uw<0", gated, ungated)

    # 代理口径迁移检查: 仅 bars_ok 样本, 比较 EOD-MFE vs 全程 MFE 两种判别器的分组差异
    bars_ok = [d for d in detail if d["mfe_src"].startswith("bars_ok") and d["mfe_whole"] is not None]
    churn = sum(1 for d in bars_ok
                if ((d["uw"] or 0) < 0 and d["eod_mfe"] < MFE_GATE_PCT)
                != ((d["uw"] or 0) < 0 and float(d["mfe_whole"]) < MFE_GATE_PCT))

    # ── 周末/假日分层 ──
    wk_g = [d for d in gated if d["span"] >= 3]
    wk_u = [d for d in ungated if d["span"] >= 3]
    rep_weekend = _report("周末/假日夜子集", wk_g, wk_u)

    print("\n=== 三方案对比 (门: EOD-MFE<0.5% ∧ 水下) ===")
    print(f"代理迁移检查: bars_ok 样本 n={len(bars_ok)}, 全程MFE 与 EOD-MFE 判别器分组不一致 n={churn} "
          f"(>0 即实证 DEB §5.16 的代理偏差)")
    for seg, seg_rep in (("全部样本", rep_main), ("周末/假日夜", rep_weekend)):
        print(f"\n[{seg}]")
        for grp in ("gated", "ungated"):
            g = seg_rep[grp]
            print(f"  {grp:8s} n={g['n']:3d} 维持=${g['hold_net']:>9.2f} | "
                  f"可反事实 n={g['n_cf']:3d} 二元门=${g['close_net'] if g['close_net'] is not None else float('nan'):>9.2f} "
                  f"减半门=${g['half_net'] if g['half_net'] is not None else float('nan'):>9.2f} "
                  f"(稳健${g['half_net_robust'] if g['half_net_robust'] is not None else float('nan'):>9.2f}) | "
                  f"二元Δ={g['binary_benefit']} 减半Δ={g['half_benefit']} 误杀={g['n_killed_binary']}")

    print("\n=== 拦截组逐笔 (按二元Δ升序, 最误杀在前) ===")
    for d in sorted((d for d in gated if d["cf_close"] is not None),
                    key=lambda x: x["cf_close"] - x["actual"]):
        b = d["cf_close"] - d["actual"]
        print(f"  {d['sym']:9s} {d['dir']:5s} {d['marked']} 夜{d['night_n']} 跨{d['span']}d "
              f"uw={d['uw']:.2f}% eod_mfe={d['eod_mfe']:.3f}%({d['mfe_src']}) "
              f"实际${d['actual']:>8.2f} EOD平${d['cf_close']:>8.2f} 二元Δ${b:>+8.2f} "
              f"减半Δ${0.5*b - 0:>+7.2f} {d['er']}")

    out = dict(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        gate=f"EOD-MFE<{MFE_GATE_PCT}% ∧ underwater",
        population=f"W_WEEK 真跨日已结算 n={len(detail)} (entry>=2026-08-16, PLAYBOOK_SIM)",
        mfe_source=dict(bars_ok=sum(1 for d in detail if d["mfe_src"].startswith("bars_ok")),
                        proxy_whole_trade=n_mfe_proxy, dropped_no_mfe=n_mfe_none),
        proxy_churn=dict(n_bars_ok=len(bars_ok), n_group_diff=churn),
        fee_model="per-share linear (fee_breakdown), fallback $4.2/leg; half=0.5×(cf+actual), robust=+$1/leg",
        main=rep_main, weekend=rep_weekend, detail=detail,
    )
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n落盘: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
