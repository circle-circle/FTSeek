# ADR-034: 组合风控 P1 日亏熔断死键接线 + P2 簇浓度影子

**日期**: 2026-08-27
**状态**: 已采纳 (用户裁决 "同意建议", 承接同日组合风控讨论)
**关联**: ADR-032 (死门修复同款方法论) / ADR-033 (同日影子批次) / 8/27 组合风控盘点

## 背景

组合风控盘点 (2026-08-27) 发现: 单票维度风控完整 (单名帽8%/总敞口80%/波动率桶SL/
连亏冷却), 但组合维度基本为零 — 净方向 ~90% LONG 是涌现属性、`risk.circuit_breaker.
strategy.daily_loss_threshold: -0.03` 自 2026-05-21 写入起全仓零消费 (死键),
"组合级日亏熔断"从未存在。压力测算: 12笔LONG×$5.5k均名义, 单日-3%纳指让SL同日触发
≈ -$1,700~-$3,000 (-1.8%~-3.1%净值), 恰落在死键阈值区间 — 写配置的人想到了, 没接线。

另: 周报路径的 `app/playbook/risk_circuit_breaker.py` 只在周五"仅记录告警不阻断",
阈值用构造默认值不读 yaml, 且消费代码读不存在的 `status["action"]` 键 — 名存实亡。

## P1: 日亏熔断接线 (执行路径变更)

`pipeline._check_daily_loss_circuit` 挂 auto_execute 开仓门 (risk_state 检查后):

- **口径**: 富途实时净值 (capital_safety.current_equity, 30s刷新, 与
  _resolve_sizing_base 同尺) vs 当日首评基线快照 (data/daily_loss_baseline.json)。
  已实现+未实现+全部交易来源天然包含 — 账户级亏损无漏项; 基线与现值强制同源,
  净值取不到时本轮跳过 (fail-open+日志) — 混用 config 兜底尺子会制造 ±15% 级假触发。
- **动作**: 当日净值跌幅 ≤ `daily_loss_threshold` (-3%) → 本轮停新开; 平仓照常
  (monitor 路径不经过此门)。自恢复: 净值回升过阈即放行, 无 pause 状态机。
- **sim 期数据产量影响**: 8月最差实盘日 8/14 -794$ ≈ -0.8%, 远不及 -3% — 纯尾部
  保护, 不伤攒数据。未采纳"触发后放行1笔观察仓"选项 (复杂度不值)。
- **范围诚实声明**: 仅拦 auto_execute 自动开仓, 手动下单通道不经过此门 (手动路径
  的工具是 kill switch)。`hourly_loss_threshold` 仍为死键 (需滚动窗口基线+暂停
  状态机, 超 P1 范围, yaml 已标注)。
- **回滚**: 阈值改 -1.0 + 重启 (等效失效, 无需改码)。
- **验证**: tests/test_daily_loss_circuit.py 9例 (触发算术/首评落基线/自恢复/
  fail-open/同尺保护/阈值SSOT/坏文件容错); 真实环境冒烟: 净值$98,945基线落盘,
  二评 loss_pct=0.0 vs threshold -3.0 正确。

## P2: 簇浓度影子 (record-only)

`scripts/cluster_concentration_shadow.py`, AFTER_HOURS 挂载, 全量重放幂等:
手工簇映射 (semis/software_cloud/high_beta/china_adr/gold_miners/pharma/
financial/consumer/industrial_energy/other) + 占位区间 [entry,exit) 重放,
每笔开仓记同簇并发数 (含自身, 与排序无关), 任意 cap 阈值可离线重切。

**首跑实证 (n=18日, 8/3-8/26) — 证伪了本 ADR 的原始动机**:
- 最大单簇并发仅 **5** (8/18 semis: WDC/SNDK/MU/LRCX/AMD时代);
- cap 敏感性: cap3 拦10笔被拦合计 **+$39** (拦了反亏) / cap4 拦1笔 -$9 / cap5+ 零拦截;
- 根因: 日内 churn (多数仓1-2小时了结) 把同刻浓度压得很低 — "12并发仓=3-4个相关
  赌注"的讨论框架错了, 那是**日累计开仓**非**同刻在场** (8/24实据: AVGO 9:48-11:02,
  AMD 10:33-12:04, 同刻semis并发仅2)。

结论: 簇帽在任何档位 (≥3) 均无正收益证据。影子继续攒样 (判据: n≥30日+blocked≥30笔
+净差>0+符号p<0.05 才议真簇帽), 但预期是维持"无簇帽"现状。负结果同样有价值:
省掉一个会反向伤害收益的门。

## 影响与纪律声明

P1 属执行路径新增拦截门 (sim 期) — 用户裁决 "同意建议" 后执行, 变更日期在案。
P2 零执行路径改动。两项回滚均为配置键/阈值, 无数据迁移。
