# CUTOVER 决策包 — 2026-09-09

> **性质**: 裁决文件, 零代码改动。三项待决 + 门1 重裁 + 翻转日 runbook。
> **上游**: docs/PROPOSAL-20260909-cutover-dual-arm-routing.md (S2 已证伪作废, 单库翻转模型确立)
> **已落地前置**: RFC-080 S0/S1/S2'/S3b/S4 (050c268c, c70c60d2, 1616e35a) — 现状零行为变化
> **红线**: C2 (打标前严格不切实盘) 仍锁。本包不解锁任何执行。

---

## 一、研究臂归宿 (唯一硬阻塞) — 含 S1 pin 的 30 天定时炸弹

### 事实链 (已逐行核实)
1. 毕业门禁/IC 读 `playbook_instructions`, 已被 S1 pin 到 `ftseek_us.db` (研究库)。
2. **翻转日后** (TRADE_ENV=0), 指令生成器经 PlaybookDB() TRADE_ENV 路由 → 新指令写
   **live 库**; 研究库从翻转日起停止生长。
3. `compute_playbook_ic(days=30)` 按 30 天窗口取样 → **翻转 +30 天后研究库窗口空**
   → 门禁判"证据不足" → 强制降级 sim (playbook_service:1121) → 降级记录经
   get_trade_records_db() (TRADE_ENV=0) 写进 live 库 = 静默回滚 cutover + 模拟行混入实盘库。
   方向 fail-safe (自动退回纸面), 但**无人知晓的 cutover 自毁**。
4. 指令不能单独 pin 在研究库: 平仓时 outcome 回填 (win/loss) 走 `_close_trade_record`
   **同一 conn** 写 trade_records + playbook_instructions (S2 证伪的同一条原子性事实)
   → 指令必须与 trade_records 同库。
5. 研究周报 `weekly_trade_summary.py` 硬编码 sim 载体 (刻意设计, 防"换库伪装没交易",
   已有 `--db` 参数); `exit_scorecard.py:244` 有 `trade_mode != 'live'` 过滤 →
   翻转后全部行都是 live → scorecard 读空。

### ~~方案 A: 翻转日证据迁移~~ — ❌ 已被用户裁决否决 (2026-09-09)

> **用户裁决**: "模拟交易和实盘交易不要混为一谈，包括历史数据"。
> 方案 A 把 sim 历史行复印进 live 库 = 正是混为一谈。**作废**。
> 同日 demix 事件 (§五) 实证了混同的真实代价: 4 行 sim 仓因误标被搬进 live 库,
> 污染实盘周报与台账对账。

### 方案 A' (替代, 遵守两账本永久物理分离):
**原则: sim 历史永远留在 sim 库 (翻转后冻结为只读档案), live 历史永远在 live 库。
零行跨库复制。跨库只允许"只读研究视图", 不允许"账本合并"。**

翻转日 runbook (顺序执行, 每步可独立验证):
1. **证据快照归档** (不搬行): 翻转前把 sim 臂最终成绩单导出为独立 JSON 档案
   (`data/cutover/sim_evidence_<date>.json`: IC 报告/回合统计/连亏序列, 即门禁当日
   实算结果) + 用户签字引用。这是预注册证据的冻结快照 — sim 数据以 sim 身份归档,
   永不改标 live。
2. **门禁证据源重指**: 毕业门禁 pin 从研究库改指 live 库 PLAYBOOK_LIVE 行
   (db_pool `ftseek_research` 一行 + 门禁加试点期语义: 翻转后 n<门槛 期间引用
   §1 快照 + 累积 live 证据, 判"试点攒样中"而非"证据不足")。
3. **降级语义改 HALT**: 翻转后门禁不达标的动作从"降级 sim 下单"改为"**停开新仓**"。
   原降级路径在 TRADE_ENV=0 下会把 sim 记录写进 live 库 = 混同 (§五同款病)。
   HALT 保持账本纯净 + 强制人工介入。
4. **翻开关**: TRADE_ENV=0 + AUTO_TRADE_ACCOUNT=real + ALLOW_REAL_TRADING=true
   (三者必须同翻 — S2' 互锁已把半翻态 fail-close)。
5. **研究脚本随翻 (各自诚实, 不合并)**: weekly_trade_summary 保持读 sim 库 —
   翻转后如实报"sim 臂已于 <date> 停止" (加一行停臂说明, 防"无交易"误读);
   exit_scorecard 加 `--arm live` 读 live 库 PLAYBOOK_LIVE (sim 判分器冻结存档)。
6. 验证: 门禁读到 live 证据 + 试点期语义生效; 两库 W3 混同守卫零告警。

否决 B (门禁双库 union 读): union 统计 = 把 sim 战绩与 live 战绩混进同一个 IC 数字,
违反"不混为一谈"裁决; 且 outcome 回填原子性仍要求指令与 trade_records 同库。
否决 C (研究臂停产不清尾): 冻结不清尾会让周报/scorecard 静默读空 (伪装"没交易")。
A' 与 C 的区别: A' 显式归档 + 显式停臂说明 + 判分器换臂, 每个读方都有诚实出口。

---

## 二、S5 FLAT_ALL 政策

### 事实链 (已逐行核实)
- position_exit_monitor:632-660: 账户级回撤 (capital_safety.current_equity, 含全部持仓)
  **刻意**参与 FLAT_ALL 触发 (原注释: "手动大仓位亏损不触发 FLAT_ALL" 被当作 bug 修掉)。
- 执行 (:810): 只平 `managed_positions` (auto 管的仓), 不碰手动仓。
- 现状无冲突: capital_safety 喂 sim 账户, 手动仓在另一 REAL 账户不计入。
- **翻转后**: 同一 REAL 账户 → 手动仓暴亏拉低账户净值 → 触发 FLAT_ALL →
  **平掉无辜的 auto 仓** (auto 仓自己的 SL/TP 本来在管它们)。

### 方案 (推荐 B: 触发源分权)
- **managed 回撤 ≥ stop_pct** (auto 自己亏出来的) → FLAT_ALL 照旧全平 (不变)。
- **账户级回撤 ≥ stop_pct 但 managed 未超** (可能手动拖累) → 动作降为
  **HALT_NEW** (停 auto 新开仓, 已有 auto 仓留给自身 SL/TP 管)。
- 理由: 连坐平仓惩罚的是无辜仓; 停新开仓已切断 auto 对恶化账户的继续暴露;
  符合"SL 兜底、耐心不硬扛、留空间"哲学。改动面: 动作判定块 ~10 行 + 配置键
  `flat_all_account_trigger_action: halt_new` (默认保持现行为, 翻转日生效 —
  现状零行为变化)。
- 备选 A (保持现状全平): 账户级保护最强, 但手动风险拥有对 auto 仓的处决权。
- 备选 C (混合: 账户级只平亏损中的 auto 仓): 复杂度高, 收益边际, 不推荐。

---

## 三、open_positions 陈旧行 (低危, 不阻塞)

### 事实链 (实弹核实 9/9)
- live 库 8 行手动仓全部 size=0.0, updated_at 停在 6/13~6/25 (僵尸行)。
- 券商真值: MU 空头 -10 实际在仓 (9/4 SELL_SHORT FILLED + 9/9 快照) — 台账 size 没跟上。
- 现行影响: ManualHoldRegistry 把这些标的当"手动持有" → auto 禁碰 (fail-safe 方向,
  只多拦不漏拦); S3a 撤回已证明**不能**用 size>0 过滤解决 (会解除 MU 的真保护)。

### 方案 (推荐: 每日对账修数, 不动判定逻辑)
live_ledger_sync (已降级为兜底对账, 每日跑) 顺手加一步: 拉 broker REAL 持仓真值,
对 account_type=real 的 open_positions 行做 size 校正 (在仓→真值, 已平→标记/清理)。
判定逻辑 (registry) 一行不动。P2 排期, 翻转日前完成即可。

---

## 四、门1 判据重裁 (RFC-062 原判据已随验证期届满失效)

既有裁决 (不重新发明, 直接引用):
- **转正门槛**: OOS ≥15 日 + 净均 >0 + 毛利 t>1 + ADF 平稳; 双窗口胜率 ≥40% 才采信。
- 周六 09:05 周检自动跑 (已挂载)。
- 切实盘须**预注册签字** (C2 红线): 达标 ≠ 自动翻转, 翻转日 = 周检达标报告 + 用户签字
  + 本包 §一 runbook 执行。

需用户确认: 门1 判据沿用上述既有裁决, 不另立新标准。

---

---

## 五、sim/live 混同清理事件 (2026-09-09, 用户裁决触发)

**用户裁决**: "模拟交易和实盘交易不要混为一谈，包括历史数据" — 直接否决原方案 A
(sim 历史复印进 live 库), 改为 A' (两账本永久物理分离, 零跨库复制)。

**审计发现现存混同 (已清)**: 两库各有对方账本的行 —
- sim 库 3 行 trade_mode='live' (真钱标签误进研究账本)
- live 库 2 行 trade_mode='simulate' (PLAYBOOK_SHADOW 影子误进实盘账本)

**根因 (活跃源, 已修)**: position_sync external_close INSERT (:1699) **硬编码
trade_mode='live'**, 无视被平仓位的真实 broker env。sim 仓被外部平仓时打上真钱标签
→ RFC-078 迁移脚本按标签把它搬进 live 库 → 混同。

**broker 真值核验 (唯一权威)**: 拉 REAL env 成交历史 (253 笔, 06-01~09-10) 逐行核对
live 库 18 行 — 14 行有 REAL 成交铁证 (正确属实盘), **4 行 external_close
(MSFT#900/AVGO#901/TTE#902/BULL#904) 零 REAL 证据** = sim 仓误标。

**两阶段清理 (demix_sim_live_records.py + demix2_broker_verified.py)**:
- 阶段1 教训: 只验"两库都有副本"不够 — TTE 被按"live 有副本"删了 sim 侧, 但 broker
  真值说两边都非真钱, sim 侧才是正确归属 → **删错方向**。
- 阶段2 修正 (broker 真值定向): TTE 从备份恢复回 sim 库 (改标 simulate); AVGO 孤行
  移回 sim 库; MSFT/BULL sim 侧已有原始 PLAYBOOK_SIM 记账 → external_close 是重复残缺
  行, 直接删 live 侧。两库终验纯净 (sim trade_mode=live 残留 0, live trade_mode!=live 残留 0)。

**防复发 (已落地)**:
- 活跃源: external_close trade_mode 改为跟随 AUTO_TRADE_ACCOUNT (与被扫描仓位 env 同源)。
- 每日守卫: daily_invariant_check W3 双向混同检测 (sim 库现 live 行 / live 库现 sim 行 → 当天告警)。
- 单测钉桩: test_rfc080_external_close_label.py (标签语义 + 源码锚点防回归硬编码)。

**门2 对照组与 A' 的协调**: A' 第5步 sim 研究周报翻转后停臂, 但 RFC-062 门2 要求
试点期 SIM 并行对照组 — 二者不冲突: 对照组 = **影子模式** (只记录不下单, 如
PLAYBOOK_SHADOW/env_separation_shadow), 本就属 sim 账本身份, 与 live 物理分离, 不违反
"不混为一谈"。翻转后 sim 影子臂继续跑 (对照组), 只是不再产生"可成交的 sim 实盘臂";
weekly_trade_summary 停的是后者, 影子对照另有载体。

---

## 裁决点汇总 (3 个)

| # | 问题 | 推荐 | 备选 |
|---|------|------|------|
| 1 | 研究臂归宿 | A': 两账本永久分离 + 证据快照归档 + 门禁改指 live + 降级改 HALT (§一, A 已被用户裁决否决) | B union 读 (混战绩,违反裁决) / C 停产不清尾 (静默读空) |
| 2 | FLAT_ALL | B: 账户级触发→HALT_NEW, managed 级→FLAT_ALL 不变 | A 保持全平连坐 / C 混合 |
| 3 | 陈旧行 | 每日 sync 对账修数, registry 不动 | 不修 (容忍多拦) |
| 附 | 门1 判据 | 沿用既有 OOS≥15日+净均>0+t>1+ADF+双窗口≥40% + 签字 | — |
