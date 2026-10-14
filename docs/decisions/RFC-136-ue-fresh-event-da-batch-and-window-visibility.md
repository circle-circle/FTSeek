# RFC-136: UE 新鲜事件 → DA 当日增量候选批 + DA 主链窗可见化

- 日期: 2026-10-06
- 状态: 已实施 (影子默认, `--execute` 未开)
- 类型: L3 (新载体脚本) + L0 (窗静默 fail-closed 可见化)
- 用户裁决: 「当日新鲜事件做当日候选池」(先影子后执行); 2026-10-06 复查批用户裁「请修复优化异常问题」

## 1. 改什么

### A. UE fresh-event → DA 当日增量批 (skill v0.92)
- `scripts/da_fresh_event_batch.py` (新): 消费 UE premarket 窗最新有效批次 (≤1h 龄),
  交 skill `pipeline.py --mode full --fresh-event` 分析 ≤3 只候选。
  **默认隔离影子**: `--no-db-write --no-pool-write --output-dir data/shadow/da_fresh_event/<ET日期>`;
  `--execute` 才写生产 DA 指令 (当前未开, 转正须预注册判据 + 用户裁决)。
- crontab: UE inject/push 退役, 原行改串 `llm_universe_scan.py scan && da_fresh_event_batch.py`
  (21:10/22:10 CST DST 双季, 每季恰一行在 UE premarket ET 窗 0900-0929 内生效)。
- `scripts/llm_universe_scan.py`: `cmd_scan` 增加 ET 窗守卫 (da_feed 0730-0805 /
  premarket 0900-0929) + `--force-window` 逃逸口 — DST 双季双挂后 off-season 那一发
  在写批次前被拒, 不落污染样本。
- skill `pipeline.py`: `_load_ue_short_candidates(window=)` 显式窗口参数 (默认 da_feed,
  常规主链零变化); `Pipeline(ue_window=, ue_only=)`; fresh-event 批复用当日
  `opportunity_scan` 市场上下文, 跳过 Stage 1 重扫; UE 候选透传字段扩充
  (batch_id/evidence/causal_chain/confidence_basis/counter_evidence/event_phase/move_basis 等)。

### B. DA 主链 ET 窗 (skill v0.92) + 可见化 (v0.93, 复查 P1-b)
- `pipeline.py --mode full` (非 fresh-event, 非 --force) 增加启动窗 07:55-08:15 ET:
  窗外不扫描/不调 LLM/不写库, 输出 `DA_MAIN_ET_WINDOW` skip JSON。
- **复查发现的定时炸弹**: EDT 季执行体是 agent job `e0d574cc1a3c` (20:00 CST=08:00 EDT),
  10-06 实测第 2 次 pipeline 启动余量仅 **4.2min**; 超窗被拒时原实现 `return` → exit 0,
  与「跑了没事」同貌 ⇒ DA 全天零指令且静默 (产物目录不生成, job last_status=ok)。
- **v0.93 修复** (本批):
  1. 窗拒绝退出码非对称: 过早 (hm<755, DST off-season 载体预期发车) 维持 exit 0 契约内
     静默; **过晚 (hm>815, 活跃季启动延迟) exit 2** — 被拒必须响。
  2. **退出码吞没根因修**: `__main__` 收尾原只计算 `_exit_code` 不使用 (仅 `_cli_teardown`
     的 os._exit 挂死路径生效), 线程干净 ⇒ 解释器 exit 0 ⇒ `sys.exit(1)`/未捕获异常
     **全部被吞成「成功」**。实测钉案: `--mode analyze` 缺 `--symbol` 修复前 exit=0。
     修复: 收尾追加 `sys.exit(_exit_code)`。
  3. **`_et_now()` 月份启发式换 zoneinfo 真源**: 原 `3<=month<=11 → -4h` 在 11 月
     (EST 已生效) 与 3/1~3月第二个周日 (EDT 未生效) 钟错 1h。实测: EST 季载体
     21:00 CST 发车 = 真实 08:00 EST (窗内), 旧启发式读成 09:00 (窗外) ⇒ **11/1 起
     EST 季每晚被 pipeline 门错拒, DA 主链整季零产出**。与 `llm_universe_scan._now_et`
     8/26 修复同款病灶 (那次是 cmd_confirm 整冬静默错样)。tzdata 缺失才回退旧近似。
- agent job `e0d574cc1a3c` prompt Step 4 同批补 skip_reason 分支
  (DA_MAIN_ET_WINDOW ≠ NON_TRADING_DAY, 防误报「非交易日」)。

### C. EST 季载体 (skill v0.92 同批)
- `ops/hermes/da_main_carrier.py` (新, 线上副本 `/root/.hermes/scripts/da_main_carrier.py`,
  sha256 对账 = tests/test_ops_scripts_sync.py): Hermes cron job `9476e44276dc`
  (0 21 * * 1-5, no_agent)。21:00 CST 在 EST=08:00 窗内执行, 在 EDT=09:00 被载体自身
  窗守卫拒绝 (事件落 `data/shadow/da_main_schedule.jsonl`)。zoneinfo 钟, 与 B.3 修复后
  的 pipeline 门一致。已知缺口 (P2-a, 11/1 前补): 无 Step-4 级摘要推送, EST 季完成时
  仅印一行计数 (no_agent stdout 即投递面)。

## 2. 为什么改
- UE inject 腿实测无增量价值 (归因台账), 退役后 UE 源承接腿 = DA-UE确认
  (`origin="ue_candidate"` → PLAYBOOK_SOURCE_SQL, 10-06 实测 2 条)。
  `app/review/source_attribution.py` 注释已同批留痕: 退役日起 UE直写腿恒 0 = 设计而非通道死。
- DA 主链是每日 70+ 指令主通道 (10-06 = 72 条), 窗静默 fail-closed + 退出码吞没
  = 两个「零产出看起来成功」的隐蔽形态, 复查批点名 P1。

## 3. 不改会怎样
- 11/1 DST 切换后 EST 季载体每晚被错拒 (B.3), DA 主链整季零指令, 且 exit 0 无告警。
- EDT 季 agent 启动延迟 >14.4min 即静默零产出 (10-06 实测余量 4.2min)。

## 4. 回滚
- fresh-event: 删 crontab 两行的 `&& da_fresh_event_batch.py` 串接段 (UE scan 主行保留);
  `--ue-window` 不传时常规 DA 仍读 da_feed; 影子产物目录可整体归档。
- 窗门: pipeline.py `git revert` 单文件 (v0.93 三处均为单文件改动);
  EST 载体: 删 job 9476e44276dc + 两侧 da_main_carrier.py。
- 退出码约定: 0=正常/契约内跳过, 1=异常, 2=迟发窗拒绝, 124=timeout。调用方影响面:
  agent job prompt (同批已补) / da_main_carrier (原样透传 rc) / da_fresh_event_batch
  (`--fresh-event` 豁免, 不经过窗门)。

## 5. 验证 (Gate 5)
- 契约测试: tests/test_da_output_contract.py `TestDAMainScheduleWindow` 4 钉
  (窗守卫字面量 / 迟发 exit 2 / 退出码不吞没 / zoneinfo 首选)。
- 实测: 窗外 (ET 12:xx) `--mode full` → exit 2 + skip JSON; `--mode analyze` 缺
  `--symbol` → exit 1 (修复前 0); 229+ 相关测试通过。
- 台账: CONTROL-LOOP-LEDGER §2.3 两行 (9476e44276dc / crontab-da-fresh-event-batch),
  test_control_loop_ledger 守卫绿。
- 生产首跑观察点: 10-07 20:00 CST agent job (EDT 窗内) + 21:10 CST fresh-event 影子
  (`data/shadow/ue_fresh_da_candidate_pool.jsonl` 首行事件)。

## 6. 2026-10-07 生产首跑复核

### A. UE/DA 融合链路
- **主链融合正常**: 08:00 ET UE `da_feed` 扫描产出, DA 主链消费后, `US.BKV` 与
  `US.MRVL` 在 bridge/raw instruction 中带 `origin="ue_candidate"`; UE 直写腿为 0 条
  (退役后设计状态, 非通道死亡)。
- **fresh-event 影子批**: 09:12 首跑因旧 bridge schema 只有 `candidate_count` 而
  DA pipeline 读 `candidates_total` 失败; v0.94 已做双键兼容。09:24–09:29 手动重跑
  成功, 3 个 UE premarket 候选中仅 `US.RARE` 进入深分析, `US.ALKT` 因不可借券、
  `US.SHEL` 因低 ATR 被筛除; 影子模式未写生产 DB。

### B. 产出口径更正
- bridge 文件为 **5 个 DA 候选**; 生产 `playbook_instructions` 最终为 **5 条 DA 指令**,
  其中 **2 条可交易 (`CEG BUY`, `BKV BUY`)**, **3 条 guardrail SKIP**
  (`PENG/NOK` 方向偏斜, `MRVL` 半导体集中度)。
- 旧钉钉推送只写「候选5只 + instruction直写DB」, 未显示 DB 最终 2/5 可交易,
  会误读为 5 条均可交易。修复: `scripts/da_bridge_dingtalk_push.py` 推送前只读查询
  生产 DB, 消息与 `data/da_push_instruction_summary.jsonl` 台账均记录
  bridge候选数、DB指令数、可交易数、SKIP数及逐标的 SKIP原因。

### C. 2026-10-07 P0 根因与执行体切换
- **checkpoint=11 不是扫描供给不足**: opportunity 漏斗 raw=104 / quality=51,
  但 20:00 agent job 的命令携带 `$SYMBOLS`; 实际 Hermes agent 进程没有注入该变量,
  filter 只收到当日已有 checkpoint 里的 11 只。watchlist SSOT 本身可读出 44 只,
  断点在 agent prompt 变量落地, 不在候选源。
- **修复**: 20:00 `e0d574cc1a3c` 从 agent job 切到 no-agent
  `da_main_carrier.py`; 载体用仓内 `get_stock_codes()` 确定性读出 watchlist,
  以 `--symbols US.A,...` 显式传入 DA pipeline, 不再依赖 prompt 环境变量。
  21:00 `9476e44276dc` 维持同一载体; 双季各自发车, off-season 一发由载体自身
  ET 窗守卫写 `skipped_outside_window` 后 rc=0 退出。输出前缀改为中性 `[DA-main]`。
- **回滚**: `e0d574cc1a3c` 恢复原 agent prompt/workdir, `9476e44276dc` 删除或恢复
  原 EST 说明; 两侧 `da_main_carrier.py` 同步回滚。数据面回滚无迁移。

### D. 2026-10-14 execute 转正 (用户裁决, SIM 盘以交易测量) — 知情破例留痕

- **裁决原文** (2026-10-14, Feishu): "目前是sim 虚拟盘，execute 能否开启，不走影子，
  通过交易测量效果 做好交易标的源源分类" + 带外补充 "覆盖DA主链的同名文件的影响
  也需要解决"。同批批准 P0 借券注入 ("如可行 同意")。
- **知情破例**: 跳过原转正门 "≥10 交易日影子攒样"。依据: ① 影子仅 6 日且空头存活
  3/3=100% LONG / 0 SHORT — 空头供给已结构性断绝 (见下 D-P0), 继续攒样 = 攒纯多头
  废样, 违背"禁纯单向多头"原则; ② SIM 盘实弹成交 (含滑点/退出/费用全链路) 本身就是
  比 fwd_ret_t1 快照更完整的测量面 (用户 9/12 阶段裁决"该出手出手"、8/14"能回测/
  实测裁决不默认攒样"的延伸); ③ 先例 RFC-082 知情破例须留痕 — 本节即留痕。
- **D-P0 同批前置 (ADR-046 语义, 代码落 llm_universe_scan.py)**: UE brief 新增
  借券快照段 (borrow_rate_series.jsonl → 具名标的 shortable/pool/fee 行, 联动初筛
  fail-open)。病灶: ADR-044 规则10 "材料无借券数据→UNKNOWN/false" + 台账从未进
  brief ⇒ 10-02 起 UE 空头 11/11 被 RFC-117 源头过滤全杀 (DEB-20261014 三线分析
  实证)。不先修此项, execute 只放多头进来 = 测量窗作废。BRIEF_TEMPLATE_VERSION
  v5→v6 (攒样系列以此切分; 影子 6 日样本作废重开, 知情接受)。
- **clobber 防护 (用户点名)**: `--execute` 原实现不传 --output-dir ⇒ pipeline 默认
  落生产 data/ 根, Stage 6b 的 opportunity_scan_<date>.json (bridge, 15+ 消费方:
  scheduler_core/_try_load_bridge、da_supply_reconcile_check、rubric、dashboard…)
  会被 1-3 候选版本**覆盖**, deep_sector_rotation/dynamic_blacklist 同被空版 clobber。
  修复: build_command 两种模式**恒传** --output-dir (本批目录) — execute 的唯一生产
  触点 = playbook_instructions DB 直写 (save_instruction 走 adapter 真 DB 路径, 不随
  output_dir)。gate jsonl/checkpoint 留本批目录 = 结算路径与影子期连续。
  契约测试: test_command_output_dir_isolated_in_both_modes。
- **UPSERT 防覆盖去重**: Stage 6b 按 date+symbol UPSERT — fresh-event 候选与当日
  DA 主链指令同标的会覆盖既有行 (方向可翻转、triggered/executed 被冲 ⇒ 已成交仓位
  的指令行显示未执行 → 重复执行风险)。修复: same_day_instruction_symbols() 进批前
  剔除当日已有指令标的 (shadow/execute 同口径; fail-open=查询失败不毙批)。这也与
  本批定位一致: 新事件**扩标的**, 重判存量不扩量。事件账本留痕 same_day_db_excluded。
- **三源分账 (用户点名"源头分类")**: pipeline Stage 6b 两处 origin 写入点按
  self.ue_only 分流 — fresh-event 批指令 origin="ue_fresh_event" (Path B), Path A
  (da_feed 并入 DA 主链) 仍 "ue_candidate", DA 自选仍 ""。source_attribution
  rfc117_v2→v3 新增 'DA-UE新事件' 腿 (CASE 序在 ue_candidate 前, 防 raw 同含两
  字面量误归; 边界测试在 test_review_source_attribution)。daily/weekly funnel 版本戳
  同步 v3。skill 版本 0.95→0.96 (CHANGELOG 同步)。
- **pool 合入生产 = 延后另裁**: --no-pool-write 两模式恒开 (隔离目录下 pool 文件是
  孤儿, 无人读)。生产池合入需改 pipeline pool 路径语义, 且会反哺 borrow_rate_snapshot
  覆盖面 (正反馈), 属独立决策 — 不在本批。
- **预注册 kill 线 (回滚判据, 2026-10-14 起算)**:
  K1. 接线死亡: execute 开闸后 3 个交易日内 fresh-event 腿 DB 新增指令 = 0 且非
      供给原因 (UE 有 LONG/SHORT surfaced 而 DB 无行) → 回滚查线。
  K2. 空头复活失败: P0 落地后 5 个交易日内 UE SHORT 候选 borrow_data_basis=MATERIAL
      占比仍 = 0 (材料进了 brief 但 LLM 不引用/池覆盖为 0) → 升级 P1 (扩池或
      basis-aware 过滤) 另裁, execute 可继续 (多头腿有效)。
  K3. 净亏损线: fresh-event 腿 ('DA-UE新事件' 分账) 累计 ≥8 笔已平或 ≥15 交易日,
      费后净额 ≤ -$500 → 回滚 --execute (删 crontab 该词), 复盘再裁。
  K4. 覆盖事故: 任何一次生产 opportunity_scan/deep_sector_rotation/dynamic_blacklist
      被本批覆盖 (da_supply_reconcile_check 或 watchdog 报警溯源到本批) → 立即回滚。
- **回滚**: crontab 两行删 `--execute` (即时生效, 单 token); 代码回滚 = revert 本批
  commit。数据面零迁移 (影子/execute 产物同目录, mode 字段区分)。
- **验证计划 (活体, 非 fixture)**: ① 次日 08:00 ET da_feed scan: universe_scan.log
  health 行出现 "借券快照:N", scan jsonl 出现 borrow_data_basis=MATERIAL 的 SHORT;
  ② 09:10 ET fresh-event 首批 execute: 事件账本 mode=execute status=complete,
  playbook_instructions 出现 origin=ue_fresh_event 行 (若供给存在); ③ T+1 三源分账
  SQL (rfc117_v3) 可读 'DA-UE新事件' 腿。TOO_EARLY 判例: 当晚无供给 ≠ 接线死,
  以 ①② 的 3 日窗为准 (K1)。
