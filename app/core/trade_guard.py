"""
FTSeek-US 交易保护网关 (Trade Guard) - 增强版

P0/P1 核心模块：负责合规检查、反手决策评估、黑天鹅拦截。
设计理念：状态机决策 + 全链路审计 + 动态阈值 + 预测分析。
"""
import json
import logging
import os
import time
import uuid
from enum import Enum
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path

logger = logging.getLogger("ftseek_us.trade_guard")

# ============================================================
# 1. 枚举定义
# ============================================================

class GuardDecision(Enum):
    """最终决策结果"""
    ALLOW = 0               # 允许执行
    ALLOW_CLOSE_ONLY = 1    # 仅允许平仓 (减仓/离场)
    BLOCK_REVERSE = 2       # 禁止反手 (维持现状或平仓)
    BLOCK_OPEN_LONG = 3     # 禁止开多
    BLOCK_OPEN_SHORT = 4    # 禁止开空
    DELAY_AND_WAIT = 5      # 延迟等待
    REDUCE_SIZE = 6         # 建议降低仓位

class GuardReason(Enum):
    """决策原因代码"""
    NONE = "无异常"
    PDT_RESTRICTED = "PDT 限制已触发 (账户违规风险)"
    PDT_WARN = "PDT 警告区 (剩余额度不足)"
    REVERSE_COOLDOWN = "反手冷却中 (防止频繁打脸)"
    REVERSE_WEAK_SIGNAL = "反手信号强度不足 (噪音过大)"
    SHORT_UNAVAILABLE = "标的不可做空 (HTB/流动性)"
    BLACK_SWAN_LONG = "黑天鹅事件：禁止逆势抄底"
    BLACK_SWAN_SHORT = "黑天鹅事件：禁止做空"
    VOLATILITY_TOO_HIGH = "波动率过高，暂停交易"

# ============================================================
# 2. 简易交易日历 (NYSE/NASDAQ)
# ============================================================

def is_trading_day(dt: datetime) -> bool:
    """
    判断给定日期是否为美股交易日。
    简化逻辑：周一至周五 (排除主要节假日逻辑，此处仅做周末过滤)。
    """
    return dt.weekday() < 5

def get_trading_window_dates(days: int = 5) -> List[datetime]:
    """获取过去 N 个交易日的日期列表"""
    dates = []
    dt = datetime.now()
    # 最多回溯 10 天以找到 5 个交易日
    for _ in range(14):
        dt -= timedelta(days=1)
        if is_trading_day(dt):
            dates.append(dt)
        if len(dates) >= days:
            break
    return dates

# ============================================================
# 3. 核心管理器
# ============================================================

class PDTComplianceManager:
    """
    模式日内交易 (PDT) 管理器
    
    状态机：
    - NORMAL: 资金 > 25k 或 DT 次数 < 2
    - WATCH: 资金 < 25k 且 DT 次数 == 2 (最后一次机会)
    - RESTRICTED: 资金 < 25k 且 DT 次数 >= 3 (禁止日内开仓)
    """
    def __init__(self, equity: float, account_type: str = "margin", data_dir: str = "data"):
        self.equity = equity
        self.account_type = account_type
        self.state_file = Path(data_dir) / "pdt_state.json"
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                logger.debug("PDT state文件读取失败，使用默认值")
        return {"dt_history": []} # List of {"timestamp": time, "symbol": str}

    def save_state(self):
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f)
        except (IOError, OSError):
            logger.debug("PDT 状态保存失败")

    def check_status(self) -> Dict[str, Any]:
        """
        检查当前 PDT 状态并提供预测。
        返回: { "status": str, "remaining": int, "can_open": bool, "reset_date": str }
        """
        if self.equity >= 25000:
            return {
                "status": "NORMAL", 
                "remaining": 999, 
                "can_open": True,
                "reset_date": "N/A (High Equity)"
            }

        # 计算过去 5 个交易日内的 DT 次数
        trading_window = get_trading_window_dates(5)
        # 取最早的一天作为截止点
        if trading_window:
            cutoff_dt = trading_window[-1]
            cutoff_ts = cutoff_dt.replace(hour=0, minute=0, second=0).timestamp()
        else:
            cutoff_ts = 0

        recent_dts = [t for t in self.state["dt_history"] if t["timestamp"] > cutoff_ts]
        count = len(recent_dts)
        remaining = max(0, 3 - count)

        status = "NORMAL"
        can_open = True
        reset_date_str = "N/A"

        # 计算预计重置日期 (下一个交易日)
        # 简单逻辑：最早的一笔 DT 记录将在 5 个交易日后过期
        if recent_dts:
            oldest_dt_ts = recent_dts[0]["timestamp"]
            # 估算：5 个交易日后
            # 这里简化为自然日 + 7 天作为保守估计
            est_reset = datetime.fromtimestamp(oldest_dt_ts) + timedelta(days=7)
            # 找到之后的第一个交易日
            while not is_trading_day(est_reset):
                est_reset += timedelta(days=1)
            reset_date_str = est_reset.strftime("%Y-%m-%d")

        if count >= 3:
            status = "RESTRICTED"
            can_open = False 
        elif count == 2:
            status = "WATCH"
            # WATCH 状态下允许最后一次，但建议降权
        
        return {
            "status": status, 
            "remaining": remaining, 
            "can_open": can_open, 
            "count": count,
            "reset_date": reset_date_str
        }

    def record_dt(self, symbol: str):
        """记录一次日内交易"""
        self.state["dt_history"].append({"timestamp": time.time(), "symbol": symbol})
        # 清理过期数据 (保留 10 天防止无限增长)
        cutoff = time.time() - (10 * 86400)
        self.state["dt_history"] = [t for t in self.state["dt_history"] if t["timestamp"] > cutoff]
        self.save_state()

class ReverseEngine:
    """
    反手决策引擎 (动态冷却 + 信号强度校验)
    """
    def __init__(self, cooldown_minutes_base: float = 15.0, strength_diff_threshold: int = 15):
        self.cooldown_base = cooldown_minutes_base
        self.strength_threshold = strength_diff_threshold
        self._reverse_history: Dict[str, float] = {} # symbol -> last_reverse_time

    def evaluate(self, symbol: str, current_direction: str, new_direction: str,
                 new_strength: int, old_strength: int, volatility: float = 0.0) -> GuardDecision:
        
        if current_direction == new_direction:
            return GuardDecision.ALLOW

        # 1. 动态冷却计算 (波动率越高，冷却越长)
        # volatility = ATR / Price (approx)
        vol_multiplier = 1.0 + (volatility * 5.0) # 放大系数，波动 20% -> 2x 冷却
        cooldown = self.cooldown_base * vol_multiplier
        
        last_time = self._reverse_history.get(symbol, 0)
        if (time.time() - last_time) < (cooldown * 60):
            return GuardDecision.DELAY_AND_WAIT

        # 2. 信号强度检查
        if new_strength - old_strength < self.strength_threshold:
            return GuardDecision.ALLOW_CLOSE_ONLY # 信号不够强，仅平仓

        # 3. 允许反手
        self._reverse_history[symbol] = time.time()
        return GuardDecision.ALLOW

class CriticalAlertInterceptor:
    """
    突发警报拦截器 (关键词黑白名单 + 来源加权 + TTL)
    """
    def __init__(self):
        # 黑名单 (直接拦截) - 核心风险词
        self.blacklist_keywords = ["FRAUD", "INVESTIGATION", "TRADING HALT", "DELISTING", "BANKRUPTCY", "SUBPOENA"]
        # 白名单 (通常不拦截，除非伴随黑名单关键词) - 噪音词
        self.whitelist_keywords = ["EARNINGS", "ANALYST", "UPGRADE", "ROUTINE FILING", "DIVIDEND", "INSIDER BUY"]
        
        self.sources_weight = {"reuters": 1.0, "bloomberg": 1.0, "sec": 1.0, "wsj": 1.0, "other": 0.3}
        # TTL: 拦截有效期 (秒)
        self.ttl_seconds = 3600 

    def check_alerts(self, symbol: str, news_items: List[Dict[str, Any]] = None, 
                     direction: str = "LONG") -> Optional[GuardDecision]:
        if not news_items:
            return None

        for news in news_items:
            title = news.get("title", "").upper()
            source = news.get("source", "other").lower()
            timestamp = news.get("timestamp", 0)
            
            # 1. TTL 检查
            if (time.time() - timestamp) > self.ttl_seconds:
                continue

            weight = self.sources_weight.get(source, 0.3)
            if weight < 0.5: continue # 忽略低可信度来源

            # 2. 关键词匹配
            # 检查是否命中黑名单
            hit_black = any(kw in title for kw in self.blacklist_keywords)
            
            # 检查是否命中白名单 (降低权重)
            hit_white = any(kw in title for kw in self.whitelist_keywords)
            
            if hit_black:
                # 如果是黑名单，但同时也包含白名单词汇 (例如 "Analyst mentions Fraud investigation")
                # 我们保守起见，依然拦截，或者可以降低权重。
                # 这里逻辑：只要包含黑名单强词，一律拦截 (宁错杀不放过)
                if direction == "LONG":
                    return GuardDecision.BLOCK_OPEN_LONG
                else:
                    return GuardDecision.BLOCK_OPEN_SHORT
            
            # 可选：如果只命中白名单，可以做某些逻辑，通常是不拦截
            
        return None

class TradeGuard:
    """
    统一交易保护网关入口
    """
    def __init__(self, config: Dict[str, Any]):
        self.equity = config.get("equity", 100000)
        self.pdt_manager = PDTComplianceManager(self.equity)
        self.reverse_engine = ReverseEngine()
        self.interceptor = CriticalAlertInterceptor()
        self.log_file = Path(config.get("data_dir", "data")) / "guard_decisions.jsonl"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def evaluate(self, order_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估订单请求，返回决策和原因。
        
        order_context 包含:
        - trace_id: 全链路追踪 ID
        - symbol, direction (OPEN_LONG/OPEN_SHORT/CLOSE)
        - signal_strength, current_holding_strength
        - is_reversal (是否为反手)
        - volatility
        - recent_news (新闻列表)
        """
        symbol = order_context["symbol"]
        trace_id = order_context.get("trace_id", str(uuid.uuid4())[:8])
        direction = order_context.get("direction", "OPEN_LONG")
        news = order_context.get("recent_news", [])
        
        decision = GuardDecision.ALLOW
        reason = GuardReason.NONE 
        
        # 1. 突发警报拦截 (最快拦截) - 优先级最高
        if direction in ["OPEN_LONG", "OPEN_SHORT"]:
            alert = self.interceptor.check_alerts(symbol, news, direction.replace("OPEN_", ""))
            if alert:
                decision = alert
                reason = GuardReason.BLACK_SWAN_LONG if "LONG" in decision.name else GuardReason.BLACK_SWAN_SHORT
                self._log_decision(order_context, decision, reason)
                return {"decision": decision, "reason": reason, "trace_id": trace_id}
        
        # 2. PDT 合规检查 - 优先级次之
        if direction.startswith("OPEN"):
            pdt_status = self.pdt_manager.check_status()
            if not pdt_status["can_open"]:
                decision = GuardDecision.BLOCK_OPEN_LONG if "LONG" in direction else GuardDecision.BLOCK_OPEN_SHORT
                reason = GuardReason.PDT_RESTRICTED
                self._log_decision(order_context, decision, reason)
                return {"decision": decision, "reason": reason, "trace_id": trace_id}
            elif pdt_status["status"] == "WATCH":
                reason = GuardReason.PDT_WARN # 记录日志但不拦截，允许交易
                # 可以在这里注入逻辑：降低仓位等
                self._log_decision(order_context, decision, reason)

        # 3. 反手检查 - 优先级较低 (策略优化)
        if order_context.get("is_reversal", False):
            reverse_decision = self.reverse_engine.evaluate(
                symbol,
                current_direction=order_context.get("current_position_dir"),
                new_direction=direction,
                new_strength=order_context.get("signal_strength", 0),
                old_strength=order_context.get("current_holding_strength", 0),
                volatility=order_context.get("volatility", 0.2)
            )
            if reverse_decision != GuardDecision.ALLOW:
                decision = reverse_decision
                reason = GuardReason.REVERSE_COOLDOWN if reverse_decision == GuardDecision.DELAY_AND_WAIT else GuardReason.REVERSE_WEAK_SIGNAL
                self._log_decision(order_context, decision, reason)
                return {"decision": decision, "reason": reason, "trace_id": trace_id}

        # 4. 执行成功
        self._log_decision(order_context, decision, reason)
        return {"decision": decision, "reason": reason, "trace_id": trace_id}

    def record_dt(self, symbol: str):
        self.pdt_manager.record_dt(symbol)

    def _log_decision(self, context: Dict, decision: GuardDecision, reason: GuardReason):
        # 增强日志字段，便于后续分析 & 对齐异常记录模板
        pdt_status = self.pdt_manager.check_status()
        
        # 判断是否需要人工介入
        action_needed = decision in (GuardDecision.BLOCK_OPEN_LONG, GuardDecision.BLOCK_OPEN_SHORT, GuardDecision.BLOCK_REVERSE)
        
        # 评估影响
        impact = ""
        if decision == GuardDecision.BLOCK_OPEN_LONG: impact = "开多被拦截"
        elif decision == GuardDecision.BLOCK_OPEN_SHORT: impact = "开空被拦截"
        elif decision == GuardDecision.DELAY_AND_WAIT: impact = "反手冷却延迟"
        elif decision == GuardDecision.BLOCK_REVERSE: impact = "反手被否决"
        elif reason == GuardReason.PDT_WARN: impact = "PDT 警告 (未拦截)"
            
        log_entry = {
            "timestamp": time.time(),
            "datetime": datetime.now().isoformat(),
            "trace_id": context.get("trace_id"),
            "symbol": context.get("symbol"),
            "direction": context.get("direction"),
            "decision": decision.name,
            "reason": reason.name,
            "type": reason.name,  # 对齐异常记录模板
            "signal_strength": context.get("signal_strength"),
            "account_status": pdt_status.get("status"),
            "pdt_remaining": pdt_status.get("remaining"),
            "volatility": context.get("volatility"),
            "news_keywords_hit": True if "BLACK_SWAN" in reason.name else False,
            "impact": impact,     # 对齐异常记录模板
            "action_needed": action_needed,  # 对齐异常记录模板
            "metadata": {
                k: v for k, v in context.items() 
                if k not in ["recent_news", "trace_id", "symbol", "direction"]
            }
        }
        try:
            with open(self.log_file, 'a') as f:
                f.write(json.dumps(log_entry) + "\n")
        except IOError:
            logger.debug("[TradeGuard] 拒绝日志写入失败")


# ============================================================
# 4. 动作优先级仲裁器 (P0-2 新增)
# ============================================================

class ActionPriority(Enum):
    """动作优先级 (数值越小优先级越高)"""
    STOPLOSS_CLOSE = 0       # 止损守护进程的风控动作 (最高)
    SYSTEM_RISK_CUT = 1      # 系统级风控 (RISK_OFF/HALT 清仓)
    PRE_TRADE_BLOCK = 2      # 交易前门禁拦截
    SCHEDULER_OPEN = 3       # 主调度器策略开仓
    SCHEDULER_ADD = 4        # 主调度器加仓
    POST_AUDIT_SUGGEST = 5   # 盘后复盘建议 (最低，无执行权)


class PendingAction:
    """待执行动作"""
    def __init__(self, symbol: str, action_type: str, priority: ActionPriority,
                 source: str, trace_id: str = "", metadata: Dict[str, Any] = None):
        self.symbol = symbol
        self.action_type = action_type  # "OPEN", "CLOSE", "ADD", "REDUCE", "REVERSE"
        self.priority = priority
        self.source = source            # "exit_monitor", "risk_engine", "scheduler", "audit"
        self.trace_id = trace_id
        self.metadata = metadata or {}
        self.timestamp = time.time()

    def __repr__(self):
        return (f"PendingAction(symbol={self.symbol}, type={self.action_type}, "
                f"priority={self.priority.value}, source={self.source})")


class ConflictArbiter:
    """
    动作冲突仲裁器 (P0-2 新增)

    职责:
    - 接收所有待执行动作
    - 按优先级排序，当同一仓位有多个动作冲突时，选择最保守的动作
    - 保证止损 > 风控 > 门禁 > 策略 > 审计
    - 防止重复开仓/平仓
    """

    def __init__(self, data_dir: str = "data"):
        self._recent_actions: Dict[str, List[PendingAction]] = {}  # symbol -> [actions]
        self._resolved_log: Path = Path(data_dir) / "conflict_resolutions.jsonl"
        self._resolved_log.parent.mkdir(parents=True, exist_ok=True)

    def submit(self, action: PendingAction) -> None:
        """提交一个待执行动作"""
        symbol = action.symbol
        if symbol not in self._recent_actions:
            self._recent_actions[symbol] = []
        self._recent_actions[symbol].append(action)

    def resolve(self, symbol: str) -> Optional[PendingAction]:
        """
        仲裁指定标的的待执行动作，返回最终允许执行的动作

        规则:
        1. 如果有 CLOSE 动作 (止损/系统风控)，取消所有 OPEN/ADD
        2. 如果有 STOPLOSS 动作，它是唯一允许执行的
        3. 同类型动作取优先级最高的
        4. 记录仲裁结果
        """
        actions = self._recent_actions.get(symbol, [])
        if not actions:
            return None

        # 按优先级排序
        actions.sort(key=lambda a: (a.priority.value, a.timestamp))
        winner = actions[0]

        # 冲突检测
        if len(actions) > 1:
            conflict_types = set(a.action_type for a in actions)
            sources = set(a.source for a in actions)
            if len(conflict_types) > 1 or len(sources) > 1:
                self._log_resolution(symbol, actions, winner)

        # 清理已仲裁的动作
        self._recent_actions.pop(symbol, None)
        return winner

    def check_conflict(self, symbol: str, new_action_type: str,
                       new_source: str) -> Optional[str]:
        """
        预判: 即将执行的动作是否与已有的高优先级动作冲突

        Returns:
            冲突原因描述 (str) 或 None (无冲突)
        """
        actions = self._recent_actions.get(symbol, [])

        for existing in actions:
            # 止损/风控 CLOSE 冲突: 阻止所有新开仓
            if existing.priority.value <= ActionPriority.SYSTEM_RISK_CUT.value:
                if new_action_type in ("OPEN", "ADD"):
                    return (f"冲突拦截: {existing.source} 的 {existing.action_type} "
                            f"(优先级{existing.priority.value}) > 新动作 {new_action_type}")

            # 重复动作检测 (同仓位同动作)
            if existing.action_type == new_action_type and existing.source != new_source:
                if time.time() - existing.timestamp < 60:  # 1 分钟内
                    return (f"重复拦截: {new_action_type} 已被 {existing.source} "
                            f"在 {int(time.time() - existing.timestamp)}s 前提交")

        return None

    def clear(self, symbol: str = None) -> None:
        """清理待执行动作"""
        if symbol:
            self._recent_actions.pop(symbol, None)
        else:
            self._recent_actions.clear()

    def _log_resolution(self, symbol: str, actions: List[PendingAction],
                        winner: PendingAction) -> None:
        """记录仲裁结果"""
        record = {
            "timestamp": time.time(),
            "datetime": datetime.now().isoformat(),
            "symbol": symbol,
            "winner": {
                "action_type": winner.action_type,
                "priority": winner.priority.value,
                "source": winner.source,
                "trace_id": winner.trace_id,
            },
            "conflicts": [
                {
                    "action_type": a.action_type,
                    "priority": a.priority.value,
                    "source": a.source,
                    "trace_id": a.trace_id,
                }
                for a in actions if a != winner
            ],
        }
        try:
            with open(self._resolved_log, 'a') as f:
                f.write(json.dumps(record) + "\n")
        except IOError:
            logger.debug("[ConflictArbiter] 仲裁日志写入失败")


# ============================================================
# 5. 幂等动作追踪器 (P0-3 新增)
# ============================================================

class IdempotentTracker:
    """
    交易动作幂等追踪器 (P0-3 新增)

    确保同一仓位 + 同一动作 + 同一来源只执行一次
    """

    def __init__(self, ttl_seconds: int = 300):
        self._executed: Dict[str, float] = {}  # key -> timestamp
        self._ttl = ttl_seconds

    def _make_key(self, symbol: str, action_type: str, source: str) -> str:
        return f"{symbol}:{action_type}:{source}"

    def is_duplicate(self, symbol: str, action_type: str, source: str) -> bool:
        """检查是否为重复动作"""
        key = self._make_key(symbol, action_type, source)
        if key in self._executed:
            if time.time() - self._executed[key] < self._ttl:
                return True
            else:
                del self._executed[key]
        return False

    def record(self, symbol: str, action_type: str, source: str) -> None:
        """记录一个已执行动作"""
        key = self._make_key(symbol, action_type, source)
        self._executed[key] = time.time()

    def cleanup(self) -> int:
        """清理过期记录"""
        now = time.time()
        expired = [k for k, v in self._executed.items() if now - v > self._ttl]
        for k in expired:
            del self._executed[k]
        return len(expired)
