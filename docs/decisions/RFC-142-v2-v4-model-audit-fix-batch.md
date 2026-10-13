# RFC-142: v2/v4 多模型审计修复批 (CapitalAllocator / Broker / exit_reason / ETH边界)

日期: 2026-10-13
状态: 已实施 (Codex 执行, Hermes 独立复查后提交)
来源: code_analysis_results/v2, v4 报告 (5 模型评测 + Hermes 人工取证)

## 背景

v2 (代码缺陷审计) 与 v4 (交易策略复查, 8月起 real 窗口) 两轮 5 模型评测产出共识发现,
经 Hermes 逐条人工取证 (部分证伪, 见 v3/v4 报告附录), Codex 实施修复, 本 RFC 落档。

## 变更清单

### v2 共识 — CapitalAllocator (app/coordinator/capital_allocator.py)
1. observe 模式: 放行并记录影子敞口 (修 reserve/release 账本不对称, 5/5 模型命中)
2. _coerce_position_pct: NaN/非正/非有限一律拒绝 (4/5 模型命中)
3. Lock→RLock + reconcile 收进锁内 (修 reset_and_reconcile 锁外竞态)
4. auto_capacity_report: fail-open → degraded 标记 + unknown 计数
5. 新增 BLOCKED_UNKNOWN_BUCKET; compute_scale_factor 非法输入不再返回 1.0

### Broker/执行层
6. broker_adapter: trd_side 强制显式 + 四值映射, 缺失/非法拒单 (修 SELL_SHORT 被吞)
7. broker_adapter: 实盘断连判定按 trade_env + allow_offline_simulated_fill (防虚假模拟成交)
8. order_executor: _reserve_capital_for_order 接入下单路径 (幂等键 + 平仓释放)
9. position_monitor: scale-out 预检异常改为中止; 补单显式 trd_side

### v4 — exit_reason 分类法
10. 新增 app/risk/exit_reasons.py 归一化层; trade_store + close_flow 双写入边界接线
11. trade_records 补 fees_est/pnl_net/net_pnl_pct 列 (向后兼容 ALTER)

### ETH 边界
12. extended_hours_exit: 边界前最后提交截止 (限价等待 20s + 撤单余量 10s)

## 验证

- 相关测试套件 326 全绿 (coordinator/execution/playbook/trade_store 等 7 文件)
- 版本棘轮: tests/test_da_output_contract.py 常数同步 skill v0.95
- 取证记录: 各模型发现的支持/证伪结论见 v3/v4 报告附录

## 未覆盖 (下批立项)

- v4 策略三项: SL_INITIAL 桶上限接入 playbook 链 / OVERNIGHT_GAP_STOP 券商侧挂单 /
  SL_BREAKEVEN 0.4%→0.8% retune (replay 验证)
- 历史小写 exit_reason 脏行清洗 (CTNT 类, writer 已消亡)

## 回滚

git revert 本批 commit; observe_only=True 下 CapitalAllocator 不阻断, 回滚风险低。
