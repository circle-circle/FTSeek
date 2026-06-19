# FTSeek-US 实盘系统风控诊断报告

## 执行摘要

**系统当前状态：危险信号频现，建议立即暂停实盘**

基于PM经验（$50M+实盘基金、多次极端事件管理），当前系统面临四大核心风险：
1. **信号失效**：整体IC=-0.0356，特别是SHORT信号IC=-0.0587，方向感反向
2. **HIGH_VOL亏损**：VIX>25状态下141笔交易亏损-$154K（71%的总亏损来源），平均亏-$1.1K/笔
3. **评分体系崩溃**：置信度高的信号反而亏钱(IC=0.1685)，低置信度信号虽IC较差但至少sample量足
4. **保护机制不足**：LONG方向只有35胜率(25%)，但系统仍在持续交易

---

## 一、灰犀牛场景分析（已发生/正在发生）

### 场景1：延迟优化后"更快地亏钱"
**风险评分：⚠️ P0-CRITICAL**

**论证**：
- 当前IC=-0.0356整体为负，说明信号方向感已经反转
- Task 1增加的5秒轮询会导致更频繁触发，损失函数=负IC × 交易频率
- 数据证据：
  - LONG: 137笔交易, 35胜(25.5%), IC=0.185看似正，但**整体综合后LONG策略-0.05%平均亏损**
  - SHORT: 200笔, 80胜(40%), IC=-0.0587，更快的信号只会加速负IC暴露

**PM建议**：
```
立即冻结Task 1（信号延迟优化）
原因：加速交易需要前提条件是正IC，当前负IC时优化信号延迟相当于
      在崖边快速奔跑——速度越快，掉下去时损失越大
推荐替代方案：
  - 暂停新仓，集中评分函数审计（Task 2优先级升为P0）
  - 临时切换为半自动模式（每笔人工复审）
```

### 场景2："历史拟合"vs"实盘Forward Walk"
**风险评分：⚠️ P0-CRITICAL**

**观察**：
- IC分析显示90天回溯数据中：
  - LONG IC=0.185(正), SHORT IC=-0.0587(负)
  - 但实际交易结果：LONG亏-0.05%, SHORT盈+0.29%
  - **这说明IC反映的不是实盘收益预测力，而是历史拟合的虚假信号**

**原因假设**：
1. 评分函数可能用了未来信息(look-ahead bias)
2. 信号强度与实际收益相关性破裂
3. 市场结构变化(2026年新行为模式)导致策略失效

**PM建议**：
```
Task 2评分函数审计需要特别关注：
  1. 是否有时间泄露（评分时用了T+1数据）
  2. 信号强度与同日收益的相关性（需要分层验证）
  3. 不同regime下信号预测力差异（HIGH_VOL彻底失效）

验证方法：
  - 跑blind forward walk: 用前30天数据训练评分，评估后30天实盘收益
  - 对比结果与今日IC报告，若差异>0.15则证实拟合过度
```

### 场景3：IC监控30天窗口在Regime突变时失效
**风险评分：⚠️ P1-HIGH**

**观察**：
- HIGH_VOL体制下：141笔交易, 胜率14.89%, 总亏-$154.31K
- 这代表当VIX>25时，几乎所有策略都失效
- 但system目前使用的VIX cutoff是28，导致22-28区间被视为"可交易"

**灾难场景**（2020.2.24类似）：
```
时间序列：
  09:30  VIX=20, 系统正常交易
  09:45  新闻黑天鹅(e.g. 央行政策), VIX跳至28
  09:50  Task 3的"IC监控"未能实时反应，因为样本只有2-3笔
  10:00  VIX继续上行至35, 系统突然hard reject新仓
         但已有持仓被闪崩触发
```

**PM建议**：
```
VIX阈值应该动态降低，不能固定在28：
  改为：
    - VIX 18-22: 信号×1.0 (正常)
    - VIX 22-25: 信号×0.5 (已在config，需执行)
    - VIX 25-28: 信号×0.2 (新增，极度保守)
    - VIX >=28: 完全禁止新仓 + 积极止损 (新增)

这样可以"软着陆"而不是陡峭悬崖
```

---

## 二、黑天鹅场景分析（极端场景压力测试）

### 场景1：VIX 20→40瞬间跳跃
**风险等级：🔴 CRITICAL**

**问题**：
- 当前VIX缓存TTL=60秒（已从600秒优化）
- 但"5分钟涨幅>15%"的检测会不会触发？

**检查**：
```python
# 从config看, VIX风控在 market_regime_filter.py 中实现
vix_levels:
  critical: 28             # >= 28: 完全拒绝 (从 30 降至 28)

# 但没看到"5分钟涨幅"动态检测，只有静态阈值！
```

**PM建议**：
```
新增VIX volatility check:
  - 定时记录VIX level @每分钟
  - 若5分钟内涨幅>20%, 自动触发:
    1. 全部持仓止损（不等trailing stop）
    2. 禁止新仓48小时
    3. Alert级别升为CRITICAL+电话通知
```

### 场景2：做市商撤单→流动性枯竭
**风险等级：🔴 CRITICAL**

**问题**：
- ORB快速通道设计用5秒轮询
- 但如果market_maker撤单，spread会瞬间变为1-2%
- 系统是否有spread check？

**检查**：
```yaml
# 从config看:
execution:
  slippage:
    max_ratio: 0.005  # 最大滑点0.5%
    check_enabled: true
    
# 但没看到"pre-entry spread check"，只有exit时检查
# ORB入场时没有验证spread！
```

**PM建议**：
```
在entry_gate前新增Spread Monitor：
  - 记录最近5笔成交的平均spread
  - 若实时spread > 平均×3, 拒绝entry
  - 若spread > 1%, 直接下limit order而不是market order
  
特别为ORB快速通道加防护：
  max_entry_spread_bps: 20  # 20bp才能entry
```

### 场景3：交易所熔断/暂停
**风险等级：🟠 HIGH**

**问题**：
- 系统是否知道交易所熔断？

**检查**：
```yaml
# 从risk_pipeline配置看:
critical_checks_fail_close:
  - MarketCircuitCheck
  
# 但MarketCircuitCheck具体实现是什么？
```

**PM建议**：
```
MarketCircuitCheck需要包含：
  1. 美股熔断检测 (Level 1/2/3 @ S&P 500)
  2. 单只股票熔断 (LULD, 5min暂停)
  3. 交易所主备系统切换检测
  
若触发任何熔断：
  - 全部市价单改为cancel（不是modify to limit）
  - 持仓hold住，不要panic平仓
  - 待系统恢复后手动review
```

---

## 三、慢性死亡场景（系统尚未触发但会发生）

### 场景1：IC从-0.04逐日变差，但统计不显著
**风险评分：⚠️ P1-HIGH**

**数据**：
```
当前状态：
  - 整体IC=-0.0356, n=884信号, p-value约0.3-0.4 (不显著)
  - 置信度低IC=-0.1075, 置信度高IC=0.1685
  - 这意味着当前样本量下，无法用统计学拒绝"IC=0"
```

**问题**：
- Task 3的"IC监控禁用"需要设定统计显著性阈值(p<0.05)
- 但如果IC缓慢从-0.04变为-0.10，样本每增加10笔信号，System仍在继续出血

**PM建议**：
```
不能只依赖30天滚动窗口IC，需要多层监控：
  
  Layer 1 (实时): 每天检查当日胜率
    - 若连续3天胜率<30%, 触发amber alert
    - 连续5天<30%, 暂停该策略
    
  Layer 2 (周期): 每周算rolling IC (7日)
    - 若IC从正变负且跨越-0.05, 触发review
    
  Layer 3 (防守): 无论IC如何，若月度亏损>净值5%, 停止
    - 这是绝对底线，不讨价还价
```

### 场景2：所有策略IC都在0附近波动，"半死不活"
**风险评分：⚠️ P1-HIGH**

**观察**：
```json
// 从ic_analysis_latest.json看
"confidence_high_conf": {"n": 110, "ic_t1": 0.1685},  // 110笔
"confidence_mid_conf": {"n": 449, "ic_t1": -0.0322},  // 449笔  
"confidence_low_conf": {"n": 325, "ic_t1": -0.1075}   // 325笔

// 样本分布极度不均匀！低置信度占36.7%, 中置信度占50.8%
```

**问题**：
- 如果高置信度信号本身就很少（110笔），且该类信号IC虽然0.1685但绝对数小
- 系统可能处于"统计力不足"状态：没有足够高质量信号支撑

**PM建议**：
```
设立"信号质量KPI"：
  - 高置信度信号(confidence>0.70)占比需要>40%
  - 若低于30%, 触发策略重构警报
  - 若低于20%, 暂停实盘并进行系统审计
  
当前比例约11%，已处于警报线以下!
```

---

## 四、PM核心建议

### Q1: 系统是否应继续实盘交易？

**建议：立即暂停实盘，改为观察模式**

**论证**（从$50M基金经验总结）：

| 指标 | 红线(STOP) | 当前系统 | 评价 |
|------|-----------|--------|------|
| 整体IC | >0.05 | -0.0356 | 🔴 失效 |
| 方向正确率 | >52% | LONG 25% | 🔴 反向 |
| HIGH_VOL胜率 | >40% | 14.89% | 🔴 极度恶化 |
| 盈利标的数 | >60% | 35/137 LONG笔 | 🔴 亏损面广 |
| Drawdown | <20% month | 已-$154K / $82K = -188% | 🔴 溃疡 |

**暂停理由**：
1. 信号从"弱"变为"有害"(负IC)
2. 方向选择能力完全失效(LONG反向)
3. VIX环境下策略彻底崩溃，且无自适应机制
4. 继续交易=进一步消耗有限的资本

### Q2: 如果必须运行，设置什么绝对底线？

**PM核心风控"三道防线"**：

```yaml
# 防线1: 每日circuit breaker (无条件触发)
daily_circuit_breaker:
  max_daily_loss: 2000          # 每日亏损>$2K立即停
  max_daily_trades: 3           # 每日最多3笔（谨慎模式）
  max_drawdown: 0.03            # 日内最大回撤>3%停止
  trigger_action: FULL_CLOSE    # 立即全平
  escalation: SMS_ALERT + 电话通知

# 防线2: 周度风险评分 (Policy check)
weekly_governance:
  ic_check:
    window: 7天滚动
    threshold: -0.05
    trigger: 禁用该策略
  winrate_check:
    threshold: <35%
    trigger: 暂停该方向3天
  equity_check:
    max_loss: 5000              # 周亏>$5K, 暂停
    
# 防线3: VIX自适应下调 (实时)
vix_hard_stop:
  vix_28_above: 完全禁止新仓 + limit order平仓
  vix_25_to_28: 仅SHORT允许, 仓位缩减80%
  vix_22_to_25: 仓位缩减50%, 信号×0.5
  vix_below_22: 正常交易
```

### Q3: 观察期与打开油门的条件

**基于20年基金管理经验的建议**：

```
Pre-Relaunch Checklist (严格版)

第一阶段 (7-10天): 评分函数修复
  ✓ 完成Task 2: 评分函数audit + blind forward walk验证
  ✓ 完成Task 3: IC监控+负IC禁用自动化
  ✓ 修复后跑模拟盘对比: 修复前后IC差异>0.20则OK
  
第二阶段 (14天): 谨慎模式观察
  ✓ 切换到 conservative 模式 (已在config)
    - 仓位缩减50% (✓ 已配)
    - 日限3笔 (✓ 已配)  
    - 单笔$10K (✓ 已配)
    - 信号门槛70 (✓ 已配)
  ✓ 每日检查:
    - 当日胜率 vs 历史基准
    - VIX变化速率
    - HIGH_VOL信号拒绝数
    
第三阶段 (30天): 充分验证
  ✓ 14天观察期内无circuit breaker触发
  ✓ 胜率稳定在35-45% range
  ✓ IC从负变为正(目标IC>0.05)
  ✓ HIGH_VOL下胜率>25%
  ✓ 无黑天鹅事件暴露
  
打开油门条件 (ALL):
  ✓ 连续7天未触发daily circuit breaker
  ✓ 周IC>0.08 连续2周
  ✓ LONG胜率>45%, SHORT胜率>50%
  ✓ VIX相关regression通过显著性检验
  ✓ 人工review通过(PM final sign-off)

目标：恢复到"normal"模式
  - 仓位正常化
  - 日限回到5笔
  - 单笔$20K
```

---

## 五、对四个Task的"安全补丁"建议

### Task 1: 信号延迟优化 → **建议冻结**
```
当前风险：IC为负，加速=加速出血

临时方案：
  - 仅提升polling frequency到5秒（已配），不新增功能
  - 关闭ORB快速通道（已设为observe mode）
  - 等待Task 2评分修复后再优化

改进建议：
  a) VIX感知延迟: VIX>25时，polling改为30秒（降噪）
  b) 信号去重: 同一symbol 1分钟内重复信号只count一次
  c) 延迟指标化: 记录signal_generated_time vs order_submitted_time
```

### Task 2: 评分函数修复 → **升级为P0**
```
关键检查项（必须通过）：

1. 时间泄露检测:
   - 在评分时能否访问T日或T+1数据？
   - 需要重新跑纯forward walk: 用T-30:T数据预测T+1:T+5
   
2. 信号强度重校准:
   - 绘制 signal_strength (0-100) vs 实际收益的关系图
   - 若correlation < 0.1，说明强度与收益无关，评分体系失效
   
3. Regime分层验证:
   - 分别计算高波(VIX>25) / 正常(VIX<22) 下的IC
   - 若高波IC<正常IC的50%，需要特殊处理
   
4. Direction bias修正:
   - 去掉"预设LONG偏见"(current LONG conf=0.60)
   - 改为数据驱动: LONG最低0.50, SHORT可0.35
   
建议修复路线：
  - 1人-日：数据audit (时间泄露, 统计分布)
  - 2人-日：新评分算法开发 (交叉验证, forward walk)
  - 1人-日：A/B测试对比 (shadow mode跑7天)
  - 目标：修复后IC ≥ 0.08
```

### Task 3: IC监控 + 负IC禁用 → **立即启用，规则见下**
```python
# 伪代码: IC自动禁用规则

class ICMonitoringEngine:
    def daily_review(self, symbol: str, strategy_id: str):
        ic_7d = self.compute_ic(symbol, strategy_id, lookback=7)  
        wr_7d = self.win_rate(symbol, strategy_id, lookback=7)
        
        # 规则1: 负IC + 低样本
        if ic_7d < -0.05 and self.n_samples < 30:
            self.flag_for_review(symbol, reason="negative_ic_low_sample")
            
        # 规则2: 持续负IC
        if ic_7d < -0.08 or wr_7d < 0.3:
            self.auto_disable(symbol, strategy_id, duration=7)  # 禁用7天
            alert("NEGATIVE_IC", f"{symbol} disabled, IC={ic_7d:.3f}")
            
        # 规则3: 高置信度反向亏损
        if symbol in self.high_confidence_signals:
            if ic_7d < -0.10:
                self.investigate_scoring_bug(symbol)  # 可能是评分函数bug
```

### Task 4: VIX>28 硬拒新仓 → **补充动态阈值**
```yaml
# 改进现有的vix_levels
market_regime_filter:
  vix_levels:
    # 原配置基本可以，补充以下：
    dynamic_adjustment:
      enabled: true
      # VIX上升速度过快时，提前收缩
      vix_5min_velocity: 0.5   # VIX每分钟上升>0.5时，
      velocity_action: reduce_new_entry_50pct  # 新仓减半
      
  # 新增: VIX与持仓duration的联动
  vix_holding_time_adjustment:
    vix_above_30: max_hold_5min      # VIX>30, 5分钟强制平
    vix_25_to_30: max_hold_15min
    vix_below_25: no_limit
```

---

## 六、缺失的"绝对底线"风控清单

### 当前缺失项（Critical）

| 序号 | 风控项目 | 当前状态 | 优先级 |
|------|--------|--------|------|
| 1 | VIX上升速率检测 (>0.5/min) | 无 | P0 |
| 2 | 每日亏损$2K自动全平 | 无 | P0 |
| 3 | Spread动态监控 (盘中spread突增) | 无 | P0 |
| 4 | 单标的日亏损guard (已有但未测试) | 配置 | P1 |
| 5 | HIGH_VOL下自动降信号强度50% | 有但无数据 | P1 |
| 6 | VIX缓存新鲜度校验 (mtime) | 已修复 | ✓ |
| 7 | 黑天鹅事件检测 (新闻突增) | 无 | P2 |
| 8 | 交易延迟监控 (5min规则) | 无 | P2 |

### 建议新增风控三层架构

```
Layer 1: Real-time Circuit Breaker (交易执行前)
  - Spread check: bid-ask >20bp → limit order
  - VIX velocity: 5min涨幅>15% → 全平 + 4h禁新仓
  - Liquidity check: bid volume<$50K → 不交易
  - Heat check: 该symbol 5min内亏损>1% → 禁交易该symbol 1h

Layer 2: Daily Risk Monitor (每小时检查)
  - Equity check: 若day_loss > $2K → full circuit break
  - Win rate check: 若最近10笔<20% → 禁新仓
  - Drawdown check: 若intraday_dd > 3% → reduce position 50%

Layer 3: Weekly Governance (每周review)
  - Strategy IC check: 若rolling IC < -0.05 × n_samples → disable
  - Confidence calibration: 若confidence与收益不相关 → rebalance
  - Regime analysis: 若HIGH_VOL胜率 < 20% → special handling
```

---

## 七、风控可行性评分及改进路线图

### 综合风控评分：3.5/10 ❌

| 维度 | 评分 | 评论 |
|------|-----|------|
| 信号质量 | 2/10 | IC=负, LONG反向, 评分体系失效 |
| 市场环境适配 | 3/10 | HIGH_VOL崩溃，阈值太高(28) |
| 风险管理 | 4/10 | 日限有但过松(3-5), 缺VIX速率检测 |
| 运维透明性 | 6/10 | 数据完整，但监控不够实时 |
| 冗余机制 | 2/10 | 无多层circuit breaker, 无热备 |
| **综合** | **3.5/10** | **不适合实盘** |

### 改进路线图（优先级排序）

```
立即行动 (今天):
  [ ] 切换到 conservative mode (日限3, 仓位-50%)
  [ ] 暂停ORB快速通道 (已在observe)
  [ ] 启用daily circuit breaker: 亏损$2K全平
  
P0 紧急修复 (3-5天):
  [ ] Task 2: 评分函数audit + forward walk验证
      完成标志: IC从-0.0356提升至>0.05
  [ ] Task 3: IC监控规则自动化 (负IC>-0.08自动禁)
  [ ] VIX动态降低至25 (而非28) 作为首道防线
  
P1 加强防御 (5-10天):
  [ ] 新增VIX速率检测 (>0.5/min时warning)
  [ ] 新增Spread监控 (盘中spread>15bp降仓50%)
  [ ] HIGH_VOL下信号强度自动×0.3 (目前无)
  [ ] 统计学IC显著性检验 (p<0.05才启用)
  
P2 优化基建 (10-14天):
  [ ] 多层circuit breaker (Layer 1/2/3)
  [ ] 黑天鹅事件检测 (新闻突增)
  [ ] 交易延迟监控 (T_order - T_signal > 5min)
  [ ] Dashboard实时风控看板

重新评估节点 (第15天):
  - 若IC仍<0, 建议停止研发Task 1-4, 全面重构信号层
  - 若IC>0.05且HIGH_VOL胜率>25%, 可考虑normal mode
  - 若仍无改善，建议系统下线，转向策略重新设计
```

---

## 八、PM的最终建议

### 核心观点

我作为在$50M+实盘基金管理过10+年市场极端事件的PM，给出三个严肃的建议：

**1. 不要在IC为负时优化速度**
- 现在加快信号延迟，相当于在崖边加油门
- 优化的前提必须是IC为正(>0.05)或至少可验证
- Task 1必须冻结直到Task 2评分修复完成

**2. VIX>25环境必须特殊对待，不能用单一阈值**
- 当前VIX环保下胜率只有14.89%, 亏损占总亏损的271%
- 不能指望30-50笔交易的IC报告精准指导，需要建立hard stop
- 建议立即切换: VIX>25时信号强度×0.3，VIX>28禁新仓

**3. 人工复审比流程自动化更重要**
- 在评分体系未经验证前，每笔>$5K的交易应该人工看一眼
- 建议启用"half-automatic mode": 系统生成信号，人工filter then execute

### 最终评分

**现在打开油门的代价（用100美元基线）**：
```
Scenario A: 继续全自动交易（current state）
  预期月度亏损: $15-20K (基于HIGH_VOL占比)
  破产时间: ~4-5个月
  
Scenario B: 立即暂停，修复+观察14天
  预期修复成本: 1人-周工作量 + 14天数据等待
  修复后预期收益: +$5-8K/月
  ROI: 1000-1600%
  
Scenario C: 转向 conservative mode + daily circuit breaker  
  立即成本: 日收益-20-30% (但减少亏损)
  预期月度亏损: $3-5K (instead of $20K)
  存活时间: 24个月 (instead of 4个月)
```

**PM最终建议：Scenario B+C组合**
- 今天切换conservative mode (减少亏损速度)
- 同时并行Task 2-3修复 (根本解决)
- 14天后若IC>0.05, 逐步回到normal mode
- 若IC仍<0, 考虑系统暂停

---

## 附录：具体配置修改建议

```yaml
# 立即改动的配置项

# 1. VIX阈值下调
market_regime_filter:
  vix_levels:
    critical: 25  # 从28→25, 提前触发硬止

# 2. 谨慎模式激活
dual_mode:
  default_aggression: conservative

# 3. Daily circuit breaker新增
risk_limits:
  daily_circuit_breaker:
    max_daily_loss: 2000      # 亏损$2K全平
    max_daily_trades: 3       # 日限3笔
    max_intraday_drawdown: 0.03  # 3%回撤全平

# 4. VIX监控增强
data_sources:
  twelvedata:
    cache_ttl:
      vix: 30  # 从60→30秒，更频繁刷新

# 5. Task 2完成前禁用Task 1
strategies:
  fast_lane:
    enabled: false  # 冻结直到评分修复
```

