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

## 追加 (2026-09-05, 用户裁"同意，按照建议执行"): P1 日亏闸加滞回带 + latch 持久化

DEB-20260905 §9.4 (第二批 · 熔断"解除与观察"机制) 修订本 ADR 的 P1 **解除语义**。

**改了什么**: 原 P1 "自恢复: 净值回升过阈即放行, **无 pause 状态机**" → 现为触发 -3% /
放行 -2% 的**滞回带** + `latched` 状态机:

- ≤ `daily_loss_threshold` (-3%) → 触发并锁存;
- 回升到 ≥ `daily_loss_release_threshold` (-2%, **新键**) → 解除;
- 两线之间 (-3%~-2%) → **维持已触发态**。

`latched` 与基线同文件持久化 (`data/daily_loss_baseline.json`), 跨重启不丢, 新交易日首评清零,
仅**状态翻转**才写盘 (本函数每轮 auto_execute 调一次)。返回键新增 `raw_breach` (原始越线与否)
与 `release_threshold_pct`, 使"越线"与"锁存态"可区分。

**为何**: 原实现放行线 == 触发线且每轮独立重算 → 净值在 -3% 附近来回时闸门逐轮 on/off 抖动。
后果不是"多拦一笔", 而是**同一交易日内入场口径不稳定** — 数据采集模式 (DEB-20260905 M-1) 下
采到的样本分不清"被熔断拦下"还是"恰好那一轮放行", 直接污染对照性。

**范围与不变量**: 仍 entry-only (只拦 auto_execute 新开仓, 平仓/monitor 不经过此门);
口径仍是富途实时净值 vs 当日首评基线 (同尺); fail-open 语义不变。
**未同步放宽的**: AbsoluteCircuitBreaker 的日亏 -$2000 闸**保持单向锁存**
(建议5 明确不动 — 它守尾部事故, 给它加自动复活等于在最极端那天反复开关闸门;
`test_daily_loss_stayed_latched_despite_recovery` 反向锁死)。

**边界容差 (本批自审抓到的缺陷)**: `_pct = _eq/_base_eq - 1` 恰卡线时有 IEEE754 表示误差 —
实证 `98000/100000-1 = -0.020000000000000018 < -0.02` ⇒ 净值正好回升到放行线被判"仍在带内",
**解除永久失效** (滞回带反成单向焊死, 恰是本修订要治的病)。修法 = 阈值比较加 `1e-9`
**不对称容差** (触发线 `_pct <= _thr+EPS` 稍早触发 = 保守; 放行线 `_pct < _rel-EPS` 卡线即解除
= 与本 ADR 原口径一致), 按仓库纪律**修代码不抬旧测基线** — 缺陷正是由本 ADR 原 9 例中的
`test_recovers_when_equity_rebounds` 撞红暴露 (该文件在 `tests/` 根目录, 只跑 `tests/risk|playbook`
会漏检, 教训入档)。

**测试**: 新增 `tests/playbook/test_daily_loss_circuit_hysteresis.py` 11 例 (带内维持 / 恰卡放行线
仍解除 / 恰卡触发线仍触发 / 跨重启 / 误配守卫 / 未触发不误拦 / 每翻转只写盘一次) +
本 ADR 原 `tests/test_daily_loss_circuit.py` 9 例全绿。

**回滚**: `risk.circuit_breaker.strategy.daily_loss_release_threshold` 改成与 `daily_loss_threshold`
同值 (-0.03) 即等效旧行为 (**热载**, 无需重启)。健全性守卫: 该键若被配得比触发线更负
(永不解除方向), 代码自动退回无滞回 — 宁可抖动也不把闸门焊死。
