# LLM 在交易链路中的角色审查 + 优化建议 (2026-05-19)

> **结论**: LLM 在交易链路中**实际几乎不起作用**. 11 个 features 7 个禁用, 4 个启用但 7 天累计 0% 成功率 / 0 实际触发. **设计与执行严重背离**.
>
> **核心矛盾**: 5/17 D-DAY 重构了 LLM 架构 (model_call.py 4 模型 yaml 路由), 但**没人调用** — PostTradeHook 0 触发, 主线 entry 极少, news_classifier 仍走旧 llm_helper 路径.

---

## 1. LLM 链路全景 (设计 vs 现状)

### 1.1 设计目的 (按交易链路位置)

```
盘前 (4:00 ET)         盘中 (9:30-16:00)             盘后 (16:00+)
─────────────       ─────────────────────       ───────────────
premarket_planner    主线 entry → thesis_inferer  daily_review
weekly_proposer       高仓位 → large_position_review  daily_auto_tuner
                      新闻流 → news_classifier      weekly_proposer (周日)
                      30min → regime_classifier
                      财报 → earnings_analyst
                      风控 → emergency_review
                      行业 → sector_correlation
                      分歧 → disagreement_arbitrator
```

### 1.2 实际状态 (5/12-5/19 7 天)

| Feature | 设计阶段 | yaml enabled | 7d 触发 | 7d 成功率 | 实际作用 |
|---|---|---|---:|---:|---|
| news_classifier | 盘中 (新闻情绪) | **true** | 16 | **0%** | 🔴 完全失败 (走旧 llm_helper, response_len=0) |
| thesis_inferer | 入场后 | **true** | 5 (5/15) | **0%** | 🔴 D-DAY 前 100% schema 失败, D-DAY 后 0 触发 |
| premarket_planner | 盘前 | (无 flag) | 1/日 | 部分 | ⚠️ 输出 plan 文件 (3 targets), 但实际下单与 plan 无关 |
| daily_review | 盘后 | (subprocess) | 1/日 | 部分 | ⚠️ 5/19 04:42 timeout 3 次 fallback |
| weekly_proposer | 周末 | (无 flag) | 0 | - | 0 触发 |
| daily_auto_tuner | 盘后 | false | 0 | - | 未启用 |
| regime_classifier | 30min/次 | false | 0 | - | 未启用 |
| earnings_analyst | 财报 | false | 0 | - | 未启用 |
| disagreement_arbitrator | 入场 | false | 0 | - | 未启用 |
| large_position_review | 入场 (>5%) | false | 0 | - | 未启用 |
| emergency_review | 风控 | false | 0 | - | 未启用 |
| sector_correlation | 行业 | false | 0 | - | 未启用 |

**LLM 成本**: 7 天累计 ~$0.05 (基本是 test_feature_xyz 跑测试)
**LLM 价值**: 0 (无任何 LLM 决策实际影响交易)

---

## 2. 4 个根本性问题

### 2.1 🔴 双套 yaml 配置 + 双套 call_llm 实现 (导致路由混乱)

**配置层**:
```yaml
# 老 (line 183-): llm_features.<name>.enabled / model
llm_features:
  news_classifier:
    enabled: true
    model: minimax        # 老调用走 multi_model_client → MiniMax-M2.5

# 新 (line 147-): llm.features.<name> = primary/fallback (D-DAY 5/17 引入)
llm:
  features:
    news_classifier: qwen-flash    # ← 实际未被任何代码读取
```

**代码层**:
- `app/llm/features/llm_helper.py:21` `call_llm()` ← 旧, 仍被 news_classifier 调用
- `app/llm/model_call.py:251` `call_llm()` ← 新, 仅被 thesis_inferer/premarket_planner 等迁移过的 features 调用

**结果**: yaml 新路由 `news_classifier: qwen-flash` 形同虚设, 实际仍调 minimax.

### 2.2 🔴 thesis_inferer / news_classifier 0% 成功率根因

**Audit 数据特征**:
```
thesis_inferer:    5 笔, latency 130-200ms, response_len=0
news_classifier:  16 笔, latency 170-220ms, response_len=0
```

`response_len=0` + 快速失败 → **不是 timeout 也不是 API 错, 是 schema 校验失败后被清空**.

**model_call.py:347-353**:
```python
if not schema_passed:
    res.response = None  # 清空
    res.error = f"schema_failed_on_{alias}"
    continue
```

**根因推测**:
- thesis_inferer schema 要求严格 (`thesis ∈ {momentum, breakout, reversion, event, technical_setup}` + 4 字段)
- LLM 实际输出可能含 markdown 包裹 (`​```json{...}​````), 或字段名大小写不一, 或多余字段
- 老 multi_model_client 容错宽松, 但新 schema_required 一票否决

### 2.3 🔴 PostTradeHook 0 触发 → thesis_inferer 链路实际未运行

**5/18 实测**:
```
[shutdown] PostTradeHook stats: {submitted: 0, completed: 0, failed: 0}
```

PostTradeHook 在 `order_executor._execute_long/short` 入场后才触发. 但 5/18 主线只有 2 笔 ARM (legacy close, 不走入场路径). **scalping 110 笔走自己的路径, 不调 PostTradeHook**.

→ thesis_inferer 设计为"主线入场后推断", 但主线几乎不入场, 链路空跑.

### 2.4 🔴 premarket_planner 输出与实际交易脱节

**5/18 plan**:
```json
{
  "date": "2026-05-18",
  "targets": [
    {"symbol": "US.NVDA", "entry_zone": [136.5, 138.2]},
    {"symbol": "US.MU",   "entry_zone": [167.0, 169.5]},
    {"symbol": "US.BWXT", "entry_zone": [114.5, 116.0]}
  ],
  "restricted": []
}
```

**5/18 实际成交**: ARM × 2, LCID × 2, TME × 3, CRM/NFLX/ORCL × 2 — **0 笔在 plan.targets 内**.

→ LLM 给出了"今日重点关注 3 标的", 但下单系统完全无视这个建议.

---

## 3. 优化建议 (按价值排序)

### Phase 1: 修 LLM 链路根因 (本周内)

#### 🎯 L1: 统一 yaml 配置 + 双套 call_llm 合并

**改动**:
1. 删除 `llm_features:` 段, 全部迁到 `llm.features:` 新路由
2. 把 `news_classifier` 内部 130 行迁到新 `model_call` (STAGE_2 跳过的)
3. 删除 `app/llm/features/llm_helper.py:call_llm`, 全代码库改 import `app.llm.model_call`

**预期**: 配置清晰, news_classifier yaml route `qwen-flash` 实际生效

#### 🎯 L2: 修 schema 0% 成功率

**改动 (model_call.py)**:
```python
# 1. schema 失败时记录 raw_response 前 200 字符 (而非清空)
if not schema_passed:
    _record_audit(feature, res, prompt, raw_response_preview=res.response[:200])
    res.response = None
    continue

# 2. 增加 schema 容错: 自动剥离 markdown ```json``` 包裹
def _strip_md(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]  # 剥外壳
        if text.startswith(("json", "JSON")):
            text = text[4:]
    return text.strip()
```

**预期**: thesis_inferer / news_classifier 成功率从 0% 提升到 ≥ 70%

#### 🎯 L3: scalping 路径接入 PostTradeHook (释放 110 笔/天 LLM 数据)

**改动 (scalping/engine.py:_execute_trade 成功后)**:
```python
# 现状: scalping 自管, 不触发 PostTradeHook
# 改: 与主线一致触发
try:
    from app.pipeline.post_trade_hook import on_position_opened
    on_position_opened(position, signal_obj, signal_context, scheduler)
except Exception as e:
    logger.debug(f"[Scalping·PostTradeHook] {e}")
```

**预期**: 110 笔 scalping 全部触发 thesis_inferer + ShadowMirror + push, **LLM 数据从 0 → 110/天**

#### 🎯 L4: premarket_planner 与主线 scan 联动

**改动 (signal_pipeline.py 扫描循环)**:
```python
# 1. 优先扫描 plan.targets (LLM 选定的高 alpha 标的)
plan = USBattlePlanManager().get_plan()
priority_symbols = [t["symbol"] for t in plan.get("targets", [])]
all_symbols = priority_symbols + [s for s in WATCH_LIST if s not in priority_symbols]

# 2. 如果信号来自 plan.targets 且 entry_zone 命中, 优先级 +20
if symbol in priority_symbols:
    target = next(t for t in plan["targets"] if t["symbol"] == symbol)
    zone = target["entry_zone"]
    if zone[0] <= price <= zone[1]:
        signal.priority_bonus = 20
```

**预期**: LLM 盘前认知传递到入场决策, plan 不再是孤岛.

---

### Phase 2: 启用关键 LLM features (2 周内)

#### 🎯 L5: 启用 regime_classifier (替代 router 的简单规则)

**现状**: scalping C.1 修复用 `signal_context["regime"]` 来自 `market_data_router.get_full_market_context()` — 用 RSI/MACD 简单算的, 不准.

**改动**:
1. `unified_config.yaml: llm_features.regime_classifier.enabled = true`
2. 30min 调度 LLM 分类: TREND_UP / TREND_DOWN / RANGE / TRANSITION
3. 缓存到 `data/regime_cache.json` (30min 有效)
4. scalping/main 入场前读 cache (零额外 latency)

**预期**: regime 判定准确率从 ~70% (router 规则) → ≥ 90% (LLM 综合判断), C.1 LONG gate 误杀率下降.

#### 🎯 L6: 启用 disagreement_arbitrator (主线高分歧仲裁)

**现状**: 主线 voting (5 model agent), 共识阈值 0.60. 当 consensus = 0.5-0.65 模糊区间时, 直接拒绝或硬通过.

**改动**:
1. `disagreement_arbitrator.enabled = true`
2. consensus ∈ [0.55, 0.70] 时调用 deepseek-pro 仲裁
3. 输入: 各 model 的 vote + factor scores
4. 输出: final_decision + rationale → 写 trade_attribution

**预期**: 模糊信号区间从"误判 50%"→"LLM 决策 70%+", 提升主线胜率 5-10%.

#### 🎯 L7: 启用 daily_auto_tuner (LLM 自动调参)

**现状**: scalping cooldown / 阈值靠人手改. Phase 1 5 项修复都是手工调.

**改动**:
1. `daily_auto_tuner.enabled = true`
2. 每日 04:30 ET 触发, 输入: 当日 daily_review.json + 7d trade_records
3. 输出: 5-10 条参数调整建议 (e.g., "scalping breakeven_after 6→8min based on 5/19 数据")
4. 写 `data/auto_tuner_proposals.db`, 人工审核后应用

**预期**: 调参速度从"每周手工"→"每日 LLM 建议", 系统自适应市场变化.

---

### Phase 3: 战略层 LLM 应用 (1 月+)

#### 🎯 L8: TopK 候选 LLM 仲裁

主线扫描出 N 个候选信号 → 取 strength Top-3 → LLM 综合判断"今天买哪个"
- 输入: Top-3 信号 + 当日 plan + 当前持仓 + VIX
- 输出: rank + reason
- 触发: 5min 一次, 仅当 K ≥ 2

#### 🎯 L9: post-trade thesis 完整闭环 (现仅设计未生效)

thesis_inferer 推断 "momentum/breakout/...", ExitPolicy 按 thesis 选退出策略:
- momentum → trailing stop wide (跟随趋势)
- breakout → quick TP at 1.5×ATR
- reversion → tight SL + small TP
- event → time-based (event 后 4h 平)

→ 现 PB-1 已写代码 (`exit_policies.py`), 但 thesis_inferer 0 成功 → 全走默认.

#### 🎯 L10: 失败模式 LLM 复盘 (替代死板 5 因子)

现 `noise_pnl=-$15k` 持续 5 天 不变. behavioral_score=84 grade=B 死板.
→ LLM 每周扫历史败局, 找模式 (e.g., "盘前 30min 内的 LONG 全亏" → 自动加 cooldown).

---

## 4. 成本与可观测性

### 4.1 7 天成本预算 (启用所有建议后)

| Feature | 频次 | 模型 | 单次成本 | 7d 累计 |
|---|---|---|---|---|
| premarket_planner | 1/日 | qwen-plus | $0.02 | $0.14 |
| regime_classifier | 30min × 7h × 5d | qwen-plus | $0.005 | $0.50 |
| thesis_inferer (启用 scalping) | 110/日 | deepseek-flash | $0.001 | $0.77 |
| news_classifier (修后) | 50/日 | qwen-flash | $0.0005 | $0.18 |
| disagreement_arbitrator | 5/日 | deepseek-pro | $0.02 | $0.70 |
| daily_auto_tuner | 1/日 | deepseek-pro | $0.05 | $0.35 |
| daily_review | 1/日 | deepseek-pro | $0.10 | $0.70 |
| **合计** | | | | **~$3.34/周** |

合理预算 (相对预期 +$2,400/周 收益, ROI 700×).

### 4.2 必加可观测性指标

```sql
-- LLM 日报 (每天 06:00 钉钉推送)
SELECT 
  feature, model,
  COUNT(*) n,
  SUM(success) ok,
  ROUND(100.0*SUM(success)/COUNT(*), 1) success_pct,
  ROUND(AVG(latency_ms), 0) avg_ms,
  ROUND(SUM(cost_usd), 4) cost
FROM llm_calls
WHERE date(timestamp/1, 'unixepoch', 'localtime') = date('now', 'localtime')
GROUP BY feature, model;

-- schema 失败 raw_response 抽样 (debug)
SELECT feature, raw_response_preview, COUNT(*) 
FROM llm_calls 
WHERE success = 0 AND raw_response_preview IS NOT NULL
GROUP BY feature, substr(raw_response_preview, 1, 50)
ORDER BY COUNT(*) DESC;
```

---

## 5. 实施优先级矩阵

| 优化点 | 改动量 | 风险 | 1 周价值 | 优先级 |
|---|:---:|:---:|---|:---:|
| L1 yaml 配置统一 | 中 | 低 | 防混乱 | P0 |
| **L2 schema 容错** | 小 | 低 | **0% → 70% 成功率** | **P0** |
| **L3 scalping 接 PostTradeHook** | 小 | 低 | **LLM 数据 0 → 110/d** | **P0** |
| L4 plan-scan 联动 | 中 | 中 | 提升 alpha 命中 | P1 |
| L5 启用 regime_classifier | 小 | 低 | C.1 修复更准 | P1 |
| L6 disagreement_arbitrator | 中 | 中 | 主线胜率 +5-10% | P2 |
| L7 daily_auto_tuner | 中 | 低 | 自适应调参 | P2 |
| L8 TopK 仲裁 | 大 | 中 | alpha 提升 | P3 |
| L9 thesis 闭环 | 小 (仅修 L2 后) | 低 | exit policy 智能化 | P3 |
| L10 LLM 复盘 | 大 | 低 | 长期 alpha 发现 | P3 |

---

## 6. 一句话回答

> **现状**: LLM 在交易链路中**形同虚设** — 11 个 features 7 个 disabled, 4 个 enabled 但 7d 累计 **0% 成功率** / 0 主线触发. 设计了完整 LLM 决策网络, 但实际**0 个 LLM 决策影响过任何交易**.
>
> **核心修复 P0** (本周, ROI 最高):
> 1. **L2** schema 容错 — 修 thesis/news 0% 成功率
> 2. **L3** scalping 接入 PostTradeHook — 释放 110 笔/天 LLM 数据
> 3. **L1** yaml 双配置统一 — 防新路由失效
>
> **战略升级 P1-P2** (2 周): 启用 regime_classifier (替 router 简单规则), disagreement_arbitrator (模糊区间仲裁), daily_auto_tuner (自动调参).
>
> **成本可控**: 7d ~$3.34, 相对预期 +$2,400 收益 ROI 700×.
