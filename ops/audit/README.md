# ops/audit — DEB 议题配套复核脚本 (record-only, DB mode=ro)

一次性审计/复核工具, 随对应 DEB 议题入库留痕, 供另一 agent 跨会话复跑。
**全部只读** (`file:...?mode=ro`), 零落库零下单零配置改动。直接
`cd /opt/ftseek/FTSeek-US && ./venv/bin/python ops/audit/<脚本>.py` 即可复现。

## DEB-20260916-da-source-traceback-reprice-geometry

DA 指令源头回溯 (识别层 vs 执行层 re-price 几何改写) 的实证脚本:

| 脚本 | 用途 | 窗口 |
|---|---|---|
| `audit_planned_vs_landed.py` | 全窗量化: DA planned_entry/SL/TP (raw_instruction_json, DB 永久保留) vs 落库 (re-price 后) 的 RR/SL距/TP 漂移 + re-priced vs 未re-price 组结局 | `--since`, 默认 2026-08-15 |
| `audit_inv_cross_full.py` | 全量: 落库 SL 是否跨过 DA 自己的 invalidation_triggers 价格型失效线 | `--since`, 默认 2026-08-15 |
| `audit_cross_outcome.py` | **核心读数**: 跨失效线组 vs 未跨组的已平仓结局 + **按日配对差 t/MDE(80%)** — 决定"几何守卫"是否值得另提 | `--since`, 默认 2026-08-15 |
| `trace_arm_origin.py` | ARM 9/15 单例深挖: DA 原始 execution_plan vs 落库指令的风险几何 + 挑战者独立性 + 输入缺口申报 | `--checkpoint`, `--decision-id` |
| `audit_inputs.py` | DA 输入时点 & planned_entry 当日可达性 (1min bar 真值) + re-price 机械公式溯源 + TP 触发史 | `--checkpoint-date`, `--db-since`; bar 字段序 b[2]=high/b[3]=low |
| `audit_da_pricing_quality.py` | §七 预测力检验: DA SL距/RR/追价距离 分桶+Spearman ρ vs 结局 (决定定价直通是否值得) | `--since`, 默认 2026-08-01 |
| `drill_chase_zero.py` | §七 追价≈0 桶下钻: 拆"按DA原价成交反而最差"的组成伪影 (月份/确认位有无) | `--since`, 默认 2026-08-01 |

### 复核方改窗口的方法
三个全窗脚本均支持 `--since YYYY-MM-DD`（不再改源码）:
```bash
./venv/bin/python ops/audit/audit_cross_outcome.py --since 2026-08-15
./venv/bin/python ops/audit/audit_cross_outcome.py --since "$(date -d '7 days ago' +%F)"
```
注意：已平仓配对样本中数值型 `invalidation_triggers` 最早出现在 2026-08-26；把 `--since` 提前只会覆盖全部可用历史，不能回填 7 月。`audit_cross_outcome.py` 的 pooled 净额只作明细，裁决读数必须看"日聚类配对差 + MDE(80%)"行。
**不依赖 checkpoint** — 全窗量化用 `raw_instruction_json` (DB 永久), checkpoint 仅 `trace_arm_origin.py`/`audit_inputs.py` 单例用 (保留期仅 2 天, 见 DEB 附带发现 C)。

### bar 字段序陷阱 (踩过的坑)
`data/replay_bars/US_<SYM>_<date>.json` 每根 = `[time, open, high, low, close]`,
即 **b[1]=open / b[2]=high / b[3]=low / b[4]=close**。`audit_inputs.py` 首版误用
b[3]/b[4] 当 high/low → COIN 算出"高<开"的物理不可能值, 已修正为 b[2]/b[3]。
复核若重写可达性判断, 务必对照 `scripts/cf_pullback_limit_replay.py:102-119` 的同一字段序。
