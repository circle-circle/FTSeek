"""
止盈止损策略 — 三层退出机制

第一层: 固定百分比止盈止损 (主要)
第二层: 时间止损 (辅助 — 持仓超时强制平仓)
第三层: 动态参数调整 (保护 — 连续盈亏时收紧/放宽)
第四层: ATR 波动率自适应 (P2 新增 — 根据市场波动率动态调整 TP/SL)

Task #9 (2026-05-30): SCALPING 退出参数迁移到 app/risk/exit_policy.yaml
  - 模块级 _SCALPING_EXIT_CONFIG 在 import 时一次性加载
  - 配置缺失或加载失败时自动回退到代码默认值 (向后兼容)
  - 不影响运行期性能 (仅初始化时读盘)
"""

import os
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
import logging

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# Task #9 (2026-05-30): exit_policy.yaml::scalping_overrides 加载器
# ═══════════════════════════════════════════════════════════
_EXIT_POLICY_PATH = os.path.join(
    os.path.dirname(__file__), "..", "risk", "exit_policy.yaml"
)


def _load_scalping_exit_config() -> dict:
    """从 exit_policy.yaml 加载 SCALPING 退出配置。

    成功条件: 文件存在 + scalping_overrides.enabled = True。
    任一失败: 返回 {} 让所有调用方走代码默认值 (向后兼容)。
    """
    if yaml is None:
        logger.warning("[SCALPING_EXIT] PyYAML 不可用, 使用代码默认值")
        return {}
    try:
        with open(_EXIT_POLICY_PATH, "r", encoding="utf-8") as f:
            policy = yaml.safe_load(f) or {}
        overrides = policy.get("scalping_overrides", {}) or {}
        if overrides.get("enabled", False):
            logger.info(
                "[SCALPING_EXIT] 已加载 exit_policy.yaml::scalping_overrides "
                f"(atr_exit={bool(overrides.get('atr_exit'))}, "
                f"trailing={bool(overrides.get('trailing'))}, "
                f"time_exit={bool(overrides.get('time_exit'))}, "
                f"phase_scaling={bool(overrides.get('phase_scaling'))})"
            )
            return overrides
        logger.info("[SCALPING_EXIT] scalping_overrides.enabled=false, 使用代码默认值")
    except FileNotFoundError:
        logger.warning(
            f"[SCALPING_EXIT] exit_policy.yaml 未找到 ({_EXIT_POLICY_PATH}), 使用代码默认值"
        )
    except Exception as e:
        logger.warning(
            f"[SCALPING_EXIT] 无法加载 exit_policy.yaml 配置, 使用代码默认值: {e}"
        )
    return {}


# 模块级缓存 — 仅在首次 import 时读盘一次, 不影响运行期性能
_SCALPING_EXIT_CONFIG: dict = _load_scalping_exit_config()


def get_scalping_exit_config() -> dict:
    """对外暴露的只读访问器 (engine.py 等调用方使用)。"""
    return _SCALPING_EXIT_CONFIG


def get_phase_scaling(phase: str) -> Tuple[float, float]:
    """读取指定时段的 (tp_scale, sl_scale), 缺失返回 (1.0, 1.0)。

    SSOT: unified_config.yaml::scalping.phases (time_gate.py 使用)
    本函数仅供 SCALPING 退出系统在缺失上下文时兜底使用。
    """
    ps = (_SCALPING_EXIT_CONFIG.get("phase_scaling") or {}).get(phase) or {}
    return float(ps.get("tp_scale", 1.0)), float(ps.get("sl_scale", 1.0))


class FixedExit:
    """
    固定百分比止盈止损
    
    P0 修复 (2026-05-12): 放宽止损参数
    - 止损从 0.8% → 1.2% (减少被正常波动触发)
    - 止盈从 1.5% → 1.8% (保持盈亏比 1.5:1)
    
    做多:
      止盈 = entry × (1 + TP%)
      止损 = entry × (1 - SL%)
    
    做空:
      止盈 = entry × (1 - TP%)
      止损 = entry × (1 + SL%)
    """

    def __init__(self, take_profit_pct: float = 0.018, stop_loss_pct: float = 0.012):
        self.tp_pct = take_profit_pct
        self.sl_pct = stop_loss_pct

    def calculate_levels(self, entry_price: float, direction: str) -> Dict:
        """计算止盈止损价格"""
        if direction == "LONG":
            tp = round(entry_price * (1 + self.tp_pct), 2)
            sl = round(entry_price * (1 - self.sl_pct), 2)
        else:  # SHORT
            tp = round(entry_price * (1 - self.tp_pct), 2)
            sl = round(entry_price * (1 + self.sl_pct), 2)

        return {
            "entry": entry_price,
            "take_profit": tp,
            "stop_loss": sl,
            "tp_pct": self.tp_pct,
            "sl_pct": self.sl_pct,
            "direction": direction,
        }

    def check_exit(self, current_price: float, levels: Dict) -> Tuple[bool, str]:
        """
        检查是否触发止盈止损
        
        Returns:
            (should_exit: bool, reason: str)
        """
        if not levels:
            return False, ""

        direction = levels.get("direction", "LONG")
        tp = levels["take_profit"]
        sl = levels["stop_loss"]

        if direction == "LONG":
            if current_price >= tp:
                return True, f"止盈触发 (${current_price:.2f} >= ${tp:.2f})"
            if current_price <= sl:
                return True, f"止损触发 (${current_price:.2f} <= ${sl:.2f})"
        else:  # SHORT
            if current_price <= tp:
                return True, f"止盈触发 (${current_price:.2f} <= ${tp:.2f})"
            if current_price >= sl:
                return True, f"止损触发 (${current_price:.2f} >= ${sl:.2f})"

        return False, ""

    def update_params(self, take_profit_pct: float, stop_loss_pct: float):
        """更新参数"""
        self.tp_pct = take_profit_pct
        self.sl_pct = stop_loss_pct


class TimeExit:
    """
    时间止损 — 防止锁仓
    
    P2 修复 (2026-05-08): 盈亏感知型时间止损
    原逻辑: 持仓超过 max_hold_minutes → 无论盈亏，强制平仓
    新逻辑: 盈利续期、亏损提前
    
    原理: scalping 是短线策略，持仓过长意味着判断错误。
          如果判断正确，应该快速盈利。
          但盈利持仓不应被硬性时间止损打断。
    """

    def __init__(
        self,
        max_hold_minutes: Optional[int] = None,
        breakeven_after_minutes: int = 6,  # B.1 修复 (2026-05-19): 2→6 min, 给信号反弹时间
        breakeven_loss_threshold: float = -0.003,
    ):
        # B.1 修复 (2026-05-19): breakeven_after 2→6 min.
        # 5/12-5/18 数据: <6min 16 笔 0% 胜率 -$973 (摩擦+噪声灾难), 30min-2h 60% 胜率 +$742.
        # 原 breakeven_after=2 在 0% gross 时立即提前止损 → 摩擦吞利.
        # 改 6 min 给市场反应时间 (注意: 真 sl_hit 价格穿 SL 路径不受影响, 那是 atr_exit 走的).
        # Task #9 (2026-05-30): max_hold_minutes 从 exit_policy.yaml::scalping_overrides.time_exit 读取
        if max_hold_minutes is None:
            te_cfg = _SCALPING_EXIT_CONFIG.get("time_exit", {}) or {}
            max_hold_minutes = int(te_cfg.get("max_hold_minutes", 5))
            self._profit_extension_minutes = int(te_cfg.get("profit_extension_minutes", 0))
        else:
            self._profit_extension_minutes = 0
        self.max_hold = max_hold_minutes
        self.breakeven_after = breakeven_after_minutes
        self.breakeven_loss_threshold = breakeven_loss_threshold

    def should_exit(
        self, entry_time: datetime, current_pnl_pct: float
    ) -> Tuple[bool, str]:
        """
        盈亏感知型时间止损检查
        
        Args:
            entry_time: 开仓时间
            current_pnl_pct: 当前盈亏百分比 (如 -0.003 = -0.3%)
        
        Returns:
            (should_exit, reason)
        """
        now = datetime.now()
        hold_minutes = (now - entry_time).total_seconds() / 60.0

        # ═══════════════════════════════════════════════════════════
        # P2 修复 (2026-05-08): 盈亏感知型时间退出
        # 盈利时延长，亏损时提前，打破"66% 被时间止损打断"的瓶颈
        # ═══════════════════════════════════════════════════════════
        
        # 盈利持仓: 延长 50% 时间 (给利润奔跑的空间)
        if current_pnl_pct > 0:
            extended_max = self.max_hold * 1.5
            if hold_minutes >= extended_max:
                # 超过延长后时间，且利润已缩水到 <50% 峰值时才退出
                if current_pnl_pct < 0.003:  # 利润不足 0.3%，不值得继续持有
                    return True, (
                        f"盈利时间止损 (持仓 {hold_minutes:.0f}min >= {extended_max:.0f}min, "
                        f"利润仅 {current_pnl_pct:.1%})"
                    )
                # 仍有利润，继续持有
                return False, ""
            return False, ""
        
        # 小幅亏损 (-0.3% ~ 0%): 原逻辑不变
        if current_pnl_pct >= self.breakeven_loss_threshold:
            # 持仓超过 breakeven 时间且亏损，提前止损
            if hold_minutes >= self.breakeven_after:
                return True, (
                    f"提前止损 (持仓 {hold_minutes:.0f}min, "
                    f"亏损 {current_pnl_pct:.1%})"
                )
            # 未超过 breakeven 时间，继续持有观察
            return False, ""
        
        # 大幅亏损 (<-0.3%): 加速止损 (原 max_hold 减半)
        accelerated_max = self.max_hold * 0.5
        if hold_minutes >= accelerated_max:
            return True, (
                f"加速止损 (持仓 {hold_minutes:.0f}min >= {accelerated_max:.0f}min, "
                f"亏损 {current_pnl_pct:.1%})"
            )
        
        # 超过最大持仓时间，强制平仓 (最后防线)
        if hold_minutes >= self.max_hold:
            return True, f"时间止损 (持仓 {hold_minutes:.0f} 分钟 >= {self.max_hold}min)"

        return False, ""

    def update_params(
        self,
        max_hold_minutes: Optional[int] = None,
        breakeven_after_minutes: Optional[int] = None,
    ):
        """更新参数"""
        if max_hold_minutes is not None:
            self.max_hold = max_hold_minutes
        if breakeven_after_minutes is not None:
            self.breakeven_after = breakeven_after_minutes


class AdaptiveExit:
    """
    动态参数调整 — 根据连续盈亏调整止盈止损
    
    连赢 (≥3次):
      止盈 × 1.3 (让利润奔跑)
      止损 × 0.8 (收紧风控)
    
    连输 (≥2次):
      止盈 × 0.8 (降低期望)
      止损 × 0.7 (减少单次亏损)
    """

    def __init__(
        self,
        win_streak_threshold: int = 3,
        loss_streak_threshold: int = 2,
        win_tp_multiplier: float = 1.3,
        win_sl_multiplier: float = 0.8,
        loss_tp_multiplier: float = 0.8,
        loss_sl_multiplier: float = 0.7,
    ):
        self.win_streak_threshold = win_streak_threshold
        self.loss_streak_threshold = loss_streak_threshold
        self.win_tp_mult = win_tp_multiplier
        self.win_sl_mult = win_sl_multiplier
        self.loss_tp_mult = loss_tp_multiplier
        self.loss_sl_mult = loss_sl_multiplier

        self.consecutive_wins = 0
        self.consecutive_losses = 0

    def get_adjusted_levels(
        self, base_tp: float, base_sl: float
    ) -> Tuple[float, float]:
        """
        获取调整后的止盈止损
        
        Returns:
            (adjusted_tp, adjusted_sl)
        """
        if self.consecutive_wins >= self.win_streak_threshold:
            adj_tp = base_tp * self.win_tp_mult
            adj_sl = base_sl * self.win_sl_mult
            logger.debug(
                f"[AdaptiveExit] 连赢 {self.consecutive_wins} 次 → "
                f"TP×{self.win_tp_mult} SL×{self.win_sl_mult}"
            )
            return adj_tp, adj_sl

        elif self.consecutive_losses >= self.loss_streak_threshold:
            adj_tp = base_tp * self.loss_tp_mult
            adj_sl = base_sl * self.loss_sl_mult
            logger.debug(
                f"[AdaptiveExit] 连输 {self.consecutive_losses} 次 → "
                f"TP×{self.loss_tp_mult} SL×{self.loss_sl_mult}"
            )
            return adj_tp, adj_sl

        return base_tp, base_sl

    def record_trade(self, pnl_pct: float):
        """记录交易结果"""
        if pnl_pct > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0

    def reset(self):
        """重置计数"""
        self.consecutive_wins = 0
        self.consecutive_losses = 0

    def get_state(self) -> Dict:
        """获取当前状态"""
        return {
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "streak": (
                "win" if self.consecutive_wins > 0
                else "loss" if self.consecutive_losses > 0
                else "neutral"
            ),
        }


# ═══════════════════════════════════════════════════════════
# P2 修复 (2026-05-08): ATR 波动率自适应退出
# 根据市场实时波动率动态调整 TP/SL，解决固定阈值不适应的问题
# ═══════════════════════════════════════════════════════════

class ATRExit:
    """
    ATR 波动率自适应止盈止损
    
    核心逻辑:
      - 使用 ATR (Average True Range) 衡量当前市场波动率
      - TP = entry ± ATR × tp_multiplier (做多取 +，做空取 -)
      - SL = entry ∓ ATR × sl_multiplier (做多取 -，做空取 +)
    
    优势:
      - 高波动市: ATR 大 → TP/SL 自动放宽，避免被噪音震出
      - 低波动市: ATR 小 → TP/SL 自动收紧，提高盈亏比
      - 盈亏比固定为 tp_multiplier / sl_multiplier，不受波动率影响
    
    建议参数:
      - tp_multiplier: 1.5~2.0 (止盈为 ATR 的 1.5~2 倍)
      - sl_multiplier: 0.8~1.0 (止损为 ATR 的 0.8~1 倍)
      - 默认盈亏比 ~2:1
    """

    def __init__(
        self,
        tp_multiplier: Optional[float] = None,
        sl_multiplier: Optional[float] = None,
        min_tp_pct: Optional[float] = None,
        max_tp_pct: Optional[float] = None,
        min_sl_pct: Optional[float] = None,
        max_sl_pct: Optional[float] = None,
    ):
        # ═════════════════════════════════════════════════════════
        # Task #9 (2026-05-30): 参数从 exit_policy.yaml::scalping_overrides.atr_exit 读取
        # 优先级: 显式参数 > YAML 配置 > 代码默认值 (向后兼容)
        # ═════════════════════════════════════════════════════════
        cfg = _SCALPING_EXIT_CONFIG.get("atr_exit", {}) or {}

        def _pick(explicit, key, default):
            return explicit if explicit is not None else cfg.get(key, default)

        self.tp_mult = float(_pick(tp_multiplier, "tp_multiplier", 1.5))
        self.sl_mult = float(_pick(sl_multiplier, "sl_multiplier", 1.0))
        self.min_tp_pct = float(_pick(min_tp_pct, "min_tp_pct", 0.005))
        self.max_tp_pct = float(_pick(max_tp_pct, "max_tp_pct", 0.05))
        # 新增: 独立的 SL 上下限 (原代码复用 min_tp_pct*0.5/max_tp_pct*0.5, 保留其作为兑底)
        self.min_sl_pct = float(_pick(min_sl_pct, "min_sl_pct", self.min_tp_pct * 0.5))
        self.max_sl_pct = float(_pick(max_sl_pct, "max_sl_pct", self.max_tp_pct * 0.5))

        # 追踪止损配置 (供 check_exit 兑底使用, 优先读取 levels 传入值)
        trail_cfg = _SCALPING_EXIT_CONFIG.get("trailing", {}) or {}
        self._trail_enabled = bool(trail_cfg.get("enabled", True))
        self._trail_activation_pct = float(trail_cfg.get("activation_profit_pct", 0.01))
        self._trail_multiplier = float(trail_cfg.get("trail_multiplier", 1.5))
        self._trail_min_pct = float(trail_cfg.get("min_trail_pct", 0.0))

    def calculate_levels(
        self,
        entry_price: float,
        direction: str,
        atr_value: float,
        phase_tp_scale: float = 1.0,
        phase_sl_scale: float = 1.0,
    ) -> Dict:
        """
        根据 ATR 计算止盈止损价格（含 TimeGate 阶段自适应）
        
        Args:
            entry_price: 入场价格
            direction: "LONG" 或 "SHORT"
            atr_value: 当前 ATR 值 (如 14 周期 ATR)
            phase_tp_scale: TimeGate 阶段止盈缩放因子 (morning=1.2, midday=0.7 等)
            phase_sl_scale: TimeGate 阶段止损缩放因子
        
        Returns:
            {"entry": ..., "take_profit": ..., "stop_loss": ..., "tp_pct": ..., "sl_pct": ...}
        """
        # 阶段调整后的 multiplier
        adj_tp_mult = self.tp_mult * phase_tp_scale
        adj_sl_mult = self.sl_mult * phase_sl_scale
        
        if atr_value <= 0 or entry_price <= 0:
            # 回退到固定百分比模式（使用阶段调整后的值）
            base_tp_pct = max(self.min_tp_pct, min(self.max_tp_pct, 0.012 * adj_tp_mult))
            base_sl_pct = max(self.min_sl_pct, min(self.max_sl_pct, 0.007 * adj_sl_mult))
            return {
                "entry": entry_price,
                "take_profit": entry_price * (1 + base_tp_pct) if direction == "LONG" else entry_price * (1 - base_tp_pct),
                "stop_loss": entry_price * (1 - base_sl_pct) if direction == "LONG" else entry_price * (1 + base_sl_pct),
                "tp_pct": base_tp_pct,
                "sl_pct": base_sl_pct,
                "direction": direction,
                "atr_fallback": True,
                "phase_tp_scale": phase_tp_scale,
                "phase_sl_scale": phase_sl_scale,
            }

        # ATR 绝对值转百分比
        atr_pct = atr_value / entry_price

        # 计算 TP/SL 百分比, 使用阶段调整后的 multiplier, 限制在 [min, max] 范围内
        # Task #9 (2026-05-30): SL 范围使用独立的 min_sl_pct/max_sl_pct (原代码复用 TP 范围×0.5)
        tp_pct = max(self.min_tp_pct, min(self.max_tp_pct, atr_pct * adj_tp_mult))
        sl_pct = max(self.min_sl_pct, min(self.max_sl_pct, atr_pct * adj_sl_mult))
        
        if direction == "LONG":
            tp = round(entry_price * (1 + tp_pct), 2)
            sl = round(entry_price * (1 - sl_pct), 2)
        else:  # SHORT
            tp = round(entry_price * (1 - tp_pct), 2)
            sl = round(entry_price * (1 + sl_pct), 2)

        risk_reward = tp_pct / sl_pct if sl_pct > 0 else 0

        return {
            "entry": entry_price,
            "take_profit": tp,
            "stop_loss": sl,
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "direction": direction,
            "atr_value": atr_value,
            "atr_pct": atr_pct,
            "risk_reward_ratio": round(risk_reward, 2),
            "atr_fallback": False,
        }

    def check_exit(self, current_price: float, levels: Dict, peak_price: float = 0) -> Tuple[bool, str]:
        """
        检查是否触发 ATR 止盈止损 (接口与 FixedExit 兼容)
        
        P0 修复 (2026-05-09): 轻量追踪止损 — 优先级重新排序，确保 TP 优先于追踪止损。
        
        退出优先级链:
          1. TP 到达 → 立即止盈 (最高优先级)
          2. SL 触及 → 立即止损 (硬性底线)
          3. 追踪止损 → 回撤锁定 (仅在 TP 未到达且 SL 未触及时)
        
        这样确保追踪止损不会在 TP 之前截断利润。
        """
        if not levels:
            return False, ""

        direction = levels.get("direction", "LONG")
        tp = levels["take_profit"]
        sl = levels["stop_loss"]

        # ═══════════════════════════════════════════════════════════
        # 优先级 1: TP 止盈 (最先检查，确保不被追踪截断)
        # ═══════════════════════════════════════════════════════════
        if direction == "LONG":
            if current_price >= tp:
                return True, f"ATR止盈 (${current_price:.2f} >= ${tp:.2f})"
        else:  # SHORT
            if current_price <= tp:
                return True, f"ATR止盈 (${current_price:.2f} <= ${tp:.2f})"

        # ═══════════════════════════════════════════════════════════
        # 优先级 2: 轻量追踪止损 (TP 未到达时才检查)
        # 安全门控: trail_stop 必须比原始 SL 更紧才允许触发
        # Task #9 (2026-05-30): trail_mult / min_profit_to_activate 默认值从
        #   exit_policy.yaml::scalping_overrides.trailing 读取, levels 传入值仍可覆写
        # ═══════════════════════════════════════════════════════════
        if self._trail_enabled and peak_price > 0 and "atr_value" in levels and levels["atr_value"] > 0:
            entry_price = levels["entry"]
            atr = levels["atr_value"]
            trail_mult = levels.get("trail_mult", self._trail_multiplier)
            min_profit = levels.get("min_profit_to_activate", self._trail_activation_pct)
            
            pnl_pct = (current_price - entry_price) / entry_price if direction == "LONG" else (entry_price - current_price) / entry_price
            
            if pnl_pct >= min_profit:
                if direction == "LONG":
                    trail_stop = round(peak_price - atr * trail_mult, 2)
                    # 安全门控: 追踪止损必须比原始 SL 更高 (更紧) 才触发
                    if trail_stop > sl and current_price <= trail_stop:
                        return True, f"追踪止损 (peak=${peak_price:.2f}, trail=${trail_stop:.2f} > SL=${sl:.2f}, ATR×{trail_mult}, pnl={pnl_pct:.1%})"
                else:  # SHORT
                    trail_stop = round(peak_price + atr * trail_mult, 2)
                    # 安全门控: 追踪止损必须比原始 SL 更低 (更紧) 才触发
                    if trail_stop < sl and current_price >= trail_stop:
                        return True, f"追踪止损 (peak=${peak_price:.2f}, trail=${trail_stop:.2f} < SL=${sl:.2f}, ATR×{trail_mult}, pnl={pnl_pct:.1%})"

        # ═══════════════════════════════════════════════════════════
        # 优先级 3: 原始 SL 兜底 (TP 未到达 + 追踪未触发)
        # ═══════════════════════════════════════════════════════════
        if direction == "LONG":
            if current_price <= sl:
                return True, f"ATR止损 (${current_price:.2f} <= ${sl:.2f})"
        else:  # SHORT
            if current_price >= sl:
                return True, f"ATR止损 (${current_price:.2f} >= ${sl:.2f})"

        return False, ""
