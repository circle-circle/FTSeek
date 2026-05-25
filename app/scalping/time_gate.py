"""
时段路由 v2.0 — 5 段细分 + 基础参数派生 + VIX 自适应 + 过渡平滑

美股时段特征 (U 型波动率 + U 型成交量):
  开盘驱动 09:35-10:00: 极高波动 (日振幅 40%) + 动量主导 + 宽价差
  早盘     10:00-11:30: 中高波动 + 趋势确立 + 流动性充裕
  午盘     11:30-14:00: 低波动 (日振幅 15%) + 均值回归主导 + VWAP 稳定
  午后     14:00-15:30: 成交量回升 + 方向受 MOC 预期影响
  收盘驱动 15:30-16:00: MOC 失衡风险 + 快进快出 + 15:55 强制平仓

幻方原则:
  - 用 base_params + phase_scalers 替代独立时段参数 (防止组合爆炸)
  - scaler 物理含义: avg(phase_amplitude) / avg(global_amplitude)

Karpathy 原则:
  - Gradients over rules: scaler 从历史数据自动校准
  - Explicit over implicit: 每个时段 enabled 显性开关
"""

import pytz
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

LONG = "LONG"
SHORT = "SHORT"

# 默认 5 阶段配置 (可被 unified_config.yaml 覆盖)
DEFAULT_PHASES_CONFIG = {
    "base_params": {
        "tp_pct": 0.025,       # P0 重写 (2026-05-20): 1.2% → 2.5%
        "sl_pct": 0.020,       # P0 重写 (2026-05-20): 0.7% → 2.0%
        "position_pct": 0.08,
        "max_hold_minutes": 60, # P0 重写 (2026-05-20): 4 → 60
    },
    "opening_drive": {
        "enabled": False,
        "start_minutes": 575,       # 09:35
        "end_minutes": 600,         # 10:00
        "directions": ["LONG", "SHORT"],
        "signals": ["momentum", "breakout"],
        "tp_scale": 1.5,
        "sl_scale": 1.5,
        "pos_scale": 0.5,
        "hold_scale": 1.5,
    },
    "morning": {
        "enabled": True,
        "start_minutes": 600,       # 10:00
        "end_minutes": 690,         # 11:30
        "directions": ["LONG", "SHORT"],
        "signals": ["rsi", "vwap", "momentum"],
        "tp_scale": 1.2,
        "sl_scale": 1.2,
        "pos_scale": 1.0,
        "hold_scale": 1.2,
    },
    "midday": {
        "enabled": True,
        "start_minutes": 690,       # 11:30
        "end_minutes": 840,         # 14:00
        "directions": ["LONG", "SHORT"],
        "signals": ["rsi", "bollinger"],
        "tp_scale": 0.7,
        "sl_scale": 0.7,
        "pos_scale": 0.9,
        "hold_scale": 1.0,
    },
    "afternoon": {
        "enabled": True,
        "start_minutes": 840,       # 14:00
        "end_minutes": 930,         # 15:30
        "directions": ["LONG"],
        "signals": ["rsi", "vwap", "momentum"],
        "tp_scale": 1.0,
        "sl_scale": 1.0,
        "pos_scale": 0.9,
        "hold_scale": 1.0,
    },
    "closing_drive": {
        "enabled": True,
        "start_minutes": 930,       # 15:30
        "end_minutes": 960,         # 16:00
        "directions": ["LONG"],
        "signals": ["rsi"],
        "tp_scale": 0.5,
        "sl_scale": 0.7,
        "pos_scale": 0.3,
        "hold_scale": 0.5,
        "force_close_at": 955,      # 15:55 强制平仓
        "no_new_entry_after": 950,  # 15:50 禁止开仓
    },
    "transition_smoothing_minutes": 2,
}

# 系统保留关键字
_META_KEYS = {"base_params", "transition_smoothing_minutes"}

# 时段标签 (日志用)
PHASE_LABELS = {
    "opening_drive":  "开盘驱动 (09:35-10:00 ET)",
    "morning":         "早盘 (10:00-11:30 ET)",
    "midday":          "午盘 (11:30-14:00 ET)",
    "afternoon":       "午后 (14:00-15:30 ET)",
    "closing_drive":   "收盘驱动 (15:30-16:00 ET)",
}


class TimeGate:
    """
    美东时段路由 v2.0

    改进:
      - 5 段细分替代旧 3 段
      - base_params + phase_scalers 替代独立参数
      - 时段切换前 transition_smoothing_minutes 使用线性插值
      - 收盘 MOC 保护: force_close_at + no_new_entry_after
      - VIX 自适应止损叠加 (通过 shared_kernel)
    """

    def __init__(self, phases_config: Optional[Dict] = None, shared_kernel=None):
        self.cfg = phases_config or DEFAULT_PHASES_CONFIG
        self.base = self.cfg.get("base_params", DEFAULT_PHASES_CONFIG["base_params"])
        self.smoothing = self.cfg.get("transition_smoothing_minutes", 2)
        self._kernel = shared_kernel
        self._cache: Optional[Tuple[str, Dict, datetime]] = None  # (phase, params, cached_at)
        self._cache_ttl_seconds: float = 1.0  # 1 秒缓存防抖

        # 构建有效阶段列表 (排除 meta keys 和 disabled 阶段)
        self._phase_names = []
        for name in self.cfg:
            if name in _META_KEYS:
                continue
            phase_cfg = self.cfg[name]
            if phase_cfg.get("enabled", True):
                self._phase_names.append(name)
        self._phase_names.sort(key=lambda n: self.cfg[n]["start_minutes"])

        logger.info(
            f"[TimeGate v2] 已启用 {len(self._phase_names)} 个时段: "
            f"{', '.join(self._phase_names)}"
        )

    # ── 时间工具 ──

    def get_now_et(self) -> datetime:
        return datetime.now(ET)

    @staticmethod
    def _to_et_minutes(dt: datetime) -> int:
        if dt.tzinfo is None:
            dt = ET.localize(dt)
        return dt.hour * 60 + dt.minute

    # ── 时段查询 ──

    def get_current_phase(self, dt: Optional[datetime] = None) -> str:
        """获取当前时段名称 (不含过渡逻辑)"""
        dt = dt or self.get_now_et()
        minutes = self._to_et_minutes(dt)

        for name in self._phase_names:
            p = self.cfg[name]
            if p["start_minutes"] <= minutes < p["end_minutes"]:
                return name
        return "closed"

    def is_trading_allowed(self, dt: Optional[datetime] = None) -> bool:
        dt = dt or self.get_now_et()
        # P0 修复 (2026-05-26): 周末 + 节假日检查 — 防止非交易日触发交易
        if dt.weekday() >= 5:  # 周六=5, 周日=6
            return False
        try:
            from app.utils.holiday_calendar import get_holiday_calendar
            if get_holiday_calendar().is_us_holiday(dt.date()):
                return False
        except Exception:
            pass  # 日历加载失败不阻断，退化为仅时间判断
        return self.get_current_phase(dt) != "closed"

    # ── 过渡检测 ──

    def _detect_transition(self, minutes: int) -> Optional[Tuple[str, str, float]]:
        """
        检测是否处于时段过渡窗口

        若当前分钟数落在 phase_a 结束前 smoothing 分钟内，
        且存在 phase_b 从 phase_a 结束分钟开始，则处于过渡期。

        Returns:
            None 如果不在过渡期
            (phase_from, phase_to, progress) 其中 progress ∈ [0, 1]
        """
        for i, name in enumerate(self._phase_names):
            p = self.cfg[name]
            end = p["end_minutes"]
            if end - self.smoothing <= minutes < end:
                # 查找下一个阶段
                next_name = None
                for j in range(i + 1, len(self._phase_names)):
                    next_p = self.cfg[self._phase_names[j]]
                    if next_p["start_minutes"] == end:
                        next_name = self._phase_names[j]
                        break
                if next_name:
                    progress = (minutes - (end - self.smoothing)) / self.smoothing
                    return (name, next_name, progress)
        return None

    # ── 参数派生 (核心) ──

    def _derive_phase_params(self, phase_name: str) -> Dict:
        """从 base_params + phase_scalers 派生时段特定参数"""
        p = self.cfg.get(phase_name)
        if not p:
            return {}

        tp = self.base.get("tp_pct", 0.012) * p.get("tp_scale", 1.0)
        sl = self.base.get("sl_pct", 0.007) * p.get("sl_scale", 1.0)
        pos = self.base.get("position_pct", 0.08) * p.get("pos_scale", 1.0)
        hold = int(self.base.get("max_hold_minutes", 4) * p.get("hold_scale", 1.0))

        # VIX 自适应止损叠加 (通过 SharedKernel)
        if self._kernel:
            sl = self._kernel.get_phase_adjusted_sl(sl, phase_name)

        return {
            "phase": phase_name,
            "phase_label": PHASE_LABELS.get(phase_name, phase_name),
            "directions": p.get("directions", ["LONG", "SHORT"]),
            "take_profit": round(tp, 4),
            "stop_loss": round(sl, 4),
            "position_pct": round(pos, 4),
            "max_hold_minutes": max(1, hold),
            "signals": p.get("signals", ["rsi"]),
            "force_close_at": p.get("force_close_at"),        # None 除非 closing_drive
            "no_new_entry_after": p.get("no_new_entry_after"),  # None 除非 closing_drive
        }

    def _interpolate_params(self, p1: Dict, p2: Dict, progress: float) -> Dict:
        """线性插值两个时段的参数 (过渡期使用)"""
        w2 = progress
        w1 = 1.0 - progress

        # directions: 取并集 (过渡期内两个方向都允许)
        dirs = list(set(p1.get("directions", []) + p2.get("directions", [])))

        # signals: 取并集
        sigs = list(set(p1.get("signals", []) + p2.get("signals", [])))

        return {
            "phase": f"{p1['phase']}→{p2['phase']}",
            "phase_label": f"{PHASE_LABELS.get(p1['phase'], p1['phase'])} →过渡→ {PHASE_LABELS.get(p2['phase'], p2['phase'])}",
            "directions": dirs,
            "take_profit": round(w1 * p1["take_profit"] + w2 * p2["take_profit"], 4),
            "stop_loss": round(w1 * p1["stop_loss"] + w2 * p2["stop_loss"], 4),
            "position_pct": round(w1 * p1["position_pct"] + w2 * p2["position_pct"], 4),
            "max_hold_minutes": max(1, int(w1 * p1["max_hold_minutes"] + w2 * p2["max_hold_minutes"])),
            "signals": sigs,
            # 过渡期不传 MOC 约束 (force_close_at 等在新阶段生效)
            "force_close_at": None,
            "no_new_entry_after": None,
            "_transition": True,
        }

    # ── 公共 API ──

    def get_params(self, dt: Optional[datetime] = None) -> Optional[Dict]:
        """
        获取当前时段的交易参数 (含过渡平滑)

        缓存 1 秒防抖，避免同一 tick 内重复计算。
        """
        dt = dt or self.get_now_et()

        # 1 秒缓存 (仅当 dt 接近时使用)
        if self._cache:
            cached_dt = self._cache[2]
            delta = abs((dt - cached_dt).total_seconds())
            if delta < self._cache_ttl_seconds:
                return self._cache[1]

        minutes = self._to_et_minutes(dt)
        params = self._get_params_impl(minutes)

        self._cache = (self.get_current_phase(dt), params, dt)
        return params

    def _get_params_impl(self, minutes: int) -> Optional[Dict]:
        # 1. 检查是否处于过渡窗口
        transition = self._detect_transition(minutes)
        if transition:
            from_phase, to_phase, progress = transition
            p1 = self._derive_phase_params(from_phase)
            p2 = self._derive_phase_params(to_phase)
            result = self._interpolate_params(p1, p2, progress)
            logger.debug(
                f"[TimeGate] 过渡期 ({progress:.0%}): {from_phase} → {to_phase} | "
                f"TP: {p1['take_profit']:.2%}→{p2['take_profit']:.2%} | "
                f"SL: {p1['stop_loss']:.2%}→{p2['stop_loss']:.2%}"
            )
            return result

        # 2. 直接匹配阶段
        for name in self._phase_names:
            p = self.cfg[name]
            if p["start_minutes"] <= minutes < p["end_minutes"]:
                return self._derive_phase_params(name)

        return None

    # ── MOC 保护 ──

    def is_moc_force_close_time(self, dt: Optional[datetime] = None) -> bool:
        """是否到达 MOC 强制平仓时间 (15:55+)

        C4 修复 (2026-05-13): closing_drive 配置优先, fallback 到全局 EOD 配置
        """
        dt = dt or self.get_now_et()
        minutes = self._to_et_minutes(dt)

        cd = self.cfg.get("closing_drive", {})
        force_close_at = cd.get("force_close_at")
        if force_close_at is None:
            from app.risk.eod_config import load_eod_config
            force_close_at = load_eod_config().force_close_minutes
        if force_close_at and minutes >= force_close_at:
            return True
        return False

    def is_new_entry_blocked(self, dt: Optional[datetime] = None) -> bool:
        """是否禁止开新仓 (MOC 保护: 15:50+)

        C4 修复 (2026-05-13): closing_drive 配置优先, fallback 到全局 EOD 配置
        """
        dt = dt or self.get_now_et()
        minutes = self._to_et_minutes(dt)

        cd = self.cfg.get("closing_drive", {})
        no_new_after = cd.get("no_new_entry_after")
        if no_new_after is None:
            from app.risk.eod_config import load_eod_config
            no_new_after = load_eod_config().no_new_entry_minutes
        if no_new_after and minutes >= no_new_after:
            return True
        return False

    def should_force_close_all(self, dt: Optional[datetime] = None) -> bool:
        """是否需要强制平仓所有仓位"""
        return self.is_moc_force_close_time(dt)

    # ── 查询接口 ──

    def get_allowed_directions(self, dt: Optional[datetime] = None) -> List[str]:
        params = self.get_params(dt)
        return params["directions"] if params else []

    def log_phase_info(self, dt: Optional[datetime] = None):
        dt = dt or self.get_now_et()
        params = self.get_params(dt)

        if params:
            extra = ""
            if params.get("_transition"):
                extra = " [过渡期]"
            logger.info(
                f"[TimeGate] 当前: {params['phase_label']}{extra} | "
                f"方向: {params['directions']} | "
                f"TP: {params['take_profit']:.1%} | "
                f"SL: {params['stop_loss']:.1%} | "
                f"最长持仓: {params['max_hold_minutes']}min"
            )
        else:
            logger.info(f"[TimeGate] 当前: {self.get_current_phase(dt)} — 不交易")

    def update_schedule(self, phases_config: Dict):
        """更新时段配置 (热重载)"""
        self.cfg = phases_config
        self.base = phases_config.get("base_params", self.base)
        self.smoothing = phases_config.get("transition_smoothing_minutes", self.smoothing)

        self._phase_names = []
        for name in self.cfg:
            if name in _META_KEYS:
                continue
            phase_cfg = self.cfg[name]
            if phase_cfg.get("enabled", True):
                self._phase_names.append(name)
        self._phase_names.sort(key=lambda n: self.cfg[n]["start_minutes"])

        self._cache = None
        logger.info("[TimeGate v2] 时段配置已热重载")
