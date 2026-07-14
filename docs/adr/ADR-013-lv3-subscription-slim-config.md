---
title: LV3 推送订阅收敛为可配置精简集 (降 CPU)
status: active
module: data
related_code:
  - app/data/futu_lv3_provider.py
  - app/data/subscription_manager.py
  - app/core/scheduler_core.py
  - config/unified_config.yaml
tags: [data, futu, lv3, performance, cpu, subscription, config]
updated: 2026-07-14
---

# ADR-013: LV3 推送订阅收敛为可配置精简集 (降 CPU)

**日期**: 2026-07-14
**状态**: 已实施
**决策者**: 用户 + Hermes

## 背景

用户报告 `main.py` 进程 CPU 占用很高，要求在不影响交易链路的前提下排查可优化点。

### 实证诊断

- 进程 PID 730262：**65.8% CPU / 57 线程**；单一热点线程 **TID 735021 ≈92% 单核**（5s 内 4.58s CPU），持续不间断。
- perf 采样：纯 Python 解释执行，**26% 花在对象构造 `slot_tp_init` + numpy datetime 索引**（`parse_dtype_from_datetime_typestr`/`prepare_index`/`array_copyto`）→ 反复构造 pandas DataFrame。
- 调用栈底 `start_thread → thread_run` = 一个 `threading.Thread`；排除 scan_loop（每 5min 有正常 sleep）→ 定位为 **Futu LV3 推送接收线程**。
- 根因：`scheduler_core` 硬编码**全量订阅 7 种类型 × 32 只美股**（`QUOTE+K_1M+K_5M+K_15M+K_30M+ORDER_BOOK+TICKER`）。每个推送 → Futu SDK 把 protobuf 建成 pandas DataFrame，handler 再 `iterrows()` 逐行建 Series。**同一笔成交同时刷新 4 个周期的在成形 bar → 每笔成交最多 4 条 kline 推送/只** → 接收线程持续满载。

### 浪费实证（零收益项）

| 类型 | 消费者 | 结论 |
|------|--------|------|
| K_5M / K_30M | 全库 0 处 | 纯浪费 |
| K_15M | 仅 `order_executor` 下单时 `get_kline` → 走 `request_history_kline`(HTTP, **不需订阅**) | 订阅浪费 |
| TICKER / ORDER_BOOK | router 注入微观因子 | **272/272 日志全 `ticker=N/A ob=N/A`**（100% 未用）；且实测几乎无推送 |
| QUOTE / K_1M / K_DAY(历史) | 价格 + 指标计算 | 必需，保留 |

附带发现：`_push_kline_cache[sym]` 只按标的存、不分周期，多订阅周期的 bar 混入同一列表，污染 K_1M 指标计算数据。

## 决策

方案（用户选定「只砍零风险项」+「立即重启生效」）：**LV3 股票推送订阅收敛为 `QUOTE + K_1M`**，并做成单一可配置源，遵循"一键可调、不硬编码"原则。

- `config/unified_config.yaml` 新增 `data_sources.futu_lv3.stock_sub_types: ['QUOTE', 'K_1M']`。
- `app/data/futu_lv3_provider.py` 新增 SSOT helper `_configured_stock_sub_types()`（读配置 + fallback `['QUOTE','K_1M']`），并作为 `subscribe()` 的 None 默认（覆盖**重连恢复**路径）。
- `app/data/subscription_manager.ensure_subscribed()` 的 None 默认改用该 helper（覆盖 **auto_loop 盘中重订阅** 路径 `ensure_subscribed(codes)`）。
- `app/core/scheduler_core.py` 两处显式订阅（启动全量订阅 + 分时调度立即订阅）改用 helper；槽位估算 `×7 → ×len(types)`。

统一后 5 处订阅默认收敛到单一配置源。

## 后果

- ✅ 砍掉 K_5M/K_15M/K_30M/ORDER_BOOK/TICKER 订阅 → 每笔成交的 kline 推送由 4 条降至 1 条，预计接收线程 CPU 下降 ~60–75%。
- ✅ 零交易链路影响：日线指标走 `request_history_kline`；1 分钟走 K_1M；K_15M 下单时按需 HTTP 拉取；被砍类型均无有效消费者。
- ✅ 顺带修复 K_1M 缓存被多周期污染的问题。
- ✅ 一键可逆/可调：如需恢复微观因子，在 YAML `stock_sub_types` 追加类型 + 重启即可。
- ⚠️ ETF/ORB 订阅（`QUOTE+K_1M+K_15M`）保持不变（ORB 策略依赖 ETF K_15M，标的数少、CPU 影响可忽略）。
- ⚠️ 生效需重启（config_container 无热重载 + 订阅在启动/盘前建立）。
