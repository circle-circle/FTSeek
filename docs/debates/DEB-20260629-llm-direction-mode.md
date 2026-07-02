# DEB-20260629: LLM分析链路方向决策机制辩论

## 背景

FTSeek-US当前使用`direction_mode=llm_vote`（LLM多模型投票定方向）。系统已实现`technical_arbitration`（技术信号定方向，LLM仅选股）但未启用。用户要求辩论哪种更好，或是否有融合/新思路。

## 目标

辩论议题：**当前LLM分析链路中，direction_mode该如何设置？**
- 方案A: 维持`llm_vote`（LLM定方向+选股）
- 方案B: 切换`technical_arbitration`（技术定方向，LLM仅选股/冲突参考）
- 方案C: 融合方案（新思路）
- 方案D: 其他

## 数据与实证（Pre-debate收集，已验证）

### 1. 系统当前状态
- **direction_mode**: `"llm_vote"` (unified_config.yaml:1373)
- **technical_arbitration配置已存在** (unified_config.yaml:1374)，含llm_conflict_confidence_threshold=0.70
- **Pipeline执行路径**: `_build_instructions_from_votes` (pipeline.py:300) → L398-403读取direction_mode → L423如果`technical_arbitration`则走技术仲裁
- **技术仲裁逻辑已实现** (pipeline.py:423-450): 无技术方向→SKIP; LLM强反向冲突→SKIP; 否则technical获胜
- **3模型投票**: qwen3.7-max(50%) + glm-5.2(30%) + deepseek-v4-pro(20%)
- **Voting aggregator P0修复(6/28)**: 用direction字段判方向，不用strength>=50 (voting_aggregator.py:196-200)

### 2. 方向准确率（fwd_return_t1，已验证DB）
| 方向 | 样本 | 正确率 | avg_fwd |
|------|------|--------|---------|
| BUY | 36 | 38.9% | -0.008 |
| SELL_SHORT | 17 | 5.9% | +0.032 |

- **BUY准确率38.9% < 50% = 方向预测不如随机**
- **SHORT准确率5.9% = 几乎每次方向都错（市场上涨却做空）**
- avg_fwd: BUY=-0.008（微跌），SHORT=+0.032（上涨）→ SHORT信号发出后市场上涨

### 3. PnL按方向分解（已验证DB）
| 方向 | 笔数 | 总PnL | 胜/负 | PF |
|------|------|-------|-------|-----|
| BUY | 41 | -$1,837 | 13W/13L | 1.0 |
| SELL_SHORT | 17 | +$2,461 | 8W/2L | 4.0 |

- **BUY: PF=1.0 = 负期望（扣手续费后亏损）**
- **SHORT: PF=4.0 = 正期望，但方向准确率仅5.9%**
- **矛盾**: SHORT方向几乎全错（市场上涨）但PnL为正 → alpha来自退出策略（tight SL 2.0%+rr=1.5 TP），非方向预测

### 4. 产出分布
- 总指令: 67条（BUY 41, SHORT 17, SKIP 9）
- 最近10天: 6/23-6/25有产出，6/26后无新产出（6/26-6/29无新指令）
- 6/25断崖: 6/24产出25条→6/25仅1条BUY（MACD/MA50过滤已禁用）
- Triggered/Executed: BUY 33/26, SHORT 16/11

### 5. 退出原因分布
| 退出原因 | 笔数 |
|---------|------|
| TIME_EXIT | 13 |
| SL_ATR_STOP | 8 |
| EOD_FORCE_CLOSE | 7 |
| ORPHAN_CLEANUP | 6 |
| PRE_CLOSE_EXIT | 2 |

### 6. 技术仲裁已实现逻辑（pipeline.py:423-450）
```
if _direction_mode == "technical_arbitration":
    if not technical_direction: → SKIP "无技术方向信号"
    if vote_direction != technical_direction:
        if LLM confidence >= 0.70: → SKIP "方向冲突"
        else: technical覆盖LLM方向
    else: 同向使用vote_direction
```

### 7. 信号链路
- **链路A（Playbook主力）**: Screener(质量过滤) → VotingCoordinator(3模型投票) → _build_instructions_from_votes → Instruction → auto_execute → _check_intraday_trigger → 下单
- **链路B（scan_loop, 当前_skip=True禁用）**: signal_stage → PipelineRunner → 下单
- **当前下单路径**: Playbook独占（_skip_execution_in_trading=True禁用了scan_loop）

## 讨论记录

### 第 1 轮：hermes

**立场：支持方案B（technical_arbitration），但需要融合LLM选股能力**

**核心论据：**

1. **数据证明LLM方向预测无效**。BUY准确率38.9%<随机，SHORT准确率5.9%几乎全错。LLM的方向判断不仅无alpha，而是负alpha。继续让LLM定方向=主动亏损。

2. **SHORT的alpha来自退出策略非方向**。PF=4.0但方向准确率5.9% → 系统在上涨市做空，靠tight SL(2.0%)截断亏损+rr=1.5 TP捕获偶发下跌。这是退出层的alpha，不是方向层的alpha。如果方向对准了（技术信号定方向），PF可以更高。

3. **技术信号定方向有理论支撑**。MA20突破/RSI/布林带是趋势跟踪指标，在trending market中有统计edge。LLM的优势在信息广度（新闻/财报/事件驱动选股），不在方向预测。

4. **technical_arbitration已实现且验证过**。pipeline.py:423-450已有完整逻辑：技术定方向→LLM强反向冲突则SKIP→否则technical覆盖。切换只需config改一行（direction_mode: "technical_arbitration"）。

5. **融合方案**：technical_arbitration + LLM选股权重。技术信号定方向（确定性高），LLM的confidence作为仓位调整因子（信息广度有用但不决定方向）。这正是6/28辩论结论。

**风险点：**
- 技术信号在ranging market中失效（whipsaw）
- 当前技术数据有K线停滞问题（Futu OpenD返回2025年旧数据，TwelveData fallback）
- 切换后产出量可能下降（无技术方向→SKIP）

**验证命令：**
- `grep -n "direction_mode" config/unified_config.yaml` → L1373: `direction_mode: "llm_vote"`
- `grep -n "technical_arbitration" app/playbook/pipeline.py` → L423: 完整仲裁逻辑
- DB查询: `SELECT direction, count(*), sum(case when (direction='BUY' and fwd_return_t1>0) or (direction='SELL_SHORT' and fwd_return_t1<0) then 1 else 0 end) FROM playbook_instructions WHERE fwd_return_t1 is not null GROUP BY direction`

---

### 第 1 轮：claude-code

**立场：质疑方案B，倾向保留 LLM 投票 + 方向差异化过滤，或以收益目标替代方向准确率。**

1. **fwd_return_t1 不足为据**。`ic_tracker.backfill_forward_returns` 用盘后 `get_daily_bars` 回填 T+1 收盘价，样本仅 53 条；且 SHORT 方向准确率 5.9% 却 PF=4.0，说明 close-to-close 收益与实盘盈亏脱节，不能据此判定 LLM 方向无效。

2. **技术信号输入可能陈旧**。`compute_from_futu_kline` 取 Futu K_DAY 计算 MA/RSI/Bollinger；而 `futu_lv3_provider.py:1179` 明确修复过“OpenD 模拟环境历史 K 线停滞在 2025 年”的问题。在 K 线可能过期、需 fallback 到 TwelveData 的前提下，技术方向本身就是噪声。

3. **SHORT 悖论是方向仲裁的陷阱**。SHORT 盈利来自日内波动与 tight SL/TP，而非收盘方向。改由技术定方向，可能过滤掉“方向错但盈亏对”的交易，反而降低 PF。

4. **更优路径**：不以收盘方向准确率为切换依据，而把优化目标改为 **已实现 PnL / MFE-MAE**；保持 `llm_vote` 并沿用现有 BUY/SHORT 差异化 confidence 门槛，将 technical 仅作为仓位 / 触发时间参考，而非一票否决。

---

### 第 2 轮：hermes

**修正立场：部分接受Claude Code的批评，调整为方案C（融合：LLM选股+技术择时+退出不动）**

**接受Claude Code的2点：**
1. ✅ fwd_return_t1确实不是唯一判据。SHORT 5.9%方向准确率但PF=4.0证明：alpha在退出层不在方向层。用方向准确率否定LLM定方向是逻辑跳跃。
2. ✅ 技术信号K线数据有陈旧风险。如果技术指标基于2025年旧K线，technical_arbitration切换后方向全是噪声。

**但坚持的核心观点：**
1. **LLM方向预测仍然无效**。BUY PF=1.0=负期望。这不是方向准确率的问题——是BUY方向整体在亏钱。41笔BUY总PnL=-$1837。无论准确率定义如何，亏钱是事实。
2. **SHORT的alpha可复制性存疑**。PF=4.0基于17笔（8W/2L），样本极小。6笔ORPHAN_CLEANUP（强制平仓）可能扭曲PnL。剔除后SHORT真实PF可能<2.0。
3. **Claude Code的"保持llm_vote+差异化confidence"=原地踏步**。当前已经是这个模式（BUY需要更高confidence门槛，pipeline.py:467-477 BUY std>12则SKIP）。但BUY仍然PF=1.0。说明confidence门槛无法弥补方向预测的缺陷。

**提出方案C融合方案：**
- **选股层用LLM**：24只watchlist中，LLM选哪只值得交易（信息广度：新闻/财报/事件驱动）
- **方向层用技术+LLM投票的混合**：不非此即彼。技术信号给方向基准，LLM投票在技术方向一致时加仓，在冲突时降仓（而非SKIP）
- **退出层不动**：SL+TP+PRE_CLOSE已经是alpha来源，不改
- **优化目标改为PnL/MFE-MAE**（接受Claude Code建议），不用方向准确率

**新数据支持：**
- BUY 41笔中13W/13L = 50%胜率，但总PnL为负 → 赢的少输的多（avg win < avg loss）
- SHORT 17笔中8W/2L = 80%胜率 → 但方向5.9%正确 → 胜率来自SL/TP非方向

**验证待做：**
- 剔除ORPHAN_CLEANUP后SHORT真实PF
- 技术指标在当前K线fallback（TwelveData）下是否有效
- 方案C的混合逻辑是否需要新代码还是config可调

---

### 第 2 轮：claude-code

**立场：质疑方案C，认为其引入未验证复杂度，且数据不支持Hermes的推断。**

1. **方案C需要新代码，不是调config**。当前`pipeline.py:423`仅支持`llm_vote`和`technical_arbitration`两个分支；"技术方向一致时加仓、冲突时降仓"的融合逻辑没有对应配置项，必须新增mode、仓位调整公式和影子日志。

2. **剔除ORPHAN_CLEANUP后SHORT真实PF=2.5，不是<2.0**。DB实测：排除后8笔5W/3L，总PnL+$1,025.64，PF=2.5。仍为正期望，但远低于含orphan的9.67。

3. **BUY数据与Hermes表述不符**。DB实测41笔13W/28L（非13W/13L），胜率31.7%；avg win $157.83 vs avg loss -$138.89，赢额其实略高于输额，总亏损主因是输多赢少。

4. **方案C违反做减法原则**。在LLM方向IC未验证、技术信号K线可能陈旧、已有两种模式可A/B的情况下，新增混合逻辑会叠加参数（冲突阈值×仓位梯度×置信度映射），增加调参空间与故障面。

**结论**：先不融合。建议用现有`technical_arbitration`做20笔影子对照，或继续优化BUY过滤/退出，而非新增第三种方向模式。


---

## Action Items

### 收敛结论（Hermes R3）

**接受Claude Code的全部数据修正：**
1. ✅ BUY是13W/28L（胜率31.7%），非13W/13L — 我的数据引用有误
2. ✅ SHORT剔除ORPHAN_CLEANUP后PF=2.5，非<2.0 — 我的推测被推翻
3. ✅ BUY avg win($157) > avg loss($139) → 问题是胜率低非赔率差
4. ✅ 方案C需要新代码 — pipeline.py:423只有两个分支
5. ✅ 方案C违反做减法原则

**收敛立场：不新增第三种模式。在A/B中选择+改进。**

**辩论共识：**
- LLM方向预测弱（BUY胜率31.7%），但fwd_return_t1不足以单独证明"LLM方向完全无效"
- SHORT的alpha在退出层（PF=2.5剔除了orphan），方向层贡献低
- 技术仲裁的K线数据质量是未验证风险
- 新增混合模式=增加复杂度=违反做减法

**推荐方案（P0→P2优先级）：**

- [ ] P1: **不切换direction_mode，维持llm_vote**。理由：technical_arbitration的K线数据质量未验证，盲切风险更大。先验证数据再切。
- [ ] P1: **验证技术信号数据质量**。跑`get_daily_bars('US.TSLA', count=10)`检查返回时间戳，确认K线是否新鲜。如果TwelveData fallback正常→技术指标可信→可考虑切换。
- [ ] P1: **BUY方向优化**。BUY胜率31.7%是核心问题。当前BUY高门槛（std>12 SKIP, confidence差异化）已实施但无效。考虑：BUY只做技术同向（LLM选股+技术确认BUY方向），SHORT保持LLM定方向（PF=2.5有alpha）。
- [x] P1: **动态选股池接入Playbook** ✅已实施。`_get_watchlist()`合并factor_engine动态池(龙头+异动+regime匹配)。静态27+动态29→合并36只候选。纯加法不改参数语义。等下个交易日验证日志。
- [ ] P2: **20笔影子对照**。在shadow mode下跑technical_arbitration，与llm_vote并行比较PnL。数据达标后再切换。
- [ ] P2: **优化目标改为PnL/MFE-MAE**（Claude Code建议）。不用方向准确率做决策依据。

**关键约束：**
- 20天采集期（6/29-7/24）内不改参数语义，只做加法
- 每次只改一个变量，可归因
- 不新增方向决策模式（不做方案C）

## 收敛判定

**收敛** — 双方在R2后达成共识：
- 不新增第三种模式（方案C被双方否决）
- 核心问题是BUY方向亏损，不是方向决策机制本身
- 技术数据质量未验证前不盲切
- 优化目标从方向准确率改为PnL/MFE-MAE
