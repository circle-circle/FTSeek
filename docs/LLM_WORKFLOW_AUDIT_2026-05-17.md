# LLM 量化工作流可用性审计 (2026-05-17)

> **角色**: LLM 工作流审计专家
> **铁律**: LLM 仅用于盘前规划 / 盘中异步归因分类 / 盘后复盘; 不参与盘中热路径
> **范围**: 仅输出审查报告 + 配置模板 + 验证命令, 不修改核心交易逻辑

---

## [📥盘前]

### 数据源清单 (`premarket_planner.py:178-275`)
| 数据源 | 来源 | 失败兜底 |
|----|----|----|
| SPY/QQQ 期货 | `_fetch_premarket_context` | "N/A" 占位 |
| VIX | `data_router.get_vix_value()` | "N/A" |
| 10Y 国债 | external API | "N/A" |
| news_summary | RSSHub + USNewsProvider | **降级 Finnhub** (line 248-256) |
| earnings | `_get_earnings_info(watchlist)` | 空列表 |
| watchlist | yaml watchlist 段 | hardcode 默认 |
| price_calibration | **O6 (2026-05-16) 自动校准** (line 188-234) | 沙盒 universe / watchlist 前 15 只 |
| lessons | `LessonInjector.generate_battle_plan_injection` | "无活跃教训" 兜底字符串 |
| exposure_context | scheduler._exposure_context | {} |

### 调用链 / 共识逻辑
```
_fetch_premarket_context (~5s)
    ↓
_fetch_news_summary  (RSSHub + Finnhub fallback)
    ↓
_get_earnings_info / price_calibration / lessons
    ↓
US_SYSTEM_PROMPT.format(...) + user_input
    ↓
debate_enabled? ─┬─ Y → enhanced_debate (已禁用, line 357 死代码)
                 └─ N → _call_llm(prompt) (单 qwen-max)
    ↓
_parse_response (json.loads + 容错抽取)
    ↓
_save_plan → daily_battle_plan_us.json
```
**实测**: enhanced_debate **已死** (line 357 `debate_enabled = False`), 实际是**单模型单次调用**, 无共识仲裁.

### 校验 / 缓存机制
- **JSON 容错抽取**: `app/llm/features/json_schema_guard.py:parse_json_strict` ✓
  - 支持 ```json fenced / 平衡块抽取 / 直接 parse 3 路径
- **严格字段校验**: ❌ 无. entry_zone 是否为 [low,high] / direction 是否 LONG|SHORT 未强校验, 仅 USBattlePlanManager 消费时部分校验
- **缓存**: `data/daily_battle_plan_us.json` 本地文件
- **盘中零网络依赖**: ✓ FastLane 走本地 `USBattlePlanManager.get_target_info(symbol)` 内存查
- **F3 重启校验** (`main.py:203-218`): `mgr.revalidate_on_restart()` 失败进 RISK_OFF

### 失败降级路径
| 失败点 | 当前行为 | 评估 |
|----|----|----|
| news_summary < 5 条 | ✅ Finnhub 降级补充 | OK |
| price_calibration 空 | ✅ O6 自动 fetch | OK |
| LLM 调用 timeout/error | ❌ `raise Exception("LLM 调用失败")` (line 408) | **缺降级到昨日 battle_plan 缓存** |
| JSON parse 失败 | ❌ raise (无字段 dict fallback) | **应改: 至少返回空 targets + warning** |
| schema 校验失败 | ❌ 无 schema 校验 → 错误字段穿透到 FastLane | **新建 plan_schema.json 必要** |

---

## [⚡盘中]

### 异步队列设计 — **当前裸 Thread, 无队列**

**`thesis_inferer.py:35-50`**:
```python
def infer_thesis_async(symbol, signal_dict, context):
    if not is_enabled("thesis_inferer"):
        return
    def _run(): ...
    thread = threading.Thread(target=_run, daemon=True, name=f"ThesisInferer-{symbol}")
    thread.start()      # ← 裸 fire-and-forget, 无 maxsize, 无 semaphore
```

**`news_classifier.py:130 classify_news`**: 是**同步函数**, 不是异步. 调用方需自己包 Thread.

| 设计点 | 现状 | 风险 |
|----|----|----|
| maxsize 背压 | ❌ 无 — Thread 无界堆积 | 短时大量入场 (例: 开盘 9:30) 可能起 50+ 线程 |
| 超时丢弃 | ⚠ LLM 单次 `timeout=15s` (yaml), 但 Thread 本身不超时 | 若 LLM 卡死, 线程不退出 (但 daemon=True, 进程退出会清) |
| 规则引擎降级 | ❌ 无 — 失败仅 log debug | 复盘看不到失败次数 |

### 背压 / 丢弃策略
**实测**: 全无. 设计意图是 fire-and-forget, 但**5/15 实测 14 笔交易**, 入场速度低于背压压力, 暂未出现堆积. 实盘高频日 (>50 笔) 时需关注.

### 规则降级逻辑
- `thesis_inferer` 失败 → trade_attribution.entry_thesis 留空 → ExitPolicy fallback `NO_ACTION` → PositionExitMonitor 6 规则接管 ✓ (PB-3 设计)
- `news_classifier` 失败 → news 标签为 "unknown" → 主线决策不依赖, 仅复盘看不到分类

### 热路径隔离验证命令
```bash
# 验证: signal_pipeline._process_symbol_impl 路径 0 LLM 调用
grep -rnE "infer_thesis|llm_helper|call_llm|multi_model_client" \
  app/core/intraday_scorer.py \
  app/pipeline/signal_pipeline.py \
  app/strategy/pure/{technical,fast_lane}.py \
  | grep -v "__pycache__\|test_"
# 预期: 0 行 ✓

# 验证: 主线下单后 LLM 都是 async (fire-and-forget)
grep -nE "Thread.*ThesisInferer|threading.Thread" app/llm/thesis_inferer.py
# 预期: thread.start() 调用, 主线立即返回

# 实测主线决策耗时 (开盘后)
journalctl -u ftseek-us | grep "IntradayScorer.*scored" | awk -F"latency=" '{print $2}' | sort -n
# 预期: p99 < 100ms (无 LLM 阻塞)
```

---

## [📊盘后]

### 归因数据流
```
04:00 ET scheduler_triggers.check_daily_review
    ↓
DailyReview.generate(date) → daily_review_YYYY-MM-DD.json (11 段)
    ↓
_trigger_eod_agent_review(scheduler, review)
    ↓
scheduler.agent_bridge.consume_eod(review)  [scheduler_triggers.py:243]
    ↓ (异步 → hermes_client subprocess)
LLMTAttributionEngine.score(trade)  [llm/attribution_engine.py:96]
    ↓ 五因子打分 (signal/execution/timing/risk/scene)
trade_attribution UPDATE overall_score
    ↓
push_hub.push(daily_review())  → DingTalk + Feishu
```

### 复盘模板结构 (`daily_review.py`)
| 11 段 JSON | 来源 |
|---|---|
| summary | total_trades/win_rate/pnl |
| by_strategy | owner_strategy 分组 |
| by_symbol | 标的 PnL |
| by_regime | regime 分组 (Y1) |
| exit_reason_pnl | F22 退出原因 × PnL |
| shadow_comparison | Z4 4 variant 对比 |
| pnl_outliers | Y5 5σ 异常 |
| symbol_loss_guard_triggers | P0-X 触发列表 |
| entry_thesis_breakdown | PB 入场理由 |
| factor_attribution | trade_attribution.factor_* 聚合 (R8 修复后真有数据) |
| improvement_suggestions | LLM 生成 |

### 推送路由
- `PushHub.push(daily_review(date, body))` → category=REVIEW_DAILY, level=NORMAL, dedup_key=`daily_review:{date}` 防当日重复
- DingTalk 主, Feishu 备, flag 兜底 (CRITICAL only)
- 同样: weekly/monthly review

### 自动调参建议接口 (`llm/daily_auto_tuner.py`)
```python
@dataclass
class TuningProposal:
    proposal_id: str
    param_key: str           # PARAM_MAP key, 例 MIN_CONFIDENCE
    current_value: Any
    proposed_value: Any
    rationale: str
    expected_improvement: str
    confidence: float
    trigger_mode: str        # auto / manual_review
```
**落库**: `decisions.db.tuning_proposals` → 等待 ConfigWriter 应用 (但 ConfigWriter 当前**不自动应用**, 需人工 review)
**热重载**: `app/config_mgmt/hot_reloader.py:_reload` 60s 冷却, 改 yaml 触发自动 reload ✓

---

## [🛡️可用性]

### 超时 / 重试配置

| 模块 | 超时 (s) | 重试次数 | 评估 |
|----|---|---|---|
| `premarket_planner._call_llm` | 30 (yaml) | **0** | ❌ 无 retry, 单次失败 raise |
| `thesis_inferer` (L3) | 15 (yaml) | 0 | OK (异步, 失败 fallback NO_ACTION) |
| `news_classifier` (L1) | 20 (yaml) | 0 | OK |
| `multi_model_client.chat` | default 60 | 0 | ❌ **60s 过长**, 建议 30s |
| `llm_helper.call_llm` | passed-through | 0 | ❌ 无 retry 层 |
| `critical_alerts.send_critical_sync` | — | **5x 重试 + backoff** | ✓ 关键告警重试合理 |

**严重缺口**: 整个 LLM 体系**无重试机制**. 唯一有重试的是 `critical_alerts` (推送层) 和 `order_retry` (下单层).

### 模型降级链 — 主 → 备 → 规则

| 链路 | 现状 |
|----|----|
| premarket: 主 qwen-max → enhanced_debate 多模型 | ❌ enhanced_debate 已死 (`debate_enabled=False`) → 实际单模型 |
| premarket fail → 昨日 plan 缓存 | ❌ 未实现 |
| thesis_inferer: deepseek → 规则 (momentum 默认) | ✓ 隐式降级 (失败时 trade_attribution.entry_thesis 留空 → fallback policy) |
| news_classifier: minimax → 规则 (lang 检测) | ⚠ 失败仅返回 None, 上游需自己处理 |
| 系统级: feature_flags.is_enabled() | ✓ 一键关 |

### 降级开关
```yaml
# 一键关全部 LLM (yaml)
llm_features:
  news_classifier: { enabled: false }   # 等
  thesis_inferer:  { enabled: false }
  emergency_review:{ enabled: false }
  # ... 12 个 features 都可单独关

# 全局总闸 (代码)
llm_budget:
  daily_max_usd: 5.0   # 超额自动停 (待验证: 实际有 halt 逻辑?)
```

**实测**: `feature_flags.is_enabled` 入口检查 ✓; `llm_budget.daily_max_usd` 当前**未在 multi_model_client 中强制 halt** (仅 per-feature `daily_max_usd` 有 budget check).

### pytest 验证命令
```bash
# LLM features 启停测试
pytest tests/coordinator/test_llm_features.py -v

# JSON Schema 容错
pytest tests/ -k "json_schema_guard"

# 盘前计划生成完整链路
pytest tests/ -k "premarket"
```

---

## [✅完整性] — 10 项硬性检查清单

| # | 检查项 | 阈值 | 当前状态 | 验证方式 |
|--|----|----|:---:|----|
| 1 | 盘前 LLM 调用超时 ≤ 30s | ≤30s | ✓ 30s | grep `timeout` premarket_planner.py |
| 2 | 盘中热路径 0 LLM 调用 | =0 | ✓ | `grep -rnE "infer_thesis\|call_llm" app/core/intraday_scorer.py app/strategy/pure/` |
| 3 | 异步 LLM 失败不阻塞主线 | 必须 | ✓ Thread daemon | `grep "daemon=True" app/llm/thesis_inferer.py` |
| 4 | JSON 输出有容错抽取 | 必须 | ✓ parse_json_strict | `cat app/llm/features/json_schema_guard.py` |
| 5 | JSON 输出有字段 schema 校验 | 必须 | ❌ 仅容错抽取, 无字段约束 | (待补) `jsonschema.validate(plan, plan_schema)` |
| 6 | LLM 调用全量审计 | 必须 | ✓ `audit_db.record_call` | `sqlite3 data/llm_audit.db ".schema llm_calls"` |
| 7 | per-feature 预算限额 | yaml 配 | ✓ `daily_max_usd` | grep `daily_max_usd` config/unified_config.yaml |
| 8 | 模型降级链 ≥ 2 层 | ≥2 | ❌ 仅 1 层 (单模型, enhanced_debate 已死) | (待补) 主→备模型切换 |
| 9 | 失败 fallback 至缓存 | 必须 | ❌ premarket fail = raise | (待补) 加载昨日 battle_plan.json 兜底 |
| 10 | Prompt 版本管理 | 必须 | ❌ 完全无 | (待补) US_SYSTEM_PROMPT 增加 `PROMPT_VERSION=2026.05.17` 常量 |

**通过率: 5/10 = 50%**. 阻塞实盘的 3 项: #5, #8, #9.

---

## 配置模板 (供后续补漏用, 不修改现有)

### 1. premarket plan_schema.json (建议新增)
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["date", "market_regime", "targets"],
  "properties": {
    "date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    "market_regime": {"enum": ["Risk-On", "Risk-Off", "Neutral"]},
    "targets": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["symbol", "direction", "entry_zone", "stop_loss", "take_profit"],
        "properties": {
          "symbol": {"type": "string", "pattern": "^US\\."},
          "direction": {"enum": ["LONG", "SHORT"]},
          "entry_zone": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
          "stop_loss": {"type": "number", "minimum": 0},
          "take_profit": {"type": "number", "minimum": 0},
          "status": {"enum": ["ACTIVE", "CANCELLED", "EXECUTED"]},
          "factors_used": {"type": "array", "items": {"type": "string"}}
        }
      }
    }
  }
}
```

### 2. yaml retry + fallback 配置 (建议补)
```yaml
llm_features:
  premarket_planner:
    enabled: true
    timeout_sec: 30
    retry:
      max_attempts: 2          # 1 次重试
      backoff_seconds: 5
    fallback_chain:
      - qwen-max               # 主
      - deepseek-chat          # 备
      - cache                  # 缓存昨日 plan (规则降级)
    prompt_version: "v2026.05.17"  # 加 prompt 版本管理
```

### 3. thesis_inferer 队列 + 背压 (建议补)
```yaml
llm_features:
  thesis_inferer:
    enabled: true
    timeout_sec: 15
    max_queue_size: 20       # 背压: >20 待处理则丢弃新请求
    max_workers: 4           # ThreadPool 限制
    drop_policy: latest      # latest / oldest
```

---

## 总结

| 维度 | 评级 | 阻塞实盘? |
|----|:---:|:---:|
| 📥 盘前 | 🟡 中 | 否 (但 #9 fallback 缺失实盘日某天 API 故障会无 plan) |
| ⚡ 盘中异步 | 🟢 良 | 否 (热路径 0 LLM 已验证) |
| 📊 盘后 | 🟢 良 | 否 |
| 🛡️ 可用性 | 🔴 差 | **是** (#5 schema / #8 降级链 / #9 缓存兜底) |

### 必修 (实盘前) 3 项
1. **#5 schema 校验**: 新增 `app/llm/schemas/plan_schema.json` + `validate_plan()`, FastLane 消费前校验, 非法拒入场
2. **#8 模型降级链**: `multi_model_client.chat(model, fallback_models=[...])` 主失败自动切备
3. **#9 缓存兜底**: premarket_planner LLM fail → 加载 `data/daily_battle_plan_us.json` (昨日) + 推送告警

### 推迟 (非阻塞)
- Prompt 版本管理 (#10)
- thesis_inferer 队列背压 (实测高频日再优化)
- enhanced_debate 复活 (短期无价值, 单模型够用)

### 一键自检命令
```bash
# 全维度
bash scripts/preflight_check.sh

# LLM 子系统
pytest tests/coordinator/test_llm_features.py -v
python scripts/why.py --date 2026-05-17  # L10 复盘对话

# 实测耗时
journalctl -u ftseek-us | grep "LLM·" | awk '{print $NF}' | grep "latency_ms" | sort -n | tail -20
```

---

**最终结论**: 当前 LLM 工作流**通过率 5/10**. 盘中热路径隔离 ✓. 盘前 / 可用性层面有 3 项阻塞实盘的缺口 (schema/降级链/缓存兜底). 建议先补这 3 项再上实盘. 文档另含配置模板供补漏参考.
