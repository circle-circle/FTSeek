# 切 REAL 前置检查清单 (2026-10-15 全链路审计 P2 批落档)

> 用途: 任何「AUTO_TRADE_ACCOUNT=real / ALLOW_REAL_TRADING=true / live_manual_arm.enabled」
> 切档动作**之前**必过本清单。来源 = 2026-10-15 全链路审计防线可达性走查
> (报告 `docs/analysis/2026-10-15-full-chain-audit.md` §四)。
> 纪律: 本清单是**启动前人工核**, 不是自动闸; 每过一项在 handoff 留一行实证。

## 硬门 (不过 = 禁止切)

1. **启动断言自检**: `risk.preflight_fail_mode` 与 `risk.acb_fail_mode` 必须为 `closed`
   (`config/unified_config.yaml`, 现 preflight=open — SIM 期刻意) ⇒ 不切 closed 则
   `app/core/di_container.py:171-179` 的 LIVE 启动断言**拒绝启动** (这是好事, 别绕)。
2. **pl_val 币种探针实证**: `venv/bin/python scripts/probe_pl_val_currency.py --env real`
   人工跑通并留档 (2026-10-15 已修键名假绿: 现在读 `REAL_ACCOUNT_ID`, 探的是目标账户)。
   空仓时 verdict=NO_POSITIONS 不算过 — 须真实持仓 ≥1 只。
3. **四锁状态核对**: `AUTO_TRADE_ACCOUNT` / `ALLOW_REAL_TRADING` / `playbook.execute_mode` /
   毕业门禁 `graduated` — 四者按当次裁决的设置逐一对 (2026-10-15 起日亏熔断已双臂化,
   LIVE 臂武装时 REAL 账户日亏闸自动生效, 独立 latch `data/daily_loss_baseline_real.json`)。
4. **live probe waiver**: `data/live_probe_lock_waiver.json` 未过期且 locks_waived 覆盖当次范围
   (过期先例: 2026-10-12 16:00 ET 到期自动失效 ⇒ ALLOW_REAL_TRADING 收回 false, 冒烟双臂项拦)。
5. **手动面口径确认**: 看板手动单**恒走 REAL** (`manual_order_service._is_live()` 恒 True,
   2026-10-15 已拆平仓单向门) — 切档后看板买入/平仓都是真钱, 操作人须知。

## 软门 (建议过, 不阻断)

6. 熔断分层地图重读 `docs/circuit-breaker-map.md` (2026-10-15 刷新版): 六套日亏损权威并存
   现状已知悉 (`symbol_daily_loss_guard.py:22-38` 登记, 合并属人工裁决)。
7. HTB 借券费读数在手: `data/htb_borrow_fee_sensitivity.json` (2026-10-15: 费用非约束,
   但 EXT 实盘空侧费前净额为负 — 策略问题先于费用问题)。
8. 净口径双实现已知悉: 报表侧 `COALESCE(pnl_net, realized_pnl)` vs LLM 侧回退净额
   (当前 delta=0; 出现「有费但 pnl_net IS NULL」行时会分叉 — 切 REAL 前建议消一)。
9. `preflight_fail_mode` 改 closed 后先在 SIM 面重启冒烟一次 (七项全绿) 再切臂。

## 切档后首日

10. 启动冒烟 `startup_smoke_check.py --wait 90` 七项 + 双臂安全项必须是**真绿**
    (锁闭期该项结构性恒绿, 不能当臂隔离证据 — `startup_smoke_check.py:147-160` 自陈)。
11. 首笔 LIVE 镜像单的三源分账/落库/推送逐条人工核 (RFC-132 LIVE 镜像终态口径)。
