# -*- coding: utf-8 -*-
"""DEB-20260928 ① trail-lock 赢家金字塔影子 — record-only 盘中采集器.

裁决: 2026-09-28 用户授权代理终裁 (父 DEB §二十)。设计/判据: DEB-20260928-trail-lock-pyramid-shadow
(§四 影子设计 + §七 预注册判据 + §十一 第三方复查 + §十二 Codex 更正 + §十三 裁决补写)。

零行为变更纪律:
  - 只追加写 data/shadow/trail_lock_pyramid.jsonl; 不读 DB、不读配置、不影响任何退出判定。
  - 调用方 (pipeline.monitor_playbook_sim_exits) 以 try/except 包裹, 本模块任何异常不得外溢。
  - 台账文件同时被 settle 脚本读 — 写单行 JSONL (原子性够: O_APPEND 单行 < PIPE_BUF)。

行类型 (schema v1):
  arm : 首次进入 trail-lock 武装态 (breakeven_triggered 首 True)。每仓至多一行,
        墓碑 = meta["_pyr_armed"] (经 monitor 既有 metadata_json 写回持久化, 重启不丢)。
        记录实际 arm_price (当时轮询价) / parent_qty / entry_price / peak_price /
        equity_at_arm — 不用固定系数代理 (§十二 判据约束)。equity 在 arm 事件时
        从 capital_state 单行镜像读一次 (每仓至多一次, ro+timeout; 该表无历史,
        settle 事后 join 不可得 — 2026-09-28 实测)。读取失败 → None 落行,
        settle 回退最新净值并打 sentinel。
  tick: 武装后每轮价格轨迹 (含退出轮, exiting=True 标记母仓本轮退出)。
        虚拟腿 trail 回放由 settle 脚本用本轨迹 + arm 日日K 兜底段完成。

回滚: 删 pipeline 调用点 5 行 + 本文件; 台账保留不删 (影子数据纪律)。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
_PATH = _ROOT / "data" / "shadow" / "trail_lock_pyramid.jsonl"

SCHEMA = "v1"

_WARNED = False  # 进程级一次性告警 (棘轮 W4: 失败必须 warning+带栈; 但不许每轮刷屏)


def _append(row: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _equity_now():
    """capital_state 单行镜像读一次 (ro, 1s 超时; 失败→None, 不阻断)。"""
    try:
        import sqlite3
        from app.core.paths import get_trade_records_db
        con = sqlite3.connect(
            Path(get_trade_records_db()).as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            row = con.execute(
                "SELECT current_equity FROM capital_state "
                "ORDER BY last_update DESC LIMIT 1").fetchone()
            return float(row[0]) if row and row[0] else None
        finally:
            con.close()
    except Exception:
        return None


def observe(*, symbol: str, direction: str, entry_price: float, current_price: float,
            qty: float, entry_time_str: str, be_armed: bool, peak_price: float,
            meta: dict, now_et, exiting: bool) -> None:
    """monitor 每轮调用 (武装判定后)。调用方已 try/except; 本函数内部仍自守。"""
    try:
        if not symbol or entry_price <= 0 or current_price <= 0:
            return
        armed = meta.get("_pyr_armed")
        ts = now_et.isoformat()
        if not armed:
            if not be_armed:
                return
            # ── 首次武装: 落 arm 行 + 墓碑 (meta 由 monitor 既有写回持久化) ──
            meta["_pyr_armed"] = {"ts": ts, "px": round(float(current_price), 4)}
            _append({
                "t": "arm", "schema": SCHEMA, "ts": ts, "symbol": symbol,
                "direction": direction, "entry_price": float(entry_price),
                "entry_time": entry_time_str, "parent_qty": float(qty or 0),
                "arm_price": float(current_price), "peak_price": float(peak_price or 0),
                "equity_at_arm": _equity_now(),
            })
            logger.info(f"[pyramid影子] {symbol} {direction} trail-lock 武装事件落盘 "
                        f"(arm={current_price}, qty={qty}) — record-only")
            return
        # ── 已武装: 每轮 tick (含母仓退出轮) ──
        _append({
            "t": "tick", "schema": SCHEMA, "ts": ts, "symbol": symbol,
            "entry_time": entry_time_str, "price": float(current_price),
            "exiting": bool(exiting),
        })
    except Exception as e:  # 自守: 影子采集任何失败都不得影响退出链路 (W4: warning+带栈, 进程级一次)
        global _WARNED
        if not _WARNED:
            _WARNED = True
            logger.warning(f"[pyramid影子] observe 失败 {symbol}: {e} (本进程只报一次)",
                           exc_info=True)
