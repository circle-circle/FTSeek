# RFC-186b 费用可行下限 sizing：让指令在费用线内可成交

- 日期：2026-11-06
- 触发：用户质问「今日推送只有 2 条指令，且都是 0股 / 风险 $0，是否正常？为什么？今晚修复，不要再出现」
- 类型：**行为变更**（单笔名义额与风险额上升），已预注册停止条件，见 §6
- 关联：RFC-186（板块快照缺失不伪装零值）、DEB-20261031-budget-design（min_notional 3000→5500）、
  2026-11-05 用户裁「恢复供给启用」（min_notional 5500→2500 + 真实费用率门 max_fee_bps=11.54）

## 1. 现象（不是"只有 2 条"，是 20 条里 18 条被同一道门同时砍掉）

`playbook_instructions` 2026-11-06 有 **20 条真实指令**（`row_kind='instruction'`，14 BUY / 6 SHORT），
推送层只显示 2 条，且两条都渲染成 `0股 | 风险 $0`。跳过 80 = 非通道候选 62 + `FEE_BPS_TOO_HIGH` 15 +
`NOTIONAL_TOO_SMALL` 3。

18 条被拒的落盘时间集中在 **09:08 ET**，拒因与股数成对出现（例：`US.CRM` 6股/31.2bps、`US.CAT` 4股/17.9bps），
说明是"先算尺寸、再被费用门否掉"，不是缺数据、也不是行情不同步。

## 2. 根因：两条闸互锁成"零成交"

费用模型（`app/execution/fee_model.py`，`max(min(0.01×股数, 0.3%×额), $1.99)/腿`）里 **$1.99/腿是固定成本**：

| 单笔名义额 | 往返费用 | 往返费率 |
|---|---|---|
| $2,500 | $3.98 | **15.9 bps** |
| $2,806（今日 AMZN 11股） | $3.98 | **14.2 bps** |
| $3,449 | $3.98 | **11.54 bps** ← 费用线临界 |
| $5,500 | $3.98 | 7.2 bps |

- 11-05 的 `min_notional_usd` 从 5500 降到 **2500**（"恢复供给启用"，数据量优先），同日加了
  `max_fee_bps = 11.54`（费用优先）。**两者在费用模型下互斥**：2500 对应的真实费率是 15.9 bps，
  任何 2500–3449 的单必然被 11.54 拒。配置文件里 10-31 的旧注释其实已写明这一点（"$2,500 时 15.9bps
  … 低于 5,500 的单做一笔亏一笔"），11-05 改的是 `min_notional`，费用门却没跟着对齐。
- 触发条件是 `RISK_OFF`：`risk 预算 = 99,128 × 0.5% × 0.3(regime) = $148.69`，4–6% 止损距离
  ⇒ 名义额只有 **$2.6–3.3k**，全部落在 12.8–31.2 bps 区间。
- 于是：**风险预算越大越安全，费用地板越小额越贵**——两条约束都没有错，但没有任何一方负责"把名义额
  抬到费用可行下限"。`compute_position_size` 只按风险算股数，`admission` 只判不修，结果就是结构性的零成交。
- 低价股还有第二层物理边界：每股佣金 $0.01 与股数成正比 ⇒ 费率恒为 `200/price` bps，与规模无关。
  `price < 200/11.54 = $17.33` 的标的**在任何规模下**都过不了费用线（今日 INTT $12.30、RIVN $13.47、
  LYFT $16.45），这类必须与"这单太小"分开表达，否则永远读不出"这只票在现费制下不能碰"。

## 3. 修复（三处，含一处纯显示）

1. **`app/playbook/admission.py`**
   - `fee_bps_at(qty, price, direction, date)`：单一费用率口径。
   - `fee_viable_qty(price, direction, max_notional, max_fee_bps, min_notional, date)`：
     在 `max_notional`（= 6% 仓位帽）内求"满足费用线的最小整股数"；**帽内无解返回 None**，
     绝不主张越帽放大。
   - `price_floor_usd(max_fee_bps)` = `200/max_fee_bps`；`evaluate()` 新增独立拒因
     `PRICE_BELOW_FEE_FLOOR(price<floor)`。
2. **`app/playbook/instruction_generator.py::compute_position_size`**：新增 `fee_floor / max_fee_bps /
   min_notional_usd / direction / trade_date`；计算完风险股数后，若费率越线则抬到
   `max(风险股数, 费用可行最小股数)`，**上界仍由 `max_position_pct` 与 `risk_cap_usd` 钳制**；
   抬不上去就保持原股数，交给 admission 明确拒（不静默放行）。求解异常只告警不阻断。
   兜底路径 `pipeline_parts/auto_execute_flow.py` 用同一开关、同一阈值（避免两路双标准）。
3. **`app/playbook/feishu_push.py`**：`position_size/risk_amount` 为 None 时显示
   `股数待算 / 风险待算`，不再渲染成 `0股 | 风险 $0`（今日两条存活指令因此被读成"不做"）。

开关：`playbook.admission.fee_floor_sizing: true`（代码默认 `False`＝旧行为，配置开）。
回滚：`fee_floor_sizing: false` + restart，立即回到"只按风险算、越线由费用门拒"。

## 4. 回放证据（今日 20 条真实参数，非合成）

用今日落库的 20 条（原始方向取自 `raw_instruction_json.admission.original_direction`，**不能用
`direction` 列**——18 条已被翻成 SKIP，拿 SKIP 跑 `evaluate` 会得到无意义的 vacuous PASS）：

| 指标 | 旧 sizing | 新 sizing |
|---|---|---|
| 通过 admission | **0 / 20**（18 REJECT + 2 未算尺寸） | **17 / 20** |
| 单笔名义额 | $2,805–5,355 | $3,655–4,351 |
| 单笔风险 | $148.7 | $165–219（+11%~+47%） |
| 费率 | 12.8–31.2 bps | 9.4–11.5 bps |

未通过的 3 条全部是费用物理不可行，不是缺尺寸：`INTT $12.30`、`LYFT $16.45`、`RIVN $13.47`（< $17.33）。
`META` 在旧口径下根本没算尺寸（0股）⇒ 新口径 6 股 / $3,693 / 11.1 bps 可成交。

## 5. 没修的部分（诚实边界）

- **费用线本身没有放宽**（`max_fee_bps` 仍是 11.54），`min_notional` 仍是 2500。
  所以"17 条可成交"≠"17 条有正期望"：10-31 记录的历史毛利 **7.83 bps < 11.54 bps**，
  按这条线成交仍是小幅负期望。本修复解决的是**指令可执行性与口径一致性**，
  不主张盈利；真正的盈利线需要名义额 ≥ $5,082（7.83 bps 盈亏平衡）或提高单笔 edge。
  这一层需要单独裁（见 §6 S2），不在本次偷偷改。
- 未改 `max_position_pct`(6%)、`risk_cap_usd_per_trade`、止损/止盈几何、regime 方向语义。
- `planned_executability.py` 的预览路径未接新参数（预览可能与生产股数不一致）——登记为后续。

## 6. 预注册停止条件（防"修好推送、修坏账本"）

- **S1（正确性，强制）**：连续 5 个交易日，逐条核对"新 sizing 股数 × 费用率 ≤ 11.54 bps"，
  出现任何一条越线 ⇒ 立即 `fee_floor_sizing: false` 回滚并查求解器。
- **S2（经济性，2 周）**：统计新口径成交的**净额（扣费后）**与旧口径（5500 时代）对比。
  若 2 周内净额中位数 ≤ −10 bps/笔，或累计净额低于"不交易"基线 ⇒ 回滚，并把
  `max_fee_bps` 与 `min_notional` 正式二选一（不再并存）。
- **S3（风险，强制）**：单笔实际风险 > `risk_cap_usd_per_trade`（若启用）或 > 6% 名义帽 ⇒
  回滚；单日总风险敞口 > 2% 权益 ⇒ 回滚并复核并发数。

## 7. 留痕

- 测试：`tests/test_fee_floor_sizing.py`（5 条，含"关闭即复现旧故障"的反向断言与
  `fee_viable_qty` 变异验证：求解器失效后股数停在 11 股/14.7bps ⇒ 断言必红）。
- 相关既有测试：`test_admission_gate / test_admission_replay / test_execution_admission /
  test_ext_admission_attribution` 共 44 条通过。
- 本次提交同时把 **11-05 已生效但未入库**的费用门实现（`max_fee_bps` + `estimate_round_trip_fee`）、
  LIVE 探针经济性豁免、以及 11-05 配置（2500 / 11.54）一并入库——它们是本修复的前置依赖。