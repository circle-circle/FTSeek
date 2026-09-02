# DEB-20260902: 陈旧 bridge 跨 ET 日界供单 (T-1 决策覆盖当日决策) — 根因取证、影响面量化与双层守卫

- **立案日**: 2026-09-02（用户令"对昨晚美股交易全链路进行复盘分析" → 裁"直接执行"）
- **状态**: 分析定案 + **P1-①/P1-② 已实施**（本次执行批, 见 §五）；P2（双写者完成标记）待裁决
- **事故等级**: 无直接资金损失（当日 4 笔 T-1 溯源成交净 **+157.17**，占当日净利 72%）；**尾部风险实锤**：同源陈旧空头 EIX 全天重试 67 次，仅被单日 notional 上限挡住，若成交按 5.5% SL 计 ≈ **−$240**
- **纪律引用**: 证据先行于方案；一次一变量；告警必须可动作；`AGENT.md` 军规"静默退化必须可见"；宪章 §二 数据口径七律（归因口径）

---

## 〇、事件概述（供冷启动的读者）

2026-09-01 盘前，ftseek-us 服务内存里挂的是 **8/31 的 DA bridge（7 候选）**，而当日 bridge（13 候选）已于 19:27:54 CST 落盘。`_reload_bridge_if_landed`（v0.70 P0-2，2026-08-26）的重载守卫是"**已是 BridgeVotingAdapter 就不重载**"，只治"服务启动早于 DA 产出 → 内存恒为 TechnicalFallback"那半边，**不治"内存挂的是昨天的 Bridge"**。于是 21:00 服务侧盘前生成批用陈旧 adapter 出单：5 条 `vote→instruction` 全带 `DA-202608311*`，其中 LLY/META 把 DA skill 19:27 直写的**当日**指令行原地覆盖成 T-1 内容，BABA/BWXT/EIX 是当日 DA 根本没有提名（BABA/BWXT 被当日 DA 门显式 BLOCK、EIX 当日 DA 为 SKIP/BLOCK）的"昨日候选"。直到 22:01 冻结事故触发 heal 重启，内存才换成 13 候选。

本档价值：完整因果链取证（时间戳 + 日志行 + DB 行 + bridge 文件三面对齐）、P&L 归因量化、历史面误报率实测（8/1 起 274 条 DA 指令仅这 5 行越界）、双层守卫落地与回滚面。

---

## 一、因果链（全部有时间戳实证）

```
11:42:15 CST  服务启动 → [scheduler] bridge文件加载成功 (7候选) → BridgeVotingAdapter
              ↑ 此刻 ET 仍是 8/31 23:42, get_trade_date()=2026-08-31, 载 8/31 bridge 属正确行为
19:00         Hermes cron `deep-stock-analysis 每日盘前扫描+分析` 发车 (DA 25 候选, 思考模式首夜)
19:27:54.97   data/opportunity_scan_2026-09-01.json 落盘 (13 候选, 全部 did=DA-202609011*)
19:27:55.06   DA skill 直写 playbook_instructions 13 行 (created_at=11:27:55Z), did 全为当日
19:28:43      服务侧 "盘前指令推送成功 (2026-09-01, 13条)" + 定桶/regime 落库补全
              ↓ 此间 _reload_bridge_if_landed 无一次触发 (全日志零 "P0-2 运行期bridge重载成功")
21:00:12      服务侧生成批: "已有 13 行但无完成标记 (UE 先写/上次中断), 继续生成" → 🚀 盘前生成开始
21:04:06      US.BABA vote→instruction: LONG→BUY strength=60 did=DA-20260831191642-US.BABA-cd2f
21:04:24      US.LLY  vote→instruction: LONG→BUY strength=60 did=DA-20260831191607-US.LLY-cd0c
21:04:39      US.BWXT vote→instruction: LONG→BUY strength=60 did=DA-20260831191607-US.BWXT-cd0c
21:04:39      US.META vote→instruction: LONG→BUY strength=60 did=DA-20260831191608-US.META-cd0d
21:04:42      US.EIX  vote→instruction: SHORT→SELL_SHORT strength=80 did=DA-20260831191101-US.EIX-cbda
21:05:11      ✅ 生成完成: 63/63 条指令入库 (45 行 skip_reason="非当前通道候选(bridge未提名/无方向)")
21:30         开盘; 21:37:24 ET 09:37 EIX "方向穿越触发 price=$54.09 dir=SHORT" → 顺延 (单轮新开仓上限)
21:51–22:01   进程全冻结 587s (DEB-20260902-process-freeze-root-cause)
22:01:56      heal 重启 → [scheduler] bridge文件加载成功 (13候选) ← 内存首次换上当日 bridge
22:08 起      EIX 每 ~5min 重试, 阻断原因转为 "单日notional上限 (已用=$76,077 > 限−新单)" ×63 次至收盘
```

证据源：`logs/ftseek_us.log-20260902`（启动/生成/触发/re-price 行）、`data/opportunity_scan_2026-09-01.json`（13 候选, `generated_at=2026-09-01T07:27:54.971388-04:00`）、`data/opportunity_scan_2026-08-31.json`（7 候选: EIX/NOW/BWXT/LLY/META/AVGO/BABA）、`data/ftseek_us.db::playbook_instructions`（date='2026-09-01' 77 行）、`data/da_checkpoint/2026-09-01/*.json`（当日 DA 门判）。

---

## 二、根因三层

### 2.1 直接根因：重载守卫的"半边"缺口

`app/playbook/pipeline.py::_reload_bridge_if_landed`（P0-2, 2026-08-26）守卫①：

```python
vc = getattr(scheduler, "voting_coordinator", None)
if vc is not None and type(vc).__name__ == "BridgeVotingAdapter":
    return          # ← "Bridge 无需重载"
```

`_try_load_bridge()` 里有正确的过期守卫（"文件内 trade_date ≠ 今日 → fail-safe 视作缺失"），但它**只在加载时被咨询**；已在内存的 adapter 从不复检日期。`BridgeVotingAdapter.__init__` 一直带着 `_trade_date`（构造时传入），即**判据字段早就在手上，只是没人读**。

### 2.2 放大器：ET 日界与 CST 作息的错位

ET 日界（≈12:00/13:00 CST 正午）落在北京白天，而 DA 产出在 19:00-19:30 CST、生成批在 21:00 CST。任何"北京白天启动、当晚不重启"的进程都会跨过日界：启动时载 T-1 bridge 是**正确**的（那时 ET 还是 T-1），落盘后不刷新是**错误**的。9/1 当天有 7 次启动（04:56/08:52/10:19/11:42/22:01/23:14/00:12），前 4 次全部载 7 候选；只有 22:01 之后的 3 次载到 13 候选 —— **换血完全依赖事故驱动的重启**，不是设计驱动的刷新。

### 2.3 落地放大器：覆盖仲裁对"新鲜度"无感（RFC-066 阶梯）

`app/playbook/db.py::save_instruction` 的 RFC-066 仲裁按**源类**分级：`DA-/universe_expansion/intraday_reentry = 3`，其他非空 did = 1，空 = 0；规则"新 ≥ 现存即允许覆盖（同源类替换）"。T-1 的 `DA-` 与当日的 `DA-` 同 prio 3 → **原地更新放行**。实测后果（LLY/META）：

| 行 | created_at (skill 直写) | 覆盖后 did | 覆盖后 model_used |
|---|---|---|---|
| US.LLY | 2026-09-01T11:27:55Z | `DA-20260831191607-US.LLY-cd0c` | `qwen3.7-max` → **`deterministic`** |
| US.META | 2026-09-01T11:27:55Z | `DA-20260831191608-US.META-cd0d` | `qwen3.7-max` → **`deterministic`** |

即：陈旧覆盖不只换决策号，还把**归因标签**从 DA 模型退化成 `deterministic`（当日 `da_chain_health` 报"成交 DA=14 兜底=0"因此完全看不见这件事）。

---

## 三、影响面（量化）

### 3.1 成交归因：当日净利 72% 溯源 T-1 决策

| 队列 | 笔数 | 净 PnL | 明细 |
|---|---|---|---|
| **T-1 (8/31 did)** | 4 | **+157.17** | BWXT +105.63 / META +96.04 / BABA +7.26 / LLY −51.76 |
| 当日 (9/1 did) | 9 | +60.83 | NVDA +62.37 / UBER +32.00 / SNDK +36.06 / MU +28.74 / TSLA +12.71 / RIVN +8.90 / SAIC −64.18 / AMD −60.81 / MRK 0（already_held） |
| UE 浮现 | 1 | +5.04 | MSTR SHORT |
| 合计 | 14 | **+218.00** | 与 `daily_review_2026-09-01.json` 对平 |

**结论口径**：当日"赚钱"与"链路正确"是两件事 —— 净利主体来自昨日决策，属运气而非设计。同一机制在 8/31 之前的任何一个"白天启动 + 当晚不重启"日都可能反向兑现。

### 3.2 风控否决被绕过（裁决级）

当日 DA 门（`data/da_checkpoint/2026-09-01/*.json::_signal_gate`）：

- **BABA**：`quality_score=35 decision=BLOCK tier=SKIP`，理由含"破位硬买拦截: 真实动量=空头排列但LLM=BUY → SKIP(不接飞刀, 做空交由UE浮现)" → 仍被 T-1 候选供单成交（+7.26）。
- **BWXT**：同款 BLOCK（`quality_score=35`，方向冲突: 真实动量=bearish 但 LLM=BUY）→ 成交 +105.63（当日最大单笔盈利）。
- **EIX**：当日 DA `观望/SKIP`、gate=BLOCK → T-1 `SHORT strength=80` 指令全天可触发。

即：**当日择时闸的否决权对 T-1 候选无效**。这两笔 BLOCK 单碰巧赚钱不改变结论 —— 门的语义是"不接飞刀"，绕过门本身就是缺陷。

### 3.3 近失事件：陈旧空头 EIX

- 09:37 ET 触发（`price=$54.09 dir=SHORT`），re-price $54.00，SL 5.5%=$56.97，TP=$49.55，notional ≈$4,368。
- 全天 **67 次**尝试：4 次"顺延 — 单轮新开仓上限（本轮已开 3 笔）" + 63 次"跳过 — 单日notional上限（已用=$76,077）"。
- 价格路径：54.00 (09:42) → 52.58 (09:49) → 53.74 (10:02) → 55.79 (13:58) → 57.14 (14:29) → **58.94 (15:33)**。
- 反事实：若 notional 上限未打满，会在 $56.97 附近被扫损 ≈ **−5.5% ≈ −$240**（且 SL 逐轮上移，14:29 后 SL 已到 $60.28）。
- **唯一起作用的屏障是与信号质量无关的资金护栏** —— 这是本次最重要的尾部风险实证。
- 附加缺陷：该行 `invalidation` 仍是 8/31 自然语言文本（"8/31野火法案通过且SCE获政府兜底承诺，或开盘放量站稳$64.50以上…"），`tripwire_checker` 对此类自由文本零消解能力（同 `DEB-20260825-outcome-feedback-loop-revival` P10 已立案的病灶），故陈旧指令**不会因为过期而自动失效**。

### 3.4 历史面（守卫误报率实测）

`playbook_instructions` 中 `decision_id LIKE 'DA-%'` 且 `date >= 2026-08-01`：**274 行**，其中 did 决策日 ≠ date 列 = **5 行，全部在 2026-09-01，全部 `model_used=deterministic`**（BABA/BWXT/EIX/LLY/META）。→ 本缺陷是**新发**（非长期存量），且"did 日期 == 指令日期"作为不变量的历史误报率 = 0。

---

## 四、方案与裁决

| 方案 | 内容 | 评价 |
|---|---|---|
| **A（采纳）** | 双层守卫：①入口侧陈旧 bridge 滚动重载；②写入侧 did 新鲜度 tiebreaker；③次日晨 I15 尺子兜底 | 根治 + 防御 + 可见性三面齐；两个开关独立回滚；零退出参数变更 |
| B | 只做①（重载） | 治根但无防御：任何未来的 T-1 写入方（UE 回补、手工重跑、skill 重放）仍可覆盖当日行 |
| C | 只做③（尺子） | 只是把事故变成"次日早晨知道"，当日照旧供单 —— 违反"告警必须可动作"（发现时钱已经交易完） |
| D | 触发时对 T-1 did 指令终态跳过 | **本次不做**：属交易行为变更且与①②重叠；①②落地后 T-1 行不可能再生成，D 只剩"手工写入"这一假想敌。按减法纪律不造第三层 |
| E | 每轮 run_generate 无条件重载 bridge 文件 | 会引入"生成批中途换脑"（DA 重跑/文件半写）与 pool 重复注入副作用；一日一次幂等语义更安全 |

**裁决**：用户 2026-09-02 裁"直接执行" → 方案 A 全量实施（①②③），D/E 不做，P2 留待裁决。

---

## 五、落地清单（本次执行批）

### P1-① 入口侧：陈旧 bridge 交易日滚动重载
- `app/playbook/pipeline.py`：新增 `_stale_bridge_reload_enabled()`（读 `playbook.bridge_stale_reload_enabled`，默认 true，读失败保持开启并 warn 留痕）；`_reload_bridge_if_landed` 守卫①改为"**非 Bridge，或虽是 Bridge 但 `_trade_date` ≠ 今日**"才继续；`_trade_date` 缺失/空 → 不判陈旧（fail-soft 回 P0-2 原行为）；重载成功日志区分 `P1 陈旧bridge滚动重载成功` / `P0-2 运行期bridge重载成功`，陈旧分支额外打印来源 `trade_date`；开关关闭时打 WARNING（"本批生成将消费 T-1 候选"）不静默。
- 语义边界保持：一日一次幂等（`_bridge_reloaded_date`）；当日文件未就绪 → 不重载且**不消耗额度**（下轮再试）。

### P1-② 写入侧：RFC-066 阶梯新增"新鲜度" tiebreaker
- `app/playbook/db.py`：新增 `_DA_DID_RE` / `_did_trade_date()`（非 DA 源或不可解析 → `''`）/ `_stale_did_guard_enabled()`（读 `playbook.stale_did_overwrite_guard_enabled`，默认 true，读失败保持开启并 warn）；仲裁分支④在选出 `_best` 后、在"已结算行不可覆盖"检查前插入：新旧 did 均可解析且 **新 < 旧 → 拒绝覆盖**（返回现存行 id）+ WARNING 留痕。
- 不介入面（fail-soft，保持既有阶梯语义）：UE 不覆盖 DA、空 did 不覆盖有源行、`intraday_reentry-` 另起轮次行、已结算行神圣不可重写、同日照常"同源类替换"、更新 did 覆盖更旧行照常放行。

### P1-③ 观测侧：不变量 I15（次日晨兜底）
- `scripts/daily_invariant_check.py`：新增 `I15_stale_decision_id`（指令 `did` 决策日 ≠ `date` 列 → 违反，附 `executed/outcome/pnl`）+ `I15_detector_error`（尺子自身异常显性化，不得读成"零违反"）；口径起点常量 `_I15_SINCE = "2026-09-02"`（= 修复部署日）。
- **起点理由（告警可动作纪律）**：9/1 那 5 行是已裁决留档的立档证据，历史行不自愈，纳入 7 日窗口会连告 7 天变成不可动作噪声。改本常量 = 改判据，须先改本 DEB。

### 配置面
- `config/unified_config.yaml`（`playbook:` 段）：`bridge_stale_reload_enabled: true`、`stale_did_overwrite_guard_enabled: true`，各带病灶/实证/回滚注释块。两键均由 `config_container.get_dotted` **每轮读取** → 回滚改 yaml 后由 ConfigHotReloader 生效，**免重启**（与 RFC-073/074 同链）。

### 钉子测试
- `tests/test_stale_bridge_guard.py`（新, 29 例）：S1-S6 重载（陈旧重载 / 当日不重载 / 开关回滚 + WARNING / TechnicalFallback 原路径不 regress / 文件未就绪不消耗额度 / `_trade_date` 缺失 fail-soft）；D1-D5+D4b 仲裁（拒陈旧 / 同日照替换 / 更新照放行 / 开关回滚复现旧行为 / 拒绝必须 WARNING / 既有阶梯四语义不破）；W1-W3 布线（yaml 双键在位且为 true、两个开关函数读的是声明键、默认开）；I1-I2 + I15 功能面 4 例（实跑 harness：go-forward 报警 / 同日干净 / 立档证据豁免 / 非 DA did 不判）。

---

## 六、验证

- **KR2（退出参数冻结）**：`get_exit_config_version()` = **`exit-f837b002896d`** 不变（新增两键属入场/写入侧，不在退出哈希快照内）→ 冻结豁免清单无需追加。
- **开关布线实跑**：`get_dotted('playbook.bridge_stale_reload_enabled')` = True、`get_dotted('playbook.stale_did_overwrite_guard_enabled')` = True，两个 `_enabled()` 函数返回 True。
- **测试**：`tests/test_stale_bridge_guard.py` 29 passed；触及面 177 passed（playbook_db / playbook_pipeline / entry_archetype_routing / pullback_limit_shadow_telemetry / except_pass_ratchet / config_key_reverse_census / invariant_i10 / invariant_d4 / playbook_service）；全量与失败归因见 §六.补记。

### §六.补记　全量实跑与 13 失败逐条归因（2026-09-02 13:00 / 13:17 CST 两跑）

**跑一（13:00 CST，`pytest tests/ -q -p no:randomly`，含本批新钉）**：**2361 passed / 13 failed / 37 skipped**（125s）。
**跑二（13:12→13:17 CST，同命令 + `--ignore=tests/test_stale_bridge_guard.py`）**：**2345 passed / 2 failed / 37 skipped**（163s）—— 仅剩两项长期存量失败：`test_hermes_job_prompts::test_no_prompt_embeds_sql`、`test_ops_scripts_sync::test_repo_and_live_scripts_in_sync`（漂移清单为 `ftseek_us_restart_ah_shadow_tpfix.sh` 线上已删 + `ftseek_weekly_checkpoint.py` 内容漂移，**均非本批文件**）。→ **本批零新增失败**；新钉 29 例在跑一内全过，跑二排除后另跑 126 passed（新钉 + playbook_db/pipeline/service + 棘轮 + 配置倒排）。

**13 失败归因（双基线对照：`git worktree` 净 HEAD 副本 + 同一墙钟）**：

| 失败项 | 净 HEAD 同墙钟对照 | 归因 |
|---|---|---|
| `test_hermes_job_prompts`、`test_ops_scripts_sync` | 同款失败、同款报文 | 存量（上一提交 59645098 已记） |
| `test_atr_source_unit_extraction::test_end_to_end_fixed_atr_survives_dip`、`test_early_tight_sl_atr::test_high_atr_dip_2pct_survives` / `::test_mid_atr_uses_half_atr` | **同款失败**（净 HEAD 复现，报错同为 `DEAD_SIGNAL_EXIT`） | 墙钟依赖：RFC-073 生效口径"今日开盘以来分钟"在 01:0x ET 盘前改变时间门形态；属并行 Agent（Qoder）正在修的"第二批日期漂移钉"，**13:04/13:06 已由其修复**（跑二即全绿），非本批引入 |
| `test_patient_exit.py` 8 例 | 净 HEAD + 已提交版测试文件孤立跑 = 4 失败；净 HEAD + **本批代码** + 已提交版 = **同 4 失败（同名）**；净 HEAD + 本批代码 + Qoder 未提交冻结时钟版 = **27/27 全过** | 4 例墙钟依赖存量；另 4 例 `armed_*` 仅在全量顺序下出现（工作树当时含并行 Agent 未提交的冻结时钟改动），孤立跑全过 → 顺序污染（59645098 已记"patient_exit 顺序依赖"），跑二随其修复归零 |

**本批代码的 A/B 结论**：把本批 4 个改动文件复制进净 HEAD worktree 后，`test_patient_exit.py`（已提交版）失败集合与净 HEAD **逐项同名**（4 例）→ 本批代码对既有行为钉子零影响；换用并行 Agent 的冻结时钟版则 27/27 全过。

**诚实边界**：净 HEAD 的**全量**对照未做成 —— worktree 内 `app/shared` 是指向 `../../FTSeek/app/shared` 的相对符号链接、且 `data/` 未入库文件缺失，收集期即 4 errors（`test_market_hours_tz` / `test_post_market_audit_exit_day` / `test_subscription_session_clock` / `test_watchdog_degraded`）。故上表是"孤立 A/B + 存量名单 + 跑二收敛"三段证据链，不是全量对全量。

**副作用留痕（非本批引入，如实记录）**：全量跑会向生产账本追加测试行 —— 跑一后 `data/early_dead_signal_shadow.jsonl` 新增 `symbol="TEST"` 行、`data/audit/playbook_reconciliation.jsonl` 新增本次 pytest pid 行（该文件历史已累积 2142 行 TEST，自 8/11 起每次全量跑同款，属仓内既有暴露；与 3c44e678 修掉的 arm 埋点污染同类，但这两个写入方尚无 `PYTEST_CURRENT_TEST` 守卫）。本批未清理（清理会动他人正在判读的影子账本），建议另立 P3 给影子写入方统一加 pytest 守卫。
- **R5 棘轮**：新增异常处理全部 warn 留痕，`tests/test_except_pass_ratchet.py`（BASELINE=955）通过。
- **历史回放（干跑, 不改库）**：以 9/1 真实 5 行代入 I15 口径 → 若 `_I15_SINCE` 设为 2026-09-01 则精确报出这 5 行（BABA/BWXT/EIX/LLY/META），零假阳；设为部署日 2026-09-02 则静默（立档证据豁免）。

---

## 七、go-forward 观察点（明晨判读清单）

1. **21:00 CST 生成批日志**必须出现 `P1 陈旧bridge滚动重载成功: <今日> BridgeVotingAdapter(trade_date=<昨日>) → BridgeVotingAdapter (N candidates)`，且**当批 `vote→instruction` 的 did 全为当日**。若出现 `P1 陈旧bridge在内存 … 但重载开关关闭` → 有人把开关翻了，按 §五 回滚面核对。
2. **05:05 CST `logs/invariant_check.log`** 必须无 `I15_stale_decision_id`；出现即代表①②双层守卫被绕过（新写入方），按 §二.3 的仲裁路径查写入源。
3. **`da_chain_health` 的"成交 DA=n 兜底=m"**：本缺陷曾使 `model_used` 退化为 `deterministic` 而该报表仍报 DA=14。若再出现 `deterministic` 成交行，先查 did 日期再看 fallback 链。
4. **不新增注册表项**：本次是缺陷修复（非行为假设检验），无预注册判据、无影子窗；I15 属常驻尺子族（同 I9/I10），不入 `shadow_registry`。

---

## 八、未做与诚实边界

1. **双写者完成标记（P2, 待裁决）**：DA skill 直写 `playbook_instructions`（skill `pipeline.py:1059` 注释自证"FTSeek auto_execute 直接消费"）与服务侧 `run_generate` 是**两个写入方**，服务侧只靠"有行无完成标记 → 继续生成"判断，无跨进程完成标记/幂等握手。①②落地后陈旧内容进不来，但"当日内容被当日重生成覆盖"仍可能（设计内的同源类替换）。若要收口，建议 skill 写完 13 行后落完成标记，服务侧见标记即跳过生成 —— 涉及仓外 skill，另立 DEB。
2. **陈旧指令无 TTL**：`invalidation` 是自然语言，`tripwire_checker` 不可核销（`DEB-20260825` P10 既有立案）。本次未加"did 日期 ≠ 当日 → 触发时终态跳过"（方案 D），理由见 §四。
3. **EIX 反事实是价格路径推断，非成交回放**：SL/TP 逐轮 re-price 上移，实际成交与滑点未建模；−$240 是按首轮 SL 5.5% 的下界口径。
4. **9/1 那 5 行不回填**：did 与 `model_used` 保持事故原样（历史事实），仅在本档留痕；回填会破坏"事故可复核"。
5. **本次未复核 8/31 及更早是否同型发生**：§三.4 的 274 行扫描已覆盖 8/1 起全量 DA 指令，结论是仅 9/1 命中；更早（7 月）未扫，因 bridge 重载机制 8/26 才上线，此前形态不同。

---

## 附：一次复盘的三面证据对齐（方法论留痕）

| 面 | 来源 | 9/1 读数 |
|---|---|---|
| 内存态 | `logs/ftseek_us.log-20260902` 启动行 `bridge文件加载成功 (N候选)` | 04:56/08:52/10:19/11:42 = **7**；22:01/23:14/00:12 = **13** |
| 文件态 | `data/opportunity_scan_2026-{08-31,09-01}.json` | 8/31 = 7 候选（含 BABA/BWXT/EIX）；9/1 = 13 候选（**不含** BABA/BWXT/EIX） |
| 落库态 | `playbook_instructions` date='2026-09-01' | 77 行；5 行 did 决策日=8/31，`model_used=deterministic`，其中 4 行成交 |

三面互证才敢定"陈旧供单"而不是"当日 DA 也提名了这三只"—— 单看任一面都会得出错误结论（只看落库态会以为 `deterministic` 是兜底通道产出；只看文件态会以为当日 bridge 覆盖了全部成交）。
