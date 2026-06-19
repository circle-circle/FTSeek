"""
共享内核 (Shared Kernel) — 多引擎共用的 SSOT 参数源

幻方原则: 多个策略共享一个内核，内核只做 SSOT，不做决策。
策略做自己的决策，但数据读写必须经过内核。

提供:
  - VIX regime 查询 (统一市场阶段分级)
  - 仓位乘数 (基于 VIX 的仓位调整系数)
  - 止损宽度调整 (基于 VIX 的止损距离系数)
  - BB 投票权重调整 (基于 VIX 的布林带权重)
  - 时段缩放因子 (从基础参数生成时段特定参数)
  - 统一时段分类 (entry + exit 共用一套 taxonomy)
  - session_vol_ratio (所有退出规则通过 ATR × ratio 获得时段感知)

Karpathy 原则: 只有一个地方定义规则。删除两个引擎中各自重复的 VIX 调整逻辑。

幻方 P2 (2026-05-01): 时段 taxonomy 统一
  - 入口侧 intraday_scorer 的 OPEN/MID_MORNING/LUNCH/AFTERNOON/CLOSE
    和出口侧 DEFAULT_PHASE_SCALERS 的 opening_drive/morning/midday/afternoon/closing_drive
    合并为一套标准名 (opening_drive / morning / midday / afternoon / closing_drive / after_hours)
  - 新增 get_current_session() → 入口和出口都用同一个函数获取时段
  - 新增 get_session_vol_ratio() → 所有退出规则通过 effective_atr = atr × ratio 获得时段感知
  - 0 个新参数，1 处改动点 (ATR 校准)，全局 session 感知
"""

import logging
import threading
from datetime import datetime
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ============================================================
# VIX 市场阶段分级 (SSOT)
# ============================================================
VIX_REGIMES = {
    "euphoria":  (0, 12),     # VIX < 12: 过度乐观
    "normal":    (12, 20),    # VIX 12-20: 正常
    "caution":   (20, 30),    # VIX 20-30: 谨慎
    "fear":      (30, 40),    # VIX 30-40: 恐慌
    "crisis":    (40, 100),   # VIX > 40: 危机
}

# ============================================================
# 基础参数 (全局默认，所有时段参数由此派生)
# ============================================================
@dataclass
class BaseScalpingParams:
    """Scalping 基础参数 — 时段参数通过 scaler 自动生成"""
    tp_pct: float = 0.012          # 基础止盈 (1.2%)
    sl_pct: float = 0.007          # 基础止损 (0.7%)
    position_pct: float = 0.08     # 基础单笔仓位 (8% of budget)
    max_hold_minutes: int = 4      # 基础最大持仓 (4 分钟)

# ============================================================
# 时段缩放因子 (从历史振幅比率推导)
# ============================================================
# 缩放因子物理含义: phase_scaler = avg(phase_amplitude) / avg(global_amplitude)
# 高波动时段 → scale > 1.0 (TP/SL 放宽), position_scale 反向 (仓位收缩)
DEFAULT_PHASE_SCALERS = {
    "opening_drive":    {"tp_scale": 1.5, "sl_scale": 1.5, "pos_scale": 0.5,  "hold_scale": 1.5},
    "morning":          {"tp_scale": 1.2, "sl_scale": 1.2, "pos_scale": 1.0,  "hold_scale": 1.2},
    "midday":           {"tp_scale": 0.7, "sl_scale": 0.7, "pos_scale": 0.9,  "hold_scale": 1.0},
    "afternoon":        {"tp_scale": 1.0, "sl_scale": 1.0, "pos_scale": 0.9,  "hold_scale": 1.0},
    "closing_drive":    {"tp_scale": 0.5, "sl_scale": 0.7, "pos_scale": 0.3,  "hold_scale": 0.5},
}

# VIX 自适应乘数 (跨时段通用，但各时段有折扣系数)
VIX_SL_MULTIPLIERS = {
    "crisis":  2.0,    # VIX > 40
    "fear":    1.8,    # VIX 30-40
    "caution": 1.3,    # VIX 20-30
    "normal":  1.0,    # VIX 12-20
    "euphoria": 0.75,  # VIX < 12
}

# 各时段对 VIX 的敏感度折扣 (午盘天然低波动，不应过度放大 SL)
PHASE_VIX_WEIGHTS = {
    "opening_drive":  1.0,   # 开盘自身波动大，VIX 系数全额
    "morning":        0.8,   # 早盘波动已降低
    "midday":         0.5,   # 午盘天然低波动，别过度放宽
    "afternoon":      0.8,
    "closing_drive":  0.6,   # 收盘快进快出，SL 不宜过宽
}

# ── 统一时段 taxonomy (P2 2026-05-01) ──
# 入口侧 (intraday_scorer) 和出口侧 (ExitMonitor) 共用一套标准名。
# 所有旧命名通过此映射归一化到 canonical session name。

_SESSION_NORMALIZE = {
    # intraday_scorer 旧名 → canonical
    "OPEN":          "opening_drive",
    "MID_MORNING":   "morning",
    "LUNCH":         "midday",
    "AFTERNOON":     "afternoon",
    "CLOSE":         "closing_drive",
    "AFTER_HOURS":   "after_hours",
    # canonical 自映射 (幂等)
    "opening_drive":  "opening_drive",
    "morning":        "morning",
    "midday":         "midday",
    "afternoon":      "afternoon",
    "closing_drive":  "closing_drive",
    "after_hours":    "after_hours",
}

# canonical session → volatility ratio vs daily average
# 值 = 该时段典型振幅 / 全天平均振幅
# 由 calibrate_scalers_from_data() 从实盘数据自动校准
# 所有退出规则通过 effective_atr = atr × ratio 获得 session 感知
DEFAULT_SESSION_VOL_RATIO = {
    "opening_drive": 1.50,   # 开盘 1.5x 日均波动
    "morning":       1.20,   # 早盘 1.2x
    # 优化 (2026-05-30 Task#7): midday 0.60 → 0.75, 避免午盘 effective_atr 被压得过低导致止损过紧
    "midday":        0.75,   # 午盘 0.75x (原 0.60, 适度抬升避免噪音误触)
    "afternoon":     0.95,   # 午后逐步回升
    # 优化 (2026-05-30 Task#7): closing_drive 1.15 → 1.0, 收盘前不需要放宽 ATR (回归基准)
    "closing_drive": 1.00,   # 尾盘 1.0x (原 1.15, 临近收盘不再放宽)
    "after_hours":   0.80,   # 盘后流动性薄, 有效波动低但跳空风险高
}

# P1 G12: 入场止损 session baseline — 与 DEFAULT_SESSION_VOL_RATIO 联动校准
# 值 = 该时段入场止损距离的基准乘数 (1.0 = 使用默认止损)
# 由 ExitQualityTracker.apply_calibration() 同步更新
ENTRY_SL_SESSION_BASELINE = {
    "opening_drive": 1.50,
    "morning":       1.20,
    "midday":        0.60,
    "afternoon":     0.95,
    "closing_drive": 1.15,
    "after_hours":   0.80,
}

# 模块级锁 — 保护 calibrate_scalers_from_data() 写入
# 防止 ExitMonitor 读取时与校准任务写入冲突 (R2 修复)
_calibration_lock = threading.RLock()

# 仓位乘数 (基于 VIX regime，两个引擎通用)
POSITION_MULTIPLIERS = {
    "euphoria": 0.7,
    "normal":   1.0,
    "caution":  0.7,
    "fear":     0.3,
    "crisis":   0.0,
}


class SharedKernel:
    """
    共享内核 — 多引擎 VIX/风控参数的 SSOT

    使用方法:
      kernel = SharedKernel(vix_provider)
      multipliers = kernel.get_vix_multipliers()
      phase_params = kernel.derive_phase_params("morning", base_params)
    """

    def __init__(self, vix_provider=None):
        """
        Args:
            vix_provider: 可调用对象，返回当前 VIX 值 (float or None)。
                         通常为 MarketStateChecker 实例或 data_router.get_vix()。
        """
        self._vix_provider = vix_provider
        self._vix_cache: Optional[Tuple[float, float]] = None  # (vix, timestamp)

    # ── VIX 查询 ──

    def get_vix(self) -> Optional[float]:
        """获取当前 VIX 值 (带简单缓存: 60s 内不重复查询)"""
        import time
        now = time.time()
        if self._vix_cache and (now - self._vix_cache[1]) < 60:
            return self._vix_cache[0]

        if self._vix_provider is None:
            return None

        try:
            vix = self._vix_provider()
            if vix is not None and vix > 0:
                self._vix_cache = (vix, now)
                return vix
        except Exception:
            pass
        return self._vix_cache[0] if self._vix_cache else None

    def get_vix_regime(self) -> str:
        """基于 VIX 判断市场阶段 → 'euphoria' | 'normal' | 'caution' | 'fear' | 'crisis'"""
        vix = self.get_vix()
        if vix is None:
            return "normal"  # 默认正常
        for regime, (low, high) in VIX_REGIMES.items():
            if low <= vix < high:
                return regime
        return "crisis"

    # ── 统一乘数 (两个引擎共用) ──

    def get_vix_multipliers(self) -> Dict:
        """
        返回两个引擎统一使用的 VIX 乘数

        Returns:
            {
                "vix": float or None,
                "regime": str,
                "position_multiplier": float,     # 仓位调整 (0.0-1.0)
                "stop_loss_multiplier": float,    # 止损距离调整
                "bb_weight": float,               # BB 投票权重
            }
        """
        regime = self.get_vix_regime()
        vix = self.get_vix()

        pos_mult = POSITION_MULTIPLIERS.get(regime, 0.5)
        sl_mult = VIX_SL_MULTIPLIERS.get(regime, 1.0)

        # BB 权重: VIX > 30 (fear/crisis) 时 BB 突破无统计意义
        if regime in ("fear", "crisis"):
            bb_weight = 0.0
        elif regime == "caution":
            bb_weight = 0.5
        else:
            bb_weight = 1.0

        return {
            "vix": vix,
            "regime": regime,
            "position_multiplier": pos_mult,
            "stop_loss_multiplier": sl_mult,
            "bb_weight": bb_weight,
        }

    def get_phase_adjusted_sl(self, base_sl_pct: float, phase: str) -> float:
        """
        计算经时段折扣后的 VIX 自适应止损

        公式: sl = base_sl × (1 + (vix_mult - 1) × phase_weight)

        午盘 phase_weight=0.5 时，VIX 恐慌导致的 SL 放宽效果减半。
        """
        multipliers = self.get_vix_multipliers()
        vix_mult = multipliers["stop_loss_multiplier"]
        phase_weight = PHASE_VIX_WEIGHTS.get(phase, 0.8)
        return base_sl_pct * (1.0 + (vix_mult - 1.0) * phase_weight)

    # ── 统一时段 API (P2 2026-05-01) ──

    @staticmethod
    def normalize_session(session_name: str) -> str:
        """将任意 session 名归一化到 canonical name

        入口侧 "OPEN" → "opening_drive"
        出口侧 "opening_drive" → "opening_drive" (幂等)
        未知名 → "after_hours" (安全降级)
        """
        if not session_name:
            return "after_hours"
        return _SESSION_NORMALIZE.get(session_name, "after_hours")

    @staticmethod
    def get_current_session() -> str:
        """识别当前美股日内时段 → canonical name

        基于美东时间 (ET / America/New_York):
          opening_drive:  09:30-10:30  (全日最高波动, 1.5x 日均ATR)
          morning:        10:30-11:30  (仍偏高, 1.2x)
          midday:         11:30-14:00  (全日最低波动, 0.6x, 缩量整理)
          afternoon:      14:00-15:00  (回升, 0.95x)
          closing_drive:  15:00-16:00  (尾盘压力, 1.15x)
          after_hours:    其他时间

        入口和出口共用此函数 — SSOT。
        """
        try:
            from pytz import timezone as _tz
            ET = _tz("America/New_York")
            now_et = datetime.now(ET)
        except Exception:
            now_et = datetime.now()

        if now_et.weekday() >= 5:
            return "after_hours"

        t = now_et.hour * 60 + now_et.minute

        if 570 <= t < 630:     # 09:30-10:30
            return "opening_drive"
        elif 630 <= t < 690:   # 10:30-11:30
            return "morning"
        elif 690 <= t < 840:   # 11:30-14:00
            return "midday"
        elif 840 <= t < 900:   # 14:00-15:00
            return "afternoon"
        elif 900 <= t <= 960:  # 15:00-16:00
            return "closing_drive"
        return "after_hours"

    def get_session_vol_ratio(self, session: str = None) -> float:
        """获取时段波动率比值 (session AT multiplier for exit rules)

        session: canonical name (opening_drive/morning/midday/...)
                 None → 自动检测当前时段

        Returns:
            session_vol_ratio: 该时段典型振幅 / 日均振幅
            所有退出规则通过 effective_atr = atr × ratio 获得 session 感知

        例:
          opening_drive → 1.50  (开盘 ATR 等效放大 1.5x → 所有止损自动放宽)
          midday        → 0.60  (午盘 ATR 等效缩小 0.6x → 所有止损自动收紧)
        """
        if session is None:
            session = self.get_current_session()
        session = self.normalize_session(session)
        return DEFAULT_SESSION_VOL_RATIO.get(session, 1.0)

    # ── 时段参数派生 (用 scaler 替代独立参数) ──

    def derive_phase_params(self, phase: str, base: Optional[BaseScalpingParams] = None,
                            scalers: Optional[Dict] = None) -> Dict:
        """
        从基础参数 + 时段缩放因子 派生时段特定参数

        Args:
            phase: 时段名称
            base: 基础参数 (默认 BaseScalpingParams())
            scalers: 时段缩放因子 (默认 DEFAULT_PHASE_SCALERS)

        Returns:
            {"take_profit": ..., "stop_loss": ..., "position_pct": ...,
             "max_hold_minutes": ...}
        """
        if base is None:
            base = BaseScalpingParams()
        if scalers is None:
            scalers = DEFAULT_PHASE_SCALERS

        s = scalers.get(phase, {"tp_scale": 1.0, "sl_scale": 1.0,
                                "pos_scale": 1.0, "hold_scale": 1.0})

        tp = base.tp_pct * s["tp_scale"]
        sl = base.sl_pct * s["sl_scale"]
        pos = base.position_pct * s["pos_scale"]
        hold = int(base.max_hold_minutes * s["hold_scale"])

        # VIX 自适应止损叠加
        sl = self.get_phase_adjusted_sl(sl, phase)

        return {
            "take_profit": round(tp, 4),
            "stop_loss": round(sl, 4),
            "position_pct": round(pos, 4),
            "max_hold_minutes": max(1, hold),
        }

    def calibrate_scalers_from_data(self, phase_amplitudes: Dict[str, float]):
        """
        从历史振幅数据自动校准时段缩放因子

        Karpathy: Gradients over rules — 用数据推导参数。

        Args:
            phase_amplitudes: {"morning": 0.018, "midday": 0.008, ...}
                              每个时段的平均历史振幅

        同步更新:
          - DEFAULT_PHASE_SCALERS
          - DEFAULT_SESSION_VOL_RATIO (退出规则用 effective_atr = atr × ratio)
        """
        if not phase_amplitudes:
            return

        # normalize keys through _SESSION_NORMALIZE
        normalized_amps: Dict[str, float] = {}
        for phase, amp in phase_amplitudes.items():
            canon = SharedKernel.normalize_session(phase)
            if canon not in normalized_amps:
                normalized_amps[canon] = amp
            else:
                normalized_amps[canon] = (normalized_amps[canon] + amp) / 2.0

        if not normalized_amps:
            return

        global_avg = sum(normalized_amps.values()) / len(normalized_amps)
        if global_avg <= 0:
            return

        for phase, amp in normalized_amps.items():
            scale = amp / global_avg
            with _calibration_lock:
                # 同步 DEFAULT_PHASE_SCALERS
                if phase in DEFAULT_PHASE_SCALERS:
                    DEFAULT_PHASE_SCALERS[phase]["tp_scale"] = round(scale, 2)
                    DEFAULT_PHASE_SCALERS[phase]["sl_scale"] = round(scale, 2)
                    DEFAULT_PHASE_SCALERS[phase]["pos_scale"] = round(1.0 / max(scale, 0.3), 2)
                # 同步 DEFAULT_SESSION_VOL_RATIO
                if phase in DEFAULT_SESSION_VOL_RATIO:
                    DEFAULT_SESSION_VOL_RATIO[phase] = round(scale, 2)
                # 注: ENTRY_SL_SESSION_BASELINE 已预留但暂未使用
                # 入场止损现已使用 session_vol_ratio (P1-2 修复)，不再需要此独立配置

        result = {}
        for p, s in DEFAULT_PHASE_SCALERS.items():
            result[p] = '{:.2f}'.format(s["tp_scale"])

        logger.info(
            "[SharedKernel] 时段缩放因子已从数据校准: scalers=%s vol_ratios=%s",
            result,
            {k: f'{v:.2f}' for k, v in DEFAULT_SESSION_VOL_RATIO.items()},
        )


# ============================================================
# 全局单例
# ============================================================
_kernel: Optional[SharedKernel] = None


def get_shared_kernel() -> SharedKernel:
    """获取全局共享内核"""
    global _kernel
    if _kernel is None:
        _kernel = SharedKernel()
    return _kernel


def init_shared_kernel(vix_provider=None) -> SharedKernel:
    """初始化全局共享内核 (注入 VIX 数据源)"""
    global _kernel
    _kernel = SharedKernel(vix_provider=vix_provider)
    logger.info("[SharedKernel] 全局共享内核已初始化")
    return _kernel
