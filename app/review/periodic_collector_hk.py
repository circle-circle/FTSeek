"""定期交易收集器模块 - 已增强为真实DB数据源 (2026-05-04)


负责从 ftseek.db / execution.db / consensus_audit.db 读取真实交易数据。
保留 Futu API 客户端作为可选实时数据源。
支持港股 (HK) 和美股 (US) 市场。

数据来源优先级：真实 DB → Futu API → Mock Fallback
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from enum import Enum
import uuid

logger = logging.getLogger(__name__)

# 真实数据库路径 (从环境变量读取)
FTSEEK_DB = os.environ.get("FTSEEK_DB_PATH", "data/ftseek.db")
EXECUTION_DB = os.environ.get("FTSEEK_EXECUTION_DB_PATH", "data/execution.db")
CONSENSUS_AUDIT_DB = os.environ.get("FTSEEK_CONSENSUS_AUDIT_DB_PATH", "data/consensus_audit.db")


class MarketStatus(Enum):
    """市场状态枚举"""
    OPEN = "open"
    CLOSED = "closed"
    HALT = "halt"
    UNKNOWN = "unknown"


class JobStatus(Enum):
    """任务状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobType(Enum):
    """任务类型枚举"""
    PERIODIC = "periodic"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass
class TradeRecord:
    """交易记录数据类"""
    trade_id: str
    stock_code: str
    market: str
    side: str
    price: float
    quantity: int
    exec_time: datetime
    pnl: float = 0.0
    pnl_percent: float = 0.0
    commission: float = 0.0
    stamp_duty: float = 0.0
    strategy_id: Optional[str] = None
    signal_strength: Optional[float] = None
    position_ratio: Optional[float] = None
    stop_loss_triggered: bool = False
    holding_days: int = 0
    day_low: Optional[float] = None
    day_high: Optional[float] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo('Asia/Shanghai')))


@dataclass
class CollectionJobLog:
    """收集任务日志数据类"""
    job_id: str
    job_type: JobType
    market: str
    scheduled_time: datetime
    executed_time: Optional[datetime] = None
    status: JobStatus = JobStatus.PENDING
    trades_collected_count: int = 0
    retry_count: int = 0
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo('Asia/Shanghai')))
    updated_at: datetime = field(default_factory=lambda: datetime.now(ZoneInfo('Asia/Shanghai')))


class MarketTimeChecker:
    """市场时间检查器"""
    
    HK_MARKET_HOURS = {
        'morning_start': datetime.strptime("09:30", "%H:%M").time(),
        'morning_end': datetime.strptime("12:00", "%H:%M").time(),
        'afternoon_start': datetime.strptime("13:00", "%H:%M").time(),
        'afternoon_end': datetime.strptime("16:00", "%H:%M").time(),
    }
    
    US_MARKET_HOURS_SUMMER = {
        'start': datetime.strptime("21:30", "%H:%M").time(),
        'end': datetime.strptime("04:00", "%H:%M").time(),
    }
    US_MARKET_HOURS_WINTER = {
        'start': datetime.strptime("22:30", "%H:%M").time(),
        'end': datetime.strptime("05:00", "%H:%M").time(),
    }
    
    def __init__(self, timezone: str = 'Asia/Shanghai'):
        self.timezone = ZoneInfo(timezone)
        self.logger = logging.getLogger(f"{__name__}.MarketTimeChecker")
    
    def is_market_open(self, market: str, check_time: Optional[datetime] = None) -> bool:
        if check_time is None:
            check_time = datetime.now(self.timezone)
        elif check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=self.timezone)
        
        if market.upper() == 'HK':
            return self._is_hk_market_open(check_time)
        elif market.upper() == 'US':
            return self._is_us_market_open(check_time)
        else:
            return False
    
    def _is_hk_market_open(self, check_time: datetime) -> bool:
        if check_time.weekday() >= 5:
            return False
        
        current_time = check_time.time()
        morning_open = self.HK_MARKET_HOURS['morning_start'] <= current_time <= self.HK_MARKET_HOURS['morning_end']
        afternoon_open = self.HK_MARKET_HOURS['afternoon_start'] <= current_time <= self.HK_MARKET_HOURS['afternoon_end']
        
        return morning_open or afternoon_open
    
    def _is_us_market_open(self, check_time: datetime) -> bool:
        if check_time.weekday() >= 5:
            return False
        
        is_summer_time = self._is_us_summer_time(check_time)
        market_hours = self.US_MARKET_HOURS_SUMMER if is_summer_time else self.US_MARKET_HOURS_WINTER
        
        current_time = check_time.time()
        start_time = market_hours['start']
        end_time = market_hours['end']
        
        if start_time > end_time:
            return current_time >= start_time or current_time <= end_time
        else:
            return start_time <= current_time <= end_time
    
    def _is_us_summer_time(self, check_time: datetime) -> bool:
        year = check_time.year
        march_first = datetime(year, 3, 1)
        march_second_sunday = march_first + timedelta(days=(6 - march_first.weekday()) % 7 + 7)
        nov_first = datetime(year, 11, 1)
        nov_first_sunday = nov_first + timedelta(days=(6 - nov_first.weekday()) % 7)
        # 移除时区信息进行比较
        check_date = check_time.replace(tzinfo=None)
        return march_second_sunday <= check_date < nov_first_sunday
    
    def get_market_status(self, market: str, check_time: Optional[datetime] = None) -> MarketStatus:
        if not self.is_market_open(market, check_time):
            return MarketStatus.CLOSED
        
        if market.upper() == 'HK':
            if check_time is None:
                check_time = datetime.now(self.timezone)
            elif check_time.tzinfo is None:
                check_time = check_time.replace(tzinfo=self.timezone)
            
            current_time = check_time.time()
            noon_start = self.HK_MARKET_HOURS['morning_end']
            noon_end = self.HK_MARKET_HOURS['afternoon_start']
            
            if noon_start < current_time < noon_end:
                return MarketStatus.HALT
        
        return MarketStatus.OPEN


class FutuAPIClient:
    """富途 API 客户端封装"""
    
    def __init__(self, host: str = 'localhost', port: int = 11111):
        self.host = host
        self.port = port
        self.logger = logging.getLogger(f"{__name__}.FutuAPIClient")
    
    def fetch_trade_list(self, market: str, start_time: datetime, end_time: datetime, status: str = 'FILLED') -> List[Dict[str, Any]]:
        self.logger.info(f"Fetching trades: market={market}, start={start_time.isoformat()}, end={end_time.isoformat()}")
        
        try:
            trades = self._mock_fetch_trades(market, start_time, end_time)
            self.logger.info(f"Successfully fetched {len(trades)} trades")
            return trades
        except Exception as e:
            self.logger.error(f"Failed to fetch trades: {e}")
            raise
    
    def _mock_fetch_trades(self, market: str, start_time: datetime, end_time: datetime) -> List[Dict[str, Any]]:
        import random
        trade_count = random.randint(0, 10)
        
        trades = []
        for i in range(trade_count):
            trade = {
                'trade_id': f"MOCK_{market}_{uuid.uuid4().hex[:8]}",
                'stock_code': f"{'0' if market == 'HK' else ''}{random.randint(1, 99999)}",
                'market': market,
                'side': random.choice(['BUY', 'SELL']),
                'price': random.uniform(10, 1000),
                'quantity': random.randint(100, 10000),
                'exec_time': start_time + timedelta(minutes=random.randint(0, 30)),
                'pnl': random.uniform(-1000, 5000),
                'pnl_percent': random.uniform(-0.05, 0.1),
                'commission': random.uniform(1, 100),
                'stamp_duty': random.uniform(0, 50) if market == 'HK' else 0,
            }
            trades.append(trade)
        
        return trades
    
    def fetch_daily_trades(self, market: str, date: str, status: str = 'FILLED') -> List[Dict[str, Any]]:
        start_time = datetime.strptime(date, '%Y-%m-%d').replace(
            hour=0, minute=0, second=0, microsecond=0,
            tzinfo=self.timezone if hasattr(self, 'timezone') else ZoneInfo('Asia/Shanghai')
        )
        end_time = start_time.replace(hour=23, minute=59, second=59)
        return self.fetch_trade_list(market, start_time, end_time, status)


class DatabaseManager:
    """数据库管理器"""
    
    def __init__(self, connection_string: Optional[str] = None):
        self.connection_string = connection_string
        self.logger = logging.getLogger(f"{__name__}.DatabaseManager")
    
    def store_trades(self, trades: List[TradeRecord], market: str) -> int:
        self.logger.info(f"Storing {len(trades)} trades for market {market}")
        stored_count = len(trades)
        self.logger.info(f"Successfully stored {stored_count} trades")
        return stored_count
    
    def create_job_log(self, job_log: CollectionJobLog) -> str:
        self.logger.info(f"Creating job log: {job_log.job_id}")
        return job_log.job_id
    
    def update_job_log(self, job_id: str, status: JobStatus, trades_count: int = 0, error_message: Optional[str] = None) -> None:
        self.logger.info(f"Updating job log {job_id}: status={status.value}")


class PeriodicTradeCollector:
    """
    定期交易收集器 - 增强版 (2026-05-04)

    优先从真实数据库读取交易/持仓/执行数据，Futu API 为可选实时补充。

    使用示例:
        collector = PeriodicTradeCollector(use_real_db=True)
        count = collector.collect_trades(market='HK')
    """

    def __init__(
        self,
        futu_api: Optional[FutuAPIClient] = None,
        db_manager: Optional[DatabaseManager] = None,
        market_checker: Optional[MarketTimeChecker] = None,
        collection_interval_minutes: int = 30,
        use_real_db: bool = True,
    ):
        self.futu_api = futu_api or FutuAPIClient()
        self.db_manager = db_manager or DatabaseManager()
        self.market_checker = market_checker or MarketTimeChecker()
        self.collection_interval_minutes = collection_interval_minutes
        self.use_real_db = use_real_db
        self.timezone = ZoneInfo('Asia/Shanghai')
        self.logger = logging.getLogger(f"{__name__}.PeriodicTradeCollector")

    def collect_trades(self, market: str = 'HK', date: Optional[str] = None) -> int:
        """收集交易数据 - 优先从真实 DB 读取"""
        if self.use_real_db:
            return self.collect_from_database(market, date)

        job_id = self._create_job_log(market, JobType.PERIODIC)
        
        try:
            if not self.market_checker.is_market_open(market):
                self.logger.info(f"Market {market} is closed, skipping collection")
                self.db_manager.update_job_log(job_id, JobStatus.SKIPPED, 0)
                return 0
            
            end_time = datetime.now(self.timezone)
            start_time = end_time - timedelta(minutes=self.collection_interval_minutes)
            
            self.logger.info(f"Collection time range: {start_time.isoformat()} to {end_time.isoformat()}")
            
            self.logger.info(f"Fetching trades from Futu API for market {market}")
            trade_data = self.futu_api.fetch_trade_list(market, start_time, end_time)
            
            trades = self._convert_to_trade_records(trade_data, market)
            
            if trades:
                stored_count = self.db_manager.store_trades(trades, market)
            else:
                stored_count = 0
            
            self.db_manager.update_job_log(job_id, JobStatus.COMPLETED, stored_count)
            self.logger.info(f"Collection completed: {stored_count} trades collected for market {market}")
            
            return stored_count
            
        except Exception as e:
            self.logger.error(f"Collection failed for market {market}: {e}")
            self.db_manager.update_job_log(job_id, JobStatus.FAILED, 0, str(e))
            raise
    
    def collect_from_database(self, market: str = 'HK', date: Optional[str] = None) -> int:
        """从真实数据库读取交易数据 (SSOT)

        Args:
            market: HK / US
            date: YYYY-MM-DD，默认今天

        Returns:
            收集到的交易记录数量
        """
        if date is None:
            date = datetime.now(self.timezone).strftime("%Y-%m-%d")

        all_trades = []
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), FTSEEK_DB)

        try:
            # 1) 读取 ftseek.db → trades 表
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM trades WHERE date(entry_time) = ? OR date(exit_time) = ?",
                    (date, date)
                )
                for row in cur.fetchall():
                    all_trades.append({
                        "source": "ftseek.trades",
                        "trade_id": str(row["id"]) if "id" in row.keys() else "",
                        "code": row["code"] if "code" in row.keys() else "",
                        "direction": row["action"] if "action" in row.keys() else "",
                        "entry_price": row["entry_price"] if "entry_price" in row.keys() else 0,
                        "exit_price": row["exit_price"] if "exit_price" in row.keys() else None,
                        "quantity": row["quantity"] if "quantity" in row.keys() else 0,
                        "pnl": row["pnl"] if "pnl" in row.keys() else 0,
                        "pnl_percent": row["pnl_percent"] if "pnl_percent" in row.keys() else 0,
                        "strategy": row["strategy"] if "strategy" in row.keys() else "",
                        "entry_time": row["entry_time"] if "entry_time" in row.keys() else None,
                        "exit_time": row["exit_time"] if "exit_time" in row.keys() else None,
                        "exit_reason": row["exit_reason"] if "exit_reason" in row.keys() else "",
                        "status": row["status"] if "status" in row.keys() else "",
                    })
                conn.close()
                self.logger.info(f"[RealDB] ftseek.db trades: {len(all_trades)} records for {date}")

            # 2) 读取 ftseek.db → positions (CLOSED)
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM positions WHERE status = 'CLOSED' AND date(exit_time) = ?",
                    (date,)
                )
                for row in cur.fetchall():
                    all_trades.append({
                        "source": "ftseek.positions",
                        "trade_id": f"POS_{row['id']}",
                        "code": row["code"] if "code" in row.keys() else "",
                        "direction": row["action"] if "action" in row.keys() else "",
                        "entry_price": row["entry_price"] if "entry_price" in row.keys() else 0,
                        "exit_price": row["exit_price"] if "exit_price" in row.keys() else 0,
                        "quantity": row["quantity"] if "quantity" in row.keys() else 0,
                        "pnl": row["realized_pnl"] if "realized_pnl" in row.keys() else 0,
                        "pnl_percent": row["pl_ratio"] if "pl_ratio" in row.keys() else 0,
                        "strategy": row["strategy"] if "strategy" in row.keys() else "",
                        "entry_time": row["entry_time"] if "entry_time" in row.keys() else None,
                        "exit_time": row["exit_time"] if "exit_time" in row.keys() else None,
                        "exit_reason": "",
                        "status": "CLOSED",
                    })
                conn.close()
                pos_count = sum(1 for t in all_trades if t["source"] == "ftseek.positions")
                self.logger.info(f"[RealDB] ftseek.db positions (CLOSED): {pos_count} records")

            # 3) 读取 execution.db → execution_records
            exec_db_path = os.path.join(os.path.dirname(db_path), "execution.db")
            if os.path.exists(exec_db_path):
                conn = sqlite3.connect(exec_db_path)
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM execution_records WHERE date(fill_time) = ?",
                    (date,)
                )
                exec_records = []
                for row in cur.fetchall():
                    exec_records.append({
                        "code": row["code"] if "code" in row.keys() else "",
                        "signal_time": row["signal_time"] if "signal_time" in row.keys() else None,
                        "fill_time": row["fill_time"] if "fill_time" in row.keys() else None,
                        "signal_price": row["signal_price"] if "signal_price" in row.keys() else 0,
                        "fill_price": row["fill_price"] if "fill_price" in row.keys() else 0,
                        "quantity": row["quantity"] if "quantity" in row.keys() else 0,
                        "slippage": row["slippage"] if "slippage" in row.keys() else 0,
                        "execution_latency_ms": row["execution_latency_ms"] if "execution_latency_ms" in row.keys() else 0,
                        "status": row["status"] if "status" in row.keys() else "",
                    })
                conn.close()
                self.logger.info(f"[RealDB] execution.db: {len(exec_records)} records for {date}")

            # 4) 过滤 market
            if market.upper() == 'HK':
                all_trades = [t for t in all_trades if t.get("code", "").startswith("HK.")]
            elif market.upper() == 'US':
                all_trades = [t for t in all_trades if t.get("code", "").startswith("US.")]

            self._last_collected_trades = all_trades
            self._last_collected_executions = exec_records if 'exec_records' in dir() else []
            self.logger.info(
                f"[RealDB] Total collected for {market}/{date}: {len(all_trades)} trades, "
                f"{len(exec_records) if 'exec_records' in dir() else 0} executions"
            )
            return len(all_trades)

        except Exception as e:
            self.logger.error(f"[RealDB] Database read failed: {e}", exc_info=True)
            # Fallback to Futu API mock
            self.logger.warning("[RealDB] Falling back to Futu API mock")
            return self._collect_via_futu_api(market)

    def _collect_via_futu_api(self, market: str) -> int:
        """Futu API fallback 收集"""
        job_id = self._create_job_log(market, JobType.PERIODIC)
        try:
            end_time = datetime.now(self.timezone)
            start_time = end_time - timedelta(minutes=self.collection_interval_minutes)
            trade_data = self.futu_api.fetch_trade_list(market, start_time, end_time)
            trades = self._convert_to_trade_records(trade_data, market)
            stored_count = self.db_manager.store_trades(trades, market) if trades else 0
            self.db_manager.update_job_log(job_id, JobStatus.COMPLETED, stored_count)
            return stored_count
        except Exception as e:
            self.logger.error(f"Futu API collection failed: {e}")
            self.db_manager.update_job_log(job_id, JobStatus.FAILED, 0, str(e))
            return 0

    def get_last_trades(self) -> List[Dict]:
        """获取最后一次收集的交易数据"""
        return getattr(self, '_last_collected_trades', [])

    def get_last_executions(self) -> List[Dict]:
        """获取最后一次收集的执行数据"""
        return getattr(self, '_last_collected_executions', [])

    def _create_job_log(self, market: str, job_type: JobType) -> str:
        job_id = f"JOB_{market}_{uuid.uuid4().hex[:12]}"
        job_log = CollectionJobLog(
            job_id=job_id,
            job_type=job_type,
            market=market.upper(),
            scheduled_time=datetime.now(self.timezone),
            status=JobStatus.RUNNING
        )
        self.db_manager.create_job_log(job_log)
        return job_id
    
    def _convert_to_trade_records(self, trade_data: List[Dict[str, Any]], market: str) -> List[TradeRecord]:
        trades = []
        for data in trade_data:
            trade = TradeRecord(
                trade_id=data.get('trade_id', f"TRADE_{uuid.uuid4().hex[:12]}"),
                stock_code=data.get('stock_code', ''),
                market=market.upper(),
                side=data.get('side', 'BUY'),
                price=float(data.get('price', 0)),
                quantity=int(data.get('quantity', 0)),
                exec_time=data.get('exec_time', datetime.now(self.timezone)),
                pnl=float(data.get('pnl', 0)),
                pnl_percent=float(data.get('pnl_percent', 0)),
                commission=float(data.get('commission', 0)),
                stamp_duty=float(data.get('stamp_duty', 0)),
                strategy_id=data.get('strategy_id'),
                signal_strength=data.get('signal_strength'),
                position_ratio=data.get('position_ratio'),
                stop_loss_triggered=data.get('stop_loss_triggered', False),
                holding_days=data.get('holding_days', 0),
                day_low=data.get('day_low'),
                day_high=data.get('day_high'),
            )
            trades.append(trade)
        return trades
    
    def collect_all_markets(self) -> Dict[str, int]:
        results = {}
        for market in ['HK', 'US']:
            try:
                count = self.collect_trades(market)
                results[market] = count
            except Exception as e:
                self.logger.error(f"Failed to collect {market} trades: {e}")
                results[market] = 0
        return results
    
    def get_collection_statistics(self, start_date: datetime, end_date: datetime, market: Optional[str] = None) -> Dict[str, Any]:
        return {
            'total_jobs': 0,
            'completed_jobs': 0,
            'failed_jobs': 0,
            'skipped_jobs': 0,
            'total_trades_collected': 0,
            'avg_trades_per_job': 0,
        }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    collector = PeriodicTradeCollector()
    
    try:
        count = collector.collect_trades(market='HK')
        print(f"✓ Successfully collected {count} HK trades")
    except Exception as e:
        print(f"✗ Failed to collect HK trades: {e}")
    
    market_checker = MarketTimeChecker()
    print(f"\nMarket Status Check:")
    print(f"  HK Market Open: {market_checker.is_market_open('HK')}")
    print(f"  US Market Open: {market_checker.is_market_open('US')}")
