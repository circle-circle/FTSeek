# DEB-20261111 — 23:35 退出监控 / PositionSync 日志前后全级别核查

- 状态：**复核与零交易行为观测/展示收口完成；sync recovery 竞态根因仍待 RFC/影子**
- 时点：2026-11-11 23:46 CST（10:46 ET）；用户给定窗口 23:35:40–23:36:41，扩查 23:20–23:46。
- 范围：`ftseek_us.log` 全级别、同窗辅助日志、只读 SQL/YAML/env、一次只读券商 `position_list_query`。
- 初始轮纪律：零下单、零 DB 写、零重启、零代码/配置/cron/推送变更；不手工改 `open_positions`。续办审计化写入见 §8/§9。

## 0. 一句话结论

用户贴出的三条 INFO 都不是强平、漏管或账实漂移：账户级回撤 0.42–0.46% 远低于 3% FLAT_ALL 阈值；legacy 退出监控“跳过 2 个”只指 PositionStore 里的 LLY/BWXT 残留内存仓，Playbook 快循环仍管理 11 个未平仓；`PositionSync Done ⚠差异` 是汇总口径把 2 个内存残留仓与 11 个 Playbook 仓重复相加。只读券商对账 11/11、方向/数量零漂移，`exit_intents` 360 全 BOOKED。23:20–23:39 主日志 377 INFO / 22 WARNING / 0 ERROR / 0 CRITICAL；22 条 WARNING 全为 chase_cap 遥测与拦截。

## 1. 三条日志逐条判定

| 日志 | 代码锚点 | 实测证据 | 判定 |
|---|---|---|---|
| `组合止损·账户级 broker_drawdown=0.43/0.44% > managed_drawdown=-0.00%, FLAT_ALL用账户级` | `app/risk/position_exit_monitor.py:645` | `capital_state_history` 23:34:10–23:36:44：peak=99999.496，equity 99561.702→99574.936，broker_dd 0.4378%→0.4246%；`risk.portfolio.stop_drawdown_pct=0.03`；窗口无 `组合止损·trigger` | INFO 选择账户级回撤作为 FLAT_ALL 判据，不是已触发 FLAT_ALL。managed 集合剔除 Playbook 后为空，故 `-0.00%`。 |
| `退出监控·playbook 跳过 2 个 playbook 仓位` | `app/risk/position_exit_monitor.py:982` | 22:42:06 LLY、22:58:27 BWXT 被 sync recovery 加入 PositionStore；AMZN 22:45:10 已清理；23:15:49 Playbook 退出监控报 11 个未平仓；23:26–23:42 快循环仍有影子/豁免记录 | legacy 防双重平仓守卫，预期行为。“2”不是 Playbook 总仓数，也不是 Playbook 跳过出场。 |
| `US Sync Done Futu:11 / 主线DB:2 / playbook自管:11 / 一致性:⚠差异` | `app/core/position_sync.py:1927`、`app/core/position_sync.py:1947`、`app/core/position_sync.py:1953` | `_db_count=position_store.count()` 实为内存仓数 2，不是 `open_positions` 表数；`open_positions` 11 = 未平 `trade_records` 11 = broker 11，逐仓数量 MATCH；23:15 后无详细一致性 WARNING | 汇总假阳性：11 ≠ 2+11。标签“主线DB”误导；详细三层校验与券商对账均无漂移。 |

## 2. 前后事件链

- 22:31–22:32：DDOG 退出，短暂 `DB 有多余持仓` 后 BOOKED。
- 22:38–22:45：AMZN sync recovery → SL_INITIAL 退出 → 内存幽灵清理；净 -51.75。
- 22:42 / 22:58：LLY / BWXT sync recovery 进入 PositionStore，形成后续“跳过 2”和汇总重复计数。
- 23:09 / 23:15：CAT / GS 退出；退出前短暂 DB-extra WARNING，随后 BOOKED；23:15:49 Playbook 未平仓数回到 11。
- 23:20–23:46：`auto_execute` 每约 3 分钟扫 27 条指令，0 成功；TMUS/LMT 量能门 BLOCK，ANET/AVGO 被 chase_cap 或 VWAP 斜率门拦截。无新成交。
- 23:26 / 23:28 / 23:30 / 23:41–23:42：PlaybookExitFastLoop 记录 TSLA 高 ATR 停滞影子、BWXT 死单早期影子、UBER/HON/PDD/TSLA/LLY/GFL W_WEEK 豁免；均为 record-only 或转 SL/EOD/5 日上限链，不是强平。
- 23:36 / 23:41 / 23:46：exit_intent_reaper 未检出账实不符；23:37 healthcheck 通过；23:38 orderflow 非采样点跳过；23:39 deploy_freshness 因 stale drift  outside safe window 跳过。

## 3. 全级别与辅助面覆盖

- 主日志 23:20–23:39：399 行 = 377 INFO + 22 WARNING；0 ERROR / 0 CRITICAL。进程 `LOG_LEVEL=INFO`，故 DEBUG 不落盘，不是“没有 DEBUG 事件”。
- 22 条 WARNING：ANET/AVGO/TMUS chase_cap 违例遥测 + enforce 拦截；无同步一致性 WARNING、无取价 CRITICAL、无组合止损 trigger。
- `error_alert` 23:31/23:34 扫 249 文件，23:16/23:19 ERROR/CRITICAL 0 条。
- `extended_hours_exit_cron` 最新 JSON ok：orphan/closekey/rth_zero_fill 正常，`outside_extended_hours` 跳过。
- `event_stream_ingest` 最新源 OK，cadence_skip 为 15min 节流；`tripwire_checker` 10:23 ET dry/enforce=false，失效 0。
- 四锁仍 sim：`AUTO_TRADE_ACCOUNT=sim`、`TRADE_ENV=1`、`FUTU_TRADE_ENV=1`、`ALLOW_REAL_TRADING=false`、`execute_mode=futu_simulate`；DB `integrity_check=ok`。

## 4. 观测面缺口与待收口

1. **汇总口径重复计数**：`Done` 行用 `position_store.count() + playbook_count` 与 Futu 比较；LLY/BWXT 既在内存残留又在 Playbook 未平集合，必然 ⚠。22:55 待生效补丁修详细 Memory 幽灵告警与 legacy trail 覆盖，不直接修该汇总公式。
2. **补丁未生效**：当前 PID 2589190 启动 17:58:59；`position_sync.py` / `position_monitor.py` mtime 22:58:31；`deploy_freshness` 持续 skip。统一重启前仍跑旧代码。
3. **SL/TP 显示污染仍在**：LLY `open_positions` SL/TP=1394.9614/1851.99，指令与 `trade_records.metadata` 真值=1352.35/1530.29；BWXT=124.8618/180.93 vs 121.12/137.05。Playbook 出场读 `trade_records.metadata_json`，实际退出真值未错；看板/旧风控取值面仍错。
4. **INFO 噪音**：账户级回撤选择与 legacy skip 每 15s 重复。可另立零交易行为展示/降噪批，改为状态变化或阈值相关时输出；本轮未改。

## 5. 可复现检查

```bash
awk '$0 ~ /^2026-11-11 23:(2[0-9]|3[0-9]):/' logs/ftseek_us.log
rg -n 'US Sync Consistency|组合止损·trigger|PlaybookExitFastLoop|策略仓位恢复|清理真幽灵仓' logs/ftseek_us.log
sqlite3 -header -column 'file:data/ftseek_us.db?mode=ro' \
  "SELECT op.symbol,op.size,tr.quantity FROM open_positions op JOIN trade_records tr ON tr.symbol=op.symbol AND (tr.exit_time IS NULL OR trim(tr.exit_time)='');"
sqlite3 -header -column 'file:data/playbook_exits.db?mode=ro' \
  "SELECT state,COUNT(*) FROM exit_intents GROUP BY state;"
sqlite3 -header -column 'file:data/ftseek_us.db?mode=ro' \
  "SELECT snapshot_time,current_equity,peak_equity FROM capital_state_history WHERE snapshot_time BETWEEN '2026-11-11T23:34:00' AND '2026-11-11T23:37:00';"
PYTHONPATH=. ./venv/bin/python - <<'PY'
from scripts.live_reconcile_check import _broker_positions, _internal_open, _diff, _traded_symbols, _external_allowlist
broker, label = _broker_positions()
internal = _internal_open()
drift, external = _diff(internal, broker, _traded_symbols(), _external_allowlist())
print(label, len(broker), len(internal), drift, external)
PY
```

注：只调用 `live_reconcile_check` 的只读函数；不运行该脚本 main，避免写审计 JSONL / PushHub 告警。

## 6. 未验证 / 未做

- 未开启 DEBUG；未重启；未跑全仓测试；未改任何交易门/仓位/退出参数。
- 未手工纠正 LLY/BWXT 历史 `open_positions` SL/TP；未修 sync recovery 竞态根因。
- 未观察统一重启后的 `Done` 行是否转 ✓；若内存残留再次被 recovery 写入，汇总仍可能 ⚠。

## 7. 下一步

统一重启窗口按 AGENT.md 核未平仓/在途出场与 WIP 归属后执行；重启后观察详细一致性 WARNING 与 `Done` 汇总。若 `Done` 仍 ⚠，另立零交易行为测量修复：让汇总比较使用互斥集合或排除 Playbook 内存残留，并把“主线DB”标签改为真实口径。LLY/BWXT 显示值走既有只读对齐/RFC-195 路径，不手工 SQL。

## 8. 2026-11-11 23:59 续办回执：汇总口径修复 + 统一重启 + live 出场复核

- **代码（零交易行为）**：`app/core/position_sync.py` 新增 `_sync_done_counts()`，Done 汇总从 `position_store.count()` 改为“内存仓排除未平 Playbook symbols”的互斥计数；日志标签 `主线DB` → `主线内存`。新增 `tests/test_sync_summary_playbook_exclusion.py` 4 例：残留 Playbook 不重复计数、非 Playbook 内存仓仍计数、DB 查询失败保留旧 fallback、无 PositionStore 回退 broker 数。
- **验证**：聚焦 `tests/test_sync_summary_playbook_exclusion.py tests/test_sync_consistency_memory_exclusion.py tests/test_playbook_trail_write_guard.py tests/test_rfc195_open_position_stop_align.py tests/test_open_position_sl_tp_contract.py tests/test_position_sync_pending_status.py` = 33 passed；相邻 `tests/coordinator/test_position_sync_p0_fixes.py tests/test_position_sync_backfill_clock.py tests/test_backfill_open_position_sl_tp.py tests/test_pending_sync_staleness.py tests/test_last_sync_at_writer_scale.py` = 44 passed；`py_compile` OK；`git diff --check -- app/core/position_sync.py` OK。测试框架报告已知生产库写入 7 次存量豁免，非本批新增。
- **重启前核**：23:57:59 未平仓 10；`exit_intents` 非 BOOKED=0；进程 17:58 后 app 侧改动仅 `twelvedata_provider.py` / `position_sync.py` / `position_monitor.py` 三个已落档零行为补丁，scripts 多为 cron 侧独立生效。23:58:02 `systemctl restart ftseek-us`，PID 2626019；启动冒烟 7/7。
- **重启后实测**：23:58:14 `Done | Futu:10 | 主线内存:0 | playbook自管:10 | 一致性:✓`；持仓恢复逐仓跳过 Playbook，不纳入主线 PositionStore；23:58:21 Playbook 退出监控 10 仓；只读券商对账 broker=10/internal=10/drift=[]/external=[]；`exit_intents` 361 全 BOOKED；启动段无 ERROR/CRITICAL/Traceback。
- **持续观察至 00:01**：`capital_state_history` 23:58:45→00:01:18 每 30s 更新，00:00 Playbook 快循环正常，无 ERROR/CRITICAL/一致性 WARNING/legacy playbook skip。Done 转 ✓ 后不再打 INFO 是既有降噪设计（状态不变转 DEBUG），不是同步停止。
- **live 新增出场**：BWXT 23:54:35 `SL_BREAKEVEN` 限价平仓，23:55:13 BOOKED，净 +$11.42（毛 +$15.66 / 费 $4.24）。23:55:10 曾出现 `exit_intent_release_blocked_by_unsettled_leg` WARNING 并推送，38s 内成交证据回流收口；tombstone/幽灵清理/PDT/归因正常。23:55 起 legacy skip 从 2→1，BWXT 内存残留被清理；重启后主线内存 0。
- **历史 SL/TP dry-run**：`scripts/backfill_open_position_sl_tp.py` 默认只报告，命中 HON/GFL `missing_tp_json` 2 行，未 `--apply`。LLY 仍是 SL CAS 候选（1394.9614 vs 真值 1352.35），但既有 `_align_open_position_stops` 只补空 TP、不覆盖已有错误 TP（1851.99 vs 1530.29）；backfill 脚本也只补空缺。故 LLY 显示污染不能靠现有两个工具完全收口，需另立审计化 CAS 展示对齐或扩展 RFC-195 路径。BWXT 已平仓，不再属未平显示修正范围。
- **未做**：未手工 SQL；未 `--apply` backfill；未修 sync recovery 竞态根因；未改交易门/出场阈值/票面；未跑全仓测试。
- **下一步**：观察重启后 30s/15s 循环是否持续 ✓ 且无 legacy playbook skip；若需纠正 LLY 展示值，先做只读 CAS dry-run + 审计落盘，再裁是否写入；HON/GFL `missing_tp_json` 可用既有脚本 `--apply` 幂等补齐。

## 9. 2026-11-12 00:13 续办回执：SL/TP 展示收口 + GFL 出场 + 9/9 账实

- **纪律**：仅测量/展示审计化写入；未改交易门/出场阈值/票面，未手工 SQL，未新增重启（沿用 §8 23:58:02 进程 PID 2626019）。
- **HON/GFL `missing_tp_json`**：既有 `scripts/backfill_open_position_sl_tp.py --apply` 补齐 2 行，审计 `data/audits/open_position_sl_tp_20261111T160544Z.jsonl`；幂等 dry-run 0 行。GFL 00:06 平仓后 `open_positions` 行随收口移除。
- **LLY recovery 展示污染**：新增 `scripts/align_playbook_recovery_display.py` + `tests/test_align_playbook_recovery_display.py`；默认 dry-run，`--apply` 先写审计。CAS 仅处理未平 Playbook、`current_sl==initial_sl` 未 trailing、SL/TP 与 `trade_records.metadata` 真值差异 >0.005、`take_profit_json` 空/`[]`。LLY 已由 1394.9614/1851.99 对齐到 1352.35/1530.29，审计 `data/audits/playbook_recovery_display_20261111T160742Z.jsonl`；00:10/00:13 多轮 sync 后未回写，幂等 dry-run 0 行。
- **GFL live 出场**：00:06:06 `SL_TRAIL_LOCK` 限价平仓，intent `d63e91d47aa0490eb272225be7d4ec37` 00:06:42 BOOKED；DB `exit_price=42.61`，毛 +$0.96 / 费 $4.66 / 净 -$3.70。00:06:25 Done 短暂 ⚠（Futu 9 / playbook 10）是成交证据回流前窗口，00:06:42 回 ✓；reaper 00:06 未检出账实不符。
- **账实/存活**：00:11:43 只读券商对账 `sim(acc=19930823) broker=9/internal=9/drift=[]/external=[]`；未平 `trade_records=9`；`exit_intents=362` 全 BOOKED；最新 Done 00:06:42 `Futu:9 | 主线内存:0 | playbook自管:9 | ✓`；`capital_state_history` 持续更新到 00:14:52，当前 broker_dd≈0.4009% < 3%，组合止损 trigger=0。重启后主日志 INFO=442 / WARNING=14 / ERROR=0 / CRITICAL=0，无 Traceback；重启后无 legacy playbook skip。
- **WARNING 分类**：14 条为启动配置/降级类、chase_cap、RSS 台阶、GFL 出场瞬时同步差异；无组合止损 trigger。另 00:07 业务心跳报 2026-11-10 bridge `edge_gate_shadow` 写入=0（近10桥日覆盖 0.9），属影子覆盖断流，早于本批且与退出/同步无关，未在本批修。
- **验证**：`./venv/bin/python -m pytest -q tests/test_align_playbook_recovery_display.py tests/test_backfill_open_position_sl_tp.py tests/test_sync_summary_playbook_exclusion.py` = 12 passed；`py_compile` OK；两个 SL/TP 脚本默认 dry-run 均 0 行；只读对账复用 §5 函数块；日志 `logs/ftseek_us.log`、`logs/exit_intent_reaper.log`、`logs/healthcheck.log`、`logs/error_alert.log`。本批文件 diff-check/trailing whitespace OK；全仓 `git diff --check` 仅命中无关 `obsidian_vault/symbols/US_GS.md` 既有 trailing whitespace，未顺手修。
- **未验证/未做**：未跑全仓测试；未开启 DEBUG；未修 sync recovery 竞态根因（行为变更需另立 RFC/影子）；未修 `edge_gate_shadow` 2026-11-10 覆盖断流；未改交易行为。
- **下一步**：继续观察 Done/资本心跳与 legacy skip；sync recovery 竞态另立 RFC/影子；`edge_gate_shadow` 断流转影子覆盖责任线排查；不据本批改交易门。
