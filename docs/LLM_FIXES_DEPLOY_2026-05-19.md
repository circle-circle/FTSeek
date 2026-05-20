# LLM 链路修复部署 + 今晚开盘验证 (2026-05-19)

> **目标**: 让 LLM 真正进入交易链路 — 从 7 天 0% 成功率 / 0 主线触发 → 今晚开始有真实数据.

---

## 1. 修复逻辑链 (3 个 P0 改动)

```
        L2-B audit 保留 raw  ──────┐
        (诊断基础, 必须先做)         │
                                    ├→ 看到 LLM 实际输出, 数据驱动改 schema
                                    │
        L1 news_classifier 迁移 ───┤
        (yaml 路由生效)            │
                                    ↓
        L3 scalping 触发 thesis ──→ LLM 数据从 0 → 110 笔/天
                                    (释放 7 天累积的诊断窗口)
```

---

## 2. 修改清单

| # | 文件 | 变更 | 行数 |
|---|---|---|---|
| **L2-B** | `app/llm/model_call.py:331-372` | API 失败 + schema 失败时调 `_record_audit`, 保留 raw response | +14 |
| **L1** | `app/news_v2/processor/llm_classifier.py:31` | 改 `from app.llm.model_call import call_llm as _model_call_llm` (替代 llm_helper) | -3 +5 |
| **L1** | `app/news_v2/processor/llm_classifier.py:144-180` | 调用 `_model_call_llm(..., schema_required={..}, schema_enums={..})`, 删除二步 parse_and_validate | -8 +9 |
| **L3** | `app/scalping/engine.py:2300-2316` | _execute_trade 入场后追加 `infer_thesis_async(...)` (异步, 不阻塞) | +16 |

净改 ~33 行, 风险面窄.

---

## 3. 今晚部署 + 验证

### 3.1 部署 (开盘前 30min)
```bash
cd /opt/ftseek/FTSeek-US
git pull origin main
sudo systemctl restart ftseek-us
sleep 10
journalctl -u ftseek-us --since "1 minute ago" | head -30
```

期望日志:
```
✅ FTSeek-US 服务已启动
[ScalpingEngine] 信号冷却时间: 300s
[CircuitBreaker] 初始化 | 最大连续亏损: 3
✅ [PostTradeHook·wired] order_executor 已绑定
[O1·SANDBOX] polling_controller 已严格过滤: 注册 12 只
```

### 3.2 实时验证 (开盘后 30min)

```bash
# 1. L2-B: schema 失败 raw 是否被保留 (期望 audit DB 有 response_len > 0 的失败行)
sqlite3 data/llm_audit.db "
SELECT feature, model, COUNT(*) n, 
  SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) ok,
  ROUND(AVG(response_len), 0) avg_resp_len,
  ROUND(AVG(latency_ms), 0) avg_ms
FROM llm_calls
WHERE date(timestamp,'unixepoch','localtime') = date('now','localtime')
GROUP BY feature, model;"
# 期望: thesis_inferer / news_classifier 出现新行
# 期望: 失败行的 avg_resp_len > 0 (修前 0)

# 2. L1: news_classifier 走新路由 (model 列应是 qwen-flash 或 fallback, 不是 MiniMax)
sqlite3 data/llm_audit.db "
SELECT model, COUNT(*) FROM llm_calls
WHERE feature='news_classifier' AND timestamp > strftime('%s','now','-1 hour')
GROUP BY model;"
# 期望: model = qwen-flash (修前 = MiniMax-M2.5)

# 3. L3: scalping 触发 thesis_inferer (期望每笔 scalping entry 后有 thesis_inferer 调用)
journalctl -u ftseek-us --since "30 minutes ago" | grep -c "L3·thesis·scalping\|ThesisInferer"
# 期望: ≥ 0 (有 scalping 入场就该有 thesis 触发)

journalctl -u ftseek-us --since "30 minutes ago" | grep "ModelCall.*schema_failed"
# 期望: 看到具体 raw_head (诊断 schema 实际失败原因)
```

### 3.3 收盘后诊断 (5/20 04:30 ET = 5/20 16:30 北京)

**核心: 看 LLM 实际成功率 + raw 内容找 schema 失败模式**

```bash
# A. 全部 features 一日成功率
sqlite3 -header -column data/llm_audit.db "
SELECT feature, model, COUNT(*) n,
  SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) ok,
  ROUND(100.0*SUM(success)/COUNT(*), 1) pct,
  ROUND(SUM(cost_usd), 4) cost
FROM llm_calls
WHERE date(timestamp,'unixepoch','localtime') = date('now','localtime')
GROUP BY feature, model
ORDER BY n DESC;"

# B. schema 失败的 raw 样本 (前 200 字诊断)
sqlite3 data/llm_audit.db "
SELECT feature, error_msg, result_summary
FROM llm_calls
WHERE success = 0 AND error_msg LIKE 'schema_failed%'
  AND date(timestamp,'unixepoch','localtime') = date('now','localtime')
LIMIT 5;"

# 数据驱动决策:
# - 如果 raw 含 markdown ```json 包裹 → 已被 parse_json_strict 处理 (line 35)
# - 如果 raw 字段名大小写偏差 → 改 schema 大小写不敏感 (Phase 2)
# - 如果 enum 值不匹配 → 加 alias map (e.g., "trend_following"→"momentum")
# - 如果 prompt 输出确实差 → 改 prompt 加 few-shot 例子
```

---

## 4. 预期结果对比

| 指标 | 5/12-5/18 历史 | 今晚 (5/19) 期望 |
|---|---|---|
| LLM 总调用数 (一天) | 1-3 (基本是 test) | **20-150** (scalping 入场触发 thesis) |
| thesis_inferer 触发数 | 0 | **≥ 5** (假设有 scalping 入场) |
| news_classifier 触发数 | 0-3 (走老 minimax) | **0-50** (走 qwen-flash) |
| schema 失败 audit 记录 | 不写 (raw 丢失) | **写, 且含 raw** |
| LLM 实际成本 (一天) | $0.00 | $0.05-0.30 |
| **LLM 决策影响交易** | **0 笔** | thesis_inferer 输出供 ExitPolicy 使用 (PB-1) |

**乐观场景**: schema 容错够好, 部分 feature 立即可用 (如 news_classifier qwen-flash 简单 sentiment)
**保守场景**: 仍 100% schema 失败, 但 raw 保留 → 明日数据驱动改 schema (一周内修)

---

## 5. 风险与回滚

### 5.1 已知风险

| 风险 | 缓解 |
|---|---|
| **L3 scalping 100+ 入场触发 thesis_inferer 拖慢主流程** | infer_thesis_async 是 daemon Thread, 主线 0 阻塞; failure log debug 级别不刷屏 |
| **L1 news_classifier 走 qwen-flash 路由失败时无 fallback** | yaml route 仅 primary 无 fallback; 失败返回 None, 不影响新闻消费下游 (L9 news_signal_gen disabled) |
| **L2-B audit DB 写入暴增 (失败也写)** | 失败本来就是少数; audit DB 7d 累计 ~100 行, 加失败也 ≤ 500 行/天, 可控 |
| **schema 仍 100% 失败** | 不阻塞交易 (thesis_inferer 失败 → ExitPolicy 走默认 trail); 仅暂时无 LLM 智能 |

### 5.2 应急回滚

```bash
# 回滚 3 个 commit
git log --oneline | head -5
git revert <L1_commit> <L2B_commit> <L3_commit> --no-edit
sudo systemctl restart ftseek-us
```

### 5.3 应急切断条件

开盘 30 min 内任一发生 → 立即停 + 排查:
- main 线 entry latency > 1s (post_trade_hook 阻塞)
- ERROR 日志暴增 (LLM 异常未被吞)
- audit DB 写入失败 (10x slowdown)

---

## 6. 监控仪表盘 (建议)

```bash
#!/bin/bash
# scripts/llm_dashboard.sh — 每 30min 跑一次, 钉钉推送
echo "=== LLM Dashboard $(date) ==="

echo "## 今日各 feature 调用统计"
sqlite3 -header -column data/llm_audit.db "
SELECT feature, COUNT(*) n, SUM(success) ok,
  ROUND(100.0*SUM(success)/COUNT(*), 1) pct,
  ROUND(AVG(latency_ms),0) ms,
  ROUND(SUM(cost_usd),4) cost
FROM llm_calls
WHERE date(timestamp,'unixepoch','localtime') = date('now','localtime')
GROUP BY feature ORDER BY n DESC;"

echo ""
echo "## schema 失败前 3 个 raw 样本"
sqlite3 data/llm_audit.db "
SELECT feature, substr(result_summary, 1, 100)
FROM llm_calls
WHERE success=0 AND error_msg LIKE 'schema_failed%'
  AND date(timestamp,'unixepoch','localtime') = date('now','localtime')
LIMIT 3;"

echo ""
echo "## L3 scalping → thesis 触发计数 (今日)"
journalctl -u ftseek-us --since today | grep -c "ThesisInferer-US\."
```

---

## 7. 后续路线 (本周 + 2 周内)

### 立刻 (今晚部署后看数据决定)
- 看 audit DB raw 内容
- 如果 schema 失败模式集中 (e.g., 字段名大小写) → 改 json_schema_guard 容错
- 如果 prompt 输出差 → 改 thesis_inferer prompt 加 few-shot

### 本周 (基于今晚数据反馈)
- L4 premarket plan ↔ 主线 scan 联动 (上次审计建议)
- L5 启用 regime_classifier (替 router 简单规则)

### 2 周
- L6 disagreement_arbitrator (主线模糊区间仲裁)
- L7 daily_auto_tuner (自动调参)

---

## 8. 一句话总结

> **3 个最小改动 (~33 行) 启动 LLM 链路**: L2-B 让失败可观测 (raw 保留), L1 让 news_classifier 走新 yaml 路由, L3 让 scalping 110 笔/天的入场触发 thesis_inferer.
> **关键策略**: 不试图一次性 fix schema 100% 通过 — 而是先获取**真实失败数据**, 明日开始数据驱动改 schema (Karpathy: gradients over rules).
> **风险窄**: thesis_inferer 是 daemon 异步, 主流程 0 阻塞; 全部失败也只是回到当前状态 (无 LLM 决策).
