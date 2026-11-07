# AUDIT-20261107 · 周六周检推送的信噪比与异常逻辑复查 (附落地修复)

**对象**: `ftseek_weekly_checkpoint.py` (Hermes cron `0e87d094dcdf`, 周六 09:05 CST) 2026-11-07 投递
**性质**: 只读复查 + 报告层/哨兵层修复; **零交易行为变更**
**执行**: 全部数字由本机 DB / 产物 / crontab 实测复现, 不引用原推送自述

---

## 一、复现结论: 数字对, 逻辑与信噪比不对

逐项重跑 `trade_records` 后, 三个经济边际数字逐位复现 (本月 12.07/8.51/+3.56, 滚动 3 月 7.99/11.87/−3.89),
且本月与滚动窗全部 `source_type=PLAYBOOK_SIM` (无母体污染)。四锁值、`207 = 105 + 89 + 13`、
VWAP 与锚限价两条判定在各自主门上均正确。

问题集中在四处:

### 1. 信噪比: 30,965 B 里 ~90% 不承载决策
| 区块 | 字节 | 占比 | 实测性质 |
|---|---|---|---|
| 注册表 OBSERVING 26 项 | 4,954 | 16% | 账本 (其中 KILLED 46 + SUPERSEDED 4 占 **47.4%**, 永不变化) |
| READY 26 项 | 7,913 | 25.6% | 与 09:15 待裁决通知**逐项重复** (①3+①b1+②21+③1 = 26) |
| 四锁/冻结/三道门/哨兵 | ~3,000 | ~10% | 真信号 |

READY 标签同时是**错的**: 26 项里 23 项 `adjudicated=true` / `read_at` 有值 (多为 10-31) / `needs_user=false`,
只有 3 项真没读过 (`risk_size_tier_forward` / `eod_mfe_gate_shadow` / `be_half_at_arm_d`)。
`get_ready_to_evaluate()` 是「样本量曾经达标」的只进不退集合, 于是「判据需实测」这句话对 88% 的条目不成立。

### 2. T3 费用门连续 4 周断供, 却被渲染成软告警
`10-17 / 10-24 / 10-31 / 11-07` 均输出 `No module named 'yaml'`。根因: `check_fee_gate_shadow()`
是**进程内** import, 而周检由 Hermes 运行时 python 3.14 执行 (`import yaml` 实测 ModuleNotFoundError),
其余碰仓内代码的检查都走 `REPO/venv/bin/python` 子进程。采集/报表 cron (6:45/6:47) 本身正常 ⇒
同一条消息里出现「T3 死」+「fee_gate_shadow READY 261/30」的自相矛盾。

### 3. E2 的「统计不可分辨」与自己的 t 值矛盾 (MDE 多乘 √2)
`mde = 2.8 * sd * sqrt(2/n)`。配对日聚类检验的 SE 就是 `sd/√n`, 正确 MDE = 2.8·SE = **28.91**,
而 |diff| = **29.91** > 28.91, t = −2.897 (df=26, p≈0.008) ⇒ 按预注册口径 `t≤−2.0 ∧ |diff|>MDE`
本应**过门**。脚本注释自己写着「t=−2.897 显著但 |diff|<MDE」。
同型公式还出现在 `falsification_gate_weekly_review.py` / `phase1_gate_backtest.py` /
`direction_reversal_test.py` / `peer_momentum_premise.py` / `probe_pyramid_backtest.py` /
`holding_extension_backtest.py`。已复核另两条点名读数: VWAP 0.0022 < 0.0080、vwap_gate_ab 0.176 < 0.276
⇒ **结论不翻转, 只有 E2 翻**。E2 已退役, 另四条支柱 (OOS 衰减 / 误杀 50.7% / SHORT 样本不足 /
执行语义差) 仍成立, 但第①条支柱的数字必须更正 —— **修正判据公式属判据级变更, 另行呈裁**。

### 4. 三条「产出缺失」是结构性假警报
| 项 | 写方门控 (实测) | 结论 |
|---|---|---|
| broker_capacity_shadow | `dual_broker.py:270` `if target_env == "real"` | sim 下永不落盘 |
| broker_stop_drift_shadow | `exit_monitor_flow.py:622` `if not exit_reason and _src == "PLAYBOOK_LIVE"` | sim 下永不落盘 |
| exit_intent_reaper_shadow | `_append_shadow` 开头 `if not findings: return`; 判据原文「检出 0 ⇒ 崩溃窗口未发生」 | **文件不存在 = 通过态** |

真正坏的是 `stop_invalidation_audit`: 15 行全在 2026-10-22, 全机 crontab 零命中 ⇒ **从未挂载**, 不是断流。

### 5. 计数锚把「没量到」显示成「0 = 没在采样」
- `budget_p3_sizing`: `count_mode=where:settled` 对 `.json` 无效 ⇒ 显示 0/60, 产物 `live.n=50`;
- `confidence_tier_gate`: 同型 ⇒ 显示 0/300, 产物 `full.low.n=454`;
- `ticket_size_breakeven` / `ssr_shadow_daily`: `.json` 无约定键 / `.db` 路径 ⇒ 恒 0。

⚠️ **`ssr_shadow_daily` 不加 DB 计数轴**: 该项注册表 notes 已有 2026-09-26 裁定
「不修计数器 (不建 DB 计数轴), 处置 = 留痕真读数」, 本次复算 191 行 / 56 个日期仍达标, 裁定不变,
仅补记复算日期。**引用「该修」前先读 notes 里的既有裁定** (本次即在此处自我更正一次)。

### 6. 其它
- 四锁行把 `graduated=False` 印成「维持拦截」, 而 `ic_tracker._check_graduation` 自注 DEPRECATED
  「不得作为切实盘依据」; 实际拦截是四锁。
- 静态字面量: 「9/18 起 82 笔零缺失」实测已 **270 笔**; 「9月修复后 +3.64bps」在 +64.81bps 后面照印。
- 月读数会因迟结算翻号: 10-31 印 10 月 +3.61bps/n=154, 月末真值 **−4.69bps/n=156** (差的两笔
  `trade_date=10-30` 但 11-03/11-04 才平的 MRK −$260.01 / LMT −$433.84)。

---

## 二、已落地修复 (报告层 / 哨兵层, 零交易行为变更)

| # | 文件 | 改动 |
|---|---|---|
| 1 | `ops/hermes/ftseek_weekly_checkpoint.py` | T3 改走 `venv/bin/python` 子进程 (**恢复 4 周断供的费用门读数**) |
| 2 | 同上 | READY 块只印 `adjudicated=false / needs_user` 的 3 项 + 一行「另 24 项已读毕」+ 到期重看项 |
| 3 | 同上 | summary() 里 `[READY_TO_EVALUATE]` 段剪掉 (与 2 重复), `[KILLED]/[SUPERSEDED]` 压缩成一行计数 |
| 4 | 同上 | E2 由 3 行冻结统计压成 1 行「已退役, 读数冻结」 |
| 5 | 同上 | 经济边际: n<30 不下结论; 结论行同印滚动 3 月; 删「9月修复后 +3.64bps」; 加「月内读数随迟结算漂移」 |
| 6 | 同上 | 四锁行标注 graduated 为 deprecated 兼容值; 「9/18 起 N 笔」改为现算 |
| 7 | `app/playbook/shadow_registry.py` | 新增 `dormant_reason` 字段 (3 条只读休眠项显式登记), `check_mounting` 缺失告警跳过 |
| 8 | 同上 | `budget_p3_sizing → json_key:live.n`; `confidence_tier_gate → json_key:full.low.n` |
| 9 | 同上 | `refresh_counts` 把 None (未量到) 与 0 (真空) 分开; summary/看板/`criteria_status` 渲染 `—` |
| 10 | 同上 | `ADJUDICATED` 补登 `confidence_tier_gate` (10-31 首读 KEEP_SHADOW, 11-07 复跑同判) |
| 11 | crontab | 补挂 `52 6 * * 2-6` `stop_invalidation_audit.py` (备份 `backups/crontab-20261107-110449.txt`) |

**效果实测 (同一份数据, 同一解释器)**:
- 正文 30,965 B → **9,206 B (−70.3%)**;
- 待裁决块 26 项 → **3 项** (真未读), 另 24 项收成一行;
- T3 行由 `⚠️ No module named 'yaml'` → `✓ n=261/30 status=CURRENT_GATE_BYPASS`;
- 三条假「产出缺失」消失; `stop_invalidation_audit` 实跑写入 16 行 (15→31), 超龄告警解除。

---

## 三、未做 (需裁决)

1. **MDE √2 修正 (7 个脚本) + E2 退役理由①更正** — 方向是放宽门槛 (更容易判「可分辨」), 属判据级变更,
   须先出前后翻转对照再裁。VWAP / vwap_gate_ab 两条经复核**不翻转**。
2. `budget_p3_sizing` / `confidence_tier_gate` 的 eval 仍未挂 cron (它们由一次性 job `2a80c016ffd6`
   在 11-07 06:05 跑出产物) ⇒ 周检「悬空观察」两行仍会每周响, 直到补挂或改 `eval_on_demand=True`。

## 四、已知红 (均**先于本批**存在, 已用 stash 对拍确认与本次改动无关)

- `tests/test_criteria_registry_visibility.py`: 判据 A11「死单率」在注册表查无 (来自 HEAD 提交 `031a5feda`)。
- `tests/test_rfc139_review_fixes.py::TestP4EntryBandWidenNotFakePending`: `entry_band_widen`
  的 `recheck_after=2026-11-04` 已过期 ⇒ `overdue` 把它抬回 `needs_adjudication=True`, 而测试断言
  「到期重看 ≠ 待裁」。这是**语义冲突** (实现 vs 测试), 需裁: 到期重看是否应进待裁面。

## 五、回滚

单文件回滚: `git revert <本批提交>`; crontab 回滚: `crontab /opt/ftseek/backups/crontab-20261107-110449.txt`。
本批不改任何交易路径代码、不改 config、不重启服务。