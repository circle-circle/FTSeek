#!/usr/bin/env python3
"""闸门拦截逐笔审计 (只读) — 回答两个问题: 哪些闸门在"贴边"拦人, vwap A/B 到底测没测出边际效应。

缘起 (DEB-20261109 §四 P4)
------------------------
11-09 DA 22 条指令只成交 8 条, 14 条被闸门挡掉。盘后复盘时自然会问:
  ① 哪些拦截是**贴边**的 (刚过线就拦), 哪些是**远离阈值**的?
     —— 贴边 ⇒ 阈值可能设得太紧; 远离 ⇒ 拦截有实质理由。
  ② vwap 门在 SIM 本来就 50/50 A/B 分流, 那它的边际效应**测出来了吗**?
     —— 关键在"双臂是否同权": 若 bypass 臂的票又被下游门二次拦掉, 两臂不可比。

本脚本只做**读数**, 不改任何拦截行为。

数据源 (全部只读)
------------------
· data/shadow/exec_gate_audit.jsonl   逐条闸门审计 (action / entry / notional / chase_cap_price ...)
· data/vwap_gate_ab.jsonl             vwap 门 A/B 台账 (arm / vwap_dev / vwap_slope)
· data/trigger_miss_telemetry.jsonl   触发门事件 (按 inst_id 去重)
· playbook_instructions (当日)        skip_reason 原文 + executed
· trade_records (当日)                实际成交与结果
· config/unified_config.yaml          阈值真值 (不用硬编码)

输出
----
A. 当日闸门拦截概览 (按 skip_reason 归类 + 参数)
B. **贴边排行**: 每条拦截离阈值多近 (比值 <1.15 ⇒ 贴边) —— 回答"该调哪个阈值"
C. **vwap A/B 可比性**: 四象限 (真 bypass / 假 bypass / gated 但成交 / gated 被拦) + 两臂实际结果
D. 与既有记分牌 (`data/gate_block_counterfactual_latest.json`) 的裁决对照 —— 避免重复造尺子

只读保证: DB `file:...?mode=ro`; 不写任何文件; 零网络; 不进 cron。

用法:
  venv/bin/python scripts/gate_interception_daily.py --date 2026-11-09
  venv/bin/python scripts/gate_interception_daily.py --date 2026-11-09 --json
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "ftseek_us.db"
YAML_PATH = ROOT / "config" / "unified_config.yaml"
EXEC_AUDIT = ROOT / "data" / "shadow" / "exec_gate_audit.jsonl"
VWAP_AB = ROOT / "data" / "vwap_gate_ab.jsonl"
SCOREBOARD = ROOT / "data" / "gate_block_counterfactual_latest.json"

MARGIN = 1.15   # 贴边判定: |实际| / 阈值 <= 1.15


def _thresholds() -> Dict[str, float]:
    """从 unified_config.yaml 读阈值真值 (读不到则用与默认一致的缺省)。"""
    out = {"vwap_tol": 0.003, "stale_ms": 30000.0, "slope_tol": 0.001}
    try:
        import yaml
        cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8")) or {}
        ep = ((cfg.get("playbook") or {}).get("execution_parameters") or {})
        for key, path in (("vwap_tol", "entry_vwap_gate_tolerance"),
                          ("stale_ms", "reprice_quote_age_max_ms"),
                          ("slope_tol", "falsification_gate_vwap_slope_tol")):
            v = ep.get(path)
            if v is None:
                v = (((cfg.get("playbook") or {}).get("risk_parameters") or {}).get(path))
            if v is not None:
                out[key] = float(v)
    except Exception as e:  # 配置读不到 ⇒ 用缺省, 并在输出里标注
        out["_fallback"] = str(e)
    return out


def _rows(sql: str, args: tuple = ()) -> List[Dict[str, Any]]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    return out


def _read_jsonl(path: Path, day: Optional[str] = None) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            j = json.loads(line)
        except ValueError as exc:
            print(f"[gate_interception] 跳过坏 JSON 行 path={path}: {exc}", file=sys.stderr)
            continue
        if day and str(j.get("date") or "")[:10] != day:
            continue
        out.append(j)
    return out


def _parse_skip(reason: Optional[str]) -> Dict[str, Any]:
    """把 skip_reason 解析成 {gate, params:{}, raw}。"""
    raw = (reason or "").strip()
    if not raw:
        return {"gate": "none", "params": {}, "raw": raw}
    head = raw.split("(")[0].strip()
    params: Dict[str, float] = {}
    for m in re.finditer(r"([a-zA-Z_]+)=(-?\d+(?:\.\d+)?)", raw):
        # 正则已限定合法 float 形态，无需静默 except。
        params[m.group(1)] = float(m.group(2))
    return {"gate": head, "params": params, "raw": raw}


def _proximity(gate: str, params: Dict[str, float], th: Dict[str, float]) -> Optional[float]:
    """实际量 / 阈值 = 1.0 表示恰在阈值上; >1 表示越线幅度。返回 None 表示算不出。"""
    if gate.startswith("vwap_gate_blocked"):
        dev = params.get("dev")
        tol = th["vwap_tol"] * 100.0  # skip_reason 里 dev 是百分数
        return abs(dev) / tol if dev is not None and tol else None
    if gate.startswith("stale_quote_gate"):
        age = params.get("age")
        return age / th["stale_ms"] if age else None
    if gate.startswith("falsification_slope_blocked"):
        slope = params.get("slope")
        return abs(slope) / th["slope_tol"] if slope is not None and th["slope_tol"] else None
    if gate.startswith("chase_cap_enforced"):
        rep, cap = params.get("reprice"), params.get("cap")
        return (rep / cap) if (rep and cap) else None
    return None


def build(day: str) -> Dict[str, Any]:
    th = _thresholds()
    instr = _rows("""SELECT symbol, direction, decision_id, skip_reason, executed, entry_price,
        market_regime FROM playbook_instructions
        WHERE date=? AND row_kind='instruction'""", (day,))
    trades = _rows("""SELECT symbol, direction, decision_id, pnl_net, entry_price, exit_price,
        strategy_id, exit_reason FROM trade_records WHERE trade_date=?""", (day,))
    by_did = {t["decision_id"]: t for t in trades if t.get("decision_id")}
    blocked = []
    for r in instr:
        if r["executed"]:
            continue
        p = _parse_skip(r["skip_reason"])
        prox = _proximity(p["gate"], p["params"], th)
        blocked.append({"symbol": r["symbol"], "direction": r["direction"], "gate": p["gate"],
                        "params": p["params"], "raw": p["raw"], "proximity": prox,
                        "executed": False, "pnl": None})
    # 被挡指令的反事实结果: 若当日有同标的成交 (多因 DA/EXT 两臂), 取 DA 口径
    for b in blocked:
        for t in trades:
            if t["symbol"] == b["symbol"] and t["strategy_id"] == "playbook_sim":
                b["pnl"] = t["pnl_net"]
                b["note"] = "同日同标的 DA 成交结果 (仅供参照, 非反事实)"
    audit = _read_jsonl(EXEC_AUDIT, day)
    ab = _read_jsonl(VWAP_AB, day)
    # vwap A/B 四象限
    skip_by_sym = {b["symbol"]: b for b in blocked}
    quadrants: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for j in ab:
        sym = j.get("symbol")
        arm = j.get("arm")
        b = skip_by_sym.get(sym)
        did = None
        for r in instr:
            if r["symbol"] == sym and r["executed"]:
                did = r["decision_id"]
        traded = did in by_did
        if arm == "bypass":
            q = "真bypass_成交" if traded else "假bypass_被下游门拦"
        else:
            q = "gated但成交" if traded else "gated被拦"
        quadrants[q].append({"symbol": sym, "direction": j.get("direction"),
                             "vwap_dev": j.get("vwap_dev"), "vwap_slope": j.get("vwap_slope"),
                             "blocked_by": b["gate"] if b else None,
                             "pnl": by_did[did]["pnl_net"] if traded else None})
    board = {}
    if SCOREBOARD.exists():
        try:
            b = json.loads(SCOREBOARD.read_text())
            gates = b.get("gates") or {}
            if isinstance(gates, dict):
                for reason, row in gates.items():
                    if isinstance(row, dict):
                        board[reason] = {
                            "n_inst": row.get("n_instructions"),
                            "verdict": row.get("verdict"),
                            "open_t1": (row.get("t1_open") or {}).get("mean"),
                            "net": row.get("t1_open_net_after_fee"),
                            "pos_rate": (row.get("t1_open") or {}).get("pos_rate"),
                        }
            elif isinstance(gates, list):
                for row in gates:
                    if isinstance(row, dict) and row.get("reason"):
                        board[row["reason"]] = row
        except ValueError:
            board = {}
    return {"date": day, "thresholds": th, "n_instructions": len(instr),
            "n_executed": sum(1 for r in instr if r["executed"]),
            "blocked": blocked, "audit_actions": dict(Counter(str(j.get("action")) for j in audit)),
            "audit_rows": len(audit), "vwap_quadrants": dict(quadrants), "scoreboard": board}


def print_report(r: Dict[str, Any]) -> None:
    print("=" * 78)
    print("闸门拦截逐笔审计 — %s" % r["date"])
    print("=" * 78)
    th = r["thresholds"]
    print("\n阈值真值: vwap %.4f | stale %.0fms | slope %.4f%s" % (
        th["vwap_tol"], th["stale_ms"], th["slope_tol"],
        "  [配置读不到, 用缺省]" if "_fallback" in th else ""))
    print("指令 %d 条, 执行 %d 条, 被挡 %d 条" % (
        r["n_instructions"], r["n_executed"], len(r["blocked"])))
    print("\n── A. 拦截概览 ──")
    for gate, n in Counter(b["gate"] for b in r["blocked"]).most_common():
        print("  %-34s %d 笔" % (gate, n))
    print("  exec_gate_audit 当日 %d 行, action 分布: %s" % (
        r["audit_rows"], r["audit_actions"]))

    print("\n── B. 贴边排行 (实际/阈值, 越小越贴边; <=%.2f 视为贴边) ──" % MARGIN)
    print("  注: 比值基于 skip_reason 里的**已四舍五入**参数 (dev 1位/坡率4位) ⇒ 1.000 = 恰在或刚过线, 非精确值")
    rows = [b for b in r["blocked"] if b["proximity"] is not None]
    rows.sort(key=lambda b: b["proximity"])
    if not rows:
        print("  (无带参数的拦截)")
    for b in rows:
        flag = "★贴边" if b["proximity"] <= MARGIN else "    "
        print("  %s %-10s %-28s 比值 %.3f  %s" % (
            flag, b["symbol"], b["gate"], b["proximity"], b["raw"][:60]))

    print("\n── C. vwap A/B 可比性 (四象限) ──")
    for q, items in sorted(r["vwap_quadrants"].items()):
        print("  %-18s %d 只  %s" % (q, len(items),
              ", ".join("%s%s" % (i["symbol"], "(+%s)" % i["pnl"] if i["pnl"] is not None else "") for i in items)))
    quad = r["vwap_quadrants"]
    real_bypass = quad.get("真bypass_成交", [])
    fake_bypass = quad.get("假bypass_被下游门拦", [])
    print("  ⇒ 假 bypass %d / bypass 臂 %d ⇒ 双臂可比性 %s" % (
        len(fake_bypass), len(real_bypass) + len(fake_bypass),
        "OK" if not fake_bypass else "被下游门污染 (边际效应不可测)"))
    for i in real_bypass:
        print("     真bypass 成交结果: %s pnl=%s" % (i["symbol"], i["pnl"]))

    print("\n── D. 既有记分牌对照 (data/gate_block_counterfactual_latest.json) ──")
    if not r["scoreboard"]:
        print("  (记分牌未生成; 跑 scripts/gate_block_counterfactual.py)")
    else:
        for reason in ("vwap_gate_block", "falsification_vwap_slope", "extreme_gap",
                       "runaway_up", "runaway_down", "low_atr_block", "vol_gate_block"):
            row = r["scoreboard"].get(reason)
            if not row:
                continue
            print("  %-26s n_inst=%-4s verdict=%-8s open_t1=%-8s net=%-8s pos%%=%s" % (
                reason, row.get("n_inst"), row.get("verdict"),
                ("%.4f" % row["open_t1"]) if isinstance(row.get("open_t1"), (int, float)) else "—",
                ("%.4f" % row["net"]) if isinstance(row.get("net"), (int, float)) else "—",
                ("%.0f%%" % (100 * row["pos_rate"])) if isinstance(row.get("pos_rate"), (int, float)) else "—"))


def main() -> int:
    ap = argparse.ArgumentParser(description="闸门拦截逐笔审计 (只读)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = build(a.date)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0
    print_report(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
