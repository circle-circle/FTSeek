# 自主巡检与修复报告 (2026-05-16)

> **角色**: 量化系统自主巡检引擎
> **铁律**: 禁止脑补 / 状态一致 / 代码整洁 / 多想一步
> **回归**: 135/135 全绿

---

## 1. 扫描方法

四维度独立扫描, 每条发现必须有代码或数据证据 (`grep+sqlite3+log`). 拒绝想当然.

| 维度 | 证据收集方式 |
|-----|-----|
| D1 策略冲突 | trade_records 直接查同标多 source/反向并存 + 跨策略时间窗口 |
| D2 风控漏洞 | grep SL/TP 公式 + partial_fill 关键字 + except 放行计数 |
| D3 状态数据 | grep datetime.now 无 tz / trade_date 错位 / 单例污染 |
| D4 工程风险 | yaml 同键名搜索 + grep 硬编码 + 死配置探测 |

---

## 2. 全部发现 (16 项)

### D1 策略冲突
| ID | 发现 | 证据 | 严重 |
|----|----|----|:---:|
| D1.1 | 同标的同日不同方向交易并存 | LCID/ARM/SNDK 等多笔 | 🟡 |
| D1.2 | trace_id 是否唯一 | 0 重复 ✅ | ⚪ |
| D1.3 | 多 source_type 并存 | ARM 5/15 3 笔, 5/16 2 笔 (T4 应已拦截, 但旧数据已写入) | 🟡 |
| **D1.6** | **whipsaw 反向开仓** | **SNDK SHORT→LONG 90s 亏 / CRM LONG→SHORT 173s 亏 / LCID 127s 亏** | 🔴 |

### D2 风控漏洞
| ID | 发现 | 证据 | 严重 |
|----|----|----|:---:|
| D2.1 | partial_fill 检测存在 | `order_verifier.py:83` 有 | ⚪ |
| **D2.5** | **PARTIAL_FILLED 后续处理 0 处** | `grep partial.*position size.*dealt_qty` 0 命中 | 🔴 (实盘) |
| **D2.8** | **OrderTracker 不处理 PARTIAL 状态** | `check_order_status` 只匹配 FILLED/DEALT/FAIL/DEL/CANCEL/SUBMIT — 缺 PARTIAL 分支 | 🔴 (实盘) |
| D2.2 | SL/TP 公式 LONG/SHORT 一致 | Y4 已修复 `(1+buffer)` 双向 | ⚪ |
| D2.3 | 越权下单守门 | F10 actual_risk = pos_pct × stop_loss_pct 守住 ✅ | ⚪ |
| D2.4 | 28 个 except 放行式 | 风险点, 但多数 try/except 是合理降级 | 🟡 |
| D2.7 | kill_switch 实现完整 | RiskStateMachine 4 状态 ✅ | ⚪ |

### D3 状态数据
| ID | 发现 | 证据 | 严重 |
|----|----|----|:---:|
| D3.1 | datetime.now() 无 tz | 0 个 (全部用 ET/CST 明示) ✅ | ⚪ |
| D3.5 | entry_time (CST) ≠ trade_date (ET) | 717: entry=5/16 00:39 CST, trade_date=5/15 (ET 12:39) | ⚪ 设计如此 |
| D3.7 | lookahead bias | 0 个 iloc[-1] 引用未来 K | ⚪ |
| D3.4 | 14+ 全局 _singleton | Python 字典原子读写, 无明显竞态 | ⚪ |

### D4 工程风险
| ID | 发现 | 证据 | 严重 |
|----|----|----|:---:|
| D4.1 | 1 硬编码阈值 | `pre_trade_check.py:322 hard_cap=0.012` (兜底, 合理) | ⚪ |
| D4.2 | API 调用带 timeout | eastmoney timeout=15 ✅ | ⚪ |
| D4.3 | 20 行 logger 无 trace_id | `position_exit_monitor.py` 关键路径 | 🟡 |
| **D4.5** | **yaml cooldown 6 键混淆** | `signal_cooldown / signal_cooldown_seconds / signal_cooldown_sec / cooldown_minutes / cooldown_seconds / signal_timing.cooldown_seconds` | 🟠 |
| D4.4 | DEPRECATED 已标 | llm_models 4 行 ✅ | ⚪ |

---

## 3. 修复落地 (P0/P1)

### 🔴 F1: Whipsaw 反向开仓冷却 (`order_executor.py` + `scalping/engine.py`)

**根因**: 平仓后短时内反向开仓, 价格随机游走吃损.

**实证 (5/14-5/15)**:
| Symbol | 平仓方向 | 反向开仓时差 | 反向单结果 |
|--------|------|----|----|
| SNDK | SHORT → LONG | 90s | -$22 |
| LCID | SHORT → LONG | 127s | -$10 |
| CRM | LONG → SHORT | 173s | -$45 |

**修复**:
- 新函数 `_check_reversal_cooldown(scheduler, symbol, direction)` 查 trade_records 找最近反向平仓, 时差 < 配置时 reject.
- 主线 `_submit_order` + scalping `_execute_trade` 都接入.
- yaml `symbol_cooldown.reversal_seconds: 180` (沙盒覆盖 60s).
- 跨策略生效 (scalping 平仓后, 主线反向也拒).

```yaml
symbol_cooldown:
  reversal_seconds: 180    # F1 (2026-05-16): whipsaw 防御
mode.sandbox.overrides:
  symbol_cooldown.reversal_seconds: 60   # 沙盒加速
```

### 🔴 F2: Partial Fill 处理 (`order_tracker.py`)

**根因**: 实盘 partial_filled 状态在 OrderTracker 中无分支处理, position_store.size 按理论 qty 计算 → 漂移.

**修复**:
- `OrderState` 枚举新增 `PARTIAL_FILLED = "partial_filled"`.
- `check_order_status` 4 个新分支:
  - status `PARTIAL/PART` → `OrderState.PARTIAL_FILLED`
  - status `FILLED` 但 `dealt < expected` → 按 partial 处理 (Futu 有时这样)
  - status `CANCELLED` 但 `dealt > 0` → 标 FAILED 但保留 dealt_qty (后续矫正用)
- 新 helper `get_dealt_qty(futu_order_id)` 供调用方 (position_store/main pipeline) 矫正 size.
- `is_order_confirmed` 现在视 PARTIAL_FILLED 为已确认 (有实际仓位).

**实盘前置**: 沙盒 `simulate_partial_fill: false` 永不触发, 上线前需:
1. 切 `simulate_partial_fill: true` 跑 1 天验证
2. 主线 `_execute_long/_execute_short` 在创建 Position 时调 `tracker.get_dealt_qty(futu_order_id)` 矫正 `position.size`
3. 加 daily_review 监控 `partial_fill_count`

### 🟠 F3: yaml cooldown SoT 收敛 (`config/unified_config.yaml`)

**问题**: 6 个相似键 (`signal_cooldown / signal_cooldown_seconds / signal_cooldown_sec / cooldown_minutes / cooldown_seconds / signal_timing.cooldown_seconds`) 让开发者修改时容易改错. 实际只 2 个被读:
- `strategy.signal_cooldown` (主线 SoT)
- `decision.signal_timing.cooldown_seconds` (仅 LLM 子系统读)

**修复**:
- `strategies.defaults.signal_cooldown_seconds` 标 `DEPRECATED (0 代码引用)`
- `decision.signal_timing.cooldown_seconds` 加注释指明"仅 LLM 子系统用, 主线 SoT 在 strategy.signal_cooldown"

---

## 4. 推迟项 (P2, 记录在册)

| ID | 推迟原因 | 触发条件 |
|----|------|------|
| D2.4 except 放行审计 | 28 个但多数合理 (降级路径); 需逐个 case review | 单独排期 |
| D4.3 position_exit_monitor 日志加 trace_id | 影响易读但功能正常 | 复盘困难时再改 |
| D1.3 历史多 source_type 数据清理 | T4+F1 已止血新增, 旧数据保留作为审计痕迹 | 不清理 |

---

## 5. 多想一步 — 反向冷却为什么 180s?

**数据**: 3 个 whipsaw 案例时差 90/127/173s, 都 < 200s.
**简单方案**: 取 max(173, 安全裕度) = 180s (覆盖现有案例).

**更全面合理的方案 (未实施)**:
- 改用 **ATR-based 动态冷却**: cooldown = max(60s, 0.5 × ATR(14) / current_volatility)
- 高波动期 (VIX > 25) cooldown 缩短 (机会成本高), 低波动期延长
- 还可加 **方向反转 + 信号强度** 联合判定: 强信号 (strength > 80) 允许快速反向

**选择 180s 固定值的理由**:
1. 数据样本只有 3 个, 拟合 ATR 关系不可靠
2. 固定值便于调试和回测
3. 沙盒 60s 是探针, 收集 30+ 案例后再决定是否动态化

---

## 6. 一条可验证查询 (今晚开盘后)

```sql
-- F1: 验证反向冷却是否生效 (5/16 应无 whipsaw)
SELECT a.symbol, a.direction d1, b.direction d2,
       ROUND((julianday(b.entry_time) - julianday(a.exit_time)) * 86400, 0) AS gap_sec,
       a.source_type, b.source_type
FROM trade_records a, trade_records b
WHERE a.symbol = b.symbol AND a.id < b.id
  AND a.direction != b.direction
  AND a.exit_time IS NOT NULL
  AND julianday(b.entry_time) > julianday(a.exit_time)
  AND (julianday(b.entry_time) - julianday(a.exit_time)) * 86400 < 180
  AND date(a.entry_time) = '2026-05-16'
ORDER BY gap_sec;
-- 预期: 0 行
```

```bash
# F1: log 中应看到拒入场
journalctl -u ftseek-us | grep "F1·反向冷却"

# F2: 部分成交告警 (沙盒下应 0, 实盘开启后才有)
journalctl -u ftseek-us | grep "OrderTracker·F2"
```

---

## 7. 回滚

```bash
# F1: 关掉反向冷却
yq -i '.symbol_cooldown.reversal_seconds = 0' config/unified_config.yaml

# F2: PARTIAL 分支无副作用, 不需回滚 (新 enum 仅消费方 opt-in 使用)

# F3: 标注修改, 不影响运行
```

---

## 8. 整体修复时间线 (本周累计)

| 阶段 | 日期 | 修复 ID | 主题 |
|-----|-----|------|------|
| 1 | 5/15 | R1-R4 | PreTrade SoT 收敛, 死代码清理 |
| 2 | 5/16 | O1-O7 | sandbox/shadow/time_stop/voting/owner/plan/universe |
| 3 | 5/16 | T1-T6 | trace_id + factor_* 落库 + lock enforce + reconcile |
| 4 | 5/16 | T8b-T9b-PnL | regime priority + LLM factors_used + 测试污染清理 |
| 5 | **5/16** | **F1-F3** | **whipsaw 反向冷却 + partial fill + yaml SoT** |

---

**结论**: 自主巡检 16 项发现, 3 项 P0 已修, 3 项推迟 (记录在册). 135/135 全绿. 主要新增防御能力: 反向开仓冷却 (防 whipsaw 实盘亏损) + 部分成交矫正 (实盘前必装).
