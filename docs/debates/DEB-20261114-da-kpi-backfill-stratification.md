# DEB-20261114 — DA 死单率 KPI 修尺子：剔除收养/补录记账行（11月「回升」证伪为尺子污染）

- 状态：**只读修尺子已落地 + 契约测试钉死；零交易行为变更、零新影子、零重启**
- 用户裁（2026-11-14）：「针对有把握的问题直接修复…从源头最优修复」「继续」（查 11 月 DA 死单率回升因）
- 关联：DEB-20261114-strategy-premarket-focus-da-retained §3（DA 核心 KPI=死单率）、RFC-209（降频人群精化）、DEB-20260727-entry-fill-price-reconciliation（GHOST-RC1 根因）、OKR-2026-11 主 KR（DA-only 净 bps）。

## 1. 结论一句话

**11 月 DA 死单率「回升」（15.8%→22.8%）不是 LLM 信号退化，而是尺子被「收养/补录记账行」污染。** 剔除后真实信号死单率 **15.8%（6/38）**，与 10 月 **15.6%（19/122）** 持平——7→9 月的改进（31%→16%）**保持住了**。同理「11 月 57% RANGE」也是补录行硬编码 RANGE 所致（真实信号为 cautious/risk_off）。**修尺子后，DA 改进靶点更清晰：不是救「回升」，而是把稳定在 ~16% 的死单率继续压到平衡线 ~19% 以下。**

## 2. 发现（只读实证，A9 干净）

DA（`owner_strategy='playbook_sim'`）按 `entry_reason` 分解：

| 月 | 表观死单率(全样本) | 真实信号死单率 | 补录/收养行 n(死) |
|---|---:|---:|---:|
| 2026-07 | 31.3% (n=182) | 31.8% (n=179) | 3 (0) |
| 2026-08 | 29.5% (n=207) | 29.6% (n=206) | 1 (0) |
| 2026-09 | 16.6% (n=175) | 16.7% (n=174) | 1 (0) |
| 2026-10 | 15.8% (n=133) | **15.6% (n=122)** | 11 (2) |
| 2026-11 | **22.8% (n=92)** | **15.8% (n=38)** | **54 (15)** |

- 11 月 21 笔表观死单中 **13 笔来自 `sync_recovery_backfill`**（+2 笔补偿）；10 月仅 2 笔。**「回升」几乎全由补录行涌入造成。**
- 补录行 **100% 硬编码 `market_regime='RANGE'`**（全史 63/63）；真实 11 月信号为 cautious(21)/risk_off(17)，**根本没有 RANGE**。「regime shift」是假象。
- 补录行是**真实持仓、真实盈亏，非重复行**：与正常信号 **零 `(symbol,trade_date)` 重叠、零 `futu_order_id` 重叠**，订单号与正常单交错（如 11-02 补录 8318948/8318993 与正常 8319065/8319124 相邻）。11 月补录净 **−$86.08**。
- 补录**每个交易日都发生**（11 月逐日 4/3/5/2/6/4/9/5/9/5 笔），10 月 11 笔 → 11 月 52 笔（**~5×**），是慢性工程问题，非单次重启。

## 3. 根因（代码自证，非推测）

`app/core/position_sync.py::_backfill_playbook_trade_record` 补录时：`peak_price=entry`、`trough_price=entry`（**峰值重置**）、`atr=_proxy_atr_from_stop`（**代理 ATR**）、`market_regime="RANGE"`（**硬编码**）、`entry_vix` 取**收养时刻**值。`scripts/archive/adopt_position_to_playbook.py` 手动收养同样重置 peak。
- 故补录行 **MFE 从收养时刻起算**（漏掉入场→收养间的有利波动）⇒ **系统性低估 MFE ⇒ 虚高「MFE<0.3% 死单」**；regime/ATR 亦不同量纲。
- **仓内早已两处告警**：`position_sync.py:704`「研究侧可按 `entry_price_source='sync_recovery_backfill'` 过滤或单独分层」；`entry_geometry_review.py:233`「ATR/peak/MFE 可能是收养时近似值，须先修口径再评估退出」。本批就是把这两句告警**落到 DA 研究尺子上**。

## 4. 修复（只读修尺子，分层≠抹账）

新增共享助手 `scripts/utils/da_sample.py`（`ARTIFACT_ENTRY_REASONS={sync_recovery_backfill, playbook_entry_bookkeeping_compensation, adopt_to_playbook}` + `split_signal_artifact`），四把 DA 研究尺一致接入：
- `da_entry_quality_trend.py`：headline 死单率/月度趋势**只算真实信号**；补录行单列 `artifact`（n/死/净$）+ 全样本 `dead_rate_all_pct` 保留。
- `pnl_expectancy_pivot_analysis.py`：`decomposition`=真实信号期望；`decomposition_all`=**全样本总账保留不抹账**；`artifact` 单列；降频反事实只算真实信号入场。
- `edge_discovery_screener.py`：发现样本剔除补录行（其特征近似会造虚假切片）；**EXT 盘前切片不受影响**（补录全属 playbook_sim）。
- `settle_da_frequency_cap_pivot.py`：RFC-209 人群精化（cohort 未起 days=0 前置澄清，见 §7）。
- **纪律**：补录行真实盈亏仍计入总账（`decomposition_all`/`artifact.net_usd`），绝不静默丢弃；只把它们从「入场质量/信号期望/edge 发现」度量中分出。

## 5. 修正后的 DA 图景（诚实，不夸大）

- **死单率趋势（真实信号）**：7月 32.0% → 8月 29.6% → 9月 16.7% → 10月 15.6% → 11月 15.8%。**改进保持，11 月无回升。**
- **DA 整体仍略负**：真实信号全史 n=718、死单率 24.0%、活单 +35.98bps/死单 −152.54bps ⇒ 平衡线 **19.1%**，实际 24.0%>19.1%。**修尺子不改变「DA 略高于平衡线、需压死单率」的结论**，只纠正「11 月恶化」的误判。
- **前进窗（10-06+）真实信号**：n=138、毛 −3.75/费 9.73/净 **−13.47bps**（净 −$992）；补录 65 笔净 −$242；全样本总账净 −11.70bps（−$1234，与修前一致）。**修尺子让前进窗信号期望更诚实（略更负），不是更好看。**
- **EXT 盘前 edge 不变**：n=36、+32.50bps、t=3.44、留一日 3.08、top3=45%，仍是唯一 ROBUST（screener 切片 23→20，去掉补录虚假切片）。

## 6. 真正的工程根因（已查明，behavior-touching，本批不动）

**定论：补录涌入 = 「成交确认 → 写台账」之间被重启/中断打断 → 孤儿仓 → 降级补录回收。** 不是 GHOST-RC1 拒记，也不是已闭环的 quote_fallback 假价（11 月 `quote_fallback`=0，07-27 go-forward 修复保持）。

证据链（只读实证）：
- 11 月 52 笔补录 **全部对应 BOOKED 指令**（`no_instruction=0`），且指令表 **零 FILLED_UNBOOKED/UNKNOWN** ⇒ 不是「记账被拒」，是「记账后被回收」。
- 机制：`playbook_service.py:2150` 成交即 `mark_execution_filled_unbooked` → 写 `trade_records`（`save_trade` 是 INSERT，正常落 `playbook_futu_simulate`+真 regime+真 ATR）→ 标 BOOKED。**若在写台账前重启/中断**，指令停在 FILLED_UNBOOKED；`position_sync._reconcile_playbook_execution_states`（:767/:858）回收时**补录降级 record 并标 BOOKED** ⇒ BOOKED+补录并存。
- 补录降级三处（`position_sync.py:712-746`）：`peak_price=entry`（重置）、`atr=_proxy_atr_from_stop`（代理）、`market_regime="RANGE"`（硬编码）⇒ MFE/regime/ATR 失真（§3）。
- **开盘集中**：补录入场时刻 81% 在 09:xx ET（开盘抢单高峰）；**重启相关**：11-12 重启最频繁（journalctl 18 条 Started/Stopped）= 当日补录最多（9 笔）。11 月开发重启频繁 ⇒ ~5× 涌入。
- 同库（SIM arm→`ftseek_us.db`，非 arm 路由问题）；`save_trade` INSERT 非 upsert（非覆盖污染）。

**性质**：这是**流程成熟度缺陷**（用户「流程成熟高效」落点）——11 月 57% DA 仓靠降级安全网回收，而非干净记账。安全网本身有效（无失控孤儿仓迹象，11 月补录净 −$86 量级小），但：①回收 record 度量失真（已由 §4 分层隔离）；②孤儿窗口（成交→回收）暴露于无 playbook 看护；③重启越频繁污染越多。

**修法方向（待用户裁，behavior-touching，须影子先行 + KR4 名额）**：
- **方案B（低风险首选）**：补录回收时**继承关联指令真值**——用 `_extract_atr_with_basis(inst)` 取真 ATR（替代理）、regime 取指令上下文/入场时刻真值（替硬编码 RANGE），peak 用分钟 bar 重建（metadata 已有 `peak_price_bar/bar_covered_until` 缝）。改的是回收 record 质量，SL 锚随之更准。
- **方案A（根治重启安全）**：成交确认即落「booking 意图」（含真成交价/regime/ATR），重启后按意图**完成原始干净记账**而非降级补录。更深、更 invasive。
- **治理约束**：二者均 behavior-touching，须影子先行 + 一次一变量 + 重启前核；**KR4 当前超编（~40 活跃实验 vs ≤5），新增影子前须先盘点腾名额**（DEB-20261114-KR4-consolidation 待用户确认 KEEP≤5）。**本批不修、不新增影子**，先落定论 + 留证，待用户裁立项。

## 7. 治理边界

- **只读修尺子**：四尺均 `mode=ro`，零 DB 写/零下单/零生产闸改/零推送；唯一写产物是 settlement 既有的 record-only shadow JSON（verdict 仍 TOO_EARLY days=0）。**零交易行为变更、零新影子、零重启**（修尺子属白名单，不触发影子先行）。
- **RFC-209 人群精化=cohort 前置澄清**：cohort 2026-11-16 未起（days=0，零前瞻样本），此时把人群从「playbook_sim ∧ A9」精化为「再剔除收养/补录行」是**预注册前置澄清，非窗内改**；已在 RFC-209 §3 落档。
- 恒真红线 C8/C9/C12/C14/C15/C16/C17 全维持；C1 不新增入场假设；OKR §7 不据小样本放大仍守。

## 8. 凭据 / 验证 / 未验证

- **凭据**：HEAD `12607e995`；新增 `scripts/utils/da_sample.py`、`tests/test_da_sample.py`（4）+ 本 DEB；改 `scripts/{da_entry_quality_trend,pnl_expectancy_pivot_analysis,edge_discovery_screener,settle_da_frequency_cap_pivot}.py` + 对应 4 测试（各 +1 分层用例）+ RFC-209 §3 修正注 + strategy DEB §3 修正注 + handoff 顶条；未提交。
- **验证**：`pytest tests/test_da_sample.py tests/test_da_entry_quality_trend.py tests/test_pnl_expectancy_pivot_analysis.py tests/test_edge_discovery_screener.py tests/test_settle_da_frequency_cap_pivot.py` **22 passed**；`py_compile`/`check_undefined_names` exit 0；四尺实跑读数见 §2/§5（KPI n=718 信号/71 补录；screener EXT 盘前仍 ROBUST；settlement TOO_EARLY days=0）。
- **未验证**：GHOST-RC1 记账被拒率上升的真因（待排查）；补录仓真实 MFE（需分钟 bar 重算，本批未做）；DA 死单率能否持续<19.1%；任何收益/live readiness。

## 9. 下一步

1. **每周复跑四尺**（已修），盯真实信号死单率是否持续<平衡线 ~19%。
2. **GHOST-RC1 排查**（待用户裁）：查 11 月记账被拒率上升真因 → 若属实，影子先行修记账路径，从源头减少孤儿仓/补录（既减真实失血，又减尺子污染）。
3. 盘前前瞻确认（cohort 11-16）+ RFC-209 降频前瞻（n≥30 约 12-28）并行；KR4 退役路由待确认。
