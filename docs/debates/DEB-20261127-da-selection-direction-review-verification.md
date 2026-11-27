# DEB-20261127 · DA 标的与方向审查建议复核及 P0 正确性修复验证

状态：**P0 正确性修复已验证并部署 · 策略建议未实施**
创建：2026-11-27
代码提交：`7c5556ef2f48a3511e60434c94e5ff59a08bca85`
关联：`docs/debates/DEB-20260902-stale-bridge.md`、`docs/OKR-2026-11.md`、`docs/decisions/RFC-215-faithful-execution-axis-closure-and-sltp-preregistration.md`、`docs/decisions/RFC-217-no-trade-short-sim-direct-verification.md`

## 1. 背景与授权

用户要求复核另一 Agent 的 DA 优化建议，通俗解释变更逻辑，复查合理性与收益，并按建议执行优化、重启、部署、提交、推送到主线。

本轮只落地已隔离复现的 **P0 正确性修复**：bridge 启动刷新基线、OKR 评分费用/净额 fail-close、CI 文案读取真实上下界。不改方向配比、追价、隔夜强平、扩池、prompt、实验 cohort、live 权限。

另一 Agent 声称已落档 `docs/debates/DEB-20261127-da-selection-direction-review.md`，本地不存在；本文作为独立复核与部署凭据。另一 Agent 称本机无 `pytest` 不准确：`./venv/bin/python -m pytest` 为 8.3.5。

## 2. 通俗结论

- **bridge 缺口**：服务启动时如果已经加载今天的候选文件，也要记住“这是哪个版本”。否则同日 DA 重跑、文件更新后，内存可能继续用旧候选。修复后，启动加载和运行期重载使用同一版本基线；文件 mtime 变化才刷新，未变化不重复读盘。
- **评分缺口**：不知道费用或净额时，不能把费用当 0、把毛收益当净收益。修复后，缺失即不可测；日等权 CI 的说明必须读取真实上下界，不能把整体为负的区间写成“下界 >0”。
- **收益边界**：这些是“尺子和供给正确性”修复，不是 alpha。官方归档 DA 整窗净收益仍为负，修复不证明盈利改善。

## 3. 已实施 P0 修复

### 3.1 bridge 启动刷新基线

- `app/playbook/pipeline.py` 新增 `_record_bridge_loaded_baseline(bridge, mtime=None)`：仅对 exact `BridgeVotingAdapter` 且带 `_trade_date` 记录 `_bridge_reloaded_date/_mtime`。
- `_reload_bridge_if_landed` 成功路径复用该 helper。
- `app/core/scheduler_core.py` 启动加载前捕获 `_bridge_mtime_before`，加载并挂载成功后记录基线，避免已有当日文件在加载期间被重写时漏掉刷新。
- 契约测试：S8 启动基线后同日刷新、S9 mtime 不变不重读、S10 helper 忽略非 Bridge/缺日期、W4 scheduler 启动布线顺序。

### 3.2 OKR 评分 fail-close 与 CI 文案

- `scripts/okr_2026_11_scoring.py` 新增 `_row_net`：优先 `pnl_net`；否则仅当 gross/fee 均已知才算 gross-fee；否则 None。
- `window_stats` 增加 `gross_missing_n/fee_missing_n/net_missing_n`；缺失时 gross/fees/net/bps/net_per_trade/日聚类 fail-close。
- `kr1_verdict` CI 文案读取真实上下界，明确“日等权净 bps，非名义加权点估计”；保留正下界测试所需 substring `CI 下界 >0`。
- `_sum_net` 改 Optional；KR2 笔均净额、exit breakdown 对未知净额 fail-close。
- 额外修复：`exit_reason_breakdown` 原排序遇 `net=None` 会 TypeError；现 unknown 排最后。
- `collect_and_score` 对 DA fee/net 缺失加 KR5 warning。

## 4. 未实施建议与理由

- **P1 DA 原计划/执行/成交血缘披露**：方向合理，但属审计/展示契约，需先冻结真实快照并明确消费方；本轮不改执行语义。
- **P1 波动风险与涨跌方向分离**：合理研究口径，不新增无人消费字段，不改 regime 门。
- **P1 输入真实被消费**：资金面 code、短历史 UNKNOWN、空财报/失败呈现、来源时效等仍挂账；正式输入调整会改变信号源，须按 R2/预注册队列推进，不能包装成零行为小修。
- **P2 供给漏斗与交易经济性**：先定位未成交停在哪一层，核真实点差/报价连续性/借券/完整成本/共同暴露；不机械扩池、不抬价格地板、不放大票面、不恢复 UE 打单。
- **明确不做**：严格原价执行（RFC-215/C16 关闭）、50/50 方向配额、小样本砍 LONG/放大 SHORT、隔夜强平（C17）、恢复 UE 打单（C18）。RFC-217 SHORT 实验仍按原 cohort/停止判据；未来 SHORT live-arm 前需补跨 regime 实验标记隔离。

## 5. 收益与证据边界

官方归档 `/tmp/ftseek-audit-okr-20261125.json` 与修复后复跑 `/tmp/ftseek-audit-da-20261127-okr-after.json` 核心读数一致：

| 人群 | n | 毛 bps | 费 bps | 净 bps | 判定 |
|---|---:|---:|---:|---:|---|
| DA 整窗 | 146 | −15.189 | 9.6479 | **−24.8369** | NOT_MET |
| post ≥11-17 | 39 | −6.4763 | 10.9915 | **−17.4678** | UNDERPOWERED |

KR2 死单率 21.2329% MET。当前 DA cohort fee/net/gross missing 均 0，因此评分 fail-close 修复不改变当前官方读数。post 子窗胜率 82.05%、均赢约 $19.40、均亏约 $125.81，一次亏损约抵消 6.5 次盈利；高胜率不等于正期望。

bridge 缺口已隔离复现，但生产发生次数和收益影响未测量。正式 DA 推理在仓外，本文不评价 prompt/challenger 本体。

## 6. 验证实录

- 针对性：`tests/test_stale_bridge_guard.py tests/test_okr_2026_11_scoring.py tests/playbook/test_no_trade_short_sim_experiment.py` → **109 passed**，日志 `/tmp/ftseek-audit-da-20261127-targeted-after.log`。
- 正确性相邻：DEB-20261126 §5 主命令 → **229 passed / 7 deselected / 2 warnings**，日志 `/tmp/ftseek-audit-da-20261127-correctness.log`。
- 行为/LIVE 相邻：DEB-20261126 §7.5 八文件 + daily_loss/live_arm P0/waiver/admission/entry_data_bounded_fetch → **455 passed**，日志 `/tmp/ftseek-audit-da-20261127-behavior.log`。
- 修复后探针：bridge 同日刷新/幂等、评分缺费缺净不可测、负 CI 不误标、unknown exit breakdown 安全 → `/tmp/ftseek-audit-da-20261127-probes-after.log`。
- 静态：5 个 touched py `py_compile`、`scripts/check_undefined_names.py`、`git diff --check` exit0 → `/tmp/ftseek-audit-da-20261127-static.log`。
- 已知测试污染豁免：生产库 `data/ftseek_us.db` 在测试中被写 5 次（capital_state 等），非本批新增。

## 7. 部署凭据

- 重启前预检：`/tmp/ftseek-audit-da-20261127-pre-restart.json`。SIM 未平 BA SHORT 23 / BABA SHORT 47 / GS SHORT 5；LIVE 无仓；两套出场账本非 BOOKED intent / 非 TERMINAL leg 均 0；`git status --porcelain -- app/ scripts/` 为空。
- `systemctl restart ftseek-us` 后 MainPID `144451`，启动 `2026-11-27 10:08:45 CST`，active/running，NRestarts=0。
- 启动冒烟：`PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` → **7/7 exit0**，日志 `/tmp/ftseek-audit-da-20261127-startup-smoke.log`。
- 重启后复核：`/tmp/ftseek-audit-da-20261127-post-restart.json`。三笔 SIM 持仓 id/方向/数量/入价/入时不变，LIVE 仍无仓，出场账本仍 0；三处生产源码工作树 SHA256 与 HEAD blob 一致。
- 保留其他会话 WIP：`docs/debates/DEB-20261125-flat-all-empty-spin-and-deploy-ledger-reconciliation.md` 未提交。

## 8. 未验证与下一步

- 未跑全量 pytest/GitHub Actions；未做生产自然 bridge 同日重写命中统计、完整 tick/订单簿回放、收益显著性检验。
- bridge 版本基线仍基于 mtime，不是内容哈希；同 mtime 重写不保证检测。正式 DA 推理代码/prompt/模型参数仍需纳入可版本化发布清单。
- 下一步仅观察自然链路与官方评分；不人工造单、不清 UNKNOWN 锁、不重置峰值/累计止损、不调方向/阈值/live 权限。11-30 正式结算不延期。
