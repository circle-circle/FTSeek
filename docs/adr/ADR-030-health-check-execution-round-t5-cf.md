# ADR-030: 体检执行轮 — T+5 反事实尺子 + BABA 碰撞修复 + order_executor 测试安全网

**日期**: 2026-08-22
**状态**: 已采纳 (用户裁决 "按照建议执行, 并将你的发现和分析更新到deb文档中")
**关联**: DEB-20260822-health-check-t5-counterfactual (唯一详细事实源), DEB-20260820 双维审计 (P1-3/P0-2 出处), ADR-021 (白名单先例)
**纪律**: 验证期白名单内 (修尺子/攒样本/做减法), 零重启、零配置变更、零实弹行为变更; 08-29 终裁前不构成任何机制变更

## 背景

独立体检会话交付全系统分析后, 用户令按建议执行。本 ADR 记录其中触及交易主路径守护名单 (`app/pipeline/order_executor.py`) 与数据层的两项, 其余细节见 DEB。

## 决策

1. **T+5 摆动反事实尺子上线为只读工具** `scripts/t5_hold_counterfactual.py` + cron 06:53 CST Tue-Sat。
   定性: 描述性证据, 供 PIVOT"更长持有期"赛道判读; **不替代** direction_counterfactual_ruler 预注册 T+3/T+5 终裁门。
2. **BABA open_positions 行恢复 auto/simulate** (曾被 position_sync 自管隔离分支翻转为 manual/real, 系用户面板手动单与 sim 仓撞 symbol 单主键行, 无资金事故)。同标的跨环境碰撞的结构性缺口登记 P1-8, 终裁后才动机制。
3. **order_executor.py `_is_option_contract` 切片逻辑 → OCC 正则**: 休眠路径正确性钉死 (全库期权代码 0 条, 三消费方均 PnL 计算 → 现网行为零变化); 配套 13 例纯函数测试作 P1-3 前置最小安全网。`_build_close_metadata_json` 半死容错分支**不在本批修** (冻结纪律), 归 P2。
4. **config/exit_calibration.json 出库** (.gitignore): cron 全量重生的人读产物, 全库零消费方。
   注: 该删除曾随并行会话 078552c5 裹挟入库、32070090 按"非其改动范围"恢复; 本 ADR 落档后由执行批重新落地, 因果以此为准。

## 回滚

| # | 回滚 |
|---|---|
| 1 | 删脚本 + 删 cron 行 + 删 data/t5_hold_cf_*.{json,jsonl} + data/t5_cf_spy_bars.json |
| 2 | `UPDATE open_positions SET owner='manual', account_type='real' WHERE symbol='US.BABA'` (不建议, 除非用户明示接管) |
| 3 | revert order_executor.py 对应 hunk + 删 tests/execution/test_order_executor_pure_minimal.py |
| 4 | 删 .gitignore 尾两行 + git add config/exit_calibration.json |
