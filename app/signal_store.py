"""
信号管理器
存储待确认信号，供 FastAPI 回调服务查询和确认
内置信号冷却：同策略 + 同标的 + 同方向 在冷却期内不重复触发
"""
import time
import threading
import logging
from typing import Dict, Optional, List

from app import config
from app.strategy.base import Signal

logger = logging.getLogger("ftseek.signal_store")
logger.propagate = False


class SignalStore:
    """线程安全的信号存储"""

    def __init__(self):
        self._signals: Dict[str, Signal] = {}
        self._lock = threading.Lock()
        self._history: List[Signal] = []  # 已处理的信号历史
        # 冷却记录表 { "strategy:symbol:action": last_timestamp }
        self._cooldown_map: Dict[str, float] = {} 
        
        # 差异化冷却: 新闻信号与技术信号使用不同的冷却时间
        self._cooldown_map_news: Dict[str, float] = {}  # 新闻信号冷却

        # DB 持久化 (lazy init)
        self._db_conn: Optional['sqlite3.Connection'] = None
        self._db_lock = threading.Lock()
        self._init_signal_db()

    def _init_signal_db(self):
        """初始化信号持久化数据库"""
        try:
            from pathlib import Path
            import sqlite3
            db_path = Path(__file__).parent.parent / "data" / "ftseek.db"
            if not db_path.parent.exists():
                db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
            # 开启 WAL 模式提升并发写入性能
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            # 确保 signals 表存在
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    name TEXT,
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity INTEGER DEFAULT 1,
                    strategy TEXT,
                    technical_score REAL,
                    sentiment_score REAL,
                    confidence REAL,
                    reason TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expire_at DATETIME,
                    confirmed_at DATETIME,
                    executed_at DATETIME,
                    status TEXT DEFAULT 'pending'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_code ON signals(code)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at)")
            conn.commit()
            self._db_conn = conn
            logger.info("✅ SignalStore DB 持久化已初始化")
        except Exception as e:
            logger.warning(f"SignalStore DB 初始化失败 (信号将仅存内存): {e}")
            self._db_conn = None

    def _persist_signal(self, signal: Signal):
        """将信号持久化到 SQLite"""
        if self._db_conn is None:
            return
        try:
            with self._db_lock:
                from datetime import datetime
                self._db_conn.execute("""
                    INSERT OR REPLACE INTO signals 
                    (id, code, name, action, price, quantity, strategy, 
                     technical_score, sentiment_score, confidence, reason,
                     created_at, expire_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    signal.id,
                    signal.code,
                    signal.name or "",
                    signal.action.value,
                    signal.price,
                    signal.quantity,
                    signal.strategy or "",
                    signal.extra.get("technical_score") if signal.extra else None,
                    signal.extra.get("sentiment_score") if signal.extra else None,
                    signal.extra.get("confidence") if signal.extra else None,
                    signal.reason or "",
                    datetime.now().isoformat(),
                    datetime.fromtimestamp(signal.expire_at).isoformat() if signal.expire_at > 0 else None,
                    "pending",
                ))
                self._db_conn.commit()
        except Exception as e:
            logger.warning(f"信号 DB 持久化失败: {e}")

    def _get_cooldown_key(self, signal: Signal) -> str:
        """生成冷却键值：策略 ID + 股票代码 + 动作"""
        strategy_id = getattr(signal, 'strategy_id', 'default_strategy')
        return f"{strategy_id}:{signal.code}:{signal.action.value}"

    def _is_from_news_source(self, signal: Signal) -> bool:
        """判断信号是否来自新闻驱动"""
        try:
            from app.strategy.consensus_types import SignalSource
            return signal.source == SignalSource.EVENT
        except Exception:
            # Fallback: 检查 extra 字段
            extra = getattr(signal, 'extra', {})
            return extra.get('source_type') == 'NEWS_DRIVEN'

    def _check_cooldown(self, signal: Signal) -> bool:
        """
        准确性优先：检查是否处于冷却期
        新闻信号: 600s 冷却 (10分钟, 给市场反应时间)
        技术信号: 120s 冷却 (2分钟, 快速响应)
        返回 True 表示可以通过，False 表示被拦截
        """
        key = self._get_cooldown_key(signal)
        now = time.time()
        
        # 差异化冷却
        is_news = self._is_from_news_source(signal)
        cooldown_seconds = 600 if is_news else config.SIGNAL_COOLDOWN
        
        cooldown_map = self._cooldown_map_news if is_news else self._cooldown_map
        
        with self._lock:
            last_time = cooldown_map.get(key)
            if last_time and (now - last_time) < cooldown_seconds:
                remaining = int(cooldown_seconds - (now - last_time))
                src = "新闻" if is_news else "技术"
                logger.info(f"🚫 {src}信号冷却拦截：{key} | 剩余 {remaining}s (冷却{cooldown_seconds}s)")
                return False
            
            # 更新冷却时间
            cooldown_map[key] = now
            return True

    def _update_signal_status(self, signal_id: str, status: str):
        """更新信号 DB 状态 (P0 修复 2026-05-21)"""
        if self._db_conn is None:
            return
        try:
            from datetime import datetime
            now = datetime.now().isoformat()
            if status == 'confirmed':
                self._db_conn.execute(
                    "UPDATE signals SET status = ?, confirmed_at = ? WHERE id = ?",
                    (status, now, signal_id)
                )
            elif status == 'executed':
                self._db_conn.execute(
                    "UPDATE signals SET status = ?, executed_at = ? WHERE id = ?",
                    (status, now, signal_id)
                )
            else:
                self._db_conn.execute(
                    "UPDATE signals SET status = ? WHERE id = ?",
                    (status, signal_id)
                )
            self._db_conn.commit()
        except Exception as e:
            logger.warning(f"信号状态更新失败: {signal_id} -> {status}: {e}")

    def add(self, signal: Signal) -> bool:
        """
        添加信号，自动设置过期时间
        新闻信号: 30分钟过期 (给市场反应和人工确认时间)
        技术信号: 5分钟过期 (快速响应)
        返回：True 表示成功入库，False 表示被冷却拦截
        """
        # 1. 先检查冷却 (准确性优先的核心)
        if not self._check_cooldown(signal):
            return False
        
        # 2. 设置过期时间 (差异化)
        is_news = self._is_from_news_source(signal)
        news_expire_seconds = 1800  # 30分钟
        
        if signal.expire_at <= 0:
            if is_news:
                signal.expire_at = time.time() + news_expire_seconds
                logger.debug(f"新闻信号过期时间: {news_expire_seconds}s (30分钟)")
            else:
                signal.expire_at = time.time() + config.SIGNAL_EXPIRE_SECONDS
                
        # 3. 入库
        with self._lock:
            self._signals[signal.id] = signal
            src = "📰 新闻" if is_news else "📊 技术"
            logger.info(f"✅ {src}信号入库：{signal.id} -> {signal.code} {signal.action.value}")
            
            # 3.5. DB 持久化 (异步写入，不阻塞主流程)
            self._persist_signal(signal)
            
            # Phase 3: 记录事件日志 (WAL)
            try:
                from app.core.event_log import log_event
                log_event(
                    trace_id=signal.trace_id,
                    event_type="SIGNAL",
                    symbol=signal.code,
                    data={
                        "action": signal.action.value,
                        "price": signal.price,
                        "score": signal.score,
                        "strategy": signal.strategy,
                        "source": "NEWS" if is_news else "TECH",
                    }
                )
            except Exception:
                pass
        
        return True

    def get(self, signal_id: str) -> Optional[Signal]:
        with self._lock:
            return self._signals.get(signal_id)

    def confirm(self, signal_id: str) -> Optional[Signal]:
        """确认信号，返回信号对象。已确认的信号不可重复执行。"""
        with self._lock:
            signal = self._signals.get(signal_id)
            if not signal:
                logger.warning(f"信号不存在：{signal_id}")
                return None
            if signal.is_expired:
                logger.warning(f"信号已过期：{signal_id}")
                del self._signals[signal_id]
                return None
            if signal.confirmed:
                logger.warning(f"信号已被确认过 (防重放): {signal_id}")
                return None
            if signal.executed:
                logger.warning(f"信号已执行过 (防重放): {signal_id}")
                return None
            
            signal.confirmed = True
            logger.info(f"🔔 信号已确认：{signal_id} -> {signal.code} {signal.action.value}")
            
            # P0 修复 (2026-05-21): 同步更新 DB 状态
            self._update_signal_status(signal_id, 'confirmed')
            
            return signal

    def reject(self, signal_id: str) -> bool:
        """拒绝/忽略信号"""
        with self._lock:
            signal = self._signals.pop(signal_id, None)
            if signal:
                self._history.append(signal)
                logger.info(f"❌ 信号已拒绝：{signal_id}")
                return True
            return False

    def mark_executed(self, signal_id: str):
        """标记信号已执行"""
        with self._lock:
            signal = self._signals.pop(signal_id, None)
            if signal:
                signal.executed = True
                self._history.append(signal)
            
            # P0 修复 (2026-05-21): 同步更新 DB 状态
            self._update_signal_status(signal_id, 'executed')

    def cleanup_expired(self):
        """清理过期信号"""
        with self._lock:
            expired = [
                sid for sid, s in self._signals.items() if s.is_expired
            ]
            for sid in expired:
                s = self._signals.pop(sid)
                self._history.append(s)
            if expired:
                logger.info(f"🧹 清理过期信号 {len(expired)} 条")

    def get_pending(self) -> List[Signal]:
        """获取所有待处理信号"""
        with self._lock:
            return [
                s for s in self._signals.values()
                if not s.confirmed and not s.is_expired
            ]

    def get_history(self, limit: int = 50) -> List[Signal]:
        """获取历史信号"""
        return self._history[-limit:]

    def get_positions(self) -> List[Signal]:
        """获取所有已确认且未过期的持仓信号"""
        with self._lock:
            return [
                s for s in self._signals.values()
                if s.confirmed and not s.executed and not s.is_expired
            ]

    def count_positions(self) -> int:
        """当前持仓数量"""
        return len(self.get_positions())


# 全局单例
signal_store = SignalStore()
