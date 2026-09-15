#!/usr/bin/env python3
"""UE 报价新鲜度门影子 — **机械裁决器** (只读; 零生产耦合; 不写任何生产状态)。

════════ 为什么存在 (2026-09-15 补口, 用户裁「请执行」) ═══════

采集环 (`crontab-ue-quote-freshness-shadow`, 15 21 * * 1-5) 挂上时, "≥10 交易日后再裁"
这句**只写在 crontab 注释与台账文字里**: 注册表无条目、jsonl 无消费者 ⇒ 到期**不会有人
被提醒去读尺子**。本脚本 + `shadow_registry.ue_quote_freshness_shadow` 就是补这个口 ——
把"谁在什么时候、按什么判据读这把尺子"变成可执行的东西 (同族病灶: 窗口制影子的
"采集开关"与"裁决开关"是两件事, 两者都要写死触发点; 参见 DEB-20260915 的 da_glm_shadow
教训 —— 那边是裁决器自删而采集行没人摘, 这边是采集行在跑而无人裁决器)。

════════ 预注册判据 (写死于台账 §2 + crontab 注释, **本脚本不改判据**) ════════

  ≥10 个**交易日有效轮次** ∧ 盘前 `genuine` 龄 **P95 ≤ 候选阈值** ⇒ 方可提议激活
  (另立 DEB + 用户裁); **有效轮次 <10 时禁止建议激活** (同 thinking-eval 先例)。

**有效轮次**定义 (本脚本实现, 2026-09-15 定): 落盘行中满足全部三条的**去重 ET 日期**数 —
  ① `status == "ok"` (取数/策略都拿到了, 不是降级行);
  ② `forced_non_trading_day` 不为真 (`--force` 人工核查行不冒充轮次);
  ③ 交易日判定为真 —— 行内 `is_trading_day` 字段优先; 老版本无该字段时按 `date` 回查日历,
     日历不可得则该行**不计** (fail-safe: 缺一轮是安全的, 假一轮不安全)。
  为什么这么严: 假日报价来自上一交易日收盘 ⇒ `genuine` 龄小时级 ⇒ 一条假日样本既冒充
  一个轮次, 又能把 P95 拽向"激活必杀"的假结论 (与影子内 §22.6 "朴素 now 制造假拒绝" 同族)。

**阈值不硬编码**: 取影子行自己的 `threshold_sec` (那是运行时从活配置读到的
`max_quote_age_sec`); 行间不一致时**并列披露**而不是挑一个 (事后挑尺子纪律)。
判据不过时**不得**改阈值重算 —— 改阈值 = 重新预注册。

用法: venv/bin/python scripts/ue_quote_freshness_shadow_eval.py [--jsonl PATH]
产物: data/shadow/ue_quote_freshness_shadow_eval_latest.json (data/* 不入库, 同既有影子)
回滚: 删本文件 (纯只读, 无调用方)。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ET = ZoneInfo("America/New_York")
SHADOW_PATH = _ROOT / "data" / "shadow" / "ue_quote_freshness_shadow.jsonl"
OUT_JSON = _ROOT / "data" / "shadow" / "ue_quote_freshness_shadow_eval_latest.json"

MIN_ROUNDS = 10          # 预注册: ≥10 交易日有效轮次 (改此值 = 重新预注册)


def _pct(values: List[float], q: float) -> Optional[float]:
    """线性插值分位数 (n=1 直接返回该值; 空集 None)。"""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo), 3)


def _is_trading_date(d: str) -> Optional[bool]:
    """按 ET 日期回查交易日 (老版本行无 `is_trading_day` 字段时用)。

    **复用探针的守卫助手**, 不自己抄一份日历判据 (同一门不能有两个真源); 那个助手是纯本地
    的, 不连 futu (`app.utils.holiday_calendar` 实测会开 OpenQuoteContext, 见探针注释)。
    """
    try:
        from scripts.ue_quote_freshness_shadow import us_trading_day_flag
        return us_trading_day_flag(_date.fromisoformat(d))
    except Exception as exc:
        print(f"  [warn] 交易日判定助手不可用 ⇒ 老行按「不计轮次」处理 (fail-safe): {exc}")
        return None


def load_rounds(path: Path) -> Tuple[Dict[str, List[dict]], Dict[str, int]]:
    """读影子 jsonl ⇒ (有效轮次 {日期: [行]}, 计数台账)。只读, 不落盘。"""
    rounds: Dict[str, List[dict]] = {}
    tally = {"lines": 0, "bad_lines": 0, "not_ok": 0, "forced": 0, "non_trading": 0,
             "undecidable": 0}
    if not path.exists():
        return rounds, tally
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        tally["lines"] += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            tally["bad_lines"] += 1
            continue
        if str(rec.get("status")) != "ok":
            tally["not_ok"] += 1
            continue
        if rec.get("forced_non_trading_day"):
            tally["forced"] += 1
            continue
        d = str(rec.get("date") or "")[:10]
        if not d:
            tally["undecidable"] += 1
            continue
        flag = rec.get("is_trading_day")
        if flag is None:
            flag = _is_trading_date(d)
        if flag is False:
            tally["non_trading"] += 1
            continue
        if flag is None:
            tally["undecidable"] += 1
            continue
        rounds.setdefault(d, []).append(rec)
    return rounds, tally


def _stale(row: dict, hyp: str) -> bool:
    """从影子行的嵌套 verdicts 里取某假设是否落 `stale_quote` (字段名以探针为准)。"""
    try:
        return "stale_quote" in row["verdicts"][hyp]["reasons"]
    except (KeyError, TypeError):
        return False


def evaluate(rounds: Dict[str, List[dict]]) -> Dict[str, Any]:
    rows = [r for recs in rounds.values() for rec in recs for r in rec.get("rows", [])]
    ages = [float(r["age_sec_genuine"]) for r in rows
            if isinstance(r.get("age_sec_genuine"), (int, float))]
    thresholds = sorted({float(rec.get("threshold_sec")) for recs in rounds.values()
                         for rec in recs if isinstance(rec.get("threshold_sec"), (int, float))})
    n_rounds = len(rounds)
    threshold = thresholds[-1] if thresholds else None
    p95 = _pct(ages, 0.95)
    out: Dict[str, Any] = {
        "n_rounds": n_rounds, "min_rounds": MIN_ROUNDS,
        "rounds": sorted(rounds.keys()), "n_rows": len(rows),
        "thresholds_seen": thresholds, "threshold_used": threshold,
        "age_genuine_p50": _pct(ages, 0.5), "age_genuine_p95": p95,
        "age_genuine_max": (round(max(ages), 3) if ages else None),
        "n_stale_genuine": sum(1 for r in rows if _stale(r, "genuine")),
        "n_newly_rejected_genuine": sum(1 for r in rows
                                        if r.get("newly_rejected_genuine")),
        "n_alias_washes_stale": sum(1 for r in rows if r.get("alias_washes_stale")),
        "n_future_quote_genuine": sum(1 for r in rows if r.get("future_quote_genuine")),
        "n_exchange_stamp_available": sum(1 for r in rows
                                          if r.get("exchange_stamp_available")),
    }
    if n_rounds < MIN_ROUNDS:
        out["verdict"] = "INSUFFICIENT_RECORD"
        out["verdict_text"] = (f"有效轮次 {n_rounds}/{MIN_ROUNDS} — **禁止建议激活** "
                               f"(预注册; 同 thinking-eval 先例)。P95={p95}s 仅供参考, 不作证据。")
    elif threshold is not None and p95 is not None and p95 <= threshold:
        out["verdict"] = "READY_TO_PROPOSE_ACTIVATION"
        out["verdict_text"] = (f"有效轮次 {n_rounds} ≥ {MIN_ROUNDS} 且 genuine 龄 P95={p95}s ≤ "
                               f"现役阈值 {threshold}s ⇒ 可提议激活并定盘前专属阈值 "
                               f"(须另立 DEB + 用户裁; 本脚本不自行激活)")
    else:
        out["verdict"] = "NOT_READY"
        out["verdict_text"] = (f"有效轮次 {n_rounds} ≥ {MIN_ROUNDS} 但 P95={p95}s > 阈值 "
                               f"{threshold}s ⇒ 现值激活会系统性误杀。**不得事后改阈值重算** "
                               f"— 若要用盘前专属阈值, 须重新预注册.")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jsonl", default=str(SHADOW_PATH), help="影子 jsonl 路径")
    parser.add_argument("--no-write", action="store_true", help="只打印, 不落 latest.json")
    args = parser.parse_args(argv)

    print(f"═══ UE 报价新鲜度门影子 · 机械裁决 (预注册: ≥{MIN_ROUNDS} 交易日有效轮次 ∧ P95 ≤ 阈值) ═══")
    rounds, tally = load_rounds(Path(args.jsonl))
    print(f"读数行 {tally['lines']} (坏行 {tally['bad_lines']} / 非 ok {tally['not_ok']} / "
          f"forced {tally['forced']} / 非交易日 {tally['non_trading']} / 判定不可得 {tally['undecidable']})")
    if tally["non_trading"] or tally["forced"]:
        print(f"   已排除非交易日/forced 行 {tally['non_trading'] + tally['forced']} 条 — "
              f"假日报价龄小时级, 会计入即污染 P95")
    res = evaluate(rounds)
    print(f"有效轮次 {res['n_rounds']}/{res['min_rounds']}  交易日: "
          f"{', '.join(res['rounds']) if res['rounds'] else '(无)'}")
    print(f"  genuine 龄 P50={res['age_genuine_p50']}s P95={res['age_genuine_p95']}s "
          f"max={res['age_genuine_max']}s (阈值取值 {res['thresholds_seen']})")
    print(f"  超阈={res['n_stale_genuine']} 会新拒={res['n_newly_rejected_genuine']} "
          f"别名洗白={res['n_alias_washes_stale']} future_quote={res['n_future_quote_genuine']} "
          f"交易所时戳可得={res['n_exchange_stamp_available']}/{res['n_rows']}")
    print(f"【裁决】{res['verdict']}: {res['verdict_text']}")
    res["generated_at"] = datetime.now(_ET).isoformat()
    res["tally"] = tally
    if not args.no_write:
        try:
            OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
            OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  → {OUT_JSON.relative_to(_ROOT)}")
        except OSError as exc:
            print(f"  [warn] 裁决结果落盘失败 (读数已打印): {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())