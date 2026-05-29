# FTSeek-US 盘前/盘后新闻催化辩论引擎

> **类型**: L3 新特性 | **焦点**: F1 (买卖规范) + F3 (框架逻辑清晰)
> **核心原则**: 不重复造轮子 — 100% 复用现有新闻管线 + 辩论架构 + LLM路由
> **调度**: 仅盘前 + 盘后，盘中不接入

---

## 1. 目标

盘前自动采集过夜重大新闻(财报/监管/突发)，通过最强模型充分辩论，将催化事件结论注入作战计划；盘后回看催化事件预测准确率，形成闭环。

## 2. 设计原则

| 原则 | 说明 |
|------|------|
| **不重复造轮子** | 复用: 新闻管线(newsnow/finnhub/rsshub/sec_edgar)、辩论架构(DebatePlanner并行+分歧度+Arbiter)、LLM路由(call_llm)、紧急拦截器关键词 |
| **盘中不介入** | 催化检测只在盘前(8:30-9:25 ET)和盘后(16:05-18:00 ET)运行，不影响交易热路径延迟 |
| **最强模型辩论** | qwen3.7-max(宏观推理) + deepseek-v4-pro(风控质疑) + 现有百炼模型(行业情绪)，禁用同族模型 |
| **渐进式叠加** | 新模块作为辩论上下文增强层注入，不修改辩论核心逻辑 |

## 3. 现有能力清单 (直接复用，不改)

```
已有 ✅                              新建 🔧
─────────────────────────────────    ─────────────────────────────
NewsNow (华尔街见闻/财联社/金十)      
Finnhub (英文公司新闻)                
RSSHub (WSJ/Reuters/Fed)            
SEC EDGAR (8-K/10-Q公告)            
EarningsCalendar (Alpha Vantage)     
NewsPipelineV2 (去重→相关性→重要性)   
                                    
DebatePlanner (Hermes↔Claude并行)    
  ├── Round 1: 并行独立分析           
  ├── 分歧度计算                      
  ├── Round 2: 交叉验证              
  └── Arbiter: 收敛生成计划           
                                    
call_llm(feature, prompt) 统一路由   
model_call.py 模型配置               
emergency_interceptor.py 关键词库    
news_attribution.py T+归因追踪       
```

## 4. 架构

### 4.1 盘前流程 (8:30-9:25 ET)

```
┌─────────────────────────────────────────────────────────────┐
│ 现有流程: run_premarket_planning()                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────────────────────┐                            │
│  │ 🔧 催化事件检测 (新增步骤)    │                            │
│  │                              │                            │
│  │  新闻管线输出 ──→ 分类器 ──→ 催化事件列表                  │
│  │  (NewsNow+Finnhub+SEC)   (复用关键词+LLM辅助)             │
│  │                              │                            │
│  │  催化事件 ──→ 多模型辩论 ──→ 催化结论                      │
│  │             (qwen3.7-max     {symbol, direction,          │
│  │              deepseek-v4-pro   strength, confidence,      │
│  │              minimax-m2.5)     reasoning}                 │
│  └──────────────┬───────────────┘                            │
│                 │                                            │
│                 ▼                                            │
│  _build_debate_context()                                     │
│    + catalyst_verdicts ← 🔧 注入催化结论                     │
│                 │                                            │
│                 ▼                                            │
│  DebatePlanner.generate_premarket_plan()  ← 现有，不改       │
│    (辩论时能看到催化事件 → 计划质量提升)                       │
│                 │                                            │
│                 ▼                                            │
│  daily_battle_plan_us.json                                   │
│    + catalyst_events ← 🔧 催化事件落盘                       │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 4.2 盘后流程 (16:05-18:00 ET)

```
┌─────────────────────────────────────────────────────────────┐
│ 现有流程: 盘后归因辩论 (cron c09e8387406b)                   │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────────────────────┐                            │
│  │ 🔧 催化回看 (新增步骤)        │                            │
│  │                              │                            │
│  │  盘前催化结论 vs 当日实际涨跌  │                            │
│  │       │                      │                            │
│  │       ▼                      │                            │
│  │  多模型辩论: 预测准不准？     │                            │
│  │  为什么准/不准？教训是什么？   │                            │
│  │       │                      │                            │
│  │       ▼                      │                            │
│  │  教训写入 lessons DB          │                            │
│  │  (LessonInjector 下次盘前注入) │                            │
│  └──────────────────────────────┘                            │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## 5. 模型选择

### 5.1 新增模型配置

需要在 `unified_config.yaml` 的 `llm.models` 新增两个强模型：

```yaml
llm:
  models:
    # 新增 — 催化辩论专用强模型
    qwen-max:
      provider: dashscope
      api_key_env: DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model_name: qwen3.7-max           # ← 最强Qwen
      timeout: 60
      cost_in: 0.008
      cost_out: 0.024
    deepseek-v4:
      provider: deepseek
      api_key_env: DEEPSEEK_API_KEY
      base_url: https://api.deepseek.com
      model_name: deepseek-v4-pro       # ← 最强DeepSeek
      timeout: 60
      cost_in: 0.004
      cost_out: 0.016
```

### 5.2 辩论角色分配

| 角色 | 模型 | 为什么选它 | 视角 |
|------|------|-----------|------|
| **宏观策略师** | qwen3.7-max | 中文理解最强，擅长宏观传导链推理，对A股/中概监管事件理解深 | "这个事件如何通过宏观链条传导到美股？" |
| **风控质疑官** | deepseek-v4-pro | 推理能力最强，擅长找漏洞和反面论证 | "这个判断有什么盲点？最坏情景是什么？" |
| **行业情绪分析师** | minimax-m2.5 (百炼) | 情绪分析专家，已有辩论角色prompt，成本低 | "市场情绪会如何反应？历史上类似事件怎么走？" |

**铁律**: 三个模型分属不同家族(阿里/DeepSeek/MiniMax)，确保真正独立视角。

### 5.3 辩论流程 (复用 DebatePlanner 模式)

```
Round 1: 并行 (3个模型同时调用, ThreadPoolExecutor)
  ├── qwen3.7-max:    宏观传导分析 + 方向建议
  ├── deepseek-v4-pro: 风险质疑 + 反面论证
  └── minimax-m2.5:   情绪反应 + 历史类比

Round 2: 交叉验证 (当分歧度 > 0.4 时触发)
  ├── 将三方结论合并 → qwen3.7-max 审阅并回应
  └── deepseek-v4-pro 审计回应后的结论

Arbiter: 聚合 (复用 DebatePlanner._run_arbiter 模式)
  ├── 共识度 ≥ 0.7 → 强催化信号 (strength × 1.2)
  ├── 共识度 0.4-0.7 → 标准催化信号
  └── 共识度 < 0.4 → 弱信号 (strength × 0.5, urgency=LOW)
```

## 6. 实现步骤

### Phase 1: 模型配置 + 催化事件分类器

**文件**:
- 修改: `config/unified_config.yaml` (+25行 — 新增模型 + catalyst feature)
- 新建: `app/catalyst/__init__.py` (5行)
- 新建: `app/catalyst/models.py` (40行 — dataclass定义)
- 新建: `app/catalyst/event_classifier.py` (120行)

**event_classifier.py 设计**:

```python
"""催化事件分类器 — 从新闻流提取交易相关催化事件

复用:
- emergency_interceptor.BLACK_SWAN_KEYWORDS (关键词库)
- NewsPipelineV2 输出 (已去重+过滤的新闻)
- call_llm(feature="catalyst_classifier") (LLM辅助分类)
"""

@dataclass
class CatalystEvent:
    event_id: str
    event_type: str      # REGULATORY|EARNINGS|FDA|MACRO|BREAKING|UPGRADE|DOWNGRADE
    severity: str        # CRITICAL|HIGH|MEDIUM
    tickers: List[str]   # ["US.FUTU", "US.BABA"]
    headline: str        # 原始标题
    summary: str         # 中英统一摘要
    source: str          # newsnow|finnhub|rsshub|sec_edgar
    published_at: float

class CatalystClassifier:
    """分类器 — 关键词匹配 + LLM辅助"""
    
    def __init__(self):
        # 复用 emergency_interceptor 的关键词库
        from app.us_pipeline.emergency_interceptor import BLACK_SWAN_KEYWORDS
        self.keywords = BLACK_SWAN_KEYWORDS
        # 新增: 利好关键词 (现有系统只有利空关键词)
        self.bullish_keywords = [...]
        # 中概股映射 (用于"中概股监管"→多个ticker)
        self.sector_mapping = {
            "chinese_adr": ["FUTU","BABA","PDD","BIDU","JD","NIO","LI","XPEV"],
            "ai_chip": ["NVDA","AMD","AVGO","MU","ARM","QCOM"],
            "ev": ["TSLA","RIVN","LCID","NIO","XPEV","LI"],
        }
    
    def classify(self, news_items: List[dict]) -> List[CatalystEvent]:
        """从新闻列表提取催化事件
        
        1. 关键词快速匹配 (O(n), 无LLM成本)
        2. 命中后 LLM 辅助: 确认事件类型 + 关联标的 + 生成摘要
        3. 中文标题翻译为英文摘要 (LLM一次调用)
        """
```

### Phase 2: 催化辩论引擎

**文件**:
- 新建: `app/catalyst/catalyst_debate.py` (250行)

**设计 — 最大化复用 DebatePlanner**:

```python
"""催化事件多模型辩论 — 复用 DebatePlanner 并行+分歧度+Arbiter 模式

不继承 DebatePlanner (职责不同), 但复用其:
- 并行调用模式 (ThreadPoolExecutor)
- 分歧度计算逻辑
- Arbiter 聚合模式
- call_llm() 统一路由
"""

class CatalystDebate:
    """催化事件辩论引擎"""
    
    # 3视角 system prompts (新建, 但复用 debate.py 的JSON输出格式)
    PROMPTS = {
        "macro_strategist": "...",   # qwen3.7-max
        "risk_officer": "...",       # deepseek-v4-pro  
        "sentiment_analyst": "...",  # minimax-m2.5 (复用 VotingCoordinator 的 minimax prompt 风格)
    }
    
    def debate(self, event: CatalystEvent, market_context: dict) -> CatalystVerdict:
        """对单个催化事件执行3模型辩论
        
        流程完全复用 DebatePlanner 模式:
        Round 1: 并行 → 分歧度 → Round 2 (可选) → Arbiter
        """
        # Round 1: 3模型并行
        results = {}
        def call_model(role, feature_name):
            results[role] = call_llm(
                feature=feature_name,
                prompt=self._build_prompt(event, market_context, role),
                json_mode=True,
                timeout=60,
            )
        
        with ThreadPoolExecutor(max_workers=3) as pool:
            pool.submit(call_model, "macro", "catalyst_macro")
            pool.submit(call_model, "risk", "catalyst_risk")
            pool.submit(call_model, "sentiment", "catalyst_sentiment")
        
        # 分歧度计算 (复用 DebatePlanner._calculate_divergence 逻辑)
        divergence = self._calculate_divergence(results)
        
        # Round 2: 交叉验证 (分歧 > 0.4)
        if divergence > 0.4:
            results = self._cross_validate(event, results)
        
        # Arbiter: 聚合 (复用 Arbiter JSON格式)
        return self._arbitrate(event, results, divergence)
```

### Phase 3: 集成到盘前流程

**文件**:
- 新建: `app/catalyst/detector.py` (60行 — 调度入口)
- 修改: `app/pipeline/llm_pipeline.py` (+15行 — _build_debate_context 注入催化结论)

**detector.py**:

```python
"""催化事件检测调度器 — 盘前/盘后入口"""

def run_premarket_catalyst_scan(scheduler) -> List[CatalystVerdict]:
    """盘前催化扫描 — 在 run_premarket_planning 之前调用
    
    1. 从 NewsPipelineV2 获取过夜新闻 (已去重+过滤)
    2. CatalystClassifier 分类 → CatalystEvent 列表
    3. 对 severity >= HIGH 的事件启动 CatalystDebate
    4. 返回 CatalystVerdict 列表
    """

def run_postmarket_catalyst_review(scheduler, catalyst_verdicts: List) -> List[dict]:
    """盘后催化回看 — 在盘后归因辩论中调用
    
    1. 读取今日催化结论
    2. 获取各标的实际涨跌
    3. 辩论: 预测准不准？教训？
    4. 写入 lessons DB
    """
```

**llm_pipeline.py 修改点** (仅2处):

```python
# 修改1: run_premarket_planning 开头, 在 _build_debate_context 之前
catalyst_verdicts = run_premarket_catalyst_scan(scheduler)

# 修改2: _build_debate_context return dict 新增字段
return {
    ...existing fields...,
    "catalyst_events": [v.to_dict() for v in catalyst_verdicts],  # 新增
}
```

### Phase 4: 辩论 Prompt 注入催化结论

**文件**:
- 修改: `app/llm/prompts/debate.py` (+20行 — ARBITER_PROMPT 增加催化事件章节)

在现有 `ARBITER_PROMPT` 中追加:

```
【催化事件分析】(如有)
{catalyst_events_section}

⚠️ 如果存在 CRITICAL 级催化事件:
- 受影响标的必须加入 targets (方向与催化一致) 或 restricted (不确定时)
- 催化事件的 reasoning 必须出现在 target.rationale 中
- 不要在 entry_zone 中使用催化事件前的价格
```

### Phase 5: 盘后催化回看

**文件**:
- 新建: `app/catalyst/postmarket_review.py` (100行)

```python
"""盘后催化回看 — 预测 vs 实际 + 教训提取"""

def review_catalyst_predictions(scheduler):
    """
    1. 读取今日 catalytic_verdicts (从 battle_plan JSON)
    2. 获取各标的当日实际涨跌 (从 trade_records + Futu行情)
    3. 计算命中率: 预测方向 vs 实际方向
    4. 对未命中事件, call_llm(feature="catalyst_review") 分析原因
    5. 生成教训 → LessonInjector.add_lesson()
    """
```

## 7. YAML 配置新增

```yaml
llm:
  models:
    qwen-max:
      provider: dashscope
      api_key_env: DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model_name: qwen3.7-max
      timeout: 60
    deepseek-v4:
      provider: deepseek
      api_key_env: DEEPSEEK_API_KEY
      base_url: https://api.deepseek.com
      model_name: deepseek-v4-pro
      timeout: 60

  features:
    # 催化辩论 — 3视角各用最强模型
    catalyst_macro:      {primary: qwen-max,    fallback: qwen-plus}
    catalyst_risk:       {primary: deepseek-v4, fallback: deepseek-pro}
    catalyst_sentiment:  {primary: qwen-plus,   fallback: deepseek-pro}  # minimax 用于投票, 这里用 qwen-plus
    catalyst_classifier: {primary: qwen-max,    fallback: qwen-plus}     # 分类+翻译
    catalyst_review:     {primary: deepseek-v4, fallback: deepseek-pro}  # 盘后回看

catalyst:
  enabled: true
  # 盘前扫描
  premarket:
    scan_news_hours: 12          # 扫描过去12小时新闻
    min_severity: HIGH           # 低于此级别不辩论
    debate_timeout_seconds: 180  # 辩论总超时
    consensus_threshold: 0.4     # 分歧度阈值
  # 盘后回看
  postmarket:
    enabled: true
    generate_lessons: true       # 生成教训写入 lessons DB
  # 标的关联
  sector_mapping:
    chinese_adr: ["FUTU","BABA","PDD","BIDU","JD","NIO","LI","XPEV"]
    ai_chip: ["NVDA","AMD","AVGO","MU","ARM","QCOM"]
    ev: ["TSLA","RIVN","LCID","NIO","XPEV","LI"]
    big_tech: ["AAPL","MSFT","GOOGL","AMZN","META"]
```

## 8. 文件清单

| 操作 | 文件 | 行数 | 说明 |
|------|------|------|------|
| 新建 | `app/catalyst/__init__.py` | 5 | 模块入口 |
| 新建 | `app/catalyst/models.py` | 40 | CatalystEvent + CatalystVerdict dataclass |
| 新建 | `app/catalyst/event_classifier.py` | 120 | 关键词+LLM分类, 复用emergency_interceptor关键词 |
| 新建 | `app/catalyst/catalyst_debate.py` | 250 | 3模型并行辩论, 复用DebatePlanner模式 |
| 新建 | `app/catalyst/detector.py` | 60 | 盘前/盘后调度入口 |
| 新建 | `app/catalyst/postmarket_review.py` | 100 | 盘后催化回看+教训 |
| 修改 | `config/unified_config.yaml` | +35 | 新增模型+features+catalyst section |
| 修改 | `app/pipeline/llm_pipeline.py` | +15 | _build_debate_context注入催化结论 |
| 修改 | `app/llm/prompts/debate.py` | +20 | ARBITER_PROMPT增加催化章节 |

**总计**: ~645行 (远低于v1方案的1040行, 因为大量复用)

## 9. 关键设计决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 辩论模型 | qwen3.7-max + deepseek-v4-pro + qwen-plus | 用户指定最强模型, 三家族确保独立视角 |
| 盘中不接入 | 仅盘前+盘后 | 用户明确要求避免影响交易延迟 |
| 信号注入方式 | 注入debate_context而非直接生成SignalCandidate | 复用现有辩论→计划→管线链路, 不绕过风控 |
| 分类器策略 | 关键词快速匹配 + LLM辅助确认 | 关键词零成本过滤95%无关新闻, LLM只处理命中项 |
| 中文桥接 | 分类器内部LLM翻译 | 不引入新模块, 一次LLM调用同时完成分类+翻译+摘要 |
| 盘后闭环 | 教训写入lessons DB | 复用LessonInjector, 下次盘前自动注入 |

## 10. 验证计划

### Gate 5 清单
- [ ] `event_classifier` 能从 NewsNow 新闻中正确识别 "证监会对富途监管" → REGULATORY, CRITICAL, [US.FUTU]
- [ ] `catalyst_debate` 3模型全部返回有效JSON (无超时)
- [ ] 催化结论正确出现在 `daily_battle_plan_us.json` 的 targets/restricted 中
- [ ] 盘后回看正确计算命中率并写入 lessons
- [ ] 无催化事件时, 盘前计划生成不受影响 (graceful degradation)
- [ ] 中文新闻标题正确翻译为英文摘要

### 影子测试
- 部署后以 `catalyst.enabled: true` 但 `dry_run: true` 运行1周
- 记录: 检出催化事件数 / 辩论方向 vs 实际涨跌 / 误报率
- 启用条件: 辩论方向准确率 ≥ 55%

## 11. 风险

| 风险 | 对策 |
|------|------|
| qwen3.7-max/deepseek-v4-pro 超时 | call_llm 已有 fallback 机制, 自动降级到 qwen-plus/deepseek-pro |
| 催化事件过多导致辩论耗时 | 只对 severity≥HIGH 的事件辩论, MEDIUM 直接评分 |
| 新闻管线本身故障 | 分类器无新闻输入时返回空列表, 不影响盘前计划 |
| 催化信号与Battle Plan方向冲突 | Arbiter Prompt 明确要求催化事件优先标注, 但不绕过风控 |
