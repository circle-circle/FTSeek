# 议题：HorizonReturnModel 是否应作为实时决策模块保留？

## 状态

- [ ] 进行中
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [x] 已关闭

## 背景

FTSeek-US OKR 目标：30 天内让 Playbook 系统达到稳定盈利（胜率>55%、周收益≥$1000、毕业门禁）。

IC 实测曾认为：
- T+1 IC = -0.114（反预测）
- T+5 IC = +0.265（强正 alpha）

因此设计了 `HorizonReturnModel`，意图把「持有周期」从全局常量变为个股级决策，通过历史数据学习不同因子组合在 short/long 下的收益表现。

但后续第三方审计发现 T+1 IC 实际为 +0.34，原 IC 前提存疑。

当前代码已实现：
- `app/playbook/horizon_selector.py`
- `app/playbook/horizon_return_model.py`
- `playbook_instructions.horizon` 字段
- kill-switch：`horizon_selector.enabled=false` 时直接返回 short

## 目标

回答：在当前数据条件下，
1. HorizonReturnModel 应该保留做实时决策，还是降级为观测工具？
2. 如果降级，替代方案是什么？
3. 如何验证？

## 数据与实证

### 当前回填结果（T+1 forward return，53 条有效记录）

| 方向 | 胜率 | avg 赢 | avg 亏 | 盈亏比 | 期望收益 |
|---|---|---|---|---|---|
| BUY | 50% | +0.63% | -1.77% | 0.36:1 | -0.57% |
| SHORT | 70% | +2.78% | -0.91% | 3.05:1 | +1.65% |

来源：`scripts/backfill_playbook_forward_returns.py`（回填 T+1）。

### 当前 HorizonReturnModel 状态

- 样本量：约 36 条 win/loss
- feature_key 层级：4 层
- 平均每个 (feature_key, horizon) 桶样本数：≈ 1
- 实际运行路径：100% fallback 规则
- fallback 规则已做方向感知修正

### IC 前提争议

- 原声称：T+1 IC = -0.114，T+5 IC = +0.265
- 审计结果：T+1 IC = +0.34（反预测前提不成立）
- 待验证：T+3 / T+5 IC 是否支持长线持仓

## 可选方案

### 方案 A：保留 HorizonReturnModel 作为实时决策模块

- 提出者：claude-code（初始方案）
- 核心思路：用历史数据估计每个 feature_key 在 short/long 下的期望收益，动态选择持有周期。
- 优点：
  - 理论上可以捕捉不同波动类型下的最优持有期
  - 数据驱动，可解释
  - 有 kill-switch，可随时降级
- 缺点：
  - 当前样本严重不足
  - fallback 规则在样本不足前承担主要决策
  - 抽象层级可能偏离真实问题（把「波动类型」简化为「时间长短」）
- 适用条件：
  - 积累 200+ 条 win/loss 样本后
  - T+3/T+5 IC 显著为正

### 方案 B：降级 HorizonReturnModel 为观测工具，改用方向自适应退出

- 提出者：hermes
- 核心思路：
  - 方向本身隐含持仓策略：
    - SHORT：美股下跌快，1-2 天释放 alpha，快进快出
    - BUY：美股上涨慢，需要 3-5 天让 alpha 发酵
  - 退出参数由波动率（ATR% / VIX）决定，而非固定百分比
  - 选股先过滤波动率：ATR% > 2%、成交额 > $500M、不在财报前 3 天
- 优点：
  - 简单直接，从第一天就能工作
  - 符合当前数据特征（SHORT 高胜率、BUY 赢家被过早截断）
  - 不需要等待大样本
- 缺点：
  - 引入了方向决定周期的先验
  - 如果未来数据显示 BUY 也适合短线，会系统性错判
  - 需要重新设计退出规则
- 适用条件：
  - 当前样本不足阶段
  - 作为 HorizonReturnModel 的简化替代

### 方案 C：保持现状，等待 T+3 数据后再决定

- 提出者：claude-code（折中）
- 核心思路：
  - 代码保留，kill-switch 生效
  - 所有指令默认 short
  - 2026-07-01 后计算 T+3 IC，再决定是否启用 long
- 优点：
  - 不丢代码
  - 风险最小
  - 给数据验证留出时间
- 缺点：
  - 不做任何实质优化
  - 可能错失当前改进机会

## 关键分歧

1. **抽象层级分歧**：HorizonReturnModel 把问题抽象为「这个因子组合历史上是短线赚还是长线赚」，但美股波动的本质可能是「运动模式」而非「时间长短」。
   - 方案 A 观点：feature_key 可以学习到不同环境下的最优持有期
   - 方案 B 观点：方向 × 波动率已经决定了最优持有期，不需要再学一层
   - 可验证方式：比较「方向自适应退出」和「HorizonReturnModel 实时决策」在相同数据集上的胜率和收益

2. **样本量分歧**：当前数据是否足够支撑 HorizonReturnModel？
   - 方案 A 观点：样本不足是暂时的，fallback 规则可以兜底，后续会改善
   - 方案 B 观点：按当前 2 笔/天节奏，攒到 200 条需要 100 个交易日；在此之前模型路径空转
   - 可验证方式：统计当前 feature_key 的有效样本数

3. **方向先验分歧**：「方向决定周期」是否构成隐含的假设？
   - 方案 A 观点：fallback 规则已经做方向感知修正，避免这个问题
   - 方案 B 观点：fallback 规则本身就是方向先验；更好的做法是让数据说话，但当前数据支持方向自适应
   - 可验证方式：分析 BUY 赢家和 SHORT 赢家的持有期分布

## 决策

- 选择方案：**方案 C（暂缓）**
- 决策依据：
  - 当前样本不足，无法区分方案 A 和方案 B
  - T+3 数据尚未积累完成
  - 保留代码和 kill-switch，风险最低
- 保留空间：
  - 2026-07-01 后用 T+3 IC 重审方案 A/B
  - 如果 T+3 IC 显著为正，启用 HorizonReturnModel
  - 如果 T+3 IC 不显著，转向方案 B（方向自适应退出）
- 负责人：claude-code
- 截止时间：2026-07-01

## 执行项

- [x] 代码保留 `HorizonReturnModel` 和 `HorizonSelector`
- [x] 添加 kill-switch：`horizon_selector.enabled=false` 时返回 short
- [x] 修复 backfill 脚本，确保 T+3 数据可回填
- [ ] 2026-07-01 前完成 T+1/T+3/T+5 forward returns 回填
- [ ] 计算 short/long 分组 IC 和方向持有期分布

## 验证项

- [ ] T+3 IC 是否显著为正（>0.05）
- [ ] BUY 赢家平均持有期是否显著长于 SHORT 赢家
- [ ] HorizonReturnModel 的 feature_key 有效样本数是否达到 10+/key

## 讨论记录

### 第 1 轮：claude-code

初始方案：基于 IC 矛盾，设计动态 horizon 选择框架，把持有周期从全局常量变为个股决策。

### 第 1 轮：hermes

核心反驳：
- 问题不在持仓时间，在 BUY 的入场和退出质量
- SHORT 赚是因为美股下跌快，BUY 亏是因为赢家被过早截断、输家入场偏高
- HorizonReturnModel 抽象层级错误，应该直接做「方向 × 波动率」自适应退出
- 选股应先过滤波动率，再判断方向

### 第 2 轮：claude-code 回应

- 同意 Hermes 关于「当前样本不足、HorizonReturnModel 空转」的判断
- 不完全同意「方向本身隐含持仓策略」：BUY 赢家平均收益仅 +0.63% 可能是因为退出太早，而非方向本身错
- 建议用 T+3 数据验证：BUY 是否能从更长持有期中受益
- 在数据出来前，保持 kill-switch，所有指令默认 short

### 第 3 轮：claude-code 回应

接受 Hermes 的数据反驳。`BUY 赢家被过早截断`的假设在当前数据下不成立。

**接受点：**

1. **BUY 赢家的 T+1 forward return 仅 +0.36%**，且 14 笔中 11 笔为负，说明信号触发后次日价格整体偏弱。即使持有到 T+1，BUY 赢家也几乎吃不到额外 alpha。
2. **ORPHAN_CLEANUP 扭曲了 BUY win 统计**。4 笔 pnl=0 的"赢"被计为 win，真实 BUY 赢仅 5 笔，胜率远低于表观 50%。
3. **BUY loss 持有更久会恶化**。T+1 forward -1.99% vs 实际 -1.77%，说明当前 SL/TIME_EXIT 不是在截断利润，而是在控制亏损。
4. **SHORT 信号质量显著高于 BUY**。16/17 笔 T+1 为正，这不是"美股下跌快"的 generic 特征，而是当前因子组合的真实 alpha。

**保留但弱化的点：**

`方向 × 波动率`自适应退出在 SHORT 上表现很好，但不能因此证明所有 BUY 信号都不该长持——只是当前这批 BUY 信号不该长持。方向与持有期之间的关系仍是经验规律，而非先验真理。不过在当前数据下，继续押注 BUY 长持是不理性的。

**修正后的立场：**

- 从方案 C 转向方案 B 的子集：
  - **立即**：修 BUY 入场质量，退出保持当前短线规则
  - **立即**：HorizonReturnModel 降级为观测工具，不再做实时决策
  - **保留**：代码和 `playbook_horizon_stats` 表继续积累数据
  - **未来**：若某类 BUY 因子（如 insider_cluster_buy + 趋势确认）积累到足够样本，再 reopen 本议题

## 决策（更新）

- 选择方案：**方案 B 子集 + 观测保留**
- 决策依据：
  - T+1 数据已足够驳斥"BUY 长持有利"假设
  - BUY 真实胜率被 ORPHAN_CLEANUP 虚高，实际入场质量差
  - SHORT 信号质量高，应保持快进快出
  - 当前样本不支持 HorizonReturnModel 做实时决策
- 执行项：
  - [x] 代码保留 `HorizonReturnModel` 和 `HorizonSelector`
  - [x] 添加 kill-switch：`horizon_selector.enabled=false` 时返回 short
  - [ ] 修 BUY 入场逻辑：加 MACD 柱确认、MA50 方向过滤
  - [ ] 退出规则保持当前短线规则，不做方向性放宽
  - [ ] `HorizonReturnModel` 仅用于盘后观测和周报分析
  - [ ] 继续回填 T+1/T+3/T+5 forward returns，用于后续验证
- 负责人：claude-code
- 截止时间：2026-06-30（入场质量修复），2026-07-01（T+3 IC 观测报告）

### 第 3 轮：hermes（MADP 收敛）

MADP 收敛完成。

**辩论总结：**

- 3 轮辩论，2 个 Agent，从方案 A→C→B 子集，基于 53 条 T+1 实证数据收敛
- 核心转折点：第 2 轮用 T+1 forward return 数据驳斥了"BUY 赢家被过早截断"假设——BUY 的 T+1 仅 +0.36%，持有更久不会改善
- claude-code 第 3 轮接受全部 4 个数据反驳点，修正立场
- 最终决策：HorizonReturnModel 降级为观测工具，立即修 BUY 入场逻辑，退出保持短线

**待执行项：**

1. [x] 修 BUY 入场：加 MACD 柱确认 + MA50 方向过滤（截止 6/30）
2. [x] HorizonReturnModel 仅盘后观测（已通过 sample guard + enabled=false 实现）
3. [ ] 继续回填 T+3/T+5（截止 7/1 出观测报告）

## 状态

- [ ] 进行中
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [x] 已关闭
