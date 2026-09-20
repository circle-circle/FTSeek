"""
美股财报日历数据源 (US Earnings Calendar)

职责:
1. 通过 Alpha Vantage 免费 API 获取本周/下周财报日历
2. 过滤出 Watchlist 中的重要股票
3. 提供格式化输出供 LLM prompt 使用

数据源: Alpha Vantage Earnings Calendar API (免费版 500 次/天)
API Key: 通过环境变量 ALPHAVANTAGE_API_KEY 配置
         未配置时使用 demo key (有限制，仅用于测试)

作者: FTSeek-US Team
版本: 1.0 (2026-04-23)
"""

import os
import csv
import io
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from pathlib import Path

from app.utils.log_sanitize import scrub_secrets
from app.utils.timezone import now_et

logger = logging.getLogger("ftseek.us_pipeline.earnings_calendar")


# ============================================================================
#  行业差异化 ATR 倍数映射表 (Phase 2 新增)
# ============================================================================
# 不同行业波动特性差异显著，财报期间需要差异化止损宽度
# 数据来源: 历史波动率统计分析 + 行业 ETF 特性
# 基准: Tech 行业 2.5x ATR 为标准起点
SECTOR_ATR_MULTIPLIERS = {
    # 科技/半导体 — 高波动，财报后 IV Crush 明显
    "Tech": {"base": 3.0, "earnings": 4.0, "flash_exit": 1.2},
    "Tech/Semiconductor": {"base": 3.5, "earnings": 4.5, "flash_exit": 1.0},
    
    # 消费周期 — 中等波动，财报后反应温和
    "Consumer Cyclical": {"base": 2.5, "earnings": 3.5, "flash_exit": 1.5},
    "Consumer Cyclical/Auto": {"base": 3.0, "earnings": 4.0, "flash_exit": 1.2},
    
    # 消费防御 — 低波动，财报后反应稳定
    "Consumer Defensive": {"base": 2.0, "earnings": 2.5, "flash_exit": 2.0},
    
    # 金融 — 财报日波动加剧
    "Financial": {"base": 2.5, "earnings": 3.5, "flash_exit": 1.5},
    
    # 医疗健康 — 波动大，但财报相对稳定
    "Healthcare": {"base": 2.5, "earnings": 3.0, "flash_exit": 1.5},
    
    # 能源 — 受大宗商品影响大
    "Energy": {"base": 3.0, "earnings": 4.0, "flash_exit": 1.0},
    
    # 通信 — 中等波动
    "Communication": {"base": 2.5, "earnings": 3.5, "flash_exit": 1.5},
    
    # 默认 (未知行业) — 保守配置
    "Unknown": {"base": 2.5, "earnings": 3.5, "flash_exit": 1.5},
}

# 获取行业 ATR 倍数的辅助函数
def get_sector_atr_multiplier(sector: str, is_earnings_period: bool = False) -> dict:
    """
    获取行业对应的 ATR 倍数配置
    
    Args:
        sector: 行业名称 (如 "Tech", "Financial")
        is_earnings_period: 是否处于财报期间 (财报日前后 3 天)
        
    Returns:
        {"base": X, "earnings": Y, "flash_exit": Z}
    """
    config = SECTOR_ATR_MULTIPLIERS.get(sector, SECTOR_ATR_MULTIPLIERS["Unknown"])
    return config


# ============================================================================
#  重要股票映射表 (P1-4: 动态从 watchlist_us.json 加载)
# ============================================================================
# 行业名称映射: watchlist sector -> earnings_calendar sector
_SECTOR_MAPPING = {
    "Technology": "Tech",
    "Semiconductors": "Tech/Semiconductor",
    "Consumer Cyclical": "Consumer Cyclical",
    "Consumer Defensive": "Consumer Defensive",
    "Financial Services": "Financial",
    "Healthcare": "Healthcare",
    "Energy": "Energy",
    "Communication Services": "Communication",
    "Industrials": "Consumer Cyclical",  # 工业用周期类代替
}

def _load_key_us_symbols() -> dict:
    """
    从 watchlist_us.json 动态加载重要股票映射表
    
    P1-4 修复: 消除硬编码的 KEY_US_SYMBOLS，与 watchlist 保持同步
    
    Returns:
        {"US.AAPL": ("AAPL", "Apple Inc", "Tech"), ...}
    """
    watchlist_path = Path(__file__).resolve().parent.parent.parent / "config" / "watchlist_us.json"
    result = {}
    
    try:
        if watchlist_path.exists():
            import json
            with open(watchlist_path, 'r') as f:
                data = json.load(f)
            
            for stock in data.get("us_stocks", []):
                code = stock.get("code", "")
                name = stock.get("name", "")
                raw_sector = stock.get("sector", "Unknown")
                # 映射行业名称到财报日历的格式
                sector = _SECTOR_MAPPING.get(raw_sector, "Unknown")
                # 从 Futu code 提取 ticker (US.AAPL -> AAPL)
                ticker = code.replace("US.", "") if code.startswith("US.") else code
                result[code] = (ticker, name, sector)
            
            if result:
                logger.info(f"[财报日历] 从 watchlist_us.json 加载 {len(result)} 只股票")
                return result
    except Exception as e:
        logger.warning(f"[财报日历] watchlist_us.json 加载失败: {e}")
    
    # Fallback: 使用静态列表 (向后兼容)
    logger.warning("[财报日历] 使用静态 KEY_US_SYMBOLS fallback")
    return _STATIC_KEY_US_SYMBOLS


# 静态 fallback (向后兼容，仅在 watchlist_us.json 不可用时使用)
_STATIC_KEY_US_SYMBOLS = {
    "US.AAPL": ("AAPL", "Apple Inc", "Tech"),
    "US.MSFT": ("MSFT", "Microsoft Corp", "Tech"),
    "US.GOOG": ("GOOG", "Alphabet Inc", "Tech"),
    "US.AMZN": ("AMZN", "Amazon.com Inc", "Consumer Cyclical"),
    "US.NVDA": ("NVDA", "NVIDIA Corp", "Tech/Semiconductor"),
    "US.TSLA": ("TSLA", "Tesla Inc", "Consumer Cyclical/Auto"),
    "US.META": ("META", "Meta Platforms", "Tech"),
    "US.NFLX": ("NFLX", "Netflix Inc", "Communication"),
    "US.AMD": ("AMD", "Advanced Micro Devices", "Tech/Semiconductor"),
    "US.BABA": ("BABA", "Alibaba Group", "Consumer Cyclical"),
    "US.JPM": ("JPM", "JPMorgan Chase", "Financial"),
    "US.V": ("V", "Visa Inc", "Financial"),
    "US.WMT": ("WMT", "Walmart Inc", "Consumer Defensive"),
    "US.PFE": ("PFE", "Pfizer Inc", "Healthcare"),
    "US.JNJ": ("JNJ", "Johnson & Johnson", "Healthcare"),
    "US.UNH": ("UNH", "UnitedHealth Group", "Healthcare"),
    "US.XOM": ("XOM", "Exxon Mobil", "Energy"),
    "US.BAC": ("BAC", "Bank of America", "Financial"),
    "US.INTC": ("INTC", "Intel Corp", "Tech/Semiconductor"),
    "US.QCOM": ("QCOM", "Qualcomm Inc", "Tech/Semiconductor"),
    "US.COST": ("COST", "Costco Wholesale", "Consumer Defensive"),
    "US.DIS": ("DIS", "Walt Disney", "Communication"),
    "US.ABBV": ("ABBV", "AbbVie Inc", "Healthcare"),
    "US.CRM": ("CRM", "Salesforce Inc", "Tech"),
    "US.CSCO": ("CSCO", "Cisco Systems", "Tech"),
    "US.PEP": ("PEP", "PepsiCo Inc", "Consumer Defensive"),
    "US.KO": ("KO", "Coca-Cola Co", "Consumer Defensive"),
    "US.LLY": ("LLY", "Eli Lilly", "Healthcare"),
    "US.MCD": ("MCD", "McDonald's Corp", "Consumer Cyclical"),
    "US.AVGO": ("AVGO", "Broadcom Inc", "Tech/Semiconductor"),
    "US.TSM": ("TSM", "TSMC", "Tech/Semiconductor"),
    "US.PDD": ("PDD", "PDD Holdings", "Consumer Cyclical"),
    "US.BIDU": ("BIDU", "Baidu Inc", "Tech"),
    "US.NIO": ("NIO", "NIO Inc", "Consumer Cyclical/Auto"),
    "US.XPEV": ("XPEV", "XPeng Inc", "Consumer Cyclical/Auto"),
    "US.LI": ("LI", "Li Auto Inc", "Consumer Cyclical/Auto"),
}

# 动态加载 (优先 watchlist_us.json，失败则 fallback 到静态表)
KEY_US_SYMBOLS = _load_key_us_symbols()


class EarningsCalendar:
    """
    美股财报日历查询器
    
    使用 Alpha Vantage 免费 API 获取财报日历数据。
    免费版限制: 500 次/天，每分钟 5 次。
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY", "demo")
        self.base_url = "https://www.alphavantage.co/query"
        self._cache = None
        self._cache_time = None
        self._cache_error = None
        self._cache_ttl_seconds = 3600  # 缓存 1 小时
        
    def _fetch_raw_calendar(self, horizon: str = "3month") -> str:
        """
        获取原始财报日历 CSV 数据
        
        Args:
            horizon: 时间范围 ('3month' 或 '6month')
            
        Returns:
            CSV 格式的字符串
        """
        params = {
            "function": "EARNINGS_CALENDAR",
            "horizon": horizon,
            "apikey": self.api_key,
        }
        
        # P0 修复 (2026-05-08): 增加重试机制和超时时间
        max_retries = 2
        for attempt in range(max_retries):
            try:
                timeout = 30 if attempt == 0 else 15  # 首次 30s，重试 15s
                resp = requests.get(self.base_url, params=params, timeout=timeout)
                if resp.status_code == 200:
                    return resp.text
                else:
                    logger.warning(f"Alpha Vantage 返回 HTTP {resp.status_code}: {resp.text[:200]}")
                    return ""
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    logger.warning(f"Alpha Vantage 请求超时 (尝试 {attempt + 1}/{max_retries})，重试中...")
                    import time
                    time.sleep(2)  # 重试前等待 2 秒
                else:
                    logger.error("Alpha Vantage 请求超时 (已达最大重试次数)")
                    return ""
            except Exception as e:
                # 日志脱敏: 异常文本含完整 URL, 而 apikey 在 query string
                logger.error(f"Alpha Vantage 请求异常: {scrub_secrets(e)}")
                return ""
    
    def _parse_csv(self, csv_text: str) -> List[Dict]:
        """解析 CSV 数据为字典列表"""
        if not csv_text or not csv_text.strip():
            return []
        
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            rows = []
            for row in reader:
                # 过滤掉无效行
                if row.get("symbol") and row.get("reportDate"):
                    rows.append(row)
            return rows
        except Exception as e:
            logger.error(f"CSV 解析失败: {e}")
            return []
    
    def get_earnings_calendar_with_status(
        self, horizon: str = "3month", force_refresh: bool = False
    ) -> Tuple[List[Dict], Optional[str]]:
        """
        获取财报日历及源状态 (带缓存)
        
        Args:
            horizon: 时间范围
            force_refresh: 强制刷新缓存
            
        Returns:
            (财报日历列表, 源错误)。双源皆空按源不可用处理，
            供 fail-closed 消费方区分断流与真实无事件。
        """
        # 检查缓存
        now = datetime.now()
        if (not force_refresh and 
            self._cache is not None and 
            self._cache_time is not None and
            (now - self._cache_time).total_seconds() < self._cache_ttl_seconds):
            logger.debug("使用财报日历缓存")
            return self._cache, self._cache_error
        
        logger.info(f"获取财报日历 (horizon={horizon})...")
        # 2026-08-29 源序反转 (用户裁"2直接做"): 富途原生升主源, AV 降备源。
        #   根因: G6 复核确认活门数据源是 AV 单点外部依赖 (配额/断流/付费墙风险),
        #   而富途原生 get_earnings_calendar (OpenD 本地, 无配额) 8/12 写成
        #   "AV 空才触发"的 fallback —— 恰最需要冗余时它不在。反转后:
        #   富途窗口 7 天恰好覆盖全部生产消费方 (days_ahead=7); AV 3month 保留为
        #   备源兼历史段补充。输出契约不变 (下游按 reportDate 自行过滤)。
        #   断流告警: 双源皆空 → WARNING 显式标记 (此前静默返回空列表, 门空转无人知
        #   —— 正是 G6 死表误判的同族失明)。
        rows = self._fetch_futu_calendar_primary()
        source_error = None
        if not rows:
            csv_text = self._fetch_raw_calendar(horizon)
            rows = self._parse_csv(csv_text)

        if not rows:
            source_error = "futu_and_av_empty"
            logger.warning(
                "[财报日历] ⚠️ 断流告警: 富途主源与 AV 备源均返回空 — "
                "财报禁建门/财报期风控将空转 (is_earnings_blackout 恒放行)。"
                "连续出现请查 OpenD 连通性与 AV key。")

        # 缓存结果
        self._cache = rows
        self._cache_time = now
        self._cache_error = source_error

        logger.info(f"获取到 {len(rows)} 条财报数据")
        return rows, source_error

    def get_earnings_calendar(self, horizon: str = "3month", force_refresh: bool = False) -> List[Dict]:
        """兼容旧契约: 只返回财报日历列表。"""
        return self.get_earnings_calendar_with_status(
            horizon=horizon, force_refresh=force_refresh
        )[0]
    
    def _fetch_futu_calendar_primary(self, days_ahead: int = 7, days_back: int = 2) -> List[Dict]:
        """
        富途原生财报日历 — 主源 (2026-08-29 由 fallback 升为主源)

        数据源: OpenQuoteContext.get_earnings_calendar (OpenD 本地, 无配额限制)
        窗口: API 硬限制单次 ≤7 天含端点 (实测 2026-08-29: 8 天即报
          "日期范围不能超过7天")。故拆两次调用并合并去重:
            过去段 [today-days_back, today] (覆盖 get_recently_reported(days_back≤2))
            未来段 [today, today+days_ahead-1] (覆盖全部生产消费方 days_ahead=7)
          主源非空时备源不触发, 窗口缺过去段会静默丢失"已发布"日历。

        Returns:
            与 _parse_csv 输出同构的列表:
            [{"symbol": "CSCO", "name": "思科", "reportDate": "2026-08-12",
              "timeOfTheDay": "post-market", "estimate": "0.8643",
              "currency": "USD", "source": "futu"}, ...]
            失败返回 [] (触发 AV 备源)。
        """
        import socket
        try:
            from futu import OpenQuoteContext, RET_OK
        except ImportError:
            logger.warning("[财报日历] futu SDK 不可用, 主源跳过")
            return []

        host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
        try:
            port = int(os.environ.get("FUTU_OPEND_PORT", "11111"))
        except (ValueError, TypeError):
            port = 11111

        # 连通性预检 (2s): OpenD 不可达时快速失败, 不阻塞盘前流程
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect((host, port))
        except (ConnectionRefusedError, OSError) as e:
            logger.warning(f"[财报日历] OpenD 不可达 ({host}:{port}), 主源跳过: {e}")
            return []
        finally:
            sock.close()

        ctx = None
        try:
            # TZ-2 (2026-08-30): 富途取数窗口锚 ET 日历日 — 原服务器 CST 日期在 ET 午后
            #   领先一天, 拉取窗口边界整体偏移 (下游 "财报前3天"门随之漂移)。
            today = now_et().date()
            # 两段窗口, 每段 ≤7 天含端点 (API 硬限制)
            windows = [
                (str(today - timedelta(days=max(days_back, 0))), str(today)),
                (str(today), str(today + timedelta(days=max(days_ahead - 1, 1)))),
            ]

            ctx = OpenQuoteContext(host=host, port=port)
            frames = []
            for begin, end in windows:
                ret, data = ctx.get_earnings_calendar(
                    market="US", begin_date=begin, end_date=end)
                if ret == RET_OK and data is not None and len(data) > 0:
                    frames.append(data)
                else:
                    logger.warning(
                        f"[财报日历] 富途主源窗口 {begin}~{end} 返回空/失败 (ret={ret})")

            if not frames:
                logger.warning("[财报日历] 富途主源全窗口无数据, 转 AV 备源")
                return []
            import pandas as pd
            data = pd.concat(frames, ignore_index=True)
            # 两窗口在 today 重叠 → 按 (security, earnings_date) 去重
            data = data.drop_duplicates(subset=["security", "earnings_date"])
            
            # pub_type → AV timeOfTheDay 契约映射
            _PUB_MAP = {"BEFORE": "pre-market", "AFTER": "post-market", "REGULAR": ""}
            
            rows = []
            for _, r in data.iterrows():
                try:
                    security = str(r.get("security", ""))
                    report_date = str(r.get("earnings_date", ""))
                    if not security.startswith("US.") or not report_date:
                        continue
                    ticker = security[3:]
                    
                    eps = r.get("eps_predict")
                    # 富途缺数据用字符串 'N/A' (与 AV CSV 契约对齐)
                    estimate = "N/A" if (eps is None or str(eps) in ("N/A", "nan", "")) else str(eps)
                    
                    rows.append({
                        "symbol": ticker,
                        "name": str(r.get("name", ticker)),
                        "reportDate": report_date,
                        "timeOfTheDay": _PUB_MAP.get(str(r.get("pub_type", "")), ""),
                        "estimate": estimate,
                        "currency": "USD",
                        "source": "futu",
                    })
                except Exception:
                    continue
            
            logger.info(
                f"[财报日历] 富途主源生效: {len(rows)} 条 "
                f"({len(frames)} 窗口合并去重)")
            return rows
        except Exception as e:
            logger.warning(f"[财报日历] 富途主源异常: {e}")
            return []
        finally:
            try:
                if ctx is not None:
                    ctx.close()
            except Exception:
                pass
    
    def get_this_week_earnings(self, force_refresh: bool = False) -> Dict[str, List[Dict]]:
        """
        获取本周财报日历，按日期分组
        
        Returns:
            {
                "2026-04-23": [{"symbol": "NVDA", "name": "NVIDIA", ...}, ...],
                "2026-04-24": [...],
                ...
            }
        """
        calendar = self.get_earnings_calendar(force_refresh=force_refresh)

        # ⚠️ TZ-2 钩子 (2026-08-30, 复活前必修): 本方法零消费方; 下方日期锚仍为服务器
        #   CST datetime.now(), 复活时须改 now_et().date() 并按 ET 口径重验窗口边界。
        today = datetime.now()
        week_start = today  # 从今天开始
        week_end = today + timedelta(days=7)
        
        result = {}
        for entry in calendar:
            try:
                report_date = entry.get("reportDate", "")
                if not report_date:
                    continue
                entry_date = datetime.strptime(report_date, "%Y-%m-%d")
                
                # 只保留本周的数据
                if week_start <= entry_date <= week_end:
                    if report_date not in result:
                        result[report_date] = []
                    result[report_date].append(entry)
            except (ValueError, TypeError):
                continue
        
        # 按日期排序
        return dict(sorted(result.items()))
    
    def get_watchlist_earnings(self, watchlist_symbols: Optional[List[str]] = None,
                                days_ahead: int = 7, force_refresh: bool = False) -> List[Dict]:
        """
        获取 Watchlist 中股票的财报日历
        
        Args:
            watchlist_symbols: 股票代码列表 (Futu 格式如 'US.NVDA')
                             None 则使用 KEY_US_SYMBOLS 中的所有
            days_ahead: 查询未来几天的数据
            
        Returns:
            过滤后的财报列表，每个元素包含:
            - symbol: Futu 格式代码
            - ticker: 原始代码
            - name: 公司名
            - report_date: 财报日期
            - estimate: 预期 EPS
            - time_of_day: 盘前/盘后
            - sector: 行业
            - days_until: 距离今天的天数
        """
        calendar = self.get_earnings_calendar(force_refresh=force_refresh)
        
        # 确定要监控的代码
        if watchlist_symbols:
            # 从 KEY_US_SYMBOLS 中匹配
            tickers_to_watch = set()
            for sym in watchlist_symbols:
                if sym in KEY_US_SYMBOLS:
                    tickers_to_watch.add(KEY_US_SYMBOLS[sym][0])
                else:
                    # 尝试直接从 Futu 格式提取 ticker
                    if sym.startswith("US."):
                        tickers_to_watch.add(sym[3:])
            watchlist_futu_map = {v[0]: k for k, v in KEY_US_SYMBOLS.items()}
        else:
            # 2026-08-21 修复覆盖缺口: None=监控日历全量 (生产调用路径)。
            #   此前只盯 KEY_US_SYMBOLS 静态 37 票, 动态池标的 (WEAV/BULL 类)
            #   财报零保护。全量覆盖后 earnings_risk_guard 按 symbol 精确匹配,
            #   不会误伤无财报标的。
            tickers_to_watch = None  # None = 不过滤
            watchlist_futu_map = {v[0]: k for k, v in KEY_US_SYMBOLS.items()}
        
        # TZ-2 (2026-08-30): 窗口锚 ET 日历日 — 原服务器 CST datetime.now() 在 ET 午后
        #   领先一天, "财报前N天"门 (screener/reentry/持仓建议消费 days_until) 整体偏移。
        #   days_until 同步改为 ET 日历日差 (原 (零点-带时分秒) 向下取整, 同一财报
        #   一天内早晚报数不同)。
        today_d = now_et().date()
        cutoff_d = today_d + timedelta(days=days_ahead)

        result = []
        for entry in calendar:
            ticker = entry.get("symbol", "")
            if tickers_to_watch is not None and ticker not in tickers_to_watch:
                continue

            try:
                report_date = entry.get("reportDate", "")
                if not report_date:
                    continue
                entry_date = datetime.strptime(report_date, "%Y-%m-%d")

                # 2026-08-21 修复: 同日边界 — 按日期比较 (BJ 案), 不与带时分秒的 now 混比。
                if today_d <= entry_date.date() <= cutoff_d:
                    futu_sym = watchlist_futu_map.get(ticker, f"US.{ticker}")
                    sector = KEY_US_SYMBOLS.get(futu_sym, ("", "", "Unknown"))[2]

                    result.append({
                        "symbol": futu_sym,
                        "ticker": ticker,
                        "name": entry.get("name", ticker),
                        "report_date": report_date,
                        "estimate": entry.get("estimate", "N/A"),
                        "currency": entry.get("currency", "USD"),
                        "time_of_day": entry.get("timeOfTheDay", "N/A"),
                        "sector": sector,
                        "days_until": (entry_date.date() - today_d).days,
                    })
            except (ValueError, TypeError):
                continue
        
        # 按日期排序
        result.sort(key=lambda x: x["report_date"])
        return result
    
    def format_for_llm(self, watchlist_symbols: Optional[List[str]] = None,
                       days_ahead: int = 7) -> str:
        """
        格式化为 LLM 可读的文本 (用于注入 prompt)
        
        Returns:
            格式化的文本字符串
        """
        earnings = self.get_watchlist_earnings(
            watchlist_symbols=watchlist_symbols,
            days_ahead=days_ahead
        )
        
        if not earnings:
            return "📅 本周 Watchlist 中无已知财报发布。"
        
        lines = ["📅 本周财报日历 (Watchlist):"]
        lines.append("")
        
        # 按日期分组
        by_date = {}
        for e in earnings:
            date = e["report_date"]
            if date not in by_date:
                by_date[date] = []
            by_date[date].append(e)
        
        for date, items in sorted(by_date.items()):
            date_obj = datetime.strptime(date, "%Y-%m-%d")
            weekday = date_obj.strftime("%A")
            lines.append(f"**{date} ({weekday})**:")
            
            for item in items:
                timing = item["time_of_day"]
                if timing == "pre-market":
                    timing_icon = "🌅 盘前"
                elif timing == "post-market":
                    timing_icon = "🌆 盘后"
                else:
                    timing_icon = "⏰ 盘中"
                
                est = item["estimate"] if item["estimate"] else "N/A"
                lines.append(
                    f"  - {item['symbol']} ({item['name']}) | {timing_icon} "
                    f"| EPS 预期: {est} | {item['sector']}"
                )
        
        lines.append("")
        lines.append("> 提示: 财报日前后波动率显著增加，请注意调整止损宽度和仓位。")
        
        return "\n".join(lines)
    
    def get_earnings_history(self, ticker: str, limit: int = 4) -> List[Dict]:
        """
        获取个股历史财报数据 (用于 LLM 分析历史表现)
        
        Args:
            ticker: 股票代码 (如 'NVDA')
            limit: 返回最近几期
            
        Returns:
            历史财报列表
        """
        params = {
            "function": "EARNINGS",
            "symbol": ticker,
            "apikey": self.api_key,
        }
        
        try:
            resp = requests.get(self.base_url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                reports = data.get("quarterlyEarnings", [])
                return reports[:limit]
            else:
                logger.warning(f"获取 {ticker} 财报历史失败: HTTP {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"获取 {ticker} 财报历史异常: {scrub_secrets(e)}")
            return []
    
    def format_history_for_llm(self, ticker: str) -> str:
        """格式化历史财报数据为 LLM 可读文本"""
        history = self.get_earnings_history(ticker)
        
        if not history:
            return f"{ticker} 财报历史不可用。"
        
        lines = [f"**{ticker} 最近 {len(history)} 期财报表现:**"]
        for i, report in enumerate(history):
            fiscal_date = report.get("fiscalDateEnding", "N/A")
            reported_eps = report.get("reportedEPS", "N/A")
            estimated_eps = report.get("estimatedEPS", "N/A")
            surprise = report.get("surprise", "N/A")
            surprise_pct = report.get("surprisePercentage", "N/A")
            
            if surprise != "N/A" and float(surprise) > 0:
                icon = "✅"
            elif surprise != "N/A" and float(surprise) < 0:
                icon = "❌"
            else:
                icon = "➖"
            
            lines.append(
                f"  {icon} {fiscal_date}: 实际 {reported_eps} vs 预期 {estimated_eps} "
                f"(超预期 {surprise_pct}%)"
            )
        
        return "\n".join(lines)

    # ── Phase 2: 财报后动量分析 ───────────────────────────────────────────

    def get_recently_reported(self, days_back: int = 1) -> List[Dict]:
        """
        获取最近 N 天已发布财报的股票 (用于盘后动量分析)
        
        Phase 2 核心: 盘后检测已发布财报的股票，
        分析超预期程度 + 市场反应，生成次日交易计划。

        2026-08-29 增强: 叠加富途 earnings_beat_rank (EPS beat_ratio/actual/estimate)
        补充超预期幅度, 供盘后动量分析与次日交易计划使用。
        
        Args:
            days_back: 查询过去几天的数据 (默认 1 = 今天)
            
        Returns:
            已发布财报列表，每个元素包含:
            - symbol: Futu 格式代码
            - ticker: 原始代码
            - name: 公司名
            - report_date: 财报日期
            - estimate: 预期 EPS
            - actual: 实际 EPS (来自富途, 可能为 None)
            - beat_ratio: 超预期幅度 (actual-estimate)/|estimate|, 可能为 None
            - time_of_day: 盘前/盘后
            - sector: 行业
            - atr_multiplier: 行业 ATR 倍数配置
        """
        calendar = self.get_earnings_calendar()

        # ⚠️ TZ-2 钩子 (2026-08-30, 复活前必修): 本方法零消费方; 下方日期锚仍为服务器
        #   CST datetime.now(), 复活时须改 now_et().date() 并按 ET 口径重验窗口边界。
        today = datetime.now()
        start_date = today - timedelta(days=days_back)
        
        # 构建 Futu 映射
        watchlist_futu_map = {v[0]: k for k, v in KEY_US_SYMBOLS.items()}

        # 富途超预期数据 (EPS)
        beat_map = self._fetch_futu_beat_rank()
        
        result = []
        for entry in calendar:
            ticker = entry.get("symbol", "")
            if ticker not in watchlist_futu_map:
                continue  # Phase 2: 只关注 watchlist 中的股票
            futu_sym = watchlist_futu_map[ticker]
            sector = KEY_US_SYMBOLS.get(futu_sym, ("", "", "Unknown"))[2]
            
            try:
                report_date = entry.get("reportDate", "")
                if not report_date:
                    continue
                entry_date = datetime.strptime(report_date, "%Y-%m-%d")
                
                # 过去 N 天内发布的
                if start_date <= entry_date <= today:
                    atr_config = SECTOR_ATR_MULTIPLIERS.get(sector, SECTOR_ATR_MULTIPLIERS["Unknown"])

                    beat_info = beat_map.get(futu_sym, {})
                    
                    result.append({
                        "symbol": futu_sym,
                        "ticker": ticker,
                        "name": entry.get("name", ticker),
                        "report_date": report_date,
                        "estimate": entry.get("estimate", "N/A"),
                        "actual": beat_info.get("actual"),
                        "beat_ratio": beat_info.get("beat_ratio"),
                        "currency": entry.get("currency", "USD"),
                        "time_of_day": entry.get("timeOfTheDay", "N/A"),
                        "sector": sector,
                        "atr_multiplier": atr_config["earnings"],  # 财报期间用 earnings 倍数
                        "flash_exit_multiplier": atr_config["flash_exit"],
                    })
            except (ValueError, TypeError):
                continue
        
        # 按日期倒序 (最近的在前)
        result.sort(key=lambda x: x["report_date"], reverse=True)
        return result

    def _fetch_futu_beat_rank(self) -> Dict[str, Dict]:
        """富途 EPS 超预期榜 → {futu_symbol: {actual, estimate, beat_ratio}} (2026-08-29).

        一次调用覆盖全市场近期已发布财报标的, 弥补 AV 日历只有 estimate 无 actual 的盲区。
        失败静默返回空 (不影响主链路)。
        """
        try:
            from app.core.config_container import get_config
            if not bool(get_config().get_dotted(
                    "futu_channels.earnings_beat_enabled", True)):
                return {}
        except Exception:
            pass
        try:
            from futu import OpenQuoteContext, RET_OK, Market, BeatType
            ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
            try:
                ret, out = ctx.get_earnings_beat_rank(Market.US, BeatType.EPS, count=100)
                if ret != RET_OK or not isinstance(out, tuple) or len(out) < 2:
                    return {}
                _, df = out
                if df is None or not len(df):
                    return {}
                result = {}
                for _, r in df.iterrows():
                    try:
                        sym = str(r.get("security", ""))
                        if not sym:
                            continue
                        actual = float(r.get("actual", 0) or 0)
                        est = float(r.get("estimate", 0) or 0)
                        beat = float(r.get("beat_ratio", 0) or 0)
                        result[sym] = {
                            "actual": actual if actual else None,
                            "estimate": est if est else None,
                            "beat_ratio": beat if beat else None,
                        }
                    except Exception:
                        continue
                logger.info(f"[财报日历] 富途 beat 榜: {len(result)} 条")
                return result
            finally:
                ctx.close()
        except Exception as e:
            logger.debug(f"[财报日历] 富途 beat 榜获取失败 (非致命): {e}")
            return {}

    def analyze_post_earnings_opportunity(self, ticker: str) -> Dict:
        """
        分析个股财报后的交易机会 (Phase 2 核心)
        
        综合财报超预期程度 + 历史表现 + 行业特性，
        生成次日交易建议。
        
        Args:
            ticker: 股票代码
            
        Returns:
            {
                "ticker": str,
                "surprise_pct": float,  # 超预期百分比
                "beat_streak": int,     # 连续超预期次数
                "sector": str,
                "atr_multiplier": float,
                "momentum_signal": str,  # "STRONG_BUY" / "BUY" / "NEUTRAL" / "SELL" / "STRONG_SELL"
                "confidence": float,     # 0.0 - 1.0
                "reason": str,           # 分析原因
            }
        """
        history = self.get_earnings_history(ticker, limit=8)
        
        # 获取行业配置 (always, even when history is unavailable)
        futu_sym = f"US.{ticker}"
        if futu_sym in KEY_US_SYMBOLS:
            sector = KEY_US_SYMBOLS[futu_sym][2]
        else:
            sector = "Unknown"
        atr_config = SECTOR_ATR_MULTIPLIERS.get(sector, SECTOR_ATR_MULTIPLIERS["Unknown"])
        
        if not history:
            return {
                "ticker": ticker,
                "surprise_pct": 0,
                "beat_streak": 0,
                "sector": sector,
                "atr_multiplier": atr_config["earnings"],
                "flash_exit_multiplier": atr_config["flash_exit"],
                "momentum_signal": "NEUTRAL",
                "confidence": 0.0,
                "reason": "财报历史数据不可用",
            }
        
        # 分析最近一期财报
        latest = history[0]
        surprise_pct = float(latest.get("surprisePercentage", 0))
        surprise_val = float(latest.get("surprise", 0))
        
        # 计算连续超预期次数
        beat_streak = 0
        for report in history:
            s = float(report.get("surprise", 0))
            if s > 0:
                beat_streak += 1
            else:
                break
        
        # 综合评分逻辑
        if surprise_pct > 10 and beat_streak >= 3:
            signal = "STRONG_BUY"
            confidence = min(0.9, 0.6 + surprise_pct / 100)
            reason = f"超预期 {surprise_pct}% + 连续 {beat_streak} 期 Beat"
        elif surprise_pct > 5 and beat_streak >= 2:
            signal = "BUY"
            confidence = min(0.8, 0.5 + surprise_pct / 100)
            reason = f"超预期 {surprise_pct}% + 连续 {beat_streak} 期 Beat"
        elif surprise_pct > 0:
            signal = "BUY"
            confidence = 0.5 + surprise_pct / 200
            reason = f"超预期 {surprise_pct}%"
        elif surprise_pct > -5:
            signal = "NEUTRAL"
            confidence = 0.4
            reason = f"略低于预期 {surprise_pct}%"
        elif surprise_pct > -15:
            signal = "SELL"
            confidence = 0.5 + abs(surprise_pct) / 100
            reason = f"低于预期 {surprise_pct}%"
        else:
            signal = "STRONG_SELL"
            confidence = min(0.9, 0.6 + abs(surprise_pct) / 100)
            reason = f"大幅低于预期 {surprise_pct}%"
        
        return {
            "ticker": ticker,
            "surprise_pct": surprise_pct,
            "beat_streak": beat_streak,
            "sector": sector,
            "atr_multiplier": atr_config["earnings"],
            "flash_exit_multiplier": atr_config["flash_exit"],
            "momentum_signal": signal,
            "confidence": round(confidence, 2),
            "reason": reason,
        }

    def format_post_earnings_plan(self, tickers: Optional[List[str]] = None) -> str:
        """
        格式化财报后动量分析结果为 LLM 可读文本 (Phase 2)
        
        Args:
            tickers: 要分析的股票代码列表，None 则自动检测最近发布的
            
        Returns:
            格式化的文本字符串
        """
        if tickers:
            opportunities = []
            for t in tickers:
                opp = self.analyze_post_earnings_opportunity(t)
                opportunities.append(opp)
        else:
            # 自动检测最近发布的财报
            recent = self.get_recently_reported(days_back=2)
            opportunities = []
            for entry in recent:
                opp = self.analyze_post_earnings_opportunity(entry["ticker"])
                opp["symbol"] = entry["symbol"]
                opp["report_date"] = entry["report_date"]
                opp["time_of_day"] = entry["time_of_day"]
                opportunities.append(opp)
        
        if not opportunities:
            return "📊 近期无已知财报发布，无动量机会。"
        
        lines = ["📊 财报后动量机会分析:"]
        lines.append("")
        
        for opp in opportunities:
            symbol = opp.get("symbol", f"US.{opp['ticker']}")
            signal = opp["momentum_signal"]
            
            if "BUY" in signal:
                icon = "🟢"
            elif "SELL" in signal:
                icon = "🔴"
            else:
                icon = "🟡"
            
            lines.append(
                f"{icon} **{symbol}** | {signal} (置信度: {opp['confidence']:.0%})"
            )
            lines.append(f"   - {opp['reason']}")
            lines.append(f"   - 连续 Beat: {opp['beat_streak']} 期 | 行业: {opp['sector']}")
            atr_m = opp.get('atr_multiplier', 3.5)
            flash_m = opp.get('flash_exit_multiplier', 1.5)
            lines.append(f"   - 财报期 ATR 倍数: {atr_m}x | Flash Exit: {flash_m}x")
            lines.append("")
        
        return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    
    calendar = EarningsCalendar()
    
    print("=" * 60)
    print("本周财报日历 (Watchlist)")
    print("=" * 60)
    print(calendar.format_for_llm())
    
    print("\n" + "=" * 60)
    print("NVDA 历史财报")
    print("=" * 60)
    print(calendar.format_history_for_llm("NVDA"))
