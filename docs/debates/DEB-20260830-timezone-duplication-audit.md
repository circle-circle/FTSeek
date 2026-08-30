# 议题: 全链路时区混乱 + 重复轮子审计 (用户令"请再检查整个项目全链路是否还存在时区混乱, 重复轮子的问题")

## 状态

- [ ] 进行中
- [x] 部分已决策 (2026-08-30 用户令"能执行修复的先做修复": TZ-1/TZ-2/TZ-3/T3-T6钩子已执行, R-1/R-2/R-3 仍待裁)
- [ ] 已决策
- [ ] 已验证
- [ ] 已关闭

> 创建: 2026-08-30 (第 1 轮, 本仓 agent)
> 方法: 两个并行探索代理全仓扫描 (排除 venv/node_modules/.git/data/logs/__pycache__) + **对全部头部声称逐条活性实证** (只读)。代理报告的每一项在采信前均经本机复核: 不仅验证代码存在, 更验证调用方是否在产、历史数据量、cron 是否调度 — 防止把死码当活病灶 (本仓 DEB-20260830 E2 的教训: 病灶定位不看活性会错挂)。
> **本轮零代码变更, 全部登记待裁决。**

---

## 一、时区审计结论

### 总量画像

`app/` 内 `datetime.now()` 445 处 / `date.today()` 44 处 / SSOT 引用约 426 处; `scripts/` (非 archive) `datetime.now()` 281 处。绕过 SSOT 直连 pytz/ZoneInfo 约 22 处。量大但多数无害 (本地日志时间戳等), 下列分级只列有行为影响的。

### A. 在产核心链 — 干净 (此前 DEB"已排除"结论对核心链成立)

scheduler_core (交易日/冷却/小时判定 :837/:1185-1186/:1501-1505/:2721-2723 全走 `now_et`)、playbook 生成主路径、退出监控、trade_recorder 主路径 (`trade_date=get_trade_date()` ET + `entry_time=now_et()` aware)、影子 jsonl 主写入点 — 全部统一 ET 口径, 走 `app/utils/timezone.py` SSOT (zoneinfo→pytz→固定偏移三级回退 + `compare_safe` 混比防护)。

### B. 在产链上 2 处真实瑕疵 (此前未登记, 本轮新开列)

**T1. 熔断器时区错 + 熔断窗口从未生效 (本轮最重要发现)**

`app/playbook/risk_circuit_breaker.py`:
- **时区错**: `:111-121` 用 `date.today()` (服务器 CST) 判停止期窗口与 `stop_until` 赋值 — CST 午夜 = ET 正午, 停止窗口在 ET 盘中翻日, 实际窗口比设计短/长约半日。
- **更深一层 (本轮核实发现, 时区错被它掩盖)**: `_stop_until` 是**纯内存实例字段** (`:64`), 而唯一调用方 `pipeline.py:3060-3061` 每轮 `RiskCircuitBreaker(db).check_status(...)` **新建实例** → `_stop_until` 恒 None → **"连续 5 笔亏损停开仓 1 天"的窗口从未跨调用生效过**。
- **实际行为 ≈ 设计意图纯属巧合**: 当前保护全靠 `:121` 之前同函数内的"最近 N 笔连续亏损"实时 DB 条件 (该查询每次调用现算, 不依赖实例状态), 连续亏损在场就持续拦 — 但熔断"锁存"语义 (条件消除后仍停满 1 天) 从未实现。
- **危害评级**: B+ (风控组件语义与实现不符; 当前不亏钱靠的是实时条件恰好兜住, 属"带病运行"而非"已漏风")。
- **修复方向 (待裁)**: `_stop_until` 持久化 (DB 或 metadata) + `date.today()` → SSOT 的 `now_et().date()`。开仓侧风控, 不属退出参数冻结范围, 但按纪律先登记。

**T2. 财报日历边界用 CST 日期**

`app/data/earnings_calendar.py:340,422,488` — "财报前 3 天"门 (reentry 证伪门 + screener 排除门消费) 的窗口边界用服务器日期, ET 午后翻日 → 窗口边界可偏 1 天 (盘中后半段多/少拦约半天)。在产, 影响轻 (门是缓冲带性质)。

### C. 确凿但休眠 (代码是错的, 路径已死或近零使用 — 登记不修, 复活前必修)

| # | 位置 | 病灶 | 本轮活性实证 |
|---|---|---|---|
| T3 | `app/coordinator/dual_mode_params.py:126,150` | 日自动交易额度按 CST 午夜重置 (ET 正午盘中翻倍额度) | 消费方为主线链 order_executor; **主线最后一笔成交 2026-06-30, 休眠两月** |
| T4 | `app/web_dashboard/order/audit.py:276,281` (+executor.py:186,653,1095 同病) | 手动单写 naive CST 进 trade_records, 与主路径 aware ET 同表口径分裂; 下游按"entry_time 前 10 位=ET 日"约定 (`pre_trade_gate.py:187` 等) 会差一天 | 历史手动单仅 **1 笔** (2026-07-10) |
| T5 | `app/review/attribution_scheduler.py:78-79` | 归因调度 CST 日期 vs ET 平仓日错位, ET 午后平仓系统性漏 | **零消费方, 死码** |
| T6 | `scripts/breakeven_shadow_settle.py:266,342` | partial_day 判定 ET 日期 vs CST 日期混比 | 不在 cron, 无调度 |
| T7 | `app/playbook/feishu_push.py:319` | cushion 默认 0.15 vs yaml 真值 0.4 (降级路径阈值漂移) | yaml 在产时读到 0.4; 与 DEB-20260830 第13轮前批 UE 默认值对齐同款病 (可顺带同批修) |
| T8 | 6 处固定 `-4` 偏移兜底 (position_sync.py:1619 / overnight_hold_shadow.py:58 / overnight_arm_report.py:35 / breakeven_shadow_settle.py:88 / premarket_reprice_shadow.py:65 / stall_entry_shadow_tag.py:76) | 冬令时错 1h | 均为 zoneinfo import 失败才触发的兜底分支, 主环境不触发 |

### D. 治理债 (口径对但不走 SSOT — 不修, 登记)

- 22 处直连 `pytz US/Eastern` (market_regime_filter.py:29 / scheduler_triggers.py:29 / intraday_scorer.py:28 / market_data_router.py:629 / pure/orb.py:74 / premarket_invalidation.py:76 / signal_pipeline.py:325 等) — 口径正确, 复制而非复用。
- `app/llm/agent_bridge.py:390,572,747,849` naive CST 时间戳全无标注 — **口径最乱的生产文件** (主线链休眠中, 复活前须先治)。
- `datetime.now()` 445 处 — 多数无害, 但 aware/naive 混算实锤会炸 (pipeline.py:7674、shadow_tracker.py:575-576 曾抛 TypeError, 已有 compare_safe 防护但覆盖不全)。

---

## 二、重复轮子审计结论

已知四项 (E10 退出双轨故意保留 / 三套调度通道 / LLM 配置仓内外双轨 / E11 双合成病根已修) 现状全部属实, 不复议。新发现按危害排序:

### R1. DB 层最发散 (🔴)

- **70+ 文件直连 sqlite3**, 帮助类四套: `app/playbook/db.py` / `app/storage/trade_store.py` / `app/storage/db_pool.py` / `app/core/db_utils.py`。
- **llm_audit.db 路径三处独立定义** (db_pool.py:49 / db_utils.py:142-144 / audit_db.py:40-43) — 本轮实测三处收敛同一文件 (在用 1MB), **治理债非行为 bug**, 但"改一处漏两处"是定时炸弹。
- **data/ 下 28 个 .db, 至少 7 个 4KB 空壳** (playbook.db / kline.db / market_data.db / news_us.db / ftseek-us.db 连字符版 / pdt_guard.db / playbook_instructions.db 等); db_pool 登记仅 9 个, 墓地库无审计。仓根 `ftseek_us.db` 0 字节诱饵已知 (DEB-20260830 台账)。
- **危害**: 路径解析双轨 + 墓地库难审计。修复须先逐个查写入方再动 (防误删活文件), 不宜快动。

### R2. 影子写入无统一器 — E13 病灶的结构性土壤 (🟠)

- `_append_jsonl` 四处各自实现 (exit_shadow.py:284 / sltp_verify_shadow.py:57 / scripts/universe_expansion_shadow.py:105 / llm_universe_scan.py:204); **60+ 处裸 `open(...,"a")`** (pipeline.py 内 12+ 处, scripts 40+ 处); 幂等/去重每处自造。
- **关联**: DEB-20260830 E13 "影子覆盖率 42% 成因三轮定位不了" — 每个影子通道自管写入与容错, 哪个环节静默吞了写入无从统一排查。统一写入器可一次收编, 顺带把 data_freshness_watchdog 的人工登记制升级为自动发现。
- **冻结期兼容**: 纯基础设施, 不碰行为。

### R3. 配置加载 6 条路径 (🟡)

config_loader (主) + config_container (自包一层, 自注"替代 config_us") + playbook 自建 mtime 缓存 (绕开 ConfigHotReloader) + config_us 遗留 (仅 pre_market_check.py 消费, 近死) + web_dashboard 三处硬编码直解 yaml (routes/trade.py:672 / routes/system.py:108 / mode_switch_service.py:415-438)。热重载各管各, 默认值散落 (cushion 0.4/0.15/0.0 三处打架即此病, 见 T7)。

### R4. 监控双轨 (🟡)

- 停更检测: `scripts/liveness_probe.py` (db_max/json_ts/行为探针) vs `scripts/data_freshness_watchdog.py` (mtime + D1/D2 自动枚举) — 同一问题两套 watchlist 两套告警口径, 哪个先报取决于 cron 排期。
- 同进程心跳两套: trading_monitor (main.py:648) + observability/watchdog & intraday_heartbeat (main.py:509)。

### R5. 轻度重复 (⚪ 登记)

- ATR 三套: compute_indicators_full.py:59 / yahoo_provider.py:237 / alphavantage_provider.py:658 (后两者是 provider 各自实现, 有边界理由)。
- VWAP 算法两处复制: pipeline.py:4350 vs scripts/phase1_gate_backtest.py:99 (回测脚本注释自认"同源", 改动不同步风险)。
- 滑点三套 (校验/模拟注入/真实统计, 目的不同, 可接受)。
- vol_ratio 同名异义: pipeline.py:4215 (真实量比) vs shared_kernel.py:289 (时段振幅常数表) — 后者在产, 易混淆。
- unified_service.py 是 model_call.py 薄包装, 命名重复无实害。

### R6. 干净区 (明确未发现)

- 指令去重/幂等: `save_instruction` 唯一定义 (db.py:314, ON CONFLICT 优先级阶梯 + RFC-066 仲裁), 全部 5 个写入方走它 — **无重复**。
- LLM 客户端: model_call.py SSOT 成立, scripts 无绕过直调 (da_glm_shadow.py:183 / challenger_model_ab.py:138 import 仓外 skill llm_synth, 成本不经 llm_audit 属已知架构)。
- 费用估算: fee_model.py SSOT 成立 (estimate_fee/estimate_round_trip), 8+ 消费方均 import, 无并行重算; 仅 cushion 口径双轨 (yaml 0.4/0.2 vs fee-aware 真算覆盖, DEB-20260830 E1 第 2 轮已核"垫层非成本"且 fee_aware 在产覆盖)。

---

## 三、可选方案 (供裁决, 均标冻结期兼容性)

| # | 方案 | 级别 | 冻结期兼容 | 回滚 |
|---|---|---|---|---|
| TZ-1 | **T1 熔断器**: `_stop_until` 持久化 + `date.today()`→`now_et().date()` | A (风控语义修复) | **兼容** — 开仓侧风控, 非退出参数 | revert + 清持久化字段 |
| TZ-2 | **T2 财报边界**: 三处 `date.today()`→ET 日期 | B | 兼容 | revert |
| TZ-3 | **T7 顺带修**: feishu_push cushion 默认 0.15→0.4 对齐 yaml | A (与前批 UE 默认值同款同批) | 兼容 | revert 单行 |
| R-1 | **统一影子写入器**: 新建 `app/observability/shadow_writer.py`, 60+ 处分批收编; 顺带 E13 覆盖监测自动化 | B (基建, 分批) | 兼容 | 分批 revert |
| R-2 | **空壳 .db 治理**: 7 个 4KB 空库逐个查写入方 → 确认死库后移 `data/archive_dead_db/` (不删除, 可逆) | B | 兼容 | 移回原位 |
| R-3 | **llm_audit 路径三定义收敛**: 统一 import db_pool 一处 | C | 兼容 | revert |
| 不做 | T3-T6 休眠路径 — 登记不修, **复活前必先修** (在本文档留钩子) | — | — | — |
| 不做 | R4 监控双轨合并 — 两者口径不同有互补性, 先观测 (watchdog 自带 D1/D2 枚举 vs probe 行为探针), 合并属优化非纠错 | — | — | — |

**执行排序建议**: TZ-3 (单行, 顺前批) → TZ-1 (风控语义, 在产) → TZ-2 → R-2 (可逆移动) → R-1 (分批大工程) → R-3。

## 四、关键分歧 (供辩论)

1. **T1 熔断器"锁存"语义该不该要?** 当前实现 = "连续亏损在场就拦, 消失即放" (实时条件); 设计 = "触发后停满 1 天" (锁存)。若用户裁"实时条件就够了", 修复只剩时区一行; 若裁"要锁存", 须加持久化。**这不是技术问题, 是风控哲学问题** — 锁存防的是"亏损刚缓解立刻又开仓"的报复性交易, 实时条件防不住这种。
2. **R-1 统一影子写入器值不值得做?** 收益: E13 类"写入静默丢失"从逐处排查变统一可测; 成本: 60+ 处分批改动, 每批都要防行为漂移。反方: E13 的 42% 覆盖成因可能根本不在写入器 (第 9 轮指向"未到该分支"), 统一器治不了那个。
3. **休眠时区 bug (T3-T6) 要不要"复活前钩子"?** 方案是给每个文件头加一行警示注释 (同 E12 锚点注释先例)。反方: 4 个文件 4 行注释, 防的是"有人复活死码时不知道要先修"。

## 五、预注册判据 (防挪门柱)

- **TZ-1 (若裁做)**: 修后次日 — ① 构造连续 5 亏损 (测试) → `_stop_until` 落盘非空; ② 重启进程 → 窗口仍在 (跨调用锁存生效); ③ 窗口判定用 ET 日期 (测试钉断言 ET 午夜前后行为); ④ 生产零行为变化 (窗口从未触发过, 修的是休眠语义激活)。
- **TZ-2 (若裁做)**: 修后 10 交易日 — 财报门拦截/放行记录与财报日历日期逐条对齐, 无 ±1 天偏移样本。
- **TZ-3 (若裁做)**: yaml 缺键时 feishu 判定阈值 = 0.4 (测试钉), 顺带断言三处默认值一致。
- **R-2 (若裁做)**: 移动前每个 .db 的写入方清单落档; 移动后 10 交易日 data_freshness_watchdog 零新告警。
- **R-1 (若裁做)**: 每收编一批 — 该批影子文件行数环比零下降 (写入不丢), watchdog 注册项同步迁移。
- **通用**: 任一判据到期无数据 = 判定不过, 不得延期。

## 六、不建议做的 (对抗性自审)

- **不建议**趁审计之势大扫除 pytz 22 处直连 — 口径全对, 纯审美工程, 冻结期内徒增回归面。
- **不建议**删任何 .db 前不做写入方考古 — 仓根 0 字节诱饵的教训: 看着像垃圾的可能是别人故意放的。
- **不建议**把 T3-T6 顺手修了 — 修死码无法用生产行为验证, 只能靠测试; 而测试钉会给人"路径活着"的错觉。登记 + 钩子注释比修更有性价比。
- **不建议**合并 R4 双轨监控 — 两套口径互补 (mtime 传感器 vs 行为传感器), 合并是优化不是纠错, 冻结期内不动。

## 七、诚实边界 (实测确认 vs 推演)

**已本机实证**: T1 全部 (代码逐行 + 调用方 pipeline.py:3060 每轮新建实例 + `_stop_until` 无持久化搜索确认); T3-T6 活性 (主线成交 MAX=6/30、手动单 1 笔、attribution_scheduler 零消费方 grep、cron 零调度); R1 数字 (70+ 文件 grep、三路径实测同文件、28 个 .db ls + 7 空壳); R2 (60+ 裸 open grep); T7 (代码 + yaml 真值); 代理报告的行号抽查全对。

**未实测 (推演/采信代理)**:
- T2 财报边界的**实际偏移样本**未逐笔回放 (只确认代码用服务器日期, 未构造"ET 午后财报标的"实例验证拦截偏移)。
- T8 六处兜底分支从未被触发过 (无法实测 import 失败路径), "主环境不触发"是读码结论。
- R1 的 70+ sqlite3.connect 中未逐个分类哪些走帮助类哪些裸连 (抽样 10 处均为裸连或轻包装)。
- R5 ATR 三套的**数值一致性**未实测 (只确认三处定义存在, 未对比同一标的输出)。
- 时区总量数字 (445/44/426/281/22) 来自代理 grep, 我抽查 5 处样本全对, 未全量复核。

## 决策

待裁 (本轮零代码变更)。

## 执行项

- [x] 四、关键分歧 1-3: 分歧1 (T1锁存语义) 经用户"能执行修复的先做修复"按方案表原样执行 (含持久化); 若后续裁"实时条件就够了", revert = 单 commit。分歧3 (T3-T6钩子) 已执行。**分歧2 (R-1 统一影子写入器) 仍待裁**。
- [x] TZ-3 (2026-08-30 第2轮执行): feishu_push 三处兜底 0.15→0.4 + 4 颗测试钉 (见第2轮)
- [x] TZ-1 (2026-08-30 第2轮执行): ET 日期 ×3处 + playbook_risk_state 落盘 + 7 颗测试钉 + pipeline:3062 死日志附带修复 (见第2轮)
- [x] TZ-2 (2026-08-30 第2轮执行): earnings_calendar 两处 + data_collector:624 (screener门真身) 锚 ET + 3 颗测试钉 (见第2轮)
- [x] T3-T6 钩子注释 (2026-08-30 第2轮执行): 4 个文件头警示, 符号名引用防行号漂移
- [ ] R-2 (若裁做): 7 空壳库写入方考古 → 可逆移动 → watchdog 验证
- [ ] R-1 (若裁做): shadow_writer 设计稿先落 DEB 再动工 (60+ 处分批)

## 验证项

- [x] TZ-1 ①②③: 测试钉内过 (连亏5→落盘非空 / 新实例窗口仍在且实时条件消失仍拦 / ET 23:30 仍停+次日 00:30 放行); ④论证成立 (组件在周报路径仅记录告警, 锁存窗口历史从未触发)
- [x] TZ-3: 缺键判定阈值=0.4 钉内过; 三处默认一致钉内过
- [ ] TZ-2 10 交易日生产观察窗 (2026-08-31 起, 至 ~2026-09-14): 财报门拦截/放行记录与财报日历日期逐条对齐, 无 ±1 天偏移样本 — **到期结算**
- [ ] R-1/R-2 按第1轮预注册判据 (待裁决后)
- [ ] 关闭条件: T1/T2 有明确裁决 (已执行, 待 TZ-2 观察窗结算); R1/R2 有裁决; T3-T6 至少有钩子或明文搁置 (钩子已落)

## 讨论记录

### 第 1 轮 (2026-08-30 20:15, 本仓 agent 全仓审计 — 用户令"请再检查整个项目全链路是否还存在时区混乱, 重复轮子的问题" → "需要 (落 DEB)")

**方法**: 双代理并行扫描 (时区 / 重复实现), 代理报告回来后**对全部头部声称逐条活性实证**才采信 — 期间修正了两处差点误报的: ① `dual_mode_params` CST 重置看着是活 bug, 核实消费方后主线链 6/30 起休眠两月 (降级为 T3 休眠); ② llm_audit 三处路径定义看着是分裂 bug, 实测三处收敛同一文件 (降级为治理债 R-3)。反向也有一处: 熔断器时区错 (代理报 T 级) 核实调用方时发现更深的 `_stop_until` 无持久化 — **时区 bug 反而被这个掩盖**, 升级为本轮最重要发现 T1。

**与既往 DEB 的关系**: 时区核心链结论与 DEB-20260830 一节"不复议清单"(时区已排除) 不冲突 — 那条的审计范围是 playbook 执行/退出链, 本轮确认该范围内干净; 新发现全在外围 (熔断器是外围风控组件, 财报日历是数据层)。重复轮子四项已知项与 DEB-20260830 E10/E11/E13 相互印证, 本轮补的是量化 (70+/60+/28 个) 与新病灶 (配置 6 路径 / 监控双轨 / 墓地库)。

**本轮教训登记**: 代理报告的"确凿 bug"必须过活性关 — 代码错 + 路径死 = 登记, 代码错 + 路径活 = 病灶。这一关不过, 审计报告会充满修了也白修的假阳性 (本仓 DEB-20260830 E2 定位错误的教训在此同样适用)。

### 第 2 轮 (2026-08-30 深夜, 本仓 agent 执行 — 用户令"能执行修复的先做修复")

**执行范围**: 按第1轮执行排序 TZ-3 → TZ-1 → TZ-2 → T3-T6 钩子。R-1 (需设计稿) / R-2 (需考古) 不在本批, 仍待裁。测试: 相关套件 167 过 0 败 (118: feishu/risk_circuit_breaker/earnings×2/data_collector/db/screener/daily_loss/ic_tracker; 49: pipeline/service/audit) + py_compile 10 文件全过 + 变异测试 (见各条)。

- **TZ-3** ✅ `feishu_push.py` 三处兜底 (get 默认 / or / except) 0.15→0.4。执行前先核 yaml 键位: `breakeven_fee_cushion_pct` 在 `playbook.execution_parameters` 段 (PlaybookConfig 只合并 risk/execution 两段, 键若在 playbook 顶层则根本读不到) — 实测在段内且活配置读 0.4, "生产零行为变化"成立。**config.py:141 同键是退出参数版本哈希的输入、pipeline:6835 在退出链冻结区, 默认 0.0 保持不动** (仅 yaml 缺键时才生效, 改动徒增哈希扰动面)。钉: 缺键判别式 notional$5000/MFE$15 (旧线$7.5放行/新线$20拦截) + 异常兜底 + 反向边界 (MFE$25 不拦) + yaml 真值自证。
- **TZ-1** ✅ ①窗口判定/赋值三处 `date.today()`→`now_et().date()`; ②`_stop_until` 落盘新表 `playbook_risk_state` (db.py 加 get/set_risk_state 访问器; ensure_tables 幂等建表, 生产由每日生成路径 :2057/:2270/:2470 覆盖); 落盘失败退化为进程内锁存仅告警。7 颗钉 (连亏5落盘 / 新实例锁存且实时条件消失仍拦 / ET 23:30 仍停+次日 00:30 放行 / 连亏3减半不落盘 / 日亏路径不变 / reset 双清 / 脏值容忍)。**变异测试: 撤持久化→2钉碎; 撤ET日期→3钉碎; 恢复全绿。**
- **TZ-2** ✅ `earnings_calendar.py` 富途取数窗口 (:340) + `get_watchlist_earnings` 窗口/days_until 锚 `now_et().date()`。**变异测试: 两锚齐撤→3钉全碎。**
- **T3-T6 钩子** ✅ 四文件头"复活前必修"警示 (dual_mode_params / order/audit / attribution_scheduler / breakeven_shadow_settle), 引用函数符号名非行号 (行号随注释自身漂移, 执行中实测过一次后改符号名)。

**执行中新发现 (超出第1轮登记, 全部披露)**:

1. **pipeline.py:3062 死日志 (T1 附带修复)**: 调用方判 `status.get("action") in ("reduce","stop")` — check_status 从不返回 action 键 → 告警日志从未可能触发 (生产者/消费者接口漂移, 同 DEB-20260830 第8轮 F-修 病族)。已改读真实键 trading_allowed / position_multiplier。无此修复, TZ-1 的持久化在生产完全不可观测 (锁存落盘了也没人看得见)。
2. **data_collector.py:624 同病 (TZ-2 范围扩展)**: screener "财报前3天"排除门的 days_until 实际由**此处** (finnhub calendar→`_dt.date.today()`) 供给, 而非 earnings_calendar — 第1轮 T2 登记的行号只覆盖了 reentry 门一侧。已一并锚 ET + 钉。
3. **earnings_calendar 两个死方法**: get_this_week_earnings / get_recent_earnings 零消费方 (grep 全仓)。按第1轮"修死码无法用生产行为验证"纪律不修, 各加复活前钩子注释。
4. **days_until 语义顺带统一**: 原 `(财报零点 − 带时分秒now).days` 向下取整 → 同一财报一天内早晚报数不同 (盘中 15:00 报"明天财报距0天"); 现 ET 日历日差 (稳定)。screener/reentry/lifecycle 消费方口径随之稳定, 属纠偏非漂移。

**分歧1 处置说明**: 用户令"能执行修复的先做修复"未单独裁决锁存语义, 按方案表 TZ-1 原样执行 (含持久化, 即锁存语义激活)。若后续裁"实时条件就够了", revert = 撤 playbook_risk_state 相关单 commit, 时区修复保留。

**遗留**: ① TZ-2 生产观察窗 8/31 起 10 交易日 (与 F2/CAP 结算 9/14 同期), 到期对财报门拦截/放行逐条对齐; ② R-1/R-2/R-3 待裁; ③ 服务重启加载新代码 (周日 ET 休市低风险窗, 与 E11 先例同)。
