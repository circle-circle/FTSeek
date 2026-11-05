# DEB-20261105 · SL_INITIAL / early-tight 触线 acceptance 与 120 秒探索

> 状态：**历史探索不支持上线；前瞻 record-only 已接线，现役交易判定不变**  
> 关联：RFC-116（peak 输入源不可全局切换）、DEB-20261105-entry-confirm-v2-mechanism-hardening、OKR-2026-11 §7  
> 边界：不改 120s entry_confirm、20% holdout、mid/grace、early-tight 参数、原始 SL 或任何下单/平仓行为。

## 1. 问题重述与根因边界

- 2026-10-01 起 12 笔 `SL_INITIAL` 中，11 笔 `_tight_sl_killed=true`。因此多数速死不是原始 `stop_loss` 直接造成，而是 early-tight 收紧后的首次触线退出。
- 2026-09-18 起有显式 `_tight_sl_killed` 的样本仅 16 笔，合计净 -$2,092.43；样本不足 30，不能据此估计稳定效应。
- early-tight 在开盘冷却结束后即可生效，而冷却按市场开盘时间计算；多笔仓位在 09:45 附近从冷却宽 SL 切到紧 SL。这是合理的机制审查对象，不等于已经证明应放宽。
- RFC-116 已证明不能无差别把 quote peak 换成 bar peak。本轮不修改 BE/trailing/peak 输入，也不把落库 MFE 大于 tick MFE 直接解释为可挽回利润。

## 2. 唯一候选机制

**early-tight 首次触线后不立即退出，给予固定 120 秒 acceptance 窗；原始 `stop_loss` 始终作为灾难硬兜底。**

- LONG：bar 收盘重新回到 early-tight 线上方 = acceptance 恢复；SHORT 镜像。
- 120 秒内未恢复 = 按当时可成交价退出；恢复也不等于最终盈利，必须继续追踪原始终 SL、后续退出和 EOD。
- 该设计属于成熟的 stop-touch / stop-acceptance 分离：减少单 tick 噪声退出，但用原始终 SL 限制等待造成的左尾。
- 本轮只采集和探索，**不在 SIM 实单启用该等待**。现役 entry_confirm 的 mid/grace 也不被复用为已证实有效的止损规则。

不并列实施 bar-MFE 豁免、冷却期改参、提前/取消 early-tight、VWAP 门或新的 15/30 分钟早退，避免一次多变量及违反 OKR-2026-11 不做清单。

## 3. 历史探索口径（运行前冻结）

人群：`PLAYBOOK_SIM ∧ exit_reason=SL_INITIAL ∧ metadata._tight_sl_killed=true ∧ 有有效 tight/原始 SL/当日 bar`。

固定参数，不做网格：

1. 以实际 early-tight 出场时间作为首次触线的可得代理；披露平仓延迟使其不等于真实首次触线时刻。
2. 取触线后前两根完整 1 分钟 bar 近似 120 秒；任一根收盘回线内 = 恢复。
3. 每根 bar 先检查原始终 SL。恢复与硬 SL 同根出现时分别报 pessimistic（硬 SL 先）/ optimistic（恢复先），不隐藏 bar 内顺序歧义。
4. 未恢复则在第二根 bar 收盘退出；恢复后继续追踪至原始终 SL 或 EOD。另列不含 BE/trailing 的简化反事实，不冒充完整生产退出链。
5. 费用沿用该笔 `fees_est`，两臂均为一趟往返；`delta_net = candidate_net - actual_net`，正数才代表改善。另报毛/费/净、bps、恢复率、延迟退出额外亏损和 EOD 结果。

历史样本仅用于判断“是否值得继续前瞻采集”；n<30 一律 `EXPLORATORY`，即使点估计为正也不得直接上线。

## 4. 前瞻采集（复用既有 penetration_shadow）

- 新增独立 `path=early_tight_initial`；既有保本/锁利路径与 B17 判分人群不混入本议题。
- 触线时记录：原始 SL、early-tight SL、last、bid/ask/spread/book source、tick/bar MFE、bar 覆盖时刻和持仓时长。
- 夜间回填 1/2/3/5/10 分钟的 wick recovery 与 close acceptance；2 分钟是主假设，其余仅描述，不做阈值选择。
- `penetration_llm_analysis` 继续只评原有保本/锁利 cohort，防止新增 SL_INITIAL 人群污染既有基率与 LLM 假设。

## 5. 从影子到 SIM A/B 的必要条件

必须同时满足：

1. 前瞻 early-tight 配对事件 **n≥30 且覆盖≥10 个 ET 交易日**；不足只能 `TOO_EARLY`，到期功效不足只能 `INCONCLUSIVE`。
2. 120 秒 candidate 相对实际立即退出的**全因扣费净 delta** 为正；日聚类 80% CI 下界>0，并报告 MDE80，禁止只报 recovery rate 或 SL_INITIAL 数量下降。
3. 前后半窗同号，剔除贡献最大 1 日后仍不为负；恢复后赢家收益不得被“延迟退出额外亏损”抵消。
4. 原始终 SL 触发、同根顺序不明、缺 bar、proxy book、跨公司行动/复权异常分别披露；高歧义样本不进入主判据。
5. 通过后才另立 RFC，提出 deterministic 20% 立即退出 holdout vs 80% acceptance 的 SIM A/B；A/B 自身仍需前瞻样本，不能把影子 PASS 当盈利证明。

任一经济判据失败则不提出 A/B；不得通过改成 60/90/180 秒、改恢复定义或挑选 archetype 继续阈值购物。

## 6. 当前动作与回滚

- 当前动作仅为 record-only 采集、回填字段和固定口径只读回放；交易行为变化为零，无需因行为上线。
- 影子异常不得阻断退出；生产触发仍按原逻辑立即平仓。
- 回滚为移除 early-tight 的 `record_penetration` 调用和新增只读字段；既有 penetration 历史文件不归并、不改写。

## 7. 结果

### 7.1 固定历史回放（2026-11-05 08:40）

复现：`PYTHONPATH=. venv/bin/python scripts/early_tight_acceptance_replay.py`；机器产物：`data/reports/early_tight_acceptance_replay_20261105.json`。

- 全部 16 笔均有可用 bar 且通过触发几何/可见性检查，覆盖 12 个 ET 日；pessimistic 与 optimistic 相同，同根硬 SL+恢复歧义为 0。
- **9/16 出现 120 秒 close acceptance**，但候选结局仅 6 笔到 EOD、3 笔恢复后触发原始终 SL、7 笔等待到期退出。
- 实际净 -$2,092.43；候选净 -$2,273.00；**delta -$180.57 / -332.97bps**。日等权均值 -$31.55，MDE80=$37.11，80%CI [-$68.65, +$5.56]。
- 前半窗 -$488.03，后半窗 +$307.46；剔除贡献最大的 2026-10-28 后 **-$441.93**。方向不稳定且依赖少数恢复大赢家。
- 典型反例：COIN 三笔恢复后仍触发原始终 SL，分别约 -$175.78/-$157.08/-$175.14；WING/ORCL 的恢复确实贡献大额正 delta，但不能代表整体。

**裁决：NO_DEPLOY / EXPLORATORY。** 120 秒恢复率不是盈利指标；当前固定候选没有可落地的净优势。简化回放不包含恢复后的完整 BE/trailing/DEAD 链，因此也不把它扩张解释为“所有 acceptance 设计必然无效”；它只否定本候选凭现有样本直接放宽 SIM 止损。

### 7.2 已落地的前瞻观测

- `exit_monitor_flow` 在真实 early-tight `SL_INITIAL` 触发时写 `path=early_tight_initial`，记录原始/紧 SL、last、L1 bid/ask/spread/book source、tick/bar MFE 与 bar 覆盖时刻；原退出仍立即执行。
- 复用既有 penetration 夜间回填，新增 2 分钟 wick/close recovery、2 分钟最劣延续/收盘位移和原始终 SL 命中。既有 `penetration_llm_analysis` 显式排除该 path，B17 cohort 不受污染。
- 报告新增 `forward_shadow`，只计部署后真实采集 trade_id；当前 **events=0 / labeled=0 / verdict=TOO_EARLY**，历史 16 笔不会凑入前瞻样本。
- 不新增 cron、推送或第 95 个影子；复用 `penetration_shadow` 注册项和既有 backfill 环。

### 7.3 验证与部署

- 最终聚焦及注册表套件 **47 passed**；退出/穿透/entry_confirm 等邻近回归 **249 passed**。
- 邻近 `test_control_loop_ledger` 另有 2 个失败：其他会话未登记的 Hermes P3/盘前 job 与 `wweek_peak_axis_shadow.py` cron；与本批 diff 无关，未代为修改。
- 08:45 按仓规重启前核 US.COIN/US.ALB 两笔未平仓且无在途出场；重启后启动冒烟 **7/7**，服务 active、启动段零 Traceback/CRITICAL/ERROR，持仓与 `120s/20%/mid/grace=3+2/FADE cap=false` 实读不变。

下一步仅按 §5 攒部署后独立样本；未达到 n≥30/≥10 日和经济门前，**不提出 SIM acceptance A/B，不改 early-tight**。
