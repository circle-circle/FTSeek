---
title: 9/8 美股盘后复盘 + 三处尺子/读数面病灶修复
status: active
module: governance
related_code: scripts/daily_alert_summary.py, app/playbook/horizon_selector.py
related_okr: OKR-2026-09
tags: [复盘, 口径, logrotate, 量纲, 消费方登记]
updated: 2026-09-09
---

# REVIEW-20260909 — 9/8 美股盘后复盘 + 三处读数面病灶修复

- **状态**: 已执行 (2026-09-09, 用户令「美股已经收盘，请复盘检查」→ 复核后裁「按照建议，如果确认无误，执行落地」)
- **执行者**: Codex (本会话)。**并行 Agent 在飞行中** (昨夜至今晨 8 个提交, 最新 `f3a880e6` RFC-079 @05:46; 工作区 64 项未提交, 含 `app/playbook/{admission,db,playbook_service}.py`、`app/llm/features/audit_db.py`、`scripts/llm_universe_scan.py` 等) ⇒ 本批**刻意避开全部在途文件**。
- **范围**: ①9/8 (ET) 交易结果与口径独立复算 ②盘后自动化环路执行健康 ③报告读数面自身的正确性。
- **纪律**: 本批**零交易行为变更、零退出参数变更、零闸门阈值变更、零新闸门** (C1/C7 合规)。三项落地全部在**读数面/文档面/钉子层**: 一个 cron 脚本、一段 Hermes prompt 文本、一处 dataclass 量纲注释。`app/` 只改了 1 个文件的**注释** (`git diff -U0` 核验: 非注释行变化仅 `confidence: float` 一行的行尾注释)。
- **一句话结论**: 9/8 交易日实际是**净亏 $165.53 的一天**，但推送出去的盘后复盘把它报成了**净赚 $119、胜率 67%**；同一天该报告只看到 **6 条盘中告警，真相是 603 条** —— 两个病灶都不是策略问题，是**读数面问题**：一个取错了口径行，一个被 logrotate 截断了 5.5 小时的日志。

---

## §A 9/8 (ET) 结果核验 — 双口径并列 (台账实数)

数据源 `data/ftseek_us.db::trade_records`，全程 `mode=ro`，`source_type='PLAYBOOK_SIM'`，净口径 `COALESCE(pnl_net, realized_pnl)`。

| 口径 | n | 净胜率 | 净 PnL | 毛 | 费 | SQL |
|---|---|---|---|---|---|---|
| **出场日 (今日真值)** | **9** | **5/9 = 56%** | **−$165.53** | −$121.56 | $43.97 | `substr(exit_time,1,10)='2026-09-08'` |
| 入场日 cohort | 6 | 4/6 = 67% | +$118.85 | — | — | `trade_date='2026-09-08' AND exit_time IS NOT NULL` |
| 差额 = 隔夜仓当日了结 | 3 | 1/3 | **−$284.38** | — | — | HOOD −20.64 (`SL_TRAIL_LOCK`) / **UBER −272.62 (`OVERNIGHT_GAP_STOP`)** / RIVN +8.88 (`SL_BREAKEVEN`) |

恒等式核验: `118.85 − 284.38 = −165.53` ✓。假盈利 1 笔 (BIDU #1465 毛 +2.60 / 净 −1.91) ✓。最大连亏 2 笔 (开盘两笔 HOOD、UBER) ✓。

**7 日窗 (9/1–9/8) 同样存在两口径**，报告用的是入场日 (脚本设计如此，`post_market_audit.py:296` 注释明写「7 日窗口按入场日分桶属边界语义，不动」)：

| 口径 | n | 净胜率 | 净 PnL |
|---|---|---|---|
| 入场日 (报告采用) | 58 | 75.9% | +$317.18 |
| 出场日 | 61 | 77.0% | +$396.08 |

**账户与持仓** (`capital_state` @07:57:56 CST): 净值 $98,866.12 | 峰值 $98,979.78 | 回撤 −0.115% | 最大回撤 −3.33% | 现金 $93,637.80 | 市值 $5,228.31 | 水位 green | `is_locked=0`。
持仓 1 只 `US.SNDK` LONG 3 @ $1752.665 (SL 1666.07 / TP 1885.29 / ATR 109.43 = 6.25%)，9/8 日收 1737.99 ⇒ 浮亏 −0.84%；入场 15:43:18 ET，`decision_id=DA-20260908201439-US.SNDK-66cf`。

**执行保真度抽验** (QCOM #1460，当日最大单笔新亏 −$227.50): 入场 09:35:24 @180.94 落在 `09:36` 分钟 bar 区间 [179.99, 181.99] 内；出场 09:45:15 @173.96 落在 `09:46` bar [173.76, 176.01] 内 (bar 标签为**收盘时刻**约定) ⇒ 成交价未越 bar 极值，无填价造假。QCOM 09:39→09:40 一分钟内从 182.32 跌到 175.05，10 分钟 −3.86% 是真实急跌，非数据毛刺。

---

## §B 病灶一 (P1，已修) — 盘后推送报告把亏损日报成盈利日

### B.1 事实

Hermes cron `7a40dd7c9e94`「FTSeek-US 盘后综合复盘」@04:08 推送飞书 (`message_id=om_x100b6532fbfabca4c3b65c8cf984a67`) 的头条写：

> **今日**: 6笔已结算 净胜率67% 净PnL=**+$119** … (毛口径: 9事件 毛$-121.56 费$43.97 → 净-$165.53 **含未结算行**)

而 Step 1 脚本自己的权威输出 `logs/audit/attribution_debate_2026-09-08.md` 写的是：

> **今日**: 9笔 毛$-121.56 费$43.97 → **净$-165.53** | 净胜率56% | ⚠️假盈利1笔

三处错：①头条取了错的那一行；②括注把**净额** −165.53 标成「毛口径」；③把它标成「含未结算行」，而该 SQL 带 `exit_time IS NOT NULL`，9 行全部已结算。

### B.2 根因 (不是 LLM 手滑，是纪律缺口)

`scripts/post_market_audit.py` 里并存两套「今日」：

| 出处 | 归集键 | 语义 | 9/8 值 |
|---|---|---|---|
| `attribution_fee_net` (`scripts/post_market_audit.py:94`) | `substr(exit_time,1,10)` | **出场日记账** — 2026-08-28 专为「隔夜仓今日了结的盈亏 (含跳空) 整笔漏记」而改 | 9 笔 −165.53 / 56% |
| `attribution_trend` (`scripts/post_market_audit.py:296`) | `trade_date` | **入场日 cohort** — 同期注释明写「7 日窗口按入场日分桶属边界语义，不动」 | 6 笔 +118.85 / 67% |

Cron prompt 的「口径纪律」6 条只钉了**净/毛**与**已平仓过滤**，**没有钉「今日」按哪个日期归集** ⇒ 写报告的模型可以合法地在两行里挑一行，而趋势表那行数字更好看。

**这不是常态而是回归**：9/3、9/4 的盘后报告头条都正确取自出场日行 (9/4「今日: 10笔 … 净$+191.17 净胜率90%」= `fee_net.today`)。且**同时段的另一个环路已经写对了** —— `ftseek-intraday-diagnose-late` @04:30 输出：

```
• 今日交易(入场日口径): 6笔 → 4胜2负, 已实现净PnL = $118.85
  🌙 另: 隔夜仓今日了结 3笔 净$-284.38 (出场日口径计入今日, 本段不含)
  → 出场日口径合计(=心跳'已平盈亏今日'): $-165.53
⚠️ 关注点: 1. 今日PnL为负(入场日口径$118.85/出场日口径$-165.53), 关注大亏交易
```

同一夜、同一份数据，两个环路给出**符号相反**的头条，而推送给用户的是错的那个。

### B.3 落地 (只改 prompt 文本，零代码)

`hermes cron edit 7a40dd7c9e94 --prompt`，prompt 6913 → 7850 字符 (净增 937)，**除 prompt 外零字段变化** (逐字段比对: `schedule`/`enabled`/`state`/`deliver`/`workdir`/`model`/`next_run_at=2026-09-10T04:00:00+08:00`/`repeat.completed` 全未变，`jobs.json` 仍 24 个作业)。三处补丁：

1. **口径纪律新增 §0** (插在原 §1 前，不重编号)：「今日 PnL/笔数/胜率」头条**一律出场日口径**，必须逐字取 `## 💰 费用净额总览` 的今日行；趋势表是入场日 cohort，只能作附表且表头标注；两口径必须并列带名 + 隔夜仓差额单列一行；**禁止**把出场日行标成「毛口径」或「含未结算行」；与 `intraday_diagnose` 读数不一致时以出场日口径为准并显式点出差异，不得静默择一。附 9/8 实证数字 (9 笔 −165.53/56% vs 6 笔 +118.85/67%，差额 −284.38 = 3 笔隔夜仓，UBER −272.62 独占)。
2. **输出格式「📈 交易概览」** 改成头条三行必带口径名 (①今日出场日口径 ②当日新开当日了结 ③🌙 隔夜仓今日了结)，趋势表降为标注「入场日cohort·不作头条」的附表。
3. **输出格式「🚨 告警摘要」** 要求 ERROR/WARN 分列、去重组的 ×出现次数照抄 Step 3 不得自行压缩，并**原样带一行 Step 3 的 `[覆盖]` 诊断** (见 §C)，档数=1 且跨度明显短于会话窗即标「⚠️ 告警覆盖可能不全」。

### B.4 未做的事 (刻意)

没有去改 `post_market_audit.py` 把趋势表也换成出场日 —— 那会同时改掉 7 日窗语义 (脚本注释已明示「不动」)，且 9/5 刚为这张表修过一次截断 bug (`attribution_trend` 表头闭合)。**一次一个变量**：本批只钉「头条取哪一行」，不动任何一行的算法。

---

## §C 病灶二 (P1，已修) — 告警汇总被 logrotate 截断，只看到会话最后 12 分钟

### C.1 事实

`data/reports/daily_alert_summary_2026-09-08.md` 报「盘中告警: 0 错误 + **6 警告** (共 6 条)」，两条唯一告警的时间戳都是 **03:48:24**。

真值：该会话 `chase_cap拦截` 共 **148 条** (SKHY 76 + SNDK 72)，时间跨度 21:35:31 → 03:58。修后复算全窗：**636 条告警，盘中 603 条 (3 ERROR + 600 WARN)，138 个去重组**，跨度 21:01:41 → 05:43:42。

**报告只看到真相的 4% (6/148) / 1% (6/603)。**

### C.2 根因

- `/etc/logrotate.d/ftseek-us` = `daily` + `rotate 14` + `compress` + `delaycompress` + **`copytruncate`** + `dateext -%Y%m%d`，由 `/etc/cron.daily/logrotate` 触发。
- 实测轮转时刻 (取轮转档首行时间)：9/2 **03:35**、9/3 **03:12**、9/4 **03:44**、9/5 **03:26**、9/9 **03:48** (非交易日 9/6 06:44、9/7 06:28 无害)。
- 美股会话 = **21:30–04:00 CST** ⇒ 轮转**每天都在会话内部发生**，会话前 ~5.5 小时被切进 `logs/ftseek_us.log-<今日>`，当前档只剩最后 12–48 分钟。
- `scripts/daily_alert_summary.py:774` 只读 `logs/ftseek_us.log` 一个文件。
- 扫描窗 `since=21:00 → until=次日06:00` 本身是对的 —— 窗口对了，**载体少了**。

**慢性、非孤例**：9/4 汇总报 2 条警告 (时间戳都是轮转后的 03:52)，而 `logs/ftseek_us.log-20260905.gz` 里有 **142 条 WARNING/ERROR**。

### C.3 落地

`scripts/daily_alert_summary.py` 新增两个只读函数 (零判级/零噪音过滤逻辑改动 —— 逐档复用 `parse_log_alerts` 这个单一真源)：

- `resolve_log_sources(log_path, since, until)` → 按 `dateext` 规则枚举 `since.date()..until.date()` 的轮转档 (`.gz` 优先于裸档)，日期升序，当前档殿后；未轮转时只返回当前档；窗口外的档 (如上一会话的 `-20260908.gz`) 由 `parse_log_alerts` 的时间戳过滤兜成 0 条 (实测 `ftseek_us.log-20260908.gz=0`)。
- `parse_log_alerts_window(...)` → 逐档解析后**跨档按 (time, level, content) 去重** (copytruncate 本身不重叠，此举只防「脚本运行途中正好轮转」的读写竞态把同一行计两次)，合并按时间升序，并返回一条 `[覆盖]` 诊断行 (档数 + 逐档条数 + 告警跨度 + 窗口边界)。
- `main()` 改用上述入口，并把 `[覆盖]` 行打到 stdout ⇒ **盲区若复发，在读数上直接可见**，不靠人事后对账。配合 §B.3 补丁 3，这行诊断会被带进飞书报告。
- **收尾自检补的一刀 (同日 09:45)**：首版只把诊断行打到 stdout，而 `build_summary_report()` 生成的**推送正文里没有** (打桩干跑实测正文 `含[覆盖]行: 0`) ⇒ 本脚本自己直推钉钉/飞书的那份报告，读者仍只看到「- ✅ 盘中系统运行正常，无异常告警」而看不到它的前提 —— 那正是 9/8 说谎的同一行。已补 `build_summary_report(..., log_coverage=…)` (可选 kwarg，旧调用面零变化)，把两行诊断渲染进正文**总结区、紧跟判级那句之后**，并注明「档数=1 且跨度远短于窗口 ⇒ 上面的告警计数不可信」。**纯观测行，不参与判级** (改判级=改判据，须另立案，见 §F-4 同源纪律)。新增 2 枚钉子锁住渲染与向后兼容 (`tests/test_daily_alert_summary_log_sources.py` 8→**10 例**)。

修后 9/8 复算 Top 组 (去重后 ×出现次数)：`chase_cap违例遥测 SKHY ×76` / `chase_cap拦截 SKHY ×76` / `chase_cap违例遥测 SNDK ×72` / `chase_cap拦截 SNDK ×72` / **`[Finnhub] 触发 429 限速 ×61`** / `[再入场] ROIV 反手拦截 ×15` / `[再入场] BE 反手拦截 ×12` / `[FundamentalProvider] Finnhub 429 ×7`。

⇒ **首次暴露两个此前完全不可见的信号**：①Finnhub 429 限速 68 次/日 (数据源配额压力，与 21:01 `US.ANET 所有数据源均失败` 可能同源)；②再入场反手闸 27 次/日。两者本批**只登记不处置** (见 §F)。

### C.4 顺带核实为「正常」的两件事 (不夸大收益)

- **`chase_cap` 闸门行为完全正确**：SKHY 76 次拦截**始终未成交**；SNDK 72 次拦截后，03:43:12 re-price 到 $1753.76 **落回禁追线 $1760 以下**才于 03:43:13 下单、03:43:18 成交 $1752.665 (`futu_order_id=8165906`, `entry_price_source=broker_deal_confirm`)。「每 ~5 分钟重评」是**设计意图** (价格可能回落到帽下，SNDK 正是因此成交)，不是缺陷。
- 但日志措辞 `→ 终态跳过 (P0-A enforce)` 与行为相反 (「终态」读起来像指令永久作废)，`playbook_instructions.skip_reason` 落的也是 `chase_cap_enforced(reprice=...,cap=...)`。**本批未改**，见 §F-1。

---

## §D 病灶三 (P2，只钉不改) — `horizon_confidence` 异基量纲，列名说谎

### D.1 事实 (data/ftseek_us.db::playbook_instructions, date ≥ 2026-08-20, n≈840)

| 取值 | 行数 | 说明 |
|---|---|---|
| `0.0` | **698 (83%)** | 两条来源不可区分：`pipeline.py:1647` 异常兜底硬编码 0.0 / 得分真近平局 |
| `0.7` | 103 | fallback 规则档常见 margin |
| `0.64` / `0.5` / `1.0` | 12 / 5 / 5 | 其中 `1.0` 含 `select()` 禁用分支的硬编码 |
| `1e-5 ~ 1e-4` (0.000136 / 7.4e-05 / 3.7e-05 …) | 长尾 | historical_return_model 近平局 |
| **`2.0` (9/1) / `1.5` (9/2)** | 各 1 | **>1 ⇒ 绝不可能是概率** |

### D.2 根因

`app/playbook/horizon_selector.py::HorizonDecision.confidence` 的定义就是 `|short_score − long_score|` —— **得分差，不是概率**。四条生成路径不同基：

1. `_score_with_model` (`:284`)：得分 = 历史收益贡献的**均值** ⇒ 近平局时 margin 落在 1e-5..1e-2；
2. `_fallback_score` (`:430`)：得分 = 0.5/0.2 步长的**规则累加** ⇒ margin 落在 0.1..2.0；
3. `select()` 禁用分支 (`:111`)：硬编码 `1.0`；
4. `pipeline.py:1647` 异常兜底：硬编码 `0.0`。

两条主路径的自然量级**相差约 5 个数量级**。危害：任何绝对阈值 (如 `>0.3` 才算有信心) 会把路径 1 的**全部**行判成低置信，与证据强度无关 —— 与军规「单位混装」前三案同型 (ATR 百分比直落绝对槽 / `da_glm_shadow` 异基 taint 误杀 / KR2 快照漏键假绿)。

### D.3 为何只钉不改 (C1 冻结安全论证)

**已核实 horizon 的选择完全不读 `confidence`**：模型路径走 `_choose()` 的**比值**判据 `long_score > short_score*(1+margin_threshold)`，fallback 路径走 `long_score > short_score * 1.1`。⇒ 归一化本字段**不可能**改变 horizon 选择，因而不触碰 `config/playbook_horizon.yaml` 的 sl/tp/time_exit/max_hold ⇒ 与 OKR-2026-09 **C1 退出参数冻结**及 **KR2 cohort 唯一值数**无耦合 (当前 `n_unique=1`，`exit-de49b1299290` n=32，末笔 9/8，零换版在轨)。

反过来说：既然零耦合、且**当前零闸门消费** (只有 `shadow_tracker.py:106` 的 DEBUG 日志 + `playbook_service.py:1800` 看板透传)，那么改计算是**纯风险零收益**；而真正会出事的是「将来有人把它当概率接进门」。所以本批把力气全花在**声明 + 钉子**上。

### D.4 落地

1. **量纲声明写进 SSOT** (`HorizonDecision.confidence` 注释块，17 行)：得分差非概率、无上界 (>1 合法，实测 2.0/1.5)、1e-5 是近平局不是缺测、四条生成路径逐条在案、`_choose` 不读本字段的 C1 安全论证、消费方登记 (零闸门消费)。`git diff -U0` 核验：**非注释行变化为 0** (唯一 +/- 是 `confidence: float` 一行的行尾注释)。
2. **消费面白名单钉子**：`tests/test_horizon_confidence_unit.py` (12 例) —— ①`confidence == |short−long|`；②fallback 能产出 >1 且**不得被截断** (若哪天恒 ≤1，说明有人偷偷改成概率口径 = 改判据，测试红)；③近平局 1e-4 量级合法 (照抄 9/8 SNDK 实值 0.000136)；④禁用分支恒 1.0 是另一基；⑤`_choose` 入参与函数体都不含 confidence；⑥fallback 判定链是比值不是 confidence 比较；⑦**引用 `horizon_confidence` 的文件集合必须与白名单逐字相等** (多 = 有人新接消费方没读声明；少 = 白名单腐烂)；⑧**全仓禁止对它做数值阈值比较**；⑨⑩量纲声明四条生成路径逐条在案、且必须是注释 (零行为变化)。

**重开条件** (满足任一才动计算，且须先落档 ADR/DEB)：①出现第一个 live 闸门消费方；②C1 冻结窗结束 (2026-09-30) 且 KR2 cohort 已闭合；③要把列名改成 `horizon_margin` —— 那是断 5 个引用面 + 840 行历史的迁移，须按减法三步走。

---

## §E 核验为健康的项 (逐条取证，不是推测)

| 项 | 读数 | 取证方式 |
|---|---|---|
| 四锁 | 全闭 ✓ | `AUTO_TRADE_ACCOUNT=sim`、`ALLOW_REAL_TRADING=false` (读 `/proc/733136/environ`)、`config/unified_config.yaml:1050 execute_mode: futu_simulate`、KR2 `cohort.n_unique=1` / `status=single_version` (`data/invariant_check_latest.json` @05:05) |
| 不变量自检 | **全过 ✓** | `logs/invariant_check.log` 05:05:02 — 58 笔，`slippage_bps`/`exit_telemetry`/`entry_price_source`/`attribution`/`fees_est` 五个字段健康度全 58/58 |
| 服务健康 | 0 异常 | 05:42:31 重启后 `NRestarts=0`，`ERROR\|CRITICAL\|Traceback` = **0**，`health_check.sh` 10 分钟一轮全过 (至 08:30)，`deploy_freshness_check` 重启后静默 = 磁盘与运行态一致 |
| 持仓对账 | `drift=[]` | `data/audit/reconcile_live.jsonl` 末行 `ts=2026-09-08T20:02:28+00:00` (= **04:02:28 CST**，盘后新鲜) broker 1 / internal 1 / external 0 |
| IC 回填 | 正常 | `logs/backfill.log` 05:32 — 总快照 4424 / 已回填 T1 4403 / 完全回填 3657 / 待回填 21 |
| 行情断线自愈 | ~100ms | 00:48:08 QUOTE/K_1M/ORDER_BOOK/TICKER 四类订阅全部 `Disconnect` → 同秒重连 + 重订 44 只 + `✅ 快照验证通过 US.SNDK $1785.33 futu_lv3`，watchdog 按设计工作 |
| 会话 ERROR | 仅 3 条 | 21:01:46 `US.ANET 所有数据源均失败`；00:47:38 `退订部分失败 成功=2 失败=2，OpenD 侧槽位可能仍被占用`；04:01:16 TwelveData read timeout (盘后) |
| 守卫重启 | 正确拒绝 | `logs/restart_ftseek_us_guarded.log` 08:05 `exit=4` — 工作区 11 项未提交 `app/\|scripts/` 改动 ⇒ 不由定时任务载进实盘进程 |
| 影子环路 | 全在跑 | `eod_depth_gate_settle` (arm 总=1 / 积压 1 / `KEEP_SAMPLING`)、`overnight_arm_report` (w_week n=13/20 修复率 69.2% 净 −733.7 / `KEEP_SAMPLING`)、`kr3_runup_shadow` (采集 7 笔 / prev_close 缺失 0)、`penetration_backfill` (written 6 / bad 0)、`shadow_board` (飞书已推送) |
| 唯一判读入口 | 按设计出「无裁决资格」卡 | `cf_pullback_limit_replay` 05:47 — 窗 9/8→9/8 配对 n=2 < 30 ⇒ 只报点估计 (配对差中位 −2.44 美元/笔，符号检验 p=0.75)，许可层级 `['L1a']`，`entry_archetype_routing_enabled` 维持关闭。**cron 只跑单日 ⇒ 卡永远无裁决资格是设计使然**；正式裁决点是 9/12 中期体检与 9/16 正式裁决 (手动跑) |

**cron `7a40dd7c9e94` 终态 `error` 是记账误报，不是丢报告**：04:00:00.038 准时派发 → 04:08:33 `ERROR cron.jobs: Timed out waiting for local fire fence … failing closed` + `fire claim ownership lost; interrupting stale run` → 04:08:51 **报告已成功投递飞书**。即投递成功之后才被判定中断，`last_status=error` / `failure_streak=1` 与事实相反。但 5 小时内 3 次 `fire claim ownership lost` (23:31:51 `36cc015242ee`、04:08:33 `7a40dd7c9e94`、04:42:10 `2a0eb17a475e`) 是新出现的模式，登记为 §F-6。

---

## §F 已登记未处置 (本批只报不改，附不做的理由)

| # | 病灶 | 证据 | 不做的理由 / 处置路径 |
|---|---|---|---|
| F-1 | `chase_cap` 日志措辞 `→ 终态跳过` 与行为相反 (实际每 ~5 分钟重评，SNDK 正因重评才在价格回落到帽下后成交) | `app/playbook/pipeline.py:4353-4355`；9/8 SKHY ×76 / SNDK ×72 | 改的是**最热路径文件的日志字符串**，需重启才生效；而 `restart_ftseek_us_guarded.py` 08:05 正因并行 Agent 的 64 项未提交改动拒绝重启 ⇒ 此刻改 = 制造部署断层却上不了线。零行为价值，等并行批收口后随批带走 |
| F-2 | `playbook_instructions.skip_reason` 在指令后续成交时不清空 ⇒ `executed=1 AND skip_reason<>''` 有 **12/495** 行 (今日 SNDK 即在列：`chase_cap_enforced(reprice=1766.75,cap=1760.0)` 却 `executed=1`) | DB 全量统计；分布 9/8×1、8/24×3、8/4×1、7/31×1、7/7×2、7/2×4 | 任何按 `skip_reason<>''` 统计「为何没成交」的查询都会把这 12 行误计为跳过。改点在 `app/playbook/db.py` / `playbook_service.py` —— **两个都在并行 Agent 在途清单里**，本批不碰 |
| F-3 | `scripts/okr_status.py:539` 追加 `okr_history.jsonl` **无幂等** | 157 行里 **17 个日期重复**：7/2 ×21、6/26 ×13、7/8 ×10、**9/6 ×9** | 盘后报告的「与昨日 delta」实际在跟当天某次临时跑比 (今日对比的就是 9/6 19:58 那行)。`okr_status.py` 也在并行 Agent 在途清单里 |
| F-4 | `daily_alert_summary` 判级把含「拦截/拒绝/失败」关键词的 **INFO 行**计成 WARN | 修后 600 条 WARN 里含 `[再入场] … INFO: … 反手拦截` ×27 | 会抬高「警告」基数。但改判级 = 改这条环路的**判据**，须先落档；且本批刚改过同文件的覆盖范围，**一次一个变量** |
| F-5 | 低 ATR 门「当日首记后静默」的去重键用 **CST 日历日**而非 ET 交易日 ⇒ 同一 ET 交易日跨 CST 午夜后重复记一次 | `US.NVS atr_pct=0.0234 < 0.03` 在 21:35 (9/8) 与 00:48 (9/9) 各记一次 | 纯日志噪音，不影响闸门判定。改点在 `app/playbook/pipeline.py`，同 F-1 理由 |
| F-6 | Hermes `fire claim ownership lost` 5 小时内 3 次；其中一次把**投递成功**的作业记成 `last_status=error` / `failure_streak=1` | `/root/.hermes/logs/errors.log:4572-4573`；`gateway.log` 04:08:51 投递成功 | 仓外基础设施 (Hermes)，非本仓代码。影响面是**失败计数失真** (streak 累积可能触发误告警)。需另立一案查 fire fence 超时阈值 |
| F-7 | Finnhub 429 限速 **68 次/日** (61 + 7)，此前被 §C 盲区完全遮蔽 | 修后复算 Top 组 | **本批新暴露**，尚无历史基线可判趋势。先攒 3–5 个交易日读数再判是否需要配额/降级策略；与 21:01 `US.ANET 所有数据源均失败` 是否同源待查 |
| F-8 | `playbook_regime_log` 混装行类型：`regime` 列存 `weekly_report`、vix/spy 全 0.0、`date` 口径不一致 (id=63 `date=2026-09-11` 未来日 / id=60 `date=2026-09-04`)、id=61 缺号 | `SELECT * FROM playbook_regime_log ORDER BY rowid DESC LIMIT 5` | 任何 `SELECT regime … WHERE date=?` 的消费方可能拿到 `weekly_report` 当 regime，或对 0.0 求均值。需先查全部消费方再定处置 |
| F-9 | `data/ftseek_us.db::alerts` 表 **0 行** (min/max timestamp 均为 NULL) 而建表与索引俱在 | `SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM alerts` | 告警实际走日志解析 (`daily_alert_summary.py:170`)，本表疑似死载体。按军规须做**五个调用面**零引用验证 (仓内 import / crontab / systemd / Hermes prompt / `.git/hooks`) 才能判退役，本批未做 |
| F-10 | `00:47:38 退订部分失败 成功=2 失败=2，OpenD 侧槽位可能仍被占用 (需人工/重试)` | 会话日志 ERROR 第 2 条 | 当前槽位 88/1000 远未到顶，但泄漏会累积。需查是否有槽位对账环路 (疑似缺) |
| F-11 | 持仓对账时间戳未标时区：报告写「2026-09-08 20:02」，实为 UTC (= CST 9/9 04:02) | `reconcile_live.jsonl` `ts` 带 `+00:00`，报告渲染时丢时区 | 读者会误判成 8 小时前的盘前快照。与 handoff 已登记的 `trigger_gate_history.jsonl` 缺 `generated_at` 同源 (时间戳必须带时区) |
| F-12 | **`tests/test_except_pass_ratchet.py` 既有红** — `except:pass/continue` 计数 BASELINE **955** → HEAD **960** → 工作区 **959** (自检收尾时跑出，非本批引入) | 逐提交同款正则复算 (`git ls-tree`+`cat-file --batch`，排除 `tests/venv/archive/obsidian`)：`be57742a`(9/7 22:58) **956 首次转红** → `dd22e09c`(9/8 07:05) 960 → `33d5568c`(9/8 19:01) 清剿回 **954** → `99198643`/`df8d0a9e`/`b816ceaf`/`42c8e91b` **955 贴线** → **`1e296430`**(9/9 00:37 批D) **956** (`app/web_dashboard/live_ledger.py` +1) → **`f3a880e6`**(9/9 05:46 RFC-079) **960** (`app/playbook/exit_engine.py` +1、`app/playbook/overnight_decision.py` +1、`app/playbook/pipeline.py` +1、`scripts/settle_eod_depth_gate_shadow.py` +1) | **本批贡献 0**：`scripts/daily_alert_summary.py` HEAD=工作区=**3**、`app/playbook/horizon_selector.py` **0**=0，两个新钉子在 `tests/` 内被 `_EXCLUDE_PARTS` 排除 ⇒ 非本批变量，**不修、不调基线**。处置路径按 `DEB-20260907` §4 先例：谁引入谁按 R5 改 `logger.warning` 留痕，基线只降不升；本批只登记 (同款既有红先例见 `REVIEW-20260908-trade-ui-snr-ux.md:291`)。**连带影响面已查**：该钉子不在 `startup_smoke_check.py` 与 `restart_ftseek_us_guarded.py` 路径上 ⇒ 不阻塞重启，只在全量/定向 pytest 时可见 |

---

## §G 亏损结构 (只报数，不出行为变更建议 — C1 冻结期)

**`SL_INITIAL` 近 7 日 7 笔全亏 −$780.90**，且 **7 笔 `mfe_pct` 全为 0.0** (从入场就没绿过)：

| 日期 | 标的 | 方向 | 净 | MFE% | MAE% | ATR% |
|---|---|---|---|---|---|---|
| 09-01 | US.SAIC | LONG | −64.18 | 0.0 | 1.238 | 5.00 |
| 09-01 | US.AMD | LONG | −60.81 | 0.0 | 1.030 | 5.00 |
| 09-01 | US.LLY | LONG | −51.76 | 0.0 | 1.012 | 0.27 |
| 09-03 | US.HPE | SHORT | −153.95 | 0.0 | 3.525 | 5.50 |
| 09-03 | US.AVGO | SHORT | −64.65 | 0.0 | 1.037 | 2.00 |
| 09-04 | US.USAR | LONG | −158.05 | 0.0 | 3.151 | 5.03 |
| 09-08 | US.QCOM | LONG | −227.50 | 0.0 | 4.120 | 4.51 |

结构读数：**5/7 落在 ATR 4.5–5.5% 桶** ⇒ 高波动票拿到宽止损 (QCOM 实测 SL 距离 3.86%)，方向一错就是 −3.9%~−4.9% × ~$5.9k 名义 = −$110~−$230/笔。QCOM 09:35:24 入场、09:45:15 被扫，**仅 10 分钟** ⇒ `early_tight_sl` (30min + MFE<0.3% 窗) 结构上来不及介入。这与 handoff 已登记的 `SL_INITIAL` 主漏同源，**不新增判据**；`overnight_arm_report` / `eod_depth_gate_settle` 两条影子正在按预注册判据攒样，本批不干预。

**UBER #1437**：9/3 09:39 入场 @78.09，SL 74.57，持 **19.4 交易小时 / 3 个隔夜**，9/8 09:30:51 被开盘跳空穿到 74.26 成交 (−0.42% 穿价)，净 −$272.62，MFE 仅 0.34%。即「隔夜仓开盘即杀」形态 —— 正是并行 Agent 昨夜立项的 **RFC-079 EOD 深水闸影子** 的目标病灶，影子 arm 已登记 (总=1，未结算积压 1)，`verdict=KEEP_SAMPLING`。本批不重复立案。

**单位经济**：9 笔出场日样本的名义额中位 ≈ $5.9k、费均 $4.89/笔 ≈ 8.3bps 单程；当日净 −$165.53 ≈ −3.1bps of notional。与 `data/exit_scorecard_latest.json` (since 8/7, n=221, 毛 +10.20bps − 费 8.95bps = 净 +1.25bps，日聚类 t=−0.10 / MDE 35.41bps ⇒ `distinguishable: false`) 一致：**edge 仍在噪声地板以下**，9/8 不构成任何方向性证据 (宪章第 9 律)。

---

## §H 验证与回滚

### H.1 验证

| 项 | 命令 / 方式 | 结果 |
|---|---|---|
| 新钉子 | `pytest tests/test_daily_alert_summary_log_sources.py` | **10 passed** (覆盖发现/排序/.gz/未轮转/窗外档归零/跨档去重/升序/诊断内容/缺文件不抛 + **正文渲染** + **旧调用面向后兼容**) |
| 新钉子 | `pytest tests/test_horizon_confidence_unit.py` | **12 passed** |
| 定向回归 | `pytest tests/test_pipeline_horizon.py tests/test_pipeline_batch_e_contracts.py tests/test_daily_alert_summary_pnl.py tests/test_daily_alert_equity_recon.py tests/test_daily_alert_summary_log_sources.py tests/test_horizon_confidence_unit.py tests/test_w2_field_cardinality_ratchet.py tests/test_replay_engine_import_smoke.py` | **76 passed** |
| 台账守卫 | `pytest tests/test_control_loop_ledger.py` | **10 passed** |
| 静态门禁 | `venv/bin/python scripts/check_undefined_names.py` × 4 个改动文件 | `GATE_EXIT=0` |
| 语法 | `py_compile` × 2 | OK |
| 真库复算 (只读) | `resolve_log_sources` + `parse_log_alerts_window` 跑 9/8 真实会话窗 | 3 档 / 636 条 / 跨度 21:01:41→05:43:42；上一会话档 `-20260908.gz=0` (时间戳过滤生效) |
| cron prompt 变更面 | 逐字段 diff before/after 快照 | **变化字段仅 `prompt`** (6913→7850)，sha1 `97f88bef…` 与预期一致 |
| `app/` 行为变更 | `git diff -U0 app/playbook/horizon_selector.py` 过滤非注释行 | **0 行可执行代码变化** |
| `app/` 行为变更 (二次硬校验) | 剥掉全部 `#` 注释与空行后 `diff HEAD 版本 ↔ 工作区版本` | **CODE-IDENTICAL-AFTER-STRIP-COMMENTS** |
| 棘轮守卫 (三枚) | `pytest tests/test_w2_field_cardinality_ratchet.py tests/test_except_pass_ratchet.py tests/test_naive_datetime_now_ratchet.py` | **20 passed / 1 failed** — 唯一失败 `test_except_pass_not_growing` (955→959) = **既有红**，归因见 F-12 |
| 定向回归 (扩面复跑) | `pytest` × 14 文件 (horizon 全家 7 + batch_e 契约 + alert_summary×2 + 台账守卫 + 回放引擎 import 冒烟 + no_archive_imports + w1 退出版本守卫) | **94 passed** |
| 共享脏文件的归属核验 | `diff /tmp/*.bak-20260909 ↔ 工作区` | 台账 **仅 +1 行** (本批行；「冻结区」段改动是并行 Agent 既有未提交内容，备份里已存在，未被本批触碰)；`handoff.md` = 头部 1 行→2 行 + 新条目 28 行 (1198→1227)，无重复条目 |
| cron 回滚可用性 | 备份 `/tmp/postclose_cron_prompt.orig-20260909.txt` ↔ `jobs.json` 旧快照 | 逐字一致 (sha1 `28059516`)；`schedule=0 4 * * 2-6`、`enabled=True` 未动 |
| **端到端打桩干跑** (补正文渲染后) | 进程内 import 脚本 + 打桩 `push_to_dingtalk`/`push_to_feishu` (零触网) + `sqlite3.connect` 强制 `file:…?mode=ro` (服务在跑、并行 Agent 在途，绝不允许写) → 调 `main()` | `rc=True` 无异常；stdout `[覆盖]` US **3 档 (…-20260908.gz=0, …-20260909=592, ftseek_us.log=44)** 跨度 `21:01:41→05:43:42` / HK 1 档 0 条；**推送正文尾部出现「🗂 日志覆盖自检」两行**且钉钉/飞书两份**逐字一致**、落盘文件与正文一致 (1618 bytes)；正文计数 **3 错误 + 600 警告 (共 603 条)** = 修后复算值。**副作用零残留**：跑完 `cp -p` 还原 `data/reports/daily_alert_summary_2026-09-08.md`，sha1 回到原件 `85dc15fc…` (该文件是 §C 的证据原件，`data/reports/` 未被 git 跟踪) |

**未跑**：全量 `pytest` (含网络依赖用例，历史上超时)；`startup_smoke_check.py` —— 本批**未重启服务**，理由见 H.3。

**唯一红项的诚实交代**：`test_except_pass_not_growing` (955→959) 在本批**开始前就已是红的**，且本批两个 `.py` 的计数在 HEAD 与工作区**完全相同** ⇒ 与落地无关。红的具体出处、转红提交与连带影响面见 **§F-12**，处置待裁见 **§I-7**。另两枚棘轮 (`w2_field_cardinality` / `naive_datetime_now`) 绿。

### H.2 回滚

```bash
# ① cron 脚本 (恢复单文件读取)
cp /tmp/daily_alert_summary.py.bak-20260909 scripts/daily_alert_summary.py
#   原 sha1 e462c5d42b03be1313f0ba52a13c47e723f260e6 → 现 2b66bbb4df41f21a2008d4ed58ba0b0b28642c33
#   (备份是**跨档拼接之前**的原版; 回滚会同时撤掉 §C.3 末条的正文渲染 log_coverage)

# ② Hermes prompt (恢复 6913 字符原版)
hermes cron edit 7a40dd7c9e94 --prompt "$(cat /tmp/postclose_cron_prompt.orig-20260909.txt)"
#   原 sha1 280595164509250e2437f3ce60a38ded508a6faa；jobs.json 全量备份 /tmp/jobs.json.bak-20260909-postclose

# ③ 量纲声明注释
cp /tmp/horizon_selector.py.bak-20260909 app/playbook/horizon_selector.py
#   原 sha1 a2b686d9ccc6693726ccc6872d2e7f57cd7f37b4 → 现 acfae6c76d8c2749da648a47d26ed658925798f6

# ④ 台账行
cp /tmp/CONTROL-LOOP-LEDGER.md.bak-20260909 docs/CONTROL-LOOP-LEDGER.md

# ⑤ 钉子
rm tests/test_daily_alert_summary_log_sources.py tests/test_horizon_confidence_unit.py
```

### H.3 是否需重启 — 结论：**本批不需要，且刻意不重启**

- `scripts/daily_alert_summary.py` **不被服务进程 import**，只由 Hermes cron `7a40dd7c9e94` Step 3 以子进程调用 (`crontab -l` 零命中，无系统 cron 挂载) ⇒ 下次 04:00 跑即生效，无需重启。
- `app/playbook/horizon_selector.py` 改动是**纯注释**，重启与否行为完全一致。
- **不重启的独立理由**：`restart_ftseek_us_guarded.py` 08:05 已因工作区 11 项 (现 64 项) 未提交 `app/|scripts/` 改动拒绝重启 (`exit=4`，「谁改的谁决定何时上线」)。此刻手动重启会把并行 Agent 在途的 `app/llm/features/audit_db.py`、`app/playbook/{admission,db,playbook_service}.py`、`scripts/llm_universe_scan.py` 一并载进模拟盘进程 ⇒ 违反该守卫的设计意图。
- **代价 (如实登记)**：本批在 `app/` 留下了 1 处注释级改动，`deploy_freshness_check.py` 会持续报 `stale_drift` 直至下次重启；因守卫的未提交改动条件仍成立，**不会触发自动重启** (安全窗 09:00–12:00 内也只告警)。

### H.4 落档面

本文件 + `handoff.md` 变更日志 + `docs/CONTROL-LOOP-LEDGER.md` §2 新增一行 (告警汇总的轮转覆盖环路，含误差信号与 `[覆盖]` 读数面)。**未改** `AGENT.md` (军规层无新增：本批是既有军规「单位/量纲声明」「消费方登记制」「基数自检」的**执行**，不是新立规矩)。

---

## §I 待用户裁

1. **F-1 / F-5 (`pipeline.py` 措辞与去重键)**：是否等并行 Agent 收口后随批带走？还是现在单独立案？
2. **F-2 / F-3 (skip_reason 清空、okr_history 幂等)**：两处都在并行 Agent 在途文件里，是否登记为其收口时的交接项？
3. **F-4 (INFO 行被判成 WARN)**：改判级属改判据，是否要立案走 ADR？
4. **F-7 (Finnhub 429 ×68/日)**：先攒 3–5 日读数，还是现在就查配额与降级链？
5. **F-9 (`alerts` 表 0 行)**：是否授权做五个调用面零引用验证 → 判退役？
6. **§D 重开条件**：`horizon_confidence` 归一化 (改名 `horizon_margin` 或分列存两基) 是否等 9/30 冻结窗结束后一并处理？
7. **F-12 (`except_pass` 棘轮既有红 955→960)**：是否要求 `1e296430`(批D) 与 `f3a880e6`(RFC-079) 的作者按 R5 把 5 处 `except:pass/continue` 改成 `logger.warning` 留痕复绿？还是授权本 Agent 代为清剿 (代价：会碰并行 Agent 刚提交的 `app/playbook/{exit_engine,overnight_decision,pipeline}.py` 与 `app/web_dashboard/live_ledger.py`，且清剿后须把 BASELINE 从 955 调低到实测值 —— 属改他人批次的判据面)？**不处置的代价**：该钉子会一直红，后续任何跑全量/定向 pytest 的 Agent 都要重新做一次「这红是不是我引入的」归因 (本批已花一轮)。
