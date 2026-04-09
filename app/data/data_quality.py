"""
数据质量监控模块

功能:
- K 线数据完整性检查
- 数据缺失检测
- 异常值检测
- 数据修复工具
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import json

logger = logging.getLogger("ftseek.data.data_quality")
logger.propagate = False  # 防止日志重复


@dataclass
class QualityReport:
    """质量报告"""
    stock_code: str
    check_date: str
    total_records: int
    expected_records: int
    missing_records: int
    completeness: float  # 完整率
    anomalies: List[Dict]
    status: str  # good/warning/bad


class DataQualityChecker:
    """数据质量检查器"""
    
    def __init__(self):
        # 港股交易时间配置
        self.trading_hours = {
            "morning_start": "09:30",
            "morning_end": "12:00",
            "afternoon_start": "13:00",
            "afternoon_end": "16:00",
        }
        
        # 1 分钟 K 线预期数量 (每交易日 240 根)
        self.expected_bars_per_day = 240
        
        logger.info("数据质量检查器已初始化")
    
    def check_kline_completeness(self,
                                  stock_code: str,
                                  kline_data: List[Dict],
                                  days: int = 5) -> QualityReport:
        """检查 K 线完整性
        
        Args:
            stock_code: 股票代码
            kline_data: K 线数据 [{time, open, high, low, close, volume}]
            days: 检查天数
        
        Returns:
            QualityReport 质量报告
        """
        if not kline_data:
            return QualityReport(
                stock_code=stock_code,
                check_date=datetime.now().strftime("%Y-%m-%d"),
                total_records=0,
                expected_records=0,
                missing_records=0,
                completeness=0.0,
                anomalies=[],
                status="bad",
            )
        
        # 按日期分组
        daily_bars = self._group_by_date(kline_data)
        
        total_records = len(kline_data)
        expected_records = len(daily_bars) * self.expected_bars_per_day
        missing_records = 0
        anomalies = []
        
        for date, bars in daily_bars.items():
            expected = self.expected_bars_per_day
            actual = len(bars)
            
            if actual < expected * 0.5:  # 缺失超过 50%
                anomalies.append({
                    'type': 'missing_data',
                    'date': date,
                    'expected': expected,
                    'actual': actual,
                    'missing_rate': 1 - actual/expected,
                })
                missing_records += expected - actual
        
        # 检查异常值
        anomalies.extend(self._detect_anomalies(kline_data))
        
        # 计算完整率
        completeness = (total_records / expected_records * 100) if expected_records > 0 else 0
        
        # 确定状态
        if completeness >= 95 and len(anomalies) == 0:
            status = "good"
        elif completeness >= 80:
            status = "warning"
        else:
            status = "bad"
        
        return QualityReport(
            stock_code=stock_code,
            check_date=datetime.now().strftime("%Y-%m-%d"),
            total_records=total_records,
            expected_records=expected_records,
            missing_records=missing_records,
            completeness=completeness,
            anomalies=anomalies,
            status=status,
        )
    
    def _group_by_date(self, data: List[Dict]) -> Dict[str, List[Dict]]:
        """按日期分组"""
        daily = {}
        for row in data:
            # 从 time 提取日期
            time_str = row.get('time', '')
            if ' ' in time_str:
                date = time_str.split(' ')[0]
            else:
                date = time_str[:10]
            
            if date not in daily:
                daily[date] = []
            daily[date].append(row)
        return daily
    
    def _detect_anomalies(self, kline_data: List[Dict]) -> List[Dict]:
        """检测异常值"""
        anomalies = []
        
        if len(kline_data) < 2:
            return anomalies
        
        for i in range(1, len(kline_data)):
            prev = kline_data[i-1]
            curr = kline_data[i]
            
            prev_close = prev.get('close', 0)
            curr_open = curr.get('open', 0)
            curr_high = curr.get('high', 0)
            curr_low = curr.get('low', 0)
            curr_close = curr.get('close', 0)
            curr_volume = curr.get('volume', 0)
            
            # 检查跳空
            if prev_close > 0:
                gap = abs(curr_open - prev_close) / prev_close
                if gap > 0.1:  # 跳空超过 10%
                    anomalies.append({
                        'type': 'price_gap',
                        'time': curr.get('time', ''),
                        'gap': gap,
                        'prev_close': prev_close,
                        'curr_open': curr_open,
                    })
            
            # 检查异常成交量
            avg_volume = sum(r.get('volume', 0) for r in kline_data[:min(20, len(kline_data))]) / min(20, len(kline_data))
            if avg_volume > 0 and curr_volume > avg_volume * 10:
                anomalies.append({
                    'type': 'volume_spike',
                    'time': curr.get('time', ''),
                    'volume': curr_volume,
                    'avg_volume': avg_volume,
                    'ratio': curr_volume / avg_volume,
                })
            
            # 检查价格异常
            if curr_high > 0 and curr_low > 0:
                intraday_range = (curr_high - curr_low) / curr_low
                if intraday_range > 0.2:  # 日内波动超过 20%
                    anomalies.append({
                        'type': 'high_volatility',
                        'time': curr.get('time', ''),
                        'high': curr_high,
                        'low': curr_low,
                        'range': intraday_range,
                    })
        
        return anomalies
    
    def check_data_freshness(self,
                              stock_code: str,
                              last_update: datetime) -> Dict:
        """检查数据新鲜度
        
        Args:
            stock_code: 股票代码
            last_update: 最后更新时间
        
        Returns:
            新鲜度检查结果
        """
        now = datetime.now()
        age = now - last_update
        
        # 交易时间内，数据应该每分钟更新
        is_trading_time = self._is_trading_time(now)
        
        if is_trading_time:
            if age.total_seconds() > 120:  # 超过 2 分钟
                status = "stale"
            elif age.total_seconds() > 60:
                status = "warning"
            else:
                status = "fresh"
        else:
            # 非交易时间，检查是否是最近一次收盘后
            status = "ok"
        
        return {
            'stock_code': stock_code,
            'last_update': last_update.isoformat(),
            'age_seconds': age.total_seconds(),
            'is_trading_time': is_trading_time,
            'status': status,
        }
    
    def _is_trading_time(self, dt: datetime) -> bool:
        """检查是否交易时间"""
        time_str = dt.strftime("%H:%M")
        
        morning = self.trading_hours["morning_start"] <= time_str <= self.trading_hours["morning_end"]
        afternoon = self.trading_hours["afternoon_start"] <= time_str <= self.trading_hours["afternoon_end"]
        
        # 排除周末
        is_weekday = dt.weekday() < 5
        
        return is_weekday and (morning or afternoon)
    
    def generate_daily_report(self,
                               stock_codes: List[str],
                               kline_data_map: Dict[str, List[Dict]]) -> Dict:
        """生成每日质量报告
        
        Args:
            stock_codes: 股票代码列表
            kline_data_map: {stock_code: [kline_data]}
        
        Returns:
            综合报告
        """
        reports = []
        
        for stock_code in stock_codes:
            kline_data = kline_data_map.get(stock_code, [])
            report = self.check_kline_completeness(stock_code, kline_data)
            reports.append(report)
        
        # 汇总统计
        total_stocks = len(reports)
        good_count = sum(1 for r in reports if r.status == "good")
        warning_count = sum(1 for r in reports if r.status == "warning")
        bad_count = sum(1 for r in reports if r.status == "bad")
        
        avg_completeness = sum(r.completeness for r in reports) / total_stocks if total_stocks > 0 else 0
        
        return {
            'date': datetime.now().strftime("%Y-%m-%d"),
            'total_stocks': total_stocks,
            'good': good_count,
            'warning': warning_count,
            'bad': bad_count,
            'avg_completeness': avg_completeness,
            'reports': [
                {
                    'stock_code': r.stock_code,
                    'completeness': r.completeness,
                    'status': r.status,
                    'anomalies_count': len(r.anomalies),
                }
                for r in reports
            ],
        }
    
    def export_report(self, report: Dict, filepath: str):
        """导出报告"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        
        logger.info(f"质量报告已导出：{filepath}")


# 全局单例
_quality_checker = None

def get_quality_checker() -> DataQualityChecker:
    """获取质量检查器单例"""
    global _quality_checker
    if _quality_checker is None:
        _quality_checker = DataQualityChecker()
    return _quality_checker
