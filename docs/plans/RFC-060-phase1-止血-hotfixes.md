# RFC-060 Phase 1: 止血（Hotfixes）

> **阶段目标**: 让系统不再出血 + 让模拟盘说真话。**Gate 1→2 的硬前提**。
> **预计周期**: 3-5 天（本周内完成部署）
> **总纲**: 见 `RFC-060-TRADING-OPTIMIZATION-PROGRAM.md`
> **回滚**: 每个 Item 独立可 `git revert`，互不影响。

---

## 阶段总览

| ID | Item | 杠杆 | 成本 | 现状结论 | 状态 |
|----|------|------|------|---------|------|
| P1.1 | 模拟盘成交模型加滑点/延迟/部分成交 | 🔴🔴🔴 | 中 | 零滑点（fill_price=order.price）| ⬜ |
| P1.2 | Kill Switch 全链路 fail-close | 🔴 | 低 | 两处 fail-open | ⬜ |
| P1.3 | VIX 阈值动态化 + 真实降仓 | 🔴🔴 | 低 | VIX>28 只 log 不降仓（假降仓 bug）| ⬜ |
| P1.4 | 修 `get_plan` 重复定义 | 🟡 | 极低 | 降级逻辑被覆盖成死代码 | ⬜ |
| P1.5 | IC 门禁接统计显著性 + 多维度 | 🔴 | 中 | 无 p 值、单一全局阈值 | ⬜ |

> 完成顺序建议：**P1.4（极低，先清死代码）→ P1.2（低）→ P1.3（低）→ P1.5（中）→ P1.1（中，改动最大放最后）**

---

## P1.1 修复模拟盘成交模型（加滑点/延迟/部分成交）

### 背景
`docs/PM_RISK_AUDIT_2026-06-17.md` 怀疑「评分函数 look-ahead bias / IC 与实盘收益相关性破裂」。**模拟盘零滑点本身就是一种系统性偏差**，足以解释"IC 看起来正（0.1685）、实际亏钱"。这是**提升实盘盈利最直接的一步**——会让你发现某些"盈利"策略在真实滑点下其实是亏损的。

### 精确现状
**文件**: `app/execution/engine.py`

| 行号 | 当前行为 |
|------|---------|
| `engine.py:441-479` | 模拟盘（无 broker）走"完整链路模式"：SENT→ACKED→FILLED |
| `engine.py:468` | `order.fill_price = order.price`（在线 broker 仿真） |
| `engine.py:477-478` | `order.fill_price = order.price; order.filled_quantity = order.quantity`（离线完整链路） |
| `engine.py:417` | 实盘无 broker 已正确 REJECT（P0 2026-06-13 修复），但**有 broker 仿真路径仍零滑点** |

**关键发现**：config 旋钮已存在（`simulate_realistic_flow` / `simulate_fill_delay_seconds` / `simulate_partial_fill`），但**只模拟状态机转换，不模拟价格滑点**。`fill_price` 永远等于 `order.price`。

**可复用的现成资产**: `app/execution/smart_order_router.py:35` 已有 `SmartOrderRouter`，含 `max_slippage_bps`、五档盘口 spread/midprice 滑点计算、滑点超限拒绝逻辑。

### 目标方案
在模拟盘 `FILLED` 前，对 `fill_price` 施加**现实滑点模型**：

```python
# engine.py 模拟成交分支，FILLED 赋值前
def _apply_sim_slippage(self, order) -> float:
    """模拟盘滑点: 限价单按盘口吃单深度估算, 市价单加方向性滑点"""
    cfg = self.config
    base_bps = float(cfg.get("simulate_slippage_bps", 5.0))      # 基础滑点 5bps
    depth_cost = float(cfg.get("simulate_depth_cost_bps", 2.0))   # 深度成本
    # 买入成交价上浮, 卖出下浮 (方向性)
    slip = (base_bps + depth_cost) / 10000.0
    mult = 1 + slip if order.direction.value == "BUY" else 1 - slip
    # 可选: 调用 SmartOrderRouter 用真实盘口精算 (orderbook 可得时)
    return round(order.price * mult, 2)

order.fill_price = self._apply_sim_slippage(order)
order.sim_slippage_bps = round((order.fill_price/order.price - 1)*10000 * 
    (1 if order.direction.value=="BUY" else -1), 1)  # 记录用于归因
```

同时：
- `simulate_fill_delay_seconds` 默认从 `0.0` 改为 `0.15`（150ms，模拟网络+撮合延迟）
- `simulate_partial_fill` 在低流动性标的（bid volume < $50K）默认按 60-80% fill rate 模拟部分成交

### 验收方式
```sql
-- 部署前 vs 后对比: 模拟成交价应不再等于下单价
SELECT 
  ROUND(AVG(ABS(fill_price - order_price)/order_price * 10000), 1) AS avg_slippage_bps,
  COUNT(*) AS n
FROM trade_records
WHERE trade_env = 1  -- 模拟盘
  AND date(entry_time) >= date('now','-3 days');

-- 期望: avg_slippage_bps > 0 (部署前应恒为 0)
```
- 配置: 新增 `simulate_slippage_bps` / `simulate_depth_cost_bps` 到 `config/unified_config.yaml` 的 `execution.simulation` 段
- 跑 1 个交易日模拟盘，验证每笔 `sim_slippage_bps > 0` 且分布在合理区间（5-30bps）

### 回滚
- 删除 `_apply_sim_slippage`，恢复 `order.fill_price = order.price`
- 或最快: config 把 `simulate_slippage_bps` 设 0（保留代码但不生效）

### 风险
- ⚠️ 模拟盘盈利预期下降——**这是预期内的、好的**（终于看见真相）
- ⚠️ 部分成交逻辑要确保 `filled_quantity` 正确更新到 PositionStore，避免仓位账实不符（复用 P1.4 修复后的对账链路验证）

### 工作量
- 编码 0.5 人日（滑点函数 + 接入 3 处 FILLED 赋值点）
- 测试 0.5 人日（覆盖 BUY/SELL/部分成交/零盘口降级）
- 配置 0.2 人日

---

## P1.2 Kill Switch 全链路 fail-close

### 背景
Kill Switch 是**最高优先级紧急停止**。异常时若放行（fail-open），等于紧急制动失灵时车辆继续加速。

### 精确现状
**两处 fail-open**（已逐行确认）：

| 行号 | 当前代码 | 问题 |
|------|---------|------|
| `app/execution/engine.py:239-240` | `except Exception: pass  # kill switch 检查异常不阻断 (fail-open...)` | 手动线下单，异常即放行 |
| `app/pipeline/order_executor.py:3552-3553` | `except Exception as _ks_err: logger.debug(f"[Kill Switch] 检查异常 (放行): {_ks_err}")` | 自动线下单，异常即放行 |

**对比**: 同模块的 IC 门禁（`order_executor.py:3539-3542`）是正确的 fail-safe（异常即不交易）。说明 fail-close 模式在代码库已有先例，可对齐。

### 目标方案
两处统一改为 **fail-close（异常即拒绝/不下单）**：

```python
# engine.py:239 (手动线)
except Exception as _ks_err:
    logger.critical(
        f"[ExecutionEngine·KillSwitch] {order.symbol} kill switch 检查异常, "
        f"安全起见拒绝入场 (fail-close): {_ks_err}"
    )
    order.state = OrderState.REJECTED
    order.failure_reason = f"Kill Switch 检查异常 (fail-close): {_ks_err}"
    return order

# order_executor.py:3552 (自动线)
except Exception as _ks_err:
    logger.warning(f"🚨 [Kill Switch] {symbol} 检查异常, fail-safe 不下单: {_ks_err}")
    return
```

同时：告警通道必须覆盖——`check_kill_switch()` 异常本身应触发钉钉/飞书 CRITICAL（不能只 log），避免静默。

### 验收方式
- 单元测试：mock `check_kill_switch` 抛异常 → 断言订单 REJECTED / 不下单
- 手动：临时让 `emergency_interceptor` import 失败 → 验证入场被拒且告警发出

### 回滚
`git revert`（独立 commit）

### 风险
- ⚠️ 若 `emergency_interceptor` 模块本身有偶发 import 问题，会导致**全场拒单**——这是 fail-close 的正确代价，但需配套监控（P3.5 TradingMonitor 复活后纳入）。过渡期可加一个「连续 N 次 fail-close → 告警」防雪崩。

### 工作量
- 0.2 人日（两处改 + 测试）

---

## P1.3 VIX 阈值动态化 + 强化降仓 ✅ 已实施 (2026-06-17)

> **⚠️ 前提修正（实施时发现）**：原 spec 认为"假降仓完全失效，需新建分级门禁"。**深查代码后推翻此结论**——系统已有多套 VIX 防御机制，问题在于**被开关关掉 / 力度不足 / 误导告警**，而非缺失。本 Item 实际落地为"启用已实现的硬阻 + 强化缩放 + 诚实化冗余检查"，而非新建。

### 背景
`PM_RISK_AUDIT` 核心数据：**HIGH_VOL（VIX>25）141 笔交易亏损 -$154K，占总亏 71%，胜率仅 14.89%**。

### 真实现状（实施时核查确认）
系统里 VIX 防御**有 4 个机制**，远比 spec 假设的多：

| 机制 | 位置 | 实际行为 | 问题 |
|------|------|---------|------|
| **VIXGuard（主硬阻门禁）** | `app/risk/vix_guard.py` + `order_executor.py:3453-3467` | VIX>28 硬阻 + VIX<25 持续15min解封(防抖) | 被 `feature_flags.vix_hard_blocking=false` **关掉**，只 WARN 不拦截 |
| **MarketRegimeFilter（信号评分）** | `app/risk/market_regime_filter.py`（RFC-056） | VIX<18 ×1.0 / 18-22 ×0.85 / 22-25 ×0.5 / ≥28 拒绝 | 已实现且在配置中，但与硬阻链路分离 |
| **RegimeAdapter（仓位缩放）** | `app/strategy/regime_adapter.py` + `_get_order_qty:1351-1377` | VIX≥25 → HIGH_VOL → 仓位 ×scale | 默认 0.5（`regime_adaptation` 配置段**不存在**，走默认）|
| **VIXCheck（pipeline 纵深）** | `app/risk/checks/vix_check.py` | VIX>40 拒；VIX>28 **只 log 不降仓** | "假降仓"死告警，职责与 VIXGuard 重叠 |

即：50% 缩放在 VIX≥25 **确实生效**，但 HIGH_VOL 仍亏 71% → **力度不足**，且 VIX>28 硬阻被关。

### 实际落地方案（已实施）
1. **启用 VIX>28 硬阻**（不是新建）：`config/unified_config.yaml` → `deployment.feature_flags.vix_hard_blocking: false → true`。激活 `VIXGuard` 下单路径硬阻（含 15min 防抖解封，安全，不会永久锁死）。
2. **强化 HIGH_VOL 仓位缩放**：新增顶层 `regime_adaptation` 配置段（原不存在，走默认 0.5）→ `high_vol_position_scale: 0.3` + `position_scales.HIGH_VOL: 0.3`。
3. **诚实化 VIXCheck**：重写 `vix_check.py`——配置化阈值（读 `market_regime_filter.vix_levels`，兼容新旧字段）、VIX≥28 真实 reject（与 VIXGuard 纵深冗余）、删除假"降仓50%"死告警、docstring 标注职责边界（缩放归 RegimeAdapter，硬阻归 VIXGuard）。

### 验证结果（已通过）
- ✅ YAML 合法；`regime_adaptation.high_vol_position_scale=0.3` 被 RegimeAdapter 读到
- ✅ `vix_hard_blocking=True`，`FeatureFlags().is_enabled()` 返回 True
- ✅ VIXCheck 分级正确：VIX=15 正常 / 23 正常 / 26 警戒放行(RegimeAdapter缩放) / 30≥28 拒 / 45 拒

### 待续（移到 P2.3 / 后续）
- VIX 上升速率检测（5min 涨幅>20% → CRITICAL 告警）：P1 内未做，挪 P2.3
- `ic_hard_blocking`（line 2125）已为 true，P1.5/P2.3 复用该 flag 机制

### 回滚
- flag: `vix_hard_blocking: true → false`（恢复 WARN 模式，1 行）
- scale: 删除 `regime_adaptation` 段（回退默认 0.5）
- VIXCheck: `git revert`（恢复旧文件）

### 风险
- ⚠️ VIX>28 后**新仓被阻，交易频次下降**——正是目的（HIGH_VOL 是出血源）。VIX<25 持续 15min 自动解封，不会永久锁死。
- ⚠️ 纵深冗余：VIXGuard + VIXCheck 两处都 reject VIX>28，可视为防御加强（一处失效另一处兜底）；若想去重，P3.1 风控路径合一时收敛。

---

## P1.4 修 `get_plan` 重复定义（恢复降级逻辑）

### 背景
Python 类中后定义的方法**覆盖**先定义的。这个 bug 让"过期计划降级为 watchlist"的安全逻辑**完全失效**。

### 精确现状
**文件**: `app/us_pipeline/battle_plan_manager.py`

| 行号 | 定义 | 内容 |
|------|------|------|
| `battle_plan_manager.py:172-206` | 第一个 `get_plan` | **含降级逻辑**：过期时自动加入 watchlist 全部标的（宽松模式） |
| `battle_plan_manager.py:487-489` | 第二个 `get_plan` | 仅 `return self._plan.copy() if self._plan else {}`（普通 getter） |

**确认**: Python 执行第二个，**降级逻辑（172-206）是死代码**。当作战计划过期，系统不会 fallback 到 watchlist，而是返回可能为空/过期的 `self._plan`——导致信号无处可去或用过期计划交易。

### 目标方案
两选一（推荐 B）：
- **方案 A（合并）**：把降级逻辑内联进 487 的 getter，删除 172 的定义。即 getter 内部判断 `is_expired()` → 触发降级
- **方案 B（重命名分离，推荐）**：
  - 487 的 getter 改名为 `get_current_plan()`（纯读，无副作用）——供所有"只读取"的调用方使用
  - 172 的逻辑改名为 `degrade_to_watchlist()`（显式副作用），由 `revalidate_on_restart()` 或定时器在检测过期时**显式调用**
  - 这样"读"和"降级"语义分离，避免 getter 隐式改状态的坑

### 验收方式
```bash
# 确认无重复定义
grep -n "def get_plan\|def get_current_plan\|def degrade_to_watchlist" app/us_pipeline/battle_plan_manager.py
# 期望: 三个方法各定义一次, 无重复
```
- 功能测试：构造过期 plan → 调用降级 → 断言 targets 含 watchlist 标的
- 启动测试：模拟 plan 过期重启 → 验证 `revalidate_on_restart` 触发降级（而非 RISK_OFF，除非 F3 校验失败）

### 回滚
`git revert`（独立、零依赖，最安全的修复）

### 风险
- 🟢 极低。纯 bug 修复，行为只会"从不降级 → 正确降级"

### 工作量
- 0.2 人日（重命名 + 改调用方 + 测试）

---

## P1.5 IC 门禁接统计显著性 + 多维度

### 背景
IC 门禁（`order_executor.py:3513`）的设计方向是对的，但当前实现**只用 30 个信号的均值 IC**，无显著性检验、单一全局阈值——PM 审计指出这会导致「IC 缓慢从 -0.04 变 -0.10，系统仍在出血」而统计上无法拒绝 IC=0。

### 精确现状
**文件**: `app/pipeline/order_executor.py:3517-3542`

```python
_IC_MIN_THRESHOLD = 0.05
_IC_MIN_SIGNALS = 30   # 30 个信号才统计
_ic = get_ic_summary(min_signals=_IC_MIN_SIGNALS, days_back=30)
# insufficient_data → return (block) ✅
# ic_mean < 0.05 → return (block) ✅
# exception → return (fail-safe) ✅
```

**问题**：
1. **无 p 值**：IC=0.04（30 信号）和 IC=0.04（500 信号）可信度天差地别，当前一视同仁
2. **单一全局阈值**：不区分策略/方向/regime。LONG 与 SHORT 的 IC 应分开看（PM 数据：SHORT 有正向，LONG 反向）
3. **只拦单不调权**：IC 低就一刀切停交易，而非降低仓位/权重（更合理的渐变）

### 目标方案
**改 A（统计显著性）**：在 `signal_ic_tracker.get_ic_summary()` 增加 p-value（基于 IC 标准误的 t 检验）。门禁规则升级为：
```python
# 显著且正向 → 放行
if ic_mean >= 0.05 and p_value < 0.05:
    pass
# 不显著 (无法拒绝 IC<=0) → 拦
elif p_value >= 0.05 and n < 100:
    return  # 样本不足, 暂停积累
# 显著但 IC 低/负 → 拦
else:
    return  # IC 不达标
```

**改 B（多维度拆分）**：门禁按 `(strategy_id, direction)` 维度分别评估，而非全局一刀切：
- LONG IC<0 → 仅禁 LONG 方向
- 某策略 IC<0 → 仅禁该策略（这正是 P2.3 IC 监控自动禁用的雏形）

**改 C（渐变响应，可后置到 P2）**：IC 0.02-0.05 区间不直接停，而是仓位缩放 ×0.5；IC<0 才停。

### 验收方式
```sql
-- 按方向/策略拆分的 IC 报告 (诊断用)
-- 实际门禁逻辑由 get_ic_summary + 单元测试验证
```
- 单元测试覆盖：高 IC 高 n（放行）/ 低 n（积累）/ 显著负 IC（拦截）/ 不显著（拦截）
- 部署后观察：LONG 方向自动交易是否因 IC 负被独立限制（预期会，这是好事）

### 回滚
- 阈值改回单一 0.05；或 feature_flag 关闭多维度拆分

### 风险
- ⚠️ 多维度拆分后，**早期某些方向/策略样本不足会暂停**——需要 graceful：样本不足时降到全局门禁，而非完全停
- ⚠️ p-value 计算依赖 IC 的标准误，需确认 `signal_ic_tracker` 记录了足够的逐信号 IC 值（不只是汇总均值）

### 工作量
- p-value + 多维度: 1 人日
- 渐变响应: 0.5 人日（可挪 P2）

---

## 阶段 1 实施状态表（迭代用）

| ID | Owner | PR/Commit | 部署日期 | 验收结果 | 状态 |
|----|-------|-----------|---------|---------|------|
| P1.1 | Claude | 待提交 | 待部署 | 方向/幅度/禁用全部验证 ✅ | ✅ 已实施 |
| P1.2 | Claude | 待提交 | 待部署 | 2 单元测试通过 ✅ | ✅ 已实施 |
| P1.3 | Claude | 待提交 | 待部署 | flag/scale/分级逻辑验证 ✅ | ✅ 已实施 |
| P1.4 | Claude | 待提交 | 待部署 | 1个getter+降级链路完好 ✅ | ✅ 已实施 |
| P1.5 | Claude | 待提交 | 待部署 | 真实数据印证(IC=-0.005,p=0.96,LONG IC=-0.25) ✅ | ✅ 已实施 |

> 完成后更新本表 + 总纲 §3 优先级矩阵状态列 + §4 Gate 1→2 勾选项。
>
> **本次实施的关键修正（验证先于实施，3/5 项推翻了原 spec 前提）**：
> - P1.4：降级逻辑**一直在工作**（via `load()`），仅删冗余死代码（非失效）
> - P1.3：VIX 防御**早已多套存在**（VIXGuard/MarketRegimeFilter/RegimeAdapter），实际是启用 flag+强化+诚实化（非新建）
> - P1.5：`ic_p_value` **早已算好**、`strategy_health` 三级已建，实际是接入门禁+方向感知（非新建 p 值）

---

## 阶段 1 完成定义（Definition of Done）
- [x] P1.1~P1.5 全部 `✅`（代码已实施并验证）
- [ ] 部署 ≥3 个交易日（待上线）
- [ ] 模拟盘 `avg_slippage_bps > 0`（P1.1 生效后，待真实交易日验证）
- [ ] HIGH_VOL（VIX>25）自动交易数下降（P1.3 生效后，待真实交易日验证）
- [ ] 连续 3 日无「幽灵仓 / kill switch 静默绕过 / 过期计划无降级」事件（待上线观察）
- [ ] 上述状态表填齐 PR/Commit/部署日期（待提交）
