# RFC-217：NO_TRADE 档 SHORT 直接验证实验（SIM-only）

- 日期：2026-11-25
- 状态：**已批准 · 部署中（收益假设待前向样本）**
- 用户裁决：「目前本身就是模拟交易 并没真实资金入场 我需要直接验证」（覆盖影子先行默认；治理闸交互确认经用户明确授权代答）
- 关联：`DEB-20261125-flat-all-empty-spin-and-deploy-ledger-reconciliation.md` §8（预注册全文）、RFC-216（VIX 连续计分，不动）、DEB-20261124 方向审计（SHORT n18 +$196.93 vs LONG n13 -$688.03）

## 1. 问题与范围

高 VIX 恐惧日 regime=NO_TRADE 对称禁新仓，但 DA 持续产出 SHORT 为主指令（11-25 实测 17 条中 15 SHORT），用户质疑「可做空情绪为何强禁」。现有证据缺口：no_trade 日放空的成本后样本≈0（历史 SHORT 正收益样本几乎全在 risk_off/cautious 档）。

本 RFC 单变量：**NO_TRADE 档仅对 SELL_SHORT 开闸（SIM-only）**，直接攒前向样本验证成本后期望。

- 不改 LONG 权限（NO_TRADE 下 LONG 双层拦截：方向滤 `NO_TRADE_EXP_LONG_BLOCKED` + sizing 乘数 0.0）；
- SHORT 仓位沿用 risk_off 同款乘数 **0.3**，不新造参数；
- 触发/reprice/EntryConfirm/chase_cap/VWAP/费用地板/借券/日亏三档/SIM 5% 峰值预算/逐仓止损全部原样；
- 不改 RFC-216 连续计分、regime 分档阈值、VIX 源；
- LIVE-capable 环境代码闸恒不生效（`is_live_capable_env()`），四锁不动。

## 2. 实施

- 配置键 `playbook.execution_parameters.no_trade_short_sim_experiment_enabled`（默认 false；回滚=改 false，热载即恢复 NO_TRADE 全禁，已开仓位由原退出链管理）
- `risk_gates.no_trade_short_sim_experiment_active()`：LIVE 闸 + 配置读取失败 fail-close 回旧门
- `risk_gates.no_trade_short_regime_mult(direction, exp_active)`：SHORT∧active→0.3，否则 0.0
- preflight NO_TRADE 分支：active 时放行本轮（日志明示 SIM-only 实验），否则原拦截
- `auto_execute_flow`：轮级 `_nt_short_exp`（异常 fail-close）+ 循环方向滤 + sizing 乘数 + cohort 标记

## 3. cohort 与判据（预注册，详 DEB §8）

- 标记：指令 metadata `no_trade_short_exp`（vix/score/at），经 entry_booking_outbox 关联 trade；**不与 RFC-216 risk_off 子 cohort、qwen post 混算**
- 度量：净 bps/笔（fees_est 后）、死单率、退出原因分布、MFE/MAE
- 结算：n≥30 或激活后 10 个交易日先到者；「可交易」=净 bps>0；n≥20 显著为负可提前裁停
- 硬停：SIM 5%/日亏/止损任一触发按原语义停新仓，实验不豁免；到期结算入注册表

## 4. 验证

- 契约测试 `tests/playbook/test_no_trade_short_sim_experiment.py` 11 例 + preflight/open_floor 既有 = 54 passed；regime/f5_shadow/mom_flow/pipeline 回归 134 passed（正常 conftest）
- 激活日（11-25）恰为 NO_TRADE 日，可即时自然验证：SHORT 进触发评估、LONG 被拦、零 Traceback
- 未验证：收益假设本身；LIVE 路径仅单测级（生产 LIVE 冻结不可能自然触达）

## 6. 2026-11-26 保真修正：LONG 首层拦截按轮暂缓（本地已改，未重启）

§1「不改 LONG 权限」的执行口径修正为：NO_TRADE 轮 LONG/BUY **本轮**不进评估，指令保持 PENDING，不写 `SKIP`/`skip_reason`；留痕走 `exec_gate_audit`（`gate=no_trade_short_exp_long`, `transient=True`）。sizing 第二层（非 SHORT 乘数 0.0）、SHORT 0.3、SIM-only/LIVE fail-close、cohort 与预注册判据全部不变。

原因、数据与否决项见 DEB-20261125 §11。本修正不扩张实验、不改收益假设；已上线进程在重启前仍按旧写法（当日终态）。
