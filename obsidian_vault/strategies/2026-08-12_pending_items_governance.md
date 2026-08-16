---
date: 2026-08-12
type: governance
actions: [dead_signal_diagnosis, fee_governance, stall_shadow_tag]
---

# 挂起事项治理 (8/12)

## 一、DEAD_SIGNAL入场侧诊断 (近7日4笔0胜率-$116)

| 特征 | DEAD(n=4) | STALL(n=9) | OTHER(n=24) |
|---|---|---|---|
| conf | 45.0 | 52.4 | 40.3 |
| MFE% | 0.04 | 0.17 | 1.19 |
| thesis"等回调"措辞 | **100%** | 44% | 33% |
| news_positive | 50% | 78% | 50% |

**发现**：
1. DEAD笔MFE≈0——入场即反向，与STALL同病（入场质量问题）
2. GS连续两天thesis相似度89%（PE估值+内部人净卖出的同一套话）——**DA对同一标的重复生成近似thesis，无增量信息**
3. 再入场2笔DEAD（CRM/TSM，MFE=0.00）——"前笔非硬亏平仓后回踩企稳"的再入场假设在这两笔被证伪
4. conf再次反向（DEAD组conf 45 > OTHER 40）——**conf与结局第三次确认无正相关**

**治理**：n=4太小不动门禁。与STALL影子合并攒样（脚本已含is_dead标签）。

## 二、费用治理 (7日$187.71) —— 结论：已自愈，无需新增动作

按名义额分桶（近7日37笔）：
| 桶 | n | 费用 | fee_bps | 净PnL |
|---|---|---|---|---|
| <$3.5K | 13 | $52.7 | 27bps | -$142 |
| 3.5-5K | 4 | $17.8 | 12bps | -$79 |
| 5K+ | 20 | $84.4 | 8bps | -$29 |

**$4.02地板单（$1.99佣金×2边）全部集中在8/5-8/6**。8/7 admission min_notional=$3000执行时复核生效后：
- min名义额 $847→$5171+
- 费率 19-24bps→**8bps稳定**
- 8/7起零<$3.5K单

**结论**：费用问题已被既有门禁解决，当前8bps是$5K+名义额的物理下限（佣金地板+交收费），进一步降费只能靠抬名义额——但max_position_pct=6%已定，无安全空间。挂起项关闭。

## 三、MFE_STALL影子打标 —— 已落地 + 历史回填出重大发现

**落地**：scripts/stall_entry_shadow_tag.py（盘后打标，零执行路径改动）
- 挂载：scheduler_core AFTER_HOURS（隔夜shadow结算后，异常隔离）
- 配置键：stall_entry_shadow_enabled（回滚=false+重启）
- 幂等：decision_id去重，文件持久化，重跑不新增 ✅

**历史回填307笔（STALL n=31 ≥ 门槛30，Fisher检验）**：

| 特征 | STALL占比 | 非STALL占比 | 提升 | p值 | 判定 |
|---|---|---|---|---|---|
| news_positive触发 | 74% | 47% | 1.6x | **0.0015** | ⚠️显著 |
| insider_net_buy触发 | 32% | 14% | 2.2x | **0.0066** | ⚠️显著 |
| thesis等回调措辞 | 35% | 26% | 1.4x | 0.106 | 弱信号 |

**⚠️ 关键反转：统计显著 ≠ 可设门禁**

news_positive全量152笔的真实分布：
- STALL 23笔（15%）= 全拦可省$756
- **但盈利单81笔+$2206会被误杀**
- 全拦净效应 **-$513**（拦掉的news_positive整体是赚钱的）

非news_positive组：STALL率仅5%，PnL +$2343——对照组更干净，但样本里news_positive本身不是毒药。

**裁决**：
1. ❌ 不设news_positive硬门禁（误杀代价>收益，净-$513）
2. ✅ news_positive从"嫌疑特征"降级为"观察特征"——它标记的是高波动入场，
   不是必败入场
3. 影子打标继续攒样，重点观察**组合特征**（news+insider双中：STALL 32% vs 非14%）
   是否能在子集上把STALL率抬到可拦水平
4. conf第三次确认无预测力（DEAD 45 > OTHER 40；STALL历史也是反向）——
   与8/7"score无rubric"结论互证，**conf不应进任何门禁**

## 四、提交清单
- scripts/stall_entry_shadow_tag.py（新增）
- app/core/scheduler_core.py（挂载点）
- config/unified_config.yaml（stall_entry_shadow_enabled键）
