# RFC-137: LIVE 券商实收费对账

> **状态**: 已实施，待生产下笔自然验证
> **日期**: 2026-10-08
> **承接**: RFC-131 / RFC-132 / RFC-135

## 1. 动机

SIM 订单无法通过 `order_fee_query` 拿到券商实收费；LIVE 可以。此前 LIVE 平仓
使用 `fee_model` 估算并写 `fees_source='est'`，适合下单前 sizing，但不是事后
会计金标准。1 股探针的费用占比极高，必须把“工程验证成本”和“策略毛利/净利”
分开核算。

## 2. 设计

LIVE `PLAYBOOK_LIVE` 平仓并写入本地台账后：

1. 用入场单、平仓单两个 order id 调 `order_fee_query(REAL)`；
2. 两单费用都返回才接受，缺失任意一单则 fail-close 不改账；
3. `fees_est` 升级为券商实收合计，`pnl_net = realized_pnl - fees_est`；
4. `fees_source='broker'`；
5. `metadata_json.fee_breakdown` 保留原估算分项，并新增：
   - `broker.entry_fee / exit_fee / total`
   - `estimated_total`
   - `broker_total`
   - `difference`
   - `source='broker'`
6. 平仓推送改用券商实收费展示净额。

若 metadata 中有 `playbook_instruction_id`，同一事务同步更新
`playbook_instructions.pnl / pnl_pct / outcome`，避免指令表与交易台账出现
“一表估算、一表实收”的口径分裂。

估算明细不被覆盖，方便后续校准 `fee_model`。

## 3. 边界

- 只作用于 `source_type='PLAYBOOK_LIVE'` 且 `trade_mode='live'`；
- SIM 行零变化；
- 查询失败只告警，不阻断、不回滚、不重开已平仓交易；
- `broker/reconciled` 状态不会被估算或重复对账覆盖；
- `order_fee_query` 是事后接口，下单前 sizing 仍使用 `fee_model`；
- 费用差异超过 `max($0.05, 估算×2%)` 时推送 HIGH 告警；
- 实收费只改会计真值，不改变策略决策。

## 4. 验证

```bash
venv/bin/python -m pytest \
  tests/execution/test_live_fee_reconciliation.py \
  tests/execution/test_dual_broker_positions.py -q

venv/bin/python -m pytest \
  tests/playbook/test_live_arm_p0_contracts.py \
  tests/playbook/test_dual_arm_routing.py \
  tests/playbook/test_live_probe_admission_exemption.py \
  tests/playbook/test_exit_recovery.py -q
```

当前结果：`13 passed`、`34 passed`、`40 passed`、`181 passed`，
`py_compile` 通过。合并运行 `live_arm_p0 → exit_recovery` 曾出现
`55 passed + 19 errors`：根因**不是既有隔离问题**，而是本功能初版在
`_close_trade_record` 内直接调 `get_dual_broker()` 单例，p0 的
`test_broker_stop_fill_is_reconciled_without_ghost_position`（PLAYBOOK_LIVE 行）
绕过注入的 FakeDualBroker 打开真实 Futu socket，后台线程污染后续 I/O guard。
HEAD 基线同命令合并跑 `55 passed` 实证为回归。复查修复：`_close_trade_record`
新增 `fee_broker` 注入参数，两个调用点传入链路已有的 `dual`，并加
`test_broker_stop_reconciliation_needs_no_broker_singleton` 回归守卫；修复后
四套件合并 `75 passed`（无 Futu socket 线程）。

## 5. 生产回填

2026-10-08 已对 PBR / CEG / BKV 三笔 LIVE 探针做幂等回填：

| 标的 | 券商实收 | 原估算 | 差异 | 净额 |
|---|---:|---:|---:|---:|
| PBR | $3.99 | $4.01 | -$0.02 | -$3.89 |
| CEG | $3.99 | $4.01 | -$0.02 | -$2.65 |
| BKV | $3.99 | $4.01 | -$0.02 | -$3.77 |

三笔合计：毛利 `+$1.66`，券商实收费 `$11.97`，净额 `-$10.31`。
回滚备份：`data/backups/ftseek_us_live_before_fee_reconcile_20261008_102314.db`。

## 6. 后续观察

- 下一笔 LIVE 平仓应出现 `fees_source='broker'`；
- 推送净额应与券商实收费一致；
- 若差异超阈值，检查 `fee_model` 费率表或多笔成交取整口径；
- 长期样本稳定后，再评估是否用实收差异校准费用模型。
