#!/usr/bin/env python3
"""RFC-207 §2 · 每日 EventContext record-only 影子（只读共享简报）

用途
----
在 ET 交易日盘前（DA 主批之后）把「事件情报 + DA raw checkpoint 证据」合成一份
**共享 MarketEventBrief**，落到 `data/shadow/event_intelligence/live/`，供人工复盘 /
后续 DEB 使用。它是 record-only 观测面，不是交易指令：

  - 不写生产 DB / playbook / config / cron / notifier / orders；
  - 不改变任何 direction / Gate / 仓位 / 退出；
  - 不计算 PnL 或反事实收益。

输入（全部只读）
----------------
  - `data/da_checkpoint/<ET date>/US.*.raw.json`：当日 target symbols + 补充证据；
  - `data/event_stream.db::canonical_events`：`first_seen_at` 在
    `[as_of-24h, as_of]` 的规范事件（`mode=ro`，不构造 EventStore，避免 CREATE TABLE）；
  - `app/event_stream` 既有快照/简报生成器（零生产消费者，本脚本是离线消费者）。

开关与付费闸
------------
  - `DA_EVENT_CONTEXT_SHADOW`（`scripts/shadow_runtime.env_flag`，缺省 false，每次实读）。
    false ⇒ 打印 `DISABLED` 并返回 0，除读 flag 外零 DB/LLM/文件写入。
  - 真实付费调用须**同时**满足 env flag=true 与 CLI `--execute`；否则是零付费 dry-run，
    只打印即将写入的预览，不落盘。
  - 单次生成最多 2 次 LLM（主分析 1 次 + 挑战者 1 次，由
    `generate_market_event_brief` 的既有结构决定）。

幂等与不可变证据
----------------
  - `input_hash` 只覆盖**证据内容**（ET 日、event_hash、supplemental evidence_hash、
    target universe、模型与预算），不含精确 as_of 时刻 ⇒ 同一交易日内证据没变的重跑
    不会重复付费。
  - `.../live/latest.json` 原子更新；`.../live/<date>/<input_hash>.json` 用 O_EXCL 新建，
    永不覆盖旧 run。
  - 相同 input_hash 且已有 brief 有效 ⇒ `ALREADY_CURRENT`，不调 LLM。
  - 相同 input_hash 的旧 run 无效 ⇒ 失败关闭 `ERROR`（不覆盖、不重复付费），
    需人工检查证据或改变输入后再跑。

Pre-DA 兜底（无当日 raw checkpoint）
-----------------------------------
  当日 `US.*.raw.json` 尚不存在（DA 主批未跑）时，缺省仍是 `NO_DATA`。显式加
  `--allow-no-checkpoint` 才启用兜底：

  - target_universe 取 `app.utils.watchlist_manager.get_stock_codes()`（watchlist，
    记录 `target_source=watchlist`），或用 `--target-symbols NVDA,AMD` 最小覆盖
    （记录 `target_source=cli_target_symbols`）；
  - 允许 0 个当日 raw 文件，只走 EventStore 快照；
  - supplemental 完全关闭（不读任何 raw 文件，`supplemental_status=disabled_no_checkpoint`,
    `supplemental_merged_fact_count=0`），**绝不把前一日 raw 当作当日证据**；
  - 若当日 raw 存在，则仍走原 checkpoint 路径（`target_source=checkpoint`），
    `--target-symbols` 不生效并在报告里记 `target_override_ignored=true`。

CLI
---
    ./venv/bin/python scripts/da_event_context_shadow.py                  # 零付费 dry-run
    ./venv/bin/python scripts/da_event_context_shadow.py --execute        # 付费（需 flag）
    ./venv/bin/python scripts/da_event_context_shadow.py \\
        --date 2026-11-12 --as-of 2026-11-12T21:00:00Z [--execute]        # 温室回放
    ./venv/bin/python scripts/da_event_context_shadow.py --execute \\
        --allow-no-checkpoint [--target-symbols NVDA,AMD]                # Pre-DA 兜底

退出码：DISABLED / DRY_RUN / NO_DATA / OK / ALREADY_CURRENT = 0；ERROR = 2。

评估器：`scripts/da_event_context_shadow_eval.py`。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.shadow_runtime import (  # noqa: E402
    atomic_write_json,
    env_flag,
    read_json,
    sha256_json,
    write_json_new,
)

FLAG = "DA_EVENT_CONTEXT_SHADOW"
SCHEMA = "da_event_context_shadow_run_v1"
REPORT_SCHEMA = "da_event_context_shadow_latest_v1"
DEFAULT_DB = ROOT / "data" / "event_stream.db"
DEFAULT_CHECKPOINT_ROOT = ROOT / "data" / "da_checkpoint"
DEFAULT_OUT_ROOT = ROOT / "data" / "shadow" / "event_intelligence" / "live"
ET = ZoneInfo("America/New_York")
LOOKBACK_HOURS = 24
MAX_BRIEF_EVENTS = 48
MAX_SUPPLEMENTAL_ITEMS = 48
MAX_LLM_CALLS = 2
CAVEAT = (
    "record-only 研究产物: 不是交易指令; 不写生产 DB/playbook/config/cron/notifier/orders; "
    "不含 PnL 或反事实收益; 受益/受损标的是待 DA 复核的研究假设。"
)


# ---------------------------------------------------------------------------
# 时间 / 路径 / 开关
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _enabled() -> bool:
    return env_flag(FLAG)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_ts(value: str, *, field: str = "timestamp") -> datetime:
    from app.event_stream.intelligence import parse_timestamp

    try:
        return parse_timestamp(value, require_timezone=True)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} requires an ISO timestamp with timezone: {value!r}") from exc


def _parse_date(value: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ValueError(f"--date must be YYYY-MM-DD: {value!r}") from exc


def _display_path(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except (OSError, ValueError):
        return str(path)


def _resolve_llm_call():
    """真实付费出口；测试必须 monkeypatch 本函数，绝不能打真 API。"""
    from scripts.da_event_intelligence import _dashscope_call

    return _dashscope_call


# ---------------------------------------------------------------------------
# 只读输入
# ---------------------------------------------------------------------------


def discover_checkpoints(
    checkpoint_dir: Path, date_et: str
) -> Tuple[List[str], List[Dict[str, str]]]:
    """从当日 raw checkpoint 推断 target symbols；错日/坏档显式排除并留痕。"""
    if not checkpoint_dir.is_dir():
        return [], []
    symbols: List[str] = []
    excluded: List[Dict[str, str]] = []
    for path in sorted(checkpoint_dir.glob("US.*.raw.json")):
        name = path.name
        if not name.startswith("US.") or not name.endswith(".raw.json"):
            continue
        symbol = name[len("US."):-len(".raw.json")].upper()
        if not symbol:
            continue
        payload = read_json(path)
        if not isinstance(payload, dict):
            excluded.append({
                "symbol": symbol, "reason": "checkpoint_unreadable",
                "path": _display_path(path),
            })
            continue
        trade_date = str(payload.get("trade_date") or "").strip()
        if trade_date and trade_date != date_et:
            excluded.append({
                "symbol": symbol, "reason": f"trade_date_mismatch:{trade_date}",
                "path": _display_path(path),
            })
            continue
        symbols.append(symbol)
    return sorted(set(symbols)), excluded


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper().removeprefix("US.")


def parse_target_symbols(value: str) -> List[str]:
    """解析 `--target-symbols`（逗号分隔，接受 `US.` 前缀）；空串 => []。"""
    symbols = {
        symbol for raw in str(value or "").split(",")
        if (symbol := _normalize_symbol(raw))
    }
    return sorted(symbols)


def watchlist_target_symbols() -> List[str]:
    """Pre-DA 兜底 target universe：只读 watchlist，不碰任何 raw checkpoint。"""
    from app.utils.watchlist_manager import get_stock_codes

    codes = get_stock_codes() or []
    return sorted({symbol for code in codes if (symbol := _normalize_symbol(code))})


def build_snapshot(db_path: Path, *, as_of: datetime, lookback_hours: int) -> Dict[str, Any]:
    from app.event_stream.intelligence import (
        build_event_snapshot,
        read_event_records,
        read_source_health,
    )
    from app.event_stream.source_registry import default_source_registry
    from app.event_stream.market_brief import validate_event_snapshot

    since = as_of - timedelta(hours=lookback_hours)
    records = read_event_records(db_path, since=_iso(since), until=_iso(as_of))
    source_health = read_source_health(db_path)
    snapshot = build_event_snapshot(
        records,
        as_of=_iso(as_of),
        since=_iso(since),
        source_health=source_health,
        expected_sources=sorted(default_source_registry()),
    )
    errors = validate_event_snapshot(snapshot)
    if errors:
        raise ValueError(f"event snapshot grounding failed: {errors[:8]}")
    return snapshot


def build_context(
    snapshot: Dict[str, Any],
    *,
    checkpoint_dir: Path,
    target_symbols: Sequence[str],
    max_events: int,
    supplemental_limit: int,
    supplemental_enabled: bool = True,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """构造 brief context；supplemental 只覆盖 `da_event_supplemental` 已注册的符号。

    `extract_supplemental_evidence` 的 `case_symbols` 只能取 `_SYMBOL_PATTERNS` 内已注册
    的符号（未知符号会 KeyError）。DA raw checkpoint 目录通常覆盖整个 watchlist，
    故 target_universe 取全部当日 raw 符号，supplemental 取「当日有档 ∩ 已注册符号」，
    与既有 `scripts/da_event_intelligence.py::_supplemental_symbols` 口径一致。

    `supplemental_enabled=False` 用于 Pre-DA 无当日 checkpoint 的兜底：直接跳过
    supplemental，确保不会读取任何（含前一日）raw 文件。
    """
    from app.event_stream.market_brief import build_brief_context

    if supplemental_limit < 0:
        raise ValueError("supplemental_limit must be >= 0")
    context = build_brief_context(
        snapshot, limit=max_events, target_universe=target_symbols,
    )
    if not supplemental_enabled:
        # 兜底路径到此为止：连 supplemental 模块都不加载，杜绝读 raw。
        return context, None

    from scripts.da_event_supplemental import (
        CASE_SYMBOLS,
        extract_supplemental_evidence,
        merge_supplemental_into_context,
    )

    registered = set(CASE_SYMBOLS)
    supplemental_symbols = sorted({
        str(symbol).upper().removeprefix("US.")
        for symbol in target_symbols
        if str(symbol).upper().removeprefix("US.") in registered
    })
    if not supplemental_symbols:
        return context, None
    supplemental = extract_supplemental_evidence(
        checkpoint_dir,
        cutoff=snapshot["as_of"],
        case_symbols=supplemental_symbols,
    )
    context = merge_supplemental_into_context(
        context,
        supplemental,
        cutoff=snapshot["as_of"],
        limit=supplemental_limit,
        case_symbols=supplemental_symbols,
    )
    return context, supplemental


# ---------------------------------------------------------------------------
# 简报有效性（写方与评估器共用的唯一判据）
# ---------------------------------------------------------------------------


def validate_brief_record(
    brief: Any,
    *,
    date_et: Optional[str] = None,
) -> List[str]:
    """校验已落盘的 brief 是否自洽、接地且无未来事件。"""
    from app.event_stream.intelligence import parse_timestamp
    from app.event_stream.market_brief import PROMPT_VERSION, validate_transmissions

    if not isinstance(brief, dict):
        return ["brief_not_object"]
    errors: List[str] = []
    if brief.get("schema") != PROMPT_VERSION:
        errors.append("brief_schema_mismatch")
    if not str(brief.get("brief_id") or ""):
        errors.append("brief_id_missing")
    as_of_dt: Optional[datetime] = None
    try:
        as_of_dt = parse_timestamp(str(brief.get("as_of")), require_timezone=True)
    except (AttributeError, TypeError, ValueError):
        errors.append("brief_as_of_invalid")
    if date_et and as_of_dt is not None:
        if as_of_dt.astimezone(ET).date().isoformat() != date_et:
            errors.append("brief_date_mismatch")
    facts = brief.get("facts")
    if not isinstance(facts, list):
        errors.append("brief_facts_missing")
        facts = []
    transmissions = brief.get("transmissions")
    if not isinstance(transmissions, list):
        errors.append("brief_transmissions_missing")
        transmissions = []
    stored_errors = brief.get("validation_errors")
    if not isinstance(stored_errors, list):
        errors.append("brief_validation_errors_missing")
    else:
        errors.extend(str(item) for item in stored_errors)
    llm_calls = brief.get("llm_calls")
    if not isinstance(llm_calls, int) or isinstance(llm_calls, bool) or not 0 < llm_calls <= MAX_LLM_CALLS:
        errors.append("brief_llm_calls_invalid")
    market_evidence = brief.get("market_evidence")
    if not isinstance(market_evidence, list):
        errors.append("brief_market_evidence_missing")
        market_evidence = []
    if transmissions:
        try:
            errors.extend(validate_transmissions(
                transmissions,
                {"facts": facts, "market_evidence": market_evidence},
                prefix="transmissions",
            ))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"transmission_malformed:{type(exc).__name__}")
    if as_of_dt is not None:
        for fact in facts:
            if not isinstance(fact, dict):
                errors.append("fact_not_object")
                continue
            event_id = str(fact.get("event_id") or "unknown")
            try:
                first_seen = parse_timestamp(
                    str(fact.get("first_seen_at")), require_timezone=True,
                )
            except (AttributeError, TypeError, ValueError):
                errors.append(f"fact_first_seen_invalid:{event_id}")
                continue
            if first_seen > as_of_dt:
                errors.append(f"future_fact:{event_id}")
    return sorted(set(errors))


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------


def _input_components(
    *,
    date_et: str,
    snapshot: Dict[str, Any],
    supplemental: Optional[Dict[str, Any]],
    target_symbols: Sequence[str],
    supplemental_limit: int,
    args: argparse.Namespace,
    target_source: str = "checkpoint",
    supplemental_status: Optional[str] = None,
) -> Dict[str, Any]:
    from app.event_stream.market_brief import PROMPT_VERSION

    components = {
        "schema": SCHEMA,
        "date_et": date_et,
        "event_hash": snapshot["event_hash"],
        "event_count": snapshot["event_count"],
        "supplemental_evidence_hash": (supplemental or {}).get("evidence_hash"),
        "target_universe": sorted(str(item) for item in target_symbols),
        "lookback_hours": args.lookback_hours,
        "max_events": args.max_events,
        "supplemental_limit": supplemental_limit,
        "main_model": args.model,
        "challenger_model": args.challenger_model,
        "prompt_version": PROMPT_VERSION,
    }
    # checkpoint 路径保持原 hash 组件不变（零行为漂移）；兜底路径显式区分证据形态。
    if target_source != "checkpoint":
        components["target_source"] = target_source
        components["supplemental_status"] = supplemental_status
    return components


def _artifact_document(
    *,
    generated_at: datetime,
    date_et: str,
    input_hash: str,
    components: Dict[str, Any],
    snapshot: Dict[str, Any],
    context: Dict[str, Any],
    supplemental: Optional[Dict[str, Any]],
    brief: Dict[str, Any],
    target_symbols: Sequence[str],
    excluded: Sequence[Dict[str, str]],
    checkpoint_dir: Path,
    db_path: Path,
    supplemental_limit: int,
    target_source: str,
    supplemental_status: str,
    args: argparse.Namespace,
    override_ignored: bool = False,
) -> Dict[str, Any]:
    supplemental_context = context.get("supplemental") or {}
    supplemental = supplemental or {}
    merged_fact_count = supplemental_context.get("merged_fact_count")
    if supplemental_status == "disabled_no_checkpoint":
        # Pre-DA 兜底：没有读任何 raw 文件，必须明确是 0 而不是 None/N。
        merged_fact_count = 0
    return {
        "schema": SCHEMA,
        "generated_at": _iso(generated_at),
        "date_et": date_et,
        "as_of": brief.get("as_of"),
        "input_hash": input_hash,
        "input_components": components,
        "target_universe": sorted(str(item) for item in target_symbols),
        "target_source": target_source,
        "target_override_ignored": override_ignored,
        "excluded_checkpoints": list(excluded),
        "checkpoint_dir": _display_path(checkpoint_dir),
        "event_db": _display_path(db_path),
        "snapshot": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "event_hash": snapshot.get("event_hash"),
            "event_count": snapshot.get("event_count"),
            "cluster_count": snapshot.get("cluster_count"),
            "since": snapshot.get("since"),
            "as_of": snapshot.get("as_of"),
            "degraded": snapshot.get("degraded"),
            "unknowns": snapshot.get("unknowns"),
        },
        "supplemental": {
            "status": supplemental_status,
            "evidence_hash": supplemental.get("evidence_hash"),
            "cutoff": supplemental.get("cutoff"),
            "cutoff_et": supplemental.get("cutoff_et"),
            "case_symbols": supplemental.get("case_symbols"),
            "stats": supplemental.get("stats"),
            "checkpoints": supplemental.get("checkpoints"),
            "merged_fact_count": merged_fact_count,
            "omitted_fact_count": supplemental_context.get("omitted_fact_count"),
            "date_only_item_count": supplemental_context.get("date_only_item_count"),
            "unknown_time_item_count": supplemental_context.get("unknown_time_item_count"),
        },
        "selection": context.get("selection"),
        "brief": brief,
        "models": {"main": args.model, "challenger": args.challenger_model},
        "limits": {
            "lookback_hours": args.lookback_hours,
            "max_events": args.max_events,
            "supplemental_limit": supplemental_limit,
            "max_llm_calls": MAX_LLM_CALLS,
            "max_tokens": args.max_tokens,
            "timeout": args.timeout,
        },
        "llm_calls": brief.get("llm_calls"),
        "record_only": True,
        "production_writes": False,
        "caveat": CAVEAT,
    }


def _latest_document(
    artifact: Dict[str, Any],
    *,
    artifact_path: Path,
    status: str,
    checked_at: datetime,
    check_count: int,
) -> Dict[str, Any]:
    brief = artifact.get("brief") or {}
    snapshot = artifact.get("snapshot") or {}
    supplemental = artifact.get("supplemental") or {}
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "date_et": artifact.get("date_et"),
        "as_of": brief.get("as_of"),
        "generated_at": artifact.get("generated_at"),
        "last_checked_at": _iso(checked_at),
        "check_count": check_count,
        "input_hash": artifact.get("input_hash"),
        "artifact_path": _display_path(artifact_path),
        "brief_id": brief.get("brief_id"),
        "event_count": snapshot.get("event_count"),
        "fact_count": len(brief.get("facts") or []),
        "transmission_count": len(brief.get("transmissions") or []),
        "validation_errors": list(brief.get("validation_errors") or []),
        "llm_calls": brief.get("llm_calls"),
        "main_model": (artifact.get("models") or {}).get("main"),
        "challenger_model": (artifact.get("models") or {}).get("challenger"),
        "target_universe": artifact.get("target_universe") or [],
        "target_source": artifact.get("target_source"),
        "target_override_ignored": artifact.get("target_override_ignored"),
        "excluded_checkpoints": artifact.get("excluded_checkpoints") or [],
        "supplemental_evidence_hash": supplemental.get("evidence_hash"),
        "supplemental_status": supplemental.get("status"),
        "supplemental_merged_fact_count": supplemental.get("merged_fact_count"),
        "degraded": brief.get("degraded"),
        "brief": brief,
        "writer": "scripts/da_event_context_shadow.py",
        "record_only": True,
        "production_writes": False,
        "caveat": CAVEAT,
    }


def _summary(doc: Dict[str, Any], *, status: str) -> Dict[str, Any]:
    return {
        "status": status,
        "date_et": doc.get("date_et"),
        "as_of": doc.get("as_of"),
        "generated_at": doc.get("generated_at"),
        "input_hash": doc.get("input_hash"),
        "artifact_path": doc.get("artifact_path"),
        "brief_id": doc.get("brief_id"),
        "event_count": doc.get("event_count"),
        "fact_count": doc.get("fact_count"),
        "transmission_count": doc.get("transmission_count"),
        "validation_errors": doc.get("validation_errors") or [],
        "llm_calls": doc.get("llm_calls"),
        "degraded": doc.get("degraded"),
        "check_count": doc.get("check_count"),
        "target_universe": doc.get("target_universe") or [],
        "target_source": doc.get("target_source"),
        "target_override_ignored": doc.get("target_override_ignored"),
        "supplemental_status": doc.get("supplemental_status"),
        "supplemental_merged_fact_count": doc.get("supplemental_merged_fact_count"),
        "excluded_checkpoints": doc.get("excluded_checkpoints") or [],
        "record_only": True,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> Dict[str, Any]:
    now = _now_utc()
    as_of = _parse_ts(args.as_of, field="--as-of") if args.as_of else now
    if as_of > now:
        raise ValueError(f"as_of is in the future: {_iso(as_of)} > {_iso(now)}")
    as_of_et_date = as_of.astimezone(ET).date().isoformat()
    date_et = _parse_date(args.date) if args.date else as_of_et_date
    if args.date and date_et != as_of_et_date:
        raise ValueError(
            f"--date {date_et} does not match --as-of ET date {as_of_et_date}"
        )
    today_et = now.astimezone(ET).date().isoformat()
    if date_et > today_et:
        raise ValueError(f"date is in the future: {date_et} > {today_et}")

    db_path = Path(args.db)
    checkpoint_dir = Path(args.checkpoint_root) / date_et
    override_symbols = parse_target_symbols(args.target_symbols)
    if override_symbols and not args.allow_no_checkpoint:
        raise ValueError("--target-symbols requires --allow-no-checkpoint")
    target_symbols, excluded = discover_checkpoints(checkpoint_dir, date_et)
    target_source = "checkpoint"
    supplemental_status = "enabled"
    fallback_active = False
    override_ignored = False
    if not target_symbols:
        if not args.allow_no_checkpoint:
            return {
                "status": "NO_DATA", "reason": "no_target_checkpoints",
                "target_source": target_source,
                "supplemental_status": supplemental_status,
                "date_et": date_et, "as_of": _iso(as_of),
                "checkpoint_dir": _display_path(checkpoint_dir),
                "target_universe": [], "excluded_checkpoints": excluded,
                "record_only": True,
            }
        # Pre-DA 兜底：当日无 raw 档（或全被错日排除）。绝不读取前一日 raw 顶替。
        fallback_active = True
        supplemental_status = "disabled_no_checkpoint"
        if override_symbols:
            target_symbols = override_symbols
            target_source = "cli_target_symbols"
        else:
            target_symbols = watchlist_target_symbols()
            target_source = "watchlist"
        if not target_symbols:
            return {
                "status": "NO_DATA", "reason": "no_target_symbols_for_fallback",
                "target_source": target_source,
                "supplemental_status": supplemental_status,
                "date_et": date_et, "as_of": _iso(as_of),
                "checkpoint_dir": _display_path(checkpoint_dir),
                "target_universe": [], "excluded_checkpoints": excluded,
                "record_only": True,
            }
    elif override_symbols:
        # 当日 raw 已在：按原路径，--target-symbols 不生效但必须可见。
        override_ignored = True
    base = {
        "date_et": date_et,
        "as_of": _iso(as_of),
        "checkpoint_dir": _display_path(checkpoint_dir),
        "target_universe": target_symbols,
        "target_source": target_source,
        "supplemental_status": supplemental_status,
        "target_override_ignored": override_ignored,
        "excluded_checkpoints": excluded,
        "record_only": True,
    }
    if not db_path.is_file():
        return {"status": "NO_DATA", "reason": "event_store_missing", **base}

    # 影子预设上限: 证据窗固定 <=24h, supplemental 最多 48 条 (0 视作取上限)。
    effective_supplemental_limit = (
        MAX_SUPPLEMENTAL_ITEMS if args.supplemental_limit <= 0
        else min(args.supplemental_limit, MAX_SUPPLEMENTAL_ITEMS)
    )
    snapshot = build_snapshot(
        db_path, as_of=as_of, lookback_hours=args.lookback_hours,
    )
    context, supplemental = build_context(
        snapshot,
        checkpoint_dir=checkpoint_dir,
        target_symbols=target_symbols,
        max_events=args.max_events,
        supplemental_limit=effective_supplemental_limit,
        supplemental_enabled=not fallback_active,
    )
    if fallback_active:
        supplemental_status = "disabled_no_checkpoint"
        context["unknowns"] = sorted({
            *(context.get("unknowns") or []),
            "supplemental_evidence_disabled_no_checkpoint",
        })
    elif supplemental is None:
        supplemental_status = "no_registered_symbols"
    else:
        supplemental_status = "merged"
    base["supplemental_status"] = supplemental_status
    components = _input_components(
        date_et=date_et,
        snapshot=snapshot,
        supplemental=supplemental,
        target_symbols=target_symbols,
        supplemental_limit=effective_supplemental_limit,
        args=args,
        target_source=target_source,
        supplemental_status=supplemental_status,
    )
    input_hash = sha256_json(components)

    out_dir = Path(args.out_dir)
    artifact_path = out_dir / date_et / f"{input_hash}.json"
    latest_path = out_dir / "latest.json"

    # 1) 幂等命中：证据没变 + 已有 brief 有效 ⇒ 零付费。
    latest = read_json(latest_path)
    if (
        isinstance(latest, dict)
        and latest.get("schema") == REPORT_SCHEMA
        and latest.get("date_et") == date_et
        and latest.get("input_hash") == input_hash
        and not validate_brief_record(latest.get("brief"), date_et=date_et)
    ):
        updated = dict(latest)
        updated["status"] = "ALREADY_CURRENT"
        updated["last_checked_at"] = _iso(now)
        updated["check_count"] = int(latest.get("check_count") or 1) + 1
        atomic_write_json(latest_path, updated)
        return _summary(updated, status="ALREADY_CURRENT")

    # 2) latest 丢失/损坏但不可变 run 还在 ⇒ 复用证据，仍不付费；无效则失败关闭。
    if artifact_path.exists():
        artifact = read_json(artifact_path)
        if not isinstance(artifact, dict) or artifact.get("input_hash") != input_hash:
            raise ValueError(
                f"run artifact exists with different payload (no overwrite): {artifact_path}"
            )
        errors = validate_brief_record(artifact.get("brief"), date_et=date_et)
        if errors:
            raise ValueError(
                "existing run artifact is invalid; refusing to overwrite or re-pay: "
                f"{errors[:8]}"
            )
        doc = _latest_document(
            artifact, artifact_path=artifact_path,
            status="ALREADY_CURRENT", checked_at=now, check_count=1,
        )
        atomic_write_json(latest_path, doc)
        return _summary(doc, status="ALREADY_CURRENT")

    # 3) 零付费 dry-run：只预览，不落任何文件。
    if not args.execute:
        from app.event_stream.market_brief import generate_market_event_brief

        brief, _artifacts = generate_market_event_brief(
            context,
            llm_call=None,
            main_model=args.model,
            challenger_model=args.challenger_model,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
        return {
            "status": "DRY_RUN",
            "reason": "llm_not_executed",
            "would_write": _display_path(artifact_path),
            "latest_path": _display_path(latest_path),
            "input_hash": input_hash,
            "brief_id": brief.get("brief_id"),
            "event_count": snapshot.get("event_count"),
            "fact_count": len(context.get("facts") or []),
            "transmission_count": len(brief.get("transmissions") or []),
            "validation_errors": brief.get("validation_errors") or [],
            "llm_calls": 0,
            **base,
        }

    # 4) 付费生成：主分析 + 挑战者，最多 2 次调用。
    from app.event_stream.market_brief import generate_market_event_brief

    brief, _artifacts = generate_market_event_brief(
        context,
        llm_call=_resolve_llm_call(),
        main_model=args.model,
        challenger_model=args.challenger_model,
        max_tokens=args.max_tokens,
        timeout=args.timeout,
    )
    if int(brief.get("llm_calls") or 0) > MAX_LLM_CALLS:
        raise ValueError(f"llm call budget exceeded: {brief.get('llm_calls')}")
    artifact = _artifact_document(
        generated_at=now,
        date_et=date_et,
        input_hash=input_hash,
        components=components,
        snapshot=snapshot,
        context=context,
        supplemental=supplemental,
        brief=brief,
        target_symbols=target_symbols,
        excluded=excluded,
        checkpoint_dir=checkpoint_dir,
        db_path=db_path,
        supplemental_limit=effective_supplemental_limit,
        target_source=target_source,
        supplemental_status=supplemental_status,
        args=args,
        override_ignored=override_ignored,
    )
    try:
        write_json_new(artifact_path, artifact)
    except FileExistsError:
        # 并发写者先到：既成证据永不覆盖；有效则复用，否则失败关闭。
        existing = read_json(artifact_path)
        if (
            isinstance(existing, dict)
            and existing.get("input_hash") == input_hash
            and not validate_brief_record(existing.get("brief"), date_et=date_et)
        ):
            doc = _latest_document(
                existing, artifact_path=artifact_path,
                status="ALREADY_CURRENT", checked_at=now, check_count=1,
            )
            atomic_write_json(latest_path, doc)
            return _summary(doc, status="ALREADY_CURRENT")
        raise ValueError(f"run artifact appeared concurrently and is unusable: {artifact_path}")

    errors = validate_brief_record(brief, date_et=date_et)
    if errors:
        return {
            "status": "ERROR",
            "reason": "brief_validation_failed",
            "input_hash": input_hash,
            "artifact_path": _display_path(artifact_path),
            "validation_errors": errors,
            "llm_calls": brief.get("llm_calls"),
            **base,
        }

    doc = _latest_document(
        artifact, artifact_path=artifact_path,
        status="OK", checked_at=now, check_count=1,
    )
    atomic_write_json(latest_path, doc)
    return _summary(doc, status="OK")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RFC-207 EventContext record-only 每日共享简报影子 (默认零付费 dry-run)",
    )
    parser.add_argument("--date", default="",
                        help="ET 交易日 YYYY-MM-DD (缺省=--as-of 的 ET 日；必须与之同日)")
    parser.add_argument("--as-of", default="",
                        help="as-of 时间 (UTC ISO，必须带时区；缺省=now；禁止未来)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="event_stream.db (只读 mode=ro)")
    parser.add_argument("--checkpoint-root", type=Path, default=DEFAULT_CHECKPOINT_ROOT,
                        help="DA raw checkpoint 根目录")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_ROOT,
                        help="影子产物目录 (latest.json + <date>/<input_hash>.json)")
    parser.add_argument("--execute", action="store_true",
                        help="真实付费 LLM 调用；缺省为 dry-run (零 LLM/零落盘)")
    parser.add_argument("--allow-no-checkpoint", action="store_true",
                        help="Pre-DA 兜底: 当日无 raw checkpoint 时仍用 EventStore 生成 "
                             "(supplemental 关闭, 绝不用前一日 raw)")
    parser.add_argument("--target-symbols", default="",
                        help="逗号分隔 target universe 覆盖 (仅在 --allow-no-checkpoint "
                             "兜底时生效, 接受 US. 前缀)")
    parser.add_argument("--lookback-hours", type=int, default=LOOKBACK_HOURS)
    parser.add_argument("--max-events", type=int, default=MAX_BRIEF_EVENTS)
    parser.add_argument("--supplemental-limit", type=int, default=MAX_SUPPLEMENTAL_ITEMS)
    parser.add_argument("--model", default="qwen3.7-max")
    parser.add_argument("--challenger-model", default="deepseek-v4.1-flash")
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--debug", action="store_true", help="错误时把 traceback 打到 stderr")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not _enabled():
        print("DISABLED")
        return 0
    if not 1 <= args.lookback_hours <= LOOKBACK_HOURS:
        print(json.dumps(
            {"status": "ERROR", "error_type": "ValueError",
             "error": f"--lookback-hours must be within 1..{LOOKBACK_HOURS} (evidence window is last 24h)"},
            ensure_ascii=False, sort_keys=True,
        ))
        return 2
    try:
        report = _run(args)
    except Exception as exc:  # noqa: BLE001 - 影子必须失败可见而不是打断调度
        if args.debug:
            traceback.print_exc(file=sys.stderr)
        report = {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "record_only": True,
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if report.get("status") in {"OK", "ALREADY_CURRENT", "DRY_RUN", "NO_DATA"} else 2


if __name__ == "__main__":
    raise SystemExit(main())