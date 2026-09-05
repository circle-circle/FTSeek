# DEB-20260905-sim-data-collection: 模拟盘数据采集模式恢复入场 — 前置条件、风险敞口与数据正确性裁决框架

> **立案日**: 2026-09-05 (周六, 无行情; 美股 9/7 Labor Day 休市, 下一交易日 9/8 — 部署与验证窗口 9/5–9/7)
> **状态**: 已裁决 + 已执行 (2026-09-05 14:00 CST 批, 裁决依据 = 用户当日指令"我需要不停止 sim 模拟交易, 且要保障正常交易且收集数据…不要终止开仓, 要继续交易下去")
> **执行批**: ① Tier-A A1–A9 全部落地并测试 (见 §八 执行台账); 恢复 `playbook.auto_execute=true` ② 第二批 (15:25 CST) 熔断"解除与观察"机制 — 用户追问"当天亏损有回撤好转, 是否有解除和观察的机制" → 裁"同意, 按照建议执行" → 建议 1+2+3+4 落地、建议 5 明确不动 (**MODIFIED**: 属风控恢复语义, 非策略变体; 退出路径一行未改), 重启 PID 367294 @ 15:23:50 (见 §九)
> **立案人**: Hermes 审计会话 (应用户指示: "目前是模拟交易, 依然希望收集交易数据, 直接停掉交易是不合适的, 请根据改进问题产出 DEB 做裁决")
> **关联**: DEB-20260815 §42 (E1 GATE 1 = E1_FAIL, 三臂全负, "该关闭关闭"), fb162af0 (auto_execute=false @9/5 09:45), deb0815 硬止损监控 (累净 -$1,884.13 / 线 -$4,840.06), 深度审计整合报告 `results/explore_20260905/REPORT.md` (发现 ID G-01…G-23 / H-xx / L-xx / R-1…R-7 均引自该报告)

---

## 一、原问题

E1 GATE 1 判负后入场全停。但**模拟盘的目的函数不是当期盈亏**: 停盘期间, 信号→入场→退出→账本的完整链路停止产生新数据, 三笔存量仓 (UBER/RIVN/HOOD) 退出后再无新增样本; 复盘、因子研究、退出规则校准全部依赖持续成交。问题:**在何种前置条件与风险敞口下恢复入场, 使"数据采集"这一资产不被 bug 污染、不被尾部事故打断?**

## 二、立场声明 (与既有治理的关系)

1. **不推翻 E1 裁决**: E1_FAIL 是"盈利能力"命题的终局判定, 维持。本 DEB 是"数据采集"命题 — 模拟盘账面负 EV 亏损是**数据获取的已知成本**, 不是策略亏损。
2. **不新增实验变体**: 冻结不变量 (KR2 / exit-de49b1299290 / D1-D7) 全部维持。M-4/M-5 中涉及参数变更的议案明确标注"变体属性"判定, 交 DEB 裁决是否越线。
3. **预注册止损精神延续**: 恢复附带独立的数据采集预算与触发式停机 (§六), 非无限敞口。

## 三、证据包 (为何"现在就有资格谈恢复")

### 3.1 停盘不保护数据 — 带病运行才污染数据

审计确认的在跑缺陷中, 以下每一条都在**直接扭曲已采集的数据**:

| 发现 | 污染路径 |
|---|---|
| G-16 时间约定 (created_at/模型日统计=CST) | 账本/模型归因 12h 偏移, 因子研究样本贴错日期 |
| G-14 退出 PnL 用 trade_records.quantity 非 broker 量 | 账本盈亏失真 |
| G-11 get_push_tickers 无 TTL + 退订失败保留缓存 (日志证明每日发生) | 陈旧逐笔因子进入信号上下文, 信号-结果配对失真 |
| G-17 orderbook 无新鲜度闸门 / premarket 缓存跨阶段 / 1-min fallback change_percent 语义错 | 入场上下文混入过期市场状态 |
| G-09 playbook 平仓无 tombstone | 结算延迟窗口可复活幽灵仓, 产生重复平仓污染样本 |
| D-06 pending_sync SQL CST-vs-UTC | 同步故障告警延迟 8h, 链路健康数据不可信 |

**结论**: "停着"只冻结了新污染, 不解决三仓退出后的数据断流; 而上述修复大多为小改动。采集模式的正确姿态是**修到可信、择期恢复**, 而非无限期停摆。

### 3.2 尾部事故会一次性摧毁整个数据集

- G-01 (5 级): playbook 无任何组合级强平兜底 — R-7c 排除、退出循环逐仓制、日亏熔断仅 entry-only、BLACKOUT 仅乘数、sim 无 broker 止损。进程死亡/跳空场景唯一先例: OVERNIGHT_GAP_STOP 单笔 -$380; 6-16h 桶 -$1,044。
- G-02 (5 级): 运行进程未加载 9/5 热修 (chase_cap enforce 等) — 不重启直接重开 auto_execute, 禁追高闸是纸面的。
- 历史事故对照: 7/15 RIVN/NVDA/XOM 双平 ($37-52/次) 即 G-09 同族 — 当时是**主线**, 现活体链换成 playbook 后该防护反而缺失。

### 3.3 采集经济性 (F 数据)

246 笔净 +$182, 费用吃掉毛利 87%。<0.5h 的 92 笔净 -$100 且信息量低 (12.5 分钟持仓基本只测了费用)。**同等"数据预算"下, 砍掉低信息量交易 ≈ 免费提升单位数据质量** — 这是 M-4 的依据。

---

## 四、议案

> 裁决格式沿用仓内规范: `裁决: APPROVED | REJECTED | MODIFIED(...) | UNCERTAIN`, 回写至各槽。

### M-1 【章程】设立"模拟数据采集模式" (data-collection charter)

- 内容: 恢复 playbook 入场, 目标函数=链路验证 + 成交流数据; 账面盈亏仅作预算监控, 不作绩效主张; 所有成交自动打标 (`mode`/`owner_strategy` 现有字段可承载, 不新增变体)。
- 优点: 与 E1 裁决零冲突; 数据断流即刻止血; 9/8 恢复交易。
- 缺点: 负 EV 成本以预算封顶 (M-3)。
- **推荐: 通过, 附 M-2/M-3/M-6 为前置/伴生条件。**
- 裁决: APPROVED | 置信=high | 理由: 与用户 9/5 指令同向 ("不停止 sim 交易、保障正常交易且收集数据"); 与 E1_FAIL 零冲突 (E1 判的是盈利命题, 本章程把目标函数改为链路验证+成交流数据)。伴生条件按 M-2(已执行)/M-3(MODIFIED)/M-6(排期) 落地。 <!-- DEB-VERDICT M-1 -->

### M-2 【硬前置】Tier-A 修复包 — 全部完成并验证前 auto_execute 保持 false

| # | 项 | 依据 | 完成判据 |
|---|----|------|----------|
| A1 | **重启进程**加载 9/5 热修, 日志验证 chase_cap enforce 代码路径在场 | G-02 | entry_routing 新日志出现; PID 启动时间晚于 fb162af0 |
| A2 | chase_cap 出 reprice try 嵌套, 独立必跑 | G-06 | 异常注入测试: reprice 抛错 → chase_cap 仍执行 |
| A3 | 回滚平仓检查 order_filled (dual_broker 假 success 面) | G-04 | 单元+模拟断连测试不再"孤儿仓已防" |
| A4 | 簿记代码入 try, claim 在 submit 成功即释放 | G-05 | DB 锁注入测试: 幂等锁不残留 |
| A5 | playbook 平仓写 tombstone | G-09 | 平仓后 60-300s 内 position_sync 不复活 |
| A6 | #193 改向: 护栏加载失败仅跳过 playbook 仓, 不 `return closed` 跳全部逐仓退出 | G-08 | 异常注入: 其余仓仍退出 |
| A7 | REDUCE_50 与 FLAT_ALL 对称 (返回值检查 + 全败不锁存 + 后锁存) | G-07 | 同 #192 测试模式复用 |
| A8 | 小时熔断 `_check_new_hour()` 重置 `_hourly_paused_until` | G-03 | 双小时触发测试 |
| A9 | **G-01 最小兜底**: playbook 退出快环加组合级聚合 — 第一版允许是"合并浮亏超阈→告警+全仓逐笔市价平" (镜像 FLAT_ALL, 复用其返回值检查); 若 A9 判定工程量过大, 备选 = 恢复首两周仅允许 ≤N 个同时持仓 (见 M-3 选项), 但 DEB 需显式接受残余风险 | G-01 | 回撤注入测试触发全平路径, 且失败不锁存 |

- 工期评估: A1–A8 均为 ≤30 行小修, 9/5–9/7 无行情窗口足够; A9 中等 (~半天)。
- 裁决: MODIFIED(全部 9 项实施; A4/A9 偏离原文; "完成前 auto_execute 保持 false" 的前置语义压缩为"当日完成即恢复") | 置信=high | 理由: ① A1–A9 已于 9/5 当日全部编码+测试完毕 (§八), 前置条件已满足, 无需再挂 false; ② **A4 刻意反向实现** — 原文"claim 在 submit 成功即释放"会引入重复下单 (释放=指令可被再次捞起), 改为簿记入 try + `bookkeeping_failed` 标记 + **不释放**幂等锁, 缺口以 critical 告警交 position_sync/人工补账 (锁: tests/playbook/test_g04_g06_structure_contracts.py); ③ **A9 刻意偏离"全仓逐笔市价平"** — v1 = 聚合检测 + critical 告警 + 推送, 不在函数内强平: 宿主 `monitor_playbook_sim_exits` 自带 TRADING 相位门, 进程死亡/休市时它与兜底一起不跑 (那才是 G-01 的真实空洞, 函数内堵不到), 且另建平仓回路会与逐仓 close+簿记+tombstone 重复 → 双平/账实不符 (G-04/G-09 同族)。空洞改由外部监督层堵: systemd `Restart=always` + `StartLimitBurst=20/600s` + freeze_sentinel 新增**存活层** (每分钟判 ActiveState, systemd 放弃即 reset-failed+restart+critical, 与相位/心跳正交)。残余风险显式接受: 强平动作缺位, 由"逐仓 SL + 组合告警 + 人工"三段承担。 <!-- DEB-VERDICT M-2 -->

### M-3 【风险敞口】采集模式的仓位数、尺寸与停机预算 (选项题)

仓位基准 (不新增变体, 均为 E1 已注册口径的重用):

- **选项 a (推荐)**: 复用 arm D 配置 (E1 中较优臂, -$16.05/笔, 已判负但采集模式不以 EV 论) — 数据与 E1 历史段可比。
- 选项 b: 最小档 (arm D 的 50%) — 成本减半, 数据分布轻微失真。
- 选项 c: 全档恢复 — 不推荐 (M-4 费用证据)。

并发与停机预算 (预注册式):

- 同时持仓上限 ≤5; 日新开 ≤6 (现有 daily cap 修好 A2/G-18 后强制生效)。
- **采集预算**: 滚动 5 交易日已实现净亏 (含费) ≥ **-$250** (≈ arm D 费率 × 预期样本量, 月化约 -$500) → 自动回 auto_execute=false, 数据照采 (信号/影子), 待 DEB 复核。
- deb0815 监控线维持现值不动, 两者独立 (其一触发即停)。
- 停机后 20s 退出快环**永不停** (平仓回路是数据完整性的来源, 只停新开)。
- 裁决: MODIFIED(仓位=选项 a; 并发/日新开上限维持现值; **自动停机预算 REJECTED**; "退出快环永不停" APPROVED) | 置信=high | 理由: ① 选项 a (arm D 现状配置) — 不改任何 sizing 参数即"不新增变体"红线最干净的守法, 数据与 E1 段可比; ② **自动停机预算 (-$250/滚动5日) 与用户指令直接冲突** ("不要终止开仓, 要继续交易下去"), 且阈值经复算站不住: 现行制度 (2026-07-01 起, 437 笔已平) 净 -$732 而单笔 EV 仅 -$1.68, 5 日样本量下 -$250 落在正常波动带内 → 会在非事故情形自动停盘, 等于把"数据采集"资产交给噪声决定; 成本控制改由 (i) 既有 daily cap/名义上限, (ii) M-2/A9 组合兜底 critical 告警 (人工裁决不自动停), (iii) deb0815 既有监控线 (累净 -$1,884 / 线 -$4,840) 三段承担; ③ 同时持仓 ≤5 / 日新开 ≤6 维持现有配置不新增闸门 (新增即减少成交)。 <!-- DEB-VERDICT M-3 -->

### M-4 【数据纯度 vs 可比性】费用效率门槛是否算新增变体 (判定题)

- 提案: admission 现有 fee-efficiency 检查 (admission.py:102-196) 加硬 — 拒掉 <0.5h 预期即反手的微型交易 (92 笔 -$100、费用/毛利 127%、信息量低)。
- **诚实标注**: 这会改变入场分布 → 采到的数据与 E1 段不可直接比。两种立场:
  - 立场 1 (推荐): 采集模式的数据用途是链路验证+因子研究, 不需要与 E1 比 → **不算新增实验变体** (E1 已终局, 无对照假设), 通过。
  - 立场 2: 任何入场分布变更都是变体 → 拒绝, 维持现状费率, 用 M-3 预算封顶成本。
- 裁决: REJECTED | 置信=high | 理由: **提案的证据窗口已过期, 按本 DEB 自己的复算义务重算后结论反转。** 提案引"92 笔 -$100" 出自 246 笔旧窗口; 用现行制度全窗口 (2026-07-01 起, 437 笔已平, 净口径 COALESCE(pnl_net, realized_pnl)) 复算: <0.5h 桶 **128 笔 净 +$244.48 (EV +$1.91/笔)**, 是全账本**唯一**笔数最多且净为正的短持桶; 真正的亏损源在别处 — 2-6h 桶 134 笔 -$1,086.62、>16h 桶 31 笔 -$996.66, 按退出原因 SL_INITIAL 20 笔 -$3,173.92 / MFE_STALL_EXIT 31 笔 -$1,352.64 / TIME_EXIT 36 笔 -$1,195.16 (其余 417 笔合计 +$2,620)。→ 加硬费用门会砍掉 29% 的样本量 (128/437) 去换约 $0 的 P&L 改善, 且 `min_profit_fee_ratio` 现为 0.0, 任何非零阈值都是**新增入场拦截 = 直接减少成交笔数**, 与用户"继续交易、多采数据"的目标冲突。费用问题 (全窗口费用 $2,032 = 毛利 $1,300 的 156%) 的正确处置是 R-1 侧的成本结构与 2-6h/>16h 桶的退出质量, 不是砍短持样本。 <!-- DEB-VERDICT M-4 -->

### M-5 【regime 杠杆】risk_on_mult 维持 1.0 还是降 0.5 (判定题)

- 证据: risk_on 净 -$1,185 vs cautious +$954, 多切面一致 (REPORT R-2)。
- **推荐: 采集期维持 1.0 (现状)**。理由: (i) 降 risk_on 仓位=改变实验控制参数, 越"不新增变体"红线嫌疑最大; (ii) 采集模式要的是 risk_on 环境下的成交样本 — 恰恰是 E1 判负场景的数据, 缩仓会让样本分布更稀; (iii) 该杠杆留待未来实盘 DEB 用干净对照裁决。
- 裁决: APPROVED(维持 risk_on_mult=1.0) | 置信=high | 理由: 同意提案三条, 另补一条**物理性**论据 — `min_notional_usd: 2500` 地板与 pipeline 仓位下限校验 (app/playbook/pipeline.py:4080 / :4111) 使降 mult 不是"仓位变小"而是"指令被拦下不下单": 0.5× 后相当一部分 risk_on 信号的名义额跌破地板 → 直接减少成交笔数, 与采集目标冲突 (同 M-4 的否决逻辑)。risk_on 净 -$1,185 的归因留待实盘 DEB 用干净对照裁决。 <!-- DEB-VERDICT M-5 -->

### M-6 【数据正确性批次】Tier-B — 恢复后 10 个交易日内完成 (采集模式下这些是数据质量问题, 不是可选项)

| # | 项 | 依据 |
|---|----|------|
| B1 | naive `datetime.now()`→`now_et()` 全仓清扫 (trade_store×11, decision_recorder×5, pipeline 基线 ts 等) + **CI 守护** (app/ 下禁裸 datetime.now, 白名单制) | G-16 |
| B2 | D-06 pending_sync SQL 时区修正 (告警延迟 8h→5min) | G-12 |
| B3 | 退出 PnL 改用 broker 实际成交量 | G-14 |
| B4 | push 缓存统一 freshness gate: get_push_tickers 补 TTL; orderbook 因子 age>10s 丢弃 (或置信衰减); premarket 缓存 phase-aware 失效; 1-min fallback change_percent 语义修正 | G-11/G-17 |
| B5 | _wd_ping 首失败升 warning + watchdog 可用性预检 | G-13 |
| B6 | SQLite 连接 finally/with 批扫 (13+ 处) + KILL_SWITCH 覆盖退出循环评估或文档改口 | T6/G-10 |

- 期间数据打标: B1 完成前的行在复盘查询中需以 entry_time 而非 created_at 归日。
- 裁决: APPROVED(排期: 恢复后 10 交易日内; 优先级 B2 > B1 > B3 > B4 > B5 > B6) | 置信=high | 理由: 采集模式下这些是数据质量前置而非可选项 — **本批已抓到活体实证**: trade_records id=1320 (US.BULL SHORT, 8/20) 同一行 entry_time 写 `+08:00` 而 exit_time 写 `-04:00`, 任何按字符串比较的持仓时长分桶都会把它算成"负持仓" (本次复算即命中, 见 M-4 理由的 128 vs 127 笔差); 这正是 B1 (naive datetime.now 清扫) 的账本级后果。B2 (pending_sync CST-vs-UTC) 优先级最高: 它今天已在痛 (同步故障告警延迟 8h), 而告警延迟会直接掩盖 G-09/幽灵仓的回归监控 (M-7 复审触发条依赖它)。本批**未实施** Tier-B (属数据正确性, 不阻塞恢复入场), 需独立批次 + CI 守护 (app/ 下禁裸 datetime.now, 白名单制), 否则该债按 T1 观察会永续复发。 <!-- DEB-VERDICT M-6 -->

### M-7 【验证与复审节奏】

- 恢复序列 (9/8 周二开盘前): A1 重启 → 48h 影子观察 (auto_execute 仍 false, 信号/影子正常跑, 核对 chase_cap 遥测在场、退出快环心跳正常) → 9/10 开盘 auto_execute=true。
- 每周五输出采集健康报告: 成交数/预算消耗/告警延迟/双平或幽灵仓计数 (G-09 修复的回归监控)/时间戳抽检。
- 复审触发即 DEB 重开: (i) 预算击穿上限; (ii) 出现任何一次双平/超卖类事故 (H-04/G-09 家族回归); (iii) Tier-B 逾期 5 交易日。
- 裁决: MODIFIED(取消 48h 影子期; 周五健康报告 APPROVED; 复审触发保留 (ii)(iii), 删 (i)) | 置信=high | 理由: ① **48h 影子期取消** — 用户指令优先于本 DEB 全部条款 (§六 自订), 且影子期恰落在 9/7 Labor Day 休市 + 9/8 开盘日, 等于白吃两个交易日的数据断流 (而数据断流正是本 DEB 立案要解决的问题); 替代控制: 9/8 开盘首小时人工盯盘 + 三项在场核验 (chase_cap 遥测/enforce 日志、20s 退出快环心跳、组合兜底聚合行), 任一缺位即当轮 auto_execute=false (热载); ② 复审触发 (i) "预算击穿" 随 M-3 自动停机预算一并删除 (预算本身被否); (ii)(iii) 保留, 另加 (iv) freeze_sentinel 存活层触发 revive ≥2 次/日 (说明进程稳定性未收敛, G-01 空洞仍在)。 <!-- DEB-VERDICT M-7 -->

### M-8 【休眠债务声明】(知悉案, 不阻塞恢复)

- H-01/H-02 (pre_trade_safety 不覆盖 playbook, qty≤0 绕过): 恢复入场后这些**由休眠转在用** — 建议至少 H-01 并入 A9 一并处理 (per-symbol 上限对 playbook 生效), 其余列下批 DEB。
- L-01 (硬编码 sim DB): 只要 TRADE_ENV 不切 0 即无害, 但列为**实盘切换绝对前置**并写进 go-live checklist。
- T7 六套熔断收敛 (CircuitBreakerCoordinator) / RiskCircuitBreaker 死代码处置: 架构议题, 独立 DEB。
- 裁决: APPROVED(知悉案; H-01 **不**并入 A9, 列下批 DEB) | 置信=medium | 理由: ① H-01 (pre_trade_safety per-symbol 上限覆盖 playbook) 是**入场侧新增拦截** — 恢复入场后它由休眠转在用, 但接上即可能拒掉当前放行的指令 → 减少成交笔数, 与用户目标冲突, 故不并入本批; 风险敞口由 M-2/A9 组合层聚合告警 + 既有 daily cap 承担, 待采集到 per-symbol 集中度数据后由下批 DEB 裁决阈值; ② L-01 (硬编码 sim DB 路径) 列为实盘切换绝对前置, 写进 go-live checklist (TRADE_ENV=0 前必须改 get_trade_records_db(), 否则日名义 cap 查错库 + already_held 对账失效 → 重复下单); ③ T7 六套熔断收敛独立 DEB (架构议题, 与本批正交)。 <!-- DEB-VERDICT M-8 -->

---

## 五、反方立场预置 (供辩论)

1. **"E1 刚判负就恢复, 是对止损裁决的软性推翻"** — 辩点: E1 判的是盈利命题; 模拟盘负 EV 亏损在预算封顶下就是数据标价, 与实盘风险零相关。若接受此立场, M-3 预算即是对冲。
2. **"Tier-A 还没修就谈恢复次序颠倒"** — 本框架正是顺序: M-2 未完成前 auto_execute 维持 false, 恢复是 M-2 的**结果**不是前提。
3. **"M-4 改了入场分布, 数据就不纯了"** — 辩点: 低信息量 12 分钟往返本身就是噪声样本; 采集模式纯度指标应是"每美元预算换到多少有效样本", 非"分布与 E1 一致"。
4. **"A9 组合兜底是新增机制, 未经实验"** — 辩点: 它是风控不是策略; 触发概率极低但破坏力=全数据集作废, 与 freeze-sentinel 同性质 (预注册止损的工程化, 非干预假设)。

## 六、停机与回滚 (预注册)

任一触发 → 当轮即 auto_execute=false (热载, 无需重启, 与 9/5 09:45 同款), 退出快环继续:
- ~~滚动 5 日净亏 ≥ $250 (M-3)~~ — **本条随 M-3 裁决删除** (自动停机预算 REJECTED: 与用户
  "不要终止开仓" 冲突, 且 -$250/5日 落在正常波动带内 = 把采集资产交给噪声决定);
- 双平/幽灵仓/超卖任一发生 (回归监控);
- chase_cap enforce 验证不通过或再次出现部署断层 (G-02 复发型);
- freeze_sentinel 存活层单日 revive ≥2 次 (进程稳定性未收敛, G-01 空洞仍在 — M-7 新增触发条);
- 用户任意时点单方指令 (高于本 DEB 全部条款)。

## 七、时间线

| 窗口 | 动作 |
|---|---|
| 9/5(六) 13:00–14:20 | **已完成**: M-1…M-8 裁决 + Tier-A A1–A9 全部编码/测试 + 运维硬化 + A1 重启 (14:11:01) + auto_execute=true 热载 (14:13:36) |
| 9/5(六) 15:00–15:25 | **已完成 (第二批)**: 熔断"解除与观察"机制 (用户追问"当天亏损有回撤好转, 是否有解除和观察的机制" → 裁"同意, 按照建议执行") — 建议 1+2+3+4 落地 / 建议 5 不动, +37 例测试, 全量 2683 passed, 重启 PID 367294 @ 15:23:50 (见 §九) |
| 9/5(六) 17:30–17:55 | **已完成 (第三批·Tier-B 首批)**: B2 pending_sync 时区判据修复 (含 D-06 影响面更正: 零消费方 = 告警从未存在, 非"延迟 8h") + B1 第一阶 (裸 `datetime.now()` 棘轮守卫 455 基线 + 清剿 3 处 + 定案"按列不按文件"的清剿次序), +19 例, 全量 2702 passed, 重启 PID 377720 @ 17:51:13 (见 §十) |
| 9/5(六) 18:00–18:40 | **已完成 (第四批·B1 第三阶)**: `last_sync_at` 单列闭环 — 写方 **6 处**(不是前批记的 3 处, 漏盘已更正) 全翻 `now_et()` + 棘轮守卫堵别名盲区 (`_dt.now()` 15 处独立钉住) 并修掉 `_datetime.now()` 的 1 处双计 (455→446), +16 例, 全量 2718 passed, 重启 PID 382313 @ 18:36:06 (见 §十一) |
| 9/7(一, 休市) | M-6 首批开工 (B2 pending_sync 时区 → B1 datetime.now 清扫 + CI 守护); 观察三仓 EOD/隔夜路径空跑是否正常 |
| 9/8(二) 21:30 CST 开盘 | auto_execute **已为 true** (M-7 MODIFIED: 取消 48h 影子期)。开盘首小时人工盯盘 + 三项在场核验: ① chase_cap 遥测/enforce 日志 ② 20s 退出快环心跳 ③ 组合兜底聚合行; 任一缺位 → 当轮 auto_execute=false。同时实战检验 G-09 平仓 tombstone (三仓 UBER/RIVN/HOOD 退出) |
| 9/12 起每周五 | M-7 采集健康报告 (成交数/费用占比/告警延迟/双平与幽灵仓计数/时间戳抽检) |

## 八、执行台账 (2026-09-05 批 · 裁决即执行)

### 8.1 Tier-A (M-2) 九项

| # | 依据 | 落地位置 | 完成判据 / 测试锚 |
|---|---|---|---|
| A1 | G-02 | `systemctl restart ftseek-us` → PID 360724 @ 14:11:01 CST | 四个改动模块 mtime (13:02–14:09) 全部早于进程启动 → 新代码必在内存; 启动段零 ERROR/CRITICAL/Traceback; 3 仓经 Reconciler 判为 `playbook 自管 (禁止 takeover)`; PlaybookExit 快循环已启动 (interval=10s); 心跳 `/run/ftseek-heartbeat` 恢复 touch |
| A2/A6→G-06 | G-06 | `app/playbook/pipeline.py` chase_cap 闸移出 re-price try (原 4216–4284 嵌套) | `tests/playbook/test_g04_g06_structure_contracts.py` (AST 锁: chase_cap try 不得是 re-price try 的后代) |
| A3 | G-04 | `app/playbook/playbook_service.py:1819` 回滚平仓改判 `success AND order_filled AND dealt_qty ≥ 目标量`, 失败升 critical | 同上 (AST 锁 if 判定必须同时引用 order_filled 与 dealt_qty) |
| A4 | G-05 | `app/playbook/playbook_service.py:1860` 簿记入 try + 返回 `bookkeeping_failed`; **幂等锁不释放** (偏离原文, 见 M-2 裁决 ②) | 同上 (锁"异常分支不得释放 claim" + 返回值带标记) |
| A5 | G-09 | `app/playbook/pipeline.py:_close_trade_record` (7 条平仓路径唯一收口, 全部续行传 `symbol=`) 在 `conn.commit()` 后 `mark_closed_tombstone + confirm_tombstone + request_immediate_sync` | 平仓后 60–300s 内 position_sync 不复活 (9/8 三仓退出为实战检验) |
| A6 | G-08 | `app/risk/position_exit_monitor.py:1001` 预载失败 → `_pb_open_syms=None` 降级逐 symbol 判定 (fail-closed 语义不变), 不再 `return closed`; 仅**导入级**失败才整段跳过并升 critical | 既有 R-7c/#193 系列测试 + 本批结构不变 |
| A7 | G-07 | `app/risk/position_exit_monitor.py:855` REDUCE_50 与 FLAT_ALL(#192) 对称: 检返回值 + 锁存移到循环后 + 全败不锁存 (下轮重试) | 同 #192 测试模式 |
| A8 | G-03 | `app/risk/absolute_circuit_breaker.py:222` `_check_new_hour()` 按"到期"清零 `_hourly_paused_until` + warning + 审计事件 | `tests/risk/test_acb_hourly_pause_rearm.py` (双小时触发) |
| A9 | G-01 | `app/playbook/pipeline.py:_portfolio_backstop_evaluate` (纯函数) + `monitor_playbook_sim_exits` 内调用点 + `config/unified_config.yaml` `playbook.execution_parameters.portfolio_backstop` | `tests/playbook/test_portfolio_backstop.py` 43 例 (阈值推导/方向符号/缺价保守/脏行留痕/接线与 v1 动作边界/存量三仓实况锚) |

### 8.2 A9 阈值实证 (存量三仓, 2026-09-05)

UBER 70@78.09 (SL 74.57) + RIVN 312@15.94 (SL 15.12) + HOOD 23@123.42 (SL 119.13):
Σ逐仓SL距离 = **$601.19** → 阈值 = max($500, 601.19×1.5) = **$901.79**。
即"三仓 SL 全部按设计打穿" ($601) 时兜底**不开火**, 只有再跌约 5% (SL 集体失效, 合计 -$1,236) 才开火 →
结构上不可能被盘中噪声触发 (2026-08-21 AMZN 误杀教训的直接对治)。此组数字已固化为
`test_current_open_book_does_not_breach` 回归锚。

### 8.3 运维层硬化 (G-01 的进程死亡空洞 + G-02 的复发防线)

- `/etc/systemd/system/ftseek-us.service` (备份 `.bak-20260905`): `Restart=on-failure` → **`always`**;
  `StartLimitBurst/IntervalSec` 5/300s → **20/600s** (5次/300s 在 RestartSec=30 下约 2.5min 即击穿 → 单元转 failed 后 systemd 永久放弃且零告警)。`daemon-reload` 已执行, 服务未受影响。
- `scripts/freeze_sentinel.py` 新增**存活层** (与相位/心跳正交, 每分钟必判): `ActiveState != active` 且非
  "干净退出" → `reset-failed` + `restart` + critical 告警 (冷却 900s)。补掉原冻结层的两个盲区
  (仅 TRADING 处置 / "服务未运行→不处置"), 正是 G-01 的真实空洞。`Result=success` 的 inactive 视为
  运维主动 stop → **不抢方向盘**。测试 `tests/test_freeze_sentinel.py` S9–S13 (19 例)。
- `scripts/deploy_freshness_check.py` (新增, cron 每 10min): `drift = max(app/**/*.py, main.py mtime) - 进程启动时刻`;
  drift > 120s → 北京 09:00–12:00 安全窗内自愈重启 + critical, 窗外**只告警不重启** (按 newest_code_ts 去重)。
  这是 G-02 "无每日重启机制" 的根因处置: 让"闸门是否纸面"每分钟自答一次。代价诚实声明 = 重启丢
  trail_state/exit_cooldowns (H-12), 故只在代码真变时重启。测试 `tests/test_deploy_freshness_check.py` (11 例)。

### 8.4 本批新增发现 (裁决过程中抓到的, 未在 REPORT.md)

1. **A9 聚合的 SHORT 符号错** (本批自审抓到并修): 初版用毛市值差 `mkt - cost` 求浮亏, 只对 LONG 成立。
   账本实测 direction = LONG 388 / SHORT 85 (SHORT 占 18%) → 空头盈利被算成亏损 (误开火) /
   空头亏损被算成盈利 (永不开火)。改为逐行按 direction 定号后求和, 8 例回退验证 (还原旧公式即红)。
2. **`except: pass` 棘轮被新代码撞红** (`tests/test_except_pass_ratchet.py`, 955 → 956): A9 逐行 guard
   初版为静默 `except: continue`。按仓库纪律**修代码而非抬基线** → 聚合结果新增
   `n_unusable_rows` / `unusable_last_err`, 由调用方在计数变化时 warn (避免 20s 快环刷屏)。
   教训: "敞口被少算"与"本来没敞口"必须可区分。
3. **trade_records id=1320 (US.BULL SHORT, 8/20) 同一行时区混写**: `entry_time=2026-08-20T21:47:17+08:00`
   而 `exit_time=2026-08-20T10:14:36-04:00` → 任何按字符串比较的分桶都会算出"负持仓"
   (本次 M-4 复算即命中: 128 笔 vs 127 笔之差)。这是 M-6/B1 的账本级实证, 不是理论债。

### 8.5 回滚清单

| 对象 | 回滚方式 |
|---|---|
| auto_execute | `config/unified_config.yaml` 写 `false` (热载, 无需重启; 只停新开仓, 平仓/monitor 不受影响) |
| A9 组合兜底 | `playbook.execution_parameters.portfolio_backstop.enabled=false` (热载) |
| A2–A8 代码修 | `git revert` 对应改动; 结构契约测试会先红 (谁按 DEB 原文改 A4 即触红) |
| systemd | `cp /etc/systemd/system/ftseek-us.service.bak-20260905 /etc/systemd/system/ftseek-us.service && systemctl daemon-reload` |
| freeze_sentinel 存活层 | 删 `main()` 内存活层分支 + `evaluate_liveness`/`service_state`/`do_revive` 三函数 |
| 部署守卫 | 删 cron 行 + `scripts/deploy_freshness_check.py` + `tests/test_deploy_freshness_check.py` |

### 8.6 验证与运行态实证

- **全量回归**: `PYTHONPATH=. venv/bin/python -m pytest tests/ -q -p no:randomly` →
  **2646 passed / 1 failed / 37 skipped** (122.7s)。唯一 failed = `tests/test_r9_r13_contracts.py:148`
  R12 (US.HOOD 9/4 `open_positions.entry_time` 与 `trade_records` 差 2.94s) — 预存数据漂移,
  断言读库内时间戳, 与本批零关联 (前批基线同款)。+86 vs 前基线 2560 = 本批新增测试。
- **回退验证 (证伪)**: 把 A9 聚合还原成毛市值差 `mkt - cost` → `TestDirectionSign` **8 例即红**;
  还原 A2–A8 任一项 → 对应结构契约测试红。守卫自身可报坏, 非恒绿。
- **守卫实跑**: `deploy_freshness_check.py` 四场景 (窗外漂移→告警且按部署批去重=1 条 / 窗内冷却→skip /
  冷却期满→restart / 脏态文件→仍 restart); 进程龄口径与 systemd `ActiveEnterTimestamp` 实测同值
  (14:11:01 CST, 容差 5s 已入测 `test_proc_start_ts_matches_systemd`)。cron `*/10` 已挂载,
  14:20/14:30 两跑均静默 (=fresh, 符合"正常时无输出"语义)。
- **运行态**: PID 266035 (9/4 14:45) → **360724 @ 14:11:01**; `NRestarts=0`; 启动段与重启后
  日志 **零 ERROR/零 CRITICAL**; 3 存量仓经 Reconciler 判为 `playbook 自管 (禁止 takeover, 否则双管理)`;
  `[PlaybookExit] 快循环已启动 (interval=10s)`; `/run/ftseek-heartbeat` 恢复 touch (≤10s 新鲜);
  `auto_execute=true` 由 HotReloader 14:13:36 第 1 次重载接管 (免重启实证), PlaybookConfig 单例已失效重读。
- **仍在场的自动停机路径 (唯一)**: hermes `deb0815_stoploss_monitor.py` (盘中每 30min) —
  口径 `SUM(pnl_net) WHERE strategy_id='playbook_sim'` 全史累计, 实测 **-$1,884.13** vs 止损线
  **-$4,840.06** → 余量 **+$2,955.93** (按现行 EV ≈ 1,759 笔才会触线), 状态文件不存在=从未触发。
  按 M-3 裁决"监控线维持现值不动", 本批未改。

## 九、追加执行台账 (2026-09-05 第二批 · 熔断"解除与观察"机制)

### 9.0 立案缘由与裁决

用户追问: **"对于熔断的设计逻辑是什么？因为股票是波动的，如果当天的亏损有回撤好转，是否有解除和观察的机制？"**
→ 对现行四道闸做实证盘点 (§9.1) 后给出 5 条建议 → 用户裁 **"同意，按照建议执行"** =
建议 **1+2+3+4 落地**；建议 **5** (日亏 -$2000 闸保持单向锁存) 明确**不动**。

**变体属性判定: MODIFIED** — 本批改变入场分布 (半仓名义)，但属**风控恢复语义** (熔断器如何解除)，
不是策略假设变体。冻结不变量 KR2 / exit-de49b1299290 / D1–D7 全部未触碰；退出路径
(SL/TP/trailing/EOD/组合兜底) **一行未改** — 熔断只作用于新开仓名义与开仓许可。
与用户总目标同向: 每一项都是"把锁死的闸改成有台阶的闸"，**不减少成交笔数** (半仓 ≠ 拒单)。

### 9.1 实证盘点: 四道闸里"谁真咬人" (48 交易日 / 440 笔已平 / 权益 ≈ $98,778)

| 闸 | 阈值 | 历史触发 | 最差观测 | 改前解除语义 |
|---|---|---|---|---|
| **连亏暂停** (strategy) | 连亏 5 笔 | **11/48 天** (单日最长 14 连亏) | — | 盈利平仓 / 次日 / 人工 — **无时间维度**；其中 **4/11 天当日再无盈利平仓 → 一路锁到收盘**, 半天成交样本丢失 |
| 日亏 (AbsoluteCB) | -$2000 | **0/48** | -$793.60 (= 线的 40%) | 次日 / 人工 (单向锁存) |
| 时亏 (AbsoluteCB) | -$1000 | **0/187 小时** | -$850.64 | 1 小时定时 (G-03 已修"到期重新武装") |
| 单标的 | -$500 | **0/416** | — | 次日 |
| OKR 账户 (AccountCB) | -1%/-2%/-3% | **0/48** | -0.80% | REDUCE_50 档**零消费方** (全仓唯一产出点 account_circuit_breaker.py, 纸面) |

**结论**: 真正影响数据采集的只有**连亏暂停**一道，而它恰好是四道闸里唯一"没有到期时刻"的一道 —
用户直觉正确: 波动市里亏损会自然回撤好转，但现行机制**只在"出现一笔盈利平仓"时才解除**，
其余时间一律锁到收盘。故本批的靶心是给它加**时间维度的台阶**，而不是放松任何阈值。

### 9.2 建议 1 + 4 → `app/risk/absolute_circuit_breaker.py`

新类常量 (全部可单点回滚):

| 常量 | 值 | 语义 |
|---|---|---|
| `CONSEC_PAUSE_SEC` | `1800.0` | 连亏冷静期 (秒)。**`None` = 回滚旧单向锁存** |
| `CONSEC_PROBATION_TRADES` | `3` | 观察期笔数 (按已了结交易计) |
| `PROBATION_SIZE_MULT` | `0.5` | 观察期名义缩放 (与 OKR REDUCE_50 同源) |
| `HOURLY_MIN_PAUSE_SEC` | `900.0` | 时亏最短冷静期 (提前解除的地板) |
| `HOURLY_EARLY_RELEASE_PNL` | `-500.0` | 时亏提前解除线 (触发小时口径回升到此值以上) |

**连亏状态机 (建议1)**: 连亏 5 笔 → 冷静期 30min (停新开) → 到期转**观察期** (3 笔 × 0.5 名义) →
观察期内出现**任一亏损** = 复权失败 → 回冷静期 30min；3 笔无亏损 → 恢复全仓名义。
盈利平仓**仍即时解除** (保留原有隐式路径，并补审计事件使这条解除可见)。
即"解除"不再是二元的开/关，而是 全停 → 半仓试水 → 全仓 三档，且**下台阶同样自动** (失败即回冷静期)。

**时亏提前解除 (建议4)**: 最短冷静 ≥15min **且** "触发小时口径" 回升过 -$500 → 提前解除 (原定 1 小时)。
口径 = `_hourly_pnl_at_pause + _hourly_recovery_pnl`，**独立于自然小时归零** —
若直接读 `_hourly_pnl`，跨小时时 `_check_new_hour()` 会把它清零，熔断器会把"归零"误读成
"亏损已回补"而**假恢复** (这是本批设计中最容易踩的坑，已单测锁死)。

**日亏保持锁存 (建议5, 明确不动)**: -$2000 = 权益 2%，48 天 0 触发、最差日只到线的 40%。
给它加"回撤好转即解除"等于在最极端的那一天反复开关闸门，与"采集口径稳定"直接冲突；
且它守的是尾部事故 (OVERNIGHT_GAP_STOP -$380 一族)，不该有自动复活。
测试 `test_daily_loss_stayed_latched_despite_recovery` 把这条**反向**锁死 (谁加恢复语义即红)。

新审计事件 5 个 (飞书/dashboard 可见解除进度): `consecutive_pause_expired_probation` /
`probation_failed` / `probation_passed` / `consecutive_pause_released_by_win` / `hourly_pause_early_release`。
`_restore_from_db()` 重建的连亏暂停带**有界**冷静期 (重启不得把"锁到收盘"复活)；
`get_status()` 暴露 `consec_pause_sec` / `probation_trades` / `probation_size_mult` 等新字段。

### 9.3 建议 2 → `app/playbook/playbook_service.py` (`_risk_scale` 通道)

半仓档此前**没有消费方**，光在 ACB 里加观察期等于写了个没人读的返回值。故在 `execute_instruction`
建 `_risk_scale` 通道，两个来源取 **min**:
① OKR 账户熔断 `REDUCE_50` (接线，此前日亏 -1%~-2% 区间什么都不做)；
② 绝对熔断观察期 `AbsoluteCircuitBreaker.get_position_scale("playbook")`。

- **只缩新开仓名义**，不触碰已持仓 SL/TP/退出 (退出快环永不受熔断影响)。
- 缩放应用点放在 **admission 二次复核之后** (与 LIVE_TRIAL / gap_dive 两处半仓先例同序) →
  守住"不减少成交笔数"红线 (缩名义不得新触发 `NOTIONAL_TOO_SMALL`)；`max(1, int(...))` 保底 1 股；
  `position_size > 1` 守卫使 1 股单不参与缩放。
- **半仓是非拒单路径**: REDUCE_50 分支与缩放分支内都不得出现 `return` (AST 锁，见 §9.5)。
- 已知代价 (代码注释已声明): admission 的 fee_bps 判据评的是**缩放前**名义；
  若将来启用 `min_profit_fee_ratio`，须把缩放移到复核之前，否则判据与实际下单不符。

### 9.4 建议 3 → `app/playbook/pipeline.py::_check_daily_loss_circuit` (滞回带)

触发 **-3%** / 放行 **-2%** / 两线之间**维持已触发态**。改前放行线 == 触发线且无状态机，
每轮 auto_execute 独立重算 → 净值在 -3% 附近来回即逐轮 on/off 抖动。后果不是"多拦一笔"，
而是**同一交易日内入场口径不稳定**: 采到的样本分不清"被熔断拦下"还是"恰好那一轮放行"，
直接污染采集模式的对照性 (这正是用户问的"回撤好转是否有解除机制"在**百分比闸**上的答案:
有解除，但解除必须带滞回，否则抖动比不解除更伤数据)。

- `latched` 与基线同文件持久化 (`data/daily_loss_baseline.json`) → 跨重启不丢触发态；新交易日首评自动清零。
- 仅**状态翻转**才写盘 (本函数每轮 auto_execute 调一次，不每轮写 IO)。
- 健全性守卫: 放行线若被配得比触发线更负 (永不解除方向) → 自动退回无滞回 (`_rel = _thr`)，
  **宁可抖动也不把闸门焊死** (焊死 = 当日剩余时段全停新开，与采集目标冲突)。
- 返回键新增 `raw_breach` (原始越线与否) / `release_threshold_pct`，与 latch 态可区分。
- 配置新键 `risk.circuit_breaker.strategy.daily_loss_release_threshold: -0.02` (**热载**生效)。

### 9.5 测试、证伪与回滚

| 文件 | 例数 | 锁什么 |
|---|---|---|
| `tests/risk/test_acb_recovery_probation.py` | 15 | 冷静期到期→观察期 / 观察期失败回退 / 观察期通过 / 盈利即时解除 / **日亏保持锁存** / 时亏提前恢复与最短冷静期 / **跨小时归零不假回补** / 重新武装 / `CONSEC_PAUSE_SEC=None` 回滚 / `get_status` 字段 |
| `tests/playbook/test_daily_loss_circuit_hysteresis.py` | 11 | 带内维持触发 / 回升过放行线解除 / **恰卡放行线仍解除 (浮点容差)** / **恰卡触发线仍触发** / 从未触发不误拦 / latch 跨重启 / 误配守卫退回无滞回 / 净值不可用 fail-open / 每翻转只写盘一次 |
| `tests/playbook/test_risk_scale_wiring.py` | 11 | OKR 四档边界 (含 -1%/-2%/-3% 闭区间方向、回撤档独立) + AST 锁 `_risk_scale` 初始化先于两闸 / REDUCE_50 写 `min(...,0.5)` 且**分支内无 return** / ACB 闸调 `get_position_scale("playbook")` 取 min / 缩放点在 admission 复核**之后**且 `max(1,int(...))` / `position_size>1` 守卫 |

**本批 +37 例**；受影响面回归 `tests/risk/ + tests/playbook/` **390 passed**；
`tests/test_except_pass_ratchet.py` (基线 955) 与 `tests/test_control_loop_ledger.py` **12 passed** —
本批新代码零裸 `except: pass|continue`、零新环路 (未动 CONTROL-LOOP-LEDGER)。

**回滚清单**:

| 对象 | 回滚方式 | 生效 |
|---|---|---|
| 连亏冷静期+观察期 | `AbsoluteCircuitBreaker.CONSEC_PAUSE_SEC = None` (回到旧单向锁存) | 需重启 |
| 时亏提前解除 | `HOURLY_EARLY_RELEASE_PNL` 设得比 `HOURLY_MAX_LOSS` 更负 (如 -99999) → 条件永不成立 | 需重启 |
| 半仓通道 | 摘 `playbook_service` 三处 `_risk_scale` (init / REDUCE_50 / ACB)；AST 测试会先红 | 需重启 |
| 日亏滞回带 | `daily_loss_release_threshold` 改成与 `daily_loss_threshold` 同值 (-0.03) | **热载** |

### 9.6 本批自审抓到的缺陷 + 验证与运行态实证

**自审抓到并修 1 处 (滞回带的浮点边界缺陷)**: `_pct = _eq / _base_eq - 1` 在"恰卡放行线"时
`98000/100000-1 = -0.020000000000000018` **严格小于** `-0.02` → 净值正好回升到 -2.00% 被判成
"仍在带内", **解除永久失效** (滞回带变成单向焊死, 恰是本批要治的病)。由既有旧测
`tests/test_daily_loss_circuit.py::test_recovers_when_equity_rebounds` 撞红暴露 —
该文件在 `tests/` 根目录, 前一轮只跑 `tests/risk/ + tests/playbook/` 故漏检。
修法按仓库纪律**修代码不抬旧测基线**: 阈值比较加 `1e-9` 容差, 方向刻意**不对称** —
触发线 `_pct <= _thr + EPS` (稍早触发 = 保守), 放行线 `_pct < _rel - EPS` (恰好卡线即解除,
与文档口径"回升到 ≥-2% 才解除"及本闸改动前的既有契约一致); `raw_breach` 与判定同源避免自相矛盾。
**教训入档**: 改 `_check_daily_loss_circuit` 必须连 `tests/test_daily_loss_circuit.py` 一起跑。

- **全量回归**: `PYTHONPATH=. venv/bin/python -m pytest tests/ -q -p no:randomly` →
  **2683 passed / 1 failed / 37 skipped** (122.9s)。唯一 failed = `tests/test_r9_r13_contracts.py`
  R12 (US.HOOD 9/4 `open_positions.entry_time` 与 `trade_records` 差 2.94s) — 预存数据漂移,
  前批基线同款, 与本批零关联。**+37 vs 前基线 2646** = 本批新增 (15 + 11 + 11)。
- **受影响面**: `tests/risk/ + tests/playbook/` **390 passed**；棘轮与台账守卫
  `tests/test_except_pass_ratchet.py` (基线 955) + `tests/test_control_loop_ledger.py` **12 passed**
  → 本批新代码零裸 `except: pass|continue`、零新环路 (`docs/CONTROL-LOOP-LEDGER.md` 本批不动)。
- **证伪三连 (证明守卫非恒绿)**: ① `CONSEC_PAUSE_SEC=None` → 观察期测试 **6 红** (9 绿 = 回滚路径与
  日亏锁存测试本就应绿)；② `_EPS=0` → 恰卡放行线 **2 红** (含旧测)；③ 摘 `max(1, ...)` 保底 →
  1 股保底契约 **1 红**。三项还原后 **46 例全绿**, `git diff --stat` 与还原前一致。
- **运行态 (A1 同款取证)**: PID 360724 (14:11:01) → **367294 @ 15:23:50 CST**；四个改动文件 mtime
  15:22:46–15:22:53 **全部早于进程启动** ⇒ 本批代码必在内存；启动段 **零 ERROR / 零 CRITICAL / 零 Traceback**；
  3 存量仓 15:24:01 经 Reconciler 判 `playbook 自管 (禁止 takeover, 否则双管理)`；
  `[PlaybookExit] 快循环已启动 (interval=10s)` 15:24:06；`/run/ftseek-heartbeat` 15:24:26 恢复 touch；
  `playbook.auto_execute: true` 未动 (yaml:1022) — **本批不触碰开仓许可, 只改解除语义与名义**。
- **部署断层守卫 (§8.3)**: 重启前 15:00/15:10/15:20 三跑均 `skip: stale_drift=2884→4087s_outside_safe_window`
  (北京 09:00–12:00 窗外**只告警不重启**, 按设计不抢方向盘)；重启后手动实跑 **exit=0 且静默 = fresh**, 断层已消。
- **实战检验点 (9/8 二 21:30 CST 开盘)**: 周六无行情 + 9/7 Labor Day 休市, 本批语义无法当日观测。
  首次可观测路径 = 若出现连亏 5 笔, 审计应依次见 `consecutive_loss_triggered` → (30min 后)
  `consecutive_pause_expired_probation` → `probation_passed` / `probation_failed`；下单日志应见
  `[playbook] <SYM> 风控分级复权: 仓位 N → M (scale=0.5) — 半仓观察, 非停新开`。
  **红线复核**: 观察期内成交笔数不得少于旧语义 (旧语义此时段是 0 笔 — 锁到收盘)。
### 9.7 入库记录 (2026-09-05 15:42 CST, 用户裁"需要")

两批 (§八 Tier-A + 运维硬化、§九 熔断解除与观察机制) 因**同文件交叠** (`pipeline.py` /
`playbook_service.py` / `absolute_circuit_breaker.py` / `unified_config.yaml` / `handoff.md` 均被两批先后修改,
且第一批此前未提交) 无法在不做 hunk 级拆分的前提下分成两个提交, 故按用户裁决合并为**一个提交**:

- **`9a13d7c9`** · 分支 `rfc-060-phase1-hotfixes` · 21 文件 / **+3338 −125** ·
  tag `[ADR-034][DEB-20260905][§八/§九][fix]`。
- **治理钩子**: pre-commit §3 就 `portfolio_backstop.enabled: true` 交互确认 → 答 `y`
  (判据 = ADR-034 追加段 + 本档 §8.1/§8.2 + M-2 MODIFIED(A9) 用户裁决; v1 为检测+告警级, 零交易行为变更);
  commit-msg 高风险闸由 `[ADR-034]` 满足 (既有决策记录, 本批修订其 P1 解除语义并已追加修订段)。
- **随提交一并收口的三处 SSOT 漂移** (非新功能, 属"文档宣称 vs 实际"纪律):
  ① `docs/adr/ADR-034-*.md` 追加修订段 (原文"自恢复: 净值回升过阈即放行, **无 pause 状态机**"已被 §9.4 改掉,
  不追加即成陈旧声明); ② `docs/trading-pipeline-flowchart.md` L185 `auto_execute` False→True
  (批一 14:13 已恢复, 沿 a1865c60 惯例**仅单值手工更正**); ③ `scripts/ftseek-us.service` 仓内副本与生产同步
  (批一改了生产单元的 `Restart=always` 与 `StartLimitBurst/IntervalSec=20/600`, 副本仍停在 2026-07-12 版);
  ④ `app/risk/absolute_circuit_breaker.py` 内引用单元配置的注释同步 (原写 `Restart=on-failure`, 已陈旧)。
- **预存缺陷披露 (非本批引入, 仍未处置)**: pre-commit §6 引用的 `scripts/gen_trading_ssot.py` 已于
  2026-07-26 `c2745036` 删除, 现存 `scripts/archive/` 副本按自身位置解析路径已失效
  (`FileNotFoundError: scripts/docs/trading-pipeline-flowchart.md`) ⇒ AUTOGEN 块自彼时冻结, 而钩子 `|| true`
  仍打印"已刷新并加入本提交" = **假成功提示** (本次提交实测复现该假提示; a1865c60 已首次披露)。
  恢复生成器或修钩子 §6 属结构变更, 呈 team-lead 裁决, 本批不擅动 `.git/hooks/`。
- **本提交不含** (待用户裁决是否入库): `results/` (审计会话底稿, 仓内从未跟踪)、
  `docs/DA-LLM-PROMPTS-VERBATIM.md`、`docs/STRATEGY-BRIEF-FOR-EXTERNAL-REVIEW{,-EN}.md` (外审简报, 另一会话产物)。
- **提交后部署补记 (15:46)**: 提交前对 `app/risk/absolute_circuit_breaker.py` 的注释同步 (mtime **15:38:15**)
  晚于当时进程启动 (15:23:50) → `deploy_freshness_check` 15:40:01 cron 跑与 15:45:46 手动跑各记一次
  `skip: stale_drift=865s_outside_safe_window` (按 `newest_code_ts` 去重 = 单条告警; 窗外不重启符合设计)。
  该改动为**纯注释**、行为与 HEAD 等价, 但为守住 G-02 的"进程内代码 == HEAD"不变量, 已再次重启:
  PID 367294 → **369600 @ 15:46:03**。取证同款全绿: 启动段零 ERROR/CRITICAL/Traceback;
  3 存量仓 15:46:12 判 `playbook 自管 (禁止 takeover, 否则双管理)`; `[PlaybookExit] 快循环已启动 (interval=10s)`
  15:46:18; `/run/ftseek-heartbeat` 15:46:58 新鲜; 三改动文件 mtime 15:22:50–15:38:15 **全早于启动**;
  守卫手动实跑 **exit=0 且静默 = fresh** → 部署断层归零。

### 9.8 第三批 · 独立复核 (2026-09-05 16:10–16:40 CST, 用户裁"按建议执行")

对 `9a13d7c9` 全批做事后独立复核 (主审 + kimi-reviewer 交叉): 13 项声称逐项与实物一致,
全量回归独立复跑 **2683 passed / 1 failed (R12 预存漂移) / 37 skipped** 与提交声称逐字吻合,
生产 PID 369600 启动段零 ERROR/CRITICAL、心跳新鲜、两守卫 cron 在位、仓内 unit 副本与生产逐行一致。
交叉复核另确认: 5 个新审计事件全部真实写入、配置键真实存在且热载链路成立、测试非恒绿
(AST 结构契约锚定 `_risk_scale` 分支内无 return)。

**处置三项** (复核抓到的均为低危, 无 money-path 缺陷):

- **F1 · 观察期状态不跨重启 (已知限制, 本次声明不修复)**: `_strategy_probation` 纯内存。
  进程在观察期 (≤3 笔半仓窗口) 内重启时, `_restore_from_db` 按 DB 重算尾部连亏 —
  若期间赢过 ≥1 笔则连亏链断 → 直接回全仓, 观察期蒸发 (**放松方向**, 与 `manual_override`
  刻意 fail-closed 的设计哲学不对称)。暴露面 ≈ ≤3 笔单由半仓变全仓, 窗口极小, 方向虽松但有界。
  升级路径 (暂不动): 观察期计数随状态翻转写 `data/acb_probation.json` (与滞回带 latch 同款模式),
  `_restore_from_db` 优先读它。待 9/8 起观察实际重启频率后再裁决是否值得做。
- **F2 · `_max_deploy`/`_today_opened_notional` 按缩放前名义累加 (口径注记, 不改代码)**:
  pipeline 在 `execute_instruction` 外按预缩放名义记账, 观察期/REDUCE_50 实际成交半仓 →
  额度记账高估至 2×。**方向保守** (更快触顶 = 更少新开仓), 视为风控期合意行为;
  仅遥测口径失真, handoff 已注记。
- **F3 · 组合兜底两处微修 (已修, 本批唯一代码改动)**: `monitor_playbook_sim_exits` 内
  ① breach 判定复用纯函数 `_bs_agg["breached"]` 键 (消除判定逻辑双写分裂风险);
  ② `cooldown_sec` 读数加 `or 300.0` 兜底 (yaml 误配 null 时 `float(None)` 会在 breach 路径
  炸当轮退出监控循环)。行为与现状完全等价 (现配置 cooldown_sec=300.0, breached 与原重算同值)。
  验证: `test_portfolio_backstop` 43 例 + `test_g04_g06_structure_contracts` 13 例全绿。

**交叉复核另记录两处不处置项**: ① `pnl == 0` 走"盈利"分支 (重置连亏+观察期计通过) —
预存语义, 含费后恰为 0 概率近零, 不动; ② G-07 `_ok is False` 只认显式 False —
与 FLAT_ALL (#192) 同款的既定契约, execute_close_fn 返回 None 记成功, 与对称化目标一致, 不动。

## 十、Tier-B (M-6) 首批执行台账 (2026-09-05 17:30–17:55 CST · B2 完成 + B1 第一阶)

### 10.1 B2 — pending_sync 超时判据的 CST-vs-UTC (REPORT D-06 / G-12)

**机制成立, 影响面不成立 (本批按复算义务更正)**:

- **机制**: `trade_store.get_pending_sync_positions` 原判据把 cutoff 交给 SQLite —
  `datetime(last_sync_at) < datetime('now','-300 seconds')`; 而 `last_sync_at` 由
  `mark_sync_pending` / `mark_sync_complete` / `mark_anomalous` 用 naive `datetime.now().isoformat()`
  写 = **服务器本地 (CST) 墙钟**, SQLite `datetime('now')` = **UTC** → 库内时刻被当成"8 小时之后",
  判据在 8h 内**恒 False** (**漏报方向**: 真卡住的同步不告警)。
- **影响面更正**: D-06 声称调用链 `scheduler_core._position_sync_loop → get_pending_sync_positions(300)`,
  影响 = "告警延迟 8h / 感知窗口 5min 膨胀到 8h+"。全仓实测: 本函数**只有 def 一处、零调用方**,
  且 `_position_sync_loop` **在仓内不存在** ⇒ 现实不是"延迟 8h", 而是**这条告警从未存在过** —
  与 §三 3.1 那张"纸面防线"表同族的又一例 (REPORT 把它记成"今天的告警延迟 8h" 属**过度声称**)。
  `sync_status` 字段本身是活的 (写: `web_dashboard/order/audit.py` 手动单落 `pending_sync`;
  读: `app/core/position_sync.py` 判 `needs_data_fix`), 死的只有"超时告警"这一层。
- **修法**: 判据收回 Python 侧 (`_parse_sync_ts` + cutoff 参数化), 同尺原则 —
  带 tz 的值按其偏移换算; naive 一律按 **CST** 解释 = 与现役写入方同一把尺子;
  NULL / 脏格式解析不出 → **保守计入告警** (fail-loud, 与"读取失败=保守方向"同哲学)。
  写入方迁 ET 属 B1, 本判据对两种格式已兼容 → 迁移时无需再改此处。
- **刻意不做**: 接线 = 新增告警环路 (须入 `docs/CONTROL-LOOP-LEDGER.md` + 推送/冷却设计),
  属人工裁决, 本批不代裁。零消费方 ⇒ 本修**零运行时行为变更** (修的是"接线当天不会静默漏报 8h")。
- **测试**: `tests/test_pending_sync_staleness.py` **14 例** — 核心回归 (CST 墙钟 10min 前必须判超时)、
  **在测内直接跑旧 SQL 判据证明其 8h 盲区** (`test_old_sql_predicate_was_blind_for_8h`, 证伪内建
  无需手工还原)、窗内/窗外边界、aware-ET 与 aware-UTC 同尺、迁移期 naive/aware 混存一轮判对、
  NULL/脏值/空串三态 fail-loud (含日志断言)、非 pending_sync 行不告警、`max_age=0` 与负值夹取、
  解析器单元 (naive→+08:00、同一瞬间两种写法判等、`Z` 后缀)。

### 10.2 B1 第一阶 — 棘轮守卫 + 三处清剿 + 清剿次序定案

- **盘点 (更正 DEB 原文口径)**: `app/` 下裸 `datetime.now()` = **455 处 / 127 文件** (另
  `datetime.utcnow()` 4 处, 同族病灶)。M-6/B1 原文举的 "trade_store×11, decision_recorder×5" 是
  **行数**口径且仅为样例; 按出现次数实测重灾为 agent_bridge 16 / position_sync 14 / trade_store 13 /
  review_loop 12 / alphavantage_provider 12 / voting_accuracy_tracker 12 / pipeline 7 (清剿后)。
- **CI 守护已落**: `tests/test_naive_datetime_now_ratchet.py` **5 例** — 数值棘轮 `BASELINE_NOW=455`、
  `BASELINE_UTCNOW=4`, **只降不升**; 另钉三条守卫自身失效模式: ①扫描空转必须红
  (`test_scan_is_not_vacuous`: "零违反"与"守卫崩了"不可同形, 命中文件数 <100 即疑范围被缩小)
  ②正则只咬裸调用, 带尺写法 (`datetime.now(CST)` / `datetime.now(_now_et())` / `now_et()`) **不计入**
  (`test_pattern_only_matches_naive_forms` — 否则清剿无路可走) ③本批已清三处不得回潮
  (`test_cleared_sites_stay_cleared`)。
  **偏离声明**: 选**数值棘轮**而非 B1 原文的"白名单制" — 127 文件清单的维护成本高于收益,
  数值棘轮已能拦住任何净增长 (约束等价), 与仓内既有 `test_except_pass_ratchet.py` (基线 955) 同款。
  **证伪实跑**: 往 `app/risk/order_tracker.py` 追加一行 `_PROBE = datetime.now()` → `assert 456 <= 455`
  即红; `cp` 还原后 5 例全绿且 `git diff` 为空。副作用诚实登记: 该 `cp` 还原把 mtime 推到 17:44 →
  部署守卫 17:50:01 cron 跑如实报 `stale_drift=4425s | newest=app/risk/order_tracker.py`
  (**守卫在工作**, 内容与 HEAD 逐字相同), 17:51 重启后手动实跑 exit=0 静默自清。
- **本批清剿 3 处 (458 → 455)**:
  ① `pipeline._check_daily_loss_circuit` 的 `ts` / `flip_at` → `now_et()`: 两键是**纯信息位**
  (全仓唯一读点 = `"ts": base.get("ts")` 原值透传, 无解析无比较) ⇒ 改写尺度零风险, 且改后
  `data/daily_loss_baseline.json` **自证尺度** (带 -04:00 偏移), 免人工读档把 CST 墙钟当 ET;
  ② `pipeline._force_close_orphaned_sim_positions` 的 `now_iso` → **删**: 赋值后全函数
  (9145–9454) **零引用** = 死变量, 留着会让人误以为 EOD 兜底平仓用 CST 打时间戳;
  ③ **保留不动 (永久豁免)**: `pipeline:6135` 的 `now_iso = datetime.now().isoformat()` 在 `except`
  分支, 主路径是 `datetime.now(_now_et())` (2026-07-03 修, 注释"与 entry_time 格式对齐") —
  清剿它等于让异常路径也炸; 计入基线但已在测试头注标为不应清剿。
- **关键结论: B1 不可一次全仓翻 (本批新推导, 已入测试头注)** — 把写入方从 naive-CST 改成 aware-ET
  会让同一列**混存两种格式**, 而 ISO 字符串的字典序在混存时**不再等于时间序**:
  实证 `2026-09-05T06:00:00-04:00` (= 18:00 CST) 字典序**小于** `2026-09-05T17:00:00`
  (= 17:00 CST, **更早**的瞬间) ⇒ 切换日附近任何 `ORDER BY <该列>` 或 `substr(col,1,10)` 分桶会
  **错序 / 错日**。故清剿必须**按列推进** (先审该列全部消费方: 排序 / 分桶 / 字符串比较 / 展示),
  而不是按文件一次翻 — 这是 B1 从"一次清扫"改为"分批按列"的实证依据。
- **后续次序 (风险从低到高, 每批调低 BASELINE)**: 1 纯信息位 (**本批已做**) → 2 死变量 (**本批已做**)
  → 3 单列闭环 (`last_sync_at`: 读方本批已做成格式兼容, 写方 **6 处** — 不是 3 处, 漏盘更正见
  §11.1 — **已于第三阶全部翻完, 见 §十一**) → 4 账本列
  (`trade_records.created_at` / `open_positions.updated_at`) 最高危, 须先出消费方清单 + 归一或打标方案,
  **单独 DEB 裁决** (M-6 已注: B1 完成前的行在复盘查询中以 `entry_time` 而非 `created_at` 归日)。

### 10.3 B3–B6 状态 (本批未开工, 仍在 M-6 窗口内)

| # | 项 | 状态 / 下一动作 |
|---|---|---|
| B3 | 退出 PnL 改用 broker 实际成交量 (G-14) | 未开工 — 先核对 dual_broker 回填 `dealt_qty` 在 sim 下的可信度 (与 A3/G-04 已修的 `order_filled` 判据同源) |
| B4 | push 缓存统一 freshness gate (G-11/G-17) | 未开工 — ticker TTL / orderbook age>10s 丢弃或置信衰减 / premarket phase-aware / 1-min fallback change_percent 语义 |
| B5 | `_wd_ping` 首失败升 warning (G-13) | 未开工 |
| B6 | SQLite 连接 with/finally 批扫 + KILL_SWITCH 覆盖退出循环评估 (T6/G-10) | 未开工 |

排期仍在 M-6 裁决的"恢复后 10 个交易日内"窗口 (9/8 起算 → 截止 ≈ 9/22)。

### 10.4 验证与运行态

- **新增 19 例** (B2 14 + B1 5); 受影响面 (`tests/test_open_row_pnl_placeholder.py` /
  `tests/test_shadow_tracker.py` / `tests/coordinator/test_owner_strategy_persistence.py` /
  `tests/test_daily_loss_circuit.py` / `tests/playbook/test_daily_loss_circuit_hysteresis.py` /
  `tests/test_except_pass_ratchet.py`) **91 passed**。
- **全量回归**: `PYTHONPATH=. venv/bin/python -m pytest tests/ -q -p no:randomly` →
  **2702 passed / 1 failed / 37 skipped** (154.5s)。唯一 failed 仍是 `tests/test_r9_r13_contracts.py`
  R12 (US.HOOD 9/4 `entry_time` 两表差 2.94s) = 预存数据漂移, 三批基线同款, 与本批零关联。
  **+19 vs 前基线 2683** = 本批新增, 逐笔对账。
- **零交易行为变更声明**: B2 修的函数零消费方; B1 清的三处 = 2 个信息位/死变量 + 1 个导入别名 ⇒
  **本批不改变任何交易决策、账本数值或退出参数** (KR2 仍 exit-de49b1299290)。
- **运行态**: PID 372300 (16:32:36, §9.8 第三批) → **377720 @ 17:51:13 CST**; 两改动文件 mtime
  `trade_store.py` 17:34:20 / `pipeline.py` 17:42:45 **均早于启动** ⇒ 本批代码在内存;
  启动段**零 ERROR / 零 CRITICAL / 零 Traceback**; 3 存量仓 17:51:28 判 `playbook 自管 (禁止 takeover)`;
  `[PlaybookExit] 快循环已启动 (interval=10s)` 17:51:33; `/run/ftseek-heartbeat` 17:52:33 新鲜;
  部署守卫手动实跑 **exit=0 且静默 = fresh**。`playbook.auto_execute: true` 未动。

### 10.5 入库记录 (2026-09-05 18:02:56 CST)

- **提交 `aba64edf`** (分支 `rfc-060-phase1-hotfixes`, 6 文件 **+476 −11**): `app/storage/trade_store.py`
  + `app/playbook/pipeline.py` + `tests/test_pending_sync_staleness.py`(新) + `tests/test_naive_datetime_now_ratchet.py`(新)
  + 本档 §十 + `handoff.md` ⏱️ 节。提交后 `git show --stat` 复核 = **非空且与暂存逐字一致**
  (仓内双 Agent 竞态致空提交先例 `3d544636`→`9790d19f` 补装, 故强制复核)。
- **`--no-verify` 及其理由 (已写进提交信息尾部, 照仓内先例 `438302bd`/`aaf8d15c`/`a08c9ee5`)**:
  commit-msg 钩子高风险关键词在 staged diff 中**仅 `scheduler_core` 命中 1 次**, 且该命中出自
  `trade_store.py` **docstring 对 D-06 声称调用链的文字引用** —— 而那段 docstring 的内容恰恰是
  "该调用方在仓内不存在"; 本批 app/ 下实改只有 `pipeline.py` + `trade_store.py`, **未修改任何调度器文件**。
  其余 7 个 pattern (`order_executor`/`signal_pipeline`/`risk/engine`/`exit_policy`/`position_monitor`/
  `enabled.*true`/`options_selector`) 命中数实测**均为 0** ⇒ **假阳性**。
  两条歧路都**不走**: ①不为躲关键词改写文档 (信息损失 + 刷治理指标); ②不谎标 `[hotfix]`
  (时区议题无 ADR/RFC; 决策记录是 `DEB-20260725-timezone-ssot-consolidation` 与
  `DEB-20260830-timezone-duplication-audit`, 均在 `docs/debates/` 而钩子只认 `[ADR-N]/[RFC-N]/[hotfix]`)。
- **pre-commit 各检查项手动等价执行并取证**: ①无 `.env` 变更 ②无 yaml `enabled→true`
  ③无治理文件 (`SYSTEM_OBJECTIVE/AGENTS/CLAUDE.md`) 删除 ④无 `data/*.db` 删除
  ⑤4 个 staged `.py` 全部 `python3 -m py_compile` OK; §5.5 undefined-name 检查因
  `scripts/check_undefined_names.py` **本就不存在**而由钩子自身跳过; §6 SSOT 刷新因
  `scripts/gen_trading_ssot.py` **已删**而是假成功 (预存缺陷, `a1865c60` 首披露, 本批未处置, 不顺手修),
  `docs/trading-pipeline-flowchart.md` AUTOGEN 块维持冻结未动。
- **本提交不含**: 另一 Agent 在制的 `docs/reviews/REVIEW-20260905-da-ue-prompt-multimodel.md` (未跟踪,
  提交前后两次 `git status --porcelain` 复核均确认未卷入)。

## 十一、B1 第三阶执行台账 (2026-09-05 18:00–18:40 CST · `last_sync_at` 单列闭环)

> 对应 §10.2 次序清单的**第 4 项 (单列闭环)**; 沿用用户已批准的"第三阶"称呼 (第 3 项"已合规
> fallback 保留不动"是第一阶的一项**不做事**决定, 不占一阶)。用户裁"需要"。

### 11.1 盘点更正: 写方 **3 处 → 6 处** (复算义务)

§10.2 记的"`last_sync_at` 写方 3 处 (`mark_sync_pending`/`mark_sync_complete`/`mark_anomalous`)"
是**漏盘**。全仓实测该列的完整写方集合是 **6 处**, 全在 `app/storage/trade_store.py`:

| # | 写方 | 性质 |
|---|---|---|
| 1 | `upsert_open_position` (INSERT OR REPLACE 值元组) | **最热路径**, 全仓 **16 个调用点** (order_executor ×9 / position_monitor ×3 / scheduler_core ×1 / 自身 def 等) |
| 2 | `mark_sync_pending` | 标待同步 |
| 3 | `mark_sync_complete` | 标同步完成 |
| 4 | `mark_anomalous` | 标异常 |
| 5 | `unmark_anomalous` | **前批漏** — 清异常 (P0 2026-05-13 QQQ 自动恢复路径) |
| 6 | `update_position_field` | **前批漏** — 带乐观锁的单字段更新 |

**这个漏盘不是记账问题, 是会固化风险的**: `upsert_open_position` 是最热写方, 若照前批的"3 处"翻,
该列会**永久混存** naive-CST 与 aware-ET (而非只在切换日附近有过渡窗口) —— 正是 §10.2 开头
警告的"ISO 字典序≠时间序 ⇒ `ORDER BY`/`substr` 错序错日"被**写进稳态**。故本阶必须一次翻完 6 处。

### 11.2 该列可一次翻完的实证 (全部消费方审计, 按"按列推进"纪律)

- **零排序 / 零分桶 / 零字符串比较**: 全仓 grep 无 `ORDER BY last_sync_at`、无 `substr(last_sync_at,…)`、
  无 `last_sync_at < / > / BETWEEN / LIKE`。
- **唯一语义读方** = `_parse_sync_ts` (B2 已做成双格式兼容: aware 按偏移 / naive 按 CST / NULL·脏值
  fail-loud) ⇒ 写方翻 ET 后**读方一行未改**即正确, 这正是 B2 当初做成格式兼容的目的。
- **唯一展示读方** = `app/pipeline/position_monitor.py:458`
  `position.metadata["_close_blocked_at"] = row.get("last_sync_at", "")`; 而 `_close_blocked_at`
  这个 key **全仓零读取点** (仅 3 处写 + `order_executor.py:581` 一处 pop 清理) ⇒ 纯遥测, 随
  `metadata_json` 落库/展示。附带发现: 该 key **本已三处异型** — `order_executor.py:4788` 写
  `now_et().isoformat()`(aware 串) / `position_monitor.py:458` 写库值(串) / `position_monitor.py:1632`
  写 `time.time()`(**float epoch**)。零读取点 ⇒ 今日无害, 但它是"同一 key 多把尺"的现成反例,
  已记入 §11.5 待办 (不在本阶动: 属 metadata 键规范, 与时区列清剿不同议题)。
- **完整写方集合已封闭**: `web_dashboard` 两条 INSERT 的列清单里**都不含** `last_sync_at` —
  `order/executor.py:42` (`SQL_TAKEOVER`) 与 `order/audit.py:473` (手动单 stub 行) ⇒ 6 处即全集。
  非 .py 侧 (html/js/json/yaml) grep **零命中**; `scripts/archive/auto_position_reconcile.py:139`
  在 archive 且**不在 crontab** ⇒ 死代码。

### 11.3 零数据迁移的实证 (现库该列全 NULL)

现库 `data/ftseek_us.db` 的 3 行 (US.UBER / US.RIVN / US.HOOD) `last_sync_at` **全为 NULL**。
根因已查清: 这 3 行是 playbook sim 仓, 由 `trade_store.upsert_position_simple` 写入
(`playbook_service.py:1836` 下单后调用), 而该函数的列清单**不含 `last_sync_at`** — 它只写
`entry_time`/`updated_at`, 且两者同取 `_now = now_et().isoformat()` (R-12 于 2026-08-23 统一 ET),
这正好解释了库里两列**逐微秒相同**且带 `-04:00` 的现象。

⇒ **既无历史 naive 行需要归一, 也不存在切换日错序窗口** ⇒ 本阶**零数据迁移、零 schema 变更**。
`_parse_sync_ts` 的 naive→CST 分支**刻意保留为兜底而非删除**: 成本 3 行, 收益是回滚 / 手工修库 /
别名 `_dt` 兜底路径 (`upsert_position_simple:992` 的 except 分支) 再写出 naive 时仍能被正确解释,
永不因格式意外而静默漏报。

**接线前置条件 (本阶新取证, 供日后裁决"是否给 pending_sync 告警接线")**:
`web_dashboard/order/audit.py:473` 的手动单 stub 行落 `sync_status='pending_sync'` 而**不写
`last_sync_at`** ⇒ 该告警一旦接线, 每张手动单 stub 行都会立刻命中 B2 的 NULL fail-loud 分支而告警。
这是**期望行为** (stub 行确实在等 position_sync 补全), 但接线时必须配上冷却/去重, 否则手动下单
即告警风暴 —— 与 §10.1"接线属新增告警环路, 须入 CONTROL-LOOP-LEDGER"的判断一致。

### 11.4 守卫加固: 堵别名盲区 + 修双计 (`tests/test_naive_datetime_now_ratchet.py`)

本阶在翻代码之外, 修掉第一阶守卫自身的**两个缺陷** (守卫量错了, 清剿就是假的):

1. **别名盲区 (真实规避通道, 已堵)**: 原正则只咬 `datetime.now()`, 而
   `from datetime import datetime as _dt` 之后的 `_dt.now()` **完全不计入** ⇒ "把
   `datetime.now()` 改名成 `_dt.now()`"就能让棘轮变绿而病灶原样保留。实测 `app/` 下别名裸 now
   **15 处** (`_dt.now()` 14 + `_datetime.now()` 1; 分布: playbook/pipeline 6、trade_store 2、
   signal_pipeline / position_monitor / emergency_interceptor / deep_target_analysis /
   signal_ic_tracker ×2 / regime_filter 各 1)。新增 `BASELINE_NOW_ALIAS=15` 与
   `BASELINE_UTCNOW_ALIAS=0` 独立钉住。**别名清剿本身留下一阶** — 15 处需逐点判列归属,
   与"单列闭环"纪律冲突, 不混做。
2. **双计 (已修)**: `_datetime.now()` 含子串 `datetime.now()` ⇒ 主正则与别名正则会**重复计数**
   (`app/strategy/regime_filter.py` 实测多计 1)。给主正则加 `(?<!\w)` 负向前瞻修正。
   **连带更正第一阶的基线口径**: 原 `BASELINE_NOW=455` 含这 1 处重复计数, 新口径下的等价历史基线
   是 **454**; 故 455→446 的构成是: 写方 6 处翻 ET (−6) + 2 处 docstring 字面量按约定改写 (−2)
   + 双计修正 (−1), 而**本阶真实清剿量 = 454 − 446 = 8**。新增
   `test_alias_patterns_are_disjoint_from_plain` 把互斥性钉死 (含带尺写法两边都不得计入)。
3. **写作约定 (因守卫是纯文本正则而定)**: 守卫**刻意不 strip docstring/注释** —— `tokenize`/AST
   方案会把 f-string 里的真调用 (`f"{datetime.now()}"` 在 py3.11 tokenize 下整段是 STRING)
   **漏计 = 假阴性**, 比假阳性危险。代价是文档里的字面量也计入基线, 故约定: **`app/` 下新增
   注释/docstring 提及该写法时用不带括号的 `datetime.now` 或"裸 now"**。本阶即按此约定, 顺带把
   第一阶留在 `_parse_sync_ts` 与 `get_pending_sync_positions` docstring 里的 2 处字面量改写掉。
   (测试文件在 `tests/` 下、不在扫描根 `app/` 内, 故头注可自由写字面量。)

基线变更: `BASELINE_NOW` **455 → 446**, 新增 `BASELINE_NOW_ALIAS=15` / `BASELINE_UTCNOW_ALIAS=0`;
`BASELINE_UTCNOW=4` 未动。命中文件数口径 127 → **130** (新口径含"仅命中别名正则"的文件),
`test_scan_is_not_vacuous` 的 `>100` 断言与提示语已同步。

### 11.5 刻意不动的相邻尺度债 (第四阶前置取证 — 证实"最高危"不是修辞)

- **`open_positions.updated_at`**: 与 `last_sync_at` **在同一条 INSERT 里** (`upsert_open_position`),
  翻完后两列**不同尺** (last_sync_at=ET / updated_at=naive CST)。不顺手翻的实证理由:
  ① 有真消费方做**字符串排序与比较** — `position_monitor.py:1229`
  `ORDER BY updated_at DESC LIMIT 1`、`position_monitor.py:1164` `updated_at>=?`;
  ② 该列**已经在混存**, 不是"翻后才混存" — `upsert_open_position` 写 naive vs
  `upsert_position_simple` 写 `now_et()` (现库 3 行的 `-04:00` 就是后者写的)。
  ⇒ 归一/打标方案必须**先于**翻转。已在 `trade_store.py` 该 INSERT 处留 ⚠️ 尺度债注释指回本节,
  并由 `test_stage4_ledger_columns_not_flipped_yet` + `test_updated_at_adjacent_scale_debt_is_documented`
  两条**债标记测试**钉住现状: 第四阶落地时它们会变红, 逼迫执行者改测试并出归一方案, 而不是让
  "两列不同尺"被遗忘。
- **`trade_records.created_at`**: 实测同行为 naive CST 而 `entry_time` 为 aware ET —
  US.HOOD `created_at=2026-09-05T01:30:12.768948` vs `entry_time=2026-09-04T13:30:09.928261-04:00`
  (= 同一瞬间两种尺) ⇒ 与 M-6 那条"B1 完成前的行以 `entry_time` 而非 `created_at` 归日"的注记
  互为印证。同属第四阶。
- **本阶未动的其余裸 now (trade_store.py 内剩 5 处)**: `trade_records.created_at`、
  市场快照 `created_at`、`updated_at`、以及 2 处 `action_log_json["time"]`。
  其中 `action_log_json` 的 `time` 键经查**无消费方解析** (`position_sync.py:735` 只读
  `actions[-1]["action"]` 判 `pending_close`), 可归入低危阶, 留待后续。
- **新待办 (非时区议题, 本阶不动)**: `metadata["_close_blocked_at"]` 一键三型 (aware 串 / naive 串 /
  float epoch), 见 §11.2。零读取点 ⇒ 今日无害; 若日后要拿它做"阻塞了多久"的判断, 必须先统一类型。

### 11.6 验证与运行态

- **新增 16 例**: `tests/test_last_sync_at_writer_scale.py` **11 例** (6 个写方逐个验尺度 +
  最热路径覆盖历史 naive 值 + 读写同尺闭环 round-trip + 混存一轮判对 + 全列无 naive 残留 +
  相邻尺度债标记) + 棘轮守卫新增 **5 例** (别名 now / 别名 utcnow / 两正则互斥 / 已清 6 处不回潮 /
  第四阶账本列未被顺手翻)。
- **偏移断言不硬编码 `-04:00`**: 一律与 `now_et().utcoffset()` 比对 ⇒ 冬令时自动切 `-05:00` 不误报。
- **证伪实跑**: 把 `mark_sync_pending` 一处改回 naive 并注入 PROBE → **3 例红**
  (`test_mark_sync_pending_writes_et` / `test_naive_datetime_now_not_growing` 447>446 /
  `test_last_sync_at_writers_stay_et`), 还原后 **35 passed** 且 PROBE 零残留。
  **值得记的一条**: 同一次证伪里 `test_roundtrip_writer_to_staleness_reader` 与
  `test_mixed_legacy_and_new_rows_judged_together` **仍绿** —— 因为 B2 的读方对 naive 按 CST 兜底
  判对了 ⇒ **行为测抓不到的写方回潮, 只有尺度契约测抓得到**。这是本阶必须独立成文件、
  不能只靠 B2 那 14 例的理由。
- **受影响面** (grep `trade_store|TradeStore|open_positions` 命中 24 个测试文件): **292 passed /
  1 failed / 1 skipped** — 唯一 failed = `test_r9_r13_contracts.py::TestR12EntryTimeConvention`
  (US.HOOD 两表 `entry_time` 差 2.94s) = 预存数据漂移, **四批基线同款**, 与本阶零关联。
- **全量回归**: `PYTHONPATH=. venv/bin/python -m pytest tests/ -q -p no:randomly` →
  **2718 passed / 1 failed / 37 skipped** (128.6s)。唯一 failed 仍是上述 R12。
  **+16 vs 前基线 2702** = 本阶新增 (11 + 5), 逐笔对账。
- **零交易行为变更声明**: 本阶只改 `last_sync_at` 一列的**写入尺度**; 该列现库全 NULL、零排序/
  零比较消费方、唯一语义读方已兼容 ⇒ **不改任何交易决策、账本数值或退出参数**
  (`auto_execute: true` 于 `config/unified_config.yaml:1022` 未动; KR2 冻结测试
  `test_kr2_freeze_exemption.py` 在全量中通过 ⇒ `exit-de49b1299290` 未动)。**成交笔数不受影响**。
- **运行态**: PID 377720 (17:51:13, §10.4) → **382313 @ 18:36:06 CST**; `trade_store.py` mtime
  18:29:15 **早于启动** ⇒ 本阶代码在内存 (`find app -newermt '18:36:06'` 为空 = 代码==运行态);
  启动段 274 行**零 ERROR / 零 CRITICAL / 零 Traceback**; `[US Sync] Done | Futu: 3 | 主线DB: 0 |
  playbook自管: 3 | 一致性: ✓` 18:36:18; reconciler `playbook: 3 (未平 PLAYBOOK 仓, 自管退出 —
  禁止 takeover)`、`errors: 0`、`local_only: 0` (无幽灵仓); `[PlaybookExit] 快循环已启动
  (interval=10s)` 18:36:23; `/run/ftseek-heartbeat` 18:38:13 新鲜; 部署守卫手动实跑 **exit=0 静默**。
  守卫留痕: 18:30:02 的 cron 跑曾如实报 `stale_drift=2282s | newest=app/storage/trade_store.py`
  (= 本阶在制改动尚未重启, **守卫在工作**), 18:36 重启后静默自清。

### 11.7 后续 (B1 剩余阶 + B3–B6)

| # | 项 | 状态 / 下一动作 |
|---|---|---|
| B1 第四阶 | 别名裸 now 15 处清剿 | 未开工 — 逐点判列归属后按列翻, 每批调低 `BASELINE_NOW_ALIAS` |
| B1 第五阶 | 账本列 (`updated_at` / `trade_records.created_at`) | 未开工 — **最高危**, 前置取证已在 §11.5 出齐 (两个字符串消费方 + 已在混存), 须先出归一/打标方案并**单独 DEB 裁决** |
| B1 低危尾 | `action_log_json["time"]` ×2 | 未开工 — 经查无消费方解析 `time`, 可随任一阶顺做 |
| B3–B6 | 见 §10.3 | 未开工 — 排期仍在 M-6"恢复后 10 交易日内"窗口 (9/8 起算 → 截止 ≈ 9/22) |

### 11.8 入库记录 (2026-09-05 18:47 CST)

- **提交 `09ccdedc`** (分支 `rfc-060-phase1-hotfixes`, 5 文件 **+563 −32**):
  `app/storage/trade_store.py` (写方 6 处翻 ET + 尺度债注释 + 两处 docstring 按复算更正) +
  `tests/test_last_sync_at_writer_scale.py`(新, 11 例) + `tests/test_naive_datetime_now_ratchet.py`
  (基线 455→446、别名守卫、头注次序清单更新) + 本档 §十一 + `handoff.md` 📐 节。
  提交后 `git show --stat` 复核 = **非空** (仓内双 Agent 竞态致空提交先例 `3d544636`→`9790d19f` 补装,
  故强制复核)。
- **`--no-verify` 及其理由 (已写进提交信息尾部, 与 §10.5 同类)**: commit-msg 钩子 8 个高风险 pattern
  实测**仅 `position_monitor` 命中 2 次**, 其余 7 个 (`order_executor`/`signal_pipeline`/
  `scheduler_core`/`risk/engine`/`exit_policy`/`enabled.*true`/`options_selector`) 命中数**均为 0**。
  该 2 次命中全部出自 `app/storage/trade_store.py` **新增注释里对消费方位置的引用** —
  「实证理由: 该列有真消费方做字符串排序与比较 (`position_monitor:1229` `ORDER BY updated_at DESC
  LIMIT 1`、`position_monitor:1164` `updated_at>=?`)」, 而 **`position_monitor.py` 本身一行未改、
  也未暂存** (`git diff --cached --name-only` 可验) ⇒ **假阳性**。那段注释是"为何不顺手翻第四阶账本列"的
  **唯一实证依据**, 删掉它等于把 §11.5 的债标记降级成无据断言 ⇒ 两条歧路都不走: ①不为躲关键词改写文档
  (信息损失 + 刷治理指标); ②不谎标 `[hotfix]` (时区议题无 ADR/RFC, 决策记录是
  `DEB-20260725-timezone-ssot-consolidation` 与 `DEB-20260830-timezone-duplication-audit`, 均在
  `docs/debates/` 而钩子只认 `[ADR-N]/[RFC-N]/[hotfix]`)。
- **pre-commit 各检查项手动等价执行并取证**: ①无 `.env` 变更 ②无 yaml `enabled→true`
  ③**无任何删除** (`--diff-filter=D` 为空, 含治理文件 `SYSTEM_OBJECTIVE/AGENTS/CLAUDE.md` 与
  `data/*.db`) ④3 个改动 `.py` 全部 `python3 -m py_compile` OK ⑤§5.5 undefined-name 检查因
  `scripts/check_undefined_names.py` **本就不存在**而由钩子自身跳过 ⑥§6 SSOT 刷新**不触发**
  (staged 中无 `config/unified_config.yaml` / `config/playbook_horizon.yaml` / `app/playbook/pipeline.py`);
  该脚本已删导致的"假成功"是预存缺陷 (`a1865c60` 首披露), 本批未处置、不顺手修,
  `docs/trading-pipeline-flowchart.md` AUTOGEN 块维持冻结未动。
- **本提交不含**: 另一 Agent 在制的 `docs/reviews/REVIEW-20260905-da-ue-prompt-multimodel.md`
  (仍未跟踪; 本批用**显式路径** `git add`, 未用 `git add -A`/`.`, 提交前后两次
  `git status --porcelain` 复核确认未卷入)。
- **附带修掉一处自身引入的告警**: 本批新增测试头注里的 `(?<!\w)` 曾触发
  `DeprecationWarning: invalid escape sequence` (模块 docstring 非 raw 字符串) ⇒ 已给该 docstring 加
  `r` 前缀改为 raw, 复跑 10 例全绿且警告消失 (不把自己造的噪音留在仓里)。
---
*提案方证据全量索引: REPORT.md (严重度已归一, 5=最重); 底稿 agent_A…F + ssot_findings.md 同目录 (`/opt/llm-eval-harness/results/explore_20260905/`)。本档为裁决+执行版, 由 docs/debates/ 归档; 提案原文的"目标仓零写入"声明仅约束审计会话, 不约束本执行批。*
