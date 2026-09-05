"""Position Intelligence - 持仓动态管理建议引擎

为现有持仓提供动态管理建议（不仅是静态 SL/TP），输出条件化的
"如果...则..." 操作清单，覆盖利润保护、接近止盈、持仓时间衰减、
Thesis 失效、板块转向、剩余 R:R 不划算等场景。

设计原则：
- 纯函数式：仅返回 PositionAdvice，不执行任何交易动作
- 数据获取容错：DB 连接失败、字段缺失均不崩溃
- 不依赖 scheduler_instance，可独立导入
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 项目数据目录（app/plan/position_intelligence.py → ../../data）
DATA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")
)

# 默认历史平均持有天数（数据不足时回退）
DEFAULT_AVG_HOLD_DAYS = 3

# urgency 排序权重（数值越小越紧急）
_URGENCY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ConditionAdvice:
    """单条条件化建议：如果 condition 则 action（reason 解释为什么）"""
    condition: str
    action: str
    reason: str

    def to_dict(self) -> Dict:
        return {
            "condition": self.condition,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass
class PositionAdvice:
    """持仓综合管理建议"""
    symbol: str
    direction: str
    action: str       # HOLD / TIGHTEN_SL / PARTIAL_EXIT / FULL_EXIT
    urgency: str      # HIGH / MEDIUM / LOW
    reason: str       # 主要建议原因
    conditions: List[ConditionAdvice] = field(default_factory=list)
    remaining_rr: Optional[float] = None
    hold_days: int = 0
    avg_hold_days: int = DEFAULT_AVG_HOLD_DAYS
    pnl_pct: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "action": self.action,
            "urgency": self.urgency,
            "reason": self.reason,
            "conditions": [c.to_dict() for c in self.conditions],
            "remaining_rr": self.remaining_rr,
            "hold_days": self.hold_days,
            "avg_hold_days": self.avg_hold_days,
            "pnl_pct": self.pnl_pct,
        }


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _calc_hold_days(created_at) -> int:
    """计算持仓天数。

    支持 str (ISO 格式 / 'YYYY-MM-DD HH:MM:SS') 或 datetime；解析失败返回 0。

    尺度修复 (2026-09-05, DEB-20260905 §十三 缺陷1): 原实现用 `txt[:26]` / `txt[:19]`
    定长截断后 `strptime`, 会把 `2026-09-03T09:39:51.298036-04:00` 的 **偏移量截掉**
    (不抛异常, 静默变成 naive), 再与裸 `datetime.now` (宿主 = CST 墙钟) 相减 ⇒ 凭空
    **+12h 偏置**。实测当天 3 个活仓: UBER/RIVN 算出 2 天 (真值 1)、HOOD 算出 1 天
    (真值 0) ⇒ Rule 4 对 UBER/RIVN 误报 "持仓2天(历史平均1天) ⇒ 减仓50%"。
    现改用 `parse_ledger_ts` (aware 归一, naive 按 CST) + `now_et()` 同尺相减。
    """
    if not created_at:
        return 0
    try:
        from app.utils.timezone import now_et, parse_ledger_ts
        dt = parse_ledger_ts(created_at)
        if dt is None:
            logger.debug(f"_calc_hold_days 无法解析尺度: {created_at!r}")
            return 0
        delta = now_et() - dt
        return max(0, delta.days)
    except Exception as e:
        logger.debug(f"_calc_hold_days 解析失败: {created_at!r} ({e})")
        return 0


def _resolve_trade_records_db() -> Optional[str]:
    """解析 trade_records 数据库路径。优先使用 app.core.paths，失败回退默认。"""
    try:
        from app.core.paths import get_trade_records_db  # type: ignore
        return str(get_trade_records_db())
    except Exception:
        if os.getenv("TRADE_ENV", "1").strip() == "0":
            return os.path.join(DATA_DIR, "ftseek_us_live.db")
        return os.path.join(DATA_DIR, "ftseek_us.db")


def _get_avg_hold_days(symbol: str, direction: str) -> int:
    """从 trade_records 表查询历史平均持有天数。

    尺度修复 (2026-09-05, DEB-20260905 §十三 缺陷2): 原实现在 SQL 里做
    `AVG(julianday(exit_time) - julianday(entry_time))` — `julianday()` 把两种尺度的
    字符串当**同一把尺**相减, 而该两列实测混存 (aware-ET / naive-CST) ⇒ 跨尺对产生
    **负持仓时长** (全库实证 16 行, 如 id=854 US.XLK −0.332 天), 把均值整体拉低。
    附带缺陷: 原 `COUNT(*)` 计的是"两列非 NULL 的行", 而 `julianday('')` 返回 NULL 会被
    AVG 跳过却仍被 COUNT 计入 ⇒ cnt 虚高, 可能让数据不足的组越过 `< 2` 门槛。
    现改为取出原始字符串, 用 `parse_ledger_ts` 归一到 aware 后在 Python 侧相减,
    且 cnt 只数**成功解析的配对**。修复后全库 792 行有退出的记录负时长归零。

    数据不足 (< 2 个可解析配对) 或异常时返回 DEFAULT_AVG_HOLD_DAYS=3。
    """
    db_path = _resolve_trade_records_db()
    if not db_path or not os.path.exists(db_path):
        return DEFAULT_AVG_HOLD_DAYS
    try:
        from app.utils.timezone import parse_ledger_ts
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            cur = conn.execute(
                """
                SELECT entry_time, exit_time
                FROM trade_records
                WHERE symbol = ?
                  AND direction = ?
                  AND entry_time IS NOT NULL
                  AND exit_time IS NOT NULL
                """,
                (symbol, direction),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        total_seconds = 0.0
        cnt = 0
        for row in rows:
            entry_dt = parse_ledger_ts(row[0])
            exit_dt = parse_ledger_ts(row[1])
            if entry_dt is None or exit_dt is None:
                continue
            total_seconds += (exit_dt - entry_dt).total_seconds()
            cnt += 1
        if cnt < 2:
            return DEFAULT_AVG_HOLD_DAYS
        avg_days = total_seconds / cnt / 86400.0
        if not avg_days:
            return DEFAULT_AVG_HOLD_DAYS
        return max(1, int(round(float(avg_days))))
    except Exception as e:
        logger.debug(f"_get_avg_hold_days({symbol},{direction}) 查询失败: {e}")
        return DEFAULT_AVG_HOLD_DAYS


def _normalize_symbol(symbol: str) -> str:
    """规范化标的：去掉 'US.' 前缀以便对比"""
    if not symbol:
        return ""
    s = str(symbol).strip().upper()
    if s.startswith("US."):
        s = s[3:]
    return s


def _find_target_in_plan(symbol: str, plan: Dict) -> Optional[Dict]:
    """在 plan.targets 中查找对应标的（兼容 'US.PG' 与 'PG'）。"""
    if not plan or not isinstance(plan, dict):
        return None
    targets = plan.get("targets") or []
    norm = _normalize_symbol(symbol)
    for t in targets:
        try:
            if _normalize_symbol(t.get("symbol", "")) == norm:
                return t
        except Exception:
            continue
    return None


# 价格数字正则：匹配 $123 / $1234.5 / $1,234.56
_PRICE_PATTERN = re.compile(r"\$\s*([0-9]+(?:[,0-9]*)(?:\.[0-9]+)?)")


def _check_invalidation_simple(
    invalidation_text: str,
    current_price: float,
    direction: str,
) -> bool:
    """简单检查 invalidation 条件是否触发（仅价格突破类）。

    思路：用正则提取文本中所有 $xxx 价格数字。
    - LONG：取最大值作为关键支撑，current_price < 关键价 → 视为跌破触发
    - SHORT：取最小值作为关键阻力，current_price > 关键价 → 视为涨破触发

    简单近似实现，覆盖如 "收盘价跌破MA20($305)" 这类常见条件，
    不追求绝对精确，依赖上层做最终决定。
    """
    if not invalidation_text or not current_price or current_price <= 0:
        return False
    try:
        prices: List[float] = []
        for m in _PRICE_PATTERN.finditer(invalidation_text):
            try:
                prices.append(float(m.group(1).replace(",", "")))
            except Exception:
                continue
        if not prices:
            return False
        if (direction or "LONG").upper() == "LONG":
            threshold = max(prices)
            return current_price < threshold
        else:
            threshold = min(prices)
            return current_price > threshold
    except Exception as e:
        logger.debug(f"_check_invalidation_simple 解析失败: {e}")
        return False


def _calc_remaining_rr(
    current: Optional[float],
    sl: Optional[float],
    tp: Optional[float],
    direction: str,
) -> Optional[float]:
    """计算当前价格下的剩余风险回报比。

    LONG:  risk = current - sl,  reward = tp - current
    SHORT: risk = sl - current,  reward = current - tp
    risk <= 0 表示已触及/超过 SL，返回 None；reward <= 0 返回 0.0。
    """
    if current is None or sl is None or tp is None:
        return None
    try:
        current = float(current)
        sl = float(sl)
        tp = float(tp)
    except Exception:
        return None
    if current <= 0 or sl <= 0 or tp <= 0:
        return None
    if (direction or "LONG").upper() == "LONG":
        risk = current - sl
        reward = tp - current
    else:
        risk = sl - current
        reward = current - tp
    if risk <= 0:
        return None
    if reward <= 0:
        return 0.0
    return round(reward / risk, 2)


def _bump_urgency(current: str, candidate: str) -> str:
    """取两个 urgency 中更高的等级。"""
    if _URGENCY_RANK.get(candidate, 99) < _URGENCY_RANK.get(current, 99):
        return candidate
    return current


# ---------------------------------------------------------------------------
# 核心 API
# ---------------------------------------------------------------------------
def analyze_position(
    symbol: str,
    position: Dict,
    plan: Optional[Dict] = None,
    market: Optional[Dict] = None,
) -> PositionAdvice:
    """分析单个持仓，生成动态管理建议。

    Args:
        symbol: 标的代码（'US.PG' 或 'PG'）
        position: 持仓字典，期望字段：
            direction / size / avg_entry_price /
            stop_loss_price (或 current_sl / initial_sl) /
            take_profit_price (或 initial_tp) /
            created_at (或 entry_time) / current_price
        plan: daily_battle_plan_us.json 内容（可选）
        market: 实时市场快照，期望字段 current_price /
            sector_intraday_change（板块当日 % 变化）
    """
    position = position or {}
    market = market or {}

    direction = (position.get("direction") or "LONG").upper()
    try:
        entry = float(position.get("avg_entry_price") or 0)
    except Exception:
        entry = 0.0
    try:
        current = float(
            position.get("current_price")
            or market.get("current_price")
            or 0
        )
    except Exception:
        current = 0.0
    sl = (
        position.get("stop_loss_price")
        or position.get("current_sl")
        or position.get("initial_sl")
    )
    tp = position.get("take_profit_price") or position.get("initial_tp")
    created_at = position.get("created_at") or position.get("entry_time")

    # 计算 P&L
    pnl_pct = 0.0
    if entry > 0 and current > 0:
        if direction == "LONG":
            pnl_pct = (current - entry) / entry
        else:
            pnl_pct = (entry - current) / entry

    hold_days = _calc_hold_days(created_at)
    avg_hold = _get_avg_hold_days(symbol, direction)

    conditions: List[ConditionAdvice] = []
    main_action = "HOLD"
    main_urgency = "LOW"
    main_reason = "正常持有"

    # ---------- Rule 1: Thesis 失效（最高优先级，命中即直接返回）----------
    if plan:
        target = _find_target_in_plan(symbol, plan)
        if target:
            invalidation = target.get("invalidation", "")
            if invalidation and current > 0 and _check_invalidation_simple(
                invalidation, current, direction
            ):
                logger.info(
                    f"[PositionIntelligence] {symbol} thesis 失效触发: {invalidation}"
                )
                return PositionAdvice(
                    symbol=symbol,
                    direction=direction,
                    action="FULL_EXIT",
                    urgency="HIGH",
                    reason=f"Thesis失效: {invalidation}",
                    conditions=[
                        ConditionAdvice(
                            condition=f"已触发: {invalidation}",
                            action="立即全部平仓",
                            reason="原入场逻辑已不成立",
                        )
                    ],
                    remaining_rr=_calc_remaining_rr(current, sl, tp, direction),
                    hold_days=hold_days,
                    avg_hold_days=avg_hold,
                    pnl_pct=pnl_pct,
                )

    # ---------- Rule 2: 利润保护（盈利>2% 时建议移 SL 至成本）----------
    if pnl_pct > 0.02 and entry > 0:
        conditions.append(
            ConditionAdvice(
                condition=f"当前盈利{pnl_pct*100:.1f}%",
                action=f"移动止损到成本价 ${entry:.2f}（保本）",
                reason="零风险持仓，保护已有利润",
            )
        )
        if main_action == "HOLD":
            main_action = "TIGHTEN_SL"
            main_reason = f"盈利{pnl_pct*100:.1f}%，建议移SL到成本"

    # ---------- Rule 3: 接近止盈（已走完 >60% 路程）----------
    progress: Optional[float] = None
    if tp and entry > 0 and current > 0:
        try:
            tp_f = float(tp)
            total_distance = abs(tp_f - entry)
            if total_distance > 0:
                progress = abs(current - entry) / total_distance
                if progress > 0.6:
                    conditions.append(
                        ConditionAdvice(
                            condition=f"已达目标{progress*100:.0f}%",
                            action="建议减仓1/3锁定利润",
                            reason="接近止盈，收益递减",
                        )
                    )
                    if progress > 0.8:
                        main_action = "PARTIAL_EXIT"
                        main_urgency = _bump_urgency(main_urgency, "MEDIUM")
                        main_reason = f"已达目标{progress*100:.0f}%，建议部分了结"
        except Exception:
            progress = None

    # ---------- Rule 4: 持仓时间过长（>1.5x 历史均值）----------
    if hold_days > 0 and avg_hold > 0 and hold_days > avg_hold * 1.5:
        conditions.append(
            ConditionAdvice(
                condition=f"持仓{hold_days}天(历史平均{avg_hold}天)",
                action="考虑减仓50%",
                reason="超过历史平均持有期，收益概率下降",
            )
        )
        if main_action == "HOLD":
            main_action = "PARTIAL_EXIT"
            main_urgency = _bump_urgency(main_urgency, "MEDIUM")
            main_reason = f"持仓{hold_days}天 > 历史均值{avg_hold}天，建议减仓"

    # ---------- Rule 5: 板块转向 ----------
    try:
        sector_change = float(market.get("sector_intraday_change") or 0)
    except Exception:
        sector_change = 0.0
    if direction == "LONG" and sector_change < -1.5:
        conditions.append(
            ConditionAdvice(
                condition=f"板块今日{sector_change:.1f}%",
                action="收紧止损",
                reason="板块转弱，防范扩大亏损",
            )
        )
        if main_action == "HOLD":
            main_action = "TIGHTEN_SL"
            main_reason = "板块转弱，建议收紧止损"
        main_urgency = _bump_urgency(main_urgency, "MEDIUM")
    elif direction == "SHORT" and sector_change > 1.5:
        conditions.append(
            ConditionAdvice(
                condition=f"板块今日+{sector_change:.1f}%",
                action="收紧止损",
                reason="板块转强，空头承压",
            )
        )
        if main_action == "HOLD":
            main_action = "TIGHTEN_SL"
            main_reason = "板块转强，空头承压"
        main_urgency = _bump_urgency(main_urgency, "MEDIUM")

    # ---------- Rule 6: 剩余 R:R 不划算 ----------
    remaining_rr = _calc_remaining_rr(current, sl, tp, direction)
    if remaining_rr is not None and remaining_rr < 1.0:
        conditions.append(
            ConditionAdvice(
                condition=f"剩余R:R仅{remaining_rr:.1f}:1",
                action="考虑平仓释放资金",
                reason="继续持有的风险回报比不划算",
            )
        )
        if main_action == "HOLD":
            main_reason = f"剩余R:R仅{remaining_rr:.1f}:1，性价比不佳"
        main_urgency = _bump_urgency(main_urgency, "MEDIUM")

    return PositionAdvice(
        symbol=symbol,
        direction=direction,
        action=main_action,
        urgency=main_urgency,
        reason=main_reason,
        conditions=conditions,
        remaining_rr=remaining_rr,
        hold_days=hold_days,
        avg_hold_days=avg_hold,
        pnl_pct=pnl_pct,
    )


def analyze_all_positions(
    positions: List[Dict],
    plan: Optional[Dict] = None,
    market: Optional[Dict] = None,
) -> List[PositionAdvice]:
    """批量分析所有持仓，按 urgency 排序（HIGH→LOW）。

    market 兼容两种形态：
    - 全局快照 dict（应用于所有持仓）
    - {symbol: {...}} 映射（按 symbol 取对应市场快照）
    """
    if not positions:
        return []
    advices: List[PositionAdvice] = []
    for pos in positions:
        try:
            symbol = pos.get("symbol") or ""
            if not symbol:
                continue
            sym_market: Optional[Dict] = None
            if isinstance(market, dict):
                if symbol in market and isinstance(market[symbol], dict):
                    sym_market = market[symbol]
                else:
                    norm = _normalize_symbol(symbol)
                    if norm in market and isinstance(market[norm], dict):
                        sym_market = market[norm]
                    else:
                        sym_market = market
            advices.append(analyze_position(symbol, pos, plan, sym_market))
        except Exception as e:
            logger.warning(
                f"[PositionIntelligence] 分析 {pos.get('symbol')} 失败: {e}"
            )
            continue
    advices.sort(key=lambda a: _URGENCY_RANK.get(a.urgency, 99))
    return advices


__all__ = [
    "ConditionAdvice",
    "PositionAdvice",
    "analyze_position",
    "analyze_all_positions",
]
