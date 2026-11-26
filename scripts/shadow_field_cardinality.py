#!/usr/bin/env python3
"""离散字段基数自检 (W0, PROPOSAL-20260905 P1, 2026-09-05 用户裁"如无问题可以实施").

病灶 (A5/A8 + 07-25 conf 塌缩): 字段"在生产"≠"有信息" — trigger 计数 237/237 恒=2、
UE conf 恒=50、DA score 2/3 塌缩于 7.0, 全是事后人工复盘才发现的常量退化。
本脚本把"唯一值数/最大单值占比/归一化熵"变成可计数的例行自检, DEGEN 判定机械执行。

覆盖 (只读, 全部 mode=ro / 只读文件):
  DA gate 日志 (da_gate_decisions.jsonl): da_score / score_band / challenger_score /
    decision / llm_self_conf / invalidation_trigger_count / tripwire_count
  DA 指令 (playbook_instructions, source=deep_analysis): score / score_band /
    confidence_semantic (raw_instruction_json)
  UE 指令 (source=universe_expansion): confidence / origin.raw_confidence /
    origin.self_tier / origin.rubric_score
  UE 扫描 (shadow/llm_universe_scan.jsonl type=scan): surfaced.confidence /
    self_tier / rubric_score / evidence_citation_state

DEGEN 判据 (默认, 乙2 §四): 唯一值<5 或 最大单值占比>60% 或 归一化熵<0.60。
  设计低基数字段 (3档/枚举) 用 FIELD_RULES 放宽 — 查"塌缩"不查"档位本身"。
  n<30 标 INSUFFICIENT (样本不足, DEGEN 判定仅供参考, 不触发退出码)。

退出码: 任一 n≥30 字段 DEGEN → 1 (供 cron 告警); 否则 0。
用法: venv/bin/python scripts/shadow_field_cardinality.py [--days 30] [--json]
"""
import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "ftseek_us.db"
_GATE_LOG = _ROOT / "data" / "da_gate_decisions.jsonl"
_UE_SCAN_LOG = _ROOT / "data" / "shadow" / "llm_universe_scan.jsonl"

# 乙2 默认判据; 设计低基数字段 (3档/枚举) 查塌缩不查档位。
DEFAULT_RULE = {"min_unique": 5, "max_top_share": 0.60, "min_entropy": 0.60}
FIELD_RULES = {
    "score_band": {"min_unique": 2, "max_top_share": 0.90, "min_entropy": 0.40},
    "self_tier": {"min_unique": 2, "max_top_share": 0.90, "min_entropy": 0.40},
    "decision": {"min_unique": 2, "max_top_share": 0.95, "min_entropy": 0.20},
    "invalidation_trigger_count": {"min_unique": 2, "max_top_share": 0.90,
                                   "min_entropy": 0.40},
    "tripwire_count": {"min_unique": 2, "max_top_share": 0.90, "min_entropy": 0.40},
    "llm_self_conf": {"min_unique": 2, "max_top_share": 0.90, "min_entropy": 0.40},
    "confidence_semantic": {"min_unique": 2, "max_top_share": 0.90,
                            "min_entropy": 0.40},
    # rubric_score 设计基数=5 (0-4 四项清单命中数): 小样本缺极端值 (0/4) 合法,
    #   默认 min_unique=5 是误报。查"塌缩到单一值", 不查"没集齐"。
    "rubric_score": {"min_unique": 3, "max_top_share": 0.60, "min_entropy": 0.50},
    # [P2.4 2026-09-12, DEB-20260910 §17] 引证三态, 设计基数=4 (no_ref / not_found /
    #   entity_linked / transmission_assumed)。放宽理由与 self_tier 同族: 查"塌缩到单一
    #   值"(=传感器死了或模型行为单一化), 不查"没集齐四档"。真实生产分布**预期高度
    #   偏斜** (有引证的行里 entity_linked 应占多数) ⇒ 用默认 min_unique=5 会天天误报。
    #   消费方登记 (AGENT.md P7): evidence_grounding_enforce 开启裁决的人工抽检分层 +
    #   P4 unit_cell_daily_readout 全漏斗 LLM 段分层。
    "evidence_citation_state": {"min_unique": 2, "max_top_share": 0.95,
                                "min_entropy": 0.20},
    "confidence_basis": {"min_unique": 2, "max_top_share": 0.95,
                         "min_entropy": 0.20},
    "event_phase": {"min_unique": 2, "max_top_share": 0.95,
                    "min_entropy": 0.20},
    "move_basis": {"min_unique": 2, "max_top_share": 0.95,
                   "min_entropy": 0.20},
    "borrow_data_basis": {"min_unique": 2, "max_top_share": 0.95,
                          "min_entropy": 0.20},
}


def _norm_entropy(counts: Counter) -> float:
    """H / log2(k), k=观测到的唯一值数。k<=1 → 0 (完全塌缩)。"""
    k = len(counts)
    if k <= 1:
        return 0.0
    n = sum(counts.values())
    h = -sum((c / n) * math.log2(c / n) for c in counts.values() if c)
    return h / math.log2(k)


def check_field(source: str, name: str, values: list) -> dict:
    """对单字段值序列做基数自检。None/空串已在调用侧滤除。"""
    rule = FIELD_RULES.get(name, DEFAULT_RULE)
    n = len(values)
    counts = Counter(str(v) for v in values)
    unique = len(counts)
    top_val, top_n = counts.most_common(1)[0] if counts else ("-", 0)
    top_share = (top_n / n) if n else 0.0
    ent = _norm_entropy(counts)
    if n < 30:
        verdict = "INSUFFICIENT"  # 样本不足, 判定仅供参考
    elif (unique < rule["min_unique"] or top_share > rule["max_top_share"]
          or ent < rule["min_entropy"]):
        verdict = "DEGEN"
    else:
        verdict = "OK"
    return {"source": source, "field": name, "n": n, "unique": unique,
            "top_value": top_val, "top_share": round(top_share, 3),
            "entropy": round(ent, 3), "verdict": verdict,
            "rule": rule}


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _load_instructions(cutoff: str) -> list:
    """playbook_instructions 近窗行 → (source, raw_instruction dict, top_conf)。"""
    if not _DB.exists():
        return []
    conn = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT confidence, raw_instruction_json FROM playbook_instructions "
            "WHERE date >= ?", (cutoff,)).fetchall()
    finally:
        conn.close()
    out = []
    for conf, raw in rows:
        try:
            d = json.loads(raw or "{}")
        except json.JSONDecodeError:
            d = {}
        out.append((d.get("source") or "", d, conf))
    return out


def collect(days: int) -> list:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    results = []

    # ── DA gate 日志 ──
    gate = [r for r in _read_jsonl(_GATE_LOG)
            if str(r.get("trade_date") or r.get("date") or "") >= cutoff]
    for field in ("da_score", "score_band", "challenger_score", "decision",
                  "llm_self_conf", "invalidation_trigger_count", "tripwire_count"):
        vals = [r[field] for r in gate if r.get(field) is not None]
        results.append(check_field("da_gate", field, vals))

    # ── 指令级 (DA / UE) ──
    insts = _load_instructions(cutoff)
    da = [d for src, d, _c in insts if src == "deep_analysis"]
    ue = [(d, c) for src, d, c in insts if src == "universe_expansion"]
    for field in ("score", "score_band", "confidence_semantic"):
        vals = [d[field] for d in da if d.get(field) is not None]
        results.append(check_field("da_instruction", field, vals))
    if ue:
        results.append(check_field(
            "ue_instruction", "confidence",
            [c for _d, c in ue if c is not None]))
        for field in ("raw_confidence", "self_tier", "rubric_score",
                      "confidence_basis", "event_phase", "move_basis"):
            vals = [(_d.get("origin") or {}).get(field) for _d, _c in ue]
            vals = [v for v in vals if v is not None]
            results.append(check_field("ue_instruction", field, vals))
        vals = [((_d.get("origin") or {}).get("short_check") or {}).get(
            "borrow_data_basis") for _d, _c in ue]
        vals = [v for v in vals if v is not None]
        results.append(check_field("ue_instruction", "borrow_data_basis", vals))

    # ── UE 扫描级 (inject 前视图, v2.9 依从监控口径) ──
    scan_rows = [r for r in _read_jsonl(_UE_SCAN_LOG)
                 if r.get("type") == "scan"
                 and str(r.get("date") or "") >= cutoff]
    surfaced = [s for r in scan_rows for s in (r.get("surfaced") or [])
                if isinstance(s, dict)]
    for field in ("confidence", "self_tier", "rubric_score",
                  "evidence_citation_state", "confidence_basis",
                  "event_phase", "move_basis"):
        vals = [s[field] for s in surfaced if s.get(field) is not None]
        results.append(check_field("ue_scan", field, vals))
    vals = [(s.get("short_check") or {}).get("borrow_data_basis")
            for s in surfaced]
    vals = [v for v in vals if v is not None]
    results.append(check_field("ue_scan", "borrow_data_basis", vals))

    return results


def regime_audit_cardinality(rows: list) -> dict:
    fields = {
        "regime": "enum",
        "spy_price_basis": "enum",
        "spy_quote_time": "provider_raw",
        "assessed_at": "utc_iso8601",
        "trend_score": "score_points",
        "volatility_score": "score_points",
        "vix_observation.value": "vix_observation.unit",
        "vix_observation.source": "enum",
        "vix_observation.unit": "enum",
        "vix_observation.as_of": "provider_observation_date",
        "vix_observation.quote_time": "provider_raw",
        "vix_observation.read_at": "utc_iso8601",
    }
    rows = [row for row in rows if isinstance(row, dict)]
    results = []
    for stage in ("preflight", "experiment", "sizing"):
        for field, unit in fields.items():
            values = []
            for row in rows:
                value = row.get("regime_reads", {})
                for part in (stage, *field.split(".")):
                    value = value.get(part) if isinstance(value, dict) else None
                if value is not None and value != "":
                    values.append(value)
            counts = Counter(str(value) for value in values)
            n, unique = len(values), len(counts)
            verdict = "INSUFFICIENT" if n < 10 else "WARNING_CONST" if unique < 2 else "OK"
            results.append({"stage": stage, "field": field, "unit": unit,
                            "n": n, "missing": len(rows) - n, "unique": unique,
                            "verdict": verdict})
    return {"record_only": True, "row_count": len(rows), "fields": results,
            "caveat": "行可重复同一轮或缓存观测；恒定也可能来自稳定市场/单一来源，不证明传感器损坏或方向edge。"}


def main() -> int:
    ap = argparse.ArgumentParser(description="离散字段基数自检 (W0)")
    ap.add_argument("--days", type=int, default=30, help="回看天数 (默认30)")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    ap.add_argument("--regime-audit", type=Path,
                    help="仅只读指定 sizing_delta JSONL，基数警告不改退出码或发通知")
    args = ap.parse_args()

    if args.regime_audit is not None:
        print(json.dumps(regime_audit_cardinality(_read_jsonl(args.regime_audit)),
                         ensure_ascii=False, indent=2))
        return 0

    results = collect(args.days)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"{'来源':<16}{'字段':<26}{'n':>5} {'唯一值':>6} {'最大单值':>22}"
              f" {'占比':>7} {'熵':>6}  判定")
        print("-" * 100)
        for r in results:
            top = str(r["top_value"])
            if len(top) > 20:
                top = top[:19] + "…"
            print(f"{r['source']:<16}{r['field']:<26}{r['n']:>5} "
                  f"{r['unique']:>6} {top:>22} {r['top_share']:>7.1%} "
                  f"{r['entropy']:>6.3f}  {r['verdict']}")
    n_degen = sum(1 for r in results if r["verdict"] == "DEGEN")
    if n_degen:
        print(f"\n⚠️  {n_degen} 个字段 DEGEN (n≥30)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
