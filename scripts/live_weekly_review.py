#!/usr/bin/env python3
"""实盘周报复盘 · 美股段 + 港股段 (RFC-078 批C, 2026-09-08).

## 为什么单独一个脚本, 而不是往 `weekly_trade_summary.py` 里加两段

既有周报体系**全部是模拟研究口径**, 与实盘无关, 混在一起会互相冒充:
  · `scripts/weekly_trade_summary.py` (Hermes 周日综合复盘 Step 5) 白名单
    `source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')` ⇒ 报的是 sim 数据收集臂;
  · `scripts/symbol_performance_report.py` 同白名单;
  · `scripts/exit_scorecard.py:244` 干脆写 `AND trade_mode != 'live'` ⇒ **显式排除实盘**。
  ⇒ 用户的真钱交易在任何一份周报里都不是一等公民。本脚本补的就是这一份。

同时按市场拆段: 港股此前**零留痕** (只有 jsonl 的 submit/cancel, 无成交价/量/盈亏),
批B 建了券商真值层 (`live_deal_records` / `live_position_snapshots`) 才有数据可报。

## 两层数据源 (缺一层就少一半真相, 故都报)

  · **券商真值层** (批B): 逐笔成交流水 + 每日仓位快照 —— 不依赖下单代码是否记得写,
    是执行事实。美股回合台账曾因 `_record_manual_close` 委派签名漂移断链
    2026-06-19~09-08 (券商已平仓、账本零记录), 只有这一层能回补。
  · **回合台账层** (既有 `trade_records`): 带 exit_reason / direction, 是决策记录。
    口径沿用 `weekly_trade_summary.py`: 出场 ET 日 `substr(exit_time,1,10)` 归集 +
    净额 `COALESCE(pnl_net, realized_pnl)` + 仅已了结 `exit_price > 0`。

## 纪律

1. **三态, 绝不伪装空**: 台账 `UNAVAILABLE` (券商不可达/未同步) 与 `NO_DATA` (窗口内
   确实没交易) 必须分开说。把"没同步上"报成"本周零交易"会让人以为实盘停了。
2. **不伪造口径**: `fee` 全 NULL (富途 deal 级不返回手续费) ⇒ 所有金额标 **毛/成交额**;
   FIFO 成本基准不完整的标的**不计入**已实现合计 (不用现价编成本)。
3. **不跨币种相加**: 美股段一律 USD, 港股段一律 HKD, **绝不**给"两市场合计盈亏"。
   给了就会被当成一个数读。
4. **缺口如实登记**: 每期报告末尾列出已知缺口, 而不是让读报告的人自己发现数据少了。
5. **每市场用自己的日历, 每列用自己的尺** (2026-09-08 补正, 本脚本第一版的真缺陷):
    · 券商真值层 `deal_date` 是**各市场本地日** (实测富途 deal 级 `create_time` 为市场
      本地时: 港股 47 笔全落 HKT 09-16 点, 美股 52 笔集中 ET 09-16 点并带 04:02 盘前
      与夜盘尾巴, 见 `app/web_dashboard/live_ledger.py` 文件头);
    · 美股 `trade_records.trade_date` 是 **ET** 日 (写方 `_et_trade_date`);
    · 港股手动单 jsonl 的 `ts` 是 **CST(=HKT)** 墙钟;
    · 美股 `manual_order_audit.timestamp` 是 **CST** 墙钟 (写方
      `app/web_dashboard/order/audit.py:183`; 实测同行 `timestamp=2026-09-04T21:52:25`
      与 `created_at=2026-09-04 13:52:25`(UTC) 差 8h)。
    ⇒ 窗口必须**按市场各算一份** (共用一个 ET 窗口切港股行 = 整段平移半天到一天),
      且切 `manual_order_audit` 时必须把 ET 窗口**换算成该列自己的 CST 尺度**
      (否则 ET 12:00 之后下的单 = CST 次日, 半天美股盘的手动单被算进错误的周)。
      换算沿用仓内既有先例 `app/web_dashboard/order/executor.py:883` (DEB-20260904 #203:
      硬编码 ±12h 在冬令时差 13h ⇒ 必须真实时区 localize, 不写死偏移)。

用法:
  PYTHONPATH=. venv/bin/python scripts/live_weekly_review.py                 # 打印 (影子)
  PYTHONPATH=. venv/bin/python scripts/live_weekly_review.py --push          # 推飞书
  PYTHONPATH=. venv/bin/python scripts/live_weekly_review.py --sync --days 7 # 先回补台账再出报告
退出码: 0 正常 / 3 台账 UNAVAILABLE (报告仍会打印, 但带明确的不可用声明) / 2 脚本异常
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_CCY = {"US": "USD", "HK": "HKD"}
_MARKET_LABEL = {"US": "美股", "HK": "港股"}
_TZ_LABEL = {"US": "ET", "HK": "HKT"}
# 开多/开空 与 平多/平空 (实测富途 trd_side 四值) 的 **SSOT 在 live_ledger**
# (`OPEN_SIDES`/`CLOSE_SIDES` + `summarize_deals`), 本脚本不再各写一份 —— 两处各判方向
# 会出现"两边错得一样"的净现金流符号反, 那种错在报告和看板上都看起来合理, 极难发现。


def _db_path() -> Path:
    from app.core.db_utils import get_dashboard_db
    return get_dashboard_db()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _money(v: Optional[float], ccy: str) -> str:
    if v is None:
        return "N/A"
    sym = "$" if ccy == "USD" else "HK$"
    return f"{sym}{v:,.2f}"


# ── 券商真值层 ────────────────────────────────────────────────────

def deal_section(market: str, start: str, end: str) -> Dict[str, Any]:
    """某市场窗口内的成交流水小结 (纯本地读, 不连券商)."""
    from app.web_dashboard import live_ledger

    deals = live_ledger.read_deals(start, end, market=market)
    cov = live_ledger.ledger_coverage()["deals"].get(market, {})
    if not deals:
        # 区分两种空: 台账整体为空(=没同步过/券商不可达) vs 台账有数据但本窗口没有(=真没交易)
        state = "NO_DATA" if (cov.get("rows") or 0) == 0 else "NO_TRADES_IN_WINDOW"
        return {"state": state, "deals": [], "n": 0, "coverage": cov,
                "detail": ("台账为空 — 未同步或券商不可达, 这**不是**本周零交易"
                           if state == "NO_DATA" else
                           f"台账有 {cov.get('rows')} 笔(覆盖 {cov.get('first')}~{cov.get('last')}), "
                           f"但本窗口 {start}~{end} 内无成交")}

    summ = live_ledger.summarize_deals(deals)   # 与看板 /api/hk/ledger 同一份口径
    fifo = live_ledger.fifo_realized(deals)
    by_sym: Dict[str, Dict[str, Any]] = {}
    for d in deals:
        s = by_sym.setdefault(d["symbol"], {"n": 0, "buy": 0.0, "sell": 0.0})
        s["n"] += 1
        if (d["trd_side"] or "").upper() in live_ledger.OPEN_SIDES:
            s["buy"] += d["amount"] or 0.0
        else:
            s["sell"] += d["amount"] or 0.0
    return {
        "state": "OK", "deals": deals, "n": summ["n"],
        "symbols": summ["symbols"], "by_symbol": by_sym,
        "open_amount": summ["open_amount"], "close_amount": summ["close_amount"],
        "net_cash": summ["net_cash"],
        "fee_rows_null": summ["fee_rows_null"],
        "unknown_side_rows": summ["unknown_side_rows"],
        "fifo": fifo, "coverage": cov,
    }


def position_section(market: str, end: str) -> Dict[str, Any]:
    """某市场**最近一个有快照的日**的仓位 (不是"今天", 避免周末/假日误报空仓)."""
    from app.web_dashboard import live_ledger

    rows = live_ledger.latest_snapshot(market, on_or_before=end)
    cov = live_ledger.ledger_coverage()["snapshots"].get(market, {})
    if not rows:
        state = "NO_DATA" if (cov.get("rows") or 0) == 0 else "FLAT"
        return {"state": state, "rows": [], "as_of": None, "coverage": cov,
                "detail": ("无持仓快照 — 未同步或券商不可达" if state == "NO_DATA"
                           else "最近快照日无在仓 (真的空仓)")}
    # 多空**分开**加总: 券商对空仓返回负 market_val, 直接 sum 会把多空对冲成一个
    # 没有物理意义的数 (实测美股段 sum=7,563.10 = TSLA +17,777.50 与 MU −10,214.40
    # 相抵, 读报告的人会以为总仓位只有 7.5K)。
    longs = [r for r in rows if (r["qty"] or 0) > 0]
    shorts = [r for r in rows if (r["qty"] or 0) < 0]
    return {"state": "OK", "rows": rows, "as_of": rows[0]["snapshot_date"],
            "long_mv": round(sum(r["market_val"] or 0.0 for r in longs), 2),
            "short_mv": round(sum(abs(r["market_val"] or 0.0) for r in shorts), 2),
            "gross_mv": round(sum(abs(r["market_val"] or 0.0) for r in rows), 2),
            "unrealized_pl": round(sum(r["pl_val"] or 0.0 for r in rows), 2),
            "n_long": len(longs), "n_short": len(shorts),
            "snapshot_days": cov.get("days", 0), "coverage": cov}


def reconcile_section(market: str, rt: Dict[str, Any], pos: Dict[str, Any]) -> Dict[str, Any]:
    """台账在仓 vs 券商在仓 差集 —— 直接量出"账本断链"的残留.

    为什么值得单独一段: `trade_records` 里 `exit_price=0` 的行语义是"在仓", 但若平仓
    没落账 (2026-06-19~09-08 的委派签名漂移断链) 这些行会**永久停在在仓态**。
    实测本段抓出 US.AAPL / US.NVDA 两笔孤儿 —— 券商已无此仓, 台账仍记在仓。
    不做这个差集, 报告会把孤儿行当"当前在仓"念出来。
    """
    if market != "US":
        return {"state": "N/A", "orphans": [], "untracked": [],
                "detail": "港股无回合台账(不进 trade_records), 无从对账"}
    if rt.get("state") == "UNAVAILABLE" or pos.get("state") not in ("OK", "FLAT"):
        return {"state": "UNAVAILABLE", "orphans": [], "untracked": [],
                "detail": (f"对账两侧不完整 (台账={rt.get('state')}, 券商快照={pos.get('state')}) "
                           "⇒ **不出差集**: 缺一侧的差集会把正常在仓报成孤儿, "
                           "比不对账更坏")}
    ledger_open = {r["symbol"] for r in (rt.get("open_rows") or [])}
    broker = {r["symbol"] for r in (pos.get("rows") or [])}
    orphans = sorted(ledger_open - broker)
    untracked = sorted(broker - ledger_open)
    return {"state": "OK", "orphans": orphans, "untracked": untracked,
            "ledger_open": sorted(ledger_open), "broker": sorted(broker),
            "detail": (f"台账记在仓 {len(ledger_open)} 标的, 券商快照 {len(broker)} 标的; "
                       f"孤儿(台账有券商无) {len(orphans)}, 未入账(券商有台账无) {len(untracked)}")}


# ── 回合台账层 (仅美股有; 港股无 exit_reason/thesis) ────────────────

def roundtrip_section(start: str, end: str) -> Dict[str, Any]:
    """live 库 `trade_records` 的实盘回合小结 (口径与 weekly_trade_summary 同源)."""
    sql = """
    SELECT COALESCE(direction,'N/A') AS direction,
           COALESCE(exit_reason,'N/A') AS exit_reason,
           COUNT(*) AS n,
           SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
           ROUND(SUM(net_pnl), 2) AS total_pnl
    FROM (
        SELECT direction, exit_reason, COALESCE(pnl_net, realized_pnl) AS net_pnl
        FROM trade_records
        WHERE trade_mode = 'live'
          AND exit_price > 0
          AND substr(exit_time, 1, 10) BETWEEN :start AND :end
    )
    GROUP BY 1, 2 ORDER BY total_pnl ASC
    """
    try:
        conn = _conn()
        try:
            rows = [dict(r) for r in conn.execute(sql, {"start": start, "end": end}).fetchall()]
            open_rows = [dict(r) for r in conn.execute(
                """SELECT symbol, direction, quantity, entry_price, entry_time, source_type
                   FROM trade_records
                   WHERE trade_mode='live' AND (exit_price IS NULL OR exit_price = 0)
                   ORDER BY entry_time DESC""").fetchall()]
            all_live = conn.execute(
                "SELECT COUNT(*), MIN(substr(entry_time,1,10)), MAX(substr(entry_time,1,10)) "
                "FROM trade_records WHERE trade_mode='live'").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return {"state": "UNAVAILABLE", "rows": [], "detail": f"台账查询失败: {exc}"}

    n = sum(r["n"] for r in rows)
    wins = sum(r["wins"] for r in rows)
    return {
        "state": "OK" if n else ("NO_TRADES_IN_WINDOW" if (all_live[0] or 0) else "NO_DATA"),
        "rows": rows, "open_rows": open_rows, "n": n, "wins": wins,
        "win_rate_pct": round(100.0 * wins / n, 1) if n else 0.0,
        "net_pnl": round(sum(r["total_pnl"] or 0.0 for r in rows), 2),
        "ledger_total_live_rows": all_live[0] or 0,
        "ledger_span": [all_live[1], all_live[2]],
        "detail": (f"台账实盘行共 {all_live[0]} 笔({all_live[1]}~{all_live[2]}), "
                   f"本窗口已了结 0 笔" if n == 0 and all_live[0] else ""),
    }


def _hk_jsonl_activity(start: str, end: str) -> Dict[str, Any]:
    """港股手动单活动的**真实源**: `data/audit/hk_manual_orders.jsonl`.

    港股单**不进** `manual_order_audit` (实测该表 160 行里只有 1 行 HK, 停在 2026-06-10),
    `hk_order_service.py:36` 把留痕写进了 jsonl。只查 DB 会得出"港股本周无手动下单"
    的**假结论** —— 而同期券商真值层有 18 笔港股成交。
    """
    path = ROOT / "data" / "audit" / "hk_manual_orders.jsonl"
    if not path.exists():
        return {"state": "NO_DATA", "ops": {}, "n": 0, "detail": f"jsonl 不存在: {path.name}"}
    import json
    ops: Dict[str, int] = {}
    status: Dict[str, int] = {}
    bad = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                day = (d.get("ts") or "")[:10]
                if not (start <= day <= end):
                    continue
                ops[d.get("op") or "?"] = ops.get(d.get("op") or "?", 0) + 1
                status[d.get("status") or "?"] = status.get(d.get("status") or "?", 0) + 1
    except OSError as exc:
        return {"state": "UNAVAILABLE", "ops": {}, "n": 0, "detail": str(exc)}
    n = sum(ops.values())
    return {"state": "OK" if n else "NONE", "ops": ops, "status": status, "n": n,
            "malformed_lines": bad, "source": path.name}


def manual_activity_section(start: str, end: str, market: str) -> Dict[str, Any]:
    """手动单活动. 美股读 live 库 `manual_order_audit`; 港股读 jsonl (两者源不同).

    `start`/`end` 是**该市场本地日**: 美股段传 ET 日、港股段传 HKT 日 (由
    `market_windows()` 给出)。两者的列尺度不同, 故处理也不同:
      · 港股 jsonl 的 `ts` 是 CST(=HKT) 墙钟 ⇒ 与 HKT 窗口**同尺**, 直接比日期串;
      · 美股 `manual_order_audit.timestamp` 是 **CST** 墙钟, 而窗口是 ET ⇒ 必须按
        **绝对时刻**换算窗口两端再用该列自己的尺度比较 (`_et_window_to_cst_bounds`)。
        同尺直比会错半天: ET 12:00 之后的单落在 CST 次日 (美股下午盘 = 北京凌晨)。
    """
    if market == "HK":
        return _hk_jsonl_activity(start, end)
    prefix = "HK." if market == "HK" else "US."
    lo, hi = _et_window_to_cst_bounds(start, end)
    sql = """
    SELECT COALESCE(status,'?') AS status, COUNT(*) AS n
    FROM manual_order_audit
    WHERE mode LIKE 'live%' AND symbol LIKE :prefix
      AND timestamp >= :lo AND timestamp < :hi
    GROUP BY 1 ORDER BY n DESC
    """
    try:
        conn = _conn()
        try:
            rows = [dict(r) for r in conn.execute(
                sql, {"prefix": prefix + "%", "lo": lo, "hi": hi}).fetchall()]
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return {"state": "UNAVAILABLE", "rows": [], "detail": str(exc)}
    return {"state": "OK" if rows else "NONE", "rows": rows,
            "total": sum(r["n"] for r in rows),
            # 尺度自证: 让读报告/读接口的人知道这段是按哪把尺切的
            "window_scale": f"ET {start}~{end} → 列尺度 CST [{lo}, {hi})"}


def _et_window_to_cst_bounds(start: str, end: str) -> tuple:
    """ET 日窗口 [start, end] → `manual_order_audit.timestamp` 的 CST 半开区间 [lo, hi).

    与 `app/web_dashboard/order/executor.py:883` 同款 (DEB-20260721 E-1 + DEB-20260904 #203):
    ET 当日 00:00 与次日 00:00 各 localize 后转 CST, **不写死 ±12h** (冬令时差 13h)。
    时区库缺失时回落"同串直比"(= 旧行为), 并在 stderr 显式声明口径降级 —— 静默用错尺
    比报错更坏。
    """
    try:
        from datetime import time as _time

        from app.utils.timezone import CST as _CST, ET as _ET, _localize
    except Exception as exc:  # noqa: BLE001 — 时区库缺失不得让周报不出
        print(f"[live_weekly_review] ⚠ 时区库不可用, 美股手动单窗口按同串直比 (口径降级): {exc}",
              file=sys.stderr)
        return f"{start}T00:00:00", f"{end}T23:59:59"

    def _midnight_to_cst(day: date) -> str:
        return (_localize(datetime.combine(day, _time.min), _ET).astimezone(_CST)
                .strftime("%Y-%m-%dT%H:%M:%S"))

    return (_midnight_to_cst(date.fromisoformat(start)),
            _midnight_to_cst(date.fromisoformat(end) + timedelta(days=1)))


# ── 渲染 ─────────────────────────────────────────────────────────

def _render_market(market: str, deals: Dict[str, Any], pos: Dict[str, Any],
                   audit: Dict[str, Any], rt: Optional[Dict[str, Any]],
                   recon: Optional[Dict[str, Any]] = None,
                   window: Optional[Dict[str, str]] = None) -> List[str]:
    ccy = _CCY[market]
    # 每段自带自己的窗口与尺度: 两段窗口**不是同一个绝对区间** (各市场本地日历),
    # 不写出来就会被当成同一周读 ⇒ 跨市场比较的结论从读法上就错了。
    span = (f" | 窗口 {window['start']}~{window['end']} ({window['tz']} 本地日)"
            if window else "")
    out = [f"【{_MARKET_LABEL[market]} · 实盘】 币种 {ccy}{span}"]

    # 成交流水 (券商真值层)
    if deals["state"] == "OK":
        out.append(f"成交(券商真值): {deals['n']} 笔 / {deals['symbols']} 标的 | "
                   f"开仓额 {_money(deals['open_amount'], ccy)} | "
                   f"平仓额 {_money(deals['close_amount'], ccy)} | "
                   f"净现金流 {_money(deals['net_cash'], ccy)}")
        f = deals["fifo"]
        out.append(f"已实现(**毛**, FIFO 完整基准): {_money(f['realized_gross_complete_basis_only'], ccy)}"
                   f" | 不含手续费(券商 deal 级不返回, {deals['fee_rows_null']}/{deals['n']} 笔 fee=NULL)")
        if f["incomplete_basis_symbols"]:
            out.append(f"  ⚠ 成本基准不完整(**未计入**合计, 不编造基准): "
                       f"{', '.join(f['incomplete_basis_symbols'])}")
        if f["unknown_sides"]:
            out.append(f"  ⚠ 未见过的 trd_side(未参与配对): {f['unknown_sides']}")
        top = sorted(deals["by_symbol"].items(), key=lambda kv: -(kv[1]["buy"] + kv[1]["sell"]))[:5]
        for sym, s in top:
            out.append(f"  {sym:<12} {s['n']:>3}笔  开 {_money(s['buy'], ccy):>14}  "
                       f"平 {_money(s['sell'], ccy):>14}")
    else:
        out.append(f"成交(券商真值): {deals['state']} — {deals.get('detail','')}")

    # 在仓 (券商真值层快照)
    if pos["state"] == "OK":
        # 多空分列: 券商对空仓返回负 market_val, 混加会得到一个没有物理意义的数
        side_txt = f"多 {pos['n_long']}" + (f" / 空 {pos['n_short']}" if pos["n_short"] else "")
        out.append(f"在仓(快照 @{pos['as_of']}, 累计 {pos['snapshot_days']} 天): "
                   f"{len(pos['rows'])} 标的({side_txt}) | "
                   f"多头市值 {_money(pos['long_mv'], ccy)}"
                   + (f" | 空头市值 {_money(pos['short_mv'], ccy)}" if pos["n_short"] else "")
                   + f" | 毛敞口 {_money(pos['gross_mv'], ccy)} | "
                   f"浮动盈亏 {_money(pos['unrealized_pl'], ccy)}")
        for r in pos["rows"][:8]:
            name = (r.get("stock_name") or "")[:8]
            out.append(f"  {r['symbol']:<12}{name:<9} {r['qty']:>9,.0f} @ "
                       f"{(r['cost_price'] or 0):>9,.2f} → {(r['cur_price'] or 0):>9,.2f}  "
                       f"mv {_money(r['market_val'], ccy):>13}  pl {_money(r['pl_val'], ccy):>11}"
                       f" ({(r['pl_ratio'] or 0):+.2f}%)")
        if len(pos["rows"]) > 8:
            out.append(f"  … 另 {len(pos['rows']) - 8} 个标的未列")
    else:
        out.append(f"在仓(快照): {pos['state']} — {pos.get('detail','')}")

    # 回合台账 (仅美股有)
    if rt is not None:
        if rt["state"] == "OK":
            out.append(f"回合台账(trade_records, 出场ET日归集/净额): {rt['n']} 笔 | "
                       f"胜 {rt['wins']} ({rt['win_rate_pct']}%) | 净 {_money(rt['net_pnl'], ccy)}")
            for r in rt["rows"][:6]:
                out.append(f"  {r['direction']:<6} {r['exit_reason']:<26} {r['n']:>3}笔 "
                           f"胜{r['wins']:>3}  {_money(r['total_pnl'], ccy):>13}")
        else:
            out.append(f"回合台账: {rt['state']} {rt.get('detail','')}")
        if rt.get("open_rows"):
            out.append(f"  未闭环台账行(exit_price=0): {len(rt['open_rows'])} 笔 "
                       f"→ {', '.join(r['symbol'] for r in rt['open_rows'][:6])}")

    # 台账 vs 券商 对账 (只在两侧都齐时才出差集)
    if recon is not None:
        if recon["state"] == "OK":
            out.append(f"台账↔券商对账: {recon['detail']}")
            if recon["orphans"]:
                out.append(f"  🚨 孤儿行(台账记在仓、券商已无) ⇒ 平仓未落账: "
                           f"{', '.join(recon['orphans'])}")
            if recon["untracked"]:
                out.append(f"  ⚠ 未入账(券商有仓、台账无在仓行): "
                           f"{', '.join(recon['untracked'])}")
        else:
            out.append(f"台账↔券商对账: {recon['state']} — {recon.get('detail','')}")

    # 手动单活动 (美股源=manual_order_audit, 港股源=hk_manual_orders.jsonl)
    src = "hk_manual_orders.jsonl" if market == "HK" else "manual_order_audit"
    if audit["state"] == "OK":
        if market == "HK":
            out.append(f"手动单活动({src}): "
                       + " | ".join(f"{k} {v}" for k, v in sorted(audit["ops"].items()))
                       + "  [" + ", ".join(f"{k} {v}"
                                           for k, v in sorted(audit["status"].items())) + "]")
        else:
            out.append(f"手动单活动({src}): "
                       + " | ".join(f"{r['status']} {r['n']}" for r in audit["rows"]))
    elif audit["state"] == "NONE":
        out.append(f"手动单活动({src}): 本窗口无手动下单留痕")
    else:
        out.append(f"手动单活动({src}): {audit['state']} — {audit.get('detail','')}")
    return out


def render(start: str, end: str, us: Dict[str, Any], hk: Dict[str, Any],
           gaps: List[str], windows: Optional[Dict[str, Dict[str, str]]] = None) -> str:
    if windows:
        span = " | ".join(
            f"{_MARKET_LABEL[m]} {windows[m]['start']}~{windows[m]['end']}({windows[m]['tz']})"
            for m in ("US", "HK") if m in windows)
    else:  # 回归/复算入口: 未给分市场窗口时退回单一窗尾串
        span = f"{start} ~ {end}"
    try:
        from app.utils.timezone import now_cst, now_et
        gen = (f"{now_et().isoformat(timespec='seconds')} (ET) / "
               f"{now_cst().isoformat(timespec='seconds')} (CST)")
    except Exception as exc:  # noqa: BLE001 — 时区库缺失不得让周报不出, 但口径降级要声明
        gen = f"{datetime.now().isoformat(timespec='seconds')} (服务器墙钟, 时区库不可用: {exc})"
    lines = [f"=== 实盘周报 (手动交易) · {span} ===",
             f"载体: {_db_path().name} | 与 SIM 数据收集仓物理隔离 (RFC-078)",
             f"生成: {gen} | "
             "口径: 券商真值层(成交/仓位) + 回合台账层(trade_records)",
             "⚠ 两市场**币种不同, 不给合计** —— 合计会被当成一个数读",
             "⚠ 两市场**日历不同**(ET / HKT), 窗口各按本地日算 —— 不是同一个绝对区间",
             ""]
    lines += _render_market("US", us["deals"], us["positions"], us["audit"],
                            us["roundtrip"], us.get("reconcile"),
                            window=(windows or {}).get("US"))
    lines += [""]
    lines += _render_market("HK", hk["deals"], hk["positions"], hk["audit"], None,
                            hk.get("reconcile"), window=(windows or {}).get("HK"))
    lines += ["", "【数据状态 (三态, 不伪装空)】"]
    for tag, sec in (("US成交", us["deals"]), ("US在仓", us["positions"]),
                     ("US回合台账", us["roundtrip"]), ("US对账", us.get("reconcile") or {}),
                     ("US手动单", us["audit"]), ("HK成交", hk["deals"]),
                     ("HK在仓", hk["positions"]), ("HK手动单", hk["audit"])):
        lines.append(f"  {tag:<12} {sec.get('state','?')}")
    if gaps:
        lines += ["", "【已知缺口 (如实登记, 不让读报告的人自己发现)】"]
        lines += [f"  · {g}" for g in gaps]
    return "\n".join(lines)


def collect_gaps(us: Dict[str, Any], hk: Dict[str, Any]) -> List[str]:
    gaps: List[str] = []
    rt = us["roundtrip"]
    # 条件含 NO_TRADES_IN_WINDOW: 台账行数少是**结构问题**(断链), 与本窗口有没有交易无关
    if rt.get("state") in ("OK", "NO_TRADES_IN_WINDOW") and rt.get("ledger_total_live_rows", 0) < 25:
        gaps.append(
            f"美股回合台账仅 {rt['ledger_total_live_rows']} 笔"
            f"({rt['ledger_span'][0]}~{rt['ledger_span'][1]}): 手动平仓账本 2026-06-19~09-08 "
            "因 `_record_manual_close` 委派签名漂移断链(券商已平仓、账本零记录), "
            "33d5568c 已修但**历史不可本地回补**; 券商真值层已回补成交流水, "
            "故本报告的成交/仓位可信、回合级 exit_reason 分析样本偏小")
    gaps.append("港股**无回合台账**: 不进 trade_records ⇒ 无 exit_reason / thesis / "
                "attribution, 港股复盘只能用成交流水 + FIFO + 仓位快照")
    if us["deals"].get("state") == "OK" and us["deals"].get("fee_rows_null"):
        gaps.append("所有金额为**毛/成交额**: 富途 deal 级不返回手续费(fee 全 NULL), "
                    "不得当净额用 (本仓已有「毛 edge 修到 0 仍净亏」的费用判决书)")
    for mkt, sec in (("US", us["positions"]), ("HK", hk["positions"])):
        if sec.get("state") == "OK" and (sec.get("snapshot_days") or 0) < 2:
            gaps.append(f"{_MARKET_LABEL[mkt]}仓位快照仅 {sec['snapshot_days']} 天(首次落地) "
                        "⇒ 本期无环比, 需 ≥2 周才能看仓位变化")
    for tag, sec in (("US", us["deals"]), ("HK", hk["deals"])):
        if sec.get("state") == "NO_DATA":
            gaps.append(f"{_MARKET_LABEL[tag]}成交流水台账为空 ⇒ 先跑 "
                        "`scripts/live_ledger_sync.py` 回补, 否则周报只能报状态不能报数")
    recon = us.get("reconcile") or {}
    if recon.get("state") == "OK" and recon.get("orphans"):
        gaps.append(
            f"美股台账有 {len(recon['orphans'])} 笔孤儿在仓行"
            f"({', '.join(recon['orphans'])}): 券商已无此仓而台账 exit_price 仍为 0 = "
            "断链期平仓未落账的残留。这批行的盈亏**永久缺失**, 不要把它们当在仓念")
    if recon.get("state") == "OK" and recon.get("untracked"):
        gaps.append(
            f"美股券商有仓但台账无在仓行: {', '.join(recon['untracked'])} ⇒ "
            "开仓未落 `trade_records`(或已被断链期的平仓覆盖), 这些仓的入场理由/止损不可复盘")
    hk_audit = hk.get("audit") or {}
    if hk_audit.get("state") == "NONE" and hk["deals"].get("n"):
        gaps.append(f"港股本窗口有 {hk['deals']['n']} 笔成交但 jsonl 零下单留痕 ⇒ "
                    "留痕链有缺口(或成交来自非看板入口), 下单理由/拒单原因不可复盘")
    return gaps


def _local_today(market: str) -> date:
    """该市场的本地"今天" (服务器是 CST ⇒ 裸 date.today() 对美股会在 CST 04:00-08:00 段多算一天)."""
    try:
        from app.utils.timezone import now_cst, now_et
        return (now_cst() if market == "HK" else now_et()).date()
    except Exception as exc:  # noqa: BLE001 — 时区库缺失不得让报告脚本崩掉
        print(f"[live_weekly_review] {market} 本地日不可用, 回落服务器本地日: {exc}",
              file=sys.stderr)
        return date.today()


def market_windows(days: int, end_date: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """每市场各一份窗口 (本地日闭区间) + 尺度标签 — 见文件头纪律 5.

    显式 `--end-date` 时两市场同串: 那是人工复算/回归要的确定性入口 (钉测试也走它)。
    """
    days = max(1, int(days))
    out: Dict[str, Dict[str, str]] = {}
    for market in ("US", "HK"):
        end = date.fromisoformat(end_date) if end_date else _local_today(market)
        out[market] = {"start": (end - timedelta(days=days - 1)).isoformat(),
                       "end": end.isoformat(), "tz": _TZ_LABEL[market]}
    return out


def default_window(days: int) -> tuple:
    """美股 (ET) 窗口 — 保留旧签名, 等价于 `market_windows(days)["US"]`."""
    w = market_windows(days)["US"]
    return w["start"], w["end"]


def build(days: int, end_date: Optional[str]) -> Dict[str, Any]:
    win = market_windows(days, end_date)
    us_w, hk_w = win["US"], win["HK"]
    us = {"deals": deal_section("US", us_w["start"], us_w["end"]),
          "positions": position_section("US", us_w["end"]),
          "roundtrip": roundtrip_section(us_w["start"], us_w["end"]),
          "audit": manual_activity_section(us_w["start"], us_w["end"], "US")}
    hk = {"deals": deal_section("HK", hk_w["start"], hk_w["end"]),
          "positions": position_section("HK", hk_w["end"]),
          "audit": manual_activity_section(hk_w["start"], hk_w["end"], "HK")}
    # 对账要等两侧都算完 (缺一侧的差集会把正常在仓报成孤儿, 比不对账更坏)
    us["reconcile"] = reconcile_section("US", us["roundtrip"], us["positions"])
    hk["reconcile"] = reconcile_section("HK", {}, hk["positions"])
    return {"start": us_w["start"], "end": us_w["end"], "windows": win,
            "us": us, "hk": hk, "gaps": collect_gaps(us, hk)}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="实盘周报复盘 (美股段 + 港股段)")
    ap.add_argument("--days", type=int, default=7, help="窗口天数 (默认 7)")
    ap.add_argument("--end-date", default=None,
                    help="窗尾 YYYY-MM-DD, 两市场同串 (默认各市场本地今天: 美股 ET / 港股 HKT)")
    ap.add_argument("--push", action="store_true", help="推飞书 (默认只打印 = 影子模式)")
    ap.add_argument("--sync", action="store_true", help="出报告前先回补券商台账")
    ap.add_argument("--sync-days", type=int, default=30, help="回补窗口 (默认 30 天)")
    args = ap.parse_args(argv)

    if args.end_date:
        try:
            date.fromisoformat(args.end_date)
        except ValueError:
            print(f"--end-date 格式应为 YYYY-MM-DD, 收到: {args.end_date}", file=sys.stderr)
            return 2

    sync_state: Dict[str, Any] = {}
    if args.sync:
        try:
            from app.web_dashboard import live_ledger
            sync_state = live_ledger.sync_all(days=args.sync_days)
            print(f"[live_weekly_review] 台账回补: overall={sync_state.get('overall')} "
                  f"deals={sync_state.get('deals', {}).get('status')} "
                  f"positions={sync_state.get('positions', {}).get('status')}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — 回补失败不得让周报不出, 但要显式声明
            sync_state = {"overall": "UNAVAILABLE", "detail": str(exc)}
            print(f"[live_weekly_review] ⚠ 台账回补异常(报告仍出, 数据可能陈旧): {exc}",
                  file=sys.stderr)

    try:
        data = build(args.days, args.end_date)
    except Exception as exc:  # noqa: BLE001
        print(f"[live_weekly_review] 报告生成失败: {exc}", file=sys.stderr)
        return 2

    report = render(data["start"], data["end"], data["us"], data["hk"], data["gaps"],
                    windows=data.get("windows"))
    if sync_state:
        report = (f"[台账回补 {sync_state.get('overall','?')}]\n" + report)
    print(report)

    if args.push:
        try:
            from app.notifier.feishu import send_text
            ok = send_text(report)
            print(f"\n[live_weekly_review] 飞书推送: {'成功' if ok else '失败'}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[live_weekly_review] 飞书推送异常: {exc}", file=sys.stderr)

    # 逐段 .get 而非直接下标: 任一段构建失败时**仍要出报告并如实报状态**,
    # 而不是让整份周报因一个 KeyError 消失 (那才是最彻底的"伪装空")。
    # 段整个缺失 (None) 同样计入不可用 —— 缺失比 UNAVAILABLE 更严重, 不得当"没问题"放过。
    unavailable = [n for n, sec in
                   (("US成交", data["us"].get("deals")), ("US在仓", data["us"].get("positions")),
                    ("US回合台账", data["us"].get("roundtrip")),
                    ("US对账", data["us"].get("reconcile")),
                    ("HK成交", data["hk"].get("deals")), ("HK在仓", data["hk"].get("positions")))
                   if sec is None or sec.get("state") in ("UNAVAILABLE", "NO_DATA")]
    if sync_state.get("overall") == "UNAVAILABLE" or len(unavailable) >= 3:
        if unavailable:
            print(f"[live_weekly_review] ⚠ 以下段无数据(未同步/不可达, **不是**零交易): "
                  f"{', '.join(unavailable)}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
