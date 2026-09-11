---
title: 退出参数优化提案辩论 (隔夜收敛/TP捕获/财报硬门/保本垫) — 三模型两轮
date: 2026-09-09
status: §1-§6 已裁决并落地 (2026-09-09 用户裁"同意" = M1+M2, 见 §6 落地回执); **§7 已裁并落地 (2026-09-11 用户裁"按建议执行" = 选项(d) 双口径并列: 主 cohort 判 BROKEN 不放宽 + sub_cohort 观测面 + W1_* 直推告警链修复, 见 §7.10 落地回执)**
trigger: 用户指令「启用多模型会话进行辩论后给出共识和优化方案 我来决策」, 针对主持方(Claude)当日上午提出的四项 SL/TP 优化点
debaters: Claude (主持/数据), Qwen3.8-Max (架构评审), Kimi-K3 (反幻觉核查), GLM-5.3 (实证复核)
---

# 退出参数优化辩论 — 共识与裁决菜单

> 数据口径: trade_records PLAYBOOK_SIM 已平仓, 正式时代 = 2026-08-01 起 (n=269, 毛+$1,545/费$1,299/净+$246)。
> 量纲注意: pnl_pct 是 ratio, mfe/mae_pct 是百分数。全部数字可经 sqlite3 mode=ro 复算。

## 0. TL;DR

**主持方的四项优化提案被三模型以独立复算全部否决** — 现行参数在正式时代口径下已优;
我方的全历史静态 MFE/MAE 反事实犯了两类方法错误(跨时代混算 + 路径时序盲区),
正式时代重算后符号全部反转。真正可立即行动的是辩论中意外发现的一个潜伏缺陷
(BE 垫 fallback 破几何), 以及四条已在攒样的预注册影子的毕业判据风险登记。

## 1. 主持方原始提案 vs 三模型独立复算

| # | 原提案(全史静态cf) | 正式时代实证 | 判定 |
|---|---|---|---|
| T1 | 隔夜收敛 (25笔−$1,426) | EOD收盘强平反事实 = **−$660 更差** (GLM 逐笔, replay_bars_daily); 隔夜仓是负向选择产物 — 盈利仓 15:52 已被 PRE_CLOSE_EXIT 平掉, 过夜的只剩水下仓; 浅水(>−1%)修复率 69%, 深水(≤−3%) 3笔全死 −$951 (Qwen) | **全体: 维持现状** |
| T2 | 硬TP1.5% (cf +$780) / dd 0.8→0.5-0.6 | TP1.5% 对 70 引擎笔净 **−$827** (受益带 MFE∈[1.5,3) +$375 < 尾部 MFE≥3% 被截 −$1,018, GLM); 正式时代重算 **−$1,355** (Qwen); metadata._exit_shadow 路径回放 be0.5_dd0.4 **−$757** + exit_param_shadow n=269 **−6.4pp** 双证伪 dd 收紧 (Kimi/Qwen) | **全体: 维持现状** |
| T3 | 财报硬门 (days_until≤2 禁新开) | 因果未立 (爆亏簇日期分散); earnings_calendar 表 0 行无读者; GLM 替代发现 entry_gap≥5% 追入 −$651, 但 R2 实查 **100% 落在已裁 D1' 域内, 增量为零** | **全体: 否决** |
| T4 | 保本线+0.1%费用垫 (救−$40) | **垫已存在**: fee-aware 0.091% + slip 0.2% = 0.347%, 受 pad<0.4 几何约束; 再加 0.1% → 武装瞬间 SL>现价 = 变相硬 TP 0.4%, 摧毁 109 笔 +$945 的 BE 引擎 (Qwen); 对 96 笔正收益 BE 的截断代价粗估 −$400 (Kimi/GLM) | **全体: 否决** |

## 2. R2 交叉质证的关键修正与发现

1. **GLM 撤回自身 R1 两项主张**(诚实登记): "17笔−$651" 在当前库不可精确复现(最接近 [5,14)带 −645.07); "+$375 分层受益" 带选错 — 现有影子 tiered_dd0.5_tier1.5 的收紧带是 MFE<1.5 (+$1,462), 而 [1.5,3) 带上所有收紧变体皆负 (−562~−1,155), Qwen 另引前瞻样本 13/13 全负。**分层锁利无需新影子 — tiered_* 变体 7/25 已立案攒样, 现领先变体亦未过毕业线(泄漏/毛收益 ≈1029/1462)。**
2. **Kimi 复算确认主持方主干数据成立**(269笔/+$1,545/隔夜−$1,426/宽SL桶+$1,376/SL cap悲观界−$207), 隔夜亏赢拆分有 ±$68 误差(MRK 归类), 不影响结论。
3. **Kimi 收回"费用是数量级杠杆"主张**: 9月笔均毛利 $12.70 vs 费 $4.83 已覆盖; 费用结构 = $1.99 最低消费地板 + SEC/TAF 过手费, 不动 sizing 无可操作面。
4. **混合时区隐患**: id=1320 entry_time 为 +08:00(8/23 R-12 修复前遗留, 1/269); 运行时 _is_overnight_position 时区安全, 风险仅在 SQL/离线分析层 substr 判日期。
5. **孤儿键确认**: exit_policy.yaml:209 overnight_loss_stop_pct 全仓零读者(census 标 orphan); 生效键 = playbook_horizon.yaml:431 overnight_gap_stop_pct=3.0。语义不同(0.03 ratio vs 3.0 pct), 非同一参数改名。

## 3. 共识 (三模型一致)

**A. 四项原提案全部不落地, 不动任何线上退出参数。**
**B. 优化通道已存在且在跑** — 五条预注册影子: D1'(跳空带入场门)/D2'(延迟确认)/B1(gap_chase_gate)/B19(RFC-079 EOD深水闸)/tiered_*(锁利变体)。攒样未满前动手 = 向未定口径下注。
**C. 唯一已证实的实弹级潜伏缺陷 = BE 垫 fallback 破几何**(Qwen 提出, 无人反对):
   8/17 be 0.5→0.4 落地时旧回退费垫 0.4 未同步复核; fee-aware 静默失败(exit_engine.py:396 except: pass)则 _be_pad=0.4+0.2=**0.6>0.4**, 武装瞬间 SL>现价 = 变相硬 TP。当前休眠(8/20 后 156/156 走 fee-aware), 一次重构即激活。同族分裂: ic_tracker.py:197/383(0.6)/feishu_push.py:331(0.4)/exit_shadow.py:177(0.0)/pipeline.py:7022(0) 四处 fallback 口径不一。修法 = 契约测试钉住 pad<trigger + fallback 收敛单一真源, **零交易行为变更**。

## 4. 裁决日议程登记 (非现在动手)

- **B1 gap_chase_gate**: n=25, 笔均−$37.3 但 WR 52%、符号检验 p=0.79 — 判据(p≤0.05)与"均值负/中位数正"篮子结构性不相容, 攒样期满大概率永不毕业, 裁决日须按预注册流程重议判据 (GLM)。
- **D1' 顺向腿**: n=35, 净−897.31, 误杀率 48–54% vs ≤40% 门 — 差距与 B1 同源, 裁决日议程。
- **B19 EOD 深水闸**: rows_total=1, KEEP_SAMPLING (min_n=8)。
- **分层锁利对账**: GLM 回溯 +$375 vs 前瞻 13/13 负的矛盾未对账 — 按 trade_id 双口径(1-min 路径 vs peak/trough 摘要)重放后再议 (Qwen)。

## 5. 用户裁决菜单

| 选项 | 内容 | 爆炸半径 | 三模型态度 |
|---|---|---|---|
| M1 | 采纳共识 A+B: 四提案不落地, 等影子毕业 | 零 | 全体一致 |
| M2 | 批准行动项 C: BE垫fallback契约测试 + 四处fallback收敛单一真源 | 仅 tests/ 与 config 校验层 | Qwen 提议, 其余不反对 |
| M3 | 推翻共识, 仍要动某参数 (请指明) | 视参数而定 | 全体反对 |

## 6. 落地回执 (2026-09-09, 用户裁"同意" = M1+M2)

**M1**: 四项原提案 (隔夜收敛/硬TP1.5%/财报硬门/BE+0.1%垫) 全部不落地, 不动任何线上退出参数;
裁决日议程 (§4) 仅登记, 攒样期满前不动作。

**M2 落地清单** (爆炸半径: tests/ + config 校验层 + 休眠兜底路径, 零交易行为变更):

| 文件 | 改动 |
|---|---|
| `app/playbook/config.py` | 新增单一真源常量 `BE_FALLBACK_FEE_CUSHION_PCT = 0.15` |
| `config/unified_config.yaml` | `breakeven_fee_cushion_pct` 0.4→0.15 (≈fee-aware 实弹均值0.147%; 修 0.4+0.2>0.4 破几何) |
| `app/playbook/exit_engine.py` / `ic_tracker.py`(×2) / `feishu_push.py` / `exit_shadow.py` / `scripts/exit_path_replay.py` | 五处分裂兜底 (0.6/0.4/0.0) 收敛共享常量 |
| `app/playbook/pipeline.py` | 仅注释: fill 腿保留 0 回退, 标记 `BE_FALLBACK_EXEMPT` (在仓旧仓 US.SNDK×2 无 fee-aware 标记, 收敛会变其实盘行为) |
| `tests/playbook/test_be_pad_geometry_contract.py` | 新增契约 12 例: yaml几何/常量几何/fee-aware故障双臂(修复臂+缺陷复现臂)/源收敛 |
| `data/config_freeze_baseline.json` + `tests/test_kr2_freeze_exemption.py` | 新版本 `exit-13f6c2cc7fc4` 豁免登记 + 钉子同步 (强制留痕纪律) |
| `tests/test_be_passive_marketable_limit.py` / `tests/test_playbook_feishu_push.py` | 裁决依据钉线 DEB-20260903→DEB-20260909; TZ-3 套牢线测试判别式按新真值 0.15 重推 ($7.5线: $5拦/$15放) |

**行为边界**: fee-aware 正常 (8/20 起 156/156) 时线上出场零变化; 变化仅在 ① fee-aware 静默失败的降级路径
(修破几何: 武装即触发变相硬TP → 回退垫 0.35 线) ② 飞书晚报"入场即套牢"阈值 0.4%→0.15% (报告口径, 非交易)
③ 影子/回放兜底垫对齐实盘。KR2 注意: 该键在退出参数指纹内 → 判分窗口自 `exit-13f6c2cc7fc4` 新开, 前后不混算。

**测试证据**: 触及面 173 passed (feishu_push/BE契约/净保本/shadow/豁免钉/replay/patient_exit);
全量 3608 passed / 2 failed, 均存量且与本次零交集 — ① test_dead_db_literal_gate 抓到 RFC-080 demix 三脚本
(HEAD 7a7581f5 引入) 引用废弃库字面量 ② test_env_separation_shadow 比对 live/sim 库历史数据行
(US.PDD 7/10 pnl_net 逐字源行缺失, 数据态)。

---

## 7. 追加 (2026-09-11): 行动项C 触发 KR2 主 cohort 破判 — 事实、断链与四选项待裁

- 状态: **已裁 (2026-09-11) 并落地 — 用户裁"按建议执行" = §7.6 选项(d) 双口径并列。**
  落地内容/测试证据/回滚方式见 §7.10。判分常量 `_FREEZE_COHORT_START` 与钉死它的测试**一字未动** (选项(a) 的改动面全部没有发生)。
- 发现路径: 审查 `DEB-20260910-da-ue-target-instruction-chain-review.md` 时, 其 §3.3 把一笔 9/10 退出、
  哈希为 `exit-13f6c2cc7fc4` 的交易 (id=1473) 记为"窗外旁证"并声明"本文不裁定全月达标"。
  该数据点实为 §6 行动项C 的未落地后果, 且已触发 OKR-2026-09 KR2 的预注册回滚线。
- 证据等级: 全部 A (当前代码与数据库可确定), 复算 SQL 见 §7.8, 只读 `mode=ro`。

### 7.1 事实: cohort 已破判, 守卫今晨已报

`scripts/daily_invariant_check.py` 于 **2026-09-11 05:05:09 CST** (= 09-10T17:05:09-04:00) 运行, 产物
`data/invariant_check_latest.json` 实测:

```
config_freeze.cohort = {
  window_start: "2026-09-03",
  versions: { exit-de49b1299290: {n:37, first:"2026-09-03", last:"2026-09-09"},
              exit-13f6c2cc7fc4: {n: 9, first:"2026-09-09", last:"2026-09-10"} },
  n_unique: 2, hash_missing_rows: 3, status: "broken" }
violations = { W1_kr2_cohort_broken: 1, W2_new_field_degen: 1 }
```

守卫告警文案逐字 (`daily_invariant_check.py:158-164`): "KR2 主 cohort 窗 (2026-09-03→2026-09-30) 出现
2 个退出哈希 ⇒ 判据 **BROKEN** (2026-09-06 修订: 变更预算归零, P0 豁免通道对本维关闭 —
`config_freeze_baseline.json` 的豁免**不适用**); 9 月单版本样本作废, 须 DEB 落档 + 10 月开跑会项目级裁决"。

即: **§6 登记的 `exit_versions` 豁免只压住 `exit_status` 维 (实测 `exit_status: "ok"`), 压不住 cohort 维。**
两维在代码里刻意分离, cohort 分支在 `if len(cohort_versions) > 1` 上无条件判破, 不读 baseline。

### 7.2 为什么无人被告知: 告警链在同一次修复里被二次断裂

`daily_invariant_check.py:36` 自述"违反数 >0 → logger.error (被 error_alert.py 15min 扫描拾取推钉钉)"。
DEB-20260910-logging 批 (commit `1271a0a0`) 确实修好了**文件覆盖面**断裂 (原只读 `logs/ftseek_us.log`,
现已含 `logs/invariant_check.log`), 但同批把触发判据从绝对条数改为相对突增:

| 环节 | 实测 | 后果 |
|---|---|---|
| 不变量自检输出形态 | 无论多少处违反, **只输出 1 行汇总 ERROR** (`:839`; `rec()` 于 `:580-582` 只计数与存例, 不打日志) | 单次运行恒 1 行 |
| error_alert 触发判据 | `MIN_THRESHOLD = 3` (`error_alert.py:78`), `threshold = max(3, ⌈3M⌉)` (`:275`), `triggered = force or count >= threshold` (`:420`) | 地板 3 行 |
| 今晨实跑 | `logs/error_alert.log` 05:15 行: "2026-09-11 05:00 ERROR/CRITICAL **1 条 < 阈值 3** (基线 M=0, 扫 158 文件)" | **未推送** |
| OKR 面板 | `scripts/okr_2026_09_scoring.py` **不在 crontab** (实测 `crontab -l` 零命中) | 无二次可见面 |

⇒ KR2 破判写进了 JSON、写进了日志、脚本 exit 1, 但**没有任何主动通道到达用户**。
结构性根因: "ERROR 突增"是**速率型**判据, 不变量违反是**事件型**告警, 两者共用行数阈值不兼容 —
调低阈值会让日常噪声刷屏, 不是解法。

### 7.3 §6 那句"判分窗口自本版本新开"目前是三处互相矛盾的未获批断言

§6 落地回执 (本文件 :79) 与 `data/config_freeze_baseline.json` 的 `exit-13f6c2cc7fc4` 条目均写有
"哈希入指纹故 KR2 判分窗口自本版本新开, 前后窗口不混算"。实测该断言**从未落地**:

| 载体 | 当前真值 | 与断言一致? |
|---|---|---|
| `daily_invariant_check.py:111` | `_FREEZE_COHORT_START = "2026-09-03"` | ✗ 未移动 |
| `tests/test_w1_exit_version_guard.py:224` | `assert '_FREEZE_COHORT_START = "2026-09-03"' in src` (字面钉死) | ✗ 仍钉旧值 |
| `docs/OKR-2026-09.md` 变更记录 | 最后一行 = **2026-09-06**, 9/9 之后**零行** | ✗ 未落档 |
| `config_freeze_baseline.json` 自身 note | "改本文件=改判据, 须先改 OKR/DEB (判据字典纪律)" | ✗ 违反自身纪律 |

⇒ 一句改写判分窗口的断言躺在**数据文件**里, 而数据文件明写自己无权改判据。这正是 OKR-2026-09 KR2
2026-09-03 勘误所定性的"判据与事实分叉"病灶复发, 且这次分叉由本 DEB 的落地回执引入。

### 7.4 切分边界实测: 干净, 无交错

| 段 | n | 毛 | 费 | 净 | 开仓日 | 退出时刻 (ET) |
|---|---:|---:|---:|---:|---|---|
| `exit-de49b1299290` | 37 | 359.60 | 181.13 | **+178.47** | 9/3→9/9 | 9/3T09:44:12 → **9/9T13:33:24** |
| `exit-13f6c2cc7fc4` | 9 | −288.44 | 41.98 | **−330.42** | 9/9→9/10 | **9/10T09:38:54** → 9/10T11:38:14 |
| 未平仓 (hash NULL) | 3 | — | — | — | 全 9/10 | — |

- 两段退出时刻间隔 ≈ **20 小时, 零交错** (`de49` 在 9/10 零退出; `13f6` 全表仅这 9 笔) ⇒ 按退出时刻切分技术上是干净边界。
- 时区自检: 本窗 46 笔已闭合 **100% 为 `-04:00`**, 3 笔 NULL (未平仓), 无无后缀/无 `+00:00` 混入
  ⇒ 本窗内日期子串比较安全 (注: 全表另有 23 笔无后缀 + 1 笔 `+00:00`, 集中在 2026-06-24~07-21, **不可**把本窗 SQL 直接套用到 6-7 月)。
- **唯一骑墙笔 = id 1473** (US.SNDK LONG, 开仓 9/9 = 变更前, 退出 9/10T09:38 = 变更后, 净 −251.82)。
  其余 8 笔新哈希全部 9/10 开仓 (BWXT +10.39 / ARM +4.77 / RIVN +8.88 / HPE −135.59 / META +22.53 /
  SNDK +4.29 / SKHY +4.88 / NVDA +1.25, 全 LONG)。
  ⇒ 若移窗, 起点应为 **9/10 而非 9/9**: 9/9 含骑墙笔, 把混源交易放进"单版本窗"会重新制造歧义。

### 7.5 功效算术: OKR 的 ~200 目标在实测速率下本就不可达

已闭合笔数按开仓日: 9/3=15, 9/4=11, 9/8=7, 9/9=5, 9/10=8 (另有 3 笔 9/10 开仓未平) ⇒ 46 笔 / 5 个交易日
(9/7 劳动节休市) = **9.2 笔/日**; 计入 9/10 那 3 笔未平的成熟口径 ≈ **9.8 笔/日**。
OKR-2026-09 KR2 自算用的是 **11.3 笔/日**, 高于实测。

| 窗口 | 交易日 | 9.2/日 | 9.8/日 | 对 ~200 目标 |
|---|---:|---:|---:|---|
| 9/3→9/30 (原判分窗, 若未破) | 19 | ≈175 | ≈186 | **仍不达标** |
| 9/10→9/30 (移窗后) | 15 | ≈138 | ≈147 | 不达标, 差额 ≈37–39 笔 |

⇒ 两点必须写进结算: ① **即使 KR2 未破, 9 月也达不到 ~200**, 故"作废整月"销毁的是一个本就功效不足的样本;
② 移窗的边际损失是 ~37 笔, 不是"整月"。外推是算术不是观测 (9/10 的 8 笔含同日进出短线, 速率本身混有旧参数日), 不得当承诺值。

### 7.6 四个选项与实测代价 (2026-09-11 用户裁选项(d), 见 §7.10)

| 选项 | 做法 | 样本后果 | 治理后果 |
|---|---|---|---|
| **(a) 移窗到 9/10** | 改 `_FREEZE_COHORT_START` + 改钉死它的测试 + 补 OKR 变更记录 | ≈138–147 笔 (<200, 须写功效不足) | **事后放宽预注册判据**; 且是本 OKR 周期内第二次放宽 (9/6 已把"全窗"放宽为"cohort 窗") |
| **(b) 维持 9/3, 接受 BROKEN** | 不改任何判据, 按预注册回滚线走 | 37 笔已闭合样本作废 (净 +178.47) | 预注册完整性最强; 但销毁与"未经批准的 P0 换版"同罚 — 而本次换版**经用户裁 + DEB 落档** |
| **(c) 裁定 M2 行为中性、豁免 cohort 维** | 让 cohort 也吃 baseline 豁免 | 保留 9/3 窗全部样本 | **最差**: 需发明"哈希等价性"新概念 (违 OKR"不新增第四把尺子"), 且直接抵触 R-4/R-4b/R-4c 的"漏键假绿"教训 — `breakeven_fee_cushion_pct` 正是在 R-4b 因"会改已实现 PnL"被**加入**指纹的 |
| **(d) 双口径并列** (倾向) | KR2 官方判定保持 **BROKEN** (不动 `_FREEZE_COHORT_START`、不动测试); 另在 cohort 块内加**标注为子窗**的 `9/10→9/30` 单版本观测面, 明写"非 KR2 判分对象" | 两份事实都保留: 破判 + ≈138–147 笔可测子窗 | 不放宽任何预注册判据; 沿用本 OKR 已有惯例"两口径并列, 不得互相冒充" (KR2 边界注); 代价是新增一个观测面, 须防其被当成第四把尺子 |

**决策的真正 crux (两难, 不回避)**: §6 声称 M2 "fee-aware 正常 (8/20 起 156/156) 时线上出场**零行为变更**"。
若该声称为真, 则 37 笔与 9 笔属**同一统计总体**, KR2 的**实质** (单参数版本窗) 未被破坏, 破的只是**代理指标**
(哈希唯一性)。但本项目"漏键假绿"病史 (R-4 / R-4b / R-4c: 键不在指纹内 ⇒ 改了已实现 PnL 而 KR2 恒绿)
恰恰证明"只影响兜底路径"这类声称曾反复出错, 而该键正是因此才被纳入指纹。
⇒ **在"零行为变更"被独立验证之前, 不得据其豁免 cohort 维** (故排除 (c))。
建议同批落一个可验证项: 对边界前后两段的退出行为分布做对照 (出场时点分布 / BE 触发率 / `breakeven_fee_cushion_pct`
实际取值路径), n=9 不足以判等价, 结论只能是"未能证伪"而非"已证中性"。

### 7.7 若选 (a) 或 (d): 落地清单 (精确到 file:line; (d) 已于 2026-09-11 执行, 逐项勾稽见 §7.10)

| # | 载体 | 改动 | 备注 |
|---|---|---|---|
| 1 | `scripts/daily_invariant_check.py:111` | (a) `_FREEZE_COHORT_START` `"2026-09-03"`→`"2026-09-10"`; (d) 保持不动, 另加子窗块 | (d) 不动判分常量是其优点 |
| 2 | `tests/test_w1_exit_version_guard.py:224` | (a) 必须同步改字面断言; 另 `:165,169,170,177,193,204` 用 `day="2026-09-03"` 作窗内测试数据, 移窗后变为窗外, 需逐例重推 | **该测试的存在目的就是强制留痕** — 改它即触发人工复核, 属护栏正常工作, 不得绕过 |
| 3 | `docs/OKR-2026-09.md` | KR2 段补当前事实 + **变更记录补 9/9 与 9/11 两行** (9/6 之后零行是分叉根因) | 无论选哪个选项都必须补 |
| 4 | `data/config_freeze_baseline.json` | 把 `exit-13f6c2cc7fc4` 条目里"判分窗口自本版本新开"改为**指向本节的待裁/已裁结论**, 不再由数据文件自行宣称判据 | 恢复"改本文件≠改判据"的纪律 |
| 5 | 告警链 (§7.2) | 三个子选项: **(i)** 不变量自检对 `W1_*` 关键键**直接走 `app/push` hub** (`sync=True`) 独立推送, 不经 error_alert 行数阈值 — 与 `1271a0a0` 已建立的模式一致; (ii) error_alert 增加**指纹白名单**单条即推; (iii) 不变量自检改为**每违反类输出一行 ERROR** (注: 今晨 2 类仍 <3, 单靠此条不足) | 倾向 (i); (iii) 单独无效, 不要只做 (iii) |
| 6 | 同批观测 | 今晨另报 `W2_new_field_degen: da_instruction.confidence_semantic` (基线外新增退化) | 与 `DEB-20260910-da-ue...` D1 相关: 该字段系 DEB-20260723 H3 为"保留语义置信度"而加的**第三个**信心字段, 现已自身塌缩 ⇒ 佐证"加字段解决不了信心口径分裂", 详见该 DEB 的审查结论 |

### 7.8 可复现 SQL (只读)

```bash
sqlite3 -header -column 'file:data/ftseek_us.db?mode=ro'
```

```sql
PRAGMA query_only=ON;
-- ① 守卫逐字同口径的 cohort 判分式 (daily_invariant_check.py:134-138)
SELECT COALESCE(json_extract(metadata_json,'$.config_version'),'(null)') v, COUNT(*) n,
       MIN(trade_date) first_open, MAX(trade_date) last_open
FROM trade_records WHERE trade_date>='2026-09-03' AND source_type='PLAYBOOK_SIM' GROUP BY 1;

-- ② 分段毛/费/净 + 切分边界 (退出时刻首末)
SELECT json_extract(metadata_json,'$.config_version') v, COUNT(*) n,
       ROUND(SUM(realized_pnl),2) gross, ROUND(SUM(fees_est),2) fees, ROUND(SUM(pnl_net),2) net,
       MIN(trade_date) first_open, MAX(trade_date) last_open,
       MIN(substr(exit_time,1,19)) first_exit, MAX(substr(exit_time,1,19)) last_exit
FROM trade_records WHERE trade_date>='2026-09-03' AND source_type='PLAYBOOK_SIM' AND exit_price>0 GROUP BY 1;

-- ③ 日均速率 (按开仓日)
SELECT trade_date, COUNT(*) n FROM trade_records
WHERE trade_date>='2026-09-03' AND source_type='PLAYBOOK_SIM' AND exit_price>0 GROUP BY 1 ORDER BY 1;

-- ④ 时区后缀自检 (套用到其他窗口前必须先跑本式)
SELECT CASE WHEN exit_time IS NULL THEN 'NULL' WHEN exit_time LIKE '%-04:00' THEN '-04:00 (ET)'
            WHEN exit_time LIKE '%+00:00' OR exit_time LIKE '%Z' THEN 'UTC' ELSE 'no-suffix' END tz,
       COUNT(*) n
FROM trade_records WHERE trade_date>='2026-09-03' AND source_type='PLAYBOOK_SIM' GROUP BY 1;

-- ⑤ 骑墙笔与新哈希逐笔
SELECT id,symbol,direction,trade_date,exit_time,pnl_net,
       json_extract(metadata_json,'$.config_version') exit_hash
FROM trade_records WHERE trade_date>='2026-09-03' AND source_type='PLAYBOOK_SIM'
  AND json_extract(metadata_json,'$.config_version')='exit-13f6c2cc7fc4' ORDER BY exit_time;
```

守卫侧只读复核: `python3 -c "import json;d=json.load(open('data/invariant_check_latest.json'));print(json.dumps(d['config_freeze']['cohort'],ensure_ascii=False));print(d['violations'])"`
告警侧: `grep '2026-09-11 05:00' logs/error_alert.log`

### 7.9 本节边界

1. §7.1–§7.9 起草时**未修改**任何判据、代码、配置、测试、基线文件或数据库; 全部数据库访问为只读 `mode=ro` + `PRAGMA query_only=ON`; 未运行任何业务脚本 (扫描/评分/注入/下单)、未调用 LLM 或券商、未重启服务。裁决后落地见 §7.10 (代码/测试/文档面, 零业务脚本、零交易行为变更)。
2. §7.5 的笔数外推是**算术**, 不是观测; §7.6 (c) 所依赖的"零行为变更"声称**尚未独立验证**, 故本节不采 (c)。
3. 未裁定 M2 本身对错 — §6 的裁决与落地不在本节复核范围, 本节只处理其**未落地的 KR2 后果**。
4. 触发频率类问题 (含 `DEB-20260910-da-ue...` D2 的 `custom_params=None` 尾部路径) 不属本节, 由该 DEB 另行处理。
5. 裁决后须回填: 本节标记为"已裁 (选项 X)" + OKR 变更记录 + 若涉代码则附测试证据与回滚方式。


### 7.10 落地回执 (2026-09-11, 用户裁"按建议执行" = 选项(d))

按 §7.7 清单逐项落地; 选项(a) 的改动面 (移窗 + 改钉死测试) **全部没有发生**。

| # | §7.7 项 | 落地内容 | 验证 |
|---|---|---|---|
| 1 | `daily_invariant_check.py:111` | `_FREEZE_COHORT_START` 保持 `"2026-09-03"` **未动**; 新增 `_SUB_COHORT_START = "2026-09-10"` + `config_freeze.sub_cohort` 观测块: 只数已闭合行 (`exit_price>0`), 输出 versions/n_unique/n_closed/status + `not_kr2_verdict: true` + 防误读 note; **不调 rec()、不产生违反、不参与 status 取劣**。缺 `exit_price` 列的旧库/最小夹具只降级本面 (`skipped_old_schema`), 不拖垮主维 (独立 try + `_is_structural_error` 判别) — 落地时实测教训: 初版无隔离, `tests/test_w1_exit_version_guard.py` 12/14 既有钉子被打红, 已修并钉死回归 | `tests/test_w1_exit_version_guard.py` sub_cohort 段 8 例 (含 12/14 回归钉) |
| 2 | 告警链 §7.7⑤(i) | 新增 `_push_w1_critical()`: `W1_*` 关键违反直走 `app/push` hub **sync=True** (HIGH/RISK_ALERT), 不经 error_alert 行数阈值; 防刷屏 = 与**上次已成功送达**的 W1 键集比对 (相同抑制, 上次失败/首次/键集变化必推); 在 `main()` 内**覆写 OUT 之前**调用; 结果落 `data/invariant_check_latest.json::w1_direct_push` (attempted/ok/error/suppressed_reason); 推送失败显性化不上抛 (JSON/exit 码照落) | 同上 W1 直推段 9 例 (首推/抑制/重试/键集变化/无键不推/hub 异常/stop 异常/调用点顺序/push_enabled) |
| 3 | OKR-2026-09 | KR2 段补"实况: KR2 已 BROKEN"块 (事实/实测/功效算术/骑墙笔/撤回声明/结算影响) + §9/6 行勘误 (告警链从未生效) + 变更记录补 **2026-09-09、2026-09-11 两行** (9/6 之后零行 = 分叉根因, 已闭口) | 文档面 |
| 4 | `config_freeze_baseline.json` | `exit-13f6c2cc7fc4` 条目撤回"判分窗口自本版本新开"的自称, 改为如实记录: 主 cohort 判 BROKEN 不重开不放宽, sub_cohort 观测面非判分对象, 详见本节 §7 + OKR 变更记录 9/11 行 — 恢复"改本文件≠改判据"纪律 | `tests/test_kr2_freeze_exemption.py` 2 例仍绿 (键集未动) |
| 5 | 补测试 | 钉子**追加在既有** `tests/test_w1_exit_version_guard.py` (新增 17 例 → 全文件 **31 例**): sub_cohort 8 例 + W1 直推 9 例。选既有文件而非新建, 与 2026-09-06 cohort 维修订同precedent (同一 KR2 判据、同一 `_check_config_freeze` 函数 ⇒ 一处安家, 避免同一契约两个测试文件各自漂移)。**过程如实记录**: 落地时曾先建独立文件 `tests/test_kr2_option_d_dual_caliber.py` (10 例), 后发现与本批新增重复且其 `_FakeHub` 未建模 `stop()`、源级断言钉的是已被修掉的重复调用点 ⇒ 已把其独有覆盖 (HIGH/RISK_ALERT 量纲、非 W1 违反不得混入直推体、note 文本自陈非判据) 并入既有文件后**删除该独立文件** | 见下"测试证据" |

**测试证据** (venv 3.11.15 / pytest 8.3.5, 2026-09-11 06:3x CST, 实测非记忆):
- `tests/test_w1_exit_version_guard.py` **31 passed** = 既有 14 例全绿 (主判据未被观测面污染) + 本批新增 17 例 (sub_cohort 8 + W1 直推 9)。
- 定向面 **172 passed / 0 failed** (240s, 9 文件): 上式 + `test_kr2_freeze_exemption.py` (2, 豁免键集未动) + `test_okr_2026_09_scoring.py` (面板只读 `config_freeze`, 新增 `sub_cohort` 键不影响其取数) + `test_error_alert.py` + `test_invariant_d4_settled_fields.py` + `test_invariant_i10_exit_time.py` + `test_w456_log_hygiene_ratchet.py` (新增 `except` 块未触 W4/W6 棘轮: 两处均 `exc_info=True`, 无 `logger.debug`, 无裸 `pass`) + `test_stale_bridge_guard.py` + `test_dead_db_literal_gate.py`。
- **对生产库实跑复核** (2026-09-11 06:24): 以 `main(push_enabled=False)` + `dic.OUT` 指向临时路径运行完整守卫 ⇒ `exit_code=1`, `violations={W1_kr2_cohort_broken:1, W2_new_field_degen:1}`, `cohort.n_unique=2` (de49 37 笔 9/3→9/9 + 13f6 9 笔 9/9→9/10) `status=broken`, `sub_cohort.n_unique=1` (13f6 8 笔闭合) `status=single_version` `not_kr2_verdict=true`, `w1_direct_push.attempted=false` 带显式 `suppressed_reason`。**零写入生产 `data/`、零外部通知、零业务脚本调用** — 与 §7.8 只读 SQL 复算逐项一致。

**落地过程自查出的两处缺陷 (已修, 如实留痕 — 二者都不在任何模型的提案里, 是写代码时发现的)**:
1. **`hub.stop()` 缺失**: 初版 `get_push_hub().push(...)` 后不关 hub。`error_alert.py:360-384` 早已记录同一课 — 短命 cron 进程会挂在 hub 的**非 daemon** worker 线程上。本守卫正是 cron (`05 5 * * 2-6`) 调用 ⇒ 修法是 `finally` 内 `hub.stop()`, 关失败只 `logger.warning(..., exc_info=True)` (不用裸 `pass`: 既撞 W4 棘轮, 也与本批立项理由"守卫自己坏了必须可见"自相矛盾)。钉子 `test_w1_push_fires_on_first_violation_and_stops_hub` + `test_w1_push_stop_failure_does_not_change_verdict`。
2. **重复调用点**: 初版在 `result` 字典内与字典后各调一次 `_push_w1_critical`。第二次调用时 `OUT` **尚未覆写** ⇒ 防刷屏判据读到的仍是上一轮状态 ⇒ 同一事实**每晚推两遍**。已删多余调用点, 并加源级顺序契约钉 `test_w1_push_reads_prev_state_before_out_is_overwritten` (断言调用点在 `OUT.write_text` 之前**且全文只有一处调用**)。
   - 注: 此缺陷若不修, 后果与 §7.2 原断链**同族而更隐蔽** — 原病灶是"该推的没推", 这个是"推两遍", 两者都会让告警失去可动作性 (刷屏 ⇒ 守卫被无视)。

**顺带修正一处既有文档错误**: `docs/OKR-2026-09.md` KR2 段"变更预算"行原写破判会"经 `error_alert.py` 推钉钉" — 该链路对本告警**从未生效** (§7.2), 已改为如实描述并在同处加勘误。

**行为边界**: 零业务脚本调用、零交易行为变更、零判据放宽 (主 cohort BROKEN 判定照旧)。首条直推将于下一次 cron 运行 (05:05) 实际送达 — W1 键集与 9/11 已报事实相同但**从未成功推送过** (旧产物无 `w1_direct_push` 记录) ⇒ 按防刷屏判据**必推一次** (这正是修复的目的: 断链期间漏掉的那一条), 之后同键集不再每晚重推。推送本身不写库、不重启服务。

**回滚**: revert `scripts/daily_invariant_check.py` 的四处 (① `_SUB_COHORT_START` 常量 + 注释块 ② `_check_config_freeze` 内 `sub_cohort_pairs` 查询及其独立容错 try ③ `info["sub_cohort"]` 两个分支 ④ `_W1_KEY_PREFIX` + `_push_w1_critical()` 含 `finally: hub.stop()` + `main(push_enabled=)` 内单一调用点与 `w1_direct_push` 键) + 删 `tests/test_w1_exit_version_guard.py` 本批新增 17 例 (sub_cohort 段 + W1 直推段, 既有 14 例不动) + revert 四个文档/基线文件的对应段落 (`docs/OKR-2026-09.md` KR2 实况块/勘误/变更记录两行、`data/config_freeze_baseline.json` 的 `exit-13f6c2cc7fc4` 条、本节 §7、`handoff.md`)。零生产代码依赖 — 本批**未动** `app/` 任何文件、未动退出参数、未动下单/持仓链路。
