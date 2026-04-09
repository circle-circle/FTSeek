"""
场景日志模块 (Scenario Log)

记录场景切换历史到数据库，支持：
1. 场景切换日志记录
2. 场景历史查询
3. 场景绩效分析
"""
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

logger = logging.getLogger("ftseek.scenario.log")
logger.propagate = False


class ScenarioLogManager:
    """
    场景日志管理器

    功能:
    1. 记录场景切换到数据库
    2. 查询场景历史
    3. 场景绩效统计
    4. 场景切换频率分析
    """

    def __init__(self, db_path: str = None):
        """
        初始化场景日志管理器

        Args:
            db_path: 数据库文件路径
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            from app import config
            self.db_path = config.DATA_DIR / "ftseek.db"

        # 确保数据目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_tables()
        logger.info(f"场景日志管理器已初始化：{self.db_path}")

    @contextmanager
    def get_connection(self):
        """获取数据库连接上下文管理器"""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row

        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")

        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败：{e}")
            raise
        finally:
            conn.close()

    def _init_tables(self):
        """初始化数据库表结构"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 场景切换日志表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scenario_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    adx_value REAL DEFAULT 0,
                    macro_regime TEXT DEFAULT '',
                    mtf_alignment REAL DEFAULT 0,
                    multi_timeframe TEXT DEFAULT '{}',
                    triggered_by TEXT DEFAULT 'auto',
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_scenario_logs_code
                ON scenario_logs(code)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_scenario_logs_scenario
                ON scenario_logs(scenario)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_scenario_logs_timestamp
                ON scenario_logs(timestamp)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_scenario_logs_code_timestamp
                ON scenario_logs(code, timestamp DESC)
            """)

            logger.info("场景日志表已初始化")

    def log_scenario_change(
        self,
        code: str,
        scenario: str,
        confidence: float,
        adx_value: float = 0.0,
        macro_regime: str = "",
        mtf_alignment: float = 0.0,
        multi_timeframe: Dict = None,
        triggered_by: str = "auto"
    ) -> bool:
        """
        记录场景切换

        Args:
            code: 股票代码
            scenario: 场景名称
            confidence: 置信度
            adx_value: ADX 值
            macro_regime: 宏观市场状态
            mtf_alignment: 多周期趋势一致性
            multi_timeframe: 多周期特征数据 (JSON)
            triggered_by: 触发方式 (auto/manual)

        Returns:
            bool: 是否成功记录
        """
        import json

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 检查是否重复记录 (1 分钟内相同场景不重复记录)
            cutoff = (datetime.now() - timedelta(minutes=1)).isoformat()
            cursor.execute("""
                SELECT COUNT(*) FROM scenario_logs
                WHERE code = ? AND scenario = ? AND timestamp > ?
            """, (code, scenario, cutoff))

            count = cursor.fetchone()[0]
            if count > 0:
                logger.debug(f"{code} 1 分钟内已记录 {scenario}，跳过")
                return False

            # 插入新记录
            cursor.execute("""
                INSERT INTO scenario_logs
                (code, scenario, confidence, adx_value, macro_regime,
                 mtf_alignment, multi_timeframe, triggered_by, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                code,
                scenario,
                round(confidence, 4),
                round(adx_value, 2),
                macro_regime,
                round(mtf_alignment, 4),
                json.dumps(multi_timeframe or {}),
                triggered_by,
                datetime.now().isoformat()
            ))

            logger.info(
                f"[场景日志] {code} → {scenario} | "
                f"置信度={confidence:.2f} | ADX={adx_value:.1f}"
            )
            return True

    def get_scenario_history(
        self,
        code: str,
        limit: int = 50,
        days: int = 7
    ) -> List[Dict[str, Any]]:
        """
        获取某股票的场景历史

        Args:
            code: 股票代码
            limit: 返回记录数
            days: 查询天数范围

        Returns:
            场景历史记录列表
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    id, code, scenario, confidence, adx_value,
                    macro_regime, mtf_alignment, multi_timeframe,
                    triggered_by, timestamp
                FROM scenario_logs
                WHERE code = ? AND timestamp > ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (code, cutoff, limit))

            rows = cursor.fetchall()
            results = []
            for row in rows:
                row_dict = dict(row)
                # 解析 JSON 字段
                if row_dict.get('multi_timeframe'):
                    import json
                    try:
                        row_dict['multi_timeframe'] = json.loads(row_dict['multi_timeframe'])
                    except:
                        row_dict['multi_timeframe'] = {}
                results.append(row_dict)

            return results

    def get_current_scenario(self, code: str) -> Optional[str]:
        """
        获取某股票的当前场景

        Args:
            code: 股票代码

        Returns:
            当前场景名称
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT scenario FROM scenario_logs
                WHERE code = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (code,))

            row = cursor.fetchone()
            return row['scenario'] if row else None

    def get_all_active_codes(self) -> List[str]:
        """
        获取所有有场景记录的股票

        Returns:
            股票代码列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT code FROM scenario_logs
                ORDER BY code
            """)
            return [row['code'] for row in cursor.fetchall()]

    def get_scenario_statistics(
        self,
        days: int = 7,
        group_by: str = "scenario"
    ) -> Dict[str, Any]:
        """
        获取场景统计数据

        Args:
            days: 统计天数
            group_by: 分组方式 (scenario/code)

        Returns:
            统计数据字典
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        with self.get_connection() as conn:
            cursor = conn.cursor()

            if group_by == "scenario":
                cursor.execute("""
                    SELECT
                        scenario,
                        COUNT(*) as total_count,
                        COUNT(DISTINCT code) as unique_codes,
                        AVG(confidence) as avg_confidence,
                        AVG(adx_value) as avg_adx,
                        MAX(confidence) as max_confidence,
                        MIN(confidence) as min_confidence
                    FROM scenario_logs
                    WHERE timestamp > ?
                    GROUP BY scenario
                    ORDER BY total_count DESC
                """, (cutoff,))
            else:  # group_by == "code"
                cursor.execute("""
                    SELECT
                        code,
                        COUNT(*) as total_count,
                        COUNT(DISTINCT scenario) as unique_scenarios,
                        AVG(confidence) as avg_confidence,
                        AVG(adx_value) as avg_adx
                    FROM scenario_logs
                    WHERE timestamp > ?
                    GROUP BY code
                    ORDER BY total_count DESC
                """, (cutoff,))

            rows = cursor.fetchall()
            results = []
            for row in rows:
                results.append(dict(row))

            return {
                "days": days,
                "group_by": group_by,
                "data": results,
                "total_records": sum(r.get('total_count', 0) for r in results) if group_by == "scenario" else len(results)
            }

    def get_scenario_switches(
        self,
        code: str,
        days: int = 1
    ) -> List[Dict[str, Any]]:
        """
        获取场景切换记录 (仅记录场景变化)

        Args:
            code: 股票代码
            days: 查询天数

        Returns:
            场景切换记录列表
        """
        history = self.get_scenario_history(code, limit=100, days=days)

        if not history:
            return []

        switches = []
        prev_scenario = None

        for record in history:
            current_scenario = record['scenario']
            if current_scenario != prev_scenario:
                switches.append({
                    'from_scenario': prev_scenario,
                    'to_scenario': current_scenario,
                    'confidence': record['confidence'],
                    'adx_value': record['adx_value'],
                    'timestamp': record['timestamp'],
                })
                prev_scenario = current_scenario

        return switches

    def get_switch_frequency(
        self,
        code: str = None,
        hours: int = 24
    ) -> Dict[str, Any]:
        """
        获取场景切换频率

        Args:
            code: 股票代码 (None=全部)
            hours: 统计小时数

        Returns:
            切换频率统计
        """
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

        with self.get_connection() as conn:
            cursor = conn.cursor()

            if code:
                cursor.execute("""
                    SELECT
                        code,
                        COUNT(DISTINCT scenario) as unique_scenarios,
                        COUNT(*) as total_logs,
                        (COUNT(*) * 1.0 / ?) as logs_per_hour
                    FROM scenario_logs
                    WHERE code = ? AND timestamp > ?
                """, (hours, code, cutoff))
            else:
                cursor.execute("""
                    SELECT
                        COUNT(DISTINCT code) as unique_codes,
                        COUNT(DISTINCT scenario) as unique_scenarios,
                        COUNT(*) as total_logs,
                        (COUNT(*) * 1.0 / ?) as logs_per_hour
                    FROM scenario_logs
                    WHERE timestamp > ?
                """, (hours, cutoff))

            row = cursor.fetchone()
            result = dict(row)
            result['hours'] = hours
            return result

    def cleanup_old_logs(self, keep_days: int = 30) -> int:
        """
        清理旧的场景日志

        Args:
            keep_days: 保留天数

        Returns:
            删除的记录数
        """
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM scenario_logs WHERE timestamp < ?
            """, (cutoff,))
            deleted = cursor.rowcount

            if deleted > 0:
                logger.info(f"清理了 {deleted} 条旧场景日志")

            return deleted


# 全局单例
_log_manager: Optional[ScenarioLogManager] = None


def get_scenario_log_manager(db_path: str = None) -> ScenarioLogManager:
    """获取场景日志管理器单例"""
    global _log_manager
    if _log_manager is None:
        _log_manager = ScenarioLogManager(db_path)
    return _log_manager
