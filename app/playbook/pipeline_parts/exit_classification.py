"""出场归因/纯分类/出场载荷纯映射域 (P1a/P1b 迁自 app/playbook/pipeline.py, DEB-20261008 §2)。

首批只承接纯判定件; _close_trade_record / monitor_playbook_exits 巨型编排
留待阶段 5 (最高风险), 本批不动。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def _net_pnl_pair(pnl: float, pnl_pct: float, fees_total, tr_row):
    """毛PnL→净 (pnl, pnl_pct) 对; 费用缺失回退毛值 (fail-open).

    tr_row: trade_records 行快照, [2]=entry_price [4]=quantity (与 _close_trade_record 同序).
    pnl_pct 为 ratio 口径; 净 ratio = 毛 ratio - fees/notional. 2026-08-20 净口径切换 (用户裁"切").
    """
    _net = round(float(pnl), 2)
    _net_pct = round(float(pnl_pct), 4)
    try:
        _f = float(fees_total) if fees_total is not None else 0.0
        if _f > 0:
            _net = round(float(pnl) - _f, 2)
            if tr_row is not None:
                _notional = float(tr_row[2] or 0) * float(tr_row[4] or 0)
                if _notional > 0:
                    _net_pct = round(float(pnl_pct) - _f / _notional, 4)
    except Exception:
        pass
    return _net, _net_pct


def _playbook_exit_owner(db_path, row, meta):
    row = dict(row)
    path = str(Path(db_path).resolve())
    entry_id = str(row.get("futu_order_id") or "")
    close_key = (f"entry:{entry_id}" if entry_id else
                 f"trade:{path}:{row['id']}:{row['entry_time']}")
    return {
        "close_key": close_key, "kind": "trade", "db_path": path,
        "trade_id": row["id"], "instruction_id": meta.get("playbook_instruction_id"),
        "entry_order_id": entry_id, "entry_time": row["entry_time"],
        "entry_price": row["entry_price"], "quantity": row["quantity"],
        "source_type": row["source_type"], "symbol": row["symbol"],
        "direction": row["direction"],
    }


def _playbook_exit_decision(reason, reference_price, meta, holding_hours=0.0):
    snapshot = dict(meta)
    for key in ("_log_tag", "_symbol", "_skip_allowed"):
        snapshot.pop(key, None)
    return {"reason": reason, "reference_price": reference_price,
            "metadata": snapshot, "holding_hours": holding_hours}


def _classify_ghost_cause(conn, symbol: str, self_trade_id: int) -> str:
    """GHOST 平仓归因 (2026-07-19, DEB-round3 GHOST 根因诊断).

    RC1 (entry_unfilled): 入场单未成交却记账 — quote_fallback 无成交校验 → 虚假仓位,
        退出时 broker_no_position → BROKER_GHOST_CLOSE (7/6 六连实证, position_sync
        证实 broker 无持仓且无今日成交记录).
    RC2 (double_tracked): 一仓两账 — 同一 broker 净仓位被 playbook (ET-aware) 与主链路
        (naive-CST) 各自记账, 先平者抽空 broker, 后者 ghost (RIVN #1060/#1069 同刻双记录).
    判定: 当日同 symbol 存在其他非 ghost 已平仓记录 → double_tracked(linked=#id:reason);
          否则 entry_unfilled_or_external.
    record-only: 供 2 周 shadow 复审区分 RC1/RC2 处置, 不改任何平仓行为.
    """
    try:
        # P1a 过渡缝: 经 pipeline 模块绑定读 datetime/_now_et — 既有
        # monkeypatch(app.playbook.pipeline.datetime / _now_et) 全部保持生效,
        # 直至时间 seam 一并迁出 (DEB §9.4 datetime ×3 暗坑)。
        from app.playbook import pipeline as _pipeline
        _today_et = _pipeline.datetime.now(_pipeline._now_et()).strftime("%Y-%m-%d")
        _row = conn.execute(
            "SELECT id, exit_reason FROM trade_records "
            "WHERE symbol=? AND id!=? AND exit_time IS NOT NULL AND exit_time != '' "
            "AND substr(exit_time,1,10)=? "
            "AND exit_reason NOT LIKE '%GHOST%' "
            "AND exit_reason NOT LIKE '%ORPHAN%' "
            "AND exit_reason NOT LIKE '%PHANTOM%' "
            "ORDER BY id DESC LIMIT 1",
            (symbol, self_trade_id, _today_et),
        ).fetchone()
        if _row:
            return f"double_tracked(linked=#{_row[0]}:{_row[1]})"
        return "entry_unfilled_or_external"
    except Exception:
        return "unknown"


def _max_drawdown_from_mfe_mae(mfe_pct: float, mae_pct: float) -> float:
    """peak-to-trough 回撤反算 (2026-08-07 B9 + 复核修复, 纯函数可单测).

    mfe_pct/mae_pct 均为方向归一化正数百分比口径 (LONG/SHORT 通用):
      peak = entry*(1+mfe/100), trough = entry*(1-mae/100)
      回撤(%) = (peak-trough)/peak*100 = 100*(mfe+mae)/(100+mfe)
    数据缺失 (mfe≤0 或 mae≤0, 含从未水下/采样缺失) → 0.0, 与历史一致不引入新值。
    """
    try:
        if mfe_pct > 0 and mae_pct > 0:
            return round(100.0 * (mfe_pct + mae_pct) / (100.0 + mfe_pct), 4)
    except Exception:
        pass
    return 0.0


def classify_be_exit(be_net: Optional[float], be_drift: Optional[float]) -> str:
    """BE 出场分类纯函数 (2026-08-21, Qoder §56.3 判序修复后口径).

    判序: OK 先 (净额正即名实相符) → GAP (坠穿≥5bp) → SLIP (其余).
    旧序 GAP 先判致 BWXT drift=-15.44bp 但净+8.82 被标瀑布自相矛盾.
    telemetry 缺失 (None) → 按净额回退 OK/SLIP (fail-open). 契约测试见
    tests/test_be_exit_class.py.
    """
    if be_net is not None and be_net > 0:
        return "OK"
    if be_drift is not None and float(be_drift) <= -5.0:
        return "GAP"
    return "SLIP"
