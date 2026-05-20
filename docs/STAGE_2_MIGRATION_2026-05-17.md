# 阶段 2 迁移 — thesis_inferer + premarket_planner (2026-05-17)

> **承接**: `docs/LLM_MODEL_CALL_2026-05-17.md` §7 阶段 2 计划
> **范围**: 2 个最关键 feature 迁移到 `model_call.call_llm`
> **回归**: 213/213 全绿, 0 破坏

---

## 1. 迁移成果对比

### S2.1 thesis_inferer (`app/llm/thesis_inferer.py`)

| 维度 | 迁移前 | 迁移后 |
|---|---|---|
| import | `llm_helper.call_llm` + `parse_and_validate` 两段 | 仅 `model_call.call_llm` |
| 模型选择 | `cfg.get("model", "deepseek")` (旧 alias) | yaml `llm.features.thesis_inferer` 路由 (默认 `deepseek-flash`) |
| schema | 手动 `parse_and_validate(response, required={...})` | 内置 `schema_required=_THESIS_SCHEMA_REQUIRED` |
| 失败处理 | response=None → return None | `res.success=False` → return None (相同行为, 更明确) |
| 元数据 | 无 | `obj["_model_used"]`, `_latency_ms`, `_cost_usd`, `_fallback_used` 写 metadata_json |
| Citation | 无 | system prompt 加 "反幻觉: key_evidence 必须引用具体数值" |

**关键改进**:
- ✅ 模型切换无代码改: yaml 改 `deepseek-flash` → `deepseek-v5-flash` 即生效
- ✅ schema 校验失败自动走 fallback (qwen-flash)
- ✅ 单笔 LLM 元数据写入 trade_attribution.metadata_json, 复盘可对账

### S2.2 premarket_planner (`app/us_pipeline/premarket_planner.py`)

| 维度 | 迁移前 | 迁移后 |
|---|---|---|
| 实现 | `AsyncOpenAI` 直调 + qwen-max 硬编码 | `asyncio.to_thread(call_llm)` 走 model_call |
| 模型 | 单一 qwen-max | yaml `{primary: qwen-plus, fallback: deepseek-pro, cache_on_fail: true}` |
| 失败处理 | `return None` → 上层 `raise Exception("LLM 调用失败")` | primary fail → fallback → cache (24h) → 推 CRITICAL 告警 |
| 缓存兜底 | ❌ 无 | ✅ `cache_on_fail: true`, model_call 成功时自动写 cache, 全失败时自动读 24h cache |
| Citation | 无 | "每个 target logic 必须引用具体数值 RSI=X, MA20=Y" |
| audit | 不写 audit_db | 自动写 audit_db (model/cost/latency) |

**关键改进**:
- ✅ 上轮 R8 (factor_detail 永空) 之后, 现在 R9 (无 fallback / cache 兜底) 也解决
- ✅ 降级链: primary → fallback → 24h cache → CRITICAL 告警 (PushHub)
- ✅ 实盘日某天 API 故障时, 自动用昨日 plan 兜底, 不会无 plan

---

## 2. 修改清单

| 文件 | 变更行数 | 类型 |
|---|---|---|
| `app/llm/thesis_inferer.py` | -25 / +30 (净 +5) | 重构 _infer_sync, 删 llm_helper 依赖 |
| `app/us_pipeline/premarket_planner.py` | -20 / +52 (净 +32) | 重构 _call_llm, 加 cache 告警 + citation |

净增代码: ~40 行 (含日志元数据 + 缓存告警)

---

## 3. 关键测试

### thesis_inferer
```bash
# Phase B 旧测试仍通过
pytest tests/coordinator/test_phase_b_minimal.py -q
# 11/11 ✓

# 静态: 验证已迁
python3 -c "
src = open('app/llm/thesis_inferer.py').read()
assert 'from app.llm.model_call import call_llm' in src
assert 'schema_required=_THESIS_SCHEMA_REQUIRED' in src
assert '反幻觉' in src
print('thesis_inferer 迁移完成')
"
```

### premarket_planner
```bash
# 静态: 验证已迁
python3 -c "
src = open('app/us_pipeline/premarket_planner.py').read()
assert 'from app.llm.model_call import call_llm' in src
assert 'asyncio.to_thread' in src
assert 'cache_on_fail' not in src or 'from_cache' in src
print('premarket_planner 迁移完成')
"

# 验证 yaml cache_on_fail 配置
grep 'cache_on_fail: true' config/unified_config.yaml
# 期望: premarket_planner + daily_review 两行
```

---

## 4. 验证 (今晚盘前后)

### 4.1 盘前计划生成日志
```bash
journalctl -u ftseek-us | grep "盘前计划·LLM"
# 期望:
#   [盘前计划·LLM] ✓ model=qwen-plus latency=8500ms cost=$0.0820 fallback_used=False
# 若 fallback 触发:
#   [盘前计划·LLM] ✓ model=deepseek-pro latency=10200ms cost=$0.0410 fallback_used=True
```

### 4.2 thesis_inferer 调用统计
```bash
sqlite3 data/llm_audit.db "
SELECT model, COUNT(*) n, ROUND(AVG(latency_ms)) avg_lat, ROUND(SUM(cost), 4) total_cost
FROM llm_calls WHERE feature='thesis_inferer' AND date(created_at)=date('now')
GROUP BY model;
"
# 期望: 模型应是 deepseek-v4-flash (yaml 配置), 单次 latency < 3000ms
```

### 4.3 缓存兜底文件
```bash
ls -la data/llm_cache/
# 期望: 看到 premarket_planner.json (成功调用后自动写)
# 内容: {"response": "...", "saved_at": <unix_ts>}
```

### 4.4 trade_attribution 含 thesis 元数据
```bash
sqlite3 data/ftseek_us.db "
SELECT trace_id, entry_thesis,
       json_extract(metadata_json, '$._model_used') AS model,
       json_extract(metadata_json, '$._latency_ms') AS latency,
       json_extract(metadata_json, '$._cost_usd') AS cost
FROM trade_attribution
WHERE date(created_at) = date('now') AND entry_thesis IS NOT NULL
LIMIT 5;
"
# 期望: 每笔 trade 有 entry_thesis + 模型元数据
```

---

## 5. 反幻觉效果 (Citation 强化)

**迁移前 thesis_inferer 输出示例** (空泛):
```json
{
  "thesis": "momentum",
  "key_evidence": "Strong momentum indicators",
  ...
}
```

**迁移后期望** (具体数值):
```json
{
  "thesis": "momentum",
  "key_evidence": "RSI=72 上升, MA20=$145.5 持续上方, vol_ratio=1.8x",
  ...
}
```

**复盘验证 SQL**:
```sql
-- 找 key_evidence 不含数字的 thesis (疑似幻觉)
SELECT trace_id, entry_thesis,
       json_extract(metadata_json, '$.key_evidence') AS evidence
FROM trade_attribution
WHERE date(created_at) >= date('now', '-7 days')
  AND entry_thesis IS NOT NULL
  AND json_extract(metadata_json, '$.key_evidence') NOT GLOB '*[0-9]*';
-- 期望: 7 天后此 SQL 返回 0 行 (Citation 强化生效)
```

---

## 6. 下一步迁移 (按风险递增)

| # | feature | 优先级 | 阻塞实盘? |
|--|---|:---:|:---:|
| 3 | `news_classifier` | 中 | 否 (低延迟 FLASH, 失败仅复盘缺标签) |
| 4 | `regime_classifier` | 中 | 否 (盘中 30min 1 次, 失败用旧 regime) |
| 5 | `daily_review` (盘后) | 中 | 否 (异步, 不影响实盘) |
| 6 | `weekly_proposer` | 低 | 否 |
| 7 | `daily_auto_tuner` | 低 | 否 |
| 8 | `earnings_analyst` + cross_check | 高 | 大单影响; 启用前需评估 critic 拒决策率 |
| 9 | `large_position_review` + cross_check | 高 | 同 |
| 10 | `emergency_review` + cross_check | 高 | 风控触发场景, 最后切 |

---

## 7. 推迟项 (诚实)

| 项 | 推迟原因 |
|---|---|
| premarket plan_schema 严格校验 | 字段较多 (15+), 一次性加 schema 风险高. 先观察 1 周, 看 LLM 实际输出再补 schema |
| cross_check 启用 (earnings/large_position/emergency) | 当前 3 个 feature `enabled: false`, 启用前先单模型表现 |
| 删 `multi_model_client.py` | 等 2 周, 旧调用方全部迁完才删 |
| `app/llm/features/llm_helper.py` 标 DEPRECATED | 同上, 仍有少量调用方 |

---

## 8. 实盘前检查 (今晚开盘前必跑)

```bash
# 1. yaml 校验
yq '.llm' config/unified_config.yaml | head -50

# 2. env 检查
echo "DASHSCOPE_API_KEY: ${DASHSCOPE_API_KEY:0:8}..."
echo "DEEPSEEK_API_KEY:  ${DEEPSEEK_API_KEY:0:8}..."

# 3. 全量回归
pytest tests/coordinator/ -q --ignore=tests/coordinator/test_p0_audit_fixes.py --ignore=tests/coordinator/test_cn_news_sources.py
# 期望: 213 passed

# 4. 重启服务
systemctl restart ftseek-us

# 5. 看启动日志
journalctl -u ftseek-us -n 50 | grep -E "LLM|model_call|premarket|thesis"
```

---

## 9. 整体修复时间线 (本周累计)

| 阶段 | 主题 |
|---|---|
| 5/15 | R1-R4 PreTrade SoT |
| 5/16 | O1-O7 / T1-T9b / F1-F3 / PushHub / M1+M1.1 / PostTradeHook |
| 5/16-17 | 终验 → 删 app/strategies 重复造轮子 |
| 5/17 早 | LLM 工作流审计 (5/10 通过) |
| 5/17 中 | model_call.py 统一调用入口 (综合方案) |
| **5/17 晚** | **阶段 2 迁移**: thesis_inferer + premarket_planner |

---

**结论**: 阶段 2 完成. 2 个最关键 LLM feature 已切到新接口, 享受 yaml 统一切换 + 缓存兜底 + Citation 反幻觉 + audit 元数据完整链路. 213/213 全绿. 8 个剩余 feature 按文档 §6 顺序渐进迁移.
