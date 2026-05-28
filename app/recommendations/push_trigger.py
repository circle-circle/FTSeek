"""盘前推荐自动推送触发器 (2026-05-28)

职责:
    1. 09:25 ET — 推送最终推荐 (BUY/SELL 列表 + 市场定调 + 行业配置)
    2. 16:30 ET — 推送盘后准确率报告 (今日命中率 + 周累计 + 标杆/反例)

集成:
    - 复用 app/push 统一推送中枢 (DingTalk → Feishu → FlagFile fallback)
    - 配置读取自 unified_config.yaml 之 recommendations 段
    - 每日幂等: 已推送日期落盘到 data/recommendations_push_state.json

调用约定:
    >>> trigger = PremarketPushTrigger()
    >>> trigger.send_final(recommendations_dict)         # 09:25 ET
    >>> trigger.send_accuracy_report(accuracy_dict)      # 16:30 ET

异常策略:
    - 任何路径下推送失败 → log.warning 并返回 False, 不抛异常
    - dry_run=true → 仅 log 不实际下发
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.push import Category, Level, PushMessage, get_push_hub

logger = logging.getLogger(__name__)


# ─── 序号 emoji 表 ───────────────────────────────────────────────
_MEDAL_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉"}
_NUMBER_EMOJIS = {
    4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟",
}


def _rank_emoji(rank: int) -> str:
    """1-3 名用奖牌, 4-10 用 keycap 数字, 之后纯数字标号."""
    if rank in _MEDAL_EMOJIS:
        return _MEDAL_EMOJIS[rank]
    if rank in _NUMBER_EMOJIS:
        return _NUMBER_EMOJIS[rank]
    return f"#{rank}"


def _direction_arrow(direction: str) -> str:
    d = (direction or "").upper()
    if d in ("BUY", "LONG"):
        return "▲"
    if d in ("SELL", "SHORT"):
        return "▼"
    return "■"


def _confidence_label(value: Any) -> str:
    """将数字置信度归一到 HIGH/MEDIUM/LOW; 已为字符串则直接返回大写形式."""
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if score >= 0.7:
        return "HIGH"
    if score >= 0.5:
        return "MEDIUM"
    return "LOW"


def _fmt_num(value: Any, fmt: str = "{:.2f}", default: str = "—") -> str:
    if value is None or value == "":
        return default
    try:
        return fmt.format(float(value))
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any, default: str = "—") -> str:
    """传入 0.032 → '+3.2%'; 传入 3.2 也尝试识别 (>1 视为已百分化)."""
    if value is None or value == "":
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    pct = num * 100 if abs(num) <= 1.5 else num
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


class PremarketPushTrigger:
    """盘前推荐自动推送触发器 (单例使用即可, 线程安全)."""

    DEFAULT_STATE_FILE = "data/recommendations_push_state.json"

    def __init__(
        self,
        config: Any = None,
        push_hub: Any = None,
        state_file: Optional[str] = None,
    ) -> None:
        self._config = config
        self._push_hub = push_hub
        self._state_file = state_file
        self._lock = threading.Lock()

    # ─── 配置读取 ────────────────────────────────────────────────
    def _cfg(self) -> Any:
        if self._config is not None:
            return self._config
        try:
            from app.core.config_container import get_config
            self._config = get_config()
        except Exception as e:
            logger.debug(f"[PushTrigger] config 加载失败, 使用默认: {e}")
            self._config = None
        return self._config

    def _get(self, path: str, default: Any) -> Any:
        cfg = self._cfg()
        if cfg is None:
            return default
        try:
            return cfg.get_dotted(path, default)
        except Exception:
            try:
                return cfg.get(path, default)
            except Exception:
                return default

    def _is_enabled(self) -> bool:
        return bool(self._get("recommendations.enabled", True))

    def _is_dry_run(self) -> bool:
        return bool(self._get("recommendations.dry_run", False))

    def _max_recommendations(self) -> int:
        try:
            return int(self._get("recommendations.max_recommendations", 10))
        except (TypeError, ValueError):
            return 10

    # ─── PushHub ─────────────────────────────────────────────────
    def _hub(self) -> Any:
        if self._push_hub is not None:
            return self._push_hub
        try:
            self._push_hub = get_push_hub()
        except Exception as e:
            logger.warning(f"[PushTrigger] PushHub 不可用: {e}")
            self._push_hub = None
        return self._push_hub

    # ─── 幂等状态 ────────────────────────────────────────────────
    def _state_path(self) -> Path:
        if self._state_file:
            p = Path(self._state_file)
            if p.is_absolute():
                return p
        try:
            cfg = self._cfg()
            base = Path(cfg.paths.base_dir) if cfg is not None else Path.cwd()
        except Exception:
            base = Path.cwd()
        rel = self._state_file or self.DEFAULT_STATE_FILE
        return base / rel

    def _load_state(self) -> Dict[str, List[str]]:
        path = self._state_path()
        if not path.exists():
            return {"final_pushed_dates": [], "accuracy_pushed_dates": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.setdefault("final_pushed_dates", [])
            data.setdefault("accuracy_pushed_dates", [])
            return data
        except Exception as e:
            logger.warning(f"[PushTrigger] 状态文件读取失败 ({path}): {e}, 将重建")
            return {"final_pushed_dates": [], "accuracy_pushed_dates": []}

    def _save_state(self, state: Dict[str, List[str]]) -> None:
        path = self._state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[PushTrigger] 状态写入失败 ({path}): {e}")

    def _already_pushed(self, kind: str, date_str: str) -> bool:
        state = self._load_state()
        key = f"{kind}_pushed_dates"
        return date_str in state.get(key, [])

    def _mark_pushed(self, kind: str, date_str: str) -> None:
        with self._lock:
            state = self._load_state()
            key = f"{kind}_pushed_dates"
            arr = state.setdefault(key, [])
            if date_str not in arr:
                arr.append(date_str)
            if len(arr) > 60:
                state[key] = arr[-60:]
            self._save_state(state)

    # ─── 公开 API: 最终推荐 ──────────────────────────────────────
    def send_final(self, recommendations: Dict[str, Any]) -> bool:
        """推送最终推荐 (09:25 ET 调用)."""
        try:
            if not self._is_enabled():
                logger.info("[PushTrigger] recommendations.enabled=false, 跳过 final 推送")
                return False
            if not isinstance(recommendations, dict) or not recommendations:
                logger.warning("[PushTrigger] send_final: recommendations 为空, 跳过")
                return False
            date_str = str(recommendations.get("date") or datetime.now().strftime("%Y-%m-%d"))
            if self._already_pushed("final", date_str):
                logger.info(f"[PushTrigger] {date_str} final 推荐今日已推送, 幂等跳过")
                return False
            content = self.format_recommendation_card(recommendations)
            title = f"美股盘前作战 | {date_str}"
            ok = self._dispatch(
                title=title,
                body=content,
                category=Category.PLAN_PREMARKET,
                level=Level.HIGH,
                dedup_key=f"reco:final:{date_str}",
                payload={"date": date_str, "kind": "premarket_final"},
            )
            if ok:
                self._mark_pushed("final", date_str)
            return ok
        except Exception as e:
            logger.warning(f"[PushTrigger] send_final 异常: {e}", exc_info=True)
            return False

    def send_accuracy_report(self, accuracy_report: Dict[str, Any]) -> bool:
        """推送盘后准确率报告 (16:30 ET 调用)."""
        try:
            if not self._is_enabled():
                logger.info("[PushTrigger] recommendations.enabled=false, 跳过 accuracy 推送")
                return False
            if not isinstance(accuracy_report, dict) or not accuracy_report:
                logger.warning("[PushTrigger] send_accuracy_report: 空报告, 跳过")
                return False
            date_str = str(accuracy_report.get("date") or datetime.now().strftime("%Y-%m-%d"))
            if self._already_pushed("accuracy", date_str):
                logger.info(f"[PushTrigger] {date_str} accuracy 报告今日已推送, 幂等跳过")
                return False
            content = self.format_accuracy_card(accuracy_report)
            title = f"盘后准确率报告 | {date_str}"
            ok = self._dispatch(
                title=title,
                body=content,
                category=Category.REVIEW_DAILY,
                level=Level.NORMAL,
                dedup_key=f"reco:accuracy:{date_str}",
                payload={"date": date_str, "kind": "premarket_accuracy"},
            )
            if ok:
                self._mark_pushed("accuracy", date_str)
            return ok
        except Exception as e:
            logger.warning(f"[PushTrigger] send_accuracy_report 异常: {e}", exc_info=True)
            return False

    # ─── 格式化: 推荐卡片 (交易员决策格式) ────────────────────
    def format_recommendation_card(self, recommendations: Dict[str, Any]) -> str:
        date_str = recommendations.get("date") or datetime.now().strftime("%Y-%m-%d")
        market_tone = (
            recommendations.get("market_regime")
            or recommendations.get("market_tone")
            or "NEUTRAL"
        ).upper()
        vix = recommendations.get("vix")
        vix_str = _fmt_num(vix, "{:.1f}") if vix is not None else "—"

        tickers_raw: List[Dict[str, Any]] = list(
            recommendations.get("recommendations")
            or recommendations.get("tickers")
            or []
        )
        tickers = tickers_raw[: self._max_recommendations()]

        bull_count = sum(
            1 for t in tickers
            if (t.get("action") or t.get("direction") or "").upper() in ("BUY", "LONG")
        )
        bear_count = sum(
            1 for t in tickers
            if (t.get("action") or t.get("direction") or "").upper() in ("SELL", "SHORT")
        )

        # ── 系统信心度 ──
        conviction = recommendations.get("system_conviction")
        debate_summary = recommendations.get("debate_summary") or ""
        arbiter_mode = recommendations.get("arbiter_mode") or ""

        lines: List[str] = []
        lines.append(f"⚔️ 美股盘前作战 | {date_str}")
        lines.append("━━━━━━━━━━━━━━━")

        # 系统健康 + 市场定调
        if conviction is not None:
            if conviction >= 0.7:
                conv_emoji, conv_label = "🟢", "高信心"
            elif conviction >= 0.4:
                conv_emoji, conv_label = "🟡", "中等"
            else:
                conv_emoji, conv_label = "🔴", "低信心/防御模式"
            lines.append(f"{conv_emoji} 系统信心: {conviction:.0%} ({conv_label})")
        lines.append(f"🌍 市场定调: {market_tone} | VIX: {vix_str}")
        lines.append(f"📊 推荐: {bull_count} 看涨 / {bear_count} 看跌")

        if debate_summary:
            lines.append(f"📝 {debate_summary}")
        if arbiter_mode and arbiter_mode != "standard":
            lines.append(f"⚙️ 模式: {arbiter_mode}")

        # ── 风险预警 (最重要的决策信息) ──
        risk_warnings = recommendations.get("risk_warnings") or []
        if risk_warnings:
            lines.append("")
            lines.append("🚨 风险预警")
            for w in risk_warnings[:3]:
                if isinstance(w, str):
                    # 截断到 80 字符保证可读
                    w_text = w[:80] + ("..." if len(w) > 80 else "")
                    lines.append(f"  ⚠️ {w_text}")

        lines.append("")

        # ── 标的推荐 (完整理由) ──
        if not tickers:
            if conviction is not None and conviction < 0.3:
                lines.append("🛑 系统建议: 今日不宜交易")
                lines.append("   信心度过低，数据源或信号质量存在问题。")
                lines.append("   等待系统修复确认后再操作。")
            else:
                lines.append("🔍 今日无高置信度标的，建议观望。")
        else:
            lines.append("📌 今日标的")
            for idx, item in enumerate(tickers, start=1):
                lines.extend(self._format_ticker_block(idx, item))
                lines.append("")

        # ── 持仓操作指令 (辩论增强后) ──
        position_actions = recommendations.get("position_actions") or []
        action_items = recommendations.get("action_items") or []

        if position_actions:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("📤 持仓操作指令")
            for pa in position_actions[:6]:
                if not isinstance(pa, dict):
                    continue
                sym = self._clean_sym(pa.get("symbol", "?"))
                action = pa.get("action", "?").upper()
                rationale = pa.get("rationale") or pa.get("reason") or ""
                zone = pa.get("target_zone") or pa.get("entry_zone")

                action_emoji = {"EXIT": "🔴", "REDUCE": "🟡", "HOLD": "🟢", "TRIM": "🟡"}.get(action, "⚪")
                zone_str = ""
                if isinstance(zone, (list, tuple)) and len(zone) >= 2:
                    zone_str = f" @{zone[0]}-{zone[1]}"

                lines.append(f"  {action_emoji} {sym}: {action}{zone_str}")
                if rationale:
                    r_text = rationale[:70] + ("..." if len(rationale) > 70 else "")
                    lines.append(f"     → {r_text}")

        elif action_items:
            avoid_syms = []
            exit_items = []
            for item in action_items:
                if not isinstance(item, str):
                    continue
                if "回避" in item:
                    avoid_syms.append(item.split(":")[0].strip())
                else:
                    exit_items.append(item)
            if avoid_syms or exit_items:
                lines.append("━━━━━━━━━━━━━━━")
                if avoid_syms:
                    lines.append(f"⛔ 回避: {', '.join(avoid_syms[:8])}" + (f" 等{len(avoid_syms)}只" if len(avoid_syms) > 8 else ""))
                if exit_items:
                    lines.append("📤 操作: " + " | ".join(exit_items[:5]))

        # ── 辩论交叉验证 ──
        debate_consensus = recommendations.get("debate_consensus") or []
        if debate_consensus:
            lines.append("")
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("🔬 辩论交叉验证")
            for item in debate_consensus[:3]:
                if isinstance(item, str):
                    lines.append(f"  • {item[:90]}")

        # ── Claude 独立审计 ──
        claude_audit = recommendations.get("claude_audit") or {}
        if claude_audit:
            audit_lines = self._format_claude_audit(claude_audit)
            if audit_lines:
                lines.append("")
                lines.extend(audit_lines)

        # ── 热点发现 ──
        hot_picks = recommendations.get("hot_picks") or []
        if hot_picks:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("🔥 热点发现 (观察池外)")
            for hp in hot_picks[:5]:
                sym = hp.get("symbol", "?")
                catalyst = hp.get("catalyst", "")
                change = hp.get("change_pct")
                source = hp.get("source", "")
                if change is not None:
                    arrow = "▲" if change > 0 else "▼"
                    change_str = f" {arrow}{abs(change):.1f}%"
                else:
                    change_str = ""
                source_tag = {"premarket_mover": " [盘前异动]", "news_catalyst": " [新闻催化]", "news_text": " [新闻]"}.get(source, "")
                lines.append(f"  💎 {sym}{change_str}{source_tag}")
                if catalyst:
                    lines.append(f"     → {catalyst}")
            lines.append("  ⚠️ 非观察池标的，仅供参考")

        # ── 被拦截的推荐 (信号验证不通过) ──
        blocked = recommendations.get("blocked_recommendations") or []
        if blocked:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("🚫 已拦截 (历史数据验证)")
            for b in blocked[:4]:
                sym = b.get("symbol", "?")
                reason = b.get("reason", "信号质量不足")
                reason_short = reason[:60] + ("..." if len(reason) > 60 else "")
                lines.append(f"  ❌ {sym}: {reason_short}")

        # ── 历史准确率 (如果有) ──
        accuracy = recommendations.get("rolling_accuracy") or {}
        if accuracy:
            lines.append("━━━━━━━━━━━━━━━")
            acc_pct = accuracy.get("accuracy_pct")
            total = accuracy.get("total_tracked", 0)
            if acc_pct is not None and total > 0:
                lines.append(f"📈 近30日准确率: {acc_pct:.0f}% ({accuracy.get('correct',0)}/{total})")

        # ── 尾部 ──
        lines.append("━━━━━━━━━━━━━━━")
        sector_alloc = recommendations.get("sector_allocation") or {}
        if isinstance(sector_alloc, dict) and sector_alloc:
            sector_parts = []
            for name, weight in sector_alloc.items():
                try:
                    pct = float(weight)
                    pct = pct * 100 if pct <= 1.0 else pct
                    sector_parts.append(f"{name} {pct:.0f}%")
                except (TypeError, ValueError):
                    sector_parts.append(f"{name} {weight}")
            lines.append("📊 行业: " + " | ".join(sector_parts))

        cutoff = recommendations.get("data_cutoff") or "09:25 ET"
        market_open = recommendations.get("market_open") or "09:30 ET"
        lines.append(f"⏰ 截止: {cutoff} | 开盘: {market_open}")
        lines.append("💡 本推荐仅供参考，请结合盘面实际走势做出决策")

        return "\n".join(lines)

    def _format_claude_audit(self, audit: Dict[str, Any]) -> List[str]:
        """格式化 Claude 独立审计摘要"""
        lines: List[str] = []
        bias = audit.get("market_bias")
        conf = audit.get("confidence")
        if bias or conf is not None:
            header = "🔍 Claude独立审计"
            if bias:
                header += f" | 偏向: {bias}"
            if conf is not None:
                header += f" | 信心: {conf:.0%}"
            lines.append(header)

        systemic = audit.get("systemic_risks") or []
        if systemic:
            for risk in systemic[:2]:
                if isinstance(risk, str):
                    r_text = risk[:80] + ("..." if len(risk) > 80 else "")
                    lines.append(f"  🔴 {r_text}")

        bears = audit.get("bear_cases") or []
        if bears:
            for bear in bears[:2]:
                if isinstance(bear, str):
                    b_text = bear[:80] + ("..." if len(bear) > 80 else "")
                    lines.append(f"  🐻 {b_text}")
        return lines

    @staticmethod
    def _clean_sym(sym: str) -> str:
        """去除 US. 前缀"""
        s = str(sym).strip()
        for prefix in ("US.", "HK."):
            if s.startswith(prefix):
                s = s[len(prefix):]
        return s

    def _format_ticker_block(self, rank: int, item: Dict[str, Any]) -> List[str]:
        symbol = item.get("symbol") or item.get("ticker") or "?"
        direction = (item.get("action") or item.get("direction") or "").upper() or "BUY"
        arrow = _direction_arrow(direction)

        conf_level = item.get("confidence_level") or item.get("confidence_label")
        conf_score = item.get("confidence_score") or item.get("confidence")
        if conf_level and conf_score is not None:
            try:
                conf_str = f"{conf_level} ({float(conf_score):.2f})"
            except (TypeError, ValueError):
                conf_str = str(conf_level)
        elif conf_level:
            conf_str = str(conf_level)
        elif conf_score is not None:
            conf_str = _confidence_label(conf_score)
        else:
            conf_str = "N/A"

        rank_marker = _rank_emoji(rank)
        sector = item.get("sector")
        sector_tag = f" [{sector}]" if sector else ""
        urgency = item.get("urgency", "").upper()
        urgency_tag = f" ⚡{urgency}" if urgency and urgency not in ("LOW", "") else ""

        # 信号验证徽章
        validation = item.get("validation") or {}
        verdict = validation.get("verdict", "")
        verdict_badge = {"GREEN": "✅", "YELLOW": "🟡", "RED": "🔴"}.get(verdict, "")
        hist_wr = validation.get("historical_wr")
        hist_tag = f" (历史{hist_wr}%WR)" if hist_wr is not None else ""

        head = f"{rank_marker} {verdict_badge} {symbol}{sector_tag} {arrow} {direction}{urgency_tag}{hist_tag}"

        entry_zone = item.get("entry_zone")
        if entry_zone and isinstance(entry_zone, str) and entry_zone:
            entry_str = entry_zone
        else:
            entry_low = item.get("entry_low")
            entry_high = item.get("entry_high")
            if entry_low is not None and entry_high is not None:
                entry_str = f"{_fmt_num(entry_low)}-{_fmt_num(entry_high)}"
            else:
                entry_str = _fmt_num(item.get("entry") or entry_low or entry_high)

        target_str = item.get("target") or ""
        stop_str = item.get("stop_loss") or ""
        rr = item.get("risk_reward")
        rr_str = _fmt_num(rr, "{:.1f}") if rr is not None else "—"
        catalyst = item.get("catalyst") or ""
        rationale = item.get("zh_thesis") or item.get("rationale") or ""

        block: List[str] = [head]
        block.append(f"   入场: {entry_str} | 目标: {target_str} | 止损: {stop_str}")
        info_line = f"   盈亏比: {rr_str}"
        if catalyst:
            info_line += f" | 催化: {catalyst}"
        block.append(info_line)
        # 完整理由 (不截断，这是用户最需要的信息)
        if rationale and rationale.strip() != "综合多源信号给出建议。":
            block.append(f"   📋 {rationale}")

        # 风险因素
        risk_factors = item.get("risk_factors") or []
        if risk_factors and isinstance(risk_factors, list):
            rf_text = "; ".join(str(r) for r in risk_factors[:2] if r)
            if rf_text:
                block.append(f"   ⚠️ 风险: {rf_text}")

        return block

    # ─── 格式化: 准确率卡片 ──────────────────────────────────────
    def format_accuracy_card(self, accuracy_report: Dict[str, Any]) -> str:
        date_str = accuracy_report.get("date") or datetime.now().strftime("%Y-%m-%d")

        today_correct = accuracy_report.get("today_correct")
        today_total = accuracy_report.get("today_total")
        today_acc = accuracy_report.get("today_accuracy")
        today_avg = accuracy_report.get("today_avg_return")

        weekly_correct = accuracy_report.get("weekly_correct")
        weekly_total = accuracy_report.get("weekly_total")
        weekly_acc = accuracy_report.get("weekly_accuracy")

        results: List[Dict[str, Any]] = list(accuracy_report.get("results") or [])
        best = accuracy_report.get("best") or {}
        worst = accuracy_report.get("worst") or {}
        insight = accuracy_report.get("insight") or accuracy_report.get("trend") or ""

        lines: List[str] = []
        lines.append(f"📊 盘后准确率报告 | {date_str}")
        lines.append("━━━━━━━━━━━━━━━")

        if today_correct is not None and today_total is not None:
            try:
                ratio_pct = (
                    float(today_acc) * 100
                    if today_acc is not None
                    else (float(today_correct) / max(1, float(today_total))) * 100
                )
                lines.append(f"📈 今日准确率: {today_correct}/{today_total} ({ratio_pct:.1f}%)")
            except (TypeError, ValueError):
                lines.append(f"📈 今日准确率: {today_correct}/{today_total}")
        elif today_acc is not None:
            lines.append(f"📈 今日准确率: {_fmt_pct(today_acc)}")

        if today_avg is not None:
            lines.append(f"💰 平均收益: {_fmt_pct(today_avg)}")

        if weekly_correct is not None and weekly_total is not None:
            try:
                ratio_pct = (
                    float(weekly_acc) * 100
                    if weekly_acc is not None
                    else (float(weekly_correct) / max(1, float(weekly_total))) * 100
                )
                lines.append(f"📅 本周累计: {weekly_correct}/{weekly_total} ({ratio_pct:.1f}%)")
            except (TypeError, ValueError):
                lines.append(f"📅 本周累计: {weekly_correct}/{weekly_total}")
        elif weekly_acc is not None:
            lines.append(f"📅 本周准确率: {_fmt_pct(weekly_acc)}")

        lines.append("")

        for r in results:
            if not isinstance(r, dict):
                continue
            symbol = r.get("symbol") or r.get("ticker") or "?"
            direction = (r.get("direction") or "BUY").upper()
            ret = r.get("return_pct")
            entry = r.get("entry_price")
            close = r.get("close_price")
            is_correct = r.get("is_correct")
            if is_correct is None:
                try:
                    if direction in ("BUY", "LONG"):
                        is_correct = float(ret) >= 0
                    else:
                        is_correct = float(ret) <= 0
                except (TypeError, ValueError):
                    is_correct = False
            mark = "✅" if is_correct else "❌"
            ret_str = _fmt_pct(ret)
            entry_str = _fmt_num(entry)
            close_str = _fmt_num(close)
            lines.append(f"{mark} {symbol}: {ret_str} (推荐{direction} @{entry_str}, 收盘 {close_str})")

        if results:
            lines.append("")

        if best:
            best_sym = best.get("symbol") or best.get("ticker") or "?"
            lines.append(f"🏆 本周最佳: {best_sym} ({_fmt_pct(best.get('return_pct'))})")
        if worst:
            worst_sym = worst.get("symbol") or worst.get("ticker") or "?"
            lines.append(f"⚠️ 本周最差: {worst_sym} ({_fmt_pct(worst.get('return_pct'))})")

        lines.append("━━━━━━━━━━━━━━━")
        if insight:
            lines.append(f"💡 趋势: {insight}")

        return "\n".join(lines)

    # ─── 内部: 实际下发 ──────────────────────────────────────────
    def _dispatch(
        self,
        title: str,
        body: str,
        category: Category,
        level: Level,
        dedup_key: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if self._is_dry_run():
            logger.info("[PushTrigger·dry_run] title=%s\n%s", title, body)
            return True

        hub = self._hub()
        if hub is None:
            logger.warning("[PushTrigger] PushHub 不可用, 跳过推送 title=%s", title)
            return False

        try:
            msg = PushMessage(
                title=title,
                body=body,
                level=level,
                category=category,
                dedup_key=dedup_key,
                payload=payload or {},
            )
            ok = hub.push(msg)
            if not ok:
                logger.warning("[PushTrigger] PushHub 返回 False, title=%s", title)
            return bool(ok)
        except Exception as e:
            logger.warning(f"[PushTrigger] 下发异常: {e}", exc_info=True)
            return False
