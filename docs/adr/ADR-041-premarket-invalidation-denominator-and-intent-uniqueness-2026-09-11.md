# ADR-041: 盘前失效门收口批 (DEB-20260911 P0+P1) — 昨收分母三源审计落棘轮哨兵 + 影子意图唯一性守卫 + A/C 采集退役

- **状态**: 已实施 (2026-09-11, 用户两轮裁决:「按建议执行」(=P0+P1, P2 明确不在本轮)
  →「按照建议执行 提交 重启」)
- **主记录 (辩论/取证/勘误/预注册判据)**:
  `docs/debates/DEB-20260911-premarket-invalidation-axis-and-rule-conflicts.md`
  (三模型两轮辩论: 勘误 E1–E8、共识 C1–C9、残余分歧 D1–D3)
  + `docs/CONTROL-LOOP-LEDGER.md` (§2.5 两行新环路、§5 G3 更新、§5 新增 G8)。
  本 ADR 只固化**决策与其理由**, 证据与实测数字不在此重复。
- **立此 ADR 的直接缘由 (诚实登记)**: 与 ADR-040 同一形态**第二次发生** —— 本批再次触及
  `scripts/daily_invariant_check.py`, 其既有形参 `def main(push_enabled: bool = True)` 出现在新增 W7
  布线的 hunk 上下文行, 命中 `commit-msg` 钩子高风险词表 `enabled.*true` ⇒ 钩子要求引用
  `[ADR-XXX]`/`[RFC-XXX]`/`[hotfix]`。**该命中是误报** (本批未启用任何策略, 见决策④"零交易行为变更"),
  但钩子按文本形态判定、**意图不可 grep** ⇒ 不辩解、不绕过、不硬凑无关编号, 按仓库既有体例
  (ADR-038 / ADR-039 / ADR-040) 补一份真决策记录。词表本身该怎么修见「未决/移交」第 1 条。

## 背景 (病灶族)

三件事同族: **口径/判据分散在多处且无机械守卫 ⇒ 每一处表面都在跑, 彼此却不知情**。

- **昨收分母三源并存 (共识 C6/N1)**: prev_close 实际有三个来源 ——
  ① `get_market_snapshot.prev_close_price` (干净) ② `get_quote` 推送缓存 → `ctx["change_percent"]`
  (**污染**: 跨日不更新、全日冻结) ③ RFC-068 日 K 自算注入 `ctx["prev_close"]` (干净, 线上
  `B_long_knife` 硬门读的就是这一侧)。实锤 2026-09-08 US.BIDU: 同一条指令 7 秒内 pmi 快照
  −7.65% vs ctx +4.07% (差 11.72pp), 反推分母 99.47 vs ≈88.26。此前**无任何守卫**核对两影子
  同标的同窗 chg 是否一致 ⇒ 凡读 `ctx["change_percent"]` 的消费者, 其读数可信度无人知。
- **同意图双影子 (共识 C9/N3)**: `A_short_chase` (盘前快照口径) 与 `short_chase_ban_shadow`
  (触发时刻口径) 同阈值 +3%、同意图"逆势追空禁入", 各自独立攒样独立裁决、台账零互引,
  且**已在真实数据上给出矛盾结论** (BIDU 9/8) ⇒ 存在"一个 FAIL 一个 PASS → 双转正打架"风险。
  既有守卫只强制**存在性登记**, 无机制强制**意图唯一性**。
- **ban-shadow 是"建了没登"第 4 例**: 线上 18 天仅 5 行 (NTES 8/24、MSTR 9/1、DDOG 9/3、
  BIDU 9/8 ×2), 注册表无条目 ⇒ 无 min_n、无死线、无结算路径, 攒到死也没人裁决。
- **辩论文档正文数字多处错误**: 由多模型两轮交叉核实出 E1–E8 (含数据窗行数、样本口径),
  行动前必读勘误表 —— 这也是"先辩论再动手"在本批的直接收益。

## 决策

### ① 昨收口径**只立哨兵, 不做单源化** (P0-1 落地; P2-1 明确不做)

- 落 W7 (`_check_chg_denominator`) 于 `scripts/daily_invariant_check.py`: 两影子
  (`premarket_invalidation_shadow.jsonl` × `entry_timing_shadow.jsonl`) 按 `inst_id` 对齐
  (退路 `(symbol,date)`), ±10min 可比窗内取最小 |Δchg|, >2pp 即报「该日所有
  `ctx["change_percent"]` 消费者读数不可信」+ 已知存量登记数。
- 棘轮基线 `data/chg_denominator_baseline.json`: `divergent` = 已知分叉存量, **只降不升,
  守卫绝不自动改写** (元环规则 5); 改 `divergent` = 改判据, 须先落档 DEB 并在 `refs` 写裁决出处。
- **为何不单源化**: 改 `ctx["change_percent"]` 的生产者会同时波及全部读侧 (跨
  `app/data/market_data_router.py` 与所有消费者), 爆炸半径远超本轮授权, 且"污染期"的前置条件
  (跨日冻结在什么条件下发生、影响哪些日) 尚未量化 ⇒ 归 P2, 需另开预注册 DEB。
  本轮 interim defense = W7 哨兵 + 污染期标注写进 ban-shadow `notes`。
- 干跑实测: paired=195 / unpaired=115 / 分歧 90 (**46%**), 窗口口径不敏感, bad_lines=0。

### ② 意图唯一性守卫**长在注册表里, 由周检承运** (P0-3①)

- `ShadowItem` 新增 `intent` / `cross_ref` 两字段 + `check_intent_uniqueness()` 五判据:
  ①同意图现役项未互指 ②悬空互引 ③单向互引 (目标现役却未回指; 指向终态留痕项豁免回指,
  但**终态项指向现役项时现役项仍须回指** —— 留痕链必须双向可见) ④注册表 id 重复
  ⑤守卫自身异常必出 `intent_uniqueness_detector_error` (R5: "零违反"≠"守卫崩了")。
- 承运面 = **周检** (`ops/hermes/ftseek_weekly_checkpoint.py` 新增 `intents` 键 + 渲染循环),
  不另起 cron。理由: 注册表变动是人工动作、无日级时效需求; 周检已是 `summary()`/`check_mounting()`
  的既有承运方, 加一路输出即闭合"函数建了没人读"的断头传感。接入本身由
  `tests/test_shadow_intent_uniqueness.py::test_weekly_checkpoint_wired` 钉住。
- 判据④ (id 重复) 是**本批自身事故催生的**: 同日多会话并发编辑产出两条 `short_chase_ban_shadow`,
  其中一条 `config_key` 写成死键 (`unified_config.yaml` 嵌套层级错 ⇒ top-level 不生效)。
  删重复只修当下, 加判据才防复发。

### ③ 结算器裁决顺序: 退役闸 → 预注册四关 → 转正前冲突核对 (P0-3②)

- 顺序不可换: 退役闸先判, 避免已裁决退役的事项继续攒样后又被"四关通过"翻案 (= 挪门柱)。
- 冲突核对结果写进产物 `conflict_check{ok,detail,refs}` 留痕, 不只落 cron stderr。
  理由: stderr 无追溯, 产物是裁决日唯一可对账的载体。
- 产物实测: A exec=1 net=+6.20 → `RETIRED_NO_PROMOTION` (cc=None, 退役项不再核对冲突);
  B exec=3 net=−280.48 miskill=0.333 → `KEEP_SAMPLING` (cc ok, refs=['RFC-068']);
  C exec=22 net=−152.49 miskill=0.682 → `RETIRED_NO_PROMOTION`。

### ④ A/C 退役**只退采集侧, 零交易行为变更** (P1)

- `C_long_chase` 采集退役; `A_short_chase` 快照口径退役, 其假设 (逆势追空禁入) **转移给
  ban-shadow** (触发时刻口径)。理由: 两者同意图同阈值, 而快照口径与执行时刻脱节
  (09:35 快照 ≠ 触发时刻), 保留双口径只会复制②要治的病灶。
- `scripts/data_freshness_watchdog.py` 同批**只改描述口径** (行仍按当日全部可执行指令落盘
  ⇒ 阈值 50h 不变), **不移除条目** —— 移除会让"B 也停产"变成静默。
- **边界 (诚实登记)**: 本批是 record-only 采集侧, 未碰任何线上硬门或出场参数;
  B 规则的**执行时刻硬门独立在线上** (RFC-068), 不受本批影响。

### ⑤ ban-shadow 登记口径: min_n=20 + 死线 2026-10-31 + 按需评估

- `eval_on_demand=True` (无独立结算器 ⇒ 挂载核对哨兵豁免); 预注册判据 = 反事实方向一致率 70%。
  死线前若样本仍不足, **预期裁决是 KILLED 而非继续攒** —— 18 天 5 行的产样率已接近坏死。
- **刻意不接入 `data_freshness_watchdog`**: 该 watchdog 是小时级阈值, ban-shadow 产样率天然
  低于任何小时级门槛 ⇒ 接入等于永久告警 (告警疲劳会掩埋真告警)。陈旧风险已由注册表
  `check_mounting()` 的 14 天哨兵 (10 天宽限) 覆盖, 有既有承运方, 不另起炉灶。
  这是对本轮建议的一处**有意偏离**, 已在辩论文档执行项与台账 §5 G8 登记。
- 写入侧已知缺陷 (缺 `date` 字段、`direction` 硬编码 `'SHORT'`) 登记在 `notes` 而**非本轮修**:
  改它属写侧契约变更, 须与②的判据同批预注册后动。

## 明确不做 / 纪律边界

- **P2 全部不做** (需另开预注册 DEB): 线上硬门、`extreme_gap` 收紧、C_v2、regime 选项 D、
  prev_close 单源化。
- 不放宽任何既有基线; W7 棘轮与注册表 baseline 只能由脚本重生成, 不手工加宽。
- 不改写辩论正文的历史错误数字, 只**追加**勘误表 E1–E8 (保留"当时怎么错的"可追溯性)。
- 辩论文档「已关闭」**故意不勾**: P2 未预注册、周检线上副本待部署、服务待重启 (提交时点)。

## 归属与提交范围 (多会话共享工作区, 必须写清)

- 本批 = 13 文件 (12 项改动 + 本 ADR)。工作区另有**他批未提交**改动 (`main.py` /
  `playbook_service.py` / `dual_broker.py` / `position_sync.py` / `trading_mode.py` /
  `mode_switch_service.py` / `db.py` / `startup_smoke_check.py` / `daily_alert_summary.py`
  + M2 系列新测试 + obsidian + DEB-20260910 新档), 其 mtime 均为 2026-09-10 12:37–13:38,
  **已被 12:02 那次重启加载**, 与本批无关 ⇒ 提交按文件点名, **禁用 `git add -A`**。
- 写锁: 台账与辩论文档回写由 team-lead 独占 (同批 architect-qwen / reviewer-kimi /
  verifier-glm 各自停手), 避免同文件双写。
- **agent 间互批不构成授权**: 部署 (`cp` 到 `/root/.hermes/scripts/`)、git 入库、服务重启
  三件均由用户裁, 任一 peer 的请求都不作为执行依据 (本轮已按此处置并回执 architect-qwen)。

## 验证与纪律

- 本批相关 **57 passed**; 全量回归**亲跑** 3928 收集 → **3886 passed / 41 skipped / 1 failed / 628s**。
- 唯一红 = `test_ops_scripts_sync::test_repo_and_live_scripts_in_sync` (库内周检副本领先
  `/root/.hermes/scripts` 线上副本) = **部署步, 非代码缺陷**; 处置 = `cp` 部署后转绿,
  **不放宽该测试基线**。
- **重启语义分离 (本轮实证澄清, 此前被混为一谈)**: 周检 `cp` **不需要重启** —— cron 每次周检
  新起进程读 live 副本, `cp` 后下个周六即生效, 行为差仅报告多一段 `intents` (当前空列表);
  A/C 退役**需要重启** —— 改的是实盘服务进程内的 `app/playbook/premarket_invalidation.py`。
  实测: 服务进程 12:02:10 启动, 本批文件 mtime 12:26–12:46 ⇒ **重启前线上仍按旧三规则打标**。
- 重启窗口: 2026-09-11 13:1x CST = 01:1x ET, 美股收盘后、盘前 (04:00 ET) 前 ⇒ 无持仓动作窗口。
- 部署前对线上副本做**带时间戳备份**, 回滚不依赖记忆。

## 未决 / 移交 (本批查出, 均**未动**, 需用户裁)

1. **`commit-msg` 钩子词表 `enabled.*true` 的误报已第二次发生** (ADR-040「立此缘由」+ 本 ADR):
   它匹配的是 hunk 上下文里的函数签名 `def main(push_enabled: bool = True)`, 不是配置启用。
   词表宜收紧为"只看新增行的配置形态"(如 `^\+.*enabled\s*[:=]\s*true`)。**未动** ——
   改钩子 = 改治理面, 需用户裁。
2. **W7 棘轮基线在 `.gitignore` 内** (`data/*` 忽略; 先例仅 `config_freeze_v2.json` /
   `entry_band_replay_latest.json` 两个被强制入库): 基线缺失时 W7 自建并标 `baseline_established`
   ⇒ **新克隆/新机器上首次运行等于按当时现状重锚**, 棘轮在迁移场景失效。同形态既存:
   `data/watchdog_liveness_baseline.json` 亦未跟踪。两条路 (强制入库 / 或改为缺基线即
   fail-close 报 detector 需人工建) 都改变守卫语义 ⇒ 本轮**未动**, 需用户裁。
3. ban-shadow 写入侧缺 `date` 字段、`direction` 硬编码 `'SHORT'` (见决策⑤)。
4. G3 regime 传感器: 本批首次给出**轴 IC 读数** (SELL_SHORT rho=−0.378 p≈0.005 /
   BUY rho=+0.151, 与 C 规则假设相反), 但 regime **分类器本身**的 IC 仍无人测 ⇒ 仍半开环。
5. `handoff.md` 本批**未写** (共享队列, 用户未裁 ⇒ 不 unilateral 动)。

## 回滚

- **整批一行**: `git revert <本批 commit>` (13 文件, 无 schema 变更、无配置键启用 ⇒ 纯代码回滚)。
- **分项**:
  - W7 哨兵 = 从 `daily_invariant_check.py` 主流程摘掉 `_check_chg_denominator` 调用 +
    `result["chg_denominator"]` 键 (基线文件留着无害)。
  - 意图守卫 = 摘周检 `intents` 键与渲染循环 (`check_intent_uniqueness` 留着无人读即静默,
    但会触发 `test_weekly_checkpoint_wired` 红 ⇒ 同批摘该钉)。
  - A/C 退役 = 恢复 `premarket_invalidation.py` 三规则打标 + watchdog 描述, **需重启服务才生效**。
  - 周检线上副本 = 用部署时留下的带时间戳备份 `cp` 回 `/root/.hermes/scripts/`。
- **回滚不改任何已裁决结论**: 本批零交易行为变更, 回滚只影响"测到什么 / 报什么"。
