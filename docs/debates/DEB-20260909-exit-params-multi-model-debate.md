---
title: 退出参数优化提案辩论 (隔夜收敛/TP捕获/财报硬门/保本垫) — 三模型两轮
date: 2026-09-09
status: 已裁决并落地 (2026-09-09 用户裁"同意" = M1+M2: 四提案不落地等影子毕业 + 行动项C落地, 见文末落地回执)
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
