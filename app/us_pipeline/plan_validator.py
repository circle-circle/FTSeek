"""
战前计划校验器 (Plan Validator)

LLM 输出后，用真实数据硬校验。Flag (标注) 为主, 仅极端情况 Reject。
自动修正明显不合理的 SL/TP 参数 (保守修正, 不改变方向)。

设计原则:
  - 不杀死有效交易想法 (RSI=76 在强上升趋势中仍然可行)
  - 只拦截明显错误 (入场区偏离现价 5%+, R:R <1.0)
  - 自动修正温和参数 (SL 过窄 → 扩展, TP 不足 → 延伸)

校验规则:
  1. RSI Guard: LONG+RSI>75 或 SHORT+RSI<25 → FLAG
  2. 入场区偏离: >5% → REJECT, >3% → FLAG+修正
  3. 止损过窄: <0.5×ATR → FLAG+扩展到 1.0×ATR
  4. 风险收益比: <1.5 → FLAG+延伸 TP 到 2.0 R:R
  5. 板块逆风: LONG in 弱势板块 → FLAG
  6. 趋势矛盾: LONG+DOWN 或 SHORT+UP → FLAG

版本: 1.0 (2026-06-12)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ftseek.us_pipeline.plan_validator")


@dataclass
class ValidationResult:
    """校验结果"""
    rejected_targets: List[Tuple[Dict, str]] = field(default_factory=list)
    flagged_targets: List[Tuple[Dict, List[str]]] = field(default_factory=list)
    adjusted_targets: List[Tuple[Dict, Dict]] = field(default_factory=list)  # (original, corrected)
    overall_issues: List[str] = field(default_factory=list)

    @property
    def total_rejected(self) -> int:
        return len(self.rejected_targets)

    @property
    def total_flagged(self) -> int:
        return len(self.flagged_targets)

    def summary(self) -> str:
        parts = []
        if self.rejected_targets:
            parts.append(f"🚫 拒绝 {self.total_rejected} 个")
        if self.flagged_targets:
            parts.append(f"⚠️ 标注 {self.total_flagged} 个")
        if self.adjusted_targets:
            parts.append(f"🔧 自动修正 {len(self.adjusted_targets)} 个")
        return " | ".join(parts) if parts else "✅ 全部通过"


class PlanValidator:
    """
    战前计划硬校验器

    使用 PreMarketScanner 的真实技术指标 + 板块数据
    校验 LLM 输出的作战计划。
    """

    def __init__(
        self,
        technicals: Dict[str, Dict],
        sectors: Dict[str, Any],
        sector_symbol_map: Dict[str, Dict],
        economic_events: Optional[List[Dict]] = None,
        price_calibration: Optional[Dict[str, Dict]] = None,
    ):
        self.technicals = technicals
        self.sectors = sectors
        self.sector_map = sector_symbol_map
        self.economic_events = economic_events or []
        self.prices = price_calibration or {}

    def validate_and_annotate(self, plan: Dict) -> Dict:
        """
        校验完整计划并标注结果

        在原 plan 上修改 (就地标注):
        - 每个 target 新增 validation_warnings: List[str]
        - 被拒绝的 target: status → "CANCELLED"
        - 自动修正的 SL/TP 直接修改
        - 顶层新增 _validation_summary: str

        Args:
            plan: LLM 输出的作战计划 JSON

        Returns:
            标注后的 plan (同一对象, 就地修改)
        """
        result = self._validate(plan)

        # 标注到 plan
        for target, reason in result.rejected_targets:
            target["status"] = "CANCELLED"
            target["open_check_status"] = "CANCELLED"
            target["cancellation_reason"] = f"Validation: {reason}"
            target["validation_warnings"] = [reason]
            logger.warning(
                f"[计划校验] 🚫 {target.get('symbol', '?')} 拒绝: {reason}"
            )

        for target, warnings in result.flagged_targets:
            existing = target.get("validation_warnings", [])
            target["validation_warnings"] = existing + warnings
            for w in warnings:
                logger.info(
                    f"[计划校验] ⚠️ {target.get('symbol', '?')}: {w}"
                )

        # 注入技术指标到每个 target
        for target in plan.get("targets", []):
            symbol = target.get("symbol", "")
            tech = self.technicals.get(symbol, {})
            if tech:
                target["technical_summary"] = self._build_tech_summary(symbol, tech)
                target["risk_reward_ratio"] = self._calc_rr(target)
                target["atr_risk"] = self._calc_atr_risk(target, tech)
                target["sector"] = self.sector_map.get(symbol, {}).get("sector", "Unknown")
                target["sector_strength"] = self.sector_map.get(symbol, {}).get(
                    "sector_strength", "NEUTRAL"
                )

        # 注入顶层元数据
        sector_summary = self.sectors.get("summary", {})
        plan["sector_rotation"] = {
            "strong": sector_summary.get("strong", []),
            "weak": sector_summary.get("weak", []),
        }
        plan["economic_events"] = self.economic_events

        plan["_validation_summary"] = result.summary()

        if result.total_rejected > 0 or result.total_flagged > 0:
            logger.info(f"[计划校验] {result.summary()}")

        return plan

    # ── 内部校验逻辑 ────────────────────────────────────────────

    def _validate(self, plan: Dict) -> ValidationResult:
        result = ValidationResult()
        targets = plan.get("targets", [])

        for target in targets:
            symbol = target.get("symbol", "")
            if not symbol:
                result.overall_issues.append("Target missing symbol")
                continue

            # 跳过已取消的
            if target.get("status") == "CANCELLED" or target.get("open_check_status") == "CANCELLED":
                continue

            rejected, warnings, corrections = self._validate_target(target)

            if rejected:
                result.rejected_targets.append((target, rejected))
            elif warnings:
                result.flagged_targets.append((target, warnings))

            # 应用自动修正
            if corrections:
                original = {k: target.get(k) for k in ("stop_loss", "take_profit", "entry_zone")}
                for key, value in corrections.items():
                    target[key] = value
                result.adjusted_targets.append((original, corrections))

        return result

    def _validate_target(self, target: Dict) -> Tuple[Optional[str], List[str], Dict]:
        """
        校验单个 target

        Returns:
            (rejection_reason or None, warnings list, corrections dict)
        """
        symbol = target.get("symbol", "")
        direction = target.get("direction", "LONG").upper()
        entry_zone = target.get("entry_zone", [])
        stop_loss = target.get("stop_loss", 0)
        take_profit = target.get("take_profit", 0)

        warnings = []
        corrections = {}

        tech = self.technicals.get(symbol, {})
        sector_info = self.sector_map.get(symbol, {})
        price_info = self.prices.get(symbol, {})

        # 获取关键数值
        rsi = tech.get("rsi", 50)
        atr = tech.get("atr", 0)
        trend = tech.get("trend", "NEUTRAL")
        current_price = price_info.get("current_price", tech.get("price", 0))

        # 计算入场区中点 (类型安全转换)
        entry_low = 0.0
        entry_high = 0.0
        entry_mid = 0.0
        if isinstance(entry_zone, list) and len(entry_zone) == 2:
            try:
                entry_low = float(entry_zone[0])
                entry_high = float(entry_zone[1])
                entry_mid = (entry_low + entry_high) / 2
            except (TypeError, ValueError):
                entry_mid = 0

        # ── Rule 0: 基本格式检查 ──
        if not entry_zone or not isinstance(entry_zone, list) or len(entry_zone) != 2 or entry_mid <= 0:
            return "entry_zone 格式无效或非数值", [], {}
        if entry_low <= 0 or entry_high <= 0 or entry_low >= entry_high:
            return f"entry_zone 无效: [{entry_low}, {entry_high}]", [], {}

        # ── Rule 1: 入场区偏离 ──
        if current_price > 0 and entry_mid > 0:
            deviation_pct = abs(entry_mid - current_price) / current_price * 100
            if deviation_pct > 5:
                return (
                    f"入场区偏离 {deviation_pct:.1f}% (现价=${current_price:.2f}, "
                    f"入场中点=${entry_mid:.2f})",
                    [],
                    {},
                )
            elif deviation_pct > 3:
                warnings.append(
                    f"入场区轻度偏离 {deviation_pct:.1f}% "
                    f"(现价=${current_price:.2f}, 入场中点=${entry_mid:.2f})"
                )
                # 自动修正: 调整入场区到现价 ±1%
                # 同时平移 SL/TP, 保持原始 R:R 结构
                shift = current_price - entry_mid
                corrections["entry_zone"] = [
                    round(current_price * 0.99, 2),
                    round(current_price * 1.01, 2),
                ]
                if stop_loss > 0:
                    corrections["stop_loss"] = round(stop_loss + shift, 2)
                if take_profit > 0:
                    corrections["take_profit"] = round(take_profit + shift, 2)
                entry_mid = current_price  # 更新用于后续计算
                # 同步更新局部变量, 使 Rule 3/4 基于修正后的值
                stop_loss = corrections.get("stop_loss", stop_loss)
                take_profit = corrections.get("take_profit", take_profit)

        # ── Rule 2: RSI Guard ──
        if direction == "LONG" and rsi > 75:
            warnings.append(f"RSI={rsi:.1f} 超买, LONG 存在均值回归风险")
        elif direction == "SHORT" and rsi < 25:
            warnings.append(f"RSI={rsi:.1f} 超卖, SHORT 存在逼空风险")

        # ── Rule 3: ATR-Based Stop Loss ──
        if atr > 0 and entry_mid > 0 and stop_loss > 0:
            if direction == "LONG":
                sl_distance = entry_mid - stop_loss
            else:
                sl_distance = stop_loss - entry_mid

            sl_atr_multiple = sl_distance / atr if atr > 0 else 0

            if sl_distance < 0:
                warnings.append(f"止损方向错误 (SL=${stop_loss:.2f} 应在入场区{'下方' if direction == 'LONG' else '上方'})")
                # 自动修正: 设置 SL = entry ∓ 1.5×ATR
                if direction == "LONG":
                    corrections["stop_loss"] = round(entry_mid - 1.5 * atr, 2)
                else:
                    corrections["stop_loss"] = round(entry_mid + 1.5 * atr, 2)
            elif sl_atr_multiple < 0.5:
                warnings.append(
                    f"止损过窄 ({sl_atr_multiple:.1f}×ATR, 应 ≥ 0.5×ATR, "
                    f"ATR=${atr:.2f})"
                )
                # 自动修正: 扩展到 1.0×ATR
                if direction == "LONG":
                    corrections["stop_loss"] = round(entry_mid - 1.0 * atr, 2)
                else:
                    corrections["stop_loss"] = round(entry_mid + 1.0 * atr, 2)

        # ── Rule 4: Risk:Reward Ratio ──
        if entry_mid > 0 and stop_loss > 0 and take_profit > 0:
            sl = corrections.get("stop_loss", stop_loss)
            tp = take_profit

            if direction == "LONG":
                risk = entry_mid - sl
                reward = tp - entry_mid
            else:
                risk = sl - entry_mid
                reward = entry_mid - tp

            if risk > 0 and reward > 0:
                rr = reward / risk
                if rr < 1.0:
                    return f"R:R={rr:.1f}:1 不足 (最低 1.5:1)", warnings, corrections
                elif rr < 1.5:
                    warnings.append(f"R:R={rr:.1f}:1 偏低 (建议 ≥ 2.0:1)")
                    # 自动修正: 延伸 TP 到 2.0 R:R
                    if direction == "LONG":
                        corrections["take_profit"] = round(entry_mid + 2.0 * risk, 2)
                    else:
                        corrections["take_profit"] = round(entry_mid - 2.0 * risk, 2)

            elif reward <= 0:
                warnings.append(f"止盈方向错误 (TP=${tp:.2f})")

        # ── Rule 5: 板块逆风 ──
        sector_strength = sector_info.get("sector_strength", "NEUTRAL")
        if direction == "LONG" and sector_strength == "WEAK":
            sector_cn = sector_info.get("sector_cn", sector_info.get("sector", ""))
            warnings.append(f"板块逆风: {sector_cn} 为弱势板块, LONG 风险增加")
        elif direction == "SHORT" and sector_strength == "STRONG":
            sector_cn = sector_info.get("sector_cn", sector_info.get("sector", ""))
            warnings.append(f"板块逆风: {sector_cn} 为强势板块, SHORT 风险增加")

        # ── Rule 6: 趋势矛盾 ──
        if direction == "LONG" and trend == "DOWN":
            warnings.append(f"趋势矛盾: 日线趋势 DOWN, LONG 逆势")
        elif direction == "SHORT" and trend == "UP":
            warnings.append(f"趋势矛盾: 日线趋势 UP, SHORT 逆势")

        rejection = None
        return rejection, warnings, corrections

    # ── 辅助方法 ────────────────────────────────────────────────

    def _build_tech_summary(self, symbol: str, tech: Dict) -> str:
        """构建技术摘要字符串 (用于 target.technical_summary)"""
        rsi = tech.get("rsi", 50)
        macd_hist = tech.get("macd_hist", 0)
        trend = tech.get("trend", "NEUTRAL")
        atr = tech.get("atr", 0)
        atr_pct = tech.get("atr_percent", 0)

        macd_label = "金叉" if macd_hist > 0 else "死叉"
        trend_cn = {"UP": "UP", "DOWN": "DOWN", "NEUTRAL": "FLAT"}.get(trend, "FLAT")

        return (
            f"RSI={rsi:.1f} | MACD{macd_label} | "
            f"趋势{trend_cn} | ATR=${atr:.2f}({atr_pct:.1f}%)"
        )

    def _calc_rr(self, target: Dict) -> float:
        """计算风险收益比"""
        entry_zone = target.get("entry_zone", [])
        sl = target.get("stop_loss", 0)
        tp = target.get("take_profit", 0)
        direction = target.get("direction", "LONG").upper()

        if not entry_zone or len(entry_zone) != 2 or sl <= 0 or tp <= 0:
            return 0.0

        entry_mid = (float(entry_zone[0]) + float(entry_zone[1])) / 2

        if direction == "LONG":
            risk = entry_mid - sl
            reward = tp - entry_mid
        else:
            risk = sl - entry_mid
            reward = entry_mid - tp

        if risk <= 0:
            return 0.0
        return round(reward / risk, 2)

    def _calc_atr_risk(self, target: Dict, tech: Dict) -> Dict:
        """计算 ATR 风险指标"""
        atr = tech.get("atr", 0)
        if atr <= 0:
            return {}

        entry_zone = target.get("entry_zone", [])
        sl = target.get("stop_loss", 0)
        tp = target.get("take_profit", 0)

        if not entry_zone or len(entry_zone) != 2:
            return {"atr": round(atr, 2)}

        entry_mid = (float(entry_zone[0]) + float(entry_zone[1])) / 2

        sl_distance = abs(entry_mid - sl) if sl > 0 else 0
        tp_distance = abs(tp - entry_mid) if tp > 0 else 0

        return {
            "atr": round(atr, 2),
            "sl_distance_atr": round(sl_distance / atr, 2) if atr > 0 else 0,
            "tp_distance_atr": round(tp_distance / atr, 2) if atr > 0 else 0,
        }
