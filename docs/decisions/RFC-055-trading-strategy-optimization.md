# RFC-055: 止损止盈策略数据驱动优化

> **服务焦点**: F1 (买卖规范与止损止盈策略)
> **改什么指标**: 胜率 30.5% → 50%+, 日均收益 -$1650 → +$500+
> **验证方式**: SQL 查询 trade_records, T+1 生效, 7 天后首次评估
> **最坏情况**: 参数回调不影响主线运行, 无新增亏损风险
> **回滚**: `git revert HEAD`

## 1. 数据诊断 (基于 319 笔交易全量分析)

### 1.1 盈利核心已验证
- **主线股票**: 151 笔, 39.7% 胜率, **+$17,299**, 日均 +$786 ✅
- **SHORT 方向**: 84 笔, **42.9% 胜率**, +$13,054 ← 系统优势所在
- **Trailing Stop 退出**: 47.3% 胜率, +$7,221 ← 唯一稳定盈利退出方式

### 1.2 五大出血点
| # | 出血点 | 数据证据 | 影响 |
|---|--------|----------|------|
| 1 | < 5min 快进快出 | 162 笔, 19.1% WR, **-$10,689** | 最大单一亏损源 |
| 2 | LONG 方向 | 72 笔, 33.3% WR | 拉低整体胜率 |
| 3 | > 6hr 持仓 | 29 笔, **-$17,329** | 隔夜风险侵蚀 |
| 4 | PRE_CLOSE 过早强平 | 占退出 54% | 截断盈利发展空间 |
| 5 | 持续亏损标的 | DIA 0%, NVDA 0%, QCOM 16.7% | 反复亏损 |

### 1.3 盈利甜蜜区
- **持仓 30min-6hr**: +$18,164 (系统全部正利润来源)
- **SHORT + Trailing Stop**: 42.9% + 47.3% = 核心 edge
- **ORCL/BWXT/SNDK/RIVN**: 高胜率标的

## 1.5 关键发现: SHORT vs LONG Trailing Stop 对比

| 方向 | Trailing Stop WR | SL Hit WR | 结论 |
|------|------------------|-----------|------|
| **SHORT** | **55.8%**, +$4,633 | 23.1% | 核心优势 |
| LONG | 38.1%, -$571 | **6.9%** | LONG 止损几乎全亏 |

## 1.6 风险收益比分析 (按持仓时间)

| 时段 | Avg Win | Avg Loss | R/R | 诊断 |
|------|---------|----------|-----|------|
| <5min | $150 | $34 | **4.46:1** | 快赢小亏, 但 WR 低 |
| 5-20min | $162 | $108 | 1.5:1 | 可接受 |
| 20-60min | $156 | $143 | **1.09:1** | ⚠️ 几乎 1:1, 需 50%+ WR |
| 1-6hr | $143 | $121 | 1.18:1 | ⚠️ 损失接近盈利 |
| >6hr | $378 | $140 | 2.69:1 | R/R 好但 WR 仅 27% + 尾部灾难 |

**根因**: 赢的金额恒定 ~$150 (止盈太紧), 亏的金额随持仓时间增长 (止损不够紧)

## 1.7 方向×退出 盈亏矩阵 (排除黑名单/scalping/options 后)

| 组合 | 交易数 | 胜率 | 总PnL | 判定 |
|------|--------|------|-------|------|
| **SHORT + TRAILING_STOP** | 36 | **63.9%** | **+$5,891** | 🏆 核心优势 |
| LONG + PRE_CLOSE_EXIT | 9 | **55.6%** | +$5,200 | ✅ 日内锁利有效 |
| LONG + TRAILING_STOP | 27 | 40.7% | +$2,086 | ✅ 可用 |
| SHORT + tp_hit | 4 | 100% | +$611 | ✅ 样本少 |
| SHORT + STRUCTURE_EXIT | 16 | 43.8% | -$537 | ⚠️ QCOM拖累 |
| SHORT + sl_hit | 48 | 22.9% | -$539 | ⚠️ 必要成本 |
| LONG + sl_hit | 15 | 13.3% | -$230 | ⚠️ 必要成本 |

**核心发现**: SHORT + Trailing Stop = **63.9% 胜率** (排除黑名单后), 这是系统唯一需要聚焦的 edge

## 2. 优化方案 (3 阶段)

### Phase 1: 止血 — 消灭 <5min 噪声交易 (🟡 配置调优)

**改动 1**: 最小持仓时间 10min → 20min
- 文件: `app/risk/exit_policy.yaml` → `exit_monitor.min_hold_minutes`
- 依据: < 5min 交易 -$10,689, 19.1% WR; 20min 以下只保留硬止损

**改动 2**: 重新启用 Time Stop, 限制最大持仓 6hr
- 文件: `app/risk/exit_policy.yaml` → `exit_rules.time_stop`
- 原值: enabled=false (O3 禁用因 sharpe=-0.40)
- 新值: enabled=true, max_holding_hours=6, 盈利≥1.0ATR 可豁免
- 依据: > 6hr 持仓 -$17,329; 甜蜜区是 30min-6hr

**改动 3**: 时间衰减加速 (150min → 90min)
- 文件: `app/risk/exit_policy.yaml` → `day_trade_mode.time_decay.max_idle_minutes`
- 依据: 2.5hr 无进展的信号已失效, 缩短到 90min 加速资金周转

**改动 4**: 全局黑名单扩充
- 文件: `config/unified_config.yaml` → `risk.global_blacklist`
- 新增: US.DIA (0% WR), US.NVDA (0% WR, 已 disabled 但不在黑名单)

**改动 5**: LONG 方向信心阈值收紧
- 文件: `config/unified_config.yaml` → `strategies.defaults.direction_bias.LONG.min_confidence`
- 原值: 0.75 → 新值: 0.80
- 依据: LONG 33.3% WR vs SHORT 42.9% WR, 需要更高确认度

### Phase 2: 提效 — 让盈利充分发展 (🔴 需审批)

**改动 6**: Pre-Close 退出窗口收窄 (15min → 8min)
- 文件: `app/risk/exit_policy.yaml` → `exit_rules.pre_close_exit.window_minutes`
- 依据: 54% 仓位被 PRE_CLOSE 强平, 利润发展空间不足
- 同时调整分层: 盈利≥0.5ATR (原1.0ATR) 即可过夜

**改动 7**: Chandelier 追踪止损收紧 (更快锁利)
- 文件: `app/risk/exit_policy.yaml` → `exit_rules.chandelier_trail.activation_profit_atr`
- 原值: 0.8 → 新值: 0.6 (更早激活追踪)
- 依据: Trailing Stop 是唯一盈利退出, 更早激活 = 更多锁利

**改动 8**: Scale Out 简化 (4层→2层, 减少过早减仓)
- 文件: `app/risk/exit_policy.yaml` → `exit_rules.scale_out.atr_tiers`
- 原: 0.5/1.0/2.0/3.0 ATR 四层
- 新: 1.5ATR 平30%, 3.0ATR 平70%+trailing
- 依据: 过早减仓(0.5ATR)截断了盈利发展, 集中到利润确认点

### Phase 3: 增强 — 信号质量优化 (🔴 需审批)

**改动 9**: 方向感知入场 (SHORT 优先)
- 文件: `config/unified_config.yaml` → `portfolio.net_exposure_target`
- 调整各 regime 下 net exposure 偏 SHORT
- trending_down: -0.2 → -0.4 (更积极做空)
- range: 0.0 → -0.15 (震荡也偏空)

**改动 10**: 最大同时持仓收紧 (5→3)
- 文件: `config/unified_config.yaml` → `scheduler.max_positions`
- 依据: 集中火力在最优信号, 避免分散

## 3. 预期效果量化

| 指标 | 当前值 | Phase 1 后 | Phase 2 后 | Phase 3 后 |
|------|--------|-----------|-----------|-----------|
| 胜率 | 30.5% | ~38% | ~42% | ~48% |
| 日均收益 | -$1,650 | ~$400 | ~$600 | ~$800 |
| 主线胜率 | 39.7% | ~44% | ~47% | ~50% |

### 推算逻辑:
- Phase 1 消灭 <5min 交易: 去掉 162 笔 19.1% WR 的亏损, 胜率自然提升
- Phase 2 让利润发展: Trailing Stop 触发率提升, 平均盈利增大
- Phase 3 SHORT 优先: 利用 42.9% 的方向优势

## 4. 验证计划

```sql
-- T+1 验证: 昨日交易
SELECT COUNT(*) n, ROUND(SUM(realized_pnl),0) pnl,
  ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) wr,
  ROUND(AVG(CAST((julianday(exit_time)-julianday(entry_time))*1440 AS INT)),0) avg_hold_min
FROM trade_records
WHERE date(entry_time) = date('now','-1 day') AND realized_pnl IS NOT NULL;

-- 7 天验证
SELECT COUNT(*) n, ROUND(SUM(realized_pnl),0) pnl,
  ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) wr
FROM trade_records
WHERE date(entry_time) >= date('now','-7 days') AND realized_pnl IS NOT NULL;

-- 持仓时间分布
SELECT CASE
  WHEN julianday(exit_time)-julianday(entry_time) < 20.0/1440 THEN '<20min'
  WHEN julianday(exit_time)-julianday(entry_time) < 6.0/24 THEN '20min-6hr'
  ELSE '>6hr' END as bucket,
  COUNT(*), ROUND(AVG(realized_pnl),2)
FROM trade_records WHERE realized_pnl IS NOT NULL
  AND date(entry_time) >= date('now','-7 days')
GROUP BY bucket;
```

## 6. 实施状态 (2026-06-01)

| Phase | 改动数 | 状态 | Commit |
|-------|--------|------|--------|
| Phase 1 (止血) | 5 | ✅ 已实施 + 已重启 | 454c9801 |
| Phase 2 (提效) | 3 | ✅ 已实施 + 已重启 | e275bad9 |
| Phase 3 (增强) | 3 | ✅ 已实施 + 已重启 | e275bad9 |
| 验证脚本 | 1 | ✅ scripts/verify_rfc055.py | 26efbae2 |

### ADR-002 后 (5/26+) 早期验证数据

5 笔真实交易 (排除 scalping cleanup):
- **60% 胜率**, +$1,706 总 PnL
- 所有退出均为 TRAILING_STOP
- 持仓时间: 54min - 416min (甜蜜区内)
- 方向: 4 SHORT + 1 LONG (SHORT 为主)

### 下一步研究计划 (T+7 评估)

1. 验证 Phase 1-3 改动是否生效 (检查 <20min 交易占比, >6hr 交易)
2. 评估 Chandelier 0.6ATR 激活后的 trailing stop 触发率变化
3. 分析 Scale Out 2层 vs 4层对平均盈利金额的影响
4. 检查 LONG min_confidence 0.85 是否过度收紧 (交易数是否骤降)
5. 如数据显示进一步优化空间, 提出 Phase 4 方案

| 文件 | 级别 | 改动内容 |
|------|------|----------|
| `app/risk/exit_policy.yaml` | 🟡 | min_hold, time_stop, time_decay, pre_close, chandelier, scale_out |
| `config/unified_config.yaml` | 🟡/🔴 | blacklist, LONG threshold, net_exposure, max_positions |
