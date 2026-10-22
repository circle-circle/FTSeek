# FTSeek-US · HANDOFF

## 2026-10-23 工程阻塞修复（CST；ET 10-22，未部署）

**触发**：用户「请修复明显阻塞和可优化项」。本批不调整信号规则、计划仓位参数、退出阈值或实盘授权。

- 行情：时敏 K 线调用只读缓存，首次连接/SDK 请求移入单个 daemon 刷新槽；槽忙不排队，不回退同步旧签名，NONE/HFQ 不污染 QFQ push 缓存。定向隔离回归 **40 passed**。
- REAL 报价：健康门不能通过关闭可选开关绕过；拒绝代理/锁定/交叉盘口、非有限价格、缺失/无效/未来的交易所时间及过期报价；SIM 原判定保持。定向隔离回归 **43 passed**，含 socket/Futu/SQLite 沙箱断言。
- 部分成交：只有身份匹配的终态累计成交量/VWAP可以 BOOKED；撤单 ACK 不算终态，metadata CAS 保存成交高水位，缺价缺量不猜。恢复器复查 FILLED_UNBOOKED，使用标的双锁、券商持仓一致性守卫与订单收据幂等；保留遗留 P3 的订单核验恢复。独立审查指出的数值订单 ID 与字符串误判已修，REAL 未决仅首次/原因变化报警。定向回归 **111 passed**。
- 证据修正：阈值算术及人群错配更正已记入 DEB §43；相关未跟踪影子文档仍随原批次留在工作区，实验阈值及前瞻判据不变。
- 合并验收：独立执行 7 个目标测试文件，**194 passed in 21.73s，退出码 0**；两轮独立代码审查完成，本批代码/测试 scoped diff 格式检查通过。详情见 `docs/debates/DEB-20261009-event-to-trade-pipeline.md` §43。

**提交授权**：2026-10-23 用户批准提交已修复改动并推送当前远程分支；只提交本批代码块、测试及记录，其他事件流/影子/配置/股票资料不纳入。

**下一步**：先确认本批及其他在途源码的上线范围，再按军规核在仓/出场意图、安排重启与启动冒烟。运行态验收 **BLOCKED**：未确认隔离交易环境，不向管理持仓的服务注入故障；未执行重启、四锁未动。只读核对 PID 3159408 启动于 10-22 21:42 CST，早于本批源码。SDK 本身未被取消，永久悬挂时刷新槽保持占用，但不拖住时敏调用线程；REAL 重启 STOP 幂等恢复、多腿费用和账户回撤仍须单独验收。

## 2026-10-20 左尾尸检：入场/止损逻辑（只读，零生产变更）

**触发**: 用户「先不动出场，确保先修好左尾（入场/止损）的逻辑合理和可落地」。

**读数** (`docs/analysis/2026-10-20-left-tail-autopsy.md`):
- 30 日 155 笔（净 −$786.97）；左尾 16 / 墓地 21。
- **开盘 0–15min：94 笔，净 −$1,175.69，左尾 13/16**；09:45–10:30 +$314.86。
- **ATR 5–8%：75 笔，净 −$1,711.08，左尾 11/16**；3–5% +$757.97。
- **UE 源：12 笔，净 −$773.91**；EXT +$166。
- 计划锚差**非单调**：执行优于计划 29 笔 −$1,042.52；≥200bps 38 笔 −$674.80；
  0–50/100–200 为正。首版分桶把负值误归 ≥200（n=67 假读数），已更正为 38。
- 止损不是根因：`SL_INITIAL` MFE 中位≈0、持仓约 17min；放宽初始 SL 只会放大亏损。
- 根因判断：**archetype 语义未被执行**——FADE_SHORT 市价追跌、PULLBACK_LIMIT 市价接刀；
  路由开关仍 false。

**P0 路由反事实 (2026-10-20, 只读)**:
- `scripts/entry_routing_counterfactual.py`，124 笔可评估 (FADE_SHORT 29 / PULLBACK 95)，0 缺 bar。
- raw_confirm_300：成交率 33.9%，cf −$517.45 vs actual +$72.33 ⇒ **delta −$589.78**；
  跳过 69 个赢家、实际净额 +$847.44。900s 仍 −$433.47；reanchor_300 −$113.83。
- **PULLBACK_LIMIT 限价路由是逆向选择**：delta **−$640.32**（跳过大量不回踩的赢家）；
  FADE_SHORT 小幅正向 **+$50.54**，样本 29、幅度小。
- **结论：不全局启用 archetype 路由**；RFC-153 的 FADE_SHORT 开盘禁追已覆盖更精准的靶面。

**修订后方案**:
- **不做**：全局 routing / 宽 PULLBACK 限价路由。
- P2 风险尺寸影子（优先，10 月可做）：ATR≥5%/开盘/UE/≥2 旗标 → 0.5× 尺寸；
  反事实 +$828~+$856；去杠杆不是 alpha，单笔 bps 不变，须如实标注。
- PULLBACK 条件门（11 月 C1）：只对左尾形态做窄门（趋势向下/跌破 VWAP/ATR≥5%/执行价低于计划），
  先只读分层，11 月预注册；不做宽路由。
- P3：保持 ATR 初始 SL；墓地时间止损只做影子。
- C1 约束：10 月新入场假设名额已被 RFC-153 占用；尺寸/条件门仍需 RFC + 用户批准。

**P2 风险尺寸首跑 (2026-10-20, 只读)**:
- `scripts/risk_size_tier_shadow.py` + `tests/test_risk_size_tier_shadow.py`；产物
  `data/shadow/risk_size_tier_latest.json`；注册表 `risk_size_tier_shadow`（OBSERVING,
  `IN_SAMPLE_COUNTERFACTUAL`）；RFC-155。
- 155 笔/22 日：R1 atr5（主）delta **+$855.54**（回撤 1057.63→656.81；半窗 +229/+626）；
  R4 ge2 delta **+$1,065.70**（回撤→534.11；半窗 +673/+393）；R6 product +$1,191.86
  （但影响 123/155 笔）；R2 open15 +$587.84（半窗 +491/+97）；R3 ue +$386.95（半窗 +387/0）。
- R4 的 ge2 = ATR/开盘/UE ≥2，与左尾尸检表含 plan-gap 的旗标集不同源，不可混读。
- 结论：预注册主臂 R1 稳定正向；R4 更强但为组合档；**均不直接切生产**。下一步只做前瞻
  记录影子（记录 would_multiplier/counterfactual_size，不改实际 size），≥10 日/30 笔再裁。

**P2 前瞻记录影子上线 (2026-10-20 23:41 CST)**:
- `app/playbook/risk_size_shadow.py` + `auto_execute_flow` 接线（execute 前构建/execute 后收口）；
  只写 `data/shadow/risk_size_tier_forward.jsonl`，**不改实际 `_size`/入场/退出**。
- 配置 `playbook.execution_parameters.risk_size_shadow_enabled: true`（回滚 false+重启）；
  注册表 `risk_size_tier_forward`（OBSERVING, min_n=30, where:success=True）；
  评估器 `scripts/risk_size_tier_forward_eval.py`（只读 join trade_records）。
- 验证：执行链+影子治理+四棘轮 **294 passed**；编译 OK；启动冒烟 **7/7**，PID **2706354**。
- 首条前瞻行待下一次成功 execute（重启后首批被 chase_cap 拦下，暂为空）。
- 读数命令：`PYTHONPATH=. ./venv/bin/python scripts/risk_size_tier_forward_eval.py`；
  满 10 ET 日/30 笔后再裁是否启用乘数。
- 重启时日志有一条 FutuLV3 退订部分失败 (真失败=4) — 属重启瞬态，冒烟通过，继续观察。

**状态**: 只读反事实 + 前瞻记录影子；**生产仓位零改动**。

## 2026-10-20 RFC-154 P0 出场条件审计 + P1 影子预注册（零生产变更）

**触发**: 用户问「初盈利就跑、赢小输大，基于幻方经验有无优化方案」→ 认可先做 P0 只读审计 + P1 影子预注册。

**P0 结果** (`scripts/exit_conditional_audit.py`, 产物 `data/shadow/exit_conditional_audit_latest.json`):
- 648 笔 post_exit → 575 笔有效 trail 样本；止损类 51 笔单列。
- **测量陷阱**: `SL_INITIAL`/跳空止损的“出场后继续持有”是止损后反转偏差；首版未剔除时 trail_0.8 均值 +32.6bps(t=5.69)，剔除后 **+12.65bps(t=2.66)**，BE 单桶仅 +0.91bps。宽止损必须做入场→出场全路径重放，本审计不评估。
- **结论**: hold-to-close −10.44bps；trail 1.2–2.0% 与 ATR 宽 trail 全负 → **不要全局让赢家跑**。BE 桶 mean +0.91 / **median −17.20** → BE 多数并不早，体感来自尾部。唯一稳定正向是 `SL_TRAIL_LOCK` +9.01bps(median +2.76, t=2.52)；`TIME_EXIT` +44bps(n=35) 仅探索。
- 左尾与成本仍是更大杠杆：平均亏 −$104.75；平均赢家 ~44bps vs 费+滑点 ~46bps。

**交付**:
- `scripts/exit_conditional_audit.py` + `tests/test_exit_conditional_audit.py`（**7 passed**；止损类剔除契约已钉）。
- `docs/decisions/RFC-154-exit-conditional-shadow.md`: P1 四臂预注册（T=trail min 0.4→0.8 主臂 / B=BE 后移 / D=BE 半仓 / X=时间退出），全路径 replay、扣费净 bps、日聚类 t/MDE、n≥30/10 日；**不碰 exit hash，不改生产参数**。
- 本批纯脚本/文档，**未重启**。

**T 臂首跑 (2026-10-20, 只读)**:
- `scripts/exit_t_arm_shadow.py` + `tests/test_exit_t_arm_shadow.py`；产物 `data/shadow/exit_t_arm_latest.json`；注册表 `exit_t_arm_min_dd_08`（OBSERVING, `FIDELITY_GATE`）。
- 139 笔 / 21 ET 日 / 0 缺 bar；baseline vs T（min DD 0.4→0.8）：pess delta **−5.77bps (t=−1.30)** / opt +0.04bps；harm 1139bps / save 0。
- **baseline vs 实盘 reason_match=0.604 < 0.80** ⇒ 按回放引擎纪律冻结变体读数；**不采纳 T 臂，不改生产**。P0 的“出场后 trail +9bps”不能直接外推为全路径改进（全路径把持有期回吐算回来了）。
- 新增可选 `adaptive_cfg` 到 `exit_replay_backtest.replay_trade`（默认 None，现有调用零变化），使回放忠实走 adaptive trailing 公式。

**下一步**: 先修回放保真度（锚定回放 / 10s 报价 / BE 限价成交 / fee-aware 垫），`reason_match ≥0.80` 后才允许读 T/B/D/X 变体；在此之前不提案、不改生产。

**回滚**: 删脚本/RFC 即可，零生产影响。

## 2026-10-20 RFC-153 FADE_SHORT 开盘快跌禁追（SIM 已上线；占用 OKR C1）

**触发**: 用户盘中令「核查记录和影子数据；当前入场大部分就亏损，需要改善」→ 取证后用户批准「批准 SIM 启用」。

**盘中取证**:
- 今日已平 5 笔：2 胜 +$26.58 / 3 负 −$317.86，净 **−$291.28**；两笔大亏同型。
- `US.LITE` SHORT 09:44、确认窗顺向 **+1.068%**、执行锚比计划锚低 **340bps**、MFE 0.03% → `SL_INITIAL` −$149.47。
- `US.NBIS` SHORT 09:41、确认窗顺向 **+1.412%**、执行锚低 **265bps**、MFE 0.52% → `SL_INITIAL` −$147.97。
- 30 日：`FADE_SHORT` 开盘 09:35–09:45 n=23 净 **−$446.69**；开盘+计划锚偏离>200bps n=11 净 **−$549.34**；确认门已确认 SHORT 中 `opp_cost_pct>=1%` **2/2 全亏 −$297.44**，0.6–1.0% 档 3/3 全赢。
- 影子/记录本身正常：TCA 4/4 执行落盘；入场确认门正常（COIN/AVGO 被弃）；pullback/trigger 遥测在写；无 ERROR/CRITICAL。

**落地** (`docs/decisions/RFC-153-fade-short-opening-chase-cap.md`):
- `entry_confirm_gate.py`: `FADE_SHORT` + 开盘 15 分钟内 + 确认点 `opp_cost_pct >= 1.0%` → 直接 `ABANDONED`（不进 grace），cause=`fade_short_chase_cap`；只作用于 FADE_SHORT，PULLBACK_LIMIT/BREAKOUT 零改动。
- 配置：`entry_confirm_fade_short_chase_cap_enabled: true` / `_max_opp_pct: 1.0` / `_window_min: 15`（一行回滚）。
- 前瞻行新增 `entry_archetype/abandon_cause/chase_cap_hit/chase_cap_threshold_pct`；`skip_reason` 仍为稳定契约 `entry_confirm_reversed`（日报/PlanEffectiveness 分母口径不变）。
- 顺带修复 RFC-152 TCA：成功成交行不再携带前几轮拦截的旧 `skip_reason`（改记 `prior_skip_reason`；10-20 RIVN/LITE 实锤）。

**验证**: 门/TCA/下游口径/影子治理/四棘轮定向回归 **214 passed**；`py_compile` 通过；生产配置实读 `cap=True / max_opp=1.0 / window=15`；启动冒烟 **7/7**。

**上线**: 2026-10-20 22:13 CST `systemctl restart ftseek-us`，PID **2689306**；重启前 4 个在仓（ASML/MSFT/NVDA/RIVN）、无在途 exit_intents；重启后无 ERROR/CRITICAL/Traceback。今日开盘窗已过，**首次实际裁决在下一交易日 09:35–09:45**；下一次 arm 的 metadata 将带 `entry_archetype/fade_short_chase_cap_*` 字段。

**预注册裁决**: 自 2026-10-21 起 ≥10 ET 交易日且 ≥30 笔 FADE_SHORT 开盘样本；主判据 = `SL_INITIAL` 笔数较基线下降 ≥50% ∧ 扣费净 bps 不恶化 ∧ 误杀净额 < 避免亏损净额（用 `_POST_WATCH` +5m/+10m 统计）。任一不满足 ⇒ 回滚并落档。

**回滚**: `entry_confirm_fade_short_chase_cap_enabled: false` + 重启（数据保留）；`git revert` 本批 + 重启。**提交状态**: 本批未提交（等用户确认）。

## 2026-10-20 盘前推送「跳过」段去噪（用户：建议部分重复太多）

**触发**: 用户贴 10-20 盘前推送，`跳过:` 一行把 51 只 `非当前通道候选(bridge未提名/无方向)` 逐只铺开（实测 1648 字符），信噪比低。
**落地** (代码 `app/playbook/feishu_push.py` `_format_morning_message`): 跳过段改为**按原因类聚合** — 取 `skip_reason` 首个 `(` 前的前缀归并；同类 ≥8 只只给 `×计数`（不列明细，占位候选自动折叠）；<8 只（多为真实门拦截：entry_confirm_reversed / chase_cap / vwap 等）保留逐只 symbol，让可 action 的拦截浮上来。**纯渲染层，零交易/风控/下单行为变更**。
**验证**: `tests/test_playbook_feishu_push.py::test_morning_skip_grouped_by_reason`（新增）+ 全文件 **19 passed**；真实 10-20 数据干跑预览：51 只占位 → 一行 `• 非当前通道候选 ×51`，1648→聚合后数百字符内。
**生效**: `feishu_push.py` 在主服务进程内 ⇒ 需重启（本次重启时点为 10-20 盘前 09:19 ET，6 笔在仓、exit_intents 非 BOOKED=0，改动仅推送渲染）。回滚: `git revert` 本提交。

## 2026-10-20 RFC-152 Phase 0：执行 TCA 台账 + 点差/报价年龄只读影子（已上线；实盘零行为变更）

**触发**: 用户「同意，按照建议执行」— 先做“一杆秤 + 点差门”，量化成交层 implementation shortfall，再谈是否加盘中指标。

**落档**: `docs/decisions/RFC-152-execution-tca-spread-shadow.md`（预注册样本门/判据/回滚；明确 Phase 0 不占 OKR-2026-10 C1 新入场假设名额）。

**落地**:
- 新增 `app/playbook/execution_tca_shadow.py`: 每笔通过全部既有闸门、真正走到 `execute_instruction` 的指令，快照计划锚/重定价锚/到达双边报价/点差(bps)/报价年龄(ms)/成交价，执行后 append `data/shadow/execution_tca.jsonl`。`book proxy` 不伪造点差；`TEST` 标的零写入；所有异常只 warn，不上抛。
- `auto_execute_flow.py`: entry_confirm 门之后、下单调用之前快照，执行后收口；全块 try/except，零执行分支改动。
- 配置: `playbook.execution_parameters.execution_tca_shadow_enabled: true` + `execution_tca_spread_block_bps: 20.0` + `execution_tca_quote_age_block_ms: 2000`（两个阈值仅 jsonl `provisional_would_block` 标签，**不是生效门**）。
- 新增 `scripts/execution_tca_report.py`: 只读 JSONL + SQLite(mode=ro)，输出点差/年龄分桶 × 扣费净 bps + 日聚类 t/MDE80 + implementation shortfall；产物 `data/shadow/execution_tca_latest.json`。
- 影子注册表: `execution_tca_spread_shadow`（OBSERVING, min_n=30, count_mode=`where:success=True`, eval_on_demand）。
- 棘轮清剿（前序 P0/P1 批带入的红点）: `bundle_builder.py` 2 处坏 JSONL 行、`breakeven_shadow_settle.py` 1 处坏 bar 时间、`be_grace_replay.py` 2 处静默吞错 → 全部加留痕/坏行计数，零控制流变更；`except_pass` 921 基线回绿。

**验证**: 新增 10 tests passed；定向回归（执行链+影子治理+四棘轮）**287 passed**；`py_compile` 通过；报表空库冒烟写出 `gate NOT YET`；实读配置 `enabled=True / spread=20.0 / age=2000`；`check_mounting / check_artifact_consumers / check_intent_uniqueness` 全 0；启动冒烟 **7/7**。

**上线**: 2026-10-20 18:04 CST `systemctl restart ftseek-us`，PID **2641637**；重启前 3 个在仓（ADSK/ASML/TSM）、`data/exit_ledger.db::exit_intents` 201 条全 BOOKED；重启后无 ERROR/CRITICAL/Traceback。

**下一步**:
- 让影子自然采集：≥10 ET 交易日且 ≥30 笔成功成交后跑 `PYTHONPATH=. ./venv/bin/python scripts/execution_tca_report.py` 出第一批读数。
- Phase 1（点差门真正生效）须另立 RFC + 用户批准，并占用 OKR-2026-10 C1 的新入场假设名额；本批不生效。
- RS / ORB+ATR 衰竭 / EventStream 风险 overlay 均未开工，按讨论顺序排队。

**回滚**: `execution_tca_shadow_enabled: false` + 重启（数据保留）；或 `git revert` 本批 + 重启。**提交状态**: 本批与前序 P0/P1 批均未提交（等用户确认后提交）。

## 2026-10-20 P1 计划一致性 + EXT 暴露影子（已上线；实盘零行为变更）

**触发**: 用户「同意 按照建议执行」— 在 P0 测量修复后继续执行源/本质层优化。

**落地**:
- **DA 计划一致性遥测**: `instruction_builder._confirmation_gap_pct` + `[PlanGap]` warning，`raw_instruction_json.confirmation_gap_pct` 落库。10-19 四笔 >1%：COIN +2.22% / RIVN +4.05% / ARM +2.72% / UNH −2.27%，与 `entry_path=chase` 的实质偏差一致；其余 4 笔 ≤0.78%。日报「计划 vs 实际」新增「计划价位偏离>1%」行，10-19 复算 4 笔。
- **重锚（受路由总开关保护）**: `entry_routing.resolve_route` 新增可选 `entry_price`；`entry_archetype_reanchor_enabled: true` 时，确认位与执行锚偏离 >1% 则用执行锚作为限价位，返回 `reanchored`。**`entry_archetype_routing_enabled` 仍为 false ⇒ 实盘零行为变更**；`auto_execute_flow` 已传入 `entry_price`，待路由转正后自动生效。
- **EXT 研究臂只读影子**: `late_premarket`（≥09:00 ET 盘前）落 trade metadata/result/attempts；`eth_stage_daily_report` 新增「④ 研究臂暴露（按 sector）」与晚入场提示。10-19 暴露：Semiconductors 3（NVDA/TSM/MU）、Unknown 2（ASML/INTC）、Consumer Defensive 1（PEP）、Industrials 1（BA）；**只读，不加帽/不拦单**。
- BE 减半 D / 宽限窗 / fee-bps 读数见下一条 P0 条目的「P1 研究读数」。

**验证**: 本批定向 **64 passed**；`py_compile` 通过；`eth_stage_daily_report --date 2026-10-19` 实测新节；10-19 gap 分布复算。

**上线**: 2026-10-20 12:45 CST 首次重启（PID 2596187）；12:48 CST 为日报 plan-gap 渲染二次重启，启动冒烟 **7/7**，PID **2596964**；重启前 4 个在仓（ADSK/ASML/TSM/BA）、exit_intents 200 全 BOOKED；重启后无 ERROR/CRITICAL。

**下一步**: D 减半影子等 2026-10-21 起前瞻 n≥30（保守口径）再裁，转正需 DEB + Playbook 部分平仓执行改造；重锚只在限价路由转正后生效，当前先观察 `PlanGap` 分布；EXT 源并入严格臂须等 20 日观察（至 11-13），行业集中度先只读。**本批与 P0 批均未提交**（等用户确认后提交）。

## 2026-10-20 10-19 全链路复查后的 P0 测量修复（纯测量，已上线）

**触发**: 用户令复查 2026-10-19 美股全链路；发现三处“尺子”不一致（日报执行率 7/7 vs PlanEffectiveness 7/8；entry_confirm 结算混入 EXT 研究臂；日报 headline 未分账研究臂），用户裁「按建议执行」。

**落地**:
- `scripts/settle_entry_confirm_forward.py`: PnL/机制队列新增 `EXCLUDE_RESEARCH_ARM_SQL`（`gate_mode=research_momentum`），只排除 10-19 新研究臂，**保留 strict EXT**（基线同尺，避免破坏 8/1–10/9 冻结基线复现）。10-16 起队列复算 **11 笔 / SL_INITIAL n=0 / 非 SL +$11.06/笔**；原报告 NVDA −$75.50 被误计入 SL_INITIAL 的假信号消失。
- `app/review/bundle_builder.py` + `bundle_render.py`: 计划分母纳入 `skip_reason='entry_confirm_reversed'`；10-19 由 7/7=100% 修正为 **计划 8 / 执行 7 / 2 分钟门放弃 1（AVGO）/ 87.5%**。新增「研究臂分账」与「入场路径/出场后路径（影子）」两节；10-19 复算显示研究臂 4 笔 +$53.18 / 其余 7 笔 +$77.75；entry chase 5 / pullback 3；post-exit close_gap 均值 +0.83%、>1% 3 笔（ARM/COIN/CRM）。
- `app/review/stage_stats.py`: `load_trades` 带出 `metadata_json`，供只读分账。
- `scripts/extended_hours_entry_executor.py`: `_write_open_position` 补落 `research_source`（此前只有 gate_mode，导致日报来源 unknown）；纯 metadata，无交易行为。
- 存量回填: 10-19 的 7 笔 EXT（ids 1681/1682/1683/1691/1692/1693/1694）按 `data/ext_entry_research_funnel.jsonl` 的 ordered 源回填 `research_source`（ASML/TSM=beat，PEP/NVDA/MU/INTC/BA=watchlist）；备份 `data/backups/research_source_backfill_20261020_113831.json`。
- 新增测试: `tests/test_review_bundle_plan_abandoned.py`、`tests/test_review_research_arm_split.py`、`tests/test_review_shadow_paths.py`；`test_entry_confirm_gate.py` 增研究臂排除例；`test_ext_admission_attribution.py` 增 research_source 落库例。

**验证**: 定向回归 **130 passed**（结算/EXT/日报）；新增影子路径测试 **3 passed**；最终日报回归 **22 passed**；`py_compile` 通过；生产库只读复算 10-19 分账/执行率；`render_md` 10-19 实测新节输出正确。

**上线**: 2026-10-20 11:42 CST `systemctl restart ftseek-us`，启动冒烟 **7/7**，PID 2586102；重启前 4 个在仓（ADSK/ASML/TSM/BA）、`exit_intents` 200 条全 BOOKED；重启后无 ERROR/CRITICAL/一致性告警。

**P1 研究读数 (2026-10-20, 未改实盘)**:
- BE 宽限窗 (5/10/15m): `scripts/be_grace_replay.py` 161 笔，N=10 主判据 **FAIL**（均值 +$3.80 / 中位 −$8.32 / 胜率 27.3% / 日聚类 t=1.48 / 剔 top3 +$0.15）。10-19 的 ARM/COIN 是尾部样本，不具普遍性；**不采纳**。
- Fee-bps 一刀切: 145 笔中 >20bps 组 7 笔净 **+$145.83**，>30bps 组 4 笔净 +$23.22；堵高费率会伤赢家，**不采纳**（既有 notional 影子维持）。
- BE 武装点减半 50%: `breakeven_shadow_settle.py` 新增 **[D]**，历史 161 笔原始读数 +$8.25/笔 / 中位 +$5.97 / 胜率 96.3%（日 t=9.21）；**保守口径**（武装点 10bps 不利滑点 + 二次出场费按实际费 25%）仍 **+$3.44/笔 / 中位 +$2.15 / 胜率 72.0% / 日 t=4.71 / 剔 top3 +$2.85**，8/9/10 月同向（+5.61/+2.85/+1.86）。前瞻样本 n=0 / **KEEP_SAMPLING**，不改实盘；需 2026-10-21 起 ≥30 笔前瞻，且 Playbook 当前 close 路径不支持部分平仓，转正须另立 DEB + 执行层改造。
- 产物: `data/shadow/be_grace_replay_latest.json`（宽限窗 FAIL 留档）、`data/breakeven_shadow_settlement.json` 的 `D`（前瞻减半影子）。
- next-open 稳健性检查（一次性）: 59 笔可用样本（很多 BE 在下一根 bar 前已出场，无法在 next-open 减半）保守 +$6.05/笔、中位 +$4.86、胜率 94.9%；未写入每日 D，作为 arm-close 保守口径的旁证。

**未做（需另裁/另批）**:
- P1 入场对齐：DA `entry_price` vs `confirmation.price_level` 分叉（10-19 5/8 `entry_path=chase`；三笔空单成交在确认位下方 2.2–3.9%）；`pullback_limit_shadow` 10-19 成交率 3/8，未达预注册门槛，**未开** `entry_archetype_routing_enabled`。需先定“哪个价格是真值”+影子样本。
- P1 出场条件化：10-19 post-exit giveback +$425，但 648 笔基线 BE/trail close_gap 均值 −0.119%/−0.395%，不支持全局放宽；需预注册 ATR/趋势条件变体并走 `exit_param_shadow`/`be04`。
- EXT 研究臂盘前时点闸（NVDA 09:15）、fee-bps 准入、pretrade 卡片 0/66、quality_shadow 常量退化、ASML overnight_arm 缺失、盘前计划截断等 P2/P3 记录卫生。

**回滚**: `git revert` 本批代码；存量 metadata 回滚 `UPDATE trade_records SET metadata_json=json_remove(metadata_json,'$.research_source') WHERE id IN (1681,1682,1683,1691,1692,1693,1694);`（备份见上）。研究臂 PnL 过滤回滚 = 删 `EXCLUDE_RESEARCH_ARM_SQL` 条件并重启/重跑结算。

## 2026-10-20 记录卫生：EXT 未平仓 exit_time 归一 NULL + PlanEffectiveness 分母

**触发**: 用户令修复 10-19 交易复查发现的两项记录口径问题。
**落地**（提交 `aa1e5230e`，未推送）:
- `scripts/extended_hours_entry_executor.py::_write_open_position`: 未平仓 `trade_records.exit_time` 由空串改 **NULL**；存量 3 行（id 1681 ASML / 1691 TSM / 1694 BA）已 `UPDATE` 为 NULL。`SELECT ... WHERE exit_time IS NULL` 现正确返回 4 个在仓（ADSK + 3 EXT），不再漏 ASML/TSM/BA。
- `app/analysis/plan_effectiveness.py`: `planned` 分母纳入 `triggered=1 AND skip_reason='entry_confirm_reversed'` 的 2 分钟门放弃目标；新增 `abandoned_count / abandoned_symbols / execution_rate_basis`，日志打印 `traded/abandoned/basis`。10-19 实跑由 7/7=100% 修正为 **7/8=87.5%**（AVGO abandoned）。
**验证**: 新增 3 例契约测试（PlanEffectiveness 2 + EXT exit_time 1）；本批相关回归 **60 passed**；`py_compile` 通过；生产库只读复算 `planned=8 / traded=7 / abandoned=1 / traded_rate=87.5%`。
**上线**: 2026-10-20 08:40 CST `systemctl restart ftseek-us`（PID 2554551），启动冒烟 **7/7**；重启前实查 4 个在仓、`exit_intents` 200 条全 BOOKED、无在途出场。
**已知非本批**: `tests/test_control_loop_ledger.py::test_ledger_job_ids_are_real` 报 `bd564dae37b0` 幽灵 job_id（台账与活体 jobs.json 脱钩）；本批未触碰台账/job，未处理。
**回滚**: `git revert aa1e5230e`；存量 3 行如需回滚 `UPDATE trade_records SET exit_time='' WHERE id IN (1681,1691,1694)`；零交易策略/阈值变更。

## 2026-10-20 RFC-151 一致性校验 P2 假阳修复（用户令修复，已上线）

**触发**: 10-19 复核发现的 P2 —— `⚠️ [US Sync Consistency] Memory 缺少 Broker 持仓` 贯穿全天刷屏（盘后实测仍每 5min 一条）。
**根因（口径漂移）**: `_verify_and_sync_capital` 的 Memory 分支用 `open_positions.owner_strategy='playbook_sim'` 构造排除集，而 sync 循环（L1701）用 `playbook_open_check`（判据 = `trade_records.source_type IN (PLAYBOOK_SIM,PLAYBOOK_LIVE)` 且未平）。EXT 盘前/盘后入场仓的 `owner_strategy='extended_hours_entry'`（trade_records 仍记 PLAYBOOK_SIM）⇒ 排除集漏这一级。现场自相矛盾：同一轮 `Done | Futu: 4 | playbook自管: 4 | 一致性: ✓` 紧接一条 Memory 缺失告警（ASML/TSM/BA）。
**落地** (提交 `609d30fea`, 未推送):
- 删内联 `owner_strategy='playbook_sim'` 查询（16 行）→ 改 `load_playbook_open_symbols()` + `playbook_open_check`（查询失败回退逐 symbol `is_playbook_open_position`，保留 fail-closed）。**未采用「补 OR extended_hours_entry」**：会给第二套判据再打补丁，下个新通道再漏一次。
- 同函数整段 `except` 由 `debug` 升 `warning(..., exc_info=True)`：debug 吞异常会让「误报消失」与「整段校验已死」在日志上无法区分（假绿）。
- 新测试 `tests/test_sync_consistency_memory_exclusion.py`（5 例，双向：不刷屏 / 真缺口仍报 / 预载回退 / 无 owner_strategy 字面量 / 异常可见）。
**验证**: 定向 **5 passed**；回归面（coordinator + playbook m2 + rfc080 + open_row_pnl + pending_sync + backfill_clock + last_sync_at）**336 passed**。活体：`07:02:44 CST` 重启（PID 2538486 线程在 L676 休眠位），启动对账 + 之后 ≥2 个 300s 周期，`US Sync Consistency` **0 条**、`校验异常` **0 条**、ERROR/CRITICAL **0 条**；`load_playbook_open_symbols()` 实测返回 {ADSK,ASML,TSM,BA} ⇒ `futu_set_for_mem=[]`。
**回滚**: `git revert 609d30fea` + `systemctl restart ftseek-us`。零交易行为影响（只发日志）。
**当日实况（ET 10-19 收盘后）**: 已了结 11 笔，毛 +$186.18 / 费 $55.25 / **净 +$130.93**（PEP PRE_CLOSE +106.64、INTC +11.40、MU +10.64 为盘后了结）；持仓 4（ADSK/ASML/TSM/BA，均 playbook 自管）；DB 净值 $99,221，回撤 -0.5% 🟢。

## 2026-10-19 财报日历刷新降噪（用户同意）

**触发**: 日志每约 50 分钟重复 `获取财报日历 (horizon=3month)` 311 条；确认是 SignalScanLoop 每 10 轮 `force_refresh=True`，绕过 EarningsCalendar 的 1h 缓存。
**落地**（代码已提交 `7c1e3e5d7`；主服务需重启）:
- `signal_pipeline`: `_EARNINGS_REFRESH_INTERVAL` 10→12（盘前 5min×12≈60min）；去掉 `force_refresh=True`，由 1h 缓存控制实际拉取。
- `earnings_calendar`: 增加内容指纹（symbol/reportDate/timeOfTheDay/estimate）；首次/内容变化 INFO，缓存命中/内容不变 DEBUG；`获取财报日历` 降到 DEBUG。
- 未改数据源、7 天双窗口、全量 310 只覆盖或财报门逻辑。
**验证**: 财报日历相关定向 **18 passed**；全量 **7382 passed / 72 skipped / 16 failed** — 16 条来自 HEAD 里另一 agent 的减法/棘轮批（W9/W456/死键/crontab/D4/I10/except_pass/naive_datetime 等）及 2 条存量数据红，与本财报改动无关；本批未触碰这些文件。
**生效**: `app/data/earnings_calendar.py` + `app/pipeline/signal_pipeline.py` 在主服务进程内 ⇒ 需按军规重启。
**上线**: 2026-10-19 19:53 CST 已重启，PID 2425002，启动冒烟 7/7；`US.ADSK`/`US.ASML` SIM LONG 保留，无非 BOOKED 出场意图。

## 2026-10-18 减法修复批（存储/棘轮/死代码）

**触发**: 用户令「按建议执行修复」— 基于 2026-10-18 减法审计（重复/冲突/过期/性能）。
**落地**:
- 存储: `scripts/db_backup.sh` 对 event_stream.db **备份副本**剥离 `raw_events.raw_artifact` 后 VACUUM（真库不动），避免 1.4GB × 7 天全量放大；`scripts/data_retention.py` replay_cache 默认 90→30 天，已 `--apply` 删除 276 个过期文件（147.4MB）。event_stream 保留窗维持 14 天（10-15 用户裁决；10-23 前无行可清）。
- 守卫: `tests/test_pipeline_size_ratchet.py` BASELINE 10,207→3,700（实际 3,670；原余量 64% 已失效），同步收紧防上调钉子。
- 死代码: 第一批删零引用 `app/plan/plan_feedback.py`、`app/utils/retry.py`、`app/catalyst/detector.py`；第二批删 `app/analytics/*`、`app/review/attribution_scheduler.py`、`app/observability/failure_tracker.py`、`app/execution/greeks_calculator.py`、`app/playbook/entry_reference.py` 及对应测试；删 9 个 `_tmp_*` 脚本 + `_dump_except_bodies.py`、`config/deprecated/watchlist_us.json`；`app/catalyst/__init__.py` 文档同步。`scripts/_tmp_adf_check.py` 被 `ops/hermes/adf_weekly_check.sh` 与测试引用，已恢复，不删。
- 卫生: 删除 `data/da_checkpoint/**/*.bak` 466 个（73.7MB，均有对应 base 文件）、1 个脚本 bak、3 个 `.DS_Store`。
**未做（有意）**:
- `.env.us` 死键批：审计清单经核对含活跃读者（如 `TIME_STOP_HOURS`、`SIGNAL_*`），不做批量删除；留待跨 `.sh/.service/Hermes` 精确批。
- 85 个 orphan 配置叶、245 条注释 cron、双 carrier、影子注册表 20 项无 eval、日亏权威、净口径、手动平仓门：需 DEB/用户裁决，未动。
- event_stream 14→5 天：保留 10-15 用户裁决；备份剥离已解决放大。
**验证**: 定向回归 **317 passed**（ratchet/retention/dead-db/shadow-chain/archive-import/trade-storage/execution/coordinator）；`bash -n scripts/db_backup.sh` + event_stream strip SQL 小样通过；`app.review/execution/playbook/observability/catalyst/plan/utils` 导入全 OK；已删模块零引用复查通过（archive 证据除外）。
**无需重启**: 改动均为测试/脚本/死代码/卫生项，未触 Playbook/exit/config 热路径；下一 03:00 备份与 04:20 retention cron 自动生效。
**回滚**: `git revert` 本批；备份脚本删 event_stream 特殊块；ratchet BASELINE 恢复 10,207（需 DEB）；死代码从 git 恢复。
**下一步**: 精确死 env/config 跨载体批、da_checkpoint 之外的 `.bak` 归档、双 carrier/影子注册表逐项裁决。
**追加 (2026-10-18, 第二批)**:
- `.env.us` 精确死键: 删 **9 个零读者键** (6×`BAILIAN_MODEL_*` 角色模型 + `DEFAULT_DATA_SOURCE` + `TRADING_STYLE` + `WATCHLIST_SYMBOLS`)；`DEEPSEEK_*` / `DATABASE_URL` / `FTSEEK_DEBATE_*` 因仓外 Hermes/archive 读者保留。`config import OK`，无需重启 (死键无读者)。
- 影子注册表: 复核后修正上一轮误判 — `entry_anchor_dual` 数据实际存在（9704 行 / 1601 dual_anchor 行），`current_n=0` 只是静态字段未 refresh；已补 `no_adjudication_reason` 防误标 READY。`eth_short_probe` 退役（KILLED；cron 已于 10-06 删除、产物 9/26 冻结、无消费方）。`futu_channel_iv_hv`（已停采）/ `ticket_size_breakeven`（read-only 按需）加 `stale_expected` 豁免预期超龄；`short_chase_ban_shadow` / `ext_entry_exec_price_shadow` 补 NO_EVAL_SCRIPT 挂账。`check_mounting` 告警 **5→0**；`overnight_gap_slcap_shadow` 载体重跑 (post-ca4487 10/09–10/16, n=33, `reason_match=0.606<0.80`) 后按用户裁 **退役 (KILLED, collection_retired=True)**，slcap 变体冻结、数据保留；专用产物 `data/exit_replay_slcap_latest.json`，共享 `exit_replay_latest.json` 已恢复 8/11 全窗产物，避免污染 `replay_engine_be_fidelity.py`。`p1_1_vwap_slope` 的终态产物消费方 `gate_block_counterfactual.py` 已按终点报表补 `_CONSUMER_ACK`。`eth_held_exit_shadow` / `ext_entry_earnings_criterion_shadow` 仍待补 eval 或人工结算。
- 验证 (影子批): 102 passed（mount sentinel / eth probe / artifact consumers / adjudicated note / ticket size / rfc139 / shadow board）。
- `entry_anchor_dual` 阶段0 只读结算器: 新增 `scripts/entry_anchor_dual_eval.py` + 2 例契约测试；读数 9704 行 / 1601 anchor / 102 inst / **8 交易日**，缺失率 0%、分歧率 44.6%、护栏触发 41.3%；周期门 8/10 未达 → `PERIOD_GATE`，`eval_on_demand=True`，不误标 READY；输出 `data/shadow/entry_anchor_dual_eval_latest.json`。质量腿仍缺生成锚反事实 PnL，脚本明确标 `NOT_COMPUTABLE_COUNTERFACTUAL`。
- 上线: 2026-10-18 22:35 CST `systemctl restart ftseek-us`；启动冒烟 **7/7** (ConfigValidator/漂移/热重载/SSOT/双臂/快循环/零 Traceback)。重启前实查：仅 US.ADSK 一个模拟持仓，exit_intents 189 条全 BOOKED，无在途出场。
- 二轮重启 (slcap 载体 + entry_anchor_dual eval 落盘后): 2026-10-18 22:43 CST，启动冒烟 **7/7**，PID 2241986；重启前同一状态 (ADSK / 189 BOOKED)。
- **提交**: `6a3fa2bdf` — `[hotfix] 减法修复批: 存储/棘轮/死代码/影子注册表` (未推送; `.env.us` 为 gitignored 运行机改动, 未入提交)。
- 退役后最终重启: 2026-10-18 22:48 CST，启动冒烟 **7/7**，PID 2243763；线上加载 slcap KILLED + entry_anchor_dual PERIOD_GATE。
- 第三批影子读数 (2026-10-19 09:39 CST, 未提交): `eth_held_exit_shadow` 按 10-15 追认改为 **PERMANENT_MEASURE 移交失败监测面** + `stale_expected`（删除过期 NO_EVAL_SCRIPT 挂账）；`ext_entry_earnings_criterion_shadow` 新增 `scripts/ext_earnings_criterion_eval.py` + 契约测试：**6674 行 / 18 ET日 / 522 legacy_blocked_new_pass**，EXT 成交自 09-23 仅 **5 笔**（均 +$15.82）⇒ 判据①样本门未达，登记 `SAMPLE_GATE`；台账轴与成交轴分离。`check_mounting` 0；105 tests passed。
- 第三批读数后重启: 2026-10-19 09:39 CST，启动冒烟 **7/7**，PID 2326756；重启前仅 ADSK 一个模拟仓，exit_intents 189 条全 BOOKED。
- 第四批影子复核 (2026-10-19, 未提交): `short_chase_ban_shadow` 落 **BLOCKED_DENOMINATOR_SPLIT** — W7 2026-10-16 `divergent_n=128` 未修；命中仅 **13 distinct inst <20**（227 行 / 8 笔匹配净 -$17.3），扫描 85 行 / 62 inst ⇒ 10/31 前先裁分母或到期退役，**不建结算器**。registry 仅 cron/子进程消费，本轮未重启；钉子口径 2→3，**127 passed**。
- 第五批退役裁决 (2026-10-19, 未提交): 用户裁「退役, 基于合理性」⇒ `short_chase_ban_shadow` **KILLED**：`unified_config.yaml` 的 `short_chase_ban_shadow_enabled` 置 **false 真停采**；注册表 `collection_retired=True` + verdict_history `KILLED_REASONABLENESS`；数据保留，重开须先修分母 + 新预注册。钉子 3→4 + config 开关钉；**127 passed**；重启 09:51 CST 冒烟 **7/7**，PID **2329597**。
- 第六批读数 (2026-10-19, 未提交): `ext_entry_exec_price_shadow` 新增只读读数器 `scripts/ext_entry_exec_price_eval.py` + 契约测试；**1686 轮 / 15 天**，current_price_pool 中位 14 → shadow_execution_pool 中位 2，**中位缩减 86.4%**；prefiltered 19,352 条 / 995 symbols；实际 EXT 成交 5 笔全 >$50、**低价成交 0**、交集 0；登记 `READOUT_ONLY`，11/30 到期人工裁维持/退役。`check_mounting` 0；**128 passed**；只读离线，未重启。
- 第七批读数 (2026-10-19, 未提交): `da_true_reverse_shadow` 阶段0健康核对: **4 events / 4 unique event_id**（TSLA 9/23、ORCL 9/29、UMC 10/5、SKHY 10/16），artifacts **4/4** 存在、checkpoint 含 `_ic_decision_id`，cron logs 10/07-10/16 无 error；登记 `PHASE0_READOUT` + `stale_expected`（事件稀疏≠断流），Phase1 enforcement 待用户裁；**136 passed**；未重启。
- **提交**: `4dbd8b26c` — `[hotfix] 影子读数: ext exec price $50 + da_true_reverse 阶段0`（已推送）。
- 第八批配置减法 (2026-10-19, 未提交): 删除 7 个零消费叶 — `pipeline.market_context.capital_safety` 5 键（仅 YAML 自引用；现役水位读 `risk.capital_safety.water_levels`）+ `database.backup_enabled` / `backup_interval_hours`（备份由 `scripts/db_backup.sh` 独立调度）。权威 strict `ConfigValidator` **0 error / 0 warning**；`config_validate.py` 的 348 未用/135 缺失是存量漂移，不含本次 7 键；census 仍显示 65 orphan 因扫描面含历史 `.bak`，live YAML 已无这些键。死键无读者，未重启。
- 第九批 crontab 减法 (2026-10-19, 非 git): 删除 **26 条被注释掉的完整调度行**（dead jobs）；活动行 **124→124 逐字等价**，总行 394→368，解释性注释保留；备份 `data/backups/crontab.backup.20261019_subtraction.txt`；未重启。
- 第十批卫生减法 (2026-10-19, 非 git): 删除 `data/*.db` 零字节占位 **12 个**（无数据；event_stream/ftseek_us 等非零库未动）；未重启。
- 第十一批 carrier 审计 (2026-10-19, 只读未改): `error_alert` 4 条 cron + 4 个 Hermes no_agent carrier 同窗；Hermes 载体正常静默（非双推），executions.db 显示 completed/error=0；双执行约 340 次/周。**裁决建议=保留双侧**（告警冗余价值 > 执行成本）；候选删 4 条 cron 行（active 124→120，等价）已备未安装。`insider` cron 仅写日志，Hermes carrier 负责投递；建议同样保留双侧。
- 第十二批备份归档 (2026-10-19, 非 git): 40 个旧 crontab 快照 + 8 个旧 config 备份 → `data/backups/config_crontab_archive_20261019.tar.gz`（256KB / 48 members）；保留最新 5 crontab + 3 config；原件删除；未重启。
- 日亏权威与净口径审计 (2026-10-19, 只读未改): `circuit-breaker-map.md` 与六权威登记已 10-15 刷新、阈值核对一致，无需改文档；净 PnL 双实现横跨 30+ SQL 站点、当前 delta=0，统一属 DEB 级测量重构，未动。
- 第十三批退役 (2026-10-19, 已提交): 用户令「按合理性和建议执行」⇒ `ext_entry_exec_price_shadow` **KILLED**：1686 轮/15 天，$50 门槛中位缩减 86.4%，实际 EXT 成交 5 笔全 >$50、低价 0、交集 0；crontab 两行内联 `EXT_ENTRY_EXEC_PRICE_SHADOW` 改 **false 真停采**；`_CONSUMER_ACK` 认领 executor 的 inert 写方引用；数据保留；重开=新预注册；**136 passed**。
- 本轮重启 (退役批): 2026-10-19 11:06 CST，启动冒烟 **7/7**，PID 2341920；重启前仅 ADSK 一个模拟仓，exit_intents 189 条全 BOOKED。
- 裁决结论 (未改): 双 carrier 保留双侧（非双推，冗余价值 > 执行成本）；六套日亏权威保留分层（地图已准确）；净 PnL 双实现留 DEB 级单口径重构（当前 delta=0）。
- **提交**: `da86474a9` — `[hotfix] 减法批收口: 配置死键 + ext price 影子退役`（已推送）。
- 第十四批退役 (2026-10-19, 本批整体提交): 用户令按建议执行 ⇒ `ue_tier_gate` **KILLED**：写者仅挂 UE inject，10/18 DA 主链改造后 `scan --da-feed` 明确 skip inject ⇒ 10/05 后断供；10/19 scan 仍 surfaced 7 但无 tier-gate 行；被拦 fwd n=4<30；`universe_expansion.tier_gate_shadow=false`；eval cron 摘除（active 124→123）；`data_freshness_watchdog` 条目移除；`_CONSUMER_ACK` 认领 scan 的 inert 写方；ADJUDICATED recheck 清空；数据保留；重开=新预注册 + 恢复写入路径；**143 passed**；后续随本批重启（见下）。
- 全量回归 (2026-10-20): 首跑 **7389 passed / 17 failed / 72 skipped** → 修复本批回归（`eth_short_probe` 词典终态、error_alert crontab 注释、3 个 eval 脚本 R5/naive-now）→ 清理 7 项外部红后 **7406 passed / 72 skipped / 0 failed (7:41)**。外部修复：config 5 死键（catalyst.premarket 3 + observability.retention_days×2 + diagnostics.risk_free_rate）、control ledger `bd564dae37b0` 留痕豁免、event_stream 测试回看窗、except-pass 7 处留痕（empty_run_metrics 5 + entry_second_chance 2）、naive generation_flow 重锚、pending top_movers `needs_adjudication`、W4/W6 6 处补栈（push/health×2 + scheduler_triggers/generation_flow/main.py/refresh_daily_bars）。strict ConfigValidator **0/0**。
- 最终重启: 2026-10-20 10:47 CST，启动冒烟 **7/7**，PID **2576099**；重启前 4 个在仓（ADSK/ASML/TSM/BA），exit_intents 200 条全 BOOKED。
- **本批整体提交**（hash 见 git log；crontab 的 ue eval 摘除与注释修正属运行机改动，不在 git）。
- 未做: 剩余 orphan 叶按块保留 — `options/short_strategy` 有活读者，`exit_profiles/playbook_horizon` 属动态退出配置，`strategies/news/llm` 多为遗留或仓外消费；需 validator/CRITICAL_KEYS/仓外三面复核；245 条注释 cron、双 carrier、日亏权威、净口径、手动平仓门仍待逐项裁决。

## 2026-10-18 推送治理 P0+P1（心跳/复盘/回滚/日报降频）

**触发**: 用户令核查钉钉/飞书推送内容与链路信噪比；选择 **P0+P1**。
**落档**: `docs/decisions/RFC-150-push-governance-p0-p1.md`。
**证据**: 10/16 共 34 条推送，盘中主动心跳 13 + 通道心跳 5 + 服务启停 8 = 26 条（约 76%）低信噪/重复；04:00–04:35 CST 复盘集群连推 5 条；失败提案连续 4 天重复「自动回滚失败」；Hermes 盘后综合复盘的最终飞书回复与 `daily_alert_summary.py` 内部飞书推送同通道双推。
**落地**（代码提交 `e1a12983a`）:
- 盘中心跳 30→**60min** 可配置（`push.intraday_heartbeat_interval_min`，读取失败回退 60）；预期 13→约 7 条/日。
- 通道心跳跨进程共享：`data/push_health_state.json` 存业务成功证据 + `flock` 全局 ping 时隙；`push.health_check_shared_state` / `health_check_interval_sec` 可回滚。
- 复盘聚合：`ReviewOrchestrator.run_daily/run_weekly(push_report=False)`，日报/周报只由 `_push_review_text` 发一份；次日提示按 `top_risk/avoid_sessions/caution_symbols` 内容指纹去重（`data/.last_review_hint.json`）；Playbook 指令效果只落日志（已含在 Hermes 05:00 CST 综合复盘）。
- 自动回滚判据：`ReviewEffectTracker.get_pending_checks()` 只返回 `success=True` 且 `rollback_version` 非空的记录，并按 `issue_id` 去重；失败/不可回滚动作不再每天重复告警。`record_action()` 同 issue 未检查记录不重复追加。
- Hermes 同通道双推：`daily_alert_summary.py` 新增 `--no-dingtalk/--no-feishu/--no-push`；Hermes job `7a40dd7c9e94` Step 3 已改 `--no-feishu`（备份 `data/backups/hermes_jobs_backup_20261018_push_governance.json`）。
- P1 降频：DA 链路日检 `5 6 * * 2-6`→`5 6 * * 6`；LLM token 成本 `20 6 * * 2-6`→`20 6 * * 6`；延长时段阶段日报去掉 `--always`（无样本静默，恢复台账既有语义）。crontab 备份 `data/crontab.backup.20261018_push_governance.txt`。
**验证**: 新增契约 `tests/test_push_governance_20261018.py` 16 例；合并回归 24 个测试文件 **363 passed**；`ConfigValidator(strict_mode=True)` 全通过；`py_compile` 全通过。
**上线**: 2026-10-18 20:49:45 CST `systemctl restart ftseek-us`（PID 2220246），启动冒烟 **7/7**。重启前实查：仅 `US.ADSK` 1 笔在仓，`exit_intents` 无在途（`BOOKED` 189 条为历史已成交）。重启后日志确认 `💓 盘中心跳后台线程已启动 (interval=60min...)`；`data/push_health_state.json` 已记录钉钉业务成功证据。
**观察**: 下一交易日盘中心跳应约每小时 1 条且不再出现同小时多进程通道心跳；16:00 ET 复盘应只剩 1 条日报（次日提示仅内容变化时出现）；05:00 CST 综合复盘的飞书不应再与脚本告警汇总重复。周六 06:05/06:20 CST 应看到 DA 链路/LLM token 周报；延长时段无样本日应静默。
**未做**: 服务启停推送（10/16 8 条）与周度三份报表（周复盘/Playbook 周报/edge 周报）未合并，待观察后再裁；交易策略/阈值/仓位零改动。
**回滚**: 见 RFC-150 §5（配置改回/`health_check_shared_state=false` + 重启、git revert、crontab/Hermes 备份恢复）。

## 2026-10-18 财报日盘前研究臂源影子（仅研究臂，20 日观察）

**触发**: 用户问盘前财报日逻辑/链路是否可行；复核发现严格臂近期 0 单，主源是全市场异动榜（仙股多），财报超预期榜/自选股源未启用，严格涨幅带 0.5–2.0% 会挡财报大跳空。用户同意先影子观察。
**落地**（`docs/decisions/RFC-149-earnings-day-premarket-shadow.md`）:
- `scripts/extended_hours_entry_executor.py` 研究臂新增 `EXT_ENTRY_RESEARCH_SOURCES`：追加 Futu 财报超预期榜 + 自选股，**只进研究池**；严格臂源/闸门零改动。
- 研究源独立涨幅带 `0.5,10.0`（实源探测：beat 8 只在 0.5–2%、0 只在 2–10%，2–10 会全挡掉）、源头价≥$5、最多 80 只；源级漏斗 `data/ext_entry_research_funnel.jsonl`（selected/reject/ordered + source/reason）；研究真单补 held/attempt 复用检查；结果/attempt 带 `research_source`。
- `.env.us`: `EXT_ENTRY_RESEARCH_SOURCES=true` + 带/价/上限；旧 `research_momentum` 臂保持 `EXT_RESEARCH_ARM_UNTIL=2026-10-31`，新 source 用独立 `EXT_RESEARCH_SOURCE_UNTIL=2026-11-13`（10/19 起第 20 个交易日）。
- 严格臂 `EXT_ENTRY_CHG_BAND=0.5,2.0`、源开关、价格/量/财报/流动性/结构闸全部不动。
**验证**: 新增研究源/带/漏斗测试 3 passed；执行器/窗口回归 41 passed。仅 cron 新进程生效，**无需重启**。
**观察**: 20 个交易日按 source 看 selected→reject→ordered→成交→净 PnL；若仍无成交或净负，关闭研究源，不转正严格臂。回滚 `EXT_ENTRY_RESEARCH_SOURCES=false`（一个词，下一 tick 生效）。

## 2026-10-18 盘前/盘后链路空跑治理（安全修复 + 运维节流）

**触发**: 用户令核查盘前/盘后链路“是否值得优化和调整和提升，避免空跑”，复核后令按建议落地。
**落档**: `docs/decisions/RFC-147-pre-post-market-empty-run-fix.md`。
**落地**:
- **推送顺序**: `run_playbook_morning_push` 09:00 前不推；当日生成完成标记存在才推；09:15 ET 兜底放行（WARNING）。修复 10/16 08:47 先推 13 条、09:01 生成后又把其中 4 条降为 SKIP 且不补推的问题。
- **实盘单例**: `get_manual_order_service()` 加双检锁；修复 5 线程筛选池并发解锁实盘（10/16 一次 5 次 `实盘已解锁`）。
- **ETH 退出**: wrapper 先按 collector 同口径探持仓，无仓跳过子进程；探针失败 fail-open。新增 ET 窗口闸 `extended_hours_exit_cron_gate.sh`（工作日 04:00–20:05 每分钟，其余/周末 5 分钟），孤儿单/close_key 守卫保留。结果 JSON 增 `collector`/`open_positions_n`。
- **EventStream**: `--cadence` 分时采频（关键窗 07:30–09:35 ET 5m / 交易日 04:00–20:00 15m / 其余 20m）；仅全源 OK 写状态，失败下个 5m tick 重试；`--force` 绕过。
- **日K刷新**: 移出 AFTER_HOURS 主循环（原单轮 658s），改独立 cron 双季 04:05/05:05 CST + 脚本内 ET 16:00 守卫；执行级日门/失败重试不变；`data/replay_bars_daily` 加入 freshness watchdog（80h）。
- **降噪/指标**: 关停的 after-hours LLM 推荐不再由调度器打 INFO；周末 `llm_scan_state.json` 去重写；新增只读 `scripts/empty_run_metrics.py`（含 UE da_feed / 09:10 漏斗，2026-10-18 追加 da_feed surfaced→DA注入/借券→最终指令）。
- **未做**（需影子/DEB）: 09:00 只验证 bridge 候选、DA/UE 发车时刻/握手、峰群前移、复盘推送合一；策略/阈值/仓位零改动。
**验证**: 相关域回归（playbook/pipeline/extended-hours/event-stream/premarket/watchdog 共 38 个测试文件）**583 passed**；全量套件超出 10 分钟工具上限未跑完（定向+相关域已覆盖本批改动）。
**上线**: 代码提交 `5d3e3b2a6`；2026-10-18 16:23 CST `systemctl restart ftseek-us`（PID 2176670），启动冒烟 **7/7**。重启前在途：`trade_records` 仅 US.ADSK LONG，`exit_intents` 0 行。crontab 已备份 `data/crontab.backup.20261018_prepost_empty_run.txt` 并应用：ETH 窗口闸、EventStream `--cadence`、日K双季 cron。重启后实测 ingest 日志出现 `cadence_skip interval=20m`；周末 `llm_scan_state.json` 未再每分钟重写。
**观察/回滚**: 周一 09:00–09:15 看盘前推送是否等生成完成、是否出现 09:15 兜底；盘中看 ETH cron 窗口闸日志与无仓跳过；17:05 ET 后看日K cron 首跑；EventStream 关键窗应恢复 5m。回滚见 RFC-147 §5。
**追加 (只读, 2026-10-18)**: `empty_run_metrics.py` 新增 `[UE 09:10 → DA → 最终指令]` 漏斗。10/16 实跑：UE surfaced 3（TSM/ERAS/AN），wrapper 选 2（ERAS/AN）并排除 TSM，DA 实际因借券丢弃 ERAS/AN、重新分析并覆盖 TSM；最终 1 条（TSM BUY，origin=ue_fresh_event，UE 原 SHORT 翻转，未触发），**新增最终标的 0**。10/15：最终 2（AMD 覆盖/翻转、REAL 保持），新增 1。根因=wrapper 的 same-day 排除未传给 pipeline。
**裁决 (2026-10-18, 用户令「逻辑以 DA 为主，避免重复和冲突」)**: **停用 09:10 fresh-event 当日写库**。crontab 已摘除两行末尾 `&& da_fresh_event_batch.py --execute`（UE 09:10 scan 保留观察/confirm/次日供料）；DA bridge 推送去掉 `--wait-fresh-event`（09:12 直接推 DA 主链批次）；`da_fresh_event_batch.py` / `origin=ue_fresh_event` 保留供人工影子/回滚，不再新增。crontab 备份 `data/crontab.backup.20261018_fresh_event_retire.txt`；RFC-148 §0 记裁决，§4 方案 A/B 不再实施。**无代码行为变更、无需重启**；下一次 09:10 只有 UE scan，不再有第二套 DA 写库。
**Phase 0 二次限价回放 (只读, 2026-10-18)**: 37 笔 ABANDONED 全样本（1m bar 37/37 校准通过）→ 市价对照臂均值 **-0.56%/合计 -20.80%**；16 个“延后 3/5/10/15 分钟 × 0.3%/0.5%/ATR”组合全部为负，最好的 15 分钟+0.3% 均值 **-0.25%**。结论: **不实施变体 C**（二次限价只收窄亏损，没有正期望；2 分钟门保护有效）。工具 `scripts/entry_second_chance_replay.py` + `scripts/backfill_minute_bars_symbols.py` 保留；补的 15 只 1m bar 在 `data/replay_bars_1m_extra/`，未碰生产 45 票缓存。详见 RFC-148 §7。
**回滚**: 见 RFC-147 §5（推送/单例/ETH/ingest/日K 各自可单独回退；crontab 改动前已备份）。

## 2026-10-18 质量反事实报表（只读，是否值得改 DA 校准）

**触发**: prompt/输入同步后，量化“降级高分 LONG / 要求市场确认 / 只保留中分回踩”的历史反事实。
**落地**（代码已提交 `d1437ab0d`）:
- 新增 `scripts/quality_counterfactual.py`: 复用 quality_shadow 标签与执行净利，比较 all / exclude_high_score_long_downgrade / require_market_confirmation / prefer_medium_pullback / exclude_high_score_long；输出 fwd、net、PF、被排除数与漏掉赢家。
- 实跑（全量 1173 条）: all net **-6.61bps / -$2,345**；排除高分 LONG 降级组 net **-5.69bps / -$1,813**（排除97，漏掉净赢家 **+$1,547**）；要求市场确认 net **-5.62bps / -$1,810**（排除35，漏掉 +$184）；只保留中分+回踩 LONG（SHORT全留）net **+21.64bps / +$3,152**，但排除827、漏掉 +$9,124。
- 结论: 降级高分 LONG 有小幅净改善但会漏掉尾部赢家；中分+回踩是强 cohort，**不能直接当独立过滤器**；需组合规则 + 资金/仓位回放/影子验证，未改 DA 实盘阈值。
**验证**: counterfactual 单测 2 passed；无写库/LLM/下单。
**下一步**: 用组合级反事实（保留 SHORT + 中分回踩 + 降级高分 LONG）和 4–6 周影子裁决，而不是单维过滤。

## 2026-10-18 UE/DA prompt 同步优化（v2.15 / ue_brief_v7）

**触发**: 复查发现 event_meta/借券补查上线后，UE prompt、brief 版本和 DA 输入未同步。
**落地**（FTSeek 代码已提交 `04955b0fa`；外部 DA 另行备份补丁）:
- `PROMPT_VERSION` v2.15：规则10 明确 `UNKNOWN` 只是占位、不代表不可借，不得因缺借券数据跳过有催化的 SHORT；新增规则12 说明 `family/form` 是代码事件属性，不是方向/置信度结论。
- `BRIEF_TEMPLATE_VERSION` ue_brief_v7：event_meta family/form 注入 + 材料性排序/来源与事件族配额；LLM 文本移除数字 `mat`（仍留代码排序/审计）。
- event_meta 透传：证据索引 `event_meta` → surfaced `evidence_matched_event_meta` → `quality_shadow.event_family`；外部 DA feed 增加 `ue_event_meta`，SHORT 文案增加 `borrow_data_basis`。
- 多样性上限：`event_stream_clue_max_per_source=10`、`max_per_family=12`，防止 8-K 洪峰挤掉新闻/板块线索。
- 外部 DA 补丁：`/root/.hermes/.../pipeline.py`（备份 `pipeline.py.bak-ue-event-meta-20261018`，py_compile 通过）；不改 DA 评分/方向/过滤。
**验证**: UE/证据/版本定向 **428 passed**；全量 **7351 passed / 72 skipped / 4 failed**（3 条存量红 + `capital_allocator` 并发跑测假红，单跑绿）。
**链路健康基线 (只读)**: 最新 scan 10/16 `v2.14/v6`，surfaced SHORT 3（MATERIAL 1/UNKNOWN 2），event_meta 覆盖 0，`borrow_on_demand` 空；DA 10 条 HIGH5/MEDIUM5，`ue_event_meta` 0/10，borrow basis MISSING 10，gaps 9。下一交易日 scan 应为 **v2.15/v7** 且 event_meta/borrow 覆盖上升。
**未改**: 高分会 LONG 与 `evidence_gaps` 的实盘校准仍留影子（quality_shadow/report），未改 DA 阈值。
**生效**: UE scan/DA 都是按次进程；主服务无需重启。

## 2026-10-18 SEC_PROVIDER 影子适配器（复用现有 provider）

**触发**: 用户继续 P0 主源；检查发现 `sec_8k_provider` 已覆盖 8-K/13DG，`sec_form4_provider` 覆盖 Form 4，禁止另写一套抓取。
**落地**（代码已提交 `6cad19b34` + `8eb930d4b`；默认关闭）:
- 新增 `app/event_stream/sources/sec_provider.py`: 复用 `fetch_recent_8k` + `fetch_recent_13dg` → canonical events（source_id=`SEC_FILINGS_PROVIDER`，含 form/items/ticker）。
- `source_registry`: `build_default_registry(provider_enabled)` 开关；config `event_stream.sec_provider_source_enabled` 默认 **false**；true 时替换 raw `SEC_EDGAR_8K_ATOM` 并补 13D/G（二选一，不重复采）。
- 修 `sec_8k_provider` 13DG 解析：只收 SC 13D/G，丢弃 SC 13E3（going-private），并过滤旧缓存。
- 新增 `app/data/sec_form4_discovery.py`: getcurrent Atom 取 Issuer 条目 + 复用现有 `_parse_form4_filing` 解析 XML; 只保留 P/S、金额≥$50K、非 10b5-1; 每次最多 20 个 filing, 10min 缓存.
- `SEC_FILINGS_PROVIDER` 同时纳入 8-K / 13D/G / Form 4；仍由 `sec_provider_source_enabled` 一开关控制，默认 false.
- 新增 `scripts/event_stream_sec_compare.py`: raw `SEC_EDGAR_8K_ATOM` vs `SEC_FILINGS_PROVIDER` 只读对比（覆盖/URL 重叠/ticker/8-K item coverage）；周末实跑 raw 40 条 8-K vs provider 3 条 Form4，8-K 重叠需交易日再比。
**验证**: 事件流/Form4/对比工具定向 **50 passed**；全量 **7348 passed / 72 skipped / 3 failed**（均为存量红；本批仅新增只读工具）。
**下一步**: 交易日开 `sec_provider_source_enabled=true` 影子，比较 coverage/重复率/下游材料/延迟；Form 4 只在需要全市场发现时补 EFTS，不复用按 symbol 查询逻辑。

## 2026-10-18 quality_shadow v2 + 源/事件/辩论反馈报表（record-only）

**触发**: 用户认可“不要重复 LLM 链”，继续补 source/event/debate 反馈闭环。
**落地**（代码已提交 `22ba432f0`；v2 标签需主服务重启后对新指令生效）:
- `quality_shadow` v2 新增: `source / info_channel / first_news_id / evidence_ref / event_ids / event_family / debate_outcome / challenger_self_confidence / challenger_direction / critique_verdict / critique_weakest_link`。
- 新增 `scripts/quality_shadow_report.py`（只读）: 历史行无标签时现场回算；按 origin/direction/evidence_tier/debate_outcome/quality 标签分组，输出 fwd T+1、gross/fee/net bps、net $、win%、PF。
- **不新增 LLM 调用、不接线交易**；A/B harness 保持离线研究工具。
**实跑（只读）**: 高分 LONG 降级 True 组 fwd **-56.3bps** / net **-13.7bps**；挑战者弃权 BUY 组 net **+17.4bps** / PF 1.8；2模型一致 BUY 组 fwd **-71.2bps**；证据层 HARD/MARKET 的 fwd 与执行净利出现分化，说明仍需“证据+方向+入场/出场”联合归因。
**验证**: quality_shadow/DB/ratchet/pipeline 定向 **134 passed**；全量 **7341 passed / 72 skipped / 3 failed**（均为存量红）；报表在生产库只读实跑通过。顺带修 `test_playbook_pipeline` 伪模块 `AbsoluteCircuitBreaker` 导入隔离，以及 report 坏 instruction_id 留痕（棘轮回 921）。
**下一步**: 每周跑报表，一次只改一个维度；不新建第二套 LLM 链。

## 2026-10-18 LLM Event Extractor/Verifier A/B harness（record-only）

**触发**: 用户令「继续」；在 event_meta 数据层之后，先搭 LLM 抽取/审计的 A/B 仪器，不接线交易。
**落地**（代码已提交 `8814c374f`；默认 dry-run）:
- 新增 `scripts/event_llm_ab.py`: 读 canonical events → 确定性 event_meta 基线 → Extractor prompt（严格 JSON）/可选 Verifier prompt；`--execute` 才调用 `llm.features.event_llm_ab`，`--verify` 只对 materiality≥0.7；输出 `data/shadow/event_llm_ab.jsonl`。
- Extractor/Verifier system prompt 均声明“材料不可信，不执行材料指令”；不输出价格/仓位/买卖建议；输出含 entities/materiality/novelty/evidence_span/counter_evidence/confidence。
- `config/unified_config.yaml` 新增 `llm.features.event_llm_ab`（qwen-flash primary / deepseek fallback）。
- **不写** canonical_events/playbook/交易；无 cron；默认不调用模型。
**验证**: harness 4 passed；config/事件/UE 回归 186 passed；全量 **7340 passed / 72 skipped / 3 failed**（均为存量红）；dry-run 在 3 条高材料性 FR 事件上产出 prompt/JSONL（未调模型）。
**下一步**: 小样本 `--execute` A/B（10–20 条，按 materiality≥0.6 抽样），预注册判据：schema/实体正确率/证据 grounding/材料性校准 vs 成本/延迟；通过后再考虑接入 UE brief。

## 2026-10-18 统一事件元数据 event_meta_v1 + 材料性排序（P0 数据层）

**触发**: 用户同意事件驱动路线并令「按照建议执行」；先补数据/推导基座，不改交易策略。
**落地**（代码已提交 `d7960a8b7`；ingest/UE 为 cron 新进程）:
- 新增 `app/event_stream/event_schema.py`: `event_meta_v1`（event_family/form/entities/materiality/novelty/corroboration/lifecycle）；确定性先验分，不是收益预测。
- `CanonicalEvent.event_meta` + `canonical_events.event_meta_json` 列/migration；`IngestService` 落库前补 meta；历史行读取时现场计算，不写库。
- UE `_gather_event_stream_clues`: 按 materiality/T0 排序后再截 `clue_limit`；clue 行追加 `family/form/mat`，让 LLM 看到结构化事件属性。
- **未改** 交易门/仓位/止损/出场；materiality 仅排序/审计。
**验证**: 事件流/UE/离线全链定向 **228 passed**；全量 **7336 passed / 72 skipped / 3 failed**（均为存量红）。24h 实查 204 条：news 187 / regulatory 17；materiality <0.5 187、0.5-0.7 17（周末无 SEC 属正常，工作日 T0 会进入高分桶）。
**生效**: EventStore 下次打开自动加列；ingest/UE cron 新进程加载；主服务无需重启。
**下一步**: LLM extractor/verifier A/B；主源扩展（Form 4/13D/G、FDA/临床、IR/新闻稿）；source/event 反馈闭环。

## 2026-10-18 质量影子标签 + 事件采集并发（用户令「按照建议执行」）

**触发**: 借券供给恢复后，按建议继续做 Batch A（方向/证据/入场质量 record-only 标签）与 Batch B（事件采集并发/超时余量）；Batch C 先做只读盘点。
**落地**（代码已提交 `027653e1d`；quality_shadow 主服务重启后对新指令生效；事件 ingest 为 cron 新进程）:
- **Batch A**: 新增 `app/playbook/quality_shadow.py`，在统一落库点 `PlaybookDB.save_instruction` 写 `metadata_json.quality_shadow`（record-only）：`score_band/origin/evidence_tier/market_corroborated/entry_archetype/evidence_gaps` + `would_downgrade_high_score_long / would_prefer_medium_pullback / would_require_market_confirmation / would_allow_unknown_short_small`。不改方向/仓位/止损/出场/门。
- **Batch B**: `IngestService` 改为源级并发（最多 4）+ 异常隔离 + `last_total_latency_ms`；`scripts/event_stream_ingest.py` 打印总时长，并把 source_health 的 PARTIAL/ERROR/stale(>30min) 折叠进 stderr 告警。T0 不再被慢源串行饿死。
- **Batch C 只读盘点**: PENDING 3217（terminal skipped/missed 占 3190，属 execution_state 未收敛的观测口径，非交易故障）；空 decision_id 2583（绝大多数为 bridge SKIP）；retention dry-run 0（raw 最早 10/9，14 天窗首个真实清理约 10/23）；`event_stream_ue_check.py` 仍未挂 cron，但 ingest 已补新鲜度日志。**prompt 注入硬化未动**（prompt 变更需 A/B + 版本升级，另批）。
**验证**: playbook/事件流/UE/离线全链定向 **294 passed**；全量 **7330 passed / 72 skipped / 3 failed**（均为存量红：factor_stats_denominator / pending_adjudication_report / shadow_artifact_consumers；capital_allocator 本轮通过）。按当前规则对历史 1173 条非 SKIP 回算：`would_downgrade_high_score_long` 97、`would_prefer_medium_pullback` 69、`would_require_market_confirmation` 35；历史行未回填，重启后新指令才落 `quality_shadow`。
**上线**: 2026-10-18 11:13 CST `systemctl restart ftseek-us`，PID 2116842，启动冒烟 **7/7**；下一轮 ingest 四源并发 `total_latency_ms` 4.2–5.3s、四源全 OK；`US.ADSK` SIM LONG 保留。
**观察**: 新指令的 `metadata_json.quality_shadow` 分层；下一轮 UE scan 的借券补查；事件 ingest 日志的 total_latency 与 health_warning；不改变实盘策略。
**回滚**: quality_shadow 为 metadata 附加，可直接 revert；事件并发可单文件 revert；无需数据迁移。

## 2026-10-18 UE 借券 UNKNOWN 逐票补查（用户裁「信息/处理流程问题可直接调整，SIM 恢复供应观察」）

**触发**: 10 月 UE SHORT 候选 24 条中 16 条 UNKNOWN。根因 = 每日借券快照只采 `daily_active_pool.json`（当日约 31–53 只），UE 榜外发现不在池 → prompt 规则10 标 UNKNOWN/false → DA 源头过滤 `borrow_feasible is False` 当死单丢弃。Futu `get_margin_ratio` 本身支持逐票查询。
**落地**（代码已提交 `f26f643a4`；UE scan 为 cron 新进程，下一轮生效）:
- 新增 `app/data/borrow_verifier.py`: UE 产出后对 SHORT 的 UNKNOWN 做小批（5）+ 逐票兜底（≤5）补查；只把 UNKNOWN 解析为 MATERIAL true/false；无数据/异常保持 UNKNOWN 并写 `borrow_checked_at/source/status/error`。
- `scripts/llm_universe_scan.py`: 合约修复后、写 scan 记录前调用；scan 记录新增 `borrow_on_demand` 计数（checked/resolved_true/resolved_false/no_data/error）。
- `config/unified_config.yaml`: 新增 `universe_expansion.borrow_on_demand_verify: true`。回滚置 false 即回旧行为。
- **未改** DA 过滤、执行借券门 `borrow_pool_gate_enabled`、仓位/止损/出场/策略流程；执行时仍逐票复核券池。
**实库验证**: 10 月 16 个唯一 UNKNOWN 空头 → **12 true / 3 false / 1 no_data**；EBS/AN/CTEV 等被误杀标的确认可借；ERAS/RGNX/VSXY 真不可借；QNCX 无数据。Futu 连续 10+ 次逐票会限流 → 小批+兜底+1s 间隔。
**测试**: borrow verifier 9 passed；UE/事件流定向 124 passed；全量 **7316 passed / 72 skipped / 4 failed**（3 条存量红 + `capital_allocator` 满载异步假红，单跑绿）。离线全链因断网在 FROZEN_CFG 关闭补查；extended-hours 基数测试改为相对日期（原 9/17 固定日期漂出 30 天窗）。
**观察**: 下一轮 UE scan 的 `borrow_on_demand`、DA feed 放行数、触发时 `borrow_pool_gate` 拦截/借券失败、SIM 空头成交与收益；不改实盘参数。
**回滚**: `borrow_on_demand_verify: false`（立即回旧行为）；或 revert `f26f643a4`。

## 2026-10-18 全链路复查确定性缺陷修复批（用户令「修复」→「下一步继续」）

**触发**: 逐段复查 EventStream 采集 → 相关性/去重 → UE 材料 → 盘前/盘中/盘后执行，确认 4 组确定性缺陷并修复；结构性方向/费用问题保持不动（需影子）。
**落地**（代码已提交 `2a03fd8bb` 并推送；主服务已重启）:
- **P0 去重**: `app/event_stream/dedupe.py` 对裸表单号（8-K/10-Q/10-K/6-K/Form 4 等）禁用 title 去重，只保留 URL 去重。实库 24h 相关 SEC 8-K 由 1 个 cluster 恢复为 5 个独立事件。
- **事件窗/相关性**: `store.py` 新增 `get_canonical_events_since()` + `first_seen_at` 索引；UE clue 改用时间窗（`scan_limit` 500→5000 仅安全上限）；`relevance.py` 排除 facts.query、清洗 Google RSS HTML/发布方尾注、英文触发词按词边界。实库 24h 587 条 canonical 可完整读取；`Spectrum News 13` 假阳性归零，真实 telecom 新闻保留。
- **采集健康**: `ingest_service.py` 按注册顺序执行（T0 先于 T2），部分 query 失败标 `PARTIAL` 并保留 `last_error`，不再 OK+空错误。
- **出场安全**: `pipeline.py` patient_exit 有效截止 = min(配置, 收盘前 10 分钟)，2026-11-27/12-24 半日市 12:50 触发 EOD；`tracking_flow.py` 孤儿 SIM 平仓提前到无指令/全终态早退之前。
**验证**: 定向回归 112 passed；聚焦 80 passed；全量 **7308 passed / 72 skipped / 3 failed**（3 条均为存量 HEAD 红：factor_stats_denominator / pending_adjudication_report / shadow_artifact_consumers；`capital_allocator` 首次满载假红单跑即绿）。`git diff --check` 干净；实库只读复验通过。
**上线**: 重启前在途 1 条 SIM LONG `US.ADSK`、非 BOOKED 出场意图 0；`systemctl restart ftseek-us` 后启动冒烟 **7/7**，PID 2082222，仓位保留，零 Traceback/CRITICAL。EventStream/UE 为 cron 新进程，下轮自动加载；Playbook 两处已随重启生效。
**回滚**: `git revert 2a03fd8bb` 后重启；EventStream 数据修复无 schema 破坏（新增索引可保留）。
**下一步**: P1-1 方向/费用结构、P1-2 借券 UNKNOWN 策略均需 4–6 周影子 + 预注册判据；P2-5 prompt 注入硬化随下次 prompt 版本影子；P2-6 PENDING/空 decision_id 对账与 UE 新鲜度告警待排期。

## 2026-10-17 全链路审计修复批（用户令「深度复查」→「确认无误按建议优化修复」）

**触发**: 主会话逐段复核数据源→指令→执行门→出场→推送/台账，确认问题后按「一次一变量」分三组落地；全部落档 `docs/decisions/RFC-146-audit-20261017-fix-batch.md`（含逐项风险/回滚/延后清单）。
**落地**（代码已改，**未提交、未重启**）:
- **执行安全**: ①MARKET 单 `price=0` 曾绕过 30% 名义上限与 equity fail-close → 参考价/共享行情估价/fail-close（`dual_broker` + `playbook_service.metadata.reference_price`）；②已认领（executed=1）指令在 outcome 未终态窗口可被盘中重跑整行覆盖 → 守卫只看 executed + UPDATE CAS（`playbook_db`）；③入场确认门重启孤儿一律 HEALED 立即执行 → 按旗标冻结的锚/窗口续 pending，超 TTL 才放行（`entry_confirm_gate`，arm 时落 `ttl_sec`）；④调度器/看板两进程同标的下单 TOCTOU → `data/locks/symbol_orders/*.lock` 非阻塞 flock，锁不可用 CRITICAL+fail-close（`playbook_service`）；⑤显式 target_env=real 可绕过 LIVE fail-mode 启动断言 → 新增 `app/core/live_readiness.py` 统一 DI/执行门，运行期 preflight 异常按环境回落（LIVE→closed / SIM→open）。**RFC-080 毕业门为工程探针有意例外，审计初判系误报，未改**。
- **数据口径**: ⑥Futu K 线缓存缺 ktype 串周期 + push buffer 混入非 1M + K_DAY 当日过滤砍到 0-1 根 → key 加 ktype/仅 1M 回写/日线取最近 count；⑦VIX 缺失静默写 20.0 + 50 根日 K 冒充 MA200 → 只写真实值/MA200 需 ≥200 根；⑧EventStream 保留策略置 NULL 撞 `TEXT NOT NULL` 每轮静默失败 → 置空串（首次真正生效）。
- **可观测/影子**: ⑨ConfigDrift 回调未接 → PushHub HIGH/SYSTEM（main.py）；⑩异步兜底告警写前留痕（daemon 线程死掉不再静默丢 CRITICAL）；⑪手动通道 `requested_by=dashboard:<ip>` + 成交后 `execution_origin/execution_overrides` 入 metadata；⑫trail_lock_pyramid 计数轴改 `json_key:n_executable`（原 12k 行虚假 READY）；⑬未结算出场意图冻结面新增 `blocked_exit_opportunity` 影子（distinct episode，n≥30 才评估）+ 30min HIGH/4h CRITICAL 年龄告警（alert-only，**不释放/不收窄/不重评**）。
- **文档**: ARCHITECTURE 行号按 HEAD 校正；charter 季度指针 OKR-2026-09→10。
**自查抓到的错**: 首轮棘轮 2 红——`except:pass` 921→924（本批新增 3 处静默 continue）与 W6 无栈告警 +7（新文件/新分支）。全改带栈告警后回绿，基线 921 不动。
**验证**: 行为/契约 + 棘轮/注册表守卫 **352 passed**；生产文件 `py_compile` 20/20；此前邻域 1378 + tests/playbook 203 + 补充 69 passed；blocked-exit 聚合器真实空台账实跑 rc=0（n=0，基线从零）。全量终轮 **7293 passed / 72 skipped / 3 failed**：本批自引入 1 红已修（`_override_audit` 的 `for _k in` 被 lv3 源码守卫正则抢先命中 → 改 `_field`），另 1 红为存量窗口校准（chase_cap 实测 11067>10500 → 调 12000），余 3 红经 detached HEAD worktree 复核为存量/数据漂移（待裁报告夹具陈旧 / p1_1 终态产物 cron 消费者未认领 / below_ma20 实时库读数翻正），逐条见 RFC-146 §5。
**延后**（须先影子定阈 + DEB 预注册）: 出场意图自动释放 / 符号级→intent 级收窄 / 受控重评。
**下一步**: 待用户授权提交；重启生效需按军规先核在途出场 SQL + `git status --porcelain -- app/ scripts/`，`systemctl restart ftseek-us` 后跑启动冒烟；首次重启观察 SymbolLock 无 unavailable、SIM 不被 LIVE 门误伤、K 线/VIX 口径字段。

## 2026-10-17 棘轮清剿批（复查另一 agent 批次发现 HEAD 双棘轮红，用户令「修」）

**触发**: 主会话复查 2026-10-17 另一 agent 批次（`7f911981a` 测量面修复 + RFC-144/145 五个提交）时发现：**两个棘轮家族在 HEAD 红**（该批提交前未跑棘轮家族，台账记过的复发模式）——except:pass 921→922（`_terminal_block_reason_for` 遥测解析循环 `except Exception: continue` 静默）、W4 debug swallow 2→3（同函数尾部读取失败只 `logger.debug`）、W6 except stack 4→5（归因写入失败 warning 缺 `exc_info`）。
**落地**: `shadow_tracker.py` 三处全改留痕（零控制流变更）——①坏行跳过改 `logger.warning(..., exc_info=True)`（R5 模式，同 `overnight_decision.py:301` 先例）②读取失败 debug→warning+exc_info ③写入失败补 `exc_info=True`。
**自查抓到的错**: 修复①第一版自己又没加 `exc_info` ⇒ 修完 W4 红变 W6 红（我自己成了 +1），第二轮补上方全绿——"修 W4 只升级日志级别不补栈 = 平移违规"，教训与 RFC-145 §2.4 方向盲同级记录。
**验证**: 棘轮家族全套 + chase_cap 邻域 + terminal_block_reason 共 **83 例全绿**；基线不动（回 921/2/4，只降不升纪律未触发）。
**生效**: 纯日志留痕、零控制流变更 ⇒ 不重启无正确性影响。`shadow_tracker` 经 pipeline 阶段6/tracking_flow 在主服务进程内有加载，新留痕严格说要 restart 才生效——可并入下次重启窗口，不必单跑。
**另一 agent 批次补登记**（该批未写 handoff，此处一句话留档）: `7f911981a` 终态门拦截归因（metadata 分布注记 + 回填 122 行已 apply + 分方向预注册窗 2026-10-17 起 + `gate_block_counterfactual` 周更 cron 已挂）；`c9162452f` 帽终态 `skip_reason` 追加 `gen=` 生成锚标签（13:11 重启已生效，新标签首个实证样本待下个交易日）；RFC-144/145 文档批结论=不建重锚机制（重开条件写死在 RFC-145 §5），B1 record-only 机检/B3 空转质量指标为推荐待办，B2 prompt 第二条待裁。

## 2026-10-16 TP-RR 门禁浮点边界容差（用户令「修复」）

**触发**: 用户贴 RIVN SHORT 门禁刷屏日志（`RR=1.500000<1.5`）令核查全链 + RFC-141 v2（2分钟版）。核查结论: v2 全链健康（今日 3 CONFIRMED 含 AVGO grace recovered / 4 ABANDONED grace persisted / 弃单观测 8 行齐 / delay 126-148s / proxy-mid 回落留痕正常）；RIVN 兜底链 SL_BREAKEVEN +$20.47 接管正常零实损；真病灶=浮点边界——指令几何十进制 RR 恰=1.5（风险0.72/回报1.08），二进制算得 1.4999999999999962 被误吞（49 笔吞单史上首例恰边界）。
**落地**（`715d1b8e1`）: 三处同尺 ε=1e-9 容差（`exit_engine` / `exit_path_replay` / `extended_hours_profit_exit_rules`），高出浮点误差 ~6 个数量级、低于最近真实吞单（1.4968）~6 个数量级 ⇒ 只救恰边界。顺带清剿 except:pass 棘轮 921→922（`b5e265ad2` bridge 台账坏行静默 continue → stderr 留痕，9/30 先例，控制流不变）。
**验证**: 棘轮家族 41 + replay/patient/EXT 108（含新钉子×2: 恰=1.5 放行 / 1.4999 仍吞）+ playbook 1053 + reentry/smoke 73 全绿；四文件 py_compile 过。
**生效**: `exit_engine` 在主服务进程内 ⇒ **需 restart 生效**（盘中 AMD/AVGO 两仓在管，重启时机待用户裁）；replay/EXT 两脚本下次运行自动加载。

## 2026-10-16 源头驱动一致性守卫 + 健康面 episode 去重（用户令「从源头修复，推送随源头调整自动体现」+ 融合全选）

**触发**: 用户裁融合 G1+G2+G4+G6 全选 + 追加要求「从源头修复，推送/展示随源头调整自动体现」。
**落地**（`1329b8631`）:
- **源头驱动机制（核心）**: `shadow_lexicon.check_lexicon_registry_consistency()`——注册表进终态而词典措辞仍活跃 ⇒ 自动暴露。接线两面：周六待裁载体出 ⚠️ 复核段 + 每日不变量自检新增 **W9**（日级可见）。首跑即抓 3 条真矛盾：futu_channel_top_movers（SUPERSEDED 已并入 KR3 轴而 KR3 已证伪）、reentry_quick_tp_gate（KILLED 但文案还说"到期结算"）已修文案；eth_exit_forgone_upside 系「终态+问题仍开放」合法形态（加"结论已出"终态标记豁免）。钉子测试含真仓库零容忍棘轮。
- **G1 健康面 episode 去重推广**：watchdog/cutover 的 1800s 时间窗冷却（对日频=无冷却，实证同一句连推 9 天）→ 内容指纹（数字归一化）变化才推，与看板 episode 同规。
**主服务零依赖，无需重启**（shadow_lexicon/daily_invariant_check 均为 cron 新进程加载）。
**融合工程挂账（预注册铺开，按序）**: G2 影子面（日频看板已具备 episode 去重；周六清单并入周频卡，前置=60 行截断修复）；G4 盘后卡（三脚本合一+延长时段盲尾统一标注，工程量最大）；G6 周六簇（8 条合一张，前置=窗口口径统一+无料不推门+截断）。每项开工前先在 DEB-20261016 补预注册段。
**下一步**: G2/G4/G6 按序开工；watchdog 周末 ET 判定修复（明天周六复发）与 error_alert 双载体去重列入 G 批附带。

## 2026-10-16 推送面准确性深查修复批 + 融合方案待裁（用户令「检查准确性/合理性/可否融合」）

**触发**: 用户要求推广 bridge 案例到全部推送面。
**深查发现**（巡检 agent + 主会话复核）与修复（`98a00502f`）:
- **我自己的雷被抓住**: bridge 等待逻辑曾把 skipped_* 当终态 ⇒ EST 季（11/01 起）错季行 08:10 写 skip 会让 09:12 推送误判批次完成（老 bug 原样复发）→ 终态只认真跑完四类 + 钉子测试。
- **regime 标签连错 3 天**：裸 SQL `ORDER BY date DESC LIMIT 1` 读到未来日的 weekly_report 幂等标记行 → 三处（da_bridge/build_symbol_rule_cards/condition_expectancy_report）统一按 db.py 口径过滤；实证修复前读 weekly_report(10-16)、修复后读 cautious(10-15)。
- **watchdog D1 假阳性**：`open_positions` 零行=账面全平=健康，被误报 never_fed 且经 meta_audit 二次放大成 P0 → 按 LATCH_TABLES 先例登记豁免+钉子测试。
- **daily_llm_token 北京日标签**：卡片现标「北京自然日口径」（聚合窗不改，保历史连续）。
**融合方案已呈用户待裁**（G1 健康面/G2 影子面/G4 盘后卡/G6 周六簇可合；G3/G5/G7 不合或部分）。
**未动余项**（深查报告 §G）：cutover/watchdog 的 1800s 冷却 << 24h 周期 ⇒ 同一告警逐日重推（解药=推广 episode 指纹）；填充行污染指令计数 3 面；error_alert 双载体同 expr 双跑；watchdog 周末用北京星期判 ET 逻辑（明天周六就复发）；eth_weekly_gate EST 季截断周五盘后；weekly_scorecard 60 行截断。这些大多改 cadence/口径语义 ⇒ 留用户裁。
**下一步**: 融合方案裁决后按组落地。

## 2026-10-16 盘前 Bridge 推送延后合并 UE 产出（用户裁「延后到 UE 产出后再推送」）

**触发**: 用户发现 21:10 后 UE→DA 产出的新标的/指令不在 20:55 的 Bridge 推送里。
**核查**: 桥接推送 20:55 CST（08:55 ET）vs fresh-event 链 09:10 ET 启动、09:13→09:25 ET 完成（实测 ~13min）——推送早了 26 分钟；指令为 DA- 前缀，` _instruction_summary` 的 DB 计数面本就覆盖它们，缺的只是时序。
**落地**（`b5e265ad2`）: ①`da_bridge_dingtalk_push.py` 加 `--wait-fresh-event`：推送前轮询 fresh-event 台账终态行（complete/failed/skip 族），批次落地再组稿 ⇒ DB 计数面自动含 UE 指令；硬死线 09:28 ET 照推（桥接内容是主面，批次故障不拖死推送）；推送窗尾 09:26→09:29 ET（仍严格盘前）；②cron 双季 20:55/21:55 → 21:12/22:12 CST（链启动后 2 分钟），已推送幂等检查在等待**之前**（错季行零等待）；③窗口边界钉更新 + 2 个新钉（终态检测/死线照推）。
**验证**: 10 例全绿；真台账检测实证（10-15=True/10-16=False 因批次未到点/未来日=False）。cron 加载即生效，未重启。
**下一步**: 今晚 21:12 CST 首轮实证——若 UE 有产出，推送的 DB 计数应含 fresh-event 指令。

## 2026-10-16 推送面巡检发现修复批（P0×3 + P1/P2，含明早载体依赖）

**触发**: 推送面巡检 agent 全量报告（§3 谎言清单/§4 感知缺口）→ 用户令「继续」。
**P0**:
- **词典谎言修复**（明早 09:15 周六待裁决载体依赖）：shadow_lexicon 的 ue_short_boost/kr3/eod_depth 三条陈旧结论改写实终局 + 新增 rfc104 词条；`--dry` 实证状态与正文一致。
- **DA stderr 采集链断裂修复**：`da_main_carrier.py` 恢复 stderr 落盘 `logs/da_pipeline_stderr.log`（10-08 接管时丢的 `2>>` 重定向 ⇒ short_funnel 影子静默断供 8 天且看板误指写方）；线上副本已同步 sha256 一致；`short_funnel_shadow_tag.py` 的 <3 评级静默跳过改 WARNING + 计数透出。
**P1**: 看板 ue_short_boost 融合行补退役标（照 carry 先例）；看板推送加 episode 级去重（`data/shadow_board_push_state.json` 指纹=告警行抹数字漂移，同一 episode 不逐日重推——治「连推 8 天同因」）；09 块「现行判据」双标清除（okr_2026_09_scoring:729/712 + okr_status 段注释）；loss_breaker 注册表加 ADJUDICATED_DO_NOT_ENABLE 挂账（status 与 verdict 一致化）。
**P2**: conf_tier_guard cron 06:55→06:05（看板 06:15 前产出，消时序倒挂）；tracking_flow 指纹死分量 pnl_net→pnl + 状态文件改绝对路径。
**验证**: 棘轮/契约/同步/看板/dedup 44+13+15 全绿；盘后载体 `--dry` 实证。Hermes jobs.json 两个 job 三处 prompt 修复（Step 8 退役 Step 11 固化 周日 OKR 指针），备份 `.bak-20261016-promptfix`。
**下一步**（巡检余量，未动）: 4 个新注册项的看板分段（现仅出现在周六清单）；carrier_orphan_census 挂 cron；推送失败钉钉兜底面统一；watchdog 纳入输入日志档。

## 2026-10-16 推送面联动新鲜度修复（用户令「确保及时更新联动感知推送」）

**触发**: 用户问两天修复波后推送面是否同步。
**自查发现+修复**（`6e6c0e82e`）:
- **OKR 面滞后**：okr_status.py 推送面把已结算的 OKR-2026-09 当现行（其 settlement 块其实已标 NOT_MET 终局，但无 10 月指针）→ 新增只读 `okr_2026_10` 状态块（KR1 BROKEN 终局带 DEB 锚/KR2 攒样/KR3 已达成/KR4 四锁指针；非判分引擎，落地债#2 仍挂账）+ 头条指针改指 10 月。
- **write-only 缺口**：conf_tier_guard 的 CONSIDER_DOWNWEIGHT 标记原零消费方 ⇒ shadow_board 新增守卫段（注册表行+最新读数+标记透出）+「降权候选」入 `_board_actionable` 推送门标记集——标记置位当日看板即推（无料不推保持：无标记静默）。
- **核对为健康**: `_board_actionable` 待裁面读注册表 ADJUDICATED（昨日结案的 ue_short_boost/rfc104/kr3 已清 needs_user ⇒ 不会再误推待裁）；`_guard_lines` 对 KILLED/停采项免停摆告警（注册表驱动红利）。
**验证**: okr 测试 44 passed（产物已带新块）；board 守卫测试 10 passed；看板实渲染确认新段。
**下一步**: 等巡检 agent 全量面报告（penetration/live_weekly/cutover/盘后追踪等面）回来后补齐余项。

## 2026-10-16 DEB-20261016 全批落地（L0 尺子三件 + 固化裁决 + L1/L3 影子 + L2 挂账）

**触发**: 用户裁「全批」（DEB §五五问全批）。
**落地**（`826a50b2d` + `7d82330b5`）:
- **L0 尺子三件**（零交易变更）：①IC 冻结队列（`monthly_ic_trend_frozen`：起点钉 2026-07-01 逐月只增不重基线 + 日聚类 CI + 活跃链路分列；首跑：9 月 +0.092 CI 含 0、10 月 −0.144 t=−1.91 临界）②cb_shadow 接 A10 时代面（era_clean n=26/+23.84 vs 全期 +43.78 分列）③long_expectancy_ledger 精确分桶（拆 ROUND 并桶伪影）。
- **固化裁决**：loss_breaker `DO_NOT_ENABLE` 终态入 verdict_history（预注册门符号相反+统计不可裁）；edge 门「永不激活」强化（滚动窗翻号实证）；审计报告 §十一更正节追加（5 处数字更正，原文不动）。
- **L1 隔夜 gap 影子**：回放引擎加 slcap_4/slcap_5 变体 + 注册表预注册（n≥30 隔夜仓∧≥10 交易日，日聚类，死线 11-30）；**首跑撞保真门 reason_match=0.586<0.80 ⇒ 读数按引擎自定门冻结**，待锚定续放或攒样。
- **L3 高信心守卫**：`conf_tier_guard_shadow.py` 每日 06:55（精确档/滚动 20 日/日聚类；CONSIDER_DOWNWEIGHT 只标记不执行）；首跑 0.7 档 n=1 无旗静默正常（与「10 月 conf 值域移向 0.6」互证）。
- **L2 挂账**：权重咬合点（screener score→rank→下游消费）未映射 ⇒ 影子先不立（「未采不注册」反向纪律），DEB §三.L2 已挂账。
**验证**: 契约+棘轮+台账 45 例全绿；脚本全部生产实跑验证。
**下一步**: L1 等攒样/锚定续放；L2 待「权重咬合点」映射子任务；DEB §五 5 问全部已裁闭。另：工作树有 Codex 会话 RFC-141 v2 在途 WIP（entry_confirm_gate.py 等），本批未触碰。

## 2026-10-16 策略面三议题 DEB 立项核查完成（信心倒挂/IC 侵蚀/闸门拦反）

**触发**: 用户令「关于策略面DEB-信心倒挂/IC侵蚀/闸门拦反的问题，请立项核查」。
**方法**: 三路并行只读归因（全部 A10 时代口径 + 净值 SSOT + 日聚类 + MDE + n<30 不结论），产物 `docs/debates/DEB-20261016-strategy-attribution-confidence-ic-gates.md`。
**头条**: 审计三头条经复算全部需修正量级——①信心倒挂=尾部 2 笔+分桶伪影（剔 SNDK/COIN 后 10 月 0.7=+33.4bps，日聚类 t=0.22 不可判；审计的 +15.31=ROUND 伪影、+15.21 不存在）；②IC 趋势线无效（滚动窗逐日重基线）但 **10 月 IC 日聚类稳健为负（CI 不含 0）真实**，全因子共动+剥 beta 仍负=「方向系统性做错」，三因（衰减/regime/10-04 prompt 换版）不可分离；③双门均 record-only 从未真拦，+43.78 的 88% 系旧时代污染，「拦反」统计不可裁 ⇒ 处置=固化不启用。
**真发现**: 10 月真出血=LONG 隔夜跳空 4 笔 −$1296（与信心正交，跨月持续）；信心=双模型共识度非 P(盈利)（r=0.031）；0.7 档逐月众数 −6~−8bps，9 月才是异常正月。
**杠杆**: L0 测量层三件（IC 冻结队列/cb_shadow A10 口径/tier 口径收口，零交易变更可直接落）；L1 隔夜跳空影子回放器（预注册草案已备，须授权）；L2 调权解冻四项（reconcile 现存读数）；L3 高信心守卫影子（可选）；明确不做=调 0.7 权重/门反向/宣判长期衰减。
**下一步**: §五待裁五问（L0 范围/L1 授权/L2 授权/L3 立项/固化裁决+审计更正回写）。

## 2026-10-16 RFC-141 v2 proxy-mid 契约热修（Codex 独立复核后续）

**触发**: 独立复核发现 v2 只按 `bid/ask > 0 ∧ bid<=ask` 判 mid 有效，但 Futu push / `get_stock_quote` 在真实盘口缺失时会用 `last_price` 回填双边价并带 `book_is_proxy` / `lv3_book_is_proxy` 哨兵；不消费哨兵会把 proxy quote 标成 `ruler_used=mid`，污染 v2 裁决分桶。
**落地**: `entry_confirm_gate._current_quotes` 消费 `book_is_proxy` / `lv3_book_is_proxy`；任一为真 ⇒ `mid=None`，该笔回落 legacy 并留痕（不改阈值、不新增行情通道）。RFC §5.10 判尺文字同步补“盘口代理不算 mid”；契约测试新增 arm 侧 / 复核侧 proxy 两个形态。
**验证**: 门契约 + 接线 69 passed；`py_compile` 通过；部署前 `app/scripts` 改动仅本批且无在途出场；`systemctl restart ftseek-us` 后启动冒烟 7/7（2026-10-16 11:54）。当前 forward 产物仍为 54 行 v1 / 0 行 v2，热修发生在首个 v2 样本前。
**入库**: 该批当时未及提交（Codex 会话结束）；2026-10-16 13:2x 主会话复核（54 例自跑全绿、文档与代码一致）后经用户授权代提交 `ae0348374`（[RFC-141] 批）+ `ff8ecc672`（[vault] 批）。

## 2026-10-15 RFC-141 v2 判尺修订落地（用户看日志裁「明显不合理，直接优化落地」）

**触发**: 用户看 21:38 live 日志（INTC SHORT 锚$92.32 现价$92.33 = **+1.6bp 纯价差跳动即弃单**）判不合理。v1 前瞻 54 行复核：弃单率 61%（33/54）、<5bp 噪声杀 3 笔、CONFIRMED opp_cost 中位 **+0.36% 已超判据③ 0.3% 轨迹**（结算器实测 c3=False）。
**病灶**: ①混尺（锚=arm 时 ask/bid 成交侧 × 复核=last trade，价差跳动被当方向信息）②一次性终态（单点采样定生死，指令盘前一次生成当日无再生通道）。
**落地**（`eedb9e16f`，9 文件；RFC-141 §5.10 先行修订，判据①②③④阈值逐字不动）:
- `entry_confirm_gate.py`: 判尺 **mid-vs-mid**（两端 bid/ask 有效才用，否则回落 legacy 混尺 + `ruler_used` 留痕；`opp_cost_pct` 定义不变保判据③同尺）+ **grace 2-of-3**（首见逆向进 grace 不即弃；≥2 票逆向才 ABANDONED；任一同侧即 CONFIRMED；断价不消耗票数；耗尽不足票=确认 fail toward trade）+ **弃单后 +5m/+10m 观测补记**（`type=entry_confirm_post` 纯观测，裁决日分解真避免 vs 误杀）。`pending_count` 含 watch（否则 scheduler 不再调 sweep 丢补记）。
- `auto_execute_flow.py`: gate 调用传 `scheduler`（arm 瞬间快照 `mid_arm`）。
- `settle_entry_confirm_forward.py`: schema v1/v2 双代通吃 + `_gen` 标签；**裁决窗 = v2 首行 date 起**（样本门 n≥60∨≥10 交易日自重计），v1 窗出 `legacy_v1_window` 留痕不裁决（两源不混算，§5.9 同纪律）；判据③只用现行代行；post 行不进 _agg。
- `daily_review.py`: post 观测行按类型精确跳过（防覆盖主判定行 outcome；历史行无 type 键须兼容——首轮误用白名单被契约测试抓红，已修为黑名单）。
- 配置三键：`entry_confirm_ruler: mid` / `grace_sweeps: 3` / `grace_votes: 2`。**回滚 v1 逐字** = `ruler: legacy` + `grace_sweeps: 0` + restart。
**§5.9 张力并陈**（未装没看见）: 预注册回放判「多探测无净 edge」测的是**旧混尺**；mid尺×持续性交互未被回放覆盖 ⇒ v2 前瞻样本=该交互首次实测，若 FAIL 优先归因 §5.9 而非尺子。
**验证**: 门契约+接线 68 例 / 棘轮家族 41 例 / 邻域 10 例全绿；全量 7207 passed 中 2 红已闭合（direction_mapping=本批引入已修；coordinator 异步排空=全量负载假红，单跑绿且与本批无交集，沿用 10s 等待语义未动）。23:14 restart 生效，冒烟 7/7；生产实证：v2 arm 行已出（`US.CAT SHORT 锚=$730.22 mid_arm=730.335 ruler=mid grace=2/3`），重启孤儿自愈按 C6 契约放行。
**待办**: v2 裁决窗自 2026-10-15 起攒样（cron 37 6 * * 2-6 自动分代）；裁决日读 `entry_confirm_post` 行分解真避免/误杀。
**v1 弃单事后分解**（`2bd8db1eb`, record-only）: replay_bars_1m（与 SIM 撮合同源，口径校验 ≤0.27%）回填 v1 全窗 33 笔 ABANDONED ⇒ 可分解 14 笔中**真避免 9 / 误杀 5**（误杀全是 +10m 内回正形态 = v2 grace 恰好救回的人群；真避免含 CRM 10-12 +10m 仍 −2.1% = v2 照拦）⇒ v2 方向获间接证据。数据源坑：futu 历史 K 与本环境模拟价水位差 ~32% 不可用；replay_bars 止于 10-14 且仅覆盖 10/22 标的，今日 2 笔待 bar 落地后重跑 `scripts/entry_confirm_postmortem_v1.py --date 2026-10-15`。

## 2026-10-15 全量回归闸门修复 + main 快进合并（用户令「合并+push」）

**触发**: 用户授权 main FF 合并 + push。
**闸门经过**（诚实记录，非一次过）: 首轮全量 15 红 → 逐根归因 = 10-12~14 断档期存量红 8 类 + 本日修复引入 2 类 + 真瞬态 1 类：
- 存量红（断档期产物未过守卫）: RFC-142 的 `app.risk.exit_reasons`/`live_fee_reconciliation` 未入隔离环境加载清单 + 沙箱 schema 缺 `net_pnl_pct`（exit_recovery ×4）；BRIEF_TEMPLATE v5→v6 两钉未跟（RFC-136 有意升级）；RFC-080 共享门钉点未随 RFC-132 结构前移；`da_gate_block_audit.py` 字面 sqlite3.connect 撞 chokepoint；两个一次性核验脚本只在 ~/.hermes 未入库（已回库 + sha256 对账 + 顺手修掉其一的 except:pass）。
- 本日引入: 我新增的三条 ADJUDICATED note 缺回源锚（补 DEB/RFC 锚）；`ue_short_boost_shadow` 终态后消费者未认领（补 _CONSUMER_ACK + 防爬升钉同步）。
- 真瞬态: okr_latest.json 05:01 抓到 `ALLOW_REAL_TRADING=true`+waiver 已过期 ⇒ KR4 alarm=True——**告警是真且正确**（正是设计要抓的状态），07:31 收旗后重生成产物即消。
- 设计性修复: entry_confirm 基线复现右删失（冻结须含结算日界）；intraday 语义静默回放语料冻结到标定窗（语料随活跃日增长使抑制率自然坍塌，非归一化失效）；coordinator 异步排空等待 2s→10s（高负载假红）。
**验证**: 全量 `pytest tests/ -q` = **7198 passed / 0 failed / 72 skipped**（终轮 13:16）；`git merge-base --is-ancestor main HEAD` 纯快进成立。
**合并**: main FF 至本分支 HEAD + push origin（main 与 rfc-060-phase1-hotfixes 同推）。
**边界**: 无行为变更批次之外的混入；vault 产物单列 `[vault]` 提交。

## 2026-10-15 棘轮双执法面合并（审计 P3）

**触发**: 审计发现棘轮家族分两互不重叠的执法面——CI 面 5 类（naive-now/except-pass/utc4/pipeline-size/W456）只在 push 时跑，无 push 日工作树漂移无人捕。
**落地**（`2465c9a2a`）: `scripts/nightly_ratchet_guard.py` 每晚 04:50（周二至周六）复跑这 5 个测试文件（测试即判据单一真源，不复制逻辑），红则 hub 告警 + exit 1（裸 traceback 同被 error_alert 崩溃尾检测覆盖）；cron+台账基线登记。首跑 33 passed。
**验证**: 生产首跑全绿。
**下一步**: 明早 04:50 首轮自动运行自查；剩 main FF 合并+push（待授权）、策略面 DEB（待立项）。

## 2026-10-15 推送旁路批（3 个活跃迁移 hub + 4 个核实免迁）

**触发**: 审计 P2-4（8-29 后新增 8 个直连旁路）→ 用户令「继续」。
**落地**（`6ceda25c5`）: ①3 个活跃旁路迁 hub-first（penetration_llm_analysis 里程碑推 / live_weekly_review 周报 / cutover_preflight 每日预检告警；sync=True + stop() 收线程池 + 异常回退直连，告警链不因路由改动断裂）；②核实免迁 4 个：da_glm_shadow_eval / da_prompt_distill_shadow_eval / deb20260901_shadow_first_report = **已耗一次性件**（窗口已关+自摘 cron+不在现役 crontab，迁移死代码无意义）；kr3_runup_report 随今日 KR3 终裁 KILLED。③无料不推合规核实：penetration 仅里程碑状态变化推、cutover 有冷却+fail-loud、live_weekly 是 --push 门后的内容型周报——审计所点 3 个「无条件推」全是已耗件。
**验证**: `test_rfc078_live_weekly_review.py` 契约测试更新到 hub 时代（默认零推送契约不变）后 42 passed；W456/except-pass 棘轮绿。纯 scripts/ 改动，cron 新进程加载，未重启。
**下一步**: 推送面收口完成（hub 外剩存量豁免件）；剩 main FF 合并+push（待授权）、策略面 DEB（待立项）。

## 2026-10-15 event_stream.db 保留窗治理落地（用户裁「推荐方案自动清理」）

**触发**: 审计 P3（event_stream.db 949MB / +170MB/天 / 无保留策略）→ 用户裁「不要将系统的空间占满了，需要及时清理」+「14天+仅清 artifact 自动化可接受」。
**核查结论**（落档备查）: `raw_events.raw_artifact`（整篇正文, 均值 261KB/行）**零代码消费方**——消费面全在 canonical_events/source_health，raw_events 仅 COUNT(*)；dedup = `INSERT OR IGNORE` on raw_id 主键 ⇒ 行在则幂等在。
**落地**: ①新 `scripts/event_stream_retention.py`——>14 天行 `raw_artifact` 置 NULL（**行全保留**），canonical/health 不动，失败 exit 1 + stderr 裸 traceback（恰被同日 P1-1 的 error_alert 崩溃尾检测覆盖）；②cron 每日 02:40 清理 + 周日 02:50 `--vacuum` 还盘（auto_vacuum=0，平时进 freelist；刻意先于 03:00 db_backup），crontab-loop-baseline 登记；③删除 4 个陈旧手动大备份（8/04+9/18, 85MB，已被每日轮换取代，删除前验当日备份在位）；④测试 2 例（老行清/行保留/canonical 不动/dry-run 零写）。
**验证**: `test_event_stream_retention.py` 2 passed + 台账守卫 10 passed；生产首跑 rc=0。
**诚实边界**: 该库 10-09 才建（最老行 <14 天）⇒ 今日首跑 `rows_to_null=0` 是**预期**；10-23 起每天清出 ~170MB/天的增量，稳态 ≈ 2.4GB（14 天窗）+ 备份 7×≈17GB（55GB 余量可容）。当前 949MB 要到首轮清理后才靠周日 VACUUM 回收。
**下一步**: 10-23 后核首轮真清（`logs/event_stream_retention.log`）；P3 余量：棘轮双执法面合并 / main FF 合并（待授权）/ 策略面 DEB（待立项）。

## 2026-10-15 P2 收尾波（熔断地图刷新 + 切 REAL 清单 + 真钱推送归 hub + chase_cap 洗清）

**触发**: 用户令「继续」。**落地**（`a0d6bc994`）:
- **熔断地图刷新**（`docs/circuit-breaker-map.md`）：L1 阈值勘误（green −5/yellow −10/orange −20/red −35，原文 −2/−3 系 L1.5 的档）；补收录 5 个实际拦截层（VIX fail-close / playbook 日亏闸（今日已双臂化）/ pre_trade_gate / symbol 级×2）；语义纸面标注（LIQUIDATE≡HALT 只拒新仓、`playbook/risk_circuit_breaker.py` 名如熔断实为 record-only）；六权威并存指向 `symbol_daily_loss_guard.py:22-38` 登记。
- **切 REAL 前置清单**（`docs/ops/real-cutover-preflight-checklist.md`）：审计 P2 项成 SOP——preflight_fail_mode 须 closed（否则 LIVE 启动断言拒启动）、pl_val 探针（键名已修）、四锁、waiver、手动面恒 REAL 口径、先 SIM 冒烟再切臂等 11 项。
- **live_deal_push 迁 hub**：真钱成交提醒改走 app/push hub（sync=True 立即送达，ImportError 回退直连，同 feishu_push 先例）——8 个旁路中风险最高的一个闭合；deal_id 幂等仍在上游。测试 12 passed。
- **chase_cap 洗清**（无代码改动）：US.CRM 4127 多轮 `skipped_chase_cap` 且查无成交 ⇒ enforce 真在拦；审计读数是「日志里的 reprice 尝试」非成交。
**验证**: ratchet 家族绿；重启冒烟 7/7（16:30:31）。
**下一步**: 剩余 7 个 scripts 层推送旁路（其中 3 个违反无料不推）留独立批；P3 待裁：event_stream 保留策略（须裁窗口）/棘轮双执法面合并/main FF 合并（ahead 60+）/策略面 DEB（信心倒挂、IC 侵蚀、闸门拦反）。

## 2026-10-15 P2 修复波（探针假绿 / 日亏熔断双臂化 / HTB 敏感性 / 盘后推送持久去重 + 两个快项）

**触发**: 用户令「继续」清队列（P2 中零 SIM 行为变更项 + 两个遗留小项）。
**落地**（`5a472b643`）:
- **陈旧测试修复**：`test_exit_full_fill_updates_records_and_removes_position` 确红（RFC-142 的 10-13 引入，exit_reason 归一化 lock_in→SL_TRAIL_LOCK 后断言未跟）→ 断言改归一化值，文件 45 passed。
- **死键 hygiene**：`short_boost_shadow` yaml 显式 false（对齐 KILLED 终态，真停采=10-06 cron 摘除）；`long_momentum_gate_enabled` 保留+dormant 注释（唯一调用链 10-06 下 cron，手动跑仍生效）；RFC-117 头部状态勘误。
- **P2-1 探针假绿修复**：`probe_pl_val_currency.py` 改读真实键名 `REAL_ACCOUNT_ID/SIMULATE_ACCOUNT_ID`（原键不存在 ⇒ acc_id=0 探错账户）。
- **P2-2 日亏熔断双臂化**：`_check_daily_loss_circuit` 重构——默认臂恒评（测试缝不变）；LIVE 臂武装（配置 enabled ∧ ALLOW_REAL_TRADING ∧ waiver 活跃）时加评 REAL 臂（独立 latch 文件 `_real.json`），任一臂触发即熔断；REAL 臂读数失败 fail-open 不影响默认臂。+3 钉子测试，12 passed。
- **P2-5 HTB 敏感性落地**（9/24 承诺「裁决前须跑」销账）：`scripts/htb_borrow_fee_sensitivity.py` 实测——rth_fade 影子 30%×1d 极端情景费 $106.85 vs 费前净 $618.79 = **费用非约束**；EXT 实盘空侧费前净 **−$131.57**（借券费非主因，净额本身为负——策略问题不是费用问题）。产物 `data/htb_borrow_fee_sensitivity.json`。
- **P2-6 盘后推送持久去重**：指纹（指令 id/outcome/exit_price/pnl_net）落 `data/playbook_evening_tracking_push_state.json`，同内容跨重启不重推（10-14 重推 3 次的根因），内容变化仍照推；+3 契约测试；conftest 缝登记（offline_chain_driver 不触该路径，无需双登记）。
**验证**: 目标套件 92 passed + 棘轮家族（W456/census/except_pass/naive-now）全绿——**W6 又抓到本批一处自引入**（tracking_flow warning 缺 exc_info，已修）；重启后冒烟 7/7（启动段 16:18:50）。
**边界**: SIM 面零行为变更（双臂化仅在 LIVE 武装时加面，当前 waiver 过期=LIVE 未武装）；热重载会读到 `short_boost_shadow=false`（调用面已下 cron，零影响）。
**下一步**: P2 余量——推送旁路 8 个迁移评估（含真钱 live_deal_push）须单独批；P3（event_stream 保留策略/棘轮双执法面/熔断地图/main FF）与策略面 DEB（信心倒挂/IC 侵蚀/闸门拦反）待用户划范围。

## 2026-10-15 ue_short_boost 冻结结算归档 + RFC-104 阶段0 FAIL 关停（用户双裁决落地）

**触发**: P1-3 注册表对账带出的两个待裁 → 用户裁「冻结样本结算后归档」+「判阶段0 FAIL 关停」。
**ue_short_boost**: 新建一次性结算器 `scripts/ue_short_boost_final_settlement.py`（数据源纪律：本地 `data/replay_bars_daily/` 日K缓存，50/50 符号覆盖实测；不走 futu 抢 OpenD 槽位；yfinance 本机无网络可达性实测全失败已移除兜底）→ 52/54 可结算（CRNX/NCSM 9 月行缺日K如实 missing）：**T+1 方向调整 mean +0.0523% / median +0.3394% / 胜率 51.9% / 26 日簇 t=0.667 / MDE80=2.94pp**（声称效应≪MDE，即使主门可达也无可测 edge），尾部 15 笔均值 −0.4618%。注册表 KILLED + 闹钟结案；读数标「样本未达门」观测值（不构成确认/证伪）。
**rfc104**: 全 8 行 cron 注释退役（生成×2/刷新×3/对账×2/毕业检查×1，备份 `/tmp/crontab_backup_20261015_rfc104.txt`）；台账行加退役标；注册表 KILLED + verdict_history 双条目（NOT_READY 读数 + PHASE0_FAIL 终裁）；ADJUDICATED 结案。阶段1 永不立项，复活须重新预注册。
**验证**: 注册表/台账契约 23 passed；py_compile 过；结算器幂等可重跑。
**边界**: 零交易行为变更（rfc104 为只读计划层、ue_short_boost 为 record-only 影子）；cron 改动即时生效无需重启；`data/loops/ext_entry_plan/` 与两份 jsonl 留档不删。
**下一步**: 无遗留动作；待裁队列剩 `long_momentum_gate_enabled` 死键立项 + P2/P3 队列。

## 2026-10-15 RFC-127 阶段1 未授权上线 → 用户裁「追认」（知情破例留痕）

**触发**: P1-3 注册表对账发现 `80d8817ba`（10-09 拆分批，自述零行为变更）实际携带 RFC-127 阶段1 变体语义上线（`pipeline.py:3324` ETH HELD 零成交 intent 在 RTH 开盘自动归档释放移交盘中），无开关、无授权记录、§4 判据从未满足 → 用户裁「追认（补授权+修文档）」。
**落地**: ①RFC-127 头部状态改「阶段1 已上线（变体语义）+ 2026-10-15 用户追认」+ 新增 §7 追认补记（事实链/部署语义≠§3.2 预注册设计（释放移交 vs MARKET 补腿，更保守）/选择偏差副作用/知情破例声明「不复制为先例」）；②注册表 `eth_held_exit_shadow` 重定位为「移交失败监测面」（新行=该移交而未移交的残余风险，出现即 P1），ADJUDICATED needs_user=False 结案；③本条。
**验证**: 注册表契约测试 13 passed；行为面零改动（本批纯文档+注册表口径，不触已上线行为）。
**下一步**: ①10-30 重看移交失败子集读数；②其余待裁不变（ue_short_boost 处置 / rfc104 阶段0 / P2 队列）。

## 2026-10-15 P1 修复批·代码面（P1-1 双 cron 静默崩 + error_alert 盲区 + P1-6 W4 棘轮红，已重启生效）

**触发**: 全链路审计 P1-1/P1-6 → 用户令「全做」。
**修复**: ①`short_supply_funnel_report.py:136` 双 NULL 崩溃（`float(None)` 笔误，9/20 起静默 25 天）→ `or 0.0` + 双 NULL 计数透出报表；②`capital_flow_daily_aggregate.py` 子进程裸 `python3`（无 numpy，9/24 起静默 21 天，父 rc=0 掩盖）→ `sys.executable` + 子失败 exit 1（失败必须可见）；③`error_alert.py` 新增 `orphan_traceback_events` 裸 Traceback 崩溃尾检测（三条件：文件尾=裸 traceback 块 ∧ mtime 在窗口内 ∧ 前一行非 ERROR/CRITICAL 防双计）——补齐「只认 `] ERROR:` 前缀」的构造性盲区；④`daily_review.py:679` debug-in-except → warning+exc_info（W4 棘轮红）。裸 python3 全仓横扫：可执行面仅 capflow 一处（余为 docstring 示例），crontab/shell 干净。
**验证**: `test_error_alert.py` 32 passed（含 4 个新盲区钉子）；W456/daily_review/capital-flow 套件 52+38 绿；漏斗报表手动实跑 **25 天来首次成功**；capital_flow_replay 手动实跑 rc=0，verdict 刷新 `all_gates_pass=false`（inflow/outflow 不过、placebo p=0.42，门维持现状）；自引入 W6 违规（新 warning 缺 exc_info）被棘轮当场抓到并修复；重启后冒烟 **7/7**（启动段 13:40:59）。
**边界**: 不触交易行为；cron 脚本改动无需重启（新进程加载），daily_review 改动经本次重启生效。
**下一步**: error_alert 新检测面上线后首轮自证（若今晚 21:10 fresh-event execute 崩了应当可见）；`data/exit_ledger.db` 孤儿文件与 `.env.us` FUTU_TRADE_ENV 废弃键留 P2 清理。

## 2026-10-15 P1 修复批·治理面（P1-2 handoff 追补 + P1-3 注册表对账 + P1-4 OKR 回写 + P1-5 指针批）

**触发**: 同「全做」。**各批提交**: `68b7b5f1e`（P1-2）/ `13201de07`（P1-3）/ `d1d8b0ade`（P1-4）/ `d46408bb7`（P1-5）。
**P1-2**: handoff 追补 10-12~14 三段（17 提交，起草 agent 只读重建 + 主会话审插）：RFC-142 多模型审计修复/RFC-132 LIVE 镜像终态/RFC-143 A10 口径 SSOT/DEB-20261014 几何轴 CLOSED/门景观三连审计等；附验证状态清单（哪些只有 commit message 自述、哪些未记录重启）。
**P1-3**: 注册表对账——补登 3 项禁止态（eth_held_exit_shadow / post_ca4487[PERMANENT_MEASURE 防机械 READY] / ext_entry_earnings_criterion_shadow）；eod_depth_gate 10-14 终裁 GATE_NOT_WORTH 回写+闹钟结案；ue_short_boost_shadow 冻结 54/60（写方链 10-06 已退役）→ needs_user；eth_earnings_direction_path 10-15 复读（SHORT 26/LONG 12 双不足，LONG 净负）；rfc104 NOT_READY 落档 + eval_script 挂错修正（原指攒计划器）；entry_anchor_dual 双轴说明（n 达标但 ≥10 交易日腿未达，~10-20）；台账补登 Hermes 一次性 job 64a9a5085416（存量红转绿）。`ue_long_momentum_gate` 核实**未在采**不注册（但其生产键 `long_momentum_gate_enabled` 现为死键，待立项）。契约测试 23 passed。
**P1-4**: OKR-2026-10 回写 KR1 BROKEN 终局（10-08 换版事实 + 10-10 方案 A 裁决 + post_ca4487 窗），变更记录补行。
**P1-5**: AGENT.md OKR 指针 09→10（过期 15 天）+ 注册表计数改非字面量；**新建 CLAUDE.md 指针**（Claude Code 接线补上）；宪章「七律」→「九律」；判据字典 B19 回写 10-14 终裁；RFC-136/DEB-20261014 头部矛盾修正；PROJECT-PORTRAIT 系时点快照不动。
**⚠️ 新发现·待用户裁（本批最大单件）**: commit `80d8817ba`（10-09 拆分批，自述零行为变更）**实际携带 RFC-127 阶段1 语义上线**——`pipeline.py:3324` `_release_eth_held_exit_for_rth_takeover`：ETH HELD 零成交 intent 在 RTH 开盘自动归档释放 + 恢复 SL/BE/trailing/EOD 评估，无开关、无授权记录（docs 全域零命中），RFC-127 头部仍写「阶段1 未实施待二次裁决」、§4 启用判据（≥5 交易日∧≥3 意图）从未满足（影子面仅 2 行）；副作用=阶段0 影子面缩为「移交失败」子集（选择偏差，10-09 后零新增）。选项：追认（补授权+修 RFC-127 头部+重写判据面）/ 回滚（恢复纯 record-only）/ 挂开关默认关。已在注册表条目+ADJUDICATED 留痕。
**下一步**: ①RFC-127 未授权行为变更待裁（见上）；②ue_short_boost 两路处置待裁；③rfc104 阶段0 是否实质 FAIL + 条数带上界 44 待裁；④P2/P3 队列（event_stream 保留策略、推送纪律 8 旁路、盘后重复推送持久 dedup、HTB 借券费敏感性、chase_cap enforce 核实等）仍待授权。

## 2026-10-15 P0-2 手动平仓单向门拆除（用户裁口径 + 用户裁方案，已重启生效）

**触发**: 全链路审计 P0-2（手动实盘「能开不能平」：看板买入无闸、平仓被 `ALLOW_REAL_TRADING=false` 拦截）→ 用户裁口径「手动平仓除富途 App 外也可走 web dashboard」→ 方案二选一中用户裁「拆闸对齐设计」。
**修复**: `app/web_dashboard/order/executor.py` 平仓路径删除 6/12 P0-2 的 ALLOW_REAL_TRADING 检查（原位留注释说明口径演变）；对齐 `validator.py:86` 既定设计（2026-06-24：该旗标只锁 AUTO pipeline，手动恒 REAL 不适用）；原闸防护场景（切回模拟盘后误下实盘）在「手动恒 REAL」设计下不成立。手动侧其余风控（LIVE_CONSERVATIVE 等）不动；`mode_switch_service.py` 的模式展示/切换语义不动。
**验证**: 平仓契约测试 `tests/web_dashboard/test_close_position_p1_contracts.py` 14 passed（无测试钉死被拦行为）；py_compile 过；重启前核：仅 US.CAT 一笔 SIM 未平仓（durable exit ledger 接管）+ 部署面仅本文件改动；`systemctl restart ftseek-us` 后 `startup_smoke_check.py --wait 90` **7/7 全绿 exit=0**（启动段 13:11:53）。
**边界**: 仅此一处行为变更（一次一变量）；`.env.us`/配置零改动；AUTO pipeline 的 ALLOW_REAL_TRADING 锁与四锁全部不受影响；`executor.py` 其余逻辑零改动。
**下一步**: 用户下次在看板平真实持仓时即验证通畅（此前必被拒）；P1 修复批范围仍待用户裁决。

## 2026-10-15 KR3 SHORT 腿终裁：FALSIFIED ⇒ KR3 达成 + 停采回流链一次闭合

**触发**: 全链路审计 P0-1（KR3 SHORT n≥30 今日到期）→ 用户令「同意，先处理P0-1」→ 裁决批执行 → 用户裁「FALSIFIED 落档+停采」。
**裁决**: SHORT would-block 已结算 n=30 恰好于预测日（10/15）达门（另 1 行未结算系采集器 trade_id 幂等写入即跳过，非停采损失）。重跑 `scripts/kr3_final_adjudication.py --window-end 2026-10-15`（B1 延长授权，产物 `data/shadow/kr3_final_adjudication.json` 的 window.authority 如实标注）：SHORT 三要素 **1/3**（①日聚类 t=-0.2994 不过门 ②声称 -21.83 < MDE80=56.45 不可测 ③OOS 同向✓）⇒ **FALSIFIED 干净证伪**；LONG 腿延长窗复核仍 FALSIFIED（0/3）与 10/4 冻结窗一致。两腿全证伪 ⇒ 按 OKR「确认或干净证伪都算达成」**KR3 达成**。阈值守卫 runup=1.0 零漂移；判分 n=224/台账 261。
**回流链**（反审计「裁决不回写」五形态，一次闭合）: ①采集 cron `10 6 * * 2-6` 注释退役留痕（备份 `/tmp/crontab_backup_20261015_kr3.txt`；动 cron 前已核消费方：registry/runup_report/top_movers_join 均只读 jsonl）②`crontab-loop-baseline.txt` 同步退役（fee_unit 先例格式）③注册表 REGISTRY→KILLED + verdict_history 追加 10/15 条目 + ADJUDICATED 闹钟结案（recheck_after 清空）④OKR-2026-10 KR3 段+变更记录回写 ⑤采集器 docstring 退役标 ⑥本条。
**验证**: py_compile 双文件过；`pytest -q tests/test_control_loop_ledger.py tests/test_criteria_registry_visibility.py tests/test_shadow_registry_criteria_provenance.py` = **22 passed / 1 failed**，唯一红 `test_enabled_job_is_in_ledger` 系**存量红非本批**：Hermes 一次性 job `64a9a5085416`（RFC-123 阶段0 毕业核验提醒，10-13 创建、2026-10-21 09:00 触发）未入台账——正是 10-12~14 SSOT 断档期产物，归 RFC-123 owner 补登，本批不代收。`git diff --check` 过。
**边界**: 零交易行为变更（kr3 门 record-only 从不拦截）；注册表 .py 改动不影响在跑服务（消费方全是 cron 新进程，主服务不 import 注册表做行为判定）；未重启；jsonl 冻结留档不删；审计报告文件是同会话确认 agent 在途 WIP，本批未触碰未提交。
**下一步**: ①确认 agent 复核轮已完成：报告已修正经济性口径（10 月 75 行全 PLAYBOOK_SIM，「extended_hours_entry 5」系误标；70 regular + 5 premarket/EXT）并补齐证据锚②RFC-123 owner 补台账行（或 10-21 一次性触发后按 spent 先例处置）③P1 修复批范围待用户裁决。

## 2026-10-15 全链路审计报告落档（5 路只读并行审计 + 确认轮复核更新）

**触发**: 用户问系统全链路是否合理完美 → 授权多 agent 审计 → 用户令「先将本报告落档」，确认轮由另一 agent 执行。
**落地**: 新增并复核更新 `docs/analysis/2026-10-15-full-chain-audit.md`：仓库分支/Playbook 链路/棘轮防线/治理待裁/策略经济性五路综合，含 P0–P3 分级清单、证据锚/SQL、交叉验证矩阵、10 项待用户裁决汇总。
**头条发现**: ①两条生产证据链静默失败 21–25 天（funnel_report cron 非零退出；capital_flow 父 cron rc=0 但子 replay 因裸 python3 缺 numpy 每次失败）且 error_alert 按构造看不见（只认 `] ERROR:` 前缀）；②handoff 缺 10-12~14 三段（17 提交零记录）；③影子注册表实测 79 项含 4 项在采未注册（军规禁止态）且无反向校验；④KR3 SHORT n=30 当日达门（终裁已由顶条闭合：FALSIFIED/KR3 达成）；⑤手动实盘"能开不能平"单向门（executor.py:409-413）；⑥10 月费用比改善（复核口径 `fee/(gross+fee)` 99.9%→44.0%；常规 `fee/gross` 为 78.5%）但 IC 恶化（月度 T+1 -0.1228）+0.7 信心档由最佳反转最差+KR1 判决面 BROKEN ⇒ sim→live 毕业证据变弱。
**边界**: 确认轮仅改 `docs/analysis/2026-10-15-full-chain-audit.md` 与本 handoff 条目，零代码/零配置/零行为变更，不触发重启；审计与复核全程只读（禁 pytest/禁写库/SQLite `-readonly`），未修复任何发现。当前工作树另有顶条 KR3 裁批在途，本条未触碰其代码/注册表/OKR改动。
**验证**: 确认轮亲核 P0-2 与 P1-1，并以只读 SQL 复核 §六经济面；修正 10 月 75 行=74 已平+1 在持、全部 PLAYBOOK_SIM（70 regular + 5 premarket/EXT）、费用比公式、post-10/09 n=28、0.7 档 +15.31；`+20.24`、`technical_breakdown 0.191`、`long_expectancy +469` 三个不可复现单源读数已降级为待证。多源交叉确认项见报告 §九矩阵。
**下一步**: P0-2（手动平仓口径）待用户裁决 → P1 修复批（cron×2+error_alert+裸 python3 横扫 / handoff 追补 / 注册表对账 / OKR 回写 / 指针批 / W4 一行修）待授权；当前分支 ahead 2（报告提交 + KR3 终裁提交），审计数据基线仍为 `4e2e072e6`。

## 2026-10-15 WIP 复核代提交 + 统一重启闭环

**触发**: 用户令「请检查其他 WIP 的内容，确认是否可以代提交，完成后重启」。
**复核与提交**: 逐项审计当前 WIP 后按可回滚边界分 5 笔入库：
`da7b48b8b`（DA 方向/隔夜复盘口径）、`1e65eac1e`（fresh-event 去重查询 fail-closed
与借券快照开关显式化，含提交钩刷新的 pipeline 快照）、`3c4adbbca`（Playbook 盘后
口径）、`5f7755a73`（10/13–10/14 vault 产物）、`5d4e906ae`（handoff 切档与 agent
治理接线）。`handoff` 档案 payload 校验 sha1
`4bff26a1698f69e4487d4cdc25854c588b6c3297`；因历史原文含尾随空格，治理提交按
「逐字迁移优先」跳过 whitespace 钩。
**验证**: 相关回归合并跑 `pytest -q tests/test_playbook_feishu_push.py
tests/test_direction_provenance_review.py tests/test_daily_review.py
tests/test_postmarket_drawdown_caliber.py tests/test_da_fresh_event_batch.py
tests/test_da_output_contract.py tests/test_ue_borrow_brief.py
tests/test_ue_evidence_index.py tests/test_review_source_attribution.py`
= **214 passed / 1 warning**；`py_compile` 通过；除逐字历史档案外 `git diff --check`
通过。提交后工作树与 `app/ scripts/` 部署面均干净。
**重启闭环**: 重启前置 SQL 显示仅 `US.CAT` 一笔 SIM 未平仓（durable exit ledger
接管）；`systemctl restart ftseek-us` 后新 PID `1333304`，
`startup_smoke_check.py --wait 90` 七项全绿（ConfigValidator/漂移/热重载/SSOT/
双臂安全/退出快循环/零 Traceback-CRITICAL，exit=0）。未 push（用户未令）。

## 2026-10-15 Playbook 盘后追踪口径修正（已入库，已统一重启）

**触发**: 用户核查 10/14 盘后报告，发现「执行 7/61」、混合已平/持仓 PnL、
亏损单负捕获率与 ORCL「未达入场价」表述误导。
**修复**: `app/playbook/feishu_push.py` 仅改展示口径：分母改为非 SKIP 可执行指令；
总结拆分已平净利/持仓浮亏/合计；每单展示净/毛/费用；捕获率毛/净分列且亏损单
不再输出负百分比；未触发单透出实际 `skip_reason`。不改下单、仓位或退出行为。
**验证**: 10/14 真实数据干跑显示 `执行 7/8 (SKIP 53)`、已平 `+$25`、持仓
`-$110`、合计 `-$85`；ORCL 拒因为 `thesis_drift_invalidate`；RIG 毛捕获 60% /
净捕获 25%。`pytest -q tests/test_playbook_feishu_push.py` = **18 passed**，
`git diff --check` 通过。
**提交**: `3c4adbbca`。Python 改动已随本日统一重启加载。

## 2026-10-15 RFC-136/Hermes commit 复查问题修复批（已入库）

**触发**: 复查 `c162df515467f63322f06d6358a3230482432a31`（FTSeek）与
`767cb364ff3e7ee732f167669dcac57baffd2c33`（DA skill v0.96）后，用户令「继续」执行修复。
**修复**: ① `da_fresh_event_batch` 当日指令查询失败改为返回 `None`；`--execute` 记录
`dedup_query_failed` 并跳过本批，防止绕过 UPSERT 防覆盖；shadow 仍降级为空集继续攒样。
② `universe_expansion.borrow_snapshot_brief: true` 显式落入
`config/unified_config.yaml`，使 ADR-046 kill-switch 可见可回滚。③ DA skill 新增真实
`Pipeline(ue_only=True)` Stage 6b 契约测试，同时断言 bridge 与 DB raw instruction 的
`origin=ue_fresh_event`。④ 本条补齐原 commit 未落 handoff 的进度档。
**验证**: FTSeek 相关回归 `pytest -q tests/test_da_fresh_event_batch.py
tests/test_da_output_contract.py tests/test_ue_borrow_brief.py
tests/test_ue_evidence_index.py tests/test_review_source_attribution.py`
= **178 passed**；DA skill 全测试 = **54 passed**。
**边界**: 只改批处理 fail-closed、显式配置与测试，不改 Hermes 分析逻辑、候选选择、
下单/退出行为；`da_fresh_event_batch.py` 由 cron 每次新进程加载，无需重启主服务。
**提交**: `1e65eac1e`（含提交钩自动刷新的 `docs/trading-pipeline-flowchart.md` 快照）。
**下一步**: 观察下一次 fresh-event cron：查 `dedup_query_failed` 是否出现、
`universe_scan.log` 借券快照计数、事件账本 `mode=execute` 与 DB `ue_fresh_event`。

## 2026-10-15 Hermes 复盘优化复查修复批 + 四锁到期收回 (用户令「继续」)

**触发**: 复查 Hermes 的日报「DA 方向产出/执行状态」与「隔夜遗留仓风险」优化后发现 2 个口径缺口 + 2 个运行/治理缺口 → 用户令「继续」执行修复闭环。
**代码修复**: ① `_get_plan_vs_actual` 计划基准纳入 `skip_reason=entry_confirm_reversed` 行，修复「当日全部被入场确认放弃」时早退导致方向统计整段消失；② ledger 缺失/坏行时不再静默少计，`planned_details` 保留 `UNKNOWN`，`direction_provenance` 新增 `original_direction_unknown(_symbols)`，Obsidian 渲染「原始方向缺失」；③ 隔夜风险块维持 record-only，不触退出规则。
**验证**: 10/13 实数据读数不变：DA 原始 BUY 11 / SELL_SHORT 5 / UNKNOWN 0，执行 5、入场确认放弃 8、其他闸门 3；隔夜遗留 2 笔净 −308.98，其中 `OVERNIGHT_GAP_STOP` 1 笔 −357.49，最差 `US.AMD`。`pytest -q tests/test_direction_provenance_review.py tests/test_daily_review.py tests/test_postmarket_drawdown_caliber.py` = **18 passed**；`py_compile` 与 `git diff --check` 通过。
**运行闭环**: 第一次重启后冒烟 6/7，唯一红项为双臂安全确认——双源均显示 `AUTO_TRADE_ACCOUNT=sim` 但 `ALLOW_REAL_TRADING=True`；查 `data/live_probe_lock_waiver.json`，RFC-135 授权已于 2026-10-12 16:00 ET 过期。按预注册到期语义将 `.env.us` 的 `ALLOW_REAL_TRADING` 收回 `false`（仅此一键，不改账户路由/退出参数），二次重启后 `startup_smoke_check.py --wait 90` **7/7 全绿 exit=0**（启动段 07:31:52）。
**边界**: 复盘代码为只读/报表口径修复，不改变下单、持仓或退出行为；未新增推送；已入库 `da7b48b8b`。`.env.us` 为运行环境文件（git 忽略），锁恢复已留本条；重启前仅 `US.CAT` 一笔 SIM 未平仓，快循环由 durable exit ledger 跨重启接管。
**下一步**: 观察 10/15 复盘自动产出新方向/隔夜段。

## 2026-10-15 handoff.md 分层瘦身 + 六面 Skill 发现性复查（第 3 批·零代码）

**触发**: 用户开新 Codex 会话完成技能复查后令「继续」；上一条已把
`handoff.md` 1.29MB → 滚动队列 + 归档列为独立批。
**落地**: ① 新会话技能目录出现 `r1/six-surface-zero-ref`，证明 `.agents/skills`
项目级发现机制生效。② 按当前 OKR 边界切档：根 `handoff.md` 保留 2026-10 条目，
体积 1,294,794 B → 105,552 B（降 91.9%）；
2026-09-30 及更早 L1449–L4226 逐字迁入
`docs/archive/handoff-archive-pre-2026-10-01.md`（2,778 行 / 1,190,755 B /
sha1 `4bff26a1698f69e4487d4cdc25854c588b6c3297`），原位留指针。③ 本条落档。
**边界**: 纯 Markdown 文档切档，零代码/零配置/零测试改动，不触交易行为、不需重启；
`handoff.md` 仍是唯一进度队列，档案仅是历史只读层；切档前头部与档案 payload 均已
`cmp` 逐字节校验，未改写历史内容，未动上方他人在途 WIP。
**下一步**: Skill 继续按原计划观察 1–2 周执行率与遗漏率；handoff 先按月滚动，
下次切档须先复查未决「下一步」是否已全部迁移/结案，未达标不自动切。

## 2026-10-15 首个仓内 model-invoked Skill 试点：六面零引用（mattpocock/skills 机制本仓化·第 2 批）

**触发**: 用户令「继续」执行第 2 批试点；原则仍是只借技能封装机制，不整包安装
`mattpocock/skills`，避免与既有 DEB/冒烟/棘轮治理重复。
**落地**: ① 新增 `.agents/skills/six-surface-zero-ref/SKILL.md`，把「归档/删除/退役/停用」
类请求路由到六面零引用核查：仓内引用 / crontab / systemd / Hermes prompt / `.git/hooks` /
scheduler 字符串面；命中即停、外部状态不可读不算零、证据须写命令+命中+分类。② 权威仍归
`AGENT.md` 五面 + DEB-20260910 §8.6 第六面，技能是执行路由而非平行军规；YAML 段删除额外
路由到 validator `REQUIRED_KEYS` / config audit `_AUDIT_ITEMS` / `CRITICAL_KEYS`。③ 本条落档。
**路径依据**: 本机 `codex-cli 0.162.1` 对应 OpenAI Codex `rust-v0.162.1` 源码
`codex-rs/ext/skills/src/host_roots.rs`：仓库级技能根为 `.agents/skills`（且项目
`.codex/skills` 另需项目 config layer）；故选零配置、可版本化的 `.agents/skills`。
**边界**: 纯 Markdown +技能发现路径，零代码/零运行配置/零测试改动，不触交易行为、不需重启；
未动他人 WIP；已随本批治理提交入库。本会话技能目录在启动时固化，不能靠本会话列表验证发现性。
**下一步**: 新开 Codex 会话后用一次「退役/归档 X」请求观察是否自动触发该技能；试点 1-2 周
只看执行率与遗漏率，不加第二个技能；`handoff.md` 分层瘦身仍单独开批待裁。

## 2026-10-15 AGENTS.md 入口接线 + GLOSSARY.md 初版（mattpocock/skills 调研落地批·零代码）

**触发**: 用户问 GitHub `mattpocock/skills` 能否对本仓做体系化优化 → 调研结论「不装插件、
只借机制、本仓化」→ 用户裁「同意，不为了安装插件而插件，不制作重复臃肿，该合并瘦身」。
**落地**: ① 新增根 `AGENTS.md` 纯路由入口——Codex CLI 只自动加载 `AGENTS.md`、Claude Code
只加载 `CLAUDE.md`，原治理文件 `AGENT.md`（单数）两个 harness 均不自动注入 = 接线断点
（此前治理靠 agent 碰巧去读）；路由零复制细则，权威仍在 `AGENT.md`。② 新增根
`GLOSSARY.md` 初版（借 mattpocock `domain-modeling` 格式）：只收引发过事故的本仓词
（口径/判据/cohort/MDE/净 bps/影子/DEB/RFC/六面零引用/启动冒烟/无料不推等），
定义路由到权威源，不造平行判据。③ 本条落档。
**边界**: 零代码/零配置/零测试改动，不触交易行为，纯 `.md` 不需重启；未动 `AGENT.md`/
宪章/OKR 既有内容；已随本批治理提交入库。
**借鉴来源**: mattpocock/skills 的 invocation 与上下文负载工程学 + GLOSSARY 格式；
明确**不整包安装**——其通用技能（grill/code-review/TDD/wayfinder）与既有 pre-commit/
冒烟/DEB/棘轮体系大面积重叠，且违反减法纪律。
**下一步**: 待用户裁决是否开第 2 批试点——把「六面零引用核查」做成首个 model-invoked
`SKILL.md`（跑 1-2 周看执行率是否真提升）；`handoff.md` 分层瘦身（1.29MB → 滚动队列 +
归档）单独开批，保持宪章 §六 SSOT 地位不变。

## 2026-10-14 口径与判据三线落档：A10 现役时代口径 SSOT / RFC-079 深水闸终裁 / 几何轴终审关轴（追补·无 handoff 记录，从 commit 重建）

**触发**: 无 handoff 记录，从 commit 重建。三条独立触发：①用户 2026-10-14 裁「同意 后续统计和数据筛选过滤旧数据」→ RFC-143；②RFC-079 EOD 深水闸 B19 预注册判据到期到达，用户裁「登记」（方案 A=维持 record-only）→ DEB-20261009 §42；③用户裁「重启」几何变更轴（推翻 DEB-20260823 §8.3 C 无限期搁置）→ DEB-20261014 预注册回放 → 用户终审关轴。

**修复或落地**（4 提交，按时间）:
- `2cad742bd` 08:21 DEB-20261009 §42 RFC-079 EOD 深水闸 B19 正式裁决 **GATE_NOT_WORTH（终态）**：settler 10-14 06:30 CST n=9(≥8)/ΣΔ−293.98(≤0)/miskill=5(>1)；miskill 行 settled=true 永久冻结只增不减 ⇒ 误杀≤1 数学不可达 ⇒ verdict 在任何未来样本下不变、转硬闸路径正式关闭。全量枚举 avoid 4 笔 +574.28（RIVN/QCOM/SNDK/AMD）vs miskill 5 笔 −868.26，最近两 trigger 全拦截正确 ⇒「门非失灵，是 −2.5% 深度区分度不足」。E 表漂移缺陷修复：E144 改钉终态不变量（原钉滚动 latest 必漂移）、E149 加 `date≤2026-10-12` 冻窗过滤（原读增长型 jsonl）；类级教训「E 表只钉冻窗/冻结行/终态不变量」。仅 DEB 一文件（+50/−2），处置=影子维持 record-only 续采。
- `8a995dad6` 16:49 RFC-143 A10 现役时代口径 SSOT：`app/core/db_utils.py:104` `CURRENT_ERA_START="2026-07-01"` + `ERA_CLEAN_PERFORMANCE_WHERE`(:105) + `era_clean_where(conn)`(:112，缺列 fail-open，与 `a9_clean_where` 同取舍)；接线 `scripts/entry_window_cohort.py:20/34`、`scripts/shadow_board.py:29/328`、`scripts/rth_fade_graduation_check.py:69/73/85`（`UPPER(exit_reason)` 修 DB 混存 `EXTERNAL_CLOSE`/`external_close` 两态的大小写漏剔）；判据落档 `docs/ADJUDICATED-CRITERIA.md:22` A10（账户级资金对账/费用标定豁免须注释留痕）；RFC `docs/decisions/RFC-143-era-scoped-clean-caliber.md`；钉子 `tests/test_era_caliber.py`。实测全库 +$12,526 vs 现役时代 −$1,455 符号翻转，连带撤回「SHORT 腿已证伪」（系旧时代人工 SHORT 单伪影）。刻意豁免：`daily_alert_summary.py` 等账户级对账、`long_expectancy_ledger.py` ERA_START 字面量不动。
- `1a893e46f` 17:51 DEB-20261014 几何轴重启预注册回放：两代仪器均撞保真门 —— v1 全路径 `reason_match=0.588`(<0.80)、arm0 净偏差 34.2%(>20%)；v2 状态锚定再现 0.729(<0.90)、再现价中位差 0.2048%(>0.20) ⇒ **三臂变体读数零产生**（未挪阈值重跑），按预注册转 record-only 影子出路待用户立项。引擎加向后兼容可选参数：`scripts/exit_replay_backtest.py:301/303` `anchor=None`、`:340` 锚定分支，默认路径与唯一外部消费者不变；新增 `scripts/deb20261014_geometry_restart_replay.py`、`..._v2.py` + DEB 文档（4 文件 / +702 行）。
- `1e426d06c` 18:08 DEB-20261014 §10.4 用户终审：**影子不立项**（现役 A10 口径 MFE≥1.5% 仅 105 笔 16.5%，近月窗 9/17–10/13 仅 18/142=12.7%；BE 出场族 232 笔中 98% MFE<1.5%）⇒ 1.5% 武装门受益面结构性不存在，几何轴 **CLOSED**，复活须新论据 + 新数据源。

**验证**（均为 commit message 自述，本批只读未复跑）：`8a995dad6`「62 tests green；三脚本真跑带口径标注；`--days 200` 旧时代零倒灌」；`2cad742bd`「E150-E153 实跑 4/4 绿（连同修正的 E144′/E149′ 共 6/6）」；`1a893e46f`「消费者烟测 8 过 / 1 存量失败与本改动无关」；`1e426d06c` **未记录**。

**边界**: `8a995dad6` 零交易行为变更（纯报表层，三脚本为 cron/手动独立进程，下次运行自动加载）；`1a893e46f` 引擎改动为默认行为不变的可选参数；`2cad742bd`/`1e426d06c` 纯文档零代码。**四提交均未记录重启**。同日另有 `c162df515`（10-14 23:55，RFC-136/ADR-046 UE fresh-event 转正），已由 10-15 handoff 条目覆盖，不在本次追补范围。

**下一步**: ①A10 谓词随后续改动接线到滚动窗口类报表（RFC-143 §3 明示本批不扩散）；②深水闸维持 record-only 续采，停采/registry 状态变更须另裁；③几何轴复活门槛=新论据+新数据源，且不得用本 DEB 已否的仪器重算；④DEB §42.4 剩余方向：入场质量（candidate C，5/10 on track）+ 上游 carry 决策（须独立预注册）。

---

## 2026-10-13 RFC-142 多模型审计修复批 + RFC-132 LIVE 镜像终态 + 当日只读复盘与观察轨快照（追补·无 handoff 记录，从 commit 重建）

**触发**: 无 handoff 记录，从 commit 重建。主线是 RFC-142 多模型代码审计（v2 缺陷审计 / v4 策略复查两轮 5 模型评测）经 Hermes 逐条人工取证后由 Codex 实施；同日用户三问（今日全链复盘 / entry-confirm 多探测有无改善 / 不应按收盘价定盈亏）触发 §40 只读复盘，用户「要」触发 §41 观察轨快照存档，用户裁「修」触发 RFC-099 推送呈现层。

**修复或落地**（7 提交，按时间）:
- `86dc77787` 05:13 DEB-20261009 §40 + RFC-141 §5 item 9：10-12 全链复盘净 −$233.52 = 日内 +$59.51 + SNDK 隔夜 gap −$293.03（全亏在隔夜）；entry-confirm 变体预注册回放 n=580 判定**无一胜出**（真 2min 端点 netVal +$1798 最优，V1/V2/V3/V4/V0_true3 全部 meanΔ<0、CI 含 0、IS/OOS 翻转、不可分辨；「多探测」V2 avoid 50.9%→65.5% 但 miskill 50.0%→55.3%，净无 gain ⇒ 冻结腿维持）；语义漂移发现（旧「已结算腿」实测 ~30s 有效延迟非名义 2min，已登记 RFC-141 §5 item 9）；方法论自纠撤回盘中「锚→收盘 hold」反事实；隔夜 gap P0 诚实结论（RFC-079 会接住 SNDK Δ+$169.79 但 live n=7 ΣΔ−$629.01/miskill 5 ⇒ B19 不 met，不实装硬闸、不调阈值、继续攒样）。
- `cbdad2a54` 08:43 DEB-20261009 §41 RFC-123 第2层观察轨滚动证据快照 #1（冻结窗 `--until 2026-10-12`，双锚影子 792 行 / 4 交易日 / 26 笔）：病灶依旧（LLM 计划价 73% 够不到）；or_low 成交 50% vs llm 38% 优势在可达不在更便宜（+2.90 vs +3.23 $/股）；ttl5 下 81% 退化市价 ⇒ 补漏赢家与占折扣不可兼得；阶段0 判据①缺失 0.00% PASS、②分歧 42.80% PASS，③④+≥10 交易日未达（4/10，~10-20）。
- `5c978bff8` 10:24 RFC-132 LIVE mirror failure terminal state and gate precedence（commit message 仅一行标题，内容从 diff 重建）：①`app/playbook/playbook_service.py:244` 抽出 `_evaluate_live_execution_gate()`，并在 `execute_instruction` 内**先于账户熔断**评估（新调用点 :1029，原 :1447 处内联实现删除）⇒ LIVE 授权/豁免失败不再被熔断掩盖根因；②`app/playbook/pipeline_parts/auto_execute_flow.py:1163-1203` LIVE 镜像执行失败改为 `mark_instruction_guardrail_skip` + `LIVE_EXECUTION_REJECTED` + `live_execution_failure` metadata，标记失败再追加错误行；③新增契约 `tests/playbook/test_live_arm_p0_contracts.py` 两例（gate 索引先于 `check_circuit_breaker`；镜像失败必须落 SKIP/原因），+21 行。
- `170bd80d8` 13:56 RFC-142 v2/v4 修复批（38 文件 / +2182−234，Co-authored-by: Codex）：**CapitalAllocator** `app/coordinator/capital_allocator.py` observe 影子敞口对称记账、`_coerce_position_pct` 非有限/非正拒绝、`Lock→RLock` 且 reconcile 收进锁、`auto_capacity_report` degraded+unknown 计数(:62)、新增 `BLOCKED_UNKNOWN_BUCKET`(:196)；**Broker/执行层** `app/execution/broker_adapter.py` `trd_side` 强制显式四值映射、缺失即拒单、`allow_offline_simulated_fill`(:41) 与断连判定(:491) 防实盘断连虚假成交、新增 `BrokerUnavailableError`(:22)；`app/pipeline/order_executor.py:466` `_reserve_capital_for_order` + `:520` `_release_reserved_capital` 接入下单路径（幂等键：同策略同 pct 视为已预留，不一致即拒；`allocator.is_observe_only()` 时直接放行 :476）、`_close_pre_check` broker 状态未知由「视为已平」改为「跳过本轮平仓」；`app/pipeline/position_monitor.py` scale-out 预检异常改中止；**exit_reason 归一化层** 新增 `app/risk/exit_reasons.py`（tighten→EARLY_TIGHT_SL / breakeven_lock→SL_BREAKEVEN / lock_in→SL_TRAIL_LOCK）双写入边界接线 `app/storage/trade_store.py:586/787` + `app/playbook/pipeline_parts/close_flow.py:54`；trade_records 补 `fees_est/pnl_net/fees_source/net_pnl_pct` 列（`trade_store.py:165`，ALTER 兼容 :212）+ `close_flow.py:406-415` 写 `net_pnl_pct`；**ETH 边界** `scripts/extended_hours_exit_executor.py:44` `SESSION_BOUNDARY_MINUTES` + `:180` `_last_submit_second`（边界前限价等待 20s + 撤单余量 10s，env 可覆写），`scripts/extended_hours_exit_cron.py` `next_open` 分支收进 `EXT_AFTERHOURS_HOLD_TO_OPEN`（旧语义回滚开关）。
- `a2895a914` 13:58 文档批（36 文件）：RFC-123 §F 复查修复记录 + §G 观察轨预注册；10-12 盘前/盘后/复盘日报入库；27 个符号档例行更新；新增 `docs/ops/DST-CRON-AUDIT-20261101.md`（15 条显式 ET 锚定任务 10-11 已整体右移 1h，含备份与回滚命令）；`docs/CONTROL-LOOP-LEDGER.md` 与 `docs/CURRENT-STRATEGY-MAP-20260923.md` 各一行勘误；`docs/references/crontab-loop-baseline.txt` 登记 `rfc123_planned_entry_anchor_backtest.py`；handoff.md 补 2026-10-11 两条（+42 行）。
- `66f63af73` 16:24 RFC-142 原始证据归档（36 文件 / +17,577 行）：`code_analysis_results/` v2–v5 四轮 4–5 模型（deepseek-v4.1-flash / qwen3.8 系列 / glm-5.3 系列 / kimi-k3）评测原始结果 + 对比报告 + project_context 快照，供后续复查溯源。
- `2354152e2` 21:32 RFC-099 推送呈现层（用户裁「修」）：`scripts/extended_hours_entry_executor.py:625-640` 严格臂成交卡新增「📐 方向依据」行（SHORT=盘前跳空≥闸→回落做空 / LONG=未及闸→动量做多；研究臂判据非财报动量，不加该行）；`app/push/message.py:499` 财报超预期标签与符号跟值走（负值→「财报不及预期: −x.x%」，正值渲染与旧文案逐字相同；当前上游对非正值返 None ⇒ 属潜伏/防御性修复而非现役缺陷）。

**验证**（均为 commit message 自述，本批只读未复跑）：`170bd80d8`「相关套件 326 全绿；棘轮同步 skill v0.95」；`2354152e2`「契约测试 +4（63 passed）」；`86dc77787`「E144-E147 实跑 4/4 绿」；`cbdad2a54`「E148-E149 实跑 2/2 绿」；`5c978bff8`、`a2895a914`、`66f63af73` **未记录**测试/验证。外部旁证：`2cad742bd`（10-14）DEB §42.5 记录「周二盘前重启（RFC-142/132 app 批，ET 02:30 安全窗）首日实盘验证通过」⇒ RFC-132/142 已随 10-13 重启进入生产；`2354152e2` 自述无需重启（executor 为 cron 每 tick 新进程，`earnings_surprise_pct` 唯一生产者即 executor）。`2354152e2` 另记录：`170bd80d8` 的 exit_reason 归一（lock_in→SL_TRAIL_LOCK）引入既有失败 `tests/extended_hours_order_execution.py::test_exit_full_fill_updates_records_and_removes_position`（干净 HEAD worktree 复现），当时判定非本批触碰、未代修 —— **本次追补未核其现状**。

**边界**: `2354152e2` 纯呈现层零交易行为变更；`170bd80d8` 触及下单/资金预留/退出边界（资金预留的强制模式须 `observe_only=False` 才生效，Phase B 切档仍待定）；`86dc77787`/`cbdad2a54` 只读且按归属纪律**不提交**他人 WIP（`scripts/entry_confirm_variant_replay.py`、`rfc123_planned_entry_anchor_backtest.py` 当时为未提交 WIP，前者由 `170bd80d8` 入库）；`a2895a914`/`66f63af73` 为文档/归档批零行为变更。DST 右移的实际执行发生在 10-11（见 `docs/ops/DST-CRON-AUDIT-20261101.md` §结论/§变更），本提交只是把它落档。

**下一步**: ①RFC-142 §未覆盖三项下批立项（SL_INITIAL 桶上限接入 playbook 链 / OVERNIGHT_GAP_STOP 券商侧挂单 / SL_BREAKEVEN 0.4%→0.8% retune 需 replay 验证）；②历史小写 exit_reason 脏行清洗（CTNT 类，writer 已消亡）；③RFC-123 第2层裁决窗 = 阶段0 满 10 交易日（~10-20），届时裁判据③再独立预注册；④`test_exit_full_fill_updates_records_and_removes_position` 归因已定位、是否已清待核（本批未跑测试）。

---

## 2026-10-12 门景观三连审计（§36/§37/§38）+ 门治理决策（§39）+ RFC-123 第2层补充回测（§35）+ ic-weekly 守卫清剿（追补·无 handoff 记录，从 commit 重建）

**触发**: 无 handoff 记录，从 commit 重建。①`e0306fb56` 是前一晚 `376fba434`（10-11 23:59，不在本次追补的 17 提交内）信噪比修复批交付后，全量回归 7133 例暴露两条自引入守卫红 ⇒ 清剿；②其余五提交回答用户「拦截门的设定和统一」「提升合理有质量标的供应」及 RFC-123 第2层「需要」的补充回测，全部只读。

**修复或落地**（6 提交，按时间）:
- `e0306fb56` 00:22 `scripts/ic_weekly_report.py` 守卫清剿：DB 路径提为模块级 `_SNAP_DB`（:23-27）使 3 处 `sqlite3.connect` 不再命中 `test_db_connection_chokepoint` 的字面路径正则（归因实测 HEAD^ 0 处 → HEAD 3 处，原 line 62/120/465），保留 `mode=ro` 只读语义；补报段 `except Exception: pass` 改 fail-loud，把异常类型+消息写进报告正文（:486-499），使「近窗零样本但全史显著」通道失败不再与「没内容」输出同形。归因纪律：修后本人文件 except:pass 回到 HEAD^ 基线 2 处、本批净贡献 +0；脏树仍报 922(+1) 的来源定位为 `scripts/extended_hours_exit_cron.py`（并行 agent WIP，未代改）。
- `84a91a53f` 07:21 新增 `scripts/rfc123_l2_archetype_ttl_sweep.py`（235 行，自包含独立，不碰他人未提交 WIP backtest，内化其 P0 跨日窗口修复）+ DEB-20261009 §35：archetype 分条件锚原理成立但 BREAKOUT n=1 不可回测（更正 §27「最大盲区」措辞，20/21 均值回归型现方案已对）；TTL 扫参稳健轴 = 改善 $/股（−0.03→+0.63）vs 错过笔（0→9）权衡，无清晰最优 TTL。
- `8580aaeef` 08:00 新增 `scripts/gate_ledger.py`（227 行）+ DEB §36：~20+ 道门 / 25 个独立门审计 jsonl 横跨 2 仓、无统一视图（stale_quote 事故根因=无台账）；口径纪律 —— exec_gate_audit 被轮询放大 8.3×，原始「10.5% 执行率 / 38.8% chase_cap」是假象，按 decision_id 去重后 600 唯一指令 85.7% 执行、被拦 86% 是预算帽 ⇒ 真供应约束在上游 DA signal_gate（BLOCK 61%）而非执行层；策展注册表 `scripts/gate_ledger.py:43` `GATE_PREREG` + 2 道无预注册门（archetype_conflict/bad_direction）标红常设守门；修复 `_prereg` 自指循环 bug（grep 排除台账 DEB 自身）。
- `6bb28e816` 08:20 新增 `scripts/da_gate_block_audit.py`（135 行）+ DEB §37 三层递进：「61% BLOCK」误导（80.5%/986 是 direction=SKIP，真方向被门拦仅 239 笔）；量纲更正（真驱动 = tier_score(0-10)+decision_conf，非 quality_score(0-100)，前轮误算已更正）；预测效度 n≈440：Spearman(tier_score,pnl)≈−0.000 零预测力（7-8 桶最差 WR44%）、Spearman(decision_conf,pnl)≈+0.159 弱但真 ⇒ 门拦在 score 上无效度依据、拦在 conf 上合理。
- `279e51506` 08:20 扩 `scripts/da_gate_block_audit.py` §④（+71 行）+ DEB §38：SKIP 质量分桶 644(65%) 是正确跳过；naive 前向反事实是假象（best_mfe 上界 vs 已成交实际方向 mfe_pct + replay_bars 选择偏差）；同口径公平比较 SKIP 中位 2.24% < 已成交 2.47% ⇒ **无证据过度保守**，naive「漏好票」结论 debunk；副产物：已成交实际 MFE 0.44%(43% 墓地) vs best_mfe 2.47% ⇒ 真供应杠杆在入场价质量。
- `83bcc97f9` 09:29 纯文档决策登记：RFC-091 §15（门台账归宿 = meta_audit_loop 结构化源，加 `_extract_gate_ledger` + `STRUCTURED_SOURCES`，不物理合并 25 个 jsonl；权威范围 = 影响供应 + 需预注册的门）+ RFC-092 §11（tier live 影子 = 重复造，`da_gate_decisions.jsonl` 已录 score+conf+decision、92%/1855 行/59 天 ⇒ conf-only A/B 纯回溯、零新 emitter/jsonl；预注册不设阈值防购物；推送复用 PushHub+钉钉但接前必须活体探针）+ DEB-20261009 §39。

**验证**（均为 commit message 自述，本批只读未复跑）：`84a91a53f`「E120-E124 实跑 5/5 绿」；`8580aaeef`「E125-E129 实跑 5/5 绿」；`6bb28e816`「E130-E135 实跑 6/6 绿」；`279e51506`「E130-E140 实跑 9/9 绿」；`83bcc97f9`「E141-E143 实跑 3/3 绿」；`e0306fb56`「test_db_connection_chokepoint 2 passed；本人文件字面路径命中 0；except:pass 回基线 2 处；钉子测试+既有契约 54 passed；真数据端到端 `--no-push` RC=0 且读数与修复前逐字一致」。

**边界**: 五个审计/回测提交全部只读（DB `mode=ro`），不改任何门逻辑/阈值/开关、不改 `signal_gate`/SKIP 逻辑、不改 `planned_entry` 生成、不触阶段0 影子；`83bcc97f9` 纯文档零代码，meta_audit_loop 接入实现前置须核归属 + 配契约测试。经 grep 核实：`scripts/gate_ledger.py`、`scripts/da_gate_block_audit.py`、`scripts/rfc123_l2_archetype_ttl_sweep.py` 均**未接 crontab、未入 `docs/references/crontab-loop-baseline.txt`、未入 `docs/CONTROL-LOOP-LEDGER.md`**（仅 `rfc123_planned_entry_anchor_backtest.py` 由 10-13 `a2895a914` 登记，crontab 实为 `53 5 * * 2-6`）。全部提交无重启记录（只读脚本无需重启）。

**下一步**: ①meta_audit_loop 接入 gate_ledger 源（RFC-091 §15）——本次追补核实 `scripts/meta_audit_loop.py:45` `STRUCTURED_SOURCES` 仍为 `("invariant","watchdog","gate_liveness")`、全文零处 `gate_ledger` ⇒ **截至 10-15 仍未实施**；②两道无预注册门 archetype_conflict/bad_direction 待补 RFC/DEB 出处或下线；③tier conf-only A/B 回溯分析器待立项（阈值裁决日定，产物不新建影子 jsonl）；④台账/分析器接推送前必须先活体探针（纸面告警断裂三形态：不变量自检告警链只读 `ftseek_us.log`、PushHub dedup 不跨进程、冷却键全局小时级）；⑤RFC-123 第2层裁决窗 = 阶段0 满 10 交易日（~10-20）。

---

### 附：10-12~14 追补批的验证状态清单（起草 agent 只读核实，2026-10-15）

**完全无验证记录（commit message 未给任何测试/实跑结果）**：`5c978bff8`（RFC-132，仅一行标题）、`a2895a914`（文档批，部分内容执行时点早于提交日）、`66f63af73`（证据归档）、`1e426d06c`（纯文档 13 行）。
**仅 message 自述、无独立复跑**：`86dc77787`/`cbdad2a54`（E144-E149 绿，部分读数依赖当时未提交的他人 WIP 产物）；`8580aaeef`/`6bb28e816`/`279e51506`/`84a91a53f`/`83bcc97f9`（E120-E143 绿）；`8a995dad6`（「62 tests green」未列清单）；`170bd80d8`（「326 全绿」未列清单、未记录重启）；`2cad742bd`（E150-E153 4/4）；`2354152e2`（63 passed，并标注 1 个 `170bd80d8` 引入的既有失败未代修）；`e0306fb56`（自述较详但同为自述）；`1a893e46f`（烟测 8 过/1 存量失败未点名）。
**部署/重启**：仅 RFC-132/142 有外部旁证（DEB §42.5 记 10-14 盘前重启首日验证通过）；其余 15 提交无重启记录（只读脚本/文档，无需重启）。
**未核实项（只读限制）**：`test_exit_full_fill_updates_records_and_removes_position` 现状；`data/reports/entry_confirm_variants_20261012.json` 与 `data/deb20261014_geometry_replay*.json` 产物是否存在/可复现。

---

## 2026-10-11 RFC-123 第2层附节 · 独立复查修复批 (用户令「请修复」)

**触发**: 用户令复查 Hermes 的 planned_entry 可行性分析 → 独立复算发现 1 个 P0 + 多处
事实错误 → 用户裁「请修复」。
**落档**: `docs/decisions/RFC-123-entry-anchor-lifecycle-separation.md` §F 复查修复记录。

**P0 · 跨日退出成交窗口倒挂**: `backtest()` 取次日 HH:MM 当窗口终点 ⇒ start>end ⇒
该笔对任何锚恒 miss (US.SPCX 10-08 入场/10-09 05:09 平仓, 1/21)。修复 = 跨日截入场日
16:00 ⇒ or_low 48%→52%、llm 29%→33%、「双轴完胜」弱化为「成交率明显优 + 改善微优
(差 0.13 $/股)」。**事实更正**: 错过笔「全是赢家」→ 8 赢 2 亏; 「今日开盘波动>ATR」
方向写反 (实测中位 0.31×ATR); 「ADR-035 独立互证」降级为「同一样本双口径互证」
(21/21 含于 v2 配对集); 「atr_dip_1.0 改善最大」自相矛盾 → atr_dip_0.5。
**可复现冻结**: 脚本默认 `--until 2026-10-09` 防后续交易日漂移 (DEB-20261007/08 验证器
已实测踩此坑: 26 项、15/31 片段红 = 无冻结 + 新平仓入库)。
**零生产代码变更, 不触交易行为/阶段0 影子攒样**; 回归测试新增
`test_rfc123_backtest_cross_day_exit_window_capped`, 25 passed (原 24 + 新 1)。

### 观察轨落地 (同日续批, 用户裁「执行」)

**新增**: RFC-123 第2层日更观察轨 — `scripts/rfc123_planned_entry_anchor_backtest.py --daily`
(cron `53 5 * * 2-6`, 05:35 bar 保障后; 产物 `data/shadow/rfc123_l2_rule_anchor_latest.json`)。
**新增**: TTL 限价→市价回退反事实 (`or_low_ttl5`/`or_low_ttl15`) — 第3层设计的离线影子,
回答「or_low + TTL 不回踩→市价进」vs 现状追高的差异。基线 n=21 读数: TTL5 = 5 限价 + 16 市价回退
+ 净 +$79.46; TTL15 = 9 限价 + 8 市价 + 4 TTL>持仓跳过 + 净 −$19.16。
**预注册** (§G): 观察指标无阈值, 只筛方向不裁决; 周三早 = 5 交易日观察窗, 10-20 = 阶段0 攒满 10 天。
**cron 入账**: `docs/references/crontab-loop-baseline.txt` + 守卫测试 2/2 绿。
测试 26 passed (原 25 + TTL 回归 1)。

## 2026-10-11 Jev 结构化决策引入评估 · 自动化决策纪律落档 (用户裁「核心是价值」)

**触发**: 用户问 Jev 模型 (快速结构化决策, 当前较火) 在项目场景中是否有用 → 两轮讨论 →
用户裁「别用模型火作为引入理由, 核心是价值, 在最合理和有价值的场景逐步实现自动化决策」。
**落档**: `docs/debates/DEB-20261011-jev-structured-decision-automation-discipline.md`。

**裁决**: Jev 不进主决策链 (辩论/thesis/硬闸门/SL-TP 永不); 只作边缘高频结构化小裁决候选。
纪律 D1–D5 生效: 价值先变尺子 (预注册可机械读取指标) / 四维优先级 (频率×低风险×可逆×
可测量) / 毕业制 (建议→半自动→全自动) / 红线 (风控熔断永不进概率模型, 方向预测被 DA
IC≈0 挡) / 窗口纪律 (10/31 前只读影子零行为变更, 11 月开跑会与 OKR 结算共裁)。
三个候选全部排队 (ops 分诊影子 = 唯一当前可影子; LLM Validator 前置初筛须越过
`da_prompt_distill_shadow` KILLED 前案判据; 退出侧仲裁等 KR1 冻结窗结算), 各须另立实施 DEB。
**本批零代码/零配置, 不触发重启, 不影响 oct cohort**; 机器索引已重建。

## 2026-10-10 既有红 16 条清剿批 (用户裁「1和2都做」= 清剿 + 推送)

**缘起**: 10-09 复查批全量套件发现的 16 条 HEAD 即红 (与迁移无关的既有债)。
**分流**: 4 agent 并行 (注册表类 4 / coordinator 2 / EXT 3 / 杂项 6) + 主线程补漏 1
(cutover_infra, 分派时漏排) = 16/16 全绿。

**定因分布**: 登记缺失 4 (chokepoint 祖父 +25 只读分析脚本 / dead_db 白名单 +1
prepare_live_probe / crontab 基线 +2 环路 data_retention+event_stream_ingest /
shadow_registry 补登 C13 已裁 FAIL 影子); 测试锚点过期 8 (RFC-131 账户路由缝 /
RFC-126 schema+签名演进 / RFC-132 P4 overlap block→warn 语义 / v2.14 版本钉 /
conftest waiver fixture 副作用 / 盘前窗时钟依赖 / 语义抑制率阈值随真实数据演进
0.40→0.25 留 4pp 余量); 副本漂移 2 (intraday_diagnose_late 三副本 sha256 归一,
仓外已备份); **真 bug 1** (trade_store.py futu_order_id 缺 ALTER 兜底 ⇒ 遗留库
_init_db 非幂等, 已修 + 迁移 UPDATE 包 try + W456 合规 warning 带栈)。

**并行协调**: intraday_diagnose.py 事件门控重写是并行会话在途 WIP (本批未动);
其 decide_by_stdout 红在对方落地后已自行转绿。
**验证**: 棘轮家族 5 件 + 18 个涉红文件合计 506 passed; 全量 7001 passed /
2 failed (2 红 = 漏排的 cutover + 并行在途, 均已于本批收口前转绿)。

## 2026-10-09 pipeline 拆分独立复查 + 修复批 + 开盘体检优化批

**触发**: 用户令「另外agent已完成了对pipeline.py的调整和优化，请复查」→ 复查报告 →
裁「请修复」→ 开盘后「请检查整个链路」→ 裁「请优化调整」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §24。

**复查 (6 保真核对 agent + 本地实证)**: 迁移本体语句级保真通过 (auto_execute 主循环
1079 行仅 3 处注入替换; close/monitor 20 条 SQL 逐字; 状态容器/函数属性回环正确)。
但抓出: **P0** trigger_flow.py 资金流背离影子块漏带 `Path/json/datetime` 三名
(NameError 被 except 吞成 warning ⇒ 影子静默死亡, 该影子是十月重裁数据底座);
**P1** 48 条根目录源码锚点测试断裂 (迁移回归口径只含 tests/playbook 所致) +
except:pass 棘轮 921→922; P2 注释/docstring 设计理由剥损 + 日志丢 {symbol} +
pipeline.py 两个死缓存声明。

**修复 (全部验证)**: ①P0 按文件内 `_Path/_json/_dt` 惯例补局部 import +
新增 `tests/playbook/test_falsification_capital_flow_shadow.py` 钉「影子真落盘」
(变异测试实证: buggy 态红/修复态绿); ②48 锚点重钉 (3 agent 并行, 只改源指针,
断言零改动; horizon_confidence 白名单显式登记新消费文件); ③except:pass 清剿
4 处 (data_retention / gate_attribution / event_pipeline_probe / insider_monitor,
R5 留痕进输出行); ④watchdog 跨开盘告警两阶段化 (跨界 WARNING+停 ping 不变,
宽限 `scheduler.watchdog_cross_open_grace_sec`=120s 未收轮才 🚨critical —
10-09 实证 121s 慢轮 0.2s 自解, 即时推送纯噪音; 真卡死检出延迟 0s→grace,
watchdog 600s stale 兜底不变); ⑤exit_lifecycle `*_pending` 瞬态族降 INFO 不打栈
(非瞬态守卫拒绝保留 WARNING+栈, 9/13 留栈教训不变; 30min 14 条 Traceback 噪音消除);
⑥P2: vwap_slope/vol_ratio/gather_evidence 设计理由注释从 HEAD 逐字补回,
`{symbol}` 日志恢复 (`_evaluate_technical_direction` 补 symbol 参数),
pipeline.py 两个死缓存声明删除; ⑦test_extended_hours_exit_cron 锁路径隔入 tmp
(与每分钟 cron 共锁假红)。

**开盘体检 (21:55 CST / 09:55 ET)**: 全链绿 — bridge 热重载生产实证、
auto_execute 3min 循环正常、8 开仓/6 平仓 (5 锁利 +1 SL_INITIAL −$157 按设计)、
LIVE C-lite 被账户熔断拦 (live 回撤 7.2%≥3% 真实触发, sim 不受影响)、
EXT 盘前窗「无持仓」符合翻面后预期。唯一真 ERROR = 上述 watchdog 噪音 (本批已治)。

**验证**: 棘轮家族 5 件 34 passed; 受影响面 (watchdog/execution/playbook/pipeline/
ext_cron/premarket_overrun) 1402 passed; 全量见 §24 落档行。
**纪律留痕**: ①「四棘轮」不得自行圈定 (迁移 agent 与复查官各漏 except_pass,
唯全量套件抓到) — 已更新 memory ratchet 条目; ②全量跑测期间禁并行 pytest
(共享生产 data/ 态 ⇒ 活体数据测试假红, 本次三例误报实证)。
**未提交**: 本批全部改动在工作树 (用户未令提交)。

## 2026-10-09 Event-to-Trade 链路 DEB 落档

**触发**: 用户令「同意先落DEB文件」。
**落档**: `docs/debates/DEB-20261009-event-to-trade-pipeline.md`。

- 新增泛化事件链路设计稿：多源采集 → append-only Event Store → 归一化/实体传导图 → 确定性推理 + LLM 校验 → 信号路由 → 影子执行 → 评估 → Playbook 实盘后置。
- 明确不重启 `news_v2`、不新建 Kafka/Redis 流平台、不建平行自动交易主管线；盘前/盘后先 observe/shadow，零真实订单。
- 吸收 SpaceX/FCC/Grain 样本溯源结论：官方频率与 “800 MHz” 叙述存在口径差异；`$17B`、`$93.5B`、`12M subscribers` 未核验，不得入事实层。
- 复用 `SourceAdapter`、`UnifiedNewsItemV2`、dedup/relevance、LLM 分类、risk/execution 与 extended-hours 护栏，仅新增事件层必要件。
- **本批零代码/配置/crontab/Hermes 变更**，不触发重启；只做 DEB + handoff 落档。
- **下一步**: 用户裁决 Phase 0 源与延迟审计、是否引入付费 wire/X、以及 Event Stream 与统一 SessionResolver 的分批关系。

## 2026-10-09 DEB-20261008 §16 复核修复批: G5 日期/E13 as-of/T+1 补齐

**触发**: 用户令「需要」(复查另一 agent 的 §14/§15 增补后执行小修批)。
**落档**: `docs/debates/DEB-20261008-direction-gates-long-short-bias.md` §16。

- **G5 归因修复**: `_G5_LOADED_AT` 10-08 23:10 → **10-07 23:10:46 CST**（原 off-by-one，
  无对应重启）；⑥ 段改读当前+轮转日志（含 .gz），命中 11→**233**；同时披露 §4.3
  「5 次 auto_execute」是日志可见窗口产物，全量重放为 **96 轮**（10-07 23:11→10-08
  03:59 CST），结论不变（0 可执行指令到达 G5）。
- **E13 as-of 钉窗**: C1–C7 全部 `date<=2026-10-07`；修复前 10-08 进窗致 C1/C2/C3
  3 红（活窗噪声），修复后 **7/7 + 结构 0 问题 rc=0**；历史行改写仍可检出。
- **F4 T+1 补齐**: 10-08 收盘后实跑非 dry 回填 3 行（PENG +6.30% / NOK −4.02% /
  MRVL −2.36%，open 锚）；四列 **78/78/78/78**，provenance 78 条；T+1 聚类重算
  n=75→78、pooled +1.359%→**+1.306%**、日均 +0.443%→**+0.418%**、MDE ~1.82→**~1.72pp**，
  t=0.48 不变（仍无显著性背书）。
- **文档状态头**: 勾「已决策」，状态头改写为裁决后实况；§15.1 的 pipeline.py:2230
  锚点补注实址（拆分后 `pipeline_parts/portfolio_guardrails.py:282`），不改写历史轮次。
- **验证**: gate_attribution 1 passed；§15.2 四文件 **38 passed**；py_compile 全过；
  E13 rc=0。零生产代码改动，未重启（服务 10-09 12:47 CST 起运行中，零 ERROR）。

## 2026-10-09 迁移复查收尾验证 + ue_quote_freshness 到期裁决 (resume 会话)

**触发**: 用户令「resume」(接 06:43–08:07 复查+修复会话被中断的全量验证)。

- **全量套件**: **6911 passed / 39 failed / 70 skipped** (536s)。39 红全部归类,
  **零迁移/修复批新引入**:
  - patient_exit ×19 = 既有债 (8047b494f 给 `_close_trade_record` 加 `fee_broker`
    未同步测试存根, 路线图 §10.6 已记, 本批已顺手修复 → 40/40 绿);
  - D-1 ×2 (be_passive 冻结基线 + graduation era 指纹红): **归因完成** —— 逐提交
    二分唯一直接跳变点 = `410297295` (10-08 22:04, RFC-106 §5.2) 把
    `overnight_gap_stop_pct=3.0` 从代码默认显式写入 yaml, 整文件哈希副作用,
    行为零变更 (9/26 钉值 `exit-34c7153088a1` 在隔离 worktree 精确复现);
  - D-2 env_separation: live 库 open_positions 8 行 6 月墓碑于 **10-03→10-04
    备份窗口**被清 (RFC-130/131 双臂收口期), 主不变量 bad=0 仍成立; 历史在
    trade_records (17 行含 6 月期) + live_round_trips(68) + live_deal_records(122) 完好;
  - ue_quote_freshness 单跑绿/全量红 = 注册表 refresh_counts 运行时自动提升
    (18 轮 ≥ min_n=10 → READY_TO_EVALUATE) 与测试钉静态 OBSERVING 冲突, 非迁移所致;
  - 其余 14 条 HEAD 隔离 worktree 实证同红 (既有债族: chokepoint/manual_hold/
    ops_sync/ext_entry×3/u1/ue_scan/unit_cell/intraday_silence/trading_day 副本等)。
- **棘轮**: except_pass+naive_datetime 13 passed; size/w456/utc4/w2 28 passed。
- **生产**: PID 221600 (10:36 重启载 e079796d1) 冒烟 **7/7**; 晨间零 ERROR/CRITICAL。
- **ue_quote_freshness 到期裁决 (逾期 11 天补跑)**: 影子 9/28 已达 10 轮门槛
  (预注册「届时手工/会话跑 eval 呈裁」未被触发); 机械裁决器跑 18 轮 →
  **NOT_READY** (genuine 龄 P95=1329.2s >> 90s 阈值, 会新拒 17/130, 别名洗白 30,
  交易所时戳可得 123/130) ⇒ 按预注册维持现状 (9/15 用户裁 C), **禁止事后改阈值**;
  要盘前专属阈值须重新预注册。读数已落 `data/shadow/ue_quote_freshness_shadow_eval_latest.json`。
- **待用户裁**: ①D-1 重钉 (exit-ca4487172a6f 登记 freeze 豁免+era/golden,
  注明行为等价防 cohort 分裂误读); ②D-2 测试前提更新 (墓碑已清, 改钉
  trade_records 历史行或更新前提); ③ue_quote 注册条目收尾 (记裁决+状态去留)
  + 测试钉改钉静态定义; ④工作树 4–5 批分拆提交 (运维加固/方向闸§15/EXT 默认值
  翻转/迁移本体+复查修复); ⑤patient_exit 存根顺手修 or 留费账批。

**续 (用户裁「继续」, ①②③⑤全部执行 + ④分拆提交完成)**:
- D-1: 豁免登记含行为等价声明 (仅解 W1_exit_version_drift; 主/oct cohort 两维
  BROKEN 维持, 预注册回滚线要求开跑会级裁决 — **05:05 不变量已推
  W1_oct_cohort_broken, 须在 10 月开跑会呈裁**); era 钉转阳性对照 (applies=False
  = 检出即牙齿)。
- D-2: 改钉 trade_records (17 行) mode/trade_mode 零 sim 误标 + 非空锚。
- ue_quote: 注册条目记 NOT_READY 裁决 + `no_adjudication_reason` 挂账阻断提升
  (污染源根除), 测试加挂账钉 + 基线钉改钉首条。
- patient_exit: 存根补 `fee_broker=None`, 40/40 绿。
- **提交治理钩抓到第二个 P0**: `generation_flow.run_playbook_topup_supplement`
  漏带 TechnicalFallbackAdapter 局部 import (复查漏网) ⇒ topup 屏幕通过分支必
  NameError 被吞 = 补量静默失效; 已修 + `test_topup_fallback_adapter.py` 分支级钉
  (修复前实测红/后绿)。
- 复验: 全量 **6939 passed / 16 failed** (16 = 全部 HEAD 隔离实证既有债),
  较接手时 39 红净清 23, 零新增。
- **分拆提交 6 笔**: `9edf3c1bd` 运维加固批 / `c7d882433` EXT 默认值翻转 /
  `befe3d37a` 方向闸复查链 / `80d8817ba` 迁移+复查修复(66文件) /
  `7974dd607` 本收尾批 / `66e371552` obsidian chore。工作树全净。
  注: `data/config_freeze_baseline.json` 系 gitignored 本机数据文件, 豁免登记
  只在本机生效 (与既有惯例一致)。
- **生产生效**: 12:47 重启 (重启前核: SPCX 多 1 仓在册, 两库未 BOOKED 意图 0),
  冒烟 **7/7**; 新码含 ue_quote 裁决挂账 + topup P0-2 修复。
- **遗留待裁 (不阻塞)**: W1_oct_cohort_broken 须 10 月开跑会项目级裁决
  (预注册: 不得静默移窗); ue_quote 影子去留 (继续采集作证据基 or KILL 摘 cron);
  16 条既有债族清剿排期。

## 2026-10-09 pipeline 连续迁移阶段10–14落地

**触发**: 用户授权「继续基于 pipeline 优化计划, 完成全部迁移, 盘中验证」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §21–§23。

- **阶段10**: `_check_intraday_trigger` 本体迁 `trigger_flow.py`;
  时间/module_file、降噪状态与全部指标回调显式注入。
- **阶段11**: `run_playbook_auto_execute` 本体迁 `auto_execute_flow.py`;
  风险/预算/触发/审计/影子回调与状态显式注入。
- **阶段12**: `_close_trade_record` 本体迁 `close_flow.py`;
  时间、分类、净值与止损连发状态显式注入。
- **阶段13**: `monitor_playbook_exits` 本体迁 `exit_monitor_flow.py`;
  函数属性、缓存、超时、出场/对账/落账回调显式注入。
- **阶段14**: `_force_close_orphaned_sim_positions` 本体迁
  `overnight_cleanup.py`; 出场与恢复回调显式注入。
- **验收**: 触发专项 109 passed; auto_execute 专项 254 passed;
  close/exit 专项 118 passed; 阶段12–13 合并回归 **1118 passed**;
  阶段14 源码宿主同步后专项 49 passed; 最终合并回归 **1118 passed**;
  行数+W456 **18 passed**。
- **规模**: `pipeline.py` 7,605→**3,577** (较 10,207 基线 **−6,630**)。
- **盘中实弹**: 03:37:40 CST 重启 PID **188719**, 冒烟 7/7;
  03:37:46 装载 17 个 bridge 候选并挂 `BridgeVotingAdapter`;
  03:37:57 退出快循环启动并检查 1 个未平仓;
  03:37:59 auto_execute 处理 74 条指令、输出预算/单日 guardrail,
  03:38:02 正常完成, 零 ERROR/Traceback/CRITICAL。
- **最终复验**: 03:51:43 CST 重启 PID **189747**, 冒烟 7/7;
  03:51:48 装载 17 个 bridge 候选, 03:51:58 退出快循环检查 1 个未平仓,
  03:52:14 auto_execute 处理 74 条指令并于 03:52:17 正常完成;
  两本 exit DB 未 BOOKED 意图为 0, 零 ERROR/Traceback/CRITICAL。

## 2026-10-09 pipeline 连续迁移阶段3b/4/5/6落地

**触发**: 用户授权「继续基于 pipeline 优化计划, 完成全部迁移, 盘中验证」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §16。

- **阶段3b**: 单候选阶段与组合闸迁入
  `instruction_builder.py` / `portfolio_guardrails.py`; 判定顺序、审计键、
  monkeypatch 缝与影子 schema 不变。
- **阶段4**: 风险闸/live 镜像迁入 `risk_gates.py`; baseline、连亏回调、
  `now_et/module_file` 均显式注入, fail-open/fail-close 语义不变。
- **阶段5**: VWAP/资金流/连败/双锚影子迁入 `intraday_indicators.py`;
  第二批完成 p50 量能曲线、盘中量能比、流动性门与借券池门接线;
  `_check_intraday_trigger` 主体暂不动。
- **阶段6**: 生成/morning push/topup/定桶迁入 `generation_flow.py`;
  盘后追踪与周报迁入 `tracking_flow.py`; 盘中证据迁入
  `intraday_indicators.py`; pipeline 全部保留同名转发器和显式回调缝。
- **阶段7**: auto_execute 的 kill/regime/risk/日亏前置闸迁入
  `risk_gates.py`; 候选与下单顺序不动。
- **阶段8**: signal context 关键位/主题缓存/持久化迁入
  `signal_context_store.py`, 新增专项 2 条。
- **阶段9**: auto_execute 预算/日帽装配迁入 `execution_flow.py`,
  DB fail-close 契约随宿主更新。
- **验收**: 专项 86/29/207/220/264/226 全绿; `tests/playbook` **1043 passed**;
  行数+W456 棘轮 **18 passed**; 阶段6专项 **34 passed**;
  Playbook+pipeline 合并回归 **1118 passed**;
  `pipeline.py` 9,754→**7,605**。
- **盘中实弹**: 01:39 重启 PID 179603, 冒烟 7/7; 01:40/01:43/01:46 三轮
  auto_execute 处理 74 条指令, 零 ERROR/Traceback/CRITICAL。第二批接线后
  复核两库未 BOOKED 出场意图=0, 02:03:58 重启 PID **181539**, 冒烟 7/7;
  02:04:04 装载 17 个 bridge 候选, 02:04:16 auto_execute 处理 74 条指令
  并正常完成, 零 ERROR/Traceback/CRITICAL。
- **阶段6生产复验**: 02:41:02 重启 PID **184156**, 冒烟 7/7; 02:41:10
  装载 17 个 bridge 候选, 02:41:22 auto_execute 处理 74 条指令并于
  02:41:25 正常完成, 退出快循环检查 1 个未平仓, 零
  ERROR/Traceback/CRITICAL。
- **阶段7生产复验**: 02:47:51 重启 PID **184644**, 冒烟 7/7;
  02:47:56 装载 17 个 bridge 候选, 02:48:06 auto_execute 处理 74 条
  指令并于 02:48:10 正常完成, 零 ERROR/Traceback/CRITICAL。
- **阶段8生产复验**: 02:54:22 重启 PID **185164**, 冒烟 7/7;
  02:54:27 装载 17 个 bridge 候选, 02:54:38 auto_execute 处理 74 条
  指令并于 02:54:41 正常完成, 零 ERROR/Traceback/CRITICAL。
- **阶段9生产复验**: 03:05:33 重启 PID **186189**, 冒烟 7/7;
  03:05:39 装载 17 个 bridge 候选, 03:05:50 auto_execute 处理 74 条
  指令并输出预算/单日 guardrail, 03:05:54 正常完成, 零
  ERROR/Traceback/CRITICAL。
- **下一批**: 生成/追踪流拆分, 或先拆 auto_execute 前置风险/预算阶段;
  每批继续零行为迁移 + 全量测试 + 盘中观察。

## 2026-10-09 最新策略语义对齐 + close-record 债落档

**触发**: 用户令「既有 close-record 债先修复掉」「确认最新策略是否已无选举投票」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §14–§15。

- **close-record 债**: 旧测试误把 `sl_broker_order_id` 当入场身份; 现改用
  `entry_broker_order_id` 断言, STOP 单号仅断言 STOP 身份。
  `tests/test_playbook_pipeline.py` 已恢复 **73/73 passed**。
- **最新策略**: 生产链路无旧 3 模型 `VotingCoordinator` 选举投票; 权威链路为
  DA bridge → deterministic technical fallback → Null 全 SKIP。
- **语义对齐**: 只改注释/日志为 decision adapter 语义; 保留
  `voting_coordinator` 字段、`vote()` 接口与 `_build_instructions_from_votes`
  兼容缝, 零判定行为变更。
- **下一批**: 阶段 3b 继续拆 `_build_instructions_from_votes` 单候选阶段。

## 2026-10-09 pipeline 拆分阶段3a投票输入/前置态落地

**触发**: 用户令「继续」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §13。

- **阶段3a**: `_direction_conditioned_ic_net` / `_normalize_regime_str` /
  `_build_vote_market_data` 迁入 `pipeline_parts/instruction_builder.py`;
  `_build_instructions_from_votes` 循环外前置态拆为 IC 加载、regime 乘数、
  漏斗计数、方向仲裁配置四个函数。
- **零行为变更**: 主循环判定/continue 顺序、日志文案、Instruction 字段与
  portfolio guardrail 均未动; pipeline 留同名懒转发器保 patch 缝。
- **验收**: 3a 专项 **275 passed**; `tests/playbook` 全量 **1035 passed**;
  `tests/test_playbook_pipeline.py` 72 passed + 1 既有 close-record 失败。
- **盘中实弹**: 重启前未 BOOKED 出场意图=0; 01:06:05 CST 重启 PID
  **177080**, 冒烟 **7/7**; 新进程加载 17 个 bridge 候选并挂
  `BridgeVotingAdapter`, 01:06:24 / 01:09:52 两轮 auto_execute 均处理 74 条
  指令, 零 ERROR/Traceback/CRITICAL。
- **行数/卫生**: `pipeline.py` 9,825→**9,754**; W456 except_stack 基线
  pipeline 70→69, `instruction_builder.py=1`。
- **下一批**: 阶段3b 拆 `_build_instructions_from_votes` 单候选阶段
  (投票快照/技术仲裁/方向门/horizon/数值构造), 继续一批一域。

## 2026-10-09 pipeline 拆分阶段2b-1运行上下文落地 + 盘中冒烟

**触发**: 用户令「继续」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §12。

- **阶段2b-1**: 新建 `pipeline_parts/runtime_context.py`, 迁 HorizonSelector
  单例、playbook 配置读取、DataCollector 装配、watchlist/positions/equity、
  sizing base 与 voting coordinator; pipeline 留同名懒转发器。
- **RuntimeContext**: 显式 DI + 懒属性, 生成/补量按原调用顺序取依赖;
  provider 注入 pipeline facade, monkeypatch 缝不变。时间/交易日函数刻意
  暂留 pipeline, 保 `datetime/_now_et` 测试缝。
- **验收**: RuntimeContext/entry/stale bridge/W456/棘轮 233 passed;
  `tests/playbook` **1030 passed**; 生成/补量根测试 22 passed;
  `pipeline.py` 9,933→**9,825** 行。W456 debug 基线 pipeline 47→44,
  新模块 3。
- **盘中实弹**: 重启前未 BOOKED 出场意图=0; 00:48:06 CST 重启 PID
  **175383**, 冒烟 **7/7**; 新进程加载 17 个 bridge 候选并挂
  `BridgeVotingAdapter`, 00:48:23 / 00:51:33 两轮 auto_execute 均处理 74 条
  指令, 零 ERROR/Traceback/CRITICAL。
- **下一批**: 阶段3候选构建/组合闸, 先拆 `_build_vote_market_data` 与
  `_build_instructions_from_votes` 的阶段函数; 时间域缝另批统一。

## 2026-10-09 pipeline 拆分阶段1收口 + 阶段2a bridge适配器落地 + 盘中冒烟

**触发**: 用户令「继续」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §11。

- **P1b收口**: `_net_pnl_pair` / `_playbook_exit_owner` /
  `_playbook_exit_decision` 迁入 `pipeline_parts/exit_classification.py`;
  §4 阶段1清单完成。
- **阶段2a**: `NullVotingAdapter` / `TechnicalFallbackAdapter` /
  `BridgeVotingAdapter` / `_validate_bridge_schema` 逐字迁入
  `pipeline_parts/bridge_adapters.py`; pipeline 只 re-export 并保留文件加载、
  健康检查、运行期重载调度入口。W456 基线同批搬家: pipeline except_stack
  71→70, 新模块 1。
- **验收**: bridge 契约 38 passed; `tests/playbook` 1027 + stale bridge/W456/
  行数棘轮 49 = **1076 passed**; `pipeline.py` 10,118→**9,933** 行。
  `tests/test_playbook_pipeline.py` 1 个 close-record SL 单号快照测试失败
  (非 bridge 域、本批未触碰), 已在 DEB §11.3 登记。
- **盘中实弹**: 重启前两库未 BOOKED 出场意图=0; 00:32:38 CST 重启 PID
  **174171**, 冒烟 **7/7**; 新进程加载 **17 个 bridge 候选** 并挂
  `BridgeVotingAdapter`, 00:32:55 / 00:36:04 两轮 auto_execute 均处理 74 条
  指令, 零 ERROR/Traceback/CRITICAL。
- **下一批**: 阶段2b RuntimeContext/装配函数 (风险高于 2a, 独立批)。

## 2026-10-09 pipeline 拆分阶段1首批(P1a)落地 + offline链验收尺修复 + 盘中实弹验证

**触发**: 用户令「今日无实盘, 按照计划对 pipeline.py 优化, 看是否可完成,
基于盘中交易阶段验证检查」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §10。

- **P1a 拆分**: §9.6 钉名 9 函数迁入新包 `app/playbook/pipeline_parts/`
  (shadow_telemetry / instruction_builder / exit_classification), pipeline.py
  留同名转发器, monkeypatch 缝全保; 10,249→**10,145 行**, 行数棘轮 2/2 绿;
  W456 实扫新包 **0 违规** ⇒ 无需迁基线键。
- **offline full chain 修活**: §9.2 前置债 **18 errors → 18 passed**
  (SIM 身份改道 / force_window / call_llm 双形存根 / get_db 签名 /
  冻结材料契约补齐 / prompt v1→v2 对齐生产 — 详见 DEB §10.4, 共 6 层腐坏)。
- **验收**: 域专项 101 passed; exit_recovery+pulse_freshness 47 passed
  (datetime ×3 缝保住); `tests/playbook` 全量 **1027 passed**。
- **盘中实弹**: 00:03:42 CST 重启 (SPCX 多/P 空在仓、零在途意图) + 冒烟
  7/7; 00:04/00:07 两轮 auto_execute 74 指令正常, **VWAP A/B 放行臂+台账已记**
  = 迁移函数转发链生产闭环实证; 重启后零 ERROR/Traceback。
- **既有债(非本批)**: `tests/test_patient_exit.py` 19 failed = `8047b494f`
  加 `fee_broker` kwarg 未同步 `_fake_close` 存根 (HEAD 静态可证), 留费账批。
- **下一批**: 阶段1余下 net PnL/exit rule 转发层两组 → 阶段2 bridge/context。

## 2026-10-08 盘前重启落地 + 明日 pipeline 拆分提醒登记（用户令「再重启一遍，明日提醒优化改造计划」）

**重启**（用户明示授权，18:02:43 CST，PID 135898）：本重启使 17:40 落盘的 G6
`sector_vision` 影子批随主进程生效（record-only、专项 38 passed、零交易判定变更）；
运维加固批自 17:23 已在跑，本次无新增面。`startup_smoke_check.py --wait 90` **7/7
通过**（启动段 18:02:47 正确锁定）；QCOM SIM 仓被 position_sync/reconciler 正确识别
为 playbook 自管（Futu=1，一致性 ✓），无在途出场意图，重启前后零 Traceback/ERROR。

**⏰ 明日提醒（2026-10-09，任何会话开工先读本条）**：执行
`docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md` §9 拆分路线，顺序：
1. **先裁决行数棘轮冲突**：`tests/test_pipeline_size_ratchet.py` 因 G6 批 +42 行
   当前红（10,249 > 10,207 基线，且基线禁止调大）⇒ G6 新逻辑挪新模块，或按棘轮
   文档落 DEB 例外后再降基线；
2. **修 offline full chain 18 errors**（阶段 4/5 验收尺）：driver 子进程
   `require_auto_sim()` 读 LIVE `.env.us`（`ALLOW_REAL_TRADING=true`）抛错 — 修法须
   把 snapshot 读取改道 workdir 冻结 SIM 副本；**只设子进程 env 不够**（文件读取
   优先于 env，`app/core/trading_mode.py:59`）；
3. **重钉 §9.6 行号后开阶段 1 首批**（10 个纯函数共 278 行；L2111 之后行号因 +42
   已漂，须按新 HEAD 重钉）；
4. **每批同批迁 W456 基线键**（pipeline.py 挂 47+71=118 条按文件豁免）；
   datetime patch 实为 **×3**（含 `test_exit_recovery.py:241` 经 `entry_env.pipeline`
   间接，原 §9.4 记 ×2 漏一处）。

**今晚冻结纪律**：21:30–04:00 CST 会话内不动 `app/`、`main.py`（延长时段出场/入场
执行器为每 1–5 分钟新子进程，直接 import 盘上代码，半截文件会当场打断 cron）。

## 2026-10-08 `40b4b6a1f` 复查修复批：G6 影子/E13/F4 dry/F2 语义已修

**触发**: 用户令「请修复」。
**落档**: `docs/debates/DEB-20261008-direction-gates-long-short-bias.md` §15。

- **G6**: `skew_gate_vision.jsonl` 新增 `sector_vision`（counts/violations/would-skip
  低置信 victim），同一产物承接 G4/G6，不新增第二影子、不改判定。
- **E13**: `OWN` 白名单加入 F4 回填脚本，守卫覆盖 5 个本 DEB 脚本。
- **F4**: dry-run 改为零写盘，不再把 provenance `filled` 覆盖成 `dry`。
- **F2**: 仓外 skill 删除 `quality_score -= 10`；纯记录 = 无扣分、无降档、仅留痕。
- **统计口径**: §13 已降级为 pooled 首读；按日聚类 D0 +0.638% (t=1.26)、
  T+1 +0.443% (t=0.48)，不作为「拦错盈利单」的显著性背书。
- **验证**: 专项 38 passed；E13 7/7 + 5 脚本全覆盖；py_compile 通过；F4 dry-run
  provenance hash 前后不变；`tests/playbook`（deselect 既有 offline-chain）1004 passed。
- **部署**: `pipeline.py` 待下一合法重启生效；F2 随下次 DA 进程生效；脚本/文档即时生效。未重启。

## 2026-10-08 复查 `40b4b6a1f`：实现可保留，F4 统计结论须降级

**触发**: 用户令复查另一 agent 的分析优化提交 `40b4b6a1f`。
**落档**: `docs/debates/DEB-20261008-direction-gates-long-short-bias.md` §14。

- **验证**: 相关五测试 40 passed；三棘轮 29 passed；`tests/playbook` 1004 passed +
  18 既有 offline-chain 环境错误；E13 7/7；F6 §2 漏斗独立复算一致。
- **主要发现**: F4「+0.92%/+1.36%」是 pooled 读数；按日聚类后 D0 +0.638%
  (t=1.26)、T1 +0.443% (t=0.48)，均与 0 不可区分，不能作为「拦错盈利单」的统计背书。
- **次要发现**: F4 的 100% 仅指 `fwd_return_intraday`（T1 当前 75/78，3 行待 10-08 收盘）；
  G6 无自动 sector 影子；E13 白名单漏检 F4 新脚本；F2 仍保留 quality_score −10；
  F4 `--dry` 会覆盖 provenance 状态。
- **处置**: 代码不建议回滚；统计表述与守卫边界留下批小修。未改交易代码、未重启。

## 2026-10-08 Playbook pipeline 拆分阶段 0：职责地图/路线图已落地，零代码变更

**触发**: 用户问 `pipeline.py` 超 1 万行是否必须打散、集中/分散是否有绝对优劣；随后令
「按照推荐建议来」。
**落档**: `docs/debates/DEB-20261008-playbook-pipeline-refactor-roadmap.md`。

- **结论**: 不按行数机械拆，也不一次性大拆；采用 facade + 逐域迁移。当前 `pipeline.py`
  10,207 行、119 个顶层函数/类，5 个 >500 行函数占约 49%，结构性风险成立但不必紧急打散。
- **阶段 0 已落地**: 11 个职责域地图（shadow/遥测、运行上下文、bridge、候选/组合闸、
  生成/追踪、风险/live 镜像、自动执行、盘中触发、出场/落账/对账、出场监控、隔夜清理）、
  模块级状态清单、测试锚点、5 阶段拆分顺序、风险评级与回归纪律。
- **零代码变更**: 未改 `app/playbook/pipeline.py`，不改变 Playbook 唯一下单管线约束。
  后续每域搬代码须独立批、独立回归、独立落档；facade 与 monkeypatch 兼容是硬门槛。

## 2026-10-08 多空不偏裁决落地批 (用户裁 F1摘/F2纯记录/F3保留G1/F4做/F5做/F6分析)

**提交**: `40b4b6a1f` (14 文件 +670/−9)。**刻意不重启** — 工作树有运维加固批在途
未提交 (其条目自明「待下一合法重启窗口」), 按军规不把他人 WIP 顺带上线;
F1 摘闸在下一合法重启后生效 (今晚 20:00 CST DA 载体首发仍处旧闸下, 多一日
旧行为无新增风险)。F5 (推送口径) 与 F2 (skill v0.95) 读工作树, 今晚 20:55 推送
与 20:00 DA 首跑即生效; F4 已执行完毕。

- **F1**: `playbook.portfolio_guardrail_enabled=false` — G4/G5/G6 砍单停用,
  影子照算照写 (G5 审计行新增 enforced 字段)。回滚: yaml 置 true。
- **F2**: skill signal_gate.py G2 降纯记录, v0.94→v0.95 (PROMPT_HASH 不变)。
- **F4**: 被拦 78 笔方向取证+回填, fwd 填充率 0%→100%; **首读: 被拦行当日
  +0.92%/T+1 +1.36% (胜率 53%/60%) — 闸净拦盈利单, 摘闸裁决获证据支持**。
- **F5**: 推送口径补 universe_expansion% (原漏 9 条可交易含 5 条 SHORT)。
- **F6**: DEB-20261008-model-long-bias-decomposition.md 落档 — 偏向非 regime
  (SPY 跌日仍 75% 多头); bearish 下模型顺势做空仅 21%/逆势抄底 62%; 对策 A-D
  待裁, 先决验证=抄底臂实战 IC。
- **守卫**: 四棘轮绿 (本批自检抓出并修复新脚本自身 W5/W6 违规 — 棘轮记忆
  第四/五次实证); E13 verify_claims 7/7 (C5 锚点迁 F4 后口径); 哨兵测试按
  「开关开=行为不变 / 开关关=零砍单+影子照写」改造。
- **遗留观察**: ①test_control_loop_ledger 红 — data_retention.py (运维加固批
  在途 cron) 未挂台账, 属他批收口范围; ②明晨核 da_checkpoint/2026-10-08
  标的数 ≈44 (载体首发实证); ③G5 首次 would-skip 影子行出现时核 enforced=false。

## 2026-10-08 运维加固批：7 项建议已落地，434 回归通过，待合法重启生效

**触发**: 用户令「从运维、性能优化、框架逻辑等角度检查项目」→ 复查后令「按照建议执行优化」。
**落档**: `docs/debates/DEB-20261008-ops-hardening-batch.md`。

- **已修**: Dashboard auto-trade YAML 先写/失败拒切换；LIVE-capable ExitPolicy 启动 fail-close；
  engine AutoGate 读取异常拒绝订单；`start.sh` lock 指纹 + `pip check` 跳过网络安装；
  systemd 删除 flock 预清锁；冒烟 waiver 自适应（严格 fail-close 解析）；`replay_cache` 90 天 retention。
- **验证**: 专项 43 passed；合并回归 `184 passed + 250 passed`（合计 434，含重叠）；
  `bash -n` / `pip check` 通过；retention dry-run `0 stale`；当前态冒烟 7/7 通过。
- **环境**: systemd 已改并 daemon-reload；crontab 已加 04:20 apply 行；依赖 marker 已预置。
- **未重启**: 工作区仍有其他 agent 在途 WIP，按 AGENT.md 不把别人 WIP 顺带上线。Python 代码待
  下一合法重启窗口生效；重启后按 AGENT.md 跑 `startup_smoke_check.py --wait 90`（waiver 会自动识别）。

## 2026-10-08 UE/DA 融合复查: 输入完整性基本成立 (今晚首发待实证), 多空不偏**未达成** (F1-F5 待裁)

> **触发**: 用户令复查 UE/DA 融合 (RFC-136/RFC-121) 的信息输入完整/新鲜度与多空偏向。

**输入完整性/新鲜度**:
- ✅ 10-07 P0 (agent job $SYMBOLS 未注入 → 只分析 11 只) 已切 no-agent 载体 +
  watchlist SSOT 实测读出 44 只; **但载体首发=今晚 20:00 CST, 尚无生产实证**
  (观察点: 明晨 da_checkpoint/2026-10-08 应 ≈44 只而非 11/22)。
- ✅ UE da_feed 融合实证 (10-07 BKV/MRVL origin="ue_candidate"); fresh-event
  影子 10-07 首跑成功 (3 候选 1 深析); cron && 串接双季行在位。
- ✅ DA checkpoint data_quality 19/19=100%; DA 深分析自取 08:00 ET 活数据,
  UE 候选只传 thesis ⇒ 融合路径新鲜度可控 (ue_quote_freshness P95 21min 陈旧
  属 UE 自身评分侧, 门未激活=正确裁决)。
- 🔴 **F5 未修**: da_bridge_dingtalk_push.py:57 仍 `LIKE 'DA-%'` ⇒ 推送口径
  漏 UE 行 (近 14 日漏 9 条可交易, 其中 5 条 SHORT) — 与多空不偏直接冲突。

**多空偏向** (DEB-20261008-direction-gates, E13 验证器我亲跑 7/7 claim 一致):
- 六道闸普查成立; 模型自身提案 77% 多头 (闸只 +1.6pp); 4 个空头清零日
  (9/21/9/22/10-02/10-07); G5 断崖 (0 空头日恒留 1 条) 已上线、零实弹未触发。
- checkpoint 级实测: 10-06 BUY16/SKIP3/SHORT3, 10-07 BUY6/SKIP3/SHORT2 —
  与 DEB L1 口径一致, 偏向源头在模型提案分布 (F6 另案)。
- **F1 (摘 G4/G5/G6) / F2 (G2 纯记录) / F3 (G1 三选一) / F4 (被拦行回填) /
  F5 (推送口径) 全部仍待用户裁决** — 摘闸方案零落地, 偏向治理未开始。

**顺手修复**: W456 第四次复发 (RFC-138 verdict_era.py 无栈 warning) →
exc_info=True 已提交 `1b37d83e2`, 四棘轮回绿。RFC-139 auto-adjudication 工作树
WIP 未动 (他批在途)。

## 2026-10-08 裁决落地批（用户裁「继续」= 三项待裁批推按建议执行）

**提交**: `c421e138a` → **main 已 ff 同步并 push**（49b8ba33b→c421e138a，74 commits；
未 checkout，服务零文件抖动）。

- **DEB-20261008 ADJUDICATED**: F1 接受阶梯止盈否决 / F2 不攒样 / F3 转介方案C H0
  另行立项 / F4 两项治理已执行（B5 待裁决报告新增「⑤ 已否决轴」附录，静默契约不动，
  7 passed；B6 exit_profiles.yaml 头部双否决警示行）。
- **RFC-099 §10.2 复核裁决**: 四触发实测均不命中 ⇒ EXT 空侧**续开观察**，零配置变更；
  n=1 不调门；两项观测债记档（研究臂细分 / dry 臂 direction 口径用顶层 direction 列）。
- 四棘轮 31 passed + doc_guard ✅ + verify_deb20261008 0 失败 ✅。

**当前待裁面**: 清空（DEB-20261008 已结、RFC-099 已裁）。下一自然到期项 =
DEB-20261007 方案 C H0 立项排期（F3 转介）+ RFC-123 双锚影子攒样（min_n=300）。

## 2026-10-08 复查批续（用户令「继续」②）：E 表刷新 + W456 收紧

**提交**: `ee5381e41`（2 文件）。无需重启（无生产代码变更）。

- **verify_deb20261007 E 表刷新**: 20 行未命中逐格实跑重证，全部为落档后数据继续
  进账（10-07 又结算 2 笔），**无一翻转结论**；E24 bp_per_trade 符号翻转
  （−0.3→+2.7bp）但 t=0.2 与零不可区分 ⇒ 门判定不变；E25 forward 缺口
  −33.9→−36.0bp（DEB-20261008 §5 引用为时点快照，方向加强）。DEB §14 新增
  第 7 轮留旧值对照表。刷新后 **E 34/34 + C 7/7 + 结构 0**。
- **W456 基线收紧**: `except_stack_baseline.json` dual_broker 7→6（愈合来自
  8047b494f 两处 exc_info 修复），tighten_ref/healed_detail 已登记，只降不升。
- 四棘轮 31 passed + doc_guard ✅。

## 2026-10-08 复查批落地 + 棘轮清剿（用户令「请复查」→「继续」）

**提交**: `8047b494f`（76 文件 +4713/−153）→ 已 `systemctl restart ftseek-us`
（12:23:06 CST），冒烟 7/7 全过（`--live-probe`，双臂安全确认 ✓）。

**复查发现并已修**:
① 本批新增 3 处无栈 warning（dual_broker query_order_fees + pipeline 对账块 ×2）→
补 `exc_info=True`；② HEAD 级棘轮旧债第三次复发（worktree 隔离实证 HEAD f3ff16fc3
即红，非本批引入）：except:pass 924→941(+17)、W456 +4、UTC-4 18→19(+1)。
全量就地清剿 25 处（pipeline 4 改 warning+exc_info / intraday_diagnose_late fail-open
留痕 stderr / scripts 15 坏行计数进 print·stderr / gate_attribution ET 改
ZoneInfo("America/New_York") 冬令时不错位），except:pass 基线 924→**921** 调低
（含净愈合 3 处），零控制流变更；W456 基线未动（dual_broker 愈 1 格 6<7，
收紧须 DEB 落档留下批）。**四棘轮 31 passed 复绿**。

**部署注意（已闭环）**: 服务 10:31 重启后 pipeline.py 又于 11:25 改（fee_broker 修复）
⇒ 运行进程一度是初版；期间无 LIVE 平仓、对账路径未触发，无影响窗口。12:23 重启后
修复版已生效。

**既有环境债（HEAD 基线实证，与本批无关，未动）**: `test_offline_full_chain` 18 errors
（driver 子进程 `require_auto_sim()` 读生产 LIVE 配置抛错）+ `test_window_isolation`
1 failed — 建议另批核查 driver 缝表/私有清单登记（offline-chain-driver-private-patch-list
记忆同型）。

**遗留**: verify_deb20261007.py 20 格 E 表陈旧（DB 落档后续进账所致，同 10-08 晨批
DEB-20261008 E 表刷新同型），建议另批刷新；DEB-20261008 F1–F4 与 RFC-099 §10.1
复核裁决仍待用户。

## 2026-10-08 Hermes 批次复查（用户令「复查分析」）

**复查范围**: RFC-137 LIVE 券商实收费对账、DEB-20261008 盈利阶梯分批止盈、
DEB-20261007 第 5/6 轮、Futu push 交易所时戳、W7 分母探针、RFC-099 §10.1。

**三处须修缺陷（均已修）**:
① RFC-137 初版在 `_close_trade_record` 内直调 `get_dual_broker()` 单例 →
p0 单测（PLAYBOOK_LIVE 行）绕过注入的 FakeDualBroker 打开真实 Futu socket，
合并跑 `live_arm_p0 + exit_recovery` 出现 `55 passed + 19 errors`（I/O guard 捕获
后台 `socket.send`）。**HEAD 基线同命令 55 全绿 ⇒ 非既有隔离问题，是本批回归**。
修复: `_close_trade_record` 新增 `fee_broker` 注入参数（两个调用点传链路已有的
`dual`），并加回归守卫 `test_broker_stop_reconciliation_needs_no_broker_singleton`；
修复后四套件合并 `75 passed`（含新守卫）。
② DEB-20261008 声称的验证器 `scripts/verify_deb20261008.py` **不存在**（「一键验证」
命令必失败）。已按 `verify_deb20261007.py` 契约补建（E 表 21 行逐格实跑 + 结构守卫 +
「同 E#」引用复用），当前 `21/21 命中, 0 结构问题`。
③ E 表 6 格陈旧/手抄偏差（E4 漏 `$` 列、E6 83.9→83.8、E7 464→465、E15 N=417→419
cap/一致性漂移、E19 命令 `\\"` 转义致 shell 不可跑、E20 N=525→527/-0.780→-0.764/
+1.125→+1.109pp）——全部为落档后 DB/影子数据继续进账所致，**无一翻转结论**，已按
验证器实测值刷新；B2/§2/§6/§12 同步。

**其余核验通过**: 生产回填 PBR/CEG/BKV 三行 `fees_source='broker'` 与 RFC §5 表逐值
一致（毛 +$1.66 / 费 $11.97 / 净 −$10.31，备份存在）；Futu SDK push 行 schema 实为
`data_date/data_time`（无 `update_time`）⇒ 时戳修复方向正确；`order_fee_query` SDK
签名/返回列（order_id/fee_amount）对上；DEB 挑战点 2（新架构八腿重算：MFE≥2% n=79
落袋中位 2.22% WR 98.7% ≈ 全样本）与挑战点 5（费用减半 26→29 笔 / 26.3%→29.3%）
独立复算维持结论；RFC-099 §10.1 抽查（`if detail:`/`if dry:`/crontab 无 `--dry`）属实。

**测试**: `26 passed`（费用对账/dual_broker/W7/LV3 时戳）+ `207 passed`
（加 entry_revalidation 五文件）+ `75 passed`（p0+exit_recovery 等四套件合并）+
DEB 验证器 21/21 + `py_compile` 通过。

## 2026-10-07 DA/UE 融合首跑复核与推送口径修正（用户令「继续」）

**结论**: DA 主链与 UE 融合正常。今日 bridge 为 **5 个 DA 候选**; 生产 DB 最终
**5 条 DA 指令**, 其中 **2 条可交易** (`US.CEG BUY`, `US.BKV BUY`), **3 条 SKIP**
(`US.PENG/US.NOK` 方向偏斜, `US.MRVL` 半导体集中度)。旧推送把「候选5」与
「instruction直写DB」并列, 未写最终可交易数, 已修正。

**UE/DA 融合**: 08:00 ET `da_feed` 主链融合正常, `US.BKV/US.MRVL` 带
`origin="ue_candidate"`; UE 直写腿 0 条为退役设计。09:12 fresh-event 影子批首跑因
`candidate_count/candidates_total` schema 错配失败, v0.94 已兼容; 09:24–09:29 重跑
成功, 仅 `US.RARE` 深分析 (`ALKT` 不可借券, `SHEL` 低 ATR), 未写生产 DB。

**修正**: `scripts/da_bridge_dingtalk_push.py` 现在只读查询生产 DB, 推送写明
「候选 / DB可交易 / guardrail SKIP」三口径, 逐标的显示 DB 状态与 SKIP 原因, 并写
`data/da_push_instruction_summary.jsonl` 台账; 新增
`tests/test_da_bridge_dingtalk_push.py` 固化 5→2 口径。详细记录见
`docs/decisions/RFC-136-ue-fresh-event-da-batch-and-window-visibility.md` §6。

## 2026-10-07 DEB-20261007 第6轮：DA LLM 盘前定价时窗与 entry_price 可行性（用户追问）

**问题**：entry_price 能否由 DA LLM 基于盘前（什么时间段）+ 历史数据 + 预判找到合理价格？

**机制现状（读码）**：这件事**系统已在做** — DA 主链窗 **07:55–08:15 ET**
（`ops/hermes/da_main_carrier.py` 守卫），更早 deep-stock-analysis 扫描窗 04:00–06:00 ET；
`planned_entry` 即 DA LLM 直写（`execution_plan.entry_price`），prompt 硬规锚定盘前有效现价
（严禁锚昨收）并注入盘前异动 + 技术/基本面/新闻历史数据段；盘中触发时机械 reprice 到实时 bid/ask。

**实测（新脚本 T1–T4）**：晚定价只机械减小位移（04–06 时 |dev| 中位 2.103% vs 07–08 时
1.436%，ρ(小时,|dev|)=−0.13），但**不改结局** — 早窗 mfe 0.72% 反而高于主链窗 0.59%；
DA 内部 Spearman(|dev|,mfe)=+0.08 / (adverse,mfe)=+0.08 / (|dev|,pnl)=−0.00 全近零。
与 DEB-20260916 §7.3（DA 精确取值 ρ 全<0.15）和本 DEB P1 六刀三层互证：
**换时窗/换 prompt 找「更合理入场价」不是可测收益杠杆**；观察通道 = RFC-123 双锚影子（已落地）。

**入库**：`scripts/deb20261007_da_timing_window.py`（只读，任意 cwd rc=0）；DEB §14 第 6 轮；
E30 计数 24→25。零生产参数改动、零行为变更、无需重启。

## 2026-10-07 DEB-20261007 第5轮：盘中 reprice / 盘前计划价获利空间分析·独立复查（用户令「请复查」）

**复核结论**：第 4 轮三条结论**全部维持**——①入场价位移轴无判别力（P1 六刀全灭复现）；
②「等回踩」负选择成立且被加固（LONG/SHORT 分层差 −$67.02/−$79.43 同向、剔最大符号仍 −$52.43）；
③MFE 杠杆在出口结构不在入场价。「不落地 / 不提案」决策不变。

**新发现 4 项须修缺陷**（均不推翻结论，已落自曝 #17–#20 + §14 第 5 轮）：
①`mfe_driver_scan.py` join 双计 4 笔（去重后 SL_TRAIL_LOCK +$6,638 / SL_INITIAL −$4,422 / Spearman n=594）；
②`r4_falsification.py` X3 结论行未随自曝#16 更新（仍印「方向有利」）+ 省额公式高估 1.27%；
③X0 的 mae 口径核查对 SHORT 空转（386/396=97% 只约束 LONG；生产码+独立验 30/34 证明结论仍对）；
④P0 头条 n=645 混入 236 笔从未 reprice 的未执行指令（executed-only 中位 1.5444% vs 混合 1.0363%）。
次级：§14 K/R/X 表无机器验证覆盖（verify 只扫 E/C 行首）；状态头「已落码 16」过期未同步。

**入库**：`scripts/deb20261007_review5_recheck.py`（V1–V7 独立对抗核验，只读 DB，任意 cwd rc=0）；
DEB §14 新增第 5 轮 + 自曝 #17–#20；E30 脚本计数 23→24。零生产参数改动、零行为变更、无需重启。

## 2026-10-07 DEB-20261007 F1–F5 落地批（用户裁「按建议执行」）

**裁决**: F1 落地（RFC-123 阶段0 双锚影子）/ F2 修（注册表判据串台）/ F3 关闭留痕（方案 C）/
F4 并入既有载体（判据实测值栏）/ F5 核实后自动关闭（推送层 G3 确在动态算）。

**提交**: `98590b07b`（F2）、`7b7e718fe`（F1+F4）、+ 本批（核验环 + 台账 + 文档）。
DEB 验证器 `scripts/verify_deb20261007.py` = E 表 34/34 + C 表 7/7 + 结构 0。

**F1 埋点要点**（`app/playbook/pipeline.py`）: 模块级纯函数 `_resolve_generated_anchor`
（三级优先级 `raw.planned_entry` → band 沿反推 → `missing`，**`missing` 绝不回退 DB
`entry_price`**）+ `_anchor_trigger_geometry`（逐分支复刻生产判定段）；`_emit_entry_shadow`
加嵌套 `dual_anchor` 块（schema `rfc123-s0-v1`），既有平键零改动 ⇒ W7 哨兵 /
`eval_vwap_gate` / `entry_gate_daily_report` 不受影响。开关
`playbook.execution_parameters.entry_anchor_dual_shadow_enabled: true`（**改它须重启**，
HotReloader 穿不透 `playbook.*`）。注册表条目 `entry_anchor_dual`
（`min_n=300`、`count_mode=where:anchor_recorded=True`、判据逐字引 RFC-123 四条）。

**⚠️ 生效状态**: 已 `systemctl restart ftseek-us`（16:26:16 CST > 提交 16:24:16），零 ERROR、
4 持仓恢复、Broker 对账一致。但重启时刻是 **ET 04:26 盘前**，`_check_intraday_trigger`
未被调用 ⇒ 生产 `anchor_recorded` 行数当时 = 0，**接线状态不可判**（不得以「测试全绿」
声称生效）。已挂一次性核验 job `fe648f3aa7e0`（CST 22:10 ≡ ET 10:10）读生产 jsonl 真值
定三态 LIVE/DEAD/TOO_EARLY；DEAD 时按脚本内三步排查（grep `[双锚影子]` warning /
核 `pb_cfg` 真读到开关 / 核启动时刻）。

**遗留债**: ①`entry_anchor_dual` 的 `eval_script` 未写（阶段1 裁决前必须补，否则重演台账
「有入口无出口」）；②RFC-123 阶段1（锚持久化分离 = 真行为变更）**未授权未动**，须待影子期
≥10 交易日读数后另案裁决；③方案 A 采集面（指令×轮）是判据人群（指令×ET 日）的**超集**，
结算器须给两口径分层读数。

## 2026-10-07 LIVE 工程探针多持仓扩展（3仓 / 每单1股）

**用户裁决**: 为验证 LIVE 多持仓股场景，`max_open_positions` 从 1 调整为 3；
`max_order_qty` 仍固定 1 股，继续先跑通全链路。

**边界不变**: 仅 LONG/BUY、仅 RTH、SIM 常驻；3 仓上限只作用于 LIVE
`PLAYBOOK_LIVE` 自动持仓，不管理/平仓手动仓；每笔新仓仍逐笔挂 broker STOP，
退出链路沿用“STOP 成交回补或规则退出前释放自有 STOP”的 P0 闭环。

**授权/破例**: 配置授权 ID 更新为
`AUTH-20261007-3POS-1SHARE-LONG-RFC132`；四锁 waiver 仍只覆盖
`ALLOW_REAL_TRADING=true`，到期时间不变，范围更新为最多 3 仓 / 每单 1 股。
原 1 仓 waiver 已备份到
`data/backups/live_probe_lock_waiver_before_3pos_20261006.json`。

**门禁**: `LIVE_PROBE_LIMIT_INVALID` 继续拒绝 `max_order_qty!=1` 或
`max_open_positions∉[1,3]`；`open_live_positions>=3` 时拒绝新仓。当前 PBR
计 1/3，满足其他条件时可自然镜像下一个 BUY 探针。

## 2026-10-06 23:55 LIVE 规则退出前释放自有 broker STOP（P0 闭环）

**背景**: 首笔 PBR LIVE 探针成交后，broker STOP 已补挂为 `WAITING_SUBMIT`。
复核发现既有 `ExitLifecycle.preflight` 会把该 SELL STOP 视作活跃平仓单并触发
`exit_unowned_active_order`。这防双卖是对的，但导致在线规则退出（TP/时间/EOD）
无法在同一套链路里平仓；DEB-20260910 已将其列为实盘部署阻塞项。

**修复**:
- `DualBrokerAdapter.query_order_status()`：只读查询订单身份/状态/成交证据；
- `_release_live_broker_stop_for_rule_exit()`：只释放
  `pb_stop_<symbol>_real`、同 symbol/方向/数量的本策略 STOP；
- 顺序固定为：先回补已成交 STOP → 未成交则撤单 → 复查到取消类终态且零成交
  → 才提交规则平仓；身份不匹配、部分成交、撤单未终态一律 fail-close 告警；
- 覆盖三条 LIVE 平仓路径：SL 兜底、常规规则退出、EOD 兜底。

**验证**: `tests/execution/test_dual_broker_positions.py`、
`tests/playbook/test_live_arm_p0_contracts.py`、`tests/test_rehang_live_stop.py`、
`tests/playbook/test_dual_arm_routing.py` 共 35 passed；退出生命周期扩展回归
`tests/execution/test_exit_lifecycle.py + tests/playbook/test_exit_recovery.py`
共 155 passed。23:50 已重启生效，退出快循环恢复，PBR 仍在监控，券商 STOP 未被
测试性撤单。

## 2026-10-06 23:30 首笔 LIVE 探针成交 + STOP 事故修复全链留痕

**事件**: 21:38 ET(09:38) US.PBR LIVE 1股 @24.0171 真实成交 (futu_id
FH1D3F38AC542E8000, 同 decision_id SIM 臂 247股 @24.02, 对照滑点 -0.012%);
broker STOP 挂单被拒「下单缺少必要参数触发价格」→ fail-close 按 RFC-132
设计精确执行 (CRITICAL + live_arm_halt.json + 钉钉 HIGH ×2), 仓位保留。

**根因**: dual_broker.place_protective_stop 只传 price 未传 aux_price
(富途 STOP 触发价必须走 aux_price — 仓内 8/5 已知, probe_sim_stop_20260805.py:44
+ 看板 executor.py:1236 生产先例); 该函数生产里程为零, 首跑即暴露。
连带: 未传 time_in_force (默认 DAY 当夜失效, W_WEEK 跨日持仓失去兜底)。

**修复链** (全部已提交):
- `9014d3ca1` STOP 修复: price=0 + aux_price + GTC + trail_spread=0 + 契约钉×3;
- `3a9daca25` 测试隔离上提: HALT/waiver fixture 从 tests/playbook/conftest.py
  上提 tests/conftest.py (修根目录 test_rfc080 读生产 waiver 恒红, 同型病灶);
- 23:16 重启生效 (冒烟 --live-probe 7/7);
- `scripts/rehang_live_stop.py --apply` 23:23 补挂 PBR STOP 成功
  (futu_id FH1D3F50CF57F79000 @22.96 GTC), 回执双台账已核
  (open_positions.sl_broker_order_id + trade_records.metadata.broker_stop);
- HALT 人工清除 23:25 (备份 data/backups/live_arm_halt_cleared_20261006_2325.json);
  门函数复测: 唯一拒绝理由 = LIVE_PROBE_OPEN_CAP_REACHED (PBR 在仓, 1 仓帽在位,
  HALT 已解) — 探针授权恢复, 待 PBR 了结后下一信号自动镜像。

**教训**: ①仓内已有知识必须检索复用 (8/5 探针脚本就记录过 aux_price);
②preflight P6「STOP 生产里程=0」的警示在探针前未做人工三段演练, 直接首跑暴露 —
下次授权前把「演练里程≥1」当硬前置执行, 不接受跳过。

## 2026-10-06 22:21 UE新鲜事件并入DA当日候选池（Phase 1影子）

**触发**: 用户裁「UE扫描做当日新鲜事件做当日候选池」。

**架构边界**:
- UE保留事件发现/供料/确认门影子，**不再由21:10 cron直接 `inject/push`**；
- DA成为唯一指令写出方：新增 `--fresh-event` 增量批，显式消费UE `premarket` 批次；
- 默认**隔离影子**：`--no-db-write --no-pool-write`，输出到
  `data/shadow/da_fresh_event/<ET日期>/`；观察合格后再切 `--execute` 写生产指令。

**改动**:
- DA skill v0.92：`_load_ue_short_candidates(window=...)` 支持显式窗口；
  `Pipeline(--ue-window, --ue-only)` 与 CLI `--ue-window/--fresh-event`；
  fresh批复用当日 `opportunity_scan` 市场上下文，跳过Stage1重扫，只分析UE候选。
- 常规DA主链增加ET 07:55-08:15启动窗；off-season发车返回
  `DA_MAIN_ET_WINDOW` skip JSON，不扫描/不调LLM/不写库，人工补跑用`--force`。
- FTSeek新增 `scripts/da_fresh_event_batch.py`：ET 0900-0929启动窗、交易日守卫、
  UE批次1h时效、当日幂等、事件审计 `data/shadow/ue_fresh_da_candidate_pool.jsonl`。
- UE `cmd_scan` 增加ET窗口守卫（da_feed 0730-0805 / premarket 0900-0929），
  `--force-window` 仅测试/人工补跑；防止DST双季双挂时off-season发车写批次。
- 新增Hermes no_agent作业 `9476e44276dc`（`0 21 * * 1-5`）与载体
  `ops/hermes/da_main_carrier.py` → `/root/.hermes/scripts/da_main_carrier.py`：
  EST 21:00 CST=08:00 ET执行主DA；EDT 09:00 ET由窗口守卫跳过。

**自动化层**:
- 备份：`data/backups/crontab-before-ue-fresh-event-20261006.txt`
  (sha256 `4650db473ed96f827cdb70b341ff0501763f88023464e3fc75f5831da808ce08`)；
- 21:10原`scan→inject→push`替换为`scan→da_fresh_event_batch.py`（影子）；
- UE premarket双季双挂：EDT `10 21` / EST `10 22`；越窗发车由脚本守卫拒绝；
- UE da_feed双季双挂：EDT `00 20` / EST `00 21`；越窗发车由脚本守卫拒绝；
- 回滚：`crontab data/backups/crontab-before-ue-fresh-event-20261006.txt`。
- Hermes jobs备份：`data/backups/hermes-jobs-before-da-est-carrier-20261006.json`
  (sha256 `4884c127639e53577d1966b6dbb9bcfc9857af28a5572f8e45898f484370569d`)；
  回滚 = 还原该JSON并删除新增job/载体。

**验证**:
- `py_compile` 通过；
- `tests/test_da_fresh_event_batch.py` 3例通过；
- `tests/test_da_main_carrier.py` 1例通过（窗外静默+审计落盘）；
- `tests/playbook/test_da_candidate_selection.py` + `tests/test_da_output_contract.py`
  **130 passed**（含fresh-event窗口/ue-only契约、DA主链ET窗与v0.92版本棘轮）；
- `tests/playbook/test_ue_funnel_batch.py::TestInjectWiring` **8 passed**；
- 上述fresh/main/candidate/output/UE接线合并集 **142 passed**；
- 冒烟：当前ET 10:18在窗外，wrapper正确记录`skipped_outside_window`且不启动管线。
- DA主链窗口冒烟：当前ET 10:28运行`--mode full`返回
  `skip_reason=DA_MAIN_ET_WINDOW`且零扫描/零LLM/零写库；
  EST载体从`/root/.hermes/scripts`无PYTHONPATH直跑，当前ET 10:33正确跳过且
  stdout/stderr=0（载体自身不import仓内app，交易日/watchlist均走venv子进程）；
  新Hermes作业45s后仍为`scheduled/enabled`，next_run=`2026-10-07 21:00 CST`。
- 结构化对账：Hermes jobs 35→36，仅新增`9476e44276dc`，既有作业零字段变化；
  crontab目标diff仅包含UE da_feed双季、UE premarket双季与fresh-event影子批。

**待裁决**: 影子观察合格后，将cron中的 `da_fresh_event_batch.py` 加 `--execute`
切换为生产DA增量指令；此前UE直注保持退役，不放宽`require_auto_sim`。

## 2026-10-06 21:05 DA cron 执行模式收口: 长命令首发即后台

**触发**: 用户令「按照建议执行优化，确保正常」(基于 10-06 DA 全链路现场观察)。

**病灶 (10-06 实证)**: cron agent 首发用前台/顺序模式跑 `pipeline.py`，被 hermes
终端工具 420s 顺序超时在 Stage 1 扫描刚完成时杀掉 —— DA 产物零落盘 (已核查无重复
副作用)，白烧一轮全市场扫描 (~7-10min Futu 配额) 后才转后台重跑成功。属 agent
执行模式选择的非确定性问题, 非管线缺陷。

**改动 (自动化层, 单点)**: `~/.hermes/cron/jobs.json` 作业 `e0d574cc1a3c` 的
`prompt` 字段 Step 3 处新增「执行方式」两条: ①**首发即用 terminal background=true
模式执行** + 输出重定向模板 + 轮询进度判据 (进程存活 / 检查点数递增 / `重活完成
N/M` 推进); ②**禁止前台/顺序模式执行**, 注明 10-06 实证教训。

**留痕与回滚**:
- 备份: `~/.hermes/cron/jobs.json.bak-rfc133-bgmode-20261006`
  (sha256 `badcc4bd478032acf1109fa4739e5a293359febe13863fdb88cd0da85f3ec59b`);
- 结构化 diff: 全部 35 个作业逐字段比对, **仅** `e0d574cc1a3c.prompt` 变化
  (4125→4596 字符), schedule/enabled/state/next_run_at 全部原样;
- 回滚 = 还原备份文件 (Hermes scheduler 每 tick 重读 jobs.json, 无需 reload)。

**验证**: JSON 合法加载; 写入 90s 后复查 prompt 存活、作业 `scheduled/enabled`、
`next_run_at=2026-10-07T20:00`、`last_status=ok`; hermes gateway 存活
(PID 3896515); 当前无 DA 管线进程在跑, 明晚 20:00 生效。

## 2026-10-06 13:42 双臂切换优化复查与探针安全补强

**触发**: 用户令「美股昨晚已经完成交易，另外的 AGENT 也针对实盘和 SIM 切换做了优化，请复查」。

**复查结论**:
- `PlaybookDB._conn()` lane 隔离修复有效：`route_playbook_db("sim")` 走研究/SIM 库，
  `route_playbook_db("real")` 走 `data/ftseek_us_live.db`；
- 昨晚未发生 LIVE 探针成交：SIM 主库 `PLAYBOOK_LIVE=0`，LIVE 库今日 `0`、
  LIVE 自动仓 `0`，`HALT` 不存在；
- 发现 SIM 指令 `3707 US.ORCL`、`3710 US.GEO` 的 `metadata_json` 残留
  `dual_arm.target_env="real"`；交易记录、持仓与执行臂本身干净，仅元数据污染。

**安全补强**:
- LIVE waiver 必须携带非空 `authorization_id`，且必须与
  `playbook.live_manual_arm.probe.authorization_id` 精确一致；
- waiver 必须只覆盖 `ALLOW_REAL_TRADING=true`，不能放宽其他四锁；
- `NOTIONAL_TOO_SMALL` 探针豁免收紧为 `BUY + position_size=1 +
  target_env in {real,live} + mode=engineering_probe`，防止标记污染放大敞口；
- 修正 waiver 字段校验优先级，保留“过期时间不可解析”的原始失败原因。
- 终检补齐两个 STOP 边界：券商 STOP 先成交时，退出监控会按 STOP 单成交
  证据补记 `SL_BROKER_STOP` 并删除 open position；平仓清理改用
  `entry_broker_order_id` 验证入场身份，不再把 STOP 单号误当入场单号；
- LIVE 手动持仓检查异常时 fail-close 拒绝新仓；SIM 保持原放行语义。

**数据清理**:
- 备份：`data/backups/ftseek_us_before_dual_arm_metadata_cleanup_20261006_134058.db`；
- 仅删除上述两条 SIM 指令的 `dual_arm` 键，未覆盖其他 metadata；
- 清理后两行 `broker_env=sim`、`dual_arm` 残留 `0`。

**验证与重启**:
- 终检定向契约 **146 passed**：waiver / admission / dual-arm routing /
  quote gate / exit compensation / LIVE P0 / startup mode / risk arm /
  atomic mode switch / startup smoke；
- `py_compile` 与 `git diff --check` 通过；
- 13:41:38 CST 重启，新 PID `4058947`，环境仍为 `TRADE_ENV=1`、
  `AUTO_TRADE_ACCOUNT=sim`、`ALLOW_REAL_TRADING=true`；
- `startup_smoke_check.py --live-probe` **7/7 通过**；
- LIVE gate 当前唯一拒绝为 `LIVE_MARKET_PHASE_REJECTED`（隔夜），
  RTH 后具备执行条件。

**边界提醒**:
- `cutover_preflight --to live --no-write` 对“全量切换”仍报
  `P1 graduated=False` BLOCK，另有 broker STOP 里程、研究臂连续性 WARN；
  `--skip-gate` 后 BLOCK=0；这说明**全量切换未毕业**，不阻断已授权的 1 股工程探针；
- 旧 `/api/mode/switch live_*` 实测返回 **410** 且无写文件/无重启；
  `switch_mode("simulate")` 在双臂健康态为 no-op，不会误关 LIVE 探针授权。
- 探针仍只允许 LONG/BUY、1 股、1 仓、RTH；SHORT 拒绝；
- 若要临时停 LIVE 新仓，优先删除 `data/live_probe_lock_waiver.json` 或落
  `data/live_arm_halt.json`，不改全局 SIM 常驻配置。

## 2026-10-05 LIVE 1 股工程探针授权与重启验证

**触发**: 用户明确指令「现在重启下一步，授权1股探针」。

**授权边界**:
- 授权 ID: `AUTH-20261005-1SHARE-LONG-RFC132`；
- 仅允许 **1 股、1 仓、LONG/BUY、RTH 工程探针**；
- 全局仍保持 `TRADE_ENV=1`、`AUTO_TRADE_ACCOUNT=sim`，SIM 常驻；
- SHORT 继续拒绝，不放大仓位，不自动平仓手动仓。

**实施**:
- `.env.us` 设置 `ALLOW_REAL_TRADING=true`；
- `config/unified_config.yaml` 设置 `playbook.live_manual_arm.enabled=true`、
  `probe.authorized=true`、`authorization_id=AUTH-20261005-1SHARE-LONG-RFC132`；
- 2026-10-05 22:03:06 CST 重启服务，新 PID `3972792`；
- 启动段确认 `broker=sim db=sim ALLOW_REAL_TRADING=True`。

**验证**:
- `startup_smoke_check.py --live-probe` **7/7 通过**；
- LIVE gate 只读评估 `allowed=true`，HALT 不存在，LIVE 自动仓 `0`；
- REAL 账户只读刷新成功：总资产 `$75,595.25`，购买力 `$32,066.01`；
- `cutover_preflight` P5 已清零；LIVE 库已备份并预迁移
  `entry_broker_order_id`，切换日有效补列 `0`；
- 22:03 轮 SHORT `US.TSLA` 被正确拒绝 (`LIVE_PROBE_DIRECTION_REJECTED`)；
- 剩余 BUY 触发分别被 chase cap / VWAP 证伪门拦截，未绕行。

**冒烟脚本补丁**:
- `startup_smoke_check.py` 新增 `--live-probe` 显式模式；
- 全局路由仍必须 `sim`，仅授权探针允许 `ALLOW_REAL_TRADING=True`；
- 启动段终止于 `[PlaybookExit] 快循环已启动`，不再把运行期 pending 回报误判为启动失败；
- 相关测试 `24 passed`。

## 2026-10-05 LIVE 探针前 P0/P5 补丁: 入场/止损单号语义拆分 + STOP 收据 SQL 修复

**触发**: 用户令「继续」。盘中实链路核查后继续下钻，发现两个启用前硬伤:

1. `TradeStore.upsert_position_simple()` 把**入场单号**写入
   `open_positions.sl_broker_order_id`。该字段被 OCO/止损替换逻辑按**真实 STOP 单号**
   消费，语义碰撞可能导致撤错入场单或止损替换被误判。
2. `PlaybookService._record_live_stop_receipt()` 更新 `open_positions` 时使用
   `source_type='PLAYBOOK_LIVE'`，但该表**没有 `source_type` 列**。首次 LIVE STOP
   记账会抛 `OperationalError`，被上层当作 STOP 失败并触发 `HALT_NEW_LIVE`。

**修复**:
- `open_positions` 新增 `entry_broker_order_id`，入场单号只写该列；
- `sl_broker_order_id` 插入时保持空，重 upsert 不覆盖既有 STOP 单号；
- LIVE STOP 收据按 `symbol + account_type='live' + owner_strategy='playbook_live'
  + owner='auto'` 定位仓位，并回写 entry/STOP 两个单号；
- 若找不到对应 LIVE 自动仓，STOP 收据 fail-close。

**验证**:
- `tests/playbook/test_live_arm_p0_contracts.py` 新增两个防回归测试：
  入场/止损单号语义拆分、真实 STOP 收据 SQL；
- 定向测试 **55 passed, 1 skipped**；`py_compile` / `git diff --check` 通过；
- `cutover_rehearse.py --to live` 副本彩排通过：切换日阻塞性 DDL 残留 0，
  SIM↔LIVE 资金基线回切可恢复；
- `TradeStore` 启动迁移已包含幂等数据修复：仅当 `sl_broker_order_id` 与同标的
  `trade_records.futu_order_id` 精确匹配时，才搬入 `entry_broker_order_id` 并清空；
  未重启、未改生产库、未授权 LIVE。当前预检 P5 仍会命中旧行，重启后应复核清零。

## 2026-10-04 RFC-134 复查补记: 16:03 重启入档 + 修复通道边界 + 工作树混批风险

**触发**: 用户令「claude 已经执行落地，请复查」。独立复查 RFC-134 全部改动面
（`main.py` 启动守卫 / `mode_switch_service.py` 410 语义 / risk 三模块 SIM 分臂 pin /
Dashboard UI 退役 / 路由 / 测试），并核验当前运行态。

**复查结论**: 通过，可保留上线。关键证据:
- 定向契约 **42 passed**（startup_mode + ACB pnl + arm risk context + atomic mode switch）；
  `tests/risk + tests/web_dashboard` 全量 **337 passed**。
- `py_compile` / Jinja parse（system+dashboard）/ `node --check`（dashboard.js +
  system.html 内联 JS，经 `{% block scripts %}` 提取）/ `git diff --check` 全过。
- 行为实测: `switch_mode("live_normal")` → **410** 无写文件/无备份/无重启；
  `switch_mode("simulate")` 健康态 no-op 不误关 `ALLOW_REAL_TRADING`；
  `set_auto_account("real")` → **410**。
- SIM 风险二线实测: CapitalSafety sim → `data/ftseek_us.db`；ACB target_env=sim；
  `TRADE_ENV=0` 注入下三模块均不漂 LIVE（钉子测试覆盖）。
- 四锁全关: `TRADE_ENV=1` / `AUTO_TRADE_ACCOUNT=sim` / `ALLOW_REAL_TRADING=false` /
  `live_manual_arm.enabled=false` + `probe.authorized=false`；live 库
  `PLAYBOOK_LIVE` **0 行**；sim 未平仓 **0**（周日休市）。

**16:03:57 重启入档（原 RFC-134 记录缺失）**: 当前进程 PID `3883669`（16:03:56 起，
`NRestarts=0`，`ExecMainStatus=0`），启动段 16:04:03 `StartupMode 模式一致:
broker=sim db=sim`。本会话 18:56 补跑冒烟 **7/7 ✓**（"启动段早于本次重启"提示
是因为运行时刻距重启 3h、未伴随新重启 —— 反假阳性守卫按设计工作，非故障）。
**该重启的发起者/原因未在任何 handoff 条目认领** — 如属其他会话请补录；本条至少
保证"谁在跑什么"可追溯。

**修复通道边界（鸡生蛋，非缺陷）**: 若 `.env.us` 真漂移到 `AUTO_TRADE_ACCOUNT=real`
或 `TRADE_ENV=0`，启动守卫拒启 ⇒ 主进程（含 dashboard/API）不可用 ⇒ API 修复通道
结构上不可达。三条出路已具备: ① 拒启前 HIGH 推送先行发出（值班可见）；
② 手改 `.env.us` 三键回 SIM；③ `FTSEEK_ALLOW_LEGACY_GLOBAL_FLIP=1` 逃生门
（仅显式测试/回滚）。已补进 RFC-134 文档「复查补记」节。

**工作树混批风险（待用户裁）**: 当前未提交面含 **RFC-132/133/134 三批运行时代码 +
本会话 3 个纯文档**共 20 文件；`data/cutover/preflight_baseline.json` 的重冻结
（00:27:52）属前夜更早批次，与 RFC-134 混放。任何人下次重启会**全量带上**。
建议尽快**分批 commit**（每批独立回滚锚）；按军规 commit 须用户明确指令，本会话未提交。

## 2026-10-04 RFC-134 旧全局实盘切换退役: UI/API 410 + 启动防误翻 + SIM 风险二线

**触发**: 用户令「根据建议执行修复」。目标是消除旧 `/api/mode/switch` 全局翻转入口与
双臂常驻模型的冲突，防止误配后 SIM 研究臂停摆、非 Playbook 默认路由漂到 LIVE 库。

**实施**:
- `switch_mode(live_*)` 与 `set_auto_account("real")` 改为 fail-closed `410`，
  不写 `.env.us`、不备份、不重启；返回码 `LEGACY_GLOBAL_REAL_SWITCH_RETIRED`。
- `switch_mode("simulate")` / `set_auto_account("sim")` 只保留为旧全局快照修复通道；
  正常 SIM_ONLY 下 no-op，不误关 LIVE 预授权；漂移修复时写
  `AUTO_TRADE_ACCOUNT=sim / TRADE_ENV=1 / ALLOW_REAL_TRADING=false`。
- `main._validate_startup_mode_or_exit` 新增生产守卫：`AUTO_TRADE_ACCOUNT=real`
  或 `TRADE_ENV=0` 拒绝启动并 HIGH 推送；仅
  `FTSEEK_ALLOW_LEGACY_GLOBAL_FLIP=1` 允许显式测试/回滚。
- 系统页移除旧“实盘保守/标准”卡片；Dashboard 移除 REAL 自动账户按钮。
- SIM ACB/OKR/CapitalSafety 显式走 `get_trade_records_db_for_arm("sim")`，
  在注入 `TRADE_ENV=0` 时仍不漂到 LIVE 库。

**边界**: 本批不实现新的 `/api/live-arm/*` 控制台，不打开
`ALLOW_REAL_TRADING`，不填写 `probe.authorization_id`，不构成 LIVE 启用授权。

**验证与部署**:
- 定向契约 **34 passed**；风险 + LIVE 臂 + 退出补偿 **232 passed**；
  Dashboard 全量 **141 passed**。
- `py_compile`、Jinja parse、`node --check`、系统页内联 JS `node --check`、
  `git diff --check` 均通过。
- `cutover_preflight.py --to live --no-write` 维持 `block=1 warn=4`，
  唯一硬阻塞仍为毕业门禁。
- 2026-10-04 01:01:45 CST 重启加载；PID `3832796`；
  `startup_smoke_check.py --wait 90` 7/7 通过，warning/error 为 0。
- 当前 `TRADE_ENV=1`、`AUTO_TRADE_ACCOUNT=sim`、`ALLOW_REAL_TRADING=false`、
  `live_manual_arm.enabled=false`、`probe.authorized=false`；无 HALT 标记。

## 2026-10-04 RFC-132 LIVE 双臂 P0 硬化: 分臂风险/资金/手动仓/STOP, 预置完成并重启

**触发**: 用户令「执行修复」，目标是下周可裁决是否做 1 股 LIVE 工程探针；SIM 必须常驻。

**用户裁决 (2026-10-04)**: 已接受策略毕业门禁知情破例，仅限 1 股 LONG
工程探针；不改变策略转正红线，不构成 LIVE 启用授权。`probe.authorized=false`
与实盘开关仍保持关闭，正式执行前需填写 `probe.authorization_id`。

**修复**:
- LIVE capital 独立实例 (`get_capital_safety_system_for_arm("real")`)，固定
  LIVE ledger + `trade_env=0`；执行前从 REAL broker 刷新资金，失败 fail-close。
- AccountCircuitBreaker / AbsoluteCircuitBreaker / symbol day loss / DualBroker
  名义本金兜底全部按 `target_env` 分库分资金；ACB 分 SIM/LIVE 两个实例。
- ManualHoldRegistry 内部键改为 `(account, symbol)`，SSOT fallback 走
  `get_trade_records_db_for_arm(target_env)`；同 symbol 双臂互不覆盖/互不注销。
- LIVE broker STOP 强验证：必须 `success=true + futu_order_id`，写
  trade/open-position receipt；失败 HIGH 推送 + 持久化 `live_arm_halt.json`，
  HALT_NEW_LIVE，不平既有仓。
- `scripts/prepare_live_probe.py --apply` 完成 LIVE schema 迁移、8 条
  size=0 manual 僵尸行清理、REAL 资金基线预置；写前备份
  `data/backups/ftseek_us_live_before_probe_prep_20261003_234848.db`。

**实测与验证**:
- REAL 账户基线：total `$73,057.35`、cash `$15,597.77`、market value
  `$57,459.58`、buying power `$31,423.59`；`capital_state` 为
  `trade_env=0 / green / unlocked`。
- `tests/playbook` 全量 **972 passed / 1 warning**；`tests/risk` 全量
  **193 passed / 1 warning**；四棘轮 **29 passed + doc_guard 通过**。
- `cutover_rehearse.py --to live`：DDL 干净 + capital 双向 roundtrip 无缝。
- `cutover_preflight.py --to live`：`block=1 warn=4`，唯一硬阻塞仍为
  P1 毕业门禁 `graduated=false / ic_t3=-0.0403`。
- 2026-10-04 00:10:44 CST 最终重启加载（含 STOP 失败推送语义修正：
  已成交但 STOP 保护失败不再误报为未执行）；重启后 PID 3826815，
  冒烟 7/7 通过，warning/error 为 0。复跑
  `cutover_preflight.py --to live --no-write` 仍为 `block=1 warn=4`。

**安全状态**: `TRADE_ENV=1`、`AUTO_TRADE_ACCOUNT=sim`、
`ALLOW_REAL_TRADING=false`、`live_manual_arm.enabled=false`、
`probe.authorized=false`。本批不构成 LIVE 启用授权。启用前仍建议先人工完成
broker STOP 挂/改/撤极小仓三段演练（P6 里程仍为 0），并提交/隔离当前 worktree。

## 2026-10-03 DEB 路由收敛执行批: LIVE 门控单点化, 镜像前置, Path A 退役

**触发**: 用户令「按照建议执行」— 采纳 DEB 修正版方案 A。

**实施**:
- 新增 `app/playbook/live_arm_gate.py`：`evaluate_live_arm_gate` 单点覆盖 arm/mode/probe
  授权、env/account/lane、LONG/BUY、1 股/1 仓、RTH、open cap；计数失败 fail-close。
- pipeline 在 `_mirror_instruction_to_live` 前重新过门；open cap/RTH/方向/授权不满足时
  不写 LIVE instruction，并经 `app.push` hub 通知。
- `PlaybookDB()` 默认改为 `ftseek_research`；`ftseek_research` 不随 `TRADE_ENV` 漂移，
  且保留 `FTSEEK_DB_FILE` A/B 覆盖。
- 删除 pipeline/service 双份 `_trade_records_db_for_arm` 包装；`paths.py` SIM 解析去重。
- `AUTO_TRADE_ACCOUNT=real` 且未显式 `target_env=real` 的旧 Path A 返回
  `LIVE_PATH_A_RETIRED`，fail-close，不再降级 SIM。
- `monitor_playbook_sim_exits` 活跃名改为 `monitor_playbook_exits`，旧名保留兼容别名；
  scheduler 活跃调用点已切换。

**验证**: `tests/playbook` 全量 **967 passed / 1 warning**；四棘轮
（except/naive-now/W456/doc_guard）**29 passed + doc_guard 通过**；受影响
`test_rfc080_half_flip_interlock.py + test_rfc080_research_pin.py +
test_cutover_infra.py + test_db_connection_chokepoint.py` 共 **45 passed**。
新增行为钉：TRADE_ENV=0 生成/执行同 SIM lane、open cap 不写镜像、非 RTH service 拒绝、
Path A fail-close、LIVE gate 子集矩阵。

**部署**: 2026-10-03 22:41:14 CST 执行 `systemctl restart ftseek-us`，重启前核对
SIM/LIVE 未平仓均为 0、有效持仓均为 0、exit intents 仅 `BOOKED`。冒烟 7/7 通过，
服务 `active/running`（PID 3817330），重启窗口零 warning/error；重启后仍为
0 未平仓/0 有效持仓。`cutover_preflight.py` 维持 `block=1 warn=6`，硬阻塞为
P1 毕业门禁。LIVE 开关仍全关；本批不构成启用授权，启用前须先收口/分离 worktree WIP。

## 2026-10-03 RFC-131 双臂补齐后重启检测: 7/7 全绿, LIVE 仍全关, preflight 维持 BLOCK

**触发**: 用户令「请重启再检测」。

**重启前安全核对**: SIM/LIVE `trade_records` 未平仓均为 0；两库
`open_positions.size>0` 均为 0（LIVE 仅剩 8 条历史 `size=0` manual 僵尸行）；
`playbook_exits.db.exit_intents` 无 `BOOKED` 之外的非终态在途出场。

**部署与检测**: 21:16:07 CST 执行 `systemctl restart ftseek-us`，服务
`active/running`（PID 3810515）。`startup_smoke_check.py --wait 90` 7/7 通过：
ConfigValidator、漂移检测、热重载、SSOT 审计、模拟盘安全、退出快循环、
零 Traceback/CRITICAL。重启后日志 warning/error 为 0。

**切换预检**: `cutover_preflight.py` 维持 `block=1 warn=6`；硬阻塞仍是
P1 毕业门禁（`graduated=false / ic_t3=-0.0403`），WARN 集中在 schema 彩排、
LIVE capital 基线、8 条僵尸 manual 行、broker STOP 里程、研究脚本载体等既有缺口。

**安全状态**: `TRADE_ENV=1`、`AUTO_TRADE_ACCOUNT=sim`、
`ALLOW_REAL_TRADING=false`、`live_manual_arm.enabled=false`、
`probe.authorized=false`；SIM/LIVE 均无自动持仓。本次重启加载的是未提交 worktree
（含其他 agent 在途 WIP），**不构成 LIVE 启用授权**；启用前仍需先隔离/提交本批并清
BLOCK/WARN 裁决面。

## 2026-10-03 RFC-130 复查补充: arm 门提前 fail-close; 明确「最小实现 ≠ 双臂已就绪」

**触发**: 用户令复查另一 agent 的 RFC-130 实施, 下周再裁决是否启用。

**已确认**: commit `175c3b359` 已部署并重启 (19:29:25, 启动冒烟 7/7); 当前仍全 SIM
(`TRADE_ENV=1 / AUTO_TRADE_ACCOUNT=sim / ALLOW_REAL_TRADING=false /
live_manual_arm.enabled=false`)。RFC-130 定向测试 115 passed, 切换/DB 契约 46 passed。

**复查发现**: 原 arm 门排在半翻互锁/毕业门禁之后, `TRADE_ENV=1` 时 `target_env=real`
会先降级 SIM, 到不了 arm 门, 与 RFC-130「未 armed ⇒ HALT_NEW_LIVE、不降级 SIM」矛盾。
已补: `app/playbook/playbook_service.py` 将 arm 门提前到所有降级逻辑前;
`tests/playbook/test_m2_c2_live_quote_gate.py` 新增未 armed 场景, 断言 broker 0 触达、
instruction 未认领、不降级 SIM。RFC-130 §7 已落档。

**启用前缺口**: L1/L2 (`route_playbook_db` + 双库出场扫描) 与 L4 (LIVE instruction 镜像)
未实施; `max_open_positions/max_order_qty/session` 仅登记未接线; 当前 `AUTO_TRADE_ACCOUNT`
仍是全局单臂派发; preflight 仍 `block=1 warn=6` (毕业门禁 `graduated=false,
ic_t3=-0.0403`)。**下周不建议启用 LIVE; 也不建议翻旧全局 `TRADE_ENV=0` 模型。**

## 2026-10-03 实盘切换目标修订: 设计更新为 SIM 常驻 + 手动 LIVE 臂, 零行为改动

**触发**: 用户明确目标为「手动切换实盘, 可空仓切, 先跑通链路; SIM 继续运行, 需要时可切实盘」。

**落地**: 在 `RFC-090` 追加 §17, 将原「全局 `TRADE_ENV=0` 四键同翻」修订为「SIM 常驻 +
LIVE manual arm」: `TRADE_ENV` 保持 1; LIVE 入场由 `playbook.live_manual_arm.enabled`
单独控制; sim/live 账本按 `target_env` 显式路由; 出场监控双库扫描; disarm 只停 LIVE 新仓、
不停 LIVE 出场。切换状态机为 `SIM_ONLY → SIM_PLUS_LIVE_ARMED → LIVE_DRAIN → SIM_ONLY`。

**治理**: C2 未放宽。首次真实 LIVE 执行仍要求毕业门禁通过, 或用户在 DEB 中显式记录
「知情破例 + 签字」。当前预检仍为 `block=1 warn=6`, 硬阻塞是毕业门禁
`graduated=false / ic_t3=-0.0403`。

**本批只改文档, 未改代码/配置/开关, 未重启。** 实施顺序与验收判据见
`docs/decisions/RFC-090-live-cutover-switch-surface-convergence.md` §17。

## 2026-10-03 影子/证伪门清理批: 已按军规重启观察, 7/7 全绿

**重启**: 用户令「重启观察」; 重启前核零未平仓/在途出场, app/ 在途改动仅剩本批
`app/playbook/shadow_registry.py` 与 `scripts/shadow_board.py`, 随后仅执行
`systemctl restart ftseek-us`。当前 `active`, PID `3776834`, 11:20:08 CST 启动,
`NRestarts=0`。

**观察**: `startup_smoke_check.py --wait 90` 7/7 全绿 (启动段 11:20:13);
未平仓 0; 注册表 76 项、`needs_adjudication=0`; mount/consumer/intent 三哨兵
均 0; `shadow_board` 推送门判定 False; 重启窗口零
`ERROR/CRITICAL/Traceback/Exception`。落档见
`docs/decisions/REVIEW-20261003-shadow-cleanup.md`。

## 2026-10-03 策略分析/优化总批复查: 结论成立, 数据逐条复算吻合; HEAD 棘轮红由在途 WIP 修复 (待归属)

> **触发**: 用户令复查 agent 的策略分析与调整优化 (DEB-20261002 系列 9 提交 +
> INCIDENT-002/RFC-129/DEB-20261003 6 提交 + 未提交的影子清理批 REVIEW-20261003)。

**复查方式**: 两路并行独立复查 (委托代理逐提交核) + 本线对未提交批逐项实证 (读原始
数据重算, 不引用其文档数字)。

**成立面 (抽验全过)**:
- 清理批 5 项退役读数全部复算吻合: reentry_quick_tp 13 行 tp_touched=0/13 ✓;
  conflict 36 事件/25 日 opposite_flip=5 成交=2 ✓; ue_midday 我亲跑 eval =
  10日 n=41 命中 22.5%/均值 −0.78% 双 FAIL ✓; cluster summary cap3 blocked=29
  +$433.18 cap4/5/6=8/4/2 ✓; earnings_clue write-only ✓。
- 「5 键热重载生效, 无需重启」为真: 日志实证 10:06:34 (yaml 末次写入后)
  HotReloader 失效链重置 PlaybookConfig/FeatureFlags/WatchlistManager ✓。
- cron 6 行注释退役带墓碑+回滚 ✓; crontab baseline 同步删 5 豁免 ✓;
  推送门 fail-open 语义+守卫测试钉 marker ✓; 停摆守卫对 KILLED/停采项豁免
  (不会退休后日日误报) ✓; 待裁面 needs_adjudication=0 ✓ (76 项注册)。
- DEB-20261002 回放方法论可靠 (entry_time 锚定无前视, 删失标注, 13 条自曝);
  R3 假信号撤回正确 (90% deterministic 标签系 or-兜底污染); P0-1 cron 撤回自洽
  (eval_on_demand=True 不该挂 cron)。
- INCIDENT-002「归档+删除」根治 close_key 死锁成立 (双守卫 fail-closed +
  归档可回插); 方案A 1351 行删除零活引用 (含 .pyc 残魂与 scheduler 墓碑测试);
  DA trace 保留不动 RFC-066 已结算行保护 (preserve 仅在 _target_id 非空时调,
  所有拒绝路径早退)。

**查出问题**:
1. 🔴 **HEAD (已提交态) 两棘轮红** — except:pass 924→926 (DEB-20261002 回放脚本
   2 处静默吞错) + W456 四击 (pipeline W4 47→48 / exit_lifecycle+1 (RFC-129) /
   eth_held_exit_shadow+1 (RFC-127) / position_sync 14→16 (INCIDENT-002))。
   即 10-01~10-03 六个提交批的验证均未跑棘轮 (10-01 我已抓过一次同型遗漏,
   未制度化)。工作树里的 5 个 app/ 文件 + 4 个 deb 脚本改动正是这些修复,
   但**无任何批次认领** (REVIEW-20261003 自称「本批 app/ 仅 shadow_registry」) —
   在途 WIP 待归属, 下个提交者须 `git diff --cached` 逐行核归属 (9c299cb31 教训)。
2. 🟡 **DEB 回放脚本欠账** (委托复查发现, 未修): gap_sl_replay/gap_avgdown_replay
   WHERE 无日期钉窗 (重跑读数随新交易漂移); gap_sl_replay PnL 公式只有 LONG 向
   (SHORT 触发 OVERNIGHT_GAP_STOP 时变 latent bug, 当前 6 笔全 LONG 不现症)。
3. 🟡 yaml 残留过时注释: unified_config 1218 行「热重载不覆盖本路径」已被 8/28
   invalidate_config_snapshots 失效链推翻, 与本批「热重载生效」自述矛盾。
4. 🟡 结构性观察: W456 基线文件 (data/*_baseline.json) 不受 git 跟踪且「基线若删
   则重建同零违反」— 删基线文件=违规自动洗白, 依赖生产 data/ 连续性 (设计内风险,
   建议知悉)。

**验证**: 工作树四棘轮 + 守卫/契约合跑 226 passed; HEAD 态棘轮红用临时 worktree
隔离实证 (已清理)。

**执行情况 (2026-10-03 用户裁「按照建议执行」)**:
- `c1c879c8a` 棘轮修复归核: 在途 WIP 8 文件署名入库 (5 app/ 日志卫生 + 3 deb 脚本
  留痕); 复查中另发现 WIP 自炸缺陷 — 四处用 `sys.stderr` 但脚本均未 `import sys`
  (异常路径即 NameError), 已补。
- `c777d57fc` P3 修复: gap_sl/gap_avgdown 钉结算窗 (与同族 12 脚本同口径
  ['2026-06-27','2026-10-02')) + 方向感知 PnL/SL 线 (修复前后 LONG 输出逐字节一致,
  SHORT 合成三态验证) + W456 三基线按 preflight 先例入库 (堵「删基线洗白」漏洞)。
- yaml 过时注释已修 (unified_config reentry_quick_tp 段, 「热重载不覆盖本路径」→
  失效链已覆盖), 留在工作树随清理批同文件提交 (避免拆 hunk 归属混乱)。
- 未动: 10-03 清理批本体 (shadow_registry/config 5 键/shadow_board/推送门等)
  仍待其责任批提交; obsidian 为例行产物。
- 终验: HEAD 四棘轮 18 passed 回绿。

## 2026-10-03 影子/证伪门清理批: SSOT 补登记 + 到期裁决落地 + 推送收口 (用户令「梳理清理, 减空耗提信噪比」)

> **落档**: `docs/decisions/REVIEW-20261003-shadow-cleanup.md` (完整盘点/判据读数/不动清单/待裁面)。
> **权限层**: 零交易行为变更 — 全部为测量面 (shadow-only 开关/cron 注释/注册表记账/推送门);
> 两条判据处置 (ue_midday / reentry_quick_tp) 为**预注册判据的机械执行**, 判据一字未改。

**结算/退役 (5 项, 数据全留档)**:
- `ue_midday_shadow` 预注册判据双 FAIL (10 日 n=41, 命中率 22.5%/门 55%, 均值
  −0.78%/门 +0.5%) ⇒ 按判据自带处置退役: 午间 cron 已注释 + `midday_shadow_enabled=false`
  (停掉每日 12:30 ET LLM 扫描 = 本批最大空耗削减)。
- `reentry_quick_tp_gate` 窗 9/1–9/30 满结算: 13 行实弹 **tp_touched=0/13** ⇒ KILLED
  停采 (开关 false); 门禁维持; quick_tp 参数零触发 = 死参数, 删除提案留用户裁 (L3)。
- `cluster_concentration_shadow` 44 日判据门 0/3 过 (cap3 blocked 29<30 且被拦合计
  +$433 = 拦盈利单) ⇒ KILLED 停采, 不立簇帽。
- `conflict_shadow` n=36 达标: 翻转仅 5/36 (成交 2), 候选端排除无证据 ⇒ KILLED 停采。
- `ue_earnings_clue_shadow` write-only 38 天 (零判据/零读方) ⇒ KILLED 停采。

**SSOT 修复**: 5 条在采未注册项补登记 (62→69, 含 `shadow_portfolio` PERMANENT_MEASURE);
后续 cron×注册表审计再补 7 条漏网 (69→76: `premarket_reprice` / `regime_divergence` /
`reentry_gate_cf` / `direction_cf` / `t5_cf` 终态, `da_true_reverse` / `ue_confirm_gate` 留观);
`premarket_peer_shadow` 重归类 KILLED+采集保留 (jsonl=UE peer_surge 输入, 同
universe_expansion 先例); `_CONSUMER_ACK` 新增 4 条逐条认领。

**待裁面清账 8→0**: 当日实跑/直读 7 项读数入 ADJUDICATED (ue_tier_gate /
ue_quote_freshness / fee_gate / trail_lock_pyramid / rfc104 / ext_entry_exec_price /
reentry_quick_tp), 全部自动续观带重看窗; `futu_channel_top_movers` 9/30 闹钟到期后
按用户确认终裁③: SUPERSEDED 并入 kr3 轴, 不新增门/影子, 采集维持 true。

**推送收口**: `shadow_board --push` 加 actionable-only 门 (无待裁/无停摆 → 只落日志不推,
fail-open); D2' 入场确认门日报双面自动续观时空输出静默 (已部署 Hermes);
`ue_short_boost` 停摆阈值按事件驱动节奏校准 4→8 天 (历史最大自然间隙 11 天)。

**cron×注册表审计**: 41 个 shadow-ish 活跃脚本逐项核对 data_path/消费方; 5 条空转环
已注释退役 (regime 分歧率、盘前重定价、方向 CF、T+5 CF、证伪门 CF), 数据/脚本留档;
`capital_flow_daily_aggregate` 等为写方/消费方假阳, 未停; `eth_ext_entry_prefilter`
改 SUPERSEDED, `ue_short_boost` 54/60 登 10/10 重看。

**部署**: 5 个 config 键置 false 全部经热重载生效 (ConfigContainer/PlaybookConfig
失效链), **服务未重启** (app/ 有其他 agent 在途 WIP, 不顺带上线); 零持仓核对 0;
crontab 午间行 + 5 条审计空转行注释 (备份
`data/crontab.backup.20261003_midday_retire.txt` / `...shadow_audit.txt`)。

**验证**: 16 个守卫/契约测试文件 **178 passed**; `check_artifact_consumers` /
`check_intent_uniqueness` / `check_mounting` 全绿 (ticket_size 已重跑刷新);
D2' 静默门完整实跑 exit=0、stdout/stderr 均空。

## 2026-10-02 多agent优化二轮复查: RFC-127/128方向成立, 两个P1纪律/安全问题就地收口

> **触发**: 用户令复查其他 agent 的 `0f563afdc` (RFC-127) 与 `f1d8fdd98`
> (RFC-128) 优化调整是否合理。

**复查结论**: 两个 RFC 的方向与根因链均成立 — ACN 卡死释放、影子幽灵样本守卫、
external_close 同步释放意图都指向真实缺口。但发现两个 P1:
1. **提交不完整**: `0f563afdc` 提交了 `tests/test_eth_hed_exit_shadow.py`
   的 observer 断言, 却未提交其依赖的 `scripts/observe_nightly_supply.py` 段3、
   `tests/conftest.py` 生产数据缝、RFC-126 复查/测试与 handoff 落档。detached
   HEAD `f1d8fdd98` 实测 1 failed/26 passed; 当前工作树补齐后 27 passed。
2. **RFC-128 会制造 `pbe_*` 孤儿单**: 原实现只要四元组匹配就把 intent 改
   `BOOKED`, 不检查腿状态; 若我方出场腿仍 `PREPARED/ACTIVE/CANCEL_REQUESTED`
   且用户手动单外部平仓, 活跃腿无人 resume/撤, `orphan_ext_order_watch` 又只认
   `ext_exit_` 前缀, 迟后成交可造裸仓。

**本批修复**: `ExitLedger.release_unsettled()` 增加守卫 — 任一腿非 `TERMINAL`
或 `dealt_qty IS NULL` 时零释放 + WARNING 留痕; `release_stuck_exit_intent.py`
apply 时同样拒绝非终态/成交未知腿。RFC-128 边界/回执同步更新。

**验证**: RFC-127/128/exit lifecycle 合跑 174 passed; `tests/execution` 232
passed; EXT/RFC-126/影子定向回归 109 passed; `py_compile` ✓; detached HEAD 红
复现 ✓。人工释放工具对 ACN (已 BOOKED/腿 TERMINAL) dry-run 幂等正常。

**部署**: 重启前核对 ACN SHORT 23 (SL 223.16)、ALK LONG 147、未决 intent 0、
非终态腿 0; app/scripts 改动仅本批复查产物。2026-10-02 10:59:44 重启,
`startup_smoke_check.py --wait 90` 7/7 全绿 (exit=0)。

**遗留 P3 (未动)**: `eth_held_exit_shadow` DB 路径仍硬编码
`data/ftseek_us.db`; `release_stuck_exit_intent.py` 券商 host/port 硬编码且
apply 再查一次快照。均只读/人工面, 后续独立小批处理。

## 2026-10-01 RFC-127 阶段0: ETH出场HELD→RTH升级影子 (用户裁「按照建议执行」, record-only)

> **触发**: 上一条复查批的现症 P1 — US.ACN SHORT 23股出场卡死, 用户选择建议方案 B
>(ETH HELD 意图 RTH 开盘后会话升级补腿), 按纪律影子先行。

**已落地**:
- `docs/decisions/RFC-127-eth-held-exit-rth-escalation.md`: 根因链 (ETH 限价腿
  TERMINAL → resume 早退 HELD; RTH 新意图被 session 冲突/symbol busy 拦;
  blocked_exits 跳过 SL 评估) + 阶段1 预注册判据 (≥5交易日且≥3意图或单意图
  SL 暴露≥$500; 可升级性 100%; 无害性反证; 告警噪声消失), 未实施行为变更。
- `app/playbook/eth_held_exit_shadow.py` + `pipeline._resume_playbook_exits` 接线:
  ETH + `exit_eth_partial_fill_held` + `is_market_open()` + 剩余>0 → 每意图一行
  `data/shadow/eth_held_exit_escalation.jsonl` (event_id 幂等); 双层防护
  (emit 自吞错 warning+栈 / 调用点保险丝), 恢复与告警行为零改动。
- `observe_nightly_supply.py` 段3 接入: 今日有行 → P1 "ETH出场HELD在RTH仍阻塞"
  (SL 评估被跳过, 需人工或等阶段1)。
- `tests/conftest.py` 登记生产数据缝; 新契约测试 11 例 (触发四象限/幂等/
  写失败自吞错/接线源级钉/observer P1)。

**验证**: 新测试 11 passed; 棘轮+W456+RFC-126+冲突/偏斜影子回归 51 passed;
`py_compile` ✓。部署完成: 22:28 重启 `ftseek-us` (重启前核过在仓 ALK/FRO/
HPE/ACN 与在途 ACN HELD, app/scripts 改动仅本批 3 文件); 启动冒烟 7/7 全绿
(exit=0)。活体验证: 重启后首个 fast loop 即落 1 行影子
(`ETH-HELD-RTH:747ba9cf...`, US.ACN 剩余23股/order 8219159/intent_age 3804s),
复扫 20s 后仍 1 行 (event_id 幂等); 退出行为零变化 (HELD 告警照旧);
observer 段3 正确升 P1。

**附带修复 (RFC-126 遗留红, 本批回归抓出)**:
`tests/test_extended_hours_order_execution.py` 的 `trade_records` 测试夹具 schema
缺 `decision_id` 列, 而 940c5c9a4 的生产 INSERT 已写该列 ⇒ 4 例既有红
(full_fill/grade/partial_fill/idempotent)。补列后该文件 39 passed;
exit_lifecycle+EXT+RFC-126+新影子+棘轮合跑 178 passed。纯测试 schema 同步,
零生产码变更, 无需重启。

**边界**: 当前 ACN 仓在阶段0 期间维持 HELD 现状 (用户未选手动平仓A);
若需立即止血仍可改走 A (手动平仓 + extended_hours 出场对账)。

## 2026-10-01 多agent优化总复查: 方案成立, 体检脚本三处缺陷就地修复 (零行为变更)

> **触发**: 用户令复查多 agent 多轮优化 (RFC-120/122/124/125/126 + EXT hotfix)。

**复查结论**: 整体方案成立 — ①RFC-120 归一化 SSOT 双侧同源, 执行层锚纪律
(生成时价, 禁 reprice 活价) 正确; ②RFC-122/125 均为 record-only 影子且判定
链零改动, 今日实跑 batch 2B/0S=100% 砍 vs portfolio 6B/2S=75% 放, divergent
如预期落盘; ③RFC-126 did 唯一性经 attempts 键 (date:symbol) 复核, 同日同标
重入被闸住, 无 did 碰撞; ④EXT 7 行回填唯一且 verdict 与 §4.1 记录一致。

**查出并修复 (observe_nightly_supply.py, 只读工具)**:
1. except:pass 棘轮 924→927 红: 3 处静默 `pass/continue` → 坏 JSON 行计数
   告警 + cap/entry 畸形行计数留痕 (R5 模式, 控制流零变更)。
2. 偏斜影子打印键名错配: `majority_pct`(不存在) → `pct`, 修掉实值 `None`。
3. 段4 归因对照增强: EXT verdict 改同行 `json_extract` (防同 symbol 多行错配);
   新增非 EXT dangling did (有 did 无指令行) → P1 幽灵归因检查。

**验证**: 契约测试 11 passed; 棘轮 2 passed (924 回基线); 合并回归 37 passed;
`py_compile` ✓; 活体复跑 10/1 体检无新增误报 (4 笔成交全归因)。零交易行为
变更, 脚本为手动/cron 进程, 无需重启; 复核结论已回写 RFC-126 §4.2。
启动冒烟七项核心断言全绿 (第八项"启动段早于本次重启"为 --wait 模式
在未重启时运行的预期语义, 非故障)。

**⚠️ 现症 P1 (需用户裁决, 本批未动交易)**: US.ACN SHORT 23股 @212.529 (sim)
在途出场卡死 — 21:25 premarket flatten LIMIT 23@215.62 (订单8219159)
60s 零成交被撤, durable intent `7b149...` 进 `exit_eth_partial_fill_held`
(ETH 设计不追价)。此后 ①RTH 新退出被同 close_key `exit_session_conflict` +
symbol busy 拦; ②fast loop 把该 symbol 加入 blocked_exits ⇒ SL(223.16) 评估
被整体跳过; 22:14 现价 227.37 已越 SL ~1.9%, 浮亏约 -$342, 每~10s 告警一次
(钉钉 21:30/21:45/22:00 已推)。DEB-20260910 §356 早已预告"长期 HELD 需人工
对账处置流程", 但该流程尚未落地 — 今天是首例实弹。待裁: A) 手动平仓后跑
extended_hours 出场对账; B) 预注册 RFC: ETH HELD 意图在 RTH 开盘后允许
会话升级重试 (或价格越 SL 时解除 symbol busy), 影子先行。

## 2026-10-01 RFC-125 批复查: 策略/逻辑成立, 三处守卫违规就地修复 (零行为变更)

> **触发**: 用户令复查 agent 对 DA/UE 主通道的分析与 RFC-125 落地批。

**复查结论 (逐条实证)**: 病灶诊断与影子设计成立 — ①checkpoint 字段契约与真实
9/23 TSLA / 9/29 ORCL 逐字段一致 (`_debate` 25 字段全对得上); ②decision_id 前缀口径
与 db.py 仲裁同源; ③db.py 扩列追加在原 4 列之后, 仲裁位置索引 r[0..3] 不受影响;
④连接池 row_factory=sqlite3.Row (Row 缺键抛 IndexError, 已被 `_value` 捕获);
⑤落盘产物与 9 月实跑 2 条独立真反向逐位吻合。

**查出并修复 (agent 验证漏跑棘轮守卫 — "117 passed" 为真但不完整)**:
1. except:pass 棘轮 924→927 红: 三个新文件各一处静默 `continue` → 改坏行计数 +
   告警留痕 (R5 模式,  precedented by 9/30 meta_audit 批)。
2. W6 日志卫生棘轮 db.py 4→5 红: 影子失败告警无栈 → 补 `exc_info=True`
   (扫描脚本 except 内告警同补)。
3. 生产数据隔离缝未登记: 冲突影子路径原为函数内联 ⇒ 无法登记; 实证
   test_playbook_db.py 触发冲突时硬闸被 db.py 自身 except 吞掉 (告警 1 次) —
   正是 conftest 缝表注释点名的反模式。已提模块常量 `_CONFLICT_SHADOW_PATH` 并
   双登记 conftest `_PRODUCTION_DATA_SEAMS` + offline_chain_driver 私有清单;
   修复后吞闸告警 0 次、测试零生产污染。

**验证**: 四棘轮 31 passed (except/naive-now/utc4/W456); 原 117 集 + test_playbook_db
全量 + offline 全链 173 passed; 扫描器幂等重跑 appended=0。db.py / conflict_shadow
改动为零行为日志参数 + 常量重构, 下次例行重启即载 (不重启不影响交易与影子写入)。

## 2026-10-01 RFC-125 多LLM真反向/指令冲突影子 (用户认同策略, 零交易行为变更)

> **触发**: 用户确认“不要冲突和重复, 充分利用多LLM真反向结论”, 且反向专指 DA 分析师与独立挑战者的正反向结论。

**已落地**:
- `app/playbook/db.py`: `save_instruction` 唯一写库收口处新增 record-only 冲突影子; 检测非空 `decision_id` 重复、同 did 材料漂移、DA/UE/reentry 活跃方向冲突; 仲裁与入库行为不变。
- `app/playbook/true_reverse_shadow.py` + `scripts/da_true_reverse_shadow.py`: 扫描 DA checkpoint, 只把 BUY↔SELL_SHORT 判为真反向; SKIP 视为弃权; 要求独立挑战者且独立方向确认反向; 保留挑战者理由、失效机制与翻向条件。
- 新产物: `data/shadow/instruction_conflict.jsonl`、`data/shadow/da_true_reverse.jsonl`, 均幂等 `event_id` 去重。
- 真 reverse 扫描已挂系统 crontab `50 20 * * 1-5`, 并登记 `docs/CONTROL-LOOP-LEDGER.md`。
- 9月实跑: 捕获 2 条独立真反向 — 9/23 `US.TSLA` (分析师 SELL_SHORT vs 挑战者 BUY)、9/29 `US.ORCL` (分析师 BUY vs 挑战者 SELL_SHORT)。
- 目标矩阵与 enforcement 门槛落档 `docs/decisions/RFC-125-multillm-conflict-resolution-shadow.md`; 阶段1仍须 ≥5 交易日影子 + 用户显式裁决。

**验证**: `pytest tests/test_true_reverse_shadow.py tests/test_instruction_conflict_shadow.py tests/playbook/test_generate_da_priority_contract.py tests/test_playbook_db.py::TestRFC066MultiRoundLedger tests/test_stale_bridge_guard.py tests/test_control_loop_ledger.py -q` → **117 passed**; `py_compile` 通过。按规程两次重启 `ftseek-us` (首次加载影子、二次加载材料字段查询修正), `scripts/startup_smoke_check.py --wait 90` 七项全绿 (ConfigValidator/漂移/热重载/SSOT/模拟盘/退出快循环/零 Traceback)。

**回滚**: 删 `db.py` 的影子调用 + 两个新模块/脚本/测试/RFC; 影子 JSONL 可保留或删除。

> [2026-10-15 切档] 2026-09-30 及更早进度与长期上下文已逐字移至
> `docs/archive/handoff-archive-pre-2026-10-01.md`（sha1
> `4bff26a1698f69e4487d4cdc25854c588b6c3297`）。根 `handoff.md` 仍是唯一进度队列，
> 本文件仅保留 2026-10 滚动窗口。
