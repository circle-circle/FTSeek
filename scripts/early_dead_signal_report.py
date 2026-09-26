#!/usr/bin/env python3
"""early_dead_signal_shadow 结算报告 (DEB-20260811-dead-order-early-detection).

对 data/early_dead_signal_shadow.jsonl 中未结算行, 配对 trade_records 最终结局:
  - 命中定义: 30min 检查点 MFE<0.2% (主规则) / 15min 检查点 MFE<0.15% (辅助)
  - 死单定义: exit_reason ∈ {TIME_EXIT, MFE_STALL_EXIT}   ← 预注册词表, 判决口径不动
  - 差集对照: 命中行中 DEAD_SIGNAL_EXIT(60min) 未触发者 = "提前量独立价值"样本
  - 现役词表读法 (2026-09-23 加, **只印不判**): 预注册词表在现役退出栈下基率仅 ~1%
    ⇒ 只印预注册口径会让"死单率"看着恒 ≈0 (实为词语表漂移, 非影子无效)。
    逐条并列 现役死信号 / +耐心出场 / +收口跳空 三档的命中组率 vs 同期基率 + lift。
    不写回任何字段 (is_dead 仍按预注册词表落库), 判据改动须另立 DEB。
裁决预注册 (攒样≥30 命中后评估):
  ①双窗口死单率均≥55% ②命中组实际净PnL<0 ③差集结局为负
用法: PYTHONPATH=. venv/bin/python scripts/early_dead_signal_report.py
"""
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "data" / "early_dead_signal_shadow.jsonl"
DB = ROOT / "data" / "ftseek_us.db"
DEAD = {"TIME_EXIT", "MFE_STALL_EXIT"}
THRESH = {15: 0.15, 30: 0.2}
# 2026-09-26 RFC-109④-B: 判据①绝对线与其预注册**相对意图** (报告 9/23 注释载明
# 「55% 线 ≈ 预注册当时基率 30% 的 1.8×」)。词表漂移后绝对线不可达 ⇒ 并列印相对线,
# 让裁决人看见判据真正在问「命中组是否比基线显著更容易死」。判决口径仍是绝对线, 不动。
PREREG_LINE = 0.55
PREREG_BASE = 0.30
REL_INTENT = PREREG_LINE / PREREG_BASE    # = 1.833×
# 现役退出栈的"死"语义三档 (只印不判; 档位选择本身就是判据问题, 见 DEB-20260811 复议)
VOCAB_BUNDLES = (
    ("现役死信号", {"DEAD_SIGNAL_EXIT"}),
    ("死信号+耐心出场", {"DEAD_SIGNAL_EXIT", "PATIENT_EXIT_STOP",
                        "PATIENT_EXIT_EOD", "PATIENT_EXIT_FILL"}),
    ("上述+收口/跳空", {"DEAD_SIGNAL_EXIT", "PATIENT_EXIT_STOP", "PATIENT_EXIT_EOD",
                       "PATIENT_EXIT_FILL", "PRE_CLOSE_EXIT", "OVERNIGHT_GAP_STOP"}),
)


def fisher_two_sided(k1, n1, k0, n0):
    """双侧 Fisher exact (纯 stdlib, 不引 scipy) — 命中组 vs 同期基线的死单率差是否显著.

    2026-09-26 RFC-109④-B 加: 原报告只给 lift **点估计**, 无 p 值 ⇒ 裁决人无法判断
    3.71× 是真效应还是小样本噪声 (n_hit 仅 86)。判据①的绝对线 (≥55%) 因词表漂移不可达,
    故必须给「相对意图 + 显著性」两面才够裁决。**只印不判**: 判决口径 (绝对线) 不动。
    """
    from math import comb

    def hyper(a, b, c, d):
        return comb(a + b, a) * comb(c + d, c) / comb(a + b + c + d, a + c)

    row1, row2, col1 = k1 + (n1 - k1), k0 + (n0 - k0), k1 + k0
    p_obs = hyper(k1, n1 - k1, k0, n0 - k0)
    p_two = 0.0
    for x in range(max(0, col1 - row2), min(row1, col1) + 1):
        px = hyper(x, row1 - x, col1 - x, row2 - col1 + x)
        if px <= p_obs * (1 + 1e-9):
            p_two += px
    return min(p_two, 1.0)


def counterfactual(hit, conn):
    """反事实: 「检查点即早退」vs「实际结局」的净额差 (2026-09-26 RFC-109④-B).

    判据①②③ 只回答「命中组是不是死单/亏钱」, 回答不了**接线该不该做** —— 早退本身
    要花钱 (砍掉反弹单)。影子行自带 entry_price + cur_pct (检查点当时盈亏%) ⇒ 按用户
    8/14 口径 (能历史重放就直接回测, 不默认前向攒样) 在已有数据上直接算。

    口径 (与生产同源, 不自造):
      notional = quantity × entry_price (trade_records 真值)
      net_cf   = notional × cur_pct/100 − fees_est   (费用按往返计, 不因早退而省)
      Δ        = net_cf − final_pnl_net              (>0 = 早退更好)
    按「检查点→最终」的符号变化分四类: 两类在**检查点时点不可区分** (cur_pct 都是小负),
    故四类 Δ 的净额才是门的真实期望, 不能只看「早退能救的那类」。

    ⚠️ 量纲坑 (本人实测踩过, 勿重蹈): cur_pct 是**百分点** (0.1=0.1%), 而
    trade_records.pnl_pct 是**小数** (0.01=1%) ⇒ 比符号前必须 ×100 换算, 否则
    阈值一设就全被短路, 会报「翻转 0 笔」的假绿。pnl_pct 是毛、pnl_net 是净。
    """
    tr = {r["id"]: r for r in conn.execute(
        "SELECT id, quantity, entry_price, fees_est, pnl_net, pnl_pct FROM trade_records")}
    # 同一笔可能同时有 15/30 检查点行 ⇒ 按 trade_id 去重 (同一批笔不重复计 Δ)
    seen = {}
    for r in hit:
        tid = r.get("trade_id")
        if tid is not None and tid not in seen:
            seen[tid] = r
    cats = {"盈→亏(早退该救)": [], "亏→盈(早退砍反弹)": [],
            "亏→亏(早退少亏)": [], "盈→盈(早退少赚)": []}
    for r in seen.values():
        t = tr.get(r.get("trade_id"))
        if not t or not t["quantity"] or not t["entry_price"]:
            continue
        notional = float(t["quantity"]) * float(t["entry_price"])
        net_cf = notional * float(r.get("cur_pct") or 0.0) / 100.0 - float(t["fees_est"] or 0.0)
        d = net_cf - float(t["pnl_net"] or 0.0)
        cf, fin = float(r.get("cur_pct") or 0.0), float(t["pnl_pct"] or 0.0) * 100.0  # 量纲统一
        if cf > 0 and fin <= 0:
            cats["盈→亏(早退该救)"].append(d)
        elif cf <= 0 and fin > 0:
            cats["亏→盈(早退砍反弹)"].append(d)
        elif cf <= 0:
            cats["亏→亏(早退少亏)"].append(d)
        else:
            cats["盈→盈(早退少赚)"].append(d)
    return len(seen), cats


def baseline_reasons(conn, since):
    """同期基线 (同口径: 已平仓、入场时间 ≥ 影子首日), 用于把绝对率折算成 lift。"""
    try:
        return [(r["exit_reason"] or "") for r in conn.execute(
            "SELECT exit_reason FROM trade_records WHERE exit_price>0 AND entry_time>=?",
            (since,)).fetchall()]
    except Exception:
        return []


def main():
    if not SHADOW.exists():
        print("shadow 文件尚不存在 (无交易越过检查点), 无结算。")
        return
    rows = [json.loads(l) for l in SHADOW.read_text().splitlines() if l.strip()]
    pending = [r for r in rows if not r.get("settled")]
    if not pending:
        print("无未结算行。")

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    settled_ct = 0
    for r in pending:
        # 按 (symbol, entry_time) 配对 trade_records
        tr = conn.execute("""
            SELECT id, pnl_net, exit_reason, metadata_json
            FROM trade_records
            WHERE symbol = ? AND entry_time LIKE ? || '%'
              AND exit_time IS NOT NULL AND exit_time != ''
            ORDER BY id DESC LIMIT 1
        """, (r["symbol"], str(r.get("entry_time", ""))[:19])).fetchone()
        if not tr:
            continue  # 交易未平仓, 留待下次
        md = {}
        try:
            md = json.loads(tr["metadata_json"] or "{}")
        except Exception:
            pass
        r["trade_id"] = tr["id"]
        r["final_pnl_net"] = tr["pnl_net"]
        r["final_exit_reason"] = tr["exit_reason"] or ""
        r["is_dead"] = (tr["exit_reason"] or "") in DEAD
        r["dead_signal_60m_fired"] = "dead_signal_exit" in md or tr["exit_reason"] == "DEAD_SIGNAL_EXIT"
        r["settled"] = True
        settled_ct += 1

    if settled_ct:
        # 2026-09-23 (挂 cron 前置加固): 写回前重读一次。本脚本是"读全量→改内存→
        #   整文件覆写", 而服务在结算期间可能又 append 了新的检查点行 (持仓跨
        #   15/30min 检查点即落行), 直接覆写会把它吞掉 (静默丢数据)。按
        #   (symbol, ts, checkpoint) 合并: 期间新增的行原样追加, 不结算不丢。
        _seen = {(r.get("symbol"), r.get("ts"), r.get("checkpoint")) for r in rows}
        try:
            _now = [json.loads(l) for l in SHADOW.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        except Exception:
            _now = []
        _extra = [r for r in _now
                  if (r.get("symbol"), r.get("ts"), r.get("checkpoint")) not in _seen]
        with open(SHADOW, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            for r in _extra:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if _extra:
            print(f"[note] 写回期间新增 {len(_extra)} 行 (未结算) 已原样保留")

    # 汇总
    done = [r for r in rows if r.get("settled")]
    print(f"\n=== early_dead_signal_shadow (已结算 {len(done)} 行, 本次新结算 {settled_ct}) ===")
    for cp in (15, 30):
        seg = [r for r in done if r.get("checkpoint") == cp]
        if not seg:
            continue
        thr = THRESH[cp]
        hit = [r for r in seg if r.get("mfe_pct", 99) < thr]
        n_hit = len(hit)
        dead_hit = sum(1 for r in hit if r["is_dead"])
        net_hit = sum(r["final_pnl_net"] or 0 for r in hit)
        diff_set = [r for r in hit if not r["dead_signal_60m_fired"]]
        net_diff = sum(r["final_pnl_net"] or 0 for r in diff_set)
        print(f"\n[{cp}min 检查点] 总行 {len(seg)} | 命中(MFE<{thr}%) {n_hit}")
        if n_hit:
            print(f"  死单率(预注册词表): {dead_hit}/{n_hit} = {100*dead_hit/n_hit:.0f}%  (裁决线 ≥55%; 词表基率见下行)")
            print(f"  命中组净PnL: {net_hit:+.1f}$  (裁决线 <0)")
            print(f"  差集(30min命中但60min DEAD_SIGNAL未触发): {len(diff_set)}笔 净{net_diff:+.1f}$")
            # 2026-09-23: 现役词表读法 (只印不判)。预注册词表 {TIME_EXIT,MFE_STALL_EXIT}
            #   在现役退出栈下基率≈1% (8/11 预注册当时为 30%) ⇒ 只印上一行会把
            #   "口径漂移"读成"影子无效/判据不可达"。绝对率同时给基率与 lift, 便于
            #   按预注册的**相对**意图 (55% 线 ≈ 当时基率 30% 的 1.8×) 复核。
            _since = min((r.get("entry_time") or "")[:10] for r in done) or "2026-08-11"
            _base = baseline_reasons(conn, _since)
            if _base:
                _n_base = len(_base)
                print(f"  基率(同期已平仓 {_n_base} 笔, 入场≥{_since}):")
                for _name, _b in VOCAB_BUNDLES:
                    _k1 = sum(1 for r in hit if r.get("final_exit_reason") in _b)
                    _k0 = sum(1 for x in _base if x in _b)
                    _hr = 100.0 * _k1 / n_hit
                    _br = 100.0 * _k0 / _n_base
                    _lift = (_hr / _br) if _br else 0.0
                    # 2026-09-26 RFC-109④-B: 补显著性 (原只给 lift 点估计, n_hit 仅 86
                    #   ⇒ 裁决人无法分辨真效应与噪声)。判据①的**相对意图** = 55%线 ÷
                    #   预注册当时基率30% = 1.83× (报告 2026-09-23 注释已载明), 词表漂移后
                    #   绝对线不可达 ⇒ 给「相对线 + p 值」两面才够裁决。仍只印不判。
                    _p = fisher_two_sided(_k1, n_hit, _k0, _n_base)
                    _sig = "***" if _p < 0.001 else ("**" if _p < 0.01 else
                           ("*" if _p < 0.05 else ("." if _p < 0.10 else "ns")))
                    print(f"    [只印不判] {_name}: 命中组 {_hr:.1f}% vs 基率 {_br:.1f}% ⇒ "
                          f"lift {_lift:.2f}× [{'✓' if _lift >= REL_INTENT else '✗'}相对线{REL_INTENT:.2f}×] "
                          f"p={_p:.2e} {_sig}")
                _dbr = 100.0 * sum(1 for x in _base if x in DEAD) / _n_base
                print(f"    [对照] 预注册词表基率 {_dbr:.1f}% (预注册当时 30%)")
            # 2026-09-26 RFC-109④-B: 反事实 — 判据①②③ 只答「命中组是不是死单」,
            #   答不了「接线该不该做」(早退要花钱: 砍掉反弹单)。历史可重放 ⇒ 直接回测
            #   (用户 8/14 口径: 不默认前向攒样)。只印不判, 判决口径不动。
            _n_uniq, _cats = counterfactual(hit, conn)
            _tot = sum(sum(v) for v in _cats.values())
            print(f"  [反事实·只印不判] 「检查点即早退」vs「实际结局」(去重 {_n_uniq} 笔):")
            for _cn, _cv in _cats.items():
                if not _cv:
                    continue
                _cv_sorted = sorted(_cv)
                print(f"    {_cn:16s} n={len(_cv):3d} ({100*len(_cv)/max(_n_uniq,1):3.0f}%) | "
                      f"合计Δ {sum(_cv):+8.0f}$ | 单笔Δ均 {sum(_cv)/len(_cv):+7.2f}$ | "
                      f"中位 {_cv_sorted[len(_cv_sorted)//2]:+7.2f}$")
            print(f"    {'净额Δ (门的真实期望)':16s} {_tot:+8.0f}$  "
                  f"[{'早退省钱' if _tot > 0 else '早退亏钱 ⇒ 接线无毛利价值'}]")
            print(f"    注: 四类在检查点时点**不可区分** (cur_pct 均为小负) ⇒ 只能按净额判, "
                  f"不能只挑「早退该救」那类接线")
    if settled_ct == 0 and not done:
        print("(全部未平仓, 待交易结束后再跑)")


if __name__ == "__main__":
    main()
