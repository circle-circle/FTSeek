#!/usr/bin/env python3
"""RFC-067 方案A: 隔夜臂精准统计报表 (2026-08-27, 用户裁"同意A").

数据源:
  1. 新台账 (8/27 起): trade_records.metadata_json.overnight_arm
     (_mark_overnight_arm 在 EOD 豁免点写入: night_n / source / 水深快照)
  2. 历史回填: date(entry)<date(exit) 的隔夜仓按 decision_id 前缀归源
     (intraday_reentry-* = 再入场臂; 其余 = DA/其他臂)

输出: 来源 × 夜数 × 最终结局 分组统计 + 修复率对比。只读, 幂等, 零副作用。
挂载: cron 20 6 * * 2-6 (2026-09-01 复查收口补挂 — 判罪线此前只在 stdout 里,
对周检/哨兵等一切自动化不可见; 注册表 overnight_kill_criterion / 判据字典 B15)。
判罪线进度同步落盘 data/shadow/overnight_arm_report_latest.json (顶层 n=已平仓数,
注册表计数锚; n≥20 时周检自动翻 READY_TO_EVALUATE 提请裁决)。
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
OUT_JSON = REPO / "data" / "shadow" / "overnight_arm_report_latest.json"


def norm_et_date(ts: str) -> str:
    """trade_records 时间戳三格式混存 (记忆坑): 统一规范化到 ET 日期.

    2026-08-27 修复 (审计 F1): 旧实现 ET 分支判据 `"-" == s[10:11]` 对 ISO "T" 分隔
    格式 (now_et().isoformat() 产出的 "2026-08-27T10:30:00-04:00") 永远 False
    (s[10]=='T'), 致所有 R-12 后 ET-aware 时间戳掉进 naive 分支被当 CST 再减 12h,
    ET 日期错前一天 (夜数 +1, 同日仓误判隔夜). 改为 fromisoformat 真解析:
    aware → astimezone(ET) 取日期; naive → 视作服务器本地 CST 再转 ET."""
    if not ts:
        return ""
    s = str(ts)
    try:
        from datetime import datetime, timezone, timedelta
        _ET = timezone(timedelta(hours=-4))  # 兜底固定 EDT 近似
        try:
            from zoneinfo import ZoneInfo
            _ET = ZoneInfo("America/New_York")
        except Exception:
            pass  # zoneinfo 不可用时保留上行近似 (DST 边界日 ±1h, 仅 00:00-01:00 ET 窗日期归属受影响)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # naive = 服务器本地 CST(UTC+8)
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.astimezone(_ET).date().isoformat()
    except Exception:
        # 兜底: 纯日期前缀 (无法解析时不误伤)
        return s[:10]


def kill_criterion_state(closed_n: int, repair_rate_pct: float | None) -> str:
    """w_week 臂判罪线机械判定 (2026-08-29 用户裁"5批" 预注册, 判据字典 B15):

    已平仓 n≥20 且修复率(结局 pnl>0)<40% → KILL_LINE_HIT (提请裁排除
    loser_overnight_enabled); n≥20 且≥40% → REPAIR_RATE_OK (维持); 其余攒样。
    未满 n=20 前只报数不动作 (防小样本冲动裁决, 与再入场臂判据同法)。"""
    if closed_n < 20 or repair_rate_pct is None:
        return "KEEP_SAMPLING"
    return "KILL_LINE_HIT" if repair_rate_pct < 40 else "REPAIR_RATE_OK"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT id, symbol, direction, decision_id, entry_time, exit_time,
               exit_reason, ROUND(COALESCE(pnl_net, realized_pnl), 2) pnl,
               metadata_json
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
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
        _holding = not d.get("exit_time")
        _tail = "持仓中" if _holding else f"结局={d['exit_reason']} ${d['pnl']}"
        print(f"  {d['symbol']:<9} {d['direction']:<5} src={arm.get('source'):<20} "
              f"night={arm.get('night_n')} 水深net={arm.get('underwater_pct_net')}% "
              f"归源={src} {_tail}")

    print(f"\n【历史回填】隔夜仓 (entry日<exit日, 仅已平仓) n={len(hist)}")
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

    # 预注册判据跟踪 (2026-08-29, 用户裁"5批"): w_week 臂生产判罪线 —
    #   已平仓 n≥20 且修复率(结局 pnl>0)<40% → 裁排除 loser_overnight_enabled。
    #   用途: 检验 8/6 裁决依据 (回放 n=15, 修复率73%, W_WEEK +$629) 是否 OOS 成立;
    #   未满 n=20 前只报数不动作 (防小样本冲动裁决, 与再入场臂判据同法)。
    ww = [(r, a) for (r, a, src) in marked if "w_week" in str(a.get("source", ""))]
    ww_closed = [r for (r, a) in ww if r["exit_time"]]
    ww_rec = sum(1 for r in ww_closed if (r["pnl"] or 0) > 0)
    ww_pnl = sum((r["pnl"] or 0) for r in ww_closed)
    _rate = (ww_rec / len(ww_closed) * 100) if ww_closed else None
    state = kill_criterion_state(len(ww_closed), _rate)
    if ww_closed:
        _verdict = ("⚠️ 已达判罪线, 提请裁决" if state == "KILL_LINE_HIT"
                    else "攒样中" if len(ww_closed) < 20 else "修复率达标")
        print(f"【预注册判据跟踪】w_week 臂已平 n={len(ww_closed)} 修复率={_rate:.0f}% "
              f"净PnL=${ww_pnl:+.1f} → {_verdict} (判据: n≥20 且修复率<40% → 裁排除)")
    else:
        print(f"【预注册判据跟踪】w_week 臂已平 n=0 (台账 {len(ww)} 笔持仓中) "
              f"(判据: n≥20 且修复率<40% → 裁排除)")

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n": len(ww_closed),  # 注册表计数锚 (min_n=20)
        "w_week": {"holding": len(ww) - len(ww_closed), "closed": len(ww_closed),
                   "repaired": ww_rec,
                   "repair_rate_pct": round(_rate, 1) if _rate is not None else None,
                   "net_pnl": round(ww_pnl, 2)},
        "criterion": "w_week 臂已平 n≥20 且修复率<40% → 裁排除 loser_overnight_enabled "
                     "(2026-08-29 用户裁'5批' 预注册; 判据字典 B15)",
        "verdict": state,
        "marked_total": len(marked), "hist_total": len(hist),
        "reentry_multi_n": len(re_multi),
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n[overnight-arm] 判罪线进度已落盘 {OUT_JSON.relative_to(REPO)}: "
          f"n={report['n']}/20 修复率={report['w_week']['repair_rate_pct']}% "
          f"verdict={state}")


if __name__ == "__main__":
    main()
