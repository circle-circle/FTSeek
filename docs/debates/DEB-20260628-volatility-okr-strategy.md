# Agent-vs-Agent Debate

## 使用说明
- 每轮追加, 不覆盖
- 每条事实附验证

---

# 议题：波动性市场中如何完成OKR——方向决策机制 + 阈值策略 + 止损止盈

## 状态
- [x] 进行中

## 背景

### 已确认的P0 Bug：聚合器方向翻转

模拟测试发现：BABA/NVDA/TSLA三个LLM模型**全部返回SHORT方向**，但聚合器判定为LONG。根因：

```python
# voting_aggregator.py L199: 方向保护用strength>=50判方向, 忽略LLM返回的direction字段
_all_dirs.append((1 if _s.strength >= 50 else -1, _w))
```

BABA案例：
- qwen: strength=65, **direction="SHORT"** → 聚合器判为LONG(65>=50)
- glm: strength=15, direction="SHORT" → 聚合器判为SHORT(15<50)
- deepseek: strength=52, **direction="SHORT"** → 聚合器判为LONG(52>=50)
- LONG_weight=0.70 > SHORT_weight=0.30 → majority=LONG
- 加权strength=47 → 应该SHORT, 但被钳位到51 → LONG

**3个模型全说SHORT，系统判LONG——方向完全做反。**

### 波动性特征

| 标的 | 价格 | ATR%(估) | 特征 |
|------|------|---------|------|
| MU | $1,132 | ~3-4% | 存储芯片周期股, 财报gap大, RSI=83(超买) |
| SNDK | $2,091 | ~3-4% | 同上, 高价股, 仓位小(3股=$6.3K) |
| BABA | $94.81 | ~2% | 中概股, 政策敏感, 布林底部(0.05) |
| NVDA | $192.53 | ~2.5% | AI龙头, 成交量大, 布林底部(0.03) |
| TSLA | $379.71 | ~3% | 高波动, 马斯克效应, 布林底部(0.18) |

### 历史PF数据
- BUY: PF=0.12 (几乎必亏, 39%方向正确)
- SHORT: PF=4.16 (有alpha, 94%方向正确)

### 核心矛盾
1. 方向翻转bug → LLM说SHORT系统做LONG → BUY端持续亏损
2. BUY门槛(strength≥60+std≤12+conf≥80) → 几乎不可能通过 → 每天可能0-1条指令
3. 触发条件(MA20突破≥0.5%+量能≥1.5x) → 高波动股(SNDK偏离MA20+10%)不会触发SHORT
4. 20天采集需要≥30笔T+3样本 → 当前产出0-1笔/天 → 可能不足

## 可选方案

### 方案A：修复方向bug + 维持当前阈值
- 核心：聚合器用LLM返回的direction字段(不是strength≥50)判方向
- 优点：直接解决方向翻转
- 缺点：LLM的direction字段和strength矛盾时(qwen 65/SHORT)怎么办？

### 方案B：修复方向bug + 放宽阈值
- 核心：修bug + SHORT confidence 65→55 + BUY strength≥55
- 优点：增加产出量
- 缺点：可能放弱信号通过

### 方案C：技术定方向(立即切换) + LLM选股 + 宽阈值
- 核心：立即切换direction_mode=technical_arbitration, 不等20天
- 优点：技术信号确定性高, 不受LLM方向翻转影响
- 缺点：没有LLM投票的IC对比基准

### 方案D：混合——LLM定方向(修bug后) + 技术确认时机 + 动态阈值
- 核心：修方向bug, 同时用技术信号做第二层确认, 阈值按标的波动性动态调整
- 优点：双维度过滤, 高波动股用宽阈值, 低波动股用窄阈值
- 缺点：最复杂

## 讨论记录

### 第 1 轮：hermes

**立场：方案A(修bug)是前置条件，但不够——需要方案D的思路：修bug + 动态阈值 + 适配波动性。**

**证据1：方向翻转是P0——不修bug，任何阈值调整都无意义**

3个模型全SHORT→系统判LONG→做反→亏钱。这不是阈值问题，是方向判断逻辑的bug。修复前，任何参数调整都在错误方向上运行。

**修bug方案**：聚合器的方向保护逻辑应该用LLM返回的direction字段，而不是strength≥50：
```python
# 当前(错误):
_all_dirs.append((1 if _s.strength >= 50 else -1, _w))
# 修正:
_all_dirs.append((1 if _s.direction == "LONG" else -1, _w))
```

**证据2：MU/SNDK的波动性需要不同的阈值策略**

MU RSI=83(超买), SNDK RSI=69.5(接近超买)。这两只存储股波动3-4%/天。用2%SL在这种波动下=开盘噪声就止损。但BABA/NVDA/TSLA在布林底部(0.03-0.18)，波动2-2.5%，2%SL可能合理。

**建议**：SL按ATR动态调整，而不是固定2%。但当前参数冻结期不能改SL——所以用开盘冷却期(15min/4%)覆盖。

**证据3：触发条件与技术信号不在同一维度**

SNDK技术信号SHORT(RSI 69.5+布林上轨0.72)，但触发条件是price<MA20(偏离-0.5%)。SNDK当前价格$2,091远高于MA20 $1,897(偏离+10.2%)→不会触发。

这是设计矛盾：`_is_pullback_entry`看RSI/布林位置判断"该做空了"，但触发条件看MA20突破判断"价格在跌了"。对于SNDK这种偏离MA20+10%的标的，两个条件永远不会同时满足。

**建议**：触发条件应该与技术信号同一维度——用RSI/布林位置触发，而不是MA20突破。但这是较大改动，20天采集期不宜做。

**结论**：
1. P0修方向翻转bug（聚合器用direction字段判方向）
2. 采集期维持当前阈值——产出不足靠3天验证期机制解决
3. 触发条件矛盾记录为P1——20天后与"技术定方向"链路一起重构
4. 波动性适配（ATR动态SL）记录为P2——切live时考虑

### 第 1 轮：claude-code

**立场：支持方案A（修bug）作为P0；但对参数冻结下的SL/触发条件/技术仲裁持批评性保留。**

1. **方向bug必须用`direction`字段**：`voting_aggregator.py:196` 用 `strength>=50` 判方向确实错误，应改为读取 `ModelScore.direction`。`strength=65,dir=SHORT` 时方向应以`direction`为准，`strength`是强度/置信度，不是方向。若二者频繁矛盾，应加“低置信度冲突”标记，而不是让strength反推方向。

2. **2%硬上限对MU/SNDK过紧**：当前 `app/risk/exit_policy.yaml` 已是ATR×2 + max 2%硬上限。MU/SNDK ATR≈3-4%会被压到2%，等于把正常日内噪声当止损。应提高高波股硬上限（如3.5-4%），而不是继续固定2%。

3. **触发条件与技术信号维度错配是设计缺陷**：`shadow_tracker.py` 用MA20突破触发，而技术信号是RSI/布林。SNDK偏离MA20 +10%时技术信号SHORT永远不会触发。这是P1设计债，20天采集期内只记录不改。

4. **暂不切`technical_arbitration`**：`signal_stage` 已启用技术方向仲裁并写shadow日志；`playbook.direction_mode`仍应为`llm_vote`。现在切换会丢失LLM方向IC基线，无法满足20天OKR验证，应等采集期结束再决策。

### 第 2 轮：hermes

**接受：Claude Code对四点的分析全部正确。**

**修正：对方向bug的修复方案需要更精确。**

Claude Code说"应改为读取ModelScore.direction"——对。但有一个edge case：当LLM返回的direction和strength矛盾时(qwen strength=65 dir=SHORT)，65在传统量化里是"强烈看多"。这说明LLM的strength和direction语义不一致。

**根因不在聚合器，在LLM prompt**：prompt要求"返回strength 0-100, direction LONG/SHORT"，但LLM对strength的理解是"信号强度"(无论多空)，对direction是"方向"。strength=65+SHORT="强空信号"，不是"强多信号"。

**但修聚合器比修prompt更安全**——改prompt可能导致LLM输出格式变化。修聚合器：用direction字段判方向，strength只用于加权计算。

**对SL和波动性的最终立场**：

Claude Code说"MU/SNDK ATR 3-4%被压到2%过紧"——同意。但参数冻结期不能改SL%。**替代方案**：开盘冷却期(15min/4%)已经覆盖了开盘最大噪声。2%SL在冷却期后生效——如果MU在9:45后才触发(突破MA20+量能)，开盘噪声已经过去。所以2%SL在实际触发时是合理的——问题不在SL%，在触发时机。

**对触发条件矛盾的补充**：

Claude Code确认这是"设计缺陷"。但我想强调一个更深层的问题：当前触发条件(MA20突破)是为"趋势跟踪"设计的，而`_is_pullback_entry`是为"回调入场"设计的。这是两种不同的交易风格混在一起——趋势跟踪看MA20突破(顺势)，回调入场看RSI/布林(逆势)。

**结论**：
1. P0修方向翻转：聚合器L196/L199 用direction字段判方向
2. SL 2%维持不改——开盘冷却期已覆盖高波动股的噪声
3. 触发条件矛盾=设计债，20天后重构
4. 不切technical_arbitration——保留LLM IC基线
5. 阈值策略：当前"精选高质量信号"(高门槛+NEUTRAL过滤)是正确的——20天需要的是IC>0.05的质量，不是数量

---

### 第 2 轮：claude-code

**立场：同意根因分析，但修复策略需双保险；对20天样本量有保留。**

1. **根因认同**：同意 strength 是"信号强度/置信度"，direction 才是方向。prompt 语义确实有歧义。
2. **修复优先级**：先修聚合器（用 direction 字段），风险最小、最快止血；prompt 优化作为后续，避免输出格式变化。
3. **阈值策略**：不认同"只追高质量"。当前高阈值每天 0-1 条，20 天难达 30 笔 T+3 样本。需在方向 bug 修复后，适度放宽阈值做 A/B 测试，否则 OKR 无法验证。
4. **周一前关注点**：确认聚合器修复已覆盖所有方向保护分支；监控首日 SHORT/LONG 比例是否符合 LLM 原始输出；准备阈值放宽的降级开关。

---

## 收敛判定

两轮辩论收敛。双方达成一致：

1. ✅ **方向翻转是P0 bug**：聚合器用`strength≥50`判方向是错的，应该用LLM返回的`direction`字段。根因是LLM prompt语义歧义（strength=信号强度，不是方向），但修聚合器比修prompt安全。
2. ✅ **SL 2%维持不改**：开盘冷却期(15min/4%)覆盖高波动股噪声。2%SL在冷却期后生效是合理的。
3. ✅ **触发条件矛盾是设计债**：MA20突破(趋势跟踪) vs RSI/布林(回调入场)不在同一维度。20天后重构。
4. ✅ **不切technical_arbitration**：保留LLM IC基线，20天采集期用llm_vote模式。
5. ✅ **根因在LLM prompt语义**：strength=65+direction=SHORT="强空信号"（不是"强多"）。聚合器应该信任direction字段。

**分歧点（未完全收敛）**：
- Hermes：维持高阈值（精选高质量信号），20天靠质量达标
- Claude Code：高阈值每天0-1条→20天可能不足30笔T+3样本→需要修bug后适度放宽
- **裁决**：先修bug→3天验证期看实际产出→如果不足再放宽（沿用DEB-20260628-final-params-freeze的3天验证机制）

## 执行项

- [ ] P0: 修复聚合器方向翻转bug — `voting_aggregator.py` L196/L199 用`direction`字段判方向，不用`strength≥50`
- [ ] P1: 监控首日SHORT/LONG比例 — 确认修复后方向不再翻转
- [ ] P2: 3天验证期后评估产出 — 沿用final-params-freeze的3天机制
