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

## 5. 2026-11-26 执行口径补充（Phase A 本地，非实验变更）

用户后续要求避免极端偏单向并按建议优化。本批仅披露来源与原各阶段时点，**不结案/扩张本实验**：NO_TRADE 的 LONG 双层拦截、SHORT 0.3、SIM-only/LIVE fail-close、原 cohort 与 n≥30或10交易日/提前裁停全部不变，未刷新本实验收益样本。

原 preflight、轮级实验态、逐指令 sizing 仍各自 assess；新增函数局部审计快照进既有 sizing_delta/reprice，不启用共享/reuse/enforce。regime_snapshot_used=False 仅表示没有新增显式共享，原120s/30s缓存仍可复用。generation_regime_text 保留原文，不能推算为生成快照；成交时 playbook_service 评估仍独立。预检早退、LONG 早拦、MOM 专用 reprice 等不在完整覆盖，缺键不等于放行或完整机会池。

**验证**：隔离构造 preflight=RISK_ON、实验态=NO_TRADE、sizing=RISK_OFF 的不同 context：BUY 原样早拦，SELL_SHORT 原3次调用、乘数0.3及执行尺寸保持，审计保留不同stage。原仓严格隔离10文件 **539 passed /43.93s /exit0**，受保护边界尝试为空，日志 `/tmp/regime-caliber-20261126-k1wfp6l1/final-integrated-v2.log`；完整范围/失败定位/缓存/只读基数消费限制见 DEB-20261122 §15。不是自然成交或双向净期望证明。

**状态/下一步**：本批未提交/推送/部署/重启；现役 RFC-217 部署状态不因本地附加审计而改变。后续经授权上线再观察覆盖；方向权限调整或 Phase B 另行评审，不以强制多空配比或放宽执行质量门制造样本。
## 6. 2026-11-26 保真修正：LONG 首层拦截按轮暂缓（已部署 14:57 CST）

§1「不改 LONG 权限」的执行口径修正为：NO_TRADE 轮 LONG/BUY **本轮**不进评估，指令保持 PENDING，不写 `SKIP`/`skip_reason`；留痕走 `exec_gate_audit`（`gate=no_trade_short_exp_long`, `transient=True`）。sizing 第二层（非 SHORT 乘数 0.0）、SHORT 0.3、SIM-only/LIVE fail-close、cohort 与预注册判据全部不变。

原因、数据与否决项见 DEB-20261125 §11。本修正不扩张实验、不改收益假设；已上线进程在重启前仍按旧写法（当日终态）。

**部署续记（2026-11-26 14:57 CST）**：用户明确授权重启及本地提交，commit `9532275dc`只提交本修正及配套测试/说明，Phase A与其他在途改动未提交、未推送。`systemctl restart ftseek-us`成功，PID55482；官方启动冒烟7/7 exit0，日志 `/tmp/ftseek-opt-20261126/startup-smoke.log`。BA/BABA/GS三仓与持久化stoploss状态前后不变，两出场账本/entry outbox均无在途。连续两轮NO_TRADE暂缓、第三轮RISK_OFF恢复的隔离行为用例通过；休市无自然执行证据，启动通过不等于盈利假设成立。详DEB§11.7，本续记随部署凭据批提交推送。

## 7. 2026-11-26 SIM-only 边界补修（本地，未重启/未部署）

§1/§2 的 LIVE-capable 闸口径补强：除 `is_live_capable_env()` 外，`no_trade_short_sim_experiment_active()` 复用 `evaluate_live_arm_gate(..., instruction_direction="SELL_SHORT")` 探测 live-arm real-target 镜像的结构能力；共享 LIVE 门可接受 SHORT 镜像或能力读取失败时，实验恒不生效。当前 `ALLOW=false`、probe=LONG，实验 active 与人群不变；cohort/n≥30 或 10 交易日/SHORT 0.3/LONG 拦截/硬停/四锁均不变。详 `docs/debates/DEB-20261126-correctness-evidence-and-reliability-fixes.md` §8。本批未提交/推送/重启/部署。

## 8. 2026-11-26 复核补记：§7 已部署；未来跨 regime 镜像边界仍待前置补口

§7 的“本地、未提交/未部署”保留为当时历史状态。实际代码提交 `29c4a757d` 已推送并于 22:53 CST 重启生效，部署文档提交为 `1e10c57e2`；服务 PID 109261，启动冒烟 7/7，详见 correctness DEB §9。

独立复核确认当前 `ALLOW=false`、共享 LIVE 门 LONG/BUY-only、实验 active=true，本批无需回滚。但 `_mirror_instruction_to_live_if_allowed` 不读取指令的 `no_trade_short_exp` 标记；实验指令若从 NO_TRADE 等待到 RISK_OFF 轮，未来在共享门与 live context 均允许 SHORT 时仍可能进入镜像路径。当前三道 LONG/ALLOW/context 边界使其不可达，不是现役 LIVE 暴露。任何未来 SHORT live-arm 授权/实现前，必须先补按指令身份的镜像排除与跨 regime 回归测试。证据与限制详 correctness DEB §10。
