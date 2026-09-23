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
# 现役退出栈的"死"语义三档 (只印不判; 档位选择本身就是判据问题, 见 DEB-20260811 复议)
VOCAB_BUNDLES = (
    ("现役死信号", {"DEAD_SIGNAL_EXIT"}),
    ("死信号+耐心出场", {"DEAD_SIGNAL_EXIT", "PATIENT_EXIT_STOP",
                        "PATIENT_EXIT_EOD", "PATIENT_EXIT_FILL"}),
    ("上述+收口/跳空", {"DEAD_SIGNAL_EXIT", "PATIENT_EXIT_STOP", "PATIENT_EXIT_EOD",
                       "PATIENT_EXIT_FILL", "PRE_CLOSE_EXIT", "OVERNIGHT_GAP_STOP"}),
)


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
                print(f"  基率(同期已平仓 {len(_base)} 笔, 入场≥{_since}):")
                for _name, _b in VOCAB_BUNDLES:
                    _hr = 100.0 * sum(1 for r in hit if r.get("final_exit_reason") in _b) / n_hit
                    _br = 100.0 * sum(1 for x in _base if x in _b) / len(_base)
                    _lift = (_hr / _br) if _br else 0.0
                    print(f"    [只印不判] {_name}: 命中组 {_hr:.1f}% vs 基率 {_br:.1f}% ⇒ lift {_lift:.2f}×")
                _dbr = 100.0 * sum(1 for x in _base if x in DEAD) / len(_base)
                print(f"    [对照] 预注册词表基率 {_dbr:.1f}% (预注册当时 30%)")
    if settled_ct == 0 and not done:
        print("(全部未平仓, 待交易结束后再跑)")


if __name__ == "__main__":
    main()
