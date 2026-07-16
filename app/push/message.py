"""PushMessage 数据契约 (2026-05-16)

统一推送消息格式 — 所有上游模块构造 PushMessage 后通过 PushHub.push() 发送.
旧 send_text / notify_order_filled 等接口保留, 内部转换为 PushMessage.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional
import time
import hashlib


class Level(Enum):
    """优先级: 决定是否走聚合 + fallback 强度"""
    INFO = "info"           # 系统启动/关闭等, 可聚合, 失败仅本地记录
    LOW = "low"             # 单笔交易成交, 可 5min 聚合
    NORMAL = "normal"       # 平仓/计划生成, 立即推送
    HIGH = "high"           # 风控触发/protection, 立即推送 + 多通道
    CRITICAL = "critical"   # kill_switch/RISK_OFF, 立即推 + 全通道 + flag 文件


class Category(Enum):
    """业务类别: 决定路由 + 格式化模板"""
    TRADE_ENTRY = "trade_entry"        # 成交开仓
    TRADE_EXIT = "trade_exit"          # 平仓
    SIGNAL = "signal"                  # 信号生成 (一般不直接推, 聚合后推)
    PLAN_PREMARKET = "plan_premarket"  # 盘前计划
    PLAN_OPEN_CHECK = "plan_open_check"  # 开盘确认
    REVIEW_DAILY = "review_daily"      # 每日复盘
    REVIEW_WEEKLY = "review_weekly"    # 每周复盘
    REVIEW_MONTHLY = "review_monthly"  # 每月复盘
    SHADOW_COMPARE = "shadow_compare"  # 影子对比
    LLM_PLAN = "llm_plan"              # LLM 推荐
    RISK_ALERT = "risk_alert"          # 风控告警
    SYSTEM = "system"                  # 系统事件 (启动/关闭/配置变更)
    HEALTH = "health"                  # 通道健康 ping


@dataclass
class PushMessage:
    """统一推送消息

    最简用法: PushMessage(title="X 成交", body="qty=100 price=$45")
    完整用法: 指定 level/category/dedup_key/aggregate_key
    """
    title: str
    body: str
    level: Level = Level.NORMAL
    category: Category = Category.SYSTEM
    # dedup: 1min 窗口内同 key 去重 (None = 不去重)
    dedup_key: Optional[str] = None
    # aggregate: 同 key 5min 内消息合并为 1 条 (None = 不聚合)
    aggregate_key: Optional[str] = None
    # 业务数据 (聚合时用于汇总, 例: pnl 求和)
    payload: Dict[str, Any] = field(default_factory=dict)
    # trace 链路追踪
    trace_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def auto_dedup_key(self) -> str:
        """若未提供 dedup_key, 用 title+body 摘要"""
        if self.dedup_key:
            return self.dedup_key
        seed = f"{self.category.value}|{self.title}|{self.body[:100]}"
        return hashlib.md5(seed.encode("utf-8")).hexdigest()[:16]

    def is_aggregable(self) -> bool:
        """是否参与聚合: 仅 LOW + 显式 aggregate_key"""
        return self.level == Level.LOW and self.aggregate_key is not None

    def is_critical(self) -> bool:
        return self.level in (Level.CRITICAL, Level.HIGH)

    def render_text(self) -> str:
        """渲染为单条文本 (旧 send_text 兼容)"""
        icon = self._icon()
        return f"{icon} {self.title}\n{self.body}"

    def _icon(self) -> str:
        if self.level == Level.CRITICAL:
            return "🚨"
        if self.level == Level.HIGH:
            return "⚠️"
        if self.category == Category.TRADE_ENTRY:
            return "📈"
        if self.category == Category.TRADE_EXIT:
            return "📉"
        if self.category in (Category.REVIEW_DAILY, Category.REVIEW_WEEKLY, Category.REVIEW_MONTHLY):
            return "📊"
        if self.category in (Category.PLAN_PREMARKET, Category.PLAN_OPEN_CHECK, Category.LLM_PLAN):
            return "📋"
        if self.category == Category.SHADOW_COMPARE:
            return "🪞"
        if self.category == Category.HEALTH:
            return "💓"
        return "ℹ️"


# 便捷构造器 — 给上游模块提供语义化 API
def trade_entry(symbol: str, direction: str, qty: int, price: float,
                strategy: str = "main", trace_id: Optional[str] = None) -> PushMessage:
    """成交开仓 (NORMAL + 立即推送 — 成交是关键事件, 不聚合)"""
    # 2026-07-16: LOW→NORMAL, 移除聚合. 根因: 7/15 9笔成交在5min聚合窗口内被聚合,
    # 但 flush 时 send 失败 (可能 PushHub async worker 异常), 且 logger.debug 吞掉错误.
    # 修复: 升级为 NORMAL 立即推送, 不走聚合桶. 每笔成交都应即时通知.
    return PushMessage(
        title=f"✅ {symbol} {direction} 成交",
        body=f"qty={qty} price=${price:.2f} strategy={strategy}",
        level=Level.NORMAL,
        category=Category.TRADE_ENTRY,
        dedup_key=f"entry:{symbol}:{trace_id or 'na'}",
        payload={"symbol": symbol, "direction": direction, "qty": qty,
                 "price": price, "strategy": strategy, "kind": "entry"},
        trace_id=trace_id,
    )


def trade_exit(symbol: str, direction: str, pnl: float, exit_reason: str,
               strategy: str = "main", trace_id: Optional[str] = None) -> PushMessage:
    """平仓 (NORMAL + 立即推送 — 平仓是关键事件, 不聚合)"""
    # 2026-07-16: 移除聚合. 根因: trade_entry 聚合导致7/15成交无通知.
    # 修复: 所有平仓立即推送, 不走聚合桶. 大额亏损也走 NORMAL (不再降级 LOW).
    sign = "+" if pnl >= 0 else ""
    return PushMessage(
        title=f"{'🟢' if pnl >= 0 else '🔴'} {symbol} 平仓 {sign}${pnl:.2f}",
        body=f"{direction} reason={exit_reason} strategy={strategy}",
        level=Level.NORMAL,
        category=Category.TRADE_EXIT,
        dedup_key=f"exit:{symbol}:{trace_id or 'na'}",
        payload={"symbol": symbol, "direction": direction, "pnl": pnl,
                 "exit_reason": exit_reason, "strategy": strategy, "kind": "exit"},
        trace_id=trace_id,
    )


def critical_alert(title: str, body: str, dedup_key: Optional[str] = None) -> PushMessage:
    """关键告警 (CRITICAL + 全通道 + flag 兜底)"""
    return PushMessage(
        title=title, body=body,
        level=Level.CRITICAL, category=Category.RISK_ALERT,
        dedup_key=dedup_key,
    )


def approaching_stop(symbol: str, direction: str, sl_distance_pct: float,
                    sl_distance_atr: float, severity: str = "WARNING") -> PushMessage:
    """接近止损预警 (HIGH, 立即推送)

    Args:
        symbol: 标的
        direction: LONG / SHORT
        sl_distance_pct: 距止损百分比
        sl_distance_atr: 距止损 ATR 倍数
        severity: WARNING (< 1.0 ATR) 或 CRITICAL (< 0.5 ATR)
    """
    lvl = Level.CRITICAL if severity == "CRITICAL" else Level.HIGH
    icon = "🚨" if severity == "CRITICAL" else "⚠️"
    return PushMessage(
        title=f"{icon} {symbol} 接近止损",
        body=(
            f"{direction} 距止损线 {sl_distance_pct:.1f}% ({sl_distance_atr:.1f}×ATR)\n"
            f"级别: {severity}"
        ),
        level=lvl,
        category=Category.RISK_ALERT,
        dedup_key=f"approaching_stop:{symbol}",
        payload={
            "symbol": symbol, "direction": direction,
            "sl_distance_pct": round(sl_distance_pct, 2),
            "sl_distance_atr": round(sl_distance_atr, 2),
            "severity": severity,
        },
    )


def daily_review(date: str, body: str) -> PushMessage:
    """每日复盘 (NORMAL + 同日去重)"""
    return PushMessage(
        title=f"FTSeek-US 每日复盘 ({date})",
        body=body,
        level=Level.NORMAL, category=Category.REVIEW_DAILY,
        dedup_key=f"daily_review:{date}",
    )
