# DEB-20260930 R1 — 入场价机制提案 (角色: 架构)

> **角色**: 架构  
> **模型**: glm-5.2  
> **日期**: 2026-09-30  
> **纪律**: record-only 分析阶段, 不修改任何生产代码/配置/DB, 不下交易指令。  
> **预注册约束**: ADR-035 C1-C4 (n≥40, 期望改善≥30%, 成交率≥50%, mfe=0 占比降) 已冻结; RFC-081 误杀率≤40% (expired 口径) 已冻结; 判据不可挪, 只可加读数。

## 代码审计基线 (提案前必读)

所有提案基于以下已确认的现有资产与接线点:

| 资产 | 文件 | 函数/位置 | 现状 |
|---|---|---|---|
| 限价路由纯函数 | `app/playbook/entry_routing.py` | `resolve_route()` L135, `chase_cap_violated()` L101, `routing_enabled()` L42 | 开关 `entry_archetype_routing_enabled=false`, 纯函数已实现 |
| 触发时 re-price | `app/playbook/pipeline.py` | L4222-4530, `_new_entry` 计算 + `resolve_route()` 调用 L4254 | BUY→ask, SHORT→bid, archetype 路由替换 entry |
| 指令 dataclass | `app/playbook/instruction_generator.py` | `Instruction` L128, `entry_archetype` L164, `confirmation` L165, `chase_cap_price` L166 | 字段已透传落库 |
| 下单构造 | `app/playbook/playbook_service.py` | `_execute_via_dual_broker` L1172, OrderType.LIMIT L1544 / MARKET L1552 | 路由→LIMIT@确认位, 无路由→MARKET |
| TTL 等待状态机 | `app/playbook/playbook_service.py` | `_handle_routed_fill_wait` L2184 | FILLED_ALL/部分成交/零成交终态已实现 |
| 限价重放影子 | `scripts/cf_pullback_limit_replay.py` | `replay_one()` L80, `_reachability()` L139, `summarize_v2()` L849 | 双臂同尺重放, C3 分层已实现 |
| 延迟确认影子 | `scripts/entry_delay_confirm_shadow.py` | `_eval_delay()` L168, `_agg_block()` L205, `_grad_pass()` L244 | N=2/5 两腿, by_branch 分层已实现 |
| ADR-035 判据 | `docs/adr/ADR-035-entry-archetype-alignment-shadow.md` | §五.2 C3 分层规则 | 桶界 dist_atr≤0.5 锁定, n≥40 裁决资格 |
| RFC-081 规则 | `docs/decisions/RFC-081-entry-delay-confirm-vs-pullback-limit-landing.md` | B/D 分流规则 L263-272 | B=延迟复核(150s), D=回调限价(300s), 互斥 |

---

## 提案 1: 可达性门 — DA 确认位距触发价 ≤0.25 ATR 才路由限价 (其余回退市价/延迟)

### 机制

**问题**: D 案影子 n=86 配对差中位 -$10.64 p=1.0 与 0 不可分辨, 但 C3 分层暴露距离梯度 — `reach_dist_atr<0.25` 子桶 fill 74.4% (29/39) vs `0.25-0.5` 25.6% vs `≥0.5` 0%。当前 `resolve_route()` 只做 `_MAX_LEVEL_DEV=0.25` (25% 偏离) 的数据质量兜底 (entry_routing.py L182-184), 不做 ATR 维度的可达性筛选 — 所有 PULLBACK_LIMIT/FADE_SHORT 指令只要确认位存在就路由限价, 不可达单 100% expired 饿死。

**改动点 (生产路径, 当前 record-only)**:
- `app/playbook/entry_routing.py::resolve_route()` L196 返回前, 新增可达性门:
  - 计算 `reach_dist_atr = |level - market_price| / (atr_pct × market_price)` (需传入 `atr_pct` 参数, 当前签名无)
  - `reach_dist_atr > 0.25` → 返回 `None` (回退现状路径), 并记 warning `[entry_routing] 确认位距触发价 {reach_dist_atr:.2f}ATR > 0.25 阈值, 不可达, 回退现状`
  - `reach_dist_atr ≤ 0.25` → 正常路由限价
- `app/playbook/pipeline.py` L4254 `resolve_route()` 调用处, 补传 `atr_pct` 参数 (当前 `_atr`/`atr_pct` 在 L4279-4290 已计算, 但在路由调用 L4254 时尚未计算 — 需将 ATR 计算提到路由调用之前, 或在路由调用后追加可达性复核)
- **保守替代**: 若不想改 `resolve_route()` 签名, 可在 pipeline L4272-4277 路由消费点追加可达性门: `_arch_route` 已含 `limit_price`, 此时 `_atr` 已算出, 复核 `|limit_price - _new_entry| / (_atr × _new_entry) > 0.25` → 覆写 `_arch_route = None` + warning

**为什么是 0.25 而非 0.5**: ADR-035 §五.2 桶界 `dist_atr≤0.5` 是"可达"定义 (C3 分层用), 不可改。但 **0.25 子桶 fill 74.4%** 是提案的**数据依据** — ADR-035 批注明确"可达性门阈值提案 (0.25 vs 0.5) 以子桶读数为数据依据 — 读数可后加, 判据不可挪"。本提案**不修改 0.5 桶界**, 只新增一个更严格的**路由前置筛选阈值** (0.25), 二者语义不同: 桶界是 C3 判读的分层维度, 路由阈值是"是否走限价"的前置门。

### 防自欺

1. **0.25 是看数据后选的吗?** — 是。C3 子桶 `<0.25ATR fill 74.4%` 是 9/8-9/29 窗 n=86 的读数。但本提案是**新提案** (非 ADR-035 冻结判据), 预注册后需 forward 新窗验证, 不追溯裁已有读数。预注册门: forward n≥40, 可达桶 fill≥50% (沿用 C3 门槛), 不可达桶不因路由门回退市价而恶化净额。
2. **回退市价不等于"没改"** — 不可达单回退市价后仍是市价追单 (与现状一致), 但**可达单走限价后 MFE 可能更高 (+15.5bp 影子读数)**。防自欺: 配对差必须在可达子集上算, 不得把回退市价单混入限价臂。
3. **ATR 口径噪声**: 影子 real:22 / proxy:65 / unknown:52。`reach_dist_atr` 的 ATR 输入质量不均。防自欺: 影子读数按 `atr_basis` 分层报 (已有 `atr_basis_mix` 字段), 裁决日 proxy/unknown 主导时分层降级方向性 (ADR-035 已预注册)。

### 接线

- **消费者**: `pipeline.py` L4254 已调 `resolve_route()`, 传 `atr_pct` 是参数扩展 (非新调用点)
- **影子载体**: `scripts/cf_pullback_limit_replay.py` 已有 `_reachability()` L139 和 `reach_dist_atr` 行级字段 — 影子重放天然按 `reach_dist_atr` 分桶, **零脚本改动**即可复跑出 0.25 子集的配对差
- **开关**: 不新增开关, 复用 `entry_archetype_routing_enabled` — 可达性门是路由内部的子逻辑, 开关关时整体不路由 (零行为变更)
- **无新产物**: 不新建文件/脚本/cron, 只改纯函数签名 + pipeline 调用参数

### 可验证性

- **影子设计**: 复跑 `cf_pullback_limit_replay.py --from-date 2026-09-08 --to-date 2026-09-29`, 从 `pullback_limit_replay_v2_latest.json` 的 rows 筛选 `reach_dist_atr ≤ 0.25` 子集, 重算配对差中位/日聚类 t/误杀率
- **预注册判据草案**:
  - forward n≥40 (可达子集, 沿用 ADR035_VERDICT_MIN_N_PAIRED=40)
  - 可达子集配对差日聚类 day_mean > 0 且 |t| ≥ df 校正临界 (非粗启发 |t|≥2)
  - 可达子集 fill_rate ≥ 50% (C3 门槛不变)
  - 误杀率 (expired 口径) ≤ 40% (RFC-081 冻结)
  - 保守替代: 若可达子集 n 不足 (0.25 子桶当前 39/86 ≈ 45%, forward 窗可能更少), 允许展宽到 0.3 ATR 重攒 — 但 0.3 须预注册为新阈值, 不得看数据后挑
- **n 地板**: 配对 n≥40 (ADR-035 冻结), 宪章 n≥30 中间态可说方向不裁开关

---

## 提案 2: 死亡族 9 点档入场推迟 — 触发价穿越后延 90s 再评估 (仅 ET 9:30-9:45 窗)

### 机制

**问题**: 死亡族 (mfe<0.3%) 20% 笔数净 -$3529, 17/20 在 ET 9 点档入场。全体 9 点档 n=134 净 -$271, 10 点档 n=21 净 +$197。开盘 30min 波动大, re-price 后紧 SL 被开盘波动扫 (MAE/SL 比值 median 0.05 → 多数单根本碰不到 SL, 死亡族是 re-price 后紧 SL 被开盘波动扫)。

**改动点 (生产路径, 当前 record-only)**:
- `app/playbook/pipeline.py` `_check_intraday_trigger()` L5426 命中后, 在 `triggered=1` 设置之前, 新增 ET 时段门:
  - 取 `now_et()` (仓内已有 `app/utils/timezone.now_et`)
  - 若 `09:30 ≤ now_et < 09:45` (开盘 15min) 且指令无 `intraday_reentry-` 前缀 (豁免规则与 RFC-081 B 案一致):
    - 不立即置 `triggered=1`, 写 `metadata_json._open_delay_arm = {armed_at, arm_price, deadline=armed_at+90s}`
    - 下一轮主循环 (5-6min) 或快循环 (PlaybookExitFastLoop 10s) 复核: `now ≥ armed_at+90s` 时取实时价, 若仍站触发方向 → `triggered=1` 走原路径; 否则 → `skip_reason='open_delay_revoked'`
  - 09:45 后正常触发, 零行为变更
- **与 RFC-081 B 案的区别**: B 案是全局 2min 延迟 (所有触发), 本提案是**时段限定** (仅 9:30-9:45) + **更短延迟** (90s vs 120s)。动机不同: B 案防"假脉冲", 本提案防"开盘波动扫紧 SL"。二者不叠加 — 本提案生效时 B 案开关 `entry_delay_confirm_enabled` 须保持 false (RFC-081 互斥规则 6 同款纪律)。

**为什么不做成纯时间否决门 (C8 前科)**: C8 是"个股盘前跌幅轴否决" — 先查基率与区分度。本提案**不是否决** (不跳过指令), 是**推迟触发** (延 90s 再判), 语义 = 给开盘波动一个冷却窗。但防自欺要求同 C8: 必须先查基率 — 9 点档 n=134 净 -$271 是否由 3-4 笔极端亏损绑架 (抗极值阶梯), 还是系统性?

### 防自欺

1. **9 点档基率先行**: 影子须先复算 9 点档的 trim_ladder (drop 1/3/5 largest abs 后中位/均值) — 若 drop 3 后中位转正, 则 -$271 是尾部驱动, 推迟门不能救 (推迟不改变哪几笔极端亏损)。只有 drop 3 后仍为负, 才说明开盘窗系统性不利。
2. **90s 是看数据后选的吗?** — 90s < RFC-081 B 案 N=2 (120s)。选 90s 的理由: 开盘 15min 内 1min bar 的微结构噪声带通常在 60-90s 消散 (但这是先验, 非数据验证)。防自欺: 90s 须预注册, forward 窗验证, 不得事后调。
3. **不变成"只在 10 点以后交易"**: 推迟门只延 90s, 不跳过。若 90s 后仍触发 → 照常进场。防自欺: 影子须报"推迟后仍触发率" — 若 <30%, 等于变相否决 9 点档全部触发 (供给腰斩风险, RFC-081 共同风险)。
4. **intraday_reentry 豁免**: 与 RFC-081 交互规则 4 一致 — 再入场通道不走延迟门 (语义=出场后快速回接, 加延迟废掉该通道)。

### 接线

- **消费者**: `pipeline.py` L4106 `_check_intraday_trigger()` 返回值 → L4107 `if not triggered: continue`。推迟门在 `_check_intraday_trigger()` 内部实现, 返回 `False` 即自然跳过本轮 (指令保持 pending, 下轮复评)。**零新调用点**。
- **快循环复核**: RFC-081 B 案已设计 `_scan_delay_arms()` 在 PlaybookExitFastLoop 头部 (~35行新函数)。本提案复用同一载体 — 90s 延迟 < 150s TTL, 快循环 10s/轮足以覆盖。若 B 案未落地, 本提案的 ARM/CONFIRM 逻辑需独立实现 (~20行, 与 B 案设计同构)。
- **影子载体**: `scripts/entry_delay_confirm_shadow.py` 已有 `_eval_delay()` L168 — 取 `price_at(entry+N)` 判仍触发与否。本提案只需新增 N=1.5 (90s=1.5min) 腿, 在 `DELAYS_MIN` 元组追加 `1.5`。但当前 `DELAYS_MIN=(2,5)` 是整数分钟, 1.5 需适配 `_min_to_hhmm` (支持小数)。**保守替代**: 直接用 N=2 (120s) 腿的已有读数, 9 点档子集单独切 — `entry_delay_confirm_shadow.py` 的 jsonl 已有 `entry_min` 和 `date`, 可按 `entry_min < "09:45"` 筛子集。
- **开关**: 新增 `entry_open_delay_enabled: false` (默认关, 与 RFC-081 B 案 `entry_delay_confirm_enabled` 独立 — 二者互斥但各有开关)
- **无新产物**: 不新建脚本/cron, 复用 `entry_delay_confirm_shadow.py` 和 PlaybookExitFastLoop

### 可验证性

- **影子设计**: 从 `entry_delay_confirm.jsonl` 筛 `entry_min < "09:45"` 子集, 按现有 `_agg_block()` 算 N=2 腿的回避率/机会成本/误杀率。用 `_eval_delay()` 的 `still_triggers` 标签区分"推迟后仍触发" vs "回避"
- **预注册判据草案**:
  - 9 点档子集 forward n≥30 (宪章地板, 此提案非 ADR-035 裁决, 用宪章门槛)
  - 回避率 ≥ 30% (沿用 RFC-081 B 案门槛)
  - 机会成本 < 0.3% (沿用 RFC-081 B 案门槛)
  - 9 点档子集 day_clustered day_mean > 0 且与 0 可分辨 (df 校正临界)
  - **死亡族捕获率**: 推迟后仍触发组的 mfe<0.3% 占比 vs 落地前基线 (20%) — 须降
- **n 地板**: 宪章 n≥30, 裁决 n≥40 (若提案升级为正式裁决)

---

## 提案 3: 限价锚优化 — 确认位 + 1 tick 让步 (improvement-on-fill 而非纯限价)

### 机制

**问题**: D 案限价臂 mean MFE 0.620% vs 市价臂 0.465% (+15.5bp, 恰好是费用线量级), 但配对差中位 -$10.64 与 0 不可分辨 — 方向有利但幅度不足。同时 expired 远距单饿死 $699 赢家 (误杀 58.3% > 40% 门)。根因: 纯限价单挂在确认位, TTL 内不回踩就 100% expired。若在确认位基础上加 **1 tick (或最小报价单位) 让步**, 稍微放宽成交条件, 可能提高 fill 率而不显著恶化入场价。

**改动点 (生产路径, 当前 record-only)**:
- `app/playbook/entry_routing.py::resolve_route()` L196, `limit_price` 计算改为:
  - BUY: `limit_price = level + tick_size` (比确认位高 1 tick, 更容易被卖盘吃到)
  - SHORT: `limit_price = level - tick_size` (比确认位低 1 tick, 更容易被买盘吃到)
  - `tick_size` 从 `market_price` 推断: ≥$1.00 的股票 tick=$0.01, <$1.00 的 tick=$0.01 (美股统一 $0.01, 但 NMS tick_size rule 对 sub-dollar 有差异 — 保守用 $0.01)
- **或**: 不改 `resolve_route()`, 改 `playbook_service.py` L1546 `price=float(_route["limit_price"])` → `price=float(_route["limit_price"]) + (0.01 if direction=="BUY" else -0.01)`

**为什么是 1 tick 而非 more**: +15.5bp 的 MFE 优势需要入场价不显著恶化。1 tick ($0.01 on a $50 stock = 0.2bp) 对入场价的恶化远小于 15.5bp 的 MFE 优势空间。但 fill 率提升幅度未知 — 这是本提案的核心待验证假设。

**与 RFC-081 D 案的关系**: D 案是"确认位限价, TTL 300s, 不成交放弃"。本提案是 D 案的**参数微调** (limit_price ± tick), 不改 TTL, 不改路由逻辑, 不改状态机。若 D 案过线接线, 本提案可作为 D 案的**第二参数**同批验证; 若 D 案不过线, 本提案无独立意义 (没有路由就没有限价单)。

### 防自欺

1. **fill 率提升 ≠ 净额改善**: fill 率从 50% 提到 60% 但每笔多付 $0.01 × qty — 影子须同时报 fill_rate 和配对差净额, 不得只报 fill_rate。
2. **tick 让步是"追"还是"等"**: BUY +tick = 比确认位高 1 tick 挂单 = 更接近市价 = 轻微追。这与 DA "低吸限价" 语义有张力。防自欺: chase_cap 仍全局兜底 (L4660 `chase_cap_violated()` 原位评估), +tick 后若越过 chase_cap → 仍 skip (已验活门)。
3. **1 tick 在不同价位不对等**: $5 股票 1 tick=0.2bp, $500 股票 1 tick=0.002bp。影子须按价位分层报 fill_rate 改善, 不得只报全体均值。

### 接线

- **消费者**: `resolve_route()` L196 返回 `limit_price` → pipeline L4277 `_new_entry = float(_arch_route["limit_price"])` → playbook_service L1546 `price=float(_route["limit_price"])`。改 `resolve_route()` 一处, 全链路透传。
- **影子载体**: `scripts/cf_pullback_limit_replay.py::replay_one()` L80 — 当前 `entry = min(lvl, b[1])` (BUY, L104) / `max(lvl, b[1])` (SHORT, L107)。模拟 tick 让步: 改 `lvl` 为 `lvl ± 0.01` 重跑, 或在 `replay_one()` 加 `tick_offset` 参数。**保守替代**: 不改 `replay_one()`, 在 build_rows 时覆写 `conf["price_level"] += 0.01` (BUY) — 但这污染原影子 jsonl, 须写独立输出文件。
- **开关**: 不新增开关。若 D 案 `entry_archetype_routing_enabled=true` 接线, tick 让步作为 `resolve_route()` 内部参数, 可用 `entry_archetype_tick_offset: 0` (默认 0 = 不让步, 与现状一致) 控制。
- **无新产物**: 不新建脚本/cron/文件

### 可验证性

- **影子设计**: 在 `cf_pullback_limit_replay.py` 中对 `replay_one()` 的 `lvl` 参数做 ±$0.01 敏感性扫描 (两臂: 原始 lvl vs lvl+tick), 同一配对群重跑, 比 fill_rate 和配对差
- **预注册判据草案**:
  - 配对 n≥40 (可达子集, 若与提案 1 叠加)
  - tick 让步臂 fill_rate > 原始臂 fill_rate (方向性, 无固定阈值 — 新读数不挪 C3 门槛)
  - tick 让步臂配对差 day_mean > 原始臂 day_mean (方向性)
  - tick 让步臂配对差不恶化 (day_mean 不显著低于原始臂, df 校正)
  - 误杀率 (expired 口径) 改善 (方向性, 不挪 40% 门)
- **n 地板**: 配对 n≥40 (若独立裁决), n≥30 (宪章中间态)

---

## 提案 4: 入场质量遥测门 — mfe<0.3% 死亡族入场特征画像 (record-only, 不拦截)

### 机制

**问题**: 死亡族 20% 笔数净 -$3529, 17/20 在 ET 9 点档入场。但死亡族的**入场特征** (archetype / anchor_dev_pct / source / vol_bucket / regime) 未在触发时被系统记录为"死亡族风险特征" — 事后复盘才知道是死亡族。本提案不在入场时拦截 (C8 前科: 不做纯时间否决门), 而是**在 re-price 时追加死亡族风险画像遥测**, 为后续提案提供数据基础。

**改动点 (纯遥测, 零行为变更)**:
- `app/playbook/pipeline.py` L4503 `_reprice_params` 构造后, 追加死亡族风险画像字段到 `metadata_json` (via `merge_instruction_metadata`, 同 L4543 reprice_audit 模式):
  - `entry_time_et`: ET 时分 (已有 `_now_et()`)
  - `is_open_window`: `09:30 ≤ entry_time_et < 09:45`
  - `anchor_dev_pct`: 从 `inst.get("anchor_dev_pct")` (Instruction dataclass L159 已有)
  - `entry_archetype`: 从 `parse_raw_semantics()` (已有)
  - `vol_bucket`: 从 `_vol_info.get("bucket")` (L4292 已算)
  - `regime`: 从 `inst.get("market_regime")` (已有)
  - `death_cluster_risk_score`: 加权分 (开盘窗 +0.4, anchor_dev>0.5% +0.3, vol_bucket high +0.2, regime≠RISK_ON +0.1) — **纯观测分, 不拦截**
- **或 (更保守)**: 不加 `death_cluster_risk_score` (防"看数据后凑权重"), 只记录原始特征字段, 由事后分析脚本算画像

**为什么不直接拦截**: C8 前科 — "个股盘前跌幅轴否决"未先查基率就被否掉。死亡族 17/20 在 9 点档是事后归因, 9 点档 n=134 中也有赢家 (否则净 -$271 而非 -$134×2)。直接拦截 9 点档 = 否决 134 笔中约 117 笔非死亡族。本提案只画像, 不拦截, 攒够 forward 数据后再由提案 2 (时段推迟门) 或新提案做拦截裁决。

### 防自欺

1. **画像不是判据**: `death_cluster_risk_score` 是观测字段, 不进 ADR-035 判据, 不进 RFC-081 门槛。防自欺: 画像字段在 `metadata_json` 中, 不在 `_reprice_params` 中 (不传给 executor, 不影响下单)。
2. **权重是拍脑袋的**: `death_cluster_risk_score` 的权重 (0.4/0.3/0.2/0.1) 无数据依据。防自欺: **保守替代方案 = 不算分, 只记原始字段**, 由 `scripts/` 事后分析脚本 (如 `entry_quality_profile.py`, 新建但纯分析非生产路径) 算画像。若选择算分, 须预注册权重并 forward 验证。
3. **死亡族定义是 mfe<0.3%, 但 mfe 是事后量**: 入场时不知道 mfe。画像只能记"入场时的风险特征", 不能记"是否死亡族"。防自欺: 画像字段命名不带 "death" / "risk" 判断词, 只记客观特征 (`is_open_window`, `anchor_dev_pct`), 画像分析留给事后脚本。

### 接线

- **消费者**: `pipeline.py` L4543 `merge_instruction_metadata()` 已在 re-price 后调用 (写 `reprice_audit`)。本提案在同一 `merge_instruction_metadata` 调用中追加画像字段 — **零新调用点**, 只扩 dict 内容。
- **影子载体**: 无现成影子直接消费画像字段。但 `cf_pullback_limit_replay.py` 的 `build_rows()` L319 已从 `raw_instruction_json` 和 `trade_records.metadata_json` 提取字段 — 画像字段写入 `metadata_json` 后, replay 天然可读。`entry_delay_confirm_shadow.py` 同理。
- **分析脚本 (可选新建)**: `scripts/entry_quality_profile.py` — 从 `trade_records` + `playbook_instructions` JOIN, 按画像字段分组算 mfe<0.3% 占比。但**这不是新产物** — 它是分析脚本 (非生产路径), 与 `exit_scorecard.py` 同类, 不进 cron, 不消费生产数据流。若用户认为"分析脚本也算新产物", 则不建, 直接用 SQL 查 + 手动分析。
- **开关**: 不需要开关 — 纯遥测字段写入 `metadata_json`, 无行为变更

### 可验证性

- **影子设计**: 不需要影子重放 — 直接从 `trade_records.metadata_json` 提取画像字段, 按 mfe<0.3% (死亡族) vs mfe≥0.3% (翻绿族) 分组, 算各特征的区分度 (似然比 / AUC)
- **预注册判据草案 (画像验证, 非开关裁决)**:
  - n≥180 (全体 9 月样本, 宪章 n≥30 ×6)
  - 至少一个特征的死亡族 vs 翻绿族区分度 |OR| > 2.0 (方向性, 无固定阈值)
  - 画像组合的 AUC > 0.65 (方向性 — 若 AUC ≤ 0.5 则画像无区分度, 退役)
  - 9 点档特征 forward 窗验证 (9/10-9/29, n≥40): 死亡族占比是否仍 >15%
- **n 地板**: 宪章 n≥30 (画像验证), 裁决 n≥40 (若画像升级为拦截门)

---

## 提案 5: expired 单二阶回收 — TTL 到期不放弃而是降级限价 (限价→改善限价→放弃)

### 机制

**问题**: D 案 expired 远距单饿死 $699 赢家, 误杀 58.3% > 40% 门。当前 `_handle_routed_fill_wait()` L2194 状态机: TTL 到期零成交 → 撤单 + `skip_reason=archetype_ttl_expired` 终态。**绝不降级市价** (ADR-035 设计原则, 消除"低吸变追高")。但"绝不降级市价"不等于"不能改善限价位" — TTL 到期后可以**在更靠近市价的位置重新挂限价** (仍非市价), 给第二次机会。

**改动点 (生产路径, 当前 record-only)**:
- `app/playbook/playbook_service.py::_handle_routed_fill_wait()` L2194, TTL 到期零成交分支:
  - 当前: 撤单 → `_skip_terminal("archetype_ttl_expired")` 
  - 改为: 撤单 → 检查 `_retry_enabled` (新配置 `entry_archetype_retry_enabled: false`):
    - 若 enabled 且 retry 次数 < 1: 在 `limit_price ± retry_offset` (如距市价 0.1 ATR 内) 重新挂限价单, 新 TTL = 原 TTL × 0.5 (150s)
    - 若 retry 仍 expired 或 retry disabled: 原 `_skip_terminal("archetype_ttl_expired")`
- **retry_offset 计算**: `min(market_price × 0.001, original_limit_price ± 0.05)` — 不超过 10bp 让步 (远小于 chase_cap 禁追线, chase_cap 仍全局兜底)
- **与提案 3 (tick 让步) 的关系**: 提案 3 是首次挂单就让步, 提案 5 是首次 expired 后二次挂单让步。二者可叠加 (首次 tick 让步 + 二次 retry), 也可独立。但叠加 = 两次让步 = 入场价恶化叠加, 影子须报累计入场价偏移。

**为什么不直接降级市价**: ADR-035 明确"绝不降级市价 (消除低吸变追高机制根因)"。本提案尊重此原则 — retry 仍挂限价 (非市价), 只是价位更靠近市价。若 retry 也 expired, 放弃 (不追)。

### 防自欺

1. **retry 是"追"吗?**: retry 把限价位从确认位移向市价 = 方向性追。但仍在限价框架内 (不市价成交, 有成交价上限)。防自欺: chase_cap 全局兜底 — retry 后的 `limit_price` 须经 `chase_cap_violated()` 复核 (L4660 已有), 越线则 skip (不再 retry)。
2. **retry 拯救的是赢家还是输家?**: expired 单中 $699 赢家被饿死, 但也有 $276 输家被躲掉 (RFC-081 盲区 3: expired 24 笔合计放弃实盘净利 +$361 = 躲烂单只在子集成立)。retry 会同时拯救赢家和输家。防自欺: 影子须报 retry 后成交组的净额 vs expired 放弃组的净额, 不得只报"多救了 $699 赢家"。
3. **retry TTL = 150s + 原 TTL 300s = 总 450s**: 资金占用周期从 300s 延到 450s, 单日帽消耗节奏变慢。防自欺: 影子须报 retry 落地后的日均成交笔数 (RFC-081 共同风险: 日均 <3 笔触发供给复审)。
4. **retry_offset = 10bp 是拍脑袋的**: 无数据依据。防自欺: 预注册 10bp, forward 验证, 不得事后调。保守替代: retry_offset = 原始确认位到 TTL 到期时市价距离的 50% (动态, 非固定) — 但动态 offset 的影子模拟更复杂。

### 接线

- **消费者**: `_handle_routed_fill_wait()` L2184 是 `_execute_via_dual_broker` L1601 的直接调用。retry 逻辑在 `_handle_routed_fill_wait` 内部, 不改调用方。
- **撤单 API**: `app/execution/dual_broker.py::cancel_order` 已存在 (ADR-035 追加)。retry 需要撤单 + 重挂, 复用同一 API。
- **影子载体**: `scripts/cf_pullback_limit_replay.py::replay_one()` L80 — 当前 TTL 到期返回 `{"status": "expired_unfilled"}`。模拟 retry: 在 `replay_one()` 中, expired 后追加二阶扫描 — 用剩余 bar (TTL 到期后 150s 内) 按 `retry_limit_price` 重判 fill。**保守替代**: 不改 `replay_one()`, 在 `summarize_v2` 中对 expired 行追加 retry 模拟 (用 bar 数据重判) — 但这需要 `replay_one` 暴露 bars 给 summary, 当前架构不支持。
- **开关**: 新增 `entry_archetype_retry_enabled: false` (默认关), `entry_archetype_retry_ttl_seconds: 150`, `entry_archetype_retry_offset_bp: 10`
- **无新产物**: 不新建脚本/cron/文件, 只改 `_handle_routed_fill_wait` 内部逻辑

### 可验证性

- **影子设计**: 在 `cf_pullback_limit_replay.py` 中对 expired 行模拟 retry: 取 TTL 到期时点后 150s 的 bar, 用 `retry_limit_price = min(market_price × 0.999, original_limit + 0.1 ATR)` (BUY) 重判 fill, 算 retry 组的配对差 vs 原始 expired 放弃组的净额
- **预注册判据草案**:
  - expired 子集 n≥40 (retry 的验证人群 = 原始 expired 单)
  - retry 后 fill_rate > 原始 expired fill_rate (=0%, 方向性)
  - retry 成交组净额 > expired 放弃组净额 (day_mean 比较, df 校正)
  - retry 成交组误杀率 ≤ 40% (RFC-081 expired 口径, 不挪门)
  - retry 不恶化总误杀率: (原始 fill 误杀 + retry 成交误杀) / (原始 fill + retry fill) ≤ 40%
- **n 地板**: expired 子集 n≥40 (ADR-035 冻结), 宪章 n≥30 中间态
