# 催化感知优先排序管线 (Catalyst-Aware Priority Pipeline)

> **状态**: 待执行 (冻结期结束后实施)
> **创建**: 2026-07-06
> **复杂度**: 3处接线, 0个新模块, 0个新DB表
> **原则**: YAGNI / Ponytail — 零件已齐, 只需接线

---

## 一、问题定义

### 现状
系统已有完整的催化检测能力 (Catalyst Detector / Reverse Catalyst / HotDiscovery / Emergency Interceptor), 但检测结果**没有接入交易作战计划的优先级排序**。

### 目标
1. 暴雷/重大利好等明确催化事件 → 盘前计划中该标的自动提权排序
2. 购买力优先分配给有明确催化的标的
3. 无催化的标的按原有 confidence 排序, 不受影响

### 断点定位 (3处死胡同)

| # | 断点 | 位置 | 现状 |
|---|------|------|------|
| A | `catalyst.enabled: false` | `unified_config.yaml` | 盘前催化扫描整个模块关闭 |
| B | `get_reverse_verdicts()` 无人调用 | `reverse_catalyst.py` | 盘中异动结论存入 `_reverse_catalyst_store` 但 playbook pipeline 从不取 |
| C | Screener 无 catalyst 加分 | `playbook/screener.py` | `_calculate_score` 不感知催化事件 |

---

## 二、数据流设计

```
                    ┌──────────────────────────────────────────────────┐
                    │  盘中 (TRADING phase)                             │
                    │                                                  │
                    │  EmergencyInterceptor                            │
                    │    → on_price_anomaly(symbol, change_pct)        │
                    │    → ReverseCatalyst.search_news + quick_debate  │
                    │    → CatalystVerdict {symbol, direction,         │
                    │        strength, confidence, severity, reasoning}│
                    │    → _reverse_catalyst_store.append(verdict)     │
                    │                                                  │
                    │  HotDiscovery (盘前异动+新闻催化)                 │
                    │    → discover(battle_plan, news_summary)         │
                    │    → hot_picks: [{symbol, catalyst, change_pct}] │
                    └────────────────────┬─────────────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────────────┐
                    │  盘前 (PRE_MARKET phase)                          │
                    │                                                  │
                    │  run_playbook_generate(scheduler)                │
                    │    ↓                                             │
                    │  Step 1: _get_watchlist(scheduler)               │
                    │    → 静态27只 + factor_engine动态池               │
                    │    ↓                                             │
                    │  Step 2: ★NEW★ _collect_catalyst_context()       │
                    │    ├─ get_reverse_verdicts()  ← 接通断点B         │
                    │    ├─ HotDiscovery.discover() ← 已有, 复用        │
                    │    └─ run_premarket_catalyst_scan() ← 接通断点A   │
                    │    → catalyst_map: {symbol: {severity, direction, │
                    │        headline, strength, source}}              │
                    │    ↓                                             │
                    │  Step 3: PlaybookScreener.screen(watchlist, ...) │
                    │    → ★NEW★ _calculate_score 末尾加 catalyst_boost │
                    │    → score += 10 if CRITICAL, +5 if HIGH          │
                    │    → triggered_factors['catalyst_boost'] = {...}  │
                    │    ↓                                             │
                    │  Step 4: _build_instructions_from_votes()        │
                    │    → VotingCoordinator.vote(symbol, market_data)  │
                    │    → ★NEW★ market_data['catalyst_context'] = ...  │
                    │       (让 LLM 看到催化事件, 自然给出更高confidence) │
                    │    ↓                                             │
                    │  Step 5: L1888 instructions.sort(                │
                    │    key=lambda x: (x['confidence'],                │
                    │                    x.get('catalyst_boost', 0)),   │
                    │    reverse=True)                                 │
                    │    → 购买力优先分配给最强催化信号                   │
                    └──────────────────────────────────────────────────┘
```

---

## 三、实施步骤 (3步接线)

### Step 1: 开启 catalyst 配置 (纯配置变更)

**文件**: `config/unified_config.yaml`
**改动**: `catalyst.enabled: false → true`

```yaml
catalyst:
  enabled: true          # ← false → true
  premarket:
    consensus_threshold: 0.4
    debate_timeout_seconds: 180
    min_severity: HIGH
    scan_news_hours: 12
```

**效果**: `run_premarket_catalyst_scan(scheduler)` 开始执行, 盘前全量扫描过夜新闻 → 分类 → 辩论 → 返回 CatalystVerdict 列表。

**验证**:
```bash
# 日志应出现
grep "\[Catalyst\] 盘前扫描" /opt/ftseek/FTSeek-US/logs/ftseek_us.log
grep "\[Catalyst\] 检出.*个催化事件" /opt/ftseek/FTSeek-US/logs/ftseek_us.log
```

**回滚**: 改回 `false` 即可, 零代码影响。

---

### Step 2: Screener 加 catalyst_boost 分

**文件**: `app/playbook/screener.py`
**函数**: `PlaybookScreener.screen()` 和 `_calculate_score()`

#### 2a. screen() 签名增加 catalyst_map 参数

```python
def screen(
    self,
    watchlist: List[str],
    regime: MarketRegime,
    positions: List[Dict[str, Any]],
    top_n: Optional[int] = None,
    catalyst_map: Optional[Dict[str, Dict[str, Any]]] = None,  # ← NEW
) -> List[ScreenResult]:
```

#### 2b. _calculate_score() 末尾加 catalyst_boost

```python
def _calculate_score(
    self,
    data: PlaybookData,
    positions: List[Dict[str, Any]],
    symbol: Optional[str] = None,           # ← NEW (用于查 catalyst_map)
    catalyst_map: Optional[Dict] = None,    # ← NEW
) -> tuple[int, Dict[str, Any]]:
    # ... 原有因子计算不变 ...

    # ── ★NEW: 催化加成 ──
    if catalyst_map and symbol:
        bare_sym = symbol.replace("US.", "")
        cat = catalyst_map.get(symbol) or catalyst_map.get(bare_sym)
        if cat:
            severity = cat.get("severity", "MEDIUM")
            cat_direction = cat.get("direction", "")
            if severity == "CRITICAL":
                score += 10
            elif severity == "HIGH":
                score += 5
            triggered_factors['catalyst_boost'] = {
                'severity': severity,
                'direction': cat_direction,
                'headline': cat.get("headline", "")[:80],
                'source': cat.get("source", ""),
            }
            logger.info(
                f"[SCREENER] {symbol} catalyst_boost: +{10 if severity == 'CRITICAL' else 5} "
                f"({severity}, {cat_direction})"
            )

    return score, triggered_factors
```

#### 2c. screen() 内部传参

在 `screen()` 的 `for symbol in watchlist:` 循环中:

```python
# 原有:
score, triggered_factors = self._calculate_score(data, positions)

# 改为:
score, triggered_factors = self._calculate_score(
    data, positions, symbol=symbol, catalyst_map=catalyst_map
)
```

**验证**:
```bash
# 日志应出现
grep "\[SCREENER\].*catalyst_boost" /opt/ftseek/FTSeek-US/logs/ftseek_us.log
```

**回滚**: 删除新增的4行即可, 原有逻辑完全不变。

---

### Step 3: Playbook pipeline 接线 (核心)

**文件**: `app/playbook/pipeline.py`
**函数**: `run_playbook_generate()`

#### 3a. 新增 _collect_catalyst_context() 函数

```python
def _collect_catalyst_context(scheduler) -> Dict[str, Dict[str, Any]]:
    """收集所有催化信号, 构建 symbol → catalyst_info 映射.

    数据源 (按优先级):
      1. ReverseCatalyst: 盘中异动 → 新闻确认 → 快速辩论结论
      2. HotDiscovery: 盘前异动 + 新闻催化
      3. run_premarket_catalyst_scan: 盘前全量新闻扫描 + 辩论

    Returns:
        {symbol: {severity, direction, headline, strength, source, reasoning}}
    """
    catalyst_map: Dict[str, Dict[str, Any]] = {}

    # ── Source 1: ReverseCatalyst (盘中积累) ──
    try:
        from app.catalyst.reverse_catalyst import get_reverse_verdicts
        verdicts = get_reverse_verdicts()
        for v in verdicts:
            sym = v.symbol
            if not sym:
                continue
            catalyst_map[sym] = {
                "severity": "HIGH",  # ReverseCatalyst 触发阈值 >=3% 异动
                "direction": v.direction,
                "headline": v.reasoning[:80] if v.reasoning else "",
                "strength": v.strength,
                "confidence": v.confidence,
                "source": "reverse_catalyst",
                "reasoning": v.reasoning,
            }
        if verdicts:
            logger.info(f"[playbook] ReverseCatalyst: {len(verdicts)} 个催化结论")
    except Exception as e:
        logger.debug(f"[playbook] ReverseCatalyst 消费失败: {e}")

    # ── Source 2: HotDiscovery (盘前异动+新闻) ──
    try:
        from app.recommendations.hot_discovery import HotDiscovery
        hd = HotDiscovery()
        plan = getattr(scheduler, 'battle_plan_manager', None)
        plan_dict = plan.get_plan() if plan and hasattr(plan, 'get_plan') else {}
        hot_picks = hd.discover(battle_plan=plan_dict)
        for p in hot_picks:
            sym = p.get("symbol", "")
            if not sym:
                continue
            full_sym = sym if sym.startswith("US.") else f"US.{sym}"
            # 不覆盖 ReverseCatalyst (优先级更高, 有辩论)
            if full_sym not in catalyst_map:
                change = p.get("change_pct")
                severity = "HIGH" if change and abs(change) >= 5 else "MEDIUM"
                catalyst_map[full_sym] = {
                    "severity": severity,
                    "direction": "LONG" if (change and change > 0) else "SHORT",
                    "headline": p.get("catalyst", ""),
                    "strength": int(p.get("signal_strength", 0.5) * 100),
                    "source": "hot_discovery",
                    "reasoning": p.get("rationale", ""),
                }
        if hot_picks:
            logger.info(f"[playbook] HotDiscovery: {len(hot_picks)} 个热点")
    except Exception as e:
        logger.debug(f"[playbook] HotDiscovery 失败: {e}")

    # ── Source 3: PremarketCatalystScan (盘前全量新闻辩论) ──
    try:
        from app.catalyst.detector import run_premarket_catalyst_scan
        verdicts = run_premarket_catalyst_scan(scheduler)
        for v in verdicts:
            sym = v.symbol
            if not sym:
                continue
            # 不覆盖已有的更高优先级来源
            if sym not in catalyst_map:
                catalyst_map[sym] = {
                    "severity": "HIGH",
                    "direction": v.direction,
                    "headline": v.reasoning[:80] if v.reasoning else "",
                    "strength": v.strength,
                    "confidence": v.confidence,
                    "source": "premarket_catalyst",
                    "reasoning": v.reasoning,
                }
    except Exception as e:
        logger.debug(f"[playbook] PremarketCatalystScan 失败: {e}")

    if catalyst_map:
        logger.info(
            f"[playbook] 催化上下文收集完成: {len(catalyst_map)} 只标的 "
            f"= {list(catalyst_map.keys())}"
        )
    return catalyst_map
```

#### 3b. run_playbook_generate() 中调用

在 `_get_watchlist(scheduler)` 之后、`screener.screen()` 之前:

```python
# 原有:
watchlist = _get_watchlist(scheduler)

# 新增下一行:
catalyst_map = _collect_catalyst_context(scheduler)

# 原有 screen 调用改为:
screen_results = screener.screen(
    watchlist=watchlist,
    regime=regime_ctx,
    positions=_get_positions_as_dicts(scheduler),
    catalyst_map=catalyst_map,    # ← NEW
)
```

#### 3c. _build_instructions_from_votes() 注入 catalyst_context 到 voting

在 `_build_vote_market_data()` 构造的 market_data 中注入催化上下文, 让 LLM 自然感知:

```python
# 在 _build_instructions_from_votes() 的 for 循环内, vote() 调用前:
if catalyst_map:
    bare_sym = symbol.replace("US.", "")
    cat = catalyst_map.get(symbol) or catalyst_map.get(bare_sym)
    if cat:
        market_data["catalyst_context"] = cat
        # 让 LLM prompt 看到: "该标的有催化事件: {headline}, 方向={direction}, 强度={strength}"
```

需要给 `_build_instructions_from_votes()` 增加 `catalyst_map` 参数, 并从 `run_playbook_generate()` 传入。

#### 3d. L1888 排序加 catalyst_boost 作为 tiebreaker

```python
# 原有 (L1888):
instructions.sort(key=lambda x: x.get("confidence", 0) or 0, reverse=True)

# 改为:
instructions.sort(
    key=lambda x: (
        x.get("confidence", 0) or 0,
        x.get("catalyst_boost", 0) or 0,
    ),
    reverse=True
)
```

需要在 `_instruction_to_db_dict()` 中把 screener 的 `catalyst_boost` 透传到 instruction:
```python
# 在 _build_instructions_from_votes() 构建 Instruction 时:
inst.catalyst_boost = sr.factors.get('catalyst_boost', {}).get('severity') == 'CRITICAL' and 10 or \
                      (sr.factors.get('catalyst_boost', {}).get('severity') == 'HIGH' and 5 or 0)
```

或者更简单: 直接在 instruction dict 里塞 `catalyst_boost` 字段, L1888 排序时读。

---

## 四、验证清单

### 代码验证
| 检查项 | 方法 |
|--------|------|
| catalyst.enabled 生效 | `grep catalyst /opt/ftseek/FTSeek-US/config/unified_config.yaml` |
| _collect_catalyst_context 被调用 | `grep "_collect_catalyst_context" /opt/ftseek/FTSeek-US/app/playbook/pipeline.py` |
| screener 接收 catalyst_map | `grep "catalyst_map" /opt/ftseek/FTSeek-US/app/playbook/screener.py` |
| L1888 排序含 catalyst_boost | `grep "catalyst_boost" /opt/ftseek/FTSeek-US/app/playbook/pipeline.py` |

### 运行验证
| 检查项 | 日志关键词 |
|--------|-----------|
| ReverseCatalyst 有产出 | `[ReverseCatalyst].*结论` |
| HotDiscovery 有产出 | `[HotDiscovery].*热点` |
| PremarketCatalyst 有产出 | `[Catalyst].*检出.*个催化事件` |
| 催化上下文收集 | `[playbook].*催化上下文收集完成` |
| Screener 加分 | `[SCREENER].*catalyst_boost` |
| 排序效果 | battle_plan.json 中 targets 顺序 (有催化的排前) |

### 数据验证
```sql
-- 检查 battle_plan 中有催化的标的是否排前
SELECT symbol, direction, confidence, 
       json_extract(metadata_json, '$.catalyst_boost') as cat
FROM playbook_instructions 
WHERE date = date('now')
ORDER BY confidence DESC;
```

---

## 五、回滚方案

| 步骤 | 回滚操作 | 影响范围 |
|------|---------|---------|
| Step 1 | `catalyst.enabled: true → false` | 盘前催化扫描停止, 0代码影响 |
| Step 2 | 删除 screener 中4行新增代码 | 因子打分回到原样 |
| Step 3 | 删除 `_collect_catalyst_context` + 恢复 screen() 调用 + 恢复 L1888 排序 | pipeline 回到原样 |

每步独立, 可单独回滚。

---

## 六、风险评估

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| CatalystDebate 超时拖慢盘前流程 | 中 | 盘前计划延迟 | `debate_timeout_seconds: 180` 已有超时保护 |
| 催化加分导致低质量标的入场 | 低 | 亏损 | catalyst_boost 只影响排序, 不跳过质量门禁 (min_confidence/min_score 不变) |
| ReverseCatalyst 误报 (非催化异动) | 中 | 错误提权 | ReverseCatalyst 有 quick_debate 验证, 无新闻时降级为 PRICE_ANOMALY (severity=MEDIUM) |
| LLM 因 catalyst_context 过度乐观 | 中 | confidence 虚高 | LONG 双门槛 (strength≥50 AND confidence≥0.6) 仍生效; catalyst_boost 只是 tiebreaker |

---

## 七、后续扩展 (不在本次范围)

1. **盘中实时扩池**: ReverseCatalyst 检测到非 watchlist 标的催化 → `add_intraday_alert()` 注入动态池
2. **催化IC追踪**: 记录 catalyst_boost → 信号 → 交易结果, 验证催化加分的预测力
3. **暴雷做空优先**: CRITICAL 级别 + direction=SHORT → 额外 +5 分 (暴雷做空胜率更高)
4. **Deepulse 主题提权**: theme_momentum > 0.5 的标的 → screener 加分 (当前只注入 signal_context 不影响 score)
