# P0 修复：添加 record_trade_to_db 方法到 TradeExecutor 类
# 此文件包含需要添加到 executor.py 的方法

from app.storage.database import ConnectionPool  # P0 修复：连接池

# P0 修复 (2026-04-12): 全局连接池
_record_trade_db_pool: ConnectionPool = None


def _get_record_trade_db_pool(db_path: str) -> ConnectionPool:
    """获取记录交易数据库连接池单例"""
    global _record_trade_db_pool
    if _record_trade_db_pool is None:
        _record_trade_db_pool = ConnectionPool(db_path, pool_size=5, timeout=30.0)
    return _record_trade_db_pool


def record_trade_to_db(self, order_id: str, signal, price: float = None, pnl: float = 0):
    """
    记录成交到数据库 (P0 修复)

    Args:
        order_id: 订单 ID
        signal: 交易信号
        price: 成交价 (可选，默认使用 signal.price)
        pnl: 盈亏 (平仓时使用，开仓时为 0)
    """
    from datetime import datetime
    from app import config

    DB_PATH = str(config.DATA_DIR / "ftseek.db")

    conn = None
    try:
        # P0 修复 (2026-04-12): 使用连接池替代直接 sqlite3.connect
        pool = _get_record_trade_db_pool(DB_PATH)
        conn = pool.get_connection(timeout=30.0)
        cursor = conn.cursor()
        
        # 生成唯一 ID
        trade_id = f"trade_{order_id}"
        
        # 检查是否已存在
        cursor.execute("SELECT id FROM trades WHERE id = ?", (trade_id,))
        if cursor.fetchone():
            logger.info(f"⏭️  成交已存在：{trade_id}")
            conn.close()
            return
        
        # 准备数据
        code = signal.code
        action = 'BUY' if str(getattr(signal, 'action', 'BUY')).upper() in ['BUY', 'LONG'] else 'SELL'
        # P1 修复 (2026-05-12): 平仓信号 quantity=0，使用 1 作为 placeholder（实际数量由 executor.py 的 record_trade_to_db 正确记录）
        quantity = getattr(signal, 'quantity', 0)
        if quantity <= 0:
            quantity = 1  # placeholder，实际平仓数量由 position.quantity 决定
        entry_price = price if price else getattr(signal, 'price', 0)
        strategy = getattr(signal, 'strategy', 'manual')
        entry_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 插入数据库
        cursor.execute("""
            INSERT INTO trades (
                id, signal_id, code, name, action,
                entry_price, exit_price, quantity,
                pnl, pnl_percent, fees,
                strategy, entry_time, exit_time,
                exit_reason, order_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            f"signal_{trade_id}",
            code,
            '',  # name
            action,
            entry_price,
            None,  # exit_price
            quantity,
            pnl,
            0,  # pnl_percent
            0,  # fees
            strategy,
            entry_time,
            None,  # exit_time
            '',  # exit_reason
            order_id,
            'OPEN' if action == 'BUY' else 'CLOSED'
        ))
        
        conn.commit()
        pool.return_connection(conn)  # P0 修复：归还连接

        logger.info(f"✅ [数据库] 成交记录成功：{code} {action} {quantity}股 @ {entry_price}")

    except Exception as e:
        logger.error(f"❌ [数据库] 记录成交失败：{e}", exc_info=True)
        # P0 修复：异常时也要归还连接
        if conn:
            try:
                pool.return_connection(conn)
            except Exception:
                pass
        raise
