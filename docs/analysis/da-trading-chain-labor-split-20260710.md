# DA 交易链路 + LLM/代码分工现状核验 (2026-07-10)

> 只读核验备忘, 供评审对照。结论: **当前所有改动零 live 交易行为变更**,
> 主链路结构未变; 会改动作的切换全部锁在 plan 的 sim 门后。

## 一、端到端链路 (LLM/代码分工)

| # | 节点 | 主导 | 证据 |
|---|---|---|---|
| 1 | DA 深度分析(LLM 合成) | **LLM** 出方向/评分/thesis/**价格数字** | `llm_synth.py` L509-517 execution_plan 要 entry/SL/TP 具体数字 |
| 2 | key_levels(支撑/阻力/现价) | **代码** 算并覆盖 LLM | `deep_analyzer.py` L212-223 用 20 日高低 |
| 3 | adapter `to_voting_result` | **抄 LLM 数字** | adapter.py L239-241, L264-266; L272 带出 invalidation |
| 4 | 写 `playbook_instructions` | 代码持久化 | db.py L119/164-165(含 entry_low/high、invalidation 列) |
| 5 | FTSeek 摄取 → generate | **代码** 定 entry(BUY=ask/SHORT=bid) | instruction_generator.py L301-311 |
| 6 | reprice | **代码** ATR 分桶重算 SL/TP | pipeline.py L2286+; `_compute_volatility_based_sl_tp` |
| 7 | 下单执行 | 代码 | pipeline 执行段 |
| 8 | 退出 | **全机械规则** | scheduler_core L892-894 注册 main/fast_lane/llm_plan |

**关键**: 整条链路 LLM 仅在节点 1(DA 入场决策, 工作区外 skill)真正参与;
执行与退出全为代码/规则。连 `LLMPlanExitPolicy` 也只读已存 LLM 数字做阈值比较
(exit_policies.py L164-193), **运行时不调 LLM**。当前退出路径无任何运行时 LLM。

## 二、是否改变交易动作? — live 动作: 零改变 (四桶)

**① 未接线(写了没人调)**
- `app/playbook/entry_reference.py` `resolve_entry_reference`: 全部命中在自身文件, 无 import。
- `app/strategy/thesis_recheck_policy.py`: flag `thesis_recheck=False`(feature_flags L36)
  且未注册进 dispatcher(scheduler_core 仅 main/fast_lane/llm_plan)。

**② 影子只读(消费但不实平)**
- `invalidation`: pipeline.py L3457-3476, 注释「不实平, 仅记录首次命中」;
  只写 `meta["invalidation_shadow"]` 累积样本, 不触发 CLOSE/REDUCE。老单无键自然跳过。

**③ 只写列(执行端不读)**
- `entry_low/entry_high`: db.py L82-83/119/164-165 写入; pipeline.py 零命中(不做门禁);
  仅 push_trigger.py L635-640 读来拼推送文案。

**④ 契约未变**
- adapter 仍写 LLM entry/SL/TP 数字(L239-241 未改)→ 阶段2(停止向 LLM 要价格、
  改调代码引擎)未落地, 仍为草案 `docs/specs/da-prompt-contract-draft-20260710.md`。

## 三、结论
- **链路结构未变**: DA(LLM 出数)→ adapter(抄数)→ DB → generate/reprice(代码 ATR)
  → 下单 → 机械退出。
- **交易动作未变**: 新增两模块未接线; 唯一新消费 `invalidation` 影子评估为 record-only。
- **会改动作的部分全部未实施**(阶段2/3 契约切换、B2/B3 thesis_recheck 接线、
  gate 改读 entry_low/high、Q2 波动率化), 均锁在 sim 门后。
- 唯一新增链路支线 = invalidation 影子记录(2026-07-11 Stage2a), 为评估 thesis 早退
  价值攒数据的骨架, 当前对 pnl 无影响。

## 四、L1 SL/TP 验证影子 (2026-07-12 落地, record-only)

> 有界自治封套的第一步: 在 reprice 既成的"代码覆盖 LLM SL/TP"点补记录分歧,
> 供日后与 pnl 联合校准。**零 live 交易行为变更**。

**埋点 (自动)**
- `app/playbook/sltp_verify_shadow.py`: `record_sltp_shadow(...)` 纯记录, 自带 try/except 永不抛。
- 挂钩点: pipeline.py reprice 段 `_reprice_params` 定稿 + DB 更新之后 (只读已决策值)。
- flag `sltp_verify_shadow=True`(默认 on)。落盘 `data/shadow/sltp_verify_shadow.jsonl`。
- **运行载体**: `main.py --start` 常驻交易服务 (含 reprice), 明天开盘触发即自动记录, 无需手动启动。

**分析 + 推送 (自动)**
- 报告: `scripts/sltp_shadow_pnl_report.py`(只读), join `inst_id → playbook_instructions.pnl_pct`。
- 核心问题: 代码偏离 LLM 的幅度与 pnl 的**相关**(非因果; 所有成交实际都用代码 SL/TP)。
- **cron**: `0 6 * * 2-6`(北京 06:00, 美股收盘后 2h, 周一~周五收盘对应周二~周六)
  → `--push` 推飞书 + 写 `logs/sltp_shadow_report.log`。
- 推送防噪: 每日一条短状态; **分歧样本≥20 才提示"可评估是否推进 L2"**, 否则静默累积。

**边界**: L1 仅埋点观测。L2(真让 LLM/代码按封套分工改 SL/TP)需 shadow 数据 → sim 验证;
L3(DA 契约加 regime/needs_wide_stop, 改工作区外 skill)难回退; 均未实施。

## 相关文件
- 阶段2 契约草案(已被有界自治方案取代): `docs/specs/da-prompt-contract-draft-20260710.md`
- spec: `LLM判断代码算数职责分离`(plan)
- 离线对齐报告: `scripts/track_a_offline_alignment.py`
- L1 埋点: `app/playbook/sltp_verify_shadow.py` / 测试 `tests/test_sltp_verify_shadow.py`
- L1 联合分析(cron+推送): `scripts/sltp_shadow_pnl_report.py`
