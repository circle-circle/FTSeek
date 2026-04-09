"""
共识触发逻辑模块
负责处理共识信号触发后的交易决策和执行
"""
import dataclasses
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from app import config
from app.strategy.base import Signal, SignalAction
from app.strategy.consensus_engine import ConsensusResult
from app.storage.signal_tracker import signal_tracker
from app.risk.circuit_breaker import CircuitBreaker
from app.analytics.trade_evaluation import get_evaluator

logger = logging.getLogger("ftseek.data.consensus_trigger")


class ConsensusTrigger:
    """共识触发器 - 处理共识信号的交易决策和执行"""
    
    # 加仓触发条件（全部满足才执行）
    _ADD_SCORE_MIN = 1.5          # 最低共识得分
    _ADD_CONFIDENCE_MIN = 0.70    # 最低置信度
    _ADD_PRICE_MIN_PCT = 0.005    # 价格须高于持仓均价至少 0.5%（顺势）
    _ADD_HOLD_MIN_MINUTES = 15    # 持仓时间须 >= 15 分钟
    _ADD_MAX_TIMES_PER_DAY = 1    # 每日每品种最多加仓 1 次
    _ADD_QTY_RATIO = 0.5          # 加仓量 = 初始信号量 × 50%
    
    def __init__(self, position_manager, trade_executor, notifier, consensus_engine,
                 adaptive_weight_manager, regime_detector, circuit_breaker,
                 hk_adapter, params_loader, hsi_fetcher):
        """
        初始化共识触发器
        
        Args:
            position_manager: 持仓管理器
            trade_executor: 交易执行器
            notifier: 通知器
            consensus_engine: 共识引擎
            adaptive_weight_manager: 自适应权重管理器
            regime_detector: 市场状态检测器
            circuit_breaker: 熔断器
            hk_adapter: 港股适配器
            params_loader: 参数加载器
            hsi_fetcher: 恒生指数数据获取器
        """
        self.position_manager = position_manager
        self.trade_executor = trade_executor
        self.notifier = notifier
        self.consensus_engine = consensus_engine
        self.adaptive_weight_manager = adaptive_weight_manager
        self.regime_detector = regime_detector
        self.circuit_breaker = circuit_breaker
        self.hk_adapter = hk_adapter
        self.params_loader = params_loader
        self.hsi_fetcher = hsi_fetcher
        
        # 追踪止盈缓存
        self._trailing_stop_cache = {}
        self._trailing_exit_time = {}
        
        # code 级别交易锁
        self._trade_locks = {}
        
        # 开仓原子锁
        self._open_position_ts = {}
        self._open_lock = threading.Lock()
        
        # 持仓期间峰值价格缓存
        self._peak_price_cache = {}
        
        # 止损冷却
        self._stop_loss_cooldown = {}
        self._stop_loss_fail_count = {}
    
    def on_consensus(self, result: ConsensusResult) -> None:
        """
        推送共识信号 (入口方法，带锁保护)
        
        Args:
            result: 共识结果
        """
        code = result.code
        
        # code 级别非阻塞锁：防止同一标的并发执行
        lock = self._trade_locks.setdefault(code, threading.Lock())
        if not lock.acquire(blocking=False):
            logger.debug(f"[幂等] {code} 上一笔交易仍在执行，跳过本次共识回调")
            return
        try:
            self._on_consensus_inner(result)
        finally:
            lock.release()
    
    def _on_consensus_inner(self, result: ConsensusResult) -> None:
        """
        _on_consensus 实际逻辑（已由外层持有 code 锁）
        
        Args:
            result: 共识结果
        """
        code = result.code
        
        # 港股交易时间检查 + 开收盘噪音时段过滤
        if not self._is_high_quality_trading_hours():
            if not self._is_trading_hours():
                logger.debug(f"[共识] {code} 非交易时间，跳过推送")
            else:
                logger.info(f"[共识] {code} 开/收盘噪音时段（信号质量差），跳过推送")
            return
        
        # 熔断检查
        if self.circuit_breaker.is_paused():
            logger.warning(f"[共识] {code} 熔断器已触发，暂停交易")
            return
        
        # ADX-趋势一致性防守检查
        if result.action in (SignalAction.BUY, SignalAction.SELL):
            _adx_val = getattr(result, 'adx_value', 0) or 0
            _regime_str = getattr(result, 'market_regime', '') or ''
            _regime_unclear = _regime_str in ('range_bound', 'high_volatility', 'neutral', '')
            if _adx_val >= 50 and _regime_unclear:
                logger.warning(
                    f"[ADX 趋势约束] {code} ADX={_adx_val:.1f} 极高但市场状态={_regime_str!r}，"
                    f"趋势方向不明，拒绝开仓 ({result.action.value if hasattr(result.action,'value') else result.action})"
                )
                return
        
        # 多周期趋势过滤
        action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
        mtf_confirmed, mtf_boost, mtf_reason = self._get_multi_timeframe_signal(code, action_str)
        
        # 同步 MTF 对齐分数至共识引擎缓存
        try:
            buy_score, _  = self._get_mtf_alignment_score(code, "BUY")
            sell_score, _ = self._get_mtf_alignment_score(code, "SELL")
            self.consensus_engine.update_mtf_alignment(code, buy_score, sell_score)
        except Exception as _mtf_e:
            logger.debug(f"[MTF 缓存同步] {code} 失败：{_mtf_e}")
        
        if not mtf_confirmed:
            logger.info(f"[多周期过滤] {code} {action_str} 被拒绝：{mtf_reason}")
            return
        
        if mtf_boost != 0:
            logger.info(f"[多周期调整] {code} 置信度调整 {mtf_boost:+.1%}: {mtf_reason}")
        
        # 追踪止盈冷却期检查
        _trailing_cooldown_reject = False
        if result.action in (SignalAction.BUY, SignalAction.SELL):
            trailing_exit_ts = self._trailing_exit_time.get(code, 0)
            elapsed = time.time() - trailing_exit_ts
            TRAILING_COOLDOWN_SEC = 15 * 60
            if elapsed < TRAILING_COOLDOWN_SEC:
                remain_min = (TRAILING_COOLDOWN_SEC - elapsed) / 60
                logger.info(
                    f"[追踪冷却] {code} 追踪止盈后冷却中，剩余 {remain_min:.1f}min，跳过开仓"
                )
                _trailing_cooldown_reject = True
        
        # 弱信号过滤
        _weak_signal_reject = False
        if result.action in (SignalAction.BUY, SignalAction.SELL):
            score_abs = abs(result.score) if hasattr(result, 'score') else 999
            if score_abs < 1.2:
                logger.info(
                    f"[弱信号过滤] {code} 共识分数={result.score:.2f} < 1.2，跳过开仓 "
                    f"(同向策略：{' + '.join(result.agree_strategies)})"
                )
                _weak_signal_reject = True
        
        # 跨标的相关性控制
        _concentration_discount = 1.0
        if result.action in (SignalAction.BUY, SignalAction.SELL):
            try:
                open_positions = self.position_manager.get_all_positions()
                open_codes = {p.code for p in open_positions if p.code != code}
                if len(open_codes) >= 2:
                    logger.info(
                        f"[相关性控制] {code} 当前已有 {len(open_codes)} 个其他标的持仓 "
                        f"({', '.join(sorted(open_codes))})，注入分散折扣 0.7x"
                    )
                    _concentration_discount = 0.7
            except Exception as _e:
                logger.debug(f"[相关性控制] 检查异常：{_e}")
        
        # 盈亏比预检
        _rr_reject_reason = None
        try:
            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
            stop_pct = abs(_dem.current_params.get('stop_loss', -0.03))
            take_pct = abs(_dem.current_params.get('take_profit', 0.045))
            if stop_pct > 0:
                rr_ratio = take_pct / stop_pct
                min_rr = 1.2
                if rr_ratio < min_rr:
                    logger.info(
                        f"[盈亏比] {code} R:R={rr_ratio:.2f}:1 < {min_rr}:1，跳过信号 "
                        f"(止盈={take_pct:.1%} / 止损={stop_pct:.1%})"
                    )
                    _rr_reject_reason = f'R:R={rr_ratio:.2f}:1 < {min_rr}:1'
        except Exception as e:
            logger.debug(f"[盈亏比] 检查失败，跳过：{e}")
        
        # 记录信号
        base_signal = result.votes[0].signal
        action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
        signal_id = signal_tracker.record_signal(
            code=code,
            strategy=f"共识 ({'+'.join(result.agree_strategies)})",
            action=action_str,
            price=result.price,
            quantity=base_signal.quantity,
            confidence=result.confidence,
            consensus_score=result.score,
            strategies=result.agree_strategies,
            market_regime=result.market_regime,
            adx=result.adx_value,
        )
        logger.debug(f"[{code}] 信号记录：{signal_id[:8]}... | 置信度={result.confidence:.2f}")
        
        # 盈亏比拒绝处理
        if _rr_reject_reason:
            signal_tracker.record_execution(signal_id, executed=False, reject_reason=_rr_reject_reason)
            return
        
        # 追踪冷却 / 弱信号拒绝
        if _trailing_cooldown_reject:
            signal_tracker.record_execution(signal_id, executed=False, reject_reason='trailing_cooldown')
            return
        if _weak_signal_reject:
            signal_tracker.record_execution(signal_id, executed=False, reject_reason='weak_signal')
            return
        
        # 冷却检查
        if not self._is_cooled_down(code):
            logger.info(
                f"[共识] {code} 冷却中，跳过推送 "
                f"(分数={result.score:+.2f}, {', '.join(result.agree_strategies)})"
            )
            signal_tracker.record_execution(signal_id, executed=False, reject_reason='cooldown')
            return
        
        # 构造共识信号
        strategies_str = " + ".join(result.agree_strategies)
        consensus_reason = "\n".join(
            f"  ▸ {v.strategy}(权重{result.weight_details.get(v.strategy, '?')}): {v.reason}"
            for v in result.votes
        )
        
        consensus_signal = Signal(
            code=code,
            name=base_signal.name,
            action=result.action,
            price=result.price,
            quantity=base_signal.quantity,
            reason=consensus_reason,
            strategy=f"共识 ({strategies_str})",
            extra={
                "consensus": True,
                "score": result.score,
                "threshold": result.threshold,
                "market_regime": result.market_regime,
                "adx": result.adx_value,
                "agree_count": len(result.agree_strategies),
                "total_count": len(result.agree_strategies) + len(result.oppose_strategies),
                "strategies": result.agree_strategies,
                "oppose": result.oppose_strategies,
                "weights": result.weight_details,
                "confidence": round(result.confidence, 2),
            },
        )
        
        # Watchlist 过滤
        import json
        try:
            watchlist_path = "/opt/ftseek/FTSeek/config/watchlist.json"
            with open(watchlist_path, 'r') as f:
                watchlist = json.load(f)
            enabled_codes = set()
            for stock in watchlist.get('hk_stocks', []):
                if stock.get('enabled', False):
                    enabled_codes.add(stock['code'])
            for stock in watchlist.get('us_stocks', []):
                if stock.get('enabled', False):
                    enabled_codes.add(stock['code'])
            
            if code not in enabled_codes:
                logger.debug(f"[跳过推送] {code} 不在启用列表中")
                return
        except Exception as e:
            logger.warning(f"watchlist 过滤异常：{e}")
        
        # 港股特性适配
        if code.startswith("HK."):
            atr_pct = self._get_atr_percent(code)
            consensus_signal = self.hk_adapter.process_signal(consensus_signal, atr_pct)
            if consensus_signal is None:
                logger.info(f"[港股适配] {code} 信号被过滤 (T+0 冲突/置信度不足/流动性限制)")
                return
        
        # 判断交易类型
        position = self.position_manager.get_position(code)
        trade_type = self._determine_trade_type(position, result.action)
        
        # 已有同向持仓时，尝试顺势加仓
        if trade_type == 'HOLD':
            added = self._try_add_position(code, position, result, consensus_signal, signal_id)
            if not added:
                action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
                logger.info(
                    f"[持仓保护] {code} 已有 {position.action} 持仓，"
                    f"不满足加仓条件，拒绝信号 {action_str} (分数={result.score:+.2f})"
                )
                signal_tracker.record_execution(signal_id, executed=False, reject_reason='existing_position')
            return
        
        # 最小持仓时间保护
        MIN_HOLD_MINUTES = 30
        if trade_type == 'CLOSE' and position is not None:
            try:
                entry_time_str = getattr(position, 'entry_time', None)
                if entry_time_str:
                    if isinstance(entry_time_str, str):
                        et = datetime.fromisoformat(entry_time_str)
                    else:
                        et = entry_time_str
                    hold_minutes = ((datetime.now() - et).total_seconds()) / 60
                    if hold_minutes < MIN_HOLD_MINUTES:
                        pnl_rate = (result.price - position.entry_price) / position.entry_price if position.entry_price > 0 else 0
                        try:
                            from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
                            stop_pct = abs(_dem.get_stop_loss())
                        except Exception:
                            stop_pct = 0.03
                        if pnl_rate > -stop_pct:
                            logger.info(
                                f"[最小持仓保护] {code} 持仓仅 {hold_minutes:.1f}min < {MIN_HOLD_MINUTES}min，"
                                f"拒绝反向信号平仓 (pnl={pnl_rate:.2%}, 止损线=-{stop_pct:.0%})"
                            )
                            return
            except Exception as _e:
                logger.debug(f"[最小持仓保护] 检查异常：{_e}")
        
        # 仓位调节
        news_boost = self._get_news_sentiment_boost(code, result.action)
        combined_boost = news_boost * _concentration_discount
        if combined_boost != 1.0:
            logger.info(
                f"[仓位调节] {code} 新闻={news_boost:.1f}x 分散={_concentration_discount:.1f}x "
                f"合并={combined_boost:.2f}x 注入 extra"
            )
            consensus_signal = dataclasses.replace(
                consensus_signal,
                extra={**(consensus_signal.extra or {}), 'news_boost': combined_boost}
            )
        
        # 开仓原子锁
        if trade_type == 'OPEN':
            _open_cooldown_sec = 5.0
            with self._open_lock:
                _last_open_ts = self._open_position_ts.get(code, 0)
                _now_ts = time.time()
                if _now_ts - _last_open_ts < _open_cooldown_sec:
                    logger.warning(
                        f"[开仓去重] {code} {_open_cooldown_sec:.0f}s 内已执行过开仓 "
                        f"(距上次 {_now_ts - _last_open_ts:.1f}s)，拒绝重复信号"
                    )
                    signal_tracker.record_execution(signal_id, executed=False, reject_reason='open_dedup')
                    return
                self._open_position_ts[code] = _now_ts
        
        # 执行交易
        trade_result = self.trade_executor.execute(consensus_signal, trade_type)
        
        if not trade_result.success:
            logger.warning(f"交易失败：{trade_result.message}")
            signal_tracker.record_execution(signal_id, executed=False, reject_reason=trade_result.message)
            self.circuit_breaker.record_reject(trade_result.message)
            return
        
        # 准备推送数据
        push_data = {
            'signal': consensus_signal,
            'trade_result': trade_result,
            'position': position,
            'consensus_result': result
        }
        
        # 记录执行成功
        signal_tracker.record_execution(
            signal_id, 
            executed=True, 
            execution_price=consensus_signal.price,
            execution_time=datetime.now().isoformat()
        )
        
        # 记录交易评估
        try:
            evaluator = get_evaluator()
            if trade_type == 'OPEN':
                evaluator.record_trade({
                    'trade_id': signal_id,
                    'code': code,
                    'action': result.action.value if hasattr(result.action, 'value') else str(result.action),
                    'entry_time': datetime.now().isoformat(),
                    'entry_price': consensus_signal.price,
                    'quantity': consensus_signal.quantity,
                    'trigger_strategy': strategies_str,
                    'trigger_reason': f"共识分数={result.score:+.2f}",
                    'confidence': result.confidence,
                    'consensus_score': result.score,
                    'market_regime': result.market_regime
                })
            else:
                if position:
                    evaluator.record_exit(
                        trade_id=signal_id,
                        exit_price=consensus_signal.price,
                        exit_time=datetime.now().isoformat(),
                        realized_pnl=trade_result.pnl or 0
                    )
        except Exception as e:
            logger.warning(f"记录交易评估失败：{e}")
        
        # 开仓成功后写入动态止损止盈价
        if trade_type == 'OPEN':
            try:
                dyn = self._get_dynamic_stop_params(code)
                entry_px = consensus_signal.price
                is_long = (result.action.value if hasattr(result.action, 'value') else str(result.action)) == 'BUY'
                sl_price = round(entry_px * (1 + dyn["stop_loss_pct"]), 4)
                tp_price = round(entry_px * (1 + dyn["take_profit_pct"]), 4)
                if not is_long:
                    sl_price = round(entry_px * (1 - dyn["stop_loss_pct"]), 4)
                    tp_price = round(entry_px * (1 - dyn["take_profit_pct"]), 4)
                self.position_manager.update_stop_loss_take_profit(code, sl_price, tp_price)
                logger.info(
                    f"[动态止损] {code} 开仓@{entry_px} | "
                    f"止损={sl_price}({dyn['stop_loss_pct']:.1%}) | "
                    f"止盈={tp_price}({dyn['take_profit_pct']:.1%}) | "
                    f"regime={dyn['regime']}"
                )
            except Exception as _dyn_e:
                logger.warning(f"[动态止损] {code} 写入失败：{_dyn_e}")
        
        # 共识平仓回填 pnl
        if trade_type == 'CLOSE' and trade_result.success and trade_result.pnl is not None:
            try:
                signal_tracker.record_exit(
                    signal_id=signal_id,
                    exit_price=consensus_signal.price,
                    exit_time=datetime.now().isoformat(),
                    pnl=trade_result.pnl,
                    pnl_pct=(
                        trade_result.pnl / (position.entry_price * position.quantity)
                        if position and position.entry_price > 0 and position.quantity > 0
                        else None
                    )
                )
            except Exception as _e:
                logger.debug(f"[signal_tracker] 共识平仓回填失败：{_e}")
        
        # 推送通知
        try:
            action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
            if trade_type == 'OPEN':
                self.notifier.send_open_position_message(**push_data)
                logger.info(
                    f"[共识推送 - 开仓] {code} {action_str} | "
                    f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                    f"{strategies_str} | 订单：{trade_result.order_id}"
                )
            else:
                self.notifier.send_close_position_message(**push_data)
                logger.info(
                    f"[共识推送 - 平仓] {code} {action_str} | "
                    f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                    f"{strategies_str} | 订单：{trade_result.order_id} | 盈亏：{trade_result.pnl:.2f}"
                )
                if trade_result.pnl is not None:
                    self.adaptive_weight_manager.record_trade(
                        agree_strategies=result.agree_strategies,
                        pnl=trade_result.pnl,
                    )
                    self.consensus_engine.record_signal_outcome(
                        code=code,
                        action=result.action,
                        pnl=trade_result.pnl,
                    )
        except Exception as e:
            logger.error(f"推送通知失败：{e}")
        
        # 更新冷却时间
        self._update_cooldown(code)
    
    def _is_trading_hours(self) -> bool:
        """检查是否为交易时间"""
        from app.data.trading_hours import get_trading_hours_manager
        manager = get_trading_hours_manager()
        return manager.is_trading_hours()
    
    def _is_high_quality_trading_hours(self) -> bool:
        """检查是否为高质量交易时间"""
        from app.data.trading_hours import get_trading_hours_manager
        manager = get_trading_hours_manager()
        return manager.is_high_quality_trading_hours()
    
    def _is_cooled_down(self, code: str) -> bool:
        """检查标的是否冷却完成"""
        from app.data.trading_hours import get_trading_hours_manager
        manager = get_trading_hours_manager()
        return manager.is_cooled_down(code)
    
    def _update_cooldown(self, code: str) -> None:
        """更新标的冷却时间"""
        from app.data.trading_hours import get_trading_hours_manager
        manager = get_trading_hours_manager()
        manager.update_cooldown(code)
    
    def _get_atr_percent(self, code: str) -> float:
        """获取 ATR 百分比"""
        from app.data.kline_processor import get_kline_processor
        processor = get_kline_processor()
        return processor.calculate_atr_percent(code)
    
    def _get_multi_timeframe_signal(self, code: str, action: str) -> tuple:
        """获取多周期信号确认"""
        from app.data.kline_processor import get_kline_processor
        processor = get_kline_processor()
        return processor.get_mtf_signal_confirmation(code, action)
    
    def _get_mtf_alignment_score(self, code: str, action: str) -> tuple:
        """获取多周期对齐分数"""
        from app.data.kline_processor import get_kline_processor
        processor = get_kline_processor()
        return processor.calculate_mtf_alignment_score(code, action)
    
    def _get_dynamic_stop_params(self, code: str) -> dict:
        """获取动态止损止盈参数"""
        from app.data.kline_processor import get_kline_processor
        processor = get_kline_processor()
        return processor.get_dynamic_stop_params(code)
    
    def _get_news_sentiment_boost(self, code: str, action) -> float:
        """获取新闻情绪倍率"""
        # 简化实现，实际应从新闻模块获取
        return 1.0
    
    def _determine_trade_type(self, position, action: SignalAction) -> str:
        """判断交易类型"""
        if position is None:
            return 'OPEN'
        if position.action == 'BUY' and action == SignalAction.SELL:
            return 'CLOSE'
        if position.action == 'SELL' and action == SignalAction.BUY:
            return 'CLOSE'
        return 'HOLD'
    
    def _try_add_position(self, code: str, position, result, consensus_signal, signal_id: str) -> bool:
        """尝试顺势加仓"""
        try:
            # 1. 得分检查
            if result.score < self._ADD_SCORE_MIN:
                logger.debug(f"[加仓拒绝] {code} 得分={result.score:.2f} < {self._ADD_SCORE_MIN}")
                return False
            
            # 2. 置信度检查
            if result.confidence < self._ADD_CONFIDENCE_MIN:
                logger.debug(f"[加仓拒绝] {code} 置信度={result.confidence:.2f} < {self._ADD_CONFIDENCE_MIN}")
                return False
            
            # 3. 价格检查
            entry_price = position.entry_price
            if entry_price > 0 and result.price < entry_price * (1 + self._ADD_PRICE_MIN_PCT):
                logger.debug(
                    f"[加仓拒绝] {code} 当前价={result.price:.3f} 未超过均价*{1+self._ADD_PRICE_MIN_PCT}="
                    f"{entry_price * (1 + self._ADD_PRICE_MIN_PCT):.3f}"
                )
                return False
            
            # 4. 持仓时间检查
            entry_time_str = getattr(position, 'entry_time', None)
            if entry_time_str:
                et = datetime.fromisoformat(entry_time_str) if isinstance(entry_time_str, str) else entry_time_str
                hold_minutes = (datetime.now() - et).total_seconds() / 60
                if hold_minutes < self._ADD_HOLD_MIN_MINUTES:
                    logger.debug(
                        f"[加仓拒绝] {code} 持仓仅 {hold_minutes:.1f}min < {self._ADD_HOLD_MIN_MINUTES}min"
                    )
                    return False
            
            # 5. 今日加仓次数检查
            today_add_count = self.position_manager.get_today_add_count(code)
            if today_add_count >= self._ADD_MAX_TIMES_PER_DAY:
                logger.info(
                    f"[加仓拒绝] {code} 今日已加仓 {today_add_count} 次，"
                    f"上限={self._ADD_MAX_TIMES_PER_DAY}"
                )
                return False
            
            # 6. 计算加仓数量
            add_qty = max(100, int(consensus_signal.quantity * self._ADD_QTY_RATIO))
            
            # 7. 构造加仓信号
            add_signal = dataclasses.replace(
                consensus_signal,
                quantity=add_qty,
                extra={**consensus_signal.extra, 'is_add_position': True}
            )
            
            # 8. 执行加仓
            trade_result = self.trade_executor.execute(add_signal, 'OPEN')
            if not trade_result.success:
                logger.warning(f"[加仓失败] {code} 下单失败：{trade_result.message}")
                return False
            
            # 9. 更新持仓
            self.position_manager.add_to_position(
                code=code,
                add_qty=add_qty,
                add_price=result.price,
                add_time=datetime.now().isoformat(),
                signal_id=signal_id
            )
            
            # 10. 记录加仓流水
            self.position_manager.record_add_position(
                code=code,
                add_qty=add_qty,
                add_price=result.price,
                add_time=datetime.now().isoformat(),
                signal_id=signal_id
            )
            
            # 11. 记录信号执行
            signal_tracker.record_execution(
                signal_id, executed=True,
                execution_price=result.price,
                execution_time=datetime.now().isoformat()
            )
            
            logger.info(
                f"[顺势加仓] {code} +{add_qty}股 @ {result.price:.3f} | "
                f"得分={result.score:+.2f} | 置信度={result.confidence:.2f} | "
                f"原均价={entry_price:.3f} | 今日第{today_add_count + 1}次 | "
                f"订单={trade_result.order_id}"
            )
            
            # 12. 钉钉推送
            try:
                action_str = result.action.value if hasattr(result.action, 'value') else str(result.action)
                self.notifier.send_text(
                    f"[顺势加仓] {code} {action_str} +{add_qty}股 @ {result.price:.3f}\n"
                    f"原均价={entry_price:.3f} | 得分={result.score:+.2f} | 今日第{today_add_count + 1}次"
                )
            except Exception:
                pass
            
            return True
        
        except Exception as e:
            logger.error(f"[加仓异常] {code}: {e}", exc_info=True)
            return False
