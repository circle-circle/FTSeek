# LLM 统一调用入口 (model_call.py) 综合方案 (2026-05-17)

> **背景**: 用户要求换 4 个新模型 + 统一接口切换 + 提升收益 + 规避幻觉 + 多模型验证
> **方案**: 综合方案 B (极简) + A (cross-validate) 优点, 不过度抽象
> **交付**: 1 个新文件 + 1 段 yaml + 20 个测试. 不破坏现有 multi_model_client
> **回归**: 213/213 全绿

---

## 1. 设计原则 (第一性原理)

| 需求 | 实现 | 不做 |
|---|---|---|
| 换 4 模型 | yaml `llm.models.<alias>.model_name` 单点 | 不抽象 tier enum |
| 统一接口 | `call_llm(feature, prompt)` 单函数 | 不引入 Router 类 |
| 提升收益 | feature → 灵活组合 primary/fallback | 不强加"用 plus 一定提升"先验 |
| 规避幻觉 | JSON Schema 强校验 + Citation prompt 习惯 | 不引入 jsonschema 包 (复用 parse_and_validate) |
| 多模型验证 | 串行 critique (primary 出方案 + fallback 评审) 默认 off | 不并行 cross_validate (LLM 同失败模式) |
| 缓存兜底 | `cache_on_fail: true` 按 feature 启用 | 不全局强制缓存 |
| 降级链 | 2 层 (primary → fallback → cache) | 不搞 3 层 |

---

## 2. 架构

```
                     yaml llm.features.<feature>
                                ↓
                      call_llm(feature, prompt, ...)
                                ↓
                  [cross_check?] ─┬─ true → _cross_check_call (critique 串行)
                                  └─ false ↓
                          ┌──────────────────────────┐
                          │  primary alias (qwen-plus)  │
                          │  _try_single_model + retry  │
                          └─────────────┬────────────┘
                                  ✓ │      ✗
                                    ↓      ↓
                              schema 校验   ↓
                                  ✓ │      ↓
                                    ↓      ↓
                          ┌──────────────────────────┐
                          │  fallback (deepseek-pro)  │ ← schema 失败也走这里
                          └─────────────┬────────────┘
                                  ✓ │      ✗
                                    ↓      ↓
                              schema 校验   ↓
                                  ✓ │      ↓
                                    ↓      ↓
                                 return    cache_on_fail?
                                              ↓ yes
                                          _load_cache (24h TTL)
                                              ↓
                                          return / fail
```

---

## 3. 文件清单

```
新增:
  app/llm/model_call.py            295 行  (统一入口 + CallResult + 缓存 + cross_check)
  tests/coordinator/test_model_call.py 250 行  (20 测试用例)

修改:
  config/unified_config.yaml       +58 行   (新 llm 段: models + features + defaults)

不动 (向后兼容):
  app/llm/multi_model_client.py   (旧 MODEL_REGISTRY 仍工作, 走 bailian 网关)
  app/llm/features/json_schema_guard.py  (复用现有 parse_and_validate)
  app/llm/features/audit_db.py    (复用现有 record_call)
  app/llm/features/llm_helper.py  (短期内仍可用)
```

---

## 4. yaml 配置 (单一切换点)

```yaml
llm:
  # 4 个模型 (升级仅改 model_name)
  models:
    qwen-flash:
      provider: dashscope
      api_key_env: DASHSCOPE_API_KEY
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      model_name: qwen3.6-flash     # ← 升级仅改这里
      timeout: 8
      cost_in: 0.0003
      cost_out: 0.0006
    qwen-plus:    {... model_name: qwen3.6-plus, timeout: 30}
    deepseek-flash: {... model_name: deepseek-v4-flash, timeout: 8}
    deepseek-pro:   {... model_name: deepseek-v4-pro, timeout: 30}

  # feature → model 映射
  features:
    # 延迟敏感
    news_classifier:   qwen-flash             # 简单格式: 单模型
    thesis_inferer:    deepseek-flash
    sentiment:         qwen-flash

    # 质量敏感
    premarket_planner: {primary: qwen-plus, fallback: deepseek-pro, cache_on_fail: true}
    daily_review:      {primary: deepseek-pro, fallback: qwen-plus, cache_on_fail: true}
    weekly_proposer:   {primary: deepseek-pro, fallback: qwen-plus}
    daily_auto_tuner:  {primary: deepseek-pro, fallback: qwen-plus}
    regime_classifier: {primary: qwen-plus, fallback: deepseek-pro}

    # 关键决策 (调用方传 cross_check=true 启用 critique)
    earnings_analyst:      {primary: qwen-plus, fallback: deepseek-pro}
    large_position_review: {primary: qwen-plus, fallback: deepseek-pro}
    emergency_review:      {primary: deepseek-pro, fallback: qwen-plus}

  defaults:
    cache_dir: data/llm_cache
    divergence_threshold: 0.5     # cross_check severity ≥ 0.5 拒方案
```

---

## 5. API 用法

### 5.1 基础调用
```python
from app.llm.model_call import call_llm

res = call_llm("thesis_inferer", "What's the thesis?")
if res:                                   # 等价 res.success
    thesis = res.response                  # 原始文本
    print(f"用 {res.model_used}, 延迟 {res.latency_ms}ms, 成本 ${res.cost_usd}")
```

### 5.2 带 JSON Schema 校验 (反幻觉)
```python
res = call_llm(
    "premarket_planner",
    prompt,
    system=US_SYSTEM_PROMPT,
    schema_required={"date": str, "market_regime": str, "targets": list},
    schema_enums={"market_regime": ["Risk-On", "Risk-Off", "Neutral"]},
)
if res:
    plan = res.response                    # 已 parse + validated 的 dict
    if res.schema_passed:
        save_plan(plan)
else:
    # primary + fallback + cache 全失败
    logger.error(f"plan 生成失败: {res.error}")
    push_critical_alert(...)
```

### 5.3 关键决策 (cross_check critique)
```python
res = call_llm(
    "emergency_review",
    prompt,
    cross_check=True,                    # 启用 critique
)
if res.success:
    if res.cross_check.get("agreed"):
        execute(res.response)
    elif res.cross_check.get("degraded"):
        # critic 失败, 仅 primary 结果, 降级处理
        review_manually(res.response)
else:
    if res.error == "cross_check_rejected":
        severity = res.cross_check["severity"]
        reasons = res.cross_check["critic_reason"]
        logger.warning(f"🚨 critic 否决 (severity={severity}): {reasons}")
        # 不执行, 推告警
```

---

## 6. 反幻觉 4 件套

| # | 策略 | 实施位置 | 默认 |
|--|---|---|---|
| 1 | **JSON Schema 强校验** | `schema_required/optional/enums` 参数 | 按需 (None = 不校验) |
| 2 | **Citation 强制** | system prompt 末尾加 "必须引用具体数据 RSI=X, MA20=Y" | 推荐写 |
| 3 | **串行 critique** | `cross_check=True` 单 feature | 默认 off (开销 2x) |
| 4 | **Cache 兜底 + 告警** | yaml `cache_on_fail: true` + critical_alerts | 关键 feature 启用 |

未做 (有意识):
- ❌ Self-consistency 多次采样 (3x 成本, marginal 收益不明)
- ❌ Prompt 版本管理目录 (用 `prompt_hash()` 写 audit_db 即可追溯)
- ❌ 并行 cross-validate (LLM 不独立, 同失败概率高)

---

## 7. 迁移路径 (渐进, 不破坏)

### 阶段 1 (已完成 ✅): 新建并行
- `app/llm/model_call.py` ✅
- yaml `llm` 段 ✅
- 测试 ✅
- 旧 `multi_model_client.chat()` 不动 ✅

### 阶段 2 (推荐): 关键 feature 迁移示范
迁移顺序 (按风险从低到高):
```
1. thesis_inferer       (FLASH, 失败仅复盘缺标签, 风险最低)
2. news_classifier      (FLASH, 同上)
3. premarket_planner    (PRO, 加 cache_on_fail 兜底)
4. daily_review         (PRO, 盘后异步, 不影响实盘)
5. weekly_proposer      (PRO)
6. earnings_analyst     (PRO + cross_check 启用)
7. large_position_review (PRO + cross_check)
8. emergency_review     (PRO + cross_check, 最高风险, 最后切)
```

### 阶段 3 (验证后): 旧 MODEL_REGISTRY 清理
- 跑 2 周 audit_db, 确认所有 feature 都走 call_llm
- 删除旧 `MODEL_REGISTRY` + `DEFAULT_MODEL_FOR`
- 删除 `multi_model_client.chat` (或保留作低层 HTTP utility)

---

## 8. 验证命令

### 8.1 模型切换 (零代码改)
```bash
# 升级到 qwen3.7
yq -i '.llm.models.qwen-flash.model_name = "qwen3.7-flash"' config/unified_config.yaml
systemctl restart ftseek-us

# 验证生效
journalctl -u ftseek-us | grep "qwen3.7-flash"
```

### 8.2 fallback 验证 (mock primary fail)
```bash
pytest tests/coordinator/test_model_call.py::test_fallback_used_when_primary_fails -v
# 预期: ✓ pass, 调用日志显示 primary 失败后切到 fallback
```

### 8.3 schema 校验 (拒非法 JSON)
```bash
pytest tests/coordinator/test_model_call.py::test_schema_fail_triggers_fallback -v
# 预期: ✓ 主模型返回"I don't know" 被拒, 走 fallback
```

### 8.4 cross_check 拒决策
```bash
pytest tests/coordinator/test_model_call.py::test_cross_check_disagree_severe_rejects -v
# 预期: ✓ critic 不同意 severity=0.8, success=False, error=cross_check_rejected
```

### 8.5 缓存兜底
```bash
pytest tests/coordinator/test_model_call.py::test_cache_fallback_when_all_fail -v
# 预期: ✓ 全部 LLM 失败时, 返回 24h 内 cache, from_cache=True
```

### 8.6 实际成本对账
```bash
sqlite3 data/llm_audit.db "
SELECT feature, model, COUNT(*) n, ROUND(SUM(cost), 4) total, ROUND(AVG(latency_ms), 0) avg_lat
FROM llm_calls WHERE date(created_at) = date('now')
GROUP BY feature, model
ORDER BY total DESC;
"
```

---

## 9. CallResult 元数据 (完整字段)

```python
@dataclass
class CallResult:
    success: bool
    response: Optional[Union[str, Dict]] = None  # str (无 schema) / dict (有 schema)
    model_used: str = ""                          # 实际用的模型 alias
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    from_cache: bool = False
    fallback_used: bool = False                   # 是否走了 fallback
    schema_passed: Optional[bool] = None          # None=未校验 / True/False
    cross_check: Optional[Dict] = None            # critique 结果 (含 severity)
    error: Optional[str] = None

    def __bool__(self) -> bool:
        return self.success
```

---

## 10. 与现有系统的关系

| 模块 | 关系 | 说明 |
|---|---|---|
| `multi_model_client.py` | **共存** (短期) | 旧代码不动, 跑 1 个月数据后清理 |
| `MODEL_REGISTRY` | 退役中 | 不再 hardcode 模型名, yaml `llm.models` 单一来源 |
| `llm_helper.py:call_llm` | 旧 API 兼容 | 旧 import 仍工作, 内部慢慢迁 |
| `json_schema_guard.py` | **复用** | model_call 调 parse_and_validate, 不重写 |
| `audit_db.py:record_call` | **复用** | model_call 自动写审计 |
| `feature_flags.py:is_enabled` | **协作** | 调用方先 is_enabled 再 call_llm |
| `multi_model_client._cost_tracker` | 旧路径用 | 新路径 cost 从 yaml.models.cost_in/out 算 |

**关键: 不破坏现有任何调用方**. 任何文件都可以渐进迁移到新 API.

---

## 11. 后续推荐 (非阻塞实盘)

### 短期 (1 周内):
1. 把 `thesis_inferer.py:_infer_sync` 迁到 `call_llm("thesis_inferer", ...)`, 启用 schema
2. 把 `premarket_planner._call_llm` 迁到 `call_llm("premarket_planner", ...)`, 启用 schema + cache_on_fail
3. 监控 `data/llm_audit.db` 看新模型表现 vs 旧

### 中期 (1 月内):
4. 启用 `earnings_analyst.cross_check=True`, 看 critic 拒决策率
5. yaml `cross_check_features: [emergency_review, large_position_review]` 默认开关
6. ConfigWriter 自动应用 `daily_auto_tuner` 提案 (当前需人工)

### 长期:
7. Prompt 版本管理: audit_db 新增 prompt_hash 列, 复盘 SQL "今天的 plan 用的是哪版 prompt"
8. A/B 测试框架: 5% 流量用 qwen-plus, 95% 用 qwen-flash, 后台对比 PnL
9. 删除 `multi_model_client.py` (1 月数据足够后)

---

## 12. 整体修复时间线 (本周累计)

| 阶段 | 主题 | 代码量 |
|---|---|---|
| 5/15 | R1-R4 PreTrade SoT | |
| 5/16 | O1-O7 / T1-T9b / F1-F3 | |
| 5/16 | PushHub + R1+R2 + M1+M1.1 + PostTradeHook | |
| 5/16-17 | 终验 → 删 app/strategies 重复造轮子, SignalCandidate 增强 | -1000 行 |
| 5/17 | LLM 工作流审计 | (报告) |
| **5/17** | **model_call.py 统一调用入口** | **+545 行 (含测试)** |

---

## 13. 总结

| 维度 | 状态 |
|---|---|
| 4 个新模型支持 | ✅ yaml 配置 |
| 统一切换接口 | ✅ `call_llm(feature, prompt)` |
| 提升收益 | ✅ 灵活 primary/fallback, 不强加先验 |
| 规避幻觉 | ✅ JSON Schema + Citation + critique |
| 多模型验证 | ✅ cross_check (串行 critique, 默认 off) |
| 不破坏现有 | ✅ multi_model_client 不动 |
| 0 新依赖 | ✅ 复用 json_schema_guard + audit_db |
| 测试覆盖 | ✅ 20 用例, 213/213 全绿 |
| 渐进迁移路径 | ✅ 3 阶段 |
| 验证命令 | ✅ 6 条 |

**用户 5 大需求**: 全部满足.
**未来切模型**: 只动 yaml 1 行 `llm.models.qwen-flash.model_name = qwen3.7-flash`.
**实施工作量**: ~6h. **净增代码**: +545 行 (含 250 行测试). **0 新依赖, 0 破坏**.

---

**结论**: 综合方案落地. 旧 `multi_model_client.chat` 1 个月观察期后可清理. 关键场景启用 `cross_check` 待用户决策.
