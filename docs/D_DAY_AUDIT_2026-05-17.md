# D-DAY 全链路深度审计 — 周一开盘前最终验证 (2026-05-17)

> **目标**: 确认周一开盘后, FTSeek-US (paper) 可正常按照设计使用和验证
> **范围**: 12 维度全链路 — 启动/配置/数据库/LLM/盘中路径/钩子/环境/烟测/历史污染
> **结论**: ✅ **READY FOR MONDAY OPEN** (213/213 测试全绿, 3 个隐藏 P0 已修)

---

## 1. 12 维度验证结果

| # | 维度 | 状态 | 结果 |
|---|---|:---:|---|
| D1 | 启动顺序 (`main.py`) | ✅ | 入口流程完整, init order 无环 |
| D2 | 回归套件 | ✅ | 213/213 全绿 (排除 2 个长期跳过) |
| D3 | yaml 配置 `unified_config.yaml` | ✅ | 修复 duplicate `llm:` key 后 4 模型 / 11 features 路由正常 |
| D4 | 数据库 schema | ✅ | `signal_records` / `trade_records` / `llm_audit` / `position_state` 列齐 |
| D5 | 死代码/旧链路 | ✅ | `multi_model_client.chat` 0 真实调用; `llm_helper` 4 残留全在 `enabled=false` 路径 |
| D6 | 残留 `llm_helper` 调用 | ✅ | 仅 strategy_engine 死分支, 不进盘中 |
| D7 | 盘中热路径 0 LLM | ✅ | `intraday_scorer.py` / `signal_pipeline.py` / `pure/technical.py` / `pure/fast_lane.py` 0 调用 |
| D8 | Hook 集成 | ✅ | PostTradeHook + F1 反向锁 + R1 SymbolLock 全部绑定 |
| D9 | 环境变量 | ✅ | 4 个 LLM API Keys + Futu + 钉钉 + 飞书 全部解析 |
| D10 | 烟测 14 个关键模块 | ✅ | 全部 import 通过 |
| D11 | PartialFill 未消费 | ⏸ | 已知遗留, 不阻塞 (paper 不触发) |
| D12 | 历史污染 | ✅ | 已清理 |

---

## 2. 本次审计发现的 3 个 P0 隐藏问题 + 修复

### P0-1. yaml duplicate `llm:` key (致命 — 路由完全失效)

**症状**:
```python
>>> _get_llm_config().get("models")
{}   # 0 models loaded
>>> _get_llm_config().get("features")
{}   # 0 features
```

**根因**: `config/unified_config.yaml` 有 2 个顶层 `llm:` key
- L107: 新加的 `models / features / defaults` (本批迁加)
- L1066: 老 `strategy_engine` 配置 (历史遗留)

YAML 解析后, 第二个 `llm:` 覆盖了第一个 → 整个新 LLM 路由失效, model_call.call_llm 永远走 "all_models_failed" 路径.

**修复**: 删除独立的 `llm:` (L1066), 把 `strategy_engine` 内嵌到主 `llm:` 段:
```yaml
llm:
  models: {...}
  features: {...}
  strategy_engine: {enabled: true, model: qwen-max, ...}   # 内嵌
```

**验证**:
```
llm.models: ['qwen-flash', 'qwen-plus', 'deepseek-flash', 'deepseek-pro']
llm.features count: 11
thesis_inferer    → deepseek-flash (deepseek-v4-flash)
premarket_planner → qwen-plus / deepseek-pro (cache_on_fail)
regime_classifier → qwen-plus / deepseek-pro
weekly_proposer   → deepseek-pro / qwen-plus
earnings_analyst  → qwen-plus / deepseek-pro
```

---

### P0-2. `.env.us` REDACTED 占位 → `int()` 解析失败 → ConfigContainer 整体崩溃

**症状**:
```
ValueError: invalid literal for int() with base 10: 'REDACTED'
[ConfigContainer] unified_config 加载失败: ...
```

**根因级联**: `OPEND_PORT=REDACTED` / `TRADE_ENV=REDACTED` / `BAILIAN_TIMEOUT=REDACTED` 等占位值无法 `int()`, 而 `os.getenv(key, "default")` 当 key 存在 (即使值是 "REDACTED") 时不会返回 default. 任一字段失败 → `config/__init__.py` import 失败 → `config_us.py` import 失败 → `ConfigContainer._load_unified_config` 失败 → 整个 yaml 配置失效 → llm.models = []

**影响层级** (任一失败都级联挂):
1. `config/__init__.py:33` `OPEND_PORT = int(os.getenv(...))`
2. `config/__init__.py:87` `BAILIAN_TIMEOUT = int(os.getenv(...))`
3. `config/__init__.py:92-95,97-98` US_CHECK_INTERVAL / US_DEFAULT_STOP_LOSS 等
4. `config/config_us.py:85` 同上
5. `app/core/config_container.py:129` `OpenDConfig.from_env`
6. `app/core/config_container.py:43` `_resolve_trade_mode` (TRADE_ENV)

**修复**: 3 文件均加 `_safe_int` / `_safe_float` 守卫:
```python
def _safe_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default
```

并补充 `_safe_float` 处理 `US_DEFAULT_STOP_LOSS / TAKE_PROFIT / TRAILING_*`.

**TRADE_ENV 额外安全保证**: 解析失败 → **强制 fallback SIMULATE** (最安全默认, 永不误启实盘):
```python
def _resolve_trade_mode() -> TradeMode:
    try:
        _raw = int(os.getenv("TRADE_ENV", "1"))
    except (ValueError, TypeError):
        return TradeMode.SIMULATE   # 任何异常 → 模拟盘
```

---

### P0-3. yaml 路由 OK, 但生产 `.env.us` API Keys 仍是 REDACTED

**当前 dev 环境**: `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` 等为 REDACTED 占位.

**影响**: dev 跑 `call_llm` → primary 失败 → fallback 失败 → cache_on_fail 兜底 (若有缓存) 或返回 `all_models_failed`. **不会崩溃**, 但 LLM feature 无法实际输出.

**生产部署前必检**:
```bash
# 在生产 servidor 上 (39.97.58.4)
echo "DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY" | head -c 20  # 应有真实值
echo "DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY" | head -c 20
```

---

## 3. 周一开盘 deploy checklist

### 3.1 部署前 (Sunday 21:00 ET)

- [ ] **生产 .env.us 替换 REDACTED** → 真实 4 个 API Keys + Futu 账号 + 钉钉 secret
- [ ] **拉最新代码**: `git pull origin main && git log -5 --oneline`
- [ ] **依赖一致**: `pip install -r requirements.txt` (本批 0 新增)
- [ ] **数据库 schema**: `python3 -c "from app.db.signal_db import init_db; init_db()"`
- [ ] **llm_cache 目录**: `mkdir -p data/llm_cache && chmod 755 data/llm_cache`
- [ ] **重启**: `systemctl restart ftseek-us`
- [ ] **健康检查**: `curl http://localhost:8899/health` 应返回 200

### 3.2 开盘后第 1 小时 (Monday 09:30-10:30 ET)

```bash
# 1. yaml 路由实际生效
journalctl -u ftseek-us --since "5 min ago" | grep "ModelCall\|llm.models"
# 期待: 看到 4 个模型注册, 0 个 "未知模型 alias" 错误

# 2. LLM 实际调用 (盘前 + 盘中 thesis)
sqlite3 data/llm_audit.db "
SELECT feature, model, COUNT(*) n
FROM llm_calls WHERE date(created_at) = date('now')
GROUP BY feature, model ORDER BY n DESC;"

# 3. 盘中无阻塞 (intraday hot path 0 LLM)
grep -i "intraday.*latency" logs/ftseek_us.log | tail -10
# 期待: scan_cycle latency < 5s

# 4. PushHub 正常 (告警 + post-trade)
grep "PushHub\|FlagFile" logs/ftseek_us.log | tail -5

# 5. ConfigContainer 加载 (无 REDACTED 错误)
grep "ConfigContainer\|_safe_int" logs/ftseek_us.log | tail -10
# 期待: 0 个 "解析失败" warning
```

### 3.3 开盘后 4 小时 (Monday 13:30 ET)

```bash
# LLM 模型实际使用统计
sqlite3 data/llm_audit.db "
SELECT model, COUNT(*) n, ROUND(AVG(latency_ms)) avg_lat, ROUND(SUM(cost), 4) total_cost
FROM llm_calls WHERE date(created_at) = date('now')
GROUP BY model;"

# 期待: 4 个模型 (qwen-flash / qwen-plus / deepseek-flash / deepseek-pro) 都有调用记录
```

---

## 4. 已知 deferred 项 (不阻塞开盘)

| 项目 | 状态 | 影响 | 推迟原因 |
|---|---|---|---|
| `news_classifier` | 未迁 model_call | 0 (无主流程调用) | 内部 130 行复杂逻辑, 单独 plan |
| `daily_review` | 未迁 model_call | 0 (走 agent_bridge subprocess) | 不同链路, 需先确认是否统一 |
| `large_position_review` | 未迁 + 0 调用 | 0 | 未来盘中触发, 需异步重设计 |
| `emergency_review` | 未迁 + 0 调用 | 0 | 同上 + signal.SIGALRM 兜底 |
| PartialFill 消费 | 缺 | 0 (paper 无此事件) | paper 模式 Futu mock 不发 PartialFill |

---

## 5. 阶段 2 总收益

| 维度 | 状态 |
|---|---|
| 4 模型统一接口 | ✅ qwen-flash/plus + deepseek-flash/pro |
| yaml 单点切换 | ✅ `llm.features` 11 路由 |
| schema 反幻觉 | ✅ 4 feature 加 (thesis/regime/auto_tuner/earnings) |
| 失败降级 | ✅ primary→fallback→cache (3 层) |
| 引用强制 | ✅ thesis_inferer 系统 prompt 加 "必须引用具体数值" |
| audit 追溯 | ✅ `llm_audit.db` 全字段 (latency/cost/cache_hit/cross_check) |
| cross_check 准备 | ✅ earnings_analyst 已具备 (盘后可启用) |
| 0 新依赖 | ✅ |
| 0 破坏 | ✅ multi_model_client / llm_helper 仍可用 (DEPRECATED 注解) |
| 测试 | ✅ 213/213 |

---

## 6. 验证一句话总结

> **可上线**. yaml routing + ConfigContainer 双双从黑色洞 (0 models / 整体崩溃) 修复到全绿. 周一开盘后, 仅需在生产服务器把 `.env.us` 的 REDACTED 替换为真实 API Keys, 重启即生效. **不需要任何代码改动**.
