"""
宏观经济日历 (US Economic Calendar)

职责:
1. 追踪影响美股的重大宏观事件 (FOMC/CPI/NFP 等)
2. 提供禁止开仓时间窗口 (事件前后高波动期)
3. 提供格式化输出供 LLM prompt 注入

设计决策:
- 内嵌已知事件日期, 不依赖外部 API (FOMC 日期提前一年公布, CPI/NFP 遵循固定日程)
- 零 API 调用成本, 无速率限制, 100% 可靠
- 聚焦于导致 VIX 飙升并使技术分析失效的事件

事件日期来源:
- FOMC: federalreserve.gov (联邦储备委员会官网)
- CPI/PPI/NFP: bls.gov (劳工统计局)
- GDP: bea.gov (经济分析局)
- ISM PMI: ismworld.org

作者: FTSeek-US Team
版本: 1.0 (2026-06-12)
"""

import logging
from datetime import datetime, date, timedelta
from typing import Dict, List

import pytz

logger = logging.getLogger("ftseek.data.economic_calendar")

ET = pytz.timezone("America/New_York")


# ============================================================================
#  2026 US Market Holidays (NYSE 休市日)
# ============================================================================

US_MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed, Jul 4 is Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}


# ============================================================================
#  2026 经济事件日历 (内嵌数据)
# ============================================================================
# 格式: "YYYY-MM-DD" -> List[event_dict]
# 每个 event: {time, name, full_name, impact, sector_impact}

ECONOMIC_EVENTS_2026: Dict[str, List[Dict]] = {}


def _register(date_str: str, time_str: str, name: str, full_name: str,
              impact: str, sector_impact: str):
    """注册一个经济事件到全局字典"""
    event = {
        "date": date_str,
        "time": time_str,
        "name": name,
        "full_name": full_name,
        "impact": impact,
        "sector_impact": sector_impact,
    }
    ECONOMIC_EVENTS_2026.setdefault(date_str, []).append(event)


# --- FOMC 利率决议 (HIGH) ---
# 会议第二天 (周三) 14:00 ET 公布利率决议, 14:30 ET 新闻发布会
# 日期来源: federalreserve.gov/monetarypolicy/fomccalendars.htm
for _d in ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
           "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]:
    _register(_d, "14:00", "FOMC", "FOMC 利率决议", "HIGH", "全市场")

# --- CPI 消费者价格指数 (HIGH) ---
# BLS 发布, 通常 8:30 ET
# 日期来源: bls.gov/schedule/news_release/cpi.htm
for _d in ["2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
           "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
           "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"]:
    _register(_d, "08:30", "CPI", "消费者价格指数 (CPI)", "HIGH", "全市场")

# --- NFP 非农就业 (HIGH) ---
# 每月第一个周五, 8:30 ET (来源: BLS empsit.htm)
for _d in ["2026-01-02", "2026-02-06", "2026-03-06", "2026-04-03",
           "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
           "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"]:
    _register(_d, "08:30", "NFP", "非农就业数据 (NFP)", "HIGH", "全市场")

# --- PPI 生产者价格指数 (MEDIUM) ---
# 通常 CPI 前后 1-2 天, 8:30 ET
# 日期来源: bls.gov/ppi
for _d in ["2026-01-14", "2026-02-27", "2026-03-18", "2026-04-14",
           "2026-05-13", "2026-06-11", "2026-07-15", "2026-08-13",
           "2026-09-11", "2026-10-14", "2026-11-13", "2026-12-11"]:
    _register(_d, "08:30", "PPI", "生产者价格指数 (PPI)", "MEDIUM", "全市场")

# --- GDP 季度报告 (MEDIUM) ---
# BEA 发布, 8:30 ET
_gdp_entries = {
    "2026-01-29": "Q4 GDP 初值",
    "2026-02-26": "Q4 GDP 修正值",
    "2026-04-30": "Q1 GDP 初值",
    "2026-05-28": "Q1 GDP 修正值",
    "2026-07-30": "Q2 GDP 初值",
    "2026-08-27": "Q2 GDP 修正值",
    "2026-10-29": "Q3 GDP 初值",
    "2026-11-25": "Q3 GDP 修正值",
}
for _d, _label in _gdp_entries.items():
    _register(_d, "08:30", "GDP", _label, "MEDIUM", "全市场")

# --- ISM 制造业 PMI (MEDIUM) ---
# 每月第一个工作日, 10:00 ET
for _d in ["2026-01-02", "2026-02-02", "2026-03-02", "2026-04-01",
           "2026-05-01", "2026-06-01", "2026-07-01", "2026-08-03",
           "2026-09-01", "2026-10-01", "2026-11-02", "2026-12-01"]:
    _register(_d, "10:00", "ISM_MFG", "ISM 制造业 PMI", "MEDIUM", "制造业板块")

# --- ISM 服务业 PMI (MEDIUM) ---
# 每月第三个工作日, 10:00 ET
for _d in ["2026-01-07", "2026-02-04", "2026-03-05", "2026-04-03",
           "2026-05-05", "2026-06-03", "2026-07-03", "2026-08-05",
           "2026-09-03", "2026-10-05", "2026-11-04", "2026-12-03"]:
    _register(_d, "10:00", "ISM_SVC", "ISM 服务业 PMI", "MEDIUM", "服务业板块")

# --- Retail Sales 零售销售 (MEDIUM) ---
# 约每月15日, 8:30 ET
for _d in ["2026-01-15", "2026-02-13", "2026-03-16", "2026-04-15",
           "2026-05-14", "2026-06-15", "2026-07-15", "2026-08-14",
           "2026-09-15", "2026-10-15", "2026-11-16", "2026-12-15"]:
    _register(_d, "08:30", "RETAIL", "零售销售数据", "MEDIUM", "消费板块")


# ============================================================================
#  辅助函数
# ============================================================================

def _parse_time(time_str: str) -> timedelta:
    """将 'HH:MM' 字符串转为 timedelta"""
    h, m = time_str.split(":")
    return timedelta(hours=int(h), minutes=int(m))


def _format_time(td: timedelta) -> str:
    """将 timedelta 转为 'HH:MM' 字符串, 钳位到 [00:00, 23:59]"""
    total_seconds = int(td.total_seconds())
    total_seconds = max(0, min(total_seconds, 86399))  # 钳位 0 ~ 23:59:59
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    return f"{h:02d}:{m:02d}"


# ============================================================================
#  EconomicCalendar 类
# ============================================================================

class EconomicCalendar:
    """
    经济日历 — 追踪影响美股的重大宏观事件

    设计: 内嵌已知事件日期 (FOMC/CPI/NFP 等), 不依赖外部 API
    事件日期来源: Federal Reserve 官网 (FOMC), BLS (CPI/NFP), BEA (GDP)
    """

    def __init__(self):
        self._cache = None
        self._cache_time = None
        self._cache_ttl = 86400  # 24 hours (events rarely change)

    # ------------------------------------------------------------------
    #  内部工具
    # ------------------------------------------------------------------

    def _now_et(self) -> datetime:
        """当前 Eastern Time 时间"""
        return datetime.now(ET)

    def _today_et(self) -> date:
        """当前 Eastern Time 日期"""
        return self._now_et().date()

    @staticmethod
    def _is_trading_day(d: date) -> bool:
        """
        判断给定日期是否为交易日

        交易日 = 周一至周五 且 不在 NYSE 休市日列表中
        """
        if d.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        if d in US_MARKET_HOLIDAYS_2026:
            return False
        return True

    # ------------------------------------------------------------------
    #  公开 API
    # ------------------------------------------------------------------

    def get_todays_events(self, date_str: str = None) -> List[Dict]:
        """
        获取指定日期(默认今日 ET)的经济事件列表

        Args:
            date_str: 'YYYY-MM-DD' 格式日期, 默认今日 ET.
                      (premarket_scanner 按扫描日期查, 传入; 其他场景默认今日.)

        每个事件:
        {
            "date": "2026-06-10",
            "time": "08:30",           # ET time
            "name": "CPI",
            "full_name": "消费者价格指数 (CPI)",
            "impact": "HIGH",
            "sector_impact": "全市场",
        }

        Returns:
            指定日期事件列表, 非交易日返回空列表
        """
        if date_str:
            try:
                target = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                target = self._today_et()
        else:
            target = self._today_et()
        if not self._is_trading_day(target):
            return []

        events = ECONOMIC_EVENTS_2026.get(target.strftime("%Y-%m-%d"), [])
        return events

    def get_upcoming_events(self, days: int = 7) -> List[Dict]:
        """
        获取未来 N 天内的经济事件 (含今日)

        Args:
            days: 查询天数范围 (默认 7)

        Returns:
            事件列表, 按日期和时间排序
        """
        today = self._today_et()
        end_date = today + timedelta(days=days)
        result = []

        current = today
        while current <= end_date:
            if self._is_trading_day(current):
                date_str = current.strftime("%Y-%m-%d")
                events = ECONOMIC_EVENTS_2026.get(date_str, [])
                result.extend(events)
            current += timedelta(days=1)

        # 按日期 + 时间排序
        result.sort(key=lambda e: (e["date"], e["time"]))
        return result

    def get_no_trade_windows(self, date_str: str = None) -> List[Dict]:
        """
        获取指定日期(默认今日 ET)禁止开仓的时间窗口

        Args:
            date_str: 'YYYY-MM-DD' 格式日期, 默认今日 ET.

        HIGH 影响事件: T-15min 到 T+30min
        MEDIUM 影响事件: T-10min 到 T+15min

        Returns:
            [
                {
                    "start": "08:15",
                    "end": "09:00",
                    "reason": "CPI 数据发布 (08:30 ET)",
                    "impact": "HIGH",
                    "event": "CPI"
                },
            ]
        """
        today_events = self.get_todays_events(date_str)
        if not today_events:
            return []

        windows = []
        for event in today_events:
            event_td = _parse_time(event["time"])

            if event["impact"] == "HIGH":
                start_td = event_td - timedelta(minutes=15)
                end_td = event_td + timedelta(minutes=30)
            else:
                # MEDIUM
                start_td = event_td - timedelta(minutes=10)
                end_td = event_td + timedelta(minutes=15)

            windows.append({
                "start": _format_time(max(start_td, timedelta(0))),
                "end": _format_time(end_td),
                "reason": f"{event['name']} 数据发布 ({event['time']} ET)",
                "impact": event["impact"],
                "event": event["name"],
            })

        # 按开始时间排序
        windows.sort(key=lambda w: w["start"])
        return windows

    # ------------------------------------------------------------------
    #  LLM 格式化
    # ------------------------------------------------------------------

    def format_for_llm(self, days: int = 7) -> str:
        """
        格式化为 LLM prompt 注入文本

        示例输出:

        【经济日历】(本周重要宏观事件)

        ⚠️ 高影响事件:
          • 2026-06-10 08:30 ET — CPI 消费者价格指数 | 影响: 全市场
          • 2026-06-17 14:00 ET — FOMC 利率决议 | 影响: 全市场

        📊 中等影响:
          • 2026-06-01 10:00 ET — ISM 制造业 PMI | 影响: 制造业板块

        (如果本周无事件): 📅 本周无重大经济数据发布。
        """
        events = self.get_upcoming_events(days=days)

        if not events:
            return "📅 本周无重大经济数据发布。"

        high_events = [e for e in events if e["impact"] == "HIGH"]
        medium_events = [e for e in events if e["impact"] == "MEDIUM"]

        lines = ["【经济日历】(本周重要宏观事件)", ""]

        if high_events:
            lines.append("⚠️ 高影响事件:")
            for e in high_events:
                lines.append(
                    f"  • {e['date']} {e['time']} ET — {e['name']} {e['full_name']}"
                    f" | 影响: {e['sector_impact']}"
                )
            lines.append("")

        if medium_events:
            lines.append("📊 中等影响:")
            for e in medium_events:
                lines.append(
                    f"  • {e['date']} {e['time']} ET — {e['full_name']}"
                    f" | 影响: {e['sector_impact']}"
                )
            lines.append("")

        return "\n".join(lines)

    def format_no_trade_windows_for_llm(self) -> str:
        """
        格式化今日禁止开仓窗口为 LLM 约束文本

        示例输出:

        🚫 禁止开仓窗口:
          • 08:15-09:00 ET: CPI 数据发布 (HIGH 影响)
          • 13:45-14:30 ET: FOMC 利率决议 (HIGH 影响)

        (如果今日无窗口): (无)
        """
        windows = self.get_no_trade_windows()

        if not windows:
            return ""

        lines = ["🚫 禁止开仓窗口:"]
        for w in windows:
            lines.append(
                f"  • {w['start']}-{w['end']} ET: {w['reason']} ({w['impact']} 影响)"
            )

        return "\n".join(lines)


# ============================================================================
#  CLI 测试入口
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    calendar = EconomicCalendar()

    print("=" * 60)
    print("今日事件")
    print("=" * 60)
    today_events = calendar.get_todays_events()
    if today_events:
        for e in today_events:
            print(f"  {e['time']} ET | {e['name']} | {e['full_name']} | {e['impact']}")
    else:
        print("  (今日无经济事件)")

    print()
    print("=" * 60)
    print("禁止开仓窗口 (今日)")
    print("=" * 60)
    windows = calendar.get_no_trade_windows()
    if windows:
        for w in windows:
            print(f"  {w['start']}-{w['end']} ET: {w['reason']} ({w['impact']})")
    else:
        print("  (无)")

    print()
    print("=" * 60)
    print("LLM Prompt: 本周经济日历")
    print("=" * 60)
    print(calendar.format_for_llm())

    print()
    print("=" * 60)
    print("LLM Prompt: 禁止开仓窗口")
    print("=" * 60)
    result = calendar.format_no_trade_windows_for_llm()
    print(result if result else "  (无)")
