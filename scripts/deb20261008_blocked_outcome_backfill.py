#!/usr/bin/env python3
"""DEB-20261008 F4: 被组合层闸拦掉指令的事后结局回填 (测量面, 零交易行为变更).

背景: 组合层闸 (G4 方向偏斜 / G6 sector 集中度) 把被拦行 direction 原地改写成
SKIP, 而 ic_tracker 回填只覆盖 direction != 'SKIP' ⇒ 被拦行 fwd_return 填充率
0.0% (n=78, 2026-09-01 起), 「闸拦对没拦对」结构性不可判。F1 摘闸后该人群
冻结为历史存量 (不再有新被拦行) ⇒ 一次性回填即可, 不挂 cron。

方向恢复 (原方向已被闸改写, 须取证):
  G4 「方向偏斜超限 … (多数方 BUY/SELL_SHORT …)」: 受害行恒为多数方
     (pipeline._trim_majority_direction 语义) ⇒ 从 skip_reason 解析, 确定性。
  G6 「sector 集中度 …」: 受害行方向不在文案 ⇒ 查 DA checkpoint
     data/da_checkpoint/<date>/<symbol>.json 的 _llm_output.direction;
     兜底 bridge data/opportunity_scan_<date>.json 候选 direction。

口径 (与 ic_tracker.backfill_forward_returns 逐字一致):
  fwd_return_intraday      = sign × (close_d0 − entry_price) / entry_price
  fwd_return_intraday_open = sign × (close_d0 − open_d0) / open_d0   (诚实尺子)
  fwd_return_t1 / _open    = 同式, close 换次一交易日 (交易日历)
  sign = BUY:+1 / SELL_SHORT:−1。entry 锚定含未成交限价纸面优势, open 锚定无
  (ic_tracker 2026-08-01 双价格基准纪律) ⇒ 两口径都落, 裁决用 open 列。

幂等: 只填当前为 NULL 的字段, 重跑零写入; 逐行取证链落
  data/shadow/blocked_outcome_backfill.jsonl (event 级, 重跑幂等去重)。
回滚: 本脚本只填 NULL 不覆盖 ⇒ 无回滚需求; 撤销填值须按 jsonl 逐行 UPDATE NULL。

用法:
  PYTHONPATH=. venv/bin/python scripts/deb20261008_blocked_outcome_backfill.py [--dry]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("ftseek.scripts.blocked_outcome_backfill")

# 加载 .env.us (MarketDataRouter 从 env 读 key) — 与 backfill_forward_returns.py 同模式
_ENV_FILE = PROJECT_ROOT / ".env.us"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

PROVENANCE_PATH = PROJECT_ROOT / "data" / "shadow" / "blocked_outcome_backfill.jsonl"
_SINCE = "2026-09-01"   # 组合层闸活跃窗 (G4 裁决起点)
_MAJORITY_RE = re.compile(r"多数方\s*(BUY|SELL_SHORT)")


def _sign(direction: str) -> int:
    return -1 if str(direction or "").upper() in ("SELL_SHORT", "SHORT") else 1


def _recover_direction(row: Dict[str, Any]) -> tuple[Optional[str], str]:
    """返回 (方向, 取证源)。G4 从文案解析 (受害行恒为多数方); G6 查 checkpoint→bridge。"""
    reason = str(row.get("skip_reason") or "")
    m = _MAJORITY_RE.search(reason)
    if m:
        return m.group(1), "skip_reason:majority"
    # G6 / 其他: DA checkpoint
    ckpt = (PROJECT_ROOT / "data" / "da_checkpoint" / str(row["date"])
            / f"{row['symbol']}.json")
    if ckpt.exists():
        try:
            doc = json.loads(ckpt.read_text(encoding="utf-8"))
            direction = ((doc.get("_llm_output") or {}).get("direction") or "").upper()
            if direction in ("BUY", "SELL_SHORT"):
                return direction, "da_checkpoint"
        except (OSError, ValueError) as read_err:
            logger.warning("checkpoint 读取失败 %s: %s", ckpt, read_err, exc_info=True)
    # 兜底: bridge 候选
    bridge = PROJECT_ROOT / "data" / f"opportunity_scan_{row['date']}.json"
    if bridge.exists():
        try:
            doc = json.loads(bridge.read_text(encoding="utf-8"))
            for cand in doc.get("candidates") or []:
                if str(cand.get("symbol") or "") == str(row["symbol"]):
                    direction = str(cand.get("direction") or "").upper()
                    mapped = {"LONG": "BUY", "SHORT": "SELL_SHORT"}.get(direction, direction)
                    if mapped in ("BUY", "SELL_SHORT"):
                        return mapped, "bridge_candidate"
        except (OSError, ValueError) as read_err:
            logger.warning("bridge 读取失败 %s: %s", bridge, read_err, exc_info=True)
    return None, "unrecoverable"


def _bars_by_date(router, symbol: str) -> Dict[str, Dict[str, Any]]:
    bars = router.get_daily_bars(symbol, count=45)
    out: Dict[str, Dict[str, Any]] = {}
    for bar in bars or []:
        bar_date = bar.get("time", "")
        if bar_date:
            out[bar_date] = bar
    return out


def _compute_fills(direction: str, entry: float, inst_date: str,
                   bars: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    """与 ic_tracker 逐字同口径的四列回填 (entry/open 双锚 × 当日/次日)。"""
    from app.utils.holiday_calendar import nth_trading_day_after

    updates: Dict[str, float] = {}
    sign = _sign(direction)
    day0 = bars.get(inst_date)
    open0 = float((day0 or {}).get("open", 0) or 0)
    close0 = float((day0 or {}).get("close", 0) or 0)
    if close0 > 0:
        updates["fwd_return_intraday"] = round(sign * (close0 - entry) / entry, 6)
        if open0 > 0:
            updates["fwd_return_intraday_open"] = round(sign * (close0 - open0) / open0, 6)
    t1 = nth_trading_day_after("US", date.fromisoformat(inst_date), 1)
    if t1 is not None:
        bar1 = bars.get(t1.isoformat())
        close1 = float((bar1 or {}).get("close", 0) or 0)
        if close1 > 0:
            updates["fwd_return_t1"] = round(sign * (close1 - entry) / entry, 6)
            if open0 > 0:
                updates["fwd_return_t1_open"] = round(sign * (close1 - open0) / open0, 6)
    return updates


def _write_provenance(records: list[Dict[str, Any]], path: Path = PROVENANCE_PATH,
                      *, dry: bool = False) -> None:
    """事件级取证台账; dry-run 必须零写盘, 不得覆盖已填充记录。"""
    if dry:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: Dict[int, Dict[str, Any]] = {}
    if path.exists():
        bad = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                existing[int(record.get("id") or 0)] = record
            except (json.JSONDecodeError, ValueError):
                bad += 1
        if bad:
            logger.warning("取证台账 %s 有 %d 行坏 JSON 已跳过", path, bad)
    for record in records:
        existing[int(record["id"])] = record
    with path.open("w", encoding="utf-8") as handle:
        for record in sorted(existing.values(), key=lambda item: int(item["id"])):
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true", help="只算不写 (预览填充率)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    from app.playbook.db import PlaybookDB
    db = PlaybookDB()  # 生产库 (写回填值; 幂等 NULL-only)
    conn = db._conn()
    rows = conn.execute(
        """
        SELECT id, date, symbol, skip_reason, entry_price,
               fwd_return_intraday, fwd_return_intraday_open,
               fwd_return_t1, fwd_return_t1_open
        FROM playbook_instructions
        WHERE direction = 'SKIP' AND entry_price > 0 AND date >= ?
          AND (skip_reason LIKE '%方向偏斜%guardrail%'
               OR skip_reason LIKE '%sector%guardrail%'
               OR skip_reason LIKE '%行业集中度%guardrail%'
               OR skip_reason LIKE '%组合方向偏斜%guardrail%'
               OR skip_reason LIKE '%组合行业集中度%guardrail%')
        ORDER BY date, id
        """,
        (_SINCE,),
    ).fetchall()
    rows = [dict(r) for r in rows]
    logger.info("被拦行 (gate SKIP, entry_price>0, since %s): n=%d", _SINCE, len(rows))

    from app.data.market_data_router import MarketDataRouter
    router = MarketDataRouter(
        twelvedata_api_key=os.environ.get("TWELVEDATA_API_KEY"),
        finnhub_api_key=os.environ.get("FINNHUB_API_KEY"),
        alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY"),
        cache_ttl_seconds=900,
        futu_opend_host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
        futu_opend_port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
        enable_futu_lv3=os.environ.get("ENABLE_FUTU_LV3", "true").lower() in ("true", "1", "yes"),
    )

    bars_memo: Dict[str, Dict[str, Dict[str, Any]]] = {}
    provenance: list[Dict[str, Any]] = []
    n_filled = n_already = n_unrecoverable = 0
    for row in rows:
        # 幂等按字段级: 只填当前 NULL 的列 (当日 intraday 已填、次日 bar 未出的行,
        # 次日 rerun 仍可补 t1 — 行级跳过会永久漏补)
        direction, source = _recover_direction(row)
        if direction is None:
            n_unrecoverable += 1
            provenance.append({"id": row["id"], "date": row["date"],
                               "symbol": row["symbol"], "status": "direction_unrecoverable"})
            continue
        symbol = str(row["symbol"])
        if symbol not in bars_memo:
            try:
                bars_memo[symbol] = _bars_by_date(router, symbol)
            except Exception as bars_err:  # 单标的失败不阻断全批 (R5 留痕)
                logger.warning("拉取日线失败 %s: %s", symbol, bars_err, exc_info=True)
                bars_memo[symbol] = {}
        bars = bars_memo[symbol]
        if not bars:
            n_unrecoverable += 1
            provenance.append({"id": row["id"], "date": row["date"],
                               "symbol": row["symbol"], "status": "no_bars"})
            continue
        computed = _compute_fills(direction, float(row["entry_price"]),
                                  str(row["date"]), bars)
        updates = {k: v for k, v in computed.items() if row.get(k) is None}
        prov = {"id": row["id"], "date": row["date"], "symbol": symbol,
                "recovered_direction": direction, "recovery_source": source,
                "entry_price": row["entry_price"], "fills": updates,
                "status": "dry" if args.dry else "filled"}
        if not updates:
            if computed:
                prov["status"] = "already_filled"
                n_already += 1
            else:
                prov["status"] = "no_bars_in_window"
                n_unrecoverable += 1
        elif not args.dry:
            db.update_instruction_tracking(row["id"], updates)
            n_filled += 1
        else:
            n_filled += 1
        provenance.append(prov)

    # 取证链落盘 (幂等: 按 id 去重, 重跑以末次为准; --dry 零写盘)
    try:
        _write_provenance(provenance, dry=args.dry)
    except Exception as prov_err:  # 台账失败不影响回填结果 (已落库), 但必须可见
        logger.warning("取证台账写入失败: %s", prov_err, exc_info=True)

    total = len(rows)
    print(f"\n═══ F4 被拦行结局回填 {'(DRY)' if args.dry else ''} ═══")
    print(f"  人群 n={total} | 本次填充 {n_filled} | 已有值跳过 {n_already} | "
          f"不可恢复 {n_unrecoverable}")
    fillable = total - n_already
    rate = (100.0 * (n_filled + n_already) / total) if total else 0.0
    print(f"  填充率 (含既有): {rate:.1f}%  (判据 ≥80%: "
          f"{'✅' if rate >= 80 else '❌'}; 本次可填 {fillable})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
