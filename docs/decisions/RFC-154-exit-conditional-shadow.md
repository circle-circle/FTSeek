# RFC-154 出场条件化影子预注册（P0 审计完成；P1 仅影子，不改生产）

**状态**: P0 只读审计完成（2026-10-20）；P1 T 臂已实现并首跑，**fidelity 0.604 < 0.80 ⇒ 读数冻结、不提案**；其余臂未开工；**未改任何生产出场参数**
**类型**: 测量 + 影子预注册（P1 实施前须用户批准）
**关联**: DEB-20261009 §25–§27（赢小输大/左尾）、RFC-152（TCA）、`post_exit_path`、`be_grace_replay`、`breakeven_shadow_settle`
**边界**: 本 RFC 不触碰 `playbook_horizon.yaml` / exit hash；未改 BE、trail、TP、SL、时间退出任何生效值。

---

## 1. P0 只读审计（2026-10-20）

工具：`scripts/exit_conditional_audit.py`；产物：`data/shadow/exit_conditional_audit_latest.json`。
口径：648 笔 `post_exit_path`（2026-07-06→10-19）→ 626 笔有 bar、596 笔有 ATR；
**有效 trail 样本 575 笔**；止损类 51 笔单列。

> ⚠️ 测量陷阱（本次自曝）：`SL_INITIAL`/`OVERNIGHT_GAP_STOP`/`SL_FIXED_PCT` 的
> “出场后继续持有”不是有效反事实——实际路径先触发了止损，从止损点之后模拟 trail
> 是**止损后反转偏差**。首版未剔除时 trail_0.8 均值被抬到 **+32.6bps (t=5.69)**；
> 剔除后为 **+12.65bps (t=2.66)**，BE 单桶仅 +0.91bps。宽止损必须做入场→出场
> 全路径重放，不在本审计范围。

### 1.1 全有效样本（n=575；正=反事实比实际出场更好）

| 变体 | mean bps | median bps | 改善占比 | 日聚类 t | MDE80 |
|---|---:|---:|---:|---:|---:|
| hold_to_close | −10.44 | −1.40 | 49.0% | −1.00 | 23.90 |
| **trail 0.8%** | **+12.65** | **+0.51** | 51.3% | **2.66** | 10.13 |
| trail 1.2% | −0.01 | −19.43 | 40.9% | −0.65 | 12.28 |
| trail 1.6% | −3.11 | −15.39 | 43.5% | −0.64 | 15.63 |
| trail 2.0% | −4.88 | −10.59 | 45.4% | −0.44 | 17.87 |
| ATR 0.35× | −3.26 | −17.62 | 42.7% | −0.40 | 16.29 |
| ATR 0.50× | −8.97 | −9.14 | 45.5% | −0.93 | 20.43 |
| ATR 0.75× | −11.85 | −4.61 | 47.6% | −1.10 | 23.70 |
| ATR 1.00× | −13.52 | −4.61 | 47.5% | −1.27 | 24.60 |
| cond MFE≥1×ATR→0.75×ATR | +11.55 | +0.44 | 51.1% | 2.30 | 10.58 |

### 1.2 按出场原因（trail 0.8% 变体）

| exit_reason | n | mean bps | median bps | 日聚类 t | 备注 |
|---|---:|---:|---:|---:|---|
| `SL_BREAKEVEN` | 236 | **+0.91** | **−17.20** | −0.03 | 多数 BE 出场并不早；均值被尾部拉正 |
| `SL_TRAIL_LOCK` | 162 | **+9.01** | **+2.76** | 2.52 | 有效正向，但幅度小 |
| `TIME_EXIT` | 35 | +44.15 | +35.21 | 2.48 | 小样本，可能偏早 |
| `DEAD_SIGNAL_EXIT` | 43 | −2.71 | −15.73 | −1.15 | 维持 |
| `PRE_CLOSE_EXIT` | 50 | +9.50 | +5.52 | 1.39 | 中性 |
| `MFE_STALL_EXIT` | 31 | +99.12 | +67.48 | 5.04 | **当前已 disabled（DEB-20260815）**，仅历史 |
| `SL_INITIAL` | 31 | (+236.57) | — | — | **止损后反转偏差，不作数** |
| `OVERNIGHT_GAP_STOP` | 11 | (+517.42) | — | — | 同上，不作数 |

### 1.3 结论

1. **不要全局“让赢家跑”**：hold-to-close 平均 −10.44bps；trail 1.2–2.0% 与 ATR 宽 trail 全部转负。
2. **BE（初盈利就跑）不是系统性太早**：BE 桶 mean +0.91bps、median **−17.20bps**；放宽到 0.8% trail 对多数 BE 出场反而更差，收益集中在少数尾部。
3. **唯一稳定的正向是 trail lock**：+9.01bps mean / +2.76 median / t=2.52；对应候选是把 `adaptive_trailing_min_drawdown_pct` 从 0.4% 抬到 0.8%（或直接用固定 0.8%），需全路径重放验证。
4. **时间退出** +44bps（n=35）值得作为探索臂，但样本小。
5. **左尾仍是最大杠杆**：平均亏 −$104.75；把平均亏损压 21% 就能把 R:R 拉回 0.29 的盈亏平衡线。止损类不能用本审计评估，需全路径重放。
6. **成本是第二杠杆**：平均赢家约 44bps，费+滑点约 46bps；“赢了也不赚钱”是结构性的，不是单纯跑早了。

## 2. P1 影子预注册（不改生产）

### 2.1 臂

| 臂 | 变体 | 依据 | 备注 |
|---|---|---|---|
| **T（主）** | `adaptive_trailing_min_drawdown_pct: 0.4 → 0.8`（或固定 0.8% trail） | trail lock +9.01bps / median +2.76 / t=2.52 | 最简单，先做全路径 replay |
| **B（次）** | BE 武装点 `0.4% → 1.0%`（或 1×ATR） | BE 桶 mean +0.91 但 median −17.20 | 尾部下注，必须报误杀 |
| **D（仓位）** | BE 时平 50%，其余继续 trail | `breakeven_shadow_settle` [D] 历史保守 +$3.44/笔，前瞻 n=0 | 需部分平仓执行改造；另立批 |
| **X（探索）** | 时间退出延长（顺势侧） | TIME_EXIT +44.15bps，n=35 | 小样本，只作描述性 |

**不测**：全局 trail 1.2–2.0%（负）、ATR 宽 trail（负）、hold-to-close（负）、2/3/4% 分批止盈（DEB-20261008 已否决）。

### 2.2 测量方法

- **入场→出场全路径重放**（1m bar），不是本 P0 的“出场后继续持有”；必须包含初始 SL、BE 武装、trail、时间退出与 EOD。
- 费用与滑点按 `fee_model.estimate_round_trip` + 30bps 往返滑点口径；主指标 = 扣费净 bps/笔。
- 统计：按日聚类 t + MDE80；n≥30/臂且 ≥10 ET 交易日；不足只报进度。
- 必须并报：`SL_INITIAL` 笔数、平均亏损、R:R、MFE 捕获率、**误杀**（被新规则提前出场的赢家）。
- 主判据（预注册）：臂 T 的扣费净 bps/笔较基线 **+≥10bps**，日聚类 **t≥2.0**，且 `SL_INITIAL` 不恶化、误杀净额 < 改善额；否则不采纳。
- 基线：2026-09-20→10-20 同人群（n=155；净 −10.32bps/笔），字段/口径与 `exit_scorecard` 一致。

### 2.3 边界

- 只读影子，不改 `playbook_horizon.yaml` / exit hash / 生产开关；不占 OKR C1 的**入场**假设名额（本项是出场策略）。
- 影子期不并发两个变化；一次一臂。
- 任何转生产须另立 RFC + 用户批准 + 新 exit 版本窗口；不得静默改 BE/trail。

## 2.4 T 臂首跑记录（2026-10-20，只读）

脚本：`scripts/exit_t_arm_shadow.py`；产物：`data/shadow/exit_t_arm_latest.json`；
注册表：`exit_t_arm_min_dd_08`（OBSERVING, `no_adjudication_reason=FIDELITY_GATE`）。

样本：139 笔 / 21 个 ET 日 / 0 笔缺 bar；baseline 与 T 臂都走 adaptive trailing 公式
（min DD 0.4 vs 0.8），双序各跑。

| 序 | baseline mean | T mean | delta mean | delta t | harm / save |
|---|---:|---:|---:|---:|---:|
| pess（主） | +9.82bps | +4.05bps | **−5.77bps** | −1.30 | 1139 / 0 bps |
| opt | +2.93bps | +2.97bps | +0.04bps | 0.44 | 805 / 0 bps |

**保真度门**：baseline vs 实盘 `reason_match_pct = 0.604 < 0.80`，按
`exit_replay_backtest` 的既定纪律（文档线 40）**冻结变体读数**。因此：

- 不采纳 T 臂；不改生产 `adaptive_trailing_min_drawdown_pct`。
- P0 的“出场后 trail +9bps”**不能**直接外推为全路径改进——全路径把持有期回吐算回来了；
  本次 T 臂在 pess 序 harm 1139bps、save 0，方向相反。
- 下一步先修回放保真度（锚定回放 / 10s 报价 / BE 限价成交 / fee-aware 垫），
  `reason_match ≥0.80` 后才允许读变体；在此之前 T/B/D/X 均不提案。

## 3. 变更记录

- 2026-10-20 v1：用户认可 P0 审计 + P1 预注册；P0 完成并产出
  `data/shadow/exit_conditional_audit_latest.json`；本批**零生产行为变更**。
- 2026-10-20 v2：用户确认开始 T 臂；实现 `exit_t_arm_shadow.py` + 注册表；
  首跑 fidelity 0.604<0.80 ⇒ 冻结、不提案；生产零改动。
