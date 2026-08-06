"""盘中再入场机会生成器 (2026-08-06, 用户指示: sim 环境直上线, 快速验真).

立项背景: 指令供给全部盘前生成 (DA bridge / UE scan / generate), 盘中唯一供给源
topup 已于 2026-08-06 终裁停供 → 盘中事实上零新增入场机会。但盘中指标链
(VWAP/slope_5m/量比/L1 spread) 全部已在实时计算, 且当日平仓 (非硬止损) 的标的
thesis 未被证伪 — 回踩 VWAP 企稳是合法的二次入场点。

机制: TRADING 相位每 300s 扫一次"当日已平仓且前笔非硬亏"的 sim 标的, 指标确认后
**重武装 (re-arm) 该标的当日指令行** (save_instruction UPSERT 刷新锚点/SL/TP +
显式 executed 复位) → 走完整既有触发门链 (13 门 + admission 入口/执行双复核 +
熔断/PDT/报价健康), 不绕过任何风控, 不新增订阅 (ADR-013 约束不变)。

安全边界:
  - 仅 execute_mode=futu_simulate 武装真指令; 其他模式自动降级 record-only;
  - 每标的每日最多 1 次再入场 (由"当日已平仓笔数==1"自然约束, 零额外状态);
  - 每日武装上限 max_arms_per_day; 10:00-13:55 ET 窗 (避开盘初噪声 + 14:00 触发cutoff);
  - 全程落 data/intraday_reentry_shadow.jsonl (含未武装候选+拒因), 供 T+1 对照评估.

回滚: playbook.intraday_reentry.enabled=false + 重启。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_OUTPUT_PATH = Path("data/intraday_reentry_shadow.jsonl")
_last_run_ts = 0.0

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "mode": "live",               # live=重武装走全门链; shadow=仅记录 (非 sim 环境强制 shadow)
    "window_start_hhmm": 1000,    # 10:00 ET 起 (避开盘初噪声)
    "window_end_hhmm": 1355,      # 13:55 ET 止 (14:00 触发 cutoff 前)
    "scan_interval_sec": 300,
    "max_arms_per_day": 3,
    "max_prior_loss_pct": 1.0,    # 前笔亏损 >1% 视为硬止损/论破位 → 不再入场 (防报复性交易)
    "vwap_band_low_pct": 0.3,     # LONG: 允许跌破 VWAP 至多 0.3% (回踩区下沿)
    "vwap_band_high_pct": 1.0,    # LONG: price ≤ vwap×(1+1.0%) (不追高位); SHORT 对称
    "min_slope_5m_pct": 0.0,      # LONG 要求 >0 / SHORT 要求 <0 (方向转正)
    "min_vol_ratio": 0.8,         # 盘中实时量能比 (与 chase 门同档)
    "max_spread_bps": 25.0,       # L1 盘口 spread 上限
    "min_atr_pct": 2.0,           # 日 ATR% 下限 (低波不做动量, 比 low_atr_gate 3% 宽一档)
    "sl_pct": 1.5,                # 再入场止损 (比首入 2.0% 紧 — 二次入场错了要快走)
    "tp_rr": 1.6,                 # TP = SL×1.6 (过 min_tp_rr=1.5 门)
}


def _cfg() -> Dict[str, Any]:
    cfg = dict(_DEFAULTS)
    try:
        from app.core.config_container import get_config
        override = get_config().get_dotted("playbook.intraday_reentry", None)
        if isinstance(override, dict):
            for k, v in override.items():
                if k in cfg and v is not None:
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def _today_et_str() -> str:
    from app.utils.timezone import now_et
    return now_et().strftime("%Y-%m-%d")


def _now_hhmm_et() -> int:
    from app.utils.timezone import now_et
    n = now_et()
    return n.hour * 100 + n.minute


def evaluate_candidate(direction: str, price: float, vwap: float,
                       slope_5m: Optional[float], atr_pct: Optional[float],
                       vol_ratio: Optional[float], spread_bps: Optional[float],
                       cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """纯函数: 盘中指标是否确认 thesis 延续 (可单测, 零 IO).

    LONG: 回踩区 [vwap×(1−low), vwap×(1+high)] 内 + slope>min + 量比/ATR/spread 达标.
    SHORT 对称 (price 在 [vwap×(1−high), vwap×(1+low)], slope<−min).
    指标缺失 (None) → 拒 (再入场是增量机会, 宁缺毋滥, 不适用 fail-open).
    """
    reasons: List[str] = []
    is_long = direction.upper() in ("BUY", "LONG")
    if price <= 0 or vwap <= 0:
        return False, ["no_price_or_vwap"]
    dev_pct = (price - vwap) / vwap * 100.0
    lo, hi = float(cfg["vwap_band_low_pct"]), float(cfg["vwap_band_high_pct"])
    if is_long:
        if not (-lo <= dev_pct <= hi):
            reasons.append(f"vwap_dev({dev_pct:+.2f}%∉[{-lo},{hi}])")
        if slope_5m is None or slope_5m <= float(cfg["min_slope_5m_pct"]):
            reasons.append(f"slope_5m({slope_5m}≤{cfg['min_slope_5m_pct']})")
    else:
        if not (-hi <= dev_pct <= lo):
            reasons.append(f"vwap_dev({dev_pct:+.2f}%∉[{-hi},{lo}])")
        if slope_5m is None or slope_5m >= -float(cfg["min_slope_5m_pct"]):
            reasons.append(f"slope_5m({slope_5m}≥{-float(cfg['min_slope_5m_pct'])})")
    if atr_pct is None or atr_pct < float(cfg["min_atr_pct"]):
        reasons.append(f"atr_pct({atr_pct}<{cfg['min_atr_pct']})")
    if vol_ratio is None or vol_ratio < float(cfg["min_vol_ratio"]):
        reasons.append(f"vol_ratio({vol_ratio}<{cfg['min_vol_ratio']})")
    if spread_bps is None or spread_bps > float(cfg["max_spread_bps"]):
        reasons.append(f"spread_bps({spread_bps}>{cfg['max_spread_bps']})")
    return (not reasons), reasons


def _load_closed_today() -> Dict[str, Dict[str, Any]]:
    """当日已平仓 sim 交易, 按 symbol 聚合。只取恰好平仓 1 笔的标的 (自然约束=每标的日 1 次)."""
    from app.core.paths import get_trade_records_db
    today = _today_et_str()
    conn = sqlite3.connect(str(get_trade_records_db()))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT id, symbol, direction, entry_price, exit_price, pnl_pct
            FROM trade_records
            WHERE trade_date = ?
              AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
              AND exit_price > 0 AND exit_time != '' AND exit_time IS NOT NULL
            ORDER BY id
        """, (today,)).fetchall()
    finally:
        conn.close()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = r["symbol"]
        e = out.setdefault(sym, {"symbol": sym, "closed_count": 0, "trades": []})
        e["closed_count"] += 1
        e["trades"].append({
            "trade_id": r["id"], "direction": r["direction"],
            "entry_price": float(r["entry_price"] or 0),
            "exit_price": float(r["exit_price"] or 0),
            "pnl_pct": float(r["pnl_pct"] or 0),  # 小数口径 (0.0033=0.33%)
        })
    return out


def _has_open_trade(symbol: str) -> bool:
    from app.core.paths import get_trade_records_db
    conn = sqlite3.connect(str(get_trade_records_db()))
    try:
        n = conn.execute("""
            SELECT COUNT(*) FROM trade_records
            WHERE symbol = ? AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
              AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
        """, (symbol,)).fetchone()[0]
        return n > 0
    finally:
        conn.close()


def _arms_today() -> int:
    if not _OUTPUT_PATH.exists():
        return 0
    today = _today_et_str()
    n = 0
    try:
        with _OUTPUT_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("date") == today and r.get("action") == "armed":
                    n += 1
    except Exception:
        return 0
    return n


def _shadow_write(row: Dict[str, Any]) -> None:
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _OUTPUT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _arm_instruction(symbol: str, direction: str, price: float,
                     cfg: Dict[str, Any], trade_id: Any) -> Optional[int]:
    """重武装当日指令: UPSERT 刷新锚点/SL/TP (过 admission 入口审计) + executed 复位。

    返回指令 id; 失败 None。复位与既有 release_execution_claim 同语义 (db.py:319)。
    """
    from app.playbook.db import PlaybookDB
    db = PlaybookDB()
    today = _today_et_str()
    sl_pct = float(cfg["sl_pct"]) / 100.0
    rr = float(cfg["tp_rr"])
    is_long = direction.upper() == "BUY"
    stop_loss = round(price * (1 - sl_pct), 2) if is_long else round(price * (1 + sl_pct), 2)
    take_profit = (round(price * (1 + sl_pct * rr), 2) if is_long
                   else round(price * (1 - sl_pct * rr), 2))
    hhmm = _now_hhmm_et()
    inst = {
        "date": today, "symbol": symbol, "direction": direction,
        "entry_price": round(price, 2),
        "stop_loss": stop_loss, "take_profit": take_profit,
        "position_size": None,          # sizing SSOT 在执行期确定 (admission 执行复核兜底)
        "confidence": 55,               # >50 避开 LONG 分歧降仓分支; confidence 无 IC 证据, 仅占位
        "decision_id": f"intraday_reentry-{trade_id}-{hhmm}",
        "thesis": f"盘中再入场: 前笔(#{trade_id})非硬亏平仓后回踩VWAP企稳, 指标二次确认",
        "horizon": "short",
        "model_used": "intraday_reentry_v1",
        "raw_instruction": {"source": "intraday_reentry", "reentry_of_trade": trade_id},
    }
    new_id = db.save_instruction(inst)   # UPSERT by (date,symbol) — executed 标志不动
    from app.utils.timezone import now_utc
    conn = db._conn()
    cur = conn.execute("""
        UPDATE playbook_instructions
        SET executed = 0, triggered = 0, execution_price = NULL, execution_time = NULL,
            skip_reason = NULL, updated_at = ?
        WHERE id = ? AND executed = 1
    """, (now_utc().isoformat(), new_id))
    conn.commit()
    if cur.rowcount == 0:
        # 原指令未执行 (仅触发未成交?) — 不复位, 避免状态机混乱; 刷新后的锚点仍生效
        logger.warning(f"[再入场] {symbol} 指令#{new_id} 原未处于 executed=1, 跳过复位 (锚点已刷新)")
        return None
    return new_id


def run_scan(scheduler=None) -> int:
    """调度器钩子: TRADING 相位每 300s 扫一轮, 返回武装指令数。全程异常隔离由调用方兜底。"""
    global _last_run_ts
    cfg = _cfg()
    if not cfg["enabled"]:
        return 0

    from app.core.scheduler_triggers import MarketPhase, get_market_phase
    if get_market_phase() != MarketPhase.TRADING:
        return 0
    now = time.time()
    if now - _last_run_ts < int(cfg["scan_interval_sec"]):
        return 0
    hm = _now_hhmm_et()
    if not (int(cfg["window_start_hhmm"]) <= hm <= int(cfg["window_end_hhmm"])):
        return 0
    _last_run_ts = now

    # sim-only 硬守卫: 非模拟环境强制 record-only (双保险, 即使 mode=live)
    mode = str(cfg["mode"]).lower()
    try:
        from app.core.config_container import get_config as _gc
        if _gc().get_dotted("playbook.execute_mode", "futu_simulate") != "futu_simulate":
            mode = "shadow"
    except Exception:
        mode = "shadow"

    router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
    if router is None:
        return 0

    arms_so_far = _arms_today()
    armed = 0
    today = _today_et_str()

    for sym, info in _load_closed_today().items():
        if arms_so_far + armed >= int(cfg["max_arms_per_day"]):
            break
        if info["closed_count"] != 1:
            continue                      # 0 笔=未平, ≥2 笔=已再入场过 → 每标的日 1 次
        prior = info["trades"][0]
        if prior["pnl_pct"] <= -float(cfg["max_prior_loss_pct"]) / 100.0:
            continue                      # 硬止损/论破位 → 不报复性再入
        direction = "BUY" if str(prior["direction"]).upper() in ("LONG", "BUY") else "SELL_SHORT"
        if _has_open_trade(sym):
            continue
        try:
            from app.playbook.pipeline import _symbol_streak_banned
            from app.playbook.config import get_playbook_config
            if _symbol_streak_banned(sym, get_playbook_config()):
                continue
        except Exception:
            pass                          # streak 查询异常 → 不拦 (与门本体 fail-open 语义一致)

        base: Dict[str, Any] = {
            "date": today, "symbol": sym, "direction": direction,
            "prior_trade_id": prior["trade_id"], "prior_pnl_pct": prior["pnl_pct"],
            "mode": mode,
        }
        try:
            ctx = router.get_full_market_context(sym) or {}
        except Exception:
            ctx = {}
        price = float(ctx.get("price", 0) or 0)
        vwap = float(ctx.get("vwap_intraday") or ctx.get("vwap") or 0)
        slope_5m = ctx.get("slope_5m")
        atr_pct = ctx.get("atr_percent")
        try:
            from app.playbook.pipeline import _calc_intraday_vol_ratio
            vol_ratio = _calc_intraday_vol_ratio(router, sym)
        except Exception:
            vol_ratio = None
        spread_bps = None
        try:
            from app.playbook.pipeline import _fetch_l1_micro
            micro = _fetch_l1_micro(scheduler, sym)
            if micro:
                spread_bps = micro.get("spread_bps")
        except Exception:
            micro = None

        ok, reasons = evaluate_candidate(
            direction, price, vwap,
            float(slope_5m) if slope_5m is not None else None,
            float(atr_pct) if atr_pct is not None else None,
            vol_ratio, spread_bps, cfg,
        )
        base.update({"price": price, "vwap": vwap, "slope_5m": slope_5m,
                     "atr_pct": atr_pct, "vol_ratio": vol_ratio,
                     "spread_bps": spread_bps, "micro": micro})
        if not ok:
            base["action"] = "rejected"
            base["reasons"] = reasons
            _shadow_write(base)
            continue

        if mode != "live":
            base["action"] = "would_arm"
            _shadow_write(base)
            logger.info(f"[再入场·影子] {sym} {direction} 指标确认, shadow 模式不武装")
            continue

        inst_id = _arm_instruction(sym, direction, price, cfg, prior["trade_id"])
        base["action"] = "armed" if inst_id else "arm_failed"
        base["instruction_id"] = inst_id
        base["sl_pct"] = cfg["sl_pct"]
        base["tp_rr"] = cfg["tp_rr"]
        _shadow_write(base)
        if inst_id:
            armed += 1
            logger.info(
                f"[再入场] {sym} {direction} 已重武装指令#{inst_id} "
                f"(前笔#{prior['trade_id']} pnl={prior['pnl_pct']*100:+.2f}%, "
                f"price=${price:.2f} vwap=${vwap:.2f} slope={slope_5m} vr={vol_ratio})"
            )
    return armed
