---
title: universe_expansion 改走 playbook 统一执行轨道
status: active
module: universe_expansion
related_code:
  - scripts/llm_universe_scan.py
  - app/pipeline/stages/signal_stage.py
  - app/playbook/pipeline.py
related_doc: docs/design/universe-expansion-eval.md
tags: [universe_expansion, playbook, execution, attribution]
updated: 2026-07-14
---

# ADR-009: universe_expansion 改走 playbook 统一执行轨道

**日期**: 2026-07-14
**状态**: 已实施
**决策者**: 用户 + Hermes

## 背景

universe_expansion（LLM 事件推理选股宇宙扩张）最初把浮现标的注入 `daily_battle_plan_us.json` targets，
由 `fast_lane` 在盘中扫描循环里执行。实证发现该链路对**宇宙外标的失效**：

- 盘中扫描只处理 `polling_controller.get_polling_batch()`，而 batch 仅含**启动时注册的 watchlist（32 只）**。
- futu LV3 也只订阅这 32 只 core watchlist。
- universe_expansion 专挑宇宙外标的（EOG/APLD/CHWY/INTC）→ 既不在 polling 注册表、也不在订阅 →
  永不进 batch → `fast_lane` 永不评估 → **永不成交**（仅 watchlist 内标的如 ORCL 例外）。

根因：universe_expansion 接到了 **watchlist 绑定的 fast_lane 扫描轨道**；而 DA 走的是
**symbol-agnostic 的 playbook 指令自动执行轨道**（`_check_intraday_trigger` 按标的 `get_full_market_context`/
`get_quote` 实时取价，不依赖订阅/轮询），天然支持任意标的。

## 决策

**信号源可多个（DA 深度分析、universe_expansion 都是"信号源 + 指令生产者"），但执行层统一。**

universe_expansion 的 `inject` 从"写 battle_plan targets → fast_lane"改为"**写 `playbook_instructions`**"，
与 DA 共用同一套自动执行 / 触发 / 退出逻辑：

| 维度 | 实现 |
|------|------|
| 写入 | `PlaybookDB.save_instruction()`，字段对齐 DA 直写口径 |
| 方向 | `LONG→BUY` / `SHORT→SELL_SHORT`（playbook 词汇，见 pipeline.py L736） |
| 入场带 | 复用 `entry_band.compute_entry_band`（执行端同源，防 band 漂移） |
| 归因（轻量，免 schema 迁移） | `decision_id=universe_expansion:{date}:{symbol}` + `raw_instruction.source`，报表可切分 DA vs universe_expansion |
| 排重 | 排除今日已在 `playbook_instructions` 的标的 → 同标的 **DA 优先，universe_expansion 让路** |
| 预算优先级 | auto_execute 排序 `DA-` 前缀在前，天然 DA 优先 |
| 生成时间 | 系统 crontab `30 20 * * 1-5`（北京 20:30 = ET 08:30 盘前，DA 后接力 scan→inject） |

fast_lane 注入路径下线：`signal_stage` 中 `discovery_source==LLM_EVENT_SCAN` 归因分支标注为迁移后死路
（保留以防遗留 target / 手动注入，零行为影响）。

## 后果

**正面**：宇宙外标的现可被执行（不受订阅/轮询限制）；退出/风控/预算与 DA 统一；消除双执行链并行冲突。

**约束**：universe_expansion 与 DA 共享 `max_daily_positions` 与统一预算，同标的由排重让路、跨标的由触发时间市场化仲裁（不加代码级抢占，避免过度设计）。

**回滚**：`universe_expansion.entry_enabled=false` → inject 仅 dry-run（总开关）；删除 cron 停止自动生成；代码改动 git 可回退。全程 Paper Trade（`source_type=PLAYBOOK_SIM`），不触及真实资金。

## 验证

e2e 写库 5 条（当日 total 13 = 8 DA + 5 universe_expansion），entry_low/high、方向、confidence(0-100)、
raw_instruction.source 均正确；排重幂等（再跑 inject 全部跳过）；dry-run 预览通过。auto_execute 对宇宙外标的
的实时取价触发成交待开盘实证。
