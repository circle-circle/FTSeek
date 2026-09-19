"""RFC-087 读数汇总: 从三份产物里抽 A/B 臂表 + 配对 + 分片 + 诊断 (只读)."""
import json

FILES = [
    ("W1-盘前 60日 (Jun24-Sep16) 紧闸门 n=56", "data/reports/replay_ext_entry_sources_20260919_premarket_fixed.json"),
    ("W1-盘前 60日 宽闸门 (复算 9/17 依据)", "data/reports/replay_ext_entry_sources_20260919_premarket_loose60.json"),
    ("W1-盘后 60日 (紧闸门)", "data/reports/replay_ext_entry_sources_20260919_afterhours_fixed.json"),
    ("W1-盘后 60日 宽闸门 (复算 §16.6 依据)", "data/reports/replay_ext_entry_sources_20260919_afterhours_loose60.json"),
    ("W2-盘前 Jan-Mar (独立窗口)", "data/reports/replay_ext_entry_sources_20260919_premarket_janmar.json"),
    ("W3-盘前 05-15→06-20 (DEB §16.9 第二窗口)", "data/reports/replay_ext_entry_sources_20260919_premarket_loose_win2.json"),
    ("W2-盘后 Jan-Mar (独立窗口)", "data/reports/replay_ext_entry_sources_20260919_ah_janmar.json"),
]
import os
FILES = [f for f in FILES if os.path.exists(f[1])]
SHOW = ("prod_like", "prod_like_925", "no_flatten", "no_flatten_gap", "rule", "read_0920", "pre_close", "a_cond", "b_band", "intraday_sl", "rth_close", "next_open", "sl_only")

for label, path in FILES:
    d = json.load(open(path))
    s = d["summary"]
    print("=" * 96)
    print(f"{label}\n  n={s.get('gates_passed')} 窗口={s.get('end')} days={s.get('days')} "
          f"固定样本对数={s.get('fixed_pairs')} 闸门漂移行={s.get('gates_drift_rows')} "
          f"MFE均值={s.get('avg_mfe_pct')}")
    arms = s.get("arms") or {}
    diag = s.get("arm_diagnostics") or {}
    pv = (s.get("paired_vs_base") or {})
    print(f"  {'臂':<12}{'n':>4}{'净均值':>9}{'中位':>9}{'最差':>9}{'最差5%':>9}{'胜率%':>7}"
          f"{'割负反转%':>10}{'捕获%':>8}{'vs基准Δ':>10}{'更好/平/更差':>14}{'t':>7}")
    for tag in SHOW:
        a = arms.get(tag)
        if not a:
            continue
        g = diag.get(tag, {})
        p = (pv.get("arms") or {}).get(tag, {})
        print(f"  {tag:<12}{a['n']:>4}{a['mean']:>9}{a['median']:>9}{a['worst']:>9}"
              f"{g.get('worst5_mean'):>9}{a['win_rate_pct']:>7}"
              f"{(g.get('reversal_after_cut_pct') if g.get('reversal_after_cut_pct') is not None else -1):>10}"
              f"{(g.get('capture_mean_pct') if g.get('capture_mean_pct') is not None else -1):>8}"
              f"{p.get('delta_mean_pp'):>10}{str(p.get('better'))+'/'+str(p.get('same'))+'/'+str(p.get('worse')):>14}"
              f"{str(p.get('t')):>7}")
    print(f"  配对基准 = {pv.get('base')}")
    sh = s.get("split_half")
    if sh:
        print(f"  分片 (split_day={sh['split_day']}):")
        for tag, v in sh["arms"].items():
            h1, h2 = v.get("h1"), v.get("h2")
            print(f"    {tag:<12} 前半 n={h1['n']} 净{h1['mean']} 最差{h1['worst']}   "
                  f"后半 n={h2['n']} 净{h2['mean']} 最差{h2['worst']}")
    print(f"  费用分档: {json.dumps(s.get('fee_bands'), ensure_ascii=False)}")