# RFC-183 盘中诊断报告口径保真（SL/TP 取授权源 + ERROR 按 ET 日过滤去重 + 触发未执行归因）

- 状态: 已落地（2026-10-30 ET 盘中）
- 类型: L1 可观测性修复（只改 `ops/hermes/intraday_diagnose_late.py` 报告的取数与格式，**零交易行为变更**）
- 触发: 用户对 10-30 12:00 ET 推送说"请核查"→ 核查后裁定"要"（按建议执行）
- 载体: Hermes cron `5513c8e94b06`（`no_agent=True`，每 30min，00-04 CST Tue-Sat），下一轮即生效，无需重启服务

## 1. 病灶（10-30 12:00 ET 推送逐条复核，DB/日志/生产函数实证）

### ① 持仓 SL/TP 取镜像列 ⇒ 高估保护

| 标的 | 报告 | 生产退出链生效值 | 偏差 |
|---|---|---|---|
| US.MRK SL | 147.5145（-2.0%） | **143.90**（-4.40%，到位亏 -$258） | 高估保护 2.4pp / $139 |
| US.MRK TP | 190.285 | **162.89**（RR=1.87 ≥ min_tp_rr 1.5，真会触发） | 目标虚高 16.8% |
| US.LMT SL/TP | 623.24 / 705.47 | 623.24 / 705.47 | 一致 |

根因：报告读 `open_positions.current_sl / initial_tp`，而这是 position_sync 通用路径按
`ATR×exit_policy.atr_stop.max_loss_pct(2%)` 夹逼写的**建仓镜像**，playbook 退出链从不回写；
授权值只在 `trade_records.metadata_json.stop_loss / take_profit`（MRK 为 `origin=sync_recovery`
补录行，镜像与授权不同源）。已用生产函数 `_compute_dynamic_effective_sl` 复算确认：
`sl_tighten_beta=1.0` 不收紧、MRK 峰值 0.239% < `breakeven_at_pct` 0.5% ⇒ 生效线即初始 143.90
（入场 30 分钟内的 `_early_tight_sl_px` 147.135 已过期，MRK 已持有 2.4h）。

### ② ERROR 扫描无日期过滤 ⇒ 上一 ET 交易日的故障当"今日异常"反复推送

报告"异常: 2个ERROR"两行时间戳 `2026-10-30 03:31:29 CST` = **2026-10-29 15:31 ET**（上一交易日尾盘）。
脚本对整个当前日志文件 `grep -c ERROR`，而该文件 03:30 CST 才轮转，首两行正是这两条 ⇒ 10-30 全天每轮
推送都会重复报告。真实事件：`llm_strategy_loop` 于 ET 15:01:15 停摆，watchdog 按 617s/1217s/1817s
三级升级后 15:31:50 重启进程；因入场触发窗 14:00 ET 已关（`trigger_parts/trigger_flow.py:282`）、
退出由 `PlaybookExitFastLoop` 独立运行，实际影响 = 策略轮空转 30 分钟 + 进程重启一次，非漏单。
10-30 当日 0 条 ERROR，RTH 内 LLM 轮次最大间隔 205s（阈值 600s）⇒ 未复发。

### ③ 同一次故障双计

告警行 + `[Watchdog.heal] restart` 动作行同秒同 logger，被计成 2 个 ERROR。

### ④ 明细缺失：触发未执行无归因

"11条 → 触发8 / 执行5 / 未触发3"里，另有 3 条**触发后被门拦**（CRM `chase_cap`/`falsification_slope`、
MU/LLY `vwap_gate`）不显示，读者无法解释 8→5 的差额。

## 2. 改动（单文件载体 + 新契约测试）

1. **SL/TP 取授权源**：新增 `auth_sl_tp` 映射（`trade_records WHERE exit_time IS NULL AND
   source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')`，`metadata_json.stop_loss/take_profit`，
   同标的多笔以最新一笔为准）；展示行优先授权值，缺失才回落镜像列（fail-open，取数失败不让整行消失）。
2. **ERROR 扫描改按 ET 交易日 + 事件去重**：逐行锚定时间戳（宿主机 CST 墙钟 ⇒ `astimezone()`
   挂本机时区再转 ET，不硬编码偏移；无时间戳裸行归到最近上一条带时间戳行），只统计 ET 日期 =
   今日的行，按 `(ET秒级时刻, logger)` 去重（同秒同 logger = 同一事件）；`INFO:` 行内提到 "ERROR"
   不再误计（判据改为 `ERROR:`/`CRITICAL:` 级别标记）。展示行标 `ET` 并注明"已按事件去重"。
3. **触发未执行归因**：指令查询补 `skip_reason`，按门分桶打印
   `└ 触发未执行: N条 (门A k: SYM/…; 门B k: …)`。

## 3. 影响面

- 交易行为：**零**。载体是只读诊断脚本，不写交易表、不下单、不改进程状态。
- 报告语义变化：持仓 SL/TP 变得更保守/更准（MRK 从 147.51 → 143.90）；"异常"段从恒显示陈旧两条
  变为当日真实事件；多一行触发未执行归因。语义指纹（静默去重）会因此变化 ⇒ 本轮必推一次，属预期。
- 副本：`ops/hermes/`（库内 SSOT）↔ `/root/.hermes/scripts/`（线上载体）↔ 本 skill `scripts/`
  三份 sha256 一致 `881bd4a6…`。

## 4. 验收

```bash
# 新增契约测试 (7 passed): 授权源/回落/解析形态 + ET日过滤 + 事件去重 + INFO不误计 + 归因段存在
cd /opt/ftseek/FTSeek-US && ./venv/bin/python -m pytest tests/test_intraday_diagnose_report_fidelity.py -q
# 载体实跑 (2026-10-30 12:13 ET): MRK SL=143.9 TP=162.89; 触发未执行 3条; 异常: 无
python3 /opt/ftseek/FTSeek-US/../ops/hermes/intraday_diagnose_late.py   # 或线上副本
```

实跑输出（节选）：

```
  US.LMT   size=9.0 dir=LONG entry=$651.535 SL=623.24 TP=705.47
  US.MRK   size=39.0 dir=LONG entry=$150.525 SL=143.9 TP=162.89
• 今日指令: 11条 → 触发8条✅, 执行5条✅, 3条未触发
  └ 触发未执行: 3条 (vwap_gate_blocked 2: US.MU/US.LLY; falsification_slope_blocked 1: US.CRM)
• 异常: 无
```

## 5. 回滚

```bash
git revert <hash>            # 载体回到镜像列 + 无日期过滤的旧扫描
cp ops/hermes/intraday_diagnose_late.py /root/.hermes/scripts/   # 线上同步
```

**回滚触发条件**：报告出现取数失败/空持仓行；ERROR 段把真故障吞掉（当日有 ERROR 却显示"无"）。

## 6. 未处理（本批范围外，留给所有者）

- `open_positions.current_sl / initial_tp` 与授权值不同源（`sync_recovery` 补录行写 ATR×2% 夹逼镜像）——
  本次只在**展示层**绕开，镜像本身仍被 dashboard / exit_monitor 兜底读。属数据层修复，另开批。
- `[US Sync Consistency] … Memory 有幽灵仓` 与 `一致性: ⚠差异` 为慢性噪声（10-27 27 次 / 10-28 195 次 /
  10-29 72 次 / 10-30 153 次），详细不一致告警当日仅 1 条且紧跟建仓（已知瞬态窗），非本批引入。

## 7. 复核时发现的他人既有红（不在本批，未代修）

- `intraday_diagnose.py`（日档 21-23 点 job）：库内 10-28 版 ↔ 线上 10-10 版 **内容漂移**
  （`test_ops_scripts_sync::test_repo_and_live_scripts_in_sync` 红）⇒ 10-28 那批的改动没部署到线上。
- `ftseek_rfc174_check.py`：线上存在、库内未入库（`test_no_unversioned_new_scripts_online` 红）。
- `except:pass` 棘轮 900 → 901（HEAD 即 901，本批 delta 0；引入者在别的批次）。