#!/usr/bin/env python3
"""RFC-200 开盘禁入窗 (0945) 黑窗影子 — 只读评估器 / 停摆可见性器。

════════ 为什么存在 (2026-11-12, 用户裁「A: 只加观测」) ════════

`playbook.execution_parameters.entry_open_blackout_hhmm` 从 0935 → 0945 后,
09:30-09:45 的逐条 trigger **一次都不跑** ⇒ 该窗口零遥测 (RFC-200 §3.1)。
写方 `auto_execute_flow.emit_blackout_shadow()` 在闸内补了 record-only 打点:
每轮一行 `kind=cycle` (证明观测活着) + 每条指令首次落入可触发带一行 `kind=hit`。
本器是该 jsonl 的**唯一读入口**, 回答三个问题:

  1. 观测还活着吗? (cycle 覆盖 / 首末时间 / n_inst / n_priced)
  2. 取价面坏了吗? (全失败 = QUOTE_ALL_FAIL; 部分轮次零价 = INCOMPLETE)
  3. 若这段时间可交易, 带几何上最多会碰到哪些指令? (hit 去重 + 决策源分桶)

⛔ 口径硬约束 (写方行内 `caveat` 同款):
  · `band_would_trigger` **只是带几何上限** — 后续门 (VWAP / 低 ATR / 流动性 /
    借券 / thesis 漂移 / 极端 gap / admission) 本轮**未观测** ⇒ 不等于会成交。
  · 本器不计算任何收益 / PnL / 出场路径; 没有成交假设就没有反事实净额。
  · `cycle` 的 n_priced / n_would_trigger 按**轮**累加, 同一指令跨轮会重复计;
    指令级读数以 `hits.n_dedup` / `n_new_hits` 为准 (本器按 (date, inst_id) 取
    **最早 ts** 去重, 抵消写方进程内去重表在重启后可能重复落行)。

════════ 机器可读状态 (可接 registry / 哨兵) ════════

  OK              至少 1 个 cycle, 取价面有成功轮次。**无 hit 也是 OK**。
  INCOMPLETE      部分轮次 n_inst>0 而 n_priced==0 (取价部分失败) — 读数不完整。
  QUOTE_ALL_FAIL  所有含指令的轮次 n_priced==0 ⇒ 取价面全失败 (结构性)。
  NO_CYCLES       指定 ET 日有行, 但没有 kind=cycle (只有 hit/坏行)。
  NO_DATA         文件缺失 / 该 ET 日无任何行。
  READ_ERROR      文件存在但不可读。

退出码: OK=0 / INCOMPLETE=2 / 其余=1。因此 "没有文件" 与 "没有 cycle" 不会
静默变成成功; "没有 hit" 不会报错。

用法:
  venv/bin/python scripts/entry_blackout_shadow_eval.py
  venv/bin/python scripts/entry_blackout_shadow_eval.py --date 2026-11-12 --json
  venv/bin/python scripts/entry_blackout_shadow_eval.py --path /tmp/x.jsonl --no-write

产物 (默认): data/shadow/entry_blackout_shadow_eval_latest.json
回滚: 删本文件 + 删注册表条目; 纯只读, 无生产调用方。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "data" / "entry_blackout_shadow.jsonl"
DEFAULT_OUT = ROOT / "data" / "shadow" / "entry_blackout_shadow_eval_latest.json"
ET = ZoneInfo("America/New_York")
SCHEMA = "entry_blackout_shadow_eval_v1"

# 本器刻意只用 stdlib (不 import app.*): `app/__init__.py` 在 import 时会向 stdout
# 打印时区 fallback 提示, 会污染 `--json` 的机器可读输出。前缀与真源同值, 由
# tests/test_entry_blackout_shadow_eval.py 的漂移钉子守住:
#   · DA_DECISION_PREFIX      = app/playbook/pipeline_parts/supply_gate.py
#   · MOM_ETF_V1_PREFIX       = app/playbook/pilot_policy.py
DA_DECISION_PREFIX = "DA-"
MOM_ETF_V1_PREFIX = "MOM-ETF-V1-"

STATUS_OK = "OK"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_QUOTE_ALL_FAIL = "QUOTE_ALL_FAIL"
STATUS_NO_CYCLES = "NO_CYCLES"
STATUS_NO_DATA = "NO_DATA"
STATUS_READ_ERROR = "READ_ERROR"

_EXIT_CODES = {
    STATUS_OK: 0,
    STATUS_INCOMPLETE: 2,
    STATUS_QUOTE_ALL_FAIL: 1,
    STATUS_NO_CYCLES: 1,
    STATUS_NO_DATA: 1,
    STATUS_READ_ERROR: 1,
}

CAVEATS = (
    "band_would_trigger 只是带几何上限: VWAP/低ATR/流动性/借券/thesis漂移/极端gap/"
    "admission 等后续门本轮未观测 ⇒ 不等于会成交。",
    "本器不计算任何收益/PnL/出场路径; 无成交假设就没有反事实净额。",
    "cycle 的 n_priced/n_would_trigger 按轮累加, 同一指令跨轮会重复计; "
    "指令级读数以 hits.n_dedup / n_new_hits 为准。",
)


def _as_int(value: Any) -> int | None:
    """宽松取整数; bool/非数值返回 None (不把 True 当 1)。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _norm_date(value: Any) -> str | None:
    """只认 `YYYY-MM-DD` (前缀 10 字符), 其它返回 None。"""
    text = str(value or "").strip()[:10]
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    return None


def _min_to_hhmm(minute: int | None) -> str | None:
    if minute is None or minute < 0:
        return None
    return f"{minute // 60:02d}:{minute % 60:02d}"


def load_rows(path: Path) -> tuple[list[dict], dict]:
    """只读 jsonl。坏行显式计数并留样例, 不静默吞掉。"""
    meta: dict[str, Any] = {
        "file_exists": path.exists(),
        "lines_total": 0,
        "bad_lines": 0,
        "non_dict_lines": 0,
        "bad_line_samples": [],
    }
    if not path.exists():
        return [], meta
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        meta["read_error"] = f"{type(exc).__name__}: {exc}"
        return [], meta

    rows: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        meta["lines_total"] += 1
        try:
            rec = json.loads(line)
        except ValueError as exc:          # JSONDecodeError 是 ValueError 子类
            meta["bad_lines"] += 1
            if len(meta["bad_line_samples"]) < 20:
                meta["bad_line_samples"].append(
                    {"line": lineno, "error": f"{type(exc).__name__}: {exc}"[:160]})
            continue
        if not isinstance(rec, dict):
            meta["non_dict_lines"] += 1
            if len(meta["bad_line_samples"]) < 20:
                meta["bad_line_samples"].append(
                    {"line": lineno, "error": f"非对象行: {type(rec).__name__}"})
            continue
        rows.append(rec)
    return rows, meta


def _pick_date(rows: list[dict], requested: str | None) -> str | None:
    if requested:
        return _norm_date(requested) or str(requested).strip()
    dates = sorted({d for d in (_norm_date(r.get("date")) for r in rows) if d})
    return dates[-1] if dates else None


def _hit_time_key(row: dict) -> tuple:
    """最早 ts 排序键; 缺 ts 的行排最后 (不让空串冒充最早)。"""
    ts = str(row.get("ts") or "")
    return (ts == "", ts, _as_int(row.get("et_min")) or 0)


def _cycle_time_key(row: dict) -> tuple:
    minute = _as_int(row.get("et_min"))
    return (minute is None, minute if minute is not None else 24 * 60,
            str(row.get("ts") or ""))


def _decision_source(decision_id: Any) -> str:
    did = str(decision_id or "")
    if did.startswith(DA_DECISION_PREFIX):
        return "DA"
    if did.startswith(MOM_ETF_V1_PREFIX):
        return "MOM"
    return "OTHER"


def _dedupe_hits(hits: list[dict], date_str: str) -> tuple[list[dict], int]:
    """按 (date, inst_id) 取最早 ts 的 hit; 返回 (去重后, 缺 inst_id 行数)。"""
    chosen: dict[tuple, dict] = {}
    n_missing_inst_id = 0
    for idx, row in enumerate(hits):
        inst_id = row.get("inst_id")
        if inst_id in (None, ""):
            n_missing_inst_id += 1
            key: tuple = ("__anonymous__", idx)
        else:
            key = (date_str, inst_id)
        prev = chosen.get(key)
        if prev is None or _hit_time_key(row) < _hit_time_key(prev):
            chosen[key] = row
    return list(chosen.values()), n_missing_inst_id


def _cycle_view(row: dict) -> dict:
    minute = _as_int(row.get("et_min"))
    return {
        "ts": row.get("ts"),
        "date": row.get("date"),
        "et_min": minute,
        "et": _min_to_hhmm(minute),
        "n_inst": _as_int(row.get("n_inst")),
        "n_priced": _as_int(row.get("n_priced")),
        "n_would_trigger": _as_int(row.get("n_would_trigger")),
        "n_new_hits": _as_int(row.get("n_new_hits")),
        "n_eligible": _as_int(row.get("n_eligible")),
        "n_quote_error": _as_int(row.get("n_quote_error")),
        "n_price_invalid": _as_int(row.get("n_price_invalid")),
        "n_eval_error": _as_int(row.get("n_eval_error")),
        "writer_status": row.get("status"),
        "blackout_hhmm": _as_int(row.get("blackout_hhmm")),
    }


def _quote_denominator(cycle: dict) -> int:
    """R1 用 eligible 候选数；旧行只有原始 n_inst 时保持旧口径。"""
    value = cycle["n_eligible"] if cycle["n_eligible"] is not None else cycle["n_inst"]
    return int(value or 0)


def evaluate(rows: list[dict], requested_date: str | None = None,
             meta: dict | None = None) -> dict:
    """评估一个 ET 交易日。纯函数; 不读文件、不写文件。"""
    meta = dict(meta or {})
    date_str = _pick_date(rows, requested_date)

    all_dates = Counter(d for d in (_norm_date(r.get("date")) for r in rows) if d)
    rows_missing_date = sum(1 for r in rows if not _norm_date(r.get("date")))
    day_rows = ([r for r in rows if _norm_date(r.get("date")) == date_str]
                if date_str else [])
    cycles_raw = [r for r in day_rows if str(r.get("kind") or "") == "cycle"]
    hits_raw = [r for r in day_rows if str(r.get("kind") or "") == "hit"]
    known = {"cycle", "hit"}
    unknown_kind = sum(1 for r in day_rows if str(r.get("kind") or "") not in known)

    cycles = sorted((_cycle_view(r) for r in cycles_raw), key=_cycle_time_key)

    n_inst_values = [c["n_inst"] for c in cycles if c["n_inst"] is not None]
    n_priced_values = [c["n_priced"] for c in cycles if c["n_priced"] is not None]
    n_would_values = [c["n_would_trigger"] for c in cycles
                      if c["n_would_trigger"] is not None]
    n_new_values = [c["n_new_hits"] for c in cycles if c["n_new_hits"] is not None]

    # R1 写方用 n_inst 记原始行数、n_eligible 记实际需取价候选；
    # NO_CANDIDATES 即使 n_inst>0 也不是报价失败。旧行没有这些字段时沿用原推断。
    cycles_with_inst = [c for c in cycles if (c["n_inst"] or 0) > 0
                        and c["writer_status"] != "NO_CANDIDATES"]
    missing_count_cycles = [c for c in cycles
                            if c["n_inst"] is None or c["n_priced"] is None]
    zero_priced_with_inst = [c for c in cycles_with_inst if c["n_priced"] == 0]
    priced_cycles = [c for c in cycles_with_inst if (c["n_priced"] or 0) > 0]
    hard_writer_statuses = {"NO_ROUTER", "DB_ERROR"}
    partial_writer_statuses = {"PARTIAL_QUOTE", "EVAL_PARTIAL"}
    hard_writer_cycles = [c for c in cycles if c["writer_status"] in hard_writer_statuses]
    partial_writer_cycles = [c for c in cycles if c["writer_status"] in partial_writer_statuses]
    quote_all_failed = ((bool(cycles_with_inst) and not priced_cycles
                         and not missing_count_cycles)
                        or (bool(cycles) and len(hard_writer_cycles) == len(cycles)))
    quote_partial_failed = ((bool(zero_priced_with_inst) and bool(priced_cycles))
                            or bool(missing_count_cycles) or bool(partial_writer_cycles)
                            or (bool(hard_writer_cycles) and not quote_all_failed))

    et_mins = [c["et_min"] for c in cycles if c["et_min"] is not None]
    gaps = [b - a for a, b in zip(et_mins, et_mins[1:])]
    hhmm_seen = sorted({c["blackout_hhmm"] for c in cycles
                        if c["blackout_hhmm"] is not None})

    # ── hits: (date, inst_id) earliest-ts 去重 ──
    deduped, n_missing_inst_id = _dedupe_hits(hits_raw, date_str or "")
    deduped = sorted(deduped, key=_hit_time_key)
    by_path = Counter(str(r.get("entry_path") or "UNKNOWN") for r in deduped)
    by_direction = Counter(str(r.get("direction") or "UNKNOWN") for r in deduped)
    by_source = {"DA": 0, "MOM": 0, "OTHER": 0}
    n_empty_did = 0
    for row in deduped:
        by_source[_decision_source(row.get("decision_id"))] += 1
        if not str(row.get("decision_id") or ""):
            n_empty_did += 1

    hit_ts = [str(r.get("ts")) for r in deduped if r.get("ts")]
    not_vol_assumed = sum(1 for r in deduped if r.get("vol_assumed_ok") is not True)
    band_flagged = sum(1 for r in deduped if r.get("band_would_trigger") is True)

    if not day_rows:
        status = STATUS_READ_ERROR if meta.get("read_error") else STATUS_NO_DATA
        detail = (f"文件不可读: {meta.get('read_error')}" if meta.get("read_error")
                  else (f"{date_str} 无任何行 (文件缺失/该 ET 日未写；R1 起写方每轮 "
                        f"均应落 cycle，故这是停摆而非‘零报价正常形态’)"
                        if date_str else
                        "文件缺失或无任何有效行 (R1 起每轮均应落 cycle，按停摆处理)"))
    elif not cycles:
        status = STATUS_NO_CYCLES
        detail = (f"{date_str} 有 {len(day_rows)} 行但没有 kind=cycle ⇒ "
                  f"无法证明观测活着 (R1 起每轮均应落 cycle)")
    elif quote_all_failed:
        status = STATUS_QUOTE_ALL_FAIL
        detail = (f"{len(cycles_with_inst)} 个含指令轮次全部 n_priced=0 ⇒ "
                  f"取价面结构性失败")
    elif quote_partial_failed:
        status = STATUS_INCOMPLETE
        detail = (f"{len(zero_priced_with_inst)}/{len(cycles_with_inst)} 个含指令轮次 "
                  f"n_priced=0 (部分取价失败) ⇒ 读数不完整")
    else:
        status = STATUS_OK
        detail = (f"{len(cycles)} 轮 cycle, {len(priced_cycles)} 轮取到价, "
                  f"n_priced 合计 {sum(n_priced_values)}")

    result = {
        "schema": SCHEMA,
        "date": date_str,
        "status": status,
        "status_detail": detail,
        "exit_code": _EXIT_CODES[status],
        "cycle": {
            "n_cycles": len(cycles),
            "first_ts": cycles[0]["ts"] if cycles else None,
            "last_ts": cycles[-1]["ts"] if cycles else None,
            "first_et_min": et_mins[0] if et_mins else None,
            "last_et_min": et_mins[-1] if et_mins else None,
            "first_et": _min_to_hhmm(et_mins[0]) if et_mins else None,
            "last_et": _min_to_hhmm(et_mins[-1]) if et_mins else None,
            "et_min_span": (et_mins[-1] - et_mins[0]) if et_mins else None,
            "gap_min": gaps,
            "n_inst_sum": sum(n_inst_values),
            "n_inst_max": max(n_inst_values) if n_inst_values else None,
            "n_priced_sum": sum(n_priced_values),
            "n_priced_max": max(n_priced_values) if n_priced_values else None,
            "n_would_trigger_sum": sum(n_would_values),
            "n_new_hits_sum": sum(n_new_values),
            "blackout_hhmm_seen": hhmm_seen,
            "blackout_hhmm_mixed": len(hhmm_seen) > 1,
            "cycles": cycles,
        },
        "quote": {
            "n_cycles_with_inst": len(cycles_with_inst),
            "n_cycles_priced": len(priced_cycles),
            "n_cycles_zero_priced_with_inst": len(zero_priced_with_inst),
            "n_cycles_missing_counts": len(missing_count_cycles),
            "n_quote_error_sum": sum(c["n_quote_error"] or 0 for c in cycles),
            "n_price_invalid_sum": sum(c["n_price_invalid"] or 0 for c in cycles),
            "n_eval_error_sum": sum(c["n_eval_error"] or 0 for c in cycles),
            "writer_status_counts": dict(sorted(Counter(
                str(c["writer_status"] or "LEGACY_UNKNOWN") for c in cycles).items())),
            "all_failed": quote_all_failed,
            "partial_failure": quote_partial_failed,
            "anomaly": quote_all_failed or quote_partial_failed,
            # R1 前写方在整轮 n_priced=0 时不落 cycle，旧样本的 NO_DATA
            # 无法区分取价失败与未运行；R1 起每轮必落 cycle，缺行直接按停摆。
            "no_rows_for_date": not day_rows,
            "unobservable_no_rows": status in (STATUS_NO_DATA, STATUS_NO_CYCLES),
            "priced_ratio_by_cycle": [
                (round(c["n_priced"] / _quote_denominator(c), 4)
                 if _quote_denominator(c) > 0 and c["n_priced"] is not None else None)
                for c in cycles
            ],
        },
        "hits": {
            "n_rows_raw": len(hits_raw),
            "n_dedup": len(deduped),
            "duplicate_hit": len(hits_raw) - len(deduped),
            "n_missing_inst_id": n_missing_inst_id,
            "first_hit_ts": hit_ts[0] if hit_ts else None,
            "last_hit_ts": hit_ts[-1] if hit_ts else None,
            "n_by_entry_path": dict(sorted(by_path.items())),
            "n_by_direction": dict(sorted(by_direction.items())),
            "n_by_decision_source": by_source,
            "n_empty_decision_id": n_empty_did,
            "n_band_would_trigger": band_flagged,
            "n_vol_assumed_not_ok": not_vol_assumed,
            "symbols": sorted(str(r.get("symbol") or "") for r in deduped
                              if r.get("symbol")),
        },
        "data_quality": {
            **meta,
            "rows_missing_date": rows_missing_date,
            "rows_other_dates": sum(v for k, v in all_dates.items() if k != date_str),
            "unknown_kind_rows": unknown_kind,
            "dates_seen": sorted(all_dates),
        },
        "mounting": {
            "data_path_exists": bool(meta.get("file_exists")),
            "data_file_lines": meta.get("lines_total", 0),
            "status_is_ok": status == STATUS_OK,
        },
        "caveats": list(CAVEATS),
    }
    return result


def _render_text(result: dict) -> str:
    cyc = result["cycle"]
    quote = result["quote"]
    hits = result["hits"]
    dq = result["data_quality"]
    lines = [
        f"== 开盘禁入窗影子 (RFC-200 §6) · {result['date'] or '(无日期)'} ==",
        f"状态: {result['status']} — {result['status_detail']}",
        (f"cycle: n={cyc['n_cycles']} 首={cyc['first_ts']} 末={cyc['last_ts']} "
         f"ET跨越={cyc['first_et']}~{cyc['last_et']} ({cyc['et_min_span']}min) "
         f"轮间gap={cyc['gap_min']}"),
        (f"指令/取价: n_inst合计={cyc['n_inst_sum']} n_priced合计={cyc['n_priced_sum']} "
         f"n_would_trigger合计={cyc['n_would_trigger_sum']} "
         f"n_new_hits合计={cyc['n_new_hits_sum']} | "
         f"取价轮次={quote['n_cycles_priced']}/{quote['n_cycles_with_inst']} "
         f"全失败={quote['all_failed']} 部分失败={quote['partial_failure']}"),
        (f"hits: raw={hits['n_rows_raw']} 去重后={hits['n_dedup']} "
         f"duplicate_hit={hits['duplicate_hit']} | "
         f"源 DA={hits['n_by_decision_source']['DA']} "
         f"MOM={hits['n_by_decision_source']['MOM']} "
         f"OTHER={hits['n_by_decision_source']['OTHER']} (空did={hits['n_empty_decision_id']}) | "
         f"路径={hits['n_by_entry_path']}"),
    ]
    if dq.get("bad_lines") or dq.get("non_dict_lines"):
        lines.append(f"⚠️ 数据质量: 坏行={dq.get('bad_lines')} "
                     f"非对象行={dq.get('non_dict_lines')} (见 data_quality.bad_line_samples)")
    lines.append("口径: " + " / ".join(CAVEATS))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RFC-200 开盘禁入窗影子只读评估器 (cycle 覆盖 / 取价异常 / hit 去重)")
    parser.add_argument("--path", default=str(DEFAULT_PATH), help="影子 jsonl 路径")
    parser.add_argument("--date", default=None, help="指定 ET 交易日 YYYY-MM-DD "
                                                     "(缺省=文件内最新日期)")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="latest.json 产物路径")
    parser.add_argument("--no-write", action="store_true", help="只打印, 不落 latest.json")
    args = parser.parse_args(argv)

    path = Path(args.path)
    rows, meta = load_rows(path)
    result = evaluate(rows, requested_date=args.date, meta=meta)
    result["path"] = str(path)
    result["generated_at"] = datetime.now(ET).isoformat(timespec="seconds")

    if not args.no_write and not meta.get("read_error"):
        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except OSError as exc:
            print(f"[warn] 评估产物落盘失败 (读数仍打印): {type(exc).__name__}: {exc}",
                  file=sys.stderr)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_render_text(result))
        if not args.no_write and not meta.get("read_error"):
            print(f"  → {args.out}")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())