"""再入场快 TP 门禁吞掉影子 (2026-08-31, record-only, 零行为变更).

背景 (DEB-20260831-reentry-quick-tp-gate-conflict):
  8/7 用户裁决的再入场"快收结构" (quick_tp=0.35×ATR clamp[1,2]% vs
  quick_sl=0.6×ATR clamp[2,3.5]% → 几何 RR≈0.58) 与 7/10 的 min_tp_rr=1.5
  退出门禁正面相撞 — 门禁把快 TP 当"冒大险博小利"的畸形几何忽略, 全部
  13 笔实弹再入场臂 0 笔 TAKE_PROFIT 出场。两次裁决互杀, 此前无人察觉。

本影子只记反事实, 不改任何退出行为:
  ① note_gate_swallowed_touch: 由 _check_playbook_exit_rules 规则1.5 的
     门禁吞掉分支调用; 价格首次触及被吞 TP 时, 把触达信息记入仓位 meta
     (JSON-safe, 随 metadata_json 跨轮持久, 键存在即去重)。
  ② settle_on_close: 由 _close_trade_record 在 conn.commit() 之后调用;
     对再入场臂 (或任何带触达记录的仓) 落一行 JSONL: 触达信息 + 实际出场
     + 反事实差 (tp_vs_actual_pct > 0 = 按 TP 出场会比实际更好)。

预注册裁决判据 (判据字典 B13): 窗口 9/1-9/30, n≥10 笔 TP 触达结算行 →
  delta 均值 > 费用线 (~0.15%) ∧ ≥70% 方向为正 → 提案恢复快 TP (门禁按
  decision_id 前缀豁免, 另立 DEB + 用户裁决); 否则 → 维持门禁, 提案废除
  quick_tp 参数。窗口内禁改门禁/快收参数 (防污染)。

回滚: yaml playbook.reentry_quick_tp_shadow_enabled=false (或删调用点两处
  try 块)。数据: data/shadow/reentry_quick_tp_shadow.jsonl。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("ftseek.us_playbook.reentry_tp_shadow")

REPO_ROOT = Path(__file__).resolve().parents[2]
SHADOW_PATH = REPO_ROOT / "data" / "shadow" / "reentry_quick_tp_shadow.jsonl"
TOUCH_KEY = "_reentry_tp_shadow_touch"
REENTRY_DECISION_PREFIX = "intraday_reentry"


def _enabled() -> bool:
    try:
        from app.playbook.config import get_playbook_config
        return bool(get_playbook_config().get("reentry_quick_tp_shadow_enabled", True))
    except Exception:
        return True


def note_gate_swallowed_touch(meta: dict, direction: str, current_price: float,
                              tp_price: float, peak_profit_pct: float = 0.0,
                              held_minutes: float = 0.0) -> None:
    """价格首次触及被门禁吞掉的 TP 时, 把触达信息记入 meta (键存在即去重).

    调用方保证只在规则1.5 门禁吞掉分支 (_tp_rr < min_tp_rr) 内调用;
    本函数不判断门禁语义, 只判断价格触达。record-only, 不返回任何信号。
    """
    if not _enabled() or not isinstance(meta, dict):
        return
    if meta.get(TOUCH_KEY):
        return
    try:
        tp = float(tp_price)
        price = float(current_price)
    except (TypeError, ValueError):
        return
    if tp <= 0 or price <= 0:
        return
    touched = (direction == "LONG" and price >= tp) or \
              (direction == "SHORT" and price <= tp)
    if not touched:
        return
    try:
        from app.utils.timezone import now_et
        ts = now_et().isoformat()
    except Exception:
        from datetime import datetime
        ts = datetime.now().isoformat()
    meta[TOUCH_KEY] = {
        "price": round(price, 4),
        "ts": ts,
        "held_minutes": round(float(held_minutes or 0.0), 1),
        "mfe_pct": round(float(peak_profit_pct or 0.0), 4),
    }


def settle_on_close(conn, trade_id: int, exit_price: float, exit_reason: str,
                    shadow_path=None) -> None:
    """平仓结算行: 再入场臂 (或带触达记录的仓) 落一行反事实对比.

    conn 为平仓落库同一连接 (只读 SELECT); 异常仅 warn 不阻断平仓链。
    """
    if not _enabled():
        return
    try:
        row = conn.execute(
            "SELECT symbol, direction, entry_price, quantity, entry_time, "
            "decision_id, metadata_json, source_type "
            "FROM trade_records WHERE id=?",
            (trade_id,),
        ).fetchone()
        if row is None:
            return
        (symbol, direction, entry_price, quantity, entry_time,
         decision_id, metadata_json, source_type) = tuple(row)
        meta = {}
        if metadata_json:
            try:
                meta = json.loads(metadata_json)
            except Exception:
                meta = {}
        decision_id = str(decision_id or "")
        is_reentry = decision_id.startswith(REENTRY_DECISION_PREFIX)
        touch = meta.get(TOUCH_KEY)
        if not is_reentry and not touch:
            return
        entry_price = float(entry_price or 0)
        if entry_price <= 0:
            return
        tp = float(meta.get("take_profit", 0) or 0)
        sl = float(meta.get("stop_loss", 0) or 0)
        tp_risk = abs(entry_price - sl) if sl > 0 else 0.0
        tp_reward = abs(tp - entry_price) if tp > 0 else 0.0
        tp_rr = round(tp_reward / tp_risk, 3) if tp_risk > 0 else None
        exit_price = float(exit_price or 0)
        sign = 1.0 if direction == "LONG" else -1.0
        gross_pnl_pct = round(sign * (exit_price - entry_price) / entry_price * 100, 4) \
            if exit_price > 0 else None
        # 正 = 按 TP 出场会比实际出场更好 (赢家回吐量化)
        tp_vs_actual_pct = round(sign * (tp - exit_price) / entry_price * 100, 4) \
            if (touch and tp > 0 and exit_price > 0) else None
        try:
            from app.utils.timezone import now_et
            ts = now_et().isoformat()
        except Exception:
            from datetime import datetime
            ts = datetime.now().isoformat()
        rec = {
            "shadow": "reentry_quick_tp_gate",
            "ts": ts,
            "trade_id": int(trade_id),
            "decision_id": decision_id,
            "is_reentry": is_reentry,
            "source_type": source_type,
            "symbol": symbol,
            "direction": direction,
            "entry_time": entry_time,
            "entry_price": round(entry_price, 4),
            "quantity": int(quantity or 0),
            "stop_loss": round(sl, 4) if sl > 0 else None,
            "take_profit": round(tp, 4) if tp > 0 else None,
            "tp_rr": tp_rr,
            "tp_touched": bool(touch),
            "touch": touch if isinstance(touch, dict) else None,
            "exit_reason": exit_reason,
            "exit_price": round(exit_price, 4) if exit_price > 0 else None,
            "gross_pnl_pct": gross_pnl_pct,
            "tp_vs_actual_pct": tp_vs_actual_pct,
        }
        path = Path(shadow_path) if shadow_path else SHADOW_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[shadow·reentry_tp] 结算记录失败 trade_id={trade_id}: {e}")
