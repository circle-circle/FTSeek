# PROPOSAL-20260906B · 退出/隔夜架构收敛终态与迁移路径（四模型会战定稿 · 待用户裁决）

> 日期：2026-09-06。状态：**P0/P1/P2a/P2b 已执行落地**（P0 三项打假完成；P1 三轨合一完成：
> 新模块 app/playbook/overnight_decision.py 为 EOD 隔夜豁免唯一决策点，两处 EOD 决策面
> 已收单点，旧函数降级转发/删除，tests/playbook/test_overnight_decision_contract.py
> 360 组全组合对旧链等价钉锁；**P2a 纯搬家完成**：`_check_playbook_exit_rules` 714 行
> 逐字迁入 app/playbook/exit_engine.py，pipeline 留转发，P7 缝随码迁移，测试钉锁；
> **P2b 参数单点化完成**：`cohort_b_skip_eligible` / `overnight_master_switch_on`
> 两纯助手收三处逐字公式（poll 盖章/sweeper/引擎读取处），poll 点不叠总开关的
> 刻意不对称与引擎默认公式 (B or long) 差异均保留并钉测试，
> tests/playbook/test_exit_engine_params.py 66 例全绿）；**P2c/P3/P4 待裁决排期**。
> 方法：qwen-architect 终态设计 × glm-verifier 爆炸半径实测 × kimi-reviewer ×2 对抗终审 × 主会话复核。
> 触发：用户裁定"多管线策略逻辑混乱、历史退出线误导人，要求从源头彻底修复乃至重构，明确复杂度，避免拖延"；
> 附硬约束：**禁止重复造轮子**（历史教训：playbook 链本身即在主线 ExitMonitor 上重造的分叉产物）。
> 纪律：C1 冻结未解冻；本文一切行为变更均需影子+裁决；唯一例外条款不适用于本提案。

---

## 〇、数据基础勘误（先修尺子，再谈方案）

kimi 双实例独立复算发现我先前口径**混入非 SIM 行**（`decision_id` 前缀 ≠ 执行链）。逐字节复核后的干净口径（`source_type='PLAYBOOK_SIM'`）：

| 结论 | 原口径（混入 SHADOW/MAIN/SCALPING） | 干净 SIM 口径 | 判定 |
|---|---|---|---|
| DA 隔夜总账 | 40 笔 +1038.73 | 31 笔 **−1009.13** | ❌ 原结论推翻（+1393 来自 PLAYBOOK_SHADOW 影子行，+704 来自 MAIN_PIPELINE） |
| 浅水 MAE<1.5% 隔夜 | 20 笔 +2840.76 | **13 笔 +784.47**（62% 胜率） | ⚠️ 方向成立，量级缩 3.6× |
| 深水 MAE≥3% 隔夜 | 9 笔 −1807.18 | 9 笔 **−1807.18**（11% 胜率） | ✅ 三模型各自复现，最硬结论 |
| 8月隔夜桶 | 17 笔 −1427.89 | 19 笔 **−1436.32** | ✅（口径微调） |
| "无 2 夜样本" | — | 属实（全量口径下有 2 夜 8 笔 −6912.34，但全属 MAIN_PIPELINE 4-6 月时代） | ⚠️ 需注明口径 |

**新增的两大事实**：
1. **MAIN_PIPELINE 才是隔夜盈利的历史大头**：55 笔 +17502.52（4-6 月 voting_llm 时代，走主线 ExitMonitor；6月 DAY_TRADE_TP 10 笔 +11678）。该时代已结束，但它证明"隔夜可赚钱"的同时证明**那是另一条链的功劳**。
2. **轨③（reentry 首夜豁免）是活代码，不是死轨**：HOOD id=1459（9/4，在仓）metadata 带 `overnight_arm.source=reentry_first_night` 实锤；TSM 8/10（−7.36）早于 W_WEEK 上线（8/15，pipeline.py:254），按排除法也走轨③。intraday_reentry.py:214-216 的"8/27 已被 W_WEEK 覆盖"注释**与事实不符**。~~且轨③有 naive datetime 未做 ET 归一（intraday_reentry.py:1241-1244）~~ **执行阶段复核：非 bug**——naive 按 ET 墙钟解释是既有约定，由 test_overnight_naive_timestamp_treated_as_et 钉死（trade_records 历史写入可能无时区），写入侧 R-12 保证 ET-aware；本次仅补注释钉语义。全表 overnight_arm 标记仅 14 行 → cron `overnight_arm_report`（n≥20 门槛）分母恒不足——**执行阶段复核：仪器本身正常**（`_mark_overnight_arm` 8/27 起工作，sweeper 两处豁免点均已打点 pipeline.py:9259/9277），饿死的是样本龄（14<20）而非仪器；P0 的"修好观测仪器"一项随之撤销（§0.5）。

**教训登记**：本次错误与 §13.12 同族——聚合查询口径未按 source_type 切净。本提案全部下游结论以干净 SIM 口径为准。

---

## 0.5 深水闸"直落"被否决——已裁决历史的执行层拦截（2026-09-06 执行阶段补）

**事件**：执行阶段曾考虑把深水闸直落（`loser_overnight_max_depth_pct: 99→2.5`，理由之一："8/20 曾生效过=先例"）。git 考古发现该理由**不成立**：

| 日期 | 提交 | 动作 |
|---|---|---|
| 2026-08-20 | bea25e31 | DEB §47：裁决启用水深闸 2.0（当日 3 笔深水全灭证据） |
| 2026-08-21 | 06432156 | **次日即误杀**：AMZN 盘中水深 −2.04% 触闸 → 撤豁免 → EOD −1.08% 割，30 分钟后回到 +1.1%（+2.2% 修复被掐死，SL −5% 全程未参与）。当日裁决回滚 99（=禁用） |

**失败机理**：8/20 版把深度检查放在 `_loser_overnight_exempt` 单点内（pipeline.py:289-291），该函数同时服务盘中 DEAD_SIGNAL/TIME_EXIT 豁免链——**盘中 −2% 是噪声，EOD −2% 才是修复失败**，闸放错了判定时刻。

**纪律结论**：直落 = 单方面推翻 8/21 已裁决的回滚，越权，**不执行**。`loser_overnight_max_depth_pct: 99` 配置本次不触碰。

**修正设计（并入 P3 影子，不直落）**：
- **EOD 判定时刻深水闸**：仅在盘末决策面（规则[3] 7598 / 兜底 9257 / orphan 9145）查决策时刻水深，超阈值才撤豁免；盘中豁免链不查深度；
- 新建独立配置键（如 `eod_depth_gate_pct`），**不复用 99 键**——保留其"永不启用"语义，防止第三次误翻；
- 证据强度自降：深水 9 笔 −1807.18 是真实数据，但"砍深水省 −2073"是反事实上限（13.3% 覆盖、尾部主导）→ 必须 P3 前向影子 + 47 笔全量回放双证，且若影子显示杀掉 ≥1 个真修复则不上。

---

## 一、根因（三模型一致）

退出参数在武装点之后仍被"按源重新判定"：`_skip_allowed` 三处独立计算（pipeline.py:7283/8547/9203）；豁免判定散落 7 文件；`exit_policy.yaml` 的 SSOT 地位是**声明性的、无机制保证**（playbook 引擎根本不读它；unified-exit-strategy.md:113 还谎称"所有持仓走 ExitMonitor"）。分叉不是线多，而是**没有单一决策点 + 没有机制保证的 SSOT**。

## 二、终态架构（经反分叉对抗审查）

**"一台引擎、仓位带档案、政策即配置、缝有契约"**

1. **收敛方向：以 playbook 链为唯一引擎**（kimi 9a 独立判定：此方向正确，非奖励分叉者——反向把 714 行+48 测试文件契约搬进 3 规则的 Registry，且需推翻 2026-07 双重平仓事故后的分离裁决 monitor.py:938-941）。**但引擎必须从 9453 行的 pipeline.py 单体拆出为独立模块**。
2. **exit_profile 参数包**：武装点一次性解析（initial_sl / tp_gate / overnight:{allow,max_nights,mae_bands}）写入指令 meta+落库；引擎零源概念；reentry ATR SL、dead_signal 加宽等源分叉全部前移到武装点。
3. **隔夜判定单点**：以 `_loser_overnight_exempt` 为体改造（它已是多处的公共入口），新签名 `overnight_decision(profile, pos_state) → {allow, track, depth_pct, reason}`；轨③（活代码）并入为 profile 等价语义，旧两函数降级为纯转发后删除。**EOD 决策面实为 3 处**（规则[3] 7598 / 兜底 9257 / orphan 9145）——收口清单必须含全部三处。
4. **exit_policy.yaml 成真 SSOT**：新增 `playbook_exit:` 段，unified_config 退出键迁入，接入既有 config_key_reverse_census 双向闭合。
5. **缝#2 闭合**：Registry 侧 pre_close.py:97/424/555 读 playbook 链写入的 meta 键（cohort/source_type）——两链"隔离"只隔离了调用没隔离数据。终态要求 Registry 规则读取的 meta 键 ∩ playbook 写入键 = **显式契约 allowlist**（机器断言）。
6. **MAIN_PIPELINE/legacy 仓不迁移**（量小语义异，成本>收益），但真值表必须显式声明其链路，消除"Registry 已降级"的误述。

## 三、迁移路径（每阶段独立可停；复杂度经 kimi 修正）

| 阶段 | 内容 | 复杂度 | 前置/裁决点 |
|---|---|---|---|
| **P0** | 打假：unified-exit-strategy.md:113、exit_policy.yaml 头、轨③"已覆盖"误注修正（~~修好观测仪器~~ 已撤销：仪器 8/27 起正常，饿死的是样本龄，见 §0.5） | **S**（零行为变更） | 无需裁决，可直接执行 |
| **P1** | 三轨合一 `overnight_decision` 纯函数（轨③是活代码→按"行为等价合并"做，非"删死轨"；~~naive-ET bug 单独立项~~ 已撤销：复核为既有约定非 bug，§0.5）；契约钉锁合并前后判定等价 | **M**（原 S，升档：活代码合并） | diff 报告裁决 |
| **P2** | **用户 9/6 裁决拆为三步**（一次一变量，每步独立可停）：**P2a 纯搬家**——`_check_playbook_exit_rules` 714 行逐字迁入 exit_engine.py，旧名留转发，测试钉锁（无需对照器，同 P1 套路）；**P2b 参数单点化**——`_skip_allowed` 三处计算收为单函数，仍判定时刻现查配置（不发身份证，热开关语义保留）；**P2c 身份证**——exit_profile 武装点写入 + 引擎优先读 profile，**唯此步需新建双跑 comparator**（挂现有轮询点 8727 与写回闸 8773 模式；**不得塞进 EXIT_VARIANTS**——铁律 exit_shadow.py:15"只评更紧变体"承载不了新旧引擎对照；diff 判定待旧仓轮换 ≥5 交易日后再计，止损方向分歧自动熔断）。热开关白名单：极少数全局总闸（如 dead_signal_exit_enabled）保留判定时刻现读，其余参数才进 profile | **P2a/P2b = S-M（直接执行）；P2c = M（影子+裁决）**（原 L，拆后爆炸半径逐步收敛） | P2a/P2b 已执行落地（9/6 批准后直接执行）；P2c 影子数据+diff=0 报告后再裁 |
| **P3** | MAE 分带隔夜政策（profile.overnight.mae_bands）+ **EOD 判定时刻深水闸**（§0.5 修正设计，新建独立键，与 MAE 分带同影子对照）。**阈值不预设 1.5%/3%**（立在 n=9-13+单 regime 上不足）；EOD 时刻 MAE 现成可用（meta.mae_pct/peak_price 每 20s 轮询持久化 pipeline.py:8642-8784，隐藏前置不成立）。`allow_hold` 是**更松变体**，走新建 comparator 前向影子，禁走 EXIT_VARIANTS | **M**（在 P2 基建上） | 影子 2-4 周 + C1 限段解冻裁决 |
| **P4** | 配置收敛（yaml 双向闭合普查）+ 反分叉机器断言入库 | **S** | — |

**总核心面**：~1127 行四个函数 + 豁免散落 7 文件 + 48 测试文件。**P2 是唯一 L，也是真正的重构心脏**；P0/P1 先行即消除 80% 误导源。

## 四、复用清单（反分叉硬约束落地）

复用：exit_shadow 轮询挂点（8727）与写回闸（8773）、`_loser_overnight_exempt` 为改造基体、config_key_reverse_census 普查模式、现有契约测试基座。
**勘正两个"伪复用"**：① `exit_replay_backtest.py` 只 import 两个数学 helper（:40-43），决策链是**重写副本**（:278）——它本身就是第三份漂移副本，不得作为"已复用生产逻辑"的证据；② `overnight_hold_shadow.py` 是**半死代码**（config 已关），方案须明示删除或并入 exit_shadow，禁止第三条影子路径并存。

## 五、反分叉验收标准（机器断言，9c，P4 入库）

1. AST 级：退出判定函数 def=1，生产调用点白名单化；
2. `_skip_allowed`/`_loser_overnight_exempt`/`overnight_loser_exempt` 三键全仓 grep 命中 = 白名单；
3. shadow emitter × 消费者（shadow_registry+cron）双向闭合，config-disabled 的影子不得挂在树里；
4. Registry 读取的 meta 键 ∩ playbook 写入键 = 显式契约 allowlist；
5. exit_policy.yaml `playbook_exit` 段接入既有 reverse census。

## 六、明确不做

- 大爆炸重写；MAIN_PIPELINE/legacy 仓迁入 playbook 引擎；删除 ExitMonitor/Registry（看板与遗留仓依赖）；
- 直接 `overnight_hold_enabled=true`（8月 −1436 证据在先，必须影子）；
- **深水闸直落**（`loser_overnight_max_depth_pct` 任何直改：8/21 已裁决回滚，正确形态=EOD 判定时刻闸进 P3 影子，§0.5）；
- 在三线现状上继续加 per-source if（dead_signal 加宽方向本身可疑，P3 时并入 profile 影子对照）；
- **富化 I/O 挪出"成交→落账"关键路径**（R-12b 取证附带发现：regime assess 的 VIX 重试链 + get_full_market_context 夹在 broker 成交回报与两表落账之间，慢路径实测 2.94s，期间 SL 监控未启动；正确形态=落账先行、富化后置或异步。独立立项，不夹进本提案）；
- **R-12b 已裁决执行**（2026-09-06）：open_positions/trade_records 双时间戳结构漂移治本=`upsert_position_simple` 加可选 `entry_time` 参数、playbook_service 传 `_now_iso` 两表同源；存量 US.HOOD 行回填成交时刻 `:09.928`（证据：dual_broker 成交日志 :09.927）；R-12 棘轮测试不动，2s 容差继续当真异常哨兵。

## 七、裁决请求

| # | 事项 | 建议 |
|---|---|---|
| V1 | P0（打假三项；观测仪器复核正常已撤项） | 零风险，请求批准直接执行 |
| V2 | P1 三轨合一（行为等价） | 请求批准立项 |
| V3 | P2 exit_profile + 引擎拆出 + comparator（L） | 请求裁决是否立项与排期 |
| V4 | P3 的 C1 限段解冻（仅 exit_policy.yaml 新增 playbook_exit 段） | 影子数据到位后再裁，本次仅登记 |
| V5 | 隔夜政策方向（浅水 hold / 深水 close_or_tighten） | 不裁阈值，待 P3 影子；登记意向 |

---
*四方会战：qwen-architect（设计）× glm-verifier（实测）× kimi-reviewer×2（对抗终审）× 主会话（复核收口）· 2026-09-06*
