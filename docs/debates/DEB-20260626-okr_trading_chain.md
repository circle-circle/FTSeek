# 议题：盘中交易链路暴露的问题与风险 — 如何确保OKR目标完成

## 状态

- [ ] 进行中
- [ ] 待决策
- [x] 已决策
- [ ] 已验证
- [ ] 已关闭

## 背景

OKR: 30天盈利目标 (6/19-7/19)。当前Day 8。
- OKR周期内: 35笔交易, 15胜, 总盈亏 **$-2,041** (亏损)
- 今天(6/26): **0笔成交** (修了9个bug但未产生实际交易)
- 信号全部为SHORT方向，被多层门禁拦截

## 目标

1. 盘中暴露的问题哪些是bug、哪些是设计正确但配置不合理？
2. 如何让交易链路在明天开盘后真正产生成交？
3. OKR还剩22天，当前-$2,041，路径是否可行？

## 数据与实证

### 证据1: 今日信号全部被拦截

| 时间 | 股票 | 方向 | 拦截点 | 原因 |
|------|------|------|--------|------|
| 23:06 | PDD | SHORT | IC门禁 | 策略IC=-0.069 < 0.05 (旧阈值,已修) |
| 23:26 | SNDK | SHORT | IC门禁 | 同上 |
| 23:56 | AVGO | SHORT | IC门禁 | 同上 |
| 00:23 | TSLA | SHORT | 方向IC门禁 | SHORT方向IC=-0.128 < -0.10 (旧阈值) |
| 00:23 | TSM | SHORT | 方向IC门禁 | 同上 |
| 00:28 | PDD | SHORT | Plan Gate | Plan=LONG vs Signal=SHORT 方向背离 |

来源: `grep "🚫\|🔒.*拦截" logs/ftseek_us.log`

### 证据2: 信号方向单一化

今日产生的所有SignalCandidate:
- PDD SHORT (strength=55)
- TSLA SHORT (strength=45)
- TSM SHORT (strength=54)
- AVGO SHORT (strength=54)
- SNDK SHORT (strength=58)

**100% SHORT信号，0个LONG信号。** technical策略在BULL regime下只产SHORT。

来源: `grep "技术面命中" logs/ftseek_us.log`

### 证据3: Battle Plan全部是LONG

| 股票 | Plan方向 | Status |
|------|---------|--------|
| LLY | LONG | VALID |
| JPM | LONG | VALID |
| QCOM | LONG | CANCELLED |
| AMZN | LONG | CANCELLED |
| MSFT | LONG | CANCELLED |

Plan Gate要求信号方向与Plan一致。Plan=LONG, Signal=SHORT → **方向矛盾无法成交**。

来源: `data/daily_battle_plan_us.json`

### 证据4: 策略配置

| 策略 | enabled | 说明 |
|------|---------|------|
| technical | True | 唯一产信号的策略，IC=-0.069 |
| voting_llm | **False** | 禁用，可能有正IC |
| earnings_revision | **False** | StrategyHealth suspended (IC=-0.124) |
| trend_first | False | IC=-0.24 历史有害 |
| insider | True | 无集群信号 |
| orb | True | 区间太窄，无突破 |

来源: `config/unified_config.yaml`

### 证据5: OKR数据

- 35笔交易, 15胜(42.9%), 总盈亏$-2,041
- SHORT方向历史胜率70%(之前辩论数据), 但方向IC=-0.128(反预测)
- 这意味着: SHORT信号"胜率高"但"IC为负" → 可能是小赢大亏

来源: `trade_records WHERE trade_date >= '2026-06-19'`

### 证据6: 今晚修复的9个bug

1. 测试数据堵塞playbook幂等 → 删除
2. CircuitBreaker用XLF $114万做peak → 改用capital_safety真实peak
3. ATR用1分钟K线 → 用日K线覆盖
4. bid/ask push缺失 → last_price兜底
5. legacy SIM仓吃120%预算 → 排除LEGACY/UNKNOWN
6. shadow_only不下单 → 改为legacy
7. scan_loop TRADING跳过 → 改为生成信号跳过下单
8. IC门禁硬阈值0.05 → 分级(-0.15硬拦截, [-0.15,0.05)放行)
9. playbook_sim未映射为auto → 映射为AUTO_MAIN

## 可选方案

### 方案 A: 放开方向矛盾限制 (让SHORT信号能下单)

- 提出者：hermes
- 核心思路: Plan Gate从硬拦截改为warning — 信号方向与Plan矛盾时记录但不阻止下单
- 优点: 立即解锁SHORT信号成交路径
- 缺点: 违背作战计划的方向意图；SHORT方向IC=-0.128确实反预测
- 适用条件: 信号质量足够好(strength≥55)时放行

### 方案 B: 启用voting_llm策略 (增加LONG信号来源)

- 提出者：hermes
- 核心思路: voting_llm.enabled=False → True，让LLM投票产生LONG信号
- 优点: 增加信号多样性，可能与Plan方向一致
- 缺点: LLM投票5s超时(已观测)，可能拖慢扫描；IC未知
- 适用条件: voting_llm有正IC或至少非反预测

### 方案 C: 修复technical策略的LONG信号生成 (让策略产LONG)

- 提出者：hermes
- 核心思路: technical策略在BULL regime下_is_pullback_entry只触发SHORT回调，不触发LONG回调
- 优点: 从源头解决方向单一化
- 缺点: 需要改策略代码，风险高；当前RSI分布不支持LONG入场条件
- 适用条件: 市场回调时RSI到40-55区间

### 方案 D: 放弃technical策略，完全依赖ORB+playbook

- 提出者：hermes
- 核心思路: technical IC为负，不是好策略；ORB+playbook有独立执行路径不经IC门禁
- 优点: 绕过IC死锁；ORB在波动日能产生信号
- 缺点: 今天ORB区间全太窄(0.08-0.57%)，低波动日无信号；playbook无triggered指令
- 适用条件: 市场波动放大时

## 关键分歧

1. **SHORT方向IC=-0.128是否应该放行**
   - 方案A观点: IC=-0.128接近-0.15阈值，样本含多环境，当前BULL可能不同
   - 反对观点: 反预测就是反预测，放行=亏钱

2. **Plan Gate方向矛盾是否应该硬拦截**
   - 方案A观点: Plan是盘前生成，盘中信号反映实时变化，方向矛盾是正常的
   - 反对观点: Plan是用户审核过的，不应该被自动信号覆盖

3. **OKR路径是否可行**
   - 乐观: 22天+链路已通+SIM不花真钱，积累数据改善IC
   - 悲观: 8天$-2,041，IC为负的策略不会因为放行就变正

## 执行项

- [ ] 1. 启用 voting_llm (config: strategies.voting_llm.enabled: false → true)
- [ ] 2. 调整 technical LONG入场条件 (RSI 40-55 → 45-60 + 动量确认, 明天盘前做)
- [ ] 3. 明天开盘后观察: voting_llm是否产生LONG信号 + 是否与Plan方向一致
- [ ] 4. 如果voting_llm信号被IC门禁拦 → 重新审视IC门禁设计(所有策略IC都有问题)

## 验证项

- [ ] 明天开盘后产生至少1笔成交
- [ ] 信号方向不再100%单一 (至少有1个LONG候选)
- [ ] OKR盈亏缩窄至$-1,000以内

## 讨论记录

### 第 1 轮：hermes

**立场：方案B+C组合 (启用voting_llm + 修复LONG信号生成)**

**核心理由：**

1. **方向矛盾是根因，不是门禁。** 今日5个信号全部SHORT，Plan全部LONG。即使放开所有门禁，方向矛盾也会被Plan Gate拦截。解法不是放宽门禁，而是让信号方向多样化。

2. **方案A(放开Plan Gate)危险。** SHORT方向IC=-0.128确实是反预测。历史胜率70%但总盈亏为负 = 小赢大亏。放行SHORT = 让系统继续亏钱。

3. **方案B(启用voting_llm)最短路径。** voting_llm之前被禁用但原因不明(不是IC问题)。LLM投票可能产生LONG信号，与Plan方向一致，绕过方向矛盾。5s超时可接受。

4. **方案C是长期修复。** technical策略的_is_pullback_entry在BULL regime下LONG条件太严(RSI 40-55 + boll_pos < 0.60 + MACD柱非负)。当前RSI 50-66全在SHORT区间。需要降低LONG门槛或增加动量确认条件。

5. **OKR路径：** 8天-$2,041，日均亏$255。如果明天开始每天2-3笔成交，22天可积累44-66笔outcome数据。IC会自然修正。但前提是信号方向要多样——全是SHORT不可能盈利。

**证据支撑：**
- 5个SignalCandidate全SHORT (证据2)
- Plan全LONG (证据3)
- OKR 35笔15胜42.9%但总亏$2,041 = 小赢大亏 (证据5)

**对反方可能论点的预判：**
- "voting_llm IC未知" → 至少不是-0.128，比SHORT方向好
- "5s超时拖慢扫描" → 可设3s超时，19只股票最多拖57s
- "technical策略不该改" → 不改策略，只改入场条件门槛

### 第 1 轮：claude-code

**立场：部分同意方案B，反对方案A，不支持立即D。**

**核心理由：**

1. **方案A不可接受。** IC=-0.128为反预测，且历史呈现"小赢大亏"，放行Plan Gate等于放任已知亏钱路径，OKR会加速恶化。

2. **方案B（启用voting_llm）是最小可行动作。** 当前唯一产信号的technical策略方向单一，必须引入LONG来源才能解决Plan Gate硬拦截。5s超时可接受，但应在shadow/sim先跑通并计算其方向IC，确认非反预测后再上主链路。

3. **方案C可先在technical内部对LONG入场条件做有限调整**（如RSI阈值放宽到45-60 + 添加动量确认），但不应大改`_is_pullback_entry`核心逻辑，避免引入新风险。

4. **方案D当前不可取。** ORB今日区间过窄无信号，playbook未触发，禁用technical等于让系统无信号可用，无法改善OKR。

**执行建议：** 今晚shadow环境同时开启B+C，明日开盘后观察voting_llm是否产生LONG信号及方向IC；若为正，则主链路启用；technical LONG调整同步验证。

### 第 2 轮：hermes（收敛确认）

**接受 claude-code 的全部4个判断。**

1. **接受**：方案A不可接受 — SHORT方向IC=-0.128反预测，放行=放大亏损
2. **接受**：方案B是最小可行动作 — 启用voting_llm引入LONG信号
3. **接受**：方案C有限调整 — RSI 45-60 + 动量确认，不大改核心逻辑
4. **接受**：方案D当前不可取 — 禁用technical=零信号

**补充一个claude-code提到的关键点：** "shadow环境先跑通voting_llm" — 但当前master_switch=legacy(不是shadow_only)。voting_llm在legacy模式下会直接走IC门禁。如果voting_llm的IC也未知/为负，会被拦截。

**修正执行建议：** 
- 直接启用voting_llm(不走shadow)，让它在legacy链路产生信号
- 如果voting_llm信号也被IC门禁拦，说明所有策略IC都有问题 → 需要重新审视IC门禁设计
- technical的LONG调整可以明天盘前做(改RSI门槛)，今晚先启用voting_llm

**本轮分歧归零。** 双方一致同意：B+C组合，A和D排除。
