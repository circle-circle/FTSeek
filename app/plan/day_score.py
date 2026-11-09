"""Day Score Engine - 今日市场适合度评分

计算"今日是否适合交易"的综合评分(0-100)，输出日度风险预算和操作建议。

评分维度:
1. VIX波动率因子
2. SPY大盘趋势因子
3. 经济事件因子
4. 持仓负荷因子
5. 本周表现因子
"""
from dataclasses import dataclass, field
from typing import List, Tuple, Optional
from datetime import datetime, timedelta
import json
import os
import sqlite3
import logging

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# 默认配置
# ──────────────────────────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    "base_score": 70,
    "vix_extreme": 30,
    "vix_high": 25,
    "vix_moderate": 20,
    "vix_low": 15,
    "spy_flat_threshold": 0.005,
    "weekly_target_default": 2000,
    "budget_strong_pct": 0.30,
    "budget_normal_pct": 0.20,
    "budget_cautious_pct": 0.10,
}

# 项目根目录
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 战绩类统计项 (近5笔胜率 / 本周已实现盈亏) 的账本来源 —— **硬编码 sim 库, 不随 TRADE_ENV 路由**。
# 2026-09-05 (DEB-20260905 §13.16) 提为常量的理由: 原先两个 helper 各自拼一遍同一路径,
# 调用侧看不出"这个评分的战绩来自哪个账本"; 而看板 (`routes/battle_plan.py`) 把该评分与
# **实盘账户**的 available_cash / position_count 混算成"今日交易适合度" ⇒ 实盘仓位建议
# (score/level/daily_risk_budget/"可适度放大仓位追趋势") 建立在 sim 战绩上。
# 常量 + `DayScore.stats_source` 让来源**可被下游读到并如实披露**, 不必读源码才知道。
STATS_LEDGER_PATH = os.path.join(_PROJECT_ROOT, "data", "ftseek_us.db")
STATS_LEDGER_DESC = "模拟盘账本 data/ftseek_us.db (硬编码, 不随 TRADE_ENV 路由)"


# ──────────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────────

@dataclass
class DayScore:
    score: int  # 0-100
    level: str  # "STRONG" / "NORMAL" / "CAUTIOUS" / "AVOID"
    reasons: List[str] = field(default_factory=list)
    daily_risk_budget: float = 0.0  # USD
    risk_budget_pct: float = 0.0  # 百分比
    avoid_windows: List[Tuple[str, str]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    computed_at: str = ""
    # 战绩类统计项 (胜率 / 周盈亏) 的账本来源。与 score/level 分开放, 供消费方如实披露
    # "这个分数里的战绩是谁的" —— 混算本身未被本批改变 (见 §13.16 待裁项), 先做到不误导。
    stats_source: str = ""


# ──────────────────────────────────────────────────────────────────────
# 配置加载
# ──────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """从unified_config.yaml加载day_score配置，失败则使用默认值"""
    config = _DEFAULT_CONFIG.copy()
    try:
        import yaml
        config_path = os.path.join(_PROJECT_ROOT, "config", "unified_config.yaml")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            day_score_cfg = raw.get("day_score", {})
            if day_score_cfg and isinstance(day_score_cfg, dict):
                config.update(day_score_cfg)
    except Exception as e:
        logger.debug(f"[DayScore] 配置加载失败，使用默认值: {e}")
    return config


# ──────────────────────────────────────────────────────────────────────
# 数据获取函数
# ──────────────────────────────────────────────────────────────────────

def _get_vix_meta() -> dict:
    """从VIX缓存读取当前值 + 口径/as-of (RFC-190 第三轮)。

    修前只返回裸浮点, 且取不到时静默回落 20.0 —— 读数面无法区分
    "VIX 真是 20" 与 "取数失败用了默认", 也无法知道这是现货收盘还是
    VIXY×0.85 代理 (RFC-190 §1: 两者可差 30%+)。
    """
    try:
        vix_path = os.path.join(_PROJECT_ROOT, "data", "vix_cache", "vix_data.json")
        if os.path.exists(vix_path):
            with open(vix_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            vix_val = data.get("vix")
            if vix_val is not None:
                try:
                    from app.data.market_env_truth import caliber_short
                    caliber = caliber_short(data.get("source"))
                except Exception:
                    caliber = "未知口径"
                return {"value": float(vix_val), "source": data.get("source"),
                        "as_of": data.get("as_of"), "caliber": caliber, "fallback": False}
    except Exception as e:
        logger.debug(f"[DayScore] VIX读取失败: {e}")
    return {"value": 20.0, "source": None, "as_of": None,
            "caliber": "默认值(取数失败)", "fallback": True}


def _get_vix() -> float:
    """从VIX缓存读取当前VIX值 (裸值; 口径见 `_get_vix_meta`)。"""
    return _get_vix_meta()["value"]


def _get_spy_trend() -> dict:
    """获取SPY近期趋势信息

    Returns:
        {"range_5d": float, "trend": "UP"/"DOWN"/"FLAT", "ranges": List[float]}
    """
    result = {"range_5d": 0.0, "trend": "FLAT", "ranges": []}
    try:
        db_path = os.path.join(_PROJECT_ROOT, "data", "ftseek_us.db")
        if not os.path.exists(db_path):
            return result

        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 检查可用的表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        rows = []
        if "kline_daily" in tables:
            cursor.execute(
                "SELECT date, open, high, low, close FROM kline_daily "
                "WHERE symbol IN ('SPY', 'US.SPY') ORDER BY date DESC LIMIT 10"
            )
            rows = cursor.fetchall()
        elif "market_snapshots" in tables:
            cursor.execute(
                "SELECT snapshot_date as date, open_price as open, "
                "high_price as high, low_price as low, close_price as close "
                "FROM market_snapshots "
                "WHERE symbol IN ('SPY', 'US.SPY') ORDER BY snapshot_date DESC LIMIT 10"
            )
            rows = cursor.fetchall()

        conn.close()

        if len(rows) >= 5:
            # 计算每日range (high-low)/close
            ranges = []
            closes = []
            for row in rows[:5]:
                h = float(row["high"]) if row["high"] else 0
                l = float(row["low"]) if row["low"] else 0
                c = float(row["close"]) if row["close"] else 0
                if c > 0:
                    ranges.append((h - l) / c)
                    closes.append(c)

            result["ranges"] = ranges
            result["range_5d"] = sum(ranges) / len(ranges) if ranges else 0.0

            # 计算MA5 vs MA10 趋势
            if len(closes) >= 5:
                ma5 = sum(closes[:5]) / 5
                # 用更多数据计算MA10
                if len(rows) >= 10:
                    closes_10 = []
                    for row in rows[:10]:
                        c = float(row["close"]) if row["close"] else 0
                        if c > 0:
                            closes_10.append(c)
                    ma10 = sum(closes_10) / len(closes_10) if closes_10 else ma5
                else:
                    ma10 = ma5

                if ma5 > ma10 * 1.001:
                    result["trend"] = "UP"
                elif ma5 < ma10 * 0.999:
                    result["trend"] = "DOWN"
                else:
                    result["trend"] = "FLAT"

    except Exception as e:
        logger.debug(f"[DayScore] SPY趋势获取失败: {e}")

    return result


def _get_economic_events() -> List[dict]:
    """获取今日经济事件

    从battle_plan中的no_trade_windows获取事件信息。

    Returns:
        [{"name": str, "time": str, "impact": str, "start": str, "end": str}]
    """
    events = []
    try:
        plan_path = os.path.join(_PROJECT_ROOT, "data", "daily_battle_plan_us.json")
        if not os.path.exists(plan_path):
            return events

        with open(plan_path, "r", encoding="utf-8") as f:
            plan = json.load(f)

        no_trade_windows = plan.get("no_trade_windows", [])
        for window in no_trade_windows:
            events.append({
                "name": window.get("reason", "Unknown Event"),
                "time": window.get("start", ""),
                "impact": window.get("impact", "HIGH"),
                "start": window.get("start", ""),
                "end": window.get("end", ""),
            })

        # 也检查 economic_events 字段 (如果存在)
        eco_events = plan.get("economic_events", [])
        for ev in eco_events:
            events.append({
                "name": ev.get("name", ev.get("event", "Unknown")),
                "time": ev.get("time", ""),
                "impact": ev.get("impact", "MEDIUM"),
                "start": ev.get("start", ev.get("time", "")),
                "end": ev.get("end", ""),
            })

    except Exception as e:
        logger.debug(f"[DayScore] 经济事件获取失败: {e}")

    return events


def _get_recent_win_rate(lookback_days: int = 7, limit: int = 5) -> float:
    """查询近期交易胜率

    Args:
        lookback_days: 回看天数
        limit: 最少查询笔数

    Returns:
        胜率(0.0-1.0)，数据不足返回0.5
    """
    try:
        db_path = STATS_LEDGER_PATH
        if not os.path.exists(db_path):
            return 0.5

        conn = sqlite3.connect(db_path, timeout=5)
        cursor = conn.cursor()

        cutoff_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        cursor.execute(
            "SELECT realized_pnl FROM trade_records "
            "WHERE trade_date >= ? AND exit_price > 0 AND realized_pnl IS NOT NULL "
            "ORDER BY exit_time DESC LIMIT ?",
            (cutoff_date, limit * 3)
        )
        rows = cursor.fetchall()
        conn.close()

        if len(rows) < limit:
            return 0.5  # 数据不足，返回中性值

        # 计算最近 limit 笔的胜率
        recent = rows[:limit]
        wins = sum(1 for r in recent if r[0] and r[0] > 0)
        return wins / len(recent)

    except Exception as e:
        logger.debug(f"[DayScore] 胜率查询失败: {e}")
        return 0.5


def _get_weekly_pnl() -> float:
    """获取本周已实现P&L"""
    try:
        db_path = STATS_LEDGER_PATH
        if not os.path.exists(db_path):
            return 0.0

        conn = sqlite3.connect(db_path, timeout=5)
        cursor = conn.cursor()

        # 本周一的日期
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        monday_str = monday.strftime("%Y-%m-%d")

        cursor.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_records "
            "WHERE trade_date >= ? AND exit_price > 0 AND realized_pnl IS NOT NULL",
            (monday_str,)
        )
        row = cursor.fetchone()
        conn.close()

        return float(row[0]) if row and row[0] else 0.0

    except Exception as e:
        logger.debug(f"[DayScore] 本周P&L查询失败: {e}")
        return 0.0


# ──────────────────────────────────────────────────────────────────────
# 主引擎
# ──────────────────────────────────────────────────────────────────────

def calculate_day_score(
    available_cash: float = 100000.0,
    position_count: int = 0,
    max_positions: int = 8,
    weekly_realized_pnl: float = 0.0,
    weekly_target: float = 2000.0,
    stats_ledger_label: Optional[str] = None,
) -> DayScore:
    """
    计算今日市场适合度评分

    Args:
        available_cash: 可用现金
        position_count: 当前持仓数
        max_positions: 最大持仓数
        weekly_realized_pnl: 本周已实现P&L (0表示自动从DB查询)
        weekly_target: 本周目标
        stats_ledger_label: 战绩类文案的**来源标注前缀** (如 "模拟盘")。给出时, 凡来自
            `STATS_LEDGER_PATH` 的统计项 (近5笔胜率 / 本周已实现盈亏) 在 `reasons` 文案里
            带上该前缀。**数字、评分、level、风险预算一律不变** —— 只去掉误导。
            看板 (`routes/battle_plan.py`, 实盘手动操作台) 传 "模拟盘"; sim 主管线
            (`us_pipeline/battle_plan_manager.py`) **不传** ⇒ 文案逐字不变。
            背景见 DEB-20260905 §13.16: 这两项统计硬编码读 sim 库, 而 available_cash /
            position_count 来自调用方的**实盘**账户 ⇒ 混算出的建议会被读成"我的实盘战绩"。
    """
    cfg = _load_config()
    base_score = cfg["base_score"]
    score = base_score
    reasons: List[str] = []
    suggestions: List[str] = []
    avoid_windows: List[Tuple[str, str]] = []
    # 战绩类文案前缀 (空串 = 不标注 = sim 主管线的既有口径)
    _stats_prefix = stats_ledger_label or ""

    # ── 1. VIX因子 (-20 ~ +10) ──
    _vix_meta = _get_vix_meta()
    vix = _vix_meta["value"]
    # 口径角标: 读数面必须能看出"现货收盘/代理/默认值"以及观测日 (RFC-190)
    _vix_tag = " (%s%s)" % (
        _vix_meta["caliber"],
        "·as of %s" % _vix_meta["as_of"] if _vix_meta.get("as_of") else "")
    if vix > cfg["vix_extreme"]:
        score -= 20
        reasons.append(f"VIX={vix:.1f}{_vix_tag} 极高，假突破概率大增")
        suggestions.append("缩小仓位，设紧止损，避免追涨")
    elif vix > cfg["vix_high"]:
        score -= 15
        reasons.append(f"VIX={vix:.1f}{_vix_tag} 偏高，市场不确定性强")
        suggestions.append("降低仓位规模，优先观望")
    elif vix > cfg["vix_moderate"]:
        score -= 5
        reasons.append(f"VIX={vix:.1f}{_vix_tag} 中等波动")
    elif vix < cfg["vix_low"]:
        score += 10
        reasons.append(f"VIX={vix:.1f}{_vix_tag} 低位，市场稳定有利趋势")
        suggestions.append("可适度放大仓位追趋势")

    # ── 2. SPY趋势因子 (-10 ~ +10) ──
    spy_info = _get_spy_trend()
    avg_range = spy_info["range_5d"]
    trend = spy_info["trend"]

    if avg_range > 0 and avg_range < cfg["spy_flat_threshold"]:
        # 连续低range横盘
        score -= 10
        reasons.append(f"SPY 5日均幅 {avg_range*100:.2f}% < 0.5%，横盘震荡方向不明")
        suggestions.append("避免趋势策略，考虑区间操作或观望")
    elif trend == "UP":
        score += 10
        reasons.append("SPY MA5>MA10，大盘趋势向上")
        suggestions.append("顺势做多为主")
    elif trend == "DOWN":
        score += 5
        reasons.append("SPY MA5<MA10，大盘下行（做空机会）")
        suggestions.append("关注空头机会，谨慎做多")

    # ── 3. 经济事件因子 (-20 ~ 0) ──
    events = _get_economic_events()
    high_impact_events = [e for e in events if e.get("impact", "").upper() == "HIGH"]
    medium_impact_events = [e for e in events if e.get("impact", "").upper() == "MEDIUM"]

    if high_impact_events:
        score -= 20
        event_names = ", ".join(e["name"] for e in high_impact_events[:3])
        reasons.append(f"重大经济事件日: {event_names}")
        suggestions.append("避免在事件公布前后30分钟开新仓")
        for e in high_impact_events:
            if e.get("start") and e.get("end"):
                avoid_windows.append((e["start"], e["end"]))
    elif medium_impact_events:
        score -= 10
        event_names = ", ".join(e["name"] for e in medium_impact_events[:3])
        reasons.append(f"中等经济事件: {event_names}")
        suggestions.append("事件时段减少操作")
        for e in medium_impact_events:
            if e.get("start") and e.get("end"):
                avoid_windows.append((e["start"], e["end"]))

    # ── 4. 持仓负荷因子 (-10 ~ 0) ──
    if max_positions > 0:
        load_ratio = position_count / max_positions
        if load_ratio >= 0.8:
            score -= 10
            reasons.append(f"持仓 {position_count}/{max_positions} 接近上限")
            suggestions.append("优先管理现有持仓，不宜加新仓")
        elif load_ratio >= 0.5:
            score -= 5
            reasons.append(f"持仓 {position_count}/{max_positions} 中等负荷")

    # ── 5. 本周表现因子 (-10 ~ +5) ──
    weekly_pnl = weekly_realized_pnl if weekly_realized_pnl != 0 else _get_weekly_pnl()
    effective_target = weekly_target if weekly_target > 0 else cfg["weekly_target_default"]

    if effective_target > 0 and weekly_pnl >= effective_target:
        score -= 10
        reasons.append(f"{_stats_prefix}本周已盈 ${weekly_pnl:.0f} >= 目标 ${effective_target:.0f}，建议收手保护利润")
        suggestions.append("本周已达目标，减少交易频率")
    elif effective_target > 0 and weekly_pnl >= effective_target * 0.8:
        score -= 5
        reasons.append(f"{_stats_prefix}本周已盈 ${weekly_pnl:.0f}，接近周目标")
        suggestions.append("谨慎操作，以保护利润为主")

    # 近期状态加分
    recent_wr = _get_recent_win_rate(lookback_days=7, limit=5)
    if recent_wr > 0.7:
        score += 5
        reasons.append(f"{_stats_prefix}近5笔胜率 {recent_wr*100:.0f}%，状态良好")

    # ── 分数裁剪 ──
    score = max(0, min(100, score))

    # ── Level映射 ──
    if score >= 75:
        level = "STRONG"
    elif score >= 60:
        level = "NORMAL"
    elif score >= 45:
        level = "CAUTIOUS"
    else:
        level = "AVOID"

    # ── 风险预算计算 ──
    budget_pct_map = {
        "STRONG": cfg["budget_strong_pct"],
        "NORMAL": cfg["budget_normal_pct"],
        "CAUTIOUS": cfg["budget_cautious_pct"],
        "AVOID": 0.0,
    }
    risk_budget_pct = budget_pct_map.get(level, 0.0)
    daily_risk_budget = available_cash * risk_budget_pct

    # ── 补充建议 ──
    if level == "AVOID":
        suggestions.append("今日不建议开新仓，专注持仓管理")
    elif level == "CAUTIOUS":
        suggestions.append("仅在高置信度信号出现时少量参与")

    return DayScore(
        score=score,
        level=level,
        reasons=reasons,
        daily_risk_budget=round(daily_risk_budget, 2),
        risk_budget_pct=round(risk_budget_pct * 100, 2),
        avoid_windows=avoid_windows,
        suggestions=suggestions,
        computed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        stats_source=STATS_LEDGER_DESC,
    )
