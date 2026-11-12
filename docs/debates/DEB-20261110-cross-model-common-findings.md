---
title: 四模型审计共性点归纳：退出链 SSOT ↔ 引擎 ↔ 账本
status: active
module: risk/exit
related_code: app/risk/exit_policy_guardian.py, app/risk/atr_stop_engine.py, app/risk/position_exit_monitor.py, app/risk/exit_policy.yaml, app/execution/exit_lifecycle.py
tags: [llm, audit, multi-model, exit-chain, consensus, ssot]
updated: 2026-11-10
---

# DEB-20261110 — 四模型审计共性点归纳（退出链）

- 日期：2026-11-10 23:40 CST
- 状态：**只读归纳，生产零变更**（本文不含代码/配置改动，未重启、未写交易库）
- 审计语料：`/root/bench-qwen-eval`（2026-11-10 批次）
  - 新模型：`qwen-latest-series-invite-202610-m9`（17 bugs / 10 logic）
  - 对照：`deepseek-v4.1-flash`（13/6）、`kimi-k3.1-pro`（5/3）、`glm-5.3`（5/7）
  - 原始产物：`results/audit_*.json`、`results/aggregate.json`、`综合评测报告.md`
- 审计对象：`FTSeek-US` 主线退出链（`exit_policy.yaml` + guardian + atr_stop_engine + position_exit_monitor + exit_lifecycle）
- 方法：跨模型聚类 → 逐条对照真实代码复核（静态读码 + 运行时探针）→ 只把 **≥2 模型提出且复核成立** 的记为共性点；单模型独家的已复核实锤单列；被反驳的单列。
- 前序：`docs/debates/DEB-20261105-*`、`docs/debates/DEB-20261028-observability-and-ssot-batch.md`（同为"配置声明 ≠ 消费方"家族）

## 0. 摘要

四份审计的措辞差异很大，但把它们叠在一起，退出链的共性问题不是"某条规则算错了"，而是**三层契约断裂**：

1. **yaml → 引擎**：SSOT 里声明的桶级/方向级参数、floor、成本缓冲开关，进不了定价函数；
2. **guardian → 真相**：启动守卫用文本 grep 自证"规则已实现"，把断裂判成"✅ 校验通过"；
3. **账本/单位 → 观测**：小数与百分数混存、失败态与空值同形，使断裂在报表里看起来"正常"。

4/4 模型命中的 6 个点全部落在这个骨架上；共 11 条共性点（4 条四方、4 条三方、3 条两方）+ 5 条数据层共性 + 7 条"分析质量"共性。

本文与既有评测报告的差异：评测报告已做过一轮取证，但**对 `direction_override` 的定性、`cost_buffer` 的生效时点、`should_earnings_tighten` 的影响面三处与实际代码不符**，见 §7 X2/X3/X4。这三处不是措辞问题，会直接影响修复优先级。

## 1. 判据与取证

- 复核记号：`✅` 代码复核成立；`⚠️` 部分成立/影响被高估；`❌` 复核不成立。
- 覆盖判定按 `bugs + logic_issues` 的实质内容计数（同一模型多处提及只记 1），不按关键词命中。
- 运行时探针（只读，复发命令见附录 C）：

```
# 探针 1：YAML 缺失时的降级策略
ExitPolicyGuardian('/nonexistent/exit_policy.yaml').load(); validate()
→ is_valid=True  rules_enabled=['atr_stop','breakeven','time_exit','earnings_guard']
  fallback.breakeven={'enabled':True,...}  fallback.time_exit={'enabled':True,...}
  fallback 无 pre_close_exit / pre_close / vol_bucket_multipliers

# 探针 2：cost_buffer 在 enabled:false 下的实际效果
entry=100  atr=1.0  atr_mult=2.0  cost_bps=50  → hard_stop=98.0 atr_stop=98.0
cost_buffer_price=0.7500  final_stop=97.25  beyond_cap=True
```

两条探针都不改状态、不连券商，可任意重跑。

## 2. 四方共识（4/4，复核成立）

### C1 YAML 加载失败时降级到"反向策略"，且校验仍通过 — P0

- 机制：`load()` 对 `FileNotFoundError` 与任意 `Exception` 都 `return self._default_policy()`（`exit_policy_guardian.py:64-78`）；`_default_policy()` 启用 `breakeven`（trigger 1.5%）与 `time_exit`（1h / 0.3%），而生产 yaml 因 ADR-008 已把两者明确禁用（`exit_policy.yaml:158-172` breakeven、`:187-192` time_exit），且 fallback 完全不含 `pre_close_exit / pre_close / vol_bucket_multipliers`（`:326-346`）。
- 复核：✅ 探针 1 实测 `is_valid=True`、4 条规则、无 `pre_close_exit`。原因不是"守卫没跑"，而是 `validate()` 只按 fallback 里的 enabled 集合做 grep（`:80-110`），必然通过。
- 影响链：`position_exit_monitor.py:262-263` 把 `guardian.policy` 缓存为 `self._cached_exit_policy`，后续 `_load_exit_policy_dict()`（`:374-376`）全部读这份缓存 → 一次 YAML 损坏 = 盘末退出整段消失 + 保本/时间退出复活，且 `main.py:237-247` 的 LIVE fail-close 拿不到失败信号。
- 为什么是 P0：这是唯一"单点故障 → 静默换一套风控"的路径，且与小节 C3 叠加时，换上去的还是一套参数不生效的策略。
- 最小修复方向：fallback 与生产 yaml 对齐（只留 `atr_stop` + `pre_close_exit` 保守全平 + `earnings_guard`）；`load()` 置 `self.degraded=True`，`validate()` 在 degraded 时强制 `is_valid=False` 并写 `rules_violated`；启动断言 `fallback.exit_rules ⊇ {atr_stop, pre_close_exit}`。

### C2 波动率桶滞回是空实现，缓存是永不复用的类级 dict — P1

- 机制：`_select_vol_bucket()` 把 `hysteresis_pct` 读进局部变量后从未使用，选桶仍是纯阈值扫描（`exit_policy_guardian.py:249-266`）；`_vol_bucket_cache` 在类体声明为可变 dict（`:247`），全仓零读写，且跨实例共享。
- 复核：✅ 静态成立。yaml 注释承诺"±0.2% 防抖动"（`exit_policy.yaml:153`），代码没有这条语义。
- 影响：桶边界在 1.49/1.50/1.51 之间来回跳时，`atr_mult` 与 `max_loss_pct` 跟着跳；docstring 的"带滞回"是**装饰性文档**。目前因为 C3（参数根本不进引擎）影响被掩盖，修 C3 后这条会立刻变成真实抖动源。
- 修复方向：实现 per-symbol 实例级状态（±0.2% 边界带），或删除滞回语义并改注释——**不允许保留"声明了但没实现"的第三种状态**。

### C3 桶级/方向级参数没有进入定价引擎的注入口 — P0

- 机制（三处叠加，模型各看到一段）：
  1. `compute_stop_lines()` 的硬止损只读 `atr_stop.max_loss_pct` / `short_overrides.max_loss_pct`，入参里没有"桶级 max_loss"（`atr_stop_engine.py:230-284`）；
  2. `guardian.get_max_loss_pct(atr_pct)` 没有 `direction` 参数，生产代码 **零调用方**（`exit_policy_guardian.py:268`，全仓 grep 仅定义处）；
  3. `position_exit_monitor.py:1933-1955` 另写了一份内联桶扫描（只求 `atr_multiplier`，LONG 用桶、SHORT 用 `multiplier_by_profit.initial`），与 guardian 的桶逻辑并行。
- 复核：✅ `exit_policy.yaml:128-151` 的 `direction_override`（low 桶 LONG 0.3% / SHORT 0.4%，high 桶 5.1% / 9.0%）在全仓**没有任何消费方**（grep 只命中 yaml 本身）。
- 影响：374 笔数据推导出的分桶×分方向 SL/TP 是当前配置的核心卖点，实际生效的只有 `max_loss_pct: 2.0` 与 `atr_mult=2.0` 两个常数。**配置越精细，SSOT 与行为的落差越大**，且所有回测/复盘都按 yaml 解释结果，归因会系统性偏错。
- 修复方向：收敛为单一 resolver（`resolve_exit_risk(atr_pct, direction, profit_atr, symbol) -> {atr_mult, max_loss_pct, source}`），monitor 与 trailing_stop 都只调它，`compute_stop_lines` 显式接收 `max_loss_pct`；禁止调用方改写共享 policy dict 注入（会引入跨仓位污染）。

### C4 启动守卫的 grep 校验近乎恒真 — P0/P1

- 机制：`_check_rule_implemented()` 走 `_grep_in_daemon()` 对整份源码 `re.search`（`exit_policy_guardian.py:113-160`, `:172-193`）；多数规则配了 OR 兜底，例如 `chandelier_stop` 匹配 `compute_stop_lines`、`breakeven` 匹配 `compute_stop_lines`、`cost_buffer` 匹配 `cost_buffer`（定义文件自身即命中）。注释、docstring、孤立函数都能让规则"通过"。
- 复核：✅ 结构成立。这解释了 C1 的 `is_valid=True`，也解释了为什么 C3 的参数断裂能长期带"✅ 校验通过"上线。
- 影响：守卫的可信度低于它打印的日志；`main.py:237` 的 fail-close 只对"真失败"生效，而这里几乎不会失败——**防线存在，但只对已经写进代码的规则生效，对"接线断了"零覆盖**。
- 修复方向：改为"注册表 + 真实 handler"校验（规则名必须在 `exit_rule_registry` 注册且被 monitor 调用），再用合成持仓对每条 enabled 规则跑一次集成断言；grep 只保留为辅助日志。

## 3. 三方共识（3/4，复核成立）

### C5 cost_buffer 已禁用但仍外扩止损线，且可突破硬止损上限 — P0

- 提出：deepseek（bugs3/4）、kimi（bugs3）、qwen（bugs6）。glm 未提。
- 机制：`compute_stop_lines()` §7 只读 `cb_cfg['k']`，从不检查 `enabled`（`atr_stop_engine.py:393-399`），而 yaml 明确 `cost_buffer.enabled: false`（`:240-242`）；外扩发生在 §6 聚合**之后**，因此 `final_stop` 可以比 `hard_stop` 更松。
- 复核：✅ 探针 2 实测：`hard_stop=98.0`、`final_stop=97.25`，50bps 成本 × k=1.5 = 75bps，**硬帽被突破 25bps**；§9 用 `final_stop` 判触发（`:415-416`），意味着 2% 的最大亏损契约实际是 2.75%。
- 影响：这不是"少赚"，是**单笔最大亏损上限失效**；成本越高、k 越大，偏离越大（SHORT 借券成本高时更明显）。
- 最小修复：`if not cb_cfg.get('enabled', False): k = 0`（或跳过 §7）；并在聚合后 clamp——LONG `final_stop = max(final_stop, hard_stop)`，SHORT 对称；配 `cost_bps=0/50`、`enabled=false/true` 四象限单测。

### C6 盘末两条规则同时 enabled，互斥表既漏又引用不存在的键 — P1

- 提出：deepseek（bugs9）、glm（bugs5）、qwen（bugs16）。kimi 只谈隔夜风险，未提冲突。
- 机制：`pre_close_exit`（window 8min / half_exit 0.5ATR / loss_flat=false）与 `pre_close`（adaptive 5-15min / tolerance 0.3ATR / close_ratio 0.5）同时 `enabled: true`（`exit_policy.yaml:203-235`）；`exit_rules_exclusivity` 只写 `pre_close: [trailing_stop, time_exit]`（`:77-79`），既没覆盖 `pre_close_exit`，又引用了 `exit_rules` 里**根本不存在**的 `trailing_stop` 键。
- 复核：✅ 两边参数确实冲突；互斥表是"腐化的表"，不是"故意留白"（`trailing_stop` 作为规则名在 exit_rules 中查无此项）。
- 影响：盘末 5-15 分钟内的行为取决于 monitor 内部评估顺序，配置读者无法推断；互斥表读起来像"已处理"，实际零约束力。
- 修复方向：二选一（或明确"先 adaptive 后 exit"的优先级并写进 yaml），互斥表加 CI：引用不存在的规则名即失败；`global_hold_protection.allowed_during_grace` 只列 `pre_close` 未列 `pre_close_exit`（qwen logi7）同族。

### C7 atr_floor 双路径：SSOT 里的 70 无人读，引擎读的是已禁用规则的 0.0 — P1

- 提出：deepseek（bugs12）、glm（logi3）、qwen（bugs15）。
- 机制：`atr_stop.atr_floor_pct: 70`（`exit_policy.yaml:117`）全仓无消费方；`apply_atr_constraints()` 实际读 `progressive_trailing.atr_floor_price_pct`（`atr_stop_engine.py:163-210`），该规则已禁用且值为 `0.0`（`exit_policy.yaml:49-51`）→ floor 恒不生效。
- 复核：✅ grep 证实 `atr_floor_pct` 只出现在 yaml 与一个无关回放脚本（`app/playbook/atr_trail_replay.py` 的同名函数），引擎侧没有读它。
- 影响：ATR 极小时止损距离形同虚设的保护**不存在**；同时"已禁用规则的参数仍被消费"属于反向 fail-safe（禁用了一条规则，它的参数还在影响定价）。
- 修复方向：floor 收敛到 `atr_stop.atr_floor_pct` 单一路径并在 yaml 注明单位（价格百分比），删除 `progressive_trailing.atr_floor_price_pct` 或明确标 tombstone。

### C8 exit_reason 词表与 yaml 规则集脱节、模块文档停在旧时代 — P2

- 提出：deepseek（logi4）、glm（logi5）、kimi（logi3）；qwen 只在数据洞察里提。
- 机制：90d `trade_records.exit_reason` 中 `SL_BREAKEVEN(264)/SL_TRAIL_LOCK(156)/DEAD_SIGNAL_EXIT(51)/PATIENT_EXIT_*(8)/EARLY_TIGHT_SL(3)` 都不是主线 yaml 的规则名；`position_exit_monitor.py` 头部 docstring 仍列 Flash Exit / Day Trade Mode / Scale Out / Time Stop 为"活跃规则"。
- 复核：✅ 词表与 yaml 规则集确实不同源（主线规则名是 `BREAKEVEN/HARD_STOP/ATR_STOP/...`，DB 里是 `SL_*` 词族）；yaml 自身 `version 3.0 / last_updated 2026-06-21`（`:41-42`）也落后于 6/30、7/05、7/24 的迭代。
- 影响：任何"按 exit_reason 归因到规则"的分析都可能张冠李戴（评测报告的复算也踩到这点，需要人工映射）。
- 修复方向：`trade_records` 增 `source_chain` + `rule_key` + `policy_version`，由 booking 层断言 `rule_key ∈ enabled 集合`；旧文档移入 ADR 或在文件头加"历史规则"分隔。

## 4. 两方共识（2/4，复核成立但影响有限）

### C9 `_REGIME_MAP` 大写键永久不可达 — P2

- 提出：kimi（bugs5）、qwen（bugs10）。
- 机制：`_normalize_regime()` 先 `str(regime).strip().lower()` 再查表（`atr_stop_engine.py:52-56`），表里 `TREND_UP/TREND_DOWN`（`:40-51`）永远匹配不上，且没有 `trend_up/trend_down` 小写别名 → 静默降级 `normal`。
- 复核：✅ 成立。`RANGE/RANGE_BOUND/HIGH_VOL` 大写键同样不可达，但有等价小写键兜底，实害只有 `trend_up/trend_down` 两个。
- 影响：外部传 `TREND_UP` 时拿到 `normal` 的 trail_mult（2.0 而非 trending 档），趋势行情里止损偏紧。属静默降级，不报错。

### C10 `executescript()` 在显式事务内执行，DDL 脱离事务保护 — P2

- 提出：deepseek（bugs13）、qwen（bugs12）。
- 机制：`ExitLedger.__init__` 在 `with self.transaction()`（内部 `BEGIN IMMEDIATE`，`:90-100`）里调 `db.executescript(...)`（`:52-88`）；Python `sqlite3` 的 `executescript` 会先隐式 COMMIT 挂起事务。连接也未开 WAL，并发只靠 `timeout=10`。
- 复核：✅ 成立；DDL 幂等（`IF NOT EXISTS`），崩溃窗口的实际后果是"建表成功但索引缺失"，不是数据损坏。
- 影响：低。真正值得做的是 WAL + busy_timeout + 启动断言索引存在（qwen opti7），顺手把 executescript 移出事务。

### C11 `should_earnings_tighten(symbol)` 忽略 symbol — P2（不是 P1）

- 提出：glm（logi1，带"消费方未证实"caveat）、qwen（bugs14）。
- 机制：函数体只 `return earnings_guard.enabled`（`exit_policy_guardian.py:316-320`），注释自承"简化实现"。
- 复核：✅ 成立，但 **全仓零调用方**（grep 仅定义处）→ 当前不产生"全市场恒收紧"，产生的是"看起来有财报收紧能力，实际没人问它"。
- 定性：评测报告写"若被消费，等于全组合永久财报收紧"——条件句成立，但**现状是零消费**，按 P1 排期会浪费预算；应并入"死 API 清理"（P2），或接线时连同 earnings_calendar 一起做。

## 5. 数据层共性（4/4 或 3/4 模型从同一份真实统计中读出）

| # | 共性观察 | 覆盖 | 复核 |
|---|---|---|---|
| D1 | 尾部损失集中在 `SL_INITIAL`（40 笔 -$5,653）+ `OVERNIGHT_GAP_STOP`（14 笔 -$4,463），54 笔 ≈ 92% 的毛亏损；隔夜单笔均值是初始止损的 2.26 倍 | 4/4 | ✅ 数字四份一致 |
| D2 | `net_pnl_pct` 存小数、`mfe_pct` 存百分数（0.009 = 0.9%），直接相除会产生 100× 量级误读 | 4/4（deepseek 读错，其余三家识别出） | ✅ 用 `pnl/n` 反推名义本金 ~$6k 可自洽 |
| D3 | 胜率与期望脱钩：7 月 51%→10 月 87%，但 11 月 86% 胜率仅 $0.23/笔（74 笔 ≈ $17） | 4/4 | ✅ 逐月统计一致 |
| D4 | `external_close(3)` / `P3_DUP_CLEANUP(2)` 的 `realized_pnl` 为 NULL，却被计入月度 n 与胜率分母 | 4/4 | ✅ 账务不完整，且污染胜率 |
| D5 | 6 月贡献 6 个月总 PnL 的 61%（77 笔），配置迭代跨月，不能当单一干净样本 | 3/4（glm/kimi 算出 61%，qwen 提及 6 月均值异常） | ✅ 与 D3 同源 |

这五条属于"退出链之外"的共性：**观测层的口径缺陷会把机制缺陷伪装成策略性格**。任何 A/B 或转正判据都必须先修 D2/D4，否则分母错、分子也错。

## 6. 共性误报模式（"分析质量"的共性，同样值得归档）

评测报告已列了逐条反驳，这里只抽**模式**——它们会在下一批审计里复发：

- **P1 不读已有修复**：deepseek 报"组合回撤对无效价格仅 log"（high），而 `position_exit_monitor.py:578-604` 的 DEB-20260904 #191 修复注释就在上下文里。→ 审计前先扫上下文里的 `DEB-/RFC-` 注释与 `# 修复` 标记。
- **P2 误读自身 docstring**：qwen #8 / deepseek #6 把"桶分支提前 return"当缺陷，docstring 原文即"优先级: 波动率桶 > 利润深度 > 做空覆盖"。→ 报"违背契约"前必须引用契约原文。
- **P3 单位陷阱**：deepseek 把 `avg_mfe 1.765%` 与 `avg_realized_net 0.009%` 同单位相除，得出"回吐 99.5%"；kimi/glm 主动识别为小数收益率。→ 跨表数值先反推一次量纲。
- **P4 定级通胀**：纵深防御缺口（qwen #3 direction 裸字符串）定 high；预配置项（deepseek direction_override）定 high。→ 定级必须区分"现网可触发"与"条件性/未接线"。
- **P5 未见调用点即下结论（双向都错）**：scan interval 被 3 个模型报"改 yaml 不生效"，实际 `position_exit_monitor.py:378-392` 明确用 yaml 覆盖类常量；反过来 `should_earnings_tighten` 被当成"恒收紧"，实际零调用方。→ 结论必须写"已查调用点：有/无"，查不到就标 `[待验证]`。
- **P6 把"已文档化"当免罪符或当罪证**：glm 用 yaml:121 注释为 direction_override 免罪，deepseek 把 yaml 已自曝零消费的 `global.max_daily_drawdown_pct` 报 high。→ 文档化只降低"意外性"，不改变"是否生效"；正确判据是**承诺日期与消费方实数**。
- **P7 上下文缺 source→consumer 映射时"造桥"**：与 v5 批次同一教训（`code_analysis_results/v5/comparison_report_v5.txt` 附录四），本轮仍复发（把 `atr_floor_pct` 当活参数、把 `exit_reason` 词表当规则名）。
- **P8 只报个案、不提机制**：四个模型都报了互斥表/单位/死键的个案，无人提出"这些应该由 CI 兜底"（零消费键扫描、互斥表引用校验、单位契约测试）。→ 审计输出若只有 bug 列表，说明还没到根因层。

## 7. 复核增量（四模型均未提出，本轮新增）

### X1 生产路径上 `atr_mult` 恒等于 2.0，整套"利润深度收紧"是死的 — P0 候选

- `trailing_stop.py:42-44` 调用 `policy.get_atr_multiplier(symbol, direction, profit_atr_abs)` **不传 `atr_pct`**（默认 0）→ `get_atr_multiplier` 的桶分支 `if atr_pct > 0` 永不进入（`exit_policy_guardian.py:219-223`）；
- 即便进入，`trailing_stop.py:45` 立刻做 `atr_mult = max(atr_mult, _ssot_mult)`，`_ssot_mult = multiplier_by_profit.initial = 2.0` → 利润深度档位 1.2/1.4/1.6/1.8 **全部被钳回 2.0**（探针：`profit_atr=5` 在 `atr_pct=0` 返回 1.2，low 桶返回 1.5，调用方最终都取 2.0）。
- 结论：`get_atr_multiplier` 的 5 档杠杆、桶 override、sector override 在生产触发路径上只剩"2.0"一个值；monitor 另算的桶 `current_sl` 在 `:1978` 被注释标为"无人读取"。**这是比 C3 更彻底的配置失效**，且所有回测若按 yaml 分档解释都会高估收益/低估回撤。
- 待人工裁决：是"设计上就该 2.0 兜底"，还是 `max()` 写错了方向（应为 `min` 或只在空值时兜底）。**不建议直接改**，先用影子记录 `atr_mult_source` 与分档命中率。

### X2 `cost_buffer` 是 2026-08-24 之后才真正生效的"新回归"

- `trailing_stop.py:135-157` 注释自述：2026-08-24 修了调用签名（原先把 dict 塞进 symbol 位、缺 qty/price → `TypeError` 被吞成 `cost_bps=0`），"成本缓冲自上线从未真正参与过 trail 计算"。
- 含义：C5 的"禁用仍生效"不是长期状态，而是**签名修复带来的副作用**——成本缓冲恢复了计算，却没人回头检查 `enabled` 门。修复时应把这条写进回归测试（`enabled=false → buffer=0`），否则未来再修一处签名又会复发。

### X3 `get_max_loss_pct` / `should_earnings_tighten` 零调用方 → 影响是"假保障"

- 评测报告对 F10 的影响描述（"全市场恒 True"）缺少"当前无消费方"这一层；对 C3 的影响描述（"方向覆盖无法传导"）成立，但更强的表述是：**guardian 的这两个 API 只是文档**。
- 这改变优先级：C3 的修复不是"给 `get_max_loss_pct` 加 direction 参数"，而是"决定这个 API 是接进 resolver 还是删除"。加参数但没人调用 = 又一层装饰。

### X4 `direction_override` 的定性纠正：配置债（P1），不是"文档化即可免罪"

- yaml:121 原文："7/24后 guardian代码消费; 当前yaml预配置"。今天 2026-11-10，**承诺日期已过 3.5 个月**，全仓 0 消费方（grep 只命中 yaml）。
- 评测报告把它归为"文档化的待启用配置，非缺陷"，据此认为 deepseek 定 high 过高。我认为两侧都偏：定性上 glm 对（"注释已声明待启用"），但**一个过期 3.5 个月、注释未更新、且与 374 笔实盘数据绑定的配置**已经不是"预配置"，是配置债；按 P1 排期、修 C3 时一并处置，而不是 high 活缺陷。
- 附带：`multiplier_by_profit` 只有 `initial` 一个键，`profit_5atr/2atr/1atr/05atr` 全部回落代码硬编码 1.2/1.4/1.6/1.8（qwen F9 独家）——与 X1 叠加后，这些硬编码值连"被用到"都算不上。

### X5 归因口径：`HARD_STOP` 标签可能对应比 hard_stop 更松的实际触发价

- §8 归因用 `stop_line`（未计 buffer），§9 触发用 `final_stop`（计 buffer）（`atr_stop_engine.py:401-416`）。C5 成立时，DB 里会出现"reason=HARD_STOP，但实际触发价比 hard_stop 低 0.75%"。
- 影响：把 C5 的亏损归因到"硬止损执行正常"，会掩盖上限被突破的事实。修复 C5 时同步落 `final_stop / binding_candidate / cost_buffer_price` 三列。

## 8. 修复优先级（建议，未执行）

本轮**零改动**。若批准，按"一次一变量 + 影子先行 + 冒烟"推进；每条都给出可验证判据。

| 优先级 | 项 | 最少改动 | 验证 |
|---|---|---|---|
| P0-1 | C5 cost_buffer 破坏硬帽 | `enabled` 门 + 聚合后 clamp hard cap | `cost_bps∈{0,50} × enabled∈{false,true}` 四例单测；探针 2 应显示 `final_stop == hard_stop` |
| P0-2 | C1 降级策略 fail-open | fallback 对齐生产 + `degraded` 标记 + 校验失败即拒启 | 缺失/空/非法 yaml 三例：`is_valid=False`、`rules_violated` 带 `policy_fallback_active` |
| P0-3 | C3/X1 参数注入链 | 单一 `resolve_exit_risk()`，`compute_stop_lines` 显式收 `max_loss_pct` | 落 `atr_mult_source/max_loss_pct_source`，影子 2 周看命中率与 SL 距离变化 |
| P1-1 | C4 grep 守卫 | 注册表 + 真实 handler + 合成持仓集成测试 | 故意断开一条 enabled 规则的接线，守卫必须变红（变异测试） |
| P1-2 | C2 滞回 | 实现或删除，二选一 | 1.49/1.50/1.51 三例单测；桶切换频次指标 |
| P1-3 | C6 盘末双规则 | 二选一 + 互斥表 CI（引用不存在的键即失败） | `pytest` 配置一致性测试；盘末窗口行为 diff |
| P1-4 | C7 atr_floor | 单一路径 + 单位命名 | floor=0/70 两例；确认引擎读的是 SSOT 键 |
| P2 | C8/C9/C10/C11 | 词表/文档、REGIME 别名、WAL、死 API 清理 | 各自单测；`exit_reason` 映射表首日覆盖率 |

不做的事：不重构 exit 模块、不替换 guardian、不改任何阈值。以上都是"让已声明的契约生效"或"删掉不生效的声明"，属减法。

## 9. 落位与索引

- 实体：`docs/debates/DEB-20261110-cross-model-common-findings.md`（本文件）。
- 用户指定路径 `docs/debeate/` 为拼写变体；仓库既有约定是 `docs/debates/`（`docs/DOCS-GUIDE.md` §3）。为让指定路径可直接访问且不产生第二份拷贝，已建目录软链 `docs/debeate -> debates`。若不需要软链可直接删除，不影响本文。
- `docs/debates/README.md` 的索引自 2026-11-06 之后未再维护（11-09/11-10 的 DEB 均未入表），本文不擅自插入，避免索引状态继续失真；如需恢复索引，建议一次性扫描目录重建。
- 本轮动作：只读代码与产物、跑两个只读探针、写本文 + 一个目录软链。**无服务重启、无 DB 写、无券商调用。**

## 附录 A：模型 × 共性点覆盖矩阵

| 共性点 | deepseek | glm | kimi | qwen | 复核 |
|---|---|---|---|---|---|
| C1 降级策略反向 | ✅ | ✅ | ✅ | ✅ | ✅ |
| C2 滞回空实现 | ✅ | ✅ | ✅ | ✅ | ✅ |
| C3 桶/方向参数进不了引擎 | ✅ | ✅ | ✅ | ✅ | ✅ |
| C4 grep 守卫恒真 | ✅ | 附带 | ✅ | ✅ | ✅ |
| C5 cost_buffer 破帽 | ✅ | — | ✅ | ✅ | ✅ |
| C6 盘末双规则/互斥表 | ✅ | ✅ | — | ✅ | ✅ |
| C7 atr_floor 双路径 | ✅ | ✅ | — | ✅ | ✅ |
| C8 词表/文档漂移 | ✅ | ✅ | ✅ | 数据侧 | ✅ |
| C9 REGIME 死键 | — | — | ✅ | ✅ | ✅ |
| C10 executescript | ✅ | — | — | ✅ | ✅ |
| C11 earnings 忽略 symbol | — | ✅ | — | ✅ | ✅（零调用方） |
| 误报：scan interval | ✅ | ✅ | — | ✅ | ❌ 已覆盖 |
| 误报：组合回撤 | ✅ | — | — | — | ❌ 已修复 |
| 误报：docstring 优先级 | ✅ | — | — | ✅ | ❌ 误读 |

## 附录 B：证据索引（file:line）

- `app/risk/exit_policy_guardian.py:64-78` load 的两条 fallback 分支
- `app/risk/exit_policy_guardian.py:80-110` validate 只校验 fallback 内的 enabled 集合
- `app/risk/exit_policy_guardian.py:113-160` 规则实现检查（含 OR 兜底）
- `app/risk/exit_policy_guardian.py:195-243` get_atr_multiplier（桶提前 return、利润深度默认值）
- `app/risk/exit_policy_guardian.py:247-266` 类级缓存 + 滞回读而未用
- `app/risk/exit_policy_guardian.py:268` get_max_loss_pct（无 direction、零调用方）
- `app/risk/exit_policy_guardian.py:316-320` should_earnings_tighten（忽略 symbol）
- `app/risk/exit_policy_guardian.py:326-346` _default_policy（启用 breakeven/time_exit，无 pre_close）
- `app/risk/atr_stop_engine.py:40-56` _REGIME_MAP 大写键 + 先 lower 再查表
- `app/risk/atr_stop_engine.py:163-210` apply_atr_constraints 读 progressive_trailing 的 floor
- `app/risk/atr_stop_engine.py:230-284` compute_stop_lines 硬止损（无桶级入参）
- `app/risk/atr_stop_engine.py:370-399` 聚合后外扩成本缓冲（不检查 enabled）
- `app/risk/atr_stop_engine.py:401-416` 归因用 stop_line、触发用 final_stop
- `app/risk/position_exit_monitor.py:262-263` 缓存 guardian.policy
- `app/risk/position_exit_monitor.py:378-392` yaml 覆盖 scan/cooldown/min_hold
- `app/risk/position_exit_monitor.py:1561-1565` 调用 trailing_stop（真实触发路径）
- `app/risk/position_exit_monitor.py:1933-1955` 内联桶扫描（只 LONG、只 atr_mult）
- `app/risk/exit_rules/trailing_stop.py:36-46` get_atr_multiplier 不传 atr_pct + max(…, 2.0) 钳平
- `app/risk/exit_rules/trailing_stop.py:135-157` cost_bps 签名修复注释（2026-08-24）
- `app/execution/exit_lifecycle.py:14` INTENT_STATES 无失败终态
- `app/execution/exit_lifecycle.py:52-100` executescript 在 BEGIN IMMEDIATE 内；索引定义
- `app/execution/exit_lifecycle.py:612-630` account 缺省 '' → route ValueError
- `app/risk/exit_policy.yaml:41-42` version/last_updated
- `app/risk/exit_policy.yaml:77-79` 互斥表（引用不存在的 trailing_stop）
- `app/risk/exit_policy.yaml:111-153` atr_stop / multiplier_by_profit / atr_floor_pct / hysteresis
- `app/risk/exit_policy.yaml:203-235` pre_close_exit + pre_close 双 enabled
- `app/risk/exit_policy.yaml:240-242` cost_buffer.enabled: false

## 附录 C：只读探针复现

```bash
cd /opt/ftseek/FTSeek-US && PYTHONPATH=. ./venv/bin/python - <<'EOF'
import logging, yaml; logging.disable(logging.CRITICAL)
from app.risk.exit_policy_guardian import ExitPolicyGuardian
from app.risk.atr_stop_engine import compute_stop_lines

g = ExitPolicyGuardian('/nonexistent/exit_policy.yaml'); g.load()
r = g.validate()
print(r.is_valid, r.rules_enabled, 'pre_close_exit' in g.policy['exit_rules'])

pol = yaml.safe_load(open('app/risk/exit_policy.yaml'))
out = compute_stop_lines(direction='LONG', entry_price=100.0, current_price=100.0,
                         atr=1.0, peak_price=100.0, policy=pol, regime='normal',
                         atr_mult=2.0, cost_bps=50.0)
print(out.hard_stop, out.cost_buffer_price, out.final_stop, out.final_stop < out.hard_stop)
EOF
```

预期输出：`True ['atr_stop','breakeven','time_exit','earnings_guard'] False` 与
`98.0 0.75 97.25 True`。

## 10. 2026-11-11 独立复核：范围纠偏与修复重排（只读，未实施修复）

- **复核方式**：Codex 独立读码、全仓调用点搜索、只读 SQLite 查询，并重跑 C1/C5 探针；
  另跑 C9/X1 纯函数探针。未改代码/配置、未写交易库、未重启、未连券商。
- **总判**：若干代码现象成立，但原文把**遗留 ExitMonitor 主链、当前 Playbook 出场链、
  观测统计**混为一个影响面，P0 数量与部分实害描述过高；**不建议按 §8 原顺序直接实施**。

### 10.1 首要边界：当前 DA/EXT 仓不受这份 YAML 管辖

- `app/risk/exit_policy.yaml:8-13` 明写 `PLAYBOOK_SIM/PLAYBOOK_LIVE` 不读本文件，而走
  `app/playbook/exit_engine.py` 与 Playbook 配置。2026-11-11 只读查询 `trade_records`
  得当前未平仓 **9 笔，全部 `PLAYBOOK_SIM`**。
- 因而 C1–C9/C11 主要是**遗留主线/手动仓的代码与配置债**；不能据此宣称当前 DA/EXT
  正在承受这些退出缺陷。C10 的 `ExitLedger` 是 Playbook 共享设施，属于例外。
- 同次 90d 查询显示 `SL_INITIAL/OVERNIGHT_GAP_STOP` 来自 `PLAYBOOK_SIM`，而
  `external_close` 来自 `POSITION_SYN`。D1 可用于另开 Playbook 只读回放，**不能反向充当
  C1–C9 的线上实害证据**；D3/D5 也须先按 `source_type`、配置 cohort 与 A9/A10 口径重算。

### 10.2 逐项裁决与优先级修正

| 项 | 独立复核 | 修订结论 |
|---|---|---|
| C1 | 探针复现：缺失 YAML 时 `is_valid=True`，fallback 复活 breakeven/time_exit 且无 pre_close_exit | 代码问题成立，当前优先级 **P1 而非 P0**。先加 `degraded` 并让校验失败；同时把启动门 scope 到实际使用 legacy policy 的链路，不能让无关的 Playbook LIVE 被全局拒启。fallback 改阈值/盘末全平属另一项行为变更，不同批上线。 |
| C2/C3/X1 | 桶/方向键零消费、`atr_pct` 未传、`max(..., initial)` 后探针各利润档均为 2.0，静态现象成立 | **不是直接修 bug 的 P0**。`max()` 在调用点明写“不低于 initial”的地板语义，改成 `min` 没有契约依据；激活桶/方向/利润档会改变退出分布，须独立裁决 + 影子 + pre/post cohort。当前先做 stale 配置标注。 |
| C4 | grep 可命中注释/孤立实现，确实不能证明 enabled 规则已接线 | **P1 校验真实性**；与 C1 同属“守卫说真话”，但注册表/集成断言不能冒充 Playbook 覆盖。 |
| C5 | LONG 探针 `98→97.25`，SHORT 对称 `102→102.75`，且 YAML `enabled:false` | 确定性代码 bug，当前 **P1**。第一批只修 `enabled` 门并补 LONG/SHORT 四象限测试；是否把成本也纳入 hard cap 是另一契约，**clamp 不同批、不默认直接加**。 |
| C6 | `check_adaptive_pre_close_exit()` 全仓无生产调用方；真实入口只调 legacy `check_pre_close_exit()`；互斥表也无消费方 | “双规则同时执行、取决于评估顺序”**不成立**。应改为 dead adaptive 配置/函数 + 腐化互斥声明，降 **P2 清理**，不做盘末行为切换。 |
| C7 | `atr_floor_pct:70` 无消费、`apply_atr_constraints()` 无生产调用方成立；但 monitor 另有 effective ATR floor、0.5% price floor 与 median TR floor | “ATR floor 保护不存在”**不成立**。`70` 单位未定义，禁止直接接线为 70%；按 dead key/单位债 **P2** 处理。 |
| C8 | DB 的 `SL_*` 主要来自 Playbook，本来就不会等于 legacy YAML 规则名 | 跨 SSOT 对比的“脱节”推断需撤回。保留 source-specific 归因/文档改进为 **P2**；不应增加“所有 `rule_key` 必须属于这一份 enabled 集合”的错误全局断言。 |
| C9 | 纯函数确把 `TREND_UP/trend_up` 降为 normal；但唯一生产 engine 调用方已先把大写枚举标准化为 `trending` | 当前主路径实害未成立，降 **P2 API 健壮性**，补小写别名与单测即可。 |
| C10 | `BEGIN IMMEDIATE` 内 `executescript` 成立；账本由 Playbook 共享，但 DDL 幂等、初始化故障窗低 | **P2 独立小修**：移出显式事务 + 索引存在性测试；WAL 是另一基础设施变量，不顺手捆绑。`timeout=10` 已有 busy timeout 语义。 |
| C11 | 定义外零调用方 | 无现网行为可修，**P2 死 API 处置**；若退役须走仓规零引用审计。 |

### 10.3 数据层不是“先改库才能做 A/B”

- D2 的单位差异真实：Playbook `mfe_pct` 以百分数写入，`net_pnl_pct` 以小数收益率写入。
  但这是读侧契约问题，**不建议迁移/改单位**，否则会破坏既有消费者；应在分析入口集中转换并加量纲契约测试。
- D4 的 NULL 计入朴素胜率是报表查询问题；现行 OKR 的 A9 谓词已经要求
  `COALESCE(pnl_net, realized_pnl) IS NOT NULL`，官方 KR 不受该 NULL 分母污染。修漏网报表即可，
  不给 `external_close/P3_DUP_CLEANUP` 编造盈亏。

### 10.4 修订后的建议顺序（均待用户批准，尚未执行）

1. **Batch A / 单变量**：C5 只修 disabled 仍生效，不动 hard-cap 语义。
2. **Batch B / 单变量**：C1 `degraded` + C4 的真实接线校验按 legacy scope 收敛；不替 fallback 发明新策略。
3. **Batch C / 独立共享设施小修**：C10 初始化事务与索引契约，单独测试、单独部署。
4. **P2 清理**：C6/C7/C8/C9/C11 与 D2/D4 读侧契约；不为清理顺带改变任何阈值。
5. **需另案影子**：只有决定启用 legacy 桶/方向/利润档时才做 C2/C3/X1；当前 DA 净期望工作不应被这条遗留链劫持。
