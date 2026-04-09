#!/usr/bin/env python3
"""
模拟盘数据库初始化脚本

功能:
- 创建模拟盘专用表 (simulation_trades, simulation_positions, simulation_account, simulation_logs)
- 初始化模拟账户 (初始资金 HK$ 1,000,000)
- 检查表是否已存在 (避免重复创建)
- 提供回滚/重置功能

执行方式:
    python scripts/init_simulation_db.py
    python scripts/init_simulation_db.py --reset  # 重置数据库
    python scripts/init_simulation_db.py --rollback  # 回滚 (删除所有表)
"""
import argparse
import sqlite3
import sys
import logging
from datetime import datetime
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import config

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("init_simulation_db")

# 数据库路径
DB_PATH = config.DATA_DIR / "simulation.db"


def get_connection():
    """获取数据库连接"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def check_table_exists(conn, table_name):
    """检查表是否存在"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name=?
    """, (table_name,))
    return cursor.fetchone() is not None


def create_tables(conn):
    """创建模拟盘专用表"""
    cursor = conn.cursor()
    tables_created = []
    
    # 1. 模拟交易记录表
    if not check_table_exists(conn, 'simulation_trades'):
        cursor.execute("""
            CREATE TABLE simulation_trades (
                id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                name TEXT,
                action TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity INTEGER NOT NULL,
                pnl REAL,
                pnl_percent REAL,
                fees REAL DEFAULT 0,
                strategy TEXT,
                signal_id TEXT,
                entry_time DATETIME NOT NULL,
                exit_time DATETIME,
                exit_reason TEXT,
                order_id TEXT,
                status TEXT DEFAULT 'open',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 创建索引
        cursor.execute("CREATE INDEX idx_sim_trades_code ON simulation_trades(code)")
        cursor.execute("CREATE INDEX idx_sim_trades_status ON simulation_trades(status)")
        cursor.execute("CREATE INDEX idx_sim_trades_entry_time ON simulation_trades(entry_time)")
        cursor.execute("CREATE INDEX idx_sim_trades_signal ON simulation_trades(signal_id)")
        
        tables_created.append('simulation_trades')
        logger.info("✅ 创建表：simulation_trades")
    else:
        logger.info("⚠️  表已存在：simulation_trades")
    
    # 2. 模拟持仓表
    if not check_table_exists(conn, 'simulation_positions'):
        cursor.execute("""
            CREATE TABLE simulation_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                quantity INTEGER NOT NULL,
                avg_cost REAL NOT NULL,
                current_price REAL DEFAULT 0,
                direction TEXT DEFAULT 'long',
                entry_time DATETIME NOT NULL,
                unrealized_pnl REAL DEFAULT 0,
                unrealized_pnl_percent REAL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("CREATE INDEX idx_sim_positions_code ON simulation_positions(code)")
        
        tables_created.append('simulation_positions')
        logger.info("✅ 创建表：simulation_positions")
    else:
        logger.info("⚠️  表已存在：simulation_positions")
    
    # 3. 模拟账户资金表
    if not check_table_exists(conn, 'simulation_account'):
        cursor.execute("""
            CREATE TABLE simulation_account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT DEFAULT 'SIMULATION_MAIN',
                initial_capital REAL NOT NULL,
                current_capital REAL NOT NULL,
                available_cash REAL NOT NULL,
                frozen_cash REAL DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                total_pnl_percent REAL DEFAULT 0,
                total_fees REAL DEFAULT 0,
                trading_day TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("CREATE INDEX idx_sim_account_trading_day ON simulation_account(trading_day)")
        
        tables_created.append('simulation_account')
        logger.info("✅ 创建表：simulation_account")
    else:
        logger.info("⚠️  表已存在：simulation_account")
    
    # 4. 模拟交易日志表
    if not check_table_exists(conn, 'simulation_logs'):
        cursor.execute("""
            CREATE TABLE simulation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                log_type TEXT NOT NULL,
                code TEXT,
                action TEXT,
                price REAL,
                quantity INTEGER,
                order_id TEXT,
                signal_id TEXT,
                message TEXT,
                details TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("CREATE INDEX idx_sim_logs_type ON simulation_logs(log_type)")
        cursor.execute("CREATE INDEX idx_sim_logs_code ON simulation_logs(code)")
        cursor.execute("CREATE INDEX idx_sim_logs_created_at ON simulation_logs(created_at)")
        
        tables_created.append('simulation_logs')
        logger.info("✅ 创建表：simulation_logs")
    else:
        logger.info("⚠️  表已存在：simulation_logs")
    
    conn.commit()
    return tables_created


def initialize_account(conn, initial_capital=1000000.0):
    """初始化模拟账户"""
    cursor = conn.cursor()
    
    # 检查是否已有账户记录
    cursor.execute("""
        SELECT * FROM simulation_account WHERE account_id = 'SIMULATION_MAIN'
    """)
    existing = cursor.fetchone()
    
    if existing:
        logger.info("⚠️  模拟账户已存在，跳过初始化")
        return False
    
    # 插入初始账户记录
    cursor.execute("""
        INSERT INTO simulation_account 
        (account_id, initial_capital, current_capital, available_cash,
         total_pnl, total_pnl_percent, total_fees, trading_day)
        VALUES ('SIMULATION_MAIN', ?, ?, ?, 0, 0, 0, date('now'))
    """, (initial_capital, initial_capital, initial_capital))
    
    conn.commit()
    logger.info(f"✅ 初始化模拟账户：初始资金 HK$ {initial_capital:,.2f}")
    return True


def reset_database(conn):
    """重置数据库 (删除所有数据，保留表结构)"""
    cursor = conn.cursor()
    
    tables = ['simulation_logs', 'simulation_positions', 'simulation_trades', 'simulation_account']
    
    for table in tables:
        cursor.execute(f"DELETE FROM {table}")
        logger.info(f"🗑️  清空表：{table}")
    
    conn.commit()
    
    # 重新初始化账户
    initialize_account(conn)
    logger.info("✅ 数据库重置完成")


def rollback_database(conn):
    """回滚数据库 (删除所有表)"""
    cursor = conn.cursor()
    
    tables = ['simulation_logs', 'simulation_positions', 'simulation_trades', 'simulation_account']
    
    for table in tables:
        if check_table_exists(conn, table):
            cursor.execute(f"DROP TABLE {table}")
            logger.info(f"🗑️  删除表：{table}")
    
    conn.commit()
    logger.info("✅ 数据库回滚完成 (所有表已删除)")


def show_status(conn):
    """显示数据库状态"""
    cursor = conn.cursor()
    
    print("\n" + "=" * 60)
    print("📊 模拟盘数据库状态")
    print("=" * 60)
    
    # 表信息
    print("\n📋 表结构:")
    tables = ['simulation_trades', 'simulation_positions', 'simulation_account', 'simulation_logs']
    for table in tables:
        exists = check_table_exists(conn, table)
        status = "✅ 存在" if exists else "❌ 不存在"
        print(f"  {status} - {table}")
    
    # 账户信息
    print("\n💰 账户信息:")
    cursor.execute("SELECT * FROM simulation_account WHERE account_id = 'SIMULATION_MAIN'")
    account = cursor.fetchone()
    
    if account:
        account_dict = dict(account)
        print(f"  初始资金：HK$ {account_dict['initial_capital']:,.2f}")
        print(f"  当前资金：HK$ {account_dict['current_capital']:,.2f}")
        print(f"  可用资金：HK$ {account_dict['available_cash']:,.2f}")
        print(f"  总盈亏：HK$ {account_dict['total_pnl']:,.2f} ({account_dict['total_pnl_percent']:.2%})")
    else:
        print("  ⚠️  账户未初始化")
    
    # 交易统计
    print("\n📈 交易统计:")
    cursor.execute("SELECT COUNT(*) as count, SUM(pnl) as total_pnl FROM simulation_trades WHERE status = 'closed'")
    stats = cursor.fetchone()
    if stats:
        print(f"  已平仓交易：{stats['count']} 笔")
        if stats['total_pnl'] is not None:
            print(f"  累计盈亏：HK$ {stats['total_pnl']:,.2f}")
        else:
            print(f"  累计盈亏：HK$ 0.00")
    
    # 持仓统计
    print("\n💼 持仓统计:")
    cursor.execute("SELECT COUNT(*) as count FROM simulation_positions WHERE quantity > 0")
    pos_count = cursor.fetchone()['count']
    print(f"  当前持仓：{pos_count} 只")
    
    print("\n" + "=" * 60)


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='模拟盘数据库初始化工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python init_simulation_db.py              # 初始化数据库
  python init_simulation_db.py --reset      # 重置数据库 (清空数据)
  python init_simulation_db.py --rollback   # 回滚数据库 (删除表)
  python init_simulation_db.py --status     # 查看数据库状态
  python init_simulation_db.py --capital 500000  # 自定义初始资金
        """
    )
    
    parser.add_argument('--reset', action='store_true', 
                        help='重置数据库 (清空所有数据，保留表结构)')
    parser.add_argument('--rollback', action='store_true',
                        help='回滚数据库 (删除所有表)')
    parser.add_argument('--status', action='store_true',
                        help='显示数据库状态')
    parser.add_argument('--capital', type=float, default=1000000.0,
                        help='初始资金 (默认：1000000)')
    parser.add_argument('--force', action='store_true',
                        help='强制执行，不提示确认')
    
    args = parser.parse_args()
    
    # 确保数据目录存在
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    logger.info(f"数据库路径：{DB_PATH}")
    
    conn = get_connection()
    
    try:
        if args.rollback:
            if not args.force:
                confirm = input("⚠️  警告：此操作将删除所有表和数据，不可恢复！确认继续？(y/N): ")
                if confirm.lower() != 'y':
                    logger.info("操作已取消")
                    return
            rollback_database(conn)
        
        elif args.reset:
            if not args.force:
                confirm = input("⚠️  警告：此操作将清空所有数据！确认继续？(y/N): ")
                if confirm.lower() != 'y':
                    logger.info("操作已取消")
                    return
            reset_database(conn)
        
        elif args.status:
            show_status(conn)
        
        else:
            # 默认：初始化
            logger.info("开始初始化模拟盘数据库...")
            
            tables = create_tables(conn)
            
            if tables:
                logger.info(f"✅ 成功创建 {len(tables)} 个表")
            else:
                logger.info("ℹ️  所有表已存在")
            
            initialize_account(conn, args.capital)
            
            logger.info("✅ 初始化完成！")
            show_status(conn)
    
    except Exception as e:
        logger.error(f"❌ 操作失败：{e}", exc_info=True)
        sys.exit(1)
    
    finally:
        conn.close()


if __name__ == '__main__':
    main()
