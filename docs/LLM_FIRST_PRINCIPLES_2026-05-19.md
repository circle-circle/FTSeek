# LLM 全局价值评估 — 第一性原则 (2026-05-19)

> **诚实承认**: L1/L2-B/L3 让 LLM "能调用", 但**没有让它"创造价值"**. 这是流程化思维的陷阱.
>
> **核心问题**: LLM 应该解决量化模型解决不了的问题, 而不是"在每个流程节点都问问 LLM".

---

## 1. 当前 LLM 设计的根本错误

### 1.1 "流程化思维" 的陷阱

我们设计了 11 个 LLM features, 每个都对应一个流程节点:

| Feature | 当前设计目的 | 实际价值 |
|---|---|---|
| thesis_inferer | 入场后给信号"贴标签" | ❌ 入场决策已做, tag 仅供事后归因 |
| regime_classifier | 30min 一次 regime 判定 | ❌ router 已有 RSI/MACD 规则, LLM 多余 |
| disagreement_arbitrator | 共识模糊时 LLM 仲裁 | ❌ 主线 voting 已是 5 model agent, 加 LLM 等于打补丁 |
| large_position_review | 大仓位 LLM 复审 | ❌ 风控规则 (max_position_pct) 已是硬约束, LLM 拦截只是冗余 |
| emergency_review | 风控触发后 LLM 复审 | ❌ 风控触发就该立刻平仓, LLM 拖慢只增风险 |
| sector_correlation | 行业关联推理 | ❌ 信号已有 SPY 对冲信息, 行业关联只是 nice-to-have |
| earnings_analyst | 财报前后分析 | ❌ 财报日期是公开数据, 用日历就够 |
| weekly_proposer | 周末参数建议 | 待评估 (从未触发过) |
| daily_review | 收盘后复盘 | ⚠️ 报告生成了但人不看 |
| premarket_planner | 盘前作战计划 | ⚠️ 输出 plan 但下单系统没用 |
| news_classifier | 新闻情绪 | ⚠️ 真有用, 但走老路径 0% 成功率 |

**11 个里 0 个真正影响过任何交易决策**.

### 1.2 真正的 LLM 能力 (第一性原则)

LLM 真正擅长且**量化系统做不到的**:

| LLM 独特能力 | 量化系统的盲区 |
|---|---|
| **理解非结构化文本** | 新闻、研报、社交情绪 |
| **多源信息综合** | 技术面 + 基本面 + 宏观 + 事件 |
| **因果推理** | 为什么这笔亏了? 不是 sl_hit 就完了 |
| **模式识别 (跨时间)** | 历史"LONG LCID 14 笔 14 亏" 这种模式 |
| **生成式输出** | 写 prompt / 改 yaml / 调阈值 |
| **自然语言交互** | 人 ↔ 系统的 query |

### 1.3 数据告诉我们 — LLM 应该聚焦哪里?

**5/12-5/18 数据**:
- LONG 73 笔 -$3196 vs SHORT 44 笔 +$1444 → **方向选择系统性错**
- LCID LONG 14 笔 -$694, TME LONG 10 笔 -$442, BA LONG 3 笔 -$404 → **同标的反复亏**
- 4/24 ORCL SHORT +$17,883 (1 笔!), 4/27 MSFT LONG +$3142, 4/30 BWXT LONG +$3165 → **历史上有 alpha 信号, 但被埋没**
- noise_pnl 持续 -$15k → **信号质量持续差, 但人没识别**

**LLM 应该解决的 3 个问题**:
1. **入场前 gate**: 拦截"LONG LCID"这类历史失败模式 (而不是入场后 tag)
2. **失败模式 mining**: 自动从 100+ 失败 trade 找出"为什么", 写成 dynamic blacklist
3. **自适应调参**: 看数据自动调 yaml 阈值 (现在靠人手改)

---

## 2. 砍掉无用的 LLM features

### 2.1 立刻停用 (无价值或负价值)

| Feature | 状态 | 停用理由 |
|---|---|---|
| **disagreement_arbitrator** | already disabled | 主线 voting 已 5 model, 再加 LLM 是打补丁 |
| **large_position_review** | already disabled | 风控硬约束已经够; LLM 拖慢入场窗口 |
| **emergency_review** | already disabled | 风控触发必须立刻平, LLM 反应慢 100x |
| **sector_correlation** | already disabled | 行业关联是 nice-to-have, ROI 不明 |
| **earnings_analyst** | already disabled | 财报数据用日历 + RSS 即可, LLM 多余 |
| **thesis_inferer (现状)** | enabled but 0% | 入场后 tag, 不影响决策 — **改造为 pre-entry gate** |
| **regime_classifier (现状)** | already disabled | router 规则够用; **保留但改用 weekly tuner** |

### 2.2 保留 + 重构

| Feature | 保留理由 | 重构方向 |
|---|---|---|
| **news_classifier** | 新闻情绪是结构化系统盲区 | 走 qwen-flash (L1 已修), 输出 → 主线信号 confidence 修正 |
| **premarket_planner** | 盘前规划有意义 | 输出与 main scan 联动 (主线 priority bonus) |
| **daily_review** | 复盘报告 | **改为生成 1-2 条具体行动建议**, 而非长文报告 |

---

## 3. 重构 3 个真正有 ROI 的 LLM 应用

### 3.1 ⭐ Pre-Entry Signal Gate (替代 thesis_inferer 现状)

**当前**: 主线信号通过 → 立刻 entry, thesis_inferer 入场后异步 tag (无影响)
**重构**: 主线扫描出 N 个候选 → LLM 对 Top-K (K=3-5) 综合判断 → 输出 go/no-go + reason

**输入**:
```python
{
  "candidates": [
    {"symbol": "LCID", "direction": "LONG", "strength": 65, "factors": {...}},
    {"symbol": "ARM", "direction": "SHORT", "strength": 70, "factors": {...}},
    ...
  ],
  "context": {
    "regime": "TREND_DOWN",
    "vix": 18.4,
    "spy_5m_momentum": -0.3%,
    "active_positions": [...],
  },
  "history": {
    "LCID_LONG_7d": {"trades": 14, "wins": 0, "pnl": -694},  # 历史失败模式!
    "ARM_SHORT_7d": {"trades": 5, "wins": 0, "pnl": -140},
  }
}
```

**输出**:
```json
{
  "decisions": [
    {"symbol": "LCID", "direction": "LONG", "go": false, "reason": "14 笔历史 0 胜率, 摩擦 0.09% > 信号空间"},
    {"symbol": "ARM",  "direction": "SHORT", "go": true,  "reason": "regime=TREND_DOWN 匹配, ARM 跌势确立"},
    {"symbol": "QQQ",  "direction": "SHORT", "go": false, "reason": "QQQ 已有 main LONG 持仓, 反向冲突"}
  ]
}
```

**集成点**: `signal_pipeline.py` 在 `_execute_long/short` 之前调用; LLM go=false → 拒绝入场.

**为什么有用**:
- ✅ 5/12-5/18 LCID LONG 14 笔 -$694 这种, LLM 看到历史就拦截
- ✅ 用 7 天滚动数据动态学习, 比静态 yaml 黑名单灵活
- ✅ 直接影响 entry 决策, 不是事后 tag

**成本**: 主线扫描频率 ~10 次/小时 × 6.5h × deepseek-pro $0.02 = ~$1.3/日

---

### 3.2 ⭐ Failure Mode Miner (新, 取代 5 因子静态归因)

**当前**: daily_review 输出 noise_pnl=-$15k (5 天不变), behavioral_score=84 (持续 grade B), 没人看
**重构**: 每周日 LLM 扫前 7 天所有亏损 trade, 输出**具体失败模式 + 可执行配置建议**

**输入**: 7 天所有 loss trades (~50-100 笔) + market context

**输出**:
```yaml
# data/llm_failure_modes_2026-05-19.yaml (人工 review 后应用)
identified_modes:
  - id: M1
    pattern: "LCID LONG 持仓 < 5min 全损"
    occurrences: 14
    total_pnl: -694
    suggested_action:
      type: blacklist
      target: "LCID:LONG"
      reason: "low-price stock + 5min hold, friction > signal"
    confidence: high

  - id: M2
    pattern: "盘后 04:00 ET 后开仓的 SHORT 全亏"
    occurrences: 8
    total_pnl: -240
    suggested_action:
      type: cooldown
      target: "after_hours_short"
      reason: "盘后流动性差 + 信号失效"
    confidence: medium

  - id: M3
    pattern: "SHORT main 持仓 1-2h trailing 都赢"
    occurrences: 2
    total_pnl: +1414
    suggested_action:
      type: amplify
      target: "main_short_trail_1to2h"
      reason: "5/15 ARM +$1219 / SNDK +$196 同模式, 应增加权重"
    confidence: high
```

**集成点**: 写到 `data/llm_proposals_<date>.yaml`, 人工每周一 review, 选择采纳的写入 `unified_config.yaml` blacklist / amplify 段.

**为什么有用**:
- ✅ 取代当前 noise_pnl=-$15k 这种"指标但没诊断"的废数据
- ✅ 自动从历史发现"LCID 总亏" / "ARM SHORT main 总赢" 这种结构化信号
- ✅ 可执行 — 直接生成 yaml 而非长文报告

**成本**: 1 次/周 × deepseek-pro $0.30 = ~$0.04/日

---

### 3.3 ⭐ Adaptive Tuner (改造现 daily_auto_tuner)

**当前**: daily_auto_tuner disabled, 阈值靠 5/15 / 5/16 / 5/19 手工改
**重构**: 每日 04:30 ET 跑, 看当日数据自动给出 5-10 条具体调整建议

**输入**: 今日 trade_records + 7d 滚动 + 当前 yaml 关键阈值

**输出** (写 `data/auto_tuner_proposals.db`):
```json
{
  "date": "2026-05-19",
  "proposals": [
    {
      "param": "scalping.breakeven_after_minutes",
      "current": 6,
      "suggested": 8,
      "reason": "5/19 数据 6min 时 30% 仍 0% gross, 延长到 8min 可能命中反弹",
      "expected_impact": "$50/日",
      "confidence": 0.6
    },
    {
      "param": "main.short_effective_threshold",
      "current": 65,
      "suggested": 60,
      "reason": "今日通过 effective>=65 仅 2 个 SHORT 信号, 放宽可释放 3-4 个高 confidence 信号",
      "expected_impact": "$200/日",
      "confidence": 0.7
    }
  ]
}
```

**集成点**: 人工每周一统一 review, 选择采纳后改 yaml.

**为什么有用**:
- ✅ 取代每天手工调 (Phase 1 5 个改动都是手工)
- ✅ 数据驱动, 有量化 expected_impact
- ✅ 不直接修改 yaml — 人工 review 防止 LLM 错误调参 wipe 系统

**成本**: 1 次/日 × deepseek-pro $0.10 = $0.10/日

---

## 4. 实施路径 + 成本对比

### 4.1 三阶段实施

**Phase 1 (本周内, 立即收益)**:
- 砍 6 个无用 features (改 yaml `enabled: false` + 删 disagreement/large_position/emergency 调用代码)
- 保留 L1/L2-B/L3 已做的修复 (基础设施)

**Phase 2 (2 周内, 重构核心 LLM 应用)**:
1. **Pre-Entry Signal Gate** (新写, 替代 thesis_inferer 现状)
2. **Failure Mode Miner** (新写, 周日 cron)

**Phase 3 (1 月+, 自适应)**:
3. **Adaptive Tuner** (启用)
4. premarket_planner 与 main scan 联动

### 4.2 成本对比 (重构前 vs 重构后)

| | 当前 (设计 11 个但 0 价值) | Phase 2 后 (3 个但全部有价值) |
|---|---|---|
| LLM features 数量 | 11 enabled (但都没用) | 3 (Pre-Gate / Miner / Tuner) + 2 辅助 (news/premarket) |
| **LLM 决策影响交易笔数** | **0** | 预期 50-100 笔/天 (Pre-Gate) |
| LLM 周成本 | $3.34 (空跑) | $7.5 (有效) |
| **预期收益影响** | $0 | **+$2k-5k/周** (拦截 LONG LCID 类失败) |
| **ROI** | 0 | **300-650×** |

### 4.3 关键放弃 (诚实)

**砍掉这些设计良好但无 ROI 的 features**:
- ❌ disagreement_arbitrator: voting 已多模型
- ❌ large_position_review: 风控硬约束足够
- ❌ emergency_review: 拖慢响应反而更危险
- ❌ sector_correlation: nice-to-have, 不优先
- ❌ earnings_analyst: 用日历就够

**这些 features 删掉是好事** — 减少认知负担, 集中资源做 3 个真正有用的.

---

## 5. 第一性原则总结

### 5.1 LLM 应该做什么 (3 个 yes)

| ✅ Pre-Entry Gate | 用 LLM 拦截"看历史就该拒"的 entry — 直接影响胜率 |
| ✅ Failure Mode Miner | 用 LLM 从批量数据找规则, 替代静态归因模板 |
| ✅ Adaptive Tuner | 用 LLM 自动调参, 替代手工改 yaml |

**共性**: 这 3 个都是**量化系统真做不到的事** — 跨时间模式 + 多源综合 + 因果推理.

### 5.2 LLM 不应该做什么 (5 个 no)

| ❌ 入场后 tag (thesis_inferer 现状) | 决策已做, tag 无意义 |
| ❌ 单点风控 review (large_position/emergency) | 拖慢硬约束, 无新信息 |
| ❌ 30min 一次 regime 分类 (regime_classifier) | router 规则已经够, 频率太高浪费 |
| ❌ 多模型仲裁 (disagreement_arbitrator) | voting 已多模型, 再加是打补丁 |
| ❌ 长文报告 (daily_review 现状) | 报告再好没人看就是 0 价值 |

### 5.3 一句话评估

> **当前 LLM 链路是"流程化思维"的反例**: 11 个 features 让架构看起来"全面", 但**0 个真正影响交易**. 真正的价值在于**用 LLM 做量化做不到的事** — pre-entry 拦截历史失败模式, 周级失败模式 mining, 日级自适应调参. **砍 6 个加新 3 个**, 周成本 ~$7.5, 预期 +$2-5k/周, ROI 300-650×.

---

## 6. 给你的建议: 不要急着做更多, 先做对的

**今晚部署 L1/L2-B/L3 (已完成)**:
- 仅作为基础设施修复 (audit 可观测 + scalping 数据释放)
- **不期望立刻提升收益**, 只是修通路

**本周后 (基于今晚 audit raw 数据)**:
1. 数据驱动改 schema (看 LLM 实际输出)
2. 关掉 5 个 disagreement/large_position/emergency/sector/earnings (`enabled: false`)
3. 设计 Pre-Entry Gate prompt (用历史 trade_records 数据训练 prompt)

**2 周后**:
4. 实施 Pre-Entry Gate, A/B 测试 1 周
5. Failure Mode Miner 周日 cron

**1 月后**:
6. Adaptive Tuner 自动产出建议
7. 逐步关掉手工 yaml 调参

---

## 7. Karpathy 原则贯彻

- **Show, don't tell**: 不在乎设计了多少 features, 在乎多少 LLM 调用真的影响了交易. 现状: **0 笔**. 重构后: 50-100 笔/天.
- **Simple beats clever**: 11 features → 3 核心. 砍掉的 8 个不是失败, 是"这本来就不需要 LLM".
- **Gradients over rules**: Failure Mode Miner / Adaptive Tuner 让系统自己从数据中学, 替代手工 yaml 调参.
- **Skin in the game**: Pre-Entry Gate 是直接修改入场决策, 不是事后 tag — LLM 必须为决策负责.
