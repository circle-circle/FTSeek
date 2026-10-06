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
