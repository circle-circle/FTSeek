# FTSeek-US 配置摘要 & OKR 进展

> 生成时间: 2026-06-19 | 模式: Shadow验证 + Playbook最小实盘(方案B)

---

## 一、当前运行模式

| 组件 | 配置值 | 含义 |
|------|--------|------|
| `pipeline.master_switch` | `shadow_only` | 自动信号管线(IC门禁)运行但**不实际下单**，积累数据验证 |
| `playbook.enabled` | `true` | Playbook系统开启 |
| `playbook.execute_mode` | `live` | Playbook指令触发后**真实下单** |
| `playbook.auto_execute` | `true` | 盘中自动执行triggered指令(每30min检查) |
| `playbook.enable_weekly` | `true` | 周五自动生成周报 |

### 模式说明
- **信号管线**(IC门禁 → Stage7 → 风控 → 执行): 全部走shadow，只记录不交易
- **Playbook**(每日LLM分析 → 指令生成 → 触发追踪 → 执行): 走live实盘
- **两套系统并行**: Playbook用实盘验证，信号管线用shadow积累IC数据

---

## 二、风控参数

### 资金
- **账户资金**: $82,000 (fixed_amount模式)
- **资金池分层**: 手动(manual) + 自动(auto)，通过ManualHoldRegistry隔离

### 仓位管理
| 参数 | 值 | 说明 |
|------|-----|------|
| `max_position_pct` | 25% | 单只最大仓位 |
| `max_per_direction` | 65% | 单方向最大暴露 |
| `max_total` | 80% | 总仓位上限 |
| `risk_per_trade_pct` | 0.8% | 单笔风险 |

### 模式自适应
| 模式 | 单仓上限 | 日亏上限 | 总暴露上限 | 熔断 |
|------|---------|---------|-----------|------|
| simulate | 15% | 5% | 80% | 8% |
| **live_conservative** | **8%** | **2%** | **40%** | **3%** |
| live_normal | 12% | 3% | 60% | 5% |

### LONG方向折扣 (动态)
| Regime | 折扣系数 | 说明 |
|--------|---------|------|
| STRONG_BULL | 1.0 | 无折扣 |
| BULL | 1.0 | 无折扣 |
| CAUTION | 0.85 | 85折 |
| BEAR | 0.7 | 7折 |
| CRISIS | 0.0 | 禁LONG |

**牛市豁免条件**: SPY > MA50 AND SPY > MA200 AND VIX ≤ 20 AND 连续上涨 ≥ 2天

### 熔断机制
| 触发条件 | 动作 |
|---------|------|
| 策略连续亏损 3 笔 | 暂停该策略 30 分钟 |
| 小时亏损 > 2% | 暂停 |
| 日亏损 > 3% | 暂停 |
| 单笔亏损 > 0.5% (symbol_daily_loss_guard) | 禁该symbol当日交易 |
| 单symbol连亏 5 笔 | 禁该symbol |

### VIX 阈值
| 级别 | VIX | 效果 |
|------|-----|------|
| relaxed | < 18 | 宽松风控 |
| normal | 18-22 | 正常 |
| tightened | 22-28 | 收紧 |
| blocked | ≥ 28 | 禁止开仓 |
| unblock | < 25 | 解除封锁 |

### 黑名单 & 自管
- **黑名单**: QCOM, LCID, TME, HK.00700
- **自管持仓**(系统不干预): AMD, BABA, NVDA, AAPL, HK.00700

---

## 三、信号管线状态 (Shadow)

### IC门禁 (已生效)
- 策略级IC计算 + 5分钟缓存
- IC < 0 的策略被自动禁用
- EARNINGS_REVISION_MOMENTUM: IC=-0.047 → 已禁用 ✅
- TECHNICAL_MULTI_FACTOR LONG: IC=-0.157 → 已禁用 ✅
- UNKNOWN LONG/SHORT: IC负 → 已禁用 ✅

### 已禁用策略
| 策略 | 方向 | IC | 状态 |
|------|------|----|------|
| scalping | 全 | 0% 胜率 | ✅ 已清理 |
| SHORT_PROXY | 全 | 期权灾难 | ✅ 7层禁用 |
| EARNINGS_REVISION | LONG | -0.047 | ✅ IC门禁拦截 |
| TECHNICAL_MULTI_FACTOR | LONG | -0.157 | ✅ IC门禁拦截 |

### 有效信号源
| 信号 | 方向 | IC | 胜率 | 说明 |
|------|------|-----|------|------|
| INSIDER_CLUSTER_BUY | LONG | 待验证 | 75%(n=8) | 最强候选，min_cluster_size=2已放宽 |
| main SHORT | SHORT | 正 | 53%(n=100) | 唯一真alpha，+$24,557 |

---

## 四、Playbook 系统状态

### 架构
```
PRE_MARKET(08:30 ET):
  Layer0: MarketRegimeGate (VIX + SPY MA)
  Layer1: Screener (28只watchlist → top N)
  Layer2: InstructionGenerator (LLM批量生成, 反幻觉)
  Layer3: DB入库 + 飞书盘前推送

INTRADAY(每30min):
  AutoExecute: 扫描triggered指令 → live下单

AFTER_HOURS(16:30 ET):
  Track: 回填outcome/shadow_pnl/MFE/MAE + 飞书盘后推送
  Weekly(周五): IC报告 + 因子分析 + 权重调整建议
```

### 6/18 首次运行结果
| Symbol | 方向 | 置信度 | Entry | SL | TP | Triggered | Shadow PnL |
|--------|------|--------|-------|----|----|-----------|-----------|
| **AVGO** | **BUY** | **64** | **392.9** | **380.37** | **417.96** | **0** | **+$1,845** |
| BA | SKIP | 0 | - | - | - | 1(missed) | -$285 |
| BWXT | SKIP | 0 | - | - | - | 0 | - |
| PDD | SKIP | 0 | - | - | - | 0 | - |
| LMT | SKIP | 0 | - | - | - | 0 | - |
| NTES | SKIP | 0 | - | - | - | 0 | - |
| JPM | SKIP | 0 | - | - | - | 0 | - |

**关键发现**:
1. AVGO BUY指令生成但未triggered (entry=392.9, 可能价格未进入±3%窗口)
2. BA因技术面价格缺失强制SKIP，但被标记为missed (triggered=1)
3. 5/7 SKIP，触发率 1/7 = 14.3% (低于目标40%)
4. Shadow PnL计算: AVGO +$1,845 (假设100股 × 价格变动)

### Regime
- 6/18: CAUTIOUS, VIX=20.0, top_n=5, pos_mult=0.5

---

## 五、OKR 进展

### 目标: 30天稳定盈利

| KR | 目标 | 当前值 | 得分 | 趋势 |
|----|------|--------|------|------|
| **KR1 胜率** | > 50% | 42.3% (78笔/30天) | 0/20 | 🔴 未达标 |
| **KR2 周PnL** | 正收益 | -$5,558 (30天) | 0/20 | 🔴 亏损 |
| **KR3 样本量** | ≥ 30笔 | 5笔(非SKIP) | 3/20 | 🟡 积累中 |
| **KR4 IC** | > 0.02 | 无数据(IC门禁shadow) | 0/20 | 🔴 待积累 |
| **KR5 触发率** | > 60% | 14.3% (1/7) | 5/20 | 🟡 需改进 |
| **总分** | 100 | - | **8/100** | 🔴 |

### 30天交易数据 (硬数据)

| 指标 | 值 |
|------|-----|
| 总笔数 | 78 |
| 胜率 | 42.3% (33W / 45L) |
| 总PnL | -$5,558 |
| 平均PnL | -$71.26/笔 |
| 最佳策略 | main SHORT: 53%胜率, +$24,557 (100笔历史) |
| 最差策略 | scalping LONG: 7%胜率 (已停用) |

### 因子表现
| 因子 | 交易数 | 胜率 | 判定 |
|------|--------|------|------|
| insider_cluster_buy | 8 | 75% | ✅ positive |
| 其他因子 | 数据不足 | - | ⏳ 待积累 |

---

## 六、关键问题 & 修复计划

### 🔴 P1: 触发率过低 (14.3% vs 目标60%)
**根因分析**:
1. AVGO entry=392.9, triggered=0 → 价格未进入±3%窗口
2. 6/7指令为SKIP → Screener筛选太严 or LLM在CAUTIOUS环境下倾向SKIP

**修复方向**:
- [ ] 检查AVGO 6/18-6/19实际价格 vs entry=392.9
- [ ] 放宽触发窗口: ±3% → ±5%
- [ ] 分析LLM SKIP原因分布

### 🟡 P2: BA triggered=1 但 direction=SKIP
**根因**: SKIP指令的entry_price=0, 任何价格都"触发"了
**修复**: 已在代码中修复(SKIP强制不触发), 需验证

### 🟡 P3: Shadow PnL计算基数
**问题**: 假设100股固定, 与position_size不匹配
**修复**: 已改用instruction.position_size, SKIP不计算

---

## 七、下一步计划

### 本周 (6/22-6/28)
| 日期 | 任务 | 负责人 |
|------|------|--------|
| 6/22 | 首日验证: 检查Playbook生成+飞书推送+触发情况 | 系统 |
| 6/23 | 分析触发率, 必要时调整窗口 | Hermes |
| 6/25 | 中期检查: 累积数据质量, LONG改善情况 | Hermes |
| 6/27 | 周五: 首份周报自动生成验证 | 系统 |

### 毕业门禁 (Shadow → Live全量)
全部满足才能切换`master_switch`从`shadow_only`到`live`:
1. ✅ IC (T+3) > 0.05 — 待积累
2. ✅ 样本量 ≥ 30 — 当前5
3. ✅ 最大回撤 < 5% — 待观察
4. ✅ JSON解析率 > 90% — 6/18: 7/7 = 100% ✅
5. ✅ 触发率 > 60% — 当前14.3% 🔴

### 决策树
```
IF 触发率 > 60% AND 样本 > 10:
    分析胜率, 评估信号质量
ELIF 触发率 < 30%:
    放宽窗口到±5%, 降低screener阈值
IF shadow运行满7天 AND IC > 0.05:
    切换master_switch到live
IF 周PnL连续2周为正:
    考虑提升仓位到live_normal
```

---

## 八、服务状态

| 服务 | 状态 | 说明 |
|------|------|------|
| ftseek-us (systemd) | 运行中 | 主交易引擎 |
| ftseek-dashboard | 运行中 | Web仪表板 :8901 |
| nginx | 运行中 | 反向代理 8888→8901 |
| 飞书推送 | 配置中 | 需验证cron是否正常触发 |

### Cron Jobs
| Job | 频率 | 说明 |
|-----|------|------|
| OKR日报 | 每日ET 17:30 | PnL汇总 + OKR评分 |
| OKR周报 | 周日ET 10:00 | 周度复盘 |
| Playbook Generate | 内置于_run_llm_loop PRE_MARKET | 每日盘前生成 |
| Playbook Track | 内置于_run_llm_loop AFTER_HOURS | 每日盘后追踪 |
| Playbook Auto-Execute | 盘中每30min | 自动执行triggered指令 |

---

## 附录: 配置变更历史

| 时间 | 变更 | 原因 |
|------|------|------|
| 6/18 | playbook.enabled: false→true | 启动shadow验证 |
| 6/18 | 触发窗口 ±2%→±3% | 提升触发率 |
| 6/19 | execute_mode: simulate→live | 方案B: Playbook最小实盘 |
| 6/19 | INSIDER min_cluster: 3→2 | 降低门槛, 2人同买已强信号 |
| 6/19 | SHORT_PROXY 7层禁用 | 期权灾难, 彻底封死 |
| 6/19 | LONG方向折扣0.7 | 历史数据: LONG胜率26.6%远低于SHORT |
| 6/19 | **Scalping 完全清理** | 删除800行代码+配置, 消除认知负担 |
| 6/19 | **AVGO Entry Price 修复** | shadow_tracker使用actual_entry_price而非decision_price |
| 6/19 | **触发窗口 ±3%→±5%** | AVGO案例: $392.9→$409.25超出±3%上限 |

---

## 九、今日关键决策 (2026-06-19)

### 1. Scalping 策略完全清理
- **动作**: 删除 `app/scalping/` 目录、清理 `unified_config.yaml`、移除14处代码引用
- **影响**: 代码库减少约800行，消除"已删除策略"的认知负担
- **保留**: DB历史查询（`strategy_id='scalping'`）用于归因分析
- **验证**: `grep -r "scalping" app/` 返回0结果

### 2. AVGO Entry Price 计算修复
- **问题**: 信号显示entry=$392.9，实际成交=$409.25，差异4.2%
- **根因**: `instruction_generator.py` 使用理论价而非实际成交价
- **修复**: `shadow_tracker.py` 优先使用 `trade_store.get_actual_entry_price()`
- **影响**: Shadow PnL计算更准确，反映真实交易成本

### 3. voting_llm 策略深度审计
- **发现**:
  - 命名不一致：`voting_llm` vs `llm_vote` 混用
  - 重复触发：同一symbol同天可能触发多次（signal_dedup未覆盖）
  - IC门禁覆盖不全：只拦截main策略，voting_llm独立运行
- **数据**: 30天48笔交易，-$11,504，胜率35.4%
- **下一步**: 统一命名、扩展signal_dedup、IC门禁覆盖所有策略
- **详细报告**: `docs/voting_llm_audit_20260619.md`

### 4. 触发窗口调整 (±3% → ±5%)
- **背景**: AVGO案例中价格超出±3%窗口导致未触发
- **决策**: 放宽到±5%，平衡触发率与价格合理性
- **验证**: 需下一个交易日监控触发率变化

---

## 十、待办事项 (Next Actions)

### 高优先级 (P0)
- [ ] **voting_llm 命名统一**: 全局grep `llm_vote` → `voting_llm`
- [ ] **扩展signal_dedup**: 覆盖voting_llm路径，防止重复触发
- [ ] **IC门禁扩展**: 覆盖所有策略（包括voting_llm）

### 中优先级 (P1)
- [ ] **触发率监控**: 添加实时指标，验证±5%窗口效果
- [ ] **Shadow PnL验证**: 下个交易日确认actual_entry_price生效
- [ ] **OKR追踪**: 完成5维度评分系统实现

### 低优先级 (P2)
- [ ] **voting_llm 策略优化**: 分析30天48笔交易，识别失败模式
- [ ] **信号质量评估**: 建立signal_quality_tracker，关联IC与胜率

