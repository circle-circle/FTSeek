# Agent-vs-Agent Debate: 基于成熟量化项目经验的FTSeek-US改进

## 状态
- [x] 进行中
- [x] 已决策
- [x] 已验证
- [ ] 已关闭

## 背景

FTSeek-US当前状态 (6/27四场辩论修复后):
- 58条指令, 36条closed (55.6%WR), net PnL +$624
- **Profit Factor = 0.99** (gross profit $4,158 / gross loss $4,213)
- **Win/Loss ratio = 0.74** (avg win $208 / avg loss $281)
- **Expectancy = -$1.52/笔** (负期望!)
- BUY: 50%WR, PF=0.53, wl_ratio=0.53 (严重负期望)
- SHORT: 70%WR, PF=6.5, wl_ratio=1.86 (强alpha)
- 最大连续亏损: 3笔
- SL_ATR_STOP: 8笔全亏 -$2,847
- EOD_FORCE_CLOSE: 7笔 100%WR +$1,423

**核心问题**: PF=0.99 + 负期望 → 当前策略长期运行必然亏损。6/27修复了仓位和SL参数, 但没解决根本的盈亏比问题。

## 数据与实证

### 当前系统 vs 成熟量化项目对比

| 维度 | FTSeek-US现状 | 成熟项目(freqtrade/nautilus/backtrader) | 差距 |
|------|-------------|--------------------------------------|------|
| **仓位管理** | 固定1% risk_per_trade | Kelly公式或volatility-adjusted | 无动态调整 |
| **退出策略** | 4规则(固定SL+保本+时间+EOD) | ATR动态SL+trailing+分批止盈 | trailing已禁用(亏$3,773) |
| **止盈** | rr=1.0 (TP=SL) | 动态R:R based on ATR/regime | TP很少触发, 全靠EOD |
| **信号验证** | IC(T+1)有, T+3/T+5积累中 | Walk-forward + out-of-sample | 有walk_forward引擎但未跑 |
| **风控** | water_level+熔断+VIX | drawdown-based position scaling | 有, 但HIGH_VOL=0.3可能过保守 |
| **方向决策** | 3模型投票(qwen/deepseek/glm) | 通常用量化因子, LLM较少 | LLM投票是创新但未验证alpha |
| **回测** | walk_forward引擎存在 | freqtrade有完整回测+优化 | 引擎存在但未用于参数优化 |
| **数据质量** | 6维度但IC backfill坏了 | 实时IC + factor decay tracking | IC backfill刚修复 |

### 关键发现: Profit Factor < 1.0 是根本问题

PF=0.99意味着每投入$1风险, 平均收回$0.99。长期必然亏损。

**分解**:
- SHORT: PF=6.5 (极强alpha, 但只有10笔样本)
- BUY: PF=0.53 (严重亏损, 26笔)
- SL: 8笔全亏 -$2,847 (SL太紧或入场时机差)
- EOD: 7笔全赢 +$1,423 (撑到收盘的仓位都赚)

**根因**: 不是方向决策错(SHORT 70%WR), 而是BUY方向入场质量差 + SL太紧砍仓 + 盈亏比不足

### 成熟项目的5个可借鉴模式

#### 模式1: ATR-based动态止损 (freqtrade/nautilus_trader)

**freqtrade做法**: `stoploss = -atr * multiplier` (通常1.5-2.0×ATR)
**FTSeek当前**: 固定2.5%(BUY)/2.0%(SHORT), 不考虑个股波动率

**改进**: 高波动股(TSLA/RIVN)ATR大→SL应该宽; 低波动股(AAPL/MSFT)ATR小→SL应该紧
**实现**: `stoploss = entry_price × max(2.5%, atr × 1.5)` — 取固定值和ATR的较大者

#### 模式2: 分批止盈 (backtrader/Jesse)

**Jesse做法**: 多层TP, 每层平不同比例
**FTSeek当前**: scale_out已禁用(4笔触发, 形同虚设), TP=SL(rr=1.0)

**改进**: 2层止盈 — 盈利1.5%平50%(保本), 盈利3%平剩余50%(锁利)
**数据支持**: EOD avg 1.8%(BUY) → 第一层在盘中触发; SHORT EOD avg 6% → 第二层触发

#### 模式3: Kelly公式仓位优化 (nautilus_trader)

**Kelly公式**: `f* = (p×b - q) / b` (p=胜率, b=盈亏比, q=1-p)
**FTSeek当前**: 固定1% risk, 不考虑胜率和盈亏比

**FTSeek的Kelly计算**:
- 全局: p=0.556, b=0.74, q=0.444 → f* = (0.556×0.74 - 0.444)/0.74 = **-0.043** (负Kelly! 不应该交易)
- SHORT: p=0.70, b=1.86, q=0.30 → f* = (0.70×1.86 - 0.30)/1.86 = **0.538** (应该用54%资金!)
- BUY: p=0.50, b=0.53, q=0.50 → f* = (0.50×0.53 - 0.50)/0.53 = **-0.443** (负Kelly! BUY不应该做)

**结论**: Kelly公式说当前BUY方向是负期望, 应该停止BUY只做SHORT。但10笔SHORT样本太少。

#### 模式4: Walk-forward回测验证 (zipline-reloaded/freqtrade)

**freqtrade做法**: 滚动窗口: 训练→验证→前进→重复
**FTSeek当前**: walk_forward引擎存在但未用于参数优化

**改进**: 用历史58笔数据跑walk-forward, 验证当前参数(SL 2.5%/2.0%, rr=1.0)是否在样本外也有效
**风险**: 58笔样本太少, walk-forward可能过拟合

#### 模式5: 因子衰减监控 (nautilus_trader/Alphalens)

**成熟做法**: 实时监控因子IC衰减, IC下降→自动降权或禁用
**FTSeek当前**: IC backfill刚修复, T+3/T+5积累中

**改进**: 每日盘后计算IC, 如果连续3天IC<0 → 自动降仓50%; 连续5天IC<0 → 暂停交易

## 可选方案

### 方案A: Kelly驱动的方向选择 (激进)
- 计算BUY和SHORT的Kelly值
- BUY Kelly<0 → 只做SHORT, 停止BUY
- SHORT Kelly>0 → 按Kelly比例调仓
- 风险: 10笔SHORT样本, 可能为噪声

### 方案B: ATR动态SL + 分批止盈 (稳健)
- SL: `max(fixed_pct, atr × 1.5)` — 高波动股自动放宽
- TP: 2层止盈(1.5%平50%, 3%平50%) — 替代rr=1.0的单层TP
- 不改方向决策, 只改退出策略

### 方案C: 先跑walk-forward验证当前参数 (保守)
- 用58笔历史数据跑walk-forward
- 如果样本外PF>1.0 → 参数OK, 继续运行
- 如果样本外PF<1.0 → 需要改参数
- 风险: 样本太少, 结论可能不可靠

### 方案D: 不改, 继续积累样本 (最保守)
- 6/27修复刚上线, 先看1周效果
- 如果修复后PF>1.0 → 不需要改
- 如果修复后PF仍<1.0 → 再考虑A/B/C

## 讨论记录

### 第 1 轮：hermes

**立场：方案D优先, 方案B作为Plan B — 先看6/27修复效果, 如果PF仍<1.0则执行B**

**理由**:
1. 6/27修复了仓位双轨制(大仓BUY -$1,153应消除) + BUY SL 2%→2.5%(减少SL触发)
2. 修复后的PF预期: SL少触发→EOD占比上升→EOD 100%WR→PF改善
3. 但: 如果修复后PF仍<1.0, 说明根本问题是策略(方向/退出), 不是参数

**方案A(Kelly)的反对**: 10笔SHORT 70%WR可能为噪声。70%WR的95%置信区间是[35%, 93%](Wilson区间)。不能基于10笔停BUY。

**方案B(ATR动态SL)的支持**: 如果修复后PF仍<1.0, ATR动态SL是下一步。高波动股SL太紧(BUY TSLA/RIVN被秒扫)是合理的改进方向。

**方案C(walk-forward)的局限**: 58笔数据, walk-forward窗口分3段每段~20笔, 每段只有10-15个closed trade, 统计不显著。

**关键指标**: 周一收盘后计算修复后的PF。如果PF>1.0 → 继续运行; 如果PF<1.0 → 执行方案B。

### 第 1 轮：claude-code

**立场：CRITIC + 量化交易专家 — 质疑方案D的"等待一周"逻辑，主张立即行动而非被动观察**

**对Hermes的四点挑战**：

1. **"等一周"在负期望下是昂贵选择**。当前PF=0.99、expectancy=-$1.52/笔，每多等一天都在积累预期亏损。除非能清晰说明修复后PF>1.0的触发机制，否则等待不是稳健，是拖延。

2. **6/27修复的是仓位和SL%，不是BUY方向的alpha**。BUY的PF=0.53是系统性亏损。 tighter position sizing只能让BUY少亏，不能让它盈利。不解决方向质量，只调参数是治标不治本。

3. **Plan B的ATR动态SL + 分批止盈，58样本撑得起来吗？** ATR校准通常需要至少30-50个有效样本 *per direction*，而BUY只有26笔、SHORT仅10笔。分层止盈后每层样本更少，统计意义堪忧。请给出最小样本数和置信度估算，否则B只是包装得更复杂的过拟合。

4. **Hermes遗漏了一个成熟项目的关键模式： regime-based direction filter**。Kelly公式本身已指向BUY为负期望，但成熟项目不会等到"样本够多"才停用一个方向，而是用rolling窗口的PF/expectancy做在线开关。FTSeek有walk_forward引擎却不用，反而让系统持续运行负期望策略，这是流程缺陷，不是数据问题。

### 第 2 轮：hermes

**接受: "等一周"在负期望下是昂贵的** — Claude Code说得对
- expectancy=-$1.52/笔, 每天~3笔 = 预期 -$4.56/天
- 但: 6/27修复改变了仓位(SL触发从$456→~$100)和SL距离(2%→2.5%)
- 修复后的expectancy ≠ 历史expectancy — 历史数据用的是旧参数
- **修正**: 不等一周, 但给周一-周二2天数据(约6-10笔)做初步判断

**接受: BUY方向alpha缺陷无法用参数修复** — 核心洞察
- BUY PF=0.53不是SL太紧的问题, 是入场质量的问题
- VotingCoordinator给BUY的confidence≥80, 但BUY 50%WR说明LLM方向判断对BUY不准
- 根因: LLM可能对"利好消息→做多"的因果链过度自信, 忽略了时机

**接受: regime-based direction filter是遗漏的模式** — 成熟项目做法
- rolling窗口PF/expectancy做在线开关: 如果BUY rolling PF<0.8连续5笔 → 自动降BUY仓位50%
- 这是freqtrade的 `protections` 模块的核心思想
- FTSeek有walk_forward引擎但没用于在线决策

**修正: 从方案D(等待)转向方案E(立即实施rolling PF门禁)**

**方案E: Rolling PF方向门禁 (来自freqtrade protections模式)**
- 每笔交易closed后, 计算该方向最近10笔的rolling PF
- rolling PF < 0.5 → 该方向仓位×0.5 (降仓不停用)
- rolling PF < 0.3 连续3笔 → 该方向暂停1天
- rolling PF > 1.0 → 恢复全仓
- BUY当前rolling PF=0.53 → 接近降仓阈值, 周一自动降仓50%

**对方案B(ATR动态SL)的修正**: 
- Claude Code指出58样本不足以校准ATR multiplier
- 但: ATR本身是市场数据(非统计推断), 不需要大样本校准
- ATR×1.5是经验值(freqtrade/nautilus都用1.5-2.0), 不需要从FTSeek数据拟合
- **修正**: 方案B不需要大样本, ATR multiplier用行业经验值1.5即可

**最终立场**:
1. 立即实施方案E (rolling PF方向门禁) — BUY自动降仓50%
2. 周一后如果PF仍<1.0 → 实施方案B (ATR动态SL)
3. 方案A(Kelly)和C(walk-forward)暂缓 — 样本不足

### 第 2 轮：claude-code

**立场：CRITIC — 质疑Plan E的三个核心假设**

1. **10笔rolling PF不足为信**。Profit Factor的方差在10样本下极大；按log(PF)近似正态，26笔BUY的95% CI仍很宽，10笔rolling PF标准误更高。最小样本应≥30笔/方向才勉强有统计意义，否则阈值0.5/0.3只是噪声触发器。

2. **50%降仓是模糊折中**。若BUY方向 truly 负期望，应hard pause而非soft reduce——半仓仍有一半负期望敞口。freqtrade protections通常用"stop trading for N candles"，不是降仓50%。

3. **"ATR×1.5行业经验无需校准"不成立**。TSLA与MSFT的波动率 regime 差异巨大，统一multiplier会系统性过松/过紧。至少应做volatility regime scaling（如高/低波动分组校准），或证明1.5在FTSeek当前股票池上out-of-sample稳健。
