#!/usr/bin/env python3
"""IC周报 — 每周日盘后推送

打通最短反馈路径: 信号→outcome→IC→周报→人工决策

检查项:
1. IC汇总 (全局+按策略+按方向)
2. 胜率 (按策略×方向)
3. 强信号表现 (strength 50+ vs mid 30-50 vs weak <30)
4. OKR进度 (执行数/outcome数/距目标差距)
5. 策略健康建议 (哪些该禁用/哪些值得关注)

输出: 飞书推送
"""
import sys, os, sqlite3, json
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = Path(__file__).parent.parent
TODAY = datetime.now().strftime("%Y-%m-%d")

def get_ic_summary():
    """IC汇总"""
    try:
        from app.analysis.signal_ic_tracker import get_ic_summary as _get
        return _get(min_signals=30, days_back=30)
    except Exception as e:
        return {"status": "error", "error": str(e)}

def get_signal_stats():
    """信号统计 (按策略×方向×强度)"""
    try:
        db = sqlite3.connect(str(BASE / "data" / "signal_snapshots.db"))
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT strategy_id, direction,
                   CASE WHEN strength < 30 THEN 'weak(<30)'
                        WHEN strength < 50 THEN 'mid(30-50)'
                        ELSE 'strong(50+)' END as bucket,
                   COUNT(*) as n,
                   AVG(fwd_ret_t1) as avg_1d,
                   AVG(fwd_ret_t5) as avg_5d,
                   AVG(CASE WHEN fwd_ret_t1 > 0 THEN 1.0 ELSE 0.0 END) as hit_rate
            FROM signal_snapshots
            WHERE fwd_ret_t1 IS NOT NULL
            GROUP BY strategy_id, direction, bucket
            HAVING n >= 5
            ORDER BY strategy_id, direction, bucket
        """).fetchall()

        stats = []
        for r in rows:
            stats.append({
                "strategy": r["strategy_id"] or "unknown",
                "direction": r["direction"],
                "bucket": r["bucket"],
                "n": r["n"],
                "avg_1d": round(r["avg_1d"] or 0, 3),
                "avg_5d": round(r["avg_5d"] or 0, 3),
                "wr": round((r["hit_rate"] or 0) * 100, 1),
            })
        db.close()
        return stats
    except Exception as e:
        return [{"error": str(e)}]

def get_okr_progress():
    """OKR进度"""
    try:
        db = sqlite3.connect(str(BASE / "data" / "signal_snapshots.db"))
        db.row_factory = sqlite3.Row

        # 总信号数
        total_signals = db.execute("SELECT COUNT(*) as n FROM signal_snapshots").fetchone()["n"]
        # 有forward_return的
        filled = db.execute("SELECT COUNT(*) as n FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL").fetchone()["n"]
        # 最近7天信号
        week_signals = db.execute(
            "SELECT COUNT(*) as n FROM signal_snapshots WHERE trade_date >= date('now', '-7 days')"
        ).fetchone()["n"]
        # 最近7天已回填
        week_filled = db.execute(
            "SELECT COUNT(*) as n FROM signal_snapshots WHERE fwd_ret_t1 IS NOT NULL AND trade_date >= date('now', '-7 days')"
        ).fetchone()["n"]

        db.close()
        return {
            "total_signals": total_signals,
            "filled": filled,
            "fill_rate": round(filled / max(total_signals, 1) * 100, 1),
            "week_signals": week_signals,
            "week_filled": week_filled,
            "okr_target_30": "KR1.3: 需≥30笔有outcome",
            "okr_gap": max(30 - filled, 0),
        }
    except Exception as e:
        return {"error": str(e)}

def generate_report():
    """生成周报"""
    ic = get_ic_summary()
    stats = get_signal_stats()
    okr = get_okr_progress()

    lines = [f"📊 IC周报 | {TODAY}\n"]

    # 1. OKR进度
    lines.append("── OKR进度 ──")
    if "error" not in okr:
        lines.append(f"  信号总量: {okr['total_signals']} (本周+{okr['week_signals']})")
        lines.append(f"  已回填: {okr['filled']} ({okr['fill_rate']}%) (本周+{okr['week_filled']})")
        lines.append(f"  KR1.3差距: 还需{okr['okr_gap']}笔有outcome (目标≥30)")
    else:
        lines.append(f"  错误: {okr['error']}")

    # 2. IC汇总
    lines.append("\n── IC汇总 (30天) ──")
    if ic.get("status") == "ok":
        lines.append(f"  全局IC: {ic['ic_mean']:.4f} (p={ic.get('ic_p_value','?'):.4f}) n={ic['n_signals']}")
        lines.append(f"  按策略:")
        for s, v in ic.get("by_strategy", {}).items():
            emoji = "✅" if v["ic"] > 0 else "🔴" if v["ic"] < -0.05 else "🟡"
            lines.append(f"    {emoji} {s}: IC={v['ic']:.4f} p={v['pval']:.3f} n={v['n']}")
        lines.append(f"  按方向:")
        for d, v in ic.get("by_direction", {}).items():
            emoji = "✅" if v["ic"] > 0 else "🔴"
            lines.append(f"    {emoji} {d}: IC={v['ic']:.4f} n={v['n']}")
    elif ic.get("status") == "insufficient_data":
        lines.append(f"  数据不足: {ic.get('n_signals',0)}/{ic.get('min_required',30)}")
    else:
        lines.append(f"  错误: {ic.get('error','unknown')}")

    # 3. 按策略×方向×强度
    lines.append("\n── 策略×方向×强度 ──")
    for s in stats:
        if "error" in s:
            lines.append(f"  错误: {s['error']}")
            continue
        emoji = "✅" if s["avg_5d"] > 0.02 else "🔴" if s["avg_5d"] < -0.02 else "🟡"
        lines.append(f"  {emoji} {s['strategy'][:20]:<20} {s['direction']:<6} {s['bucket']:<10} n={s['n']:>4} 1d={s['avg_1d']:+.2f}% 5d={s['avg_5d']:+.2f}% WR={s['wr']:.0f}%")

    # 4. 建议
    lines.append("\n── 策略建议 ──")
    suggestions = []
    if ic.get("status") == "ok":
        for s, v in ic.get("by_strategy", {}).items():
            if v["ic"] < -0.05 and v["n"] >= 20:
                suggestions.append(f"  🔴 {s}: IC={v['ic']:.4f} 持续负IC, 建议禁用或降权")
            elif v["ic"] > 0.05 and v["n"] >= 20:
                suggestions.append(f"  ✅ {s}: IC={v['ic']:.4f} 正IC, 建议关注")
        for d, v in ic.get("by_direction", {}).items():
            if v["ic"] < -0.05 and v["n"] >= 20:
                suggestions.append(f"  🔴 {d}方向: IC={v['ic']:.4f} 负IC, 信号反指标")
    if not suggestions:
        suggestions.append("  (样本不足, 暂无建议)")
    lines.extend(suggestions)

    return "\n".join(lines)

def main():
    report = generate_report()
    print(report)

    # 飞书推送
    try:
        from app.notifier.feishu import send_text as feishu_send
        feishu_send(report)
        print("\n✅ 已推送飞书")
    except Exception as e:
        print(f"\n🟡 飞书推送失败: {e}")

if __name__ == "__main__":
    main()
