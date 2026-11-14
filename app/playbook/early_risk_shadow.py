"""RFC-203 早期风险 record-only 影子 (2026-11-13).

用户目标: 解决「小亏变大亏」。但按 C4 纪律, 先只记录候选, **不改变任何
入场 / 退出 / 尺寸行为**。本模块只做三件事:

  1. `grace_candidate`   宽限期 (held < 5min) 的 extreme 波动仓, 价格越过
                        方向性 2% 灾难线 → 记录候选, 不退出。
  2. `anchor_candidate`  计划锚 (raw planned_entry / 生成锚) 到实际 reprice/成交
                        的方向性偏离 > 3% → 记录 would_skip=true, 不拦截。
  3. `queue_candidate`   确认 (entry_confirm.confirmed_at) 到实际执行的时间差
                        > 0s → 只测量排队 latency, 不立即下单。

统一 JSONL: data/early_risk_shadow.jsonl。所有行带 `caveat`, 明示这是候选
识别面而非反事实 PnL, 3% / 2% / 5min 都只是**本影子的候选阈值**, 不是生产规则。

纪律:
  · 只 append, 不写 DB / 不下单 / 不 reprice / 不改变返回值。
  · TEST 符号不写 (07-30 遥测污染案军规)。
  · 进程内按 (date, inst_id/trade_id, rule) 去重; 重启后可重复 (离线按同键去重)。
  · 任何异常只 `logger.warning(..., exc_info=True)`, 绝不冒泡到交易路径。
  · 开关: `pb_cfg.get("early_risk_shadow_enabled", True)` — YAML 未写也默认观测;
    置 false 才真停写方 (主线程负责改 config, 本模块不碰 YAML)。
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA = "early_risk_shadow_v1"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "data" / "early_risk_shadow.jsonl"

# 候选阈值 — 刻意集中在此并写进每行, 防止日后被误读成生产阈值。
GRACE_DISASTER_PCT = 2.0
GRACE_MAX_HELD_MINUTES = 5.0
ANCHOR_DRIFT_PCT = 3.0

_RULE_GRACE = "grace_candidate"
_RULE_ANCHOR = "anchor_candidate"
_RULE_QUEUE = "queue_candidate"

_APPEND_LOCK = threading.Lock()
_DEDUP_LOCK = threading.Lock()
_DEDUP: set = set()
_DEDUP_MAX = 20000
_WARNED: set = set()


# ── 基础工具 ────────────────────────────────────────────────────────────


def _num(value: Any) -> Optional[float]:
    """严格取有限浮点; bool/NaN/inf/空值 → None (不伪造数值)。"""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _norm_direction(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in ("BUY", "LONG"):
        return "LONG"
    if text in ("SELL_SHORT", "SHORT", "SELL"):
        return "SHORT"
    return text


def is_test_symbol(symbol: Any) -> bool:
    """TEST 符号不写生产影子 (2026-07-30 遥测污染案军规)。"""
    return "TEST" in str(symbol or "").upper()


def _warn_once(key: str, message: str, error: Optional[BaseException] = None) -> None:
    """进程内只报一次的配置/降级告警, 带栈且不静默。"""
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, exc_info=error if error is not None else True)


def _get_pb_cfg(pb_cfg: Any = None) -> Any:
    if pb_cfg is not None:
        return pb_cfg
    from app.playbook.config import get_playbook_config

    return get_playbook_config()


def enabled(pb_cfg: Any = None) -> bool:
    """观测开关, 代码默认 True (YAML 可以不写这个键)。"""
    try:
        return bool(_get_pb_cfg(pb_cfg).get("early_risk_shadow_enabled", True))
    except Exception as error:  # pragma: no cover - 防御配置容器异常
        # 观测开关读取失败保守保持开启: 关掉观测不会让交易更安全。
        _warn_once("cfg", f"[EarlyRiskShadow] 配置读取失败 → 保持观测开启: {error}", error)
        return True


def clear_dedup_for_tests() -> None:
    """仅供测试隔离进程内去重表; 生产路径不调用。"""
    with _DEDUP_LOCK:
        _DEDUP.clear()


def _date_str(now_et: Any) -> Optional[str]:
    try:
        return now_et.strftime("%Y-%m-%d")
    except Exception as error:
        _warn_once("date", f"[EarlyRiskShadow] 日期格式化失败 (跳过本次): {error}", error)
        return None


def _iso_ts(now_et: Any) -> Optional[str]:
    try:
        return now_et.isoformat(timespec="seconds")
    except Exception as error:
        _warn_once("ts", f"[EarlyRiskShadow] 时间格式化失败 (跳过本次): {error}", error)
        return None


def _claim(key: Tuple[str, str, str]) -> bool:
    with _DEDUP_LOCK:
        if key in _DEDUP:
            return False
        if len(_DEDUP) >= _DEDUP_MAX:
            # 有界防增长; 清空后同一仓位可能再记一行, 由离线 (date, id, rule) 去重兜底。
            _DEDUP.clear()
        _DEDUP.add(key)
        return True


def _release(key: Tuple[str, str, str]) -> None:
    with _DEDUP_LOCK:
        _DEDUP.discard(key)


def _identity(trade_id: Any, inst_id: Any, symbol: Any, meta: Mapping[str, Any]) -> str:
    if trade_id not in (None, ""):
        return f"trade:{trade_id}"
    if inst_id not in (None, ""):
        return f"inst:{inst_id}"
    # 两个 id 都缺失时用入场时间 + 方向兜底, 避免 20s 循环刷屏。
    return f"{symbol}:{meta.get('entry_time', '')}:{_norm_direction(meta.get('direction'))}"


# ── 原子 append (best effort) ───────────────────────────────────────────


def append_row(row: Dict[str, Any], path: Any = None, logger: Any = None) -> bool:
    """追加一行 JSONL。失败只 warning + exc_info, 绝不抛给交易路径。"""
    log = logger or globals()["logger"]
    if not isinstance(row, dict):
        log.warning("[EarlyRiskShadow] 非 dict 行被拒绝 (不影响交易)")
        return False
    if is_test_symbol(row.get("symbol")):
        return False
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        payload = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    except Exception as error:
        log.warning(f"[EarlyRiskShadow] 行序列化失败 (不影响交易): {error}", exc_info=True)
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _APPEND_LOCK:
            # O_APPEND + 单次 write = 进程内/多进程追加尽量不交错 (best effort)。
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                written = os.write(fd, payload)
            finally:
                os.close(fd)
        if written != len(payload):
            log.warning(
                f"[EarlyRiskShadow] 部分写入 {written}/{len(payload)} 字节 "
                f"(不影响交易): {target}")
            return False
        return True
    except Exception as error:
        log.warning(f"[EarlyRiskShadow] JSONL 写盘失败 (不影响交易): {error}", exc_info=True)
        return False


# ── vol_bucket / ATR ───────────────────────────────────────────────────


def atr_fraction(meta: Mapping[str, Any], entry_price: Any = None) -> Optional[float]:
    """返回分数口径 ATR (0.025 = 2.5%)。

    取值优先级: meta.atr_pct → meta.attribution.atr_pct (生产 trade_records
    实际写在这里) → 绝对 meta.atr / entry。绝对 atr 可能是 proxy (atr_basis),
    所以只作最后兜底, 不复用其做桶推导以外的用途。
    """
    if not isinstance(meta, Mapping):
        return None
    attribution = meta.get("attribution")
    if not isinstance(attribution, Mapping):
        attribution = {}
    for raw in (meta.get("atr_pct"), attribution.get("atr_pct")):
        value = _num(raw)
        if value is not None and value > 0:
            # 生产 atr_pct 是分数 (0.025); 兼容百分数 (2.5) 形态但绝不把 0.025 当百分比。
            return value / 100.0 if value > 1.0 else value
    atr_abs = _num(meta.get("atr"))
    entry = _num(entry_price)
    if atr_abs is not None and atr_abs > 0 and entry is not None and entry > 0:
        return atr_abs / entry
    return None


def _vol_buckets(pb_cfg: Any, horizon: str) -> Mapping[str, Any]:
    try:
        cfg = _get_pb_cfg(pb_cfg)
        params = cfg.get_horizon_params(horizon or "short") or {}
        return params.get("vol_buckets") or cfg.get("vol_buckets") or {}
    except Exception as error:  # pragma: no cover - 防御配置容器异常
        _warn_once("volbuckets", f"[EarlyRiskShadow] vol_buckets 读取失败: {error}", error)
        return {}


def resolve_vol_bucket(
    meta: Mapping[str, Any],
    entry_price: Any = None,
    *,
    pb_cfg: Any = None,
    horizon: str = "short",
) -> Tuple[str, str]:
    """解析波动桶, 返回 (bucket, source)。

    `bucket` 只会是 meta/attribution 里的真值、按现有 vol_buckets 配置从 ATR 推导出的
    桶名, 或 "unknown"。**缺 bucket 且缺 ATR 时绝不冒充 extreme**。
    """
    if not isinstance(meta, Mapping):
        meta = {}
    attribution = meta.get("attribution")
    if not isinstance(attribution, Mapping):
        attribution = {}
    reprice = meta.get("reprice_audit")
    if not isinstance(reprice, Mapping):
        reprice = {}
    for candidate in (
        meta.get("vol_bucket"),
        attribution.get("vol_bucket"),
        reprice.get("vol_bucket"),
    ):
        text = str(candidate or "").strip().lower()
        if text and text not in ("unknown", "none", "null"):
            return text, "meta"
    fraction = atr_fraction(meta, entry_price)
    buckets = _vol_buckets(pb_cfg, horizon)
    if fraction is not None and buckets:
        try:
            from app.playbook.bucket_lineage import select_vol_bucket

            bucket = select_vol_bucket(fraction, buckets)
            if bucket:
                return str(bucket).strip().lower(), "derived_from_atr"
        except Exception as error:  # pragma: no cover - 纯防御
            _warn_once("bucket_select", f"[EarlyRiskShadow] 桶推导失败: {error}", error)
    return "unknown", "missing"


def _current_mfe_mae(
    direction: str, entry: float, current: float, meta: Mapping[str, Any]
) -> Tuple[float, float, float, float]:
    if direction == "LONG":
        cur_mfe = (current - entry) / entry * 100.0
        cur_mae = (entry - current) / entry * 100.0
    else:
        cur_mfe = (entry - current) / entry * 100.0
        cur_mae = (current - entry) / entry * 100.0
    cur_mfe = max(0.0, cur_mfe)
    cur_mae = max(0.0, cur_mae)
    mfe = max(cur_mfe, _num(meta.get("mfe_pct")) or 0.0)
    mae = max(cur_mae, _num(meta.get("mae_pct")) or 0.0)
    return round(mfe, 4), round(mae, 4), round(cur_mfe, 4), round(cur_mae, 4)


# ── 三类候选 ───────────────────────────────────────────────────────────


def record_grace_candidate(
    *,
    meta: Mapping[str, Any],
    symbol: Any,
    direction: Any,
    entry_price: Any,
    current_price: Any,
    stop_loss: Any,
    held_minutes: Any,
    now_et: Any,
    trade_id: Any = None,
    inst_id: Any = None,
    decision_id: Any = "",
    pb_cfg: Any = None,
    logger: Any = None,
    path: Any = None,
) -> bool:
    """宽限期灾难候选 — 只记录, 不退出。

    条件: 0 <= held_minutes < 5 ∧ vol_bucket == extreme ∧ 方向性逆向幅度 > 2%。
    缺 bucket 且无法按现有配置推导 → unknown, 不作为 extreme 候选。
    """
    if not enabled(pb_cfg) or is_test_symbol(symbol):
        return False
    if str(decision_id or "").startswith("MOM-ETF-V1-"):
        return False  # RFC-203 grace 人群是 DA early-tight，不混入 MOM swing 出场。
    entry = _num(entry_price)
    current = _num(current_price)
    held = _num(held_minutes)
    if entry is None or current is None or entry <= 0 or current <= 0:
        return False
    if held is None or held < 0 or held >= GRACE_MAX_HELD_MINUTES:
        return False
    direction_norm = _norm_direction(direction)
    if direction_norm not in ("LONG", "SHORT"):
        return False
    if not isinstance(meta, Mapping):
        meta = {}

    bucket, bucket_source = resolve_vol_bucket(
        meta, entry, pb_cfg=pb_cfg, horizon=str(meta.get("horizon") or "short"))
    if bucket != "extreme":
        return False

    if direction_norm == "LONG":
        adverse_pct = (entry - current) / entry * 100.0
    else:
        adverse_pct = (current - entry) / entry * 100.0
    if adverse_pct <= GRACE_DISASTER_PCT:
        return False

    date = _date_str(now_et)
    ts = _iso_ts(now_et)
    if date is None or ts is None:
        return False
    key = (_RULE_GRACE, date, _identity(trade_id, inst_id, symbol, meta))
    if not _claim(key):
        return False

    mfe_pct, mae_pct, current_mfe_pct, current_mae_pct = _current_mfe_mae(
        direction_norm, entry, current, meta)
    fraction = atr_fraction(meta, entry)
    row: Dict[str, Any] = {
        "ts": ts,
        "date": date,
        "schema": SCHEMA,
        "kind": _RULE_GRACE,
        "rule": "grace_extreme_2pct",
        "symbol": str(symbol),
        "direction": direction_norm,
        "trade_id": trade_id,
        "inst_id": inst_id,
        "decision_id": str(decision_id or ""),
        "entry_price": round(entry, 4),
        "current_price": round(current, 4),
        "stop_loss": round(_num(stop_loss), 4) if _num(stop_loss) is not None else None,
        "held_minutes": round(held, 2),
        "atr_pct": round(fraction * 100.0, 4) if fraction is not None else None,
        "vol_bucket": bucket,
        "vol_bucket_source": bucket_source,
        "extreme_verified": True,
        "adverse_pct": round(adverse_pct, 4),
        "disaster_line_pct": GRACE_DISASTER_PCT,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "current_mfe_pct": current_mfe_pct,
        "current_mae_pct": current_mae_pct,
        "would_exit": False,
        "execution_blocked": False,
        "caveat": (
            "record-only 候选识别: 不退出、不改尺寸; 2%/5min 是本影子候选阈值, "
            "不是生产规则; 不计算反事实 PnL。"
        ),
    }
    ok = append_row(row, path=path, logger=logger)
    if not ok:
        _release(key)
    return ok


def compute_anchor_drift_pct(
    direction: Any, planned_entry: Any, effective_entry: Any
) -> Optional[float]:
    """方向性偏离, 正数 = 对交易更不利 (LONG 更高价 / SHORT 更低价)。"""
    planned = _num(planned_entry)
    effective = _num(effective_entry)
    if planned is None or effective is None or planned <= 0 or effective <= 0:
        return None
    direction_norm = _norm_direction(direction)
    if direction_norm == "SHORT":
        return round((planned - effective) / planned * 100.0, 6)
    if direction_norm == "LONG":
        return round((effective - planned) / planned * 100.0, 6)
    return None


def record_anchor_candidate(
    *,
    symbol: Any,
    direction: Any,
    planned_entry: Any,
    planned_entry_source: Any = "",
    effective_entry: Any,
    current_price: Any = None,
    now_et: Any,
    inst_id: Any = None,
    decision_id: Any = "",
    threshold_pct: float = ANCHOR_DRIFT_PCT,
    pb_cfg: Any = None,
    logger: Any = None,
    path: Any = None,
) -> bool:
    """计划锚追价候选 — 只记录 would_skip, 绝不拦截执行。"""
    if not enabled(pb_cfg) or is_test_symbol(symbol):
        return False
    drift = compute_anchor_drift_pct(direction, planned_entry, effective_entry)
    if drift is None or drift <= float(threshold_pct):
        return False
    date = _date_str(now_et)
    ts = _iso_ts(now_et)
    if date is None or ts is None:
        return False
    direction_norm = _norm_direction(direction)
    key = (_RULE_ANCHOR, date, f"inst:{inst_id}" if inst_id not in (None, "") else str(symbol))
    if not _claim(key):
        return False

    row: Dict[str, Any] = {
        "ts": ts,
        "date": date,
        "schema": SCHEMA,
        "kind": _RULE_ANCHOR,
        "rule": "planned_anchor_drift_3pct",
        "symbol": str(symbol),
        "direction": direction_norm,
        "inst_id": inst_id,
        "decision_id": str(decision_id or ""),
        "planned_entry": round(_num(planned_entry), 4),
        "planned_entry_source": str(planned_entry_source or ""),
        "effective_entry": round(_num(effective_entry), 4),
        "current_price": (round(_num(current_price), 4)
                          if _num(current_price) is not None else None),
        "drift_pct": drift,
        "threshold_pct": float(threshold_pct),
        "would_skip": True,
        "execution_blocked": False,
        "caveat": (
            "record-only 候选识别: 3% 只是候选阈值, 不是生产 chase/anchor 规则; "
            "不拦截、不改下单参数、不计算反事实 PnL。"
        ),
    }
    ok = append_row(row, path=path, logger=logger)
    if not ok:
        _release(key)
    return ok


def _seconds_between(start: Any, end: Any) -> Optional[float]:
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(str(start))
        end_dt = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return None
    try:
        if (start_dt.tzinfo is None) != (end_dt.tzinfo is None):
            # 一边 aware 一边 naive: 按有 tz 的那侧补齐, 让差值仍是同一墙钟口径。
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=end_dt.tzinfo)
            else:
                end_dt = end_dt.replace(tzinfo=start_dt.tzinfo)
        return (end_dt - start_dt).total_seconds()
    except Exception as error:  # pragma: no cover - 防御极端 tz 对象
        _warn_once("latency", f"[EarlyRiskShadow] latency 计算失败: {error}", error)
        return None


def record_queue_candidate(
    *,
    symbol: Any,
    direction: Any,
    confirmed_at: Any,
    execution_time: Any,
    now_et: Any,
    inst_id: Any = None,
    decision_id: Any = "",
    arm_delay_actual_sec: Any = None,
    latency_sec: Any = None,
    execution_time_source: str = "execution_time",
    pb_cfg: Any = None,
    logger: Any = None,
    path: Any = None,
) -> bool:
    """确认后排队候选 — 只测量 latency, 不立即下单、不改执行顺序。"""
    if not enabled(pb_cfg) or is_test_symbol(symbol):
        return False
    latency = _num(latency_sec)
    if latency is None:
        latency = _seconds_between(confirmed_at, execution_time)
    if latency is None or latency <= 0:
        return False
    date = _date_str(now_et)
    ts = _iso_ts(now_et)
    if date is None or ts is None:
        return False
    direction_norm = _norm_direction(direction)
    key = (_RULE_QUEUE, date, f"inst:{inst_id}" if inst_id not in (None, "") else str(symbol))
    if not _claim(key):
        return False

    row: Dict[str, Any] = {
        "ts": ts,
        "date": date,
        "schema": SCHEMA,
        "kind": _RULE_QUEUE,
        "rule": "confirm_to_exec_latency",
        "symbol": str(symbol),
        "direction": direction_norm,
        "inst_id": inst_id,
        "decision_id": str(decision_id or ""),
        "confirmed_at": str(confirmed_at or ""),
        "execution_time": str(execution_time or ""),
        "execution_time_source": str(execution_time_source or "execution_time"),
        "latency_sec": round(latency, 3),
        "arm_delay_actual_sec": (_num(arm_delay_actual_sec)
                                 if _num(arm_delay_actual_sec) is not None else None),
        "would_defer_order": False,
        "execution_blocked": False,
        "caveat": (
            "record-only 测量: 只量化确认→执行的排队延迟, 不立即下单、不改执行路径、"
            "不计算反事实 PnL。"
        ),
    }
    ok = append_row(row, path=path, logger=logger)
    if not ok:
        _release(key)
    return ok