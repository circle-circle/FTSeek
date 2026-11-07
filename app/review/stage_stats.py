"""阶段统计 SSOT (2026-09-18 用户裁: 日报/周报加「阶段×方向×出场原因」+ 延长时段单列)。

为什么需要: 交易结果里**阶段差异被混在一起** —— 实测 8/1–9/18 盘中 327 笔 +$163、
盘前 4 笔 −$649, 合计看是"略微盈利", 拆开才看见盘前链在放血。同一份口径要同时供
日报 / 周报 / 延长时段阶段日报使用 ⇒ 唯一实现放这里, 各报告只调 `render_stage_section`。

阶段口径 (唯一来源 `app/core/trade_identity`):
  1) 优先落库列 `trade_records.entry_session` (实测 875 条里 400 条为空 ⇒ 必须有回退);
  2) 回退 `phase_from_time(entry_time)` (ET 时钟窗口, 盘外时段返回空 → 归入"未标注", 不臆造)。
本模块**不写死"盘前/盘中/盘后"的边界**, 一律走上述 SSOT。
"""
from __future__ import annotations

import sqlite3
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from app.core.trade_identity import phase_cn, phase_from_time
from app.review.metric_caliber import caliber_note, dead_rate_caliber, is_be_exit as _is_be_exit
from app.utils.timezone import to_et

# 展示顺序: 盘前 → 盘中 → 盘后 → 未标注 (未标注恒在末尾, 不许混进任何阶段)
_STAGE_ORDER = ["premarket", "regular", "afterhours", ""]
_STAGE_LABEL = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后", "": "未标注"}
# 延长时段 = 盘前 + 盘后 (用户口中的"盘前/盘后链")
EXT_SESSIONS = ("premarket", "afterhours")
# 样本不足阈值: 少于这个数只给方向性提示, 不给结论 (与既有判据纪律一致)
MIN_N_FOR_READING = 30


def normalize_stage(entry_session: Optional[str], entry_time: Optional[str]) -> str:
    """落库 session (优) → ET 时钟回退 → 空串 (未标注)。"""
    raw = str(entry_session or "").strip().lower()
    if raw:
        cn = phase_cn(raw)
        if cn:
            return raw if raw in _STAGE_LABEL else {"盘前": "premarket", "盘中": "regular",
                                                    "盘后": "afterhours"}[cn]
    if not entry_time:
        return ""
    return phase_from_time(entry_time)


def load_trades(conn: sqlite3.Connection, start: str, end: str,
                require_exit: bool = True) -> List[Dict[str, Any]]:
    """读 [start, end] (ET 出场日, 闭区间) 的成交, 归一化阶段。

    require_exit=True 只取**已平仓** (pnl_net 非空), 避免把在场单混进统计。
    """
    wanted_columns = [
        "id", "symbol", "direction", "entry_time", "exit_time", "exit_reason", "entry_session",
        "strategy_id", "owner_strategy", "source_type", "trade_mode", "entry_reason",
        "signal_id", "decision_id", "entry_price", "exit_price", "realized_pnl",
        "pnl_net", "fees_est", "mfe_pct", "mae_pct", "quantity", "metadata_json",
    ]
    available_columns = {
        row[1] for row in conn.execute("pragma table_info(trade_records)")
    }
    selected_columns = [column for column in wanted_columns if column in available_columns]
    sql = (f"select {', '.join(selected_columns)} "
           "from trade_records where exit_time is not null "
           "and coalesce(data_quality, '') <> 'duplicate_external_close'")
    if require_exit:
        sql += " and pnl_net is not null"
    sql += " order by exit_time, id"
    rows: List[Dict[str, Any]] = []
    for r in conn.execute(sql):
        exit_date = _exit_date_et(r["exit_time"])
        if not start <= exit_date <= end:
            continue
        d = dict(r) if not isinstance(r, sqlite3.Row) else {k: r[k] for k in r.keys()}
        d["stage"] = normalize_stage(d.get("entry_session"), d.get("entry_time"))
        d["net"] = float(d.get("pnl_net") or 0.0)
        d["gross"] = float(d.get("realized_pnl") or 0.0)
        d["dir_cn"] = "做多" if str(d.get("direction") or "").upper() in ("BUY", "LONG") else "做空"
        rows.append(d)
    return rows


def _exit_date_et(exit_time: Optional[str]) -> str:
    if not exit_time:
        return ""
    try:
        value = datetime.fromisoformat(str(exit_time).replace("Z", "+00:00"))
        return to_et(value).date().isoformat()
    except (TypeError, ValueError):
        return ""


def rows_by_exit_stage(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把同一批成交按**出场**时段重贴 stage 标签 (入场时段口径的对照臂)。

    为什么需要 (2026-10-25 周日复盘实测): 按入场时段分桶会把"盘中/盘后建仓、次日
    盘前兑现"的仓全部记进建仓时段, 而它们的利润实际由延长时段退出链交付 ——
    实测 W42 净额 +$582.11 里 **80% 来自盘前出场的 4 笔 (+$466.00)**, 同期盘中
    出场的 34 笔只有 +$94.07。只看入场口径会得出"延长时段合计只贡献 $343"的错误印象。

    只替换 `stage` 字段, 不碰 net/exit_reason/pnl ⇒ 两个口径可同屏并列且必然对账。
    盘外时段 (20:00–03:59 ET) 归空串=未标注 (不臆造), 与入场口径同律。
    """
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["stage"] = phase_from_time(d.get("exit_time")) or ""
        out.append(d)
    return out


def stage_breakdown(rows: Iterable[Dict[str, Any]]) -> "OrderedDict[str, Dict[str, Any]]":
    """按阶段聚合: n / 净额 / 胜率 / MFE=0 笔数 / 方向拆分 / 出场原因拆分。"""
    out: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for st in _STAGE_ORDER:
        out[st] = {"n": 0, "net": 0.0, "wins": 0, "mfe0": 0, "be": 0, "dirs": {}, "reasons": {}}
    for r in rows:
        st = r.get("stage") or ""
        b = out.setdefault(st, {"n": 0, "net": 0.0, "wins": 0, "mfe0": 0, "be": 0, "dirs": {}, "reasons": {}})
        b["n"] += 1
        b["net"] += r["net"]
        if r["net"] > 0:
            b["wins"] += 1
        if r.get("mfe_pct") is not None and float(r["mfe_pct"] or 0) <= 0:
            b["mfe0"] += 1
        if _is_be_exit(r.get("exit_reason")):
            b["be"] += 1
        d = b["dirs"].setdefault(r["dir_cn"], {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += r["net"]
        k = str(r.get("exit_reason") or "?")
        e = b["reasons"].setdefault(k, {"n": 0, "net": 0.0})
        e["n"] += 1
        e["net"] += r["net"]
    # 丢掉完全为空的阶段桶 (未标注无样本时不必占位)
    for st in list(out.keys()):
        if out[st]["n"] == 0:
            del out[st]
    return OrderedDict((k, out[k]) for k in _STAGE_ORDER if k in out)


def _fmt(net: float) -> str:
    return f"{'+' if net >= 0 else '-'}${abs(net):,.2f}"


def render_stage_section(rows: List[Dict[str, Any]], title: str = "分阶段成交统计",
                         caliber: Optional[str] = None) -> str:
    """Markdown 段: ①阶段总表 ②阶段×方向×出场原因 ③延长时段单列 (样本不足标注)。

    口径提示随输出一起给出 (净额=pnl_net 费后; 阶段=entry_session 优先, 空则按 ET 时钟回退)。
    `caliber` 可覆盖阶段来源说明 —— 出场时段对照臂传自己的口径 (否则会印成入场口径, 误导)。
    """
    bd = stage_breakdown(rows)
    if not bd:
        return f"### {title}\n（区间内无已平仓样本）"
    _caliber = caliber or ("阶段=落库 entry_session 优先, 为空按 ET 时钟回退")
    _cal = dead_rate_caliber(rows)
    lines = [f"### {title}",
             f"_口径: 净额=费后 pnl_net; {_caliber}; 样本 <{MIN_N_FOR_READING} 笔仅作方向性参考_",
             f"_{caliber_note(_cal['nonbe_dead'], _cal['nonbe_n'])}_",
             "",
             "| 阶段 | 笔数 | 净额 | 笔均 | 胜率 | MFE=0 | BE出场 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    tot_n, tot_net = 0, 0.0
    for st, b in bd.items():
        tot_n += b["n"]
        tot_net += b["net"]
        wrt = f"{b['wins'] / b['n'] * 100:.0f}%" if b["n"] else "—"
        flag = " ⚠️样本少" if b["n"] < MIN_N_FOR_READING else ""
        _be_pct = f"{b['be'] / b['n'] * 100:.0f}%" if b["n"] else "—"
        lines.append(f"| {_STAGE_LABEL.get(st, st)}{flag} | {b['n']} | {_fmt(b['net'])} | "
                     f"{_fmt(b['net'] / b['n'])} | {wrt} | {b['mfe0']} | {_be_pct} |")
    _tot_be = sum(b["be"] for b in bd.values())
    lines.append(f"| **合计** | {tot_n} | {_fmt(tot_net)} | "
                 f"{_fmt(tot_net / tot_n) if tot_n else '—'} | — | — | "
                 f"{(f'{_tot_be / tot_n * 100:.0f}%') if tot_n else '—'} |")
    # 阶段 × 方向 × 出场原因
    detail = []
    for st, b in bd.items():
        for d_cn, dv in sorted(b["dirs"].items(), key=lambda kv: -kv[1]["n"]):
            for reason, rv in sorted(b["reasons"].items(), key=lambda kv: -kv[1]["n"]):
                pair = [r for r in rows
                        if (r.get("stage") or "") == st and r["dir_cn"] == d_cn
                        and str(r.get("exit_reason") or "?") == reason]
                if not pair:
                    continue
                net = sum(x["net"] for x in pair)
                w = sum(1 for x in pair if x["net"] > 0)
                detail.append((st, d_cn, reason, len(pair), net, w))
    if detail:
        lines += ["", "#### 阶段 × 方向 × 出场原因", "",
                  "| 阶段 | 方向 | 出场原因 | 笔数 | 净额 | 胜率 |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for st, d_cn, reason, n, net, w in sorted(detail, key=lambda t: (t[0], -t[3])):
            lines.append(f"| {_STAGE_LABEL.get(st, st)} | {d_cn} | {reason} | {n} | {_fmt(net)} | "
                         f"{w / n * 100:.0f}% |")
    # 延长时段单列 (盘前+盘后): 用户关心的"延长时段链"口径
    ext = [r for r in rows if (r.get("stage") or "") in EXT_SESSIONS]
    if ext:
        net = sum(r["net"] for r in ext)
        w = sum(1 for r in ext if r["net"] > 0)
        note = " ⚠️ 样本少, 仅方向性" if len(ext) < MIN_N_FOR_READING else ""
        lines += ["", f"**延长时段 (盘前+盘后) 单列**: {len(ext)} 笔 / 净 {_fmt(net)} / "
                      f"胜率 {w / len(ext) * 100:.0f}%{note}",
                  "_盘中 (regular) 与延长时段分账: 混在一起会互相稀释 (实测盘中 327 笔"
                  " +$163 掩盖盘前 4 笔 −$649)_"]
    return "\n".join(lines)


def window_of(closed_trades: Iterable[Dict[str, Any]]) -> Optional[tuple]:
    """由成交列表推导 ET 日期窗口 (闭区间); 无有效 exit_time → None。"""
    days = []
    for t in closed_trades or []:
        et = str((t or {}).get("exit_time") or "")
        if len(et) >= 10 and et[:4].isdigit():
            days.append(et[:10])
    if not days:
        return None
    return min(days), max(days)


def rows_for_closed_trades(closed_trades, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """日报/周报专用入口: 用报告自己的成交窗口回查阶段统计 (窗口一致才可对账)。

    报告里没有可用 exit_time (或库缺失) → 返回空 (调用方不渲染该段, 绝不臆造区间)。
    """
    win = window_of(closed_trades)
    if not win or not db_path:
        return []
    try:
        return rows_from_db(db_path, win[0], win[1])
    except Exception:
        return []


def rows_from_db(db_path: str, start: str, end: str) -> List[Dict[str, Any]]:
    """便捷入口: 直接给库路径 + ET 日期区间 (闭区间)。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        return load_trades(conn, start, end)
    finally:
        conn.close()
