# ADR-015: 平仓归因修正 — close-side source_type 按持仓 owner 派生

- 状态: Accepted
- 日期: 2026-07-14
- 分支: rfc-060-phase1-hotfixes

## 背景

冻结期系统为 playbook-only：逐笔退出由 `monitor_playbook_sim_exits` 自管，主线两条
逐笔退出路径（`position_monitor.py:1272` dispatcher / `position_exit_monitor.py:799`
scan）均跳过 playbook。

**但仍有一条主线路径能触碰 playbook 仓**：组合级熔断 `_check_portfolio_drawdown`
（`position_exit_monitor.py:703`）遍历的 `managed_positions`（L681-697）只过滤
ghost/pending/manual/protected，**未过滤 playbook**。playbook 仓 `source=auto_main→managed`
会被纳入，一旦 `FLAT_ALL`/`REDUCE_50` 触发即经主线路径 `_close_position` 平掉 playbook 仓。

问题在于 `_close_position` 的 `save_trade` record dict **漏传 `source_type`** →
`trade_store` 默认 `MAIN_PIPELINE`（`trade_store.py:726`）→ 平掉的 playbook 仓被写成
`source_type=MAIN_PIPELINE` 但 `owner_strategy=playbook_sim` 的记录：
- 钉钉/飞书按 source_type 误标"主线策略 (LLM投票)"。
- 与真正的 PLAYBOOK_SIM 记录并存 → 归因双计数。

实证：2026-07-14 CVE(id 1058)/EOG(id 1059) 即此产物（已由
`scripts/dedup_playbook_main_dupes.py` 去重）。

## 决策

**保留组合级熔断对 playbook 的覆盖**（账户级急停不能丢，且 playbook 自身无组合级熔断），
**只修归因**：close-side `source_type` 按持仓真实 `owner_strategy` 派生。

- 新增 `_OWNER_SOURCE_TYPE` 映射（order_executor.py 模块级常量）：
  `playbook_sim→PLAYBOOK_SIM` / `playbook_live→PLAYBOOK_LIVE` /
  `playbook_shadow→PLAYBOOK_SHADOW` / `orb_engine→ORB_ENGINE`。
- `_close_position` 的 save_trade dict 增加 `"source_type"` 键，用该表按 owner 派生，
  未命中（main/auto_main/legacy/manual）仍落 `MAIN_PIPELINE` — **行为不变**。

因组合熔断 → `execute_close_fn` → `_close_position` 是同一条 save_trade，此一处修正
即覆盖组合熔断路径，无需散点改动。

## 后果

- 正向: 主线路径若平掉 playbook 仓，记录归位为 PLAYBOOK_SIM，通知不再误标、归因不再双计数。
- 中性: 非 playbook 仓（owner=main 等）source_type 不变，主线归因保持原样。
- 需重启生效（纯代码，无 schema 变更）。
- 未改动组合熔断的仓位选择逻辑（用户选择"保留熔断"）— playbook 仍受账户级急停保护。

## 关联

- 去重脚本: `scripts/dedup_playbook_main_dupes.py`（dry-run 优先，--apply 前自动备份 DB）。
- 前置存档: ADR-014（playbook 退出重构 + 影子模块）。
