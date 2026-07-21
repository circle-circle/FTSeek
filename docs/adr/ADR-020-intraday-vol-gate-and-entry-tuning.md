# ADR-020: 盘中量能门启用 + 入场侧参数调优 (config 驱动, 富途模拟盘)

- 状态: Accepted
- 日期: 2026-07-21
- 关联: ADR-016 (universe-scan V2 prompt), DEB-20260721-fullstack-consistency-audit (一致性审计), GHOST-RC1 (入场确认时序修复)

## 背景

`config/unified_config.yaml` 在往期会话中积累了一批**入场侧**的 feature-enable 与参数调优, 因治理钩子 (SYSTEM_OBJECTIVE.md §5: 启用 disabled feature 需 ADR) 拦截而一直悬于工作区未提交。本 ADR 一次性补齐审批依据, 使其可正常入库。全部改动仅作用于**富途模拟盘** (`execute_mode: futu_simulate`), 无真实资金风险, 且均可一键回退。

本轮一致性审计 (DEB-20260721) 已将纯正确性修复 (路径锁现值 / 熔断 fail 语义 / 死配置清理) 拆分为独立提交 (`d6f6d083`); 本 ADR 覆盖**剩余的行为性 feature-enable**, 与审计修复解耦。

## 决策

启用以下 config 项 (均为 config 驱动, 不碰代码逻辑):

### 1. 盘中量能门 `intraday_vol_gate_enabled: true` (触发钩子的项)

- **语义**: `today_cum_vol / (avg_daily_vol_5d × elapsed_fraction) ≥ chase_vol_ratio_threshold` 才允许"追入"; 否则拦截 (防缩量追高)。
- **配套参数**:
  - `chase_vol_ratio_threshold: 0.8` (二次校准值, 原 1.5)
  - `intraday_vol_gate_grace_min: 15` (盘初 15min 豁免, 数据最不可靠)
- **根因与校准依据**: 分子原聚合 `K_1M`(300s 缓存) 系统性滞后低估 → 门槛 1.5 令几乎全部追入被 BLOCK。修复分子改用 Futu LV3 实时快照当日累计量 (`get_quote()["volume"]`, <100ms, 无缓存滞后)。重启后实测午盘 18 只标的 ratio 分布 0.51~1.34 (中位数 ~0.73), 因真实成交量呈 U 型 (开盘前置), 午盘"正常节奏"天然落在 0.7~0.9; 1.5 等于要求异常放量才准追入。校准至 0.8 = "不低于午盘正常节奏", 实测仍可拦 ~2/3 缩量追高, 同时放行正常/放量标的, 恢复样本积累。
- **fail-open**: 数据缺失/豁免窗内不拦 (本轮审计 GATE-6 已补失明告警可观测)。

### 2. 入场成交确认窗 `entry_confirm_max_wait_s: 30` (GHOST-RC1 时序修复)

- 原 5s → 30s, 对齐平仓侧 30s 轮询先例。
- **根因**: SUBMITTED 是等待态而非失败态; 实证 EOG(7/20)/TTE/UBER(7/21) 均在拒记后 9~29s 内成交 → 5s 窗误判"未成交" → 真仓无账走 position_sync 降级补录 (假归因 50/0.5)。仅 `fill=0` 且快查无成交价的模糊单进等待, 正常成交不阻塞。

### 3. 早期紧 SL `early_tight_sl_enabled: false` (shadow-only, 保持关闭)

- 明确记录**保持 disabled** (不启用实单)。参数 `early_tight_sl_minutes: 30` / `early_tight_sl_pct: 1.0` 保留供 exit_shadow `EARLY_TIGHT_VARIANTS` (early_tight_30m_1pct) 反事实采集口径对齐。
- 积累期实单退出参数冻结, 避免引入 config_version 断点污染毕业统计; 待 2 周 shadow 数据 `counterfactual_saved_pct>0` 且稳定后再评估实单化。

### 4. Universe 浮现门 `universe_expansion.min_confidence: 0.70 → 0.50`

- **根因 (死锁修复)**: ADR-016 的 V2 prompt Track A 夹逼强制无证据候选 `conf≤0.60`, 与旧门槛 0.70 叠加 = 被夹逼候选 100% 进不来 (实测 4 天 28 浮现仅 2 注入, 链路近乎白跑)。
- 0.50 让 `MARKET_CORROBORATED` 类进入; `self_tier` 标记保留, 评测按 tier 切片。

## 影响范围

- **仅富途模拟盘** (`force_simulate` / `futu_simulate`); 无真实资金。
- 盘中量能门 (#1) 与 universe 浮现门 (#4) 改变**入场标的集合**; entry_confirm (#2) 改变入场确认记账准确性 (减少假归因); early_tight (#3) **无行为变更** (保持关闭)。
- 不碰退出链路本身、不碰下单执行逻辑。
- 全部可回退 (见下)。

## 验证

- `python -c "import yaml; yaml.safe_load(...)"` YAML 解析通过。
- 盘中量能门: 日志打印 `[playbook] 盘中量能门: {symbol} intraday_vol_ratio=... → PASS/BLOCK`; 数据缺失时 (本轮审计 GATE-6) 打印 `量能数据缺失 (fail-open 放行)` 可 grep 计数。
- entry_confirm: 观察 SUBMITTED→FILLED 的 9~29s 单不再走 position_sync 降级补录 (假归因 50/0.5 消失)。
- universe 浮现: `cmd_scan` 后注入率从 ~7% (2/28) 回升。

## 回退

编辑 `config/unified_config.yaml` 后重启服务:
- `intraday_vol_gate_enabled: false` (整门关闭) 或 `chase_vol_ratio_threshold: 1.5` (回原阈值)
- `entry_confirm_max_wait_s: 5`
- `universe_expansion.min_confidence: 0.70`
- `early_tight_sl_enabled` 保持 false (本就未启用)
