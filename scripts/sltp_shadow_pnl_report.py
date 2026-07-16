#!/usr/bin/env python3
"""SL/TP 验证影子 × pnl 联合分析 (只读, 2026-07-12)。

join: data/shadow/sltp_verify_shadow.jsonl 的 inst_id → playbook_instructions.id.pnl_pct
(主键直连, 无模糊匹配)。

核心问题: 代码 SL/TP 与 LLM 分歧越大, 成交的 pnl 是更好还是更差?
注意 (诚实边界):
  - 所有成交实际都用了**代码** SL/TP (reprice 覆盖 LLM 值)。故本报告衡量的是
    "代码偏离 LLM 的幅度" 与结果的**相关** (非因果), 用于判断是否值得推进 L2
    (一致时让 LLM 说了算、分歧时代码兜底)。
  - 若高分歧组明显更好 → 代码的覆盖是对的 (LLM 会更差); 若高分歧组更差/无差异
    → LLM 的数字并不比代码差, 覆盖它没收益。

运行: PYTHONPATH=. ./venv/bin/python scripts/sltp_shadow_pnl_report.py
"""
import json
import statistics
from collections import defaultdict
from pathlib import Path

from app.storage.db_pool import get_db

SHADOW = Path(__file__).resolve().parents[1] / "data" / "shadow" / "sltp_verify_shadow.jsonl"


def _load_shadow():
    if not SHADOW.exists():
        return []
    rows = []
    for line in SHADOW.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _pnl_by_inst(ids):
    ids = [i for i in ids if i is not None]
    if not ids:
        return {}
    out = {}
    with get_db().connect("ftseek_us") as c:
        q = ("SELECT id, pnl_pct, exit_reason FROM playbook_instructions "
             "WHERE id IN (%s)" % ",".join("?" * len(ids)))
        for r in c.execute(q, ids):
            out[r[0]] = {"pnl_pct": r[1], "exit_reason": r[2]}
    return out


def _stats(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    wins = sum(1 for v in vals if v > 0)
    return {"n": len(vals), "mean": statistics.mean(vals),
            "median": statistics.median(vals), "winrate": wins / len(vals)}


def _fmt(s):
    if not s:
        return "n=  0"
    return (f"n={s['n']:>3}  mean={s['mean']:+.4f}  "
            f"median={s['median']:+.4f}  win={s['winrate']*100:.0f}%")


def _group_report(title, pairs):
    """pairs: [(key, pnl)]; 按 key 分组打印 _stats。"""
    g = defaultdict(list)
    for k, pnl in pairs:
        g[k].append(pnl)
    print(f"── {title} ──")
    for k in sorted(g, key=lambda x: str(x)):
        print(f"  {str(k):<22} {_fmt(_stats(g[k]))}")
    print()


def _push(msg):
    """推送飞书 (best-effort, 失败不抛)。"""
    try:
        from app.notifier.feishu import send_text
        send_text(msg)
    except Exception:
        pass


def main(push=False):
    print("=" * 70)
    print("SL/TP 验证影子 × pnl 联合分析")
    print("=" * 70)
    shadow = _load_shadow()
    if not shadow:
        print(f"\n⏳ 暂无影子数据: {SHADOW} 不存在或为空。")
        print("   → 待下一个交易日 reprice 跑过后 (flag sltp_verify_shadow=on) 再复跑本脚本。\n")
        if push:
            _push("📊 SL/TP影子: 今日暂无记录 (reprice 未触发?), 继续观测")
        return

    pnl_map = _pnl_by_inst({r.get("inst_id") for r in shadow})

    # 只保留"已成交且有非零结果"的影子记录
    joined = []
    for r in shadow:
        info = pnl_map.get(r.get("inst_id"))
        pnl = info.get("pnl_pct") if info else None
        if pnl is not None and abs(pnl) > 1e-9:
            r = dict(r)
            r["_pnl"] = pnl
            r["_exit_reason"] = info.get("exit_reason")
            joined.append(r)

    print(f"\n影子记录={len(shadow)}  已join成交(非零pnl)={len(joined)}")
    if not joined:
        print("  → 影子已在记录, 但对应指令尚无平仓结果。等成交平仓后复跑。\n")
        if push:
            _push(f"📊 SL/TP影子: 累计记录 {len(shadow)} 条, 尚无平仓结果, 继续累积")
        return

    # 分歧样本 (sl_agree 明确为 False)
    div = [r for r in joined if r.get("sl_agree") is False]
    agr = [r for r in joined if r.get("sl_agree") is True]
    print(f"  其中 SL 一致={len(agr)}  SL 分歧={len(div)}  "
          f"(缺 LLM SL 无法判定={len(joined)-len(agr)-len(div)})\n")

    # 1) 核心: SL 一致 vs 分歧 的 pnl
    s_ag = _stats([r['_pnl'] for r in agr])
    s_dv = _stats([r['_pnl'] for r in div])
    print("── 1) SL 一致 vs 分歧 的 pnl ──")
    print(f"  一致(agree)  {_fmt(s_ag)}")
    print(f"  分歧(diverge){_fmt(s_dv)}\n")

    # 2) 分歧幅度分箱 vs pnl (仅有 sl_div_pct 的)
    def _bin(d):
        if d is None:
            return "na"
        for lo, hi in [(0, 0.2), (0.2, 0.5), (0.5, 1.0), (1.0, 2.0)]:
            if d < hi:
                return f"[{lo},{hi})"
        return "[2,+)"
    _group_report("2) SL 分歧幅度(占entry%) 分箱 vs pnl",
                  [(_bin(r.get("sl_div_pct")), r["_pnl"]) for r in joined
                   if r.get("sl_div_pct") is not None])

    # 3) 按 regime / direction / bucket 看分歧组
    if div:
        _group_report("3a) 分歧组 by regime", [(r.get("regime") or "?", r["_pnl"]) for r in div])
        _group_report("3b) 分歧组 by direction", [(r.get("direction") or "?", r["_pnl"]) for r in div])
        _group_report("3c) 分歧组 by bucket", [(r.get("bucket") or "?", r["_pnl"]) for r in div])

    print("注: pnl 为 playbook_instructions.pnl_pct 原始存储值; 关注一致/分歧组的相对差异, 非绝对量纲。")
    print("=" * 70)

    if push:
        parts = [f"📊 SL/TP影子累计: 记录{len(shadow)} join成交{len(joined)}",
                 f"一致组 {_fmt(s_ag)}",
                 f"分歧组 {_fmt(s_dv)}"]
        parts.append("✅ 分歧样本≥20, 可评估是否推进L2"
                     if len(div) >= 20 else f"⏳ 分歧样本 {len(div)}/20, 继续累积")
        _push("\n".join(parts))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="SL/TP 验证影子 × pnl 联合分析")
    ap.add_argument("--push", action="store_true", help="推送飞书摘要")
    a = ap.parse_args()
    if a.push:
        from scripts.utils.market_guard import exit_if_not_trading_day
        exit_if_not_trading_day()
    main(push=a.push)
