# LLM 应用审查与增强方案

> **日期**: 2026-05-14
> **目标**: 充分发挥 LLM 能力 + 保持可控, 提升胜率与收益
> **方法**: 现状盘点 → 缺口识别 → P0/P1/P2 增强 + 6 条可控性原则

---

## 1. 现状盘点

### 1.1 LLM 模型生态 (`app/llm/multi_model_client.py`)

| 模型 | 角色 | 输入价 (¥/1k) | 输出价 (¥/1k) | 当前用途 |
|-----|-----|-----|-----|---------|
| **Qwen-Max** | 综合策略 / 归因 | 0.008 | 0.020 | premarket_planner, attribution_engine |
| **DeepSeek-V4-Pro** | 推理 / 假设生成 | 0.004 | 0.016 | weekly_proposer (参数提议) |
| **MiniMax-M2.5** | 情绪分析 | 0.005 | 0.010 | **未实际调用** (注册了未启用) |
| **GLM-4.7** | 技术指标分析 | 0.005 | 0.010 | 备用 (未启用) |

预算控制: `daily_max_usd: 5.0` (yaml `llm_budget`), per_module 拆分 + 70%/100% 告警 ✅

### 1.2 各阶段 LLM 应用矩阵

| 阶段 | 模块 | 当前实现 | 控制度 | 价值评估 |
|-----|------|---------|--------|---------|
| **盘前** | `premarket_planner.py` (699 行) | Qwen-Max 生成 daily_battle_plan_us.json | 高 (JSON 结构化, OpenChecker 二次校验) | ⭐⭐⭐⭐ 已成熟 |
| **盘前** | LLM watchlist 候选 | OpenChecker (gap+方向背离过滤) | 中 (硬规则) | ⭐⭐⭐ |
| **盘中** | `voting_coordinator` (4 模型) | 加权聚合输出 SignalScore | 中 (consensus_level 阈值) | ⭐⭐⭐ |
| **盘中** | `audit_position_async` | 持仓后 fire-and-forget LLM 风险审计 30s 超时 | 中-低 (单次调用, 失败静默) | ⭐⭐ 用法过窄 |
| **盘中** | `iterative_debate` / `dual_agent_debate` | 模型对话, 默认关闭 (`FTSEEK_DEBATE_MODE=false`) | 高 (默认 off) | ⭐ 几乎未用 |
| **盘后** | `agent_bridge.generate_eod_review` | Hermes 外部 agent 调用 | 中 (外部依赖) | ⭐⭐⭐ |
| **盘后** | `attribution_engine.py` (371 行) | Qwen-Max 写 trade_attribution | 高 (结构化 + DB 落库) | ⭐⭐⭐⭐ |
| **盘后** | `weekly_proposer.py` (347 行) | DeepSeek 周度参数调整提议 | 高 (proposals.db 暂存待回测) | ⭐⭐⭐ |
| **新闻** | `news_v2/pipeline.py` | dedup + relevance + 地缘正则扫描 | 高 (零 LLM) | ⭐ **重大缺口** |
| **风控** | (无 LLM) | - | - | **缺口** |

### 1.3 可控性现状

| 机制 | 实现 | 评分 |
|-----|------|------|
| 预算上限 | `daily_max_usd` + per_module + 告警阈值 | ✅ |
| 输出格式 | JSON 结构化 (premarket/attribution) | ✅ |
| 调用超时 | 多数 30-120s | ✅ |
| LLM 故障降级 | premarket 有兜底; audit 静默失败 | ⚠ 部分 |
| 调用审计 | `_record_call` 记录 cost/tokens | ✅ |
| 自由文本拒绝 | 无 schema 强制 | ⚠ |
| 回滚能力 | weekly_proposer 走 proposals.db, 其他无 | ⚠ |
| Shadow 隔离 | shadow_tracker 已有, LLM 决策未对接 | ⚠ |

---

## 2. 关键缺口 (按价值排序)

### P0-L1 — 新闻 LLM 解读完全缺失 ⭐⭐⭐⭐⭐
**现状**: `app/news_v2/processor/` 是空目录, `__init__.py` 注释写 "预留: LLM 分析"。新闻经过 dedup + relevance + 地缘正则后, **直接落库**, 无 LLM 抽取事件类型 / 影响标的 / 时效窗口 / 情绪极性.

**风险**: 美股 alpha 大头来自新闻驱动 (财报 / FDA / M&A / 央行). 当前系统看不懂新闻, 等于把最大的 alpha 渠道关闭.

**价值估算**: 启用后, 突发新闻 1-5 分钟内可生成 owner=llm_news 信号, 比 IntradayScorer 因子模型快 10-30 分钟. 历史回测 30 天估算: 新闻驱动信号占 alpha 20-35%.

**方案**:
```
app/news_v2/processor/
├── llm_classifier.py       # MiniMax-M2.5 提取: event_type / sentiment / impact_tickers / horizon
├── event_signal_generator.py  # 严重事件 → 生成 NewsSignal (owner_strategy=llm_news)
└── news_attribution.py     # 24h 后回看新闻信号准确率, 写 news_quality.db
```

**LLM Prompt 模板** (结构化):
```json
{
  "event_type": "earnings|fda_approval|merger|guidance_cut|geopolitical|fed_speech|rumor",
  "primary_tickers": ["NVDA"],
  "impacted_tickers": [{"ticker":"AMD","direction":"DOWN","confidence":0.6}],
  "sentiment": -0.7,
  "time_horizon_minutes": 60,
  "credibility": 0.8,
  "action_hint": "WAIT|BUY|SELL|HEDGE",
  "rationale": "短句"
}
```

**可控性**:
- 输出强制 JSON schema 校验, 不通过 → drop
- credibility < 0.5 → 不生成信号, 仅记录
- 同 ticker 5min 内不重复出信号 (cooldown)
- LLM 信号必须经过 RiskGateway (与因子信号同样卡)

---

### P0-L2 — 盘中 LLM 风控 gating ⭐⭐⭐⭐
**现状**: `audit_position_async` 仅在持仓后 fire-and-forget. 突发事件 (VIX 跳 30% / 单股 -10% 闪跌 / 突发新闻) 时无 LLM 投票"立即平仓 vs 持有".

**风险**: 因子模型对"非典型事件"反应慢. LLM 是非线性事件最好的解码器, 但当前未启用做风控.

**方案**: 触发式 LLM 风控 — 不是周期性, 而是事件触发:
```python
# app/risk/llm_emergency_review.py 新增
class LLMEmergencyReview:
    """
    触发条件 (任一):
    - 持仓单股 5min 内 -3%
    - VIX 5min 内 +20%
    - 该 symbol 新闻 LLM 标 sentiment < -0.6
    - SystemCircuitBreaker 进入 DEGRADED

    动作:
    - 同步调 LLM (Qwen-Max, 30s 超时)
    - 输出: HOLD / REDUCE_50 / FLAT_NOW
    - 必须经过 RiskKernel + 单股 cooldown 才执行
    """
```

**可控性**:
- 同 symbol 1 小时内最多 1 次 LLM emergency review
- LLM 决策即使 FLAT_NOW 也走标准平仓路径 (非直接 broker 调用)
- 审计: emergency_reviews 表记录每次决策 + 后续 PnL → 评估 LLM 价值

---

### P0-L3 — F16 entry_thesis (LLM 推断) ⭐⭐⭐
**现状**: F16 entry_thesis 字段已设计未实现 (Phase B 待做), LLM 是最佳推断者.

**风险**: 不知道入场理由 → 退出策略一刀切 (chandelier 用于所有入场), 无法做 thesis-driven exit.

**方案**: 入场时 LLM 推断 thesis (异步, 不阻塞下单):
```python
# 当 IntradayScorer 出 SignalScore 后异步调
def infer_thesis_async(signal, context):
    prompt = f"""
    Symbol: {signal.symbol} {signal.direction}
    Factors: trend={f.trend}, momentum={f.momentum}, ...
    Context: {ctx_summary}
    
    What's the entry thesis? Return JSON:
    {{"thesis": "momentum|breakout|reversion|event|technical_setup",
      "key_evidence": "短句",
      "expected_horizon_hours": int,
      "thesis_validity_check": "what to monitor"}}
    """
```
写入 trade_attribution.entry_thesis, ExitDispatcher 路由对应 ExitPolicy.

**可控性**:
- 异步 fire-and-forget, 不阻塞下单
- thesis ∈ 5 个枚举值, 其他 fallback 到 momentum (默认)
- ExitDispatcher 用 thesis 选 ExitPolicy, 但 thesis 缺失时退化到 main

---

### P0-L4 — Earnings/Events 处理缺失 ⭐⭐⭐
**现状**: `earnings_risk_guard.py` 仅基于日历做风控 (财报前 24h 收紧止损), 无 LLM 解读财报内容 / guidance / 分析师反应.

**方案**:
```
app/llm/earnings_analyst.py
- 财报发布后 5 分钟内: LLM 分析 EPS beat/miss + guidance + outlook
- 输出: post_earnings_signal (BUY/HOLD/SELL + confidence + horizon)
- 仅在持仓 symbol 触发 (避免遍历全市场)
- 24h 后回看准确率, 写 earnings_attribution.db
```

**可控性**:
- 输入: 仅持仓 symbol 的财报, 不主动遍历
- 输出: 必须经 RiskKernel + thesis check, 不直接平仓

---

### P1-L5 — 多模型分歧仲裁 ⭐⭐⭐
**现状**: voting_coordinator 加权聚合, 但 4 模型严重分歧时 (Qwen=BUY, DeepSeek=SELL) 当前用 weighted_avg 输出 NEUTRAL → 信号被压制丢失.

**方案**: 分歧度 > 阈值 → 启动 `iterative_debate` (已存在但默认关闭):
```python
if vote_disagreement > 0.5:  # 分歧度
    final = iterative_debate.run(
        candidates=[qwen_view, deepseek_view],
        max_rounds=2,
        moderator="qwen-max"  # 第三方模型仲裁
    )
```

**可控性**:
- 仅高分歧时启用 (~10% 调用占比)
- max_rounds=2, 单次 < 60s
- 预算 cap: 每日 debate 不超过 10 次

---

### P1-L6 — 场景分类器 ⭐⭐⭐
**现状**: 当前用 `regime` (TREND_UP/DOWN/RANGE/HIGH_VOL/LOW_VOL) 5 分类, 基于 VIX + SPY MA. 信号系统对所有场景一刀切.

**方案**: LLM 每 30 分钟做"场景诊断":
```json
{
  "regime": "morning_drive_chop|trend_continuation|reversal|news_driven|liquidity_thin",
  "key_drivers": ["..."],
  "best_strategies": ["mean_reversion", "wait"],
  "avoid_strategies": ["breakout"],
  "vix_outlook": "stable|spike_likely",
  "horizon_hours": 1.0
}
```
写 `market_scenario.db`, IntradayScorer 取场景调整因子权重.

**可控性**:
- 每 30 分钟 1 次, 每日 ~13 次, 成本 ~$0.20/日
- 场景标签必须在白名单, 自由文本 drop
- 场景驱动调权严格上限 (单次 ±10%)

---

### P1-L7 — Pre-trade LLM 单笔确认 (大头寸) ⭐⭐
**现状**: 因子信号通过 RiskGateway 即下单, 大头寸 (>5% equity) 无 LLM 二次审视.

**方案**: position_pct > 5% 时同步调 LLM (60s 超时):
```
Are there any anomalies in this trade?
Check:
- Is there material news in last 4h not yet reflected?
- Any related ticker (sector peer) showing opposite move?
- Any earnings/event in next 48h?

Output: GREEN_LIGHT | YELLOW_REDUCE_TO_3PCT | RED_SKIP
```

**可控性**:
- 仅大头寸 (~5%-10% 信号占比)
- LLM 只能"降仓" 不能"加仓"
- 单股 1 小时 cooldown

---

### P1-L8 — AutoTuner 重写 (代替死码) ⭐⭐
**现状**: `app/strategy/auto_tuner.py` 占位 (deprecated). `weekly_proposer` 已部分接管, 但是周级别. 需要日级别 tuner 用 LLM.

**方案**: 收盘后 LLM 看 daily_review JSON 输出"明日参数微调":
```json
{
  "param_changes": [{"path":"day_trade_mode.tier_1.profit_pct","from":4.0,"to":4.5,"reason":"今日 4% 触发后回吐"}],
  "confidence": 0.6,
  "trigger_mode": "shadow_only|canary|full"
}
```
默认 shadow_only, 7 天 shadow 优于 → 升 canary, 30 天 canary 优 → 升 full.

**可控性**:
- 必走 ConfigWriter PARAM_MAP 白名单 (已有 16 项)
- 默认 shadow, 不直接改主线
- 每日提议 ≤ 3 项

---

### P2-L9 — 新闻 → 信号生成
基于 P0-L1 新闻分类器, 严重事件直接生成 owner=llm_news 信号. 与因子信号竞争 SignalArbitrator.

### P2-L10 — LLM 复盘对话
启动 `python scripts/why.py NVDA 2026-05-14` → LLM 解读这笔交易完整链路.

### P2-L11 — 行业关联推理
TSMC drop → NVDA/AMD 关联评估. 比 sector_limits.py 静态映射更灵活.

### P2-L12 — 多语言新闻整合
中文央行 / 日文日银会议 / 英文财报混合处理.

---

## 3. 可控性 6 条原则

无论加什么 LLM 能力, 必须满足:

| 原则 | 实施 |
|------|------|
| **预算硬墙** | `llm_budget.daily_max_usd` (已 5 USD), 加 per-feature cap; 100% 时硬停 LLM |
| **输出 schema 校验** | 所有 LLM 输出必须 JSON 强制 schema, 失败 → drop + log; 不允许自由文本影响仓位 |
| **建议-决策分离** | LLM 出"建议", 代码执行"决策" (RiskKernel + cooldown + capital cap 二次校验) |
| **A/B 隔离** | 重大新功能默认 shadow, 24h-7天 burn-in 后才升主线 |
| **回滚能力** | LLM 写入 (proposal/config) 必须有 rollback API; ConfigWriter 已支持 |
| **离线 fallback** | LLM API 故障 → 退化纯因子模型, 不停机 |

### 已有保障
- ✅ MultiModelClient 预算 + cost tracking
- ✅ ConfigWriter PARAM_MAP 白名单 + max_delta
- ✅ premarket_planner / attribution JSON 结构化输出
- ✅ proposals.db 暂存待回测

### 待补
- ⚠ news_v2 / earnings_analyst 的 schema 校验
- ⚠ LLM 决策类输出 (audit / emergency_review) 的 cooldown 表
- ⚠ Shadow executor 接 LLM 决策路径 (Phase C 配套)

---

## 4. 优先级路线图 + 价值估算

### P0 (今晚验收 + 明日 Phase B 后启动)
| ID | 工作量 | 价值 | 触发条件 |
|----|-------|------|---------|
| L1 新闻 LLM 解读 | 6-8h | ⭐⭐⭐⭐⭐ alpha 来源 | Phase B 后启动 |
| L2 LLM 风控 gating | 4-6h | ⭐⭐⭐⭐ 黑天鹅保护 | Phase B 后 |
| L3 entry_thesis 推断 | 3-4h | ⭐⭐⭐ 与 F16/Phase B 配套 | 与 Phase B 同步 |
| L4 Earnings analyst | 4-6h | ⭐⭐⭐ | 财报季前 |

### P1 (本月)
| ID | 工作量 | 价值 |
|----|-------|------|
| L5 分歧仲裁 | 2-3h | ⭐⭐⭐ |
| L6 场景分类 | 4-5h | ⭐⭐⭐ |
| L7 大仓位 LLM 确认 | 2-3h | ⭐⭐ |
| L8 日级 AutoTuner | 6-8h | ⭐⭐ |

### P2 (本季)
- L9 新闻信号生成 (依赖 L1)
- L10 复盘对话
- L11 行业关联
- L12 多语言

---

## 5. 成本估算 (启用全部 P0 后)

| 调用 | 频率 | 模型 | 单次成本 | 日累计 |
|-----|------|-----|---------|-------|
| premarket_planner | 1/日 | Qwen-Max | $0.05 | $0.05 |
| 新闻分类 (L1) | ~50 条/日 | MiniMax | $0.005 | $0.25 |
| LLM 风控 (L2) | ~5 触发/日 | Qwen-Max | $0.04 | $0.20 |
| thesis 推断 (L3) | ~20 信号/日 | DeepSeek | $0.01 | $0.20 |
| Earnings (L4) | ~3 持仓/日 | Qwen-Max | $0.06 | $0.20 |
| attribution (现有) | ~20 笔/日 | Qwen-Max | $0.02 | $0.40 |
| weekly_proposer (现有) | 1/周 | DeepSeek | $0.30 | $0.04/日均 |
| eod_review (现有) | 1/日 | Hermes | $0.15 | $0.15 |
| **合计** | | | | **$1.49/日** |

**预算空间**: yaml `daily_max_usd: 5.0`, 实际 ~30%, 留 70% buffer 给突发使用.

---

## 6. 期望胜率 / 收益提升

> 估算基于行业经验 + 已有数据回测假设, 模拟盘验证后修正.

| 增强项 | 单项 alpha 估算 | 累计估算 (考虑相关性) |
|-------|---------------|--------------------|
| 现状 baseline | - | win_rate ~45%, avg_pnl ~+0.2% |
| + L1 新闻解读 | +5% win_rate | win_rate ~50% |
| + L2 LLM 风控 | -30% max drawdown | sharpe +0.2 |
| + L3 thesis-driven exit (配 F16) | +0.3% avg_pnl | avg_pnl ~+0.5% |
| + L4 Earnings | +10% earnings 季节胜率 | (季节性) |
| + L5 分歧仲裁 | +2% win_rate (分歧场景) | (~10% 信号占比) |
| + L6 场景分类 | +5-8% win_rate (合适场景) | win_rate ~55% |
| **全部 P0+P1 上线 (3 月)** | | win_rate ~52-55%, avg_pnl ~+0.5%, sharpe ~1.5 |

---

## 7. 实施建议 (实施顺序)

**今晚开盘**: 仅验证现有 P0 修复 (Phase A)

**明日**: Phase B 启动 (与本文 L3 entry_thesis 互补 — 一起做)

**Phase B 完成后启动**:
1. **第 1 周**: L1 新闻 LLM 解读 (alpha 价值最高)
2. **第 2 周**: L2 LLM 风控 + L4 Earnings (防御端)
3. **第 3 周**: L5 分歧仲裁 + L6 场景分类
4. **第 4 周**: L7 大仓位确认 + L8 日级 AutoTuner

**Shadow 验证策略**:
- 每个 L 项默认 shadow 模式 1 周
- shadow PnL 优于 baseline → 升 canary (10% 流量)
- canary 2 周 优 → 升 full

**风险熔断**:
- 任意 LLM 单日预算超 80% → 该 module 自动 disable 24h
- LLM 决策导致单股亏损 > 5% → emergency_review 该 module 拉清单
- 连续 5 天 shadow PnL 劣于 baseline → 该 module rollback 旧版本

---

**结论**: LLM 现状是 **70% 设计完成, 30% 实际启用**. 最大缺口在新闻处理 (alpha 端) 和 LLM 风控 (防御端). P0 全部完成后预期提升 win_rate 5-10 个百分点 + 显著降低尾部风险, 月成本可控在 $50 以内 (~预算 30%).

下一步选择: ① Phase B 完成后启动 P0-L1 (新闻) ② 或先做 P0-L3 (entry_thesis) 与 Phase B 一起 ③ 其他.
