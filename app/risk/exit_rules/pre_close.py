"""
FTSeek-US Pre-Close Exit 规则 (规则 3.5)

Task #19 拆分自 position_exit_monitor.py。
盘末强制退出: 分层退出 + 时间加权策略 (2026-05-03 P2 优化)。
"""
import logging
from datetime import datetime, date
from typing import Optional, Dict, Any

from .base import BaseExitRule
from ..atr_stop_engine import compute_profit_atr_signed
from ..eod_config import load_eod_config
from app.storage.trade_store import cohort_b_skip_allowed

logger = logging.getLogger("ftseek.exit_monitor")

try:
    from pytz import timezone
    ET = timezone('US/Eastern')
except ImportError:
    ET = None


def check_pre_close_exit(position, current_price, atr, trail_state, direction, policy_guardian=None):
    """Pre-Close Exit 判定逻辑 (从 ExitMonitor._check_pre_close_exit 原样迁移)"""
    try:
        if ET:
            now_et = datetime.now(ET)
        else:
            now_et = datetime.now()

        if now_et.weekday() >= 5:
            return None

        pc_cfg = {}
        if policy_guardian is not None and hasattr(policy_guardian, "policy"):
            pc_cfg = policy_guardian.policy.get("exit_rules", {}).get("pre_close_exit", {})

        # 2026-06-30: 检查enabled - 如果exit_policy.yaml中pre_close_exit.enabled=false, 直接返回
        if pc_cfg.get("enabled", True) is False:
            return None

        # C4 修复 (2026-05-13): 全局 EOD 配置优先
        eod_cfg = load_eod_config()

        window_minutes = int(pc_cfg.get("window_minutes", 15))
        minutes_to_close = 16 * 60 - (now_et.hour * 60 + now_et.minute)
        if minutes_to_close > window_minutes or minutes_to_close < 0:
            return None

        # P0 修复 (2026-05-02): 冷却期检查
        last_scan = trail_state.get('_last_pre_close_check', 0)
        now_ts = now_et.timestamp()
        if now_ts - last_scan < 60:
            return None

        # P1 修复 (2026-05-08): 入场后最小持有时间保护
        entry_time = position.entry_time
        if entry_time:
            try:
                if hasattr(entry_time, 'tzinfo') and entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=ET) if ET else entry_time
                held_minutes = (now_et - entry_time).total_seconds() / 60.0
                min_hold_minutes = float(pc_cfg.get("min_hold_minutes_before_pre_close", eod_cfg.min_hold_minutes_before_eod_exit))
                if held_minutes < min_hold_minutes:
                    logger.debug(
                        f"[盘末退出·跳过] {position.symbol} 入场仅 {held_minutes:.0f} 分钟 "
                        f"< {min_hold_minutes:.0f} 分钟，不触发盘末退出"
                    )
                    return None
            except Exception as _silent_e:
                logger.debug(f"[silent_except:pre_close.py] {_silent_e}")

        if atr <= 0 or position.avg_entry_price <= 0:
            return None

        # 通过验证后才记录冷却时间戳 (避免无效调用消耗冷却窗口)
        trail_state['_last_pre_close_check'] = now_ts

        profit_atr_signed = compute_profit_atr_signed(direction, position.avg_entry_price, current_price, atr)

        # ── RFC-061 (2026-06-24): cohort B 盈利仓跳过 EOD 持夜到 ~T+5 ──
        # MAIN_PIPELINE 的 ATR_STOP 是独立引擎 (atr_stop_engine), 不进本函数的判定链,
        # 故跳过 pre_close 不影响硬止损. LIVE 需 live_overnight_enabled() 显式开 (不从 sim 继承).
        try:
            _meta = getattr(position, "metadata", None) or {}
            _cohort = _meta.get("cohort", "A")
            _src = _meta.get("source_type", "")
            if _cohort == "B" and profit_atr_signed > 0 and cohort_b_skip_allowed(_src):
                logger.info(
                    f"[pre_close·RFC-061] {position.symbol} cohort=B profit={profit_atr_signed:.2f}ATR "
                    f"→ 跳过 EOD 持夜 (MAX_HOLD_T5 兜底)"
                )
                return None
        except Exception as _e:
            logger.debug(f"[pre_close·RFC-061] cohort 检查异常 (降级为 A): {_e}")

        # P2 优化 (2026-05-03): 时间加权阈值
        time_weighted = pc_cfg.get("time_weighted", False)
        threshold_mult = 1.0
        time_window_label = "standard"

        if time_weighted:
            if minutes_to_close < 5:
                threshold_mult = float(pc_cfg.get("urgent_threshold_mult", 0.6))
                time_window_label = "urgent"
            elif minutes_to_close < 10:
                threshold_mult = float(pc_cfg.get("warning_threshold_mult", 0.8))
                time_window_label = "warning"

        # 层4: ≥hold_threshold ATR → 保留过夜 (hold_threshold=99 时实质禁用)
        hold_threshold = float(pc_cfg.get("hold_threshold_atr", 99)) * threshold_mult

        # P1 (2026-06-04): close_profit_atr — 盈利落袋为安
        close_profit_atr = float(pc_cfg.get("close_profit_atr", 99))
        if profit_atr_signed >= close_profit_atr:
            return {
                "reason": "PRE_CLOSE_EXIT",
                "reason_cn": f"盘末盈利落袋 (浮盈{profit_atr_signed:.1f}ATR >= {close_profit_atr}ATR)",
                "exit_price": current_price,
                "description": (
                    f"浮盈 {profit_atr_signed:.1f} ATR >= {close_profit_atr} ATR, "
                    f"盈利落袋为安, 全平"
                ),
                "close_ratio": 1.0,
            }

        # P1 修复 (2026-05-11): 隔夜风险敞口总上限
        max_overnight_pct = float(pc_cfg.get("max_overnight_exposure_pct", 0.50))
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            account_equity = 0.0
            try:
                from app.risk.capital_safety import get_capital_safety_system
                cs = get_capital_safety_system()
                account_equity = getattr(cs._state, 'current_equity', 1000000)
            except Exception:
                account_equity = float(cfg.get_dotted("capital.fixed_amount", 82_000))  # SSOT: YAML=82000

            position_value = current_price * position.size
            position_pct = position_value / account_equity if account_equity > 0 else 0
            if position_pct > max_overnight_pct * 0.3:
                hold_threshold *= 1.5
                logger.warning(
                    f"[盘末退出·隔夜收紧] {position.symbol} 单只占比 {position_pct:.0%}, "
                    f"hold_threshold 提高至 {hold_threshold:.1f} ATR"
                )
        except Exception as e:
            logger.debug(f"[盘末退出·隔夜检查] 失败: {e}")

        if profit_atr_signed >= hold_threshold:
            position_value = current_price * position.size
            if position_value > 100_000:
                logger.warning(
                    f"⚠️ [盘末退出·豁免·大额] {position.symbol} "
                    f"浮盈 {profit_atr_signed:.1f} ATR (阈值{hold_threshold:.1f}, {time_window_label}窗口), "
                    f"持仓价值 ${position_value:,.0f}, "
                    f"允许过夜 — 请确认这是有意持仓"
                )
            else:
                logger.debug(
                    f"[盘末退出·跳过] {position.symbol} 浮盈 {profit_atr_signed:.1f} ATR ≥ {hold_threshold:.1f} "
                    f"({time_window_label}窗口), 允许持仓过夜"
                )
            return None

        # 分层退出 (应用时间加权阈值)
        loss_flat = pc_cfg.get("loss_flat", True)
        half_exit_atr = float(pc_cfg.get("half_exit_below_atr", 0.5)) * threshold_mult
        quarter_exit_atr = float(pc_cfg.get("quarter_exit_below_atr", 1.0)) * threshold_mult

        # 层1: 亏损仓位
        if profit_atr_signed < 0:
            if loss_flat:
                return {
                    "reason": "PRE_CLOSE_EXIT",
                    "reason_cn": f"盘末强制退出 (亏损仓位, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                    "exit_price": current_price,
                    "description": (
                        f"浮亏 {profit_atr_signed:.1f} ATR, "
                        f"距收盘 {minutes_to_close} 分钟 ({time_window_label}窗口), "
                        f"不持有亏损过夜"
                    ),
                    "close_ratio": 1.0,
                }
            else:
                # P1 (2026-06-04): loss_flat=false → 亏损保留过夜
                # 隔夜保护由 atr_stop (4.0×ATR) 在次日开盘自动执行
                logger.info(
                    f"[盘末退出·保留过夜] {position.symbol} 浮亏 {profit_atr_signed:.1f} ATR, "
                    f"loss_flat=false 保留过夜, 隔夜保护由atr_stop管理"
                )
                return None

        # 层2: 0~half_exit_atr 微利 → 减半仓
        elif profit_atr_signed < half_exit_atr:
            return {
                "reason": "PRE_CLOSE_EXIT_PARTIAL",
                "reason_cn": f"盘末减仓50% (微利{profit_atr_signed:.1f}ATR, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                "exit_price": current_price,
                "description": (
                    f"浮盈 {profit_atr_signed:.1f} ATR < {half_exit_atr:.1f} "
                    f"(距收盘 {minutes_to_close} 分钟, {time_window_label}窗口), "
                    f"减仓50%降低隔夜风险"
                ),
                "close_ratio": 0.5,
            }

        # 层3: half_exit_atr~quarter_exit_atr → 减 1/4
        elif profit_atr_signed < quarter_exit_atr:
            return {
                "reason": "PRE_CLOSE_EXIT_PARTIAL",
                "reason_cn": f"盘末减仓25% (盈利{profit_atr_signed:.1f}ATR, {time_window_label}窗口, 距收盘{minutes_to_close}分钟)",
                "exit_price": current_price,
                "description": (
                    f"浮盈 {profit_atr_signed:.1f} ATR < {quarter_exit_atr:.1f} "
                    f"(距收盘 {minutes_to_close} 分钟, {time_window_label}窗口), "
                    f"减仓25%"
                ),
                "close_ratio": 0.25,
            }

        return None

    except Exception as e:
        logger.debug(f"盘末退出检查失败 {position.symbol}: {e}")
    return None


class PreCloseExitRule(BaseExitRule):
    """盘末分层强制退出 (优先级最高, 不参与质量评分竞争)"""

    rule_name = "PRE_CLOSE_EXIT"

    def should_exit(self, position, market_data: Dict[str, Any], trail_state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return check_pre_close_exit(
            position,
            market_data["current_price"],
            market_data.get("effective_atr", market_data.get("atr", 0.0)),
            trail_state,
            market_data["direction"],
            market_data.get("policy_guardian"),
        )


# ============================================================
#  自适应 Pre-Close Exit (Task #3, 2026-05-31)
# ------------------------------------------------------------
#  目标: 将 PRE_CLOSE 占比从 54% 降至 <35%
#  策略: 基于 PnL/ATR 的四分支自适应退出 + regime 动态窗口
#       1) 利润    : PnL >=  1.0 ATR → 全平 (锁利)
#       2) 持平    : |PnL| <  0.3 ATR → 50% 平仓 (减少隔夜风险)
#       3) 亏损    : PnL <= -1.5 ATR → 全平 (止损)
#       4) 中间地带: 0.3~1.0 ATR 小利 / -1.5~-0.3 ATR 小亏 → 不强平, 允许过夜
# ============================================================

_DEFAULT_ADAPTIVE_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "mode": "adaptive",
    "profit_exit": {
        "min_profit_atr": 1.0,
        "close_ratio": 1.0,
    },
    "breakeven_exit": {
        "tolerance_atr": 0.3,
        "close_ratio": 0.50,
    },
    "loss_exit": {
        "min_loss_atr": 1.5,
        "close_ratio": 1.0,
    },
    "force_close_remaining": False,
    "window_minutes": {
        "trending": 10,
        "range_bound": 5,
        "high_vol": 15,
        "default": 10,
    },
}


def _resolve_window_minutes(regime: Optional[str], cfg_window: Dict[str, Any]) -> int:
    """基于 regime 解析退出窗口分钟数。"""
    if not isinstance(cfg_window, dict):
        return 10
    key = (regime or "default").lower() if isinstance(regime, str) else "default"
    # 兼容常用别名
    aliases = {
        "trend": "trending",
        "trending_up": "trending",
        "trending_down": "trending",
        "range": "range_bound",
        "sideways": "range_bound",
        "choppy": "range_bound",
        "volatile": "high_vol",
        "highvol": "high_vol",
        "high_volatility": "high_vol",
    }
    key = aliases.get(key, key)
    val = cfg_window.get(key, cfg_window.get("default", 10))
    try:
        return int(val)
    except (TypeError, ValueError):
        return 10


def _coerce_attr(obj: Any, key: str, default: Any = None) -> Any:
    """同时支持 dict 与对象两种 position/market_data 接口。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def check_adaptive_pre_close_exit(
    position: Any,
    market_data: Dict[str, Any],
    trail_state: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """自适应收盘前退出策略 (Task #3, 2026-05-31).

    分级逻辑 (基于 PnL/ATR):
      1) 利润分支: PnL >= profit_exit.min_profit_atr        → close_ratio (默认 1.0)
      2) 持平分支: |PnL| < breakeven_exit.tolerance_atr     → close_ratio (默认 0.5)
      3) 亏损分支: PnL <= -loss_exit.min_loss_atr           → close_ratio (默认 1.0)
      4) 中间地带: 不强平 (force_close_remaining=False)

    退出窗口按 regime 动态:
      trending=10min, range_bound=5min, high_vol=15min, default=10min

    Args:
        position: 持仓 dict 或对象, 需含 entry_price/avg_entry_price, direction, quantity/size
        market_data: 行情 dict, 需含 current_price, atr, minutes_to_close, regime(可选)
        trail_state: 追踪状态 dict (用于 60s 冷却)
        config: pre_close 配置段 dict (来自 exit_policy.yaml)

    Returns:
        触发: dict {reason, reason_cn, exit_price, close_ratio, description, metadata}
        未触发: None
    """
    try:
        cfg = dict(_DEFAULT_ADAPTIVE_CONFIG)
        if isinstance(config, dict):
            # 浅合并: 顶层覆盖 + 子段合并
            for k, v in config.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    merged = dict(cfg[k])
                    merged.update(v)
                    cfg[k] = merged
                else:
                    cfg[k] = v

        if not cfg.get("enabled", True):
            return None

        # ── 1. 距收盘窗口判断 (按 regime 动态) ──
        minutes_to_close = market_data.get("minutes_to_close")
        if minutes_to_close is None:
            return None
        try:
            mtc = float(minutes_to_close)
        except (TypeError, ValueError):
            return None
        if mtc < 0:
            return None

        regime = market_data.get("regime") or market_data.get("market_regime")
        window_minutes = _resolve_window_minutes(regime, cfg.get("window_minutes", {}))
        if mtc > window_minutes:
            return None

        # ── 2. 60s 冷却, 避免重复触发 ──
        if isinstance(trail_state, dict):
            now_ts = datetime.now().timestamp()
            last_ts = float(trail_state.get("_last_adaptive_pre_close_ts", 0) or 0)
            if now_ts - last_ts < 60:
                return None

        # ── 3. 关键字段抽取 (兼容 dict / 对象) ──
        entry_price = float(
            _coerce_attr(position, "entry_price", None)
            or _coerce_attr(position, "avg_entry_price", 0)
            or 0
        )
        current_price = float(market_data.get("current_price", 0) or 0)
        atr = float(market_data.get("atr", 0) or market_data.get("effective_atr", 0) or 0)
        direction = str(_coerce_attr(position, "direction", "LONG") or "LONG").upper()
        symbol = _coerce_attr(position, "symbol", "?")

        if entry_price <= 0 or current_price <= 0 or atr <= 0:
            logger.debug(
                f"[adaptive_pre_close·跳过] {symbol} 参数不足: "
                f"entry={entry_price} cur={current_price} atr={atr}"
            )
            return None

        # 通过验证后才记录冷却时间戳 (避免无效调用消耗冷却窗口)
        if isinstance(trail_state, dict):
            trail_state["_last_adaptive_pre_close_ts"] = now_ts

        # ── 4. PnL/ATR 计算 (有符号) ──
        if direction in ("SHORT", "SELL"):
            profit = entry_price - current_price
        else:
            profit = current_price - entry_price
        profit_atr = profit / atr

        # ── RFC-061 (2026-06-24): cohort B 盈利仓跳过 EOD 持夜 (adaptive 路径同 legacy) ──
        try:
            _meta = position.get("metadata", {}) if isinstance(position, dict) else (getattr(position, "metadata", None) or {})
            _cohort = _meta.get("cohort", "A") if isinstance(_meta, dict) else "A"
            _src = _meta.get("source_type", "") if isinstance(_meta, dict) else ""
            if _cohort == "B" and profit_atr > 0 and cohort_b_skip_allowed(_src):
                logger.info(
                    f"[pre_close·adaptive·RFC-061] {symbol} cohort=B profit={profit_atr:.2f}ATR → 跳过 EOD 持夜"
                )
                return None
        except Exception as _e:
            logger.debug(f"[pre_close·adaptive·RFC-061] cohort 检查异常 (降级 A): {_e}")

        # ── 5. 四分支自适应判定 ──
        profit_cfg = cfg.get("profit_exit", {}) or {}
        breakeven_cfg = cfg.get("breakeven_exit", {}) or {}
        loss_cfg = cfg.get("loss_exit", {}) or {}

        min_profit_atr = float(profit_cfg.get("min_profit_atr", 1.0))
        profit_ratio = float(profit_cfg.get("close_ratio", 1.0))
        tolerance_atr = float(breakeven_cfg.get("tolerance_atr", 0.3))
        breakeven_ratio = float(breakeven_cfg.get("close_ratio", 0.5))
        min_loss_atr = float(loss_cfg.get("min_loss_atr", 1.5))
        loss_ratio = float(loss_cfg.get("close_ratio", 1.0))
        force_remaining = bool(cfg.get("force_close_remaining", False))

        base_meta = {
            "profit_atr": round(profit_atr, 3),
            "atr": atr,
            "minutes_to_close": mtc,
            "window_minutes": window_minutes,
            "regime": regime,
            "mode": cfg.get("mode", "adaptive"),
        }

        # 分支 1: 利润达标 → 锁利全平
        if profit_atr >= min_profit_atr:
            logger.info(
                f"[adaptive_pre_close·锁利] {symbol} profit={profit_atr:.2f}ATR"
                f" >= {min_profit_atr:.2f} → close {profit_ratio*100:.0f}% (regime={regime}, mtc={mtc:.0f}min)"
            )
            return {
                "reason": "PRE_CLOSE_EXIT",
                "reason_cn": (
                    f"盘末锁利退出 (浮盈{profit_atr:.1f}ATR>={min_profit_atr:.1f}, "
                    f"距收盘{mtc:.0f}分钟, regime={regime})"
                ),
                "exit_price": current_price,
                "close_ratio": profit_ratio,
                "description": f"PnL {profit_atr:.2f} ATR 达到锁利阈值, 全平防止隔夜回吐",
                "metadata": {**base_meta, "branch": "profit"},
            }

        # 分支 3: 重大亏损 → 止损全平 (优先级高于持平判断)
        if profit_atr <= -min_loss_atr:
            logger.info(
                f"[adaptive_pre_close·止损] {symbol} loss={profit_atr:.2f}ATR"
                f" <= -{min_loss_atr:.2f} → close {loss_ratio*100:.0f}% (regime={regime}, mtc={mtc:.0f}min)"
            )
            return {
                "reason": "PRE_CLOSE_EXIT",
                "reason_cn": (
                    f"盘末止损退出 (浮亏{profit_atr:.1f}ATR<=-{min_loss_atr:.1f}, "
                    f"距收盘{mtc:.0f}分钟, regime={regime})"
                ),
                "exit_price": current_price,
                "close_ratio": loss_ratio,
                "description": f"PnL {profit_atr:.2f} ATR 触及隔夜止损线, 全平避免跳空风险",
                "metadata": {**base_meta, "branch": "loss"},
            }

        # 分支 2: 持平/微动 → 减半仓
        if abs(profit_atr) < tolerance_atr:
            logger.info(
                f"[adaptive_pre_close·持平减仓] {symbol} pnl={profit_atr:.2f}ATR"
                f" |.| < {tolerance_atr:.2f} → close {breakeven_ratio*100:.0f}% (regime={regime}, mtc={mtc:.0f}min)"
            )
            return {
                "reason": "PRE_CLOSE_EXIT_PARTIAL",
                "reason_cn": (
                    f"盘末持平减仓 ({breakeven_ratio*100:.0f}%, |PnL|{abs(profit_atr):.2f}ATR<{tolerance_atr:.2f}, "
                    f"距收盘{mtc:.0f}分钟, regime={regime})"
                ),
                "exit_price": current_price,
                "close_ratio": breakeven_ratio,
                "description": f"PnL {profit_atr:.2f} ATR 处于持平带, 减半仓降低隔夜敞口",
                "metadata": {**base_meta, "branch": "breakeven"},
            }

        # 分支 4: 中间地带 (小利 0.3~1.0 ATR / 小亏 -1.5~-0.3 ATR) — 默认不强平
        if force_remaining:
            logger.info(
                f"[adaptive_pre_close·强制兜底] {symbol} pnl={profit_atr:.2f}ATR"
                f" 中间带 + force_close_remaining=True → 全平"
            )
            return {
                "reason": "PRE_CLOSE_EXIT",
                "reason_cn": f"盘末强制兜底 (中间带, PnL{profit_atr:.1f}ATR, 距收盘{mtc:.0f}分钟)",
                "exit_price": current_price,
                "close_ratio": 1.0,
                "description": "force_close_remaining=True, 中间带兜底平仓",
                "metadata": {**base_meta, "branch": "force_remaining"},
            }

        logger.debug(
            f"[adaptive_pre_close·过夜] {symbol} pnl={profit_atr:.2f}ATR 处于中间带 "
            f"({-min_loss_atr:.1f}~{-tolerance_atr:.1f} 或 {tolerance_atr:.1f}~{min_profit_atr:.1f}), "
            f"允许持仓过夜 (regime={regime}, mtc={mtc:.0f}min)"
        )
        return None

    except Exception as e:
        logger.warning(f"[adaptive_pre_close·异常] {e}", exc_info=True)
        return None


# ═══════════════════════════════════════════════════════════════════
#  RFC-061 (2026-06-24): MAX_HOLD_T5 — cohort B 持仓 ≥5 交易日强制了结
# ═══════════════════════════════════════════════════════════════════
# cohort B 盈利仓跳过 EOD 持夜后的硬上限. 用交易日历算 5 交易日后 (遇假期自动跳过,
# 如 Juneteenth), 比日历日准. 仅 cohort B; A 组日内已平不会触及.
# 由 position_exit_monitor 在 pre_close 之前调用 (优先级高于 pre_close).

def check_max_hold_t5(position, current_price=None) -> Optional[Dict]:
    """RFC-061: cohort B 持仓 ≥5 交易日后强制了结 (MAX_HOLD_T5).

    Returns: 命中则返回 exit decision dict (close_ratio=1.0, 全平); 否则 None.
    仅对 cohort B 生效. MAIN_PIPELINE 的 ATR_STOP 独立引擎仍并行生效 (不冲突).
    """
    try:
        meta = getattr(position, "metadata", None) or {}
        if not isinstance(meta, dict):
            return None
        if meta.get("cohort") != "B":
            return None
        # LIVE 守卫: 实盘需显式开 (不从 sim 继承)
        if not cohort_b_skip_allowed(meta.get("source_type", "")):
            return None
        entry_time = getattr(position, "entry_time", None)
        if not entry_time:
            return None
        # 归一化为 ET date
        try:
            if hasattr(entry_time, "tzinfo") and entry_time.tzinfo is not None:
                _entry_date = entry_time.astimezone(ET).date() if ET else entry_time.date()
            else:
                _entry_date = entry_time.date()
        except Exception:
            return None
        from app.utils.holiday_calendar import nth_trading_day_after
        deadline = nth_trading_day_after("US", _entry_date, 5)
        if deadline is None:
            return None  # 日历异常, 不强平 (保守)
        now_et = datetime.now(ET) if ET else datetime.now()
        if now_et.date() < deadline:
            return None  # 未到 5 交易日, 继续 hold
        symbol = getattr(position, "symbol", "?")
        logger.info(
            f"[MAX_HOLD_T5·RFC-061] {symbol} cohort=B 持仓至 {_entry_date}+5交易日 (deadline={deadline}) → 强制了结"
        )
        return {
            "reason": "MAX_HOLD_T5",
            "reason_cn": f"cohort B 持仓≥5交易日 (RFC-061) 强制了结",
            "exit_price": current_price if current_price else 0,
            "close_ratio": 1.0,
            "description": f"MAX_HOLD_T5: cohort B held since {_entry_date}, deadline {deadline}",
        }
    except Exception as e:
        logger.warning(f"[MAX_HOLD_T5·RFC-061] 异常 (降级不触发): {e}")
        return None
