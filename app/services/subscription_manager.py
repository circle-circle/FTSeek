#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SubscriptionManager — 订阅管理服务

架构位置:
  DataSubscriber.subscribe / unsubscribe / reconnect
    ↓ 提取为
  app/services/subscription_manager.py

设计目的:
  将 Futu 订阅管理逻辑从 DataSubscriber 中提取为独立服务，
  包括订阅/退订/重连/回调设置等。

版本历史:
  2026-05-19 v1: 创建 (Phase 10D)
  2026-05-19 v2: 升级为生产版本 (Phase 16)
"""

import logging
import time
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Callable

logger = logging.getLogger("ftseek.services.subscription_manager")


class SubscriptionManager:
    """订阅管理服务 — Futu 订阅/退订/重连"""

    def __init__(
        self,
        quote_ctx,
        config,
        market_data_store,
        kline_buffers_by_period: Dict[str, Dict[str, Any]],
        kline_buffers: Dict[str, Any],
        update_multi_timeframe_trend_fn: Optional[Callable] = None,
        get_subscribe_codes_fn: Optional[Callable] = None,
        is_trading_hours_fn: Optional[Callable] = None,
    ):
        """
        初始化订阅管理器

        Args:
            quote_ctx: Futu 行情上下文 (可以是 None，延迟创建)
            config: 配置对象
            market_data_store: 市场数据存储
            kline_buffers_by_period: 按周期分组的 K 线缓冲区
            kline_buffers: K 线缓冲区 (按代码)
            update_multi_timeframe_trend_fn: 多周期趋势更新函数
            get_subscribe_codes_fn: 获取订阅代码列表函数
            is_trading_hours_fn: 判断是否交易时段函数
        """
        self._quote_ctx = quote_ctx
        self._config = config
        self._store = market_data_store
        self._kline_buffers_by_period = kline_buffers_by_period
        self._kline_buffers = kline_buffers
        self._update_multi_timeframe_trend = update_multi_timeframe_trend_fn or (lambda *args: None)
        self._get_subscribe_codes = get_subscribe_codes_fn or (lambda: getattr(config, 'WATCH_LIST_HK', []))
        self._is_trading_hours = is_trading_hours_fn or (lambda: True)
        self._subscribed = False
        self._reconnect_lock = None  # threading.Lock (由调用方注入)
        self._last_quote_time = 0.0
        self._quote_handler = None
        self._kline_handler = None

    def subscribe(self, codes: List[str] = None) -> None:
        """
        订阅股票行情 (生产版本 - 2026-04-22 升级为动态池驱动)

        Args:
            codes: 可选，显式指定订阅列表。None 时从动态池获取。
        """
        from futu import SubType

        if self._quote_ctx is None:
            logger.warning("[订阅管理] 无行情上下文，跳过订阅")
            return

        sub_type_map = {
            "QUOTE":  SubType.QUOTE,
            "K_1M":   SubType.K_1M,
            "K_3M":   SubType.K_3M,
            "K_5M":   SubType.K_5M,
            "K_15M":  SubType.K_15M,
            "K_30M":  SubType.K_30M,
            "K_60M":  SubType.K_60M,
            "K_DAY":  SubType.K_DAY,
        }

        # 获取订阅标的列表
        if codes is None:
            subscribe_list = self._get_subscribe_codes()
        else:
            subscribe_list = codes

        # 过滤港股
        subscribe_list = [c for c in subscribe_list if c.startswith("HK.")]

        sub_types = [sub_type_map[st] for st in self._config.SUBSCRIBE_TYPES if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]

        logger.info(f"开始订阅 {len(subscribe_list)} 只港股标的，类型={[str(s) for s in sub_types]}")
        success_count = 0
        fail_count = 0
        for code in subscribe_list:
            for st in sub_types:
                ret, msg = self._quote_ctx.subscribe([code], [st])
                if ret == 0:
                    success_count += 1
                    logger.debug(f"订阅成功：{code} -> {st}")
                else:
                    fail_count += 1
                    if "无权限" in str(msg) or "no permission" in str(msg).lower():
                        logger.warning(f"订阅跳过 (无权限): {code} -> {st} | {msg}")
                    else:
                        logger.error(f"订阅失败：{code} -> {st}: {msg}")
        logger.info(f"订阅完成：成功={success_count}, 失败={fail_count}, 总计={success_count+fail_count}")
        self._subscribed = True

        # 启动预热
        self._warm_up_buffers()

    def _warm_up_buffers(self):
        """
        启动预热 (Warm-up) — 从 Futu 拉取历史 K 线填充 MTF 缓存

        目标:
            系统重启或隔夜后，内存中的 K 线 Buffer 为空。
            本方法会拉取最近 3 天的 K 线数据并推入 Buffer，
            从而立即初始化多周期趋势 (_multi_timeframe_trends)。

        策略:
            1. 获取 Watchlist
            2. 对每个标的请求 K_60M 和 K_DAY (最影响 MTF 对齐度)
            3. 少量 K_1M (辅助短期趋势)
            4. 限制并发和频率，防止触发 OpenD Limit
        """
        from futu import KLType

        watchlist = getattr(self._config, 'WATCH_LIST_HK', [])
        if not watchlist:
            logger.warning("[预热] 无 Watchlist，跳过预热")
            return

        try:
            pool_codes = self._get_subscribe_codes()
            if pool_codes:
                watchlist = pool_codes
        except Exception:
            pass

        if self._quote_ctx is None:
            logger.warning("[预热] 无行情上下文，跳过预热")
            return

        logger.info(f"[预热] 开始拉取 {len(watchlist)} 只股票历史数据...")

        # 定义需要的 K 线类型和数量
        warmup_targets = [
            (KLType.K_60M, 50),
            (KLType.K_DAY, 60),
            (KLType.K_15M, 100),
        ]

        success_count = 0
        fail_count = 0

        for code in watchlist:
            try:
                for ktype, count in warmup_targets:
                    end_time = datetime.now().strftime('%Y-%m-%d')
                    start_time = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')

                    ret, data, _ = self._quote_ctx.request_history_kline(
                        code, start=start_time, end=end_time, ktype=ktype, autype='qfq'
                    )
                    if ret == 0 and data is not None and not data.empty:
                        # Determine the correct key for the buffer
                        key = ktype.name if hasattr(ktype, 'name') else str(ktype)
                        if key.startswith('KLType.'):
                            key = key.split('.')[-1]

                        buffer = self._kline_buffers_by_period[code][key]

                        for _, row in data.iterrows():
                            # 构造 kline dict 以复用 push_kline 逻辑
                            kline = {
                                'time_key': row['time_key'],
                                'open': row['open'],
                                'high': row['high'],
                                'low': row['low'],
                                'close': row['close'],
                                'volume': row['volume'],
                            }
                            buffer.push_kline(kline)

                        # 触发趋势更新
                        last_row = data.iloc[-1]
                        kline_last = {
                            'time_key': last_row['time_key'],
                            'open': last_row['open'],
                            'high': last_row['high'],
                            'low': last_row['low'],
                            'close': last_row['close'],
                            'volume': last_row['volume'],
                        }
                        self._update_multi_timeframe_trend(code, key, kline_last)

                        success_count += 1

                    time.sleep(0.1)  # 简单限流
            except Exception as e:
                fail_count += 1
                logger.warning(f"[预热] {code} 失败：{e}")

        logger.info(f"[预热] 完成：成功填充 {success_count} 组数据，{fail_count} 组失败")
        logger.info("[预热] MTF 缓存已初始化，策略可随时响应")

    def unsubscribe_all(self) -> None:
        """
        取消所有港股行情订阅 (用于港股收盘后释放富途 OpenD 槽位)
        """
        from futu import SubType

        if not self._quote_ctx:
            logger.debug("[动态订阅] 无活跃连接，无需退订")
            return

        # 获取当前订阅的代码列表
        try:
            codes = self._get_subscribe_codes()
            codes = [c for c in codes if c.startswith("HK.")]
        except Exception as e:
            logger.warning(f"[动态订阅] 获取代码列表失败：{e}")
            codes = getattr(self._config, 'WATCH_LIST_HK', [])

        if not codes:
            logger.debug("[动态订阅] 无代码，无需退订")
            return

        sub_type_map = {
            "QUOTE":  SubType.QUOTE,
            "K_1M":   SubType.K_1M,
            "K_3M":   SubType.K_3M,
            "K_5M":   SubType.K_5M,
            "K_15M":  SubType.K_15M,
            "K_30M":  SubType.K_30M,
            "K_60M":  SubType.K_60M,
            "K_DAY":  SubType.K_DAY,
        }
        sub_types = [sub_type_map[st] for st in self._config.SUBSCRIBE_TYPES if st in sub_type_map]
        if not sub_types:
            sub_types = [SubType.QUOTE]

        logger.info(f"开始退订 {len(codes)} 只港股标的 (收盘释放槽位)...")
        success_count = 0
        fail_count = 0

        for code in codes:
            for st in sub_types:
                try:
                    ret, msg = self._quote_ctx.unsubscribe([code], [st])
                    if ret == 0:
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    fail_count += 1
                    logger.warning(f"[退订] {code} {st} 异常：{e}")

        self._subscribed = False
        logger.info(f"[动态订阅] 退订完成：成功={success_count}, 失败={fail_count} | 槽位已释放")

    def reconnect(self) -> None:
        """
        断线重连 (优化版 - 2026-03-27): 优先复用现有连接

        优化逻辑:
        1. 先测试现有连接是否可用
        2. 仅在网络真正断开时重建
        3. 重新订阅而非重新创建连接
        """
        if self._reconnect_lock is not None and not self._reconnect_lock.acquire(blocking=False):
            logger.warning("[重连] 已有重连任务进行中，跳过")
            return

        try:
            logger.info("[重连] 检测到行情断线，开始自动重连...")
            # 关闭旧连接（强制释放 Futu SDK 内部 socket）
            try:
                if self._quote_ctx is not None:
                    self._quote_ctx.close()
            except Exception:
                pass
            finally:
                self._quote_ctx = None

            # 等待 2 秒让 OpenD 清理旧会话
            time.sleep(2)

            # 重建连接 → 重订阅 → 重设 handler（指数退避，最多 5 次）
            max_retries = 5
            for attempt in range(1, max_retries + 1):
                try:
                    self.subscribe()
                    self._last_quote_time = time.time()
                    logger.info(f"[重连] 第 {attempt} 次重连成功，行情已恢复")
                    return
                except Exception as e:
                    logger.error(f"[重连] 第 {attempt} 次重连失败：{e}")
                    # 清除上次失败的 ctx，下次重建
                    try:
                        if self._quote_ctx is not None:
                            self._quote_ctx.close()
                    except Exception:
                        pass
                    self._quote_ctx = None
                    time.sleep(min(5 * attempt, 30))  # 最长等 30 秒

            logger.error("[重连] 达到最大重试次数，行情断线未恢复，请人工检查")
        finally:
            if self._reconnect_lock is not None:
                self._reconnect_lock.release()

    def is_subscribed(self) -> bool:
        """检查是否已订阅"""
        return self._subscribed
