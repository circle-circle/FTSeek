# DEB-20260918 · 质检口径与 LLM 思考预算批（Codex 批落档 + 独立复核结论）

**范围**：本文件为工作区里"另一主题的未提交改动"补落档 —— 这批改动在 `DEB-20260918-extended-hours-ssot-and-observability.md` §23.4 曾被登记为**未提交、无落档**（当时拒绝连带提交）。2026-09-18 经独立复核（逐行读码 + provider 实弹 + 生产审计取证 + 同库同窗口 A/B）后落档并按主题提交。

**结论一句话**：这批改动方向正确、证据成立；其中 `reasoning_effort: high` 经实弹确认**不会触发静默 fallback**，豁免改动经 A/B 确认**只豁免已知坏行、没有挪门柱**。复核另外发现并已修掉 3 项：双实现分叉、缩进/同文件卫生缺口、以及**两项测量口径变更此前无任何落档**（本文件 §3 即为该落档）。

---

## 1. 改动清单与判定

| # | 改动 | 文件 | 判定 | 关键证据 |
|---|---|---|---|---|
| 1 | GLM-5.3 `reasoning_effort: high`（UE 思考预算上界） | `config/unified_config.yaml` | ✅ 安全、依据成立 | 见 §4 |
| 2 | 中性 `data_quality` 行豁免（质检读数） | `scripts/daily_invariant_check.py` | ✅ 合规 | 见 §3.1 |
| 3 | 观测端点并入极值（MFE/MAE 口径） | `app/playbook/pipeline.py` | ✅ 正确，⚠️ 口径位移需切线 | 见 §3.2 |
| 4 | 极值口径 SSOT 合并（复核新增） | `app/core/trade_extremes.py`(新) + 两处调用方 | ✅ 消除双实现分叉 | 见 §3.3 |
| 5 | 受控修补工具 | `scripts/repair_trade_exit_invariants.py`(新) | ✅ 安全设计合格 | §5.2 |
| 6 | 配置快速校验 | `scripts/config_validate.py --quick` | ✅ 附加式，不改默认路径 | §5.1 |
| 7 | `except` 块告警补栈（W6 棘轮） | `app/data/futu_lv3_provider.py`、`app/pipeline/order_executor.py`、`scripts/settle_winner_overnight_shadow.py` | ✅ 且补齐了同文件漏改 1 处 | §5.3 |
| 8 | 回归测试 | `tests/test_invariant_d4_settled_fields.py`、`tests/test_config_validate_quick.py`、`tests/test_trade_exit_invariant_repair.py`、`tests/coordinator/test_model_call.py` | ✅ 115 passed | §6 |

---

## 2. 为什么必须落档（本批的元教训）

三项改动**改的是读数，不是交易**（质检豁免数、MFE/MAE 基线、截断率），所以它们不会在交易链路上暴露问题，却会让**跨日期对比失真**：

- 豁免一旦生效，`violations` 与 `sample` 的语义变了 ⇒ 历史"违反数"与本日不可直接比；
- 极值口径一旦生效，`mfe_pct` 只增不减 ⇒ 凡"按 MFE 阈值分组"的复盘/影子读数跨切线日不可比；
- 截断率取决于 `max_tokens` 与思考预算 ⇒ 配置变更后 7 日窗口内会混两种口径。

**规则**：改读数的改动，必须同时落档三件事 —— ①改了哪条读数 ②切线日期 ③哪条后验指标能证伪它。缺任何一项，下一批复核就只能在"数字对不上"里打转。

---

## 3. 测量口径声明（本批最重要的一节）

### 3.1 中性 `data_quality` 行豁免（切线日 2026-09-18）

- **改了什么**：`daily_invariant_check` 的"主不变量"抽样循环原先只对 `settled`（结算）段豁免已知坏行；现在主段同样跳过 `data_quality` 前缀为 `duplicate/outlier/invalid/void/excluded` 的行，并以 `sample_exempt_marked` 显式计数（透明度保留），`sample` 分母不变。
- **为什么不是挪门柱**（同库同窗口 A/B，输出与基线全部重定向到 /tmp，只读生产库）：
  | 版本 | sample | sample_exempt_marked | violations | rc |
  |---|---|---|---|---|
  | HEAD（旧） | 47 | —(无此键) | `I1_pnl_vs_prices×2`、`W1_kr2_cohort_broken×1` | 1 |
  | 本批（新） | 47 | **2** | `W1_kr2_cohort_broken×1` | **1（仍红）** |
  消失的 2 条正是 `data_quality='duplicate_external_close'`（如 id=1511 US.MRCY：`realized_pnl`/`pnl_net` 故意留 NULL 的"外部平仓重复"类）⇒ 与文件既有坏行豁免同口径；既有测试 `test_normal_data_quality_labels_are_not_exempt` 钉住 `real/clean/manual` **不豁免**。
- **切线含义**：`violations` 的历史序列在 2026-09-18 前后不可直接比；比值口径请用 `sample_exempt_marked` 显式剔除后再比。
- **复核踩过的坑（登记以免后人重犯）**：把 HEAD 版脚本复制到 `/tmp` 跑对比时，`REPO` 派生常量指向 `/tmp`，`W2` 基数自检找不到 `scripts/shadow_field_cardinality.py` 而报 `detector_error` —— 那是**搭台伪影**，不是被复核的改动。做 A/B 必须让两个版本都在仓内运行、只重定向写路径。

### 3.2 MFE/MAE 极值口径（切线日 2026-09-18）

- **改了什么**：平仓写记录时，把观测到的入场/出场价并入 `peak_price/trough_price`（含 `*_bar` 两套键），再据此算 `mfe_pct/mae_pct`。方向语义经核对与 `app/risk/atr_stop_engine` 注释一致：LONG `peak=最高价`，SHORT `peak=最低价`；参数位置也核对过（`_tr[1]`=direction、`_tr[2]`=entry_price，不是"看起来像"，是调用点同用法：`pipeline.py:6418/6464`、`:6463/6571`）。
- **实盘命中面**（近 300 条了结行）：`peak_price/trough_price` 填充 295/300、`*_bar` 291/300 ⇒ 多数是"填补 0 值/收紧"，非凭空造键；**63/300（21%）的极值会被改写**；5/300 两套键都缺 ⇒ 由本改动**新建**（读数上无法区分"实测 bar 极值"与"只看到端点"，属可接受的保守下界）。
- **切线含义（必须遵守）**：折入端点 ⇒ **MFE 单调抬高、MAE 单调加深** ⇒ 跨切线日的 `mfe_pct/mae_pct` 统计不可直接比。已知受影响消费方：`app/decision/edge_weekly_report.py:215`（give-back 比值 `mfe_pct > pnl_pct*100`）、任何按 `trade_records.mfe_pct` 阈值分组的复盘/影子读数。
- **不受影响**：`app/playbook/exit_engine.py:935/976` 的 `dead_signal`/`mfe_stall` 用的是**在仓** `peak_profit_pct`（活状态），不是本改动写的 metadata 副本；`_include_observed_prices_in_extremes` 收到的 `metadata` 是 `json.loads` 出的新副本，**不污染在仓风控对象**。
- **元数据新增镜像键**：`metadata.mfe_pct/mae_pct` 与列同值（4 位舍入）。注意**列的 `mfe_pct` 仍是权威**；既有 10/300 行 metadata-pct 与列 pct 已不一致（先存在，非本批引入），引用时优先取列。

### 3.3 极值口径 SSOT 合并（复核新增，行为中性）

- **问题**：同一件事曾有两份实现 —— `pipeline._include_observed_prices_in_extremes` 与 `repair_trade_exit_invariants._include_observed_prices`，且对**同一类输入动作相反**：极值字段存在但不可解析时，管线版按缺失处理并**覆盖**，修补版 `continue` **跳过**保留原值。
- **处置**：实现下沉到 `app/core/trade_extremes.include_observed_prices`，管线留薄壳（调用点与既有测试不提签名），修补脚本改为 `import`（并自行把仓根插入 `sys.path`）。口径**裁决取管线版**（不可解析=缺失并覆盖），理由是静默保留脏值会让极值永远卡在垃圾值上。
- **行为中性证据**：修补窗口（`trade_date>=2026-09-11`、非中性行 45~47 行）内**非数值极值 0 处** ⇒ 两版在现网数据上差异命中 0 次；合并后实跑修补工具仍 `0/0 counts_match=true rc=0`。
- **防复活**：`tests/test_trade_extremes_ssot.py` 钉住 ①唯一实现口径（LONG/SHORT、0/负=缺失、只在变了才写、不可解析=缺失）②管线薄壳与唯一实现**逐例同值**③修补脚本**不得再定义本地副本**且引用同一函数对象。

---

## 4. LLM 思考预算：`reasoning_effort: high`（切线日 2026-09-18）

**通路核实**（逐行）：`app/llm/model_call.py:148-149 payload.update(extra_body)` 发生在 `:159-160 setdefault("enable_thinking", False)` **之前** ⇒ `enable_thinking=true` 与 `reasoning_effort=high` 都进请求体（yaml 注释里的行号声明属实）。

**依据核实**（生产 `llm_calls`，只看 `is_test=0`）：`feature='universe_expansion_reasoning'` 且 `model LIKE '%glm-5.3%'` 共 **9 笔，其中 6 笔触顶**（`tokens_out>=16000`，最高 **24000**），错误串全部为 `schema_failed_on_glm-5.3 finish_reason=length` ⇒ "不传 effort 时无界思考烧满输出预算并 schema_failed" **成立**。

**provider 实弹**（不经过仓库代码，排除 payload 组装干扰；同一提示词三臂）：

| 臂 | HTTP | completion_tokens | reasoning_tokens | finish_reason | JSON 有效 | 延迟 |
|---|---|---|---|---|---|---|
| 不传 effort | 200 | 4000（本人设的上限） | **3998** | **length** | **否（截断）** | 56.0s |
| `reasoning_effort=low` | 200 | 688 | 4 | stop | 是 | 9.8s |
| `reasoning_effort=high` | 200 | 894 | 39 | stop | 是 | 11.1s |

结论：**①`reasoning_effort` 被 dashscope 接受（200，非 400）⇒ 不会触发"400 → 静默 fallback"这一最坏失败模式**（该失败模式会使本改动变成空操作且无人察觉）；②不传时确实烧满预算并截断；③`high` 完整收尾且 JSON 有效。

**未复现的部分（如实登记）**：Codex 报告的绝对值（`high` 3256 tok/34s、`low` 1727/18s）在本次提示词下不可比（本探针提示词小得多：`high` 894 tok/11.1s；**方向一致、绝对数不可比**）。**边界**：`schema_failed` 还有第二种成因 —— **键名漂移**（探针里 glm 返回 `affected_tickers`、deepseek 返回 `impacted_tickers`，而契约要求 `surfaced`，发生在**远未触顶**时）⇒ 本项只覆盖"触顶型"，不要把它写成"UE schema_failed 已解决"。

**后验验收（可证伪，不需新建哨兵）**：已有仪表就是 `scripts/daily_llm_token_report.py`（cron `20 6 * * 2-6 --push`）—— 它已含「当日 per-feature 截断率」与「7 日 per-feature 截断率」两段，判据为 `error_msg LIKE '%finish_reason=length%'` 且 `n>=10`。实测 **9/9 触顶行都带该标记** ⇒ 该表看得见本类失败（无需双份哨兵）。
**验收判据**：`universe_expansion_reasoning` 的 7 日截断率应显著下降（改前读数 6/9 触顶）；若 7 日内仍 >5% 且 n≥10，则本项**证伪**，应回到 `max_tokens`/思考预算复核。**读数注意**：窗口跨切线日时是新旧口径混合。

---

## 5. 工具与卫生

### 5.1 `config_validate.py --quick`
新增快速档（YAML 解析 + `ConfigValidator` 严格校验），默认路径不变。回归：`tests/test_config_validate_quick.py`。

### 5.2 `repair_trade_exit_invariants.py`（受控修补）
- 默认 **dry-run 走内存副本**（不碰生产库）；`--apply` 先做 SQLite online backup 到 `data/backups/`，再 `BEGIN IMMEDIATE` + 异常回滚；
- `--expected-duplicates/--expected-extremes`：命中数与预期不符 ⇒ **exit 2**（机器可判）；
- 实跑读数：`0/0`、`counts_match=true`、`rc=0`（与常量 `EXPECTED_*=0` 一致 ⇒ 当前无可修项）。
- 该脚本**不再持有极值实现副本**（见 §3.3）。

### 5.3 日志卫生
`except` 块内告警补 `exc_info=True` 并改 `%s` 惰性格式（W6 棘轮口径）。复核补了两处同文件缺口：`app/data/futu_lv3_provider.py` 原改动把 `logger.warning` **多缩进 4 格**（语法合法、仓内无 ruff/black 钩子故不会报红），以及同文件 `[FutuThreads]` 那处仍是 f-string 且无栈 —— 一并修掉。

---

## 6. 验证记录

- 本批相关测试：`115 passed`（含 `test_cancel_orphan_ext_order.py` 22 例、`test_extended_hours_entry_executor.py`、`test_config_validate_quick.py`、`test_trade_exit_invariant_repair.py`、`test_invariant_d4_settled_fields.py`、`tests/coordinator/test_model_call.py`、占位守卫与影子回声套件）。
- 复核新增测试：`tests/test_trade_extremes_ssot.py`（8 例）。
- 实跑：修补工具 dry-run `0/0 rc=0`；provider 三臂表见 §4；A/B 表见 §3.1。
- 独立复核方法：`git status/log` 定范围 → 逐行读 diff（file:line）→ provider 级实弹 + 生产 `llm_calls` 取证 → 同库同窗口 A/B（写路径全部重定向）→ 涉及文件全量跑测。

---

## 7. 未做 / 待办

- **UE 键名漂移**（`surfaced` vs `affected_tickers/impacted_tickers`）：本批未覆盖，属 prompt/schema 契约侧，建议单独立项（当前成本 = 一次调用白烧）。
- **自动化撤单**（孤儿 ETH 单方案 B 自动那半 + `resume_close`）：需用户签字，未实施；人工确认入口见 RFC-083 §6。
- **提交形态**：本批与其"改我文件"的批次分两个 commit 落库（主题分离），避免又回到"连带提交"。