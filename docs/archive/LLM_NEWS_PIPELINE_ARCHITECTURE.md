# FTSeek LLM 新闻驱动交易管线 — 完整架构文档

> 版本: v2.0 | 日期: 2026-04-15 | 作者: FTSeek Team

---

## 1. 管线总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                    FTSeek LLM 新闻驱动交易管线                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  [数据层] ────→ [分析层] ────→ [决策层] ────→ [执行层] ────→ [复盘层]  │
│                                                                     │
│  📡 新闻源        🧠 LLM引擎     📋 SignalStore   ⚡ TradeExecutor   📊 策略复盘 │
│  📈 美股夜盘      📊 行情快照     🔗 Signal Bridge   🛡️ 风控检查      🔄 迭代引擎 │
│  🔴 实时快讯      🗺️ 事件映射     ⏰ 差异化冷却      📱 钉钉推送       💡 Prompt优化│
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 完整数据流 (端到端)

### 2.1 实时新闻触发流 (每3分钟)

```
RSSHub/Futu (每3min采集)
    ↓
rsshub_multi_source_cron.py
    ↓
push_event_impacts() ← 命中 event_impact_map.json (16类规则)
    ↓
_trigger_unified_news_strategy()  ← 一次LLM调用, 双路输出
    ├─→ DingTalk 推送 (策略卡片)
    └─→ SignalStore 注入
          ↓
    [SignalStore.add()]
          ↓ 差异化处理
          ├─ 新闻信号: 冷却600s, 过期1800s (30min)
          └─ 技术信号: 冷却120s, 过期300s (5min)
          ↓
    共识引擎 (consensus_engine.py)
          ↓
    TradeExecutor → 下单
          ↓
    策略复盘引擎 (自动记录, 30min后评估)
```

### 2.2 盘前策略推送 (08:00 交易日)

```
08:00 crontab 触发
    ↓
pre_market_strategy_push.py
    ↓
1. 采集最新新闻 (RSSHub缓存 + 富途缓存)
2. 获取美股隔夜行情 (Futu OpenD)
3. LLM 盘前策略分析 (qwen-plus + enable_thinking)
   Prompt: 新闻 + 美股行情 + 技术面 + Watchlist
   输出: 市场判读 + 主题 + 具体策略(方向/仓位/止盈止损)
4. 格式化 Markdown 推送至钉钉
5. 保存到 logs/pre_market_strategy_YYYYMMDD.md
```

### 2.3 盘后策略复盘 (16:30 交易日)

```
16:30 crontab 触发
    ↓
post_market_strategy_review.py
    ↓
1. 检查所有待评估策略 (30min到期)
2. 获取最新行情计算实际盈亏
3. 生成每日复盘报告:
   - 当日信号数 / 已评估 / 待评估
   - 盈利 / 亏损 / 平均PnL / 胜率
   - 按事件类型分类统计
4. 生成迭代建议:
   - 胜率<40% → 提高置信度门槛
   - 胜率>65% → 可提高仓位
   - 某事件类型表现差 → 调整止损
5. 推送至钉钉 + 保存到日志
```

---

## 3. 冲突解决矩阵

| 冲突 | 原因 | 解决方案 | 状态 |
|------|------|----------|------|
| 双LLM调用 | `llm_impact_analyzer` + `llm_news_strategy` 分析同一条新闻 | 合并为 `_trigger_unified_news_strategy()`, 一次调用双路输出 | ✅ 已修复 |
| 信号过期过短 | 新闻信号5min过期, 来不及执行 | 新闻信号30min, 技术信号保持5min | ✅ 已修复 |
| 冷却冲突 | 所有信号统一120s冷却 | 新闻信号600s, 技术信号120s | ✅ 已修复 |
| EventDrivenStrategy | 已注册但未激活, 不冲突 | 保持现状, 未来可替换为LLM引擎 | ⏳ 待规划 |
| llm_market_analyst | 09:15-10:00窗口限制, 不影响新闻管线 | 新闻引擎独立, 不受窗口限制 | ✅ 无冲突 |

---

## 4. 文件清单

### 4.1 核心管线文件

| 文件 | 行数 | 说明 |
|------|------|------|
| `config/event_impact_map.json` | 16规则 | 事件-影响映射 (新增8类: 卫星/AI芯片/并购/财报/能源/宏观/加密/FDA) |
| `app/factors/llm_news_strategy.py` | 643 | LLM 新闻策略引擎 (LLM + 降级规则双模式) |
| `app/factors/news_signal_bridge.py` | 314 | 信号桥接器 (策略→Signal→SignalStore + 钉钉推送 + 复盘记录) |
| `app/factors/strategy_review.py` | ~500 | 策略复盘引擎 (记录/评估/统计/迭代建议) |
| `app/signal_store.py` | 增强版 | 差异化冷却 + 差异化过期 (新闻vs技术) |
| `scripts/rsshub_multi_source_cron.py` | 920 | 统一LLM管线入口 (已消除双LLM调用) |
| `scripts/pre_market_strategy_push.py` | ~700 | 盘前策略推送 (08:00, LLM分析+钉钉) |
| `scripts/post_market_strategy_review.py` | ~200 | 盘后复盘 (16:30, 评估+迭代建议) |

### 4.2 数据文件

| 文件 | 说明 |
|------|------|
| `data/strategy_records.json` | 所有策略信号记录 (持久化) |
| `data/strategy_outcomes.json` | 所有策略评估结果 (持久化) |
| `logs/pre_market_strategy_*.md` | 每日盘前策略报告 |
| `logs/strategy_review_*.md` | 每日策略复盘报告 |

---

## 5. LLM 分析框架

### 5.1 实时新闻分析 Prompt

```
# Role: FTSeek 首席量化宏观分析师
# Input: 新闻标题 + 内容 + 事件类型 + 相关股票 + 行情数据
# Output: 严格 JSON

{
  "impact_chain": "事件→指标→个股的传导路径",
  "overall_sentiment": "BULLISH/BEARISH/NEUTRAL",
  "stocks": [
    {
      "code": "US.AVGO",
      "name": "博通",
      "direction": "STRONG_BUY/BUY/NEUTRAL/SELL/STRONG_SELL/WATCH_ONLY",
      "confidence": "HIGH/MEDIUM/LOW",
      "position_tier": "FULL/HALF/LIGHT/TRACE/SKIP",
      "urgency": "IMMEDIATE/NEXT_OPEN/WAIT_CONFIRM/MONITOR_ONLY",
      "reasoning": "具体买卖逻辑 (50字以内)",
      "stop_loss_pct": -4.0,
      "take_profit_pct": 12.0,
      "impact_score": 0.8,
      "second_order_effects": ["效应1", "效应2"],
      "risk_flags": ["风险1"]
    }
  ]
}
```

### 5.2 盘前策略分析 Prompt

```
# Role: FTSeek 首席盘前策略分析师
# Input: 最新新闻 + 美股隔夜行情 + 富途快照 + Watchlist
# Framework: 
  1. 新闻定性 (宏观/地缘/行业/财报/并购/突发)
  2. 传导路径 (事件→指标→个股)
  3. 二阶效应 (替代/流动性/情绪传染)
  4. 技术面配合 (价格/涨跌/成交量)
  5. 生成策略 (方向/置信度/仓位/止盈止损)
```

### 5.3 降级规则引擎 (LLM 不可用时)

```python
FALLBACK_RULES = {
    "收购": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "合作": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "超预期": {"direction": "BUY", "confidence": "MEDIUM", "position_tier": "HALF"},
    "不及预期": {"direction": "SELL", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "制裁": {"direction": "SELL", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "暴跌": {"direction": "SELL", "confidence": "MEDIUM", "position_tier": "LIGHT"},
    "停火": {"direction": "NEUTRAL", "confidence": "LOW", "position_tier": "SKIP"},
    # ... 17条规则
}
```

---

## 6. 持续迭代逻辑

### 6.1 反馈闭环

```
策略生成 ──→ 信号注入 ──→ 执行交易 ──→ 30min后评估 ──→ 记录结果
    ↑                                                          │
    │                                                          ↓
    └──── 迭代建议 ←──── 统计分析 ←──── 盘后复盘 ←──── 持续跟踪
```

### 6.2 自动迭代建议规则

| 条件 | 建议动作 |
|------|---------|
| 整体胜率 < 40% | 提高 LLM 置信度门槛 (仅 HIGH) |
| 整体胜率 > 65% | 提高默认仓位 (LIGHT → HALF) |
| 方向准确率 < 50% | 增加技术面验证权重 |
| 某事件类型胜率 < 30% | 对该类型收紧止盈止损 |
| 某事件类型胜率 > 70% | 提高该类信号默认仓位 |
| 平均盈亏 < -1% | 收紧止损参数 |
| 平均盈亏 > +2% | 策略表现优秀, 保持现状 |

### 6.3 策略版本管理

- 当前版本: `v1.0`
- 版本号存储在 `StrategyRecord.strategy_version`
- 每次迭代建议采纳后, 版本号递增
- 统计按版本分组, 支持版本间对比

---

## 7. Crontab 调度

```bash
# 08:00 盘前策略推送 (交易日)
0 8 * * 1-5 python3 scripts/pre_market_strategy_push.py

# 16:30 盘后策略复盘 (交易日)
30 16 * * 1-5 python3 scripts/post_market_strategy_review.py

# 每3分钟 RSSHub 新闻采集 (含实时LLM策略触发)
*/3 * * * * python3 scripts/rsshub_multi_source_cron.py
```

---

## 8. 手动操作指南

```bash
# 测试盘前策略推送 (使用模拟新闻)
python3 scripts/pre_market_strategy_push.py --test

# 真实盘前推送 (采集最新新闻 + 真实行情)
python3 scripts/pre_market_strategy_push.py

# 手动触发盘后复盘
python3 scripts/post_market_strategy_review.py

# 查看策略统计
python3 -c "
from app.factors.strategy_review import get_review_engine
engine = get_review_engine()
stats = engine.get_stats_summary()
print(json.dumps(stats, indent=2, ensure_ascii=False))
"

# 查看待评估策略
python3 -c "
from app.factors.strategy_review import get_review_engine
engine = get_review_engine()
print(f'待评估: {len(engine._pending_assessments)} 条')
for rid, at in engine._pending_assessments.items():
    from datetime import datetime
    print(f'  {rid} → {datetime.fromtimestamp(at).strftime(\"%H:%M\")}')
"
```

---

## 9. 风险控制

| 层级 | 机制 | 说明 |
|------|------|------|
| L1 | 信号冷却 | 同股票同方向冷却600s (新闻) / 120s (技术) |
| L2 | 信号过期 | 新闻信号30min / 技术信号5min |
| L3 | 熔断器 | circuit_breaker (已有) |
| L4 | 持仓上限 | max_open_positions (已有) |
| L5 | 日交易限制 | max_daily_trades (已有) |
| L6 | 置信度门槛 | LOW 信号可被降级为 SKIP |

---

## 10. 测试验证

### 10.1 已通过的测试

| 测试 | 状态 | 说明 |
|------|------|------|
| 语法检查 | ✅ | 所有 .py + .json 文件通过 |
| 模块导入 | ✅ | 3个新模块独立导入成功 |
| 管线完整性 | ✅ | 8个关键文件全部存在 |
| 冲突解决 | ✅ | 5个冲突点全部消除 |
| Crontab 调度 | ✅ | 08:00 和 16:30 已配置 |
| 钉钉推送 | ✅ | 测试推送成功 |
| 降级规则引擎 | ✅ | 收购/合作/暴跌/制裁 测试通过 |

### 10.2 待实盘验证

| 项目 | 说明 |
|------|------|
| LLM 模式 (非降级) | 需要 BAILIAN_API_KEY 正常 |
| Futu OpenD 行情 | 需要 OpenD 服务运行 |
| 信号执行 | 需要 TradeExecutor 初始化 |
| 策略复盘准确率 | 需要积累至少5个样本 |

---

**文档版本**: v1.0 | **更新日期**: 2026-04-15 | **下次审核**: 策略样本≥10后
