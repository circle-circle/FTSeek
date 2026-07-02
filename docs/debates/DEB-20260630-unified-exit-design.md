# DEB-20260630: 统一退出策略设计辩论

## 背景

设计了一套基于股票分层+波动率的针对性SL/TP统一退出策略(见docs/design/unified-exit-strategy.md)。需要验证设计方案是否可行，是否有遗漏的矛盾。

## 目标

1. 验证"波动率桶动态选择SL multiplier"在ExitMonitor中是否可实现
2. 验证vol_buckets(盘前SL生成)和exit_policy(盘中SL执行)的multiplier是否需要对齐
3. 识别设计中的实现盲点和风险

## 数据与实证

### 核心矛盾：两套multiplier完全不同

| 计算 | vol_buckets(盘前) | exit_policy(盘中) |
|------|-------------------|-------------------|
| 公式 | sl_pct = max(floor, min(ceil, atr_pct × sl_multiplier)) | SL_price = entry ± atr_mult × ATR |
| medium ATR%=2% | max(1.8, min(2.8, 2.0×0.7))=1.8% | 2.0×2.0%=4.0% |
| high ATR%=5% | max(2.5, min(5.0, 5.0×0.6))=3.0% | 2.0×5.0%=10.0%→cap 2.0% |

**问题**: 盘前instruction生成SL=1.8%(vol_buckets), 盘中ExitMonitor用atr_stop=4.0%覆盖 → SL从1.8%放宽到4.0% → SL契约触发(initial_sl=1.8%是floor) → 最终SL=1.8%

**结论**: SL契约保护了initial_sl不被覆盖, 但ExitMonitor的atr_stop计算是多余的 → initial_sl才是真正生效的SL

### 代码验证

- ExitMonitor的atr_stop_engine.py不支持按波动率桶动态选择multiplier(line 239: atr_mult=2.5固定)
- pre_close.py的close_profit_atr/half_exit_below_atr从pc_cfg全局读取(line 116/171), 不支持按桶动态选择
- exit_policy.yaml不支持vol_bucket_overrides字段
- vol_buckets的sl_multiplier(0.6-0.8)和exit_policy的atr_mult(2.0)是完全不同的计算方式

### PF分解

| 方向 | PF | 胜率 | avg_win | avg_loss |
|------|----|------|---------|----------|
| LONG | 0.73 | 27.3% | $377 | -$195 |
| SHORT | 2.00 | 43.0% | $596 | -$226 |

### 当前持仓波动率分层

| 持仓 | ATR% | 桶 | 当前initial_sl | SL% |
|------|------|----|--------------|-----|
| BIDU | 3.70% | high | $111.18 | 1.8% |
| IWM | 1.99% | medium | $303.24 | 2.1% |
| MSFT | 3.47% | medium | $378.21 | 2.1% |
| SNDK | 9.88% | extreme | $2,130.16 | 1.8% |
| UNH | 2.35% | medium | $425.82 | 2.1% |

### 设计方案(4桶)

| 桶 | ATR% | SL倍数 | max_loss% | close_profit_atr | half_exit |
|----|------|--------|-----------|-----------------|-----------|
| low | <1.5% | 1.5× | 1.5% | 1.5 ATR | 0.3 ATR |
| medium | 1.5-3.5% | 2.0× | 2.0% | 2.0 ATR | 0.5 ATR |
| high | 3.5-8% | 2.5× | 3.0% | 2.5 ATR | 0.8 ATR |
| extreme | >8% | 3.0× | 4.0% | 3.0 ATR | 1.0 ATR |

## 讨论记录

### 第 1 轮：hermes

**立场：设计方向正确，但实现路径需要调整 — 不是改ExitMonitor，而是统一到vol_buckets**

**论点1: ExitMonitor不支持动态multiplier → 改ExitMonitor成本高**

代码验证: atr_stop_engine.py的atr_mult是固定参数(line 239), 不按波动率桶选择。要实现动态multiplier需要:
- 改atr_stop_engine: 按ATR%选桶 → 选multiplier
- 改pre_close.py: 按ATR%选桶 → 选close_profit_atr/half_exit
- 改exit_policy.yaml: 加vol_bucket_overrides字段
→ 改3个文件, 工程量大, 风险高

**替代方案: 让initial_sl成为唯一SL, ExitMonitor的atr_stop只作为hard_stop上限**

initial_sl由vol_buckets在开仓时计算(已经按桶分层), 写入DB后不被覆盖(今天已修复position_sync)。ExitMonitor的atr_stop改为纯hard_stop(max_loss_pct), 不再用multiplier×ATR计算SL。

即:
- **SL = initial_sl**(vol_buckets计算, 按桶分层) → 已有, 不需改
- **hard_stop = entry × max_loss_pct**(exit_policy, 按桶) → 需要改max_loss_pct按桶
- **pre_close = close_profit_atr**(全局, 不按桶) → 当前够用, 暂不改

**论点2: vol_buckets的sl_multiplier和设计方案的"SL倍数"是不同概念**

设计方案写"SL倍数=2.0×"指的是atr_mult(SL_price = entry ± 2.0×ATR)。但vol_buckets用的是sl_multiplier(sl_pct = atr_pct × 0.7)。两者不等价:
- atr_mult=2.0, ATR%=2% → SL=4%
- sl_multiplier=0.7, ATR%=2% → SL=1.4%→floor 1.8%

**修正: 设计方案应该用vol_buckets的sl_multiplier概念, 不是atr_mult**

设计方案的4桶应该改为:

| 桶 | ATR% | sl_multiplier | sl_floor% | sl_ceil% | short_widen% |
|----|------|--------------|-----------|---------|------------|
| low | <1.5% | 0.8 | 1.0 | 1.5 | 0.2 |
| medium | 1.5-3.5% | 0.7 | 1.5 | 2.0 | 0.3 |
| high | 3.5-8% | 0.6 | 2.0 | 3.0 | 0.5 |
| extreme | >8% | 0.5 | 2.5 | 4.0 | 0.8 |

**论点3: extreme桶是必要的**

ARM(ATR%=11%)和SNDK(ATR%=10%)在当前high桶(sl_ceil=5.0%)下SL=5%, 但今天ARM亏损6.3%、TSLA亏损7.5% → SL不够宽。extreme桶(sl_ceil=4.0%→实际sl_pct=max(2.5, min(4.0, 11×0.5))=4.0%)比high桶更宽。

但: extreme桶的sl_multiplier=0.5 → ATR%=11% → sl_pct=5.5%→ceil 4.0% → SL=4.0% → 仍可能不够(ARM今天亏6.3%)。

**问题: SL再宽也挡不住方向错误 → 信号层问题不在退出层修**

**行动项**:
- [P0] 修正设计方案: 用sl_multiplier概念替代atr_mult, 4桶参数对齐vol_buckets
- [P0] 确认实现路径: initial_sl是唯一SL(vol_buckets), ExitMonitor只做hard_stop上限
- [P1] extreme桶新增到vol_buckets
- [P2] pre_close按桶动态选择 → 暂不做, 当前全局close_profit_atr=2.0够用

### 第 2 轮：hermes（自验证修正）

**验证发现: get_atr_multiplier已有非线性状态机(5档利润分级)**

exit_policy_guardian.py:195: multiplier按profit_atr分级:
- profit<0.5: 2.0×(initial) → 亏损时SL最宽
- 0.5≤profit<1.0: 1.8× → 微利收紧
- 1.0≤profit<2.0: 1.6× → 盈利收紧
- 2.0≤profit<5.0: 1.4× → 大盈利更紧
- profit≥5.0: 1.2× → 锁利

**但: 这个非线性状态机被initial_sl约束覆盖了!**

SL契约(line 1654): _fixed_sl = max(_fixed_sl, _floor_sl) (LONG)
→ atr_stop(非线性) 和 initial_sl 取更紧的 → initial_sl生效

**核心矛盾: 盘前ATR vs 盘中ATR差异巨大**
- BIDU: 盘前ATR=2.81, 盘中ATR=4.19 → 差49%
- MSFT: 盘前ATR=7.41, 盘中ATR=12.84 → 差73%
→ initial_sl基于盘前ATR算, 盘中波动更大 → SL可能太紧

**修正论点1: 不应该让initial_sl成为唯一SL → 应该用盘中ATR动态计算**

但: 去掉initial_sl约束 → atr_stop(2.0×ATR)在高波动股会太宽(ARM: 2.0×36.62=73.24 → SL=22%)
→ 需要max_loss_pct硬上限 → 当前2.0%太紧(ARM今天亏6.3%)

**修正方案: 按波动率桶选择max_loss_pct, atr_stop用盘中ATR**

| 桶 | ATR% | atr_mult | max_loss% |
|----|------|----------|-----------|
| low | <1.5% | 2.0× | 1.5% |
| medium | 1.5-3.5% | 2.0× | 2.0% |
| high | 3.5-8% | 2.0× | 3.0% |
| extreme | >8% | 2.0× | 4.0% |

→ atr_mult固定2.0×(不按桶), max_loss_pct按桶(控制上限)
→ 盘中ATR动态计算SL, 但不超过max_loss_pct
→ initial_sl仍作为floor(防盘中ATR异常低时SL太松)

**修正论点2: vol_buckets的sl_multiplier概念不需要改**

vol_buckets(盘前SL生成)和exit_policy(atr_stop)是两套独立计算:
- vol_buckets → initial_sl (floor, 盘前)
- atr_stop → current_sl (运行时, 被initial_sl约束)

两者不需要"对齐multiplier" → 它们是不同层的不同计算, SL契约负责取更紧的

**修正论点3: extreme桶的max_loss_pct=4%不足以覆盖ARM的6.3%亏损**

ARM方向错误(做空后涨6.3%), max_loss=4%会在4%触发止损 → 比今天6.3%好(少亏2.3%)
但: 如果max_loss=6% → 在6%触发 → 和今天差不多
→ max_loss不是越高越好, 4%是合理的止损上限

**最终修正行动项**:
- [P0] exit_policy.yaml: max_loss_pct从固定2.0%改为按波动率桶(1.5/2.0/3.0/4.0%)
- [P0] 新增extreme桶(ATR%>8%)
- [P1] 不改atr_mult(固定2.0×), 不改vol_buckets(盘前SL生成不变)
- [P1] 不去掉initial_sl约束(仍作为floor)
- [P2] pre_close按桶动态选择 → 暂不做

### 验证补充：ATR限幅+hard_stop+滑点

**ATR限幅(position_monitor.py:333-342)**:
- min_atr_pct=0.5%, max_atr_pct=2.5%
- ARM ATR%=11% → 被截断到2.5% → effective_atr=$8.30
- atr_stop = 332.05 + 2.0×8.30 = $348.65 (5.0%)
- hard_stop = 332.05 × 1.02 = $338.69 (2.0%)
- SL = min(atr_stop, hard_stop) = $338.69 (2.0%)

**滑点问题**:
- ARM: SL=$338.69(2%), exit_price=$353.01 → 滑点$14.32(4.3%)
- TSLA: SL=$397.96(3.1%), exit_price=$414.72 → 滑点$16.76(4.2%)
- → 即使max_loss_pct按桶放到4%, 滑点4%仍然会放大亏损
- → 滑点是执行层问题, 不是SL参数问题

**结论: 统一退出策略的参数设计不是核心问题, 滑点才是**

### 滑点根因: 不是限价单, 是scan频率

**_close_position(line 4395-4397)**:
- 平仓用LIMIT单, 限价=current_price±2% → 限价单本身没问题
- 但: current_price是ExitMonitor检测时的价格, 已经超SL了!

**ARM案例**:
- SL=$338.69(2%) → 应在$338.69触发
- 但ExitMonitor每15秒scan一次 → 价格从$338跳到$353(15秒内涨4.5%)
- 检测时已经是$353.01 → 限价=360.07 → 成交=353.01
- → "滑点"实际是scan频率不足, 不是执行问题

**设计修正: 按波动率桶调整scan频率**
- low/medium: 15秒(当前)
- high: 10秒
- extreme: 5秒

→ 但: scan频率受系统性能限制, 5秒scan 5个持仓=1秒/个 → 可能太紧
→ 替代: broker端STOP单(不依赖软件scan)

### 收敛结论（单agent fallback, CRITIC验证中）

**核心发现: 滑点根因是scan频率(30秒), 不是SL参数**

monitor_interval=0.5分钟=30秒 → ExitMonitor每30秒scan一次
ARM ATR%=11% → 30秒内可能波动4.5% → SL触发时价格已远超SL

**统一退出策略最终方案**:

1. SL参数: 维持当前架构
   - initial_sl(vol_buckets, 盘前) 作为floor
   - atr_stop(2.0×ATR, 盘中) + max_loss_pct(2.0%) 作为hard_stop
   - SL契约: 取更紧的
   
2. max_loss_pct按波动率桶: 需要改atr_stop_engine.py
   - low(<1.5%): 1.5%
   - medium(1.5-3.5%): 2.0%
   - high(3.5-8%): 3.0%
   - extreme(>8%): 4.0%
   
3. scan频率按波动率桶: 需要改position_monitor.py
   - low/medium: 30秒(当前)
   - high: 15秒
   - extreme: 10秒
   
4. pre_close参数: 维持全局close_profit_atr=2.0(暂不按桶)
5. TP: rr_ratio=999(禁用, pre_close负责盈利落袋)
6. VIX叠加: 已启用(只VIX, regime防double-reduce)

**行动项**:
- [P0] max_loss_pct按波动率桶: 改atr_stop_engine.py, 从exit_policy.yaml读固定值改为按ATR%选桶
- [P0] 新增extreme桶到vol_buckets(playbook_horizon.yaml)
- [P1] scan频率按波动率桶: 改position_monitor.py, 按持仓ATR%动态调整sleep间隔
- [P2] pre_close按桶动态选择 → 暂不做, 当前全局值够用
- [P2] broker端STOP单 → 替代软件scan, 但需要改broker_adapter

### 第 1 轮：claude-code (CRITIC)

**论点1修正: atr_mult改造成本言过其实**
resolve_trail_multiplier只解析chandelier trail, 与atr_stop无关。atr_stop的mult来自policy_dict.exit_rules.atr_stop.multiplier_by_profit.initial(L1644), 是dict取值, policy_guardian可动态注入。改成按vol_bucket取mult只需改1行policy查询逻辑。

**论点2反驳: initial_sl作为唯一SL是退步**
initial_sl在盘前用盘前ATR计算, 盘中ATR飙升时不调整。若仅用initial_sl:
- 波动率暴涨→SL过紧被扫损(假止损)
- 波动率暴跌→SL过松放任回撤
atr_stop的价值在于随盘中ATR动态调整。提议"atr_stop仅作hard_stop上限"会丢弃动态调整能力, 是退步。

**论点3反驳: extreme桶方向错误**
ARM亏6.3%>SL=5.5%, 若为跳空低开则任何SL无效, 加宽SL到8%只会亏更多。extreme桶需要的是信号过滤(高波动品种降仓/放弃), 而非无脑放宽SL。

**修正建议**: 保留atr_stop动态机制, 让atr_mult按vol_bucket从policy动态取值(改1行), 既保留动态调整又实现分层。

### 收敛: Hermes接受CRITIC全部修正

1. ✅ 接受: atr_mult可以按桶动态取值(改1行policy查询) — 不需要"initial_sl作为唯一SL"
2. ✅ 接受: 保留atr_stop的盘中动态调整能力 — initial_sl仍作为floor, 但atr_stop负责动态SL
3. ✅ 接受: extreme桶不改SL宽度, 改为信号过滤(高波动品种降仓)

**最终统一退出策略**:
- atr_mult按vol_bucket动态取值: low=1.5×, medium=2.0×, high=2.5×, extreme=3.0×
- max_loss_pct按桶: low=1.5%, medium=2.0%, high=3.0%, extreme=4.0%
- initial_sl仍作为floor(防盘中ATR异常低)
- atr_stop(盘中ATR×桶multiplier) + hard_stop(max_loss_pct) → 取更紧的
- SL契约: min/max(atr_stop, initial_sl) → 保留动态调整+floor保护
- pre_close: loss_flat=true + close_profit_atr=2.0(全局, 暂不按桶)
- extreme桶: 不是放宽SL, 而是降低仓位(vix_position_mult×bucket_position_mult)

### 第 2 轮：claude-code (orchestrator)

当前设计仍把 atr_mult 与 max_loss_pct 两套参数混为一谈，易在实盘中顾此失彼。按 vol_bucket 动态取 mult 虽改动小，但桶边界与 atr_stop 耦合未显式定义，盘中 ATR 跳变可能频繁跨桶。建议将桶判定收敛到单一 source（如 vol_buckets service），并在 sim 中验证跨桶抖动与滑点影响。

### 第 3 轮：hermes (接受Claude Code R2建议)

**接受: 盘中ATR跳变跨桶问题**

Claude Code指出: ATR在桶边界(3.5%/8%)附近波动时, atr_mult频繁切换(2.0×→2.5×→2.0×) → SL不稳定 → 可能导致假触发。

**修正: 加滞回区间(hysteresis)**

桶切换加2%滞回: 
- 从medium升到high: ATR%需≥3.5%+0.2%=3.7%
- 从high降回medium: ATR%需≤3.5%-0.2%=3.3%
- 防止边界抖动导致频繁切换

**实现**: 在get_atr_multiplier中记录上次桶, 加滞回判断

**接受: 桶判定收敛到单一source**

当前有两个桶判定:
- vol_buckets(盘前, instruction_generator) → 算initial_sl
- exit_policy(盘中, ExitMonitor) → 算atr_stop
→ 应该用同一个函数判定桶

**实现**: 抽取_get_vol_bucket(atr_pct)公共函数, 两处调用

**接受: sim验证跨桶抖动**

明天盘前生成指令后, 检查:
- 每个持仓的盘中ATR%是否在桶边界附近
- 是否有频繁跨桶的现象
- 如果有 → 加滞回区间

**收敛: 双方一致, 辩论结束**

最终方案:
1. atr_mult按桶: low=1.5×/medium=2.0×/high=2.5×/extreme=3.0×
2. max_loss_pct按桶: 1.5%/2.0%/3.0%/4.0%
3. 桶判定加滞回区间(±0.2%)
4. _get_vol_bucket(atr_pct)公共函数
5. initial_sl仍作为floor
6. extreme桶降仓(不放宽SL)
7. pre_close: loss_flat=true + close_profit_atr=2.0
