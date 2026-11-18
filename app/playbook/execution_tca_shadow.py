"""执行侧 TCA 台账 + 点差/报价年龄门只读影子 (RFC-152 Phase 0)。

零交易行为变更: 只在本轮"真正走到下单调用"时快照到达报价, 执行结束后追加一行
JSONL; 所有判定 (含 provisional_would_block) 只落盘, 不改变 auto_execute 的任何分支。

单位契约 (RFC-152 §3):
  planned_entry / db_entry_price / execution_entry / bid / ask / mid / fill_price:
      美元绝对价
  spread_bps / planned_to_exec_bps / exec_to_fill_bps / planned_to_fill_bps:
      bps (1bp = 0.01% = 0.0001); 方向归一后正数 = 对交易更不利
  quote_age_ms: 毫秒
  session_min: 距 09:30 ET 的分钟数
  atr_pct: 百分数 (2.5 = 2.5%) — 调用方从执行链的分数口径 ×100 后传入
  size: 股; notional: 美元

失败纪律: 任何异常只 warn, 绝不阻断下单 (度量故障不得变成交易故障)。
回滚: playbook.execution_parameters.execution_tca_shadow_enabled=false + 重启。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_TCA_PATH = _ROOT / "data" / "shadow" / "execution_tca.jsonl"
SCHEMA = "execution_tca_v1"

_CFG_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "spread_block_bps": 20.0,
    "quote_age_block_ms": 2000,
}


def tca_config(pb_cfg=None) -> Dict[str, Any]:
    """读 playbook.execution_parameters 的 execution_tca_* 扁平键。

    配置不可读 ⇒ enabled=False (维持零写入, 不静默改变生产行为)。
    """
    cfg = dict(_CFG_DEFAULTS)
    try:
        if pb_cfg is None:
            from app.playbook.config import PlaybookConfig
            pb_cfg = PlaybookConfig()
        enabled = pb_cfg.get("execution_tca_shadow_enabled", None)
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        spread = pb_cfg.get("execution_tca_spread_block_bps", None)
        if spread is not None:
            cfg["spread_block_bps"] = max(0.0, float(spread))
        quote_age = pb_cfg.get("execution_tca_quote_age_block_ms", None)
        if quote_age is not None:
            cfg["quote_age_block_ms"] = max(0.0, float(quote_age))
    except Exception as error:
        logger.warning(f"[TCA] 配置读取失败 → 影子关闭 (不影响交易): {error}",
                       exc_info=True)
        cfg["enabled"] = False
    return cfg


def shadow_enabled(pb_cfg=None) -> bool:
    return bool(tca_config(pb_cfg).get("enabled"))


def _num(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "null"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_dir(direction: Any) -> str:
    value = str(direction or "").upper()
    if value in ("BUY", "LONG"):
        return "LONG"
    if value in ("SELL_SHORT", "SHORT", "SELL"):
        return "SHORT"
    return value or "UNKNOWN"


def deviation_bps(direction: Any, earlier: Any, later: Any) -> Optional[float]:
    """方向归一偏差: 正数 = 对交易更不利 (LONG 更高价/SHORT 更低价)。"""
    start = _num(earlier)
    end = _num(later)
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    if _norm_dir(direction) == "SHORT":
        return round((start - end) / start * 10000.0, 4)
    return round((end - start) / start * 10000.0, 4)


def compute_spread_bps(bid: Any, ask: Any, book_is_proxy: bool = False) -> Optional[float]:
    """双边报价点差 (bps)。proxy/交叉/缺失/非正 ⇒ None (不伪造)。"""
    if book_is_proxy:
        return None
    best_bid = _num(bid)
    best_ask = _num(ask)
    if best_bid is None or best_ask is None:
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return None
    return round((best_ask - best_bid) / mid * 10000.0, 4)


def extract_quote(ctx: Optional[Dict[str, Any]], quote_source: str) -> Dict[str, Any]:
    """从 market context / get_quote 提取双边报价。缺失字段一律 None。"""
    out: Dict[str, Any] = {
        "bid": None, "ask": None, "mid": None, "spread_bps": None,
        "quote_age_ms": None, "book_is_proxy": False,
        "quote_source": quote_source or "missing",
    }
    if not isinstance(ctx, dict):
        return out
    book_is_proxy = (ctx.get("lv3_book_is_proxy") is True
                     or ctx.get("book_is_proxy") is True)
    bid = _num(ctx.get("lv3_bid") or ctx.get("bid"))
    ask = _num(ctx.get("lv3_ask") or ctx.get("ask"))
    quote_age = _num(ctx.get("lv3_quote_age_ms"))
    if quote_age is None:
        quote_age = _num(ctx.get("quote_age_ms"))
    mid = None
    if not book_is_proxy and bid is not None and ask is not None and bid > 0 and ask >= bid:
        mid = round((bid + ask) / 2.0, 6)
    out.update({
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_bps": compute_spread_bps(bid, ask, book_is_proxy),
        "quote_age_ms": int(quote_age) if quote_age is not None else None,
        "book_is_proxy": bool(book_is_proxy),
    })
    return out


def _raw_instruction(inst: Dict[str, Any]) -> Dict[str, Any]:
    raw = inst.get("raw_instruction_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def resolve_planned_entry(inst: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """生成侧计划锚 (不拿 reprice 后的 DB 值冒充)。

    优先级: raw.planned_entry > raw.confirmation.price_level > missing。
    """
    raw = _raw_instruction(inst)
    for key, source in (
        ("planned_entry", "raw.planned_entry"),
    ):
        value = _num(raw.get(key))
        if value is not None and value > 0:
            return value, source
    confirmation = raw.get("confirmation")
    if isinstance(confirmation, dict):
        level = confirmation.get("price_level")
        if isinstance(level, str):
            level = level.replace(",", "")
        value = _num(level)
        if value is not None and value > 0:
            return value, "confirmation.price_level"
    return None, "missing"


def _entry_archetype(inst: Dict[str, Any]) -> Optional[str]:
    arch = _raw_instruction(inst).get("entry_archetype")
    return str(arch).upper() if arch else None


def _source(decision_id: Any) -> str:
    value = str(decision_id or "")
    if value.startswith("universe_expansion"):
        return "UE"
    if value.startswith("DA-"):
        return "DA"
    return "OTHER"


def _session_min(now_et) -> Optional[int]:
    try:
        open_at = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        return int((now_et - open_at).total_seconds() // 60)
    except Exception:
        return None


def build_attempt_row(
    *,
    inst: Dict[str, Any],
    direction: Any,
    planned_entry: Optional[float],
    planned_entry_source: str,
    execution_entry: Any,
    ctx: Optional[Dict[str, Any]],
    quote_source: str,
    now_et,
    size: Any,
    notional: Any,
    atr_pct: Any = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """构建一行待收口的 TCA 记录; 影子关闭/TEST 标的 ⇒ None。"""
    cfg = cfg if cfg is not None else tca_config()
    if not cfg.get("enabled"):
        return None
    symbol = str(inst.get("symbol") or "")
    if not symbol or "TEST" in symbol.upper():
        return None
    quote = extract_quote(ctx, quote_source)
    reasons = []
    spread = quote.get("spread_bps")
    quote_age = quote.get("quote_age_ms")
    if spread is not None and spread >= float(cfg.get("spread_block_bps") or 0.0):
        reasons.append("spread")
    if quote_age is not None and quote_age >= int(cfg.get("quote_age_block_ms") or 0):
        reasons.append("quote_age")

    row: Dict[str, Any] = {
        "schema": SCHEMA,
        "ts_et": now_et.isoformat(timespec="seconds"),
        "date": now_et.strftime("%Y-%m-%d"),
        "inst_id": inst.get("id"),
        "decision_id": str(inst.get("decision_id") or ""),
        "symbol": symbol,
        "direction": _norm_dir(direction),
        "source": _source(inst.get("decision_id")),
        "entry_archetype": _entry_archetype(inst),
        "planned_entry": _num(planned_entry),
        "planned_entry_source": planned_entry_source,
        "db_entry_price": _num(inst.get("entry_price")),
        "execution_entry": _num(execution_entry),
        **quote,
        "session_min": _session_min(now_et),
        "atr_pct": _num(atr_pct),
        "size": _num(size),
        "notional": _num(notional),
        "provisional_would_block": bool(reasons),
        "provisional_block_reasons": reasons,
        "success": None,
        "executed": None,
        "fill_price": None,
        "planned_to_exec_bps": deviation_bps(direction, planned_entry, execution_entry),
        "exec_to_fill_bps": None,
        "planned_to_fill_bps": None,
        "message": None,
        "error": None,
        "skip_reason": inst.get("skip_reason"),
    }
    return row


def finalize_and_record(
    row: Optional[Dict[str, Any]],
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    path: Optional[Path] = None,
) -> bool:
    """收口执行结果并追加 JSONL。永不上抛。

    path 仅用于测试/回灌注入 (默认 `_TCA_PATH`); 生产调用点不传。
    """
    if not isinstance(row, dict):
        return False
    try:
        if isinstance(result, dict):
            success = bool(result.get("success"))
            row["success"] = success
            row["executed"] = success
            if success:
                # 成功行不得带前几轮拦截的旧 skip_reason (2026-10-20 RIVN/LITE 实锤):
                # 指令可能先被 chase_cap/falsification 拦过, 后续轮次才成交; 旧标签会让
                # 报表把成功成交误读成"被拦单"。保留下车原因到 prior_skip_reason。
                if row.get("skip_reason"):
                    row["prior_skip_reason"] = row["skip_reason"]
                    row["skip_reason"] = None
                fill = _num(result.get("execution_price"))
                row["fill_price"] = fill
                row["exec_to_fill_bps"] = deviation_bps(
                    row.get("direction"), row.get("execution_entry"), fill)
                row["planned_to_fill_bps"] = deviation_bps(
                    row.get("direction"), row.get("planned_entry"), fill)
            else:
                row["message"] = str(result.get("message") or "")[:500]
                if result.get("skip_reason"):
                    row["skip_reason"] = result.get("skip_reason")
            if error:
                row["error"] = str(error)[:500]
        else:
            row["success"] = False
            row["executed"] = False
            row["error"] = str(error or "no_result")[:500]
        _append_row(row, path)
        return True
    except Exception as _error:
        logger.warning(f"[TCA] 收口写盘失败 (不影响交易): {_error}", exc_info=True)
        return False


def _append_row(row: Dict[str, Any], path: Optional[Path] = None) -> None:
    target = Path(path) if path else _TCA_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
