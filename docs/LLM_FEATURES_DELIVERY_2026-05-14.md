# LLM 增强 L1-L12 交付报告

> **日期**: 2026-05-14
> **状态**: 12 项全部交付, **默认全部 disabled**, yaml 单点开启
> **测试**: 32 单测全绿, coordinator 总计 75/75
> **可控性**: 6 大原则全部落实

## 交付清单

### L0 共享基础设施 (4 模块)
| 模块 | 路径 | 用途 |
|-----|------|------|
| feature_flags | `app/llm/features/feature_flags.py` | 单点 yaml 开关 + per-feature 预算读取 |
| json_schema_guard | `app/llm/features/json_schema_guard.py` | LLM 输出 JSON 强制校验 + clamp_numeric |
| audit_db | `app/llm/features/audit_db.py` | 每次 LLM 调用落 `data/llm_audit.db` |
| llm_helper | `app/llm/features/llm_helper.py` | 统一调用入口 (auto audit + budget check) |

### L1-L12 业务模块
| ID | 路径 | 行数 | 默认 |
|----|------|-----|------|
| **L1** 新闻 LLM 解读 | `app/news_v2/processor/llm_classifier.py` | ~210 | off |
| **L9** 新闻信号生成 | `app/news_v2/processor/event_signal_generator.py` | ~160 | off |
| - 新闻归因回看 | `app/news_v2/processor/news_attribution.py` | ~190 | (auto) |
| **L2** LLM 风控 gating | `app/risk/llm_emergency_review.py` | ~210 | off |
| **L3** entry_thesis 推断 | `app/llm/thesis_inferer.py` | ~150 | off |
| **L4** Earnings analyst | `app/llm/earnings_analyst.py` | ~200 | off |
| **L5** 分歧仲裁 | `app/llm/disagreement_arbitrator.py` | ~170 | off |
| **L6** 场景分类器 | `app/llm/regime_classifier.py` | ~210 | off |
| **L7** 大仓位 LLM 确认 | `app/llm/large_position_review.py` | ~165 | off |
| **L8** 日级 AutoTuner | `app/llm/daily_auto_tuner.py` | ~210 | off |
| **L10** 复盘对话 CLI | `scripts/why.py` | ~165 | **on** (CLI 无副作用) |
| **L11** 行业关联推理 | `app/llm/sector_correlation_reasoner.py` | ~180 | off |
| **L12** 多语言新闻 | (扩 L1, lang 检测内置) | - | off |

**总计**: ~2400 行新代码 + 配置 + 单测.

---

## 可控性 6 大原则 — 实施核对

| 原则 | 实施方式 | 验证 |
|------|---------|------|
| **预算硬墙** | yaml `llm_features.<feature>.daily_max_usd` + global `llm_budget.daily_max_usd: 5.0` | `audit_db.get_feature_cost_today()` 实时查 |
| **输出 schema 强制校验** | `parse_and_validate()` 必走, 失败 drop + log | 单测覆盖 enum / required / type |
| **建议-决策分离** | LLM 出建议 (decision/proposal) 走代码二次校验 (RiskKernel + cooldown + ConfigWriter) | 各 feature 文档明示 |
| **A/B 隔离** | 默认全部 disabled, yaml 单点开 | `test_all_features_default_disabled_in_yaml` 强制断言 |
| **回滚能力** | L8 走 ConfigWriter PARAM_MAP (max_delta) + proposals.db 暂存 | 现有 ConfigWriter rollback API |
| **离线 fallback** | LLM 调用失败 → return None, 业务降级 | `llm_helper.call_llm` 失败处理 + audit 记 success=False |

---

## 落地数据库 (新增 5 个)

| DB | 表 | 用途 |
|---|---|---|
| `data/llm_audit.db` | `llm_calls` | 所有 LLM 调用审计 (feature/model/cost/latency) |
| `data/news_quality.db` | `news_classifications` | L1 新闻分类结果 |
| `data/news_attribution.db` | `news_attribution` | L1 信号 T+24h 回看, hit_rate 统计 |
| `data/emergency_reviews.db` | `emergency_reviews` | L2 紧急风控决策审计 |
| `data/earnings_attribution.db` | `earnings_signals` | L4 财报后信号 + 24h 回看 |
| `data/market_scenario.db` | `market_scenarios` | L6 场景标签时间序列 |
| `data/auto_tuner_proposals.db` | `tuning_proposals` | L8 自调参提议 (pending/canary/full) |

---

## yaml 配置

新增节: `llm_features.*` (config/unified_config.yaml lines ~50-130)

```yaml
llm_features:
  news_classifier:        { enabled: false, model: minimax, daily_max_usd: 0.50 }
  news_signal_gen:        { enabled: false, min_credibility: 0.6, ... }
  news_multilang:         { enabled: false }
  emergency_review:       { enabled: false, model: qwen, daily_max_usd: 0.30 }
  thesis_inferer:         { enabled: false, model: deepseek, daily_max_usd: 0.40 }
  earnings_analyst:       { enabled: false, model: qwen, daily_max_usd: 0.40 }
  disagreement_arbitrator:{ enabled: false, daily_max_calls: 10, ... }
  regime_classifier:      { enabled: false, interval_minutes: 30 }
  large_position_review:  { enabled: false, position_pct_threshold: 0.05 }
  daily_auto_tuner:       { enabled: false, model: deepseek }
  replay_dialog:          { enabled: true, daily_max_usd: 1.00 }
  sector_correlation:     { enabled: false, anchor_move_threshold: 0.02 }
```

---

## 启用顺序建议 (模拟盘验证)

各 feature 互相**独立**, 可任意单开. 建议从风险最小开始:

| 阶段 | 启用 | 验证目标 |
|-----|------|---------|
| **第 0 步** | replay_dialog (已开) | `python scripts/why.py NVDA 2026-05-14` 可正常输出 |
| **第 1 周** | news_classifier (L1) | 看 `data/news_quality.db.news_classifications` 落库, 抽样 10 条手工核对分类质量 |
| **第 2 周** | regime_classifier (L6) + thesis_inferer (L3) | 30 分钟 1 次场景标签 + 入场后 thesis 落 trade_attribution |
| **第 3 周** | news_signal_gen (L9) | L1 已稳后开 L9, 观察 signal_arbitration 多源候选 |
| **第 4 周** | emergency_review (L2) + earnings_analyst (L4) | 触发驱动型, 看 emergency_reviews / earnings_signals 表 |
| **第 5 周** | disagreement_arbitrator (L5) + large_position_review (L7) | 投票分歧 / 大仓时启用 |
| **第 6 周** | daily_auto_tuner (L8) + sector_correlation (L11) | 收盘后调参提议 + 行业联动 |
| **第 7 周** | news_multilang (L12) | 多语言新闻支持 |

每阶段验证标准:
- 该 feature 的 audit_db 调用记录正常
- daily_max_usd 不超
- 业务侧无新增异常 (journalctl ERROR 不增)
- 回看准确率 (where 适用) ≥ baseline

---

## 验证命令

```bash
# 1. 单测全绿 (75/75)
PYTHONPATH=venv/lib/python3.8/site-packages \
  /Library/Developer/CommandLineTools/usr/bin/python3 \
  -m pytest tests/coordinator/ -q

# 2. 启用某 feature: 编辑 yaml 后重启
# 例: 开 L1 新闻分类
yq -i '.llm_features.news_classifier.enabled = true' config/unified_config.yaml
systemctl restart ftseek-us

# 3. 实时查 LLM 调用情况
sqlite3 data/llm_audit.db "
SELECT feature, COUNT(*) as n, ROUND(SUM(cost_usd),3) as cost,
       ROUND(AVG(latency_ms),0) as avg_ms
FROM llm_calls
WHERE date(timestamp,'unixepoch','localtime')=date('now','localtime')
GROUP BY feature ORDER BY n DESC
"

# 4. L1 新闻分类抽查
sqlite3 data/news_quality.db "
SELECT event_type, COUNT(*) as n, AVG(credibility) as avg_cred,
       AVG(sentiment) as avg_sent
FROM news_classifications
WHERE classified_at > strftime('%s','now','-24 hours')
GROUP BY event_type ORDER BY n DESC
"

# 5. L8 提议 review (应用前必看)
sqlite3 data/auto_tuner_proposals.db "
SELECT proposal_id, param_key, proposed_value, trigger_mode, confidence,
       SUBSTR(rationale, 1, 80) as rationale
FROM tuning_proposals WHERE status = 'pending' ORDER BY created_at DESC LIMIT 10
"

# 6. 复盘对话 (L10 默认开)
python scripts/why.py NVDA 2026-05-14
python scripts/why.py --recent 5 --question "为什么今天亏损?"
```

---

## 紧急关停

任何 feature 出问题, 单点关:
```bash
yq -i '.llm_features.<feature>.enabled = false' config/unified_config.yaml
systemctl restart ftseek-us
```

全部关:
```bash
yq -i '.llm_features.[].enabled = false' config/unified_config.yaml
```

预算超 100% 自动 halt: `audit_db.get_feature_cost_today()` 在 `llm_helper.call_llm` 内被检查, 超 cap 直接 return None.

---

## 价值估算 (启用 P0 L1-L4 后, 模拟盘 30 天预期)

| 项 | baseline | + L1-L4 |
|----|---------|---------|
| 信号源数量 | 1 (因子) | 2 (因子 + LLM 新闻) |
| 黑天鹅响应延迟 | 60-300s | 10-30s (L2 emergency) |
| 退出策略匹配度 | 一刀切 | thesis-driven (L3 + Phase B) |
| 财报季胜率 | ~35% (typical) | ~55% (L4 帮助) |
| **预期胜率** | ~45% | ~52-55% |
| **预期 sharpe** | ~1.0 | ~1.5 |
| **每日 LLM 成本** | $0.84 | ~$1.5-2.0 |

实际数据需模拟盘 30 天观察后修正.

---

**结论**: 12 项全部交付, 默认 off, 75 单测全绿. 模拟盘可逐项打开验证, 任何单项失败可独立关停, 不影响系统其他部分.
