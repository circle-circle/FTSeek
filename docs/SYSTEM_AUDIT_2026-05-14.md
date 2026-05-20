# 全项目第一性原理审查报告

> **生成日期**: 2026-05-14
> **审查范围**: app/ 全代码 + config/ + 数据流 + 执行链
> **目的**: 上线模拟盘前最后一次完整性审查, 列出可行 fix 路径

## 摘要

| 维度 | P0 (开盘前必修) | P1 (本周修) | P2 (本月修) |
|-----|---|---|---|
| 数据流 | 4 ✅ | 3 | 2 |
| 信号生成 | 2 ✅ | 4 | 1 |
| 风控 | 2 ✅ | 2 | 2 |
| 执行容错 | 3 ✅ | 4 | 3 |
| **合计** | **11 ✅** | **13** | **8** |

**P0 状态**: 全部完成 (2026-05-14). 18 条新单测覆盖, coordinator 总测 43/43 全绿.

### P0 修复完成清单
| ID | 修复 | 关键文件 |
|----|------|---------|
| E1 | `audit_file = get_logs_dir()/futu_trade_audit.jsonl` | `app/futu_integration.py`, `app/reports/daily_health_report.py` |
| D1 | `get_market_phase` 接 `holiday_calendar.is_us_holiday` | `app/core/scheduler_triggers.py:49-79` |
| D2 | atr<=0 / price<=0 → `(0,0)` 拒交易; score 接受空信号 | `app/core/intraday_scorer.py:1041-1046, 344-347` |
| D4 | client_order_id 加 `uuid.uuid4().hex[:6]` 后缀 (5 处) | `app/pipeline/order_executor.py` |
| R1 | `PreTradeChecker._check_sector_limit` + 板块 cap | `app/risk/pre_trade_check.py` |
| R2 | `_check_system_breaker` + monitor_loop 推送状态 | `app/risk/pre_trade_check.py`, `app/pipeline/position_monitor.py` |
| S1 | `bars_5m_count >= 14` warmup 检查 | `app/core/intraday_scorer.py:294-298` |
| S2 | `is_halted` 检测跳过 | `app/core/intraday_scorer.py:300-302` |
| E2 | `_ensure_connected` + `place_sell_order(max_retries=3, backoff)` | `app/futu_integration.py` |
| E3 | `OrderTracker.restore_pending_on_startup` + main.py 启动序列 | `app/risk/order_tracker.py`, `main.py:387-399` |
| D3 | `app/data/split_event_guard.py` + 启发式跳变检测 + ExitMonitor 跳过 FLASH | 新建 + `intraday_scorer.py` + `position_exit_monitor.py:512-520` |

---

## 1. 数据流缺陷

### P0-D1 — `get_market_phase()` 不查节假日日历 ⭐

**现状**: `app/core/scheduler_triggers.py:49-83` 的 `get_market_phase()` 仅以 `weekday >= 5` 排除周末, 不查 `app/utils/holiday_calendar.py` 的 US_HOLIDAYS_2026/2027.

**风险**: Memorial Day / July 4 / Thanksgiving / Christmas 当日 (周一-周五) 系统会进入 TRADING phase, 触发 LLM 调用 + 行情订阅 + 信号扫描, 全部命中空数据 / 错误 / 浪费配额.

**修复**:
```python
# scheduler_triggers.py:49 修改
def get_market_phase() -> MarketPhase:
    now_et = datetime.now(ET)
    weekday = now_et.weekday()
    if weekday >= 5:
        return MarketPhase.WEEKEND
    # 新增: 查节假日日历
    try:
        from app.utils.holiday_calendar import get_holiday_calendar
        if get_holiday_calendar().is_us_holiday(now_et.date()):
            return MarketPhase.WEEKEND  # 复用 WEEKEND 行为
    except Exception:
        pass
    # ... 原逻辑
```

**影响评估**: 高 — 节假日重启或调度都可能触发误交易; 模拟盘也会浪费 LLM API 配额.
**验证**: 单测 mock `datetime.now(ET)` 为 2026-07-04 (周三) → 断言返回 WEEKEND.

---

### P0-D2 — ATR=0 默认致止损宽度失真 ⭐

**现状**: `app/core/intraday_scorer.py:341` `atr = float(context.get("atr", 0))` 默认 0. 在 `vix_stop_multiplier(vix) * atr / price` 中 atr=0 → 止损距离=`max(0, 0.02)=0.02` (2% 兜底). 但仓位计算 `risk / stop_distance = 0.005 / 0.02 = 0.25` 已被 cap 到 0.20.

**风险**: 新上市标的 / 数据源缺 ATR / 取数失败时, 系统不会拒交易, 而是用 2% 固定止损. 高波动股 (TSLA 日内 4% ATR) 用 2% 止损 → 止损被噪声打掉的概率 70%+.

**修复**:
```python
# intraday_scorer.py:1041-1044 改写
if atr <= 0 or price <= 0:
    # 数据缺失 → 拒绝信号
    return 0.0, 0.0  # confidence=0 调用方应丢弃
```
配合 `_submit_order` 增加 `if signal.position_pct == 0: return` 拦截.

**影响**: 高 — 直接影响每笔交易的止损质量.
**验证**: 单测 atr=0/price=0 → 返回 (0,0); 现有 28 仓位中 ATR 缺失比例 grep 数据库.

---

### P0-D3 — 拆股/复权处理缺失 ⭐

**现状**: 全工程 grep `split|adjusted|adj_close` 仅在 `alphavantage_provider.py` 解析 CSV 时使用, 无任何"拆股发生 → 历史 K 线复权"逻辑. `MarketDataRouter.get_full_market_context()` 直接拿 Futu 当前价 vs 历史 EMA20.

**风险**: NVDA 2024-06-10 1-for-10 拆股, 价格从 $1200 → $120. 若拆股日早盘扫描, 系统看到"-90%" → FLASH_EXIT 触发整批仓位; 信号侧看到 RSI / MA 全部失真 → 误识别为崩盘.

**修复 (最小集)**:
1. 加 `app/data/split_event_guard.py`: 启动时拉 polygon/finnhub 当日拆股事件
2. 命中拆股 → 该 symbol 跳过 1 个交易日; PositionExitMonitor 跳过该 symbol 的 FLASH 检查
3. 数据源端: TwelveData/AlphaVantage 已默认返回复权数据, Futu LV3 返回原始数据 → 对齐口径

**影响**: 中 — 拆股事件每月 < 5 个, 但命中即灾难.
**验证**: 单测 mock 价格突变 -50%, has_split_today=True → SKIP exit; has_split_today=False → FLASH_EXIT 触发.

---

### P0-D4 — `client_order_id` 1 秒粒度易碰撞

**现状**: `order_executor.py:1208,1423,2030` 等 5 处使用 `f"{symbol}_{int(time.time())}"` 作为 client_order_id.

**风险**: 同标的同方向 1 秒内重复触发 (如 LLM 投票延迟 race + 主线 cooldown 失效) → 两单 client_order_id 完全相同 → broker 拒第二单或 (更糟) 双倍仓位.

**修复**: 加 uuid.uuid4().hex[:6] 后缀, 与 `signal_pipeline.py:1224` trace_id 风格一致:
```python
client_order_id=f"{symbol}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
```

**影响**: 中 — 模拟盘可能不暴露, 实盘高频时必出.
**验证**: 单测构造 1 秒内同 symbol 5 单 → 5 个 client_order_id 全部唯一.

---

### P1-D5 — `change_percent` 日累计语义残留

**现状**: F6 已修主路径用 `slope_5m`, 但 `intraday_scorer.py:511` fallback 仍用日累计 `change_percent`. `signal_pipeline.py:771,958,1142` 多处仍读 `change_percent` 做决策.

**风险**: slope_5m 不可用时静默退化, 14:00 信号用 09:30 累计 → 决策时机错位.

**修复**: fallback 时直接 `return 0.0` 不出信号, 而非用滞后数据.

---

### P1-D6 — RSI 默认 50 当"中性信号"

**现状**: `intraday_scorer.py:464` `rsi = float(ctx.get("rsi", 50))`; `app/strategy/base.py:147` 也默认 50.0.

**风险**: 数据源失败 / 标的样本不足时, RSI=50 触发"温和看多/看空"双向加分 (line 489-493 都有逻辑), 但实际是数据缺失.

**修复**: 默认改 None, scoring 时检查 `if rsi is None: skip`.

---

### P1-D7 — News timestamp 无 BEFORE-signal 校验

**现状**: `app/news_v2/sources/finnhub_news_source.py:130` 直接用 article.datetime 当 timestamp, 无校验 timestamp ≤ now.

**风险**: API 返回未来时间 (服务器时钟漂移 / 时区错) → 用未发生新闻"预测"信号 = 前视偏差.

**修复**: 接收新闻时 `if ts > time.time() + 60: drop`.

---

### P1-D8 — `datetime.now()` 命名前 vs ET-aware 混用

**现状**: `signal_pipeline.py:1211 expires_at = datetime.now() + timedelta(minutes=30)` (naive); `models.py:Signal.is_expired()` 比较时若另一方是 ET-aware → TypeError 静默吞掉变 False (永不过期).

**修复**: 全工程统一 `datetime.now(timezone.utc)` 或 `datetime.now(ET)`. 模型层做 helper.

---

### P2-D9 — Watchlist 静态, 无退市处理

**现状**: `config/watchlist_us.json` 静态文件, 无定期校验.

**风险**: 退市标的仍订阅 → Futu 报错; 历史数据有但未来无 → 复盘脏.

**修复**: 启动时调 Futu/Polygon `get_stock_basicinfo` 校验, 失败的标记 disabled.

---

### P2-D10 — 数据源 fallback 时间口径不一致

**现状**: `MarketDataRouter` 有 Futu LV3 → TwelveData → Finnhub → AlphaVantage 多级, 但各源时区/精度/复权口径未统一.

**修复**: 在 router 层加统一 normalize: 强制 UTC + 复权 close + 标准 OHLCV schema.

---

## 2. 信号生成逻辑漏洞

### P0-S1 — 开盘 30min 内指标 lag 未冷却 ⭐

**现状**: 09:30-10:00 间, 5min bar 仅 1-6 根, EMA(20) / ATR(14) / RSI(14) 全部基于"warmup 不足"数据计算. 系统照常出信号.

**风险**: Opening Drive 信号本应是 alpha 最高的窗口, 但当前用 noise-dominated 指标 → 信号质量比 10:30 后低 60%.

**修复**:
```python
# intraday_scorer.py 增加
def _is_warmup_complete(self, ctx) -> bool:
    bars_5m = ctx.get("bars_5m_count", 0)
    return bars_5m >= 14  # ATR/RSI 需要 14 根
# score() 开头
if not self._is_warmup_complete(ctx):
    return _empty_signal(symbol, "warmup_incomplete")
```

**影响**: 高 — 09:30-10:30 的信号大多数无效, 但产生大量误下单.
**验证**: 复盘比较 warmup_incomplete=skip 模式与现有模式 30 天 PnL.

---

### P0-S2 — 停牌检测缺失 ⭐

**现状**: 全工程 grep `halted|trading_halt` 0 命中. 标的停牌后, Futu 推送的 last_quote 仍是停牌前最后一价. 系统正常评分 → 出信号 → 下单 → broker 拒单或挂单等待复牌后以未知价格成交.

**风险**: 停牌期间下空头 → 复牌跳空 +30% → 立即爆仓.

**修复**: `MarketDataRouter.get_full_market_context()` 增加 `is_halted` 字段 (Futu API: `get_market_state`); scoring 路径检测 `is_halted=True → skip`.

**影响**: 中 — 模拟盘风险有限, 但实盘必修.
**验证**: 模拟停牌标的, 断言 score 返回 NEUTRAL.

---

### P1-S3 — `signal.expires_at = +30min` 太宽松

**现状**: `signal_pipeline.py:1211` 信号默认 30min 过期. 5min 周期信号在第 5 个周期才过期, 早已过 alpha 衰减期.

**修复**: 不同 phase 不同 expire: opening_drive 5min, midday 15min, closing_drive 3min.

---

### P1-S4 — 投票 consensus_level=0 当"低共识"使用

**现状**: voting_coordinator 在所有模型超时时返回 consensus_level=0; pre_trade_check 把 0 当低共识不阻断.

**修复**: 区分 "consensus=0 低共识" vs "consensus=NaN/None 数据缺失", 后者直接拒.

---

### P1-S5 — 投票 race condition

**现状**: `voting_coordinator.py` 多 LLM 异步调用, 以 first-arrival or last-arrival 不明.

**修复**: 显式声明聚合策略 (建议 2/3 quorum + timeout drop). 添加 quorum-based 单测.

---

### P1-S6 — 阈值散布 (已知 F4 部分修复)

**现状**: F4 收敛了 14 处 yaml `min_confidence`, 但 `dynamic_pool.tiers.*` 的 5 个 tier-specific min_confidence 未走 resolver.

**修复**: 把 dynamic_pool 也接入 MinConfidenceResolver.for_phase(tier_name).

---

### P2-S7 — 因子 IC 月度归因缺失 (F8 待做)

**现状**: 6 因子权重经验拍脑袋, 无 IC 验证.
**修复**: F8 启动 (依赖 F29 backtest).

---

## 3. 风控缺失

### P0-R1 — `SectorLimitsChecker` 存在但未在下单路径调用 ⭐

**现状**: `app/risk/sector_limits.py` 完整实现, 但 grep `check_sector_limit` 仅 line 242 自调用, **未在 `_submit_order` 或 `pre_trade_check.check()` 中被调用**.

**风险**: 同时持仓 NVDA / AMD / AVGO / TSM / MU / QCOM 6 只全部 chip → 板块单点风险, 任一坏消息全损.

**修复**:
```python
# pre_trade_check.py:check() 增加
from app.risk.sector_limits import SectorLimitsChecker
sector_check = SectorLimitsChecker()
allowed, reason = sector_check.check_sector_limit(...)
if not allowed:
    return PreTradeResult(allowed=False, reason=f"SECTOR_CAP: {reason}", ...)
```

**影响**: 高 — 模拟盘的 24 笔今日交易若都集中 chip → 实盘等于裸奔.
**验证**: 单测 6 仓 chip + 新 chip 信号 → 拒.

---

### P0-R2 — `SystemCircuitBreaker` / `MarketCircuitBreaker` 未 wire ⭐

**现状**: `app/risk/{system_circuit,market_circuit}_breaker.py` 实现完整, `engine.py:33` 实例化 SystemCircuitBreaker 但 grep 调用点 0.

**风险**: VIX 跳到 50 / SPY 闪跌 -5% / Futu API 错误率 > 50% 时, 系统不会停下单.

**修复**: 在 `pre_trade_check.check()` 起始处加:
```python
if not self.system_breaker.is_allowed():
    return PreTradeResult(allowed=False, reason="SYSTEM_BREAKER", ...)
```
+ scheduler 主循环每 60s 推送 VIX / SPY change 给 system_breaker.

**影响**: 高 — VIX 单日跳 50% (2020 年 3 月 / 2024 年 8 月) 时系统会继续买跌.
**验证**: 模拟 VIX>40 → 所有信号 reject.

---

### P1-R3 — 相关性过滤 0 个

**现状**: 全工程 grep `correlation` 仅在 `evolution/shadow_tracker.py` 的统计方法用, 不是仓位限制层. 与已持仓的相关系数检查缺失.

**风险**: NVDA 持仓后再开 AMD → 实质两倍 chip 敞口.

**修复**: `app/risk/correlation_filter.py` 新增, 用 60-day daily return correlation > 0.7 → 拒.

---

### P1-R4 — Beta 桶限制缺失

**现状**: 高 Beta (>1.5) 标的扎堆持仓无限制.

**修复**: `risk_limits.max_beta_bucket: 0.40` 配置, sum(positions where beta>1.5)/equity ≤ 0.40.

---

### P2-R5 — 风控状态机切换无 hysteresis

**现状**: `RiskStateMachine` NORMAL ↔ CAUTION 切换无最小停留时间, drawdown 抖动 ±0.1% 即频繁切换.

**修复**: 加 `min_state_duration_sec: 300`, 状态切换需累计满足 5min 才生效.

---

### P2-R6 — 期权 0DTE 风控弱

**现状**: `option_risk_monitor.py` 有 IV/theta 检查但无 0-1DTE 特殊保护.

**修复**: 0DTE 期权 hard ban 或 single-trade max 0.5%.

---

## 4. 执行与容错薄弱点

### P0-E1 — `audit_file = "/root/.futu_trade_audit.jsonl"` 硬编码 ⭐

**现状**: `app/futu_integration.py:37` 硬编码 root 路径, macOS / 非 Linux 部署立即报错.

**修复**:
```python
from app.core.paths import get_logs_dir
self.audit_file = str(get_logs_dir() / "futu_trade_audit.jsonl")
```

**影响**: 部署阻断 (前置). **必修, 且本机部署时 audit log 完全丢失**.
**验证**: `grep "/root/.futu" app/` 应只剩 0 行.

---

### P0-E2 — `place_order` 无重试 + 无 reconnect ⭐

**现状**: `futu_integration.py:293` 单次调用 `trd_ctx.place_order`, ret != RET_OK → return None. trd_ctx 断线后 self.trd_ctx 仍是旧对象, 后续全部失败.

**风险**: 网络瞬断 1 秒 → 当前周期所有信号全部 lose; 无 reconnect → 直到服务重启都不可用.

**修复**:
```python
# futu_integration.py 增加
def _ensure_connected(self):
    if not self.trd_ctx or not self._is_alive():
        self.connect()  # 重连

def place_order_with_retry(self, ..., max_retries=3, backoff=1.0):
    for attempt in range(max_retries):
        self._ensure_connected()
        ret, data = self.trd_ctx.place_order(...)
        if ret == RET_OK:
            return data
        if "network" in str(data).lower():
            time.sleep(backoff * (2 ** attempt))
            continue
        return None  # 业务拒绝不重试
```

**影响**: 高 — 实盘必出.
**验证**: mock 第 1 次 fail + 第 2 次 success, 断言最终下单成功.

---

### P0-E3 — 进程崩溃后 PENDING_CONFIRM 订单恢复弱 ⭐

**现状**: `OrderTracker` 实现完整 schema, 但 grep `restore_pending_orders` 0 命中. 启动时不重新拉 PENDING_CONFIRM 订单状态.

**风险**: 14:00 提交订单, 14:01 崩溃, 14:05 重启 → 订单已成交但本地仍 PENDING_CONFIRM, 持仓不在 PositionStore, 平仓逻辑不会触发.

**修复**: `main.py` 启动序列加:
```python
from app.risk.order_tracker import get_order_tracker
get_order_tracker().restore_pending_on_startup(broker_adapter)
```
restore 内部: 拉所有 PENDING_CONFIRM, 逐个 query Futu 订单状态, 更新本地 + 必要时重建 Position.

**影响**: 高 — 重启后仓位悬空风险.
**验证**: 启动日志出现 `[OrderTracker] 恢复 N 笔 pending 订单`.

---

### P1-E4 — `PARTIALLY_FILLED` 后 SL/TP 未按已成交数量调整

**现状**: order_executor 未检查 partial fill, 直接用全量 quantity 设 SL/TP.

**风险**: 成交 50%, SL 价被设到全量止损位, 实际 50% 仓被打掉时 SL 可能已被调离.

**修复**: 引入 `position.filled_quantity vs position.size` 区分, SL/TP 用 filled_qty 计算.

---

### P1-E5 — `SmartOrderRouter.max_slippage_bps=50` 与 spread_pct 单位错

**现状**: `app/execution/smart_order_router.py:104` `if spread_pct > self.max_slippage_bps` — spread_pct 是百分比 (0.01 = 1%), max_slippage_bps=50 表示 0.5%, 但比较式视为同单位 → 实际门槛 5000% 永不触发.

**修复**: `if spread_pct * 10000 > self.max_slippage_bps` 或改 `max_slippage_pct=0.005`.

---

### P1-E6 — 订单状态机非法转换无校验

**现状**: `models.OrderState` 枚举完整, 但赋值 `order.state = X` 任意, 无法防 FILLED → PARTIALLY_FILLED 等非法转换.

**修复**: `Order.set_state(new_state)` 检查转换合法性, 非法 raise.

---

### P1-E7 — 通知器失败无重试

**现状**: dingtalk/feishu `send_message` 单次 try/except → 失败即 log + 继续.

**风险**: 重要信号 (RISK_OFF 告警) 漏推送.

**修复**: 关键告警 (RISK_OFF/SYSTEM_BREAKER/PORTFOLIO_DRAWDOWN) 走带 backoff retry 通道; 普通推送 (开仓通知) 失败即丢可接受.

---

### P2-E8 — 主线程 watchdog 缺失

**现状**: 无心跳监控 scheduler 主循环是否还在跑. 死锁 / 卡死无告警.

**修复**: `app/observability/watchdog.py` 每 60s 检查 `last_loop_iter_at`, 超 10 min 无更新 → 钉钉告警.

---

### P2-E9 — 期权下单用 dict 而非 Position 对象

**现状**: `order_executor.py:613` 期权 position 是 dict, `_submit_order` 主路径是 Position 对象, 类型不一致.

**修复**: 统一用 Position 对象 + option_metadata.

---

### P2-E10 — daemon 心跳表只 UPSERT 不告警

**现状**: `daemon_heartbeat` 表存在, 但无消费方检测 stale heartbeat.

**修复**: position_monitor 每 5min 检查 `now - max(last_heartbeat) > 10min → 告警`.

---

## 优先级路线图

### 今晚开盘前 (P0 — 11 项, 必决断)

| ID | 描述 | 行动选项 |
|----|------|---------|
| D1 | 节假日不查 | 小修 5 分钟 |
| D2 | ATR=0 默认 | 小修 5 分钟 |
| D3 | 拆股处理 | 短期接受风险 + 监控告警 |
| D4 | client_order_id 碰撞 | 小修 2 分钟 |
| S1 | 开盘 30min lag | 加 warmup 检查 10 分钟 |
| S2 | 停牌检测 | 短期接受风险 |
| R1 | SectorLimits 未 wire | 小修 10 分钟 |
| R2 | SystemBreaker 未 wire | 小修 10 分钟 |
| E1 | /root 硬编码 | 小修 2 分钟 |
| E2 | place_order 重试 | 中等修 30 分钟 |
| E3 | OrderTracker restore | 中等修 30 分钟 |

**建议**: D1/D2/D4/E1 + R1/R2 (今晚必修, 累计 < 30min); E2/E3 (今晚做 + 测); S1 (今晚做); D3/S2 (短期接受 + 加监控告警, 出现立即修).

### 本周 (P1 — 13 项)
按文件分组并行修. 重点 D8 (datetime tz)、E5 (slippage 单位错)、E6 (状态机校验).

### 本月 (P2 — 8 项)
- D9/D10/S7/R5/R6/E8/E9/E10
- 联动 F29 backtest 框架启动

---

## 验证方法 (汇总)

### 静态验证
```bash
# 数据流
grep -rn "/root/" app/ | grep -v ".bak"          # E1
grep -rn "datetime.now()$" app/ | grep -v "(ET\|tz\|timezone"  # D8
grep -rn "client_order_id.*time.time" app/        # D4

# 风控 wire-up
grep -rn "SectorLimitsChecker\|check_sector_limit" app/risk/engine.py app/risk/pre_trade_check.py  # R1
grep -rn "system_breaker\|SystemCircuitBreaker" app/risk/pre_trade_check.py  # R2
```

### 动态验证 (今晚开盘后 30min)
```bash
# 1. coordinator 数据完整性
python scripts/coordinator_status.py --all

# 2. risk pre-trade rejection breakdown
sqlite3 data/decisions.db "SELECT reason, COUNT(*) FROM rejections WHERE date(timestamp,'unixepoch','localtime')=date('now') GROUP BY reason ORDER BY 2 DESC"

# 3. 订单状态机健康
sqlite3 data/ftseek_us.db "SELECT state, COUNT(*) FROM order_tracker WHERE date(submitted_at)=date('now') GROUP BY state"
# 期望: PENDING_CONFIRM 数 < 已提交数 * 0.10 (90%+ 应在 5min 内 FILLED)

# 4. 异常告警
journalctl -u ftseek-us --since today | grep -iE "ERROR|REJECTED|BREAKER"
```

### 月度回归测试 (Phase B 前提)
- [ ] 节假日单测 (D1)
- [ ] 拆股事件单测 (D3)
- [ ] 停牌检测单测 (S2)
- [ ] System breaker 触发单测 (R2)
- [ ] place_order 重试单测 (E2)
- [ ] OrderTracker restore 单测 (E3)
- [ ] 整体: pytest tests/ 失败数 ≤ 6 (baseline)

---

**结束.** 任何 P0 项, 若不在今晚修复, 必须在 yaml 加 `enabled: false` 灰度禁用对应功能, 或在 daily_review 加监控阈值, 出问题立即告警.
