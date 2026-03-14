"""
数据订阅器
对接 futu-api 订阅实时行情，分发给策略引擎
信号经过「加权共识引擎」评分，多策略加权确认后才推送
"""
import os
import sys
import logging as stdlib_logging

# 必须在导入 futu 之前设置日志环境变量和配置
os.environ["FUTU_LOG_LEVEL"] = "ERROR"
os.environ["FUTU_LOG_TO_FILE"] = "0"
os.environ["FUTU_LOG_PATH"] = "/tmp/futu_logs"

# 仅配置 futu 日志，不影响应用日志
futu_logger = stdlib_logging.getLogger("futu")
futu_logger.setLevel(stdlib_logging.CRITICAL)
# 清除 futu 日志的所有 handler，防止文件锁
futu_logger.handlers = []
futu_logger.propagate = False

import logging
import threading
import time
from collections import defaultdict
from typing import List, Dict, Tuple

from app import config
from app.strategy.base import BaseStrategy, Signal, SignalAction
from app.strategy.indicators import IndicatorBuffer, adx as calc_adx
from app.signal_store import signal_store
from app.notifier.dingtalk import DingTalkNotifier
from app.strategy.consensus_engine import ConsensusEngine, ConsensusResult
from app.trader.executor import TradeExecutor
from app.trader.position_manager import PositionManager

logger = logging.getLogger("ftseek.subscriber")

# 推送冷却 (同标的达成共识后冷却)
SIGNAL_COOLDOWN = int(getattr(config, "SIGNAL_COOLDOWN", 300))

# 共识引擎参数
CONSENSUS_WINDOW    = int(getattr(config, "CONSENSUS_WINDOW", 60))
CONSENSUS_THRESHOLD = float(getattr(config, "CONSENSUS_THRESHOLD", 1.5))
CONSENSUS_ADX_TREND = float(getattr(config, "CONSENSUS_ADX_TREND", 25))
CONSENSUS_ADX_RANGE = float(getattr(config, "CONSENSUS_ADX_RANGE", 20))

# ADX 计算周期
ADX_PERIOD = 14


class QuoteHandler:
    def __init__(self, strategies, notifier):
        self.strategies = strategies
        self.notifier = notifier

    def on_recv_rsp(self, rsp_pb):
        pass


class DataSubscriber:
    """富途行情订阅管理器 (加权共识模式)"""

    def __init__(self, strategies: List[BaseStrategy], notifier: DingTalkNotifier):
        self.strategies = strategies
        self.notifier = notifier
        self._quote_ctx = None
        self._running = False

        # 推送冷却: code -> last_push_time
        self._cooldown: Dict[str, float] = {}
        self._cooldown_lock = threading.Lock()

        # K 线缓冲: 为每个 code 维护滑动窗口, 用于计算 ADX
        self._kline_buffers: Dict[str, IndicatorBuffer] = defaultdict(lambda: IndicatorBuffer(maxlen=200))

        # 交易执行器和持仓管理器
        self.trade_executor = TradeExecutor()
        self.position_manager = PositionManager()

        # 加权共识引擎
        self._consensus = ConsensusEngine(
            window_seconds=CONSENSUS_WINDOW,
            threshold=CONSENSUS_THRESHOLD,
            adx_trend=CONSENSUS_ADX_TREND,
            adx_range=CONSENSUS_ADX_RANGE,
            on_consensus=self._on_consensus,
        )

    def _get_quote_ctx(self):
        if self._quote_ctx is None:
            import logging as stdlib_logging
            
            # 配置 futu 日志到临时目录，避免文件锁冲突
            futu_logger = stdlib_logging.getLogger("futu")
            futu_logger.setLevel(stdlib_logging.ERROR)
            
            # 移除现有的文件 handler
            for handler in futu_logger.handlers[:]:
                if isinstance(handler, stdlib_logging.FileHandler):
                    futu_logger.removeHandler(handler)
            
            try:
                from futu import OpenQuoteContext
                self._quote_ctx = OpenQuoteContext(
                    host=config.OPEND_HOST, port=config.OPEND_PORT
                )
                logger.info("行情上下文已创建")
            except Exception as e:
                logger.error(f"创建行情上下文失败：{e}")
                raise
        return self._quote_ctx

    def _is_cooled_down(self, code: str) -> bool:
        now = time.time()
        with self._cooldown_lock:
            last_time = self._cooldown.get(code, 0)
            if now - last_time < SIGNAL_COOLDOWN:
                return False
            self._cooldown[code] = now
            return True

    # ----------------------------------------------------------
    #  ADX 实时更新
    # ----------------------------------------------------------
    def _update_adx(self, code: str, kline: dict):
        """每根 K 线到达时更新 ADX 并注入共识引擎"""
        buf = self._kline_buffers[code]
        buf.push_kline(kline)

        if buf.size < ADX_PERIOD * 2 + 5:
            return  # 数据不够

        adx_val = calc_adx(
            list(buf.high), list(buf.low), list(buf.close), ADX_PERIOD
        )
        if adx_val is not None:
            self._consensus.update_adx(code, adx_val)

    # ----------------------------------------------------------
    #  共识达成回调
    # ----------------------------------------------------------
    def _on_consensus(self, result: ConsensusResult):
        """推送共识信号 (修改版) - 先判断持仓，再执行交易和推送"""
        code = result.code

        if not self._is_cooled_down(code):
            logger.info(
                f"[共识] {code} 冷却中，跳过推送 "
                f"(分数={result.score:+.2f}, {', '.join(result.agree_strategies)})"
            )
            return

        base_signal = result.votes[0].signal
        strategies_str = " + ".join(result.agree_strategies)
        n_agree = len(result.agree_strategies)

        # 构造合并推送的 reason
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
            strategy=f"共识({strategies_str})",
            extra={
                "consensus": True,
                "score": result.score,
                "threshold": result.threshold,
                "market_regime": result.market_regime,
                "adx": result.adx_value,
                "agree_count": n_agree,
                "total_count": n_agree + len(result.oppose_strategies),
                "strategies": result.agree_strategies,
                "oppose": result.oppose_strategies,
                "weights": result.weight_details,
            },
        )

        # 2026-03-13: 添加 watchlist 过滤，仅推送启用的股票
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
                return  # 跳过未启用股票的推送
        except Exception as e:
            logger.warning(f"watchlist 过滤异常：{e}")

        # 2026-03-13: 修改逻辑 - 先判断持仓，再执行交易和推送
        # 1. 检查持仓
        position = self.position_manager.get_position(code)
        
        # 2. 判断交易类型
        trade_type = self._determine_trade_type(position, result.action)
        
        # 3. 执行交易
        trade_result = self.trade_executor.execute(consensus_signal, trade_type)
        
        if not trade_result.success:
            logger.warning(f"交易失败：{trade_result.message}")
            return  # 不推送
        
        # 4. 准备推送数据
        push_data = {
            'signal': consensus_signal,
            'trade_result': trade_result,
            'position': position,
            'consensus_result': result
        }
        
        # 5. 推送
        if trade_type == 'OPEN':
            self.notifier.send_open_position_message(**push_data)
            logger.info(
                f"[共识推送 - 开仓] {code} {result.action.value} | "
                f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                f"{strategies_str} | 订单：{trade_result.order_id}"
            )
        else:
            self.notifier.send_close_position_message(**push_data)
            logger.info(
                f"[共识推送 - 平仓] {code} {result.action.value} | "
                f"分数={result.score:+.2f} | 市场={result.market_regime} | "
                f"{strategies_str} | 订单：{trade_result.order_id} | 盈亏：{trade_result.pnl:.2f}"
            )

    def _determine_trade_type(self, position, action: SignalAction) -> str:
        """判断是开仓还是平仓"""
        if position is None:
            return 'OPEN'
        if position.action == 'BUY' and action == SignalAction.SELL:
            return 'CLOSE'
        if position.action == 'SELL' and action == SignalAction.BUY:
            return 'CLOSE'
        return 'OPEN'  # 同向信号，暂不处理或加仓


    def _process_signal(self, signal: Signal):
        if signal is None:
            return
        self._consensus.submit(signal)

    # ----------------------------------------------------------
    #  行情订阅
    # ----------------------------------------------------------
    def subscribe(self):
        from futu import SubType

        ctx = self._get_quote_ctx()

        sub_type_map = {
            "QUOTE": SubType.QUOTE, "K_1M": SubType.K_1M,
            "K_5M": SubType.K_5M, "K_15M": SubType.K_15M, "K_DAY": SubType.K_DAY,
        }

        sub_types = [sub_type_map[st] for st in config.SUBSCRIBE_TYPES if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]

        for code in config.WATCH_LIST:
            for st in sub_types:
                ret, msg = ctx.subscribe([code], [st])
                if ret == 0:
                    logger.info(f"订阅成功: {code} -> {st}")
                else:
                    if "无权限" in str(msg) or "no permission" in str(msg).lower():
                        logger.warning(f"订阅跳过(无权限): {code} -> {st} | {msg}")
                    else:
                        logger.error(f"订阅失败: {code} -> {st}: {msg}")

    def _setup_handlers(self):
        from futu import StockQuoteHandlerBase, CurKlineHandlerBase

        strategies = self.strategies
        process_signal = self._process_signal
        update_adx = self._update_adx

        class MyQuoteHandler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_str):
                ret, data = super().on_recv_rsp(rsp_str)
                if ret != 0:
                    return
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    quote = row.to_dict()
                    for strategy in strategies:
                        sig = strategy.on_quote(code, quote)
                        if sig:
                            process_signal(sig)

        class MyKlineHandler(CurKlineHandlerBase):
            def on_recv_rsp(self, rsp_str):
                ret, data = super().on_recv_rsp(rsp_str)
                if ret != 0:
                    return
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    kline = row.to_dict()
                    k_type = kline.get("k_type", "K_1M")
                    # 更新 ADX (每根 K 线)
                    update_adx(code, kline)
                    for strategy in strategies:
                        sig = strategy.on_kline(code, kline, k_type)
                        if sig:
                            process_signal(sig)

        ctx = self._get_quote_ctx()
        ctx.set_handler(MyQuoteHandler())
        ctx.set_handler(MyKlineHandler())
        logger.info("行情回调 handler 已设置")

    def start(self):
        self._running = True
        try:
            self.subscribe()
            self._setup_handlers()
            logger.info(
                f"数据订阅已启动 | 共识窗口={CONSENSUS_WINDOW}s | "
                f"触发阈值={CONSENSUS_THRESHOLD} | "
                f"ADX趋势>{CONSENSUS_ADX_TREND} 震荡<{CONSENSUS_ADX_RANGE} | "
                f"推送冷却={SIGNAL_COOLDOWN}s"
            )
        except Exception as e:
            logger.error(f"数据订阅启动异常：{e}")
            raise

        def cleanup_loop():
            while self._running:
                time.sleep(60)
                signal_store.cleanup_expired()
                self._cleanup_cooldown()

        t = threading.Thread(target=cleanup_loop, daemon=True)
        t.start()

    def _cleanup_cooldown(self):
        now = time.time()
        with self._cooldown_lock:
            expired = [k for k, t in self._cooldown.items() if now - t > SIGNAL_COOLDOWN * 2]
            for k in expired:
                del self._cooldown[k]

    def stop(self):
        self._running = False
        if self._quote_ctx:
            self._quote_ctx.close()
            self._quote_ctx = None
            logger.info("行情上下文已关闭")

    def get_snapshot(self, codes=None):
        ctx = self._get_quote_ctx()
        codes = codes or config.WATCH_LIST
        ret, data = ctx.get_market_snapshot(codes)
        return data if ret == 0 else None
