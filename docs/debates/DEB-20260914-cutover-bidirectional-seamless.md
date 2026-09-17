# DEB-20260914 · sim⇄live 双向无缝切换的基建与待裁项

> **性质**: 取证 + 基建交付 + 待裁清单。**本批 `app/` 零改动**（落地物全在 `scripts/` + `tests/`）。
> **触发**: 用户裁「按照建议，目前只是现将基建做好，待未来可切换时，可以实现 sim 到实盘以及
> 实盘再切换为 sim，无缝实现」。
> **上游**: `docs/decisions/CUTOVER-DECISION-PACKAGE-20260909.md`（单向 sim→live）、
> `docs/PROPOSAL-20260909-cutover-dual-arm-routing.md`（S2 证伪 → 单库翻转模型确立）。
> **红线**: C2（打标前严格不切实盘）仍锁；本文件不解锁任何执行。

---

## 零、一句话结论

**当前不具备切换资格，且资格缺口是证据层面的，不是工程层面的。** 毕业门禁实算
`graduated=False`：`ic_t3 = -0.0155`（**IC 为负**，判据要求 >0.05）、`no_consecutive_5_losses = False`
（**存在连亏 5 笔**）、样本量 `n_t3 = 265` 充足；就绪报告 G1-1 净期望 **-$15.62/笔**
（毛期望仅 $0.79，avg 名义 $5467，haircut 15bps）、G1-4 退出 R:R **0.53** vs 阈值 1.29。
⇒ **第一优先级是把净期望转正，不是把切换工程做完。** 本批交付的是"等你想切的那天，
一条命令告诉你能不能切、切了会崩什么"的基建，以及 4 项需要评审的 `app/` 修复提案。

---

## 一、本批已落地（基建，零行为变化）

### 1.1 `scripts/cutover_preflight.py` — 双向切换预检（生产库纯只读）
一条命令 + 一个退出码回答"能不能切"。10 个探针，每条 finding 带 `读数/为何/处置/证据` 四段，
不留"只报警不给路"：

| 探针 | 内容 | 当前读数（2026-09-14 14:58 实测） |
|---|---|---|
| P0 | 三态一致性 + **双向**半翻检测 | ✅ `TRADE_ENV=1 AUTO=sim ALLOW_REAL_TRADING=false ISOLATION=1` 自洽 |
| P1 | 毕业门禁实算（研究库 pin） | ⛔ `graduated=False` `ic_t3=-0.0155` `n_t3=265` 未过 `ic_positive`/`no_consecutive_5_losses` |
| P2 | schema 漂移 + 切换日 DDL 面 | ⚠️ 共有表 35 · 列集不同 4 张 · 列序不同 1 张 · **切 live 触发补列 33 条** · 切 sim 0 条 |
| P3 | capital_state 双臂基线 | ⚠️ sim 1 行(envs=[1]) · **live 0 行** · live 无 `account_id` 列 |
| P4 | open_positions 臂判别/PK 碰撞面/僵尸行 | ⚠️ PK=symbol 且**无 source_type 列** · live 8 行全僵尸(size=0) · 两库同标的重叠 **0** |
| P5 | `sl_broker_order_id` 语义碰撞（**跨表铁证**） | ⚠️ 入场单号污染 sim **2 行** / live 0 行；真 SL 单号 live 1 行 |
| P6 | broker STOP 兜底里程 | ⚠️ `pb_stop_` 留痕 {sim:0, live:0} · 日志 `live STOP 单已挂` **0 次** |
| P7 | W3 双向混同守卫 | ✅ 两库 `trade_mode` 归属纯净（双向 0 行） |
| P8 | 研究臂窗口老化（翻转+30天炸弹） | ⚠️ 研究库 2151 行（近30天 1423）· A' 六步**一步未实施** |
| P9 | 台账连续性 / 研究脚本载体 | ⚠️ live 分臂 {MANUAL:12, POSITION_SYNC:2}，**auto 臂 0 行（从未跑过）**；`weekly_trade_summary.py` 硬编码 sim 载体 |

汇总 `block=1 warn=7 ok=2`，**exit=1**（P1 阻塞）。产物落 `data/cutover/preflight_*.json`
（`data/*` 已被 `.gitignore` 覆盖 ⇒ 零仓库污染）。

**两处判定口径上的刻意选择**（都不是省事，是防假信号）：
- **P5 不用"数字 vs 字母"启发式**，而用跨表铁证：`open_positions.sl_broker_order_id` ==
  同标的 `trade_records.futu_order_id`（入场单号）⇒ 证明该列装的是入场单。格式差异
  （sim `8170259` 纯数字 / REAL `FH1CB1B96E52BF2000` 字母数字）只作旁证。
- **P2 的 DDL 面从两库实测 diff 推导**，不复制 `app/` 里的 ALTER 清单 —— 复制必漂移，
  漂了以后工具会"报一个已经不存在的缺口"或更糟"漏报一个新增的"。

### 1.2 `scripts/cutover_rehearse.py` — 库副本沙箱彩排
把切换日的 DDL 与状态迁移**先在副本上跑一遍**，生产库零接触（启动即硬断言沙箱不在 `data/` 下；
`_copy_db` 连 `-wal`/`-shm` 一起拷 + 对副本 `wal_checkpoint(TRUNCATE)`，只拷主库会丢未 checkpoint 的行）。
迁移走 **app 真实代码路径**而非复制 SQL（`TradeStore(db_path)` / `PlaybookDB(conn=…)` /
`CapitalSafetySystem(db_path,trade_env)` / `ensure_lessons_lifecycle_columns(conn)`）⇒ 不会与生产漂移。

实测（`--to live`，15:05）：
```
[1] 迁移前缺口: 缺表 7 · 缺列 33
      capital_state: ['account_id']
      lessons: ['recurrence_count','last_triggered_at','cost_since']
      playbook_instructions: 27 列 (含 execution_state/execution_attempt/broker_order_id/broker_order_status)
      trade_records: ['entry_vix','entry_session']
[2] 四条迁移路径全 ✅
[3] 迁移后残留缺列: 1 (已自愈 32) | 阻塞性 0
[4] capital_state 双臂回切实证 → ⛔ seamless_roundtrip=False
→ DDL 面: 干净 (阻塞性残留 0 ⇒ 切换日零未知 DDL)
→ 回切面: ⛔ 会丢本臂基线 (含 is_locked 风险锁定被静默解除)
```
**33 条这个数字被两个独立工具交叉验证**（preflight P2 与 rehearse [1] 同源同值）。

### 1.3 `tests/test_cutover_infra.py` — 20 例，**变异测试 4 刀全杀**
钉五件事：①只读性（AST 级只扫真正传给 `execute*()` 的字面量 SQL，不用全文 grep ——
探针的 `why/fix` 散文里合法出现 "ALTER"/"CREATE TABLE" 在解释病灶，全文匹配会把说明文字
误判成写操作 ⇒ 钉子恒红失去价值）②P5 探针双向灵敏度（种得中就报、种不中就不报）
③彩排**方向接线**（回归钉，见 §四自查）④良性白名单防腐（每项必须有当下仍成立的源码锚点）
⑤回切缺陷**当前必判红**（修好后钉子会反转，强制把修复显式记录）。

| 刀 | 变异 | 结果 |
|---|---|---|
| 1 | `before = _diff(other,tgt)` → `_diff(tgt,other)` | **初次存活** ⇒ 收紧锚点后杀（见 §四） |
| 1b | `after = _diff(other,tgt)` 同向反转 | 杀 |
| 2 | preflight 内塞 `UPDATE open_positions SET size=0` | 杀（AST 只读钉） |
| 3 | P5 判定改 `if True:` 恒报污染 | 杀（假阳性钉） |
| 4 | 白名单摘掉 `close_retry_state` | 杀（防腐钉） |

---

## 二、四项 `app/` 修复提案（**本批未实施**，需评审）

### D-A（P0·回切致命）`capital_state` 按臂读取
**病灶**：`app/risk/capital_safety.py:755` 是
`SELECT * FROM capital_state ORDER BY id DESC LIMIT 1` —— **不按 `trade_env` 过滤**；
而 `:741` 建了 `idx_capital_state_trade_env` 唯一索引，**两臂基线本来各自留存在表里**。
读到异臂行后 `:757-766` 判"环境切换"→ `return` 丢弃全部状态 → 首次券商同步按
`:193-204` 把 `initial_capital` 重置为当时净值。

**实证**（彩排 [4]，沙箱内种两臂行后调**真实** `_load_state`）：
```
沙箱行: {'id': 2, 'trade_env': 0, 'peak_equity': 500000.0, 'is_locked': 0}
沙箱行: {'id': 1, 'trade_env': 1, 'peak_equity': 99127.14, 'is_locked': 1, 'lock_reason': 'rehearse_sim_lock'}
_load_state 实捞 (trade_env=1): {'initial_capital': 100000.0, 'peak_equity': 0.0, 'is_locked': False}
```
⇒ `live→sim` 回切时 sim 臂自己那行明明在表里，却因 live 行 id 更大被整份丢弃：
**`peak_equity` 归零、`max_drawdown_pct` 归零、`is_locked=True → False`**。
最后一条是 **fail-open**：一个已经生效的风险锁定，在回切时被**静默解除**。
这也正是用户要的"实盘再切换为 sim 无缝"里最硬的一块。

**提案**（`_load_state` 改按臂取行，保留现有跨臂重置语义作兜底）：
```python
c.execute("SELECT * FROM capital_state WHERE trade_env=? ORDER BY id DESC LIMIT 1",
          (self.trade_env,))
row = c.fetchone()
if row is None:            # 本臂无行 → 才回落到旧的全表最新行 + 环境切换重置语义
    c.execute("SELECT * FROM capital_state ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
```
**现状零行为变化论证**：实测 sim 库 `capital_state` 只有 1 行且 `trade_env=1`
（preflight P3 `envs=[1]`），live 库 0 行 ⇒ 加 `WHERE trade_env=?` 后取到的仍是同一行；
`_save_state` 用 `INSERT OR REPLACE` + per-env 唯一索引，写入侧不变。
**风险**：属风险限制器状态加载，需重启（G-02）。**钉子已就位**：
`tests/test_cutover_infra.py::test_capital_roundtrip_probe_flags_lost_baseline`
当前断言 `seamless_roundtrip is False`；修复落地后该钉会红，钉子里已写明
"请把本钉反转为 assert True，并在 DEB/handoff 记录该修复" ⇒ **修复无法悄悄发生**。

### D-B（P1·切换即引爆）`sl_broker_order_id` 语义单一化
一列三义、按车道分裂：写侧 `app/storage/trade_store.py:974` 把**入场单号**写进名为 `sl_` 的列
（docstring 自认"语义匹配: 入场单的 broker order id"）；`app/playbook/pipeline.py:6667`
也按入场单号读（用作归属身份令牌，**这路是对的**）；而看板读侧
`app/web_dashboard/order/oco_handler.py:86` / `executor.py:1155` 按**止损单号**读。
当前不可达（污染行在 sim 库，SL 语义读侧经 `get_dashboard_db()` 只开 live 库）；
单库翻转后同表同列两种语义 ⇒ OCO 去撤"已成交的入场单"（真 SL 单在券商端成孤儿挂单，
DEB-20260903 #123 只把它从"静默清库"改成"响亮报错+保留 id"，**孤儿本身没消除**），
且止损替换被 `executor.py:1171` 的 P1-9 保险丝**永久中止**（旧单状态是 `FILLED_ALL`，
不含 `CANCEL` ⇒ 中止下新 STOP）⇒ 真实危害是**止损永远挪不动**。
**提案**：新增 `entry_broker_order_id` 列承载入场单号，`sl_broker_order_id` 回归单一 SL 语义，
写侧双写一个过渡期 + 写后确认；读侧 `pipeline.py:6667` 改读新列。
需动订单管道 + 加列迁移 + 重启。**注意**：`open_positions` 已有 `owner`/`account_type`
可作臂判别器（live 8 行全 `owner='manual'`、sim 全 `owner='auto'`），故
RFC-080 S2' 注释里"source_type 列区分"对 `trade_records` 成立、对 `open_positions` **不成立**
（该表无此列），修 D-B 时不要顺手假设它存在。

### D-C（P1·政策，需用户裁）FLAT_ALL 触发源分权（= 提案 S5）
翻转后同一 REAL 账户 ⇒ 账户级净值含手动仓（且 live 库 `live_position_snapshots` 里还有
**港股真实持仓**：腾讯/智谱/MINIMAX-W，HKD 计价，浮亏 -2.55%/-17.98%/-20.68%）。
手动仓暴亏 → 账户级回撤触发 FLAT_ALL → 执行侧只平 `managed_positions` ⇒
**平掉无辜的 auto 仓**。推荐 B：`managed` 回撤超限 → FLAT_ALL 照旧；
仅账户级超限而 managed 未超 → 动作降为 `HALT_NEW`。落地方式沿用 S5 既定纪律：
配置键 `flat_all_account_trigger_action: halt_new`，**默认保持现行为、翻转日生效**。

### D-D（P2）`open_positions` PK=symbol 的臂共存
两库 `open_positions` 均为 `symbol TEXT PRIMARY KEY`，**都没有 `source_type` 列**；
列集相同（各 41 列）但**列序不同**（`sl/tp_broker_order_id` sim 在第 30/31、live 在第 38/39）。
单库翻转后同标的两臂物理上不能共存一行，UPSERT `ON CONFLICT(symbol) DO UPDATE` 会互相覆盖
`direction/size/SL/owner`。S3b 反向守卫只挡 `executor.submit_order` 路径，
**其自身已注明 APP 直接下单不经此路径**。当前两库同标的重叠 **0**（preflight P4），
所以是"未来时"而非"现在时"。另：列序差异当前无致害点（全仓仅 2 处
`SELECT * FROM open_positions`，生产那处 `pipeline.py:6659` 用 `cursor.description` 建 dict），
但属留给未来下标访问的地雷。

---

## 三、切换日 runbook（在决策包 §一 A' 之上补齐回切方向）

A' 六步（证据快照归档 / 门禁改指 live + 试点期语义 / **降级语义改 HALT** / 三开关同翻 /
研究脚本各自诚实停臂 / W3 零告警）**目前一步都未实施**，且**只覆盖 sim→live**。补齐后：

**T-7d 起（预置，全部现状零行为变化）**
1. `scripts/cutover_preflight.py` 进 cron（建议每日 06:20，紧随 `data_freshness_watchdog`），
   `exit=1` 即推送 ⇒ 阻塞项回退能被当天发现，而不是切换日才发现。
2. D-A 落地（按臂读取，附 `test_capital_roundtrip_probe_flags_lost_baseline` 反转）。
3. D-B 落地（`entry_broker_order_id` + 过渡期双写 + 写后确认）。
4. D-C 配置键预置（默认现行为）。
5. `live_ledger_sync` 加 broker 真值校正 `size` 的一步，清掉 live 8 行僵尸（决策包 §三）。
6. broker STOP 三段演练（挂/改/撤，极小仓位、人工经看板），把 P6 里程从 0 变 ≥1。

**T-1d（彩排）**
7. `scripts/cutover_rehearse.py --to live --keep` ⇒ 要求 `DDL 面: 干净` 且 `回切面: 无缝`；
   把副本上跑出来的 schema 作为**预置迁移**正式施加到 live 库，使切换日 DDL = 0。
8. `scripts/cutover_rehearse.py --to sim` ⇒ **回切方向同样彩排**（本批新增的能力）。

**T0（切换日，需周检达标报告 + 用户签字）**
9. 证据快照归档 → 门禁改指 live + 试点期语义 → 降级语义改 HALT →
   三开关同翻（`TRADE_ENV=0` + `AUTO_TRADE_ACCOUNT=real` + `ALLOW_REAL_TRADING=true`）→ 重启。
10. `playbook.live_trial` 仓位 ×0.5 起步（`playbook_service.py:1256` 已有）+ SIM 影子对照组并行。
11. 验证：preflight `exit=0`；W3 双向 0；`live STOP 单已挂` 出现 ≥1 次且 `pb_stop_` 留痕入库。

**回切（live→sim，用户明确要求的双向能力）**
12. 三开关同翻回（`TRADE_ENV=1` + `AUTO_TRADE_ACCOUNT=sim` + `ALLOW_REAL_TRADING=false`）+ 重启。
13. **回切后必须验 D-A**：`capital_state` 的 sim 臂行被正确恢复（`peak_equity`/`max_drawdown_pct`/
    `is_locked` 与切出前一致）—— 这正是 D-A 未修时会静默 fail-open 的地方。
14. 台账不搬行：sim 历史永远在 sim 库、live 历史永远在 live 库（用户 2026-09-09 裁决
    "模拟交易和实盘交易不要混为一谈，包括历史数据"；§五 demix 事件已付过代价，
    TTE 一度被删错方向）。回切**不做任何跨库复制**。

---

## 四、自查（本批自己犯的错，记下来防再犯）

1. **彩排方向接线写反**：`--to live` 的目标库被我复制成了 **sim** 库 ⇒ 缺口恒算成
   `缺列 0`、工具**假绿**。是靠"33 条"与 preflight P2 对不上才发现的
   （两个独立工具同源同值才叫交叉验证）。修后补了 `test_rehearse_direction_wiring_anchor`。
2. **锚点钉太松，被变异测试当场抓出**：第一版锚点只查裸子串 `_diff(other, tgt)`，
   而 `main()` 里 `before`/`after` 各有一次 ⇒ 反转 `before` 那处时 `after` 顶包命中，
   **刀1 存活**。收紧为带赋值前缀的唯一串后，`before`/`after` 任一反转都杀。
   教训：源码锚点钉必须钉到**唯一串**，否则"看起来钉住了"比没钉更危险。
3. **测试夹具的静默空表**：`_mk_db` 只在传 `entry_oid` 时才插 `trade_records` 行，
   混同用例没传 ⇒ 表是空的，探针报"0 行纯净"是**真空而非漏报**，测试因此假红。
   已在夹具注释里写明这个前提，并补了反向混同（sim 库现 `trade_mode='live'`）用例。

---

## 五、刻意未动

**本批** `app/` **零改动**（D-A~D-D 全部只是提案，一行未落；注意仓库 `app/` 另有他人在途改动，见 §六）；
`config/unified_config.yaml` 零改动；
`.env*` / 账户 / 模式 / LIVE / 持仓 / 风险预算 / 杠杆 零改动；
`data/*_baseline.json` 任何判据零改动；`SYSTEM_PROMPT_V2` / `PROMPT_HASH_V2` /
`PROMPT_VERSION` / `BRIEF_TEMPLATE_VERSION` / `SCORE_VERSION` 五项模型输入指纹零改动；
P4 漏斗 / 退出参数 / tier / confirm / grounding / schema_enforce 全部开关零改动。
生产库全程只读（彩排前后 10 项台账指纹逐一相同，两库 `size`/`mtime` 未变，沙箱已清）。
**未提交**（仓内提交需 `[用户裁"…"]` 标签）。

---

## 六、⚠️ 环境事实：`app/` 有他人在途改动（非本批，本批未碰）

写本文件时（15:20）实测 `git status --porcelain app/` **非空**，7 个文件在
**14:57–15:17** 之间被改（本批 14:04 起只写 `scripts/` + `tests/` + `docs/`，
14:28 那次核验时 `app/` 还是干净的）：

| 文件 | mtime | 看着像 |
|---|---|---|
| `app/data/vix_source.py`（未跟踪·新增） | 14:57:36 | VIX 数据源重构 |
| `app/data/market_data_router.py` | 15:00:06 | 同上 |
| `app/data/alphavantage_provider.py` | 15:00:29 | 同上 |
| `app/risk/vix_monitor.py` | 15:01:51 | 同上 |
| `app/pipeline/order_executor.py` | 15:02:59 | — |
| `app/playbook/playbook_service.py` | 15:04:43 | — |
| `app/analysis/market_state.py` | 15:17:36 | — |

`git diff --stat app/` = 6 files, 73 insertions(+), 13 deletions(-)。
反向复核（正确姿势）：`git diff --name-only app/` 去掉上述 6 个文件后**为空** ⇒ 本批未碰 `app/`。

**运维后果（需知晓）**：
- 运行中的服务仍是 **14:08:20** 启动的那份代码（`MainPID=1330923`、`NRestarts=0`）；
  `HotReloader` 只盯配置文件（启动日志 `[HotReloader] 启动 (cooldown=60s, files=1)`）
  **不盯 `app/*.py`** ⇒ 上述改动**尚未生效**。
- 部署守卫已发现但延迟处理：`scripts/deploy_freshness_check.py` 输出
  `[20260914_152030] skip: stale_drift=4156s_outside_safe_window | newest=app/analysis/market_state.py`
  （4156s = 15:17:36 − 服务启动 14:08:20），`exit=0`。
  ⚠️ 这条 `exit=0` **不代表"无漂移"**，而是"有漂移但在安全窗外，跳过"。
- **本批不为此重启**：他人在途可能是半成品，重启会把未完成代码推上生产；
  也不碰这 7 个文件（含不做 `git checkout` / `git stash`）。

**本批自己的一个假绿 bug（记下来防再犯）**：核验 `app/` 是否干净时用了
`git status --porcelain app/ && echo "(app/ 空=零改动 ✅)"` —— `git status --porcelain`
**无论有无输出都 exit 0**，所以那句 ✅ 在 `app/` 明明有 7 个改动文件时照样打印。
判"空"必须用 `test -z "$(git status --porcelain app/)"`，不能靠 `&&` 链。
本文件的"零改动"结论已改用逐文件 mtime + `git diff --stat` + 反向 name-only 三重复核。

---

## 七、双向彩排终局读数（15:28 实测，补 §一.2）

两个方向都跑过，`--to sim` 是决策包**从未覆盖**的回切方向：

| 方向 | 迁移前缺口 | 迁移后阻塞性残留 | DDL 面 | 回切面（capital_state） |
|---|---|---|---|---|
| `--to live` | 缺表 7 · **缺列 33** | **0**（自愈 32，余 1 = 死列 `account_id`） | ✅ 干净 | ⛔ 丢本臂基线 |
| `--to sim` | 缺表 11 · 缺列 **0** | **0** | ✅ 干净 | ⛔ 丢本臂基线 |

⇒ **schema/DDL 双向都不是阻塞**（幂等自愈迁移覆盖完整）。**唯一挡住"双向无缝"的是 D-A**
（`capital_state` 不按臂读取）。这是个好消息：待办从"一堆 schema 工程"收敛成**一处 6 行改动**。

### 7.1 `--to sim` 的 11 张"缺表"逐类实证良性（已全部进白名单 + 防腐钉）

| 类 | 表 | 证据锚点 |
|---|---|---|
| (a) **被主动 DROP 的退役死表** | `daemon_heartbeat` | `trade_store.py:350` `DROP TABLE IF EXISTS` |
| | `entry_context_snapshot` | `trade_store.py:365`「死表退役 (2026-09-13, 用户裁"删除")」 |
| | `option_positions` | `trade_store.py:251`「2026-07-01 表已删除 (自动交易不做期权)」（**仅注释，无主动 DROP** ⇒ 实测彩排后仍存在） |
| (b) `CREATE IF NOT EXISTS` 自建 | `live_deal_records` / `live_position_snapshots` / `live_round_trips` | `app/web_dashboard/live_ledger.py` |
| | `thesis_challenges` | `app/llm/thesis_challenge.py` |
| (c) 全仓 `app/`+`scripts/` **零引用**孤儿表 | `option_peak_profits` / `performance_stats` / `shadow_daily_stats` / `shadow_schema_version` | rg 实测 0 命中 |

### 7.2 ⚠️ 新发现：切 live 当天会在**真钱库**上执行 2 条 `DROP TABLE`

`TradeStore._init_db` 的退役逻辑是无条件的，所以 `--to live` 彩排实测（沙箱副本，`--keep` 后查）：
```
彩排后 live 副本 daemon_heartbeat:      已被 DROP
彩排后 live 副本 entry_context_snapshot: 已被 DROP
彩排后 live 副本 option_positions:       仍存在 (只有注释, 无主动 DROP)
```
两张都是**已退役死表**（一张 2026-09-13 由用户裁删除），删掉是有意的、不丢有用数据。
但这正是彩排工具的价值所在：**"切换日会在真钱库上删表"这件事必须在切换前就知道**，
而不是切换当天在日志里发现。建议 T-1d 彩排（§三 第 7 步）后，把这 2 条 DROP 写进
切换日 runbook 的"预期 DDL 清单"，并在 T-7d 先对 live 库做一次带留痕的备份
（`scripts/db_backup.sh` 每日 03:00 已在跑，切换日建议手动加一次即时备份）。

### 7.3 钉子补强（变异测试总计 **6 刀全杀**）

| 刀 | 变异 | 结果 |
|---|---|---|
| 5 | 白名单摘掉 `daemon_heartbeat` | 杀（`test_benign_tables_retired_anchor`） |
| 6 | 在 `scripts/` 造一个 `performance_stats` 引用方 | 杀（`test_benign_tables_selfcreate_and_orphan_anchors`） |

刀6 的钉法值得记：孤儿表的"零引用"是**会腐化的事实**（今天没人读，明天有人加一行 SELECT），
所以钉子不是断言一个常量，而是**每次跑都重扫 `app/`+`scripts/` 全量 `.py` 语料**；
语料为空时先 `assert corpus` 兜底，防"扫描路径写错 ⇒ 零引用恒成立 ⇒ 钉子假绿"。

---

## 八、方案(a) 已执行：预检挂成每日哨兵（15:45–16:03）+ D-A 暂缓的理由

用户裁「检查确认无误，请选择最合理的方案建议执行」⇒ 两个候选里选了 **(a)** 并已执行完毕。
本节是执行记录 + **为什么不是 (b)**，供后续评审复核（含推翻）。

### 8.1 两个候选与选择理由

| | 方案 | 内容 | 判 |
|---|---|---|---|
| (a) | **把预检挂成每日哨兵** | 给 `cutover_preflight.py` 加棘轮基线 + cron，把"切换就绪度"从**一次性人工体检**变成**每日自动看守** | ✅ **已执行** |
| (b) | 现在就落 D-A 补丁 | 改 `app/risk/capital_safety.py:755` 为按臂读取 | ⏸ **暂缓**（理由见 §8.4） |

选 (a) 弃 (b) 的三条理由（**风险不对称**是核心）：

1. **改 `app/` 必须重启（G-02），而 `app/` 现在不是我的**。§六 记录的他人在途改动到 16:03 已从 7 个
   扩到 **9 个已跟踪 + 1 个新增**：`app/analysis/market_state.py`(15:17:36)、
   `app/data/alphavantage_provider.py`(15:00:29)、`app/data/market_data_router.py`(15:00:06)、
   `app/pipeline/order_executor.py`(15:02:59)、`app/plan/analysts/sentiment_analyst.py`(**15:51:46**)、
   `app/playbook/data_collector.py`(15:44:10)、`app/playbook/pipeline.py`(15:40:59)、
   `app/playbook/playbook_service.py`(15:04:43)、`app/risk/vix_monitor.py`(15:01:51)、
   `app/data/vix_source.py`(未跟踪新增 14:57:36)；配套还有 `tests/test_vix_single_source_contract.py`、
   `tests/playbook/test_data_collector_vix_source.py` 等未跟踪新测试 ⇒ 是一个**在写的 VIX 单一源重构**。
   为落 D-A 而重启 = **把别人的半成品推上生产**。
2. **D-A 当前不可达 ⇒ 没有紧迫性**。P3 实测 live 库 `capital_state` **0 行**、sim 库 **1 行且
   `trade_env=1`**，而 `capital_safety.py:741` 的 per-env 唯一索引保证每库只会有本臂的行
   ⇒ 今天给 `:755` 加上 `WHERE trade_env=?` **取到的是同一行**，行为零变化（§二 D-A 已论证）。
   它是"**切换日才引爆**"的雷，不是"现在正在流血"的伤口。
3. **切换本身被毕业门禁硬卡**（§零：`ic_t3=-0.0155` IC 为负、连亏 5 笔、G1-1 净期望 **-$15.62/笔**）。
   ⇒ 第一优先级是把净期望转正。在此期间最有价值的基建是**每天自动告诉你"离能切还差什么、
   有没有悄悄变差"**，而不是提前把切换日的补丁打上、还要为它付一次重启代价。

**一句话**：(b) 的收益要等到切换日才兑现，代价（重启 + 踩他人在途）今天就要付；
(a) 的收益今天就开始兑现（哨兵上岗），代价接近零（`scripts/` 自留地 + 一行 cron）。

### 8.2 已落地（`scripts/` + `tests/` + `.gitignore` + cron，`app/` 仍**零改动**）

- **`scripts/cutover_preflight.py` 新增三开关**（棘轮语义照抄 `scripts/data_freshness_watchdog.py`
  的既有范式，不另发明）：
  - `--accept-baseline`：**人工**把当前 10 探针状态冻结为棘轮基线，写
    `data/cutover/preflight_baseline.json`（含 `frozen_at` / `rule` / `keys` / `readings`）。
  - `--watch`：只对**状态跃迁**告警，分四类 `new` / `escalated` / `improved` / `gone`
    （`diff_against_baseline`，`:178`）。
  - `--push`：跃迁时推飞书，**30min 同告警去重**（`push_alert` + `_hash_msg`，同 watchdog 的 cooldown）。
- **退出码契约**（cron 兜底依赖它，实测）：`--accept-baseline`→0；`--watch` 无基线→**2**（fail-loud）；
  `--watch` 无跃迁→**0**（静默）；`--watch` 有跃迁→**1**（已推飞书）；常规模式→`1 if blocked else 0`。
- **基线已冻结**（15:52:08）：`{P0:ok, P1:block, P2:warn, P3:warn, P4:warn, P5:warn, P6:warn,
  P7:ok, P8:warn, P9:warn}`。
- **cron 已装（第 276 行，末行）**：`38 6 * * *` `--watch --push`，`rc>=2` 走**钉钉**兜底
  （区别于 rc=1 的飞书：rc≥2 意味着**哨兵自己坏了/基线丢了**，此时飞书那条路也不可信，
  必须走第二条通道）。选 06:38 是**避开 06:35 的两个一次性作业**
  （`da_glm_shadow_eval.py` = `35 6 15 9 *`、`da_prompt_distill_shadow_eval.py` = `35 6 16 9 *`）。
  安装后逐字节复核：**前 275 行与备份 `/tmp/crontab_backup_20260914_1555.txt` `diff` 为空**，
  行数 275→276（只加一行，未改任何既有作业）。
- **`.gitignore` 白名单**放行 `data/cutover/preflight_baseline.json`：棘轮基线是**判据种子**
  （记录"何时把什么冻结为已知常态"），与既有 `!data/config_freeze_v2.json` 同性质 ⇒ 须入库供评审；
  同目录逐次快照 `preflight_*.json` 仍出库（cron 每日重生成，入库会把树搞脏）。已用 `git add -n` 验证生效。

### 8.3 三个刻意的设计决定（**改之前请先读这里**）

1. **`_SEV = {"ok":0, "na":1, "warn":2, "block":3}` —— `na > ok` 是故意的，不是笔误。**
   若把 `na` 排在 `ok` 之下，则给 cron 悄悄加一个 `--skip-gate`/让某探针变成 `na`
   会被 `diff_against_baseline` 判成 `improved`（✅ 好消息）而不是 `escalated`
   ⇒ **把告警无声关掉还能收到"改善了"的喜报**。钉子 `test_diff_severity_ordering_is_not_alphabetical`。
2. **`--watch` 只比 `status` 不比 `reading`**：P1 的 `ic_t3`、P8/P9 的行数**每天都会漂**，
   比 reading 会变成天天告警 ⇒ 告警疲劳 ⇒ 真跃迁被淹没。钉子 `test_statuses_compares_status_not_reading`。
   代价说清：**同一 status 内的读数恶化不会告警**（例如 `ic_t3` 从 -0.0155 掉到 -0.30 仍是 `block`）。
   这是刻意取舍——趋势面已由 06:25 的 `data_freshness_watchdog` 与各 scorecard 看守，
   本哨兵只负责**切换就绪度的结构性跃迁**。
3. **基线脚本自身永不自动写**（只有 `--accept-baseline` 这个显式人工动作会写）。
   若让 `--watch` 每次自动滚动基线，则"跃迁"永远为零 ⇒ **哨兵恒静默 = 假绿**。
   配套 fail-loud：**无基线时 `--watch` 返回 2 并推送**，把"没在哨"和"哨了没事"区分开。
   钉子 `test_accept_baseline_roundtrip_and_watch_never_autowrites` + `test_watch_fails_loud_without_baseline`。

**为什么 `--watch` 必须是棘轮而不是"有 BLOCK 就报"**：毕业门禁（P1）在 IC 转正之前会**长期** block，
天天推同一条"你还没毕业"= 噪声。**基线内的状态静默、跃迁才告警**，才能让它跑一年还有人看。

### 8.4 D-A 暂缓：不是不修，是**排在他人 VIX 重构落定之后的同一批**

- **必须修**（结论不变，§二 D-A / §七）：`capital_safety.py:755` 的 `ORDER BY id DESC LIMIT 1`
  不带 `trade_env` 过滤 ⇒ `live→sim` 回切时丢本臂基线、**`is_locked=True→False` 风险锁定被静默解除
  （fail-open）**。这是"实盘再切回 sim 无缝"里最硬的一块，也是双向彩排后**唯一**的阻塞项。
- **触发条件（满足任一条即应立刻落 D-A，不要再等）**：
  ① 他人的 `app/` 在途改动落定（提交或撤回）⇒ 重启代价消失；
  ② 毕业门禁 P1 由 `block` 转 `ok`（IC 转正 + 无连亏 5 笔）⇒ 切换进入实际日程；
  ③ 任何人准备执行 `--to live`（哪怕只是彩排转正式）。
- **落地时的一并事项**：改 `WHERE trade_env=?` + 本臂无行才回落旧语义；
  **反转钉子** `test_capital_roundtrip_probe_flags_lost_baseline`（当前断言 `is False`，
  修复落地后会自动变红，钉内已写明须改为 `True` 并记录）⇒ **修复无法悄悄发生**；
  顺带把 P3 的"live 侧基线行为空"预置进切换日 runbook（`capital_safety.py:193-204` 会把
  `initial_capital` 重置为当时净值 ⇒ 不预置就是回撤保护开局失明）。
- **哨兵已代它站岗**：在 D-A 落地前，若有人把 live 库 `capital_state` 从 0 行变成有行、
  或 P3 状态跃迁，`--watch` 会在次日 06:38 报出来 ⇒ 暂缓期**不是无人看守期**。

### 8.5 钉子与变异测试（20 例 → **28 例**；变异 6 刀 → **8 刀全杀**）

新增 6 例全部钉 §8.2/§8.3 的新语义：`test_statuses_compares_status_not_reading`、
`test_diff_classifies_four_transition_kinds`、`test_diff_severity_ordering_is_not_alphabetical`、
`test_accept_baseline_roundtrip_and_watch_never_autowrites`、`test_watch_fails_loud_without_baseline`、
`test_watch_silent_when_unchanged`。新增 2 刀：

| 刀 | 变异 | 结果 |
|---|---|---|
| 7 | `_SEV` 里把 `na` 调到 `ok` 之下（0/1 互换） | 杀（`test_diff_severity_ordering_is_not_alphabetical`） |
| 8 | 让 `--watch` 在无跃迁时也返回 0 但**跳过比对**（装成通过） | 杀（`test_watch_silent_when_unchanged` / `test_watch_fails_loud_without_baseline`） |

刀 8 的钉法值得记：**"静默"和"没在哨"输出长得一样**，所以钉子不是断言 exit=0，
而是**临时删掉基线文件**再跑 ⇒ 必须 exit=2；以及**篡改一个探针状态**再跑 ⇒ 必须 exit=1 且报出该探针。
只断言 exit=0 的钉子会被"永远返回 0"这种最蠢的变异白拿。

`pytest tests/test_cutover_infra.py -q` = **28 passed**（16:00 复核）。

### 8.6 终局核验读数（16:03）

- **哨兵 dry-run 验收**：`--watch`（刻意**不加** `--push`，避免验收动作本身发出告警）
  ⇒ `block=1 warn=7 ok=2 na=0 | worst=block`，`[watch] 与基线一致, 静默`，**exit=0**，**耗时 6s**
  ⇒ 与 06:41 的 `fee_unit_economics_scorecard` 无重叠风险（6s ≪ 3min 间隔）。
- **生产库零接触（第三次复核）**：`/tmp/db_ledger_baseline.py` 15:01:37（彩排前）vs 15:59:31（现在）
  ⇒ 两库 `open_positions` 行数/`sha256`/`max_updated_at`、`trade_records` 计数/`max_trade_date`、
  `symbols` 列表**逐项相同**，仅 `label`/`taken_at` 两个元数据字段不同。
  ⚠️ 期间 15:41:27 / 15:54:44 有**真钱成交落账**（`HK.00100` BUY 20@251.6、BUY 40@251.0），
  已核实其写入面是 `live_deal_records`（`app/web_dashboard/live_deal_push.py:128` 复用
  `live_ledger.record_deal_push`），**不写** `open_positions`/`trade_records`
  ⇒ 指纹不变是**正确的不变**，不是探针瞎了。
- **服务**：`active`，`MainPID=1330923` / `NRestarts=0` / `ActiveEnterTimestamp=14:08:20`
  ⇒ **本批未触发任何重启**，生产仍跑 14:08:20 的代码。
- **日志**：`logs/ftseek_us.log` 全档 `ERROR`/`CRITICAL`/`Traceback` 命中 **0**。
- **部署守卫**：`deploy_freshness_check.py` → `skip: stale_drift=6206s_outside_safe_window`，
  `newest=app/plan/analysts/sentiment_analyst.py`。两条含义都要说清：
  ① 漂移源是**他人 15:51 的在途改动**，不是本批（本批只写 `scripts/`+`tests/`+`docs/`）；
  ② `SCAN_DIRS=("app",)` + `SCAN_FILES=("main.py",)`（`:43-44`）⇒ **不扫 `scripts/`**，
  所以本批为变异测试做的 `scripts/` mtime 复位对守卫**零影响**；
  ③ 再强调一次：这个 `exit=0` 是"**有漂移但在安全窗外，跳过**"，**不等于"无漂移"**。
- **`git status` 复核**：`app/` 的 9+1 个改动**全属他人**（时间戳 14:57–15:51，本批 14:04 起只写
  `scripts/`+`tests/`+`docs/`+`.gitignore`）；`app/risk/capital_safety.py` `git diff --stat` **为空**
  ⇒ D-A 确实一行未落。

### 8.7 已知非问题（记下来免得后人当 bug 查）

`data/cutover/` 的逐次快照 `preflight_*.json` **无保留期清理**（每日 +1 个，约 12KB）。
刻意不做清理：与仓内既有形态一致（`data/replay_bars/` 1913 个、`data/position_paths/` 381 个、
`data/signal_quality/` 336 个文件，均无 prune），且 `data/*` 已 gitignore、
`preflight_latest.json` 是现成指针 ⇒ 一年约 4.4MB / 365 个文件，属可接受的审计留痕。
真要收敛应作为**全仓 `data/` 保留期策略**统一做，不该只给这一个目录开特例。

### 8.8 回滚（三处，互相独立）

1. **cron**：删 crontab 第 276 行（该行尾已内联注明「回滚=删本行」）。
2. **基线入库**：删 `.gitignore` 中新增的三行（`!data/cutover/` / `data/cutover/*` /
   `!data/cutover/preflight_baseline.json`）+ 其上方 5 行注释（该块头已注明「回滚 = 删以下三行」）。
3. **脚本/钉子**：`scripts/cutover_preflight.py`、`scripts/cutover_rehearse.py`、
   `tests/test_cutover_infra.py` 均为**未跟踪新增文件**，删除即完全回滚，不留残余。

**三处都不触碰 `app/`、`config/`、`.env*`、生产库、crontab 既有 275 行** ⇒ 回滚零风险。

---

## 九、D-A 待审 patch（**已写好、已实证、未落地**）

> **状态**：补丁文件 `docs/debates/patches/D-A-capital-state-per-arm.patch`（185 行，
> `app/risk/capital_safety.py` **+19/−2** + `tests/test_cutover_infra.py` **+141/−9**）。
> **`app/` 工作树零改动**（`git diff --stat app/risk/capital_safety.py` 为空），服务**未重启**。
> **`git apply --check` 已对当前工作树通过**（16:2x 实测，dry-run 不写盘）。
> **落地时机**：见 §8.4 三条触发条件（他人 VIX 重构落定 / P1 转 ok / 有人要执行 `--to live`）。

### 9.1 病灶一句话

`capital_state` 表**每臂各留一行**（`:741` 的 per-env 唯一索引就是这么设计的），
但 `_load_state`（`:755`）**只取 `id` 最大的那一行、不看是哪一臂**。
⇒ 谁的行更晚写入，谁就"顶掉"另一臂。被顶掉的那臂会落进 `:762` 的
"检测到交易环境切换 ⇒ 自动重置资金基准"分支，`return` 掉，**整份基线不加载**：

| 字段 | 应有值（本臂） | 实际值（被顶掉后） | 后果 |
|---|---|---|---|
| `is_locked` | `True` | **`False`** | **风险锁定被静默解除（fail-open）** |
| `lock_reason` | `回撤超限` 等 | `""` | 锁因丢失，事后无法追溯 |
| `peak_equity` | 历史峰值 | **`0.0`** → 由 broker 重新初始化 | **回撤记忆归零**，从 0% 重新算 |
| `initial_capital` | 本臂真值 | 配置默认（100000） | 回撤百分比分母错 |

`live→sim` 回切**必然踩中**：sim 臂先有基线（`id` 小），切到 live 后 live 臂写入新行（`id` 大），
回切时 `_load_state(trade_env=1)` 捞到的是 live 那行 ⇒ sim 的锁定与回撤历史**双双蒸发**。

### 9.2 修法（`app/` 部分全文，19 行里 12 行是注释）

```diff
@@ -752,8 +752,25 @@
             conn = sqlite3.connect(self._db_path)
             conn.row_factory = sqlite3.Row
             c = conn.cursor()
-            c.execute("SELECT * FROM capital_state ORDER BY id DESC LIMIT 1")
-            row = c.fetchone()
+            # D-A (DEB-20260914 §8.4/§九): capital_state **按臂读取**。
+            #   病灶: 原 SELECT 不带 trade_env 过滤, 而 L741 的 per-env 唯一索引明确
+            #   允许两臂各留 1 行 ⇒ 若另一臂的行 id 更大 (更晚写入), 本臂基线会被整份
+            #   丢弃并落到下方"检测到交易环境切换"分支 ⇒ is_locked True→False
+            #   (风险锁定被静默解除, fail-open) + peak_equity/回撤记忆归零。
+            #   live→sim 回切必然踩中: sim 臂先有基线 (id 小), live 臂后写 (id 大)。
+            #   本臂无行才回落旧语义, 保留 L760 的切换告警留痕 ⇒ 现状零行为变化。
+            try:
+                c.execute("SELECT * FROM capital_state WHERE trade_env = ? "
+                          "ORDER BY id DESC LIMIT 1", (self.trade_env,))
+                row = c.fetchone()
+            except sqlite3.OperationalError:
+                # trade_env 列缺失 (未跑过 L723 迁移的旧库): 回落旧语义 —— 原实现靠
+                # L761 的 `"trade_env" in row.keys()` 容忍缺列, 此处保持同一容忍度,
+                # 不让"缺列"升级成 fail-safe 锁仓 (那会改变原行为)。
+                row = None
+            if row is None:
+                c.execute("SELECT * FROM capital_state ORDER BY id DESC LIMIT 1")
+                row = c.fetchone()
             conn.close()
```

**设计要点三条**：
1. **先按臂查，查不到才回落旧语义**。回落分支保留了 `:760` 那条
   "检测到交易环境切换"的 `logger.warning` 留痕 ⇒ 可观测性不退化。
2. **`_save_state` 一行未改**。写侧本来就是 `INSERT OR REPLACE` + per-env 唯一索引（`:828`/`:741`），
   已经保证"1 行/臂"，问题纯在读侧。
3. **`except sqlite3.OperationalError` 不是防御性废话，是必需的**（§9.5 刀C 有实证）：
   `_ensure_db_table` 整体裹在 `try/except`（`:746-747`）里、ALTER 也单独 `try/except: pass`（`:725-726`），
   所以"迁移没跑成 ⇒ 缺 `trade_env` 列"是**可达状态**。原实现靠 `"trade_env" in row.keys()` 容忍它；
   若新写法不容忍，缺列会冒到外层 `except`（`:806`）置 `_save_failed=True` ⇒
   **把"读不到臂"升级成"禁止一切新开仓"**，那才是真回退。

### 9.3 「现状零行为变化」的穷举证明（四种库形态，逐一对拍）

| # | 库形态 | 旧代码 | 新代码 | 同? |
|---|---|---|---|---|
| 1 | **sim 库今天**：1 行，`trade_env=1`，`self.trade_env=1` | `LIMIT 1` 取该行 → 加载 | `WHERE trade_env=1` 取**同一行** → 加载 | ✅ |
| 2 | **live 库今天**：0 行 | `row=None` → `else` 分支「无历史记录」 | `WHERE` → None → 回落 → None → **同一** `else` 分支 | ✅ |
| 3 | **缺 `trade_env` 列**（未迁移旧库） | `SELECT *` OK → `:761` `keys()` 无该键 → `saved_env=None` → 不重置 → 加载 | `WHERE` 抛 `OperationalError` → `row=None` → 回落 `SELECT *` OK → **同上路径** | ✅ |
| 4 | **两臂都有行**（切换日之后） | 取 `id` 最大者 ⇒ 若是另一臂则**丢弃本臂** | 取本臂行 ⇒ **正确恢复** | ⚡ **这就是要修的** |

⇒ **今天两个生产库分别落在形态 1 和 2，都在"✅ 同"里**。唯一会发生行为变化的形态 4，
正是当前的 bug 本身。**这是"零行为变化"可被论证（而不是被声称）的原因。**

### 9.4 已在隔离副本实证（不是纸面推演）

用 `git worktree add --detach /tmp/wt-da-20260914 HEAD` 开隔离工作树（**主工作树全程未被写**），
把未跟踪的 `scripts/cutover_*.py` + `tests/test_cutover_infra.py` 复制进去，打补丁后**真跑**：

- **`pytest tests/test_cutover_infra.py -q` = 33 passed**（原 28 + 新 5）。
- 新增 5 个行为钉，各钉一件**互不替代**的事：

| 钉子 | 钉什么 | 若没有它会怎样 |
|---|---|---|
| `test_capital_load_prefers_own_arm_over_newer_other_arm` | 本臂行优先于 `id` 更大的另一臂行 | 修复本体无人看守 |
| `test_capital_load_does_not_inherit_other_arm_lock` | **不得继承**另一臂的锁 | 可被"总挑 `is_locked=1` 那行"这种错实现骗过 |
| `test_capital_load_falls_back_when_own_arm_absent` | 本臂无行 ⇒ 回落旧语义（重置基准） | 回落分支被误删也没人发现 |
| `test_capital_load_tolerates_missing_trade_env_column` | 缺列不得升级成 fail-safe 锁仓 | 形态 3 静默变成"禁止开仓" |
| `test_capital_load_empty_table_is_not_an_error` | 0 行不报错不锁仓（live 库今天的形态） | 空表被当成错误 |

- **反转既有钉子**：`test_capital_roundtrip_probe_flags_lost_baseline` 由 `assert … is False`
  改为 `is True`，并**在 docstring 里留反转史**（建钉时刻意写明"修好后请反转并记录"，
  现在兑现）。断言同时收紧到 `peak_equity == 99127.14` / `is_locked is True` /
  `lock_reason == "rehearse_sim_lock"` 三项，而不只一个布尔。

### 9.5 三刀变异测试（全杀，且**各杀不同的钉子**）

在隔离副本里把补丁改坏，看守卫会不会睡着：

| 刀 | 变异 | 结果 | 杀掉的钉子 |
|---|---|---|---|
| A | **整个 `app/` 补丁回退**（`git checkout --`），新钉子保留 | 杀 | 3 个：反转钉 + `prefers_own_arm…` + `does_not_inherit…` |
| B | 把按臂过滤换成**错实现** `ORDER BY is_locked DESC, id DESC`（"总挑锁着的那行"） | 杀 | **只**杀 `does_not_inherit_other_arm_lock` |
| C | 把 `except sqlite3.OperationalError` 换成 `except ValueError`（永不命中 ⇒ 缺列冒到外层） | 杀 | **只**杀 `tolerates_missing_trade_env_column` |

**刀 B 是这批最有价值的一刀**：它证明 `does_not_inherit_other_arm_lock` **不可被
`prefers_own_arm…` 替代** —— 在"总挑锁着的行"这个错实现下，前者红、后者绿
（因为那个场景里错实现恰好挑对了）。少写这一条钉子，错实现就能混过去。

**刀 C 顺手取到了实证日志**（不是推理，是跑出来的）：
```
WARNING ftseek.capital_safety:capital_safety.py:830
  [资本安全] 加载状态失败 (fail-safe 将拦截新开仓): no such column: trade_env
```
⇒ §9.2 第 3 点"缺列会升级成禁止开仓"由这条日志坐实。

**一个我自己写错、被跑出来的钉子 bug（记下防再犯）**：
`tolerates_missing_trade_env_column` 初版预置 `cs._save_failed = True` 再断言 `_load_state()`
把它清成 `False` —— **失败**。原因是 `_load_state` 的**成功路径根本不碰** `_save_failed`，
只有 `_save_state`（`:823`）会清。已改为**预置 `False` 再断言仍是 `False`**，
这才等价于"缺列没有触发外层 except"。**教训**：断言"某标志没被置起"时，
必须先确认成功路径**真的会**写这个标志，否则钉的是一个不存在的语义。

### 9.6 落地程序（等触发条件满足时照抄，约 15 分钟）

```bash
cd /opt/ftseek/FTSeek-US
# 0) 前置: 确认他人 app/ 在途已落定, 且 capital_safety.py 仍与 HEAD 同源
git diff --stat app/risk/capital_safety.py            # 必须为空
# 1) 干跑校验 (不写盘)
git apply --check -v docs/debates/patches/D-A-capital-state-per-arm.patch
# 2) 落地
git apply -v docs/debates/patches/D-A-capital-state-per-arm.patch
# 3) 验证 (必须 33 passed)
PYTHONPATH=. venv/bin/python -m pytest tests/test_cutover_infra.py -q
# 4) 重启 (G-02: 改 app/ 必须重启; 重启前后按 14:25 条目那套取证)
sudo systemctl restart ftseek-us
# 5) 重启后确认按臂加载生效 (应看到"状态已加载 | peak=… (保留)", 且无"检测到交易环境切换")
grep -n '资本安全' logs/ftseek_us.log | tail -20
# 6) 重冻哨兵基线 (P3 读数/状态可能变; 人工动作)
PYTHONPATH=. venv/bin/python scripts/cutover_preflight.py --accept-baseline
```

**回滚**：`git apply -R docs/debates/patches/D-A-capital-state-per-arm.patch` + 重启。
补丁只碰 `_load_state` 的读语句与测试，**不含 DDL/迁移**，回滚零残留
（`capital_state` 表结构与数据全程未动）。

### 9.7 ⚠️ patch 会腐化，以及怎么办

`.patch` 文件是**上下文敏感**的：`:752` 附近只要有别人改过一行，`git apply` 就会失败。
这是刻意的取舍 —— **宁可响亮地失败，也不要静默地错位应用**。应对顺序：

1. `git apply --check` 失败 ⇒ 先试 `git apply -3`（三方合并，能自动吃掉多数上下文漂移）；
2. 仍失败 ⇒ **照 §9.2 的 diff 手工重放**（改动只有 1 处、19 行，其中 12 行是注释）；
3. 重放后**必须重跑 §9.5 的三刀**，尤其是刀 B —— 手工重放最容易把"按臂过滤"写成"按锁过滤"；
4. 落地后把本节状态从"待审"改为"已落地 + 提交 hash"，并反转 §9.4 提到的钉子记录。

**不要用 `patch -p1 --fuzz=3` 之类放宽匹配**：这个补丁动的是**风险锁定**的读取路径，
错位应用（例如把过滤条件套到别的查询上）比不应用更危险。

---

## 十、⚠️ 自查：本批产物曾把 4 例治理门顶红（2026-09-15 已修 3 例，1 例非本批）

**怎么被发现的（这条比修复本身重要）**：不是我自己发现的。并行工作流
（DEB-20260914 VIX 批）在 09-14 18:32 的 handoff 条目里跑**全仓回归**
（5089 passed / 3 failed），把三例失败**逐例归因到本工作流的在途产物**，并明确
「**未代改** —— 介入会与那条工作流冲突」。我昨日只跑了 `tests/test_cutover_infra.py`
（28 passed）就下了"全绿"的结论。

**教训（已立规矩）**：新增 cron 行 / 新增 `scripts/` 文件之后，**必须跑全仓治理门**，
不能只跑自己的钉子。本仓有一批**全仓扫描型**守卫，它们的判据面是"整个仓库 + crontab"，
所以**任何新文件都可能是它们的输入**：
`tests/test_except_pass_ratchet.py`（全仓 `except:pass` 计数棘轮）、
`tests/test_dead_db_literal_gate.py`（全仓 `ftseek_us_live.db` 字面量声明门）、
`tests/test_control_loop_ledger.py`（**crontab 全解析面** ↔ 台账/基线对账）、
`tests/test_w456_log_hygiene_ratchet.py`、`tests/test_ops_scripts_sync.py` 等。
自己的测试文件天然不会去查这些。

### 10.1 四例逐条：谁的责任、怎么修

| # | 守卫 | 红的原因 | 归属 | 处置 |
|---|---|---|---|---|
| 1 | `test_except_pass_ratchet` | 计数 **951 → 960**：`cutover_preflight.py` 9 处 + `cutover_rehearse.py` 1 处 `except…: pass/continue` | **本批** | ✅ 10 处全清剿，`BASELINE` 按"只降不升"调低 **951 → 950** |
| 2 | `test_dead_db_literal_gate` | `cutover_preflight.py:59` / `cutover_rehearse.py:45` 的 `LIVE_DB = DATA / "ftseek_us_live.db"` 未声明 | **本批** | ✅ 两条补进 `ALLOWLIST` 并写明理由（双载体工具，与 `env_separation_shadow`/`daily_invariant_check` 同类） |
| 3 | `test_control_loop_ledger::test_crontab_loops_are_ledgered_or_baselined` | crontab 第 276 行调 `cutover_preflight.py`，**既未入台账也未登记基线** | **本批** | ✅ 入台账 `crontab-cutover-preflight` 行（含层级/测什么/6 条失效模式与对策） |
| 4 | `test_control_loop_ledger::test_crontab_baseline_has_no_ghost_entries` | 基线里的 `da_glm_shadow_eval.py` 已不在 cron 面 | **非本批** | ⏸ 未代改，见 §10.4 |

**#3 刻意选"入台账"而不是"塞进存量基线"**：`crontab-loop-baseline.txt` 是给**存量未入账环**的
棘轮墓地，而本哨兵是**新环**，按台账 §6.1「新增 cron job 必须入账」应当声明它属于哪一层、
测什么、失效模式是什么。塞基线等于**一出生就进墓地**，恰好是那道守卫要防的事。

### 10.2 清剿不是"把 pass 换成 log"，是把**假绿面**补掉

10 处里有 6 处是同一形态：`try: con = _ro(db) / except FileNotFoundError: continue`
——「某一臂的库文件不在就跳过这一臂」。对一个**诊断器**来说这是最坏的失效：
库被改名/删除时，P3–P9 会**集体报"干净"**，而这正是本工具要防的失效形态。
所以修法不是加一行 log，而是：

- 新增 `_open_lane(tag, db)`（`cutover_preflight.py:82`）：缺失时写 `SKIPPED_SOURCES`；
- `main()` 汇总**显式打印**：`→ ⚠️ 有 N 个数据源打不开而被跳过 (上述读数可能不完整, 不得当作'干净')`；
- 冷却态读/写失败改**具名留痕**，并把失败方向定成 fail-loud（宁可重复推送，不静默抑制告警）；
- P6 日志档 `OSError` 留痕（否则"零 STOP 里程"无法区分"真没挂过"与"日志没扫到"）；
- `cutover_rehearse.py` 的沙箱守卫由 `try/relative_to + except ValueError: pass` 改
  `parents` 判定（语义等价：`relative_to` 成功 ⟺ 目标是自身或自身祖先），
  不再把"守卫通过"与"守卫异常"共用一条 `except` 通道。

### 10.3 钉子 28 → 30，变异 8 → **10 刀全杀**

新增 2 钉，各配一刀变异：

| 钉子 | 钉什么 | 变异刀 | 结果 |
|---|---|---|---|
| `test_missing_lane_is_recorded_not_silently_skipped` | 缺库**必留痕**（阳性）+ 有库**不误留痕**（阴性），6 个探针逐个过 | **刀9**：`_open_lane` 去掉 `SKIPPED_SOURCES.append` 退回静默 | 杀 |
| `test_probe_crash_returns_two_so_dingtalk_fallback_fires` | 探针抛异常 ⇒ `main()` **必须 `return 2`** | **刀10**：`return 2` → `return 1` | 杀 |

**刀10 钉的是整条告警链的命门**：cron 的兜底只看 `rc>=2`，而 `rc=1` 的语义是
"有跃迁且**已推飞书**"。若哨兵崩溃返回 1，就**双通道全静默**（飞书没推、钉钉不触发），
而"守卫自己崩了"与"零违反"输出不可区分 —— 与 §8.3 的 `na>ok`、"基线不自动写"
是同一条设计脉络的三个面。

**阳性对照的纪律**（沿用 §16:30 handoff ⑪ 的教训）：新钉不止断言"不崩"，
而是**先造缺库场景证明留痕真的会出现**，再造有库场景证明不会误报。
只断言"不崩"的钉子会被"永远 continue"白拿。

### 10.4 剩下那 1 例不是本批的，但**明天会复发**

`da_glm_shadow_eval.py` 的 cron 行是 `35 6 15 9 *`（**09-15 一次性**），今日 06:35 已跑
（`logs/da_glm_shadow_eval.log` mtime 实证），随后被 owner 从 crontab 摘除
（crontab 276 → 277 行的净变化里含这一删一增），但 `docs/references/crontab-loop-baseline.txt:29`
的基线行没同步删 ⇒ 幽灵。**本批未代改**（与对方 18:32 的处置对称：不介入他人在途工作流）。

**处置建议（一行）**：删 `docs/references/crontab-loop-baseline.txt` 第 29 行。
**⚠️ 但同类会立刻复发**：`da_prompt_distill_shadow_eval.py` 的 cron 行是 `35 6 16 9 *`
（**09-16 一次性**，仍在 crontab 第 241 行），明天跑完被摘除后会产生**第二个幽灵**。
⇒ 根因不是漏删一行，而是**一次性作业不该进"存量环路基线"**：基线的语义是
"存量未入账的常驻环"，一次性作业跑完即消失，天然与 `test_crontab_baseline_has_no_ghost_entries`
的反向核对冲突。**建议 DA 工作流 owner 一并处置**：两个一次性 eval 作业都不入基线
（改入台账并用「留痕不入守卫」行标记，该标记已是台账既有豁免机制）。

### 10.5 终局回归读数（16:52）

- **全仓 `pytest tests/` = 5148 passed / 1 failed / 54 skipped（288s）**，
  唯一失败 = §10.4 的幽灵条目（非本批）。
- 四门定向复跑 = **44 passed / 1 failed**（同一条幽灵）。
- 本批钉子 `tests/test_cutover_infra.py` = **30 passed**。
- 哨兵改后 dry-run：`block=1 warn=7 ok=2 na=0`、`[watch] 与基线一致, 静默`、**exit=0**
  ⇒ 清剿未改变任何探针读数与棘轮行为。
- **生产库零接触（第四次复核，口径按臂分开看）**：
  `live` 臂 6 项指纹**逐项相同**（行数/sha/`max_updated_at`/`trade_records` 计数/`max_trade_date`/symbols）；
  `sim` 臂 4 项变化**全部来自真实交易**而非本批（FCX 已平 ⇒ symbols `[FCX,UBER]→[UBER]`、
  `open_positions` 2→1 行、`trade_records` 823→827、`max_trade_date` 09-11→09-14），
  且 `max_updated_at` 未变（UBER 从未移动止损）。
  **判读纪律**：指纹"变了"必须先归因再下结论 —— 本批只开只读连接（`mode=ro` URI + AST 只读钉），
  变化项与昨日 5 笔成交一一对得上，属**正确的变化**。
