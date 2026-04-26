"""
风控状态管理 (Risk State Management)

功能:
- 跟踪每个标的的风控状态
- 记录拒绝原因和冷却时间
- 提供状态化风控检查
"""

import logging
from app.storage.repository import get_db
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ftseek.risk.risk_state")
# 2026-04-10: propagate=True (默认), 通过 ftseek 父级 logger 输出


@dataclass
class RiskState:
    """标的风险状态"""
    code: str                              # 股票代码
    last_reject_time: Optional[datetime] = None  # 最后拒绝时间
    reject_reason: str = ""                # 拒绝原因
    reject_count_1h: int = 0               # 1 小时内被拒绝次数
    cooldown_until: Optional[datetime] = None  # 冷却结束时间
    risk_score: float = 0.0                # 风险评分 (0-1, 越高风险越大)

    # 详细统计
    total_checks: int = 0                  # 总检查次数
    total_rejects: int = 0                 # 总拒绝次数
    last_check_time: Optional[datetime] = None  # 最后检查时间

    def is_in_cooldown(self) -> bool:
        """是否处于冷却期"""
        if self.cooldown_until is None:
            return False
        return datetime.now() < self.cooldown_until

    def cooldown_remaining(self) -> int:
        """冷却剩余时间 (秒)"""
        if self.cooldown_until is None:
            return 0
        remaining = (self.cooldown_until - datetime.now()).total_seconds()
        return max(0, int(remaining))

    def add_reject(self, reason: str, cooldown_minutes: int = 5):
        """
        记录拒绝

        Args:
            reason: 拒绝原因
            cooldown_minutes: 冷却时间 (分钟)
        """
        self.last_reject_time = datetime.now()
        self.reject_reason = reason
        self.reject_count_1h += 1
        self.cooldown_until = datetime.now() + timedelta(minutes=cooldown_minutes)
        self.total_rejects += 1

        # 更新风险评分 (被拒绝次数越多，风险评分越高)
        self.risk_score = min(1.0, self.total_rejects / max(1, self.total_checks))

    def add_check(self):
        """记录检查"""
        self.total_checks += 1
        self.last_check_time = datetime.now()

        # 重置 1 小时计数
        if self.last_reject_time:
            if datetime.now() - self.last_reject_time > timedelta(hours=1):
                self.reject_count_1h = 0


class RiskStateManager:
    """
    风控状态管理器

    跟踪所有标的的风控状态，提供状态化风控检查
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        初始化状态管理器

        Args:
            db_path: 数据库路径，默认使用 data/risk_state.db
        """
        if db_path is None:
            db_path = "/opt/ftseek/FTSeek/data/risk_state.db"

        self.db_path = db_path

        # 确保目录存在
        db_dir = Path(db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        # 内存缓存 {code: RiskState}
        self._cache: Dict[str, RiskState] = {}
        self._cache_max_size = 500  # 最多缓存 500 个标的

        # 配置
        self.default_cooldown_minutes = 5   # 默认冷却时间
        self.max_cooldown_minutes = 30      # 最大冷却时间

        self._init_db()
        logger.info(f"风控状态管理器已初始化 | 数据库：{db_path}")

    def _init_db(self):
        """初始化数据库表"""
        with get_db(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS risk_state (
                    code TEXT PRIMARY KEY,
                    last_reject_time TIMESTAMP,
                    reject_reason TEXT,
                    reject_count_1h INTEGER DEFAULT 0,
                    cooldown_until TIMESTAMP,
                    risk_score REAL DEFAULT 0.0,
                    total_checks INTEGER DEFAULT 0,
                    total_rejects INTEGER DEFAULT 0,
                    last_check_time TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # 创建索引
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_risk_score ON risk_state(risk_score)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_cooldown ON risk_state(cooldown_until)
            ''')

            conn.commit()

            # 加载缓存
            self._load_cache()
    def _load_cache(self):
        """从数据库加载缓存"""
        with get_db(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT code, last_reject_time, reject_reason, reject_count_1h,
                       cooldown_until, risk_score, total_checks, total_rejects,
                       last_check_time
                FROM risk_state
            ''')

            for row in cursor.fetchall():
                self._cache[row[0]] = RiskState(
                    code=row[0],
                    last_reject_time=self._parse_time(row[1]),
                    reject_reason=row[2] or "",
                    reject_count_1h=row[3] or 0,
                    cooldown_until=self._parse_time(row[4]),
                    risk_score=row[5] or 0.0,
                    total_checks=row[6] or 0,
                    total_rejects=row[7] or 0,
                    last_check_time=self._parse_time(row[8]),
                )
    def _parse_time(self, value) -> Optional[datetime]:
        """解析时间字符串"""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None

    def get_state(self, code: str) -> RiskState:
        """
        获取标的的风险状态

        Args:
            code: 股票代码

        Returns:
            风险状态对象
        """
        if code not in self._cache:
            self._cache[code] = RiskState(code=code)

            # 限制缓存大小
            if len(self._cache) > self._cache_max_size:
                # 删除最久未访问的
                oldest_code = min(self._cache.keys(),
                                 key=lambda k: self._cache[k].last_check_time or datetime.min)
                del self._cache[oldest_code]

        return self._cache[code]

    def check_cooldown(self, code: str) -> Tuple[bool, int]:
        """
        检查标的是否处于冷却期

        Args:
            code: 股票代码

        Returns:
            (是否冷却中，剩余秒数)
        """
        state = self.get_state(code)
        is_cooldown = state.is_in_cooldown()
        remaining = state.cooldown_remaining() if is_cooldown else 0

        return is_cooldown, remaining

    def record_reject(self, code: str, reason: str,
                      cooldown_minutes: Optional[int] = None):
        """
        记录拒绝

        Args:
            code: 股票代码
            reason: 拒绝原因
            cooldown_minutes: 冷却时间 (分钟)
        """
        state = self.get_state(code)

        # 根据拒绝次数动态调整冷却时间
        if cooldown_minutes is None:
            # 基础冷却时间 + 累进惩罚
            base = self.default_cooldown_minutes
            penalty = min(15, (state.reject_count_1h // 3) * 5)  # 每 3 次增加 5 分钟，最多 15 分钟
            cooldown_minutes = min(self.max_cooldown_minutes, base + penalty)

        state.add_reject(reason, cooldown_minutes)
        self._save_state(state)

        logger.debug(f"[风控状态] {code} 被拒绝：{reason}, 冷却{cooldown_minutes}分钟")

    def record_check(self, code: str):
        """
        记录检查

        Args:
            code: 股票代码
        """
        state = self.get_state(code)
        state.add_check()
        self._save_state(state)

    def _save_state(self, state: RiskState):
        """保存状态到数据库"""
        with get_db(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO risk_state
                (code, last_reject_time, reject_reason, reject_count_1h, cooldown_until,
                 risk_score, total_checks, total_rejects, last_check_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (
                state.code,
                state.last_reject_time.isoformat() if state.last_reject_time else None,
                state.reject_reason,
                state.reject_count_1h,
                state.cooldown_until.isoformat() if state.cooldown_until else None,
                state.risk_score,
                state.total_checks,
                state.total_rejects,
                state.last_check_time.isoformat() if state.last_check_time else None,
            ))
            conn.commit()
    def get_risk_score(self, code: str) -> float:
        """
        获取标的风险评分

        Args:
            code: 股票代码

        Returns:
            风险评分 (0-1)
        """
        state = self.get_state(code)
        return state.risk_score

    def get_all_states(self) -> Dict[str, RiskState]:
        """
        获取所有标的的状态

        Returns:
            状态字典
        """
        return self._cache.copy()

    def clear_old_data(self, hours: int = 24) -> int:
        """
        清理旧数据

        Args:
            hours: 清理超过 N 小时的数据

        Returns:
            清理的记录数
        """
        with get_db(self.db_path) as conn:
            cursor = conn.cursor()
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

            # 重置超过指定时间的 1 小时计数
            cursor.execute('''
                UPDATE risk_state
                SET reject_count_1h = 0
                WHERE last_reject_time < ?
            ''', (cutoff,))
            cleared = cursor.rowcount
            conn.commit()

            logger.info(f"已清理 {cleared} 条记录的 1 小时计数")
            return cleared
    def get_statistics(self) -> Dict:
        """
        获取统计信息

        Returns:
            统计字典
        """
        total_codes = len(self._cache)
        in_cooldown = sum(1 for s in self._cache.values() if s.is_in_cooldown())
        high_risk = sum(1 for s in self._cache.values() if s.risk_score >= 0.7)

        return {
            "total_codes": total_codes,
            "in_cooldown": in_cooldown,
            "high_risk": high_risk,
            "avg_risk_score": sum(s.risk_score for s in self._cache.values()) / max(1, total_codes),
        }
