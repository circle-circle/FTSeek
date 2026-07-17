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
        """渲染为单条文本 (旧 send_text 兼容).

        交易类 (TRADE_ENTRY/TRADE_EXIT) 若 payload 带富字段, 输出增强多行 emoji 文本
        (复刻 legacy 飞书版式); 其余类别维持 icon+title+body。
        """
        if self.category in (Category.TRADE_ENTRY, Category.TRADE_EXIT):
            try:
                rich = self._render_trade_text()
                if rich:
                    return rich
            except Exception:
                pass
        icon = self._icon()
        return f"{icon} {self.title}\n{self.body}"

    def render_markdown(self) -> str:
        """渲染为 markdown (钉钉富卡). 交易类输出富格式; 非交易类回退 render_text()."""
        if self.category in (Category.TRADE_ENTRY, Category.TRADE_EXIT):
            try:
                md = self._render_trade_markdown()
                if md:
                    return md
            except Exception:
                pass
        return self.render_text()

    def _render_trade_text(self) -> str:
        """交易类增强多行文本 (飞书)."""
        p = self.payload or {}
        symbol = p.get("symbol", "?")
        direction = str(p.get("direction", "") or "")
        is_long = direction.upper() in ("BUY", "LONG")
        dir_cn = "买入/做多" if is_long else "卖出/做空"
        src = _source_cn(p.get("source_type"))
        lines = [self.title, ""]
        lines.append(f"📍 标的: {symbol}  |  方向: {dir_cn}")
        if self.category == Category.TRADE_ENTRY:
            lines.append(f"📈 成交价: ${float(p.get('price', 0) or 0):.2f}  数量: {int(p.get('qty', 0) or 0)} 股")
        else:
            _pnl = float(p.get("pnl", 0) or 0)
            _psign = "+" if _pnl >= 0 else "-"
            lines.append(f"💰 实现盈亏: {_psign}${abs(_pnl):.2f}  原因: {p.get('exit_reason', '?')}")
        if src:
            lines.append(f"📡 来源: {src}")
        _sl = p.get("sl")
        _tp = p.get("tp")
        if _sl or _tp:
            sl_s = f"${float(_sl):.2f}" if _sl else "N/A"
            tp_s = " / ".join(f"${float(x):.2f}" for x in (_tp or [])[:3]) or "N/A"
            lines.append(f"🛡️ 止损: {sl_s}  🎯 止盈: {tp_s}")
        ss = p.get("signal_strength")
        if ss:
            sc = p.get("signal_confidence")
            bar = _strength_bar(int(ss))
            conf_s = f"  置信度 {float(sc):.0%}" if sc is not None else ""
            lines.append(f"🧠 强度 {int(ss)}/100 {bar}{conf_s}")
        vote = p.get("voting")
        if isinstance(vote, dict) and vote:
            fs = vote.get("final_strength")
            cl = vote.get("consensus_level")
            vb = []
            if fs is not None:
                vb.append(f"最终评分 {fs}")
            if cl is not None:
                vb.append(f"一致性 {float(cl):.0%}")
            if vb:
                lines.append("🗳️ " + "  ".join(vb))
        strat = p.get("strategy")
        if strat:
            lines.append(f"策略: {strat}")
        return "\n".join(lines)

    def _render_trade_markdown(self) -> str:
        """交易类富 markdown (钉钉)."""
        p = self.payload or {}
        symbol = p.get("symbol", "?")
        direction = str(p.get("direction", "") or "")
        is_long = direction.upper() in ("BUY", "LONG")
        dir_cn = "买入/做多" if is_long else "卖出/做空"
        src = _source_cn(p.get("source_type"))
        head = "✅ 成交通知" if self.category == Category.TRADE_ENTRY else (
            "🟢 平仓通知" if float(p.get("pnl", 0) or 0) >= 0 else "🔴 平仓通知")
        lines = [f"### [FTSeek-US] {head} | 🇺🇸 美股", "",
                 f"**📍 标的**: {symbol}  **📊 方向**: {dir_cn}"]
        if self.category == Category.TRADE_ENTRY:
            lines.append(f"**📈 成交价**: ${float(p.get('price', 0) or 0):.2f}  **📦 数量**: {int(p.get('qty', 0) or 0)} 股")
        else:
            _pnl = float(p.get("pnl", 0) or 0)
            _psign = "+" if _pnl >= 0 else "-"
            lines.append(f"**💰 实现盈亏**: {_psign}${abs(_pnl):.2f}  **📉 原因**: {p.get('exit_reason', '?')}")
        if src:
            lines.append(f"**📡 来源**: {src}")
        _sl = p.get("sl")
        _tp = p.get("tp")
        if _sl or _tp:
            sl_s = f"${float(_sl):.2f}" if _sl else "N/A"
            tp_s = " / ".join(f"${float(x):.2f}" for x in (_tp or [])[:3]) or "N/A"
            lines.append("")
            lines.append(f"**🛡️ 止损**: {sl_s}  **🎯 止盈**: {tp_s}")
        ss = p.get("signal_strength")
        if ss:
            sc = p.get("signal_confidence")
            bar = _strength_bar(int(ss))
            lines.append("")
            lines.append(f"**🧠 决策依据**: 强度 {int(ss)}/100 {bar}")
            if sc is not None:
                lines.append(f"- 置信度: {float(sc):.0%}")
        vote = p.get("voting")
        if isinstance(vote, dict) and vote:
            fs = vote.get("final_strength")
            cl = vote.get("consensus_level")
            if fs is not None or cl is not None:
                lines.append(f"- 投票: 最终评分 {fs if fs is not None else '?'} / 一致性 "
                             f"{float(cl):.0%}" if cl is not None else f"- 投票: 最终评分 {fs}")
        strat = p.get("strategy")
        if strat:
            lines.append(f"- 策略: {strat}")
        return "\n".join(lines)

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


# ─── 渲染 helper ───
def _strength_bar(strength: int) -> str:
    """0-100 强度 → 10 格进度条."""
    try:
        filled = max(0, min(10, int(strength) // 10))
    except Exception:
        filled = 0
    return "`" + "█" * filled + "░" * (10 - filled) + "`"


def _source_cn(source_type) -> str:
    """来源代码 → 中文标注. None/空 → 空串; 未知 → 原样返回."""
    if not source_type:
        return ""
    return {
        "MAIN_PIPELINE": "主线策略 (LLM投票)",
        "MANUAL": "人工交易",
        "POSITION_SYNC": "🔄 历史仓位同步",
        "playbook_sim": "Playbook 模拟盘 (DA/UE)",
        "playbook_live": "Playbook 实盘 (DA/UE)",
    }.get(str(source_type), str(source_type))


# 便捷构造器 — 给上游模块提供语义化 API
def trade_entry(symbol: str, direction: str, qty: int, price: float,
                strategy: str = "main", trace_id: Optional[str] = None,
                sl: Optional[float] = None, tp: Optional[list] = None,
                signal_strength: Optional[int] = None,
                signal_confidence: Optional[float] = None,
                voting: Optional[Dict] = None,
                source_type: Optional[str] = None) -> PushMessage:
    """成交开仓 (NORMAL + 立即推送 — 成交是关键事件, 不聚合).

    富字段 (sl/tp/signal_strength/signal_confidence/voting/source_type) 可选,
    提供则渲染富卡 (钉钉 markdown / 飞书多行), 不提供则精简版。
    """
    # 2026-07-16: LOW→NORMAL, 移除聚合. 根因: 7/15 9笔成交在5min聚合窗口内被聚合,
    # 但 flush 时 send 失败 (可能 PushHub async worker 异常), 且 logger.debug 吞掉错误.
    # 修复: 升级为 NORMAL 立即推送, 不走聚合桶. 每笔成交都应即时通知.
    _payload = {"symbol": symbol, "direction": direction, "qty": qty,
                "price": price, "strategy": strategy, "kind": "entry"}
    for _k, _v in (("sl", sl), ("tp", tp), ("signal_strength", signal_strength),
                   ("signal_confidence", signal_confidence), ("voting", voting),
                   ("source_type", source_type)):
        if _v is not None:
            _payload[_k] = _v
    return PushMessage(
        title=f"✅ {symbol} {direction} 成交",
        body=f"qty={qty} price=${price:.2f} strategy={strategy}",
        level=Level.NORMAL,
        category=Category.TRADE_ENTRY,
        dedup_key=f"entry:{symbol}:{trace_id or 'na'}",
        payload=_payload,
        trace_id=trace_id,
    )


def trade_exit(symbol: str, direction: str, pnl: float, exit_reason: str,
               strategy: str = "main", trace_id: Optional[str] = None,
               signal_strength: Optional[int] = None,
               signal_confidence: Optional[float] = None,
               source_type: Optional[str] = None) -> PushMessage:
    """平仓 (NORMAL + 立即推送 — 平仓是关键事件, 不聚合).

    富字段可选, 提供则渲染富卡, 不提供则精简版。
    """
    # 2026-07-16: 移除聚合. 根因: trade_entry 聚合导致7/15成交无通知.
    # 修复: 所有平仓立即推送, 不走聚合桶. 大额亏损也走 NORMAL (不再降级 LOW).
    sign = "+" if pnl >= 0 else ""
    _payload = {"symbol": symbol, "direction": direction, "pnl": pnl,
                "exit_reason": exit_reason, "strategy": strategy, "kind": "exit"}
    for _k, _v in (("signal_strength", signal_strength),
                   ("signal_confidence", signal_confidence),
                   ("source_type", source_type)):
        if _v is not None:
            _payload[_k] = _v
    return PushMessage(
        title=f"{'🟢' if pnl >= 0 else '🔴'} {symbol} 平仓 {sign}${pnl:.2f}",
        body=f"{direction} reason={exit_reason} strategy={strategy}",
        level=Level.NORMAL,
        category=Category.TRADE_EXIT,
        dedup_key=f"exit:{symbol}:{trace_id or 'na'}",
        payload=_payload,
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
