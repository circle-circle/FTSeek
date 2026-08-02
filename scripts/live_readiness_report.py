#!/usr/bin/env python3
"""实盘就绪门径汇总 (RFC-062 在建清单⑧, 2026-08-02) — 两门读数一页.

为什么: 门 1 (8-29 裁决) 与门 2 (试点判据) 的读数散落在 4 个文件/2 张表,
裁决时不该临时拼数。本脚本把每条判据的当前读数+冻结阈值+达标状态汇总成一页,
只读快照 — 裁决仍以 8-29 当日实读为准 (RFC-062 冻结纪律)。

数据源: exit_scorecard_latest.json (G1-1/G1-4) · direction_scorecard_latest.json
  (G1-2/G1-3) · trade_records.metadata_json.execution.slippage_bps (G2-1 参考)
  · playbook_instructions ssr_shadow 标记 (G2-2 参考) · audit/reconcile_live.jsonl (G2-3)
输出: 控制台表 + data/live_readiness_latest.json。纯只读 (不连券商, 无 SDK 悬挂)。

用法: venv/bin/python scripts/live_readiness_report.py
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "ftseek_us.db"
EXIT_SC = ROOT / "data" / "exit_scorecard_latest.json"
DIR_SC = ROOT / "data" / "direction_scorecard_latest.json"
RECONCILE = ROOT / "data" / "audit" / "reconcile_live.jsonl"
OUT = ROOT / "data" / "live_readiness_latest.json"


def _cfg():
    try:
        from app.core.config_container import get_config
        g = get_config()
        return {
            "haircut_bps": float(g.get_dotted("live_readiness.slippage_haircut_bps", 15)),
            "g2_slip_bps": float(g.get_dotted("live_readiness.gate2.max_median_slippage_bps", 15)),
            "g2_reject": float(g.get_dotted("live_readiness.gate2.max_reject_rate", 0.02)),
            "g2_zero_days": int(g.get_dotted("live_readiness.gate2.reconcile_zero_drift_days", 5)),
        }
    except Exception:
        return {"haircut_bps": 15.0, "g2_slip_bps": 15.0, "g2_reject": 0.02, "g2_zero_days": 5}


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _g1(cfg: dict) -> list:
    """门 1 四判据 — 返回 [{id, name, threshold, reading, pass, note}]"""
    rows = []
    ex, dr = _load_json(EXIT_SC), _load_json(DIR_SC)
    payoff = ex.get("payoff", {})
    he = dr.get("sections", {}).get("headline_exec", {})

    # G1-1 净期望−滑点折算 ≥ $0/笔 (exit_scorecard 净口径)
    adj = payoff.get("expectancy_slip_adj")
    rows.append({
        "id": "G1-1", "name": "净期望−滑点折算 ≥ $0/笔",
        "threshold": f"haircut={cfg['haircut_bps']:g}bps 冻结",
        "reading": f"${adj}" if adj is not None else "no_data",
        "pass": (adj is not None and adj >= 0),
        "note": f"毛期望 ${payoff.get('expectancy_per_trade')} · avg名义 ${payoff.get('avg_notional')}",
    })

    # G1-2 BUY/SHORT 已执行分桶 n≥30
    nb = (he.get("已执行·BUY") or {}).get("n", 0)
    ns = (he.get("已执行·SELL_SHORT") or {}).get("n", 0)
    rows.append({
        "id": "G1-2", "name": "BUY/SHORT 已执行分桶 n≥30",
        "threshold": "n≥30 双桶",
        "reading": f"BUY n={nb} · SHORT n={ns}",
        "pass": (nb >= 30 and ns >= 30),
        "note": "SHORT 边缘桶, 裁决日以实读为准",
    })

    # G1-3 尺子健康: UE critique 倒挂需 n≥30 复读
    ue = dr.get("sections", {}).get("ue_critique_candidates", {})
    down, keep = ue.get("DOWN/DROP", {}), ue.get("KEEP", {})
    nd = down.get("n", 0)
    inverted = (down.get("hit_t1") or 0) > (keep.get("hit_t1") or 0)
    rows.append({
        "id": "G1-3", "name": "尺子健康: critique 无未解释倒挂",
        "threshold": "DOWN/DROP n≥30 复读",
        "reading": f"n={nd} · DOWN命中{down.get('hit_t1')}% vs KEEP命中{keep.get('hit_t1')}%",
        "pass": (nd >= 30 and not inverted),
        "note": "倒挂仍在案 (LLM 差评组反而命中高) — 需 n≥30 复证" if inverted else "未见倒挂",
    })

    # G1-4 退出 R:R ≥ 1.29 (或有已登记改善轨迹)
    rr, brr = payoff.get("rr"), payoff.get("breakeven_rr")
    rows.append({
        "id": "G1-4", "name": "退出 R:R ≥ 1.29 或改善轨迹",
        "threshold": "R:R≥1.29 冻结 (当前盈亏平衡 %s)" % brr,
        "reading": f"R:R={rr} (缺口 {payoff.get('rr_gap')})",
        "pass": (rr is not None and rr >= 1.29),
        "note": "改善轨迹由周五迭代登记, 本表不自动判",
    })
    return rows


def _median(vals: list):
    vals = sorted(vals)
    n = len(vals)
    if not n:
        return None
    return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2, 2)


def _g2(cfg: dict) -> list:
    """门 2 数据源就绪度 (试点前参考读数, 非判分)。"""
    rows = []
    conn = sqlite3.connect(str(DB))

    # G2-1 SIM 实测滑点 (参考; 判据针对试点实盘)
    slip = {"DA": [], "UE": [], "other": []}
    for s, did in conn.execute(
            "SELECT json_extract(metadata_json,'$.execution.slippage_bps'), decision_id "
            "FROM trade_records WHERE json_extract(metadata_json,'$.execution.slippage_bps') IS NOT NULL"):
        bucket = "DA" if str(did).startswith("DA-") else ("UE" if str(did).startswith("universe_exp") else "other")
        slip[bucket].append(float(s))
    allv = slip["DA"] + slip["UE"] + slip["other"]
    med, med_da, med_ue = _median(allv), _median(slip["DA"]), _median(slip["UE"])
    rows.append({
        "id": "G2-1", "name": "实测滑点中位 ≤ 假设",
        "threshold": f"≤{cfg['g2_slip_bps']:g}bps (试点实盘判)",
        "reading": f"SIM 中位 {med}bps · DA {med_da} · UE {med_ue} (n={len(allv)})",
        "pass": None,
        "note": "SIM 撮合偏乐观, 仅供参考; 判分用试点实测",
    })

    # G2-2 SSR 影子上界 (参考; 判据针对试点拒单率)
    marked = trig = 0
    for (rij,) in conn.execute(
            "SELECT raw_instruction_json FROM playbook_instructions "
            "WHERE direction='SELL_SHORT' AND raw_instruction_json LIKE '%ssr_shadow%'"):
        try:
            s = json.loads(rij).get("ssr_shadow", {})
            marked += 1
            trig += 1 if s.get("would_trigger") else 0
        except Exception:
            pass
    rate = round(trig / marked * 100, 1) if marked else None
    rows.append({
        "id": "G2-2", "name": "拒单率 < 2% 且合规拒单=0",
        "threshold": f"<{cfg['g2_reject'] * 100:g}% (试点实盘判)",
        "reading": f"SSR 影子上界 {trig}/{marked} = {rate}% (日低触-10%口径)",
        "pass": None,
        "note": "保守上界非指令时刻状态; 真实拒单率试点实测",
    })
    conn.close()

    # G2-3 对账连续零漂移 (SIM 期即跑, 真判据)
    days_ok, last_status, streak = set(), "no_data", 0
    day_status = {}
    try:
        for line in RECONCILE.read_text(encoding="utf-8").strip().splitlines():
            rec = json.loads(line)
            day = rec.get("ts", "")[:10]
            st = rec.get("status", "")
            day_status[day] = st if day_status.get(day) != "drift" else "drift"
            last_status = st
    except Exception:
        pass
    for day in sorted(day_status, reverse=True):
        if day_status[day] == "ok":
            streak += 1
        else:
            break
    days_ok = sum(1 for s in day_status.values() if s == "ok")
    rows.append({
        "id": "G2-3", "name": f"对账连续 {cfg['g2_zero_days']} 日零漂移",
        "threshold": f"{cfg['g2_zero_days']} 日 streak",
        "reading": f"连续 ok {streak} 日 · 累计 ok {days_ok}/{len(day_status)} 日 · 最新 {last_status}",
        "pass": (streak >= cfg["g2_zero_days"]),
        "note": "SIM 期已跑通 (外部持仓白名单口径), 试点期同一判据直接复用",
    })

    rows.append({
        "id": "G2-5", "name": "实盘净期望 ≥ SIM 同期 − 不确定带",
        "threshold": "相对口径 (试点判)",
        "reading": "N/A — 试点未开始",
        "pass": None,
        "note": "SIM 并行对照组设计见 RFC-062 门 2",
    })
    return rows


def main() -> int:
    cfg = _cfg()
    g1, g2 = _g1(cfg), _g2(cfg)
    n_pass = sum(1 for r in g1 if r["pass"])

    print(f"=== 实盘就绪门径汇总 · {datetime.now():%Y-%m-%d %H:%M} (快照, 非裁决) ===")
    print(f"门 1 (2026-08-29 裁决) — 当前 {n_pass}/4 达标")
    for r in g1:
        mark = "✅" if r["pass"] else "❌"
        print(f"  {mark} {r['id']} {r['name']}")
        print(f"     阈值 {r['threshold']} | 读数 {r['reading']}")
        print(f"     {r['note']}")
    print("门 2 数据源就绪度 (试点判据, 当前为参考读数)")
    for r in g2:
        mark = "—" if r["pass"] is None else ("✅" if r["pass"] else "❌")
        print(f"  {mark} {r['id']} {r['name']}")
        print(f"     阈值 {r['threshold']} | 读数 {r['reading']}")
        print(f"     {r['note']}")

    OUT.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "caliber": "快照非裁决 — 门1以 2026-08-29 实读为准 (RFC-062)",
        "gate1": {"pass_n": n_pass, "total": 4, "items": g1},
        "gate2": {"items": g2},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
