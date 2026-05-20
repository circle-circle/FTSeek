# LLM Agent 真正影响交易 — 实施 + 启用路径 (2026-05-19)

> **第一性原则**: LLM 只做量化做不到的事 — 跨时间模式识别 + 多源综合 + 因果推理.
> **目标**: 在不确定性中寻找高概率的确定性, 提升胜率与收益.

---

## 1. 已实施 — 4 项 (今晚部署即生效, 但默认 disabled)

### 1.1 砍掉 5 个无用 LLM features (yaml DEPRECATED)

`config/unified_config.yaml` line 218-274:
- ❌ earnings_analyst: 财报数据用日历即可
- ❌ disagreement_arbitrator: 主线 voting 已 5 model
- ❌ regime_classifier: router 规则已够 (待 weekly 复用)
- ❌ large_position_review: 风控硬约束足够
- ❌ sector_correlation: ROI 不明确

每个加 `# DEPRECATED 2026-05-19` 注释 + 数据驱动 reasoning.

### 1.2 ⭐ Pre-Entry Signal Gate — `app/llm/pre_entry_gate.py` (核心)

**真正影响交易决策的 LLM 应用** — 在主线 `_execute_long/short` 入场前调用.

**集成点**: `app/pipeline/order_executor.py`
- `_execute_long` line 1300-1326 (LONG 入场前)
- `_execute_stock_short` line 1166-1191 (SHORT 入场前)

**输入**:
- 候选信号 (symbol/direction/strength/factor_detail)
- 7 天历史 (该 symbol+direction 的 trades/wins/win_rate/avg_pnl)
- 当前 market context (regime/vix/spy_momentum)

**输出**: `GateDecision(go=bool, reason=str, confidence=float, source=str)`

**安全设计 (4 重保护)**:
1. **默认 disabled** (`yaml.llm_features.pre_entry_gate.enabled=false`)
2. **LLM 失败 → go=True** (核心 — 不阻塞主线)
3. **5s 超时** (主线 entry 不能等)
4. **历史 < 5 笔 → 放行** (避免拒绝新机会)

**5min 缓存** 同 symbol+direction 决策, 减成本 + 减延迟.

### 1.3 Failure Mode Miner — `scripts/llm_failure_miner.py`

**周日 cron 跑** — 用 LLM 扫前 7 天 loss trades, 找出可执行失败模式.

输出 `data/llm_proposals/failure_modes_<date>.json`:
```json
{
  "summary": "...",
  "modes": [
    {"id": "M1", "pattern": "LCID LONG 14 笔 0 胜率",
     "action": {"type": "blacklist", "yaml_path": "scalping.blacklist", ...},
     "confidence": "high"}
  ]
}
```

**Dry-run 已验证**: 5/12-5/18 67 losses → 15 聚合模式 + 5 winners.

### 1.4 Adaptive Tuner — `scripts/llm_adaptive_tuner.py`

**每日 04:30 ET cron** — 看当日 + 7d 数据, 输出 yaml 调参建议.

输出 `data/llm_proposals/tuner_<date>.json`:
```json
{
  "proposals": [
    {"yaml_path": "scalping.breakeven_after_minutes",
     "current_value": 6, "suggested_value": 8,
     "reason": "...", "expected_impact_usd_per_day": 50,
     "confidence": "medium"}
  ]
}
```

**人工每周一 review 后采纳**, 替代手工每天改 yaml.

---

## 2. 启用路径 (3 阶段, 数据驱动渐进)

### Phase 1: 今晚部署 (基础设施)
**今天就做**:
```bash
git pull && systemctl restart ftseek-us
sleep 10
journalctl -u ftseek-us | grep "PreEntryGate" | head -5
# 期望: 0 行 (因为 disabled, 但 import 路径正常)
```

**全部 disabled** — 不影响今晚交易. 仅基础设施就绪.

### Phase 2: 周二启用 Pre-Entry Gate (基于今晚 audit raw 数据)

**前置条件** (周二开盘前必查):
```bash
# 1. 看 LLM 是否真的能成功 (基于今晚 L2-B 修复后 audit DB)
sqlite3 -header -column data/llm_audit.db "
SELECT feature, COUNT(*) n,
  SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) ok,
  ROUND(100.0*SUM(success)/COUNT(*),1) pct
FROM llm_calls
WHERE date(timestamp,'unixepoch','localtime') = '2026-05-19'
GROUP BY feature ORDER BY n DESC;"

# 2. 看 schema 失败的真实 raw response (诊断)
sqlite3 data/llm_audit.db "
SELECT feature, error_msg, substr(result_summary, 1, 200)
FROM llm_calls WHERE error_msg LIKE 'schema_failed%'
  AND date(timestamp,'unixepoch','localtime') = '2026-05-19'
LIMIT 5;"

# 决策标准:
# - thesis_inferer 成功率 ≥ 30% → 启用 Pre-Entry Gate
# - thesis_inferer 仍 0% → 先调 schema (改 enum / 大小写容错), 不启用 Gate
```

**启用步骤** (周二 21:00 北京):
```yaml
# config/unified_config.yaml
llm_features:
  pre_entry_gate:
    enabled: true   # 周二启用
```

**首日观察 (周二开盘 1h 内)**:
```bash
# Pre-Entry Gate 拦截统计
journalctl -u ftseek-us --since "1 hour ago" | grep -c "PreEntryGate.*GO"
journalctl -u ftseek-us --since "1 hour ago" | grep -c "PreEntryGate.*NO"

# 拦截哪些 symbol+direction (期望: LCID LONG / TME LONG 等历史败局)
journalctl -u ftseek-us --since "1 hour ago" | grep "PreEntryGate.*NO" | head -10
```

**A/B 测试 1 周 (周二-下周一)**:
```sql
-- 比较"启用前 5/12-18" vs "启用后 5/20-26" 业绩
SELECT 'before' phase, COUNT(*) n, 
       ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) wr,
       ROUND(SUM(realized_pnl),2) pnl
FROM trade_records WHERE trade_date BETWEEN '2026-05-12' AND '2026-05-18'
  AND realized_pnl IS NOT NULL
UNION ALL
SELECT 'after', COUNT(*), 
       ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1),
       ROUND(SUM(realized_pnl),2)
FROM trade_records WHERE trade_date BETWEEN '2026-05-20' AND '2026-05-26'
  AND realized_pnl IS NOT NULL;
-- 期望: after 胜率 ≥ 12% (vs before 7%), pnl ≥ +$200/天 (vs before -$300/天)
```

### Phase 3: 5/26 后启用 Failure Miner + Adaptive Tuner

**前置条件**: Pre-Entry Gate A/B 测试通过 (after 优于 before).

**部署 cron**:
```bash
# /etc/cron.d/ftseek_us_llm
# 每周日 04:30 ET (= 周一 16:30 北京) Failure Mode Miner
30 04 * * 0 ftseek cd /opt/ftseek/FTSeek-US && /usr/bin/python3 scripts/llm_failure_miner.py >> logs/failure_miner.log 2>&1

# 每日 04:30 ET 后 Adaptive Tuner
35 04 * * 1-5 ftseek cd /opt/ftseek/FTSeek-US && /usr/bin/python3 scripts/llm_adaptive_tuner.py >> logs/adaptive_tuner.log 2>&1
```

**人工 review 工作流** (每周一 09:00 北京):
```bash
# 看 LLM 提的建议
ls -lt data/llm_proposals/

# 看具体内容 + 决定采纳哪些
cat data/llm_proposals/failure_modes_2026-05-26.json | jq '.modes[] | select(.confidence == "high")'

# 采纳的写到 yaml
yq -i '.scalping.blacklist = ["US.LCID:LONG", "US.TME:LONG"]' config/unified_config.yaml
git commit -am "采纳 LLM Failure Miner 建议: blacklist LCID/TME LONG (5/12-19 14+10 笔 0 胜率)"
systemctl restart ftseek-us
```

---

## 3. 真正的 LLM 价值矩阵

| LLM Feature | 价值类型 | 量化预期 | 启用时机 |
|---|---|---|---|
| **Pre-Entry Gate** | 入场前拦截失败模式 | **+$2-5k/周** (拦 LCID LONG 类) | 5/20 (依赖今晚 audit) |
| **Failure Mode Miner** | 周级模式发现 | **替代 noise_pnl 静态归因** | 5/26+ |
| **Adaptive Tuner** | 日级调参建议 | **替代手工调 yaml** | 5/26+ |
| news_classifier (现有, 修后) | 新闻情绪 | 进入信号 confidence | 等 schema 通过率验证 |
| premarket_planner (现有) | 盘前 watchlist | 主线 scan priority | 等 main scan 联动 (Phase 4) |

**5 个真正有价值的 LLM 应用** vs 11 个原设计 features.

---

## 4. 风险与回滚

### 4.1 Pre-Entry Gate 风险

| 风险 | 缓解 |
|---|---|
| **LLM 误判, 拒了真正盈利信号** | A/B 1 周, 数据驱动评估; LLM confidence < 0.7 时 review |
| **LLM 调用 5s 超时拖慢主线** | timeout=5s, 缓存 5min 复用; 失败默认 go=True |
| **LLM 自身故障** | go=True 降级 + 1min 短缓存防反复调失败 LLM |
| **被无故启用 (yaml 误改)** | 默认 disabled + DEPRECATED 注释 + 启动期检查 |

### 4.2 应急回滚

```bash
# 回滚 Pre-Entry Gate (yaml)
yq -i '.llm_features.pre_entry_gate.enabled = false' config/unified_config.yaml
systemctl restart ftseek-us

# 回滚整个 LLM Agent 实施 (4 个 commit)
git log --oneline | head -5
git revert <pre_entry_gate_commit> <yaml_deprecated_commit> ...
systemctl restart ftseek-us
```

### 4.3 应急切断条件

启用 Pre-Entry Gate 后, 任一发生立即 disable:
- LLM 调用超时率 > 30% (latency > 5s)
- 拦截率 > 90% (拦掉太多, 可能 schema 太严)
- 单笔 PnL < -$500 (类似 5/12 QCOM)
- ERROR 日志暴增

---

## 5. 监控仪表盘

```bash
#!/bin/bash
# scripts/llm_agent_dashboard.sh — 每 30min 钉钉推送
echo "=== LLM Agent Dashboard $(date) ==="
echo ""
echo "## 今日 LLM 总调用 / 成功率"
sqlite3 -header -column data/llm_audit.db "
SELECT feature, model, COUNT(*) n,
  SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) ok,
  ROUND(100.0*SUM(success)/COUNT(*),1) pct,
  ROUND(SUM(cost_usd),4) cost
FROM llm_calls 
WHERE date(timestamp,'unixepoch','localtime') = date('now','localtime')
GROUP BY feature ORDER BY n DESC;"

echo ""
echo "## Pre-Entry Gate 决策统计 (期望今晚 0 行 — disabled)"
journalctl -u ftseek-us --since today | grep -c "PreEntryGate"

echo ""
echo "## 最新 Failure Mode 建议"
ls -1t data/llm_proposals/failure_modes_*.json 2>/dev/null | head -1 | xargs cat 2>/dev/null | jq '.modes[] | {id, pattern, confidence, action: .action.type}' | head -20

echo ""
echo "## 最新 Adaptive Tuner 建议"
ls -1t data/llm_proposals/tuner_*.json 2>/dev/null | head -1 | xargs cat 2>/dev/null | jq '.proposals[] | {param: .param_name, suggested: .suggested_value, confidence}' | head -20
```

---

## 6. 测试覆盖

```
tests/coordinator/test_pre_entry_gate.py:
  ✅ test_gate_disabled_returns_go_true        # 默认 disabled 必须放行
  ✅ test_gate_history_insufficient_passes     # 历史不足放行
  ✅ test_gate_llm_failure_falls_back_to_go    # LLM 失败降级 (核心安全)
  ✅ test_gate_llm_rejection_when_history_bad  # 历史 14 笔 0 胜率拦截
  ✅ test_gate_cache_reuse                     # 5min 缓存
  ✅ test_gate_cache_separate_per_direction    # LONG/SHORT 分开缓存
  ✅ test_gate_disabled_no_history_query       # disabled 不查 DB
```

7/7 全过. **完整 coordinator 回归 247/248** (1 个 pre-existing fail 与本次无关).

---

## 7. 一句话总结

> **从"流程化思维"到"价值导向"**: 砍 6 个无用 LLM features, 重构 3 个真正影响交易的 (Pre-Entry Gate / Failure Miner / Adaptive Tuner). 默认 disabled — 今晚仅基础设施就绪, 等周二审视 audit raw 数据后渐进启用.
>
> **关键设计原则**:
> - **Skin in the game**: Pre-Entry Gate 直接修改 entry 决策, LLM 必须为亏损负责
> - **Fail safe**: LLM 失败必降级 go=True, 不阻塞主线
> - **Data-driven**: 启用前必看 audit DB raw, 启用后必跑 A/B 1 周
> - **Simple**: 4 个 LLM 应用 + cron + yaml proposal review, 而非 11 个分散 hook
>
> **预期 ROI**: 启用 Pre-Entry Gate 后 1 周, 拦截 LCID LONG 类历史败局, 预期 +$2-5k/周, 周成本 ~$10. ROI 200-500×.
