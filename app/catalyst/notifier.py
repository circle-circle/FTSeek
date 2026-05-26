"""
催化辩论引擎 — 通知推送层

双通道推送: 飞书(主) + 钉钉(备)
复用 app.notifier.feishu / app.notifier.dingtalk 已有基础设施

调用点:
- 盘前: detector.py run_premarket_catalyst_scan 完成后
- 盘后: postmarket_review.py run_postmarket_catalyst_review 完成后
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("ftseek_us.catalyst.notifier")


# ─── 格式化 ────────────────────────────────────────────────

_DIRECTION_EMOJI = {
    "LONG": "📈", "SHORT": "📉", "AVOID": "⏸️",
}

_SEVERITY_EMOJI = {
    "CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡",
}

_EVENT_TYPE_CN = {
    "REGULATORY": "监管",
    "DELISTING": "退市风险",
    "FDA_APPROVAL": "FDA批准",
    "FDA_REJECTION": "FDA驳回",
    "EARNINGS_BEAT": "财报超预期",
    "EARNINGS_MISS": "财报不及预期",
    "DOWNGRADE": "评级下调",
    "UPGRADE": "评级上调",
    "MACRO": "宏观",
    "BREAKING": "突发",
    "PRICE_ANOMALY": "价格异动",
}


def format_premarket_report(verdicts: List, market_ctx: Optional[Dict] = None) -> str:
    """格式化盘前催化辩论报告"""
    if not verdicts:
        return ""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"🧪 盘前催化辩论报告 ({now_str})",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"催化事件: {len(verdicts)} 个",
    ]

    # 市场背景
    if market_ctx:
        vix = market_ctx.get("vix", "N/A")
        regime = market_ctx.get("regime", "N/A")
        spy = market_ctx.get("spy_trend", "N/A")
        lines.append(f"市场: VIX={vix} | SPY={spy} | Regime={regime}")

    lines.append("")

    # 按 strength 降序排列
    sorted_verdicts = sorted(
        verdicts,
        key=lambda v: v.strength if hasattr(v, 'strength') else v.get("strength", 0),
        reverse=True,
    )

    for i, v in enumerate(sorted_verdicts, 1):
        # 兼容 dataclass 和 dict
        if hasattr(v, 'to_dict'):
            vd = v.to_dict()
        else:
            vd = v

        direction = vd.get("direction", "AVOID")
        emoji = _DIRECTION_EMOJI.get(direction, "❓")
        symbol = vd.get("symbol", "?")
        strength = vd.get("strength", 0)
        confidence = vd.get("confidence", 0)
        consensus = vd.get("consensus_level", 0)
        reasoning = vd.get("reasoning", "")[:80]
        sl = vd.get("sl_pct", -0.05)
        tp = vd.get("tp_pct", 0.10)
        horizon = vd.get("time_horizon", "swing")
        event_type = vd.get("event_type", "")
        severity = vd.get("severity", "")

        # 格式化百分比
        sl_str = f"{sl:+.0%}" if isinstance(sl, float) and abs(sl) < 1 else f"{sl}"
        tp_str = f"{tp:+.0%}" if isinstance(tp, float) and abs(tp) < 1 else f"{tp}"

        sev_emoji = _SEVERITY_EMOJI.get(severity, "")
        type_cn = _EVENT_TYPE_CN.get(event_type, event_type)

        lines.append(f"{i}. {emoji} {symbol} | {direction} | 强度{strength} | 共识{consensus:.0%}")
        if type_cn:
            lines.append(f"   {sev_emoji} {type_cn}")
        lines.append(f"   推理: {reasoning}")
        if direction != "AVOID":
            lines.append(f"   SL={sl_str} TP={tp_str} | {horizon}")

        # 风险警告
        risks = vd.get("risk_warnings", [])
        if risks:
            lines.append(f"   ⚠️ {'; '.join(risks[:2])}")
        lines.append("")

    # 操作建议汇总
    longs = [v for v in sorted_verdicts
             if (v.direction if hasattr(v, 'direction') else v.get("direction")) == "LONG"]
    shorts = [v for v in sorted_verdicts
              if (v.direction if hasattr(v, 'direction') else v.get("direction")) == "SHORT"]
    avoids = [v for v in sorted_verdicts
              if (v.direction if hasattr(v, 'direction') else v.get("direction")) == "AVOID"]

    if longs or shorts or avoids:
        lines.append("📋 汇总:")
        if longs:
            symbols = ", ".join(
                (v.symbol if hasattr(v, 'symbol') else v.get("symbol", "?")) for v in longs
            )
            lines.append(f"  📈 看多: {symbols}")
        if shorts:
            symbols = ", ".join(
                (v.symbol if hasattr(v, 'symbol') else v.get("symbol", "?")) for v in shorts
            )
            lines.append(f"  📉 看空: {symbols}")
        if avoids:
            symbols = ", ".join(
                (v.symbol if hasattr(v, 'symbol') else v.get("symbol", "?")) for v in avoids
            )
            lines.append(f"  ⏸️ 回避: {symbols}")

    lines.append("")
    lines.append("─" * 20)
    lines.append("⚠️ 催化辩论结论仅供参考, 最终交易决策由风控引擎判定")

    return "\n".join(lines)


def format_postmarket_report(review_result: Dict) -> str:
    """格式化盘后催化回看报告"""
    if not review_result or review_result.get("skipped"):
        return ""

    total = review_result.get("total", 0)
    if total == 0:
        return ""

    hits = review_result.get("hits", 0)
    misses = review_result.get("misses", 0)
    accuracy = review_result.get("accuracy", 0)
    lessons = review_result.get("lessons", [])
    details = review_result.get("details", [])

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    accuracy_emoji = "🟢" if accuracy >= 0.6 else ("🟡" if accuracy >= 0.4 else "🔴")

    lines = [
        f"📊 盘后催化回看报告 ({now_str})",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"命中率: {accuracy_emoji} {hits}/{total} ({accuracy:.0%})",
        f"",
    ]

    # 详细预测对比
    if details:
        lines.append("📋 逐笔回看:")
        for d in details[:8]:  # 最多8条
            symbol = d.get("symbol", "?")
            predicted = d.get("predicted", "?")
            actual_ret = d.get("actual_return")
            status = d.get("status", "?")
            emoji = "✅" if status == "hit" else ("❌" if status == "miss" else "⚪")

            pred_emoji = _DIRECTION_EMOJI.get(predicted, "")
            actual_str = f"{actual_ret:+.1%}" if actual_ret is not None else "N/A"

            lines.append(f"  {emoji} {symbol} | 预测{pred_emoji}{predicted} | 实际{actual_str}")

            if status == "miss":
                reasoning = d.get("reasoning", "")[:60]
                if reasoning:
                    lines.append(f"     原推理: {reasoning}")

        lines.append("")

    # 教训
    if lessons:
        lines.append("📚 教训:")
        for lesson in lessons[:3]:
            lines.append(f"  • {lesson[:80]}")
        lines.append("")

    lines.append("─" * 20)
    lines.append("💡 教训已注入 LessonInjector, 下次盘前辩论自动参考")

    return "\n".join(lines)


# ─── 推送 ──────────────────────────────────────────────────

def notify(message: str) -> tuple:
    """双通道推送: 飞书(主) + 钉钉(备)

    Returns:
        (ok_feishu, ok_dingtalk)
    """
    if not message:
        return False, False

    ok_feishu = False
    ok_dingtalk = False

    # 飞书
    try:
        from app.notifier.feishu import FeishuNotifier
        feishu = FeishuNotifier()
        if feishu.webhook_url:
            ok_feishu = feishu.send_text(message)
            logger.info("[CatalystNotify] 飞书推送%s", "✓" if ok_feishu else "✗")
        else:
            logger.debug("[CatalystNotify] FEISHU_WEBHOOK_URL_US 未配置")
    except Exception as e:
        logger.warning("[CatalystNotify] 飞书推送异常: %s", e)

    # 钉钉
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        dingtalk = DingTalkNotifier()
        if dingtalk.enabled:
            ok_dingtalk = dingtalk.send_text(message)
            logger.info("[CatalystNotify] 钉钉推送%s", "✓" if ok_dingtalk else "✗")
        else:
            logger.debug("[CatalystNotify] DINGTALK_WEBHOOK_URL_US 未配置")
    except Exception as e:
        logger.warning("[CatalystNotify] 钉钉推送异常: %s", e)

    return ok_feishu, ok_dingtalk


def push_premarket_report(verdicts: List, market_ctx: Optional[Dict] = None):
    """盘前催化报告 → 格式化 → 推送"""
    try:
        report = format_premarket_report(verdicts, market_ctx)
        if report:
            ok_f, ok_d = notify(report)
            logger.info("[CatalystNotify] 盘前报告已推送 (飞书:%s 钉钉:%s)",
                        "✓" if ok_f else "✗", "✓" if ok_d else "✗")
        else:
            logger.debug("[CatalystNotify] 无催化事件, 跳过推送")
    except Exception as e:
        logger.warning("[CatalystNotify] 盘前推送失败: %s", e)


def push_postmarket_report(review_result: Dict):
    """盘后催化回看报告 → 格式化 → 推送"""
    try:
        report = format_postmarket_report(review_result)
        if report:
            ok_f, ok_d = notify(report)
            logger.info("[CatalystNotify] 盘后报告已推送 (飞书:%s 钉钉:%s)",
                        "✓" if ok_f else "✗", "✓" if ok_d else "✗")
        else:
            logger.debug("[CatalystNotify] 无回看数据, 跳过推送")
    except Exception as e:
        logger.warning("[CatalystNotify] 盘后推送失败: %s", e)
