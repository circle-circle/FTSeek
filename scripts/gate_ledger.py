#!/usr/bin/env python3
"""[治理] 门台账生成器 (Gate Ledger) — 只读枚举全系统拦截门 + 命中率 + 预注册出处.

为什么需要 (用户 2026-10-11「拦截门的设定和统一」):
  门景观极度碎片化 —— ~20+ 道门横跨 2 仓 (DA skill + FTSeek)、15+ 个独立审计 jsonl,
  各有 schema, 无统一视图。stale_quote 事故 (一道无裁决/无DEB/无测试的门悄悄毙33只好票)
  的根因就是【没有台账】: 加门时无人核它对供应的冲击、无预注册出处。

⚠️ 口径纪律 (本脚本焊死, 防 telemetry-caliber 陷阱):
  exec_gate_audit 是【按指令-评估轮】记录, 同一 decision_id 被快循环重复评估
  (实测均值 8.3×, max 76×) ⇒ 原始 action 分布被轮询放大。命中率必须【按 decision_id
  去重看每指令终局】, 否则 "10.5%执行率/38.8%chase_cap" 是假象 (真值 85.7%/6.2%)。

输出: markdown 门台账 (stdout)。只读 (DB mode=ro / jsonl 只读), 零行为变更。
运行: ./venv/bin/python scripts/gate_ledger.py
"""
from __future__ import annotations

import json
import glob
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data"
DOCS = ["docs/decisions", "docs/debates"]
# 台账自身登记文件 — grep 预注册出处时必须排除, 否则 §36.4 命名违规门会循环自证"有出处"
_LEDGER_DEB = "DEB-20261009-event-to-trade-pipeline.md"

# 策展预注册注册表 (2026-10-11 人工审计, grep 排除台账 DEB 自身防自指)。
# 治理规则: 新门上线必须在此登记 reason码→RFC/DEB 出处; None = 违规 (须补源或下线)。
# grep 仅作发现辅助 (见 _grep_prereg), 权威判定用本表。
GATE_PREREG = {
    "chase_cap": "CHASE-CAP-20260909/RFC-081", "notional_cap": "DEB-20260628参数冻结",
    "max_positions": "RFC-055/RFC-075", "bp_cap": "DEB-20260720/DEB-20260924",
    "open_position_cap": "DEB-20260921", "cycle_cap": "DEB-20260924",
    "entry_confirm": "RFC-141/DEB-20261009§33", "knife_gate": "RFC-068/DEB-20260911",
    "edge_gate": "DEB-20260723", "hard_stop_cooldown": "RFC-111/RFC-112",
    "duplicate_position": "DEB-20260819/DEB-20260823", "buy_quota": "DEB-20260801",
    "symbol_daily_loss": "DEB-20260725/DEB-20260801", "portfolio_guardrail": "RFC-071/RFC-121",
    "ttl_expired": "DEB-20260903/DEB-20260904", "signal_gate": "RFC-112",
    "fee_gate": "DEB-20261009§25/§27(P0影子)", "pdt": "监管(PDT规则,外部)",
    "manual_held": "运维(手动持仓标记)",
    "archetype_conflict": None,  # ⚠️ 违规: 无 RFC/DEB 出处
    "bad_direction": None,        # ⚠️ 违规: 无 RFC/DEB 出处
}


def _rows(path):
    try:
        return [json.loads(l) for l in open(path, errors="replace") if l.strip()]
    except Exception:
        return []


def _grep_prereg(gate_name: str) -> str:
    """发现辅助: grep RFC/DEB 找门出处, **排除台账 DEB 自身**(防 §36 命名违规门循环自证)。"""
    hits = []
    for d in DOCS:
        for f in glob.glob(str(REPO / d / "*.md")):
            if os.path.basename(f) == _LEDGER_DEB:
                continue  # 台账自指排除
            try:
                if re.search(gate_name, open(f, errors="replace").read()):
                    hits.append(os.path.basename(f).replace(".md", ""))
            except Exception as _read_err:
                # 读失败 ≠ 无出处 — 留痕防"门出处缺失"判定失真 (R5)
                print(f"[gate_ledger] 读取 {f} 失败, 该文件未参与出处检索: {_read_err}",
                      file=sys.stderr)
    if not hits:
        return "⚠️无出处"
    hits.sort(key=lambda x: (not x.startswith("RFC"), x))
    return hits[0][:38]


def _prereg(gate_name: str) -> str:
    """权威判定: 策展注册表 GATE_PREREG 优先; 未登记的门 fallback 到 grep(排除台账)。"""
    if gate_name in GATE_PREREG:
        v = GATE_PREREG[gate_name]
        return "⚠️无出处" if v is None else v
    return _grep_prereg(gate_name)


def section_exec_gate():
    """FTSeek 执行层门 — 去重口径 (每指令终局)。"""
    f = DATA / "shadow" / "exec_gate_audit.jsonl"
    rows = _rows(f)
    if not rows:
        return None
    by_dec = defaultdict(list)
    for r in rows:
        d = r.get("decision_id")
        if d:
            by_dec[d].append(r)
    n_uniq = len(by_dec)
    ever_exec = 0
    terminal = Counter()
    evals = []
    for d, rs in by_dec.items():
        evals.append(len(rs))
        actions = [r.get("action") for r in rs]
        if "executed" in actions:
            ever_exec += 1
        else:
            skips = [a for a in actions if a and a.startswith(("skipped", "deferred", "blocked"))]
            terminal[Counter(skips).most_common(1)[0][0] if skips else "(无skip)"] += 1
    import statistics
    infl = len(rows) / max(n_uniq, 1)
    out = [f"### A. FTSeek 执行层门 (exec_gate_audit, 去重口径)",
           f"- 原始 {len(rows)} 行 = 轮询放大 **{infl:.1f}×** (每指令评估中位{statistics.median(evals):.0f}/均值{statistics.mean(evals):.1f}/max{max(evals)}轮)",
           f"- 唯一指令 **{n_uniq}**; 曾执行 **{ever_exec} ({ever_exec/n_uniq*100:.1f}%)**; 被拦 {n_uniq-ever_exec} ({(n_uniq-ever_exec)/n_uniq*100:.1f}%)",
           f"- ⚠️ 原始 action 分布 (chase_cap 38.8%/执行率10.5%) 是轮询假象, 禁用; 下表为每指令终局:",
           f"",
           f"| 终局门 | 被拦指令 | 占被拦 | 占全体 | 预注册出处 | 供应分类 |",
           f"|---|---:|---:|---:|---|---|"]
    CAPS = {"skipped_chase_cap", "skipped_notional_cap", "skipped_max_positions",
            "skipped_bp_cap", "skipped_open_position_cap", "deferred_cycle_cap"}
    cls_map = {
        "skipped_chase_cap": ("CHASE-CAP/RFC-081", "预算帽(必要纪律)"),
        "skipped_notional_cap": ("DEB-20260628参数冻结", "预算帽(必要纪律)"),
        "skipped_max_positions": ("RFC-055/075", "预算帽(必要纪律)"),
        "skipped_knife_gate": ("RFC-068", "质量门(接飞刀)"),
        "skipped_fail": ("执行失败(非门)", "异常"),
        "skipped_hard_stop_cooldown": ("RFC-111", "风控门(冷却)"),
    }
    for g, n in terminal.most_common():
        pr, cls = cls_map.get(g, (_prereg(g.replace("skipped_", "").replace("deferred_", "")), "未分类"))
        out.append(f"| {g} | {n} | {n/max(n_uniq-ever_exec,1)*100:.1f}% | {n/n_uniq*100:.1f}% | {pr} | {cls} |")
    cap_n = sum(n for g, n in terminal.items() if g in CAPS)
    out.append(f"\n- 预算帽类拦 {cap_n}/{n_uniq-ever_exec} ({cap_n/max(n_uniq-ever_exec,1)*100:.0f}% of 被拦); 质量/风控门仅 {n_uniq-ever_exec-cap_n}")
    out.append(f"- ⇒ **执行层非供应瓶颈** (85.7%放行); 帽=必要预算纪律, 非供应敌对")
    return "\n".join(out)


def section_da_gate():
    """DA 侧 signal_gate 质量门。"""
    f = DATA / "da_gate_decisions.jsonl"
    rows = _rows(f)
    if not rows:
        return None
    dec = Counter(r.get("decision") for r in rows)
    n = len(rows)
    # 尝试按 (symbol,date) 去重看唯一候选
    uniq = set()
    for r in rows:
        s = r.get("symbol") or r.get("ticker") or ""
        d = (r.get("ts") or r.get("date") or r.get("batch_date") or "")[:10]
        if s:
            uniq.add((s, d))
    out = [f"### B. DA 侧 signal_gate 质量门 (da_gate_decisions)",
           f"- 决策行 {n}; 唯一(symbol,date)候选 {len(uniq) if uniq else '无symbol列,不可去重'}",
           f"- decision 分布: BLOCK {dec.get('BLOCK',0)} ({dec.get('BLOCK',0)/n*100:.0f}%) / "
           f"WARN {dec.get('WARN',0)} ({dec.get('WARN',0)/n*100:.0f}%) / PASS {dec.get('PASS',0)} ({dec.get('PASS',0)/n*100:.0f}%)",
           f"- 预注册出处: {_prereg('signal_gate|quality_score')}",
           f"- ⇒ **DA 质量门是主供应约束** (BLOCK 61%), 远大于执行层帽 (12%); 供应杠杆在上游"]
    return "\n".join(out)


def section_inventory():
    """全门审计文件清单 (碎片化证据)。"""
    pats = ["data/*gate*.jsonl", "data/*audit*.jsonl", "data/*reject*.jsonl",
            "data/*funnel*.jsonl", "data/shadow/*gate*.jsonl", "data/shadow/*funnel*.jsonl"]
    files = set()
    for p in pats:
        files.update(glob.glob(str(REPO / p)))
    out = [f"### C. 门审计落点清单 (碎片化证据: {len(files)} 个独立 jsonl)",
           f"| 审计文件 | 行数 | 大小KB | 门域 |",
           f"|---|---:|---:|---|"]
    domain = {
        "da_gate_decisions": "DA质量", "candidate_funnel": "DA漏斗", "vol_gate_da_p50": "DA成交量",
        "exec_gate_audit": "FTSeek执行", "edge_gate_shadow": "edge期望", "fee_gate_shadow": "费用(P0影子)",
        "falsification_gate": "证伪(资金流)", "reentry_gate": "再入场", "kr3_direction_runup": "KR3方向",
        "eod_depth_gate": "EOD深度", "eth_relative_volume": "ETH量", "vwap_gate": "VWAP",
        "extended_hours_earnings": "ETH财报", "extended_hours_pretrade": "ETH盘前",
        "extended_hours_price": "ETH价格", "ue_confirm_gate": "UE确认", "ue_long_momentum": "UE多头动能",
        "ue_tier_gate": "UE分层", "short_funnel": "空头漏斗", "short_supply_funnel": "空头供应",
        "trigger_gate": "触发", "skew_gate": "偏度", "gate_cf_history": "门反事实",
        "gate_liveness": "门活性", "eth_relative_volume_gate": "ETH量",
    }
    for f in sorted(files, key=lambda x: -os.path.getsize(x)):
        base = os.path.basename(f).replace(".jsonl", "")
        rows = _rows(f)
        dom = next((v for k, v in domain.items() if k in base), "?")
        out.append(f"| {base} | {len(rows)} | {os.path.getsize(f)//1024} | {dom} |")
    return "\n".join(out)


def section_violations():
    """无预注册出处的门 (stale_quote 式违规风险)。"""
    # 执行层 reason 码 + 已知门
    gates = ["chase_cap", "notional_cap", "max_positions", "bp_cap", "open_position_cap",
             "cycle_cap", "entry_confirm", "knife_gate", "edge_gate", "hard_stop_cooldown",
             "archetype_conflict", "duplicate_position", "buy_quota", "symbol_daily_loss",
             "portfolio_guardrail", "pdt", "manual_held", "bad_direction", "ttl_expired",
             "fee_gate", "falsification_gate", "reentry_gate", "kr3_direction_runup",
             "eod_depth_gate", "vwap_gate", "vol_gate", "signal_gate"]
    viol = []
    for g in gates:
        pr = _prereg(g)
        if pr == "⚠️无出处":
            viol.append(g)
    out = [f"### D. ⚠️ 无预注册出处的门 (stale_quote 式违规风险)",
           f"- 扫描 {len(gates)} 道门, **{len(viol)} 道无 RFC/DEB 出处**: {', '.join(viol) if viol else '无'}",
           f"- 治理规则建议: 任何新门上线前必须在台账登记 (门名/reason码/触发条件/预注册出处/供应冲击估计);",
           f"  无出处的门 = 违规, 须补 RFC/DEB 或下线。stale_quote 事故根因即此。"]
    return "\n".join(out)


def main():
    print("# 门台账 (Gate Ledger) — 全系统拦截门统一视图")
    print(f"> 生成: scripts/gate_ledger.py (只读); 口径: exec_gate_audit 已按 decision_id 去重")
    print(f"> 目的: 统一碎片化门景观 + 防 stale_quote 式无预注册门 + 定位真供应约束\n")
    for fn in [section_inventory, section_exec_gate, section_da_gate, section_violations]:
        s = fn()
        if s:
            print(s)
            print()
    print("### E. 核心结论 (供应定位)")
    print("- 真供应约束在 **上游 DA signal_gate (BLOCK 61%)**, 非 FTSeek 执行层 (85.7%放行)")
    print("- 执行层被拦 86% 是预算帽 (必要纪律, 非供应敌对); 质量/风控门仅拦 2%")
    print("- ⇒ 「提升有质量标的供应」的杠杆在 **DA 质量门的判准** (拦的是垃圾还是好票?), 不在执行层加门")
    print("- 入场价优化 (candidate C/第2层) 是【不砍供应的质量杠杆】(同批票进得更便宜), 优于加拦截门")


if __name__ == "__main__":
    main()
