# RFC-195 三处静默缺陷收口：盘前生成 NameError / 平仓告警 stale 计时器 / open_positions 合成止损

- 日期: 2026-11-09
- 状态: 已落地 (缺陷修复批, 观察面为主)
- 前序: RFC-194 (EXT 入场 T-1 分位审计)
- 变更面:
  - `app/playbook/instruction_generator.py` — `compute_instruction_values` 补 `trade_date` 形参
  - `app/playbook/close_alert_state.py` — **新增**, 平仓失败告警的进程内状态载体
  - `app/playbook/pipeline.py` — 状态改由新模块持有 + 成功收口清键 (净行数 −1, 棘轮不破)
  - `app/core/position_sync.py` — `_align_open_position_stops` + reconcile 采纳后对齐
  - `tests/test_rfc195_*.py` — 10 例 (修前 5 failed + 1 collection error)

## 1. 为什么做 (2026-11-09 盘中实测)

三条独立缺陷，全部**静默**（有日志但都不阻断，因此长期无人发现），且都在同一天盘中留下实证。

### 1a. 盘前生成恒定 NameError ⇒ 开盘 10 分钟不执行

11-06 的 [RFC-186b] 在 `instruction_generator.py:583` 传入 `trade_date=trade_date`，
但 `compute_instruction_values` **没有该形参** ⇒ 生成链最后一步必崩：

```
22:06:14 ERROR [playbook] 盘前生成异常 (已隔离, 不影响 scheduler): name 'trade_date' is not defined
  File "app/playbook/instruction_generator.py", line 583, in compute_instruction_values
NameError: name 'trade_date' is not defined
```

后果链 (今日实测)：
- `playbook_generate_state` 至今**没有 11-09 的完成标记**(最后一条 11-06) ⇒ 09:15 只能「兜底放行 (生成未完成)」
- 每轮先跑 ~295s 再崩，今日 **16 次**；`playbook.generate 308/305/300/303/300/295/230/227s` 全部为长尾
- 主循环每轮被这条死步占住 ~5min ⇒ 09:33 watchdog 判「pre_market 跨开盘 ≥120s，入场执行被阻塞」
- **首次 auto_execute 落在 09:40:22**（而非 09:30），全天只有 09:40/09:45/09:51 三轮

注：指令供给并未断（DA 08:33 已写入 22 行，RFC-148 后 DA 是唯一写库者），断的是
**playbook 自有生成的冗余与开盘时序**——即「DA 哪天断供，fallback 也已经是坏的」。

### 1b. 平仓告警 stale 计时器 ⇒ 误报 HIGH，并把真卡单掩埋

`pipeline._close_fail_first_seen` 的键是 `symbol:date:reason`，**成功平仓后从不清除**：

| 标的 | 首见时刻 | 本次报错 | 声称 age | 真相 |
|---|---|---|---|---|
| US.GOOG | 06:23:12 (上一笔平仓) | 09:35:45 | **11553s** | 本次 intent 09:35:40 建、09:36:06 BOOKED |
| US.MSFT | 07:53:20 | 07:55:27 | 121s | 真实慢: 07:56:12 才成交 (172s) |

GOOG 两条「判定为真卡单」ERROR + HIGH 推送纯误报（`11553 = 09:35:45 − 06:23:12`，逐秒吻合）。
同一 key 族的 `_close_fail_alerted` 也按 `symbol:date:reason` 去重 ⇒ **误报过一次后，
同标的当天真卡单不再告警**。不是限流、不是查询退避问题。

### 1c. open_positions 合成止损与指令真值分裂

`_sync_to_position_store_locked` 在 playbook 落库前抢先建仓（11-09 US.LLY，order 09:40:35，
sync 09:40:44），止损走「ATR×mult → `exit_policy.max_loss_pct` 2% 硬底」：

```
22:40:44 SL=$1172.47 (15.0%) TP=$1448.35 ATR=$52.81×4 hard_stop=$1172.47   ← clamp 前中间值 (仅日志)
22:40:44 策略仓位恢复 US.LLY … | SL=$1351.79                               ← clamp 后 = −2%
22:41:15 已补录 trade_record (id=1822, sl=$1311.52)                        ← reconcile 用指令真值 −5%
```

出场链只读 `trade_records`（`exit_engine` 全文无 `open_positions` 引用）⇒ **零交易影响**；
但 `open_positions` 是推送/看板/旧风控告警的取值面（`dingtalk.py:200` 读 `initial_sl`），
同一笔仓两表两个止损。当日 LLY 实际按 1311.52 管理、09:45 被「规则0.5」收紧到 1362.42、
09:50:27 保本出场净 +$8.27。

## 2. 本批做了什么

| 文件 | 改动 | 语义 |
|---|---|---|
| `instruction_generator.py` | 补 `trade_date: Optional[str]=None` 形参；None ⇒ ET 当日；失败降级 None (费用按最新费率档) 并 WARNING 留痕 | 修崩溃；费用模型按成交日选 SEC §31 费率档 |
| `close_alert_state.py` (新) | 持有 `FIRST_SEEN` / `ALERTED` + `clear_symbol()`；pipeline 以别名引用 | 状态迁出 pipeline.py（行数棘轮只降不升），判定语义不动 |
| `pipeline.py` | `_book_playbook_exit_result` 在 `booked` 后调 `_clear_close_fail_state(symbol)` | 成功收口清该标的两族键 |
| `position_sync.py` | `_align_open_position_stops(conn, symbol, sl, tp)`；reconcile 采纳成交后调用 | CAS 对齐：只改 `initial_sl`，`current_sl` 仅在「仍等于旧 initial_sl」(未被 trailing 收紧) 时改 |

## 3. 行为影响与预注册停止条件

- **1a 会真的打开 RFC-186b 的费用可行下限 sizing**（该逻辑此前从未在生成链跑过；配置
  `playbook.admission.fee_floor_sizing=true` / `max_fee_bps=11.54` / `min_notional_usd=2500` 早已存在）。
  上线前预演（今日 14 条 BUY/SHORT 指令, equity $99,561.79, 6% 帽）：

  | 标的 | 现状 | 生效后 | 名义额 | 费率 |
  |---|---|---|---|---|
  | US.NEM | 29 股 | 30 股 | $3,656 → $3,782 | 11.6bps |
  | US.UNH | 9 股 | 10 股 | $3,568 → $3,964 | 11.5bps |

  其余 12 条不变（现有股数已≈费用可行下限，费率 9.9–11.5bps）。**预演未建模 regime/water 乘数**，
  故它只证明「量级是 1 股」，不是逐笔预测。

- **S1 (方向性)**: 上线后 3 个交易日内，生成链仍出现 `盘前生成异常` ⇒ 立即回滚（说明还有第二处未定义名）。
- **S2 (经济性)**: 抬股数后新增笔的净额中位为负且低于未抬笔中位 ⇒ 关回 `fee_floor_sizing=false`。
- **S3 (结构性)**: 日名义额帽 (80% × equity) 触顶天数在首个交易周 ≥2 天 ⇒ 关回。
- **1b/1c 不改任何成交集合**（只改告警状态与展示面），不设停止条件；若 `_align_open_position_stops`
  误改了已收紧的仓位（日志出现对齐行数 >0 且该仓 `current_sl` 应保持），按 §5 回滚。

## 4. 验证

- 新增 10 例：`tests/test_rfc195_generate_trade_date.py` (3) / `..._close_fail_alert_reset.py` (3) /
  `..._open_position_stop_align.py` (4)
- **变异验证**: `git worktree` 取修前 HEAD 跑同一批 ⇒ **5 failed + 1 collection error**，
  唯一修前通过的是 `test_terminal_failure_still_alerts_immediately`（守「终态零延迟」不变语义，两侧应绿）。
- 回归: 130 个相关测试文件 2080 passed / 5 pre-existing failed / 11 pre-existing errors；
  与修前 worktree 基线差集仅 1 条 `test_exit_recovery` teardown（单跑 40/40 绿，基线上同族另一参数
  同样在批量下报错 ⇒ 顺序敏感的既有守卫，与本批无关）。
- `tests/test_pipeline_size_ratchet.py` 绿：pipeline.py 3700 行（= 基线，本批净 −1 行）。

## 5. 回滚

| 项 | 回滚 |
|---|---|
| 1a | `git revert` 本提交；或临时在 `compute_instruction_values` 里 `trade_date=None` 硬置 |
| 1b | 删 `_book_playbook_exit_result` 里那两行调用（状态本体留新模块无害） |
| 1c | 删 reconcile 里 `_align_open_position_stops(...)` 调用块 |
| 全批 | `git revert <sha>` + `systemctl restart ftseek-us` |

## 6. 遗留 / 不在本批

- `_close_fail_first_seen` 的宽限值 120s 对 MSFT 这类真实 172s 成交仍偏紧（本批不动阈值：
  改阈值 = 改告警行为，须先攒延迟分布）。
- 恢复路径的「ATR×mult → 2% 硬底」合成止损本身仍存在（1c 只做收口对齐，不改合成逻辑）；
  高 ATR 票（LLY ATR 3.9%/日）在恢复窗口内的 2% 止损仍可能在 playbook 接管前触发 —— 今日未触发。
- DA 侧不写 `playbook_generate_state` 完成标记属既有分工；本批只修「playbook 自己也写不出来」。