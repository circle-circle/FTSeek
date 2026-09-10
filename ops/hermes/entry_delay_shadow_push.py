#!/usr/bin/env python3
"""D2' 入场确认门影子每日推送 (no_agent cron script, 2026-09-11 用户裁「要」).

读结算产物 (05:58 CST crontab entry_delay_confirm_shadow.py 已落盘):
  - data/shadow/entry_delay_confirm.jsonl   逐笔反事实 (幂等键 trade_id+delay_min)
  - data/shadow/entry_delay_confirm_latest.json  汇总+预注册判据
  - data/ftseek_us.db (mode=ro)  trade_records.mfe_pct → 墓地捕获 (MFE<0.3%)

输出 5-7 行中文报告到 stdout (飞书推送)。record-only 影子的消费面:
每日亏损对比 (回避省$ vs 照进多付$) + forward 攒样进度 + 毕业判据状态。
结算文件陈旧 (>26h) → 推警告行 (传感器失效可见化, 8/28 教训)。
本脚本零写入 (DB ro + 不碰 jsonl), 失败时输出错误行而非静默。
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
JSONL = REPO / "data/shadow/entry_delay_confirm.jsonl"
LATEST = REPO / "data/shadow/entry_delay_confirm_latest.json"
DB = REPO / "data/ftseek_us.db"

GRAVE_MFE = 0.3          # 墓地线: MFE<0.3% = 入场即错族
FORWARD_START = "2026-09-09"  # DEB-20260909 影子落地日, forward 攒样窗起点 (裁决日以 forward 冻结值为准)
STALE_HOURS = 26         # 结算 cron 05:58, 推送 06:10; >26h = 昨晨结算缺失


def _fmt_usd(v: float) -> str:
    return f"${v:+,.0f}"


def main() -> int:
    now = datetime.now()
    lines = [f"🔬 入场确认门影子(D2') 日报 {now:%m-%d}"]

    # ── 新鲜度门 ──
    if not LATEST.exists() or not JSONL.exists():
        print("\n".join(lines + ["⚠️ 影子产物缺失 — 结算链未跑, 查 crontab 58 5 行"]))
        return 0
    latest = json.loads(LATEST.read_text())
    gen_at = datetime.fromisoformat(latest["generated_at"].replace("Z", "+00:00").split("+")[0])
    age_h = (now - gen_at).total_seconds() / 3600
    if age_h > STALE_HOURS:
        print("\n".join(lines + [
            f"⚠️ 结算陈旧: latest.json 生成于 {gen_at:%m-%d %H:%M} ({age_h:.0f}h 前)",
            "→ 查 logs/entry_delay_confirm.log 与 crontab 58 5 * * 2-6 行"]))
        return 0

    rows = [json.loads(l) for l in JSONL.open() if l.strip()]
    r2 = [r for r in rows if r.get("delay_min") == 2 and r.get("status") == "ok"]
    if not r2:
        print("\n".join(lines + ["⚠️ jsonl 无 N=2 ok 行"]))
        return 0

    # ── 昨日批次 (最近一个已结算交易日) ──
    batch_day = max(r["date"] for r in r2)
    day = [r for r in r2 if r["date"] == batch_day]
    d_av = [r for r in day if not r.get("still_triggers")]
    d_dl = [r for r in day if r.get("still_triggers")]
    saved = -sum(r.get("pnl_net") or 0 for r in d_av)
    opp = sum((r.get("opp_cost_pct") or 0) / 100 * (r.get("notional") or 0) for r in d_dl)
    lines.append(f"结算日 {batch_day}: n={len(day)} | 回避 {len(d_av)} 笔(净省{_fmt_usd(saved)})"
                 f" / 照进 {len(d_dl)} 笔(多付{_fmt_usd(opp)}) → 假想差 {_fmt_usd(saved - opp)}")
    if not day:
        lines[-1] += " (零成交日)"

    # ── 墓地捕获 (MFE<0.3%, 需 join trade_records) ──
    try:
        db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        mfe = dict(db.execute("SELECT id, mfe_pct FROM trade_records WHERE mfe_pct IS NOT NULL"))
        db.close()
        day_g = [r for r in day if (mfe.get(r["trade_id"]) or 99) < GRAVE_MFE]
        g_av = [r for r in day_g if not r.get("still_triggers")]
        g_saved = -sum(r.get("pnl_net") or 0 for r in g_av)
        lines.append(f"墓地捕获(MFE<0.3%): 当日 {len(day_g)} 笔, 门回避 {len(g_av)} 笔"
                     + (f" (省{_fmt_usd(g_saved)})" if g_av else ""))
    except Exception as e:  # noqa: BLE001
        lines.append(f"墓地捕获: join 失败 ({e})")

    # ── forward 攒样进度 (裁决口径) ──
    fwd = [r for r in r2 if r["date"] >= FORWARD_START]
    f_av = [r for r in fwd if not r.get("still_triggers")]
    f_dl = [r for r in fwd if r.get("still_triggers")]
    if fwd:
        av_rate = len(f_av) / len(fwd) * 100
        f_opp = (sum(r.get("opp_cost_pct") or 0 for r in f_dl) / len(f_dl)) if f_dl else 0.0
        f_saved = -sum(r.get("pnl_net") or 0 for r in f_av)
        f_opp_usd = sum((r.get("opp_cost_pct") or 0) / 100 * (r.get("notional") or 0) for r in f_dl)
        lines.append(
            f"forward({FORWARD_START[5:]}起): n={len(fwd)}/20 | 回避率 {av_rate:.0f}%(门≥30%)"
            f" | 机会成本 {f_opp:.3f}%(门<0.3%) | 净差 {_fmt_usd(f_saved - f_opp_usd)}")
    else:
        lines.append(f"forward({FORWARD_START[5:]}起): n=0/20 攒样中")

    # ── 全窗判据 (latest.json 冻结值, 仅作参照) ──
    grad = latest.get("graduation", {}).get("2", {})
    g2 = latest.get("delays", {}).get("2", {}).get("ALL", {})
    lines.append(
        f"全窗参照: n={g2.get('n', '?')} 回避率 {(g2.get('avoidance_rate') or 0) * 100:.0f}%"
        f" 成本 {g2.get('opp_cost_mean_pct', '?')}% | 判据 ALL={'✅' if grad.get('ALL_pass') else '❌'}"
        f" SL_INIT={'✅' if grad.get('SL_INITIAL_pass') else '❌'}")
    lines.append("record-only 零交易影响 | 裁决日按 forward 冻结值提请 (RFC-081)")

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"🔬 入场确认门影子日报: 脚本异常 {exc!r}")
        sys.exit(0)
