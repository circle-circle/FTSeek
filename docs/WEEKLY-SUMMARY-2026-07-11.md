# 本周调整汇总 · 决策链路 + 退出引擎 (截至 2026-07-11)

> 用途: 本周所有调整的**汇总审阅稿**, 供另一 Agent 独立审阅。
> 关键背景: **所有交易均在富途模拟盘 (v2 sim)**, 风险可控; 修改遵循"数据驱动验证 · 先看数据再动手 · surgical edit · 诚实记录"。
> 状态口径: ✅=已落地并验证 · ⏳=已搭骨架待数据/外部配合 · ❌=显式不做。
> 权威原始方案 (本稿为汇总, 细节以原文档为准):
> - [决策链路·双脑清理](file:///opt/ftseek/FTSeek-US/docs/proposals/decision-chain-single-brain-cleanup-20260711.md)
> - [退出引擎优化](file:///opt/ftseek/FTSeek-US/docs/proposals/exit-engine-optimization-20260711.md)
> - [PLAYBOOK退出测量优先重构](file:///opt/ftseek/FTSeek-US/docs/proposals/PLAYBOOK退出测量优先重构.md) (Plan, 未执行, 为退出侧前置论证)

---

## 0. 一句话总览

本周把 FTSeek-US 的交易链路收敛为**单一决策脑 (DA=deep_analysis 入场脑, FTSeek=执行脑)**,
补齐了 DA 决策契约的落库与可见性 (invalidation/thesis), 修复了 confidence 量纲与来源使其
"第一次能对着正确的量做校准观测", 新增了 dashboard 决策链路可视化 (漏斗/追踪/脑健康/退出画像/
校准 5 端点), 并对退出引擎做了**测量优先**的第一批优化 (归类纠错 + MFE 数据管道 + invalidation
影子骨架)。多处推翻了先前基于脏数据/查询错误的结论 (见 §6 勘误)。

**⚠️ 重要: 本周代码改动尚未 git commit** (working tree, `git status -s` 约 60 个改动文件)。
审阅者若需比对基线, 应针对工作树而非最近提交 (最近提交止于 07-07)。

---

## 主题一 · 决策链路双脑清理 (✅ 架构定案, 代码待周末落地/下周验证)

**问题**: 表面有"3 模型 LLM 投票 + 5 路盘中独立入场"的多脑并行, 实证已自然停火但代码仍活
(复燃风险), 且存在可证空转的死门禁。

**结论与改动** (详见原文档 §1-2):
- **3 模型 LLM 投票已全死**: scheduler 恒把 `voting_coordinator` 赋成 Bridge/Null 适配器,
  di_container 的真·VotingCoordinator 工厂是 DEAD fallback, 永不触发。
- **盘中 5 路独立入场** (ORB/technical/insider/earnings_revision/voting_llm) 实盘已休眠 ——
  `trade_records.strategy_id` 实证: 6/24 起成交 100% `playbook_sim`。本次是把既成事实用**配置固化防复燃**。
- **deep_analysis 方向门禁 (pipeline L786-868) 可证空转**: 自我背书 + 对 list 调 `.get()` 恒抛异常 → 删除零行为变化。
- 计划改动 (全部带开关可回滚): `TechnicalFallbackAdapter` 兜底 + `signal_stage.independent_entry_enabled`
  双脑门 + 删死门禁 + 断 3 模型复活路径 (中和不物理删)。
- **回滚开关**: `playbook.fallback_when_no_bridge: none` / `signal_stage.independent_entry_enabled: true`。

**验证锚点**: `scripts/check_decision_chain.py` (近7/30天/单日); 通过标准 = ⛔独立入场泄漏 = 0。
基线实测 (07-11): 近7天 0 泄漏, `playbook_sim` 占 88%。

**审阅关注**: ①fallback 默认值该 `none`(保守) 还是 `technical`; ②"物理不删 voting_coordinator.py"
的隔离是否彻底 (screener/condition_taxonomy 仍引用 voting_aggregator)。

---

## 主题二 · DA 决策契约上链 + 校准闭环 + Grounding 定论

### 2.1 决策契约上链 (Stage 1 ✅ / Stage 2-3 ⏳ 待 Hermes)
- **最高杠杆缺口**: DA bridge 输出 thesis + invalidation 很好且可证伪, 但 FTSeek 在 playbook 边界
  **全扔了** (Instruction 无 invalidation 字段, thesis 被模板覆盖)。
- **Stage 1 (✅)**: `Instruction` 加 `invalidation` + `da_thesis` 字段, 从 `vote_result` 带入;
  invalidation 入独立列, da_thesis 入 `raw_instruction_json` (不覆盖模板 thesis, 下游报表零影响)。
  不动入场/出场逻辑; 生产库缺列 → 周一盘前 `ensure_tables` 自动补。
- **Stage 2/3 (⏳)**: 需 Hermes 侧产出**结构化** invalidation (机器可判定), repo 侧先影子后灰度实盘。

### 2.2 校准闭环 (量纲+来源修复 ✅ / 重校准 ⏳ 待样本)
- **量纲修复 (✅)**: `signal_lifecycle.signal_confidence` 混合量纲 (0-1 / 0-100 / 0-1 三源),
  4 行脏值把 `playbook_sim` 均值拖到 3.99。归一 (`_norm_conf`, >1.5 除 100, 夹 [0,1]) + 存量 UPDATE →
  均值 3.99 → 0.694。
- **来源修复 (✅)**: 该字段此前取 `snapshot or trade or consensus`, **不是 DA 指令原值** →
  追 GOOG 发现指令 conf=35 但归因 conf=0.70, 近 20 笔 19 笔不一致。改为**优先取
  playbook_instructions 的 DA confidence** (JOIN decision_id), `--full` 重建 397 行, GOOG/NU
  归因 conf 0.70 → 0.35 (DA 原值)。
- **校准结论**: 实测 256 笔 DA confidence 与胜率**非单调 (U 型)** → **DA confidence 尚未校准**;
  但这是第一次拿"决策脑真给的分数"在评。样本 <50 不下结论, go-forward 干净样本需卡 `date≥07-11`。

### 2.3 Grounding 架构定论 (深查后证伪先前假设)
- **结论: DA 脑是 Hermes skill 侧自包含的, 不读 repo 分析文件 → grounding 无法纯 repo 注入。**
- 驱动交易的 `opportunity_scan_*.json` 由外部 `~/.hermes/skills/deep-stock-analysis/` 自有 pipeline
  生成; 数据源直接打 Finnhub + futu 新闻; 从 repo 只读 `ftseek_us.db` + `.env.us`。
- 三目标重估: news_v2/sec 均**非缺口** (skill 已自取); factor_ic 是唯一可能增量但需改 skill 侧 (外向/难回退)。
- **隐患记录**: 存在两套 IC (repo `factor_ic_latest.json` vs skill `claim_ic_cache.json`), 口径可能分叉, 统一 grounding 前需对齐。

### 2.4 顺手修的真问题 (✅)
- `premarket_enrichment.json` **孤儿文件**: 停在 6/26, 全仓无写方 (写入代码已重构掉); 3 个顾问路径读死文件。
  改为直接调 `collect_premarket_data()` (内存版, 新鲜)。不影响实盘 (那三处非交易驱动)。

### 2.5 日内 IC — edge 尺子对齐 horizon (✅ 2026-07-12)
- **动机**: 系统纯日内 (DA horizon 恒 short, 当天平), 但 IC 只测 `fwd_return_t1/t3/t5` 日线 horizon
  → **测的是系统收割不到的多日 edge (horizon 错配)**。审阅提问点出此张力。
- **改动** (ic_tracker.py + db.py): 加 `fwd_return_intraday` (entry→**当日收盘**, 与 t+N 同用收盘口径),
  作为**主 edge 尺子**; `compute_playbook_ic` 返回 `overall_ic_intraday`/`sample_intraday`。
  **保留 t+1/t+3/t+5 供对比** —— 若某标的多日 IC 显著 > 日内 IC, 即"该标的该不该转非日内持有"的证据
  (未来按标的做选择性过夜/波段, 接 RFC-061)。
- **验证**: 迁移补列 (旧库 ALTER) + 日内收益回填算式 (AAA+3%/CCC-1%) + IC intraday 桶均经内存库
  实测通过。生产库 `fwd_return_intraday` 列待下次 `ensure_tables` 自动补, 盘后 backfill 自然填充
  (同 invalidation 列路径, 不强写线上库)。
- **线上回填基线 (✅ 2026-07-12, 已备份 `ftseek_us.db.bak.20260712_intradayic`)**: 迁移成功,
  近30天非SKIP 179 条 intraday 覆盖 175 (98%)。IC: **intraday=-0.14(n=142)** / t1=-0.04 / t3=-0.09 /
  t5=-0.15。**两条结论**: ①日内 IC 为负 → 印证 "DA confidence 尚未校准", 且这次量在对的 horizon;
  ②t+3/t+5 也全为负且不高于日内 → **该窗口无被日内退出留在桌上的多日 edge, 数据暂不支持转过夜/波段**。
  **污染 caveat**: 窗口跨 07-11 断点 (黑名单停用/仓位规模变/双脑) + confidence 本周才修, 属结构性基线,
  **非定论**; 干净结论待 `date≥07-11` go-forward 样本。**数据质量疑点**: 日志见 `US.US.MU` 双前缀
  (Futu 取数失败→TwelveData 兜底), t+N 一直也走此路径 (非本次引入), 另开只读核查符号归一化。

---

## 主题三 · Dashboard 决策链路可视化 (✅ P0+P1+P2)

**目标**: 让 DA决策脑 → playbook执行脑 单链路在 UI 上"感知进度 + 感知策略逻辑", 让静默降级/退出脱钩**可见**。

新增 [`app/web_dashboard/routes/chain.py`](file:///opt/ftseek/FTSeek-US/app/web_dashboard/routes/chain.py) **5 端点** (不动 dashboard.js/dashboard.py):
- **P0**: `/api/chain/funnel` (今日漏斗: 候选→指令→触发→成交→持仓→退出) ·
  `/api/chain/trace/{decision_id}` (单笔时间线 + confidence 跨表一致性告警) ·
  `/api/brain/health` (决策脑健康 + 投票适配器推断 + 降级告警)。
- **P1**: `/api/chain/exit-profile` (exit_reason 归 5 类可视化)。
- **P2**: `/api/chain/calibration` (confidence 分桶 vs 胜率, 判单调性)。
- 旧模块整合: "今日已平"并入漏斗; "退出画像"下沉复盘区 (实时 vs 复盘时间尺度分离)。

**实测**: 07-10 漏斗 4→8→3→3→0→3; GOOG 追踪正确串起链路并触发 confidence 分歧告警;
无 bridge 日脑健康正确报 degraded/technical 降级。

---

## 主题四 · 退出引擎优化 (✅ 批次1 A+C + Stage 2a; ⏳ 批次2 待样本)

**起点**: 用户直觉"到点就走·不看逻辑过于粗暴, 伤收益/胜率"。**调查后部分证伪**:
退出引擎其实成熟 (6 级优先级链 + breakeven + trailing), 系统是**纯日内** (executed 单 horizon 全 short,
DA 建议持有 ~4.3h), PRE_CLOSE +1.44% / EOD +0.98% 实际赚钱 → 到点平符合 DA 意图。
**真正弱点**: TRAILING_STOP 是唯一系统性亏损的"主动"退出。

### C — 退出画像归类修复 (✅, chain.py)
- **这是上一轮我自己引入的可视化 bug**: CAT 映射 logic 类只认 `TAKE_PROFIT`, 漏
  `DAY_TRADE_TP/SCALE_OUT/ORB_TAKE_PROFIT` → 造出"逻辑类仅 7%/止盈仅 1 次"的假象, 反过来强化了用户直觉。
- 修复: 补齐止盈标签 · `ORB_BREAKDOWN`→止损 · TRAILING_STOP 按用户选**留止损类并 hint 标注** ·
  take_profit_n 用 `_PROFIT_REASONS` 集合统计。
- **实测 (近60天)**: 逻辑类 7%→**19笔/16%/胜率100%/均+1.14%**, take_profit_n 1→19, 两条假告警消失。
  止损类 (含 TRAILING) 47笔/39.5%/胜率23.4% 现为最大且唯一系统性亏损类 → 坐实批次2 目标。

### A — MFE/MAE 数据管道 (✅)
- **A2 回填** [`scripts/backfill_mfe_mae.py`](file:///opt/ftseek/FTSeek-US/scripts/backfill_mfe_mae.py) (默认 dry-run, --commit 写库, 改前已备份 DB):
  394 单扫描, 133 单回填 (17 peak 反算 + 116 realized 兜底), **mfe 合理占比 9%→43%** (复核已升至 61%,
  因新单走 go-forward)。填不了的保持 0 (不塞假值)。
- **A3 go-forward** (pipeline.py 采集处 + 反算处各加量纲夹取 `_MFE_MAE_SANE_MAX=50`); trough 采集已存在。
- **A4 探针** `check_decision_chain.py --mfe-health` (覆盖率 + giveback 分类)。
- **局限 (诚实)**: 当前 giveback 多为 0, 因回填用 realized 兜底 (mfe=pnl); 真实 giveback 需 go-forward 真实峰值累积 → 批次2 前置。

### Stage 2a — invalidation 影子骨架 (⏳ 已搭, 不实平)
- 新增 [`app/playbook/invalidation_shadow.py`](file:///opt/ftseek/FTSeek-US/app/playbook/invalidation_shadow.py) 纯函数模块 (自测 6/6):
  结构化 rules 优先 + 自然语言尽力解析, 返回 `{would_trigger, reason, parseable}`。
- 接入: 建仓 metadata 写 `invalidation` (playbook_service.py); monitor 循环 not-exit 分支影子评估,
  首次命中记 `meta.invalidation_shadow`, **不实平**。老单无键→自然跳过, go-forward 新单生效。

### 批次2 / Stage 2b (⏳ 未开工, 有前置)
- **批次2 (TRAILING 调参)**: 等 go-forward 真实 MFE 累积 (≥2-3 周日内样本), 用 giveback 分辨"锁太松/太紧"再分因子调 breakeven/drawdown。
- **Stage 2b**: 需 Hermes 侧产出结构化 `invalidation_rules`, 影子命中率验证后再灰度实平。

---

## 5. 关联但未执行的前置方案 (供审阅背景)

[PLAYBOOK退出测量优先重构.md](file:///opt/ftseek/FTSeek-US/docs/proposals/PLAYBOOK退出测量优先重构.md) (Plan, **未执行**) 是退出侧的"测量优先"论证:
- 核心观点: **退出只是下游; 67% 亏损单从不浮盈 = 入场信号问题** → MFE/MAE 遥测最高价值消费者是入场闸门, 而非退出调参。
- 明确**本轮不改退出参数**, 只建"记录+冲突收敛+测量口径", 并设退出参数重设的启用门槛 (每 exit_reason 分组 ≥8 笔 + MAE 数据 ≥2 周)。
- 本周实际落地的退出侧工作 (主题四) 与此方向一致 (测量优先, 参数不动)。

---

## 6. 勘误与诚实记录 (本周关键自我纠正)

审阅者请重点核查这几处推翻: 它们是"先看数据"过程中对先前错误结论的纠正。

| # | 先前错误结论 | 真相 | 影响 |
|---|---|---|---|
| 1 | "MFE 存量脏值 >100% (14 笔)" | mfe_pct 列量纲本是"百分比数值"(2.79=2.79%), 我做 giveback 分析时写 `mfe_pct*100` 才显示成 278%。列真实 max=3.82, **无 >50 脏值**。真实缺口是纯粹的 91% 为 0 (需回填)。 | 量纲夹取代码作 go-forward 防御保留 (无害), 但"清理存量脏值"动机不成立 (存量清 0 笔)。 |
| 2 | "退出逻辑类仅 7%, 止盈仅 1 次" (用户直觉据此强化) | chain.py CAT 归类 bug (我上一轮引入), logic 类漏标 DAY_TRADE_TP/SCALE_OUT 等。真实逻辑类 19 笔/胜率 100%。 | 已修复; 假告警消失。 |
| 3 | "65-80% 桶均 PnL -$210, 校准差" | -$210 几乎全由 6/24 三笔旧仓位规模制度 (单笔 ~$30K, 现 ~$3.2K, 9x) 造成。改用收益率% 后降为温和 -0.51%。 | 教训: 跨制度小样本必须看 % 不看 $。 |
| 4 | "grounding 缺口 = news_v2/sec/factor_ic 未注入 prompt" | DA 脑 skill 侧自包含, 不读 repo 分析文件。news_v2/sec 非缺口, factor_ic 需改 skill 侧。 | grounding 无法纯 repo 注入; 记录两套 IC 分叉隐患。 |
| 5 | "到点就走伤收益/胜率" (用户直觉) | 系统纯日内, PRE_CLOSE/EOD 实际赚钱, 到点平符合 DA 意图。真弱点是 TRAILING_STOP。 | 优化目标从"时钟类"重定向到"止损/跟踪类"。 |

---

## 7. 验证与部署状态

- **代码质量**: 本周所有改动文件 `py_compile` + `GetProblems` 无错; `invalidation_shadow.py` 自测 6/6。
- **端点**: `TestClient` 验 chain 5 端点均 200/401 (auth-gated, 路由已注册, 非 404)。
- **服务**: `ftseek-dashboard.service` (:8901) 已重启 **active (running)**, 启动日志无异常。
- **待周一生效**: Stage 1 invalidation 列由 `ensure_tables` 盘前自动迁移; 校准 go-forward 干净样本卡 `date≥07-11`。
- **未 commit**: 主题一 (双脑清理) 代码计划周末落地, 其余主题为工作树改动, **均未 git commit**。
- **审阅回环 (✅ 2026-07-11)**: 审阅 Agent 报"DA→执行脑 3 数据断裂", 已逐条实证 (详见单脑文档
  "Stage 1 断点复核"): invalidation 全空=根因误判 (数据在 JSON 顶层, 路径正确, 待周一 DA 首跑自愈);
  position_size 全0=夸大 (仅 DA 路径审计断裂, 成交单已正确带 size), **已修** `db.py _TRACKING_FIELDS`
  补 `position_size`/`take_profit`; invalidation_shadow=claim1 下游, 自愈。内存库往返测试通过。

---

## 8. 供审阅者的重点核查清单

1. **双脑清理**: fallback 默认值取舍 (`none` vs `technical`); voting_coordinator 隔离是否留有复燃路径; 删死门禁是否确为零行为变化。
2. **决策契约**: Stage 1 invalidation 落库是否真的对下游报表零影响 (da_thesis 不覆盖模板 thesis); 周一列迁移是否幂等安全。
3. **校准**: confidence 来源改 JOIN playbook_instructions 后, 老单回退旧三源是否会再引入不一致; U 型非单调是否只是小样本噪声。
4. **退出**: TRAILING_STOP 系统性亏损结论是否成立 (样本 47 笔够不够定性); 量纲夹取阈值 50 是否合理; invalidation 影子"不实平"接入点是否可能误触发。
5. **诚实性**: §6 五处勘误的证据链是否可复现 (尤其 #1 量纲、#3 跨制度污染)。
6. **风险边界**: 全程富途模拟盘; 未 commit 状态下审阅需针对工作树。

---

## 9. 源文档与关键代码位置索引

| 类别 | 路径 |
|---|---|
| 方案·双脑清理 | `docs/proposals/decision-chain-single-brain-cleanup-20260711.md` |
| 方案·退出引擎 | `docs/proposals/exit-engine-optimization-20260711.md` |
| 方案·退出测量(未执行) | `docs/proposals/PLAYBOOK退出测量优先重构.md` |
| 决策链路可视化 | `app/web_dashboard/routes/chain.py` (5 端点) |
| invalidation 影子 | `app/playbook/invalidation_shadow.py` |
| MFE/MAE 回填 | `scripts/backfill_mfe_mae.py` |
| 链路/MFE 探针 | `scripts/check_decision_chain.py` (`--days/--calibration/--invalidation/--mfe-health`) |
| 退出主逻辑 | `app/playbook/pipeline.py` (监控循环采集 + `_close_trade_record` + 影子接入) |
| 建仓写入 | `app/playbook/playbook_service.py` (metadata 写 invalidation) |
| 退出参数 SSOT | `config/playbook_horizon.yaml` (`factor_exit_rules`) |
