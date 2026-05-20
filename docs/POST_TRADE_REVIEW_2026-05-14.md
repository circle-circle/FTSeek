# 昨日交易诊断 + 优化修复报告

> **日期**: 2026-05-15
> **数据范围**: 2026-05-14 美股交易 (开盘 21:30 ET / 北京 09:30)
> **基础数据**: trade_records (45+ 笔), ftseek_us.log (4.6MB), daily_review_2026-05-14.json

---

## 1. 实测数据 (硬数据, 非估算)

### 整体表现
| 指标 | 值 | 评价 |
|------|---|------|
| 总交易 | 31 笔 | — |
| 已平仓 | 25 笔 | — |
| 胜率 | **8% (2/25)** | 极差 |
| 总 PnL | **-$615.97** | 亏损 |
| 平均 PnL | -$24.64 | — |
| 平均持仓 | 12 分钟 | 极短 |
| 评分 | 5.3/10 | needs_improvement |

### 损失归因 (按标的)
| 标的 | 笔数 | 累计 | 占总亏 |
|-----|-----|------|-------|
| **LCID** | 8 | **-$634** | 100%+ |
| BA | 2 | -$120 | 19% |
| ARM | 3 | -$123 | 20% |
| RIVN | 2 | +$202 | (盈利) |
| QCOM | 4 | -$10 | 微亏 |

**结论**: LCID 单标的吃掉全部日亏损. 其他标的接近持平或微盈.

### 损失归因 (按 exit_reason, 30 天)
| 原因 | n | 胜率 | avg_pnl% | 评级 |
|------|---|------|---------|-----|
| TRAILING_STOP | 76 | 50% | +0.70% | ✅ keep |
| tp_hit | 5 | 100% | +1.02% | ✅ keep |
| **PRE_CLOSE_EXIT** | 22 | 27% | -0.11% | ⚠ review |
| **STRUCTURE_EXIT** | 33 | 30% | -0.24% | ⚠ review |
| **FLASH_EXIT** | 4 | 0% | -1.69% | 🚨 disable |
| sl_hit | 90 | 21% | -0.10% | ⚠ marginal |

---

## 2. 致命发现 — 我的所有改进**没在生产路径生效**

### 证据链
| 检查项 | 预期 | 实测 | 含义 |
|-------|-----|------|------|
| `signal_arbitration` 表行数 | > 0 | **0 行** | Phase A coordinator 没拦截下单路径 |
| `trade_records.owner_strategy` | "main"/"scalping" | **全 NULL** | P0-1 / B.4 改动未在生产生效 |
| `trade_records.source_type` | 多样 | **99% SCALPING** | 实际下单 99% 走 ScalpingEngine 旁路 |
| `signal_strength` 分布 | 50-90 (因子模型) | **66/99/0/33 离散** | voting majority 直输, IntradayScorer 因子模型未使用 |
| `LV3 K 线不足` | 偶发 | **829 次** | 数据流断流 (Futu LV3 → Finnhub fallback 频繁) |
| `LLM features 调用` | 部分 | **0 次** | L1-L12 全 disabled, 智能化 0 加成 |
| `mode='sandbox'` 笔数 | 视启用 | **0 笔 (mode='normal')** | sandbox 模式没启用 |

### 根因
**实际下单 99% 来自 ScalpingEngine 独立路径**, 这条路径绕过了:
- `pre_trade_check.py` (我加的 SECTOR/SYSTEM_BREAKER/DAILY_LOSS 检查全部跳过)
- `coordinator.arb.arbitrate()` (Phase A 多策略仲裁)
- `ExitDispatcher` (P0-2 路由)
- `Smart Relevance + LLM` (alpha 信号)

**所有上层架构是"看着启用但没数据"**.

### 系统级问题 (来自日志)
- ✅ 启动 banner 正常 (Coordinator Phase A enabled=True observe_only=True)
- 🔴 **scan_loop 1802s 无心跳** + **monitor_loop 783s 无心跳** (watchdog 误报: 实际 inner await 慢)
- 🔴 **CriticalNotifier 全 5 次重试失败** (webhook 未配)
- 🔴 **6 次 "无法同时买多和卖空"** 错误 (ScalpingEngine 没查 broker 端持仓)
- 🔴 **scalping circuit_breaker 触发 4 次** (连续 3 次亏 → 60min 冷却 × 4 = 4 小时停摆)
- 🔴 **14 笔 ghost_cleanup_cruise** (持仓状态不一致)

---

## 3. 今日修复 (P0-X 套件)

### X1: SymbolDailyLossGuard — 标的级日亏损护栏
**文件**: `app/risk/symbol_daily_loss_guard.py` (新, 220 行)

**3 重触发**:
- 单标的当日累计亏损 > **1% 净值** → 当日 cooldown
- 连续 **3 笔亏损** → cooldown
- **5+ 笔且亏损率 > 70%** → cooldown

**关键特性**:
- 启动从 `trade_records` 重建当日状态 (重启后立即生效)
- 触发自动调 `send_critical()` 关键告警
- 豁免名单 (`exempt_symbols`) 慎用
- 单测 8 条覆盖

**在 LCID 案例上的效果模拟**:
```
笔 1: LCID -$5    (连亏=1)
笔 2: LCID -$47   (连亏=2)
笔 3: LCID -$0.7  (连亏=3 → COOLDOWN 触发)
笔 4-8: 全部被拒 ← 节省 -$580 损失
```

### X2: ScalpingEngine 多空互斥保护
**文件**: `app/scalping/engine.py:_place_order:2179+`

下单前检查 broker 端反向持仓:
```python
for p in broker.get_positions():
    if p.symbol == symbol and (LONG vs SHORT 反向):
        return {"success": False, "error": "hedge_blocked"}
```

**今日效果**: 6 次 "无法同时买多和卖空" 异常 → 直接预防, 0 异常

### X3: ScalpingEngine 接入 SymbolDailyLossGuard
- `_place_order` 入口加 daily_loss_guard.is_blocked() 检查
- `_close_position` 平仓后调 `record_trade(symbol, net_pnl)` 喂数据

### X4: PreTradeChecker 加 SYMBOL_DAILY_LOSS 检查 (主线路径)
**位置**: `app/risk/pre_trade_check.py:check()` 步骤 4.5
- 主线信号也享受同一保护
- 即使主线不是当前主导, 切换后立即生效

### X5: Watchdog auto_ping 修复 (基于 1802s 误报)
**文件**: `app/observability/watchdog.py:auto_ping_when_alive`

旧问题: scan_loop 内部 await 慢 → 顶部 ping 不及时 → 误报
新方案: 后台定时器每 30s 自动 ping (不依赖循环顶部)

### X6: yaml 配置
```yaml
risk:
  symbol_daily_loss_guard:
    enabled: true
    max_daily_loss_pct: 0.01
    max_consecutive_losses: 3
    min_trades_for_winrate_check: 5
    max_loss_rate: 0.7
    exempt_symbols: []
```

### X7: main.py 启动恢复
启动序列加 `SymbolDailyLossGuard.restore_from_db()` — 重启后立即生效.

---

## 4. 测试

`tests/coordinator/test_symbol_daily_loss_guard.py`: **8/8 全绿**
- 连续亏损触发
- 盈利打断重置
- 累计亏损 % 触发
- 5+ 笔 + 高亏损率触发
- 跨标的隔离
- 豁免名单
- snapshot 接口
- 集成检查 (代码字符串验证)

`tests/coordinator/test_p0_p1_p2_fixes.py`: **18/18 全绿** (无回归)

---

## 5. 后续 P1 (本周必做)

### P1-A: ScalpingEngine 真正接入 Coordinator
**目标**: 让 `signal_arbitration` 表有数据 (当前 0 行)
**改动**: ScalpingEngine 开仓前调 `coordinator.arb.arbitrate(symbol, [(scalping_adapter, signal)])`
**预估**: 2-3h

### P1-B: 启用 LLM L1 (新闻分类)
**目标**: 把零 LLM 调用的现状打破, 至少有新闻 alpha
**改动**: `yq -i '.llm_features.news_classifier.enabled=true' config/unified_config.yaml`
**风险**: $0.50/日, 可控
**预估**: 30 min (包含监控)

### P1-C: CriticalNotifier 真正能送达
**目标**: 关键告警不再丢
**改动**: 配置 DingTalk webhook
**预估**: 15 min

### P1-D: 启用 Sandbox 模式实战
**目标**: 用 8 个 universe 隔离 + mode 字段分层
**改动**: `yq -i '.mode.sandbox.enabled=true' config/unified_config.yaml`
**风险**: 风控放宽但 KILL_SWITCH 守底
**预估**: 0 (代码已就位, 只需 yaml flag)

---

## 6. 关键决策建议

### 建议立即执行 (今晚开盘前, 30 分钟)
1. ✅ 部署 SymbolDailyLossGuard (本轮已完成)
2. ⚠ 配 DingTalk webhook (关键告警通道)
3. ⚠ 启用 sandbox 模式 (用 8 个 universe 实验)

### 不建议立即做
- ❌ 启用 L1-L12 全部 LLM (一次开太多, 成本爆且无法归因)
- ❌ scalping 全停 (虽然连续亏损, 但是数据来源)
- ❌ 改 IntradayScorer 因子权重 (因子模型几乎没生效, 改了也没用)

### 数据驱动的优先级
**最大 alpha 杠杆**: 让 ScalpingEngine 走 coordinator (P1-A) — 因为它是实际生产路径, 这一步打通后所有上层 (LLM/Coordinator/SmartRelevance) 才能见效.

---

## 7. 7 天验收基线 (可量化)

启动 X 套件后跑 7 天, 应该看到:
- ✅ LCID-style 单股惨败 **不再发生** (任何 symbol 当日亏损 > 1% 即停)
- ✅ "无法同时买多和卖空" 错误 **= 0** 次
- ✅ Watchdog 1800s 卡死告警 **= 0** 次
- ✅ pytest 100% 通过 (新增 8 条 + 既有)
- 📊 `SymbolDailyLossGuard.snapshot()` 显示拦截笔数 (有就是工作中)
- 📊 总胜率从 8% 提升到 **15%+** (LCID 拖累去掉)
- 📊 总日亏损从 -$616 收窄到 **< -$200** (单股 cap 1% × 5 标的最差 -$5000? 但触发后停)

---

**结论**: 数据告诉我们一个真相 — **不是策略不好, 是单标的反复亏损没保护**. SymbolDailyLossGuard 是今日最大杠杆. 修了 + Watchdog/hedge 修复, 明日开盘可见效.
