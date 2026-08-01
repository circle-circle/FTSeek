# FTSeek-US · HANDOFF

> **这是什么**: DA-FTSeek 链路迭代的**活文档**。每次调整/计划都追加到这里, 避免跨会话/跨 Agent 从头开始。
> **怎么用**: 每次改动后更新"变更日志"(最新在最上) + "下一步"。其他 Agent 先读本文件再动手。**多模型协作规范见 `docs/specs/multi-agent-collaboration-charter.md`（角色协议/数据口径七律/落档强制/验证期变更纪律，含用户在内人人遵守）。**
> **最后更新**: 2026-07-28 (DA 提速批独立复核) · 环境: 富途**模拟盘** (v2 sim, 无真实资金) · venv: `./venv/bin/python`

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
| `python scripts/okr_status.py` | OKR 总览 (胜率/PnL/IC/因子/void率趋势)。落 `data/okr_latest.json` |
| `python scripts/trigger_gate_report.py` | 触发门判分 (只读): executed vs missed 的 fwd_return 缺口/方向分层/机会成本。落 `data/trigger_gate_latest.json`+history |
| `python scripts/direction_hit_scorecard.py [--since D]` | 方向命中率记分牌 (只读, 2026-07-24): 指令台账 fwd_return 按 方向/来源/辩论共识/regime/周 切片 — "LLM看没看清方向"的信心仪表。落 `data/direction_scorecard_latest.json` |
| `python scripts/exit_replay_backtest.py [--since D] [--refresh-bars]` | 离线退出回放器 (只读, 2026-07-25): PLAYBOOK_SIM 历史交易 × 1min bar 反事实回放 dd/be/tiered/early_tight 网格, 双序区间+Δbaseline 口径。落 `data/exit_replay_latest.json`, bar 缓存 `data/replay_bars/`。详见 `docs/debates/DEB-20260725-exit-replay-backtest.md` |
| `python scripts/entry_gate_replay.py [--since D]` | 入场门/时点回放 L2 (只读, 2026-07-26): 全部方向指令 (含未成交) × 1min bar — ATR门网格拦对/拦错 + 入场时点变体 (09:35~11:00)。ATR 缺失用日K回算补齐 (无前视)。落 `data/entry_gate_replay_latest.json`。详见 `docs/debates/DEB-20260726-backtest-l2-l3.md` |
| `python scripts/historical_robustness_check.py [--years 5]` | 跨期稳健性 L3 (只读, 2026-07-26): 78 标的 × 5 年日K, ATR 分桶机会结构逐年检验 — 防"6 周样本规律"当结构规律引用。日K缓存 `data/replay_bars_daily/` (与 L2 共享)。落 `data/historical_robustness_latest.json` |

## 🗺️ 退出旋钮地图 (`config/playbook_horizon.yaml`, DA 恒走 `short_horizon`)
**A. 赢家被锁太小 (R:R 缺口主因) — `factor_exit_rules.default`**
- `drawdown_from_peak_pct` = **0.8** (L49) ← 07-19 从 0.4 回归到 0.8 (handoff 处方)。给赢家 0.8% 呼吸空间, 不再 0.4% 就锁。
- `breakeven_at_pct` = 0.5 (L48) ← 不动, 仍早保本 (亏不了)。

**B. 赢家被噪声扫损 (杀赢家) — `short_horizon.vol_buckets`**
- low `sl_floor_pct`=2.0 (L370), medium=2.5 (L378) ← 多数标的落此, 实际止损≈2-2.5%
- ⚠️ 07-09 基于**日线回测**收紧过(“2-3%最优”); 与 live scorecard 冲突时**信 scorecard**。

**C. 已核实**: `short_horizon.rr_ratio=999` (L353) → 固定止盈**关闭**, 靠 trailing+EOD。所以封顶赢家的是 A 的 drawdown 锁, **不是** rr_ratio。

**D. 早期紧SL (方向全错快速脱身) — `unified_config.yaml::playbook.execution_parameters`**
- `early_tight_sl_enabled` = **false** ← 07-20 修复M 从 true 降级为 **shadow-only** (积累期实单退出参数冻结)。
- 逻辑保留供口径对齐: 入场后30min内 breakeven未激活 → SL收紧到1%. 但**不再实单化**, 改由 `exit_shadow.EARLY_TIGHT_VARIANTS` (`early_tight_30m_1pct`) record-only 采集反事实 `counterfactual_saved_pct` (正=紧SL更优)。
- **为什么降级**: 见变更日志 07-20 修复M — 积累期每改一次实单退出参数就断一次 config_version 窗口, 污染毕业(胜率/PnL)判定。新退出规则一律先走 shadow, 2 周数据选出赢家再一次性切换。
- 回滚/实单化: `early_tight_sl_enabled` 改 true (哈希变化会自然开新 config_version 窗口)。

## ➡️ 下一步 (按时间顺序)
0a. **07-25 04:20 CST (收盘后, 已排定时任务)**: 重启 `ftseek-us.service` 激活凌晨修复批 (5项, 见变更日志首节) + 启动验证三判据: ①[ConfigGov] 热重载/漂移检测启动 + [ConfigAudit] 审计通过 ②无启动期 stages Pipeline 扫描 ③Reconciler 无 playbook 仓 broker_only 补录。
0. **八月终裁框架已预注册** (`docs/specs/august-verdict-framework-20260831.md`, 2026-07-24 冻结): 08-29 盘后按 G1~G5 判据出 GO(切实盘)/PIVOT(降频高信念或更长持有期或纯事件驱动)/STOP 结论。8月只允许"修尺子、攒样本、做减法"三类动作，禁新机制。前置：P0.2 conf修复(X1赛道前提) + UE tier透传(事件赛道前提, 周末补)。
1. **07-25 晨**: 看两个定时任务产出 — 00:30 盘中链路验收 + 08:30 首夜报告 (`scripts/first_night_gate_report.py`)。判据异常 → 按 crash-day DEB 第8轮预案一键回滚 (三开关在 `playbook.execution_parameters`)。
1b. ~~**07-25 04:20 CST** (美股收盘后): 重启 `ftseek-us.service` 激活 topup 盘中窗~~ → 已合并入 0a (盘中窗 07-24 晚重启时已提前激活, 本次重启另携 yaml 盘中窗恢复 1130 + 凌晨修复批)。
1c. **07-27 周一 10:30-11:30 ET**: 盘中窗首日验证 — 日志 `[playbook·topup] 补量启动 (intraday)` + DB 新行 `raw.timing=intraday` + 定桶/regime 当轮补齐; 效果按 timing 切分 (盘前批 vs 盘中批)。回滚: `intraday_start/end` 设同值。
2. **07-24 scan 生成后**: 跑挑战者 A/B 第2轮 `./venv/bin/python scripts/challenger_model_ab.py 2026-07-24 15`, 两日合并裁决是否切 glm-5.2 (首轮裁决: 机械判据过/行为判据存疑, 维持 deepseek)。
3. **周末 (07-25/26)**: 已立项 P-1 名单合一(角色标签单真源, 非简单并集——watchlist 含低波防御股不可入 topup) + P-2 订阅动态化(指令+持仓+基准; 前置=隐性消费者 5 处定性)。方案见 da-chain-audit DEB §七。~~ConfigGov 失联~~/~~预热绕过门~~/~~补录守卫~~/~~解锁反模式 9 处~~/~~MFE shadow 死块~~ → 已于 07-25 凌晨提前修复 (见变更日志首节), 周末不再重复。
3b. ~~**待用户裁决 (辩论已完成)**~~ → **✅ 已裁 2026-08-01: 方案 B 批准** (见变更日志首节): `docs/debates/DEB-20260725-llm-cache-cross-symbol-pollution.md` — LLM 缓存跨标的污染。**辩论方 (主工程会话) 已逐 Q 实证作答 (§八)**: Q1 无隐蔽消费端 / Q2 死路由成立 (grep 命中全是注释) / Q3 日志零命中→方案B依据成立 / Q4 追加发现 audit 库 57% 是 test 污染。**双方一致推荐方案 B** (附失败提示验证条件)。
3c. ~~**待用户裁决 (辩论已完成, 有分歧点)**~~ → **✅ 已裁 2026-08-01: 终裁后再议** (采纳辩论方立场, D 类前置可先做, 见变更日志首节): `docs/debates/DEB-20260725-llm-thinking-mode-tradeoff.md`。**辩论方作答 (§七)**: 事实链全部复核属实 (含 hermes config 0 thinking 键实锤); **分歧点 = B 的时机**: 作者倾向三前置后灰度, 辩论方指出 Q1 判据需 4-6 周样本与 08-29 终裁撞期, 建议 **D 立即做、B 延至终裁后**。用户拍板: 终裁后再议。
3d. ~~**待用户裁决 (辩论已完成)**~~ → **✅ 已裁 2026-08-01: B 三件套全做批准** (见变更日志首节): `docs/debates/DEB-20260725-llm-failure-semantics.md`。**辩论方作答 (§五)**: 事实链复核属实; **支持 B 三件套全做** (②重试分流加 flag + 双信号触发 + 比例制降幅修正), 作为 thinking 灰度硬前置且独立价值成立。双方无实质分歧。
3e. **新起案 (待辩论/裁决)**: `docs/debates/DEB-20260725-da-ue-pe-agent-optimization.md` — DA/UE 链路 PE 工程与 Agent 编排优化 (用户命题)。P1-P5 (PE层: 可证伪字段/rubric化conf/负反馈注入/信息饮食/SHORT高门槛) + A1-A4 (编排层: **分歧奖励反转**=最大错位/UE self-critique/裁判分离存储/时效复核) + E1-E2 (评估闭环)。修尺子五项可先行, A1 等 n≥100 (衍接 08-07), 编排重构终裁后。预注册 Q1-Q4。
3f. ~~**新起案 (待辩论)**~~ → **已落地+三角色闭环 (2026-07-29)**: `docs/debates/DEB-20260729-position-path-lake.md` — 辩论方 T2 修正 (钩子→盘后批量 cron 04:35 CST) 后落地, Qoder 独立复核 G1/G3/G5 全复现 (§十一)。**G3 首日即校准: dd 0.6 优势 +$406→+$85 (一阶近似高估 5 倍实证), 08-07 假设 H(0.6>0.8) 保持但期望值校准**。观察项 ([time,close] 无 OHLC) 经 §十二证伪+§十三接受撤销 — 实为 [t,o,h,l,c] 五元组, 误因系复核打印自行切片, data-map 已补格式行。DEB 状态 ✅ 已验证。UE O1/O2 裁决独立于本案仍在 DEB-20260729-supply-side §二待裁。
3g. ~~**辩论完成, 待用户裁决 4 点**~~ → **✅ 已裁 2026-08-01: 全部 4 点批准** (见变更日志首节; 交易影响侧尊重冻结按自带保险丝排队): `docs/debates/DEB-20260729-topup-event-veto-shadow.md` — 辩论方 **T1 反转**: news_sentiment 实测三值 {0:77,+1:35,-1:3} 非退化 (作者 §二结论系 falsy 过滤 bug+小窗, 已自我修正), veto 直接用现存 sentiment+earnings_days_until 两字段零新建; T3 收窄为财报前≤1日; T4 G4 降级日级。**裁决点**: ①F2a (半小时语义确认+口径成文) 现在做否; ②F2b 缓办规则批准否 — F1 三日速率 <3笔/日直接不做, ≥5笔/日启动瘦身版 (半天); ③(§十新增) F2b 瘦身版并入**板块盘中急跌 veto 源** (pulse weakest3/≤-2% → veto 同板块 LONG, 字段零新建, 随②同生同死); ④(§十新增) **pulse 历史落盘** append jsonl (纯记录侧, 不挂 F1 规则, 批准即做)。另 §十实证: sector_pulse 名义 UE 消费方系时区死锁 (UE 09:10 ET 盘前跑 vs pulse 10:00 ET 首写, 当日守卫 100% 失败=死接线), 建议不修, 真归宿=③。批准后本案挂起等 F1 数据, 不占验证窗口。
3h. **已裁 O1 · 步骤1 已执行 · G3 观察期至 08-05**: `docs/debates/DEB-20260729-reanchor-knife-window.md` — 辩论方全线支持删除: E2 去重=7 独立事件 (NVDA 22 次实为 2 事件轮询重复), E1 反事实 6 可算 4 亏合计 -5.09% (MU -3.41 典型接飞刀), 埋点链路验证=真零实弹, T2 无意图证据 (双向救援设计落空半成品)。`entry_reanchor_enabled: false` 双处已关停+重启。Qoder 复核全复现 (§十, 含 MU 独立重算 -3.23%/-3.41% 双口径吻合)。**08-05 G3 漏斗对照通过 → 步骤3 代码退役**。
3i. ~~**辩论闭环, 待用户裁决 D1-D5 (用户命题"否则白干")**~~ → **✅ 已裁 2026-08-01: D1-D5 全批 (实为追认 — 全部已于 07-30 落地/实弹, 见变更日志首节勘误; 余 G4 08-07 矩阵净口径化+G5 报表三列按原排期)**: `docs/debates/DEB-20260730-fee-net-pnl.md` — 全库 PnL 系毛盈亏零扣费。**两轮口径修正后终版 (SIM-only 白名单, §九)**: 07-29 毛+49.8→净+19.3 (三方一致) / **07月 毛−91→净−815** (费724) / **全史 168笔 毛−1365→净−2124**; 假盈利14 / SL_BREAKEVEN 桶毛+89→净−48 (假保本14笔)。⚠️口径教训: 作者首版 515 笔混入已退休引擎 (MAIN_PIPELINE +23298), 辩论方揪出但其"191 笔"自身混入 SHADOW — **跨引擎拼接系口径事故#1 第三次重演, D3 前置定为正向白名单 `IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')` 而非黑名单**。**双方一致**: D1 净额SSOT / D2 fee_model+yaml费率表+三态 / D3 回填+08-07矩阵净口径重算 / D4 保本线费用感知 (挂矩阵, 实弹排 F1 验收后) / D5 门槛 record-only 且永不作入场硬拦。~~待用户: D1-D5 拍板 + TAF/SEC 对 SHORT 入场腿照收的终确认~~ → 已随 D1-D5 一揽子批准 (TAF/SEC 对 SHORT 入场腿照收, 按 §九 监管惯例)。**§十 动态源实测 (用户问"更全面动态")**: ①`order_fee_query` = broker 实收落地接口但 SIM 订单历史为空 (实测), LIVE 专属 ②`get_margin_ratio.short_fee_rate` SIM 即可动态拿 (SNDR 3.0%/年) → SHORT 借券费盲区可补 ③SEC $20.60/M 自 2026-04-04 (此前 $0) → 费率表须 effective_from 时间序列, 回填按成交日分段。
4. **周五盘后**: 老动作不变 — `scripts/exit_scorecard.py --since 2026-07-21` 看 R:R。另数 trigger_miss 新两门计数: `liquidity_gate` (07-30 盘前上线) + `borrow_pool_gate` (07-30 盘中上线, SHORT 专属) — 零计数合法 (现宇宙本不该拦), 有计数则逐笔复核拦截正确性。
5. **08-07 裁决点**: P0 shadow + 三门 go-forward 数据 → 阈值精调 / X1 降频是否启动。X2 若启动须满足**四前置**（新增第 4 条"SHORT 不隔夜"，依据主文档 §13.1，叙事已由 §14.2 修正）。
5a. **08-07 数据就绪审计 (Qoder, 07-29 夜, 用户令"先检查确认数据")** — 距裁决日 7 个交易日, 逐决定项盘点:
   - 🟢 **鲜活健康**: 路径湖 74 且 cron 04:35 日增 (→08-07 ~120+, dd 0.6/0.8+分层反事实主料); trigger_miss_telemetry (当日 22:38 仍写); trigger_gate_history 累计快照 (n=88/93 起, 逆向选择 gap 追踪); exit_param_shadow 114 行 (be/dd A/B 主料); scorecard 已平仓 512 (A1 切片 n≥100 可达)。
   - 🔴 **X1 降频不可评 (双重)**: ①P0.2 conf 校准前置未落地; ②高 conf 供给近两周枯竭。**〔08-01 勘误 — 原 bullet 数字全部证伪, 以下为 DB 复核值〕** 原称"近 14 日成交 108/108 全部 conf≥0.85"❌ → 实测成交侧 `signal_confidence` (0-1 量纲): 109 笔仅 **3 笔 ≥0.85 / 103 笔 <0.7**; 原称"指令分布双峰 319≥0.85/142<0.7"❌ → 指令侧 `confidence` 实为 **0-100 量纲** (旧统计按 0-1 读数致全失真), 实测近 14 日 191 条仅 **3 条 ≥85 / 185 条 <70**, 全史 410 条 = 135≥85 / 234<70 / 37 中间, 取值仅六档 {35,50,70,72,88,90}。原称"高 conf 桶近 14 日 pnl -$361"不可复现 → 现口径该桶 **n=3, net -$160.87** (07-19→08-01, 样本饿死无统计意义); 真正装满样本的是 <0.7 桶 (n=103, net -$802.76)。**供给枯竭**: 07-20 起日产 ≥85 指令 ≈0 (07-30:1 / 07-31:2 / 其余 0) — 与 7月上"32 笔 ≥0.85"形成 regime 级落差。结论修正: X1 不可评的根因 = 高 conf 供给枯竭 + P0.2 未落地 (桶退化结论仍成立); "高 conf 反转"叙事降级为 n=3 方向性观察。建议不变: 08-07 将 X1 移"只读"档并如实公布, 判据重设。
   - 🔴→🟢 **L2/L3 落点已确认 + 单位 bug 已修 (08-01)**: 真实落点 = `scripts/entry_gate_replay.py`→`data/entry_gate_replay_latest.json` (L2) + `scripts/historical_robustness_check.py`→`data/historical_robustness_latest.json` (L3)。两者系**离线回放工具** (非每日自动 shadow)，handoff 原误认 `challenger_model_ab.jsonl` 为落点。**修复前 L2 门网格全拦 (bug)**: DB 存 fraction (0.038=3.8%) 而门阈值用 percentage (2.5/3.0/4.0) → 所有指令被所有门拦截。已修 (`entry_gate_replay.py` load_instructions ×100)。**修复后重跑 (n=119, since 07-24) 结果反转**: 低 ATR 门 live_lo2.5/lo2.8/lo3.0 从历史"拦对"变**拦错** (拦掉组均% +0.12/+0.07/+0.05 > 放行组 -0.31/-0.37/-0.44)；高 ATR 门 hi4.0/lo3.0_hi4.0 从历史"拦错"变**拦对**。时点变体均值全负 (-0.08%~-0.21%)。L3 复跑结论不变 (3-4桶 vs 2.5-3桶 1/6 年占优 → 非结构性)。**08-07 含义**: ATR 门参数近期市况失效，精调须用修复后口径重评；L2/L3 已更新至 08-01。
   - 🟡 **challenger_model_ab.jsonl 07-24 停更 (16 行)**: 系**手动脚本** (`scripts/challenger_model_ab.py`)，非 crontab 自动流。handoff 步骤 2 "第 2 轮"未执行，非断流。DEB-20260724-challenger-model-ab 状态仍"待决策 (等第 2 轮)"。08-07 前如不补跑，该案自动落"显式延期"。
   - 🟡 **time_exit_counterfactual.jsonl 07-24 停更 (25 行)**: 脚本已归档 `scripts/archive/`，crontab 已注释。系 **intentional retirement**，非断流。三门 TIME_EXIT 反事实评估由 `exit_param_shadow.jsonl` (114 行, 鲜活) 替代。
   - 🟡 **MFE_STALL 仅 6 例** (规则 07-24 上线) → 08-07 ~15 例, 趋势级可读不可定论, 落"只读"档。
   - 🟡 **vol_bucket 分层 dd**: 路径湖分桶后每桶 n<30, 08-07 只能给方向不给参数。
   - 行动项: ①✅ L2/L3 落点确认+单位 bug 修复+重跑 (08-01 完成); ②✅ watchdog 未盯 challenger/time_exit_cf — 两者均非每日自动流，无需纳入; ③X1 反转证据纳入 08-07 决策矩阵只读行 (待 08-07 矩阵编制时落)。

---

## 📝 变更日志 (最新在上)

### 2026-08-01 (数据勘误+消误导·Qoder, 用户问"63.9%正确如否 再消除误导") · 弃权桶 Simpson 翻案 + 指令推送图标语义化
**勘误 (我此前结论被公平口径推翻)**: "弃权桶 T+1 命中 63.9% (n=119) 全场最优" 算术正确但**口径误导** — 拆分后 已执行 45.5% (n=66, 均方向收益 −0.58%, pnl −$524) / 未执行 86.8% (n=53)。未执行 BUY 高分是限价单逆选择结构 (价格跑远=纸面对单), 非弃权特有 (各桶未执行均 63-100%)。**成交口径下 debate_outcome 无区分度** (弃权 45.5% / 无标记 45.2% / 分歧 44.4%); 全场"58.3% vs 抛硬币"的边缘**全部来自未成交那一半**, 已执行整体仅 ~45%。同时澄清: 满屏 conf=35 的真凶 = 弃权中性 0.50 × signal_gate WARN −0.15 (skill pipeline.py:507), 弃权罚分 07-18 已修不在产。
**消误导落地**: ①feishu_push 指令推送图标从 conf 阈值断言 (≥70✅/≥50⚠️/<50❌, 伪校准) 改辩论语义标签 (✅一致/➖弃权/⚠️分歧/🎚非辩论源), conf 数值保留 (仓位加权事实); ②direction_hit_scorecard debate_outcome 四切片补"已执行·"孪生行, 两口径永久并排防再误读。
**遗留裁决项 (未动)**: 已执行·弃权桶期望为负 (n=66) vs 砍之则指令流 −39% 且 G3 攒样本受损 — 交易行为变更, 待用户裁。pytest 1009 全过, 服务已重启。

### 2026-08-01 (优先级批次·Qoder, 用户裁"按照建议优先级执行") · P0.2 校准标尺 + challenger A/B 终裁 + P-1/P-2 名单订阅重构
**P0.2 conf 校准 (修尺子)**: direction_hit_scorecard 增逐 conf 值校准表+预注册分桶裁决 — 读数 **inverted 倒挂** (分歧≤40: 62.5% vs 高确认>75: 52.3%), conf 不能用于门控 (X1 前置未达成, 如实落盘); daily_invariant_check 增 I9 双侧量纲守卫 (trade 侧 [0,1] / 指令侧 [0,100], 47 笔全过)。
**challenger A/B 第 2 轮 + 终裁 (DEB-20260724-challenger-model-ab §六)**: 07-31 正常日 n=15 补跑, 两日合并 n=31 — glm-5.2 机械判据全过 (成功 31/31, 延迟 12.4s 快 29%) 但**镜像退化坐实** (31/31 全 DISAGREE 零粒度 vs deepseek 2×PARTIAL), 反向供给无优势 (4:5), 成本 3×。**终裁: 不切, 维持 deepseek-v4-pro**; conf 合成链路校准前置未达成, 换模型=换秤砣不修秤。
**P-1 名单合一 (DEB-20260724-da-chain-audit §7, 逾期项清零)**: watchlist 块升为唯一真源 — 57 条目注入 roles 标签 (benchmark=SPY/QQQ / liquidity_whitelist=53 / topup_eligible=24 与原静态池对账一致 / subscribe=46); SMCI/PLTR/MRVL/SNOW/SHOP/DELL/CRWD 7 只 topup 专属迁入 (enabled:false 不进扫描池); 静态 `playbook.topup.pool` 退役, pipeline 消费端改 `get_symbols_by_role("topup_eligible")` 派生。**防御股未进 topup_eligible** (§7.2 前置发现执行)。
**P-2b 订阅动态化**: `_get_subscription_codes` 重写 = 基准 ∪ 当日有效指令标的 ∪ playbook 未平仓 ∪ 主线 PositionStore 安全网; 静态 watchlist 降可配置 (默认关); 回滚 `subscription.dynamic_enabled=false` 走保留 legacy 路径。LV3 额度对账: 动态峰值 ≤55 只 ≪ 原静态 44+ 规模, 历史无额度错误。周末实测应订集合=基准 2 只 (正确)。**周一盘中验收点: 指令标的 60s 内进订阅 / vol_gate·vwap 数据缺失率下降 / 无隐性消费者告警**。
**验证**: pytest 1009 全过 (+9 新回归锁派生对账与回滚路径), 服务重启 0 ERROR, YAML 生产加载器验证通过。

### 2026-08-01 (全链路体检批次·Qoder, 用户裁"整体完整的做好检查和配置以及验证") · 四路并行审计 → 8 项修复落地
**审计方式**: 调度健康/数据链路/死代码残留/验收欠账 四路并行 + 尺子层自检 (不变量 47 笔全过, IC 回填 98.1%)。
**修复落地** (全部修尺子/做减法类, pytest 1000 全过, 服务已重启生效):
1. **health_check.sh Dashboard 探针假阴性 (P0)**: 裸 `ss` 在 cron PATH 不存在 (/usr/sbin/ss) → 07-31 起每 30 分钟假告警 (今日 68 条)。改绝对路径, 模拟 cron PATH 复跑 ✅。
2. **symbol_performance_report 周报结构性失效 (P1)**: 三重失效 — ①周一 08:00 CST 跑, market_guard 目标日=周日恒非交易日静默退出 (days_back 周一改 3); ②更早三周 `No module named 'scripts'` (sys.path 顶部注入); ③口径化石: source_type 还在滤 MAIN_PIPELINE/ORB_ENGINE → 改 D3 白名单 + D1 净口径。实跑产出真实读数 (14 天 58 笔净 -$380, 保留 2/观察 10)。
3. **7 月月报结构性漏跑 (P1)**: `_check_monthly_review` 要求 day==1 但 WEEKEND 分支先 continue — 1 日落周末/假日则整月永触不到。月报调用上移到静默分支之前 (只读库不需行情) + 哨兵文件幂等 (原内存标记重启即丢, 窗口内重启会重复推送)。手动验证 2026-07 月报可生成 (186 笔, 胜率 51.6%)。
4. **复盘三处僵尸读数 (P1)**: ①review_orchestrator 决策漏斗 decisions.db 恒 0 → 改 playbook_instructions (含 _build_decision_funnel 重写, conf 按 0-100 量纲分桶); ②review_loop `voting_data_missing` 规则每日误报指向死管线 → 注销 (实现留墓碑); ③daily/weekly 拒绝分析读 rejections.db 停写 33 天恒 0 → 退役标注指向漏斗"未执行"。
5. **僵尸开关×5 断线 (P2 做减法)**: yaml `diagnostics.schedule.post_market` (runner 零调用+statistical_validator 悬空) / `risk.pre_trade.battle_plan_bonus_enabled` (零代码读取) / `strategies.voting_llm` / `strategy_registry.voting_llm` / `strategy_registry.BATTLE_PLAN_FAST_PATH` (均仅门死路径消费) → false + ZOMBIE 注记。
6. **五因子计算断线 (P2 做减法)**: daily_review Phase6 每日全量加载 30 天 trade_records 算无效读数 → 停用返回退役标记; dashboard 两读点 (analytics.py/trade.py) 加 retired 水印 (历史快照)。
7. **陈旧 battle plan 喂 LLM 断线 (P2)**: EOD 复盘注入 (scheduler_triggers) + dashboard challenge_thesis bear_cases (无 stale 检查) + main.py 启动加载块 → 全部断线留墓碑。
8. **G4 存活判据落盘 (P0 验收)**: 终裁 G4 (回撤<5% + 连亏<5笔) 原**无任何判分产物**, 08-29 将无据宣判。exit_scorecard 增 g4_survival 块: 回撤用 capital_state 权益曲线峰谷自算 (drawdown_pct 列实测全 ≤0 分数不可信) + 连亏峰值 (exit_time 序, 净口径) → 当前读数: 回撤 0.29% ✅ / 连亏 7 笔 ❌ → FAIL (since 07-24)。
9. **测试污染生产修复 (P2)**: test_push_hub 全通道失败用例把 t/b 写进生产 `data/push_failures.jsonl` → autouse fixture 重定向 tmp_path, 复跑零污染。
**审计发现但未动 (留裁决/排队)**:
- **验收硬伤 (08-29)**: G1 按框架零容错条款, W29 已负 → **8 月 GO 数学上已不可能**, 终裁实为 PIVOT vs STOP; G2 PF=0.81; G3 55/150 (在途); G5 13.0% ✅ 唯一达标。三赛道仅"更长持有期"有望但隔夜四前置+T+5 口径+基线切分未补齐。
- **已批准未执行**: ~~P0.2 conf 校准 / P-1 名单合一+P-2 订阅动态化 / challenger A/B 第 2 轮~~ → 当日优先级批次已全部清零 (见上条); 余: thinking D 前置 / G5 报表三列。
- **数据缺口 (不修因属新采集/交易路径)**: trade_attribution 执行质量字段从未填充 (真实滑点在 metadata_json); fees 100% est 无实收; 报告族推送单通道无 fallback (建议切 PushHub); 历史闭环硬缺口 12 处 (07-21 前, 本周 47/47 干净); order_executor decisions.db 死写+post_trade_hook 哑弹 ImportError (交易路径, 未动); check_position_reconcile 死注册; hard_to_borrow/short_interest 无生产者 (已知挂账 93 天)。
- **方向策略体检结论** (答用户"vs 抛硬币"): 整体 T+1 命中 58.3% (n=307, z≈2.9 显著), 但 conf 阶梯倒挂 (高确认 52.3% vs 分歧 62.5%) — 边缘来自事件质量 (rubric≥3 68.2%, critique DOWN/DROP 71.4%) 不来自 confidence。

### 2026-08-01 (推送兜底·Qoder, 用户裁"4可做") · 日/周/月复盘推送切 PushHub 双通道
报告族推送原走 `scheduler.notifier.send_text` — 单通道钉钉, 无重试/无 fallback/无失败落盘, webhook 故障=报告静默丢失 (全系统"每天最重要的一条消息"反成兜底最弱环)。新增 `_push_review_text` (scheduler_triggers.py): PushMessage(title=首行, body=其余) 经 PushHub 钉钉→飞书→FlagFile 逐级 fallback + 全失败落 push_failures.jsonl; hub 自身异常退化旧路径。日/周/月三调用点已切。端到端实测主通道成功 ✅, pytest 1000 全过, 服务已重启。

### 2026-08-01 (复盘链路修复批次·Qoder, 用户裁"按照建议修复") · 日报/周报五组修复 — 渲染器致命 bug + D1 净口径推送 + 两死源切换 + 五因子退役标注
**背景**: 核对 07-31 日报发现 1 致命 + 4 类失真 (见上一"核对"条目)。
**① 渲染器变量覆盖 (致命)**: `_format_daily_review` 循环变量 `pnl` 遮蔽函数级 `pnl` — 头部"总盈亏"实为最后一笔逐笔盈亏 (07-31 报 -$0.48 系 ORCL 单笔, 真实毛 -$140.06)。两循环变量改名 `t_pnl`, 注释留档。
**② 净口径三列推送 (D1 对齐)**: daily+weekly `_generate_summary` 增 `pnl_basis`/`fees_total`/`total_pnl_net`/`win_rate_net`/`fake_wins`; 渲染头部改 `胜率 毛(净, 假盈利n笔) / 总盈亏 毛|费|净 / 平均每笔 毛|净`, header emoji 由净驱动, 旧 JSON 回退毛。周报顺带补上**从未有过**的 source_type 白名单 (D3)。
**③ 漏斗数据源**: decisions.db (07-12 停写, 日报"决策数 0"假象根因) → `playbook_instructions GROUP BY direction, executed`, 带 `source` 标记, 标签随源 ("指令数/周指令数")。
**④ 计划基准**: `_get_plan_vs_actual` 从已退役 battle plan → 当日 playbook 指令集, 输出 已执行|未成交|计划外|执行率; **计划外>0=链路异常** 守卫 (07-31 实测 0 ✓)。
**⑤ 五因子块退役标注 (做减法)**: daily 推送渲染为"⚠️ 基线已退役, 读数无效 — 模块待 08-07 后下线" (JSON 保留); 三处结构性死分支 (else 内含 hold<lo/hold>hi 恒不可达 → early/late 恒 $0) 选择退役而非修复。
**附带**: 6维度块 `data_available=False` 原渲染 ❌ → 改 N/A (执行质量依赖未填充的 trade_attribution, 系"无数据"非"失败")。
**验证**: 07-31 日报重生成 — 总盈亏 毛$-140.06|费$49.92|净$-189.98 ✓ / 胜率 58.3%(净33.3%,假盈利3) ✓ / 指令数23条(BUY:10,未执行8,SELL_SHORT:2,未执行3) ✓ / 计划执行 12|11|0|52% ✓ / 周报头部 净-109.03, 假盈利7 ✓。pytest 1000 passed (test_daily_review 夹具 source_type 对齐白名单)。

### 2026-08-01 (尺子批次②·Qoder) · P0 裁决口径预注册 + P1 三把尺子 + P2 复验 (用户裁"按建议执行")
**P0-① 终裁 G1/G2 净口径预注册**: `august-verdict-framework-20260831.md` G1/G2 补〔净口径〕脚注 + 变更记录条目 (按防挪门柱条款 §三.1) — 冻结文本原未声明毛/净, 08-07/08-29 裁决若按毛读 G1 将得虚假"正期望"信号 (实测毛+$1.9→净−$3.03/笔)。性质=口径澄清非阈值修改。
**P0-② §5a X1 数字勘误** (见 §5a 原位勘误): 原"成交 108/108 conf≥0.85"❌ (实测 3/109, 量纲 0-1); 原"指令双峰 319/142"❌ (`confidence` 列实为 **0-100 量纲**, 旧统计按 0-1 读全失真; 实测近14日 3/191≥85, 全史 135/410≥85); 原"高 conf 桶 -$361"不可复现 (现口径 n=3 net -$160.87)。**新发现: 高 conf 供给 07-20 起枯竭** (日产≥85 指令≈0) — X1 不可评根因从"桶退化"精确化为"供给枯竭+P0.2 未落地"。
**P0-③ exit_scorecard `pnl_basis` 戳记**: 无条件写入 JSON 输出 (history.jsonl 新旧行口径可分辨, 防跨窗口拼接事故第四课)。
**P1a daily_invariant_check 两修**: I1 价格/数量 ≤0 原静默跳过 → 单独告警 `I1_price_qty_invalid` (近7日 47 笔零异常, 零噪音); I6 bar 峰物理检查补 SHORT 镜像 (`peak_price_bar`=min(lows) 按 pipeline.py:4933, ≤min(e,x)×1.005, 现库 7 笔 SHORT 有 bar 样本可守)。
**P1b ic_health_check 对齐**: Pearson→**Spearman** (与 signal_ic_tracker/expectancy_ic_backtest 同口径, 原两尺 IC 值不可比); 最小样本 3→5 (n=3 的 Pearson |r|≈1 系假象); 修 falsy bug (IC=0.0 原误报"样本不足")。实跑: LONG +0.106 / SHORT -0.019 / signed +0.080 (n=544/135)。
**P1c exit_scorecard 补 Profit Factor**: G2 裁决线 ≥1.3 直接读数 (原需手算)。**当前读数 (since 07-24, n=55, 净): PF=0.81 — G2 同样不达标**。另加 n<30 小样本警告行。
**P2 复验结论**: 见 P0-②; 附带口径教训 — **同一库内 confidence 两量纲并存** (指令 0-100 / 成交 0-1), 已列入 P0.2 校准案的必须统一项。
**验证**: exit_scorecard 实跑 PF=0.81 ✓ / invariant 编译+实跑 (I3×1 见下) ✓ / ic_health 实跑 ✓ / pytest -k "scorecard or invariant or ic_" 13 passed ✓。
**I3 容差修复 (用户裁"修")**: I3 (quote 版 peak 语义) 容差 1e-6 → **0.5% 对齐 I6** — id=1189 PDD exit 88.37 vs quote-peak 88.36 差 1 tick (bar 版 88.4 证实物理合法, quote 采样时滞) 属误报。修后复跑 47 笔全过 ✓, 今晚 cron 起零噪音。真数据腐败 (错位$级) 仍远超 0.5% 必报警。
**列账闭环**: 08-01 审计"未修列账"中 ic_health Pearson (105行) + invariant I6 SHORT (107行) 本批已修; trigger_gate source_type (103行) 前批已修; 余 exit_replay bars_cache autype 标记 (104行) 待办。

### 2026-08-01 (尺子审计·Qoder) · 全面精度审计 → 3 项致命/高危修复 +  net 期望值翻负 (重大发现)
**审计范围**: exit_scorecard / direction_hit_scorecard / trigger_gate_report / exit_replay_backtest / atr_gate_shadow / daily_invariant_check / ic_health_check / backfill_fee_net / expectancy_ic_backtest 共 9 把尺子，四路并行 adversarial 审计。
**修复① (致命) direction_hit_scorecard.py SHORT 命中率反转**: `fwd_return_t1/t3` 写入时已方向调整 (`ic_tracker.py: sign*(close-entry)/entry`，正=方向对)，但 SQL 又 `CASE direction` 二次调整 → SHORT 命中率测的是亏损率。修法: 删二次 CASE，直接 `fwd_return_t1>0` 判命中。修复后 SHORT n=14 命中 71.4%/方向收益 +4.39% (原被反转)。36 passed。
**修复② (高危) exit_scorecard.py 切 net PnL**: 原用毛 `realized_pnl`，违反 DEB-20260730 D1 (用户 08-01 裁"pnl_net 为唯一对外口径")。改 `COALESCE(pnl_net, realized_pnl)`。
**修复③ (高危) exit_scorecard.py 基线警告**: 原默认 30 天窗口混入 07-24 三门上线前旧退出参数。加 stderr 警告: since<2026-07-24 时提示跨 regime。
**🔴 重大发现 — net 期望值翻负**: 修复②后重跑 `--since 2026-07-24`:
- **毛口径 (修复前)**: n=55, 胜率 69.1%, R:R=0.51, 期望 **+$1.9/笔**, 总盈亏 **+$104.63**
- **净口径 (修复后)**: n=55, 胜率 54.5%, R:R=0.67, 期望 **-$3.03/笔**, 总盈亏 **-$166.6**
- 费用把 14 笔假盈利翻亏 (胜率 69%→54%)，avg亏从 -$45.66 收窄至 -$35.01 (费也减亏)，但期望值整体翻负。
- **08-07 含义**: G1 判据"每笔期望 ≥$0 连续 4 周"在净口径下**不达标**。此前 gross 口径给出的"正期望"信号系费用盲区所致。08-07 裁决矩阵必须以 net 为准。
**其他审计结论 (未修，列账)**:
- `trigger_gate_report.py` 无 source_type 过滤 (SHADOW/SIM/LIVE 混算) — 高危，08-07 后修
- `exit_replay_backtest.py` bar 缓存无 autype 版本标记 (旧 QFQ 静默复用) — 中危，`--refresh-bars` 可规避
- `ic_health_check.py` 用 Pearson vs `expectancy_ic_backtest.py` 用 Spearman，IC 值不可比 — 中危
- `direction_hit_scorecard.py` 报告 n 为 T+1 样本，T+3 有效 n 不可见 — 中危
- `daily_invariant_check.py` I6 只查 LONG 不查 SHORT — 低危
- `atr_gate_shadow.py` / `exit_replay_backtest.py` 公式/方向/双序区间/Δbaseline 全部正确 ✓
- `backfill_fee_net.py` 费率模型/SHORT 腿/SEC effective_from/幂等性全部正确 ✓
**验证**: direction_hit 36 passed + exit_scorecard 基线警告触发正常 + net 口径实跑自洽。
**trigger_gate_report.py 基线警告**: 同 exit_scorecard 加 since<2026-07-24 stderr 警告。注: playbook_instructions 无 source_type 列，实测 executed=1 的 win/loss 与 SIM/LIVE 一一对应，无 SHADOW 混入。

**🔬 net 翻负深度解剖 (07-24 以来 n=55)**:

*按来源*:
| 来源 | n | 净总额 | 净均笔 | 毛总额 | 费总额 |
|---|---|---|---|---|---|
| UE | 17 | **+$91.05** | +$5.36 | +$202.97 | $111.92 |
| DA | 34 | -$95.95 | -$2.82 | +$46.88 | $142.83 |
| topup | 4 | -$161.70 | -$40.42 | -$145.22 | $16.48 |

*按方向*:
| 方向 | n | 净总额 | 净均笔 | 毛总额 | 费总额 |
|---|---|---|---|---|---|
| SELL_SHORT | 9 | **+$106.90** | +$11.88 | +$166.09 | $59.19 |
| BUY | 46 | -$273.50 | -$5.95 | -$61.46 | $212.04 |

*来源×方向交叉 (n≥3)*:
| 切片 | n | 净均笔 | 净总额 |
|---|---|---|---|
| UE·SHORT | 7 | **+$24.21** | +$169.49 |
| DA·BUY | 34 | -$2.82 | -$95.95 |
| UE·BUY | 10 | -$7.84 | -$78.44 |

*按 exit_reason*:
| 出场原因 | n | 净总额 | 净均笔 | 毛总额 | 费总额 |
|---|---|---|---|---|---|
| MFE_STALL_EXIT | 17 | **-$746.33** | -$43.90 | -$674.91 | $71.42 |
| SL_INITIAL | 1 | -$104.77 | -$104.77 | -$100.64 | $4.13 |
| SL_BREAKEVEN | 12 | -$12.49 | -$1.04 | +$56.86 | $69.35 |
| PRE_CLOSE_EXIT | 5 | +$151.96 | +$30.39 | +$173.15 | $21.19 |
| SL_TRAIL_LOCK | 20 | **+$545.03** | +$27.25 | +$650.17 | $105.14 |

*按周*:
| 周 | n | 净总额 | 净均笔 | 毛总额 | 费总额 |
|---|---|---|---|---|---|
| W29 (07-24~27) | 8 | -$57.57 | -$7.20 | -$24.49 | $33.08 |
| W30 (07-28~31) | 47 | -$109.03 | -$2.32 | **+$129.12** | $238.15 |

**结构诊断**:
1. **MFE_STALL 是最大出血点**: 17 笔净亏 -$746 (占全部净亏 4.5 倍)。零 MFE 即损 = 入场质量问题，非退出问题。
2. **SL_BREAKEVEN 是费用陷阱**: 12 笔毛正 +$56.86 但费 $69.35 → 净 -$12.49。7/12 是假盈利 (gross>0 net≤0)。D4 费用感知保本 (07-30 已实弹) 应缓解。
3. **BUY 侧系统性负期望**: 46 笔净 -$273.50，费 $212 (占毛损 3.5 倍)。DA·BUY 是最大负贡献 (n=34, -$95.95)。
4. **UE·SHORT 是唯一正期望切片**: n=7, +$24.21/笔。样本小但信号强 (方向命中 71.4%, 修复后)。
5. **费用结构**: avg $4.93/笔 (min $4.03, max $24.45)，占 avg |gross PnL| ($30.13) 的 16%。W30 费 $238 > 毛利 $129 → 净负。
6. **W30 毛正净负**: 47 笔毛 +$129.12 但费 $238.15 → 净 -$109.03。交易频率 × 固定费 = 规模不经济。

**08-07 裁决含义**:
- **G1 (期望≥$0 连续 4 周) 净口径不达标**: W29 -$7.20/笔, W30 -$2.32/笔。
- **赛道信号**: UE·SHORT 正期望但 n=7 不足 30 门槛；DA·BUY 负期望是主拖累。
- **处方方向**: ①降频减费 (W30 日均 12 笔, 费 $238) ②MFE_STALL 入场质量门 (非退出调参) ③BUY 侧门槛上调或 conf 门 ④UE·SHORT 扩样本验证。
- **08-07 矩阵必须以 net 为准**, gross 口径已失效。

### 2026-08-01 (数据侧·Qoder) · 5a 数据就绪审计行动项①②闭环 — L2/L3 落点确认 + ATR 单位 bug 修复 + 重跑
**背景**: handoff 5a (07-29 夜) 行动项①"另一模型确认 L2/L3 落点+修断流" + ②"watchdog 是否盯 challenger/time_exit_cf"。
**L2/L3 真实落点**: `scripts/entry_gate_replay.py`→`data/entry_gate_replay_latest.json` (L2 入场门/时点回放) + `scripts/historical_robustness_check.py`→`data/historical_robustness_latest.json` (L3 跨期稳健性)。两者系**离线回放工具** (按需重跑，非每日自动 shadow)。handoff 原误认 `data/shadow/challenger_model_ab.jsonl` 为 L2/L3 落点 — 实为手动 A/B 脚本 (`scripts/challenger_model_ab.py`)，非 crontab 自动流。
**ATR 单位 bug (修尺子)**: `entry_gate_replay.py` 从 DB 读 `atr_pct` 为 fraction (0.038=3.8%)，但门阈值用 percentage (2.5/3.0/4.0) → 修复前所有 119 指令被所有门拦截 (门网格全同，显然异常)。根因 = DB 写入侧 (`pipeline.py:1916` `atr_provider.get_atr_pct` 返回 atr/price fraction) 与回放脚本门阈值单位不一致；回算补齐源 (`atr14_pct_series`) 返回 percentage，故 07-26 首跑 (212/308 回算) 结果正常，07-24 后全原生 fraction 即触发 bug。修法: `load_instructions` 读 DB 值 ×100 统一为 percentage。13 passed。
**修复后 L2 重跑 (n=119, since 07-24)**: 门网格结果**反转** — 低 ATR 门 live_lo2.5/lo2.8/lo3.0 从 07-26 历史"拦对"变**拦错** (拦掉组均% +0.12/+0.07/+0.05，放行组 -0.31/-0.37/-0.44)；高 ATR 门 hi4.0/lo3.0_hi4.0 从历史"拦错"变**拦对**。时点变体均值全负 (-0.08%~-0.21%，07-26 首跑全正)。**08-07 含义**: ATR 门参数系近期市况+本信号源经验参数 (DEB-20260726 已预注册降级)，近期反转进一步证实不得引用为结构规律；精调须用修复后口径重评。
**L3 重跑 (78 标的 × 5 年)**: 结论不变 — 3-4桶 vs 2.5-3桶 1/6 年占优 → ❌ 非结构性。
**challenger/time_exit_cf 状态**: ①`challenger_model_ab.jsonl` 07-24 停更 = 手动脚本第 2 轮未跑 (handoff 步骤 2 待办)，非断流；08-07 前不补跑则该案自动"显式延期"。②`time_exit_counterfactual.jsonl` 07-24 停更 = 脚本已归档 `scripts/archive/` + crontab 已注释，系 intentional retirement；TIME_EXIT 反事实评估由 `exit_param_shadow.jsonl` (114 行, 鲜活) 替代。
**watchdog 覆盖**: `data_freshness_watchdog.py` WATCHLIST 未含 challenger/time_exit_cf — 两者均非每日自动流，无需纳入 (行动项②闭环)。
**落盘**: `data/entry_gate_replay_latest.json` (08-01T10:22) + `data/historical_robustness_latest.json` (08-01T10:23)。

### 2026-08-01 (执行·Qoder, 用户裁"请执行") · 记录侧两件落地 (ecfaaca1) + LLM 两件勘正: 07-25 早已落地, 另一模型"零命中"结论有误
**勘正 (先取证后动口)**: 另一模型 0662078c 称"llm-cache B / failure-semantics B 三件套确认未实施 (git 全史零命中)" — **两条均不成立**, 两案 2026-07-25 已裁决已执行 (DEB §八/§五状态行本就有记录):
- ①缓存方案B: yaml 6 处 `cache_on_fail: false` 带 DEB 注释 (:436-527 实测) — 方案B 的 yaml 变体即批准形态 ("cache_on_fail 全置 false 或删分支"二选一), 失败提示附带条件当日已验 (deep_analysis_engine.py:489-494 显式 WARNING)。
- ②失败语义三件套: B① finish_reason 全链 (model_call.py:47/:185/:249/:636) + B② 双信号降档 (:419-428, flag retry_downshift_enabled) + B③ hermes 质量门 (hermes_worker.py:275/:291/:300/:330) — 代码锚点全部实测在产。
- 教训 (双边): 验证"未实施"须查代码锚点而非仅 git 提交信息; 我复核方此前亦未回读 DEB 状态行即采信"待实施", 同记一过。
**新执行 (ecfaaca1, 纯记录侧零交易影响)**:
1. **pulse 历史落盘** (topup-veto DEB ④): sector_pulse.py 原子写后 append `data/sector_pulse_history.jsonl` (全量 12 板块读数), 失败只告警不污染主文件 — ③板块 veto 阈值设计的攒料前提。实测 exit=0 + 首行已落 (注: 手动验证跑在收盘后, pulse.json 被覆写为盘后零值读数, 周一 10:00 ET cron 自动刷新, 无实际影响)。
2. **token 报表截断率列** (failure DEB Q4 验收尾款): daily_llm_token_report.py 按任务日截断率 + 近7日截断率段 (预注册判据 >5% 且 n≥10 → 🔴 复核 max_tokens)。内存库合成冒烟: 截断计数/n≥10 下限滤除/渲染标记全过; 真库 07-31 干跑正常 (当日零截断, 新段正确隐藏)。
**验证**: 1000 passed (36s)。**至此五案裁决全部闭环**: topup veto ①②③按保险丝等 F1 数据 (挂起) / fee D1-D5 在产 (追认) / LLM 两件 07-25 已在产 / thinking 终裁后再议 / pulse 落盘+截断率列今日补齐。

### 2026-08-01 (用户裁决·Qoder 落档) · 四项 DEB 一次性拍板 — topup veto / 费用净额 D1-D5 / LLM 两件 / thinking 时机
**裁决方式**: Qoder 逐案通俗解释"是什么/跟钱什么关系/代价"后用户四连拍, 全部按推荐通过:
1. **DEB-20260729-topup-event-veto-shadow → 批准全部 4 点** (F2a 口径成文 + F2b 速率保险丝缓办规则 + 板块急跌 veto 源 + pulse 历史落盘)。直打 07-31 最大出血点 (topup 3/3 全执行全落亏)。自带保险丝: F1 三日速率 <3笔/日不启用。
2. **DEB-20260730-fee-net-pnl → 全批 D1-D5**。⚠️**勘误 (另一模型 0662078c 提出, Qoder 复核属实)**: 本裁决实为**对既成事实的追认**, 非新增待办 — D1(A)/D2/D3/D5 已于 07-30 盘前全落地 (fee_model.py 18测 + 回填515行 + go-forward平仓钩子 + I8不变量, 见 DEB §十一); D4 保本线费用感知已于 07-30 用户裁"直接实现"**提前实弹** (15c6a451, breakeven_fee_aware_enabled=true 在产两日, exit 版本已轮转 exit-ed3625929072, 见 §十二)。动机记录不变: 7月毛-91→净-815, 费用是账面亏损 8 倍, 14 笔假保本。
3. **DEB-20260725 两件打包批准**: ①llm-cache-cross-symbol-pollution → 方案 B (缓存跨标的串号修复, 附失败提示验证条件); ②llm-failure-semantics → B 三件套 (重试分流 flag + 双信号触发 + 比例制降幅修正), 系 thinking 灰度硬前置且独立价值成立。
4. **DEB-20260725-llm-thinking-mode-tradeoff → 终裁后再议** (采纳复核方立场): 08-29 终裁前不开深度思考灰度, 保护 8 月样本单一脑子可归因; D 类前置可先做。
**执行纪律**: 费用批 D1-D5 已全部在产 (追认, 无新增待办, 仅 G4 08-07 矩阵净口径化+G5 报表三列为原排期); 真正待实施 = **LLM 两件 (缓存方案B + 失败语义三件套, 另一模型确认 git 全史零命中)** + topup veto 记录侧 (④pulse 落盘可随时做, ①②③按自带保险丝等 F1 数据)。实施排期交另一模型, Qoder 复核。

### 2026-07-31 (收盘复盘·Qoder) · 净 -$189.98 三连阳终结 + 反馈环首夜实弹产出 (lessons×7 / us.json H1×12)
**总账**: 12 笔全平 (4W/8L), 毛 -$140.06, 费用 ~$49.92, 净 **-$189.98** — 终结 +145.8/+19.3/+94.8 三连阳。03:52 position_sync `Futu:0 | 主线DB:0 | playbook:0 | ✓` 全平无残留。
**亏损结构**: 三笔"零 MFE 即损" (SHOP SL_INITIAL -104.77 · AVGO MFE_STALL -89.84 · GOOG空 MFE_STALL -61.76, MFE≈0/MAE 3.5~4.9) 合计 -$256.4 > 全天净亏 → 入场质量问题非持仓管理问题。纪律面: MFE_STALL ×4 小亏区间切除, 无 07-23 式拖延。BREAKEVEN ×3 各付 ~$4 费 (净拖累 -$8.34) = D4 费用感知保本 (07-30 已实弹) 首批实弹样本 — 含费 cushion 下仍净负 $4 级, 估计费与实际费的 gap 待 G4 净口径化时量化。
**供给归因**: DA 16→执行9 · UE 4→执行0 · **TFB/topup 3 (GOOG/NVDA/SHOP) 全执行且全部落入当日亏损/刮擦名单** — 连续两日负期望, 08-07 后候选: topup 独立 scorecard 或执行门槛上调 (trigger_gate scorecard 08-01 06:20 首跑供数)。11 指令 no_trigger。
**反馈环首夜兑现 (04:32-04:37)**: `教训提取: 7 候选, 7 写入` (_extract_lessons arbiter_conclusion 修复首次生产) + `us.json 记忆已更新 (H1: 12 笔归因)` (⑦接线首火) — 归因→记忆→教训全链路首次完整跑通, lessons 缓刑观察期 (~08-14) 自此有真实样本。减分: 日度验证 0/0 claim 有证据 (可信度 0% weak) — 验证供给问题, 观察中。
**遗留**: ①anchor_dev 8/16 疑为另一模型 UTC 13:36 临时回填 (bridge 仍 0/16), skill 发射缺口未修 — 明日 opportunity_scan 无字段则补丁锚点上移至 scan 发射路径。②明早 06:20 三张 scorecard 首次自动化运行。③anchor_dev DA 映射修复待下次 main.py 重启生效。

### 2026-08-01 (凌晨盘中·Qoder) · 盘中现场检查 + F1 anchor_dev DA 最后一英里补齐 (用户裁"根据建议执行")
**现场检查结论**: 链路正常合理 — 批3 重启后 NewsV2/interceptor 退役日志实弹 · auto_recovery 新司机 position_sync 循环正常 (Futu:5=playbook:5 一致性✓) · 供给 23 条 · auto_execute 5min 节奏稳 · kill switch 直挂门无误触 · guardrail 活 (已开 $34.8k/12笔) · TSM/ORCL 真成交+真均价回填+钉钉推送 · 出场链 7 笔已平 (净约-$255, MFE_STALL×2/BREAKEVEN×3/TRAIL_LOCK/SL_INITIAL)。AMZN 极端 gap 门 ×11 正确拦截 (entry $236 vs 市价 $271 — F1 幻影被拦的活证据)。
**执行 (record-only, F1 收尾)**: DA 链 anchor_dev_pct 双断点之仓内半修复 — ①`BridgeVotingAdapter.vote` 透传 cand 字段 ②`Instruction` dataclass 加 `anchor_dev_pct: Optional[float]=None` ③成功路径构建点透传 vote_result ④`_instruction_to_db_dict` 加落库顶层键。冒烟 4/4 (vote透传/缺省None/db_dict/缺省None) + DB 落库 2/2 (带值持久化/无键NULL不炸) + 定向 44 passed。**下次重启生效** (生成链盘前跑, 不盘中重启)。
**遗留① (交另一模型/明日验证)**: DA skill 侧断点 — 今日 opportunity_scan 16 候选 0 只带 anchor_dev_pct, skill 补丁插入点疑似不在 opportunity_scan 产出路径 (或补丁在今晨 18:07 扫描后才应用)。明日文件若仍无字段, 需把补丁锚点移到 opportunity_scan 候选发射路径。
**遗留② (挂账 08-07 批)**: regime 门 MA 数据失败鲁棒性 — AlphaVantage SPY 日线间歇失败致今日 44/53 次评估 MA20/MA50=0.00 参与评分 (交叉组件 +2→-2 静默扭曲; `spy_trend_at_generation` 元数据因 0>=0 错标 above_ma20)。今日凑巧 score=2 判定不变 (MA 有效时同分)。建议仿 vix_uncertain 加 ma_uncertain 跳过组件 — 代码逻辑修复非调参, 已列入 08-07 后批次。

### 2026-07-31 (夜·Qoder 复核) · 另一模型三提交复查 + ⑨死亡名单翻案重核终裁 (DEB 第5轮) + 墓碑按终裁执行
**三提交复查结论 (全过, 两处提醒)**:
- `d9c49739` 批1+批2 (另一模型实施并代提交我的批2): 双态实测记录完整 (kill switch 激活/清除两态 · veto 哑弹坐实 · regime 缓存命中 0.000s · NewsV2 退役日志实弹 · 998 passed+重启冒烟 7/7)。
- `1f97ee79` ⑥⑧⑪用户裁决执行: scorecards 路由三 json 文件名实核匹配 · **⑧起案归类错误被复核方自纠** (open_price_refresh 非 LLM 非推送, 剔除关停面 — 起案方 accept) · 双开关 (weekly_proposer 入口 raise 模式 + after_hours 单点守卫 fail-open) 实现与承诺一致 · yaml 首插断块自纠如实记录。
- `679b826b` 批3六执行项: ④auto_recovery 迁移验证 RiskStateMachine 确为 __new__ 双检锁单例 (:49-62) + auto_recovery 非 RISK_OFF 早退 (:206) 零成本声明属实; ⑦update_from_attribution 签名匹配 + _prune_locked 有 MAX_TAGGED 上限 (记忆不爆炸); ⑬test_upsert_later_writer_wins 真行为测; ⑤⑩⑭与承诺一致。
- **提醒1**: `test_src_priority_ordering` 是自实现的拷贝断言 (改生产值测试仍过), 建议 08-07 后把 _src_priority 提为模块级函数让测试调真身。**提醒2**: 两提交均 --no-verify (理由 hook enabled 正则误报) — 流程上属绕过, 建议修 hook 正则而非容忍绕过。
**⑨翻案重核 (对方退回 → 我逐条 grep 终裁, DEB 第5轮全表)**: 13 条 **8 真死 / 5 起案误判** — 复核方三大反例全对 (market_state 9 文件活 import · RiskEngine scheduler_core:335 活注入 · strategy/factors regime_filter 活调用); 但复核方两处反例也不准确 (detector "文件不存在"系找错目录 · NewsNow "活实例化"宿主函数本身是死的)。yaml 僵尸重核命中率仅 **2/11** (真僵尸仅 event_day_*+signal_scoring, 其余 9 块全活)。**教训**: Explore 子代理零调用结论必须主线程复核后才入案。
**墓碑执行 (注释级零行为变更)**: 墓碑×7 (run_llm_strategy_scan/newsnow/market_regime_filter 类/catalyst detector/postmarket_review/_premarket_news_summary 死读/debate_planner 死注入) + shadow 标注×1 (market_regime_detector 非死仅 shadow) + yaml ZOMBIE×2。py_compile+yaml 校验全过, 物理删除留 08-07 后。

### 2026-07-31 (夜·Qoder) · 全链清理批1六项+批2两项执行 + 剩余 12 项 DEB 起案 (用户裁"按建议执行")
**批1 (零风险, 已落地)**: ①`catalyst.reverse_catalyst.enabled: false` (LLM 止血) ②日志降噪×3 (exit monitor 行数变化才 info/overnight 心跳→debug/playbook ensure_tables→DEBUG) ③数据膨胀治理 — 根目录 33 个 .bak 归档 `data/backups/root_bak_archive_20260731.tar.gz` (204MB→48MB) + 4 个零字节 .db 清除 + 根 ftseek_us.db 空壳 (0表, sqlite_master 验证) 移 quarantine + rsshub news_us 挂 cleanup_old(48h) 一次性清理 25MB→588K ④MarketRegimeGate 类级 TTL 缓存 (120s/vix_uncertain 30s — 调用方每次新建实例, 实例缓存无效, 省 AlphaVantage 付费调用) ⑤OpenD 冗余项调研后改道 DEB (触执行面) ⑥crontab 注释漂移修正。
**批2 (保护活链)**: ①battle plan 启动 veto 降级为告警 (main.py) — **意外发现原 RISK_OFF 调用 import `app.risk.risk_state_machine` 路径根本不存在** (真实路径 app.us_pipeline.*), ImportError 被 except 吞掉, veto 从来都是哑弹还刷误导性 error 日志 → 诚实降级 warning-only ②**kill switch 直挂 playbook 下单入口** (pipeline.py auto_execute 头部, fail-close 与 order_executor:3430 同口径) — 原链路 flag→scan_loop(60s)→RiskStateMachine→门 ≈90s 延迟且线程一死即断路; auto_execute 是 playbook 下单唯一汇合点 (topup 只落库指令), 单门全覆盖, 平仓路径不受影响。
**测试**: conftest 加 autouse fixture 每用例清 regime 类级缓存 (全量跑 test_risk_on_normal_vix 串上下文挂/单跑过的实证); 全量回归见 DEB/测试记录。
**DEB-20260731-fullchain-cleanup-remaining.md 起案**: 12 项 — ③decisions.db 空桩(D1补最小实现) ④interceptor 假哨兵(E1 先迁 auto_recovery 再退役) ⑤因子权重双通道(F1 停周五通道) ⑥记分牌零读者(用户问题: 看→挂dashboard/不看→停cron) ⑦us.json 冻结记忆(H1 接线) ⑧三个 LLM 推送(用户问题: 看→留/不看→停) ⑨死代码×9+僵尸配置×11(I1 墓碑零行为变更) ⑩重复统计(J2 先记口径差) ⑪HK 手动页(用户问题) ⑫双 trade ctx(K1 口径契约) ⑬UPSERT 优先级(L1 加单测) ⑭playbook 无券商侧 STOP(M2 shadow 评估)。含 4 个待另一模型复核分歧点。**生效提示**: reverse_catalyst 关/regime 缓存/kill switch 直挂/veto 降级均下次重启生效。

### 2026-07-31 (深夜·Qoder) · 用户裁决: NewsV2 整体退役 (已执行) + lessons 留生产线两周试用期
**NewsV2 退役 (C2, 用户条件"重复无意义则退役"核实成立)**: on_news_passed=只打日志 · immediate_scan 配置级死亡全史零触发 · UE 自有 finnhub 直采链不读它 → `news_v2.enabled: false` + scheduler 构建门 (退役/真故障分级日志), models 包保留 (UE 引用), 改 true 可复活。**下次重启生效**, 省 NewsV2Loop 线程+RSSHub/SEC 轮询+dedup 库读写+日志噪音。
**lessons (裁决保留)**: 定位=每日复盘辩论副产品 (边际成本≈0); 现役 2 条是共性候选样板但单样本孤证; 当前 record-only **零拦截风险**, 未来注入仅 prompt 软建议且前置 ≥10条+质量审计。试用期至 ~08-14, 审计标准: 重复出现/n≥2/指向具体旋钮; 审计不过连生产线一起关 (含 accumulator 门槛是否重校准一并裁)。
**回归**: 998 passed EXIT=0。详见 DEB-20260731 第 4 轮落档。

### 2026-07-31 (午后·Qoder) · DEB-feedback-loop 收敛 5 项执行落档 (用户裁"请执行") + 998 回归全过
**另一模型取证辩论 → 我独立复核全接受** (DEB 第2轮): lessons 74/76 主线时代陈旧 (A1 前提翻案) · NewsV2 非停摆 (通过=3, C2 前提削弱) · 07-30 三池名义额同量级 (B "双标"实证不成立) · F1 升主交付。
**执行结果** (详见 DEB 第3轮落档表):
①**F1 anchor_dev_pct 统一插桩**: db.py 幂等迁移列已生效 (58列) + save_instruction 接入 (ON CONFLICT COALESCE 防 topup/主链 UPSERT 冲 NULL) + UE inject 记 scan→inject 漂移; **DA skill 侧 staged** `scripts/patches/apply_anchor_dev_da_skill.py` (仓外分类器拦截, 待用户一键); 冒烟三路径 (插入/保留/覆盖) 全过。
②**A4 lessons 治理**: 74 条主线时代降 inactive (快照 `data/audit/lessons_a4_downgrade_snapshot.json` 可逆, 现役=2); **断更根因修复** — `_extract_lessons` 读 `debate_result` (按角色字典) 而 actionable_fixes 在 `arbiter_conclusion` → 07-07 起恒=0, review_orchestrator.py:1676 已改优先读 arbiter_conclusion; 另两因 (accumulator 规则门槛与 07-22 退出链加固脱节 + 07-23 宕机错过复盘) 落档, 门槛重校准属参数调整**留用户裁决**。
③**C1' on_immediate 全史核查闭合**: `immediate_scan.enabled: false` (unified_config.yaml:815) 配置级死亡 + 18 个 gz 归档零 `NewsV2-Immediate` 行 → 自 06-30 挂接从未且不可能触发, **C2 退役条件成立待裁**。
④**B1 sizing-delta shadow 已挂**: pipeline.py 兜底 sizing (DA/UE 唯一 sizing 入口) 落 `data/shadow/sizing_delta.jsonl` (指令 regime 文本 vs MarketRegimeGate 实采 + 各乘数 + size)。**意外发现**: 07-19 F1 修复已让兜底 sizing 走 gate SSOT — 原 B "DA 裸奔" 结构性部分可能已过时, shadow 给终局证据。
⑤死调用 order_executor Task#29 块 (24行) 已删。
**回归**: pytest 998 passed EXIT=0。全部为 record-only/数据治理/代码卫生, 与 08-07 参数冻结零冲突。

### 2026-07-31 (午·Qoder) · 全链路复盘 → 闭环两项 + DEB 起案 (用户裁"1和2先闭环, 其他DEB")
**链路体检结论**: 信号发现层超预期 (方向命中 9-10/12, DA 候选带真催化) · regime 门控真实生效 (cautious×0.5 sizing) · **迭代层半断裂** — 三记分牌手动-only 零代码读者, lessons 注入接退役主线, DA/仓内 regime 双轨断开, NewsV2 零通过空转。
**闭环①**: exit_scorecard/direction_hit/trigger_gate 挂周六 06:20 cron (fwd 回填+factor_ic 之后), 迭代仪式去人依赖。
**闭环② miss 机会成本首跑 (解剖出幻影)**: IC backfill 手动补 307 条 → 10 miss 表面方向命中 9/10 均 +5.76%/条, **但 5/8 入场区全天未触** (MSFT −13%/MU −17% 脱节, 永远不可成交, 门拦对了); 真 miss 仅 SONO/E/ABBV 3 条。**新发现 F1**: DA 入场区与市价严重脱节 (生成侧价格基准缺陷) — 建议 bridge 落库记 entry 偏离度 record-only, 2 周量化后再裁"合理性门"。
**DEB-20260731 起案** (`docs/debates/DEB-20260731-feedback-loop-wiring.md`): 议题A lessons 改接现役链 (A1生成注入/A2执行门/A3维持) · 议题B regime 双轨对齐 (B1接SSOT/B3维持, 建议先 shadow 记 sizing 差值) · 议题C NewsV2 (C1诊断→C2退役) + 顺手项 order_executor:5185 死调用删除。待用户裁决。

### 2026-07-31 (收盘复盘·Qoder 主工程) · 07-30 净 +$94.78 三连净正 + topup 上线以来首补首胜 + 五项验收 4 过 1 空窗 + 另一模型三批改动复核代提交
**战绩 (净口径首个完整日)**: 12 笔 7W/5L, 毛 +$145.48 费 $50.70 **净 +$94.78** — 07-28 起三连净正 (+145.8/+19.3/+94.8)。结构: 4 笔 TRAIL_LOCK 赢家 (TSM+56/GRBK SHORT+46/NVDA+35/SHOP+5.7) + 3 笔 PRE_CLOSE 小胜 vs 5 笔 MFE_STALL 砍损 (GS/T/LLY 零 MFE 单 -44/-42/-43)。
**验收对账**: ①**topup 首补首胜**: 22:30 盘中窗 +1 (SHOP, 上线以来第一条), 净 +$5.66 TRAIL_LOCK — 第四道暗门修复当夜即兑现; 但盘前窗 3 轮仍 +0 (momentum NEUTRAL 为主, 属市况非故障) ②Phase1 evidence 1/1 落库 (SHOP agree=1/2, sector null — SHOP 不在 rotation 名单, 覆盖率待扩) ③G3 reanchored 真归零 (测试污染清后首夜 0) ④费用净额 go-forward: 12/12 fees_est/pnl_net 非空, 推送净额口径实弹 3 笔自洽 ⑤D4 费用感知保本**零样本** (昨夜无 SL_BREAKEVEN 出场, 判据顺延) ⑥流动性/借券池门零拦截 (无小票触发场景, fail-open 未误伤 — GRBK SHORT 正常放行成交)。
**MFE_STALL 首次正名**: 5 笔零/低 MFE 单全部 150min 被砍 (对照 07-23 同款单 TIME_EXIT 拖 4-6h 亏 -370) — 平均单笔损失从 -$37 压到 -$31 且释放资金。high_atr shadow 继续攒样本。
**另一模型三批改动复核 (全过, 代提交 72b6e5fc/f68b19d9/eacb0916)**: ①平仓推送净额主口径 (生产实弹 3 笔验证) ②dashboard 闲置自动退出 — systemd socket 配套完整核实 (socket enabled/--fd 0/Restart=on-failure), curl 实测拉起 302 ✓ ③battle_plan 过期水印 + DEB 三案落档 (含 veto-shadow T1 falsy bug 作者自纠: `if r['sentiment']` 把 0.0 滤掉致"常量退化"误判 — falsy 陷阱家族)。
**待办滚动**: ①S1 按 evidence agree 分桶 (需 2-3 日样本) ②D4 判据顺延至首笔 SL_BREAKEVEN ③topup 盘前窗 NEUTRAL 高占比继续观察 (若 3 日 premarket 恒 +0 而 intraday 有产出, 补量窗口重心可议) ④SHOP 类池标的不在 rotation sector 名单 — evidence sector 覆盖率待 P4a 同款扩充。

### 2026-07-31 (晨·Qoder) · 运维三项 — health_check 内存监控 + hermes 周重启 + dashboard 按需激活 (用户裁)
**①health_check.sh**: 新增第6项内存水位 — 可用 <300MB 告警附 top3 RSS 大户 (07-30 OOM 杀 hermes 零预警的教训); dashboard 检查从 HTTP curl 改**被动端口监听** (配合③, curl 会唤醒按需服务)。单次跑验证通过。
**②hermes gateway 周重启**: cron `30 5 * * 0 XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart hermes-gateway` (systemd --user 单元, 通路已验证) — 418MB/13h 增长长尾封顶。
**③dashboard 按需激活 (已部署✓)**: `app.py` 加 ASGI 活动追踪 (http+websocket 入站报文) + 闲置 watchdog (DASHBOARD_IDLE_MINUTES 默认15, os._exit(0) 干净退出); socket unit 持 127.0.0.1:8901, service 改 StandardInput=socket + --fd 0 + **Restart=on-failure** 关键: 正常退出不复活。用户执行部署后端到端验证: systemd 持端口 + 连接触发 fd0 拉起 + HTTP 302/1ms ✓; 闲置 15min 自退释放 ~128MB + 12 条 OpenD 连接, 下次访问冷启动 2-5s。实测: 中间件启动服务正常, 1.2s 阈值下 watchdog ~60s 自退 ✓ + 998 passed。

### 2026-07-31 (晨·Qoder) · 收盘复盘 + pytest 挂死根治 + 系统巡检 (用户令"要清理挂死+检查优化")
**昨夜复盘 (07-30 ET)**: 12 笔全平 毛+$145.48/费$50.70/**净+$94.78** 净胜率58% — G5 净额推送 12/12 新格式 ✓ · topup F1 首次实产 (盘中窗 +1 条 SHOP 净+$5.66, 盘前三窗 +0 门拦 by design) · P0 三采集全夜无缺 (spread 10×57, markout 36/36, 借券 57) · 盘后报告费用净额总览已出 (近7日净−393.54 假盈利6笔)。stall 反事实: T/LLY 切对, CRM/MRK 被杀 ~$20 留桌, 漏血程度轻微。
**pytest 挂死根治**: 三具挂死 (最久 11h, 150-170MB/个, OOM 推手之一) 全部清掉 (可用内存 1378→1660MB)。根因 = 测试触达 lazy 连接 futu 的 app 代码路径, SDK 非 daemon 线程卡解释器退出 (测试本身 36s 全过)。修复: `tests/conftest.py` 加 pytest_sessionfinish 把非主线程置 _daemonic — 前台全量验证 998 passed + EXIT=0 干净退出。**今后后台跑测试不再产生残留**。
**巡检结论**: 0 failed units · 无僵尸进程 · hermes cron 全 completed · 磁盘 41% 健康 · OpenD 连接 主服务22/仪表12 (观察项)。**发现缺口**: health_check.sh (*/10 cron) 不查内存水位 — 07-30 OOM 杀 hermes 时无预警; 另 hermes gateway 418MB/13h 持续增长, 小机定时重启值得考虑 (均需用户裁决, 未动)。

### 2026-07-30 (深夜·Qoder) · P0 实盘数据准备三采集上线 — spread/盘口 + markout + 借券费率 (用户裁"要", record-only)
**为什么**: 08-02 复盘→切实盘的核心未知是 sim→live **兑现率** — sim 按盘口快照成交, 低流动/宽价差标的盈利含虚高; 借券费率只用于门没落历史; 成交后价格漂移 (adverse selection) 无数据。三项采集只读落盘, 零交易影响, 攒 2-4 周替换 est 成本模型, 为 08-29 终裁提供量化依据。
**落地 (scripts/ 三新脚本, 统一 flush+os._exit 防 futu SDK 挂死, cron timeout 180)**:
- `spread_depth_capture.py` — 盘中每 30min 对活跃池 (57只) 抓 bid/ask/量 → `data/shadow/spread_depth.jsonl`。**设计改道**: 原计划 get_order_book, 实测需 ORDER_BOOK 订阅且坏连接会永久挂死; 改 `get_market_snapshot` 批量 (自带 bid/ask/量, Nasdaq Basic 权限即有, 单次往返零订阅)。
- `markout_capture.py` — 每 5min 扫当日入场, 对 +60/300/900s 三 horizon 记方向感知 drift → `data/shadow/markout.jsonl` (trade_id+horizon 幂等)。
- `borrow_rate_snapshot.py` — 21:05 开盘后对活跃池查空头池 → `data/shadow/borrow_rate_series.jsonl` (池数/年费率/可空, 复用 `dual_broker.get_short_pool`)。
**验证 (盘中实测)**: spread 57/57 ✓ (WDFC 0.72% 宽 vs WMT 0.027% 窄 — 成本区分度正是目标数据) · markout 36/36 (12笔×3horizon, 报价 12/12) ✓ · 借券 57/57 ✓ · 三脚本均秒退无挂死。cron 5 条已挂 (*/30 + */5 盘中双时段 + 21:05)。**futu 新坑落档**: OpenQuoteContext 握手超时**不抛异常返回坏 context** (后续查询挂死) → 探活模式 get_global_state + 重试 3 次, 已内置于 spread 脚本; 另杀 1 个挂死 pytest 残留 (b3btjs59l 后台任务, 测试 36s 早过 998 passed, futu 线程卡退出 2h20m 白占 154MB)。

### 2026-07-30 (晚·Qoder) · G5 报表净额化 — 平仓推送+盘后复盘从毛口径切净口径 (用户裁"要做")
**实锤**: DEB-fee-net-pnl D1-D3 净额只到 DB 层, 推送/报表全毛 (trade_exit 标题/audit 全部 SUM(realized_pnl), pnl_net 零引用) — 07-29 那种"毛+50 推送, 净实为+19"的误导延续。**落地两处**: ①单笔平仓推送 — `trade_exit` 加 fees/pnl_net 可选参, 提供时标题 `净-$1.41 (毛+$3.20/费$4.61)` 图标按净额着色, pipeline 平仓环复用前段 `_fees_total` 传入, 估算失败自动退回毛口径 (向后兼容其他调用方) ②盘后复盘 (hermes 04:00) — 新增归因视角0 费用净额总览: 今日/近7日 毛/费/净三列 + 净胜率 + 假盈利计数 + 费用覆盖率, 旧库无列静默跳过。纯展示层, 零交易链路变更。
**验证**: 双态标题 ✓ (净口径/旧兼容) + 真实库近7日 50笔 毛−237.09 费251.22 净−488.32 假盈利6笔 覆盖率100% ✓ + 服务 21:05 重启 (今晚平仓即带净口径)。明晨 04:00 复盘报告首见三列总览。

### 2026-07-30 (晚·Qoder) · DA 补跑 + regime shadow 挂死根因修复 + battle_plan 退役三步 (用户令)
**DA 补跑**: 18:00 盘前扫描触发后被 OOM 杀掉 (内核 18:12 杀 hermes 主进程, 3.5GB 小机; 挂死进程堆系推手), gateway 18:13 自拉起但执行标 unknown, 07-30 计划未生成。清理 3 个挂死进程后 18:30 手动补跑, 19:10 completed — opportunity_scan 13 候选 + 13 指令写 DB, 今晚供给就绪。
**shadow 挂死根因 (实锤)**: 非采集卡死 — 脚本干完活 (jsonl 行已写) 但 futu SDK 非 daemon 线程 (network_manager/callback_executor/lv3 watchdog) 使解释器永卡 threading._shutdown; print 块缓冲随进程卡死 → 日志全空假象; cron 每晚新增实例逐夜堆积 (07-28/07-29 两具 20-44h)。修复: main 末尾 flush + os._exit (复现验证秒退) + cron timeout 300 双保险。
**battle_plan 无损退役三步**: ①写入方已死 (premarket_planner.py 已删, ADR-012 冻结, legacy_battle_plan_enabled:false) ②dashboard stale 水印本就存在 (battle_plan_is_stale 三处), 补 `_stale_notice` 显式横幅字段 ③UE 去重源**经查证为死代码** (_load_battle_plan/_existing_plan_symbols 全仓零调用), 直接删除 — 此前"UE 永久回避 5 只"担忧证伪, 实为零效应残留。交易面零消费实证: playbook 读 daily_active_pool + playbook_instructions。**遗留疑点**: 文件 07-30 10:31 被写过一次, 现存代码无写入路径, 写入方身份未定论。

### 2026-07-30 (傍晚·盘中方向证据 Phase1) · topup 指令落库时并排记录三层证据 (record-only, 用户裁"统一, 今晚开盘前完成")
**背景**: 用户问"盘中能否优雅简单精准识别多空方向" — 盘点结论: 识别管道已存在三层 (technical 三票[今日通电]/VWAP 硬门[live]/sector_pulse[有人生成没人吃]), 缺的是互通与预测力检验。三段式: Phase0 今晚 technical 裸奔基线 / **Phase1 本批** / Phase2 数据胜出者升门 (另裁)。
**实现** (`pipeline._gather_intraday_evidence`, topup 落库环挂接): raw.evidence = {vwap_side/vwap_dev_pct, order_flow_imbalance, ticker_buy_sell_ratio, volume_ratio, sector, sector_pulse_chg, pulse_asof_et, **agree/avail 一致度**(方向无关统一式 sign×证据正负)}。record-only: 失败返 None 不阻断落库, 任何异常不影响指令。
**连带修**: rotation 细分行业名 × pulse SPDR 12 键两套名库直匹配全 null (Semiconductors≠Semis / Health Care≠Healthcare) → `_SECTOR_TO_PULSE` 关键词映射表, 离线覆盖 18/18, 映射不上如实 null 不猜。
**验证**: 真数据 NVDA SHORT agree=3/3 (VWAP below+OFI 负+Semis -2.18%) / HCA BUY agree=1/3; mock 双向语义 (同数据 SHORT 2 票 vs BUY 0 票); router=None 异常安全; 998 passed / 重启冒烟 7/7。
**消费判据 (预注册)**: 攒 2-3 日后 S1 按 agree 分桶 (3/3 vs ≤1/3) 切方向命中率 — 差距 ≥15pp 才提 Phase2 升门案; 无差距则证据叠加不升门, 如实结案。

### 2026-07-30 (午后·遥测测试污染修尺子) · pytest 假事件写进生产遥测 5.6%/13.4% — 守卫+存量清理 (用户裁"要")
**发现路径**: 07-29 复盘时 reanchor 关停后 trigger_miss 里仍持续出现 `reanchored` 事件 — 时间戳与每次全量回归完全吻合: 测试直调 `_check_intraday_trigger`, `_emit_trigger_miss`/`_emit_entry_shadow` 无隔离, **07-11 埋点上线起每跑一次 pytest 就写假事件** (trigger_miss 331/5893=5.6%, entry_shadow 336/2498=**13.4%**)。危害现行: G3 观察判据"reanchored 桶归零"被污染成永远非零; entry_shadow 的入场时机门离线切片 13.4% 假样本。
**修复**: ①两 emit 函数入口加 `"TEST" in symbol.upper()` 守卫 (测试标的一律不落盘, 连 ZTEST 类交易所测试符号也覆盖) ②存量清理 331+336 行 (bak_testclean_20260730 双备份) ③闭环: 跑污染源测试文件 13 passed 且遥测零新增 / 全量 998 passed 后仍 0 残留 / **G3 判据修后真读数 = 0** ✓。
**免重启说明**: 生产服务从不处理 TEST 标的, 守卫只对 pytest 进程有意义 (每次冷加载新代码) — 不重启, 避免非必要扰动。
**教训 (归档)**: 只读埋点的"零行为改动"承诺不含测试进程 — best-effort append 类埋点必须带测试标的守卫, shadow/telemetry 消费前先查 symbol 基数里有无 TEST。

### 2026-07-30 (盘中·Qoder) · 流动性门两补丁实弹 — ①名义额接真实 sizing ②SHORT 借券池门 (用户裁"直接做 并更新待办")
**① sizing 修复**: 流动性门名义额原硬编码 `cap×4%`≈$3,280, 与真实 sizing (上限 $3,925) 低估 ~20% → 改从 `inst.position_size` 实取, 无值回退旧口径。**② 借券池门 v2**: SHORT 触发前查 `get_margin_ratio` — `is_short_permit=False` (券商不可卖空, sim 假成交陷阱) 或券池余量 < 计划股数×10 安全倍率 → 拦。复用流动性门同套纪律: (symbol,date) 日级缓存零配额增量 / fail-open / 拦截日志每标的每日一次 / telemetry reason=borrow_pool_gate / 回滚=关 `borrow_pool_gate_enabled`。
**验证**: 功能级 10/10 (L1-L5+B1-B5) + **端到端实盘接口实测** (SNDR 池 58万股/费率 3.0%/可空✓, NVDA 1.09亿股✓, BABA 852万股✓ — `dual_broker.get_short_pool` SIM 环境可查) + 998 passed + 服务 13:00 重启冒烟干净。注: futu SDK 后台线程非 daemon, 脚本直调需显式退出, 服务进程无此问题。

### 2026-07-30 (盘前·流动性门上线) · 用户裁"小盘票补充限制" — 触发侧第四门: 仙股地板 + 名义额/ADV$ 上限 (实盘基准审计 T1-#5 落地)
**盘点实锤**: 现有三面对小盘票零覆盖 (screener 无流动性过滤 / 触发侧仅 >8% gap 门 / UE 仅 LLM 软自报 crowding_low); **低 ATR 门恰好反向** — 挡低波不挡高波小票。sim 按盘口快照成交, 低流动标的盈利含虚高成分 (实盘冲击 30-100bps)。
**实现** (`pipeline._check_liquidity_gate`, 触发侧 gap 门旁): ①仙股地板 $3 ②仓位名义额 ≤ 0.5% × ADV$ (5日均成交额, 复用 get_daily_bars + (symbol,date) 日级缓存 — 零 K 线配额增量, bar-ruler 教训)。只减不加 / fail-open (数据不足放行, 与量能门同口径) / 门自身异常不拦交易 / telemetry reason=liquidity_gate / 拦截日志每标的每日一次 / 回滚=关 `liquidity_gate_enabled`。**spread 门如实放弃**: router quote 无 bid/ask 字段, 不硬造。
**校准验证**: 功能级 5 场景 ✓ + 真实抽查 — 现役最小盘 GNK 仅 0.053% of ADV 照常放行, 现有宇宙零误伤, 只拦真枯竭票。998 passed / 冒烟 7/7。
**验收 (预注册)**: ①3 日内 trigger_miss reason=liquidity_gate 出现即证明通电 (无也合法 — 现宇宙本就不该拦) ②被拦标的记入 telemetry 供反事实 (拦错了会在 scorecard 显形) ③与 UE 注入侧的关系: 门在触发层兜底, UE prompt 侧暂不加机械过滤 (一次一变量, 若门频繁拦 UE 标的再前置)。

### 2026-07-30 (盘前·D4 保本线费用感知实弹) · 用户改裁"直接实现" — 单开关落地, 双向修锚 (小仓抬线灭假保本/大仓放宽 6 倍缓解死亡谷)
**如实定性**: 严格属出场行为变更 (非纯尺子), 但 0.15% cushion 本身是错尺子; 今日出场侧唯一实弹 → 分层一次一变量合规。实现: meta._qty 注入 + cushion 单点换算 (下游 ratchet/归类/be_shadow B 锚三处自动同口径) + `breakeven_fee_aware_enabled: true` (回滚=关开关) + **exit_config_version 轮转 → exit-ed3625929072** (毕业统计窗口切开, 07-20 军规)。功能级四场景 ✓ / 998 passed / 冒烟 7/7。
**验收 (预注册, DEB §十二)**: ①BREAKEVEN 出场笔 _be_cushion_fee_aware 在 metadata 且净额≥−$0.5 ②3 日 BREAKEVEN 桶净额≥0 (基线 毛+89/净−48) ③占比不异常放大 ④回滚触发=假保本净亏>2笔。**08-07 be_shadow 解读须注明 07-30 起基线已是费用感知线。**

### 2026-07-30 (盘前·费用净额口径落地) · DEB-fee-net-pnl D1A/D2/D3/D5 当日完成, 今晚首验 (用户裁"执行")
**一句话**: 全库 PnL 从毛口径换真 — fee_model 纯函数 (SHORT 对称/SEC 时间序列/借券 365) + 515 行回填 (白名单 168 笔终版: 全史 毛−1364.61 费759.42 净−2124.03, 假盈利 14 笔三方一致) + 平仓钩子 go-forward 记费 (try/except 零阻断) + I8 不变量 (毛−费=净) + data-map 白名单强制段落。G1 18/18 / G2 手算 10/10 / G3 残留 0 / 998 passed / 冒烟 7/7。**交易链路零变更** (纯记账侧)。
**今晚验证判据 (预注册)**: ①新平仓笔 fees_est/pnl_net 非空 + I8 过 (明晨 05:05 cron 自动) ②抽 1 笔手工复算逐分吻合 ③触发/成交/退出日志与改动前同构。
**排队**: D4 保本线费用感知实弹 (F1 后一次一变量); G4 矩阵净口径化 (08-07 编制时); G5 报表三列 (下期)。提交 82235545, 详情 DEB §十一。

### 2026-07-30 (盘前·Qoder) · DEB-20260730-fee-net-pnl 起案: 实盘费率净额口径 (用户命题"以实盘为标准统计, 否则白干")
**实锤**: 全库 5 条 PnL 路径全毛公式零扣费 (`order_executor.py:4462`/`pipeline.py:5710/5954/5663`/`audit.py:305`), `Order.commission` 零赋值, replay 反推费用恒 0; 唯一费用意识=拍的 0.15% cushion (当前仓位下巧合≈真实 11-17bps, 结构错误)。**按用户费率表 est 回填**: 07-29 毛+49.8→净+19.3 (侵蚀61%, BABA 毛正净负) / 07月 毛-214→净-955 (费741) / 历史515笔 毛+19535→净+13063 / 假盈利单35 / SL_BREAKEVEN 桶毛+89→净-48 (假保本14笔-77)。结构: 最低佣金$1.99×双边→每单固定成本≈$4-5, 毛利<$5皆白干。**DEB 落档** `docs/debates/DEB-20260730-fee-net-pnl.md`, 裁决点 D1 净额SSOT / D2 fee_model+yaml费率表+fees_source三态 / D3 回填+08-07矩阵净口径重算(允许翻转) / D4 保本线费用感知(行为变更须排队) / D5 入场费用门槛shadow-only; 预注册 G1-G5, 辩论方 T1-T5。诚实边界: SHORT 借券费未含(净额系上限)/est≠实收/sim期无法对账。已挂 handoff 3i 待辩论+裁决。**零代码变更**。

### 2026-07-30 (盘前·Qoder) · 07-29 交易日休市复盘 (用户令)
**当日战绩**: 7 成交全平 +$50 (SNDR SHORT +94 / ADBE +90 / LLY+5 / MRK+5 / BABA+3 / GNK -76 / NVDA -70)。MFE_STALL_EXIT 首战 2 发 (GNK/NVDA 各磨 150min 小亏离场, 规则按设计工作)。13 跳过 BUY 门归因: low_atr 10 只 (DA 防御股老摩擦) / extreme_gap 5 / streak_ban 2 (PDD/UNH) / vwap_gate 3 / vol_gate 1 (T)。
**O1 关停 day-1 (G3 观察)**: live reanchored=0 ✓ (遥测 8 条全 US.TEST 测试噪声), 其余桶无异常漂移。
**🔴 F1 day-1 = +0, vote_none 仍全量**: topup 三轮 (21:02/22:30/22:46) 过滤审计 input=17/17/18 → instructions=0, **vote_none=17/17, 17/17, 18/18** — F1 band (RSI 55-65/30-45→0.6) 已随 13:45 重启在腔内, 但死路径未复活。两读: ①当日池内标的 RSI 本就不在新 band (合法零) ②根因不止 quality 分 (vote None 另有其门)。**鉴别需 per-symbol RSI 快照下钻 — 若 day-2 仍全量 vote_none, 必须做 (查 17 只评估时 RSI 落点), 否则 F1 三日验收将无脑判死**。供给速率 3 日计数: 0/3 起。
**健康**: 路径湖 81 (+7, ok=7/7); sector_pulse 修复后全日零泄漏; 服务日志 0 ERROR; 持仓全平。注: 日志 03:43 日切, 复盘需读 ftseek_us.log-20260730 (当日窗在轮换文件里)。
**regime_history vix/spy=0.0 修复** (app/strategy/regime_filter.py): 根因=RegimeVerdict 从未携带市场数值字段, record_regime_change 三调用点只能吃默认 0 (建表起即空壳)。修复: ①verdict 补 vix/spy_price/spy_ma200 (默认 0.0 兼容旧构造) ②evaluate_regime 六构造点注入 ③三记录点透传 ④**无值首调跳过** (重启期全 0 churn 行不再落表, 今日 13:44 一秒 8 行实证)。内联冒烟 5 项全过 (含单例实证: 13:44 连翻系多进程各自首调, 非代码 bug)。零交易影响 (该表纯审计, 无代码消费方; 门禁用内存活 verdict)。
**推送标题去重** (app/playbook/feishu_push.py): 盘后追踪卡与盘前指令卡共用硬编码标题"📋 Playbook 交易指令" → 钉钉观感重复 (实为不同内容, 幂等无恙)。_send 加 title 参数, 盘后卡改"📊 Playbook 盘后追踪"。11 tests passed。两项均随下次服务重启生效。
**附带确认**: playbook_regime_log 自 07-09 无真实行属已知休眠 (DA 主链接管生成, feishu_push.py:59 注释明载), 盘前推送走实时 assess() 无影响。

### 2026-07-29 (凌晨·Qoder) · sector_pulse 进程泄漏修复 + 消费方审计 (用户查"多个进程")
**泄漏修复**: 脚本不调 `router.close()` → Futu 非守护线程吊住解释器退出 → cron 每 30min 泄漏一个进程 (实见 12 僵尸, 各 ~70MB)。已补 try/finally close (scripts/sector_pulse.py), 僵尸全清, 实测 exit=0 无残留。数据面始终健康 (每 30min 一写, 原子)。
**消费方审计 (顺藤)**: UE 注入系时区死锁 (UE 09:10 ET 盘前跑 vs pulse 10:00 ET 首写 + 当日守卫 = 100% 跳过=死接线); DA 零消费; 覆盖写无历史。两项 (③板块急跌 veto 源并入 F2b / ④pulse 历史落盘) 已挂入 `DEB-20260729-topup-event-veto-shadow.md` §十 待裁决, 详见 3g。

### 2026-07-29 (起案·Qoder) · DEB-20260729-position-path-lake — 日内数据资产化: 持仓路径湖 + 联邦存储串联层 (用户裁"可起草")
**命题**: 日内交易全链路数据 (信号→退出) 如何沉淀为独有可迭代燃料。作者主张: 燃料=决策×状态×路径×反事实 (行情数据是商品); 缺"路径"是 dd 扫描被迫一阶近似 + be_shadow 攒样本慢的共同根因。
**存储架构决策 (用户点名问题)**: **不建单体仓** — handoff 同构: 写侧各自存储原样不动 (零回归), 串联靠 `decision_id` 官方主键 (实测 trade_records 近 7 日 78/78 全填充), 索引成文 (data-map.md, 比照 circuit-breaker-map), 读侧统一为视图非管道重写。维度纪律: 不按标的/行业学参数 (每股 n<5 = 占星术), 层级学习 = 全局→单维下钻 (分桶→方向→时段→regime)。
**交付定义**: P0 平仓后异步采 entry-30min→exit+30min 1min 路径 (复用 fetch_bars + replay_bars 313 文件约定, 失败只 WARNING 绝不影响退出链路); P1 decision_view.py 只读视图 + data-map.md。预注册 G1-G5 验收 + T1-T5 辩论任务 + 四条诚实边界 (不解决 n 饥饿/选择偏差原样/OPE 方差需 shadow 终裁/前视风险)。已否决: 单体仓重写 / ML 退出模型 / 先加 shadow / 按标的学参。
**配套架构决策 (用户同意)**: Obsidian 定位**人读镜像层非机器真相源** — 三边界: ①真相源唯一在 DB/jsonl, vault 机器写人读、手写内容独立目录 ②DEB/handoff 正本留 repo git (审计), vault 只放镜像 ③写 vault 失败不得影响交易链路。衔接: data-map 管机器串联, vault (symbols/ 页) 管人脑串联, decision_view 渲染产物进 vault。

### 2026-07-29 (独立复核·Qoder) · 供给侧双修+看板审计三 commit 证伪式复核 — 通过, 2 个口径修正已写回 DEB 第二轮
**复核对象**: 684e057e (topup F1 + dd 扫描 + UE 定性反转) / 3b03bd1e (复盘补充) / 079116a4 (看板 P0×2+P1)。
**独立重跑全过 (不引用作者数字)**: ①F1 band inline 回归 20/20 — 旧 12 band 零变化 + 新边界无洞 (LONG rsi=55→0.7/56-65→0.6/66→0.3, SHORT 45→0.7/30-44→0.6/29→0.3), 接线在 live 路径 (evaluate L221 + yaml 0.5) ②dd 扫描逐字复现: 109 笔 -477.6, 0.6→-71.8 (+405.8), 排序 0.6>0.8>1.0>1.2 ③DA cron stderr `2>>logs/da_pipeline_stderr.log` 实测已在 jobs.json (append) ④看板旧键 slippage_pct 全库确认从未存在 (常量退化指控成立), 新键同 SQL 复跑 74 样本/5.1bps/超标 2 (与作者 68/6.97/1 差系隔夜窗口推移, 非错误) ⑤date('now') 代码清零 (仅剩 2 注释) ⑥980 passed 独立复现 (39.2s)。
**2 个口径修正 (已写回 DEB 第二轮, 不改结论)**: R1 UE SHORT band 实为非对称 [锚×0.95, 锚×1.03] (CHASE 5%/KNIFE 3%) 非"±3%", reanchor 上限 yaml 实测 4.0% 非 3% — 9-19% gap 对真值同样远超, "机制各自正确组合失效"定性不变; R2 F1 "单测"实为 inline 未落库 (commit 无测试文件) — 已在 DEB 补可复现断言清单视同补档。
**纪律核对**: F1 信号面扩大但基线为零 (只增不改) + 验收判据预注册 (3 日补量>0, 胜率<30% 回滚) + 当日无叠加变更 → 合规。UE O1/O2 仍待用户裁决 (复核方同意作者建议: O2 观察 1 周 + trigger_miss 反事实统计)。

### 2026-07-29 (收盘复盘补充·Qoder 主工程) · 独立抽验通过 + 假价修复满分首战 (另一模型复盘遗漏项) + dd 参数扫描建议
**独立抽验 (不采信作者数字)**: 6/6 全胜 +$191.90 ✓ / 捕获率 49% 复算一致 ($390.9→$191.9) ✓ / 全 SL_TRAIL_LOCK ✓。
**补验出的满分首战 (收盘复盘未提)**: ①入场价源 **6/6 broker_deal_confirm**、平仓 **6/6 broker_deal** — 07-27 假价双修 (确认窗带价) 首个交易日零兜底零假价; 假价源新增 0 (3日≤3 判据首日满分) ②不变量机器对账首次 cron 自动跑 (05:05) 74 样本全过 — "用户人肉对账→机器每晚对账"闭环第一晚即生效。
**对收盘复盘的两点校准**: ①high_atr shadow "今晚反指" — n=1 且为 V 型反转日, 该样本恰是 shadow 要收的反例, 结论留给 n≥5, 不预设方向 ②be_shadow 强顺风日零样本符合设计, 但样本量风险实在 → **对策不是再加 shadadow, 是离线反事实**: 捕获率 49% 两日一致 (周一亏日/周二胜日都 ~50%), exit_telemetry 已有 peak/exit 全量数据, 周五 exit_scorecard 加 **dd 参数扫描** (0.6/0.8/1.0/1.2 各档反事实落袋) — 零新埋点, 全历史即样本, 直接量化 "dd=0.8 回吐一半" 的替代收益, 与 be_shadow 互补供 08-07 裁决。
**明日待办排序 (在收盘复盘 4 条上)**: P0 topup F1 DEB (根因双坐实) + DA cron stderr 重定向 (一行); P1 UE SHORT 锚定修法 (等反弹锚在单日有效期内存活率≈0, 应改顺势确认锚) + dd 参数扫描并入周五 scorecard。

### 2026-07-28 (收盘复盘·Qoder) · 6/6 全胜 +$191.90 但捕获率 49% 老问题持续 / topup 根因最终确认 / DA 提速+板块验收达成 / 链路零异常
**战绩**: 6 触发 6 成交 6 全胜 +$191.90, 全部 SL_TRAIL_LOCK 平仓零过夜 — TSM+37.40/NVDA+22.66/MRK+22.75/BABA+9.43 (LONG) + ZIP+51.15/BCAX+48.51 (UE SHORT)。对比周一 (10笔80%WR -$118): 两日落袋 +$74。
**核心矛盾未变 (数据实证)**: 方向 6/6 全对 (MFE 1.18~2.59%) 但**赢家捕获率 ~49%** — MFE 美元潜力 ~$390.9 vs 落袋 $191.9 (TSM 54%/NVDA 54%/MRK 44%/BABA 30%/ZIP 50%/BCAX 52%)。trailing dd=0.8 继续系统性回吐一半浮盈; 顺风日被盈利掩盖, 但 R:R 引擎的结构性天花板仍在 — 正是 tiered_dd/be_shadow 在测对象, 周五 exit_scorecard 同口径对比。
**链路健康**: 零 Traceback / 不变量对账 74 样本零违反 (attribution 74/74) / e_shadow 检查点 7/7 按持仓时长应采尽采 / 定桶 18/18 / gap 守卫拦 GLW/MU/WDC/T 按设计。
**topup 根因最终确认**: 全日 4 轮 (盘前×2+盘中×2) vote_none=15/16/13/13 全量 + **零评估异常** + mkt_data_none=0 → 根因③ (死路径/门槛) 跨双窗坐实。F1 修复 DEB 可起。
**Shadow 积累**: ①high_atr_stall 首样本 — TSM 60min 时水下-1.23%/MFE 0.199% 触发反事实出场 382.22, 实际 TSM 拉回 +$37.40 → **规则今晚反指 (会错杀赢家), n=1** ②be_shadow A/B 零触发符合设计 (今夜全 trail_lock 出场, 无保本出场供 A, 无回踩 +0.30% 供 B) — 注意: 若市场持续强顺风, be_shadow 攒样本会慢, 08-07 裁决点样本量风险前置提示。
**DA 验收 (首跑)**: ①38.9min 达成 (报告自证 2334s, exit 0, 50 分析/50 IC 写入) ③板块真实数字引用 ✓ — SUI "Real Estate 昨日-0.41% 第8弱" + 挑战者同引, T "通信板块盘中脉搏0.0%缺乏动能" (A3 预言应验: 今夜 pulse 平盘噪声被 LLM 正确中性化) ④11/11 LONG (上涨日合理; 逆风日才是板块级联条款真考验) ②**不可验 — cron 命令 `2>/dev/null` 丢弃 stderr, 建议改重定向到文件** ⑤部分可验 (50/50 无大面积退化, 精确 coverage 对照需 skill 侧数据)。
**UE 观察 (n=3 不下结论)**: 3 个未触发 SHORT (MU/WDC/GLW) 锚定价高于市价 9-19% — 等反弹做空的入场在单日有效期内存活概率≈0, UE SHORT 锚定离市价过远的质量问题, 记入 scorecard 跟踪。
**明日待办**: ①topup F1 修复 DEB 起草 ②DA cron stderr 重定向修复 (一行) ③周五 exit_scorecard 看捕获率口径 ④high_atr/be_shadow 样本跟踪。

### 2026-07-28 (开盘夜·链路健康检查) · Qoder 全链实测: 指令→触发→成交→退出→记录全绿 + topup 死路径诊断遥测确认 + DA 提速验收①达成
**入口层**: DA 18:00→**18:39** 完成 (39min vs 旧 102min, 提速验收判据① ~18:45 达成; 板块上下文已注入, 候选0分析含板块引用), 11 候选全 LONG; UE 21:10 产出 7 条 (含 2 SHORT — 板块级联 v2.3 首批 SHORT 实单); 定桶 18/18 全覆盖 (vol_bucket+atr_pct)。
**触发/成交 (截至 22:06 CST)**: 6 触发 6 成交零miss — DA 3 (NVDA/BABA/MRK) + UE 3 (TSM LONG + ZIP/BCAX SHORT); 极端 gap 守卫正常拦截 GLW/MU/WDC/T (entry 偏离 8-19% 拒触发, 按设计)。
**退出侧**: 4 LONG 持仓 SL 全设 (按桶 3.6-5%), 20s 退出循环正常; ZIP/BCAX 两 SHORT 已 SL_TRAIL_LOCK 锁利平仓 (+$51.15/+$48.51, 持仓 12-15min)。
**记录**: 已平 2 笔 attribution 五维块/config_version=exit-8ef6dbb13e5e/exit_slippage/mfe 全落, 与 07-24 同构 — 无记录回归 (entry_slippage_bps 不在 trade_records.metadata 是既有设计, 07-24 亦无, 非今晚故障)。
**topup 诊断确认 (下午静态实锤 → 遥测坐实)**: 今晚盘前批 2 轮 vote_none=15/15, 16/16 全量 + **零"评估异常"WARNING** + mkt_data_none=0 → 排除①异常②字段缺失, 根因③ (门槛/死路径) 确认。盘中窗 22:30 起继续观察。
**观察 (非新故障)**: 两 SHORT 各回吐 ~50% MFE (ZIP mfe $102→锁$51 / BCAX $94→$49) — 保本死亡谷/锁利呼吸空间老问题在新数据上继续显现, 正是 be_shadow A/B 在测对象。
**环境**: VIX 18.3 NORMAL / 零 Traceback / 16:39 重启后 Watchdog 零 ERROR / sector_pulse 22:00 正常刷新。

### 2026-07-28 (下午·topup 零产出根因静态实锤) · Qoder 预诊断: 两条回调路径被质量函数判死 — 死代码, 零产出是结构性必然 (待今晚数据确认+用户裁决修复)
**生产遥测 (周一 5 轮全量)**: 07-27 20:47/21:02 (premarket×2) + 22:32/22:47/23:19 (intraday×3) — input 18-20 → instructions=0, mkt_data_none=0 (ctx 构建健康), vote_fail=0, 全部掉进 vote None 隐形路径 (旧审计盲区)。离线复现尝试被冷启动挂起阻断 (收盘时 LV3 连接重试), 改走静态契约审计, 命中根因。
**代码实锤 (无需数据即可证)**: `technical.py` 06-26 新增的两条回调入场路径与 `_pullback_quality` 评分表系统性矛盾 (yaml `min_pullback_quality: 0.5` 实测生效) —
- **LONG 路径3 死**: RSI 55-65 动量延续入场 (`_is_pullback_entry` 放行), 但质量表 LONG 对 RSI 55-65 返回 **0.3 < 0.5** → evaluate 必然 return None。自 06-26 加入起从未放行一笔。
- **SHORT 路径3 死**: RSI 30-45 趋势延续做空, 质量表 SHORT 对 RSI<45 返回 **0.3 < 0.5** → 同样从未放行。阴跌市 (RSI 30-45 最常见) 的 SHORT 主入口恰好是这条死路径。
**有效接收窗推导 (为何天天 +0)**: LONG 实际可行域 = RSI∈[30,55] ∧ momentum 2/3 同向 ∧ MACD柱≥-0.005 ∧ 布林位置约束; CAUTIOUS 市里池内科技股 momentum 多 NEUTRAL (指标 2/3 不一致) 或 SHORT 向, SHORT 向 RSI 又常落 30-45 死路径 → 零产出是结构性的, 与 07-24 复现 (8 NEUTRAL/3 非回调窗/2 质量不足) 完全互洽。
**今晚确认路径 (自动出数据)**: 20:45 CST 盘前批起, 昨夜诊断补丁 (vote WARNING + vote_none 桶) 随 12:55 重启在腔内 — ①无 "评估异常" WARNING + vote_none 全量 → 根因③坐实 (排除①异常②字段缺失) ②若有 WARNING → 改走异常分支修。
**修复选项 (待用户裁决, 均涉信号分布 → DEB+一次一变量)**:
- **F1 修死路径**: 质量表补两 band (LONG RSI 55-65→0.6 / SHORT RSI 30-45→0.6), 或反向承认不要这两路径删死代码 — bug 级内部矛盾, 但放行面扩大 = 信号分布变更, 须 shadow/DEB。
- **F2 事件 veto shadow (攒样本类合规)**: technical 方向对新闻/财报全盲 (RSI 32"深度回调 LONG"可能是接飞刀), 盘中新闻/8-K 已分钟级采集, 做 record-only veto 标注。
- **F3 池卫生**: P-1 名单合一+ATR 过滤 (已在计划)。
- **诚实边界**: topup 是"趋势+回调"纯技术形态, CAUTIOUS regime 天然低频, 即使修好也可能补不满 30/日 — 那是设计诚实非故障; 供给缺口大头仍在 DA/UE 侧。

### 2026-07-28 (下午·A2 原子性修复 v2) · 用户点破"全部完成才落盘丢整批"风险 → 改按序流式消费, 原子性回到旧模式水平
**用户问**: 新模式是否要等全部完成才落盘, 中途异常/重启会丢批次吗? **如实确认**: v1 实现确实如此 — 并发段 (~40min) 进程死亡丢整批 (旧串行逐股落盘只丢尾部)。复核方 A2 判"知情接受", 用户不接受 — 正确, 有零成本正解不该接受退化。
**v2 修法 (pipeline.py, 4 处改写)**: 预收集全量等待 → **按序流式消费**: futures 后台并发 (池跨主循环存活), 主循环 `_futs[i].result()` 按序阻塞取第 i 股, 取到即进原串行落盘段 → **逐股持久化原子性 = 旧模式** (死亡只丢未落盘部分), 提速不变 (第 i 股落盘时 i+1/i+2 后台在跑); 完成/失败日志移入 worker (实时可见); 主循环后关池。
**验证**: AST ✓ / v1 预收集残留清零断言 ✓ / 标准库语义微验证 (3 任务: 保序消费 + 等待期后台完成时序断言) ✓。
**A1 补充判据** (复核方提出, 采纳): 今晚验收加第⑤条 — stderr 逐股 coverage_pct 与昨日串行对照无显著下降 (3×并发对 OpenD 频限的静默退化检测)。A3 注记: 今晚 pulse 为凌晨平盘噪声 (asof 已标注), 真载荷是日更 rotation, 明起 pulse 才有意义。

### 2026-07-28 (下午·独立复核) · Qoder 对 DA 板块接线+提速批的证伪式复核 — 通过, 附 3 个非阻断攻击点
**独立实证 (不引用作者数字)**: ①diff 与声明逐行一致 (llm_synth 7 行可选段 / pipeline 板块上下文+ThreadPool(3) 两阶段), .bak 双备份存在 ②线程安全论证成立 — llm_synth 无状态 urllib 单次调用无共享 session / data_collector 全 subprocess / deep_analyzer sqlite 纯 SELECT / claim_ic_cache 写入方仅 alpha_backfill (analyze 路径外) / 模块级状态全为不可变配置常量 → "零并发写风险"属实 ③落盘副作用 (gate jsonl/ic_writer/obsidian) 确认留在串行主循环 (diff 未触碰) ④异常语义等价 (worker 捕获→主循环 raise→逐股 except→失败记录, 不会全批 abort) ⑤结果按原 idx 键控 = 顺序/信号分布不变 (变更纪律#2 合规) ⑥功能测试独立重跑: 注入/无键兼容/自证要求 3/3 过, py_compile 双过 ⑦遥测核实基线: 昨日 scan json mtime 19:42 + 12/12 LONG 均与文档一致 (七律#7) ⑧scanner 确产 sector_changes/symbol_sectors 两键, pulse 文件原子写且 cron 窗与 18:00 DA 无读写竞态。
**攻击点 (3, 均非阻断)**:
- **A1 (中, 验收判据缺口)**: 四条验收缺"采集质量对照"。3×并发对 Futu OpenD 账户级频限是新增压力, 静默退化模式 = `_quality` coverage 下降但流程"成功" → "质量零变化"会假成立。建议补判据⑤: 今晚 stderr 每股 coverage_pct 与昨日串行对照无显著下降 (`数据采集: {...}` 行本就逐股打印, 对照零成本)。
- **A2 (低, 失败原子性位移)**: 旧=边分析边落盘 (逐股持久化), 新=全部 ~25 股分析完才进串行落盘段 → 进程在 ~35min 处死亡则整天 DA 批丢失 (旧模式只丢尾部)。总暴露窗 102→40min 已大幅降低概率, 且桥三态告警+TechnicalFallback 兜底存在 → 知情接受。
- **A3 (微, 今晚 pulse 是平盘噪声)**: sector_pulse.json asof 01:18 ET 全 ±0.02% — 今晚首跑注入的盘中脉搏近零信息量 (asof 有标注 LLM 可识别); 明起 pulse 为前一交易日下午数据才有意义, 今晚真载荷是 scanner 日更 rotation。
**结论**: 改动属白名单"修尺子" (接线已有数据+执行顺序, 信号分布不变), 跨域改动已按宪章落档知会; 批准今晚 18:00 首跑, 验收按原四判据+A1 补的采集质量对照执行。

### 2026-07-28 (下午·DA 板块接线+提速批) · Hermes skill 域改动, 已落档知会 (用户裁"按建议执行")
**诊断**: ①DA prompt 要求"宏观→板块→个股"因果链但材料里无板块事实 (LLM 编板块层; 自家 scanner 的 rotation 只在尾部写文件给别人, 个股分析全程没用) ②18:00→19:42=1h42m 系纯串行 for × ~25标的 × (采集1-2min+LLM×2), 架构性慢非浪费。
**改动 (Hermes skill 工作区外, Bash+Python 精确替换, .bak-20260728_cascade 双备份)**:
1. `llm_synth.py`: 材料新增"板块上下文"段 (消费 collected["sector_context"], 无则不展示兼容旧调用); 要求板块层因果引用真实数据、逆板块风自证
2. `pipeline.py`: ①循环前构建板块上下文 (内存 rotation 弱→强全榜 + sector_pulse.json 盘中脉搏 + 每股所属板块) 注入每标的 ②两阶段并发 — 重活 (采集+2次LLM) ThreadPool(3), 落盘副作用 (gate jsonl/ic_writer sqlite/obsidian) 留主线程串行 = 零并发写风险; 预期 1h42m→~40min, 质量零变化 (同模型同 prompt 同材料)
**验证**: py_compile+AST ✓ / 板块段注入+无键兼容功能测试 ✓ / data_collector 无模块级可变状态 (线程安全) ✓ / 循环体无 data 残留引用 ✓
**验收 (今晚 18:00 首跑)**: ①完成时间应 ~18:45 (原 19:42) ②stderr 含"深度分析完成 N/M"并发日志 ③抽查 analysis 的板块层因果是否引用真实数字 ④方向分布 (对照昨日 12/12 LONG)。失败回滚: 两个 .bak 文件恢复。
**宪章注记**: DA skill 属另一模型域 — 本批改动机械性 (接线+执行顺序), 不动 prompt 决策逻辑与模型选择; 已在此落档知会, 另一模型有异议可回滚重做。

### 2026-07-28 (下午·板块级联批) · 07-27 大跌日复盘处方: 板块维度"接线" + 盘中脉搏上线 (用户裁"要动")
**背景**: 07-27 芯片/存储大跌日系统满舵做多 (DA 12/12 LONG, UE 给 TSM BUY -95); 查证发现**板块数据早就存在但没人吃**: deep_sector_rotation.json 每日盘前生成、当天明确标 Technology -1.44% 最弱, 仓内零消费者; UE"市场佐证"只读周更机械影子 capture。缺的不是采集是接线。
**三件套落地 (信号侧, 门控可回滚, 零交易链路变更)**:
1. **数据源接线**: `_gather_market_corroboration` 主源升级为日更 deep_sector_rotation (带 sector_changes 数字), 周更 capture 降 fallback
2. **UE prompt v2.3 板块级联条款** (第9条): 事件涉板块龙头/产业链必须评估同板块传导 (弱势标的 SHORT 侧+受益替代品 LONG 侧); 轮出板块可作级联 SHORT 的 evidence_ref (MARKET_CORROBORATED); 逆板块风 LONG 须自证; 级联 SHORT 仍受第8条高门槛
3. **盘中板块脉搏** `scripts/sector_pulse.py`: 11 SPDR + SMH 实时涨跌 → sector_pulse.json (原子写, <6 成功不覆盖); cron 双段窗 (22-23×1-5 + 0-3×2-6, CST×ET 纪律); 上线即有消费者 (UE 材料"盘中板块脉搏"行) — 防"没人吃"重蹈覆辙
**期望管理 (已向用户说明)**: 抓的是事件驱动板块行情; 纯资金面无声轮动仍在新闻系统能力外。盘中即时交易级消费 (#3 盘中重推理) 未裁决, pulse 先积数据。
**验收**: 今晚 21:10 UE 扫描材料应含"轮出板块(带数字)"; SHORT 产出占比与级联质量跟踪一周后评估 #2/#3 加码。

### 2026-07-28 (午·保本死亡谷 shadow) · 参数级体检揭出捕获率 11% 死亡谷 → A/B 双变体 shadow 上线 (用户裁"要")
**体检发现 (修真后 104 笔)**: 入场时点嫌疑排除 (开盘窗 MFE 最高 0.85%, 与 L2 时点衰减一致); 真问题 = 退出"死亡谷": SL_BREAKEVEN 26 笔均浮盈 0.96% 只捕获 **11%** (vs TRAIL_LOCK 55%) — 浮盈 0.5-1.2% 的仓在保本锚上被回踩杀。价值估算: 捕获率提至 40-55% ≈ +$120-155/周 (当周亏损的 15-20%); 但保本是双刃剑 (07-23 崩盘日救过仓) → 只能 shadow 不能拍脑袋。
**shadow 双变体** (`pipeline` 规则1 附近, record-only → `data/breakeven_shadow.jsonl`):
- **A (触发线 0.5→0.8%)**: SL_BREAKEVEN 出场时 MFE<0.8% → 记快照 (A 方案下不会在此出场), 最终反事实由离线回放补
- **B (锚+0.15% 缓冲)**: B 锚更高更早触发 — 真实未出但 B 锚被碰时记反事实出场点, 真实仓继续持有可直接对比
- **口径修正 (裁决时必读)**: 现状 fee_cushion 已=0.15% (保本锚=entry+0.15%), 死亡谷 11% 捕获率是在此锚下发生; B 方案实测的是总缓冲 **0.30%**
- 防重标记 _be_shadow_a/_b; A/B 双场景功能级验证 (真实决策零变化) ✓; 自查揭出 B 块 _be_line 作用域 NameError 隐患并修 (静默失败教训第二次应用)
**判据**: 与高 ATR shadow 同批, n≥10 后 08-07 裁决; A 的离线回放补算工具挂账 (数据先积)。

### 2026-07-28 (晚·不变量拉网+制度化自检) · 用户拷问"为什么修尺子问题都是我发现" → 主动拉网出 4 类遗留 (含自引) + 每日机器对账上线
**方法论回答**: 已修问题事后看全部违反某个跨字段物理不变量 (盈亏≠价差/bar峰<成交价/负MFE/时间乱序), 但无人每天检查 — 用户看"钱对不对"这个终极不变量所以总先发现。处方 = 不变量固化成每日机器对账。
**拉网实锤 4 类 (全修)**:
1. **🔴 自引 (最痛)**: 昨晚回填 recompute 把 peak/trough 按物理高/低价理解, 库内语义是"最有利/最不利" (**坑库既有经验未查** — 用户批评的实证) → SHORT 21 笔 mfe/mae 反向 + LONG 6 笔半更新 (trough 缺时连 mfe 也跳) + 16 笔微负需 clamp → recompute 修正 (方向无关统一式+条件拆开+clamp≥0), 55 笔重算, 全库负 mfe/mae 归零; 备份 bak_mfefix_*
2. entry_time 写成 CST 旧账 7 笔 (三笔带 +08:00 实锤) → CST→ET 修复 7/7 (全库余 16 笔为 6月前旧账, 判分窗外不动)
3. LLY peak 追踪停在出场前一刻 (30s 轮询粒度) 1 笔 → max(peak,exit) 修正
4. entry_session 恒 'regular' — 定性合法常量 (PLAYBOOK 只在盘中开仓), 非退化, 不动
**制度化**: `scripts/daily_invariant_check.py` (I1-I7 物理不变量 + 字段健康度) 每交易日盘后 cron 05:05 跑, 违反→ERROR→error_alert 推钉钉; 首跑即抓住 LLY 残留验证闭环; 终验全绿 (72 笔样本)。

### 2026-07-28 (早·三链复盘审计执行批) · 推送去重 + 双 CST 修复 (用户裁"按建议执行")
**三链审计结论**: 链1 (进程内 orchestrator, 唯一有 Issue 自动修复闭环) / 链2 (Hermes 04:00 综合报告, OKR+告警独有) / 链3 (Hermes 05:00, feedback_loop.py 零 LLM, **DA 记忆闭环=信号链上游**) — 三链骨架各有不可替代性, 不砍。
**诚实修正**: 审计初报称链2 有"归因辩论"与链1 重叠 — 读完整 prompt 后证伪 (Step 2 是方向归因脚本非 LLM 辩论), 方案 A 原样撤回。
**已执行**: ①B: 链3 [SILENT] 化 — 记忆更新照跑, 不再重复推报告 (失败时仍推错误摘要); 早晨飞书 3→2 份 ②链2 Step 4 ShadowTracker `date.today()` CST 日期 bug (家族第八例): 04:00 CST 跑时 CST 已跨日但 ET 交易日还是昨天 → 一直 track 无指令的"明天" — 改 now_et()。均经 hermes cron edit 写回并断言验证。
**链1 同批早先已修** (f89655af/218a26f5): 重启双跑幂等持久化 + orchestrator CST 缺省日期×6 (07-28 空复盘脏文件真凶)。
**挂账**: 链1 的 3 次辩论调用不在 llm_audit.db (独立 provider 路径, 预算守卫盲区)。
**验收**: 明晨 04:00 链2 报告 Shadow tracking 日期应为 ET 交易日; 05:00 链3 飞书应静默; 16:00 ET 链1 单轮单份。

### 2026-07-28 (凌晨·首夜复盘修复批) · 高 ATR 退出档 shadow + 归因块带回 (用户裁"按建议执行", 当时已无持仓)
**背景**: 07-27 首夜 10 笔全平, 胜率 80% 但净 -118 (8 小胜 +58 vs TSM/GS 两笔 MFE_STALL 大亏 -176, 盈亏比 1:12)。两笔共性: ATR 5% 高波动 + 从未派过 (MFE≈0.07%) + 享 -5% 宽 SL + 150min 慢停损 — 正是 L2 回测 hi4.0 翻案处方缺失的代价。
**#1 高 ATR 退出档 shadow** (`pipeline._check_playbook_exit_rules` 规则 3.4 前, record-only): ATR≥4% 且 held≥60min 且 MFE<0.3% 且水下 → 反事实退出点写 `data/high_atr_stall_shadow.jsonl` (防重标记 `_high_atr_stall_shadow`)。零行为影响; n≥5 后 08-07 裁决点定转 live。功能级验证: 触发+落盘+防重+不改真实决策 ✓。插曲: 首版用了 pipeline 不存在的 now_et 会被 try/except 吞成永久静默 — 自检揭出并修 (函数内 import), "静默失败路径自己先过尺子"再次应验。
**#2 归因块** (`playbook_service` 记账 metadata): 新增 `attribution` 块 (signal_source/timing/vol_bucket/atr_pct/decision_id) — 单表即可切 UE/DA/topup, 不再跨表 join。侦察修正复盘误判: decision_id 列其实 10/10 有值 (复盘时查错了地方—查了 metadata), 指令表也已有 vol_bucket/atr_pct 列; 真缺口仅为 metadata 自证块。
**验证**: 980 passed / 重启冒烟 7/7 / shadow 测试行已清。下一步: 明日盘前窗 topup 新桶日志定 topup 零产出真因; shadow 等高 ATR 样本。

### 2026-07-27 (深夜·topup 零产出定性) · 用户问"topup 能否正常下单" → 实锤"上线以来全史零补出" + 双盲区补丁, 根因待明日盘中诊断
**用户问**: topup 是否可正常下单。**定性**: 机制层全部正常 (窗口如期 5 次/日、质量门过 18-20 只、分窗幂等、D2 退避均工作); 但 **topup 来源指令全史零条** — 不是今日市况, 是结构性零产出 ("线路从未通电"第二例)。
**证据链**: ①TechnicalFallback 方向产出日志全史 0 次 (vote 从未成功出方向) ②过滤审计 skips=18 但 breakdown 全 0 — 双盲区实锤: `vote_result is None` 分支无审计桶 + breakdown 硬编码清单缺 vote_none/technical_no_signal 两桶 ③vote() 内 eval 异常被 debug 级吞 (静默失败)。
**本批补丁 (可观测, 零行为变更)**: eval 异常 debug→warning / vote_none 审计桶补齐 / breakdown 清单补 vote_none+tech_no_sig 两桶。980 passed + 重启冒烟 7/7。
**待诊 (明日盘中自动出数据)**: 新日志会区分三种真因 — ①eval 异常 (WARNING 可见) ②ctx 指标字段缺失致 momentum 恒 NEUTRAL ③策略门槛真拦 (回调质量门)。待真因落地后再修本体。同时注意: 盘前窗 topup 时 DA bridge 已在 (今日 12 条 DA), 但 topup 强制用 TechnicalFallbackAdapter — 设计如此 (池补量用确定性方向非 LLM)。

### 2026-07-27 (周一·仓外调度面 CST×ET 错位全普查) · Hermes 11 + crontab 28 条逐个判定 → 实锤 1 处 (S4 自身) 已修 (用户裁"需要")
**背景**: 盘中诊断 job 错位窗是家族第五例后, 用户批准对仓外调度面 (Hermes jobs + 系统 crontab) 全量按 ET 口径过一遍。
**判定结果**:
- **✅ 自洽 37/39**: 盘后复盘/回填类全在 CST 凌晨 `2-6` (=ET 周一至五收盘后) ✓; 盘前类 `1-5` 20:00-21:10 ✓; error_alert 双段窗 (21-23×1-5 + 0-5×2-6) 早已是正确拆法 ✓; universe_expansion_shadow 周一午间抓上周五轮动係"最近交易日"正确语义, 不动。
- **🔴 实锤 1 处 — 讽刺地正是昨日 S4 周末采集自身**: `15 */3 * * 6,0` = CST 周六+周日, 但 **ET 周日下午/晚间 = CST 周一 00:00-15:00 不在窗内** — 周末新闻缺口只堵了 2/3。修: 保留原窗 + 新增 `15 0-15/3 * * 1` (CST 周一凌晨~午后 = ET 周日全天), 周末覆盖至此完整 (ET 周五晚~周日深夜); 备份 crontab.backup.20260727_*。Q4 判据不变。
**方法论**: 修尺子的修复自己也要过尺子 — S4 是昨日堵"周末缺口"时建的, 建时用了 CST 周末直觉而非 ET 周末定义。仓外调度面至此与仓内代码面同标准清零。

### 2026-07-27 (周一·Hermes 盘中诊断 job 错位窗收窄) · 用户查出凌晨分钟级 LLM 调用 → CST×ET 错位窗实锤并拆窗修复
**用户疑问**: 07-27 凌晨 00:01/00:30/04:01 仍有 Hermes Agent 循环调用大模型, 是否必要。
**定性**: 实锤 = `ftseek-intraday-diagnose` (36cc015242ee, 05-18 建, qwen3.7-max agent 每 30min 推飞书盘中诊断)。盘中时段有真实价值 (非空转); 但窗口 `*/30 21-23,0-4 * * 1-6` 含 **CST×ET 错位段** (position_sync 同款家族第五例): CST 周一 0-4 = ET 周日下午、CST 周六 21-23 = ET 周六上午, 每周 ~14 次纯空转; 且 prompt 内 Step 0 交易日守卫是 "LLM 先花钱再发现不用干活" — 违反 07-25 军规"守卫须在 LLM 之前"。
**处置**: 拆窗修复 (备份 jobs.json.bak_20260727_diagnose_window) — 原 job 收窄 `*/30 21-23 * * 1-5` + 新建 `ftseek-intraday-diagnose-late` (5513c8e94b06) `*/30 0-4 * * 2-6`, 盘中覆盖不变、每周 ~14 次错位空转归零。
**挂账**: 节假日仍靠 prompt 内守卫 (每假日 ~16 次守卫调用) — 发现 hermes 原生支持 `--script --no-agent` (零 LLM 守卫形态, 正是军规写的 script 门控), 建议下轮把交易日检查迁 script 前置; 及彻底去 LLM 化 (纯脚本诊断+推送) 候选。

### 2026-07-27 (周一·信息饮食增强批) · C1 finnhub + C2 13D/G 举牌 + P4a 补缺 (用户裁"提升标的/指令质量优先完成")
**裁决口径**: 信号侧=修尺子不影响交易链路, 提质量项优先。新源按分层原则接入 (探索层可插拔/门控/独立回滚), 判分走 S0→S1 同一闭环。详表 DEB-20260727 §十一。
1. **C1 finnhub 市场新闻进 UE 信息饮食** (已付费闲置能力): `_gather_news` 同构并入 (渠道标 finnhub_market), 真跑实证周末 RSS 停更窗被补 20 条。回滚 `clue_finnhub_news:false`。
2. **C2 SC 13D/G 举牌直连** (常先于新闻的强 HARD_EVENT): 取 Subject 方零猜测, 13D 带看多先验标注; 周日 EDGAR 合法空, title 格式假设今晚首交易时段核验。回滚 `clue_13dg_events:false`。
3. **P4a 词典补缺**: 台账 78 标的全覆盖 (补 23) + 新歧义词 E/NU/CME 守卫, 断言全过。
4. **C3 官方源路由如实跳过** (公共实例不支持, 留自建 docker 时再议)。
**验证**: py_compile 4 / 端到端 brief 实跑 (健康行新增 8-K/13DG 位) / 重启冒烟 7/7。今晚 21:10 UE 扫描起三新渠道正式供给, 2 周后 Q1 与存量 5 渠道同场判分。

### 2026-07-27 (周一·时区同款漏网全仓扫尾) · 六类反模式拉网 → 8 处真问题统一修复 (用户令"统一修复")
**背景**: position_sync CST×ET 错位后用户要求全面复查。按六类反模式拉网 (本地 weekday 判周末 / SQL date('now')=UTC / date.today()=CST / 本地 hour 判时段 / utcnow 残留 / naive 日期串), 候选 30+ 处逐个定性: **8 处真问题修复, 其余误报 (已有 ET 主路径/输入已是 ET/审计戳自洽)**。
**修复清单 (按危害排序)**:
1. **holiday_calendar.is_trading_day 缺省参数** (最高危): date.today()=CST → CST 周一凌晨把 ET 周日判成"交易日", 所有无参调用方中招 → 缺省改 ET。**冒烟黄金证据: 修前此刻 (ET 周日) 会返 True, 修后 False** ✓
2. **earnings_guard 财报窗口** ×2: date.today()=CST 提前 12h 进/出窗口, 边界日误拦/误放 → ET
3. **thesis_inferer 兜底 UPDATE**: SQL date('now')=UTC vs trade_date 列=ET → ET 20:00 后 thesis 更新静默丢失 → get_trade_date() 参数
4. **postmarket_review**: date(exit_time)=date('now') UTC 错位 → ET 参数
5. **trading_monitor**: UTC weekday + 硬编码 UTC13:30-20:00 窗口 (冬令时漂 1h) + 残留 utc_now-4h 硬编码 → 全改 ET
6. **rejection_logger 双向错位**: 写 CST naive × 读 date('now')=UTC → 写读统一 ET (1 写 + 5 查)
7. **signal_stage fast_lane 日界**: CST 日界在 ET 盘中 12:00 清去重集 → ET
8. **扫尾**: review_orchestrator 教训过期 date('now') → ET 参数; exit_shadow_graduation utcnow() → SSOT now_utc
**明确不动 (定性合理)**: 建表 DEFAULT datetime('now') (审计戳 UTC 自洽); pdt_guard/shadow_tracker/playbook 等 weekday (输入已 ET); try-except CST 兜底行 (降级路径); obsidian/reports 展示类; option_risk_monitor (模块已停用+偏差方向保守)。
**验证**: py_compile 9 文件 / is_trading_day 无参≡显式 ET 断言 / 980 passed / 重启冒烟 7/7。回滚: 各文件独立 revert。

### 2026-07-27 (周一·信息渠道二轮复核补丁) · S0 歧义词守卫 + 基数自检军规 (用户裁"需要", 零交易行为变更)
辩论方对执行批 (commit 3b618b77) 逐行复测, 发现并修复一处准确性漏洞, 教训固化:
1. **S0 歧义词守卫补齐** (`llm_universe_scan.py`): 渠道归因的标题兜底 `\bSYM\b` 漏了 P4a 的歧义词排除 — F/T/V/MA 等歧义 ticker 会被标题里任意独立字母误归因, 污染 S1 渠道切片。修法: 歧义 ticker 只信 symbols 标注, 标题兜底仅留非歧义。3/3 断言通过。
2. **基数自检军规** (AGENT.md 军规第 6 条 + scorecard 落地): "常量退化"已三例 (DA conf 35 / UE conf 50 / info_source 恒常量) — 新增落库字段必带 distinct 计数体检 (n≥10 且 distinct<2 → WARNING)。实跑 scorecard 不误报 ✓。
**分层原则备忘**: 测量层准确性优先 (宁可 none_matched), 探索层灵活性优先 (可插拔/降级/回滚), 预注册判据做单向阀门。详见 DEB-20260727 §八。
**回滚**: git revert 两文件各自独立。

### 2026-07-27 (周一·position_sync 周末空转修复) · 用户查日志揪出 2 真 bug + 1 措辞 (86 次/日解锁循环)
**用户疑问**: 周末日志反复"实盘交易已解锁"循环是否合理。诊断实锤三件事:
1. **Bug① 时区批同款漏网** (`position_sync._get_market_phase`): 周末判定用 CST weekday × 时段判定用 ET 时刻 — CST 周一凌晨=ET 周日下午的 12h 错位窗内, ET 周日被误判 TRADING(30s)/AFTER(5min) → 周日白天白跑 84 轮。修复: weekday 统一 ET 口径 (ET 周六/日→CLOSED), 节假日检查同步用 ET date, 删旧 CST 周一凌晨特判。冒烟: 当前 ET 周日晚正确判 CLOSED ✓, 重启后日志 "sync paused" ✓。
2. **Bug② 自咬尾巴死循环** (空仓无缓存分支): `_reset_futu_ctx` 断连接**并清零空查询计数器** → "连续3次确认真空"永不可达 (86 条全"连续1次"实锤) → 每轮重建连接+重新 unlock REAL。修复: 查询成功返回空 = 连接健康, 不再 reset, 计数器递增至确认真空。验证: 重启后首现"连续2次" ✓。unlock REAL 本身 = 06-25 三态融合刻意设计 (sim 下查 REAL 持仓防真金脱管), 保留。
3. **措辞**: "✅ 实盘交易已解锁" → "REAL 持仓查询权限已解锁 (只读对账用...)" 消惊悚。
**连带修复**: ①`test_trade_recorder` 时间敏感假阳性 (UTC date('now') 查询 vs ET get_trade_date 落库, ET 20:00 后跨日必失败 — 测试大修密封化漏网) → 查询口径对齐 SSOT, 980 全绿 ②启动冒烟脚本假阳性窗 (restart 后新段未写出时旧段全绿放行 — 本次实战暴露) → --wait 模式拒绝早于重启 120s 的旧段。
**回滚**: git revert 单文件各自独立。

### 2026-07-27 (周一·信息渠道修工具批) · S0/P4a/S1接线/S2/S3/S4 六项落地 (用户裁"要执行", 零交易行为变更)
**背景**: DEB-20260727 辩论方复核 (§六) 发现两处"信息在管道里被抹掉"的漏点 — 新闻标的标注仅 11% / origin.info_source 退化为常量 (第三例)。修工具六项全落地, 详表见 DEB §七。
1. **P4a 标的映射器** (`rsshub_us_news.py`): ~140 标的词典 + 歧义词防误报 + 覆盖率仪表 — **近14天真实新闻重算 10%→21%**。标的精准度第一杠杆。
2. **S0 渠道归因** (`llm_universe_scan.py`): origin 落 `info_channel`(首报渠道)+`first_news_id` 机械归因, 替代常量。
3. **S1 接线** (`direction_hit_scorecard.py`): info_channel×fwd 7 渠道切片, 2 周后出首份渠道判分 (Q1)。
4. **S2 消单点** (yaml): rsshub instances 1→3 (回退代码早已在, 纯配置)。
5. **S3 EDGAR 8-K 直连** (新 `app/data/sec_8k_provider.py` + brief 接入 `clue_8k_events`): 分钟级官方一手源 (RSS 链实测 p50=74min), item 白名单 11 类**含方向先验标注** (4.02重述=强利空等), CIK→ticker 零猜测。真拉验证 96h 窗 65 条 ✓, 周一盘前批首实战。
6. **S4 周末采集缺口** (crontab): 周末每 3h 低频采集, 堵"重磅事件周一前被 RSS 滚动窗冲掉"。
**回滚**: 各项独立 (见 DEB §七); M1 事件驱动/M2 付费源维持待裁。

### 2026-07-27 (周一·信息渠道 DEB 起案) · 时效/独家/反馈响应补强方案留档 (用户令"要")
**疑点结案**: news_us.db 停更于周六 06:02 = **相位门控设计** (scheduler_core.py:2274 OVERNIGHT/WEEKEND/盘中全跳过) 非故障; 但暴露真缺口 — 周末 0 采集 + RSS feed 滚动 = 周末重磅事件可能周一前被冲掉 (S4 补扫方案)。
**DEB**: `docs/debates/DEB-20260727-info-channel-timeliness.md` — E1-E8 实查 (单点 RSSHub 免费实例/标题级粒度/来源判分缺失/决策→执行 15h 才是时延大头); 修尺子 S1-S4 (来源级判分/消单点/EDGAR 8-K 直连/周末补扫) 可先行, M1 事件驱动/M2 付费源带 Q1-Q4 预注册判据待裁; 核心论点 = UE 29% 命中下渠道是放大器不是发动机, M3 决策提速 > M1 事件驱动 > M2 花钱买数据。零代码变更, 交另一模型辩论。

### 2026-07-26 (周日夜·回测体系 L2/L3 落地) · 入场门回放 + 跨期稳健性首跑 — ATR 甜蜜区被降级为经验参数 (用户批"要", 只读零 live)
**是什么**: 用户命题"拉历史数据+成交历史构建回测体系"。分层落地: L2 `entry_gate_replay.py` (308 方向指令含未成交 × 1min bar, 补上路径2缺口) + L3 `historical_robustness_check.py` (78 标的 × 5 年日K)。**L4 边界写死**: LLM 信号端到端历史回测永久不可行 (无新闻快照/prompt漂移/前视), 提案一律引用 DEB §L4 驳回。
**L2 首跑 (n=307, bar 覆盖 307/308, ATR 回算补齐 212 无前视)**:
- 入场时点单调衰减: 09:45-10:00 甜区 (均值 0.12%/胜率 0.56) → 11:00 期望砍半 (0.06%) — topup 盘中窗吃的是下半衰期, 判分须与盘前批分口径 (定量锚首次落地)。
- ATR 门: **lo3.0 拦对 (第三重确认, Δ+0.49%)**; 但 **hi4.0 上限门指令层"拦错"翻案** — ≥4% 桶裸持有正期望, 交易层亏损疑似退出工程失配 (SL 被高波打穿) 而非信号差 → 处方方向改"高ATR退出档"而非拦入场, 待路径1高ATR切片交叉验证。
**L3 首跑 (78/78 零失败, 判据❌不通过)**: 3-4% 桶动量跟随仅 1/6 年优于 2.5-3% 桶 → **ATR 甜蜜区非市场结构规律, 是"LLM信号质量×波动环境"交互 (仅在本系统信号分布上三重确认有效)。按预注册判据降级: 经验参数需每月复检, 08-29 终裁不得引用为结构规律。** 但机会规模分层跨 6 年稳定 (波幅 1.8/2.5/3.0/5.4%) 结构性成立。
**预注册判据**: L2-Q1 (lo3.0 升 live 三条件) / L2-Q2 (hi4.0 翻案待路径1交叉) / L2-Q3 (时点甜区 n≥600 复跑再议) / L3-Q1 (已裁: 降级) — 见 `docs/debates/DEB-20260726-backtest-l2-l3.md`。
**维护**: 无 cron (避免再造后台任务), 建议周五盘后随 exit_scorecard 手动复跑 L2; 缓存复用重跑零 API 增量。

### 2026-07-26 (周日夜·GitHub 异地容灾接线) · 首次推送+历史清洗+每夜备份自动化 (用户授权配置)
1. **历史清洗推送**: 本地 510 提交经 git-filter-repo 剥离 .env*/node_modules (531MB→49MB, 密钥从全部历史剥净) — 与另一模型独立清洗结果逐字节一致互为验证。远端 master/main/rfc-060 = 清洗版; 港股旧代码 (5/26, 172 提交, 本地原本没有) 全量保全至 `archive/legacy-*` 三分支, 零丢失。
2. **每夜备份自动化**: `scripts/git_backup_push.sh` (cron 03:30) — ①全分支推送 (已提交工作异地容灾) ②git plumbing 临时索引做运行态快照强推 `backup/runtime-snapshot` (未提交的 data/config 变更也有异地副本, 不触碰工作区/主索引, 服务零感知), 失败钉钉告警。端到端实测: 快照 b2f4fc8f 已上 GitHub; 曾撞 GitHub 100MB 限被拒 → 补 node_modules gitignore 后通过。
3. **gitignore 封堵**: `.backup_premigration/` (含 env 明文副本+旧历史完整镜像, 本地保险, 30 天后可清) / node_modules/ / tmp_e2e_out.txt。
4. **连带生效**: CI (980 门禁) 自下次 push 起真正运行; 本地 master 已切清洗版并跟踪远端。
5. **分支治理收尾 (用户裁"要")**: 删远端 `archive/legacy-main` (空壳 Initial commit) + `archive/legacy-ftseek-us` (4/28 早期美股版, 已被现历史覆盖; 本地 rewrite.git oldgh/* 仍留副本可反悔)。**最终 5 分支**: `rfc-060-phase1-hotfixes`(工作真源) / `master`+`main`(稳定基线) / `backup/runtime-snapshot`(每夜快照) / `archive/legacy-hk-master`(港股 172 提交唯一副本, 保留)。心智模型: 只看 rfc 和 master, 其余是保险柜。
**建议遗留**: ①GitHub 仓库已确认 Private (用户) ②.env.us 密钥曾在本地历史数月, 择机轮换 BAILIAN/DASHSCOPE key (低风险高收益) ③GitHub 网页 Settings → Default branch 改为 rfc-060-phase1-hotfixes (仅用户可操作)。

### 2026-07-26 (周日晚·回归验证接线批) · 启动冒烟脚本 + CI 补面 + 重启 SOP 军规 (用户裁"需要")
**背景**: base.* 事故实锤回归体系缺口 — 980 测试全绿 + 人工重启 grep 都没拦住 ConfigGov 降级运行; 且 ci.yml 只监 master/main, 当前工作分支 rfc-060 本周 10+ commit CI 一次没跑。一次性接线, 之后零维护, 主战场回策略。
1. **`scripts/startup_smoke_check.py`** (新建): 重启后七条机器断言 (ConfigValidator 全过/漂移检测/热重载/SSOT 审计 0 BLOCKING/模拟盘 TRADE_ENV=1/退出快循环/零 Traceback), 以最后 [PreFlight] 为启动段界, 启动段过旧(>6h)判 exit=2; `--wait N` 轮询适配启动耗时, `--push` 失败飞书告警。**验证**: 正例当前启动段 7/7 ✓; 负例用 17:37 故障日志样式合成复现 — 若当时有此脚本会抓住 5 项失败; 过旧启动段 exit=2 ✓。
2. **ci.yml 补面**: 触发分支加 `rfc-*` (消"CI 从没跑过"盲区); 三子集升级为全量 `tests/` (扫尾批后 980/35s 首次具备当门禁成本)。注: 未推远端, GitHub runner 上首跑效果待 push 后观察 (requirements 安装 `|| true` 可能缺依赖)。
3. **AGENT.md 军规**: 重启 SOP = restart + smoke `--wait 90`, 禁临场 grep 代替; 删 yaml 段零引用验证须含隐性消费面 (REQUIRED_KEYS/_AUDIT_ITEMS/CRITICAL_KEYS)。
**不做**: 大规模补覆盖率/策略细粒度单测 — 本周末三次事故无一是"缺单测"造成, 且违背"聚焦策略"主方向。

### 2026-07-26 (周日晚·校验清单同步修复) · cleanup-r3 删 yaml 段但漏改校验清单 → 启动 4 假错误+2 假 BLOCKING, 已修并重启验证全绿
**根因**: 70cecbdb (cleanup-r3) 删了 yaml 无消费者段 (`base:`/`stop_loss:`/`take_profit:`), 但 validator REQUIRED_KEYS + config_audit CRITICAL 清单未同步 → 17:37 重启报 4 个 `base.*` 必需键缺失, raise 被外层吃成"降级运行" → **ConfigGov 整块中断 (漂移检测/热重载/审计均未启动)**; 且 Layer 1.7 还埋着 stop_loss/take_profit 2 个假 BLOCKING (被 validator 先挡住未暴露)。
**修复 (尺子同步减法, 零行为变更)**: ① validator REQUIRED_KEYS 移除 4 个 `base.*` 键 ② config_audit 审计项移除 scheduler.min_confidence + stop_loss/take_profit.default_* 3 项 ③ config_defaults 同步删 CRITICAL_KEYS 条目及死常量 SCHEDULER_MIN_CONFIDENCE/DEFAULT_STOP_LOSS_PCT/DEFAULT_TAKE_PROFIT_PCT (全库确认无其他消费者) ④ yaml 删废弃残留键 scheduler.min_confidence (消 DEPRECATED WARNING, 运行时早已走 MinConfidenceResolver)。
**验证**: 18:07 重启 — ConfigValidator ✅全部通过 / 漂移检测+热重载启动 / SSOT 审计 0 BLOCKING / 启动段 0 ERROR 0 Traceback / 模拟盘+Futu 连接正常。
**遗留 (非阻断)**: 审计尚有 3 条 INFO 级警告 `signal_fusion.us.*` 三权重键缺失走 CFG 兜底 — FUSION_* 同样无任何代码消费者, 同款死审计项, 建议下轮减法批一并清。**方法论教训**: 删 yaml 段的零引用验证必须含“校验/审计清单”这个隐性消费面 (validator REQUIRED_KEYS / config_audit _AUDIT_ITEMS / CRITICAL_KEYS)。回滚: git revert 本批。

### 2026-07-26 (周日·扫尾清零批) · 修数据+理逻辑收尾 (用户令"清干净后集中优策略")
1. **悬空键真 bug ×2**: `risk.blacklist`→`global_blacklist` (llm_pipeline 黑名单此前恒空形同虚设) / `main_pipeline.allow_live`→`strategy_budgets.` 真键 (值同 False 零行为变化, yaml 恢复真源)。
2. **实盘闸门 fail-open 反转** (order_executor ~3192): allow_live 检查异常时原逻辑**放行实盘** → 改 fail-closed (real 账户下异常即拒)。
3. **yaml 死段删除 12 段 133 行** (逐段 grep 验证零读取): OPTION_QUALITY_GATE/STRATEGY_ORTHOGONALIZATION/agents/base/debate_mode/debate_planner/llm_voting/logging/signal_fusion/顶层stop_loss+take_profit/trading_hours。yaml 48 顶层键, 解析+容器加载 ✓。
4. **测试大修 (代理执行, 341 工具调用)**: 修复 30 文件 ~95 测试 (断言追平当前语义, 禁弱化) + 删 66 个引用已退役目标的测试 + 密封化 (LLM/网络/wall-clock/env 泄漏全 monkeypatch — 其一 `TRADE_ENV=0` 进程泄漏曾级联炸 ~100 测试)。**164 failed → 4 failed**。
5. **TradeStore P0 双修** (剩余 4 失败全歼): ①全新 DB 引导缺 20 列 (save_trade/upsert 引用但 _init_db 从未建, 生产 DB 靠 ad-hoc ALTER 存活) → 补幂等 ALTER 清单 (=生产 schema 实测 diff) ②upsert row=None 时 row[8]/row[9] 无守卫 TypeError (新标的首次 upsert 必炸路径)。6/6 ✓。
6. **run_llm_backtest 死函数删** + trade_records.json 半截迁移收尾 (getter 删+暂存) + **watchdog cron 加 || 钉钉兜底** (监控者自己崩要有人知道) + **tests/test_no_archive_imports.py** (archive 零活 import 固化为 CI 断言, F1 教训) + 122s 纯睡眠测试修复 + 每次跑测试真调 LLM 的密封漏洞堵住。
7. **测试遗留 2 项已知**: P2 退出阶段非 daemon 线程挂死 (futu network_manager/PushAggregator, 跑测试用 timeout 兜) / P3 `_collect_fundamental` industry="" 通过质量检查 — 均记 DEB 待裁。
**全量测试终跑: 980 passed / 0 failed / 34.9s** (此前: 16 收集 ERROR + ~164 failed + risk 目录纯睡眠 122s + 退出挂死永不结束 → 现在 35 秒全绿, 测试套件首次恢复为可信回归工具)。

### 2026-07-26 (周日·重启验证+开盘就绪) · R2 删除首次重启全绿 + 复核修复/修尺子三项落地 + 周一就绪总检 (用户裁"重启后能修的修, 轻装上阵")
1. **重启验证 (145 项删除后首次)**: 09:00 CST 重启 — 0 Traceback/ImportError, ConfigGov 全绿, 退出快循环+Watchdog 启动, 模拟盘 ✓。R2 删除正式验证安全。
2. **复核修复已提交** (d0e8c759): Hermes 6 脚本移回 + api_limit_monitor 移回+import 修。
3. **修尺子三项**: Q2 price_fail_closed 清空为 [] (机制留, 备份 .bak_20260726) / 5e llm_features 5 隐身键补 yaml 显式化 (值=代码默认零行为变化) / Q6 三高危文件入 data_freshness_watchdog (干跑即捕获 SHORT 门控 2100h 停更×2 — 设计意图达成)。
4. **周一就绪总检全绿**: 三门 ON / topup 30/18+盘中窗 1130 / UE v2(v2.2 prompt)+负反馈+critique / thinking dr_debate / prompt_cache×2 / 主线静默; crontab 22 条无断链; Hermes 断链复查 3/4 为检查正则误报 (属 skill 自带目录)。
5. **新备忘 (既有损坏非 R2 造成)**: 盘后综合复盘 job (7a40dd7c) Step6 引用 `scripts/position_reconcile.py` — 本仓 git 历史从未有过此文件 (幻影引用), agent 该步失败但不阻塞其余; 下次修 job prompt 时删除或指向真实对账方式。
**周一观察清单**: 08:30 首夜报告 / 提量后成交量 (保险丝: 日亏>$150 回滚 25/12) / topup 盘中窗首验 10:30-11:30 ET / UE v2.2 首跑 21:10 (含 P1-P5+A2 全家桶, SHORT 量预期下降是设计意图) / thinking dr_debate 时延-empty率 / 两条 exit shadow 自动采集。

### 2026-07-26 (周末·减法审计R2) · 代码/配置/数据三维废物清算 145 对象 (用户四项全选: T1+T2+T3+DEB)
**审计**: 3 路并行 (scripts 死活/app 模块引用/配置键+data 产消对账), 详见 `docs/debates/DEB-20260726-subtraction-audit-round2.md`。
1. **T1 零风险**: atr_gate_shadow cron 停 (45KB/日零读者); yaml 删死 flag×3+shadow_mode 段+voting_* 三死路由+strategies.orb 死牌子; DEB-20260724 已裁决 3 模块删除。
2. **T2 死代码**: app/ 死模块 **73 文件 ~13,500 行** git rm (三重验证: 模块名+包级导入+类名; 分类器拦截后补强验证模式); 失效测试 16 个删除 (**pytest 收集从 16 ERROR → 1048 全过**); 孤儿脚本 58 个归档 scripts/archive/ (scripts 根 118→60)。4 个 `__init__.py` 断链已修。
3. **T3 隐身放行**: **SHORT 门控 88 天空文件静默 fail-open** (hard_to_borrow/short_interest) → 加 stale WARNING (实测触发, 行为零变化); 悬空配置键补齐 (risk.portfolio 回撤三键+max_per_sector, 值=代码默认零变化, yaml 恢复真源); event_log (常驻线程+零读者) 与 position_snapshot (每小时×3 取价+零读者) 停写。
**遗留裁决 (DEB §五)**: SHORT 门控数据源接线 vs 废弃 / price_fail_closed 65 天冻结 / api_limit_state 仓外生产者 / 推送 7 套+regime 7 套框架收敛 (留 08-29 后) / data 产消注册表进 watchdog。
**回滚**: 全部 git revert 级; crontab 备份 data/crontab.backup.20260726_*.txt。

### 2026-07-25 (周末·浪费清扫四项批) · 全项目定时任务审计落地 (用户四项全选)
**审计**: 3 路并行扫描 (Hermes cron / 系统 crontab / 仓内任务) — 仓内干净 (全部 LLM 已审计 ≤$1.10/天), 浪费集中在 Hermes+crontab。
1. **Hermes 重复任务暂停** ✅: `895169e95c50` ftseek-eod-feedback-loop 与孪生任务跑同一 `feedback_loop.py` 仅晚 30min (~30万 token/次重复) + 周一复盘周日数据 → `hermes cron pause` (可逆)。
2. **两个每周必崩脚本修复** ✅: ①`ic_weekly_report.py` ic_mean=None 时 `f"{None:.4f}"` TypeError → `_fnum` 守卫 + by_strategy/direction 空值防护, 实测跑通并推飞书 ②crontab L20 `symbol_performance_report.py` 补 `PYTHONPATH=.` (兄弟条目均有), 实测跑通。crontab 备份 `data/crontab.backup.20260725_225836.txt`。
3. **僵尸任务批清理** ✅: crontab 注释 4 条 (带日期+原因+恢复条件): `update_signal_lifecycle` (无 live 交易 2 周+, **转 LIVE 须恢复**) / `backfill_voting_outcomes_v2` (卡死行永真) / `factor_exit_pnl` (死消费者) / `time_exit_counterfactual` (无 TIME_EXIT 记录)。main.py Task#36 死 import 前批已移除。`/etc/cron.d/ftseek-tasks` L7 (*/5 curl :8901/health, 与 health_check.sh 重复) 已由用户手动注释 ✅ (备份 `ftseek-tasks.bak_20260725_zombie`, 已验证)。
4. **combined_monitor 停用** ✅: 两条 cron 注释 — 39 次/日检出 84 告警但**零推送代码** (检出即终点); 覆盖已由 error_alert.sh (ERROR 计数+钉钉) + intraday_heartbeat (静默失败检测+推送) 承担。

### 2026-07-25 (周末·LLM 成本堵漏) · Hermes 空转 cron 停用 (用户查账发现, 直接拍板执行)
**发现**: Hermes cron `81002dff9b1d` "FTSeek-US Agent 请求处理" `*/5 * * * *` 24/7 无窗轮询 `~/.hermes/ftseek_requests/` 队列 — 每发 2 次 qwen3.7-max 调用 × 38,370 输入 token (~$0.61/次), 产出 8 字符 `[SILENT]`。**~$177/天, 04-25 创建以来 24,487 次 ≈ $1.5 万量级**, 全部在仓外 (不进 llm_audit.db / 不受 $5/天预算守卫)。
**必要性核查 (结论: 无存在必要)**: ①仓内+`/opt/ftseek` 全局 grep 零生产者 (无任何代码写该队列) ②留存 50/50 次输出全是 [SILENT] ③队列目录 05-26 起为空 — 生产者早已退役, 消费者空转 3 个月。
**动作**: `hermes cron pause 81002dff9b1d` (可逆, 备份 `jobs.json.bak_20260725_disable_reqpoll`)。其余 enabled cron 不动 (盘中诊断等均有正确交易时段窗)。
**复活条件**: 若未来重建队列生产者, 须用 shell 门控形态 (script job: 空队列 0 成本退出, 非空才唤起 agent), 禁止恢复 LLM 直轮询。累计浪费金额计入 8 月复盘账本。

### 2026-07-25 (周末·P5/A2 落地) · SHORT 高门槛 + UE self-critique shadow (用户裁"提质量且不改交易流程可执行")
**双条件过滤裁决**: P5 ✅ (输出字段加法, SHORT 照常入库执行) / A2 ✅ (shadow 形态, 注入集合零变化) / **A1 ❌ 不动** (改 conf=改执行排序, 继续攒 n≥100) / **A4 ⚠️ 拆分** (字段已被 horizon_hours 覆盖; price-in 复核要挂盘前主循环侵入面大, 列下周)。
1. **P5-UE** (`llm_universe_scan.py` v2.2 规则8): SHORT 强制 short_check{borrow_feasible/downside_catalyst_expiry_hours/squeeze_risk} + "无具体下跌催化剂不得浮现"; normalize 解析含 provided 标记 (模型没交作业可切片) → inject 落 origin。
2. **P5-DA** (skill `llm_synth.py`, 备份 .bak_20260725_shortgate): 主分析师 prompt 补 SHORT 三问 (答不全改 SKIP) + 挑战者补 SHORT 加严审查项 (缺问标 missed_risks)。DA 是重灾区 (分歧SHORT 0%命中)。
3. **A2 self-critique shadow** (`llm_universe_scan.py` cmd_scan): surfaced 后同模型二次调用逐条攻击因果链最弱环, verdict(KEEP/DOWNGRADE/DROP) 只落库不拦截; 回滚 `universe_expansion.self_critique=false`。判分路径: scorecard 新增 critique 切片 → DROP 组若显著更差再裁升级实拦。
**端到端验证亮点**: critique 真调用中主动引用 P5 规则 DROP 了无催化剂 SHORT 桩 ("违反 SHORT 高门槛强制约束...") — 两新规则互锁。P5 冒烟 3/3 ✓; py_compile ✓。
**验证路径**: 周一首跑后 scorecard 看 short_check.provided 率/critique 分布; SHORT 浮现量预期下降 (设计意图: 宁缺毋滥)。

### 2026-07-25 (周末·UE v2.2 修尺子批落地) · PE 优化 P1-P4+E1 全部执行 (用户裁"明确的现在就做, 下周开盘验证")
**范围**: DEB-da-ue-pe-agent-optimization 修尺子先行五项, 全部 UE 侧 (`llm_universe_scan.py` → **PROMPT_VERSION v2.2**) + scorecard 读侧。零执行行为变更 (新字段纯落库, 排序/门禁不动)。
1. **P1 可证伪字段**: 非 SKIP 标的强制输出 invalidation/horizon_hours/expected_move_pct → normalize 解析 + inject 落 origin。
2. **P2 rubric 化**: evidence_checklist 四项 bool (催化具体/24h内/量价印证/非拥挤) → **rubric_score 代码侧计算** (校准从模型移到代码), 与 conf 并列落库供 Q2 相关性对比。
3. **P3 负反馈注入**: `_build_feedback_table()` 查本源近7日浮现→fwd 结果小表注入 brief + "连错同标的同方向须新增量证据"规则 — 验证时表内已含 GOOG SHORT 连两日 (-6.9%✗) 实弹。回滚: `universe_expansion.clue_feedback=false`。
4. **P4 信息饮食**: 每条新闻带时效小时数 (RFC2822 解析修复后 20/20 生效); premarket_movers 注入**放弃** (数据陈旧 06-26, 防腐守卫教训)。
5. **E1 切片**: scorecard 新增 prompt_ver (v2.1/v2.2) + rubric (≥3/≤2) 两组 — v2.2 效果自动 A/B 可判。
**端到端验证**: 真 LLM 调用 (v2.2 prompt + 真实 brief) → 浮现 7 标的 **v2.2 字段完整率 7/7** (含合理 invalidation 如 'XLI转跌或LMT放量跌破日内均线'); 冒烟 3 场景 ✓; py_compile ✓。
**验证路径 (下周)**: 周一 21:10 cron 首跑 v2.2 → 每日 scorecard 看 prompt_ver/rubric 切片 → n≥30 后按预注册 Q1/Q2/Q4 判 (重复推荐占比/rubric vs conf 相关性/命中是否脱离 29% 负 edge)。
**未动 (按分级)**: P5 SHORT 高门槛/A1 分歧奖励反转/A2 self-critique/A4 时效复核 — 待裁决或等样本; A3 裁判分离存储 (skill 侧) 排下周。

### 2026-07-25 (周末·LLM DEB 裁决落地批) · Cache B + 失败语义三件套 + Thinking D 实测/接线 (用户裁"无争议完成更新")
**裁决**: 用户定调信号产生/筛选优化属"修尺子"; 无争议三项立即落地。
1. **Cache 方案 B**: yaml 6 处 `cache_on_fail→false` (跨标的污染消毒, 历史零命中); 附带条件验证 ✓ (dashboard 失败路径有显式 WARNING 非静默)。回滚: 改回 true。
2. **失败语义 B 三件套** (`model_call.py` + `hermes_worker.py`): ①finish_reason 全链透传+落审计 (截断可观测) ②双信号截断降档自救 (parse失败且length → ×0.5 下限1024, flag `llm.retry_downshift_enabled` 回滚) ③hermes 空 verdict 质量门 (per-role 兜底+缺席标注+absent_roles 透出)。验证: py_compile+真调用回归 ✓。
3. **Thinking D 实测实锤**: 探针 A/B 对照 — DashScope 不传参时 **reasoning=117tok/2.8s** vs 显式False 0/0.8s → **外部 Hermes DA 链 (48标的) 一直 thinking ON 运行且系统正常** (18:05→19:31, 余量2h)。用户担忧的"全标的耗时"已是现状。
4. **Thinking 基建接线**: yaml 路由级 `extra_body` 透传+合并 (调用方优先, 无配置行为不变, 回归冒烟 ✓) — per-feature 灰度至此 = yaml 2 行。
**待用户确认**: thinking 分层方案首灰特征 (建议 dr_debate, 见下次回复方案表)。Q4 观测缺口 (audit 盲区+test 污染 57%) 另立案。

### 2026-07-25 (周末·thinking 首灰 + 缓存探针实锤) · dr_debate 灰度上线 + 隐式缓存观测落地 (用户批)
1. **dr_debate thinking 首灰上线**: yaml `extra_body.enable_thinking: true` + 调用方 max_tokens 2500→5000 (防 07-06 empty_response 复现)。端到端真调用: 6.7s/输出完整/finish=stop ✓。观察指标: empty 率/时延/截断率 (finish_reason 已落审计)。回滚: 删 yaml 两行 + max_tokens 改回。效果**不设为 08-29 终裁判据** (样本周期 4-6 周不够)。
2. **缓存机制探针实锤 (用户问 cache_control ephemeral)**: DashScope 兼容模式实测 — ① **deepseek-v4-pro 隐式前缀缓存自动生效** (同前缀第2发 cached=3072/3298, 时延 -45%, 无需任何参数) ② qwen3.7-max 未见命中 (同条件 cached 恒=0) ③ Anthropic 式 `cache_control: ephemeral` 被**静默忽略** (接受不报错不生效) → 无需也无法显式标记, 降本靠"固定前缀 prompt 结构"+deepseek 自动缓存。
3. **cached_tokens 观测落地** (`model_call.py`): CallResult.cached_tokens + audit error_msg 拼接。生产链端到端: agent_bridge 第2发 cached=1024/1344 (76% 前缀命中) 已入库 ✓ — token 大头 (agent_bridge 4.5k×28次/日) 的真实成本从此可见。
4. **prompt 结构建议入帐 (不动手)**: 缓存友好 = 固定 system/规则前置 + 变化数据 (价格/时间戳) 后置; 待首周 cached_tokens 数据出来后再定向优化 prompt 拼接顺序 (避免盲改)。
5. **⚠️ 勘误 (用户指正, 2026-07-25 晚)**: 上述②的"cache_control 被静默忽略"结论**错误** — 我的探针只发了一次 (创建轮必然 cached=0) 就误判。重测 (创建轮→命中轮): **qwen 显式缓存完全有效** — 第1发 cache_creation=3719tok, 第2/3发 cached=3719 (99.4%), TTL 5min (ephemeral_5m)。**已落地**: `model_call.py` 新增 route 级 `prompt_cache` 开关 (仅 dashscope 启用数组格式, 自救重试同步透传); yaml 首批开 deep_target_analysis + universe_expansion_reasoning (qwen 主力+批量连发适配 5min TTL)。生产链端到端: **98% 命中, 时延 3559→762ms (-79%)**, audit 已记。回滚: 删 yaml prompt_cache 行。方法教训: **缓存类探针必须测"创建→命中"全周期, 单发判无效=方法错误**。

### 2026-07-25 (周末·离线退出回放器落地) · 路径1 建成 + 首轮网格结果 (只读工具, 零 live 变更)
**是什么**: `scripts/exit_replay_backtest.py` — PLAYBOOK_SIM 全历史 (n=145) × Futu 1min bar (深度实测≥2026-01) 离线反事实回放退出参数。直接 import 实盘 `_compute_dynamic_effective_sl`/`_get_factor_exit_config` 杜绝两份数学; pess/opt 双序报区间; 费用/滑点/冷却/TP门禁/MFE_STALL/TIME_EXIT 全链复刻。验证锚点: 回放MFE vs 记录 median差0.077%, 退出价差median 0.136%; 固有乐观偏差+$1.9/笔 (bar影线>轮询采样, 变体排序无偏, 紧dd类收益为保守低估)。详见 `docs/debates/DEB-20260725-exit-replay-backtest.md`。
**首轮结果 (Δbaseline 口径)**:
- 全史 (n=141): early_tight 巨量止血 +$1050~1170 但**集中6月崩盘regime** (近期仅+$40~78, 与live shadow互证); dd 0.4/0.5/0.8 处平台区 (差≤$2/笔)。
- **近期窗口 (n=62≥30) 一致指向"更早保护"**: be_0.3 (+$191~209) ≈ dd_0.4 (+$160~187) > tiered_dd0.4_tier1.2 (+$131~149) — 与07-19放宽处方有张力, 裁决路径=tiered_dd live shadow (07-25起采集) 2周后双源交叉, 一次一变量, 暂不行动。
- 所有变体期望仍负 → 退出调参是二阶杠杆, 与核心诊断一致。
**边界**: 三门/入场门不可回放 (路径2); sl_floor网格需ATR重建 (v2候选); 4笔时区修复前损坏时间戳交易已排除。

### 2026-07-25 (周末·提量裁决) · 用户裁: 验证期保持 UE 优先 (ADR-020 维持) + 提升每日交易量快速攒样本
**用户裁决**: ① ADR-020 "UE 最优先"维持不变 (信息源复盘提出的翻转议题否决 — 验证期以攒样本为重, UE 继续优先成交攒其判分样本) ② 提升每日交易量。
**漏斗诊断 (动手前实测)**: 执行侧上限全部未触顶 (max_daily_positions=30 昨日用 8 / notional cap 用 32% / 预算用 5%); 真瓶颈在供给侧 (近5日指令 15-21 条/日) + 三门拦截 (验证对象不动)。top_n 已废弃不截断, 非限量器。
**提量落地 (仅抬配额不降标准, 三门/质量门/退出参数零变更)**:
1. **topup 配额**: target_min_instructions 25→30, max_add_per_day 12→18 (`unified_config.yaml`, 备份 .bak_20260725_topup_quota)。
2. **topup 池扩容 16→24 只**: +ARM/MRVL/UBER/RIVN/SNOW/SHOP/DELL/CRWD — 选标口径 = ATR 甜蜜区 3-4% 画像 (07-24 复盘唯一正期望桶) + 避开低波防御股 (2.5-3.0% 次差桶)。仍经 Screener 质量门+三门。
3. **生效**: 热重载实战首秀 — 10:02 CST HotReloader 自动生效 (凌晨 ConfigGov 修复的直接收益, 零重启), 运行时确认 30/18/24。
**预期效果**: 盘前批 (08:45-09:30) + 盘中窗 (10:30-11:30, 周一首验) 双窗可补至 ≤18 条/日, 叠加 DA 13-18 + UE 2-5 → 供给上限 ~30-40, 经三门后预期成交 15-25 笔/日 (vs 昨日 8)。样本速度翻倍, 08-07 裁决点前可攒 ~150-250 笔 go-forward。
**风险提示 (已入帐)**: 提量 = 日亏损绝对值可能同步放大 (昨日 -$31 是 8 笔规模); 若日亏 > $150 (回到旧基线量级) → 配额回滚 25/12 一行恢复。回滚: yaml 两处改回 + 删池扩容段。

### 2026-07-25 (周末·信息源复盘) · UE/DA/topup 源头质量实证切片 — 3 项修尺子落地 (策略/退出逻辑冻结不动)
**背景**: 用户定调"现有策略逻辑不动, 单独分析信息源头质量"。近14天 134 条指令按源实证切片 (fwd_return 前向台账, 不受退出污染)。
**源头画像 (实测)**:
- **DA** (108指令/成交69%): 方向命中 52% 均fwd +0.31% (薄弱正edge, 与北极星诊断一致); 但 **conf 退化为常量** (89/95条<40, gate WARN 罚成 35 后分层能力=0); **debate_outcome 是隐藏分层器**: 分歧BUY 67%命中+0.9% > 弃权 47%+0.39% (反直觉: 有争议的 BUY 更好); **SHORT 系统性烂**: 全部 SHORT 相关 outcome 均负 (分歧SHORT T+1 0%命中 -4.5%, n=6; SELL_SHORT 整体 29% -2.2%, n=17)。
- **UE** (26指令/成交81%): **方向命中仅 29% 均fwd -0.67% (负edge)**; 高度重复推荐 (ORCL SHORT×5/EOG BUY×4, 无衰减机制); conf 库内恒=50 (07-16 cap 把 LLM 0.85-0.92 分层全抹); tier 写侧 07-22 已落但 v2 时代样本少且初样本全负 (HARD_EVENT n=2 0%命中 — n<30 不下结论, 八月继续攒)。
- **topup**: 零成交样本 (首跑+0 诚实), 周一盘中窗才有数据; 风险前瞻: 池源=watchlist 含低波防御股 = 昨日复盘 2.5-3.0% 次差桶主力 → 与 P-1 名单合一联动设计 ATR 过滤。
**修尺子落地 (3 项, 均纯归因数据, 零排序/交易行为变更)**:
1. **UE cap 前原始 conf 落库** (`llm_universe_scan.py` origin.raw_confidence): 排序仍用 capped=50, 归因恢复分层。
2. **DA confidence_semantic 落库** (Hermes skill `pipeline.py` raw_instruction 补字段, 备份 .bak_20260725_confsem): bridge cand 块早已双写但落库丢失 → P0.2 conf 校准修复 (X1 赛道前提) 的归因地基补齐。下次 DA 运行自动生效。
3. **判分工具补读侧** (`direction_hit_scorecard.py` 新增 2 组切片): debate_outcome 4 档 (弃权/分歧BUY/分歧SHORT/一致) + UE self_tier 4 档 — 八月终裁事件赛道判据消费端接通, 周度仪表化。
**观察入帐 (不动手, 攒样本/待裁)**: ① UE 重复推荐衰减 (同 symbol+direction 连续负 fwd → 冷却; 信息源侧的"连败封禁", 需 DEB) ② DA SHORT 质量门候选 (SHORT 需 HARD_EVENT 级证据, 与 X2 四前置"SHORT不隔夜"呼应; n=17 小样本先观察) ③ debate_outcome 分层若 2 周后仍稳定 → 候选替代 conf 做仲裁变量 (需 DEB)。
**⚠️ ADR-020 与实证冲突, 需用户重新裁决 (2026-07-25 勘误发现)**: auto_execute 预算排序主键 = `_src_priority` (ADR-020, 07-17 用户指定 **UE 最优先**、DA 次之), conf 仅同源内次键。当时逻辑"新资讯动态标的优先占预算"; 但如今指令层实证 UE 方向命中 29%/均fwd -0.67% (负 edge) vs DA 52%/+0.31% — **最弱源持有最高执行优先级** (07-24: UE 指令转化80% vs DA 31%, UE 占昨日一半仓位含 LMT/RTX 两大亏单)。候选处方: _src_priority 改 DA(2)>UE(1) 或按滚动 fwd 胜率动态定序 — 属 ADR 变更, 待用户确认后一行切换。(勘误过程: 曾试 UE conf cap 50→30 降优先级, 发现对跨源排序无效且污染 conf 分桶已回滚, cap 保持 50。)
**验证**: py_compile 3 文件 ✓; scorecard 新切片实跑出数 ✓; skill 侧 patch 带备份+锚点断言 ✓。无需重启 (llm_universe_scan/DA skill 均为每日新进程)。

### 2026-07-25 (周末·首夜复盘) · 07-24 交易链路实证复盘 — 防守已到位, 下一刀在赢家捕获率 (只读分析, 零改动)
**总评**: 三门砍掉大亏尾部 (-$31 vs 基线 -158/-164/-379), 但 R:R 倒挂创新低 (昨日 0.35 / 近14天 0.58, 北极星需 >1.29)。
**发现 A (最强, 双重确认)**: ATR 甜蜜区 3.0-4.0% — 昨日赢家 (NVDA/TSM/BABA/GOOG, atr 3.5-3.9, mfe≥0.95) vs 输家 (RTX/MRK/LMT/TMO, atr 2.58-3.00, mfe≤0.33) 按 ATR 完美二分; 近14天分桶: <2.5% 35%WR -$6.7/笔 | **2.5-3.0% 52%WR -$8.5/笔 (现在放行的次差桶)** | **3.0-4.0% 71%WR +$3.2/笔 (全仓唯一正期望桶)** | ≥4% 38%WR -$25/笔。
**发现 B (机制实锤)**: 赢家捕获率 15% — TSM/GOOG/BABA mfe≈1% 全部只落袋 0.14-0.15%; drawdown 0.8 对 mfe≈1% 是"利润归零锁" (公式 (mfe-0.8)/mfe: NVDA 1.9%→54% 实测吻合)。R:R 倒挂的直接现场。
**发现 C**: STALL 150min 反事实混合 (e_shadow: LMT/RTX 早45min砍各省~$15/$12, TMO/MRK 早砍更差) → 不动, 攒样本 08-07 裁。
**发现 D**: 8 笔全部 10:25-26 单点批量入场, LMT/TMO 入场即水下 (45min MAE 0.67-1.15%) → 周一 topup 盘中窗 = 入场分散化第一步, 先看它的数据。
**周末议题 (按数据强度)**: ① ATR 门 2.5→3.0 + ≥4% 上限门 (先 shadow 干跑, n=21/17/13 样本小禁直接切) ② 分层 drawdown lock (mfe<1.2 用 0.4-0.5 / ≥1.5 保持 0.8, exit_param_shadow 变体) ③ STALL 不动 ④ 入场分散化等 topup 数据 ⑤ 方向偏斜 16:2 继续观察。全部遵守验证期纪律: shadow-first, 零 live 参数变更。

**议题①② shadow 已搭建 (用户批准"同意", 当日落地)**:
- **② 分层 drawdown 变体** (`exit_shadow.py::EXIT_VARIANTS` 新增 2 个): `tiered_dd0.4_tier1.2` (peak<1.2% 用 dd0.4, 否则 0.8) + `tiered_dd0.5_tier1.5`。铁律合规: dd_eff 恒不松于现行 0.8 → 路径可观测。poll 循环内按 peak 切档; finalize 通用循环自动覆盖无需改。冒烟 3/3 (小peak用紧锁先触发/大peak回落 0.8 与 baseline 同步/SHORT 镜像)。周一开盘起随退出快循环自动采集, 落 `data/shadow/exit_param_shadow.jsonl`。
- **① ATR 门干跑判分** (`scripts/atr_gate_shadow.py` 新建, 只读): 6 变体 (lo2.8/lo3.0/hi4.0/lo3.0_hi4.0/lo3.0_hi5.0 + live 对照), 口径 = 拦"实际已成交"单的 saved=-pnl (均比现行严 → 完全可测)。首夜 go-forward: lo3.0 拦 4 笔 省 +$73.3 仅误杀 MRK(+$0.1)。⚠️ 14 天回看 (lo3.0 省 +$398 但杀赢家 24 笔含 NVDA+102) 混杂旧退出时代, **裁决只用 07-24 起 go-forward 样本**。cron 已加 (每交易日 05:58 CST `--since 2026-07-24` 累计判分, 落 `data/shadow/atr_gate_shadow_latest.json`)。
- 生效: 09:14 CST 重启 (active/0 Traceback/审计过)。裁决点: 与 08-07 合并, ≥2 周 go-forward 样本后提 DEB。回滚: 变体从 EXIT_VARIANTS 删除 + 删 cron 行 + 删脚本。

### 2026-07-25 (盘后·隐患扫描) · 时区同款"跨模块隐性契约"全仓排查 → 用户裁"顺手修", 6 项全部修复落地
**背景**: 用户问"时区之外还有没有同类未发现问题"。按 6 类高危反模式扫描: 配置漂移/单位口径/cron季节错位/半日市/静默吞异常/PDT。用户裁决"那就顺手修复掉"。
**✅ 健康项 (实测, 无需动)**: 配置审计 0 条; mfe/profit 百分比口径无混用; PDT "51/3" 係合规放行 (equity $98k≥$25k 豁免); dual_broker 平仓失败有告警链非静默。
**修复清单 (6 项, 全部落地)**:
1. **半日市 13:00 ET 提前收盘支持**: `holiday_calendar.py` 新增 `US_EARLY_CLOSE_DATES` (2026-11-27/2026-12-24/2027-11-26) + `get_us_close_minutes_et()` (780/960); 接入 3 消费点 — `pipeline.py:5092` (sim 退出主循环 minutes_to_close) / `pre_close.py:48` (主线 pre_close 窗口) / `pipeline.py:3223` (量能门 elapsed_fraction 分母), 均 try/except 兜底 960。position_sync 同步频率档未接 (无害仅多同步 3h, 范围控制)。验证: 4 断言 + 模拟 11-27 12:53 ET → mtc=7 命中 PRE_CLOSE 窗口 ✓。
2. **监控 cron 冬令时盲区**: crontab `error_alert.sh` (*/15) + `combined_monitor.py` (*/10) 凌晨窗口 0-4 → **0-5** (冬令时收盘 05:00 CST 覆盖); 备份 `data/crontab.backup.20260725_*.txt`。
3. **Task#36 死引用移除** (`main.py`): migrate_trade_records 块删除留墓碑 (schema 早稳定, TradeStore 自建表); 重启后 Task#36 WARNING = 0 ✓。
4. **定桶落库日志降噪** (`pipeline.py`): 零工作轮 (幂等全跳) INFO→debug, 有工作仍 INFO。
5. **PDT 日志豁免标注** (`pdt_guard.py`): 计数行追加 "(equity≥$25k 豁免)"/"(PDT 受限)", 消 "51/3" 误读。
6. **遥测读侧去重展示** (`first_night_gate_report.py` + `early_acceptance_check.py`): 增去重标的数 — 重跑 07-24: "streak_ban=159 (3 标的) low_atr_block=265 (5 标的)" ✓ (写侧遥测语义不动)。
**验证**: py_compile 7 文件 ✓; 半日市 4 断言+窗口模拟 ✓; test_playbook_exit_rules 1 passed ✓; 两报告重跑全过且无度量回归 ✓; main.py AST ✓。
**生效**: 08:48 CST 重启 (盘后 0 持仓): active / Task#36 WARNING=0 / 0 Traceback / ConfigGov 全绿 / 模拟盘 ✓。回滚: 各项独立 git revert; cron 用备份还原。
**分类**: 全部"修尺子/修基建", 零退出参数变更, 零 config_version 断点。半日市改动在 11-27 前无行为差异 (普通日 960 不变)。

### 2026-07-25 (盘后·时区SSOT全仓统一) · 修尺子 — 5 Critical + 4 High 时区不一致系统修复, 零交易行为变更
**背景**: 07-24 观察⑤ 实锤 `_et_trade_date(now_et())` 双重转换 bug 后, 全仓时区审计发现 5C+4H 共 9 类不一致 (134 文件)。本批为系统性补全, 与 07-24 单点修复互补。
**分类**: 修尺子 (度量/校准修复)。零退出参数变更, 零 config_version 断点, 零信号分布影响。
**DEB**: `docs/debates/DEB-20260725-timezone-ssot-consolidation.md` (完整清单+验证+回滚)
**修复摘要**:
1. **C1** `trade_recorder.py` trade_date 二次转换 → `get_trade_date()` (ET 上午交易不再标到前一天)
2. **C2** `symbol_daily_loss_guard.py` 读写契约统一 (48h 宽窗口 + `_et_trade_date` 精确过滤, 消除 CST-cutoff vs ET-aware-ISO 12h 偏移)
3. **C3** `datetime_utils.py` 合并为 shim (消除两模块 `to_et()` naive 假设冲突: CST vs UTC 差 8-13h)
4. **C4/C5** 22 处硬编码 `timezone(timedelta(hours=-4))` + 月份 DST 判断 → SSOT `now_et()`/`ET` (冬令时不再差 1h)
5. **H1** 5 处 `entry_time=datetime.now()` → `now_et()` + scheduler_core `astimezone` 修正
6. **H2** `intraday_heartbeat.py` 信号漏斗 cutoff / 显示 / 推送统一 ET
7. **H3/H4** app/ 下 50+ 处 `utcnow()` → `now_utc()`/`now_et()` (**0 残留**, grep 确认)
8. **H6** pytz `replace(tzinfo=)` → `localize()` (timezone.py + risk 模块, 消除 LMT 偏移)
9. **额外** `scheduler_core.py` 裸 `import pytz` → SSOT ET (消除 pytz 缺失时启动崩溃)
**验证**: py_compile 134 文件 ✓; 核心模块 import ✓; `utcnow()`/硬编码偏移/`replace(tzinfo=ET)` 残留 = 0 (grep 确认)。
**对毕业数据**: 无影响 (不改退出参数/触发逻辑/config_version; 只修"读哪天的交易"的尺子)。
**生效**: 盘后 0 持仓窗口, 下次重启即生效。回滚: `git revert` 整批或按文件独立 revert。
**独立审查 (Qoder, 盘后)**: 批次完整合理 — 声明逐项实证复核全过 (残留 grep/C1・C2・C3・H2 diff 实核/运行时双路径语义/shim 消费者全查); **发现并补修 H6 漏网 2 处** (pipeline.py:3980 `replace(tzinfo=_et_tz)` + shadow_tracker.py:506-507 `replace(tzinfo=_ET)` — 别名逃过字面 grep, 运行时实锤 -04:56 LMT/56min 误差, 影响 holding_hours 度量; 同款 localize 处方补修)。to_et 语义变更 (naive UTC→CST) 查无真实调用方, 风险解除。first_night_gate_report 重跑无度量回归。**08:15 CST 已重启含补修 (active/0 Traceback/ConfigGov 全绿)**。详见 DEB §九。

### 2026-07-25 (盘后·首夜验证遗留处置) · 修尺子两项 — stall 埋点 + 判据容差, 零行为变更
**背景**: C0 首夜验证 MRK 被脚本误报 ❌ (触发时 cur=-0.03% 水下合法, 成交后落库 +$0.12 被落库PnL尺子误判)。两项均"修尺子"类, 不改任何触发/退出行为。
1. **stall 触发现场埋点** (`pipeline.py`): 规则 3.4 触发时写 `meta["stall_exit"]={stall_minutes, mfe_at_trigger, cur_at_trigger, threshold}`; 主退出落盘处从内存 meta 搬运入 `_meta_out` (必须显式搬 — `_meta_out` 基于开环旧行重建会丢字段)。SL 兜底路径在规则评估前且只产 SL_*, 无需搬; 部分平仓/ghost 路径由每轮 meta 写回 (L5432) 兜底。
2. **判据尺子修正** (`first_night_gate_report.py`): "水下"判定优先读 `stall_exit.cur_at_trigger` (触发现场); 无埋点存量单回退落库 PnL 容差 ±半tick×qty。重跑 07-24: MRK 转 ✅, **四判据全过 (结论行从"1项异常"转"全部通过")**。
**验证**: py_compile 2 文件 ✓; `test_playbook_exit_rules.py` 1 passed (存量 44 场景套件零回归); 定制冒烟 3 场景 (触发+埋点/水上无埋点/曾浮盈无埋点) ✓。
**生效**: 盘后 playbook 0 持仓零风险窗口, 07:49 CST 已重启 (ConfigGov 全绿/模拟盘/0 Traceback), 周一开盘即带埋点。回滚: 两文件各自 git revert, 不影响三门开关。

### 2026-07-25 (凌晨·现场修复批) · 巡检定性项提前修复 — 5 项落地, 04:20 重启生效 (用户授权"能现场修的不等周末"+重启权)
**范围纪律**: 只修巡检/日志已定性、有明确处方的小改动; 大项 (P-1 名单合一 / P-2 订阅动态化 / P0.2 conf修复 / UE tier透传) 仍留周末。零实单退出参数变更, 零 config_version 断点。
1. **启动预热绕过静默门 → ✅修** (`signal_pipeline.py::run_signal_scan`): 外部入口 (ImmediateScan/force_scan) 补同款 `strategy.main_scan_enabled` 门, 静默时直接 return。冒烟: mock 双场景 (静默不下钻/开启下钻) ✓。回滚: 删该块。
2. **Reconciler broker_only 补录缺 playbook 守卫 → ✅修** (`reconciler.py::_add_legacy_position`): 补录路径补 `is_playbook_open_position` 守卫 (与收养路径同款), playbook 仓不再被补录成 unknown+protected stub。冒烟: 双场景 (playbook跳过/非playbook照常) ✓。验收: 下次重启无 "broker_only 补录: <playbook持仓>" 日志。
3. **解锁 spam 反模式全量收敛 → ✅修**: 剩余 9 处 `ManualOrderService()` 直构全部改 `get_manual_order_service()` 单例 + 删 disconnect (thesis_challenge×3 / plan/pipeline×2 / llm_validator / compute_indicators_full / plan_feedback / premarket_enrichment)。与 07-24 夜 fundamental_provider 同款处方; 至此全仓直构仅剩单例工厂本体。
4. **ConfigGov 热重载失联 (周末必修项提前) → ✅修**, 双管齐下:
   - 根因实锤: main.py Layer 1.7 strict 审计在 Layer 2/3 之前, 存量 BLOCKING (`CFG.MIN_CONFIDENCE 0.7 ≠ YAML 0.8`, 全仓仅此一条) raise 被外层"降级运行"吞 → 漂移检测+热重载根本没启动。
   - (a) 消漂移: `config_defaults.py` MIN_CONFIDENCE 0.70→0.80 同步 YAML SSOT。影响面已查: 运行时真值由 resolver 读 YAML (intraday=strategies.defaults 0.7 / ssot 0.8) **不变**; 变化仅 resolver 异常兜底 + DEGRADED 降档地板 (0.7→0.8, 更保守, 且主线扫描已静默=休眠路径)。
   - (b) 结构加固: main.py Layer 2/3 启动前置、strict 审计后置 (告警/raise 语义不变) — 未来再有存量 BLOCKING 不再连坐热重载。
   - 验证: venv 实测 audit BLOCKING 1→0, resolver 各 phase 取值不变 ✓。验收: 重启后日志见 "[ConfigGov] ✅ 热重载已启动" + "[ConfigAudit] ✅ 审计通过"。
5. **🔪 减法: P0 MFE shadow 死路径移除** (`position_monitor.py` L1084-1142 整块删, 留墓碑注释指向 e_shadow_*): 无脚本消费者 (仅 DEB 文档引用且已同步标注), jsonl 从未产出。
**验证**: py_compile 11 文件 ✓; 定制冒烟 4 场景 ✓; pytest tests/risk+core+config 92 过/16 败 — git stash 对照基线同样 16 败 (含 test_exit_engine.py 引用已删模块, 均基线遗留) → **零新回归**。
**生效安排**: 盘中不重启 (守首夜三门+8持仓观察不受污染), **04:20 CST (收盘后) 定时重启 `ftseek-us.service`** 并验启动日志 (ConfigGov 全绿 / 无预热扫描 / reconciler 无 playbook stub)。回滚: 各项独立 git revert, 互不耦合。

**04:20 重启验证结果 (2026-07-25 04:21:46 CST 启动 / ET 16:21:46, 已过 16:00 收盘)**:
- 服务状态: `systemctl restart ftseek-us` 成功, PID 46905 运行中 ✅
- 运行模式: 模拟盘 `TRADE_ENV=1 | FUTU_TRADE_ENV=1` ✅
- 判据① [ConfigGov]: `[ConfigGov] ✅ 漂移检测已启动 (interval=4h)` + `[ConfigGov] ✅ 热重载已启动 (cooldown=60s)` + `[ConfigAudit] ✅ SSOT 配置一致性审计通过 — 无漂移检测` (0 BLOCKING) ✅
- 判据② 无启动期主线扫描: 重启后日志无「开始信号扫描」「自适应扫描」「run_signal_scan」等批量 stages Pipeline 痕迹 ✅
- 判据③ Reconciler 无 playbook 补录: `[M1] 启动对账完成: matched=0 broker_only=0 local_only=0 legacy_tagged=0`, 未见「broker_only 补录」或 playbook 持仓 stub ✅
- 附加: Futu 连接正常, OpenD 127.0.0.1:11111 连通; 启动后仅 1 次 `SubscriptionManager reconnect_failed_x1` 瞬时告警, 3ms 内重连成功并恢复 `FutuLV3·历史 盘后连接 OpenD 成功`; 无 Traceback, 无 ERROR 循环 ✅
- 重启后 `early_acceptance_check.py --phase OPEN`: 今日开仓 8 笔/已平 8 笔, 入场滑点 8/8, streak_ban=159 low_atr_block=265, 通过 ✅
- **结论: 三判据全绿, 无需回滚, 凌晨修复批全部生效。**

### 2026-07-24 (深夜·现场巡检) · 首夜实盘全面体检 — 8 绿 3 黄 1 减法发现
**✅ 健康项 (实测)**: ①解锁spam修复稳定(0次) ②主线扫描静默生效("开始信号扫描"0次) ③三门live实拦: 连败封禁拒 MSFT/PDD 新触发 + GOOG SHORT 由 SL_TRAIL_LOCK 锁利 +$5.5 ④**C1 往返滑点闭环首夜双样本**: GOOG 入场 2.03bps + 出场 2.19bps (往返 ~4bps 极低, S1 降级再坐实) ⑤今日 18 指令 regime/atr 覆盖 18/18 (方向4首夜达成) ⑥入场滑点 8/8 全录(中位~2.6bps) ⑦E shadow 45/60/90min 检查点 7/7 实采 ⑧Futu 启动抖动后 20s 退出循环稳定。
**⚠ 异常定性 (周末处置, 今夜均无实害)**:
1. **启动预热绕过静默门**: `_run_signal_scan_body`(:517) 启动期跑了一轮 stages Pipeline (44 标的, 23:41-42, 后零复发); 三重下单锁在(signal拦截+执行冻结+source白名单) → 预热路径补同款 main_scan_enabled 门。
2. **Reconciler broker_only 补录缺 playbook 守卫**: 每次重启把 7 个 playbook 仓补录成 unknown+protected stub (收养路径有 is_playbook_open_position 守卫, 补录路径没有) → 补同款守卫。
3. **TwelveData 429 自熔断 10min** (备源自恢复, 无害, 仅记录)。
**🔪 减法发现**: 今晨 P0 MFE shadow (position_monitor → mfe_early_exit.jsonl) 是**挂错宿主的死路径**(playbook 仓不进主线 store); 同功能由正确宿主的 e_shadow_45/60/90m (trade_records.metadata) 覆盖且首夜 7/7 实采 → 分析 SQL 改读 e_shadow_*, 死块周末移除。方法教训: **落 shadow 前先查正确宿主是否已有同功能埋点**。
**待观察 (今夜剩余)**: 首批 MFE_STALL_EXIT 资格约 00:55 CST (TSM/LMT/RTX/TMO/MRK 五候选若仍水下); EOD 处置; 00:30 检查点。

### 2026-07-25 (凌晨) · topup 路径今夜验证尝试 — 机制全绿, 策略诚实 +0, 完整路径待周一
**背景**: 用户要求今夜走通 topup 成交路径。双窗已过 → 临时延盘中窗至 1345 ET (不降质量标准, 仅加评估时点)。发现: yaml 热重载失效 (ConfigGov 因存量 MIN_CONFIDENCE BLOCKING 审计降级失联 — **存量告警升级为功能损失, 周末必修**) → 00:23 重启生效。
**结果**: 机制全绿 — 窗口门/D2退避(尝试 1/2 → 15min → 2/2 封顶, live 首验完美)/11 候选评估/无方向不硬凑, 全部按设计。**策略层两次实时价评估 (12:23/12:38 ET) 均 +0** — 池内今日确无"趋势+回调到位"setup, 宁缺毋滥是诚实答案。未验段 (写库→触发→成交→退出→归因) 需真 setup 日 — 周一 10:30-11:30 ET 窗口。盘中窗已恢复 1130 (yaml 已改, 下次重启生效, 周末批次会重启)。

### 2026-07-24 (夜·P0事故修复) · 实盘解锁 spam 止血 — FundamentalProvider 反模式
**现象**: 盘中无人手动下单, 但 manual_order_service 每 ~3s 一次"连接+实盘解锁", 撞富途限频 (30s/10次) 报 ERROR。
**根因链**: topup 盘中窗 (22:24 重启提前激活) 每 ~2min 扫 11 只 → Screener → `FundamentalProvider._fetch_futu` **每只标的 new 全新 ManualOrderService + connect()** (绕过单例) → connect() 对实盘无条件 unlock_trade → 解锁 spam。放大器 = 观察② +0 重试风暴。
**修复** (`app/data/providers/fundamental_provider.py`): 改用 `get_manual_order_service()` 单例 (connected 短路→零额外解锁) + 删 disconnect (单例连接常驻, 不可断 dashboard 共享连接)。23:01 重启, 8 持仓全部重新接管。
**验证**: 23:02 后 6.5min 内 topup/Screener 22 轮次活动, 解锁次数 **0** (此前同负载每轮 8-11 次); 预期未来每进程生命周期最多 1 次首连解锁。
**遗留** (周末收敛, 入减法第1批): 同款反模式共 **9 处** `ManualOrderService()` 直接构造 (thesis_challenge×3 / plan/×4 / premarket_enrichment) — 均低频路径但同存解锁副作用, 统一改单例。另: 行情快照类需求长期应脱离交易服务 (只建 quote_ctx 不碰解锁), 归入减法 B 类评估。

### 2026-07-24 (晚) · 指令链回溯 + topup 首跑验证 — 零代码改动, 四条计入观察

**指令链回溯 (全实测)**: 今晚 13 条指令全部来自 Hermes DA skill (v0.61) 直写 DB — 18:05→19:31 CST 分析 ~48 只 → 19:31:31 写 `opportunity_scan_2026-07-24.json` + 同秒批写 `playbook_instructions` (与 `daily_battle_plan_us.json` 无关, 那是 07-23 旧产物)。漏斗: 48 只 → gate 全部 SHORT/SKIP/tier差 BLOCK → 13 只 BUY tier中 WARN 放行。推送口径: 35% = gate WARN 罚 (0.50−0.15, 辩论弃权未罚, 语义置信度在 bridge `confidence_semantic`); 0股/风险$0 = sizing 执行时才算; ❌ = confidence<50 图标非未成交。

**topup 首跑验证 (设计路径自动触发, 非手动)**: 机制全过 — 时间门 (08:31 干跑 None / 08:45:02 首触发)、循环接线 (~2.5min/轮)、缺口计算 (13<25→补≤12)、池去重 (排 NVDA/MU/MSFT)、Screener 13/13 过质量门、无方向不硬凑 (+0 零写入)。+0 根因 (离线逐只复现, ctx 字段健康): 8 只 NEUTRAL 无方向 + 3 只有方向非回调窗 (TSLA/NFLX/QCOM) + 2 只回调质量 q=0.3<0.5 (GOOG/AAPL)。cautious 大盘下池内无"趋势明确+回调到位"标的, 是 technical 策略的诚实答案, 非数据/代码故障。

**计入观察 (验证期不动手, 9 月或用户裁决再议)**:
1. **过滤审计盲区 (修尺子候选)**: `_build_instructions_from_votes` 的 `vote_result is None` 分支 (pipeline.py:765) 无计数器 → 审计 breakdown 全 0 却 skips=13 无法自解释; `TechnicalFallbackAdapter` None 路径无 info 日志。一行计数+一行日志可补。
2. **topup +0 重试风暴 (行为变更, 排队)**: 幂等标记仅在 ≥1 条写入时落, +0 → 08:45–09:30 每轮重扫 13 只 (~60s/轮 ≈18 轮近连续)。候选方向: +0 也落标记 / 退避。
3. **gate RR<1 不 BLOCK (skill 侧)**: BABA 指令 R:R 0.65 仅 WARN 放行 — signal_gate 的 WARN 理由无 RR 拦截; 是否加 RR 下限硬门待裁决。
4. **双 adapter 弃权口径分叉 (休眠)**: skill 侧 adapter 07-18 已修"弃权不罚", FTSeek 侧 `BridgeVotingAdapter` (pipeline.py:414-428) 仍把 BUY↔SKIP 当分歧罚 0.15 (且 "BUY"≠"LONG" 字符串比较会误罚方向一致); 今晚直写 DB 路径未触发, 若 bridge 兜底路径复活需先对齐。

**topup 盘中窗实施 (知情破例已落地, 未激活)** — 用户裁决 2026-07-24 晚: 盘前批用昨收价评回调窗系统性偏陈旧 (首跑 +0 实证: 8 NEUTRAL/3 非回调窗/2 质量不足), 加盘中实时评估窗。
- 改动: `pipeline.py` 新增 `_topup_window()` (双窗口: 盘前 845-930 / 盘中 1030-1130 ET, 可配置) + `run_playbook_topup_supplement` 改分窗幂等 (raw.source+raw.timing, 历史无标记行视为 premarket) + 两窗共享 max_add_per_day=12 + 归因 `raw.timing=premarket/intraday`; `scheduler_core.py` TRADING 分支接线 (定桶 catch-up 前, 新行当轮补 vol_bucket/regime); `unified_config.yaml` `playbook.topup` 增 `premarket_end_et_hhmm:930 / intraday_start_et_hhmm:1030 / intraday_end_et_hhmm:1130`。
- 冒烟: py_compile ✓ + 窗口单测 13 例 ✓ + 桩写库全链 8 场景 ✓ (盘中首跑/分窗幂等/跨窗不阻塞/共享上限/窗外不触发/历史标记兼容), 零生产写入。
- **激活安排 (守一次一变量)**: 今日 (07-24) 已是退出三门首日 + 盘前 topup 已跑, 盘中窗**不在今日叠加** — 07-25 04:20 CST (美股收盘后) 重启 `ftseek-us.service`, 首个 live 盘中窗 = **周一 07-27 10:30-11:30 ET**。周一验证: 日志 `[playbook·topup] 补量启动 (intraday)` + DB 新行 `raw.timing=intraday` + 定桶/regime 当轮补齐 + 与盘前批效果切分。
- 回滚: `playbook.topup.enabled: false` (整体) 或把 `intraday_start/end` 设为相同值 (仅关盘中窗); 代码级 git revert 两 .py 文件。

**0成交诊断 (22:20 CST 实测, 用户问"还没有自动成交是否正常")** — 机制全部正常, 成交为零是 OKR 账户熔断按设计拦截:
- 链路实测: 18 条指令 (13 DA + **5 条 UE** `universe_expansion` 09:10 ET 写入: TSM/LMT/RTX BUY + GOOG/XLY SELL_SHORT — 非 topup 非 catch-up 生成); 8 条价格触发 (triggered=1: TMO/NVDA/BABA/MRK/TSM/LMT/RTX/GOOG); 触发检测/低ATR门 (拦 CSX/JPM/KO/MA/XLY)/连败封禁 (拦 MSFT/PDD/UNH)/量能门/预算门 全部按设计工作。
- **0 成交根因**: `capital_state` peak=101,263.65 (2026-06-25 创立) vs equity=98,067.69 (全现金无持仓) → 回撤 **3.156% ≥ 3%** → AccountCircuitBreaker 动作=**LIQUIDATE** → `playbook_service.execute_instruction` 入口拒绝 ("[OKR熔断] 暂停 Playbook 执行", 09:37 ET 首轮起 40+ 次)。06-25 上线的 OKR 风控规则在生效, 非故障。昨两日盘中权益高于 98,226 (3%线) 故判 NORMAL; 昨日 EOD 平仓后权益定死 98,068 → 今起全天越线。
- **⚠ 自锁状态 (需用户裁决, 不擅自动风控)**: 不能开仓 → 权益无法回到 peak → 熔断永不解除。选项: (a) 维持现状 ( playbook 停摆待自然变化) (b) 重置 peak 基准 (等于豁免历史亏损, 08 终裁框架外的破例) (c) 调 3% 阈值。在裁决前 playbook 新开仓持续为 0。
- 对今日验证影响: 触发侧两门 (连败/低ATR) 拦截日志已验证; MFE_STALL_EXIT (持仓门) 今日无新仓 → 无样本; topup 盘前批 +0 已验证; 盘中窗 07-27 激活计划不变。

5. **观察⑤ — `_et_trade_date(now_et())` 双重转换 bug → ✅ 已修复 (07-24 盘中, 用户授权"明确 bug 可以做修复")**: `_et_trade_date` 契约是"naive CST − 12h → ET 日期", 但读侧传入**已是 ET** 的 `now_et()` → ET 中午前日期=昨天。实锤: 熔断日志"日亏损=-0.39%"精确等于 07-23 已实现亏损 (−$379.21/98,068=−0.3867%)。**单点修复** (`app/utils/timezone.py`): tz-aware 输入走 `astimezone(ET).date()` 分支, naive 输入维持 CST−12h 契约 — 一处改动覆盖全部 13 处 `now_et()` 调用点 (account_circuit_breaker / absolute_circuit_breaker×2 / drawdown_control×2 / playbook risk_circuit_breaker / trade_store×2 / order_executor×2 / feishu_push×3 / weekly_report) + 6 处 aware `position.entry_time` 调用点 (order_executor×5 / position_monitor×2)。安全性论据: 写入侧实测本就用正确标签 (今日 8 笔成交 trade_date='2026-07-24'), 只修读侧无读写分裂; naive 分支零变化。验证: py_compile ✓ + 6 用例 (aware ET/UTC/CST + naive CST 边界) ✓ + 22:49 重启后 venv 生产直测 `_et_trade_date(now_et())='2026-07-24'`、熔断器 `NORMAL 日盈亏=0.00%` (真实当日, 修复前=-0.39%) + 零 Traceback。⚠ 口径注意: 修复后 HALT/REDUCE 档读**真实当日**日亏 — 今起若当日已实现亏损 ≥1%/2% 会按设计触发 (非误报)。回滚: git revert `app/utils/timezone.py` 该 hunk。

**心跳"今日"时区统一 (00:4x 用户问"是否还跨时区, 请统一时区统计") → ✅ 已修**:
- 对账结论: 00:23 北京 (12:23 ET) 心跳"已平盈亏 +$5.52/1笔"**未跨时区** — 与 trade_records 精确一致 (当时仅 GOOG 11:02 ET +5.52 已平; TSM 12:30/NVDA 12:43 在其后)。
- 但发现隐患: 心跳 SQL 用 `date('now','-4 hours')` (UTC−4h) 近似 ET — 夏令时碰巧正确, **冬季 EST 晚间 20:00-24:00 会把"今日"错成次日**, 且与系统唯一真源 `_et_trade_date(now_et())` 口径分裂。
- 修复: `intraday_heartbeat._query_today_trades` 改为 Python 侧 `_et_trade_date(now_et())` 作 SQL 参数 — 与熔断器/写入标签同一口径, EDT/EST 自动。venv 直测: 今日 3 笔 +$39.12 (GOOG+5.52/TSM+5.65/NVDA+27.95), 持仓 5 只 ✓ → 00:53 重启, 下一次心跳起生效。全仓扫描确认 `date('now','-4 hours')` 近似仅此一处。
- 6. **计入观察⑥ (心跳健康探测回退)**: 00:23 心跳"已运行 0s | thread=n/a broker=n/a" = `_query_service_health` 异常被 `_safe_query` 吞掉回退默认值 (服务实际已运行 1.5h)。探测失败原因待查, 非时区问题。

**熔断解锁执行 (22:20 用户裁决: "需要解锁, 在模拟账号中尽快实现交易收集数据" — 知情破例, 选 peak 重置方案)**:
- 操作: `capital_state.peak_equity` 101,263.65 → **98,067.69** (17,911 行全量更新防竞态) → 22:24 `systemctl restart ftseek-us` (加载 DB 峰值, 内存单例无他法)。旧 peak 已存档于本节上文。ACB 绝对熔断事前验证=放行 (07-23 EOD MRK/MA 盈利已清连败计数), 单锁无叠加。
- 结果 (22:26 首个执行周期, 全部 futu_simulate 真实模拟成交+钉钉推送): **8/8 触发指令全部成交** — DA 4 笔: TMO 4股@563.08 / NVDA 13股@207.88 / BABA 21股@112.24 / MRK 24股@131.06; UE 4 笔: TSM@406.59 / LMT@585.32 / RTX@214.24 / GOOG SHORT@320.63。熔断器恢复"账户正常" (回撤 0.00%)。连败封禁 (MSFT/PDD/UNH) 与低ATR门照常拦截, 风控其余维度未动。
- **副作用 (透明记录)**: 本次重启把 topup 盘中窗提前一天激活 — **首个 live 盘中窗=今日 10:30-11:30 ET (非周一)**。首两轮 22:30/22:32 各 +0 (池 11 只, 无方向/被门拦; 与盘前批同因: cautious 下池内无"趋势+回调"标的)。`raw.timing=intraday` 归因字段就位, 效果可按 timing 切分, 口径不受污染。观察② (+0 重试风暴) 在新窗口复现 (~2min/轮), 符合既有记录。原定 07-25 04:20 重启 cron 已取消 (激活目的达成)。
- 后续: 持仓 8 笔由 playbook 退出监控+独立快循环托管, MFE_STALL_EXIT 等三门首次有真实持仓可管; 明日盘前按常态。

### 2026-07-24 (复核批) · 第二模型独立复核 — X2 第四前置 + shadow 异常当日结案
**锚点**: 主文档 `docs/debates/DEB-20260724-monitoring-rigidity-premarket-intelligence.md` §十三 (第 5 轮) + §十四 (第 6 轮作者复核)。用户委托第二模型对当日四文档独立复核: 方法论与证据链通过, C0~C5 无推翻。
1. **X2 第四前置 — SHORT 不隔夜** (立案保留, **叙事已被第6轮同窗复核修正**): 原叙事"日内有效隔夜衰减"不成立 — 7月 SHORT 全时限无效 (日内39.0%/T+1 44.1%/T+3 37.9%, n=59), 65.2%WR 系 6 月旧时代样本。真命题 = SHORT 入场质量门 (待立案); "不隔夜"作为止损性约束保留且更强。
2. **shadow "+$903" 异常 — 立案当日结案** (第6轮对账确认): okr by_strategy 的 playbook_shadow +$903/21笔 实查为 2026-06-22~23 旧影子队列, 30d 滑窗滤掉 06-22 两笔 (−$660.95) 后数字逐项吻合; 非现存正期望口袋, 07-24 起自然 age out。
3. **三件事优先级** (第6轮修正后): ① C2 第二步 **非零风险** (prompt=信号分布源头, v2.0 相变前车之鉴) → 先 scan-eval 离线 A/B, 首夜+模型A/B第2轮收尾后再上 live ② exit_shadow 毕业**基线已失效** (三门改了退出栈) → 窗口从 07-24 新基线重起 ③ 真反向→SKIP 攒 ≥30 样本裁决。

### 2026-07-24 · 退出三门 live + 扩容补量 + 滑点裁决 + 模型A/B — 当日全部逻辑见两份 DEB
**主文档**: `docs/debates/DEB-20260724-monitoring-rigidity-premarket-intelligence.md` (诊断+共识C0~C5+落地记录+topup+提前验收, §十二有全索引) · `docs/debates/DEB-20260724-challenger-model-ab.md` (模型切换评估)
1. **三门 live** (crash-day DEB 第8轮, 绕过 shadow-first 经用户裁决): ① MFE_STALL_EXIT (持仓≥150min且峰值浮盈<0.5%且水下→提前离场, 历史 0/37 零例外) ② 同标的低MFE连败封禁 (触发侧) ③ 低ATR硬门 (atr_pct<2.5% 拒触发)。开关: `mfe_stall_exit_enabled`/`symbol_streak_ban_enabled`/`low_atr_gate_enabled`。
2. **regime/VIX/SPY 落库补全**: DA/UE 直写指令三字段恒 NULL → 盘前 `run_playbook_normalize_vol_buckets` 统一补写。
3. **滑点裁决**: 入场滑点已有基建+50笔数据, 中位 4.6bps < 5bps 预注册线 → 限价单方案降级不建; 补了**出场侧**滑点埋点 (`exit_slippage_bps`, 往返另一半)。
4. **论据三分类记分牌**: `scripts/factor_verdict_scorecard.py` → 16 个"方向错用"(信号真方向反, 如 valuation_expensive IC 0.20/α-0.57%) vs 仅 3 个真该剔 — 回灌 prompt 为第二步待裁决。
5. **扩容补量 topup** (`playbook.topup`, target 25/日): 指令不足时从 16 只流动性池走**与 DA 同链**补足 (质量门+technical确定性方向, 无方向不硬凑); 归因 `raw.source=topup_pool`, **评估必须按 source 切分**。注: 旧 ADR 的 fallback_pool 从未实现, 已由此替代。
6. **链路提前验收三阶段**: `scripts/early_acceptance_check.py` (PRE/OPEN/MID)。PRE 干跑: 三门将拦 11/21, 被拦组 07-23 实亏 -$311(1胜) vs 放行组 -$68(5胜)。
7. **挑战者模型 A/B** (deepseek-v4-pro vs glm-5.2, n=15 record-only): 机械判据 glm 全优 (快29%/零空响应) 但人格更保守 (93% SKIP vs deepseek 敢给 4 个反向) — 与 SHORT 恢复目标冲突, 首轮裁决不切。DA/UE 配置**不统一** (DA=skill硬编码, UE=统一路由表), 收敛待办。
8. **时区双轨坑** (分析时必踩): 遥测 jsonl 的 date=CST, trade_records=ET, 跨表 join 需双日兼容。
9. **方向信心体系** (回应"对标的/方向没底气"): 新建方向命中率记分牌 (工具表) — 7月全部 51.9%≈掷硬币, 但辩论共识分层单调有效 (分歧45.1% vs 一致58.1%)。细切后发现坑: conf≤40 桶 87% 是"弃权"(近中性不该杀), 真反向仅 n=10 (40%/-0.51%) — 立案 `docs/debates/DEB-20260724-debate-divergence-consumption.md`: 真反向→SKIP shadow + 弃权豁免(并入P0.2) + 辩论产物透传落库; 一刀切"分歧→SKIP"已自我否决。

### 2026-07-20 (四批) · 退出策略全链路审计修复 — 2P0 + 2P1

**背景**: 退出策略配置层→分桶匹配→动态SL→退出优先级链→影子评估 全链路交叉比对。

**P0-1 — 7/19 dd=0.8 处方被因子分桶架空** (`config/playbook_horizon.yaml`)
- 通俗: 7/19 把 default 的锁利回撤从 0.4 放宽到 0.8（释放赢家），但 15 个趋势类因子规则仍是 0.5 → 绝大多数带因子标签的单子实际还在用 0.5，处方没喂到病人嘴里。下周五 exit_scorecard 会得出"放宽没效果"的假阴性。
- 修复: 全部 15 个 `trailing_enabled: true` 因子的 `drawdown_from_peak_pct` 0.5→0.8 对齐 default。`trailing=false`（快速证伪类）不动（无 dd 字段，走 profit_override 语义）。
- 回滚: 改回 0.5。

**P0-2 — SHORT 配上多头退出规则（方向分桶漏洞）** (`app/playbook/pipeline.py` `_get_factor_exit_config`)
- 通俗: SHORT 指令的 conditions 若全是 bullish 因子（如逆向做空过热股），第一轮方向匹配空手后，第二轮原样取第一个有规则的 condition → SHORT 拿到 `trailing=true, max_hold=3天` 的多头规则。与 7/7 fix 目标直接矛盾。
- 修复: 第二轮回退增加方向过滤 — 跳过方向相反的 condition，全部跳过则落 default（max_hold=1天，对 SHORT 合理）。direction=None 兼容旧行为。
- 冒烟: 5 场景全过（SHORT+bullish→default / LONG+bullish→因子 / SHORT+bearish→空头规则 / LONG+bearish→default / None→兼容）。

**P1-1 — 影子 baseline 漂移 + fee_cushion 缺失** (`app/playbook/exit_shadow.py`)
- baseline_be1.0_dd0.5 注释称"对照=现行default"，但现行 default 已是 be0.5/dd0.8 → 对照组失真。
- 修复: ① 新增 `baseline_be0.5_dd0.8` 为正确对照，旧 baseline 标记 LEGACY 保留（不破坏 in-progress 数据键名）；② `_variant_effective_sl` 补 `fee_cushion_pct` 参数，保本线数学对齐实盘 pipeline L3961。
- 冒烟: LONG/SHORT fee_cushion 方向正确 + dd 主导时不干扰 + 新旧 baseline 共存。

**P1-2 — rr_ratio=999 与 vol_bucket TP 冲突（注释澄清）** (`config/playbook_horizon.yaml`)
- 999 哨兵（"去TP"决策）实际从未生效：instruction_generator L399 vol_bucket 路径覆写 rr_ratio=tp_multiplier(1.5~2.0)。配置声明与真实行为相反。
- 处理: 加注释澄清真实行为（每单有TP, RR 1.5-2.0），待数据裁决去留。不改代码。

**已生效: 2026-07-20 重启 `ftseek-us.service` (四批)。**

### 2026-07-20 (三批) · 实盘就绪 P0 预备 — B4 数据新鲜度门 + B3 交易环境横幅

**背景**: 富途平台架构深查后确定的实盘前 P0 清单中, 先落地模拟盘可验证的两项。

**B4 — 开仓数据新鲜度门 (降级/延迟行情禁开新仓)** (`app/risk/data_health_guard.py` + `playbook_service.py` + `playbook/config.py` + `futu_lv3_provider.py`)
- 通俗: 行情降级到 Finnhub 时报价延迟 15 分钟, 系统之前会照常按旧价开仓; 现在降级期间/报价过期 (>90s) 一律禁止开新仓, 平仓不受影响。
- 专业: 三处配合 — ① 根因: provider L850 曾把 fallback 报价 `timestamp` 覆写为抓取时间 (把 15min 延迟"洗白", 摧毁 07-19 finnhub C3 修复), 改为仅缺失时兜底; ② 新增 `check_quote_freshness()` (不依赖 bid/ask, 补 `check_quote_health` 盲区: 降级源恰好无盘口 → 整体绕过 health 门), 降级源集合 `DEGRADED_QUOTE_SOURCES` + 时间戳年龄双判定, 兼容 Unix float / ISO 字符串; ③ 接线 playbook 开仓汇合点 (health 门之后), 配置 `entry_freshness_gate_enabled: True` / `entry_max_quote_age_sec: 90.0`, 取价失败维持 fail-open 与既有语义一致。
- 冒烟: 7 场景全过 (finnhub 拦/fallback+兜底盘口拦[health门盲区实证]/futu过期拦/ISO过期拦/新鲜放行/无时间戳放行/配置键)。
- 回滚: `entry_freshness_gate_enabled: False` (一键) 或删 playbook_service 新鲜度门块。

**B3 — 启动交易环境横幅** (`app/core/scheduler_core.py` start 处)
- 启动时醒目打印 AUTO_TRADE_ACCOUNT / ALLOW_REAL_TRADING / execute_mode / acb·preflight_fail_mode; real 模式用 WARNING 级 + "实盘要求均为 closed" 提示。纯可见性, 硬拦截仍在 di_container LIVE 启动断言 (已存在, L158-179: real + fail-open → 拒绝启动)。
- 排查确认: 原计划的 "precheck_fail_mode 切换预案" 大部分已存在 — di_container 已有 LIVE 启动断言, 本批只补横幅可见性。

**已生效: 2026-07-20 重启 `ftseek-us.service` (三批)。**

### 2026-07-20 (二批) · 开盘前全链路隐藏 bug 排查 — 4 处实锤修复

**背景**: 由 `_track_bar_peak_trough` SHORT 方向语义 bug 触发的同类问题全链路深查 (方向语义/数据源语义/键名契约/截断行为), 目标: 消除开盘交易的不利因素。

**B1 — VWAP 硬门数据源语义错配 (P0 行为级)** (`app/data/market_data_router.py` + `pipeline.py` VWAP门)
- 通俗: 实盘启用的 VWAP 方向门, 一直在拿"60天平均价"当"今天的 VWAP"用 — 近期上涨的股票做空恒被拦, 下跌的股票做多恒被拦, 方向性系统误拦。
- 专业: `ctx["vwap"]` 主路径来自 `calculate_from_klines(daily_klines)` (60根日K 全量 cumsum), 07-16 修复的"当日累计 VWAP"假设只在日K不足的 1M fallback 路径成立。修复: router 用 1min K 过滤今日 bar 单独算 cum(tp×vol)/cum(vol), 新增 `ts_ctx["vwap_intraday"]` (门专用, 0=今日bar<5 视缺失) 并覆盖 `ts_ctx["vwap"]`; 门侧优先读 `vwap_intraday`, 缺失 fail-open。
- 回滚: 删 router 当日VWAP块 + 门侧改回 `ctx.get("vwap")`。

**B2 — get_kline HTTP 冷路径旧数据截断 (P0 数据完整性)** (router `count 200→800`, pipeline 两处 `240→800`)
- 通俗: 冷缓存时问券商要 K 线, 要少了会拿到"昨天最旧的一段"而不是"最新的", 导致今天的数据全为空。
- 专业: get_kline HTTP 路径 `start=T-1` 范围含 ~780 根 1min bar, OpenD 对 `max_count<范围总数` 返回**最旧N根** (provider 06-30 注释实证)。count=200/240 时冷路径拿纯 T-1 bar → 今日过滤恒空: 盘中量能门恒 fail-open、#3 bar 峰谷采集失效、`slope_5m` 算的是昨天中午的斜率。修复: 三处 count → 800 (覆盖 T-1+今日全时段, 仍单次请求不多耗配额)。已知残留: push 缓存仅存 300 根, 15:30 后 push 路径今日累计量略低估 (触发窗 14:00 截止, 影响可忽略)。

**B3 — ORB 开盘区间键名契约 (P1)** (router ORB 段)
- 专业: HTTP 路径 bar 键是 `time`, push 路径是 `time_key`; ORB 段只认 `time_key` → HTTP 冷路径下 `opening_high/low` 永不注入 → ORB 策略 `opening_range_ready` 恒 False。修复: `k.get("time_key") or k.get("time")`。

**B4 — 归因 peak/trough SHORT 塞错桶 (P1 归因失真)** (`app/analysis/trade_attribution.py`)
- 通俗: 与 bar 峰谷同款方向 bug — 做空单的"最有利价(最低)"被塞进高价桶、"最不利价(最高)"被塞进低价桶, 被 max/min 直接忽略。
- 专业: SHORT 时 `metadata.peak_price`=持仓期最低价/`trough_price`=最高价, 原实现不分方向 peak→all_highs/trough→all_lows → SHORT 的 D2入场/D3出场/D5锁利维度退化回 entry/exit 近似。修复: 按 direction 分桶。冒烟: SHORT 100→97 (peak95/trough102) 的 best_exit 修复前 97 (假"精准") → 修复后 95。
- 同批新增 (用户手写): `_calc_intraday_vol_ratio` 盘中实时量能比替代原日级 volume_ratio (审查通过, 契约/时区/fail-open 均正确; 已知偏差: elapsed_fraction 线性假设 vs 成交量 U 型分布 → 早盘门偏松/午后偏严, 14:00 截止后无影响)。
- 排查确认无问题: 触发链路方向语义 (漂移作废/SL穿越/VWAP侧向/miss分类)、MFE/MAE SHORT 反算、`_is_instruction_expired` timezone、entry_time 时区 (带-04:00 与 futu ET wall time 对齐)、volume/time 字段契约、#6 _tr/_meta 收敛。
- **已生效: 2026-07-20 14:16 CST 重启 `ftseek-us.service` 完成** (启动日志验证无导入错误, 对账/订阅/Watchdog 均正常; 代码已验证: py_compile 3 文件 + 量能比/归因双向冒烟全通过)。

### 2026-07-20 · 参数冻结下的结构性优化批次 (#1/#2/#3/#4/#6/#7, 全部不碰实单退出参数)

**背景**: 积累期参数冻结 (避免 config_version 断点), 本批只做归因补齐/测量精度/灾难兜底/可观测性, 零行为变更类改进一律 record-only。

**#1 — missed 指令 skip_reason 归因黑洞补齐** (`app/playbook/shadow_tracker.py`)
- 通俗: 94 条"没做成的交易"不知道为什么没做成, 现在每条都有说法。
- 专业: shadow_tracker 两个写 `outcome='missed'` 的分支不写 skip_reason → 代码修复 (仅原 reason 为空时补标, 不覆盖 auto_execute 具体拒因) + DB 回填 94 条 (81 no_trigger + 13 trigger_no_execution)。剩余空归因 = 0。

**#2 — 触发门反向选择周度判分器** (新建 `scripts/trigger_gate_report.py`, 只读)
- 通俗: "没追到的车是不是比追到的更好"固化成周度报表。
- 首跑结论: missed 组 fwd_return +0.915% vs executed +0.249% (缺口 0.666pp), **集中在 BUY 侧** (+1.152pp), SHORT 侧健康 (−0.219pp); 机会成本累计 $12,819。内置判读: 缺口>0 且双向同号稳健 且持续≥2周 → 下一个 shadow 变体应为"触发条件放宽" (目前双向不同号, 继续观察)。

**#3 — peak/trough 1min bar 并行采集 (record-only)** (`app/playbook/pipeline.py::_track_bar_peak_trough`)
- 通俗: 峰谷价以前靠"每轮瑞一眼报价", 两眼之间的最高/最低点全丢 (MFE 系统性低估约一半)。现在同时用 1 分钟 K 线高低点并行测一份。
- 专业: futu LV3 `get_kline(K_1M)` (300s 缓存, 基本不耗配额) 取入场后 bar 的 max(high)/min(low), 单调更新 `metadata.peak_price_bar/trough_price_bar`, 并进 `exit_telemetry`。**不参与任何退出决策**; 周度对比 bar 版 vs quote 版差距后再评估切换 (切换=行为变更, 需开新 config_version 窗口另行批准)。
- **同日人工审阅修复 (方向语义)**: 初版不分方向一律 peak=max(highs)/trough=min(lows), SHORT 时与 quote 版既有语义 (peak=最有利价, SHORT 取最低; trough=最不利价, SHORT 取最高) 完全反转 → SHORT 的 bar 版 MFE 会符号反转, 周度对比产生"bar 版更差"假象。修复: helper 加 `direction` 参数 (默认 LONG 向后兼容), SHORT 分支 peak=min(lows)/trough=max(highs)。双向冒烟通过; 周日修复, 盘中未跑过, 无脏数据需清洗。
- 回滚: 删 monitor 循环里 `_bar_track_new` 一行 + helper。

**#4 — grace 5min 不再完全豁免, 保留 4% 灾难兜底** (`pipeline.py::monitor_playbook_sim_exits`)
- 通俗: 入场后 5 分钟内以前是"完全不看盘", 闪崩 10% 也不管; 现在闪崩超 4% 照样斩。
- 专业: grace 期 `continue` 前先查是否击穿 `cooldown_sl_pct` (4%) 兜底线, 与规则 0 "冷却期放宽而非豁免"语义对齐。非参数变更 (4% 是既有冷却参数), 属灾难兜底补洞。

**#6 — `_close_trade_record` 读写收敛 (行为零改变)** (`pipeline.py`)
- 通俗: 平仓时对同一笔交易反复查数据库 10 次、写 3 次, 现在查 1 次、写 2 次, 内容一模一样。
- 专业: 函数开头读一次行快照 `_tr` + 解析一份 `_meta`, 各块 (MFE反算/exit_shadow/holding_hours/pnl_pct兜底/telemetry/ghost_cause/归因/推送) 顺序加工同一 dict; telemetry+ghost 合并为主 UPDATE 前一次写回, 归因写回保留 (commit 时点不变)。消除多次读改写的互相覆盖风险面。
- 验证: 临时 DB 全链路冒烟 (MFE 反算 5%/MAE 2%/telemetry 含 bar 峰谷/instruction 回写 win+MFE美元/open_positions 删除/归因落盘) 全部通过。

**#7 — void 率趋势接入 okr_status** (`scripts/okr_status.py` 新增 `void_trend` 段)
- 口径: void/(win+loss+void) 周度分组, ≥10% 打 ⚠️。首跑: W25 25.0% / W26 14.3% / W27 25.9% / **W28 2.9%** — 幽灵仓根因修复生效的趋势证据; 若回升 = 执行/对账链路又在产幽灵仓。

**未做 (有意为之)**: #5 broker 侧止损底线单 — 切 LIVE 前头号阻断项, 保持在毕业 checklist, SIM 阶段不做。

### 2026-07-20 · DEB-20260720 盲区审计修复批次: 保护毕业数据质量 + 零成本防真金 (修复 M/5/7/6a/3b)

**辩论锚点**: `docs/debates/DEB-20260720-blindspot-risk-audit.md` (三轮收敛)。当下 SIM 阶段做 5 项 (M+5+7+6a+3b), 全部保护毕业判定数据质量或零成本防真金; 修复 1/6b 为切 LIVE 头号阻断 (本批不做); 4/3a/2 延后。

**关键洞察 (元风险拆两半)**: 毕业判定分两个维度, 对退出参数污染的敏感度不同 —
- **IC 侧 (`IC(T+3)>0.05`) 天然免疫**: IC 基于 forward return = 入场价→N日后日线价, 是**入场锚定、退出无关**的量。改 time_exit/drawdown 不动 forward return。
- **PnL/胜率侧 (cap_reduction 45%胜率+正PnL) 受污染**: 已实现 PnL = 入场 edge × 退出执行。退出参数一变, 同一批入场信号的胜率就变。
→ 最优解不是"停止探索", 而是**打标 + 按版本取样 + 探索走 shadow** (修复M)。

**修复 M (P0) — 退出参数 config_version 打标 + 毕业统计按版本窗口 + 探索走 shadow**
- 通俗解释: 以前"边积累数据边调退出参数"就像"边煮饭边换锅", 最后不知道哪锅饭是哪个参数煮的, 毕业判定的胜率被多套参数搅浑。现在给每笔平仓记录盖一个"退出参数版本印章", 毕业统计只数**当前印章**的样本, 参数探索全部放到"影子"里跑 (不动真单)。
- 专业解释: (a) `PlaybookConfig.get_exit_config_version()` 对会改变已实现 PnL 的退出参数 (factor_exit_rules / vol_buckets / cooldown / early_tight / fee_cushion) 算稳定短哈希; (b) `_close_trade_record` 平仓时把版本写进 `metadata_json.config_version`; (c) `scripts/okr_status.py` 新增 `graduation_sample` 段, 用 `json_extract` 只取当前版本窗口的已平仓样本算胜率/PnL; (d) 参数探索继续走 `exit_shadow` (record-only)。
- 收益: 既保护毕业判定不被多套参数污染 (PnL 侧可比), 又不牺牲探索速度 (shadow 并行)。复用现有 metadata_json + shadow 框架, 零新基建 (未加 DB 列)。
- **联动: 修复 B (早期紧SL) 收编为 shadow-only** — `early_tight_sl_enabled: true→false`, 改由 `exit_shadow.EARLY_TIGHT_VARIANTS` (`early_tight_30m_1pct`) 采集反事实 `counterfactual_saved_pct`。避免又引入一个 config_version 断点。
- 回滚: 打标不可逆但无害 (只多一个 metadata 字段); 窗口化统计改回全样本即删 `graduation_sample` 段; early_tight 实单化改回 `enabled: true`。

**修复 5 (P1) — reconcile 幂等失效**
- 通俗解释: 对账用的"上次状态存档"文件有时会被清空成 2 字节 `{}`, 导致每次重启都以为"全是新持仓"重复告警/重复对账。根因是写文件时如果对账中途异常, 会用空存档覆盖好存档, 且异常被 debug 日志吞了看不见。
- 专业解释 (`app/positions/reconciler.py`): `_save_signatures` 增加防覆盖 — 本轮签名为空但历史非空时跳过写入 (保留好状态); 异常从 `logger.debug` 升级 `logger.warning`; `_load_signatures` 对"文件存在但空/非法"显式 WARN (幂等失效可见)。
- 收益: 消除每次重启的重复告警/重复对账, 对账幂等恢复。回滚: 移除防覆盖分支。

**修复 7 (P1) — NULL 口径污染绩效**
- 通俗解释: 统计"真实成交笔数"的 SQL 有个 NULL 陷阱 —— `exit_reason NOT IN(...)` 遇到 NULL 值会判成"未知"从而**漏掉**该行, 幽灵单反而被算进绩效。而毕业判定就是吃这个绩效。
- 专业解释: 全仓 6 个 web_dashboard 文件 (analytics/dashboard/risk/trade/data_service/thesis) 都存在同一模式 —— 正确的 `COALESCE(exit_reason,'') NOT IN(...)` 后面又跟一个冗余且 NULL-unsafe 的 `AND exit_reason NOT IN(...)`。系统性删除冗余子句, 保留 COALESCE 版作为唯一真相 (16 处 COALESCE 守卫全部保留)。
- 收益: 毕业判定输入干净 (PHANTOM/ORPHAN 不进胜率分母), 与修复 M 同一目标 (保护毕业数据) 的两面。回滚: git revert。

**修复 6a (P0 提前到现在) — LIVE 启动断言 (零数据扰动)**
- 通俗解释: 现在风控检查异常时是"放行开仓"(fail-open, SIM 期合理)。但万一某次误切真账户时忘了改成"拒绝开仓", 就会在真金上裸奔。加一道启动断言: 真账户模式下如果 fail 模式不是 closed, **直接拒绝启动**。SIM 模式完全不受影响。
- 专业解释 (`app/core/di_container.py`): 仅在 `_auto_account=='real'` 分支内, 断言 `risk.acb_fail_mode==closed && risk.preflight_fail_mode==closed`, 否则 `raise RuntimeError` 拒绝启动 (配置读取失败也拒绝)。
- 收益: 零成本防止积累期误配置/误切 real 时 fail-open 裸奔。回滚: 移除断言块。

**修复 3b (P0 起逐步) — 平仓失败可观测性**
- 通俗解释: 以前"为什么平仓失败/仓位卡住"这一半是黑暗的 (异常被吞进 debug 日志)。平仓失败=仓位卡住无止损保护, 是切实盘后最致命的场景。现在平仓失败升级为结构化 error + 飞书 HIGH 告警。
- 专业解释 (`app/playbook/pipeline.py`): 新增 `_alert_close_failure(symbol, env, reason, detail)` (WARN 日志 + push HIGH, 按 symbol+日+reason 去重防刷屏), 接入正常退出/SL兜底/EOD兜底三条平仓链路的所有卡住点 (BROKER_EXCEPTION / CLOSE_FAILED / ORDER_NOT_FILLED / SL_* / EOD_* 连续失败≥3)。record-only, 不改平仓行为。
- 收益: 补上"为什么平仓失败"这一半黑暗; 当下 SIM 也能先积累平仓失败画像。回滚: 移除 helper 调用。

**验证**: py_compile 全过 (12 文件); `get_exit_config_version()` 实测产出稳定哈希 `exit-4bc8f6797088` 且缓存一致; reconciler 防覆盖分支实测跳过写入并 WARN; `okr_status.py` graduation_sample 窗口化实测输出 (Closed:0 = 打标从今起, 符合预期); exit_shadow early_tight 变体实测触发 + 反事实计算正确; pytest 9 项失败经 `git stash` 对照证实为**基线遗留** (test fixture 缺 `mfe_pct` 列 + intraday trigger mock 设置), 与本批改动无关, **零新回归**。

**本批不做 (切 LIVE 前)**: 修复 1 (broker stop-limit 冗余止损底线) + 修复 6b (移除 SIM→REAL 账户 fallback)。**延后 (P1-P2)**: 修复 4 (半日/EOD 硬编码读交易日历) / 3a (删死 AuditLogger) / 2 (删 HSI 死逻辑 + 补 SPY 急跌维度)。

### 2026-07-19 · 退出侧结构性修复: 释放赢家 + 方向全错快速脱身

**核心诊断** (数据实证, 与 7/12 handoff 一致):
- 信号≈摄硬币偏正 (50.8% 持到收盘命中, +0.37%/笔) → DA 不是问题。
- **漏血在退出**: SL_TRAIL_LOCK 15笔 100%WR 但 avg_win=$27.59 (MFE 1.48% 仅捕获一半) vs EOD 赢家 avg=$96.74。
- **根因**: `drawdown_from_peak_pct=0.4` 在日内噪声底附近触发, 系统性截断赢家 → avg_win 上不去 → R:R 被压。
- **TIME_EXIT 11笔 0%WR**: MFE 全部<0.5% (4笔=0), 方向从未兑现, 但等 4-6h 才触发 TIME_EXIT。

**修复 A: `drawdown_from_peak_pct` 0.4 → 0.8** (`config/playbook_horizon.yaml` L49)
- 通俗解释: 以前浮盈从峰值回撤 0.4% 就锁利平仓, 但美股日内波动随便就 0.5-1%, 0.4% 等于“刚赚一点就被噪声晃出来”。现在给 0.8% 呼吸空间, 让赢家跑完趋势再锁。
- 专业解释: trailing stop 的 drawdown trigger 从 40bp 放宽到 80bp, 降低 trail_lock 触发频率, 提升 avg_win, 从而抬 R:R。
- breakeven_at_pct=0.5 不动 (仍早保本, 亏不了), 只放宽锁利回撤容忍。
- 回滚: 改回 0.4。判分: 下周五 exit_scorecard R:R + avg_win 对比。

**修复 B: 早期紧SL** (`unified_config.yaml` + `pipeline.py` 规则0.5)
- 通俗解释: 以前方向做错了, 要等到亏 2% 才止损 (或者等 4-6h TIME_EXIT), 像“温水煮青蛙”。现在入场后 30 分钟内, 如果价格反向走 1% 就立即止损 — 快速认错, 小亏出场。
- 专业解释: 入场后 30min 内 breakeven 未激活 (peak_profit < 0.5%) → SL 从正常 2-2.5% 收紧到 1%. 方向对→浮盈→breakeven激活→本规则自动失效; 方向错→1%SL快速触发SL_INITIAL。
- 与 cooldown_sl_pct(4%) 互补: cooldown 防开盘瞬间秒杀(放宽); 本规则防方向全错慢性失血(收紧). cooldown 优先。
- 退出原因仍为 SL_INITIAL, 无新标签, 复用现有基建。
- 回滚: `early_tight_sl_enabled` 改 false。判分: TIME_EXIT 笔数下降 + SL_INITIAL 早期笔数/损失。

**为什么不用 15min 硬时间门 (DIRECTION_FAIL)?**
- 15min 太短, 无法区分“方向错”和“慢热型” (EOD 赢家前 15min 也可能浮亏)。
- 硬时间门 = 另一种“砍赢家”, 与 handoff 诊断方向相反。
- 早期紧SL 更智能: 用价格行为 (1% 反向) 而非时间来判断, 且方向对时自动失效。

**验证**: py_compile ✓; PlaybookConfig 加载确认 drawdown=0.8 / early_tight=true/30min/1% ✓。

---

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
