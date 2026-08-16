---
type: "review"
title: "Playbook 时代专属优化分析 (2026-08-03)"
created: "2026-08-03"
tags: [playbook, optimization, regime, exit-path]
---

# Playbook 时代 (6/27+) 专属优化分析 — 排除主管线历史数据

> 数据口径: 仅 trade_records 2026-06-27+ (193笔) + playbook_instructions 6/27+ (506条/186已执行)。
> 主管线时代 (4-6月) 数据全部排除。

## 一、时代画像

| 指标 | 值 |
|---|---|
| 笔数/胜率/PnL | 193笔 / 50% / -206 |
| 盈亏比 | 0.91 (平均赢 +31 vs 平均亏 -34) |
| 单笔最大亏损 | -161 (AMD 07-02 LONG, 87h 隔夜 TIME_EXIT) |
| 亏损中位 | -22; 亏损>$100 仅 6 笔 |
| 方向 | LONG 150笔 52% -135 / SHORT 43笔 44% -71 (无显著方向 edge) |
| 持仓 | 中位 2.5h; >8h 共 12 笔 +81 (微利, 隔夜期望≈0) |

## 二、决定性分解: 输家路径 vs 正常路径

**56 笔输家路径单 -2039 vs 其余 137 笔 +1833** — 整个盈亏平衡完全由 56 笔拖累。

输家路径 (TIME_EXIT/MFE_STALL_EXIT/SL_INITIAL/TRAILING_STOP):
- TIME_EXIT 33笔 **0 盈利** ($-1011) — 时间止损从未砍错过盈利单, 砍的全是"入场就错"的单
- MFE_STALL_EXIT 17笔 -675
- 特征: LONG 88% (49/56) | 7/21-24 集中 27笔 (-860, 占输家亏损 42%) | source 全 PLAYBOOK_SIM

## 三、regime × 方向 (playbook 时代, 反直觉)

| regime | 方向 | n | WR | PnL |
|---|---|---|---|---|
| cautious | LONG | 88 | 55% | +51 |
| cautious | SHORT | 24 | 62% | **+184** |
| risk_on | LONG | 27 | 41% | **-180** |
| risk_on | SHORT | 8 | 25% | +41 |
| RANGE | SHORT | 4 | 0% | -216 |

- cautious 整体 112笔 56% +235 — 正贡献 regime
- **risk_on 下 LONG 是最大负贡献组合** (27笔 -180) — 反直觉: "risk_on 应做多"但在本系统是追高被套
- RANGE 下 SHORT 0% (4笔, 样本小)

## 四、标的 (playbook 时代, n≥5)

正贡献: BABA +184 (7笔) / META +180 (7笔) / COIN +88 (5笔) / NVDA +70 (12笔) / MRK +68 (9笔) / PDD +42 / LLY +4
负贡献: GOOG -218 (7笔) / CRM -145 (7笔) / GS -106 (6笔) / UBER -102 (3笔) / DIS -71 / JPM -58 / MSFT -51 / AMZN -36

## 五、优化建议 (playbook 时代数据支撑, 按 ROI)

1. **入场质量是第一杠杆 (替代"退出策略优化")**
   56 笔输家单 -2039 = 全部亏损。TIME_EXIT 33笔 0 盈利证明: 时间止损本身没毛病, 砍的都是入场错误的单。
   问题在"为什么这些单会被生成", 不在退出。过滤规则:
   - risk_on 下 LONG 降权/禁 (27笔 41% -180, 数据明确)
   - RANGE 下 SHORT 禁 (4笔 0% -216, 样本小待验证)
   - 7/21-24 式集中周: 连续 2 日输家单增多 → 自动降量 (需机制设计)

2. **退出: 保持 SL_TRAIL_LOCK 为主 (+1134, 37笔, 唯一显著正贡献)**
   TIME_EXIT 保留为"入场错误的最终止损", 但它的存在本身是入场质量的信号。
   MFE_STALL_EXIT 17笔 -675 待 mfe_pct 数据修复后做反事实 (当前 mfe/mae 字段异常, 273% 之类)。

3. **方向×regime 组合过滤 (替代单一方向倾斜)**
   cautious 双向都行 (LONG +51 / SHORT +184); risk_on 禁 LONG; RANGE 禁 SHORT。
   不是"倾斜 SHORT", 是 regime 条件化。—— 修正上一轮"SHORT 是唯一 edge"的结论 (playbook 时代无方向 edge)。

4. **标的聚焦**: 正贡献 7 标 (BABA/META/COIN/NVDA/MRK/PDD/LLY) 为重点池, 负贡献 8 标 (GOOG/CRM/GS/UBER/DIS/JPM/MSFT/AMZN) 回避。OOS 验证纪律: 双窗口稳定再采信。

5. **隔夜纪律**: AMD 87h -161 是最大单笔。隔夜单 SL 严格 (用户已裁 SL/TP 说了算, 保持)。

6. **归因闭环**: SHORT 方向命中 54.5% (记分牌) 但实际 PnL -71 — 方向对钱没赚到, 归因应回答 (滑点/退出时点/成本)。

7. **数据质量**: mfe_pct/mae_pct 字段异常, 修复后才能做 MFE 锁盈反事实。

8. **OOS 纪律**: W30 (-615) vs W31 (+129) 周度翻转, 参数决策必须跨周验证。

## 六、一句话结论

**Playbook 时代的问题不是"不会退" (SL_TRAIL_LOCK 已证明有效), 也不是"不会选" (方向无 edge),
而是"会进错" — 56 笔输家单 (TIME_EXIT 全亏) 吃掉全部利润。
第一优化杠杆 = 入场质量过滤 (risk_on 禁 LONG / RANGE 禁 SHORT / 集中周降量), 而非退出或标的。**