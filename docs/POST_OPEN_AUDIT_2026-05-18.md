# 5/18 美股交易日全链路深度核查 (2026-05-18 → 5/19 04:00 ET 收盘)

> **结论**: ⚠️ **6 个 P0 / P1 问题需立即处理**, 系统在开盘后 30 分钟出现仓位状态机紊乱, 13 笔 scalping 全部摩擦成本亏损, D-DAY 改造的 LLM 链路 **0 次触发**.
> **业绩**: 15 笔 / -$265 / 0% 胜率 / 12 亏损 + 3 stale_cleanup
> **告警**: 19 ERROR + 3093 WARNING (主因: PROTECTED 跳过 969 次 + Router K线不足 ~1000 次 + Scalping 冷却重复 421 次)

---

## 1. 业绩与战损全景

| 维度 | 值 |
|---|---|
| 总笔数 | 15 (12 closed loss + 3 stale_cleanup) |
| 总盈亏 | **-$265.22** |
| 胜率 | **0%** |
| 最大单笔亏损 | US.TME LONG **-$153** (持仓 86min, ATR 止损) |
| 仍持仓 | 1 (US.QQQ LONG 500 @ $624 — 跨日盈利仓位) |
| 触发熔断 | **3 次** (22:34 / 00:22 / 02:20 三次连续亏损 3 次) |
| 组合止损 | **WARN×23 / REDUCE_50×3 / FLAT_ALL×7** |
| 决策被拒 | 25 (13 SHORT_STRENGTH_WEAK + 12 PRE_TRADE_CHECK) |

---

## 2. P0 问题: 仓位状态机紊乱 (开盘 30 min 内 ARM 仓位反复腾挪)

### 2.1 ARM 仓位 size 振荡 (13 min 内)
```
22:02:00  54 shares @ cost=$203.53           初始 (legacy)
22:02:12  SELL 27 → 27 shares @ $210.69      partial close (REDUCE_50)
22:02:24  sync: 27 shares cost=$216.11       ★ 错误覆盖 cost ★
22:02:34  PORTFOLIO_DRAWDOWN_HARD_STOP -5.2% ← 因为 cost=$216 vs price=$210 触发
22:02:54  sync: ghost 清理 → 0 shares
22:03:25  sync: 从 DB 恢复 → 27 shares       ← 撤销了刚才的平仓
22:03:55  sync: 27 → 54 shares cost=$203.53  ← 完全回滚
22:04:26  sync: 54 → 81 shares               ← 反向开仓堆积
22:14:48  BUY 40 → 41 (反向 cover) PnL=-2.6%
22:15:05  BUY 20 → 21 (再 cover)  PnL=-2.6%
22:15:07  sync: 21 → 41 shares              ← 又回滚
```

### 2.2 根因 (3 个互锁 BUG)

**BUG-A**: `position_sync` 在 partial_close 后写入了**错误的 avg_entry_price**
- 22:02:24 写入 cost=$216.11 (该价格其实是当时市价, 不是真正成本基础)
- exit_monitor 用此 cost 计算组合止损: `(price - cost) / cost = (210.69 - 216.11) / 216.11 = -2.5%` 触发 REDUCE_50
- 真实 cost=$203.53, ARM 实际是 +3% 浮赢, **不应该触发任何 portfolio drawdown**

**BUG-B**: `position_sync` 把已平仓的仓位**从 DB 反向恢复**到内存
- broker 异步确认延迟 5s → sync 检查时 broker 还没确认 → 把 DB 的旧持仓写回内存
- 触发新一轮 PORTFOLIO_DRAWDOWN → 再下平仓单 → 又触发 sync → 死循环

**BUG-C**: `PortfolioDrawdown` 阈值在 **legacy/PROTECTED 仓位** 上空触发
- `_check_portfolio_drawdown` 只过滤了 `MANAGED` (line 271 of `position_exit_monitor.py`)
- 但 PROTECTED ARM 仍在 `positions` 列表中, 被加进 total_pnl/total_cost 计算
- 实际上 22:00 前的 `[退出监控·M1·跳过] US.ARM source=legacy protection=protected` 也证实 ARM 是 legacy 但仍在评估
- 看代码: `is_strategy_managed` 返回 True 仅 MANAGED, 但 ARM 在闭包里被分类为 legacy → MANAGED 默认 (因为 `default_for(legacy)` 可能返回 MANAGED)

### 2.3 关键代码定位

`app/risk/position_exit_monitor.py:271-275`:
```python
managed_positions = [p for p in positions if is_strategy_managed(p)]
portfolio_action = self._check_portfolio_drawdown(managed_positions, get_price_fn)
```

需查 `app/positions/source.py:get_position_source(legacy_arm)` 返回什么, 以及 `default_for(LEGACY)` 是否是 MANAGED 还是 PROTECTED.

`app/risk/position_exit_monitor.py:213-218`:
```python
total_pnl += (price - p.avg_entry_price) * getattr(p, 'size', 0)
```

`p.avg_entry_price` 来源于 `open_positions` DB. 需追查 partial_close 流程为何写入 cost=$216.11.

---

## 3. P0: D-DAY 改造的 LLM 链路 0 次触发

### 3.1 现象
- `data/llm_audit.db` 最后一条记录 = **2026-05-17 07:22:29** (D-DAY 测试时写入), 5/18 全天 0 条
- `PostTradeHook stats: {submitted: 0, completed: 0, failed: 0}` × 4 次 shutdown 全部为 0
- 全文无 `model_call`/`call_llm`/`thesis_inferer` 触发日志
- 15 笔 trade close 全部没触发 thesis 推理

### 3.2 根因
PostTradeHook 在 `main.py` 注册了, 但 `order_executor` 的 `on_position_closed` 钩子链路未调用 `post_trade_hook.dispatch()`. 需查:
- `app/pipeline/order_executor.py` 是否调用 `post_trade_hook.on_position_closed()`
- `main.py` 的 PostTradeHook 实例是否正确注入到 order_executor

### 3.3 影响
- **D-DAY 改造完全没生效**: model_call.py + 4 模型路由 + cache_on_fail + cross_check 全部空跑
- 周一开盘前的 premarket_planner 也没触发
- `qwen3.6-plus` 实际只在 04:38-04:42 收盘后 daily_review 链路被调用, 走的还是老 agent_bridge

---

## 4. P1: Scalping 策略**100% 摩擦成本亏损**

### 4.1 数据
13 笔 scalping 全亏, 但 gross PnL 几乎都是 0.00% ~ -0.09%, **真正吞噬利润的是摩擦成本**:

| Symbol | Gross PnL | Net PnL | 摩擦 | 持仓时长 | 真实亏损来源 |
|---|---|---|---|---|---|
| LCID | 0.00% | **-0.09%** | $17.99 | 2min | 摩擦 |
| TME | -0.06% | -0.11% | $11.58 | 5min | 摩擦+小亏 |
| LCID | -0.09% | -0.18% | $17.92 | 3min | 摩擦+ATR 噪声 |
| TME | -0.77% | — | — | 86min | **真实亏损 (持仓过夜熔断)** |
| ... | | | | | |

### 4.2 根因
- 低价股 (LCID $5.75, TME $9.08) 的 round-trip 摩擦 ≈ 0.09% — 单笔 PnL 必须 > 0.09% 才能盈利
- TP/SL 设置过紧 (TP=$5.97, SL=$5.61, range 0.4% / -2.4%) — TP 几乎不可能命中
- "提前止损 (持仓 2min, 亏损 0.0%)" 时间触发 → 全部按"亏损"计入 → 触发 CB 熔断

### 4.3 熔断逻辑反人类
`scalping/circuit_breaker.py`: 连续 3 次 sl_hit (即使每次 -0.0%) → 60 min 冷却.
今天 3 次熔断 = 9 笔 sl_hit, **每次"亏损"金额 < $20 但累计触发熔断浪费交易窗口**.

---

## 5. P1: 服务在交易时段中途重启

### 5.1 时间表
```
19:47:38  shutdown 开始
19:48:09  fresh start                   (开盘前 1.5h)
19:48:40  shutdown
19:49:19  fresh start                   (1 min 后又重启)
20:06:45  shutdown
22:28:04  shutdown                      ← ★ 美股交易中! (10:28 ET, 开盘后 58 min) ★
```

### 5.2 影响
- 22:28 重启**正好在 ARM 平仓混乱后**, 很可能是手动干预但 grace shutdown 不彻底
- LLM 缓存 + position_state 在此期间可能未完整持久化
- post_trade_hook 在重启间隙的 close 事件全部丢失

---

## 6. P1: AgentBridge LLM 全部超时 (qwen3.6-plus 120s × 3 retry 全失败)

```
2026-05-19 04:38:07  attempt 1 timeout
2026-05-19 04:40:13  retry 2/2 timeout
2026-05-19 04:42:24  ERROR 全部 3 次失败
```

- 时间是 04:42 ET (盘后) — daily_review LLM
- 走的是老 `agent_bridge` 路径 (不是 model_call.py)
- 用的是 D-DAY 配的新模型 `qwen3.6-plus`, 但用旧路径调用 → 没有 fallback / cache_on_fail
- 整个 daily_review LLM 链路在 6 分钟超时空转后 fallback 到 hermes_processor 的 round-cap fallback

---

## 7. P2: Router LV3 K 线数据不足 (~1000 次/天)

| Symbol | 不足次数 |
|---|---|
| US.CRM | 254 |
| US.QCOM | 196 |
| US.RIVN | 158 |
| US.ARM | 103 |
| US.ORCL/NFLX/LCID/SNDK | ~56 each |
| US.QQQ | 35 (盘后还在响) |

每 3 min 报 1 次, 持续到收盘后 6:13 仍在告警. Router fallback 应该自动切到 Yahoo/Alpha Vantage, 但日志显示 fallback 也没数据 → 实际信号扫描可能基于不完整数据.

---

## 8. 修复优先级

### P0 (周二开盘前必修)

| # | 问题 | 修复方案 |
|---|---|---|
| 1 | ARM 仓位 cost 被错误覆盖 ($216.11) | 查 `app/storage/trade_store.py` partial_close 路径, 确保 `avg_entry_price` 永远是首次入场加权均价 |
| 2 | sync 从 DB 反向恢复刚平仓的位置 | 加 broker_filled_at 时间戳, sync 在 fill 事件后 30s 内不允许 DB 反向恢复 |
| 3 | PortfolioDrawdown 在 PROTECTED 仓位上误触发 | 把 LEGACY 默认从 MANAGED 改为 PROTECTED; PortfolioDrawdown 函数内显式过滤 PROTECTED |
| 4 | PostTradeHook 0 次触发 | 在 `order_executor.py` 的 `_close_position` 路径加 `post_trade_hook.on_position_closed()` 调用并 unit-test |

### P1 (本周内修)

| # | 问题 | 修复方案 |
|---|---|---|
| 5 | Scalping 摩擦吞利 | 加摩擦成本预算: 单笔预期 PnL < 2× 摩擦时直接拒绝下单 |
| 6 | 熔断把 -0.0% 也算 sl_hit | 加门槛: gross PnL > -0.1% 不计入 consecutive_losses |
| 7 | 22:28 服务重启原因 | 查 systemctl journal, 确认是 OOM / manual / 别的 |
| 8 | LLM 120s × 3 全超时 | 把 daily_review 从 agent_bridge 迁到 model_call (本批 STAGE_2 跳过的, 原因: 走 agent_bridge subprocess 链路). 参考 `STAGE_2_BATCH2_2026-05-17.md` 第 8 节 |

### P2 (跟踪)

| # | 问题 | 修复方案 |
|---|---|---|
| 9 | LV3 K 线数据不足 | 查富途 LV3 订阅状态; 确认 fallback 到 Yahoo 是否生效 |

---

## 9. 验证 BUG-A 的最简实验

```bash
sqlite3 data/ftseek_us.db "
SELECT symbol, avg_entry_price, size, updated_at,
       json_extract(action_log_json, '$[#-1].action') as last_action
FROM open_positions WHERE symbol LIKE '%ARM%';"
# 期待: 如果 ARM 还在, avg_entry_price 应该 = $203.53 (LEGACY 真实 cost)
# 实际: 已平仓所以现在不存在, 但可查 trade_records 还原
```

```bash
# 检查 partial_close 时的 cost 写入路径
grep -n "avg_entry_price\|avg_cost\|partial_close" app/storage/trade_store.py
grep -n "update_position\|partial_close" app/positions/store.py
```

---

## 10. 与 D-DAY 验证报告的对比

D-DAY 报告 (`docs/D_DAY_AUDIT_2026-05-17.md`) 在 12 维度全绿:
- D7: 盘中热路径 0 LLM ← ✅ 验证通过
- D8: PostTradeHook 集成 ← ✅ 当时通过, **实际生产 0 触发**
- D2: 213/213 测试 ← ✅ 但**测试覆盖 PostTradeHook → order_executor 的注入链路不充分**

**教训**: pytest 213/213 不等于生产 wired-up. 需加端到端测试: 模拟 close trade → 断言 PostTradeHook stats++.

---

## 11. 一句话总结

> 周一开盘 30 min 内仓位状态机三个 BUG 互锁互催, 引发 ARM 仓位 LONG↔SHORT 反复腾挪 + 7 次 FLAT_ALL 误触发. Scalping 13 笔被摩擦成本吃干净 + 3 次熔断浪费交易时段. **D-DAY 改造的 LLM 链路完全没有触发**: 15 笔 trade 0 次 thesis 推理, 0 次 model_call. 表面上系统按设计运行 (没崩, 钉钉告警发了), 但内部状态污染 + LLM 0 触发等于"假活". 周二前必须修 P0-1/2/3/4.
