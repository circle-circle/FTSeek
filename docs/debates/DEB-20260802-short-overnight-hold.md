# DEB-20260802 · SHORT 隔夜持仓试点：被 EOD 强平扔掉的 edge 能否收回？

> **状态**: 🔵 进行中 — 独立复核已落 (§五.五: 隔夜段口径 bug, B 方案降级) + 待辩论 + 待用户裁决
> **起案方**: Qoder，2026-08-02
> **关联**: handoff 08-01 终裁（quota 999+半码 0.5)· DEB-20260801（方向门校准）· 宪章 §四.2 一次一变量 · 8 月验证期（只修尺子/攒样本/做减法）
> **裁决点**: A（赢家隔夜）/ B（全员隔夜）/ C（纯影子）/ D（不动）+ 配套风险护栏，见 §六

---

## §0 一句话

SHORT 是全库唯一实证正 edge（已执行，EOD +0.50~0.81% → T+1 +0.97~1.46%），但 `overnight_hold_enabled: false` 把所有仓位当日强平，**隔夜段收益（约 +0.3~0.4pp/笔）被系统性扔掉**；然而进一步分解发现：**隔夜 edge 集中在"EOD 时亏损"的仓位（均值回归），而现有代码语义只让"EOD 时盈利"的仓位过夜——赢家桶的隔夜期望 ≈ 0 且窗口间变号**。要不要隔夜、按什么语义隔夜，需要辩论后由用户裁决。

---

## §一 事实链（2026-08-02 全部可复现，SQL 见文末）

### 1.1 SHORT 分 horizon edge（已执行，fwd 口径=指令价基准，费前毛利）

| 窗口 | n | EOD（日内） | T+1 开盘 | T+1 收盘 | T+3 收盘 |
|---|---|---|---|---|---|
| 全史（playbook 上线起） | 49 | +0.81% | +1.19% | **+1.46%** | +0.96% |
| since 07-01 | 35 | +0.50% | +0.98% | **+0.97%** | +0.89% |

- BUY 对照（全史 n=164）: 日内 −0.19% / T+1 −0.48% / T+3 −0.15% — **负 edge 不过夜，本 DEB 只讨论 SHORT**
- T+1→T+3 不再增长（1.46→0.96 / 0.97→0.89）→ **edge 在 T+1 日兑现完毕，持仓上限 2 日即可，无需更长**

### 1.2 隔夜段（T+1 开盘 − 当日收盘）分解 — 关键发现

| 口径 | 窗口 | n | 隔夜段期望 | 负 gap 占比 | 最差 |
|---|---|---|---|---|---|
| 全体 SHORT | 全史 | 44 | **+0.31pp** | — | — |
| 全体 SHORT | 07-01 起 | 30 | **+0.42pp** | 40% | −4.89pp |
| **EOD 盈利**（现有 skip 语义会过夜的桶） | 全史 | 26 | **−0.05pp** | 46% | −7.06pp |
| **EOD 盈利** | 07-01 起 | 16 | +0.29pp | 38% | −4.89pp |
| **EOD 亏损**（现有语义当日平的桶） | 全史 | 18 | **≈+0.83pp**（倒算） | — | — |
| **EOD 亏损** | 07-01 起 | 14 | **≈+0.56pp**（倒算） | — | — |

- 隔夜段中位数 ≈ 均值（+0.39 vs +0.42），**非离群值驱动**
- **结论**: edge 的隔夜增量主要来自"当日被套的 SHORT 隔夜均值回归"；赢家桶隔夜期望全史 ≈ 0、07-01 起 +0.29pp，**窗口间不稳定且 n<30（律 5 不可下结论）**
- 07-06 起 SHORT 频率 1.93 笔/日；EOD 时盈利占比 52%(27 笔中 14 笔）

### 1.3 现有机制（代码事实，非配置直觉）

| 组件 | 位置 | 行为 |
|---|---|---|
| 隔夜总开关 | `unified_config.yaml:1103` `overnight_hold_enabled: false` | false → `_skip_allowed=False` → **全部当日强平**(pipeline.py:5103 盘中 + :6185 EOD 兜底，两处收口） |
| skip 语义 | pipeline.py:5098 / :6183 | 开关 ON 时 `_skip_allowed = (cohort=="B" or horizon=="long")` — **short horizon 即使开总开关也不过夜** |
| EOD 盈利才跳过 | pipeline.py:6195-6210 | skip_allowed 且 EOD 估值盈利 → 持过夜；**亏损/无报价 → 强制平仓（保守）** |
| MAX_HOLD | pipeline.py:5397 + _horizon_max_hold_hit | 盘中检查，max_hold_days 从 factor_exit_rules→horizon 配置读（short=1, long=5)，仅 skip_allowed 时生效 |
| MAX_HOLD_T5 | pipeline.py:6188 + :100 | cohort B 满 5 交易日硬强平（EOD 兜底） |
| 隔夜跳空止损 | pipeline.py:5106 | `overnight_gap_stop_pct` 默认 3.0（horizon 级），已在盘中监控消费 |
| exit_shadow | app/playbook/exit_shadow.py | 反事实影子设施已在（DEB-20260725/29 用过），可加"hold 到 T+1 开盘/收盘"变体 |

### 1.4 费用与 PDT

- SHORT 已平 38 笔（07-01 起）: avg 费 **$4.88**, avg 名义 **$4,610** → 往返税 **0.106%**（固定最低佣金主导，费与仓位大小无关）
- 持有 2 天 vs 当日：费用不变（同一往返），毛盈亏变大 → **费用占比自然摊薄**
- 隔夜仓**不构成日内往返** → 不占 PDT 日内交易额度（8 月 PDT 已触线 3 次，真实收益）

### 1.5 起案方两处勘误（诚实声明）

1. 我在向用户的提案中说"`skip_earnings_5d` 财报过滤已存在"——**核查后不存在**：playbook 无在产硬财报门（仅 LLM prompt 上下文与 horizon_selector 读取字段）。隔夜事件风险护栏**需新建**或作为残余风险接受。
2. 提案中"EOD 强平每笔扔 +0.65pp"是**无条件口径**(T+1 收盘 − 日内）；在现有"赢家才过夜"语义下可收割量 ≈ 0（全史）~ +0.29pp（07-01 起），见 §1.2。**真实可收割量取决于隔夜语义选择，这是本 DEB 的核心分歧。**

---

## §二 论证

### 2.1 尺度错配是真实的（做加法的理由）

日级 edge 用小时级工具收割：SHORT edge 在 T+1 才完全兑现（+0.50→+0.97%），EOD 强平把隔夜段+T+1 日段扔掉；同时每笔固定交 0.106% 往返税。隔夜持仓一石二鸟：收回 edge 增量 + 摊薄费用占比 + 豁免 PDT。

### 2.2 但"收回"的语义陷阱（做减法的理由）

- 现有代码语义 = **赢家过夜、输家当日平**。数据：赢家桶隔夜期望 ≈ 0（全史）且窗口间变号 → **开总开关可能什么都收不到，只收到隔夜跳空风险**（赢家桶最差 −7.06pp）
- 要收到 +0.3~0.4pp 无条件 edge，须**全员过夜（含输家）** = 对当日被套仓位赌均值回归。这在样本内成立（n=18/14,+0.56~0.83pp），但:
  - n 远 <30，两个窗口的输家桶合计仅 32 笔
  - 语义上是"扛单等回归"——与风控纪律直接冲突，一次财报/squeeze 跳空可抹掉全部累计（−4.89pp 单尾 = 约 12 笔的 +0.4pp)
  - SHORT 隔夜跳空单边无保护（涨无顶），现有 gap_stop 3.0% 是否覆盖财报级跳空存疑

### 2.3 n≥30 纪律与试点设计冲突

- SHORT 频率 1.93/日 → 2 周试点 ~19 笔，赢家桶 ~10 笔 → **任何分层都 <30**
- 8 月全月 ~37 笔，赢家桶 ~19 笔 — 依然 <30
- 诚实选项：① 接受"探索性试点"地位（读数只作方向参考，08-29 前不下转正结论）;② 纯影子路线（方案 C)，用 exit_shadow 攒**全量** SHORT 的反事实（不过滤赢家/输家），2 周 ~19 个全路径影子 + 历史回放；③ 放弃（方案 D)

### 2.4 模拟盘保真度边界

- SIM 隔夜：无借券利息建模、无真实跳空滑点、成交价=报价 → **隔夜 edge 在 SIM 会被系统性高估**（真实世界 SHORT 过夜有 borrow fee + 开盘滑点）
- BROKER_GHOST_CLOSE 已有 4 笔 −$168（入场侧 CANCELLED/TIMEOUT 假仓位）→ 隔夜增加跨日对账表面，需先确认 position reconciliation 对隔夜仓的覆盖
- 这些不失效试点，但判分口径必须标注"SIM 上界"，转正到 LIVE 前需重估

---

## §三 可选方案

### 方案 A — 赢家隔夜试点（最小改动，沿用现有语义）

- 改动：`overnight_hold_enabled: true` + short_horizon 新增 `allow_overnight: true` 接入两处 `_skip_allowed`（约 10 行）+ `short_horizon.max_hold_days: 1→2`（盘中 MAX_HOLD 生效）
- 行为：EOD 盈利 SHORT 持过夜，T+1 日内由 SL/trailing/gap_stop 管理，T+1 EOD 或 T+2 强平；输家仍当日平
- 预期：按 §1.2 赢家桶数据，edge 增量 ≈ 0（全史）~ +0.29pp（07-01 起）— **主要价值是验证机制 + 攒真实路径样本，不是收益**
- 风险：赢家桶隔夜最差 −7.06pp;gap_stop 3.0% 托底
- 回滚：`allow_overnight: false`（或关总开关）

### 方案 B — 全员隔夜试点（收割无条件 edge，语义激进）

- 改动：A 的全部 + EOD skip 条件从"盈利"改为"全部 SHORT"（亏损仓也不过夜强平）
- 预期：样本内 +0.3~0.4pp/笔（n=44/30 无条件）
- 风险：**扛单等回归语义**；输家桶 n 小；一次财报跳空可吞掉全部累计；与"负 edge 当日止损"纪律冲突
- 护栏（若选 B 必配）:gap_stop 3.0% 生效确认 + 隔夜仓事件过滤（需新建，§1.5.1)+ max_hold=2 硬顶 + 隔夜仓半码（叠加现有 BUY 半码思路，SHORT 隔夜仓 ×0.5)
- 回滚：同 A

### 方案 C — 纯影子（零风险，测量先行，符合 8 月"攒样本"定位）

- 改动：exit_shadow 增加两个变体 `HOLD_T1_OPEN` / `HOLD_T1_CLOSE`，对**全部** SHORT 已平仓记录反事实；不动任何交易行为
- 历史侧：用 §1.1/1.2 的 fwd 口径回放近 6 周（已有数据，零成本）
- 预期：2 周攒 ~19 个全路径影子（含盘中路径，比 fwd 收盘价口径更真）→ 08-15 前后给 A/B 提供真实判据
- 缺点：影子口径 ≠ 真实成交（无滑点/无对账），且 8 月窗口内 SHORT 影子 n 仍 <30 — **本质是把决定推迟到 9 月**
- 回滚：无需回滚（record-only）

### 方案 D — 不动（接受 EOD 收割，9 月再说）

- 理由：8 月定位"验证不是盈利"；SHORT 当日 edge 已为正（EOD +0.50~0.81%)，隔夜是锦上添花；n<30 下任何试点都出不了有纪律的结论
- 代价：每月扔掉样本内 ~+0.4pp × ~37 笔 ≈ +15pp 累计毛 edge(SIM 口径上界）

---

## §四 关键分歧（供辩论）

1. **隔夜语义**: 赢家过夜（A，期望≈0 但风控兼容）vs 全员过夜（B，期望+0.4pp 但扛单语义）——edge 在输家桶是数据事实，收不收？
   - 可验证：方案 C 影子分别按两个语义打分，2 周后对比
2. **窗口之争**: 赢家桶全史 −0.05pp vs 07-01 起 +0.29pp — 哪个代表 8 月？（ regime 依赖性，样本内不可判）
3. **n<30 处理**: 接受探索性试点（读数不转正）vs 推迟到有纪律样本（C/D)——宪章律 5 与"8 月验证窗口"的张力
4. **扛单语义兼容性**: B 的"输家隔夜"是否与同日生效的"SHORT SL 1.2-1.5×ATR"纪律冲突？（盘中 SL 触发当日已出，隔夜桶只剩"没到 SL 的输家"——是否本质是"SL 够紧时扛单无害"?)
5. **SIM 上界折价**: 隔夜 edge 在 SIM 高估多少？（borrow fee ~0.3-8%/年按日计 + 开盘滑点）→ 判分阈值是否应上浮（如 SIM 读数 > +0.6pp 才认定真实 +0.4pp)?

---

## §五 辩论任务

1. 【辩论方】独立复现 §1.2 全部六个数字（无条件/赢家/输家 × 两窗口），重点验证"赢家桶期望≈0"与"输家桶倒算值"
2. 【辩论方】答辩 §四.4: 盘中 SL 已截断大输家后，"隔夜输家桶"是否仍含尾部风险（给出 SL 未触发但隔夜 gap<−3% 的历史联合分布）
3. 【辩论方】核查 §1.5.2: EOD 强平丢 edge 的"可收割量"在三种语义下的正确估计（我给的：A≈0~0.29 / B≈0.31~0.42 / 影子待测）
4. 【独立复核方】只读复现 §1.1/1.3/1.4，含机制代码事实（_skip_allowed 两处、MAX_HOLD 消费点、gap_stop 3.0)
5. 【辩论方】评估 gap_stop 3.0% 对财报级跳空的充分性（历史 SHORT 标的财报次日开盘 gap 分布）;§1.5.1 事件护栏的实现成本
6. 争议点：B 方案是否本质上是"用 SHORT 的方向 edge 做伪装，行均值回归策略之实"——若是，它该作为独立策略另行预注册，而非搭 SHORT 试点便车？

---

## §五.五 独立复核（第三模型, 2026-08-02, 用户令"核查新 DEB 提案"）— 隔夜段口径 bug 实锤, 核心叙事不成立

### 5.5.1 复现情况

| 项 | 作者 | 复核 | verdict |
|---|---|---|---|
| §1.1 EOD/T+1/T+3 (entry 基准) | 全史 49 笔 +0.81/+1.46/+0.96; 07-01 35 笔 +0.50/+0.97/+0.89 | 47 笔 +0.81/+1.459/+0.955; 33 笔 +0.50/+0.973/+0.89 | ✅ 一致 (n 差 2 系未滤 entry_price>0，无实质影响) |
| §1.3 机制锚点 | overnight_hold_enabled:false (yaml:1103) / _skip_allowed 双处 / gap_stop 3.0 (horizon yaml:428 + pipeline:5106) / MAX_HOLD (5397) / MAX_HOLD_T5 (6189) | 全部逐行命中 | ✅ 属实 (仅注: pipeline:5104 处 _skip_allowed 覆盖为显式 false 分支, 与作者描述一致) |
| §1.4 费用 | 38 笔 avg 费 $4.88 / avg 名义 $4,610 | 38/$4.88/$4,610 精确一致 | ✅ |
| §1.1 "T+1 开盘"列 | 标为 fwd_return_t1_open = "T+1 开盘收益" | **列语义 = open_d0→close_t1 (open 基准孪生列, ic_tracker: `sign*(close_t1-open_d0)/open_d0`), 不是 entry→open_t1** | ❌ 标注错误, 本列含 T+1 全天 |
| **§1.2 隔夜段** | 代理公式 gap_pp = fwd_return_t1_open − fwd_return_intraday | **量纲混合** (前者基准 open_d0/后者基准 entry); 实测的近似是"close_d0→close_t1 跨日全段"而非"EOD→T+1 开盘" | ❌ 口径 bug, 数字全部失真 |

### 5.5.2 真隔夜段重算（日 K 直算 close_d0 → open_t1 × sign, replay_bars_daily 覆盖 44/47）

| 口径 | 窗口 | n | 作者值 | **真隔夜值** | 最差 |
|---|---|---|---|---|---|
| 全体 SHORT | 全史 | 44 | +0.31pp | **+0.198pp** | −3.65pp |
| 全体 SHORT | 07-01 起 | 30 | +0.42pp | **≈+0.20pp** | −3.65pp |
| EOD 盈利桶 | 全史 | 26 | −0.05pp | **+0.202pp** | −3.65pp |
| EOD 盈利桶 | 07-01 起 | 16 | +0.29pp | **+0.035pp** | — |
| EOD 亏损桶 | 全史 | 18 | +0.83pp (倒算) | **+0.192pp** | −2.27pp |
| EOD 亏损桶 | 07-01 起 | 14 | +0.56pp (倒算) | **+0.378pp** | −2.27pp |

### 5.5.3 结论（影响核心叙事）

1. **"隔夜 edge 集中在 EOD 亏损仓位（均值回归）"不成立**。全史两桶几乎无差（+0.202 vs +0.192）；07-01 窗口输家桶 +0.378 但 n=14 <30（律5）。作者 +0.83/+0.56pp 系代理公式伪影（把 T+1 全天的收益摊进"隔夜桶"），真隔夜只有 ~+0.2pp。
2. **"赢家桶隔夜期望 ≈ 0（全史 −0.05pp）"是代理公式伪影**：真值全史 +0.202pp。作者对方案 A"收不到东西"的判断在全史口径不成立。
3. **方案 B（全员隔夜）失去数据依据**：B 相对 A 的增量 = 输家桶差 ≈ 全史 0 / 07-01 +0.34pp (n=14 不稳) — 远低于作者声称的 +0.83pp；而 B 承担"弃当日止损纪律 + 财报/squeeze 跳空"的尾部风险。**B 的期望收益 ≈ A + 无统计意义的差**。
4. **隔夜正增量本身仍成立**（~+0.2pp/笔, SIM 口径, 两窗口一致）— 但幅度是作者声称的一半左右，且作者"被扔掉 +0.3~0.4pp"应修正为 **~+0.2pp**。
5. 修正后的方案矩阵: A 赢家过夜预期 +0.04~0.2pp/笔（近期~0）；B 相对 A 无显著增量 → **B 应降级**；C（影子）任一语义打分仍是最干净测量路径；D 不变。作者倾向"C 先行 + A 并行"在修正口径下依然成立（A 的收益预期下调但不影响机制验证价值）。
6. **另两点确认**（供辩论引用）: ①§1.5.1 财报门不存在属实（playbook 无硬财报门）；②SIM 上界折价论证（borrow fee + 滑点 + 无建模）成立，判分须标注。

### 5.5.4 对 §四 关键分歧的修正
- §四.1 "edge 在输家桶是数据事实" → **不成立**（真隔夜两桶无差）
- §四.2 "赢家桶 −0.05 vs +0.29" → 真值全史 +0.202 / 07-01 +0.035，"窗口之争"降级为"近期 ≈0"单窗口观察
- §四.4 "B 是否行均值回归之实" → 该问法仍有意义，但 B 的收益依据已不复存在

**复核方立场的裁决倾向**（供用户参考）: **C 先行（影子双语义打分）+ A 并行（机制验证, 收益预期 ~0）**、**B 搁置至影子读数** — 与作者倾向一致, 但理由从"B 能收输家 edge"修正为"B 无额外收益且尾部风险不对称"。D 为兜底。另建议: 若选 A, short_horizon.max_hold_days 1→2 与 gap_stop 3.0% 覆盖确认即可, 无需新建财报门（A 只过夜赢家, 事件风险远小于 B）。

---

## §六 待用户裁决

1. **主裁决**: A / B / C / D?（起案方倾向：**C 先行 + A 并行**——影子零风险攒全量反事实，同时用赢家语义验证机制与攒真实路径；B 待影子读数后另行裁决；D 为兜底）
2. 若选 A 或 B:`short_horizon.max_hold_days 1→2` 批准否？(T+3 数据证明 2 日足够）
3. 若选 B：隔夜仓半码（×0.5)+ 事件护栏新建，批准否？
4. 判分口径：是否接受"SIM 上界"标注 + §四.5 的上浮阈值？
5. 探索性地位确认：试点桶 n<30，读数**只作方向参考、08-29 前不作转正依据**——批准否？（律 5 的显式豁免申请，仅限本试点）

---

## 可复现命令

```bash
# §1.1 + §1.2 一把全出
./venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect("data/ftseek_us.db")
for w,label in [("2026-06-01","全史"),("2026-07-01","07-01起")]:
    r = c.execute("""
    SELECT COUNT(*) n,
     ROUND(AVG(fwd_return_intraday)*100,3) eod,
     ROUND(AVG(fwd_return_t1_open)*100,3) t1o,
     ROUND(AVG(fwd_return_t1)*100,3) t1,
     ROUND(AVG(fwd_return_t3)*100,3) t3
    FROM playbook_instructions
    WHERE direction='SELL_SHORT' AND COALESCE(executed,0)=1 AND date>=?""",(w,)).fetchone()
    print(label, "horizon:", r)
    r = c.execute("""
    WITH d AS (
      SELECT (fwd_return_t1_open - fwd_return_intraday)*100 gap_pp,
             fwd_return_intraday*100 intra
      FROM playbook_instructions
      WHERE direction='SELL_SHORT' AND COALESCE(executed,0)=1 AND date>=?
        AND fwd_return_t1_open IS NOT NULL AND fwd_return_intraday IS NOT NULL)
    SELECT COUNT(*) n_all, ROUND(AVG(gap_pp),3) gap_all,
      SUM(CASE WHEN intra>0 THEN 1 ELSE 0 END) n_prof,
      ROUND(AVG(CASE WHEN intra>0 THEN gap_pp END),3) gap_prof,
      ROUND(MIN(CASE WHEN intra>0 THEN gap_pp END),2) prof_worst,
      SUM(CASE WHEN intra<=0 THEN 1 ELSE 0 END) n_lose,
      ROUND(AVG(CASE WHEN intra<=0 THEN gap_pp END),3) gap_lose
    FROM d""",(w,)).fetchone()
    print(label, "gap分解:", r)
EOF

# §1.4 费用
./venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect("data/ftseek_us.db")
r = c.execute("""
SELECT COUNT(*), ROUND(AVG(fees_est),2), ROUND(AVG(entry_price*quantity),0)
FROM trade_records WHERE source_type LIKE 'PLAYBOOK%' AND direction='SHORT'
AND entry_time>='2026-07-01'""").fetchone()
print("SHORT n/avg费/avg名义:", r)
EOF

# §1.3 机制（只读）
grep -n "overnight_hold_enabled" config/unified_config.yaml
grep -n "_skip_allowed" app/playbook/pipeline.py | head -5
grep -n "overnight_gap_stop_pct\|_horizon_max_hold_hit\|_cohort_b_max_hold_hit" app/playbook/pipeline.py
```

---

### 变更记录
- 2026-08-02 v1: 起案（Qoder，用户令"先针对方案写成 DEBATE 文档，需要另外模型进一步分析辩论再定方案")。含起案方两处勘误（§1.5)。
- 2026-08-02 v2: 独立复核（第三模型，用户令"核查新 DEB 提案"）— 隔夜段口径 bug 实锤（§五.五）: 代理公式 fwd_return_t1_open − fwd_return_intraday 量纲混合（open_d0 vs entry 基准），"输家桶 +0.83pp / 赢家桶≈0"系伪影；真日 K 重算 (~+0.2pp 全体, 两桶无差) → B 方案失去数据依据, 倾向 C+A 不变, 理由修正。
