#!/usr/bin/env python3
"""盘前 DA bridge 生成完成 · 钉钉摘要推送 (2026-07-12)。

背景: 指令来源已收敛为单一作业 A(盘前 ET06:00 deep-stock-analysis)。
原 B(bridge-generation)自带的钉钉"盘前Bridge生成完成"摘要随 B 暂停而消失,
本脚本把该摘要独立出来: 读 A 写的 bridge 文件 (opportunity_scan_{ET日期}.json) → 推钉钉。

设计:
  - 与 A 解耦: 不改 A 的 prompt; 单挂 cron 在 A 完成之后 (盘前) 触发。
  - ET 窗口守卫: 08:50-09:29 ET 才允许推送 (2026-10-16 起, 合并 UE fresh-event 内容, 死线 09:28), 双季 cron 双挂时每季恰一响。
  - 当日幂等: 已有 pushed 记录则不再推送, push_failed 允许下一挂重试。
  - 新鲜度守卫: 只推"今日 ET"的 bridge 文件; 文件不存在/非今日 → 跳过不推 (绝不推陈旧)。
  - 跳过留痕: 窗外/缺文件/解析失败/已推送均写入 summary 台账, 不再静默。
  - 失败安全: 取价/推送异常都吞掉, 绝不抛错。
运行: PYTHONPATH=. ./venv/bin/python scripts/da_bridge_dingtalk_push.py [--date YYYY-MM-DD] [--dry] [--force-window]
"""
import argparse
import fcntl
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.utils.timezone import ET

DATA = Path(__file__).resolve().parents[1] / "data"
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "ftseek_us.db"
PUSH_WINDOW_START_MIN = 8 * 60 + 50
# 2026-10-16: 窗尾 09:26→09:29 ET (合并推送版): 等 fresh-event 批次的死线 09:28,
# 留 1min 开盘前边距; 仍严格盘前 (09:30 前), 语义不变。
PUSH_WINDOW_END_EXCLUSIVE_MIN = 9 * 60 + 29
# 2026-10-16 (用户裁「延后到 UE 产出后再推」): fresh-event 链 (UE scan 09:10 ET →
# da_fresh_event_batch --execute, 实测 ~13min) 的指令写 playbook_instructions (DA- 前缀),
# 桥接推送的 DB 计数面会覆盖它们 — 但推送若早于批次完成就漏掉这部分。改为:
# 推送前等待当日 fresh-event 批次终态行, 到硬死线 (09:28 ET, 留 1min 给推送窗尾) 不等了
# 照推 (桥接内容是主面, UE 补充是次面, 死线防批次故障拖死推送)。
# 2026-10-18 用户裁「DA 为主」: fresh-event 当日写库停用; 生产 cron 不再传
# --wait-fresh-event; 等待函数/flag 保留供回滚与历史重放, 默认不等待。
FRESH_EVENT_LEDGER = DATA / "shadow" / "ue_fresh_da_candidate_pool.jsonl"
FRESH_EVENT_WAIT_DEADLINE_MIN = 9 * 60 + 28
FRESH_EVENT_POLL_SEC = 30
# 终态口径 (2026-10-16 巡检实证排雷): skipped_* 族=「本行未生产」不能当「批次已完成」
# — EST 季错季行会在 08:10 ET 写 skipped_outside_window, 若算终态 ⇒ 09:12 的推送读到它
# 误判批次完成 (老 bug 原样复发)。terminal 只认「真跑完」四类 (complete/complete_no_scan/
# pipeline_failed/dedup_query_failed); 行的 date 键即 ET 日, 天然不读昨日。
_FRESH_EVENT_TERMINAL = ("complete", "complete_no_scan", "pipeline_failed",
                         "dedup_query_failed")

# 自含 dotenv 加载: FINNHUB_API_KEY / 钉钉 webhook 均在 .env.us, 保证 cron 下也能拿到
try:
    from dotenv import load_dotenv
    _ROOT = Path(__file__).resolve().parents[1]
    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / ".env.us")
except Exception:
    pass


def _fresh_event_terminal_today(date: str) -> bool:
    """当日 fresh-event 批次是否已到终态 (complete/complete_no_scan/failed/dedup_failed)。
    skipped_* 族不算终态 (淡季错季行会写 skip ⇒ 误认会复发「早推漏 UE」bug)。"""
    try:
        if not FRESH_EVENT_LEDGER.exists():
            return False
        for line in reversed(FRESH_EVENT_LEDGER.read_text(
                encoding="utf-8").splitlines()[-50:]):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception as _e:
                # 2026-10-16 棘轮清剿 (b5e265ad2 新增点顶爆基线 921→922):
                #   坏行跳过改 stderr 留痕 (9/30 先例), 控制流不变仍跳过。
                print(f"[bridge] fresh-event 台账坏行跳过: {_e}", file=sys.stderr)
                continue
            if (row.get("date") == date and row.get("mode") == "execute"
                    and row.get("status") in _FRESH_EVENT_TERMINAL):
                return True
    except Exception:
        return False
    return False


def _wait_fresh_event_done(date: str) -> str:
    """等当日 fresh-event 批次终态, 到硬死线不等 (返回等待结局供留痕)。"""
    import time as _time
    while True:
        if _fresh_event_terminal_today(date):
            return "batch_done"
        now = _now_et()
        if now.hour * 60 + now.minute >= FRESH_EVENT_WAIT_DEADLINE_MIN:
            return "deadline_proceed_without_batch"
        _time.sleep(FRESH_EVENT_POLL_SEC)


def _today_et():
    return datetime.now(ET).strftime("%Y-%m-%d")


def _now_et():
    return datetime.now(ET)


def _in_push_window(now=None):
    now = now or _now_et()
    minutes = now.hour * 60 + now.minute
    return PUSH_WINDOW_START_MIN <= minutes < PUSH_WINDOW_END_EXCLUSIVE_MIN


def _bridge_path(date):
    return DATA / f"opportunity_scan_{date}.json"


def _instruction_summary(date, db_path=DB_PATH):
    """读生产DB最终指令状态, 区分 bridge 候选与真正可交易指令。"""
    try:
        connection = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT symbol, direction, COALESCE(skip_reason, '') AS skip_reason "
            "FROM playbook_instructions "
            "WHERE date = ? AND (decision_id LIKE 'DA-%' "
            "OR decision_id LIKE 'universe_expansion%') ORDER BY id",
            (date,),
        ).fetchall()
        connection.close()
        instruction_rows = [dict(row) for row in rows]
        executable_rows = [
            row for row in instruction_rows
            if row["direction"] in ("BUY", "SELL_SHORT") and not row["skip_reason"]
        ]
        skipped_rows = [
            row for row in instruction_rows
            if row["direction"] == "SKIP" or row["skip_reason"]
        ]
        return {
            "status": "ok",
            "instruction_count": len(instruction_rows),
            "executable_count": len(executable_rows),
            "skipped_count": len(skipped_rows),
            "rows": instruction_rows,
        }
    except Exception as exc:
        return {"status": "db_error", "error": str(exc), "rows": []}


def _append_summary_record(date, bridge, summary, status, path=None, reason=None):
    """推送口径留痕: bridge候选数与DB可交易数不得再混为一个数字。"""
    record_path = path or (DATA / "da_push_instruction_summary.jsonl")
    record = {
        "type": "da_push_instruction_summary",
        "date": date,
        "status": status,
        "reason": reason,
        "bridge_candidate_count": len(bridge.get("candidates", []) or []),
        "db_instruction_count": summary.get("instruction_count"),
        "executable_count": summary.get("executable_count"),
        "skipped_count": summary.get("skipped_count"),
        "symbols": [
            {
                "symbol": row.get("symbol"),
                "direction": row.get("direction"),
                "skip_reason": row.get("skip_reason"),
            }
            for row in summary.get("rows", [])
        ],
        "recorded_at": datetime.now(ET).isoformat(),
    }
    if reason is None:
        del record["reason"]
    record_path.parent.mkdir(parents=True, exist_ok=True)
    with record_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _has_pushed_record(date, path=None):
    record_path = path or (DATA / "da_push_instruction_summary.jsonl")
    if not record_path.exists():
        return False
    bad_lines = 0
    found = False
    with record_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                continue
            if record.get("date") == date and record.get("status") == "pushed":
                found = True
                break
    if bad_lines:
        # 2026-10-09 复查棘轮清剿: 坏行计数留痕, 不得静默 (R5 模式)
        print(f"  [warn] push 台账坏行 {bad_lines} 条已跳过", file=sys.stderr)
    return found


def _append_skip_record(date, reason, path=None):
    summary = {
        "status": "ok",
        "instruction_count": None,
        "executable_count": None,
        "skipped_count": None,
        "rows": [],
    }
    return _append_summary_record(
        date, {}, summary, "skipped", path=path, reason=reason)


def _finnhub_price(symbol):
    """best-effort 取现价, 失败返回 0。"""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        return 0.0
    sym = symbol.replace("US.", "")
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={sym}&token={key}"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=8) as resp:
            return float(json.loads(resp.read()).get("c", 0) or 0)
    except Exception:
        return 0.0


def _health_section(date):
    """DEB-20260819 §十九-P0-2: 全链健康心跳 (静默降级可见化).

    05:50 cron 中段失败 (富途断连等) 是静默降级 — 卡陈旧仍注入。
    推送加一行健康摘要: 卡重建状态/日K新鲜度/regime。失败安全: 全 try。
    """
    # §二十四-N3: 基准从"calendar当日"改"全池日K数据末日" — 周一/节后不误报
    #   (周六重建的卡含周五数据=周一DA的正确输入; 标签对数据, 不对日历)
    #   顺带终局票(SKYT类)自然识别: 落后全池末日>1自然日=陈旧
    # §二十六-N5: 绝对地板守卫 — 全池相对判定在"重建cron连续多日挂掉"时假绿
    #   (pool_max_end冻结→lag相对自身=0), 补绝对阈值: 落后推送日>5自然日→⚠️全池陈旧
    # §二十六-N6: 基准用众数(与构建器一致), max对未来日期脏数据不稳健
    parts = []
    pool_mode_end = ""
    try:
        import glob as _glob
        from collections import Counter as _Cnt
        files = sorted(_glob.glob(str(DATA / "replay_bars_daily" / "US_*.json")))
        ends = {}
        corrupt = []
        for fp in files:
            try:
                ends[fp] = json.loads(open(fp).read())[-1][0][:10]
            except Exception:
                corrupt.append(fp)  # §二十六-N7: 解析失败=数据错误, 单列报警不归终局
        valid = [e for e in ends.values() if e]
        if valid:
            pool_mode_end = _Cnt(valid).most_common(1)[0][0]
            from datetime import datetime as _dt
            def _lag(e):
                return (_dt.strptime(pool_mode_end, "%Y-%m-%d")
                        - _dt.strptime(e, "%Y-%m-%d")).days
            # §二十四-N1: 终局票(落后>7天,同构建器众数判别)剔出分母
            final_n = sum(1 for e in ends.values() if _lag(e) > 7)
            active = {fp: e for fp, e in ends.items() if _lag(e) <= 7}
            bad = [fp for fp, e in active.items() if _lag(e) > 1]
            n_ok = len(active) - len(bad)
            note = f"+{final_n}终局" if final_n else ""
            note += f"+{len(corrupt)}损坏" if corrupt else ""
            status = "⚠️陈旧" if (bad or corrupt) else "✅"
            # N5 绝对地板: 全池末日落后推送日>5自然日 → 全池陈旧 (周一lag3/长周末lag4不触发)
            pool_lag_abs = (_dt.strptime(date, "%Y-%m-%d")
                            - _dt.strptime(pool_mode_end, "%Y-%m-%d")).days
            if pool_lag_abs > 5:
                status = "⚠️全池陈旧"
            parts.append(f"日K{status}({n_ok}/{len(active)}@{pool_mode_end}{note})")
    except Exception:
        pass
    try:
        cards = list((DATA / "symbol_rule_cards").glob("*.json"))
        if cards:
            if pool_mode_end:
                # 卡新鲜 = generated >= 全池数据末日 (重建晚于数据即新鲜)
                # §二十六-N5: 绝对地板已在日K段报警, 此处保持相对判定
                n_fresh = 0
                for fp in cards:
                    try:
                        g = json.loads(fp.read_text()).get("generated", "")
                        n_fresh += 1 if g >= pool_mode_end else 0
                    except Exception:
                        pass
            else:
                n_fresh = sum(1 for fp in cards
                              if json.loads(fp.read_text()).get("generated") == date)
            mark = ("✅全部新鲜" if n_fresh == len(cards)
                    else f"⚠️{n_fresh}/{len(cards)}新鲜")
            parts.append(f"股性卡{mark}")
        else:
            parts.append("股性卡⚠️缺失")
    except Exception:
        pass
    try:
        import sqlite3 as _sq
        con = _sq.connect(str(DATA / "ftseek_us.db"))
        # 2026-10-16 修 (巡检): ORDER BY date DESC 会命中周报幂等标记行
        # (regime='weekly_report' 且 date=未来周五) ⇒ regime 标签曾连错 3 天。
        # 过滤标记行, 与 app/playbook/db.py get_regime_log 同口径。
        r = con.execute("SELECT date, regime FROM playbook_regime_log "
                        "WHERE regime NOT LIKE '%weekly%' "
                        "ORDER BY date DESC LIMIT 1").fetchone()
        if r:
            parts.append(f"regime={r[1]}({r[0]})")
        con.close()
    except Exception:
        pass
    # H3 (§二十二): 三段全异常时兜底输出, 防静默降级上移一层
    return "🩺 链路健康: " + " | ".join(parts) if parts else "🩺 链路健康: ⚠️检查异常"


def _pangan_section(candidates):
    """DEB-20260819 §十五附加 + §十七-L1: 股性卡注入/引用统计 (盘后判分的第一手观测).

    口径 (§十七-L1 修正版):
      注入组 = pangan_injected 非空的候选 (实际写入 prompt 的规则列表)
      引用   = thesis/bull_case/bear_case 文本中出现卡规则关键词 (剔除打标字段防自引用)
      判分分组 = cited vs 注入未引用 (同为带卡票, 选样受控) — 非"非空vs空"(带卡vs无卡选样混淆)
    失败安全: bridge 无该字段 (旧文件) → 返回空段, 不影响主消息。
    """
    with_card = [c for c in candidates if c.get("pangan_injected")]
    if not with_card:
        return []
    # §二十二-22.4: 两级计数 — 规则级关键词为准(cited), 宽词("股性")作参考列
    _KW_RULE = ("贴日低", "贴日高", "跳空高开", "开盘30min", "开盘5min", "NR7", "窄幅日",
                "上破开盘")
    _KW_WIDE = ("股性档案", "股性")
    lines = ["", "🎯 股性卡引用 (DEB-20260819)",
             f"带卡候选: {len(with_card)}/{len(candidates)}"]
    cited_n = wide_n = 0
    for c in with_card:
        refs = c.get("pangan_injected", [])
        # 自引用防护: 扫描文本必须剔除打标字段本身
        #   (规则名含"贴日低"等关键词, 否则注入组 100% 假阳性)
        _c = {k: v for k, v in c.items() if k != "pangan_injected"}
        text = json.dumps(_c, ensure_ascii=False)
        cited = any(k in text for k in _KW_RULE)
        wide = any(k in text for k in _KW_WIDE)
        cited_n += cited
        wide_n += wide
        mark = "✅引用" if cited else ("🔸宽提" if wide else "➖注入未引用")
        rules = "/".join(r.split("_", 1)[-1][:8] for r in refs[:3])
        lines.append(f"  {c.get('symbol','?')}: {mark} [{rules}]")
    lines.append(f"引用率(规则级): {cited_n}/{len(with_card)} | 宽词提及: {wide_n}")
    return lines


def _build_message(date, bridge):
    candidates = bridge.get("candidates", []) or []
    instruction_summary = _instruction_summary(date)
    instruction_rows = {
        row.get("symbol"): row for row in instruction_summary.get("rows", [])
    }
    if instruction_summary.get("status") == "ok":
        count_line = (
            f"候选: {len(candidates)}只(动态扫描+熟股) | "
            f"DB可交易: {instruction_summary['executable_count']}/"
            f"{instruction_summary['instruction_count']} | "
            f"guardrail SKIP: {instruction_summary['skipped_count']}"
        )
    else:
        count_line = (
            f"候选: {len(candidates)}只(动态扫描+熟股) | "
            "DB最终状态查询失败(推送仅代表bridge候选)"
        )
    lines = [f"📊 盘前Bridge生成完成 ({date})", count_line, ""]
    for c in candidates:
        sym = c.get("symbol", "")
        current = _finnhub_price(sym)
        direction = c.get("direction", "")
        conf = c.get("confidence", 0)
        entry = c.get("entry_price", 0)
        sl = c.get("stop_loss", 0)
        debate = (c.get("debate_outcome", "") or "")[:40]
        price_str = f"现价${current:.2f}" if current else "现价N/A"
        lines.append(f"{sym}: {direction} conf={conf} {price_str}")
        lines.append(f"  LLM建议入场${entry} SL${sl} (实际以re-price为准)")
        db_row = instruction_rows.get(sym)
        if db_row:
            if db_row["direction"] in ("BUY", "SELL_SHORT") and not db_row["skip_reason"]:
                lines.append(f"  DB状态: {db_row['direction']} ✅可交易")
            else:
                lines.append(
                    f"  DB状态: {db_row['direction']} ⚠️SKIP — {db_row['skip_reason']}"
                )
        if debate:
            lines.append(f"  辩论: {debate}")
    _pg = _pangan_section(candidates)
    if _pg:
        lines.extend(_pg)
        lines.append("")
    _hs = _health_section(date)
    if _hs:
        lines.append(_hs)
        lines.append("")
    if instruction_summary.get("status") == "ok":
        lines.append(
            f"落库口径: DA {instruction_summary['instruction_count']}条 | "
            f"可交易 {instruction_summary['executable_count']}条 | "
            f"SKIP {instruction_summary['skipped_count']}条; "
            "仅 BUY/SELL_SHORT 进入执行"
        )
    else:
        lines.append("落库口径: DB查询失败, 本消息候选数不等于可交易数")
    lines.append("实际执行价=re-price实时bid/ask")
    return "\n".join(lines)


def main(date=None, dry=False, force_window=False, wait_fresh_event=False):
    date = date or _today_et()
    now = _now_et()
    if not force_window and not _in_push_window(now):
        print(f"[skip] 当前 ET {now.strftime('%H:%M')} 在推送窗口 08:50-09:29 外")
        _append_skip_record(date, "outside_window")
        return

    path = _bridge_path(date)
    if not path.exists():
        print(f"[skip] bridge 文件不存在: {path.name} (A 可能尚未生成或非交易日)")
        _append_skip_record(date, "bridge_missing")
        return
    try:
        bridge = json.loads(path.read_text())
    except Exception as e:
        print(f"[skip] bridge 文件解析失败: {e}")
        _append_skip_record(date, "bridge_parse_failed")
        return
    td = bridge.get("trade_date")
    if td and td != date:
        print(f"[skip] bridge trade_date={td} ≠ 目标日 {date}, 视为陈旧, 不推")
        _append_skip_record(date, "trade_date_mismatch")
        return

    if not dry and _has_pushed_record(date):
        print(f"[skip] {date} 已有 pushed 记录, 当日不重复推送")
        _append_skip_record(date, "already_pushed")
        return

    # 2026-10-16: 等 fresh-event 批次终态 (UE 产出指令入 DB 后, DB 计数面才完整)
    if wait_fresh_event:
        wait_outcome = _wait_fresh_event_done(date)
        print(f"[fresh-event] 等待结局: {wait_outcome}")

    msg = _build_message(date, bridge)
    print(msg)
    if dry:
        print("\n[dry-run] 未推送")
        return
    instruction_summary = _instruction_summary(date)
    try:
        from app.push import push_text_safe
        from app.push.message import Level as _Lv, Category as _Ct
        push_text_safe(msg, level=_Lv.NORMAL, category=_Ct.SYSTEM)
        print("\n[ok] 已推送 (PushHub)")
        _append_summary_record(date, bridge, instruction_summary, "pushed")
    except Exception as e:
        print(f"\n[warn] 钉钉推送失败(不影响链路): {e}")
        _append_summary_record(date, bridge, instruction_summary, "push_failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="盘前 DA bridge 钉钉摘要 (只读 bridge 文件)")
    ap.add_argument("--date", help="bridge 日期 YYYY-MM-DD (默认今日 ET)")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    ap.add_argument("--force-window", action="store_true", help="跳过 ET 窗口守卫")
    ap.add_argument("--wait-fresh-event", action="store_true",
                    help="推送前等当日 UE fresh-event 批次终态 (死线 09:28 ET 照推)")
    a = ap.parse_args()
    main(date=a.date, dry=a.dry, force_window=a.force_window,
         wait_fresh_event=a.wait_fresh_event)
