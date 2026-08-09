"""盘前失效评估器 (record-only shadow, 2026-08-09 用户批准方案1).

问题: 盘前指令基于昨日数据生成, 隔夜+盘前的真实变化 (跳空/逆向走) 目前要到
触发时刻才被门感知 — 盘前 09:30 前的信息完全没被用于指令侧校验。

机制: 每个交易日 09:35-09:59 ET 评估一次 — 对当日全部可执行指令取盘前快照
(futu get_snapshot, 历史连接路径), 计算盘前涨跌幅 vs prev_close, 按三条候选
规则打"若启用将失效"标记, 落 data/premarket_invalidation_shadow.jsonl。
零行为变更; 攒样本后与 trade_records 对照判定哪条规则有真价值。

候选规则 (全部来自已辩论证据):
  A: SHORT 且盘前涨>+3% — DEB-20260808 逆势追空亏损桶 (-$172)
  B: LONG 且盘前跌<-5%  — 接飞刀区 (DEB §5.3 / DA prompt v2.4 已禁产)
  C: LONG 且盘前涨>+3%  — 追涨延伸日 (Qoder ≥10:00 复算: 顺势 LONG -12.22/笔)
  注: LONG 盘前跌 -2%~0 (回调日) 是赢家模式 (+$103), 明确不打标。

回滚: playbook.premarket_invalidation_observe 置 false + 重启。
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_OUTPUT_PATH = Path("data/premarket_invalidation_shadow.jsonl")
_WINDOW = (935, 959)          # 09:35-09:59 ET 评估窗
_done_date = None             # 进程内每日一次

_RULES = (
    # (rule_id, direction, chg阈值方向, 阈值, 说明)
    # 枚举依据 (2026-08-09 DB 实查): playbook_instructions.direction 实际值为
    #   BUY(382)/SELL_SHORT(122)/SKIP(164) — "SHORT" 枚举仅存在于 trade_records.
    #   05b50d8f 把规则改成 "SHORT" 系误改 (其"实际指令枚举"说法与 DB 不符),
    #   改回 SELL_SHORT 并由 evaluate_rules 做双向归一防漂移。
    ("A_short_chase", "SELL_SHORT", ">", 3.0, "逆势追空 (DEB-20260808 亏损桶)"),
    ("B_long_knife", "BUY", "<", -5.0, "接飞刀区"),
    ("C_long_chase", "BUY", ">", 3.0, "追涨延伸日"),
)


def _canon_direction(direction: str) -> str:
    """方向枚举归一: SHORT/SELL_SHORT/SELL → SELL_SHORT (指令表口径)."""
    d = str(direction or "").upper()
    return "SELL_SHORT" if d in ("SHORT", "SELL_SHORT", "SELL") else d


def evaluate_rules(direction: str, chg_pct: float):
    """纯函数: 返回命中的 rule_id 列表 (可单测)."""
    direction = _canon_direction(direction)
    hits = []
    for rid, d, op, th, _ in _RULES:
        if direction != d:
            continue
        if op == ">" and chg_pct > th:
            hits.append(rid)
        elif op == "<" and chg_pct < th:
            hits.append(rid)
    return hits


def _observe_enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted(
            "playbook.premarket_invalidation_observe", True))
    except Exception:
        return False


def run_check(scheduler=None) -> int:
    """调度器钩子: 每日 09:35-09:59 ET 评估一次, 返回落盘行数. 异常静默."""
    global _done_date
    try:
        import pytz
        from datetime import datetime
        now_et = datetime.now(pytz.timezone("America/New_York"))
        today = now_et.strftime("%Y-%m-%d")
        hm = now_et.hour * 100 + now_et.minute
        if not (_WINDOW[0] <= hm <= _WINDOW[1]) or _done_date == today:
            return 0
        if not _observe_enabled():
            return 0

        from app.playbook.db import PlaybookDB
        rows = PlaybookDB().get_instructions_by_date(today)
        insts = [r for r in rows
                 if r.get("direction") in ("BUY", "SELL_SHORT") and not r.get("skip_reason")]
        if not insts:
            return 0

        router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
        futu = getattr(router, "futu", None) if router is not None else None
        if futu is None or not hasattr(futu, "get_snapshot"):
            return 0

        symbols = sorted({r["symbol"] for r in insts})
        snaps = futu.get_snapshot(symbols)
        if not snaps:
            return 0
        snap_by = {r["code"].replace("US.", "", 1): r for r in snaps if r.get("last_price", 0) > 0}

        _done_date = today
        written = 0
        flagged = 0
        _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _OUTPUT_PATH.open("a", encoding="utf-8") as f:
            for r in insts:
                sym = r["symbol"].replace("US.US.", "US.").replace("US.", "", 1)
                s = snap_by.get(sym) or snap_by.get(r["symbol"])
                if not s or not s.get("prev_close"):
                    continue
                chg = (s["last_price"] - s["prev_close"]) / s["prev_close"] * 100.0
                hits = evaluate_rules(r["direction"], chg)
                if hits:
                    flagged += 1
                rec = {
                    "ts": now_et.isoformat(timespec="seconds"),
                    "date": today, "symbol": r["symbol"], "direction": r["direction"],
                    "inst_id": r.get("id"), "decision_id": r.get("decision_id"),
                    "premarket_price": s["last_price"], "prev_close": s["prev_close"],
                    "chg_pct": round(chg, 3), "anchor": r.get("entry_price"),
                    "would_invalidate": hits, "schema": "v1",
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                written += 1
        logger.info(f"[盘前失效] 评估落盘 {written} 行 (命中规则 {flagged} 条) → {_OUTPUT_PATH.name}")
        return written
    except Exception as e:
        logger.debug(f"[盘前失效] 评估异常 (已隔离): {e}")
        return 0
