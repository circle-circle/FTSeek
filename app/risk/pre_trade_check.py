"""预交易风控检查器

优化 2026-03-27:
- 优化 2：添加市场状态过滤（SPY MA200 + VIX）
- 优化 3.3：提高信心阈值 0.50 → 0.65

P0 配置统一化修复 (2026-04-06):
- 从 config_us.py 的 RISK_CONFIG 读取 min_confidence，移除硬编码
"""
import logging
from dataclasses import dataclass
from typing import Optional, List
from app.core.models import SignalScore
from app.analysis.market_state import can_trade as market_can_trade
from app.risk.pdt_guard import get_pdt_guard

logger = logging.getLogger(__name__)

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
        min_signal_strength: int = 50,       # P0-辩论修复: 99→50 (P3过度收紧导致零交易, P0守卫兜底)
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
            pass
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
        # 旧逻辑: market_can_trade() 一刀切拦截所有方向 (LONG+SHORT)
        # 新逻辑: VIX>=25 拦截所有方向; SPY<MA200 仅拦截 LONG (允许 SHORT)
        # 数据支撑: SHORT 胜率35% 盈利$12,021 vs LONG 胜率20.3% 盈利$1,933
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
            return PreTradeResult(
                allowed=False,
                reason="P1_VIX_HIGH(VIX>=25 blocks all directions)",
                checks_passed=checks_passed,
            )
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
        _P0_BLACKLIST = {
            "US.QCOM", "US.TME", "US.NVDA", "US.META",
            "US.GOOGL", "US.QQQ", "US.UNH", "US.NFLX", "US.DIA",
        }
        if _symbol in _P0_BLACKLIST:
            return PreTradeResult(
                allowed=False,
                reason=f"P0_BLACKLISTED({_symbol})",
                checks_passed=checks_passed,
            )
        checks_passed.append("P0_GUARD")

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

        # 1. 信号强度检查（SHORT 方向使用 effective_strength = 100 - strength）
        # P0-辩论修复 (2026-05-26): 方向感知门槛
        # LONG: strength >= 50 = 看多信号 (strength越高越看多)
        # SHORT: effective = 100-strength >= 50, 即 strength <= 50 = 看空信号
        # 数据: SHORT strength=-1(eff=101) 43.6%WR +$12K | strength=66(eff=34) 15.8%WR
        direction_str = str(signal.direction).upper()
        is_short = "SHORT" in direction_str
        effective_strength = (100 - signal.strength) if is_short else signal.strength
        # SHORT方向使用更宽松的门槛 (看空信号本身就稀缺且高价值)
        _dir_threshold = eff_strength - 10 if is_short else eff_strength
        _dir_threshold = max(_dir_threshold, 30)  # 绝对下限30
        if effective_strength < _dir_threshold:
            return PreTradeResult(
                allowed=False,
                reason=f"SIGNAL_TOO_WEAK(dir={direction_str} strength={signal.strength} effective={effective_strength}<{_dir_threshold})",
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
        if _p1_vwap_enabled and _is_long and signal.strength < 80:
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
                        reason=f"P1_VWAP_WEAK(strength={signal.strength}<80, vwap_dev={_vwap_dev:.4f}<{_vwap_threshold})",
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
            equity = float(get_capital_safety_system()._state.total_equity or 0)
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
                pass
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
