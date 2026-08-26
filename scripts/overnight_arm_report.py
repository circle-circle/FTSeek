#!/usr/bin/env python3
"""RFC-067 方案A: 隔夜臂精准统计报表 (2026-08-27, 用户裁"同意A").

数据源:
  1. 新台账 (8/27 起): trade_records.metadata_json.overnight_arm
     (_mark_overnight_arm 在 EOD 豁免点写入: night_n / source / 水深快照)
  2. 历史回填: date(entry)<date(exit) 的隔夜仓按 decision_id 前缀归源
     (intraday_reentry-* = 再入场臂; 其余 = DA/其他臂)

输出: 来源 × 夜数 × 最终结局 分组统计 + 修复率对比。只读, 幂等, 零副作用。
挂载: 人工按需 / 可加入盘后复盘链 (待数据积累后)。
"""
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"


def norm_et_date(ts: str) -> str:
    """trade_records 时间戳三格式混存 (记忆坑): 统一规范化到 ET 日期.
    naive=CST(UTC+8) → -12h 近似转 ET 日期; 带 -04:00/-05:00 后缀直接取日期; +00:00 → -4h."""
    if not ts:
        return ""
    s = str(ts)
    try:
        if s.endswith("+00:00") or s.endswith("Z"):
            from datetime import datetime, timedelta
            dt = datetime.fromisoformat(s.replace("Z", "+00:00")) - timedelta(hours=4)
            return dt.date().isoformat()
        if "-" == s[10:11] and (s.endswith("-04:00") or s.endswith("-05:00")):
            return s[:10]
        if s[10:11] in ("T", " "):
            # naive = CST → -12h
            from datetime import datetime, timedelta
            dt = datetime.fromisoformat(s) - timedelta(hours=12)
            return dt.date().isoformat()
        return s[:10]
    except Exception:
        return s[:10]


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, symbol, direction, decision_id, entry_time, exit_time,
               exit_reason, ROUND(COALESCE(pnl_net, realized_pnl), 2) pnl,
               metadata_json
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
          AND exit_time != '' AND exit_time IS NOT NULL
          AND entry_time >= '2026-06-01'
        ORDER BY entry_time
    """).fetchall()
    con.close()

    marked, hist = [], []
    for r in rows:
        md = {}
        try:
            md = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        except Exception:
            pass
        arm = md.get("overnight_arm")
        src = "reentry" if str(r["decision_id"] or "").startswith("intraday_reentry-") else "da_other"
        ed, xd = norm_et_date(r["entry_time"]), norm_et_date(r["exit_time"])
        if isinstance(arm, dict) and arm.get("night_n"):
            marked.append((r, arm, src))
        elif ed and xd and ed < xd:
            # 历史隔夜仓: 夜数按自然日差近似 (交易日口径待假日历精修, 报表用途足够)
            from datetime import date
            try:
                n = (date.fromisoformat(xd) - date.fromisoformat(ed)).days
            except Exception:
                n = 1
            hist.append((r, src, n))

    print(f"═══ RFC-067 隔夜臂报表 (6/1 起) ═══")
    print(f"\n【新台账 overnight_arm】(8/27 部署起累计) n={len(marked)}")
    for r, arm, src in marked:
        d = dict(r)
        print(f"  {d['symbol']:<9} {d['direction']:<5} src={arm.get('source'):<20} "
              f"night={arm.get('night_n')} 水深net={arm.get('underwater_pct_net')}% "
              f"归源={src} 结局={d['exit_reason']} ${d['pnl']}")

    print(f"\n【历史回填】隔夜仓 (entry日<exit日) n={len(hist)}")
    grp = defaultdict(lambda: [0, 0.0, 0])
    for r, src, n in hist:
        d = dict(r)
        g = grp[(src, "1夜" if n <= 1 else "多夜")]
        g[0] += 1
        g[1] += d["pnl"]
        g[2] += 1 if d["pnl"] > 0 else 0
    print(f"  {'来源':<10} {'夜数':<6} {'n':>3} {'净PnL':>9} {'胜率':>7}")
    for (src, nb), (cnt, pnl, wins) in sorted(grp.items()):
        print(f"  {src:<10} {nb:<6} {cnt:>3} {pnl:>+9.1f} {wins/cnt*100:>6.1f}%")

    # 预注册判据提示 (RFC-067)
    re_multi = [(r, n) for (r, src, n) in hist if src == "reentry" and n > 1]
    print(f"\n【预注册判据跟踪】再入场多夜样本 n={len(re_multi)} (判据: n≥10 且修复率<40% → 裁排除)")


if __name__ == "__main__":
    main()
