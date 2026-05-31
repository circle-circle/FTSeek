"""
Signal Lifecycle Updater — 保持 signal_lifecycle 表与 trade_records 同步

职责:
  1. 新交易写入 trade_records 后, 自动同步到 signal_lifecycle
  2. 关联 signal_snapshots (IC 数据) + decisions (投票数据)
  3. 由 cron 或盘后审计触发

用法:
  python scripts/update_signal_lifecycle.py          # 增量更新
  python scripts/update_signal_lifecycle.py --full   # 全量重建
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("update_lifecycle")

DB_PATH = PROJECT_ROOT / "data" / "ftseek_us.db"
SNAP_DB = PROJECT_ROOT / "data" / "signal_snapshots.db"
DEC_DB = PROJECT_ROOT / "data" / "decisions.db"


def ensure_lifecycle_table(conn: sqlite3.Connection):
    """确保 signal_lifecycle 表存在"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_lifecycle (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id        INTEGER,
            decision_id     TEXT,
            trace_id        TEXT,
            trade_date      TEXT,
            symbol          TEXT,
            signal_strength INTEGER,
            signal_confidence REAL,
            signal_direction TEXT,
            fwd_ret_t1      REAL,
            fwd_ret_t5      REAL,
            fwd_ret_t20     REAL,
            decision_strength INTEGER,
            consensus_level REAL,
            vix             REAL,
            trade_direction TEXT,
            entry_price     REAL,
            exit_price      REAL,
            realized_pnl    REAL,
            pnl_pct         REAL,
            exit_reason     TEXT,
            holding_hours   REAL,
            owner_strategy  TEXT,
            source_type     TEXT,
            trade_mode      TEXT,
            direction_aligned INTEGER,
            trade_win       INTEGER,
            created_at      TEXT NOT NULL,
            updated_at      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lc_trade_id ON signal_lifecycle(trade_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lc_symbol ON signal_lifecycle(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lc_date ON signal_lifecycle(trade_date)")
    conn.commit()


def full_rebuild():
    """全量重建 signal_lifecycle"""
    logger.info("全量重建 signal_lifecycle...")
    
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("ATTACH DATABASE '%s' AS snap" % str(SNAP_DB))
    conn.execute("ATTACH DATABASE '%s' AS dec_db" % str(DEC_DB))
    
    ensure_lifecycle_table(conn)
    conn.execute("DELETE FROM signal_lifecycle")
    
    trades = conn.execute("""
        SELECT id, decision_id, trace_id, trade_date, symbol,
               direction, entry_price, exit_price, realized_pnl, pnl_pct,
               exit_reason, holding_hours, owner_strategy, source_type,
               trade_mode, signal_strength, signal_confidence
        FROM trade_records ORDER BY id
    """).fetchall()
    
    inserted = 0
    for t in trades:
        (tid, dec_id, trace_id, tdate, symbol,
         tdir, entry_p, exit_p, pnl, pnl_pct,
         exit_reason, hold_hrs, strat, src, mode,
         trade_str, trade_conf) = t
        
        # Signal snapshot data
        snap_str = None
        snap_conf = None
        snap_dir = None
        fwd_t1 = fwd_t5 = fwd_t20 = None
        
        if dec_id:
            row = conn.execute("""
                SELECT strength, confidence, direction, fwd_ret_t1, fwd_ret_t5, fwd_ret_t20
                FROM snap.signal_snapshots WHERE decision_id = ?
            """, (dec_id,)).fetchone()
            if row:
                snap_str, snap_conf, snap_dir, fwd_t1, fwd_t5, fwd_t20 = row
        
        # Decision data
        dec_str = consensus = vix_val = None
        if dec_id:
            row = conn.execute("""
                SELECT final_strength, consensus_level, vix
                FROM dec_db.decisions WHERE decision_id = ?
            """, (dec_id,)).fetchone()
            if row:
                dec_str, consensus, vix_val = row
        
        # Best available strength
        best_str = snap_str or trade_str or dec_str
        best_conf = snap_conf or trade_conf or consensus
        
        direction_aligned = 1 if (snap_dir and snap_dir == tdir) else 0
        trade_win = 1 if (pnl_pct and pnl_pct > 0) else (0 if (pnl_pct and pnl_pct < 0) else None)
        
        conn.execute("""
            INSERT INTO signal_lifecycle (
                trade_id, decision_id, trace_id, trade_date, symbol,
                signal_strength, signal_confidence, signal_direction,
                fwd_ret_t1, fwd_ret_t5, fwd_ret_t20,
                decision_strength, consensus_level, vix,
                trade_direction, entry_price, exit_price,
                realized_pnl, pnl_pct, exit_reason, holding_hours,
                owner_strategy, source_type, trade_mode,
                direction_aligned, trade_win, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            tid, dec_id, trace_id, tdate, symbol,
            best_str, best_conf, snap_dir,
            fwd_t1, fwd_t5, fwd_t20,
            dec_str, consensus, vix_val,
            tdir, entry_p, exit_p,
            pnl, pnl_pct, exit_reason, hold_hrs,
            strat, src, mode,
            direction_aligned, trade_win,
            datetime.now().isoformat()
        ))
        inserted += 1
    
    conn.commit()
    logger.info("全量重建完成: %d 行" % inserted)
    
    # Summary
    total = conn.execute("SELECT COUNT(*) FROM signal_lifecycle").fetchone()[0]
    with_signal = conn.execute("SELECT COUNT(*) FROM signal_lifecycle WHERE signal_strength IS NOT NULL AND signal_strength > 0").fetchone()[0]
    with_dec = conn.execute("SELECT COUNT(*) FROM signal_lifecycle WHERE decision_strength IS NOT NULL").fetchone()[0]
    with_fwd = conn.execute("SELECT COUNT(*) FROM signal_lifecycle WHERE fwd_ret_t1 IS NOT NULL").fetchone()[0]
    aligned = conn.execute("SELECT COUNT(*) FROM signal_lifecycle WHERE direction_aligned = 1").fetchone()[0]
    
    print("\n=== signal_lifecycle Summary ===")
    print("Total: %d" % total)
    print("With signal_strength: %d (%.0f%%)" % (with_signal, with_signal/total*100 if total else 0))
    print("With decision data: %d" % with_dec)
    print("With forward returns: %d" % with_fwd)
    print("Direction aligned: %d" % aligned)
    
    conn.execute("DETACH DATABASE snap")
    conn.execute("DETACH DATABASE dec_db")
    conn.close()


def incremental_update():
    """增量更新: 只处理新交易"""
    conn = sqlite3.connect(str(DB_PATH))
    ensure_lifecycle_table(conn)
    
    # Find trade_ids not yet in lifecycle
    conn.execute("ATTACH DATABASE '%s' AS snap" % str(SNAP_DB))
    conn.execute("ATTACH DATABASE '%s' AS dec_db" % str(DEC_DB))
    
    new_trades = conn.execute("""
        SELECT t.id FROM trade_records t
        WHERE t.id NOT IN (SELECT trade_id FROM signal_lifecycle WHERE trade_id IS NOT NULL)
    """).fetchall()
    
    if not new_trades:
        logger.info("无新交易需要更新")
        conn.execute("DETACH DATABASE snap")
        conn.execute("DETACH DATABASE dec_db")
        conn.close()
        return
    
    logger.info("增量更新: %d 条新交易" % len(new_trades))
    
    # Process each new trade (reuse full_rebuild logic for each)
    for (tid,) in new_trades:
        t = conn.execute("""
            SELECT id, decision_id, trace_id, trade_date, symbol,
                   direction, entry_price, exit_price, realized_pnl, pnl_pct,
                   exit_reason, holding_hours, owner_strategy, source_type,
                   trade_mode, signal_strength, signal_confidence
            FROM trade_records WHERE id = ?
        """, (tid,)).fetchone()
        
        if not t:
            continue
            
        (tid, dec_id, trace_id, tdate, symbol,
         tdir, entry_p, exit_p, pnl, pnl_pct,
         exit_reason, hold_hrs, strat, src, mode,
         trade_str, trade_conf) = t
        
        snap_str = snap_conf = snap_dir = None
        fwd_t1 = fwd_t5 = fwd_t20 = None
        
        if dec_id:
            row = conn.execute("""
                SELECT strength, confidence, direction, fwd_ret_t1, fwd_ret_t5, fwd_ret_t20
                FROM snap.signal_snapshots WHERE decision_id = ?
            """, (dec_id,)).fetchone()
            if row:
                snap_str, snap_conf, snap_dir, fwd_t1, fwd_t5, fwd_t20 = row
        
        dec_str = consensus = vix_val = None
        if dec_id:
            row = conn.execute("""
                SELECT final_strength, consensus_level, vix
                FROM dec_db.decisions WHERE decision_id = ?
            """, (dec_id,)).fetchone()
            if row:
                dec_str, consensus, vix_val = row
        
        best_str = snap_str or trade_str or dec_str
        best_conf = snap_conf or trade_conf or consensus
        direction_aligned = 1 if (snap_dir and snap_dir == tdir) else 0
        trade_win = 1 if (pnl_pct and pnl_pct > 0) else (0 if (pnl_pct and pnl_pct < 0) else None)
        
        conn.execute("""
            INSERT INTO signal_lifecycle (
                trade_id, decision_id, trace_id, trade_date, symbol,
                signal_strength, signal_confidence, signal_direction,
                fwd_ret_t1, fwd_ret_t5, fwd_ret_t20,
                decision_strength, consensus_level, vix,
                trade_direction, entry_price, exit_price,
                realized_pnl, pnl_pct, exit_reason, holding_hours,
                owner_strategy, source_type, trade_mode,
                direction_aligned, trade_win, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            tid, dec_id, trace_id, tdate, symbol,
            best_str, best_conf, snap_dir,
            fwd_t1, fwd_t5, fwd_t20,
            dec_str, consensus, vix_val,
            tdir, entry_p, exit_p,
            pnl, pnl_pct, exit_reason, hold_hrs,
            strat, src, mode,
            direction_aligned, trade_win,
            datetime.now().isoformat()
        ))
    
    conn.commit()
    logger.info("增量更新完成: %d 条" % len(new_trades))
    
    conn.execute("DETACH DATABASE snap")
    conn.execute("DETACH DATABASE dec_db")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Signal Lifecycle Updater")
    parser.add_argument("--full", action="store_true", help="全量重建")
    args = parser.parse_args()
    
    if args.full:
        full_rebuild()
    else:
        incremental_update()
