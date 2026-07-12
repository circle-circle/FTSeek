# 方案: Deep Analysis 驱动交易 — 充分规划→执行

> 状态: 草案 (待用户补充优化后决策执行)
> 日期: 2026-07-08
> 作者: Hermes Agent (草案), 用户 (待补充)
> 关联: DEB-20260708-obsidian-knowledge-archive.md, deep-stock-analysis skill

---

## 1. 问题陈述

### 1.1 当前痛点

| 痛点 | 根因 | 影响 |
|------|------|------|
| 3模型投票经常 NEUTRAL → 零指令产出 | 3模型看同一份 snapshot,数据相同→strength 聚在 40-60 区间→聚合后 NEUTRAL | 日均产出 1-3 条而非假设的 3-5 条 |
| 实时 LLM 调用在交易窗口内 | voting 在盘中 9:30-16:00 调 3 次 LLM API | 占用 30-60s/标的,延迟下单,API 限频风险 |
| 3模型无信息增量 | 同一 snapshot + 同一 prompt → 3 次相同判断 | 投票=冗余,不是交叉验证 |
| deep_analysis 已有 8 维数据但不参与方向决策 | deep_analysis 只写 signal_snapshots.db 做 IC 追踪,不注入候选/方向 | 最深度的分析被浪费 |
| 候选池窄 (27 静态 + ~50 动态) | MOVER_POOL(20只)+dragonhead(~50只)只做已有池的 quote 扫描 | COP/OXY/NET 等标的永远不会出现 |
| 板块轮动无叙事 | premarket_scanner 有 ETF 排名但无个股→板块映射 | "资金从 AI 切向能源"这种判断做不了 |

### 1.2 对标差距

参考报告(2026-07-08 机会扫描)vs FTSeek 现有能力:

| 报告能力 | 报告实现 | FTSeek 现状 | 差距 |
|---------|---------|------------|------|
| 6 渠道扫描 | movers+after_hours+short+heat+surprise+策略筛选 | 3 渠道(涨幅/跌幅/量榜) | 缺 3 个渠道 |
| 四信号评分 | catalyst/momentum/sentiment/value + override | 纯技术(MA/动量/量比/PE) | 单维度 vs 多维度 |
| 板块叙事 | "资金从 AI 切向能源" + 个股标注 | ETF 排名,无叙事,无个股映射 | 有数据无叙事 |
| 操作分桶 | 质地/超跌/趋势空/回避/观望 | top_bullish/top_bearish/watchlist | 无策略分类 |
| 触发位 | SMA10 支撑/前高阻力 + entry_hint | 交给后续 deep_analysis | 扫描层无触发位 |
| 质量闸 | 252→137,剔除原因可追溯 | 市值过滤 + 主流标的加分 | 无审计日志 |
| 自动执行 | 报告→交易 | 报告独立于交易 | 完全断裂 |

---

## 2. 核心架构

### 2.1 设计原则

**充分规划→执行**: 把 LLM 的"思考"从交易窗口内移到交易窗口外。盘中只做"执行",不做"决策"。

```
当前:  watchlist → screener → 3模型实时投票 → 聚合 → 过滤 → instruction → 执行
提议:  盘前 deep_analysis 多LLM讨论 → bridge文件(方向/策略/触发位) → FTSeek 读 bridge 直接执行
```

### 2.2 架构图

```
┌─────────────────────────────────────────────────────────────────┐
│  Deep Analysis Skill (外部脚本,盘前 8:00 ET 运行)               │
│                                                                 │
│  阶段1: 多渠道扫描 (规则,不调LLM)                                │
│    ├─ 富途: 涨幅榜/跌幅榜/量榜 (各30个)                          │
│    ├─ Finnhub: earnings surprise (今日财报+昨日超预期)            │
│    ├─ Finnhub: insider transactions (近7天MSPR异常)              │
│    ├─ (可选) 富途: 夜盘异动                                       │
│    └─ 合并去重 → 质量闸(>$2B/>$5/非杠杆ETF) → ~60-100只候选      │
│                                                                 │
│  阶段2: 四信号评分 (规则,不调LLM)                                │
│    ├─ catalyst: news关键词→claim_type分类(product/earnings/...)  │
│    ├─ momentum: MA排列/5日动量/量比 (当前已有)                    │
│    ├─ sentiment: community bull_pct/bear_pct/crowding           │
│    ├─ value: PE/PB vs行业中枢                                     │
│    ├─ featured override: momentum≥8且方向确定 → 单列featured      │
│    ├─ value trap: PE低但板块ETF跌>2% → value分override到4        │
│    └─ 综合分 = balanced均权 + 每多一渠道+0.5(封顶10)             │
│                                                                 │
│  阶段3: 板块叙事+操作分桶 (规则,不调LLM)                         │
│    ├─ 个股→sector映射 (company_profile.industry)                 │
│    ├─ 板块ETF涨跌排名 (复用premarket_scanner)                    │
│    ├─ 叙事: 最强板块均值-最弱>3% → "资金从X切向Y"               │
│    ├─ 分桶: 质地/突破型, 超跌反弹型, 趋势空, 回避, 观望           │
│    └─ 触发位: SMA10/SMA20/20日低/52周高 → support/resistance     │
│                                                                 │
│  阶段4: 市况合成 (1次LLM,宽视角)                                 │
│    ├─ 输入: 板块轮动数据 + 候选列表 + 新闻流                      │
│    ├─ LLM产出: 市况叙事 + 当日主线 + 板块强弱 + 风险事件标注      │
│    └─ ~30秒                                                      │
│                                                                 │
│  阶段5: 个股深度分析 (并行,每候选1次LLM)                         │
│    ├─ 输入: 单股8维数据(快照/K线/资金/异动/新闻/研报/情绪/基本面) │
│    ├─ LLM产出: bull_case/bear_case/verdict/execution_plan        │
│    ├─ 当前已有: llm_synth.py synthesize()                        │
│    └─ 30只并行 → ~2-3分钟                                        │
│                                                                 │
│  阶段6: 交叉辩论 (1次LLM,审查官)                                 │
│    ├─ 输入: 阶段5 per-stock分析 + 阶段4 市况叙事                 │
│    ├─ 角色: 审查官,质疑每个方向判断                               │
│    │   "COP看多但油价OPEC+增产,催化是否短暂?"                    │
│    │   "RIVN超跌反弹但EV板块弱,是否逆势?"                        │
│    ├─ 产出: 修正后的方向+置信度+风险标注                          │
│    └~ ~1分钟                                                     │
│                                                                 │
│  阶段7: 最终裁定 (1次LLM或规则)                                  │
│    ├─ 输入: 阶段5分析 + 阶段6审查 + IC反馈(claim_ic_cache)       │
│    ├─ 产出: 最终direction/confidence/entry/SL/TP/size_hint      │
│    ├─ 写入 signal_snapshots.db (IC追踪,已有)                     │
│    ├─ 写入 Obsidian symbol page (已有)                           │
│    └─ 写入 bridge JSON 文件                                       │
│                                                                 │
│  输出: data/opportunity_scan_YYYYMMDD.json                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ↓ (文件)
┌─────────────────────────────────────────────────────────────────┐
│  FTSeek 运行时 (交易管线,盘前 9:00 ET 读取)                      │
│                                                                 │
│  改动1: _read_bridge_file()                                      │
│    ├─ 读 data/opportunity_scan_YYYYMMDD.json                     │
│    ├─ 返回 instructions 列表                                     │
│    └─ 替代 _get_watchlist + screener.screen                      │
│                                                                 │
│  改动2: _build_instructions_from_bridge()                        │
│    ├─ bridge JSON → Instruction 对象                             │
│    ├─ direction/entry/SL/TP/confidence 从 bridge 来              │
│    └─ 替代 _build_instructions_from_votes (3模型投票删除)        │
│                                                                 │
│  保留 (安全机制不动):                                            │
│    ├─ risk_engine 仓位计算 (buying_power/risk_budget/max_pos)    │
│    ├─ 财报日历排除 (days_until_earnings < 3)                     │
│    ├─ 市场regime检查 (NO_TRADE时全跳)                            │
│    ├─ ExitMonitor 退出监控 (SL/TP/EOD)                           │
│    ├─ position_sync 持仓同步                                     │
│    └─ post-vote deep_analysis方向门禁 (bridge=snapshots,自动通过)│
│                                                                 │
│  执行: _check_intraday_trigger → execute_instruction → 下单     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Bridge 文件格式

```json
{
  "scan_date": "2026-07-09",
  "generated_at": "2026-07-09T08:00:00-04:00",
  "market_condition": "资金从AI/半导体momentum切向能源/价值与防御",
  "sector_rotation": {
    "strongest": ["XLE", "XLRE"],
    "weakest": ["XLI", "XLY"]
  },
  "risk_events": ["7/14 CPI", "OPEC+ 8月增产"],
  "data_quality": {
    "channels_green": ["movers", "earnings", "insider", "news"],
    "channels_empty": ["short_interest", "heat"],
    "raw_candidates": 152,
    "after_quality_gate": 87,
    "after_debate": 31
  },
  "instructions": [
    {
      "symbol": "US.COP",
      "name": "康菲石油",
      "direction": "BUY",
      "confidence": 0.65,
      "score": 6.0,
      "strategy_type": "quality_breakout",
      "sector": "Energy",
      "entry": {
        "hint": "回踩105-106",
        "price_range": [105.0, 107.0]
      },
      "stop_loss": 102.69,
      "take_profit": 115.65,
      "position_size_hint": "normal",
      "trigger": {
        "support": 105.74,
        "resistance": 109.88
      },
      "bull_case": [
        "油价霍尔木兹地缘上行带E&P走强",
        "PE~18低估+距高点回撤~20%相对错杀"
      ],
      "bear_case": [
        "OPEC+ 8月增产,催化或短暂"
      ],
      "verdict": "做多,但注意催化短暂性,仓位适中",
      "causal_chain": "地缘→油价→E&P走强→估值修复",
      "reviewer_notes": "审查官: 催化短暂但PE~18+回撤20%有安全垫",
      "signals": {
        "catalyst": 6,
        "momentum": 4,
        "sentiment": null,
        "value": 8
      },
      "channels": ["movers", "earnings"]
    },
    {
      "symbol": "US.INTC",
      "name": "英特尔",
      "direction": "SELL_SHORT",
      "confidence": 0.55,
      "score": 5.5,
      "strategy_type": "trend_short",
      "sector": "Semiconductor",
      "entry": {
        "hint": "反弹至SMA50 $115.4",
        "price_range": [114.0, 117.0]
      },
      "stop_loss": 124.2,
      "take_profit": 99.46,
      "position_size_hint": "small",
      "trigger": {
        "support": 99.46,
        "resistance": 115.4
      },
      "bull_case": [],
      "bear_case": [
        "半导体de-rating中最弱-9.7%",
        "跌破全部均线,代工/转型脆弱"
      ],
      "verdict": "轻度看空,反弹至阻力做空",
      "causal_chain": "三星财报不及AI预期→半导体估值消化→INTC最弱",
      "reviewer_notes": "审查官: 板块de-rating非个股利空,但INTC基本面最弱可做空",
      "signals": {
        "catalyst": 5,
        "momentum": 5,
        "sentiment": null,
        "value": 5
      },
      "channels": ["movers", "heat"]
    }
  ],
  "watch_only": [
    {
      "symbol": "US.WULF",
      "name": "TeraWulf",
      "reason": "Anthropic $19B结构利好 vs -8.9%破位,方向未定"
    }
  ],
  "avoid": [
    {
      "symbol": "US.AMD",
      "reason": "半导体de-rating,超跌易急反弹,轧空风险"
    },
    {
      "symbol": "US.MU",
      "reason": "同上,回避追空"
    }
  ]
}
```

---

## 4. 多LLM讨论设计

### 4.1 阶段定义

| 阶段 | 名称 | LLM调用 | 输入 | 输出 | 耗时 | 备注 |
|------|------|---------|------|------|------|------|
| 1 | 多渠道扫描 | 0 | 富途+Finnhub API | 候选列表~100只 | 秒级 | 纯规则 |
| 2 | 四信号评分 | 0 | 候选+快照+K线+news+sentiment | 评分+分桶+触发位 | 秒级 | 纯规则 |
| 3 | 板块叙事 | 0 | 评分结果+ETF排名 | 叙事+sector分组 | 秒级 | 纯规则 |
| 4 | 市况合成 | 1 | 板块数据+候选+新闻流 | 市况叙事+主线+风险 | ~30s | 宽视角LLM |
| 5 | 个股分析 | N(并行) | 单股8维数据 | bull/bear/verdict/plan | ~3min | 当前已有 |
| 6 | 交叉辩论 | 1 | 全部个股分析+市况 | 修正方向+风险标注 | ~1min | 审查官 |
| 7 | 最终裁定 | 1 | 分析+审查+IC反馈 | 最终instruction | ~30s | 裁定 |

总LLM调用: 3 + N (N=候选数,通常20-40)
总耗时: ~5-8分钟

### 4.2 阶段6 交叉辩论设计 (核心差异)

这不是3模型独立投票后聚合。是递进式辩论:

**阶段5输出**(分析官): "COP看多,PE~18低估+油价地缘催化"

**阶段6输入**(审查官): 
- 全部30只候选的分析结果
- 市况叙事(阶段4)
- IC反馈(claim_ic_cache: "product_catalyst IC=+0.56✅, macro_headwind IC=-0.07⚠️")

**阶段6产出**(审查官):
- "COP: 方向维持看多,但标注'催化短暂'(OPEC+8月增产),置信度从0.7降到0.65"
- "RIVN: 方向从轻度看多降为观望,EV板块整体弱+稀释real,逆势博反弹风险高"
- "NET: featured确认,momentum-breakout方向确定性最高,但标注'近高位不追'"

**审查官prompt设计要点**:
- 不重新分析个股(不重复阶段5的工作)
- 只做交叉验证: 个股方向 vs 板块趋势 vs IC反馈
- 输出格式: 每只标的一行修正(direction维持/降级/升级 + 理由)

### 4.3 阶段7 最终裁定

**输入**: 阶段5分析 + 阶段6审查 + claim_ic_cache

**产出**: 
- 最终direction (BUY/SELL_SHORT/SKIP)
- confidence (0-1)
- entry/SL/TP (具体价位)
- position_size_hint (normal/small/speculative)
- strategy_type (quality_breakout/oversold_bounce/trend_short/avoid/watch)

**裁定规则**:
- 阶段5和阶段6方向一致 → 维持,confidence取两者均值
- 阶段5和阶段6方向矛盾 → 降级为SKIP或watch_only
- IC反馈: 如果核心论据IC<0 → 降低confidence,标注"使用低IC论据"
- 风控: 如果板块在avoid列表 → 强制SKIP

---

## 5. FTSeek 运行时改动

### 5.1 改动范围

| 文件 | 改动 | 行数 | 风险 |
|------|------|------|------|
| app/plan/factor_engine.py | _get_stock_pool() 加 Source 3: 读 bridge 文件候选 | ~10行 | 低(纯加法) |
| app/playbook/pipeline.py | 新增 _read_bridge_file() + _build_instructions_from_bridge() | ~60行 | 中 |
| app/playbook/pipeline.py | 删除 voting 调用 (保留 voting_coordinator 代码,不调用) | ~-40行 | 中 |
| app/playbook/pipeline.py | _check_intraday_trigger: entry 条件来源从 instruction 改为 bridge entry_range | ~10行 | 低 |

### 5.2 数据流对比

**当前**:
```
_get_watchlist (27静态+factor_engine动态)
  → screener.screen (质量门槛+因子打分)
  → voting_coordinator.vote (3模型LLM)
  → aggregator.aggregate (strength≥50判方向)
  → _build_instructions_from_votes (过滤+生成instruction)
  → _check_intraday_trigger (突破/触及检查)
  → execute_instruction (下单)
```

**改后**:
```
_read_bridge_file (读opportunity_scan_YYYYMMDD.json)
  → _risk_filter (regime/earnings/buying_power检查)
  → _build_instructions_from_bridge (JSON→Instruction对象)
  → _check_intraday_trigger (entry_range检查)
  → execute_instruction (下单)
```

### 5.3 保留的安全机制 (不动)

| 机制 | 位置 | 保留原因 |
|------|------|---------|
| risk_engine 仓位计算 | app/risk/ | buying_power/risk_budget/max_position 不变 |
| 财报日历排除 | screener.py | days_until_earnings < 3 → 跳过 |
| 市场regime检查 | screener.py | NO_TRADE → 全跳 |
| ExitMonitor 退出监控 | position_exit_monitor.py | SL/TP/EOD 不变 |
| position_sync 持仓同步 | position_sync.py | Broker-DB对账不变 |
| deep_analysis方向门禁 | pipeline.py:642 | bridge方向=signal_snapshots方向,门禁自动通过 |

### 5.4 删除的流程

| 流程 | 位置 | 删除原因 |
|------|------|---------|
| 3模型LLM投票 | voting_coordinator.vote() | 决策前移到盘前deep_analysis |
| 聚合器方向判定 | aggregator.aggregate() | bridge已有方向 |
| post-vote过滤 | _build_instructions_from_votes | bridge已做过风控+分桶 |

注意: voting_coordinator 和 aggregator 的代码保留(不删除文件),只是pipeline不调用。保留是为了:
- fallback: 如果bridge文件不存在或为空,fallback到3模型投票
- IC归因: voting_coordinator的_record_decision_to_db仍用于记录决策

---

## 6. 候选发现: 多渠道设计

### 6.1 渠道清单

| 渠道 | 数据源 | API | 免费? | 当前有? |
|------|--------|-----|-------|---------|
| 涨幅榜 | 富途 | get_market_rank | ✅ | ✅ |
| 跌幅榜 | 富途 | get_market_rank | ✅ | ✅ |
| 量榜 | 富途 | get_market_rank | ✅ | ✅ |
| 夜盘异动 | 富途 | get_market_rank(after_hours) | ✅ | ❌ 需加 |
| 财报超预期 | Finnhub | /calendar/earnings | ✅ | ❌ 需加 |
| 内部人异动 | Finnhub | /stock/insider-transactions | ✅ | ❌ 需加 |
| 做空榜 | Finnhub | /stock/short-interest | ❌ 付费 | ❌ 做不到 |
| 热度榜 | 社交平台 | 专有API | ❌ | ❌ 做不到 |

### 6.2 质量闸

```
原始 ~150个去重名
  → 过闸: 市值>$2B + 价格>$5 + 非杠杆/反向/宽基ETF
  → ~60-100个合格候选
  → 四信号评分
  → top 30-50进入LLM分析
```

### 6.3 做不到的补偿

- 做空榜: 用富途的"做空排行"(如有)或insider_sentiment的bearish方向替代
- 热度榜: 用community_sentiment的post_count作为热度代理

---

## 7. 四信号评分设计

### 7.1 信号定义

| 信号 | 数据来源 | 评分逻辑 | 0-10映射 |
|------|---------|---------|---------|
| catalyst | news标题关键词→claim_type | product_catalyst=高, earnings_beat=高, valuation=中, macro=低 | 0(无催化)→10(强独有催化) |
| momentum | K线 MA/动量/量比 | 多头排列+放量=高, 空头排列=低 | 0(空头)→10(多头突破) |
| sentiment | community bull/bear_pct | bull>85%=拥挤反向, bull 60-70%=健康偏多 | 0(极端看空)→10(健康偏多) |
| value | PE/PB vs行业中枢 | PE<15+行业对比=低估, PE>60=高估 | 0(高估)→10(低估) |

### 7.2 Override 逻辑

**featured override**:
- 条件: momentum≥8 且方向确定(不是NEUTRAL)
- 效果: 即使综合分不够,单列为featured
- 案例: NET momentum=8(突破52周高)但value=1(近高位),综合分5.3被拉低,override为featured

**value trap detection**:
- 条件: PE<15(机械value高分8-10) 但板块ETF当日跌>2%(de-rating)
- 效果: value分override到4-5
- 案例: INTC PE低但半导体de-rating,机械value分8→override到5

### 7.3 综合分公式

```
综合分 = (catalyst + momentum + sentiment + value) / 计分信号数
       + 每多一渠道命中 × 0.5 (封顶10)

N/A信号不计分母 (如sentiment数据缺失,不拉低综合分)
```

### 7.4 操作分桶

| 桶 | 条件 | strategy_type | position_size_hint |
|----|------|---------------|-------------------|
| 质地/突破型 | score≥6 + momentum≥6 + 52周位置>30 | quality_breakout | normal |
| 超跌反弹型 | score≥5.5 + 5日跌>10% + 52周位置<20 | oversold_bounce | small |
| 趋势空型 | score≤4 + 空头排列 + 板块弱 | trend_short | small |
| 回避型 | 当日跌>5%且5日跌>15%且板块de-rating | avoid | none |
| 观望型 | 4<score<5.5 或 方向打架 | watch | none |

---

## 8. 板块叙事设计

### 8.1 数据来源

- 个股sector: company_profile.industry (Finnhub /stock/profile2)
- 板块ETF涨跌: premarket_scanner._scan_sector_rotation (已有,11个SPDR)
- 个股当日涨跌: snapshot.change_pct

### 8.2 叙事生成

**规则层(阶段3,不调LLM)**:
- 按sector分组统计个股涨跌均值
- 最强板块 - 最弱板块 > 3% → "轮动日"
- 标注极值个股: "OXY+5.9%, COP+4.7%领涨能源"

**LLM层(阶段4)**:
- 输入: 规则叙事 + 新闻流 + 板块ETF数据
- LLM合成: "7/7轮动日:资金从AI/半导体momentum切向能源/价值——能源(油价破$69,霍尔木兹地缘)/房地产/科技逆势收红,工业/消费/材料全线走弱。"

### 8.3 板块→个股映射

```
sector_map = {
    "Technology": ["AAPL", "MSFT", "GOOG", ...],
    "Semiconductor": ["NVDA", "AMD", "MU", "INTC", ...],
    "Energy": ["COP", "OXY", "XOM", "DVN", ...],
    ...
}
```

用于:
- value trap检测(板块ETF跌→个股value降分)
- plan_validator方向检查(LONG不在最弱板块)
- 叙事中标注"半导体de-rating非个股利空"

---

## 9. 触发位设计

### 9.1 计算

从20日K线计算:
- SMA10, SMA20
- 20日最高/最低
- 52周最高/最低(从snapshot)

### 9.2 输出

```json
{
  "entry": {
    "hint": "回踩105-106",
    "price_range": [105.0, 107.0]
  },
  "trigger": {
    "support": 105.74,    // SMA10
    "resistance": 109.88   // SMA20
  }
}
```

### 9.3 FTSeek执行

_check_intraday_trigger 当前检查:
- 突破当日high/low (突破单逻辑)

改后检查:
- 实时价格 ∈ entry.price_range → 触发
- 或 突破 resistance → 触发(突破型)
- 或 跌破 support → 触发(做空型)

---

## 10. 风控与安全

### 10.1 Bridge文件缺失/空

```
if not bridge_file or len(bridge.instructions) == 0:
    logger.warning("bridge文件缺失或空,当日不交易")
    # 可选: fallback到3模型投票 (保留voting_coordinator代码)
```

### 10.2 方向一致性

bridge的direction和signal_snapshots的direction自动一致(都由阶段7写入)。
pipeline.py:642的deep_analysis门禁自动通过,不需要改。

### 10.3 仓位安全

bridge只给position_size_hint(normal/small/speculative),实际仓位由risk_engine计算:
- normal: buying_power × max_position_pct × 1.0
- small: × 0.5
- speculative: × 0.3

### 10.4 Avoid列表

bridge的avoid列表写入FTSeek的blacklist(当日有效):
- avoid中的标的不会被开仓
- 如果已有持仓,不会自动平仓(只阻止新开)

### 10.5 回退机制

如果bridge文件不存在或解析失败:
- 选项A: 当日不交易 (安全)
- 选项B: fallback到3模型投票 (保留voting_coordinator,需要用户配置)

建议选项A,因为回退到3模型投票会引入不一致的决策质量。

---

## 11. 能实现到什么程度

### 11.1 能力矩阵

| 能力 | 实现程度 | 说明 |
|------|---------|------|
| 多渠道扫描 | 5/8渠道 | 缺做空榜/热度榜(API限制) |
| 多LLM讨论 | 3阶段(分析+审查+裁定) | 递进式辩论,非独立投票 |
| 四信号评分 | 完整 | catalyst/momentum/sentiment/value + override |
| 板块叙事 | 规则+LLM合成 | 阶段4 LLM做叙事 |
| 操作分桶 | 完整 | 质地/超跌/趋势空/回避/观望 |
| 触发位 | 完整 | SMA支撑/阻力/entry_range |
| 自动执行 | 完整 | bridge→instruction→execute |
| 安全机制 | 完整保留 | regime/earnings/buying_power/ExitMonitor |
| Obsidian归档 | 完整 | 每次分析写入symbol page(已有) |
| IC追踪 | 完整 | signal_snapshots.db(已有) |

### 11.2 做不到的

| 限制 | 原因 | 影响 |
|------|------|------|
| 做空榜/热度榜 | Finnhub免费版无API | 候选池少10-20只 |
| 审查官判断力 | LLM vs人类领域专家 | 审查质量受模型能力限制 |
| 实时市况变化 | bridge盘前生成,盘中不更新 | 如果盘中突发事件(如CPI超预期),bridge不调整 |
| 板块映射完整性 | 依赖company_profile.industry | 部分标的industry字段可能为空 |

### 11.3 实时性补偿

bridge盘前8:00 ET生成,盘中9:30-16:00执行。如果盘中突发:
- FTSeek的news_v2 pipeline已有实时新闻采集
- ExitMonitor已有SL/TP/EOD退出
- 可以在Phase 2后加"盘中bridge更新"(每小时重跑阶段4-7),但这是Phase 3+的优化

---

## 12. 实施分阶段

### Phase 1: 外部脚本 (冻结期可做)

| 子任务 | 文件 | 内容 | 预计 |
|--------|------|------|------|
| 1.1 多渠道采集 | data_collector.py | 新增 collect_after_hours, collect_earnings_surprise, collect_insider_movers | 2h |
| 1.2 四信号评分 | market_scanner.py | 重写 _quick_score → _four_signal_score | 3h |
| 1.3 板块叙事 | market_scanner.py | 重写 _market_narrative + 个股sector映射 | 2h |
| 1.4 操作分桶 | market_scanner.py | 新增 _bucketing | 1h |
| 1.5 触发位计算 | market_scanner.py | 新增 _calc_trigger_levels | 1h |
| 1.6 审查官LLM | llm_synth.py | 新增 _debate_review (阶段6) | 2h |
| 1.7 最终裁定 | llm_synth.py | 新增 _final_arbitration (阶段7) | 1h |
| 1.8 Bridge输出 | pipeline.py | 新增 _write_bridge_file | 1h |
| **小计** | | | **~13h** |

### Phase 2: 运行时改动 (冻结期后)

| 子任务 | 文件 | 内容 | 预计 |
|--------|------|------|------|
| 2.1 候选注入 | factor_engine.py | _get_stock_pool 加 Source 3 读 bridge | 0.5h |
| 2.2 Bridge读取 | pipeline.py (FTSeek) | 新增 _read_bridge_file | 1h |
| 2.3 Instruction构建 | pipeline.py (FTSeek) | 新增 _build_instructions_from_bridge | 2h |
| 2.4 删除voting调用 | pipeline.py (FTSeek) | 注释/删除vote调用,加fallback | 1h |
| 2.5 Trigger适配 | pipeline.py (FTSeek) | _check_intraday_trigger 读 entry_range | 1h |
| 2.6 Avoid列表 | pipeline.py (FTSeek) | bridge.avoid → 当日blacklist | 0.5h |
| **小计** | | | **~6h** |

### Phase 3: 验证 (冻结期后)

| 子任务 | 内容 | 预计 |
|--------|------|------|
| 3.1 盘前跑deep_analysis | 生成bridge文件,人工审查内容 | 1个交易日 |
| 3.2 重启FTSeek | 读bridge,确认候选进池 | 30min |
| 3.3 盘中执行 | 确认trigger触发→下单 | 1个交易日 |
| 3.4 盘后复盘 | 对比bridge预测 vs实际执行 vs PnL | 1h |
| **小计** | | **~2个交易日** |

---

## 13. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| bridge文件不存在/解析失败 | 低 | 当日不交易 | fallback到3模型投票(可选) |
| LLM分析方向全错 | 中 | 亏损 | risk_engine仓位控制+ExitMonitor止损 |
| 候选数据不全(COP无富途快照) | 中 | screener过滤掉 | 四信号评分用Finnhub数据兜底 |
| API限频(30只并行LLM) | 中 | 阶段5超时 | 分批+asyncio.gather+超时降级 |
| 盘中突发事件bridge不更新 | 低 | 方向过时 | news_v2实时新闻+ExitMonitor |
| 审查官LLM判断力不足 | 中 | 方向修正不准 | IC反馈持续学习+人工审查bridge |

---

## 14. 开放问题 (待用户决策)

1. **fallback策略**: bridge文件缺失时,当日不交易(选项A)还是回退3模型投票(选项B)?

2. **盘中更新频率**: bridge只在盘前生成一次,还是每小时更新?(Phase 3+优化)

3. **3模型投票代码**: 完全删除还是保留作为fallback?(建议保留代码,默认不调用)

4. **执行时机**: bridge有entry_range,是等价格进入range才触发(限价逻辑),还是开盘立即执行(市价逻辑)?

5. **仓位控制**: position_size_hint(normal/small/speculative)的乘数(1.0/0.5/0.3)是否合理?

6. **审查官模型选择**: 用哪个LLM做审查官? (qwen3.7-max? glm-5.2? 还是和阶段5同一个模型?)

7. **候选数量**: 进入LLM分析的候选上限? (30? 50? 取决于API成本和时间)

8. **做空标的**: bridge的SELL_SHORT方向是否直接执行,还是需要额外确认?

---

## 15. 后续优化方向 (Phase 3+)

- 盘中bridge更新: 每小时重跑阶段4-7, 修正方向
- 回测框架: 用历史bridge文件 vs 实际PnL做回测
- IC反馈闭环: bridge指令的IC反馈→下次阶段7裁定参考
- 多市场扩展: HK市场用同一架构
- 实时风控叠加: 盘中VIX突变→bridge动态调整仓位

---

## 附录A: 现有代码复用清单

| 现有代码 | 复用方式 | 改动 |
|---------|---------|------|
| data_collector.collect_all() | 阶段5个股数据采集 | 不改 |
| llm_synth.synthesize() | 阶段5 LLM分析 | 不改 |
| ic_writer.write_signal_snapshot() | 阶段7 IC写入 | 不改 |
| strategy_memory.update_from_trade() | 盘后战绩更新 | 不改 |
| claim_ic_cache.json | 阶段6/7 IC反馈 | 不改 |
| ObsidianWriter.write_symbol_analysis() | 阶段7 Obsidian归档 | 不改 |
| premarket_scanner._scan_sector_rotation() | 阶段3板块轮动 | 不改(只读结果) |
| factor_engine._get_stock_pool() | Phase 2候选注入 | 加Source 3 |
| risk_engine | Phase 2仓位计算 | 不改 |
| ExitMonitor | Phase 2退出监控 | 不改 |
| position_sync | Phase 2持仓同步 | 不改 |

## 附录B: 新增代码清单

| 文件 | 新增 | 类型 |
|------|------|------|
| data_collector.py | collect_after_hours_movers() | 函数 |
| data_collector.py | collect_earnings_surprise() | 函数 |
| data_collector.py | collect_insider_movers() | 函数 |
| market_scanner.py | _four_signal_score() | 方法(替代_quick_score) |
| market_scanner.py | _market_narrative_v2() | 方法(重写) |
| market_scanner.py | _bucketing() | 方法(新增) |
| market_scanner.py | _calc_trigger_levels() | 方法(新增) |
| market_scanner.py | _featured_override() | 方法(新增) |
| market_scanner.py | _value_trap_check() | 方法(新增) |
| llm_synth.py | _debate_review() | 函数(阶段6) |
| llm_synth.py | _final_arbitration() | 函数(阶段7) |
| pipeline.py (skill) | _write_bridge_file() | 方法(新增) |
| factor_engine.py | Source 3 in _get_stock_pool() | ~10行 |
| pipeline.py (FTSeek) | _read_bridge_file() | 方法(新增) |
| pipeline.py (FTSeek) | _build_instructions_from_bridge() | 方法(新增) |
