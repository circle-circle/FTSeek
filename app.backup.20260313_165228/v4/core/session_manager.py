"""
MarketSessionManager (市场时段管理器)

管理港股/美股交易时段，确保只在交易时间内执行

港股 (0700.HK):
- 上午：09:30-12:00 HKT
- 下午：13:00-16:00 HKT
- 时区：Asia/Shanghai (HKT=GMT+8)

美股 (MU):
- 常规：21:30-04:00 EST (跨日)
- 时区：America/New_York (EST=GMT-5)
"""
from datetime import datetime, time, timedelta
from typing import Optional, Dict

try:
    import pytz
    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False


class MarketSessionManager:
    """市场时段管理器"""
    
    # 市场配置
    MARKET_CONFIG = {
        '0700.HK': {
            'timezone': 'Asia/Shanghai',
            'sessions': [
                {'start': '09:30', 'end': '12:00'},  # 上午
                {'start': '13:00', 'end': '16:00'},  # 下午
            ],
            'name': '港股'
        },
        'MU': {
            'timezone': 'America/New_York',
            'sessions': [
                {'start': '21:30', 'end': '04:00'},  # 跨日时段
            ],
            'name': '美股'
        }
    }
    
    def __init__(self):
        """初始化市场时段管理器"""
        if HAS_PYTZ:
            self.hk_tz = pytz.timezone('Asia/Shanghai')
            self.us_tz = pytz.timezone('America/New_York')
        else:
            # 简单时区偏移 (不处理 DST)
            self.hk_tz = None
            self.us_tz = None
    
    def _get_timezone(self, code: str):
        """获取市场对应的时区"""
        if code not in self.MARKET_CONFIG:
            raise ValueError(f"未知市场代码：{code}")
        
        tz_name = self.MARKET_CONFIG[code]['timezone']
        
        if HAS_PYTZ:
            return pytz.timezone(tz_name)
        else:
            # 简单处理：返回时区名称和偏移
            if tz_name == 'Asia/Shanghai':
                from datetime import timezone
                return timezone(timedelta(hours=8)), 'Asia/Shanghai'
            elif tz_name == 'America/New_York':
                from datetime import timezone
                return timezone(timedelta(hours=-5)), 'America/New_York'
            else:
                from datetime import timezone
                return timezone.utc, 'UTC'
    
    def _parse_time(self, time_str: str) -> time:
        """解析时间字符串为 time 对象"""
        parts = time_str.split(':')
        return time(int(parts[0]), int(parts[1]))
    
    def _is_weekend(self, dt: datetime) -> bool:
        """检查是否为周末"""
        return dt.weekday() >= 5  # 5=周六，6=周日
    
    def _time_in_range(self, t: time, start: time, end: time) -> bool:
        """检查时间是否在范围内 (支持跨日)"""
        if start <= end:
            # 不跨日：start <= t < end (end 是开区间)
            return start <= t < end
        else:
            # 跨日情况 (如 21:30-04:00)
            # t >= start 或 t < end
            return t >= start or t < end
    
    def is_trading_session(self, code: str, timestamp: datetime = None) -> bool:
        """
        检查指定时间是否为交易时段
        
        Args:
            code: 股票代码 (0700.HK 或 MU)
            timestamp: 检查的时间点，None 表示当前时间 (假设输入为市场本地时间)
        
        Returns:
            bool: 是否为交易时段
        """
        if code not in self.MARKET_CONFIG:
            raise ValueError(f"未知市场代码：{code}")
        
        if timestamp is None:
            timestamp = datetime.now()
        
        # 周末非交易
        if self._is_weekend(timestamp):
            return False
        
        config = self.MARKET_CONFIG[code]
        
        # 假设输入 timestamp 已经是市场本地时间 (naive datetime)
        local_t = timestamp.time()
        
        # 检查是否在任何交易时段内
        for session in config['sessions']:
            start_t = self._parse_time(session['start'])
            end_t = self._parse_time(session['end'])
            
            if self._time_in_range(local_t, start_t, end_t):
                return True
        
        return False
    
    def get_session_type(self, code: str, timestamp: datetime = None) -> str:
        """
        获取交易时段类型
        
        Args:
            code: 股票代码
            timestamp: 检查的时间点，None 表示当前时间
        
        Returns:
            str: PRE (盘前), REGULAR (常规交易), POST (盘后), CLOSED (收盘)
        """
        if code not in self.MARKET_CONFIG:
            raise ValueError(f"未知市场代码：{code}")
        
        if timestamp is None:
            timestamp = datetime.now()
        
        # 周末直接返回 CLOSED
        if self._is_weekend(timestamp):
            return "CLOSED"
        
        if self.is_trading_session(code, timestamp):
            return "REGULAR"
        else:
            return "CLOSED"
    
    def next_session_start(self, code: str, timestamp: datetime = None) -> datetime:
        """
        获取下一个交易时段开始时间
        
        Args:
            code: 股票代码
            timestamp: 当前时间，None 表示当前时间 (假设输入为市场本地时间)
        
        Returns:
            datetime: 下一个交易时段开始时间 (与输入相同时区)
        """
        if code not in self.MARKET_CONFIG:
            raise ValueError(f"未知市场代码：{code}")
        
        if timestamp is None:
            timestamp = datetime.now()
        
        config = self.MARKET_CONFIG[code]
        
        # 假设输入 timestamp 已经是市场本地时间 (naive datetime)
        local_time = timestamp
        local_t = local_time.time()
        
        # 检查今天是否还有未开始的交易时段
        for session in config['sessions']:
            start_t = self._parse_time(session['start'])
            
            # 创建今天的候选时间
            candidate = local_time.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
            
            # 如果候选时间还没到，且不是周末，返回这个时间
            if candidate > local_time:
                if not self._is_weekend(candidate):
                    return candidate
        
        # 今天没有更多交易时段了，找下一个交易日
        next_day = local_time + timedelta(days=1)
        
        # 跳过周末
        while self._is_weekend(next_day):
            next_day += timedelta(days=1)
        
        # 返回下一个交易日的第一个交易时段
        first_session = config['sessions'][0]
        start_t = self._parse_time(first_session['start'])
        candidate = next_day.replace(hour=start_t.hour, minute=start_t.minute, second=0, microsecond=0)
        
        return candidate
    
    def get_market_hours(self, code: str) -> dict:
        """
        获取市场交易时间配置
        
        Args:
            code: 股票代码
        
        Returns:
            dict: 市场时间配置
        """
        if code not in self.MARKET_CONFIG:
            raise ValueError(f"未知市场代码：{code}")
        
        config = self.MARKET_CONFIG[code]
        result = {
            'timezone': config['timezone'],
            'name': config['name']
        }
        
        # 根据市场类型返回不同的格式
        if code == '0700.HK':
            result['morning'] = '09:30-12:00'
            result['afternoon'] = '13:00-16:00'
        elif code == 'MU':
            result['regular'] = '21:30-04:00'
        
        return result
