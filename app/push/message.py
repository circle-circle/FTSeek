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

from app.core.trade_identity import trade_identity


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
        identity = _identity_line(p)
        if identity:
            lines.append(identity)
        if self.category == Category.TRADE_ENTRY:
            lines.append(f"📈 成交价: ${float(p.get('price', 0) or 0):.2f}  数量: {int(p.get('qty', 0) or 0)} 股")
            # 2026-07-23: 计划价与偏离显性化 (DEB 实证: 偏离度与输赢无关, 但可观测防意外滑点)
            _pe = p.get("planned_entry")
            if _pe:
                _dev = p.get("entry_dev_pct")
                _dev_s = f" (偏离 {float(_dev):+.1f}%)" if _dev is not None else ""
                lines.append(f"📐 计划价: ${float(_pe):.2f}{_dev_s}")
        else:
            _pnl = float(p.get("pnl", 0) or 0)
            _psign = "+" if _pnl >= 0 else "-"
            # 2026-08-21 (用户裁"落"): BE 出场分类后缀 — 消除"保本名义亏损"歧义.
            #   GAP=触发时已坠穿保本线≥5bp(瀑布截停, 亏损=坠落本身); SLIP=门口成交滑点残差; OK=净正.
            _er = str(p.get("exit_reason", "?") or "?")
            _bec = p.get("be_exit_class")
            if _bec == "GAP":
                _bd = p.get("be_drift_bps")
                _bd_s = f"(坠{abs(float(_bd)):.0f}bp)" if _bd is not None else ""
                _er += f" 🪂瀑布止损{_bd_s}"
            elif _bec == "SLIP":
                _er += " ·滑点残差"
            elif _bec == "OK":
                _er += " ✓"
            # 2026-08-27 同markdown渲染器: 净额主口径 (飞书文本版).
            _pn = p.get("pnl_net")
            if _pn is not None:
                _pn = float(_pn)
                _nsg = "+" if _pn >= 0 else "-"
                _fee = p.get("fees")
                _fee_s = f"/费${float(_fee):.2f}" if _fee is not None else ""
                lines.append(f"💰 实现盈亏: {_nsg}${abs(_pn):.2f} (毛{_psign}${abs(_pnl):.2f}{_fee_s})  原因: {_er}")
            else:
                lines.append(f"💰 实现盈亏: {_psign}${abs(_pnl):.2f}  原因: {_er}")
            # 2026-07-23: 平仓路径画像显性化 (MFE/MAE/表态时点/capture — E shadow 同源口径)
            if p.get("entry_price") and p.get("exit_price"):
                _held = p.get("held_h")
                _held_s = f" (持有 {float(_held):.1f}h)" if _held is not None else ""
                lines.append(f"📈 入场 ${float(p['entry_price']):.2f} → 出场 ${float(p['exit_price']):.2f}{_held_s}")
            if p.get("mfe_pct") is not None:
                _t2m = p.get("time_to_mfe_trading_min") or p.get("time_to_mfe")
                _t2s = f" ({float(_t2m):.0f}min到峰)" if _t2m else ""
                _cap = p.get("capture_pct")
                _cap_s = f"  capture {float(_cap):.0f}%" if _cap is not None else ""
                lines.append(f"📊 路径: MFE {float(p['mfe_pct']):.2f}%{_t2s} / MAE {float(p.get('mae_pct', 0) or 0):.2f}%{_cap_s}")
            # [2026-09-16 用户裁"同意按建议执行"第1条] 生效SL观测性: 区分初始线 vs 紧SL线 (ARM 9/15 案)
            _eff_sl = p.get("effective_sl")
            if _eff_sl:
                _ep = float(p.get("entry_price", 0) or 0)
                _eff_pct = f" ({(float(_eff_sl) - _ep) / _ep * 100:+.1f}%)" if _ep > 0 else ""
                _eff_src = "早期紧SL(规则0.5)" if p.get("tight_sl_triggered") else "生效止损"
                lines.append(f"🛡️ 触发线: ${float(_eff_sl):.2f}{_eff_pct} ← {_eff_src}")
            _e60 = p.get("e60_ok")
            if _e60 is not None:
                lines.append("⏱️ 60min表态: " + ("✓ 已表态" if _e60 else "⚠️ 未表态(MFE<0.3%)"))
        if src:
            _sd = p.get("src_detail")
            lines.append(f"📡 来源: {src}" + (f" · {_sd}" if _sd else ""))
        _sl = p.get("sl")
        _tp = p.get("tp")
        if _sl or _tp:
            _px = float(p.get("price", 0) or 0)
            sl_s = f"${float(_sl):.2f}" if _sl else "N/A"
            if _sl and _px > 0:
                sl_s += f" ({(float(_sl) - _px) / _px * 100:+.1f}%)"
            tp_s = " / ".join(f"${float(x):.2f}" for x in (_tp or [])[:3]) or "N/A"
            if _tp and _px > 0:
                tp_s += f" ({(float(_tp[0]) - _px) / _px * 100:+.1f}%)"
            lines.append(f"🛡️ 止损: {sl_s}  🎯 止盈: {tp_s}")
            # 2026-08-14 止损可见性: β收紧后的有效止损 (退出实际生效值, 与展示止损区分)
            if p.get("eff_sl") is not None:
                _eff = float(p["eff_sl"])
                _eff_pct = f" ({(_eff - _px) / _px * 100:+.1f}%)" if _px > 0 else ""
                lines.append(f"⚡ 有效止损(β收紧): ${_eff:.2f}{_eff_pct} ← 实际生效")
        if p.get("inv_price"):
            lines.append(f"🧯 论点证伪价: ${float(p['inv_price']):.2f} (A-lite shadow)")
        _prof = _profile_line(p)
        if _prof:
            lines.append(f"🏷️ 画像: {_prof}")
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
        identity = _identity_line(p)
        head = "✅ 成交通知" if self.category == Category.TRADE_ENTRY else None
        if head is None:
            # 2026-08-27 (用户核查RIVN净亏显示🟢): 图标按净额着色 — 毛正净负的保本单
            #   (SLIP/GAP残差) 不再显示🟢. 无pnl_net时退回毛口径.
            _judge = p.get("pnl_net")
            _judge = float(_judge) if _judge is not None else float(p.get("pnl", 0) or 0)
            head = "🟢 平仓通知" if _judge >= 0 else "🔴 平仓通知"
        lines = [f"### [FTSeek-US] {head} | 🇺🇸 美股", "",
                 f"**📍 标的**: {symbol}  **📊 方向**: {dir_cn}"]
        if identity:
            lines.append(f"**{identity}**")
        if self.category == Category.TRADE_ENTRY:
            lines.append(f"**📈 成交价**: ${float(p.get('price', 0) or 0):.2f}  **📦 数量**: {int(p.get('qty', 0) or 0)} 股")
            _pe = p.get("planned_entry")
            if _pe:
                _dev = p.get("entry_dev_pct")
                _dev_s = f" (偏离 {float(_dev):+.1f}%)" if _dev is not None else ""
                lines.append(f"**📐 计划价**: ${float(_pe):.2f}{_dev_s}")
        else:
            _pnl = float(p.get("pnl", 0) or 0)
            _psign = "+" if _pnl >= 0 else "-"
            # 2026-08-21 (用户裁"落"): BE 出场分类后缀 (同飞书口径): GAP/SLIP/OK.
            _er = str(p.get("exit_reason", "?") or "?")
            _bec = p.get("be_exit_class")
            if _bec == "GAP":
                _bd = p.get("be_drift_bps")
                _bd_s = f"(坠{abs(float(_bd)):.0f}bp)" if _bd is not None else ""
                _er += f" 🪂瀑布止损{_bd_s}"
            elif _bec == "SLIP":
                _er += " ·滑点残差"
            elif _bec == "OK":
                _er += " ✓"
            # 2026-08-27 (用户核查RIVN): 净额主口径 — 毛正净负的保本单显示真净亏,
            #   毛值/费用降为附注; 无pnl_net退回毛口径 (与标题同源逻辑).
            _pn = p.get("pnl_net")
            if _pn is not None:
                _pn = float(_pn)
                _nsg = "+" if _pn >= 0 else "-"
                _fee = p.get("fees")
                _fee_s = f"/费${float(_fee):.2f}" if _fee is not None else ""
                lines.append(f"**💰 实现盈亏**: {_nsg}${abs(_pn):.2f} (毛{_psign}${abs(_pnl):.2f}{_fee_s})  **📉 原因**: {_er}")
            else:
                lines.append(f"**💰 实现盈亏**: {_psign}${abs(_pnl):.2f}  **📉 原因**: {_er}")
            if p.get("entry_price") and p.get("exit_price"):
                _held = p.get("held_h")
                _held_s = f" (持有 {float(_held):.1f}h)" if _held is not None else ""
                lines.append(f"**📈 路径**: 入场 ${float(p['entry_price']):.2f} → 出场 ${float(p['exit_price']):.2f}{_held_s}")
            if p.get("mfe_pct") is not None:
                _t2m = p.get("time_to_mfe_trading_min") or p.get("time_to_mfe")
                _t2s = f" ({float(_t2m):.0f}min到峰)" if _t2m else ""
                _cap = p.get("capture_pct")
                _cap_s = f"  capture {float(_cap):.0f}%" if _cap is not None else ""
                lines.append(f"**📊 MFE/MAE**: {float(p['mfe_pct']):.2f}%{_t2s} / {float(p.get('mae_pct', 0) or 0):.2f}%{_cap_s}")
            # [2026-09-16 用户裁"同意按建议执行"第1条] 生效SL观测性 (同飞书文本口径)
            _eff_sl = p.get("effective_sl")
            if _eff_sl:
                _ep = float(p.get("entry_price", 0) or 0)
                _eff_pct = f" ({(float(_eff_sl) - _ep) / _ep * 100:+.1f}%)" if _ep > 0 else ""
                _eff_src = "早期紧SL(规则0.5)" if p.get("tight_sl_triggered") else "生效止损"
                lines.append(f"**🛡️ 触发线**: ${float(_eff_sl):.2f}{_eff_pct} ← {_eff_src}")
            _e60 = p.get("e60_ok")
            if _e60 is not None:
                lines.append("**⏱️ 60min表态**: " + ("✓ 已表态" if _e60 else "⚠️ 未表态(MFE<0.3%)"))
        if src:
            _sd = p.get("src_detail")
            lines.append(f"**📡 来源**: {src}" + (f" · {_sd}" if _sd else ""))
        _sl = p.get("sl")
        _tp = p.get("tp")
        if _sl or _tp:
            _px = float(p.get("price", 0) or 0)
            sl_s = f"${float(_sl):.2f}" if _sl else "N/A"
            if _sl and _px > 0:
                sl_s += f" ({(float(_sl) - _px) / _px * 100:+.1f}%)"
            tp_s = " / ".join(f"${float(x):.2f}" for x in (_tp or [])[:3]) or "N/A"
            if _tp and _px > 0:
                tp_s += f" ({(float(_tp[0]) - _px) / _px * 100:+.1f}%)"
            lines.append("")
            lines.append(f"**🛡️ 止损**: {sl_s}  **🎯 止盈**: {tp_s}")
            # 2026-08-14 止损可见性: β收紧后的有效止损 (退出实际生效值, 与展示止损区分)
            if p.get("eff_sl") is not None:
                _eff = float(p["eff_sl"])
                _eff_pct = f" ({(_eff - _px) / _px * 100:+.1f}%)" if _px > 0 else ""
                lines.append(f"**⚡ 有效止损(β收紧)**: ${_eff:.2f}{_eff_pct} ← 实际生效")
        if p.get("inv_price"):
            lines.append(f"**🧯 论点证伪价**: ${float(p['inv_price']):.2f} (A-lite shadow)")
        _prof = _profile_line(p)
        if _prof:
            lines.append(f"**🏷️ 画像**: {_prof}")
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
def _profile_line(p: Dict[str, Any]) -> str:
    """画像行: cohort 分组 / horizon / streak 等分桶标签拼接 (有则显示).

    2026-07-23: 把当日已落库的分桶画像显性化到通知 (cohort=A/B 实验组,
    horizon=持有期档, emergence_streak=UE 浮现连续天数, tier=UE 证据层级)。
    """
    parts = []
    if p.get("cohort"):
        parts.append(f"cohort {p['cohort']}")
    if p.get("horizon"):
        parts.append(f"horizon {p['horizon']}")
    if p.get("self_tier"):
        parts.append(str(p["self_tier"]))
    _stk = p.get("emergence_streak")
    if _stk and int(_stk) > 1:
        parts.append(f"续期D{int(_stk)}")
    if p.get("conf_note"):
        parts.append(str(p["conf_note"]))
    return " · ".join(parts)


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
    key = str(source_type).strip().upper()
    return {
        "MAIN_PIPELINE": "主线策略 (LLM投票)",
        "MANUAL": "人工交易",
        "POSITION_SYNC": "🔄 历史仓位同步",
        "PLAYBOOK_SIM": "Playbook 模拟盘 (DA/UE)",
        "PLAYBOOK_LIVE": "Playbook 实盘 (DA/UE)",
    }.get(key, str(source_type))


def _identity_line(p: Dict[str, Any]) -> str:
    env = {"real": "实盘", "sim": "模拟", "test": "测试", "unknown": "未标注"}.get(
        str(p.get("trade_env") or ""), ""
    )
    phase = str(p.get("trade_phase") or "")
    if env and phase:
        return f"🏷️ 口径: {env} · {phase}"
    return f"🏷️ 口径: {env}" if env else ""


# 便捷构造器 — 给上游模块提供语义化 API
def trade_entry(symbol: str, direction: str, qty: int, price: float,
                strategy: str = "main", trace_id: Optional[str] = None,
                sl: Optional[float] = None, tp: Optional[list] = None,
                signal_strength: Optional[int] = None,
                signal_confidence: Optional[float] = None,
                voting: Optional[Dict] = None,
                source_type: Optional[str] = None,
                extra: Optional[Dict[str, Any]] = None) -> PushMessage:
    """成交开仓 (NORMAL + 立即推送 — 成交是关键事件, 不聚合).

    富字段 (sl/tp/signal_strength/signal_confidence/voting/source_type) 可选,
    提供则渲染富卡 (钉钉 markdown / 飞书多行), 不提供则精简版。
    extra (2026-07-23): 附加画像字段直并 payload — planned_entry/entry_dev_pct/
    cohort/horizon/self_tier/emergence_streak/inv_price/src_detail/conf_note。
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
    if extra:
        for _k, _v in extra.items():
            if _v is not None:
                _payload[_k] = _v
    _payload.update(trade_identity(
        symbol,
        source_type,
        _payload.get("trade_mode") or _payload.get("mode") or _payload.get("account_type"),
        _payload.get("session") or _payload.get("entry_session"),
    ))
    return PushMessage(
        title=f"✅ {symbol} {direction} 成交",
        body=f"qty={qty} price=${price:.2f} strategy={strategy}",
        level=Level.NORMAL,
        category=Category.TRADE_ENTRY,
        dedup_key=f"entry:{symbol}:{trace_id or 'na'}",
        payload=_payload,
        trace_id=trace_id,
    )


def _exit_reason_cn(reason: str, tight_sl: bool = False,
                    be_class: Optional[str] = None) -> str:
    """平仓原因 → 简短中文标签 (未知原因原样返回 → 不丢信息).

    [2026-09-18 用户令\"其他按建议执行\"第⑤条] 推送可见性闭合: 标题(钉钉 markdown
    `title` = 通知栏/会话列表摘要)原来只有净额, 读者要展开卡片才知道是哪种出场;
    `SL_INITIAL` 更分不清\"初始线砍\"还是\"规则0.5 紧SL砍\"。此处只做显示映射,
    零行为变更 (触发与否仍由 `tight_sl_triggered` 元数据决定, 本函数不判定)。
    """
    _raw = str(reason or "").strip()
    _key = _raw.upper()
    _label = {
        "SL_BREAKEVEN": "保本线",
        "SL_TRAIL_LOCK": "锁利线",
        "SL_INITIAL": "止损(紧SL)" if tight_sl else "止损(初始线)",
        "SL_FIXED_PCT": "止损(固定%)",
        "OVERNIGHT_GAP_STOP": "隔夜跳空止损",
        "TAKE_PROFIT": "止盈",
        "PRE_CLOSE_EXIT": "收盘平仓",
        "PATIENT_EXIT_EOD": "收盘平仓",
        "EOD_FORCE_CLOSE": "EOD平仓",
        "TIME_EXIT": "时间退出",
        "DEAD_SIGNAL_EXIT": "死信号退出",
        "MFE_STALL_EXIT": "动能枯竭退出",
        "BROKER_GHOST_CLOSE": "幽灵仓清理",
        "GHOST_CLEANUP": "幽灵仓清理",
        "RECONCILE_GHOST_CLEANUP": "幽灵仓清理",
        "EXTERNAL_CLOSE": "外部平仓",
    }.get(_key, _raw or "?")
    if be_class == "GAP":
        _label += "🪂"
    return _label


def trade_exit(symbol: str, direction: str, pnl: float, exit_reason: str,
               strategy: str = "main", trace_id: Optional[str] = None,
               signal_strength: Optional[int] = None,
               signal_confidence: Optional[float] = None,
               source_type: Optional[str] = None,
               extra: Optional[Dict[str, Any]] = None,
               fees: Optional[float] = None,
               pnl_net: Optional[float] = None) -> PushMessage:
    """平仓 (NORMAL + 立即推送 — 平仓是关键事件, 不聚合).

    富字段可选, 提供则渲染富卡, 不提供则精简版。
    extra (2026-07-23): 附加画像字段直并 payload — entry_price/exit_price/held_h/
    mfe_pct/mae_pct/time_to_mfe/capture_pct/e60_ok/cohort/horizon/src_detail。
    fees/pnl_net (2026-07-30, DEB-fee-net-pnl G5): 提供时标题以净额为主口径
    (净$X (毛$Y/费$Z)), 图标按净额着色; pnl 参数始终传毛值。
    标题尾缀出场原因 (2026-09-18): 见 _exit_reason_cn。
    """
    # 2026-07-16: 移除聚合. 根因: trade_entry 聚合导致7/15成交无通知.
    # 修复: 所有平仓立即推送, 不走聚合桶. 大额亏损也走 NORMAL (不再降级 LOW).
    sign = "+" if pnl >= 0 else ""
    _payload = {"symbol": symbol, "direction": direction, "pnl": pnl,
                "exit_reason": exit_reason, "strategy": strategy, "kind": "exit"}
    for _k, _v in (("signal_strength", signal_strength),
                   ("signal_confidence", signal_confidence),
                   ("source_type", source_type),
                   ("fees", fees), ("pnl_net", pnl_net)):
        if _v is not None:
            _payload[_k] = _v
    if extra:
        for _k, _v in extra.items():
            if _v is not None:
                _payload[_k] = _v
    _payload.update(trade_identity(
        symbol,
        source_type,
        _payload.get("trade_mode") or _payload.get("mode") or _payload.get("account_type"),
        _payload.get("session") or _payload.get("exit_session") or _payload.get("entry_session"),
    ))
    _er_s = _exit_reason_cn(exit_reason, bool(_payload.get("tight_sl_triggered")),
                            _payload.get("be_exit_class"))
    if pnl_net is not None:
        _nsign = "+" if pnl_net >= 0 else ""
        _fee_txt = f"/费${fees:.2f}" if fees is not None else ""
        _title = (f"{'🟢' if pnl_net >= 0 else '🔴'} {symbol} 平仓 "
                  f"净{_nsign}${pnl_net:.2f} (毛{sign}${pnl:.2f}{_fee_txt}) · {_er_s}")
    else:
        _title = f"{'🟢' if pnl >= 0 else '🔴'} {symbol} 平仓 {sign}${pnl:.2f} · {_er_s}"
    return PushMessage(
        title=_title,
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
