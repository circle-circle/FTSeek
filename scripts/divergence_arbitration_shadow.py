#!/usr/bin/env python3
"""分歧仲裁 · 影子记录 (只读, 2026-07-12)。

问题: DA 侧 llm_synth.py 处理"2模型方向分歧"时只看 score 差距, 不区分
  - 单方 SKIP (如 BUY vs SKIP): 现状保留原方向 → 合理
  - 绝对反向 (BUY vs SELL_SHORT): 现状仍保留 score 高的一方 → 可能开出高风险单 (如 NVDA 亏损)

候选新规则 (用户提出):
  - 绝对反向 (一多一空)         → SKIP (阻塞, 现状缺这道保护)
  - 单方 SKIP + 另一方有方向    → 跟随有方向的一方 (现状已如此)
  - 其余                        → 保持现状

本脚本 **零 live 改动**: 读历史 playbook_instructions 的 raw_instruction_json.debate_outcome,
对每个分歧候选套用新规则, 与现状对比, 并 join pnl_pct 回答:
  "现状放行的绝对反向单, pnl_pct 是赚是亏? 新规则拦掉是否更好?"
记录累积到 data/shadow/divergence_arbitration_shadow.jsonl (按 instruction id upsert)。

运行: PYTHONPATH=. ./venv/bin/python scripts/divergence_arbitration_shadow.py [--days N] [--push]
"""
import argparse
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage.db_pool import get_db
from app.utils.timezone import ET
SHADOW = Path(__file__).resolve().parents[1] / "data" / "shadow" / "divergence_arbitration_shadow.jsonl"
DATA_DIR = Path(__file__).resolve().parents[1] / "data"
# 主模型 = DA config.LLM_MODEL (2026-07-12: qwen3.7-max, 产出 orig_dir);
# 挑战者模型名从 bridge.debate.challenger_model 读 (2026-07: deepseek-v4-pro, 产出 chall_dir)。
PRIMARY_MODEL = "qwen3.7-max"

LONG = {"BUY", "LONG"}
SHORT = {"SELL_SHORT", "SHORT"}
SKIP = {"SKIP", "NEUTRAL", "", "NONE"}


def _norm(d):
    return (d or "").strip().upper()


def _parse_debate(text):
    """返回 (kind, a, b): kind ∈ {DIVERGE, AGREE, NONE}; a/b 为方向 (DIVERGE 时)。"""
    if not text:
        return "NONE", None, None
    m = re.search(r"\(([A-Za-z_]+)\s+vs\s+([A-Za-z_]+)\)", text)
    if m and "分歧" in text:
        return "DIVERGE", _norm(m.group(1)), _norm(m.group(2))
    if "方向一致" in text or "一致" in text:
        return "AGREE", None, None
    return "NONE", None, None


def _diverge_type(a, b):
    """OPPOSITE=一多一空 / ONE_SKIP=单方SKIP / OTHER。"""
    if (a in LONG and b in SHORT) or (a in SHORT and b in LONG):
        return "OPPOSITE"
    if a in SKIP or b in SKIP:
        return "ONE_SKIP"
    return "OTHER"


def _new_rule(kind, a, b, current_dir):
    """新规则判定的方向。OPPOSITE→SKIP; ONE_SKIP→跟随有方向一方; 其余→保持现状。"""
    if kind != "DIVERGE":
        return current_dir
    t = _diverge_type(a, b)
    if t == "OPPOSITE":
        return "SKIP"
    if t == "ONE_SKIP":
        real = a if a not in SKIP else b
        return "BUY" if real in LONG else ("SELL_SHORT" if real in SHORT else "SKIP")
    return current_dir


def _bridge_debate(date, _cache={}):
    """读 opportunity_scan_{date}.json → {symbol: debate_dict}; 文件缺失返回空。
    提供 challenger_model / challenger_direction / challenger_fallback 等模型归因信息。"""
    if date in _cache:
        return _cache[date]
    m = {}
    p = DATA_DIR / f"opportunity_scan_{date}.json"
    if p.exists():
        try:
            for c in json.loads(p.read_text()).get("candidates", []) or []:
                if isinstance(c, dict) and c.get("symbol"):
                    m[c["symbol"]] = c.get("debate") or {}
        except Exception:
            pass
    _cache[date] = m
    return m


def _collect(days):
    end = datetime.now(ET)
    start = (end - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    with get_db().connect("ftseek_us") as c:
        rows = c.execute(
            "SELECT id, date, symbol, direction, executed, outcome, pnl_pct, raw_instruction_json "
            "FROM playbook_instructions WHERE date >= ? AND raw_instruction_json IS NOT NULL "
            "AND raw_instruction_json != ''", (start,)).fetchall()
    for r in rows:
        try:
            j = json.loads(r["raw_instruction_json"])
        except Exception:
            continue
        kind, a, b = _parse_debate(j.get("debate_outcome"))
        if kind != "DIVERGE":
            continue
        cur = _norm(r["direction"])
        new = _new_rule(kind, a, b, cur)
        deb = _bridge_debate(r["date"]).get(r["symbol"], {})
        out.append({
            "id": r["id"], "date": r["date"], "symbol": r["symbol"],
            "orig_dir": a, "chall_dir": b, "type": _diverge_type(a, b),
            "orig_model": PRIMARY_MODEL,
            "chall_model": deb.get("challenger_model"),
            "chall_fallback": deb.get("challenger_fallback"),
            "chall_verdict": deb.get("challenger_verdict"),
            "current_dir": cur, "current_executed": int(r["executed"] or 0),
            "current_outcome": r["outcome"], "pnl_pct": r["pnl_pct"],
            "new_rule_dir": new, "differs": bool(new != cur),
            "recorded_at": datetime.now(ET).isoformat(),
        })
    return out


def _upsert(records):
    SHADOW.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if SHADOW.exists():
        for line in SHADOW.read_text().splitlines():
            try:
                o = json.loads(line)
                existing[o["id"]] = o
            except Exception:
                pass
    for rec in records:
        existing[rec["id"]] = rec
    with SHADOW.open("w") as f:
        for o in existing.values():
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return len(existing)


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    wins = sum(1 for v in vals if v > 0)
    return {"n": n, "mean_pnl_pct": mean, "sum_pnl_pct": sum(vals), "winrate": wins / n}


def _report(records):
    lines = ["=" * 68, "分歧仲裁影子 (只读) — 新规则 vs 现状", "=" * 68]
    if not records:
        lines.append("窗口内无分歧候选 (无 DIVERGE debate_outcome)。")
        return "\n".join(lines)

    by_type = Counter(r["type"] for r in records)
    lines.append(f"分歧候选 {len(records)} 个 | 类型: OPPOSITE={by_type['OPPOSITE']} "
                 f"ONE_SKIP={by_type['ONE_SKIP']} OTHER={by_type['OTHER']}")

    # 核心: OPPOSITE(绝对反向) — 现状放行, 新规则拦截
    opp = [r for r in records if r["type"] == "OPPOSITE"]
    opp_exec = [r for r in opp if r["current_executed"]]
    lines.append("")
    lines.append(f"【绝对反向 OPPOSITE】{len(opp)} 个 (现状保留方向 / 新规则→SKIP拦截)")
    for r in opp:
        tag = "成交" if r["current_executed"] else "未成交"
        pnl = f"pnl_pct={r['pnl_pct']}" if r["pnl_pct"] is not None else "pnl_pct=NA"
        lines.append(f"  {r['date']} {r['symbol']}: {r['orig_dir']} vs {r['chall_dir']} "
                     f"→现状={r['current_dir']}({tag},{r['current_outcome']},{pnl}) →新规则=SKIP")
    st = _stats([r["pnl_pct"] for r in opp_exec])
    if st:
        verdict = "拦掉更好 ✅ (这些已成交反向单净亏)" if st["mean_pnl_pct"] < 0 else "拦掉会错失 ⚠️ (净盈)"
        lines.append(f"  → 已成交反向单 {st['n']} 笔: 均 pnl_pct={st['mean_pnl_pct']:.4f} "
                     f"总={st['sum_pnl_pct']:.4f} 胜率={st['winrate']*100:.0f}% → {verdict}")
    else:
        lines.append("  → 尚无已成交的反向单可评估 (数据积累中)")

    # ONE_SKIP: 新规则与现状一致 (跟随有方向一方), 仅信息展示
    one = [r for r in records if r["type"] == "ONE_SKIP"]
    one_exec = [r for r in one if r["current_executed"]]
    lines.append("")
    lines.append(f"【单方SKIP ONE_SKIP】{len(one)} 个 (新规则跟随方向, 与现状一致 → 无变化)")
    st2 = _stats([r["pnl_pct"] for r in one_exec])
    if st2:
        lines.append(f"  已成交 {st2['n']} 笔: 均 pnl_pct={st2['mean_pnl_pct']:.4f} 胜率={st2['winrate']*100:.0f}%")

    # 模型归因: 分歧时各模型选的方向 + (已成交时) 谁对
    lines.append("")
    lines.append("【模型归因】主模型(orig) vs 挑战者(chall) 各自选的方向")
    model_calls = Counter()
    for r in records:
        if r.get("orig_model"):
            model_calls[(r["orig_model"], r["orig_dir"])] += 1
        if r.get("chall_model"):
            model_calls[(r["chall_model"], r["chall_dir"])] += 1
    for (mdl, d), n in sorted(model_calls.items()):
        lines.append(f"  {mdl}: {d} × {n}")
    scored = [r for r in records if r["current_executed"] and r["pnl_pct"] is not None]
    if scored:
        lines.append("  已成交分歧的对错 (交易方向属于哪个模型):")
        for r in scored:
            winner = r["orig_model"] if r["current_dir"] == r["orig_dir"] else r.get("chall_model")
            res = "赢" if r["pnl_pct"] > 0 else "亏"
            lines.append(f"    {r['symbol']}: 交易={r['current_dir']}(来自{winner}) "
                         f"pnl_pct={r['pnl_pct']} → {res}")

    diff = [r for r in records if r["differs"]]
    lines.append("")
    lines.append(f"新规则与现状产生差异的候选: {len(diff)} 个 (全部来自 OPPOSITE→SKIP)")
    lines.append("注: 未成交的差异无 pnl 无法评估; 结论以'已成交反向单 pnl_pct'为准, 需样本积累。")
    lines.append("=" * 68)
    return "\n".join(lines)


def main(days=30, push=False):
    records = _collect(days)
    total = _upsert(records)
    report = _report(records)
    print(report)
    print(f"\n影子记录已 upsert: 本窗口 {len(records)} 条, 累计 {total} 条 → {SHADOW}")
    if push:
        try:
            from app.notifier.feishu import send_text
            opp = [r for r in records if r["type"] == "OPPOSITE"]
            send_text(f"🌓 分歧仲裁影子: {len(records)}分歧候选, 绝对反向{len(opp)}个(新规则将拦), 累计{total}条")
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="分歧仲裁影子 (只读, 零 live 改动)")
    ap.add_argument("--days", type=int, default=30, help="回看天数 (默认30)")
    ap.add_argument("--push", action="store_true", help="推送飞书摘要")
    a = ap.parse_args()
    main(days=a.days, push=a.push)
