# RFC-148 UE 09:10 fresh-event 同标的覆盖风险与最小修复方案

**状态**: 已裁决并落地 (2026-10-18 用户裁: 停用当日写库; 方案 A/B 不再实施; 只读指标保留)
**触发**: 2026-10-18 用户问 “UE 9:10 的扫描经过 DA 后期产出是否能统计到最终指令”; 复核发现可统计，但 wrapper 的同标的排除没有真正作用到 DA pipeline，存在 UPSERT 覆盖风险。

## 0. 裁决 (2026-10-18)

用户裁「逻辑以 DA 为主，避免重复和冲突」→ **停用 09:10 fresh-event 当日写库**:

- crontab 摘除两行末尾的 `&& da_fresh_event_batch.py --execute`；UE 09:10 scan 保留
  (观察 / confirm / 次日供料)。
- DA bridge 推送不再传 `--wait-fresh-event`；09:12 直接推 DA 主链批次。
- `scripts/da_fresh_event_batch.py` 与 `origin=ue_fresh_event` 腿保留供人工影子/回滚，
  但生产不再新增；`app/review/source_attribution.py` 该腿仅历史归因。
- 覆盖风险从生产链路消失；§4 的方案 A/B 不再实施，保留为历史备选。
- 代价: 08:00 后新出的 8-K 不再当日变成 DA 指令，转次日或人工处理。

## 1. 现状链路与可统计字段

链路:

```
UE 09:10 premarket scan
  -> da_fresh_event_batch (select_fresh_candidates + same_day_instruction_symbols)
  -> external DA pipeline --fresh-event (ue_only=True, window="premarket")
  -> bridge -> playbook_instructions
```

最终指令的 `raw_instruction_json` 已保留来源:

- `origin`: `ue_fresh_event` = 09:10 fresh-event 批; `ue_candidate` = 08:00 da_feed 并入 DA 主链; 空 = 纯 DA/app。
- `ue_batch_id`: 对应 UE 扫描批次。
- `ue_direction` / `ue_confidence` / `ue_scanned_at`: UE 原始方向与置信度。
- `ue_causal_chain` / `ue_evidence` / `ue_counter_evidence`: UE 论据。

已加入只读指标 `scripts/empty_run_metrics.py` 的 `[UE 09:10 → DA → 最终指令]` 段:
surfaced / wrapper 选中 / same_day 排除 / borrow 丢弃 / 最终指令 / actionable / triggered / executed /
方向保持或翻转 / 覆盖 / 无最终指令。

## 2. 实测 (2026-10-16)

- UE surfaced: TSM SHORT, ERAS SHORT, AN SHORT。
- wrapper 选中: ERAS, AN; `same_day_db_excluded=["TSM"]`。
- DA pipeline 实际: ERAS/AN `borrow_feasible=False` 被跳过; **重新注入并分析了 TSM**。
- 最终指令: TSM BUY, `origin=ue_fresh_event`, `ue_direction=SHORT`, `skip_reason=未触发`。
  `created_at` (08:46 ET) 早于本批 `scanned_at` (09:12 ET) ⇒ 本批 UPSERT **覆盖了 DA 主链的既存 TSM 行**。
- 结论: 09:10 批当天**新增最终标的 0**; 1 条最终指令是覆盖/翻转; 2 条被借券挡掉。

2026-10-15 同样出现: wrapper 排除 AMD/WDC，但 pipeline 实际分析并覆盖 AMD（最终 AMD BUY，UE 原 SHORT）。
说明这不是单例，而是 wrapper 的选择与 pipeline 的输入源不一致。

## 3. 根因

`da_fresh_event_batch.build_command()` 只传 `--fresh-event` / `--fresh-context-dir` / `--output-dir` /
`--no-pool-write`，**没有把 wrapper 过滤后的候选传给 pipeline**。
`pipeline.py --fresh-event` 自己又从 `llm_universe_scan.jsonl` 读取 `window="premarket"` 的最新批次，
因此 wrapper 的 `same_day_instruction_symbols` 排除对 pipeline 无效；同标的仍在 pipeline 内被注入，
Stage 6b 按 `date+symbol` UPSERT 覆盖既存指令行。

## 4. 最小修复方案 (建议 P0)

**方案 A (最小、推荐先做): 把同标的排除列表显式传给 pipeline。**

1. 外部 skill `pipeline.py`:
   - 新增 CLI `--exclude-symbols SYM1,SYM2` (默认空 = 当前行为)。
   - `--fresh-event` 模式下，把该集合传入 `_load_ue_short_candidates()`；
     在遍历 surfaced 时，若 `bare(symbol)` 在集合内则跳过。
   - 保留原有 borrow/streak/方向/资格过滤，不改变其他链路。
2. FTSeek wrapper `scripts/da_fresh_event_batch.py`:
   - `build_command()` 增加 `--exclude-symbols`，值来自已计算出的
     `same_day_instruction_symbols` (保持 `same_day_db_excluded` 现有语义)。
   - execute/shadow 两种模式都记录该参数到事件日志，便于审计。
3. 先在 `--no-db-write` shadow 模式下跑 10–20 个交易日:
   - 对比 “排除前/后” DA 实际分析集合、最终指令集合、方向翻转、覆盖数。
   - 判据: 覆盖数 = 0; 新增最终标的不得减少; 若减少，逐笔列出被排除的标的与原因。
4. 通过后再开 execute；回滚 = 去掉 `--exclude-symbols` 参数/开关，恢复当前行为。

**方案 B (更彻底，后续): 把 wrapper 过滤后的完整候选文件传给 pipeline。**
- wrapper 把选中的原始 surfaced 行 (保留全部 `ue_*` 字段) 写到隔离目录；
  pipeline 新增 `--fresh-candidates-file`，从该文件读候选，不再自己重读主 scan。
- 优点: wrapper 成为唯一选择源，漏斗的 wrapper 选中 = DA 实际分析。
- 成本: 外部 skill 改动更大，需要版本契约与回归测试；建议在方案 A 稳定后做。

**防御层 (可选 P1): DB 写入守卫。**
- 对 `origin=ue_fresh_event` 的 UPSERT，若同日同标的存在 `triggered=1` 或 `executed=1` 的指令行，
  拒绝覆盖并写审计告警。这是兜底，不替代方案 A。

## 5. 不做

- 不改 UE 09:10 扫描本身、不改 DA 方向/阈值/评分、不改仓位/止损/出场。
- 不直接把方案 A 上生产；先 shadow，按仓规一次一变量。

## 6. 验收指标 (只读)

`python scripts/empty_run_metrics.py --days 10` 的 `[UE 09:10 → DA → 最终指令]`:
- `overwrite` 应从 1/日降为 0。
- `wrapper选中` 与 `最终指令` 的差异应可解释 (borrow/da_skip)。
- 每个批次的 `actionable/triggered/executed` 与 `no_final_symbols` 应保持或改善。
