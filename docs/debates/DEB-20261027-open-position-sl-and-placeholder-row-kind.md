# 议题：open_positions 止损/时区一致性 + 供给占位行结构化（2026-10-27）

> 关联：`DEB-20261027-da-disagree-abstain-tier-fix.md`（同日 DA 链路两处缺陷修复）。
> 路径约定：本 DEB 裸名 `position_sync.py` / `db.py` / `pipeline.py` 指本仓
> `/opt/ftseek/FTSeek-US`；`exit_monitor_flow.py` 指本仓 `app/playbook/pipeline_parts/`。

## 状态

- **裁决**：已实施（批次 A / B / C 全部落地）。
- **用户指令**：2026-10-27「按照建议执行」（指上一轮提出的 P0 修复 + 占位行设计优化）。
- **用户明确划出范围**：「重启守卫不要限死了，暂不动」⇒ 上一轮建议的 P0-1
  （把 `restart_ftseek_us_guarded.py` 变成唯一重启路径）**本批不做**，见 §5.1。
- **性质**：全部为**记录一致性修复**，**零交易行为变更**（论证见各批 §「零行为变更」段）。

## 0. 一句话结论

两个独立的记录层缺陷：① `open_positions` 的止损与 `entry_time` 与出场链不同源
（止损缺失 / 时区差 12h）；② 供给占位行靠**字符串前缀**识别，污染 12+ 个读数面。
本批把二者分别改为「同源取值」与「结构化列标记」，均不动任何交易判定。

---

## 1. 批次 A：open_positions 的 SL/TP 与 entry_time

### 1.1 病灶（2026-10-27 实测）

| 仓 | open_positions | trade_records | 差在哪 |
|---|---|---|---|
| `US.DYN` | `initial_sl=0, current_sl=0, initial_tp=0`；`entry_time=2026-10-27T21:47:45.691846`（naive CST） | `metadata_json.stop_loss=15.17`；`entry_time=2026-10-27T09:47:21-04:00` | SL 缺失 + 时区差 12h |
| `US.UNH` | 同上（SL=0，naive CST） | `stop_loss=392.92`；ET-aware | 同上 |
| `US.MRK`（对照，走 playbook 路径） | `initial_sl=134.51`；ET-aware | `stop_loss=411.6` | 正常 |

**根因**：`app/core/position_sync.py` 的 `_is_pb_sim` 分支（Playbook SIM 仓位走
broker 同步建仓）写 `open_positions` 时硬写 `initial_sl=0, current_sl=0,
initial_tp=0`，且 `entry_time=datetime.now().isoformat()`（宿主 CST、naive）。

### 1.2 先例与更正（两处）

1. **R-12 已有先例，但本分支被漏掉**：`app/storage/trade_store.py:1023-1036`
   按 **DEB-20260823 R-12** 把 `entry_time` 统一到 ET 口径（实证原 bug：
   `open_positions=2026-08-19T21:36:54` vs `trade_records=2026-08-19T09:36:54`）。
   但 R-12 只覆盖了 `trade_store.upsert_position_simple`（playbook 路径），
   `position_sync` 的 `_is_pb_sim` 分支当年未纳入 ⇒ 本次补齐，不是新造口径。
2. **我最初把病灶归给「P3 补录路径」，不准确**。实测：**P3 补录只写
   `trade_records`，从不写 `open_positions`**；`open_positions` 那行是
   `position_sync.py:1533` 的 `_is_pb_sim` INSERT 建的。修复点因此更小更准。

### 1.3 修法

- 新增助手 `app/core/position_sync.py::_playbook_position_fields(cursor, symbol)`：
  - SL/TP 真源 = 该 symbol **未平** `PLAYBOOK_*` `trade_records` 的
    `metadata_json.stop_loss` / `take_profit` —— **与出场链同一真源**
    （出场链读 `trade_records`，见 `exit_monitor_flow.py:88`）；
  - `entry_time` 真源 = 同一条 `trade_records.entry_time`（ET-aware）；
  - 合并语义**只补空、不覆盖已有权威值**：已有 `initial_sl>0` 保留；已有
    带时区的 `entry_time` 保留；否则取 `trade_records`；再取不到 → SL/TP=0、
    `entry_time=now_et()`（回退也是 ET 口径）。
- `_is_pb_sim` 分支改用该助手的返回值。

### 1.4 零行为变更的论证

出场链（`exit_monitor_flow.py:88`）读 **`trade_records`**，**不读** `open_positions`。
实证：`US.UNH` 的早期紧SL 日志显示 `SL 392.9200→406.6825`，正是其
`trade_records.metadata_json.stop_loss` ⇒ **止损一直是活的**。

`open_positions` 的 SL/TP 仅被两处消费：① 重启恢复路径
（`scheduler_core.py:1685→1735/1737`）；② 只读审计/报表。⇒ 本改动只让两表一致，
**不改变任何下单、仓位、止损、结算决策**。

> **不新增运行期兜底分支**：曾考虑「恢复路径在 `initial_sl<=0` 时从
> `trade_records` 回补」（兜底 B），复核后**放弃** —— `open_positions` 的 SL 在
> 恢复路径的唯一消费方是 `app/risk/engine.py:333` 算「距止损 20%」的**告警阈值**
> （非止损门），为一个告警级消费方加常驻运行期分支不值（YAGNI）。历史行改用
> 一次性回填脚本处理。

### 1.5 历史行回填

新增 `scripts/backfill_open_position_sl_tp.py`（默认只报告，`--apply` 才写，幂等，
写前落审计 JSONL 到 `data/audits/`）。

**范围严格限定本批修的那条写入路径**：① `trade_records` 有止损而
`open_positions` 记成 0；② `open_positions.entry_time` 无时区。
**不含** `initial_tp` 缺失 —— 那是**另一条写入路径**
（`playbook_service.py` → `trade_store.upsert_position_simple`，其列清单本就不含
`initial_tp`）的既有缺口，与本批无关，另案记录（§5.3）。

实测命中 1 行（`US.UNH`），已 `--apply` 执行；再跑 0 行（幂等）。

### 1.6 验证

- 契约测试 `tests/test_open_position_sl_tp_contract.py`（9 例）：助手取值与合并
  语义、量纲（绝对价格 USD）、源级锚点（分支不得再硬写 `0,0,0` + `datetime.now()`）、
  生产回归（未平 PLAYBOOK 仓不得丢止损、entry_time 必须带时区）。
- 生产回归测试**先红后绿**：修前检出 `US.UNH` 丢止损 ⇒ 回填后转绿。

> **一条被纠正的不变式**：曾把「`open_positions.initial_sl` 必须等于
> `trade_records.metadata_json.stop_loss`」写成不变式，实测 `US.TSM` 不等
> （422.40 vs 411.60）—— 因为前者是**执行时 re-price 后**的止损、后者是**入场快照**，
> 且出场链的早期紧SL/保本/锁利会继续移动生效线。⇒ 正确不变式是「**不得缺失**」，
> 不是「必须相等」。已改写测试。

---

## 2. 批次 B：供给占位行结构化（`row_kind`）

### 2.1 先纠正前提：占位行是**承重的**，不是垃圾

| 用途 | 消费方 | 拆了会怎样 |
|---|---|---|
| 漏斗供给审计（唯一 DB 级「筛过但未提名」证据） | `scripts/short_funnel_shadow_tag.py:118-148` | RFC-109 门提案的唯一量化基础消失 |
| `outcome='skipped'` 的写回来源 | `app/playbook/shadow_tracker.py:216-221` | 2649/2704 历史行的 outcome 语义断链 |
| 45 日标的全集 | `scripts/trend_state_recorder.py:58-73` | 趋势数据集少了「被筛未提名」这一人口 |

同时它们污染 **12+ 个读数面**（周报 `total_decisions` ×4、`pre_market_check` 报
「当日指令 72 条」实为 17、dashboard 跳过段、`decision_view` 每天 55–70 条
`decision_id=''` 幽灵条目、`db.get_stats` 的 conf 分布被 OTHER 桶摧毁、
`okr_status`/`unified_trade_metrics` 的 LIMIT-500 窗口被吃光）。

**关键事实**：`playbook_instructions` 里**没有任何一条**占位行参与下单、仓位、
结算决策 —— 对订单流完全惰性。

### 2.2 两条硬约束

1. `skip_reason.startswith("非当前通道候选")` **必须继续可用** ——
   `scripts/observe_nightly_supply.py:109` 按它过滤「实体供给」；改了文案 ⇒
   供给漏斗虚胖 4 倍 ⇒ 假 P1/P2 告警。另有
   `scripts/deb20261007_fee_gap_forward.py:73` 按子串分类、
   `tests/test_da_gate_bucket_label.py:54` 钉死前缀。
2. 「被筛但未提名」集合必须**换家**，否则 §2.1 的三个用途断裂。

### 2.3 已有归属地（不新建表）

**`playbook_signal_context`** —— 实测今日 72 行，与当日 72 条指令**完全对齐**；
55 个占位 symbol **55/55 全部命中**。它在 `pipeline.py:1306` 于**投票之前**按
symbol 粒度写入 ⇒ **这就是「被筛但未提名」的天然归属地**。
（另一候选 `data/shadow/candidate_funnel.jsonl` 只覆盖「DA 评过分的」，漏掉
「从未进 DA 评分」的那部分 ⇒ 不完整。）

### 2.4 修法（L1：把「占位」从文案变成结构）

- `playbook_instructions` 新增列 `row_kind`：
  `'instruction'`（真指令，默认）/ `'supply_placeholder'`（供给占位行）。
- 写入端结构化赋值：`pipeline.py` 的占位行创建点显式传
  `row_kind="supply_placeholder"`；`_instruction_to_db_dict` 落库；
  `Instruction` dataclass 新增同名字段（默认 `'instruction'`）。
  DA skill 直写的行**不传**该字段 ⇒ 落库默认 `'instruction'`，语义不变。
- `skip_reason` 文本**逐字保留**（硬约束 1 成立）。
- 读数面统一用 `app/playbook/db.py::ROW_KIND_EXCLUDE_PLACEHOLDER_SQL` 过滤，
  不再各处硬编码字符串。
- 迁移：`db.py::ensure_tables` 的 `needed` 字典自动 ALTER；
  RFC-066 迁移的新表列清单同步补 `row_kind`（该迁移有「列集合校验」，缺列会中止）。

### 2.5 历史行回填

新增 `scripts/backfill_instruction_row_kind.py`（默认只报告、`--apply` 才写、幂等、
写前落审计）。范围 = **既有占位行前缀契约**（与 `observe_nightly_supply.py:109`
同源，不引入第二套判定）。

---

## 3. 批次 C：读数面收口（4 + 1 个）

| # | 读数面 | 旧读数 | 新读数 |
|---|---|---|---|
| 1 | `app/review/weekly_review.py::_get_weekly_signal_funnel` | `total_decisions` 虚胖约 4×；`by_direction_strength` 出现巨型 SKIP 桶 | 只统计真指令 |
| 2 | `scripts/pre_market_check.py` §5e/§5f | 「当日指令统计 共 72 条」（实 17）；方向构成/regime 构成被占位行稀释；跳过成因 TOP-3 恒为「非当前通道候选」 | 报真指令数 |
| 3 | `scripts/decision_view.py` | `decision_id IS NOT NULL` 对 `decision_id=''` 成立 ⇒ 每天 55–70 条幽灵「未成交指令」 | 同时排除占位行 **并**修 `decision_id <> ''` |
| 4 | `app/playbook/db.py::get_summary_stats` 的 `conf_stats` | `OTHER` 桶 ~1400 条占位行、`avg_conf=0` ⇒ conf 分布被摧毁 | 只统计真指令 |
| 5 | `app/playbook/weekly_report.py::_analyze_user_skips` | `total_skips` 每日 ~50–70（占位行被当「用户跳过」） | 只数真跳过 |

**口径变更登记**（宪章要求逐项登记）：

| 读数 | 旧口径 | 新口径 | 影响 |
|---|---|---|---|
| 周报 `total_decisions` | 含占位行 | 仅真指令 | 数值下降约 4×（**口径修正, 不是数据丢失**） |
| 周报 `total_skips` | 含占位行（当作用户跳过） | 仅真跳过 | 语义修正：占位行是系统供给痕迹, 不是用户行为 |
| `pre_market_check` 当日指令数 | 含占位行 | 仅真指令 | 报告值 72 → 17 |
| `decision_view` 未成交指令 | 含 `decision_id=''` 占位行 | 仅真指令且 did 非空 | 幽灵条目消失 |
| `get_summary_stats.conf_stats` OTHER 桶 | 含占位行 | 仅真指令 | conf 分布恢复意义 |

**顺手修（drive-by）**：`scripts/decision_view.py` 的打印行 `pnl={o['realized_pnl']:>8}` —
`realized_pnl` **可空**（未结算/无 pnl 的成交行）⇒ `None:>8` 抛
`TypeError: unsupported format string passed to NoneType.__format__`，该脚本**根本产不出输出**。
该崩溃在 **HEAD 原版同样存在**（已把 HEAD 版放到同目录实跑对照，崩点一致）⇒ 与占位行过滤无关，
属既有 bug；但它挡住了本读数面的验证 ⇒ 一并修（只影响打印，零行为变更）。

**未收口（留给后续批，见 §5.2）**：`scripts/okr_status.py:18`、
`app/metrics/unified_trade_metrics.py:136`（`include_skip=True` 的显式 opt-in
消费方，作者已知并接受）、`scripts/short_supply_funnel_report.py:76`、
`scripts/playbook_chain_audit.py:105`、`app/web_dashboard/routes/chain.py:62`、
`scripts/da_chain_health_report.py:83`、`app/playbook/feishu_push.py:229/299`
（推送里的「跳过 N 只」是给人看的现状描述，改与不改属展示口径，另议）。

**必须保持工作的消费方**（占位行仍以**同样的文本**留在表里，只是不再被上述读数面
计入）：`observe_nightly_supply.py`、`short_funnel_shadow_tag.py`、
`trend_state_recorder.py`、`shadow_tracker.py`、`feishu_push.py`。

---

## 4. 验证

| 项 | 结果 |
|---|---|
| `tests/test_open_position_sl_tp_contract.py` | **9 passed**（生产回归项在回填后由红转绿） |
| `tests/test_instruction_row_kind_contract.py` | **9 passed**（迁移+回填后生产回归项由 skip 转 pass） |
| 相关回归（`test_playbook_db/pipeline/feishu_push/arbitration_shadow/instruction_generator_horizon` + 两个新契约） | **168 passed** |
| `tests/playbook` 全量（1345 例） | 1336 passed / **4 failed + 5 errors** ⇒ **既有红点，非本批引入**（见下） |
| `py_compile` 全部改动文件 | OK |

> **既有红点的证明方式**（不靠推测）：把 **HEAD 原版**（不含本批任何改动）检出到独立 worktree、
> 挂同一份生产 `data/` 跑**同一条** pytest 命令 ⇒ **14 failed / 1321 passed / 27 errors**，
> 比本批版本（6 failed / 1334 passed / 19 errors）**更差**；两次运行的**失败集合还不同**
> （`test_entry_revalidation` vs `test_exit_recovery`）⇒ 非确定性顺序污染。
> 另: 这些用例**单独跑全部通过**（实测 16 passed）。
> ⇒ 记为既有红点，如实记录未修。

## 5. 仍未落地 / 待裁

1. **重启守卫（用户明确划出范围，本批不做）**：「把
   `restart_ftseek_us_guarded.py` 变成唯一重启路径 + 交易时段硬拒」。
   现状：该守卫是一次性 `at` 任务的可选脚本，其两条规则（工作区干净 / 美股不在盘中）
   在 2026-10-27 09:39 ET 那次并行会话重启中**本可拦下**，但守卫不是强制路径。
   **用户裁决：暂不动。** 留待用户另批。
2. **L2 其余读数面**（§3 末表）：逐个登记口径后再改，不在本批。
3. **`upsert_position_simple` 不写 `initial_tp`**：`playbook_service.py:2256` →
   `trade_store.upsert_position_simple` 的列清单不含 `initial_tp` ⇒ MRK/ARM/CRM
   等新仓的 `open_positions.initial_tp` 为空。出场链用
   `trade_records.metadata_json.take_profit` ⇒ 止盈判定不受影响；仅记录不一致。
   **与本批批次 A 不同写入路径，故未纳入**（§1.5）。
4. **L3 数据搬家**（占位行迁出主表）：等 L1/L2 读数面全部收口后另立批。
5. **`US.MSFT` EXT 仓已携 5 个交易日**（10-20 07:31 ET 盘前开于 $535.60，
   `sl_broker_order_id` 空，盘前影子记 `underwater_no_extended_loss_stop`）：
   EXT 持仓无券商侧止损，另案。

## 6. 执行记录（部署）

- **迁移时机**：`row_kind` 列由 `ensure_tables` 自动 ALTER ⇒ 在服务重启时落库。
  故执行顺序 = ① 代码落地 → ② 干净重启（加载三批代码 + 迁移）→ ③ 跑两个回填脚本
  → ④ 回归与契约测试 → ⑤ 提交。
- **重启前核对**（军规）：
  - 在途出场 `exit_intents` **238 全 BOOKED，non-booked = 0**；
  - 持仓 4 个（`US.MSFT` EXT + `US.MRK`/`US.TSM`/`US.CRM` playbook_sim）；
  - `git status --porcelain -- app/ scripts/` 逐行核对：`app/` 下 **7 个改动全属本批**；
    `scripts/` 另有 **2 个他人 WIP**（`da_chain_contract_audit.py` / `da_scope_confidence_audit.py`，
    DEB §12 批）—— 它们是 **cron 独立脚本、不被服务 import**，重启不会载进交易进程，
    且**不纳入本次提交**。
- **重启**：`systemctl restart ftseek-us` → 2026-10-27 23:22:46 CST，PID **185826**。
- **启动冒烟**：`scripts/startup_smoke_check.py --wait 90` ⇒ **7/7 全过**
  （ConfigValidator / 漂移检测 / 热重载 / SSOT 0 BLOCKING / 双臂安全 / 退出快循环 / 零 Traceback）。
- **迁移实测**：日志 `迁移: 已补列 playbook_instructions.row_kind (TEXT NOT NULL DEFAULT 'instruction')`。
- **回填实测**：
  - `backfill_instruction_row_kind.py` ⇒ 待标记 **2704 行 / 49 个交易日**（首日 2026-08-19、
    末日 2026-10-27），`--apply` 已执行；再跑 **0 行**（幂等）。
  - `backfill_open_position_sl_tp.py` ⇒ 批次 A 的历史行已于早前 `--apply` 修掉 `US.UNH`，
    本次再跑 **0 行**（幂等）。
- **读数面实测（部署后）**：
  - `pre_market_check` 「当日指令统计」= **共 17 条 | DA=17**（旧口径 **72 条**）；
    「指令方向构成」= BUY=11 SELL_SHORT=3；跳过成因从恒为「非当前通道候选」变为
    **真实跳过** `entry_confirm_reversed×3`。
  - `get_summary_stats(30).conf_stats` ⇒ `DA n=291 avg_conf=50.6` / `UE n=10 avg_conf=56.8`
    —— **被摧毁的 OTHER 桶（~1400 行 avg_conf=0）消失**。
  - `decision_view --days 3` ⇒ **25 决策**（旧的每日 55-70 条 `decision_id=''` 幽灵条目消失）；
    顺手修的 `realized_pnl` 空值崩溃已消，脚本可正常出报告。
  - `row_kind` 分布：`instruction 1690` / `supply_placeholder 2704`；今日 17 + 55。
- **未回填历史行**：按批次 A 的既定裁决，`entry_time` 的历史 naive 行**不回填**
  （只保证新行合规 + 在本 DEB 记录口径差异），避免动历史时间戳。

## 7. 复查清单（请第二 agent 逐条打假）

1. 读 `app/core/position_sync.py` 的 `_is_pb_sim` 分支，确认 SL/TP/entry_time
   均来自 `_playbook_position_fields`，且不再出现 `0, 0, 0, datetime.now()` 硬写。
2. 读 `app/core/position_sync.py::_playbook_position_fields`，确认：真源是
   `trade_records.metadata_json`；只补空不覆盖；回退 `now_et()`；排除已平仓与
   非 PLAYBOOK 来源。
3. 复跑 `scripts/backfill_open_position_sl_tp.py`（不带 `--apply`）⇒ 应输出
   「无需修复 (0 行)」；确认它的范围**不含** `initial_tp` 缺失的行。
4. 读 `app/playbook/db.py`：确认 `row_kind` 在 `needed` 字典、RFC-066 新表列清单、
   独立 CREATE TABLE 三处都在；`INSERT` 的占位符数量与参数数量匹配
   （新增 1 个参数 ⇒ `VALUES(...)` 末尾的 `?` 也要多一个）。
5. 确认 `skip_reason` 文本**逐字未改**（`observe_nightly_supply.py:109` 与
   `deb20261007_fee_gap_forward.py:73` 的契约未破）。
6. 复跑 `scripts/backfill_instruction_row_kind.py` ⇒ 0 行（幂等）。
7. 复算 5 个读数面的新旧值，确认只有占位行被排除、真指令与真跳过一个不少
   （尤其 `weekly_report._analyze_user_skips` 里 `outcome='skipped'` 的真跳过仍在）。
8. 确认 `decision_view.py` 的改动同时排除 `decision_id=''`（幽灵条目修复），
   且未误伤 UE 通道（`universe_expansion%`）与 DA 通道的行。
9. 确认本批**未**改任何交易门、仓位、退出、Prompt、名单、并发；**未**改
   `skip_reason` 文案；**未**动 `restart_ftseek_us_guarded.py`。

---

最后更新：2026-10-27（本条）
