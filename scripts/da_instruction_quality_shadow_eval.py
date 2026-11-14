#!/usr/bin/env python3
"""DA 指令质量日报告影子 — 机械评估器 (只读; 零生产耦合; 不写任何文件)。

输入: `data/shadow/da_instruction_quality/latest.json`
      (写方: scripts/da_instruction_quality_shadow.py)
      或 `--date YYYY-MM-DD` 指向 `data/shadow/da_instruction_quality/<date>/<hash>.json`。

输出状态 (刻意只有四态):
  OK       文件可读、schema 合法、有样本, 且一致性/几何率/时效全部达标。
  WARN     **仅报告态** (report_only=True): 一致率或几何率低于阈值, 或报告过旧。
           不改 gate、不下单、不写任何生产状态, 退出码仍为 0。
  NO_DATA  文件缺失, 或 `metrics.total == 0` (当天没有可读指令卡)。
  ERROR    文件不可解析 / schema 不符 / 关键字段缺失 / generated_at 不可解析。

读数:
  `direction_plan_consistent_rate`  SKIP 执行字段契约的一致性
  `price_geometry_rate`             方向单 stop/entry/tp 几何合法性 (未知计入失败)
  `age_hours`                       latest 报告龄 (小时), 超过 MAX_AGE_HOURS → WARN
  `date_age_days`                   报告 ET 日期距今天数 (跨日补跑可见)

阈值是**报告阈值**, 不是生产门 (改阈值 = 重新预注册观测口径, 须另立 DEB):
  MIN_CONSISTENCY_RATE = 0.90
  MIN_GEOMETRY_RATE    = 0.90
  MAX_AGE_HOURS        = 36.0

退出码: OK/WARN=0, NO_DATA=1, ERROR=2。

用法:
  venv/bin/python scripts/da_instruction_quality_shadow_eval.py
  venv/bin/python scripts/da_instruction_quality_shadow_eval.py --date 2026-11-12 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = "da_instruction_quality_shadow_v1"
DEFAULT_ROOT = ROOT / "data" / "shadow" / "da_instruction_quality"

STATE_OK = "OK"
STATE_WARN = "WARN"
STATE_NO_DATA = "NO_DATA"
STATE_ERROR = "ERROR"

MIN_CONSISTENCY_RATE = 0.90
MIN_GEOMETRY_RATE = 0.90
MAX_AGE_HOURS = 36.0

REPORT_ONLY_NOTE = (
    "WARN is report-only: it never gates entries, changes orders, or writes production state."
)


def _shadow_runtime():
    from scripts import shadow_runtime
    return shadow_runtime


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/New_York"))
    return parsed


def _age_hours(generated_at: Any, now: datetime) -> Optional[float]:
    ts = _parse_ts(generated_at)
    if ts is None:
        return None
    return round((now - ts).total_seconds() / 3600.0, 2)


def _date_age_days(day: Any, now: datetime) -> Optional[int]:
    text = str(day or "").strip()[:10]
    try:
        parsed = _date.fromisoformat(text)
    except ValueError:
        return None
    return (now.date() - parsed).days


def _rate_from_metrics(metrics: Dict[str, Any], key: str,
                       numerator: str, denominator: str) -> Optional[float]:
    value = metrics.get(key)
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    try:
        num = int(metrics.get(numerator) or 0)
        den = int(metrics.get(denominator) or 0)
    except (TypeError, ValueError):
        return None
    return round(num / den, 4) if den > 0 else None


def _base_result(path: Path) -> Dict[str, Any]:
    return {
        "schema": "da_instruction_quality_shadow_eval_v1",
        "state": STATE_ERROR,
        "path": str(path),
        "date": None,
        "content_hash": None,
        "total": None,
        "valid_card": None,
        "directional_count": None,
        "skip_count": None,
        "skip_inconsistent": None,
        "direction_plan_consistent_rate": None,
        "price_geometry_rate": None,
        "age_hours": None,
        "date_age_days": None,
        "thresholds": {
            "min_consistency_rate": MIN_CONSISTENCY_RATE,
            "min_geometry_rate": MIN_GEOMETRY_RATE,
            "max_age_hours": MAX_AGE_HOURS,
        },
        "warning_reasons": [],
        "report_only": True,
        "note": REPORT_ONLY_NOTE,
    }


def evaluate(path: Any = None, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """读一份日报告并给出 OK/WARN/NO_DATA/ERROR; 纯读, 不落盘。"""
    target = Path(path) if path is not None else DEFAULT_ROOT / "latest.json"
    result = _base_result(target)
    if now is None:
        now = datetime.now(ZoneInfo("America/New_York"))

    if not target.exists():
        result["state"] = STATE_NO_DATA
        result["error"] = "MISSING_FILE"
        return result

    data = _shadow_runtime().read_json(target)
    if not isinstance(data, dict):
        result["state"] = STATE_ERROR
        result["error"] = "UNREADABLE_OR_INVALID_JSON"
        return result

    if str(data.get("schema") or "") != SCHEMA:
        result["state"] = STATE_ERROR
        result["error"] = f"SCHEMA_MISMATCH: {data.get('schema')!r}"
        return result

    metrics = data.get("metrics")
    if not isinstance(metrics, dict) or not isinstance(metrics.get("total"), int):
        result["state"] = STATE_ERROR
        result["error"] = "MISSING_METRICS"
        return result

    result["date"] = data.get("date")
    result["content_hash"] = data.get("content_hash")
    result["total"] = metrics["total"]
    result["valid_card"] = metrics.get("valid_card")
    result["directional_count"] = metrics.get("directional_count")
    result["skip_count"] = metrics.get("skip_count")
    result["skip_inconsistent"] = metrics.get("skip_inconsistent")
    result["direction_plan_consistent_rate"] = _rate_from_metrics(
        metrics, "direction_plan_consistent_rate",
        "direction_plan_consistent", "valid_card")
    result["price_geometry_rate"] = _rate_from_metrics(
        metrics, "price_geometry_rate",
        "price_geometry_valid", "directional_count")
    result["age_hours"] = _age_hours(data.get("generated_at"), now)
    result["date_age_days"] = _date_age_days(data.get("date"), now)

    if metrics["total"] <= 0:
        result["state"] = STATE_NO_DATA
        return result

    if result["age_hours"] is None:
        result["state"] = STATE_ERROR
        result["error"] = "MISSING_OR_INVALID_GENERATED_AT"
        return result

    warnings: List[str] = []
    consistency = result["direction_plan_consistent_rate"]
    geometry = result["price_geometry_rate"]
    if not result["valid_card"]:
        warnings.append("no_valid_cards")
    if consistency is not None and consistency < MIN_CONSISTENCY_RATE:
        warnings.append("direction_plan_consistency_below_threshold")
    if geometry is not None and geometry < MIN_GEOMETRY_RATE:
        warnings.append("price_geometry_below_threshold")
    if result["age_hours"] > MAX_AGE_HOURS:
        warnings.append("stale_report")

    result["warning_reasons"] = warnings
    result["state"] = STATE_WARN if warnings else STATE_OK
    return result


def _resolve_date_artifact(root: Path, day: str) -> Path:
    """从历史目录里挑一份 (按 generated_at 最新; 解析失败按文件名)。"""
    directory = Path(root) / day
    candidates = sorted(directory.glob("*.json")) if directory.exists() else []
    if not candidates:
        return directory / "latest.json"      # 不存在 → NO_DATA

    def _key(path: Path):
        data = _shadow_runtime().read_json(path)
        if isinstance(data, dict):
            return (str(data.get("generated_at") or ""), path.name)
        return ("", path.name)

    return max(candidates, key=_key)


def _render_text(result: Dict[str, Any]) -> str:
    lines = [
        f"state={result['state']} date={result['date']} total={result['total']} "
        f"valid={result['valid_card']} consistency={result['direction_plan_consistent_rate']} "
        f"geometry={result['price_geometry_rate']} age_hours={result['age_hours']} "
        f"date_age_days={result['date_age_days']}",
    ]
    if result.get("error"):
        lines.append(f"error={result['error']}")
    if result.get("warning_reasons"):
        lines.append("warnings=" + ",".join(result["warning_reasons"]))
    lines.append(f"path={result['path']}")
    if result["state"] == STATE_WARN:
        lines.append("note: WARN is report-only; no gate/order/production effect.")
    return "\n".join(lines)


def _exit_code(state: str) -> int:
    if state in (STATE_OK, STATE_WARN):
        return 0
    if state == STATE_NO_DATA:
        return 1
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=None, help="显式报告文件路径")
    parser.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--now", default=None,
                        help="ISO 时间; 仅供测试/复盘固定年龄口径")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    if args.path:
        target = Path(args.path)
    elif args.date:
        target = _resolve_date_artifact(Path(args.root), args.date)
    else:
        target = Path(args.root) / "latest.json"

    now = _parse_ts(args.now) if args.now else datetime.now(ZoneInfo("America/New_York"))
    if args.now and now is None:
        print(f"ERROR: --now 不是可解析的 ISO 时间: {args.now}", file=sys.stderr)
        return 2

    result = evaluate(target, now=now)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(_render_text(result))
    return _exit_code(result["state"])


if __name__ == "__main__":
    sys.exit(main())