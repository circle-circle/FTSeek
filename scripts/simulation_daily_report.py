#!/usr/bin/env python3
"""
FTSeek 模拟期统一日终报告 (Simulation Daily Report)

设计原则 (方案 A — 纯测量):
- 不预设收益目标，只记录系统实际产出的 alpha
- 双市场合并视图 (港股 HK + 美股 US)
- 只读数据源，不碰任何交易逻辑
- 分级响应: $300 预警 → $600 警戒 → $1,000 熔断
- 连续亏损保护: 3天 > $500/天 → 建议暂停

运行时间: 北京时间 05:00 (美股盘后审计完成后)
推送渠道: 钉钉 Markdown
数据持久化: data/simulation_tracker.json
"""
import os
import sys
import json
import sqlite3
import logging
import time
import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

# ============================================================================
# 路径配置
# ============================================================================
PROJECT_ROOT_HK = "/opt/ftseek/FTSeek"
PROJECT_ROOT_US = "/opt/ftseek/FTSeek-US"
TRACKER_FILE = os.path.join(PROJECT_ROOT_HK, "data", "simulation_tracker.json")
LOG_FILE = os.path.join(PROJECT_ROOT_HK, "logs", "simulation_report.log")

# 分级阈值 (USD)
WARN_THRESHOLD = -300      # 黄线预警
ALERT_THRESHOLD = -600     # 红线警戒
CIRCUIT_BREAKER = -1000    # 熔断
CONSECUTIVE_LOSS_DAYS = 3  # 连续亏损天数
CONSECUTIVE_LOSS_AMOUNT = -500  # 连续亏损金额阈值

# 汇率估算 (USD/HKD)
HKD_TO_USD = 0.128

# ============================================================================
# 日志配置
# ============================================================================
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
    ]
)
logger = logging.getLogger("simulation_report")


# ============================================================================
# 数据模型
# ============================================================================
@dataclass
class MarketDayStats:
    """单日单市场统计"""
    market: str = ""              # "HK" or "US"
    date: str = ""                # YYYY-MM-DD
    signals_generated: int = 0    # 当日信号总数
    signals_executed: int = 0     # 实际执行数
    open_positions: int = 0       # 收盘时持仓数
    closed_trades: int = 0        # 当日平仓数
    realized_pnl_local: float = 0.0  # 已实现盈亏 (本地货币)
    realized_pnl_usd: float = 0.0    # 已实现盈亏 (USD)
    win_rate: float = 0.0         # 胜率
    avg_pnl_pct: float = 0.0      # 平均盈亏%
    max_single_loss: float = 0.0  # 最大单笔亏损 (USD)
    max_single_gain: float = 0.0  # 最大单笔盈利 (USD)


@dataclass
class SimulationTracker:
    """模拟期累计追踪"""
    start_date: str = ""
    total_days: int = 0           # 总交易日数
    trading_days: int = 0         # 实际交易天数
    daily_records: List[Dict] = field(default_factory=list)
    
    # 累计指标
    total_pnl_usd: float = 0.0
    avg_daily_pnl_usd: float = 0.0
    best_day_usd: float = 0.0
    worst_day_usd: float = 0.0
    winning_days: int = 0
    losing_days: int = 0
    
    # 风控指标
    consecutive_losses: int = 0   # 连续亏损天数
    consecutive_loss_amounts: List[float] = field(default_factory=list)
    circuit_breaker_triggered: bool = False
    max_drawdown_from_peak: float = 0.0
    
    # 分市场累计
    hk_total_pnl_usd: float = 0.0
    us_total_pnl_usd: float = 0.0


# ============================================================================
# 数据读取层 (只读)
# ============================================================================
class HKDataFetcher:
    """港股数据读取 (只读 ftseek.db)"""
    
    def __init__(self):
        self.db_path = os.path.join(PROJECT_ROOT_HK, "data", "ftseek.db")
    
    def _connect(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
    
    def get_today_closed_pnl(self, date_str: str) -> tuple:
        """获取当日已平仓盈亏 (HKD)"""
        try:
            conn = self._connect()
            c = conn.cursor()
            # 查询当日 exit_time 的已平仓记录
            c.execute("""
                SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0)
                FROM positions
                WHERE date(exit_time) = ? AND status = 'CLOSED'
            """, (date_str,))
            row = c.fetchone()
            count, pnl = row[0] or 0, float(row[1] or 0.0)
            
            # 获取当日平仓交易的盈亏分布
            c.execute("""
                SELECT realized_pnl FROM positions
                WHERE date(exit_time) = ? AND status = 'CLOSED'
                AND realized_pnl IS NOT NULL
            """, (date_str,))
            pnls = [r[0] for r in c.fetchall()]
            
            conn.close()
            return count, pnl, pnls
        except Exception as e:
            logger.warning(f"HK 当日盈亏查询失败: {e}")
            return 0, 0.0, []
    
    def get_today_signals(self, date_str: str) -> int:
        """获取当日信号总数"""
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute("""
                SELECT COUNT(*) FROM signals
                WHERE date(created_at) = ?
            """, (date_str,))
            count = c.fetchone()[0]
            conn.close()
            return count
        except Exception as e:
            logger.warning(f"HK 信号数查询失败: {e}")
            return 0
    
    def get_open_positions(self) -> int:
        """获取当前持仓数"""
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
            count = c.fetchone()[0]
            conn.close()
            return count
        except Exception as e:
            logger.warning(f"HK 持仓数查询失败: {e}")
            return 0
    
    def get_historical_pnl_series(self, days: int = 30) -> List[Dict]:
        """获取最近 N 天的日盈亏序列"""
        try:
            conn = self._connect()
            c = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            c.execute("""
                SELECT date(exit_time) as day, COUNT(*) as trades,
                       COALESCE(SUM(realized_pnl), 0) as pnl
                FROM positions
                WHERE status = 'CLOSED' AND exit_time >= ?
                GROUP BY date(exit_time)
                ORDER BY day
            """, (cutoff,))
            rows = c.fetchall()
            conn.close()
            return [
                {"date": r[0], "trades": r[1], "pnl_hkd": float(r[2])}
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"HK 历史序列查询失败: {e}")
            return []


class USDataFetcher:
    """美股数据读取 (只读 ftseek_us.db)"""
    
    def __init__(self):
        self.db_path = os.path.join(PROJECT_ROOT_US, "data", "ftseek_us.db")
    
    def _connect(self):
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
    
    def get_today_closed_pnl(self, date_str: str) -> tuple:
        """获取当日已平仓盈亏 (USD)"""
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute("""
                SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0)
                FROM trade_records
                WHERE trade_date = ?
            """, (date_str,))
            row = c.fetchone()
            count, pnl = row[0] or 0, float(row[1] or 0.0)
            
            c.execute("""
                SELECT realized_pnl FROM trade_records
                WHERE trade_date = ? AND realized_pnl IS NOT NULL
            """, (date_str,))
            pnls = [r[0] for r in c.fetchall()]
            
            conn.close()
            return count, pnl, pnls
        except Exception as e:
            logger.warning(f"US 当日盈亏查询失败: {e}")
            return 0, 0.0, []
    
    def get_today_signals(self, date_str: str) -> int:
        """获取当日投票/信号数"""
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute("""
                SELECT COUNT(*) FROM trade_records
                WHERE trade_date = ?
            """, (date_str,))
            count = c.fetchone()[0]
            conn.close()
            return count
        except Exception as e:
            logger.warning(f"US 信号数查询失败: {e}")
            return 0
    
    def get_open_positions(self) -> tuple:
        """获取当前持仓数 + 方向分布"""
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM open_positions")
            total = c.fetchone()[0]
            
            c.execute("SELECT direction, COUNT(*) FROM open_positions GROUP BY direction")
            by_direction = {r[0]: r[1] for r in c.fetchall()}
            conn.close()
            return total, by_direction
        except Exception as e:
            logger.warning(f"US 持仓数查询失败: {e}")
            return 0, {}
    
    def get_capital_state(self) -> Dict:
        """获取资本安全状态"""
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute("""
                SELECT current_equity, initial_capital, drawdown_pct,
                       water_level, is_locked, peak_equity
                FROM capital_state ORDER BY id DESC LIMIT 1
            """)
            row = c.fetchone()
            conn.close()
            if row:
                return {
                    "current_equity": float(row[0] or 0),
                    "initial_capital": float(row[1] or 0),
                    "drawdown_pct": float(row[2] or 0),
                    "water_level": row[3] or "unknown",
                    "is_locked": bool(row[4]),
                    "peak_equity": float(row[5] or 0),
                }
            return {}
        except Exception as e:
            logger.warning(f"US 资本状态查询失败: {e}")
            return {}


# ============================================================================
# 追踪器持久化
# ============================================================================
class SimulationTrackerStore:
    """模拟追踪数据持久化"""
    
    def __init__(self, path: str = TRACKER_FILE):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
    
    def load(self) -> SimulationTracker:
        """加载追踪数据"""
        if not os.path.exists(self.path):
            return SimulationTracker(start_date=datetime.now().strftime("%Y-%m-%d"))
        
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            tracker = SimulationTracker(**data)
            return tracker
        except Exception as e:
            logger.warning(f"加载追踪数据失败，使用空状态: {e}")
            return SimulationTracker(start_date=datetime.now().strftime("%Y-%m-%d"))
    
    def save(self, tracker: SimulationTracker):
        """保存追踪数据"""
        # 转换为 dict (排除 dataclass 的 field 默认值问题)
        data = {
            "start_date": tracker.start_date,
            "total_days": tracker.total_days,
            "trading_days": tracker.trading_days,
            "daily_records": tracker.daily_records,
            "total_pnl_usd": tracker.total_pnl_usd,
            "avg_daily_pnl_usd": tracker.avg_daily_pnl_usd,
            "best_day_usd": tracker.best_day_usd,
            "worst_day_usd": tracker.worst_day_usd,
            "winning_days": tracker.winning_days,
            "losing_days": tracker.losing_days,
            "consecutive_losses": tracker.consecutive_losses,
            "consecutive_loss_amounts": tracker.consecutive_loss_amounts,
            "circuit_breaker_triggered": tracker.circuit_breaker_triggered,
            "max_drawdown_from_peak": tracker.max_drawdown_from_peak,
            "hk_total_pnl_usd": tracker.hk_total_pnl_usd,
            "us_total_pnl_usd": tracker.us_total_pnl_usd,
        }
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


# ============================================================================
# 钉钉推送 (独立实现，不依赖 app.notifier)
# ============================================================================
class DingTalkReporter:
    """独立钉钉报告推送 (复用 webhook 配置)"""
    
    def __init__(self):
        # 尝试从 HK 配置读取
        self.webhook_url = ""
        self.secret = ""
        self._load_config()
    
    def _load_config(self):
        """从环境或配置文件加载 webhook
        
        加载优先级:
        1. .env 文件 (crontab 环境唯一可靠来源)
        2. 系统环境变量 (交互式 shell)
        3. app.config 模块 (进程内)
        """
        # 优先级 1: 从 .env 文件直接读取 (crontab 必须)
        env_path = os.path.join(PROJECT_ROOT_HK, ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('#') or '=' not in line:
                            continue
                        key, _, value = line.partition('=')
                        key = key.strip()
                        value = value.strip()
                        if key == "DINGTALK_WEBHOOK_URL":
                            self.webhook_url = value
                        elif key == "DINGTALK_SECRET":
                            self.secret = value
                if self.webhook_url:
                    logger.info(f"钉钉 webhook 已从 .env 加载: {self.webhook_url[:30]}...")
            except Exception as e:
                logger.warning(f"读取 .env 失败: {e}")
        
        # 优先级 2: 环境变量覆盖 (交互式 shell)
        if not self.webhook_url:
            self.webhook_url = os.getenv("DINGTALK_WEBHOOK_URL", "")
            self.secret = os.getenv("DINGTALK_SECRET", "")
            if self.webhook_url:
                logger.info(f"钉钉 webhook 从环境变量加载: {self.webhook_url[:30]}...")
        
        # 优先级 3: app.config fallback (进程内)
        if not self.webhook_url:
            try:
                sys.path.insert(0, PROJECT_ROOT_HK)
                from app import config as ftseek_config
                self.webhook_url = getattr(ftseek_config, 'DINGTALK_WEBHOOK_URL', '')
                self.secret = getattr(ftseek_config, 'DINGTALK_SECRET', '')
                if self.webhook_url:
                    logger.info(f"钉钉 webhook 从 app.config 加载: {self.webhook_url[:30]}...")
            except Exception:
                pass
        
        if self.webhook_url:
            logger.info(f"✅ 钉钉 webhook 已就绪: {self.webhook_url[:30]}...")
        else:
            logger.warning("⚠️ 钉钉 webhook 未配置，报告仅保存到本地")
    
    def _sign_url(self) -> str:
        if not self.secret:
            return self.webhook_url
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f"{self.webhook_url}&timestamp={timestamp}&sign={sign}"
    
    def send_markdown(self, title: str, text: str) -> bool:
        if not self.webhook_url:
            return False
        import requests
        try:
            url = self._sign_url()
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": title, "text": text},
            }
            resp = requests.post(url, json=payload, timeout=10)
            result = resp.json()
            if result.get("errcode") == 0:
                logger.info("✅ 钉钉报告推送成功")
                return True
            else:
                logger.error(f"钉钉推送失败: {result}")
                return False
        except Exception as e:
            logger.error(f"钉钉推送异常: {e}")
            return False


# ============================================================================
# 报告生成器
# ============================================================================
class SimulationDailyReport:
    """模拟期日终报告生成器"""
    
    def __init__(self):
        self.hk = HKDataFetcher()
        self.us = USDataFetcher()
        self.store = SimulationTrackerStore()
        self.dingtalk = DingTalkReporter()
    
    def _get_yesterday_date_str(self) -> str:
        """获取"昨日"日期 (报告针对已结束的交易日)"""
        # 北京时间 05:00 运行，美股昨日收盘 = 美东昨日
        # 港股昨日收盘 = HKT 昨日
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        yesterday = now_bj - timedelta(days=1)
        return yesterday.strftime("%Y-%m-%d")
    
    def _get_current_bj_date(self) -> str:
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    
    def fetch_market_data(self, date_str: str) -> Dict[str, Any]:
        """采集双市场当日数据"""
        result = {}
        
        # --- 港股 ---
        hk_closed_count, hk_pnl_hkd, hk_pnls = self.hk.get_today_closed_pnl(date_str)
        hk_signals = self.hk.get_today_signals(date_str)
        hk_open = self.hk.get_open_positions()
        
        hk_pnl_usd = hk_pnl_hkd * HKD_TO_USD
        hk_wins = sum(1 for p in hk_pnls if p > 0) if hk_pnls else 0
        hk_wr = (hk_wins / len(hk_pnls) * 100) if hk_pnls else 0
        hk_avg = (sum(hk_pnls) / len(hk_pnls) * HKD_TO_USD) if hk_pnls else 0
        
        result["HK"] = {
            "date": date_str,
            "signals": hk_signals,
            "closed_trades": hk_closed_count,
            "open_positions": hk_open,
            "realized_pnl_hkd": hk_pnl_hkd,
            "realized_pnl_usd": round(hk_pnl_usd, 2),
            "win_rate": round(hk_wr, 1),
            "avg_pnl_usd": round(hk_avg, 2),
            "max_loss": round(min(hk_pnls, default=0) * HKD_TO_USD, 2),
            "max_gain": round(max(hk_pnls, default=0) * HKD_TO_USD, 2),
        }
        
        # --- 美股 ---
        us_closed_count, us_pnl_usd, us_pnls = self.us.get_today_closed_pnl(date_str)
        us_signals = self.us.get_today_signals(date_str)
        us_open, us_directions = self.us.get_open_positions()
        us_capital = self.us.get_capital_state()
        
        us_wins = sum(1 for p in us_pnls if p > 0) if us_pnls else 0
        us_wr = (us_wins / len(us_pnls) * 100) if us_pnls else 0
        us_avg = (sum(us_pnls) / len(us_pnls)) if us_pnls else 0
        
        result["US"] = {
            "date": date_str,
            "signals": us_signals,
            "closed_trades": us_closed_count,
            "open_positions": us_open,
            "directions": us_directions,
            "realized_pnl_usd": round(us_pnl_usd, 2),
            "win_rate": round(us_wr, 1),
            "avg_pnl_usd": round(us_avg, 2),
            "max_loss": round(min(us_pnls, default=0), 2),
            "max_gain": round(max(us_pnls, default=0), 2),
            "capital": us_capital,
        }
        
        # --- 合并 ---
        total_pnl_usd = hk_pnl_usd + us_pnl_usd
        result["total_pnl_usd"] = round(total_pnl_usd, 2)
        result["total_closed"] = hk_closed_count + us_closed_count
        result["total_signals"] = hk_signals + us_signals
        
        return result
    
    def update_tracker(self, tracker: SimulationTracker, market_data: Dict) -> Dict:
        """更新累计追踪器，返回分级响应"""
        total_pnl = market_data["total_pnl_usd"]
        today = market_data.get("HK", {}).get("date", "")
        
        # 判断是否为有效交易日 (有信号或有交易)
        is_trading_day = market_data["total_signals"] > 0 or market_data["total_closed"] > 0
        
        if is_trading_day:
            tracker.trading_days += 1
            tracker.total_pnl_usd += total_pnl
            tracker.avg_daily_pnl_usd = tracker.total_pnl_usd / tracker.trading_days
            
            # 日记录
            record = {
                "date": today,
                "hk_pnl_usd": market_data["HK"]["realized_pnl_usd"],
                "us_pnl_usd": market_data["US"]["realized_pnl_usd"],
                "total_pnl_usd": total_pnl,
                "signals": market_data["total_signals"],
                "trades": market_data["total_closed"],
            }
            tracker.daily_records.append(record)
            
            # 最佳/最差日
            if total_pnl > tracker.best_day_usd:
                tracker.best_day_usd = total_pnl
            if total_pnl < tracker.worst_day_usd:
                tracker.worst_day_usd = total_pnl
            
            # 胜/负天数
            if total_pnl > 0:
                tracker.winning_days += 1
                tracker.consecutive_losses = 0
                tracker.consecutive_loss_amounts.clear()
            else:
                tracker.losing_days += 1
                tracker.consecutive_losses += 1
                tracker.consecutive_loss_amounts.append(total_pnl)
            
            # 分市场累计
            tracker.hk_total_pnl_usd += market_data["HK"]["realized_pnl_usd"]
            tracker.us_total_pnl_usd += market_data["US"]["realized_pnl_usd"]
            
            # 峰值回撤
            peak = max([r["total_pnl_usd"] for r in tracker.daily_records] + [0])
            current_drawdown = tracker.total_pnl_usd - peak
            if current_drawdown < tracker.max_drawdown_from_peak:
                tracker.max_drawdown_from_peak = current_drawdown
        
        tracker.total_days += 1
        
        # --- 分级响应 ---
        response = {"level": "OK", "message": ""}
        
        if total_pnl <= CIRCUIT_BREAKER:
            response = {"level": "CIRCUIT_BREAKER", "message": f"🚨 触发熔断! 今日亏损 ${total_pnl:.0f} ≥ ${CIRCUIT_BREAKER}"}
            tracker.circuit_breaker_triggered = True
        elif tracker.consecutive_losses >= CONSECUTIVE_LOSS_DAYS and all(
            a <= CONSECUTIVE_LOSS_AMOUNT for a in tracker.consecutive_loss_amounts[-CONSECUTIVE_LOSS_DAYS:]
        ):
            response = {"level": "PAUSE_RECOMMEND", "message": f"⚠️ 连续 {tracker.consecutive_losses} 天亏损，建议暂停交易"}
        elif total_pnl <= ALERT_THRESHOLD:
            response = {"level": "ALERT", "message": f"🔴 红线警戒: 今日亏损 ${total_pnl:.0f}"}
        elif total_pnl <= WARN_THRESHOLD:
            response = {"level": "WARN", "message": f"🟡 预警: 今日亏损 ${total_pnl:.0f}"}
        
        return response
    
    def build_markdown_report(self, market_data: Dict, tracker: SimulationTracker,
                               response: Dict) -> str:
        """构建钉钉 Markdown 报告"""
        today = market_data.get("HK", {}).get("date", "N/A")
        total_pnl = market_data["total_pnl_usd"]
        hk = market_data["HK"]
        us = market_data["US"]
        
        # 报告标题 emoji
        if response["level"] == "CIRCUIT_BREAKER":
            title_emoji = "🚨"
        elif response["level"] == "PAUSE_RECOMMEND":
            title_emoji = "⚠️"
        elif response["level"] in ("ALERT", "WARN"):
            title_emoji = "🔴"
        elif total_pnl > 0:
            title_emoji = "🟢"
        else:
            title_emoji = "📊"
        
        days_elapsed = (datetime.now() - datetime.strptime(tracker.start_date, "%Y-%m-%d")).days
        day_num = f"第{tracker.trading_days}个交易日" if tracker.trading_days > 0 else "模拟第1天"
        
        lines = [
            f"### {title_emoji} FTSeek 模拟日报 | {today}",
            f"**{day_num}** (模拟已运行 {days_elapsed} 天)",
            "",
        ]
        
        # --- 每日盈亏 ---
        pnl_sign = "+" if total_pnl >= 0 else ""
        pnl_emoji = "🟢" if total_pnl >= 0 else ("🔴" if total_pnl < WARN_THRESHOLD else "🟡")
        lines.extend([
            f"**📊 当日模拟盈亏**: {pnl_emoji} **${pnl_sign}{total_pnl:,.2f}**",
            f"港股: HK${hk['realized_pnl_hkd']:,.0f} (${hk['realized_pnl_usd']:+,.2f})",
            f"美股: ${us['realized_pnl_usd']:+,.2f}",
            f"信号: {market_data['total_signals']} 次 | 交易: {market_data['total_closed']} 笔",
            "",
        ])
        
        # --- 分级响应 ---
        if response["level"] != "OK":
            lines.extend([
                f"**{response['message']}**",
                "",
            ])
        
        # --- 港股详情 ---
        lines.extend([
            f"**🇭🇰 港股 (HK)**",
            f"- 信号: {hk['signals']} | 平仓: {hk['closed_trades']} | 持仓: {hk['open_positions']} 只",
        ])
        if hk['closed_trades'] > 0:
            lines.append(f"- 胜率: {hk['win_rate']:.0f}% | 平均: ${hk['avg_pnl_usd']:+,.2f}")
        if hk['max_loss'] < 0:
            lines.append(f"- 最大亏损: ${hk['max_loss']:.0f} | 最大盈利: ${hk['max_gain']:.0f}")
        lines.append("")
        
        # --- 美股详情 ---
        lines.extend([
            f"**🇺🇸 美股 (US)**",
            f"- 交易: {us['closed_trades']} | 持仓: {us['open_positions']} 只",
        ])
        if us.get('directions'):
            dir_str = " / ".join(f"{k}: {v}" for k, v in us['directions'].items())
            lines.append(f"- 方向: {dir_str}")
        if us['closed_trades'] > 0:
            lines.append(f"- 胜率: {us['win_rate']:.0f}% | 平均: ${us['avg_pnl_usd']:+,.2f}")
        if us.get('capital'):
            cap = us['capital']
            lines.append(f"- 水位: {cap.get('water_level', 'N/A')} | 回撤: {cap.get('drawdown_pct', 0):.1f}% | 净值: ${cap.get('current_equity', 0):,.0f}")
        lines.append("")
        
        # --- 累计趋势 ---
        if tracker.trading_days > 0:
            total_sign = "+" if tracker.total_pnl_usd >= 0 else ""
            avg_sign = "+" if tracker.avg_daily_pnl_usd >= 0 else ""
            win_rate = (tracker.winning_days / tracker.trading_days * 100)
            
            lines.extend([
                f"**📅 累计趋势**",
                f"- 总模拟 PnL: **${total_sign}{tracker.total_pnl_usd:,.2f}**",
                f"- 日均 PnL: ${avg_sign}{tracker.avg_daily_pnl_usd:,.2f}",
                f"- 盈利天数: {tracker.winning_days}/{tracker.trading_days} ({win_rate:.0f}%)",
                f"- 最佳日: ${tracker.best_day_usd:+,.2f} | 最差日: ${tracker.worst_day_usd:+,.2f}",
                f"- 分市场: HK ${tracker.hk_total_pnl_usd:+,.2f} | US ${tracker.us_total_pnl_usd:+,.2f}",
            ])
            
            if tracker.consecutive_losses > 0:
                lines.append(f"- ⚠️ 连续亏损: {tracker.consecutive_losses} 天")
            if tracker.max_drawdown_from_peak < 0:
                lines.append(f"- 峰值回撤: ${tracker.max_drawdown_from_peak:,.2f}")
            lines.append("")
        
        # --- 系统状态 ---
        lines.extend([
            f"**⚙️ 系统状态**",
            f"- 港股 FTSeek: 运行中 (数据截至 {today})",
            f"- 美股 FTSeek-US: 运行中 (数据截至 {today})",
            f"- 报告生成: {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M CST')}",
        ])
        
        return "\n".join(lines)
    
    def run(self):
        """主流程"""
        logger.info("=" * 60)
        logger.info("开始生成模拟期日终报告...")
        
        # 1. 确定报告日期
        date_str = self._get_yesterday_date_str()
        logger.info(f"报告日期: {date_str}")
        
        # 2. 加载追踪器
        tracker = self.store.load()
        logger.info(f"追踪器状态: 起始日 {tracker.start_date}, 已运行 {tracker.trading_days} 个交易日")
        
        # 3. 采集数据
        market_data = self.fetch_market_data(date_str)
        logger.info(f"数据采集完成: HK PnL=${market_data['HK']['realized_pnl_usd']:+.2f}, "
                     f"US PnL=${market_data['US']['realized_pnl_usd']:+.2f}, "
                     f"合计=${market_data['total_pnl_usd']:+.2f}")
        
        # 4. 更新追踪器
        response = self.update_tracker(tracker, market_data)
        logger.info(f"分级响应: {response['level']} - {response['message']}")
        
        # 5. 保存
        self.store.save(tracker)
        
        # 6. 构建报告
        report_md = self.build_markdown_report(market_data, tracker, response)
        
        # 7. 推送
        if self.dingtalk.webhook_url:
            title = f"FTSeek 模拟日报 | {date_str}"
            success = self.dingtalk.send_markdown(title, report_md)
            if success:
                logger.info("✅ 报告推送完成")
            else:
                logger.warning("⚠️ 报告推送失败，已保存到本地")
        else:
            logger.info("钉钉未配置，报告已保存到本地")
        
        # 8. 同时保存到 JSON 报告
        report_path = os.path.join(PROJECT_ROOT_HK, "logs", "audit",
                                    f"simulation_report_{date_str}.json")
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        report_json = {
            "date": date_str,
            "market_data": market_data,
            "tracker_summary": {
                "total_pnl_usd": tracker.total_pnl_usd,
                "avg_daily_pnl_usd": tracker.avg_daily_pnl_usd,
                "trading_days": tracker.trading_days,
                "winning_days": tracker.winning_days,
                "losing_days": tracker.losing_days,
                "consecutive_losses": tracker.consecutive_losses,
                "circuit_breaker_triggered": tracker.circuit_breaker_triggered,
            },
            "response": response,
            "generated_at": datetime.now().isoformat(),
        }
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(report_json, f, indent=2, ensure_ascii=False)
        logger.info(f"JSON 报告已保存: {report_path}")
        
        logger.info("模拟期日终报告完成")
        return report_json


# ============================================================================
# 入口
# ============================================================================
if __name__ == "__main__":
    report = SimulationDailyReport()
    result = report.run()
