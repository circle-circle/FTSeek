"""
app/strategy/scalping_strategy.py — Scalping 超短线策略 (统一多空)
设计原则: Karpathy 风格 (纯函数信号生成，极简上下文，自带风控参数)
"""
import logging
from collections import defaultdict
from typing import Optional, Dict

from .base import BaseStrategy, Signal, SignalAction
from .indicators import IndicatorBuffer, rsi
from app.core.types import SignalSource, TradeTier

logger = logging.getLogger("ftseek.strategy.scalping")

# ── 纯函数信号生成器 ───────────────────────────────────────
def generate_scalp_signal(ctx: Dict) -> Optional[Dict]:
    """
    ctx: {price, rsi, vol_ratio, vwap, support, resistance}
    返回: {"dir": "LONG/SHORT", "type": "...", "conf": 0-1, "sl_atr": float, "tp_atr": float}
    """
    p = ctx.get("price", 0)
    r = ctx.get("rsi", 50)
    v = ctx.get("vol_ratio", 1.0)
    sup = ctx.get("support", p)
    res = ctx.get("resistance", p)

    # 基础过滤：无波动/中性区直接跳过（节省计算）
    if abs(r - 50) < 15 and v < 0.8:
        return None

    # ── SHORT 方向 ──
    # A. 阻力位衰竭 (摸顶)：价格触顶 + RSI超买 + 量能萎缩
    if res > 0 and p >= res * 0.995 and r > 75 and v < 1.0:
        return {"dir": "SHORT", "type": "FADE_RES", "conf": 0.75, "sl_atr": 1.0, "tp_atr": 1.5}
    
    # B. 支撑破位 (杀跌)：价格跌穿 + 放量恐慌 + RSI中性偏空
    if sup > 0 and p <= sup * 0.995 and r < 45 and v > 1.3:
        return {"dir": "SHORT", "type": "BREAK_SUP", "conf": 0.85, "sl_atr": 0.8, "tp_atr": 1.2}

    # ── LONG 方向 ──
    # C. 支撑反弹 (抄底)：价格触底 + RSI超卖 + 量价平稳
    if sup > 0 and p <= sup * 1.005 and r < 25 and v < 1.1:
        return {"dir": "LONG", "type": "BOUNCE_SUP", "conf": 0.75, "sl_atr": 1.0, "tp_atr": 1.5}
    
    # D. 阻力突破 (追涨)：价格突破 + 放量确认 + RSI中性偏多
    if res > 0 and p >= res * 1.005 and r > 55 and v > 1.3:
        return {"dir": "LONG", "type": "BREAK_RES", "conf": 0.85, "sl_atr": 1.2, "tp_atr": 2.0}

    return None


class ScalpingStrategy(BaseStrategy):
    """
    Scalping 策略：
    - 独立于 confluence_v2 (不共享状态)
    - 兼容标准 Signal 接口 (无缝接入现有管线)
    - 支持多空双向 (由 generate_scalp_signal 统一裁决)
    """
    name = "scalping"
    
    def __init__(self, cfg: dict = None):
        super().__init__(cfg or {})
        # 默认启用状态由 config 控制
        self.enabled = self.config.get("enabled", True)
        self._buffers = defaultdict(IndicatorBuffer)
        
    def on_kline(self, code: str, kline: dict, kline_type: str, shared_buffer: list = None) -> Optional[Signal]:
        if not self.enabled:
            return None
            
        # 仅处理分钟级 K 线 (Scalping 需要高频率数据)
        if kline_type not in ("K_1M", "K_3M", "K_5M"):
            return None
            
        buf = self._buffers[code]
        buf.push_kline(kline)
        
        # 预热检查 (需要 30 根 K 线计算 RSI 和支撑阻力)
        if buf.size < 30:
            return None
            
        closes = list(buf.close)
        volumes = list(buf.volume)
        price = closes[-1]
        
        # 1. 计算指标
        rsi_val = rsi(closes, 14)
        if rsi_val is None:
            return None
            
        # 量能比 (当前量 / 过去20根均量)
        avg_vol = sum(volumes[-20:]) / 20
        vol_ratio = volumes[-1] / max(avg_vol, 1)
        
        # 支撑/阻力 (过去30根的高低点)
        resistance = max(buf.high)
        support = min(buf.low)
        
        # 2. 构建上下文并调用纯函数
        ctx = {
            "price": price,
            "rsi": rsi_val,
            "vol_ratio": vol_ratio,
            "vwap": price,  # 简化 VWAP
            "support": support,
            "resistance": resistance
        }
        
        signal_data = generate_scalp_signal(ctx)
        if not signal_data:
            return None
            
        # 3. 转换为标准 Signal 对象
        direction = signal_data["dir"]
        action = SignalAction.BUY if direction == "LONG" else SignalAction.SELL
        
        sig = Signal(
            code=code,
            action=action,
            price=price,
            reason=f"{signal_data['type']} | RSI:{rsi_val:.1f} Vol:{vol_ratio:.2f}x",
            strategy=self.name,
            tier=TradeTier.TIER_SCALP,
            extra={
                "signal_type": signal_data["type"],
                "conviction": signal_data["conf"],
                "sl_atr": signal_data["sl_atr"],
                "tp_atr": signal_data["tp_atr"]
            }
        )
        
        logger.info(f"[{self.name}] {code} {direction} | {signal_data['type']} (RSI:{rsi_val:.1f}, Vol:{vol_ratio:.2f}x)")
        return sig
