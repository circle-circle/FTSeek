# 熔断/风控分层地图 (2026-07-26 立, 2026-10-15 全链路审计刷新)

> 结论: 6 套"熔断"是**分层风控而非重复** — 触发条件正交, 合一反而制造单点故障。
> 本文回答"谁管什么/何时触发/怎么恢复/谁在调用", 消除"N 套熔断"的重复错觉。
> 维护约定: 新增任何熔断类机制前, 先对照本表确认层位, 禁止再造平行层。
> **2026-10-15 刷新说明**: 审计发现 L1 阈值写错 (写 −2%/−3% 实为空降档) + 5 个实际拦截层未收录
> + 2 处语义纸面。本版全部修正; 「当日亏损事实六套权威并存」登记见
> `app/risk/symbol_daily_loss_guard.py:22-38` (合并/裁剪属人工风险裁决, 未经裁决不得动)。

## 主表 (拦截链上真实存在的层)

| 模块 | 层位 | 管什么 | 触发条件 | 恢复方式 | 主要调用方 |
|---|---|---|---|---|---|
| `risk/absolute_circuit_breaker.py` | L0 绝对线 | 账户生死线 | 净值绝对回撤触及硬线 | 人工确认 | playbook_service 硬门 (fail-close) |
| `risk/capital_safety.py` | L1 账户水位 | 资金水位/预算分配 | **回撤分档 green −5% / yellow −10% / orange −20% / red −35% + BLACKOUT 锁** (`capital_safety.py:61-64`, 阈值在 `unified_config.yaml:1945-1957` water_levels) | 水位回升自动 | 32 文件 (预算/仓位/playbook) |
| `risk/account_circuit_breaker.py` | L1.5 账户熔断 | 递减式风控 (reduce→halt→liquidate) | 日内亏损 −1%/−2%/−3% 分级 | 按级恢复 | scheduler/执行链; ⚠️ **LIQUIDATE 与 HALT 同效 (只拒新仓不平存量, `account_circuit_breaker.py:8-22` 自陈)** — 别当"全平熔断"读 |
| `risk/market_circuit_breaker.py` | L2 市场线 | 大盘异动熔断 (VIX/SPY 急变) | 市场级触发 | 市场平稳后 | 盘中监控 |
| `risk/circuit_breaker.py` | L3 策略级 | 单策略连续亏损熔断 + 数据源熔断 | 策略连亏 N 笔 / 数据源失败率 | 冷却期后 | 23 文件 (策略/数据源) |
| `risk/drawdown_control.py` | L4 仓位级 | 单仓回撤控制 | 单仓回撤阈值 | 平仓即止 | 退出链 |

## 2026-10-15 补收录 (此前地图漏掉的实际拦截层)

| 模块 | 管什么 | 触发/方向 | 备注 |
|---|---|---|---|
| `risk/vix_guard.py` + `playbook_service` VIX 硬门 | VIX 高企拒新开仓 | VIX 取数异常/None/超阈 ⇒ **fail-close 拒单** (链路: `is_vixy_proxy_window` 单一口径, 四个调用方) | 2026-09 已修"纸面化"(恒 return 20.0 伪兜底已删, None 分支真实可达) |
| `playbook/pipeline_parts/risk_gates.py` 日亏闸 | playbook 日内 −3% 停新开 (滞回: −2% 放行线) | latch 落 `data/daily_loss_baseline.json`; **2026-10-15 起双臂化**: LIVE 臂武装时加评 REAL 臂 (独立 latch `_real.json`) | SIM 期 fail-open (显式), LIVE 期由启动断言反向兜住 (preflight_fail_mode 必须 closed) |
| `risk/pre_trade_gate.py` | 盘前已实现+未实现日回撤 | 非 USD currency ⇒ fail-close 拒单 (`:222-235`) | 探针 `scripts/probe_pl_val_currency.py` 2026-10-15 修键名假绿 |
| `risk/symbol_day_loss.py` | 单标的日亏 $500 | 标的级 | 六权威之一 |
| `risk/symbol_daily_loss_guard.py` | 单标的日亏守卫 | 标的级 | 头注 :22-38 登记「当日亏损事实六套权威并存」, 合并属人工裁决 |

## 语义纸面 (名实不符, 读数时别误信)

- `app/playbook/risk_circuit_breaker.py` — **名如熔断实为 record-only**: 头注自陈"从未接线,
  唯一调用方=周五周报 logger.warning, 只记录不阻断"。与 L3 `risk/circuit_breaker.py` 极易混淆。
- L1.5 的 "≥3% = LIQUIDATE 档" — 与 HALT 同效 (只拒新仓), 不平存量。

相关但不属熔断: `emergency_interceptor.check_kill_switch` (人工急停开关, 文件哨兵语义,
读磁盘实时值绕过热重载延迟 — 三处裸 regex 读法是刻意设计, 勿"优化"成 get_dotted)。

历史: 曾有第 7 套 `risk/kill_switch.py` (与 emergency_interceptor 重复) — 2026-07-26 减法R2 已删。
