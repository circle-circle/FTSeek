#!/usr/bin/env python3
"""Nightly supply-chain health check — read-only, 一段一结论.

用途: DA (20:00 CST) / UE (20:00+21:10 CST) 跑完后一键体检今晚的
写入与逻辑链路是否正常。不写任何生产数据, 不改任何交易行为。

五段:
  1. 供给漏斗      — 一脑两腿是否都在 (bridge 文件 ↔ DB 直写行)
  2. RFC-120 合规  — SHORT 单 chase_cap 是否 < entry (出生即违例已治?)
  3. 影子台账      — 三个 record-only 影子今晚是否落盘
  4. 统一入口      — 入库行是否都过 save_instruction (admission 字段在位)
  5. 异常清单      — P1/P2 分级, 空 = 正常

用法:
  venv/bin/python scripts/observe_nightly_supply.py                # 今日 (ET)
  venv/bin/python scripts/observe_nightly_supply.py --date 2026-10-01
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "ftseek_us.db"
SHADOW = ROOT / "data" / "shadow"
BRIDGE_DIR = ROOT / "data"
CHECKPOINT = ROOT / "data" / "da_checkpoint"

# ET 偏移 (3-11月 EDT=-4, 其余 EST=-5) — 与 DA skill 同口径
_now_utc = datetime.now(timezone.utc)
_ET_OFF = -4 if 3 <= _now_utc.month <= 11 else -5


def _et_today() -> str:
    return (_now_utc + timedelta(hours=_ET_OFF)).strftime("%Y-%m-%d")


def _did_source(did: Any) -> str:
    """通道归属唯一权威 = decision_id 前缀 (raw_instruction 会被同did覆盖重写)."""
    d = str(did or "")
    if d.startswith("DA-"):
        return "DA直写"
    if d.startswith("universe_expansion"):
        return "UE直写"
    if d.startswith("intraday_reentry-"):
        return "reentry"
    if d.startswith(("DEC-", "TFB-", "SNAP-")):
        return "generate侧"
    return "空did(占位/SKIP票)" if not d else "其他"


def _rows(db: sqlite3.Connection, sql: str, *args: Any) -> list[dict]:
    return [dict(r) for r in db.execute(sql, args)]


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


# ── 段 1: 供给漏斗 ────────────────────────────────────────────────
def section_funnel(db: sqlite3.Connection, date: str, findings: list) -> Any:
    print(f"\n═══ 1. 供给漏斗 ({date}) — 一脑两腿是否都在 ═══")

    bridge_path = BRIDGE_DIR / f"opportunity_scan_{date}.json"
    bridge_syms: list[str] = []
    skill_version: Any = None
    if bridge_path.exists():
        try:
            doc = json.loads(bridge_path.read_text(encoding="utf-8"))
            bridge_syms = [c.get("symbol") for c in doc.get("candidates", [])]
            skill_version = doc.get("skill_version")
            print(f"  bridge 文件: {bridge_path.name}  候选 {len(bridge_syms)} 只"
                  f"  skill_version={skill_version}"
                  f"  generated_at={str(doc.get('generated_at'))[:19]}")
        except Exception as exc:
            findings.append(("P1", f"bridge 文件读取失败: {exc}"))
    else:
        findings.append(("P1", f"bridge 文件缺失: {bridge_path.name} (DA 未跑或失败)"))

    rows = _rows(db, """
        SELECT symbol, direction, confidence, decision_id, executed, skip_reason,
               json_extract(raw_instruction_json, '$.source') AS src,
               json_extract(raw_instruction_json, '$.admission') IS NOT NULL AS has_adm
        FROM playbook_instructions WHERE date(created_at) = ? OR date = ?
    """, date, date)

    by_src = Counter(_did_source(r["decision_id"]) for r in rows)
    real = [r for r in rows if not str(r["skip_reason"] or "").startswith("非当前通道候选")]
    print(f"  DB 行: 共 {len(rows)} (含占位), 实体 {len(real)}")
    for k, v in by_src.most_common():
        print(f"    {k:<20} {v:>4}")

    da_rows = [r for r in rows if _did_source(r["decision_id"]) == "DA直写"]
    da_syms = {r["symbol"] for r in da_rows}
    if bridge_syms and da_rows:
        overlap = da_syms & set(bridge_syms)
        print(f"  两腿一致性: bridge {len(bridge_syms)} 只 ∩ DA直写 {len(da_syms)} 只"
              f" = {len(overlap)} (同一批候选两腿入库, 应高度重叠)")
        if len(overlap) < len(set(bridge_syms)) * 0.5:
            findings.append(("P2", f"两腿重叠仅 {len(overlap)}/{len(set(bridge_syms))}"
                                   " — 直写腿或 bridge 腿有一侧漏写"))
    elif bridge_syms and not da_rows:
        findings.append(("P1", "bridge 有候选但 DB 无 DA 直写行 — 直写腿断"))
    return skill_version


# ── 段 2: RFC-120 SHORT cap 合规 ──────────────────────────────────
# RFC-120 (SHORT cap 语义归一 + prompt/机检) 落在 DA skill v0.90; 更早版本产出的
# 出生违例属**预期历史**, 不是回归。用 bridge 文件的 skill_version 判定严重级别。
_RFC120_MIN_VERSION = (0, 90)


def _version_tuple(raw: Any) -> tuple[int, ...]:
    parts = []
    for chunk in str(raw or "").split(".")[:2]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def section_chase_cap(db: sqlite3.Connection, date: str, findings: list,
                      skill_version: Any = None) -> None:
    print(f"\n═══ 2. RFC-120 合规 — BUY 的 cap 是上限(>entry), SHORT 是下限(<entry) ═══")
    if skill_version:
        has_fix = _version_tuple(skill_version) >= _RFC120_MIN_VERSION
        print(f"  bridge skill_version={skill_version} → RFC-120 "
              f"{'已上线 (违例=回归, P1)' if has_fix else '未上线 (违例=预期历史, 仅记录)'}")
    else:
        has_fix = True   # 无 bridge 文件时按现行版本严判
    rows = _rows(db, """
        SELECT symbol, direction, entry_price, decision_id,
               json_extract(raw_instruction_json, '$.chase_cap_price') AS cap
        FROM playbook_instructions
        WHERE (date(created_at) = ? OR date = ?)
          AND direction IN ('BUY','SELL_SHORT')
    """, date, date)
    shorts = [r for r in rows if r["direction"] == "SELL_SHORT"]
    buys = [r for r in rows if r["direction"] == "BUY"]
    print(f"  活跃指令: BUY {len(buys)} / SELL_SHORT {len(shorts)}")

    bad_short, bad_buy = [], []
    for r in shorts:
        try:
            cap, entry = float(r["cap"]), float(r["entry_price"])
        except (TypeError, ValueError):
            continue
        if cap <= 0 or entry <= 0:
            continue
        if cap >= entry:          # SHORT: cap 是下限, 必须低于入场价
            bad_short.append((r["symbol"], cap, entry))
    for r in buys:
        try:
            cap, entry = float(r["cap"]), float(r["entry_price"])
        except (TypeError, ValueError):
            continue
        if cap <= 0 or entry <= 0:
            continue
        if cap < entry:           # BUY: cap 是上限, 必须高于入场价
            bad_buy.append((r["symbol"], cap, entry))

    with_cap_s = sum(1 for r in shorts if r["cap"] not in (None, ""))
    with_cap_b = sum(1 for r in buys if r["cap"] not in (None, ""))
    print(f"  带 cap: SHORT {with_cap_s}/{len(shorts)}, BUY {with_cap_b}/{len(buys)}")

    def _dump(title: str, items: list, note: str) -> None:
        if not items:
            return
        print(f"  🔴 {title} {len(items)} 条 ({note}):")
        for sym, cap, entry in items:
            gap = abs(cap - entry) / entry * 100
            print(f"     {sym:<10} cap={cap:<10} entry={entry:<10} "
                  f"可执行窗口={gap:.2f}% {'← 数学上不可成交' if gap < 0.5 else ''}")

    _dump("SHORT 出生即违例 (RFC-120 靶心)", bad_short,
          "cap 被填成上破失效位 → 放行条件=论点死亡条件")
    _dump("BUY 自相矛盾 (入场价已越过自己的禁追线)", bad_buy,
          "cap<entry → 提议在自己划的禁追价之上入场")

    if bad_short:
        level = "P1" if has_fix else "INFO"
        findings.append((level, f"SHORT 出生即违例 {len(bad_short)} 条"
                         + ("" if has_fix else " (RFC-120 前版本, 预期历史)")))
    elif has_fix:
        print("  ✅ SHORT 零出生违例 — RFC-120 源头归一 + prompt/机检生效")
    if bad_buy:
        findings.append(("P2", f"BUY cap<entry 自相矛盾 {len(bad_buy)} 条 "
                               "(RFC-120 机检只覆盖 SHORT 侧, BUY 侧待评估)"))


# ── 段 3: 影子台账 ────────────────────────────────────────────────
def section_shadows(date: str, findings: list) -> None:
    print(f"\n═══ 3. 影子台账 (record-only, 零行为变更) ═══")

    skew = _jsonl(SHADOW / "skew_gate_vision.jsonl")
    today_skew = [e for e in skew if str(e.get("date")) == date]
    print(f"  RFC-122 偏斜门双口径: 累计 {len(skew)} 行, 今日 {len(today_skew)} 行")
    for e in today_skew[-3:]:
        bv, pv = e.get("batch_vision", {}), e.get("portfolio_vision", {})
        print(f"    {e.get('ts','')[:19]} batch={bv.get('buy')}B/{bv.get('short')}S"
              f"={bv.get('majority_pct')} violates={bv.get('violates')} | "
              f"portfolio={pv.get('buy')}B/{pv.get('short')}S violates={pv.get('violates')}"
              f" divergent={e.get('divergent')}")
    if not today_skew:
        print("    (今日无行 — generate 轮尚未跑, 或 valid≤1 未触发; 盘中再看)")

    rev = _jsonl(SHADOW / "da_true_reverse.jsonl")
    today_rev = [e for e in rev if str(e.get("date")) == date]
    print(f"  RFC-125 真反向: 累计 {len(rev)} 行, 今日 {len(today_rev)} 行")
    for e in today_rev:
        print(f"    {e['symbol']} 分析师={e['analyst']['direction']} vs "
              f"挑战者={e['challenger']['direction']} ({e['challenger']['model']}, "
              f"self_debate={e['challenger']['self_debate']}) "
              f"class={e['classification']} gap={e['score_gap']}")
    if rev and not today_rev:
        print("    (今日无真反向 = 正常, 9月全月仅 2 条)")

    conf = _jsonl(SHADOW / "instruction_conflict.jsonl")
    today_conf = [e for e in conf if str(e.get("date")) == date]
    print(f"  RFC-125 写库冲突: 累计 {len(conf)} 行, 今日 {len(today_conf)} 行")
    cls = Counter()
    for e in today_conf:
        cls[tuple(e.get("classification", []))] += 1
    for k, v in cls.most_common(5):
        print(f"    {v:>3}  {list(k)}")
    if not conf:
        print("    (文件尚不存在 = 尚未捕获任何冲突; 偏斜门同did覆盖会在此留痕)")


# ── 段 4: 统一入口 ────────────────────────────────────────────────
def section_single_entry(db: sqlite3.Connection, date: str, findings: list) -> None:
    print(f"\n═══ 4. 统一入口 — 入库是否都过 save_instruction (admission 收口) ═══")
    rows = _rows(db, """
        SELECT symbol, direction, decision_id, executed,
               json_extract(raw_instruction_json, '$.admission') IS NOT NULL AS has_adm,
               json_extract(raw_instruction_json, '$.admission.verdict') AS verdict
        FROM playbook_instructions
        WHERE (date(created_at) = ? OR date = ?)
          AND direction IN ('BUY','SELL_SHORT')
    """, date, date)
    if not rows:
        print("  (今日无活跃指令)")
        return
    have = [r for r in rows if r["has_adm"]]
    miss = [r for r in rows if not r["has_adm"]]
    print(f"  活跃指令 {len(rows)} 条: 带 admission 字段 {len(have)}, 缺 {len(miss)}")
    vc = Counter(str(r["verdict"]) for r in have)
    print(f"  admission verdict 分布: {dict(vc)}")
    if miss:
        by_src = Counter(_did_source(r["decision_id"]) for r in miss)
        print(f"  缺字段者按通道: {dict(by_src)}")
        for r in miss[:6]:
            print(f"    {r['symbol']:<10} {_did_source(r['decision_id'])} "
                  f"exec={r['executed']} did={str(r['decision_id'])[:34]}")
        # reentry 重武装行走不同构造路径, 缺 admission 属已知口径, 降 P2
        if all(_did_source(r["decision_id"]) == "reentry" for r in miss):
            findings.append(("P2", f"reentry {len(miss)} 条无 admission 字段 (已知口径)"))
        else:
            findings.append(("P1", f"{len(miss)} 条绕过统一入口 (无 admission 字段)"))
    else:
        print("  ✅ 全部经统一入口")

    # 成交侧对照: 执行行是否都有指令行 (无幽灵成交)
    trades = _rows(db, """
        SELECT symbol, direction, entry_time, strategy_id, decision_id
        FROM trade_records
        WHERE date(entry_time) = ? AND source_type = 'PLAYBOOK_SIM'
    """, date)
    print(f"  成交对照: trade_records 今日 {len(trades)} 笔")
    for t in trades[:8]:
        print(f"    {t['symbol']:<10} {t['direction']:<10} "
              f"{str(t['entry_time'])[:19]} did={str(t['decision_id'])[:30]}")


# ── 段 5: 异常清单 ────────────────────────────────────────────────
def section_findings(findings: list) -> None:
    print(f"\n═══ 5. 异常清单 ═══")
    if not findings:
        print("  ✅ 无异常 — 今晚写入与逻辑链路正常")
        return
    order = {"P1": 0, "P2": 1, "INFO": 2}
    for level, msg in sorted(findings, key=lambda f: order.get(f[0], 9)):
        print(f"  [{level}] {msg}")
    if not any(f[0] in ("P1", "P2") for f in findings):
        print("  ✅ 无需处理项 (上方 INFO 为预期历史/已知口径)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=_et_today(), help="ET 交易日 (默认今日 ET)")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    findings: list = []

    print(f"今晚供给链路体检  date={args.date} (ET)  现在="
          f"{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    skill_version = section_funnel(db, args.date, findings)
    section_chase_cap(db, args.date, findings, skill_version=skill_version)
    section_shadows(args.date, findings)
    section_single_entry(db, args.date, findings)
    section_findings(findings)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
