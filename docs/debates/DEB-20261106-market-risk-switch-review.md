# DEB-20261106 · 「系统性风险开关」立案复核：三条轴的数据裁决与三处真缺口

> **性质**: 立案复核 + 只读回放 + 提案（**不含任何已落地的行为变更**）。
> **状态**: 待用户裁决（2026-11-06）；Phase 0 只读回放已完成；已过第二 agent 复核（§十四）与第一 agent 交叉核查（§十六）；日内运行模型见 §十五。
> **立案人**: Hermes（应用户令「出DEB」，前序触发 = 11-05 崩盘日亏损溯源）。
> **目标读者**: ① **裁决者**（用户）——只需读 §零 / §五 / §八；② **执行者**——§六；
> ③ **复查者（第二 agent）**——§三 证据表逐格可跑、§七 点名挑战项。
> **红线**: 本提案不申请任何交易参数/阈值/门限改动；Phase 0 为只读脚本（无 cron、无消费者、
> 不写生产库）。凡涉及行为变更的项均列入 §八 frontier 待裁，未获裁决前不落码。

> **第二复核警示（2026-11-06 08:00 CST，Codex）**：§十四已用 `first_seen_at`、DA 直连源码和原始指令方向复核。
> **E12 的“盘前已入库”、D3/F5 的“DA 实际不是 qwen”两项核心证据不成立；E3 是当前状态与原始方向口径差。**
> 轴 A/B 的“不加价格硬门”结论保留；D1 不再按“零行为变更的一行事实”推荐直接上线，改为先离线 A/B 的
> **Event Intelligence v1**。请优先读 §十四至§十七，勿继续引用被推翻的 E12/D3/F5 结论。

> **日内需求补记（2026-11-06 08:13 CST，Codex）**：用户明确当前主要做日内交易，要求按当天获取信息、推理并产出方向与指令。
> §十五将 Event Intelligence 从盘前单批次扩展为“盘前全量构图 + 盘中增量刷新 + 当日指令生命周期”；
> 仍只做离线设计/回放，不新增宏观硬门，不恢复 UE 直接入场，不授权生产改单或重启。

> **离线原型实施补记（2026-11-06 09:43 CST，Codex）**：R0/R1、抽样 R2b 与 R3 已在隔离产物中执行，见 §十七。
> 已实现 as-of 快照、日内游标、共享双 LLM 简报、标的方向/指令卡、修订/失效与仲裁；未接生产 DB/pool、
> cron、notifier 或 Playbook，未重启。单日结果只证明链路可运行，不证明方向 edge，也不构成 live 批准。

---

## 零、一句话结论

用户要求的「regime + 系统性风险开关」，在我这一轮复算后**不该建成一个门**：

1. **「大盘普跌日禁做多」这条轴已在 2026-09-24 用 71 交易日 / 789 条 LONG 回放关闭**，
   且最普跌桶的毛收益是**最高**的（Q1 毛·日内 +1.059%，DEB-20260924 §三.6）。重开它 = 重走已关闭的路。
2. **「入场那一刻指数正在急跌就禁做多」这条新轴，本 DEB 已用只读回放跑完**（601 笔 LONG / 88 交易日）：
   在机制上说得通的阈值（SPY ≤ −1.5%）全窗口只有 **n=2 / 1 个交易日**；
   在样本足够的阈值（≤ −0.5%，n=72 / 15 日）上，日聚类差额 **CI95 [−36.68, +14.94] 含 0**。
   ⇒ **无判别力 + 关键区间无可测样本**。按预注册纪律不得据此加门（§五）。
3. 11-05 当日**可被任何「禁新多」门救回的金额上界 = $50.64 毛**（BA −62.50 + CRM +11.86），
   而当日净亏 −$115.92 的主体是**昨日下午建立的 SHORT（ALB −108.28，开盘被保本止损扫掉）**，
   与隔夜缺口（10-30 冲击日两笔 OVERNIGHT_GAP_STOP 合计 −702.35，占该日冲击组全部净额的 106%）。

**真正该修的是三处结构性缺口**（都是可验证的事实，不需要新阈值）：

> **2026-11-07 复核就地更正（Hermes，用户令「按建议核查」后执行）**：下列 ①③ 两条的前提已被实测推翻，
> 原说法保留在删除线内，**不得再作为批准理由引用**。更正依据见 §二十二。

① ~~**模型看不见市场级风险事件**（当日 02:35 UTC 已采到 "Chip selloff drags global stocks lower"）~~
**改判为：看不见的根因是"晚到"，不是"没渲染"**。`02:35Z` 是**发布时间**，该条 `first_seen_at = 18:49Z`
（=13:49 ET），晚于 DA 批次 13:35Z 约 5 小时 ⇒ 08:35 ET 那一刻库里没有这条；当日 10 条市场级风险新闻里
as-of 08:00 ET 前**仅 1 条**可见（还是"亚洲燃料油 slump"的油品叙事）。DA/UE 的市场环境段确实无事件成分
（`regime=cautious · VIX=13.91 · SPY above_ma20`），但**渲染层补一行救不了这一天**；
② **盘前/盘后通道零市场门**（`scripts/extended_hours_entry_executor.py` 对 VIX/regime/risk_state 命中 **0** 次，11-05 有 4 笔走这条通道；另测：该通道 41 笔带 admission 判决的成交里 **10 笔是 REJECT 仍成交**，判决不是执行闸）；
③ ~~**`model_used` 归因列失真**（11-05 全 12 条指令写 `qwen3.7-max`，而当日实际 LLM 调用是 `deepseek-v4-pro` ×7）~~
**撤回**：11-05/11-06 各 36 个 checkpoint 的 `_llm_output._model_reported` = `qwen3.7-max` 36/36，
与 `playbook_instructions.model_used` 同值 ⇒ 归因列准确；`agent_bridge=deepseek-v4-pro ×14`
（21:32–21:36 CST）是 finish/finalize 另一条链（§一#2 / §十四 B / §二十一 同判）。

**期望管理（先说清）**: 三项修复**不会多出单，也不会直接赚钱**。①买的是"模型能看见今天在崩"这一条语义正确；
②买的是通道间风险覆盖一致（可能减少几笔盘前单）；③买的是"以后任何按模型分层的复盘不再算错"。
若用户要的是"崩盘日少亏"的**金额**答案，本 DEB 的数据结论是：**价格类市场门不是那个杠杆**（见 §五），
真正与之相关的是隔夜缺口与入场后退出语义（已另有 RFC-160 等线索，本提案不并入）。

---

## 一、上一轮口头结论的更正（先行自曝，防下游继续引用）

本轮逐条回读源码与 DB 后，前一轮对话里 **4 条说法站不住**，就地更正如下：

| # | 上一轮说法 | 回读实测 | 处置 |
|---|---|---|---|
| 1 | 「DA 原始方向 **BUY 11** / SELL_SHORT 3」 | **两个数都对，量的是不同层**（§十四 C 提出，本轮独立复算一致）：bridge 原始候选 **18 = 15 LONG + 3 SHORT**（`data/opportunity_scan_2026-11-05.json`，`model_used=qwen3.7-max`，E15）；过费用门后原始方向 **11 BUY**（ADMA/RIVN/RUN/TIC `FEE_BPS_TOO_HIGH` 改 SKIP，但 `raw_instruction_json.direction` 仍 BUY）；当前 DB 可变状态 **9 BUY**（GOOG/LMT 后续 `entry_confirm_reversed`）（E3） | **撤回「11 无法复现」这一更正**：11 = 费用门后原始方向层，9 = DB 当前态。任何方向/收益审计必须先声明层级 |
| 2 | 「qwen3.7-max 根本不在交易路径（0~5 次/月，全离线周报）」 | **上一轮说法在本条上成立；我的「部分证伪」才是错的**：DA 主模型由 DA skill 自建 DashScope 请求（`llm_synth.py` `urlopen`），**不经** `app.llm.model_call(feature="agent_bridge")`；11-05 全部 **36/36** checkpoint 的 API 回显 `_llm_output._model_reported = qwen3.7-max`，挑战者回显 `deepseek-v4.1-flash`（36/36）（E14）。我引用的 `agent_bridge` 调用发生在 **21:32–21:34Z**，与 DA 批次（13:35Z）既不同链也不同时点（E10b 已撤回） | **撤回本条更正**。`model_used='qwen3.7-max'` 与真实一致 ⇒ D3 从「已证实归因失真」降级为「健壮性改进」 |
| 3 | 「崩盘日 DA 仍全力做多 → 当日净 −$115.92」 | 当日 −$115.92（E4）中 **−$108.28 是 SHORT（ALB）**，建立于 11-04 16:42（前一日盘后），11-05 09:30:46 开盘被保本止损扫出；9 条 BUY 里 **7 条被现役门拦掉**（thesis_drift ×4 / vwap ×2 / no_trigger ×1），只有 BA(−62.50)、CRM(+11.86) 成交（E3） | **因果链更正**。当日亏损主体不是"多单方向错"，而是①隔夜持仓的开盘缺口 ②盘中入场的 BA 一笔 |
| 4 | 「regime_classifier 近 30 天 0 次调用 = 配了不跑」 | 事实是**双重不存在**：`llm.features.regime_classifier` 在 `app/` `scripts/` 内**零实现**（仅 `feature_flags.py` 的文档串与字典各一处），且 `llm.features.regime_classifier.enabled: false`（E9） | **降级为"纸面配置"**。它不是一个"休眠的分类器"，而是**从未建过**；把它"启用"不是修链，是新建一个 LLM 分类器——不在本提案范围 |

> 纪律依据：`review-system-design` 技能「被证伪声称须同步清除」+「撤回与声称同权」。

---

## 二、问题陈述（含普查）

### 2.1 系统里已有 6 套市场/风险判定，覆盖却不对称

| # | 实现 | 消费方 | 状态 |
|---|---|---|---|
| 1 | `app/playbook/market_regime.py::MarketRegimeGate` | Playbook 生成（`generation_flow.py:124,258`）、auto_execute 前置（`risk_gates.py:215`）、仓位乘数（`auto_execute_flow.py:644`）、UE 扫描 brief（`llm_universe_scan.py:2655`） | 活，**DA 路径的唯一市场输入** |
| 2 | `app/analysis/market_state.py::get_checker()` | 主线 `order_executor.py:3656`（VIX 新鲜度 fail-close）、`playbook_service.py:1298` | 活 |
| 3 | `app/us_pipeline/risk_state_machine.py::RiskStateMachine` | auto_execute 前置（`risk_gates.py:216`）、`order_executor.py:1927`、`signal_pipeline.py:254`、`scheduler_core.py:2810` | 活，但**生产者只对持仓新闻**（见 2.3） |
| 4 | `app/risk/vix_guard.py::VIXGuard` | `order_executor.py:3675`、`playbook_service.py:1307`（键 `risk.playbook_vix_gate_enabled=true`） | 活，硬门 28 |
| 5 | `app/risk/market_regime_filter.py` | 无（类级 TOMBSTONE 2026-07-31；仅 `pre_trade_check.py:82-84` 读它的配置块） | **僵尸类** |
| 6 | `app/risk/market_regime_detector.py` | 仅 `scripts/regime_divergence_shadow.py` | 影子专用 |
| 7 | `llm.features.regime_classifier` | 无（零实现 + `enabled: false`） | **纸面配置** |

⇒ 结论：**不缺"市场判定"，缺的是"市场级事件"这个输入**，以及**通道覆盖的一致性**。
本提案据此收敛：不新建第 7 套判定，只补输入与覆盖（§六）。

### 2.2 DA 路径的"市场环境"到底看到了什么（11-05 实测）

`playbook_regime_log` 在 11-05 记录 `cautious / VIX=13.91 / SPY=766.81 / MA50=766.45 / score=4`（E1）。
同一行 `created_at=2026-11-05T14:00:21Z` = **09:00 ET 盘前**，用的是**昨收 SPY** 与**盘前 VIXY 代理**。
当天 SPY 的 5m 路径是：09:30 −0.16% → 09:55 首次跌破 −1.5% → 10:30 −2.54% → 收盘 −4.41%（E2）。
即：**盘前那一刻，价格类输入没有、也不可能有"正在崩"的证据**；唯一在 02:35 UTC 就给出的证据是**新闻**
（"Chip selloff drags global stocks lower on AI spending concerns"、"AI stocks fall… blue wave"），
而这条新闻从未进入 `regime`/`market context` 的任何字段。

### 2.3 全局风险状态机对"市场级新闻"结构性失效

`RiskStateMachine` 的 `RISK_OFF` 会拦掉所有 OPEN/ADD/REVERSE（`ACTION_MATRIX`），且 auto_execute 前置**已经在读它**
（`risk_gates.py:216-229`）。但唯一的新闻生产者 `emergency_interceptor.py` 在 `:580` 有：

```python
if not relevant_positions:
    continue      # 只处理「与持仓具名相关」的新闻
```

⇒ 没有个股具名的市场级新闻（11-05 那三条正是这种）**永远走不到 set_state**。
这不是"缺一个开关"，而是**既有开关的输入端被限定了人群**。

### 2.4 通道覆盖不对称

| 通道 | VIX 新鲜度 fail-close | VIX≥28 硬门 | regime NO_TRADE | RiskStateMachine | 日亏熔断 |
|---|---|---|---|---|---|
| 主线 `order_executor` | ✅ | ✅ | —（用 market_state） | ✅ | ✅ |
| Playbook `auto_execute` | ✅（`playbook_service`） | ✅ | ✅ | ✅ | ✅ |
| **盘前/盘后 `extended_hours_entry_executor`** | ❌ | ❌ | ❌ | ❌ | ❌ |

实测：该脚本对 `vix|regime|risk_state` **0 命中**，订单直接 `tctx.place_order`（E7）。
11-05 走这条通道的有 4 笔（ALB/APP/AAPL/ABBV），其中 ALB 是当日最大单笔亏损（−108.28）。

---

## 三、数据与实证表（每行作者实跑，命令整段可复制）

> 全部在 `/opt/ftseek/FTSeek-US` 下执行。`venv/bin/python3.11` 为项目 venv。
> 期望列 = 真实输出片段（非"应该会返回"）。

| 编号 | 声称 | 复现命令 | 期望输出（实跑） |
|---|---|---|---|
| E1 | 11-05 的 regime 判定是 cautious，输入是昨收 SPY + 盘前 VIX | `venv/bin/python3.11 -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select date,regime,vix,spy_price,spy_ma50,score from playbook_regime_log where date>='2026-11-03' order by date\").fetchall())"` | `('2026-11-05', 'cautious', 13.91, 766.81, 766.45180304824, 4)` |
| E2 | 11-05 SPY 开盘持平、盘中破 −1.5% 于 09:55、收盘 −4.41% | `venv/bin/python3.11 scripts/intraday_shock_replay.py --trace-day 2026-11-05` | `09:30 close=765.60 chg=-0.16%` / `首次 <=-1.5%: 2026-11-05 09:55:00 (-1.59%)` / `15:55 close=735.72 chg=-4.05%` |
| E3 | 11-05 指令**当前 DB 态**：BUY 9（成交 2，毛 −50.64）、SELL_SHORT 3（0 成交）、SKIP 67；9 条 BUY 里 7 条被现役门拦住（不是「原始方向」口径，见 E15） | `venv/bin/python3.11 -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select direction,count(*),sum(executed),round(sum(pnl),2) from playbook_instructions where date='2026-11-05' group by direction\").fetchall())"` | `[('BUY', 9, 2, -50.64), ('SELL_SHORT', 3, 0, 0.0), ('SKIP', 67, 0, 0.0)]` |
| E4 | 11-05 已实现净 −115.92（费 29.55），其中 ALB（SHORT，隔夜）−108.28、BA −62.50 | `venv/bin/python3.11 -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select count(*),round(sum(pnl_net),2),round(sum(fees_est),2) from trade_records where date(exit_time)='2026-11-05'\").fetchone())"` | `(7, -115.92, 29.55)` |
| E5 | 冲击日（SPY 日跌 ≤−1.5%）LONG 净额显著为负，但**赢家占比过半**；且当日冲击组净额的主要来源是隔夜缺口单 | `venv/bin/python3.11 scripts/market_shock_census.py` | `B -3~-1.5%     LONG        n= 10 net=  -733.70 avg=  -73.37` 与 `小计 net=-663.73, 其中 OVERNIGHT_GAP_STOP=-702.35 (106%)` |
| E6 | 入场时点指数急跌 → LONG 结局：全窗口口径与阈值敏感性 | `venv/bin/python3.11 scripts/intraday_shock_replay.py --since 2026-07-02 --until 2026-11-05` | `LONG n=601 dates=88`；`th=-1.5%  n=  2 日= 1`；`日聚类差 CI95[-36.68,+14.94]` |
| E7 | 盘前/盘后通道零市场门 | `grep -ic "vix\|regime\|risk_state" scripts/extended_hours_entry_executor.py; true` | `0` |
| E8 | Playbook auto_execute 前置确有四道门（kill/regime/risk_state/日亏） | `grep -c "regime_no_trade\|risk_state_\|kill_switch\|daily_loss" app/playbook/pipeline_parts/risk_gates.py` | `21` |
| E9 | regime_classifier 零实现 + enabled:false | `grep -rn "regime_classifier" --include=*.py app/ scripts/` | `app/llm/features/feature_flags.py:12` 与 `"regime_classifier": False` 两行（无任何实现文件） |
| E10a | 11-05 指令落库的 model_used 是 qwen3.7-max | `venv/bin/python3.11 -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select model_used,count(*) from playbook_instructions where date='2026-11-05' and direction!='SKIP' group by model_used\").fetchall())"` | `[('qwen3.7-max', 12)]` |
| ~~E10b~~ | ~~同一时点真实 LLM 调用是 deepseek-v4-pro~~ **已撤回**：该 feature 与本时点都不属 DA 链（见 §十四 B / E14） | `venv/bin/python3.11 -c "import sqlite3;c=sqlite3.connect('data/llm_audit.db');print(c.execute(\"select created_at,feature,model from llm_calls where feature='agent_bridge' and created_at like '2026-11-05%' order by created_at limit 3\").fetchall())"` | `('2026-11-05 21:32:11', 'agent_bridge', 'deepseek-v4-pro')`（时点远晚于 DA 的 13:35Z） |
| E11 | RISK_OFF 的新闻生产者只处理"与持仓具名相关"的新闻 | `grep -n "if not relevant_positions" app/us_pipeline/emergency_interceptor.py` | `580:            if not relevant_positions:` |
| ~~E12~~ | ~~市场级新闻在崩盘前 11 小时已入库~~ **已撤回**：`event_time` 是**发布时间**不是可见时间。按 `first_seen_at`，两条 02:35Z 新闻分别 **15:04Z / 18:49Z** 才入库，均晚于 13:35Z 的 DA 批次 ⇒ 不能用来解释盘前未避险。可用的较早事件是 11-04 的 `Morning Bid…blue wave will drown data centres`（18:39Z 已入库）与 `post-election rally…AI split`（19:54Z） | `venv/bin/python3.11 -c "import sqlite3;c=sqlite3.connect('data/event_stream.db');print(c.execute(\"select headline,event_time,first_seen_at from canonical_events where headline like '%Chip selloff%' or headline like '%blue wave will drown%' or headline like '%sharp split in the AI trade%' order by event_time\").fetchall())"` | `'2026-11-05T18:49:08.657800+00:00'` 与 `'2026-11-04T18:39:07.215751+00:00'` |
| E13 | 样本底座：A10 口径下 `trade_records` 共 765 笔（601 LONG / 88 个交易日，功效推算的分母） | `venv/bin/python3.11 scripts/market_shock_census.py` | `样本 n=765` |
| E14 | DA 主模型与挑战者的**API 回显**（不是静态配置） | `venv/bin/python3.11 -c "import json,glob,collections;c1=collections.Counter();c2=collections.Counter();[[c1.update([(json.load(open(f)).get('_llm_output') or {}).get('_model_reported')]),c2.update([((json.load(open(f)).get('_llm_output') or {}).get('_debate') or {}).get('challenger_model_reported')])] for f in glob.glob('data/da_checkpoint/2026-11-05/US.*.json') if not f.endswith('.raw.json')];print(dict(c1),dict(c2))"` | `{'qwen3.7-max': 36} {'deepseek-v4.1-flash': 36}` |
| E15 | 11-05 候选三层口径的入口：bridge 原始 18 = 15 LONG + 3 SHORT | `venv/bin/python3.11 -c "import json,collections;d=json.load(open('data/opportunity_scan_2026-11-05.json'));print(d['model_used'],d['candidate_count'],dict(collections.Counter(x['direction'] for x in d['candidates'])))"` | `qwen3.7-max 18 {'LONG': 15, 'SHORT': 3}` |
| E16 | 采集时延（冻结截止 2026-11-06 00:00 UTC）：11-05 的 NEWS_WIRE 从发布到入库中位 **89.8 分钟**，47/68 超 60 分钟 | `venv/bin/python3.11 -c "import sqlite3,statistics;c=sqlite3.connect('data/event_stream.db');v=sorted(r[0]/60000 for r in c.execute(\"select published_to_seen_ms from canonical_events where event_time like '2026-11-05%' and source_id='FINNHUB_GENERAL_NEWS' and published_to_seen_ms is not null and first_seen_at<'2026-11-06T00:00:00+00:00'\"));print(len(v),round(statistics.median(v),1),sum(1 for x in v if x>60))"` | `68 89.8 47` |

> 脚本入库：`scripts/intraday_shock_replay.py`、`scripts/market_shock_census.py`（只读、无 cron、无消费者）。
> 二者默认把 5m K 线缓存到 `data/replay_cache/spy5m/`（runtime 目录，不入库）。

---

## 四、受众理解用的最小类比

- **已有 6 套「今天适合交易吗」的判断，都只看价格和波动率**（SPY 均线 + VIX）。
  这就像开车只看后视镜：11-05 的**盘前**镜子里的路是平的（SPY 昨收在 MA50 上方 0.36 美元），
  而前方已经封路（全球芯片抛售的头条 11 小时前就摆在那里）。
- **要装的不该是第七个「刹车」，而是那块「前风挡」**——把已经采到、已经入库的市场级事件，
  让做决策的模型看见（D1）。
- **另一个通道（盘前/盘后）连后视镜都没有**（D2）。
- **而仪表盘上的"车型"标签写错了**（D3）：库里写 qwen3.7-max，实际跑的是 deepseek-v4-pro。

---

## 五、三条轴的数据裁决（本 DEB 的核心产出）

| 轴 | 定义 | 数据来源 | 裁决 | 依据 |
|---|---|---|---|---|
| **A. 盘前/日级普跌** | 08:00 ET QQQ 盘前涨跌 / 日收盘涨跌 → 禁新多或降档 | DEB-20260924 §三.6，71 交易日 / 789 条 LONG | **已关闭（无判别力）** | 最普跌桶 Q1 毛·日内 **+1.059%（最高桶之一）**；主对比 p=0.998 且符号翻正；日级回归 p=0.212。本轮 A10 期复核（E5）同向：`−1.5~−0.5%` 桶 LONG n=105 净 −1427.81，而 `+0.5~+1.5%` 桶 n=131 净 **−1485.88** —— 涨日同样出血，**不单调** |
| **B. 入场时点盘中急跌** | 入场那一刻 SPY 相对昨收 ≤ −X% → 禁新多 | 本 DEB 新建只读回放（E6），601 笔 LONG / 88 交易日 | **无判别力 + 关键区间无可测样本 ⇒ 不得加门** | ≤−1.5%：n=2 / 仅 1 个交易日（unpowered）；≤−1.0%：n=8 / 3 日，日聚类差 CI95 **[−165.29, +1.06]** 含 0；≤−0.5%：n=72 / 15 日，CI95 **[−36.68, +14.94]** 含 0 |
| **C. 事件驱动（市场级风险新闻）** | 近 12h 出现"系统性 risk-off"级市场新闻 → 改市场视角/禁新多 | **未测**（输入存在、从未接线） | **只登记，不判决**；本轮只提出"让模型看见"（D1），不提出任何拦截门 | 无历史读数；任何拦截阈值都将是拍脑袋 |

**功效声明（不用"攒样"掩盖不可达）**: 按本轮回放的事件率，≤−1.5% 组为 2 笔 / 88 交易日，
要凑到 DEB-20260924 预注册下限（n≥30 且 ≥6 个交易日）约需 **1300+ 个交易日（5 年以上）**；
≤−1.0% 组为 8 笔 / 3 日，凑 n≥30 约需 **330 个交易日（约 15 个月）**。
⇒ **前向攒样同样不可行**；本轴应以"关闭 + 明确重开门槛"处置（§八 F2），而不是挂影子等。

**单日集中度披露**: 冲击日净额 b 由**单日**决定（10-30 两笔隔夜缺口 −702.35 已超过全组净额），
任何"冲击日亏 $X"的表述都必须带这一天。

---

## 六、方案设计（只做三处结构性修复，不做门）

### 6.1 架构一句话

把**已经采集**的市场级事件做成 DA/UE 市场环境段里的**一行事实**（D1），把**盘前/盘后通道**
接上与其余通道同源的 VIX 门（D2，先 record-only），把**归因列**改成真实调用模型（D3）。
不新增判定器、不新增 cron、不新增台账、不改任何交易阈值。

### 6.2 关键接口

**D1 — 市场级风险事件事实行（输入保真，零阈值）**

- 生产者：新函数 `app/playbook/market_regime.py::market_risk_events(hours=12)`（纯读 `event_stream.db`）。
- 判据（确定性，非 LLM）：`event_type='NEWS_WIRE'` ∧ 时间窗内 ∧ 命中**市场级词表**
  （`selloff / plunges / rout / crash / circuit breaker / global stocks / risk-off / recession fears`
  ∧ 不含单一具名个股 ticker）⇒ 取最近 ≤3 条 headline + 计数 + 最早/最新时点。
  词表常量集中在同一模块，禁止散落。
- 渲染：`llm_universe_scan._gather_macro_brief()` 与 DA 的 market_context 段各加一行：
  `市场级风险事件(近12h): 3 条 | 最新 2026-11-05T02:35Z "Chip selloff…"`；无命中则显式
  `近12h 无市场级风险事件`（**显式零值优于缺行**，防"看不见=没有"）。
- **它不是门**：不加分、不降档、不改变任何 regime 判定；失败一律降级为"缺行"并 WARNING。

**D2 — 盘前/盘后通道市场门对齐（record-only 先行）**

- 在 `extended_hours_entry_executor.py` 的下单前，复用现有 `VIXGuard.should_block()` 与
  `market_state.get_checker().get_vix_cached()`（**同源，不新写阈值**）；新配置键
  `risk.ext_hours_vix_gate_enabled`（默认 `false` = record-only）。
- `false` 时只落 `data/ext_hours_vix_gate_shadow.jsonl`（symbol / vix / would_block / ts），
  **不改任何下单行为**；`true` 才 enforce（需另一批裁决 + 停止条件）。

**D3 — `model_used` 改为「API 回显优先」（健壮性，非已证实缺陷；§十四 B 后的降级版）**

- ~~按 E10b 修「已失真」的归因列~~ **撤回**：本批实测静态值 `qwen3.7-max` 与 API 回显 36/36 一致（E14），不存在已发生的失真。
- 保留的**健壮性**改动（可做、可选、单提交）：DA 侧写库时优先落 `_llm_output._model_reported`（API 信封回显，已在
  `llm_synth.py` 捕获），静态 `config.LLM_MODEL` 仅作回显缺失时的兜底。理由 = 别名变更/未来快照号会被自动记录，
  不需要人改配置。**注意字段位置**：`_model_reported` 在 `_llm_output` 内层，不在 checkpoint 顶层。
- 验收：同一交易日 `playbook_instructions.model_used` == checkpoint `_llm_output._model_reported`（36/36）；
  不追改历史行。
- 该列一旦修正，**历史行不追改**（保留原值 + `metadata_json` 标注切换日），避免第二真源。

### 6.3 明确不做（同批写死，防复活）

1. **不建**"崩盘日禁多"门（§五 轴 A 已关闭、轴 B 无判别力）。
2. **不把**市场级新闻接进 `RiskStateMachine.RISK_OFF`（全局全动作封锁 + 历史误触发前科见 §八 F4）。
3. **不启用** `llm.features.regime_classifier`（零实现，启用=新建 LLM 分类器，无真值可校准）。
4. **不换模型**（用户原问题的正式答复见 §八 F5）。

---

## 七、预注册判据 / 回滚 / 停止条件

### 7.1 D1（输入保真）判据

- **P1（必须）**: 接线后连续 5 个交易日，DA/UE brief 的"市场级风险事件"行与 `canonical_events`
  逐日对账一致率 100%（抽样 3 日逐条比对 headline 与计数）；任一不一致 = 构建缺陷，回滚。
- **P2（降级观察）**: 5 日内 DA 产出的 `SKIP` 占比若较前 10 日均值 **+20pp**，或 LONG 条数 **低于前 10 日均值 50%** ⇒ 回滚（照抄 DEB-20260924 §六 的回滚触发器，同族口径）。
- **P3（不设成功门）**: 本项**不承诺**任何胜率/净额改善；只交付"模型可见性"。

### 7.2 D2（record-only）判据

- 首窗 10 个交易日；**只在 onset**：`would_block=true` 且该指令最终成交的笔数 ≥1 时，进入下一次裁决。
- 若 10 日内 `would_block=true` 命中 0 次 ⇒ 记"当前 VIX 门对本通道无实际作用"，**维持 record-only，不 enforce**。

### 7.3 回滚（两层）

- **法子**: D1 删 `market_risk_events()` 调用行（配置键 `playbook.market_risk_events_brief=false` 兜底）；
  D2 删新键或置 `false`；D3 `git revert` 单提交。
- **触发条件（出现即回滚，不留给下次讨论）**: ①对账不一致 ②P2 触发 ③D1/D2 任一路径出现异常未降级到"缺行"而是抛出到主流程。

---

## 八、frontier（待用户裁的问题）

| 编号 | 待裁问题 | 推荐 | 一行理由 |
|---|---|---|---|
| F1 | 是否批准 D1（市场级风险事件事实行进 brief） | **批准**（纯输入保真、零阈值、有 P1/P2 回滚） | 这是唯一能解释"11-05 盘前模型只看见平静"的修法，且不动任何门 |
| F2 | 「入场时点盘中急跌」轴如何处置 | **登记为 CLOSED-UNPOWERED**（重开门槛 = 若未来 12 个月内该人群累计 ≥30 笔，重跑 `scripts/intraday_shock_replay.py`） | 当前 n=2/1 日；前向攒样需 5 年，按 9/24 先例应关闭而非挂影子 |
| F3 | 是否批准 D2（盘前/盘后通道 VIX 门 record-only） | **批准 record-only**，enforce 另裁 | 覆盖不对称是事实缺陷；但 enforce 是行为变更，须先看首窗 |
| F4 | 是否把市场级新闻接进 `RiskStateMachine.RISK_OFF` | **不做** | ①它是全局全动作封锁（连 SHORT 一起禁），②历史前科：`lawsuit`/`fraud` 曾反复误触发 RISK_OFF 需人工恢复（`emergency_interceptor.py:583-596` 注释自陈） |
| F5 | 原问题「把分析师 qwen3.7-max 换成 deepseek-v4.1-flash」 | **本案不换**，模型档位比较另开 A/B 案 | 前一轮「qwen 不在交易路径」在本条上成立（E14：DA 主模型回显 36/36 = qwen3.7-max）；我此前据 `agent_bridge` 推出的「已经是 deepseek-v4-pro」**已撤回**。换不换应由离线 bakeoff 裁决，不借本案顺带换 |
| F6 | 是否顺手启用 `regime_classifier` LLM 分类器 | **不启用** | 零实现；且"让 LLM 每 30 分钟判一次 regime"没有可程序化回填的真值，无法校准（`review-system-design` 铁律 8） |
| F7 | 隔夜缺口（10-30 两笔 −702.35）是否单独立轴 | **建议单独立轴（另开 DEB）**，不并入本案 | 它占冲击日净额 106%，量级远大于入场时点门，但属出场/持仓语义，与本案正交 |

---

## 九、复查清单（点名请第二 agent 挑战；只读：禁改文件、禁 git 写、禁重启）

| 编号 | 声称 | 复查命令 | 反例判据（出现即我错） |
|---|---|---|---|
| C1 | 「入场时点急跌」轴无判别力且关键区间无样本 | `venv/bin/python3.11 scripts/intraday_shock_replay.py --since 2026-07-02 --until 2026-11-05` | 若 ≤−1.5% 组 n≥30 且日数≥6，或该组日聚类 CI 不含 0 ⇒ 我的"不得加门"结论被推翻 |
| C2 | 11-05 可拦上界只有 $50.64 | `venv/bin/python3.11 -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select symbol,round(pnl,2) from playbook_instructions where date='2026-11-05' and direction='BUY' and executed=1\").fetchall())"` | 若存在第三条已成交 BUY，或某条已成交 BUY 的绝对值净额使我低估上界 ⇒ 上界错误 |
| C3 | 盘前/盘后通道零市场门 | `grep -ic "vix\|regime\|risk_state" scripts/extended_hours_entry_executor.py; true` | 输出 >0 ⇒ 覆盖不对称不成立 |
| C4 | DA 落库 `model_used` 与 API 回显一致（原「归因已失真」说法已撤回，见 §一#2） | `venv/bin/python3.11 -c "import json,glob,sqlite3;m=sqlite3.connect('data/ftseek_us.db').execute(\"select distinct model_used from playbook_instructions where date='2026-11-05' and direction!='SKIP'\").fetchall();c=set();[c.add(((json.load(open(f)).get('_llm_output') or {}).get('_model_reported'))) for f in glob.glob('data/da_checkpoint/2026-11-05/US.*.json') if not f.endswith('.raw.json')];print(m,sorted(c))"` | 若两边模型名不一致（或回显为 None）⇒ 归因失真成立，D3 升回 P0 |
| C5 | 市场级新闻到不了 RISK_OFF | `grep -n "set_state" app/us_pipeline/emergency_interceptor.py` | 若出现一条不在 `relevant_positions` 分支之后的 set_state 路径 ⇒ 「结构性失效」说过强 |
| C6 | 盘前普跌轴已被关闭且不该重开 | `grep -n "关闭该轴" docs/debates/DEB-20260924-premarket-index-caliber-and-down-day-regime.md` | 命中 0 次 ⇒ 我在误引既有裁决（本提案地基塌陷） |
| C7 | A10 期冲击日出血不单调（涨日同样出血） | `venv/bin/python3.11 scripts/market_shock_census.py` | 若 `+0.5~+1.5%` 桶 LONG 净额为正 ⇒ "不单调"论据弱化（但主判据仍是轴 B） |

**请复查者重点挑战三点**: ① 轴 B 的"unpowered"是否掩盖了一个真实但被阈值选错的效果（换特征而非换阈值来攻击）；
② 我把 11-05 亏损主体判为"隔夜缺口 + 单一盘中单"是否低估了"系统性风险未联动"的贡献；
③ D1 是否其实是一次行为变更（它改变模型所见 ⇒ 可能改变产出结构），我按"输入保真"处理是否过轻。

---

## 十、决策依据最小充分集

| 项 | 本提案状态 |
|---|---|
| ① 固定样本来源（可复跑） | ✅ `trade_records` + `daily_kline_v1.json` + 富途 5m 历史（E6），脚本入库 |
| ② 多窗口（≥2 段） | ✅ 全窗口 88 日 + 阈值敏感性四档并列（E6） |
| ③ 配对/对照臂 | ✅ 同日/同窗对照（冲击组 vs 其余；日聚类） |
| ④ 尾部 | ✅ 最差单笔与单日集中度已披露（§五 单日集中度） |
| ⑤ 费用后口径 | ✅ 全部用 `pnl_net`（净），并给出 `fees_est` 合计（E4） |
| ⑥ 机制诊断 | ✅ 分桶形状 + 阈值敏感性 + 单日拆解（入场时点 vs 隔夜缺口） |
| ⑦ 功效/显著性声明 | ✅ 事件率与"需 330 / 1300+ 交易日"的显式推算（§五） |
| ⑧ 预注册判据 + 回滚 + 复现命令 | ✅ §七（D1/D2 判据与两层回滚）+ §三（命令） |

未凑齐项：**无**（就"该不该建价格类市场门"这个问题而言）。D3 属缺陷修复，不适用本表。

---

## 十一、自曝 / 更正记录

1. 本提案推翻前一轮对话的 4 条说法（§一），其中「BUY 11」是**数字错误**，「qwen 不在交易路径」是**半对半错**。
2. 本轮最初的设计假设是「建一个市场冲击门」，被自己的回放数据否证（轴 B 无判别力）。
   **原说法 → 实测 → 改成什么**：原「11-05 这类崩盘日应禁新多」→ 实测全窗口只有 1 个交易日满足该状态且 n=2 →
   改为「不建门，只做输入保真 + 通道对齐 + 归因修复」。
3. 本提案**不引用** DEB-20260924 §三.3 的 27 日首读数字（已被其 §三.6 的 71 日扩窗取代）。
4. 本提案未测「事件驱动」轴（§五 轴 C）——它的输入存在（E12）但从未接线，
   按 `review-system-design` 规则，无真值 = 不配概率门，故本提案只提"可见性"，不提拦截。
5. `intraday_shock_replay.py` 的 ≤−2% 组（n=1）报告的是"逐笔"而非统计量；任何引用不得升格为分布结论。

---

## 十二、与既有文件关系表

| 既有文件 | 它覆盖什么 | **本提案不重裁什么** | 本提案依赖它哪一段 |
|---|---|---|---|
| `DEB-20260924-premarket-index-caliber-and-down-day-regime.md` | 盘前大盘口径修复（P0，已落地）+ 盘前普跌轴关闭（71 日回放） | 不重开该轴；不改其关闭判决；不重算其 F5 影子 enforce | §三.6（关闭判决与四条判据）、§六（回滚触发器，本提案 D1 的 P2 照抄）、§八 F5/F6 |
| `DEB-20260914-premarket-data-validity-audit.md` | 盘前 VIX/板块脉搏口径统一、`vix_source` 单一真源 | 不动 VIX 口径与代理窗口判据 | D2 复用 `get_checker().get_vix_cached()` 与 `VIXGuard`，即该批收敛后的口径 |
| `DEB-20260823`（VIX 门补齐，`playbook_vix_gate_enabled`） | Playbook 路径补 VIX 门 | 不改该键语义与阈值 28 | D2 把**同一条门**补到尚未覆盖的盘前/盘后通道 |
| `DEB-20261105-extended-hours-chain-audit.md` | 盘前/盘后入场/出场/账实整体审计 | 不重裁其闸门结论（"不放宽闸、不放大、不复活盘前止损"） | 其通道事实（EXT 净额口径、共享快照）作为 D2 的现状基线 |
| `DEB-20260805-futu-unused-capabilities.md` | 富途能力盘点（含 `US.VIX` 不可直查） | — | E2 的 5m 取数走 `US.SPY`（非指数快照），与该案结论一致 |

**唯一增量**: ①「入场时点盘中急跌」这一**新特征轴**的首次回放与终局读数（轴 B）；
②把"市场级事件"从采集层提升到**决策输入层**的接线提案（D1）；
③`model_used` 归因失真这一**新缺陷**（E10a vs E10b）；
④盘前/盘后通道 VIX 门覆盖不对称的**量化登记**（E7）。

---

## 十三、讨论记录

- **2026-11-06（立案轮，Hermes）**: 应用户令「出DEB」。取号前双查 `docs/debates/` 文件名与 `git log`
  （无 20261106 号）；P0.5 撞车普查命中 DEB-20260924 同主题，据其 §三.6 关闭判决把原"冲击门"提案
  收敛为"三条轴裁决 + 三处修复"。Phase 0 只读回放已跑完（601 笔 LONG / 88 日），读数见 E6。
  本文件提交时**未改动任何代码、配置、cron**；两个证据脚本为新增只读脚本。

---

## 十四、第二 Agent 复核与目标实现方案更新（Codex，2026-11-06 08:00 CST）

> 本轮应用户要求，联合复核本 DEB 与 `DEB-20261106-da-election-reasoning-audit.md`。目标不是再证明“新闻有用”，
> 而是让 LLM 的信息覆盖、分析速度和可执行方向产出形成可验证闭环。
> 本轮做了定点只读复现，**没有重跑 E6 的 601 笔全窗**，因此不声称独立重算轴 B；也没有改生产代码、配置、cron 或历史数据。

### 14.1 复核裁决总表

| 原项 | 第二复核 | 更新后的处置 |
|---|---|---|
| 轴 A/B：普跌/盘中急跌硬门 | **维持原裁决**：现有证据不支持加门；本轮未发现足以推翻的新证据 | 不重开阈值购物，不接 `RiskStateMachine.RISK_OFF` |
| D1：模型应看到市场级事件 | **方向成立，但原关键证据和上线分类不成立** | 保留事件输入目标；撤回“一行风险词、零行为变更、可直接 live”，改离线 Event Intelligence v1 |
| D2：EXT 的 VIX record-only | 与事件推理是正交事项 | 可独立评审，**不得与事件 A/B 同批上线** |
| D3：DA 的 `model_used` 已证实失真 | **证据不成立**：比较了不同生产链和不同时点 | 撤回“实际不是 qwen”的断言；静态字段可改健壮性，但不是本批已证实 P0 缺陷 |
| F5：据 D3 决定不换模型 | 结论“不据错证据换模型”保留，理由重写 | 当前 DA 直连与 API 回显均为 qwen3.7-max；挑战者为 deepseek-v4.1-flash；模型 A/B 另案，不借本案换模型 |

### 14.2 两项事实撤回与一项口径修正

#### A. E12 把 `event_time` 当成了“已经采到”的时间

`canonical_events.event_time` 是新闻发布时间，**系统能否在决策时使用它必须看 `first_seen_at`**。实库复核：

| headline | `event_time` | `first_seen_at` | 11-05 08:35 ET DA 是否可用 |
|---|---|---|---|
| `Chip selloff drags global stocks lower…` | 11-05 02:35 UTC | **11-05 18:49 UTC** | **否**，盘前尚未入库 |
| `AI stocks fall, oil bounces back…` | 11-05 02:35 UTC | **11-05 15:04 UTC** | **否**，盘前尚未入库 |
| `Morning Bid: Markets fret blue wave will drown data centres` | 11-04 05:41 UTC | **11-04 18:39 UTC** | **是** |
| `The post-election rally masks a sharp split in the AI trade…` | 11-04 19:21 UTC | **11-04 19:54 UTC** | **是** |

所以，“Chip selloff 在盘前 11 小时已经入库”必须撤回；它不能用于解释盘前为何没有避险。
**但 D1 的目标没有被整体证伪**：后两条选后 AI 分化新闻在 DA 前已经入库，确实可以提供更早的事件输入。
后续所有回放必须以 `first_seen_at <= as_of` 过滤，禁止用后见新闻构造盘前先知。

#### B. D3/F5 用 `agent_bridge` 证明 DA 实际模型，链路不匹配

- DA 主模型在 `/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py:54` 自己构造 DashScope 请求，
  在 `:116` 直接 `urlopen`，并在 `:134` 保存 API 信封 `_model_reported`；它不是 `app.llm.model_call(feature='agent_bridge')`。
- `/root/.hermes/skills/deep-stock-analysis/scripts/config.py:66` 的 DA 默认主模型是 `qwen3.7-max`。
- 11-05 的 **36/36** checkpoint 顶层记录 `_ckpt_model=qwen3.7-max`，且内层 `_llm_output._model_reported` 的 API 回显为 `qwen3.7-max`，
  挑战者回显 **36/36 = deepseek-v4.1-flash**。
- E10b 的 `agent_bridge` 调用集中在 **21:32–21:34 UTC**，DA 指令生成于 **13:35 UTC**，既非同链也非同时点。

因此“DA 实际跑的是 deepseek-v4-pro”没有证据支持。`pipeline.py:2357` 写静态 `config.LLM_MODEL` 的确可以改成
优先保存 `_model_reported` 以增强健壮性，但本批静态值与 API 回显一致，不能称为已经发生的归因失真。

#### C. BUY 9 与 BUY 11 是不同状态层，不是谁算错

| 口径 | 数量 | 原因 |
|---|---:|---|
| DA bridge 原始候选 | **18 = 15 LONG + 3 SHORT** | `opportunity_scan_2026-11-05.json` |
| 费用门后原始可执行方向 | **14 = 11 BUY + 3 SELL_SHORT** | ADMA/RIVN/RUN/TIC 改 SKIP；盘前报告保留该原始方向 |
| 当前 DB 可变状态 | **12 = 9 BUY + 3 SELL_SHORT** | GOOG/LMT 后续 `entry_confirm_reversed` 又改 SKIP |

这 6 条后来变 SKIP 的行，`raw_instruction_json.direction` 仍保留原始 BUY。
因此 E3 应命名为“当前 DB 方向”，不能用来否定“费用门后的原始方向 BUY 11”。任何方向/收益审计必须声明使用
bridge、raw original direction 还是 current mutable direction。

### 14.3 两日市场口径必须分开

- **11-04 首个选后完整收盘**：本批 DA 输入材料记录三大指数上涨、AI 硬件/存储分化下跌；选举审计回答的是这一日后的盘前决策。
- **11-05 常规时段**：本 DEB 的 5m 回放显示 SPY 09:30 为 −0.16%、09:55 为 −1.59%、15:55 为 −4.05%。

若用户说“11-05 大部分美股大跌”，不能用 11-04 的指数上涨反驳。两份 DEB 研究的是**连续但不同时段**的问题：
盘前能否识别选后结构性风险，以及盘中价格急跌门是否有效。二者不能混成同一市场日叙事。

### 14.4 原 D1 为什么不足以实现用户目标

1. **风险词过窄且只偏空**：`Morning Bid…blue wave…data centres` 和 `sharp split in the AI trade` 不含原方案的
   selloff/plunges/rout/crash 等词，按原过滤器仍可能漏掉；同时它没有机会侧和相对受益侧。
2. **一行 headline 不等于推理输入**：缺事实/观点分层、跨源 corroboration、价格确认、已定价程度、传导时窗和失效条件。
3. **每股重复读新闻不等于全面**：当前 36 股各自摄取、各自解释，覆盖不一致且重复消耗 token；需要的是一次市场级共识简报 + 每股本地证据。
4. **提及不等于产出方向**：选举审计实测只有 4/36 主因果链使用选举，18 只 bridge 仅 TSLA 由选举主因驱动。
5. **挑战文本不等于裁决**：NVDA/SNDK 的挑战者反证和 TSLA 的实质质疑没有充分改变指令；不补裁决契约，新增信息仍可能只成为 risks 文案。
6. **D1 是信号源行为变更**：它可能改变 direction/score/SKIP/bridge，不能称“零行为变更”。上线后才看 5 日 SKIP 比例，不能替代上线前离线 A/B。

### 14.5 目标架构：Event Intelligence v1（复用现有主管线）

```text
现有 EventStore / FRED / 行情与板块快照
  → as-of 选择（first_seen_at <= batch_as_of，杜绝未来数据）
  → 去重、跨源确认、事实/观点分层、源健康与Stale标记
  → 每批一次 MarketEventBrief（全局主模型；有事件时全局挑战者复核）
  → 同一简报注入 DA 36 股；UE 仅复用简报做受影响标的提名
  → 每股主模型 + 挑战者输出方向、价格已反映程度、失效条件和证据ID
  → 结构化仲裁（先离线， unresolved fatal flaw 不得静默发布）
  → 现有 Stage2 / signal gate / Playbook 唯一指令与执行链
```

**不新建**第二交易管线、宏观硬门、全局 RISK_OFF 分类器或另一套 EventStore。
UE 直接入场仍关闭；其标的只走既有 `scan --da-feed` 供 DA，是否进入深分析仍由既有资格和预算裁决。

#### A. 每批共享 `market_event_brief_v1`

```json
{
  "schema": "market_event_brief_v1",
  "as_of": "批次时点",
  "event_hash": "输入事件集合哈希",
  "facts": [
    {"event_id": "可回库核查", "event_time": "发布时间", "first_seen_at": "系统可见时间",
     "source_tier": "T0/T1/T2", "status": "UNVERIFIED/CORROBORATED"}
  ],
  "market_evidence": [
    {"metric": "指数/板块/利率/油价", "value": "实际值", "as_of": "行情时点", "source": "来源"}
  ],
  "transmissions": [
    {"event_ids": ["事实依据"], "theme": "AI硬件/云软件/医疗等", "sign": "POSITIVE/NEGATIVE/MIXED/UNKNOWN",
     "horizon": "INTRADAY", "price_in": "PRICED/PARTIAL/UNPRICED/UNKNOWN",
     "invalidation": "可观察失效条件", "confidence": 0.0}
  ],
  "unknowns": ["没有证据就必须显式保留的缺口"]
}
```

- `sign` 是主题层研究假设，**不是直接交易指令**；每股方向仍须本地基本面、技术面、价格和可执行性共同决定。
- `price_in` 必须有前日/盘前价格、量能或板块反应作证；缺数据只能 `UNKNOWN`，禁止把“我认为错杀”当成事实。
- 所有数字必须能回指事件或行情来源；媒体推测与官方事实不得混写。

#### B. 每股方向与指令契约

主模型与挑战者都必须给出机器可判字段，而不是只写叙事：

| 字段 | 允许值/用途 | 消费方 |
|---|---|---|
| `event_role` | `PRIMARY/CONFIRM/CONTRARY/NONE`，防止事后把公司事件交易包装成宏观交易 | 离线 comparator；转 live 前的报告与审计 |
| `event_ids` | 本股实际使用的事件证据集合 | grounding 校验、报告、回放 |
| `independent_direction` | `BUY/SELL_SHORT/SKIP` | 现有辩论合并与方向比较 |
| `price_in` + 证据 | 判断事件是否已被价格反映 | 挑战者核验、离线方向比较 |
| `opposition` | `NONE/ABSTAIN/OPPOSE`，替代歧义的 `DISAGREE+SKIP` | 仲裁器 |
| `fatal_flaw` + `evidence_ids` | 足以否定方向或入场条件的实质错误；无则显式空 | 仲裁器、修复重试、审计 |
| `entry/stop/invalidation` | 可执行计划及失效条件 | 现有 Playbook/门禁 |
| `valid_until` | 当日指令有效期 | 离线 validator；live 前必须新增 Playbook 双头检消费者与契约测试 |

离线阶段这些字段的消费者是 comparator/validator；转 live 前必须按 AGENT 的 prompt 字段登记制注册真实消费者与基数自检，
未登记不得加入生产 prompt。

#### C. 双模型仲裁必须先解决“反对但无效”

- `ABSTAIN`：没有独立方向，不等于反对，维持现有中性语义。
- `OPPOSE`：必须给反方向或明确的 `OPPOSE_SKIP`，并附证据；禁止再用 `DISAGREE+SKIP` 同时表达两种状态。
- `fatal_flaw` 非空：在 R3 影子仲裁中进入 **repair/SKIP** 分支，不得只追加 risks 后按原 score 发布。
- 普通 PARTIAL、非致命瑕疵：保留现行 WARN/置信度机制作为对照，避免把所有质疑都硬 BLOCK。

R3 是独立变量。Event Intelligence 的 A/B 先证明“更好信息是否改变并改善方向”，再评仲裁；不得同批叠加。

### 14.6 高效实现：全局一次、缓存复用、按需挑战

以下是**每个盘前批次或盘中简报版本的工程验收预算，不是当前实测成绩，也不是全天调用上限**：

| 项 | 预算 |
|---|---|
| 全局 LLM 调用 | 无事件 0 次；有事件主简报 1 次 + 全局挑战至多 1 次 |
| 额外批次时延 | P95 ≤120s，且既有 08:57 ET 截止不变 |
| 每股新增 prompt | ≤1200 tokens；只传相关 transmission，不复制整库新闻 |
| 缓存 | `event_hash + prompt_version + model`；同批 36 股共享，事件未变不重复生成 |
| 降级 | 全局简报失败 → 确定性 as-of 事件行 + `degraded=true`；不得阻塞 DA 或伪造“无事件” |
| 观测 | 全局/每股 latency、token、event coverage、grounding、模型回显全部落离线产物 |

“全面”不等于把 22,541 条事件塞给模型。应按 as-of、materiality、novelty、corroboration 和相关性压缩成少数事件簇，
同时保留 source health、漏采和 unknowns；否则只是更慢、更贵、更矛盾的 prompt。

### 14.7 实施顺序与验收（更新 F1）

#### R0：确定性 as-of 原型（无 LLM、无生产消费者）

- 从现有 `EventStore.get_canonical_events_since()` 读取，按 `first_seen_at` 截止、去重并生成事件簇清单。
- 11-05 08:35 ET 冒烟必须选到 `Morning Bid…data centres` 与 `post-election rally…AI split`，
  **不得选到**上述两条 02:35 UTC 新闻。
- 输出 source/event/time/corroboration/unknowns，验证事件覆盖、去重和时延；此步通过前不调用 LLM。

#### R1：全局 MarketEventBrief 离线 A/B

- 先跑 11-05 冻结事件集 smoke，再扩展到预先选定的事件日、轮动日、无事件日；不得只挑崩盘日。
- 验收：引用事件 100% 存在且 `first_seen_at<=as_of`；无来源数字为 0；事实/观点、price_in、unknowns 可机检。
- R0/R1 只验证“信息是否全、快、可追溯”，不宣称方向或收益改善。

#### R2：36 股方向与指令离线 A/B

- 对照组 = 原 11-05 checkpoint；实验组唯一变量 = 同一 MarketEventBrief。模型、温度、原 prompt 其他部分、候选池、门禁全部冻结。
- 使用现有 `Pipeline(output_dir=<隔离目录>, db_write=False, pool_write=False)`；无 cron、无 notifier、无生产 DB/checkpoint 覆写。
- 比较 `event_role`、方向/score 变化、挑战者 fatal flaw、bridge 增减、token/时延；AMZN 等只有 thesis 真采用事件时才允许记为宏观受益。
- 扩大样本后按 entry/open 双基准、绝对与相对 SPY 收益分别评估方向；交易经济结果仍用 DA-only 费后净 bps、日聚类、样本量/MDE。

#### R3：仲裁影子与上线裁决

- 单独比较现行合并与 `OPPOSE/fatal_flaw` 结构化仲裁，测供给、误杀、修复成功率和净经济价值。
- R1/R2 不达标则不进入 R3；任何 live prompt/合并行为变更仍需独立开关、用户批准和回滚。

### 14.8 更新后的推荐裁决

1. **推荐直接实施 R0→R2 离线原型**，这正是“更全面、更快并能产出方向”的最小可验证路径；不等于批准 live D1。
2. **不批准**原 D1 以“零行为变更”名义直接进生产；原 P1/P2 只能作为后续 live 观察，不能替代离线 A/B。
3. **撤回** E12 的盘前已入库说法和 D3 的已证实模型失真说法；F5 不做模型切换，DA 模型比较另案。
4. 轴 A/B 维持不加价格门；D2 保持独立 record-only 议题，不与本事件链捆绑。
5. 若只做一件事，先做 **R0 的 as-of 事件选择器**：它既防止未来数据，也是后续全局简报、方向产出和回放可信度的共同地基。

## 十五、日内交易运行模型（用户补充需求，2026-11-06 08:13 CST）

### 15.1 目标不是“新闻摘要”，而是当日可执行决策

用户的核心需求应落成以下闭环：

```text
当天新事实 → 跨资产/板块传导 → 受益与受损标的 → 可成交方向
          → entry/stop/target/invalidation/valid_until → 新证据下修订或作废
```

- **盘前**回答“今天有哪些重要主题、哪些标的可能受益/受损、什么价格条件才值得交易”。
- **盘中**回答“出现了什么新事实或证伪、哪些现有判断需要改变、指令是否仍有效”。
- **日落前**结算指令有效性；未执行指令不得把昨天的叙事带入第二天。已成交仓位仍由现有 Playbook 出场链管理，
  不因本方案新增强制平仓，也不得以旧简报替代当日持仓风险评估。
- 新闻负面情绪不自动等于 SHORT，利空下跌也不自动等于追空；必须经过 `price_in`、标的基本面、相对强弱、入场和费用判断。
  没有可执行优势时，合理产出是 **SKIP**。

这不是新增市场硬门或新入场通道，而是同一个 DA Event Intelligence 实验的信息与决策层升级。

### 15.2 信息全面性的地基：连接器管事实，LLM 管理解与推理

不能让模型依赖参数记忆“知道今天发生了什么”。DashScope 直连本身不是实时新闻源；即使后续给 LLM 配搜索工具，
搜索结果也必须先规范化落证据库，再进入推理。

| 层 | 必须负责 | 不得负责 |
|---|---|---|
| 确定性采集 | 新闻/官方披露/公司事件、指数与板块、市场宽度、利率/美元/波动率/商品行情；保存原文、URL、来源和时区 | 不根据标题直接生成交易方向 |
| 时间账本 | `event_time`、`first_seen_at`、`as_of`、行情 `observed_at`；统一 UTC 持久化并保留 ET 会话日 | 不用事件发生时间冒充系统当时已知 |
| LLM 信息扩展 | 查询扩展、同义事件聚类、遗漏线索提名、跨事件因果链、二阶受益/受损映射、反证与未知项 | 不虚构来源、数字、报价或“已确认”事实 |
| 确定性校验 | 证据 ID/时间/数字回指、schema、方向价格关系、指令状态、幂等和预算 | 不替 LLM 写宏观叙事 |

“全面”以覆盖矩阵和漏报审计衡量，而不是以塞入多少条新闻衡量。每个简报必须同时带 `source_health`、采集缺口和 `unknowns`；
源故障时明确 `degraded=true`，不能把“没采到”写成“今天无重大事件”。

### 15.3 两个时间尺度：盘前全量构图，盘中增量推理

#### A. 盘前基线（现有 DA 主批）

1. 以上一交易日收盘至本批 `as_of` 的事件为主，并保留仍在影响当天的未失效旧事件。
2. 生成一次共享 MarketEventBrief：事实/观点分离，给出主题、多空传导、相对受益/受损、价格反应和失效条件。
3. 同一简报进入现有 DA 候选深分析；每股只接收相关 transmission + 本地证据，避免 36 股重复阅读全库。
4. 产出首批日内指令卡；后续 Stage2、signal gate、费用门和 Playbook 保持原样。

#### B. 盘中增量（先冻结回放，暂不接生产）

1. 采集后只推进 `(first_seen_at, event_id)` 游标；允许短重叠窗处理迟到/回填事件，再以事件 ID 幂等去重。
2. 无新增重大事件、无既有证据修订、无相关价格证伪时：**0 次 LLM、0 次新指令、默认静默**。
3. 有新事件时，将“上一不可变简报版本 + 新事件簇 + 最新行情快照”交给全局主模型，生成带 `supersedes` 的新版本；
   全局挑战者至多复核一次。旧版本永不改写，保证任何时点可重放。
4. 只深分析受影响标的：现有未执行指令、当日 DA 候选及经既有资格链提名的标的。UE 仍只供料，不恢复直接入场。
5. 纯价格跳动由现有行情门、tripwire/thesis-drift 和 Playbook 处理；没有新事实时，不让 LLM 每几分钟为波动编故事。
6. 调度器可以频繁做廉价游标检查，但不得每 5 分钟无差别重跑全市场双 LLM。重大事件去抖窗、每日调用预算和降级策略
   必须在 R1 用真实事件分布预注册，不能无上限循环。

采集时延 `event_time→first_seen_at` 与处理时延 `first_seen_at→brief→instruction` 分开计量；R0 必须同时输出
`published_to_seen_ms` 的 P50/P90/超窗占比，区分“当时无事件”与“事件晚到”。
§14.6 的 P95≤120s 只约束系统已见事件后的新增处理，不冒充外部新闻源的全链路时效保证。

### 15.4 日内指令卡与安全生命周期

最小机器契约如下；离线阶段由 comparator/validator 消费，live 前逐字段登记真实消费者：

```json
{
  "schema": "intraday_instruction_v1",
  "session_date": "ET 交易日", "symbol": "US.XXX", "as_of": "决策时点",
  "brief_version": "不可变简报版本", "event_ids": ["实际主因证据"],
  "event_role": "PRIMARY/CONFIRM/CONTRARY/NONE",
  "direction": "BUY/SELL_SHORT/SKIP",
  "entry_trigger": {"type": "机器可判类型", "operator": ">/</...", "price": 0.0},
  "stop_loss": 0.0, "target": 0.0,
  "invalidation": "可观察证伪条件", "valid_until": "ET 当日时间",
  "price_in": {"status": "PRICED/PARTIAL/UNPRICED/UNKNOWN", "evidence_ids": []},
  "opposition": "NONE/ABSTAIN/OPPOSE", "fatal_flaw": null
}
```

- `BUY/SELL_SHORT` 必须有数值化入场、止损、目标和当日 `valid_until`；方向、止损/目标价格关系、费用和既有 RR 由确定性校验。
  `SKIP` 必须保留主因，不为了“有产出”而强给方向。
- `valid_until` 应取催化有效期与当日会话终点的较早者。**当前仓库检索不能证明它已是 Playbook 消费字段**；
  live 前必须在 `run_playbook_auto_execute` 与 `_check_intraday_trigger` 两处头检统一拒绝过期指令，并做契约测试，不能只落 JSON 文案。
- 新证据导致变化时，由确定性 reconciler 比较新旧卡并执行 `NO_CHANGE/ISSUE/REVISE/INVALIDATE`，不是让 LLM 直接改数据库状态。
  每次修订保留 `instruction_version/supersedes_decision_id` 和完整输入快照。
- 只有**未触发、未成交、无券商单、无在途 entry_confirm** 的指令才允许被后续版本 supersede；其余旧指令和执行状态不可覆盖，
  更不能自动反手。已成交仓位只走现有持仓/出场链。
- `scripts/da_fresh_event_batch.py` 当前刻意剔除当日已有指令标的，因为底层按 `date+symbol` UPSERT 会覆盖执行状态。
  因此它只能复用隔离目录、锁、`db_write=False/pool_write=False` 等机制，**不能直接拿来实现盘中改单**。
- EOD 到期只作废未执行指令；跨日重评必须生成新会话日、新 `as_of`、新简报版本，禁止复活旧指令。

### 15.5 双 LLM 的有效分工：一个构建，一个试图推翻

| 角色 | 必答问题 | 有效产出 |
|---|---|---|
| 全局主模型 | 今天最重要的事实是什么，经何种链条影响哪些板块/标的，哪些已反映 | 有证据 ID 的多空传导图、未知项、失效条件 |
| 全局挑战者 | 是否遗漏相反事件，是否把相关性当因果，是否有更合理解释，价格是否已走完 | `ABSTAIN/OPPOSE`、反证、fatal flaw，不写泛泛 risks |
| 标的主模型 | 全球主题与本公司/技术/价格/费用合并后，具体做什么 | LONG/SHORT/SKIP 对应的可执行指令卡 |
| 标的挑战者 | 入场是否追高/追空，证据是否支持本股方向，止损目标是否自洽，何条件应不做 | 独立方向、证据化反对和修复点 |
| 确定性仲裁/校验 | 字段、证据、时间、状态和价格关系是否有效；fatal flaw 是否解决 | 接受、至多一次 repair、SKIP 或保留现行 WARN 对照 |

`ABSTAIN` 不是反对；`OPPOSE` 必须携带反方向或 `SKIP` 建议及证据。存在有证据且未解决的 fatal flaw 时，
R3 影子应进入 repair/SKIP，而不是像 TSLA 案例一样只追加风险文案后原方向发布。普通措辞分歧不得升级成硬 BLOCK。
R2 仍冻结现有双模型调用方式，只改变共享事件简报；上述仲裁规则到 R3 才单独比较，避免同批改变两个变量。

### 15.6 验收必须同时量“全、快、准、可执行、经济”

| 维度 | 主指标与硬要求 |
|---|---|
| 全 | 对事后裁定的重大事件集计算 as-of 召回/漏报；逐源覆盖、重复簇、source health、unknowns；事后标签只用于评分，绝不回填输入 |
| 快 | 分别报采集、全局简报、标的指令 P50/P95；采集侧另报 `published_to_seen_ms` P50/P90/超窗占比；无新事件 LLM=0；按日/事件报告调用数、token、费用与降级率 |
| 准 | 引用事件 100% 存在且 `first_seen_at<=as_of`，无来源数字=0；方向变化必须能回指 `event_role/event_ids`，不能只看新闻提及率 |
| 可执行 | 可交易卡字段完整率 100%；方向价格关系/费用/RR 校验；过期执行=0、重复券商单=0、已执行状态被覆盖=0 |
| 方向价值 | 与原 DA 冻结 checkpoint 同标的比较；报实际 entry 与决策后首个可成交价格双基准，绝对及相对 SPY/板块收益并列 |
| 交易经济 | 主读数为 decision 后至实际出场/当日收盘的 DA-only 费后净 bps；15/60 分钟只作诊断，不做阈值购物；按日聚类 80% CI、n/MDE 必报 |

11-05 只能作为一个冻结回归日，不得据此调参或宣称收益改善。盘前与盘中分开出分，避免把盘前正确、盘中追错混成一个平均数；
任何“Event Intelligence 有 edge”的结论仍需满足当前 OKR 的样本与 DA-only 口径。

### 15.7 实施顺序与停止条件

1. **R0a 盘前快照**：完成 §14.7 的 as-of 选择器和 11-05 smoke，落事件覆盖及 `published_to_seen_ms` 时延分布。
2. **R0b 日内游标回放**：在同一冻结事件账本上按时间推进；盘前不得看到 `first_seen_at=11-05 18:49 UTC` 的迟到新闻，
   游标到达后必须能看到；覆盖迟到、重复、撤回/修订、源中断和无事件日。
3. **R1 初始简报 + delta 简报**：验证 grounding、覆盖、版本不可变、处理时延和零事件零调用，不评收益。
4. **R2a 盘前方向 A/B**：按 §14.7 对 36 股隔离重放，唯一变量为共享简报。
5. **R2b 盘中指令 A/B**：冻结价格与事件时间线，比较原指令、新事件指令和反事实失效；增加过期、改单、在途订单保护测试。
   R2a/R2b 依次进行，不与 R3 仲裁同批叠加。
6. **R3 独立仲裁影子**：验证 `OPPOSE/fatal_flaw` 是否提高费后净价值，而非只增加 SKIP 或文本。

R0–R2 全部使用隔离产物，无 cron、无 notifier、无生产 DB/checkpoint 覆写，也不新增常驻影子任务。
若 R1 不能证明信息覆盖/时延改善，停止，不继续加 prompt；若 R2 只有叙事变好而方向/费后经济不改善，停止，不接 live。
live 前置另需用户批准、独立开关、字段消费者/基数/单位契约、版本化改单与 `valid_until` 双头检测试、回滚方案。

**下一步最小实现仍是 R0a+R0b，而不是直接改生产 prompt。** 先把“当时知道什么、何时知道、后来新增什么”做成可信地基，
再让双 LLM 负责它最擅长的跨事件推理和标的映射，才能同时追求全面、快速与可执行。

---

## 十六、第一 Agent 对第二 Agent 复核的逐条核查（Hermes，2026-11-06 08:20 CST）

> 性质：**只读核查**。对 §十四 的每条可判真伪的声称独立复跑，给出接受/不接受。
> 未改动任何生产代码/配置/cron；本轮只改本文件（撤回与自曝，见 §一 就地更正与本节）。

### 16.1 裁决：复核成立，我的两处事实错误成立，一处口径分层纠正成立

| §十四 的声称 | 我的复核命令 | 实测 | 判定 |
|---|---|---|---|
| `event_time` ≠ 可见时间；两条 02:35Z 新闻 `first_seen_at` = 15:04Z / 18:49Z，晚于 DA 批次 | `venv/bin/python3.11 -c "import sqlite3;c=sqlite3.connect('data/event_stream.db');print(c.execute(\"select headline,event_time,first_seen_at from canonical_events where headline like '%Chip selloff%' or headline like '%blue wave will drown%' or headline like '%sharp split in the AI trade%' order by event_time\").fetchall())"` | `2026-11-05T18:49:08.657800+00:00` / `2026-11-05T15:04:09.554085+00:00`；`Morning Bid…` = `2026-11-04T18:39:07`、`sharp split…` = `2026-11-04T19:54:06` | **✅ 接受，我错**。E12 已撤回（表内划除）；「盘前 11 小时已入库」不成立 |
| DA 主模型 = qwen3.7-max（API 回显 36/36），挑战者 = deepseek-v4.1-flash（36/36） | `venv/bin/python3.11 -c "…glob da_checkpoint…; print(dict(c1),dict(c2))"`（E14 原文） | `{'qwen3.7-max': 36} {'deepseek-v4.1-flash': 36}` | **✅ 接受，我错**。E10b 已撤回；§一#2 就地改判为「前一轮说法成立」 |
| DA 不经 `agent_bridge`；后者在 11-05 的调用集中在 21:32–21:34Z，与 13:35Z 的 DA 批次不同链不同时点 | `venv/bin/python3.11 -c "…select created_at,feature,model from llm_calls where feature='agent_bridge' and created_at like '2026-11-05%' order by created_at limit 3"` | 首行 `2026-11-05 21:32:11 / agent_bridge / deepseek-v4-pro`（7 条全部 21:32–21:34） | **✅ 接受**。我据此推「DA 实际跑 pro」属跨链误推 |
| BUY 11 与 BUY 9 是不同状态层：bridge 18(15+3) → 费用门后 11 BUY → DB 当前 9 BUY | E15 + `…select symbol,skip_reason,raw_instruction_json … direction='SKIP'` | `qwen3.7-max 18 {'LONG': 15, 'SHORT': 3}`；6 条 SKIP 的 raw 方向仍为 BUY（ADMA/RIVN/RUN/TIC 费用门、GOOG/LMT `entry_confirm_reversed`） | **✅ 接受**。我「11 无法复现」的更正**本身是错的**，已在 §一#1 撤回 |
| 本轮未重跑 E6 全窗，不声称独立重算轴 B | —（其自陈） | 与事实一致（节内无轴 B 新读数） | **✅ 接受**（自陈边界清楚，不冒充实测） |

### 16.2 两处需要精确化的表述（不影响结论方向）

1. **`_model_reported` 的字段位置**：§14.2-B 写「checkpoint 同时记录 `_ckpt_model`、`_model_reported`」。实测 `_model_reported`
   在 **`_llm_output` 内层**（`_llm_output._model_reported`），checkpoint 顶层只有 `_ckpt_model`（静态配置值）。
   两者**独立取证**才成立：`_ckpt_model` 只是静态字面量，**真正的证据是内层 API 回显**。写 D3 修复时若只读顶层会取到 None。
2. **DA 批次时点**：§14.2-A 原表头写「11-05 08:03 ET DA 是否可用」，而 `opportunity_scan_2026-11-05.json` 的
   `generated_at = 2026-11-05T08:35:26-05:00`，指令落库 13:35Z = 08:35 ET。08:03 是另一个时点。
   **结论不变**（两条新闻分别在 10:04 ET / 13:49 ET 才入库，均晚于 08:35 ET），但时点应写实，否则复查者按 08:03 复跑会对不上；现已在 §14.2/§14.7 统一修正。

### 16.3 复核未覆盖、但我认为必须补的一个缺口：**采集时延**

§14.2 只说「盘前尚未入库」，没答「**晚了多久、是不是常态**」。实测（E16）：

- 冻结截止 2026-11-06 00:00 UTC，11-05 当日 `FINNHUB_GENERAL_NEWS`：n=68，`published_to_seen_ms` 中位 **89.8 分钟**，**47/68（69%）超过 60 分钟**，最长 1129 分钟；
- 全库（11-01 起，n=16823）：中位 29.4 分钟，**p90 = 906 分钟（15 小时）**。

含义有三条，且都改变设计：
1. 上一轮「收集 ✅」只在「最终会入库」的意义上成立；**对 08:35 ET 的批次，时延直接决定可用性**。
2. R0 的 `first_seen_at <= as_of` 过滤器是必要但不充分的——它会把「晚到」判成「当时没有」。因此 **R0 产物必须同时输出时延分布**
   （`published_to_seen_ms` 的 P50/P90/超窗占比），否则「无事件」与「晚到」不可区分，而这正是本次事故的形态。
3. 这给出一条与「要不要 LLM 简报」正交、且**不需要裁决阈值**的观测项：把每日 `published_to_seen_ms` P90 与前一日对比，
   超窗占比劣化即告警。它属于记录面，可直接落（按缺陷修复处置，非行为变更）。

### 16.4 更新后的我的推荐裁决（替代 §八 F1 的原始推荐）

| 编号 | 更新后的推荐 | 理由 |
|---|---|---|
| F1 | **批准 R0（确定性 as-of 选择器 + 时延分布）**，再按 R1/R2 离线 A/B 决定是否进生产；**不批准**原 D1 以「零行为变更」名义直接上线 | §十四 §14.4 第 6 点成立：改变模型所见即信号源行为变更；我的 P1/P2 只能当上线后观察，不能替代上线前离线 A/B |
| F1-b（新增） | 采集时延观测（§16.3 第 3 条）单独立项，按缺陷修复落，不等 R0 | 零阈值、零行为变更，且是「无事件 vs 晚到」可区分的前提 |
| F2 / F4 / F6 | **维持**（轴 B 关闭 + 不接全局 RISK_OFF + 不启用 regime_classifier） | 本轮无新证据推翻 |
| F3 | **维持**（D2 独立 record-only，不与事件链同批） | 与事件推理正交，捆绑会污染 A/B |
| F5 | **改为**：本案不换模型；DA 主模型档位比较另开离线 bakeoff | 撤回「已经是 deepseek-v4-pro」的错误前提；换档是独立变量 |
| F7 | **维持**（隔夜缺口单独立轴） | 与本案正交，量级更大 |

### 16.5 未裁决 / 留给复查者

1. 我建议**保留 §六 原 D1 原文不删**（历史留档），正文以 §十四、§十五及本节为准 —— 若复查者认为应整段替换，请明示，我按裁决改。
2. E10b/E12 两行已加删除线并撤回；验证器 `scripts/verify_deb20261106.py` 的 E 行正则**跳过带删除线的行**（故 22 条中实检 15 条 E + 7 条 C；E10b/E12 两条撤回行不再参与机械比对）。
3. 本轮未复核 `DEB-20261106-da-election-reasoning-audit.md` 的全部读数（仅抽验其引用的 `4/36`、`4/18`、`1/18`、`22 只`与 `TSLA` 存在且与该文件内文一致）；该文件应另派独立复查。

---

## 十七、Event Intelligence 离线原型实施与实测（2026-11-06 09:43 CST）

### 17.1 本轮回答的问题与边界

原审计的结论仍是：**生产双 LLM 对选后分化只有部分捕捉、部分进入主因果，不能把硬门回避记成模型推理效果。**
本轮不是用文档再解释一次，而是把目标链做成可运行离线原型：

```text
生产 EventStore 只读 as-of 快照
  → 精确去重/事件哈希/采集时延/source health/日内 delta 游标
  → 全局 qwen 主分析 + deepseek 挑战者，共享 MarketEventBrief
  → 按公司行业筛选相关传导，注入同一 DA 双模型
  → BUY/SELL_SHORT/SKIP + entry/stop/target + event_ids + price_in + valid_until
  → ISSUE/REVISE/INVALIDATE/NO_CHANGE + 在途保护 + 独立 R3 仲裁
```

代码入口为 `app/event_stream/intelligence.py`、`app/event_stream/market_brief.py`、
`app/event_stream/instructions.py`；复现入口为 `scripts/da_event_intelligence.py`、
`scripts/da_event_instruction_replay.py`、`scripts/da_event_arbitration_replay.py`。
唯一派生验收总表是 `data/shadow/event_intelligence/acceptance_20261105_v1/summary.json`。

**没有完成的部分不得被“原型已落地”掩盖**：36 股 R2a 未跑；价格/收益反事实未跑；delta 有 51 个未选事件簇；
全局增量 prompt 仍偏大；生产 `valid_until` 和版本化改单消费者不存在；无任何费后经济改善证据。

### 17.2 R0：当时可见性、迟到事件与游标

- 生产 SQLite 以 `mode=ro` 读取，统一 `first_seen_at<=as_of`，快照和事件哈希不可变；原模糊去重未改，新增精确 URL/标题聚类。
  1,976 事件、三个时点由三分钟以上降至 **10.66 秒**。
- `r0_smoke_20261105_v4` 全部机器检查通过：盘前能看到 blue-wave/data-centres 与 AI split；盘前看不到两条迟到新闻；
  15:05 UTC 能看到 `AI stocks fall, oil bounces back...`，18:50 UTC 才看到 Chip selloff。
- 盘前 36 小时全类型窗为 **1,709 条**，`published_to_seen_ms` P50 **1,099,797 ms（18.3 分钟）**、
  P90 **29,248,323 ms（487.5 分钟）**，527/1,709（30.84%）超 60 分钟。该口径含 SEC 等全类型，
  不得与 §16.3 的 68 条 FINNHUB 新闻中位数 89.8 分钟混用。
- 无变化的 `NO_CHANGE + llm_calls=0`、重叠游标去重、UPDATED/RETRACTED、future-event 拒绝均有契约测试。

证据：`data/shadow/event_intelligence/r0_smoke_20261105_v4/smoke_summary.json`。

### 17.3 R1：真实双模型初始简报与增量修订

| 批次 | 调用 | 主/挑战时延 | 总时延 | 双调用 prompt tokens | 结果 |
|---|---:|---:|---:|---:|---|
| 13:35 UTC 初始 | 2 | 17.47s / 9.59s | **27.05s** | 39,851 | 3 transmissions；挑战者推翻油价因果跳跃；validation errors=0 |
| 15:05 UTC delta | 2 | 22.78s / 9.39s | **32.17s** | 84,154 | 3 transmissions；supersedes 初始简报；validation errors=0 |

- 两批均满足有事件全局调用不超过 2、新增时延低于 120 秒；这只是单批实测，**不是 P95**。
- delta 记录 99 条新事件，42 个 delta 簇入选、51 个未选。未选集已留在 selection ledger，不能宣称 99 条都被 LLM 审阅。
- 迟到标题通过 `member_facts` 进入 AMD 实际输入，不再被簇主标题遮蔽；初始简报的油价 fatal 不再原样沿用，
  delta 简报 `fatal_flaw=null`。这证明链路能修订，不证明新叙事正确。
- 两批 `degraded=true` 来自历史 source-health 行无法完整还原 as-of，不是 schema 或 grounding 错误。
- **效率债**：delta 双调用 84,154 prompt tokens 过高；本批为保持事件变量不再改全局 prompt。下一步应投影 prompt 专用 facts、
  去掉重复 URL/正文，并分别报告入选/未选覆盖，不能用每股 970 tokens 掩盖全局成本。

证据：`r1_brief_20261105_live_v1/market_event_brief.json`、`r1_delta2_live_v1/market_event_brief.json`
（均位于 `data/shadow/event_intelligence/`）。

### 17.4 R2：方向/指令卡、相关性与 Token 实测

指令卡缺字段不得默认成功；可交易方向必须有数值 entry/stop/target、正确价格几何、invalidation 和当日 ET `valid_until`。
控制与实验均关闭动态历史注入，唯一计划变量是共享简报；模型均为 qwen3.7-max + deepseek-v4.1-flash。

| AMD 批次 | 控制→实验 | 主新增 prompt | 挑战者新增 prompt | 双模型合计 | 预算结论 |
|---|---|---:|---:|---:|---|
| v2 全量段 | SHORT→SHORT | 1,999 | 1,830 | **3,829** | FAIL |
| v3 首次压缩 | SHORT→SHORT | 700 | 841 | **1,541** | FAIL |
| delta 最终压缩 | BUY→SKIP | 575 | 395 | **970** | PASS（≤1,200） |

最终 delta 卡为 VALID，`event_role=PRIMARY`，引用 `0cb864.../4bf4a0.../d501a9...`，并输出 SKIP；
这里 SKIP 是有证据的“不做”指令，不为凑供给强制产生 BUY/SHORT。

本批还修掉三个真实错误：
1. `ai` 子串误中 `causal_chain`，及全板块快照令所有股票都“相关”于油价/黄金；改为词边界、公司行业/业务标签和主题/因果链筛选。
2. 空结果不得回退注入全部无关主题；油价反证中 merely 提及 AI 也不能把 oil transmission 选成 AI 主题。
3. grounding 只接受**实际注入该股的事实 ID**；全局 fatal 仅在事件簇/传导证据相关时进入该股仲裁，不再全局污染。

HCA/NVDA/TSLA 的旧 v2 配对分别为 SKIP→BUY、SKIP→SHORT、SHORT→SHORT，均 VALID；但它们使用已 superseded 的宽相关段。
尤其 HCA 将 USD/gold 事件当 PRIMARY 的证据链，是**反例而非改善**，不得拿方向变化宣称 Event Intelligence 有效。

### 17.5 新的反证：相同控制输入并不稳定

三次 AMD 控制运行的 prompt SHA-256 完全相同（`9ef7f205...96e689ea`）、`_prompt_hash=50409228fb19`、温度均为 0，
主 prompt 均为 11,512 tokens，但方向为 **SHORT / SHORT / BUY**。因此温度 0 也不等于供应商侧完全可复现。

所以 delta 的 BUY→SKIP 以及此前单次 A/B 方向变化都只能记为**观察到的供给差异**，不能归因于事件简报，更不能宣称经济改善。
下一轮必须先做重复控制/固定控制响应，再以预注册重复次数比较方向分布；否则继续扩 36 股只会放大采样噪声。

### 17.6 R2b 生命周期与 R3 仲裁结果

- 初始 AMD 实验卡 SHORT → delta 候选 SKIP：未执行状态由 reconciler 判 **INVALIDATE / SKIP**；
  同一候选若 `executed=true`，判 **NO_CHANGE / EXECUTION_STATE_PROTECTED + review_required**。
- stale revision、过期未执行失效、方向/价位改单版本 lineage、触发中/券商在途/entry-confirm 保护、坏候选不覆盖好卡均有测试。
- R3 共检查 12 张 VALID 卡，另有 v1 两张 DEGRADED 被排除。按 arm 分列：

| 批次 | 控制 actionable 原→R3 | 实验 actionable 原→R3 | 说明 |
|---|---:|---:|---|
| AMD v2 | 1→1 | 1→1 | 无结构化反对 |
| AMD v3 | **1→0** | 1→1 | 仅控制臂被有证据 OPPOSE_SKIP 拦截 |
| HCA/NVDA/TSLA v2 | 1→1 | 3→3 | HCA 的 ABSTAIN+独立 SKIP 不硬挡 BUY，保留反例 |
| AMD delta | 1→1 | 0→0 | R3 未再改变供给 |

R3 没有减少任何实验臂 actionable 供给；上述计数包含重复实验批次，不是独立股票日样本，更不是收益统计。

证据：`data/shadow/event_intelligence/r2_amd_delta2_pair_v1/reconciliation.json`、
`data/shadow/event_intelligence/r3_all_valid_v1/summary.json`。

### 17.7 验证、停止条件与下一步

- 聚焦与 EventStore 回归共 **111 passed**；目标 Python 文件 `py_compile` 通过；`scripts/verify_deb20261106.py` **22/22、BAD=0**。
- 全部产物均为 `data/shadow/event_intelligence/` 隔离文件；生产 DB 写入=0、pool 写入=0、notifier=0、重启=0。
- **下一步 1（正确性）**：固定控制响应或做重复控制，先量化同 prompt 的方向方差；再决定是否扩 36 股。
- **下一步 2（覆盖/效率）**：压缩全局 prompt facts，审核 51 个未选 delta 簇的重大事件召回；采集 P90 与 source-health 历史化另按 §16.3 落地。
- **下一步 3（经济）**：只有通过方向稳定性后，才做 decision 后可成交价格/当日收盘的 DA-only 费后反事实；单日不得称 edge。
- **live 仍停止**：需用户单独批准、字段消费者/基数/单位契约、版本化指令存储、Playbook `valid_until` 双头检、在途单保护和回滚方案。

复现命令：

```bash
venv/bin/python -m pytest \
  tests/test_event_stream_phase1.py tests/test_event_intelligence.py \
  tests/test_market_brief.py tests/test_intraday_instructions.py \
  tests/test_instruction_lifecycle.py tests/test_instruction_arbitration.py -q

venv/bin/python scripts/da_event_intelligence.py brief \
  --snapshot data/shadow/event_intelligence/r0_smoke_20261105_v4/snapshots/0002_2026-11-05T150500p0000.json \
  --previous-brief data/shadow/event_intelligence/r1_brief_20261105_live_v1/market_event_brief.json \
  --output-dir <new-isolated-dir> --execute

venv/bin/python scripts/da_event_instruction_replay.py \
  --raw-root data/da_checkpoint/2026-11-05 \
  --brief data/shadow/event_intelligence/r1_delta2_live_v1/market_event_brief.json \
  --symbols AMD --output-dir <new-isolated-dir> --execute
```

---

## 十八、R0.5 重复性实测（第一 Agent 执行，2026-11-06 10:52 CST）

> 回应 §17.5 的自证与 §17.7 的「下一步 1」。**只读 + 隔离产物**，无生产写入、无 cron、无重启。
> 探针：`scripts/da_repeatability_probe.py`（复用 `da_event_instruction_replay.run_arm(arm='control', brief=None)`）；
> 合并读数：`data/shadow/event_intelligence/r0_5_repeatability_merged_v1/summary.json`。

### 18.1 设置

- 输入：`data/da_checkpoint/2026-11-05/US.AMD.raw.json` 的同一份 `collected`，**无事件简报**（控制臂）。
- 6 次独立进程调用（2 + 4 两批，同一 prompt sha `9ef7f205…96e689ea`，`identical_prompts=true`）。
- 温度沿用生产控制臂配置（0）；模型沿用 DA 生产主/挑战者（qwen3.7-max + deepseek-v4.1-flash）。

| 运行 | 方向 | score | status | event_role |
|---|---|---:|---|---|
| v1 #1 | SELL_SHORT | 6.0 | VALID | NONE |
| v1 #2 | SELL_SHORT | 6.0 | VALID | NONE |
| v1b #1 | SELL_SHORT | 6.0 | DEGRADED | NONE |
| v1b #2 | SELL_SHORT | **8.0** | VALID | NONE |
| v1b #3 | **SKIP** | 6.0 | VALID | NONE |
| v1b #4 | SELL_SHORT | **8.0** | VALID | NONE |

`direction_counts = {SELL_SHORT: 5, SKIP: 1}`；`score_values = [6.0, 8.0]`；`status_counts = {VALID: 5, DEGRADED: 1}`；
`stable_direction = false`、`stable_score = false`。

### 18.2 三条结论（只对这份输入负责）

1. **同输入同温不保证同方向**：6 次里 1 次翻成 SKIP（≈17%）。与 §17.5 的 3 次（SHORT/SHORT/BUY）**同向证据**：
   方向翻转率在 1/6 与 1/3 之间，单次 A/B 的「方向变了」不具备可归因性。
2. **score 也不稳定**：同输入出现 6.0 与 8.0 两个值。任何按 score/confidence 分档的对照（含仓位乘数、门槛比较）
   在单次样本上同样不可分辨 —— 这条比方向更隐蔽，因为 score 看起来「连续、稳定」。
3. **契约层有噪声**：一次 `DEGRADED`（`bear_case` 论据缺数字锚点）出现在同输入的第 3 次。
   ⇒ 「VALID 率」本身也带方差，不能拿单次 VALID/DEGRADED 当质量结论。

### 18.3 对裁决的影响（更新 F1 前置）

- R2 已观测到的 `BUY→SKIP`（AMD delta）**落在本节的噪声带内**，不得作为 Event Intelligence 有效的证据；
  这与 §17.5 的自我判定一致，本节提供独立复现。
- **F1 硬前置（建议同批写入）**：任何方向/score 对照必须先给出「同输入重复 N 次的分布」作为噪声基线；
  N 的建议起点 ≥10（本批 6 次已出现 1 次翻转，按 1/6 估计，要区分 20pp 的方向差需 N≈40+ 量级，届时另算 MDE）。
  未给噪声基线的 A/B 一律记「不可归因」。
- 成本参照（供裁决）：单次控制臂 ≈ 主+挑战 2 次调用、实际耗时 ≈ 130–150s、prompt ≈ 11.5k tokens。
  本节 6 次 ≈ 13–15 分钟墙钟。**产物未记 $ 成本字段**，故金额不可自证（这是观测缺口，非本轮可补）。

### 18.4 同批落地的一处守卫（非交易变更）

新增 `tests/test_offline_prototype_isolation.py`（3 例，已跑通）：断言 ①`app/event_stream/__init__.py` 不 re-export
离线原型三模块 ②`app/` 下无任何指向它们的 import ③检测器有牙（植入 import 必须判红）。
负向验证：人为在 `app/event_stream/relevance.py` 尾部植入 `from app.event_stream.intelligence import build_snapshot`
⇒ 该测试转为 1 failed，恢复后 3 passed（已复原，`git diff` 为空）。
目的：把 §17.1「零生产消费者」从口头声明变成可执行契约，堵住「下次重启静默加载 62KB 离线代码」的口子。

---

## 十九、模型能力对拍：deepseek-v4.1-flash vs qwen3.7-max（第一 Agent 执行，2026-11-06 11:5x CST）

> 用户要求「不能只看 max/flash 的名字定能力」。本节只报**实测**，不报名字直觉。
> 探针：`scripts/da_model_swap_probe.py`（`--mode battery` 客观小项；`--mode repeat` 真实 DA 任务换主模型）。
> 产物：`data/shadow/event_intelligence/{battery_max_v3,battery_flash_v3,model_swap_flash_repeat_v1,r0_5_repeatability_v1c}/`。零生产写入。

### 19.1 客观小项（可判对错，5 题，直连端点绕开 DA wrapper 的 json_mode）

| 小项 | qwen3.7-max | deepseek-v4.1-flash | 说明 |
|---|---|---|---|
| json_contract | PASS | PASS | 严格 JSON + 枚举 + 置信区间 |
| arith_notional | PASS | PASS | 1200×45.5=54600 |
| multi_constraint_order | PASS | PASS | 先过滤(<98 剔 B)再降序 ⇒ C,A |
| numeric_extract | PASS | PASS | 千分位/小数位不失真 |
| abstain_on_insufficient | **FAIL(答"0")** | **FAIL(空, 撞 max_tokens=600)** | 信息不足应拒答 |

- 打平 **4/5**，失败项相同；小项时延中位数 **5.5s（max） vs 1.3s（flash）**。
- 但**失败形态不同**：max 直接编了一个数 `0`（幻觉）；flash 不编数，而是持续思考撞到 600 token 上限后**返回空**。
  ⇒ 在"拒答"这一维度 flash 比 max 更保守，代价是空响应（下游需按空值处理）。
- 判分器本身修了两处假红（记录在此以免下游误读）：①`-1,234.50` 带负号导致数值比对失败 ②`54,600` 千分位被切成 `54`/`600`。修后两模型各 4/5。

### 19.2 真实 DA 任务换主模型（同一 raw、无事件简报、温度 0）

| 主模型 | 运行数 | 方向分布 | score | status | 时延中位 |
|---|---:|---|---|---|---:|
| qwen3.7-max（v1+v1b+v1c） | 12 | SELL_SHORT **11** / SKIP 1 | {6.0, 8.0} | VALID 11 / DEGRADED 1 | 161.8s |
| deepseek-v4.1-flash | 6 | **SKIP 6** | {5.0} | **DEGRADED 6** | 114.6s |

- flash 6/6 报 `_error="LLM synthesis failed"`、只产生 1 次调用（主调用即失败），
  卡片回落为默认 SKIP 并带 8 项契约违约（`missing_event_ids` / `missing_price_in_*` / `challenger_missing_*`）。
  **若原样上线，等于 DA 永久 SKIP（不开仓）。**
- 但**不是"能力不足"**：同一 `run_arm` 另跑的 1 次 flash 调用**成功**（2 次调用齐全、契约字段齐、
  `_finish_reason=stop`、`_model_reported=deepseek-v4.1-flash`）。⇒ 失败是**间歇性**，不是结构性。
- 已定位的机制（代码级）：`llm_synth.synthesize` 在**关思考**分支用 `max_tokens=3000, timeout=60`；
  成功那次是**开思考**分支（`max_tokens=16000, timeout=240`）。
  ~~关思考时被 60s 硬墙掐断（6 次失败耗时高度一致 108–115s ≈ 60s×2 含重试）~~
  **本条归因已被 §二十 证伪**：失败与超时无关，是 `max_tokens` 被推理 token 吃穿导致 JSON 截断。
- 换档的真实成本（同输入 US.AMD，主调用）：
  | 指标 | qwen3.7-max（生产 checkpoint） | deepseek-v4.1-flash（探针成功那次） |
  |---|---|---|
  | prompt_tokens | 12,630 | 10,749 |
  | completion_tokens | 6,939 | **15,128** |
  | reasoning_tokens | 5,316 | **11,552** |

  ⇒ "flash 更便宜"在本任务上**不成立**（完成 token 2.2×、总 token +32%）；它小提示快、长输入重。

### 19.3 结论（对"能不能换 flash"的正面回答）

1. **不能直接换**，但理由不是"名字小"：置换后 6/6 硬失败，且失败率不是 100%（有 1 次成功）⇒ 这是**配置/预算不匹配**，不是模型能力判决。
2. **要谈换档必须先做两件事**：①给 flash 单独配置 `max_tokens/timeout`（按 16k/300s 起）②在该配置下重跑本节 19.2 的 N≥10 对拍。
   在此之前任何"flash 更省/更强"的说法都无据。
3. **客观能力上 flash 不劣**：4/5 打平，且在"不编数"这一点上优于 max（max 该题编了个 0）。
   ⇒ 与 §17.5/§十八 同一条纪律：**能力结论必须来自同题多次测量，不能来自模型名。**
4. 成本口径仍缺：产物无 `cost` 字段，本节只能给 token 与墙钟，**金额不可自证**。

---

## 二十、flash 预算对齐后的 10 次实测（第一 Agent，2026-11-06 12:2x CST）——并就地更正 §19.2 的机制

> 用户批准「按 16k/300s 给 flash 单独配好，再跑 N=10」。探针加 `--force-max-tokens 16000 --force-timeout 300`
> （仅本进程覆盖，不改 config、不改生产）。产物：`data/shadow/event_intelligence/model_swap_flash_budget16k_v1/`。

### 20.1 结果（US.AMD，无事件简报，温 0，预算 16000 / 300s，n=10）

| 运行 | 方向 | score | status | finish_reason | completion | reasoning | 耗时 |
|---|---|---|---:|---|---|---:|---:|---:|
| 1 | SKIP | 3.0 | VALID | stop | 14,955 | 10,538 | 154.1s |
| 2 | SELL_SHORT | 6.0 | VALID | **length** | **16,000** | 13,197 | 179.0s |
| 3 | SKIP | 5.0 | DEGRADED | stop | 15,455 | 11,991 | 107.1s |
| 4 | SKIP | 5.0 | DEGRADED | **length** | **16,000** | 15,636 | 106.5s |
| 5 | SKIP | 5.0 | DEGRADED | **length** | **16,000** | 14,591 | 105.8s |
| 6 | SKIP | 5.0 | DEGRADED | **length** | **16,000** | 13,321 | 111.1s |
| 7 | SELL_SHORT | 6.0 | VALID | **length** | **16,000** | 12,999 | 162.4s |
| 8 | SELL_SHORT | 6.0 | VALID | stop | 15,265 | 11,552 | 159.3s |
| 9 | SKIP | 5.0 | DEGRADED | stop | 15,013 | 11,255 | 99.9s |
| 10 | SKIP | 5.0 | DEGRADED | **length** | **16,000** | 14,665 | 104.0s |

汇总：`direction={SKIP:7, SELL_SHORT:3}`、`status={VALID:4, DEGRADED:6}`、`score={3.0,5.0,6.0}`、时延中位 109.1s。
（注：§19 的两行"成功/失败"标注以本表 `finish_reason` 为准；上表 status 由卡片契约判，`length` 截断后 JSON 解析失败即回落 DEGRADED。）

### 20.2 机制结论（更正 §19.2）

- **6 次失败全部是 `finish_reason=length` 且 `completion_tokens` 精确等于 16000**；
  `_error` 是 JSON 截断（`Unterminated string starting at: line N`）。
- **4 次成功全部 `stop`，completion 14,955–15,455**，即**贴着上限**完成。
- ⇒ 真正的闸门是 **推理 token 吃穿输出预算**：flash 在这个 prompt 上 reasoning 恒在 **10.5k–15.6k**，
  加上 JSON 正文 ~1.5k，**16k 只是"刚好够、六成概率不够"**。要稳定需 ≥24k（推理 15.6k + 正文 1.5k + 余量），
  且要先确认该模型端点的输出上限是否允许。
- 由此**撤回 §19.2 的"60s 超时"归因**：`~110s` 只是"截断后快速失败"的耗时，与 timeout 无关。
  这也解释了 §19.2 那 6 次 `_error="LLM synthesis failed"` 的真实身份 —— **同一个截断**，不是网络问题。

### 20.3 与 max 的对比（同输入、同 12/10 次口径）

| 指标 | qwen3.7-max（生产预算，n=12） | deepseek-v4.1-flash（16k/300s，n=10） |
|---|---|---|
| VALID 率 | 11/12 = 92% | **4/10 = 40%** |
| 方向分布 | SELL_SHORT 11 / SKIP 1 | SKIP 7 / SELL_SHORT 3（其中 6 个 SKIP 是截断回落） |
| 仅 VALID 子集 | SELL_SHORT 11 / SKIP 1 | SELL_SHORT 3 / SKIP 1 |
| 主调用 reasoning | 3.2k–5.3k | **10.5k–15.6k（≈2.5–3×）** |
| 主调用 completion | 4.9k–6.9k | 14.9k–16.0k（**≈2.4×，且常触顶**） |
| 时延中位 | 161.8s | 109.1s（失败快、成功 155–180s） |

### 20.4 裁决建议

1. **不作为 DA 主模型替换**：同输入下有效卡产出率 40% vs 92%，且失败是**静默回落 SKIP**（生产语义 = 不开仓），
   这比"方向错"更危险——错误会伪装成"策略保守"。
2. 若仍要用 flash（例如用于更短的下游任务），前置条件：①输出预算 ≥24k 并实测端点上限 ②有效卡产出率 ≥90%
   ③在 VALID 子集上与 max 做 N≥10 方向对拍。三条未过不得上线。
3. **能力层面的公允结论不变**：flash 在客观小项上与 max 打平（4/5）、且不编数；
   它的短板是**长输入下的推理预算效率（2.5–3× reasoning）**，不是"笨"。这正是"不能只看名字"的反面同样成立 ——
   也不能只看"打平"就换档。
---

## 二十一、11-06 全链路镜像数据点（Hermes，2026-11-07 只读追加）

> 性质：**纯追加观测，不改本 DEB 任何结论**。为 §五「regime 轴无判别力」与 §零「DA 正收益非来自 regime 判断」补一个**镜像日**实证。
> 口径：`data/ftseek_us.db`（`playbook_instructions` row_kind='instruction'；`trade_records` trade_date='2026-11-06'）、`data/da_checkpoint/2026-11-06/`、富途日K。
> 纪律：本条所有数字一条命令可复现；不据此新增任何门/阈值/行为变更；历史行不追改。

**11-06 DA 运行**：08:00:23 ET 起，08:35 完成；candidates_total=93，analyses=36；instruction 行 20 = BUY 11 / SELL_SHORT 5 / SKIP 4（net +6 多）。

**生成时市场快照（20/20 行一致）**：`market_regime=risk_off · spy_trend=below_ma20 · VIX=18.04`（沿用 11-05 收盘，DA 08:00 生成时尚未开盘）。

**模型归因（再次印证 E14 / §一#2 / §十四 / C4）**：36 个 checkpoint 的 `_llm_output._model_reported` = **qwen3.7-max 36/36**；`playbook_instructions.model_used` 同值 ⇒ **归因列与 API 回显一致，无失真**。`llm_audit.feature='agent_bridge'=deepseek-v4-pro ×14（21:32–21:36 CST = 08:32–08:36 ET，finish/finalize 链）` 是**另一条链**，不是逐股分析模型。→ 正式撤回任何「model_used=qwen3.7-max 是假的／DA 实为 deepseek-v4-pro」的说法（该错误与 §一#2 记录的同型误判一致）。

**镜像结果（vs 11-05）**：口径见下注（2026-11-07 复核后补，原表两行不同基数）。

| 日 | regime | DA 净方向 | 当日 QQQ | DA(playbook_sim) 净 |
|---|---|---|---|---|
| 11-05 | risk_off | net 多（BUY 9 / SHORT 3） | −6.5% | 当日 −115.92（**exit 日口径**，全时段 7 笔）主体是**隔夜 SHORT(ALB −108.28)**，非长仓（§一#3） |
| **11-06** | risk_off | **net +6 多**（BUY 11 / SHORT 5） | **+1.4%** | 常规段 **+$53.55**（**entry 日口径**，12 笔平仓：毛 103.47／费 49.92 = **费占毛利 48.2%**，11 胜 1 负，多为 BE 微利，TSLA 空反弹 SL_INITIAL −58.37 净）｜**同基数（exit 日全时段）14 笔 = 毛 167.77／费 58.35／净 +109.42、13 胜 1 负** |

> **口径更正（2026-11-07）**：原表把 11-05 的 exit 日全时段净额（−115.92）与 11-06 的 entry 日常规段净额（+53.55）
> 并排 → 不可直接相减/相比。同一基数下 11-06 = **+109.42**。金额本身复算无误（12 笔常规段逐笔求和 = 103.47/49.92/53.55），
> 原表笔数「13 笔 12 胜 1 负」两个口径都不符：常规段 **12 笔 11 胜 1 负**，全部平仓 **14 笔 13 胜 1 负**。

**含义（不改结论，只补证据）**：同一个 `risk_off` 标签覆盖了 −6.5% 续跌日与 +1.4% 反弹日两种相反市况，DA 两日都 net 多。⇒ `regime` 字段**不构成方向信号**，与 §五「轴 A/B 无判别力、不建门」一致；DA 的 +$53.55 是方向运气对齐（反弹日做多赚），非 regime 推理之功，与 §零「三项修复不承诺胜率/净额改善」一致。同日 EXT 通道 5 笔建仓中 **2 笔已平 +$55.87**（NET 盘前 +44.78 / NVDA 盘后 +11.09），另 3 笔仍在仓；DA 费用吃掉近半毛利 —— 与既有「毛利被费用侵蚀、edge 需过费用线」结论同向。

**新前瞻风险登记（非本 DEB 结论，仅记录）**：11-06 收盘仍持 **5** 只隔夜多单（GOOG / PLTR / MRVL / AVGO，均 LONG；PLTR/MRVL/AVGO 于 16:27–16:42 ET 尾盘/盘后建仓；**另含 11-05 08:42 ET 盘前建仓的 MCD，已持两夜**，2026-11-07 复核 `open_positions` 实测 5 行）→ 与 §八 F7（隔夜缺口单独立轴）相关，不属本 DEB 处置范围。注：MCD 的 `metadata_json.admission_verdict=REJECT(NOTIONAL_TOO_SMALL 2254<5500)` 却已成交 —— 与 §零② 同族（该通道不消费 admission），登记待裁。

---

## 二十二、2026-11-07 复核结论与落地记录（Hermes）

> 触发链：用户令「按建议核查」→「按最合理方案执行」。本节记录**独立复跑的读数**与**实际落地了什么**；
> 不改 §五–§八 任何裁决方向，不新增任何交易阈值/门。
> 复核口径：`data/ftseek_us.db` / `data/llm_audit.db` / `data/event_stream.db` / `data/da_checkpoint/<日>/` /
> `data/shadow/event_intelligence/*` / 富途日K；回放脚本原样重跑。

### 22.1 复核表（关键读数，含被推翻项）

| 项 | 原声称 | 独立实测 | 判定 |
|---|---|---|---|
| `model_used` 归因 | 11-05 失真（实为 deepseek-v4-pro ×7） | 11-05/11-06 各 36 checkpoint 内层 `_model_reported`=qwen3.7-max **36/36**，DB 同值；`agent_bridge` 14 条 = 21:32–21:36 CST 另一条链 | ✅ 原声称撤回（无 P0） |
| 轴 A | 71 日 / 789 笔 LONG 已关闭 | DEB-20260924 §三.6 逐字一致（引用既有裁决，本轮未重跑 5m 回放） | ✅ |
| 轴 B | 601 笔 / 88 日，CI95 含 0 | `scripts/intraday_shock_replay.py --since 2026-07-02 --until 2026-11-05` 重跑：`LONG n=601 dates=88`；−0.5% 组 CI95[−36.68,+14.94]；−1.0% [−165.29,+1.06]；−1.5% n=2/1 日 | ✅ 逐位复现 |
| F5 换档 | flash 40% vs max 92% | 产物实读：VALID 4/10；6 次 `finish_reason=length` 且 completion 恒 **16000**；reasoning 10,538–15,636 vs max 生产 2,235–6,595 | ✅ |
| 11-06 运行 | 93 候选 / 36 分析 / 08:35 完成 / VIX 18.04 | 日志 93 / 36-36；`generated_at=2026-11-06T08:35:12-05:00`；raw 36/36 `vix.current=18.04` | ✅ |
| QQQ | 11-05 −6.5% / 11-06 +1.4% | 富途日K `change_rate` −6.4889% / +1.3987% | ✅ |
| 11-05 净额 | −115.92，主体 ALB −108.28 | exit 日聚合 7 笔 = −115.92；ALB（11-04 16:42 建仓）−108.28 | ✅ |
| 11-06 净额 | +53.55（毛 103.47／费 49.92） | 常规段 12 笔逐笔求和 = 103.47 / 49.92 / 53.55（费占毛利 48.2%） | ✅ |
| 11-06 笔数 | 13 笔 12 胜 1 负 | 常规段 **12 笔 11 胜 1 负**；全部平仓 **14 笔 13 胜 1 负** | ❌ 已就地更正（§二十一） |
| 隔夜仓 | 4 只（GOOG/PLTR/MRVL/AVGO） | `open_positions` **5** 只（+MCD，11-05 盘前，已持两夜） | ❌ 已就地更正（§二十一） |
| 隔夜缺口量级 | 10-30 两笔 −702.35 | LMT −438.00 + MRK −264.35 | ✅ |
| F4 前科 | lawsuit/fraud 曾误触发 RISK_OFF | `app/us_pipeline/emergency_interceptor.py:583-596` 注释逐字在 | ✅ |

### 22.2 为什么不按「🔴 批准 F1（D1 直接落地）」执行

三条都是实测，不是口径之争：

1. **因果链不成立**：`Chip selloff…` 的 `first_seen_at = 2026-11-05T18:49Z`（13:49 ET），DA 批次 13:35Z ⇒
   那一刻库里**没有这条**。当日 10 条市场级风险新闻中，as-of 08:00 ET 前可见**仅 1 条**（"Asia fuel oil
   benchmarks slump…"，油品/地缘，不含美股崩盘语义）。D1 按现设计会在 brief 里印"近 12h 无市场级风险事件"
   （§6.2 规定显式零值优于缺行）⇒ **把假阴性写实**，比缺行更糟。
2. **文档自己已改判**：§16.4 明文「F1｜**批准 R0**，再按 R1/R2 离线 A/B 决定是否进生产；**不批准**原 D1 以
   '零行为变更' 名义直接上线」；§14.4 第 6 条指出改变模型所见即**信号源行为变更**；§18.3 把"同输入重复
   N≥10 的分布"写成 **F1 硬前置**，而 §17.5/§十八 已实测温度 0 下 6 次里 1 次翻向（≈17%）。
3. **词表有可举证的误报**：按 §6.2 原 8 词表，11-05 批次时点命中 3 条**全是美国中期选举政治新闻**
   （"Republicans **routed**…"、"Trump's midterm **rout**"、"election **rout**"），真崩盘 0 条。
   修正为「市场范围词 ∧ 危机词共现」后：11-05 = 10 条命中、as-of 前可见 1；11-06 = 3 条命中、可见 2
   （含 `Markets fret over growth and inflation, just as tech rout gains speed`，06:19 ET 已入库）。
   ⇒ 词表本身需要裁决，且 11-06 说明"信息在不在"跟日子有关，不能用一个单日事故推一条通用门。

### 22.3 为什么不落地 D2（F3 record-only），以及复核中发现的口径问题

先纠正我自己在建议里的一处错误前提：我曾用 `trade_records.entry_vix` 全库最大 18.66 论证「同源 VIX 门恒不触发」。
`entry_vix` 与执行门读的确实是**同一条代理腿**（`VIXY×0.85`，`app/data/vix_source.py::vixy_to_vix`；
`VIXGuard.BLOCK_THRESHOLD=28` 就作用在这个代理值上），所以"这两天的代理取值离 28 很远"成立
（11-05 = 13.91、11-06 = 18.04）⇒ record-only 在崩盘日不会给出任何 `would_block=true`。

但同一批实测暴露了更值得处理的事：**同一天的"VIX"有两个口径，且跨过阈值**。

| 时刻 | 读取方 | 值 | 来源 |
|---|---|---|---|
| 11-06 08:23 ET（盘前） | DA brief / `VIXGuard`（premarket 走代理窗口） | **18.04** | `VIXY 21.22 × 0.85`（`US.AMD.raw.json:market_context.vix`） |
| 11-06 23:59 ET（盘后） | `data/vix_cache/vix_data.json` → 看板 / `day_score` / `health_check` | **28.18** | FRED `VIXCLS`（11-05 收盘，FRED 日频 T-1） |

两者差 10.14 点，**正好横跨硬拒阈值 28**：模型和门看见"平静 18"，看板看见"恐慌 28"。
这与 DEB-20260914 §八记录的口径分裂同型（当时是 14.71 vs 17.84），并未真正收敛 —— 只是值变了。

⇒ 结论修正为：门不是不存在，而是**读代理**；而代理与现货的关系已被证伪为"常数换算"——
同收盘时刻 `spot/(0.85×VIXY)` 近一年 n=251 中位 0.88、p10–p90 = 0.68–1.13、极值 0.61/1.56，
且逐月中位从 0.65 漂到 1.35（详见 §二十三 与 RFC-190）。崩盘日（11-05）比值 1.56 为年内极值。
盘前与现货**同刻**对照不可能（VIX 现货盘前不撮合），但已实测盘前这条腿读到的是**上一交易日收盘**
（22/22 快照逐字等于前一日 VIXY 收盘）⇒ 盘前既无新鲜度、系数又漂，两个问题叠加。

（另：`VIXGuard` 是 28/25 迟滞的**全局**门，`playbook_vix_gate_enabled: true` 已在 playbook 路径生效；
所以 F3 真正的问题不是"要不要给 EXT 通道加门"，而是"这条全局门用的 VIX 口径对不对"。这个问题覆盖所有通道，
优先级高于 F3 本身。三选项与预注册判据见 RFC-190。）

### 22.4 本轮实际落地（RFC-189，零交易行为变更）

| 文件 | 性质 |
|---|---|
| `app/event_stream/latency_report.py`（新，约 180 行） | 只读：按 ET 日聚合发布→入库 P50/P90/超 60min 占比 + as-of 可见性 + 劣化判据（纯函数，可测） |
| `scripts/data_health_check.py` | 新增 `Event Ingest` 检查项（该脚本由盘前综合预检 Step 2 每日执行 ⇒ 每日可见，**不新建 cron**） |
| `tests/test_event_ingest_latency_observability.py`（新，4 例） | 守：检测器分离政治 rout 与市场 selloff / ET 日归属与 as-of 切分 / 劣化双侧条件与小样本豁免 / 头条用上一完整 ET 日 |

- **实跑读数（2026-11-07，生产库只读）**：
  `✅ 事件采集 | 2026-11-06 n=848 | P50=19min P90=487min >60min=252/848 | 市场级风险 3 条: as-of 08:00 ET 前可见 2 / 晚到 1 | 前日 2026-11-05 P90=363min`
- **变异验证**：把检测器退回"只看危机词"，`test_market_risk_detector_separates_election_from_selloff` 立刻红；
  恢复后 4 例全绿（文件 sha256 复原校验一致）。
- **回滚**：`git revert` 单提交即可；无配置键、无 cron、无台账行、无交易路径引用。
- **它不回答什么**：不给"该不该加门"的答案，也不改变 DA/UE 的输入；D1/R0 的 A/B 仍需另批。

### 22.5 仍待裁

1. **VIX 口径（新发现，优先级最高）**：代理（`VIXY×0.85`，门与 DA brief 读）vs 现货（FRED，`vix_cache`/看板读）
   同日 18.04 vs 28.18、横跨阈值 28。先做同刻对照定权威口径，再谈阈值或 D2；在此之前不改任何 VIX 代码。
2. **D1（把事实行喂进 brief）**：三项前置（词表共现化 + as-of 零值语义写"截止 08:35 已入库 0 条" + N≥10 噪声基线）
   未完成前不进生产；§18.3 的基线建议先跑。
3. **F3 / D2**：改判为"等 VIX 口径定谳后再议"——现在加门等于在一个未定口径的值上加阈值。
4. **EXT 通道不消费 admission**：41 笔带判决成交里 10 笔 REJECT 仍成交（含在仓 MCD）—— 是否按缺陷修，需裁决。
5. **F7 隔夜缺口轴**：另开 DEB，样本须含 5 只（含 MCD 持两夜）。

---

## 二十三、VIX 口径：新鲜度、系数漂移与硬门代价（Hermes，2026-11-07）

> 复跑：`venv/bin/python scripts/vix_caliber_audit.py`（只读；`--json` 出机器读数）。
> 性质：**只登记读数与三选项，不含任何行为变更**；决策与预注册判据在 `RFC-190`。

### 23.1 盘前这条"实时代理"没有当日新鲜度

`fetch_vixy_via_opend` 读富途快照 `last_price`；`last_price` 在盘前停在**上一时段收盘**（盘前真实价在 `pre_price`）。
实测 `data/da_checkpoint/*/US.*.raw.json`（按日聚合当日**全部** raw 文件）：2026-10-07～11-06 的
**23/23 个交易日**，盘前 `vixy_price` 的取值集合全部 ⊆ {前一交易日 VIXY 收盘}。
⇒ 08:00 ET 的 VIX 与 FRED T-1 收盘**同时点**，"盘前能捕捉 VIX 跳升"不成立。
（只取当日第一个标的的 raw 会误报缺字段：11-02 单文件为 None，按日聚合后 23/23 全等。）

### 23.2 0.85 不是换算系数，是会漂的经验式

同一收盘时刻 `VIX_spot / (0.85 × VIXY_close)`，近一年 n=251：中位 **0.88**、p10–p90 **0.68–1.13**、
极值 **0.61 / 1.56**（11-05 为年内极值）。逐月中位：

| 月 | 25-12 | 26-01 | 26-03 | 26-05 | 26-07 | 26-08 | 26-09 | 26-10 | 26-11 |
|---|---|---|---|---|---|---|---|---|---|
| spot/(0.85×VIXY) | 0.65 | 0.73 | 0.91 | 0.78 | 0.95 | 0.95 | 1.08 | 1.18 | 1.35 |

⇒ 一年前代理高估约 50%，现在低估约 26%，崩盘日低估 56%。**同一个常数在不同月份含义相反**。

### 23.3 换口径的代价：会拦掉窗口内最赚钱的一天

A10（2026-07-01 起，90 个有成交的交易日 / 791 笔 / 净 **−1,867.33**）：

| 判据 | 命中 | 笔数 | 净额 |
|---|---|---|---|
| 代理 > 28（现役） | 0 天 | — | — |
| 现货 T-1 收盘 > 28 | 1 天（**2026-11-06**，前收 28.18） | 18 | **+109.42** |

⇒ 把门"修正"到现货只会拦掉最好的一天；而 11-05 那种日内 16.61→28.18 的跳升，任何盘前口径都看不见
（能看见它的日内价格轴已被 §五 轴 B 证伪）。**VIX 阈值型拦截在本系统上是负 edge**，与轴 A 同向。

### 23.4 结论（待裁，见 RFC-190）

- **A（推荐先做）**：纯标注口径（「VIX 代理(VIXY×0.85, T-1)」vs「VIX 现货(T-1)」），零行为变更；
- **B**：把门判据换成现货 —— 实测代价 = 拦掉 11-06（+109.42/18 笔），**不建议**；
- **C**：盘前改读 `pre_price` + 滚动系数，record-only 影子先验（判据 3 项写死在 RFC-190 §3）；
- A2（把 T-1 现货也喂给模型）= 输入变更，须按 §18.3 先出 N≥10 噪声基线。



## 二十四、环境 vs 执行：真值口径归因（2026-11-07，A10 窗口 781 笔已平仓）

上游：`data/market_env_daily.jsonl`（CBOE 官方 VIX/VIX9D/VIX3M + 自算 rv20，见 RFC-190 §7），
复跑 `scripts/market_env_ledger.py report`。**本节所有环境变量都用官方真值，不用 VIXY 代理。**

### 24.1 输入真值体检（先证"输入是否可信"）

| 项 | 读数 |
|---|---|
| 代理 vs 真值(前收) | 23 天：中位 **−16.7%**、平均 −17.3%、`\|差\|≥20%` **7 天** |
| 最差 5 天 | 11-06 **−36%**、11-04 −26%、11-03 −26%、10-19 −25%、10-26 −22% |

⇒ 代理不是"略偏"，而是**系统性低估**且近两个月加剧；任何基于代理值的环境结论都不可用。

### 24.2 总量：费用已是毛利 1.76 倍

| 口径 | 金额 |
|---|---|
| 毛（realized_pnl 合计） | **+2,439.54** |
| 费 | **4,306.81（占毛 176.5%）** |
| 净 | **−1,867.33** |
| 已平仓 / 未平仓 | 781 / 10 |

### 24.3 执行面：亏损集中在一个止损 + 一个隔夜机制

亏损侧（净额升序前四）：

| exit_reason | n | 毛 | 费 | 净 | 均笔 |
|---|---|---|---|---|---|
| `SL_INITIAL` | 42 | −5,840.06 | 199.44 | **−6,039.50** | −143.80 |
| `OVERNIGHT_GAP_STOP` | 14 | −4,463.20 | 67.37 | **−4,530.57** | **−323.61** |
| `MFE_STALL_EXIT` | 31 | −1,223.08 | 129.56 | −1,352.64 | −43.63 |
| `TIME_EXIT` | 36 | −1,042.71 | 152.43 | −1,195.16 | −33.20 |

盈利侧（净额降序前四）+ 费用机理：

| exit_reason | n | 毛 | 费 | 净 | 费/毛 |
|---|---|---|---|---|---|
| `SL_TRAIL_LOCK` | 199 | +9,826.36 | 974.41 | **+8,851.95** | 9.9% |
| `PRE_CLOSE_EXIT` | 54 | +1,853.09 | 255.03 | +1,598.05 | 13.8% |
| `SL_BREAKEVEN` | **304** | **+3,159.00** | **1,992.03** | **+1,166.96** | **63.1%** |
| `TAKE_PROFIT` | 2 | +695.50 | 8.57 | +686.92 | 1.2% |

**这就是"费 176.5% 毛利"的机理**：304 笔保本离场毛利 +3,159，被费吃掉 1,992（63%），
剩 +1,167 —— 即 **每 4 笔保本单里有近 3 笔的毛利等于白干**。真正的印钞桶是 `SL_TRAIL_LOCK`（199 笔 +8,852，费只占 9.9%）。

`OVERNIGHT_GAP_STOP` 均笔 −323.61，是 `SL_INITIAL`（−143.80）的 **2.25 倍**：隔夜的单笔伤害远超日内在场。

### 24.4 环境面：四个轴全部无单调性

| 轴 | 分桶（日数 / 净额） |
|---|---|
| QQQ 当日 | ≤−2%：5 / **−382.67**；−2~−1%：15 / −779.04；−1~0%：25 / −1,020.84；0~1%：25 / **+56.53**；>1%：18 / −174.88 |
| VIX 真值分位(252d) | 0–20%：21 / **−2,257.31**；20–40%：16 / **+1,144.88**；40–60%：10 / −325.32；60–80%：15 / +834.99；80–100%：28 / −1,264.57 |
| 期限结构 slope | <−10%：80 / −2,001.84；−10~0%：6 / −160.83；0~10%：2 / **+213.99**；>10%（倒挂）：2 / **+81.35** |
| 系统 regime 标签 | risk_on 360 / **−1,834.70**；cautious 304 / +15.02；RANGE 37 / +174.09；risk_off 18 / −135.72；UNKNOWN 62 / −86.02 |

（QQQ 轴有 2 天账本无当日收益——非交易日口径/数据缺口，已单独计为 `(missing)`，不计入任何桶。）

三处反直觉但一致的读数：**最低 VIX 分位最亏（−2,257）**、**倒挂日反而赚**、**亏损主体在 risk_on 桶**。
与轴 A（最普跌桶毛利最高）、§五（两日同 regime 相反市况）同向：**波动/恐慌/regime 都不是方向信号**。

### 24.5 结论（本轮唯一硬结论）

1. **环境不是解释变量**，费用与两个尾部机制才是 —— 781 笔已平仓里 4 个 exit_reason 就吃掉净额的 **−13,117.87**，而毛额只有 +2,439；
2. **不要再在 VIX/regime/崩盘日上建门**：这次用的是官方真值，比此前用代理得出的同结论更硬（轴 A/B 关闭 → 本节补证）；
3. 研究预算应转向：**费用结构**（费/毛 176.5%，隔夜与盘前刷单是主源）+ **`OVERNIGHT_GAP_STOP` 的触发质量**（均笔 −323.6）；
4. **功效边界（防误读）**：n=90 个交易日 / 日净额 sd **262.52** ⇒ 45/45 分组的最小可测差异 ≈ **$110/日**。
   上表分桶差多数落在噪声内，**不得**据单桶（尤其倒挂 n=2）反推规则；本节只用它做"没有单调性"的定性判断。

## 二十五、源级经济学：费用杠杆的可达性复核（2026-11-07）

用户批「从费用结构 / 隔夜缺口里挑一个开工」。开工第一步是**查这两轴是否已被裁过** —— 两轴结果都在已关闭账本内
（§25.4）。故本轮不重开，改做三件更基础的事：先排除单笔劫持（§25.1），再做源级分解（§25.2–25.3），
最后算出唯一未被封的那条算术（§25.5–25.6）。

### 25.1 费用结论的稳健性（排除"少数单劫持符号"）

| 剔除阈值 | n | 毛 | 费 | 净 | 费/毛 |
|---|---|---|---|---|---|
| 全部 | 781 | +2,439.55 | 4,306.81 | **−1,867.33** | **176.5%** |
| `fee_bps ≤ 400` | 779 | +2,302.62 | 3,851.54 | −1,548.98 | 167.3% |
| `fee_bps ≤ 200` | 778 | +2,383.09 | 3,750.68 | −1,367.66 | 157.4% |
| `fee_bps ≤ 100` | 776 | +2,389.99 | 3,742.66 | −1,352.73 | 156.6% |
| `fee_bps ≤ 50` | 773 | +2,327.11 | 3,691.75 | −1,364.70 | 158.6% |

⇒ 与 2026-10-10 那批（2 笔仙股使净额符号翻转）不同：**本窗口符号不随阈值变化**，费/毛 恒在 156–177%。
CTNT 915bps（费 451.26）+ GIPR 204bps（100.86）合计只占全样本费 **12.8%**，不足以劫持结论。

### 25.2 按供给源（`decision_id` 前缀）

| 源 | n | 毛 bps | 费 bps | 净 bps | WR | 墓地率(MFE<0.3%) | 净额 |
|---|---|---|---|---|---|---|---|
| **DA** | 578 | **+7.3** | 9.6 | −2.3 | 67.0% | 21.3% | **−654.68** |
| `universe_expansion` | 91 | **−13.2** | 12.9 | −26.0 | 57.1% | 28.6% | **−1,105.49** |
| `intraday_reentry` | 26 | −16.6 | 7.7 | −24.4 | 42.3% | 38.5% | −349.73 |
| EXT-* 各组 | 1–7/组 | 多为正 | ~9 | — | — | — | 合计约 +$500（n 过小不裁） |

**DA 是唯一毛为正的源，且只差 2.3bps 就跨过费用线**；`universe_expansion` 与 `reentry` 是**毛为负**
（不是费用问题，是信号本身负期望）。UE 这行与 `C18` 恒真红线登记的 91 笔 Σ净 −$1,105.5 完全一致（口径：bps 按名义加权，
C18 的 −33.3bps 为逐笔均值口径，两者不可互换）。

### 25.3 按进场时段

| 时段 | n | 毛 bps | 费 bps | 净 bps | 净额 |
|---|---|---|---|---|---|
| `regular` | 696 | **3.0** | 10.0 | **−7.0** | **−2,373.72** |
| `premarket` | 27 | 27.0 | **53.0** | −26.0 | −326.17 |
| `afterhours` | 10 | 88.3 | 9.4 | +78.8 | +361.44（n=10，仅登记） |

盘前 27 笔费用率 **53bps** —— 小额 + 地板费，毛再好也被啃掉；但 n=27 且已有 EXT 相关裁决在先，
**本轮只登记不可执行结论**。

### 25.4 两轴已在关闭账本内（故本轮不动）

| 轴 | 已裁结论 | 出处 |
|---|---|---|
| 费用结构 | 地板占 83%、富途**港实体**地板 $1.99/腿不可省、$0 佣金是美国实体属性、随机砍单反事实毛利费同比例缩放**不翻正**、`min_notional` 已 enforce | `exit-parameter-validation/references/fee-structure-exhaustion-20260825.md` |
| 隔夜/缺口 | 跨日拆两腿实测 **日内腿 −$5,396 / 隔夜腿 +$2,137 ⇒ 隔夜是减损机制不是亏损源**；禁隔夜、隔夜减半、深水闸（B19 **GATE_NOT_WORTH** 2026-10-14 终裁）、盘前止损、开盘延后网格全负 | 同上 skill + `ADJUDICATED-CRITERIA` B15/B16/B19 |
| UE 打单腿 | C18 **恒真红线**（`universe_expansion.entry_enabled=false`），复活需 scan 窗 n≥20 ∧ 命中率≥50% ∧ 前瞻>0 | `ADJUDICATED-CRITERIA` C18 / RFC-182 |

⇒ 「治理费用结构」「提升 OvernightGapStop 质量」两项**都属重开已关闭的路**，本轮不做。

### 25.5 唯一未被封的算术：DA 的名字帽 vs 费用线

费用地板模型（skill 实测口径）：`fee_bps = 39,800 / N + 0.42`。

| 名义额 N | 费用率 | 对 DA（毛 7.3bps）的净额 |
|---|---|---|
| $3,000 | 13.7bps | **−6.4bps** |
| $4,000 | 10.4bps | −3.1bps |
| $5,595（现中位） | 7.5bps | **−0.2bps**（打平线） |
| $5,951（现单名帽 6% × 富途净值 $99,179） | 7.1bps | +0.2bps |
| **$10,000** | **4.4bps** | **+2.9bps** |

（口径提醒：DA 组实测费 9.6bps 高于公式在 N=5,641 的 7.5bps —— 差额来自组内混着 28.7% 的 N<4,000 小额笔，
上表是"单笔都在该名义"的纯算术，不是组均值复现。）

⇒ DA 现在正好卡在打平线上。要留 3bps 余量，N 需 ≈$10k ⇒ 单名帽 6% → **10.6%**
（单笔风险 $282 → $500，+77%；heat 4% 可容并发 13.6 → 8 笔）。**这是风险胃口决策，不是参数优化**，
须用户签字 + 预注册三档停止条件（见 RFC-191）。

### 25.6 反直觉结论（必须写死，防后人重踩）

把 224 笔小额单（N<4,000，毛 **−5.9bps**）的名义抬到 $4,500：费用率从 15.1 → 9.3bps（**bps 变好**），
但净额从 **−$1,368 → −$1,533（Δ −$165，变差）** —— 负毛收益被更大的名义放大。
⇒ **「抬名义过费用线」只对有正毛的源成立**；不要用费用线去救负毛人群（UE/reentry/小额笔），
那是把亏损按倍数放大。

### 25.7 本轮结论

1. 无可安全落地的费用/隔夜动作（两轴已关）；
2. 唯一开放项是「DA 单名帽 vs 费用线」，本质是风险额度决策（RFC-191，待裁）；
3. 若不抬帽，剩余杠杆只有 DA 的入场质量（毛 7.3bps 是天花板）与外部路径（换券商实体/换信号源）；
4. 全域净 −1,867.33 / 毛 6.4bps / 费 11.4bps 的结论在敏感性检验下稳健。


### 25.8 核查 RFC-191 后的修正（同日自查，原推荐已撤回）

对 §25.5 的"抬帽过费用线"做了三项核查，**结论反转**（详见 `RFC-191 §8`）：

| 核查 | 实测 | 判定 |
|---|---|---|
| 帽子绑定率 | 仅 **26.9%** 的笔顶到 6% 帽；43.1% 低于 90% 帽（regime 乘数打折） | §25.5 表"每笔都到帽"是高估 |
| 基线复现 | 模型重建规模重放净 −1,137.65 vs 实盘 −1,867.33，误差 **39%**（>20% 门） | 幅度不可用，只留方向 |
| 亏损主体 | **≥$80 组 653 笔（83.6%）净 −2,267，毛仅 +1.5bps**；qty 中位 **16 股** ⇒ 恒付地板 | 抬帽救不到主体 |

**逃地板算术**：需 qty>199 ⇒ 名义 $16,000（px=80，**16.1% 权益**）/ $30,000（px=150）/ $80,000（px=400）。
8% 帽在 px=80 只能买 99 股，仍在地板上。且日名义帽 0.8（$79,343）已有 20/90 天触及，名义 ×1.33 后
**35/90 天触顶** ⇒ 用笔数换名义。

**价格带真相（费用叙事的分层版）**：

| 价格带 | n | 费 bps | 毛 bps | 净额 |
|---|---|---|---|---|
| <$10 | 13 | 113.4 | +78.4 | −245.13 |
| $10–20 | 36 | 18.6 | +31.2 | +257.75 |
| $20–40 | 29 | 11.8 | +39.6 | +357.63 |
| $40–80 | 50 | 8.9 | +10.1 | +29.42 |
| ≥$80 | **653** | 8.8 | **+1.5** | **−2,267.00** |

⇒ 这不是"费用太高"，是"高价股这个主力人群毛 edge 只剩 1.5bps"。**费用侧动作到此为止**；
下一步若要做，命题应改成「毛 edge 是否随价格带单调下降」（选股宇宙问题），须另立预注册。

## 二十六、命题验证：价格带假说 **证伪** + 尾部结构（2026-11-07）

§25.8 提出「毛 edge 是否随价格带单调下降」并把它作为下一步命题。本节按预注册要素（价格带分层、n≥30、
双口径、集中度、剔尾稳健性）跑完，**结论：假说不成立**，同时露出真实结构。

### 26.1 三口径并列（缺一即误读）

| 价格带 | n | 均值 bps | 中位 bps | Winsor(p5/p95) 均值 | 费 bps | 净额 |
|---|---|---|---|---|---|---|
| <$20 | 49 | +40.5 | +36.1 | +37.7 | **42.9** | +12.62 |
| $20–40 | 29 | +43.0 | +35.3 | +34.7 | 11.8 | +357.63 |
| $40–80 | 50 | +8.9 | +17.6 | +5.1 | 8.9 | +29.42 |
| $80–150 | 154 | −3.9 | +21.6 | +3.5 | 9.4 | −445.10 |
| ≥$150 | 499 | +0.9 | +21.6 | +6.7 | 8.6 | −1,821.90 |

- **中位几乎不随价格变化**（35–36 → 17.6 → 21.6 → 21.6）；均值单调下降（40.5 → 0.9）；
- Winsor 后仍有 ~30bps 差（<$40 组 35–38bps vs ≥$40 组 3.5–6.7bps），但其 n 只有 78 且 <$20 组费用 42.9bps
  把毛利吃光（净 +12.62）⇒ 不构成"低价格更好"的可交易结论。

### 26.2 决定性检验：尾部构成 ≈ 人群构成（价格带无判别力）

最差 39 笔（5%）的价格带分布 vs 全体分布：

| 带 | 尾部占比 | 全体占比 |
|---|---|---|
| ≥$150 | **24/39 = 62%** | **499/781 = 64%** |
| $80–150 | 10/39 = 26% | 154/781 = 20% |
| $40–80 | 2/39 = 5% | 50/781 = 6% |
| <$40 | 3/39 = 8% | 78/781 = 10% |

⇒ 尾部就是按人群比例抽出来的，**价格带不挑尾部**。假说证伪。

### 26.3 真实结构：5 笔决定符号，且尾部是"入场即错"

| 剔除 | n | 毛 | 费 | 净 |
|---|---|---|---|---|
| 0 | 781 | +2,439.55 | 4,306.81 | **−1,867.33** |
| 最差 5 笔（0.6%） | 776 | +4,354.65 | 4,283.43 | **+71.16** |
| 最差 10 笔 | 771 | +5,913.81 | 4,255.81 | +1,657.94 |
| 最差 39 笔（5%） | 742 | +11,523.47 | 4,121.51 | +7,401.89 |

剔除 39 笔后，**每个价格带都回到 +23 ~ +68bps**（费用 9–12bps）⇒ **策略的"常规笔"全部有 edge**，
负期望完全由 5% 的尾部承担。

尾部画像（最差 39 笔）：

| 特征 | 尾部 | 其余 |
|---|---|---|
| `mfe_pct` 中位 | **0.07%** | 0.63% |
| mfe<0.3%（入场即错）占比 | **82%** | 23.3% |
| ATR% 中位 | 5.37 | 4.07 |
| 盘前 gap 中位 | 0.82% | 0.16% |
| confidence 中位 | 0.50 | 0.50（**无差异**） |
| 价格中位 | 180.9 | 215.9（无差异） |
| 方向 | LONG 74% | LONG 78%（无差异） |

最差 10 笔中 **7 笔出手是 `OVERNIGHT_GAP_STOP`**（LMT/ORCL/GAP/AMD/CRM/QCOM/RIVN，全 LONG）。

### 26.4 判定与不做的事

1. **不建价格带门**：尾部按人群比例分布，门只会随机砍掉同样的比例（含赢家）；
2. **不建 confidence/方向/时段门**：尾部与全体在三个事前特征上无差异；
3. **不重开隔夜轴**：尾部确实与隔夜跳空同源，但跨日拆腿实测隔夜腿 **+$2,137（减损机制）**，
   B19 已终裁 GATE_NOT_WORTH，重开 = 阈值购物；
4. **不把"剔尾后 +23~68bps"当可交易收益**：那是条件化在结果上的口径（选样谬误），只能用于
   "策略本体有 edge、亏损在尾部"这个定性判断。

### 26.5 本节唯一的可执行结论

**期望由 5% 的入场即错笔决定，而这批笔没有可利用的事前特征。** ⇒ 继续在入场/退出门上挖，
预期回报接近零（与 §26.3 的"常规笔全部为正"一起看：门想拦的正是不可识别的那批）。
能改变符号的只剩两条结构性路径：**提高信号质量（抬中位毛）** 或 **降费用地板（换券商实体）**。
本 DEB 至此关闭"用门/参数把净额翻正"这一整类提案。

## 二十七、DA 入场质量复盘：墓地不可事前识别，但已结构性减半（2026-11-07）

用户批「转信号质量：查 DA 那 578 笔里"入场即错"的决策链」。本节按决策链复盘走完，结论两条：
**① 没有任何事前输入能识别墓地；② 墓地率已从 28.5% 降到 14.8%，真实进步，但单笔代价翻倍。**

### 27.1 十二个事前特征全部无判别力（AUC 0.47–0.54）

样本：DA 578 笔，墓地（`mfe_pct<0.3%`）**123 笔 / 21.3%**，净 **−$8,917**；存活 455 笔 净 **+$8,262**。

| 特征 | 墓地中位 | 存活中位 | AUC |
|---|---|---|---|
| 盘前 gap% | 0.00 | 0.10 | 0.479 |
| 开盘后入场分钟 | 13 | 13 | 0.537 |
| RSI | 54.27 | 54.17 | 0.515 |
| ATR% | 4.13 | 4.07 | 0.487 |
| 量比 | 0.82 | 0.88 | 0.472 |
| VWAP 偏离% | −0.04 | −0.02 | 0.537 |
| signal_strength | 35 | 50 | 0.468 |
| confidence | 0.35 | 0.50 | 0.468 |
| 入场滑点 bps | 0.45 | 0.96 | 0.486 |

（AUC=0.5 即抛硬币。confidence 的 0.468 是唯一"反向"信号 —— 但 `B12` 已明令禁止 conf 接入任何新 live 门。）

### 27.2 墓地不是"某天/某票/某标签"现象

- 按日：59/85 个交易日都出现墓地，单日中位 1 笔，最多 9 笔（08-14，当日 9/11）；
- 按标的：137 个标的，最多单票 28 笔；最亏 SNDK −608 / CAT −456 / LMT −431；
- 按条件标签：`earnings_beat_streak` 21%、`pe_above_industry` 15%、`insider_net_sell` 21%、`news_negative` 21%、`below_ma20` 13% —— **全部在 13–21% 区间，无区分度**。

### 27.3 决定性证据：墓地是"方向错"，不是"出场杀"

`playbook_instructions.fwd_return_*`（入场后裸持有收益，未扣费、未走退出链）：

| 期限 | 墓地 中位 | 墓地 均值 | 墓地 正占比 | 存活 中位 |
|---|---|---|---|---|
| 当日（intraday） | **−0.8%** | −1.1% | **28.5%** | 0.0% |
| T+1 | −1.3% | −1.2% | 29.5% | −0.2% |
| T+3 | −1.4% | −1.7% | 33.3% | −0.3% |
| T+5 | **−2.0%** | −1.6% | 40.0% | −0.8% |

⇒ 墓地组**买入方向本身就是错的，且错了还在继续错**（持到 T+5 更差）；出场链不是凶手。
⇒ 存活组 fwd 也≈0/微负 ⇒ **DA 的 alpha 不在持有收益，全部来自退出链**（与既有"出场链值 $11.6k/4个月"一致）。

### 27.4 反直觉：追价不是病因

`anchor_dev_pct`（成交价相对 DA 计划价）：**墓地 −0.663% vs 存活 −0.908%** —— 墓地反而**更接近计划价**。
"买得更低更好"的方向性成立，但它对应的轴（限价回调路由）已于 **ADR-035 影子 KILLED（2026-09-21，C3 成交率 27.1%）**，
故**不重开**。

### 27.5 时代分层：A10 混算掩盖了真实改善

| 时段 | n | 墓地率 | 存活均笔 | 墓地均笔 | 净 | 期望/笔 |
|---|---|---|---|---|---|---|
| 7–8 月 | 274 | **28.5%** | +19.87 | −58.79 | **−691.49** | −2.52 |
| 9–11 月 | 304 | **14.8%** | +16.87 | **−96.25** | **+36.81** | **+0.12** |
| 全窗 | 578 | 21.3% | +18.16 | −72.50 | −654.68 | −1.13 |

⇒ 墓地率**腰斩**（28.5% → 14.8%）是真的；但单笔墓地代价**翻倍**（−58.79 → −96.25），
两者抵消后只到**打平（+$0.12/笔）**。

### 27.6 经济阈值（算术，不是承诺）

期望 = (1−p)×存活均笔 + p×墓地均笔。全窗口径下 **打平线 = 墓地率 20.0%**（现 21.3%，差约 7 笔）；
等价地，现墓地率下存活均笔需 **+8%（$18.16 → $19.60）**。

### 27.7 判定

1. **不建任何入场门**（12 特征 AUC≈0.5，且 confidence 门被 B12 禁止）；
2. **不重开** ADR-035（限价回调）、盘前跌幅否决、追价帽（均已在关闭账本）；
3. **可做且零风险**：报告层把 A10 拆成"7–8 月 / 9–11 月"两段呈现 —— 混算会让"已打到打平"的现状
   看起来仍是 −$655/578 笔，进而诱导继续调参；
4. **研究报告口径**：以后引用 DA 表现必须标时代段；全窗数字只作历史，不作现状。

## 二十八、⛔ 撤回 §27.5：「墓地率腰斩」是指标漂移，不是入场改善（2026-11-07）

§27.5 写「墓地率 28.5% → 14.8% 是真实进步」。用**与出场路径无关**的量复核后，**该结论作废**。

### 28.1 检验设计（为什么必须换口径）

`mfe_pct` 是**持仓期间峰值**，它由出场链决定：BE 类出场（`SL_BREAKEVEN`/`SL_TRAIL_LOCK` 等）
必须先走到 +0.4% 以上才武装 ⇒ **这些笔在定义上不可能 mfe<0.3%**。
所以出场构成一旦变化，"死单率"的分母就被改写。免疫口径只有裸持有收益 `fwd_return_*`
（与出场方式无关）。

### 28.2 结果：入场质量没有改善，反而变差

| 人群 | 7–8 月 墓地/n | 9–11 月 墓地/n |
|---|---|---|
| 全部 | 78/274 (**28.5%**) | 45/304 (**14.8%**) |
| **BE/锁利类出场** | **0/151（0%）** | **0/242（0%）**（定义上恒 0） |
| **非 BE/锁利出场** | 78/123（**63%**） | 45/62（**73%**） |

| 量 | 7–8 月 | 9–11 月 | 判读 |
|---|---|---|---|
| BE/锁利类出场占比 | **55%** | **80%** | 分母膨胀 ← 漂移来源 |
| 非 BE 人群 墓地率 | 63% | **73%** | **变差** |
| 非 BE 人群 `fwd_intraday` 均值 | −0.63% | **−0.77%** | **变差** |
| 非 BE 人群 `fwd_intraday` 正占比 | 35% | 39% | 无改善 |
| 全部 `fwd_intraday` 正占比（半月序列） | 64/46/37/46% | 51/43/43/45/45% | 无趋势 |

⇒ **"腰斩"= BE 出场占比从 55% 涨到 80% 的算术后果**；真实的入场质量（非 BE 人群）从 63% 恶化到 73%。

### 28.3 连带影响（比结论本身更重要）

1. **`mfe_pct<0.3%` 不可跨"出场链时代"比较**：出场链一改（8/17 be_0.4、8/20 方案A），
   这个指标会**自动"变好"**，与信号质量无关。凡以它为端点的判据（含 KR 类死单率目标、
   A11 口径下的达标判定）都必须在同一出场构成下比较，或改报"非 BE 人群死单率 + fwd 收益"。
   ⚠️ A11 是**判据字典条目**，改它=重裁，本节只登记漂移事实，**不动判据**。
2. DA 现状更准确的描述：**80% 的笔在 BE 附近结束（≈0），20% 的笔里 73% 是死单** ——
   整条链的期望 ≈ 0，靠的是 BE 抽干方差，不是信号方向。
3. 与 §27.3 一致（存活组 fwd≈0）：**alpha 全在出场链**，入场侧至今没有可测的改善证据。

### 28.4 本节对历史结论的回溯

| 结论 | 状态 |
|---|---|
| §27.5「墓地率腰斩是真实进步」 | ❌ **撤回** |
| §27.1「12 特征无判别力」 | ✅ 不变（用的是 mfe，但结论是"无差别"，漂移不会制造 AUC≈0.5） |
| §27.3「墓地是方向错」 | ✅ 不变（用的是 fwd，免疫口径） |
| §27.6「打平线 墓地率 20%」 | ⚠️ 降级为"仅在同一出场构成下有效" |

## 二十九、指标漂移体检：把"看起来变好"与"真的变好"分开（2026-11-07）

§28 发现 `mfe<0.3%` 会被出场构成改写。本节把它做成**可复跑的工具**，并对全库跑一次，
给出受影响判据清单——不改任何判据，只让漂移可见。

### 29.1 工具

`scripts/metric_drift_audit.py`（只读、无 cron、无写入）+ `tests/test_metric_drift_audit.py`（5 例，
守标准化语义 / verdict 单位 / BE 族定义）。三个口径缺一即误读：

| 口径 | 定义 | 会不会被出场链污染 |
|---|---|---|
| `raw_dead` | `mfe<0.3%` 占比 | **会**（BE 类必须先到武装线 ⇒ 定义上不可能成死单） |
| `nonbe_dead` | 非 BE 出场人群里的死单率 | 不会（但分母随 BE 占比缩放） |
| `adj_dead` | 按参考期 BE 占比标准化的死单率 | **不会**（同构成比较） |
| `fwd_mean` / `fwd_pos` | 裸持有到当日收盘（`fwd_return_intraday`） | **不会**（与出场方式无关） |

### 29.2 DA 实测（参考构成 = 全窗 BE 占比 68%）

| 时段 | n | BE 占比 | raw 死单 | 非BE 死单 | **同构成 adj** | fwd 均值 | fwd 正占比 | mfe 中位 | 持仓h |
|---|---|---|---|---|---|---|---|---|---|
| 7–8 月 | 274 | 55.1% | 28.5% | 63.4% | **20.3%** | −0.239% | 45.6% | 0.60 | 1.6 |
| 9–11 月 | 304 | **79.6%** | **14.8%** | **72.6%** | **23.2%** | −0.198% | 45.2% | 0.61 | **0.4** |

判定：**DRIFT**（raw −13.7pp "变好"，同构成 +2.9pp **变差**，fwd 仅 +4bps < 5bps 门）。

⇒ 一句话：**死单率的"腰斩"由 BE 出场从 55% 涨到 80% 制造；同构成下死单率反而升了 2.9pp。**

### 29.3 受影响判据清单（只登记，不修改）

`grep` 全仓 `mfe_pct` / `死单率` 的消费方，凡**跨时段比较**都会漂移：

| 消费方 | 用法 | 漂移风险 |
|---|---|---|
| `app/review/*`（daily/monthly/stage_stats/decision_diagnostics/bundle_*） | 报告层死单率与墓地桶 | 高（跨月对比直接读 raw） |
| `app/push/message.py` / `edge_weekly_report.py` | 推送里的死单率行 | 高（用户按此判断"变好"） |
| `scripts/kr3_final_adjudication.py` | KR3 判据含 "MFE=0 次级端点" | 高（跨时代判分） |
| `app/playbook/exit_shadow.py` | `dead_signal_60m_mfe0.3` 等变体定义 | 中（同一窗内比较则无碍） |
| `app/playbook/exit_engine.py` | `dead_signal_exit_mfe_pct` / `mfe_stall` 阈值 | 低（当期行为判据，不作跨期结论） |
| `scripts/gate_a_mfe_replay.py`、`scripts/deb2026100*/**` | 历史回放与探针 | 中（回放窗内一致即可） |
| `ADJUDICATED-CRITERIA` A11 | 死单率唯一口径 | 高（**条文不改**，本节只登记口径边界） |

**处置原则**：判据条文与阈值**一律不动**（改 = 重裁）；报告与推送在引用死单率时，
必须**同屏并列 BE 占比**，或改用 `adj_dead` / `fwd`。这一条属"展示层"，改起来零风险，但需用户批。

### 29.4 局限（诚实）

- `fwd_return_intraday` 来自 `playbook_instructions`，DA 侧填充 96%，非 DA 源较薄；
- `ALL` 口径含非 DA 源，判定为 **MIXED**（不是 DRIFT）—— 说明漂移在 DA 链上最明显；
- 标准化用"全窗 BE 占比"，换参考期只改水平、不改排序（三口径排序稳健）。

## 三十、入场错可否事前识别？—— 24 特征普查 + entry_confirm 反事实（2026-11-07）

用户问：「能否基于个股表现/指标/规律识别入场错，类似但不限于 entry_confirm」。
本节做三件事并给出**否定结论**（工具：`scripts/dead_cohort_feature_scan.py`，只读可复跑）。

**预注册判读纪律**（写死在脚本里，防事后挪门）：
AUC 检出下限 = `0.5 + 1.96×sqrt((n+1)/(12·n_dead·n_alive))`；本案 **n=578 / 墓地 123 ⇒ 下限 0.558**，
低于此值的差异一律视为噪声；有经济意义的门需 AUC ≈ **0.65**（把 21% 墓地率压到 18% 且误杀可控）。
反事实臂成立需：日聚类 |t|≥2 ∧ 单笔最大贡献 <50% ∧ 双时代同号。

### 30.1 特征普查（4 族 24 个，全部「入场前可得」）

| 族 | 特征数 | AUC 区间 | 最好者 | 置换 p |
|---|---|---|---|---|
| DA 输入（gap/入场分钟/RSI/ATR/量比/VWAP偏离/strength/confidence/计划价偏离） | 9 | 0.472–0.537 | vwap_dev 0.537 | 0.27 |
| 个股历史（该标的先验死单率/先验净额/交易次数） | 3 | 0.377–0.541 | **sym_dead_prior 0.377** | **0.00** |
| 个股当日价格行为（开→入涨幅/区间位置/VWAP偏离/早盘振幅/早盘波动/前5分钟收益/开盘缺口） | 7 | 0.477–0.492 | day_open_gap 0.492 | 0.81 |
| 大盘同刻（QQQ 自开盘到入场） | 1 | 0.519 | — | 0.63 |

**唯一"显著"的是 `sym_dead_prior` AUC 0.377（p<0.01，双时代同向 0.395/0.360）——但方向是反的**：
该标的**历史死单率越低，本次越容易死**（即"历史干净的票更容易翻车"）。这是典型的
**均值回归/选择假象**，作为规则等于"专挑历史差的票"，不可执行。
其余 23 个特征**全部低于 0.558 检出下限**，即在现有样本量下连统计意义都没有。

### 30.2 entry_confirm 反事实（真实 1min bar，T×条件 = 12 变体）

不确认则跳过该笔（Δ = −实际净额）；确认则以 T 时刻价入场、同出场价（Δ = 价差×股数）。

| T | 确认条件 | 通过 | 跳过 | 避开死单 | 误杀赢家 | ΣΔ | 日聚类 t | MDE | 单日占比 |
|---|---|---|---|---|---|---|---|---|---|
| 5 | 未破入场价 0.3% | 238 | 264 | 74 | 152 | **+1,042.96** | **+0.58** | 61.2 | **80%** |
| 10 | 未破入场价 0.3% | 184 | 318 | 92 | 182 | +867.67 | +0.43 | 69.0 | 96% |
| 5 | 站上 VWAP | 255 | 247 | 85 | 129 | +512.27 | +0.34 | 52.1 | 116% |
| 15 | 仍站在入场价同侧 | 245 | 257 | 104 | 116 | +109.90 | +0.05 | 71.0 | 752% |
| 30 | 仍站在入场价同侧 | 238 | 263 | 105 | 116 | **−1,897.37** | −0.88 | 73.5 | −44% |

- 最好变体 +$1,043，但 **t=0.58（远不到 2）**、**单日贡献 80%**、逐笔 **中位数为负**
  （多数笔只是入场更差，个别笔靠躲开大亏撑起均值）——与 §26 的"少数笔决定符号"同型；
- **跳过 46–63% 的成交** 才能换来这点差额 ⇒ 误杀赢家 116–210 笔，是典型的"用大量好单换少数坏单"；
- **T 越长越差**（30 分钟两族 −1,900；持有到确认会先被噪声打掉）。

### 30.3 重复开仓规则：同标的上一笔亏了就不再开

| 组 | n | 墓地率 | 净额 |
|---|---|---|---|
| 上一笔净额 ≤0 才开 | 143 | **30%** | **−562.32** |
| 上一笔净额 >0 才开 | 298 | 17% | +206.95 |

Δ = +$562.32、双时代同号（−187 / −375），但 **日聚类 t = +0.82**、**单笔最大占比 48%**
（CRM 一笔 −268）⇒ **不成立**（三条件只过 1 条）。

### 30.4 为什么必然失败（机理，与 §27.3 一致）

墓地组裸持有到收盘 **−0.8%、正占比 28.5%**，T+5 仍 −2.0% ⇒ **入场方向本身错**。
而方向错的原因不在价格几何（gap/VWAP/振幅/动量/个股历史/大盘同刻全部无判别力），
在**DA 对催化剂的解读**（同一批 thesis 模板：财报超预期、合同、评级调整，见 §27 样本）。
价格类指标只能看到"已经在动"，看不到"这个动因是否成立"。

### 30.5 判定（本轮新增两族被证伪）

| 轴 | 状态 |
|---|---|
| E2 入场动量确认 | 已 KILLED（2026-09 退役） |
| ADR-035 限价回调路由 | 已 KILLED |
| 盘前跌幅否决 / 追价帽 / confidence 排序 | 已在关闭账本 |
| **个股当日价格行为确认（本轮 7 特征 + 12 变体）** | ❌ **新增证伪** |
| **个股历史表现（本轮 3 特征）** | ❌ **新增证伪**（唯一非空的是反向假象） |
| 大盘同刻（QQQ） | ❌ 新增证伪 |

⇒ **入场侧不存在"识别"空间**：24 个事前特征 + 12 个确认变体 + 1 个重入规则，全部不过门。

### 30.6 唯一剩下的路（不给门，改输入）

要改变方向错，只能改 **DA 看到什么**（事件核实/催化剂质量/失效条件），而这类改动
**不在历史里**（新 prompt 的输入无法回放）⇒ 按既定纪律只能走**预注册 + shadow 攒样**，
不能回测。且必须先回答：新输入如何改变 thesis 的**可证伪性**（否则只是换措辞）。

**本节结论**：不要再为"入场错"设计筛选器；要动就动输入，且必须 A/B 预注册。

## 三十一、I2/I3 回测审计：证伪条件与催化剂（2026-11-07）

接 §30 的"改输入"路线，先做两项**可在历史里回测**的部分
（工具：`scripts/entry_input_audit.py invalidation|catalyst`，只读可复跑）。

### 31.1 I2 证伪条件是"装饰品"

| 指标 | 值 |
|---|---|
| DA 指令里有 invalidation 文本 | 1,066 / 1,102 = 97% |
| 能抽出可解析证伪价 `alite_inv_price` | 377 / 578 成交 = **65%**（中位距入场 **−4.12%**） |
| **出场前真的被穿越** | **15 / 377 = 4.0%** ⇒ 对 **96%** 的笔从未生效 |
| 被穿越组死单率 | **60%**（9/15）vs 未穿越 19%（基线 21%） |
| 若按证伪价先出场 | ΣΔ **−198.00**，日均 −2.71，**t=−0.85**，单笔最大占比 59%，双时代同负（−84 / −114） |

两点：① 证伪价对 96% 的笔**从未被触发**——不是"没接线"，是**写的内容本身不会被触发**
（多为事件型：财报日期待确认、监管消息、合同执行），当日不可观测；
② 当它真的被穿越时，死单率 60%（基线 2.9 倍）——**有信息但 n=15 且按它出场反而更差**。
⇒ **不接成退出规则**；真问题是要么把证伪条件改写成当日可观测，要么别假装它有约束力。

### 31.2 I3 催化剂分桶：最大的负项集中在"财报业绩"

| 催化剂（thesis 首类命中） | n | 死单% | 净额 | 笔均 | 7-8月净 | 9-11月净 |
|---|---|---|---|---|---|---|
| **财报业绩** | **212** | 24% | **−1,084.48** | −5.12 | **−1,056.5** | **−28.0** |
| 评级目标价 | 120 | 22% | +259.07 | +2.16 | −193.3 | +452.4 |
| 技术形态 | 95 | 16% | **+520.41** | +5.48 | +463.4 | +57.0 |
| 资金持仓 | 48 | 21% | +137.72 | +2.87 | −149.6 | +287.4 |
| 其他 | 36 | 6% | +490.90 | +13.64 | +117.4 | +373.5 |
| 合同订单 | 23 | 35% | **−810.86** | **−35.25** | +55.8 | −866.6 |
| 产品发布 | 16 | 12% | −230.98 | −14.44 | +3.3 | −234.2 |
| 事件政策 | 16 | 31% | +184.49 | +11.53 | +6.5 | +178.0 |
| 并购合作 | 12 | 25% | −120.95 | −10.08 | +61.7 | −182.6 |

表面结论"财报是黑洞"（212 笔吃掉 −1,084，等于 DA 全部净亏的 1.66 倍），
**但 7–8 月 −1,056 / 9–11 月仅 −28**（笔均 −0.29），日聚类 Welch **t = −0.72** ⇒ **不成立**。
210/212 财报类 thesis 都含具体业绩数字/超预期字样 ⇒ 这类"利好已公布再买入"是最可疑的模板，
但现有样本量证不实。

### 31.3 当日"同催化剂聚集度"：数值单调，统计不成立

| 当日单催化剂最多 | 天数 | 笔数 | 笔均 | 盈利天占比 |
|---|---|---|---|---|
| 1–2 笔 | 32 | 114 | **+8.46** | 66% |
| 3–4 笔 | 36 | 277 | −1.04 | 58% |
| **5+ 笔** | 17 | 187 | **−7.12** | **41%** |

数值完全单调（+8.46 → −1.04 → −7.12），盈利天占比 66%→58%→41%，
但 **5+ vs 其余 Welch t = −1.22（n=17 天）** ⇒ 未过门。
机制假设仍成立：DA 在"一个主题"上一天连开 5+ 只，多半是在追已被定价的故事。

### 31.4 同标的+同催化剂跨日重复：非单调，无规律

重复 0 次 +1.65/笔（n=263）｜1 次 **−11.59**（n=96）｜≥2 次 +0.12（n=219）⇒ 无稳定衰减。

### 31.5 顺带核查：出场链还有没有提款空间？

| 组 | n | MFE p50 | MFE p90 | 费/毛 | 净额 |
|---|---|---|---|---|---|
| `SL_BREAKEVEN` | 237 | **0.57%** | **0.96%** | 48.1% | +1,269.77 |
| `SL_TRAIL_LOCK` | 154 | — | — | **9.9%** | +6,584.88 |

BE 组里 **MFE ≥1% 的只有 15/237（6%）**，≥2% 的 1 笔 ⇒ **"把 BE 改成 trail"没有钱可捡**：
这些笔真的只走到 0.6%，出场链已把能收的收完了。

### 31.6 价格带（构成问题，非可预测性）

| 价格带 | n | 净额 | 笔均 | 7-8月 | 9-11月 |
|---|---|---|---|---|---|
| <$10 | 5 | +69.02 | +13.80 | +81.0 | −11.9 |
| **$10–20** | 27 | **+579.05** | **+21.45** | **+214.1** | **+365.0** |
| $20–40 | 16 | −264.02 | −16.50 | −610.3 | +346.3 |
| $40–80 | 34 | +432.13 | +12.71 | +433.8 | −1.6 |
| **≥$80** | **496 (86%)** | **−1,470.86** | **−2.97** | −810.0 | −660.9 |

$10–20 是唯一双时代同正（+214 / +365，笔均 +21.45，n=27），≥$80 双时代同负且占 86% 的成交。
$10–40 合并 vs ≥$80：日均 +8.51 vs −17.30，**Welch t=+0.92** ⇒ **方向一致但未过门**（n=43 笔）。

### 31.7 判定：入场侧六个方向全空，系统处于"结构性打平"

| 方向 | 结果 |
|---|---|
| 24 个事前特征（§30.1） | ❌ 全在检出下限以下 |
| entry_confirm 12 变体（§30.2） | ❌ t=0.58 / 单日 80% |
| 重复开仓规则（§30.3） | ❌ t=0.82 / 单笔 48% |
| **证伪价当退出（§31.1）** | ❌ 触发率 4%，按它出场更差 |
| **催化剂分桶（§31.2）** | ❌ 财报 −1,084 全来自 7–8 月，t=−0.72 |
| **同催化剂聚集度（§31.3）** | ❌ 单调但 t=−1.22 |
| 出场链提款（§31.5） | ❌ BE 组 94% 的笔从未过 1%，无线可提 |

**结论**：在"不改输入、不改标的构成、不降费用"的前提下，DA 的净额已在**局部最优 ≈ 0**。
剩下的可动项只有两个，且都不是"识别入场错"：
① **换输入**（I1：催化剂是否已被定价 / 失效条件当日可观测）——不在历史里，只能预注册 + shadow；
② **换标的构成**（$10–20 带双时代同正 vs ≥$80 占 86% 且双时代同负）——属 universe 调整，
   需先补样本（n=27）再谈，不能拿 27 笔立门。
