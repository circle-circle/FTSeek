# RFC-130: SIM/LIVE 双臂策略一致性对齐 + LIVE 手动臂启用开关

> **状态**: ✅ 最小范围已实施 (2026-10-03, 用户裁「我认同双臂常驻，想让sim和实盘一致的策略，同时配置实盘启用开关」; 复查补充见 §7; L1/L2/L4 延续见 RFC-131)
> **日期**: 2026-10-03
> **作者**: Hermes
> **类型**: L2 (策略语义对齐, 零当前行为变更 — 前提实测见 §2)
> **承接**: `RFC-090-live-cutover-switch-surface-convergence.md` §17 (Codex 双臂常驻设计, WIP);
> `RFC-061-profitable-overnight-hold.md` (cohort B 持夜, 本 RFC 退役其 LIVE env 门);
> DEB-20260815 (W_WEEK 豁免起源)。
> **红线**: C2「打标前严格不切实盘」不变。本 RFC 的 arm 开关是**技术门**, 不是切换授权 —
> 首次真实 LIVE 执行仍需毕业门禁通过或用户 DEB 显式「知情破例 + 签字」。

---

## 1. 缘起

2026-10-03 空跑审计批发现: RFC-090 §17 双臂架构声称 LIVE 臂「同一套规则引擎」,
但引擎内实测存在 **2 处 SIM-only 策略分叉** (另有 3 处 LIVE 特有执行安全层, 不属策略):

| # | 分叉点 | SIM 臂 | LIVE 臂 (改前) | 分类 |
|---|---|---|---|---|
| 1 | `w_week_loser_exempt` (`overnight_decision.py`, `source_type != "PLAYBOOK_SIM" → False`) | 水下仓豁免隔夜 (≤5 交易日, DEAD_SIGNAL/TIME_EXIT/MAX_HOLD/EOD 四处全豁免) | **不豁免** — 水下仓被四条规则照常砍 | **策略语义 → 对齐** |
| 2 | `cohort_b_skip_allowed` (`trade_store.py`, LIVE 需 env `RFC061_LIVE_OVERNIGHT_ENABLED=1`, 默认 False) | cohort B 盈利仓可跳过 EOD 持夜 | **不可** — EOD 平 | **策略语义 → 对齐** |
| 3 | broker STOP (`dual_broker.py:497`) | sim 券商不支持 STOP, 规则引擎兜底 | 真挂 broker STOP 单 | 执行安全层 → **保留 LIVE 特有** |
| 4 | `pre_trade_gate.py:111` / `pdt_guard.py:224` | 无 | LIVE 额外前置门 + PDT 守卫 | 执行安全层 → **保留** |
| 5 | `EXT_REAL_AUTHORIZED` (`trading_mode.py:338`) | ETH 出场链启用 | 不启用 (RTH-only) | 授权层 (RFC-126 独立授权) → **保留** |

分叉后果: 用户 8/16 四原则「耐心不硬扛、留翻转空间」在 SIM 靠 W_WEEK 豁免实现,
LIVE 臂默认不复现 ⇒ **SIM 成绩不能外推到 LIVE** (W_WEEK 修复收益 +$629/修复率 73%
与 cohort B 持夜收益在 LIVE 不存在), 且 LIVE 会更早砍水下仓。

用户裁决: **策略一致** — 对齐 #1 #2 (策略语义层); #3-#5 是 LIVE 额外保护, 对齐 = 拆保护, 不动。

## 2. 零当前行为变更前提 (实测, 2026-10-03)

- live 库 (`ftseek_us_live.db`) `trade_records`: 仅 MANUAL 12 / POSITION_SYNC 2, **auto 臂 0 行**;
- live 库 `open_positions`: 8 行全部 `size=0` (manual 僵尸行), **auto size>0 = 0**;
- ⇒ **LIVE auto 臂从未运行**。#1 #2 的对齐只影响未来 armed 的 LIVE 臂; SIM 侧判定路径逐字不变
  (SIM 在白名单内, 语义同旧 `!= "PLAYBOOK_SIM"` 的 SIM 分支)。
- 三态实测: `TRADE_ENV=1 / AUTO_TRADE_ACCOUNT=sim / ALLOW_REAL_TRADING=false` 一致 (preflight P0 ✅)。

## 3. 变更集

### 3.1 策略对齐 (删 SIM-only 门)

| 文件 | 改动 |
|---|---|
| `app/playbook/overnight_decision.py` | `w_week_loser_exempt` source 门 `!= "PLAYBOOK_SIM"` → `not in ("PLAYBOOK_SIM", "PLAYBOOK_LIVE")` (双臂白名单; SHADOW/MAIN_PIPELINE/ORB 仍不豁免) |
| `app/storage/trade_store.py` | **删除** `live_overnight_enabled()` + `cohort_b_skip_allowed()` (对齐后恒 True 死门; env `RFC061_LIVE_OVERNIGHT_ENABLED` 随之退役) |
| `app/playbook/exit_engine.py` | `cohort_b_skip_eligible` 公式 `cohort=="B" and cohort_b_skip_allowed(src)` → `cohort=="B"` (签名保留); 两处「仅 PLAYBOOK_SIM」过期注释同步 |
| `app/risk/exit_rules/pre_close.py` | 三处 `and cohort_b_skip_allowed(src)` 恒真条件删除 + import 删除 |
| `app/playbook/pipeline.py` | 两处过期注释同步 (无代码改动) |

**净效果**: cohort B / W_WEEK 的隔夜语义不再按 source_type 分叉; `source_type` 参数保留
(调用方矩阵/契约测试签名不变), 不再参与判定。

### 3.2 LIVE 手动臂启用开关 (用户裁「配置实盘启用开关」)

键路径沿用 RFC-090 §17.3.1 设计 (**不另造**), `config/unified_config.yaml`:

```yaml
playbook:
  live_manual_arm:
    enabled: false      # true = LIVE 臂可入场; false = 只停新仓, 不停出场
    max_open_positions: 1   # V1 首期占位, 消费方接线在 RFC-090 L4 批
    max_order_qty: 1
    session: RTH
```

消费方: `app/playbook/playbook_service.py` `_execute_via_dual_broker_inner`,
`target_env=real` 分支总前置门:

- `enabled=false` (默认): 共享门返回 **HALT_NEW_LIVE**, 不调用 broker、不降级 SIM;
- `enabled=true` 只是必要条件之一; 仍须通过 `evaluate_live_arm_gate` 的
  mode/auth/env/account/lane/LONG/BUY/1 股/1 仓/RTH/open-cap 全量校验, 任一项失败均 fail-close;
- **出场不受本门管**: disarm 只停新仓, 已有 LIVE 仓继续由出场监控管理 (RFC-090 §17.5 LIVE_DRAIN 语义);
- **fail-closed**: 读配置异常 → 视为未 armed, 拒绝。

当前落地关系: `evaluate_live_arm_gate` 是 service 权威门与 pipeline 镜像前置门共享的
单一判定面; `route_playbook_db`、双库退出、镜像与限额接线已由 RFC-131/DEB-20261003
收口。后续扩展不得另造平行门或先镜像后校验。

### 3.3 契约测试翻面 + 墓碑 (4 文件)

| 文件 | 改动 |
|---|---|
| `tests/playbook/test_exit_engine_params.py` | 重写: ①矩阵改钉 `== (cohort=="B")` 双臂同语义; ④新增退役墓碑 (`trade_store` 不得再生两函数 / 全仓代码面不得再读 env 门 / W_WEEK 双臂白名单直调本体 / AST 扫退出链无残留调用); ②③⑤ 逐字保留 |
| `tests/playbook/test_overnight_decision_contract.py` | `test_w_week_sim_only` → `test_w_week_dual_arm` (LIVE 豁免 + SHADOW 仍不豁免) |
| `tests/playbook/test_w_week_depth_cap_contract.py` | `test_live_not_exempt` → `test_live_same_as_sim` + `test_shadow_source_still_not_exempt` |
| `tests/playbook/test_w_week_dead_signal_contract.py` | `test_live_underwater_dead_signal_fires` → `test_live_underwater_overnight_exempt_same_as_sim` (LIVE 水下隔夜豁免 DEAD_SIGNAL, 与 SIM 同) |
| `tests/playbook/test_m2_c2_live_quote_gate.py` | `_live_prereqs` 增 `armed=True` 参数 (arm 门是 live 新仓新前置, helper 构造 live 前置须一并构造) |
| `tests/playbook/test_exit_compensation.py` | fixture config 增 `live_manual_arm.enabled=True` (real 参数化用例走 armed 态, 补偿路径测「入场已提交后簿记失败」语义) |

## 4. 验证 (2026-10-03 实测)

1. **等基对比**: worktree@HEAD 基线 `tests/playbook/` 1 failed (环境性, 与本批无关) vs 改后
   **953 passed 全绿**; 中途 13 个新增失败逐个归因 = 三类直接语义后果 (旧分叉断言 ×1 /
   arm 门新前置 ×12), 全部按「翻面 + 显式 armed」修, 无绕过。
2. **arm 门端到端** (哑 broker + monkeypatch, 不真下单):
   - `target_env=real` + 未 armed ⇒ `{"success": False, "message": "[LIVE_ARM] … HALT_NEW_LIVE"}`, **broker 触达 0 次** ✅;
   - `target_env=sim` + 未 armed ⇒ 不受 arm 门影响 (门只在 `_is_live` 分支) ✅。
3. **config 键运行进程视角**: `get_dotted("playbook.live_manual_arm")` 实读返回
   `{enabled: False, …}` ✅ (非装饰键, 消费方接线同批)。
4. **退役墓碑**: `trade_store` 无 `cohort_b_skip_allowed`/`live_overnight_enabled`;
   全仓 `app/scripts/ops` 代码面无 `RFC061_LIVE_OVERNIGHT_ENABLED` 读取 (注释留痕允许)。

## 5. 影响面与边界

- **SIM 行为**: 逐字不变 (白名单含 SIM; cohort 公式净效果同旧)。
- **LIVE 行为**: 当前零影响 (auto 臂从未运行); 未来 armed 后 W_WEEK/cohort B 与 SIM 同语义 —
  这正是「SIM 成绩可外推 LIVE」的前提。
- **明确不做**: ①不动 #3-#5 执行安全层 (broker STOP/PDT/购买力/EXT 授权);
  ②不实施 RFC-090 L1-L6 其余批次 (lane 路由/双库出场扫描/两步开火仪式/限额接线);
  ③不翻任何现有开关 (`ALLOW_REAL_TRADING` 仍 false, `TRADE_ENV` 仍 1);
  ④不动毕业门禁 (preflight block=1 `graduated=False ic_t3=-0.0403` 仍是切换硬阻塞)。
- **回滚**: revert 本批 commit (策略分叉恢复 SIM-only, arm 门移除); config 键删除无副作用
  (消费方同批回滚)。回滚即恢复「SIM 成绩不可外推 LIVE」状态。

## 6. 预注册 (arm 开火前必须满足, 裁决后冻结)

```text
live_manual_arm.enabled=true 的前置 (缺一不可):
  ① preflight block=0 (毕业门禁 graduated=true, 或用户 DEB 显式知情破例+签字);
  ② ALLOW_REAL_TRADING=true + REAL_ACCOUNT_ID + unlock (预授权, 重启生效);
  ③ live capital_state 已显式预置基线行 (RFC-090 §17.4.1, 防首次同步归零回撤保护);
  ④ live open_positions 8 条 size=0 僵尸行已校正 (live_ledger_sync 拉券商真值);
  ⑤ broker STOP 挂/改/撤三段真钱极小仓演练 ≥1 次 (preflight P6: 生产里程现为 0);
  ⑥ RFC-090 L1-L4 lane 路由落地 (route_playbook_db + 双库出场扫描 + instruction 镜像),
     或用户显式裁「全局 TRADE_ENV=0 旧模型」并知悉 §17.2 四条反对理由。
```

---

## 7. 2026-10-03 复查补充 (Codex, 零开关改动)

### 7.1 已确认

1. **部署状态**: commit `175c3b359` 后服务已于 19:29:25 CST 重启, 当前 `active`;
   启动冒烟 7/7 通过 (19:29:30 启动段)。
2. **当前模式**: `TRADE_ENV=1` / `AUTO_TRADE_ACCOUNT=sim` / `ALLOW_REAL_TRADING=false`,
   `live_manual_arm.enabled=false`; 生产仍全 SIM, 未启用 LIVE。
3. **定向测试**: RFC-130 相关 6 个测试文件 `115 passed`; 切换/DB 契约另有 `46 passed`。
4. **策略对齐**: W_WEEK 豁免与 cohort B 持夜已扩展到 `PLAYBOOK_LIVE`, 旧
   `RFC061_LIVE_OVERNIGHT_ENABLED` 读取面清零。

### 7.2 复查发现并已补

旧实现曾把 arm 门放在半翻互锁与毕业门禁之后。若 `TRADE_ENV=1` 且有人请求
`target_env=real`, 请求会先被旧互锁降级为 SIM, **到达不了 arm 门**, 与本文
「enabled=false ⇒ HALT_NEW_LIVE, 不降级 sim」的语义相反。

已补:

- `app/playbook/playbook_service.py`: 权威门改为调用共享 `evaluate_live_arm_gate`;
- `app/playbook/pipeline.py`: 写 LIVE 镜像前重新调用共享门, 拒绝时不写 LIVE instruction;
- `tests/playbook/test_m2_c2_live_quote_gate.py`: 新增
  `test_disarmed_live_halt_precedes_half_flip_downgrade`, 断言未 armed 时
  broker 触达 0 次、不降级 SIM、instruction 未认领。

### 7.3 启用前仍缺 (不可把本批误读为双臂切换已完成)

1. **L1/L2 已由 RFC-131 最小落地**: `route_playbook_db(target_env)` 与双库
   退出扫描已实施; 但 preflight 的 schema/capital/manual-position 风险未清零。
2. **L4 已由 RFC-131 最小落地**: LIVE instruction 镜像、同一 `decision_id`、
   explicit `target_env="real"` 与 1 股/1 仓接线已实施。
3. **限额键已接线**: `max_open_positions=1` / `max_order_qty=1` / `session=RTH`
   均由共享 LIVE gate 判定; service 权威门与 pipeline 镜像前置门同口径。
4. **预检仍 BLOCK**: `block=1 warn=6`; 毕业门禁 `graduated=false`,
   `ic_t3=-0.0403`。策略转正仍不得启用; 1 股工程探针须走 RFC-131 独立授权。
5. **旧模型风险仍在**: 若下周直接翻 `AUTO_TRADE_ACCOUNT=real + TRADE_ENV=0`,
   仍是旧单库翻转, SIM 研究臂会停, 不是本 RFC/§17 目标架构。

### 7.4 下周决策板

| 决策 | 建议默认 | 说明 |
|---|---|---|
| 是否启用 `live_manual_arm.enabled=true` | **暂不启用** | preflight 仍 BLOCK, 且工程探针需单独授权 |
| 是否先跑旧模型全局翻转 | **不建议** | 会停 SIM, 与「SIM 继续运行」冲突 |
| 是否接受知情破例先做 1 股链路 | 用户裁决 | 走 RFC-131 `probe.authorization_id`, 且仅限 1 股 LONG |
| 是否先做只读/哑 broker 彩排 | **建议** | 可在零真钱下验证 arm 门与路由缺口 |

---

## 8. 后续实施记录 (2026-10-03, RFC-131)

本文件的「最小范围」已被 RFC-131 延续为 1 股 C-lite 工程探针:

- `route_playbook_db("sim"|"real")` 分臂路由;
- SIM/LIVE 双库退出扫描与分臂组合兜底;
- LIVE instruction 镜像, 同 `decision_id`, 强制 1 股/1 仓/LONG;
- LIVE 失败一律 `HALT_NEW_LIVE`, 不再降级 SIM;
- `TRADE_ENV` 不再作为 LIVE lane 的降级依据。

启用仪式、SSOT 边界与剩余阻塞见
`docs/decisions/RFC-131-live-one-share-engineering-probe.md`。

注意: RFC-131 代码已完成并测试通过，但因当前 worktree 混有其他 WIP，
尚未重启部署到运行中的 ftseek-us 服务。
