---
title: chase_cap 锚归因 + 第2批几何一致性 (prompt 侧 + 标签侧)
rfc: RFC-144
date: 2026-10-17
status: 已落地 (P0/P1 已实施; P2 "放宽闸" 维持不做)
owner: Hermes (用户 2026-10-16「按建议执行」)
module: cross (DA skill 生成侧 × FTSeek-US 执行侧)
related: docs/GATE-MISKILL-LEDGER.md, docs/decisions/CHASE-CAP-20260909-post-enforcement-evidence.md, RFC-120
---

# RFC-144: chase_cap 锚归因 + 第2批几何一致性

## 1. 触发与问题

用户 2026-10-16 令「误杀写入、累计、共性分析」+「DA 生成侧不一致请再深度复查确认」；
10-17 追问「chase_cap 的 prompt 是什么逻辑、需要怎么修改、什么收益」⇒「按建议执行」。

复核把 2026-09-01 起**终态死于追价帽**的指令全量列出（`skip_reason LIKE 'chase_cap_enforced%'`，n=39，全部 executed=0），
按"是否符合方向几何"分类，结果**推翻了初判**：

| 类别 | n | 均 fwd(T+1开盘) | 中位 | 正占比 |
|---|---|---|---|---|
| 出生即违例（cap 对**生成锚**已违例：LLM 填反/自造） | 10 | +0.31% | −0.02% | 50% |
| **锚漂移致违例**（cap 对生成锚**合规**，但 entry 被 reprice 逐轮上调后落到锚的错误一侧） | **28** | **+3.31%** | +3.21% | **71%** |
| 几何合规（真追高被拦，闸工作正确） | 1 | +0.35% | — | 100% |

- **RFC-120 那一类已被治住**：10 条出生即违例全部 ≤ 2026-09-30（归一化上线日）；**10/1 起 8 条全是锚漂移（8/8）**。
- 锚漂移实例：`US.REAL` 生成锚 9.30 / cap 9.50（+2.2%，合规）→ reprice 到 10.48 ⇒ cap 落在锚下方 ⇒ **整日 0 成交**；
  `US.SNDK` 1535 → 1596.49（cap 1580）；`US.CMC`（10-16）生成锚 64.68 / cap 66.50 → 25 轮空转后回落成交。
- 执行层兜底 `normalize_chase_cap` 的锚**按设计**用生成价（`auto_execute_flow.py:840-847`：用活价会让 cap 追价 = 门恒放行），
  故这类"生成后漂移"不在它的救治范围内 —— 设计使然，不是漏接线。

⇒ 原判"DA 生成侧不一致"只覆盖 28% 的死因；主因是**锚不一致**（帽锚计划锚、entry 被 reprice 抛弃）。

## 2. 本批做了什么

### 2.1 标签侧（FTSeek，零行为变更）
`app/playbook/pipeline_parts/auto_execute_flow.py` 帽终态 skip_reason 追加 `gen=`（生成锚）：
`chase_cap_enforced(reprice=10.48,cap=9.5,gen=9.3)`。
- 目的：两类死因**按标签可分**（真追高 vs 计划锚被抛弃），无需回解 `raw_instruction_json`。
- 前缀 `chase_cap_enforced(reprice=` 逐字不变（旧行兼容 + 两处既有源码契约测试）。
- 取不到生成锚 → 省略 `gen` 段（不写 `gen=None` 免得被当数值解析）；`_gen_cc` 预置 None 防异常路径 NameError。
- 测试：`tests/test_chase_cap_enforce.py::test_skip_reason_carries_generation_anchor`。

### 2.2 prompt 侧（DA skill `llm_synth.py`，三处同步 + 一条机检）
| 处 | 位置 | 内容 |
|---|---|---|
| SYSTEM 几何条款 | `:249-255` | 新增「第2批几何一致性」：若 `entry_strategy` 写了第2批触发价 P2，则 **BUY cap ≥ P2 / SHORT cap ≤ P2**；否则该腿结构上永不执行；二选一（抬 cap 或删第2批价） |
| 字段语义 | `:1240-1246` | 同条款 + **null 语义澄清**：「无禁追语句时 cap=null 是**正确输出**，不算"写不出"」（执行层对 null 不判 → 不漏风控）；并明确 cap 不是 invalidation 位 |
| 挑战者核验项 e | `:2436-2437` | 挑战者同卡可见：核验 cap 与 P2 的几何关系，属实质硬伤（计入 PARTIAL） |
| record-only 机检 | `:1666-1706` | `chase_cap_vs_batch2_conflict`：方向感知动词（BUY 突破/站上/上破/越过；SHORT 跌破/下破/失守）+ 均线括号形态 `突破MA60(67.53)` + 个位数价格；**只报告不 repair**（同 knife_geometry / RFC-120 哲学） |

**抽取局限写进代码注释**（防后人把命中率当精确比率）：只在"第2批/第二批"之后找价位；抓不到数字不判（宁漏不误）；
命中数是**下界**。实测 7 案例探针：CMC(67.53 vs 66.50) / REAL(9.60 vs 9.5) / SHORT 下破 三例命中；
自洽(68 vs 67.53)、单腿、第2批是回踩腿、止盈价 72.0 四例**不误报**。

**prompt 指纹已断基线**：`54a787f3427b → 3ad15c002fd6`（`_prompt_fingerprint` 由 SYSTEM+USER 模板+温度派生，改字即变）
⇒ 改版前后的 DA 样本**不可混窗**（口径七律#2），旧样本保留旧哈希，勿回改。
测试：`tests/test_chase_cap_batch2_geometry.py`（10 例：4 命中/不误报/缺字段不判/**只报告不改字段**/prompt 三处同步钉）。

### 2.3 明确**不做**：`P2 放宽闸`（价格越过帽且计划锚失效时允许重锚）
我此前建议「先按预注册口径把它算干净再谈」。**算完了，结论是"算不了"**：

现役预注册面 `data/shadow/pullback_limit_replay_v2_latest.json`（窗 9/8–10/16，配对 n=167）自报：
- `evaluability.status = NOT_EVALUABLE_FIDELITY`（fidelity reason_match 0.688 < 0.8）⇒ **L0 逐笔归因 / L1b 聚合对账均"不许可"**；
- `caliber_sensitivity` 明写「两口径中位数符号不一致 ⇒ 本窗结论由观测模型决定而非入场方式 ⇒ **不可判读**」；
- `adr035_verdict`: 2/4 pass，`wire_routing=false`，blocked_by=fidelity_gate；
- 同面 `d_branch_miskill`: expired 分母 n=152 / 误杀 89 = **58.6% > 40% 门**，`forgone_actual_net_usd = $1,412.44`。

⇒ 拿这把**未获许可的尺子**去切"锚漂移子群"会制造一个看似精确的假读数（尺子滥用）。
**处置：B 不动，等尺子修好（reason_match ≥0.8）或另立 B14 式预注册面再谈。** 本批只落地"不改变成交行为"的
两件（标签归因 + prompt 教自洽），并把 28 条锚漂移登记为缺陷类（描述性人口统计，不是效应估计）。

## 3. 收益（诚实分层）

**确定性（与行情无关）**
1. 帽终态致死 ≈ **1.18 条/交易日**；其中 72% 是"计划自相矛盾"型 → prompt 修复后应趋零（机检作攒样尺子）。
2. 消除整日空转（CMC 25 轮 / REAL 整日 / TSLA 37+ 轮）对算力、推送、复盘的污染。
3. 让 `chase_cap_price` 重新成为**有信息**的字段：现状填充率 98%（PULLBACK_LIMIT 320/320）而文本依据仅 36%
   ⇒ 约 2/3 是自造值；null 语义澄清 + 机检是往"有依据"收敛的路径。

**行情相关（方向参考，非收益承诺）**：上述 39 条 T+1 开盘基准均 +2.47%（67% 正）；锚漂移子类 +3.31%（71% 正）。
三条折扣必须同屏：①从"开盘价"起算（非实际可成交价）；②"越过帽"筛出的本就是动量延续票（选样偏差）；
③样本是"被拦"人群非随机人群。**故本批不对成交金额作任何承诺。**

**代价**：prompt 换版 ⇒ prompt 指纹断基线、旧样本不可混窗；本批**不改任何门的行为**（enforce 保持 true）。

## 4. 回滚

| 项 | 回滚 |
|---|---|
| 标签侧 | 删 `gen=` 段构造（`_cc_gen_txt`）即可；已落标签是纯文本注记，旧解析按前缀不受影响 |
| prompt 侧 | 还原 `llm_synth.py` 四处文本 + 删机检块 + 删测试；指纹自动回到新值（旧样本仍在，可切分回旧窗） |
| 不做项 | 无（B 未实施） |

## 5. 生效取证（诚实边界）

- 标签侧：`app/` 改动 ⇒ 已 `systemctl restart`；但新标签**只在帽拦截发生时**才落 ⇒ 盘后/周末**无从产生**，
  属 TOO_EARLY。验收命令（下一交易日帽拦截后）：`grep -o "chase_cap_enforced([^)]*)" logs/ftseek_us.log | tail -3` 应见 `gen=`。
- prompt 侧：改动在 DA skill 脚本内，**DA 下次生成即生效**（同一进程读取脚本，无需重启）。
  机检已确认**接线在活路径**上：`llm_synth.py:2201` 由 `DA_SCHEMA_ENFORCE=1`（默认开）调用，违约既进
  `result["_contract"]["violations"]`，也打 stderr `[Contract] <sym> 违约: ...` ⇒ 验收有现成观测通道。
  指纹已由生产取证确认可观测：`data/da_checkpoint/2026-10-16/*.json` 实测记录 `prompt_hash: "54a787f3427b"`
  = 本次改前的冻结指纹（US.SKHY/US.BWXT/US.NBIS 等）⇒ 改后新生成的 checkpoint 应记录 `3ad15c002fd6`。
  验收：`grep -ho '"prompt_hash": "[0-9a-f]*"' data/da_checkpoint/<date>/*.json | sort | uniq -c`
  应出现 `3ad15c002fd6`，且 `chase_cap_vs_batch2_conflict` 违约率应趋零。
  真实数据基线（改前，`RFC-144` 探针 + `batch2_realdata_readout.py`）：since 9/1 命中 **8 条**（下界），
  其中 5 条终态死于帽 ⇒ 这条机检就是那 8 条的攒样尺子。