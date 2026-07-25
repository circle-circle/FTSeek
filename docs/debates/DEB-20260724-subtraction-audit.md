# 议题：全链路减法审计 — 决策统一到 playbook 管线后，哪些该停、该删、该合

## 状态

- [x] 进行中（第 1 轮：减法清单 + 逐项证据 + 风险分级，待用户裁决/辩论方攻击）
- [ ] 待决策
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> **本文用途**：回应用户 2026-07-24 "所有决策和链路集中统一到 playbook 管线，还有哪些值得做减法"。基于当日 DA 链路审计（da-chain-audit DEB）+ 新增定向核查，给出带证据与风险分级的减法清单。
> **作者**：Qoder　**日期**：2026-07-24
> **减法纪律（每项必须走的三步）**：① 遥测/日志证零消费 → ② 配置级停用（可一键回滚）→ ③ 稳定一个周期后代码退役。禁止跳步直删（scan_loop 寄宿任务教训：整线程停会误伤巡检/复盘）。

---

## 一、A 类：已证死/僵尸（低风险，quick wins）

| # | 对象 | 证据 | 建议动作 | 风险 |
|---|---|---|---|---|
| A1 | **ORB 残留死配置**（第 2 轮修正：非僵尸运行，已死透） | 配置双轨：引擎读顶层 `orb.enabled=False`（**引擎早已不初始化**，这才是 6/22 后零交易的真因）；`strategies.orb.enabled=true` 只管订阅路径且 universe=0 → 实际零订阅。**无任何运行时行为可停** | 删除 `strategies.orb` 死配置牌子（防误导后续审计）+ 顶层 orb 节标注 deprecated；纯墓碑清理，无行为差异 | 极低 |
| A2 | **三模型投票路由 voting_\*** | `llm.features.voting_deepseek/glm/qwen` 配置存在，**代码零引用**（全仓 grep）；投票方向准确率 47%≈随机已被 bridge 替代（SKILL 归档） | 删除 3 个 config 路由项 + `agents.*.model` 4 个死配置 | 低 |
| A3 | **iterative_debate 模块** | 仓内除自身外**零消费方** | 退役（先加 deprecation 注释，下周删） | 低 |
| A4 | **exit_param_override.json 生成端** | 消费端 2026-07-11 已删（pipeline.py 改动9 注释），Hermes feedback_loop.py:348/439 仍在**写无人读的文件** | 随 DA skill 下次迭代注掉两行调用（工作区外） | 低 |
| A5 | **app 根遗留文件** us_stock_monitor / market_profile / market_data_backup / simple_dashboard | core/main.py **零引用**（入口脚本待最终确认 start_dashboard.sh 指向） | 确认后移 `deprecated/` 归档 | 低 |
| A6 | **数据文件卫生** | 仓库根 ftseek.db/signals.db/signal_snapshots.db 与 data/ 正主并存；data/ 下 15+ 个 `.bak*` DB（部分 >1 月） | 建 `data/archive/`，按保留策略归档（≥30 天的 bak） | 低（只移不删） |

## 二、B 类：主线残躯整链退役（中风险，需分阶段）

**现状**：主线（非 playbook）执行链 4+ 日零交易、100% 成交来自 PLAYBOOK_SIM，但以下组件仍在加载/空转：

| 组件 | 当前状态 | 07-24 已做 | 剩余 |
|---|---|---|---|
| scan_loop 扫描本体 | 已静默（`strategy.main_scan_enabled=false`） | ✅ | 观察一周无异常后评估代码级退役 |
| order_executor | 休眠（无主线信号→无调用） | — | 随扫描静默自然死；一周后确认零调用 |
| position_exit_monitor 主线退出循环 | 空转（playbook 仓已排除、主线仓恒 0） | — | 加同款配置门静默（保留 reconciler 对账职能） |
| PositionStore + fast_lane ExitPolicy | 恒空但每轮对账/注册 | — | 保留（对账安全网，成本低）；fast_lane policy 注册可评估摘除 |
| daily_active_pool / battle_plan 注入路径 | UE 已迁走（ADR），残留加载 45 条记录 | — | 核实剩余消费者后收敛 |

**分阶段原则**：本类每一项都可能与 reconciler/风控安全网交织，必须逐个走三步减法，**不打包一次删**。

## 三、C 类：双轨合一（已立项，本文只挂账）

- P-1 watchlist/topup 名单合一（角色标签单真源）——已立项，周末
- P-2 订阅动态化（扩展 `_get_subscription_codes` 回调）——已立项，周末
- 挑战者模型配置收敛（DA skill 硬编码 → 统一路由表）+ 4 处 deepseek-v4-pro 残留引用——challenger DEB 待办
- 文档减法：fallback_pool ADR（从未实现，已被 topup 替代）、SKILL 吞吐旧数字——过时知识修正

## 四、D 类：明确不减（备胎有真实价值）

| 对象 | 保留理由 |
|---|---|
| TechnicalFallbackAdapter + fallback_when_no_bridge | bridge 全缺席日的确定性备胎（topup 也依赖它出方向） |
| scan_loop 寄宿任务（盘前/盘后巡检、日周月复盘、持仓快照、watchdog） | 活跃关键任务，已验证不可随扫描一起停 |
| NullVotingAdapter | 保守回退语义（fallback=none 时的全 SKIP） |
| reconciler / position_sync 对账 | 安全网，成本低、防 ghost 仓 |
| 全部 shadow 体系 | 验证期核心资产 |
| app/agents 部分 | vix_monitor/review_orchestrator 仍在消费（VotingConfig/debate_planner），只减死支不动活支 |

## 五、诚实弱点

1. A5 的"零引用"只查了 core/main 入口，crontab/shell 脚本引用未穷尽——归档前须全量 grep（含 scripts/、*.sh）。
2. B 类"主线恒空"基于近 4 日窗口；若存在月度级低频主线路径（如月度再平衡）会误判——退役前按三步纪律观察一周。
3. ~~ORB enabled=true 但零交易的原因未深挖~~ → **第 2 轮已结案**：配置双轨，引擎读顶层 `orb.enabled=False` 早已停用（"已死"非"病了"）；`strategies.orb.enabled=true` 是无效死牌子（universe=0）。A1 降级为纯墓碑清理。**连带方法教训：同名配置双轨（顶层 orb vs strategies.orb）是审计误导源，减法时应一并收敛。**
4. debate_planner.enabled=true 且 review_orchestrator 在用——复盘链路的 LLM 辩论是否值得保留属于成本问题（$/次），不在本轮减法范围。

## 六、建议执行顺序

| 批次 | 内容 | 时点 |
|---|---|---|
| 第 1 批（quick win，配置级） | A1 ORB 停用 + A2 死路由删除 + A6 数据归档 | 周末（与 P-1/P-2 同批） |
| 第 2 批（观察后） | B 类主线残躯：scan_loop 静默观察 1 周 → position_exit_monitor 同款静默 → 代码退役评估 | 下周 |
| 第 3 批（跨域协调） | A4 Hermes 生成端注写 + 挑战者配置收敛 | 随 DA skill 下次迭代 |
| 持续 | A3/A5 deprecation 标注 → 删除 | 下周 |

### 第 1 轮讨论记录

- 作者（Qoder，2026-07-24）：基于当日全链路审计产出减法清单：A 类 6 项已证死/僵尸（含意外发现 ORB 僵尸拖订阅）、B 类主线残躯分阶段退役、C 类双轨合一挂账、D 类 6 项明确保留。核心纪律：三步减法（证零消费→配置停→代码退役），禁止跳步。交辩论方攻击点：ORB "该死还是病了"、B 类是否存在低频主线路径、A5 引用扫描完备性。
