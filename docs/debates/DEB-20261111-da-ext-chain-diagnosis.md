# DEB-20261111 — DA / EXT 全交易链路只读诊断与优化队列

- 状态：**Batch 0 测量/契约修复已部署；交易规则零变更**
- 时点：2026-11-11 15:46 CST（2026-11-11 02:46 EST）；经济快照截至 2026-11-10 盘后收口。
- 范围：DA 指令输入/输出、Playbook 触发/重定价/成交/出场、EXT 盘前盘后独立入场与退出、账本/归因/影子治理。
- 纪律：本批零代码/配置/DB 写、零重启、零券商调用；只新增本 DEB 与 handoff 条目。测量修复不等于交易 edge 改善。

## 0. 一句话结论

11-10 没有新的 P0 交易链路故障：DA 供给完整、开盘禁入与轮询符合设计、出场 intent 收口、服务 active。
真正优先修的是**测量与契约**：EXT 风险影子未注册、EXT `pending_sync` 状态不收口、DA 输入新鲜度缺少统一 as-of 契约、EXT 无固定 TP 的退出契约不透明。
经济上，DA 的主要出血口仍是开盘 0–15 分钟尾损；EXT 11 月利润几乎全部来自研究源臂，严格臂小样本为负，不能把 EXT 整体当稳定 edge。

## 1. 链路地图

| 层 | 现役路径 | 关键事实 |
|---|---|---|
| DA 调度 | Hermes ET-pinned job `9694df7b6024` → `ops/hermes/da_main_carrier.py` → skill `pipeline.py` | 08:00 ET 发车；11-10 08:01:04→08:26:31，33 raw / 33 analyses / 21 bridge / 21 DB instruction。 |
| DA 生成 | scanner + UE `--da-feed` + watchlist/extra → `_select_deep_candidates` → `collect_all` → LLM → `signal_gate` → `adapter` | BLOCK 不写库；WARN 降 confidence；`raw_instruction.planned_*` 保留计划价。 |
| DA 执行 | Playbook trigger → `auto_execute_flow` → reprice / chase cap / entry confirm / sizing → `PlaybookService.execute_instruction` → DualBroker SIM | 09:01 供给完整则跳过全量 generate（RFC-196）；开盘 5 分钟禁入；成交以 broker deal 为准。 |
| DA 出场 | `monitor_playbook_sim_exits` / exit_engine / durable exit ledger / PositionSync | 11-10 20 个 exit intent 全 BOOKED；GOOG 存量 SL/TP 已 CAS 校正。 |
| EXT 入场 | cron → `extended_hours_entry_executor.py` → Futu movers/财报日历 + Finnhub union + research beat/watchlist → 自有硬闸 → `OpenSecTradeContext.place_order(Session.ETH)` | 不消费 DA 指令，不写 `playbook_instructions`；成交后写 Playbook 账本。 |
| EXT 出场 | `extended_hours_exit_cron_gate.sh` → premarket/afterhours exit shadow → `extended_hours_profit_exit_rules.py` | 只做 TP/BE/trail/cert_lock；`SL_INITIAL` 转 `hold_no_stop`，亏损止损交 RTH。 |
| 主管线 | `signal_pipeline.scan_loop` 保留 housekeeping；新仓冻结 | 不得整体 gate 掉 scan_loop；order_executor 白名单仍守门。 |

## 2. DA 输入 / 输出信息源

### 2.1 输入

- 候选：scanner、UE `scan --da-feed`（只供料，不复活 inject）、watchlist/extra symbols；11-10 selection policy `da_ue_v1`，selected=33。
- 每标的 19 维：Futu snapshot/kline/capital_flow/capital_distribution/tech/cap/deriv anomaly/news/research/sentiment；Finnhub news_v2/fundamentals/analyst_consensus/insider_transactions/insider_sentiment/company_profile/earnings_calendar；market_context（SPY/QQQ/VIXY）与 industry_peers。
- 上下文注入：sector_context、scan_condition、UE 因果链/时效/失效条件、股性卡/成交档案/条件黑名单等。
- 新鲜度实证：33/33 raw 都有 `_quality` 与 `_source_status`；其中 31 份 19/19、2 份 18/19。`_source_status` 只覆盖 news/news_v2/research/sentiment 四类。VIXY 33/33 标 `as_of_basis=prev_close`，不是当前实时 VIX；snapshot session 31 premarket / 2 missing。
- 结论：**19 维覆盖 ≠ 19 维实时可用**。执行端已有 RFC-185 reprice quote age gate；DA 生成端仍缺统一 per-source as-of/age/session/fallback 契约。

### 2.2 输出

- LLM JSON → `signal_gate` PASS/WARN/BLOCK → `adapter.to_voting_result` 合成 confidence/entry/SL/TP → `opportunity_scan_YYYY-MM-DD.json` + `playbook_instructions`。
- 指令级已落：`decision_id`、`raw_instruction.planned_*`、`evidence_gaps`、`recommendation_evidence`、`data_quality`、`decision_conf`、`confidence_semantic`。
- 执行端会重定价：`auto_execute_flow.py:447` 起取实时 bid/ask，重算 entry/SL/TP/size；`metadata_json.reprice_audit` 保留首跳/轮数；`price_ledger.py` 可重建 planned/effective/fill 三层。
- 实证 MPWR：plan 1320/1250/1550 → effective 1366.57/1298.24/1469.06 → fill 1364.73；planned RR 3.2857 → effective RR 1.4999。GOOG 成交 RR 1.484354，固定 TP 被 `min_tp_rr` 吞掉，实际由 trailing/EOD 管理。
- 归因必须用严格 `decision_id` 成交：11-10 DA `executed=1` 闩 15，但真实新成交 14；GOOG 是 `already_held` 防重闩。Obsidian 幻影成交已修，不得回退。

## 3. EXT 盘前盘后链路

- 候选源：Futu movers top50、Futu/Finnhub 财报日历 union、research beat rank / watchlist；strict 臂要求同相位财报 surprise，research 臂独立涨幅带 0.5–10%。
- 硬闸：价格/量/成交额/市值/spread/ask depth/price drift/往返费/VWAP 距离与成熟窗；admission 是 record-only 审计，不是额外硬闸。
- 配额：A $5000 / B $2500；日帽 10、盘前帽 9、在仓帽 5；固定 initial SL -5%，无固定 TP。
- 订单路径：直连 `OpenSecTradeContext.place_order(Session.ETH)`，不经 `PlaybookService` / `DualBrokerAdapter`，但写 `open_positions` + `trade_records(source_type=PLAYBOOK_SIM)`。这是受控 SIM 例外，也是统一风控/对账契约缺口。
- 退出：`decide_original_profit_exit` 复用 Playbook 利润保护；cert_lock 按仓 metadata 覆盖全局 env。AMZN entry_grade=A → cert_lock=0；NET entry_grade=B → cert_lock=3。两者均无固定 TP。
- 跨臂：EXT 不读 DA 方向；`ext_entry_risk_shadow` 只在成交后读当日 DA 卡。11-10 8 笔 EXT，`da_available=3/8=38%`，冲突 0。盘前早段无 DA 是时序事实，不能伪造覆盖。

## 4. 经济快照与分臂

### 4.1 11-10 交易日

- 按出场日：19 平 +$187.19 = 新 DA 11 平 +$32.59 + 新 EXT 6 平 +$68.74 + 旧 CAT/CVS 2 平 +$85.86。
- 当日开仓未平 5：DA PDD/HON/UBER，EXT AMZN/NET；另有旧 BA/GOOG，现存 7 仓。
- DA 新仓已平费毛比 58.3%；单日 n=11，不做策略裁决。

### 4.2 11 月 A9/A10 已平 cohort

| 臂 | n | 净额 | 净 bps/笔 | WR | avgW / avgL | 费/毛 |
|---|---:|---:|---:|---:|---:|---:|
| DA | 63 | +$254.54 | +8.55 | 84.1% | +17.98 / -69.82 | 51.0% |
| EXT | 28 | +$338.94 | +26.8 | 89.3% | +19.94 / -53.20 | 26.0% |

- DA 开盘 0–15：n35 净 -$210.53；`SL_INITIAL` 7 笔 -$696.47，其中 early-tight 6 笔 -$551.69。
- EXT 分臂：research_momentum n23 +$389.64；strict n5 -$50.70。EXT 整体为正主要由研究臂贡献，严格臂样本极小且为负。
- RFC-149 研究源窗（10-19→11-10，描述性、非最终结算）：n34 +$446.21；剔最大单笔 n33 +$339.57；beat n15 +$180.82 / 剔最大 +$111.24；watchlist n19 +$265.39 / 剔最大 +$158.75。仍须到 11-13 按预注册完整结算，不提前转正。

## 5. 问题清单

### P0 · 测量 / 契约 / 治理

1. **EXT 风险影子未注册**：`ext_entry_risk_shadow.jsonl` 已在采，`shadow_registry.REGISTRY` 实载 105 项但无 `ext_entry_risk_shadow`。按军规“在采影子未注册=禁止态”。同时 OBSERVING=52，需按 KR4 区分纯测量与实验并收缩，不是再造第 53 个实验。
2. **EXT `pending_sync` 不收口**：AMZN/NET 券商持仓存在，PositionSync 日志一致性 ✓；但 `open_positions.sync_status` 仍 pending。根因是 `position_sync.py:1672` 对已有行只在 size/direction 变化时 UPDATE；EXT 写入时 size/direction 已正确，状态永不翻 synced。它是状态契约缺陷，不是券商缺仓。
3. **DA 输入 as-of 契约不完整**：`_quality` 只证明维度存在，不证明时效；VIXY 已正确标 prev_close，但 capital_flow/fundamentals/analyst/insider/earnings/peers/market_context 缺统一 as-of/age/fallback 披露。
4. **计划价 / 重定价 / 成交价必须强制三层披露**：已有 `price_ledger` 与 `reprice_audit`，但日报/TCA 若只看 DB `entry_price` 会误读 DA 计划质量。MPWR/GOOG 证明 planned RR 与 fill RR 可显著不同。
5. **EXT 退出契约不透明**：无固定 TP 是设计内，但 A 档 cert_lock=0 时利润保护依赖 BE/trail/RTH；AMZN/NET 跨夜且 pending_sync。需要逐仓显式披露 TP/cert_lock/BE/trail/RTH 接管与隔夜的初始风险，不是立即加亏损止损。

### P1 · 经济 / 风险结构

1. **DA 赢小输大**：主修开盘尾损，继续 RFC-155 R2 open15 0.5× 独立前瞻（当前 0/30，TOO_EARLY）。不放宽 early-tight/原始 SL，不抬 BE/TP，不抬票面摊费。
2. **EXT 研究臂依赖**：11 月 EXT 正收益几乎全来自 research；strict n5 为负。11-13 必须按 RFC-149/RFC-167 三行读数（全样本/剔最大/分 source）机械结算，不自动续期。
3. **EXT 风险平价未测**：A/B 固定票面 + -5% SL 与 DA 风险预算 sizing 不同源；EXT 自有配额/在仓帽，未走 PlaybookService 账户熔断/组合护栏。先影子比较初始风险 USD、名义、费毛、隔夜 gap，不直接改仓位。
4. **跨臂方向覆盖不足**：11-10 DA 可得率 38%，冲突 0；不能据此证明无冲突风险，也不能直接加 DA 硬否决。先补覆盖与时序经济学。

### P2 · 数据源 / 证据 / 文档

1. **证据精确链接**：DA 已有 `evidence_gaps/recommendation_evidence`，建议做 claim→source item/publish_time/as-of 的只读覆盖率审计；不得把 `quality_score` 接成 gating（D3 红线）。
2. **新闻/研报去重与时效**：Futu news + Finnhub news_v2 + research + sentiment 可能重复同一事件；先 record-only 去重/聚类，不改 prompt 权重。
3. **VIXY 盘前真值**：若要用 `pre_price` 替代 prev_close，走 RFC-190 选项 C：先证明盘前成交质量并重标阈值，不直接切换。
4. **RFC-149 文档残留矛盾**：§2 末行仍写 `EXT_RESEARCH_ARM_UNTIL 10-31→11-30`，与顶部修正、§3、实际 env（旧臂 10-31 / source 11-13）冲突。应更正为 source 专用死线，避免误续期。

## 6. 优化建议（一次一变量）

### Batch 0 · 零交易行为，建议优先批准

| 项 | 动作 | 验收 |
|---|---|---|
| 0.1 影子注册 | 将 `ext_entry_risk_shadow` 注册为 record-only 测量项，给 data_path/config_key/min_n/复核日；纳入 KR4 盘点 | REGISTRY 实载可查；session_attribution H 段可读；不接交易门。 |
| 0.2 sync 状态收口 | PositionSync 对 broker 已匹配但 `pending_sync` 的已有行补 `synced/last_sync_at`；加同尺寸 EXT 回归 | AMZN/NET 下一轮 sync 后 synced；一致性检查覆盖 sync_status；不改出场参数。 |
| 0.3 三层价格账 | 日报/TCA 强制 planned/effective/fill + reprice rounds + slippage；成交数用严格 decision_id | MPWR/GOOG 可复算；`executed=1` 不再当成交数。 |
| 0.4 DA as-of manifest | 每维落 as_of/session/age/fallback/degraded；prompt 与日报显式标 stale | 33 raw 可生成 manifest；VIXY prev_close 不被当实时；无新硬闸。 |
| 0.5 EXT 退出契约 | 逐仓披露 TP/cert_lock/BE/trail/RTH 接管、初始风险 USD、隔夜 gap 风险 | AMZN A/cert0 与 NET B/cert3 差异可见；只读，不改退出。 |
| 0.6 RFC-149 预结算 | 修正文档矛盾；准备 11-13 三行读数 + 分 source + 日聚类/MDE/费用 | 到期可机械裁决；不提前转正/续期。 |

### Batch 1 · 影子 / 预注册，不改现役行为

- EXT 风险平价影子：初始风险 USD、名义、费毛、隔夜 gap、DA 可得率/冲突经济学；按日聚类 + MDE。
- EXT 无 TP / cert_lock / BE / trail 反事实：复用既有 bar 与费用模型，先落 DEB/RFC，不直接改仓。
- DA 证据精确链接覆盖率与 unsupported-claim 审计；不接 quality_score 门。
- VIXY `pre_price` 质量影子（RFC-190 选项 C），未裁不切换。

### Batch 2 · 行为变更，须另立 RFC + 用户裁

- RFC-155 R2 open15 0.5×：只在 11-27 机械判据通过后讨论转生产。
- RFC-149 研究源臂：11-13 按预注册转正/退役；任何转正另立 DEB，不自动延长。
- EXT TP/cert_lock/风险平价/跨臂方向约束：影子充分后一次一变量。

## 7. 明确不做

- 不关闭 chase cap、不放宽 early-tight/原始 SL、不抬 BE/固定 TP、不抬票面摊费。
- 不在盘前盘后新增亏损止损；不强平 AMZN/NET/BA/GOOG。
- 不复活 UE inject、C8/C13 盘前跌幅/bear/延续门、C9/C10/C11/C12/C14/C17/C18 同轴提案。
- 不复活 RFC-160 同轴硬闸；不把 `quality_score` 接成 gating。
- 不基于 EXT 11 月 n28 或研究源窗描述性正读数放大/转正。
- 不把测量修复描述成 edge 改善。

## 8. 可复现检查

```bash
./venv/bin/python scripts/session_attribution.py --date 2026-11-10
./venv/bin/python - <<'PY'
from app.playbook.shadow_registry import REGISTRY
print(len(REGISTRY), any(i.id=='ext_entry_risk_shadow' for i in REGISTRY))
PY
./venv/bin/python - <<'PY'
import sqlite3
conn=sqlite3.connect('file:data/ftseek_us.db?mode=ro',uri=True)
print(conn.execute("SELECT symbol,sync_status FROM open_positions WHERE symbol IN ('US.AMZN','US.NET')").fetchall())
conn.close()
PY
rg -n 'EXT_RESEARCH_ARM_UNTIL|EXT_RESEARCH_SOURCE_UNTIL' .env.us scripts/extended_hours_entry_executor.py docs/decisions/RFC-149-earnings-day-premarket-shadow.md
```

## 9. 验证与未验证

- 已验证：服务 active（PID 2557980）；DA 11-10 schedule complete；bridge/DB 21；严格成交 14；session_attribution 复跑；EXT 分臂/研究源窗只读 SQL；registry 实载；PositionSync 日志与代码路径。
- 未验证：未实施任何修复；未跑全仓测试；未重启；未调用券商；AMZN/NET 下一轮 sync 状态未观测；RFC-149 11-13 最终结算未完成；RFC-155 R2 新窗未来样本未产生。

## 10. 2026-11-11 16:52 Batch 0 实施回执（17:04 复核补针；用户裁「按照建议核查确认做合理优化」）

- 已落地（零交易行为）：
  1. `ext_entry_risk_shadow` 补注册为 record-only 测量项（REGISTRY 105→106，intent 检查清零）。
  2. `position_sync._reconcile_pending_sync_status`：broker 已匹配时 CAS 收口 `pending_sync→synced`，只改状态/时间，不改 size/SL/TP。
  3. `session_attribution` 新增 J 段 planned/effective/fill 三层价格账（严格 decision_id）与 K 段 DA 输入新鲜度审计；G 段披露 EXT grade/cert_lock/初始风险/利润保护。17:04 补针：初始风险改用 `initial_sl`，不再把 trailing 后的 `current_sl` 当初始风险；cert_lock/BE/trail 标注为触发阈值/条件路径，不冒充当前已触发保护。
  4. RFC-149 §2 残留“旧臂 10-31→11-30”更正为旧臂 10-31 / source 11-13；脚本台账同步 A-K。
- 部署：`systemctl restart ftseek-us` 后启动冒烟 **7/7**；PID 2584070；7 笔未平仓不变；日志确认 `US.NET/US.AMZN pending_sync→synced`，PositionSync 一致性 ✓。
- 验证：聚焦干净子集 **101 passed**；`session_attribution --json` J=21 指令/14 严格成交/15 executed 闩，K=31 份 19/19 + 2 份 18/19、VIXY 33/33 prev_close；`git diff --check` 通过。延迟复核启动冒烟用不带 `--wait` 的当前启动段判定，7/7 通过；`--wait` 仅用于重启后立即等待新启动段。

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
  tests/test_ext_entry_risk_shadow.py
```
- 已知存量：扩展跑 `test_rfc169_reconcile_linkage.py` 有 7 个 teardown 网络边界错误（VIX 外联），与本轮 sync 改动无关；测试框架报告已知生产库写入豁免 7 次（capital_state/earnings_calendar 等），非本轮新增写入点。
- 未做：不改 DA skill/prompt/生成端 as-of 写入（先只读审计）；不改 EXT TP/cert_lock/sizing/跨臂硬约束；不新增 cron/推送；不手工改 DB；不提前结算 RFC-149/RFC-155。
- 下一步：11-13 按 RFC-149/RFC-167 三行读数结算研究源臂；11-27 按 RFC-155 §6 结算 R2；Batch 1 影子另裁。

## 11. 2026-11-11 22:20 · 盘前指令推送源头 / 推理 / 新鲜度 / 有效性只读核查

- **触发**: 用户贴 2026-11-11 盘前 `📋 Playbook 交易指令`（27 条，25 可执行 + 2 SKIP），要求核查产出源头、推理是否合理、新鲜、有效。
- **源头**:
  - DA 主链 `9694df7b6024` 08:01:09 ET 发车，08:31:39 ET `complete`：79 扫描 → 35 分析 → 27 bridge / 27 DB instruction。
  - 用户这条推送来自 in-app `run_playbook_morning_push` → `PlaybookPusher.push_morning_instructions`，日志 22:01:45 CST（09:01:45 ET）`sent=True sync=True`；读取 `playbook_instructions` + 实时 `MarketRegimeGate.assess()`。
  - 22:12:15 CST（09:12:15 ET）另有 `scripts/da_bridge_dingtalk_push.py --wait-da` 的 bridge 摘要推送，台账 `da_push_instruction_summary.jsonl` = 27/25/2；两者不是同一消息。
- **新鲜度**:
  - bridge `generated_at=08:31:38 ET`，DB `created_at=13:31:38 UTC`，推送 09:01 ET；计划_age≈30min，执行端另走 re-price 实时 bid/ask。
  - `da_input_freshness_manifest --date 2026-11-11`：raw 35、parse_error 0、quality 35×19/19、snapshot 35 premarket；但 per-dimension `asof_n=0`，VIX 35/35 `prev_close`，`decision_use=display_only_not_a_trading_gate`。
  - 实时 regime：`risk_off, VIX=15.44, SPY=749.92, MA20=760.69, MA50=764.55, MA200=728.30, score=-2`；`spy_price_basis=kline_last_close`，盘前 SPY=757.01 仍 < MA20，regime 不因盘前价翻转。
- **推理**:
  - 27/27 有 `conditions_json`；bridge 有 thesis / recommendation_evidence / debate / evidence_gaps。新闻引用多为当日盘前（如 AMZN 05:01 ET、TSLA 06:47 ET、GFL 07:31 ET、HIMS 08:00 ET），但语义仍是 `UNADJUDICATED_NEWS_REFERENCES`，不是已裁决方向事件。
  - 辩论分布：1 条 `✅ 2模型一致`（META），16 条 `方向一致但挑战者有质疑`，8 条 `挑战者弃权`。5 条有 evidence_gaps（GFL/GOOG/LMT/META/PDD）。
  - UE da_feed 08:03:45 ET 浮现 5（4 SHORT actionable）；DA 分析 3 条 UE origin：TMUS/GFL 由 UE SHORT 翻为 BUY，HIMS 保持 SHORT 但 admission SKIP。TMUS  thesis 承认 SpaceX 竞争但押注基本面/低吸；GFL thesis 押注拒绝收购后的错杀轧空，且挑战者“明确反对但未给反方向/输出自相矛盾”、自身有 2 条证据缺口 ⇒ 这两条是最需要人工复核的推理，不是链路错误。
- **有效性**:
  - 25 条可执行指令 SL/TP 几何全部正确，`risk_reward` 与复算一致；RIVN `PRICE_BELOW_FEE_FLOOR(13.75<17.33)`、HIMS `SL_STOP_TOO_WIDE(10.7%>8%)` 两条 SKIP 符合现役 admission。
  - TSM R:R=0.94<1 仍 PASS：现役 admission 用 SL 宽度/费用/价格地板替代静态 RR 门，不拦 RR；属低赔率计划，不是无效指令。
  - 隔夜敞口 6 仓与 `open_positions` 复算一致（BA/GOOG/PDD/HON/UBER/NET）；当日 EXT DDOG/QCOM 不入隔夜段。今日指令中 NET/UBER/GOOG/BA/HON/PDD 已持仓，执行时受重复开仓守卫/already_held 对账约束，推送是计划池而非成交清单。
- **显示问题（不改交易）**: `feishu_push._format_morning_message` 的 conf_tag 只匹配 `2模型一致/一方弃权/真反向/方向分歧`，现役 debate_outcome 文案是 `挑战者弃权/方向一致但挑战者有质疑`，导致 24 条非一致信号统一显示 `🎚`，吞掉“质疑/弃权”语义。建议后续零行为展示修复；本轮未改代码。
- **验证**: 只读 SQL / bridge JSON / `MarketRegimeGate().assess()` / `da_input_freshness_manifest.py --date 2026-11-11` / `ue_da_chain_health.py --date 2026-11-11 --json` / `logs/ftseek_us.log` / `data/da_push_instruction_summary.jsonl`。产物：`/tmp/ftseek_da_freshness_20261111.json`、`/tmp/ftseek_ue_da_health_20261111.json`。
- **未做**: 零代码/配置/DB/cron/推送改动；未重启；未验证开盘后触发/成交；未修 `🎚` 标签；不恢复 UE inject/fresh-event execute。

## 12. 2026-11-11 22:49 · 开盘后日志 / 链路 / 逻辑策略只读核查

- **窗口**: 2026-11-11 22:30–22:49 CST（09:30–09:49 ET），`MarketPhase.TRADING`。
- **健康**: `ftseek-us` active（MainPID 2589190，NRestarts=0）；`scripts/startup_smoke_check.py` 7/7。`logs/ftseek_us.log`、`logs/ext_entry.log`、`logs/rsshub_us.log` 开盘后 0 ERROR/CRITICAL。`event_stream_ingest.log` 早前 GOOGLE_NEWS_RSS 单次 ERROR 后续恢复 OK；EventStream 是旁路发现源，不直接下单。
- **入场链**: RTH 新仓 7 笔全部 `BOOKED` / `FILLED_ALL`：
  - MRK BUY 25 @146.27（09:38:38 ET）
  - AMZN BUY 14 @272.89（09:38:15 ET；09:45:09 ET 平仓）
  - TSLA SELL_SHORT 10 @387.755（09:41:45 ET）→ `Direction.SHORT` → DualBroker → Futu `TrdSide.SELL`，正股做空路由正确。
  - LLY BUY 3 @1423.43、GFL BUY 96 @42.60、CAT BUY 6 @593.27、GS BUY 4 @915.16。
  - `SUBMITTED/SUBMITTING` 为异步确认中间态，最终均由 order receipt reconcile 收口到 `BOOKED`，未见卡单。
- **退出链**: `PlaybookExitFastLoop` 持续运行；SIM 臂保护由退出快循环 + durable exit intent 承担。开盘后三笔退出均闭环：
  - NET LONG 09:30:45 ET `SL_TRAIL_LOCK`，净 +85.43，intent BOOKED。
  - DDOG LONG 09:32:13 ET `SL_TRAIL_LOCK`，净 +15.25，intent BOOKED。
  - AMZN LONG 09:45:09 ET `SL_INITIAL`（early-tight SL 触发），净 -51.75，intent `12780b3d…` BOOKED，`exit_price_source=broker_deal`，券商持仓 14→0。
  - `data/playbook_exits.db` 非 BOOKED intent = 0；`exit_intent_reaper` 未检出账实不符。
- **门禁/策略合理性**:
  - 已持仓票 UBER/GOOG/PDD/BA/HON 命中重复开仓守卫，计划池≠成交清单，预期。
  - EntryConfirm v2（120s + grace 2/3）对 META/BABA/MSFT/NET/TSM/MU 等出现反转放弃；slope/VWAP/chase/stale quote 拦截有具名日志，属现役执行闸。
  - risk_off sizing 日志 `water=1.00 × regime=0.30 × conf=1.00`，风险金额约 $165–213，符合风险_off 降仓。
  - 组合方向偏斜 guardrail 日志 `would-skip … F1摘闸影子照算, 不拦截` 与 `portfolio_guardrail_enabled=false` 一致，是 record-only 影子，不是硬闸被绕过。
  - AMZN 亏损由 `early_tight_sl_enabled=true` + ATR 比例紧 SL 触发（tight=1.25%），链路执行正常；单笔亏损不能单独判该规则无效，须按预注册 cohort 结算。
  - EXT 开盘后无新入场；DDOG/QCOM 为盘前 EXT 仓。QCOM 仍 `PLAYBOOK_SIM` 未平，W_WEEK 豁免转 SL/EOD/5日上限链，SL=171，无 TP 是已知 EXT 退出契约。
- **观测面问题（非交易阻断）**:
  1. **PositionSync memory 幽灵误报**: 22:39 起 `Memory 有幽灵仓: {'US.AMZN'}`，22:42 起 `{'US.LLY'}`，随后每轮 `一致性: ⚠差异`。券商/DB 持仓存在且数量匹配，退出账本干净。代码根因：`_check_consistency` 用 `playbook_open_check` 从 `futu_set_for_mem` 排除自管仓，但 `mem_set` 未做同口径排除；sync recovery 先把新仓写入 position_store，随后收养为 playbook_sim，内存残留被误判为幽灵。`主线DB` 计数还把 EXT QCOM（owner_strategy=extended_hours_entry）与 playbook 自管口径重叠，展示易误读。
  2. **LLY open_positions 展示真值不一致**: `open_positions` SL/current_sl=1394.9614、TP=1851.99；指令与 `trade_records.metadata` 真值 SL=1352.35、TP=1530.29。出场链 `exit_monitor_flow` 读 `trade_records`，实际退出真值未错；但 `open_positions` 是看板/旧风控取值面，被 sync recovery 合成 ATR/TP 与一次 legacy `update_trail` 污染。RFC-195 对齐未在运行路径收口，需修复并防止 legacy trail 写 playbook 仓。
- **待生效修复**: TwelveData HTTP 429 熔断/WARNING 修复、盘前预检 UE/做空展示纠偏已落代码，但主服务未重启，进程内旧代码待统一重启窗口；开盘后未再出现 429 ERROR。
- **验证**:
```bash
./venv/bin/python scripts/startup_smoke_check.py
sqlite3 'file:data/ftseek_us.db?mode=ro' "SELECT id,symbol,direction,execution_state,broker_order_status FROM playbook_instructions WHERE date='2026-11-11';"
sqlite3 'file:data/ftseek_us.db?mode=ro' "SELECT id,symbol,entry_time,exit_time,exit_reason,pnl_net,source_type,owner_strategy FROM trade_records WHERE trade_date='2026-11-11';"
sqlite3 'file:data/playbook_exits.db?mode=ro' "SELECT state,COUNT(*) FROM exit_intents GROUP BY state;"
rg '2026-11-11 22:(3[0-9]|4[0-9]):.*(ERROR|CRITICAL)' logs/ftseek_us.log logs/ext_entry.log logs/rsshub_us.log
rg 'PositionSync|PlaybookExitFastLoop|EntryConfirm|would-skip|US.TSLA|US.AMZN' logs/ftseek_us.log
```
- **未做**: 零代码/配置/DB/cron/推送/交易行为变更；未重启；未手工改 DB；未修 memory 误报与 LLY 展示真值；未据单笔亏损改 early-tight SL / guardrail / 任何交易门。
- **下一步**: 待 WIP 收口后做零交易行为观测修复：一致性检查 memory 侧同口径排除自管仓；open_positions SL/TP 对齐指令真值；禁止 legacy update_trail 写 playbook 仓。观察下一轮 UE scan 429；不恢复 UE 直注，不提前结算 RFC-149/RFC-155。

## 13. 2026-11-11 22:55 · 两个非阻断观测问题修复回执（出场零影响；heat 输入口径保守变化）

- **问题 1 — PositionSync `Memory 有幽灵仓` 误报**:
  - 根因: `_verify_and_sync_capital` 的 Memory 分支只从 broker 侧 (`futu_set_for_mem`) 排除自管/playbook 仓，未从 `mem_set` 排除。sync recovery 竞态先把新仓写入 `position_store`，随后补录 trade_record 收养为 playbook_sim；下一轮 broker 侧已排除该仓，memory 侧残留 ⇒ `mem_set - futu_set_for_mem` 恒含该仓，每轮误报（11-11 AMZN/LLY）。
  - 修复: 新增 `mem_set_for_check`，与 broker 侧同口径排除 `_self_managed` + `playbook_open_check`，再做缺失/幽灵比较。只改告警，不参与交易。
- **问题 2 — LLY open_positions 显示 SL/TP ≠ 指令真值**:
  - 根因: `_check_positions` 的 trail 回写循环遍历 `monitored_positions`（含 recovery 残留的 playbook 仓），用 recovery 合成 SL/TP（ATR×mult，LLY=1394.96/1851.99）调 `upsert_open_position(action=update_trail)`，覆盖 RFC-195 已对齐的指令真值（1352.35/1530.29）。出场链读 trade_records，实际退出未错；错在显示/旧风控取值面。
  - 修复: 回写循环对 `playbook_open_check` 命中的自管仓跳过（SSOT 判据，与退出/一致性同口径）。退出仍由 `monitor_playbook_sim_exits` 独占，**出场零影响**。边界补充：`portfolio_heat` 会读 open_positions 的 current_sl/initial_sl 作为 admission 输入，因此 heat 口径会恢复指令真值；方向为保守（LLY 案例 open risk 增加，不会放宽准入），不能再笼统称“所有交易行为零变化”。
- **改动**: `app/core/position_sync.py`（+12）、`app/pipeline/position_monitor.py`（+10）、`tests/test_sync_consistency_memory_exclusion.py`（+2 例）、新 `tests/test_playbook_trail_write_guard.py`（2 例）。
- **验证**: 聚焦 5 文件 29 passed + 相邻 5 文件 44 passed；`py_compile` / `git diff --check` OK。7 次生产库写入为已知存量豁免（pdt_guard 等），非本批新增。
- **未做 / 边界**: 未重启（代码不热重载，待统一窗口）；未手工改 DB；LLY 历史合成显示值停止覆盖后随下次重启恢复/reconcile 对齐收口，未即时纠正；recovery 路径「playbook 仓不入 position_store」的根因修法属行为变更，按治理需影子/RFC，本批不做；残留内存仓在问题 1+2 修复后无害（退出跳过、手动安全网按 source 跳过、不再误报/回写）。
- **复跑**:
```bash
./venv/bin/python -m pytest -q tests/test_sync_consistency_memory_exclusion.py \
  tests/test_playbook_trail_write_guard.py tests/test_rfc195_open_position_stop_align.py \
  tests/test_open_position_sl_tp_contract.py tests/test_position_sync_pending_status.py
```
- **下一步**: 统一重启生效后核 PositionSync 一致性转 ✓、LLY open_positions 对齐真值；不据本批改交易门。
