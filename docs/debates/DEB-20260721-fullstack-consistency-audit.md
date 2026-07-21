# 议题：全域一致性审计 — 时区/量纲/配置遮蔽/fail-open/SSOT 分裂的根因清单与修复辩论

## 状态

- [ ] 进行中
- [ ] 待决策
- [x] 已决策（批次① 已执行; 批次②/意图值裁决 保留待决）
- [ ] 已验证
- [ ] 已关闭

## 背景

用户质疑："为什么只有主动反问才会找到细节问题?成败在于细节。" 遂发起一次**全域对抗式审计**,由 4 个专项 agent 分轴排查(时区一致性 / 量纲·标尺 / 配置遮蔽·冲突 / fail-open 异常·逻辑矛盾·SSOT 分裂),Qoder 对最高危项**逐条 grep 亲验**。

系统背景(供辩论 agent 建立上下文):
- 服务器运行在**北京时区 CST(+08:00)**,美股交易时区 **ET(-04:00 EDT / -05:00 EST)**。北京比 ET 快 12–13 小时。
- 美股盘中(9:30–16:00 ET)= 北京**次日**凌晨(22:30–06:00 次日)。因此任何用 `datetime.now()`(naive=北京)或 `date.today()`(北京日期)去查 ET-keyed 数据,盘中会**差一天**。
- 规范的 trade_date 写入 = `_et_trade_date(now_et())`(见 `app/utils/timezone.py:85-93`,对 naive CST 减 12h 得 ET 日期)。
- 两套时区工具:`app/utils/timezone.py`(`to_et` 把 naive 当 CST/北京 —— **本系统正确**);`app/utils/datetime_utils.py`(`to_et` 把 naive 当 UTC —— 仅用于 DB ISO 值)。
- 规范量纲:`trade_records.pnl_pct` = **比例(0-1)**;`trade_records.mfe_pct/mae_pct` = **百分比(0-100)**;`signal_confidence` = 比例(0-1);battle-plan `targets[].confidence` = 百分比(0-100)。

**审计方法学说明(供 agent 判断可信度)**:
- 标注 `[已亲验]` = Qoder 已用 grep/Read 核对源码双侧确认。
- 标注 `[agent报告-未亲验]` = 来自 subagent 分析,Qoder 未逐条核对,**辩论时应先复核**。
- 已在本轮修复并提交的项单独标注,不在待辩论范围,仅供背景。

## 目标

本次辩论希望回答:
1. 每个 `[agent报告-未亲验]` 项是否**真实成立**(先复核证据,再讨论)。
2. 对成立项,修复的**正确方案**是什么?是否触及策略参数(需人审批)还是纯正确性 bug(可直接修)。
3. 修复的**优先级与批次**,以及**回滚/历史数据污染**风险。
4. 系统性根因(PlaybookConfig 遮蔽机制、fail-open 偏置)是否应做**架构级修复**而非逐点打补丁。

## 数据与实证

### 已修复并提交(背景,不辩论)

| commit | 内容 |
|---|---|
| `a79b042d` | P0 时区(short_strategy/position_sync/position_monitor)+ pnl_pct 去×100 + 今日日期统一 ET(trade_store/feishu_push/weekly_report/order_verifier)+ 量能门配置化 + dashboard confidence 0.5→60 + 死配置删除 |
| `6898d9fc` | 3 个风控熔断器盘中失明: date.today()(北京)→ ET trade_date (account_circuit_breaker / playbook.risk_circuit_breaker / drawdown_control)。实证: `_et_trade_date()=2026-07-20` vs `date.today()=2026-07-21`,正是差一天 |

历史数据回填: 36 行(3 holding_hours + 33 pnl_pct 百分比→比例)。

---

## 可选方案(按发现分组;每组既是"发现"也是"待辩论修复方案")

> 说明:下面每个 finding 给出 **证据 / 影响 / 修复建议 / 争议点**。辩论 agent 应对每项给出:成立性判定、方案取舍、优先级。

---

### A 组 — 配置 dotted-path 拼写错(声明值永不生效)

#### A-1 `[已亲验]` MIN_EFFECTIVE_STRENGTH 路径拼写错
- **证据**: `app/core/config_keys.py:20` = `"risk.pre_trade.min_effective_strength"`,但 YAML(`unified_config.yaml:1419`)是 `min_effective_strength_floor: 5`。消费者 `scheduler_triggers.py:496` / `scheduler_core.py:1002` 用该常量 → 路径不匹配 → 恒回退字面量 `50`。
- **影响**: YAML 声明的 5 是死值;最小有效强度门恒为 50(链式 fallback)。SSOT 常量层出现漏洞。
- **修复建议**: 改常量为 `"risk.pre_trade.min_effective_strength_floor"`;**但注意** 5→50 是行为变更,需确认哪个是意图值。
- **争议点**: 意图强度门到底是 5 还是 50?若一直跑 50 且表现可接受,改成 5 反而放松门槛 → 需数据支撑。

#### A-2 `[已亲验]` position_monitor 读不存在的 `risk.position.max_position_pct`
- **证据**: `position_monitor.py:1650` = `cfg.get_dotted("risk.position.max_position_pct", 0.15)`。YAML `risk.position` 块(1408-1411)只有 `max_per_direction/max_per_symbol/max_total`,**无** `max_position_pct`。真实单仓上限在 `risk.position_sizing.max_position_pct=0.12`。
- **影响**: 对账循环用 15% 上限,而开仓侧用 12% → 仓位可比开仓门大 25%,对账/开仓不一致。
- **修复建议**: 路径改 `risk.position_sizing.max_position_pct`(→ 生效 0.12)。
- **争议点**: 对账用更松的 15% 是否有意(容忍已开仓漂移)?还是纯 bug?

#### A-3 `[已亲验]` order_executor TP 分层/移动止损路径不存在
- **证据**: `order_executor.py:2334-2338` 读 `risk.stop_loss.default` / `risk.take_profit.tier1/2/3` / `risk.stop_loss.trailing_atr_mult`,YAML 全无(只有 `default_stop_loss_pct:0.05@1559` 和 `default_take_profit_pct:0.1@1900`,名字对不上)。行 2326 注释声称"P0-3 fix 用正确 get_dotted 路径"—— 实际路径仍错。
- **影响**: TP 三档(0.08/0.15/0.25)与 trailing ATR 倍数(2.0)写死不可调,直接影响 PnL 落点。改 YAML 的人改的是死键。
- **修复建议**: 二选一 —— (a) YAML 补齐这些键;(b) 代码路径改指向现有 `default_stop_loss_pct`/`default_take_profit_pct` 并新增 tier 键。
- **争议点**: 当前写死值是否就是期望值(那只需删死路径注释)?还是需要真正做成可调?

#### A-4 `[已亲验]` execution.twap_* 路径不存在 → TWAP kill-switch 失效
- **证据**: `order_executor.py:538-542` 读 `execution.twap_threshold_usd`/`twap_slices`/`enable_twap` 等,YAML `execution:` 块(453-475)无这些键(只有 `order_type.twap_min_value:100000`)。
- **影响**: ≥$5000 的单恒触发 TWAP 拆分(用写死常量),**无法从配置关闭**。文档声称的 kill-switch 是死路径。
- **修复建议**: YAML 补 `execution.enable_twap` 等键,或代码改指向 `order_type.twap_min_value`。
- **争议点**: 模拟盘阶段 TWAP 是否必要?阈值 $5K 是否合理?

---

### B 组 — 量纲·标尺跨表分裂 `[agent报告-未亲验,需先复核]`

#### B-1 `trade_outcomes.pnl_pct` 存百分比 vs `trade_records.pnl_pct` 存比例
- **证据(待核)**: `decision_recorder.py:321` = `((exit-entry)/entry*100)`(百分比);规范 `pipeline.py:3779` 文档为比例。
- **影响**: 跨表 join/聚合/阈值过滤差 100 倍。
- **修复建议**: 去掉 `*100`,统一比例;需评估历史数据与消费者。
- **争议点**: 该表消费者是谁?改写入是否破坏已训练模型/历史分析?

#### B-2 `signal_outcomes.pnl_pct` 存百分比(order_executor:5313 `pnl_pct*100`)
- **证据(待核)**: 同一函数向 trade_records 写比例(4992/5002),却向 signal_outcomes 写 `*100`。
- **影响**: 信号质量分析跨表差 100 倍。
- **争议点**: 同 B-1。

#### B-3 `max_drawdown` 一列混美元/百分比
- **证据(待核)**: `order_executor.py:5005` 写 `max_adverse_excursion`(美元);`pipeline.py:3798` 写 `_mae_pct`(百分比)。同列两种单位。
- **影响**: 该列的 AVG/WHERE/排序无意义。
- **修复建议**: 定一种单位并在写入侧断言。
- **争议点**: 选美元还是百分比?两条平仓路径谁改?

#### B-4 MFE/MAE 三表三单位
- **证据(待核)**: position(美元)/ trade_records.mfe_pct(百分比)/ playbook_instructions(美元)/ shadow_tracker(美元)。`edge_weekly_report.py:212` 已知需 `mfe_pct > pnl_pct*100` 对齐。
- **影响**: 命名 `mfe_pct` vs `pnl_pct` 同名不同标尺,极易误用。
- **争议点**: 是否重命名列(如 `mfe_ratio`)还是全转比例?

#### B-5 报表 `:.2%` 作用于百分比标尺数据(显示 100x)
- **证据(待核)**: `post_market_summary.py:73/86/91/105` 与 `pre_market_strategy.py:123` 用 `:.2%`,若上游 `daily_health_report.py:164` 已 `*100` 存百分比 → 再×100 显示。**Qoder 已确认代码位置,但未确认调用方传入的数据标尺**。
- **争议点**: 需确认 `trading_stats` 的实际来源标尺。若来源是比例则无 bug。

---

### C 组 — fail-open 异常吞噬(安全门静默放行)`[agent报告-未亲验,需先复核]`

> 系统性观察: 风控栈普遍"内部检查异常 → 放行"(注释频现"放行""不阻断""避免误杀")。单个 `MarketStateChecker` 的 datetime bug 可同时废掉 `pre_trade_check.py` 里 6+ 个门。

| 编号 | 位置 | 门 | fail 模式 | 备注 |
|---|---|---|---|---|
| C-1 | pre_trade_check.py:186-192 | VIX/SPY regime | OPEN | **注: VIX≥25 软门是有意设计(2026-06-04 决策,改警告+下游缩仓),但 SPY block 一并被吞是否有意需辩** |
| C-2 | pre_trade_check.py:630-645 | market_circuit_breaker | OPEN | 市场级熔断被吞→暂停期仍开新仓 |
| C-3 | account_circuit_breaker.py:81-87 | peak_equity 取值 | OPEN | capital_safety 异常→peak=current→dd_pct 恒 0 |
| C-4 | risk/engine.py:281-298 | 仓位集中度限制 | OPEN | 异常→跳过限制→return True |
| C-5 | signal_pipeline.py:1386-1391 | 价格边界 | OPEN | 内外层均 return True |
| C-6 | position_exit_monitor.py:776-780 | is_strategy_managed fallback | OPEN(误平) | 模块导入失败→全视为 managed→手动仓可能被自动平 |
| C-7 | playbook_service.py:709-719 | 绝对熔断 | OPEN(默认) | acb_fail_mode 默认 open,连读配置都 fallback open |
| C-8 | order_executor.py:3731 | 重复仓位守卫 | OPEN | 异常→可能开同向重复仓 |

- **修复建议(分歧大)**: 危险门改 **fail-close**(异常时阻单)?还是保留 fail-open 但**必须告警+降级监控**?
- **核心争议**: 模拟盘阶段"fail-open 收集样本" vs 实盘"fail-close 保命"。是否按 sim/live 分流?C-6(误平手动仓)方向与其他相反(fail-open 导致**多平**),优先级应更高。

---

### D 组 — 逻辑矛盾 / 方向符号 `[agent报告-未亲验,需先复核]`

#### D-1 weekly_optimizer SHORT 方向符号算反
- **证据(待核)**: `backtest/weekly_optimizer.py:266` = `(exit-entry)/entry`,只对 LONG 正确;SHORT 盈利被算成亏损。系统其他处均方向感知。
- **影响**: 优化器系统性低估/惩罚做空策略。
- **争议点**: 该优化器输出是否真在用于选择?若已停用则降级。

#### D-2 `compute_confidence()` 实为数据完整度评分,非预测置信度
- **证据(待核)**: `instruction_generator.py:506-538` 返回 = 数据质量+条件验证(0-90),但存入 `confidence` 列并被 feishu 推送门(≥70)、IC 分析当预测置信度用。`pipeline.py:1104` 注释已承认"confidence=90 但胜率 31%"。
- **影响**: IC 分析在错误标签量上做,推送门基于无预测力的分数。
- **争议点**: 这是命名/语义问题还是需重构评分?短期是否只需改门槛来源?

#### D-3 `scale_out.py:298` `sign = "+" if LONG else "+"` 两分支相同
- **证据(待核)**: 仅影响中文日志显示字符串。
- **影响**: 纯显示,cosmetic。

---

### E 组 — 剩余时区项(未修,分级)`[agent报告,部分已亲验双侧]`

| 编号 | 位置 | 问题 | 级别 |
|---|---|---|---|
| E-1 | web_dashboard/order/executor.py:663, validator.py:241 | "今日"订单过滤/日限用北京 date → 盘中漏计,日限可被绕过 | P0(需核) |
| E-2 | web_dashboard/order/audit.py:238/323, thesis.py:230, position_recovery.py:280 | 硬编码 UTC-4(EDT),EST 冬令时差 1h → trade_date 边界错 | P1 |
| E-3 | execution/options_selector.py:24, greeks_calculator.py:196 | DTE 用北京 date → 差 1 天 → 影响 Greeks/期权筛选 | P1 |
| E-4 | core/models.py:416 | holding_hours fallback naive−aware 可能 TypeError | P1 |
| E-5 | analysis/voting_accuracy_tracker.py:126/149, signal_ic_tracker.py:249 | 预测日期用北京 → 与 ET trade_date 错配 | P1 |
| E-6 | earnings_calendar.py / attribution_scheduler.py / dual_mode_params.py / sec_form4_provider.py / pipeline.py 4399/4601 dedup | 各类北京 date 边界 off-by-one | P2 |

---

### F 组 — 系统性根因(架构级,供辩论是否值得动)

#### F-1 PlaybookConfig.get 的"静默遮蔽机"设计
- **证据 `[已亲验模式]`**: fallback 链 = `YAML > 调用方显式默认 > DEFAULTS`。任何 `pb_cfg.get(key, 显式默认)` 都绕过 DEFAULTS;若 playbook 节缺失/损坏,`get_dotted("playbook")` 静默返回 `{}`,则所有 `pb_cfg.get` 返回字面量第二参,而非 DEFAULTS。2026-07-10 只对个别 key 打了补丁(把值显式写进 YAML),同模式还有 9 处(daily_notional_cap_pct 默认 0.20 vs YAML 0.80 等)。
- **争议点**: 是否应(a) 让 `get` 在 YAML+DEFAULTS 双缺失时抛错;或(b) 删除 `default` 参数强制走 DEFAULTS?这是根因级修复但改动面大。

#### F-2 ConfigWriter.PARAM_MAP 写入死路径
- **证据(待核)**: `config_writer.py:101-112` 自动调参写 `risk.portfolio.{warn,reduce,stop}_drawdown_pct`,但 YAML 无 `risk.portfolio` 块,消费者 `position_exit_monitor.py:526-528` 也读同样不存在的路径 → 自动调参写入是 no-op,组合回撤门(FLAT_ALL/REDUCE_50)不可调。
- **争议点**: 补 YAML 块还是废弃该自动调参项?

#### F-3 fail-open 系统性偏置(见 C 组)
- 是否需要一个统一的"安全门异常策略"抽象(sim=open+告警 / live=close),而非每处各自 `except: pass`。

---

## 关键分歧(供辩论聚焦)

1. **纯正确性 bug vs 策略参数变更的边界**:A 组配置修复会改变**生效阈值**(如 5↔50、12%↔15%)。这算"修 bug 恢复意图"还是"调策略参数需人审批+数据验证"?
   - 观点甲:路径拼错导致的值本就是意外,修回声明值 = 恢复意图,应直接修。
   - 观点乙:系统已在错误值上跑出了当前表现,盲目"修正"等于未经回测的调参,可能变差。
   - 可验证方式:对每个 key 拉取该值影响的历史交易分布,对比声明值 vs 实际值的反事实表现。

2. **fail-open vs fail-close**:模拟盘阶段是否容忍 fail-open 以收集样本?实盘是否必须全部 fail-close?C-6(误平手动仓)是否应无条件优先修?

3. **量纲统一的历史数据处理**:B 组改写入侧后,历史 `trade_outcomes/signal_outcomes` 的百分比数据是否回填?是否会污染已训练模型/IC 序列?

4. **根因级 vs 逐点补丁**:F-1(PlaybookConfig 遮蔽)是否值得做架构级修复(风险:回归面大),还是继续逐 key 显式化(风险:漏网+复发)?

5. **审计可信度**:`[agent报告-未亲验]` 项在采纳前是否必须由辩论 agent 独立复核 file:line 双侧证据(避免 subagent 幻觉)?

## 决策

- 选择方案: **批次① 已执行**(2026-07-21, 用户授权"达成一致就执行"): C-6 fail-open→安全侧 / E-1 日限 ET 化 / A-1~A-4 "修路径锁现值" / D-3 显示修复。全部零行为变更(生效值不变, 仅消除死路径与危险 fallback)。
- 决策依据: 审计发起方对 A 组的唯一顾虑是"改变生效阈值需审批", "锁现值"方案将当前实际生效值写入 YAML, 顾虑消除; C-6/E-1 双方均标优先且复核证实。
- 保留空间: **批次②③ 已执行**(批次② 2026-07-21: C-7 fail-close / D-1 / B-1 方向盲+回填 / F-1 审计脚本, 见第 3 轮; 批次③ 2026-07-21: GATE-1 OKR 熔断纳入 acb_fail_mode / Gate4/5/6 失明可观测性, 见第 5 轮)。仍保留: A 组意图值裁决(测量建议维持 50)、B 组量纲迁移(建议改为约定文档化)、D-2 列重命名、GATE-2/3/4、F-3 统一抽象、E-2~E-6、冻结解除 checklist。
- 负责人: Qoder
- 截止时间: 批次①② 已完成; 剩余项(意图值/量纲文档化/D-2 标注)待用户裁决后排期

## 执行项(候选,待辩论后确认与排序)

- [x] A-1~A-4:配置路径修复 — **批次① 完成"修路径锁现值"**(config_keys.py A-1 / YAML 补 max_position_pct·stop_loss·take_profit·twap 键, 全部锁定原实际生效值, 零行为变更); 意图值裁决(5vs50 等)保留待反事实数据
- [x] B-1:SHORT 方向盲修复+18 行回填(批次② 完成; ×100 量纲未动) — B-2~B-5 裁决更新: 实测约定分裂证实但样本小, 建议文档化约定+读取侧适配, 不迁移
- [ ] C-1~C-8:fail-open 门策略(需 sim/live 分流决策)
- [x] C-6:is_strategy_managed fallback True→False + ERROR 告警(对齐同文件 L679 已验证安全模式)
- [x] C-7:acb_fail_mode open→closed(批次② 一行配置, 熔断器 fail-close)
- [x] D-1:weekly_optimizer SHORT 符号(批次② 顺手修, 休眠 bug 零行为变更)
- [ ] D-2:compute_confidence 语义/命名(测量已证实: conf 与胜率无正相关且中段倒挂 18.9%; 重命名推迟, 先消费侧标注)
- [x] D-3:scale_out sign 显示修复(按 pnl_pct 符号)
- [x] E-1:web_dashboard 日限/今日过滤 北京 LIKE→ET 交易日窗口 [D 12:00, D+1 12:00) 北京区间(executor.py/validator.py; 已核实写入侧为 naive 北京, 实证 07-16T00:03 与 23:58 分属两个 ET 交易日)
- [ ] E-2~E-6:剩余时区(分批)
- [x] F-1:PlaybookConfig 遮蔽机制(批次② 完成: 只读遮蔽审计脚本 scripts/audit_config_shadowing.py 替代架构级修复)
- [ ] F-2:ConfigWriter 死路径

## 验证项

- [ ] 每个 `[agent报告-未亲验]` 项由辩论 agent 独立 grep 双侧证据复核,标注"证实/证伪"。
- [ ] 每个拟修配置项附反事实数据(声明值 vs 实际值对历史交易的影响)。
- [x] 修复后语法校验 + `systemctl restart ftseek-us` + 启动日志无错 + 对账 errors=0。（批次① 2026-07-21 13:57 PID 109163: AST 5/5, 配置解析 10/10, 对账 errors=0, 无 traceback）（批次② 2026-07-21 14:24 PID 113972: AST 3/3, acb_fail_mode=closed 解析确认, 对账 errors=0, 无 traceback）（批次③ 2026-07-21 15:17 PID 120998: AST 2/2, acb_fail_mode=closed 复核, 对账 errors=0, 无 traceback）
- [ ] 何时关闭:待辩论产出分级执行清单并经用户批准。

## 讨论记录

### 第 1 轮

- **Qoder(审计发起)**:完成 4 轴全域审计。已亲验并修复 3 熔断器盘中失明(commit 6898d9fc,纯时区正确性)+ 前序三批(a79b042d)。已亲验但未修的配置拼写错(A-1~A-4,改变生效阈值,待审批)。其余 B/C/D/E/F 组为 subagent 报告,**部分未亲验,辩论前须复核**。核心待决:正确性修复 vs 策略调参的边界、fail-open 策略、量纲历史数据处理、是否做 F-1 根因级修复。
- **辩论 Agent 1 (Qoder-复核, 2026-07-21)**: 对 5 个代表项独立 grep 双侧复核, 结果: **证实 3 / 降级 1 / 新发现 1**。
  - 复核明细: A-1 证实 (config_keys.py:20 vs YAML:1419 双侧亲验)。B-1 证实且**发现审计遗漏**: 文件实际在 `app/storage/decision_recorder.py`(非 app/decision/), L321 除 ×100 外, 公式 `(exit-entry)/entry` **无方向感知 — SHORT 在 trade_outcomes 符号也是反的**(双重缺陷, 该表可信度比审计评估更低)。B-2 证实但定性变更: L5313 注释自述"# 存储百分比" — 是**有意的表内约定**非笔误, 应按"约定分裂治理"而非"修 bug"裁决。C-6 证实 (app/risk/position_exit_monitor.py:776 `except: is_strategy_managed = lambda p: True`)。**D-1 降级为休眠 bug**: 该公式仅是 `pnl_pct IS NULL` 的 fallback (weekly_optimizer.py:263-267 优先用 DB 已算好的方向感知值); 实测 437 笔已平仓 NULL=0 — 从未走过此路径, "系统性惩罚做空"不成立, 降 P3 顺手修。
  - 分歧 1 (A 组边界): 支持**"修路径、锁现值"拆分原则** — 立即修正路径对齐, 但把**当前实际生效值**写进 YAML 作为新声明值 (零行为变更, 先消除 SSOT 谎言); 要不要把值改回原意图 (5↔50, 12%↔15%) 是调参, 等反事实数据走冻结-测量-裁决流程。此原则可统一处理 A-1~A-4/F-2。
  - 分歧 2 (fail-open): sim 期保留 fail-open 但**每处必须加 WARNING+计数器**(否则永远不知道门失明了多少次); 两个例外: **C-6 无条件优先修**(方向相反, fail-open 导致多平手动仓, 唯一"越开越危险"项), **C-7 绝对熔断器应 fail-close**(熔断器本职是保命, 失明的熔断器=没有熔断器)。F-3 统一抽象 (sim=open+告警/live=close) 方向正确但推迟到实盘准备期, 现在只加告警。
  - 分歧 3 (量纲历史): 先盘点消费者再动; 统一时**写入侧+历史回填必须一次性完成**(分批会造成表内双标尺混存, 比现状更糟); trade_outcomes 因叠加 SHORT 方向盲, 建议评估"新列重建"而非原地改写。
  - 分歧 4 (F-1): **反对现在做架构级修复**(改 get 语义回归面=全部 pb_cfg.get 调用点)。中间路线: 写一个**只读遮蔽审计脚本**(遍历所有 pb_cfg.get(key, default) 调用点, 对照 YAML+DEFAULTS 报差异), 一次性暴露全部 9 处并可进 CI 防复发 — 成本一个脚本, 不碰运行时, 符合"必要性驱动而非形式主义"原则。
  - 分歧 5 (可信度): **必须复核** — 本轮抽验即证据: D-1 被降级(压噪) + B-1 挖出方向盲(增量), 复核同时有双向价值。
  - 批次建议: ① 立即(纯正确性零行为变更): C-6 / E-1(日限绕过先核实) / A组修路径锁现值 / D-3; ② 需单点决策: C-7 fail-close / B组消费者盘点后统一 / D-2 先只换推送门来源(列重命名推迟); ③ 推迟: F-1 用审计脚本替代 / F-3 实盘前 / E-2~E-6 分批 / D-1 顺手。A组意图值裁决(5vs50等)在反事实数据前不动。
- **<辩论 Agent 2>**:（待填)

### 第 2 轮 — 批次① 执行记录 (2026-07-21, Qoder)

- 用户授权"达成一致就执行"。发起方与复核方在批次① 上一致(发起方对 A 组唯一顾虑"改变生效阈值"被"锁现值"方案消除), 执行:
  1. **C-6**: `position_exit_monitor.py:776` fallback `True→False` + `logger.error` 告警, 照抄同文件 L679 已验证的 P0-3 安全模式(该处为漏修的第二份拷贝)。
  2. **E-1**: 复核证实且更实 — 写入侧 `datetime.now().isoformat()`(naive 北京), 实证 DB 中 `07-16T00:03` 与 `07-16T23:58` 同北京日分属两个 ET 交易日 → 日限盘中清零可绕过 ~2 倍。修复: executor.py/validator.py 改按 ET 交易日 D 对应北京窗口 `[D 12:00, D+1 12:00)` 过滤(对齐 `_et_trade_date` 的 -12h 约定); 写入侧与 59 条历史数据不动。
  3. **A-1~A-4 锁现值**: config_keys.py 补 `_floor` 后缀; YAML `min_effective_strength_floor: 5→50`(原 5 为死值, 50 为实际生效值)、补 `risk.position.max_position_pct: 0.15`、补 `risk.stop_loss/take_profit` 块(-0.06/2.0/0.08/0.15/0.25)、补 `execution.twap_*` 5 键(5000/4/30/true/true)。**全部 = 路径修复前实际生效值, 零行为变更**; TWAP kill-switch 自此真实可用。
  4. **D-3**: `sign = "+" if pnl_pct >= 0 else ""`(pnl_pct 已方向感知)。
- 验证: 5 文件 AST 通过; 配置加载实测 10/10 路径解析 = 期望值(min_effective_strength_floor=50 / max_position_pct=0.15 / SL·TP 5 键 / twap 3 键)。重启与启动日志验证见验证项。
- **未执行(保留)**: 批次②(C-7 fail-close / B 组 / D-2)、A 组意图值裁决、F-1 审计脚本、F-3 统一抽象 — 待用户单点裁决。

### 第 3 轮 — 批次② 执行 + 测量记录 (2026-07-21, Qoder, 用户授权"按照建议执行")

- 执行(4 项):
  1. **C-7**: `acb_fail_mode: open → closed`(一行配置)。绝对熔断器检查异常时拒绝开仓 — 失明的熔断器=没有熔断器; 秒级可回滚(改回 open)。
  2. **D-1**: weekly_optimizer fallback 公式改方向感知(休眠 bug 顺手修, 437 笔 NULL=0 从未触发, 零行为变更)。
  3. **B-1**: `app/storage/decision_recorder.py` SHORT 方向盲修复(公式加方向感知)+ decisions.db/trade_outcomes 18 行历史回填(先备份, 只翻 SHORT 行符号)。×100 量纲**未动**(等 B 组约定裁决)。
  4. **F-1**: 新增只读遮蔽审计脚本 `scripts/audit_config_shadowing.py`(AST 解析 pb_cfg.get 调用点对照 YAML+DEFAULTS, 不碰运行时, 可进 CI)。
- 测量结论(只读, 供后续裁决):
  - **A-1 (5vs50 反事实)**: 已平仓 436 笔中 signal_strength<50 有 193 笔 — **floor=50 的门对存量记录并非约束性过滤**(有效强度≠落库 strength: bonus 调整/playbook 旁路/补录假值)。5~50 段 51 笔 avg_pnl≈0.0%、胜率 51%(final_strength 口径 32 笔 avg+0.2%) — **无证据支持降回 5, 维持锁现值 50**; 5vs50 裁决的实际意义低于审计预期。
  - **B-5 (量纲标尺实测)**: trade_records=小数制(437 笔 |x|>1.5 为 0, avg|x|=0.011); signal_outcomes=百分比制(196 笔, avg|x|=0.73, 24 笔>1.5); decisions.db/trade_outcomes=百分比制(18 笔, avg|x|=1.45)。**约定分裂证实但样本小** → 治理建议: 文档化"表内约定"+读取侧适配, 不做大规模迁移。
  - **D-2 (conf-胜率)**: 分桶无单调性 — conf<0.4 胜率 47.5%(n=183), conf[0.6,0.7) 仅 **18.9%**(n=95, 最差), conf≥0.8 39.5%(n=114)。**confidence 与胜率无正相关甚至中段倒挂** → 证实语义质疑: 该字段是"信号共识度"而非"胜率预估", 在校准前不应用于仓位或推送门加权。列重命名推迟, 先在消费侧文档标注。
- 未执行(仍保留): A 组意图值裁决(A-1 测量已给出"维持 50"建议)、B 组量纲统一迁移(建议改为约定文档化)、D-2 列重命名、F-3 统一抽象、E-2~E-6。

---

## 第 4 轮 — 信号筛选链路 + 入场门拦截链路 全域复盘 (2026-07-21, Qoder 发起, 待辩论)

> 用户指令: "解决完逻辑和 bug, 再复盘信号筛选以及门拦截的问题"。前三轮已清确定性 bug; 本轮为**链路级复盘**, 聚焦筛选闸与入场门的 (a) 覆盖完整性 (b) 量纲一致性 (c) fail 语义一致性。方法学同前: `[已亲验]` = grep/Read 双侧确认; `[agent报告-未亲验]` = subagent 梳理待复核。

### G-0 关键前提 (辩论时必须先接受): 当前只有 Playbook 一条活链路 `[已亲验]`

- `app/pipeline/order_executor.py:3125-3133` 链路统一硬门: `source_type ∉ {PLAYBOOK_SIM, PLAYBOOK_LIVE, ORB_ENGINE}` 一律 `return None` (7-02 冻结期硬编码)。
- **推论**: 主链 `_submit_order` 上梳理出的 G1~G28 共 ~28 道门中, 大量 fail-open 门 (PDT #20 / 价格动量 #25.11 / 标的日亏损 #17 / US Battle Plan #25.10) **当前是死路径**, 主线信号根本进不来。**本轮复盘的实盘风险聚焦 Playbook 活链路**; 主链 fail-open 集群留待"冻结解除前"专项处理 (与 F-3 合并), 现在动它=改死代码, 违反必要性原则。
- 待辩论: 冻结解除是否有明确的"重新审视主链 28 门 fail 语义"的 checklist? 硬门 G0 解除时若忘记同步收紧主链 fail-open, 会一次性放开一大片失明门。

### 信号筛选链路复盘 (候选 → 可交易 instruction)

链路 (已亲验入口, 中间闸为 agent 梳理待复核):
`daily_active_pool + Finnhub movers → quality_gate(价≥$5/市值≥$2B) → factor_screen(5因子+regime, top_n=8) → PlaybookScreener(NO_TRADE/持仓/财报<3d skip) → VotingCoordinator(LLM, 失败降级技术面) → _build_instructions_from_votes(11闸) → guardrails(min_rr/sector/skew/日名义/日仓数) → instruction`

| 编号 | 发现 | 证据 | 量纲/风险 | 验证态 |
|---|---|---|---|---|
| **SF-1** | **confidence 三标尺并存** — `signal_quality.min_confidence=0.7`(0-1) 与 `playbook.execution_parameters.min_confidence=60`(0-100) 与 `vote_result.final_confidence`(0-1, 写 instruction 时×100) 三处"confidence"量纲不一, 靠各消费端各自转换, 无统一层 | `config/unified_config.yaml` signal_quality/playbook 段 + `instruction_generator.py:506` compute_confidence | 与 B 组同源(量纲分裂), 但这里是**入场门量纲**, 漏乘/多乘 100 会静默全放或全拦且不报错 | `[已亲验]` 配置双值存在; 转换链 `[agent报告-未亲验]` |
| **SF-2** | VotingCoordinator 任一 adapter 抛异常 → 降级 TechnicalFallback (fail-open to tech) | agent 梳理, 未给行号 | 设计意图(防 LLM 断链), 但降级后是否仍受同一 confidence 门约束需确认 | `[agent报告-未亲验]` |
| **SF-3** | `compute_confidence` 异常 → confidence=0 (下游闸 SKIP, 等效 fail-close); 但 `compute_position_size` balance 缺失 → return None 不发单 (fail-open 到"不交易", 安全) | `instruction_generator.py:36/506` | 语义正确, 仅记录 | `[agent报告-未亲验]` |
| **SF-4** | battle_plan FAST_PATH 与 playbook 链路并行, 同标的可能双通道各生成一次 instruction, 靠 `signal_stage` 的 `target.status==CANCELLED` 拦重复; cancel 状态延迟仍可能重复入场 | `signal_stage.py:406` | 冻结期 battle_plan 是否还活? 若活则去重竞态是真风险 | `[agent报告-未亲验]` 需确认 battle_plan 活性 |

### 入场门拦截链路复盘 (Playbook 活链路, 按执行顺序)

| 顺序 | 门 | 证据 file:line | 阈值/配置 | 异常 fail 模式 | 验证态 |
|---|---|---|---|---|---|
| 1 | 账户熔断(OKR, 日亏≥2%停/≥3%平) | `playbook_service.py:686-697` | 无配置开关 | **fail-open 写死** `:696-697` ⚠️ | `[已亲验]` |
| 2 | 绝对熔断(连亏5/日亏$2K/时亏$1K) | `playbook_service.py:703-719` | `risk.acb_fail_mode` | 配置驱动(批次② 已改 **closed**) | `[已亲验]` |
| 3 | PDT(余额<25k 且 5日≥3笔) | `playbook_service.py:877-897` | `risk.acb_fail_mode` 复用 | 配置驱动(默认取 open, closed 时拒) | `[已亲验]` |
| 4 | 报价健康门(bid/ask/spread/龄) | `playbook_service.py:916-942` | `pretrade_max_spread_pct=0.05`/`_age_sec=60` | **fail-open**(quote 缺失也放行) ⚠️ | `[已亲验]` 批次① 已加 |
| 5 | 数据新鲜度门(降级源/>90s) | `playbook_service.py:944-961` | `entry_max_quote_age_sec=90` | **fail-open**(`_arr_ctx=None` 放行) ⚠️ | `[已亲验]` 批次① 已加 |
| 6 | 盘中量能门(ratio≥0.8, 开盘15min豁免) | `pipeline.py:3274-3306` | `chase_vol_ratio_threshold=0.8` | **fail-open**(无 vol 数据 `_vol_ok=True`) ⚠️ | `[agent报告-未亲验]` |
| 7 | 极端gap>8% / thesis漂移>5% | `pipeline.py:3338-3359` | 内置 8%/5% | 正常拒 | `[agent报告-未亲验]` |
| 8 | RR门(min_rr=1.0 指令级 / min_tp_rr=1.5 出口级) | `pipeline.py:1295/4434` | 参数化 | 正常拒 | `[agent报告-未亲验]` |

### 本轮核心待辩论议题

- **GATE-1 (熔断 fail 语义不统一)** `[已亲验]`: 三道熔断中, 绝对熔断(2)/PDT(3) 已由 `acb_fail_mode` 配置驱动(批次② 已翻 closed), 但**账户熔断 OKR(1) `:696-697` 仍是写死的无条件 fail-open**, 未纳入统一 fail 模式。这是 C-7 的**遗漏第三份拷贝**(类比 C-6 是漏修的第二份拷贝)。争议: 账户熔断本职是"日亏 2%/3% 保命", 失明时放行是否可接受? 建议纳入同一 `acb_fail_mode` 开关。回滚成本: 一处 try 分支改造 + 复用现有配置键。
- **GATE-2 (数据门 fail-open 集群)** `[部分亲验]`: 报价健康(4)/新鲜度(5)/量能(6) 三门在数据缺失时全部静默放行。开盘薄盘 or 数据源降级(finnhub 延迟)时, 追入限价/市价单保护最薄弱。争议: sim 期保留 fail-open+告警(承前三轮共识) 还是对"数据门"这一类破例改 fail-close? 数据缺失≠系统异常, 数据门 fail-close 会在数据源抖动时减少成交样本(与 chase_vol 0.8 放宽积累样本的目标冲突)。需权衡。
- **GATE-3 (confidence 入场门量纲)** = SF-1: 见上, 与 B 组量纲分裂同源但落在**入场门**, 危害更直接。是否在筛选链入口加一层"confidence 归一化(统一 0-100)"适配器, 而非逐消费端转换?
- **GATE-4 (筛选闸 vs 门 职责重叠)**: min_confidence 在筛选侧(`_build_instructions_from_votes`)与 pre_trade 侧(`MinConfidenceResolver`)各拦一次, 量纲还不同(0-100 vs 0-1); min_rr 在 guardrail(1.0)与出口(1.5)各一次。重复拦截是"纵深防御"还是"SSOT 分裂导致的双维护点"? 需辩论边界。

### 建议批次划分 (供辩论, 未执行)

- **批次③-立即(低风险一致性)**: GATE-1 账户熔断纳入 `acb_fail_mode`(与批次② C-7 同性质, 补齐第三份拷贝, 零行为变更当 mode=open)。
- **批次③-需单点决策**: GATE-2 数据门 fail 语义(是否对数据门破例 fail-close)、GATE-3 confidence 归一化层。
- **推迟**: GATE-4 职责重叠边界(需先定 SSOT 归属)、主链 28 门 fail-open 集群(与 F-3 合并到冻结解除前)。

- **<辩论 Agent 1 第 4 轮裁决> (Qoder-复核, 2026-07-21)**: 独立双侧复核全部未亲验项 + 抽验已亲验项。结果: **证实 5 / 定性变更 2 / 降级 1 / 新发现 2 / 纠正 1**。
  - **G-0 证实**(order_executor.py:3125-3135 亲验), 且推论可加强: battle_plan FAST_PATH 走主链 fast_lane 纯策略 → `_submit_order`, `strategy_id=BATTLE_PLAN_FAST_PATH` 非 playbook 三值 → **同被 G-0 拦截**。因此 **SF-4 降级为休眠**(冻结期双通道重复入场不可能发生), 与主链 28 门集群一并挂入"冻结解除 checklist"。支持建立该 checklist, 本文档第 4 轮即是载体。
  - **GATE-1 证实**(`:696-697` 写死放行亲验), 支持纳入 `acb_fail_mode`。但**纠正批次③的"零行为变更当 mode=open"定性**: 批次② 已把 `acb_fail_mode` 翻为 **closed** → 纳入后 OKR 熔断失明立即变 fail-close, 这是**行为变更**(方向与 C-7 裁决一致: 失明的熔断器=没有熔断器, 我支持, 但须明示而非以零行为变更名义混入)。
  - **新发现(Gate4/5/6 失明不可观测)**: 报价健康门/新鲜度门异常分支是 `logger.debug`(`:942/:961`), 量能门数据缺失时无任何日志(`:3297` 静默 `_vol_ok=True`) — 生产 INFO 级别下**三门失明次数无法统计**, 不满足前三轮"fail-open 必须 WARNING+可计数"共识。建议列为批次③-立即(debug→warning + 失明一行日志, 零行为变更)。
  - **GATE-2 裁决: 反对现在改 fail-close** — 数据缺失≠系统异常, 失明频率未知时翻 fail-close 是盲改, 且与量能门 0.8 攒样本目标冲突。正确顺序: 先补上一条可观测性 → 攒失明频率数据 → 再裁决。
  - **SF-1 证实且更广**: YAML 中 `min_confidence` 共 8 处至少两种量纲(0-1: signal_quality 0.65/0.7/0.45/0.55; 0-100: playbook L1153=60、direction_filter BUY/SHORT=70), pipeline.py:2761 按 0-100 消费。但 **GATE-3 归一化层建议降优先级**: D-2 测量已证 confidence 与胜率无正相关甚至中段倒挂(18.9%) — 在无预测力字段上先做量纲统一工程, 收益存疑; 正确顺序是先裁 D-2 语义(该字段是否保留为门), 再定是否值得建适配层。短期低成本替代: 各消费点加量纲断言(值>1 视为 0-100, ≤1 视为 0-1 即报警)。
  - **SF-2 定性变更(风险不成立)**: "任一 adapter 抛异常 → 降级 TechnicalFallback"不准确。实际架构(pipeline.py:302-384 亲验): adapter 在 **scheduler 启动时按 bridge 状态一次性选定**(Bridge/TechnicalFallback/Null), 运行期不切换; TechnicalFallbackAdapter 内部异常 → return None = SKIP(fail-close); 可关停(`fallback_when_no_bridge=none`); 真·3模型 VotingCoordinator 已退休(07-11 双脑清理)。无"异常降级到技术面"路径, 改记录为架构合规。
  - SF-3 未复核(声明为 fail-close/安全语义, 仅记录项, 低优先级不阻塞)。Gate7/8 证实(extreme_gap 8% / thesis drift 5% 可配 / min_rr=1.0 guardrail L1296 / MIN_TP_RR=1.5 已 config 化 L4439)。
  - **批次③修订建议**: 立即 = GATE-1 纳入 acb_fail_mode(明示为行为变更) + Gate4/5/6 失明可观测性(零行为变更); 需单点决策 = 无(GATE-2 先测量、GATE-3 待 D-2 语义裁决); 推迟 = GATE-4、冻结解除 checklist 专项。
- **<辩论 Agent 2 第 4 轮裁决>**:(待填)

### 第 5 轮 — 批次③ 执行记录 (2026-07-21, Qoder, 用户授权"再查看后执行")

- 如实说明: Agent 2 槽位执行时仍为空(全文无落盘)。已落盘两方意见在批次③ 上一致: 发起方标 GATE-1 立即, 复核方同意(仅纠正定性); Gate4/5/6 可观测性符合双方前三轮"fail-open 必须告警可计数"共识; GATE-2/3 双方均不主张现在动。执行一致范围:
  1. **GATE-1**: `playbook_service.py` OKR 熔断异常分支纳入 `risk.acb_fail_mode`(照抄同文件 ACB/PDT 已验证模式, C-7 第三份拷贝补齐)。**明示为行为变更**: 当前 mode=closed → OKR 检查失明即拒绝开仓。回滚: acb_fail_mode 改回 open。
  2. **Gate4/5 失明可观测**: 报价健康门/新鲜度门异常分支 `logger.debug→warning`(零行为变更, 生产日志可 grep 计数)。
  3. **Gate6 失明可观测**: 量能门"启用+非豁免窗+数据缺失"分支补 warning 日志(原完全静默; 豁免窗跳过是设计行为维持 debug; 零行为变更)。
- **未执行(维持第 4 轮裁决)**: GATE-2 数据门 fail-close(先攒失明频率数据)、GATE-3 confidence 归一化层(待 D-2 语义裁决)、GATE-4 职责重叠、冻结解除 checklist 专项(主链 28 门 + SF-4 竞态)。
