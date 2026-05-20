#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场趋势门禁 (Market Regime Gate)

防止在恒指下跌趋势中开新仓。

根因:
- 恒指 MA5 < MA20 时，个股做多信号多为假突破
- 逆市做多胜率低
- 横盘震荡 (gap < 0.3%) 不拦截

配置:
    strategy.market_regime_gate:
      enabled: true
      hsi_gap_min_pct: 0.3

行为:
- 实盘：MA5 < MA20 且 gap >= 0.3% → 硬拦截
- 模拟盘：告警但放行 (数据收集)
- 横盘震荡：放行

版本历史:
  2026-05-19: 从 executor.py 提取
"""

import time as _time
from typing import Tuple, Optional, Dict

from app.trader.gates.base_gate import GateInterface
from app.strategy.base import Signal


# HSI 数据缓存 (5 分钟 TTL)
_hsi_cache: Dict[str, Optional[float]] = {
    'ma5': None,
    'ma20': None,
    'current': None,
    'ts': 0,
}


class MarketRegimeGate(GateInterface):
    """
    市场趋势门禁 — 防止在恒指下跌趋势中开新仓

    依赖 context:
        - config: 配置 (提供 MARKET_REGIME_GATE)
        - logger: 日志记录器
        - failure_tracker: 失败追踪器
        - is_sim: 是否模拟盘
        - futu_limiter: Futu API 限流器 (可选)
        - quote_ctx: 行情上下文 (可选)
    """

    def name(self) -> str:
        return "MarketRegimeGate"

    def is_enabled(self, context: dict) -> bool:
        config = context.get('config')
        if config is None:
            return True
        _mr_cfg = getattr(config, 'MARKET_REGIME_GATE', {'enabled': True})
        return _mr_cfg.get('enabled', True)

    def _get_hsi_data(self, context: dict) -> bool:
        """
        获取恒指数据 (MA5, MA20)

        Returns:
            bool: 是否成功获取
        """
        global _hsi_cache
        config = context.get('config')
        logger = context.get('logger')
        futu_limiter = context.get('futu_limiter')

        _cache_ttl = 300  # 5 分钟
        _now_ts = _time.time()

        if _hsi_cache['ma5'] is not None and (_now_ts - _hsi_cache['ts']) <= _cache_ttl:
            return True  # 缓存有效

        try:
            from futu import OpenQuoteContext, KLType, RET_OK

            _hq = OpenQuoteContext(host=config.OPEND_HOST, port=config.OPEND_PORT)
            _ret_k, _hsi_data = None, None

            if futu_limiter and futu_limiter.acquire(timeout=5):
                _ret_k, _hsi_data = _hq.get_cur_kline("HK.800000", 30, KLType.K_DAY)
                futu_limiter.record()
            else:
                _ret_k, _hsi_data = _hq.get_cur_kline("HK.800000", 30, KLType.K_DAY)

            _hq.close()

            if _ret_k == RET_OK and _hsi_data is not None and len(_hsi_data) >= 20:
                _closes = _hsi_data['close'].values.tolist()
                _hsi_cache['ma5'] = sum(_closes[-5:]) / 5
                _hsi_cache['ma20'] = sum(_closes[-20:]) / 20
                _hsi_cache['current'] = _closes[-1]
                _hsi_cache['ts'] = _now_ts
                return True
            else:
                if logger:
                    logger.debug(f"[市场趋势门禁] HSI 数据获取失败 (ret={_ret_k})，降级放行")
                _hsi_cache['ma5'] = None
                _hsi_cache['ts'] = 0
                return False

        except Exception as e:
            if logger:
                logger.debug(f"[市场趋势门禁] HSI 数据获取异常：{e}")
            _hsi_cache['ma5'] = None
            _hsi_cache['ts'] = 0
            return False

    def check(self, signal: Signal, context: dict) -> Tuple[bool, str]:
        """
        检查市场趋势是否允许开仓

        Returns:
            (True, ""): 通过
            (False, "原因"): 拦截
        """
        config = context.get('config')
        logger = context.get('logger')
        failure_tracker = context.get('failure_tracker')
        is_sim = context.get('is_sim', True)

        if not self.is_enabled(context):
            return True, ""

        if config is None:
            return True, ""

        # 获取配置
        _mr_cfg = getattr(config, 'MARKET_REGIME_GATE', {
            'enabled': True,
            'hsi_gap_min_pct': 0.3,
        })
        _gap_min = _mr_cfg.get('hsi_gap_min_pct', 0.3)

        # 获取恒指数据
        if not self._get_hsi_data(context):
            return True, ""  # 数据获取失败，降级放行

        _ma5 = _hsi_cache['ma5']
        _ma20 = _hsi_cache['ma20']
        _current = _hsi_cache['current']

        if _ma5 is None or _ma20 is None:
            return True, ""

        _gap_pct = (_ma20 - _ma5) / _ma20 * 100 if _ma20 > 0 else 0

        if _ma5 < _ma20 and abs(_gap_pct) >= _gap_min:
            # 恒指处于下跌趋势 (MA5 < MA20 且偏离幅度≥阈值)
            msg = f"市场趋势门禁: HSI MA5({_ma5:.0f}) < MA20({_ma20:.0f}) 差距{_gap_pct:.1f}%≥{_gap_min}%"

            if is_sim:
                # 模拟盘：告警 + 打标签，不硬拦截
                if logger:
                    logger.warning(f"[模拟门禁·影子] {signal.code} {msg}，告警但放行 (数据收集)")
                signal.extra = signal.extra or {}
                signal.extra['hsi_regime_warning'] = f'downtrend_gap_{_gap_pct:.1f}pct'
                return True, ""  # 告警但放行
            else:
                # 实盘：硬拦截
                msg += "，下跌趋势中禁止开新仓 (逆市做多风险高)"
                if logger:
                    logger.info(f"[市场趋势门禁] {signal.code} {msg}")
                if failure_tracker:
                    failure_tracker.record_failure(signal.code, 'market_regime_gate', msg)
                return False, msg

        elif _ma5 < _ma20:
            # MA5 < MA20 但 gap 太小，视为横盘震荡
            if logger:
                logger.debug(f"[市场趋势门禁] HSI MA5({_ma5:.0f}) < MA20({_ma20:.0f}) 但差距{_gap_pct:.1f}% < {_gap_min}%，视为横盘，放行")
        else:
            if logger:
                logger.debug(f"[市场趋势门禁] HSI MA5({_ma5:.0f}) >= MA20({_ma20:.0f})，趋势允许开仓")

        # 通过：趋势允许
        return True, ""
