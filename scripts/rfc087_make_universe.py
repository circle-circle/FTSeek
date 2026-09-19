"""从既有产物裁出"固定样本宇宙"文件 (只含成样笔的 (日,标的)), 供 --universe-from 重跑。

用途: 补/换臂时保证样本逐笔相同 (配对), 且只拉涉及的标的 (接口预算与时间省一个量级)。
"""
import json
import sys

PAIRS = [
    ("replay_ext_entry_sources_20260919_premarket_loose60.json", "_rfc087_univ_loose60.json"),
    ("replay_ext_entry_sources_20260919_premarket_loose_win2.json", "_rfc087_univ_win2.json"),
    ("replay_ext_entry_sources_20260919_afterhours_loose60.json", "_rfc087_univ_ah_loose60.json"),
]
for src, dst in PAIRS:
    p = f"data/reports/{src}"
    d = json.load(open(p))
    tr = d["trades"]
    out = {"summary": dict(d["summary"]), "trades": tr, "all_candidates": tr}
    out["summary"]["trimmed_for"] = "RFC-087 补臂重跑: 只在成样笔的标的集上重算"
    dst_path = f"data/reports/{dst}"
    json.dump(out, open(dst_path, "w"), ensure_ascii=False, indent=1)
    print(f"{dst_path}: {len(tr)} 笔 / {len({r['symbol'] for r in tr})} 只 / "
          f"session={out['summary'].get('session')} end={out['summary'].get('end')} "
          f"days={out['summary'].get('days')} min_chg={out['summary'].get('min_chg')} "
          f"min_vol={out['summary'].get('min_vol')}")