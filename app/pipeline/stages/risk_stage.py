"""
Risk Stage — 仓位计算 + OrderRequest 构建

职责:
  1. 仓位计算 (PositionSizer — 资金水位 + tier + 风险参数)
  2. 止损/止盈计算 (ATR-based)
  3. 构建 OrderRequest (已审批, 可直接提交执行引擎)

注意: 风控门禁 (PreTradeChecker/TradeGuard/VIX/做空门禁等) 统一由
_submit_order._check_gates_and_conflicts 执行，本阶段不再重复。

输入: SignalCandidate
输出: OrderRequest 或 None (仓位计算为 0)
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any

from app.core.pipeline_contracts import SignalCandidate, OrderRequest

logger = logging.getLogger(__name__)


def _get_futu_provider():
    """获取 FutuLV3Provider 单例 (顽强，失败返回 None)"""
    try:
        from app.data.futu_lv3_provider import get_futu_lv3_provider
        return get_futu_lv3_provider()
    except Exception as e:
        logger.debug(f"[Risk] 获取 Futu provider 失败: {e}")
        return None


def _check_liquidity(symbol: str) -> Optional[Dict[str, Any]]:
    """获取盘口流动性评估 (辅助决策)"""
    try:
        provider = _get_futu_provider()
        if provider and hasattr(provider, 'get_order_book_quality'):
            return provider.get_order_book_quality(symbol)
    except Exception as e:
        logger.debug(f"[LIQUIDITY] {symbol} 检查失败: {e}")
    return None


def _get_ticker_sentiment(symbol: str) -> Optional[Dict[str, Any]]:
    """获取成交流向情绪 (辅助决策)"""
    try:
        provider = _get_futu_provider()
        if provider and hasattr(provider, 'get_ticker_sentiment'):
            return provider.get_ticker_sentiment(symbol)
    except Exception as e:
        logger.debug(f"[TICKER] {symbol} 流向查询失败: {e}")
    return None


def _check_global_blacklist(symbol: str) -> bool:
    """检查标的是否在全局黑名单中 (P0 2026-05-30)

    从 unified_config.yaml 的 risk.global_blacklist 读取黑名单，
    支持 US.XXXX 与 XXXX 两种格式匹配。
    命中则丢弃信号，跨策略统一拦截。
    """
    try:
        from app.core.config_container import get_config
        blacklist = get_config().get_dotted("risk.global_blacklist", []) or []
    except Exception as e:
        logger.debug(f"[BLACKLIST] 读取全局黑名单失败: {e}")
        return False

    if not blacklist:
        return False

    # 标准化 symbol 格式（支持 US.XXXX 和 XXXX 两种）
    clean_symbol = symbol.replace("US.", "")
    for bl_symbol in blacklist:
        if clean_symbol == str(bl_symbol).replace("US.", ""):
            logger.warning(f"[BLACKLIST] {symbol} 被全局黑名单拦截，禁止入场")
            return True
    return False


def process(candidate: SignalCandidate,
            risk_engine=None,
            position_store=None,
            execution_engine=None,
            polling_controller=None) -> Optional[OrderRequest]:
    """
    对信号候选执行风控审批 + 仓位计算。

    Args:
        candidate: SignalCandidate (来自 SignalStage)
        risk_engine: RiskEngine 实例 (含 PreTradeChecker)
        position_store: 持仓管理器 (仓位限制检查需要)
        execution_engine: 执行引擎 (下单时需要 broker_adapter)
        polling_controller: 自适应轮询控制器

    Returns:
        OrderRequest 或 None
    """
    symbol = candidate.symbol

    # ── 0. 全局黑名单拦截 (P0 2026-05-30) ──
    # 性能优先: 在任何仓位/止损计算之前过滤亏损标的
    if _check_global_blacklist(symbol):
        return None

    # ── 0.01 Restricted 列表拦截 (Legacy 独有逻辑, 迁移自 _process_symbol_impl L1352-1365) ──
    # 来自作战计划 plan.json 的 restricted 字段，人审级别的禁入名单
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        _bp_mgr = USBattlePlanManager()
        if not _bp_mgr._plan:
            _bp_mgr.load(_bp_mgr.plan_path)
        _plan = _bp_mgr.get_plan()
        _restricted = _plan.get("restricted", []) if _plan else []
        if symbol in _restricted:
            logger.warning(f"🚫 [Restricted] {symbol} 在限制列表中，拒绝交易")
            return None
    except Exception as e:
        logger.debug(f"[Restricted] 检查异常 (放行): {e}")

    # ── 0.05 Trend-First 持仓上限 (Task #19) ──
    # observe_only=false 后需控制其独立的并发持仓数, 避免资金超限
    try:
        strategy_id = str(getattr(candidate, 'strategy_id', '') or '')
        if strategy_id == 'trend_first' and position_store is not None:
            from app.core.config_container import get_config
            tf_cfg = get_config().get_dotted('strategies.trend_first') or {}
            max_pos = int(tf_cfg.get('max_positions', 3))
            current_tf = 0
            for pos in (position_store.all() if hasattr(position_store, 'all') else []):
                owner = str(getattr(pos, 'owner_strategy', '') or getattr(pos, 'strategy_id', '') or '')
                if owner == 'trend_first':
                    current_tf += 1
            if current_tf >= max_pos:
                logger.warning(
                    f"[RiskStage] {symbol} trend_first 持仓已达上限 "
                    f"({current_tf}/{max_pos}), 拒绝入场"
                )
                return None
        # ── 0.06 ORB 持仓上限 (统一计数: orb_pipeline + orb_engine) ──
        if strategy_id == 'orb' and position_store is not None:
            from app.core.config_container import get_config
            orb_cfg = get_config().get_dotted('strategies.orb') or {}
            max_pos = int(orb_cfg.get('max_positions', 3))
            current_orb = 0
            for pos in (position_store.all() if hasattr(position_store, 'all') else []):
                owner = str(getattr(pos, 'owner_strategy', '') or getattr(pos, 'strategy_id', '') or '')
                # 2026-06-01: 统一计数 orb_pipeline + orb_engine + 旧 "orb" 标签
                if owner in ('orb', 'orb_pipeline', 'orb_engine'):
                    current_orb += 1
            if current_orb >= max_pos:
                logger.warning(
                    f"[RiskStage] {symbol} ORB 持仓已达上限 "
                    f"({current_orb}/{max_pos}, 含pipeline+engine), 拒绝入场"
                )
                return None
    except Exception as e:
        logger.debug(f"[RiskStage] trend_first 持仓限制检查异常(放行): {e}")

    # ── 0.07 全局持仓总数检查 ──
    # 防止 ORB(max=3) + TrendFirst(max=3) + 其他策略 总和超过全局上限
    if position_store is not None:
        try:
            from app.core.config_container import get_config
            _raw_max = get_config().get_dotted('strategies.defaults.max_positions', None) if get_config() else None
            global_max = int(_raw_max) if _raw_max is not None else 5
            all_positions = position_store.all() if hasattr(position_store, 'all') else []
            total_count = len(list(all_positions))
            if total_count >= global_max:
                logger.warning(
                    f"[RiskStage] {symbol} 全局持仓已达上限 "
                    f"({total_count}/{global_max}), 拒绝入场"
                )
                return None
        except Exception as e:
            logger.debug(f"[RiskStage] 全局持仓检查异常: {e}")

    # ── 0.1 流动性检查 (P1 2026-05-30) ──
    # 辅助决策: spread 过大拒绝入场, spread 偏大仅降权
    liquidity = _check_liquidity(symbol)
    if liquidity and not liquidity.get('tradeable', True):
        logger.warning(
            f"[LIQUIDITY] {symbol} 流动性不足(spread="
            f"{liquidity.get('spread_pct', 0):.3f}%)，拒绝入场"
        )
        return None

    # 流动性降权: spread > 0.2% 时置信度 ×0.85
    if liquidity and liquidity.get('spread_pct', 0) > 0.2:
        old_conf = float(getattr(candidate, 'confidence', 0.0) or 0.0)
        new_conf = round(min(1.0, old_conf * 0.85), 4)
        candidate.confidence = new_conf
        if isinstance(candidate.metadata, dict):
            candidate.metadata['liquidity_penalty'] = 0.85
            candidate.metadata['liquidity_spread_pct'] = liquidity.get('spread_pct', 0)
        logger.info(
            f"[LIQUIDITY] {symbol} spread={liquidity.get('spread_pct', 0):.3f}%，"
            f"置信度降权×0.85 ({old_conf:.3f} → {new_conf:.3f})"
        )

    # ── 0.2 成交流向确认 (P1 2026-05-30) ──
    # 辅助决策: 信号方向与成交流向一致 → 加权, 反之降权
    ticker_data = _get_ticker_sentiment(symbol)
    if ticker_data and ticker_data.get('confidence', 0) > 0.5:
        sentiment = float(ticker_data.get('sentiment', 0) or 0)
        raw_dir = candidate.direction
        direction = (raw_dir.value if hasattr(raw_dir, 'value') else str(raw_dir)).upper()
        is_long = direction in ('LONG', 'BUY')
        is_short = direction in ('SHORT', 'SELL')

        old_conf = float(getattr(candidate, 'confidence', 0.0) or 0.0)
        new_conf = old_conf
        ticker_factor = None

        if is_long and sentiment < -0.3:
            ticker_factor = 0.85
            new_conf = round(old_conf * ticker_factor, 4)
            logger.info(
                f"[TICKER] {symbol} {direction} 信号但成交流向偏空"
                f"(sentiment={sentiment:.2f})，降权×0.85"
            )
        elif is_short and sentiment > 0.3:
            ticker_factor = 0.85
            new_conf = round(old_conf * ticker_factor, 4)
            logger.info(
                f"[TICKER] {symbol} {direction} 信号但成交流向偏多"
                f"(sentiment={sentiment:.2f})，降权×0.85"
            )
        elif (is_long and sentiment > 0.3) or (is_short and sentiment < -0.3):
            ticker_factor = 1.08
            new_conf = round(min(1.0, old_conf * ticker_factor), 4)
            logger.info(
                f"[TICKER] {symbol} {direction} 信号与成交流向一致"
                f"(sentiment={sentiment:.2f})，增强×1.08"
            )

        if ticker_factor is not None:
            candidate.confidence = new_conf
            if isinstance(candidate.metadata, dict):
                candidate.metadata['ticker_factor'] = ticker_factor
                candidate.metadata['ticker_sentiment'] = sentiment

    # ── 1. 风控门禁 ──
    # 风控门禁统一由 _submit_order._check_gates_and_conflicts 执行，避免重复。
    # _check_gates_and_conflicts 是完整门禁集合 (RiskStateMachine/ADR/VIX/做空门禁/
    # TradeGuard/动量门控/作战计划)，pre_trade_check 仅是其子集。
    # 此处不再调用 risk_engine.pre_trade_check()，防止每单 3 次重复检查。

    # ── 3. 仓位计算 ──
    quantity = _compute_position_size(symbol, candidate, risk_engine)
    if quantity <= 0:
        logger.warning(f"[Risk] {symbol} 仓位计算为 0, 跳过")
        return None

    # ── 4. 止损/止盈计算 ──
    ctx = candidate.context
    atr = ctx.atr if ctx else 0
    price = ctx.price if ctx else 0

    # 方向判断
    _dir_raw = candidate.direction
    _dir_str = (_dir_raw.value if hasattr(_dir_raw, 'value') else str(_dir_raw)).upper()
    _is_short = _dir_str in ('SHORT', 'SELL')

    # 优先: 作战计划固定 SL/TP (fast_lane)
    plan_sl = None
    plan_tp = None
    if isinstance(candidate.metadata, dict):
        _psl = candidate.metadata.get('plan_stop_loss')
        _ptp = candidate.metadata.get('plan_take_profit')
        if _psl and float(_psl) > 0:
            plan_sl = float(_psl)
        if _ptp and float(_ptp) > 0:
            plan_tp = float(_ptp)

    # SL/TP defaults from config (dynamic_exit at top-level, NOT under risk.)
    # 2026-06-21: 修复路径 — get_dotted('risk.dynamic_exit...') 永远命中 fallback,
    #   因 dynamic_exit 在 yaml top-level 而非 risk 子树. 去掉 'risk.' 前缀.
    #   同时对齐 exit_policy.yaml: max_loss_pct=2.0% (was -0.045)
    try:
        from app.core.config_container import get_config as _get_risk_cfg
        _risk_cfg = _get_risk_cfg()
        _sl_pct = float(_risk_cfg.get_dotted('dynamic_exit.default.stop_loss', -0.02) or -0.02)
        _tp_pct = float(_risk_cfg.get_dotted('dynamic_exit.default.take_profit', 0.05) or 0.05)
    except Exception:
        _sl_pct, _tp_pct = -0.02, 0.05
    stop_loss = price * (1 + _sl_pct)
    take_profit_1 = price * (1 + _tp_pct)

    if plan_sl:
        # 作战计划指定了固定止损/止盈
        stop_loss = plan_sl
        take_profit_1 = plan_tp or (price * 1.06)
        logger.debug(f"[Risk] {symbol} 使用作战计划 SL={plan_sl} TP={plan_tp}")
    elif isinstance(candidate.metadata, dict) and candidate.metadata.get('stop_loss') and str(candidate.strategy_id) == 'orb':
        # ORB 策略: 使用区间中点 + ATR兜底 计算的精确止损
        orb_sl = float(candidate.metadata['stop_loss'])
        stop_loss = orb_sl
        # TP 由 trailing stop 管理, 这里设一个保守的固定目标 (2x SL distance)
        sl_dist = abs(price - orb_sl)
        if _is_short:
            take_profit_1 = price - sl_dist * 2.5
        else:
            take_profit_1 = price + sl_dist * 2.5
        logger.info(f"[Risk] {symbol} ORB止损: SL={orb_sl:.2f} (区间中点+ATR) TP={take_profit_1:.2f}")
    elif atr > 0 and price > 0:
        # ATR 动态止损
        atr_mult = 1.5
        if isinstance(candidate.metadata, dict):
            _sl_mult = candidate.metadata.get('stop_loss_atr_mult')
            if _sl_mult and float(_sl_mult) > 0:
                atr_mult = float(_sl_mult)

        if _is_short:
            stop_loss = price + (atr * atr_mult)
            take_profit_1 = price - (atr * atr_mult * 2)
        else:
            stop_loss = price - (atr * atr_mult)
            take_profit_1 = price + (atr * atr_mult * 2)

    # ATR观察日志: 入场决策关键参数 (2026-05-30)
    sl_distance = abs(price - stop_loss) if price > 0 else 0
    confidence = candidate.metadata.get("confidence", 0.0) if candidate.metadata else 0.0
    direction = candidate.direction.value if hasattr(candidate.direction, 'value') else str(candidate.direction)
    logger.info(
        f"[ATR_ENTRY] symbol={symbol} direction={direction} "
        f"ATR={atr:.4f} SL_distance={sl_distance:.4f} "
        f"position_size={quantity} confidence={confidence:.3f}"
    )

    return OrderRequest(
        candidate=candidate,
        quantity=quantity,
        order_type="LIMIT",
        stop_loss=round(stop_loss, 2),
        take_profit_levels=[
            {"ratio": 0.50, "price": round(take_profit_1, 2)},
            {"ratio": 0.50, "price": round(take_profit_1 * 1.03, 2)},
        ],
        risk_approved=True,
        risk_checks_passed=[],
        risk_notes=candidate.metadata.get("risk_notes", []),
    )


def _compute_position_size(symbol: str, candidate: SignalCandidate, risk_engine) -> int:
    """计算目标仓位 (股数)"""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        capital = get_capital_safety_system()
        equity = capital._state.current_equity if capital._state and capital._state.current_equity > 0 else 1_000_000.0

        # 获取 tier
        tier = "inst"
        try:
            from app.core.config_container import get_config
            tiers = get_config().get_dotted("capital.tiers") or {}
            for name in ["micro", "small", "medium", "large", "inst"]:
                t = tiers.get(name, {})
                min_e = t.get("min_equity", 0)
                max_e = t.get("max_equity", float("inf")) or float("inf")
                if min_e <= equity <= max_e:
                    tier = name
                    break
        except Exception:
            pass

        # 获取水位
        water_level = "green"
        try:
            if risk_engine and hasattr(risk_engine, 'capital_safety'):
                level = risk_engine.capital_safety.get_water_level()
                if level:
                    water_level = level
        except Exception:
            pass

        # 单笔仓位占比 (默认 5%)
        position_pct = 0.05
        risk_mult = 1.0
        if water_level == "yellow":
            risk_mult = 0.5
            position_pct = 0.025
        elif water_level in ("orange", "red"):
            risk_mult = 0.0
            return 0

        max_order_amount = equity * position_pct
        price = candidate.context.price if candidate.context else 0
        if price <= 0:
            return 0

        raw_qty = int(max_order_amount / price)
        # 取整到 10 股
        qty = max(10, (raw_qty // 10) * 10)

        # ── P0 (2026-06-02): 仓位硬上限 — 防止异常大单 ──
        # 案例: RIVN qty=3250 ($54K) 因 ATR 过低导致止损距离极小，反算出巨大仓位
        # 硬上限: 单笔不超过 $20,000 或 500 股 (取较小值)
        max_position_value = 20_000.0
        max_shares = 500
        capped = False

        # 价值上限
        if qty * price > max_position_value:
            qty = int(max_position_value / price)
            qty = max(10, (qty // 10) * 10)  # 重新取整
            capped = True

        # 股数上限
        if qty > max_shares:
            qty = max_shares
            capped = True

        if capped:
            logger.warning(
                f"[Risk] {symbol} 仓位触发硬上限: {qty}股 (${qty*price:,.0f}) | "
                f"limits: ${max_position_value:,.0f} / {max_shares}股"
            )

        logger.info(f"[Risk] {symbol} 仓位={qty}股 (${qty*price:,.0f}) | "
                    f"equity=${equity:,.0f} | tier={tier} | wl={water_level} | pct={position_pct:.1%}")

        return qty
    except Exception as e:
        logger.error(f"[Risk] {symbol} 仓位计算异常: {e}")
        return 0
