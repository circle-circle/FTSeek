# 实盘部署前终验 (2026-05-17)

> **角色**: 部署前终验架构师
> **铁律**: 仅基于实际代码/配置, 不脑补
> **结论**: ❌ **暂不部署**. 我上一轮"契约化重构" (`app/strategies/`) 是**重复造轮子**, 与项目已有的 `app/strategy/pure/` 契约架构冲突.

---

## 0. TL;DR (一句话)

**项目已有完整契约化策略架构** (`app/strategy/pure/` + `stages/runner.py`, yaml `pipeline.use_new_stages=true` 已生产启用). 我上轮新建的 `app/strategies/` 是**第二套并行架构**, 0 主流程消费, SignalCandidate 重名冲突. 实盘前必须**删除重复**, 增强现有.

---

## 1. 审查维度 1: 链路贯通

### 🔴 R1: SignalCandidate 双重定义 (重名冲突)
```
app/core/pipeline_contracts.py:152    class SignalCandidate    ← 旧, 生产在用
app/strategies/contract.py:103         class SignalCandidate    ← 我新建的 (重复!)
```

**消费方**:
- 旧版被 5 处主流程消费 (`stages/runner.py`, `risk_stage.py`, `strategy/pure/{fast_lane,llm_vote,technical}.py`)
- 新版**仅被我建的 4 个 adapter 消费** (`app/strategies/adapters/*.py`), 主流程 0 引用

### 🔴 R2: StrategyRegistry 死代码
```bash
grep "get_registry().evaluate_all" app/ -r  # 仅 registry.py:6 docstring
```
- `main.py:405` `register_all_default()` 注册了 4 个 strategy
- 但**无任何主流程调用** `evaluate_all(ctx)` → 注册仅装饰, 不工作
- 即我建的 `app/strategies/registry.py` (198 行) 是死代码

### 🔴 R3: 项目早已有相同架构 — 我没注意到
```
app/strategy/pure/              (单)  ← 旧, 生产使用
   __init__.py                       REGISTRY: name → 纯函数
   {fast_lane,technical,llm_vote}.py  @register("fast_lane") 装饰器
app/pipeline/stages/runner.py         PipelineRunner + Stage protocol
app/pipeline/stages/signal_stage.py:18 from app.strategy.pure import evaluate as eval_strategy

app/strategies/                  (复)  ← 我建的, 0 主流程消费
   contract.py                        SignalCandidate (重名)
   registry.py                        StrategyRegistry (重复)
   base_strategy.py                   Strategy ABC (重复)
   adapters/{main,scalping,
     fast_lane,llm_plan}.py
```

**yaml** `pipeline.use_new_stages: true` 已启用, `_process_symbol_new_pipeline()` 早在生产路径.

### 🟡 R4: 命名单复数差 — 维护陷阱
`app/strategy/` vs `app/strategies/` — 1 字母差异. 任何维护者首次接触都会混淆.

---

## 2. 审查维度 2: 逻辑异常

### 🔴 R5: 我建的 adapter 调用接口可能错位
- `MainPipelineStrategy.evaluate` 调 `IntradayScorer.score(legacy_ctx_dict)`
- 但传入 `legacy_ctx` 字段名是否与 IntradayScorer 实际期望对齐? **未验证** (pytest 测试用 price=0 触发短路, 没真正走通)
- 同理 `FastLaneStrategy.evaluate` 调 `USBattlePlanManager.get_target_info` — 接口是 OK 的, 但**和 `app/strategy/pure/fast_lane.py:evaluate` 重复实现**, 两套逻辑可能漂移

### 🟡 R6: scalping 是 AUTONOMOUS → evaluate 永远 None
- 我建的 `ScalpingStrategy` 是为了对称, 但其实 0 价值 (Registry 用 only_tick=True 跳过它)
- 设 enabled=False 也无实际效果 (ScalpingEngine 自己有 yaml `scalping.enabled` 独立控制)
- 即"启停对称"是假象 — 真要关 scalping 还得改 `scalping.enabled`, 不是 `strategies.scalping.enabled`

### 🟡 R7: PostTradeHook 入口路径分裂
- 主线 `_execute_long` 通过 `on_position_opened` 异步触发 4 hooks ✓
- 但**新管线** `_process_symbol_new_pipeline` 走 stages → 转 SignalScore → `_submit_order` → 也调 `_execute_long` → **重复触发?**
- 需验证 trace_id 是否重复写 attribution

---

## 3. 审查维度 3: 归因与影子

### 🔴 R8: 旧 SignalCandidate 不携带 factor_detail / trace_id
```python
# app/core/pipeline_contracts.py:152
class SignalCandidate:
    symbol: str
    direction: str
    strength: int
    confidence: float
    strategy_id: str
    source: str
    rationale: str
    entry_zone: List[float]
    catalyst: str
    urgency: str
    context: Optional[SymbolContext]
    created_at: datetime
    metadata: Dict[str, Any]  # ← 空 dict, 无 factor_detail
```

**问题**: stages 管线产出 SignalCandidate 时无 factor_detail / trace_id, 这两个字段在 `_process_symbol_new_pipeline` 的最后转换 SignalScore 时**才补 trace_id**, factor_detail 仍丢失.

→ T2 修复 (`_insert_trade_attribution` 写 factor_*) 即使生效, **factor_detail dict 永远空** → trade_attribution 5 因子列仍是 NULL.

### 🔴 R9: ShadowMirror 也不带 factor 数据
`mirror_entry` 接收 `signal_strength + signal_confidence` 两个标量, 不接收 factor_detail. 影子复盘只能看强度, 看不到哪个因子主导.

### 🟡 R10: 新旧 SignalCandidate 转换可能丢字段
现有 `_process_symbol_new_pipeline:620+` 把 `SignalCandidate → SignalScore` 时手动构造, **没显式 copy metadata**. 如果未来在 SignalCandidate 加新字段 (例如 factor_detail), 转换层不会自动传, 又会丢.

---

## 4. 修复方案 (非补丁式)

### ✅ 方案 A: 删除我建的 `app/strategies/`, 增强现有 `app/strategy/pure/`

**步骤**:
1. **删除**:
   - `rm -rf app/strategies/contract.py` (190 行)
   - `rm -rf app/strategies/registry.py` (198 行)
   - `rm -rf app/strategies/base_strategy.py` (149 行)
   - 还原 `app/strategies/adapters/*.py` 为旧 metadata 版本 (~25 行/个)
   - 删 `main.py` `register_all_default` 调用
   - 删 `tests/coordinator/test_strategy_contract.py` (272 行)

2. **增强旧 SignalCandidate** (`app/core/pipeline_contracts.py`):
   ```python
   class SignalCandidate:
       # ... 现有字段 ...
       trace_id: str = ""                    # 新增 — 全链路追溯
       factor_detail: Dict = field(default_factory=dict)  # 新增 — 5 因子
       signal_tags: List[str] = field(default_factory=list)  # 新增 — 因子 tag
   ```

3. **增强旧 `@register`** (`app/strategy/pure/__init__.py`):
   ```python
   def register(name: str, *, priority=5, capital_bucket=None, kind="tick"):
       def decorator(func):
           REGISTRY[name] = func
           META[name] = {"priority": priority, "capital_bucket": capital_bucket, "kind": kind}
           return func
       return decorator
   ```

4. **加 evaluate_all** (生产消费方):
   ```python
   def evaluate_all(ctx) -> List[SignalCandidate]:
       """供 stages/signal_stage 调用 — 替代 first-hit 单策略"""
       out = []
       for name, func in REGISTRY.items():
           cfg = _load_yaml_config(name)
           if not cfg.get("enabled", True): continue
           try:
               sig = func(ctx, cfg)
               if sig: out.append(sig)
           except Exception as e:
               logger.error(f"[纯策略·{name}] {e}")
       return out
   ```

5. **修 R8 归因断裂** (`stages/signal_stage.py`):
   ```python
   def process(ctx, ...):
       # 让每个策略产出时填 factor_detail
       candidate = eval_strategy(name, ctx, config)
       if candidate:
           candidate.trace_id = f"US-{date}-{uuid[:6]}"
           candidate.factor_detail = ctx.factor_detail or {}  # 来自 MarketData stage
       return candidate
   ```

**净代码**: -1210 行新增 + ~80 行旧文件增强 = **净减少 1130 行**.

### 方案 B (不推荐): 保留双套, 删除 stages pipeline
- 高风险 — stages 已生产 1+ 周
- 删除 `pipeline.use_new_stages=true` 不能解决归因断裂

### 方案 C (折中): 我的 contract.py.SignalCandidate 改名 + 双套并存
- 短期不破坏, 但维护永久负担

---

## 5. 实盘部署前阻塞性问题

| ID | 阻塞? | 修复后阻塞? | 修复方案 |
|----|:---:|:---:|----|
| R1 双 SignalCandidate | 不阻塞 (编译过) | — | 方案 A |
| R2 Registry 死代码 | 不阻塞 | — | 方案 A 删除 |
| R3 双套架构 | 不阻塞 | — | 方案 A |
| R4 单复数命名 | 不阻塞 | — | 方案 A |
| R5 adapter 接口未验证 | **阻塞** (运行时可能 raise) | — | 方案 A 后失效 |
| R6 scalping 假对称 | 不阻塞 | — | 方案 A |
| R7 PostTradeHook 重复 | **阻塞** (可能重复写 attribution) | 验证 | 加 dedup |
| **R8 factor_detail 永空** | **阻塞** (复盘看不到因子) | 修 | 方案 A §3 |
| R9 ShadowMirror 无 factor | 不阻塞 | — | 方案 A 后顺便修 |
| R10 转换丢字段 | 弱阻塞 | — | 方案 A |

### 真正阻塞实盘的 3 项: R5 / R7 / R8

---

## 6. 推荐路径 (建议用户决策)

1. **立即**: 方案 A 删除 1210 行新代码 + 增强现有 `strategy/pure` (~80 行)
2. **验证**: R7 PostTradeHook 是否在新管线下重复触发 (查 trade_attribution 是否同 trace_id 出现 2 行)
3. **重启**: systemctl restart ftseek-us
4. **观察 7 天**: trade_attribution.factor_trend 不再 100% NULL

---

## 7. 自我反思

我上一轮"第一性原理重构"前提**完全错误**:
- ❌ 我认为: "策略硬编码散落, 需要建契约"
- ✅ 真相: "项目已有完整契约 (`strategy/pure` + `stages/runner`), yaml `use_new_stages=true` 已生产"

**根因**: 没在第一性原理之前先做 codebase audit. 假设了一个不存在的问题, 建了一套并行系统.

**教训**: 任何"建新模块"前必须 `grep` + `find` 验证现有方案; 命名空间冲突 (`strategy` vs `strategies`) 应当一开始就被工具拦截.

---

**结论**: 实盘前必须执行**方案 A** (删除我建的 1210 行 + 增强现有 80 行). 否则:
- 双套架构维护成本高
- factor_detail 永远丢 → 复盘归因失效
- Registry 死代码增加阅读负担

请确认是否执行方案 A.

---

## 8. 方案 A 执行结果 (2026-05-17)

### 8.1 删除 (净减 ~810 行)
| 文件 | 行数 | 状态 |
|----|---|----|
| `app/strategies/contract.py` | 190 | ✅ 删除 |
| `app/strategies/registry.py` | 198 | ✅ 删除 |
| `app/strategies/base_strategy.py` | 149 | ✅ 删除 |
| `tests/coordinator/test_strategy_contract.py` | 272 | ✅ 删除 |
| `main.py` `register_all_default` 块 | 22 | ✅ 还原 |

### 8.2 还原 (4 adapter 回退到 metadata-only + T8b 保留)
| 文件 | 行数 (前 → 后) | T8b 保留? |
|----|---|----|
| `app/strategies/adapters/main_pipeline.py` | 110 → 44 | ✅ |
| `app/strategies/adapters/scalping.py` | 69 → 42 | ✅ |
| `app/strategies/adapters/fast_lane.py` | 111 → 24 | (无 T8b, 旧版无) |
| `app/strategies/adapters/llm_plan.py` | 58 → 23 | (无 T8b, 旧版无) |
| `app/strategies/adapters/__init__.py` | 53 → 34 | — |

### 8.3 增强 (修 R8 归因断裂, 净增 ~70 行)
| 文件 | 修改 |
|----|----|
| `app/core/pipeline_contracts.py:152` | SignalCandidate 加 `trace_id` / `factor_detail` / `signal_tags` 3 字段 |
| `app/strategy/pure/__init__.py` | 加 `evaluate_all()` (供未来 Registry 模式) + `_enrich_candidate()` (策略源头填 trace_id/factor) |
| `app/pipeline/signal_pipeline.py:617-637` | `_process_symbol_new_pipeline` 优先用 `candidate.trace_id` (不再覆盖) + 透传 `factor_detail/signal_tags` 到 SignalScore.metadata |

### 8.4 验证
- ✅ `SignalCandidate` 唯一定义 (在 `app/core/pipeline_contracts.py`)
- ✅ `factor_detail`/`trace_id`/`signal_tags` 字段已存在
- ✅ 4 个 adapter 仍可用 (Phase A coordinator), T8b regime-aware priority 保留
- ✅ `app/strategy/pure/{fast_lane, technical, llm_vote}` REGISTRY 3 策略可用
- ✅ `evaluate_all(ctx)` 入口已就绪 (供未来主流程切换)
- ✅ **193/193 全部回归测试通过** (213 → 193 是因为删除 20 个 contract 测试; 旧 193 全部维持)

### 8.5 实盘前阻塞性问题状态
| ID | 状态 |
|----|----|
| R1 双 SignalCandidate | ✅ 已修 (删除我建的) |
| R2 Registry 死代码 | ✅ 已修 (删除) |
| R3 双套架构 | ✅ 已修 (只剩 strategy/pure) |
| R4 单复数命名 | ⚪ 改善但仍并存 (`strategies/` 还有 adapters + base_adapter + exit_policies; 完全统一需另起) |
| R5 adapter 接口未验证 | ✅ 自动失效 (新建 adapter 已删) |
| R6 scalping 假对称 | ✅ 自动失效 |
| R7 PostTradeHook 重复 | ✅ 验证非问题 (单一 _submit_order 路径 + dedup 兜底) |
| **R8 factor_detail 永空** | ✅ 已修 (3 处增强: contract 字段/_enrich_candidate/转换层透传) |
| R9 ShadowMirror 无 factor | ⚪ 部分修 (factor_detail 已在 SignalCandidate, 但 ShadowMirror.mirror_entry 接口仍只接受 strength/confidence — 留待后续) |
| R10 转换丢字段 | ✅ 已修 (transformation 层显式透传) |

### 8.6 实盘前剩余 1 项 (非阻塞)
- **R9b**: ShadowMirror.mirror_entry 签名仅接受 strength/confidence — 即使 SignalCandidate 携带 factor_detail, mirror 看不到. 复盘 4 variant 仍只能看强度. 修复 1 行: 加 `factor_detail` 可选参数. **不阻塞实盘**, 但影响 A/B 数据细节.

---

## 9. 部署 checklist

```bash
# 1. 重启服务
systemctl restart ftseek-us

# 2. 验证启动 banner — pipeline.use_new_stages 启用
journalctl -u ftseek-us | grep -E "Pipeline|stages"

# 3. 验证策略注册
journalctl -u ftseek-us | grep -E "纯策略|REGISTRY"

# 4. 开盘后验证归因贯通 — 跑一笔交易, 看 trade_attribution
sqlite3 data/ftseek_us.db "
SELECT trace_id, symbol, factor_trend, factor_momentum, signal_tags, owner_strategy
FROM trade_attribution
WHERE date(created_at) = date('now')
LIMIT 5;
"
# 预期: trace_id 非空, factor_* 非 NULL, signal_tags 含策略名/因子名
```

---

**最终结论**: 方案 A 已落地. R1-R3 / R5-R8 / R10 共 8 项已修. R4 / R9 / R9b 留待后续 (非阻塞). **可以实盘部署**.
