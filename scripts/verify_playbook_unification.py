#!/usr/bin/env python3
"""
验证脚本：确认 PLAYBOOK_SHADOW 已废弃，PLAYBOOK_SIM 真实下单路径可用。

运行环境：周六（非交易日），市场关闭。
策略：用最小 mock scheduler 调用 playbook pipeline 函数，观察日志与 DB 状态变化。
"""
import os
import sys
import logging

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

# 加载环境变量
from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, '.env.us'))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("verify_playbook")


def run_verification():
    # 最小化 scheduler mock：只提供 pipeline 可能用到的属性
    class MockScheduler:
        def __init__(self):
            self.watchlist = []
            self.position_store = None
            self.voting_coordinator = None
            self.trade_store = None
            self.data_router = None

    scheduler = MockScheduler()

    from app.playbook.pipeline import (
        run_playbook_generate,
        monitor_playbook_sim_exits,
        run_playbook_track,
    )
    from app.playbook.db import PlaybookDB

    db = PlaybookDB()
    db.ensure_tables()

    today = "2026-06-26"
    playbook_db_path = "data/ftseek_us.db"
    logger.info("=" * 60)
    logger.info("[Step 1] run_playbook_generate(force=True)")
    try:
        res_gen = run_playbook_generate(scheduler, force=True)
        logger.info(f"run_playbook_generate result: {res_gen}")
    except Exception as e:
        logger.exception("run_playbook_generate failed")

    logger.info("=" * 60)
    logger.info("[Step 2] monitor_playbook_sim_exits")
    try:
        n_exits = monitor_playbook_sim_exits(scheduler)
        logger.info(f"monitor_playbook_sim_exits returned: {n_exits}")
    except Exception as e:
        logger.exception("monitor_playbook_sim_exits failed")

    logger.info("=" * 60)
    logger.info("[Step 3] run_playbook_track(force=True)")
    try:
        res_track = run_playbook_track(scheduler, force=True)
        logger.info(f"run_playbook_track result: {res_track}")
    except Exception as e:
        logger.exception("run_playbook_track failed")

    # DB 状态检查
    logger.info("=" * 60)
    logger.info("[Step 4] DB 状态检查")
    import sqlite3
    from app.core.paths import get_trade_records_db

    conn = sqlite3.connect(str(get_trade_records_db()))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 未平仓 PLAYBOOK_SIM/LIVE 数量
    rows = c.execute("""
        SELECT source_type, COUNT(*) as cnt
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
          AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
        GROUP BY source_type
    """).fetchall()
    logger.info("open PLAYBOOK_SIM/LIVE positions: %s", [(r["source_type"], r["cnt"]) for r in rows])

    # 列出这 5 条
    rows = c.execute("""
        SELECT id, symbol, direction, entry_price, quantity, entry_time, source_type
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
          AND (exit_price = 0 OR exit_price IS NULL OR exit_time = '' OR exit_time IS NULL)
        ORDER BY symbol
    """).fetchall()
    for r in rows:
        logger.info("  open: id=%s %s %s qty=%s entry=%s source=%s", r["id"], r["symbol"], r["direction"], r["quantity"], r["entry_price"], r["source_type"])

    # 今日指令数
    c2 = sqlite3.connect(playbook_db_path)
    c2.row_factory = sqlite3.Row
    inst_rows = c2.execute(
        "SELECT COUNT(*) as cnt FROM playbook_instructions WHERE date=?", (today,)
    ).fetchone()
    logger.info("today playbook_instructions count: %s", inst_rows["cnt"])

    # 当前 open_positions 中 source='broker_sync' 的数量
    op_rows = c2.execute("SELECT source, COUNT(*) as cnt FROM open_positions GROUP BY source").fetchall()
    logger.info("open_positions by source: %s", [(r["source"], r["cnt"]) for r in op_rows])

    conn.close()
    c2.close()
    logger.info("=" * 60)
    logger.info("验证完成")


if __name__ == "__main__":
    run_verification()
