# RFC-078 · 实盘/SIM 库级隔离落地 + 港股成交台账 + 周报拆分推送

- **状态**: **EXECUTED** (2026-09-08 三批落地并逐条复核判据, 见 §7; 用户令「请按照目标需求请进行设计后执行」)
- **承接**: DEB-20260905-sim-data-collection §13.15(收敛层) / §13.16(影子核对) / §13.17(schema 对齐, 判定 `READY_TO_SWITCH`)
- **前置证据**: `scripts/env_separation_shadow.py` 2026-09-08 复跑 → **阻塞缺口 0 / live 侧失败 8 条全部 sim 侧同败(现役缺陷, 非切换缺口) / 安全指纹未变** ⇒ `READY_TO_SWITCH`

## 1. 病灶(取证结论)

设计**确实是双库**: `app/core/db_utils.py:120 get_main_db()` 按 `TRADE_ENV`(0=live/1=sim) 分流。
但现网 `TRADE_ENV=1` ⇒ 全局路由到 sim 库, 而看板手动交易 `_is_live()` **硬编码 `return True`**
(`app/web_dashboard/manual_order_service.py:111`) ⇒ **券商侧打真钱 REAL 账户, 账本侧写进 sim 数据收集仓**。

实证(2026-09-08):

| 载体 | `manual_order_audit` live_conservative | `trade_records` | 最后写入 |
|---|---|---|---|
| `data/ftseek_us.db` (sim) | **64 行, 至 2026-09-04 (仍在写)** | 795 行(仅 3 行 `trade_mode='live'`) | 活跃 |
| `data/ftseek_us_live.db` | 93 行, **止于 2026-06-22** | 17 行, 止于 2026-07-02 | **死库(零写入)** |

`_assert_env_routing_consistent()`(`db_utils.py:82`) **不告警** —— 它比的是 `TRADE_ENV` vs
`AUTO_TRADE_ACCOUNT`(都 sim, 自洽), 而看板手动链路绕开了这条比较 ⇒ 一致性断言存在**盲区**。

港股**不进任何 DB**: `app/web_dashboard/hk_order_service.py:36` 只写
`data/audit/hk_manual_orders.jsonl`(63 行, 仅 `submit`/`cancel`, **无成交无盈亏**);
`trade_records` 中 `symbol LIKE 'HK.%'` = **0 行** ⇒ 港股复盘当前**无数据可做**。

## 2. 为什么不能翻全局 `TRADE_ENV=0`

`get_main_db()` 是**全仓 90+ 文件共用**的解析点。翻 `TRADE_ENV=0` 会把**自动 sim 数据收集管线**
(现役 ≈200 笔/月, 喂 IC / OKR / exit_scorecard / KR2 cohort) 一并灌进 live 库 ⇒
用"污染实盘库"换"隔离看板", 方向相反。

**正确落点 = 看板专属路由**: 看板(手动实盘台) → live 库; 自动管线 → 保持 `TRADE_ENV` 路由。
这与 `tests/test_dashboard_sim_isolation.py:17` 预告的终局一致(「库级分离(看板 DB 路由切到
`ftseek_us_live.db`)」), 且不触碰 `open_positions` PK=`symbol` 的物理约束。

## 3. 设计

### 批 A · 看板专属库路由 + 历史实盘账本迁移
- `app/core/db_utils.py` 新增 `get_dashboard_db()` / `get_dashboard_trade_records_db()`:
  **恒指 live 库, 与 `TRADE_ENV` 解耦**; 由 `FTSEEK_DASHBOARD_DB_ISOLATION`(默认 `1`) 控制,
  设 `0` 即回落 `get_main_db()` = **一键回滚**。
- 解析器放在 `app/core/db_utils.py`(已在 `test_dead_db_literal_gate.ALLOWLIST` 且是 mode SSOT 本体),
  看板侧**零路径字面量** ⇒ 同时满足 `test_dashboard_has_no_hardcoded_main_db_path`。
- 替换看板内 **12 个**解析点(`analytics/battle_plan/watchlist/stock_detail/trade/data_service/executor/manual_order_service`)。
- `app/web_dashboard/env_scope.py` 谓词层**保留不动**(缺列容错 + 双保险)。
- 一次性迁移 `scripts/migrate_live_records_to_live_db.py`: 把 sim 库里**实属实盘**的行搬进 live 库
  (`trade_records WHERE trade_mode='live'` 3 行 / `manual_order_audit WHERE mode LIKE 'live%'` 64 行),
  **默认 dry-run**, 先备份两库, 按 `(symbol, entry_time, source_type)` / `(timestamp, symbol, order_id)` 去重。
  时间窗实测**几乎不重叠**(live 库 audit 止于 06-23, sim 侧 06-24 前仅 2 行; `trade_records`
  live 06-12~07-02 vs sim live 行 07-09~08-20 **零重叠**) ⇒ 迁移是干净追加。

### 批 B · 港股成交台账入库
- live 库新表 `hk_trade_records`(港股属**实盘手动**侧): `deal_id` 唯一键幂等 upsert。
- `hk_order_service.sync_deal_history(days)`: `history_deal_list_query` 为主 + `deal_list_query` 补当日,
  `trd_env=REAL` + `_acc_id()`(`HK_REAL_ACCOUNT_ID` 优先)。
- **三态纪律**: 成功→行数; 未连接/查询失败→`UNAVAILABLE` 明示, **绝不伪装成 0 成交**; 空窗→`NO_DEALS`。

### 批 C · 周报拆分推送
- 新增 `scripts/live_weekly_review.py` = **实盘周报**: 美股段(live 库 `trade_records`) + 港股段(`hk_trade_records`), 分市场两段独立小结。
- `scripts/weekly_trade_summary.py` = 维持**模拟研究周报**, 但标题显式标注口径(消除「周报=实盘」误读)。
- cron: 实盘周报独立时段, 与既有周日综合复盘(`0 10 * * 0`)错开; 首两周 `--no-push` 影子。

## 4. 预注册验收判据

1. `FTSEEK_DASHBOARD_DB_ISOLATION=1` 时 `get_dashboard_db()` = live 库; `=0` 时 = `get_main_db()`。
2. 看板 12 个解析点全部改走新解析器, 看板内**零**主库路径字面量(既有门自动验)。
3. **sim 库零污染**: 切换前后 `data/ftseek_us.db` 的 `open_positions` 全表 md5 **不变**, 且 sim 库
   不得新增 `trade_mode='live'` 行; 自动 sim 管线吞吐不受影响(仍写 sim 库)。
4. 迁移脚本 dry-run 与实跑行数一致; 备份文件存在; 迁移后 live 库 `trade_records` 实盘行数 = 17 + 迁入数。
5. 港股 `hk_trade_records` 建表幂等; 券商不可达时返回 `UNAVAILABLE` 而非空列表。
6. 全量测试不新增红; `tests/test_hermes_job_prompts.py` 在改任何 cron prompt 后必须复绿。
7. 服务重启后 `scripts/startup_smoke_check.py --wait 90` 通过, `NRestarts=0`。

## 5. 回滚

- 批 A: `.env.us` 置 `FTSEEK_DASHBOARD_DB_ISOLATION=0` + 重启 ⇒ 看板回到 sim 库(零代码回滚)。
  迁移回滚: `cp data/backups/<ts>/ftseek_us_live.db.pre-migrate data/ftseek_us_live.db`。
- 批 B: `DROP TABLE hk_trade_records` + 删新增方法(纯增量, 不改既有路径)。
- 批 C: 删 cron 行 + 删脚本。

## 6. 已知取舍(如实登记)

- **看板分析口径会变少**: `trade_records` 795 → 17+3 笔。这是**正确**的(那 795 笔本就是模拟盘),
  但历史/绩效/退出分布等面板会立刻显得"空"。属产品语义变更, 已在 §13.15 备案。
- **live 库休眠列刻意不补**(`trade_records` 7 / `playbook_instructions` 22 / `capital_state.account_id` 1):
  看板 SQL 不引用, 补了会掩盖真实缺口(沿用 §13.17 裁决)。
- `capital_state` live 库 **0 行** ⇒ 权益曲线/回撤历史会空。两处读点已验**有降级**
  (`routes/risk.py:87` try/except → 默认 100000; `routes/analytics.py:50` → 空 history),
  且 `app/risk/capital_safety.py:723/741/828` 的 `trade_env` 唯一索引 + `INSERT OR REPLACE`
  会在下次资本刷新时自愈建行。

## 7. 执行结果 (2026-09-08 落地, 判据逐条复核)

### 7.1 实测推翻设计的三处更正 (记档, 不事后美化)

1. **批B 表名**: `hk_trade_records` → **市场中立** `live_deal_records` + 新增 `live_position_snapshots`。
   原因: 首版假定"港股 ctx 只返回港股成交", **实测为假** —— 账户是综合账户,
   `history_deal_list_query` 一次返回 99 笔 = **HK 47 + US 52**。按首版落表会把美股成交记进
   "港股表"且 `currency` 硬编码 HKD ⇒ **跨市场污染 + 币种错记**, 正是本 RFC 要消灭的错误类型。
   现 `market`/`currency` 由**每行自带的 `deal_market`** 推导 (不由"哪个 ctx 返回的"推导),
   两市场 ctx 都查, `deal_id` 幂等 upsert ⇒ 重叠返回同一笔只落一行。判据 5 的**表名作废, 语义保留**。
2. **批B 落点**: 不在 `hk_order_service` 加方法, 而是新模块 `app/web_dashboard/live_ledger.py`
   + 入口 `scripts/live_ledger_sync.py`。原因: 这层是**两市场共用的券商真值层**, 塞进港股服务
   会让美股腿无处安放; 且它的价值恰恰在于**不依赖下单代码是否记得写** (断链 3 个月的教训)。
3. **批A 解析点计数**: 设计写"12 个", 实改 = **11 个既有点 + 1 个新增点** (`live_ledger._db_path`)
   = 12 ⇒ 与判据 2 一致 (计数口径在此写明, 免得下一位复核时对不上)。

### 7.2 判据逐条结果

| # | 判据 | 结果 | 证据 |
|---|---|---|---|
| 1 | 开关分流 | ✅ | `tests/test_rfc078_dashboard_db_isolation.py` (9 例): `=1` → live 库, `=0` → `get_main_db()` |
| 2 | 看板 12 解析点 + 零路径字面量 | ✅ | `grep -c get_dashboard_db() app/web_dashboard/` = 12; `test_dashboard_has_no_hardcoded_main_db_path` 绿 |
| 3 | **sim 库零污染** | ✅ | 迁移前后逐数复核未变: `open_positions` 3 / `trade_records` 795 / `trade_mode='live'` 3 / `audit mode LIKE 'live%'` 64; sim 管线最后写入 `2026-09-05T01:30` (= ET 09-04 13:30 盘中) ⇒ 仍在写 |
| 4 | 迁移行数一致 + 备份 + 17+迁入 | ✅ | dry-run 计划 3+64 = `--apply` 实迁 3+64; live 库 `trade_records` **20** (17+3)、`manual_order_audit` **160** (96+64); 复跑 dry-run `to_migrate 0 / already_present_skipped 3+64` (幂等); 备份 `data/backups/20260908-193724-rfc078-migrate/`; 为保真钱 `fees_est` 给 live 库补 5 列 (`config_version/fees_est/fees_source/mae_pct/mfe_pct`) |
| 5 | 建表幂等 + 不可达报 UNAVAILABLE | ✅ (表名见 7.1) | `tests/test_rfc078_live_ledger.py` 33 例 |
| 6 | 全量测试不新增红 | ✅ | **3423 passed / 0 failed / 41 skipped** (162.0s, `-p no:randomly`)。中途曾 2 红 = **上一批界面守卫门正确咬到本批新表**(见 §7.9-6), 已按表收紧转绿。上一批记录的**预存红** `test_r9_r13_contracts.py::TestR12EntryTimeConvention` (HOOD 2.94s) 本次**未复现**(该例是时间敏感的入场时刻口径断言, 与本批无关, 按纪律不追修); `test_hermes_job_prompts` 绿 (本批未改任何 cron **prompt**, 只加 crontab 行) |
| 7 | 重启后 smoke 全绿 + `NRestarts=0` | ✅ | 7 项全绿, `NRestarts=0`。**注意**: `--wait` 模式只在**刚重启后**有效 (它要求启动段不早于脚本启动前 120s, `scripts/startup_smoke_check.py:95`), 本批未重启 `ftseek-us` (理由见 7.5) ⇒ 用单次判定 |

### 7.3 券商真值层已回补的数据 (2026-09-08)

- 成交 **99 笔**: HK 47 (2026-07-16~09-08, 4 标的) + US 52 (2026-07-10~09-04); 跨市场污染 **0**
  (`unknown_market_rows=0`, `currency` 与 `market` 一一对应); `fee` **99/99 行 NULL** (不伪造 0)。
- 持仓快照 **5 行** @2026-09-08 (HK 3 / US 2) —— 实盘在仓此前**两地都不落库**, 这是第一份。
- 看板端点实测: `GET /api/hk/ledger?days=30` → `state=OK`、42 笔港股成交、`window_tz=HKT`、
  FIFO 毛已实现 HK$49,200 (含 `HK.02513` 的 `INCOMPLETE_COST_BASIS` 声明)、3 个持仓。

### 7.4 时间尺度补正 — 守卫门咬出来的**真缺陷** (本批最重要的自查)

4 个全量测试红逐点核过, **2 个不是风格问题而是口径错误**:

1. **券商 `create_time` 是各市场本地时** (实测小时直方图: 港股 47 笔全落 HKT 09-16 点;
   美股 52 笔集中 ET 09-16 点并带 `04:02` 盘前起点与 `00:26/22:xx/23:xx` 夜盘尾巴) ⇒
   `deal_date = create_time[:10]` 就是该市场交易日 (**不需**换算), 但**查询窗口与快照日必须按
   各市场本地日取**。首版三处用服务器 CST 墙钟 (UTC+8, 比 ET 快 12~13h) ⇒ 美股窗尾被推成 ET 的
   "明天"、当日美股仓位被标成 ET 次日。已改 `market_now()/market_today()/window_for()` +
   快照日 per-market (主键含 `market` ⇒ 两腿本可不同日), 起始多放宽 1 天 (窗口是日粒度 + upsert
   幂等 ⇒ 放宽只多覆盖边界日, 少一天则**静默漏一整天真钱成交**)。
2. **周报第一版用一个 ET 窗口切全部数据源**, 而 `manual_order_audit.timestamp` 是 **CST 墙钟**
   (写方 `app/web_dashboard/order/audit.py:183` 用裸 now; 实测同行 `timestamp=2026-09-04T21:52:25`
   与 `created_at=2026-09-04 13:52:25`(UTC) 差 8h) ⇒ 美股下午盘 (ET 12:00 后 = 北京次日) 的
   手动单被归到后一天, **跨周边界即错周**。已改 `_et_window_to_cst_bounds()`: 按**绝对时刻**
   localize 换算窗口两端 (不写死 ±12h, 沿用 `order/executor.py:883` 的 DEB-20260904 #203 判例:
   冬令时差 13h), 且**港股 jsonl (CST=HKT) 同尺不换算** —— 二次换算是反方向的同款错, 两头都钉了测试。
   同时窗口改为**每市场各一份** (`market_windows()`), 报告头与每段标题都带自己的窗口与尺度标签
   (两段不是同一个绝对区间, 不写出来就会被当同一周读)。
3. `synced_at` 定为 **UTC 带偏移** (它是"我们什么时候去拉的"审计时刻, 不是交易口径) +
   **一次性数据归一 104 行** (备份 `data/backups/20260908-211134-rfc078-synced-at-normalize/`)。
   归一时踩到并记档一个陷阱: `app/utils/timezone.py` 三个助手对 **naive 的假定各不相同** ——
   `to_utc()` 视 naive 为 **UTC** (:322)、`to_cst()` 视为 **ET**、`to_et()` 视为 **CST**。
   用 `to_utc()` 归一会把 CST 墙钟原样贴 `+00:00` = **错 8 小时** (已实测复现、回滚重做,
   正确写法 `_localize(dt, CST).astimezone(UTC)`)。
4. 另 2 红是**留痕纪律**: `except OSError: pass` (新解析器读 `.env.us`) → `logger.warning`
   (读不到开关就静默回落 = "看板可能写错库"无人知晓); 3 处 disconnect 清理 `except Exception: pass`
   → `logger.warning` (断开失败 = futu 非守护线程残留 = cron 跑完不退出, 静默吞会丢掉唯一线索)。
5. **死库门**咬到 `scripts/weekly_trade_summary.py:145` (报告文本里硬编码库文件名) → 改为指向
   SSOT 解析器 `get_dashboard_db()`, **不**扩 ALLOWLIST (该门只应放行解析器/模式切换器/双载体检查)。
6. `tests/test_env_separation_shadow.py` 的"历史行 `pnl_net` 必须 NULL"被**合法迁移**证伪
   (迁入的 `US.TTE`/`US.PDD` 带着 sim 侧当期费用净额管线算出的真 `pnl_net`) ⇒ **收紧而非放宽**:
   任何 `trade_date < 2026-09-01` 且 `pnl_net` 非 NULL 的 live 行, 必须在 sim 库找到同
   `symbol` + 同 `entry_time` 且 `realized_pnl`/`pnl_net` **完全相同**的 `trade_mode='live'` 源行
   (= 逐字搬运的自证)。已做双向证伪: 把一行 `pnl_net` 改成 `-99.99` → 转红; 真迁移行 → 放行。
   证伪力**强于**原判据 (原判据只挡"写非 NULL", 新判据还要求值与源库逐字一致)。

### 7.5 刻意不做的 (含待裁)

- **不翻全局 `TRADE_ENV=0`**: 见 §2 —— 会把自动 sim 数据收集管线 (≈200 笔/月, 喂 IC/OKR/
  exit_scorecard/KR2 cohort) 灌进 live 库, 用"污染实盘库"换"隔离看板", 方向相反; 且与看板专属
  路由**互斥** (两者只需其一, 现已选后者)。
- **未重启 `ftseek-us.service`**: 本批对它的导入图只有 `app/core/db_utils.py` 的**纯新增函数**
  (既有路径零改动), 且该服务于 20:56:55 启动**晚于**本批最后一次 `app/` 改动 (20:56:02) ⇒
  运行进程已载入新代码; 当时距美股开盘 (21:30 CST) 仅 20 分钟, 不为零收益的 restart 冒盘中风险。
  看板侧 `ftseek-dashboard.service` 是 **socket-activated** (每次激活都是新进程) ⇒ 新端点已生效。
- **港股回合台账仍缺** (无 `exit_reason`/`thesis`/`attribution`, 港股不进 `trade_records`) ⇒
  港股复盘当前只能用成交流水 + FIFO + 仓位快照。**是否建港股回合台账 = 下一批待裁**。

### 7.6 已知缺口 (第一期实盘周报末尾原文登记, 不让读报告的人自己发现)

- **孤儿行 `US.AAPL` / `US.NVDA`**: 台账 `exit_price=0` 而券商已无此仓 = 断链期平仓未落账的残留,
  这批行的盈亏**永久缺失**, 不要把它们当在仓念。
- **未入账 `US.MU` / `US.TSLA`**: 券商有仓而台账无在仓行 ⇒ 入场理由/止损不可复盘。
- **`HK.02513` FIFO `INCOMPLETE_COST_BASIS`**: 仓位建于台账首日之前 ⇒ 其盈亏**未计入**合计
  (不用现价或均价编造成本基准)。
- **所有金额是毛/成交额** (`fee` 全 NULL) ⇒ 不得当净额用 (本仓已有「毛 edge 修到 0 仍净亏」的费用判决书)。
- **快照仅 1 天** ⇒ 本期无环比, 需 ≥2 周才能看仓位变化。
- **美股回合台账仅 18 笔** (2026-06-11~08-20): 断链历史不可本地回补 ⇒ 成交/仓位可信 (券商真值层),
  但回合级 `exit_reason` 分析样本偏小。

### 7.7 cron (已注册; 原 crontab 备份 `/tmp/cron.bak-rfc078`)

```
10 17 * * 1-5  scripts/live_ledger_sync.py --days 30      >> logs/live_ledger_sync.log   # 港股收盘后
10 9  * * 6    scripts/live_ledger_sync.py --days 30      >> logs/live_ledger_sync.log   # 美股周五收盘后
40 7  * * 1    scripts/live_weekly_review.py --days 7 --sync --sync-days 14 --push >> logs/live_weekly_review.log
```

台账已入账 `docs/CONTROL-LOOP-LEDGER.md` §2.3/§2.4 (`crontab-live-ledger-sync` /
`crontab-live-weekly-review`)。周报**默认影子** (只打印), 只有 cron 那一行带 `--push` 才发飞书。

### 7.8 测试与回滚

- 新增用例: 批A 9 + 批B **33** + 批C **22** + `weekly_trade_summary` 口径自称 1
  + 呈现契约 **12** (§7.9) = **77 例**; 另**收紧**上一批 2 条界面守卫门 (批2 colspan 逐处判定 /
  批3 stack 类比值, 见 §7.9-6)。全量 **3423 passed / 0 failed / 41 skipped** (162.0s)。
- **看板已重启**: 模板 `hk_trade.html` 改动 (22:06) **晚于**原看板进程启动 (22:01) ⇒
  `systemctl restart ftseek-dashboard.service` (socket-activated, 22:17:17 起
  `ActiveState=active` / `NRestarts=0`)。`ftseek-us.service` **未动** (理由见 §7.5)。
- 回滚 (逐批独立, 均为增量):
  - 批A: `.env.us` 置 `FTSEEK_DASHBOARD_DB_ISOLATION=0` + 重启看板 ⇒ 零代码回滚;
    迁移回滚 `cp data/backups/20260908-193724-rfc078-migrate/ftseek_us_live.db.pre-migrate data/ftseek_us_live.db`。
  - 批B: `DROP TABLE live_deal_records; DROP TABLE live_position_snapshots;` + 删 `live_ledger.py`
    与两个端点 (不改任何既有读写路径); `synced_at` 归一回滚
    `cp data/backups/20260908-211134-rfc078-synced-at-normalize/ftseek_us_live.db data/ftseek_us_live.db`。
  - 批C: 删 3 行 cron + 删 `live_weekly_review.py` / `live_ledger_sync.py`
    (`weekly_trade_summary.py` 的口径自称保留 —— 那是消除"周报=实盘"误读的净收益)。

### 7.9 看板呈现面 (超出 §3 原设计的增量, 记档)

原 §3 只设计了"数据落对库 + 周报分开推"。**建完不接前端 = 数据存在而用户看不见**: 港股
历史成交此前在看板上**完全没有**(只有实时持仓 + 今日订单), 美股页的「交易记录」是回合台账
+ 审计, 而回合台账断链过 3 个月 ⇒ 两页都需要一个真值层入口。故本批多做了:

1. **方向语义收敛到单一 SSOT**: `live_ledger.summarize_deals()` + `OPEN_SIDES/CLOSE_SIDES`
   同时供实盘周报的 `deal_section` 与看板端点的 `summary` 使用。若前端按 `trd_side` 自己累加,
   就会出现"两边错得一样"(净现金流符号反)且互相印证 ⇒ 前端只**展示**后端汇总, 不自行聚合金额。
2. **新端点 `GET /api/us/ledger` + `POST /api/us/ledger/sync`** (与港股端点同源同纪律, 只差
   市场与尺度: `window_tz` 港股 HKT / 美股 ET)。两个 POST 都调**同一个** `live_ledger.sync_all`
   —— 综合账户下不存在"只补美股一腿", 保留两条路径只为让两页各有本市场语义的按钮。
3. **前端**: 港股页新增「📒 实盘成交台账」卡(三态分支 / 毛口径琥珀提醒绑定 `fee_rows_null` /
   回补按钮 / 限 50 行**且说出来**限了行 / 展示新→旧); 美股页**只加一行**上下文条 `#ledgerStrip`
   —— 本页已有「交易记录」表, 再放一张成交流水表 = 今日成交同页出现两次 ⇒ **刻意不做第二张表**
   (信噪比), 但必须排在记录表**之前**(先读口径提醒再读数据)且随卡片折叠一起显隐。
4. **台账不进 30s 券商轮询**: 本地只读, 页面加载一次; 只有点「回补」才连券商(省富途频率额度,
   且台账并不需要实时)。
5. **呈现契约测试** `tests/web_dashboard/test_trade_ui_rfc078_ledger_contract.py` (**12 例**)
   钉住这一类面板最容易**安静坏掉**的五种方式: 三态被前端塌缩成同一句"无成交"、毛口径提醒被当
   视觉噪声删掉、信噪比反向退化(第二张成交表)、表头 ↔ `td[data-label]` 漂移(承批3 A12 纪律)、
   前端另写一份方向语义; 另钉两市场端点的尺度/币种/只读性(GET 里不得出现 `sync_all`, 否则刷新
   页面就打券商)与两个 POST 必须同一个 `sync_all`。
   **自查**: 该文件首版有 3 处**锚点 bug**(切片结束锚点是起始锚点自身的前缀 ⇒ `index` 命中 `i`
   本身 ⇒ 切出**空串**, 断言在空串上假绿/假红), 已修并逐个复核切片非空(`us_poll` 33,668 字符 /
   `hk_sync` 776 / `hk_labels` 801, `data-label` 序列与 `<th>` 序列逐字相等)。
6. **上一批守卫门咬到本批新表 (全量测试曾 2 红) → 按表收紧而不是放宽**。这两条红是本批
   最有价值的一次交叉验证: "加了第三张表"这件事**被既有契约自动发现了** ⇒ 批2/批3 的门是真门。
   - `test_hk_tables_columns_match_colspan` (批2): 原判据"港股页所有 `colspan` 只许是 `{10}`"
     ⇒ 台账表 9 列转红。**不是**改成 `{9,10}` 了事 (那只挡得住"出现第三种列数", 挡不住
     "台账空态行套了持仓表的 10" = 错位), 改成**逐处判定**: `colspan==9` **当且仅当**该处落在
     台账表自己的两个区域 (HTML 骨架 / 台账 JS 块) 内。已双向证伪: 台账空态行改 10 → 红;
     订单表骨架改 9 → 红。
   - `test_stack_css_present_on_both_pages` (批3): 原判据"`class="hk-stack"` 恰好 2 处" ⇒
     第三张表转红。改成 `hk.count('class="hk-stack"') == hk.count("<table") == 3` ——
     钉的是"**每张表都挂 stack 类**"(堆叠是 CSS 变形而非第二套渲染), 写死张数在加表时只会
     假红/假绿, 钉比值才钉住它真正要防的事。
7. **看板重启**: 模板改动晚于原进程启动 ⇒ 已 `systemctl restart ftseek-dashboard.service`
   (见 §7.8); 端点复验 `/trade`(含 `#ledgerStrip`) / `/trade/hk`(含 `ledger-ctx`+`ledger-tbody`)
   / `GET /api/us/ledger` (`state=OK`, 31 笔, `window_tz=ET`, `fee_rows_null=31`, FIFO 毛
   $3,530.20, `incomplete_basis_symbols=[US.SNDK]`) 均 200。
