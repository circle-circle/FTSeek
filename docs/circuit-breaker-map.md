# 熔断/风控分层地图 (2026-07-26, 减法R2-5d 裁决产物)

> 结论: 6 套"熔断"是**分层风控而非重复** — 触发条件正交, 合一反而制造单点故障。
> 本文回答"谁管什么/何时触发/怎么恢复/谁在调用", 消除"5 套熔断"的重复错觉。
> 维护约定: 新增任何熔断类机制前, 先对照本表确认层位, 禁止再造平行层。

| 模块 | 层位 | 管什么 | 触发条件 | 恢复方式 | 主要调用方 |
|---|---|---|---|---|---|
| `risk/absolute_circuit_breaker.py` | L0 绝对线 | 账户生死线 | 净值绝对回撤触及硬线 | 人工确认 | risk_engine |
| `risk/capital_safety.py` | L1 账户水位 | 资金水位/预算分配 (green/yellow/red) | 回撤分档 (-2% 停新仓 / -3% 全平) | 水位回升自动 | 32 文件 (预算/仓位/playbook) |
| `risk/account_circuit_breaker.py` | L1.5 账户熔断 | 递减式风控 (减仓→停新→全平) | 日内亏损阈值分级 | 按级恢复 | scheduler/执行链 |
| `risk/market_circuit_breaker.py` | L2 市场线 | 大盘异动熔断 (VIX/SPY 急变) | 市场级触发 | 市场平稳后 | 盘中监控 |
| `risk/circuit_breaker.py` | L3 策略级 | 单策略连续亏损熔断 + 数据源熔断 | 策略连亏 N 笔 / 数据源失败率 | 冷却期后 | 23 文件 (策略/数据源) |
| `risk/drawdown_control.py` | L4 仓位级 | 单仓回撤控制 | 单仓回撤阈值 | 平仓即止 | 退出链 |

相关但不属熔断: `emergency_interceptor.check_kill_switch` (人工急停开关, 文件哨兵语义,
读磁盘实时值绕过热重载延迟 — 三处裸 regex 读法是刻意设计, 勿"优化"成 get_dotted)。

历史: 曾有第 7 套 `risk/kill_switch.py` (与 emergency_interceptor 重复) — 2026-07-26 减法R2 已删。
