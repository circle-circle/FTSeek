#!/usr/bin/env python3
"""盘前失效评估器影子结算器 (2026-09-11, 用户裁「做」— 断头传感器修复).

背景: `app/playbook/premarket_invalidation.py` (8/9 落地, record-only) 每日 09:35-09:59 ET
对当日全部可执行指令打 A/B/C 三规则标记, 落 `data/premarket_invalidation_shadow.jsonl`。
31 天 310 行零结算 — 全仓无结算器、台账无此行 (8/28 "传感器静默零产出" 同款病灶)。
本脚本是缺失的判分侧: 纯读, 零写入影子文件, 零交易行为变更。

三规则 (与 evaluate_rules SSOT 同源, 阈值见 premarket_invalidation.py):
  A_short_chase: SHORT 且盘前涨 >+3% (逆势追空, DEB-20260808)
  B_long_knife:  LONG 且盘前跌 <-5% (接飞刀; **执行时刻口径硬门 RFC-068 已在线**,
                 本影子是盘前快照口径 — 两者可分离: 盘前打标但执行时已回升出区 → 放行成交,
                 该子集正是"回升放行"政策的实测样本)
  C_long_chase:  LONG 且盘前涨 >+3% (追涨延伸日)

结算口径 (预注册, 2026-09-11 写死在代码, 先于任何裁决读数):
  对每条 flagged 行 join trade_records (decision_id 精确匹配, 已平仓+净额非空):
    - executed 子集: 影子未拦 (record-only) ⇒ 实际结局 = 反事实结局,
      禁入价值 = −Σpnl_net (executed); 误杀率 = executed 中 pnl_net>0 占比。
    - not-executed 子集只计数 (无结局, 不进价值口径)。
  判据 (与 D1' 同族, 预注册):
    每规则 executed n≥20 ∧ 禁入价值>0 (即 executed 净<0) ∧ 误杀率≤40%
    ∧ 全窗与滚动20交易日两窗同向 → 提请转正裁决 (另裁, 不自动生效)。
    任一不满足 → KEEP_SAMPLING。B 规则额外注记: 其硬门已在线 (执行时刻口径),
    本判据针对的是"盘前口径收紧/放宽执行时刻口径"的增量, 裁决时须对照 RFC-068 勘误节。

退役闸 (2026-09-11 DEB-20260911 两轮多模型辩论, 用户裁「按建议执行」):
  A_short_chase / C_long_chase 已裁退役 (昨收轴追高/追空侧; 采集侧同日停采,
  见 premarket_invalidation.py _RULES 注)。历史行仍照常结算呈报 — 退役=停采集+
  禁转正, 不删数据不抹读数 — 但裁决恒为 RETIRED_NO_PROMOTION, 任何后续读数
  不得作为转正依据。

转正前冲突核对 (DEB-20260911 Q4, 防双影子双转正):
  READY_TO_ADJUDICATE 前置 _conflict_check: 相关组件须在注册表
  (app/playbook/shadow_registry.py) 或台账 (docs/CONTROL-LOOP-LEDGER.md)
  可互引; 注册表/台账不可读或任一相关组件未互引 → 降级 KEEP_SAMPLING 并
  stderr 留痕 (宁可不转正, 不可带病转正)。

挂载: crontab 2 6 * * 2-6 (D2' 58 5 结算后同班; 本器只依赖 trade_records 已平仓行, 不依赖 fwd 回填)。
产物: data/premarket_invalidation_report_latest.json (atomic write)。

用法: venv/bin/python scripts/settle_premarket_invalidation.py [--days N]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
SHADOW = ROOT / "data" / "premarket_invalidation_shadow.jsonl"
OUT = ROOT / "data" / "premarket_invalidation_report_latest.json"

MIN_N = 20          # 预注册: 每规则 executed 样本下限
MAX_MISKILL = 0.40  # 预注册: 误杀率上限
RULES = ("A_short_chase", "B_long_knife", "C_long_chase")

# 2026-09-11 DEB-20260911 裁决 (用户裁「按建议执行」): 昨收轴追高/追空侧退役。
# 历史行仍照常结算呈报 (退役=停采集+禁转正, 不删数据不抹读数), 裁决恒
# RETIRED_NO_PROMOTION。回滚=退役须另开辩论, 不得仅删本元组。
RETIRED_RULES = ("A_short_chase", "C_long_chase")

# 转正前冲突核对 (DEB-20260911 Q4): 规则 → 转正前必须在注册表或台账可互引的
# 相关组件。A/C 已退役永不达此关, 列 B 备将来; 新规则入会须先登记此表。
_CONFLICT_REFS = {
    "B_long_knife": ("RFC-068",),  # 执行时刻口径硬门已在线, 转正裁决须对照勘误节
}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _load_flags() -> list:
    if not SHADOW.exists():
        return []
    out = []
    for ln in SHADOW.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception as e:  # 坏行留痕不静默 (settle_eod_depth R5 同款纪律)
            print(f"[settle_pmi] 影子档坏行跳过: {e} | {ln[:120]!r}", file=sys.stderr)
            continue
        if r.get("would_invalidate"):
            out.append(r)
    return out


def _settle(flags: list, conn: sqlite3.Connection, roll_since: str) -> dict:
    by_rule = {k: {"flagged": 0, "executed": 0, "net": 0.0, "wins": 0,
                   "not_executed": 0, "roll_flagged": 0, "roll_executed": 0,
                   "roll_net": 0.0, "roll_wins": 0}
               for k in RULES}
    for f in flags:
        did = f.get("decision_id")
        d = f.get("date") or ""
        t = None
        if did:
            t = conn.execute(
                "SELECT COALESCE(pnl_net, realized_pnl) AS net FROM trade_records "
                "WHERE decision_id=? AND realized_pnl IS NOT NULL AND exit_time IS NOT NULL "
                "LIMIT 1", (did,)).fetchone()
        in_roll = d >= roll_since
        for rule in f.get("would_invalidate") or []:
            a = by_rule.get(rule)
            if a is None:
                continue
            a["flagged"] += 1
            if in_roll:
                a["roll_flagged"] += 1
            if t is None:
                a["not_executed"] += 1
                continue
            net = float(t["net"] or 0.0)
            a["executed"] += 1
            a["net"] += net
            a["wins"] += 1 if net > 0 else 0
            if in_roll:
                a["roll_executed"] += 1
                a["roll_net"] += net
                a["roll_wins"] += 1 if net > 0 else 0
    return by_rule


def _conflict_check(rule: str):
    """转正前冲突核对 (DEB-20260911 Q4). 返回 (ok, detail).

    注册表/台账不可读, 或 _CONFLICT_REFS 声明的相关组件未在两者任一互引
    → (False, detail), 调用方降级 KEEP_SAMPLING 并 stderr 留痕。
    """
    refs = _CONFLICT_REFS.get(rule, ())
    if not refs:
        return True, "无声明的相关组件"
    try:
        corpus = (ROOT / "app" / "playbook" / "shadow_registry.py").read_text(
            encoding="utf-8")
    except Exception as e:
        return False, f"注册表不可读: {e}"
    try:
        corpus += "\n" + (ROOT / "docs" / "CONTROL-LOOP-LEDGER.md").read_text(
            encoding="utf-8")
    except Exception as e:
        return False, f"台账不可读: {e}"
    missing = [r for r in refs if r not in corpus]
    if missing:
        return False, f"相关组件未互引: {missing}"
    return True, f"互引齐: {list(refs)}"


def _verdict(rule: str, a: dict, conflict=None) -> str:
    """裁决顺序: 退役闸 → 预注册四关 (全窗 ∧ 滚动20日同向) → 转正前冲突核对.

    conflict: 调用方预算好的 (ok, detail) — 供 main() 一次计算两处复用 (裁决 + 报告留痕);
    缺省则本函数自行核对 (单测两参调用形态)。
    """
    if rule in RETIRED_RULES:
        return "RETIRED_NO_PROMOTION"
    if a["executed"] < MIN_N:
        return "KEEP_SAMPLING"
    miskill = a["wins"] / a["executed"]
    full_ok = a["net"] < 0 and miskill <= MAX_MISKILL
    roll_ok = (a["roll_executed"] >= 5 and a["roll_net"] < 0
               and (a["roll_wins"] / a["roll_executed"]) <= MAX_MISKILL)
    if not (full_ok and roll_ok):
        return "KEEP_SAMPLING"
    ok, detail = conflict if conflict is not None else _conflict_check(rule)
    if not ok:
        print(f"[settle_pmi] conflict_check 降级: {rule} READY_TO_ADJUDICATE "
              f"→ KEEP_SAMPLING ({detail})", file=sys.stderr)
        return "KEEP_SAMPLING"
    return "READY_TO_ADJUDICATE"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28, help="滚动窗交易日近似 (自然日)")
    args = ap.parse_args()

    flags = _load_flags()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    roll_since = (date.today() - timedelta(days=args.days)).isoformat()

    by_rule = _settle(flags, conn, roll_since)
    conn.close()

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "flags_total": len(flags),
        "window": {"roll_since": roll_since},
        "prereg": (f"每规则 executed n≥{MIN_N} ∧ 禁入价值>0(executed净<0) ∧ 误杀率≤{MAX_MISKILL:.0%} "
                   f"∧ 全窗与滚动窗同向 → READY_TO_ADJUDICATE (提请转正另裁, 不自动生效); "
                   f"B_long_knife 执行时刻硬门已在线(RFC-068), 其判据针对盘前口径增量"),
        "rules": {},
    }
    for k in RULES:
        a = by_rule[k]
        miskill = (a["wins"] / a["executed"]) if a["executed"] else None
        # 冲突核对一次算两处用 (裁决 + 报告留痕): 降级必须在产物里可审计, 否则
        # "conflict_check 挡下"与"四关没过"在 latest.json 里长得一模一样。
        cc = None if k in RETIRED_RULES else _conflict_check(k)
        report["rules"][k] = {
            **a,
            "ban_value_usd": round(-a["net"], 2),
            "miskill_rate": round(miskill, 3) if miskill is not None else None,
            "conflict_check": ({"ok": cc[0], "detail": cc[1],
                                "refs": list(_CONFLICT_REFS.get(k, ()))} if cc else None),
            "verdict": _verdict(k, a, cc),
        }
    _atomic_write(OUT, json.dumps(report, ensure_ascii=False, indent=1))

    # stdout (cron log 捕获)
    print(f"[settle_pmi] flags={len(flags)} → {OUT.name}")
    for k in RULES:
        r = report["rules"][k]
        mk = f"{r['miskill_rate']*100:.0f}%" if r["miskill_rate"] is not None else "-"
        print(f"  {k:14s} flagged={r['flagged']:2d} exec={r['executed']:2d} "
              f"净${r['net']:+8.2f} 误杀={mk:4s} 滚动20日 exec={r['roll_executed']:2d} "
              f"净${r['roll_net']:+8.2f} → {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
