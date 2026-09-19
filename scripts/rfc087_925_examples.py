"""RFC-087 通俗说明用: 920→925 到底改了哪些笔、改了多少钱 (只读产物)."""
import json

for label, path in (("紧口径 n=56", "data/reports/replay_ext_entry_sources_20260919_premarket_fixed.json"),
                    ("宽口径 n=345", "data/reports/replay_ext_entry_sources_20260919_premarket_loose60.json")):
    try:
        d = json.load(open(path))
    except Exception:
        continue
    print("=" * 88)
    print(label)
    rows = []
    for r in d["trades"]:
        a = (r.get("prod_like") or {}).get("net_pct")
        b = (r.get("prod_like_925") or {}).get("net_pct")
        if a is None or b is None:
            continue
        rows.append((b - a, r["day"], r["symbol"], (r.get("read_0920") or {}).get("net_pct"),
                     (r.get("pre_close") or {}).get("net_pct"), a, b))
    same = [x for x in rows if abs(x[0]) <= 0.001]
    diff = [x for x in rows if abs(x[0]) > 0.001]
    print(f"  总 {len(rows)} 笔: 同判 {len(same)} 笔, 有差异 {len(diff)} 笔")
    diff.sort(reverse=True)
    print("  差异最大的 5 笔 (晚 5 分钟平 = 变好):")
    for x in diff[:5]:
        print(f"    {x[1]} {x[2]:<9} 09:20 读数 {x[3]:>7}% → 09:25 现价 {x[4]:>7}%  ⇒  现役 {x[5]:>7}% → 后移 {x[6]:>7}%  (差 {x[0]:+.3f}pp)")
    print("  差异最负的 3 笔 (晚 5 分钟平 = 变差):")
    for x in diff[-3:]:
        print(f"    {x[1]} {x[2]:<9} 09:20 读数 {x[3]:>7}% → 09:25 现价 {x[4]:>7}%  ⇒  现役 {x[5]:>7}% → 后移 {x[6]:>7}%  (差 {x[0]:+.3f}pp)")
    if diff:
        tot = sum(x[0] for x in diff)
        print(f"  差异合计 {tot:+.3f}pp / 均摊到全样本 {tot/len(rows):+.4f}pp/笔")
        print(f"  $5000 名义下: 差异合计 ≈ ${tot/100*5000:+.2f} / 均摊 ≈ ${tot/len(rows)/100*5000:+.2f} 每笔")