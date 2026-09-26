#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0-3 借券费率时间序列采集 (shadow 记录, 不影响交易)
==================================================
对每日活跃池标的查询空头池费率, 落盘 data/shadow/borrow_rate_series.jsonl:
  {"date": "...", "ts_et": "...", "symbol": "US.XXX",
   "pool": 池数(股), "fee_rate": 年化费率(%), "shortable": bool}

用途: 成本校准 (卖空轨费用) + 拥挤度早期信号 (费率飙升/池枯竭 = 做空拥挤)。
cron: 5 21 * * 1-5 (开盘后 35min, 池数据已稳定)。
"""
import sys
import json
import logging
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.utils.timezone import now_et

OUT_PATH = BASE_DIR / "data" / "shadow" / "borrow_rate_series.jsonl"
LOG_PATH = BASE_DIR / "logs" / "borrow_rate_snapshot.log"


def setup_logger():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lg = logging.getLogger("borrow_rate_snapshot")
    lg.setLevel(logging.INFO)
    if not lg.handlers:
        fh = logging.FileHandler(LOG_PATH)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(fh)
    return lg


def main() -> int:
    logger = setup_logger()
    today_et = now_et().strftime("%Y-%m-%d")
    # 源头拦: 非交易日不采集不落盘。cron `5 21 * * 1-5` 只按星期排, **不含美股假日** ⇒
    # 2026-09-07 (Labor Day, 休市) 落了 36 行, 其中 35 行费率与 9/4 完全相同 (陈旧沿用)。
    # 纯 append 模式 ⇒ 陈旧行永久留在时序里, 被 load_borrow_rates() 当"最近一条"返回。
    from app.utils.holiday_calendar import is_trading_day_str
    if not is_trading_day_str("US", today_et):
        logger.info("借券费率采集: %s 非美股交易日 (假日/周末), 跳过 — 零落盘", today_et)
        return 0
    plan_path = BASE_DIR / "data" / "daily_active_pool.json"
    if not plan_path.exists():
        logger.warning("daily_active_pool.json 不存在, 跳过")
        return 0
    try:
        plan = json.loads(plan_path.read_text())
    except Exception as e:
        logger.warning(f"读取 active pool 失败: {e}")
        return 1

    codes = sorted({
        str(t.get("code", "")).strip()
        for t in plan.get("entries", []) or []
        if isinstance(t, dict)
        and str(t.get("code", "")).strip().upper().startswith("US.")
        and len(str(t.get("code", "")).strip()) <= 9
    })
    if not codes:
        logger.info("无活跃标的, 跳过")
        return 0

    logger.info(f"借券费率采集启动: {len(codes)} 只标的")

    from app.execution.dual_broker import get_dual_broker
    resp = get_dual_broker().get_short_pool(codes)
    if not resp:
        logger.warning("get_short_pool 返回空 (接口异常或池数据缺失)")
        return 1

    now = now_et()
    today = now.strftime("%Y-%m-%d")
    now_iso = now.isoformat()
    n_ok = 0
    with open(OUT_PATH, "a") as f:
        for code in codes:
            entry = resp.get(code)
            if not isinstance(entry, dict):
                continue
            f.write(json.dumps({
                "date": today, "ts_et": now_iso, "symbol": code,
                "pool": entry.get("pool"),
                "fee_rate": entry.get("fee_rate"),
                "shortable": entry.get("shortable"),
            }, ensure_ascii=False, default=str) + "\n")
            n_ok += 1
    logger.info(f"✅ 落盘 {OUT_PATH.name}: {n_ok} 只标的 (date={today})")
    return 0


if __name__ == "__main__":
    rc = main()
    # 2026-07-30: 采集链加载 futu 行情上下文, SDK 非 daemon 线程会导致解释器
    #   退出挂在 threading._shutdown (07-28/07-29 实证)。落盘已完成, flush 后硬退出。
    import os
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
