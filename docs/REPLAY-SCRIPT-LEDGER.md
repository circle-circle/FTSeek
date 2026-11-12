# 回放/回测脚本台账 (REPLAY-SCRIPT-LEDGER)

> 2026-08-30 建 (方法论收敛①)。解决问题: 29 个回放脚本散落, 复用靠手工, 活性无人知。
> 官方引擎: `scripts/exit_replay_backtest.py` — 公共原语:
> `load_trades` / `fetch_bars`(带缓存) / `collect_bars` / `replay_trade` / `_fee_of` / `aggregate` / `_parse_et`
> **公约**: 新写回放脚本必须 `import exit_replay_backtest` 取原语, 只写差异逻辑;
> 禁止再手搓取 bar/缓存/费用/交易加载。违者同"控制环路未入账"处理。
> **公约附则 (2026-09-07 增, DEB-20260907 §7.4)**: 被本公约指定为"单一入口"的模块
> **必须有 import 级钉子** (`tests/test_replay_engine_import_smoke.py`)。根因: 引擎在
> 提交 14e9a544 (P2a 搬家) 后 ImportError 36 小时, 全量回归 0 红、6 个复用方被打穿
> (含 3 个 cron 活体的**懒 import** ⇒ 模块级检查全绿, 只在 cron 跑到那行才炸)。
> 公约要求所有人复用引擎, 却没人给引擎上锁 = 公约自身无牙。

## 状态定义

- **活体**: 挂 cron 或被影子注册表引用, 持续产出
- **证据冻结**: 被已裁决 DEB 引用为证据基线 — **不可改不可删** (改了=裁决记录失真)
- **候选退役**: 零 DEB 引用 + 无日志活性, 删除前需人工确认内容无未落档价值

## 台账

| 脚本 | 行数 | 状态 | 归属/证据 |
|---|---|---|---|
| exit_replay_backtest.py | 709 | **活体·官方引擎** | 10 份 DEB 引用的回放基线; **2026-09-07 修 ImportError** (退出数学 import 由 `pipeline` 改指 SSOT 本体 `exit_engine`; P2a 撤了 pipeline 侧 `_compute_dynamic_effective_sl` 转发器) + 补 import 冒烟钉子, 见 DEB-20260907 §七。**2026-09-07b**: `load_trades` 增出 `decision_id` 一列 (重放器 DEAD_SIGNAL 的 reentry 呼吸窗判据; 全窗 19 笔 reentry), 纯增列不改既有键 |
| limit_entry_shadow_replay.py | 267 | **活体** | cron 挂载 + 注册表 eval + 近30天有日志 |
| phase1_gate_backtest.py | 267 | 候选退役 | 原影子注册表 p1_3_vol_p50 eval; 2026-09-01 p1_3 裁杀 (5天3样本, 用户裁) 后零活体引用, 删除前需人工确认内容无未落档价值 |
| entry_gate_replay.py | 215 | 证据冻结 | 3 份 DEB |
| gate_a_mfe_replay.py | 426 | 证据冻结 | 2 份 DEB (复用官方引擎×3) |
| overnight_loser_multiday_replay.py | 238 | 证据冻结 | 2 份 DEB |
| e2_momentum_confirm_replay.py | 204 | 证据冻结 | 2 份 DEB |
| entry_band_replay.py | 351 | 证据冻结 | 1 份 DEB |
| 其余 19 个 (各 1 份 DEB 引用) | — | 证据冻结 | symbol_rule/vol_arm×2/holding_extension/overnight_loser/e2_swing/e1_preview/momentum_eod/mfe_stall/no_trigger/capital_flow/position_size/long_to_short_flip/loser_hold_path/cap_spectrum/admission/swing_hold/probe_pyramid/exit_replay_shadow/limit_entry_shadow(兼活体) |
| trend_gate_replay.py / overnight_1m_replay.py / minute_rules_replay.py / expectancy_ic_backtest.py / winner_trail_backtest.py / daily_r9r10_replay.py / ah_exit_replay.py | — | **候选退役** | 零 DEB 引用 + 无日志活性, 删除前需人工确认内容无未落档价值 |
| be_counterfactual_replay.py | 63 | 候选退役(特殊) | 零引用, 但系官方引擎复用范本 (MU案), 建议保留作模板 |
| cf_pullback_limit_replay.py | 999 | **活体·唯一现行判读入口** | DEB-20260901 Phase B/C: cron 47 5 * * 2-6 + 注册表 pullback_limit_shadow eval; 读官方引擎 bar 缓存格式 (replay_bars/, 只读零拉取)。**v2 (2026-09-07, DEB-20260907 §三)**: 双臂同尺 (两臂都走 exit_path_replay、同为净额), 修 v1 两处反向口径缺陷; 新增保真度门 (reason_match_rate<0.8 ⇒ ADR-035 四判据一律不作数)。**2026-09-07b (DEB-20260907 §4.8)**: 保真度门重写为「**同纪年 ∧ 当日出场**域 × arm_lag=1 × `sample_source` **两口径都过**」(缺格 fail-closed), 新增 2×2 `fidelity_matrix`、两道数学上限 (`ceiling_cross_day` / `ceiling_dead_epoch`)、`caliber_sensitivity` (两口径配对差中位数异号 ⇒ `NOT_EVALUABLE_CALIBER_SPLIT`); 实盘臂按 4 口径全跑, cf 臂增 `sens_bar_close` 敏感性臂; 传 `decision_id` 给重放器。**2026-09-07c (DEB-20260907 §4.9 判据换轨)**: 新增 `ruler_license` 三级(四许可)卡 (L0 逐笔 / L1a 配对差 / L1b 聚合对账 / L2 路径归因; 阈值预注册 0.25·0.80, 许可域 = 既有门域, 两 `sample_source` 都过才发放, 缺指标 fail-closed)、`_epoch_guard` (机械自更新的纪年守卫, 门域内 n≥5 且复刻率=0 ⇒ `NOT_SINGLE_EPOCH` + `epoch_floor`=末次日期+1; 替代人工点名 `dead_epoch_tracks`, 并附 `masked_zero_match_tracks` 披露被域收窄藏起来的坏轨 — 实测 `SL_INITIAL` 全口径 n=8 复刻 0 而当日域仅 n=4)、`_readout_card` (6–7 行人话读数卡, 随 stdout + JSON 落盘)、`MONEY_CRITERIA_BINDING_FROM="2026-09-08"` (生效日之前的窗只出 `PROVISIONAL_INDICATIVE_ONLY`, 防「用自己设计的门裁自己」)。既有 `evaluability` 字段全部保留 |
| exit_path_replay.py | 570 | **活体·库(无独立入口)** | DEB-20260907 §三: cf_pullback_limit_replay v2 的**唯一重放器** (两臂同尺的实现载体); 复用 `exit_engine._compute_dynamic_effective_sl`/`_get_factor_exit_config` + `fee_model.estimate_round_trip`; **刻意不 import 官方引擎** (只需 bar 循环, 不需取 bar/加载交易 ⇒ 不违公约), 亦**不调 `_check_playbook_exit_rules`** (该函数写生产影子文件且依赖墙钟); 无 cron、零副作用。**2026-09-07b (DEB-20260907 §4.8 保真度专项)**: ①补三轨 `DEAD_SIGNAL_EXIT`/`MFE_STALL_EXIT`/`TIME_EXIT` (实盘同序、配置驱动、含 reentry 呼吸窗与浅亏转持有分支) ⇒ `rules_omitted` 只剩真需跨日 bar/墙钟的 5 项; ②新增两个**双报口径**参数 `arm_lag_bars` (0=乐观上界/1=订单因果下界) 与 `sample_source` (bar_extreme=观测上界/bar_close=下界), 非法口径名直接 `raise` 防"双报退化成同口径"; ③`fidelity_report` 增当日域切分与 `ceiling_cross_day`。仍零副作用、仍不调 `_check_playbook_exit_rules`。**2026-09-07c (DEB-20260907 §4.9)**: `fidelity_report` 增**钱口径四指标** (`sign_agreement_rate` / `bias_to_mae_ratio` / `median_abs_db_net` / `mae_over_median_abs_net`) — 与 `reason_match_rate` **并列不替换** (判别力实证: 烂窗 7/11–8/14 标签一致率仅 0.384/0.444 而钱口径照样满分过关 ⇒ 钱口径看不出路径归因已失效); 并修掉零误差时 `bias_to_mae_ratio` 除零退化成 `None` 的缺陷 (`None` 在许可卡里=不许可 ⇒ 完美尺子反被否) |
| ticket_size_breakeven.py | 678 | **活体·手动(建议不入 cron)** | DEB-20260907 §五: 客单价费率盈亏平衡账。**非回放** (无 bar、无网络), 只读 `ftseek_us.db` + `unified_config.yaml`, 费率走 `fee_model.estimate_round_trip`; 输出 `data/shadow/ticket_size_breakeven_latest.json`。归口本台账防散落。档位反事实仍归 `admission_replay.py` (勿重复) |
| trivial_rule_control_arm.py | 805 | **活体·手动(建议不入 cron)** | DEB-20260907 §六: 全系统第一个**对照臂** (浮现层 `signal_snapshots`, 五条平庸规则臂, 按日聚类配对差 + MDE@80% + 四离群口径同号表)。**非回放**: 只读 `replay_bars_daily` 缓存、零网络拉取 (同 gate_counterfactual_ruler 族口径); 输出 `data/shadow/trivial_rule_control_arm_latest.json` |
| gap_alignment_shadow.py | 170 | **活体** | DEB-20260901 P1/Phase C: cron 52 5 * * 2-6 + 注册表 gap_alignment; 影子落盘非回放 (无 bar 拉取), 归口本台账防散落 |
| rfc192_verification.py | 355 | **活体·手动(建议不入 cron)** | RFC-192 复核尺子 (2026-11-08)。**非回放**: 只读 `ftseek_us.db` (mode=ro) + `data/da_checkpoint/`, 零网络、零写入 (连 `data/shadow/` 都不写), 费率走 `fee_model.estimate_round_trip`。答 8 问: A 宇宙/基线复现 (三套"现状"基线各自出处) / B **开盘跳空 vs 盘前跳空**两口径的阈值表与一致性 / C 单地板 vs 双地板费率 vs 实测 / D sizing 机制与 C1 可达区间 (含"历史风险预算不可还原"的证明) / E A·A'·B 方案复算 (日聚类 t) / F 时代分层 (分月) / G RFC-192 §5 五道门逐条可达性 / H 多空拆分。**同一 gap 口径的阈值表逐位复现 DEB §54.3** (143/+1609.03, 205/+953.20, 230/+865.66, 307/+774.48, 362/+544.12, 全做 499/−400.35) ⇒ 该尺子与被审文档同源可核。被 RFC-192 §10 与 RFC-191 §8.6 引用为证据基线 ⇒ **证据冻结** (改它=裁决记录失真) |
| session_attribution.py | 759 | **活体·手动(建议不入 cron)** | 交易日归因尺子 (2026-11-10, DEB-20261109)。**非回放**: 只读 `ftseek_us.db` (mode=ro) + `data/da_checkpoint/` + `data/shadow/exec_gate_audit.jsonl` + `data/shadow/ext_entry_risk_shadow.jsonl`, 零写入。段: A 三口径对照 (开仓日/出场日/生成时点快照) + 未平仓与跨日现存仓 (含 TP/sync 状态) / B 按臂拆分 + 跨日平掉的遗留仓清单 / C 出场归因 **exit_reason × be_exit_class**(OK/GAP/SLIP) 双层分组 / D 闸门拦截分类 + exec_gate_audit 当日 action 分布 / E 跨臂方向冲突 (DA 方向 vs EXT 方向, 含**被拦但未执行**的 DA 指令) / F 盘前跳空阈值反事实 (RFC-192 C2 口径候选 a) / G 只读体检三件套 (无 TP 跨夜仓含 EXT 退出契约 / 同标的平后立即重入 / 跨臂相反暴露) / H EXT 入场风险影子读数 (覆盖率 + 冲突组 vs 非冲突组) / I 月初入场 cohort 盈亏结构 (打平胜率/费毛比/入场时段/原始SL与early-tight分型, DEB-20261111) / J DA planned-effective-fill 三层价格账 (严格 decision_id, DEB-20261111) / K DA 输入新鲜度审计 (覆盖≠实时, DEB-20261111) / L 数据准确度覆盖 (TCA slippage/entry_price_source、stop_loss_price 缺值、legacy 卫生, DEB-20261111 Batch A)。设计意图: 盘后复查最常见的两类错 —— **口径混用** 与 **只看标签不看分层**。被 DEB-20261109 引用 ⇒ 随该 DEB 演进 |
| da_input_freshness_manifest.py | 178 | **活体·手动(建议不入 cron)** | DA raw 输入 per-source as-of/session/age/fallback/degraded 只读清单 (DEB-20261111 Batch A)。**非回放**: 只读 `data/da_checkpoint/<date>/*.raw.json`, 零写入、不接交易门。输出 dimensions 聚合 + quality/VIX/snapshot/source_status 覆盖。被 DEB-20261111-full-chain-accuracy-inspection 引用 |
| gate_interception_daily.py | 280 | **活体·手动(建议不入 cron)** | 闸门拦截逐笔审计 (2026-11-10, DEB-20261109 Batch 0.4/0.5)。**非回放**: 只读 `playbook_instructions` + `trade_records` + `data/shadow/exec_gate_audit.jsonl` + `data/vwap_gate_ab.jsonl` + `config/unified_config.yaml` (阈值真值不硬编码), 零写入。段: A 当日各闸门拦截概览 / B **贴边排行** (实际量/阈值, ≤1.15 记贴边; 参数取自 skip_reason 的已舍入值) / C **vwap A/B 四象限** (真bypass / 假bypass / gated但成交 / gated被拦 ⇒ 量化双臂可比性) / D 与既有记分牌 `data/gate_block_counterfactual_latest.json` 的裁决对照 (避免重复造尺子)。**不改任何拦截行为** |
| migrate_ext_open_exitprice_null.py | 95 | **一次性迁移(已执行)** | DEB-20261109 Batch 0.1: 未平仓行 `exit_price=0` → NULL (对齐规范写入方 `app/storage/trade_store.py:823`)。dry-run 默认 + `--execute` 自动备份 + 幂等 + 校验。**已于 2026-11-10 19:32 执行** (1 行: id=1828 US.CVS; 备份 `data/ftseek_us.db.bak-deb20261109-b01-20261110-193246`; 校验全库 `exit_price=0` 计数=0)。**证据冻结** (改了=迁移记录失真) |
| align_playbook_recovery_display.py | 161 | **一次性修复工具(默认 dry-run)** | RFC-195 后续: Playbook 恢复合成 SL/TP 展示污染 CAS 对齐。默认只报告；`--apply` 才写 open_positions，逐行审计 `applied/rowcount`，`updated_at=now_et()`；11-11 已执行并留 `data/audits/playbook_recovery_display_*.jsonl`。不碰 trade_records 出场真值。 |
| us_overnight_shadow.py / us_overnight_shadow_eval.py | 170 / 78 | **活体·手动 record-only(未挂 cron)** | 夜盘只读快照与汇总器；写方受 `US_OVERNIGHT_SHADOW` 环境开关控制，周末/假日停采，跨午夜按 session_date 归档；evaluator 只读行数/标的数/坏行/最新快照，NO_DATA 非零。已注册 `us_overnight_shadow`（PERMANENT_MEASURE）；零下单、零交易 DB 写入、无收益判据。 |
| archetype_bars_ensure.py | 94 | **活体** | DEB-20260901 Phase B 补盲: cron 35 5 * * 2-6; `import exit_replay_backtest.fetch_bars` 复用 (公约合规), 工具类非回放 |
| da_blocked_signal_cf_eval.py | 240 | **活体** | DEB-20260901 Phase C 观察点③ (B14): cron 15 6 * * 2-6 + 注册表 da_blocked_cf; 日K缓存 CF 评估族 (同 gate_counterfactual_ruler 口径, 只读零拉取) |
| entry_blackout_shadow_eval.py | 468 | **活体·手动(建议不入 cron)** | RFC-200 §6/§8 开盘禁入窗黑窗影子只读评估器 (2026-11-12)。**非回放**: 只读 `data/entry_blackout_shadow.jsonl`, 零生产写入 (仅 `data/shadow/entry_blackout_shadow_eval_latest.json` 产物), 不接交易门。答: cycle 覆盖/首末时间/取价异常 (QUOTE_ALL_FAIL / INCOMPLETE) / R1 writer_status / hit 按 (date, inst_id) **最早 ts** 去重 + duplicate_hit / decision_id 源分桶 (DA/MOM/OTHER)。`band_would_trigger` 只是带几何上限、不算收益。状态机 OK/INCOMPLETE/QUOTE_ALL_FAIL/NO_CYCLES/NO_DATA/READ_ERROR: 无 hit 不报错, 无文件/无 cycle 非零。 |
| entry_blackout_window_stats.py | 132 | **活体·手动(建议不入 cron)** | RFC-200 §8 历史开盘时间桶 SQL 统计 (2026-11-12)。**非 bar 回放**: 只读 `trade_records` (mode=ro), 净盈亏 `pnl_net` 优先否则 `realized_pnl-fees_est`, 未平仓单列, 输出各 ET 入场桶与日簇描述性区间；零写入、不接交易门、不是因果反事实。 |

## 唯一现行判读入口 (2026-09-07c 立, DEB-20260907 §4.9)

> **只看一个命令、一个输出。** 用户 2026-09-07: 「历史的尺子和回测太多了, 我都有点迷糊了,
> 我需要真实可用保真的数据, 才能有准确的依据。」⇒ 台账不再靠"多把尺子互相印证"取信,
> 而是指定**唯一判读入口**, 并让该入口自己声明它能回答什么。

- **命令**: `./venv/bin/python scripts/cf_pullback_limit_replay.py --from-date <起> --to-date <止>`
- **看哪里**: stdout 末尾的 `读数卡` (同一份内容在 `data/shadow/pullback_limit_replay_v2_latest.json`
  的 `summary.readout_card` / `summary.ruler_license`)。**先读「效力」行, 再读「结论」行** —
  效力为「指示性」或「不可用」时, 结论行不得被任何 DEB/RFC/ADR 引用为裁决依据。
- **卡上四级的含义**: `L0 逐笔归因` / `L1a 配对差(两臂同尺)` / `L1b 聚合对账(对账本)` /
  `L2 出场路径归因` —— ❌ 的那一级对应的那类话**不能说**, 与四判据过没过无关。
- **三道独立的闸** (缺一不可, 都在 `ruler_license` 里; **尺子许可 ≠ 裁决资格**):
  ①**生效日** `MONEY_CRITERIA_BINDING_FROM=2026-09-08` — 钱口径判据在 9/1–9/4 窗上设计 ⇒
  裁该窗 = 凑门 ⇒ 只出 `PROVISIONAL_INDICATIVE_ONLY`;
  ②**宪章 n** `MIN_N_FOR_CONCLUSION=30` — 管「能不能说任何结论」。实测 9/4 单日 n=6 时
  四级许可**全部发放**且 6/6 同向 ⇒ 符号检验 p=0.016<0.05, 若无此闸就会每天打出一张自称
  「正式」「有统计学证据表明优于」的小样本卡 ⇒ n<30 时卡上必有「⚠ 样本不足 … 不得被引用为
  裁决依据」, 且**结论行只报点估计**, 不写「有统计学证据」字样;
  ③**裁决 n** `ADR035_VERDICT_MIN_N_PAIRED=40` — 管「能不能裁 `entry_archetype_routing_enabled`」
  这个开关。与 ② 分开是因为: 只用 30 ⇒ 9/8 之后第一个 n=30 的窗就会 `verdict_eligible=true`,
  把「攒够样再判」悄悄换成「攒到地板就判」⇒ 30≤n<40 时卡上必有「⚠ 未达本裁决门槛 … 可说方向,
  不得裁开关; 不得降门槛, 也不得把生效日之前的窗并进来凑数」。
- **正式裁决点 (用户 2026-09-07 裁「按照建议执行」= 手动跑, 不入 cron)**: **9/12(六) 中期体检**
  `--from-date 2026-09-08 --to-date 2026-09-11` (预期 n≈30, 只验证三道闸咬合, **不裁决**) →
  **9/16(三) 正式裁决** `--from-date 2026-09-08 --to-date 2026-09-15` (预期 n≈45 ⇒
  `verdict_eligible=true`)。分支规则见 DEB-20260907 §4.9 末 / `handoff.md` 下一步 `0E`。
  cron (`47 5 * * 2-6`) 仍只跑单日 ⇒ **cron 日志里的卡永远是「无裁决资格」**, 这是设计使然。
- **其它脚本的定位**: `exit_path_replay.py` 是它的重放器(库, 无独立判读入口);
  `exit_replay_backtest.py` / `trivial_rule_control_arm.py` / `ticket_size_breakeven.py`
  各答**另一个问题**(官方引擎回放 / 对照臂 / 费率账), 不得与入口读数混算或互相冒充。
- **禁止**: 拿"证据冻结"脚本的历史读数当作现行判据; 拿全日域 `fidelity_matrix` 的好看数字
  替换门域读数(= 挑口径); 拿 `per_day` 的单日卡拼接成窗口结论(宪章禁跨窗拼接)。

## 归口执行记录

- 2026-08-30: 台账建立 + 官方引擎公约写入。实测复用率 4/29 → 目标: 新增脚本 100% 复用。
- 2026-09-01: DEB-20260901 Phase C 补登记 4 脚本 (前三个 8/31-9/1 上线时漏登记, 本轮勘误入册)。日K缓存 CF 评估族 (gate_counterfactual_ruler / da_blocked_signal_cf_eval / t5_hold_counterfactual) 均只读 replay_bars_daily 缓存、零网络拉取, 与官方引擎 1 分钟回放分工明确, 不视为公约违例。
- 2026-09-01: p1_3_vol_p50 用户裁杀 → phase1_gate_backtest.py 转候选退役 (零活体引用; 脚本保留备查, 其历史读数未被他档引用为证据基线)。
- 2026-09-07 (DEB-20260907): ①**官方引擎救活** — `exit_replay_backtest.py` 自 P2a 提交 14e9a544 起 ImportError (从 `app.playbook.pipeline` 取已被撤掉转发器的 `_compute_dynamic_effective_sl`), 级联打穿 6 个复用方: 模块级硬断 `limit_entry_counterfactual` / `entry_gate_replay` / `be_counterfactual_replay`, 懒 import 断 `archetype_bars_ensure`(cron 35 5 * * 2-6) / `post_exit_path_shadow`(cron 45 5 * * 2-6) / `position_path_collector`(cron 15 5 * * 2-6)。修根因 (改指 SSOT `exit_engine`, 数学零变化) + 新增 `tests/test_replay_engine_import_smoke.py` 9 例; **首次带病 cron = 9/8**, 活体旁证 `post_exit_path.jsonl` 末次成功写入 9/5 05:45。②**新登记 4 脚本** (exit_path_replay / ticket_size_breakeven / trivial_rule_control_arm 新增, cf_pullback_limit_replay 升 v2)。③**公约附则**写入 (单一入口必须有 import 钉子)。④**盲区登记**: 台账"证据冻结"状态的脚本既不会被改也不会被测 ⇒ 静默腐烂无人知, 本次 36 小时即实例; 冻结不等于豁免冒烟。
- 2026-09-07c (DEB-20260907 §4.9 判据换轨): 给唯一现行判读入口装上**自我声明**能力 — 三级(四许可)卡 + 机械纪年守卫 + 人话读数卡。台账含义三条: ①**尺子必须自带分辨率声明**, 「过门/不过门」这种单一二元读数会同时过松与过严 (9/1–9/4 实测: 逐笔不可判读 MAE/中位净额 = 1.25–1.88, 而两臂同尺的配对差可用 ⇒ 同一把尺子部分可用); ②**判据生效日必须落库** (`MONEY_CRITERIA_BINDING_FROM`), 在设计判据所用的窗上裁决 = 凑门; ③**纪年守卫要从数据算不能人工点名** — 人工字典漏一条轨, 整窗读数的数学上限就被误读成「重放器不努力」。零 live 改动 (`app/` 与 `main.py` 未碰), DB 全 `mode=ro`, 零历史回填。
- 2026-09-07b (DEB-20260907 §4.8 重放器保真度专项): `exit_path_replay.py` 三步全部落地 (三轨 + arm_lag + sample_source), 保真度 全窗 0.400→**0.570**、8/15 单纪年∧当日域 →**0.791**、ADR-035 窗当日域 0.633→**0.786/0.821**(两口径)。**新登记两道结构性上限**: 参数纪年 (MFE_STALL/TIME_EXIT 现纪年不可达 ⇒ 全窗上限 0.843) 与跨日出场 (8/15+ 窗 20/178 ⇒ 上限 0.888) ⇒ **0.8 门只能在「同纪年∧当日出场」域上要求**, 两条排除预声明并随报告落盘。台账含义: 回放脚本的保真度读数**必须带域与口径标签**, 否则不同窗/不同口径的数字会被互相冒充 (本批之前 0.400/0.633/0.646 三个读数就曾在同一句里被当成同一条尺子)。
- 2026-11-08 (RFC-192 复核批): 新登记 `rfc192_verification.py` — **只读尺子, 不入 cron**。
  缘起: RFC-192 与 DEB §44/§54 的关键读数 (499 笔阈值表 / A·A′·B 方案) **没有产出脚本**,
  且同一份 RFC 出现三套互不吻合的"现状"基线 (−$400 / −$882 / −$1,270) ⇒ 违反"没有命令的读数等于没有读数"。
  尺子复现了 DEB §54.3 的阈值表逐位数值, 并补出三条原文没有的事实:
  ① 阈值表用的 gap 字段是**开盘跳空** (`entry_gap.aligned_gap_pct` = open vs prev_close) 而非盘前跳空;
  ② 费率是**双地板** `max(39800/N, 262/P)+0.21`, 第二项与名义无关 ⇒ 抬帽压不掉;
  ③ 历史风险预算不可还原 (549/598 行 `risk_amount` 被 re-price 覆写为派生值) ⇒ C1 只能给区间。
  台账含义: **判据类文档必须自带可复算脚本**, 否则其"改善幅度"不可判读;
  且**阈值类证据必须与被执行层读同一个字段** (字段口径漂移 = 拿没标定的阈值去筛)。
- 不做的事: 不重写证据冻结脚本 (可复现性 > 整洁); 不建回放框架抽象层 (脚本即一次性证据, 框架化是过度设计)。
