# ADR-023: 耐心出场 (Patient Exit) — 死信号浅水区等回本退出 (8/13 用户裁决直接 sim)

- 状态: Accepted (sim 观察期)
- 日期: 2026-08-13
- 关联: DEB-20260813-dead-order-early-exit §十四~§二十, ADR-022

## 背景

DEB-20260813 战略收敛 (§十一): 活单 n=66 净 +$9.13/笔 (胜率 54.5%) 已达标,
亏损全部来自死单 n=28 (-$1,083)。DEAD_SIGNAL_EXIT (60min MFE<0.2%) 触发时
浅水下死单常被立即市价卖在低点, 随后价格站回 — 51 笔轮询级回放 (replay_bars
1min bar, §14) 实证"等价格站回入场价再市价卖"相对立即平 **+$107~112 ≈ +$6/日**,
δ 扫描证明严格保本 (δ=0) 最优 (δ=0.2% → -$13, δ=0.5% → -$124, 让价单调劣化),
止损 1.0/1.5/2.0 三档稳健 (§19 仲裁后口径: 1.5% 档 +$107, 16 fill/5 stop/18 eod/7 wait)。

用户裁决 (§十六): "新的不用影子验证, 直接在 sim 观察"。

## 决策

**规则引擎等待变体** (不挂限价单 — Futu paper 无排队建模会复现回测乐观偏差,
§17 验收清单实锤 close_position 恒 MARKET):

1. **arm**: DEAD_SIGNAL_EXIT 触发且信号时刻水下 ∈ [0, patient_exit_max_underwater_pct]
   (默认 1.0%) → 不立即平仓, 转等待态。深水下 (>1%) 与微盈 (>0) 不在作用域,
   行为与改动前完全一致 (fail-safe)。
2. **等待腿** (20s 轮询, 每轮评估, 跳过正常规则链防 TIME_EXIT/MFE_STALL/PRE_CLOSE
   市价抢卖; SL 灾难硬兜底优先级不变):
   - `fill`: profit_pct ≥ 0 (站回入场价) → 市价卖, exit_reason=PATIENT_EXIT_FILL
   - `stop`: profit_pct ≤ -patient_exit_stop_pct (默认 1.5%) → 立即卖, PATIENT_EXIT_STOP
   - `eod`: 到达 patient_exit_deadline_hhmm (默认 1550, 先于 PRE_CLOSE 15:52)
     → 强制卖, PATIENT_EXIT_EOD
   - `expire`: armed 状态跨交易日 → 作废清状态 + 墓碑 `_patient_exit_expired`
     防重新 arm (隔夜仓次日水下是另一回事, 回正常规则链)
3. **状态持久化**: `trade_records.metadata_json.patient_exit` (armed_at/armed_day/
   stop_pct/deadline_hhmm/immediate_exit_price/exit_mode/waited_minutes) —
   重启安全; expire 墓碑同步搬运 (开环旧行复活防护, 同 stall_exit 模式)。
4. **双口径记账** (§18.3-5): arm 时快照 `immediate_exit_price` (立即平反事实参考价),
   平仓时算 `patient_vs_immediate_delta` = 实际退出 vs 立即平的净改善 ($, 方向感知)。
5. **单开关门控**: `patient_exit_enabled` (默认 true), 一键回滚。

纯函数 (可单测零 IO): `patient_exit_arm_eligible` / `patient_exit_eval_wait`。

## 下游安全验证 (改动前 grep + 逐行读码)

- `intraday_reentry._HARD_STOP_REASONS`: 三腿加入禁入集 (DEAD_SIGNAL_EXIT 同族,
  fill 保本退出若漏归类会回退 pnl 代理误放行再入场)
- `chain.py` 退出归类: 三腿归 "stop" 类 (dashboard 展示)
- `early_dead_signal_report.py`: 无需改 (arm 时规则函数已写 meta["dead_signal_exit"],
  `dead_signal_60m_fired` 仍正确识别)
- `feishu.py` 推送: else fallback 覆盖未知退出码, 无需改
- `_close_trade_record`/open_positions 物理删除: 不受影响 (走主退出路径)

## 已知限制 (诚实披露)

- sim 观察期无影子对照 — 用户明确裁决跳过; 判据: 成交率 ≥40% + 净改善 >$0
  + 止损腿偏差 ≤2x + ≥10 个浅水区样本首评
- deadline 1550 是固定 hhmm (半日市 13:00 收盘时 eod 腿会在 15:50 前由 PRE_CLOSE
  接管 — armed 仓跳过规则链, 但半日市 13:00 收盘时 minutes_to_close≤8 的仓在
  PRE_CLOSE 前不会到 deadline; 半日市罕见, 首评时观察)
- 等待期跳过正常规则链 = TIME_EXIT/MFE_STALL 对 armed 仓延后生效 (设计意图,
  stop/eod 腿兜底)

## 回滚

`patient_exit_enabled: false` + 重启 (已 armed 仓: 状态保留但不再评估腿 →
expire 墓碑次日清状态回正常链), 或 git revert。

## 验证

tests/test_patient_exit.py 27 passed (纯函数 18 + 循环级集成 9: arm 持有/fill+delta/
stop/eod-deadline/expire+墓碑/开关关闭/深水下不 arm/SHORT 对称/等待中无动作,
全 mock 隔离零生产副作用) + 全量回归 (结果见提交信息)。
