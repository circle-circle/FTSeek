#!/usr/bin/env python3
"""盘前统一目标推送 (2026-07-16, 方案B)

在两条选股链都完成后 (UE cron ET 09:10 之后), 读当日 playbook_instructions 全表,
按来源 (DA 深度分析 / UE 池外扩展) 分组, 合并成一条飞书推送。

设计:
  - DA (decision_id LIKE 'DA-%'): deep_analysis 深度分析, 池内 ~80 只
  - UE (decision_id LIKE 'universe_expansion:%'): 池外事件扩展
  - 两条链目标本就落同一张表, 此脚本只做只读聚合 + 渲染 + 推送。
  - 全推 (带 ✅≥70 / ⚠️≥50 / ❌<50 图标), 让人工按置信度自行判断。
  - UE 缺失区分: 查 jsonl 今日是否跑过 scan → 未扫描(故障) vs 无池外机会(正常)。

安全: 纯只读 (读 DB + jsonl)。不下单/不改 DB/不改指令。
      DA 今日 0 条 → 推送中显著标注 (应每交易日出货)。
      非交易日跳过。

建议 cron: 每交易日 ET 09:15 (CST 21:15), UE inject 之后 5min, 开盘前 15min。
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta, date as _date

os.chdir('/opt/ftseek/FTSeek-US')

ET = timezone(timedelta(hours=-4))  # EDT; 换季 -5 仅影响显示
DB = 'data/ftseek_us.db'
UE_JSONL = 'data/shadow/llm_universe_scan.jsonl'


def _today_et():
    return datetime.now(ET).strftime("%Y-%m-%d")


def _is_trading_day(date_str):
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        return get_holiday_calendar().is_trading_day("US", _date.fromisoformat(date_str))
    except Exception:
        try:
            return datetime.fromisoformat(date_str).weekday() < 5
        except Exception:
            return True


def _load_instructions(date_str):
    try:
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute(
            "SELECT symbol, direction, confidence, entry_price, stop_loss, take_profit, "
            "conditions_json, decision_id, horizon, skip_reason "
            "FROM playbook_instructions WHERE date=? ORDER BY decision_id",
            (date_str,)
        )
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        return rows
    except Exception as e:
        print(f"[probe] 读 playbook_instructions 异常: {e}")
        return []


def _ue_scan_ran_today(date_str):
    """当日 UE jsonl 是否有 scan 记录 + 最后一轮 surfaced 数。"""
    scan_ran = False
    surfaced = 0
    try:
        with open(UE_JSONL, errors='ignore') as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('type') == 'scan' and r.get('date') == date_str:
                    scan_ran = True
                    surfaced = len(r.get('surfaced') or [])
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[probe] 读 UE jsonl 异常: {e}")
    return scan_ran, surfaced


def _fmt_inst(r, idx):
    d = ("BUY LONG" if r['direction'] == 'BUY'
         else "SELL SHORT" if r['direction'] == 'SELL_SHORT'
         else r['direction'])
    conf = r['confidence'] or 0
    ic = "✅" if conf >= 70 else "⚠️" if conf >= 50 else "❌"
    hz = r.get('horizon') or 'short'
    hz_ic = {"short": "⚡", "long": "🌙"}.get(hz, "")
    lines = [f"#{idx} {r['symbol']} {d} {ic} {conf}% [{hz_ic}{hz}]"]
    if r['entry_price']:
        lines.append(
            f"   入场 ${r['entry_price']:.2f} → 止损 ${r['stop_loss'] or 0:.2f} "
            f"→ 止盈 ${r['take_profit'] or 0:.2f}"
        )
    try:
        cond = json.loads(r['conditions_json']) if r['conditions_json'] else []
        if cond:
            lines.append("   条件: " + " + ".join(cond[:3]))
    except Exception:
        pass
    return "\n".join(lines)


def _render(date_str, rows, ue_scan_ran, ue_surfaced):
    da = [r for r in rows if (r['decision_id'] or '').startswith('DA-')
          and r['direction'] != 'SKIP']
    ue = [r for r in rows if (r['decision_id'] or '').startswith('universe_expansion:')
          and r['direction'] != 'SKIP']

    L = [f"📋 盘前交易目标 ({date_str})", "━" * 22, ""]

    # DA 段
    if da:
        L.append(f"【DA 深度分析】{len(da)} 个目标")
        for i, r in enumerate(da, 1):
            L.append(_fmt_inst(r, i))
            L.append("")
    else:
        L.append("【DA 深度分析】⚠️ 今日 0 个目标 (应每交易日出货, 请检查 deep_analysis cron)")
        L.append("")

    # UE 段 — 区分故障 vs 无机会
    if ue:
        L.append(f"【UE 池外扩展】{len(ue)} 个目标")
        for i, r in enumerate(ue, 1):
            L.append(_fmt_inst(r, i))
            L.append("")
    elif not ue_scan_ran:
        L.append("【UE 池外扩展】⚠️ 今日未扫描 (llm_universe_scan 未跑/失败, 检查 logs/universe_scan.log)")
        L.append("")
    else:
        L.append(f"【UE 池外扩展】今日无池外机会 (scan 出 {ue_surfaced} 候选, 经排重/低置信过滤后 0 注入)")
        L.append("")

    return "\n".join(L).rstrip()


def main():
    today = os.getenv('PUSH_DATE') or _today_et()
    print(f"=== 盘前统一推送 @ {datetime.now(ET).isoformat()} (ET日期={today}) ===")

    if not _is_trading_day(today):
        print(f"{today} 非交易日, 跳过")
        return 0

    rows = _load_instructions(today)
    ue_scan_ran, ue_surfaced = _ue_scan_ran_today(today)
    body = _render(today, rows, ue_scan_ran, ue_surfaced)

    print("─" * 50)
    print(body)
    print("─" * 50)

    if os.getenv('DRY_RUN') == '1':
        print("[DRY_RUN] 不推送")
        return 0

    try:
        from app.notifier.feishu import send_text
        ok = send_text(body)
        print(f"飞书推送: {'成功' if ok else '失败'}")
        return 0 if ok else 1
    except Exception as e:
        print(f"飞书推送异常: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
