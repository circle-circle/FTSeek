#!/usr/bin/env python3
"""DA 链路日检 (只读, 2026-07-10 · P0 观测)。

回答: 某交易日 DA 链是否按预期工作 —
  1. 生成: 当日 playbook_instructions 几点生成 / 多少条 / 是否盘前 / 方向分布
  2. 执行: 当日成交按 strategy_id (playbook_sim=DA vs technical_fallback=兜底 vs manual)
  3. 判定: DA_ACTIVE / FALLBACK / NO_PLAN / GENERATED_NOT_FILLED
  4. 闸门: signal_gate BLOCK/WARN/PASS 分布 + 拦截率 (定位"指令量骤降")

零 live 行为变更, 纯读 DB。供"跑通+收集+检验"用。
运行: PYTHONPATH=. ./venv/bin/python scripts/da_chain_health_report.py [--date YYYY-MM-DD] [--days N] [--push]
"""
import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage.db_pool import get_db
from app.utils.timezone import ET
# 2026-08-26: 盘前判定改分钟级 (小时==9 时须比分钟), 常量由"小时"改为"开盘分钟数"
OPEN_ET_MIN = 9 * 60 + 30  # 开盘 ET 09:30; created_at(UTC) 换算的 ET 时刻早于该值视为盘前
SNAP_DB = Path(__file__).resolve().parents[1] / "data" / "signal_snapshots.db"


def _today_et():
    return datetime.now(ET).strftime("%Y-%m-%d")


def _et_min(utc_ts):
    """created_at 形如 'YYYY-MM-DDTHH:MM'(UTC 字符串) → 当日 ET 分钟数 (0-1439) 或 None。
    2026-08-26: 新增分钟精度辅助, 盘前判定需区分 09:00-09:29 与 09:30 后;
    换算按真实 ET 偏移 (pytz/zoneinfo localize 后 astimezone), 不再硬编码 -4。"""
    try:
        dt = datetime.fromisoformat(str(utc_ts)[:19]).replace(tzinfo=timezone.utc)
        et = dt.astimezone(ET)
        return et.hour * 60 + et.minute
    except Exception:
        return None


def _et_hour(utc_ts):
    """created_at 形如 'YYYY-MM-DDTHH:MM:...'(UTC) → ET 小时(int) 或 None。
    2026-08-26: 原 (h-4)%24 硬编码只对 EDT 成立, 冬令时(EST,-5)全部偏 1 小时;
    改为按该 UTC 时间戳对应日期的真实 ET 偏移换算 (复用 _et_min)。"""
    m = _et_min(utc_ts)
    return None if m is None else m // 60


def _gate_stats(date):
    """DA 信号在 signal_gate 的判定分布 (只读 signal_snapshots.db)。
    取 trade_date=date 且 decision_id 前缀 DA- 的行, 统计 factors_json.gate_decision。
    BLOCK=拦截丢弃(不写库) / WARN=降级放行 / 其它=直通。拦截率=BLOCK/total。"""
    stats = {"total": 0, "BLOCK": 0, "WARN": 0, "PASS": 0}
    if not SNAP_DB.exists():
        return stats
    try:
        con = sqlite3.connect(f"file:{SNAP_DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT factors_json FROM signal_snapshots "
            "WHERE decision_id LIKE 'DA-%' AND trade_date=?", (date,)).fetchall()
        con.close()
    except Exception:
        return stats
    for (fj,) in rows:
        try:
            f = json.loads(fj) if fj else {}
        except Exception:
            f = {}
        if not isinstance(f, dict):
            continue
        g = f.get("gate_decision")
        if g is None:
            continue
        stats["total"] += 1
        stats[g] = stats.get(g, 0) + 1
    return stats


def _report_date(c, date):
    # 1) 生成
    rows = list(c.execute(
        "SELECT direction, created_at, confidence FROM playbook_instructions WHERE date=?", (date,)))
    dir_cnt = Counter(r[0] for r in rows)
    actionable = dir_cnt.get("BUY", 0) + dir_cnt.get("SELL_SHORT", 0)
    et_hours = sorted({_et_hour(r[1]) for r in rows if r[1] and _et_hour(r[1]) is not None})
    # 盘前判定: 可执行指令是否都在开盘前生成
    # 2026-08-26: 原只比较小时(<9), 09:00-09:29 生成的可执行单被误判盘前;
    # 改为分钟级 (小时<9 或 小时==9 且分<30 ⇔ ET 分钟 < 09:30), 与注释语义对齐。
    premarket = all((_et_min(r[1]) is not None and _et_min(r[1]) < OPEN_ET_MIN)
                    for r in rows if r[0] in ("BUY", "SELL_SHORT") and r[1]) if actionable else False

    # 2) 执行 (trade_records 按 entry_time 当日)
    trades = list(c.execute(
        "SELECT strategy_id, pnl_pct FROM trade_records WHERE substr(entry_time,1,10)=?", (date,)))
    sid_cnt = Counter((t[0] or "unknown") for t in trades)
    da_fills = sid_cnt.get("playbook_sim", 0)
    fb_fills = sid_cnt.get("technical_fallback", 0)

    # 3) 判定
    if actionable == 0:
        # [2026-08-31 DA链复查] 非交易日(周末/节假日)无指令是预期, 不报警 —
        #   实测手动 --date 周末误报 ⚠️NO_PLAN。holiday_calendar 失败退回旧措辞。
        _nontrading = False
        try:
            from datetime import date as _date_cls
            from app.utils.holiday_calendar import get_holiday_calendar
            _y, _m, _dd = (int(x) for x in date.split("-"))
            _nontrading = not get_holiday_calendar().is_trading_day("US", _date_cls(_y, _m, _dd))
        except Exception as _e:
            print(f"WARNING: 假日历判定失败, 按交易日处理: {_e}")
        if _nontrading:
            verdict = "✅ 非交易日 — 无 DA 指令属预期 (周末/节假日)"
        else:
            verdict = "⚠️ NO_PLAN — 当日无可执行 DA 指令 (应触发 technical 兜底)"
    elif fb_fills > 0 and da_fills == 0:
        verdict = f"🔧 FALLBACK — 走了 technical 兜底 ({fb_fills} 笔)"
    elif da_fills > 0:
        verdict = f"✅ DA_ACTIVE — DA 指令已执行 ({da_fills} 笔 playbook_sim)"
        # 2026-08-26: DA 成交当日兜底链也在成交, 说明部分单走了 TFB (bridge 可能未接上),
        # 不能纯 ✅ 掩盖 — 附警告保留线索。
        if fb_fills > 0:
            verdict += f" ⚠️ 兜底链(TFB)同日也有成交({fb_fills} 笔), 疑似 bridge 未接上"
    else:
        verdict = "⏳ GENERATED_NOT_FILLED — 有 DA 指令但当日无成交 (未触发入场?)"

    # 4) 闸门 (signal_gate 拦截分布)
    gate = _gate_stats(date)

    return {
        "date": date, "inst_total": len(rows), "actionable": actionable,
        "skip": dir_cnt.get("SKIP", 0), "et_hours": et_hours, "premarket": premarket,
        "da_fills": da_fills, "fb_fills": fb_fills, "sid": dict(sid_cnt),
        "gate": gate, "verdict": verdict,
    }


def _fmt(r):
    hrs = "/".join(f"ET{h}" for h in r["et_hours"]) or "—"
    fresh = "盘前✅" if r["premarket"] else ("盘中/盘后⚠️" if r["actionable"] else "—")
    g = r["gate"]
    if g["total"]:
        br = g["BLOCK"] / g["total"] * 100
        passed = g["total"] - g["BLOCK"] - g["WARN"]
        flag = " ⚠️拦太狠" if br >= 40 else ""
        gate_line = (f"\n    gate: {g['total']}信号 BLOCK{g['BLOCK']}({br:.0f}%)"
                     f" WARN{g['WARN']} PASS{passed}{flag}")
    else:
        gate_line = "\n    gate: 无 DA 信号快照"
    return (f"{r['date']}  指令{r['inst_total']}(可执行{r['actionable']}/SKIP{r['skip']}) "
            f"生成{hrs} {fresh} | 成交 DA={r['da_fills']} 兜底={r['fb_fills']}\n    {r['verdict']}"
            f"{gate_line}")


def main(date=None, days=1, push=False):
    end = date or _today_et()
    dates = [(datetime.strptime(end, "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(days)][::-1]
    print("=" * 70)
    print(f"DA 链路日检  ({dates[0]} ~ {dates[-1]})")
    print("=" * 70)
    reports = []
    with get_db().connect("ftseek_us") as c:
        for d in dates:
            r = _report_date(c, d)
            reports.append(r)
            print(_fmt(r))
    print("=" * 70)

    if push:
        last = reports[-1]
        try:
            from app.notifier.feishu import send_text
            send_text("📋 DA链路日检 " + _fmt(last))
        except Exception as _e:
            print(f"WARNING: DA链路日检飞书推送失败 (报告已打印未推送): {_e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DA 链路日检 (只读)")
    ap.add_argument("--date", help="截止日 YYYY-MM-DD (默认今日 ET)")
    ap.add_argument("--days", type=int, default=1, help="回看天数 (默认1)")
    ap.add_argument("--push", action="store_true", help="推送飞书摘要")
    a = ap.parse_args()
    if a.push:
        from scripts.utils.market_guard import exit_if_not_trading_day
        exit_if_not_trading_day()
    main(date=a.date, days=a.days, push=a.push)
