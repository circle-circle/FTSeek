# DEB-20261111 · “赢小输大”结构诊断与分级修复单

> 状态：**诊断与测量实施完成；R2 独立前瞻已预注册，当前 TOO_EARLY；现役交易行为不变**
> 数据时点：2026-11-10 ET 已平仓、2026-11-11 10:16 CST 复核
> 口径：2026-11-01 起入场 cohort，`PLAYBOOK_SIM`、A9 排除为空、A10 `data_quality IN ('clean','real')`；费后净额优先 `pnl_net`。已平仓费用全部为 `fees_source='est'`，不是券商逐笔实收。

## 1. 先回答结论

“赢小输大”确实存在，但 **11 月 DA 当前不是净亏损，而是高胜率、负偏斜、尾损决定月度结果**。
第一优先级不是放宽止损或提前/推迟止盈，而是：

1. 把开盘 0–15 分钟、early-tight、原始 SL 三类尾损分开计量；
2. 复用 RFC-155 既有采集，对 **R2 开盘 0–15 分钟 0.5×尺寸**另起独立确认 cohort；
3. `entry_confirm`、early-tight acceptance、RFC-188 各按原判据攒样，不调参、不互相混样；
4. 费用只做第二变量，禁止靠抬票面摊费，禁止与 R2 同批上线。

当前不建议任何生产交易参数变更。R2 新确认通过后才另立 RFC，由用户决定是否 SIM 启用。

## 2. 当前经济结构

11 月已平仓 91 笔，另有 7 仓未平不混入已实现结果。DA=`playbook_sim`，EXT=`extended_hours_entry`。

| 来源 | n | 胜率 | 毛额 | 估算费 | 净额 | 净 bps | 笔均盈利 | 笔均亏损 | 盈亏平衡胜率 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| DA | 63 | 84.1% | +$519.67 | $265.13 | **+$254.54** | **+8.55** | +$17.98 | −$69.82 | **79.5%** |
| EXT | 28 | 89.3% | +$457.92 | $118.98 | **+$338.94** | **+26.80** | +$19.94 | −$53.20 | 72.7% |

DA 笔均亏损是笔均盈利的 **3.88×**，当前 84.1% 胜率只比盈亏平衡线高 4.6pp。
因此“继续提高小盈利胜率”不是目标；目标是降低少数大亏，同时保住当前总净额。

样本边界：DA n=63，低于 KR1 的 n≥120；+8.55bps 是当前点估计，不宣称 edge 已稳定转正。

## 3. 尾损到底在哪里

### 3.1 `SL_INITIAL` 是当前主出血点

- DA 7 笔 `SL_INITIAL` 净 **−$696.47**；其余 56 笔合计 **+$951.01**。
- 最差 3 笔贡献 DA 净亏损总额的 **62.1%**。
- 7 笔中 **6 笔发生在开盘 0–15 分钟**；7 笔中 **6 笔 `_tight_sl_killed=true`**；两者交集 5 笔。
- 唯一原始 SL 案例是 11-02 ORCL LONG：入场 124.615、原始 SL 118.59、出场 118.502，约 −4.906%。
- 6 笔 early-tight 实际亏损约 1.2%–2.4%，已经小于多笔原始 SL 的 4%–5% 风险距离；不能据此反推“关闭 early-tight 会更好”。

`SL_INITIAL` 同时覆盖原始线与 early-tight 是既有标签契约（`app/playbook/exit_engine.py:552`、
`app/playbook/exit_engine.py:628`），分析必须读取 `_tight_sl_killed`，不能把 7 笔都归成“原始止损太宽”。

### 3.2 开盘 0–15 分钟是当前最清晰的集中带

| DA 入场时段 | n | 胜率 | 净额 | `SL_INITIAL` | 笔均盈利 | 笔均亏损 |
|---|---:|---:|---:|---:|---:|---:|
| 09:35–09:45 | 35 | 77.1% | **−$210.53** | 6 | +$15.74 | −$79.44 |
| 09:45–10:30 | 20 | 95.0% | **+$392.89** | 0 | +$20.69 | −$0.22 |
| 10:30 后 | 8 | 87.5% | **+$72.18** | 1 | +$19.24 | −$62.50 |

这是当前 cohort 的方向性证据，不是直接禁入依据。硬跳过开盘段也会同时跳过 27 笔盈利单；
优先验证减半尺寸，而不是新造“09:45 前禁止交易”硬门。

### 3.3 历史隔夜大亏不能机械搬到当前月

历史 `SL_INITIAL + OVERNIGHT_GAP_STOP` 的尾部集中结论仍成立，但本 11 月 DA 已平仓 cohort
没有 `OVERNIGHT_GAP_STOP`。RFC-160 已按 n=12、ΣΔ=+$52.14、误杀 9>1 终裁
`GATE_NOT_WORTH / KILLED`；不得借本次问题复活同轴 EOD MFE 硬闸。

## 4. 为什么不应先动止盈、保本线或原始 SL

- 11 月 DA 31 笔 `SL_BREAKEVEN` 全部净盈利，合计 **+$299.32**，不是当前出血桶。
- 既有固定 TP 预注册所有候选阈值净 Δ 均为负，固定止盈会截断右尾。
- RFC-188 §10 的出场后漂移不支持把 BE 0.4 简单抬到 0.8；当前预期为负，继续 record-only 证伪。
- DEB-20261105 固定 120 秒 early-tight acceptance 历史回放净 Δ **−$180.57**，且半窗反号、剔最佳日为负；
  部署后真实事件仅 3 笔/3 日，远低于 n≥30/≥10 日，故保持 `NO_DEPLOY`。

所以不能用“赢家拿久一点、输家止损宽一点”这种对称直觉修负偏斜；它可能同时砍右尾、放左尾。

## 5. 分级修复方案

### P0 · 现在只做测量修正，零交易行为变化

在既有 `scripts/session_attribution.py` / 日报复用展示，不新增影子或推送：

- 每日并列 `avg_win / avg_loss / 盈亏平衡胜率 / 净expectancy / 费毛比`；
- `SL_INITIAL` 拆成原始 SL 与 `_tight_sl_killed=true`；
- 入场时段拆 0–15、15–60、60+ 分钟，并列笔数、净额、尾损、放弃赢家成本；
- DA/EXT、已实现/未实现、当前 cohort/旧 cohort 继续分栏。

legacy ExitMonitor 的 C1/C5 等项不属于当前 Playbook 管辖，不作为本议题的生产修复。

### P1 · 首选候选：R2 开盘 0–15 分钟 0.5×尺寸，但须重新前瞻确认

既有 RFC-155 采集已够回答“是否值得进一步验证”，不足以直接上线：

| 规则 | 受影响 n | 净 Δ | 结论 |
|---|---:|---:|---|
| R1 ATR≥5% → 0.5× | 19 | **−$16.17** | 放弃，不上线 |
| R2 open15 → 0.5× | 31 | **+$401.95** | 仅作新假设 |
| R4 ≥2 flags → 0.5× | 12 | +$154.32 | 样本不足，不选 |

R2 虽把日级最大回撤从 $943.87 降至 $616.58、左尾净额从 −$1,492.28 改善至 −$924.18，
但前半窗 **−$41.44**、后半窗 **+$443.39**；10-30 单日贡献 +$327.49，剔该日后仅 **+$74.46**。
这又是在六个既有臂中事后选出的最佳臂，因此旧样本只用于出题，不能同时用于验收。

建议复用 `risk_size_tier_forward` 采集，预注册 **R2-only 新 cohort**，不新增注册项：

1. 2026-11-11→11-27 重计，至少 30 笔 R2 受影响交易且覆盖 ≥10 个 ET 日；
2. 全部成功 assignment 结局覆盖率 ≥95%，pending 不得转 0 或移出分母；
3. 净 Δ>0、日级回撤不恶化、左尾净额改善、避免亏损>放弃赢家；
4. 前后半窗不反号，剔贡献最大 1 日后仍为正；
5. 日聚类 80%CI 下界>0 且日均效应>MDE80；不足则 `INCONCLUSIVE_NO_DEPLOY`；
6. 同期不改 `entry_confirm`、early-tight、BE/TP/SL、票面或费用门；
7. 通过后另立 RFC + 用户批准，才在 sizing SSOT 单点启用 0.5×，回滚为 1.0×。

尺寸只降低美元左尾与回撤，不宣称创造 alpha，也不借此加仓或提频。

### P2 · 现役 `entry_confirm` 只按 RFC-158 继续结算

当前 treatment resolved n=31/60、holdout n=14/20、配对日 3/10，覆盖率未达 95%， verdict=`INSUFFICIENT`。
treatment/holdout 每 resolved assignment 净额为 $2.95/$3.73，尚不能证明减尾同时不损总净额。
不得因单日胜率或 BABA 个案改 120 秒、holdout、mid/grace 或继续阈值购物。

### P3 · 费用放在 R2 之后，一次一变量

DA 估算费占毛额 **51.0%**，必须跟踪，但不能靠抬名义额解决：RFC-191 已撤回 Option B、作废 Option C。
既有费用影子中，`<$4,000` 被拦组 42 笔净 −$107.79，可保留为后续候选；
`<$5,000` 被拦组 96 笔却净 **+$616.01**，不能用更大的名义门一刀切。
任何费用行为变更须另起当前 cohort 预注册，且不得与 R2 同批。

### P4 · EXT 不放大、不新建门

EXT 当前净额为正，但 n=28、两笔大亏占其净亏损总额 **96.4%**，尾部集中且仍处于 n<30 小样本边界。
按 OKR 继续观察，不用 EXT 的盈利掩盖 DA 问题，也不据小样本放大或转正。

## 6. 验收与停止线

任何后续生产变更都必须同时报告：DA 净 bps/笔、总净额、日聚类 80%CI+MDE80、笔均盈亏、
`SL_INITIAL` 频率与损失、开盘段放弃赢家成本、费毛比、半窗和剔最佳日结果。

- 只净 bps 改善但总净额/回撤恶化：不接；
- 只减少 `SL_INITIAL` 数量但总净额变差：不接；
- 效应 <MDE：写“不可分辨”，不写“已证伪/已有效”；
- 到 11-28 若 DA 净 bps 仍 ≤0 且日聚类 CI 含 0，按 OKR 走降频/换策略/承认无 edge，不默认再加门。

## 7. 复现与代码锚点

```bash
PYTHONPATH=. ./venv/bin/python scripts/session_attribution.py --date 2026-11-10
PYTHONPATH=. ./venv/bin/python scripts/early_tight_acceptance_replay.py --help
```

- 当前统计：`data/ftseek_us.db::trade_records`，谓词见本文开头；未平仓与 `fees_source IS NULL` 行不进费后分子。
- 风险尺寸：`scripts/risk_size_tier_forward_eval.py`、`app/playbook/risk_size_shadow.py:42`、
  `data/shadow/risk_size_tier_forward.jsonl`。
- early-tight：`app/playbook/exit_engine.py:552`、`docs/debates/DEB-20261105-sl-initial-early-tight-acceptance.md`。
- `entry_confirm`：`data/shadow/entry_confirm_forward_latest.json`、`docs/decisions/RFC-158-entry-confirm-reopen-v2-preregistration.md`。
- 止盈/保本：`docs/decisions/RFC-188-staged-lock-shadow.md`、
  `docs/analysis/2026-09-24-take-profit-recalibration-preregistration.md`。
- 费用：`docs/decisions/RFC-191-single-name-cap-vs-fee-line.md`、`data/fee_gate_shadow_latest.json`。

## 8. 本轮实际动作与未验证

- 实际动作：只读 SQL、既有 JSON/影子计算与代码核对；新增本诊断文档和 handoff 记录。
- 未做：未改任何 `.py`/YAML/ENV/DB，未新增 cron/推送/影子项，未重启服务，未调用券商。
- 未验证：R2 独立新 cohort 尚未开始，所有尺寸/费用/退出生产效果均未获新证据支持。

## 9. 2026-11-11 实施回执：P0 + R2 预注册已落地

- **P0 日报**：`scripts/session_attribution.py` 新增 I 段，输出月初至 `--date` 的 A9/A10 入场 cohort；
  DA/EXT 并列胜率、笔均盈亏、打平胜率、净 bps、费毛比；DA 入场时段与原始 SL/early-tight 分型单列。
- **R2 判分**：`scripts/risk_size_tier_forward_eval.py` 新增 `r2_open15_confirmation`；
  冻结 2026-11-11→11-27，只裁决 open15，含 30/10 样本门、95% 结局覆盖、日聚类 CI+MDE、半窗和剔最佳日。
- **预注册**：`docs/decisions/RFC-155-left-tail-risk-sizing-shadow.md` §6；
  功效预警为旧窗剔最佳日约 +$5.73/日 vs 10 日 MDE80 $19.36/日，短期不足只能不可分辨。
- **测试**：`tests/test_session_attribution.py` 新增 3 例，R2 评估器新增至 9 例；
  直接契约 12 例，与相邻风险尺寸测试合计 **22 passed**，两脚本 `py_compile` 通过。
- **生产库只读复跑**：I 段重现 DA 63 笔 +$254.54/+8.55bps、开盘段 −$210.53、early-tight 6 笔 −$551.69；
  R2 新窗 0/30、0/10、0/0 resolved，`TOO_EARLY`。
- **未做**：未改 `app/`、YAML/ENV/DB、实际尺寸、入场、退出、cron、推送或注册项；
  未重启服务，未调用券商。脚本修改不产生任何生产交易行为。
