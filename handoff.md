# FTSeek-US · HANDOFF

> **这是什么**: DA-FTSeek 链路迭代的**活文档**。每次调整/计划都追加到这里, 避免跨会话/跨 Agent 从头开始。
> **怎么用**: 每次改动后更新"变更日志"(最新在最上) + "下一步"。其他 Agent 先读本文件再动手。
> **最后更新**: 2026-07-19 · 环境: 富途**模拟盘** (v2 sim, 无真实资金) · venv: `./venv/bin/python`

---

## 🎯 当前北极星 (换过 KPI)
- **只追"每笔期望值 ≥ $0"**, 不追胜率/周收益数字。收益是期望转正后的自然结果, 直接追会逼出"加仓凑收益"的自杀行为。
- 活跃 OKR: **`docs/OKR-2026-08.md`** (2026-07-14→08-31)。上一轮 `docs/OKR-2026-07.md` 已结算 **MISSED**。
- 重心在 **FTSeek 退出侧**, 不在 DA 预测侧 (见下"核心诊断")。

## 🔬 核心诊断 (数据实证, 别再推翻重来)
用 `scripts/exit_scorecard.py` 实测 (污染窗口 n=61/71):
1. **信号 ≈ 掷硬币偏正, 不是问题**: 持到收盘命中 50.8%, 均值 +0.37%/笔 → DA 有薄弱正 edge, 达到">50%"的诉求。
2. **漏血在退出**: 退出把 8 笔本该收盘为正的赢家做成亏损, 只救回 2 笔 → 胜率 50.8%→41%。
3. **R:R 结构性不足**: avg赢 $118 ≈ avg亏 $110, R:R=1.07 < 盈亏平衡 1.29 → 每笔期望 **−$10.57**。
4. **confidence 反向**: 低信心桶反而更好 (Playbook IC 为负) → 按 confidence 加仓 = 给更差信号加钱。**已拍平** (见变更日志)。

一句话: **不需要更准的预测; 需要退出别砍赢家 + 把 R:R 拉过 1.29。**

## 🧭 每周迭代循环 (每周五盘后)
```
1. 跑 scripts/exit_scorecard.py            → 期望值 / 杀赢家数 / R:R 缺口
2. 只问: 赢家还在被砍吗? R:R 离 1.29 还差多少?
3. 只改一个退出参数 (其余全冻结)
4. 下周同一张表对比: 变好→保留, 变差→回滚
铁律: 一次一个变量、一个指标、可回滚。禁止在期望转正前加仓/放大 R:R 追收益。
```

## 🛠️ 工具
| 工具 | 用途 |
|---|---|
| `python scripts/exit_scorecard.py [--since 2026-07-11] [--days N]` | 退出纪律周度判分 (只读)。落 `data/exit_scorecard_latest.json`+history |
| `python scripts/okr_status.py` | OKR 总览 (胜率/PnL/IC/因子)。落 `data/okr_latest.json` |

## 🗺️ 退出旋钮地图 (`config/playbook_horizon.yaml`, DA 恒走 `short_horizon`)
**A. 赢家被锁太小 (R:R 缺口主因) — `factor_exit_rules.default`**
- `drawdown_from_peak_pct` = **0.5** (L45) ← 浮盈回撤0.5%就平, 锁太紧, avg_win 上不去。07-09 从 0.8 收紧来的。
- `breakeven_at_pct` = 1.0 (L44)

**B. 赢家被噪声扫损 (杀赢家) — `short_horizon.vol_buckets`**
- low `sl_floor_pct`=2.0 (L370), medium=2.5 (L378) ← 多数标的落此, 实际止损≈2-2.5%
- ⚠️ 07-09 基于**日线回测**收紧过("2-3%最优"); 与 live scorecard 冲突时**信 scorecard**。

**C. 已核实**: `short_horizon.rr_ratio=999` (L353) → 固定止盈**关闭**, 靠 trailing+EOD。所以封顶赢家的是 A 的 drawdown 锁, **不是** rr_ratio。

## ➡️ 下一步 (下周五第一个动作)
**改 A: `drawdown_from_peak_pct` 0.5 → 0.8** (先只改 `default` L45)。
- 目的: 让赢家跑, 抬 avg_win, R:R 过 1.29。一个数字, 秒回滚, scorecard 判分。
- A 不够再动 B (`sl_floor` low/medium +0.5%)。**别 A/B 同时动。**

---

## 📝 变更日志 (最新在上)

### 2026-07-19 · DEB-round3 pool→trade 链路审计修复批次 (commit `74defd97` + `5ccad370`)
- **辩论锚点**: `docs/debates/DEB-20260719-pool-to-trade-chain-audit.md` (三方收敛执行清单 v2)。P0 5 项 + P1 5 项全量落地。
- **#1 bridge 三态告警**: `opportunity_scan_{date}.json` 缺失/空/schema非法 → 飞书 HIGH (当日去重)。新增 schema fail-safe (非法视作缺失不消费半损坏文件)。覆盖"启动后文件仍缺"盲区 — `run_generate` + `auto_execute` (每5min) 双入口直读文件, 不依赖启动时一次性加载。
- **#2 失效门组** (`_check_intraday_trigger`): ①触发窗收紧 **14:00 ET** 截止 (`entry_time_cutoff_enabled`, 默认True); ②财报禁建日拒触发 (`earnings_blackout_gate_enabled`, 复用 `scheduler.earnings_risk_guard`); ③漂移>5% **指令作废** (`thesis_drift_invalidate` 写 skip_reason 白名单落库, auto_execute+trigger 双保险跳过; trigger_miss 遥测即 shadow 记录)。
- **#3-B**: SCAN 注入 `expires_at=24h`; **消费端首轮遗漏只写不读, 本轮核查发现已补** (commit `5ccad370`: auto_execute 跳过 + trigger 拒触发双保险)。
- **GHOST 根因修复**: RC1 — `playbook_service._confirm_order_filled` 入场单 order_status 短轮询≤5s, 未成交拒记账+释放抢占 (与 close 路径共用 2026-07-07 确立的真相源; 查询异常降级维持旧兜底); RC2 — `_classify_ghost_cause` 写 `metadata.ghost_cause` (double_tracked/entry_unfilled_or_external, record-only 供 2 周复审)。
- **熔断 shadow 告警版**: 当日 ≥2 笔分钟级(holding≤0.1h)亏损平仓 → 飞书 HIGH, **不阻断** (`sl_burst_alert_enabled` 默认True)。注: holding_hours 落库 round(.,1), 故用 ≤0.1 近似 <5min 口径。
- **30min-abort shadow**: `exit_shadow.ABORT_30M_VARIANTS` 全路径采集 would_abort (MFE<0.1%且浮亏) + close 反事实 `counterfactual_saved_pct` (正=abort更优)。TIME_EXIT 11笔0%WR 根因=方向未兑现, 但实单 abort 反事实 -$160 (杀 EOD 慢热赢家) → 仅 shadow。
- **#11 IC 覆盖**: `ic_tracker` 未触发指令缺 entry_price → 当日 open 代理回填, `fwd_proxy=1` 落库标记可剔除; db 迁移列已手动触发落地 (lazy 迁移周日未跑, 明日前已补齐)。
- **#6-A**: factor_exit conditions 零命中 → WARN (进程级去重, default 兜底行为不变)。
- **#12**: `scripts/gate_cost_report.py` (stdlib 只读) — 各门拦截成本 + VWAP 门 blocked/allowed `fwd_return_intraday` 终审 + 分钟级亏损统计。实测: §4 近14天 56笔 胜率39.3% 总PnL+$40.11。
- **δ.6 清洗口径入 `docs/OKR-SPEC.md`**: void 不计 + 事件日双向对称剔除 + SHADOW 本就不计; 清洗后基线 80笔/45.0%WR/+$1422.40。
- **验证**: py_compile 全过; pytest 33+28 项通过 (4 项 pre-existing 失败经 stash 对照证实与本次无关); 服务重启 PID 408034 零 error; 新符号导入全通。
- **⏳ 明日开盘待办**: ①bridge 文件外部依赖 (Hermes ET06:53 前后产出, 周五 7-17 已正常; 若缺失 #1 告警兜底+TechnicalFallback 降级); ②#2 漂移门/熔断/bridge 告警首日上线, 关注飞书 HIGH 是否误报; ③2 周后跑 `gate_cost_report.py` + abort_30m/ghost_cause 样本复审。
- **P2 本批不做** (数据驱动后再议): #8 入场带 / #7 UE调参+DA提量。**不做**: #5/#9/#10/#6-B、30min-abort 实单版、实单熔断。

### 2026-07-12 · LLM edge 判分 (n=211) + P1 影子判决埋点 (零交易逻辑)
- **缘起**: 另一 Agent 主张"接线 LLM 擅长的质疑/环境判断"(7 项 37 行)。核心方向对 (LLM 做减法/veto, 不做加法/predict, 与北极星一致), 但它引用的支撑数字**统计无效**: conf<40 胜率 0%(n=8)、6/16 幻觉(单日 anecdote)、47% 方向(n 未知)。
- **想直接回测"挑战者质疑→盈亏" — 查完数据: 不成立**。`data/debate_verdicts/` **空**; `debates/` 是 5 月因子分箱非逐笔判决; `model_votes.model_role` **88% UNKNOWN**(无 CHALLENGER 角色); `trade_outcomes` 干净 join **n=13**; `llm_calls` 只有 hash 无正文。**当年数据根本没留**, 跑出来只能是编的。
- **唯一真实可用样本 = fuzzy (symbol, date) join `decisions`↔`trade_records` = n=211** (相关非因果)。落 **`scripts/llm_edge_scorecard.py`** → `data/llm_edge_scorecard.json` (可复现)。结果: **conviction/共识 与盈亏负相关 (r≈−0.21)** —— LLM 越自信/越一致, 结果越差。独立佐证 conf 拍平 + "LLM 不会预测"。⚠️ 置信边界: 模糊 join、时间混淆(早期 regime=None 且更赚)、混合 source_type → **强线索非因果**。
- **P1 埋点 (先造能测的数据, 再让数据决定接不接 SKIP-gate)**: DA 挑战者/校验产物**已持久化在 `data/daily_battle_plan_us.json`** (`targets[].verdict` 总判决 / `data_cited[]` 论据含 `[Futu]` 溯源标 / `factor_validation` 逐因子 `FALSE_POSITIVE` 伪信号标 / `catalyst.source_count`)。新增 **`scripts/shadow_verdict_recorder.py`** 逐日抽取 → **`data/shadow_verdicts.db`** (按 (plan_date,symbol) UPSERT 幂等)。字段: evidence_sourced/unsourced、factor_confirmed/weak/false_positive、catalyst_* 等。
- **关键工程判断**: 原计划要 `decision_id` 穿 `signal_pipeline` 做 clean trace_id 链 —— 那是**高风险治理路径**, **不做**。改用**与 scorecard 同款 fuzzy (symbol,date) join** (已验证 n=211 可行) → **零 pipeline 改动、零交易逻辑、零服务重启**。埋点仅读 JSON 产物 + 写独立库。
- **调度**: crontab 加 `30 21 * * 1-5` (服务器 CST 21:30 = ET 09:30, DA 计划生成后) 逐个交易日捕获。**回滚**: `crontab -e` 删该行即可; 删 `scripts/shadow_verdict_recorder.py` + `data/shadow_verdicts.db` 彻底清除。
- **首跑**: 落 5 条 (plan_date=07-11), 5/5 含 FALSE_POSITIVE 因子 + 未溯源论据; join 盈亏 0 行 (当日未平)。
- **⏳ 待办 (3-4 周后)**: `python scripts/shadow_verdict_recorder.py --report` 攒够 ≥30 闭环后, 比"被标记(伪信号/未溯源) vs 未标记"的期望值差 → **差显著为负才接 SKIP-gate** (阈值用该回测校准); 无差异则连质疑都无接线价值。严防像退出砍赢家那样在入场端 veto 掉赢家。

### 2026-07-12 · 交易页 Phase B+C (sim/live 隔离 + 轻量 UX, 不改交易逻辑)
- **调查结论**: 交易页数据源已基本正确 —— 实时路径 `DataService._get_env_ctx()` 因 `_is_live()==True` 恒查**真实账户** (TrdEnv.REAL/REAL_ACCOUNT_ID); 交易记录/决策质量 `slow-data` 早已从本页移除 (`trade.js` L1467)。**唯一 sim 泄漏 = DB 兜底**。
- **Phase B (后端, sim/live 隔离)**:
  - `data_service.py` `_fetch_positions_from_db`: SQL 加 `AND LOWER(COALESCE(account_type,'')) != 'simulate'` (排除模拟仓, 保留 real+NULL); SELECT 补 `account_type,owner` 写入 dict。
  - `dashboard.py` `_parse_positions`: 防御性 `continue` 跳过 `account_type=='simulate'`。
  - 根因: `open_positions` 表混存 sim+live, 券商宕机走 DB 兜底时会把**模拟仓当实盘仓**渲染 (实测当时表内唯一仓即 `account_type=simulate/owner_strategy=playbook_sim`)。
- **Phase C (前端, 轻量)**: `trade.html` `.trade-summary` 资金头条加 `position:sticky;top:0;z-index:50` → 滚动持仓/记录时资产/购买力/盈亏/新鲜度圆点常驻可见。
- 验证: py_compile ✓ / SQL 谓词自检 (sim+大写SIM 排除, real+NULL 保留) ✓ / `_parse_positions` 防御跳过 ✓ / 重启 dashboard (PID 355297) 无 error / 兜底路径实测**返回 0 真实仓** (原会返回 1 条 sim)。
- **回滚**: 删 SQL 谓词 + `_parse_positions` continue + sticky CSS 即恢复, 零交易逻辑。

### 2026-07-12 · 交易页 Phase A (数据诚实化, 不改交易逻辑)
- **背景**: web dashboard 交易页 (`/trade`) 是**手动真实实盘**界面 (非模拟盘, `manual_order_service._is_live()` 恒 True → `TrdEnv.REAL`), 要逐步替代富途 app。目标: ①数据准确无虚构 ②信噪比更好。
- **Phase A = 纯诚实化, 零交易逻辑/下单/sizing 改动, 可回滚**。核心: 券商实时不可用时**绝不把 $0/成本价伪装成真实盈亏**。
- 后端 (3 文件):
  - `data_service.py` `_fetch_positions_from_db`: DB 降级持仓打 `data_source="db_fallback"`。
  - `dashboard.py` `_parse_positions`: 透传 `price_basis`(live/cost/delayed/none) + `stale`; `_parse_account` 打 `fx_estimated`(7.8 汇率兜底路径为 True); `_calc_pnl` 加 `positions_stale` 聚合标记。
  - `trade.py` snapshot: 加 `stale` + `as_of` 快照级新鲜度。
- 前端 (`trade.js`+`trade.html`):
  - 持仓 stale → 浮盈/今日盈亏显示 `--` (不再是伪造 $0); 现价加时段徽标 (成本/延迟)。
  - 顶部持仓盈亏/今日盈亏: 降级时显示 `--`(黄)。购买力估算加 `≈` 标记。
  - 新鲜度圆点三态: **绿=实时券商 / 黄=降级·成本价 / 灰=离线** + 文字提示。
- 验证: py_compile ✓ / `node --check` ✓ / 逻辑自检 ✓ (fx_estimated 三态正确; db_fallback→stale/cost, Futu行内价→delayed)。
- **待办**: ①**重启 `ftseek-dashboard.service`** 才能生效后端改动 (前端需浏览器硬刷新)。②Phase B(sim/live 记录隔离)/Phase C(信噪比 UX) 尚未做。

### 2026-07-12
- **拍平 conf_factor** (数据支持, 可回滚): 新增 config `playbook.risk_parameters.confidence_sizing_enabled` (默认 **false** → conf_factor 恒 1.0)。改动仅 `pipeline.py` L2338-2345 reprice 块 (DA 路径); DEC 路径 `instruction_generator.py` L393 本就不 conf 加权。验证: flag 经 `get_playbook_config()` 实测=False。**开回来**: yaml 改 true。
- **新增 `scripts/exit_scorecard.py`**: 退出纪律周度判分器 (混淆矩阵 + expectancy + R:R + 杀赢家数)。只读。
- **OKR 换轨**: `OKR-2026-07.md` 结算为 MISSED (根因=退出漏血, 非预测); 新建 `OKR-2026-08.md` 以 expectancy 为核心 KR, 删除"加仓追收益"危险对策。
- **日内 IC 上线** (前序): `fwd_return_intraday` 列已迁移到线上库 `data/ftseek_us.db` (已备份 `.bak.20260712_intradayic`)。近30天覆盖 175/179。IC: intraday −0.14 / t1 −0.04 / t3 −0.09 / t5 −0.15 (污染窗口, 非定论)。
- **待办/未做**: 退出旋钮 A 的实际修改留到下周五循环; `US.US.` 双前缀脏 symbol 2 条 (id 245/323, 陈旧 DEC 行, 不影响交易) 暂不清洗, 自然 age out。
- **开盘前就绪检查 + 重启 (✅ 2026-07-12 11:24)**: 全链路体检通过 (模块导入/配置开关/DB schema+迁移幂等/sizing 模拟/服务/循环存活)。
  已**重启 `ftseek-us.service`** (新 PID 345299) 激活 conf 拍平 —— 冒烟检查全绿: 无 error、OpenD 连通、
  安全确认**模拟盘** (TRADE_ENV=1)、Futu 交易上下文连接、调度器配置加载。
  **⚠️ 周一盘前须确认**: DA 的 bridge 文件由 deep_analysis cron 在 **ET 09:00** 生成; 若缺失, 当天降级
  `TechnicalFallbackAdapter` (技术兜底), **不走 DA 信号** → 整条 DA-FTSeek 链路当天不生效。
- **源码锚点提交 (✅ 2026-07-12)**: `9c5cb2cc`(133源码/文档) + `3fb5d6c1`(补齐 2 源码)。含本会话全部改动 + 此前累积。
  **未入库(有意)**: `data/*.db`·`app/data/*.db`(live 数据) / `logs/*` / 一堆未跟踪 junk。
  **⚠️ `config/unified_config.yaml` 卡治理未提交**: 含 `entry_reanchor_enabled:true`·`trailing_enabled:true` 等
  `enabled→true` 项, pre-commit hook (SYSTEM_OBJECTIVE §5) 要求 **ADR/RFC 人审批**才能入库。
  这些值**已在运行时生效** (磁盘配置已加载), 只是没进 git 历史。要入库须先补 ADR/RFC 或经你确认 `--no-verify`。
  (提交时两个 governance hook 均触发: pre-commit 拦 yaml enabled→true; commit-msg 要求高风险改动带 `[ADR]/[RFC]/[hotfix]` 标签。)

### (更早) 2026-07-11 及之前 — 见 `docs/WEEKLY-SUMMARY-2026-07-11.md`
- 决策链路双脑清理、退出引擎批次1、校准闭环、dashboard 可视化、_TRACKING_FIELDS 审计断裂修复等。

---

## ⚠️ 长期 caveat (读之前先知道)
- **样本量**: ~49 笔/月, 干净窗口 (date≥07-11) 目前近 0 样本。多数结论 n<50, 要几周 go-forward 才显著。别把污染窗口的数字当定论。
- **模拟盘宽恕一切**: sim 不建模真实滑点/流动性; 即使 sim 正期望也未必活到实盘。
- **DA 是 repo 外的 Hermes skill**: 若信号在干净窗口被证伪 (期望仍<0), 修复点在 skill, 不在本 repo (见 OKR-2026-08 硬退出触发器 2026-08-10)。
- **纪律**: hard-to-reverse/线上写操作先备份+确认; 不为凑样本放松风控; 一次一个变量。
