"""预交易风控检查器

优化 2026-03-27:
- 优化 2：添加市场状态过滤（SPY MA200 + VIX）
- 优化 3.3：提高信心阈值 0.50 → 0.65

P0 配置统一化修复 (2026-04-06):
- 从 config_us.py 的 RISK_CONFIG 读取 min_confidence，移除硬编码

2026-06-01 重大重构 (RFC-056/058 真实接线):
- 黑名单从硬编码改为读 unified_config.yaml::risk.global_blacklist (SSOT)
- 加入 VIX 多档过滤 (RFC-056): VIX>=high LONG×0.5, SHORT×0.85; VIX>=mid ×0.85
- 加入 LONG 方向折扣 (RFC-058): 默认 strength × 0.7, BULL regime 豁免
"""
import logging
import time
from dataclasses import dataclass
from typing import Optional, List, Set, Dict, Any
from app.core.models import SignalScore
from app.analysis.market_state import can_trade as market_can_trade
from app.risk.pdt_guard import get_pdt_guard

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────
# 2026-06-01: yaml 黑名单缓存 (60s TTL, 避免每次入场都读盘)
# ─────────────────────────────────────────────────────────────────────────
_BLACKLIST_CACHE: Set[str] = set()
_BLACKLIST_CACHE_TS: float = 0.0
_BLACKLIST_TTL: float = 60.0


def _load_blacklist() -> Set[str]:
    """读 unified_config.yaml::risk.global_blacklist + data/dynamic_blacklist.json (60s 缓存)"""
    global _BLACKLIST_CACHE, _BLACKLIST_CACHE_TS
    now = time.time()
    if now - _BLACKLIST_CACHE_TS < _BLACKLIST_TTL and _BLACKLIST_CACHE:
        return _BLACKLIST_CACHE
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        bl = cfg.get_dotted("risk.global_blacklist", []) or []
        _BLACKLIST_CACHE = {str(s).strip() for s in bl if s}
    except Exception as e:
        logger.warning(f"[BlackList] yaml 读取失败 (用空集合保守放行): {e}")
        _BLACKLIST_CACHE = set()
    # 改动5b: 合并 deep_analysis 动态黑名单 (falling knife 等)
    try:
        import json as _json, os
        _dyn_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "dynamic_blacklist.json")
        if os.path.exists(_dyn_path):
            with open(_dyn_path) as _f:
                _dyn = _json.load(_f)
            for _sym in _dyn.get("symbols", []):
                _BLACKLIST_CACHE.add(str(_sym).strip())
            if _dyn.get("symbols"):
                logger.info(f"[BlackList] 动态黑名单合并: {_dyn['symbols']}")
    except Exception as _dyn_err:
        logger.debug(f"[BlackList] dynamic_blacklist.json 读取失败(忽略): {_dyn_err}")
    _BLACKLIST_CACHE_TS = now
    return _BLACKLIST_CACHE


# ─────────────────────────────────────────────────────────────────────────
# RFC-056/058 配置缓存
# ─────────────────────────────────────────────────────────────────────────
_FILTER_CFG_CACHE: Dict[str, Any] = {}
_FILTER_CFG_TS: float = 0.0


def _load_filter_config() -> Dict[str, Any]:
    """读 unified_config.yaml 中 RFC-056/058 配置 (60s 缓存)"""
    global _FILTER_CFG_CACHE, _FILTER_CFG_TS
    now = time.time()
    if now - _FILTER_CFG_TS < 60 and _FILTER_CFG_CACHE:
        return _FILTER_CFG_CACHE
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        risk = cfg.get_section("risk", {}) or {}
        _FILTER_CFG_CACHE = {
            "vix_levels": (risk.get("market_regime_filter", {}) or {}).get("vix_levels", {}),
            "vix_long_high_mult": (risk.get("market_regime_filter", {}) or {}).get("long_in_high_zone_strength_mult", 0.5),
            "vix_short_high_mult": (risk.get("market_regime_filter", {}) or {}).get("short_in_high_zone_strength_mult", 0.85),
            "long_direction_filter": risk.get("long_direction_filter", {}) or {},
        }
        _FILTER_CFG_TS = now
    except Exception as e:
        logger.warning(f"[FilterCfg] yaml 读取失败: {e}")
        _FILTER_CFG_CACHE = {"vix_levels": {}, "long_direction_filter": {}}
    return _FILTER_CFG_CACHE

@dataclass
class PreTradeResult:
    allowed: bool
    reason: Optional[str]
    checks_passed: List[str]


class PreTradeChecker:
    """预交易风控检查器"""

    def __init__(
        self,
        min_confidence: Optional[float] = None,  # P0 修复：从配置读取，默认 None
        min_signal_strength: int = 50,       # P0-辩论修复: 99→50 (strength已缩放到0-100, 50=3指标共振)
    ):
        # F4 (2026-05-14): 收敛到 MinConfidenceResolver, 不再各自直读 yaml
        if min_confidence is None:
            try:
                from app.core.min_confidence_resolver import get_min_confidence_resolver
                self.min_confidence = get_min_confidence_resolver().for_phase("intraday")
            except Exception:
                from app.core.config_defaults import CFG
                self.min_confidence = CFG.MIN_CONFIDENCE
        else:
            self.min_confidence = min_confidence

        self.min_strength = min_signal_strength

    def _get_effective_thresholds(self) -> tuple:
        """P0 修复 (2026-05-04): DEGRADED 模式下降级置信度/信号分阈值。

        Returns:
            (effective_min_confidence, effective_min_strength)
        """
        # F4 (2026-05-14): 每次现查 resolver, 让 ConfigWriter 调参立即生效
        # 沙盒模式 (2026-05-14): overrides 优先
        try:
            from app.core.config_container import get_config as _gc
            _cfg = _gc()
            _sandbox = bool(_cfg.get_dotted("mode.sandbox.enabled", False))
            _overrides = (_cfg.get_dotted("mode.sandbox.overrides", {}) or {}) if _sandbox else {}
        except Exception:
            _sandbox = False
            _overrides = {}

        try:
            if _sandbox and "risk.signal_quality.min_confidence" in _overrides:
                eff_conf = float(_overrides["risk.signal_quality.min_confidence"])
            else:
                from app.core.min_confidence_resolver import get_min_confidence_resolver
                eff_conf = get_min_confidence_resolver().for_phase("intraday")
        except Exception:
            eff_conf = self.min_confidence

        # 沙盒 strength override
        if _sandbox and "strategies.defaults.min_strength" in _overrides:
            eff_strength = int(_overrides["strategies.defaults.min_strength"])
        else:
            eff_strength = self.min_strength
        try:
            from app.us_pipeline.battle_plan_manager import USBattlePlanManager
            plan = USBattlePlanManager().get_plan()
            if plan.get("market_regime") == "DEGRADED":
                from app.core.config_defaults import CFG
                eff_conf = max(CFG.MIN_CONFIDENCE, eff_conf - 0.10)
                eff_strength = max(40, self.min_strength - 10)
        except Exception:
            # 同批清剿: DEGRADED 收紧折扣读不到 = 门槛静默留在常态档 (弱市里反而更宽松),
            # 也是 fail-open。不留痕则"今天门槛为何没收紧"事后无从追查。
            logger.warning(
                "[PreTradeCheck·thresholds] battle_plan market_regime 读取失败 → DEGRADED "
                "收紧折扣未应用, 本次按常态门槛判定 (fail-open)", exc_info=True)
        return eff_conf, eff_strength
    
    def check(self, signal: SignalScore, lesson_injector=None) -> PreTradeResult:
        """执行预交易检查

        信号强度说明（0-100，LLM 评分）：
          LONG  方向：strength > 50 代表看多，effective = strength
          SHORT 方向：strength < 50 代表看空，effective = 100 - strength
        因此 SHORT 方向 strength=21 → effective=79，是强烈看空，应放行。
        min_strength 默认 50 → 要求 effective ≥ 50，即偏离中性程度足够。
        
        检查顺序:
        1. 市场状态检查（优化 2：SPY MA200 + VIX）
        2. 信号强度检查
        3. 置信度检查（优化 3.3：阈值 0.65）
        4. 信号未过期
        5. [P0-fix 2026-05-04] 教训约束检查 (仅降级+警告, 不拦截)
        """
        checks_passed = []
        
        # 0. 市场状态检查 (P1 优化 2026-05-24: 方向感知 Regime Gate)
        # P0 (2026-06-04): VIX>=25 不再硬拦截，改为警告标记。
        # 分层防御: IntradayScorer VIX缩仓(0.8x/0.5x) + ATR止损放宽 + capital_safety总仓上限。
        _p1_vix_ok = True
        _p1_spy_ok = True
        try:
            from app.analysis.market_state import MarketStateChecker as _MSC
            _msc = _MSC()
            _p1_vix_ok = _msc.vix_below_25()
            _p1_spy_ok = _msc.spy_above_ma200()
        except Exception:
            pass  # 数据不可用时放行，避免误杀
        
        if not _p1_vix_ok:
            logger.warning(f"[P1-VIX] VIX>=25 高波动模式，不拦截，下游自适应缩仓")
        checks_passed.append("P1_VIX")
        
        _direction = str(getattr(signal, 'direction', '')).upper()
        _is_long = "LONG" in _direction and "SHORT" not in _direction
        if _is_long and not _p1_spy_ok:
            # P1 Regime Gate: SPY<MA200 仅拦截做多
            # 配置开关: risk.p1_regime_gate.spy_long_block
            _p1_enabled = True
            try:
                from app.core.config_container import get_config as _gc2
                _p1_enabled = _gc2().get_dotted("risk.p1_regime_gate.enabled", True)
            except Exception:
                pass
            if _p1_enabled:
                return PreTradeResult(
                    allowed=False,
                    reason="P1_SPY_BEAR(SPY<MA200 blocks LONG only)",
                    checks_passed=checks_passed,
                )
        checks_passed.append("P1_SPY")

        # 0.05. P1-1 HIGH_VOL 守卫 (2026-05-23, 辩论发现)
        # HIGH_VOL regime: 141笔 14.9%WR -$39,537 — 必须收紧
        # 使用 VIX 作为 HIGH_VOL 代理: VIX 20-25 = caution zone
        try:
            from app.analysis.market_state import MarketStateChecker
            _msc = MarketStateChecker()
            _vix_data = _msc._fetch_vix()
            _vix = _vix_data[0] if isinstance(_vix_data, tuple) else (_vix_data or 20)
            if isinstance(_vix, (int, float)) and 20 <= _vix < 25:
                # Caution zone: 提高信号/置信度门槛
                self._high_vol_active = True
                self._high_vol_vix = _vix
                logger.info(f"[P1-1] HIGH_VOL 警戒: VIX={_vix:.1f} (20-25区间), 收紧入场门槛")
            else:
                self._high_vol_active = False
        except Exception:
            self._high_vol_active = False

        # 0.1. P0 辩论止血守卫 (2026-05-23, 基于309笔实盘数据)
        # P0-1: 禁止期权交易 (0%WR, -$23,635)
        _symbol = getattr(signal, 'symbol', '')
        _direction = str(getattr(signal, 'direction', '')).upper()
        if "OPTION" in _direction:
            return PreTradeResult(
                allowed=False,
                reason=f"P0_OPTION_BANNED(direction={_direction})",
                checks_passed=checks_passed,
            )
        # P0-1b: 期权风格symbol检测 (如 US.SNDK260605P1370000)
        _sym_clean = _symbol.replace("US.", "")
        if len(_sym_clean) > 6 and any(c.isdigit() for c in _sym_clean[1:]):
            return PreTradeResult(
                allowed=False,
                reason=f"P0_OPTION_BANNED(symbol={_symbol})",
                checks_passed=checks_passed,
            )
        # P0-3: 持续亏损股票黑名单 (数据驱动)
        # 2026-06-01 重构: 删除硬编码, 改为读 unified_config.yaml::risk.global_blacklist (SSOT)
        _blacklist = _load_blacklist()
        if _symbol in _blacklist:
            return PreTradeResult(
                allowed=False,
                reason=f"P0_BLACKLISTED({_symbol})",
                checks_passed=checks_passed,
            )
        checks_passed.append("P0_GUARD")

        # ── RFC-056 (2026-06-01): VIX 多档过滤 (真实接线版, 替代 market_regime_filter.evaluate) ──
        # 数据驱动 (n=141): HIGH_VOL regime 14.9% wr -$39,537
        # 注意: VIX>=25 在 step 0 P1 gate 已改为警告(不拦截), 这里处理分级削弱
        # P0 (2026-06-17): 不修改 signal.strength (消除副作用), 改用 metadata 传递调整值
        _rfc_strength = signal.strength  # 本地工作副本, 不修改原始信号
        try:
            _flt_cfg = _load_filter_config()
            _vix_levels = _flt_cfg.get("vix_levels", {}) or {}
            if _vix_levels:
                _cur_vix = None
                try:
                    from app.analysis.market_state import MarketStateChecker as _MSC2
                    _msc2 = _MSC2()
                    _vd = _msc2._fetch_vix()
                    _cur_vix = _vd[0] if isinstance(_vd, tuple) else _vd
                except Exception:
                    pass
                if isinstance(_cur_vix, (int, float)) and _cur_vix > 0:
                    _vix_critical = float(_vix_levels.get("critical", 28))
                    _vix_high = float(_vix_levels.get("high", 25))
                    _vix_mid = float(_vix_levels.get("mid", 22))
                    _is_long = "LONG" in _direction and "SHORT" not in _direction
                    if _cur_vix >= _vix_critical:
                        _mult = 0.3
                        _new_strength = int(_rfc_strength * _mult)
                        logger.warning(
                            f"[RFC056·VIX-critical] {_symbol} VIX={_cur_vix:.1f}>={_vix_critical} "
                            f"strength {_rfc_strength}×{_mult}={_new_strength} (不拦截, 重度削弱)"
                        )
                        _rfc_strength = _new_strength
                    elif _cur_vix >= _vix_high:
                        _mult = float(_flt_cfg.get("vix_long_high_mult", 0.5)) if _is_long \
                                else float(_flt_cfg.get("vix_short_high_mult", 0.85))
                        _new_strength = int(_rfc_strength * _mult)
                        logger.info(
                            f"[RFC056·VIX-high] {_symbol} VIX={_cur_vix:.1f} {_direction} "
                            f"strength {_rfc_strength}×{_mult}={_new_strength}"
                        )
                        _rfc_strength = _new_strength
                    elif _cur_vix >= _vix_mid:
                        _new_strength = int(_rfc_strength * 0.85)
                        logger.info(
                            f"[RFC056·VIX-mid] {_symbol} VIX={_cur_vix:.1f} "
                            f"strength {_rfc_strength}×0.85={_new_strength}"
                        )
                        _rfc_strength = _new_strength
        except Exception as _e_rfc056:
            logger.debug(f"[RFC056] 异常 (规则降级): {_e_rfc056}")
            try:
                from app.core.degradation_monitor import get_degradation_monitor
                get_degradation_monitor().record("rfc056_vix_filter", str(_e_rfc056))
            except Exception:
                pass
        checks_passed.append("RFC056_VIX")

        # ── RFC-058 (2026-06-01): LONG 方向默认折扣 30% (BULL 豁免) ──
        # 数据驱动 (n=134): LONG 20.9% wr +$2,644 vs SHORT 33.9% +$13,016
        #
        # Phase 1 (2026-06-19): 动态折扣 — 根据 regime 调整
        # 原固定 0.7 折扣导致 LONG 信号过少 (39% WR vs SHORT 53% WR)
        # 改进: BULL/STRONG_BULL 不惩罚, CAUTION 15%, BEAR 30%
        try:
            _ldf = _flt_cfg.get("long_direction_filter", {}) if "_flt_cfg" in dir() else {}
            _is_long = "LONG" in _direction and "SHORT" not in _direction
            if _is_long and _ldf.get("enabled", False):
                _use_dynamic = _ldf.get("use_dynamic", False)
                
                if _use_dynamic:
                    # Phase 1: 动态折扣
                    _dynamic_discounts = _ldf.get("dynamic_discounts", {}) or {}
                    
                    # 获取当前 regime (优先从信号 metadata, 否则从 evaluate_regime 计算)
                    _current_regime = None
                    if hasattr(signal, "metadata") and signal.metadata:
                        _current_regime = signal.metadata.get("regime")
                    
                    if not _current_regime:
                        # 从 VIX + SPY 推导 regime
                        try:
                            if "_cur_vix" in dir() and isinstance(_cur_vix, (int, float)):
                                if _cur_vix >= 35:
                                    _current_regime = "CRISIS"
                                elif _cur_vix >= 25:
                                    _current_regime = "BEAR"
                                elif _cur_vix >= 20:
                                    _current_regime = "CAUTION"
                                else:
                                    # VIX < 20, 检查 SPY
                                    if "_p1_spy_ok" in dir() and _p1_spy_ok:
                                        _current_regime = "BULL" if _cur_vix >= 15 else "STRONG_BULL"
                                    else:
                                        _current_regime = "CAUTION"
                        except Exception:
                            pass
                    
                    # 应用动态折扣
                    if _current_regime and _current_regime in _dynamic_discounts:
                        _discount = float(_dynamic_discounts[_current_regime])
                        if _discount < 1.0:
                            _new_strength = int(_rfc_strength * _discount)
                            if _ldf.get("log_every_discount", True):
                                logger.info(
                                    f"[RFC058·LONG折扣·动态] {_symbol} regime={_current_regime}, "
                                    f"strength {_rfc_strength}×{_discount:.2f}={_new_strength}"
                                )
                            _rfc_strength = _new_strength
                        # discount >= 1.0 不打折
                    else:
                        # regime 无法判断, 使用默认折扣
                        _discount = float(_ldf.get("default_discount", 0.7))
                        _new_strength = int(_rfc_strength * _discount)
                        if _ldf.get("log_every_discount", True):
                            logger.info(
                                f"[RFC058·LONG折扣·默认] {_symbol} regime=UNKNOWN, "
                                f"strength {_rfc_strength}×{_discount}={_new_strength}"
                            )
                        _rfc_strength = _new_strength
                else:
                    # 原固定折扣模式 (向后兼容)
                    _discount = float(_ldf.get("default_discount", 0.7))
                    _exempt = _ldf.get("bull_regime_exempt", {}) or {}
                    _vix_max = float(_exempt.get("vix_max", 20))
                    _bull_exempt = isinstance(_cur_vix, (int, float)) and _cur_vix < _vix_max if "_cur_vix" in dir() else False
                    if not _bull_exempt:
                        _new_strength = int(_rfc_strength * _discount)
                        if _ldf.get("log_every_discount", True):
                            logger.info(
                                f"[RFC058·LONG折扣] {_symbol} BULL 豁免不满足 (vix={_cur_vix if '_cur_vix' in dir() else 'N/A'}), "
                                f"strength {_rfc_strength}×{_discount}={_new_strength}"
                            )
                        _rfc_strength = _new_strength
        except Exception as _e_rfc058:
            logger.debug(f"[RFC058] 异常 (规则降级): {_e_rfc058}")
            try:
                from app.core.degradation_monitor import get_degradation_monitor
                get_degradation_monitor().record("rfc058_long_filter", str(_e_rfc058))
            except Exception:
                pass
        checks_passed.append("RFC058_LONG")

        # P0 (2026-06-17): 将 RFC 调整后的 strength 存入 metadata (供下游读取, 不污染原始信号)
        if _rfc_strength != signal.strength:
            if signal.metadata is None:
                signal.metadata = {}
            signal.metadata["rfc_adjusted_strength"] = _rfc_strength
            signal.metadata["rfc_original_strength"] = signal.strength

        # 削减后强度归零 → 拒绝
        if _rfc_strength <= 0:
            return PreTradeResult(
                allowed=False,
                reason=f"STRENGTH_ZERO_AFTER_FILTER(symbol={_symbol})",
                checks_passed=checks_passed,
            )

        # 0.5. PDT 保护检查 (P0 2026-04-21 新增)
        # 默认所有信号为日内交易，除非 metadata 中明确标记 force_overnight=True
        is_overnight = signal.metadata.get("force_overnight", False)
        pdt_guard = get_pdt_guard()
        # 同步资金状态
        pdt_guard.update_equity(signal.metadata.get("account_equity", 0))
        allowed, pdt_reason, risk_level = pdt_guard.check_trade_permission(is_overnight=is_overnight)
        if not allowed:
            return PreTradeResult(allowed=False, reason=f"PDT_BLOCKED: {pdt_reason}", checks_passed=checks_passed)
        checks_passed.append("PDT_GUARD")
        
        # 0.5. 获取当前有效的阈值 (DEGRADED 模式下降级)
        eff_conf, eff_strength = self._get_effective_thresholds()

        # P1-1 (2026-05-23): HIGH_VOL 收紧门槛
        # 数据: HIGH_VOL regime 14.9%WR -$39,537, 必须提高入场标准
        if getattr(self, '_high_vol_active', False):
            _hv_conf_boost = 0.10   # 置信度 +10% (e.g. 0.50 → 0.60)
            _hv_strength_boost = 10  # 信号强度 +10 (e.g. 99 → 109, 但上限100)
            eff_conf = min(eff_conf + _hv_conf_boost, 0.90)
            eff_strength = min(eff_strength + _hv_strength_boost, 100)
            checks_passed.append("HIGH_VOL_TIGHTENED")

        # 1. 信号强度检查
        # P0-辩论修复 (2026-05-26): strength 统一为 direct 语义
        # 越高 = 信号越强 (LONG/SHORT 均同), 无需反转
        # P0 (2026-06-17): 使用 RFC 调整后的 strength (不修改原始信号)
        direction_str = str(signal.direction).upper()
        is_short = "SHORT" in direction_str
        effective_strength = _rfc_strength  # 使用 RFC-056/058 调整后的值
        if effective_strength < eff_strength:
            return PreTradeResult(
                allowed=False,
                reason=f"SIGNAL_TOO_WEAK(dir={direction_str} rfc_strength={_rfc_strength}<{eff_strength})",
                checks_passed=checks_passed,
            )
        checks_passed.append("SIGNAL_STRENGTH")

        # 1.5. P1 VWAP Gate (2026-05-24: 辩论共识 — 低强度多头信号需动量确认)
        # 数据支撑: 强度66的89笔仅11.2%胜率, 缺乏技术确认的信号是噪音
        # 规则: LONG + strength<80 + 价格<VWAP → 拦截
        # 数据源: signal.metadata["vwap_deviation"] (>0 = 价格在VWAP上方)
        # 软门禁: VWAP数据不可用时放行 (不阻断交易)
        _p1_vwap_enabled = True
        try:
            from app.core.config_container import get_config as _gc3
            _p1_vwap_enabled = _gc3().get_dotted("risk.p1_regime_gate.vwap_filter_enabled", True)
        except Exception:
            pass
        if _p1_vwap_enabled and _is_long and _rfc_strength < 80:
            _vwap_dev = None
            try:
                _meta = getattr(signal, 'metadata', None) or {}
                _vwap_dev = _meta.get("vwap_deviation")
            except Exception:
                pass
            if _vwap_dev is not None:
                _vwap_threshold = -0.001  # 价格低于VWAP 0.1% 视为弱势
                if float(_vwap_dev) < _vwap_threshold:
                    return PreTradeResult(
                        allowed=False,
                        reason=f"P1_VWAP_WEAK(rfc_strength={_rfc_strength}<80, vwap_dev={_vwap_dev:.4f}<{_vwap_threshold})",
                        checks_passed=checks_passed,
                    )
                checks_passed.append("P1_VWAP")
            # VWAP 数据不可用 → 软放行，记录日志
            # 后续由 signal_pipeline 负责注入 VWAP 数据

        # 2. 置信度检查（优化 3.3：阈值 0.65）
        if signal.confidence < eff_conf:
            return PreTradeResult(
                allowed=False,
                reason="LOW_CONFIDENCE",
                checks_passed=checks_passed,
            )
        checks_passed.append("CONFIDENCE")
        
        # 2.5. [P0-fix 2026-05-04] 教训约束检查 — 仅降级不拦截
        # 辩证结论: 当前阶段教训置信度不足以支撑硬拦截，
        # 改为: 提高信心门槛 + 降低仓位 + 推送警告，让用户决策。
        if lesson_injector is not None:
            try:
                direction_str = str(signal.direction).upper()
                is_short = "SHORT" in direction_str
                session = "open"  # 默认盘中，可由 signal.metadata 覆盖
                constraints = lesson_injector.generate_trade_constraints(
                    symbol=signal.symbol, session=session
                )
                # 教训收紧: 提高信心门槛 (仅记录 + 推送, 不拦截)
                if constraints.get('extra_confidence', 0) > 0:
                    adjusted_min = eff_conf + constraints['extra_confidence']
                    if signal.confidence < adjusted_min:
                        logger.warning(
                            f"[教训约束] {signal.symbol} 信心不足 (需 {adjusted_min:.2f} vs 实际 {signal.confidence:.2f}): "
                            f"{constraints['reason']} — 已降级为警告模式, 不拦截"
                        )
                        # 不拦截，但标记为受教训约束
                        checks_passed.append("LESSON_CONFIDENCE_WARN")
                # 教训收紧: 仓位限制 (记录并传递到 PositionSizer)
                if constraints.get('max_position_pct', 1.0) < 1.0:
                    logger.warning(
                        f"[教训约束] {signal.symbol} 仓位收紧至 {constraints['max_position_pct']:.0%}: "
                        f"{constraints['reason']}"
                    )
                    checks_passed.append("LESSON_CONSTRAINTS")
            except Exception as le:
                logger.warning(f"[教训约束] 检查失败 (放行): {le}")
        
        # 3. 信号未过期
        if signal.is_expired():
            return PreTradeResult(
                allowed=False,
                reason="SIGNAL_EXPIRED",
                checks_passed=checks_passed,
            )
        checks_passed.append("NOT_EXPIRED")

        # 4. F10 (2026-05-14): 单笔交易实际风险预算检查
        # 防御 _compute_position_and_stop 的逃逸路径 (yaml 漂移 / 第三方写入 / 测试桩)
        budget_check = self._check_trade_risk_within_budget(signal)
        if not budget_check[0]:
            return PreTradeResult(
                allowed=False,
                reason=budget_check[1],
                checks_passed=checks_passed,
            )
        checks_passed.append("RISK_BUDGET")

        # 4.5 P0-X (2026-05-15, 基于 14 日 LCID 8 笔 -$634 教训): 标的级日亏损护栏
        try:
            from app.risk.symbol_daily_loss_guard import get_symbol_daily_loss_guard
            blocked, reason = get_symbol_daily_loss_guard().is_blocked(signal.symbol)
            if blocked:
                return PreTradeResult(
                    allowed=False,
                    reason=f"SYMBOL_DAILY_LOSS_GUARD: {reason}",
                    checks_passed=checks_passed,
                )
            checks_passed.append("SYMBOL_DAILY_LOSS")
        except Exception as e:
            logger.debug(f"[PreTradeCheck·daily_loss] 异常 (放行): {e}")

        # 5. P0-R1 (2026-05-14): 行业集中度检查 — 防 chip 板块扎堆
        sector_check = self._check_sector_limit(signal)
        if not sector_check[0]:
            return PreTradeResult(
                allowed=False,
                reason=sector_check[1],
                checks_passed=checks_passed,
            )
        checks_passed.append("SECTOR_LIMIT")

        # 6. P0-R2 (2026-05-14): 系统级熔断 (VIX / SPY 闪跌 / API 故障率)
        sys_check = self._check_system_breaker()
        if not sys_check[0]:
            return PreTradeResult(
                allowed=False,
                reason=sys_check[1],
                checks_passed=checks_passed,
            )
        checks_passed.append("SYSTEM_BREAKER")

        # 所有检查通过
        return PreTradeResult(
            allowed=True,
            reason=None,
            checks_passed=checks_passed,
        )

    def _check_sector_limit(self, signal: SignalScore) -> tuple:
        """P0-R1: 板块集中度检查"""
        try:
            from app.risk.sector_limits import SectorLimitsChecker
            from app.risk.capital_safety import get_capital_safety_system
            from app.core.config_container import get_config

            # 取 equity + 当前持仓
            equity = float(get_capital_safety_system()._state.current_equity or 0)
            if equity <= 0:
                return True, None  # 无 equity 数据 → 跳过 (不阻断)

            cfg = get_config()
            max_sector = float(cfg.get_dotted("risk.position.max_per_sector", 0.40))
            checker = SectorLimitsChecker(max_sector_weight=max_sector)

            # 估算新仓位市值: position_pct * equity
            pos_pct = float(getattr(signal, "position_pct", 0) or 0)
            new_value = pos_pct * equity
            if new_value <= 0:
                return True, None

            # 取当前持仓列表 (从 PositionStore, 通过 signal.metadata 注入)
            positions_list = (signal.metadata or {}).get("_current_positions", [])

            allowed, reason = checker.check_sector_limit(
                symbol=signal.symbol,
                new_position_value=new_value,
                total_portfolio_value=equity,
                current_positions=positions_list,
            )
            if not allowed:
                return False, f"SECTOR_CAP: {reason}"
            return True, None
        except Exception as e:
            logger.debug(f"[PreTradeCheck·sector] 检查失败 (放行): {e}")
            return True, None

    def _check_system_breaker(self) -> tuple:
        """P0-R2: 系统级熔断 — 数据源健康度 + 市场闪跌"""
        try:
            # 1) 市场闪跌熔断 (MarketCircuitBreaker)
            try:
                from app.risk.market_circuit_breaker import is_trading_suspended
                if is_trading_suspended():
                    return False, "SYSTEM_BREAKER: market_suspended"
            except Exception:
                # 2026-09-10 (DEB-20260910-logging-observability §10.7, except:pass 清剿):
                # 本维异常 = **放行** (fail-open)。静默 pass 会让"熔断未触发"与"熔断检查
                # 根本没跑成"在读数上完全一致 → 值班人把失明读成安全态。留痕 + 栈。
                logger.warning(
                    "[PreTradeCheck·system_breaker] 市场闪跌熔断检查自身异常 → 本维放行 "
                    "(fail-open, 不等于「未熔断」); 若持续出现说明 market_circuit_breaker "
                    "不可用, P0-R2 保护实际失效", exc_info=True)
            # 2) 数据源熔断 — 通过 signal.metadata 注入的 system_breaker_state
            #    (scheduler 在 monitor_loop 喂 VIX/数据源状态后填充)
            try:
                breaker_state = (signal_metadata := getattr(self, "_last_breaker_state", None))
                if breaker_state == "TRIGGERED":
                    return False, "SYSTEM_BREAKER: data_source_triggered"
            except Exception:
                pass
            return True, None
        except Exception as e:
            logger.debug(f"[PreTradeCheck·system_breaker] 检查失败 (放行): {e}")
            return True, None

    def update_breaker_state(self, state: str):
        """供 monitor_loop 推送 SystemCircuitBreaker 状态. state ∈ {NORMAL, DEGRADED, TRIGGERED}"""
        self._last_breaker_state = state

    def _check_trade_risk_within_budget(self, signal: SignalScore) -> tuple:
        """
        F10 (2026-05-14): 验证 position_pct × stop_loss_pct ≤ max_trade_risk_pct × buffer

        即使 IntradayScorer 算错了仓位/止损 (或上游绕过它), 这里也兜底拦截。
        Returns: (allowed, reason)
        """
        try:
            from app.core.config_container import get_config
            cfg = get_config()
            max_risk = float(cfg.get_dotted("risk.max_trade_risk_pct", 0.010))
            # 加 20% buffer 容许小数舍入和滑点
            hard_cap = max_risk * 1.20
        except Exception:
            hard_cap = 0.012  # 1.2% 的硬上限兜底

        position_pct = float(getattr(signal, "position_pct", 0.0) or 0.0)
        stop_loss_pct = float(getattr(signal, "stop_loss_pct", 0.0) or 0.0)
        if position_pct <= 0 or stop_loss_pct <= 0:
            # 数据不全, 不在这里拦截 (let downstream sizer 决定)
            return True, None
        actual_risk = position_pct * stop_loss_pct
        if actual_risk > hard_cap:
            return False, (
                f"TRADE_RISK_EXCEEDS_BUDGET("
                f"pos={position_pct:.3f} × stop={stop_loss_pct:.3f} = {actual_risk:.4f} > {hard_cap:.4f})"
            )
        return True, None
