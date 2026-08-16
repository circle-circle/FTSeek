---
date: 2026-08-12
type: entry_price_mechanism
action: stale_anchor_shadow_tag (Plan B)
pending: entry_reference接线评估 (Plan A)
---

# 入场价格机制: Plan B落地 + Plan A评估 (8/12)

## 一、现状机制澄清 (用户问"是否支持市价/灵活范围")
系统已是**两段式**: 触发端=范围, 成交端=市价
- 触发: entry_band LONG区间 = 锚价×[1-3%, 1+5%] (下防飞刀/上防追涨)
- 成交: playbook_service L1065 order_type=MARKET
- 数据验证: 32笔已执行全部锚点下方接到(median -1.63%) 净+$235胜率53%
  → 回调接货是edge来源, **不应改纯市价绕过锚点**

## 二、真实缺口: 锚价是盘前死数字
6笔今日指令锚 vs 盘前对照:
| 标的 | 锚 | 盘前 | 偏离 | 问题 |
|---|---|---|---|---|
| AVGO | 403 | 420.97 | -4.3% | 锚太保守, 趋势上行追不上 |
| HON | 239.5 | 231.00 | +3.7% | 支撑破位, 锚悬空 |
| GOOG | 350 | 345.05 | +1.4% | 接近合理 |

既有门已覆盖深度场景: drift门(逆>5%作废)/extreme_gap(逆>8%作废)/出带影子。
**真实缺口 = 趋势逃逸但锚陈旧、未触发又错过** (AVGO型)。

## 三、Plan B 落地: 陈旧锚影子打标 (record-only)
scripts/stale_anchor_shadow_tag.py: 用trigger_miss每轮观测价(11k+条)重建指令当日
价格路径, 对未触发指令打陈旧度标签(too_low/too_high/带内) + 结局。
挂载AFTER_HOURS隔离, 幂等, 回滚=配置键false+重启。

**历史回填539笔**:
| 分类 | n | 触发率 | 含义 |
|---|---|---|---|
| too_low (锚太保守) | **51** | **0%** | 🔴错过的上行机会 |
| too_high (锚太乐观) | 12 | 0% | 追不上 |
| in_band | 114 | 44% | 正常, 已执行PnL -$680 |

→ **51笔too_low从未触发=被陈旧锚错过的真实机会** (NEM锚92.4观测111+/LLY锚1135观测1207+)。
这是接entry_reference或加"锚自愈"硬门的量化依据。

## 四、Plan A: entry_reference 接线评估
entry_reference.py 已设计7种动态锚枚举(market/回踩支撑/突破阻力/VWAP/MA20/MA50/ORB),
但DA路径**未接线** (文件注释"Phase 2才接线")。见对话举例说明。

## 五、下一步
- 观察stale_anchor_shadow攒样, 若too_low持续高频且对应大涨 → 接entry_reference
- 短期保守替代: 锚自愈规则 (触发时锚已陈旧>阈值且当日单边 → 转影子不硬触发)
