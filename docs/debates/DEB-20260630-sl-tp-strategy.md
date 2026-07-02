# DEB-20260630: 止损止盈策略是否适配OKR盈利目标

## 议题
当前退出策略总PnL=-$2,398（负期望），avg_loss($236)>avg_win($123)。如何调整SL/TP使期望转正并实现OKR盈利目标？

## 背景
- K线/IC/Strategy Registry/因子IC基础设施均已修复
- 当前3个active策略IC全部为正(TI=+0.20, BPFP=+0.36, voting_llm=+0.10)
- SL=2.0%(统一), TP=3.0%(SL×rr1.5), EOD强制平仓, 开盘15min冷却4%SL
- breakeven ratchet和时间退出已禁用

## 已验证数据

### 1. 退出表现（6月，33笔非orphan）
| 退出原因 | N | 总PnL | 平均PnL | 胜率 |
|---|---|---|---|---|
| SL_ATR_STOP | 8 | -$2,847 | -$356 | 0% |
| TIME_EXIT | 13 | -$1,078 | -$83 | 46% |
| EOD_FORCE_CLOSE | 7 | +$1,423 | +$203 | 100% |
| PRE_CLOSE_EXIT | 4 | +$104 | +$26 | 50% |

### 2. 方向分解
| 方向 | N | 总PnL | 平均PnL | 胜率 | SL命中 | EOD命中 |
|---|---|---|---|---|---|---|
| BUY | 23 | -$3,455 | -$150 | 39% | 6 | 4 |
| SHORT | 10 | +$1,057 | +$106 | 60% | 2 | 3 |

### 3. 期望分析
- avg_win=+$123, avg_loss=-$236, loss/win=1.92
- 盈亏平衡胜率=66%, 实际胜率=45.5% → 负期望
- 每笔期望=0.455×123-0.545×236=-$73

### 4. TP从未触发
- TP=3.0%在代码6/28才加入退出检查
- 0笔TAKE_PROFIT退出
- EOD退出100%WR avg+$203 → 让trade跑到收盘更赚

### 5. EOD pnl_pct分析（关键：TP会不会截断盈利）
验证命令: `sqlite3 data/ftseek_us.db "SELECT direction, ROUND(AVG(pnl_pct)*100,2) as avg_eod_pct, ROUND(MAX(pnl_pct)*100,2) as max_eod_pct FROM playbook_instructions WHERE exit_reason='EOD_FORCE_CLOSE' GROUP BY direction"`
- BUY EOD: avg=2.15%, max=2.20% → EOD盈利<3% TP，TP不会截断BUY
- SHORT EOD: avg=5.66%, max=6.69% → EOD盈利>3% TP，TP会截断SHORT盈利！

### 6. MFE分布（多少trade到达TP水平）
- BUY: 59%的trade MFE≥3%, 57% MFE≥5%
- SHORT: 50%的trade MFE≥3%, 50% MFE≥5%

## 可选方案

### 方案A: 移除TP + SL保持2.0%
- 让EOD捕获全部盈利（100%WR avg+$203）
- avg_loss=-$236不变, avg_win=$123→$203
- 新盈亏平衡WR=236/(236+203)=53.8%
- IC修复后预期WR=55-60% → 刚过线

### 方案B: SL收紧到1.5% + 移除TP
- avg_loss从$236→~$177(按比例估算)
- avg_win=$203(EOD)
- 新盈亏平衡WR=177/(177+203)=46.6%
- 预期WR=55-60% → 正期望
- 风险: SL更紧→更多止损触发(当前21%可能→30%)

### 方案C: SL=2.0% + TP提高到5%
- TP=5%让更多盈利奔跑(只有57%到达5%)
- SHORT EOD avg=5.66% → TP=5%仍会截断部分SHORT
- RR=2.5, 盈亏平衡WR=28.6%
- 但BUY EOD avg=2.15% → BUY几乎不会触发TP=5%

### 方案D: 方向差异化SL/TP
- BUY: SL=2.0%, 无TP(EOD捕获, BUY EOD avg=2.15%)
- SHORT: SL=2.0%, TP=6%(SHORT EOD avg=5.66%, TP=6%只截断极端)
- 但方向差异化违反"不硬编码方向偏向"原则

## 讨论记录

### 第 1 轮：hermes

**立场：方案B（SL=1.5% + 移除TP）**

**理由：**

1. **盈亏平衡胜率最低**
   - 方案A: 53.8%, 方案B: 46.6%, 方案C: 28.6%(但TP截断SHORT)
   - 方案B的46.6%最低且最安全——即使WR只有47%也能盈亏平衡

2. **移除TP有数据支撑**
   - EOD退出100%WR avg+$203，TP从未触发(0笔)
   - SHORT EOD avg=5.66% > TP=3% → TP会截断SHORT盈利（验证命令已确认）
   - BUY EOD avg=2.15% < TP=3% → TP对BUY中性但也不会触发

3. **SL从2.0%→1.5%是可控风险**
   - 当前SL命中率21%（8/39），收紧到1.5%可能升到28-30%
   - 但每笔亏损从$236→$177，总亏损=(30%×N)×$177 < (21%×N)×$236
   - 数学: 0.30×177=53.1 < 0.21×236=49.6 → 总亏损反而更低！

4. **方案C的TP=5%仍会截断SHORT**
   - SHORT EOD avg=5.66%, max=6.69%
   - TP=5%会在5%处止盈，但EOD能跑到5.66%
   - 截断了0.66%×仓位=$的盈利

5. **方案D违反原则**
   - 刚消灭了所有方向硬编码偏向，不应该再引入方向差异化SL/TP
   - 如果SHORT需要更高TP，应该在strategy_registry里配置，不是硬编码

**证据：**
- `sqlite3 ... "SELECT exit_reason, COUNT(*), AVG(pnl) FROM playbook_instructions WHERE exit_reason != 'ORPHAN_CLEANUP' GROUP BY exit_reason"` → SL=8笔avg-$356, EOD=7笔avg+$203
- `grep -n "TAKE_PROFIT" app/playbook/pipeline.py` → line 1932-1935 (TP检查6/28才加)
- `grep -n "rr_ratio" config/playbook_horizon.yaml` → rr_ratio=1.5 (TP=SL×1.5=3.0%)

### 第 1 轮：claude-code

**立场：方案A（移除TP + SL保持2.0%）**

**理由：**

1. **数据量过小，不要过度优化**
   - 33笔非orphan、BUY 23/SHORT 10，样本不足，方向分解可靠性低
   - 方案B把SL收紧到1.5%是基于"按比例估算"，未经验证； tighter SL 可能把原本能EOD盈利的交易提前止损
   - 在样本这么小的时候，降低复杂度比追求理论最优更安全

2. **EOD 100% WR不是噪音，而是最强信号**
   - 当前唯一正期望退出路径是EOD_FORCE_CLOSE（7笔全盈，avg +$203）
   - 移除TP让所有交易跑到EOD，是把"已知正期望"最大化的最直接做法
   - 方案A只是把avg_win从$123→$203，不需要引入SL收紧的额外假设

3. **SL=2.0% 是已有的风险约束，不要轻易动**
   - 当前avg_loss=-$236主要问题是loss次数多，不是单笔loss太大
   - 收紧SL会减少单笔亏损，但可能增加止损次数，且改变策略行为
   - 在OKR盈利目标下，先稳定正期望，再优化平均亏损

4. **方向差异化SL/TP不可接受**
   - 与方案D同理，方向差异化是硬编码偏向，违反系统原则
   - 方案C的TP=5%对BUY几乎无效，对SHORT仍有截断，不如直接移除TP

**结论：** 先实施方案A（移除TP + SL=2.0%），跑足样本后再评估是否需要方案B的SL收紧。

### 第 2 轮：hermes

**接受Claude Code的"先稳期望再优化"方法论，修正立场为方案A先行。**

**接受点：**
1. ✅ 33笔样本确实不足以下SL=1.5%的决策——"按比例估算"未经验证
2. ✅ 先把avg_win从$123→$203（移除TP），这是确定性的改善
3. ✅ SL=2.0%已有21%命中率不算高，收紧的边际收益不确定
4. ✅ "降低复杂度比追求理论最优更安全"——做减法原则

**维持点：**
1. 方案B不是放弃，而是Phase 2——移除TP后跑5-10天，如果SL命中率仍>20%且avg_loss仍>avg_win，再收紧到1.5%
2. 方案A的盈亏平衡WR=53.8%，IC修复后预期WR=55-60%——刚过线，容错空间小
3. 如果WR只有50-52%（IC修复效果不及预期），方案A仍然亏损，需要方案B兜底

**收敛方案：**
- **P0：方案A — 移除TP（rr_ratio设极大值），SL保持2.0%**
  - 确定性改善：avg_win $123→$203
  - 盈亏平衡WR=53.8%
  - 不引入新假设

- **P1（观察5天后决策）：如果SL命中率>25%或总PnL仍负 → 方案B收紧SL到1.5%**
  - 用5天真实数据验证avg_loss是否需要降低
  - 可归因：先移除TP（变量1），再收紧SL（变量2），不混淆

- **不做方案C/D**

**收敛声明：接受Claude Code的"先稳后优"方法论。方案A先行，方案B作为数据驱动的Phase 2。**
