# RFC-134: 旧全局实盘切换退役与双臂防误翻守卫

> **状态**: 已实施并重启验证（不构成 LIVE 启用授权）
> **日期**: 2026-10-04
> **承接**: RFC-131, RFC-132, RFC-090 §17
> **触发**: 复查发现 `/api/mode/switch` 与 `/api/auto-trade/auto-account` 仍保留旧全局翻转语义

---

## 摘要

旧 Web Dashboard 开关会把 `AUTO_TRADE_ACCOUNT=real`、`TRADE_ENV=0`、
`ALLOW_REAL_TRADING=true` 作为一组全局键写入 `.env.us` 并重启服务。该模型与
RFC-131 的“SIM 常驻 + LIVE 显式臂”冲突：SIM Playbook 执行会触发
`LIVE_PATH_A_RETIRED`，大量非 Playbook 默认路由也会漂到 live 库。

本 RFC 将旧 REAL 全局入口退役为 `410`，保留 SIM-only 修复通道，并在生产启动层
阻断误翻。UI 不再展示实盘模式卡片或 REAL 账户按钮。

---

## 修复范围

| 层 | 修复 |
|---|---|
| API | `switch_mode(live_*)` 与 `set_auto_account("real")` 返回 `410`，不写 `.env.us`、不备份、不重启 |
| UI | 系统页移除“实盘保守/标准”卡片；Dashboard 移除 REAL 自动账户按钮 |
| 启动 | `AUTO_TRADE_ACCOUNT=real` 或 `TRADE_ENV=0` 时拒绝启动并 HIGH 推送；仅 `FTSEEK_ALLOW_LEGACY_GLOBAL_FLIP=1` 显式豁免测试/回滚 |
| 修复通道 | `switch_mode("simulate")` 与 `set_auto_account("sim")` 仅在旧全局快照已漂移时原子修复，并显式关 `ALLOW_REAL_TRADING` |
| 风险二线 | AccountCircuitBreaker、AbsoluteCircuitBreaker、CapitalSafety 的 SIM 分臂显式 pin 到 SIM ledger，即使 `TRADE_ENV=0` 也不漂移 |

## 语义边界

- `TRADE_ENV=1` 与 `AUTO_TRADE_ACCOUNT=sim` 是生产常量；
- `ALLOW_REAL_TRADING=true` 只能作为 LIVE 臂预授权，不代表全局翻 REAL；
- LIVE 入场只允许通过 `playbook.live_manual_arm` + `target_env="real"`；
- LIVE disarm 只停新仓，出场继续；
- 本 RFC 不实现新的 LIVE 臂控制台 API，仅先消灭旧危险入口。

## 验收

1. 旧 live API 请求返回 `410 LEGACY_GLOBAL_REAL_SWITCH_RETIRED`，且无文件/重启副作用；
2. 正常 SIM_ONLY 状态下调用 SIM 修复入口为 no-op，不会误关 LIVE 预授权；
3. 误配 `AUTO_TRADE_ACCOUNT=real / TRADE_ENV=0` 时主服务启动退出并发 HIGH 推送；
4. `TRADE_ENV=0` 注入下，SIM ACB/OKR/CapitalSafety 仍读 SIM ledger；
5. UI 无旧实盘模式卡片、无 REAL 自动账户按钮；
6. LIVE 开关仍全关：`probe.authorized=false`，`live_manual_arm.enabled=false`。

## 后续

探针成功后再实现 `/api/live-arm/*` 双臂控制台：
`status / authorize / arm / disarm`。该控制台只改 LIVE 臂状态，不改
`TRADE_ENV` 或 `AUTO_TRADE_ACCOUNT`。

---

## 实施记录

- 2026-10-04 01:01:45 CST 执行 `systemctl restart ftseek-us` 加载本批；
- 重启后服务 `active/running`，PID `3832796`；
- `startup_smoke_check.py --wait 90` 7/7 通过（01:02:08）；
- 重启窗口 warning/error 为 0；
- 定向契约 34 passed；风险 + LIVE 臂 + 退出补偿 232 passed；
  Dashboard 全量 141 passed；`py_compile`、Jinja parse、`node --check`、
  内联 JS `node --check`、`git diff --check` 均通过；
- `cutover_preflight.py --to live --no-write` 维持 `block=1 warn=4`，
  唯一 BLOCK 仍为毕业门禁；本批只退役旧全局入口，不改变该门禁；
- 当前安全状态：`TRADE_ENV=1`、`AUTO_TRADE_ACCOUNT=sim`、
  `ALLOW_REAL_TRADING=false`、`live_manual_arm.enabled=false`、
  `probe.authorized=false`，`live_arm_halt.json` 不存在。

---

## 复查补记（2026-10-04 19:05 CST）

独立复查结论：**通过，可保留上线**。

| 复核面 | 结果 |
|---|---|
| 定向契约 | 42 passed（startup_mode + ACB pnl + arm risk + atomic switch） |
| risk + dashboard 全量 | 337 passed |
| 静态 | py_compile / Jinja parse / node --check（外置+内联）/ git diff --check 全过 |
| 行为实测 | `live_*` 与 `set_auto_account("real")` → 410，无文件/重启/备份副作用；健康态 `simulate` no-op |
| SIM 风险二线 | CapitalSafety→`ftseek_us.db`、ACB/OKR target_env=sim；`TRADE_ENV=0` 注入不漂 LIVE |
| 安全状态 | 四锁全关；live 库 `PLAYBOOK_LIVE` 0 行；sim 未平仓 0 |

### 16:03:57 重启入档（原记录缺失）

当前进程 PID `3883669`（16:03:56 起，NRestarts=0，ExecMainStatus=0），启动段
16:04:03 `StartupMode 模式一致: broker=sim db=sim`。18:56 补跑
`startup_smoke_check.py` **7/7 ✓**（"启动段早于本次重启"提示 = 反假阳性守卫按设计
工作，距重启 3h 无新段属预期）。该重启的发起者/原因未在 handoff 认领，已由
handoff「RFC-134 复查补记」补档；如属其他会话请补录。

### 修复通道边界（鸡生蛋，非缺陷）

若 `.env.us` 真漂移到 `AUTO_TRADE_ACCOUNT=real` 或 `TRADE_ENV=0`：

1. 启动守卫**拒启** ⇒ 主进程（含 dashboard/API）不可用 ⇒ `/api/mode/switch`、
   `/api/auto-trade/auto-account` 修复通道**结构上不可达**；
2. 拒启前 HIGH 推送先行发出（`main._validate_startup_mode_or_exit` 内
   `push_text_safe`）— 值班人不会盲等；
3. 实际修复途径：**手改 `.env.us` 三键**（`AUTO_TRADE_ACCOUNT=sim` /
   `TRADE_ENV=1` / `ALLOW_REAL_TRADING=false`），或
   `FTSEEK_ALLOW_LEGACY_GLOBAL_FLIP=1` 逃生门（仅显式测试/回滚，用完即撤）。

这是 fail-closed 设计的代价而非缺陷：API 修复通道只服务于"服务存活期内发生漂移、
尚未重启"的窗口；拒启态的修复以 env 文件为 SSOT。值班处置顺序写死为：
**收推送 → 查三键 → 手改回 SIM → 重启 → 冒烟**。

---

## 2026-10-05 LIVE 探针前 P0/P5 补丁

继续复核时确认两项启用前硬伤并已修复:

1. **单号语义碰撞**: `open_positions.sl_broker_order_id` 曾被写入入场单号，
   而 OCO/止损替换把它当真实 STOP 单号消费。
2. **STOP 收据 SQL 无效**: 更新仓位时引用了 `open_positions` 不存在的
   `source_type` 列，首次 LIVE STOP 记账会被误判失败并触发 `HALT_NEW_LIVE`。

修复后的语义:

- `entry_broker_order_id` 只保存入场单号；
- `sl_broker_order_id` 只保存真实 STOP 单号，新开仓时为空；
- STOP 收据只更新 `account_type='live'`、`owner_strategy='playbook_live'`、
  `owner='auto'` 的自动仓；
- 找不到对应 LIVE 自动仓时 fail-close，不静默丢收据。

验证: 定向测试 `55 passed, 1 skipped`；`py_compile` / `git diff --check` 通过；
`cutover_rehearse.py --to live` 副本彩排通过（阻塞性 DDL 残留 0、双臂资金基线可恢复）。

边界: 本补丁未重启、未修改生产库、未打开任何 LIVE 开关。`TradeStore` 启动迁移
会用同标的入场单号精确匹配来清理旧行；重启后需复核 P5 清零。

---

## 2026-10-05 LIVE 1 股工程探针授权

用户明确授权后，生产进入「全局 SIM 常驻 + 显式 LIVE 工程探针」状态：

- `ALLOW_REAL_TRADING=true`；
- `live_manual_arm.enabled=true`；
- `probe.authorized=true`；
- `authorization_id=AUTH-20261005-1SHARE-LONG-RFC132`；
- 全局 `TRADE_ENV=1`、`AUTO_TRADE_ACCOUNT=sim` 不变。

重启后启动段为 `broker=sim db=sim ALLOW_REAL_TRADING=True`。这不是旧全局 REAL
翻页；LIVE 只能通过显式臂、1 股、1 仓、LONG/RTH gate 执行。

验证记录:

- `startup_smoke_check.py --live-probe` 7/7 通过；
- LIVE gate `allowed=true`，HALT 不存在，LIVE 自动仓 0；
- preflight P5 清零，LIVE 库 `entry_broker_order_id` 已预迁移，切换日有效补列 0；
- SHORT 触发被 `LIVE_PROBE_DIRECTION_REJECTED` 拒绝；
- 未绕过 chase cap / VWAP 证伪门，未人工选择标的。
