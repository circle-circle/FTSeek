# RFC-091: Loop Engineering 落地 — 元环 findings 归宿环 (试点第 1 个 loop, L1 只读)

> **状态**: v2 裁决闭环已落地入库 (契约钉 21 例绿; 相关守卫 62 例绿; 台账已入账) · **Gate 6 观察窗待开** (首个真实调度窗 = 2026-09-22 07:20 CST)
> **日期**: 2026-09-20
> **作者**: Hermes
> **审批人**: 项目所有者 (用户令「按建议执行」= 全权委托: 先起候选 B 纯只读审计环, 跑稳后再上候选 A; 编号顺延 — `RFC-089` 已被 ADF 周检加严占用, 故本设计落 RFC-091)
> **关联**: `docs/CONTROL-LOOP-LEDGER.md` (§2.5 元环 / §3 稳定包络 / §6 维护规则)、`docs/ADAPTATION-CONTROL-LAW.md`、
> `DEB-20260919-review-decision-support-system.md` (**RFC-088**, 影子裁决侧消费者 — 本环刻意不重叠)、
> `scripts/gate_liveness_probe.py`、`scripts/data_freshness_watchdog.py`、`scripts/daily_invariant_check.py`、`scripts/config_key_reverse_census.py`
> **变更类型**: L1 (新增观测/归宿层; **不触碰任何交易执行链**, 不读 DB, 不连富途)
> **外部依据**: 《用 Loop Engineering 搭一套会自己进化的量化交易系统》(用户提供) — 本文只取其**可验证的部分**:
> 触发器 / 状态文件 / 验证器 / 停止条件 / 权限分层; 不采纳其"无人值守下单"与"自动进化"两节 (与本项目纪律冲突, 见 §4)

---

## 1. 问题陈述 (先摆事实)

用户的问题: 交易 loop (数据→信号→验证→执行→风控) 已自动化; **调优 meta loop 仍是"用户 + agent + 对话"** —
每轮从零起意、经验不跨轮、多 agent 并行改仓互踩。

按此口径逐条实测, 缺的不是"又一个探测器", 而是**探测器产出的归宿**:

| 事实 | 实测 |
|---|---|
| 测量免疫系统已存在 | `docs/CONTROL-LOOP-LEDGER.md` §2.5 已列 D1/D2/D3/W7/权益对账/意图唯一性等 8 类探测器, 均有载体 |
| 死门检测已存在且已在跑 | 2026-09-19 门活性探针实测 `dead=4` (`short_squeeze` / `analyst_divergence` / `sec_event` / `ticker_sentiment`), 逐日产出 |
| **但"这 4 项谁裁、何时裁、裁成什么"没有任何载体** | 全仓无 loop 级状态文件 (`data/loops/` 不存在); 责任与期限只活在会话里 ⇒ **压缩即丢** |
| 探测器自己坏掉也没人知道 | 反向配置普查 `scripts/config_key_reverse_census.py` 台账声称已挂 cron, **实测无任何载体** (见 §6 首轮发现) |

⇒ 结论: 本环**不新增任何检测逻辑** (做减法), 只补 Loop Engineering 里真正缺的那一件事 ——
**finding 的生命周期 + 外部可验的停止条件**。

---

## 2. 六部件映射 (外部框架 → 本仓实物)

| 部件 | 本仓实物 | 状态 |
|---|---|---|
| ① 触发器 | Hermes `no_agent` job `fcc7301f2e47` (`20 7 * * 2-6`, 紧随门活性探针 07:15) | 本批新增 |
| ② 技能手册 | `~/.hermes/skills/devtools/ftseek-loop-engineering` (六部件映射 + 三层权限 + 新增环路的入账清单) | 本批新增 |
| ③ 状态文件 | `data/loops/meta_audit/STATE.md` (人读) + `findings.json` (机读) + `findings_history.jsonl` (流水) | 本批新增 |
| ④ 验证器 | `tests/test_meta_audit_loop.py` 11 例契约钉 + 独立 agent 复查 (异模型, 见 §7) | 本批新增 |
| ⑤ Worktree | 沿用既有纪律 (并行 agent 共存: `git worktree` + `hash-object` 注入自己 hunk) — 本批只提交自己的 5 个文件 | 沿用 |
| ⑥ 连接器 | 终端/文件/推送 (飞书)。**实盘下单天然是死的** (`ALLOW_REAL_TRADING=false` + `allow_live=false`), 本环不复用也不试探 | 沿用 |

---

## 3. 环的行为 (逐条可验)

```
scripts/meta_audit_loop.py        L1 只读环本体 (只写 data/loops/meta_audit/)
ops/hermes/meta_audit_carrier.py   Hermes no_agent 载体 (线上副本同 sha256)
Job fcc7301f2e47                  20 7 * * 2-6  (周二~周六 CST)
```

1. **消费**既有产物 (只读 JSON, 零新检测): `gate_liveness_latest.json` / `watchdog_liveness_latest.json` /
   `invariant_check_latest.json` / `config_key_reverse_census_latest.json`。
2. **对账** 进状态文件:
   - 新增 → `OPEN`, 带 `owner` + `decide_by` (首次出现 + 14 自然日; 不用交易日历 — 该 accessor 会开 OpenD 连接);
   - 源侧回读确认消失 → `CLOSED`, 必须带 `close_evidence` ("当日产物不再报该项" + age);
   - **源产物不可用/停更 → `UNVERIFIABLE`, 绝不自动关闭**;
   - 关闭项复发 → `REOPEN` (复发必须可见)。
3. **owners**: 死门类 → `用户(裁决)` (退役/激活属行为变更); 维护类 (生产者未喂数 / 不变量 / 探测器停更) → `维护批`;
   `W7 chg 分母分叉` 单独路由到 `用户(裁决)` (G8 生产修复被列为 P2, 须另开预注册, 不得在测量批里顺手改)。
4. **停止条件 (外部可验, 禁"agent 说完成")** — 由 STATE.md 承载, 任何 agent 可只读复算:
   `LOOP_DONE ⇔ ①OPEN 全有 owner+decide_by ∧ ②无逾期未裁 ∧ ③CLOSED 全带回读证据 ∧ ④探测器产物全新鲜`。
5. **静默契约**: 状态签名不含时间戳, 无变化 = 零输出 = 不推送 (避免把"尺子"变成新的刷屏源);
   每轮必落 `logs/meta_audit.log` (含 `[carrier …] rc/dur/out_lines` 头) ⇒ **"没推送"不等于"没跑", 心跳可外部回读**。

---

## 4. 权限分层 (与交易纪律对齐, 这是本设计的主约束)

| 层 | 范围 | 依据 |
|---|---|---|
| **L1 只读** | 拉数/算指标/出判决书/出告警 —— 全自动 | 零风险; 本环**全部**动作都在此层 |
| **L2 SIM 内** | 可自动重跑/落库/起影子 (SIM 期授权) | 不动真钱 |
| **L3 实盘 / 改策略** | 只能产**提案 + 预注册判据**, 人裁 | 双锁已在 (`ALLOW_REAL_TRADING=false` + `strategy_budgets.allow_live=false`) |

**明确不采纳外部文章的两节**: ①"loop 无人值守直接下单" (与本项目预注册+人裁纪律直接冲突);
②"loop 自己改自己" —— 本环**不得修改自身之外的代码**, 只能写自己的 STATE/findings 与提案文本。

---

## 5. 首个 loop 的验收判据 (本批 Gate 5 实测)

| # | 判据 | 实测 |
|---|---|---|
| A1 | 环跑通且首轮产出非空 | ✅ 12 项未决 (4 死门 + 7 维护项 + 1 探测器停更) |
| A2 | 无状态变化时静默 | ✅ 复跑/三跑 stdout 长度 0 |
| A3 | 状态变化时投递摘要 | ✅ 注入新载体异常 → 摘要含该 key |
| A4 | 不自动关闭 | ✅ 源停更 + 该项消失 ⇒ UNVERIFIABLE (契约钉) |
| A5 | 关闭必须有回读证据 | ✅ CLOSED 项 `close_evidence` 非空 |
| A6 | 探测器缺失/产物坏必产 finding | ✅ `detector:*` 四种源全命中 |
| A7 | 逾期可见且不自动动作 | ✅ `⏰ 逾期未裁` + 状态仍 OPEN + LOOP_DONE=False |
| A8 | L1 只读 | ✅ 契约钉: 全仓文件 mtime 快照比对, 越界写入 = 0; 源码禁 `app*`/`futu`/`sqlite3` |
| A9 | 载体四路契约 | ✅ 四路**逐路实弹**: 静默路 rc0 零输出 (out_len=0) / `META_AUDIT_FORCE=1` 摘要路 / 子脚本缺失 rc3 (`FTSEEK_REPO=/tmp/empty…` 实跑) / 超时 rc2 (伪仓慢子脚本 `sleep 30` + `CARRIER_TIMEOUT_S=2` 实跑, 各到期望 rc) |
| A10 | 入账与对账守卫 | ✅ `test_control_loop_ledger.py` + `test_ops_scripts_sync.py` 13 例绿 |

**首轮实测读数 (2026-09-20)**: 探测器 3/4 新鲜 · 未决 12 · `LOOP_DONE=False` 且为假的是第④条 (config_census 停更 534h)。

---

## 6. 首轮发现 (环的价值本身)

| # | finding | 证据 | 处置 |
|---|---|---|---|
| 1 | **反向配置普查无载体** (台账 §2.5 D3 行曾声称"cron 40 7 * * 0 与正向普查同班") | 该 crontab 行跑的是**正向** `config_key_census.py`; 反向普查在 `crontab -l` + `jobs.json` **双查皆无载体**; 产物停在 2026-08-29 (22 天) | 台账行已**更正**; 用户裁「如果无用就退役」⇒ 实测判定**不退役判定本身** (它有活消费者: 回归测试在真仓上跑门禁), **退役"把它当周期传感器"的假设**; 不补周期载体 (见 §12) |
| 2 | 4 道死门待裁 (`short_squeeze` / `analyst_divergence` / `sec_event` / `ticker_sentiment`) | 门活性探针 C2 命中率判据: 17 交易日零命中 | 归 `用户(裁决)`, `decide_by=2026-10-04` (首次出现 + 14 自然日) |
| 3 | watchdog D1 `open_positions:never_fed` | 11 个写入方零行落库 | 归 `维护批` |
| 4 | invariant I1/I4/W1/W6/W7 五项 | 见 `data/invariant_check_latest.json` | W7 → `用户(裁决)`; 其余 → `维护批` |

---

## 7. 开发过程中实测到的四个真缺陷 (都靠测试/实跑抓出, 非审查推测)

| # | 缺陷 | 为什么危险 | 修法 |
|---|---|---|---|
| 1 | 源新鲜度阈值按"日频直觉"取 26h | **周日在正常调度间隔下误报 3 源停更** (30h/31h 是周末的正常间隔) — 误报会把真信号淹掉 | 阈值改为"该载体调度面自带的最长自然间隔 + 1 tick": `* * 2-6` ⇒ 96h; 周频 ⇒ 192h |
| 2 | 静默契约的 hash 含时间戳 | 每轮 hash 都变 ⇒ 每轮都推, **静默契约形同虚设** | 签名只取状态变化维度 (open/closed/overdue/stale/done) |
| 3 | 停止条件漏"逾期"一条 | 1 项逾期下仍打印 `LOOP_DONE=True` — **"没人裁"被读成"收敛完成"** | 加为独立判据② |
| 4 | 判据标签与取值**位置对齐**错位 | digest 显示"②无逾期=True"实际取的是 c2=关闭证据 = **说反**;"看起来有断言" 的静默缺陷 | 判据一律以 `(标签, 布尔)` 对呈现, 禁止 `c1/c2/...` 位置对齐 |

**独立复查 (异模型)**: 本 RFC 与代码提交后交独立 agent 复查 (方法: 读码 + 实跑复算, **不采信本文叙述**;
复查结论与本轮发现一并落 `DEB-20260920` 或本文追加节)。
**本文内已撤回一处未实弹就写下的声称**: §5 A9 首版写"缺失与超时已实测"而当时只跑过静默/摘要两路 ——
已补做 rc3 (子脚本缺失, 伪仓实跑) 与 rc2 (超时, `sleep 30` 慢子脚本 + 2s 上限) 两路实弹后, 该声称才成立。
(同类先例: 文档写"已设 cron/提醒"而实物不存在 ⇒ 凡"已实测"字样必须能指到当次命令与返回值。)

---

## 8. 明确不做 (范围边界)

- ❌ 不做候选 A (复盘→候选→预注册优化环) — 待本环跑稳 (≥2 周无静默死亡、无假阳) 再接 RFC-088 的 `bundle_*.json` / `pending_adjudications`。
- ❌ 不碰影子裁决 (`criteria_status` / `pending_adjudications` 那半边消费者是 RFC-088), 本环只承载**探测器级 finding**。
- ❌ 不自动改参/改门/删产出/重启/下单; 不引入 `app.*` / `futu` / `sqlite3` 依赖。
- ❌ 不做多 loop 并行; 不做"自动进化到实盘"。

## 9. 回滚

删 Hermes job `fcc7301f2e47` + 删 `ops/hermes/meta_audit_carrier.py` 与线上副本 +
删 `scripts/meta_audit_loop.py`、`tests/test_meta_audit_loop.py`、`data/loops/meta_audit/` + 删台账 §2.5 本行。
零交易链耦合 (不读 DB / 不连富途 / 不改配置), 回滚不动任何在役环路。

## 10. 观察窗与复盘条件

- **Gate 6**: 首个真实调度窗 2026-09-22 07:20 CST; 观察 ≥2 周 (至 2026-10-04, 与 4 道死门的 `decide_by` 同日)。
- 复盘三问: ①有没有静默死亡 (心跳缺口) ②有没有假阳 (误报 forcing 用户注意力) ③`decide_by` 到期时用户是否**真的被提醒到并能一次裁决**。
- 满足后: 开候选 A (优化环) 的 RFC; 不满足则先修本环 (环路自身也要过"外部可验"这关)。
---

## 11. 独立复查回执 (异模型, Gate 5 证据)

**复查者**: 独立 agent (模型族 = glm-5.2, 与本环作者 Hermes 不同族 — 满足"生成用 A / 验证用 B");
**方法**: 读码 + **自建临时仓实跑证伪** (不采信本文叙述) + `git show` 回读; 全程只读, 未改任何文件。
**结论**: `verdict = 确认 — 实现与其声称一致, 逐条实测通过, 无 P0/P1 缺陷`; `defects = []`。

逐条 (每条附命令/返回):

| 复查项 | 结果 |
|---|---|
| 契约钉 + 台账守卫 + 对账守卫 | ✅ 实跑 **24/24 绿** (11 meta_audit + 10 ledger + 3 ops_sync) |
| 规则(a) 停更源 + 该项消失 → 非 CLOSED | ✅ 临时仓把产物 mtime 改 200h 并移除 dead gate → `UNVERIFIABLE` |
| 规则(b) CLOSED 必须带回读证据 | ✅ `close_evidence="源侧回读: …(age 0.1h, 阈值内)"` |
| 规则(c) 探测产物缺失/损坏必产 finding | ✅ 4 产物全缺 → 4 个 `detector:*`; 坏 JSON → "不可解析" finding |
| 规则(d) 静默契约 hash 不含时间戳 | ✅ 连跑 3 轮 hash 恒定, 第 2/3 轮 stdout 长度 0 |
| 逾期 → `LOOP_DONE=False` | ✅ 手改 `decide_by=2026-09-01` → 判据②=False |
| REOPEN 复发可见 | ✅ `closed_at=None` + history 记 REOPEN |
| L1 边界 (文件 + import) | ✅ 快照比对零越界; 仅 stdlib, 零 `app/futu/sqlite3` |
| 载体 rc 三态 | ✅ rc0 静默 / rc2 超时 / rc3 缺失 逐路实弹; 两副本 sha256 一致 |
| 台账 D3 更正 | ✅ 与实物相符 (周日行跑正向 census, 反向普查双查无载体, 产物停更 22 天) |
| §5/§6 数字 | ✅ 12 未决 (4+7+1) / 4 死门 / invariant 5 项 与 `findings.json`·`gate_liveness_latest.json` 精确一致 |
| §7 四缺陷 | ✅ 均已在码中修复 (96h/192h 阈值、hash 去时间戳、逾期独立判据、`(标签, 布尔)` 对) |

**复查者未能验证的部分 (诚实登记)**: ① Gate 6 真实调度窗 (2026-09-22 07:20 未到);
② 观察窗内长期行为 (≥2 周静默死亡/假阳); ③ `META_AUDIT_FORCE=1` 路径的载体级实弹 (它刻意不在生产仓上跑以免污染 state)
—— ①②属时间未到, ③**作者侧已实弹** (2026-09-20 12:29 生产仓 `META_AUDIT_FORCE=1` 跑载体 → rc=0 + 4 行摘要落 `logs/meta_audit.log`),
此条为复查者视角的留白, 非未测项。

**载体在真调度器下的实弹 (作者侧, 复查者未覆盖)**: 手工触发 job `fcc7301f2e47` →
`executions.db` 行 `status=completed` / `delivery_outcome=suppressed` / `source=direct` / `scheduled_instant=NULL`(手工触发标记);
`logs/meta_audit.log` 落 `[carrier …] rc=0 dur=0.1s out_lines=0`; `last_digest.json` 签名未变 ⇒ 未推送。

---

## 12. 后续处置: 反向配置普查 (D3) — 「如果无用就退役」实测裁定 (2026-09-20)

**用户口径**: 「如果无用就退役」。**处置 = 判据先实测, 再裁**:

| 问题 | 实测答案 |
|---|---|
| 判定本身有用吗? | **有**。它有**活消费者** = `tests/test_config_key_reverse_census.py::test_real_repo_has_no_new_invisible_switch`(每次全量回归在**真仓**上跑门禁, 新增隐形开关即红) + `test_declared_switches_match_code_defaults`; 冻结基线仅剩 1 条二级面 (规则本身已停用) |
| 缺的是什么? | 缺的是**周期挂载**, 不是判定; 台账原行声称的 cron 从未存在 (文档声称≠实物) |
| 该补 cron 吗? | **不补** (做减法): 一个"已有活消费者 + 慢漂移"的配置面守卫, 不需要再养一条周期环。若日后要周更读数, 一行开关即得: 周日正向普查行末 `&& … config_key_reverse_census.py --gate` |

**由此产生的代码变更 (本环首轮就暴露了自己的一处设计错)**:
把"按需工具"当"周期传感器"是错的 —— 会给用户**每天**报一条"探测器停更"的假阳, 而那是既定设计。
故引入**按需源语义**: 第 5 列写消费者路径 ⇒ ①产物陈旧**不判失明**, 陈旧读数也不作为 live finding (避免拿 22 天前的读点当今天的问题);
②只有**消费者消失**才算失明; ③该源的 `detector:*` finding 在消费者复活时关闭 (首版只认"产物恢复新鲜" ⇒ 按需源的 detector finding **永不收敛**)。

**本轮新增两个实测缺陷 (都靠实跑抓出)**:
1. `SOURCES` 升 5 元组后 `main()` 一处仍按 4 元组解包 ⇒ 环崩溃 —— **但崩溃被环自身的异常路径如实报成 rc=2 + 具体错误**, 未被读成"零 finding"(该路径因此首获实战验证)。
2. `detector:*` finding 的关闭条件过窄 (只认周期源) ⇒ 按需源的该 finding 永久 OPEN = **一个永不收敛的 finding**, 正是本环要防的病。

契约钉从 11 例增至 **14 例**。首轮读数随之更新: 未决 **10** (4 死门 + 1 watchdog + 5 invariant), `LOOP_DONE=True` (config_census 两项按按需源语义关闭并留证据)。

---

## 13. v2 裁决闭环 (2026-09-20 用户授权试跑)

### 13.1 语义修正

用户裁决: `LOOP_DONE` 的目标语义是 **还有没有未决问题**。因此 v1 的
“OPEN 全有 owner+decide_by 即 TRUE” 被判定为语义错误。v2 停止条件:

```text
无 OPEN / UNVERIFIABLE / DECIDED / ACTION_PENDING / VERIFY_PENDING / REVIEW_DUE / BACKLOG
∧ 探测器健康
∧ CLOSED 全带证据
```

`CONTROLLED` 未到期不算未决; 到期后没有验证结果则转 `REVIEW_DUE`; 验证失败 reopen 原项并升级。

### 13.2 无幻觉零问题

`{}` 不再等价于零问题。源产物必须携带 expected 字段和内部生成时间;
`intraday_health_latest.json` 必须显式携带 `schema_version` / `generated_at` /
`no_findings` / `findings` / `counts`。缺字段或字段类型错一律产生
`detector:*` finding, 旧 finding 转 `UNVERIFIABLE`。

### 13.3 盘中异常收敛

新增 `scripts/intraday_health_snapshot.py`: 扫描近 24 小时活跃与当日轮转日志,
只识别字面量 `] ERROR:` / `] CRITICAL:`; 每条聚合为 normalized signature,
输出 `data/intraday_health_latest.json`。任一 ERROR/CRITICAL 即 P0,
`pnl_path=capital_preservation`。现有 `error_alert` 负责实时推送, 且阈值已改为
**1 条 ERROR/CRITICAL 即触发** (同签名小时级冷却降噪); 本环负责次日收敛、归档和裁决,
不替代实时告警。

### 13.4 裁决与关闭

新增 append-only `data/loops/meta_audit/decisions.jsonl` 与
`scripts/meta_audit_decide.py`。支持:

```text
FIX_NOW / ACTION_DONE / VERIFY_PASS / VERIFY_FAIL
ACCEPT_RISK / DEFER / RETIRE / OUT_OF_SCOPE / INVALID_SOURCE
```

测试通过只能进入 `VERIFY_PENDING`; `CLOSED` 必须源侧回读不再报该项,
或显式 `RETIRE / OUT_OF_SCOPE / INVALID_SOURCE`。bundle 默认 `closure_mode=ALL`,
主问题关闭前所有 linked findings 必须已关闭或有显式终态。

### 13.5 注意力与维护批

- WIP 上限 3; 超限时新 P2/P3 进入 `BACKLOG`, 不丢弃, 且 `LOOP_DONE=False`。
- SLA: P0 当日 / P1 24h+3d / P2 7d+14d / P3 30d。
- 摘要显示 P0-P3、`pnl_path`、WIP、BACKLOG 和最紧急项。
- 盘中 ERROR/CRITICAL、风险、冻结破坏、执行盲区立即升级。

### 13.6 实测

- `pytest -q tests/test_meta_audit_loop.py tests/test_error_alert.py tests/test_control_loop_ledger.py tests/test_ops_scripts_sync.py`
  → **62 passed**。
- 生产快照: `intraday_health_latest.json` `no_findings=true`,
  近 24h ERROR/CRITICAL = 0, 扫描 267 个日志文件。
- v2 首轮状态: `LOOP_DONE=FALSE`, 未决 10 = P0 2 / P1 7 / P2 1,
  WIP 0/3, BACKLOG 0。

---

## 14. v2.1 收口: 盘中日志签名归并 + 判决书载体补齐 (2026-09-22, 用户裁「按建议执行」)

### 14.1 实测病灶 (两条, 都有实物)

1. **同根因双记**: 2026-09-22 生产 `findings.json` 里, `intraday_health:ERROR:177a3510b29f`
   (P0, due 当日) 的 evidence 原文就是 invariant 那条日志行 `[不变量自检] 51 处违反:
   W7_chg_denominator_divergence×38 …`, 而同一事项已作为
   `invariant:W7_chg_denominator_divergence` (P1, due 2026-10-04) 在册 ⇒ 一因两条, 严重度还不同。
2. **签名漂移 ⇒ 天天新生 P0**: 该源 key = 日志文本哈希 (§13.3 设计), 而违反计数/耗时每天都变
   ⇒ 每天都是"新签名" ⇒ 每天新生一条 P0 且当日到期 (§13.5 SLA)。观察窗复盘三问之②
   ("有没有假阳 forcing 用户注意力") 在首日就给出答案: 有。

### 14.2 修法 (做减法, 不新增门)

| # | 改动 | 位置 |
|---|---|---|
| ① | **稳定族键**: evidence 数字归一化后取 sha1(10) ⇒ `intraday_health:rc<hex>`; 复现落在同一条 finding, 不新生 P0 | `_intraday_family` |
| ② | **根因归并**: evidence 点名了已在册结构化根因 (`W1_/W4_/W6_/W7_/I4_/D1:`) ⇒ 不新建条, 把签名追加到该条 (`merged_signatures`, 去重写入), 不升 P0 | `_structured_roots` + `collect()` |
| ③ | **归并项永不入库**: `@append@` 项只走挂载路径 (首版曾把它当新条创建, 紧接着被读成"源不再报该项" ⇒ **自我制造的假关闭**, 见 §14.3) | `reconcile()` |
| ④ | **旧键迁移不假关闭**: 旧 `intraday_health:ERROR:*` 键 ⇒ 命中结构化根因的**显式**关成 `MERGED_DUPLICATE` 并写明"这是归并, 不是修复", 无根因可挂的原地改名为族键 (生命周期照常) | `_migrate_intraday_keys` |

保留: 全新根因仍是 **P0 + 当日到期** (设计意图不变 —— 真新事必须抬到人眼)。

### 14.3 开发中自造的两个缺陷 (都靠实跑抓出, 披露留痕)

1. **归并项被创建** → 同轮又被判"源不再报该项" ⇒ 假关闭记录 (已回滚该轮产物 + 清 2 条 history 伪事件, 修法见 ③)。
2. **键改名未同步 `finding["key"]`** ⇒ 摘要/STATE 仍印旧键名 (`intraday_health:ERROR:691c…`),
   而 store 里已是 `…:rc64370b7228` —— 不查就看不见 (已修 + 回读确认)。

### 14.4 收口后的生产读数 (2026-09-22 实跑, 幂等复跑三次同结果)

- 未决 **12 → 11** (P0 2 / P1 8 / P2 1); 双记消失: `intraday_health` 那条 P0 关成
  `MERGED_DUPLICATE`, 签名挂到 `invariant:W7_chg_denominator_divergence` (`merged_from_keys` 已登记);
  盘前跨界那条改名为 `intraday_health:rc64370b7228` (P0, 当日到期 —— 首次出现的真新事, 不变)。
- 契约钉 14 → **18 例**, 连同台账/载体同步共 **38 passed**。

### 14.7 推送面自解释 (2026-09-22 用户令「需要推送 添加容易理解的注释」)

无人值守推送的三条硬约束 (凡 loop 产出推给用户者一体适用):

1. **术语就地翻译**: 收货人不在终端, 不能 grep/PRAGMA; 推送里每个行话 (「净」/「计划外」/「P0 异常」/「待裁决」)
   必须在同一屏内给出白话定义, 不与 JSON/MD 分家。
2. **数字就地给口径**: 报「净」必须同屏给出「毛/费」, 抬头标窗口与单位; 但**缺失即省, 绝不为文案编数**
   (取不到毛/费时只留口径文字, 由测试钉住)。
3. **点破误导点**: 「待裁决 N」必须写明 "样本攒够 ≠ 判据通过" 并给最久等待日期 —
   否则读者会把它读成"判据已过"或"系统卡住"。

实现面: 日环载体 (`e86cd69ed2be`) 转交 `review_bundle.py --print-push` 的白话判决书 (**日推**, 用户令);
回滚 `REVIEW_BUNDLE_QUIET=1` 回严格静默。**代价已知**: 与 04:07 盘后综合复盘头条同源, 存在重复 —— 用户明示要推。
子脚本出口纪律: `--print-push` 时 stdout **只有**推送文本 (JSON 摘要禁混入, 否则污染投递)。

### 14.5 RFC-088 P5 载体补齐 (判决书证据面)

- 核查实测: DEB-20260919 §16 声称的 `ops/hermes/review_bundle_carrier.py` + 线上副本 + Hermes job
  **三者皆无** ⇒ P5 从未落地, 产物停在 `bundle_2026-09-18`。本次按声称形态补齐:
  job `e86cd69ed2be`, `35 6 * * 2-6`, 子脚本 `scripts/review_bundle.py --date <ET当日> --vault`。
- 四路实弹各一次: rc0 **零字节** ✓ / 子脚本缺失 rc3 ✓ / 超时 rc2 ✓ / rc0 但零产物 rc4 ✓。
- 一处**显式偏离 §7** (待裁): 默认不 `--push` (理由与开关见载体 docstring 与台账行)。
- **周环已挂** (2026-09-22 同日): job `1264a1421f20` `45 6 * * 6`, 载体 `ops/hermes/review_coverage_carrier.py`。
  该扫描 stdout 是整块 JSON ⇒ 取"签名变化才出声" (与 `4378c93a89e6` 同形), 另加 rc4 = "rc0 但零产物/摘要不可解析"。
  实测: 首跑出声 3 行 / 二跑 0 字节 / rc3 / rc4 / 真调度实弹 (rc=0 dur=12.4s, delivery suppressed)。
- **W7 分叉溯源已定论 + 抓包载体落地** (2026-09-22): ctx 侧 `change_percent` = 前一交易日涨跌幅 (3 样本 ≤0.003pp, 与交易所日K对表);
  取数面三选一未定 ⇒ 限时只读 job `8a989cf4501d` (`33 21 * * 1-5`, 盘前窗, 只抓 2 只) 判定, 裁决落地即删。证据档 `data/audit/w7_denominator_probe.jsonl`。
- **W7 分母相位修复已落地** (2026-09-22, 用户裁「先修分母」; 详见 `DEB-20260922` §4.4):
  病灶行级 = `LV3TechnicalCalculator._calc_change_percent` 从日K序列相邻两根 bar 算涨跌幅 ⇒ 盘前/盘中恒等于**前一交易日涨跌幅**,
  而 `market_data_router.py` "用快照价覆盖 K 线价"只换 `price`、**没重算 `change_percent`** ⇒ ctx 输出"实时价 + 昨日涨跌幅"。
  修法 = Step 8 后按 (实时价, RFC-068 真昨收) 重算 + `change_percent_source` 溯源键, fail-open 缺任一则保留原值。
  3 条守卫 (`tests/test_router_prev_close_injection.py`), 相关面回归 281 passed。
  **教训 (已入 skill)**: 同一次口径修复只修了半个字典 (RFC-068 8/29 只修 `prev_close`), 派生量漏在门外 ⇒ 覆盖基准量时必须扫同族派生字段。
- **月环不是载体缺口, 是本体未实现**: `review_bundle.py --period month` 直接 `error: … arrive with P5` ⇒ 只能在 "单开一批实现月度双窗口+判据总账" 与 "把 §7/§16 该行标注未实现" 之间选。台账已加显式注释行防误读。
