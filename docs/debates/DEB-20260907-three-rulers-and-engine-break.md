# DEB-20260907-three-rulers-and-engine-break: Claude 九条建议核查 → 三把只读尺子落地 + 官方回放引擎被打穿（计划外根因修复）

> **立案日**: 2026-09-07（周一，美国劳动节休市；下一交易日 9/8）
> **状态**: 已执行（三项只读交付物 + 一项计划外根因修复），**未动任何 live 路径**；裁决项见 §九
> **立案人**: 用户把 Claude 对 `docs/analysis/2026-09-07-core-chain-brief-for-claude.md` §10 九条自查疑点的回复贴回，令「请核查给出你的分析判断」→ 汇报后裁「按照建议执行落地」
> **执行批**: ① 配对反事实尺子（`scripts/exit_path_replay.py` 新建 + `scripts/cf_pullback_limit_replay.py` v2）② 客单价费率盈亏平衡账（`scripts/ticket_size_breakeven.py` 新建）③ 笨规则对照臂（`scripts/trivial_rule_control_arm.py` 新建）④ **计划外**：官方回放引擎 ImportError 根因修复 + 补钉子
> **关联**: brief §10-A…K（九条疑点原文）、§9.4「已确认失效的尺子」、ADR-035（`entry_archetype_routing_enabled` 预注册四判据）、RFC-071（`min_notional_usd` 3000→2500）、RFC-077（毕业门禁草案）、`docs/specs/multi-agent-collaboration-charter.md` §二 数据口径七律、`docs/REPLAY-SCRIPT-LEDGER.md`（官方引擎公约）、提交 `14e9a544`（P2a 引擎纯搬家）、提交 `bace63a6`（交易日守卫批）

---

## 一、原问题与本次核查结论

Claude 的核心判断是：**九条里第 9 条（净 edge 未被证明）其实是第 0 条**，在 edge 符号未定之前给信号调精度是无意义的；而真正**不依赖「信号有 edge」也能确定性获益**的只有第 6 条（语义鸿沟 = 实现落差），因为它可以用**配对样本**测，SNR 比任何横截面 PnL 判据高一个量级。其建议排序为 8 → 6 → 5 → 1 → 2 → 7 → 3/4 → 9，并额外指出四点框架外问题：`n≥30` 门槛对 PnL 类判据在数量级上不够、**没有对照臂**、**不知道信号的噪声底**、Phase1 price-in 确认门该提为一等公民。

**核查结论（同意 / 修正 / 反驳）**：

| Claude 的判断 | 核查 | 依据 |
|---|---|---|
| 判据终点应从 PnL 换成高 SNR 代理 | **同意，且本批就是落地** | 交付物③实测：浮现层收益类判据需 835~4152 个交易日才可分辨（§六），不是攒样能救的 |
| 没有对照臂是全系统最大方法论缺口 | **同意** | 交付物③补上；同时证明「补上之后仍然不可分辨」——缺口是真实的，但补上它得到的第一个结论是**反面**的 |
| 配对反事实是唯一几十量级就能出结论的设计 | **同意设计，但当前不可判读** | 交付物① n_paired=30 已达量级，却被保真度门拦下（`NOT_EVALUABLE_FIDELITY`，§三） |
| 费用/持有期量纲（brief §10-J）是确定性漏损 | **同意，并给出可换算的三个逃生门** | 交付物②：双地板公式 + 净值/帽/毛边际三者等价可换（§五） |
| 「保本漏武装」类病灶应先诊断再修 | **同意方法论，但诊断结果推翻了立案** | 本批只诊断不修（§四）。三组独立数据证明该病灶**不成立**：①实盘 `mfe_pct` 遥测健康（`mfe_valid_rate` 262/262=100%）②偏差是**双侧**的、重放整体偏保守（中位 −0.041pp）③真病灶是重放器把 `SL_TRAIL_LOCK` **84/101 笔误分类成 `SL_BREAKEVEN`**。并**否证**了流传的 15 笔 −$1,595.78 读数（穷举 7 种定义均不可复现） |
| 第 8 条（生效时点空窗）是当下缺陷、明天就撞劳动节 | **已由上一批解决，本批无需动** | 提交 `bace63a6`：DA 主链交易日守卫 + `trading_day_check.py` exit code 契约修复（一处救活全仓 9 个死假日分支） |
| 第 1/2/5/7 条（挑战者、triggers、均值锚、write-only 字段） | **本批刻意不做** | 全部要动 prompt / 契约 / live 路径，与「三项只读」的授权范围冲突；挑战者另有 9/12 B11 裁决窗，见 §九 |

**一句话**：Claude 的方法论诊断成立，本批把其中**三条可只读落地**的做完了；做完之后最重要的发现不是任何一条读数，而是**两把尺子都自报「没资格说话」**（①保真度不过门、③功率不足）、**一次成功的自我否证**（§四：把「保本漏武装」这个 live 病灶立案推翻，改判为尺子病灶 —— 它原本会导向一次错误的退出路径改动），以及**顺手挖出官方回放引擎已被打穿 36 小时**（§七）。

---

## 二、本批边界（什么没动）

- **零 live 改动**：`app/` 与 `main.py` **一行未改** ⇒ freshness 守卫扫描面（`scripts/deploy_freshness_check.py:43-44` `SCAN_DIRS=("app",)` / `SCAN_FILES=("main.py",)`）无变化，**服务不需重启**。
- 全部改动落在 `scripts/`（4 个）+ `tests/`（5 个）+ `docs/`。
- **只读纪律**：DB 一律 `file:...?mode=ro`；日K/1min bar 只读 `data/replay_bars*/` 缓存、**零网络拉取**；输出只写 `data/shadow/*_latest.json`（fresh 覆写、幂等）；**不回填任何历史行**。
- **不造平行层**：费率用 `app.execution.fee_model.estimate_round_trip`；退出数学用 `app.playbook.exit_engine`（`_compute_dynamic_effective_sl` / `_get_factor_exit_config`）；阈值一律读 `get_playbook_config()` / `unified_config.yaml`，本批不新增任何阈值语言。
- **不动的三件事**：挑战者 Pass2（等 9/12 B11 窗）、DA prompt 删均值锚（要动 prompt 指纹）、冻结期参数（C7）。

---

## 三、交付物①：配对反事实尺子（v2 双臂同尺）

### 3.1 v1 的两处口径缺陷（为什么要重写而不是调参）

`cf_pullback_limit_replay.py` v1 的反事实臂只近似「原始 SL 触及 / 否则 EOD 收盘」两种出场，而实盘臂走**完整退出引擎**（保本 ratchet / 回撤锁利 / 早期紧 SL / TP / PRE_CLOSE）⇒ **系统性低估反事实臂**；同时 v1 拿 `cf_pnl`（毛）比 `actual_net`（= `trade_records.pnl_net`，净）⇒ 又**系统性高估反事实臂**。两处偏差方向相反、量级未知 ⇒ **v1 的差值不可判读**，而 `playbook.entry_archetype_routing_enabled` 这个活体开关正等它裁决（ADR-035）。

v2 的三条不变量（写在 `scripts/exit_path_replay.py` 模块头）：

1. **两臂同尺** —— 实盘入场价与反事实入场价走**同一个重放器**、同为净额；实盘真值（`pnl_net` / `exit_reason`）另列，只用于**保真度校验**，不进配对差。重放器未复刻的规则因此成为**共模缺失**，在配对差里抵消。
2. **复用 SSOT 数学** —— 保本/锁利有效止损直接 import `exit_engine`，费用直接 import `fee_model`，不新建第二套公式。
3. **零副作用** —— 刻意**不**调 `_check_playbook_exit_rules`：该函数体内写生产影子文件（`data/breakeven_shadow.jsonl` / `early_dead_signal_shadow.jsonl`）并依赖墙钟（`_is_overnight_position` 用 `datetime.now`，会把历史重放日误判成隔夜仓）。离线重放若调它 = 污染尺子 + 假隔夜分支。

### 3.2 读数（9/1–9/4，可复现命令见 §十）

| 项 | 值 | 说明 |
|---|---|---|
| `n_rows` / `n_paired` | 37 / **30** | 7 笔无实盘成交 ⇒ 不入配对（合计只在 paired 群上算） |
| 口径 a（双臂同尺）合计 / 中位 | **+21.13 / −7.03** | 均值为正、中位为负 ⇒ 由少数笔驱动 |
| 口径 b（cf 重放 vs 账本真值）合计 | **−164.22** | 实盘侧是真实发生的钱 |
| `cf_better_count` / 符号检验 p（单侧） | 7 / **0.9993** | 反事实臂**不优于**实盘臂 |
| 稳健性（剔 1/3/5 笔最大绝对值） | 中位恒 **−8.53**，合计 −152.38 / −116.08 / −120.58 | 四口径**同号** ⇒ 结论对极值不敏感 |
| `fill_rate` | 0.514 | 限价挂得到一半 |
| **可判读性** | **`NOT_EVALUABLE_FIDELITY`** | `reason_match_rate` **0.633 < 0.8** 门槛 |

### 3.3 为什么「不可判读」才是本交付物的主要产出

保真度分桶（`fidelity.by_db_reason`）：

| 实盘出场原因 | n | 复刻命中 | 命中率 | 净偏差 |
|---|---|---|---|---|
| SL_BREAKEVEN | 20 | 19 | **0.95** | +1.20 |
| SL_TRAIL_LOCK | 7 | **0** | **0.00** | **−43.24** |
| SL_INITIAL | 2 | **0** | **0.00** | +73.79 |
| DEAD_SIGNAL_EXIT | 1 | **0** | **0.00** | −54.32 |

重放器对**锁利轨（SL_TRAIL_LOCK）复刻 0/7** —— 而这正是最赚钱的一轨（全窗 101 笔 +$4,580.38，见 §五同源库）。一把在锁利轨上 0/7 的尺子，去裁「入场方式该不该接线」，无论差值是正是负都不能用。

因此 **ADR-035 四判据虽 3/4 通过（C1 n≥20 ✓ 30、C2 期望改善≥30% ✓ 45.54%、C3 fill_rate≥50% ✓ 51.4%、C4 mfe0 占比下降 ✗），`adr035_verdict.wire_routing` 仍为 `false`，`blocked_by = fidelity_gate`**。这是**刻意设计**：保真度门先于四条判据，不过门则四条**一律不作数（含通过项）**——否则会出现「尺子有偏，但偏差恰好帮我的判据过关」这种自我恭维。

**下一步（未做，需另裁）**：修重放器保真度，再判读入场方式；修好之前 `entry_archetype_routing_enabled` 维持关闭。**根因已在 §4.5 定位并量化**：`SL_TRAIL_LOCK` 在全 395 笔上被误分类成 `SL_BREAKEVEN` 达 **84 笔**（实盘该轨共 101 笔 +$4,580.38），即 §三 那个 0/7 不是小样本偶发，而是同一个系统性偏差；三步修法见 §4.6（peak 采样与实盘 10s 轮询同构 → 核对锁利轨 peak 状态传递 → 补 TIME_EXIT/MFE_STALL/DEAD_SIGNAL 三轨 57 笔），**全部不碰 live 代码**。

---

## 四、重放器保真度病灶：「保本漏武装」这个立案**不成立**，真病灶在尺子上

> 本节是一次**自我否证**：前序会话把一批重放读数命名为「保本漏武装」（暗示 live 退出路径有缺陷）。
> 本批用三组独立数据把它推翻，并定位到真正的病灶。**结论方向与立案相反**，故整节按证据重写。

### 4.1 起点：一个看起来像 live 缺陷的交集

用交付物①的重放器对 `since 2026-07-11` 全部 **395 笔**已平仓 SIM 单逐笔重放（bar 缓存全覆盖，`no_bar_cache=0`），取「**实盘以真止损 `SL_INITIAL` 出场，而重放显示保本棘轮本应已武装**」这一交集：

**n = 8 笔；实盘净额合计 −$1,221.05（毛额 −$1,167.77）；重放净额合计 +$93.17；差 +$1,314.22。**

| 日期 | 标的 | 方向 | 持仓h | 实盘净 | 实盘 MFE | 实盘 MAE | 重放出场 | 重放净 | 重放 MFE |
|---|---|---|---|---|---|---|---|---|---|
| 08-18 | US.MU | LONG | 6.7 | −308.88 | 0.169% | 5.227% | SL_BREAKEVEN | +11.66 | 0.424% |
| 08-28 | US.AFRM | LONG | 1.9 | −308.07 | **0.000%** | 5.169% | SL_BREAKEVEN | +11.75 | 0.660% |
| 08-31 | US.TTD | LONG | **0.1** | −92.44 | **0.000%** | 1.378% | SL_BREAKEVEN | +11.77 | 0.471% |
| 08-31 | US.PCG | SHORT | **0.1** | −74.67 | **0.000%** | 1.080% | SL_BREAKEVEN | +11.76 | 0.410% |
| 09-01 | US.SAIC | LONG | **0.1** | −64.18 | **0.000%** | 1.238% | SL_BREAKEVEN | +11.59 | 0.437% |
| 09-01 | US.AMD | LONG | 0.4 | −60.81 | **0.000%** | 1.030% | SL_BREAKEVEN | +11.00 | 0.713% |
| 09-03 | US.HPE | SHORT | 0.4 | −153.95 | **0.000%** | 3.525% | SL_BREAKEVEN | +11.86 | 0.674% |
| 09-04 | US.USAR | LONG | **0.1** | −158.05 | **0.000%** | 3.151% | SL_BREAKEVEN | +11.78 | 0.662% |

若只看这张表，结论会是「实盘漏掉了 0.4~0.7% 的有利波动 ⇒ 保本没武装 ⇒ 白亏 $1,221」。**下面三节说明这个读法是错的。**

### 4.2 反证①：实盘的 MFE 遥测是**健康**的，那些 0.000% 是真值

仓内已有现成健康判据 —— `app/decision/edge_weekly_report.py:311-315`：「**盈利单有正 MFE 的比例** `mfe_valid_rate` < 50% ⇒ `mfe_pct` 大面积失效 ⇒ 阻塞 MFE 类退出调优」。实测：

| 窗口 | 盈利单 n | 有正 MFE | `mfe_valid_rate` |
|---|---|---|---|
| since 2026-07-11 | 262 | 262 | **100.0%** |
| 08-20 ~ 08-25 | 45 | 45 | **100.0%** |
| 08-26 起 | 76 | 76 | **100.0%** |

⇒ **该告警不触发，`mfe_pct` 不是坏列**。所以 §4.1 里 7 笔 `mfe_pct = 0.000%` 是**真值**（该仓位在实盘观测里确实从未出现过有利价），不是遥测缺口。这一步排除了「实盘记录坏了」这个候选解释。

（另一条交叉验证路径不可用，如实登记：`data/trail_state.db` 的 `trail_state_snapshot.trail_high/trail_low` 对 playbook SIM 路径**未落盘** —— 全表仅 28 行，§4.1 的 8 个标的一个都没有，唯一命中的是 2026-06-12 的 US.MU。故无法用峰值轨迹遥测独立复核；但 `mfe_valid_rate` 已足够定性。）

### 4.3 反证②：偏差是**双侧**的，重放整体偏保守 ⇒ 不存在「实盘系统性少看」

全 395 笔逐笔比对 `重放 MFE − 实盘 MFE`：

| | 笔数 | 占比 | 中位差 |
|---|---|---|---|
| 重放 > 实盘 | 171 | 43.3% | +0.073pp |
| 重放 < 实盘 | 218 | **55.2%** | **−0.472pp** |
| 相等 | 6 | 1.5% | — |
| **全体** | 395 | 100% | 中位 **−0.041pp** / 均值 **−0.394pp** |

⇒ 重放器整体**偏保守**，**没有**「实盘单向少看有利波动」的偏差。「保本漏武装」作为一个 live 病灶**不成立**。

偏低那一侧（55.2%）的成因是明确且已声明的：重放器在**第一个退出触发就停**，而它**未复刻** TIME_EXIT / MFE_STALL_EXIT / DEAD_SIGNAL_EXIT / MAX_HOLD_T5 / 耐心出场（`exit_path_replay.py` 模块头 `rules_omitted` 已列为**共模缺失**）⇒ 实盘那些仓位活得更久、期间继续走出有利波动 ⇒ 实盘 MFE 更大。

### 4.4 那 8 笔究竟是什么：**bar 极值 vs 10 秒轮询**的观测落差（USAR 铁证）

`US.USAR 2026-09-04 LONG 312 股`：实盘 09:37:30 成交 **$18.885**，09:45:18 以 $18.405 出场（持仓 **7.8 分钟**），`mfe_pct = 0.000%`、`mae_pct = 3.151%`、净 −$158.05。同日 1min bar（重放器**跳过入场那根 09:37**，从 09:38 起评估）：

| 时刻 | open | **high** | low | close |
|---|---|---|---|---|
| 09:37（入场根，不评估） | 19.135 | 19.30 | 18.95 | 18.97 |
| **09:38** | 18.9901 | **19.01** | 18.86 | 18.97 |
| 09:39 | 18.985 | 18.985 | 18.51 | 18.53 |
| 09:40 | 18.5355 | 18.5755 | 18.22 | 18.2596 |
| 09:41~09:45 | — | ≤18.52 | — | — |

重放 MFE = (19.01 − 18.885)/18.885 = **+0.662%** —— **全部来自 09:38 单根 bar 的 high**。而实盘 10 秒一轮的快环在整个 7.8 分钟里**一次都没有看见高于 $18.885 的报价**（`mfe_pct` 恰为 0.000%，且 4.2 已证该列健康）。

- 实盘退出快环生效间隔 = **10 秒**（`scheduler.playbook_exit_fast_loop_interval_sec: 10`；代码默认 `PLAYBOOK_EXIT_FAST_LOOP_INTERVAL_SEC = 20` 被 yaml 覆写为 10；生产调用点 `app/playbook/pipeline.py:7697` 位于 `monitor_playbook_sim_exits`）。
  **勘误**：不是 `scheduler.monitor_interval_minutes: 0.25`（15s）—— 那是通用监控节拍，不是 playbook 退出武装的节拍。
- §4.1 的 8 笔持仓 0.1~6.7h，其中 **5 笔 ≤0.4h（6~24 分钟）** ⇒ 评估窗只有几根 bar，**一根上影线就足以造出 0.4~0.7% 的 MFE**。

全库同类规模：**实盘 MFE=0 而重放 MFE>0 共 39 笔（9.9%）**，其中重放 MFE ≥0.4%（越过武装线 `breakeven_at_pct=0.4`）**13 笔** ⇒ §4.1 的 8 笔只是这 13 笔中 `db_reason` 恰为 `SL_INITIAL` 的子集，**不是一个独立病灶**。

### 4.5 真正的病灶：重放器把**锁利轨**系统性误分类成**保本轨**

全 395 笔 `reason_match_rate = **0.400**`（§三 的 30 笔配对子集上为 0.633）。最大失配桶：

| 实盘出场 | 重放出场 | 笔数 |
|---|---|---|
| **SL_TRAIL_LOCK** | **SL_BREAKEVEN** | **84** |
| PRE_CLOSE_EXIT | SL_BREAKEVEN | 21 |
| TIME_EXIT | PRE_CLOSE_EXIT | 20 |
| MFE_STALL_EXIT | PRE_CLOSE_EXIT | 20 |
| DEAD_SIGNAL_EXIT | PRE_CLOSE_EXIT | 17 |
| DEAD_SIGNAL_EXIT | SL_BREAKEVEN | 11 |
| TIME_EXIT | SL_BREAKEVEN | 10 |

全窗实盘 `SL_TRAIL_LOCK` 共 **101 笔 +$4,580.38**（最赚钱的一轨），重放把其中 **84 笔判成 `SL_BREAKEVEN`**。这就是 §三 保真度门里「`SL_TRAIL_LOCK` 复刻 **0/7**、净偏差 −43.24/笔」在**更大样本上的同一个根因**。

分类线是 `eff_sl > be_line + be_tol → SL_TRAIL_LOCK，否则 SL_BREAKEVEN`；84 笔**全部落在 else 分支** ⇒ 锁利几何（`drawdown_from_peak_pct` 0.8 / `trailing_step_pct` 2.0）在重放里**从未把 `eff_sl` 推到净保本线之上**。这不像参数差（参数直读 `get_playbook_config()`），更像 **peak 状态传递**被 §4.4 的 peak 口径差与 §4.3 的提前退出联合压低了 `peak_profit_pct`。

### 4.6 结论与建议：**改尺子，不改退出路径**

- **撤销「保本漏武装」这个立案名** —— 它把尺子的偏差读成了 live 的缺陷。**不建议**据此改退出路径，也**不建议**提「入场即预挂净保本限价单」的议案：在 §4.3/§4.4 澄清后，那 8 笔里大部分是重放器的**幻影 MFE**，不是实盘漏掉的利润；为幻影改 live 退出语义，会撞 KR2 cohort 单版本冻结与 C7 冻结期，且大概率重演 §11 的「越紧越亏」。
- **应立的是「重放器保真度专项」**，三步按性价比排序，**全部不碰 live 代码**：
  1. **peak 采样与实盘同构（最高优先）**：把 peak 更新从「1min bar 有利极值」改为「按 10s 步长在 bar 内插值采样」，或**双报**（bar-极值 peak / poll-同构 peak）作为敏感性区间。直接压 §4.4 的 39 笔幻影与 §4.5 的分类漂移。
  2. **核对锁利轨的 peak 状态传递**：84 笔全落 else 分支，优先查 `peak_profit_pct` 在 bar 循环里的累积是否与 `exit_engine` 逐字同构（而非先动 `drawdown_from_peak_pct` 参数）。
  3. **补三轨缺失规则**：TIME_EXIT / MFE_STALL_EXIT / DEAD_SIGNAL_EXIT 共 **57 笔**被重放塞进 `PRE_CLOSE_EXIT`；这三条是 `rules_omitted` 里**不需要墙钟**的部分，可离线复刻。OVERNIGHT_GAP_STOP / MAX_HOLD_T5 / 耐心出场仍需跨日 bar 或墙钟，**维持共模缺失**。
- **一条重要的自我确认**：§4.3/§4.4 的偏差对**配对差是共模的**（两臂走同一重放器、同一 peak 口径）⇒ §三 口径 a 的 +21.13 / −7.03 **不受影响**；受影响的只有**保真度**（重放 vs 实盘真值），而保真度门正是为此而设 ⇒ **门生效了，这个设计是对的**。这是本批对自己尺子的一次成功自检，也是「先验尺子、再验结论」这条纪律的具体兑现。

### 4.7 否证一个流传读数（宪章「遥测 > 自述」的直接应用）

前序会话口头报告过「保本漏武装 **15 笔 −$1,595.78**」。**本批未能复现该读数**，已穷举 7 种自然定义：

| 定义 | n | 实盘净合计 |
|---|---|---|
| V1 `db=SL_INITIAL` ∧ 重放∈{保本,锁利} | 8 | −1,221.05 |
| V2 `db_net<0` ∧ 重放∈{保本,锁利} | 93 | −2,769.08 |
| V3 V2 ∧ `live_mfe<0.4` | 30 | −2,226.99 |
| V4 重放∈{保本,锁利} ∧ `db∉` | 66 | −1,193.47 |
| V5 重放 MFE≥0.4 ∧ `db_net<0` | 94 | −2,777.60 |
| V6 重放 MFE≥0.4 ∧ `live_mfe<0.4` | 37 | −2,221.43 |
| V7 V6 ∧ `db_net<0` | 31 | −2,235.51 |

另：全窗 `SL_INITIAL` 共 20 笔 −$3,173.92；毛额口径 V1 = −$1,167.77。**没有任何一种给出 (15, −1595.78)**。

**处置**：按宪章「遥测 > 自述」与「结论必须附可复现命令」，**15 笔 −$1,595.78 不得作为证据被任何后续 DEB/RFC 引用**。可引用的只有本节各表（命令见 §十）。**没有产生命令的读数，等于没有读数** —— 这条否证本身是本批的产出之一，而且它恰好拦住了一个会导向错误 live 改动的立案。

### 4.8 保真度专项**执行结果**（2026-09-07 用户裁「按照建议执行优化」⇒ §4.6 三步全部落地，全程零 live 改动）

改动面：`scripts/exit_path_replay.py`（382→540 行）+ `scripts/cf_pullback_limit_replay.py`（526→659 行）+ `scripts/exit_replay_backtest.py`（`load_trades` 增 `decision_id` 一列）+ 两个测试文件。**`app/` 与 `main.py` 零改动 ⇒ 服务不需重启**；DB 仍全 `mode=ro`；零历史回填。

**① 采样口径（原「peak 与 10s 轮询同构」）— 落地方式改为「可证区间双报」，不猜中间值。**
10s 价格路径无法从 OHLC 反演，插值 = 造数据。改为两个**可证边界**：`sample_source="bar_extreme"`（fav/adv 取 bar 极值 = 假设轮询看到了整根极值，实盘观测**上界**）与 `"bar_close"`（fav/adv 均取收盘 = 假设轮询只看到收盘档，**下界**）。实盘每 bar ~6 次轮询，真值必落两者之间 ⇒ 保真度门要求**两口径都过**，配对差要求**两口径中位数同号**。

**② 锁利轨 peak 状态传递 — 真因不是状态传递，也不是参数，是「订单因果性」。**
91 笔 `SL_TRAIL_LOCK` 误分类的配置解析与实盘 **100% 一致**；病灶是旧实现在**同一根 1min bar** 内先用有利极值把 peak 抬过武装线、再用同一根的不利极值打掉刚武装的保本线 ⇒「武装即死」，锁利分支**数学不可达**（重放 MFE 达标率 0% vs 实盘 81%）。但净保本限价单是在武装**之后**才挂出的，不可能被武装之前发生过的价格成交 ⇒ 新增 `arm_lag_bars`（1 = 本根观测到的 peak 从下一根起才在场，**因果下界**；0 = 旧行为，**乐观上界**），同样双报。

**③ 三轨补齐**：`DEAD_SIGNAL_EXIT` / `MFE_STALL_EXIT` / `TIME_EXIT` 按实盘同序插入（PRE_CLOSE→DEAD_SIGNAL→MFE_STALL→TIME_EXIT，对应 `exit_engine.py:668/891/932/972`），全部**配置驱动**（含现网已关闭的 MFE_STALL，使任意纪年窗都可重放）；含 Phase0 reentry 呼吸窗（`decision_id` 前缀 `intraday_reentry` → 60→120min，全窗 19 笔）、8/04「浅亏转持有」分支、TIME_EXIT 的**毛口径**亏损门与 factor 档 `time_exit_hours`。隔夜日重置（RFC-064/073）与 W_WEEK 水下豁免需跨日 bar + 墙钟 ⇒ **维持共模缺失**。

**量化（`arm_lag_bars=1`；修前 = §4.5 的 0.400 基线）**

| 窗口 / 域 | n | 修前 | ②后 | ③后 | ③+①`bar_close` |
|---|---|---|---|---|---|
| 7/11 全窗（跨 3 纪年） | 395 | 0.400 | 0.466 | 0.509 | **0.570** |
| 8/15 单纪年 | 178 | — | 0.646 | 0.663 | **0.725** |
| 8/15 单纪年 ∧ 当日出场 | 158 | — | — | 0.728 | **0.791** |
| 9/03 KR2 cohort（n<30 仅参考） | 23 | — | 0.783 | 0.826 | **0.870** |
| ADR-035 窗 9/1–9/4 ∧ 当日域（门口径） | 28 | (0.633)\* | — | 0.786 | **0.821** |

\* **不同分母，只作方向参考**：§三 的 0.633 是 n=30 **全域**（含 2 笔跨日）口径，本行是 n=28 **当日域**。同窗同域可比的修前读数需重跑旧实现，本批未做（旧实现已被 `arm_lag_bars=0` + `sample_source="bar_extreme"` 两参数保留，可复现：两参数取旧值即旧行为，但**不含**三轨 ⇒ 严格的「修前同域」读数需再 revert 三轨，代价大于价值，故如实标注为不可比）。

分轨：`SL_TRAIL_LOCK` 复刻 全窗 10/101→**36/101**、8/15+ 7/51→**27/51**（`bar_close` 当日域 **37/46**）；`DEAD_SIGNAL_EXIT` 8/15+ **0/20→7/20**（`bar_close` 当日域 9/14）；9/1–9/4 窗**弱桶清零**（此前 `SL_TRAIL_LOCK` 0/7、`SL_INITIAL` 0/2、`DEAD_SIGNAL` 0/1）。钱口径：当日域净偏差 mean **−1.03** / MAE **21.05**（9/1–9/4 窗 +6.58 / 14.87），对比修前 `SL_TRAIL_LOCK` 桶 −43.24/笔、口径修正前 −65.82/笔。

**两道结构性上限（本批新发现，必须与门同屏，否则「0.8 门」会被读成努力不够）**

1. **参数纪年错位**：`MFE_STALL_EXIT` 末次实盘触发 8/11（现 `mfe_stall_exit_enabled=False`）、`TIME_EXIT` 末次 8/14 ⇒ 全窗 395 笔含这两轨 **62 笔**在现行配置下**不可能**被复刻 ⇒ 全窗保真度**数学上限 0.843**，0.8 门在全窗**不可达**。宪章禁跨窗拼接 ⇒ 只能换窗（8/15 起），不能改门。
2. **跨日出场**：实盘次日才出场的仓，其真值路径需跨日 bar + 墙钟。8/15+ 窗 **20/178** 跨日（该子集 match 仅 **0.150**）⇒ 该窗上限 **0.888**；9/1–9/4 窗上限 **0.933**。

⇒ 门只在「**同纪年 ∧ 当日出场**」域上要求；两条排除均**预声明**并披露样本数（`gate_domain` / `ceiling_cross_day` / `ceiling_dead_epoch` 三个字段随报告落盘），不按「哪个域过门」事后挑选。

**剩余失配定性（8/15+ 当日域 `bar_close`，26 笔）**：`SL_TRAIL_LOCK↔SL_BREAKEVEN` **对称**混淆 7+7=14 笔（54%），钱差中位仅 $2.47 / $19.94 ⇒ 是分类线（`eff_sl > be_line + 1bp`）在观测分辨率下的**噪声**，不是系统性偏差；其余散在 `SL_INITIAL→SL_BREAKEVEN`(3) / `DEAD_SIGNAL→SL_BREAKEVEN`(2) / `PRE_CLOSE→TIME_EXIT`(2) 等。另一条共模残差已定位：`factor_exit_rules` 档位本身也跨纪年（重放按**现行**档算 TIME_EXIT，实盘当时是另一档）⇒ 时间类轨在历史窗仍有残差，同属「只读现行 config」的必然代价。

**一个否证（避免下一批据此立错案）**：§4.4 的 USAR 铁证曾指向「实盘 `metadata.peak_price` 停在入场价」是 live 遥测缺陷（bars 显示 09:38 整根在入场价上方，而 meta.peak_price == entry）。全窗量化后只有 **2/158（1.3%）**（USAR 9/4、MSTR 9/1）⇒ **不是系统性病灶**，只作个案留痕，**不立案、不改 live**。

**对 ADR-035 的影响：门读数变了，结论方向没变（且这次是干净尺子上的同号结论）**

- 9/1–9/4 窗：`NOT_EVALUABLE_FIDELITY` —— `lag1|bar_extreme` **0.786 < 0.80**（未过）、`lag1|bar_close` **0.821**（过），两口径须**都**过 ⇒ 不过门。
- 四判据 **2/4**（此前 3/4，含 C2「期望改善 +153%」）；配对差中位 **−9.63，两口径逐字相同**，合计 −27.75 / −14.19，符号检验 **p=0.9974** ⇒ 反事实（确认位限价）臂**不优于**实盘臂。
- ⇒ `wire_routing=false`、`blocked_by=fidelity_gate`，**`entry_archetype_routing_enabled` 维持关闭**。§4.6 那句「门生效了，这个设计是对的」至此有了第二层证据：**弱桶清零之后**，配对差仍是同号负值 —— 此前那个「+153% 改善」确实是重放器把实盘赢家判成输家的伪影，不是入场方式的优势。

### 4.9 判据换轨：从「过没过门」到「这把尺子能回答哪一级问题」（2026-09-07 用户裁「我需要真实可用保真的数据，才能有准确的依据」）

**触发动机（用户原话）**：「历史的尺子和回测太多了，我都有点迷糊了，我需要真实可用保真的数据。」——问题不在尺子不够多，而在**每把尺子都没说自己能量到多细**。§4.8 之后共有 5 套读数（标签门 / 双口径 / 当日域 / 抗极值阶梯 / ADR-035 四判据），却没有一处回答「所以我现在能说哪句话、不能说哪句话」。

**换轨前的三个实证（都是本批测出来的，不是推测）**

1. **钱口径门无判别力 ⇒ 不能替换标签门，只能并列分级。** 烂窗 7/11–8/14（跨 3 纪年）标签一致率仅 **0.384 / 0.444**，而钱口径照样满分过关：`|bias|/MAE` = **0.054 / 0.009**、符号一致率 **0.852 / 0.875**。即「钱对得上」与「路径说得对」是两件事，前者看不出后者已经失效。
2. **逐笔不可判读 ⇒ 只有聚合/配对差可用。** 9/1–9/4 窗每笔净额中位仅 **|\\$11.86|**，而重放单笔误差 MAE **\\$14.87（bar_close）/ \\$22.32（bar_extreme）** ⇒ 比值 **1.25 / 1.88**，即**尺子的单笔误差比被测单笔盈亏还大**。任何「这一笔差 \\$X 是因为 Y」的说法都在噪声里；但同一把尺子在聚合层近零偏差（`bias/MAE` 0.088）+ 方向判对率 0.857 ⇒ 配对差可用。
3. **标签门对经济等价标签过严。** `SL_BREAKEVEN ↔ SL_TRAIL_LOCK` 对称混淆占 8/15+ 当日域剩余失配的 54%，钱差中位仅 **\\$2.47** ⇒ 标签失配不等于钱读错了。

⇒ 单一二元门（过 / 不过）在这三条实证下**同时过松与过严**：过松是「过了门就什么都能说」，过严是「不过门就什么都不能说」。改为**三级（四许可）卡 `ruler_license`**，阈值全部预注册，沿用既有 0.80 形状，不新造阈值语言：

| 许可 | 对应读数 | 判据（两 `sample_source` 都过才发放） | 9/1–9/4 实测 |
|---|---|---|---|
| **L0 逐笔归因** | `rows[].v2.paired_diff` 单笔 | `mae_over_median_abs_net ≤ 0.25`（分辨率须优于被测量的 1/4） | **1.25 / 1.88 ⇒ ❌** |
| **L1a 配对差** | `caliber_a_same_replayer`（两臂同尺） | `sign_agreement_rate ≥ 0.80` | **0.857 / 0.893 ⇒ ✅** |
| **L1b 聚合对账** | `caliber_b_vs_ledger`（对账本真值） | `bias_to_mae_ratio ≤ 0.25` ∧ `sign_agreement_rate ≥ 0.80` | **0.088 / 0.443 ⇒ ❌** |
| **L2 出场路径归因** | `exit_reason` 因果故事 | 既有标签门 `reason_match_rate ≥ 0.80` ∧ 无 n≥5 弱桶（原样保留） | **0.786 / 0.821 ⇒ ❌** |

**L1 为什么必须拆 a/b（本批唯一一次判据形状修正，且发生在生效日之前）**：起初 L1 是一级（`bias/MAE ≤ 0.25 ∧ 符号一致 ≥ 0.80`），实测直接判死整级 —— 因为两种读数对「系统偏差」的要求**相反**：

- `caliber_a`（限价确认位臂 vs 现行入场臂）两臂**同一重放器、同一 config** ⇒ 偏差同时作用于两臂，在差值里**共模抵消**（代码既有注释即如此声明），故只要求方向判对率；
- `caliber_b`（cf 重放 vs `trade_records` 账本真值）跨尺子 ⇒ 偏差**全额生效**。实测 `bar_close` 口径 bias **+\$6.58/笔 = 其 MAE 的 44%**，而待裁效应中位数仅 **−\$9.63/笔** ⇒ 偏差与效应同量级，这类读数不可当结论。

合成一级的后果是**连带否掉本次唯一要裁的那个读数**（L1a）。拆级只用同两个预注册阈值（0.25 / 0.80），改的是**阈值归属**而非阈值本身；且钱口径判据的正式生效日为 **2026-09-08**（见下），本次修正在生效日之前完成 ⇒ 不构成对已出裁决的追溯改门。抵消是一阶近似而非恒等（两臂入场价不同 → 出场路径可能不同），残余部分由 L0 的 MAE 界定，这一限制写进许可卡的 `why` 字段随报告落盘。

**许可判定域 = 既有保真度门同域**（`arm_lag_bars=1` ∧ 当日出场 ∧ 两口径），预注册。全日域读数照旧披露在 `fidelity_matrix`，**不参与许可** —— 换域即挑口径（全日域 `bias/MAE` 是 0.049/0.192，看起来更好看，但那是把不可复刻的跨日仓算进来的结果）。

**纪年守卫 `_epoch_guard`（机械、自更新，替代人工点名）**：§4.8 的「参数纪年错位上限」靠人工在字典里点名 `MFE_STALL_EXIT`，再关一条轨就漏判。现改为从数据算：门域内某 `db_reason` **n≥5 且复刻率=0** ⇒ 判为「不可达轨」，`epoch_floor` = 这些轨**末次日期 +1 天**（= 最早可用窗起点），窗起点 ≤ `epoch_floor` ⇒ 状态 `NOT_SINGLE_EPOCH`。门域零样本 ⇒ `single_epoch=None`（fail-closed，不宣称同纪年）。零复刻有两种成因（该轨在现行纪年已关闭 / 重放器根本没实现），**都使本窗不可判读，守卫不区分**。附带披露 `masked_zero_match_tracks`：域收窄会藏坏轨 —— 实测 `SL_INITIAL` 全口径 **n=8 复刻 0**，当日域只剩 **n=4 < 5** ⇒ 躲过既有弱桶规则；不改门，但「无弱桶 ≠ 无坏轨」这句必须与门同屏。

**人话读数卡 `readout_card`（6 行必读 + 最多 2 行条件警告，随 stdout 与 JSON 落盘）**：把上面全部读数翻译成「可以说 / 不可以说」，并标明本次读数的**效力**。不新增任何判据。9/1–9/4 实跑输出（`./venv/bin/python scripts/cf_pullback_limit_replay.py --from-date 2026-09-01 --to-date 2026-09-04`）：

```text
┌─ 读数卡: 这把尺子本次能回答什么 (不新增判据) ─────────────────────
│ 窗 2026-09-01 → 2026-09-04 | 配对 n=30 | 纪年: 单一参数纪年
│ ⚠ 域外零复刻轨 (不计入门, 但存在): SL_INITIAL 全口径 n=8 复刻 0.0, 当日域仅 n=4 < 5 ⇒ 躲过弱桶规则
│ ⚠ 未达本裁决门槛: 配对 n=30 ≥ 30 (宪章可下结论) 但 < 40 (ADR-035 接线裁决预注册门槛) ⇒ 可说方向, 不得裁 entry_archetype_routing_enabled; 不得降门槛, 也不得把生效日之前的窗并进来凑数 (宪章禁跨窗拼接)
│ L0  逐笔归因    : ❌ 不许可 — 尺子单笔误差 = 被测单笔量级的 1.88 倍 (阈 ≤0.25; 两口径 {'lag1|bar_extreme': 1.88, 'lag1|bar_close': 1.25}) ⇒ 不可以说「这一笔差 $X 是因为 Y」这类逐笔归因
│ L1a 配对差(两臂同尺): ✅ 许可 — 方向判对率 = 0.857 (阈 ≥0.8, 两口径 {'lag1|bar_extreme': 0.857, 'lag1|bar_close': 0.893}); 系统偏差同时作用于两臂 ⇒ 在配对差里抵消, 故不作要求 ⇒ 可以说「整体上限价确认位比现行入场好/差 $M」这类配对差结论 (本次要裁的就是它)
│ L1b 聚合对账(对账本): ❌ 不许可 — 系统偏差/离散度 = 0.443 (阈 ≤0.25)、方向判对率 = 0.857 (阈 ≥0.8, 两口径 {'lag1|bar_extreme': 0.857, 'lag1|bar_close': 0.893}) ⇒ 不可以说「重放出来的总钱数对得上账本真值」这类跨尺子聚合对账结论
│ L2  出场路径归因  : ❌ 不许可 — 同纪年当日域标签一致率 {'lag1|bar_extreme': 0.786, 'lag1|bar_close': 0.821} (阈 ≥0.8), 无 n≥5 弱桶 ⇒ 不可以说「因为触发了哪条退出规则」这类出场路径归因
│ 结论: 限价确认位 vs 现行入场 — 点估计劣于 (配对差中位 -9.63 美元/笔, n=30, 两口径同号), 符号检验 p=0.9974 ⇒ 没有统计学证据判定优劣 (点估计不是结论); 接线开关 entry_archetype_routing_enabled 维持关闭
│ 效力: 指示性, 不具裁决效力 — 钱口径判据是在本窗数据上设计的, 用它裁本窗 = 凑门; 判据自 2026-09-08 起才有约束力, 正式裁决点 = 该日起新窗攒到 n≥40 配对
└───────────────────────────────────────────────────────────────────
```
（以上为**逐字**输出，未节略；同一份内容在 `data/shadow/pullback_limit_replay_v2_latest.json` 的 `summary.readout_card`）

**尺子许可 ≠ 裁决资格（本批实测出的缺口，必须与许可同屏）**：许可卡发放的是**尺子分辨率**，不是**结论资格**。装完卡立刻用 cron 的真实单日路径（`--date 2026-09-04`）试跑，发现 **n=6 的窗四级许可全部发放**（L0 0.36 / L1a 1.0 / L1b 0.199 / L2 1.0），若不设第二道闸，9/8 生效日之后单日 cron 就会每天打出一张自称「效力: 正式」的卡；更糟的是 6/6 同向时符号检验 **p=0.016 < 0.05**，结论行会写「**有统计学证据**表明优于」——那是小样本巧合，不是证据。故新增 `MIN_N_FOR_CONCLUSION = 30`（宪章「n<30 不结论」）作**第二道闸**，与许可分开落盘：

- `ruler_license.sample_size` = `{n_paired, min_for_conclusion, sufficient}`；
- `ruler_license.verdict_eligible` = `生效日已到 ∧ n≥30 ∧ status=EVALUABLE_AT_AGGREGATE_LEVEL`；
- 读数卡：`n<30` ⇒ 加一行 `⚠ 样本不足 … 不得被引用为裁决依据`，**结论行只报点估计与 p，不写「有统计学证据」字样**，效力行写「尺子许可正式生效 … 但配对 n=X < 30 ⇒ 无裁决资格，只可作方向性参考」。

**n 门本身还要再分两级**（本批第二次实测纠偏）：`MIN_N_FOR_CONCLUSION=30` 是宪章「能不能说任何结论」的地板，而**本裁决**预注册的是 `ADR035_VERDICT_MIN_N_PAIRED=40`。若只用 30，则 9/8 之后第一个 n=30 的窗就会打出 `verdict_eligible=true` 的卡 ⇒ 把「攒够样再判」悄悄换成「攒到地板就判」，正是生效日纪律要防的那类挪门。故三道闸**互相独立、缺一不可**：

| 闸 | 管什么 | 常量 | 不过闸时卡上说什么 |
|---|---|---|---|
| 生效日 | 判据能不能用来裁 | `MONEY_CRITERIA_BINDING_FROM=2026-09-08` | 「效力: 指示性, 不具裁决效力」 |
| 宪章 n | 能不能说任何结论 | `MIN_N_FOR_CONCLUSION=30` | 「⚠ 样本不足 … 不得被引用为裁决依据」+ 结论行只报点估计 |
| 裁决 n | 能不能裁这个开关 | `ADR035_VERDICT_MIN_N_PAIRED=40` | 「⚠ 未达本裁决门槛 … 可说方向, 不得裁 `entry_archetype_routing_enabled`；不得降门槛, 也不得把生效日之前的窗并进来凑数」 |

实测三态：9/4 单日 n=6 ⇒ 三道全不过；9/1–9/4 窗 n=30 ⇒ 只被生效日闸与裁决 n 闸拦住（指示性 + 未达本裁决门槛）；n≥40 且窗起点 ≥9/8 ⇒ `verdict_eligible=true`，两行警告消失。

**生效日纪律（防「用自己设计的门裁自己」）**：钱口径判据是在 9/1–9/4 窗的数据上设计出来的，用它裁决该窗 = 凑门。故 `MONEY_CRITERIA_BINDING_FROM = "2026-09-08"`：窗起点早于该日 ⇒ 状态 `PROVISIONAL_INDICATIVE_ONLY`（层级照常发放，只是**不具裁决效力**）；≥ 该日 ∧ L1a 过 ∧ 两口径同号 ⇒ `EVALUABLE_AT_AGGREGATE_LEVEL`。

**对 ADR-035 的影响：结论不变，性质变了。**

- 旧读法：「9/1–9/4 窗 `NOT_EVALUABLE_FIDELITY`，四判据 2/4，开关维持关闭」——听起来像「尺子坏了所以没结论」。
- 新读法：**尺子在 L1a 这一级是好的**，而这一级恰好就是待裁问题所在；在该级上读出的答案是「点估计劣于、但 p=0.9974 ⇒ 无统计学证据」⇒ **不优于** ⇒ 开关维持关闭。L0/L1b/L2 三级不许可只是禁止了另外三类话（逐笔归因、对账本聚合、路径因果），不影响本裁决。
- **正式裁决点 = 2026-09-08 起的新窗攒到 n≥40 配对**。按 9/1–9/4 实测速率 **7.5 笔/交易日**，4 个交易日（9/8–9/11）≈30 笔、6 个（9/8–9/15）≈45 笔 ⇒ **两个跑点已写死进 `handoff.md` 下一步 `0E`**：**9/12（六）中期体检**（`--from-date 2026-09-08 --to-date 2026-09-11`，预期 n≈30 ⇒ 卡上应出现「未达本裁决门槛」，**只验证两道 n 闸与生效日闸真的咬合**，不裁决）+ **9/16（三）正式裁决**（`--from-date 2026-09-08 --to-date 2026-09-15`，预期 n≈45 ⇒ `verdict_eligible=true`）。
+- **分支规则（预注册，事后不得挪门）**：①n<40 ⇒ **顺延**，不降门槛，且**不得把 9/1–9/4 的 30 笔并进来凑数**（宪章禁跨窗拼接）；②`NOT_SINGLE_EPOCH` / L1a 不许可 / 两口径中位数异号 ⇒ 不裁决，先修尺子或换窗；③L1a 许可 ∧ n≥40 ∧ 点估计优于 ∧ p<0.05 ∧ **ADR-035 四判据全过** ⇒ 才谈接线（四判据全过是既有预注册，不因许可卡放宽）；④其余 ⇒ 开关维持关闭，并触发 §九 第 7 条的「删字段」裁决（`entry_archetype`/`confirmation`/`tripwires` 退出 live 契约）——那是 live 契约变更，需独立预注册，**不在本命令内自动执行**。
+- **为什么手动跑而不入 cron**：cron（`47 5 * * 2-6`）只跑**单日**，n≈6，永远攒不到 40；自动滚动累积会新造一个每天产生新读数的影子实体，与 §九 第 3 条「对照臂 / 费率账不入 cron」同理。**用户 2026-09-07 裁「按照建议执行」= 采①手动跑。**
- 待裁 ⑦（挑战者 AGREE 升档 / 删均值锚 / write-only 字段）**仍排 9/12 B11 窗，本批未插队**。

## 五、交付物②：客单价费率盈亏平衡账

### 5.1 机械确定的部分（不需要样本，是公式）

费用是**双地板**，两条互不可替代：

```
fee_bps ≈ max(39800 / N, 262 / P) + 0.21        N = 名义额(USD), P = 股价(USD)
```

- `39800/N` = 每腿 $1.99 佣金地板（×2 腿）⇒ **加名义额**才压得掉；
- `262/P` = 每股佣金 $0.01 + 交收费 $0.003 + TAF + SEC 的渐近线 ⇒ **换高价股**才压得掉；
- 加名义额压不掉股价项，换高价股压不掉名义额项。

现行**可达名义带** = $2,500（`admission.min_notional_usd`）~ $5,926.66（净值 $98,777.71 × `max_position_pct` 6%）：

| 位置 | 费地板 | 条件 |
|---|---|---|
| 带上界 + 最优股价 | **7.04 bps** | 名义 $5,927 且 P≥$600 |
| 带下界（任何股价） | **15.92 bps** | 名义 $2,500，股价再高也躲不掉佣金地板 |
| `min_entry_price = $5` 处渐近线 | **52.61 bps** | 全表最贵的一档 |

**两个此前未被写清的语义**：

1. `admission.min_notional_usd = 2500` 的真实语义**不是**「小单过滤」，而是**费用地板 15.92bps**；RFC-071 把它从 3000 降到 2500 = 把费用地板从 **13.27bps 抬到 15.92bps**（换吞吐量，代价是单位经济学）。
2. 股价 < $20 的标的**在任何名义额下都压不到 13.3bps 以下**；而现行 `min_entry_price = $5` 恰好落在渐近线最贵档。

### 5.2 实测的部分（窗口统计量，附 caveat）

`since 2026-07-11`，n_trades = **399**（≥30，无 n 旗标）：

| 口径 | 毛边际 |
|---|---|
| 名义加权 | **6.13 bps** |
| 名义加权 · 剔极值 | **7.57 bps** |
| 逐笔中位 | **14.11 bps** |
| 逐笔均值 · 剔极值 | 2.09 bps |

生成侧分布（472 条指令，名义均值 $4,632.89 / 中位 $5,132.32）：**45.1% 的指令 fee_bps > 10bps**；按名义额分档，`[1500,2500)` 档 34 条均 18.12bps、`[2500,3000)` 档 27 条均 16.10bps、`[5000,5900)` 档 228 条（48.3%）均 8.74bps。

**caveat（必须与数字同屏）**：剔极值后毛边际从 6.13→7.57bps，**翻过了带上界地板 7.04bps** ⇒ 「带上界处费用吃光毛边际」这个 A/B 对比是**边际的**，不可当定论；而**带下界处（15.92bps）远高于任何毛边际口径，是确定的**。

**另一条同义反复警告**：`fee_model_vs_ledger` 的 `mean_abs_diff_bps = 0.0065` 看似完美，但 `fees_source` 全为 `est` ⇒ `pnl_net` 本来就是本模型算出来的，**一致是同义反复，不构成对券商实收的验证**。

### 5.3 三个等价逃生门（可换算，不是三选一的建议）

要让「带上界」够到实测毛边际 6.13bps，三者**等价可换**：

| 路径 | 所需 | 当前 | 缺口 |
|---|---|---|---|
| 加净值 | **$114,196.83** | $98,777.71 | **+15.6%** |
| 加单名帽 | **6.94%** | 6.0% | +0.94pp |
| 提高每笔毛边际 | **7.04 bps** | 6.13 bps | +0.91bps |

前两条是同一件事（名义上界 = 净值 × 帽）；第三条是**改持有期或改选股**——更长持有期 ⇒ 更大毛波动 ⇒ 同一笔费用占比更低。这与 Claude 的第 6 条判断会师：**30bps 级来回成本决定了可行持有期的下限**，而 DA 天生说的是波段语言、执行层跑的却是日内动量。

**本脚本不做**：不改配置、不改代码、不下「该拦」的结论——只给档位算数字。档位反事实归口既有 `scripts/admission_replay.py`（勿重复实现）。

---

## 六、交付物③：笨规则对照臂（全系统第一个对照臂）

### 6.1 设计

同一批浮现层候选（`signal_snapshots`，**无执行/费用/门禁 ⇒ 无选择效应**），同一收益窗（`close_t0 → close_tn`，与库内 `fwd_ret` 同窗），**两臂共用同一收益、只差方向指派** ⇒ 配对结构。五条平庸臂，每条都声明信息集与「两链决策时是否已知」：

`ALWAYS_LONG` / `PREV_DAY_MOMENTUM` / `OVERNIGHT_GAP` / `T0_CLOSE_MOMENTUM`（含决策时未知信息，作上界参考）/ `CONTRARIAN_LLM`（反指检验）。主指标 = **按日聚类的配对差 + MDE@80%**（宪章七律 #9），并强制自报「无法分辨」。SHORT 基准与 `signal_ic_tracker` 同式 `(t0/tn)−1`，**不是** `−long_basis`。

### 6.2 数据完整性（先验尺子，再验结论）

| 项 | 值 |
|---|---|
| 可配对行 / 交易日 | **941 / 45**（去重前 1094；重复登记 113 = 同一假设被 DA 多次重跑各落一行） |
| 口径逐行校验（本脚本重算 vs 库内 SSOT `fwd_ret`） | **不一致 0 行** |
| 回填行（`created_at` 晚于 `trade_date`） | 11 ⇒ 浮现层基本实时登记 |
| T+5 覆盖 | **仅 42 行**（DA `alpha_t5` 全 NULL）⇒ **T+5 读数当前不可用** |
| 中性方向剔除 | 954 行（`sign=0` 不入配对、**不记 0 收益**，否则把差值拉向 0） |

### 6.3 功率声明（本交付物的主要产出）

`LLM vs 0`（主口径 `abs_lt_0_5`，T+1）：日差 sd **0.925pp**，MDE@80% **0.3861pp**，`daily_mean_diff` **−0.06pp**，`t_daily` −0.435，置换 p 0.6656，`resolvable = false`，**days_needed = 1864**。

五臂横比：日差绝对值区间仅 **0.036–0.138pp**，最小 MDE@80% **0.3429pp** ⇒ **需 835~4152 个交易日**，`resolvable_arms = []`（**没有任何一臂可分辨**）。

### 6.4 四离群口径同号表（军规「剔除前 3 笔后仍成立」的强化版）

| 对照 | raw | abs<0.5 | winsor1/99 | excl_top3 | 四口径同号 |
|---|---|---|---|---|---|
| LLM_vs_ZERO | +0.174pp | −0.060 | −0.039 | −0.061 | **否 ⇒ 不可采信** |
| ALWAYS_LONG | +0.317 | +0.036 | +0.042 | +0.036 | 是（偏 LLM） |
| PREV_DAY_MOMENTUM | +0.276 | +0.090 | +0.057 | +0.090 | 是（偏 LLM） |
| OVERNIGHT_GAP | +0.283 | +0.096 | +0.104 | +0.096 | 是（偏 LLM） |
| T0_CLOSE_MOMENTUM | +0.122 | −0.065 | −0.060 | −0.117 | **否 ⇒ 不可采信** |
| CONTRARIAN_LLM | +0.142 | −0.138 | −0.106 | −0.140 | **否 ⇒ 不可采信** |

**单行劫持实锤**：`LLM_vs_ZERO` 的 raw 口径为**正**（+0.174pp），而三个稳健口径全为**负**。翻号来源是**一行**：`US.ADBT 2026-09-02 SHORT`（`close_t0` 1.72 → `close_t1` 0.3437，`fwd_ret_t1 = +4.0044` = 单日 80% 崩盘，全库 `|fwd_ret_t1|>0.5` 仅此 1 行）。**941 行里的一行，把头条口径的符号买走了。** 这正是「四口径同号才采信」这条军规的存在理由，也是本批把它写成代码（`cross_policy_sign_table`）+ 写成钉子（`tests/test_trivial_rule_control_arm.py::test_cross_policy_sign_table_flags_unstable_conclusions`，用例数据即照抄 ADBT 形态）的原因。

### 6.5 结论与含义

- **反面证据**：一行规则臂与 LLM 臂在本窗口**不可区分**。注意这不是「LLM 无 edge」的终裁（脚本 `not_a_verdict` 字段明写不下此判），而是「**用浮现层收益类判据无法裁决任何 prompt/执行改动**」。
- **配对确实降了方差，但没降一个量级**：各臂日差 sd 与 `LLM vs 0` 的 sd（0.925pp）同量级，而待分辨的效应本身只有 0.036~0.138pp。
- **这与 Claude「n≥30 对 PnL 类判据在数量级上不够」的判断一致，且给了具体倍数**：不是 30 不够，是**差 20~90 倍**（835~4152 交易日 vs 现有 45）。
- **⇒ 尺子选错了，不是样本不够**。判据终点必须换到高 SNR 代理：成交保真度（确定性，无需 n）、配对反事实（信号方差被消掉）、trigger 条件收益（按持仓日采样）。这正是交付物①的方向。
- **本脚本不下结论、不改配置、不入 cron**（建议先手动跑，见 §九）。

---

## 七、计划外根因修复：官方回放引擎被打穿 36 小时（P2a 级联）

### 7.1 病灶

`docs/REPLAY-SCRIPT-LEDGER.md` 的公约要求「新写回放脚本必须 `import exit_replay_backtest` 取原语」，但**没有任何测试钉住引擎本身可 import**。

提交 `14e9a544`（P2a「引擎纯搬家」，2026-09-06）把 `_compute_dynamic_effective_sl` 本体逐字迁入 `app/playbook/exit_engine.py`，并**撤掉了 `app/playbook/pipeline.py` 侧的转发器**（`pipeline.py:6678` 留有注释说明；`_get_factor_exit_config` 的转发器保留了，`_compute_dynamic_effective_sl` 的没有）。而官方引擎 `scripts/exit_replay_backtest.py:40-43` 仍从 `pipeline` 取这两个名字 ⇒ **当场 ImportError**。

**全量回归 0 红（当时 3200 passed）** —— 没有任何测试发现。发现路径：本批为复现 §四 而写的探针 `import exit_replay_backtest` 直接炸。

### 7.2 影响面（实测，不是推断）

| 消费方 | import 形态 | 状态（修复前） |
|---|---|---|
| `limit_entry_counterfactual.py:38` | 模块级 | **硬断** |
| `entry_gate_replay.py:37` | 模块级 | **硬断** |
| `be_counterfactual_replay.py:7` | 模块级 | **硬断** |
| `archetype_bars_ensure.py:59` | **函数内懒 import** | 断（只在补盲分支触发）— **cron `35 5 * * 2-6` 活体** |
| `post_exit_path_shadow.py:97` | **函数内懒 import** | 断（只在截断 bar 修复分支触发）— **cron `45 5 * * 2-6` 活体** |
| `position_path_collector.py:70` | **函数内懒 import** | 断 — **cron `15 5 * * 2-6` 活体** |

**懒 import 三处最危险**：模块自身可 import ⇒ 任何 import 级检查都是绿的，只在 cron 跑到那一行时抛，除日志外无人看见。

**定时炸弹时点**：P2a 提交于 9/6（周日）；三个 cron 均为 `* * 2-6`（周二~周六），9/6 周日、9/7 周一劳动节均不调度 ⇒ **首次带病运行 = 9/8（周二）05:15/05:35/05:45**。活体旁证：`data/shadow/post_exit_path.jsonl` 末次成功写入 **2026-09-05 05:45**。

**注意**：`archetype_bars_ensure.py` 的**主要职责就是补拉缺失 bar**，即它每次运行几乎必然走到那个懒 import ⇒ 9/8  detonation 概率接近 1，不是边角情况。

### 7.3 修法与回滚

- **修根因，不修症状**：`scripts/exit_replay_backtest.py` 的 import 改指向 SSOT 本体 `app.playbook.exit_engine`（两个函数都在那里，P2a 声明「逐字迁入」⇒ **数学零变化**），并同步模块头 docstring 的口径声明。**不动 `pipeline.py`**（不恢复转发器——转发器是搬家期兼容层，依赖它才是定时炸弹）。
- **补钉子**：新建 `tests/test_replay_engine_import_smoke.py`（9 例）——模块级 4 个消费方可 import、懒 import 3 个消费方可 import 且其依赖的引擎原语在位、公约承诺的原语清单（`load_trades`/`fetch_bars`/`replay_trade`/`aggregate`/`_parse_et`）不缺、**引擎用的退出数学必须 `is` exit_engine 本体对象**（不是副本、不是转发器）。钉子只测可 import，**不跑回放**（不碰网络、不碰生产 `data/`）。
- **回滚**：删该测试文件 + 还原 import 两行（回到无牙状态）。
- **零 live 影响**：`scripts/` 不被 `app/` 或 `main.py` import ⇒ 服务不需重启。

### 7.4 这条病灶的方法论含义（比修复本身重要）

**「证据冻结」状态的脚本是盲区**：台账把 `entry_gate_replay.py` / `be_counterfactual_replay.py` 等标为「证据冻结（不可改不可删）」，于是它们**既不会被改、也不会被测**，静默腐烂 36 小时无人知。公约要求所有人复用官方引擎，却没人给引擎上锁。**任何被公约指定为「单一入口」的模块，必须有一个 import 级钉子**——这是本批新增的第四条纪律（前三条见 `AGENT.md` 军规）。

---

## 八、测试与验证台账

### 8.1 新增测试（5 个文件，127 例）

| 文件 | 例数 | 锁的是什么 |
|---|---|---|
| `tests/test_exit_path_replay.py` | 28 | 重放器规则次序、入场 bar 整根跳过、SL 三分类、限价地板 vs bar 极值两种成交价口径、SSOT 数学同源 |
| `tests/test_cf_pullback_limit_replay_v2.py` | 17 | 双臂同尺、保真度门先于 ADR-035 四判据、paired 群口径、`NOT_EVALUABLE` 语义 |
| `tests/test_ticket_size_breakeven.py` | 28 | 双地板公式、带内地板、三个等价逃生门换算、只读与落点 |
| `tests/test_trivial_rule_control_arm.py` | 44 | 配对结构（离群裁剪必须同时作用两臂）、SHORT 基准同式、按日聚类 + MDE 自报「无法分辨」、四口径同号表、方向无定义行不入配对、只读零拉取 |
| `tests/test_replay_engine_import_smoke.py` | 9 | 官方引擎与其 7 个消费方可 import、原语在位、数学指向 SSOT 本体 |

### 8.2 全量回归与 7 个失败的定性

首轮 `pytest -q` = **7 failed / 3200 passed / 37 skipped**。逐条定性：

| # | 失败 | 归因 | 处置 |
|---|---|---|---|
| 1-3 | `test_pullback_limit_shadow_telemetry.py::TestReplayShadowNullCaliber` ×3 | **本批**：v2 的 SELECT 新增 `t.exit_time` / `t.metadata_json`，测试合成表 DDL 未跟（生产真表两列都在）⇒ `OperationalError` | **已修**：fixture DDL 补两列（不改生产、不改 v2 口径） |
| 4 | `test_except_pass_ratchet.py` 955→956 | **本批**：`exit_path_replay.py` bar 循环里一处 `except: continue` | **已修（按 R5，不放宽基线）**：改为计数并在报告透出 `bars_skipped_bad_row`。BASELINE **维持 955**。实测 395 笔重放该计数**全为 0**（缓存无损坏，且计数器可用） |
| 5-6 | `test_net_breakeven_contract.py` / `test_w_week_dead_signal_contract.py` 的 MAX_HOLD 两例 | **非本批 · 劳动节伪失败**：两例用 `now_et() − 3 天` = 9/4 作入场，`horizon_max_hold_hit` 走 `nth_trading_day_after("US", 9/4, 1)` = **9/8**（因 9/7 休市），而 `now = 9/7 < 9/8` ⇒ 规则**正确地**不触发，测试期望触发 | **不修**（属他批测试的日历依赖）；**9/8 自愈**。已实证：`is_trading_day('US', 2026-09-07) = False` |
| 7 | `test_trading_day_check_contract.py::test_stdout_token_and_exit_code_agree` | **非本批**：提交 `bace63a6`（交易日守卫批）的契约测试要求 stdout 逐字只有 token，但 OpenD 在场时 futu SDK 把连接日志打到 stdout ⇒ `strip()` 后不等于 token。**功能无损**：9 个消费方中 4 个读 exit code、2 个用 `"NON_TRADING_DAY" in stdout.upper()` 子串匹配、`business_heartbeat` 读 returncode，全部不受污染影响 | **不修**（越权改他批已提交文件 + 该批对 stdout 字节形态有明确要求）；已在 §九 列为待用户裁决项。**【2026-09-07 追记：用户裁「按照建议执行优化」⇒ 已按「治根」选项修复，见 §4.8 与 §8.4】** |

修复后：**3 failed / 3213 passed / 37 skipped**（3204 + 9 冒烟钉子），3 个失败 = 上表 #5/#6/#7，**全部与本批无关**。`pyflakes` 对本批 5 个新文件 + 2 个改动文件**零告警**（`tests/test_pullback_limit_shadow_telemetry.py:20 'pytest' imported but unused` 为**存量**，本批未碰 import 段）。

### 8.4 保真度专项批（§4.8）的测试与回归

- `tests/test_exit_path_replay.py` **28→45 例**：新增三轨 12 例 + **两个双报口径的钉子 5 例**（`arm_lag=1` 下同根「武装即触发」必须不再发生、默认值必须是因果下界、`sample_source` 两口径在**出场原因与成交价上都必须分岔**（USAR 型幻影保本）、非法口径名必须 `raise`、`bar_close` 下 SL/TP 也按收盘判＝观测模型贯穿而非只改 peak）；三轨 12 例（触发/关闭/阈值抑制/reentry 加宽/SHORT 对称/PRE_CLOSE 次序优先/浅亏转持有与边界/TIME_EXIT 毛口径与**严格 `>` 小时边界**/`time_exit_hours` 走 factor 档/SL 优先级）+ **cfg 键集漂移钉子** `test_test_cfg_keyset_matches_load_cfg`（`replay_exit` 对 cfg 用严格下标，`load_cfg` 每加一键就会打穿手搓 cfg 的调用方 —— 本批加 9 键时正是这样炸的，故钉进 CI）；`rules_omitted` 披露面搬家断言同步更新。
- `tests/test_cf_pullback_limit_replay_v2.py` **17→22 例**：双口径门（缺 `bar_close` 格 ⇒ **fail-closed**，不得退化成单口径过门）、当日域门口径 + `ceiling_cross_day` 披露、两口径中位数异号 ⇒ `NOT_EVALUABLE_CALIBER_SPLIT` 且 `blocked_by=caliber_split`、死纪年上限内部一致性。
- **`trading_day_check.py` stdout 治根**（§九 待裁 ⑥ 的「治根」选项）：`is_trading_day()` 的 Futu 权威层触发 SDK `FTLog` 单例把连接日志打到 stdout（`futu/common/ft_logger.py:102` 在**构造那一刻**绑 `sys.stdout`）。修法＝判定期间 `sys.stdout` 让位 stderr + 已绑真 stdout 的 `FT*Log` handler 逐个 `setStream(sys.stderr)` + 只在**入口处取得的**真 stdout 打 token（模块级快照会绕过调用方 `redirect_stdout` 替身，实测打穿 3 例契约测试）。**token 逐字与 exit code 语义均不变** ⇒ `bace63a6` 的字节形态纪律仍成立，改变的只有「噪声去哪」。实测 stdout == `NON_TRADING_DAY`、exit 1，`on_disconnect`（延迟发出，落在 token 之后）也走 stderr。`tests/test_trading_day_check_contract.py` **27/27 全绿**。
- 全量回归：**2 failed / 3236 passed / 37 skipped** —— 2 个 = §8.2 #5/#6 的劳动节伪失败（**9/8 自愈**，非本批）；#4 `except:pass` 棘轮**未再触发**（新代码按 R5 改为 warn 留痕，`BASELINE` 维持 955）；#7 已修。`pyflakes` 对本批 5 个改动文件**零新增告警**（`exit_replay_backtest.py:288/694` 两处为 HEAD 存量，逐字比对确认）。

### 8.5 判据换轨批（§4.9）的测试与回归

- `tests/test_exit_path_replay.py` **45→50 例**：钱口径四指标数值钉子（对称误差 ⇒ `bias/MAE=0`、`MAE/median=0.1`）、符号判对率必须计翻转（把赢判成输）、**零误差不得退化成 `None`**（许可卡把 `None` 判为不许可 ⇒ 完美尺子反被否，本批修掉的除零 fail-closed 缺陷）、**判别力钉子**（路径全错而钱口径满分 ⇒ 不得用后者替换标签门）、**经济等价标签盲区**（`SL_BREAKEVEN↔SL_TRAIL_LOCK` 钱差近零而标签门判失配 ⇒ 两级必须并列）。
- `tests/test_cf_pullback_limit_replay_v2.py` **22→40 例**：三种尺子形态（噪声主导 / 偏差主导 / 方向失明）× 四级许可的发放矩阵；**L1a 与 L1b 必须能分岔**（偏差主导时 a 许可 b 不许可 —— 这条钉的就是「合成一级会连带否掉待裁读数」）；单口径不过 ⇒ 整级不许可（挑口径防线）；指标缺失 ⇒ fail-closed；生效日边界（9/03 ⇒ `PROVISIONAL_INDICATIVE_ONLY`，`MONEY_CRITERIA_BINDING_FROM` 当天 ⇒ 正式）；纪年守卫三态（零复刻轨 ⇒ `NOT_SINGLE_EPOCH` + `epoch_floor` = 末次日期+1 / 门域零样本 ⇒ `None` / 域收窄藏轨 ⇒ 披露但不改判）；`build_rows` 必须给配对样本写 `date`（守卫的换窗起点依赖它，源码钉子）；既有 `evaluability` 字段全部保留（历史 JSON 连续性）；读数卡必须出现四级 + 结论 + 效力、**点估计与统计证据分开说**（不得把「中位数为负」写成「劣于」的结论）、无配对时必须说「没测到」而不是「无效」、域外零复刻轨必须上卡、**尺子许可与裁决资格必须分闸，且两道 n 门必须分开**（n=6 且四级全发放 ⇒ `verdict_eligible=False` + 卡上必须出现「样本不足」「无裁决资格」且**不得**出现「有统计学证据」；n=30 ⇒ 宪章闸过、裁决闸未过 ⇒ 卡上必须出现「未达本裁决门槛」+「不得裁 `entry_archetype_routing_enabled`」+「禁跨窗拼接」；n=40 ⇒ 资格发放、两行警告消失）；**八个预注册常量本身钉死** (`test_preregistered_thresholds_are_pinned`: 生效日 2026-09-08 / 宪章 n=30 / 裁决 n=40 / 0.25 / 0.25 / 0.80 / 0.80 / 5) —— 悄悄挪门必须打红 CI, 不能只改常量就生效。
- 全量回归：**2 failed / 3259 passed / 37 skipped**（143.70s）（2 个仍是 §8.2 #5/#6 劳动节伪失败，9/8 自愈）；`pyflakes` 对本批 4 个改动文件零告警；`app/` 与 `main.py` **零改动** ⇒ 服务未重启；DB 全 `mode=ro`；零历史回填。

### 8.3 只读与幂等实证

- 三个脚本的 DB 连接串均为 `file:...?mode=ro`（钉子逐字锁源码）。
- 输出仅 `data/shadow/{ticket_size_breakeven,trivial_rule_control_arm}_latest.json`，fresh 覆写；重跑读数一致（幂等）。
- 零网络：`trivial_rule_control_arm.py` 钉子断言源码不含 `OpenQuoteContext` / `request_history_kline` / `import futu` / `requests.get`；`exit_path_replay.py` 只吃传入的 bars。
- **未回填任何历史行**；未新增任何生产影子文件写入点。

---

## 九、未做 / 待用户拍板

1. **`admission.min_entry_price` 是否上调**（现 $5 = 渐近线 52.61bps 全表最贵档；股价 <$20 在任何名义额下压不到 13.3bps 以下）。本批**只给数字不给结论**；上调会直接改变候选宇宙 ⇒ 属 live 变更，需独立预注册。**建议：先不动**，等 §五 的三个逃生门里「加净值」这条自然达成（+15.6%）后重算。
2. **`min_notional_usd` 是否回调 2500→3000**（= 费用地板 15.92→13.27bps，代价是吞吐量）。RFC-071 当初为换吞吐量而降；本批量化了这个交换的价格，**未建议回滚**。
3. **对照臂 / 费率账是否入 cron**。**建议：先手动跑，不入 cron**。理由：两者都是「读数型」而非「攒样型」脚本，入 cron 会制造一个每天产生新读数的影子实体，而当前读数已明确「不可分辨」——在尺子换掉之前，每日重跑只是重复同一个负面结论，反而给「攒够样就能判」的错觉续命。
4. ~~**交付物① 的下一步 = 「重放器保真度专项」**~~ **【✅ 已执行 2026-09-07（用户裁「按照建议执行优化」）：三步全部落地 + 新增采样口径双报，读数与新发现的两道结构性上限见 §4.8；结论＝门仍未过（0.786/0.821 两口径须都过），`entry_archetype_routing_enabled` 维持关闭】**（§4.6 三步，全部不碰 live 代码）：① peak 采样与实盘 **10s 轮询同构**（或双报 bar-极值 / poll-同构 作敏感性区间）② 核对锁利轨 `peak_profit_pct` 状态传递（84 笔全落 else 分支，先查状态传递、不要先动 `drawdown_from_peak_pct` 参数）③ 补 TIME_EXIT / MFE_STALL_EXIT / DEAD_SIGNAL_EXIT 三轨（共 57 笔被塞进 `PRE_CLOSE_EXIT`，均不需墙钟）。三步做完过保真度门（≥0.8）后，才可判读 ADR-035 四判据、才可谈 `entry_archetype_routing_enabled` 接线。**修好之前该开关维持关闭。** 是否立项请裁。
5. **「保本漏武装」立案已撤销（§4.6）**：三组数据证明它是**尺子偏差**而非 live 缺陷，故**不建议**改退出路径、也**不建议**提「入场即预挂净保本限价单」的议案（为幻影 MFE 改 live 退出语义，会撞 KR2 cohort 单版本冻结与 C7，且大概率重演 brief §11 的「越紧越亏」）。**需用户确认此撤销**——若仍认为存在实盘武装缺口，则需先补一项本批无法取得的数据：这 8 笔在净保本线的**可成交概率**（需逐笔/盘口，1min bar 的 high/low 只证明价格发生过、不证明有量可成交）。
6. ~~**`trading_day_check.py` stdout 污染**（§8.2 #7）~~ **【✅ 已执行 2026-09-07：采「治根」选项（改脚本、不改测试），token 逐字与 exit code 语义不变，27/27 契约测试全绿，实现与实测见 §8.4】**。原议：是否授权改**脚本**（把 futu SDK 日志从 stdout 引开，token 逐字不变）还是改**测试**（放宽为「token 作为独立行出现」）。前者治根但触碰他批已提交且对字节形态有要求的文件；后者弱化契约。
7. **挑战者 Pass2 / DA prompt 删均值锚 / write-only 字段**：本批刻意未动（要改 prompt 指纹与 live 契约）。挑战者另有 **9/12 B11 裁决窗**；Claude 建议的「AGREE 不再升档」（一行开关、按 DEGEN 棘轮已可判死）属 live 语义变更，**建议排到 9/12 窗一并裁**，不要单独插队（否则一次一可归因单元被打破）。

8. ~~**配对反事实的正式裁决点怎么跑**~~ **【✅ 已裁 2026-09-07（用户裁「按照建议执行」）：采①手动跑，不新增自动累积机制】**。两个跑点与四条分支规则见 §4.9 末与 `handoff.md` 下一步 `0E`：**9/12（六）中期体检**（9/8–9/11，预期 n≈30，只验证三道闸咬合，**不裁决**）+ **9/16（三）正式裁决**（9/8–9/15，预期 n≈45 ⇒ `verdict_eligible=true`）。**已预注册的约束**：n<40 顺延不降门槛、禁把 9/1–9/4 的 30 笔并进来凑数（禁跨窗拼接）、`NOT_SINGLE_EPOCH`/L1a 不许可/两口径异号 ⇒ 不裁决、接线仍须 ADR-035 四判据全过。**未做（需独立预注册，不在本批）**：若裁决结果为「不优于」，第 7 条建议的删字段（`entry_archetype`/`confirmation`/`tripwires` 退出 live 契约）属 live 契约变更，须另开裁决点。

---

## 十、可复现命令

```bash
cd /opt/ftseek/FTSeek-US

# 交付物① 配对反事实（多日 v2；只读，写 data/shadow/）
./venv/bin/python scripts/cf_pullback_limit_replay.py --from-date 2026-09-01 --to-date 2026-09-04
# ↑ 唯一现行判读入口 (§4.9 / REPLAY-SCRIPT-LEDGER「唯一现行判读入口」节)。
#   stdout 末尾的「读数卡」= 本批唯一需要人读的产物；同一份内容在
#   data/shadow/pullback_limit_replay_v2_latest.json 的 summary.readout_card / summary.ruler_license。
#   判读顺序: 先读「效力」行 (指示性 / 正式 / 不可用), 再读「结论」行;
#   ❌ 的那一级对应的那类话不能说, 与 ADR-035 四判据过没过无关。

# 交付物② 客单价费率账（--no-write 只打印不落盘）
./venv/bin/python scripts/ticket_size_breakeven.py

# 交付物③ 笨规则对照臂（--permutations 默认 5000；--no-dedupe 关闭去重）
./venv/bin/python scripts/trivial_rule_control_arm.py

# 保真度专项批（§4.8）: 重放器 40 例 + v2 口径 22 例 + 交易日守卫契约 27 例
./venv/bin/python -m pytest tests/test_exit_path_replay.py \
  tests/test_cf_pullback_limit_replay_v2.py tests/test_trading_day_check_contract.py -q

# stdout 洁净度实证（应只输出一个 token; SDK 连接日志走 stderr）
./venv/bin/python scripts/trading_day_check.py 2>/dev/null; echo "exit=$?"

# 本批测试（5 个文件 127 例）
./venv/bin/python -m pytest tests/test_exit_path_replay.py \
  tests/test_cf_pullback_limit_replay_v2.py tests/test_ticket_size_breakeven.py \
  tests/test_trivial_rule_control_arm.py tests/test_replay_engine_import_smoke.py -q

# 全量回归（预期 3 failed / 3213 passed / 37 skipped；3 个失败见 §8.2 #5/#6/#7）
./venv/bin/python -m pytest -q

# 官方引擎 import 冒烟（§七）
./venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import exit_replay_backtest; print('OK')"

# 劳动节伪失败自证（§8.2 #5/#6）
./venv/bin/python -c "from datetime import date; from app.utils.holiday_calendar import nth_trading_day_after, get_holiday_calendar; \
print(get_holiday_calendar().is_trading_day('US', date(2026,9,7)), nth_trading_day_after('US', date(2026,9,4), 1))"
# → False 2026-09-08

# §四 全 395 笔逐笔重放（8 笔交集 / 7 种定义穷举 / MFE 双侧偏差 / 400 笔出场原因失配表）:
#   探针为一次性脚本, 刻意未入仓 (它只是取证, 不是尺子; 入仓会与 §4.6 的保真度专项重复)。
#   复现方式 = scripts/exit_replay_backtest.load_trades("2026-07-11", None) 取 395 笔
#            + _bar_cache_path 只读 data/replay_bars/ 缓存 (零网络)
#            + 逐笔调 scripts/exit_path_replay.replay_exit (live 入场价/SL/TP/qty/meta)
#   §4.1 八笔 = db_reason=='SL_INITIAL' ∧ 重放∈{SL_BREAKEVEN,SL_TRAIL_LOCK}
#   §4.2 mfe_valid_rate = 盈利单中 mfe_pct>0 的占比 (判据源: app/decision/edge_weekly_report.py:311-315)
#   §4.4 USAR 逐 bar = data/replay_bars/US_USAR_2026-09-04.json (入场根 09:37 不评估, 09:38 high=19.01)
#   §4.5 失配表 = Counter((db_reason, replay_reason)) over 395 笔
# 实盘退出快环生效间隔 (§4.4 勘误的依据):
grep -n "playbook_exit_fast_loop_interval_sec\|monitor_interval_minutes" config/unified_config.yaml
# → 1864:  playbook_exit_fast_loop_interval_sec: 10   (生效值; 代码默认 20 被覆写)
#   1862:  monitor_interval_minutes: 0.25             (通用监控节拍, 非退出武装节拍)
```
