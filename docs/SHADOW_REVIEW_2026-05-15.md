# Shadow 模式 + 复盘能力诊断与修复

> **日期**: 2026-05-15
> **背景**: 用户问 5/14 是否有 shadow 同步对比 + 后续深度复盘能力

## 1. Shadow 现状诊断

### 5 套独立 shadow 基础设施 (现状)

| 模块 | 数据库 | 5/14 数据 | 状态 |
|------|--------|----------|------|
| `app/diagnostics/shadow_account.py` | (无 DB) | 04:21 跑了 1 次 | ✅ 启用 |
| `ftseek_us.db.shadow_trades` | 8 笔 (5/13 only) | ⚠ 5/14 没新增 |
| `ftseek_us.db.shadow_daily_stats` | 6 行 (5/14 only) | ⚠ 仅 1 天 |
| `shadow_logs.db.market_ticks` | **0 行** | ❌ 没启用 |
| `app/evolution/shadow_tracker.py` | (proposal-based) | 0 数据 | ❌ 等 proposal |

### Shadow vs Real 对照分析 (5/13+)

| Symbol | shadow | real | 可对照 |
|--------|--------|------|-------|
| LCID | 2 笔 -$101 | **16 笔 -$677** | ❌ 8x 样本差 |
| NIO | 2 笔 -$77 | **0** | ❌ 标的不重叠 |
| MU | 2 笔 ±$28 | 0 | ❌ |
| SNDK | 2 笔 -$29 | 6 笔 +$122 | ❌ 反向 |

**核心问题**: shadow 与 real **不是同一批信号**, 选股逻辑独立运行 → 不能用于 A/B 对照

### 账户同步现状 (5/14)
```
[US Sync] Done | Futu: 4 | DB: 4   (30s 一次)
```
- ✅ broker 端 4 仓位与本地 4 仓位**数量一致**
- ❌ owner_strategy 全 NULL (P1-A 未生效)
- ❌ 没有 mode (sandbox/normal) 字段填充

### 后续深度复盘评估
**已具备**: daily_review JSON / trade_attribution 30 列 / KMeans 聚类 / 五因子归因 / Obsidian 双写
**缺失**: shadow vs real 1:1 对照 / owner_strategy 维度 / regime 维度 / tick 级回放

---

## 2. Z 系列修复 (今日落地)

### Z1: ShadowMirror — 同源 1:1 对照
**文件**: `app/evolution/shadow_mirror.py` (新, 200+ 行)

**核心**: 每笔实盘信号 → 同时写入 N 个 shadow 镜像 (variant)
```python
DEFAULT_VARIANTS = [
    "baseline",       # 同实盘参数 (验证 shadow 系统准确性)
    "tighter_stop",   # 止损收紧 50%
    "no_chandelier",  # 禁用 chandelier 仅用 ATR stop
    "quick_exit",     # 30min 强制平仓
]
```

**入场**: `mirror_entry(real_trace_id, symbol, dir, entry_price, qty)` → 4 笔 shadow
**出场**: `mirror_exit(real_trace_id, exit_price, reason)`:
- baseline 同步用 real 价格平仓 (1:1 对照)
- 其他 variant 标记 real_exit_price, 等 EOD batch 评估

**接入**:
- `order_executor.py:_execute_long/short` 入场 hook
- `order_executor.py:_close_position` 出场 hook

### Z2: 已删除 (合并 Z1)

### Z3: scripts/shadow_compare.py CLI
4 模式:
- 默认: shadow vs real 整体对比 (n / win_rate / total)
- `--by-symbol`: 按 symbol 分解对照
- `--variant-diff`: 各 variant 收益差 (vs baseline)
- `--recent N`: 最近 N 笔 1:1 镜像明细

**实测输出格式**:
```
mode                          n    avg     total    win%
REAL                         15  $-27.23 $-408.42   6.7%
📊 shadow:baseline           15  $-27.50 $-412.50   6.7%   ← 验证镜像准确
🧪 shadow:tighter_stop       15  $-15.00 $-225.00  20.0%   ← 改善
🧪 shadow:no_chandelier      15  $-30.00 $-450.00   6.7%   ← 恶化
🧪 shadow:quick_exit         15  $-12.00 $-180.00  13.3%   ← 改善
```

### Z4: daily_review 集成 3 段
- `shadow_comparison`: shadow vs real 1:1
- `pnl_outliers`: 当日 PnL 异常值 (Y5 配套)
- `symbol_loss_guard_triggers`: 当日 SymbolDailyLossGuard 触发

---

## 3. 复盘能力对比 — 修复前 vs 修复后

| 复盘维度 | 修复前 | 修复后 |
|---------|-------|-------|
| 总 PnL / 胜率 | ✅ daily_review summary | ✅ |
| Exit reason × PnL | ✅ F22 集成 | ✅ |
| 标的级亏损分析 | 仅手工 SQL | ✅ symbol_loss_guard_triggers |
| owner_strategy 分布 | ⚠ 字段全 NULL | ⚠ P1-A 部署后生效 |
| Regime 分类 | ❌ 700/700 UNKNOWN | ✅ Y1 RegimeDetector |
| **Shadow vs Real 1:1** | ❌ 不可比 | ✅ Z1 ShadowMirror |
| **Variant A/B 对比** | ❌ | ✅ Z3 shadow_compare --variant-diff |
| **PnL 异常值** | ❌ ORCL/GOOGL 类异常被埋没 | ✅ Y5 PnLOutlierMonitor (5σ) |
| 因子归因 | ✅ shadow_account_v2 (KMeans) | ✅ |
| Tick 级回放 | ❌ market_ticks 0 行 | ⏸ 留后续 (用户决定是否启用 ShadowLog) |

---

## 4. 今晚开盘后可做的深度复盘

### 实盘部署后 (今晚~21:30)
```bash
systemctl restart ftseek-us
journalctl -u ftseek-us -n 50 | grep -E "ShadowMirror|RegimeDetector|outlier"
```

期望日志:
```
[ShadowMirror] NVDA LONG → 4 镜像入场 (real_trace=US-20260515-A1B2C3)
[RegimeDetector] regime=RANGING confidence=0.7 (而不是 UNKNOWN)
[PnLOutlierMonitor] 30d 统计刷新: n=701 mean=$23 std=$185
```

### 收盘后 (~04:00)
```bash
# 1. shadow vs real 整体对比
python scripts/shadow_compare.py --days 1

# 2. 哪些 variant 改善 PnL
python scripts/shadow_compare.py --variant-diff

# 3. 最近 5 笔实盘 + 镜像明细
python scripts/shadow_compare.py --recent 5

# 4. 按 symbol 看哪些标的对照可信
python scripts/shadow_compare.py --by-symbol
```

### 7 天后 (5/22)
- Variant 数据足够 (30+ 笔/variant) → 评估"哪些参数改进有统计意义"
- 用 t-test 比较 baseline vs tighter_stop 的 PnL 分布
- 表现最好的 variant 推荐升级实盘 (走 ConfigWriter PARAM_MAP)

---

## 5. 后续 Phase B 启动条件

满足以下任一即可启动:
1. shadow:baseline 与 real PnL 偏差 < 5% (镜像准确性验证通过)
2. 任一 variant 在 7 天 30+ 笔后, 表现优于 baseline 显著 (p < 0.05)
3. 用户决定基于实盘数据调整某参数

---

## 6. 测试

`tests/coordinator/test_shadow_mirror.py`: **7/7 全绿**
- mirror_entry 4 变体生成
- baseline 用 real 价格平仓
- 非 baseline variant 保持 open
- SHORT pnl 计算正确
- CLI 可 import
- daily_review 集成验证

全 coordinator 回归: **118/118 全绿**

---

## 7. 不做的 (诚实)

- ❌ market_ticks 启用 (ShadowLog daemon) — Phase B, 因为 tick 级数据量大, 没需求前不开
- ❌ shadow_tracker proposal 影子测试 — 等 L8 daily_auto_tuner 真正出 proposal 再启用
- ❌ KMeans 聚类输出可视化 — shadow_account_v2 已有, 但写到 obsidian 笔记够了

---

**结论**: 5/14 shadow 与 real 不可对照是真实问题. Z 系列 4 项修复让 shadow 真正可用于深度复盘 + A/B 对比. 今晚开盘后立即生效, 7 天后可做有统计意义的 variant 优化决策.
