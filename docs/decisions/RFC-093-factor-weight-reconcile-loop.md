# RFC-093: 因子权重对账环路 — IC 通道 × 台账 verdict × 现金口径

> **状态**: 已落地 (2026-09-21, 用户对"对账清单 + 只读告警"回「按建议执行」)
> **类型**: L1 (新只读探测器 + 载体 + 台账登记 + 契约测试; **零交易行为变更** — 未改权重/配置/DB 判据)
> **触发**: 因子权重三方对账 (对账结果与待裁项 = `docs/debates/DEB-20260921-factor-weight-verdict-reconciliation.md`)
> **前置教训**: 注册表/探测器"有入口无出口"同族病灶 (RFC-092 §1) —— 数字一直在产出但无对账、无读者

## 1. 立这个环的理由 (实测, 非推测)

playbook screener 对**触发**因子的加分由三处相加（代码实测）：

```
eff = base(config/playbook_screening.yaml) + ic_adj(data/factor_ic_latest.json) + Σhist(weight_adjustments 全量)
```

而同一个因子另有两套独立读数：台账 verdict（`factor_analyzer.classify_factor`，贝叶斯胜率区间 + PF）与现金口径（同表净额）。
**三条反馈回路各写各的，没有任何一处在对账** ⇒ 2026-09-21 实测出七类错配
（正权重×负证据 5 / IC 与台账反号 5 / Σhist 双计存量 11 / 小样本高权重 1 / 罚错 1 / 尺子冲突 3 / 台账分母口径 8，跨类有重叠）。

其中两条是"看不见的失败"级：

1. `ic_adj` 在 `screener._calculate_score` 里被**无条件**调用（`ic_feedback_to_screener` flag 只门控 strategy 级加分）
   ⇒ "关掉 IC 反馈"关不掉它，反号每天在打分里生效；
2. 台账 `verdict=negative` 有一半靠**分母口径**撑（`missed`/`already_held`/`void` 全进胜率分母，窗口占比 44%）
   ⇒ 任何"按台账裁因子"的动作都在对 40% 噪声排序（详见 DEB §4.1）。

## 2. 环路定义

| 项 | 内容 |
|----|------|
| 层 | **元环 §2.5（测量免疫系统）** —— 不产生交易决策，只让"三把尺子互相矛盾"可见 |
| 子脚本 | `scripts/factor_weight_reconcile.py`（纯只读：台账以 `mode=ro` 打开；不写 config/prompt/DB） |
| 载体 | `ops/hermes/factor_weight_reconcile_carrier.py`（线上副本 `/root/.hermes/scripts/` 同 sha256）+ Hermes job **`241c896213eb`** |
| 调度 | `50 6 * * 6`（周六 06:50 CST，在 factor_ic_backfill 05:40 与周级台账刷新之后） |
| 产物 | `data/factor_weight_reconcile_latest.json`（含 items / mismatches / signature）+ state `data/factor_weight_reconcile_state.json` |
| 取证 | `logs/factor_weight_reconcile.log` 每轮一行 `[carrier …] rc/dur/bytes`（**静默轮也留痕** ⇒ "没推送≠没跑"） |
| 判据 | 七类错配的判据写在脚本头（A–G），阈值：台账样本门 30、IC 门 `|IC|>0.05 且 n≥30`、剔零WR 门 50% |
| 输出契约 | **静默类**：对账面签名（`verdict \| n档 \| eff符号 \| ic_adj \| 金额符号 \| 剔零WR档`）无变化 ⇒ 零输出 = 不推送；首跑或变化 ⇒ 投递全量板 |
| 退出码 | 0 正常（含静默）；**2 = 探测失败**（输入缺失/不可读），必须可见、严禁读成"无错配" |

权限边界：**L1 只读**。环路不得改自身的判据之外的任何代码，也不得改权重/配置/门禁；
权重处置一律由 DEB §5 的待裁项 + 用户裁决走（L3）。

## 3. 失效模式与对策

| 失效模式 | 对策 |
|----------|------|
| 环路自己静默死掉（gateway 死 / 载体不在） | 每轮取证头落 `logs/`；state/产物 mtime 可外部回读；载体 rc≠0 由 Hermes 记 failed 并投递异常行 |
| 探测失败被读成"无错配" | rc=2 + 显式"探测失败…不得读成『无错配』"行；测试钉死 |
| 静默契约被签名里的易变字段破坏（每轮都"变" ⇒ 每周都推） | 签名只取状态维度（不含绝对金额与 mtime）；测试钉"同面对账面零输出" |
| 判据写死成"永远全绿" | 七类判据逐类有检出测试（合成行），disabled 因子不进 A 类的反例钉 |
| 台账窗口/读库漂移（sim→live 切换） | 读库路径支持 `FTSEEK_LEDGER_DB` 覆盖；输出头部打印实际读的库与两侧快照时间 |
| 产物陈旧被当有效读数 | 输出头部固定打印 `ledger_updated_at` 与 IC 快照时间 |

## 4. 回滚

删 Hermes job `241c896213eb` + 删 `ops/hermes/factor_weight_reconcile_carrier.py` 与线上副本
+ 删 state/产物 + 删 `scripts/factor_weight_reconcile.py` 与 `tests/test_factor_weight_reconcile.py`
+ 删台账 §2.5 对应行 + 本文件。（零交易行为依赖：无任何生产代码 import 本环。）

## 5. 契约测试

`tests/test_factor_weight_reconcile.py`（6 例）：首跑全量 / 同面静默 / 签名变化重抬 / 七类判据逐类可检出 /
disabled 不进 A 类 / 探测失败 rc=2 可见 / 源码级只读钉（`mode=ro`、无 `yaml.dump`、无台账写语句）。