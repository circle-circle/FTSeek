#!/usr/bin/env python3
"""每日 LLM token / 成本消耗统计推送 (2026-07-23)。

回答: 昨日各 LLM 任务 (feature) 与各模型烧了多少 token / 多少钱 / 调了多少次 /
      成功率与延迟如何 —— 供判断"哪个任务该优化 prompt / 换更便宜模型 / 关闭"。

数据源: data/llm_audit.db 表 llm_calls (SSOT; 由 app/llm/model_call.py 每次调用落库)。
  注: 2026-07-23 修复了 _record_audit 参数名 bug (cost/error → cost_usd/error_msg),
      此前审计写入被静默吞掉, token 全为 0; 修复后数据方才有效。

设计:
  - 只读 DB, 零 live 行为变更。
  - 盘后统计前一日 (LLM 调用跨 ET/CST 午夜, 用 --days-back 对齐)。
  - 落盘 data/llm_token_daily_YYYY-MM-DD.json (单日档) 供离线复盘。
  - 失败安全: DB / 推送异常都吞掉, 绝不抛错阻断 cron。
运行: PYTHONPATH=. ./venv/bin/python scripts/daily_llm_token_report.py [--date YYYY-MM-DD] [--push] [--dry]
"""
import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "llm_audit.db"

# 自含 dotenv 加载: 飞书 webhook 在 .env.us, 保证 cron 下也能拿到
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass


def _fmt_usd(v: float) -> str:
    if v >= 1:
        return f"${v:.2f}"
    if v >= 0.01:
        return f"${v:.3f}"
    return f"${v*1000:.2f}m"  # 毫美元, 避免一堆 $0.000


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _load_budget() -> dict:
    """读 yaml llm_budget 配置 (daily_max_usd / warn_pct / halt_pct). 失败返回默认."""
    default = {"daily_max_usd": 5.0, "warn_pct": 0.7, "halt_pct": 1.0}
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "unified_config.yaml").read_text(encoding="utf-8"))
        b = (cfg or {}).get("llm_budget", {}) or {}
        alerts = b.get("cost_alerts", {}) or {}
        return {
            "daily_max_usd": float(b.get("daily_max_usd", default["daily_max_usd"])),
            "warn_pct": float(alerts.get("warn_pct", default["warn_pct"])),
            "halt_pct": float(alerts.get("halt_pct", default["halt_pct"])),
        }
    except Exception:
        return default


def _query_day(conn: sqlite3.Connection, day: str) -> dict:
    """聚合某一自然日 (localtime) 的 LLM 调用. 返回 {total, by_feature, by_model}.

    仅生产流量: 排除 is_test=1 测试行 (2026-08-27 测试流量隔离)."""
    where = "date(timestamp,'unixepoch','localtime')=? AND COALESCE(is_test,0)=0"
    rows = conn.execute(
        f"SELECT feature, model, tokens_in, tokens_out, cost_usd, latency_ms, success, error_msg "
        f"FROM llm_calls WHERE {where}",
        (day,),
    ).fetchall()

    total = {"calls": 0, "ok": 0, "tok_in": 0, "tok_out": 0, "cost": 0.0, "lat_sum": 0.0, "trunc": 0}
    by_feature = defaultdict(lambda: {"calls": 0, "ok": 0, "tok_in": 0, "tok_out": 0,
                                      "cost": 0.0, "lat_sum": 0.0, "trunc": 0})
    by_model = defaultdict(lambda: {"calls": 0, "ok": 0, "tok_in": 0, "tok_out": 0, "cost": 0.0, "trunc": 0})

    for feat, model, ti, to, cost, lat, ok, err in rows:
        ti = int(ti or 0); to = int(to or 0); cost = float(cost or 0)
        lat = float(lat or 0); ok = int(ok or 0)
        # 2026-08-01 (failure DEB Q4 验收): 截断可查询 — B① 落审计的 error_msg 标记
        trunc = 1 if "finish_reason=length" in (err or "") else 0
        for bucket in (total, by_feature[feat or "?"], by_model[model or "?"]):
            bucket["calls"] += 1
            bucket["ok"] += ok
            bucket["tok_in"] += ti
            bucket["tok_out"] += to
            bucket["cost"] += cost
            bucket["trunc"] += trunc
        total["lat_sum"] += lat
        by_feature[feat or "?"]["lat_sum"] += lat

    return {
        "date": day,
        "total": total,
        "by_feature": {k: dict(v) for k, v in by_feature.items()},
        "by_model": {k: dict(v) for k, v in by_model.items()},
    }


def _query_trunc_7d(conn: sqlite3.Connection, day: str) -> list:
    """7 日窗口 per-feature 截断率 (failure DEB Q4 预注册验收: >5% 且 n≥10 → max_tokens 复核)."""
    rows = conn.execute(
        "SELECT feature, COUNT(*), SUM(CASE WHEN error_msg LIKE '%finish_reason=length%' THEN 1 ELSE 0 END) "
        "FROM llm_calls WHERE date(timestamp,'unixepoch','localtime') BETWEEN date(?, '-6 days') AND date(?) "
        "AND COALESCE(is_test,0)=0 "
        "GROUP BY feature",
        (day, day),
    ).fetchall()
    out = []
    for feat, n, tr in rows:
        n = int(n or 0); tr = int(tr or 0)
        if n >= 10 and tr > 0:
            out.append({"feature": feat or "?", "calls": n, "trunc": tr, "rate": tr / n})
    return sorted(out, key=lambda x: x["rate"], reverse=True)


def _build_message(today: dict, prev: dict, trunc_7d: list | None = None) -> str:
    t = today["total"]
    calls = t["calls"]
    if calls == 0:
        return f"🪙 LLM 消耗日报 {today['date']}\n(当日无 LLM 调用记录)"

    tok_total = t["tok_in"] + t["tok_out"]
    ok_rate = (t["ok"] / calls * 100) if calls else 0
    avg_lat = (t["lat_sum"] / calls / 1000) if calls else 0  # 秒

    # 环比昨日成本
    pv = prev["total"]["cost"] if prev else 0
    delta = ""
    if pv > 0:
        pct = (t["cost"] - pv) / pv * 100
        arrow = "▲" if pct >= 0 else "▼"
        delta = f" ({arrow}{abs(pct):.0f}% vs 昨)"

    # 预算告警 (只提示不拦截): 当日成本占 daily_max_usd 的比例
    budget = _load_budget()
    cap = budget["daily_max_usd"]
    alert_line = None
    if cap > 0:
        used_pct = t["cost"] / cap
        if used_pct >= budget["halt_pct"]:
            alert_line = f"🔴 超预算! 当日 {_fmt_usd(t['cost'])} / 上限 {_fmt_usd(cap)} ({used_pct*100:.0f}%)"
        elif used_pct >= budget["warn_pct"]:
            alert_line = f"🟡 接近预算 当日 {_fmt_usd(t['cost'])} / 上限 {_fmt_usd(cap)} ({used_pct*100:.0f}%)"

    lines = []
    if alert_line:
        lines.append(alert_line)
    lines += [
        f"🪙 LLM 消耗日报 {today['date']}",
        f"总计: {calls} 次调用 | {_fmt_tok(tok_total)} tok | {_fmt_usd(t['cost'])}{delta}",
        f"成功率 {ok_rate:.0f}% | 均延迟 {avg_lat:.1f}s",
        "",
        "── 按任务 (Top 成本) ──",
    ]

    feats = sorted(today["by_feature"].items(), key=lambda kv: kv[1]["cost"], reverse=True)
    for feat, s in feats[:10]:
        tok = s["tok_in"] + s["tok_out"]
        okr = (s["ok"] / s["calls"] * 100) if s["calls"] else 0
        flag = " ⚠️" if okr < 80 else ""
        trunc_s = f" 截断{s['trunc']/s['calls']*100:.0f}%" if s["calls"] and s["trunc"] else ""
        lines.append(f"{feat}: {s['calls']}次 {_fmt_tok(tok)}tok {_fmt_usd(s['cost'])} 成功{okr:.0f}%{trunc_s}{flag}")

    lines.append("")
    lines.append("── 按模型 ──")
    models = sorted(today["by_model"].items(), key=lambda kv: kv[1]["cost"], reverse=True)
    for model, s in models:
        tok = s["tok_in"] + s["tok_out"]
        lines.append(f"{model}: {s['calls']}次 {_fmt_tok(tok)}tok {_fmt_usd(s['cost'])}")

    if trunc_7d:
        lines.append("")
        lines.append("── 截断率 (近7日, finish_reason=length) ──")
        for r in trunc_7d:
            mark = " 🔴>5% 复核 max_tokens" if r["rate"] > 0.05 else ""
            lines.append(f"{r['feature']}: {r['trunc']}/{r['calls']} ({r['rate']*100:.1f}%){mark}")

    return "\n".join(lines)


def main(date=None, days_back=1, push=False, dry=False):
    if not DB_PATH.exists():
        print(f"[warn] 审计库不存在: {DB_PATH}")
        return

    if date:
        target = date
    else:
        target = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    prev_day = (datetime.strptime(target, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        today = _query_day(conn, target)
        prev = _query_day(conn, prev_day)
        trunc_7d = _query_trunc_7d(conn, target)
    finally:
        conn.close()

    msg = _build_message(today, prev, trunc_7d)
    print("=" * 60)
    print(msg)
    print("=" * 60)

    # 落盘单日档
    try:
        out = DATA / f"llm_token_daily_{target}.json"
        out.write_text(json.dumps(today, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] {out}")
    except Exception as e:
        print(f"[warn] 落盘失败: {e}")

    if push and not dry:
        try:
            from app.notifier.feishu import send_text
            send_text(msg)
            print("[pushed] 飞书")
        except Exception as e:
            print(f"[warn] 推送失败: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="每日 LLM token/成本统计推送 (只读)")
    ap.add_argument("--date", help="指定统计日 YYYY-MM-DD (默认: 今日往前 days-back 天)")
    ap.add_argument("--days-back", type=int, default=1, help="盘后统计前 N 日 (默认1)")
    ap.add_argument("--push", action="store_true", help="推送飞书")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    a = ap.parse_args()
    main(date=a.date, days_back=a.days_back, push=a.push, dry=a.dry)
