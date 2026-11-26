---
title: DA 信息输入与推理链路查勘 —— 三处 P0 实测断裂 + 载体验证空洞 + 一处预注册自证伪
status: active
module: cross
related_code: /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py
related_okr: OKR-2026-11
tags: [da, llm, prompt, input-hygiene, shadow, governance, verification-gap]
updated: 2026-11-20
---

# DEB-20261119: DA 信息输入与推理链路查勘

> **状态**: 立案完成 · **§5.1 三项 P0 全部落地**（P0-2 OKR 口径更正 / P0-3 改臂不改判据 / P0-1 测试修复 **154 passed 0 failed** + cron **已装机并原样实跑验证 rc=0**，用户裁「执行1-3」→「授权安装」）；P1/P2 待裁
> **日期**: 2026-11-19（ET）立案 / 2026-11-20 执行批落档
> **性质**: **纯只读查勘 + 落档 + 测试修复。零生产代码改动、零重启、零行为变更、零付费调用。**
> **触发**: 用户 2026-11-19「我想查勘 da 基于信息输入的推理逻辑和链路，是否合理高效」
> **关联**: DEB-20261118-da-input-hygiene-preregistration、DEB-20261117-da-analyst-qwen4max、
> DEB-20260907、DEB-20260825 第14轮、DEB-20261106 §30.1/§31.2、判据字典 C12/B23、RFC-215

---

## 0. 结论（先给判定，再给证据）

| 轴 | 判定 | 决定性依据 |
|---|---|---|
| **推理逻辑合理性** | **合理** — 工程纪律远超常见水平 | 8+ 轮有据 prompt 修订：删心理锚、反凑数（反方允许 0 条）、删零消费自评、反 Goodhart 证伪官、事实/推断/不确定三分 + 数字溯源核验（§2.9） |
| **链路效率** | **不高效，且已穿透死线** | 死线余量 10.6min → 12.3min → **0（11-19 rc=124 timeout，砍掉 2 只候选）**（§2.5） |
| **有效性杠杆** | **仓内已终局裁决：加深分析不是杠杆** | C12 恒真红线，`confidence` eta²=**0.07%**（§4） |

**核心重框**：本次查勘**没有找到"推理不够深"的证据**，找到的三类真问题是——

1. **一个已成功采集的硬数据维度，100% 静默断裂 ≥4 个月**（§2.1，键名语义碰撞；门历史触发 **0/732**、跨 23 交易日；`llm_synth.py:624` 与 `deep_analyzer.py:304` **两条消费路径同门同瞎**）；
2. **生产载体 3,427 行在所有验证门之外**，155 个契约测试零人跑，一条**日历自腐**的红测试躺了 25 天无人发现（§2.2）；
3. **已预注册的输入卫生实验，其对照臂与现役行为相同 ⇒ 保证产出 null，并会按预注册判据永久关闭该议题**（§2.3，字节级吻合实证）。

三条都**不是**"分析质量"问题，因此都**不触 C12**。

---

## 1. 查勘方法与边界

**方法**：主线 + 三路只读子查勘（输入面测绘 / 推理逻辑审查 / 效率成本实测，其中效率轴由两个独立 agent 各跑一遍、结论互相印证）。全程 `sqlite3 'file:...?mode=ro'` + JSON 只读；未写仓内文件、未改代码、未重启、未调用任何付费模型。

**测试隔离已验证**：跑 skill 侧 155 测试前后做完整性快照 —— 生产库行数不变（`trade_records` 1264 / `playbook_instructions` 5236 / `lessons` 580）、skill `data/` mtime 不变 ⇒ 隔离有效。结果 **1 failed / 153 passed / 1 skipped in 12.14s**（§2.2）。

**载体定位（前提更正）**：仓内 DA 三件套是**死代码**——
- `app/llm/prompts/deep_target_analysis.py:3-7` 自述零调用；唯一发射点 `app/plan/deep_analysis_engine.py:488`，上游只有 `web_dashboard/routes/battle_plan.py:637`；
- 实测 `llm_audit.db`：`feature='deep_target_analysis'` **2 行，最后 2026-07-25 11:46:45**；`app/plan/analysts/` 四个子分析师仅被该引擎调用 ⇒ 同死；
- **生产真身**：`/root/.hermes/skills/deep-stock-analysis/scripts/{pipeline.py, data_collector.py(1474行), llm_synth.py(3427行), signal_gate.py}`；prompt 真身 = `llm_synth.SYSTEM_PROMPT`(:211) + `_format_data_for_prompt`(:434-1137) + `effective_user_template`(:1481)。

**核不到的（不编）**：① Finnhub 4 槽信号量的实际等待时长（未跑计时，生产在跑不敢压）；② qwen4-max 美元成本（无公开挂牌价/账单）；③ 社区讨论 22.4% 投入的方向性贡献（DEB-20261118 Layer 1 合规挂起至 12 月，未跑付费回放）；④ borrow/做空利率是否**应该**进 DA 输入面（只核到它**不在**，未核到有无议题讨论过要不要加）；⑤ 全链 429（对齐日志无 LLM retry/429，但 Finnhub 成功重试无日志 ⇒ 未能验证）。

---

## 2. 实测发现

### 2.1 P0-A · 资金面维度 100% 静默断裂（键名语义碰撞）

`llm_synth.py:624` 的富分支门写的是：

```python
if isinstance(cap_dist, dict) and "_error" not in cap_dist and cap_dist.get("code") == 0:
```

但真实 payload 是 `{"code": "US.AMD", "capital_in_super": 289344323.207, ...}` —— **`code` 是符号名（str），不是状态码（int）** ⇒ 门恒 `False`。

| 实测项 | 值 |
|---|---|
| **该门历史触发次数（全量 raw 扫描）** | **0 / 732**，跨 **23 个交易日**（2026-10-20 → 11-19）；`code` 取值分布 = `str:<symbol>` **731** / `None` 1 ⇒ **从未有过 int 0** |
| **bug 年龄** | **≥4 个月** —— 该门出现在**现存最老的 `.bak`**（`llm_synth.py.bak-20260718_124329`，2026-07-18 12:43:29）；23 日只是 checkpoint 留存窗，不是故障窗 |
| **受害消费方** | **两个，不是一个**：`llm_synth.py:624`（LLM prompt 路径）**与 `deep_analyzer.py:304`（技术面/深度分析路径）携带逐字相同的门** ⇒ **没有任何一条路径看得到这份数据** |
| 11-19 走瘦兜底 `资金面 (分时)` | **36/36**（`## 资金面` 段 36/36 有渲染，但渲染的是兜底文案） |
| 11-19 渲染出 `- 最新主力流入: N/A` | **36/38 raw**（`capital_flow.data[-1].main_in_flow` 字面就是 `"N/A"`） |
| 已成功采集但 **0 进 prompt** 的数值字段 | **8 个**（`capital_in/out_super`、`_big`、`_mid`、`_small`；实测 AMD/AMZN 均 8 个非零数值键） |
| 独立复核（另一 agent，跨三日 66 checkpoint） | **66/66 都有 8 个非零数值字段，全部未进 prompt** |
| 该段占 prompt 字符 | **0.2%** |

**范围界定（避免过度指控）**：`data_collector.py:175/190/202` 也有三处 `r.get("code") == 0`，但那三处读的是 **HTTP JSON API 响应**（`_curl_json` → `NEWS_BASE/news_search`），`code` 确实是状态码 ⇒ **那三处正常**（实测 11-19：`## 最新新闻` 36/36、`## 研报` 36/36、`## 社区讨论` 31/36 均渲染）。**本缺陷专属于 Futu OpenD 脚本 payload 形状**（`get_capital_distribution.py --json` 把符号名放在 `code` 键），共 2 处。

**为什么没有任何门能发现它**：`_source_status` 只覆盖 4 键（news/news_v2/research/sentiment，`data_collector.py:135`），其余 **12 源返回空 dict 也计 `ok`** ⇒ `data_quality.coverage_pct` 11-18 **31/31 恒 100.0**、11-19 35/36=100.0，prompt 头部照样写"19/19 维度有效"。**采集层报成功、呈现层丢光、质量审计报满分、两条消费路径同门同瞎** —— 四层互不校验，且 4 个月无人发现。

复现：
```bash
cd /opt/ftseek/FTSeek-US && ./venv/bin/python -c "
import json,glob
fs=sorted(glob.glob('data/da_checkpoint/2026-11-19/*.raw.json'))
print(sum(1 for f in fs if '最新主力流入: N/A' in (json.load(open(f)).get('user_prompt') or '')),'/',len(fs))"
```

### 2.2 P0-B · 载体验证空洞：155 契约测试零人跑，红测试日历自腐 25 天

生产 DA 载体在 **git 仓外**（`/root/.hermes` 下无 `.git`）：3,427 行 `llm_synth.py`、17,944 行 Python、21 个测试文件 / 155 个测试、版本史 = **105 个 `.bak-*` 拷贝**。

| 覆盖门 | 是否覆盖 skill 载体 |
|---|---|
| `check_undefined_names.py`（本批抓到 4 个 NameError 的那道门） | ❌ |
| pre-commit hook | ❌ |
| G-02 自动部署断层检测 | ❌ |
| 启动冒烟 `startup_smoke_check.py` | ❌ |
| 仓内 9000+ 测试 | ❌ |
| **cron 跑 skill pytest** | ❌ **`crontab \| grep -i hermes \| grep -c pytest` = 0** |

**唯一红测试的真相（我两次归因错误后的实测版）**：

```
tests/test_prompt_source_truth.py::test_prev_close_apply_changes_snapshot_and_prompt
  assert out["snapshot"]["prev_close_price"] == 32.69   E  assert 32.91 == 32.69
```

实测诊断（非推断）：
```
fixture: prev_close_bar_date='2026-10-21', prev_close_price=32.91, bar_close=32.69
判定:    prev_close_reconcile = {'mode':'apply', 'status':'stale_bar',
              'implied_prev_close':32.6899, 'implied_vs_bar_pct':-0.0004}
        prev_close_delta_pct = 0.673
```

- `implied_vs_bar_pct = -0.0004` ⇒ **交叉验证完美通过，修正数学是对的**；
- 拒绝原因是 `prev_close_reconcile.py:104` 的 `gap_days > 4` ⇒ `status='stale_bar'`，保守不覆盖（**行为正确**）；
- `today` 取**真实 ET 当日**（`prev_close_reconcile.py:45-50`），而 fixture 的 bar 日期**硬编码 2026-10-21** ⇒ 10-23 写测试时 gap=2 **绿**；**10-26 起 gap>4 ⇒ 红至今 25 天**；
- 生产 kline 每日新取（gap=1），**不受此分支影响** ⇒ **这不是生产回归，是测试自腐**。

**⇒ 真正的洞不是"生产昨收口径坏了"，而是：这套 155 个契约测试目前提供的是零保证。** 它既不能证明生产对、也不能证明生产错；一个日历自腐的红测试躺 25 天无人发现，而且它把本次查勘的主线**带偏了两次归因**（§3）。凡在此载体上声明"测试通过"的结论，在 §5-P0-1 落地前**不可采信**。

### 2.3 P0-C · DEB-20261118 Layer 1 对照臂 = 现役 ⇒ 保证 null，会错误关闭议题

DEB-20261118 §6.2 预注册的两臂：

> **两臂**：A = 现役渲染（原样）；B = 去重后渲染（**仅按 id 去跨栏目重复**，不动时效）
> **人群**：2026-11-17 的 35 个 checkpoint，其中 **28 个有重复**、30 个有陈旧

而 §1 登记的冗余件数 = **97**。实测生产 prompt：

| 日 | raw 含 prompt | 带「去重N条与新闻同稿」注记 | **累计已去重条数** | 研报全同源被省略 |
|---|---|---|---|---|
| 11-17 | 35 | **28** | **97** | 0 |
| 11-18 | 31 | 28 | 111 | 1 |
| 11-19 | 36 | 31 | 128 | 1 |

**28 = 28，97 = 97，字节级吻合。** 即：DEB-20261118 §1 数的 97 件是**采集层**重复，而**渲染层早已按 id 去重并把这 97 条剔除**，还在 prompt 里明写注记告诉模型。实现位置 `llm_synth.py:685-750`：`_seen_source_keys` 以 `news` 全量播种(:685) → `news_v2` 段查表打 `[与新闻同稿]` 标记(:706) → `research` 段查表**整条省略**(:736) 并输出 `去重{N}条与新闻同稿`(:750)。回滚旋钮 `DA_SOURCE_TRUTH_FIX` 默认 `"1"`(:436)，实测 `.env*` / `/root/.hermes/cron/jobs.json` **均未关闭**。

**⇒ B 臂（按 id 去跨栏目重复）== A 臂（现役）。** 而 §6.2 的判据是读数前写死的：

> 若 ①②**双双为 0/35** 且 ③ 配对差中位 = 0 ⇒ **判定输入不是因果，本议题关闭**，不进 Layer 2、不改生产。

⇒ 按原文开工，会花 **$90–130**（140 次付费调用）买一个**同义反复的 0/35**，然后**按预注册判据永久关闭输入卫生议题**。§5 的触发窗是 **2026-12-07 ~ 12-21**（post 子窗 n≥120），**今天 11-19，还有时间修**。

**修法（改臂，不改判据 —— 改判据=重裁）**，两个候选，均零代码至开工：
- **B′ = `DA_SOURCE_TRUTH_FIX=0`（未去重渲染）**：用现成回滚旋钮，A/B 真有差异，回答"**已部署的去重机制本身是否有效**"。若 0/35 ⇒ 该机制惰性，议题可**凭真实证据**诚实关闭，而不是凭同义反复。
- **B″ = 时效臂（>7 日陈旧剔除）**：测**真有方差**的那一轴（陈旧 323 件 / 26.15%，其中 research 185/312=**59.3%**；本次三路复测 11-19 仍 ~25%，持续存在）。DEB-20261118 自己已把时效排为"第二批"。

### 2.4 P1 · 性价比倒挂：最贵的位置放了最软的料

prompt 实测体量（11-19，n=36，总 1,258,124 字符）：**均值 34,948 字符**（p50 34,555 / max 49,803）。
⚠️ **DEB-20261118 引用的"16–19k"已过期约 1.9×** —— 该数字是 `DA_INPUT_FULL=1`（11-17 生效）之前的口径。

按字符占比（26 段）：

```
社区讨论 22.4%(31/36) ← 第一大输入，富途社区帖，无时效门
技术面   12.6%        Finnhub新闻事实流 8.9%   大盘环境·契约 5.3%
近期交易经验 4.5%     最新新闻 3.9%            异动信号 3.2%
...
研报      1.4%  ← 实测 59.3% >7日陈旧
资金面    0.2%  ← 本该是硬数据，36/36 全 N/A（§2.1）
数据源状态 0.0%(5/36) 财报日历 0.0%(2/36)
```

`DA_INPUT_FULL=1` 把增量**配给了最弱证据源**：社区帖 5帖×120字 → **30帖×800字**（`llm_synth.py:773-774`，一跃成第一大输入）；研报 采5/渲5 → 采10/渲10（把陈旧率 59.3% 的源翻倍）。硬证据（资金面）反而全断。

**财报日历反向断裂**：36/38 采到 `_note="internal calendar healthy; no earnings in lookahead window"`，但空窗时**整段不渲染** ⇒ 模型无法区分"近期无财报"与"财报源坏了"，而这是 `SYSTEM_PROMPT` 风险日历条款（`llm_synth.py:244`）明写要用的输入。

**顺带纠正落档口径错（范围经复核后缩小）**：`DA_INPUT_FULL` 实际只管 **6 项**（呈现层 5 + 采集量 3 + 摘要截断 1，落在 9 个代码点），**不是 9 项**。`DA_THINKING_BUDGET`(**8192**→0)、`DA_UNCAPPED_MAX_TOKENS`(→131072)、`DA_ANALYST_TIMEOUT`(→900) 是**三个独立旋钮、各自回滚**，不受 `DA_INPUT_FULL` 控制。

- **真正错的只有一处**：`OKR-2026-11.md:214`「`DA_INPUT_FULL=1`（9 项输入量解除）」把三项独立旋钮并入该开关 ⇒ **已更正**（§9）。
- **我初稿的另一处指控是错的**：`handoff:296`（本批插入后行号移至 **:308**）原文写「解除 **9 项能力限制**」+「**④–⑨ 统一开关 `DA_INPUT_FULL`**」—— **它本来就分得对**（9 项是 R1/R2 合计解除数，6 项归该开关）。`DEB-20261117:135/:175` 同样写「#4–#9」，**也对**。⇒ 我先前说"两处口径错"是**过度指控**，已登记为 §3 第 7 条。
- **另核出一个新误记**：`DA_THINKING_BUDGET` 改前默认值 = **8192**（亲读 `config.py.bak-llmmodel-qwen4max-20261117:151` = `int(_read_env(...) or "8192")`；`.env*` 无覆盖；现 `config.py:176` 回滚注亦写「加 `DA_THINKING_BUDGET=8192`」）⇒ **OKR 的「8192→0」正确**，而 `handoff:308` 的「**16384** 硬帽」与效率子查勘沿用的 16384 均为**误记**（疑与 `DA_UNCAPPED_MAX_TOKENS` 旧值 16000 混淆）。该条属**他人历史条目**，按 DOCS-GUIDE §4「不静默改写历史」**不改原文**，在此登记 + 在 OKR 更正注旁挂账。影响面：11-18「11/31 候选思考 ≥16000，旧帽会截断」的**结论方向仍成立**（旧帽 8192 只会截得更早），但**引用的帽值数字错**。

### 2.5 P1 · 效率：死线余量已穿透，门禁跑在花钱之后

| 日 | p50/p90/max 延迟(s) | 墙钟(ET) | 距 08:57 死线 | 总token | 分析→指令→BOOKED | **BLOCK 候选烧掉的 token** |
|---|---|---|---|---|---|---|
| 11-17 | 339/435/524 | 08:01→08:46:26 | **10.6min** | 2.26M | 35→14→6 (17%) | **1.32M = 59%** |
| 11-18 | 367/442/562 | 08:01→08:44:43 | **12.3min** | 2.09M | 31→17→7 (23%) | 0.84M = 40% |
| 11-19 | 394/483/557 | 08:01→**08:56:59 rc=124 timeout** | **≈0（撞线）** | 2.48M | 36→20→11 (31%) | 1.01M = 41% |

- **p50 逐日 +8%**（339→367→394），候选数 31→36 ⇒ **不是单候选失控**（max 557s < 900s 超时），是**总量失控**。11-19 `pipeline_timeout_finalized`，raw 采集 38 只、**US.BE / US.GEHC 两只分析被砍**（无 checkpoint），靠 finalize 从 36 个 checkpoint 补写 20 条指令。
- **门禁在 LLM 之后才跑** ⇒ **40–59% 的 token 花在算完就被扔掉的候选上**；每 BOOKED 摊 **22.5万–37.5万 token**。挑战者占 35% token。
- **采集侧不是瓶颈**：8 worker 真满载（峰值重叠恰=**8**，时间加权 6.96/6.79，无 >5s 空洞）；429 三日 **0**；采集失败 **1/684 源次**（11-19 US.META 19 源 ok=18）；输入重复放大仅 **22%** 共有前缀（7,533 字符），76% chunk 逐候选唯一 ⇒ **重复放大不是主要成本**。
- **真串行瓶颈在发车头部 12–16min**（scanner/preflight/UE 供料/首批采集）= 墙钟 **1/3**。
- **抬 `DA_MAX_WORKERS` 也不会提速的那一段**：`_FINNHUB_SEM = Semaphore(4)`（`data_collector.py:651`，env `DA_FINNHUB_MAX_CONC`）—— 每符号 7 个 Finnhub 任务 × 8 并发候选 = **56 任务挤 4 槽，全局串行化 14:1**。
- 缓存命中 26.7%/25.3%/25.4%（**主调用口径，与文档一致**）；全链口径 20.7%/19.6%（两口径不矛盾，不可混称）。共享行情 TTL 600s + 行业 peer TTL 1800s + `_MC_LOCK`（注释记"35只=105个OpenD子进程白扇出"）⇒ 供给端治理到位；但 10min TTL 对 45min 整轮 ≈ 重取 4–5 次，且每日首批 8 个 `market_context` 均不同（cold-miss）、重复 profile 调度 110/97 次。
- **单位经济**：DA 净期望 **−16.51bps/笔**（CI80 不含零）⇒ **任何正的 LLM 成本都已超过它产生的毛利**，负毛利下成本比较恒成立、无需单价。量级参照：日均 2.1–2.5M token ≈ **30M token/月**。

### 2.6 P1 · 跨语言同事件去重结构性不可达

去重链覆盖 news→news_v2→research 三段（§2.3），但 `[与新闻同稿]` 标记在三日 **102 个 prompt 中触发 0 次**，而两段**并列渲染** 36/36（11-19）、29/31（11-18）。

原因：`_source_key`(:389) 优先 `news_id`、其次 URL id/规范 URL、最后标题。富途中文新闻与 Finnhub 英文新闻 **id 不同、URL 不同、标题不同语言** ⇒ 键永不碰撞。

⇒ **唯一存活的重复轴，正好是同一事件以两种语言从两个供应商到达的那一条**，而 prompt 里无任何"这两段可能同源"的提示。严重度**中**（两段功能有别：`news_v2` 段头明写"新闻类论据必须引用此处事实"= 引用权威源，`news` 是中文标题流），但它与 DEB-20261118 §31.2 的担忧同构（一份证据被当成两个独立来源 ⇒ 虚增催化剂类目表观证据量）。**修它 = 改模型输入 = L2 行为变更，须预注册。**

### 2.7 P2 · 输出面 write-only 群已成系统性（军规"无消费方字段一律删"未在 DA 侧执行）

| 字段 | 实测 | 消费方 |
|---|---|---|
| `strategy_hint.recommended_exit` | `SL_TRAIL_LOCK` **11-17 35/35、11-18 31/31、11-19 36/36 恒常量** | `grep -rn "recommended_exit\|recommended_hold" app/ scripts/` = **0 命中** |
| `strategy_hint.recommended_hold` | `mid(2-12h)` 三日 100% 恒定 | 同上 0 命中 |
| `matched_patterns_json` | **NULL 285/285**（写侧 `instruction_builder.py:561-563` 硬编码 `[]`） | 仅 `review_orchestrator.py:80` 的**注释** |
| `pattern_verdict` | **'' 285/285**（同上硬编码 `None`） | 同上 |
| `horizon` | `'short'` **283/285 = 99.3%**（`:588` 默认值） | — |
| `llm_self_conf` | 三日**恒 u=2**（{0.8, 0.6}） | 仅离线报表 |
| `confidence`（顶层） | 三日**恒 u=2**（{中, 中高}） | 仅离线报表 |
| `horizon_confidence` | **3 条 >1.0**（1.5×2、2.0×1）；174 条恰 0.35；14% <0.01（最小 1.5e-05）；写侧 `:589` **无 clamp** | — |
| `_ue_origin` / `_ue_short_check` / `_ue_transmission_confirmation_required` | 11-19 **36/36 恒 False / 空 dict / 恒 False**；UE 供料实际仅覆盖 **2/36** 候选 | **0 消费方** |

**基数门设计上抓不到这类塌缩**：`scripts/shadow_field_cardinality.py:48` 的门槛是 `min_unique: 2`，而 `llm_self_conf`/`confidence` **恰好 u=2 擦边通过**。W2 棘轮（`daily_invariant_check.py::_check_field_cardinality`）同理。⇒ **"恒二值"型塌缩在当前所有门下均隐形**。

`recommendation_evidence` 36/36 存在但无 live 消费方（`da_recommendation_evidence_replay.py:46` 是**重算**不读落盘字段，SKILL.md 自述"不参与 prompt、Signal Gate、admission、仓位或执行"）—— 属**已声明的 record-only，非违规**，登记以完账面。

### 2.8 P2 · 仓内死文件与生产纪律方向相反

`app/llm/prompts/deep_target_analysis.py:96` 写着「你的方向判断历史命中率约50%(≈掷硬币)」+ `:23-107` 的 `_calibration_block()`（近30日净盈亏注入 prompt）。

而生产 `llm_synth.py:204` **明写已删除该锚**：

> 删除"命中率约50%"全局心理锚（**均匀降档压力源, 与 7.0 塌缩同族机制**）—— DEB-20260907 第5项

且 `llm_synth.py:196` 注明"修订考古只留代码注释，不进 prompt 文本；**复述进 prompt 会重新注入锚点**"。

⇒ **死文件与生产纪律方向相反。** 下一个 agent 若照死文件"恢复校准纪律"，会把已治好的病再注入一次。按军规应删（须走五面零引用核查，尤其 Hermes prompt 文本面）。

### 2.9 推理逻辑：做得好的部分（有据，不是溢美）

- **反凑数**：DEB-20260907 把"多空各至少2条"改为**合计 2-6 / 方向侧 ≥2 / 反方允许 0 条** —— 显式取消填充压力；
- **反 Goodhart**：证伪官**删除 `my_score−1` 惩罚**（那是低报诱导），允许 SKIP，并新增 `falsification_target` / `flip_condition`（AGREE 时强制填，防仪式化）；
- **删零消费自评**：USER 模板的 confidence 五级自评被删，理由是**零门消费**（quality_score 链不消费它，tier 只用 score + 辩论一致性，`signal_gate.py:94` record-only），换成 `evidence_gaps[]` 并带护栏"SKIP 率升幅 ≤10pp"；
- **单一实现**：删掉 prompt 里的假 SL 拦截规则，把方向性硬门移到规则层（`pipeline.py B_long_knife` / `chase_cap`），避免两处实现漂移；
- **事实/推断/不确定三分**：`## 论据溯源清单`（`_extract_source_numbers`:1259，"LLM输出必须引用这些，不得编造"）+ `_verify_sourced_numbers`:3276 + `_audit_claims`:1553 + `_reclassify_claim`:1545 + `evidence_gaps[]`；数据源状态段区分 `FAILED(采集失败; 不等于无新闻, 禁止写成"没有催化剂")` / `EMPTY(源成功但无结果)` / `DISABLED`（:526-534）—— **优于仓内死文件**；
- **矛盾并置处理得当**：`## 标的基本面` 的成交量/换手率/量比是上一交易日口径，prompt **自带 ⚠️ 说明**（实测 AMD 段有）。

**真缺陷在"反证被吞掉"，不在"想得浅"**：
- **挑战者正确预测了 ELV 会在失效位之前被扫损，系统照样进场，ELV −$326.68**；
- 弃权（SKIP）语义 + 抬分机制**吞掉反证**；
- 排序**不看** RR / pattern / claim-IC / 历史；
- **25.4% 渲染项 >7 日陈旧**，且 news/research/sentiment **三源无时效门**（`news_v2` 有 7 日硬窗，其余没有）。

---

## 3. 更正登记（本次查勘主线自己犯的错，全部实测推翻）

| # | 我先前的说法 | 实测真相 | 教训 |
|---|---|---|---|
| 1 | 红测试是"**11-17 批（qwen4-max + DA_INPUT_FULL）破坏了昨收口径**" | `llm_synth.py` mtime 11-17 18:15，但 `prev_close_reconcile.py` **10-23 21:15 后未再改**；真因是 fixture bar 硬编码 2026-10-21 + `today` 取真实 ET 日 ⇒ **日历自腐**，10-26 起红 | 只看 mtime 先后就下归因，没读判定分支 |
| 2 | 改口说"是 **10-23 21:15 的实现改动**弄红的（红 28 天）" | 实现改动与测试同日、且 `status='stale_bar'` 是**保守拒绝**（`implied_vs_bar_pct=-0.0004` 证明数学正确）⇒ **实现没错，测试自腐** | 二次归因仍停在"谁改了文件"，没问"判定为什么是这个分支" |
| 3 | `da_score` / `challenger_score` / `da_instruction.score` 三值化是**塌缩字段，应退役** | `_SCORE_BAND_MAP = {"HIGH":8.0,"MEDIUM":6.0,"LOW":3.0}`(:1511) 是 **DEB-20260825 第14轮 P8 的刻意设计**：要 0-10 分同时告诉模型命中率≈50% ⇒ 收敛到安全中间值 **7.0（131/195=67%）** ⇒ 方差塌缩 ⇒ 合并规则 `\|orig-chall\|>3→SKIP` **结构性死亡（实测 0/107）**。改成"模型数三个可判定锚定条件给档、档→数由代码映射"，恢复 HIGH-LOW=5>3 让辩论覆盖可触发。回滚 `DA_SCORE_BAND=0`。另 `_apply_score_band`(:1521) 把原始数值存 `score_legacy` 供影子对比，`except: pass # 永不阻断主流程` | **三值化是治好了一个更坏的塌缩**，不是塌缩本身。（对 `invalidation_trigger_count`/`tripwire_count`/`confidence_semantic` 的退役判断仍成立） |
| 4 | 生产可能**丢了**死文件里那套"命中率约50%"校准纪律 | `llm_synth.py:204` **刻意删除**，理由落档（均匀降档压力源，与 7.0 塌缩同族） | 拿死文件当规范去核生产，方向反了 |
| 5 | （子查勘）DEB-20261118 的"97 件重复被渲染进 prompt" | **渲染层为假** —— 97 件已被剔除且 prompt 明写注记（§2.3）。但**"陈旧 26.15%"为真** | 采集层计数 ≠ 渲染层暴露 |
| 6 | （子查勘，**已写进本 DEB 初稿后才抓到**）"对照 `deep_analyzer.py:305` 读同一字段**无 code 门** ⇒ 技术面路径用得到，只有 LLM 看不到" | `deep_analyzer.py:304` 携带**逐字相同**的 `dist.get("code") == 0` 门 ⇒ **两条路径都看不到**。真相比我初稿写的更严重，初稿把缺陷范围写小了 | **子查勘的"对照"结论也必须落到源码行**；我在初稿里直接采信了转述，是 §3 前 4 条同一个毛病的第 5 次复发 |

| 7 | "`OKR-2026-11:214` **与 `handoff:296`** 的「9 项」口径错" | **只有 OKR:214 错**。`handoff:296`（现 :308）原文写「解除 9 项能力限制」+「**④–⑨ 统一开关 `DA_INPUT_FULL`**」⇒ **本来就分得对**；`DEB-20261117:135/:175` 同样写「#4–#9」也对 | **过度指控**：把"合计 9 项"误读成"9 项都归该开关"，没读后半句。P0-2 范围因此从 2 处缩到 **1 处** |

| 8 | "约 14 分钟后 **G-02 会自动重启**并顺带上线别人的半成品"（08:55 向用户发的紧迫预警） | **G-02 根本没触发** —— `logs/deploy_freshness.log` 实证 08:39:01 / 08:49:02 / 08:59:02 三次全部 `skip: stale_drift=37547s/38089s/38161s_outside_safe_window`。09:00:23 那次是**对方自己的受控重启**（其 `DEB-20261120-mu-exit-protection-and-state-hygiene.md` 明写「已决策·已部署，受控重启，冒烟 7/7」，触发源 = 用户直接令该 session 修 MU 出场）⇒ **用户授权的正常部署，非盲重启** | **观测对、推断错**：drift 非空与"文件正在被写"都是实测事实，但我**凭记忆里的 G-02 规则推断行为，没读它自己的执行日志**。与第 1/2 条同族（读规则而不读实际执行证据） |

**方法论结论（比单条更正重要）**：本次 8 条更正里 **7 条源于"读声明/转述/规则而不读判定分支、源码行、原文或执行日志"**（第 4 条另因：拿死文件当规范去核生产，权威源选错）。在一个 3,427 行、105 个 `.bak`、零 git、零 CI 的载体上，**任何基于 mtime / 文档 / 注释 / 子查勘转述 / 记忆中的规则的归因，都必须落到"实际执行走了哪个分支、那一行源码写了什么、那句原文的完整上下文、那个机制自己的日志说了什么"才算数**。这也是 §5-P0-1 排在第一位的理由。

**本 DEB 的自查口径**：§2.1（0/732 + 双路径同门）、§2.2（判定分支 `stale_bar` + `implied_vs_bar_pct`）、§2.3（28/97 字节级吻合 + `DA_SOURCE_TRUTH_FIX` 默认值 + 三段去重链源码）三处 P0 证据**均由主线亲自复跑/亲读源码**，不采信转述。§2.4/§2.5/§2.7 的量化读数来自子查勘实测，主线抽样复跑了 prompt 体量与段落渲染率（36/36、31/36）但未逐条复算 token 账。

---

## 4. C12 边界：为什么本 DEB 不提"加深分析"

判据字典 **C12**（2026-10-02，n=572，dead base rate 26.0%）是**恒真红线**：

> **不得以「加深 DA/UE 分析质量」作为降低「入场即错」的手段**

实测：8/8 轴被预注册判据否掉（G3 eta²≥5% / G4 \|t\|≥2 / G8 max bucket ≤50%）——
- `confidence` eta² = **0.07%**，四档死单率 **23.1 / 26.3 / 26.2 / 26.3%** ⇒ **零梯度**；
- Group A（分析产物）max eta² **2.29%** / median 1.10% vs Group B（市场状态）max **2.12%** / median 1.03% ⇒ **不可分辨**；
- ⇒「**加深分析不是杠杆**」。

**本 DEB 的三条 P0 均不触 C12**，因为它们修的不是"分析深度"：
- P0-A 修的是**数据搬运断裂**（采到的硬数据没送到模型面前），不是让模型想更多；
- P0-B 修的是**验证所有权**（谁跑测试），与模型行为无关；
- P0-C 修的是**实验设计的有效性**（避免买一个同义反复的 null），是省 $90–130 并防止错误关闭议题。

同理，`DEB-20261106 §30.1`（入场前 24 个特征全在检出下限以下）⇒ 本 DEB **不得**把去重/时效/资金面修复做成任何**入场门或打分特征**；`B23`（逐笔净 bps sd≈121 ⇒ 检出 10bps 需 2,302 笔 ≈118 个月）⇒ **净 bps 不得作为本议题任何一臂的判据终点**。

---

## 5. 处置路线

### 5.1 零行为 · 建议即刻（不改任何模型输入、不改任何执行）

| # | 动作 | 成本 | 为什么是 P0 |
|---|---|---|---|
| **P0-1** | **给 skill 载体建最小验证闭环**：一条 cron 跑 155 测试（只读、不推生产、失败只落文件不告警刷屏），**先修日历自腐类**（冻结 `today` / 冻结样本窗，同 RFC-123 附F.5 的 `--until` 冻结纪律）让红绿重新有意义 | 极低（纯治理接线，无 `app/` 改动 ⇒ **不触 G-02、不需重启**） | **在这条落地前，DA 侧任何"测试通过"的声明都不可采信**；本次它已把主线带偏两次（§3） |
| **P0-2** | 文档更正：`DA_INPUT_FULL` **9项→6项** —— **仅 `OKR-2026-11.md:214` 一处**（`handoff:308` 与 `DEB-20261117:135/:175` 原文已正确，**不改**，见 §3 第 7 条）；附注三个独立旋钮各自回滚 + `DA_THINKING_BUDGET` 改前真值 **8192**（非 16384） | 极低 | 口径错会让下一次回滚**回滚错东西**（以为关一个开关能退回全部九项） |
| **P0-3** | **改 DEB-20261118 §6.2 的 B 臂**（B′ 未去重 / B″ 时效臂，§2.3），**判据一字不改** | 零代码（纯预注册文本） | 触发窗 **12-07~12-21**；不改就会花 $90–130 买一个保证的 null 并**永久错误关闭**输入卫生议题 |

### 5.2 须落档 + 预注册后才能动（都改模型输入 = L2 行为变更，影子先行）

| # | 动作 | 为什么必须预注册 |
|---|---|---|
| **P1-1** | 修资金面门（§2.1）—— **两处同修**：`llm_synth.py:624` + `deep_analyzer.py:304`；只修一处等于没修（另一条路径仍瞎） | 修它会把 **8 个新数值字段送进 prompt** ⇒ 改变模型输入 ⇒ 改变输出。**虽然是 ≥4 个月、0/732 触发的明显 bug，仍不得直接改**（一次一变量 + 影子先行）。且须同批修 `_source_status` 覆盖面（12/16 源无状态 ⇒ coverage_pct 恒 100.0 是假满分），否则修完仍无门可发现同类断裂 |
| **P1-2** | news/research/sentiment **时效门**（治 26.15% 陈旧 + research 59.3%） | DEB-20261118 已登记为"信号源大改"（过滤后总量 1235→831 = −32.7%、research 312→46 = −85.3%），必走 DEB-20261106 §30.6 A/B 预注册。**且当前被 §4 排序约束合规挂起**（R2 现役变量未读数） |
| **P1-3** | **门禁前移**：把可在 LLM 前判定的硬门（`B_long_knife` / `chase_cap` 几何）移到 LLM 之前，省掉 40–59% 的 BLOCK token | 可**先离线回放证明"前移不改变最终 BOOKED 集合"**；若证明成立 ⇒ **零行为省钱**，不必预注册行为变更；若不成立 ⇒ 退回 L2 流程。这是本次唯一"可能既省钱又零行为"的一项 |
| **P1-4** | 跨语言同事件去重（§2.6，`_source_key` 语言绑定） | 改渲染 = 改输入。严重度中，排在 P1-1/P1-2 之后 |

### 5.3 P2 · 单独排期

- **删死文件** `app/llm/prompts/deep_target_analysis.py` + `app/plan/deep_analysis_engine.py` + `app/plan/analysts/`（§2.8）—— 须走**五面零引用核查**（仓内 import / crontab / systemd / **Hermes prompt 文本 `/root/.hermes/cron/jobs.json`** / `.git/hooks`）；
- **write-only 字段群**（§2.7）—— 同样须五面核查；**注意 `strategy_hint.recommended_exit` 是 skill 侧写、仓内零读，跨仓删除须两侧都核**；
- **基数门升级**：`min_unique: 2` 抓不到"恒二值"塌缩 ⇒ 建议加"最大类占比"维度（如 top1 share >90% 即报），否则 §2.7 那一整类永远隐形。**这是改门不是改行为，但仍须先落档**（改门=改判定口径）。

---

## 6. 明确不做

1. **不加深分析、不加 prompt 条款、不新增分析维度** —— C12 恒真红线（§4）。
2. **不把去重/时效/资金面修复做成任何入场门或打分特征** —— DEB-20261106 §30.1。
3. **不用净 bps 作为本议题任何一臂的判据终点** —— B23 功效实测不可行。
4. **不重开已闭合类**：不碰 C8/C9/C11/C14/C15/C16/C17/C18；不改任何已预注册阈值（含 DEB-20261118 §6.2 判据本身）。
5. **不在 R2（qwen4-max + DA_INPUT_FULL）读数前改输入** —— 一次一变量；R2 于 11-17 生效，是本 cohort 现役变量（DEB-20261118 §4）。
6. **不抬 `DA_MAX_WORKERS`** —— 已满载（峰值重叠恰=8），瓶颈在 `_FINNHUB_SEM=4` 与发车前置串行段（§2.5）。抬它只会在死线上多撞一次。

---

## 7. 可复现命令

```bash
# §2.1 资金面断裂（36/38）
cd /opt/ftseek/FTSeek-US && ./venv/bin/python -c "
import json,glob
fs=sorted(glob.glob('data/da_checkpoint/2026-11-19/*.raw.json'))
print(sum(1 for f in fs if '最新主力流入: N/A' in (json.load(open(f)).get('user_prompt') or '')),'/',len(fs))"

# §2.1 该门历史触发次数 = 0/732（跨 23 交易日），且 code 恒为符号字符串
cd /opt/ftseek/FTSeek-US && ./venv/bin/python -c "
import json,glob,collections
c=collections.Counter()
for f in glob.glob('data/da_checkpoint/2026-*/[A-Z]*.raw.json'):
    d=json.load(open(f)); cd=(d.get('collected') or {}).get('capital_distribution') or {}
    if not isinstance(cd,dict) or not cd: continue
    v=cd.get('code'); c['int:0' if v==0 else type(v).__name__]+=1
print(dict(c))"

# §2.1 两条消费路径同门同瞎（skill 侧）
grep -rn 'get(\"code\") == 0' /root/.hermes/skills/deep-stock-analysis/scripts/{llm_synth,deep_analyzer,data_collector}.py
#   ⇒ llm_synth.py:624 + deep_analyzer.py:304 (Futu OpenD payload, 坏)
#     data_collector.py:175/190/202 (HTTP 状态码 payload, 正常)
# §2.1 bug 年龄 ≥4 个月（现存最老 .bak 已含该门）
ls -1tr /root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py.bak* | head -1   # bak-20260718_124329

# §2.2 skill 载体 155 测试（跑前后请自行做 DB 行数 + data/ mtime 快照）
cd /root/.hermes/skills/deep-stock-analysis && /opt/ftseek/FTSeek-US/venv/bin/python -m pytest tests/ -q
crontab -l | grep -i hermes | grep -c pytest      # = 0

# §2.2 红测试判定分支（证明是 stale_bar 而非实现错误）
cd /root/.hermes/skills/deep-stock-analysis && /opt/ftseek/FTSeek-US/venv/bin/python -c "
import sys,os; sys.path[:0]=['scripts','tests']; os.environ['DA_PREV_CLOSE_BAR_RECONCILE']='apply'
import test_prompt_source_truth as T, llm_synth
s=llm_synth._apply_prev_close_fix(T._collected())['snapshot']
print(s['prev_close_reconcile'], s['prev_close_bar_date'], s['prev_close_delta_pct'])"

# §2.3 去重已在渲染层生效（28/97 与 DEB-20261118 字节级吻合）
cd /opt/ftseek/FTSeek-US && ./venv/bin/python -c "
import json,glob,re
for day in ('2026-11-17','2026-11-18','2026-11-19'):
    fs=[f for f in sorted(glob.glob(f'data/da_checkpoint/{day}/*.raw.json'))]
    n=note=0; dups=[]
    for f in fs:
        p=(json.load(open(f)).get('user_prompt') or '')
        if not p: continue
        n+=1; m=re.search(r'去重(\d+)条与新闻同稿',p)
        if m: note+=1; dups.append(int(m.group(1)))
    print(day,'prompt',n,'带注记',note,'累计去重',sum(dups))"
```

> ⚠️ **checkpoint 路径在仓内**（`/opt/ftseek/FTSeek-US/data/da_checkpoint/`），**不在 skill 目录**。本次查勘主线第一次测量就是从 skill cwd 跑，得到全 0，险些把"去重未生效"当成结论写进本 DEB。

---

## 8. 局限

1. **样本窗仅 3 个交易日（11-17/18/19）** —— 效率与 token 读数的趋势（p50 +8%/日）方向明确但斜率不可外推；死线穿透只有 11-19 一个观测点。
2. **§2.1 的资金面断裂是渲染层实测（36/36、66/66），但"8 个字段进 prompt 后是否改善决策"未测** —— 那是 P1-1 预注册要回答的，本 DEB 不预设方向。
3. **§2.3 的字节级吻合（28/97）证明 B 臂 == A 臂，但不能证明"去重无效"** —— 恰恰相反，去重可能有效，只是**这个实验测不出来**。B′ 臂才是测它的那一个。
4. **§2.7 的 write-only 判定基于 `grep app/ scripts/`** —— 未覆盖 skill 侧内部消费与 Hermes prompt 文本面；删除前必须补五面核查。
5. **美元成本不可算**（qwen4-max 无公开单价/账单）—— §2.5 的单位经济结论依赖"DA 净期望为负 ⇒ 任何正成本已超毛利"这一不等式，不依赖单价。
6. **本 DEB 全部读数来自只读实测，未做任何付费回放** ⇒ 所有"若修则改善"的表述都是**待预注册验证的假设**，不是结论。

---

## 9. 决策与执行记录

**用户裁「执行1-3」(2026-11-20)** ⇒ 执行 §5.1 三项 P0；cron 首次安装被权限分类器拦截，**用户随后明示「授权安装」⇒ 已装机并实跑验证**。**结果：三项 P0 全部落地。**

### 9.1 P0-2 已落地（范围经复核后缩小）

- **改**：`docs/OKR-2026-11.md:214`「`DA_INPUT_FULL=1`（9 项输入量解除）」→「（**6 项**输入量解除 = #4–#9）」+ ⚠️  dated 更正注（三个独立旋钮各自回滚、误读会让回滚回滚错东西）。
- **不改**：`handoff:308`（原 :296）与 `DEB-20261117:135/:175` **原文已正确**（「解除 9 项能力限制」+「④–⑨ 统一开关」/「#4–#9」）⇒ 我初稿的"两处口径错"是过度指控，已登记 §3 第 7 条。他人历史条目按 DOCS-GUIDE §4 **不静默改写**。
- **附带核出的新误记**：`DA_THINKING_BUDGET` 改前真值 **8192**（亲读 `config.py.bak-llmmodel-qwen4max-20261117:151`，`.env*` 无覆盖，现 `config.py:176` 回滚注亦写 8192）⇒ OKR 的「8192→0」正确，`handoff:308` 的「16384 硬帽」与效率子查勘沿用的 16384 均误记（疑与 `DA_UNCAPPED_MAX_TOKENS` 旧值 16000 混淆）。**结论方向不受影响**（旧帽 8192 只会截得更早），但引用数字错，已在 OKR 更正注旁挂账。

### 9.2 P0-3 已落地（改臂不改判据，原文保留）

`docs/debates/DEB-20261118-da-input-hygiene-preregistration.md` 两处 ⚠️ dated 注记：

1. **§1 口径误标更正**：亲读工具源码定位根因 —— `scripts/da_chain_contract_audit.py:251 source_stats(collected, ...)` 读 **`collected`（采集层）**（:389→:394），而 `user_prompt` 只在 :395 被取出**算 sha256 与字符数**（:402-403），**从不参与 items 计数** ⇒ 「1235 条**渲染进 prompt** 的源料」是误标，97 件是**去重前**的数。**仍成立的部分**：陈旧 323 件/26.15%（research 59.3%）**为真**，因渲染层无时效门 ⇒ **本议题的真问题在时效轴，不在同 id 去重轴**。
2. **§6.2 B 臂作废 + 更正为 B′/B″**：原 B 臂 ≡ A 臂（实测 11-17 **28/35 带去重注记、累计恰 97 条**，与该节「28 个有重复」及 §1「97 件」字节级吻合）。**B′（推荐，零新代码）= `DA_SOURCE_TRUTH_FIX=0` 未去重渲染**，命题改为「已部署的去重机制本身是否有效」；**两臂渲染差异已被现存契约测试 `test_source_truth_rollback_restores_old_render` 钉住 ⇒ 臂间差异可零成本证明，不必先花钱**。**B″（备选，第二批）= 时效臂**。
   **判据 / 终点 / n / MDE / 成本口径一字未改**；B′ 与 B″ 不得同批跑（一次一变量）；仍受该 DEB §4 排序约束与 §5 触发窗（**2026-12-07~12-21**）约束；**本更正只改设计文本，不开工、不花钱、不改任何生产代码**。另注 B′ **只覆盖 news↔research 同 id 轴，不覆盖 §2.6 的跨语言轴**，后者若立项须另臂。

### 9.3 P0-1 测试修复已落地（载体红→绿）

- **改**：`/root/.hermes/skills/deep-stock-analysis/tests/test_prompt_source_truth.py::test_prev_close_apply_changes_snapshot_and_prompt` —— 用 `monkeypatch.setattr(llm_synth, "reconcile_prev_close", partial(_pc.reconcile_prev_close, today=date(2026,10,22)))` **冻结窗口**。用的是**生产函数自己的 `today` 参数**，范式与同目录 `test_prev_close_reconcile.py`（8 处显式 `today=`）完全一致；**不 stub 任何类、不改生产代码**。附 4 行注释说明非显然约束（日历自腐 + 本载体无 CI）。
- **验证**：全量 **154 passed / 1 skipped / 0 failed in 4.04s**（修前 1 failed / 153 passed / 1 skipped）。**隔离逐字对上**：跑前后 `trade_records=1264` / `playbook_instructions=5236` / `lessons=587` 行数不变、skill `data/` 近 2 分钟写入文件数 **0**、`scripts/` 近 10 分钟改动 **0**（只 `tests/test_prompt_source_truth.py` 一个文件）。
- **潜伏自腐扫描结论（不盲改绿测试）**：crude 扫描（硬编码日期 + 无冻结关键词）标出 4 个候选（`test_leveraged_etf_filter` / `test_pipeline_finalize` / `test_prompt_execution_contract` / `test_ue_overlap_attribution`），**但精确追踪后均未被证实**：生产侧真实取 `today` 的三处闸（`llm_synth.py:2123` `>7`、`:2144` `<=4`、`:2214` `>7`，分属 `_load_symbol_card_for_prompt`:2093 与 `_load_trading_card_for_prompt`:2188）**没有任何测试调用**（`grep -ln` 于 `tests/*.py` = 0 命中）⇒ 它们不是"潜伏自腐"，而是**零测试覆盖**。故本批**只修已证实红的那一条**，其余登记为覆盖面缺口，不动绿测试。

### 9.4 P0-1 cron 已装机（首次被拦 → 用户「授权安装」→ 装后实跑验证）

**拦截与授权经过**：首次 `crontab /tmp/cron_new.txt` 被「Auto mode: action blocked by classifier — Stage 2 classifier error」拦截 ⇒ **未重试、未绕过**（同 22:15 条 `systemctl restart` 的处置范式），如实上报并把完整可应用包落本节。**用户随后明示「授权安装」⇒ 执行安装。**

**装前安全核对（防覆盖他人改动）**：预备文件是 08:48 的 crontab 快照 ⇒ 安装前把当前 `crontab -l`（433 行）与预备文件的前 433 行 **`diff` 逐字比对，一致** ⇒ 期间无人改 crontab，整文件安装不会覆盖任何人的改动。**装后复核**：`crontab -l` = **447 行**、`skill_carrier_test_gate` 命中 **1**、**原有 433 行逐字保留**（`diff` 一致）。装机前全量备份：`/opt/ftseek/backups/crontab-20261120-084150-pre-skill-test-gate.txt`（433 行 / 66288 字节）。

**装后原样实跑验证（不是"装上就宣称成功"）**：从 `crontab -l | tail -1 | cut -d' ' -f6-` 抽出**与 cron 完全同一条命令**（392 字符）执行 ⇒ **rc=0**；产物 `logs/skill_carrier_tests.log` **938 字节**、末行 `154 passed, 1 skipped in 4.59s`；`logs/skill_carrier_tests_latest.txt` = `2026-11-20T09:01:32+08:00` + 同一摘要行；无残留 pytest 进程。今天 **Friday（dow=5）∈ 1-5** ⇒ **首次自动触发 = 今日 11:02 CST**。

**台账已同步入账**：`docs/CONTROL-LOOP-LEDGER.md` §2 crontab 表追加 `crontab-skill-carrier-test-gate` 行（5 列）。守卫实跑：`test_crontab_loops_are_ledgered_or_baselined` / `test_ledger_names_carrier_script_for_crontab_rows` / `test_crontab_baseline_entries_carry_reason` / `test_crontab_baseline_has_no_ghost_entries` **4 passed**；全文件 **1 failed / 9 passed**，唯一红点仍是 §9.5 的**既有**幽灵 job_id（数量与改前相同 ⇒ 本批未新增红）。

**选时与 footprint 依据（全部实测，非估计）**：
- 峰值 RSS **129,540 KB ≈ 126MB**、墙钟 **4.75s**（`/usr/bin/time -v`）；宿主 available **1266MB**。
- **枚举 cron 分钟占用后发现没有任何空闲分钟** —— `extended_hours_exit_cron.py` 与 `freeze_sentinel.py` 是每分钟族 ⇒ 错峰纪律的实质是"别再叠重解释器"，故选**占用最低**槽。
- 选 **`2 11 * * 1-5`（CST 11:02）**：该分钟仅 **2** 个每分钟哨兵；且 11:02 CST = **ET 22:02**，落在盘前(04:00–09:25)/盘后(16:00–19:55)窗**之外** ⇒ 退出 cron 实际空转。并发 **2→3**，远低于 2026-11-03 齐发 **12–13** 个致 **3.6min 进程级静默冻结**的阈值（crontab 头部记载）。
- 比 DA 发车早 **~10h**：DA 主链是 **Hermes job（`0 8 * * 1-5` tz=America/New_York ⇒ 21:00 CST）**，**不在系统 crontab**（台账 §2 已记其为唯一 ET-pinned 载体）。
- 命令行**不含 `%`**（cron 会把它当换行符）；用 `date -Iseconds` 取时间戳；`flock -n` 防叠加 + `timeout 300` 上限；**只落日志、零推送**（军规：禁直连 notifier，新增推送须走 `app/push/` hub 且默认静默 ⇒ 本批不新增推送面）。

**(a) cron 行（连同注释，追加到 `crontab -l` 末尾即可；已备于 `/tmp/cron_new.txt`，447 行）**：

```cron
2 11 * * 1-5 flock -n /tmp/skill_carrier_test_gate.lock bash -c 'cd /root/.hermes/skills/deep-stock-analysis && { date -Iseconds; timeout 300 /opt/ftseek/FTSeek-US/venv/bin/python -m pytest tests/ -q; } >> /opt/ftseek/FTSeek-US/logs/skill_carrier_tests.log 2>&1; { date -Iseconds; tail -n 1 /opt/ftseek/FTSeek-US/logs/skill_carrier_tests.log; } > /opt/ftseek/FTSeek-US/logs/skill_carrier_tests_latest.txt'
```

**(b) 台账行（装机后必须同步入账，否则违反台账 §6.1「新增 cron job 必须入账」；追加到 `docs/CONTROL-LOOP-LEDGER.md` §2 的 crontab 表）**：

```markdown
| crontab-skill-carrier-test-gate | skill 载体契约测试闭环 (生产 DA 真身 `/root/.hermes/skills/deep-stock-analysis/scripts/llm_synth.py` 等的 155 个测试; 系统 crontab 非 Hermes, 2026-11-20 DEB-20261119 P0-1, 用户裁「执行1-3」) | `2 11 * * 1-5` (CST; = ET 22:02, 比 DA 发车 08:00 ET 早 ~10h) | 只跑 `pytest tests/ -q` 于 skill 目录, 结果 append `logs/skill_carrier_tests.log` + 时间戳与末行摘要写 `logs/skill_carrier_tests_latest.txt`。**零模型调用 / 零 DB 写 / 零推送**。补的是仓外载体验证盲区: 该载体 llm_synth.py 3427 行 / 17944 行 Python / 105 个 .bak, 仓内五道门 (check_undefined_names / pre-commit / G-02 / 启动冒烟 / 9000+测试) 一道都不覆盖, 此前 cron 跑 skill pytest 条数=0 ⇒ 一条日历自腐红测试躺 25 天无人发现并把查勘主线带偏两次归因 | 失效模式: ①测试自身日历自腐 (已修一例: `test_prompt_source_truth.py` 冻结 `today=date(2026,10,22)`, 范式同 `test_prev_close_reconcile.py`); ②`flock -n` 抢不到锁则跳过本轮 (不叠加); ③`timeout 300` 上限 (实测 4.75s / 峰值 RSS 126MB); ④宿主内存压力 ⇒ 选 11:02 CST 因该分钟仅 2 个每分钟哨兵且退出 cron 在 ET 22:02 空转, 并发 2→3 (远低于 2026-11-03 齐发 12-13 致 3.6min 静默冻结阈值); ⑤**只落日志不告警** ⇒ 红了不主动通知, 须人或后续门读 `_latest.txt` (刻意: 军规禁直连 notifier, 本批不新增推送面); ⑥**零测试覆盖缺口仍在**: `llm_synth.py:2123/:2144/:2214` 三处真实 today 闸无任何测试调用 (§9.3)。回滚 = 删 cron 行 + 本台账行; 装机前备份 `/opt/ftseek/backups/crontab-20261120-084150-pre-skill-test-gate.txt` |
```

> **为何当时不先写台账行（纪律留痕）**：cron 未装就入账 = **记载与活体调度脱钩**，正是 `test_ledger_job_ids_are_real` 的幽灵纪律要防的 ⇒ 首次被拦时**只落包不入账**，待用户授权装机后**同批补台账行**（现已补，见上）。
> **守卫兼容性已核并实跑通过**：`_SCRIPT_REF = scripts/([A-Za-z0-9_]+\.py)` ⇒ (b) 行含 `scripts/llm_synth.py` **能过** `test_ledger_names_carrier_script_for_crontab_rows`；(b) 行**不含任何 12 位 hex job_id**（`grep -oE "[0-9a-f]{12}"` 空）⇒ 不触幽灵检查；(a) cron 行**不含 `scripts/*.py`** ⇒ 不进 `_cron_scripts()`、不触 `test_crontab_loops_are_ledgered_or_baselined`（但台账 §6.1 纪律仍要求入账，故 (b) 已落）。
> **下面 (a)/(b) 保留为已应用内容的逐字留档**（便于回滚与复核），非待办。

### 9.5 本批既有红点（非本批造成，如实登记）

`tests/test_control_loop_ledger.py` 实跑 **1 failed / 9 passed**：`test_ledger_job_ids_are_real` 报幽灵 job_id **`68cc76b16ebd`**。**本批一行未改台账**（`git status --porcelain -- docs/CONTROL-LOOP-LEDGER.md docs/references/crontab-loop-baseline.txt` **为空**）⇒ **既有红点**，属先前条目所记「棘轮既有红点另账」。**不在本批处置范围**，登记以免被误读为本批引入。

### 9.6 本批改动面与部署判定

| 面 | 改动 |
|---|---|
| `app/` · `main.py` | **本批零改动** ⇒ 本批不触发 G-02、不需重启、不部署。⚠️ **收口核验时曾发现系统级 drift 非负并据此向用户发紧迫预警，该预警的推断部分已证错（§3 第 8 条）**：6 个文件（`position_monitor.py`/`scheduler_core.py` mtime 11-20 08:39:49、`trailing_stop.py`/`position_exit_monitor.py`/`trail_state_store.py` 08:49:13、`config/unified_config.yaml` 02:31:09，共 164+/21−）属**另一 agent 的 MU 出场保护修复**；`logs/deploy_freshness.log` 实证 **G-02 三次全部 `skip: ...outside_safe_window`、从未触发**，09:00:23 的重启是**对方自己的受控重启（其 DEB-20261120 明写冒烟 7/7，用户直接授权）**。**重启后实测**：MainPID 3524086→3574534、`ERROR\|CRITICAL\|Traceback` 自 09:00 起 **0**、`open_positions` 仍 2 仓（LLY/MU）、**H-12 暴露面仍 SAME**（`current_sl == initial_sl`）、**本批 cron 不受影响**（447 行/命中 1）。**我未重启、未回退、未触碰对方任何文件** |
| 仓内 `scripts/` · `config/` · `tests/` | **零** |
| skill 侧 `scripts/`（生产代码） | **零** |
| skill 侧 `tests/test_prompt_source_truth.py` | **1 个测试函数**（冻结 `today`，+9 行注释与 partial） |
| `docs/OKR-2026-11.md` | :214 一行 + ⚠️ 更正注 |
| `docs/debates/DEB-20261118-da-input-hygiene-preregistration.md` | §1 与 §6.2 两处 ⚠️ 注记（**原文保留、判据未改**） |
| `docs/debates/DEB-20261119-da-reasoning-chain-audit.md` | 本文件（§2.1/§2.4/§3/§5.1 复核更正 + 本节） |
| `docs/debates/README.md` | 索引 +1 行（他人两行逐字保留） |
| `handoff.md` | 立案条 + 执行条 |
| 系统 crontab | **433 → 447 行**（+1 cron 行 +13 行注释/空行；原有 433 行逐字保留；用户「授权安装」后装机，原样实跑 rc=0） |
| `docs/CONTROL-LOOP-LEDGER.md` | §2 crontab 表 **+1 行** `crontab-skill-carrier-test-gate`（台账 §6.1 入账纪律；4 道相关守卫 passed） |
| 付费模型调用 | **0** |

## 10. 2026-11-21 CST / 11-20 ET · 今日交易链核查后的零输入修复

### 10.1 授权与处置边界

用户在今日多空方向与交易链核查后明确「如果确认有问题 若有把握请进行合理优化和调整」。本批只修观测与证据保存，不将一般修复授权解释成撤销 §6 第5条的 R2 输入冻结。§5.2 P1-1 的两处资金门、财报空窗呈现、时效/跨语言去重、交易规则均未改；不增加分析维度、不接新入场门、不改四锁/仓位/回撤阈值/auto_execute/NO_TRADE、不下单、不重启、不重跑付费模型、不提交推送。

### 10.2 已写入的实现

- `scripts/da_chain_contract_audit.py`：原新闻/研报计数增加 `basis=collected_before_render`，`rendered_items=null`；修正「采集重复已进入模型」「采集无时间等于模型无法判断」的过度声明。新增 `input_visibility`，只对比原存档 `collected` 与 `user_prompt` 栏目：资金八字段完整性（全零有效、布尔/非有限数值无效）、预期资金数值格式/分时兜底、健康空财报栏目是否存在、任务成功数与显式来源状态数量、原事件注入/拒绝元数据；CLI 同时输出逐标的与汇总。缺 prompt 为未知，不填实际渲染覆盖百分比。
- `scripts/da_event_context_shadow.py`：新 immutable run artifact 增加独立 `llm_artifacts={main_raw,challenger_raw,prompt_version}`，保留两角色解析后的原始 JSON（含缺键与 null），不存 prompt 全文、不进入 `latest.json`。原生成器已返回这些数据，本批只接住此前被丢弃的返回值；不改 normalize/validator/prompt/hash/brief/retry/调用次数，旧 run 不补历史回包、不覆盖、不重新付费。
- 两个对应测试文件覆盖采集/渲染口径、正常与失败/未知边界、实际 CLI 集成只读性、成功 latest 不泄漏原 JSON、无 confidence 键与显式 null、OPPOSE 缺顶层证据仍失败、失败不改 latest/重复不付费、旧 run 不补历史。

### 10.3 验证记录

可复跑验收命令（事件测试全部使用 sandbox 假 LLM，审计默认只读且不加 `--out`）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python -m pytest -q -p no:cacheprovider tests/test_da_chain_contract_audit.py tests/test_da_event_context_shadow.py tests/test_market_brief.py
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. ./venv/bin/python scripts/da_chain_contract_audit.py --date 2026-11-20 --json
```

- 审计改前基线 `25 passed`；先加九条契约钉子为 `9 failed / 25 passed`，补实现后 `34 passed`，随后追加数值占位符/CRLF/真实 CLI 三条边界测试。GLM 首次联合回归为 **`91 passed / 1 failed`**：新增 CLI 钉子的末行误假设 tmp_path 只有 checkpoint 目录，而 `tests/conftest.py:725–745` 的 autouse 夹具早已写入 `live_probe_lock_waiver.json`。前面可见性/原文件只读断言均通过，非审计写文件；主线程亲读夹具后将钉子改为**调用前后顶层目录 + 全部递归文件 bytes 快照一致**（不排除夹具文件、不放松只读契约），GLM 同命令复跑 **`92 passed / 0 failed`，exit0，7.24秒**；主线程已亲读完整结果日志 `/tmp/da-repair-verifier-wOMroQ/pytest_after_fixture_fix.log`。初次失败日志保留 `/tmp/da-repair-verifier-wOMroQ/pytest.log`。
- 事件代理先行 sandbox 回归 `tests/test_da_event_context_shadow.py tests/test_market_brief.py` 为 `55 passed`；主线程已逐段核对实际 diff，GLM 首次联合92例中事件及market_brief共55例全过，不以代理自述代验收。
- GLM 实跑今日 CLI **exit0**（stderr空），独立 fresh 脚本不 import 审计模块复算五组数字，全部逐项一致：**37/37**完整资金分布未检出预期数值格式，资金栏目 **intraday36 + missing1**，**36/37**分时主力 N/A，**37/37**健康空财报无栏目，**34/37**满任务但来源状态数量不足（另三项任务数分别19/18×1、19/17×2），**37/37**共享事件因 `brief_date_mismatch` 未注入。报告 `/tmp/da-repair-verifier-wOMroQ/audit_20261120.json`，复算脚本同目录 `recompute.py`。
- 覆盖 pytest + CLI 前后，**74 checkpoint JSON（37 raw+37 analysis）与12 event live JSON（含latest）SHA256逐字一致**；主线程另用 `cmp` 对两组 before/after清单核对 exit0，未加 `--out`。清单同目录 `hash_ckpt_before.txt` / `hash_ckpt_after_audit.txt` / `hash_event_before.txt` / `hash_event_after_audit.txt`。
- Kimi 独立审查**无阻断/高/中问题**，确认仅审计/留痕，既有normalize/validator/hash/prompt/latest选择逻辑未改；指出两处低级测试盲点：日期拒绝finding严重度未钉住、旧run仍有latest的ALREADY_CURRENT分支未覆盖。本轮追加 `severity=info + reasons` 钉子并把既有same_input_hash测试参数化覆盖legacy，主线程同命令最终实跑 **`93 passed / 0 failed`，exit0，10.63秒**，日志 `/tmp/da-repair-verifier-wOMroQ/pytest_final_review_nails.log`。此93为主线程复跑，不冒称Kimi执行了测试；此前92为GLM独立复跑。
- 主线程执行四文件 `git diff --check` / `PYTHONDONTWRITEBYTECODE=1 ./venv/bin/python scripts/check_undefined_names.py <四文件>`，均exit0；亲验pyflakes可导入（排除工具缺依赖静默放行），AST解析4/4。

### 10.4 未验证与下一步

1. 本批观测修复**不代表资金分布已送进生产模型**；资金/健康空日历实际输入改动仍走 §5.2 与 R2 读数后的影子/预注册。
2. 新回包留痕不能恢复 11-20 已丢失回包，不能判定失败来自缺键、null、错拼或其他原模型输出；事件生成失败根因与自然运行首个新 artifact 仍未验，不通过降低 confidence/OPPOSE/日期校验门救活简报。
3. 09:58 入场轮停摆只有源码/日志旁证：SDK 默认建连 READY 等待无 deadline、请求发出后才进入20秒等待；无当时 PID 线程栈，**不能归因为某一具体 SDK 调用**，也不能把13.17秒 GC 等同615秒整进程冻结。连接策略/线程诊断未改，不以取消线程冒险造成迟到下单。
4. 检查的是保存的二级栏目与当前资金格式，不做全 prompt 事实核验/资金数值等式校验/渲染项时龄复算；日历栏目存在也不等于内容完整，来源状态数量不等于逐源质量覆盖。其他 WIP 原文与代码保留。
5. 独立回归、今日存档只读 hash 核验与Kimi复核已完成，两处测试盲点已补并最终93例全绿；本批四代码/测试文件 + 本文/`handoff.md`，未提交推送、不需服务重启。后续只观察自然新run留痕；实际输入修复仍按§5.2，不能把本批验收当成R2输入解冻。

## 11. 2026-11-21 CST / 11-20 ET · 执行恢复根因修复（隔离验证中，未部署）

### 11.1 授权、优先级与裁决边界

用户先指出「为什么不修复 今日没有正常交易 为什么」，随后明确「请根据优先级做根因修复 确保可正常交易」。§10 只修观测而未修执行停摆，处置优先级不足；本批先修执行阻塞，再核账户熔断口径、VIX 来源和累计止损恢复语义。R2 输入冻结不限制独立执行故障修复，但不得借恢复交易重置历史峰值、调高回撤阈值、改变 VIX 数值来源或自动重武装已获裁的一次性累计止损例外。正常交易指有效信号可以完成评估和提交，不等于承诺成交或绕过保护。

全部代码变更在 `/opt/ftseek/FTSeek-US/.qoder/worktrees/trade-recovery-20261121`。生产实际源码基线保存在 `/tmp/trade-recovery-baseline-v9nejdv8`，包含 SHA 清单 `source_manifest.json`；隔离 HEAD 缺少生产在途依赖，另复制了 1329 个 app/tests/config 源文件（无交易数据、凭据或 `.env`），因此 **git HEAD diff 不等于本批 diff**，必须逐文件比对该生产基线。生产 Python、配置、交易数据和服务未改，不提交、不推送、不下单。

### 11.2 今日零新仓的三段因果及前述判断更正

- 开盘至 09:57 ET：此前累计止损监控将 `auto_execute=false`，恢复热载于 09:58 进入入场轮。
- **09:58–10:17 ET：真实执行停摆**。日志最后停在入场预算附近，Futu 建连超时与 10:08 心跳 stale 615 秒为旁证；23:17 CST 受控重启后恢复轮输出。没有原 PID 的线程栈，不能断言具体 SDK 调用，更不能将 13.17 秒 GC 等同整个进程冻结。`handoff.md` 的 22:5x 条仅覆盖较早核查，不能据它否定随后这段故障。
- 重启后：多个入场轮正常完成，账户峰值回撤约 3.03–3.07% 被拒；10:23 起市场环境为 NO_TRADE。全日最终 26 条指令为 **22 SELL_SHORT / 1 BUY（PDD）/ 3 SKIP（PANW、RIVN、CRM）**，0 新入场、0 新入场 outbox；唯一交易是 MU 6 股隔夜止损，09:30:17 ET 成交，毛 -256.68 / 净 -260.79。LLY 3 股仍在仓。此前 22 空/2 多/2 SKIP 是较早快照，不作为全日终值。

### 11.3 已确认保护不是误拦截

**账户峰值回撤**：同一模拟账户 19930823 的峰值 99999.496 可由 10-27 ET 的九个 broker equity 快照和账户日志复核；11-21 07:26 CST 净值 96889.835，现金 92853.062 + 市值 4036.773，回撤 **3.1097%**。当日净 -260.79 / 当前净值仅约 -0.269%，不等于历史峰值回撤。代码 3% AccountCircuitBreaker 与另一个 5% Capital/Drawdown 阈值不是同一闸门；LIQUIDATE 历史上只拒新仓，未接强制全平。现有 exit ET 日期归集、净额优先及 SIM/REAL 分臂均未发现本批应修算术错误，增加契约测试而不重置状态。峰值不变时净值须严格高于 96999.51112 才越过 3% 边界，行情变化会改变实际恢复时点，不能承诺下一交易日自动解除。

限制：历史 equity 行未逐行带账户身份；现金流缺独立完整账本，部分历史 cash+market_value 不等于 equity。仅峰值和本轮净值关键快照逐字段相等，不能将全段净值变化全部归因为交易亏损，也不能据局限虚构一个较低峰值。

**VIX**：Router 和 VIXMonitorLoop 盘前/盘中均优先 VIXY×0.85，监控中的约 19 不是独立 spot VIX 真值。10:23 ET 同分钟，Router VIXY 23.54×0.85=20.01；公开分钟序列 spot VIX 范围约 29.33–29.54（不是同 tick）。VIXY 是短期限 VIX 期货组合价格，不是指数点位，固定系数存在结构性口径错误，且修正更可能加强防守而非放行。RFC-190 已于 11-07 立此问题，数值来源变更仍待专门裁决；本批不偷偷改系数、改源或 NO_TRADE 阈值。现有 quote 观测时间被丢弃、cache 只按写入时点验鲜等缺口仍未修，不以一个旧 FRED 日值冒充当前分钟。只读研究来源：[spot 分钟序列](https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?period1=1795183200&period2=1795273200&interval=1m)、[VIXY 分钟序列](https://query1.finance.yahoo.com/v8/finance/chart/VIXY?period1=1795183200&period2=1795273200&interval=1m)、[ProShares 产品定义](https://www.proshares.com/our-etfs/strategic/vixy)。

**累计止损监控**：`tripped=true` 是 §43.2 已获裁的一次性人工恢复例外，脚本不再自动重触发；触发存档 n838/net -4908.07 与后续 n839/net -5168.86 不是同一个时点。保留 tripped、原基线和止损线，不能称自动止损已重新值守；重武装必须另裁。

### 11.4 执行修复及验收约束

- Futu 普通与历史行情建连共用锁；候选连接使用 `is_async_connect=True`，第一次 READY 查询前设置 3 秒连接等待，READY 成功才发布给共享 provider；失败关闭候选，不把未就绪连接标为可用。保留原权限不足时降级逻辑。**这不构成 SDK 全生命周期有界承诺**：SDK 锁、控制 socket 发送和关闭仍可能阻塞。
- 入场只读输入共享每 scheduler 单飞槽：行情、持仓查询及市场环境构造/评估只允许在有界等待的只读 worker 中运行；首个超时暂缓当前入场，旧 worker 未退出时后续轮立即暂缓，不增长线程、不取消/重放下单。worker 不执行 trigger、指令写入、审计、风险状态改写或订单提交；迟到结果不得回收或重新标时。市场环境的 class cache 也必须禁止迟到 worker 发布，否则下一轮仍可能读入已弃结果。
- 持仓快照使用 `query_positions` 的显式成功契约，不能把查询失败当成空仓；超时/失败在 root 暂缓，不进入下单。Trigger、knife、reprice、MOM、blackout 影子取价和 TCA 到达价均须保持相同暂缓边界；preflight 仅将市场环境只读部分隔离，不能把会写风险基线/熔断状态的整段 preflight 放入可弃线程。
- Watchdog stale 告警前记录所有线程栈，已有 600 秒告警冷却限制输出频率；栈记录失败仍告警，盘中不自动重启的既有纪律不改。它只为未来故障取证，不补造 09:58 历史栈。

### 11.5 已实跑验证与未验证

主线程来源/风险七文件回归 **108 passed / 0 failed / exit0 / 5.30 秒**，日志 `/tmp/trade-recovery-baseline-v9nejdv8/readiness_regression_final.log`；GLM 独立使用正常 conftest 同命令 **108 passed / exit0 / 4.67 秒**（工具回报，无另存结果日志）。Kimi 对此来源/风险子集独立审查无阻断/高/中/低问题，并独立复跑 **108 passed / exit0 / 3.92 秒**；只批准限定的 quote-ready 建连和 stale 诊断声明，未审在途 entry 变更，未宣称历史 615 秒根因已证。OpenD/socket、通知/重启、broker 输入均为桩，账户测试使用 tmp SQLite，未连接生产 broker 或写生产数据。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /opt/ftseek/FTSeek-US/venv/bin/python -m pytest -q -p no:cacheprovider tests/test_futu_quote_ready.py tests/observability/test_watchdog_stale_stack.py tests/test_futu_kline_sick_gate.py tests/test_futu_dailyk_quota.py tests/test_futu_kline_cache_caliber.py tests/risk/test_account_circuit_breaker_pnl.py tests/test_futu_snapshot_na_tolerance.py
```

两个原日 K 测试在收盘后假失败已先用未改 provider 基线复现；只冻结测试到 09:00 ET，不改生产缓存标准。另一个 high-ATR 的 0.5 秒非阻塞钉子被无关 staging-shadow 冷加载生产 YAML 耗时干扰，只打桩该无关配置边界，不改 0.5 秒限值或高 ATR 影子逻辑。7 文件 undefined-name 门 exit0、AST 7/7，pyflakes 3.4.0 可导入，非缺依赖静默放行。

GLM 正常 conftest 联合来源/风险七文件 + `tests/playbook/test_entry_data_bounded_fetch.py` 为 **176 passed / exit0 / 10.52 秒**（108+68），完整日志 `/tmp/trade-recovery-combined-independent.log`，主线程亲读结果末行。新测试使用真实 helper、trigger/auto 流和 regime 实现，外部 I/O、风险/准入回调为桩；pipeline owner 接线通过编译实际两个转发函数的 AST 验证，不代表完整应用 bootstrap 已验。主线程另在成功路径注入执行 callback 的调用线程身份断言，**1 passed / 0.48 秒**，日志 `/tmp/trade-recovery-caller-thread-probe.log`；这只证调用了 mock PlaybookService，不是 broker 成交或真实 DB 认领。

邻域第一次十一文件收集因隔离缺 `scripts/entry_blackout_shadow_eval.py` 退出 2；剔除此文件的十文件实跑 **40 failed / 205 passed / exit1 / 14.18 秒**，日志 `/tmp/trade-recovery-neighbor-independent.log`，不能冒称当轮全绿。主线程亲读失败清单：20 项 pipeline MagicMock 自动生成不存在的单飞 state 导致假 busy，2 项 blackout 旧夹具把查询异常当成无价 fallback 或同时计为无效价格，18 项 MOM/R2 缺少或落后生产脚本。随后只补入生产原样三脚本 `entry_blackout_shadow_eval.py` / `mom_etf_v1_generate.py` / `risk_size_tier_forward_eval.py`（SHA 在基线 `scripts_source_manifest.json`，不是本批实现），并修两文件测试状态/正常无价与异常语义；不绕过有界 helper、不恢复异常放行、不改生产价格或禁触窗。修后邻域和 entry 独立审查尚在执行。

主线程阶段性源码/测试14文件 AST14/14、undefined 门及全 worktree diff-check exit0；上述邻域夹具修正会另补检查。10:01 CST fresh 核生产服务 active、MainPID 3645396、仍于 11-20 23:17:28 CST 启动，八个源码/配置目标 SHA 与生产基线相等，生产 `entry_data_fetch.py` 不存在：本批确未部署。下一步完成邻域回归、独立审查；部署需 fresh 在途出场/WIP 两核、操作授权和受控重启/启动冒烟，不能携带其他在途 WIP 整仓覆盖。账户真实回撤、VIX 来源裁决及自然有效信号的前向验收仍是交易恢复边界。

### 11.6 独立审查发现的真实阻断（不能以176绿例盖章）

1. **HIGH：迟到报价可从底层缓存复用**。Kimi 使用实际 helper + 实际 Futu `get_quote`、仅阻塞 SDK 桩，第一轮 0.02 秒超时后释放 SDK，迟到响应仍写 `quote:US.AAPL` / `_cache_time`，下一轮拿到同一对象且 SDK 总调用数仍为 1。初版只弃 `flight.value` 的保证不足，单测假 router 未覆盖 provider 缓存。Futu 单 quote 的订阅/快照/fallback 共三处缓存写，Finnhub 与 TwelveData 各一处 quote cache 写均须纳入同一请求上下文；不通过删除共享缓存解决，否则可删除另一调用者的合法结果。修法为 data 层共享 ContextVar 默认允许普通调用发布，入场 worker 仅在自身线程禁止 quote cache 发布并 finally 复原；正常已存在 cache 读取保留时龄和身份，入场新取到的 quote（含准时结果）不写共享缓存。代价是可能增加后续获取次数，不改数值/源顺序/TTL/原时间戳。独立实际 provider 再现回归未完成前，此 HIGH 未闭合。
2. **HIGH：畸形持仓行可经 SIM fail-open 下单**。Kimi 设置显式成功但 `positions=[None]`，或 `[{code:'US.AAPL',qty:'bad'}]`，真实 auto-flow 在 `already_held` 的 `p.get/float` 抛错后走旧 SIM fail-open，实际调用 mock execute 一次。只有 dict/list 顶层校验不够；须在循环前逐行验证 code 与有限 qty，合法长/短/零及数字字符串保留，任一行异常使整轮暂缓、零订单/零业务写。不改变原 broker 源回包，不能只修 `[None]` 示例。补齐真实边界测试并独立再现未下单前，此 HIGH 未闭合。

邻域两夹具修正后实施代理正常 conftest **111 passed / 2.47 秒**（`/tmp/trade-recovery-legacy-fixtures.log`，主线程已亲读基线相对 diff；仍不是最终联合验收）。三脚本补齐后的 GLM 子集 **6 failed / 41 passed / exit1 / 2.95 秒**，日志 `/tmp/trade-recovery-script-neighbors.log`：又缺 readiness 脚本、settle 脚本版本落后，以及四项 MOM FakeBroker 仍仅提供 `get_positions`。再原样补入生产 `verify_mom_etf_v1_readiness.py` / `settle_entry_confirm_forward.py`（基线 SHA 清单现共五脚本），MOM 假 broker 改为显式 `query_positions(success=True, positions=[], target_env=sim)`；均未改生产脚本业务。最终回归仍待新 app 修复完成，不静默剔除红项、不把依赖补齐算成本批功能实现。

### 11.7 后续实证：quote 修复成立，K线迟到发布仍阻断

- quote 修复实施代理正常 conftest 两文件 **102 passed / 5.64 秒**（原68 + 新34），实际 provider quote/cache/连接检查/限流实现保留，仅隔离 SDK/HTTP/环境/遥测。Kimi 独立再跑真实 helper + Futu 无网络探针 exit0：超时释放后 `_cache/_cache_time` 均未写，下一轮 SDK 调用数从1变2；普通非entry调用继续发布，后续命中原对象、原cache_time与timestamp不变。这只闭合 quote 子路径，不能据此批准全链。
- **新增 HIGH：迟到 K1M 数据仍可复用**。Kimi 沿真实 router `get_full_market_context` + Futu `get_kline`、仅 fake SDK，再现 history query 超时后向 `_kline_data_cache['US.AAPL|QFQ|K_1M']` 发布30根bar；第二轮 `recent_closes` 来自该缓存、SDK总调用数仍1。缓存TTL60秒，进入ATR/VWAP/reprice；router daily cache 也是可达发布点。正在完整定位 entry 可达 K线/日K缓存及ring写入，须扩展请求级发布隔离，不删除共享健康缓存、不关普通线程缓存，不以缩窄宣称替代修复。最终验收尚未完成。
- Kimi 另四文件正常 conftest邻域原结果 **136 failed / 102 passed / exit1 / 14.10 秒**，完整输出 `/root/.qoder/tmp/-opt-ftseek-FTSeek-US/tool-outputs/session-qs_01m7wt071g9679zxrmm3758fa5/bdv0d52p5.output`。主线程亲读：红项仅 entry_revalidation 与MOM两文件，旧fake assess不接受新keyword、broker无显式query_positions、隔离包未加载真实helper/policy及缺生产脚本。原日志另记录 **22次 PDT默认库写入隔离worktree data/ftseek_us.db**，不是原生产库，但不满足该代理只读任务；不称零数据写。
- 主线程修 `test_entry_revalidation.py` 的真实helper/policy白名单、assess关键字及显式成功持仓回包；`test_dual_arm_routing.py` / `test_live_arm_p0_contracts.py` 新autouse仅将默认PDT库导向各test tmp_path、清空并恢复PDT单例，不改生产路径/阈值或pytest全局豁免。四文件已派正常conftest安全复跑，结果未到前不宣称通过；外部及tmp外数据库写入不得再以存量豁免放过。

### 11.8 已修复的入场可达缓存与外部持仓边界

本节更新 §11.6–11.7 的产品缺口状态，不抹掉早期红例。最终应用范围为 **13 app + 15 tests = 28 文件，生产前像相对21改/7新**；五个恢复的生产脚本原样不变，不算功能实现。精确清单及冻结后 SHA：`/tmp/trade-recovery-baseline-v9nejdv8/final_owned_scope.json`。工作树另留有八个 HEAD 中已有、生产已不存在的旧 Python 文件，本批逐一确认与 HEAD 字节相等，未新增/修改/删除，不能当成本批新文件；整仓 HEAD diff 仍不能部署。

- `DATA_CACHE_PUBLICATION_ALLOWED` 默认 True，入场 worker 从构造/取数开始置 False，finally 复原；旧 quote-only 名称无引用、无兼容 alias。只隔离共享数据发布，不删除已有缓存，不关普通读取或健康 SDK push。
- 已覆盖当前 entry 图中的 Futu quote 三出口、K1M TTL 缓存与 history→无 TTL ring 发布、ring→TTL 提升、日K payload/时间/dirty/落盘整块；TwelveData 公共 `_set_cache`（quote/daily/time_series/SPY）；Finnhub quote；Router daily 的两源分支及 SPY MA200/20/50；AlphaVantage live VIX/FRED 的值与 cache_time 对。已有缓存命中保留对象与原时龄，日K磁盘读取保留原年龄/字节；数值、来源顺序、TTL、限流/健康/失败冷却不改。准时新结果也不发布，可能增加请求量，生产热/冷性能尚未验。
- Futu minute history 的原30秒查询冷却不变。弃迟到结果后立即重试可以暂缺 minute bars，这不是复用迟到缓存；证明第二次实际 SDK 请求的测试只将假墙钟推进31秒，deadline 仍用真实 monotonic，没有修改生产节流。
- 持仓在指令循环前逐行校验 raw `code` 的完整 US 大写词法及有限非布尔 `qty`，任何一行畸形整轮暂缓。Kimi 实证初版仅 prefix 校验仍使 `US.AAPL ` / `US. AAPL` / `US..AAPL` 漏掉持仓匹配并提交；现用 fullmatch 拒绝，不 strip/洗白输入，保留合法股票、BRK.B/BRK-B、期权形式、带符号数值与数字字符串原对象。畸形两指令场景须零取价/零提交/零业务追踪写入。
- 查询异常不是 ATR 缺失终态：两条旧 raises 回归改为暂缓审计、原行/追踪零写、零提交，再恢复真实 helper 的新鲜101报价并确认下一轮一次提交；不是删断言或恢复失败放行。终态缺 ATR 等其余判据仍保留。
- 独立真实 helper + provider/router 无网络探针已确认 quote/K1M/Futu daily/Router daily 的迟到及准时结果均不发布、后续新请求 SDK=2、普通发布/原命中与年龄不变。三文件回归142 helper +34 quote +57 context；57项使用真实 leaf/cache方法但 `_EntryContext` 是叶子适配器，不是完整 Router bootstrap，另有 Kimi 实际 Router transitive 探针补证。执行服务仍为 Mock，不冒称真实 DB 认领或 broker 成交。

### 11.9 阶段验收读数与隔离阻断（历史11:27 CST 时点，最终状态见11.10）

- Kimi 三文件正常 conftest **233 passed/19.67秒/exit0**，修 pandas 顺序后独立 `--noconftest` **233 passed/19.79秒/exit0**，两种模式分别记账。此前414组合仍有181 legacy setup errors及一次40ms快响应超时，不能由233覆盖：最先遗漏 progress/round_timer 纯模块，正常模式预加载也掩盖了 pandas C-extension 在全局 FrozenDatetime 后才导入的问题。现显式加载真实纯依赖、进度文件仅指 tmp，pandas 在冻结前加载；quote fixture 等待余量由0.04调0.2秒，应用默认3秒不变，超时及缓存断言未变。
- 主线程 legacy 冷 `--noconftest` **181 passed/15.12秒/exit0**，日志 `/tmp/trade-recovery-main-legacy-cold.log`。独立四邻域及最终414组合仍在复跑，未据主线程一次通过宣布完整终验。
- GLM 核心十文件正常 conftest初终验 **341 passed/25.47秒/exit0**，`/tmp/trade-recovery-core-final-independent.log`；quote测试余量修后最终r2 **341 passed/28.13秒/exit0**，`/tmp/trade-recovery-core-final-r2.log`。当时报告称13 app及quote fixture前后SHA相等，复核`/tmp/trade-recovery-sha-pre-core-r2.txt`实际仅12 app+quote fixture、漏market_regime，不能追认补填预检记录；已有12+1前后相等保留，完整13 app的最终凭据见11.10。旧13清单中的无关pilot/entry_confirm/dual_broker也不能当成本批完整SHA。旧日志不覆盖。
- GLM 14文件邻域 **478 passed/31.87秒/exit0**（1个既有转义序列warning），`/tmp/trade-recovery-neighbor14-final.log`，**仅功能绿，隔离未通过**：会话尾记录5次非tmp worktree主库写语句，经存量豁免放行，涉及capital_state/history/sqlite_master。通用“pdt_guard等”是豁免标签而非真实调用栈，实际来源仍在定位；不因worktree DB mtime未变就称零SQL写。原生产根未触及与该测试违背tmp-only是两个独立事实，不能互抵。此前四邻域两次 **2 failed/236 passed** 已证明临时PDT改道消除了那22次写，余下两raises随后才改契约，红日志保留。
- 主线程27文件AST通过、零新增UndefinedName/UndefinedLocal、diff-check exit0；TwelveData两处`np.ndarray`字符串注解是前像已有静态诊断，函数内已有局部numpy导入，本批不扩改，不能写成全文件零诊断。统一配置及五恢复脚本SHA与前像一致。
- 11:27:55 CST fresh核：生产11个已有app目标SHA全与前像相等，新helper/policy仍不存在，配置未改，service active/MainPID3645396/仍11-20 23:17:28 CST启动。本批未部署/重启/提交/推送/下单。历史615秒具体根因仍无线程栈；SDK调用不能取消、GIL/SDK锁/send/close及其他预算/sizing取数不构成全系统有界保证。

当时必须再闭合非tmp写入来源及回归、独立审查，最终结果如下。生产部署与重启仍需本批操作授权、在途出场/WIP两核及启动冒烟；自然有效信号/真实提交与成交尚未验。真实3.11%回撤保护、累计止损一次性例外与RFC-190改源裁决均保留，不能承诺11-23无条件开仓。

### 11.10 最终隔离验收（11:52 CST，未部署生产）

**实际范围**：13 app+15 tests=28，生产前像相对21改/7新。最后新增owned仅`tests/test_entry_confirm_live_wiring.py`夹具资本库隔离，13 app源码未再改。主线程11:52:46重枚举1329生产源前像与当前worktree，实际差集恰为28；八个HEAD-only旧py仍与HEAD相等且生产不存在，未删。全28 AST通过、零新增UndefinedName/UndefinedLocal、冻结SHA漂移空、diff-check exit0；两处TwelveData旧np字符串注解诊断不扩改，统一配置/五个恢复脚本仍与前像SHA一致。不得部署整仓HEAD diff。

**原5次非tmp调用的根因与夹具修复**：严格拒写诊断追到`test_defer_keeps_instruction_pickable_next_cycle`→`_save`→真实PlaybookDB/admission→`_equity_usd`→CapitalSafetySystem首次默认构造→`_ensure_db_table`，涉及capital_state/history/sqlite_master。通用PDT豁免标签不是此真实栈。env现在用真实`CapitalSafetySystem(db_path=tmp_path/capital.sqlite)`并注入单例，真实准入/config/settler路径及全部断言保留。dual两项cold失败来自没有根conftest提供的TEST-AUTH测试waiver；该文件仅增加tmp waiver/HALT路径与先前tmp PDT，真实LIVE门未改，不构成生产授权。Kimi逐份生产前像diff复核成立。

| 执行者/范围 | 正常conftest | `--noconftest` | 日志 |
|---|---|---|---|
| GLM核心10文件，最后quote夹具余量版本 | 341 passed /28.13s /exit0 | 本轮未跑 | `/tmp/trade-recovery-core-final-r2.log` |
| Kimi三entry文件+legacy | 414 passed /41.71s /exit0 | 414 passed /38.42s /exit0 | `/tmp/reviewer-threefocused-normal.log`、`/tmp/reviewer-threefocused-cold.log` |
| Kimi legacy单文件 | 181 passed /18.11s /exit0 | 181 passed /17.50s /exit0 | `/tmp/reviewer-legacy-normal.log`、`/tmp/reviewer-legacy-cold.log` |
| Kimi四邻域最终 | 238 passed /32.52s /exit0 | 238 passed /19.62s /exit0 | `/tmp/reviewer-neighbors-final-normal.log`、`/tmp/reviewer-neighbors-final-cold.log` |
| Kimi entry_confirm严格守卫 | 16 passed /9.66s /exit0 | 本轮未跑 | `/tmp/reviewer-entry-confirm-final.log` |
| GLM14邻域加固严格守卫 | 478 passed /33.54s /exit0，1个既有warning | 本轮未跑 | `/tmp/trade-recovery-neighbor14-strict-final-v2.log` |
| 主线程两末夹具加固严格守卫 | 31 passed /4.99s /exit0 | 本轮未跑 | `/tmp/trade-recovery-main-two-fixtures-confirmed.log` |

集合重叠，不能相加为独立总样本。主线程亲读上述结果；Kimi最终仅批准此28scope，无阻断，不代表全仓/部署/生产验证。

GLM严格日志`/tmp/trade-recovery-neighbor14-final-denials.log`全文为`SESSION_VIOLATIONS=0 / SESSION_KNOWN_HITS=0 / WAIVERS_CLEARED=True`，无DENY行。插件`/tmp/trade_recovery_dbdeny_plugin.py`守卫resolved原仓根及其worktree内`.db/.sqlite/.sqlite3`，相对路径按worktree解析，临时路径豁免；configure期实际清空conftest已知写库字典，sessionfinish合并其违规账。13 app+两末夹具前后15行SHA一致（`/tmp/trade-recovery-sha-pre-neighbor14-strict.txt`、`/tmp/trade-recovery-sha-post-neighbor14-strict.txt`），包括market_regime，完整匹配最终清单。主线程最后复跑额外断言已知写库字典空、违规/known账空、完整footer为0/0/True，并扩展预捕获connect所用conftest路径谓词到同一原仓根；日志`/tmp/trade-recovery-main-confirmed-denials.log`同样无DENY。

主线程上一探针31 passed/8.82s后wrapper exit1（`/tmp/trade-recovery-main-two-fixtures-strict.log`）不是新SQL拒绝：把64字节session footer误计成DENY，原日志仅0/0/False，False因手动清字典没有设置插件flag，不能据此证明清空也不能叫产品缺陷；新探针按DENY记录及实际字典/flag断言纠正。历史22次worktree PDT写、5次豁免调用、旧单DENY诊断、全部红日志保留。最终零写声明仅限对应测试运行、该守卫覆盖的DB写入；configure前导入窗口及非SQL副作用不由flag单独证明，不能扩成所有测试/全系统零副作用。

严格14文件可复跑命令（在隔离worktree，插件仅临时诊断，不改测试全局豁免或生产权限；重跑应另设新LOG_PATH以区分历史）：

```bash
set -o pipefail
FTSEEK_PROGRESS_FILE=/tmp/trade-recovery-neighbor14-strict-v2-progress PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="${PWD}:/tmp" /opt/ftseek/FTSeek-US/venv/bin/python -m pytest -q -p no:cacheprovider -p trade_recovery_dbdeny_plugin tests/test_market_regime.py tests/test_market_regime_f5_shadow.py tests/test_playbook_pipeline.py tests/test_auto_execute_open_floor.py tests/test_reprice_quote_age_gate.py tests/test_risk_size_tier_forward_eval.py tests/playbook/test_mom_etf_v1_flow.py tests/playbook/test_mom_instruction_isolation.py tests/test_entry_blackout_shadow_eval.py tests/test_entry_confirm_live_wiring.py tests/test_reprice_audit_persistence.py tests/playbook/test_entry_revalidation.py tests/playbook/test_dual_arm_routing.py tests/playbook/test_live_arm_p0_contracts.py
```

**生产边界与下一步**：11:52:46 fresh核生产11已有app SHA均同前像、新helper/policy不存在、统一配置不变，service active/PID3645396/仍11-20 23:17:28 CST启动；未部署Python/配置、未重启/下单/提交推送，纯MD治理归账不触重启。源码隔离验收完成，但任务仍in_progress：本批生产操作授权、fresh在途出场/WIP两核、受控systemctl重启/启动冒烟及自然有效信号/真实认领/提交/成交尚未验。worker不能取消、GIL/SDK lock/send/close及其他sizing/budget取数仍可能阻塞，完整SDK生命周期未证明有界；准时entry新结果也禁发布可能增加请求量，性能未验。57项context为真实leaf适配器而非完整bootstrap，真实Router无网络探针补证，执行service仍Mock。真实3.11%回撤保护、累计止损例外和RFC-190裁决继续保留，不强制开仓，不承诺11-23成交。

### 11.11 用户授权后的生产部署与启动验收（13:00 CST）

**授权与版本边界**：用户新消息「授权重启完成修复」授权本批部署/重启，随后通过问题明确授权仅本批本地提交不推送，选择部署前安全审查；未发现安全问题。28修复差集用临时index从生产前像生成，patch-id与独立release staged diff一致；仅本地新提交`fff29129a`（28文件，2650增/84删），parent`f296a3491`，干净分支`trade-recovery-release-20261121`/`/tmp/ftseek-trade-recovery-release-20261121`。提交中排除了生产已有其他WIP，实际部署以精确patch施加当前生产文件、验证匹配冻结SHA；不把release旧HEAD整文件覆盖生产。生产HEAD/index未动，不推送，纯MD收尾暂未提交，不能把28blob SHA与部署完整blob误等同。

**重启前两核**：12:55:34 fresh查SIM唯一未平LLY1918 LONG3@1410.64/PLAYBOOK_SIM，LIVE无未平仓；playbook_exits.db及exit_ledger.db所有intent BOOKED、所有leg TERMINAL，未决/非终态均0。所有28目标匹配保存生产前像，配置未变。源码mtime晚于旧启动的app为0；两脚本da_chain_contract_audit/da_event_context_shadow为本会话前一已验观测批，不是他人新WIP、不在service启动import图，未再次部署/执行。其余未提交WIP均比旧启动早、前后SHA验证不变。

**部署**：保存源码zip前像`/tmp/trade-recovery-source-before-deployment-20261121.zip`及prepared审计`/tmp/trade-recovery-deployment-20261121.json`后，`git apply --check`、再次CAS28前像通过，应用精确28差集（13app+15tests）；全部生产后像SHA与final_owned_scope.json相等、全28 AST通过、unowned app/scripts内容不变、统一配置及deb0815_stoploss_state字节不变、LLY完整未平trade行未改、两出场账本未决仍0。不是全库恢复，无SQL写/券商订单动作；服务自身自然读数写入不计为人工修改。最终重启前再次SQL/WIP/SHA核通过。

**已执行SOP**：只用`systemctl restart ftseek-us`，随后`PYTHONPATH=/opt/ftseek/FTSeek-US /opt/ftseek/FTSeek-US/venv/bin/python /opt/ftseek/FTSeek-US/scripts/startup_smoke_check.py --wait 90`。12:58:00新PID3739964/active/running（旧3645396），12:58:20机器冒烟 **7/7 exit0**：ConfigValidator/漂移检测/热重载/SSOT/双臂安全/出场快循环/零Traceback-CRITICAL。启动日志broker=sim/db=sim/ALLOW_REAL_TRADING=False；Futu1/主线内存0/playbook自管1对账一致，Reconciler及持仓恢复均明确LLY由Playbook管理、不纳主线store。LLY完整未平trade行仍与before相等，LIVE无仓、两个退出账本未决和非终态腿仍0，app晚于新启动漂移0。独立GLM只读生产复核在跑，结果返回后补凭据。

**READY黄金路径实证**：13:00:52主线程从已部署provider源码编译实际`_open_ready_quote_context`函数体（常量3秒），调用真实本地OpenD SDK `get_global_state`成功1.315秒并close。没有fake SDK、没有取quote/position/order；此为实际函数体与SDK的READY路径，不是完整provider bootstrap，不能外推全部SDK方法都3秒有界。记录已加部署审计。

**仍在保护且未验证**：生产peak99999.496保留/equity96894.392，真实回撤3.10511965%仍>=3%，AccountCircuitBreaker继续拒新仓是正确行为，不因重启清零；配置/累计止损state字节不变，不改VIX数值或stoploss重武装。当前周末market closed/sync paused，真实自然有效信号→入场认领/提交/成交未发生，不强制造单或承诺11-23成交。已授权部署/重启已执行，不重复apply；独立生产健康复核/未来自然链路验收补充后再收口任务。历史615秒具体调用根因、完整SDK/GIL/其他budget-sizer有界性与缓存禁发布请求成本仍未证。

**独立生产复核补记（2026-11-21）**：旧28文件批GLM只读验收已完成，主线程亲读`/tmp/recovery-verify-20261121-part1.log`确认28/28完整后像及1293/1293非本批运行源码不变。首档错查open_positions.quantity，后改查size确认LLY唯一SIM仓；13:04:17迟跑--wait虽七绿却exit2（启动freshness>120秒），13:04:48无wait复跑7/7 exit0；两错误保留。旧批不再重复apply/restart，未来自然成交仍未验。

### 11.12 SIM独立5%裁决与隔离实施（2026-11-21，未部署）

用户明确「目前是sim模拟交易，需要确保能交易开新仓，兼顾模拟交易和风控」，随后选择「SIM回撤5%」。约束原话：「真实峰值不重置，LIVE门槛、日亏闸门和止损均保持不变。」只在worktree实现热读`risk.account.sim.max_peak_drawdown: 0.05`；LIVE历史3%、两臂日亏1%减半/2%停新仓/3%拒新仓、原SL/TP、累计止损tripped、四锁及VIX来源/阈值均不动。新机制是既有AccountCB L1.5的有限预算消费者，不造平行CB，不强制造单。最终实际数量须扣已有仓权益回吐风险、初始SL与既有4%开盘包络、未来退出费/候选往返费/0.2%垫；缺账户/持仓/SL/新报价/锁/回执证据一律暂缓，不终结指令。

覆盖service与cron启用的EXT两真实SIM入场，检查在claim/send之前，账户thread+flock覆盖检查→提交→成交落账，退出/LIVE不取预算锁。固定工厂全局single-flight把懒SDK构造及只读取数放在worker，调用者3秒边界；提交前fsync唯一UUID单槽标记，明确零成交仅清自己的标记，UNKNOWN/partial/落账失败保留，terminal且精确BOOKED与完整账实一致才清，历史已平仓/跨日精确身份回执可恢复。EXT已知尚未发送暂缓只释放当前reserved attempt，不把机会封成整日failed。

预注册影子/隔离判据及并发误写方案六面纠偏详worktree同名DEB§11.12；dormant误写模块/独立DEB不纳入成果、不删除旧退出设施。源码前像`/tmp/sim5-budget-baseline-20261121-otemnxxe`；新的提交/部署/重启操作未获授权，不复用旧28批已执行的授权。

真实台账仅mode=ro：主线程亲读`/tmp/sim5-main-readonly-facts-20261121.log`（exit0），peak99999.496/equity96894.392、唯一LLY1918 LONG3/PLAYBOOK_SIM/order8386998/初始SL1341.4、open_positions sim3与5667 BOOKED/sim/8386998一致，四未决态0。5%账面余量1894.8708 USD；以周五陈旧收1347.11计算距离风险17.13+0.2%垫8.08266+真实SELL退出费2.0919=27.30456，扣后1867.56624仍需扣候选风险/往返费；不是fresh放行裁决。GLM汇总`/tmp/sim_entry_budget_verification-20261121.log`附录为事后重构、非原样可执行日志，环境文件读被拒后未重试，WAL只读可能触碰shm mtime，不能声称全文件mtime不变。

### 11.13 SIM5%隔离联合验收与冻结（2026-11-21，未部署）

**验证**：主线程最终11文件DB+全局真实socket/Futu构造器封锁，**452 passed/19.92s/exit0**，`/tmp/sim5-main-eleven-strict-final-20261121.log`；独立GLM七旧邻域**295 passed/17.52s/exit0**，`/tmp/sim5-regression-20261121-postfixture.log`；两日志亲读，均NET_GUARD_VIOLATIONS=0、SESSION_VIOLATIONS=0、SESSION_KNOWN_HITS=0、WAIVERS_CLEARED=True。集合重叠，不加总。政策Kimi归一化MEDIUM已关闭：real/live及空白大小写统一strip/lower后再读daily/peak，四例不同SIM/LIVE峰值证明不误读SIM峰值；主线程政策strict61日志`/tmp/sim5-acb-main-strict-fixed-20261121.log`，Kimi独立61/1.02s但未运行strict wrapper，不能混称其证据。

```bash
PYTHONPATH=/tmp:/opt/ftseek/FTSeek-US/.qoder/worktrees/trade-recovery-20261121 /opt/ftseek/FTSeek-US/venv/bin/python /tmp/sim5-strict-runner-20261121.py -p sim5_net_guard_20261121 -q tests/risk/test_account_circuit_breaker_pnl.py tests/risk/test_account_circuit_breaker_sim_budget.py tests/risk/test_sim_entry_budget.py tests/risk/test_sim_entry_budget_wiring.py tests/playbook/test_entry_data_bounded_fetch.py tests/playbook/test_exit_compensation.py tests/playbook/test_m2_b1_execution_state.py tests/playbook/test_live_arm_p0_contracts.py tests/playbook/test_live_probe_admission_exemption.py tests/playbook/test_dual_arm_routing.py tests/playbook/test_existing_position_ledger.py
```

上述命令只在隔离worktree运行，临时plugins须仍存在。首次七邻域**44 failed+251 passed/写库拒绝1**保留于`/tmp/sim5-regression-20261121-frozen.log`：仅补compensation预算seam/临时marker目录及live_probe实际临时CapitalSafety库，旧断言未改/豁免未重登记。早11文件**439 passed+1 failed**保留于`/tmp/sim5-main-eleven-strict-20261121.log`，唯一红为输入语义case在40ms调度裕量内误超时；只该case改test-only200ms，why/零写断言不变，其他timeout测试40ms和生产3s不变。更早287范围中286绿1红由临时runner缺__main__引发spawn递归，修runner后287绿，二日志保留；不把harness错误说成生产缺陷。

**价差口径分歧**：Kimi初审报MEDIUM，已有LONG取ask/SHORT取bid确实比立即平仓侧多占价差，但该实现有意预占券商净值标记上界→SL回吐，不是立即平仓价。如果权益mark在mid/last，直接改LONG=bid/SHORT=ask会漏算equity→可执行平仓的价差损失，故主线程未放松预算，新增8个多空bid-mid-ask区间→SL/跨stop及4个候选入场侧/委托价包络钉子，12例已纳入452。原预注册“可执行报价”应读为“报价区间保守权益标记上界”；宽价差保守误拦代价仍在，实际券商mark口径未证。**最终再裁补记（2026-11-21）**：Kimi明确撤销“报价方向错误”的MEDIUM及提交阻断建议。其独立算术确认LONG mark100/bid90/stop90时平仓侧修法只储备2.013费用、却漏记10美元权益回吐，SHORT对称；多占full-spread保留为非阻断可用性限制。主线程亲读最终独立`/tmp/sim5-independent-final-rerun-20261121.log`：315 passed/13.70s/exit0、SESSION_VIOLATIONS=0/SESSION_KNOWN_HITS=0/WAIVERS_CLEARED=True，覆盖新增12个钉子及200ms测试窄改。该运行未装全局NET插件，不能借用主线程NET0凭据；集合与452重叠不加总。本scope无HIGH/MEDIUM阻断，审查意见不等于用户提交/部署授权。

**凭据**：13 code/config/tests（9既有+4新），另两既有MD，不含dormant误写方案。SHA清单`/tmp/sim5-budget-baseline-20261121-otemnxxe/sim5-final-owned-scope.json`、同目录`sim5-owned-source.patch` SHA256 `e5a02a8270fdf7df8af4ea3caa66171a621663ac2340cf9b6092d31082de4a70`。12Python AST/undefined0、scoped diff-check/patch反向check通过；解析YAML去掉唯一新SIM键后与前像结构相等。生产9既有目标同前像、4新文件不存在；本轮仅此DEB/handoff纯MD同步，不部署Python/config、不重启、不提交推送或人工订单。

**未验证及下一步**：生产仍3%，SIM5%未生效；价差审查已闭合，须新本批交付授权、fresh在途出场及app/scripts WIP核，仅应用精确差集、systemctl受控重启/七条冒烟，再看有效信号自然entry→claim→send→fill→BOOKED，周末无成交不等于恢复交易。完整SDK/GIL/send/close/其他sizing有界性未证，worker不可取消；EXT无ID/generic remark缺精确身份仍需人工核证，gap/borrow/超垫滑点可能穿过有限预算。任务保持in_progress，旧28批授权不复用、不再次apply/restart，真实峰值/累计止损/原SL/TP及四锁保持原裁。

### 11.14 SIM5%本批授权交付与受控重启（2026-11-21 15:50 CST）

**授权/范围**：用户本轮明确「请应用部署和重启」，随后授权仅本批隔离本地提交、不推送及部署前审查（未发现安全问题）。本地提交 `a365e64cab7b963b8c98651292cf3711c3fd6d5e`，parent `fff29129a`，13 code/config/tests（9既有+4新）/1793增21删；`git -C /tmp/ftseek-sim5-budget-release-20261121 show --stat a365e64cab7b963b8c98651292cf3711c3fd6d5e` 可核，release工作树干净。两处既有MD另归账，不混入提交、生产HEAD/index未改变，无推送。旧28批不重复部署。

**差集/备份**：部署用标准补丁 `/tmp/sim5-budget-baseline-20261121-otemnxxe/sim5-canonical-release.patch`，SHA256 `eb99b6e8b619cae1ae0a9ae73afbd1951b23b0d1940726b6e7a98a492803724e`。原difflib补丁与标准git补丁patch-id不同，不冒称相等；12Python完整后像逐字相等，release父批缺少生产4行恢复注释，故只应用精确补丁、不用release整份YAML覆盖。生产13完整后像均匹配冻结清单，解析YAML仅多SIM5%键，既有注释保留。源码前像zip `/tmp/sim5-budget-source-before-deployment-20261121.zip`，SHA256 `13130499143da3f12dc0cfca003cbdcf99cb631f50d3c7930eee2eef1ba80534`；部署审计 `/tmp/sim5-budget-deployment-20261121.json` 为当前交付状态，早期freeze manifest未部署旗标保留其冻结时点含义。

**前置与应用**：15:44 prepared及15:49:26重启前最终mode=ro核：SIM唯一LLY1918 LONG3/PLAYBOOK_SIM/order8386998，LIVE无仓，四未决entry为0；`playbook_exits.db` 和 `exit_ledger.db` 中 `exit_intents WHERE state!='BOOKED'` 与 `exit_legs WHERE state!='TERMINAL'` 均0。应用13后像SHA匹配、12Python AST/undefined0、1303非本批app/scripts/main源码SHA不变，无他人晚于12:58当前进程的待上线源码；累计止损state/exit_policy字节不变，真实峰值与数据库不主动写入，不人工清锁或造单。

**启动验证**：`systemctl restart ftseek-us` 于15:50:21启动PID3762881，active/running；立即执行 `PYTHONPATH=/opt/ftseek/FTSeek-US /opt/ftseek/FTSeek-US/venv/bin/python /opt/ftseek/FTSeek-US/scripts/startup_smoke_check.py --wait 90`。15:50:41 **7/7 exit0**（ConfigValidator/漂移检测/热重载/SSOT零BLOCKING/双臂安全/退出快循环/零Traceback或CRITICAL），主线程亲读 `/tmp/sim5-budget-startup-smoke-20261121.log`。独立GLM于15:55 CST完成只读复核，主线程亲读其汇总日志 `/tmp/sim5-production-independent-20261121.log`（这是验收汇总，非完整原始stdout）。独立复算13后像/1303非本批源码/累计止损state/exit_policy/生产HEAD与index均一致，新启动后源码漂移0；实跑生产 `get_peak_drawdown_threshold`（sys.path首位生产root、断言模块路径、不构造CapitalSafetySystem）返回sim0.05/live0.03/real0.03，日亏0.01/0.02/0.03不变。LLY未平trade55字段、open_positions42字段与前像零差，SL1341.4保持；SIM capital_state peak99999.496/equity96894.392未变，last_update15:50:38是心跳刷新而非峰值重置。playbook_exits仍BOOKED443/TERMINAL690且未决0、exit_ledger空、SIM入场outbox14全BOOKED、LIVE无未平trade；LIVE open_positions仅US.MU size0历史残留，不冒称LIVE表无任何行。独立只读读取七条冒烟日志而未迟跑--wait，未调用网络或订单、未写真实DB。隔离452/295/315凭据沿用，不冒称本轮重跑，不加总重叠集合。

**边界/下一步**：生产SIM历史回撤配置5%，LIVE3%、日亏1%缩量/2%停新仓/3%拒新仓、真实峰值/原SL及TP/累计止损tripped/四锁/VIX参数保持。重启健康不代表周末有效信号自然entry→claim→send→fill→BOOKED已发生，陈旧报价和账面余量不是fresh放行裁决；SDK/GIL/send/close/其他sizing有界性、EXT无ID身份人工核证、gap/borrow/超垫滑点仍为局限。下一RTH仅观察自然链路，不再apply/restart、不强制交易、不重置峰值。若另获回退授权，只收紧SIM配置5%→3%并核热载；不整体覆盖数据库/其他WIP。任务保留in_progress至自然链验收，纯MD补记不触发重启。

## 12. DA新鲜度与可见性尺子修复（2026-11-21，本地应用，零正式输入变更）

**授权与边界**：用户「授权你检查 如有把握明确可调整优化 可以直接落地」。本轮仅修已有只读清单与记录型证据血缘，不把一般授权解释为撤销R2冻结、批准KR4实验名额或$90–130离线LLM预算。资金双路径错误门、正式时效过滤、跨语言去重、提示词、模型与采样、分级/方向及入场/出场门均未改。

**已明确修复**：`scripts/da_input_freshness_manifest.py` 旧实现每源只取最新浅层时间，非法时间亦能计asof，遗漏指数/peer/分时资金嵌套时点，混淆抓取、事件、日K与观测。现按原始记录盘点，合法且非未来的信息时点才计覆盖；发布时间/观测/日K日期与采集/供应商时钟/事件日期分列，空时点与坏格式分列，披露逐条未知、未来及最老/中位/最新年龄。`total_n/asof_n/missing_n`单位为raw中的源快照份数，`record_n/timed_record_n/unknown_record_n`为源记录数，role计数为显式时钟字段数，age为秒；三组基数分别自检，不混加。年龄参照二次回写的raw.saved_at归档时间，不冒充模型决策时点或实时行情延迟；日K日期按ET午夜解析，历史长窗口不是陈旧报价门。

**新闻证据修复**：仓外真实 `recommendation_evidence.py` 原FUTU固定index<8，R2已全量呈现，旧持久卡420条false与实际prompt矛盾。现在实际`_llm_output._user_prompt`优先；既有 `scripts/da_recommendation_evidence_replay.py` 传raw.user_prompt，按本源栏目完整标题行及呈现次数匹配，保留Finnhub同稿标签、源内引用ID去重。每条原始行先消费其呈现次数，再做引用去重，防止重复story留出“虚假可见名额”。无实际prompt时才共享DA_INPUT_FULL并标`CURRENT_RENDER_VIEW_NOT_HISTORICAL_PROOF`，不以今日配置证明历史可见。`visibility_basis`枚举的消费方为既有offline replay逐卡/汇总分布，不是新LLM字段或方向特征；同一批全部OBSERVED的基数退化仅表示同源取证。历史raw/analysis原样保留，不回填旧证据卡，不运行付费citation A/B。

**凭据/范围**：仅3既有Python载体+2既有tests（仓内manifest、offline replay及manifest test；仓外recommendation_evidence及其test），另本DEB/handoff既有MD归账。隔离载体 `/tmp/ftseek-da-input-repair-20261121`；5目标前像zip `/tmp/da-input-repair-before-20261121.zip` SHA256 `2284f7acaac2b9f171ce058a50fd01ed8430b82e85d33023b255a86623665d6b`；最终生产前像差集 `/tmp/da-input-repair-owned-final-20261121.patch` SHA256 `382f5023e039d3b89a52b9bf5a0a2e1b634c13d92cff516f63e9c3ee4ad65c7b`；before/after及保护清单 `/tmp/da-input-repair-scope-20261121.json`。已按SHA核对后精确更新现有本地载体，依赖hermes模块先于replay；未新建commit、未发布、未重启。production_applied仅指本地源文件已更新，不是服务部署。5后像匹配、6保护文件（五个DA输入/生产管线源及交易YAML）/74 checkpoint JSON/1306非本批app/scripts/main源字节不变，生产HEAD/index保持；交易PID3762881仍15:50:21启动。

**主线程验证及独立审查**：`PYTHONDONTWRITEBYTECODE=1 /opt/ftseek/FTSeek-US/venv/bin/python /tmp/da-input-test-runner-20261121.py`，最终 **47 passed/7.88s/exit0**，已亲读 `/tmp/da-input-pytest-final-47-20261121.log`。runner只在/tmp使用无凭据test-config，守卫拒绝.env、网络/子进程、非tmp SQLite和非tmp文件写，未放宽生产豁免。全部5文件AST/name检查清。Kimi最终静态审查无剩余严重/中问题，未代跑测试：时间溢出、重复标题次数、括号越界、原始重复story与引用去重交叉均补钉子；`/tmp/da-input-duplicate-story-red-20261121.log` 的1红+46绿保留，随后47绿。Kimi先前“[Breaking]自身漏配”未复现，已明确更正为短标题foo误可见；钉子须short→long才击中旧贪婪式，主线程实际两式结果[True,False]与[False,True]。两次临时pytest首次因O_TMPFILE打开/tmp、默认日志打开/dev/null被更严格守卫挡住，原日志保留first/second；仅把临时目录/日志显式导向/tmp子路径，未放行生产写。

**真实资料初核（GLM，最终真实载体另验）**：37 raw/37 analysis，701 FUTU+283 Finnhub=984条，保存prompt逐条可见，输入方向BUY4/SHORT24/SKIP9，非固定全空。news最小/中位/最大归档龄490/114624/15955793秒，旧“最新一条”的尺子掩盖旧料；kline2161条，34份19/19、1份18/19、2份17/19，VIX prev_close37份。5个peer price_as_of:null是源时点真缺，不是坏日期或模型错误。指数日K/盘前、个股日线排列/盘前异动为不同窗口；AVGO日线多头但盘前跌、UNH反向，并不自动证明推理错，不翻方向、不强制跟随大盘。

**真实载体初轮补记**：GLM直接运行原仓manifest/replay，未预绑定evidence/llm_synth、未覆写ROOT，纯test-config仅去掉凭据读取；主线程已亲读两个runner及 `/tmp/da-manifest-real-carrier-20261121.json`、`/tmp/da-replay-real-carrier-20261121.json`。37材料/984引用全部真实prompt可见、37候选及prompt/collected不变，5空peer计observed.missing_n=5而invalid_clock_n=0。合跑两真实测试虽 **47 passed/1.87s、pytest RC0**，但 `/tmp/da-input-pytest-independent-real-47-20261121.log` 的末尾全零守卫断言失败，**整个runner不是exit0**：49次.env.us读、97次Futu日志写尝试均在open前被拒，生产conftest捕获后pytest仍绿，不能称“零尝试”。hermes-only23例guard五项全零。初轮CPython pyc写被拒亦保留披露；显式dont_write_bytecode关闭，不放行生产__pycache__。独立runner允许/tmp目录O_TMPFILE及/dev/null，主线程更严runner只把临时目录/日志导到/tmp子路径，二者边界不混称。

**最终真实载体验收**：两真实测试加 `--noconftest` 排除其不依赖的生产基建，显式/tmp日志；`/tmp/da-real-pytest-run-noconftest-20261121.py` 完整stdout `/tmp/da-real-pytest-noconftest-stdout-20261121.log` 已亲读，**47 passed/0.55s/整个进程exit0**，env/write_non_tmp/socket/subprocess/sqlite_non_tmp五项计数均0、DETAIL全空、all-zero断言通过且conftest/app.*模块零导入。仅临时runner选项改变，不改生产conftest/凭据豁免或任何DA源码。原仓manifest/replay再验完整stdout `/tmp/da-real-manifest-run2-stdout-20261121.log` 与 `/tmp/da-real-replay-run2-stdout-20261121.log` 已亲读：真实__file__/默认ROOT、两hermes依赖路径断言成立，无module预绑定/ROOT覆写，37材料/984可见引用、prompt/collected零差，两个进程五项实测guard全零；只允许纯test-config，script自报llm/db/checkpoint=0不作独立测量。输出 `/tmp/da-manifest-real-carrier-run2-20261121.json`、`/tmp/da-replay-real-carrier-run2-20261121.json`。独立冻结复算 `/tmp/da-scope-sha-recheck-20261121.json` 及runner已亲读：5后像/6保护/74历史JSON/1306非本批源逐个SHA全匹配，HEAD/index不变，overall=ALL_MATCH；清单stage更新为local_final_sources_validated，只指本地载体验收，不是服务部署。任务5本轮诊断修复验收闭合，原沙箱环境错误、真实测试49/97拒绝及pyc拦截历史保留，不加总重叠47例。

**未验证/下一步**：未证明下一轮自然DA、趋势预测命中率/净收益改善；精确标题呈现不是独立因果证据，非同轮raw/prompt仍为回放局限。资金/时效行为改动依原DEB排序与R2预注册挂起，不改正式输入/模型/方向/交易门，不批准付费实验；任务3/4仅待下一RTH自然入场，不重复SIM部署/重启。

## 13.1 盘后反馈最小修复落地（2026-11-22，用户「请执行复查落地修复」）

**边界**：只修`daily_review.py`与`lesson_injector.py`，新增`tests/review/test_lesson_outcome_validation.py`；未改Hermes正式prompt/模型/analyzer（资金分布、短K UNKNOWN、健康空财报继续R2冻结）、reprice异常后的入场集合、scheduler/config或风险参数。未清历史教训、未提交/推送/重启。无观察时不改confidence/expiry；已平净损益证据仍按原公式影响legacy反馈，不能笼统称全系统零行为影响，也未证明DA正式prompt已消费legacy lessons。真实峰值不重置，LIVE门槛、日亏闸门和止损均保持不变。

**实现**：`daily_review.py:_generate_summary`补`summary.trades=closed_trades`（空为[]），兼容scheduler/review_loop旧读取且未平仓不混入；隔夜当日退出仍由原查询纳入。`LessonInjector.validate_lessons`对无盘前观察、仅未平仓或非法时间不加分、不续期；盘前及高信号已平样本按`pnl_net`，缺失按`realized_pnl-fees_est`；带offset转ET，naive按Playbook入场ET，04:00≤t<09:30，pytz非法DST时间按UNKNOWN。约束公式/入口未改，loader的active/ACTIVE与未来跨类别校验一致性留局限，不借机改生命周期。

**验证**：`/tmp/ftseek-da-feedback-repair-20261122/before/`保留前像；同一20例在保留旧代码上 **17 failed/3 passed**，含真正插入high_signal_loss后未平与毛正净负分支，日志`regression-before-final.log`，guard全0。最终原路径 `/tmp/da-feedback-regression-runner-20261122.py after` **20 passed/1.22s，整个runner exit0**，env/network/subprocess/non_tmp_write/sqlite_non_tmp全0，日志`regression-after-accepted.log`；临时DB，无网络/凭据/生产写。首轮45 pytest与最终扩展24 pytest虽绿，但各自whole-runner因1 env+28真实DB（首轮另1 Futu日志写）阻断，保留日志不作guard验收。Kimi静态审查无高严重度，指出高信号测试空fixture后已补真分支与closed net；其独立重跑before红及18旧例绿，不冒称其执行最终20 guard runner；补测后终审确认高信号/混合持仓/DST三问题闭合，无新高/中阻断。

**保护/下一步**：`/tmp/ftseek-da-feedback-repair-20261122/final_scope.json`记录3目标SHA；`scope-check.json`及stdout确认scheduler/config/auto_execute/llm_synth/deep_analyzer/真实DB 6保护SHA不变、AST与diff-check通过，5历史premarket_loss仍conf1.0/validation47,47,19,10,9/expiry12-05。未跑全仓测试、未验证自然复盘或收益提升；源码已落地但运行中PID仍15:41:48旧进程，**部署/重启未授权、未发生**，下一会话需单独核在途出场与他人WIP。

**用户授权重启后的验证（2026-11-22 21:35 CST）**：用户后续明确「请重启确认」，并选择Run L3 deep security review；L3返回0 findings。`restart-precheck.py`只读证明SIM唯一LLY1918 LONG3@1410.64/SL1341.4、LIVE无未平、playbook_exits与exit_ledger未决intent/leg均0；独立只读preflight `task10-readonly-preflight.{py,json,log}`证明app/scripts/main较旧PID新mtime仅本批2源，未将其他WIP视为待上线源（mtime不冒充内存字节码证明）。随后 `systemctl restart ftseek-us` 于21:35:52启动 **PID3849017**；`PYTHONPATH=/opt/ftseek/FTSeek-US /opt/ftseek/FTSeek-US/venv/bin/python /opt/ftseek/FTSeek-US/scripts/startup_smoke_check.py --wait 90` **7/7通过/exit0**，日志 `startup-smoke.log`。启动后两源SHA与验收一致、保护config不变；`restart-postcheck.log`确认LLY仍在、peak99999.496/initial77806.99/trade_env1不变，累计止损tripped/net/stop_line/baseline及5教训原值未改，LIVE无仓，KILL_SWITCH仍不存在。新启动日志未见warning/Traceback/CRITICAL。另一会话早前Skill(run)权限拒绝未执行重启，原线程基于本用户直接授权执行，不继承/借用拒绝路径。自然日复盘、长期准确率/盈利及自然entry→fill→BOOKED仍需后续观察，冒烟不代表这些已验证。

## 13.2 DA与盘前盘后策略再核（2026-11-22，只读审计）

**请求与边界**：用户「请再确认当前DA链路以及盘前盘后逻辑策略 是否有可调整优化提升准确率和盈利可能性的问题点」。本轮审计原仓及实际Hermes技能，不用研究worktree旧副本作生产缺陷证据；仅记原DEB/handoff与/tmp审计凭据，不改正式输入、模型、交易门、数据库、配置或调度，不付费调用LLM、不提交/推送/重启。R2冻结、C8–C18已裁轴和KR4名额保持；“真实峰值不重置，LIVE门槛、日亏闸门和止损均保持不变。”

### 13.1 主线程已复现的盘后教训缺陷及影响边界

- **取错报告字段**：`app/review/daily_review.py:138–142` 将逐笔记录置于顶层`report['trades']`，`_generate_summary:1141–1230`不产`trades`键；`app/core/scheduler_triggers.py:612–614`却读取`review['summary']['trades']`并默认空列表。原方法构造1笔已平记录，实际取值表达式收到0笔。此证据是原函数/表达式的隔离契约复现，不冒充整轮scheduler实跑。
- **无样本自证**：`app/review/lesson_injector.py:276–287`在没有盘前交易时仍计validated、confidence加0.05，`:320–325`续期并加validation_count。内存SQLite中0.50→0.55、validation_count 0→1已复现；无观察不能当统计支持。应将缺样、遵守建议与有证据支持分开，修复前不得用此置信度宣称盘前策略更准确。
- **时段错分**：`lesson_injector.py:338–346`按原字符串小时固定减13且截止09:00，忽略原时区/DST/09:00–09:30。三例：`2026-11-20T08:30-05:00`和`22:15+08:00`均误判非盘前；`2026-07-20T21:30+08:00`（ET09:30）误判盘前。正确方向是统一America/New_York及04:00≤t<09:30，不是修改交易准入时段。
- **不扩大因果结论**：DA `deep_analyzer.py:572–573`读`strategy_memory.get_strategy_hint`，其`:465–470`消费`lesson_log`；以上LessonInjector消费的是另一张`lessons`。尚不能把旧教训验证器缺陷称为DA提示词污染或亏损根因，也未授权清教训/放宽风控。实际活跃行及中间调用变异由独立只读复核补证。

**可复跑凭据**：`PYTHONDONTWRITEBYTECODE=1 /opt/ftseek/FTSeek-US/venv/bin/python /tmp/da-session-lesson-probe-20261122.py`，输出 `/tmp/da-session-lesson-probe-20261122.json`，主线程亲跑exit0、assertions=PASS。仅提取未修改AST方法与scheduler表达式、合成交易/内存SQLite；无生产模块导入、构造器或真实DB写。文件记录三源SHA；不是完整生产测试套件。

### 13.2 调度与半日市核对

现役载体源码`/root/.hermes/scripts/da_main_carrier.py:547`准入07:55–08:15 ET、到截止点不再开pipeline；Playbook `auto_execute_flow.py:414–427`同源读取开盘禁入09:45。`unified_config.yaml:1350–1379`确认门开启、120秒等待并保留20%对照臂，因此不能把所有订单一律说成09:47才可能进入。源码/配置不是逐笔真实放行证据。

`holiday_calendar.py:73/100`包含11-26休市与11-27半日市；`exit_monitor_flow.py:161–165`按日历计算收盘剩余时间，`exit_engine.py:810`消费最后8分钟。`scheduler_triggers.py:487`日报和`refresh_daily_bars.py:74`日K仍16:00后，半日市表现为复盘/刷新延后，**不构成已证的出场漏保**。未运行未来半日市实盘验证，不藉此修改隔夜政策或提前强平。

### 13.3 独立审查后主线程亲读确认的优化顺序

| 优先级 | 确定的问题/来源 | 建议及未证边界 |
|---|---|---|
| P1 正式输入真实性 | 仓外`futuapi/scripts/quote/get_capital_distribution.py:44–55`成功响应`code`为标的字符串；`deep-stock-analysis/scripts/llm_synth.py:624`与`deep_analyzer.py:304`都要求`code==0`，正常资金分布被跳过。`llm_synth.py:1137–1150/1168/1193`却可因非空code把此源计为有效。 | 原资金schema修复队列优先；先区分成功分布未呈现、分时fallback、源缺值。fallback存在，不能说完全无资金面；`main_in_flow='N/A'`的上游缺值与distribution消费错码并存，不能互相否定。正式prompt/analyzer修复改变R2输入，仍须原预注册，不自行上线。 |
| P1 计划到执行一致性 | `auto_execute_flow.py:947–957`在需要波动桶但ATR缺失时抛ValueError；`:1341–1346`只有EntryDataUnavailable会暂缓，普通Exception仅DEBUG。`:1481–1489`明示失败回退DB原值，`:2043–2046`传custom_params；`playbook_service.py:1379–1383`缺键回原entry/SL/TP/size。 | 先按异常类型/原参数回退/后续成交拆现有日志与审计，不能宣称每笔已按新价重算风险。chase-cap及后续门仍在，不是裸奔；本轮未统计触发频率或跑订单。改为暂缓会改变入场集合，须独立实施批准与回归，不能借此放宽止损。 |
| P2 盘后反馈真实性 | §13.1取错字段、空样本自证、时段错分均已复现。独立静态复核亦确认日报summary无trades契约。 | 修复必须区分“当日是否入场”与“已平净收益验证”：简单改读顶层closed_trades会漏掉未平盘前仓，简单塞入所有交易又不能把未实现收益当实得PnL。不得把遵守建议/缺样当效果证明，不自动清教训/改交易约束。 |
| P2 技术指标未知值 | `pipeline.py:305–346`筛选ATR走完成日线口径；`llm_synth.py:591–621`与`deep_analyzer.py:207–241`直接用raw末bar。短历史时prompt MA/涨跌补0，analyzer把no_data改“震荡”。 | 先披露有效根数/末bar日期/完成度可判定性；UNKNOWN不等于0或震荡。当前盘前实际不完整bar比例未测，不能说已污染；统一口径会改正式输入/趋势，按R2排队，不新增交易门。 |
| P2 财报负事实呈现 | `data_collector.py:875–885`有健康空日历结果；`llm_synth.py:998–1022`仅next_earnings非空才呈现。覆盖计有效不等于模型收到“该窗口无财报”。采集`:907–912`仍月份近似DST，渲染已用真实时区，两者不可混称。 | 区分健康空/源降级/真缺失，核窗口与来源再呈现；不把空日历说成37次API失败，不据此预测方向。补正式prompt属输入变化，非本轮直接改动。 |

Kimi本轮为静态审查，未运行套件、未读生产库/凭据、未启动生产模块；主线程已亲读上述决定性源码，而非只采纳摘要。`da_event_prompt_canary`报告生成与实际prompt注入是两个载体，报告还有preexecute下游消费者；本轮未独立证明当前开关/全部外部调度，仍记UNKNOWN，不以报告mtime或registry登记认定注入已停，也不擅自增设cron/实验名额。

### 13.4 当前经济证据、真实教训暴露及口径纠错

原仓评分载体与独立只读SQL均得到以下当前已平样本（`PLAYBOOK_SIM`、11-01起、A9主口径）；**pre/post指11-17分析师模型切换前后，不是盘前/盘后时段**。

| 来源/版本窗 | n | 毛bps | 费用bps | 净bps | 净额USD |
|---|---:|---:|---:|---:|---:|
| DA整窗 | 131 | -21.9856 | 9.5165 | -31.5021 | -1832.82 |
| DA pre（切换前） | 107 | -17.8310 | 9.2405 | -27.0715 | -1327.49 |
| DA post（切换后） | 24 | -44.2648 | 10.9970 | -55.2618 | -505.33 |
| EXT独立来源 | 47 | -20.0935 | 9.8567 | -29.9502 | -604.31 |

DA整窗14日簇，日等权均值-37.8699bps、CI80[-52.1116,-23.6282]、MDE80=31.1327bps；表内净bps为名义额加权，不能将日等权CI冒充其严格区间。KR1 NOT_MET；KR2死单29/131=22.1374%达≤23%线，**死单率达标不等于赚钱**。post仅24笔/3个有样本日，低于30/120，不能把观测负值断言为新模型造成恶化或已证明方向能力。毛收益也负，手续费不是唯一问题；fees_est未统一涵盖借券/资金/数据/模型成本，成交价已含滑点不得重复扣。原评分A9/A10敏感性标记/排除数见JSON，不把独立主口径SQL称为全组合敏感性重跑。

**纠正独立摘要两处表述**：其“盘前/盘后切分=11-17”应为版本切换前后；“转产决策延至12-15”没有裁决依据，12-15只是post攒够120的速率外推，原OKR仍§6按post达120或12月开跑会先到、§5在11-30结算不变。`--as-of`不设SQL上界，不是当日历史快照；不得用未低于MDE的基线差异宣称已显著回归。

**活跃教训暴露**：`/tmp/da-lesson-facts-20261122.json`为原库URI mode=ro+query_only等价SELECT，不调用含DDL的LessonInjector构造器。78条有效active/ACTIVE中5条premarket_loss（277/278/468/530/539），置信度均1.0、validation_count=47/47/19/10/9、expiry均12-05；说明有实际受此逻辑管理的行，不证明每次计数的历史原因。另5条文字命中不全属premarket_loss，不能混报“10条禁做教训”。DA的lesson_log是独立消费源；11-20的37份保存prompt未见“避免盘前”（GLM只读检查），不得用旧lessons缺陷指认DA方向污染。上述教训文本内的极端百分比或保证式收益是原库内容，未采信为事实或执行指令。

**凭据/安全边界**：主线程亲读 `/tmp/da-okr-scoring-run-20261122.py`、`/tmp/da-okr-scoring-20261122-stdout-20261122.log`（载体返回0，env/非tmp写/socket/subprocess/非只读sqlite五项guard均0）与 `/tmp/da-okr-scoring-2026-11-22-20261122.json`；`/tmp/da-independent-recompute-20261122.py`及同名JSON显示独立SQL mode=ro+query_only/finally close，数值一致。后者未安装同套audit hook，不混称实测五项guard均零。评分可复跑：`PYTHONDONTWRITEBYTECODE=1 /opt/ftseek/FTSeek-US/venv/bin/python /tmp/da-okr-scoring-run-20261122.py 2026-11-22`；独立复算用同解释器运行上述recompute脚本。无LLM/网络/订单、未改生产源码或DB。

**主线程追加实际prompt验真（不是仅采纳摘要）**：已运行 `/tmp/da-current-input-facts-20261122.py` exit0，完整产物 `/tmp/da-current-input-facts-20261122.json`含37份11-20 raw路径/SHA及逐份结果（仅json读取，无生产导入/DB/凭据/网络/模型）。37/37 distribution.code为US.*、8字段齐全而现有门为False，保存prompt的分布栏目0/37；分时栏目36/37，主力N/A36/37，**不是37份都进入fallback**。财报37/37实际为_quality=ok/next_earnings=None/lookahead_days=7，财报栏目0/37；不能以不存在的data键断言“日历源全缺”。日K36份60根、US.ANT仅1根且保存MA5/10/20/60均0.00，短历史退化已实际发生；末bar日期36份11-19、1份11-18，本样本未见11-20当日bar，其他批次完成度未验。37prompt均无“避免盘前”；event_context均injected=False/reason=brief_date_mismatch，说明本批实际未注入该事件块，不能把“env开”直接当已消费，亦不据历史批次证明当前开关或全部调度状态。独立检查原先无持久stdout，现以上述主线程产物补证；未复验559条lesson_log全文或其全部同义措辞，不作绝对无污染保证。

**未采信的追加摘要**：另一路复核将全量PLAYBOOK_SIM称为DA臂，且把近30日gap亏4504.86描述为LONG净亏3137.64的约一半，存在人群/算术矛盾，本轮不引用其跨期方向收益表。触发时正斜率不证明未成交空单整日错误或闸门已节省利润；历史LONG亏损不自动证明长期系统性负期望，SIM5%预算门通过也不保证周一可入场。未依该摘要重开C类闭轴、添加A/B/隔夜强平实验、换源或重启。

**结论/下一步**：先补事实/消费契约与计划执行一致性，再按已有预注册验证净经济增益，不用“空头更多”“信息更多”“思考更长”替代可执行收益证据。本轮只读审计完成后仅原DEB/handoff落档，未实施上表行为修复；R2/闭轴/实验名额不变。下一自然交易链仍按原任务3/4观察，不造单、不重复部署/重启，不把自然DA未运行的周末当失败或验收成功。
