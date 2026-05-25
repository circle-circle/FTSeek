"""
持仓管理器 (Position Manager)

负责跟踪模拟交易持仓状态，支持开仓、平仓、盈亏计算。
支持金字塔加仓逻辑。
"""

import sqlite3
import logging
import threading
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
import os
import yaml

from app.storage.database import ConnectionPool  # P0 修复：连接池

logger = logging.getLogger("ftseek.position_manager")
logger.propagate = False  # 防止日志重复


# P0 修复 (2026-04-12): 全局连接池单例
_position_db_pool: Optional[ConnectionPool] = None


def _get_position_db_pool(db_path: str) -> ConnectionPool:
    """获取持仓数据库连接池单例"""
    global _position_db_pool
    if _position_db_pool is None or _position_db_pool.db_path != db_path:
        _position_db_pool = ConnectionPool(db_path, pool_size=5, timeout=30.0)
    return _position_db_pool


@dataclass
class Position:
    """持仓数据模型 (2026-03-17 新增止盈止损字段，2026-04-15 新增归因追踪)"""
    id: int
    code: str
    action: str  # BUY/SELL
    quantity: int
    entry_price: float
    entry_time: str
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    status: str = 'OPEN'
    exit_price: Optional[float] = None
    realized_pnl: Optional[float] = None
    stop_loss: Optional[float] = None  # 止损价
    take_profit: Optional[float] = None  # 止盈价
    highest_price: Optional[float] = None  # 最高价 (用于移动止盈)
    lowest_price: Optional[float] = None  # 最低价 (用于做空移动止盈)
    trace_id: str = ""           # 全链路归因 ID (2026-04-15 Phase 1)
    # Phase 1: 退出状态机
    exit_state: Optional[str] = None
    state_updated_at: Optional[str] = None
    # 信号归因 (2026-05-22 新增)
    signal_id: Optional[str] = None  # 关联的信号 ID


class PositionManager:
    """
    持仓管理器 (单例模式)
    
    管理交易持仓的创建、查询、更新和平仓操作。
    使用 SQLite 数据库持久化存储持仓信息。
    支持金字塔加仓逻辑。
    
    P0 (2026-04-16): 改为单例模式，避免多实例重复加载配置和连接。
    """
    
    _instance: Optional['PositionManager'] = None
    _init_lock = threading.Lock()

    @classmethod
    def get_instance(cls, db_path: str = None, config_path: str = None) -> 'PositionManager':
        """获取全局单例"""
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = cls(db_path=db_path, config_path=config_path)
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（仅测试用）"""
        with cls._init_lock:
            cls._instance = None
    
    def __init__(self, db_path: str = None, config_path: str = None):
        """
        初始化持仓管理器
        
        Args:
            db_path: 数据库文件路径，默认使用 /opt/ftseek/FTSeek/data/ftseek.db
            config_path: 配置文件路径，回退读取路径
        """
        self.db_path = db_path or '/opt/ftseek/FTSeek/data/ftseek.db'
        self.config_path = config_path or '/opt/ftseek/FTSeek/config/unified_config.yaml'
        # P0 2026-05-21: 回调注入 — 替代循环依赖的 lazy import
        self._on_position_opened_callback = None
        self._ensure_table()
        self._config = self._load_config()
    
    def register_on_position_opened(self, callback):
        """
        注册开仓回调 — 消除 position_manager → data_subscriber 循环依赖
        
        Args:
            callback: 函数签名 callback(code: str, source: str) -> None
        """
        self._on_position_opened_callback = callback
        logger.info("[PositionManager] 开仓回调已注册")
    
    def _load_config(self) -> Dict[str, Any]:
        """
        加载配置（优先从配置中心，回退到默认值）
        
        P0 (2026-04-16): 修复 config_center 读取路径适配，
        支持从 source_aware_risk / trading 等实际存在的配置块读取。

        Returns:
            配置字典
        """
        # 优先从配置中心读取
        try:
            from config.config_center import get_config_center
            config_center = get_config_center()
            trading_cfg = config_center.get("trading", {})
            risk_cfg = config_center.get("risk_limits", {})
            
            # 路径 1: 显式 position 块 (trading.position)
            position_cfg = trading_cfg.get("position", {})
            # 路径 2: risk_limits 中的分层配置
            source_aware = risk_cfg.get("source_aware_risk", {})
            time_aware = risk_cfg.get("time_aware_risk", {})
            has_risk_limits = bool(risk_cfg and (source_aware or time_aware or risk_cfg.get("max_single_position")))
            
            if position_cfg or has_risk_limits:
                logger.info("持仓配置已从配置中心加载")
                
                # 合并配置
                merged = {
                    "position": position_cfg or {
                        "initial_ratio": trading_cfg.get("default_position_size", 0.50),
                        "max_position_ratio": risk_cfg.get("max_single_position", 0.20),
                        "total_max_ratio": risk_cfg.get("max_total_position", 0.80),
                    },
                    "pyramid": trading_cfg.get("pyramid", {"enabled": True}),
                    "exit": trading_cfg.get("exit", {}),
                    "cooldown": trading_cfg.get("cooldown", {}),
                    "hk_market": trading_cfg.get("hk_market", {"enabled": True}),
                    "short_strategy": trading_cfg.get("short_strategy", {"enabled": True}),
                }
                # 注入 source_aware_risk 供分层风控使用
                if source_aware:
                    merged["source_aware_risk"] = source_aware
                if time_aware:
                    merged["time_aware_risk"] = time_aware
                
                return merged
        except Exception as e:
            logger.warning(f"从配置中心读取持仓配置失败: {e}")

        # 回退到默认配置
        logger.debug("使用默认持仓配置")
        default_config = {
            'position': {
                'initial_ratio': 0.50,
                'max_position_ratio': 0.20,
                'total_max_ratio': 0.80
            },
            'pyramid': {
                'enabled': True,
                'level1': {
                    'price_increase': 0.05,
                    'ratio': 0.30,
                    'min_confidence': 0.70
                },
                'level2': {
                    'price_increase': 0.10,
                    'ratio': 0.20,
                    'min_confidence': 0.70
                }
            },
            'cooldown': {
                'hours': 4,
                'max_trades_per_day': 3,
                'min_confidence': 0.60,
                'add_position_confidence': 0.70
            }
        }
        
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    # 合并配置，确保所有键都存在
                    return self._merge_config(default_config, config)
            except Exception as e:
                logger.warning(f"[仓位配置] 加载配置文件失败，使用默认配置: {e}")
        
        return default_config
    
    def _merge_config(self, default: Dict, loaded: Dict) -> Dict:
        """
        合并配置字典
        
        Args:
            default: 默认配置
            loaded: 加载的配置
            
        Returns:
            合并后的配置
        """
        result = default.copy()
        for key, value in loaded.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result
    
    def _get_connection(self) -> sqlite3.Connection:
        """P0 修复 (2026-04-12): 从连接池获取连接"""
        pool = _get_position_db_pool(self.db_path)
        return pool.get_connection(timeout=30.0)

    def _return_connection(self, conn: sqlite3.Connection):
        """P0 修复 (2026-04-12): 归还连接到连接池"""
        pool = _get_position_db_pool(self.db_path)
        pool.return_connection(conn)

    def _ensure_table(self):
        """确保 positions 表存在 (2026-03-17 新增止盈止损字段)"""
        conn = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    current_price REAL,
                    unrealized_pnl REAL,
                    status TEXT DEFAULT 'OPEN',
                    exit_price REAL,
                    realized_pnl REAL,
                    exit_time TEXT,
                    stop_loss REAL,
                    take_profit REAL,
                    highest_price REAL,
                    lowest_price REAL
                )
            ''')
            conn.commit()

            # 确保旧数据库添加新字段
            self._ensure_exit_columns(conn)
            self._ensure_state_machine_columns(conn)
        finally:
            if conn:
                self._return_connection(conn)
    
    def _ensure_state_machine_columns(self, conn):
        """确保 positions 表有退出状态机字段 (2026-05-10 P0 修复)
        
        exit_state 和 state_updated_at 是 DynamicExitManager 10 层退出状态机的核心字段。
        缺失时整个状态机 (BE_READY→BE_ACTIVE→TRAIL→PARTIAL→BREAKEVEN→...) 完全失效。
        """
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        columns = [row[1] for row in cursor.fetchall()]
        
        def _add_column_safe(col_name, col_type, default=None):
            if col_name in columns:
                return
            try:
                if default is not None:
                    cursor.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_type} DEFAULT {default}")
                else:
                    cursor.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_type}")
                logger.info(f"[Schema迁移] 添加 {col_name} 字段到 positions 表")
            except Exception as e:
                if 'duplicate column name' not in str(e).lower():
                    raise
        
        _add_column_safe('exit_state', 'TEXT', "'INIT'")
        _add_column_safe('state_updated_at', 'TEXT')
        conn.commit()
    
    def _ensure_exit_columns(self, conn):
        """
        确保 positions 表有止盈止损字段 (2026-03-17 新增)

        Args:
            conn: 数据库连接
        """
        cursor = conn.cursor()

        # 检查字段是否存在
        cursor.execute("PRAGMA table_info(positions)")
        columns = [row[1] for row in cursor.fetchall()]

        # P0 修复 (2026-04-13): 连接池中的连接可能在表创建前建立，
        # PRAGMA 可能返回空结果。使用 try/except 处理 ALTER TABLE 的重复列错误。
        def _add_column_safe(col_name, col_type):
            if col_name in columns:
                return
            try:
                cursor.execute(f'ALTER TABLE positions ADD COLUMN {col_name} {col_type}')
            except Exception as e:
                if 'duplicate column name' not in str(e).lower():
                    raise
                logger.debug(f"字段 {col_name} 已存在，跳过添加")

        _add_column_safe('stop_loss', 'REAL')
        _add_column_safe('take_profit', 'REAL')
        _add_column_safe('highest_price', 'REAL')
        _add_column_safe('lowest_price', 'REAL')

        conn.commit()
    
    def get_position(self, code: str) -> Optional[Position]:
        """
        获取某股票的当前持仓 (status=OPEN)
        
        Args:
            code: 股票代码
            
        Returns:
            Position 对象，如果不存在则返回 None
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM positions 
                WHERE code = ? AND status = 'OPEN'
                LIMIT 1
            ''', (code,))
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            return self._row_to_position(row)
        finally:
            self._return_connection(conn)

    def get_position_by_id(self, position_id: int) -> Optional[Position]:
        """按 position_id 精确查询持仓（用于保护校验等按 ID 回读场景）

        Args:
            position_id: 持仓记录 ID

        Returns:
            Position 对象，如果不存在则返回 None
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM positions WHERE id = ? LIMIT 1',
                (position_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_position(row)
        finally:
            self._return_connection(conn)

    def get_all_positions(self) -> List[Position]:
        """
        获取所有持仓
        
        Returns:
            所有持仓的列表
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM positions 
                WHERE status = 'OPEN'
            ''')
            rows = cursor.fetchall()
            
            return [self._row_to_position(row) for row in rows]
        finally:
            self._return_connection(conn)
    
    def get_open_positions_count(self) -> int:
        """
        获取当前持仓数量 (2026-03-25 新增)
        2026-05-08 修复: 本地 DB 优先，避免 live_query Futu API 永久阻塞
        
        Returns:
            当前 OPEN 状态的持仓数量
        """
        # 本地 DB 优先（零阻塞风险，SQLite + 连接池 timeout=30s）
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM positions 
                WHERE status = 'OPEN'
            ''')
            count = cursor.fetchone()[0]
        finally:
            self._return_connection(conn)
        
        # live_query 作为增强校验（不阻塞主路径）
        try:
            from app.factors.live_position_query import get_live_position_query
            live_count = get_live_position_query().count_positions(market='HK')
            if live_count != count:
                logger.warning(f"[持仓对账] 本地DB={count} vs 富途={live_count}，以DB为准")
        except Exception:
            pass  # 富途查询失败不影响主路径
        
        return count
    
    def create_position(self, code: str, action: str, quantity: int,
                       entry_price: float, entry_time: str,
                       stop_loss: float = None, take_profit: float = None,
                       signal_id: str = None, strategy: str = None,
                       trace_id: str = "", trader_type: str = None,
                       strategy_tags: str = None,
                       force: bool = False) -> int:
        """
        创建持仓记录 (支持止盈止损 + 信号关联 + 全链路归因 - 2026-04-15 更新)

        Args:
            code: 股票代码
            action: 买卖方向 (BUY/SELL)
            quantity: 数量
            entry_price: 开仓价格
            entry_time: 开仓时间
            stop_loss: 止损价 (可选)
            take_profit: 止盈价 (可选)
            signal_id: 关联的信号ID (可选)
            strategy: 策略名称 (可选)
            trace_id: 全链路追踪 ID (2026-04-15 新增)
            trader_type: 交易类型 (AI=自动执行, SYNC=同步创建, MANUAL=手动)
            force: 跳过防呆检查 (2026-05-22 新增, 仅用于 position_sync 幽灵仓修复)
                   调用方已确认 Futu 持有该持仓，必须强制创建本地记录

        Returns:
            新建持仓的 ID (force=False 时可能返回已有持仓 ID)
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # 检查表是否有止盈止损字段，没有则添加
            self._ensure_exit_columns(conn)
            
            # 检查表是否有 signal_id 和 strategy 字段，没有则添加
            self._ensure_signal_columns(conn)

            # 检查表是否有 trace_id 字段，没有则添加
            self._ensure_trace_column(conn)

            # 检查表是否有 strategy_tags 字段 (P2 2026-05-17: 门禁组合对比标签)
            self._ensure_strategy_tags_column(conn)
            
            # P0 防呆检查 (2026-05-06): 防止重复创建
            # force=True 时跳过 (调用方已确认 Futu 持有该持仓，必须强制创建)
            if not force:
                # 检查是否已有同名标的的OPEN持仓
                cursor.execute("SELECT id, quantity, entry_price FROM positions WHERE code = ? AND status = 'OPEN' LIMIT 1", (code,))
                existing = cursor.fetchone()
                if existing:
                    logger.warning(
                        f"[防呆] {code} 已存在 OPEN 持仓 (ID={existing[0]}, qty={existing[1]}), "
                        f"拒绝重复创建。如需加仓请使用 update_position_fields"
                    )
                    return existing[0]  # 返回已有持仓ID，避免调用方报错

                # P1 修复 (2026-05-16): 检查最近 5 分钟内刚关闭的同价持仓
                # 根因: exit 关闭持仓 → reconcile 重新创建 → 新信号又来 → 多条记录
                # P0 修复 (2026-05-22): 时区 bug — datetime('now') 返回 UTC，exit_time 存的是本地时间
                #   'T' > ' ' 导致字符串比较永远为 True → 所有同价 CLOSED 都被误判为"5min 内"
                #   改用 Python 计算本地时间阈值，并统一替换 'T' 分隔符
                from datetime import datetime as _dt, timedelta as _td
                _local_5min_ago = (_dt.now() - _td(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
                cursor.execute("""
                    SELECT id FROM positions WHERE code = ? AND status = 'CLOSED'
                    AND abs(entry_price - ?) / ? < 0.005
                    AND replace(exit_time, 'T', ' ') >= ?
                    ORDER BY id DESC LIMIT 1
                """, (code, entry_price, entry_price, _local_5min_ago))
                recent_close = cursor.fetchone()
                if recent_close:
                    logger.warning(
                        f"[防呆] {code} 5 分钟内刚关闭同价持仓 (ID={recent_close[0]}), "
                        f"拒绝重复创建 (可能是 reconcile 残留)"
                    )
                    return recent_close[0]
            else:
                logger.info(f"[create_position] force=True 跳过防呆检查 ({code})")

            
            # Phase 4a (2026-05-21): 止损一致性 — 开仓时强制设置 stop_loss
            # 根因: 历史 68 条 stop_loss=0/NULL 导致止损形同虚设
            if stop_loss is None or stop_loss <= 0:
                stop_loss = self.calculate_stop_loss_price(entry_price)
                logger.info(
                    f"[止损自动设置] {code} entry={entry_price:.3f} → stop_loss={stop_loss:.3f} (-3.5%)"
                )
            if take_profit is None or take_profit <= 0:
                take_profit = self.calculate_take_profit_price(entry_price)
                logger.info(
                    f"[止盈自动设置] {code} entry={entry_price:.3f} → take_profit={take_profit:.3f} (+5%)"
                )

            # P0 2026-05-22: INSERT OR IGNORE 防止 UNIQUE constraint 冲突
            # 根因: 多线程竞态 (position_sync + reconcile 同时运行) 导致同 code OPEN 记录冲突
            # 修复: INSERT OR IGNORE + 检查 rowcount，未插入则查询已有 OPEN 记录的 ID 并更新
            cursor.execute('''
                INSERT OR IGNORE INTO positions 
                (code, action, quantity, entry_price, entry_time, status, stop_loss, take_profit, signal_id, strategy, trace_id, trader_type, strategy_tags)
                VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)
            ''', (code, action, quantity, entry_price, entry_time, stop_loss, take_profit, signal_id, strategy, trace_id, trader_type or 'MANUAL', strategy_tags))
            
            if cursor.rowcount == 0:
                # 已有 OPEN 记录 (UNIQUE constraint 触发)，查询并更新
                cursor.execute(
                    "SELECT id FROM positions WHERE code=? AND status='OPEN' LIMIT 1",
                    (code,)
                )
                existing = cursor.fetchone()
                if existing:
                    position_id = existing[0]
                    cursor.execute(
                        "UPDATE positions SET quantity=?, entry_price=?, stop_loss=?, take_profit=? WHERE id=?",
                        (quantity, entry_price, stop_loss, take_profit, position_id)
                    )
                    logger.warning(
                        f"[create_position] {code} 已有 OPEN 记录 (id={position_id})，"
                        f"更新数量/价格而非新建 (UNIQUE constraint 保护)"
                    )
                else:
                    logger.error(f"[create_position] {code} INSERT OR IGNORE 未插入且无 OPEN 记录，异常状态！")
                    position_id = None
            else:
                position_id = cursor.lastrowid
            
            conn.commit()

            # 自动订阅动态池 (新成交股票加入实时监控)
            try:
                from app.utils.daily_active_pool import get_daily_active_pool, SOURCE_POSITION
                pool = get_daily_active_pool()
                pool.add_position(code, metadata={"action": action, "quantity": quantity})
                # P0 2026-05-21: 回调注入 — 替代循环依赖的 lazy import
                # 通知 DataSubscriber 订阅此标的
                if self._on_position_opened_callback is not None:
                    try:
                        self._on_position_opened_callback(code, SOURCE_POSITION)
                    except Exception as e:
                        logger.warning(f"[PositionManager] 开仓回调执行失败: {e}")
            except Exception:
                pass  # 不影响主流程

            return position_id
        finally:
            self._return_connection(conn)
    
    def _ensure_signal_columns(self, conn):
        """确保 signal_id 和 strategy 字段存在"""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        columns = [row[1] for row in cursor.fetchall()]

        # P0 修复 (2026-04-13): 安全添加字段，处理连接池 PRAGMA 返回空的情况
        def _add_column_safe(col_name, col_type):
            if col_name in columns:
                return
            try:
                cursor.execute(f'ALTER TABLE positions ADD COLUMN {col_name} {col_type}')
            except Exception as e:
                if 'duplicate column name' not in str(e).lower():
                    raise

        _add_column_safe('signal_id', 'TEXT')
        _add_column_safe('strategy', 'TEXT')
    
    def _ensure_trace_column(self, conn):
        """确保 trace_id 字段存在 (2026-04-15 Phase 1 新增)"""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'trace_id' not in columns:
            try:
                cursor.execute('ALTER TABLE positions ADD COLUMN trace_id TEXT DEFAULT ""')
                conn.commit()
            except Exception as e:
                if 'duplicate column name' not in str(e).lower():
                    raise

    def _ensure_strategy_tags_column(self, conn):
        """确保 strategy_tags 字段存在 (P2 2026-05-17: 门禁组合对比标签)"""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'strategy_tags' not in columns:
            try:
                cursor.execute('ALTER TABLE positions ADD COLUMN strategy_tags TEXT DEFAULT NULL')
                conn.commit()
            except Exception as e:
                if 'duplicate column name' not in str(e).lower():
                    raise
    
    def update_price(self, code: str, price: float):
        """
        更新当前价和浮动盈亏
        
        Args:
            code: 股票代码
            price: 当前价格
        """
        position = self.get_position(code)
        if position is None:
            return
        
        # 计算浮动盈亏
        unrealized_pnl = self.calculate_pnl(
            entry_price=position.entry_price,
            exit_price=price,
            quantity=position.quantity,
            action=position.action
        )
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            # 用 id 精确定位，避免批量更新同品种所有 OPEN 仓
            cursor.execute('''
                UPDATE positions 
                SET current_price = ?, unrealized_pnl = ?
                WHERE id = ?
            ''', (price, unrealized_pnl, position.id))
            conn.commit()
        finally:
            self._return_connection(conn)
    
    def close_position(self, code: str, exit_price: float, exit_time: str) -> float:
        """
        平仓并返回已实现盈亏（已扣除交易费用）

        注意：只关闭 get_position() 取到的第一条 OPEN 记录（按 id 定位），
        避免批量关闭导致同品种多仓叠加问题。

        Args:
            code: 股票代码
            exit_price: 平仓价格
            exit_time: 平仓时间

        Returns:
            已实现盈亏 (正=盈利，负=亏损)
        """
        position = self.get_position(code)
        if position is None:
            return 0.0

        # 计算毛盈亏
        gross_pnl = self.calculate_pnl(
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=position.quantity,
            action=position.action
        )

        # 2026-04-08: 扣除交易费用，计算净盈亏（与富途 App 保持一致）
        try:
            from app.trader.cost_calculator import get_cost_calculator
            calculator = get_cost_calculator()

            # 判断港股/美股
            is_hk_stock = code.startswith("HK.")

            if is_hk_stock:
                # 港股：计算往返费用
                cost_info = calculator.calculate_round_trip_cost(
                    position.entry_price,
                    exit_price,
                    position.quantity
                )
                total_fees = cost_info['total_cost']
            else:
                # 美股：简化费用估算（佣金 + 监管费 + 清算费）
                trade_value = abs(gross_pnl) + position.entry_price * position.quantity
                total_fees = max(1.0, trade_value * 0.001)  # 约 0.1% 费用

            # 净盈亏 = 毛盈亏 - 交易费用
            # 注意：费用总是正数，从盈亏中扣除
            if position.action == 'BUY':
                # 做多：费用减少盈利或增加亏损
                net_pnl = gross_pnl - total_fees
            else:
                # 做空：费用同样减少盈利或增加亏损
                net_pnl = gross_pnl - total_fees

            logger.info(
                f"[平仓盈亏] {code} 毛盈亏={gross_pnl:.2f}, 费用={total_fees:.2f}, "
                f"净盈亏={net_pnl:.2f}"
            )
        except Exception as e:
            logger.warning(f"[平仓费用] {code} 费用计算失败，使用毛盈亏：{e}")
            net_pnl = gross_pnl

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            # 用 id 精确定位，避免同品种多条 OPEN 记录时批量关闭
            cursor.execute('''
                UPDATE positions
                SET status = 'CLOSED',
                    exit_price = ?,
                    exit_time = ?,
                    realized_pnl = ?,
                    current_price = ?,
                    unrealized_pnl = ?
                WHERE id = ?
            ''', (exit_price, exit_time, net_pnl, exit_price, net_pnl, position.id))
            conn.commit()

            # P1 2026-04-27: 同步关闭 trades 表中对应的 OPEN 记录
            # P0 2026-05-21: 修复 AttributeError — Position 对象无 signal_id 属性
            try:
                _trade_updates = 0
                _sig_id = getattr(position, 'signal_id', None)
                if _sig_id:
                    # 优先用 signal_id 精确匹配
                    cursor.execute(
                        "UPDATE trades SET status='CLOSED', exit_price=?, exit_time=?, "
                        "pnl=?, pnl_percent=?, fees=?, exit_reason='position_closed' "
                        "WHERE signal_id=? AND status='OPEN'",
                        (exit_price, exit_time, net_pnl,
                         net_pnl / (position.entry_price * position.quantity) if position.entry_price > 0 and position.quantity > 0 else 0,
                         total_fees,
                         _sig_id)
                    )
                    _trade_updates = cursor.rowcount
                if _trade_updates == 0:
                    # fallback: 用 code 匹配最近的 OPEN trade
                    cursor.execute(
                        "UPDATE trades SET status='CLOSED', exit_price=?, exit_time=?, "
                        "pnl=?, pnl_percent=?, fees=?, exit_reason='position_closed' "
                        "WHERE code=? AND status='OPEN' "
                        "AND id = (SELECT id FROM trades WHERE code=? AND status='OPEN' ORDER BY id DESC LIMIT 1)",
                        (exit_price, exit_time, net_pnl,
                         net_pnl / (position.entry_price * position.quantity) if position.entry_price > 0 and position.quantity > 0 else 0,
                         total_fees,
                         code, code)
                    )
                    _trade_updates = cursor.rowcount
                if _trade_updates > 0:
                    logger.info(f"[平仓同步] {code} 已同步关闭 {_trade_updates} 条 trade 记录")
            except Exception as _trade_sync_e:
                logger.warning(f"[平仓同步] {code} trades 表同步失败: {_trade_sync_e}")
        finally:
            self._return_connection(conn)

        # 自动同步动态池: 检查是否还有剩余持仓
        try:
            remaining = self.get_position(code)
            if remaining is None:
                # 该股票已无持仓，从动态池移除
                try:
                    from app.utils.daily_active_pool import get_daily_active_pool
                    get_daily_active_pool().remove_position(code)
                except ImportError:
                    pass
        except Exception:
            pass  # 不影响主流程

        # P0 修复 (2026-05-11): 平仓后立即更新 SmartStop 状态
        # 旧代码只在 check() 时调用 _update_loss_state，但无信号时 check() 不被调用
        # 导致连续亏损时 SmartStop 状态永远不更新 → 无法触发熔断保护
        try:
            from app.risk.circuit_breaker import SmartStopGuard
            guard = SmartStopGuard(db_path=self._db_path if hasattr(self, '_db_path') else None)
            guard._update_loss_state()
            if net_pnl < 0:
                logger.info(f"[SmartStop·联动] {code} 亏损 {net_pnl:.2f}，已更新连败状态")
            else:
                logger.info(f"[SmartStop·联动] {code} 盈利 {net_pnl:.2f}，已重置连败状态")
        except Exception as _ss_err:
            logger.debug(f"[SmartStop·联动] 状态更新失败: {_ss_err}")

        return net_pnl
    
    def partial_close_position(self, code: str, close_qty: int, exit_price: float, exit_time: str) -> float:
        """
        部分平仓并返回已实现盈亏（已扣除交易费用）

        Args:
            code: 股票代码
            close_qty: 平仓数量
            exit_price: 平仓价格
            exit_time: 平仓时间

        Returns:
            已实现盈亏 (正=盈利，负=亏损)
        """
        position = self.get_position(code)
        if position is None:
            return 0.0

        if close_qty >= position.quantity:
            # 如果平仓数量 >= 持仓数量，则全部平仓
            return self.close_position(code, exit_price, exit_time)

        # 计算毛盈亏 (按平仓数量计算)
        gross_pnl = self.calculate_pnl(
            entry_price=position.entry_price,
            exit_price=exit_price,
            quantity=close_qty,
            action=position.action
        )

        # 2026-04-08: 扣除交易费用，计算净盈亏（与 close_position 保持一致）
        try:
            from app.trader.cost_calculator import get_cost_calculator
            calculator = get_cost_calculator()

            # 判断港股/美股
            is_hk_stock = code.startswith("HK.")

            if is_hk_stock:
                # 港股：按平仓比例计算费用
                # 往返费用基于平仓部分的买卖金额
                cost_info = calculator.calculate_round_trip_cost(
                    position.entry_price,
                    exit_price,
                    close_qty
                )
                total_fees = cost_info['total_cost']
            else:
                # 美股：简化费用估算
                trade_value = abs(gross_pnl) + position.entry_price * close_qty
                total_fees = max(1.0, trade_value * 0.001)  # 约 0.1% 费用

            # 净盈亏 = 毛盈亏 - 交易费用
            net_pnl = gross_pnl - total_fees

            logger.info(
                f"[部分平仓盈亏] {code} 平仓{close_qty}股 毛盈亏={gross_pnl:.2f}, "
                f"费用={total_fees:.2f}, 净盈亏={net_pnl:.2f}"
            )
        except Exception as e:
            logger.warning(f"[部分平仓费用] {code} 费用计算失败，使用毛盈亏：{e}")
            net_pnl = gross_pnl

        # 更新持仓：减少数量，保持 OPEN 状态
        new_quantity = position.quantity - close_qty

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            # 用 id 精确定位，避免同品种多条 OPEN 记录时批量更新
            cursor.execute('''
                UPDATE positions
                SET quantity = ?,
                    current_price = ?,
                    unrealized_pnl = ?,
                    exit_price = ?,
                    exit_time = ?,
                    realized_pnl = COALESCE(realized_pnl, 0) + ?
                WHERE id = ?
            ''', (new_quantity, exit_price, net_pnl, exit_price, exit_time, net_pnl, position.id))
            conn.commit()
        finally:
            self._return_connection(conn)

        return net_pnl
    
    def calculate_pnl(self, entry_price: float, exit_price: float,
                      quantity: int, action: str) -> float:
        """
        计算盈亏
        
        Args:
            entry_price: 开仓价格
            exit_price: 平仓价格 (或当前价)
            quantity: 数量
            action: 买卖方向 (BUY/SELL)
            
        Returns:
            盈亏金额 (正=盈利，负=亏损)
        """
        if action == 'BUY':
            # 买入：价格上涨盈利
            pnl = (exit_price - entry_price) * quantity
        else:
            # 卖出 (做空)：价格下跌盈利
            pnl = (entry_price - exit_price) * quantity
        
        return round(pnl, 2)
    
    def check_stop_loss(self, code: str, current_price: float) -> bool:
        """
        [防御性校验] 检查止损价是否异常并修复。

        注意：平仓决策已由 DynamicExitManager.check_exit_priority 全权处理。
        此方法仅用于防御性校验（修复异常止损价），不再触发平仓。

        Args:
            code: 股票代码
            current_price: 当前价格

        Returns:
            True 如果触发止损（仅供日志/预警），否则 False
        """
        position = self.get_position(code)
        if position is None:
            return False

        # 获取方向 (BUY/SELL) — 必须在分支前定义，避免 fallback 分支 UnboundLocalError
        direction = position.action

        # 使用持仓记录中的止损价（优先）
        if position.stop_loss and position.stop_loss > 0:
            
            # 2026-04-24 修复: 防御性校验 — 止损价必须低于入场价至少 0.5%
            # 防止 stop_loss=entry_price 导致开仓即止损
            if position.stop_loss >= position.entry_price * 0.995:
                # P0 修复 (2026-04-27): 计算合理的止损价并更新 DB，防止循环警告
                
                try:
                    from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                    stop_pct = abs(_dem.current_params.get('stop_loss', 0.025))  # P1-SSOT: fallback对齐YAML
                except Exception:
                    stop_pct = 0.025  # P1-SSOT
                
                avg_cost = position.entry_price
                # 做多: Entry * (1 - pct), 做空: Entry * (1 + pct)
                if direction == 'BUY':
                    dynamic_sl = avg_cost * (1 - stop_pct)
                else:
                    dynamic_sl = avg_cost * (1 + stop_pct)

                logger.warning(
                    f"[止损异常] {code} stop_loss={position.stop_loss} >= "
                    f"entry_price*0.995={position.entry_price*0.995:.4f}，"
                    f"使用动态止损兜底并更新 DB -> {dynamic_sl:.4f}"
                )
                
                # 立即更新 DB，防止下一次 tick 再次进入此分支
                self.update_stop_loss_take_profit(code, dynamic_sl, position.take_profit)
                if direction == 'BUY':
                    return current_price <= dynamic_sl
                else:  # SELL
                    return current_price >= dynamic_sl
            else:
                # 止损价已设置且合理：只有当前价跌破止损价才触发
                if direction == 'BUY':
                    return current_price <= position.stop_loss
                else:  # SELL: 价格上涨触发止损
                    return current_price >= position.stop_loss

        # 未设置止损价时，用动态配置的百分比
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            stop_pct = abs(_dem.current_params.get('stop_loss', 0.025))  # P1-SSOT
        except Exception:
            stop_pct = 0.025  # P1-SSOT: 默认止损 2.5%
        avg_cost = position.entry_price
        if avg_cost <= 0:
            return False
        pnl_rate = (current_price - avg_cost) / avg_cost
        if direction == 'BUY':
            return pnl_rate <= -stop_pct
        else:  # SELL: 价格上涨亏损
            return pnl_rate >= stop_pct

    def check_take_profit(self, code: str, current_price: float) -> bool:
        """
        [防御性校验] 检查止盈价是否异常并修复。

        注意：平仓决策已由 DynamicExitManager.check_exit_priority 全权处理。
        此方法仅用于防御性校验，不再触发平仓。

        Args:
            code: 股票代码
            current_price: 当前价格

        Returns:
            True 如果触发止盈（仅供日志/预警），否则 False
        """
        position = self.get_position(code)
        if position is None:
            return False

        direction = position.action

        # 使用持仓记录中的止盈价（优先）
        if position.take_profit and position.take_profit > 0:
            if direction == 'BUY':
                return current_price >= position.take_profit
            else:  # SELL: 价格下跌触发止盈
                return current_price <= position.take_profit

        # 未设置止盈价时，用动态配置的百分比
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            take_pct = abs(_dem.current_params.get('take_profit', 0.08))  # P1-SSOT: fallback 对齐 YAML (0.08)
        except Exception:
            take_pct = 0.08  # 默认止盈 8% (对齐 YAML default)
        avg_cost = position.entry_price
        if avg_cost <= 0:
            return False
        pnl_rate = (current_price - avg_cost) / avg_cost
        if direction == 'BUY':
            return pnl_rate >= take_pct
        else:  # SELL: 价格下跌盈利
            return pnl_rate <= -take_pct
    
    def check_breakeven_stop(self, code: str, current_price: float) -> tuple:
        """
        [DEPRECATED] 保本止损检查已废弃 — DynamicExitManager.check_breakeven_stop 为唯一决策源。
        保留此接口仅用于向后兼容，始终返回 (False, None)。

        Args:
            code: 股票代码 (unused)
            current_price: 当前价格 (unused)

        Returns:
            (False, None)
        """
        logger.debug(f"[DEPRECATED] check_breakeven_stop({code}) called, always returns (False, None)")
        return False, None
    
    def check_partial_take_profit(self, code: str, current_price: float) -> float:
        """
        [DEPRECATED] 分批止盈检查已废弃 — DynamicExitManager.check_exit_priority 为唯一决策源。
        保留此接口仅用于向后兼容，始终返回 0.0。

        Args:
            code: 股票代码 (unused)
            current_price: 当前价格 (unused)

        Returns:
            0.0 (不再使用)
        """
        logger.debug(f"[DEPRECATED] check_partial_take_profit({code}) called, always returns 0.0")
        return 0.0
    
    def update_breakeven_stop(self, code: str, breakeven_price: float = None):
        """
        更新止损到保本价（保本止损）
        
        Args:
            code: 股票代码
            breakeven_price: 保本价（默认成本价 * 1.01 覆盖费用）
        """
        position = self.get_position(code)
        if position is None:
            return
        
        if breakeven_price is None:
            breakeven_price = position.entry_price  # fallback
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            # 用 id 精确定位，避免批量更新同品种所有 OPEN 仓
            cursor.execute('''
                UPDATE positions 
                SET stop_loss = ?
                WHERE id = ?
            ''', (breakeven_price, position.id))
            conn.commit()
            logger.info(f"[保本止损] {code} 止损已上移到保本价 {breakeven_price:.2f}")
        finally:
            self._return_connection(conn)

    def update_exit_state(self, code: str, new_state: str, reason: str = ""):
        """更新持仓退出状态机状态 (INIT → BE_ACTIVE → TRAILING → SCALE_OUT)

        Args:
            code: 股票代码
            new_state: 新状态值 (来自 ExitReason 或自定义状态)
            reason: 状态变更原因
        """
        position = self.get_position(code)
        if position is None:
            return

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE positions
                SET exit_state = ?, state_updated_at = ?
                WHERE id = ?
            ''', (new_state, __import__('datetime').datetime.now().isoformat(), position.id))
            conn.commit()
        finally:
            self._return_connection(conn)

    def update_peak_price(self, code: str, highest_price: float = None, lowest_price: float = None):
        """更新持仓期间最高价/最低价 (P0 修复 2026-05-10)
        
        trailing_stop 和 breakeven_stop 依赖 peak_price 判断回撤。如果 DB 中
        highest_price 为 None，服务重启后峰值数据丢失，追踪止盈永远不会触发。
        
        Args:
            code: 股票代码
            highest_price: 新高价 (做多时更新)
            lowest_price: 新低价 (做空时更新)
        """
        position = self.get_position(code)
        if position is None:
            return
        
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            if highest_price is not None:
                cursor.execute(
                    'UPDATE positions SET highest_price = ? WHERE id = ?',
                    (highest_price, position.id)
                )
            if lowest_price is not None:
                cursor.execute(
                    'UPDATE positions SET lowest_price = ? WHERE id = ?',
                    (lowest_price, position.id)
                )
            conn.commit()
        finally:
            self._return_connection(conn)

    def update_stop_loss_take_profit(self, code: str, stop_loss: float, take_profit: float):
        """动态更新持仓的止损价和止盈价（开仓后立即调用，写入 DB）

        Args:
            code:        股票代码
            stop_loss:   新止损价（绝对价格，做多时低于成本，做空时高于成本）
            take_profit: 新止盈价（绝对价格）
        """
        position = self.get_position(code)
        if position is None:
            logger.debug(f"[动态止损] {code} 无持仓，跳过止损更新")
            return

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE positions
                SET stop_loss = ?, take_profit = ?
                WHERE id = ?
            ''', (stop_loss, take_profit, position.id))
            conn.commit()
            logger.info(
                f"[动态止损写入] {code} 止损={stop_loss:.4f} 止盈={take_profit:.4f}"
            )
        finally:
            self._return_connection(conn)

    def update_position_fields(self, position_id: int, quantity: int = None, entry_price: float = None):
        """Surgical update of position fields by ID (用于对账同步)

        Args:
            position_id: 持仓 ID
            quantity: 新数量 (可选)
            entry_price: 新成本价 (可选)
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            updates = []
            params = []
            if quantity is not None:
                updates.append('quantity = ?')
                params.append(quantity)
            if entry_price is not None:
                updates.append('entry_price = ?')
                params.append(entry_price)
            if updates:
                params.append(position_id)
                cursor.execute(
                    'UPDATE positions SET ' + ', '.join(updates) + ' WHERE id = ?',
                    params
                )
                conn.commit()
        finally:
            self._return_connection(conn)
    
    def calculate_stop_loss_price(self, avg_cost: float) -> float:
        """
        计算止损价格
        已从硬编码 5% 改为对齐 DynamicExitManager 默认参数 (-3.5%)。

        Args:
            avg_cost: 平均成本价

        Returns:
            止损价格 (成本价的 96.5%)
        """
        return round(avg_cost * (1 - 0.035), 2)

    def calculate_take_profit_price(self, avg_cost: float) -> float:
        """
        计算止盈价格
        已从硬编码 15% 改为对齐 DynamicExitManager 默认参数 (+5%)。

        Args:
            avg_cost: 平均成本价

        Returns:
            止盈价格 (成本价的 105%)
        """
        return round(avg_cost * (1 + 0.05), 2)
    
    def _row_to_position(self, row: sqlite3.Row) -> Position:
        """将数据库行转换为 Position 对象 (2026-03-17 新增止盈止损字段)
        
        DB 列顺序（PRAGMA table_info positions）:
          0=id, 1=code, 2=action, 3=quantity, 4=entry_price, 5=entry_time,
          6=current_price, 7=unrealized_pnl, 8=status, 9=exit_price,
          10=realized_pnl, 11=exit_time, 12=stop_loss, 13=take_profit,
          14=highest_price, 15=lowest_price, 16=signal_id, 17=strategy
        """
        # 优先用字典访问（更安全，不受列顺序影响）
        try:
            keys = row.keys()
        except Exception:
            keys = []

        def _get(key, idx, default=None):
            if keys and key in keys:
                val = row[key]
            elif len(row) > idx:
                val = row[idx]
            else:
                val = default
            # 数字字段：若值为字符串则返回 None（防止 exit_time 等文本字段错位）
            return val

        def _float(key, idx, default=None):
            v = _get(key, idx, default)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return Position(
            id=row[0],
            code=row[1],
            action=row[2],
            quantity=row[3],
            entry_price=row[4],
            entry_time=row[5],
            current_price=row[6],
            unrealized_pnl=row[7],
            status=row[8],
            exit_price=row[9],
            realized_pnl=row[10],
            # 正确的列索引：exit_time=11，stop_loss=12，take_profit=13
            stop_loss=_float('stop_loss', 12),
            take_profit=_float('take_profit', 13),
            highest_price=_float('highest_price', 14),
            lowest_price=_float('lowest_price', 15),
            exit_state=_get('exit_state', 27, 'INIT'),
            state_updated_at=_get('state_updated_at', 28),
            signal_id=_get('signal_id', 16),  # 2026-05-22: 添加 signal_id 映射
        )
    
    # ==================== 顺势加仓方法 ====================

    def _ensure_is_add_column(self, conn):
        """确保 positions 表有 is_add 字段（标记该仓位是加仓而非初始开仓）"""
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(positions)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'is_add' not in columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN is_add INTEGER DEFAULT 0")
            logger.info("已添加 is_add 字段到 positions 表")
        conn.commit()

    def add_to_position(self, code: str, add_qty: int, add_price: float,
                        add_time: str, signal_id: str = None) -> Optional[int]:
        """
        顺势加仓：更新现有 OPEN 持仓的数量和加权均价，并按新均价重算止盈止损。
        返回 position.id，失败返回 None。
        """
        position = self.get_position(code)
        if position is None:
            return None

        old_qty = position.quantity
        old_price = position.entry_price
        new_qty = old_qty + add_qty
        new_avg_price = round((old_price * old_qty + add_price * add_qty) / new_qty, 4)

        # 按新均价重算止盈止损（从动态配置读取，失败则用默认值）
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            stop_pct = abs(_dem.current_params.get('stop_loss', 0.025))  # P1-SSOT
            take_pct = abs(_dem.current_params.get('take_profit', 0.08))  # P1-SSOT
        except Exception:
            stop_pct, take_pct = 0.025, 0.08  # P1-SSOT

        if position.action == 'BUY':
            new_stop_loss = round(new_avg_price * (1 - stop_pct), 4)
            new_take_profit = round(new_avg_price * (1 + take_pct), 4)
        else:  # SELL（做空）
            new_stop_loss = round(new_avg_price * (1 + stop_pct), 4)
            new_take_profit = round(new_avg_price * (1 - take_pct), 4)

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE positions
                SET quantity = ?, entry_price = ?, stop_loss = ?, take_profit = ?
                WHERE id = ?
            ''', (new_qty, new_avg_price, new_stop_loss, new_take_profit, position.id))
            conn.commit()
            logger.info(
                f"[加仓] {code} qty {old_qty}→{new_qty} | "
                f"均价 {old_price:.4f}→{new_avg_price} | "
                f"止损={new_stop_loss} 止盈={new_take_profit}"
            )
            return position.id
        finally:
            self._return_connection(conn)

    def get_today_add_count(self, code: str) -> int:
        """
        查询今日对该品种的加仓次数（查 positions 表中 is_add=1 且今日开仓的记录）
        """
        conn = self._get_connection()
        try:
            self._ensure_is_add_column(conn)
            cursor = conn.cursor()
            today = datetime.now().strftime('%Y-%m-%d')
            cursor.execute('''
                SELECT COUNT(*) FROM positions
                WHERE code = ? AND is_add = 1 AND date(entry_time) = date(?)
            ''', (code, today))
            result = cursor.fetchone()
            return result[0] if result else 0
        finally:
            self._return_connection(conn)

    def record_add_position(self, code: str, add_qty: int, add_price: float,
                            add_time: str, signal_id: str = None) -> int:
        """
        在 positions 表中新建一条 is_add=1 的加仓记录（用于统计加仓次数）
        返回新建记录的 id
        """
        conn = self._get_connection()
        try:
            self._ensure_is_add_column(conn)
            self._ensure_signal_columns(conn)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO positions
                (code, action, quantity, entry_price, entry_time, status, is_add, signal_id, trader_type, strategy, strategy_tags)
                VALUES (?, (SELECT action FROM positions WHERE code=? AND status='OPEN' LIMIT 1),
                        ?, ?, ?, 'CLOSED', 1, ?, 'AI', 'pyramid', 'add')
            ''', (code, code, add_qty, add_price, add_time, signal_id))
            conn.commit()
            return cursor.lastrowid
        finally:
            self._return_connection(conn)

    # ==================== 金字塔加仓相关方法（旧，保留备用）====================

    def can_add_position(self, code: str, signal_confidence: float) -> bool:
        """
        检查是否可以加仓
        
        检查条件:
        1. 是否有持仓
        2. 仓位上限 (added < initial)
        3. 信号置信度
        4. 冷却时间
        5. 今日交易次数
        
        Args:
            code: 股票代码
            signal_confidence: 信号置信度 (0-1)
            
        Returns:
            bool: 是否可以加仓
        """
        # 1. 检查是否有持仓
        position = self.get_position(code)
        if position is None:
            return False
        
        # 2. 检查仓位上限 (需要查询历史交易记录计算已加仓数量)
        # 获取初始仓位和已加仓数量
        initial_quantity = self._get_initial_position_quantity(code)
        added_quantity = self._get_added_position_quantity(code)
        
        if added_quantity >= initial_quantity:
            return False
        
        # 3. 检查信号置信度
        min_confidence = self._config.get('cooldown', {}).get('add_position_confidence', 0.70)
        if signal_confidence < min_confidence:
            return False
        
        # 4. 检查冷却时间
        if self.is_in_cooling_period(code):
            return False
        
        # 5. 检查今日交易次数
        today_count = self.get_today_trade_count(code)
        max_trades = self._config.get('cooldown', {}).get('max_trades_per_day', 3)
        if today_count >= max_trades:
            return False
        
        return True
    
    def get_add_quantity(self, code: str, price_increase: float) -> int:
        """
        计算加仓数量 (金字塔)
        
        金字塔加仓规则:
        - 价格涨幅 >= 10%: 加仓 20%
        - 价格涨幅 >= 5%: 加仓 30%
        - 否则：不加仓
        
        Args:
            code: 股票代码
            price_increase: 价格涨幅 (小数，如 0.05 表示 5%)
            
        Returns:
            int: 加仓数量，无法加仓则返回 0
        """
        position = self.get_position(code)
        if position is None:
            return 0
        
        # 获取初始仓位
        initial_quantity = self._get_initial_position_quantity(code)
        if initial_quantity == 0:
            return 0
        
        pyramid_config = self._config.get('pyramid', {})
        
        # 检查金字塔加仓是否启用
        if not pyramid_config.get('enabled', True):
            return 0
        
        # 检查 level2 (涨幅 >= 10%)
        level2 = pyramid_config.get('level2', {})
        if price_increase >= level2.get('price_increase', 0.10):
            ratio = level2.get('ratio', 0.20)
            add_qty = int(initial_quantity * ratio)
            # 确保不超过剩余可加仓数量
            added_qty = self._get_added_position_quantity(code)
            max_add = initial_quantity - added_qty
            return min(add_qty, max_add)
        
        # 检查 level1 (涨幅 >= 5%)
        level1 = pyramid_config.get('level1', {})
        if price_increase >= level1.get('price_increase', 0.05):
            ratio = level1.get('ratio', 0.30)
            add_qty = int(initial_quantity * ratio)
            # 确保不超过剩余可加仓数量
            added_qty = self._get_added_position_quantity(code)
            max_add = initial_quantity - added_qty
            return min(add_qty, max_add)
        
        return 0
    
    def is_in_cooling_period(self, code: str, hours: int = None) -> bool:
        """
        检查是否在冷却期内
        
        Args:
            code: 股票代码
            hours: 冷却时间 (小时), 默认从配置文件读取
            
        Returns:
            bool: 是否在冷却期内
        """
        if hours is None:
            hours = self._config.get('cooldown', {}).get('hours', 4)
        
        # 获取最后交易时间
        last_trade_time = self._get_last_trade_time(code)
        if last_trade_time is None:
            return False
        
        # 计算冷却期结束时间
        now = datetime.now()
        cooldown_end = last_trade_time + timedelta(hours=hours)
        
        return now < cooldown_end
    
    def get_today_trade_count(self, code: str) -> int:
        """
        获取今日交易次数
        
        查询 paper_trades 表今日记录
        
        Args:
            code: 股票代码
            
        Returns:
            int: 今日交易次数 (表不存在时返回 0)
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            today = datetime.now().strftime('%Y-%m-%d')
            cursor.execute('''
                SELECT COUNT(*) FROM paper_trades 
                WHERE code = ? AND date(timestamp) = date(?)
            ''', (code, today))
            result = cursor.fetchone()
            return result[0] if result else 0
        except Exception:
            return 0  # 表不存在或查询失败，返回 0 不阻断开仓
        finally:
            self._return_connection(conn)
    
    def _get_initial_position_quantity(self, code: str) -> int:
        """
        获取初始仓位数量 (第一次开仓的数量)
        
        Args:
            code: 股票代码
            
        Returns:
            int: 初始仓位数量
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT quantity FROM positions 
                WHERE code = ? 
                ORDER BY entry_time ASC 
                LIMIT 1
            ''', (code,))
            result = cursor.fetchone()
            return result[0] if result else 0
        finally:
            self._return_connection(conn)
    
    def _get_added_position_quantity(self, code: str) -> int:
        """
        获取已加仓数量 (通过 paper_trades 中的 ADD 类型记录计算)
        
        Args:
            code: 股票代码
            
        Returns:
            int: 已加仓数量
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COALESCE(SUM(quantity), 0) FROM paper_trades 
                WHERE code = ? AND trade_type = 'ADD'
            ''', (code,))
            result = cursor.fetchone()
            return result[0] if result else 0
        finally:
            self._return_connection(conn)
    
    def _get_last_trade_time(self, code: str) -> Optional[datetime]:
        """
        获取最后交易时间
        
        Args:
            code: 股票代码
            
        Returns:
            datetime: 最后交易时间，无交易记录返回 None
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT timestamp FROM paper_trades 
                WHERE code = ? 
                ORDER BY timestamp DESC 
                LIMIT 1
            ''', (code,))
            result = cursor.fetchone()
            if result and result[0]:
                # 兼容 Python 3.6: 使用 strptime 替代 fromisoformat
                timestamp_str = result[0]
                try:
                    # 尝试 ISO 格式 (带微秒)
                    return datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%S.%f')
                except ValueError:
                    try:
                        # 尝试 ISO 格式 (不带微秒)
                        return datetime.strptime(timestamp_str, '%Y-%m-%dT%H:%M:%S')
                    except ValueError:
                        # 尝试数据库默认格式
                        return datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
            return None
        finally:
            self._return_connection(conn)
