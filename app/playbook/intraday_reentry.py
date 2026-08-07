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

# 前笔退出判据 (2026-08-07 精化, 替代纯 pnl 代理):
#   硬止损/0-WR 类 = thesis 被证伪 → 不再入 (防报复性交易);
#   良性类 (保本/锁利/止盈/移动止损/日终) = thesis 未证伪 → 可再入;
#   未知 reason → 回退 pnl 代理 (max_prior_loss_pct 阈值)。
#   分类数据依据 (2026-08-07 全量 trade_records, pnl_net 口径):
#     MFE_STALL_EXIT 28笔 0%WR -$1169 / TIME_EXIT 33笔 0%WR -$1151 → 0胜率纯亏损,
#     停滞=方向从未兑现, 再入=给失败 thesis 第二次机会 → 禁入;
#     SL_TRAIL_LOCK 46笔 91.3%WR +$1015 / SL_BREAKEVEN 43笔 41.9%WR -$65 (中性) →
#     保本/锁盈=thesis 未证伪 → 可入;
#     PRE_CLOSE_EXIT 36笔 61.1%WR +$568 / EOD_FORCE_CLOSE 7笔 71.4%WR +$332 →
#     日终全平偏健康 → 可入 (半日市防御; 常规日这些平仓发生在窗口后, 不构成候选);
#     操作类 (GHOST/EXIT_MONITOR/OKR_AUDIT) = 非 thesis 信号 → 禁入 (fail-closed)。
_HARD_STOP_REASONS = frozenset({
    "SL_INITIAL", "SL_ATR_STOP", "SL_FIXED_PCT", "OVERNIGHT_GAP_STOP",
    "MFE_STALL_EXIT", "TIME_EXIT",                # 0% WR 纯亏损 (n=61, -$2320)
    "BROKER_GHOST_CLOSE", "EXIT_MONITOR_CLOSE", "FORCED_CLOSE_OKR_AUDIT",  # 操作类
})
_BENIGN_REASONS = frozenset({
    "SL_BREAKEVEN", "SL_TRAIL_LOCK", "TAKE_PROFIT", "TRAILING_STOP",
    "TRAILING_STOP_BY_MAIN", "PRE_CLOSE_EXIT", "EOD_FORCE_CLOSE",
})


def prior_exit_reenterable(exit_reason: Optional[str], pnl_pct: float,
                           max_prior_loss_pct: float) -> bool:
    """纯函数: 前笔退出是否允许再入场 (可单测, 零 IO).

    pnl_pct 为小数口径 (0.0033=0.33%); max_prior_loss_pct 为百分数 (1.0=1%).
    """
    r = str(exit_reason or "").upper()
    if r in _HARD_STOP_REASONS:
        return False
    if r in _BENIGN_REASONS:
        return True
    try:
        return float(pnl_pct) > -float(max_prior_loss_pct) / 100.0
    except Exception:
        return False


def quick_sl_tp_pct(atr_pct: Optional[float], cfg: Optional[Dict[str, Any]] = None):
    """纯函数: ATR 相对快收 SL/TP 百分数 (2026-08-07 用户裁决: SL放宽/TP降档/按标的).

    SL = clamp(quick_sl_atr_mult×atr_pct, floor, ceil) — 放宽给跨夜空间;
    TP = clamp(quick_tp_atr_mult×atr_pct, floor, ceil) — 降档快速落袋.
    atr_pct 为百分数口径 (5.0=5%); 缺失/<=0 → (None, None), 调用方回退 sl_pct/tp_rr.
    """
    cfg = cfg or _cfg()
    try:
        atr = float(atr_pct)
    except Exception:
        return None, None
    if atr <= 0:
        return None, None
    sl = min(max(float(cfg["quick_sl_atr_mult"]) * atr,
                 float(cfg["quick_sl_floor_pct"])), float(cfg["quick_sl_ceil_pct"]))
    tp = min(max(float(cfg["quick_tp_atr_mult"]) * atr,
                 float(cfg["quick_tp_floor_pct"])), float(cfg["quick_tp_ceil_pct"]))
    return round(sl, 2), round(tp, 2)


def cumulative_exposure_ok(prior_notional: float, max_pos_pct: float,
                           equity: float, mult: float) -> bool:
    """纯函数: 同标的当日累计暴露护栏 (可单测, 零 IO).

    再入场估算名义按单笔上限满配 (max_pos_pct×equity, 保守 — 实际 sizing 受
    regime/水位乘数只会更小, 估算不超则实际必不超; 估算超则宁拦勿放)。
    条件: prior_notional + max_pos_pct×equity ≤ mult×max_pos_pct×equity
        ⇔ prior_notional ≤ (mult−1)×max_pos_pct×equity。
    equity/max_pos_pct 异常 → True (fail-open: 护栏是增量保护, 不阻断再入场)。
    """
    if equity <= 0 or max_pos_pct <= 0:
        return True
    return prior_notional <= (float(mult) - 1.0) * max_pos_pct * equity


def _cumulative_exposure_ok(prior: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """包装: 从 capital_safety 取实时净值 + playbook config 取单笔上限, 调纯函数."""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        _cs = get_capital_safety_system()
        _equity = float(getattr(_cs._state, "current_equity", 0) or 0)
        if _equity <= 0:
            return True
        from app.playbook.config import get_playbook_config
        _max_pos_pct = float(get_playbook_config().get("max_position_pct", 0.06) or 0.06)
        _prior_notional = float(prior.get("entry_price") or 0) * float(prior.get("quantity") or 0)
        return cumulative_exposure_ok(
            _prior_notional, _max_pos_pct, _equity,
            float(cfg.get("max_cumulative_pos_mult", 1.5) or 1.5),
        )
    except Exception:
        return True

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "mode": "live",               # live=重武装走全门链; shadow=仅记录 (非 sim 环境强制 shadow)
    "window_start_hhmm": 1000,    # 10:00 ET 起 (避开盘初噪声)
    "window_end_hhmm": 1355,      # 13:55 ET 止 (14:00 触发 cutoff 前)
    "scan_interval_sec": 300,
    "max_arms_per_day": 3,
    "max_prior_loss_pct": 1.0,    # 前笔亏损 >1% 视为硬止损/论破位 → 不再入场 (防报复性交易)
    "max_cumulative_pos_mult": 1.5,  # 同标的当日累计名义 ≤ 1.5×单笔上限 (防再入场=加仓的标的集中暴露)
    "vwap_band_low_pct": 0.3,     # LONG: 允许跌破 VWAP 至多 0.3% (回踩区下沿)
    "vwap_band_high_pct": 1.0,    # LONG: price ≤ vwap×(1+1.0%) (不追高位); SHORT 对称
    "min_slope_5m_pct": 0.0,      # LONG 要求 >0 / SHORT 要求 <0 (方向转正)
    "min_vol_ratio": 0.8,         # 盘中实时量能比 (与 chase 门同档)
    "max_spread_bps": 25.0,       # L1 盘口 spread 上限
    "min_atr_pct": 2.0,           # 日 ATR% 下限 (低波不做动量, 比 low_atr_gate 3% 宽一档)
    "sl_pct": 1.5,                # 再入场止损回退值 (ATR 缺失时; 主口径见 quick_* 快收组)
    "tp_rr": 1.6,                 # TP = SL×1.6 (回退口径, 过 min_tp_rr=1.5 门)
    # 2026-08-07 (用户裁决: 再入场可跨夜 → SL 放宽给空间 / TP 降档快落袋, 退出栈不变):
    #   ATR 相对化 = 按标的自适应 (高波 AVGO 与低波 AAPL 不同档); 触发 re-price 同式复算.
    #   SL = clamp(0.6×ATR%, 2.0, 3.5)  TP = clamp(0.35×ATR%, 1.0, 2.0)
    #   示例: AVGO(ATR5%)→SL3.0/TP1.75  AAPL(ATR1.8%)→SL2.0/TP1.0  TSM(ATR3%)→SL2.0/TP1.05
    #   代价明示: RR≈0.5-0.6 倒置, 需净胜率 >~62% 才正期望 — 由 eval_intraday_reentry 验证.
    "quick_sl_atr_mult": 0.6,
    "quick_sl_floor_pct": 2.0,    # 对齐 vol_bucket 低波噪声地板, 不入 <2% 扫损区
    "quick_sl_ceil_pct": 3.5,
    "quick_tp_atr_mult": 0.35,
    "quick_tp_floor_pct": 1.0,
    "quick_tp_ceil_pct": 2.0,
    # 水下仓持夜豁免 (2026-08-06 用户裁决"再入场亏损可隔夜"):
    #   仅首夜 (entry date == 当日 ET; 次日 PRE_CLOSE/EOD 正常平仓), 仅水下仓 (盈利仍当日落袋,
    #   与 DEB-20260806-asymmetric ① 一致), 仅本生成器武装的仓 (decision_id 前缀判定).
    #   注意: overnight_hold_enabled 总开关下 OVERNIGHT_GAP_STOP 不生效, 持夜风险由
    #   原 SL 1.5% 兜底 (开盘跳空越 SL 有次日市价滑点风险, sim 期可接受, 影子记录对照).
    "overnight_hold_loser_enabled": False,
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


def _now_iso_et() -> str:
    from app.utils.timezone import now_et
    return now_et().isoformat(timespec="seconds")


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
            SELECT id, symbol, direction, entry_price, exit_price, pnl_pct, exit_reason, quantity
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
            "exit_reason": r["exit_reason"],
            "quantity": float(r["quantity"] or 0),
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


def _armed_symbols_today() -> set:
    """当日已武装锁定的标的集合 (armed 或 arm_failed — 锚点已刷新即锁定)。

    2026-08-07 (用户接管审查#2): 一次性武装 — 武装后即使未触发也不再重刷锚点,
    防止\"武装→拦→重武装\"循环让 entry 追着市价跑 (跟随重武装语义), 锚点锁定
    在武装时刻的确认价, 符合\"回踩企稳再入\" thesis (price ≤ entry 才触发)。
    """
    if not _OUTPUT_PATH.exists():
        return set()
    today = _today_et_str()
    out = set()
    try:
        with _OUTPUT_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("date") == today and r.get("action") in ("armed", "arm_failed"):
                    out.add(r.get("symbol"))
    except Exception:
        return set()
    return out


def _shadow_write(row: Dict[str, Any]) -> None:
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _OUTPUT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _arm_instruction(symbol: str, direction: str, price: float,
                     cfg: Dict[str, Any], trade_id: Any,
                     atr_pct: Optional[float] = None) -> Optional[int]:
    """重武装当日指令: UPSERT 刷新锚点/SL/TP (过 admission 入口审计) + 触发状态复位。

    返回指令 id; 失败 None。
    2026-08-07 修复 (审查盲区1/2):
    - 盲区1: save_instruction 按 (date,symbol) UPSERT (db.py:171 ON CONFLICT DO UPDATE)
      会原地覆盖当日原指令行 (decision_id/thesis/confidence/direction 全被改写) →
      首笔成交的指令层归因断链 (review_orchestrator.py:100 按 decision_id JOIN,
      首笔变孤儿; 原指令 thesis/confidence 记录被销毁)。
      修复: 覆盖前先整行归档 original_instruction 快照到 metadata_json
      (含原始 decision_id/thesis/confidence/SL/TP/raw/metadata), 事后可查,
      不破坏 (date,symbol) 唯一约束。
    - 盲区2: UPDATE 原带 WHERE executed=1 → 原指令处于 executed=0/triggered=1
      (触发未成交) 时不复位 → 13 门触发链不会重走, 指令变成永远等不到的触发;
      且 arm 前已确认该标的当日持仓已平 (closed_count==1 + _has_open_trade=False),
      无条件复位安全。修复: 去掉 WHERE executed=1, 无条件复位。

    2026-08-07 (用户裁决快收结构): SL/TP 主口径改 quick_sl_tp_pct (ATR 相对,
    SL放宽/TP降档); ATR 缺失回退 sl_pct/tp_rr 固定值。
    """
    from app.playbook.db import PlaybookDB
    db = PlaybookDB()
    today = _today_et_str()
    is_long = direction.upper() == "BUY"
    _q_sl, _q_tp = quick_sl_tp_pct(atr_pct, cfg)
    if _q_sl is not None and _q_tp is not None:
        sl_pct, tp_pct = _q_sl / 100.0, _q_tp / 100.0
    else:
        sl_pct = float(cfg["sl_pct"]) / 100.0
        tp_pct = sl_pct * float(cfg["tp_rr"])
    stop_loss = round(price * (1 - sl_pct), 2) if is_long else round(price * (1 + sl_pct), 2)
    take_profit = (round(price * (1 + tp_pct), 2) if is_long
                   else round(price * (1 - tp_pct), 2))
    hhmm = _now_hhmm_et()

    # ── 盲区1修复: UPSERT 前先取原指令快照 (当日该标的唯一行) ──
    orig_fields = None
    try:
        orig = db._conn().execute(
            "SELECT id, decision_id, thesis, confidence, direction, entry_price, "
            "       stop_loss, take_profit, raw_instruction_json, metadata_json "
            "FROM playbook_instructions WHERE date = ? AND symbol = ?",
            (today, symbol),
        ).fetchone()
        if orig is not None:
            orig_fields = {
                "instruction_id": orig[0], "decision_id": orig[1], "thesis": orig[2],
                "confidence": orig[3], "direction": orig[4], "entry_price": orig[5],
                "stop_loss": orig[6], "take_profit": orig[7],
                "raw_instruction_json": orig[8], "metadata_json": orig[9],
            }
    except Exception:
        orig_fields = None                    # 快照失败不阻断武装 (归档是增量审计)

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
        "metadata_json": {
            "source": "intraday_reentry",
            "reentry_of_trade": trade_id,
            "original_instruction": orig_fields,   # 首笔归因快照 (盲区1)
        },
    }
    new_id = db.save_instruction(inst)   # UPSERT by (date,symbol) — executed 标志不动
    from app.utils.timezone import now_utc
    conn = db._conn()
    cur = conn.execute("""
        UPDATE playbook_instructions
        SET executed = 0, triggered = 0, execution_price = NULL, execution_time = NULL,
            skip_reason = NULL, updated_at = ?
        WHERE id = ?
    """, (now_utc().isoformat(), new_id))
    conn.commit()
    if cur.rowcount == 0:
        # 行缺失 (并发删除等极端情况) — 防御, 不视为武装成功
        logger.warning(f"[再入场] {symbol} 指令#{new_id} 复位未命中 (行缺失), 记 arm_failed")
        return None
    logger.info(
        f"[再入场] {symbol} {direction} 重武装指令#{new_id} 完成 "
        f"(原指令#{orig_fields['instruction_id'] if orig_fields else '?'} 已归档 metadata_json)"
    )
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
    armed_syms = _armed_symbols_today()
    armed = 0
    today = _today_et_str()

    for sym, info in _load_closed_today().items():
        if sym in armed_syms:
            continue                      # 一次性武装锁定: 锚点不再跟随市价重刷 (防追价循环)
        if arms_so_far + armed >= int(cfg["max_arms_per_day"]):
            break
        if info["closed_count"] != 1:
            continue                      # 0 笔=未平, ≥2 笔=已再入场过 → 每标的日 1 次
        prior = info["trades"][0]
        direction = "BUY" if str(prior["direction"]).upper() in ("LONG", "BUY") else "SELL_SHORT"
        if not prior_exit_reenterable(prior.get("exit_reason"), prior["pnl_pct"],
                                      float(cfg["max_prior_loss_pct"])):
            base = {
                "ts_et": _now_iso_et(), "date": today, "symbol": sym,
                "direction": direction,
                "prior_trade_id": prior["trade_id"], "prior_pnl_pct": prior["pnl_pct"],
                "prior_exit_reason": prior.get("exit_reason"), "mode": mode,
            }
            base["action"] = "rejected"
            base["reasons"] = [f"exit_reason({prior.get('exit_reason')})"]
            _shadow_write(base)           # 可审计: 白名单拦截的候选落 shadow, 不静默丢弃
            continue                      # 硬止损/0-WR/操作类 → 不报复性再入
        if not _cumulative_exposure_ok(prior, cfg):
            base = {
                "ts_et": _now_iso_et(), "date": today, "symbol": sym,
                "direction": direction,
                "prior_trade_id": prior["trade_id"], "prior_pnl_pct": prior["pnl_pct"],
                "prior_exit_reason": prior.get("exit_reason"), "mode": mode,
            }
            _pnot = float(prior.get("entry_price") or 0) * float(prior.get("quantity") or 0)
            base["action"] = "rejected"
            base["reasons"] = [f"cumulative_exposure(prior_notional=${_pnot:.0f})"]
            _shadow_write(base)           # 可审计: 累计暴露拦截落 shadow
            continue                      # 首笔名义已高 → 再入=标的集中暴露, 拦
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
            "ts_et": _now_iso_et(),
            "date": today, "symbol": sym, "direction": direction,
            "prior_trade_id": prior["trade_id"], "prior_pnl_pct": prior["pnl_pct"],
            "prior_exit_reason": prior.get("exit_reason"),
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

        inst_id = _arm_instruction(sym, direction, price, cfg, prior["trade_id"],
                                   atr_pct=float(atr_pct) if atr_pct is not None else None)
        base["action"] = "armed" if inst_id else "arm_failed"
        base["instruction_id"] = inst_id
        _f_sl, _f_tp = quick_sl_tp_pct(float(atr_pct) if atr_pct is not None else None, cfg)
        base["sl_pct"] = _f_sl if _f_sl is not None else cfg["sl_pct"]
        base["tp_pct"] = _f_tp if _f_tp is not None else round(cfg["sl_pct"] * cfg["tp_rr"], 2)
        _shadow_write(base)
        if inst_id:
            armed += 1
            logger.info(
                f"[再入场] {sym} {direction} 已重武装指令#{inst_id} "
                f"(前笔#{prior['trade_id']} pnl={prior['pnl_pct']*100:+.2f}%, "
                f"price=${price:.2f} vwap=${vwap:.2f} slope={slope_5m} vr={vol_ratio})"
            )
    return armed


def _is_reentry_instruction(symbol: str, date_str: str) -> bool:
    """该标的当日指令是否本生成器武装 (decision_id 前缀判定). 异常 → False (不豁免, 保守)."""
    try:
        from app.playbook.db import PlaybookDB
        conn = PlaybookDB()._conn()
        row = conn.execute(
            "SELECT decision_id FROM playbook_instructions WHERE date = ? AND symbol = ?",
            (date_str, symbol),
        ).fetchone()
        did = str(row[0] or "") if row else ""
        return did.startswith("intraday_reentry-")
    except Exception:
        return False


def overnight_loser_exempt(symbol: str, entry_time_str: str, profit_pct: float,
                           is_reentry=None) -> bool:
    """EOD 强平豁免判定 (2026-08-06 用户裁决"再入场亏损可隔夜").

    全部满足才豁免 (任一不满足 → 正常当日平仓):
      1. 配置开关 overnight_hold_loser_enabled;
      2. 水下仓 (profit_pct < 0) — 盈利仓仍当日落袋;
      3. 首夜 (entry 日期 == 今日 ET) — 次日起不再豁免, PRE_CLOSE/EOD 正常收;
      4. 本生成器武装的仓 (decision_id 前缀) — 其他来源仓位不适用。

    is_reentry: 可注入的 (symbol, date_str)->bool, 默认查库; 单测传桩。
    """
    cfg = _cfg()
    if not cfg.get("overnight_hold_loser_enabled"):
        return False
    try:
        if float(profit_pct) >= 0:
            return False
    except Exception:
        return False
    try:
        from datetime import datetime as _dt
        from app.utils.timezone import now_et as _now_et_fn
        _et_now = _now_et_fn()
        _entry = _dt.fromisoformat(str(entry_time_str))
        _entry_date = (_entry.astimezone(_et_now.tzinfo).date()
                       if _entry.tzinfo is not None else _entry.date())
        if _entry_date != _et_now.date():
            return False                     # 非首夜 → 不豁免
    except Exception:
        return False                         # 日期解析异常 → 不豁免 (保守)
    checker = is_reentry or _is_reentry_instruction
    try:
        return bool(checker(symbol, _entry_date.isoformat()))
    except Exception:
        return False
