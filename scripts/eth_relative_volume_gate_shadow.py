#!/usr/bin/env python3
"""ETH 影子⑤：事件相对量闸 (record-only, 零下单).

提案文档 (唯一规格来源): docs/analysis/2026-09-24-eth-microstructure-shadow-preregistrations.md §⑤

## 预注册判据 (从提案逐字复制, 读数前写死; 读数出来前不得改本段)

采样门：≥15 个交易日 且 would_pass_relative=true 样本 n≥30；
立项 DEB 条件（三条全满足）：① cf_net_usd/笔 中位数 > 0 ②胜率 ≥55% ③剔除最强单日后 ①仍 > 0；
否决：任一不满足 → 弃案，绝对量闸维持现状，影子停采留档；
强制披露：反事实无排队/冲击建模，读数是上界；判据用上界仍不过线则结论更稳。
死线：2026-11-30 前未达采样门 → 自动停采留档（防僵尸影子，EXT 臂死线先例）。

## 人群

RFC-104 计划内事件票 (data/loops/ext_entry_plan/<date>_<session>.json) 中
被 executor **绝对量闸拒绝** (reject_reason = "turnover_too_low") 的票-轮。
数据源: data/extended_hours_pretrade_gate_rejects.jsonl。

## 字段可得性探查结论 (2026-09-24)

- 拒绝台账 (pretrade_gate_rejects) 中 turnover_too_low 行包含:
  symbol, decision_price, reason, quote{bid, ask, ask_volume, market_cap, spread_pct}
  **不含当轮 volume 或 turnover 数值** (executor 变量未落盘)。
- 既有 K 线缓存 (data/replay_bars_1m/) 仅覆盖 RTH (09:31+), 无盘前/盘后分时段量。
  (carry 影子曾证"盘前量历史不可得" — 与本探查一致。)
- data/replay_bars_daily/ 仅含日K [date, o, c, h, l], 无 volume 列。

## Median basis 选择: 前向自积累 (option a)

理由: 延长时段分时段量历史不可得 (上述探查); RTH 全天量与盘前量口径不同源,
禁止冒充 (铁律)。本影子每轮记录该票当轮 decision_price × ask_volume 作为
周转近似 (非真实 turnover, 如实披露), 中位数在自身历史满 20 交易日后才可算,
此前行记 ratio=null + basis="accumulating"。

## 反事实 (cf_net_usd) 构造

以拒绝轮之后**首个可观测成交价** (保守 taker 语义: 买入按 ask、含全费+当时点价差)
模拟入场、按现行退出规则近似出场, 记 cf_net_usd。
无法可靠构造反事实的轮记 cf_net_usd=null + cf_basis 原因, **不硬算**。
当前实现: 从 extended_hours_entry_executions.jsonl 查后续同票同 session 执行;
若无执行记录 → cf_net_usd=null, cf_basis="no_subsequent_execution_observable"。

## 载体

- 零下单、零推送、零配置改动; 只读消费既有台账。
- sqlite 一律 file:...?mode=ro (本脚本不读 sqlite, 仅 jsonl)。
- ZoneInfo ET; 幂等键 (et_date, symbol, session, round_ts)。
- 自门控: reconcile_ledger.jsonl PASS 日数 ≥ 7 才开跑。
- 死线: 2026-11-30 (到期自动停采留档)。
- 新代码零 except:pass (warning + exc_info)。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

ET = ZoneInfo("America/New_York")

# ── 路径 ──
REJECTS_PATH = ROOT / "data" / "extended_hours_pretrade_gate_rejects.jsonl"
PLAN_DIR = ROOT / "data" / "loops" / "ext_entry_plan"
RECONCILE_PATH = PLAN_DIR / "reconcile_ledger.jsonl"
EXECUTIONS_PATH = ROOT / "data" / "extended_hours_entry_executions.jsonl"
OUT_PATH = ROOT / "data" / "shadow" / "eth_relative_volume_gate_shadow.jsonl"

# ── 写死常量 (提案原文) ──
RELATIVE_RATIO_THRESHOLD = 1.5  # would_pass_relative = ratio ≥ 1.5
SENSITIVITY_THRESHOLDS = (1.2, 2.0)  # 仅记录, 不改判
MEDIAN_HISTORY_DAYS = 20  # 自身历史满 20 交易日后才可算中位数
SAMPLING_GATE_DAYS = 15  # ≥15 个交易日
SAMPLING_GATE_N = 30  # would_pass_relative=true 样本 n≥30
DEADLINE = date(2026, 11, 30)  # 死线: 2026-11-30
RECONCILE_PASS_MIN_DAYS = 7  # 自门控: PASS 日数 ≥ 7
FEE_PCT = float(0.53)  # 费率 % (与 rth_fade_shadow 同口径)


def _et_today() -> date:
    return datetime.now(ET).date()


def _check_deadline(today: date | None = None) -> bool:
    """死线检查: 返回 True 表示仍在有效期内。"""
    t = today or _et_today()
    return t <= DEADLINE


def _check_reconcile_gate() -> tuple[bool, int]:
    """自门控: reconcile_ledger.jsonl 中 PASS 日数 ≥ 7 才开跑。
    返回 (is_active, pass_day_count)。"""
    if not RECONCILE_PATH.exists():
        return False, 0
    pass_days: set[str] = set()
    try:
        with RECONCILE_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if str(row.get("verdict", "")).upper() == "PASS":
                        d = str(row.get("et_date", ""))
                        if d:
                            pass_days.add(d)
                except json.JSONDecodeError:
                    _log.warning("reconcile_ledger parse error", exc_info=True)
                    continue
    except OSError:
        _log.warning("reconcile_ledger read failed", exc_info=True)
        return False, 0
    n = len(pass_days)
    return n >= RECONCILE_PASS_MIN_DAYS, n


def _load_plan_symbols(et_date: str, session: str) -> set[str]:
    """加载计划文件中的事件票集合。"""
    plan_file = PLAN_DIR / f"{et_date}_{session}.json"
    if not plan_file.exists():
        return set()
    try:
        with plan_file.open() as f:
            rows = json.load(f)
        return {str(r.get("symbol", "")).upper() for r in rows if r.get("symbol")}
    except (json.JSONDecodeError, OSError):
        _log.warning("plan file parse error: %s", plan_file, exc_info=True)
        return set()


def _load_turnover_rejects(et_date: str, session: str) -> list[dict]:
    """从拒绝台账加载 turnover_too_low 拒绝行 (按日期+session 过滤)。"""
    if not REJECTS_PATH.exists():
        return []
    results = []
    target_prefix = f"{et_date}T"
    try:
        with REJECTS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    _log.warning("rejects parse error", exc_info=True)
                    continue
                ts = str(row.get("ts", ""))
                if not ts.startswith(target_prefix):
                    continue
                if str(row.get("session", "")) != session:
                    continue
                for rj in row.get("rejects", []):
                    if str(rj.get("reason", "")) == "turnover_too_low":
                        rj["_ts"] = ts
                        rj["_session"] = session
                        rj["_et_date"] = et_date
                        results.append(rj)
    except OSError:
        _log.warning("rejects read failed", exc_info=True)
    return results


def _load_volume_history(symbol: str, session: str) -> list[dict]:
    """从本影子台账加载该票历史 (前向自积累)。"""
    if not OUT_PATH.exists():
        return []
    sym_key = f"US.{symbol}" if not symbol.startswith("US.") else symbol
    history = []
    try:
        with OUT_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as _exc:
                    _log.warning("_load_volume_history: 忽略异常继续 (复核修复 2026-09-24, 控制流零变更): %s", _exc, exc_info=True)
                    continue
                if (str(row.get("symbol", "")) == sym_key
                        and str(row.get("session", "")) == session
                        and row.get("turnover_approx") is not None):
                    history.append(row)
                elif (str(row.get("symbol", "")) == symbol
                        and str(row.get("session", "")) == session
                        and row.get("turnover_approx") is not None):
                    history.append(row)
    except OSError as _exc:
        _log.warning("_load_volume_history: 忽略异常继续 (复核修复 2026-09-24, 控制流零变更): %s", _exc, exc_info=True)
    return history


def _compute_median_baseline(history: list[dict], min_days: int = MEDIAN_HISTORY_DAYS) -> float | None:
    """计算自身中位数基线。历史满 min_days 个交易日才返回中位数, 否则 None。"""
    # 按 et_date 去重 (同一日只取一个 turnover_approx)
    by_date: dict[str, float] = {}
    for row in history:
        d = str(row.get("et_date", ""))
        ta = row.get("turnover_approx")
        if d and ta is not None and ta > 0:
            by_date.setdefault(d, float(ta))
    if len(by_date) < min_days:
        return None
    return median(by_date.values())


def _load_subsequent_executions(symbol: str, session: str, et_date: str,
                                 after_ts: str) -> list[dict]:
    """查找拒绝后同票同 session 的执行记录 (用于反事实)。"""
    if not EXECUTIONS_PATH.exists():
        return []
    sym_key = f"US.{symbol}" if not symbol.startswith("US.") else symbol
    results = []
    try:
        with EXECUTIONS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as _exc:
                    _log.warning("_load_subsequent_executions: 忽略异常继续 (复核修复 2026-09-24, 控制流零变更): %s", _exc, exc_info=True)
                    continue
                if (str(row.get("symbol", "")) in (sym_key, symbol)
                        and str(row.get("session", "")) == session):
                    ts = str(row.get("ts", ""))
                    if ts > after_ts:
                        results.append(row)
    except OSError as _exc:
        _log.warning("_load_subsequent_executions: 忽略异常继续 (复核修复 2026-09-24, 控制流零变更): %s", _exc, exc_info=True)
    return results


def _compute_cf_net_usd(reject_row: dict, executions: list[dict]) -> tuple[float | None, str]:
    """计算反事实净收益。

    返回 (cf_net_usd, cf_basis)。无法可靠构造时返回 (None, reason)。
    """
    quote = reject_row.get("quote") or {}
    ask = quote.get("ask")
    bid = quote.get("bid")
    if not ask or not bid or ask <= 0 or bid <= 0:
        return None, "no_valid_quote_for_cf"

    # 保守 taker 语义: 买入按 ask
    entry_price = float(ask)

    # 查找后续首个可观测成交价 (执行记录中的 avg_fill_price 或 price)
    exit_price = None
    for ex in sorted(executions, key=lambda x: str(x.get("ts", ""))):
        fill = ex.get("fill", {})
        afp = fill.get("avg_fill_price")
        if afp and float(afp) > 0:
            exit_price = float(afp)
            break
        p = ex.get("price")
        if p and float(p) > 0:
            exit_price = float(p)
            break

    if exit_price is None:
        return None, "no_subsequent_execution_observable"

    # 净收益 = (exit - entry) / entry × size - fees
    # 简化: 1 股口径, 含双边费 (FEE_PCT%)
    ret_pct = (exit_price - entry_price) / entry_price * 100
    net_pct = ret_pct - 2 * FEE_PCT  # 双边费
    # 含价差 (bid/ask 之间的成本已包含在 entry=ask 里)
    cf_net_usd = round(net_pct / 100 * entry_price, 4)  # 1 股口径 USD
    cf_basis = f"taker_ask_entry_1share_fee_{FEE_PCT}pct_each_way"
    return cf_net_usd, cf_basis


def _load_seen_keys() -> set[tuple[str, str, str, str]]:
    """加载已记录的幂等键。"""
    seen: set[tuple[str, str, str, str]] = set()
    if not OUT_PATH.exists():
        return seen
    try:
        with OUT_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    key = (str(row.get("et_date", "")),
                           str(row.get("symbol", "")),
                           str(row.get("session", "")),
                           str(row.get("round_ts", "")))
                    seen.add(key)
                except json.JSONDecodeError as _exc:
                    _log.warning("_load_seen_keys: 忽略异常继续 (复核修复 2026-09-24, 控制流零变更): %s", _exc, exc_info=True)
                    continue
    except OSError as _exc:
        _log.warning("_load_seen_keys: 忽略异常继续 (复核修复 2026-09-24, 控制流零变更): %s", _exc, exc_info=True)
    return seen


def collect(et_date: str | None = None, session: str | None = None,
            dry: bool = False) -> list[dict]:
    """主采集逻辑。返回新记录列表。"""
    today = _et_today()
    if not _check_deadline(today):
        print(f"[eth_vol_gate] 死线 {DEADLINE} 已过, 停采留档")
        return []

    active, pass_days = _check_reconcile_gate()
    if not active:
        print(f"[eth_vol_gate] dormant: reconcile PASS 日数 {pass_days}/{RECONCILE_PASS_MIN_DAYS}")
        return []

    target_date = et_date or today.isoformat()
    sessions = [session] if session else ["premarket", "afterhours"]
    seen = _load_seen_keys()
    fresh: list[dict] = []

    for sess in sessions:
        plan_syms = _load_plan_symbols(target_date, sess)
        if not plan_syms:
            continue
        rejects = _load_turnover_rejects(target_date, sess)
        for rj in rejects:
            raw_sym = str(rj.get("symbol", "")).replace("US.", "")
            if raw_sym not in plan_syms and f"US.{raw_sym}" not in plan_syms:
                continue  # 非计划内事件票
            sym = f"US.{raw_sym}" if not rj.get("symbol", "").startswith("US.") else str(rj["symbol"])
            ts = str(rj.get("_ts", ""))
            key = (target_date, sym, sess, ts)
            if key in seen:
                continue

            # 周转近似: decision_price × ask_volume (非真实 turnover, 如实披露)
            quote = rj.get("quote") or {}
            dp = rj.get("decision_price")
            ask_vol = quote.get("ask_volume")
            turnover_approx = None
            if dp and ask_vol and float(dp) > 0 and float(ask_vol) > 0:
                turnover_approx = round(float(dp) * float(ask_vol), 2)

            # 历史中位数 (前向自积累)
            history = _load_volume_history(raw_sym, sess)
            med = _compute_median_baseline(history)
            ratio = None
            basis = "accumulating"
            if med is not None and med > 0 and turnover_approx is not None:
                ratio = round(turnover_approx / med, 4)
                basis = f"self_median_{len({r.get('et_date') for r in history})}_days"

            would_pass = None
            if ratio is not None:
                would_pass = ratio >= RELATIVE_RATIO_THRESHOLD

            # 敏感性 (不改判)
            sens = {}
            if ratio is not None:
                for th in SENSITIVITY_THRESHOLDS:
                    sens[f"would_pass_{th}"] = ratio >= th

            # 反事实
            execs = _load_subsequent_executions(sym, sess, target_date, ts)
            cf_net, cf_basis = _compute_cf_net_usd(rj, execs)

            rec = {
                "et_date": target_date,
                "symbol": sym,
                "session": sess,
                "round_ts": ts,
                "decision_price": dp,
                "reject_reason": "turnover_too_low",
                "quote_bid": quote.get("bid"),
                "quote_ask": quote.get("ask"),
                "quote_ask_volume": ask_vol,
                "quote_market_cap": quote.get("market_cap"),
                "quote_spread_pct": quote.get("spread_pct"),
                "turnover_approx": turnover_approx,
                "turnover_approx_basis": "decision_price × ask_volume (非真实 turnover, 上限近似)",
                "median_baseline": med,
                "median_basis": basis,
                "median_history_days": len({str(r.get("et_date", "")) for r in history}),
                "relative_ratio": ratio,
                "would_pass_relative": would_pass,
                "relative_ratio_threshold": RELATIVE_RATIO_THRESHOLD,
                **sens,
                "cf_net_usd": cf_net,
                "cf_basis": cf_basis,
                "cf_fill_price_basis": "first_subsequent_execution_avg_fill_price" if cf_net is not None else None,
                "recorded_at": datetime.now(ET).isoformat(),
                "deadline": DEADLINE.isoformat(),
            }
            fresh.append(rec)
            seen.add(key)

    if fresh and not dry:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUT_PATH.open("a") as f:
            for rec in fresh:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[eth_vol_gate] 已追加 {len(fresh)} 行 → {OUT_PATH}")
    elif fresh:
        print(f"[eth_vol_gate] --dry 模式, {len(fresh)} 行未落盘")
    else:
        print(f"[eth_vol_gate] {target_date} 无新增 turnover_too_low 计划内事件票")

    return fresh


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="ETH 影子⑤: 事件相对量闸 (record-only)")
    ap.add_argument("--date", default=None, help="ET 日期 YYYY-MM-DD (默认今日)")
    ap.add_argument("--session", default=None, choices=["premarket", "afterhours"])
    ap.add_argument("--dry", action="store_true", help="只打印不落盘")
    args = ap.parse_args(argv)
    try:
        collect(et_date=args.date, session=args.session, dry=args.dry)
    except Exception:
        _log.warning("shadow⑤ collect failed", exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
