from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from datetime import datetime
from pathlib import Path
from app.utils.timezone import ET, _localize

from app.utils.symbol_utils import normalize_symbol

POLICY_VERSION = "da_ue_v1"
FUNNEL_PATH = Path(__file__).resolve().parents[2] / "data/shadow/candidate_funnel.jsonl"
_ET = ET
_SYMBOL = re.compile(r"US\.[A-Z]{1,6}(?:[.-][A-Z])?\Z")
# 真实存在但**不在本策略可交易宇宙**的形状: 权证/优先股/存托份额等多字母后缀。
#   实测 (2026-09-12, data/shadow/llm_universe_scan*.jsonl 近 901 次浮现):
#   US.BBBY-WT / US.PSA-PK / US.CNO-PA / US.TDS-PV 共 4 次 (0.44%)。
#   仍一律拒绝 (不交易权证/优先股), 但原因码必须说清"是不交易的证券类型",
#   不是"标的写错了" —— 资格/预算/否决/仲裁分阶段可归因, 不混成一个 SKIP (P1.1)。
_SYMBOL_UNSUPPORTED_SHAPE = re.compile(r"US\.[A-Z0-9]{1,6}[.-][A-Z0-9]{1,4}\Z")
logger = logging.getLogger(__name__)


def positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def canonical_symbol(value):
    symbol = normalize_symbol(value) or ""
    return symbol if symbol.startswith("US.") else f"US.{symbol}"


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()[:24]


def make_batch_id(source, now):
    return f"{source}:{_fingerprint([source, now.isoformat()])}"


def require_auto_sim():
    from app.core.trading_mode import read_startup_mode_snapshot, validate_startup_mode_config

    snapshot = read_startup_mode_snapshot()
    mode = validate_startup_mode_config(
        snapshot["AUTO_TRADE_ACCOUNT"], snapshot["TRADE_ENV"],
        snapshot["ALLOW_REAL_TRADING"], snapshot["FTSEEK_DB_FILE"],
    )
    if not mode["valid"] or mode["broker_route"] != "sim" or mode["db_route"] != "sim" or mode["allow_real_trading"]:
        raise RuntimeError("candidate optimization requires automatic SIM broker and SIM ledger")
    for key, expected in (("AUTO_TRADE_ACCOUNT", "sim"), ("TRADE_ENV", "1"),
                          ("ALLOW_REAL_TRADING", "false")):
        actual = os.getenv(key, "").strip().lower()
        if actual and actual != expected:
            raise RuntimeError(f"candidate optimization process/file mode mismatch: {key}")
    return mode


def load_candidate_policy(pb_cfg=None):
    from app.playbook.admission import get_admission_config
    from app.playbook.config import get_playbook_config

    cfg = pb_cfg if pb_cfg is not None else get_playbook_config()
    admission = get_admission_config()
    return {
        "version": POLICY_VERSION,
        "min_price": float(admission.get("min_entry_price", 5.0)),
        "low_atr_enabled": bool(cfg.get("low_atr_gate_enabled", False)),
        "min_atr_ratio": float(cfg.get("low_atr_gate_min_atr_pct", 0.03)),
        "max_scan_age_hours": float(cfg.get("candidate_scan_max_age_hours", 3.0)),
        "max_quote_age_sec": float(cfg.get("entry_max_quote_age_sec", 90.0)),
        "max_spread_ratio": float(cfg.get("pretrade_max_spread_pct", 0.05)),
        "quote_gate_enabled": bool(cfg.get("pretrade_quote_gate_enabled", True)),
        "borrow_gate_enabled": bool(cfg.get("borrow_pool_gate_enabled", False)),
    }


def evaluate_candidate(snapshot, policy):
    reasons, missing = [], []
    symbol_text = str(snapshot.get("symbol", ""))
    if not _SYMBOL.fullmatch(symbol_text):
        reasons.append("unsupported_security_shape"
                       if _SYMBOL_UNSUPPORTED_SHAPE.fullmatch(symbol_text)
                       else "invalid_symbol")
    price = positive_number(snapshot.get("price"))
    if price is None:
        missing.append("price")
    elif price < policy.get("min_price", 0):
        reasons.append("price_floor")
    if policy.get("low_atr_enabled"):
        atr = positive_number(snapshot.get("screening_atr_ratio"))
        if atr is None:
            missing.append("screening_atr_ratio")
        elif atr < policy["min_atr_ratio"]:
            reasons.append("low_atr")
    quote_age = snapshot.get("quote_age_sec")
    if quote_age is None:
        missing.append("source_asof")
    elif quote_age < -5:
        reasons.append("future_quote")
    elif quote_age > policy.get("max_quote_age_sec", 90):
        reasons.append("stale_quote")
    if policy.get("quote_gate_enabled"):
        bid, ask = positive_number(snapshot.get("bid")), positive_number(snapshot.get("ask"))
        if bid is None or ask is None:
            missing.append("bid_ask")
        elif ask < bid:
            reasons.append("crossed_quote")
        elif (ask - bid) / ((ask + bid) / 2) > policy.get("max_spread_ratio", 0.05):
            reasons.append("wide_spread")
    if policy.get("borrow_gate_enabled") and snapshot.get("direction_hint") in ("SHORT", "SELL_SHORT"):
        if snapshot.get("shortable") is False:
            reasons.append("not_shortable")
        elif snapshot.get("shortable") is not True:
            missing.append("shortable")
    return {
        "status": "REJECT" if reasons else ("UNKNOWN" if missing else "PASS"),
        "reasons": reasons,
        "missing_fields": missing,
    }


def _source_time(quote, now):
    raw = quote.get("source_asof") or quote.get("update_time")
    if not raw:
        return None, None
    try:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            stamp = datetime.fromtimestamp(raw, _ET)
        else:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = _localize(stamp, _ET)
        return stamp.isoformat(), (now - stamp).total_seconds()
    except (ValueError, TypeError, OverflowError, OSError):
        return None, None


def _quote_price(quote, now):
    hm = now.hour * 100 + now.minute
    if 400 <= hm < 930:
        return positive_number(quote.get("pre_price"))
    for key in ("price", "last_price", "close"):
        price = positive_number(quote.get(key))
        if price is not None:
            return price
    return None


def screen_candidates(candidates, router, *, source, batch_id, now=None, quotes=None,
                      policy=None, atr_provider=None):
    """采集快照 + 纯资格判定 (IO 与规则分离, 行情/时间/策略均可注入测试)。

    Args:
        candidates: 候选行 (需含 symbol/code; direction|direction_hint 可选;
                    `effective_price` 存在时作为参考价, 保证筛选价 == 调用方锚价)
        router:     行情路由 (仅在 quotes=None 时用于批量取价)
        source:     来源标签 (DA / UE / ...) — 审计分源, 不改变判定
        batch_id:   批次标识, 参与 candidate_id
        now:        带时区观测时点 (naive 直接拒绝, 不猜时区)
        quotes:     预取报价 {symbol|US.symbol: {...}}; 传入即不再取价
        policy:     load_candidate_policy() 结果 (可注入)
        atr_provider: 复用调用方已构建的 ATRProvider (与执行门同源同实例);
                    None 时按 router 自建 (筛选口径 get_screening_atr, 非止损口径)
    """
    from app.core.atr_provider import ATRProvider

    now = now or datetime.now(_ET)
    if now.tzinfo is None:
        raise ValueError("candidate timestamp must include a timezone")
    now = now.astimezone(_ET)
    policy = policy if policy is not None else load_candidate_policy()
    symbols = list(dict.fromkeys(canonical_symbol(c.get("symbol") or c.get("code")) for c in candidates))
    supplied_quotes = quotes is not None
    if quotes is None:
        try:
            valid_symbols = [s for s in symbols if _SYMBOL.fullmatch(s)]
            quotes = router.get_batch_quotes(valid_symbols) if router and valid_symbols else {}
        except Exception as exc:
            logger.warning("candidate batch quotes unavailable: %s", exc, exc_info=True)
            quotes = {}
    quotes = {canonical_symbol(k): v for k, v in (quotes or {}).items() if isinstance(v, dict)}
    provider = atr_provider if atr_provider is not None else ATRProvider(router)
    # 2026-09-24 (切实盘前置遗留 P2): 原硬编码 "sim" — 切实盘后 screening 标签失真。
    # 改用 config_container._resolve_trade_mode (TRADE_ENV SSOT, 解析失败安全回落
    # SIMULATE)。词汇保持 "sim"/"live": 下游 daily_ic_analysis 按 trade_mode 分桶,
    # 历史行全是 "sim", SIM 环境输出不变 ⇒ 桶连续性不断。
    from app.core.config_container import TradeMode, _resolve_trade_mode
    _trade_mode_label = "sim" if _resolve_trade_mode() == TradeMode.SIMULATE else "live"
    atr_cache, results = {}, []
    for candidate in candidates:
        row = dict(candidate)
        symbol = canonical_symbol(row.get("symbol") or row.get("code"))
        quote = quotes.get(symbol, {})
        price = _quote_price(quote, now)
        price_source = quote.get("source") or quote.get("data_source")
        # 调用方已按自身口径 (如 UE 盘前锚定 pre_price / 缺 pre_price 不回落昨收)
        # 定出有效参考价时, 资格判定必须用同一价格 —— 否则筛选价与指令锚价分叉,
        # 审计上"筛过的价"与"写进指令的价"不是同一个数。
        override_price = positive_number(row.get("effective_price"))
        if override_price is not None:
            price = override_price
            price_source = f"{price_source or 'unknown'}+caller_effective"
        if not supplied_quotes and not quote:
            price = positive_number(row.get("price") or row.get("last_price"))
            price_source = "candidate_unverified"
        if symbol not in atr_cache:
            # atr_reason 必须说清"为什么没有筛选 ATR": 形状不支持 / 门关着 / 取数失败
            #   是三件不同的事 —— 此前统一写 invalid_symbol, 低ATR门关闭时连合法标的
            #   也被标成"标的无效", 审计上会把配置状态误读成数据质量。
            if not _SYMBOL.fullmatch(symbol):
                atr_cache[symbol] = {"atr": None, "status": "UNKNOWN",
                                     "reason": "symbol_shape_unsupported"}
            elif not policy.get("low_atr_enabled"):
                atr_cache[symbol] = {"atr": None, "status": "UNKNOWN",
                                     "reason": "low_atr_gate_disabled"}
            else:
                try:
                    atr_cache[symbol] = provider.get_screening_atr(symbol, asof=now)
                except Exception as exc:
                    atr_cache[symbol] = {"atr": None, "status": "UNKNOWN", "reason": "atr_unavailable"}
                    logger.warning("candidate ATR unavailable for %s: %s", symbol, exc,
                                   exc_info=True)
        atr = atr_cache[symbol]
        atr_value = positive_number(atr.get("atr")) if atr.get("status") == "PASS" else None
        source_asof, age = _source_time(quote, now)
        direction = str(row.get("direction") or row.get("direction_hint") or "").upper()
        facts = {
            "symbol": symbol, "price": price, "quote_source": price_source,
            "source_asof": source_asof, "quote_age_sec": age,
            "bid": positive_number(quote.get("bid_price") or quote.get("bid")),
            "ask": positive_number(quote.get("ask_price") or quote.get("ask")),
            "screening_atr_ratio": atr_value / price if atr_value is not None and price is not None else None,
            "screening_atr": atr_value, "atr_basis": atr.get("basis"),
            "atr_source": atr.get("source"), "atr_adjustment": atr.get("adjustment"),
            "atr_reason": atr.get("reason"), "bars_asof": atr.get("bars_asof"),
            "direction_hint": direction,
            "shortable": row.get("shortable") if isinstance(row.get("shortable"), bool) else None,
        }
        verdict = evaluate_candidate(facts, policy)
        fingerprint_facts = {k: v for k, v in facts.items() if k != "quote_age_sec"}
        # fetched_at = 行情抓取时刻 (源侧 timestamp), 与 source_asof (行情所属时刻) 分开;
        # 缺源侧时间戳时退化为本次筛选时刻, 但仍不冒充 source_asof。
        fetched_at = now.isoformat()
        raw_stamp = quote.get("timestamp")
        if isinstance(raw_stamp, (int, float)) and not isinstance(raw_stamp, bool):
            try:
                fetched_at = datetime.fromtimestamp(float(raw_stamp), _ET).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        screening = {
            **facts, **verdict, "policy_version": POLICY_VERSION,
            "input_hash": _fingerprint([fingerprint_facts, policy]),
            "candidate_id": f"{batch_id}:{symbol}:{direction or 'UNDECIDED'}",
            "batch_id": batch_id, "source": source, "trade_mode": _trade_mode_label,
            "observed_at": now.isoformat(), "fetched_at": fetched_at,
            "policy": dict(policy),
        }
        row["candidate_screening"] = screening
        results.append(row)
    return results


def select_candidates(screened, budget, *, allow_unknown=True, already_selected=None,
                      sort_key=None, defer_reason=None):
    """在合格集合内按预算选取 —— "先过滤后预算", 淘汰即从剩余合格队列补位。

    这是修正"先截断再过滤 → 淘汰后不补位"的单一入口 (P1.1/P1.4/P1.5)。
    纯函数: 不做 IO, 不读配置, 名次与预算全部由调用方显式给定。

    Args:
        screened: `screen_candidates()` 的输出 (每行含 `candidate_screening`)
        budget:   本阶段可消费名额 (>=0)。分析名额不是成交配额, 允许 0 选取。
        allow_unknown: True = UNKNOWN 仍可进入研究/LLM 阶段 (缺数 ≠ 拒绝,
                       执行阶段仍受原风险门约束); False = 只放行 PASS。
        already_selected: 已占名额的 symbol 集合 (跨阶段/跨来源去重 ——
                       重复项不占两个名额)。
        sort_key: 可选显式排序键 (如 DA 空头按 10-score 的方向强度);
                  默认保持输入顺序 (不改变来源既有排序含义),
                  同键以 (原序, symbol) 稳定 tie-break。
        defer_reason: 可选纯谓词 `fn(row) -> str|None`。返回字符串 = 该行**本轮不可
                  消费该预算** (如 UE 写库名额: 无有效价格 ⇒ 产不出可执行指令),
                  字符串即记为它的 selection_status (调用方命名, 如
                  "deferred_no_price"); 返回 None = 正常参与选取。
                  与资格判定分层: 资格 UNKNOWN ≠ 拒绝 (研究可继续), 但预算只发给
                  能真正产出下一步动作的候选 —— 否则名额被空耗, 合格队列补不上位。

    Returns:
        被选取的行 (原 dict 引用, 已就地写 `selection_status` / `selection_rank`)。
        未选中的行同样被标注: rejected_ineligible / unknown_deferred / duplicate /
        budget_cut —— 四态分开记录, 不混成一个 SKIP。
    """
    rows = list(screened or [])
    consumed = {canonical_symbol(s) for s in (already_selected or set())}
    indexed = list(enumerate(rows))
    if sort_key is not None:
        indexed.sort(key=lambda pair: (
            sort_key(pair[1]), pair[0],
            canonical_symbol(pair[1].get("symbol") or pair[1].get("code")),
        ))
    try:
        budget = max(0, int(budget))
    except (TypeError, ValueError):
        budget = 0
    selected = []
    for _, row in indexed:
        screening = row.get("candidate_screening")
        screening = dict(screening) if isinstance(screening, dict) else {}
        status = str(screening.get("status") or "UNKNOWN").upper()
        symbol = canonical_symbol(row.get("symbol") or row.get("code"))
        row.pop("selection_rank", None)
        deferred = str(defer_reason(row) or "") if defer_reason is not None else ""
        if status == "REJECT":
            row["selection_status"] = "rejected_ineligible"
        elif status != "PASS" and not allow_unknown:
            row["selection_status"] = "unknown_deferred"
        elif deferred:
            row["selection_status"] = deferred
        elif symbol in consumed:
            row["selection_status"] = "duplicate"
        elif len(selected) < budget:
            row["selection_status"] = "selected"
            row["selection_rank"] = len(selected) + 1
            consumed.add(symbol)
            selected.append(row)
        else:
            row["selection_status"] = "budget_cut"
        screening["selection_status"] = row["selection_status"]
        if "selection_rank" in row:
            screening["selection_rank"] = row["selection_rank"]
        row["candidate_screening"] = screening
    return selected


def selection_summary(screened):
    """按 selection_status 计数 + 拒绝原因分布 (漏斗审计/日志用, 纯函数)。"""
    summary = {"total": 0, "selected": 0, "budget_cut": 0, "duplicate": 0,
               "rejected_ineligible": 0, "unknown_deferred": 0, "evaluated": 0,
               "reason_codes": {}}
    for row in screened or []:
        summary["total"] += 1
        status = str(row.get("selection_status") or "evaluated")
        summary[status] = summary.get(status, 0) + 1
        screening = row.get("candidate_screening")
        if isinstance(screening, dict):
            for code in screening.get("reasons") or []:
                summary["reason_codes"][code] = summary["reason_codes"].get(code, 0) + 1
    return summary


def get_instruction_screening(inst):
    direct = inst.get("candidate_screening")
    if isinstance(direct, dict):
        return direct
    raw = inst.get("raw_instruction") or inst.get("raw_instruction_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    screening = raw.get("candidate_screening") if isinstance(raw, dict) else None
    return screening if isinstance(screening, dict) else None


def record_candidate_batch(candidates, *, batch_id, source, phase, path=None):
    import fcntl

    records = []
    for rank, candidate in enumerate(candidates, 1):
        screening = candidate.get("candidate_screening")
        if not isinstance(screening, dict):
            continue
        records.append({**screening, "rank": rank,
                        "selection_status": candidate.get("selection_status", "evaluated")})
    payload = {"type": "candidate_batch", "batch_id": batch_id, "source": source,
               "phase": phase, "policy_version": POLICY_VERSION, "candidates": records}
    target = Path(path) if path is not None else FUNNEL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_UN)
