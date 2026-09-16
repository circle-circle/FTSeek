# ops/audit — DEB 议题配套复核脚本 (record-only, DB mode=ro)

一次性审计/复核工具, 随对应 DEB 议题入库留痕, 供另一 agent 跨会话复跑。
**全部只读** (`file:...?mode=ro`), 零落库零下单零配置改动。直接
`cd /opt/ftseek/FTSeek-US && ./venv/bin/python ops/audit/<脚本>.py` 即可复现。

## DEB-20260916-da-source-traceback-reprice-geometry

DA 指令源头回溯 (识别层 vs 执行层 re-price 几何改写) 的实证脚本:

| 脚本 | 用途 | 硬编码 |
|---|---|---|
| `audit_planned_vs_landed.py` | 全窗量化: DA planned_entry/SL/TP (raw_instruction_json, DB 永久保留) vs 落库 (re-price 后) 的 RR/SL距/TP 漂移 + re-priced vs 未re-price 组结局 | 窗口 `date>='2026-08-15'` |
| `audit_inv_cross_full.py` | 全量: 落库 SL 是否跨过 DA 自己的 invalidation_triggers 价格型失效线 | 同上窗口 |
| `audit_cross_outcome.py` | **核心反证**: 跨失效线组 vs 未跨组的已平仓结局对比 (净盈亏/WR/SL_INITIAL/mfe=0) — 决定"几何守卫"该不该做 | 同上窗口 |
| `trace_arm_origin.py` | ARM 9/15 单例深挖: DA 原始 execution_plan vs 落库指令的风险几何 + 挑战者独立性 + 输入缺口申报 | id=2697, checkpoint 2026-09-15/US.ARM |
| `audit_inputs.py` | DA 输入时点 & planned_entry 当日可达性 (1min bar 真值) + re-price 机械公式溯源 + TP 触发史 | checkpoint 2026-09-15, bar 字段序 b[2]=high/b[3]=low |

### 复核方改窗口的方法
三个 `audit_*.py` 的窗口在 SQL `WHERE i.date >= '2026-08-15'` 一处, 改成更早 (如 `2026-07-01`) 即可扩样本验证"跨线组结局中性"反证 (DEB 关键分歧1 的可验证方式)。
**不依赖 checkpoint** — 全窗量化用 `raw_instruction_json` (DB 永久), checkpoint 仅 `trace_arm_origin.py`/`audit_inputs.py` 单例用 (保留期仅 2 天, 见 DEB 附带发现 C)。

### bar 字段序陷阱 (踩过的坑)
`data/replay_bars/US_<SYM>_<date>.json` 每根 = `[time, open, high, low, close]`,
即 **b[1]=open / b[2]=high / b[3]=low / b[4]=close**。`audit_inputs.py` 首版误用
b[3]/b[4] 当 high/low → COIN 算出"高<开"的物理不可能值, 已修正为 b[2]/b[3]。
复核若重写可达性判断, 务必对照 `scripts/cf_pullback_limit_replay.py:102-119` 的同一字段序。
