# 回放/回测脚本台账 (REPLAY-SCRIPT-LEDGER)

> 2026-08-30 建 (方法论收敛①)。解决问题: 29 个回放脚本散落, 复用靠手工, 活性无人知。
> 官方引擎: `scripts/exit_replay_backtest.py` — 公共原语:
> `load_trades` / `fetch_bars`(带缓存) / `collect_bars` / `replay_trade` / `_fee_of` / `aggregate` / `_parse_et`
> **公约**: 新写回放脚本必须 `import exit_replay_backtest` 取原语, 只写差异逻辑;
> 禁止再手搓取 bar/缓存/费用/交易加载。违者同"控制环路未入账"处理。

## 状态定义

- **活体**: 挂 cron 或被影子注册表引用, 持续产出
- **证据冻结**: 被已裁决 DEB 引用为证据基线 — **不可改不可删** (改了=裁决记录失真)
- **候选退役**: 零 DEB 引用 + 无日志活性, 删除前需人工确认内容无未落档价值

## 台账

| 脚本 | 行数 | 状态 | 归属/证据 |
|---|---|---|---|
| exit_replay_backtest.py | 701 | **活体·官方引擎** | 10 份 DEB 引用的回放基线 |
| limit_entry_shadow_replay.py | 267 | **活体** | cron 挂载 + 注册表 eval + 近30天有日志 |
| phase1_gate_backtest.py | 267 | 候选退役 | 原影子注册表 p1_3_vol_p50 eval; 2026-09-01 p1_3 裁杀 (5天3样本, 用户裁) 后零活体引用, 删除前需人工确认内容无未落档价值 |
| entry_gate_replay.py | 215 | 证据冻结 | 3 份 DEB |
| gate_a_mfe_replay.py | 426 | 证据冻结 | 2 份 DEB (复用官方引擎×3) |
| overnight_loser_multiday_replay.py | 238 | 证据冻结 | 2 份 DEB |
| e2_momentum_confirm_replay.py | 204 | 证据冻结 | 2 份 DEB |
| entry_band_replay.py | 351 | 证据冻结 | 1 份 DEB |
| 其余 19 个 (各 1 份 DEB 引用) | — | 证据冻结 | symbol_rule/vol_arm×2/holding_extension/overnight_loser/e2_swing/e1_preview/momentum_eod/mfe_stall/no_trigger/capital_flow/position_size/long_to_short_flip/loser_hold_path/cap_spectrum/admission/swing_hold/probe_pyramid/exit_replay_shadow/limit_entry_shadow(兼活体) |
| trend_gate_replay.py / overnight_1m_replay.py / minute_rules_replay.py / expectancy_ic_backtest.py / winner_trail_backtest.py / daily_r9r10_replay.py / ah_exit_replay.py | — | **候选退役** | 零 DEB 引用 + 无日志活性, 删除前需人工确认内容无未落档价值 |
| be_counterfactual_replay.py | 63 | 候选退役(特殊) | 零引用, 但系官方引擎复用范本 (MU案), 建议保留作模板 |
| cf_pullback_limit_replay.py | 218 | **活体** | DEB-20260901 Phase B/C: cron 47 5 * * 2-6 + 注册表 pullback_limit_shadow eval; 读官方引擎 bar 缓存格式 (replay_bars/, 只读零拉取) |
| gap_alignment_shadow.py | 170 | **活体** | DEB-20260901 P1/Phase C: cron 52 5 * * 2-6 + 注册表 gap_alignment; 影子落盘非回放 (无 bar 拉取), 归口本台账防散落 |
| archetype_bars_ensure.py | 94 | **活体** | DEB-20260901 Phase B 补盲: cron 35 5 * * 2-6; `import exit_replay_backtest.fetch_bars` 复用 (公约合规), 工具类非回放 |
| da_blocked_signal_cf_eval.py | 240 | **活体** | DEB-20260901 Phase C 观察点③ (B14): cron 15 6 * * 2-6 + 注册表 da_blocked_cf; 日K缓存 CF 评估族 (同 gate_counterfactual_ruler 口径, 只读零拉取) |

## 归口执行记录

- 2026-08-30: 台账建立 + 官方引擎公约写入。实测复用率 4/29 → 目标: 新增脚本 100% 复用。
- 2026-09-01: DEB-20260901 Phase C 补登记 4 脚本 (前三个 8/31-9/1 上线时漏登记, 本轮勘误入册)。日K缓存 CF 评估族 (gate_counterfactual_ruler / da_blocked_signal_cf_eval / t5_hold_counterfactual) 均只读 replay_bars_daily 缓存、零网络拉取, 与官方引擎 1 分钟回放分工明确, 不视为公约违例。
- 2026-09-01: p1_3_vol_p50 用户裁杀 → phase1_gate_backtest.py 转候选退役 (零活体引用; 脚本保留备查, 其历史读数未被他档引用为证据基线)。
- 不做的事: 不重写证据冻结脚本 (可复现性 > 整洁); 不建回放框架抽象层 (脚本即一次性证据, 框架化是过度设计)。
