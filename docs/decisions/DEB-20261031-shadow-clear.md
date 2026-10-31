# DEB-20261031 影子验收清单清账 (14 项待裁面复核 + 出口登记)

- 日期: 2026-10-31
- 触发: cron 待裁决触发通知 (job 4378c93a89e6) 报「① 需要你决定（14 项）」, 用户令「核查给出建议意见」后裁「同意」
- 性质: **测量/登记批, 零交易行为变更** (不改任何门禁、参数、采集开关; 只跑既有 eval + 写登记 + 修报表打印层)
- 结论一句话: 14 项里**只有 1 项是首次待裁的真欠账之外的真裁决**(entry_confirm 预注册后果未执行),
  9 项属「已读过、重看线到期」被错标成待裁, 5 项属「判据从未读过」; 另有 1 项 (early_dead) 按用户裁退役。

## 一、病灶 (为什么通知会报 14 项)

`criteria_status.pending_adjudications` 的 `needs_adjudication = ready ∧ 无挂账原因 ∧ (未读过 ∨ needs_user ∨ 重看逾期)`
把**三种不同状态**压成同一布尔: 「从未读过」/「读过了处置待裁」/「读过了只是重看线到期」。
报表层再把它印成「**① 需要你决定（N 项）**」, 而同一条目的背景块印「你要决定: 暂无」
⇒ **同一份推送自相矛盾 12 次** (实测: 14 项里 12 项条内写「暂无」)。

修法 (本批已落地, 打印层):
1. ① 按 `adjudicated_at` 拆成 **① 首次待裁** / **①b 到期重看** 两段 (分界: 判据是否读过);
2. 背景块的动作行改为三态: `NEEDS_USER` → 立刻处置 / 已读 → 到期重看(二选一: 登记新 recheck 或裁退役/转正) / 未读 → 暂无;
3. 判据/门槛/采集开关**一个未动** (改判据 = 重新裁决)。

## 二、逐项实测读数 (2026-10-31 实跑, 全部可复跑)

| id | 通知声称 | 本批实测 | 判定 |
|---|---|---|---|
| futu_channel_capital_flow | 9/26: IC −0.1447 (t=−1.52), 增量 −0.1326, OOS 双负 | `futu_channel_shadow_eval.py`: n_pairs=127, IC **+0.0071** (t=0.079), 增量 **−0.0248**, OOS **异号**(−0.0708/+0.0547), verdict FAIL; OFI 基线现有 51911 行 (旧注释「基线不存在」已过期) | 结论方向不变(无预测力), 旧数值作废 |
| execution_tca_spread_shadow | 样本 37/30 达门 | `execution_tca_report.py`: 成交 37 但**仅 8 个 ET 交易日** ⇒ `gate_passed=false` (门=≥10 日 ∧ ≥30 笔); 点差 p50 13.79 / p90 29.46 bps; 报价年龄 p50 1.89s / p90 14.3s; 计划→成交 均 +111.4 / 中位 +126.8 bps (方向语义未定谳, **只作上界, 不当成本**) | **样本门未过**, 不呈裁 |
| entry_confirm_forward | 判据实测 FAIL | 产物 FAIL: n=65 全量 +6.27/笔 (t=0.84), 但 SL_INITIAL 族 n=5 −120.37/笔 ⇒ 判据① 不过。`verdict_why` 自称「轴 KILLED, enabled=false」而 `config/unified_config.yaml:1267` **仍是 true**, 10/12 起 `entry_confirm_reversed` 弃单 **78 笔 / 15 日** (10/30: 计划18 弃7) | 🔴 **P1 预注册后果未执行**, 待用户裁 |
| fee_gate_shadow | 产物无读数 | `fee_gate_shadow.py`: baseline n=207 净 **−1.21/笔**、WR 78.7%、费 9.33bps; 各候选门 **blocked 组更赚** (notional<5000: 拦掉 +7.59/笔 vs 留下 −3.12/笔) | 无可立项的门 (方向相反) |
| eth_earnings_direction_path | 「尺子坏了, 结构性不可重跑, 不得裁决」 | **该判断已过期**: RFC-109④-D 已把 eval 改为读入库产物; 今日实跑通: SHORT n=199 净 **−1.87/笔** (WR41%)、LONG n=151 净 **−0.10/笔** (WR52%) ⇒ 判据 (每方向净>0) 不过 | 可裁: 维持不转正 (勿停采前先定用途) |
| eth_premarket_exit_shadow | 9/26: tighten +0.369% ✓, hold −0.302%, hold_no_stop −0.301% | `settle_extended_hours.py` (新结算 180, 累计 30246): tighten n=125 **+0.369%**; hold n=912 **−0.696%**; hold_no_stop 行级 n=15556 **+0.272%** / **持仓级 n=65 +0.153%** | 判据「族净额>0 且 n≥30」: **持仓级下 hold_no_stop 也转正**; 行级读数被重复计权 (见三) |
| eth_afterhours_exit_shadow | 9/26: 无一族净额>0 (hold_to_next_open −6.992%) | hold_to_next_open n=112 **−6.992%** (不变); hold_no_stop 行级 n=12235 **+0.060%** / **持仓级 n=51 −0.003%**; tighten n=144 −0.060% | 判据仍不过, 但「贴线 0.003pp」= 不可作过门证据, 也不可宣称显著为负 |
| exit_param_shadow | 11 变体仅 2 PASS (均为已生产腿) | `exit_shadow_graduation.py` **逐条复现**: BE/DD 5 变体全 FAIL (最好 be0.3_dd0.3 Δ+0.153, 一致性 32.8%); early_tight 5 变体全 FAIL; 30min-abort FAIL; dead_signal_60m ×2 PASS (=生产腿) | 结论稳 (era 漂移未翻盘) |
| early_dead_signal_shadow | 反事实 −$595(30m)/−$415(15m), 维持观测 | 全样本: **−$1641(30m) / −$1498(15m)**; 亏→盈(早退砍反弹) 63%; 死单率绝对线 36.6%<55% (相对意图 3.74× 过) | 用户裁 **退役不转正** (status→KILLED, 采集保留) |
| ue_confirm_gate | 还看不出结论 | 产物复现 KEEP_SAMPLING: PRICE_IN 前瞻 **+3.99%** (判据要 ≤0)、CONFIRM **−0.01%** (要 >0) | 一致, 反向 |
| short_funnel_shadow | 49/10 达标; 61 只被掐, 主因 bridge 26 | 复现: 49 交易日 / DA 1025 只→看空 149(14.5%)→空头指令 88(59.1%)→成交 52; 原因: bridge 26 + **entry_confirm_reversed 22** (全部 10/12 门上线后) + PRICE_TOO_LOW 5 + 其他 | 新增掐口 (见四) |
| instruction_conflict_shadow | 产物无读数 | 90 事件 / **19 交易日**, 同 decision_id 持久多行 **0**, 全 DA 源 ⇒ 判据①②过; **③「checkpoint 扫描=落盘」无结算器** | 缺 eval, 不缺样本 |
| blocked_exit_opportunity | 产物无读数 | 48 episode / 49 观测: 冻结时长 median & p90 = **0s** (max 66.5s), `stop_breach` **1/48 = 2.1%** | 无告警升级门依据 |
| factor_neg_edge_shadow | 产物无读数 | 全史 701 行: BUY 被标 SKIP −8.66/笔 vs 放行 −4.15/笔 (WR 57% vs 70%) = **有判别力**; SELL_SHORT 被标 **+19.62**/笔 vs 放行 +5.17 = **反向** | 🔴 空头侧因子方向错 (改用法须另立 DEB) |

## 三、新发现 (通知没说的)

1. **entry_confirm 门: 预注册后果未执行 (P1)**。RFC-141 §5 案例3 逐字: 「判读 1 明确不过 ⇒ 轴 KILLED, enabled=false, 产物留档」。
   现役 `entry_confirm_enabled: true` 且每天在弃单 ⇒ 决策与生产不一致。**本批不动生产**, 待用户裁:
   (a) 按预注册关门 (`false` + restart); (b) 破例续跑 (须在 RFC-141 留痕说明 n=5 欠功率 + 预注册「续跑至族 n≥20 再判」)。
2. **eth 退出影子的 n 轴被重复计权 (口径缺陷)**。`position_axis_note` 自述「行级 n=逐分钟采样行 / 持仓级 n=独立持仓」, 实测膨胀 **10.7×**
   (hold_no_stop: 12235 行 vs 51 持仓)。9/26 结论引用的 `n=2312/2640` 是**行级**, 不是独立样本;
   而判据写的是「净额>0 且**样本** ≥30」⇒ 应按持仓轴读。持仓轴下: 盘前 hold_no_stop +0.153% (n=65)、盘后 −0.003% (n=51)。
3. **空头侧"负 edge 因子"方向反了** (见上表 factor_neg_edge): 被标 SKIP 的空头净 +19.62/笔 高于放行的 +5.17/笔
   ⇒ 该因子在 SHORT 侧标记的是**更好的单**, 与 short_funnel 的「空头被掐」互相印证 (同向证据, 但两者人群重叠未验, 不称独立证据链)。

## 四、short_funnel: 被掐空单的反事实 (新增 `scripts/short_blocked_cf_eval.py`)

用户裁「不立门, 先回测」。口径: SHORT @ 当日 open 锚, T+1 主窗, 费用线沿用 B14 预注册 ±0.15%, (date,symbol) 去重。
数据: DA stderr 日志重解析 (jsonl 的 `rated_bearish_syms` 只存前 10 个 ⇒ 不能用于 symbol 级回测) × `playbook_instructions` × `data/replay_bars_daily`。

| 桶 | n | T+1 均值 | 中位 | WR | p_pos | 判定 |
|---|---|---|---|---|---|---|
| bridge/通道候选未提名 | 25 | **+1.256%** | +0.710% | 64.0% | 0.115 | INSUFFICIENT_N |
| entry_confirm 2min 门弃单 | 13 | +0.621% | −0.429% | 46.2% | 0.710 | INSUFFICIENT_N |
| admission 价量门 | 7 | +0.628% | +0.581% | 57.1% | 0.500 | INSUFFICIENT_N |
| sector 集中度 | 1 | −1.166% | — | 0% | 1.000 | INSUFFICIENT_N |
| **全人群** | **47** | **+0.990%** | — | 57.4% | **0.191** | OVER_BLOCKING_SUSPECT (不显著) |

读法: 方向**偏正**(放行会赚 0.99%/笔, 超费用线 6.6 倍), 但 **p=0.19 不显著、n=47<50**, 且本口径**无 SL/TP**
(实际退出会削掉尾部) ⇒ **不构成开门依据**。下一步: 用现役退出规则 (SL/TP/时间腿) 重跑一遍 + 样本攒到每桶 ≥30。

## 五、登记与后续

- 5 项首次待裁已登记 (read_at=2026-10-31), 9 项到期重看的 recheck 前移 (见 `ADJUDICATED`)。
- early_dead 按用户裁退役: registry status → KILLED, ADJUDICATED recheck=None (不再周期推回), 采集器保留作轻量回归监测。
- 复算命令: `futu_channel_shadow_eval.py` / `execution_tca_report.py` / `settle_entry_confirm_forward.py` /
  `fee_gate_shadow.py` / `earnings_direction_path_eval.py` / `settle_extended_hours.py` / `exit_shadow_graduation.py` /
  `early_dead_signal_report.py` / `short_funnel_shadow_eval.py` / `short_blocked_cf_eval.py` /
  `blocked_exit_opportunity_shadow.py` / `factor_neg_edge_shadow.py`。
- 回滚: 本批无生产变更; 报表拆分回滚 = `git revert` 该脚本提交; 登记回滚 = 删对应 `ADJUDICATED` 条目的新日期。