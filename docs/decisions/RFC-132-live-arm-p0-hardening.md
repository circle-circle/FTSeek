# RFC-132: LIVE 双臂 P0 风险硬隔离与切换前预置

> **状态**: 已实施并重启验证 (不构成 LIVE 启用授权)
> **日期**: 2026-10-03
> **作者**: Codex
> **审批人**: 项目所有者 (下周裁决是否启用)
> **承接**: RFC-130, RFC-131, DEB-20261003

---

## 摘要

修复 SIM/LIVE 双臂落地后仍存在的四个 P0 风险：LIVE 风控读取 SIM 资金与 SIM 台账、
手动实盘持仓守卫查错库、broker STOP 失败被静默当作成功、LIVE schema/资金基线未预置。
修复后 SIM 常驻不变，LIVE 只在显式 `target_env="real"` 且人工 arm 授权后运行；
本次未打开任何实盘开关。

---

## 问题陈述

| # | 风险 | 修复前行为 | 后果 |
|---|---|---|---|
| 1 | LIVE 资金/水位 | `get_capital_safety_system()` 跟随 `AUTO_TRADE_ACCOUNT=sim` | LIVE 用 SIM 净值、SIM 回撤与 SIM 峰值做风控 |
| 2 | LIVE 手动仓 | `ManualHoldRegistry` fallback 查 SIM 库且内存按 symbol 单键 | LIVE manual 仓不可见；同 symbol 双臂互相覆盖注册状态 |
| 3 | LIVE broker STOP | 返回值不检查，异常仅 warning | STOP 挂失败仍按成功入账，真钱仓位缺兜底止损 |
| 4 | LIVE schema/baseline | `capital_state=0` 行、8 条 size=0 manual 僵尸行、切换日首次 DDL | 回撤保护开局失明；manual 守卫永久误挡 |

实测 LIVE 账户资金快照（2026-10-03 23:48 CST）：

```text
total_assets = 73,057.35 USD
cash         = 15,597.77 USD
market_val   = 57,459.58 USD
buying_power = 31,423.59 USD
```

---

## 提议方案

### 方案概述

把「资金、熔断、手动守卫、台账、STOP 证据」全部改成显式 `target_env` 解析：

```text
SIM:  legacy singleton + data/ftseek_us.db      + PLAYBOOK_SIM
LIVE: per-arm instance + data/ftseek_us_live.db + PLAYBOOK_LIVE
```

### 详细设计

1. **资金 SSOT**
   - `get_capital_safety_system_for_arm("real")` 返回独立 LIVE 实例；
   - LIVE 固定读写 `ftseek_us_live.db` 的 `capital_state.trade_env=0`；
   - LIVE 执行前调用 `DualBrokerAdapter.get_account_info("real")` 刷新净值/现金/购买力；
   - 查询失败、净值为 0 或负数时 `LIVE_CAPITAL_FAIL_CLOSE`。

2. **风险门分臂**
   - AccountCircuitBreaker 按 `target_env` 读取对应台账和峰值；
   - AbsoluteCircuitBreaker 改为 SIM/LIVE 两个实例，启动重建分别只读
     `PLAYBOOK_SIM` / `PLAYBOOK_LIVE`；
   - 单标的日亏门显式传入 LIVE ledger 路径；
   - DualBroker 名义本金兜底按 `target_env` 取对应资金实例。

3. **手动实盘持仓**
   - `ManualHoldRegistry` 内部键改为 `(account, symbol)`；
   - `is_auto_blocked(symbol, target_env="real")` 查 LIVE 库；
   - SIM/LIVE 同 symbol 可各自保留注册状态，互不注销、互不阻塞；
   - LIVE manual 持仓只阻止 LIVE auto 新仓，不会被自动平仓。

4. **STOP fail-close**
   - LIVE STOP 返回必须同时满足 `success=true` 且 `futu_order_id` 非空；
   - STOP id 写入 `trade_records.metadata_json` 与 `open_positions.sl_broker_order_id`；
   - 失败/无 id/台账写失败时：`app.push` HIGH 通知、写入
     `data/live_arm_halt.json`、HALT_NEW_LIVE；
   - 既有仓位不平仓，由人工接管或退出监控继续处理。

5. **LIVE 预置脚本**
   - 新增 `scripts/prepare_live_probe.py`；
   - 默认 dry-run；`--apply` 先做 WAL-safe SQLite 备份；
   - 走 app 真实迁移入口，不复制 SQL 清单；
   - 清理 `owner=manual AND size<=0` 僵尸行；
   - 从 REAL 账户初始化 `capital_state` 基线；
   - 不修改 `.env.us` 与 unified_config 开关。

---

## 风险评估

### 最坏情况

券商资金查询失败或 LIVE 库写入失败。此时资金、账户熔断、水位与 STOP 验证均 fail-close，
拒绝 LIVE 新仓；SIM 不受影响。

### 缓解措施

- LIVE 资金刷新先于风险门执行；
- STOP 失败持久化 HALT 状态，重启后仍生效；
- 预置脚本先备份再写库，且幂等；
- preflight 只读，不因诊断本身写生产库。

---

## 验证计划

```bash
venv/bin/python -m pytest \
  tests/playbook/test_live_arm_p0_contracts.py \
  tests/risk/test_arm_risk_context.py -q

venv/bin/python scripts/prepare_live_probe.py --json /tmp/live_probe_prep_dry.json
venv/bin/python scripts/prepare_live_probe.py --apply --json /tmp/live_probe_prep_applied.json
venv/bin/python scripts/cutover_preflight.py --to live
venv/bin/python scripts/cutover_rehearse.py --to live
venv/bin/python scripts/startup_smoke_check.py --wait 90
```

---

## 回滚计划

```bash
git revert <this-commit>
```

数据回滚可使用
`data/backups/ftseek_us_live_before_probe_prep_20261003_234848.db`。
若 STOP 失败触发 HALT，恢复必须人工确认后修改/删除
`data/live_arm_halt.json`，不允许代码自动清除。

**回滚触发条件**: LIVE 资金分臂读取异常、SIM 常驻行为回归、或 STOP fail-close 误伤
SIM 执行。

---

## 实施记录

- 2026-10-03 23:48:48 CST 执行 `prepare_live_probe.py --apply`；
- 备份：`data/backups/ftseek_us_live_before_probe_prep_20261003_234848.db`；
- schema 迁移：trade_store / playbook_db / capital_safety / lessons / voting_predictions 全部 `ok`；
- 清理 8 条 LIVE `size=0` manual 僵尸行；
- LIVE capital baseline：`initial=current=peak=$73,057.35`，`trade_env=0`，`green/unlocked`；
- 幂等重跑：僵尸行删除 0，资金基线仍可刷新；
- `cutover_rehearse.py --to live`：DDL 干净、双向 capital roundtrip 无缝；
- `cutover_preflight.py --to live`：`block=1 warn=4`；唯一 BLOCK 为毕业门禁
  `graduated=false, ic_t3=-0.0403`。
- 2026-10-04 00:06:46 CST 执行 `systemctl restart ftseek-us`；重启前
  SIM/LIVE 未平仓/有效持仓均为 0，exit intents 仅 `BOOKED`。重启后服务
  `active/running`（PID 3826299），`startup_smoke_check.py --wait 90` 7/7
  通过，日志 warning/error 为 0。
- 2026-10-04 00:10:44 CST 追加执行 `systemctl restart ftseek-us`，加载
  STOP 失败推送语义修正（已成交但 STOP 保护失败 ≠ 未执行）。重启后服务
  `active/running`（PID 3826815），冒烟 7/7 通过（00:11:00），重启窗口
  warning/error 为 0。复跑 `cutover_preflight.py --to live --no-write`
  仍为 `block=1 warn=4`，唯一 BLOCK 为毕业门禁。

---

## 下周决策板

| 决策 | 当前建议 | 原因 |
|---|---|---|
| 是否启用 LIVE 1 股探针 | 用户裁决 | 技术硬隔离已补；策略毕业门禁仍未过 |
| 是否接受策略门禁知情破例 | 已接受 (2026-10-04) | 仅限 1 股 LONG 工程探针，不构成策略转正或启用授权 |
| 是否先做人工 STOP 三段演练 | 建议 | preflight P6 仍显示 broker STOP 生产里程 0 |
| 是否继续运行 SIM | 必须 | `AUTO_TRADE_ACCOUNT=sim` 不变，SIM 研究臂继续攒样 |
| 是否翻旧 `TRADE_ENV=0` | 禁止 | 会退回单库翻转模型，与双臂常驻目标冲突 |

2026-10-04 用户裁决「知情破例」：接受 `graduated=false / ic_t3=-0.0403`
作为工程探针破例，不改变 LIVE 当前全关状态。后续启用仍需单独填写
`probe.authorization_id`，并确认 `ALLOW_REAL_TRADING` 与
`live_manual_arm.enabled`。

**授权来源复核 (2026-10-04 09:32 CST, Hermes)**：上述「用户裁决」原仅以
`handoff.md` 转述为据，属 C2 红线事项（切实盘须预注册签字），第三方复查无法独立核实。
主会话已当面询问，用户明确回复「确认授权来自于我」⇒ 授权链闭合，本破例成立。

- 授权范围（与 §6.2 边界一致，未扩大）：仅 1 股 / 1 仓 / LONG / RTH 工程链路验证；
- 授权**不构成**：策略转正、LIVE 启用、SHORT 放行、手动仓自动平仓；
- 授权**不豁免**后续技术前置：`probe.authorization_id` 非空 + `ALLOW_REAL_TRADING`
  + `live_manual_arm.enabled` 三关仍须单独打开并经用户最终确认；
- 复核时实测全关状态（未被本次授权改动）：`TRADE_ENV=1`、`AUTO_TRADE_ACCOUNT=sim`、
  `ALLOW_REAL_TRADING=false`、`live_manual_arm.enabled=false`、`probe.authorized=false`。

启用前仍必须：提交/隔离本批、重启并跑冒烟、用户填写
`probe.authorization_id`、确认 `ALLOW_REAL_TRADING` 与 `live_manual_arm.enabled`。
