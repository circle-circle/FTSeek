#!/usr/bin/env python3
"""C2 论据记分牌三分类 (2026-07-24, DEB-20260724 §八共识落地).

背景: factor_ic_latest.json 的 verdict 列标签写着 alpha 实际按 IC 判定 (判据混淆,
第 2 轮辩论 §7.4 抓出), 且单一判据会误判——IC 与 alpha 度量两样东西:
  - IC (Spearman) = 排序力 (信号强弱与前瞻收益的秩相关)
  - avg_alpha     = 方向调整后剥 beta 超额 = 均值方向盈亏

三分类处置规则 (第 2/3 轮共识, 预注册):
  ①样本不足   n < MIN_N 或 有交易记录但 trade_n < MIN_TRADE_N → 不下结论, 不进任何 live 路径
  ②方向错用   IC > +0.05 且 avg_alpha < 0 → 排序力真实但被反着用 (如"估值贵"看空论据
              支撑做多) → 处置=REVERSE_CANDIDATE (改作回避/反向过滤器, 不删除)
  ③健康       IC > +0.05 且 avg_alpha ≥ 0 → 处置=KEEP (可加权)
  ④无价值     |IC| ≤ 0.05 且 avg_alpha < 0 → 处置=PRUNE_CANDIDATE (从 prompt 降权/剔除)
  ⑤反向IC     IC < -0.05 → 处置=MANUAL_REVIEW (可能信号方向标注错误, 人工复核)
  其余         → WATCH (中性观察)

输出: data/factor_verdicts.json + 控制台表格。
纯离线分析产物 (record-only): 本脚本不改任何 prompt/config; 处置建议的消费
(回灌 DA/UE prompt) 是 C2 的第二步, 需另行裁决后实施。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IC_LEDGER = ROOT / "data" / "factor_ic_latest.json"
OUT = ROOT / "data" / "factor_verdicts.json"

# 预注册阈值 (第 2 轮约束②: 最低样本门槛; 阈值与 factor_ic_backfill 的 0.05 对齐)
MIN_N = 30          # signal 层最低样本
MIN_TRADE_N = 20    # trade 层最低样本 (insider_net_sell 7 笔教训)
IC_TH = 0.05


def classify(name: str, d: dict) -> dict:
    ic = d.get("ic")
    n = d.get("n", 0)
    alpha = d.get("avg_alpha")
    trade_n = d.get("trade_n")  # 可能缺失 (未回填 trade 层)

    row = {
        "factor": name, "n": n, "ic": round(ic, 4) if ic is not None else None,
        "avg_alpha_pct": round(alpha * 100, 3) if alpha is not None else None,
        "trade_n": trade_n,
        "trade_wr": round(d.get("trade_wr", 0), 3) if d.get("trade_wr") is not None else None,
    }

    if ic is None or alpha is None or n < MIN_N:
        row.update(verdict="样本不足", action="NONE",
                   note=f"n={n}<{MIN_N} 或字段缺失, 不下结论")
    elif trade_n is not None and trade_n < MIN_TRADE_N:
        # signal 层可参考, 但 trade 层结论 (胜率/盈亏) 不可判
        if ic > IC_TH and alpha < 0:
            row.update(verdict="方向错用(候选)", action="REVERSE_CANDIDATE",
                       note=f"signal层判据成立但 trade_n={trade_n}<{MIN_TRADE_N}, 仅候选")
        else:
            row.update(verdict="trade样本不足", action="NONE",
                       note=f"trade_n={trade_n}<{MIN_TRADE_N}")
    elif ic > IC_TH and alpha < 0:
        row.update(verdict="方向错用", action="REVERSE_CANDIDATE",
                   note="排序力真实但方向盈亏为负 → 改回避/反向过滤, 不删除")
    elif ic > IC_TH and alpha >= 0:
        row.update(verdict="健康", action="KEEP", note="排序力+方向盈亏双正")
    elif abs(ic) <= IC_TH and alpha < 0:
        row.update(verdict="无价值", action="PRUNE_CANDIDATE",
                   note="无排序力且负α → prompt 降权/剔除候选")
    elif ic < -IC_TH:
        row.update(verdict="反向IC", action="MANUAL_REVIEW",
                   note="IC 显著为负, 疑似方向标注错误, 人工复核")
    else:
        row.update(verdict="观察", action="WATCH", note="")
    return row


def main() -> int:
    if not IC_LEDGER.exists():
        print(f"[factor_verdict] 台账不存在: {IC_LEDGER}", file=sys.stderr)
        return 1
    ledger = json.loads(IC_LEDGER.read_text())
    rows = [classify(k, v) for k, v in ledger.items() if isinstance(v, dict)]
    rows.sort(key=lambda r: (r["action"], -(r["ic"] or 0)))

    out = {
        "generated_at": datetime.now().isoformat(),
        "criteria": {"min_n": MIN_N, "min_trade_n": MIN_TRADE_N, "ic_threshold": IC_TH,
                     "source": "DEB-20260724 §7.4/§八 三分类共识"},
        "verdicts": rows,
        "summary": {},
    }
    for r in rows:
        out["summary"][r["action"]] = out["summary"].get(r["action"], 0) + 1

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print(f"{'因子':<28} {'n':>5} {'IC':>8} {'α%':>8} {'tr_n':>5} {'判定':<14} 处置")
    print("-" * 92)
    for r in rows:
        print(f"{r['factor']:<28} {r['n']:>5} "
              f"{(r['ic'] if r['ic'] is not None else float('nan')):>8.3f} "
              f"{(r['avg_alpha_pct'] if r['avg_alpha_pct'] is not None else float('nan')):>8.3f} "
              f"{(r['trade_n'] if r['trade_n'] is not None else '-'):>5} "
              f"{r['verdict']:<14} {r['action']}")
    print(f"\n汇总: {out['summary']}  → 已写 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
