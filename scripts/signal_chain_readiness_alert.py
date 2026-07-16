#!/usr/bin/env python3
"""选股链每日就绪告警 (2026-07-16)

两条并行选股链的"出货就绪"检查, 只在异常时推飞书。非交易日直接跳过。

链路:
  DA (deep_analysis): 外部 Hermes skill 生成, 直写 playbook_instructions (decision_id LIKE 'DA-%')
  UE (universe_expansion): scripts/llm_universe_scan.py scan→inject, 写 playbook_instructions
                           (decision_id LIKE 'universe_expansion:%') + scan 记录落 jsonl

分层判定 (关键: 区分"故障"与"正常无机会"):
  DA:
    - 当日 DA- 指令 == 0  → 告警 (DA 应每交易日出货)
  UE:
    - 当日 jsonl 无 scan 记录  → 告警 (scan 没跑/LLM 失败 — 真故障)
    - 有 scan 记录 但 inject 0 条 → 静默 (正常: 今日无池外机会 / 都被排重或 conf<阈值)

安全: 纯只读 (读 DB + jsonl)。不下单/不改 DB/不改指令。异常才推飞书, 正常无声。
      发现异常 → 退出码 1; 一切正常 → 0。

建议 cron: 每交易日 ET 10:00 (CST 22:00) 左右, 此时 DA/UE 当日都应写完。
"""
import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta

os.chdir('/opt/ftseek/FTSeek-US')

ET = timezone(timedelta(hours=-4))  # EDT; 换季 -5 仅影响显示, 不影响日期判定
DB = 'data/ftseek_us.db'
UE_JSONL = 'data/shadow/llm_universe_scan.jsonl'


def _today_et():
    return datetime.now(ET).strftime("%Y-%m-%d")


def _is_trading_day(date_str):
    """复用项目节假日日历 (与 llm_universe_scan.py 一致); 不可用时 fail-open 按周末兜底。"""
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        from datetime import date as _date
        return get_holiday_calendar().is_trading_day("US", _date.fromisoformat(date_str))
    except Exception:
        # 兜底: 仅按周末判定 (宁可多检查不漏)
        try:
            wd = datetime.fromisoformat(date_str).weekday()
            return wd < 5
        except Exception:
            return True


def _count_instructions(date_str):
    """当日 playbook_instructions 中 DA / UE 各出货几条。"""
    da = ue = 0
    try:
        c = sqlite3.connect(DB)
        cur = c.cursor()
        cur.execute(
            "SELECT "
            "SUM(CASE WHEN decision_id LIKE 'DA-%' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN decision_id LIKE 'universe_expansion:%' THEN 1 ELSE 0 END) "
            "FROM playbook_instructions WHERE date=?",
            (date_str,)
        )
        row = cur.fetchone()
        da = int(row[0] or 0)
        ue = int(row[1] or 0)
        c.close()
    except Exception as e:
        print(f"[probe] 读 playbook_instructions 异常: {e}")
    return da, ue


def _ue_scan_ran_today(date_str):
    """当日 UE jsonl 是否有 scan 记录 + surfaced 候选数。
    返回 (scan_ran: bool, surfaced_count: int)。"""
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
                    surfaced = len(r.get('surfaced') or [])  # 取最后一轮
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[probe] 读 UE jsonl 异常: {e}")
    return scan_ran, surfaced


def main():
    today = _today_et()
    print(f"=== 选股链就绪检查 @ {datetime.now(ET).isoformat()} (ET日期={today}) ===")

    if not _is_trading_day(today):
        print(f"{today} 非交易日, 跳过")
        return 0

    da, ue = _count_instructions(today)
    scan_ran, surfaced = _ue_scan_ran_today(today)
    print(f"[DA] 当日指令: {da}")
    print(f"[UE] 当日指令: {ue} | scan记录={'有' if scan_ran else '无'} | surfaced候选={surfaced}")

    alerts = []

    # DA: 应每交易日出货
    if da == 0:
        alerts.append(f"DA 链今日 0 条指令 (应每交易日出货) — 检查 Hermes deep_analysis cron / bridge 生成")

    # UE: 区分故障 vs 正常无机会
    if not scan_ran:
        alerts.append(f"UE 链今日无 scan 记录 — llm_universe_scan.py scan 未跑或失败 (检查 logs/universe_scan.log / LLM API)")
    elif ue == 0 and surfaced > 0:
        # scan 出了候选但一条没注入 — 可能全被排重/conf<0.70, 属正常但值得留意 (仅打印不告警)
        print(f"[note] UE scan 出 {surfaced} 候选但注入 0 条 (排重/低置信/已持仓过滤) — 正常, 不告警")
    # scan_ran 且 surfaced==0 → 今日无池外机会, 完全正常, 静默

    print("─" * 50)
    if alerts:
        body = f"⚠️ 选股链就绪告警 ({today})\n" + "\n".join(f"• {a}" for a in alerts)
        print(f"❌ {len(alerts)} 项异常:")
        for a in alerts:
            print(f"   - {a}")
        try:
            from app.notifier.feishu import send_text
            send_text(body)
            print("已推送飞书告警")
        except Exception as e:
            print(f"飞书推送失败 (不影响退出码): {e}")
        return 1

    print("✅ 选股链就绪 (DA/UE 正常)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
