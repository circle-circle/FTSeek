"""盘前尾段影子行情采集器 (仅观测, 零决策变更).

DEB-20260805(二) C1 终裁 (2026-08-05 用户裁"执行"):
09:00-09:30 ET 尾窗每 60s 对当日可执行指令标的采一次 Futu 快照,
落 data/premarket_shadow.jsonl, 供"盘前尾段价 vs 开盘价"缺口锚定测量
(claim 3a, n≈50 一周攒样)。不触碰任何交易/入场链路;
回滚 = config playbook.premarket_shadow.observe → false。

C2 前置实测 (scripts/probe_futu_caps_20260805.py, 2026-08-05):
request_history_kline(extended_time=True) 可用 (08-05 盘前 29 根 1M K 线),
盘前快照链路走 get_snapshot (历史连接路径, 不占交易槽位)。
"""
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_OUTPUT_PATH = Path("data/premarket_shadow.jsonl")
_COLLECT_INTERVAL_SEC = 60
_WINDOW_START_HM = 900   # 09:00 ET
_WINDOW_END_HM = 930     # 09:30 ET
_THREAD_ARM_HM = 855     # 08:55 ET 起允许起线程
_last_run_ts = 0.0
_thread_started = False


def _observe_enabled() -> bool:
    try:
        from app.core.config_container import ConfigContainer
        return bool(ConfigContainer.get_instance().get_dotted("playbook.premarket_shadow.observe", False))
    except Exception:
        return False


def _load_today_instructions():
    """当日可执行指令 (direction BUY/SELL_SHORT 且未被 skip)."""
    try:
        from app.playbook.db import PlaybookDB
        import pytz
        today = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
        rows = PlaybookDB().get_instructions_by_date(today)
    except Exception as e:
        logger.debug(f"[盘前影子] 读取当日指令失败: {e}")
        return []
    out = []
    for r in rows:
        if r.get("direction") not in ("BUY", "SELL_SHORT"):
            continue
        if r.get("skip_reason"):
            continue
        if not (r.get("entry_price") or 0):
            continue
        out.append({
            "inst_id": r.get("id"),
            "symbol": r.get("symbol"),
            "direction": r.get("direction"),
            "entry_price": float(r.get("entry_price") or 0),
            "confidence": r.get("confidence"),
        })
    return out


def _collect_once(scheduler) -> int:
    """采集一轮快照并落盘 (调用方负责窗口/节流判断)."""
    global _last_run_ts
    import pytz
    now_et = datetime.now(pytz.timezone("America/New_York"))
    instructions = _load_today_instructions()
    if not instructions:
        _last_run_ts = time.time()
        return 0

    router = None
    if scheduler is not None:
        router = getattr(scheduler, "data_router", None) or getattr(scheduler, "market_data", None)
    futu = getattr(router, "futu", None) if router is not None else None
    if futu is None or not hasattr(futu, "get_snapshot"):
        return 0

    symbols = sorted({i["symbol"] for i in instructions})
    snap_rows = futu.get_snapshot(symbols)
    if not snap_rows:
        _last_run_ts = time.time()
        return 0
    snap_by_symbol = {
        r["code"].replace("US.", "", 1): r for r in snap_rows if r.get("last_price", 0) > 0
    }

    written = 0
    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _OUTPUT_PATH.open("a", encoding="utf-8") as f:
        for i in instructions:
            s = snap_by_symbol.get(i["symbol"])
            if not s:
                continue
            rec = {
                "ts_et": now_et.isoformat(timespec="seconds"),
                "date": now_et.strftime("%Y-%m-%d"),
                "session": "premarket_tail",
                **i,
                "last_price": s["last_price"],
                "prev_close": s["prev_close"],
                "bid": s["bid"],
                "ask": s["ask"],
                "volume": s["volume"],
                "turnover": s["turnover"],
                "quote_update_time": s["update_time"],
                "data_source": "futu_snapshot_premarket",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1

    _last_run_ts = time.time()
    if written:
        logger.info(f"[盘前影子] 尾窗快照落盘 {written} 行 → {_OUTPUT_PATH}")
    return written


def _thread_loop(scheduler) -> None:
    """窗口内独立 60s 节奏采集 (2026-08-05 首日教训: 调度器 PRE_MARKET 串行分支
    末尾的钩子会被 2h 节流 LLM 扫描阻塞过 09:30, 整天 0 行 → 采集器必须自带节奏)."""
    global _thread_started
    import pytz
    try:
        while True:
            now_et = datetime.now(pytz.timezone("America/New_York"))
            hm = now_et.hour * 100 + now_et.minute
            if hm >= _WINDOW_END_HM:
                break
            if hm >= _WINDOW_START_HM and (time.time() - _last_run_ts) >= _COLLECT_INTERVAL_SEC:
                try:
                    _collect_once(scheduler)
                except Exception as e:
                    logger.warning(f"[盘前影子] 采集轮异常 (下轮继续): {e}")
            time.sleep(30)
    finally:
        _thread_started = False


def run_collect(scheduler=None) -> int:
    """调度器钩子: 窗口附近 (08:55-09:30 ET) 起一次 daemon 采集线程, 幂等."""
    global _thread_started
    if not _observe_enabled() or _thread_started:
        return 0

    import pytz
    now_et = datetime.now(pytz.timezone("America/New_York"))
    hm = now_et.hour * 100 + now_et.minute
    if hm < _THREAD_ARM_HM or hm >= _WINDOW_END_HM:
        return 0

    _thread_started = True
    threading.Thread(target=_thread_loop, args=(scheduler,), daemon=True,
                     name="premarket-shadow").start()
    logger.info("[盘前影子] 尾窗采集线程已启动 (09:00-09:30 ET, 60s 节奏)")
    return 0
