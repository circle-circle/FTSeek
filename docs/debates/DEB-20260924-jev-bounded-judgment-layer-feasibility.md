# DEB-20260924: Jev（TypeSafe System One）作为 FTSeek「有界判断部件」— 可行性与 Phase 0 规格

> **状态**: 提案，**未落码**（本文 = L5 纯文档，零行为变更；Phase 0 若获裁，另立实现批次）
> **待裁**: 用户（§10 的 **F1–F9**，每条附推荐 + 保守替代）
> **2026-09-24 深夜更新（第 3 轮）**: 追查发现**阿里云百炼当天已上线同协议族模型** `decision-model-preview`（§4.5，E21–E26）；原"美国早期厂商 + waitlist + 数据外发"门槛**更正为一次控制台操作**（§11⑧）。**注意：仍未成功实弹调用过一次**，平台能力描述全部属自述。
> **类型**: 立项提案（若 Phase 0 落码则为 L1；本文本身零代码）
> **目标读者分层**:
> - **裁决者（用户）**: 读 §0 结论 / §10 待裁九问（F1–F9；新增 F8 开通申请、F9 首次实弹三连）
> - **执行者（下一会话的本人）**: 读 §5 规格 / §8 步骤与回滚
> - **复查者（第二 agent）**: 读 §3 证据表 / §9 复查清单 / §11 自曝，按 §9 逐条跑命令
> **前置**: 承接 2026-09-21 的 Jev 一手资料核查轮；仓内 `grep -rli "typesafe\|\bjev\b" app/ scripts/ docs/ config/` = **0 命中** ⇒ 无既有实现、无既有裁决冲突（§3 E1）
> **复核日**: 2026-10-21（与注册表现有 `recheck_after` 同日，便于一次裁完）

---

## 0. 一句话目标 + 可判定子目标

**目标**：决定要不要把「有界语义判断」从生成式 LLM 里拆出来（交给 Jev 一类决策模型，或本地等价物），以及**先在哪一个判断上做零风险验证**。

| # | 子目标（可判真假） | 判定方式 |
|---|---|---|
| G1 | 厘清 FTSeek 的"判断面"缺口究竟是**成本/延迟**还是**裁决带宽** | §3 E4（7 天 LLM 成本 $0.87）vs E3（19 个达标影子未裁） |
| G2 | 给出**零外发、零行为变更、可回放**的 Phase 0 规格 | §5 规格可独立执行；§9 C7/C8 给反例判据 |
| G3 | 厂商声明与第三方实测**分开标注**，不混作证据 | §4 逐行标 `[厂商]` / `[第三方]` / `[未实跑]` |

**本提案的直接结论（供快速裁决）**:
1. Jev 在 FTSeek 的**经济价值 ≈ 0**（我们七天全部 LLM 花销 $0.8675），"便宜 1000×"不构成理由；
2. 唯一站得住的理由是 **70–500ms 可进热路径** + **概率作为显式不确定性口径**（三路分叉：自动 / 升级 / 转人工）；
3. 但**裁决带宽瓶颈（19 个 READY 影子）不是"缺一个裁判"，是缺消费方与结算面**——换更便宜的裁判不解决"没人裁"（§12 反方 §12.1）；
4. 因此推荐：**Phase 0 先做本地可回放基线（零外发、零依赖、当天可跑），用历史标签问"这个判断到底有没有可测信号空间"**；只有基线显示"有空间"，才值得付"数据外发 + 早期厂商依赖"的代价去测 Jev 本体。
5. **交易前评估「要不要下单」（用户 2026-09-24 追加议题，§5.4）**：同轴已有两次不利读数（edge gate rank IC 双窗负 −0.396/−0.172；被拦候选反事实 T+1 +0.113% → `KEEP_SAMPLING`），且**语义判断的合法作用域只有 10.5%、去重后 0.5 个/交易日** ⇒ **不新开并列影子**，若要做只按 §5.4.4 挂同轴增量臂；并且**先裁时点**（F7）——若允许把评估提前到计划阶段，Jev 的唯一技术理由（延迟）随之归零。
6. **可用性前提已变（2026-09-24 追查，§4.5）**：**阿里云百炼今天 15:24 上线了同名协议族的 `decision-model-preview`（"决策模型（预览版）"，System One 协议，限时免费，RPM 1200）**，且我们账号**已有该平台的 key**。⇒ 原先把"美国早期厂商 + waitlist + 数据外发"当作前置代价的判断**不再成立**；现在只差**一个业务空间 ID（WorkspaceId）**（§4.5 实测三态）。**但注意：仍未完成任何一次成功的实弹调用**——能力描述目前全是平台自述。

---

## 1. 非目标（明确不做）

| ❌ 不做 | 理由 |
|---|---|
| 用 Jev 预测方向 / 收益（当 α） | 官方自己声明 API 能表达交易决策 ≠ 有预测力；第三方扑克实测显示它**不能从原始状态推结论**（须把结论喂进 state）⇒ 当预言机用即瞎猜 |
| 用 Jev 生成 thesis / 叙事 | 它不生成文本；叙事层仍归 DA（glm-5.3） |
| 用 Jev 做算术 / 费用 / SL 线 / 日期 | 官方 jaggedness 文档自曝算术、计数、日期不可靠 ⇒ 沿用既有纪律"触发判定代码算、禁 LLM 估" |
| 任何不经影子 + 真值回填就作用于真金白银的决策 | 承「死影子」先例（§3 E11/E12） |
| 本文改任何代码 / 配置 / cron | 本文是文档；Phase 0 另行立项 |

---

## 2. 问题陈述（含普查实测）

### 2.1 触发

用户问：FTSeek 的瓶颈之一是"既不想要死板的硬阈值，又怕 LLM 是瞎猜"，Jev 是否正好卡在中间。上一轮（9/24）已给出逐阶段映射；本轮要求把它落成可独立复查的 DEB。

### 2.2 普查：缺口到底在哪一层（先证明，不凭印象）

| 层 | 现状（实测） | 结论 |
|---|---|---|
| **成本** | 7 天全部 `is_test=0` LLM 调用 56 次 / **$0.8675**（E4） | 成本不是问题；"便宜 1000×"在绝对额上省不到 $1/周 |
| **延迟** | `agent_bridge` 均 59.5s（max 241s）、`universe_expansion_reasoning` 均 **106.9s**（max 433s） | 真问题：现有 LLM **物理上进不了盘中热路径**（70–500ms 才能） |
| **判断数量** | 候选 70–79 条/日（`playbook_instructions`，E7）、成交 3–16 笔/日（E8） | 体量中等；"给每一行打一个判断"的边际成本≈0 才有意义 |
| **裁决带宽** | 影子注册表 47 项 / 活跃 34 / **样本达标未裁 19**（E3） | **最真实的缺口**——但这缺的是"消费方 + 结算面 + 人的时间"，不是"更便宜的裁判" |
| **不确定性口径** | 现有门全是硬阈值；`min_confidence: 60` 只做**仓位缩放**不做拦截（9/21 实测：conf<60 的 129 行里 57 行仍成交） | 灰区**没有显式出口**：没有"升级人工"这一档 |

### 2.3 被普查推翻的初始设想

初始映射把"新闻/催化剂分类"列为 A 类首选（体量大、昂贵 LLM）。**普查证伪**：
- `news_classifier` 开关现值为 **False**（`app/llm/features/feature_flags.py:24`），消费方 `app/news_v2/processor/llm_classifier.py:140` 因此**未启用**；
- 历史语料几乎为空：`data/news_quality.db::news_classifications` = **37 行**，`data/news_v2/` = **0 文件**（E5）。

⇒ 新闻分类链**既不在跑、也没有可回放的历史标签**，做它既无收益也无验证手段。**撤下首选，改列入"待有语料再议"。**

---

## 3. 数据与实证表（每行作者已实跑，期望列 = 真实输出）

工作目录统一 `/opt/ftseek/FTSeek-US`；解释器 `./venv/bin/python`。

| # | 声称 | 复现命令 | 实测输出（作者实跑值） |
|---|---|---|---|
| E1 | 仓内无 Jev/TypeSafe 既有实现或裁决 | `grep -rli "typesafe\|\bjev\b" app/ scripts/ config/ ops/ ~/.hermes/scripts/ \| head` | 空输出（`docs/` 下只有本提案与其索引行自引用） |
| E2 | 穿透链的生产者/消费者落点 | `grep -rln "penetration_pattern" app/ scripts/ tests/`；`grep -rln "penetration_shadow" app/ scripts/ tests/` | 前者仅 `scripts/penetration_llm_analysis.py`（+同目录 `.pyc`）；后者含写者 `app/playbook/penetration_shadow.py`、注册 `app/playbook/shadow_registry.py`、判据/结算 `scripts/penetration_llm_analysis.py` + `scripts/penetration_outcome_backfill.py`、测试 `tests/test_penetration_shadow.py` |
| E3 | 影子注册表现状（裁决带宽瓶颈） | 见下方代码块 ① | `注册总数=47 活跃=34 样本达标(READY)=19 状态变迁通知=18` |
| E4 | 7 天 LLM 基线（成本/延迟） | 见下方代码块 ② | `56 次 / $0.8675`；`agent_bridge n=36 均59460ms max241389ms $0.1558`；`universe_expansion_reasoning n=19 均106904ms max433330ms $0.7062`；`penetration_pattern_analysis n=1 4075ms`；同期 `is_test=1` = 1028 行 |
| E5 | 新闻链未启用且无语料 | `grep -n "news_classifier" app/llm/features/feature_flags.py`；`./venv/bin/python -c "import sqlite3;print(sqlite3.connect('data/news_quality.db').execute('select count(*) from news_classifications').fetchone())"`；`ls data/news_v2/ \| wc -l` | `24:    "news_classifier": False,`；`(37,)`；`0` |
| E6 | 催化剂分类器路由 | `sed -n '340,342p' config/unified_config.yaml` | `catalyst_classifier:` / `fallback: deepseek` / `primary: qwen` |
| E7 | DA/UE 指令体量 | `./venv/bin/python -c "import sqlite3,collections;c=sqlite3.connect('data/ftseek_us.db');print(sorted(collections.Counter(r[0] for r in c.execute('select date from playbook_instructions')).items())[-8:])"` | `[('2026-09-15',79),('2026-09-16',71),('2026-09-17',72),('2026-09-18',75),('2026-09-21',72),('2026-09-22',68),('2026-09-23',71),('2026-09-24',72)]` |
| E8 | 成交体量（复盘/归因真值来源） | 同上换 `trade_records.trade_date` | `近 8 日 3–16 笔/日`；全表 894 行 |
| E9 | 穿透影子类别失衡（真读数） | 见代码块 ③ | `labeled=90`；`recovered_5m: {True: 88, False: 2}` ⇒ **88:2** |
| E10 | 穿透影子周分析读数 | `cat data/shadow/penetration_analysis_report.md` | `n_labeled: 42`；`v_rate_5m: 0.976`；`hypotheses_scored: []`（LLM 出题为空 ⇒ 不判）；`graduation_gate {n:20,matched:8,precision:0.7}`（源码常量回显） |
| E11 | 「死影子 / 读数虚增」先例 | `sed -n '229p' docs/debates/DEB-20260923-shadow-board-caliber-fixes.md` | `段10 近7天读数由 30min行1316 命中192 回到真实生产样本 30min行19 命中(修口径后)10 已结算9` |
| E12 | 结算面现状（补挂后已工作） | 见代码块 ④ | `early_dead_signal_shadow.jsonl 389 行 settled=381`；`high_atr_stall_shadow.jsonl 292 行（无 settled 字段）` |
| E13 | 现行裁决口径（必须引用，不许另立） | `sed -n '1,20p' docs/ADJUDICATED-CRITERIA.md` | `A1 影子攒样最小样本 n≥30 才评估; n≥60 才谈裁决`；`A7 促进/转正双窗口 双窗口 ≥40% 才采信`；`A8 转正四判据 OOS≥15日 ∧ 净均值>0 ∧ 毛利 t>1 ∧ ADF 平稳` |
| E14 | 新增调度/脚本的入账守卫 | `grep -n "def test_" tests/test_control_loop_ledger.py tests/test_ops_scripts_sync.py` | 台账文件 **10** 条，入账相关 4 条：`test_enabled_job_is_in_ledger` / `test_crontab_loops_are_ledgered_or_baselined` / `test_ledger_names_carrier_script_for_crontab_rows` / `test_crontab_baseline_has_no_ghost_entries`；同步文件 **3** 条（含 `test_repo_and_live_scripts_in_sync` 双向 sha256） |
| E15 | **本提案作者的一次口径事故（自曝）** | 见 §11 | 用 epoch 浮点与**文本时间列**比较，得到"近 7 天 `risk_blocked` = 5223"；实为**全历史** 5223，且最后一条在 `2026-05-22T03:40:54` ⇒ 该数字已作废，不再引用 |
| E16 | 执行门审计：拦截构成（**下单前判断的合法作用域**） | 见代码块 ⑤ | 窗口 7/17–9/23 / **49 个交易日** / 3824 行：`executed 432` / **资金·物理帽 2991（78.2%）**（chase_cap 1074 + notional_cap 919 + max_positions 779 + cycle_cap 214 + bp_cap 3 + duplicate 2）/ **信号质量类 401（10.5%）**（knife_gate 219 + fail 182） |
| E17 | 执行门前**去重真实体量**（唯一键 = `inst_id`） | 见代码块 ⑤ | `distinct inst_id` 全量 **473** = **9.7 个/交易日**；`executed` 去重 422（8.8 行/日 → 去重后 422）；**信号质量类拦截去重后仅 26 个 inst_id（≈0.5 个/交易日）** ⇒ 401 行主要是**逐轮重复计数**（轮询放大），不是 401 个事件 |
| E18 | 已成交笔的**真值回填路径已验证** | 见代码块 ⑥ | `executed` 行含 `decision_id` 427 条（去重后仍 427）→ **427/427 全部 join 到 `trade_records`**（100%）；`trade_records` 894 笔中 885 笔有 `pnl_net` |
| E19 | 同轴已有的**否定读数 ①**（下单前评分） | `sed -n '61p;319p' docs/debates/DEB-20260923-shadow-board-caliber-fixes.md` | `rank IC (预测期望 vs 实际笔均) 两窗皆负: 近7天 −0.396 (键数 24) / 近30天 −0.172 (键数 174)`；④ edge 门 `不放开也不实装` |
| E20 | 同轴已有的**未定读数 ②**（被拦候选反事实） | 代码块 ⑦ | `data/shadow/da_blocked_cf_history.jsonl` 末行：`n_signals=191, n=178`；`T+1 mean_signed_pct=+0.113%`、`win_rate=0.438`、`p_under=0.0576` → `verdict=KEEP_SAMPLING`（**被拦候选并不显著更差**，未越 ±0.15% 费用线） |
| E21 | **百炼已上线同协议族模型**（平台侧自述） | `GET https://dashscope.aliyuncs.com/api/v1/models?page_no=1&page_size=100`（Bearer `$DASHSCOPE_API_KEY`，翻完 518 条后过滤 `decision`） | 条目：`model=decision-model-preview` / `name=决策模型（预览版）` / `provider=qwen-domain-model` / `published_time=2026-09-24 15:24:10` / `context_window=65536` / `prices=[input_token 0]`（限时免费）/ 描述含"并行完成分类、是非判断与评分，并返回概率分布和置信度" |
| E22 | **协议与端点（官方文档）** | `https://help.aliyun.com/zh/model-studio/decision-model-api`（标题即"决策模型 System One API"） | `POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone`；body = `state` + `questions{choice\|noul\|score}`；**一次前向返回概率分布 + 置信度（choice/score 有 confidence，noul 即 p）**；"不生成文本，延迟与成本与输出长度无关"；限流 **RPM 1200 / TPM 2M**；上下文 65,536；最大输出长度 **0** |
| E23 | **端点实测三态（本提案作者实跑）** | 代码块 ⑧ | 通用域名 `/chat/completions` + `model=decision-model-preview` → **403 `access_denied`**；通用域名 `/compatible-mode/v1/systemone` → **404**（该路径不存在于通用域名）；工作空间网关（无效 ws）→ **400 `Workspace endpoint is invalid.`**（`<ws>.cn-beijing.maas.aliyuncs.com` 泛解析生效）⇒ 正确入口 = 工作空间网关，**当前缺口 = 业务空间 ID** |
| E24 | **对照臂：区分"模型不存在"与"无权限"** | 同 ⑧ 的同一端点逐个换 model | 自造名 `definitely-not-a-real-model-xyz-123` / `decision-model` / `qwen-decision` / `decision-preview` / `bailian-decision-model-preview` → **`model_not_found`**；而 `decision-model-preview` → **`access_denied`**（**错误码不同 ⇒ 该模型在平台确实存在**，不是我们编出来的名字） |
| E25 | 承载的是 **TypeSafe System One 协议**（文档指示用 TypeSafe 官方 SDK） | `curl -s https://pypi.org/pypi/typesafe-sdk/json` | 包存在：`typesafe-sdk 0.7.1`，summary = "Python SDK for TypeSafe AI API"，homepage `typesafe.ai`；百炼文档直接指示 `TypeSafeClient(base_url="https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode")` |
| E26 | **无新增厂商/密钥依赖** | `grep -oE "^[A-Z_0-9]*(DASHSCOPE\|BAILIAN)[A-Z_0-9]*" .env.us \| sort -u` | 已有 `DASHSCOPE_API_KEY` / `BAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1` / 8 个 `BAILIAN_MODEL_*`；`config/unified_config.yaml` 中 10+ 个 `llm.models` 走同一 key |

```python
# ① 影子注册表活体读数（只读：refresh_counts 不写盘，仅内存置 current_n）
import sys, importlib; sys.path.insert(0, "/opt/ftseek/FTSeek-US")
m = importlib.import_module("app.playbook.shadow_registry"); m.refresh_counts()
live = [s for s in m.REGISTRY if s.status.value in ("OBSERVING", "READY_TO_EVALUATE")]
print(len(m.REGISTRY), len(live), len([s for s in live if s.current_n >= s.min_n]))

# ② 7 天 LLM 基线（llm_calls.timestamp 是 epoch 浮点，不是文本）
import sqlite3, time
l = sqlite3.connect("data/llm_audit.db"); w = time.time() - 7 * 86400
print(l.execute("select count(*), round(sum(cost_usd),4) from llm_calls where is_test=0 and timestamp>=?", (w,)).fetchone())
for r in l.execute("select feature,count(*),round(avg(latency_ms)),max(latency_ms),round(sum(cost_usd),4) "
                   "from llm_calls where is_test=0 and timestamp>=? group by feature order by 2 desc", (w,)): print(r)

# ③ 穿透影子类别失衡
import json, collections
rows = [json.loads(x) for x in open("data/shadow/penetration_shadow.jsonl", encoding="utf-8") if x.strip()]
lab = [r for r in rows if r.get("status") == "labeled"]
print(len(lab), collections.Counter(bool(r.get("recovered_5m")) for r in lab))

# ④ 结算面现状
for f in ["data/early_dead_signal_shadow.jsonl", "data/high_atr_stall_shadow.jsonl"]:
    rs = [json.loads(x) for x in open(f, encoding="utf-8") if x.strip()]
    print(f, len(rs), collections.Counter(str(r.get("settled")) for r in rs if "settled" in r))

# ⑤ 下单前判断的作用域与**去重**体量 (exec_gate_audit)
rows = [json.loads(x) for x in open("data/shadow/exec_gate_audit.jsonl", encoding="utf-8") if x.strip()]
c = collections.Counter(r.get("action") for r in rows)
days = {r.get("date") for r in rows}
print(len(rows), len(days), c)                                    # 3824 49 {...}
print("全量去重 inst:", len({r.get("inst_id") for r in rows}))     # 473
q = [r for r in rows if r.get("action") in ("skipped_knife_gate", "skipped_fail")]
print("质量类行:", len(q), "去重 inst:", len({r.get("inst_id") for r in q}))   # 401 / 26

# ⑥ 真值回填可行性 (executed → trade_records)
import sqlite3
ids = {r.get("decision_id") for r in rows if r.get("action") == "executed" and r.get("decision_id")}
db = sqlite3.connect("data/ftseek_us.db")
q = f"select count(*) from trade_records where decision_id in ({','.join('?' * len(ids))})"
print(len(ids), db.execute(q, tuple(ids)).fetchone()[0])           # 427 427

# ⑦ 同轴既有反事实读数 (被拦候选)
last = [json.loads(x) for x in open("data/shadow/da_blocked_cf_history.jsonl", encoding="utf-8") if x.strip()][-1]
print(last["n_signals"], last["n"], last["verdict"], last["horizons"]["T+1"])

# ⑧ 百炼 decision-model-preview 端点探测 (2026-09-24 实跑; 全程 raw HTTP, 未经 app.llm ⇒ llm_audit 零新增)
import json, urllib.request
K = <DASHSCOPE_API_KEY>
def probe(model, url="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}).encode()
    req = urllib.request.Request(url, data=body, headers={"Authorization": f"Bearer {K}", "Content-Type": "application/json"})
    try:
        return url, json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return url, json.load(e)
print(probe("decision-model-preview")[1]["error"]["code"])            # access_denied
print(probe("definitely-not-a-real-model-xyz-123")[1]["error"]["code"])  # model_not_found
print(probe("decision-model-preview", "https://dashscope.aliyuncs.com/compatible-mode/v1/systemone"))  # HTTP 404
# 工作空间网关 (示例用无效 ws, 泛解析: 返回 Workspace endpoint is invalid.)
print(probe("decision-model-preview", "https://no-such-ws-test123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone"))
```

---

## 4. Jev 是什么 / 不是什么（来源分层，**含"未实跑"声明）

**声明**：本提案作者**没有实跑过 Jev**（TypeSafe 为 2026-09-15 发布、当前 waitlist/早期接入）。以下每条按来源标注；`[厂商]` 与 `[第三方]` 不得混作证据。

| 事实 | 值 | 来源类型 |
|---|---|---|
| 定位 | "System One" 决策模型：输入 state，输出**类型化决策 + 概率**；不生成文本 | `[厂商]` + 多篇 `[第三方]` 一致 |
| 三原语 | `Choice`（N 选 1，选项上限 255）/ `Score`（按 rubric 打分）/ `Noul`（P(是)，0–1） | `[厂商]` docs 一致 |
| 延迟 | 端到端 70–500ms | `[厂商]`；`[第三方]` 评论"与其并行采样设计一致，但测自其自有区域" |
| 价格 | 输入 $0.042/M tok（=$42/B），**输出免费** ⇒ 约 $0.0004/次 | `[厂商]` 公布价 |
| 加速/降本声明 | 快 20–200×、便宜 40–400×（部分二手文转述为"1000×"） | `[厂商]`；转述有放大，**采信区间取 40–400×** |
| 准确率 | 67.8%（对比 GPT-5.6 Terra 67.9%） | `[厂商]` |
| 第三方实测（中文客服 50 题） | **64–65%**；便宜小模型组第 2、强模型组垫底；最快 0.73s/题；阈值边缘抖动（1.99 vs 2.00 翻判据） | `[第三方]` |
| 第三方实测（扑克 vs 求解器） | 与最优解吻合 **63%**；**置信度倒挂**（错得最狠处 0.86）；**必须把结论喂进 state 才能判对** | `[第三方]` |
| 第三方实测（agent 轨迹分类） | 79%（19/24） | `[第三方]` |
| 官方自曝弱点 | 算术/计数/日期不可靠、多跳推理弱、无关上下文掉准、易被注入 | `[厂商]` jaggedness doc |
| 复刻易度 | 发布 2 天内 ≥6 个开源复刻；社区判断为"BERT 类编码器 + 校准奖励 RL" | `[第三方]` |
| **注意（与上一轮口述的一处出入）** | `Noul` **没有独立 confidence 字段**（`Choice`/`Score` 才有）；Noul 的 0–1 输出本身即概率 | `[第三方]` 集成说明 |

**尖点**：它的护城河是 **速度 / 成本 / 类型安全 / 校准声明**，**不是智力**（准确率≈廉价小模型）。校准是**群体属性**，厂商亦自认"单条 0.8 不保证对" ⇒ **必须在我们自己的标签上验**。

---

### 4.5 国内直连路径（2026-09-24 追查）：百炼 `decision-model-preview` — **同一协议族**

**一句话**：阿里云百炼于 **2026-09-24 15:24** 上线 `decision-model-preview`（"决策模型（预览版）"），**协议就是 TypeSafe System One**（`POST /compatible-mode/v1/systemone`，文档标题即"决策模型 System One API"，并直接指示使用 TypeSafe 官方 SDK `typesafe-sdk`）。⇒ 上面 §4 里"美国早期厂商 / waitlist / 数据外发"的门槛**大幅下降**：**我们账号已有该平台 key（E26）**。

| 维度 | 百炼 `decision-model-preview`（平台自述 + 文档） | 对 §4 判断的影响 |
|---|---|---|
| 能力 | 一次前向返回 **分类 / 是非判断 / 评分 + 概率分布 + 置信度**（`choice`/`score` 有 confidence，`noul` 即 p）；**不生成文本** | 与 Noul/Choice/Score 三原语**逐项对应**（§5.1 的三个问题可直接落形态） |
| 端点 | `POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone`（新加坡 `ap-southeast-1`） | 与通用 `dashscope.aliyuncs.com` **不是同一入口**（E23） |
| 价格 | **限时免费**（`prices: input_token = 0`） | 成本理由本来就已删除（§11⑥），此处只是"连省都不用谈" |
| 限流 | RPM **1200** / TPM **2,000,000** | 我们的量级（个位数/日）离限流线 4 个数量级 ⇒ **无配额焦虑** |
| 上下文 | 65,536 tok；最大输出长度 **0** | 与"不生成文本"一致；`state` 超长会被拒或截断 ⇒ 仍需最小充分集 |
| 不支持 | Function Calling / 结构化输出(标不支持) / 上下文缓存 / 批量推理 / 模型体验 / 训练调优 | ⇒ 只能当**判断部件**用，不能当工具调用层 |
| 求值形状 | `state`（文本/对象/数组）+ `questions{id: {type, instructions, criteria}}`；问题数不设上限（建议 ≤16） | 正好是"原子化问题 + 代码合成"的官方形态 |

**实测三态（E23/E24，本提案作者实跑）**：

| 探测 | 结果 | 读法 |
|---|---|---|
| 通用域名 `/chat/completions` + 该模型 | **403 `access_denied`** | 通用域名上不可达 |
| 通用域名 `/compatible-mode/v1/systemone` | **404** | 该路径只存在于工作空间网关 |
| `<ws>.cn-beijing.maas.aliyuncs.com`（无效 ws） | **400 `Workspace endpoint is invalid.`** | 网关泛解析生效；缺的是**业务空间 ID** |
| 对照臂：自造模型名 ×5 | 全部 **`model_not_found`** | ⇒ `decision-model-preview` 返回的是 `access_denied` 而**非** not_found ⇒ **该模型确实在平台存在** |

**结论（可行性判定）**：
1. **协议层完全匹配、可用性大幅改善**——比 §6 的方案 A（TypeSafe 直连）更现实：同一个厂商、同一个 key、国内、免费、限流宽。
2. **但仍未完成任何一次成功的实弹调用**：缺 **业务空间 ID（WorkspaceId）**；模型是否还需在模型广场点「立即申请」（预览版常见），**未验证**。文档对 403 `access_denied` 给出的两条解法之一就是"在模型广场对应该模型卡片点 **立即申请**"（另一条是 Token Plan 专属 key，我们不是这种情形）。
3. **纪律不变**：平台自述的能力**不等于**校准成立；第三方对 Jev 的 64–65% 读数**不能直接搬给这个模型**（同一协议族 ≠ 同一权重）。⇒ §5.3 的"自验校准"判据、§5.4 的同轴增量臂纪律，一个字都不放松。
4. **取证方式合规**：本次探测全程 raw HTTP（curl/urllib），**未经 `app.llm`**，故 `llm_audit.db` 零新增（唯一近 1 小时新增行是 21:12:41 的 UE 生产调用，与本探测无关）。

---

## 5. 方案设计（Phase 0 规格）

### 5.1 判什么（三个原子问题，禁多因子）

| 编号 | 原语 | 原子问题（一问一件） | 输入（最小充分集） | 真值（可回填） | 现有规则结果（对照臂） |
|---|---|---|---|---|---|
| **Q1** | Noul | "这笔候选**入场后是否净亏**（含费）？" | 方向 / 计划入场价 / 现价 / 当日涨跌 / ATR% / regime / 时段 | `trade_records.pnl_net`（894 行，E8） | 现役门链实际判定（放行/拦） |
| **Q2** | Noul | "该持仓**此刻继续持有是否比收口更差**？" | 当前浮盈 / MFE / 持仓分钟 / 距 SL/TP / 时段 | 用离场后 1 分 bar 回放（沿用 `penetration_outcome_backfill.py` 同法） | 退出引擎实际动作 |
| **Q3** | Choice | "这条告警属于 **良性噪声 / 真故障 / 待观察**？" | 告警指纹 / 类别 / 近 N 次同指纹 / 是否触发业务动作 | `issue_registry` 的持久化标记（同日/次日是否再发、是否被 `mark_resolved`） | 现役指纹+冷却规则 |

**为什么是这三个**：Q1 体量与真值都最硬（候选 70+/日、`pnl_net` 权威）；Q2 有现成的 backfill 管线可照抄；Q3 有 `issue_registry` 既有指纹去重可作对照臂（§3 E14 之外的 `app/core/issue_registry.py::is_duplicate`）。

**走 A′（百炼 System One）时的落形态（§4.5）**：三个问题**可并进同一次请求**（`questions` 是 map，加问题几乎不加延迟）——`Q1 → {\"type\":\"noul\"}`、`Q2 → noul`、`Q3 → {\"type\":\"choice\", \"criteria\":{\"benign\":\"良性噪声\",\"real\":\"真故障\",\"watch\":\"待观察\"}}`；`state` 只放三问**并集**的最小充分集。返回物里 **`choice`/`score` 带 confidence、`noul` 只有 p**（E22）⇒ §5.2 schema 的 `confidence` 列对 Q1/Q2 恒为 `null`，判据里**不许**给 noul 设 confidence 门。
**为什么不是"影子裁决"**：见 §12.1——元环裁决**没有客观真值**，它属"确定性地把证据整理成表"，不该走概率判断（这是我上一轮映射里的错项，见 §11）。

### 5.2 落盘与消费（纪律：一个写者 + 一个消费者 + 一个真值回填器）

- 产物：`data/shadow/jev_judgment_shadow.jsonl`（追加，一条 = 一次判断）
  ```json
  {"ts":"2026-10-01T13:31:02","et_date":"2026-10-01","q":"Q1","subject_id":"<decision_id|trade_id>",
   "state_hash":"<sha1(最小充分集)>","answer":0/1,"p":0.73,"confidence":null,
   "rule_result":"PASS|BLOCK","truth":null,"settled":false,"model":"local-baseline|jev|llm"}
  ```
- 采样口径：**每条候选落一行 + 每条成交落一行**（不许只落"命中阈值"的行——否则无对照臂、判据永不可裁，承 RFC-084 教训）
- `count_mode`：该影子必须用 `distinct:<subject_id>`（防逐轮落行虚增 `current_n`）
- 真值回填：settler 与**同一批**入 `shadow_registry` + 台账（承 E11/E12 死影子教训）
- **仓库不新增第二套台账**：注册进既有 `app/playbook/shadow_registry.py`（唯一 SSOT），不另建清单文件

### 5.3 预注册判据（**先于读数写死**，承 RFC-104 先例）

| 判据 | 门限 | 出处 |
|---|---|---|
| 样本 | `n≥30` 才评估；`n≥60` 才谈裁决 | 判据字典 A1 |
| 窗口 | 双窗口（≥2 段历史期）同向 | A7（双窗口 ≥40% 才采信） |
| 判别力 | 命中组 vs 未命中组**净均差 lift ≥ 1.3×**，且**两窗同向** | 承 RFC-104 词语表漂移教训：**绝对率会随基率漂移，只认 lift** |
| 校准 | 分箱 reliability：`p∈[0.5,0.6)` 组实测频率落在 `[0.4,0.7)`；`p≥0.8` 组实测 ≥ `0.65` | 无先例 → **本提案首次预注册，需用户裁**（F3） |
| 误杀代价 | 若用于拦截：被拦组的净均**不得优于**放行组（否则门在筛更差的单） | A4（统计显著 ≠ 可上门禁，须算误杀代价） |
| 判决方向 | 方向性结论**由本窗实数推导**，禁在渲染里写死结论 | DEB-20260923 格式化硬结论事故 |

**未过门 = 写"证据不足"，不写"负期望"**（判据字典纪律）。

---

### 5.4 追加议题（用户 2026-09-24 追加）：**交易前评估「要不要下单」能否做影子判断**

**结论先行**：技术可行（record-only、零风险），但**合法作用域极小 + 先验不利 + 前向攒样功效不足**。三项实测如下；若仍要做，唯一合规形态是**挂在同轴既有读数面上的增量臂**（5.4.4），不是新开一条并列影子。（上轮映射把它列为"🟢 干净赢面"，**此处下调**，见 §11 ⑦。）

#### 5.4.1 合法作用域：语义判断能碰的只有 10.5%，去重后 ≈0.5 个/交易日

| 拦截构成（`exec_gate_audit.jsonl`，7/17–9/23，3824 行 / 49 交易日） | 行数 | 占比 | 去重 `inst_id` | 语义判断可否翻案 |
|---|---|---|---|---|
| 资金 / 物理帽（chase_cap 1074 + notional_cap 919 + max_positions 779 + cycle_cap 214 + bp_cap 3 + duplicate 2） | 2991 | **78.2%** | — | ❌ **禁止**：翻案 = 让模型决定超帽加仓 / 绕持仓上限 |
| 信号质量类（knife_gate 219 + fail 182） | 401 | **10.5%** | **26** | ✅ 唯一合法作用域 |
| 实际执行 `executed`（真值来源，非拦截） | 432 | 11.3% | 422 | — |

⇒ **两个独立的事实在此同时成立**：①入口去重后 **9.7 个/交易日**；②语义判断真正能碰的那一类，49 个交易日里只有 **26 个 inst_id ≈ 0.5 个/交易日**（401 行是**逐轮重复计数**——轮询放大，不是 401 个事件）。

#### 5.4.2 体量与功效：前向攒样不可行 ⇒ 必须走历史回放

- 单靠前向采集：按 A1（n≥30 才评估）需 **≈60 个交易日（3 个月）**；n≥60 需 **≈120 个交易日**。
- 真值回填路径**已验证可通**：`executed` 的 427 个 `decision_id` → **427/427 join 到 `trade_records`**（E18）。
- ⇒ 唯一务实路线是**历史回放**（合用户 8/14 偏好"能回测就别攒样"），且必须**在现役门链基线上算增量**（承 post-blackout 基线纪律）。

#### 5.4.3 先验证据：这条轴上已有两次"否定 / 未定"读数

| # | 既有读数（同轴） | 含义 |
|---|---|---|
| ① | `edge_gate_shadow`（**本身就是一个"下单前期望值评估器"**）rank IC **两窗皆负**：近 7 天 −0.396（键 24）/ 近 30 天 −0.172（键 174）；且分桶与实际笔均无单调关系 | "用评分决定要不要下单"在这条轴上**已被测过一次，结论是负预测力**（DEB-20260923 §2.1/§8④：不放开也不实装） |
| ② | `da_blocked_cf`（被拦候选 T+1/T+3/T+5 反事实）：`n_eval=178`，**T+1 mean = +0.113%**、win_rate 0.438、`p_under=0.0576` → `KEEP_SAMPLING` | **被拦的候选并不显著更差**（未越 ±0.15% 费用线）⇒ "再拦一次"没有先验空间 |

⇒ 引 Jev 进来必须先回答"**为什么这次会不一样**"。目前唯一可能的答案：既有 edge gate 是**从历史拟合的数值模型**，Jev 是**零样本 + 校准概率**——但第三方扑克实测恰恰显示它**不能从原始状态推结论、必须把结论喂进 state**，所以这个答案**未经验证且方向不利**。

#### 5.4.4 若仍要做：唯一合规形态（同轴增量臂）

1. **不新开影子文件**：在既有 `data/shadow/exec_gate_audit.jsonl` **同轴加字段**（承"同轴同文件、加字段区分"纪律），避免第二套账本与 T14 同轴打架。
2. **判据必须是增量**：只在**已被现役门链放行**的子集上算 lift/AUC（残存效应），不是全量 lift。
3. **真值两臂分列**：`executed` 臂用真 `pnl_net`（E18，100% 可回填）；**被拦候选臂必须显式标 `synthetic=true`**（bar 回放构造，复用 `scripts/penetration_outcome_backfill.py` 同法 + `data/replay_bars/`），产物带 integrity 四键；两臂**不得混算成一个数字**。
4. **时点纪律（as-of）**：输入只许用**决策时点可得**的值 —— 直接受 DEB-20260914 定谳约束（"时点不清的值被当成实时值"是已实锤病灶），否则回放出的 lift 是假的。
5. 预注册判据：增量 lift ≥1.3× ∧ 两窗同向 ∧ 被拦组净均显著差于放行组（否则门在筛更差的单）∧ 增量臂 `n≥30`。

#### 5.4.5 先裁**时点**，再裁**模型**（本议题最关键的一问）

| 评估时点 | 可用模型 | Jev 的技术理由 |
|---|---|---|
| **门链检查那一刻**（毫秒级窗口，价格会漂移） | 只有 70–500ms 类快模型 | ✅ **唯一成立的位置**：现有 LLM 实测均 59.5s / 106.9s，物理上进不去 |
| **扫描 / 计划阶段**（提前几十秒~几分钟） | 现役 LLM 即可（qwen-flash / deepseek） | ❌ **消失**：延迟不再是理由，只剩成本 —— 而 7 天全部 LLM 花销 $0.8675（E4），成本理由已按 §11⑥ 删除 |

⇒ **若允许把"要不要下单"提前到计划阶段评估，本议题对 Jev 的需求自动归零**；反之若坚持在下单前那一刻，则必须承认这是一个**作用域 10.5%（去重 0.5 个/日）、已有两次不利读数**的小口子，且验证只能靠回放。

---

## 6. 方案对比（含"不做"）

| 方案 | 内容 | 赚什么 | 代价/风险 | 结论 |
|---|---|---|---|---|
| **A. 云 API 直连 Jev** | 用 TypeSafe/OpenRouter 走 3 个原子问题 | 立刻可测真机；70–500ms；类型安全 | **数据外发**（交易状态/候选外发第三方）；waitlist/无 SLA；早期厂商；中文弱 | **仅在 B 显示"有信号空间"后开** |
| **A′. 百炼 `decision-model-preview`（同协议族，2026-09-24 新选项）** | 与 A 同样的三原语，但走**阿里云百炼工作空间网关** `/systemone` | 与我们现有 key/厂商**同一套**；国内；**限时免费**；RPM 1200 无配额焦虑；协议与 SDK 与 TypeSafe 官方一致（§4.5） | 缺 **WorkspaceId**、可能需「立即申请」；平台自述能力**未实弹验证**；仍属外发（但外发对象是我们已在用的同一云厂商） | ✅ **A′ 取代 A 成为"要测真机时"的首选入口**（仍排在 B 之后） |
| **B. 本地可回放基线（零外发）** | 用**已有历史标签 + 传统分类器/规则集成**（不用 Jev）回答 Q1–Q3，算 lift/AUC/校准 | 当天可跑、零成本、零外发、可复跑；**先建立"这个判断到底有没有可测信号"的下限** | 不是 Jev 本体，测不到 Jev 的独有优势 | ✅ **推荐先做（Phase 0）** |
| **C. 本地自训校准头** | BERT 类编码器 / 梯度提升 + 校准，用 FTSeek 自己的标签训 | 无外发、无厂商依赖、可在自家数据上校准、无中文弱点 | 要标注+训练周期；当前标签量（894 笔）偏小 | 长期最优，**Phase 1 之后再议** |
| **D. 不做** | 维持硬阈值 + 人肉裁决 | 零成本 | "灰区无出口""裁决带宽"两个缺口原样保留 | 若 F1 选"不做"，须接受此后果并写进文档 |

**推荐路径**：`B →（有信号空间）→ A 小样对照 →（A 与 B 无差异且成本/延迟有真收益）→ C`。
若目标改为**交易前 veto**（用户 2026-09-24 追加议题）⇒ 不进本表的 A/B/C/D，走 §5.4.4 的**同轴增量臂**形态，并先裁时点（F7）。

---

## 7. 决策依据最小充分集（8 项，缺哪项就不得上产）

| # | 要求 | 本提案状态 |
|---|---|---|
| ① 固定样本来源 | ✅ 可复跑：`trade_records` / `penetration_shadow.jsonl` / `issue_registry` 均为固定窗口文件 |
| ② 多窗口 ≥2 段 | ✅ 规格已写（§5.3），实测待 Phase 0 |
| ③ 配对/对照臂（含更简替代） | ✅ 现役门结果作对照臂 + §6 方案 D 作更简替代 |
| ④ 尾部（最差 5% / 单笔集中度） | ⬜ Phase 0 必出：净均差之外必须给最差 5% 与单笔最大贡献 |
| ⑤ 费用后口径（净而非毛） | ✅ 用 `pnl_net` |
| ⑥ 机制诊断 | ⬜ 待定：需给出"为什么这组更差"的可解释量（如 MFE 分布 / 时段集中） |
| ⑦ 功效/显著性声明（n 与 MDE/t） | ⬜ **当前样本 894 笔、真值子集更小 ⇒ 大概率"证据不足"；须先算 MDE 再决定 n 目标**（不许用算术自洽的错 σ） |
| ⑧ 预注册判据 + 回滚 + 复现命令 | ✅ §5.3 / §8 |

⇒ **凑不齐 ⑦ ⇒ 只能作为"假设待攒样"进影子，不得上产**（本提案已如实标注）。

---

## 8. 实施步骤（每步独立可回滚；Phase 0 全程零交易行为变更）

| 阶段 | 动作 | 验证命令与期望 | 回滚 |
|---|---|---|---|
| **P0-a**（只读） | 复算 Q1 的历史 lift/AUC/校准（**不落新文件，先出一次性报告**） | `./venv/bin/python scripts/jev_q1_baseline.py --dry-run` → 打印 `n= / lift= / 分箱表`；若 `n<30` 直接停 | 无副作用（只读） |
| **P0-b** | 注册影子到 `shadow_registry.py`（`count_mode="distinct:subject_id"`、`min_n=60`、`graduation_criteria` 抄 §5.3 原文） | `./venv/bin/python -c "import app.playbook.shadow_registry as m;print([ (s.id,s.count_mode,s.min_n) for s in m.REGISTRY if 'jev' in s.id])"` | 删注册项 |
| **P0-c** | 采集器 + settler 同批落地；**同批入** `docs/CONTROL-LOOP-LEDGER.md` + 脚本回库 `ops/hermes/` | `./venv/bin/python -m pytest tests/test_control_loop_ledger.py tests/test_ops_scripts_sync.py -q` → 全绿 | 摘 cron + 删台账行 + `collection_retired=True` |
| **P0-d**（可选，需 F4+F8 批） | 同一批 state 同时打**百炼 `/systemone`**（`gateway="bailian-systemone"`，走 A′）与本地基线（`gateway="local-baseline"`） | 同文件出现两种 `gateway` 值；对比 lift/一致率/延迟 | 关 config 开关（`llm_features.jev_shadow.enabled=false`）**＋** 采集器不再发请求（网关侧零调用） |

**回滚两层**：
- **法子**：删 `shadow_registry` 注册项 → 摘 cron → 删台账行 → `collection_retired=True`（数据一律保留不删）
- **触发条件（出现即回滚，不留给下次讨论）**：
 1. 出现任何**由该影子产物而非事实闸**导致的下单（生产日志/DB 可见）；
 2. `已结算 M/N` 中 `M=0` 连续 **2 个交易日**（结算面停摆 = 假活跃）；
 3. 影子文件被**非采集路径**写入（判据：`ts` 值域出现常量或 9 位小数合成价，承 9/21 污染事故）；
 4. 判断被用于**自动执行**（而非只记录）且未经用户裁。

---

## 9. 复查清单（请第二 agent 逐条挑战；"反例判据"列 = 什么输出说明我错）

| # | 声称 | 复查命令 | 反例判据 |
|---|---|---|---|
| C1 | 仓内无 Jev/TypeSafe 既有实现 | `grep -rli "typesafe\|\bjev\b" app/ scripts/ config/ ops/ ~/.hermes/scripts/` | 出现任何命中 ⇒ 声称不成立。注：命令**刻意不含 `docs/`**（`docs/` 下会命中本提案与其 README 索引行这两个自引用文件，实测只有这两个） |
| C2 | 7 天 LLM 成本 $0.8675、56 次 | §3 代码块 ② | 数字偏差 >5% ⇒ 我口径错（注意 `is_test` 过滤） |
| C3 | `news_classifier` 未启用 | `grep -rn "is_enabled(\"news_classifier\")" app/ -A2`；`grep -n "news_classifier" app/llm/features/feature_flags.py` | 任一消费点实际在跑（日志/DB 有别处产出）⇒ 声称不成立 |
| C4 | 新闻语料无可回放量 | E5 命令 | `news_classifications` 或 `data/news_v2/` 出现 ≥1000 行/文件 ⇒ 我漏了采集面 |
| C5 | 19 个影子样本达标未裁 | §3 代码块 ① | 数目 ≠19 ⇒ 我口径错；**并请复核 `min_n` 与判据 n 是否同轴**（不同轴 = 我误读） |
| C6 | 穿透失衡 88:2（非 84:2） | §3 代码块 ③ | `False` 计数明显 ≠2 ⇒ 我读错样本（注意只算 `status=labeled`） |
| C7 | Phase 0 规格零行为变更 | 读 §8：全部动作只写 `data/shadow/` 与注册表 | 规格里出现任何写生产 DB / 改 config 生效值 / 下单路径 ⇒ 声称不成立 |
| C8 | 元环裁决"无客观真值" | 读 `app/playbook/shadow_registry.py` 的 `graduation_criteria` 与 `eval_script` 产物 | 若存在可程序化回填的客观真值字段 ⇒ 我的判断过强 |
| C9 | 我的口径事故（§11 E15）确实作废 | `./venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/ftseek_us.db');print(c.execute(\"select count(*),max(timestamp) from events where event_type='risk_blocked'\").fetchone())"` | 若真存在近 7 天大量 `risk_blocked` ⇒ 我的"作废"结论过头 |
| C10 | §4 的 `[厂商]/[第三方]` 标注与来源一致 | 抽查 §4 任一行，用其来源类型复核原始出处 | 出现"厂商数字当第三方实测"或反之 ⇒ 标注体系失效 |
| C11 | 百炼确有 `decision-model-preview`（且协议是 System One） | 代码块 ⑧ 前两条 + `GET https://dashscope.aliyuncs.com/api/v1/models?page_no=1&page_size=100`（翻完 518 条） | 若模型列表里查不到该条目，或 `decision-model-preview` 也返回 `model_not_found` ⇒ 我的"平台已上线"结论不成立（可能只是某条转述里的名字凑巧被风控拦下） |
| C12 | 该模型的正确入口是**工作空间网关**（非通用域名） | 代码块 ⑧ 第三/四条：通用域名 `/systemone` → 404；`<任意ws>.cn-beijing.maas.aliyuncs.com` → 400 `Workspace endpoint is invalid.` | 若通用域名其实也能调通（例如需要额外 header / 新版本已合并入口）⇒ §4.5 的"缺 WorkspaceId 是唯一缺口"结论过强，应改为"换入口即可" |

**点名请复查者挑战这 4 点**（不点名 = 复查退化为确认作者叙事）：
1. **§12.1 的"换裁判不解决没人裁"**——如果我说错，Phase 0 的顺序就要反转（先上 Jev，先做元环）；
2. **§5.3 的 lift 口径与校准分箱门限**——这是我首次预注册，可能把一个行业惯例写成了过严/过松的门；
3. **§6 推荐路径 B→A→C**——是否存在更省的路（例如直接用现役 `qwen-flash` 打同一批 state 做对照，而不必引入任何新模型）；
4. **§5.4.3 的"先验不利"结论（新增）**——我据此把"下单前 veto"从干净赢面下调；若我对 edge gate / da_blocked_cf 两条既有读数的**同轴性**判断有误（例如它们测的根本不是同一个判断），F6 应直接反转成立案。
5. **§4.5 的端点是排他的（新增）**——我据 404/400 断言"正确入口只有工作空间网关"。若百炼早已把 `/systemone` 合进通用域名（或需要不同 header/区域域名），则"只差一个 WorkspaceId"的结论要弱化为"只差一次正确调用"。

---

## 10. 待裁问题（frontier，逐条给推荐 + 保守替代）

| # | 问题 | 推荐 | 一行理由 | 保守替代 |
|---|---|---|---|---|
| **F1** | Phase 0 载体选 **B（本地回放基线）** 还是直接 **A（云 API）**？ | **B 先做** | 零外发/零依赖，当天可跑；若连历史回放都无 lift，则 A 不必测 | 若用户希望直接看真机：A 的 Q1 单点小样（n=200，成本 ≈$0.08） |
| **F2** | 首个打点选 **Q1/Q2/Q3** 中的哪个？ | **Q1**（入场后是否净亏） | 体量 70+/日、真值 `pnl_net` 权威、现役门链天然作对照臂 | Q3（告警噪声，零交易风险，但真值弱） |
| **F3** | §5.3 的**校准门限**是否照此预注册？ | 照此预注册 | 校准是本模式唯一卖点，必须自己验，不许采信厂商 | 先只记录不判定（`[只印不判]`），攒够 60 再定门 |
| **F4** | 是否允许**数据外发**给第三方 API？ | **先不许**；仅 B 通过后按最小充分集字段外发 | 交易状态外发涉合规/ToS；先外发字段白名单 | 全程不外发（走 C：本地自训） |
| **F5** | 影子裁决带宽（19 READY）怎么办？ | **用确定性代码出"证据表"**，不引 Jev | 元环无客观真值，判断形状是"整理"不是"概率" | 人肉逐条读（维持现状） |
| **F6** | 交易前评估「要不要下单」（下单前 veto）是否立案？ | **不新开并列影子**；若要做，只按 §5.4.4 挂**同轴增量臂** | 作用域仅 10.5%（去重 0.5 个/日）、体量 9.7/日、同轴已有两次不利读数；新开并列影子 = 第二套账本 | 先只做 §5.4.1/5.4.2 的"作用域 + 功效"复算（只读，零产物，当天可交） |
| **F7** | 「要不要下单」的评估**时点**放哪？ | **计划 / 扫描阶段**（提前） | 放这里 Jev 的必要性归零、可用现役 LLM、体量从 0.5/日 提到 70+/日 | 坚持"下单前那一刻"：接受 §5.4.5 的全部约束，并承认只能靠回放验证 |
| **F8** | 是否**开通/申请**百炼 `decision-model-preview`（§4.5）？ | **是——成本为 0，且这一步只解锁"能不能测"，不解锁"能不能用"** | 限时免费、同一 key、同一厂商、RPM 1200；唯一代价是**一次控制台操作**（拿 WorkspaceId + 如需要则在模型广场点「立即申请」） | 不开通：则 §4.5 永远停在"平台自述"，Phase 0 的 D 步（真机对照）无法执行 |
| **F9** | 若开通成功，**首次实弹**打什么？ | **只打"连通性 + 形态"三连**（不碰生产语义）：① `state` 里塞一段真候选 JSON 看是否 200；② 同一 `state` 问 3 个 `noul` 看返回的概率/置信度字段是否真存在；③ 同一 `state` 重发 3 次看**幂等性**（判断是否稳定） | 在**校准**之前必须先证明"它返回的东西就是文档说的东西"；③ 尤其重要——若同一输入三次答案不同，则"同一 state 可回放"这条纪律立刻失效 | 直接进 §5.1 三问的正式影子（跳过连通性三连）——不推荐，会把"接口问题"混进"能力读数" |

---

## 11. 自曝 / 更正记录（本提案推翻或不再引用）

| # | 我此前的说法 | 实测/更正 | 处置 |
|---|---|---|---|
| ① | "穿透判别 84:2 失衡" | 实测 `labeled=90`，`recovered_5m` = **88:2**；9/12 周报 `v_rate_5m=0.976`（n=42） | 全文改用 88:2；**此前的 84:2 不再引用** |
| ② | "新闻/催化剂分类 = 昂贵 LLM，体量大"（列为 A 类首选） | `news_classifier` 开关 **False**；语料 `news_classifications` = **37 行**、`data/news_v2/` = 0 文件 | **撤下首选**，改列"待有语料再议"（§2.3） |
| ③ | "近 7 天 `risk_blocked` 5223 条（高体量判断面）" | 5223 是**全历史**计数，最后一条 `2026-05-22T03:40:54` | 该数字**作废**；已改用字符串窗口重算（E15）。**根因**：`events.timestamp` 是 ISO **文本**列，我用 epoch 浮点比较 ⇒ 静默全表命中。教训：**报数前先验列类型与格式**（与"SQLite 时区三格式混存/字符串 date 比较虚增"同族） |
| ④ | "Jev 比 LLM 贵 ~1000×"、"Noul + 概率 + 校准置信度" | 厂商声明是 **40–400×**（二手转述才有 1000×）；`Noul` **没有独立 confidence 字段**（Choice/Score 才有） | §4 已按来源分层更正；采信区间取 40–400× |
| ⑤ | 上一轮映射把"影子裁决（元环）"列为 **✅✅ 最匹配** | 元环**缺客观真值回填**，不满足"每判断必配真值"纪律；其判断形状是"整理证据"而非"概率判断" | 降级为 F5：用确定性代码做，不引 Jev（§12.1） |
| ⑥ | "Jev 便宜 1000× ⇒ 值得引入"（隐含的成本理由） | 7 天全部 LLM 花销 **$0.8675** | 成本理由**删除**；改以延迟（70–500ms 进热路径）+ 显式不确定性为唯一理由（§0） |
| ⑧ | 全文（含 §0 旧版、§6 方案 A、§12.2"数据外发"行）把"**美国早期厂商 + waitlist + 数据外发**"当作引入 Jev 的**前置门槛** | 2026-09-24 追查发现**阿里云百炼当天已上线同协议族的 `decision-model-preview`**（System One 协议 + TypeSafe 官方 SDK，限时免费，RPM 1200），**且我们账号已在用该平台 key** | **前提更正**：门槛从"等 waitlist + 数据出境"降为"**一次控制台操作拿 WorkspaceId**"；§4.5 为新增章节，§6 增 **A′**，§0 增第 6 点，F8/F9 新增。**但纪律与结论不变**：仍未成功实弹调用过任何一次；平台自述能力≠校准成立 |
| ⑦ | 上一轮把「入场门/守卫（这笔候选是否 suspect）」列为 **🟢 A 类干净赢面** | 实测该轴：① 合法作用域仅 **10.5%**（78.2% 的拦截是资金/物理帽，翻案=让模型超帽加仓，**禁止**）② 去重体量 **0.5 个/交易日**（49 交易日仅 26 个 inst_id）③ 已有两次不利读数（edge gate rank IC 双窗负 −0.396/−0.172；da_blocked_cf T+1 +0.113% → KEEP_SAMPLING） | 下调为"**先验不利，须先证增量**"；立案形态限定为同轴增量臂（§5.4），并先裁时点（F7） |

---

## 12. 反方视角（自我对抗，三条必答）

### 12.1 这个改动会不会多出单 / 多赚钱？

**不会。** 已实测：因子 verdict 全负、毛期望 ≈0.2bps vs 费用地板 ~10bps。判定层换更快的裁判**不产生 edge**，只改变成本/延迟/不确定性口径。
**收益必须如实写成**："enables X"（显式灰区出口、可加判断数量、热路径可用）——**不许写 "improves WR"**。
**更要命的一击**：19 个达标影子未裁，缺的不是"裁判"而是"消费方 + 结算面 + 人的时间"。**把裁判换便宜，裁决欠账一条不会少** ⇒ 这是本提案最可能的失败点，也是 F5 独立于 Jev 单独裁决的理由。

### 12.2 新增的失败模式是什么？

| 新失败模式 | 机制 | 防线 |
|---|---|---|
| **概率被当硬阈值用** | 把 `p>0.8` 直接当门 → 用"有权威感的错误信心"替代"一眼可见的硬阈值"，比原来更危险 | §5.3 判据先验校准；上产前必须过误杀代价关（A4） |
| **又一个无结算影子** | 建了产物、settler 没挂 cron ⇒ 天天刷行、永远裁不了（E11/E12 前车之鉴） | §8 P0-c 同批入账 + 回滚触发条件 2 |
| **数据外发 / 厂商绑定** | 交易状态外发第三方；早期厂商无 SLA、waitlist | F4 默认不外发；路径 B/C 不依赖厂商。**2026-09-24 更新**：走 A′（百炼）时外发对象变成我们**已在用的同一云厂商**（E26），"早期厂商无 SLA"一项消失；但"交易状态出境/出域"本身仍需 F4 白名单把关 |
| **输入错配（喂太多）** | 官方自曝"无关上下文掉准确率" | 最小充分集（§5.1 输入列）+ `state_hash` 可回放 |
| **算术/日期被当判断** | 官方自曝算术/计数/日期不可靠 | §1 非目标已列为禁入 |
| **语义判断翻案资金帽（新增，承 §5.4）** | 若把 veto 接到门链上游或不限定作用域，模型可"说服"系统超 `notional_cap` / 绕 `max_positions` → 直接放大风险敞口 | §5.4.1 白名单：作用域只许 `knife_gate` / `fail` 两类，产物记 `scope="quality_only"`；越界即回滚 |

### 12.3 与已回滚 / 已证伪方案的关系（防重走）

| 既有结论 | 与本提案关系 |
|---|---|
| 三模型投票已废；`debate_engine/` 从未落地 | 本提案**不复活多模型辩论**；不加投票层 |
| 因子权重 verdict 全负；`premarket_invalidation` 仍 record-only | 本提案**不声称能救胜率**；不碰方向 |
| "缓存器造完实测收益 0 又回滚" | 本提案的 Phase 0 **先出读数再决定建不建**，不为设施而设施 |
| RFC-092 影子清账批（已执行，19 READY 收敛） | 本提案**不重裁** RFC-092；只补"待裁队列的消费者形态"一节（F5） |
| 阿里云百炼《决策模型 System One API》+ 平台模型列表 | 同协议族模型的端点、请求形态、配额（**2026-09-24 上线，平台自述**） | 不将其能力声明当已验证事实；不改任何现役 LLM 路由 | §4.5 / F8 / F9 |
| RFC-104（判据先于读数写死）/ DEB-20260923（死影子、污染、词语表漂移） | 本提案 §5.2/§5.3/§8 直接继承其纪律，不新立口径 |

---

## 13. 讨论记录

### 第 1 轮（2026-09-24，用户 + Hermes）

- **用户**: 要求把 Jev 逐阶段映射落成 DEB 文件，供独立复查后再动手。
- **Hermes（本提案）**: 落码前先做普查；普查推翻初始映射的三处（新闻链首选、84:2 读数、元环最匹配），并把"成本理由"整条删除（7 天 LLM 花销 $0.8675）。给出 F1–F5 待裁，推荐 B（本地回放基线）先行。
- **待补**: 用户裁决 F1–F5；如选 Phase 0 落码，另立实现批次并按 §8 分步提交。

### 第 2 轮（2026-09-24 同日晚，交易前评估议题）

- **用户**: 追加需求——"想利用它在交易前做一轮评估要不要下单，是否可以做影子判断，先分析再加入 DEB 文档"。
- **Hermes（本提案 §5.4）**: 先分析后落文档，分析结论三条实测：
 1. **合法作用域只有 10.5%**（78.2% 的拦截是资金/物理帽，翻案=让模型超帽加仓，禁止）；去重后该类 **26 个 inst_id / 49 交易日 ≈ 0.5 个/日**（401 行是逐轮重复计数）；
 2. **前向攒样不可行** ⇒ 只能历史回放；真值路径已验证（executed 427/427 join `trade_records`）；
 3. **同轴已有两次不利读数**：`edge_gate_shadow` rank IC 两窗皆负（−0.396/−0.172）、`da_blocked_cf` 被拦候选 T+1 **+0.113%** → `KEEP_SAMPLING`（被拦的并不更差）。
 ⇒ 新增 **F6**（不新开并列影子，只做同轴增量臂）与 **F7**（先裁时点：若允许提前到计划阶段评估，Jev 的必要性归零）；自曝 ⑦ 下调上轮"入场门/守卫＝干净赢面"的判断；§12.2 新增失败模式"语义判断翻案资金帽"。
- **待补**: 用户裁 F6 / F7；若仍要立案，另一批执行 §5.4.4 五条约束。

### 第 3 轮（2026-09-24 深夜，百炼 `decision-model-preview` 追查）

- **用户**: 告知"百炼已上线 Jev 类轻量决策模型 `decision-model-preview`"，要求**检查、测试、确认可行性并更新 DEB**。
- **Hermes（本提案 §4.5）**: 按"对照臂优先"做端点探测，结论三条：
 1. **模型确实存在且是同一协议族**——平台模型列表（518 条）有该条目（`qwen-domain-model`/65,536/限时免费/发布 2026-09-24 15:24），文档标题即"决策模型 System One API"并指示用 TypeSafe 官方 SDK（E21/E22/E25）；对照臂证明错误码可区分存在性（E24：自造名 → `model_not_found`，该名 → `access_denied`）；
 2. **尚未实弹成功**——通用域名 403/404，正确入口是工作空间网关但**缺 WorkspaceId**，且预览版可能还需「立即申请」（E23）；
 3. **原判断的前提更正**（自曝 ⑧）："美国早期厂商 + waitlist + 数据外发"不再是引入门槛；§6 新增 **A′** 与 F8/F9。
- **待补**: 用户裁 **F8**（是否开通/申请）；开通后由 Hermes 执行 **F9** 三连探测（连通性/字段/幂等），**未通过 F9 之前不写任何正式影子**。

---

## 附: 与既有文件的关系（防第二套账本）

| 既有文件 | 它覆盖什么 | 本提案**不重裁**什么 | 本提案依赖它哪一段 |
|---|---|---|---|
| `docs/ADJUDICATED-CRITERIA.md` | 已终裁判据字典（A1–A9/B*/C*/D*） | 不改任何既有判据；新增门限须用户裁（F3） | A1/A4/A7/A8 |
| `docs/decisions/RFC-092-*.md` | 影子注册表清账（19 READY 状态收敛） | 不重裁其收敛结论 | §2.2 裁决带宽现状；F5 |
| `docs/debates/DEB-20260923-shadow-board-caliber-fixes.md` | 影子看板口径纠偏、结算环补挂、污染隔离 | 不重裁其口径结论 | E11/E12、§5.3 判决方向纪律 |
| `docs/decisions/RFC-104-*.md` | ETH 微观结构三影子（判据先于读数） | 不重裁其判据 | §5.3 "判据先于读数写死" |
| `docs/PROMPT-CLAIM-REGISTRY.md` | Prompt 断言 ↔ 真实代码消费方登记表 | 不重裁 | §12.1：该表即"新闻链无消费方"同类问题的既有人工机制 |
| `docs/CONTROL-LOOP-LEDGER.md` + `tests/test_control_loop_ledger.py` | 调度载体入账 SSOT | 不新增第二套台账 | §8 P0-c 入账步骤 |
| `DEB-20260923-shadow-board-caliber-fixes.md` §2.1 / §8④ | **edge 门**（下单前期望值评分）分桶复核与"不放开也不实装"结论 | 不重裁其结论 | §5.4.3 先验证据 ①（同轴负预测力） |
| `app/decision/edge_shadow_gate.py` + `data/shadow/exec_gate_audit.jsonl` | 现役下单前评分器与执行门审计产物 | 不重裁其口径 | §5.4.1 作用域表 / §5.4.4 同轴加字段对象 |
| 影子 `da_blocked_cf`（判据字典 B14） | 被拦候选 T+1/T+3/T+5 反事实 | 不重裁 | §5.4.3 先验证据 ② |
| `DEB-20260914-premarket-data-validity-audit.md` | 数据**时点**口径定谳（time-of-point 混用是实锤病灶） | 不重裁 | §5.4.4 第 4 条 as-of 纪律 |
| 阿里云百炼《决策模型 System One API》文档 | 该模型的端点/请求形态/配额/限制（**平台自述**） | 不将其能力声明当已验证事实 | §4.5 全节 / §5.1 三问的落形态 |
| `typesafe-sdk` 0.7.1（PyPI） | TypeSafe 官方 Python SDK（百炼文档指定用它） | 未安装、未引入依赖 | §4.5 E25：证明"同一协议族"而非"同名巧合" |
| `.env.us` 的 `DASHSCOPE_API_KEY` / `BAILIAN_*` + `config/unified_config.yaml` 的 `llm.models` | 现有百炼接入面 | 本提案**不改任何 key/模型路由**（零行为变更） | §4.5 E26：无新增厂商依赖 |