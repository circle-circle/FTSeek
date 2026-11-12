# RFC-201 · 2026-11-12 独立核查后的五项修正

**触发**: 用户 2026-11-12「另外 agent 执行了落地调整优化，请发挥你的能力核查」→ 核查报出
3 个问题 + 2 项技术债，用户裁「按建议优化」。

**基线**: `3743d6430`（并行 agent 的 `10694b619` 之后）。

## 修了什么

### ① 暂时态不再写终态字段 `skip_reason`（交易安全）
- 位置: `app/playbook/pipeline_parts/auto_execute_flow.py::_record_mom_defer`
- 改前: MOM 报价/日历/预算/入口 4 个**暂时性** defer 会写 `skip_reason`；该列只在
  `executed=1` 时被清空（`db.update_instruction_tracking` 的单点不变式），所以当天不成交
  就永久留痕。
- 后果（改前）: 下游 3 处把"非空 skip_reason"直接当"已跳过" ——
  `premarket_invalidation.py:108`（不再评估作废）/ `shadow_tracker.py:350,362`（不取样）/
  `ue_batch.py:86`（不参与打分）。与并行批自己修掉的 `MOM_FEE_SIZE_INFEASIBLE` 终态跳过同源。
- 改后: defer 只经 `exec_gate_audit`（`transient=True`）+ 日志留痕，零 DB 写。
- 回滚: 恢复 `_record_mom_defer` 里那一行 `update_instruction_tracking`。

### ② `RebalanceDayStatus.__bool__`（加固，1 行）
- `frozen dataclass` 恒 truthy：任何 `if rebalance_day_status(...)` 误写都会让调仓闸**每天开**。
  同一提交里的 `AMBIGUOUS` 哨兵已显式 `__bool__`，本类型补齐，两种新类型防护对称。
- 现有调用方均用 `.is_rebalance_day`（已 grep 全仓），故零行为变更。

### ③ 归因来源分层（报表可审性）
- `instruction_attribution.resolve_trade_instruction_ids(..., sources=None)`：回填
  `metadata` / `metadata_stale` / `decision_id` / `ambiguous` / `unmatched`。
  `metadata_stale` = metadata 记过指令 id 但该指令已不在表内 → **静默降级到模糊匹配**
  （最容易被误读成"精确"的形态）。
- 接入: `bundle_builder`（3 个 metric + render 一行）/ `daily_review`（`attribution_source_counts`）
  / `plan_effectiveness`（`attribution_source_counts`）。
- 实测（2026-11-09/10/11 三个交易日）: 精确 8/14/8、回退 0、陈旧 0 ⇒ **当前执行率里没有猜测值**。
- 回滚: `sources` 为可选参数，不传即退回旧行为（已有用例钉住）。

### ④ 三处止损表示不再分叉（展示列）
- `open_positions.stop_loss_price` 是下单时写入、无人维护的**展示列**（dashboard /
  battle_plan 的"已跌破止损"告警来源）。NVDA 实测仍为 165.43，而权威真值
  （`trade_records.metadata.stop_loss` 与 `open_positions.current_sl`）已是 156.00。
- `scripts/manual_nvda_sl_override_20261111.py`：
  ① apply/revert 现在三处同写；② 新增 `--sync-display`（只对齐展示列，不动真值）；
  ③ 幂等保护（重跑不再把 `stop_loss_prev` 覆盖成当前值 → 原始 165.43 会永久丢）；
  ④ `--revert` 改用**最早**一份备份（取最新会变成"回滚了展示修复但仍保留止损改动"）；
  ⑤ 备份改整行快照（`SELECT *`），故 revert 能还原 `stop_loss_price`。
- 已执行 `--sync-display`：165.43 → 156.0，再跑一次验证幂等。

### ⑤ live probe 5 例"过期契约测试"修复（技术债）
- 根因实测: 生产自 **2026-11-06** 起 `fee_gate_enforce=false`（用户裁关闭费用门拦截），
  `NOTIONAL_TOO_SMALL` / `FEE_BPS_TOO_HIGH` 不再 reject，只进 `metrics.would_reject`
  ⇒ 探针豁免场景**无拦可豁**，`exempted_reasons` 恒空。红的是场景，不是豁免机制。
  （已确认与并行批无关：`e1fa4d881` worktree 同样 5 红。）
- 改法: fixture 在**测试侧**显式打开费用门以复现可拦场景（保持"守豁免机制边界"的原意）；
  新增 `test_production_fee_gate_is_currently_unenforced` 钉住生产现状 —— 若有人把开关改回
  true，该用例变红并提示"删掉覆盖、直接跑生产口径"，而不是放宽断言。

## 验证

- `pytest tests/playbook/ tests/review/ tests/test_auto_execute_open_floor.py
  tests/test_instruction_attribution.py tests/test_plan_effectiveness_abandoned.py
  tests/test_except_pass_ratchet.py` → **1399 passed / 1 skipped / 3 errors**
  （3 errors = `test_exit_compensation` 网络边界，预存，`e1fa4d881` worktree 同红）。
- `test_live_probe_admission_exemption.py` → 10 passed（改前 5 failed）。
- 端到端渲染: `scripts/review_bundle.py --date 2026-11-11` 产出
  `- 归因来源: 精确 metadata 8 / decision_id 回退 0 / 陈旧 metadata 0`。
- DB 终态: `metadata.stop_loss=156.0 / prev=165.43 / current_sl=156.0 / initial_sl=156.0 /
  stop_loss_price=156.0`（三表示一致）。
- 服务重启 + 启动后 ERROR 计数（见提交信息）。

## 未验证 / 不做

- 未跑全仓（8805 例）；只跑受影响面。
- MOM 真实链路（今晚 09:50 ET 首次调仓）未验证 —— 本批只去掉了"暂时态写终态字段"。
- 未动任何策略阈值 / TP / SL / 持有期 / 费用门开关本身。