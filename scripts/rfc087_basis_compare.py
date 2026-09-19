"""对比: 9/17 收口裁决依据的原始(污染)产物 vs 修复后复算 (只读)."""
import glob
import json
import os

OLD = [
    "data/reports/replay_ext_entry_sources_latest_premarket_feeaware.json",
    "data/reports/replay_ext_entry_sources_latest_afterhours_feeaware.json",
]
NEW = [
    "data/reports/replay_ext_entry_sources_20260919_premarket_loose60.json",
    "data/reports/replay_ext_entry_sources_20260919_afterhours_loose60.json",
]
ARMS = ("rule", "pre_close", "rth_close", "intraday_sl", "next_open", "sl_only")


def show(path, tag):
    if not os.path.exists(path):
        print(f"  {tag}: (不存在) {path}")
        return
    s = json.load(open(path))["summary"]
    print(f"  {tag}: session={s.get('session')} n={s.get('gates_passed')} end={s.get('end')} "
          f"days={s.get('days')} min_chg={s.get('min_chg')} min_vol={s.get('min_vol')} "
          f"MFE={s.get('avg_mfe_pct')} 闸门漂移行={s.get('gates_drift_rows')}")
    a = s.get("arms") or {}
    for t in ARMS:
        if t in a:
            print(f"      {t:<12} 净均值 {a[t]['mean']:>7} 中位 {a[t]['median']:>7} "
                  f"最差 {a[t]['worst']:>8} n={a[t]['n']}")


print("=== 旧(污染)产物 ===")
for p in OLD:
    show(p, os.path.basename(p))
print("=== 修复后复算 ===")
for p in NEW:
    show(p, os.path.basename(p))
print("=== 现有 RFC-087 产物清单 ===")
for p in sorted(glob.glob("data/reports/replay_ext_entry_sources_20260919_*.json")):
    print("   ", p)