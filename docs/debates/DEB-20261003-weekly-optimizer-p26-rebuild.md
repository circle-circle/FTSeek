# DEB-20261003: WeeklyOptimizer P2-6 架构收敛 — 退出参数评估回归已有真源与官方引擎

> **状态**: ✅ **已裁决并落地** — 用户 2026-10-03 裁「A」(退役收敛), 当日执行完毕 (见 §8 执行记录)。此前同日早批已裁「同意 停推送 + 复用已有的周报显示真实参数与影子参数效果对比」(P0-1/P0-3/周报承接, commit 00cccd060)
> **裁决**: 方案 A — WeeklyOptimizer/LiveBacktestComparison 整体删除 (1351 行), 退出参数评估职责收敛到既有三资产 (§4 方案 A)。B/C/D 未采纳 (死因见 §4 比较表)
> **守卫**: tests/test_weekly_optimizer_contracts.py 由绊线转墓碑 (7 例, 复活即红, 反证已做)
> **交易行为变更**: 本文档落地前 **零**。P0-1 停的是推送 (record 面 JSON/MD 仍落盘), 不碰任何生产参数
> **样本与证据**: `data/optimization_reports/` 7 周报告 (08-28 ~ 10-02) + `data/shadow/exit_param_shadow.jsonl` (533 行, 可用 473) + `config/playbook_horizon.yaml` 现行真值 + 生产 DDL (`app/storage/trade_store.py`)
> **前置实测** (本会话 2026-10-03 复算, 全部可复跑): 契约测试 `tests/test_weekly_optimizer_contracts.py` 3 xfail-strict 绊线钉住 P0-①② 缺陷现状

---

## 1. 一句话目标

**WeeklyOptimizer 的 `_replay_trade` 另造了一套 4 参数近似回放, 与生产退出链零共享代码 ⇒ 它优化的从来不是真实策略。P2-6 = 把「退出参数效果评估」职责收敛到已有的官方回放引擎 + 实弹影子判据 + `playbook_horizon.yaml` 真源, 不再维护第二套数学。**

## 2. 背景: P0 实测证据摘要 (2026-10-03 核查, 细节见契约测试与本节命令)

| # | 缺陷 | 实锤 | 复算命令 |
|---|------|------|---------|
| P0-① | 列名错配 ⇒ SL 反事实从未触发 | `load_trade_rows` 请求 `max_drawdown_during_trade`, 生产 DDL 真实列名 `max_drawdown` ⇒ `pick()` 静默回退 NULL ⇒ `_replay_trade` 止损分支 `mdd >= abs(sl)` 恒 False; 205 笔 IS 样本 mdd>0 = **0 笔**; 5 个 SL 网格值 → **1 个 pnl 序列** (逐元素相同) | `pytest tests/test_weekly_optimizer_contracts.py -v` (3 xfail) |
| P0-①b | 头条 `-0.4` = tie-break 伪影 | 480 组合 10 个并列最高分 1.654, `_subsample` 等距确定性 + 稳定排序 ⇒ 恒选网格序首个 = `PARAM_GRID["stop_loss_pct"][0]` = -0.40 (同时是 `PARAM_BOUNDS` 边界); **连续 7 周输出常量** (score 从 -2.57 摆到 +2.44 而 sl 恒 -0.4) | `ls data/optimization_reports/` 逐份读 best_params |
| P0-② | 量纲猜测错 | `_row_to_trade` 的 `mdd > 1.5 → /100` 启发式: 生产 `max_drawdown` 全列百分数 (0.45~7.5), 阈值把人群劈两半量纲差 100 倍; 修好列名后 5 个 SL 值 score 全 **-999** (被 `MAX_DD_HARD_LIMIT=0.30` 淘汰) ⇒ **修复后输出「无解」** | 契约② xfail 绊线 + 本会话 mae_pct 代理重跑 |
| P0-③ | live 基线虚构 | `LIVE_CURRENT_PARAMS` 自称「与 unified_config.yaml 对齐」: sl=-0.045 / tp=0.025 / trailing_stop_trigger=0.01 在 `config/*.yaml` + `.env.us` **全部零命中** (trailing_stop_trigger 键名不存在且全仓零消费点 = 死参数); playbook 链真实退出参数在 `playbook_horizon.yaml` (be=0.4 / dd=0.8 / trailing_step=1.5~2.0 / time_exit=6h) | `grep -rn 'stop_loss_pct: -0.045\|trailing_stop_trigger' config/ .env.us` |
| P0-④ | 4 参数 2 个不属于被测链 | `min_signal_strength` playbook/plan 零消费 (unified_config 4 处不同段无单一真值); `trailing_stop_trigger` 全仓零消费 ⇒ 报告建议调的参数不改变产生样本的那条链的任何行为 | `grep -rn 'min_signal_strength' app/playbook/ app/plan/` (零命中) |
| P0-⑤ | OOS 统计失效 | IS/OOS 窗口双闭区间重叠 (9/25 6 笔同进两窗); 头条 n=41 vs 实评 n=9 (mss=70 过滤后); bootstrap CI [-6.94, +35.11] 跨零 42 单位宽; `degradation=max(0,(IS-OOS)/IS)` 在 OOS>IS 噪声下恒 0 ⇒ overfit_warning 恒 False 打 ✅ | `data/optimization_reports/weekly_optimization_20261002_230523.json` |

**用户已裁 (2026-10-03)**: P0-1 停推送 ✅ 已落地 (`_TASK16_WEEKLY_OPTIMIZER_ENABLED=False`, Task#30 同款 kill switch 造型); P0-3 契约测试 ✅ 已落地; **P0-2 逐项修复不做** — 修完 ⑥⑦ 后当前样本下输出「无解」, 不如直接走 P2-6 重建。

## 3. 问题定义: 为什么「修」不如「收敛」

这条链要回答的真实问题是: **「playbook 退出参数 (be/dd/trailing/time_exit) 当前值是否最优, 调整候选是否过门」**。仓库里已经有三套为这个问题建的资产, 而 WeeklyOptimizer 一套都没用:

| 已有资产 | 是什么 | 与生产链共享代码? | 现状 |
|---|---|---|---|
| `scripts/exit_replay_backtest.py` | 官方离线回放引擎: 1min bar 反事实评估退出参数, **直接复用生产 SSOT** `app.playbook.exit_engine._compute_dynamic_effective_sl` / `_get_factor_exit_config`; 双序假设 (pess/opt) 报区间; 内建 `reason_match_pct < 0.80 ⇒ 变体裁决一律冻结` 门 | ✅ 同一函数 | 活跃; 9/27 勘误注明「参数时代混算」污染 (跨参数落地日窗口只能看方向不能定值) |
| `app/playbook/exit_shadow.py` + `data/shadow/exit_param_shadow.jsonl` | 实弹轮询影子: 盘中每轮巡检用与生产一致的纯逻辑回放更紧变体, close 时对比 capture; `EXIT_VARIANTS` 7 臂 (be/dd/profit_override/tiered) | ✅ 与 `_compute_dynamic_effective_sl` 同语义 | 活跃攒样 (533 行, 昨夜 10-02 仍有新行); **9/26 已裁决: 三门 (N≥30/均值改善/一致性≥55%) 下 BE/DD 族 5 变体全 FAIL, 无新变体可毕业** (shadow_registry ADJUDICATED 留痕) |
| `config/playbook_horizon.yaml` | 退出参数唯一真源 (7/7 起 Pipeline 退出引擎唯一配置源), 每次调参带数据依据 + 回滚线注释 | — (它就是生产值) | 活跃; 现行 default be=0.4 / dd=0.8 |
| `scripts/trailing_polling_sim.py` | 轮询级 trailing/breakeven 模拟器 (DEB-20260808 §19.5-1): 30s 粒度采样、bar 级 peak、冷却期、费垫按日期 | ⚠️ 对齐语义但独立实现 | 可用; `data/replay_bars/` 2334 文件覆盖至 10-02 |

**WeeklyOptimizer 的 `_replay_trade`**: 4 参数 (mss/sl/tp/trail) 近似回放, 逐笔用 `max_drawdown`/`pnl_pct` 聚合字段猜触发, **与上述任何一套零共享代码**。它的参数空间 (sl=-0.15~-0.40, tp=0.03~0.10) 甚至不在生产参数值域内 (生产 fixed_stop_loss_pct=2.0%, be=0.4%, dd=0.8% — 两套单位并存正是 P1-5 病灶)。

⇒ **它不是「官方引擎的粗化版」, 是「另一门语言」。** 修好它的列名/量纲/窗口, 得到的仍是一门与生产无关的语言的正确答案。

## 4. 候选方案 (R1 提案, 待 R2 交叉证伪)

### 方案 A: WeeklyOptimizer 退役退出参数职责, 评估面收敛到已有三资产 (做减法, 推荐)

- **机制**: Task#16 kill switch 永久化 (删 `_replay_trade`/`PARAM_GRID`/网格搜索/敏感性/安全区/bootstrap 全套 ~600 行); 「参数是否该调」的唯一入口 = ①实弹影子 (`exit_param_shadow` + `exit_shadow_graduation.py` 预注册三门) ②需要反事实全窗口时跑官方引擎 (`exit_replay_backtest.py`, 先看 reason_match≥0.80 冻结门 + era 分层)。周报展示面已由 `weekly_scorecard_carrier` 第三块承接 (exit_shadow_report: 现行真实参数 + 影子各臂效果对比 + M5 漂移诊断)。
- **防自欺**: 契约测试 3 条 xfail-strict 绊线随代码删除转为**墓碑测试** (断言 `weekly_optimizer` 不再被 scheduler 引用 + `LIVE_CURRENT_PARAMS` 不再存在); shadow_registry `exit_param_shadow` 条目已留痕 9/26 裁决, 判据冻结不可复用为新提案 (阈值购物禁令)。
- **接线**: 零新表/零新文件; 消费面 = 周六载体 (已落地) + 影子看板段7 (已有)。
- **可验证性**: `grep -rn 'weekly_optimizer' app/ --include='*.py'` 仅剩 import 残根清零; 全量回归等基对比新增失败 = 0。
- **代价**: 失去「每周自动出一份参数建议报告」的形态 — 但该形态 7 周输出的常量伪影已证明是负资产; 真实调参节奏 (7/9, 7/16, 7/19, 8/17) 全部由 DEB/影子数据驱动 + 用户裁决, 从未消费过 WeeklyOptimizer 报告 (`optimization_reports/` 全仓零消费者)。

### 方案 B: 保留 WeeklyOptimizer 壳, 内核换成官方引擎调用

- **机制**: `run_weekly_optimization` 改为编排 `exit_replay_backtest.replay_*` (生产 SSOT 数学), 参数空间换成 `playbook_horizon.yaml` 真值域 (be/dd/trailing_step/time_exit), live 基线运行时读 yaml。
- **防自欺**: reason_match<0.80 冻结门 + era 分层 (跨参数落地日窗口拒裁) 必须前置; 契约测试改为钉「live 基线 == yaml 运行时读值」。
- **接线**: 周报推送面可恢复 (kill switch 置 True 的前提达成)。
- **可验证性**: 同参数同窗口下 WeeklyOptimizer 输出 == 官方引擎输出 (逐字对账)。
- **代价**: 官方引擎单次全窗回放成本高 (逐笔拉 bar); era 混算污染使「每周自动跑」在参数落地日附近天然不可裁 ⇒ 自动化形态与判据纪律冲突, 大概率退化为「跑了但不能信」的僵尸尺子。**与 9/26 影子裁决 (全变体 FAIL) 叠加: 当前没有任何待裁参数问题在排队。**

### 方案 C: 用 `trailing_polling_sim.py` (轮询级) 重建周度对比

- **机制**: 30s 轮询粒度模拟 be/dd/trailing 变体, 每周出「现行 vs 候选」净差报告。
- **防自欺**: 与影子实弹数据交叉校准 (同一笔交易影子 capture vs 模拟 capture 偏差分布)。
- **代价**: `trailing_polling_sim` 是**独立实现** (对齐语义非共享代码) — 重蹈 `_replay_trade` 覆辙的结构性风险; 且与 `exit_param_shadow` (实弹轮询真值) 功能重叠, 实弹数据在攒的情况下模拟器的边际价值 = 只能测「更松」变体 (影子只测更紧, 因更松骑过实盘退出点路径不可观测) — 这是它唯一的差异化生态位, 但「更松」方向当前无提案在排队。

### 方案 D: 什么都不做 (kill switch 保持 False, 代码留存不删)

- **机制**: 现状冻结 — 推送已停, 报告文件继续每周五落盘 (无人消费), 代码 ~950 行留存。
- **防自欺**: 契约测试绊线在, 任何人恢复推送会先撞 XPASS 报警 + kill switch 测试红。
- **代价**: 违反「删代码消除根因 > 留着打补丁」纪律; 950 行死代码 + `LIVE_CURRENT_PARAMS` 虚构常量仍在仓里, 未来 agent 会再次把它当真源引用 (本次事故的同款入口); `live_backtest_comparison.py` 共享同一虚构基线, 留着 = 病灶留着。

### 比较与推荐

| 维度 | A (退役收敛) | B (换内核) | C (轮询级重建) | D (冻结) |
|---|---|---|---|---|
| 与生产链共享数学 | ✅ (收敛到 SSOT) | ✅ | ❌ 独立实现 | ❌ |
| 新代码量 | **负** (~-600 行) | 中 | 中 | 0 |
| 当前有无待裁参数问题 | — | **无** (9/26 全 FAIL) | **无** | — |
| 僵尸尺子风险 | 无 (删) | 高 (era 污染 ⇒ 自动跑不可裁) | 中 | 高 (留存) |
| 符合「做减法」纪律 | ✅✅ | ⚠️ | ❌ | ❌ |

**推荐 A**, 理由: ①用户纪律「删代码消除根因 > 加校验打补丁」「方案越复杂 = 方案有问题」; ②真实问题 (退出参数该不该调) 已有两套活资产在回答 (实弹影子攒样中 + 官方引擎按需跑), WeeklyOptimizer 是第三套且是坏的; ③B/C 都在给「当前无人排队的问题」重建自动化 — 8/14 用户方法论: 历史数据可回放时直接回放裁决, 不养前向自动机; ④D 留病灶。**A 的执行前提**: 契约测试转墓碑 + `live_backtest_comparison.py` 同批处置 (共享虚构基线, 不可单独留) + CONTROL-LOOP-LEDGER 补 Task#16 退役行 (当前未入账)。

## 5. 硬约束 (任何方案不得违反, 全部为已裁决纪律)

1. **era 分层**: 裁 be/dd 须 baseline 按入场日取当时参数值, 或只取现行参数时代样本 (9/27 勘误, 引擎头注 ⛔5)。
2. **reason_match_pct < 0.80 ⇒ 变体裁决一律冻结** (官方引擎头注 ⛔6, 与影子尺子同源)。
3. **OOS + 转正门槛**: 双窗口 ≥40% 才采信; 转正 OOS≥15 日 + 净均>0 + 毛利 t>1 + ADF 平稳 (既定纪律, 周六 09:05 周检)。
4. **预注册冻结 + 禁阈值购物**: 判据在验证窗口开启前冻结; 拿已结算案例反推阈值 = 禁止; `exit_param_shadow` 9/26 判据已裁 FAIL, 复用同判据重启 = 阈值购物。
5. **参数落地走既有通道**: 影子毕业 (`exit_shadow_graduation.py` 三门) 或 DEB 回放提案 + 用户裁决 (be_0.4 8/17 先例), 不存在「优化器自动落地」路径 (Task#16 注释自始写明「仅产出建议报告, 不自动落地」)。

## 6. R1/R2 辩论任务分派 (按 multi-model-debate skill v0.1.0)

- **反幻觉/数据保真**: 复算 §2 表全部数字 (尤其「7 周常量」「零消费者」「9/26 全 FAIL」); 攻击 A 的「无待裁问题」前提是否成立 (是否有影子/DEB 正在排队等参数评估)。
- **架构**: 攻击 A 的删除边界 (`live_backtest_comparison.py` 是否可单独留? `weekly_review` 编排是否引用?); B 的 era 污染与自动化冲突论证是否成立。
- **实证/可行性**: 实跑 `exit_replay_backtest.py` 现行窗口拿 reason_match 真值; 实跑 `exit_shadow_graduation.py` 确认 9/26 读数可复现; 给出 A 方案的删除清单与等基对比命令。
- **收敛问题**: 「只允许做一件事」= A 的删除批次, 还是先补 Task#16 台账行再删?

## 7. 已落地清单 (本草案提交时已生效, 非待裁项)

| 项 | 状态 | 载体 |
|---|---|---|
| P0-1 停推送 | ✅ | `app/core/scheduler_triggers.py` `_TASK16_WEEKLY_OPTIMIZER_ENABLED=False` + try 块首句 RuntimeError 门 (Task#30 同款造型, except logger.debug 优雅吞, 周五主流程零影响) |
| P0-3 契约测试 | ✅ | `tests/test_weekly_optimizer_contracts.py` (列名存在性 ×2 + 参数可识别性 ×1, 全部 xfail-strict 绊线; kill switch 状态守卫 ×2 常绿) |
| 周报承接展示 | ✅ | `ops/hermes/weekly_scorecard_carrier.py` 第三块 `exit_shadow_report.py` (现行真实参数 via M5 漂移诊断 + 影子 7 变体 vs 实盘 capture/净差, 净口径 fee=0.08%; 线上副本已同步同 sha256; 台账行已更新) |
| P2-6 重建 | ⏳ 本草案 | 待辩论收敛 + 用户裁决 |

**回滚**: P0-1 = kill switch 置 True (须先满足本草案恢复条件); 周报块 = JOBS 元组删一行; 契约测试 = 删文件 (不建议, 它是唯一钉住病灶的守卫)。

## 8. 执行记录 (2026-10-03, 用户裁「A」后当日落地)

| 步骤 | 动作 | 结果 |
|---|---|---|
| Guard-First 普查 | grep 全仓消费者: weekly_optimizer (app 侧仅 scheduler_triggers Task#16 + live_backtest_comparison import; 其余全是 docs/.qoder 文档引用) / live_backtest_comparison (仅 Task#16) / optimization_reports 产物 (零消费者) / `app/backtest/__init__.py` (无 re-export, 仅 walk_forward+statistical_gate) | ✅ 删除闭包 = 2 模块 + Task#16 接线 + 契约测试转墓碑, 无第三方消费者 |
| 删除 | `git rm app/backtest/weekly_optimizer.py app/backtest/live_backtest_comparison.py` (957+394=1351 行) | ✅ |
| 接线摘除 | scheduler_triggers Task#16 try 块 → 退役注释墓碑 (P8/ORB 退役同款造型); kill switch flag 一并删除 (模块已不存在, 留 flag = 装饰性配置) | ✅ |
| 契约测试转墓碑 | v1 3 条 xfail 绊线 (对象已删) → v2 墓碑 7 例: 模块文件不存在 ×2 + import 层面钉死 + scheduler 无活引用 + 全仓无活引用 + 生产 DDL 列名锚 + LIVE_CURRENT_PARAMS/死参数键不得再生。**反证已做**: 从 HEAD 恢复 weekly_optimizer.py → 4 例红; 再删 → 7 例绿 ⇒ 墓碑真咬人 | ✅ |
| 台账补账 | CONTROL-LOOP-LEDGER §2.1 增 P12 退役行 (Task#16 此前**从未入过台账**, 本行即补账) | ✅ |
| 等基对比 | 裸 worktree@HEAD 基线 vs 活仓改动后, 相关测试面: **新增失败 = 0**; 基线侧 3 条「已消除」逐条归因为环境性 (worktree 缺 data/+logs/: FileNotFoundError ×2 + sqlite unable to open ×1; 活仓同套件 9 passed) | ✅ |
| 守卫套件 | test_weekly_optimizer_contracts + dead_db_literal_gate + ops_scripts_sync + control_loop_ledger + replay_engine_import_smoke = 32 passed | ✅ |

**执行中发现的同族残留 (不在本批范围, 留档待独立评估)**: `app/risk/position_exit_monitor.py:537` 存在 `dyn_cfg.get("stop_loss", -0.045)` 运行时 fallback 默认值 — 与 LIVE_CURRENT_PARAMS 虚构基线同族的「代码默认值」坏习惯 (恰为同一数值 -0.045), 但它在风控退出路径且是 fallback 而非对比基线, 未按退役批连带修改 (外科手术纪律); 墓碑测试已注明该排除项及原因。

**P2-6 原命题的落点**: 用户原始表述是「用轮询级模拟器重建」; 执行时按方案 A 收敛 — 重建载体不是新模拟器, 而是**既有的**官方引擎 (`exit_replay_backtest.py`, 复用生产 SSOT `_compute_dynamic_effective_sl`, 与轮询级模拟器 `trailing_polling_sim.py` 同族语义) + 实弹影子 (`exit_param_shadow`, 攒样中) + yaml 真源。当前无待裁参数问题排队 (9/26 影子三门全 FAIL 留痕), 故不新建任何自动机; 下次需要评估退出参数时按 §5 硬约束直接跑这两套。
