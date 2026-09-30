# RFC-120: chase_cap 方向语义倒挂修复 (SHORT 出生即违例死锁)

- **日期**: 2026-09-30
- **类型**: L1 功能修复 (执行层归一 + 源头归一 + prompt/机检 + 遥测回写)
- **状态**: 已实施 (用户令「执行修复 确保修复可有效合理交易」)
- **裁决链**: 9/30 盘后核查报告 (6 条未执行归因) → 用户裁执行 P0-1/P0-2/P1-3/P1-4;
  P1-3 的"N 轮终态作废"被证据**否决** (9/8 SNDK 被拦 ~24 轮后靠重评成交 +$116.02,
  终态作废会杀死合法救援) → 改为源头消灭结构性死锁, 保留重评机制;
  P2-5 (偏斜门批次口径) 未动 — 行为变更需影子预注册, 另行提案。

## 根因

权威语义 (`entry_routing.chase_cap_violated`): BUY cap=上限 (价>cap 拦),
SHORT cap=下限 (价<cap 拦)。但 DA prompt 对 SHORT 无操作性锚点 (llm_synth.py:1142
只举 LONG 例子) → LLM 把 invalidation 的**上破失效位**填进 SHORT 的 cap
(9/30 实证: BE「放量突破$302.35前高」→cap=302/entry=298; KMX 63/60.5; TSLA 358/355)
→ 下单价天然低于 cap → **出生即违例 = 结构性死锁** (放行条件=论点死亡条件)。

## 量化影响 (DB 实证, 9/4 enforce 上线后)

| 分组 | n | 执行 | 执行率 |
|---|---|---|---|
| SHORT 合规 (cap<entry) | 33 | 24 | 72.7% |
| SHORT 倒挂 (cap>=entry) | **18** | **0** | **0.0%** |
| SHORT 无 cap | 10 | 8 | 80.0% |

- 跨 9/9–9/30 共 15 个交易日; TSLA 单指令空转 37+ 轮; 9/30 三条空单全灭
  (撞用户核心原则「真正有空头产出」)。
- **BUY 侧同病**: cap<=entry 18 条, 0 执行 (9/8–9/30) — 修复双向对称。
- 对照组: 9/30 ARM SHORT cap=285<entry=291.28 (合规) → 正常执行 +$10.83。

## 修复 (源头过滤 > 层层校验; 归一 SSOT 单函数)

倒挂值**零信息增量** (上破失效位已由 stop_loss/invalidation 承担) → 确定性归一为
入场带顺势沿 (= 生成入场价 ×(1∓CHASE_ZONE_PCT), 与 UE 约定 LONG=entry_high/
SHORT=entry_low 同口径):

1. **SSOT**: `app/playbook/entry_band.py::normalize_chase_cap(direction, entry, cap)`
   → `(effective_cap, normalized)`。合规/缺失/畸形一律原样 (零行为变更)。
2. **源头 (DA skill v0.90)**: `scripts/pipeline.py::_write_bridge_file` chokepoint
   写库前归一 (bridge 候选 + 直写 DB 全覆盖); 归一函数从 FTSeek 同源导入,
   失败走同口径本地降级副本 (v0.88 momentum 同纪律)。锚=生成入场价
   `vr["entry_price"]` (未被 reprice 污染)。
3. **执行层兜底 (pipeline.py chase 门)**: 判定前归一 (立即救活当日已入库死指令,
   无需等 DA 重跑)。**锚纪律**: 必须用生成时价 — 优先级 ①`raw.planned_entry`
   ②band 沿反推 (`entry_low/(1-CHASE)`|`entry_high/(1+CHASE)`, reprice 永不覆盖列)
   ③`confirmation.price_level`; 全缺→不归一维持原状 (宁可继续拦也不静默放行)。
   **禁用 DB entry_price 列当锚** (reprice 逐轮覆盖 → cap 追价 = 静默废闸)。
   异常 fail-open 到原值 (归一失败维持现状拦截)。
4. **Prompt (P0-2)**: llm_synth.py 契约行 1081 补方向语义; 1142 补 SHORT 对称说明
   + 9/30 BE 实证反例。PROMPT_HASH 变 (v0.90 基线切分)。
5. **机检 (P0-1, record-only)**: `_validate_output_contract` 新增
   `chase_cap_inverted_short/_long` — 同 knife_geometry 哲学只报告不 repair
   (改 cap=改交易决策), 违约行=prompt 合规率攒样尺子 (修复后应趋零)。
6. **日志方向分流 (P1-3)**: SHORT 违例文案「涨回帽上即放行」(原「回落帽下」对
   空单方向说反)。
7. **skip_reason 回写 (P1-4)**: `_write_round_skip_reason()` 接入 VWAP 门 +
   证伪斜率门 (非终态值, 不入 INVALIDATED_SKIP_REASONS; 同前缀幂等; 失败只 warn;
   成交时 db 层 executed=1 清库不变式照常兜底) — 修 LITE 9/30 型归因黑洞。
8. **遥测**: 影子行/审计加 `cap_inverted` + `orig_cap_price` → replay/月报可切分
   "真追价违例"与"倒挂归一后仍违例"两人群。

## 9/30 三条死指令回放 (活体探针实测)

| 标的 | cap 归一 | 现价判定 | 语义 |
|---|---|---|---|
| TSLA | 358→337.25 (锚=conf 355) | 353.74 → **放行** | 死锁解除 |
| BE | 302→283.1 (锚=planned 298) | 279.41 → 仍拦 | 已追空 6.2%+ = 真违例, 回升即放行 (窗口非空) |
| KMX | 63→57.475 (锚=planned 60.5) | 56.64 → 仍拦 | 同上, 真追空语义正确 |

## 验证

- 新契约测试 `tests/test_chase_cap_inversion_fix.py` 32 passed (纯函数 12 +
  源级锚纪律 + 真实死指令回放 + 回写幂等/非终态)。
- 受影响套件: chase_cap_enforce/round_counter/da_output_contract 87 passed;
  playbook 全面 1050 passed (+1 窗口尺测试因归一块插入扩窗 6500→10500, 已更注)。
- 活体探针: DB 真实行喂归一链 ✓; skill 侧同源导入 ✓; 校验器 SHORT/BUY 双命中 +
  合规静默 + 不进 repairable ✓; 降级副本同口径 ✓。

## 回滚

- FTSeek 侧: `git revert` 本 commit (归一块独立, chase 门判定次序未动)。
- skill 侧: `git revert` v0.90 commit; 备份 `llm_synth.py.bak-rfc120-20260930` /
  `pipeline.py.bak-rfc120-20260930`。
- 紧急降级: 双侧归一块各自独立, 可单侧回滚 (执行层回滚后源头归一仍防新指令死锁)。

## 攒样判据 (预注册, 30 天窗口 10/1–10/31)

1. `chase_cap_inverted_*` 机检违约率: prompt 修复后应趋零 (基线: 9/4–9/30 空头
   18/61=29.5% 倒挂率); ≥15 日后若仍 >10% → prompt 教学无效, 提案改为写入侧强制重生。
2. 归一人群 (影子行 `cap_inverted=true`) 执行率与盈亏 vs 合规人群: 执行率应
   从 0% 恢复至合规组水平 (~70%+); 若归一人群盈亏显著差于合规组 → 归一沿口径
   (×0.95) 需重裁。
3. born-violated 新发率 (DB 直查): 源头归一生效后应为 0 (写入侧拦截)。
