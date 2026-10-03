# RFC-131: LIVE 1 股工程探针与双臂路由落地

> **状态**: ✅ L1/L2/L4 代码已实施并通过测试 (2026-10-03; 默认零实盘行为; **尚未重启部署**)
> **日期**: 2026-10-03
> **承接**: RFC-090 §17 (双臂常驻), RFC-130 (策略语义一致 + manual arm)
> **用户裁决**: 先不接受策略转正破例; 先用 1 股跑通工程链路; SIM 继续运行

---

## 1. 定位: C-lite, 不是策略转正

本 RFC 只授权一种 LIVE 形态:

```text
同一 SIM 决策 → LIVE 镜像 instruction → LONG → 1 股 → RTH → LIVE 台账
```

它不是毕业门禁通过，也不是按 SIM 仓位复制策略实盘，更不是用 1 股盈亏外推策略期望。
C2 的策略转正红线保留。1 股探针需要单独的
`playbook.live_manual_arm.probe.authorization_id` 显式授权；该授权只覆盖工程验证，
不覆盖后续放大仓位。

## 2. 双臂链路

```text
同一策略/decision_id
 ├─ SIM:  PLAYBOOK_SIM  → futu_simulate → data/ftseek_us.db
 └─ LIVE: PLAYBOOK_LIVE → real broker   → data/ftseek_us_live.db
```

### 2.1 路由

- instruction: `route_playbook_db("sim" | "real")`
  - SIM → `ftseek_research` / `data/ftseek_us.db`
  - LIVE → `ftseek_us_live` / `data/ftseek_us_live.db`
- execution ledger: `get_trade_records_db_for_arm("sim" | "real")`
  - SIM 不随 `TRADE_ENV=0` 漂移；
  - LIVE 恒 `ftseek_us_live.db`。

### 2.2 出场

`monitor_playbook_exits` 分别打开 SIM/LIVE 两本台账（`monitor_playbook_sim_exits`
仅保留兼容别名）：

- SIM 库只扫 `PLAYBOOK_SIM`；
- LIVE 库只扫 `PLAYBOOK_LIVE`；
- broker 路由仍按行 `source_type` 判定 `target_env`；
- 组合兜底按臂分别聚合，SIM 浮亏不因 LIVE 敞口触发，反之亦然。

### 2.3 镜像

SIM 自动执行成功后，若 LIVE C-lite 全部前置满足：

1. 复制当日 instruction 到 LIVE 库；
2. 保留同一个 `decision_id`；
3. `metadata_json.dual_arm` 记录来源 instruction id；
4. 强制 `position_size=1`；
5. 以 `target_env="real"` 调用 LIVE service。

SIM 执行失败时本批不镜像 LIVE，避免同一工程故障在真钱臂重复。

## 3. 控制面

```yaml
playbook:
  live_manual_arm:
    enabled: false
    mode: engineering_probe
    max_open_positions: 1
    max_order_qty: 1
    session: RTH
    probe:
      authorized: false
      authorization_id: ""
      direction: LONG
```

LIVE 新仓必须同时满足：

1. `enabled=true`；
2. `mode=engineering_probe`；
3. `probe.authorized=true` 且 `authorization_id` 非空；
4. `ALLOW_REAL_TRADING=true`；
5. `REAL_ACCOUNT_ID` 已设置；
6. instruction DB 是 `ftseek_us_live`；
7. `max_order_qty=1` 且 `max_open_positions=1`；
8. `probe.direction=LONG` 且指令方向为 `BUY`；
9. LIVE 未平仓 `PLAYBOOK_LIVE` 数量 < 1；
10. 当前市场相位为 RTH TRADING（由共享 LIVE gate 检查；service 权威门与镜像前置门同口径）。

任何失败都是 `HALT_NEW_LIVE`，不降级 SIM。

## 4. SSOT 与互不污染

| 数据 | SIM 臂 | LIVE 臂 |
|---|---|---|
| 指令 | `ftseek_us.db` | `ftseek_us_live.db` |
| 成交/持仓 | `ftseek_us.db` | `ftseek_us_live.db` |
| 资金 | 模拟账户 | REAL 券商账户 |
| broker 真相 | simulate context | real context |
| 对照键 | `decision_id` | 同一 `decision_id` |
| 手动仓 | 不管理 | 计入账户总风险，不自动平仓 |

本地库不是资金 SSOT。券商是 LIVE 资金/持仓/订单终态 SSOT；本地库只保存执行过程、
策略状态和对账证据。账实不一致时：券商真值优先，停 LIVE 新仓，通知人工裁决。

## 5. 已实施

| 层 | 内容 |
|---|---|
| L1 | `route_playbook_db` + per-arm trade ledger path |
| L2 | 双库退出扫描 + per-arm portfolio backstop |
| L3 | RFC-130 manual arm fail-closed 门；DEB 收口后由共享 `evaluate_live_arm_gate` 单点判定 |
| L4 | LIVE instruction 镜像 + explicit `target_env` + 1 股/1 仓接线 |
| 观测 | LIVE 探针准备/失败走 `app.push` hub |

补充实施（DEB-20261003 路由收敛裁后）：

- `PlaybookDB()` 默认改为 `ftseek_research`，SIM 生成/执行不再依赖 `TRADE_ENV`；
  `FTSEEK_DB_FILE` A/B 覆盖语义保留。
- `app/playbook/live_arm_gate.py` 是 LIVE 探针唯一前置判定面，覆盖 arm/mode/auth、
  env/account/lane、LONG/BUY、1 股/1 仓、RTH、open cap 与计数失败 fail-close。
- pipeline 在写 LIVE 镜像前重新过门；open cap/RTH/方向/授权不满足时不写 LIVE instruction。
- 旧 `AUTO_TRADE_ACCOUNT=real` 无显式 `target_env=real` 路径已 fail-close 退役，
  不再降级 SIM。
- `monitor_playbook_sim_exits` 活跃名改为 `monitor_playbook_exits`，旧名保留兼容别名。

默认配置全部关闭，当前生产行为仍为 SIM-only。

**部署状态**: 2026-10-03 22:41:14 CST 已按用户指令执行
`systemctl restart ftseek-us`，加载 DEB 路由收敛批。重启前核对 SIM/LIVE 未平仓均为 0、
有效持仓均为 0、exit ledger 无非终态在途出场；重启后 `startup_smoke_check.py --wait 90`
7/7 通过，服务 `active/running`（PID 3817330），重启窗口零 warning/error。复跑
`cutover_preflight.py` 仍为
`block=1 warn=6`，且 LIVE 开关保持全关（`TRADE_ENV=1`、
`AUTO_TRADE_ACCOUNT=sim`、`ALLOW_REAL_TRADING=false`、
`live_manual_arm.enabled=false`、`probe.authorized=false`）。本次重启仅加载代码，
不构成 LIVE 启用授权；当前 worktree 仍混有其他 agent 的在途 WIP，启用前必须先
隔离/提交本批。

## 6. 启用前仍必须完成

1. `scripts/cutover_preflight.py` 技术项清零；当前仍 `block=1 warn=6`。
2. LIVE `capital_state` 显式预置，防回撤保护开局失明。
3. LIVE 8 条 `size=0` manual 僵尸行用券商真值校正。
4. schema 彩排清零切换日 DDL 面。
5. broker STOP 挂/改/撤生产里程 ≥1；当前为 0。
6. 手动持仓与 auto LIVE 的同 symbol 双向守卫复核。
7. 当前工作树混有其他 WIP；启用前必须先分离并提交本批。
8. 已完成：DEB 收口批已于 2026-10-03 22:41:14 CST 重启加载，冒烟 7/7 通过；
   后续任何路由/门控变更仍须执行同款重启纪律。

## 7. 1 股探针 runbook

### 7.1 盘前

1. 冻结代码，运行定向测试与 preflight；
2. 确认 `TRADE_ENV=1`、`AUTO_TRADE_ACCOUNT=sim` 不变；
3. 预授权 `ALLOW_REAL_TRADING=true` + `REAL_ACCOUNT_ID`，重启；
4. 填写 `probe.authorization_id` 并置 `probe.authorized=true`；
5. 用户最终确认后才置 `enabled=true`。

### 7.2 盘中

- 只接受自然产生的 BUY 信号，不造信号；
- SIM 正常执行后，先通过共享 LIVE gate，再镜像 1 股；open cap/RTH/方向/授权不满足时不写 LIVE 指令；
- LIVE 成交后立即验证 broker STOP 与台账；
- SHORT 信号本阶段跳过并记录原因。

### 7.3 停止线

以下情况必须 `enabled=false`（只停新仓，出场继续）：

- broker/local 订单或持仓不一致；
- LIVE 订单终态失败或长期未知；
- broker STOP 拒绝/失败；
- OpenD/行情链路导致 fail-close；
- 用户人工接管。

非安全性的 SIM/LIVE 差异只通知并记录，不自动平仓。

## 8. 验收

至少 5 个交易日窗口，不强行凑 5 笔。每日报告必须包含：

- `decision_id`、symbol、direction；
- SIM/LIVE planned/submit/fill price；
- slippage、fees、latency；
- broker order id / STOP id；
- LIVE open positions；
- broker vs local reconciliation；
- skipped reasons（含 SHORT skipped）；
- 异常与通知清单。

判定标准是「链路可解释且无人工修数」，不是 1 股盈亏。

## 9. 回滚

- 置 `enabled=false`：立即停 LIVE 新仓，已有仓继续由 LIVE_DRAIN 管理；
- 置 `probe.authorized=false`：撤销工程探针授权；
- `ALLOW_REAL_TRADING=false` + 重启：进程级 fail-close；
- 代码回滚本批：恢复旧全局路由，但不得在回滚态尝试双臂。
