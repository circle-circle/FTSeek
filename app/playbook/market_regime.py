"""
Market Regime Gate - 市场环境门禁

基于 SPY 趋势 + VIX 波动率判断市场环境，决定今天的操作基调。

Regime 判定：
- RISK_ON: 市场趋势向上，VIX 低，可以正常操作
- CAUTIOUS: 市场不确定，需要缩小仓位
- RISK_OFF: 市场风险偏高，防守为主
- NO_TRADE: 极端环境，全部 SKIP

设计原则：
- 简单的规则判断，不是 LLM 预测
- 使用现有 MarketDataRouter 和 AlphaVantageProvider
- 每个 regime 影响后续 Screener 的 top_n 和仓位倍率
"""
import logging
from typing import Dict, Any, Optional
from enum import Enum
from dataclasses import dataclass

from app.data.market_data_router import MarketDataRouter, get_market_data_router
from app.data.alphavantage_provider import AlphaVantageProvider, get_alpha_vantage_provider

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """市场环境枚举"""
    RISK_ON = "risk_on"
    CAUTIOUS = "cautious"
    RISK_OFF = "risk_off"
    NO_TRADE = "no_trade"


@dataclass
class RegimeContext:
    """市场环境上下文"""
    regime: MarketRegime
    vix: float
    spy_price: float
    spy_ma20: float
    spy_ma50: float
    spy_ma200: float
    spy_change_pct: float
    score: float  # 综合评分；VIX 20–22 连续过渡开启时可含小数
    
    # 影响后续流程的参数
    top_n: int  # Screener 筛选数量
    position_multiplier: float  # 仓位倍率
    min_strength: int  # 最低信号强度要求
    vix_uncertain: bool = False  # RFC-060: VIX 取不到真实值时为 True, regime 判定不可信
    # [DEB-20260924 F5-B'影子] 盘前 spy_price 口径遥测 (record-only, 不影响主判定):
    #   spy_price 现为日K口径 (盘前=昨收, regime_log 4/4 逐日实证 + 9/21 距MA20 0.11% 翻转实例)。
    #   影子字段双算"若用盘前 pre_price 会怎样", 攒 5-10 个交易日后裁决是否 enforce。
    spy_price_basis: str = "kline_last_close"   # kline_last_close | pre_price(仅遥测标注, 主判定不变)
    spy_pre_price: float = 0.0                  # 盘前时段快照 pre_price (0=不可用)
    score_shadow: Optional[float] = None        # 用 pre_price 重算的 score (None=无双算/无差异)
    regime_shadow: Optional[str] = None         # score_shadow 对应的 regime 值

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime.value,
            "vix": self.vix,
            "spy_price": self.spy_price,
            "spy_ma20": self.spy_ma20,
            "spy_ma50": self.spy_ma50,
            "spy_ma200": self.spy_ma200,
            "spy_change_pct": self.spy_change_pct,
            "score": self.score,
            "top_n": self.top_n,
            "position_multiplier": self.position_multiplier,
            "min_strength": self.min_strength,
            "vix_uncertain": self.vix_uncertain,
            "spy_price_basis": self.spy_price_basis,
            "spy_pre_price": self.spy_pre_price,
            "score_shadow": self.score_shadow,
            "regime_shadow": self.regime_shadow,
        }


def shadow_score_delta(spy_price: float, spy_pre_price: float,
                       spy_ma20: float, spy_ma200: float) -> int:
    """[DEB-20260924 F5] 纯函数: 盘前价替换昨收后, score 中 SPY 相关两项 (±2/±1) 的变化量。

    与 assess() 主路径的组件 1 (SPY vs MA20, ±2) 和组件 3 (SPY vs MA200, ±1) 同式;
    MA20/MA50/MA200 保持日K口径不变 (均线是历史序列, 与"今天用什么价"无关)。
    独立成纯函数供单测 (9/21 实例: spy=761.69/ma20=762.50/pre=763.x → delta=+4, cautious→risk_on)。
    """
    if not (spy_price > 0 and spy_pre_price > 0):
        return 0
    delta = 0
    if spy_ma20 > 0:
        delta += (2 if spy_pre_price > spy_ma20 else -2) - (2 if spy_price > spy_ma20 else -2)
    if spy_ma200 > 0:
        delta += (1 if spy_pre_price > spy_ma200 else -1) - (1 if spy_price > spy_ma200 else -1)
    return delta


def vix_score(vix: float, *, smooth_20_22: bool = False) -> float:
    """VIX 分项；可选在 20–22 连续过渡，消除边界 3 分跳变。

    旧阶梯完整保留在开关关闭侧：15/20/30 阈值不变。开启侧仅把
    20<VIX<22 改为 +1 → -2 的线性插值，两端及区间外逐值等于旧规则。
    """
    if vix < 15:
        return 3
    if vix <= 20:
        return 1
    if smooth_20_22 and vix < 22:
        return 1.0 - 1.5 * (float(vix) - 20.0)
    if vix < 30:
        return -2
    return -5


def score_to_regime_value(score: float, vix_uncertain: bool = False) -> str:
    """[DEB-20260924 F5] 纯函数: score → regime 值 (镜像 assess() 主路径映射, 仅供影子遥测)。

    ⚠️ 阈值与主路径 (>=5 RISK_ON / >=0 CAUTIOUS / >=-5 RISK_OFF / else NO_TRADE) 及
    RFC-060 vix_uncertain 降级 (RISK_ON→CAUTIOUS) 保持一致; 主路径改阈值时此处必须同改
    (tests/test_market_regime_f5_shadow.py 钉住两者一致性)。
    """
    if score >= 5:
        r = "risk_on"
    elif score >= 0:
        r = "cautious"
    elif score >= -5:
        r = "risk_off"
    else:
        r = "no_trade"
    if vix_uncertain and r == "risk_on":
        r = "cautious"
    return r


class MarketRegimeGate:
    """市场环境门禁"""

    # 2026-07-31 全链清理: 类级 TTL 缓存 — 调用方 (auto_execute/sizing 兜底) 每次新建实例
    #   且逐条指令各 assess 一次, 无缓存时每个周期打 AlphaVantage 付费 API 多次.
    #   regime 基于 MA20/50/200+VIX, 分钟级新鲜度足够; uncertain 降级结果短 TTL 防兜底滞留.
    _assess_cache = None          # tuple(monotonic_ts, RegimeContext)
    _ASSESS_CACHE_TTL = 120.0     # 正常结果缓存 120s
    _ASSESS_CACHE_TTL_UNCERTAIN = 30.0  # VIX uncertain 降级结果仅 30s

    def __init__(
        self,
        market_data_router: Optional[MarketDataRouter] = None,
        alphavantage_provider: Optional[AlphaVantageProvider] = None,
    ):
        self.router = market_data_router
        self.av = alphavantage_provider
        
        # 如果没有传入，尝试创建默认实例
        if not self.router:
            try:
                # 2026-07-11: 复用进程级共享单例, 避免每次实例化都新建 Futu 连接+watchdog
                self.router = get_market_data_router()
            except Exception as e:
                logger.warning(f"Failed to create MarketDataRouter: {e}")
        
        if not self.av:
            try:
                # 2026-11-03 性能批: 与 router 一样走进程级单例 (原每个 regime 实例 new 一个,
                #   实测一个交易时段 137 次 AlphaVantage 初始化)。模块级 import 便于测试打桩。
                self.av = get_alpha_vantage_provider()
            except Exception as e:
                logger.warning(f"Failed to create AlphaVantageProvider: {e}")
    
    def assess(self) -> RegimeContext:
        """
        评估当前市场环境
        
        评分逻辑：
        - SPY > MA20: +2 (短期趋势向上)
        - SPY < MA20: -2 (短期趋势向下)
        - MA20 > MA50: +2 (中期趋势向上)
        - MA20 < MA50: -2 (中期趋势向下)
        - SPY > MA200: +1 (长期趋势向上)
        - SPY < MA200: -1 (长期趋势向下)
        - VIX < 15: +3 (低波动，市场平静)
        - VIX 15-20: +1 (正常波动)
        - VIX 20-30: -2 (高波动，市场紧张；RFC-216 开关开启时 20–22 连续 +1→-2)
        - VIX > 30: -5 (极高波动，市场恐慌)
        
        Regime 映射：
        - score >= 5: RISK_ON (top_n=10, multiplier=1.0)
        - score 0-4: CAUTIOUS (top_n=8, multiplier=0.5)  # 2026-07-21: 5→8 见 L251 注释
        - score -5 to -1: RISK_OFF (top_n=3, multiplier=0.3)
        - score < -5: NO_TRADE (top_n=0, multiplier=0.0)
        """
        score = 0

        # TTL 缓存命中: 直接返回 (省 AlphaVantage 付费调用 + VIX 失败时的 2s sleep)
        import time as _time_mod
        _now_mono = _time_mod.monotonic()
        _cached = MarketRegimeGate._assess_cache
        if _cached is not None:
            _c_ts, _c_ctx = _cached
            _ttl = self._ASSESS_CACHE_TTL_UNCERTAIN if _c_ctx.vix_uncertain else self._ASSESS_CACHE_TTL
            if (_now_mono - _c_ts) < _ttl:
                logger.debug(f"[Regime] 缓存命中 ({_now_mono - _c_ts:.0f}s < {_ttl:.0f}s): {_c_ctx.regime.value}")
                return _c_ctx
        spy_price = 0.0
        spy_ma20 = 0.0
        spy_ma50 = 0.0
        spy_ma200 = 0.0
        spy_change_pct = 0.0
        
        # 优先使用 MarketDataRouter (MA20/MA50/MA200 同源自 Futu 日K, 2026-07-03 起 Router 已返回)
        if self.router:
            try:
                spy_ctx = self.router.get_spy_context()
                if spy_ctx:
                    spy_price = spy_ctx.get("spy_price", 0.0)
                    spy_ma200 = spy_ctx.get("spy_ma200", 0.0)
                    spy_ma20 = spy_ctx.get("spy_ma20", 0.0)
                    spy_ma50 = spy_ctx.get("spy_ma50", 0.0)
                    spy_change_pct = spy_ctx.get("spx_change_percent", 0.0)
                    logger.info(f"[Regime] SPY from Router: price={spy_price:.2f}, MA200={spy_ma200:.2f}")
            except Exception as e:
                logger.warning(f"[Regime] Failed to get SPY from Router: {e}")
        
        # 补充 MA20/MA50 数据 (AV 仅作 fallback — 免费额度每日耗尽曾致 regime 静默降级)
        if self.av and (spy_ma20 == 0 or spy_ma50 == 0):
            try:
                av_spy_ctx = self.av.get_spy_context()
                if av_spy_ctx:
                    if spy_price == 0:
                        spy_price = av_spy_ctx.get("spy_price", 0.0)
                    spy_ma20 = av_spy_ctx.get("spy_ma20", 0.0)
                    spy_ma50 = av_spy_ctx.get("spy_ma50", 0.0)
                    if spy_change_pct == 0:
                        spy_change_pct = av_spy_ctx.get("spx_change_percent", 0.0)
                    logger.info(f"[Regime] SPY from AV: MA20={spy_ma20:.2f}, MA50={spy_ma50:.2f}")
            except Exception as e:
                logger.warning(f"[Regime] Failed to get SPY from AlphaVantage: {e}")
        
        # 获取 VIX
        # RFC-060 (2026-06-22): 区分真实值与兜底值.
        # 旧代码: vix = 20.0 默认值, router.get_vix() 失败回退 20.0 →
        #   盘前生成时若 FRED 尚未就绪, regime 用兜底 20.0 误判 CAUTIOUS
        #   (20.0 卡 NORMAL/CAUTIOUS 边界). 实测 06-22: regime 16:02:20 拿 20.0,
        #   真值 18.44 在 16:02:21 才到缓存.
        # 修复: 用 get_vix_or_none(), 拿不到真实值则重试一次 (等 FRED 缓存刷新),
        #   仍拿不到则标记 vix_uncertain, regime 判定降级保守.
        vix = None
        vix_uncertain = False
        if self.router:
            try:
                vix = self.router.get_vix_or_none()
                if vix is not None:
                    logger.info(f"[Regime] VIX from Router: {vix:.2f}")
            except Exception as e:
                logger.warning(f"[Regime] Failed to get VIX from Router: {e}")
        if vix is None and self.av:
            try:
                vix = self.av.get_vix_or_none()
                if vix is not None:
                    logger.info(f"[Regime] VIX from AlphaVantage: {vix:.2f}")
            except Exception as e:
                logger.warning(f"[Regime] Failed to get VIX from AlphaVantage: {e}")
        if vix is None:
            # 重试一次: 盘前 FRED 首次请求可能未就绪, 1-2s 后缓存刷新即可拿到
            import time as _time
            _time.sleep(2)
            if self.router:
                try:
                    vix = self.router.get_vix_or_none()
                except Exception:
                    pass
            if vix is None and self.av:
                try:
                    vix = self.av.get_vix_or_none()
                except Exception:
                    pass
        if vix is None:
            # 所有源失败: 用 20.0 兜底但标记不确定, 避免 regime 误判
            vix = 20.0
            vix_uncertain = True
            logger.warning("[Regime] VIX 所有源均失败, 使用兜底 20.0 (regime 判定不可信)")
        
        # 评分计算
        # 1. 短期趋势 (SPY vs MA20)
        if spy_price > 0 and spy_ma20 > 0:
            if spy_price > spy_ma20:
                score += 2
                logger.debug(f"[Regime] SPY > MA20: +2")
            else:
                score -= 2
                logger.debug(f"[Regime] SPY < MA20: -2")
        
        # 2. 中期趋势 (MA20 vs MA50)
        if spy_ma20 > 0 and spy_ma50 > 0:
            if spy_ma20 > spy_ma50:
                score += 2
                logger.debug(f"[Regime] MA20 > MA50: +2")
            else:
                score -= 2
                logger.debug(f"[Regime] MA20 < MA50: -2")
        
        # 3. 长期趋势 (SPY vs MA200)
        if spy_price > 0 and spy_ma200 > 0:
            if spy_price > spy_ma200:
                score += 1
                logger.debug(f"[Regime] SPY > MA200: +1")
            else:
                score -= 1
                logger.debug(f"[Regime] SPY < MA200: -1")
        
        # 4. 波动率 (VIX)
        # RFC-060: VIX 不确定时跳过 VIX 评分 — 兜底 20.0 会给 -2 误判 CAUTIOUS.
        # RFC-216: 可选 20–22 连续过渡，避免 VIXY 代理在 20 附近微小抖动造成
        #   +1 → -2 的 3 分跳变及 RISK_OFF → NO_TRADE 硬翻转。缺键/读取失败=旧规则。
        _vix_smooth_20_22 = False
        if not vix_uncertain:
            try:
                from app.playbook.config import get_playbook_config
                _vix_smooth_20_22 = bool(get_playbook_config().get(
                    "market_regime_vix_20_22_smoothing_enabled", False))
            except Exception as _vix_cfg_err:
                logger.warning(f"[Regime] VIX连续计分开关读取失败，沿用旧阶梯: {_vix_cfg_err}", exc_info=True)
        if vix_uncertain:
            logger.debug("[Regime] VIX uncertain, 跳过 VIX 评分")
        else:
            _vix_points = vix_score(vix, smooth_20_22=_vix_smooth_20_22)
            score += _vix_points
            logger.debug(
                f"[Regime] VIX分项={_vix_points:+.2f} "
                f"(smooth_20_22={_vix_smooth_20_22})")

        if _vix_smooth_20_22 and 20 < vix < 22:
            score = round(score, 4)

        # Regime 映射
        if score >= 5:
            regime = MarketRegime.RISK_ON
            top_n = 10
            position_multiplier = 1.0
            min_strength = 50
        elif score >= 0:
            regime = MarketRegime.CAUTIOUS
            # 2026-07-21 标的量辩论 P1: cautious top_n 5→8 — 低频日供给端保险.
            #   只动指令生成数量, 不动任何盘中质量门 (VWAP/量能/RR 已验证拦得对).
            #   近5日全 risk_on 故现状增量≈0, 仅 cautious 日生效. multiplier/min_strength 不变.
            #   回滚: 改回 5 (注意同步下方 VIX 不确定降级分支).
            top_n = 8
            position_multiplier = 0.5
            min_strength = 60
        elif score >= -5:
            regime = MarketRegime.RISK_OFF
            top_n = 3
            position_multiplier = 0.3
            min_strength = 70
        else:
            regime = MarketRegime.NO_TRADE
            top_n = 0
            position_multiplier = 0.0
            min_strength = 999

        # RFC-060: VIX 不确定时 regime 降一级保守 (RISK_ON→CAUTIOUS, CAUTIOUS→CAUTIOUS)
        # 理由: 缺 VIX 时无法确认波动环境, 不应给激进仓位.
        if vix_uncertain and regime == MarketRegime.RISK_ON:
            regime = MarketRegime.CAUTIOUS
            top_n = 8  # 2026-07-21 P1: 与上方 CAUTIOUS 分支同步 5→8
            position_multiplier = 0.5
            min_strength = 60
            logger.info("[Regime] VIX uncertain, RISK_ON 降级为 CAUTIOUS (保守)")

        logger.info(
            f"[Regime] Assessment: {regime.value} (score={score}) | "
            f"SPY={spy_price:.2f} MA20={spy_ma20:.2f} MA50={spy_ma50:.2f} MA200={spy_ma200:.2f} | "
            f"VIX={vix:.2f}{'(uncertain)' if vix_uncertain else ''} | top_n={top_n} multiplier={position_multiplier}"
        )

        # ── [DEB-20260924 F5-B'影子] 盘前 pre_price 双算遥测 (record-only, 主判定不变) ──
        # 病根: spy_price 盘前=昨收 (regime_log 4/4 逐日实证; 9/21 距MA20 0.11% → -2分 → cautious,
        #   而当日实际收涨 +1.5% — 边界日占比 33%, 见 DEB §三.9 证据3)。
        # 修法分两步: 本步只**记录**"若用盘前 pre_price, score/regime 会变成什么"(影子字段);
        #   enforce (spy_price 直接换 pre_price) 属行为变更, 须影子攒 5-10 交易日后另行裁决 (F5-B')。
        # 数据源: get_quote 快照路径已带 pre_price (8/25 修复); push/订阅路径无盘前字段 (置0),
        #   pre_price<=0 时影子字段保持 None/0 = "本轮不可双算", 不猜不补。
        spy_pre_price = 0.0
        score_shadow = None
        regime_shadow = None
        if self.router and spy_price > 0 and spy_ma20 > 0:
            try:
                _q = self.router.get_quote("US.SPY") or {}
                spy_pre_price = float(_q.get("pre_price") or 0.0)
            except Exception as _e_pre:
                # 2026-09-25 W4棘轮清剿 (RFC-107 §3D): debug→warning+栈 — 影子取价失败必须可见;
                #   频率=regime评估周期(非热循环), 主判定不受影响的语义保留在消息文本。
                logger.warning(f"[Regime] F5影子 pre_price 获取失败 (影子跳过, 主判定不受影响): {_e_pre}", exc_info=True)
        if spy_pre_price > 0:
            _delta = shadow_score_delta(spy_price, spy_pre_price, spy_ma20, spy_ma200)
            if _delta:
                score_shadow = score + _delta
                regime_shadow = score_to_regime_value(score_shadow, vix_uncertain)
                if regime_shadow != regime.value:
                    # 影子与主判定不同档 = F5 缺陷的真实暴露日, WARNING 级留痕 (可 grep 攒证据)
                    logger.warning(
                        f"[Regime][F5影子] 盘前价双算翻转 regime: {regime.value}(score={score}, "
                        f"spy_price=昨收{spy_price:.2f}) → {regime_shadow}(score={score_shadow}, "
                        f"pre_price={spy_pre_price:.2f}) | 主判定不变(record-only, enforce 待裁决)")

        ctx = RegimeContext(
            regime=regime,
            vix=vix,
            vix_uncertain=vix_uncertain,
            spy_price=spy_price,
            spy_ma20=spy_ma20,
            spy_ma50=spy_ma50,
            spy_ma200=spy_ma200,
            spy_change_pct=spy_change_pct,
            score=score,
            top_n=top_n,
            position_multiplier=position_multiplier,
            min_strength=min_strength,
            spy_pre_price=spy_pre_price,
            score_shadow=score_shadow,
            regime_shadow=regime_shadow,
        )
        MarketRegimeGate._assess_cache = (_now_mono, ctx)
        return ctx
