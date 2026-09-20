# RFC-091: Loop Engineering 落地 — 元环 findings 归宿环 (试点第 1 个 loop, L1 只读)

> **状态**: 已落地入库 (契约钉 11 例绿; 载体四路实测; 台账已入账) · **Gate 6 观察窗待开** (首个真实调度窗 = 2026-09-22 07:20 CST)
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
| A9 | 载体四路契约 | ✅ 静默 rc0 零输出 / 强制摘要 / 子脚本缺失 rc3 / 超时 rc2 (缺失与超时已实测, 见 §7 待补项) |
| A10 | 入账与对账守卫 | ✅ `test_control_loop_ledger.py` + `test_ops_scripts_sync.py` 13 例绿 |

**首轮实测读数 (2026-09-20)**: 探测器 3/4 新鲜 · 未决 12 · `LOOP_DONE=False` 且为假的是第④条 (config_census 停更 534h)。

---

## 6. 首轮发现 (环的价值本身)

| # | finding | 证据 | 处置 |
|---|---|---|---|
| 1 | **反向配置普查无载体** (台账 §2.5 D3 行曾声称"cron 40 7 * * 0 与正向普查同班") | 该 crontab 行跑的是**正向** `config_key_census.py`; 反向普查在 `crontab -l` + `jobs.json` **双查皆无载体**; 产物停在 2026-08-29 (22 天) | 台账行已**更正**; 归 `维护批` (补周频载体 或 按其自身口径退役), 不自动执行 |
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

**独立复查 (异模型)**: 本 RFC 与代码提交后交独立 agent 复查 (方法: 读码 + 实跑复算, 不采信本文叙述;
复查结论与本轮发现一并落 `DEB-20260920` 或本文追加节)。**尚待补**: 超时路 (rc2) 与"子脚本缺失路"(rc3) 的**实测**证据
(当前为代码审查 + 同族载体先例, 未逐路实弹) — 观察窗内补。

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