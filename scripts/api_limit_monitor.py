#!/usr/bin/env python3
"""
API 限流监控脚本

功能:
1. 实时记录每个 API 的调用次数
2. TwelveData 接近 800 次/天时告警 (700 次预警，800 次切换)
3. 自动切换到备用数据源 (Finnhub)
4. 发送钉钉告警

使用方式:
- 手动调用：python scripts/api_limit_monitor.py check
- 定时任务：每 5 分钟执行一次
- 程序导入：from scripts.api_limit_monitor import APILimitMonitor
"""
import os
import sys
import json
import tempfile
import logging
import hashlib
import hmac
import base64
import urllib.parse
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional
from dotenv import load_dotenv

# 加载环境变量
_env_path = Path(__file__).resolve().parent.parent / ".env.us"
if _env_path.exists():
    load_dotenv(_env_path)

# 配置日志 (仅在直接运行时生效，作为模块导入时不干扰主应用日志)
_logger_configured = False

def _ensure_logger():
    """确保日志配置（惰性初始化，避免模块导入时重复配置）"""
    global _logger_configured
    if not _logger_configured:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        _logger_configured = True

logger = logging.getLogger(__name__)

# 常量
# 2026-07-10 修复: 文件已迁入 scripts/archive/, parent.parent 只到 scripts/ →
#   误写 scripts/data/api_limit_state.json, 架空了仓库根 data/ (premarket_data_check 查此处).
#   上溯三级回到仓库根 data/.
STATE_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "api_limit_state.json"
TWELVEDATA_DAILY_LIMIT = 800  # TwelveData 免费计划：800 次/天
TWELVEDATA_WARNING_THRESHOLD = 700  # 预警阈值
FINNHUB_DAILY_LIMIT = 60 * 60 * 24  # Finnhub: 60 次/分钟，理论上 unlimited daily


class DingTalkSender:
    """钉钉消息发送器"""
    
    def __init__(self, webhook_url: Optional[str] = None, secret: Optional[str] = None):
        self.webhook_url = webhook_url or os.getenv("DINGTALK_WEBHOOK_URL_US", "")
        self.secret = secret or os.getenv("DINGTALK_SECRET_US", "")
        self.enabled = bool(self.webhook_url)
    
    def _build_signed_url(self) -> str:
        """构建带签名的 URL"""
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
    
    def send_markdown(self, title: str, content: str) -> bool:
        """发送 Markdown 消息"""
        if not self.enabled:
            logger.warning(f"[钉钉] 推送已禁用：{title}")
            return False
        
        import requests
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": content,
            },
        }
        
        try:
            url = self._build_signed_url()
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode", 0) != 0:
                logger.warning(f"[钉钉] 发送失败：{result}")
                return False
            logger.info(f"[钉钉] 推送成功：{title}")
            return True
        except Exception as e:
            logger.error(f"[钉钉] 推送异常：{e}")
            return False


class APILimitMonitor:
    """
    API 限流监控器
    
    功能:
    1. 记录每个 API 的调用次数 (按天重置)
    2. TwelveData 接近限额时告警 (700 次预警，800 次切换)
    3. 自动切换到备用数据源
    4. Finnhub 分钟级高频调用预警 (限制 60 次/分钟)
    5. 发送钉钉告警
    """
    
    def __init__(self, state_file: Path = STATE_FILE):
        self.state_file = state_file
        self.dingtalk = DingTalkSender()
        self.state = self._load_state()
        
        # 当前数据源
        self.current_source = self.state.get("current_source", "twelvedata")
        
        # 告警状态 (避免重复告警)
        self.warning_sent = self.state.get("warning_sent", False)
        self.limit_reached_sent = self.state.get("limit_reached_sent", False)
        
        # P2 修复 (2026-04-16): Finnhub 分钟级监控
        import collections
        self._finnhub_call_times = collections.deque()
        self._finnhub_warned = False
    
    def _load_state(self) -> Dict[str, Any]:
        """加载状态文件"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                
                # 检查是否需要重置 (新的一天)
                last_date = state.get("last_reset_date", "")
                today = datetime.now().strftime("%Y-%m-%d")
                
                if last_date != today:
                    logger.info(f"[API 监控] 新的一天，重置计数器 (上次：{last_date})")
                    state = self._create_fresh_state()
                
                return state
            except Exception as e:
                logger.error(f"[API 监控] 加载状态文件失败：{e}")
                return self._create_fresh_state()
        else:
            return self._create_fresh_state()
    
    def _create_fresh_state(self) -> Dict[str, Any]:
        """创建新的状态"""
        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "last_reset_date": today,
            "twelvedata_calls": 0,
            "finnhub_calls": 0,
            "alphavantage_calls": 0,
            "current_source": "twelvedata",
            "warning_sent": False,
            "limit_reached_sent": False,
            "last_warning_time": None,
            "last_switch_time": None,
        }
    
    def _save_state(self):
        """保存状态文件 (2026-07-10: 原子写 — 临时文件 + os.replace,
        避免 twelvedata/finnhub 两 provider 无锁并发时, 读方撞上 open('w') 截断的空文件
        导致 'Expecting value' 解析报错 + 计数丢失)
        2026-07-11: 改用 tempfile.mkstemp 保证临时名全局唯一 — 原 os.getpid() 名在
        两 provider 线程同 PID 并发时撞名, 一线程 os.replace 消费后另一线程
        再 replace 报 'No such file'; mkstemp 不依赖 pid/tid, 彻底隔离"""
        _tmp = None
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            _fd, _tmp = tempfile.mkstemp(
                dir=str(self.state_file.parent),
                prefix=self.state_file.name + ".tmp.",
            )
            with os.fdopen(_fd, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
            os.replace(_tmp, self.state_file)  # 原子替换: 读方永远看到完整文件
            _tmp = None
            logger.debug(f"[API 监控] 状态已保存：{self.state_file}")
        except Exception as e:
            logger.error(f"[API 监控] 保存状态文件失败：{e}")
            if _tmp:  # 清理残留临时文件
                try:
                    os.unlink(_tmp)
                except OSError:
                    pass
    
    def record_call(self, source: str, count: int = 1):
        """
        记录 API 调用
        
        Args:
            source: 数据源名称 (twelvedata, finnhub, alphavantage)
            count: 调用次数
        """
        source_lower = source.lower()
        if source_lower not in ["twelvedata", "finnhub", "alphavantage"]:
            logger.warning(f"[API 监控] 未知数据源：{source}")
            return
        
        key = f"{source_lower}_calls"
        self.state[key] = self.state.get(key, 0) + count
        self._save_state()
        
        logger.info(f"[API 监控] 记录调用：{source} +{count} (总计：{self.state[key]})")
        
        # P2 修复 (2026-04-16): Finnhub 分钟级监控
        if source_lower == "finnhub":
            self._check_finnhub_minute_limit(count)
        
        # 检查 TwelveData 限额
        if source_lower == "twelvedata":
            self._check_twelvedata_limit()

    def _check_finnhub_minute_limit(self, count: int = 1):
        """
        检查 Finnhub 分钟级调用频率 (限制 60 次/分钟)
        """
        import time
        now = time.time()
        # 清理 60 秒前的记录
        while self._finnhub_call_times and self._finnhub_call_times[0] < now - 60:
            self._finnhub_call_times.popleft()
        
        # 添加本次调用
        for _ in range(count):
            self._finnhub_call_times.append(now)
            
        current_cpm = len(self._finnhub_call_times)
        
        # 如果超过 45 次/分钟 (75% 阈值)，发出警告
        if current_cpm > 45 and not self._finnhub_warned:
            logger.warning(
                f"[API 监控] ⚠️ Finnhub 调用频率过高: {current_cpm} 次/分钟 "
                f"(限制 60 次/分钟), 请检查轮询间隔"
            )
            self._finnhub_warned = True
            
            # 发送钉钉警告
            self.dingtalk.send_markdown(
                "⚠️ Finnhub API 频率预警",
                f"## ⚠️ Finnhub API 频率预警\n\n"
                f"**当前频率**: {current_cpm} 次/分钟\n"
                f"**限制**: 60 次/分钟\n\n"
                f"建议: 增加轮询间隔或检查是否有重复调用。"
            )
        elif current_cpm <= 30:
            # 如果频率降下来了，重置警告标记
            self._finnhub_warned = False
    
    def _check_twelvedata_limit(self):
        """检查 TwelveData 限额"""
        calls = self.state.get("twelvedata_calls", 0)
        current_source = self.state.get("current_source", "twelvedata")
        
        # 已经达到限额并切换
        if current_source != "twelvedata":
            return
        
        # 预警阈值 (700 次)
        if calls >= TWELVEDATA_WARNING_THRESHOLD and not self.state.get("warning_sent"):
            self._send_warning_alert(calls)
            self.state["warning_sent"] = True
            self.state["last_warning_time"] = datetime.now().isoformat()
        
        # 达到限额 (800 次)
        if calls >= TWELVEDATA_DAILY_LIMIT and not self.state.get("limit_reached_sent"):
            self._switch_to_finnhub()
            self.state["limit_reached_sent"] = True
            self.state["last_switch_time"] = datetime.now().isoformat()
        
        self._save_state()
    
    def _send_warning_alert(self, calls: int):
        """发送预警告警"""
        remaining = TWELVEDATA_DAILY_LIMIT - calls
        percentage = (calls / TWELVEDATA_DAILY_LIMIT) * 100
        
        title = "⚠️ TwelveData API 用量预警"
        content = f"""## ⚠️ TwelveData API 用量预警

**当前用量**: {calls} / {TWELVEDATA_DAILY_LIMIT} 次
**剩余配额**: {remaining} 次
**使用比例**: {percentage:.1f}%

**建议操作**:
- 减少非必要 API 调用
- 增加缓存时间
- 准备切换到 Finnhub

> 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
        
        self.dingtalk.send_markdown(title, content)
        logger.warning(f"[API 监控] 发送预警：{calls}/{TWELVEDATA_DAILY_LIMIT}")
    
    def _switch_to_finnhub(self):
        """切换到 Finnhub"""
        old_source = self.state.get("current_source", "twelvedata")
        self.state["current_source"] = "finnhub"
        self._save_state()
        
        calls = self.state.get("twelvedata_calls", 0)
        
        title = "🚨 TwelveData API 限额已用尽"
        content = f"""## 🚨 TwelveData API 限额已用尽

**当前用量**: {calls} / {TWELVEDATA_DAILY_LIMIT} 次
**已切换数据源**: {old_source} → Finnhub

**影响**:
- 技术指标可能不可用 (Finnhub 不支持)
- 实时报价正常

**恢复时间**: 次日 00:00 自动重置

> 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"""
        
        self.dingtalk.send_markdown(title, content)
        logger.critical(f"[API 监控] 已切换到 Finnhub (TwelveData: {calls}/{TWELVEDATA_DAILY_LIMIT})")
    
    def get_current_source(self) -> str:
        """获取当前数据源"""
        return self.state.get("current_source", "twelvedata")
    
    def should_use_twelvedata(self) -> bool:
        """判断是否应该使用 TwelveData"""
        return self.get_current_source() == "twelvedata"
    
    def should_use_finnhub(self) -> bool:
        """判断是否应该使用 Finnhub"""
        return self.get_current_source() == "finnhub"
    
    def get_status(self) -> Dict[str, Any]:
        """获取当前状态"""
        today = datetime.now().strftime("%Y-%m-%d")
        td_calls = self.state.get("twelvedata_calls", 0)
        fh_calls = self.state.get("finnhub_calls", 0)
        av_calls = self.state.get("alphavantage_calls", 0)
        
        return {
            "date": today,
            "current_source": self.get_current_source(),
            "twelvedata": {
                "calls": td_calls,
                "limit": TWELVEDATA_DAILY_LIMIT,
                "remaining": max(0, TWELVEDATA_DAILY_LIMIT - td_calls),
                "percentage": (td_calls / TWELVEDATA_DAILY_LIMIT) * 100,
                "warning_sent": self.state.get("warning_sent", False),
                "limit_reached": td_calls >= TWELVEDATA_DAILY_LIMIT,
            },
            "finnhub": {
                "calls": fh_calls,
                "limit": "60/min",
            },
            "alphavantage": {
                "calls": av_calls,
                "limit": "500/day",
            },
            "last_updated": datetime.now().isoformat(),
        }
    
    def reset(self):
        """手动重置状态"""
        self.state = self._create_fresh_state()
        self._save_state()
        logger.info("[API 监控] 状态已手动重置")
    
    def check_and_alert(self):
        """检查并发送告警 (定时任务调用)"""
        status = self.get_status()
        td = status["twelvedata"]
        
        logger.info(
            f"[API 监控] 状态检查 | "
            f"TwelveData: {td['calls']}/{td['limit']} ({td['percentage']:.1f}%) | "
            f"当前数据源：{status['current_source']}"
        )
        
        # 如果已经切换，不需要再检查
        if td["limit_reached"]:
            return
        
        # 检查是否需要发送预警
        if td["calls"] >= TWELVEDATA_WARNING_THRESHOLD and not td["warning_sent"]:
            self._send_warning_alert(td["calls"])
            self.state["warning_sent"] = True
            self.state["last_warning_time"] = datetime.now().isoformat()
            self._save_state()
        
        # 检查是否需要切换
        if td["calls"] >= TWELVEDATA_DAILY_LIMIT:
            self._switch_to_finnhub()
            self.state["limit_reached_sent"] = True
            self.state["last_switch_time"] = datetime.now().isoformat()
            self._save_state()


# ── 与数据提供商集成 ──────────────────────────────────────────────

def get_monitor() -> APILimitMonitor:
    """获取监控器单例"""
    return APILimitMonitor()


def record_twelvedata_call(count: int = 1):
    """记录 TwelveData 调用 (供 TwelveDataProvider 调用)"""
    monitor = get_monitor()
    monitor.record_call("twelvedata", count)


def record_finnhub_call(count: int = 1):
    """记录 Finnhub 调用 (供 FinnhubProvider 调用)"""
    monitor = get_monitor()
    monitor.record_call("finnhub", count)


def record_alphavantage_call(count: int = 1):
    """记录 AlphaVantage 调用"""
    monitor = get_monitor()
    monitor.record_call("alphavantage", count)


def should_use_twelvedata() -> bool:
    """判断是否应该使用 TwelveData"""
    monitor = get_monitor()
    return monitor.should_use_twelvedata()


def should_use_finnhub() -> bool:
    """判断是否应该使用 Finnhub"""
    monitor = get_monitor()
    return monitor.should_use_finnhub()


def get_current_data_source() -> str:
    """获取当前数据源"""
    monitor = get_monitor()
    return monitor.get_current_source()


# ── CLI 入口 ──────────────────────────────────────────────────────

if __name__ == "__main__":
    _ensure_logger()  # 仅在直接运行时配置日志
    if len(sys.argv) < 2:
        print("用法：python api_limit_monitor.py <command>")
        print("命令:")
        print("  check    - 检查当前状态")
        print("  status   - 显示详细状态")
        print("  reset    - 手动重置状态")
        print("  test     - 测试钉钉推送")
        sys.exit(1)
    
    command = sys.argv[1]
    monitor = APILimitMonitor()
    
    if command == "check":
        status = monitor.get_status()
        print(f"\n{'='*60}")
        print(f"API 限流监控状态 ({status['date']})")
        print(f"{'='*60}")
        print(f"当前数据源：{status['current_source']}")
        print(f"\nTwelveData:")
        print(f"  调用次数：{status['twelvedata']['calls']}/{status['twelvedata']['limit']}")
        print(f"  剩余配额：{status['twelvedata']['remaining']}")
        print(f"  使用比例：{status['twelvedata']['percentage']:.1f}%")
        print(f"  预警已发送：{status['twelvedata']['warning_sent']}")
        print(f"  限额已用尽：{status['twelvedata']['limit_reached']}")
        print(f"\nFinnhub:")
        print(f"  调用次数：{status['finnhub']['calls']}")
        print(f"\nAlphaVantage:")
        print(f"  调用次数：{status['alphavantage']['calls']}")
        print(f"{'='*60}\n")
        
        # 自动检查并告警
        monitor.check_and_alert()
    
    elif command == "status":
        status = monitor.get_status()
        print(json.dumps(status, indent=2, ensure_ascii=False))
    
    elif command == "reset":
        confirm = input("确认重置状态？(y/N): ")
        if confirm.lower() == 'y':
            monitor.reset()
            print("状态已重置")
        else:
            print("取消重置")
    
    elif command == "test":
        print("测试钉钉推送...")
        success = monitor.dingtalk.send_markdown(
            "🧪 API 限流监控测试",
            f"""## 🧪 API 限流监控测试

**测试时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**测试内容**: 钉钉推送功能

**当前状态**:
- TwelveData 调用：{monitor.state.get('twelvedata_calls', 0)}
- 当前数据源：{monitor.get_current_source()}

> 如果收到此消息，说明推送功能正常。"""
        )
        print(f"推送结果：{'成功' if success else '失败'}")
    
    else:
        print(f"未知命令：{command}")
        sys.exit(1)
