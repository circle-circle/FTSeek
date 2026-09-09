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

### 方案 (推荐 A: 翻转日证据迁移)
翻转日 runbook (顺序执行, 每步可独立验证):
1. **迁移脚本** `scripts/cutover_migrate_research_evidence.py` (翻转获批后先建, 干跑默认):
   playbook_instructions + trade_records (sim 历史) 幂等复制 sim库→live库。
   schema 差异已核: live 库 playbook_instructions 缺 22 列,
   PlaybookDB._migrate_instruction_columns 首开自动补齐 (S2 前置核查已验证)。
2. **重指 pin**: db_pool `ftseek_research` 一行改指 live 库 (S1 pin 的设计意图就是
   "研究证据载体"单一别名, 载体切换只动这一处)。
3. **翻开关**: TRADE_ENV=0 + AUTO_TRADE_ACCOUNT=real + ALLOW_REAL_TRADING=true
   (三者必须同翻 — S2' 互锁已把半翻态 fail-close)。
4. **研究脚本随翻**: weekly_trade_summary cron 加 `--db live库` (参数已存在);
   exit_scorecard 的 `trade_mode != 'live'` 过滤改为按 source_type 白名单
   (翻转日同批, 否则 scorecard 静默读空)。
5. 验证: 门禁在 live 库读到完整历史 IC + 新指令持续入册; 周报/scorecard 出数。

否决 B (门禁双库 union 读): 永久双读复杂度 > 一次性迁移; 且 outcome 回填原子性
仍要求指令与 trade_records 同库, union 救不了平仓路径。
否决 C (研究臂停产): RFC-062 门2 要求试点期对照组; 且 IC/OKR 周报是现役治理面。

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

## 裁决点汇总 (3 个)

| # | 问题 | 推荐 | 备选 |
|---|------|------|------|
| 1 | 研究臂归宿 | A: 翻转日证据迁移 + pin 重指 (runbook 五步) | B union 读 / C 停产 (均否决理由见 §一) |
| 2 | FLAT_ALL | B: 账户级触发→HALT_NEW, managed 级→FLAT_ALL 不变 | A 保持全平连坐 / C 混合 |
| 3 | 陈旧行 | 每日 sync 对账修数, registry 不动 | 不修 (容忍多拦) |
| 附 | 门1 判据 | 沿用既有 OOS≥15日+净均>0+t>1+ADF+双窗口≥40% + 签字 | — |
