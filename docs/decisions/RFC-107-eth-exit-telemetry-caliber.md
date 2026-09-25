# RFC-107 — ETH 退出遥测口径修复批 (MFE/MAE 峰谷真值 + broker_remark 契约对齐) + B21 错失上行影子预注册

日期: 2026-09-25
裁决: 用户裁「按建议执行」(MU 9/25 盘前 BE 案复核回执, 三条建议 ①②③)
分类: L1 (功能修复 <50 行) + L5 (文档预注册)
交易行为变更: **零** (全部为观测/遥测口径; 唯一交易侧消费者 symbol_streak_ban 只会更准)

---

## 1. 背景 (MU 案两处 P2)

MU 9/25 04:04 ET 盘前 SL_BREAKEVEN 出场 (成交 1092.0×5, 净 +$15.67) 复核发现两处 P2 遥测口径瑕疵:

1. **推送 "MFE 0.36% capture 100%" 偏低**: `mfe_pct` 列取累积值 (bar/轮询口径, 冻结在
   `bar_covered_until`), ETH tick 不进 bar。真值: tick 峰 1092.63 (+0.4219%) / bar 峰 1092.8
   (+0.4375%) → 真 capture ≈ 83-86%, 非 100%。`time_to_mfe=9.7min` 是 9/23 早段小峰时点,
   与 9/25 盘前武装时刻不是一回事。
2. **`broker_remark_prefix` 记录与实际下单前缀不一致**: metadata 记 `ext_exit_SL_BREAKEVEN`,
   实际券商单 remark=`pbe_*`。根因: `extended_hours_exit_executor.py` 把前缀写在嵌套
   `metadata["extended_hours_exit"]["broker_remark_prefix"]`, 而 `exit_lifecycle.reserve_leg`
   只认 `decision.metadata` 的**顶层** `broker_remark_prefix` (且 intent session=ETH) →
   读不到 → 回退 `pbe_`。后果不只是观测字段: `reconcile_extended_hours_exits.py` 与
   `orphan_ext_order_watch.py` 均按 `remark.startswith("ext_exit_")` 过滤, ETH 单对
   对账环/孤儿单监测**失明**。

## 2. 修复 (最小变更)

### 2.1 `app/playbook/pipeline.py` — MFE/MAE 恒取峰谷真值 (~15 行)

`_close_trade_record` 内: 原「仅 `mfe_pct<=0` 才从 peak_price 反算」改为**恒取
max(累积值, peak_price 反算, peak_price_bar 反算)**; MAE 对称 (trough_price/trough_price_bar)。
与既有 `_realized_fav` 落袋兜底同型。`_mfe_accum` 保留累积基线。

**推送时点防混读**: `_px_extra` 组装处, 若最终 mfe > 累积值 (即峰值来自重算, 旧
`time_to_mfe` 时点已失真), `time_to_mfe`/`time_to_mfe_trading_min` 置 None →
推送不再显示 "(Xmin到峰)"。

**影响面分析** (mfe_pct/mae_pct 列消费者逐一核过):
- 交易侧唯一消费者 = `_symbol_streak_ban` (低MFE连败封禁, mfe<0.5 才计入) — mfe 更真
  只会**缩小**误封禁, 不放大;
- 其余 (推送 / shadow_board / edge_weekly_report / breakeven_shadow_settle) 皆观测面;
- 50% 量纲夹取 (`_MFE_MAE_SANE_MAX`) 位置不变, 仍在重算之后。

**MU 案回放验证**: 新逻辑得 mfe=0.4375% (bar峰1092.8), capture=83.2% (原 100%), t2m 抑制=True。

### 2.2 `scripts/extended_hours_exit_executor.py` — remark 契约对齐 (1 行 + 注释)

`metadata["broker_remark_prefix"] = f"ext_exit_{reason}"` 补顶层键; 嵌套键保留 (向后兼容)。

**功能验证** (真 ExitLedger 实例, 三向):
- ETH + 顶层键 (修后) → remark=`ext_exit_SL_BREAKEVEN*` ✓ (对账环/孤儿监测恢复可见)
- ETH + 仅嵌套键 (修前对照) → 仍 `pbe_*` (证明修复点精确, 无契约破坏)
- RTH + 顶层键 → 仍 `pbe_*` (reserve_leg 的 session=ETH 前置条件未动)

### 2.3 测试

`test_extended_hours_order_execution.py` 39 passed; `test_playbook_pipeline.py` +
`test_patient_exit.py` + `test_rfc080_system_order_remark.py` +
`test_extended_hours_reconciliation.py` 133 passed; `test_extended_hours_exit_book_repair.py` +
`test_external_close_book_reconciliation.py` + `test_rfc080_half_flip_interlock.py` +
`test_control_loop_ledger.py` 93 passed。py_compile 两文件 OK。
既有断言 `exit_decision["metadata"]["extended_hours_exit"] == {...}` 不受影响 (嵌套键原样)。

## 3. B21 预注册 (docs/ADJUDICATED-CRITERIA.md, 用户裁「按建议执行」第②条)

「延长时段利润保护出场后错失上行」影子: 样本=两条 ETH 退出影子的执行类 action 行,
结算复用既有 `settle_extended_hours.py` holdout (cron 已挂, 零新代码)。判据:
n≥20 ∧ d0_close_ret_pct 均值>费用线 ∧ ≥60% 为正 → 提案宽限窗另立 DEB+用户裁;
不过线 → 维持「触线即出」, 留档反证。首样本 = MU 9/25。
背景硬约束: 富途历史K线/快照**无盘前数据** (实测 request_history_kline 9/25 = 0 行),
tick 级盘前路径唯一持久源 = 影子 jsonl 逐分钟行, 事后无法补拉。

## 4. 9/18 ACN/WBD 挂账补核 (建议③)

`extended_hours_exit_executions.jsonl` 中两笔 `exit_state=HELD / exit_order_evidence_pending`
(9/18 盘后 SL_TRAIL_LOCK) 实为**等证据中间态, 非失败**: 同晚已由历史对账补平 —
`playbook_exits.db` exit_intents 两笔均 `state=BOOKED`; trade_records id=1543 ACN
净 +$59.09 / id=1544 WBD 净 +$51.66 均已落账; 当前无非终态 intent。挂账关闭。

## 5. 部署

- `scripts/extended_hours_exit_executor.py`: cron 每分钟新进程, 下一 tick 自动生效。
- `app/playbook/pipeline.py`: 常驻服务需 `systemctl restart ftseek-us` (HotReloader 穿不透
  playbook.*)。重启前置已核: 未平仓仅 AVGO 16股 (sim, synced), 改动归属本批 3 文件。
- 回滚: `git revert` 本提交即可 (零配置/零 DB schema 变更)。
