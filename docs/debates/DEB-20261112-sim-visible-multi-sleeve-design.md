---
title: 富途 SIM 可见的多策略 sleeve 设计：保留 DA，独立编号/预算/退出
status: implemented_pending_anchor
module: cross
related_okr: OKR-2026-11
related_deb: DEB-20261112-beyond-da-strategy-research, DEB-20261112-umar-review-external-strategies, DEB-20261111-win-small-loss-large-diagnosis
tags: [playbook, futu-sim, multi-sleeve, design, shadow-first]
updated: 2026-11-12
revision: v3 + R5–R13 共识/实施/独立复核（DA 主链内 MOM-ETF-V1 已接线；入口已开启，锚点日待验证）
---

# DEB-20261112 · 富途 SIM 可见的多策略 sleeve 设计

> 状态：**已实施 / 入口已开启 / 锚点日 2026-11-13 ET 待验证；R13 独立复核修复 P0 后重新启用**
> 用户问题：当前已是模拟交易，是否有能直接在富途 App 模拟盘中可见、类似 DA 链路的交易逻辑？统计按 sleeve 编号分开，不混淆。
> 结论（v2 收窄）：**“可见”可以几乎零代码做到；“像 DA 一样跑成一条独立策略线”不行**。
> 新策略走 `Playbook → DualBrokerAdapter → Futu TrdEnv.SIMULATE` 就会出现在富途 App 模拟盘（这一点 v1 正确，代码实证同前）。
> 但 v1 低估了三处：①**退出/隔夜是 DA 日内语义，不是 horizon 参数**（三处开关 + 至少四条全局规则会当日/当小时砍掉 swing 仓）；②**费用地板 + 整数股取整**让“月频低换手”并不自动便宜——$2,500 目标在 QQQ 只能买 3 股，往返 **20bps**（DA 现役 9.7bps）；③**治理面**：OKR-2026-11 明确“至多一个新入场假设”，sleeve 若同时上 MOM/REV/ETF 三个入场源属直接冲突。
> 详见 §0 修订摘要与 §1.1/§4.6/§4.7。

## 0. 修订摘要（v2 勘误 + v3 收敛）

> 阅读顺序：**§0.3（只复用、不另造链路）→ §13（R3 独立复核）→ §14（R4 复跑与修改清单）**。正文各节已按 §14 修正，历史轮次保留可追溯。

### 0.1 勘误：v1 的六条说法被代码/配置实证推翻或收窄

| # | v1 说法 | 代码实证 | 对结论的影响 |
|---|---|---|---|
| E1 | “`exit_engine` 已按 horizon 读规则；新增 `swing` horizon/因子规则即可，不改 short/long” | `app/playbook/exit_engine.py:406` `_skip_allowed = meta.get("_skip_allowed", _cohort=="B" or _horizon=="long")`；`:409` 被 `overnight_master_switch_on()` 一票否决；`config/unified_config.yaml:1594` **`overnight_hold_enabled: false`**；`app/playbook/pipeline_parts/overnight_cleanup.py:157-190` EOD 兜底只认 cohort B | **新 horizon 会被当 short 处理**：盈利仓 `PRE_CLOSE_EXIT` 当日平、EOD 兜底强平。21 日持有**物理上不成立**，除非改代码（§1.1、§4.5） |
| E2 | “ETF 数据缺口：QQQ/XLE/XLRE/XLY 有 5y；SPY/IWM/XLK/SMH 需补” | `data/replay_bars_daily/` 实测：QQQ 1347 根（2021-07-01→2026-11-10，6 列）/ XLE 1330 / XLRE 1326 / XLY 1330（5 列）；**SPY/IWM 各只有 216 根（2026-01-02 起）**，够 MA200 但不够 252d；XLK/SMH 缺失（R3-08） | “文件存在 ≠ 可回测”；且新增票是引导问题（`scripts/refresh_daily_bars.py:162` 只增量刷新已存在的 `US_*.json`） |
| E3 | “ETF 点差/流动性更稳定，费用地板问题较轻” | 佣金地板 `max(min($0.01×股数, 0.3%×额), $1.99)/腿`（`app/execution/fee_model.py:96-97`，`config/unified_config.yaml:20-22`）；**整数股取整后** QQQ $2,500 目标只能买 3 股（$2,024）⇒ 往返 **20bps**（R3-09 实测） | 低换手不等于低费用——费用由**每腿固定 $1.99** 与**整数股取整**共同决定分母（§4.6） |
| E4 | “统计隔离：DA KR1 仍只算 `decision_id LIKE 'DA-%'`” | KR 脚本确实按前缀（`scripts/da_expectancy_report.py:24-27` 只有 DA/EXT 两桶）；但**日报/周报/记分卡的漏斗把未知前缀默认归 `DA自选`**：`app/review/source_attribution.py:7-16` 的 `CASE ... ELSE 'DA自选'`，消费方 `app/review/daily_review.py:1218`、`app/review/weekly_review.py:187`、`scripts/direction_hit_scorecard.py:56` | KR 不污染 ✓，但**DA 采纳率/漏斗口径会污染**。必须同步加 CASE 腿，否则“按 sleeve 分开”在报表层是假的（§1.2、§3.2） |
| E5 | “必须测试 DA 哈希/行为不变” | `app/playbook/config.py:165-232` 把**整本 `playbook_horizon.yaml`**（去注释键）纳入 `exit_config_version` | 往该文件加 `swing_horizon`/`factor_exit_rules.sleeve_mom` **必然改哈希**（设计使然），不是 bug。要么接受 cohort 断点并落档，要么把 sleeve 参数放哈希外（§1.3） |
| E6 | 草案 YAML 的键名可用 | `instruction_generator` 读的是 `buy_fixed_stop_loss_pct` / `sell_fixed_stop_loss_pct`（`app/playbook/instruction_generator.py:528-531`）与 `vol_buckets`（`:246`）；`skip_pre_close_if_profit` **在 exit_engine 无消费方**（只有 `horizon_selector.py:35-42` 的默认字典） | v1 的 `fixed_stop_loss_pct: 3.0` 会**静默落 fallback**；`skip_pre_close_if_profit` 是死键，真正闸门是 `_skip_allowed`（§4.5） |

### 0.2 增量：v1 未覆盖、但决定可行性的七节

- **§1.1 隔夜三闸**：`_skip_allowed` / 总开关 / EOD 兜底 —— swing 仓当日被平的真正原因。
- **§1.2 归因污染面**：`ELSE 'DA自选'` 的精确清单与最小修法。
- **§1.3 配置哈希与 cohort 断点**：改一页 yaml 会重打所有新成交的 `exit_config_version`。
- **§1.4 指令覆盖与优先级**：`save_instruction` 的优先级阶梯（DA=3 / 其他=1）+ `_src_priority`（UE=2 / DA=1 / 其他=0）。
- **§4.6 费用算术**：每腿名义额 ↔ 往返 bps ↔ 年化拖累；与 `max_daily_notional_usd` 的三处互锁。
- **§4.7 功效与决策日**：月频 sleeve 的 MDE 量级 → 本月/本年**不可能**得出统计结论，只能做“管道 demo”。
- **§7.0 治理门**：OKR-2026-11 §1.4/§7 的“一个入场假设”、KR4“≤5 个实验 + 预注册效应量/n/MDE/决策日”、影子注册表登记义务。

### 0.3 方向调整（R4，2026-11-12）：只复用 DA/Playbook，不另造交易链路

> 用户追加约束：**不另造交易链路，复用 DA/Playbook 现有管线。**
> 本节是全文的收敛口径；与 §13（R3 独立复核）同向，并把“复用”落成可验收清单。

**一句话**：sleeve 不是新系统，是 **Playbook 的又一个写入方 + 一个策略语义分派点**。仓库现在已有 5 类写入方（DA 主链 / UE 历史腿 / topup / fresh-event / **EXT 扩展时段臂**，见 §2.2），多 sleeve 不改变架构；改变的是“同一条管线里，谁用哪套语义”。

#### 0.3.1 复用矩阵（必须复用 / 允许新增 / 禁止新造）

| 层 | 复用什么（不改） | 允许的最小新增 | 禁止新造 |
|---|---|---|---|
| 信号 | — | 一个写入方（脚本/函数）产出 `playbook_instructions` | 新 DB / 新表 / 新信号框架 |
| 事前准入 | `save_instruction`（admission 预判 + 覆盖仲裁 + 幂等，`db.py:563-584,652`） | `decision_id` 前缀 | 绕过 `save_instruction` 直写（EXT 旧做法，**不复制**） |
| 入场 | `run_playbook_auto_execute`（预算/日帽/持仓帽/冷却/同票互斥/chase_cap/entry_confirm/TCA） | `trigger_mode` 分派（缺省走原函数） | 新入场循环 / 新调度器 / 新 broker |
| 执行 | `execute_instruction → DualBrokerAdapter → TrdEnv.SIMULATE` | `metadata` 标记 | 新 `source_type` / 新下单表 / 直连 broker |
| 出场 | `monitor_playbook_sim_exits → exit_engine` 同一监控 | `exit_profile` 分派 + EOD 兜底同款分派 | 新退出监控 / 新止损系统 / 新 STOP 单体系 |
| 对账 | `position_sync`（remark 白名单 + 精确匹配） | 无 | 改 `remark` 格式 |
| 预算 | `load_budget_and_guardrails`（全局帽） | sleeve 子帽（读 `decision_id` 前缀） | 新资金账户 / 新预算器 |
| 统计 | `trade_records` + 现有报表脚本 | 归因分桶（**按 `decision_id` 前缀**，见下） | 新账本 / 新绩效系统 |
| 实验治理 | `shadow_registry` | 建项占位 | 旁路台账 |

#### 0.3.2 这条约束带来的四处收敛（相对 v2）

1. **S5 预算子帽不是链路必需**：全局帽已对 sleeve 生效（`daily_notional_cap_pct 0.8 × 基数 ≈ $65,600`、`max_open_positions`、`max_open_risk_pct 4.0`）。子帽只是可选的风险约束，不做也不影响复用。
2. **S4 归因腿不是链路必需**：`trade_records.decision_id` 已落库（EXT 的 `EXT-<date>-<symbol>-<session>` 是现成先例），成交统计按前缀分桶即可。`source_attribution.py` 的 `ELSE 'DA自选'` 只污染**指令漏斗**（日/周复盘的采纳率分母），不污染成交归因；补腿是报表口径决策，可后置（但要记在案，不能当“已隔离”）。
3. **metadata 透传不是链路必需**：R3-01/02 属实（指令 `metadata` 不进成交表，`playbook_service.py:2282-2315` 固定构造）。但成交 `metadata_json` 已复制 `conditions` 且有 `decision_id` 列 ⇒ 归因与过滤都能用既有字段，**不需要加透传补丁**（这也符合“不另造”）。
4. **代码 seam 收敛到 3 个，且都可后置**：S1 `trigger_mode`（要“定时入场”才需要）、S2 `exit_profile`、S3 EOD 兜底同款。若接受 DA 日内语义 ⇒ **一个 seam 都不用做**，sleeve 就是“另一个信号源”（= §0.3.3 读法 A 的零 seam 变体）。

#### 0.3.3 两种“复用”读法（必须先选定，再谈实现）

| 读法 | 含义 | 新增代码 | 能得到什么 | 得不到什么 |
|---|---|---|---|---|
| **A 管线复用 + 信号新写** | 复用入场/执行/出场/对账/预算，新增一个写入方 | 0（日内语义）～2（21 日持有：S2+S3） | 见得到、算得清的新策略线 | 若不批 S2/S3，仍受 DA 日内几何约束 |
| **B 连信号也复用 DA** | 只用 `decision_id`/预算/统计把 DA 拆账 | 0 | 纯分账、纯展示 | 不是新策略；不改期望，不解决“赢小输大” |

**结论**：用户要的“类似 DA 链路的交易逻辑可见、统计分开”，读法 A 即可满足；读法 B 只能验证链路，不能带来新 alpha。无论哪种，**都不需要新链路**——差别只在是否新增一个写入方（A）以及是否批准 2 个语义分派点（A 的 swing 变体）。

## 1. 现有链路核验（为什么“可见”不是问题）

| 环节 | 代码锚点 | 现状 | 对新 sleeve 的含义 |
|---|---|---|---|
| 指令执行 | `app/playbook/playbook_service.py::execute_instruction` | 生成 `Order`，`target_env=sim` 时 `source_type=PLAYBOOK_SIM`（`playbook_service.py:1639-1698`） | 新 sleeve 可复用同一执行函数 |
| 券商路由 | `app/execution/dual_broker.py::submit_order` | `TrdEnv.SIMULATE`，`ctx.place_order(..., remark=order.client_order_id)`（`dual_broker.py:306`） | 富途 App 模拟盘可见订单/持仓 |
| 订单备注 | `playbook_service.py:2014` | `client_order_id = f"pb_{instruction_id}_{target_env}_{attempt}"`，原样进 `remark` | App 端可见 pb 编号；sleeve 标签主靠内部 DB。**改 remark 有硬契约**（§6.2） |
| 自动执行 | `app/playbook/pipeline_parts/auto_execute_flow.py` | 取当日指令，复验 trigger/admission/budget 后执行 | 新 sleeve 需要 sleeve-aware trigger/policy，不能硬套 DA 突破触发 |
| 退出管理 | `app/playbook/exit_engine.py` | 按 `metadata.horizon` 读 `playbook_horizon.yaml`，**但同时**执行一组全局 DA 日内规则 | ⚠️ v1 此处过于乐观，见 §1.1/§4.5 |
| 来源白名单 | `app/pipeline/order_executor.py:3582` | 新仓仅 `PLAYBOOK_SIM/PLAYBOOK_LIVE` | 新 sleeve 仍用 `PLAYBOOK_SIM`，不绕管线；**不要新造 `SLEEVE_SIM` 之类 source_type** |
| 同票互斥 | `playbook_service.py:1770-1815` duplicate guard + `auto_execute_flow.py:245-260` broker `already_held` | `open_positions` 以 symbol 为主键，同票已持仓则跳过 | 多 sleeve 必须设计同票冲突规则，避免仓位孤儿 |
| 资金/日帽 | `app/playbook/pipeline_parts/execution_flow.py:22-26,78-90` | `max_deploy` 与 `today_opened_notional` 统计**全部** `PLAYBOOK_SIM/LIVE` | sleeve 与 DA **共享**购买力与日名义帽（§5.6） |

**关键判断（v2）**：富途 App 可见性由 `TrdEnv.SIMULATE` 决定，不由策略名决定。难的不是“能不能下单”，而是三件事：
1. **退出语义**：DA 的隔夜/EOD/BE/死信号是“日内前提”下的优化，不是 horizon 参数；
2. **费用语义**：每腿 $1.99 固定地板让“小仓位、低换手”照样贵；
3. **治理语义**：新入场源在 11 月是名额受限资源，不是工程自由度。

### 1.1 隔夜三闸：为什么 `swing` horizon 单独存在也无法持夜

| 闸 | 位置 | 现状 | 对 swing 的后果 |
|---|---|---|---|
| ① `_skip_allowed` | `exit_engine.py:404-412` | 只有 `cohort=="B"` 或 `horizon=="long"` 才允许盈利仓跳过 EOD/PRE_CLOSE | `swing` 落入 else ⇒ **每日收盘前 8 分钟盈利即平** |
| ② 隔夜总开关 | `config/unified_config.yaml:1594` `overnight_hold_enabled: false` + `exit_engine.py:409` | 总开关关闭 ⇒ 无条件 `_skip_allowed=False`（**一处收口，全局生效**） | 即使 ① 放行，仍被 ② 抹平 |
| ③ EOD 兜底 | `overnight_cleanup.py:157-190` | 只跳过 cohort B 盈利仓；其余 `EOD_FORCE_CLOSE`；水下仓走 W_WEEK 5 交易日豁免 | swing 仓每日被强平；W_WEEK 只对**水下**仓且**≤5 日**，与 21 日持有正交 |

另外，DA 的一组**全局**日内规则不认 horizon，会在一小时内终结 swing 仓（均在 `exit_engine.py`，配置在 `unified_config.yaml`）：

| 规则 | 触发 | 对 21 日持有的后果 |
|---|---|---|
| `early_tight_sl`（`:560-605`） | 入场 30 分钟内 MFE 未达 0.3% ⇒ SL 收紧到 1%（或 0.5×ATR） | 月频仓开盘半小时不动就变 1% 止损 |
| `dead_signal_exit`（`:1032-1074`） | 60 分钟 MFE < 0.2% ⇒ DEAD_SIGNAL | 同上 |
| `cooldown_sl_pct`（`:536-538`，现值 4.0） | 开盘 15 分钟内 SL 放宽到 4% | 3% 初始 SL 在首 15 分钟被放宽；**09:46 后入场即可绕开（配置杠杆，§2.3）** |
| `TAKE_PROFIT`（`:749`） | `take_profit > 0` 即生效 | 若生成器写 TP>0，会在排名退出前先 TP；**生成器置 `take_profit=0` 即关闭（配置杠杆，§2.3）** |
| `mfe_stall_exit`（`:1096`） | 150 分钟停滞 | **当前 `mfe_stall_exit_enabled: false`（不在现役，R3-07 复跑确认）**；将来打开才需并入 allowlist |
| `adaptive_trailing`（`:500-504`，`unified_config.yaml:1581` 现役 true） | 峰值回撤 0.4–1.2% 即锁 | 趋势仓被 1% 噪声震出 |
| **`profit_override`（`:223-250`）** | 全局键现值 **`profit_override_threshold_pct: 0.8` / `profit_override_drawdown_pct: 1.0`**（`unified_config.yaml:1643-1644`）；且 `:201` 在 `peak < breakeven_at_pct` 时提前返回 ⇒ **由因子块 `breakeven_at_pct` 决定何时开始锁** | 因子块把 BE 抬到 3% ⇒ 0–3% 区间不锁；BE=0.4（DA）则 +0.8% 后就锁（R3-06 修正 v2 的“+2% 必锁”） |
| `sl_tighten_beta`（`config.py::tighten_sl_by_beta`，`:196`） | 入场 SL 先过全局 beta 收紧 | 现值 `1.0` = no-op（R3-07 探针确认） |
| `patient_exit`（`pipeline.py:2755-2877`） | 只在 `DEAD_SIGNAL_EXIT` 触发后武装（`pipeline.py:2757-2779`） | 属 DEAD_SIGNAL 的**下游**：关了 `dead_signal_exit` 即自然不触发，无需单独分派 |

**结论**：如果 sleeve 接受 **DA 日内语义**，上表全部无需处理（这也正是“只复用、不另造”最省的一档）；只有当目标必须是多日/21 日持有时，才需要在**现有** `exit_engine` 入口加一次 `metadata.exit_profile` 分派（缺省 `da` 逐字不变）并给 EOD 兜底同款分派。这是一处分派点，不是新链路（§0.3 / §2.1），且可后置于 P0/P1。

> **[R5 修正]** 生产 monitor 会**先注入** `meta["_skip_allowed"] = cohort_b_skip_eligible(...)`（`exit_monitor_flow.py:520,525`，该函数只认 `cohort=="B"`，`exit_engine.py:306-316`），所以 `exit_engine.py:406` 里 `horizon=="long"` 那条缺省分支在监控路径上**永远走不到**；随后 `:409` 隔夜总闸（false）再清零。两点推论：①只加 `swing` horizon 不可能持夜；②cohort 是 `hash(symbol-date)%2` 随机分配（`trade_store.py:56-64`），总闸一旦打开会出现“约一半 sleeve 仓靠抽签持夜”的静默分裂 —— sleeve 必须显式覆盖 `_skip_allowed`，且报表不得把 cohort 当 sleeve 分层。

### 1.2 归因污染面（“按 sleeve 分开统计”的最小全集）

`app/review/source_attribution.py:7-16` 的 CASE 没有 `ELSE 'unknown'`，任何未知前缀都落 `DA自选`。若 sleeve 用 `MOM-*`，下列报表会把 sleeve 指令算进 DA 漏斗：

| 消费方 | 位置 | 后果 |
|---|---|---|
| 日复盘漏斗 | `app/review/daily_review.py:1218` | DA 采纳率/条数被 sleeve 稀释 |
| 周复盘漏斗 | `app/review/weekly_review.py:187` | 同上，跨周 |
| 方向命中记分卡 | `scripts/direction_hit_scorecard.py:56` | 方向命中率混源 |
| playbook 统计 | `app/playbook/db.py:1827-1831` | 该处 `ELSE 'OTHER'`，**不会**误归 DA（好消息） |
| 期望值报告 | `scripts/da_expectancy_report.py:24-27` | 仅 DA/EXT 两桶；sleeve 不进桶但会进“整体组合”读数 |

**修法（P0，小改）**：`PLAYBOOK_SOURCE_SQL` 增加 `WHEN decision_id LIKE 'MOM-%' THEN 'MOM'` 等腿 + `source_labels()` 同步 + 契约测试（含“旧前缀归属不变”的反向断言）。同时明确：`scripts/da_expectancy_report.py` 的 `SOURCE_PREDICATES` 加 sleeve 桶，OKR 的“整体组合”读数改为**含/不含 sleeve 并列**。

**边界（R4 补）**：以上污染只在**指令漏斗**（采纳率/条数）。**成交统计不用等这个修法**：`trade_records.decision_id` 已落库，按前缀分桶即可分开（`EXT-<date>-<symbol>-<session>` 是现成先例）。所以“统计按 sleeve 分开”可以先用查询实现，报表补腿后置。

### 1.3 配置哈希与 cohort 断点

`PlaybookConfig.get_exit_config_version()`（`app/playbook/config.py:165-232`）对**整本** `playbook_horizon.yaml` 做 SHA1（剔除 `_annotation` 类键），并把该短哈希写进每笔 `trade_records`。后果：

- 为 sleeve 新增 `swing_horizon` / `factor_exit_rules.sleeve_mom` ⇒ **所有新成交哈希变化**，DA 的“同一 config_version 窗口取样”护栏会重开窗口。
- 这不是“测试能否保持不变”的问题，而是**必须二选一并落档**：
  - **方案 A（推荐，简单）**：接受断点。在 DEB/OKR 里写明“2026-xx-xx 起 exit_config_version 变更，前后分段报告”，且断点当天不起其他退出参数改动（一次一变量）。
  - **方案 B（昂贵）**：sleeve 参数移到哈希外的新文件（需改 `get_horizon_params` 的加载面，并保证 DA 读取路径逐字不变；回归面更大）。
- 无论哪个方案，都要在 `tests/` 里钉一条“DA 配置读取结果 = 改动前常量快照”的契约，防止“哈希断了但 DA 行为也变了”的双重事故。

### 1.4 指令覆盖与优先级（多写入方共存的真实规则）

| 机制 | 位置 | 规则 | 对 sleeve 的风险 |
|---|---|---|---|
| 指令 UPSERT 优先级 | `app/playbook/db.py:563-584` | `DA-`/`universe_expansion*`/`intraday_reentry-` = 3；其他非空 `decision_id`（TFB-/SNAP-/DEC-…）= 1；空 = 0；**新行优先级 ≥ 现存行才允许覆盖** | `MOM-*`=1：DA=3 可以在同 symbol+同日**覆盖掉** sleeve 指令（ETF 与 DA 重叠低，但非零；个股 MOM 必踩） |
| 执行排序 | `auto_execute_flow.py:174-181` | UE=2 → DA=1 → 其他=0，再按 confidence 降序 | sleeve 天然排最后，**符合“DA 优先”**；不要随手改成 2/3 |
| 单轮水坝 | `auto_execute_flow.py:1240-1290`，`max_new_positions_per_cycle: 3` | 每轮最多 3 笔新开 | 调仓日 2 条腿占 2/3；同日 DA 单顺延 |
| 同票互斥 | `playbook_service.py:1770-1815`；`auto_execute_flow.py:245-260` | `open_positions` 按 symbol；broker `already_held` 方向感知 | sleeve 持仓会挡住同向 DA（若 DA 覆盖该 ETF）；反向不挡 |
| 幂等 | `db.py:652` | 同 `decision_id`（非空）原地更新 | 调仓重跑必须**复用同一 decision_id**，否则制造重复行 |

## 2. 目标架构

```text
[DA 生成器]        decision_id=DA-*      ─┐
[MOM 生成器]       decision_id=MOM-*     ─┤
[REV 生成器]       decision_id=REV-*     ─┼→ playbook_instructions
[ETF 生成器]       decision_id=ETF-*     ─┤      (sleeve tag / horizon / exit_profile / policy)
                                         ┘
        ↓ save_instruction → admission/fee_floor/price_floor（同一把尺）
        ↓ auto_execute_flow：按 sleeve policy 选择 trigger/entry/budget（DA 路径逐字不变）
        ↓ PlaybookService.execute_instruction（唯一执行收口）
        ↓ DualBrokerAdapter.submit_order(target_env=sim)
        ↓ Futu OpenD TrdEnv.SIMULATE → 富途 App 模拟盘可见
        ↓ open_positions / trade_records（decision_id + sleeve tag 归因）
        ↓ monitor_playbook_sim_exits：按 exit_profile 分派（da = 现役；swing = 新规则集）
```

原则（v1 五条，新增第 6 条）：

1. **Playbook 仍是唯一下单/持仓管理管线**；不新建平行 broker/exit 管线。
2. **DA 默认 policy 不变**；新 sleeve 通过显式 `sleeve_tag/decision_prefix` 路由到新 policy。
3. **统计隔离**：DA KR1 仍只算 `decision_id LIKE 'DA-%'`；新 sleeve 用独立前缀，不混入 DA（**且必须修掉 §1.2 的漏斗默认腿**）。
4. **四锁不动**：新 sleeve 只在 SIM 环境；LIVE 路径不因此打开。
5. **不新造 `source_type`**：继续用 `PLAYBOOK_SIM`，sleeve 只体现在 `decision_id` + `metadata`。新 source_type 会同时撞 `order_executor` 白名单、`position_sync` 对账、预算查询、OKR 口径四处。
6. **新增：执行层“语义分派点”必须显式且可回归**（`trigger_mode`、`exit_profile`、`sleeve` 三个键），禁止靠字符串巧合（如 horizon 名称）隐式分流。

### 2.1 需要新开的执行层接缝（v2 新增，逐条可验）

| # | 接缝 | 现状 | 最小改法 | DA 不受影响的证明 |
|---|---|---|---|---|
| S1 | 触发分派 | `auto_execute_flow.py:327` 无条件调 `_check_intraday_trigger`（内含 breakout/量能/VWAP/时间窗） | 读 `metadata.trigger_mode`；`intraday_breakout`（缺省）走原函数，`scheduled` 走极简版 | 缺省分支逐字走原函数；加“无 `trigger_mode` ⇒ 调用同一函数”的契约测试 |
| S2 | 退出档 | `exit_engine.py:404` 起 DA 日内规则；另有 `profit_override`（`:223-250`）与 `sl_tighten_beta`（`:196`）两处**不受因子块控制**的全局几何 | `metadata.exit_profile`（缺省 `da`）；`da` 分支零改动，`swing` 跳过 §1.1 表内**全部**规则（含 profit_override / beta 收紧） | 用固定价格路径做 golden：`exit_profile=da` 的输出必须与改动前逐字相同 |
| S3 | 隔夜闸 | `_skip_allowed` + 总开关 + EOD 兜底（§1.1） | 仅在 `exit_profile=swing` 时允许 `_skip_allowed=True`；**总开关语义保持不变**（不为 sleeve 打开全局 `overnight_hold_enabled`） | 总开关仍 `false`；DA 仓位无 `exit_profile` ⇒ 行为不变 |
| S4 | 归因 | `source_attribution.py` ELSE 兜底 | 加 sleeve CASE 腿 | 旧前缀断言不变 |
| S5 | 预算子帽 | 只有全局帽 | `sleeve` 维度查询（`decision_id` 前缀计数/名义额） | 全局帽代码不改，仅新增“开仓前子帽检查” |

> S1–S3 都是“在 DA 规则前加一个显式分派”，属于新增分支而非改旧分支；这比“给 DA 放宽参数”安全一个量级，但仍必须一次一变量、并有 golden 回归（§6.4）。
> **[R4 收敛]** 按 §0.3.2：**S4/S5 不是链路必需**（归因按 `decision_id` 查询即可、全局帽已生效），可后置或不做；只有 **S1–S3** 会动共享代码，且默认应后置到 P2、需用户裁决。

### 2.2 现有非 DA 来源的复用先例：EXT 扩展时段臂（R4 新增）

这是“不另造链路”在仓库里已经跑通的样板，也是 sleeve 命名/归因的现成范式：

| 事实 | 证据 | 对 sleeve 的含义 |
|---|---|---|
| EXT 有独立 `decision_id` 规范 | `EXT-<date>-<symbol>-<session>`（`scripts/extended_hours_entry_executor.py:3682`） | 直接沿用：`MOM-ETF-<date>-<symbol>`（R3-04） |
| EXT 成交仍进同一张表 | `trade_records`：`source_type=PLAYBOOK_SIM`、`trade_mode=simulate`、`mode=futu_simulate`（`extended_hours_entry_executor.py:3662-3690`） | 统计/对账/出场监控零改动即可接管 |
| ⚠️ EXT 的 `owner_strategy` 是自有值 | 同处写 `owner_strategy='extended_hours_entry'`（Playbook 路由的 sleeve 会得到 `playbook_sim`） | 归属不要混用：sleeve 用 `decision_id` 前缀，`owner_strategy` 保持路由默认，勿为此改值 |
| EXT 出场由 Playbook 监控统一执行 | 只依赖 `metadata_json.stop_loss` 与未平仓筛选（`exit_monitor_flow.py` 不按前缀过滤） | sleeve 不需要新退出监控 |
| EXT 用 `metadata.gate_mode` 做样本隔离 | `extended_hours_entry_executor.py:3581-3582`、`:3700` | 同法可用（但见 §6.3 的 A9 限制） |
| ⚠️ EXT **不写** `playbook_instructions` | `:3625` 注释明确“绕开 save_instruction 收口点” | **这是反面教材**：sleeve 必须走 `save_instruction`，否则 admission/覆盖仲裁/幂等全丢 |

**结论**：把 sleeve 做成“EXT 的同类写入方 + 走 `save_instruction`”，就是最符合“复用现有管线”的形态；EXT 只提供命名与字段范式，不复制它的直写旁路。

**写入方契约（从现有写入方照抄，不新造）**：

1. 只调 `db.save_instruction(inst_dict)`（最小字段 `date/symbol/direction`，其余按 `generation_flow.py:282`、`llm_universe_scan.py:4566` 的同类字段）——admission 预判、覆盖仲裁、幂等全部自动继承。
2. **写后必须回读确认**：`save_instruction` 返回 id **不代表入围**（覆盖仲裁拒绝时也返回现存行 id，`db.py:571` 文档串 + `:729` 实现）。现成工具：`app/playbook/ue_batch.py:89 confirm_saved()`（返回 saved/rejected/unknown，按 `decision_id/symbol/direction` 回读比对），`llm_universe_scan.py:4570` 即此用法——直接复用，不要另写确认逻辑。
3. **不要自己写冷却/同票互斥/预算**：`auto_execute_flow` 已有 broker `already_held`、`duplicate_position_guard`、同日硬止损冷却、预算帽。写入方重复实现 = 两把尺子。
4. **不要自己写 broker 调用**：一律交给 `execute_instruction`。

### 2.3 配置即用杠杆：能不改代码就别改（R4 新增）

下面这些 DA 语义可以用**现役配置**调成 sleeve 想要的形状，避免新增 seam（都要在预注册里写死，且改配置会动 `exit_config_version`，见 §1.3）：

| 目标 | 现役杠杆 | 效果 | 代价 |
|---|---|---|---|
| 不要固定止盈 | 指令 `take_profit=0`（`exit_engine.py:749`：TP>0 才生效） | TP 规则自然关闭 | 无（生成器置 0） |
| 抬高保本线/弱化锁利 | `factor_exit_rules.<sleeve 因子>.breakeven_at_pct`（因子级键） | BE 与 `profit_override` 都要等峰值过 BE 才动（`exit_engine.py:201` 提前返回） | 0–3% 区间无保护，须接受 |
| 避开开盘冷却放宽 | 入场时刻 **09:50**（`cooldown_sl_pct 4.0` 只在开盘 15 分钟内生效，`exit_engine.py:536-538`） | SL 不被放宽到 4% | 晚 20 分钟成交，动量成本略升 |
| 只走日线/不做日内 | ⚠️ 不能用 `long_horizon(max_hold_days: 3)` 当“零代码 3 日持有” | 参数存在，但**语义不成立**（R5-02）：monitor 注入 `_skip_allowed=cohort_b_skip_eligible()`（只认 cohort B，`exit_monitor_flow.py:520,525`），且 `exit_engine.py:409` 又被隔夜总闸清零 ⇒ 仍当日平仓 | 只有 S1/S2/S3 才能改持有期 |
| 不引入新 horizon | 不碰 `playbook_horizon.yaml` | `exit_config_version` 不变、DA cohort 不断（§1.3） | 只能用 short/long 两档几何 |

**仍必须改代码的三条**（配置救不了，见 §1.1）：`early_tight_sl`（30 分钟收紧到 1%）、`dead_signal_exit`（60 分钟 MFE<0.2% 出场）、隔夜三闸（`_skip_allowed` / 总开关 / EOD 兜底）。`mfe_stall_exit` 现值为 `false`（R3-07 复跑确认），不在现役，不必处理；将来打开则并入同一 allowlist。

## 3. 编号与归因契约

| 字段 | DA | MOM sleeve | REV sleeve | ETF sleeve |
|---|---|---|---|---|
| `decision_id` | `DA-*` | `MOM-*` | `REV-*` | `ETF-*` |
| `source_type` | `PLAYBOOK_SIM` | `PLAYBOOK_SIM` | `PLAYBOOK_SIM` | `PLAYBOOK_SIM` |
| `owner_strategy` | `playbook_sim`（路由不变） | `playbook_sim`（路由不变） | 同左 | 同左 |
| `metadata.sleeve` | `da` | `mom` | `rev` | `etf` |
| `metadata.trigger_mode` | 缺省（= breakout） | `scheduled` | `scheduled` | `scheduled` |
| `metadata.exit_profile` | 缺省（= `da`） | `swing` | `swing_short` | `swing` |
| `conditions_json` | DA 条件 | 含 `sleeve_mom` | 含 `sleeve_rev` | 含 `sleeve_etf` |
| `horizon` | short/long | swing | swing_short | swing |
| 报表桶 | `DA-%` | `MOM-%` | `REV-%` | `ETF-%` |

说明：

- `owner_strategy` 当前参与路由/持仓归属，短期不建议改值；sleeve 隔离优先用 `decision_id + metadata.sleeve`。若后续要把 `owner_strategy` 细化为 `playbook_sim_mom`，须先审计 open_positions/duplicate guard/报表消费方。
- **REV/`swing_short` 在本轮只是占位**：本文只对 MOM/ETF 给参数草案；REV 一旦提案须各自走 §7.0 治理门（尤其 G1 名额），不得借“sleeve 框架”顺带上线。
- **decision_id 必须稳定、可重放、且含 symbol**：用 `MOM-ETF-{date}-{symbol}`（对齐 EXT 的 `EXT-<date>-<symbol>-<session>`）。`decision_id` 索引非唯一，只带日期会跨票歧义（R3-04）；调仓重跑/重启重放复用同一 id，命中 `db.py:652` 的原地更新，避免重复行。
- **`metadata.sleeve` 只在指令表有效**：成交 `trade_records.metadata_json` 是固定构造（`playbook_service.py:2282-2315`），**不会复制指令 metadata**（R3-01/02）。因此验收与统计一律以 `trade_records.decision_id` 前缀为准；若确实需要成交侧 `sleeve` 字段，只能另加透传（违反“不另造”，不推荐）。
- 新增落库字段遵守两条军规：**基数自检**（`decision_id` 前缀/`trigger_mode`/`exit_profile` 各自 distinct 计数，常量退化即 WARNING）与**单位声明 + 契约测试**（`sleeve_risk_pct` 单位是“权益百分比”，不是“美元”）。

### 3.1 为什么“同票互斥”对 ETF 试点低风险、对个股 MOM 高风险

`config/unified_config.yaml:3033+` 的 `watchlist.etfs` 全部 `enabled: false`，备注“主线仅用作市场方向参考”，DA 主链不交易 ETF ⇒ ETF sleeve 与 DA 同票冲突≈0。
但个股 MOM 与 DA 天然同池：`db.py:563-584` 的覆盖阶梯（DA=3 > MOM=1）会让 **DA 指令把 sleeve 指令覆盖掉**（静默丢单，不是报错）；同时 `broker already_held` 会让先开的 sleeve 单挡住 DA 同向单。个股 MOM 必须先解决“谁先占位”的显式裁决，才谈试点。

### 3.2 报表侧待办（“分开统计”真正的工程量）

1. `app/review/source_attribution.py`：加 sleeve CASE 腿 + `source_labels()` + 契约测试。
2. `scripts/da_expectancy_report.py`：加 sleeve 桶；输出“DA / EXT / sleeve 合计 / 整体”四列，sleeve 不得并入 DA。
3. `app/review/daily_review.py` / `weekly_review.py`：确认漏斗展示不把 sleeve 计入 DA 分母。
4. `scripts/direction_hit_scorecard.py`：sleeve 单独一行或排除（按用户裁决）。
5. `scripts/stall_entry_shadow_tag.py:123-127`：该脚本是 `ELSE 'other'`，**不会**误归 DA（R3-14 修正 v2 的说法）；主污染面仍是 `source_attribution.py`。

## 4. 信号与执行设计（首个试点建议：ETF/MOM）

### 4.1 为什么首个试点优先 ETF

- DA 主要做个股日内；ETF sleeve 与 DA 同票冲突概率低。
- ETF 点差/流动性更稳定（**但费用地板不因此变便宜**，见 §4.6）。
- 避免个股隔夜跳空左尾与 DA 尾损叠加。
- 数据（R3-08 复跑实测，v2 的“只缺 XLK/SMH”说法低估了）：

| 标的 | 根数 | 起点 | 列宽 | 能否 12-1（252d+skip21） |
|---|---:|---|---:|---|
| QQQ | 1347 | 2021-07-01 | 6 | 能 |
| XLE | 1330 | 2021-07-27 | 5 | 能 |
| XLRE | 1326 | 2021-08-02 | 5 | 能（余量最小） |
| XLY | 1330 | 2021-07-27 | 5 | 能 |
| SPY | 216 | 2026-01-02 | 5 | **不能**（够 MA200，不够 252d） |
| IWM | 216 | 2026-01-02 | 5 | **不能** |
| XLK / SMH | 无 | — | — | 需引导 |

- `scripts/refresh_daily_bars.py:162` 只增量刷新已存在的 `US_*.json` ⇒ **新增票需要一次全量引导**；新增行只写 5 列（丢 volume，`:209`），与 QQQ 的 6 列混存 ⇒ 任何量能类判据先对齐列宽。ETF 无 1m 缓存。

### 4.2 MOM/ETF 候选逻辑（预注册草案）

- **频率**：每 21 个交易日调仓；信号用前一交易日收盘数据，禁止当日收盘前视。
- **票池**：PIT 候选池；首试点可用 ETF 白名单，后续扩展 liquid stocks。**票池必须在预注册时写死并发版本号**，中途加票 = 换实验。
- **相对动量**：12-1 momentum = `close[t-21] / close[t-252] - 1`，取 top N。
- **绝对动量**：QQQ `close > MA200` 或 12m 收益 > 0；否则空仓/现金。
- **方向**：首试点只多，不做空；避免借券/short pool 复杂化。
- **权重**：inverse ATR20 或等权，单票上限由 sleeve budget 钳制（注意与单名帽 6% 取小，§4.6）。
- **入场**：调仓日 **09:50 ET 之后**。两个理由（都是现役配置，不需要改代码）：①黑窗判据 `ET < entry_open_blackout_hhmm`（`trigger_flow.py:311-323`，现值 `0935`）——09:35 贴边界，任何上调都会**静默杀死** sleeve；②`cooldown_sl_pct 4.0` 在开盘 15 分钟内把 SL 放宽到 4%（`exit_engine.py:536-538`）——09:50 入场可绕开。限价/市价由 sleeve policy 决定，仍过 chase-cap/admission。
- **出场**：调仓日排名跌出/top N 替换；3×ATR 初始止损；`swing` max_hold 兜底；不套 DA 的 0.4% BE/日内早退（前提是 S2/S3 已落地，§2.1）。

### 4.3 sleeve policy（新增配置命名空间，默认关闭）

```yaml
playbook:
  sleeves:
    enabled: false
    mom_etf:
      decision_prefix: "MOM-ETF-"
      sleeve_tag: "mom_etf"
      universe_version: "etf_v1_20261112"   # 票池写死版本，禁中途加票
      universe: ["US.QQQ", "US.XLE", "US.XLRE", "US.XLY"]
      rebalance_days: 21
      lookback_days: 252
      skip_days: 21
      top_n: 2
      absolute_filter: { symbol: "US.QQQ", ma: 200 }
      trigger_mode: scheduled
      scheduled_et: "09:50"          # 09:35 贴黑窗边界；<09:45 仍在冷却窗（SL 放宽到 4%）
      entry_style: limit
      entry_confirm_profile: "global"   # 无 per-instruction 关闭缝，见 §4.4
      chase_cap_enforced: true
      exit_profile: swing
      horizon: swing
      max_open_positions: 2
      max_leg_notional_usd: 4920     # 与单名帽 6% 取小；整数股后实际 ~$4.7k（§4.6）
      max_daily_notional_usd: 9840   # 2 腿；v1 的 5000 与 sizing 互斥
      sleeve_risk_pct: 0.25          # 权益百分比；刻意不用 risk_pct_per_trade（撞 live_readiness 同名键）
```

纪律：这是**配置草案**，不是已生效配置。`enabled=false` 时零行为；DA 不读 sleeves 块。

### 4.4 触发器与 DA 闸门矩阵（v2 新增）

现状 `auto_execute_flow` 对所有指令调用 `_check_intraday_trigger`（当日高/低点突破 + 量能 + VWAP + 时间窗）。这不是“一个 if”，而是一串 DA 前提下的闸门。逐条定去留：

| 闸 | 位置 | scheduled 模式 | 理由 |
|---|---|---|---|
| 当日高/低点突破 | `trigger_flow.py` 主体 | **跳过** | 月频调仓无“突破”语义 |
| 量能比 | `trigger_flow.py:452-560` | **跳过**（或只做“可成交性”下限） | 调仓不依赖当日放量 |
| VWAP 偏离/斜率 | `trigger_flow.py:838-890` | **保留，但必须当入场过滤预注册**（R3-13 修正） | 现役是**方向一致性硬门**：LONG 须在 VWAP 上侧（`dev < -tol` 拦）、SHORT 在下侧；保留会改变 scheduled MOM 的入场人群，不是“仅执行质量” |
| 开盘黑窗 | `trigger_flow.py:311-323` + `unified_config.yaml:1311-1312` | **保留**，scheduled 取 09:50（09:35 贴边界，黑窗上调会静默杀 sleeve） | 边界脆弱 |
| 14:00 cutoff | `trigger_flow.py:286-303` | **保留** | 收盘前流动性风险 |
| RFC-141 入场确认门（120s） | `entry_confirm_gate.py:183-248`，配置 `unified_config.yaml:1324` | **只能走全局**：代码没有 per-instruction 关闭缝；且它是**在采前瞻实验**（20% holdout） | 若 sleeve 走 120s 门，会在 cohort 里混入非 DA 人群 ⇒ 要么按 sleeve 分桶披露，要么关门前先补开关 + 声明“sleeve 不入 RFC-141 队列”。v1 的 `entry_confirm_enabled: false` 是**当前不存在的开关** |
| 同票/在途/interlock（RISK/BLACKOUT/cooldown） | `auto_execute_flow.py` 各段 | **保留** | 安全闸 |

**结论**：`trigger_mode=scheduled` 不是“绕过触发”，而是“换一个触发判据并显式列出保留的闸门”。凡是保留的闸门都要在 `metadata` 里留 `trigger_miss_reason`，否则“没下单”无法区分是策略还是数据问题。

### 4.5 退出设计（v2 重写：`exit_profile` 而不是“新 horizon”）

`horizon` 决定“参数表取哪一页”；`exit_profile` 决定“哪些规则跑”。v1 只做了前者，缺后者。建议：

```yaml
swing_horizon:
  label: "SWING_HORIZON"
  rr_ratio: 3.0
  fixed_stop_loss_pct: 3.0
  buy_fixed_stop_loss_pct: 3.0        # instruction_generator 实际读这两个键
  sell_fixed_stop_loss_pct: 3.0
  max_hold_days: 42
  overnight_gap_stop_pct: 5.0
  time_exit_hours: 999
  vol_buckets:                        # 不给就回落到 DA 的全局桶 → 止损宽度被 DA 化
    low:    { atr_pct_max: 0.015, sl_multiplier: 2.0, sl_floor_pct: 2.0, sl_ceil_pct: 5.0, tp_multiplier: 3.0 }
    medium: { atr_pct_max: 0.035, sl_multiplier: 2.5, sl_floor_pct: 3.0, sl_ceil_pct: 6.0, tp_multiplier: 2.5 }
    high:   { atr_pct_max: 999.0, sl_multiplier: 2.0, sl_floor_pct: 4.0, sl_ceil_pct: 8.0, tp_multiplier: 2.0 }

factor_exit_rules:
  sleeve_mom:
    trailing_enabled: false           # 让 21 日趋势自己走；trailing 用 horizon 外的新规则另立
    breakeven_at_pct: 3.0             # 0.4 → 3.0（DA 的 0.4 是日内语义）
    drawdown_from_peak_pct: 99
    time_exit_hours: 999
    max_hold_days: 42
```

边界（逐条对应 §1.1；R3-05/06/07 修正）：

- **sleeve 用显式 allowlist，不用“排除清单”**：只允许 ①初始 SL ②自身 gap stop ③排名/调仓退出 ④max_hold。凡不在 allowlist 的 DA 规则一律不跑。这比“逐个排除”安全：将来新增 DA 规则不会自动漏进 sleeve。
- **改因子块不够，但比 v2 想象的好**：`profit_override` 全局现值是 `0.8 / 1.0`（`unified_config.yaml:1643-1644`），且 `_compute_dynamic_effective_sl` 在 `peak < breakeven_at_pct` 时提前返回（`:201`）。因子块把 BE 抬到 3% ⇒ 0–3% 不锁；BE=0.4（DA 现值）则 +0.8% 后开始锁。**v2 的“+2% 必锁 / 1.5-0.5”是按代码默认值写的，不是现役配置（R3-06）。**
- **`mfe_stall_exit` 当前 `false`**（`unified_config.yaml:1446`），不在现役；`sl_tighten_beta: 1.0` 为 no-op（R3-07）。将来打开时必须并入 allowlist 判定。
- **不改** `short_horizon/long_horizon`；不改 DA 的 BE/trail/early-tight。
- **必须改** `exit_engine` 的一处分派：`exit_profile=swing` 时**白名单外全不跑**（含 `early_tight_sl` / `dead_signal_exit` / `adaptive_trailing` / `profit_override` / `cooldown_sl` / W_WEEK / reentry 豁免），并允许 `_skip_allowed=True`（**但不由总开关开**，总开关继续 `false`）。`patient_exit` 随 `dead_signal_exit` 一并失效，无需单独处理。
- **TP 用配置关**：`rr_ratio>=100` 死哨兵已退役（`instruction_generator.py:544-550`，TP 恒由 `sl_distance × rr` 算出）⇒ 生成器算完后**在写入方把 `take_profit` 置 0**（`exit_engine.py:749` 只在 TP>0 时生效）。这是写入方一行，不改共享代码。
- **必须改** `overnight_cleanup.py` 的 EOD 兜底同款分派（否则盘后兜底仍强平）。
- 宽止损/长持有是**独立策略几何**，不是给 DA 放宽 SL（C11 禁区）。
- 回归要求：`exit_profile=da`（含缺省）在固定价格路径上的出场原因/价格必须与改动前**逐字一致**（golden 测试），否则“改退出”与“改 sleeve”混在一起无法归因。

### 4.6 费用算术：低换手不等于低成本（R3-09 按整数股重算）

佣金地板：`max(min($0.01×股数, 0.3%×名义), $1.99)/腿`（`fee_model.py:96-97`），另有 `settlement $0.003/股`、卖出侧 SEC/TAF。ETF 单价高 ⇒ **整数股取整**后名义额显著低于目标值，bps 被抬高一档；v2 的“$2500≈16bps”是按连续股数算的，不成立。

QQQ（last close $674.76）实测（`estimate_round_trip`，往返）：

| 目标名义额 | 实际股数 | 实际名义额 | 往返费 | 往返 bps | 组合年化拖累（12 × 往返 bps） |
|---:|---:|---:|---:|---:|---:|
| $2,000 | 2 | $1,350 | $4.03 | **29.9bps** | ≈359bps |
| $2,500（v1 草案隐含） | 3 | $2,024 | $4.05 | **20.0bps** | ≈240bps |
| $3,449 | 5 | $3,374 | $4.09 | 12.1bps | ≈145bps |
| $4,920（单名帽 6%） | 7 | $4,723 | $4.13 | 8.74bps | ≈105bps |
| $5,400（8 股） | 8 | $5,398.08 | $4.1492 | 7.686bps | ≈92bps |
| $10,000 | 14 | $9,447 | $4.27 | 4.52bps | ≈54bps |

低价 ETF 取整粒度更细，同等目标名义额 bps 更好（$4,920 档：XLE 79 股 $4,915 → 9.30bps；XLRE 110 股 $4,904 → 9.71bps；XLY 40 股 $4,842 → 8.94bps）。
年化拖累算法：每 21 日调仓换掉 2 个槽位（每槽卖+买），每槽每年转 12 次 ⇒ 组合拖累 ≈ `12 × 往返 bps`（与两个槽位的资金基数相消）。

三处互锁（R3-10 修正：全局日帽不是某个 `max_daily_notional_usd` 键，而是 `daily_notional_cap_pct 0.8 × 基数 ≈ $65,600`）：

1. `playbook.admission.max_fee_bps: 11.54` + `fee_floor_sizing: true` ⇒ 系统会把股数抬到“费用可行下限”；按整数股，QQQ 最小可行是 **6 股 ≈ $4,049（10.15bps）**，不是 $3,449。这会**顶穿** v1 的 `max_daily_notional_usd: 5000`（2 腿 ≈ $8,100）。
2. 按草案的 `sleeve_risk_pct: 0.25` 与基数 $82k 反推：单笔风险 $205，3% 止损 ⇒ 名义 ≈ $6,833；再被单名帽（`max_position_pct: 0.06`，`unified_config.yaml:1706`）≈ $4,920 截断 ⇒ **每腿 ≈ $4.7k（7 股），2 腿 ≈ $9,446**。v1 的 `5000` 日帽与自身 sizing 数学自相矛盾（实际还会叠加风险乘数/risk_off 缩放，须实算确认）。
3. `admission.min_notional_usd: 2500` 是**下限**，不是目标；sleeve 的 `max_leg_notional_usd` 必须 ≥ 费用可行下限，否则永远在“被抬仓”和“被日帽截断”之间打架。

**结论**：若要 12 次/年调仓且年化拖累 ≤1%，每腿实测需 ≈ **8 股 QQQ / $5.4k**（7.686bps）；$4.7k 档（8.74bps）≈1.05%/yr，已在下限附近。低于 $2.5k/腿时费用率 20bps+，必须先证明毛利显著高于 DA 的 +7.8bps 才有意义。这一条在 P1 前必须定稿，因为它同时决定子帽、单名帽与 `top_n`。**不得为摊费抬票面**（OKR-11 §7）。

### 4.7 功效与决策日：月频 sleeve 不可能在本季读出结论

预注册必须写“所需 n / MDE / 决策日”（OKR KR4）。粗算（假设每笔 21 日持仓的净收益标准差 σ≈6%，MDE80 = 2.802 × σ/√n）：

| 累计 n | MDE80（每笔） | 能分辨的最小边际 |
|---:|---:|---|
| 24（≈1 年） | ≈343bps | 只有 >3.4%/笔 的效应才可分辨 |
| 72（≈3 年） | ≈198bps | >2%/笔 |
| 120（≈5 年） | ≈153bps | >1.5%/笔 |

对照：sibling DEB（`DEB-20261112-beyond-da-strategy-research.md` §4.3）的 H2 long-only 在**非 PIT 110 股池**上净 +357bps/21d；ETF 四票版本**没有任何本地证据**。也就是说：

- **1 年内不可能**把 sleeve 的正负与噪声分开；任何“sleeve 赚钱/亏钱”的叙事都必须标 `功效不足`。
- **口径按调仓簇，不按笔（R3-12）**：2 腿/调仓 ≈ 12 簇/年；两腿高度相关（同为权益 beta），若 ρ=0.5 则 MDE80 ≈ 420bps、ρ=1 ≈ 485bps。预注册必须报 rebalance-day clusters + 相关性敏感性，不能只报 trade n（宪章第 6 律）。
- 因此 P2 的产品定位应是 **“管道 demo + 归因台账”**，而不是“策略验证”；预注册的结论类型应为 `PLUMBING_OK / PLUMBING_BROKEN`，**不设 PASS/FAIL 的收益判据**。
- 若要做收益验证，应先按 sibling DEB 的 P0（PIT 票池 + 真实成本）走研究沙盒，再谈生产影子。

## 5. 预算、冲突与风控

1. **全局帽不变**：daily notional / max daily positions / portfolio heat / circuit breaker 仍是全局上限。
2. **sleeve 子帽**：每个 sleeve 有 `max_open_positions/max_leg_notional/max_daily_notional/sleeve_risk_pct`，子帽 ≤ 全局帽（且 §4.6 的算术必须闭合）。
3. **同票互斥**：`open_positions` symbol 主键不变；同票已有持仓/在途出场时，新 sleeve 跳过。首试点 ETF 与 DA 个股重叠低；个股 MOM 后续须显式优先级（建议 DA 日内优先，MOM 只在无 DA 持仓/当日无 DA 指令时开），并注意 §1.4 的覆盖阶梯会**静默吞掉**被覆盖的 sleeve 指令。
4. **不新建平行熔断**：sleeve 风控接入现有分层地图（`docs/circuit-breaker-map.md`）；不得自写一套独立熔断。
5. **费用同尺**：admission/fee_model 同源；新 sleeve 不享受费用豁免（LIVE 工程探针豁免不适用于策略 sleeve）。
6. **共享日帽（v2 新增，R3-10 修正）**：`today_opened_notional` / `open_positions_count` 统计全部 `PLAYBOOK_SIM/LIVE`（`execution_flow.py:78-90`）。全局日帽是 **`daily_notional_cap_pct 0.8 × 基数 ≈ $65,600`**（不是某个 `max_daily_notional_usd` 键；sleeve 的 `max_daily_notional_usd` 是新增子帽）。因此：
   - 调仓日 sleeve 会**占用 DA 当天的日名义额度与持仓名额**（`max_daily_positions: 16`、`max_open_positions: 999`）；
   - 组合热帽 `max_open_risk_pct: 4.0`（`unified_config.yaml:1860`）把 sleeve 的止损风险也计入，sleeve 满仓时 DA 可用热预算下降；
   - 这两条不是缺陷，但必须在 P2 前写成“调仓日 DA 容量预期下降”的明文，避免事后归因为“DA 变差”。
7. **ETF 观察名单不是免费的（v2 新增）**：`watchlist.etfs[].enabled` 目前全 false；把它们打开会进 `polling_controller`（`scheduler_core.py:815-825`，非基准 ETF 提到 `HIGH_VOLATILITY` 档）与 K_1M 订阅槽位（`scheduler_core.py:1600+` 注释：30 只×2 槽位=60 为安全线）。sleeve 只在下单时用报价，**不要**为了 sleeve 去翻 `watchlist.etfs.enabled`，除非先核订阅槽与轮询预算。

## 6. 富途 App 可见性与验收

### 6.1 可见性

- 订单/持仓出现在富途 App 模拟盘，因为 `target_env=sim → TrdEnv.SIMULATE`。
- App 端 remark 当前为 `pb_{instruction_id}_sim_{attempt}`（`playbook_service.py:2014`）；内部 DB 用 `decision_id=MOM-*` 区分 sleeve。

### 6.2 若要求 App 端直接显示 sleeve 字样（v2 精确化）

`remark` 不是展示字段，是**恢复/对账凭证**，四处硬契约：

| 消费方 | 位置 | 契约 |
|---|---|---|
| 系统单白名单 | `app/core/position_sync.py:269` | 前缀必须 `pb_` 起（否则该单被归 MANUAL，**可能被当作手动仓处理**） |
| 恢复路径精确匹配 | `app/core/position_sync.py:759` | 无 `broker_order_id` 时按 `f'pb_{iid}_{env}_{attempt}'` **等值**匹配；改格式必须两侧同改 |
| 外部单分类影子 | `app/execution/broker_capacity_shadow.py:207` | 以 `remark.startswith("pb_")` 区分外部/系统 |
| LIVE 保护单守卫 | `app/playbook/pipeline_parts/live_stop_guard.py:109` | 期望 `pb_stop_{symbol}_real` |

**建议**：首试点不改 remark（用内部编号归因）；若必须改，采用**保留前缀 + 尾部追加**的形式（如 `pb_{iid}_{env}_{attempt}_m`），并一次性改完上述四处 + 加回归测试。这不是“首试点必要条件”。

### 6.3 零代码可见性 demo（v2 新增，满足用户原始意图的最小动作）

用户原始问题是“**能不能在富途 App 里看见一条非 DA 的交易逻辑**”。这件事**不需要 sleeve 基础设施**：

1. 手工构造**一条** `MOM-ETF-<date>-<symbol>-TEST` 指令（`target_env="sim"`；`playbook_instructions` **没有 `source_type` 列**，R3-03），走 `execute_instruction(target_env="sim")`；
2. 富途 App 模拟盘立刻可见订单/成交/持仓；
3. 当日收盘由现役 DA 规则平掉（`PRE_CLOSE_EXIT`）——对“看见”这个目的完全够用，且**不新增任何代码/配置**；
4. 若用户要看见“持续持有”，才需要 §2.1 的 S2/S3。
5. **污染隔离（R3-01 修正：v2 此条原先写错）**：指令 `metadata.exclusion_reason` **不会**进成交表（`playbook_service.py:2282-2315` 固定构造，全文件无该键），所以 A9 不会自动剔除 demo 成交。二选一：
   - **（推荐，零代码）** demo 用 `MOM-ETF-<date>-<symbol>-TEST` 命名 + 收盘后一次性 SQL 给该行写 `metadata_json.exclusion_reason`（EXT 首跑批的现成做法），并**回读核验**；统计侧按 `decision_id` 前缀与 `exclusion_reason` 双重排除。
   - （要自动化才做）在成交 metadata 构造处加“指令 metadata 白名单透传”，属共享代码改动，**违反“不另造”优先级最低**。
   同时按既有约定 `TEST` 标记不落影子 jsonl（`exit_engine.py:517`）。**不要把 demo 行留在任何表现类读数里。**

这条路径把“可见性验证”和“策略基础设施”解耦：先交付可见性（当天可做），再按 §4/§7 的纪律做 sleeve（多周工程 + 治理门）。

### 6.4 验收清单（SIM 试点）

1. 生成 1 条 `MOM-ETF-TEST` 指令，走 Playbook 执行，富途 App 模拟盘可见订单/成交/持仓。
2. `trade_records.decision_id LIKE 'MOM-%'`（成交侧归因键，R3-01/02）；DA 报表 n/净 bps 不变；**日报/周报的 DA 漏斗分母不变**（验证 §1.2 的修法；未补腿前漏斗仍会误归，必须显式披露）。
3. 同票互斥生效：已持仓 symbol 不重复开；无仓位孤儿。
4. 退出走 `exit_profile=swing` 规则；`exit_profile=da`（含缺省）golden 回归逐字不变。
5. fee precheck / realized fee_bps / slippage 落库；admission 拒因可审计。
6. 关闭 `sleeves.enabled=false` 后，新 sleeve 停止生成/执行，DA 照常。
7. **基数自检**：`sleeve`/`trigger_mode`/`exit_profile` 在 `trade_records` 的 distinct 计数 ≥2（否则字段退化，参见军规“常量退化已三例”）。
8. **格式与量纲**：`sleeve_risk_pct` 等新键写入契约测试（百分比 vs 美元）。

## 7. 分阶段落地与治理（v2 重写：先过治理门，再谈阶段）

### 7.0 治理门（未过不许进 P2）

| 门 | 依据 | 对本文的含义 |
|---|---|---|
| **G1 OKR 入场假设名额** | `docs/OKR-2026-11.md:30`（C1 至多一个新入场假设，须独立 RFC + 用户批准）、`:164`（不新开 C1 之外的入场假设） | MOM/REV/ETF 三个入场源**不能**在 11 月同时上；本月最多“一个假设 + 用户批准”，或把整个 sleeve 降为研究/演示 |
| **G2 实验上限** | `docs/OKR-2026-11.md:123-129`（KR4：并行实验 ≤5，每个须有预期效应量/所需 n/MDE/决策日；未注册=禁止态） | 新增 sleeve 若在采任何数据，必须在该表里占位并写 §4.7 的 MDE/决策日。**注意实况（R3-11 复跑）：注册表 `REGISTRY` 实载 106 项，OBSERVING 53 / KILLED 47 / SUPERSEDED 4 / GRADUATED 2**——“≤5 个”是 KR4 目标而非现状，所以“挤掉谁”必须先盘点再由用户裁，不能当作已有 5 个的简单替换 |
| **G3 影子注册** | `app/playbook/shadow_registry.py:837` REGISTRY（SSOT）+ 军规“在采影子未注册 = 禁止态” | P1 record-only 生成器必须在 REGISTRY 建项（id/`data_path`/`min_n`/`graduation_criteria`/`eval_script`/`created`），判据**读数前写死** |
| **G4 同轴去重** | DEB 模板“立案前强制检查：同轴既有影子枚举” | `e2_momentum`（KILLED，`shadow_registry.py:1652-1679`）是入场动量确认，与本 sleeve 不同轴但要引用；**同轴的是 sibling DEB 的 H2**（研究沙盒）——两者不得算作两个独立实验来凑证据，也不得重复攒样 |
| **G5 cohort 断点** | §1.3 | 新增 `swing_horizon` 必改 `exit_config_version`；须在 OKR/DEB 落档断点日与分段报告 |
| **G6 一次一变量** | 宪章/OKR | P0 数据、P1 影子、P2 SIM、S2/S3 退出分派**不得同批上线**；每批单独可回滚 |

### 7.1 阶段表

| 阶段 | 内容 | 是否富途可见 | 治理 |
|---|---|---|---|
| P0 | DEB/RFC 裁决；G1–G6 逐条给结论；补数据（XLK/SMH 引导 + 列宽对齐）；归因**先按 `decision_id` 查询实现**（不动代码） | 否 | 零交易行为 |
| P0.5 | **零代码 demo**（§6.3）：一条手工 `MOM-ETF-<date>-<symbol>-TEST` 指令 | **是** | 一次性测试批；**`-TEST` 后缀不会自动隔离**（R5-01：A9 只认成交 `metadata.exclusion_reason`，`exit_engine.py:517` 的守卫是 symbol 不是 decision_id）⇒ 收盘后人工补 `exclusion_reason` + 回读，并在当次报表/影子读取显式排除该行 |
| P1 | record-only 影子生成器：写 `data/shadow/mom_sleeve.jsonl`，不下单；**REGISTRY 建项** | 否 | 影子先行；判据/效应量/n/MDE/决策日预注册 |
| P2 | SIM 可见小仓试点：`sleeves.enabled=true`，独立编号/子帽；S1/S2/S3 分派 + golden 回归 | **是** | 新入场源 RFC + 用户裁；一次一变量 |
| P3 | 结算：**只判“管道是否工作”**；收益读数一律标 `功效不足`（§4.7）；晋级须 PIT + 真实成本 + 前瞻 | 是/否 | 数据判，不投票 |

建议默认走 P0.5 → P1 → P2。若用户要求跳过 P1 直接 SIM 可见试点，须在 RFC 记录**知情破例**：理由、最小子帽、回滚键、观察窗、DA 不受影响证明（含 golden 回归报告）。

## 8. 风险与禁区

- **不得污染 DA KR**：新 sleeve 收益不得混入 DA-only 净 bps；不得用 MOM/ETF 尾部掩盖 DA；**并须修掉 §1.2 的 `ELSE 'DA自选'`**，否则“不混”只是 KR 脚本层面成立。
- **不得改 DA 退出/触发**：所有 sleeve policy 必须显式路由；DA 默认路径回归测试（golden）必须通过。
- **不得用全局开关换 sleeve 便利**：尤其 `overnight_hold_enabled=true`、`entry_confirm_enabled=false` 这类“一处收口”的键——它们同时改变 DA。
- **不得绕 Playbook**：禁止新 sleeve 直连 broker/notifier/webhook；也禁止复制 EXT 的“不写 `playbook_instructions` 直写 `trade_records`”旁路（§2.2）——那会同时丢 admission、覆盖仲裁和幂等。
- **不得盘中拉 Futu K_1M**：信号用日 K/缓存；执行用实时报价路由。**注意**：打开 `watchlist.etfs.enabled` 会顺带扩 K_1M 订阅槽与轮询档位（§5.7）。
- **不得抬票面摊费**：sleeve 子帽是风险预算，不是费用套利（§4.6 已给出算术，按此定尺）。
- **不得把回测/演示当晋级**：PIT/真实成本/前瞻影子/MDE 缺一不可；P0.5 demo 与 P2 试点属**已知情破例**，读数不得写入 KR。
- **不得静默失败（v2 新增）**：调仓日“应生成未生成”、“应执行被闸门拦”、“被覆盖阶梯吞掉”三种情况都必须有留痕与日检（建议挂 `daily_invariant_check` 的 W 项或独立哨兵）；否则一个月只有一次机会，错过一天整月无仓而无人知。
- **不得跨月复算**：sleeve 月频 + DA 日频混合在“整体组合”读数里会稀释 DA 的 n 与方差；整体读数必须含/不含并列。

## 9. 待用户裁决（R4 收敛版；R3 原始清单见 §13.6）

1. **读法选择（最关键）**：§0.3.3 的 **A（管线复用 + 信号新写）** 还是 **B（连信号也复用 DA，只拆账）**？这决定后面所有工程是否发生。
2. **语义边界**：sleeve 必须留在 **DA 日内语义**内（零 seam，只是另一个信号源），还是批准 **S2/S3** 让 21 日持有成立？不批则只能做日内/隔夜 demo，不能叫 swing。
3. **demo 隔离**：先做 §6.3 零代码 demo + 收盘后人工 A9 排除（推荐），还是先补“指令→成交 metadata 透传”？
4. **首试点**：ETF/MOM（低冲突，但 SPY/IWM 只有 216 根、不能做 252d；四票集中度高）还是个股 MOM（有 sibling H2 证据，但覆盖阶梯会静默吞单）？
5. **治理**：sleeve 是否占用 OKR-2026-11 的“唯一新入场假设”名额（G1）？KR4 实载 106 项如何盘点（G2）？P2 是否留到 12 月 pivot？
6. **费用尺**：整数股下选 **8 股 QQQ/$5.4k（7.686bps，≈0.92%/yr）** 还是 7 股/$4.7k（8.74bps，≈1.05%/yr）？**不得抬票面摊费**。
7. **App 端 sleeve 字样**：是否必须？若必须，另立 remark 契约审计（§6.2 四处）+ UI 验证（R3-15：App 是否展示 remark 未独立验证）。
8. **票池**：4 只 ETF 是否足够？是否允许扩到 10–12 只行业 ETF（动订阅/轮询预算与 `universe_version`）？

## 10. 最小可行路径（若只做一件事）

按“用户意图 = 在 App 里看见一条独立策略，且不另造链路”排序：

1. **今天**：§6.3 零代码 demo（1 条 `MOM-ETF-<date>-<symbol>-TEST`，须用户点头；读法 A/B 都成立）→ 满足“可见”。
2. **本周**：§4.6 费用尺定稿 + 归因查询（`decision_id LIKE 'MOM-%'`，零改代码）→ 满足“统计分开”；是否补 `source_attribution` 漏斗腿另裁。
3. **12 月**：按 OKR §6 的 pivot 讨论决定是否进 P2（SIM 可见持续持有）。此时才需要 S1/S2/S3，且一次一变量。

这条路径的好处：把“可见”“统计隔离”“策略验证”三件事拆开，任何一步被否都不会阻塞另一步；且**全程没有新链路**。P0.5 是**一次性测试批**、P1 不下单，二者都不构成“持续的新入场假设”，因此不占 11 月名额；但 P0.5 仍会真实下一笔 SIM 单，须用户点头后再做。

## 11. 本文未做的事

- 未改任何 `.py`/YAML/ENV/DB/cron/推送/影子注册；未重启；未下单。
- 未启用 sleeves；未新建生产影子；未补 ETF 数据；未跑全仓测试；未改 `watchlist.etfs`。
- 本文是设计草案与可行性核验（v2 含代码锚点勘误），不是上线裁决。

## 12. 核验锚点清单（v2，供后人复查）

| 结论 | 锚点 |
|---|---|
| 可见性由 TrdEnv.SIMULATE 决定 | `app/execution/dual_broker.py:306`；`playbook_service.py:1639-1698` |
| remark 硬契约 | `playbook_service.py:2014`；`position_sync.py:269,759`；`broker_capacity_shadow.py:207`；`live_stop_guard.py:109` |
| 隔夜三闸 | `exit_engine.py:404-412`；`unified_config.yaml:1594`；`overnight_cleanup.py:157-190` |
| DA 全局日内退出规则（不受因子块控制） | `exit_engine.py:196`（sl_tighten_beta）、`:223-250`（profit_override）、`:500-504`（adaptive trailing）、`:560-605`（early_tight）、`:1032-1074`（dead_signal）、`:1096`（mfe_stall）；`pipeline.py:2755-2877`（patient_exit 依赖 dead_signal）；`unified_config.yaml:1579-1584` |
| horizon 参数表 | `config.py:152-161`；`instruction_generator.py:246,447-451,528-531` |
| 配置哈希 | `config.py:165-232` |
| 指令覆盖/幂等 | `db.py:563-584,652` |
| 执行排序/水坝 | `auto_execute_flow.py:174-181,1240-1290` |
| 共享日帽 | `execution_flow.py:22-26,78-90`；`unified_config.yaml:1860` |
| 费用地板 | `fee_model.py:96-97`；`unified_config.yaml:20-22,1818-1840` |
| 归因兜底 | `source_attribution.py:7-16`；`daily_review.py:1218`；`weekly_review.py:187`；`direction_hit_scorecard.py:56` |
| DA 期望值桶 | `scripts/da_expectancy_report.py:24-27` |
| 影子注册表 | `shadow_registry.py:837`（REGISTRY）、`:1652-1679`（e2_momentum KILLED） |
| OKR 治理 | `docs/OKR-2026-11.md:30`（C1）、`:123-129`（KR4）、`:164`（不做清单） |
| ETF 数据/刷新 | `data/replay_bars_daily/US_QQQ.json`（2021-07-01→2026-11-10）；`scripts/refresh_daily_bars.py:162,197-215` |
| ETF 观察名单 | `unified_config.yaml:3033+`；`scheduler_core.py:815-825,1600+` |
| 非 DA 来源先例（EXT） | `scripts/extended_hours_entry_executor.py:3625,3682,3700`；DB：`trade_records.decision_id='EXT-<date>-<symbol>-<session>'` |
| 现役全局开关 | `unified_config.yaml:1297`（early_tight=true）、`:1413`（dead_signal=true）、`:1446`（mfe_stall=false）、`:1581`（adaptive=true）、`:1594`（隔夜总闸=false）、`:1603`（W_WEEK=true）、`:1643-1644`（profit_override 0.8/1.0）、`:1455`（beta=1.0） |
| 整数股费用尺 | 本轮实测（`estimate_round_trip`）：QQQ 3 股 $2,024→20.01bps / 7 股 $4,723→8.74bps / 8 股 $5,398→7.686bps |
| 影子注册表规模 | `REGISTRY` 实载 106（OBSERVING 53 / KILLED 47 / SUPERSEDED 4 / GRADUATED 2） |

## 13. R3 独立复核（2026-11-12 03:07 CST；DA 主链内 sleeve 化约束）

> 角色：独立复核方（只读复现 + 证伪式攻击）。复核对象：v2，sha256 `e55cc4da899638d8a9a9de81c6c6fca1ece5247f04fa965ed09437939fed81b8`。
> 用户追加约束：**不偏离 DA 现有逻辑/流程另造一套交易链路，不重复造轮子；直接基于 DA 现有链路做设计区分。**
> 总结论：**方向可行，但 v2 按原文不可直接执行。**“富途 App 可见”几乎零代码；“像 DA 一样跑成一条独立策略线”必须补退出分派、metadata 透传、归因腿和治理名额。v2 的零代码 demo 有硬隔离 bug，不能原样下单。

### 13.0 对“只基于 DA 链路做区分”的可行性判定

**可行，且应该复用同一条 Playbook/DA 管线。** 但必须把“复用管线”和“复用 DA 日内语义”分开：

| 层 | 必须复用 | 允许的最小新增 | 是否算新链路 |
|---|---|---|---|
| 指令 | `playbook_instructions` + `save_instruction` 仲裁 | `decision_id` 前缀、`metadata.sleeve/trigger_mode/exit_profile`、必要时的 sleeve 子帽 | 否 |
| 入场 | `auto_execute_flow` 同一循环、预算、guardrail、admission、chase_cap、entry_confirm、TCA | 在现有 `_check_intraday_trigger` 调用点加 `trigger_mode` 分派；缺省仍走 DA 原函数 | 否 |
| 执行 | `PlaybookService.execute_instruction → DualBrokerAdapter → TrdEnv.SIMULATE` | 不新增 broker/source_type/下单表 | 否 |
| 出场 | `monitor_playbook_sim_exits → exit_engine` 同一监控 | 在现有 `exit_engine`/`overnight_cleanup` 内加 `exit_profile` 分派；缺省 DA 逐字不变 | 否 |
| 统计 | 现有 `trade_records`/报表 | 归因 CASE 腿、expectancy sleeve 桶、A9/A10 口径 | 否 |

**关键取舍**：

- 如果“基于 DA 链路”指**完全保留 DA 触发与 DA 日内退出**：可以做编号/归因/预算隔离，也可以做 App 可见 demo；但 sleeve 只是 DA 语义内的子集或演示，**不能做 21 天 swing，也不会自动破除“赢小输大”**。
- 如果目标是**独立 MOM/swing 策略线**：仍走同一条管线，但必须在现有函数内加显式最小分派（S1/S2/S3）。这不是另造链路，而是同一链路里的策略语义分支；必须影子先行、DA 缺省 golden 回归、一次一变量。
- “赢小输大”的根因在 DA 日内几何：BE/早退/锁利截断赢家，`SL_INITIAL`/隔夜 gap 承担左尾。**只换入场标签、不换出场几何，不会解决这个问题。**

推荐收窄为：**DA 主链不变 + 一个 sleeve 试点 + 最小分派点**。不建议同时上 MOM/REV/ETF 三个生成器；先一个假设、一个编号、一个预算子帽、一个退出档。

### 13.1 复现与证据

本轮只读复跑：

```bash
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python - <<'PY' > /tmp/deb_sleeve_review/verify_r3.stdout.json
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('verify_v2', '/tmp/deb_sleeve_review/verify_v2.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.OUT = Path('/tmp/deb_sleeve_review/verify_r3.json')
module.main()
PY

PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python /tmp/deb_sleeve_review/verify_r3_supplement.py \
  > /tmp/deb_sleeve_review/verify_r3_supplement.stdout.json
```

产物：

- `/tmp/deb_sleeve_review/verify_r3.json` sha256 `45e54f075e09e814ab2bfefcc5549db1150db050a8e79e7c7a31cb2055ca41d6`
- `/tmp/deb_sleeve_review/verify_r3_supplement.py` sha256 `3b9f756e5aec58f60b6263e38aca36e727db9cc4ec2750af7ec09d824cc63ad5`
- `/tmp/deb_sleeve_review/verify_r3_supplement.json` sha256 `b28241263a30021c4a4daf4e5ac95bc1493bb0c76aa7af52bc9e0d57f82cc829`

上一轮聚焦测试：`./venv/bin/python -m pytest -q tests/execution/test_fee_model.py tests/test_fee_floor_sizing.py` = **27 passed**。测试框架报告已知存量生产库写入：`data/ftseek_us.db ×5`（`capital_state/capital_state_history/sqlite_master`）。本轮未重跑该测试，避免重复生产库写入；本轮探针本身只读 DB/本地缓存，零下单、零重启、零 repo 代码/配置/DB/cron 变更。早期探针曾误触发一次 Futu quote-context 连接/断开（无下单），本轮复跑已移除该 import，零券商调用。

### 13.2 R3 确认项（v2 成立的部分）

| # | 确认项 | 证据 |
|---|---|---|
| C1 | App 可见性路径成立 | `playbook_service.py::execute_instruction` → `_execute_via_dual_broker_inner`；`target_env=sim` 派生 `source_type=PLAYBOOK_SIM`、`owner_strategy=playbook_sim`；`dual_broker.py:306` 传 `remark` 并走 `TrdEnv.SIMULATE` |
| C2 | 直接执行的 demo 仍会被现有出场监控接管 | `exit_monitor_flow.py` 选择所有未平 `PLAYBOOK_SIM/LIVE` trade records，不按 DA 前缀过滤 |
| C3 | 隔夜三闸成立 | `exit_engine.py:404-412` `_skip_allowed`；`overnight_hold_enabled:false`；`overnight_cleanup.py:157-195` 只认 cohort B 盈利仓 |
| C4 | 归因污染成立 | `source_attribution.py` `ELSE 'DA自选'`；探针中 `MOM-* / REV-* / ETF-* / EXT-*` 均落 `DA自选`；`app/playbook/db.py` 统计腿 `ELSE 'OTHER'` 安全 |
| C5 | 配置哈希会断 cohort | `config.py::get_exit_config_version` 哈希整本 `playbook_horizon.yaml` + 退出键；当前 `exit-ca4487172a6f` |
| C6 | 生成器键名勘误成立 | `instruction_generator.py` 读 `buy_fixed_stop_loss_pct/sell_fixed_stop_loss_pct` 与 `vol_buckets`；`skip_pre_close_if_profit` 在 exit_engine 无消费方 |
| C7 | 指令覆盖/优先级风险成立 | `db.py::save_instruction`：DA/UE/reentry=3，其他非空=1，空=0；同 `decision_id` 幂等，已执行终态不可重写；DA 可覆盖同日同票未执行 MOM |
| C8 | 治理约束成立 | OKR-2026-11 C1 至多一个新入场假设且须 RFC+用户批准；KR4 并行实验 ≤5 是目标；`e2_momentum` 已 KILLED |
| C9 | 当前无 MOM 成交污染 | 只读 DB：`PLAYBOOK_SIM` DA 609/closed 603、EXT 61、OTHER 171、EMPTY 4、MOM 0；未平 Playbook 6 |

### 13.3 R3 攻击点与必须修正项

| # | v2 说法/隐含假设 | R3 复核结果 | 必须动作 |
|---|---|---|---|
| R3-01 | §6.3：指令写 `metadata.exclusion_reason` 后，A9 会自动剔除成交 | **硬错误。** 指令 metadata 会落 `playbook_instructions.metadata_json`，但成交 metadata 在 `playbook_service.py:2282-2315` 固定构造，不复制 `inst.metadata`，也没有 `exclusion_reason`。全文件无 `exclusion_reason` 字符串。DB 中指令 exclusion=0；成交 exclusion=4，全是 EXT 首跑测试且 `playbook_instruction_id=None` | demo 前必须补“指令→成交”metadata 透传，或成交后 race-safe 补写并校验；否则只能人工 A9 排除且须用户知情批准 |
| R3-02 | §6.4：成交 `metadata.sleeve='mom_etf'` 可验收 | **同根因不成立。** `metadata.sleeve` 也不会自动到 `trade_records` | 与 R3-01 同批修；验收必须查成交表，不只查指令表 |
| R3-03 | demo 构造指令 `source_type=PLAYBOOK_SIM` | `playbook_instructions` **没有 `source_type` 列**；成交 source_type 由 `target_env` 派生 | demo 参数写 `target_env="sim"`；不要伪造指令列 |
| R3-04 | `MOM-ETF-{rebalance_date}` 每票一条 | `decision_id` 索引非唯一；同 ID 跨 symbol 会歧义 | 用 `MOM-ETF-{date}-{symbol}`；调仓重跑复用同 ID |
| R3-05 | swing 只需跳过 early/dead/stall/adaptive/profit_override/beta | **仍漏三条日内语义**：①`cooldown_sl_pct:4.0` 前 15 分钟可把 3% SL 放宽到 4%；②固定 `TAKE_PROFIT` 在 TP>0 且 RR≥1.5 时仍 active，和“只走排名/SL/max_hold”矛盾；③W_WEEK 水下亏损豁免会继承 DA 5 日修复（探针：-1% true，+1% false） | swing 用**显式 allowlist**：初始 SL、自身 gap stop、排名/调仓退出、max_hold；显式排除 DA BE/profit_override/early/dead/stall/cooldown/W_WEEK/reentry 豁免；TP=0 或显式跳过 |
| R3-06 | `profit_override_threshold_pct:1.5 / drawdown:0.5`，+2% 会被锁 | 当前 YAML 是 **0.8 / 1.0**。且 `_compute_dynamic_effective_sl` 在 `peak < breakeven_at_pct` 时提前返回；若 swing BE=3，peak=2% 不锁（SL 仍 97），peak=4% 才锁到 102.96 | 参数按当前配置重写；不能把“+2% 必锁”当通用结论 |
| R3-07 | `mfe_stall_exit` 会 150 分钟砍仓；beta 收紧需确认 | 当前 `mfe_stall_exit_enabled:false`，不是现役；`sl_tighten_beta:1.0` 探针确认 no-op | 风险表区分“现役”和“未来打开才生效” |
| R3-08 | ETF 数据只缺 XLK/SMH | **低估历史缺口**：QQQ 1347 根（2021-07-01→2026-11-10，列宽 5/6）；XLE 1330、XLRE 1326、XLY 1330（列宽 5）；SPY/IWM 仅 216 根（2026-01-02→2026-11-10），够 MA200 但不够 252d lookback；XLK/SMH 缺失；无 ETF 1m。`refresh_daily_bars.py` 只刷已有文件，新增行只写 5 列 | P0 先补 PIT/历史/列宽；不能把“文件存在”当“可回测” |
| R3-09 | `$2500≈16bps`、`$3449` 是可行下限、`$4920≈≤8bps` | 整数股后不成立。本地 last close 探针：QQQ $2500→3 股/$2024/20.0bps；$3449→5 股/$3374/12.12bps；$4920 cap→7 股/$4723/8.74bps；费用可行最小 6 股/$4049/10.15bps。XLE/XLRE/XLY 的 $4920 cap 约 9.30/9.71/8.94bps | 费用尺按整数股重算；不得为摊费抬票面（OKR 禁止） |
| R3-10 | 与 `max_daily_notional_usd` 三处互锁 | 现有全局日帽不是该键，而是 `daily_notional_cap_pct:0.8 × risk_base_capital`（$82k→$65,600）；`max_open_risk_pct:4.0` 在 admission 段 | sleeve 子帽可以是新键，但必须和现有计算出的全局帽比较；不要引用不存在的全局键 |
| R3-11 | “不得挤掉现有 5 个实验” | 注册表实载 **106** 项：OBSERVING 53、KILLED 47、SUPERSEDED 4、GRADUATED 2。≤5 是 KR4 目标，不是现状 | 新 sleeve 占位前必须盘点/用户裁；不能把“现有 5 个”当事实 |
| R3-12 | 24 笔/年 → MDE80≈343bps | 独立腿假设下数值对，但宪章要求按日/调仓簇。2 腿/调仓≈12 簇/年；若腿间相关 ρ=0.5，MDE80≈420bps；ρ=1≈485bps | 预注册报 rebalance-day clusters + 协方差/相关敏感性，不只报 trade n |
| R3-13 | VWAP 门只是执行质量/防追高 | 当前是**方向一致性硬门**：LONG 必须在 VWAP 上侧，SHORT 下侧；保留它会改变 scheduled MOM 的入场人群 | 若保留，预注册为 sleeve 入场过滤；若绕过，显式分派并留 `trigger_miss_reason` |
| R3-14 | `stall_entry_shadow_tag.py` 旁路也会落 `DA自选` | 该脚本实际 `ELSE 'other'`，不是 `DA自选` | 修正污染清单；主污染面仍是 `source_attribution.py` |
| R3-15 | App 端可见 pb 编号/sleeve 字样 | 代码只证明 `remark` 被发送；**App UI 是否展示 remark 未独立验证**。remark 还是恢复/对账凭证 | 首试点不改 remark；内部 DB 归因为准。若必须 App 显示 sleeve，另做 remark 契约审计 + UI 验证 |

补充：`_get_factor_exit_config` 按 conditions 顺序/方向匹配；`sleeve_mom` 可能在第二轮命中，但其他 DA condition 可能第一轮先命中。swing 档应显式选择 sleeve factor block，或要求单一 sleeve condition，不能靠字符串巧合。

### 13.4 直接 demo 不等于 DA-like 全链路

`execute_instruction` 直接调用会绕过 `auto_execute_flow` 的 scheduled trigger、chase_cap、entry_confirm、单轮水坝和日预算加载；它仍保留 portfolio guardrail、admission、VIX、buy quota、duplicate guard。因此：

- **能证明**：SIM 下单可见、成交落库、现有 exit monitor 会接管。
- **不能证明**：像 DA 一样被定时触发、过完整入场闸、按 sleeve 预算执行。
- 若要“DA-like 自动入场”，必须走 `auto_execute_flow`，并在其中加 S1 `trigger_mode` 分派；不能用手工 `execute_instruction` 冒充完整链路。

### 13.5 DA 主链内的最小落地路径（R3 推荐）

> **[R4 覆盖 2026-11-12]** 本节阶段顺序以 **§10** 为准：metadata 透传从 P0 降为可选（成交归因改用 `decision_id`），S4 归因腿与 S5 子帽不再是链路前置，S1/S2/S3 一律后置到 P2 且需用户裁决。

| 阶段 | 内容 | 是否交易 | 是否新链路 | 前置 |
|---|---|---|---|---|
| P0 修尺子 | metadata 透传/exclusion、归因 CASE 腿、expectancy sleeve 桶、`decision_id` 含 symbol、ETF 历史/列宽审计、整数股费用尺 | 否 | 否 | 无 |
| P0.5 可见性 demo | 一条手工 `MOM-ETF-{date}-{symbol}-TEST` 走现有 `execute_instruction(target_env="sim")`；DA 日内退出接管 | 是，一次性 SIM | 否 | R3-01/02 修复或人工隔离 + 用户批准；不得写入 KR |
| P1 DA 链内影子 | 一个 sleeve 假设 record-only；复用指令 schema/报表，不下单；注册表占位 | 否 | 否 | KR4 盘点 + C1 名额裁决 + 预注册 n/MDE/决策日 |
| P2 最小 policy 分支 | 若批准 swing：S1 trigger_mode、S2 exit_profile allowlist、S3 overnight_cleanup 同款分派；DA 缺省 golden 逐字不变 | 是，SIM 小仓 | 否 | 独立 RFC + 用户批准 + 一次一变量；大概率 12 月 pivot |

编号建议：首试点只用 `MOM-ETF-{date}-{symbol}`；`REV-*`/`ETF-*` 保留占位，不同批上线。统计必须 DA / EXT / sleeve / 整体含 sleeve / 整体不含 sleeve 并列；DA KR1 仍只算 `DA-%`。

### 13.6 仍需用户裁决

1. **语义边界**：接受“DA 主链同最小分派”以支持 swing，还是本月只允许 DA 日内语义内的 demo/子集？
2. **demo 隔离**：先修 metadata 透传再 demo，还是接受一次性人工 A9 排除 + 知情破例？
3. **治理名额**：是否占用 C1 唯一新入场假设？KR4 106 项注册表如何盘点出 ≤5 个现役实验？
4. **费用尺**：接受整数股后 $4k 上下/腿、往返约 9–12bps，还是坚持更小票面并接受更高费用率？不得抬票面摊费。
5. **出场 allowlist**：是否批准 swing 只保留初始 SL/gap/rank/max_hold，显式排除 DA 日内修复规则？
6. **App 标签**：是否必须 App 端显示 sleeve 字样？若必须，另立 remark/UI 验证批。

### 13.7 R3 未验证 / 不做

- 未验证富途 App UI 是否展示 remark；未下单、未重启、未改 repo 代码/配置/DB/cron/推送/影子注册。
- 未实现 S1/S2/S3/S4/S5；未补 ETF 数据；未建 PIT mart；未新建生产影子。
- 未重跑会写生产库的聚焦测试；上一轮 27 passed 及已知写入豁免见 §13.1。
- 本文仍为设计草案 / 待用户裁决；R3 不替代 v2，也不静默改写 v2 历史轮次。

## 14. R4 本轮复核记录（2026-11-12；用户追加“只复用、不另造链路”后）

> 角色：作者对 R3 的**逐条复跑**（仓纪律：复核方自报可能误报，作者必须在真文件上重跑）。
> 本轮只读：代码/配置读取、SQLite 只读查询、本地 `estimate_round_trip` 计算、`REGISTRY` 计数；**零下单、零重启、零 repo 代码/配置/DB/cron 变更**。

| R3 条目 | 本轮复跑 | 证据（本轮独立执行） |
|---|---|---|
| R3-01/02 metadata 不通透 | **成立** | `playbook_service.py:2282-2315` 固定构造 metadata（仅 `conditions`/`decision_id` 等白名单键）；PRAGMA `playbook_instructions` 无 `source_type` 列；DB：指令 `exclusion_reason` 0 行、成交 4 行（EXT 首跑批） |
| R3-03 指令无 source_type | **成立** | PRAGMA table_info 实测列清单无该列 |
| R3-04 decision_id 需含 symbol | **成立** | `db.py` 仅有普通索引 `idx_playbook_instructions_decision_id`（非唯一） |
| R3-05 漏 cooldown/TP/W_WEEK | **成立** | `exit_engine.py:536-538`（首 15 分钟 SL→4%）；`:749`（TP>0 即生效）；`unified_config.yaml:1594/1603`（隔夜总闸 off、W_WEEK on） |
| R3-06 profit_override 现值 | **成立** | `unified_config.yaml:1643-1644` = 0.8 / 1.0；`exit_engine.py:201` BE 之前提前返回 |
| R3-07 mfe_stall/beta 状态 | **成立** | `unified_config.yaml:1446`（false）、`:1455`（beta 1.0）；`config.py:332` `beta>=1.0 → 原样返回` |
| R3-08 ETF 历史缺口 | **成立** | QQQ 1347/XLRE 1326/XLE 1330/XLY 1330（5–6 列）；**SPY/IWM 各 216 根（2026-01-02 起）**，不足以 252d；XLK/SMH 缺 |
| R3-09 整数股费用尺 | **成立（已重算并替换 §4.6）** | QQQ：$2,500→3 股 $2,024 → 20.01bps；$3,449→5 股 $3,374 → 12.12bps；$4,920→7 股 $4,723 → 8.74bps；费用可行最小 6 股 $4,049 → 10.15bps |
| R3-10 全局日帽键 | **成立** | 全仓无 `max_daily_notional_usd`；`unified_config.yaml:1157` `daily_notional_cap_pct: 0.8` |
| R3-11 注册表规模 | **成立** | `REGISTRY` 实载 106：OBSERVING 53 / KILLED 47 / SUPERSEDED 4 / GRADUATED 2 |
| R3-12 调仓簇 MDE | **未复算（接受为敏感性假设）** | 本文只采用“按簇报告 + ρ 敏感性”的口径要求，不把 420/485bps 当实测 |
| R3-13 VWAP 是方向硬门 | **成立** | `trigger_flow.py:857-861`：`is_short ? dev>tol 拦 : dev<-tol 拦` |
| R3-14 stall 脚本归因 | **成立** | `scripts/stall_entry_shadow_tag.py:127` `ELSE 'other'` |
| R3-15 App UI 未验证 | **接受（本轮同样无法验证）** | 本轮无 App 访问；只确认 remark 发送路径 |

### 14.1 本轮对文档做的实质修改（对应“只复用”约束）

1. 新增 **§0.3 复用矩阵 + 两种读法**，把“不另造链路”变成可验收的边界；明确 S4/S5 与 metadata 透传**都不是链路必需**，代码 seam 收敛为 S1/S2/S3 且可后置。
2. 新增 **§2.2 EXT 先例**：仓库已用同一条 Playbook 管线承载第二个策略线（`EXT-<date>-<symbol>-<session>`）；同时标注 EXT 直写 `trade_records`、绕过 `save_instruction` 的做法**不复制**。
3. 新增 **§2.3 配置即用杠杆**：TP=0、因子块 BE、09:50 入场绕冷却、复用 `long_horizon`、不新增 horizon 块（不破 `exit_config_version`）。
4. 按 R3 逐条修正 v2 的数值/结论：费用表改整数股（§4.6）、profit_override 改现役 0.8/1.0 且受 BE 门控（§1.1/§4.5）、`mfe_stall` 标为未启用（§1.1）、VWAP 改为方向硬门（§4.4）、SPY/IWM 历史缺口（§4.1）、注册表 106 项（§7 G2）、A9 不会自动剔除 demo（§6.3）、成交归因改用 `decision_id`（§3/§6.4）、`decision_id` 含 symbol（§3）、stall 脚本非 DA 污染（§1.2）。
5. §9/§10 重写为“先选读法 → 再谈工程”的两步裁决，避免在语义未定前讨论实现。

### 14.2 仍未解决、留给下一轮的三个问题

1. **“只复用”与“21 日持有”不可兼得**：不做 S2/S3 就只能日内语义（复用最彻底、但策略形状受限）。这是语义取舍，不是工程能力问题。
2. **demo 隔离没有零代码且自动的路径**：A9 只认成交 `metadata.exclusion_reason`，指令侧写不进去；要么人工补写，要么加透传（与“不另造”冲突）。
3. **四票 ETF 池的证据基础薄弱**：SPY/IWM 无法做 12-1；XLK/SMH 需引导；四票 top2 等于 50% 集中度，且 sibling DEB 无 ETF 版本证据。

## 15. R5 共识设计说明（2026-11-12 03:34 CST；核查 R4 后）

> 角色：独立复核方。复核对象：v3/R4，sha256 `64d465c7edc28ba1677965c0d9a591e61707869a35758f359826c0edcdbdeb8c`。
> 结论：**R4 的“只复用 DA/Playbook、不另造链路”方向成立，可以收敛为共识设计；但 R4 仍有 4 处“零代码/配置即用”说法必须修正。** 本节是 R3/R4/R5 的共识版说明，仍待用户裁决，不是上线批准。

### 15.1 R4 增补核查：哪些成立，哪些要改

| # | R4 说法 | R5 核查 | 共识处理 |
|---|---|---|---|
| R5-01 | demo 用 `-TEST` 命名即可隔离；`TEST` 标记不落影子 | **不成立。** A9 谓词只认成交 `metadata_json.exclusion_reason`，不认 `decision_id` 后缀；探针中 `MOM-ETF-...-TEST` + 空 metadata 仍被 A9 保留。`exit_engine.py:517` 的影子守卫是 `_symbol != "TEST"`，不是 decision_id 含 TEST；真实 ETF demo 不会因 `-TEST` 自动免影子 | demo 必须成交后人工补 `exclusion_reason` 并回读核验，同时在报表/影子口径显式排除；不得宣称“零代码自动隔离” |
| R5-02 | 复用 `long_horizon(max_hold_days:3)` 可 3 日持有、无需新 horizon | **参数存在，但语义不成立。** 现役 `overnight_hold_enabled:false`；生产 monitor 注入 `_skip_allowed=cohort_b_skip_eligible(...)`，不是按 horizon long 放行。long_horizon  alone 不能突破隔夜总闸/EOD 兜底 | “配置即用”只限于 TP=0、BE 因子块、09:50 避冷却等；持有天数仍受 S3/隔夜闸约束 |
| R5-03 | 写后回读直接复用 `ue_batch.confirm_saved()` | **工具存在，但有前置条件。** `confirm_saved` 无 `candidate_screening.candidate_id` 时返回 `unknown`，并比对 `source/decision_id/symbol/direction/skip_reason` | sleeve 写入方必须提供稳定 `candidate_id`（可用 decision_id）和 `source`；否则不能把返回 id 当入围 |
| R5-04 | metadata 透传不是链路必需，S2 可用 `exit_profile` | **归因可不透传，但出场路由不能悬空。** 成交 metadata 固定构造；`exit_monitor_flow` 当前不把 `decision_id/exit_profile` 注入 meta。若只写指令 `metadata.exit_profile`，exit_engine 看不到 | 共识用 `decision_id` 前缀做权威 sleeve 键；在现有 monitor/exit_engine 接缝注入/派生 `_sleeve/_exit_profile`，或成交时白名单写入最小路由键。不需要全量 metadata 透传，但需要显式路由 |
| R5-05 | 21 日持有只需 S2+S3 | **若还要定时调仓入场，需 S1+S2+S3。** 只 S2/S3 仍会被 DA breakout/VWAP/时间窗触发门筛选 | 模式表必须区分“DA 触发 + swing 退出”和“scheduled 触发 + swing 退出” |
| R5-06 | QQQ $5,400/8 股往返 7.65bps | 方向正确，精确复算为 **7.686bps**（费 $4.1492 / 名义 $5,398.08） | 费用表用整数股精确值；不影响“不得抬票面摊费”结论 |
| R5-07 | EXT 是同管线非 DA 先例；但直写 trade_records 不复制 | **成立。** EXT 用 `EXT-{date}-{symbol}-{session}`、`source_type=PLAYBOOK_SIM`、`owner_strategy=extended_hours_entry`，成交进同一表并由 Playbook 出场监控接管；同时明确绕开 `save_instruction` | sleeve 学 EXT 的编号/同表/同出场，不学 EXT 的直写旁路；必须走 `save_instruction` |
| R5-08 | S4/S5 不是链路必需 | **运行链路成立，报表隔离不成立。** 全局帽已覆盖 sleeve；成交按 `decision_id` 可查。但日报/周报漏斗仍 `ELSE 'DA自选'`，expectancy 报告只有 DA/EXT 桶 | S5 子帽可选；S4 报表腿可后置，但在补腿前只能叫“可按前缀查询”，不能叫“报表已隔离” |

### 15.2 共识设计逻辑（通俗版）

**一条流水线，多个入口标签；DA 是默认档，sleeve 是显式档。**

1. **不新建系统**：仍用 `playbook_instructions → save_instruction → auto_execute_flow → execute_instruction → DualBrokerAdapter → Futu SIM → monitor_playbook_sim_exits`。不新建 DB/表/source_type/broker/退出循环/预算器/绩效系统。
2. **编号即归属**：`DA-*` 是 DA；`MOM-ETF-{date}-{symbol}` 是首试点 sleeve；`EXT-*` 是既有扩展时段臂。成交侧权威键是 `trade_records.decision_id`，不是指令侧 `metadata.sleeve`。
3. **写入方只负责信号**：sleeve 生成器只产出指令并调 `save_instruction`；冷却、同票互斥、预算、admission、chase_cap、entry_confirm、TCA 都继承现有管线，不重复造轮子。
4. **语义必须显式**：缺省 = DA 原触发/原退出。要定时入场，加 `trigger_mode` 分派；要 swing 持有，加 `exit_profile` allowlist + EOD 同款分派。不能靠 horizon 名字或字符串巧合隐式改变 DA。
5. **风控共享，子帽可选**：全局日帽/持仓帽/组合热帽对 sleeve 生效；sleeve 子帽是额外风险约束，不是链路必需。不得为摊费抬票面。
6. **统计分开是口径工程**：DA KR 仍只算 `DA-%`；sleeve 按前缀单独桶；整体组合含/不含 sleeve 并列；表现读数拼 A9/A10；日报/周报漏斗必须补腿或显式披露未隔离。
7. **破除“赢小输大”靠出场几何，不靠标签**：若完全保留 DA 日内 BE/早退/锁利，sleeve 仍会赢小输大。要改变收益分布，必须批准独立出场 allowlist；否则只是新信号源/分账演示。

### 15.3 先选模式，再谈实现（共识模式表）

| 模式 | 含义 | 需要接缝 | 能得到 | 得不到 |
|---|---|---|---|---|
| B 纯拆账 | 连信号也复用 DA，只按编号/预算/统计拆账 | 0 | 链路验证、分账展示 | 新 alpha；不解决赢小输大 |
| A1 DA 语义内新信号 | 新写入方，但仍过 DA breakout/VWAP/日内退出 | 0（信号写入方不算交易链路接缝） | App 可见的“非 DA 候选”日内交易 | 定时调仓、21 日持有、独立出场几何 |
| A2 定时入场 + DA 日内退出 | scheduled 触发，但退出仍 DA | S1 | 定时调仓的日内 demo | 持续持有；仍受 DA 日内几何 |
| A3 定时入场 + swing 持有 | scheduled 触发 + 独立出场 allowlist | S1+S2+S3（另需 S0 路由键） | 真正独立 MOM/swing 策略线 | 本月无治理名额/未批准则不能上 |

**推荐共识**：本月只做 B/A1 的口径与可选 demo；A2/A3 进入 RFC/影子/12 月 pivot 讨论。若用户核心目标是“提升收益、破除赢小输大”，最终必须评估 A3 或 DA 自身出场优化；A1/B 不会自动达成。

### 15.4 最小接缝共识（修订 R4 的“0～2 个 seam”）

| 接缝 | 位置 | 最小动作 | 是否新链路 | 必需性 |
|---|---|---|---|---|
| S0 路由键 | `exit_monitor_flow` / 成交 metadata 构造 | 以 `decision_id` 前缀派生 `_sleeve/_exit_profile`；或白名单写入最小路由/排除键 | 否 | A3 必需；demo 隔离必需 |
| S1 触发分派 | `auto_execute_flow` 调 `_check_intraday_trigger` 处 | `trigger_mode=scheduled` 走显式 scheduled 判据；缺省走 DA 原函数 | 否 | A2/A3 必需 |
| S2 出场档 | `exit_engine` | `exit_profile=swing` 只跑 allowlist：初始 SL、自身 gap、rank/rebalance、max_hold；白名单外 DA 规则不跑 | 否 | A3 必需 |
| S3 EOD 兜底 | `overnight_cleanup` | 同款 `exit_profile` 分派；全局 `overnight_hold_enabled` 不为 sleeve 打开 | 否 | A3 必需 |
| S4 报表腿 | `source_attribution` / expectancy / 日周报 | sleeve CASE 腿 + 含/不含整体并列 | 否 | 链路可后置；“报表不混淆”必需 |
| S5 子帽 | 现有预算查询后 | 按 `decision_id` 前缀加 sleeve 计数/名义/风险子帽 | 否 | 可选风险约束 |

**Demo 隔离不是 S0 的自动替代品**：一次性 demo 若不改代码，必须成交后人工写 `trade_records.metadata_json.exclusion_reason`，回读核验，并在当次报表/影子读取中显式排除该 trade_id/decision_id。`-TEST` 后缀本身不够。

### 15.5 编号、预算与统计契约（共识版）

- **首试点编号**：`MOM-ETF-{YYYYMMDD}-{symbol}`；测试批追加 `-TEST`，但测试隔离靠成交 exclusion/显式谓词，不靠后缀。
- **路由字段**：`source_type=PLAYBOOK_SIM`、`owner_strategy=playbook_sim` 不变；不新造 `SLEEVE_SIM`。
- **指令侧标记**：`metadata.sleeve/trigger_mode/exit_profile` 可写，但成交侧权威仍是 `decision_id`；若需要成交侧 profile，必须 S0 显式路由。
- **写入确认**：`save_instruction` 返回 id 后必须回读；复用 `confirm_saved` 时提供 `candidate_screening.candidate_id` 与 `source`。
- **出场几何**：swing 试点 TP=0；初始 SL 用 sleeve 参数；不继承 DA BE/profit_override/early/dead/cooldown/W_WEEK/reentry 豁免，除非预注册显式允许。
- **费用尺**：按整数股和真实 fee_model 定稿；QQQ 7 股/$4,723≈8.74bps，8 股/$5,398≈7.69bps；不得为摊费突破风险帽。
- **统计桶**：DA / EXT / MOM-ETF / 其他 / 整体含 sleeve / 整体不含 sleeve；DA KR1 只认 `DA-%`；n<30 不下结论；月频按调仓簇报 MDE/相关性敏感性。

### 15.6 落地顺序与仍需裁决

1. **P0 口径/数据/费用**：确定模式 B/A1/A2/A3；补 ETF 历史/列宽/PIT；整数股费用尺；定义 decision_id 统计查询和报表披露。零交易行为。
2. **P0.5 可选 demo**：用户批准后一条 SIM 指令；走现有 Playbook；成交后人工 exclusion + 回读；不写 KR，不宣称自动隔离。
3. **P1 单 sleeve 影子**：一个假设、一个编号、record-only；KR4 注册表盘点后占位；预注册效应量/n/调仓簇 MDE/决策日。
4. **P2 最小分派**：若批准 A3，S0/S1/S2/S3 一次一变量；DA 缺省 golden 回归；SIM 小仓；大概率 12 月 pivot。

仍需用户裁：①模式选择；②demo 是否接受人工隔离；③C1/KR4 名额；④费用尺；⑤swing allowlist；⑥App 是否必须显示 sleeve 字样。

### 15.7 R5 复现与未验证

```bash
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python /tmp/deb_sleeve_review/verify_r5.py \
  > /tmp/deb_sleeve_review/verify_r5.stdout.json
```

- `/tmp/deb_sleeve_review/verify_r5.py` sha256 `952af7257ff494058eb614523979b959fbba4bc9e446230535432f0642ad423e`
- `/tmp/deb_sleeve_review/verify_r5.json` sha256 `69784f243d3ba3d23c639da729683cff5313c5a7108ae1636e7a822e17934d60`

本轮只读代码/配置/本地缓存/内存 SQLite；零下单、零重启、零 repo 代码/配置/DB/cron/推送/影子注册变更。未验证富途 App UI；未实现 S0–S5；未补 ETF 数据；未建 PIT mart；未跑全仓测试。

## 16. R6 极简收敛（2026-11-12 03:42 CST；回答“更合理优雅、避免过度设计”）

> 结论：**还能更简。** 共识从“多 sleeve 框架”降级为“DA 主链上的一个试点 policy”。优雅不是零改动，而是**只改必须改的语义，其余全部复用**。

1. **S0 可再降级**：Playbook 成交 metadata 已写 `attribution.decision_id`（`playbook_service.py:2308-2313`），exit monitor 又把 `metadata_json` 读成 meta。因此出场侧可直接读已有 `meta.attribution.decision_id` 做路由，**不必新增 metadata 透传，也不必默认注入 `_decision_id`**。只有旧行/EXT 直写缺该键时，才回退读 `trade_records.decision_id` 列。
2. **只做一个试点，不做通用框架**：首试点只认 `MOM-ETF-V1-*`（或 `MOM-ETF-{date}-{symbol}` 加版本段）。不做 REV/ETF 占位、不做 `sleeves.enabled` 命名空间、不做通用 `trigger_mode/exit_profile` 枚举、不做 S5 子帽、不改 App remark。第二个策略真出现时再抽象。
3. **一个单点解析函数**：`policy_for(decision_id)` 返回 `DA_DEFAULT` 或 `MOM_ETF_V1`。入场触发、出场引擎、EOD 兜底都只问这一个函数；DA_DEFAULT 逐字走旧路径。避免前缀字符串散落多处。
4. **参数尽量放指令/policy 版本，不碰 `playbook_horizon.yaml`**：SL/size/TP=0 由写入方给；max_hold/allowlist 作为试点 policy 常量或独立版本键。这样 DA `exit_config_version` 不断；试点 cohort 用 decision_id 前缀/版本识别。改试点参数 = 新版本，不混 DA。
5. **出场先做最小闭环**：初始 SL + max_hold + TP=0；排名/调仓替换退出后置。先证明“宽止损/长持有/少截断”的管道和几何可行，再补复杂调仓。
6. **统计先查询，后报表工程**：P0 用 `decision_id LIKE 'MOM-ETF-%'` 只读查询分桶；报表腿、子帽、自动 exclusion 都后置。demo 仍人工补成交 `exclusion_reason` 并回读，不宣称自动隔离。
7. **过度设计删除清单**：多策略同时上、通用 sleeve 配置框架、成交 metadata 全量透传、新 source_type/owner、新预算器、新绩效系统、App remark 改格式、为摊费抬票面、未预注册就扩票池。

**通俗版**：不是给商场再盖一栋楼，而是在老店里加一个挂牌实验柜台。收银、仓库、保安都用原来的；只给这个柜台一块牌子（decision_id）和一张小规则卡（policy）。牌子已经会随成交落库，所以不用新布线；规则卡集中放一处，所以不会把老店规则改乱。

## 17. R7 复查建议（2026-11-12；复核 R5/R6 + 执行链反证）

> 角色：作者复核 R5/R6（由独立复核方追加）。方法：在真文件上逐条重跑，并额外追问“**执行链实际会怎样**”，而不是只看文档是否自洽。
> 结论：**R5 的 8 条全部成立，R6 的方向成立（单点 `policy_for` 是好设计）；但 R6-4 与执行链冲突，另有三个实现陷阱会在 P2 咬人。**

### 17.1 逐条核查

| 条目 | 本轮复跑 | 增补 |
|---|---|---|
| R5-01 `-TEST` 不自动隔离 | **成立** | A9 只认成交 `metadata_json.exclusion_reason`；`exit_engine.py:517` 守卫是 `_symbol != "TEST"`（symbol 级）⇒ demo 若用真实 ETF，还会继续写影子 jsonl（见 §17.3） |
| R5-02 `long_horizon` 不能突破隔夜闸 | **成立，且更强** | monitor 先注入 `_skip_allowed=cohort=="B"`（`exit_monitor_flow.py:520,525`），`:409` 总闸（false）再清零 ⇒ `:406` 的 `horizon=="long"` 分支在监控路径**是死代码**。另：cohort 由 `hash(symbol-date)%2` 随机分配（`trade_store.py:56-64`）⇒ 总闸若开，约一半 sleeve 仓靠抽签持夜 |
| R5-03 `confirm_saved` 需 `candidate_id` | **成立** | 缺 `candidate_screening.candidate_id` 直接 `unknown`（`ue_batch.py:89-113`） |
| R5-04 出场路由不能悬空 | **成立，但可再简化** | monitor 的 SELECT **已经取了 `decision_id`**（`exit_monitor_flow.py:89-90`）⇒ 直接 `policy_for(r["decision_id"])` 即可，连 `meta.attribution.decision_id` 都不必读，也覆盖 EXT/补录行 |
| R5-05 A3 需要 S1+S2+S3 | **成立** | — |
| R5-06 费用精确值 | **成立** | 8 股 QQQ：$5,398.08 / 费 $4.1492 / **7.686bps**；7 股：$4,723.32 / $4.1293 / 8.742bps（本文已回填） |
| R5-07 EXT 先例 | **成立** | 补充：EXT 的 `owner_strategy='extended_hours_entry'` 是直写路径自有值；走 Playbook 的 sleeve 会得到 `playbook_sim` |
| R5-08 S4 运行可行、报表不可省 | **成立** | 未补腿前只能说“可按前缀查询”，不能说“报表已隔离” |
| R6-1 用已有字段路由 | **成立（且比 R6 更简）** | 见上：用 `r["decision_id"]`，不需要新增注入 |
| R6-2 只做一个试点 | **成立** | 但去掉 `sleeves.enabled` 后必须补“回滚定义”（§17.3） |
| R6-3 单点 `policy_for` | **成立** | 建议放在 `app/playbook/`（不是脚本），入场/退出/EOD 三处共用，且带 `DA_DEFAULT` 逐字分支 |
| R6-4 参数放指令/policy、不碰 YAML | **部分不成立** | 见 §17.2-1：**reprice 会覆盖写入方给的 SL/TP/size** |
| R6-5 出场先做最小闭环 | **成立** | 但必须写死“P3 只判管道”，不得把 pilot 读数当 momentum 策略结论 |
| R6-6 统计先查询后报表 | **成立** | — |
| R6-7 过度设计删除清单 | **成立** | — |

### 17.2 三个必须在 P2 前解决的实现陷阱

1. **reprice 会覆盖写入方的 SL/TP/size（R6-4 的硬伤）**
   `auto_execute_flow.py:447-560` 在触发瞬间取实时价重算几何，`:806-829` 把 `_reprice_params`（`entry_price/stop_loss/take_profit/position_size`）传给 executor 并回写 DB。SL 来源是 `_hp = pb_cfg.get_horizon_params(inst.horizon)`：
   - horizon 无对应块 ⇒ `logger.warning` + **固定 fallback**（`buy_fixed_stop_loss_pct 2.5` / `sell 2.0`）；
   - horizon 有块 ⇒ `vol_buckets` 的 ATR 倍数。
   ⇒ “SL/size 由写入方给、不碰 `playbook_horizon.yaml`”在**执行跳**不成立。三选一，必须预注册：
   - **(a)** 加 `swing_horizon` 块（最简单；代价是 `exit_config_version` 断点，需 §1.3 的落档）；
   - **(b)** 把 policy 分支延伸到 reprice（即 S1 扩为 S1a 触发 + S1b 定尺），代码面更大；
   - **(c)** 接受 DA 的 ATR 定尺（那“固定 3% / 3×ATR”就作废，滑向 long_horizon 语义）。

2. **“不碰 YAML”≠“拿到干净几何”**
   `_horizon_cfg = {}` ⇒ `max_hold_days` 兜底 `1`（`exit_engine.py:439`）、`overnight_gap_stop_pct` 兜底 3.0；`_exit_cfg` 落 `factor_exit_rules.default`（`breakeven_at_pct 0.4` / `trailing_enabled true` / `time_exit_hours 6.0`）。⇒ swing 分支必须**自带完整配置字典**（max_hold/gap/BE/trail/time_exit 全显式），不能靠“没配所以不生效”——那是本仓“静默回落默认值”的典型事故面。

3. **总闸清零在 exit_engine 内部，不在 monitor**
   `exit_engine.py:409-410` 与 `overnight_cleanup.py:170-173` 都会在 `overnight_hold_enabled=false` 时把 `_skip_allowed` 抹成 False。只改 monitor 注入**无效**；S3 必须把这两处门条件做成 policy-aware（`da` 分支逐字不变，`sleeve` 分支跳过总闸），否则 21 日持有根本到不了。

### 17.3 两个流程建议

4. **demo 的影子产物要清**：`_symbol != "TEST"` 是 symbol 级守卫，真实 ETF 的 demo 会照写 `data/shadow/winner_overnight_shadow.jsonl`、`early_dead_signal_shadow.jsonl`、`reentry_tp` 等。demo 前先列出“会产生的影子文件”，事后删除或打标，否则这些结算脚本会拿到一笔假样本（比 A9 污染更隐蔽）。
5. **回滚定义（R6-2 去掉开关后必需）**：kill switch = 停写入方 + `policy_for` 回退（`MOM-ETF-V1` → `DA_DEFAULT`）；**存量仓怎么办必须预注册**——“按 max_hold 走完”或“人工平”，不能临场定。建议前者（与策略语义一致，且不产生额外成交）。

### 17.4 建议的最终形态（一句话）

**本月做 A1/B（口径 + 可选 demo）；A3 先写 RFC 的实现节（把 §17.2 三条与 R6-4 的二选一钉死），再谈 P2。** 不要在同一次变更里同时上“新 horizon 块 + policy 分支”——那会把 `exit_config_version` 断点与几何变更混在一个 cohort 里，事后无法归因。

### 17.5 本轮对本文的修改

- §1.1：补 monitor 注入 / cohort 随机 / 总闸清零（R5-02 + 本轮发现）。
- §2.3：修正“复用 long_horizon 当零代码持夜杠杆”（不成立）。
- §6.3 / §7.1：明确 `-TEST` 不自动隔离，必须人工写 `exclusion_reason` 并回读。
- §4.6 / §9 / §12 / §15.5：费用精确值回填 7.686bps（8 股）。
- 新增本节 §17。

## 18. R8 最小实施裁决建议（2026-11-12；复核 R6/R7 与执行侧预算/归因）

> 角色：方案复核与最小化收敛；追加独立链路复核，并在真文件核对 reprice、预算查询与归因兜底。
> 性质：**待用户裁决的实施建议，不是 A3/C1 名额、预算参数或 SIM 启用的批准**。本节只追加新结论，不改写 R6/R7 历史。
> 结论：若原始目标仍是“保留 DA、复用 Playbook、独立编号/预算/退出、统计不混淆，并在 Futu SIM 看见一条真正非 DA 的策略线”，建议选择一个 **`MOM-ETF-V1` 的 A3 完整垂直切片**。若本月治理门不批 A3，则只做 record-only；不要用 B/A1 或一次性 demo 冒充该目标已经实现。

### 18.1 对 R6/R7 的五项修正与收敛

| # | 建议 | 理由 / 代码证据 |
|---|---|---|
| R8-01 | **禁止用 `policy_for: MOM → DA_DEFAULT` 回滚存量仓**；开关只禁止 MOM 新仓执行，存量仓始终按原版本 policy 退出 | §17.3-5 的“回退 DA_DEFAULT + 存量按 max_hold 走完”机械上不可兼得：落回 DA 后，`exit_engine.py:409-410` 与 `overnight_cleanup.py:167-173` 的隔夜闸会让存量仓当日退出，走不到 max_hold |
| R8-02 | **执行侧 sleeve 子帽是正式试点前置项，但不是新预算器** | 原目标明确“独立预算”；且 `auto_execute_flow.py:454-587,806-829` 会在执行跳重算并覆盖 SL/TP/size，写入方自定限额不是硬约束。全局帽查询（`execution_flow.py:78-90`）只限制组合，不提供 sleeve 独立预算 |
| R8-03 | **S4 归因腿是正式试点前置项；独立绩效仍先用前缀查询** | `source_attribution.py:7-13` 的 `ELSE 'DA自选'` 会把 MOM 指令并入 DA 日/周漏斗。可按 `trade_records.decision_id` 查询 ≠ 现有报表已隔离 |
| R8-04 | **首版策略定义为固定持有 21 个交易日，不做排名替换退出** | §16-5 若只留 SL + max_hold，就不是完整“月频轮动”。将假设直接冻结为“动量选股 + 固定 21 交易日退出 + 初始 SL/gap 保护 + TP=0”，可省去排名退出且语义完整；若坚持月频轮动，则排名/调仓退出不能后置 |
| R8-05 | **正式路径跳过 P0.5 手工 demo** | 直接 `execute_instruction` 不验证 scheduled trigger/reprice/执行侧预算，还需人工 exclusion 与影子产物清理。首笔正式 SIM 试点天然提供 App 可见性，无需另造一笔待清洗成交，也不改 remark |

与 §17.4 的差异：R8 不把 A1/B 当作原始目标的正式实现，只把它们视为“治理未批 A3 时的降级观察”。工程最小化不能以牺牲独立预算、独立退出和统计隔离为代价。

### 18.2 推荐的最小实施契约

1. **一个版本化 policy**：新增单点 `policy_for(decision_id)`，只特殊识别 `MOM-ETF-V1-*`，其余前缀逐字走现有默认路径。`decision_id=MOM-ETF-V1-{YYYYMMDD}-{symbol}`，重跑复用同一 ID。第二个策略出现时再抽象，不做 REV/ETF 占位或通用 sleeves 框架。
2. **不碰 `playbook_horizon.yaml`**：选择 §17.2-1 **方案 (b)**，把同一 policy 分支延伸至 reprice。MOM 的 entry/SL/size 由 V1 显式规则在触发时重算，TP=0；不得落入未知 horizon 的固定 2.5%/2.0% 回退。DA `exit_config_version` 应保持不变。
3. **一个写入方**：冻结现有历史足够的 ETF 小池、只多、top 2，使用前一交易日收盘数据；只调 `save_instruction`，并用带 `candidate_screening.candidate_id` 与 `source` 的 `confirm_saved` 回读。复用现有调度、报价、admission、全局风控、执行和对账，不新增 DB/表/source_type/broker/调度器。
4. **S1a scheduled 触发**：09:50 ET 后执行；保留报价新鲜度、chase cap、同票互斥及全局安全闸，DA 缺省仍调用原 breakout 函数。RFC-141 等在采实验须按 `decision_id` 排除 MOM 或预注册独立桶，不得混入 DA cohort。
5. **S1b 执行侧独立预算**：首版只需“最多 2 仓 + 单腿名义上限 + 当日名义上限”三项硬约束，在 reprice/fee-floor sizing 后、下单前检查。费用可行股数若超过任一帽则跳过；不得为摊费抬仓或突破既有单名/全局帽。
6. **S2+S3 独立退出**：monitor 直接用已查询的 `decision_id` 路由。V1 只运行初始 SL、隔夜 gap 保护、21 个交易日到期退出；排除 DA 的 early-tight/dead-signal/BE/trailing/PRE_CLOSE/EOD_FORCE_CLOSE 等规则。`exit_engine` 与 `overnight_cleanup` 两处总闸都须 policy-aware，全局 `overnight_hold_enabled` 继续为 `false`。policy 自带完整参数，禁止依赖 DA 默认值兜底。
7. **最小统计隔离**：`source_attribution` 增加 `MOM-ETF-V1-%` CASE 腿与 label；成交绩效先按 `trade_records.decision_id` 分桶查询，DA / MOM / 整体含 MOM / 整体不含 MOM 并列，不新建绩效系统。
8. **单一新仓开关**：建议 `mom_etf_v1_entries_enabled=false` 只在入场执行处禁止 MOM 新仓；写入方可继续 record-only。开关状态不得改变 `policy_for` 对已有 V1 编号的解析，紧急人工平仓另走既有流程，不与普通回滚混用。

### 18.3 最小落地顺序

| 阶段 | 内容 | 行为边界 |
|---|---|---|
| R8-P0 | 独立 RFC：用户裁 A3/C1 名额、固定 21 日语义、费用尺与三项预算；随后建一个 record-only 写入方并按 G2/G3 注册 | 不下单；不采集未注册影子 |
| R8-P1 | 在 `mom_etf_v1_entries_enabled=false` 下落位 policy、S1a/S1b、S2/S3、执行侧预算和 S4 归因；完成 §18.4 golden | 代码可分小批，启用前零 MOM 交易行为；共享代码生效仍须按仓规核 WIP/在途出场、统一重启与启动冒烟 |
| R8-P2 | 用户批准后启用 SIM，首批最多一个调仓簇、两笔仓 | 只判 `PLUMBING_OK / PLUMBING_BROKEN`；不判 alpha，不放大 |

代码接缝可以分别开发和回归，但对外行为只启用一次“完整的 V1 策略 cohort”，避免先后开出 A2/A3 混合样本。

### 18.4 启用前 golden / 契约测试

1. **DA 零漂移**：缺省及显式 DA 的触发、reprice、退出原因/价格、EOD 结果与改动前一致，`exit_config_version` 不变；EXT/topup 等共享出场路径同步回归。
2. **定尺与预算**：MOM reprice 后 SL/size/TP=0 来自 V1，无 2.5%/2.0% 或 DA factor 默认回落；fee-floor sizing 后超子帽会拒单且不抬仓。
3. **多日退出**：固定价格路径下无 DA 早退、PRE_CLOSE 或 EOD 强平；初始 SL/gap 保护与 21 个交易日退出按预期触发。
4. **回滚与重启**：关闭新仓开关后无 MOM 新成交，存量 V1 仓仍按 V1 管理；重启恢复后 policy 不变，禁止回退 DA_DEFAULT。
5. **归因与实验隔离**：MOM 不落 `DA自选`，旧前缀归属不变；RFC-141 等 DA cohort 无 MOM；整体含/不含 MOM 可对账。
6. **可见性与账实**：首笔正式 SIM 订单/成交/持仓可由现有链路旁证，订单、`trade_records.decision_id`、持仓和退出闭环一致；App UI 另由用户目检，不以代码发送 remark 代替 UI 验证。

### 18.5 不做项与证据边界

- 不做通用多 sleeve 配置框架、REV/ETF 占位、全量 metadata 透传、新 source_type/owner、App remark 改格式、新预算器或新绩效系统。
- 固定 21 日退出下不做排名替换；不扩 ETF 票池、不为工程试点打开 ETF 1m 订阅。若转为收益研究，PIT/历史/真实成本另按 sibling DEB 前置，不与管道试点混批。
- H2 long-only 虽有研究读数，但票池非 PIT、alpha vs QQQ 的 t=2.24，仍属 GRAY（`DEB-20261112-beyond-da-strategy-research.md` §4.3）。R8-P2 只证明链路与策略语义可运行，**不得宣称已找到新 alpha 或解决 DA 赢小输大**。
- 本节仍待用户裁：A3 与 C1 名额、固定 21 日假设、具体预算/费用尺及 SIM 启用日。未获裁决前维持 record-only / 零交易行为。

本轮只读核验 `auto_execute_flow.py`、`execution_flow.py`、`source_attribution.py` 及 R7 所列退出锚点；本批仅追加本文档，零代码/配置/DB/cron/推送/影子注册变更，零重启、零下单。

## 19. R9 共识汇总与第一层落地验证（2026-11-12 04:15 CST）

> 结论：**可以汇总共识，也可以直接落地“验证层”；但不能把未批准的 A3/C1/预算/SIM 启用直接当成已批准。** 本批已落地一个未接线的单点 policy 路由模块 + 契约测试，零交易行为、零配置变更、零重启。

### 19.1 通俗易懂的调整设计逻辑

把 DA/Playbook 想成一家老店：收银、仓库、保安、账本都已经有。现在不是盖新楼，而是加一个**挂牌实验柜台**：

1. **牌子**：`MOM-ETF-V1-{date}-{symbol}`。牌子随指令和成交落库，统计按牌子分桶，不混 DA。
2. **规则卡**：`policy_for(decision_id)` 只认这一个牌子；其他所有牌子照旧走 DA。规则卡集中一处，避免字符串散落。
3. **老店规则不动**：DA 的触发、定尺、退出、预算、对账全部复用；试点只在必须改变语义的地方分支。
4. **先验证最小闭环**：scheduled 入场、TP=0、初始 SL、隔夜 gap 保护、21 个交易日到期退出；排名替换/扩票池/通用框架后置。
5. **开关只管新仓**：未来 `mom_etf_v1_entries_enabled=false` 只禁止新 MOM 仓；存量 V1 仓仍按 V1 规则退出，不能回退 DA_DEFAULT。

### 19.2 收益：不是承诺利润，而是买可验证的信息

- **保留 DA**：DA KR 仍只算 `DA-%`，试点不掩盖 DA 正负。
- **降低混账/事故面**：不新建 source_type/broker/退出循环/预算器/绩效系统；复用现有安全闸。
- **针对“赢小输大”的机制**：减少 DA 日内 BE/早退/锁利对赢家的截断，用初始 SL/gap/max_hold 控制左尾；是否改善盈亏比必须前瞻验证。
- **费用可控**：整数股尺子已复算，QQQ 7 股≈8.742bps、8 股≈7.686bps；不得为摊费抬票面。
- **统计诚实**：月频/21 日持有样本慢，P2 只判 `PLUMBING_OK/BROKEN`，不宣称 alpha；收益结论需 PIT/真实成本/调仓簇 MDE。

### 19.3 本批已落地验证

- 新增 `app/playbook/pilot_policy.py`：`policy_for()` 单点路由；`MOM_ETF_V1` 固定 21 交易日、TP=0、exit allowlist=`INITIAL_SL/OVERNIGHT_GAP_STOP/MAX_HOLD`；`mom_etf_v1_decision_id()` 稳定编号。
- 新增 `tests/playbook/test_pilot_policy.py`：DA/EXT/UE 缺省不变、V1 前缀命中、allowlist 排除 DA 日内修复、开关与 policy 解析分离、前缀精确匹配。
- 验证：`./venv/bin/python -m pytest -q tests/playbook/test_pilot_policy.py` = **6 passed**；`py_compile` OK；模块未被 live 路径 import，未接线。
- 只读 readiness：`/tmp/deb_sleeve_review/verify_r9_readiness.py` / `.json`；sha256 见 handoff。关键读数：`policy_for` 已存在但未接线；`mom_etf_v1_entries_enabled` 未配置；`swing_horizon` 未新增；`source_attribution` 尚无 MOM 腿；MOM 成交 0；注册表 106 项；A9 仍不自动排除 `-TEST`。

### 19.4 可以直接做什么，必须批准什么

**现在可直接继续**：独立 RFC 草案、golden 测试计划、默认关闭的垂直切片开发、record-only 写入方设计（注册前不采集）。

**SIM 新仓启用前必须用户裁**：A3/C1 入场假设名额、固定 21 交易日语义、三项预算/费用尺、KR4 注册表占位、启用日。未裁前维持零 MOM 交易行为。

### 19.5 本批不做

未接 auto_execute/exit_engine/overnight_cleanup；未改 YAML/DB/cron/推送/影子注册；未重启；未下单；未补 ETF 数据/PIT；未验证富途 App UI；未跑全仓测试。

## 20. R10 七条防误判约束采纳与共识冻结（2026-11-12 04:30 CST）

> 角色：独立复核与共识收口。用户转贴的七条建议**全部采纳**。本节覆盖 §4.1/§4.2/§4.5/§19.2 中过强或易误读的表述；历史轮次保留不改写。
> 总结论：**V1 首先是工程试点，不是收益证明；DA 主链不改；“管道跑通 / 出场几何改善 / 入场有 alpha”必须分三层取证，不能共用一次 SIM 试点下结论。**

### 20.1 三层证据必须分开

| 层 | 要回答的问题 | 允许的成功判据 | 禁止的误读 |
|---|---|---|---|
| L1 · PLUMBING | 指令写入、定时触发、reprice、预算帽、持夜、V1 退出、DA 零漂移、编号/报表/重启恢复/回滚是否正确 | `PLUMBING_OK / PLUMBING_BROKEN` | 平均盈利、MFE 捕获率、净 bps 只能作观察读数，不能判策略有效 |
| L2 · EXIT_GEOMETRY | 在**同一批冻结 MOM 入场**上，只改变一个出场参数，净期望/尾损是否改善 | 预注册的配对净 R/净 bps/调仓簇 Δ + 尾损约束 | 不能拿 DA vs MOM 的直接差异归因于出场；不能同时开多个同票 SIM 策略臂 |
| L3 · ENTRY_ALPHA | 冻结出场几何后，动量过滤/票池/现金纪律是否相对基准有净 alpha | 预注册基准比较 + PIT/真实成本/调仓簇 MDE | 正收益不能只因为权益 beta 或 QQQ 趋势过滤；ETF 小池不是已证明 alpha 票池 |

DA 保留是贯穿三层的 G0 约束：DA 触发、定尺、退出、预算、对账、KR 口径零漂移；MOM 不得混入 `DA-%`。月频 top 2 每年约 12 个调仓簇，短期无论盈亏都大概率功效不足。

### 20.2 单变量顺序与基准

1. **冻结同一批 MOM 信号**：记录信号日、票池版本、入场价/股数、初始 R、数据版本与现金过滤状态。
2. **先做出场反事实**：离线回放或 record-only，只改变一个出场参数；不得把 3% SL 与 ATR SL 试完后挑表现好的称为 V1。
3. **出场几何冻结后，再测入场变量**：动量过滤、票池、现金纪律分别预注册，不与出场参数同批改。
4. **生产 SIM 只跑一个已批准版本**：不在同一票上同时开多个策略臂；反事实留在离线/record-only。

L3 基准至少并列：

- 四只 ETF 等权买入持有；
- QQQ 买入持有；
- 相同时段、相同现金过滤条件下的简单基准（例如 QQQ MA200 过滤后的持有/现金）。

没有这些基准，MOM 的正收益可能只是市场 beta，不是选择能力。

### 20.3 swing 诊断必须 horizon 对齐

DA 的 `MFE<0.3%` 死单率是日内语义，不能直接搬到 21 日策略。V1 诊断改为：

| 诊断 | 口径 | 用途 |
|---|---|---|
| 入场后 1/5/10 日 MAE/MFE | 统一用初始风险 R 表示；多头初始 R = entry − initial SL | 看早期波动与路径，不是新退出门 |
| 前 5 日触发初始 SL 比例 | 按实际触发/成交，不用事后 MFE 选样 | 判断入场是否过早错 |
| 到期前从未达到 +1R 比例 | 21 个交易日窗口内 | 判断右尾是否被截断 |
| overnight / intraday 收益分解 | 同一批入场、同一价格基准 | 判断收益来源，不混 DA 日内口径 |

这些只作诊断，**不得偷偷变成“5 日不动就退出”的新门**。任何新退出门都必须另走预注册与单变量验证。

### 20.4 出场评价必须看净期望与尾损

平均盈利变大不等于策略变好。必须合并胜率、平均亏损与成本：

`E = p × AvgWin − (1 − p) × AvgLoss − Cost`

至少并列报告：

- 净 R/笔、净 bps/笔、调仓簇净收益；
- 盈亏比、胜率、净期望；
- MFE 捕获率与 MAE（不能只看捕获率）；
- p90/p95 尾损、预期短缺 ES、最大回撤；
- `INITIAL_SL / OVERNIGHT_GAP_STOP / MAX_HOLD` 分别贡献多少损益；
- 相对等权 ETF、QQQ 与现金过滤基准的净 alpha。

**gap 保护表述收敛**：现有 `OVERNIGHT_GAP_STOP` 是历史命名，代码判据是“持仓过夜且亏损 ≥ 阈值”，并不真正校验跳空；它只能在跳空后限制后续损失，无法消除已经发生的 gap，实际成交也可能显著差于触发价。V1 若沿用该规则，只能称“隔夜亏损阈值兜底”，不得宣称消除左尾。

另：V1 初始 SL=3%、隔夜亏损阈值=5% 时，现有优先级下 3% SL 通常先触发；5% 腿更多是兜底/语义兼容，不应预设它会产生独立保护收益。收益归因必须按实际 `exit_reason`，不得按名称想象。

### 20.5 V1 读数前必须冻结的细节

| 项 | V1 草案冻结值/规则 | 当前状态 |
|---|---|---|
| 票池 | `US.QQQ / US.XLE / US.XLRE / US.XLY`，版本 `etf_v1_20261112`，top 2，只多 | 草案；PIT/复权/分红/时间戳未补 |
| 信号 | 12-1 momentum = `close[t-21] / close[t-252] − 1`，只用前一交易日收盘 | 草案；不得当日收盘前视 |
| 现金纪律 | QQQ `close > MA200` 才交易；否则空仓/现金 | `pilot_policy.py` 常量；未接线；不得为攒样本放宽 |
| 调仓日历 | 从 RFC 预注册锚点起，每 21 个 NYSE 交易日一次；节假日不计入 | 锚点日期未批准；未写死不得启用 |
| 缺数据/闸门拦截 | 当次调仓跳过并留痕，不补跑、不顺延；下一仍按锚点 +21 交易日 | 规则冻结；写入方/哨兵未实现 |
| 指令有效期 | `same_day_only`；仅当日 09:50–14:00 ET 有效，未成交/被拦即作废 | 常量；执行侧未实现 |
| 权重 | `equal_notional`；V1 不引入 inverse ATR | 常量；未接线 |
| 初始 SL | 固定 3.0%；管道 V1 不依赖实时 ATR | 常量；reprice 分支未实现 |
| 止盈 | TP=0 / disabled | 常量；未接线 |
| 隔夜亏损阈值 | 5.0%，沿用现有“过夜亏损阈值”语义，不宣称真实 gap 判定 | 常量；policy-aware 退出未实现 |
| 最大持有 | 21 个交易日到期退出；不做排名替换退出 | 常量；日历边界见 §20.8 |
| 出场 allowlist | `INITIAL_SL / OVERNIGHT_GAP_STOP / MAX_HOLD` | 常量；未接线 |
| 费用尺 | 7/8 股 QQQ 只是成本测算，不是择股或放大仓位理由；费用可行尺寸超预算就跳过 | 预算未批准；不得抬票面摊费 |
| 新仓开关 | `mom_etf_v1_entries_enabled=false` 只禁新 MOM 仓；存量 V1 仍按 V1 退出 | 常量名存在；YAML/执行未接 |

`pilot_policy.py` 当前只是**规则卡常量与路由函数**，不是执行生效。常量冻结 ≠ 交易行为冻结；启用前仍须 RFC、golden、预算与用户批准。

### 20.6 ETF 风险描述收敛

ETF 能降低单公司财报、诉讼等特异事件风险，但不能直接宣称“隔夜左尾更低”：

- QQQ 有集中度与系统性风险；
- XLE/XLRE/XLY 仍受商品、利率、行业政策影响；
- 四选二意味着组合持有池的 50%，横截面排名容易不稳定；
- 这四只 ETF 适合作为低冲突工程试验田，不是已证明更优的 alpha 票池。

正式研究必须确认复权、分红、时间戳、可交易价与真实成本口径。§4.1 的“避免个股隔夜跳空左尾”只能理解为降低单公司特异风险，不能理解为已证明组合左尾更低。

### 20.7 成功表述必须预注册

“平均盈利明显变大”“尾损没有失控”都过于主观。每层读数前必须写清：

| 字段 | L1 管道 | L2 出场反事实 | L3 入场 alpha |
|---|---|---|---|
| 对照基线 | 改动前 DA golden / 现有安全闸 | 同一批冻结入场的 V1 出场 | 等权 ETF / QQQ / 同现金过滤基准 |
| 主要指标 | 管道断言通过/失败 | 配对净 R 或净 bps/调仓簇 | 净 alpha/调仓簇 |
| 效应量 | 不适用 | 读数前填数值 | 读数前填数值 |
| 所需样本 | 首个调仓簇 + 重启/回滚演练 | 按配对差值标准差算调仓簇数 | 按基准差值标准差算调仓簇数 |
| MDE80 | 不适用 | `2.802 × sd(cluster_delta) / sqrt(n_clusters)` | 同式，按基准差值 |
| 决策日期 | 首簇结束后固定日期 | 预注册日期 | 预注册日期 |
| 尾损/回撤约束 | 安全闸/账实一致 | p95/ES/maxDD 不恶化超过预注册边际 | 同左 |
| 多重比较 | 不适用 | 一次一个主参数；其余描述性 | 多过滤/多票池须预注册族与校正 |

**样本不足时，无论净收益为正、接近零还是为负，都只能写“功效不足/不可分辨”**；不能只在结果不好时才援引样本不足。未填效应量/所需簇数/决策日期 = 未预注册，不得读数裁决。

### 20.8 R10 新增实现陷阱（P2 前必须 golden）

1. **21 交易日到期不能静默失败**：`overnight_decision.horizon_max_hold_hit()` 调 `nth_trading_day_after("US", entry_date, max_hold_days)`，后者默认 `max_days=30`。21 个交易日遇节假日可能超过 30 个自然日，返回 `None` 后 `MAX_HOLD` 不触发。V1 必须显式给足日历搜索窗口或用交易日区间计数，并加节假日 golden。
2. **EOD 兜底不能套 cohort B 5 日帽**：`overnight_cleanup.py` 当前用 cohort B 的 5 交易日逻辑；V1 必须 policy-aware 到 21 交易日，且全局 `overnight_hold_enabled=false` 不被打开。
3. **极端价格路径不能关掉保护**：`exit_monitor_flow.py` 对 `abs(current-entry)/entry > 15%` 直接跳过本轮退出评估。21 日 ETF 可能超过该阈值；V1 不能盲继承此 DA 异常闸，至少要有 policy-specific  sanity bound/告警/人工兜底，避免“越跌越不评估”。
4. **allowlist 必须真正短路 DA 规则**：不是只设 `_skip_allowed=True`。early-tight、cooldown、BE、trailing、dead-signal、MFE stall、PRE_CLOSE、EOD_FORCE_CLOSE、W_WEEK 豁免都不得作用于 V1；DA 缺省路径逐字不变。
5. **reprice 必须 policy-aware**：`auto_execute_flow.py` 会覆盖写入方 SL/TP/size；未知 horizon 会落 DA 固定 2.5%/2.0% 回退。V1 的 3% SL/TP=0/等名义尺寸必须在触发重算后仍成立。
6. **归因与测试隔离不能靠后缀想象**：`source_attribution.py` 未知前缀落 `DA自选`；A9 只排除成交 `metadata_json.exclusion_reason`，`-TEST` decision_id 不自动排除；`exit_engine` 影子守卫是 `_symbol != "TEST"`。正式 V1 用真实 ETF 时不得产生未注册影子样本。

### 20.9 最小落地共识与当前边界

**可以落地**：未接线 policy 常量、契约测试、只读 readiness、独立 RFC 草案、golden 计划、record-only 反事实设计。

**不能直接落地**：SIM 新仓启用、预算数值、调仓锚点、KR4 占位、A3/C1 名额。它们仍须用户批准；未批准前维持零 MOM 交易行为。

当前已验证：`pilot_policy.py` 只识别 `MOM-ETF-V1-`，DA/EXT/UE 缺省返回 `None`；V1 常量冻结 21 交易日/TP=0/3% SL/5% 隔夜阈值/等名义/当日有效/QQQ MA200；7 条契约测试通过。当前未验证：S1a/S1b/S2/S3/S4、golden 全量、record-only 写入方、ETF PIT/分红/时间戳、富途 App UI、SIM 启用。

一句话共识：**保留 DA 老店，只加一个挂牌柜台；先证明柜台能正确收银、持夜、退出、记账，再在同一批进货上研究怎么卖得更好，最后才和别的店比是否真有选股能力。**

## 21. R11 用户批准后的实施记录（2026-11-12 04:55 CST）

> 用户裁决：「批准执行，明日交易日验证」。本批从 R10 共识进入实施：默认关闭切片 → 重启冒烟 → 入口开关开启。仍只走 SIM，四锁不动，不接实盘。

### 21.1 已落地

- `app/playbook/pilot_policy.py`：V1 规则卡扩展为候选/日历/定尺/预算/退出/执行分支；`policy_for` 仍只认 `MOM-ETF-V1-`。
- `scripts/mom_etf_v1_generate.py`：调仓日写入方，只调 `save_instruction`；支持 `--dry-run`。
- `auto_execute_flow.py`：DA 循环前处理 MOM；MOM 执行后从 DA 指令列表过滤，DA 缺省路径不变。
- `exit_monitor_flow.py`：MOM 行提前分支，只跑 V1 allowlist，更新 peak/MFE/MAE，不写 DA 退出影子。
- `overnight_cleanup.py`：SELECT 补 `decision_id`；MOM 行跳过 DA EOD 强平。
- `source_attribution.py`：新增 `MOM-ETF-V1` 报表腿与 label。
- `config/unified_config.yaml`：`playbook.mom_etf_v1_entries_enabled` 冒烟后由 false → true。
- crontab：备份 `data/crontab.backup.20261112_rfc199.txt`；新增 RFC-199 写入方行（CST 22:35 周一-五，脚本内再核锚点/21 交易日/现金过滤）。
- `docs/decisions/RFC-199-mom-etf-v1-sim-plumbing-pilot.md`：用户批准、冻结参数、L1 判据、回滚。

### 21.2 验证

- `./venv/bin/python -m pytest -q tests/playbook/test_pilot_policy.py tests/playbook/test_mom_etf_v1_flow.py` = **15 passed**。
- `py_compile` 覆盖 pilot_policy / writer / auto_execute / exit_monitor / overnight_cleanup / source_attribution，exit 0。
- `systemctl restart ftseek-us` 后 `scripts/startup_smoke_check.py --wait 90` = **7 项通过**；锚点由 11/12 校准为 11/13 后二次重启复跑通过。
- 已知无关失败：`tests/test_review_source_attribution.py::test_weekly_signal_funnel_reports_source_direction` 报 `no such column: row_kind`，属现有 weekly_review/测试表结构 WIP，不在本批修复。

### 21.3 下一交易日验证口径

- 锚点：`2026-11-13` ET（北京时间 11/13 22:30 开盘）；写入方 CST 22:35 运行，auto_execute 09:50 ET 后才执行。
- 若 QQQ 收盘 ≤ MA200，V1 正确空仓；不得为攒样本放宽现金过滤。当前本地 11/10 收盘 QQQ 674.76 < MA200 680.34，锚点日是否交易取决于 11/11 收盘刷新。
- L1 只看：指令写入、定时触发、reprice 后 3% SL/TP=0、子帽/全局帽、持夜、V1 退出 allowlist、DA 零漂移、编号/报表、重启恢复、回滚。
- 平均盈利/MFE/净 bps 只作观察；样本不足一律“功效不足/不可分辨”。

### 21.4 回滚

- 停新仓：`playbook.mom_etf_v1_entries_enabled=false`（热重载）；存量 V1 仍按 V1 退出，不回退 DA_DEFAULT。
- 停写入方：删除 crontab RFC-199 两行；紧急人工平仓走既有 Playbook/DualBroker 流程。

### 21.5 未验证

- 真实 SIM 订单/成交/持夜/21 交易日退出尚未发生；富途 App UI 未目检；full golden 未跑；ETF PIT/复权/分红/时间戳未补；L2/L3 收益验证未开始。

## 22. R12 指令生成方与入场质量边界（2026-11-12 05:05 CST）

> 用户追问：指令方谁来生成？是什么逻辑？是否基于 LLM 辩论？需要有合理、有质量的入场指令和方向。

### 22.1 当前事实：MOM-V1 是机械写入方，不是 LLM 辩论

- 生成方：`scripts/mom_etf_v1_generate.py`，由 crontab 在调仓日运行，只调 `save_instruction` 写入 `playbook_instructions`。
- 逻辑：前一交易日收盘 → 四 ETF 12-1 动量排名 → QQQ `close > MA200` 现金过滤 → top 2 → 只多 BUY → 等名义金额 → 3% 初始 SL / TP=0 / 21 交易日退出。
- `confidence=100` 是管道常量，用于通过既有写入/执行口径，**不是 LLM 质量分，也不是胜率预测**。
- DA 主链仍是 LLM deep_analysis/UE 供料 + voting/confirm + 盘中触发；MOM-V1 刻意不混入 DA 的 LLM 人群，避免混轴。

### 22.2 质量不等于“有 LLM”

- DA 已有 LLM 分析，但实测仍是高胜率、负偏斜、费用/尾损侵蚀；LLM 辩论不自动等于正期望。
- V1 当前质量保障只有：数据新鲜度、前收盘无前视、QQQ MA200 现金纪律、费用/预算帽、当日有效、DA 零漂移。
- V1 是工程试点，不是已证明 alpha；“合理”指可解释、可复现、可归因，“有质量”必须靠净期望/尾损/基准比较证明。
- 方向只多/现金，不做空：空头腿在既有探测中未证明独立 alpha，且借券/short pool 会引入新变量。

### 22.3 可加的质量层（不改变 V1 行为）

| 层 | 内容 | 边界 |
|---|---|---|
| Q1 · record-only 质量档案 | 动量排名、动量广度、MA200 斜率、波动率、回撤、相关性、fee bps、初始 R、相对基准动量 | 只写 metadata/报表，不新增门 |
| Q2 · LLM advisory 辩论 | 宏观/行业/拥挤度/事件风险，输出结构化评分 + 证据 + 反方意见 | 只作旁路建议，与机械基线对比；不直接改 V1 入场 |
| Q3 · V2 入场假设 | 若 Q1/Q2 预注册增量 IC/净期望达标，再单变量 RFC | 不与出场几何同批改；不占多个 C1 |

### 22.4 禁区

- 不把 LLM confidence/辩论票数当质量判据；必须看费后净期望、尾损、基准 alpha。
- 不用事后 MFE/盈亏选样来“提高质量”；不为攒样本放宽 QQQ MA200。
- 不在 V1 管道试点中偷偷加入 LLM 硬门；若要做，先 record-only + 预注册 + 用户批准。

## 23. R13 独立复核：P0 属实、修复、重新启用（2026-11-12 06:10 CST）

> 角色：独立复核 + 修复 + 部署后 readiness。用户转贴的 P0 复查结论逐条核实：**5 项 P0 均属实**；R11 的“已接线但入口开启”当时确实不宜判 `PLUMBING_OK`。本轮先修复，再重启冒烟，再重新开启入口与写入 cron。

### 23.1 P0 确认与修复

| # | 复查结论 | 核实 | 修复 |
|---|---|---|---|
| P0-1 | 写入方可能把仲裁失败误报 saved；as-of 只查 `< trade_date` | 属实 | `mom_etf_v1_generate.py` 接 `confirm_saved`，saved/rejected/unknown 分列，非 saved exit 1；asof 必须等于上一交易日；decision_id 统一 YYYYMMDD |
| P0-2 | MOM 专用分支绕过报价年龄/chase/冷却/TCA；子帽缺全局购买力 | 属实 | 删除 pre-loop 专用执行函数，MOM 收回 DA 主循环；共享 `_reprice_quote_age_gate`、chase、冷却、admission、TCA；`mom_budget_reject_reason` 补 `GLOBAL_BUYING_POWER_CAP` |
| P0-3 | REDUCE_50/观察期缩仓后费用/最小名义可能失效 | 属实 | `playbook_service.py` 在所有风险乘数之后、下单之前按最终股数复核 fee/min-notional/MOM 日帽；不满足拒单；LIVE 拒绝 |
| P0-4 | `INITIAL_SL` 错桶；3% SL 先判导致 5% gap 不可达；日历失败静默取消 max_hold | 属实 | allowlist/退出统一 `SL_INITIAL`；过夜先判 5% 再判 3%；日历失败 ERROR + 保守 `entry+29d` 兜底；entry_time 非法保守 `MAX_HOLD` |
| P0-5 | RFC-141 仍混入 MOM 成交 | 属实 | `settle_entry_confirm_forward.load_sim_pnl` 排除 `MOM-ETF-V1-%`；R13 再补 `load_fills` 同口径排除，防同票同日 join 污染 |

### 23.2 R13 额外收紧（不新增策略轴）

1. **执行时点复检**：MOM 在 `service.execute_instruction` 前再查 `entries_enabled` 与 `after_entry_cutoff`，防止 13:59 启动的长轮次把单下到 14:00 后。
2. **票池完整性 fail-close**：四 ETF 任一缺 bar/不足 253 根/asof 不一致 ⇒ 当次不产出候选，不用剩余 3 只偷偷改票池。
3. **动量索引冻结**：`asof` = 前一交易日（信号日）；12-1 = `close[asof-21] / close[asof-252] - 1`，等价于截至 asof 的 `bars[-22]/bars[-253]`，至少 253 根。该口径在读数前写死，不再事后解释。
4. **readiness 脚本**：`--run-tests` 失败时 exit 1；快照含代码哈希、开关、PID、cron、DB 计数、聚焦测试。

### 23.3 部署后证据

- 聚焦：`tests/playbook/test_pilot_policy.py + test_mom_etf_v1_flow.py + test_reprice_quote_age_gate.py + tests/execution/test_fee_model.py + test_fee_floor_sizing.py` = **63 passed**。
- 相邻：`test_offline_full_chain / eod_close_window_contract / overnight_decision_contract / chase_cap_enforce / execution_admission / reprice_audit_persistence / risk/test_exit_monitor` = **125 passed**。
- 全 `tests/playbook`（R13 初版）：**1189 passed, 5 failed, 3 errors**；失败/错误均为无关 WIP（live_probe_admission_exemption、exit_compensation 网络边界 teardown），不在本批修复。
- 重启：`systemctl restart ftseek-us`；启动冒烟 **7/7**；PID `2667199`，ActiveEnter `2026-11-12 06:07:11 CST`。
- 热重载：`mom_etf_v1_entries_enabled=true` 已加载；cron 写入方已挂回且仅 1 条。
- readiness：`data/readiness/mom_etf_v1_r13.json`，sha256 `ba8a78a98aba90921647f2b51edad2f5af247fe53bde1397055f133fc6bdfbdb`；MOM 指令/成交/持仓均 0；聚焦 22 passed。
- 四锁不动：`AUTO_TRADE_ACCOUNT=sim`、`ALLOW_REAL_TRADING=false`、`execute_mode=futu_simulate`、毕业门禁 fail-close。

### 23.4 明日验证口径（仍只 L1）

- 锚点 `2026-11-13` ET；写入方 CST 22:35（ET 09:35）运行，auto_execute 09:50 ET 后才执行。
- 若 QQQ 上一交易日收盘 ≤ MA200，或四 ETF 数据不完整 ⇒ 正确空仓/跳过，记 `PLUMBING_PENDING / CASH_GATE_OK` 或 `DATA_GATE_OK`，不判策略失败，也不放宽过滤。
- 若产出指令，L1 只看：写入仲裁、定时触发、reprice 3% SL/TP=0、最终股数费用、子帽/全局帽、持夜、V1 退出 allowlist、DA 零漂移、编号/报表、重启恢复、回滚。
- 平均盈利/MFE/净 bps 只作观察；样本不足一律“功效不足/不可分辨”。

### 23.5 未验证 / 不做

- 真实 SIM 订单/成交/持夜/21 交易日退出尚未发生；富途 App UI 未目检。
- ETF PIT/复权/分红/时间戳未完全验证；日 K 增量 QFQ 与历史重基线一致性仍是数据债。
- L2 同入场出场反事实、L3 基准 alpha 未开始；不得用本试点判策略有效。
- 不新增 LLM 硬门、不新增影子、不抬票面摊费、不放宽 QQQ MA200。

### 23.6 回滚

- 停新仓：`playbook.mom_etf_v1_entries_enabled=false`（热重载）；存量 V1 仍按 V1 退出。
- 停写入方：删除 crontab RFC-199 两行；备份 `data/crontab.backup.20261112_rfc199_r13_pre_enable.txt`。
- 紧急人工平仓走既有 Playbook/DualBroker，不与普通回滚混用。
