#!/usr/bin/env python3
"""因子权重对账 (纯只读): 三把尺子对账 —— IC 通道 / 台账 verdict / 现金口径。

背景 (为什么要有这把尺子)
    playbook screener 给每个**触发**因子加的分, 实测由三处相加 (代码实测, 非估算):

        eff = base(config/playbook_screening.yaml, screening_factors[*].weight)
            + ic_adj(data/factor_ic_latest.json: |IC|>0.05 且 n>=30 → ±1, 见
                     app/playbook/screener.py::_load_factor_ic_adjustments)
            + Σhist(config/playbook_screening.yaml, weight_adjustments 全量逐条加一次,
                    见 app/playbook/screener.py::_calculate_score L352-359)

    而**同一个因子**另有两套独立读数: 台账 verdict (app/playbook/factor_analyzer.py::
    classify_factor — 贝叶斯胜率区间 + profit_factor) 与现金口径 (同表的 total_pnl)。
    三把尺子各写各的, 没有任何一处在对账 ⇒ 会出现「正权重买负证据」「罚错了」
    「尺子互相矛盾」而无人知晓 (2026-09-21 审计实测)。

本脚本查什么 (全部只读, 只写两份自己的产物)
    A 正权重 × 负证据   enabled 且 eff>0 且 台账 verdict=negative 且 台账 n>=30
    B IC 通道反号       ic_adj 符号与台账 verdict 相反 (IC 说好、台账说坏, 或反之)
    C Σhist 双计存量    weight_adjustments 非空的因子 (与 base 是否已含该 delta 无法唯一分解)
    D 小样本高权重      eff>0 且 台账 n<30 (权重之王压在 n=1 的因子上)
    E 罚错了            eff<0 且 verdict!=negative 且现金口径 >0
    F 尺子冲突          verdict=negative 而 total_pnl>0 (或反向) —— 胜率口径与金额口径分歧
    G 台账分母口径      verdict=negative 但**剔掉非结果行** (missed/already_held/void, pnl 为 NULL/0)
                        后胜率 ≥50% ⇒ 该 negative 是分母口径造出来的, 不是"亏钱"

静默契约
    只在**对账面发生变化**时输出 (首次运行输出全量); 无变化 → 零输出 = 不推送。
    对账面签名 = 每因子 `verdict|n档|eff符号|ic_adj|金额符号` (不含绝对金额/mtime, 否则每轮都"变")。

不改任何东西: 不写 config/ 与 prompt, 不写 DB (台账以 mode=ro 打开), 不碰交易路径。
    权重调整属策略变更 ⇒ 本脚本只出清单, 处置留给用户裁决 (见 docs/debates/DEB-20260921-*)。

用法: python scripts/factor_weight_reconcile.py [--dry] [--force]
    · --dry   只打印, 不写 state/产物 (人工查看)
    · --force 忽略 state, 强制输出全量
    · 环境变量 FTSEEK_LEDGER_DB 可换台账库 (默认 sim: data/ftseek_us.db)
退出码: 0 正常 (含"静默"); 2 = 输入缺失/探测失败 ⇒ 必须可见, 不得读成"无错配"。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
YAML_PATH = REPO / "config" / "playbook_screening.yaml"
IC_PATH = REPO / "data" / "factor_ic_latest.json"
LEDGER_DB = Path(os.environ.get("FTSEEK_LEDGER_DB", str(REPO / "data" / "ftseek_us.db")))
PRODUCT = REPO / "data" / "factor_weight_reconcile_latest.json"
STATE = REPO / "data" / "factor_weight_reconcile_state.json"

MIN_LEDGER_N = 30   # 台账样本门 (与 ic_adj 的 n>=30 同量级)
IC_MIN_N = 30
IC_TH = 0.05


def _ic_adj(stat: dict) -> int:
    """逐字复刻 screener._load_factor_ic_adjustments 的判据。"""
    ic, n = stat.get("ic"), stat.get("n") or 0
    if ic is None or n < IC_MIN_N:
        return 0
    if ic > IC_TH:
        return 1
    if ic < -IC_TH:
        return -1
    return 0


def _ledger() -> tuple[dict, str]:
    """读台账 (只读)。返回 ({factor_key: row}, 最新 last_updated)。"""
    if not LEDGER_DB.exists():
        raise FileNotFoundError(f"台账库不存在: {LEDGER_DB}")
    uri = f"file:{LEDGER_DB}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT factor_key, total_trades, wins, losses, win_rate, avg_pnl, total_pnl, "
            "       profit_factor, verdict, last_updated "
            "FROM playbook_factor_stats WHERE analysis_level = 'single'").fetchall()
    finally:
        conn.close()
    out, newest = {}, ""
    for k, n, w, l, wr, avg, tot, pf, vd, lu in rows:
        out[k] = {"n": n, "wins": w, "losses": l, "wr": wr, "avg_pnl": avg, "total_pnl": tot,
                  "pf": pf, "verdict": vd, "last_updated": lu}
        if lu and lu > newest:
            newest = lu
    return out, newest


def _sign(x) -> str:
    if x is None:
        return "?"
    return "+" if x > 0 else ("-" if x < 0 else "0")


def _n_band(n) -> str:
    return "n<30" if (n or 0) < MIN_LEDGER_N else "n>=30"


def collect() -> dict:
    cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8")) or {}
    factors = cfg.get("screening_factors", {}) or {}
    hist: dict = {}
    for adj in cfg.get("weight_adjustments", []) or []:
        f = adj.get("factor")
        if f is not None:
            hist[f] = hist.get(f, 0) + int(adj.get("delta", 0) or 0)
    ic = json.loads(IC_PATH.read_text(encoding="utf-8"))
    ledger, ledger_ts = _ledger()

    items = []
    for name, fcfg in factors.items():
        st = ic.get(name) or {}
        base = int(fcfg.get("weight", 0) or 0)
        adj = _ic_adj(st)
        h = hist.get(name, 0)
        eff = base + adj + h
        row = ledger.get(name) or {}
        wins, losses = row.get("wins") or 0, row.get("losses") or 0
        n_decided = wins + losses
        items.append({
            "factor": name,
            "enabled": bool(fcfg.get("enabled", False)),
            "base": base, "ic_adj": adj, "sigma_hist": h, "eff": eff,
            "resonance_gated": bool(fcfg.get("requires_co_factor", False)),
            "ic": st.get("ic"), "ic_n": st.get("n"),
            "ledger_n": row.get("n"), "ledger_wr": row.get("wr"),
            "ledger_wins": wins, "ledger_losses": losses, "ledger_n_decided": n_decided,
            "ledger_wr_decided": (wins / n_decided) if n_decided else None,
            "ledger_nonoutcome_share": ((row.get("n") or 0) - n_decided) / (row.get("n") or 1)
            if row.get("n") else None,
            "ledger_avg_pnl": row.get("avg_pnl"), "ledger_total_pnl": row.get("total_pnl"),
            "ledger_pf": row.get("pf"), "ledger_verdict": row.get("verdict"),
        })
    try:
        ledger_db_label = str(LEDGER_DB.relative_to(REPO))
    except ValueError:
        ledger_db_label = str(LEDGER_DB)
    return {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "ledger_db": ledger_db_label,
        "ledger_updated_at": ledger_ts,
        "ic_snapshot": ic.get("_meta", {}).get("generated_at") if isinstance(ic.get("_meta"), dict) else None,
        "items": items,
    }


def find_mismatches(data: dict) -> dict:
    A, B, C, D, E, F, G = [], [], [], [], [], [], []
    for it in data["items"]:
        vd, n, tot = it["ledger_verdict"], it["ledger_n"], it["ledger_total_pnl"]
        cash = _sign(tot)
        cash_txt = {"+": "现金同向", "-": "现金反向", "0": "现金≈0", "?": "无现金口径"}[cash]
        if it["sigma_hist"]:
            C.append(f"{it['factor']}({it['sigma_hist']:+d})")
        if it["enabled"] and it["eff"] > 0 and vd == "negative" and (n or 0) >= MIN_LEDGER_N:
            A.append((it["eff"], f"{it['factor']}(eff{it['eff']:+d}, n={n}, WR={it['ledger_wr']:.1%}, "
                                 f"现金{tot:+.0f})"))
        if it["ic_adj"] and vd in ("negative", "positive") and \
                (it["ic_adj"] > 0) != (vd == "positive"):
            B.append((it["ic_adj"], f"{it['factor']}(ic_adj{it['ic_adj']:+d} vs verdict={vd}, {cash_txt} "
                                    f"${tot:+.0f})"))
        if it["eff"] > 0 and (n or 0) < MIN_LEDGER_N:
            D.append((it["eff"], f"{it['factor']}(eff{it['eff']:+d}, 台账n={n})"))
        if it["eff"] < 0 and vd != "negative" and (tot or 0) > 0:
            E.append((it["eff"], f"{it['factor']}(eff{it['eff']:+d}, verdict={vd}, ${tot:+.0f}, n={n})"))
        if (vd == "negative" and (tot or 0) > 0) or (vd == "positive" and (tot or 0) < 0):
            F.append((abs(tot or 0), f"{it['factor']}(verdict={vd} 但 ${tot:+.0f}, WR={it['ledger_wr']:.1%}, "
                                     f"PF={it['ledger_pf']}, n={n})"))
        # G: verdict=negative 却建立在"把非结果行(missed/already_held/void, pnl 为 NULL/0)计入分母"上
        wrd = it.get("ledger_wr_decided")
        if vd == "negative" and wrd is not None and wrd >= 0.5:
            G.append((it.get("ledger_nonoutcome_share") or 0,
                      f"{it['factor']}(现口径 WR={it['ledger_wr']:.1%}={it['ledger_wins']}/"
                      f"{it['ledger_n']}, 剔非结果行后 WR={wrd:.1%}={it['ledger_wins']}/"
                      f"{it['ledger_n_decided']}, 非结果行占"
                      f"{(it.get('ledger_nonoutcome_share') or 0):.0%})"))

    def _flat(pairs, reverse=True):
        return [t for _, t in sorted(pairs, key=lambda x: x[0], reverse=reverse)]

    return {"A_正权重负证据": _flat(A), "B_IC通道反号": _flat(B),
            "C_Σhist双计存量": sorted(C), "D_小样本高权重": _flat(D),
            "E_罚错": _flat(E, reverse=False), "F_尺子冲突": _flat(F), "G_台账分母口径": _flat(G)}


def _render(data: dict, mm: dict, changed: list, first_run: bool) -> str:
    L = [f"🧾 **因子权重对账** ({data['generated_at'][:10]})",
         "",
         "**口径 (代码实测)**: 触发因子的实际贡献 `eff = base(yaml) + ic_adj(ic文件) + Σhist(调整台账)`；"
         f"台账 = `{data['ledger_db']}` 的 `playbook_factor_stats(single)`，更新于 "
         f"{(data['ledger_updated_at'] or '?')[:16]}Z；IC 快照 {(data['ic_snapshot'] or '?')[:16]}。",
         ""]
    L.append("| 因子 | en | base | ic_adj | Σhist | **eff** | 台账n | WR | 剔零WR | 金额$ | verdict |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for it in sorted(data["items"], key=lambda x: (-x["eff"], x["factor"])):
        wr = f"{it['ledger_wr']:.0%}" if it["ledger_wr"] is not None else "—"
        wrd = f"{it['ledger_wr_decided']:.0%}" if it.get("ledger_wr_decided") is not None else "—"
        tot = f"{it['ledger_total_pnl']:+.0f}" if it["ledger_total_pnl"] is not None else "—"
        gate = " ⚙" if it["resonance_gated"] else ""
        L.append(f"| {it['factor']}{gate} | {'✔' if it['enabled'] else '✘'} | {it['base']:+d} | "
                 f"{it['ic_adj']:+d} | {it['sigma_hist']:+d} | **{it['eff']:+d}** | "
                 f"{it['ledger_n'] if it['ledger_n'] is not None else '—'} | {wr} | {wrd} | {tot} | "
                 f"{it['ledger_verdict'] or '—'} |")
    L.append("")
    L.append("_⚙ = 有共振门控 (无 volume_expansion/insider_cluster_buy 共振时该项贡献回退为 0)。_")
    L.append("_WR = 台账胜率 = win/全部行; **剔零WR = win/(win+loss)** —— 台账分母含 missed/already_held/void "
             "等**非结果行** (pnl 为 NULL/0), 两列差异即分母口径量级。_")
    L.append("")
    titles = {
        "A_正权重负证据": "① 正权重买『负证据』(enabled, eff>0, verdict=negative, n≥30)",
        "B_IC通道反号": "② IC 通道与台账 verdict 反号 (IC 加权的方向与台账结论相反)",
        "C_Σhist双计存量": "③ Σhist 非空 (与 base 是否重复计数无法唯一分解 — 见 DEB 局限)",
        "D_小样本高权重": "④ 正权重压在样本不足的因子上 (台账 n<30)",
        "E_罚错": "⑤ 罚错了 (eff<0 但台账非 negative 且现金为正)",
        "F_尺子冲突": "⑥ 尺子冲突 (verdict 胜率口径与现金口径反向)",
        "G_台账分母口径": "⑦ 台账分母口径: verdict=negative 但剔掉非结果行后胜率≥50%",
    }
    for k, title in titles.items():
        v = mm[k]
        L.append(f"**{title} — {len(v)} 项**")
        L.append("  " + ("；".join(v) if v else "无 ✅"))
        L.append("")
    L.append("**处置**: 改权重/改门属策略变更 ⇒ 本表只出清单，处置待裁 "
             "(清单与建议见 `docs/debates/DEB-20260921-factor-weight-verdict-reconciliation.md`)。")
    if first_run:
        L.append("_（首次运行 ⇒ 输出全量；之后只在对账面变化时出现，无变化即静默。）_")
    else:
        L.append(f"_（本次变化 {len(changed)} 项: {', '.join(changed[:10])}）_")
    return "\n".join(L)


def build_report(force: bool = False, dry: bool = False) -> str:
    data = collect()
    mm = find_mismatches(data)
    sig = {it["factor"]: "|".join([str(it["ledger_verdict"]), _n_band(it["ledger_n"]),
                                   _sign(it["eff"]), f"ic{it['ic_adj']:+d}",
                                   f"m{_sign(it['ledger_total_pnl'])}",
                                   "wrd" + ("hi" if (it.get("ledger_wr_decided") or 0) >= 0.5 else "lo")])
           for it in data["items"]}
    state = {}
    if STATE.exists() and not force:
        try:
            state = json.loads(STATE.read_text(encoding="utf-8")).get("signature", {})
        except Exception:                                              # noqa: BLE001
            state = {}
    changed = sorted(k for k, v in sig.items() if state.get(k) != v)
    first_run = not state
    if not force and not first_run and not changed:
        return ""
    out = _render(data, mm, changed, first_run)
    if not dry:
        data["mismatches"] = mm
        data["signature"] = sig
        tmp = PRODUCT.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, PRODUCT)
        st_tmp = STATE.with_suffix(".tmp")
        st_tmp.write_text(json.dumps({"signature": sig, "updated_at": data["generated_at"]},
                                     ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(st_tmp, STATE)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印, 不写 state/产物")
    ap.add_argument("--force", action="store_true", help="忽略 state, 强制输出全量")
    a = ap.parse_args()
    try:
        out = build_report(force=a.force, dry=a.dry)
    except Exception as e:                                             # noqa: BLE001
        print(f"[factor_weight_reconcile] 探测失败 ({type(e).__name__}: {e}) "
              f"⇒ 对账未完成, 不得读成『无错配』")
        return 2
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())