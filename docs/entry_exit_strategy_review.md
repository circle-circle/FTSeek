# 入场检查与退出策略评审（逐条实证核验）

> **用途**：供另一 Agent 交叉确认。每条均给出**特征字符串**（用于 grep 定位）+ 真实代码片段 + 核验结论。
> **核验日期**：2026-07-16　**目标文件**：`app/playbook/pipeline.py`
> **核验方式**：规范 Grep/Read 工具直接读真实代码，不依赖注释、不依赖记忆。

## ⚠️ 重要声明（先说结论的可靠性）

1. **行号会漂移**：`pipeline.py` 在活跃变动（核验期间行数 4197→4221）。**请以"特征字符串"grep 定位为准，行号仅为参考**。
2. **更早一轮评审作废**：此前曾引用 `exit_manager.py` / `exit_rules.py` / `price_service.py` 及所谓 "bug"，经核验**这些文件均不存在**，是错误结论，已全部作废。本文件为唯一有效版本。
3. 旧的 `docs/misleading_logs_audit.md` 同为该轮错误产物，**应忽略/删除**。

---

## 核验总表

| # | 建议 | 特征字符串 | 状态 |
|---|------|-----------|------|
| 1 | VWAP 门开盘 fail-open | `_gate_reason = "vwap_missing"` | ✅ 代码真实，建议成立 |
| 2 | 追入场景无动量确认 | `_vol_ok = True` | ✅ 代码真实，建议成立 |
| 3 | 入场重锚缺 RR 底线校验 | `entry_reanchor_enabled` | ✅ 代码真实，建议成立 |
| 4 | `MIN_TP_RR=1.5` 硬编码 | `_MIN_TP_RR = 1.5` | ✅ 代码真实，建议成立 |
| 5 | 开盘冷却 15min/4% 需回验 | `cooldown_sl_pct` | ✅ 代码真实，建议成立 |
| 6 | filled_price 覆盖退出价 | `close_res.get("filled_price", 0)` | ✅ **已生效，无需修改** |

---

## 逐条详情

### 1. VWAP 方向一致性硬门 — 开盘时段 fail-open

- **特征定位**：`_gate_reason = "vwap_missing"`（约 L2804）
- **真实逻辑**：
  ```python
  if not _vwap_populated:
      _gate_reason = "vwap_missing"   # fail-open: 无 VWAP 数据不拦
  ```
  拦截条件为 `_gate_enabled and triggered and _gate_block`（约 L2817）。当 VWAP 未就绪（盘初 K 线不足）时 `_vwap_populated=False` → 不拦。
- **核验结论**：代码真实。盘初（playbook 主战场 cohort A）VWAP 缺失时门失效。
- **建议**：盘初 VWAP 未就绪窗口内，用开盘价锚定临时 VWAP 或叠加开盘专用门；**先影子记录"若启用会拦多少"再决定**（fail-open 是有意设计，改动需数据支撑）。

### 2. 追入场景无动量确认（量能门已移除）

- **特征定位**：`_vol_ok = True`（约 L2707）
- **真实逻辑**：追入分支（突破追涨/破位追空）`triggered = _vol_ok`，而 `_vol_ok` 恒为 `True`（量能门已移除）。当前追入仅靠 `CHASE_ZONE_PCT`（+5%）+ 8% 极端 gap 过滤保护。
- **核验结论**：代码真实。移除是有意为之（日级 vol_ratio 对盘中突破零预测力），但追入场景当前无动量确认。
- **建议**：用 1分钟K线聚合"今日累计量/同时段均量"造真盘中 vol_ratio，**先影子验证预测力达标再加回**，不要直接换数据源硬上。

### 3. 入场重锚缺 RR 底线校验

- **特征定位**：`entry_reanchor_enabled`（约 L2751）；重锚分支 `_reanchored = True`
- **真实逻辑**：重锚单跳过入场前 SL 检查——`if triggered and not _reanchored:` 才做 SL 检查（约 L2766）。重锚后由下游 re-price 重算 entry/SL/TP。
- **核验结论**：代码真实。重锚改变了 DA 原计划入场位，若重锚到 RR 恶化位仍放行。
- **建议**：重锚触发后加一道"重锚后 RR ≥ 底线"校验，与退出侧 `MIN_TP_RR` 逻辑对齐。

### 4. `MIN_TP_RR = 1.5` 硬编码

- **特征定位**：`_MIN_TP_RR = 1.5`（约 L3408）
- **真实逻辑**：`_MIN_TP_RR = 1.5` 为模块级硬编码常量，用于 TP 的 RR 底线门禁（TP 相对初始 SL 的 RR<1.5 时忽略该 TP）。同函数内其他退出参数（`cooldown_sl_pct`、`factor_exit_rules`）均走 config。
- **核验结论**：代码真实。该值未 config 化，与其他参数不一致。
- **建议**：移到 config（SSOT），便于调参不碰代码。**低风险、符合 config 驱动原则。**

### 5. 开盘冷却参数（15min / 4%）需 MFE 数据回验

- **特征定位**：`cooldown_sl_pct`（约 L3372 `_check_playbook_exit_rules` 内；约 L3601 `monitor_playbook_sim_exits` 内）
- **真实逻辑**：开盘前 15 分钟（`minutes_from_open < 15`）SL 放宽到 `cooldown_sl_pct`（默认 4%，从 config 读）。LONG 取 `min(effective_sl, cooldown_sl)`、SHORT 取 `max(...)`，确为放宽方向。
- **核验结论**：代码真实，逻辑方向正确。但 15min/4% 是经验值。
- **建议**：用已落库的 MFE/MAE（exit_telemetry）回验——若多数被秒杀仓 MFE 仍=0，说明是**信号问题而非时机问题**，应收紧入场而非放宽 SL。

### 6. filled_price 覆盖退出价 —— 已生效，无需修改

- **特征定位**：`close_res.get("filled_price", 0) or current_price`（共 3 处，约 L3701 / L3896 / L3910）
- **真实逻辑**：`close_res = dual.close_position(...)` 真实平仓后，用 `close_res.get("filled_price", 0) or current_price` 作为最终退出价（broker 实际成交价覆盖本轮报价）。
- **核验结论**：✅ **已生效**。原"需实证确认"已闭环——PnL 用 broker 实际成交价，无系统性高估。**此条无需修改。**

---

## 供另一 Agent 独立验证的命令

```bash
cd /opt/ftseek/FTSeek-US
# 一次定位全部 6 条特征（行号以实际输出为准）
grep -nE '_vol_ok = True|vwap_missing|entry_reanchor_enabled|_MIN_TP_RR =|cooldown_sl_pct|close_res.get\("filled_price"' app/playbook/pipeline.py

# 逐条读上下文（用上面 grep 出的真实行号替换 START/END）
# sed -n 'START,ENDp' app/playbook/pipeline.py
```

**预期结论**：6 条代码依据全部真实存在；其中 #6 的 filled_price 覆盖已生效（3 处），无需改动；#1–#5 为可执行的改进建议，优先级见下。

## 优先级（供决策，非本次执行）

- **P1（低风险直接收益）**：#4 MIN_TP_RR→config
- **P2（影子先行，数据达标再启用）**：#1 VWAP 开盘门、#2 真 vol_ratio、#3 重锚 RR 校验、#5 冷却参数回验
- **已闭环**：#6 无需处理

---

# 追加存档（2026-07-17）：confidence 语义 + 入场门作用域失效 + 统一收口审计

> 本节独立成篇，记录 2026-07-17 的调查结论与已落地改动，供后续 agent / 自己回看。

## A. confidence 到底是什么（结论：非胜率概率，是分来源的标签）

- **DA/bridge 路径**（特征：`_is_bridge`，`pipeline.py:991-995`）：confidence = 辩论共识档。
  - 基线 0.50 → 两模型方向一致 `+0.20` = **70**；方向分歧 `-0.15` = **35**；无辩论 = **50**（`pipeline.py:387-404` BridgeVotingAdapter）。
- **非 bridge 路径**（`instruction_generator.py:440-472` `compute_confidence`）：confidence = 数据完整度分（6 维 ok 数×12 + 条件命中×28，封顶 90）。代码注释直言"90 是数据完整度不是确信度，conf=90 但胜率 31%"。
- **UE（universe_expansion）**：confidence 分布宽（实测 0.5/0.85/0.88/0.92），语义与 DA 的三档不同。
- **实测**：`ic_tracker` 显示 DA confidence 的 IC 为负（低信心桶反而更好）→ **当前 confidence 未校准、近似反指**，不能直接当门槛。
- **佐证（清理后干净数据分层，原生 playbook 92 笔）**：conf<0.5 正期望(+234/57%)、**conf 0.5–0.7 负期望(-634/17%)**、conf≥0.9 靠少数大赢撑(+2512/46%)。

## B. 入场 confidence 门（G6）作用域失效 —— 门装在没车走的路上

- **特征定位**：`if confidence < _min_conf_for_dir`（`pipeline.py:1059`，BUY 门槛 70 / SHORT 65，config 驱动）。
- **真实问题**：实盘走量的 **DA/UE 单由 skill 直写 DB**（`opportunity_scan_YYYYMMDD.json` → playbook_instructions），**不经过 `_generate_instructions`，因此完全绕过 G6 门**。注释佐证 `pipeline.py:1311-1313`："bridge 候选已由 deep_analysis skill 直写 DB，不需要 VotingCoordinator"。
- **数据佐证**：大量 conf=35 的 DA **BUY** 单成交（07-13~07-16 META/AMZN/MSFT/NVDA/LLY…），按 G6=70 本应 SKIP。
- **结论**：G6 只对内部管线有名义作用；而 NullVotingAdapter 下内部管线本就不生成单 → **门形同虚设**。判据（反指 confidence）与作用域（错路）双错。

## C. 已落地：下单前统一收口审计（纯记录、零风险）

- **收口点**：`run_playbook_auto_execute` 下单前唯一汇合点（`execute_instruction()` 前，`pipeline.py` 执行循环）。DA/UE/内部所有单必经此处 → 一处全覆盖，未来新来源自动纳入。
- **新增**：`_emit_exec_gate_audit()`（`pipeline.py`，`run_playbook_auto_execute` 上方）。每个到达漏斗的候选记一行 → `data/shadow/exec_gate_audit.jsonl`。
  - 字段：`{ts,date,inst_id,symbol,direction,confidence,source(da_bridge/universe_expansion/internal),decision_id,action,entry,size,notional,edge_bucket,edge_expectancy,edge_verdict,edge_reason}`。
  - action 覆盖：`executed / skipped_fail / error / skipped_bp_cap / skipped_notional_cap / skipped_max_positions`。
- **edge 判定首次覆盖 DA/UE**：收口点调用 `EdgeShadowGate.evaluate(..., log=False)`（`edge_shadow_gate.py` 加 `log` 开关，避免与旧路径双写）。纯影子，不拦单、不改动作，best-effort try/except。
- **展示/推送（复用现有链路，零新建）**：`scripts/shadow_board.py` 新增第 9 段 `_sec_exec_gate()`。每日 cron `15 6 * * 2-6`（CST 06:15 / ET 盘后）`shadow_board.py --push` → 飞书。
  - **核心指标**：已执行单里 EdgeShadowGate 判 BLOCK 的比例 = "edge 门若实装，会拦掉多少我们实际下的单"。

## D. 待决策（数据说话后，非本次执行）

- 攒数据：`exec_gate_audit.jsonl` 每笔自动落库；飞书每日展示"edge门若实装会拦 X%"。
- 待"拦截比例 + edge 与实盘动作分歧"稳定后，二选一（人工/灰度审批）：
  - **装数据驱动实门**：用风险归一期望（非 confidence）在收口点做真门；或
  - **确认无门更好**：移除 G6 名义门，避免"以为有保护其实没有"。

## 独立验证命令（2026-07-17 追加项）

```bash
cd /opt/ftseek/FTSeek-US
# 门作用域失效 + confidence 计算
grep -nE 'if confidence < _min_conf_for_dir|_is_bridge =|不需要VotingCoordinator' app/playbook/pipeline.py
# 收口审计 + edge log 开关
grep -nE '_emit_exec_gate_audit|log=False' app/playbook/pipeline.py
grep -n 'log: bool = True' app/decision/edge_shadow_gate.py
# 看板段 + cron
grep -n '_sec_exec_gate' scripts/shadow_board.py
crontab -l | grep shadow_board
```

---

# 追加存档（2026-07-17 之二）：US.html 决策卡方法论 vs DA/UE 指令方向 — 对比评判

> 参照物：根目录 `US.html`（外部保存的投研报告《US_深度分析_板块轮动战略_20260716》）。
> **核心判断：项目不缺零件，缺"组装+实装"。** US.html 的"决策卡"本质 = 项目已有的 confidence / invalidation / EdgeShadowGate / theme_context / exec_gate_audit 的**组装**。方向是组装而非新建（符合"复用已有、避免过度设计"）。

## A. US.html 如何给指令和方向（成熟投研方法论）

- **决策卡**：评级+CONF+加仓触发+回调区+双止损（波段线/长线线，杠杆越高越紧）+事件止损
- **情景概率**：Bull/Base/Bear + E[P]（GOOGL E[P]+6.2%；BABA 悲观71%占优 E[P]+20.5%）
- **裁判日**：可证伪日期+到点验证（7/22 GOOGL、8/28 BABA、8/26 NVDA 财报）
- **veto**：GLM EXTREME VETO → BABA 建仓上限 0%（不追现价）
- **自上而下**：板块轮动大叙事（AI硬件→AI变现软件+港股深价值）→ 个股
- **已有持仓管理**：持有/加仓触发/减仓/杠杆分层止损
- **实盘收盘回验**：7/16 收盘数据验证轮动已发生

## B. 项目 DA/UE 实际（已实证）

- 指令字段：symbol/direction/entry/SL/TP/confidence + invalidation/da_thesis（自然语言）
- confidence：DA 三档（35/50/70 辩论共识）、UE 宽分布；**IC 为负（反指/未校准）**
- 板块叙事：`theme_context`/`sector_rotation` 仅作 ctx 信息字段，**未约束方向**（`pipeline.py:1566-1591`）
- invalidation：落库（Stage1）+ `invalidation_shadow` 影子（Stage2a）；**自然语言、无裁判日、影子未实装**
- 收口审计：`exec_gate_audit` + `EdgeShadowGate` 影子（已落地，2026-07-17）

## C. 问题点（逻辑思路 → 落地）

### 思路层面
1. **方向是单点 entry/SL/TP，缺"情景概率+期望值"契约** → sizing 用风险归一期望替代反指的 confidence。
2. **缺"裁判日/催化剂"时间维度，退出是纯技术时间止损**（TIME_EXIT 2h / PRE_CLOSE 收盘前8min，与催化剂无关）→ invalidation 结构化为"裁判日+可观察触发"，延伸 Stage2a 影子。
3. **板块叙事有数据但未约束方向**（DA/UE 自下而上，可能逆主线）→ `theme_context`/`sector_rotation` 作顶层约束/加权（顺主线加分、逆主线降权/veto），零新采集。
4. **无 veto/一票否决**（辩论只有共识度±，强烈反对也放行）→ 对极端反对实装 veto（复用 EdgeShadowGate verdict，影子先行）。

### 落地层面
5. **缺"已有持仓怎么办"**（加仓触发/减仓/事件止损/杠杆分层止损）→ 补持仓管理。
6. **confidence 反指却仍是方向/sizing 核心输入** → 借 `exec_gate_audit` 攒的数据，把主输入从 confidence 切到期望值（已识别，顺势加速）。

## D. 优先级（克制，复用已有）

- **P1**：#6 期望值替代 confidence（借 exec_gate_audit 数据）；#3 板块叙事约束方向（复用 theme_context）
- **P2**：#2 invalidation 结构化+裁判日（延伸 Stage2a 影子）；#4 veto 实装（复用 EdgeShadowGate，影子先行）
- **P3**：#1 决策卡化；#5 持仓管理（价值大但工作量大，分阶段）

## E. 独立验证命令（本追加项）

```bash
cd /opt/ftseek/FTSeek-US
# 板块叙事仅作信息字段（未约束方向）
grep -nE 'theme_context|theme_narratives' app/playbook/pipeline.py
# invalidation 影子（自然语言、机器尽力解析、未实装）
grep -nE 'evaluate_invalidation_shadow|自然语言|尽力解析' app/playbook/invalidation_shadow.py
# 收口审计（已落地）
grep -nE '_emit_exec_gate_audit' app/playbook/pipeline.py
# US.html 决策卡证据（情景概率/裁判日/veto）
grep -nE 'E\[P\]|裁判日|EXTREME VETO|决策卡' US.html
```
