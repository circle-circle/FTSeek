# DEB-20261111 — 全链路信息 / 数据 / 参数准确度巡检

- 状态：**Batch A 零交易行为项已部署；行为相邻项仍待裁 / 须 RFC**
- 时点：2026-11-11 17:40 CST（2026-11-11 04:40 EST）；DA 最新完整批次=2026-11-10，EXT 当前盘前=2026-11-11。
- 范围：配置参数真值、DA 输入/输出、EXT 盘前盘后、账本/出场、报表/影子治理。
- 纪律：本轮只读；不改交易规则、不新增 cron/推送、不手工改 DB、不调用券商下单。测量缺口不等于 edge 改善。

## 0. 一句话结论

当前无新 P0 交易链路故障：四锁 sim、服务 active、DB integrity ok、7 仓全部 synced、exit_intents 354 全 BOOKED。
准确度缺口集中在 **DA as-of/UE prompt 契约、TCA 滑点覆盖、EXT research market_cap 缺失、BE 阈值缺键、历史账本卫生、影子挂载**。
这些缺口主要影响“读数可信度/供给质量”，不是当前持仓出场安全。

## 1. 已确认正常项

| 面 | 证据 | 判定 |
|---|---|---|
| 四锁 | `TRADE_ENV=1` / `FUTU_TRADE_ENV=1` / `AUTO_TRADE_ACCOUNT=sim` / `ALLOW_REAL_TRADING=false` / `playbook.execute_mode=futu_simulate`；启动冒烟 7/7 | OK |
| DB 完整性 | `PRAGMA integrity_check=ok`；`foreign_key_check=[]` | OK |
| 持仓账本 | open_positions 7 = open trade_records 7；AMZN/BA/GOOG/HON/NET/PDD/UBER 全 synced；AMZN/NET initial_risk 按 initial_sl 复算 244.08/122.88 | OK |
| 资金状态 | capital_state cash 73727.782 + market_value 26042.74 = current_equity 99770.522；last_update 当前 | OK |
| DA 11-10 | 33 信号 BLOCK12/WARN19/PASS2；21 指令（17 可执行/4 SKIP）；14 严格成交；15 executed 闩（GOOG=already_held）；planned 字段 21/21；effective RR 几何 0  mismatch；BOOKED broker_env=sim | OK |
| EXT 11 月 | 30 笔（28 已平+2 未平）；A25/B5；strict5/research25；research_beat13/research_watchlist12；无 TP；earnings_period_age 无 >150；实际名义≤目标票面（整数股截断） | OK |
| 出场台账 | playbook_exits.db exit_intents 354 全 BOOKED；exit_legs 559；EXT exit cron orphan/closekey/rth_zero_fill ok | OK |
| 注册表 | REGISTRY 106；ext_entry_risk_shadow 已注册；RFC-160 eod_mfe_gate_shadow=KILLED；check_intent_uniqueness=[]；check_artifact_consumers=[] | OK |
| 报表契约 | session_attribution J 严格 decision_id；K 覆盖≠实时；G initial_risk 用 initial_sl | OK |

## 2. 问题清单

### P1 · 读数可信度 / 供给质量

1. **DA 输入覆盖≠实时，as-of 契约缺口大**
   - 证据：11-10 raw 33；quality 31×19/19 + 2×18/19；`_source_status` 只覆盖 news/news_v2/research/sentiment；VIXY 33/33 `prev_close`；snapshot 31 premarket / 2 missing；kline 最后 bar 全为 2026-11-09。
   - 顶层 as-of 缺失：analyst_consensus/cap_anomaly/capital_distribution/capital_flow/company_profile/deriv_anomaly/earnings_calendar/fundamentals/industry_peers/insider_sentiment/insider_transactions/market_context/tech_anomaly 均 33/33；research 3/33、sentiment 5/33、snapshot 2/33。
   - 影响：无法证明每维数据在决策时点新鲜；prompt 可能消费陈旧上下文。
   - 建议：落 per-source `as_of/session/age/fallback/degraded` manifest；日报显式 stale；不新增硬闸。

2. **UE→DA prompt 级上下文/borrow basis 覆盖异常**
   - 证据：`ue_da_chain_health --limit 30`：prompt_ue_context_coverage=0、prompt_ue_short_check_coverage=0、prompt_ue_event_reaction_coverage=0、borrow_basis MISSING 30、evidence_gaps 10；analysis 侧有 `_ue_*` 字段，但 raw user_prompt 未命中健康脚本的 UE 标记。
   - 影响：可能是 UE 注入缺失，也可能是探针键名漂移；空头 borrow basis 不可审计。
   - 建议：先核对注入契约 vs 探针标记；补 borrow_basis/event_reaction 落盘；不接 quality_score 门。

3. **TCA 滑点 / entry_price_source 覆盖不足**
   - 证据：11 月 A9/A10 已平 n91：`execution.slippage_bps` 31/91（DA 31/63，EXT 0/28）；`entry_price_source` 63/91（DA 63/63，EXT 0/28）。不变量 sample73：slippage 21/73、entry_price_source 49/73。
   - 影响：EXT 全量、DA 部分无法做决策价↔成交价滑点归因；低于 KR5 费用/滑点覆盖≥95% 目标。
   - 建议：EXT 成交写 broker deal 滑点/价格来源；DA 缺口可回补则回补；报表单列覆盖率。

4. **EXT research 臂 market_cap_missing 拦截异常**
   - 证据：11-11 盘前 research funnel selected67/reject462；market_cap_missing29，含 MRVL/SMCI/DUOL/SNDK/P/AVGO/QQQ/ARM/ORCL/META/INTC；research_volume312、research_chg_band77、turnover_too_low26。
   - 影响：大市值主流标的因市值缺失被拒，研究源供给被数据质量扭曲。
   - 建议：审计 market cap 来源（Futu snapshot vs Finnhub profile）与 fallback as-of；先修数据，不盲目放宽闸。

5. **`breakeven_at_pct` 运行时缺键 / 默认值口径分叉**
   - 证据：`get_playbook_config()` 无 `breakeven_at_pct`；exit_engine 多处默认 0.5，era 遥测默认 0.4；YAML 注释多处写 0.4；fee_cushion0.15 + slip0.2 = 0.35。
   - 影响：BE 激活阈值依赖代码默认值，文档/遥测/执行可能不同口径。
   - 建议：显式落 YAML 键 + 契约测试统一默认；若改阈值属行为变更，须 RFC/影子。

6. **MFE 重算 / chg 分母存量分叉**
   - 证据：invariant 11-10：I4_mfe_recalc×14（例 COIN id1770 mfe0.269 recalc0.000）；W7_chg_denominator_divergence×40，baseline divergent130。
   - 影响：受影响行的 MFE/MAE 与 change_percent 消费者读数不可信。
   - 建议：stored vs recalculated 分层；统一 prev_close 分母 SSOT；改基线须 DEB，不静默。

### P2 · 卫生 / 治理 / 语义

7. **open_positions.stop_loss_price 契约漂移**：GOOG/HON `stop_loss_price=NULL`，但 initial_sl/current_sl 有值；EXT 写 stop_loss_price。出场链读 trade_records，当前无交易影响；建议统一或标 deprecated。
8. **历史账本卫生**：已平缺 pnl/fees 5（9/16 MRCY/SNDK，9/17 GIPR/NBIS/AEHL）；decision_id 缺失 293（主要 legacy/scalping）；重复 decision_id 6（5 月 DEC）。当前 DA/EXT 严格 cohort 干净；建议隔离/标注 legacy，不静默改写历史。
9. **earnings_calendar 表空 + 静态 fallback**：DB earnings_calendar rows=0；EXT 日志出现“使用静态 KEY_US_SYMBOLS fallback”；候选日历 107/98 只。建议记录 source/as_of，修复写入方或标 dead。
10. **影子挂载 / KR4 盘点**：check_mounting 报 budget_p3_sizing、confidence_tier_gate 悬空；OBSERVING=53，需区分 permanent measure 与实验并收缩到 KR4≤5 实验。
11. **配置语义/元数据 stale**：config.updated=2026-04-29；COST_MODEL.commission_per_share=0.0 与执行 fee_model（0.01/股、1.99 低消）不同源；playbook.fee_gate.enabled=true（主管线）vs admission.fee_gate_enforce=false（Playbook）。建议标清 SSOT/消费方，更新元数据。
12. **EXT 候选噪音**：盘前 snapshot dropped OTC/未知（AMNF/CCRN/NLST/PRKR/TOF/UNCRY/IFNNY）；price_gate84 多为 $0。建议上游过滤 OTC/未知并留痕。

## 3. 优化队列（一次一变量）

### Batch A · 测量/契约，零交易行为

1. DA per-source as-of manifest + stale 披露；UE prompt 契约/探针核对。
2. TCA 覆盖：EXT slippage_bps/entry_price_source 写方；DA 缺口回补；覆盖率报表。
3. EXT market_cap 来源审计 + fallback provenance；OTC/未知候选上游过滤。
4. `breakeven_at_pct` 显式 SSOT + 默认值契约测试（先文档/契约，改阈值另裁）。
5. open_positions.stop_loss_price 统一/deprecated；legacy 账本隔离标注。
6. 影子挂载修复 + KR4 permanent measure/实验分类。

### Batch B · 行为变更，须 RFC + 影子 + 用户裁

- BE 阈值实际变更、EXT TP/cert_lock/风险平价/跨臂约束、market_cap fallback 若改变下单分布、任何闸放宽。
- 不关 chase cap、不放宽 SL/BE/TP、不抬票面、不新增 EXT 亏损止损、不复活 UE inject/RFC-160。

## 4. 可复现检查

```bash
PYTHONPATH=. ./venv/bin/python scripts/da_chain_health_report.py --date 2026-11-10 --days 1
PYTHONPATH=. ./venv/bin/python scripts/ue_da_chain_health.py --json --limit 30
PYTHONPATH=. ./venv/bin/python scripts/shadow_field_cardinality.py --days 30 --json
PYTHONPATH=. ./venv/bin/python scripts/session_attribution.py --date 2026-11-10 --json
PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py
```

产物：`/tmp/ftseek_da_chain_health_20261110.txt`、`/tmp/ftseek_ue_da_chain_health.json`、`/tmp/ftseek_field_cardinality.json`、`/tmp/ftseek_session_attribution_20261110_batch0.json`。

## 5. 验证与未验证

- 已验证：只读 SQL/JSONL/YAML/env/registry/启动冒烟；DA 11-10、EXT 11-11 盘前、11 月 A9/A10 cohort。
- 未验证：未改代码/配置/DB；未跑全仓测试；未调用券商下单；UE prompt 缺口是注入缺失还是探针漂移未定论；market_cap_missing 根因未修；未来 RFC-149/RFC-155 样本未产生。

## 6. 2026-11-11 17:59 Batch A 实施回执（用户裁「按照建议执行」）

- 已落地（零交易行为）：
  1. **EXT TCA 写方**：`_write_open_position` 成交后落 `execution.slippage_bps / arrival_price / fill_price / entry_price_source`，正=不利，LONG/SHORT 同 Playbook 口径；只增 metadata，不改下单/出场。
  2. **BE 阈值显式 SSOT**：`playbook.execution_parameters.breakeven_at_pct: 0.5`，固化当前运行时默认值（非阈值变更）；契约测试断言 fee+slip < arm。
  3. **DA 输入新鲜度清单**：新增 `scripts/da_input_freshness_manifest.py`，per-source as-of/age/fallback/degraded 只读披露；11-10 raw33、parse_error0。
  4. **UE/DA 契约诊断**：`ue_da_chain_health` 增 analysis 侧 UE 覆盖；最新 30 条 prompt UE=0、analysis UE=1、gap=1 ⇒ 更像注入稀疏，不只是探针漂移。
  5. **session_attribution L 段**：TCA 覆盖 / stop_loss_price 缺值 / legacy 卫生只读披露；11 月 A9/A10 n91 slip31/source63，EXT slip0/source0。
  6. **影子挂载收口**：`budget_p3_sizing` / `confidence_tier_gate` 改判 `eval_on_demand=True`；`check_mounting=[]`、intent/artifact consumers 清零。
- 部署：`systemctl restart ftseek-us` + 启动冒烟 **7/7**；PID 2589190；7 仓不变且全 synced；PositionSync 一致性 ✓。
- 验证：合并聚焦子集 **112 passed**；`py_compile` 通过；`git diff --check` 通过。产物：`/tmp/ftseek_da_freshness_manifest_20261110.json`、`/tmp/ftseek_ue_da_chain_health_batcha.json`、`/tmp/ftseek_session_attribution_20261110_batcha.json`。

```bash
PYTHONPATH=. ./venv/bin/python -m pytest -q \
  tests/test_position_sync_pending_status.py \
  tests/test_ext_entry_risk_shadow_registry.py \
  tests/test_session_attribution_chain_contracts.py \
  tests/test_session_attribution.py \
  tests/test_playbook_price_ledger.py \
  tests/test_shadow_registry_criteria_provenance.py \
  tests/test_shadow_intent_uniqueness.py \
  tests/coordinator/test_position_sync_p0_fixes.py \
  tests/test_position_sync_backfill_clock.py \
  tests/test_pending_sync_staleness.py \
  tests/test_last_sync_at_writer_scale.py \
  tests/test_rfc195_open_position_stop_align.py \
  tests/test_open_position_sl_tp_contract.py \
  tests/test_ext_entry_risk_shadow.py \
  tests/test_ext_entry_tca_contract.py \
  tests/test_breakeven_ssot_contract.py \
  tests/test_da_input_freshness_manifest.py \
  tests/test_ue_da_chain_health_analysis_coverage.py \
  tests/test_shadow_mounting_batch_a.py
```

- 未做 / 未验证：DA prompt 生成端 as-of 写入未改（本轮只读清单）；UE 注入缺失未修（涉及 DA skill/prompt，须另裁）；EXT market_cap fallback / OTC 上游过滤未改（可能改变供给分布，属行为相邻）；历史 DB 未回补；`stop_loss_price` 未统一写方；未新增 cron/推送；未跑全仓测试。
- 下一步：EXT TCA 覆盖只会在后续成交累积；market_cap 根因先只读审计；UE 注入契约另立 DEB；11-13 RFC-149、11-27 RFC-155 死线不变。

## 7. 2026-11-11 18:24 UE prompt 缺口补注（不静默改写历史）

§6 第 4 条「prompt UE=0 / analysis UE=1 ⇒ 更像注入稀疏」后续被更细口径修正：旧探针分母只取可执行指令，漏掉 UE 独有但最终 SKIP 的候选；重叠票 `ue_*` 是溯源回填，按设计不注入 prompt。新审计见 `DEB-20261111-ue-role-contribution-audit.md`：11 月 UE 独有分析 18 个，prompt-eligible 18，raw prompt 送达 18/18，缺失 0。当前不修重叠票 prompt 注入；若改变该门控，属行为相邻变更，须另裁。
