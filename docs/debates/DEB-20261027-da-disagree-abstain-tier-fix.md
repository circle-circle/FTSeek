# 议题：DA 挑战者「明确反对」被当成「弃权」抬升到最高档 —— 档位阶梯缺陷修复（2026-10-27）

> 关联：`DEB-20261027-da-scope-and-confidence-coupling.md`（§11 全链路复核，本次为其 §11.5 A/B/C/D 任务书**之外**的新发现）；
> `DEB-20261008-model-long-bias-decomposition.md`、`DEB-20260814`（gate 影子机制）。
> 代码路径约定：本 DEB 裸名 `signal_gate.py / llm_synth.py / adapter.py / pipeline.py` 均指
> `/root/.hermes/skills/deep-stock-analysis/scripts/`；`app/`、`scripts/`、`ops/` 前缀为本仓 `/opt/ftseek/FTSeek-US`。

## 状态

- **裁决**：已实施修复（live，带回滚开关），2026-10-27。
- **实施范围**：
  1. `signal_gate.py` 弃权分支 + `llm_synth.py` 弃权文案（**档位阶梯缺陷**，§1–§7）；
  2. `llm_synth.py` 挑战者 Prompt 两处文案（**C-A 变体**，§8，经用户终审覆盖 §14/§15 裁决后落地）。
- **零 Prompt 改动、零门槛改动、零名单/并发/仓位/退出改动**（除 §8 明列的两处 Prompt 文案）。
- **用户指令**：2026-10-27「按照建议执行优化修复」+「如果明确存在逻辑问题，请针对 prompt 直接做修复」。
- **性质**：① 是**逻辑缺陷修复**（非策略调参）；② 是**Prompt 判据口径修复**（条数门槛 → 严重性口径，方向/模型/conf 映射/门全部不变）。两者均为交易行为变更，故按军规落档 + 带回滚 + 台账留痕。

## 0. 一句话结论

挑战者返回 `verdict=DISAGREE`（明确反对）却把方向填成 `SKIP` 时，`signal_gate` 的弃权分支**只看方向不看 verdict**，一律按 `score/10` 把 `decision_conf` 抬到 0.6–0.8 ⇒ tier 良/优 ⇒ **PASS**：即「明确唱反调」反而拿到全链最高档。修复后该形态不再抬升（conf 0.50 ⇒ tier 中/WARN），**只降级、不删除**。

## 1. 病灶（新发现，不在 §11.5 A/B/C/D 任务书内）

§11.5 的 C 批针对的是**Prompt 文案**（挑战者「一条／两条反证」冲突）。本条是**下游档位阶梯对既有 verdict 的解释错误**，两者是不同变量，本条不占用 C 批名额。

实测样本（2026-10-27 当批，ET 08:39 收口）：

| 字段 | 读数 |
|---|---|
| symbol | `US.META` |
| analyst direction / score | SELL_SHORT / 8.0 |
| `challenger_verdict` | **DISAGREE**（明确反对） |
| `challenger_direction` | **SKIP**（未给反方向 ⇒ 输出自相矛盾） |
| gate `decision_conf` | **0.80** |
| gate tier / decision | **优 / PASS** |
| adapter `confidence`（落库） | 0.50 |
| `debate_outcome` 文案 | `➖ 挑战者弃权(SKIP) [合法弃权, 不罚], 保持SELL_SHORT, confidence不罚` |

即：台账把「唱反调」记成「合法弃权」，档位给到最高，下游按 PASS/normal 执行。

## 2. 证据表（作者实跑，命令可整段复制）

```bash
cd /opt/ftseek/FTSeek-US
# 台账全量复算：verdict=DISAGREE 且 challenger_direction=SKIP 且分析师方向有效
python3 - <<'EOF'
import json
from collections import Counter
rows=[json.loads(l) for l in open('data/da_gate_decisions.jsonl',encoding='utf-8') if l.strip()]
def tier(score,conf,d):
    if d not in ("BUY","SELL_SHORT"): return "SKIP"
    if score>=7 and conf>=0.7: return "优"
    if score>=6 and conf>=0.6: return "良"
    if score>=5 and conf>=0.5: return "中"
    return "差"
DEC={"优":"PASS","良":"PASS","中":"WARN","差":"BLOCK","SKIP":"BLOCK"}
n=0; mig=Counter()
for r in rows:
    vd=str(r.get('challenger_verdict') or '').upper(); cd=str(r.get('challenger_direction') or '').upper()
    dr=str(r.get('direction') or '').upper()
    if vd!="DISAGREE" or cd!="SKIP" or dr not in ("BUY","SELL_SHORT"): continue
    s=float(r.get('da_score') or 5.0); n+=1
    mig[(DEC[tier(s,max(0.50,s/10.0),dr)], DEC[tier(s,0.50,dr)])]+=1
print("总行数",len(rows),"受影响",n,"迁移",dict(mig))
EOF
```

**读数**：全账 2308 行，受影响 **10 行（0.43%）**，分布 2026-09-14 / 09-21 / 09-29 / 09-30 / 10-05 / 10-06 / 10-07 / 10-15 / 10-26 / 10-27，**每日至多 1 行**。判定迁移：**10/10 为 PASS→WARN**。

逐行（旧→新）：

| date | symbol | dir | score | 旧 conf/档/判 | 新 conf/档/判 |
|---|---|---|---|---|---|
| 2026-09-14 | US.NOK | BUY | 6.0 | 0.60 良 PASS | 0.50 中 WARN |
| 2026-09-21 | US.TK | SELL_SHORT | 6.0 | 0.60 良 PASS | 0.50 中 WARN |
| 2026-09-29 | US.MDB | SELL_SHORT | 6.0 | 0.60 良 PASS | 0.50 中 WARN |
| 2026-09-30 | US.KMX | SELL_SHORT | 6.0 | 0.60 良 PASS | 0.50 中 WARN |
| 2026-10-05 | US.XP | SELL_SHORT | 6.0 | 0.60 良 PASS | 0.50 中 WARN |
| 2026-10-06 | US.NU | SELL_SHORT | 6.0 | 0.60 良 PASS | 0.50 中 WARN |
| 2026-10-07 | US.BKV | BUY | 8.0 | 0.80 优 PASS | 0.50 中 WARN |
| 2026-10-15 | US.ORCL | BUY | 6.0 | 0.60 良 PASS | 0.50 中 WARN |
| 2026-10-26 | US.VZ | SELL_SHORT | 6.0 | 0.60 良 PASS | 0.50 中 WARN |
| 2026-10-27 | US.META | SELL_SHORT | 8.0 | 0.80 优 PASS | 0.50 中 WARN |

**零行降级到 BLOCK** ⇒ 修复只降级不删除，供给不灭。

## 3. 根因定位（含对作者自身判断的更正）

**更正**：本会话最初把病灶定位在 `debate_state.classify_debate` 的「ABSTAIN 先于 DISAGREE 命中」的优先级倒置。**该定位错误**，实测排除：

- `debate_state.py` 的该优先级是 **2026-10-23 明确设计并被契约测试锁死**的（`tests/test_debate_state.py::test_abstain_priority_over_raw_disagree`），不是缺陷；
- 且 `classify_debate` 的产出只被 `llm_synth.py:2843-2846` 用于计算 `agreed = (state == "AGREE")`，**下游 gate/adapter 根本不读 `state`**，改 `debate_state` 不会改变任何数值行为。

**真实根因**：`signal_gate.py` 的弃权分支只看 `challenger_direction`，不看 `challenger_verdict`：

```python
elif "SKIP" in (_cd, _od):
    #   弃权≠反对, 主模型方向+score 是唯一可用信号 → score≥7 恢复"优/PASS".
    _decision_conf = max(_decision_conf, score / 10.0)
```

该分支原注释「弃权≠反对」本意就是要区分「真弃权」与「明确反对」，但实现漏判 verdict。同一漏判在 `llm_synth.py` 的弃权文案分支复现（把 DISAGREE 标成「合法弃权」），在 `adapter.py` 同样存在但方向相反（adapter 弃权分支是 `pass` 保持 0.50 ⇒ 不抬升，故 adapter 侧**本就无此病灶**，只是因此与 gate 长期存在 conf 口径差）。

## 4. 修法与改动清单（已实施）

### 4.1 `signal_gate.py`（行为修复，带回滚开关）

弃权分支新增 verdict 判别：仅当 `challenger_verdict == "DISAGREE"` **且**挑战者非自辩（T4 `challenger_self_debate`）时，**不抬升**（保持基准 0.50）；其余形态（真弃权、AGREE、真反向）行为逐条不变。

- 回滚：`DA_GATE_DISAGREE_NO_LIFT=0`（或删该段），**一行级、即时生效、无需重启**（skill 每批新起进程）。
- 留痕：命中时向 `reasons` 追加一行，含**旧口径反事实 conf**，使台账自包含可复算：
  `弃权抬升抑制: 挑战者明确DISAGREE但未给反方向(输出自相矛盾) → 不按score抬升conf (2026-10-27 B2; 旧口径conf=0.80)`

### 4.2 `llm_synth.py`（文案去伪，不改数值）

弃权文案分支对 `verdict == DISAGREE` 单独出文案：`➖ 挑战者弃权(SKIP) [明确反对但未给反方向, 输出自相矛盾], 保持{dir}, 不按弃权抬升confidence`。

**两条硬约束**（违反其一即破坏下游）：

1. **必须保留「弃权」二字**：`app/playbook/admission.py:240`（`"弃权" in debate_outcome` → 弱信号分层标记）与 `app/review/daily_review.py:914`（`'弃权' in ...`）按子串分层，去掉会断历史可比性。
2. **不得含 `AGREE` 子串**（含 `DISAGREE` 亦不可）：`signal_gate.py:395`（另 `:512` 的 p0 影子段同款）与 `adapter.py:218` 有 `"AGREE" in debate_outcome` 判定。当前所有 `debate_outcome` 文案均不含该子串 ⇒ 该判定**恒不命中＝死代码**；一旦写入 `DISAGREE`，同向分支会白捡 `+0.20`。机器可读 verdict 已在 `_debate.challenger_verdict` 字段，不靠文案承载。

### 4.3 未改动（明确声明）

`debate_state.py`（契约测试锁死，改动无收益且破坏已裁断言）、`adapter.py`（本无病灶）、Prompt、门槛、名单、并发、仓位、退出、`quality_score`、AGREE 率目标。

## 5. 验证

| 项 | 命令 | 结果 |
|---|---|---|
| 新增契约测试 | `PYTHONPATH=scripts venv/bin/python -m pytest tests/test_disagree_no_lift_gate.py -q`（skill 仓） | **9 passed** |
| 既有 state 契约 | 同上 + `tests/test_debate_state.py` | **17 passed** |
| skill 全量 | `pytest tests/ -q` | 131 passed / 1 skipped / **1 failed（既有红点，非本批引入，见 §7）** |
| 本仓 DA 相关 | `pytest tests/test_da_output_contract.py test_da_trading_day_guard.py test_da_fundamental_contract.py test_da_chain_contract_audit.py test_da_scope_confidence_audit.py test_da_main_carrier.py test_ops_scripts_sync.py -q` | **181 passed** |
| 编译 | `py_compile signal_gate.py llm_synth.py` | OK |
| **Prompt 补丁干净应用** | `git apply --check docs/offline/ca-20261027/ca_variant.patch`（skill 仓 scripts/） | 通过，无冲突 |
| **Prompt 指纹自校验** | `_prompt_fingerprint(challenger_system, "v3-falsify", _SAMPLING_TAG)` 复算改前值 | 得 `bd4f78da4ac7`，**与今日生产台账 `challenger_prompt_hash` 逐字一致** ⇒ 复算方法可信 |
| **改后指纹** | 同上 | `40e9681c9661` = handoff 记录的 C-A 离线臂哈希（§8.4） |
| **回滚路径实跑校验** | `git apply -R --check .../prompt_ca_revert.patch`（临时副本） | 通过 ⇒ 回滚可用 |
| **契约兼容性（改前核）** | 读 `llm_synth.py:2590-2612` 挑战者输出契约 | 无条数类约束；`skip_reason` 本就必填；`:2581` 已有「证据不足→SKIP+skip_reason」⇒ 新文案不引入违约 |
| **Prompt 改动后全量回归** | skill 仓 `pytest tests/ -q` | 131 passed / 1 skipped / 1 failed（同一既有红点） |
| **分析师侧指纹未动** | `llm_synth.PROMPT_HASH` | 仍为 `c7444d67ce58`（本次只改挑战者侧） |

新增契约用例锁死 9 条：① DISAGREE+SKIP 不抬升且不 BLOCK；② 抑制行进 reasons；③ 真弃权（PARTIAL_AGREE+SKIP）行为不变；④ 同向 AGREE 不变；⑤ 真反向不变；⑥ 自辩 DISAGREE 不抑制；⑦ 回滚开关恢复旧行为；⑧ 文案去伪且保留「弃权」；⑨ 真弃权文案不变。

## 6. 绩效证据（诚实声明：此项**不构成收益证据**）

10 行受影响中**实际成交 3 笔**：

| date | symbol | pnl_net | exit_reason |
|---|---|---|---|
| 2026-09-29 | US.MDB | +10.56 | SL_BREAKEVEN |
| 2026-10-06 | US.NU | +9.02 | SL_BREAKEVEN |
| 2026-10-07 | US.BKV | +91.92 | SL_TRAIL_LOCK |

其余 7 行未成交（PENDING 或已翻 SKIP）。

**若按新口径回溯，这 3 笔会被降级到 WARN ⇒ 可能少赚约 +$111.50。** n=3、单笔噪声量级远大于该值、且未按日聚类，**远低于任何可判定 MDE**，因此：

- 本修复的正当性**只来自档位阶梯的语义正确性**（「明确反对」不应拿最高档），**不来自任何收益证据**；
- **不得**据此声称修复改善收益；反之也不得据此否决修复（那会把 3 笔样本当判据，违反宪章第 9 律）；
- 正确表述：**这是一个语义/一致性修复，收益影响未知，需 ≥5 个交易日观察期后按既有执行契约结算。**

另：修复后 gate 与 adapter 在该形态上首次给出**同一个 conf（0.50）**，消除了「同事件两个数」的一处来源（§11.8 E17 所述差异仍存在于真弃权分支与分数差衰减项，本批未动）。

## 7. 既有红点（非本批引入，如实记录未修）

`tests/test_prompt_source_truth.py::test_prev_close_apply_changes_snapshot_and_prompt` 失败：断言 `prev_close_price == 32.69`，实得 `32.91`。

**已用 HEAD 原版代码复跑证明为既有红点**（把 `git show HEAD:` 的原 `llm_synth.py`/`signal_gate.py` 复制到临时目录跑同一测试，同样失败）。该测试依赖活数据（真实日 K 前收盘），随行情滚动失配。**不在本批修复范围**（属 prev_close 对账口径问题，另立）。

## 8. B3：Prompt C-A 变体 —— **已实施**（用户终审覆盖 §14/§15 裁决）

### 8.1 前次建议被自我推翻，现按用户裁决落地

前次建议中「按 DEB §14/§15 已验收结果推进生产影子」**与 DEB 自身裁决矛盾**，该更正当时成立：

- `DEB-20261027-da-scope-and-confidence-coupling.md` §14 verdict = `REPLAY_LIMITED`，原文明确「**不支持申请生产影子**」；
- §15 verdict = `OFFLINE_CHECK_PASS_LIMITED`，原文明确「仍无反向单反证实模、T2–T4 实模、生产排序或盈利证据，**不自动申请生产影子**」。

**用户于 2026-10-27 终审指令**：「如果明确存在逻辑问题，请针对 prompt 直接做修复」—— 即：在**逻辑问题已确证**的前提下，跳过「再申请一轮生产影子」的前置，直接修 Prompt。三角色制里「用户终审」为最终裁决，本次据此落地，并在此留痕原裁决被覆盖的事实。

### 8.2 逻辑问题（确证，与 §1 同源但落在 Prompt 侧）

挑战者 Prompt 内部存在**三条互相排斥的裁决规则**：

| 位置 | 原文 | 判据类型 |
|---|---|---|
| `llm_synth.py:2555`（system 铁律） | 「如果你找不到**≥2条**有数据支撑的具体反证…并给 **AGREE**」 | **条数** |
| `llm_synth.py:2576`（system 裁决定义） | 「PARTIAL_AGREE: 方向一致, 但验证发现**至少一条实质硬伤**」 | **严重性** |
| `llm_synth.py:2635/2637`（user 第三步） | 「你找到了**≥2条**…→DISAGREE」/「找不到**≥2条**…→AGREE」 | **条数** |

**一条决定性反证同时满足「<2条→AGREE」与「≥1条硬伤→PARTIAL」⇒ 两条规则给出相反指令。** 该矛盾在生产的后果（12 个交易日实测）：`PARTIAL_AGREE` 占 61%–96%（今日 23/29 = 79%），AGREE 高分档（conf 0.70 / tier 优）近乎不可达。

### 8.3 改法（严格照 DEB §13「只改严重性优先于条数」）

应用 `docs/offline/ca-20261027/ca_variant.patch`（离线验收过的 C-A 变体），**只改两处文案，不改方向规则、模型、score/conf 映射、门、字段**：

- `:2555` 铁律：去掉条数门槛 ⇒「反证按实质与出处评价，**不设条数门槛**。找不到有数据支撑的实质反证时…verdict仍须满足下述方向条件」。
- `:2634-2638` 裁决四条：改为**方向 + 严重性**口径
  1. 独立方向相反 **且** 有足以推翻方向的数据反证 → DISAGREE（my_direction 给反方向）
  2. 同向但**至少一条**有出处的实质硬伤 → PARTIAL_AGREE
  3. 同向且**无实质硬伤** → AGREE（次要风险只写 missed_risks，**不构成 PARTIAL**）
  4. 证据不足 → my_direction=SKIP + skip_reason 写明具体证据缺口

**契约兼容性已核**（改前核，非事后追认）：

- 挑战者输出契约（`llm_synth.py:2590-2612`）**本就没有任何条数类约束**；`missed_risks` 契约原文即「若无实质反证写'无实质反证', 禁止编造」，与新文案同向；
- 新增的第 4 条（证据不足→SKIP+skip_reason）与**既有**契约一致：`skip_reason` 本就是必填字段（`:2604`），`:2581` 早已写「证据不足就给 SKIP 并在 skip_reason 写明具体证据缺口」⇒ **不引入新违约、不需改契约、不需改校验器**；
- 方向规则未放宽：DISAGREE 仍**必须给反方向**，与原「≥2条且足以推翻方向」同向。

### 8.4 强交叉验证：生产 prompt 与离线验收臂**逐字节相同**

用 `_prompt_fingerprint` 生产同口径（`challenger_system` + `"v3-falsify"` + `_SAMPLING_TAG='t0'`）复算：

| 版本 | 指纹 | 出处 |
|---|---|---|
| 改前 | `bd4f78da4ac7` | **与今日生产台账 `challenger_prompt_hash` 逐字一致** ⇒ 复算方法已自校验 |
| 改后 | `40e9681c9661` | **与 handoff 记录的 C-A 离线回放臂哈希逐字一致** |

⇒ **生产 prompt 现与离线验收过的 C-A 变体完全同一**，§14/§15 的离线证据（2 样本 / 10 次调用 / 161,951 tokens / 0 错误 / finish=stop / 契约零违规）适用于生产这一份文本，**不是「另一个版本」**。这也意味着 §14/§15 的证据范围限制（无反向单反证、T2–T4 未实模、无生产排序/盈利证据）**依然成立**，本次落地不解除这些限制。

### 8.5 影响面与回滚

- **不改**：方向规则、模型、score/conf 映射、所有门、输出字段、契约校验器、tier 阶梯、`quality_score`。
- **可观测**：挑战者 prompt 指纹 `bd4f78da4ac7` → `40e9681c9661` ⇒ **明日台账可按该键切分改版前后**（口径七律#2）。
- **回滚**：`git apply -R docs/debates/patches/DEB-20261027-da-disagree-abstain-tier-fix/prompt_ca_revert.patch`（已在临时副本上**实跑校验逆向应用干净通过**）；或置 `DA_CHALLENGER_PROMPT_VARIANT=baseline`（未实现，见 §9.6，若需则另立）。
- **生效时点**：skill 每批由 carrier 新起进程 ⇒ **明日 08:00 ET 那批即生效，无需重启 ftseek-us**（已确认 `app/` 不 import skill）。

### 8.6 本次落地**不能**被当成的东西

- **不是**收益改善证据：离线回放与本批均无正负 bps 结论，n=2 样本远低于 MDE。
- **不是**「PARTIAL 问题已解决」的证明：只有**明日起的新批台账**能读出 PARTIAL 占比是否真的下降。**验收读数**：明日台账 `challenger_verdict` 分布中 `PARTIAL_AGREE` 占比应显著低于 61%–96% 区间；若无变化，说明模型仍倾向判硬伤，**再议 Prompt，不得据此判定修复失败**。
- **不解除** §14/§15 的限制（无反向单反证、T2–T4 未实模、无生产排序证据）。

## 9. 活体验证：仲裁留痕首次落地（同批附带产物）

§11.8 记「`instruction_arbitration.jsonl` 尚未产生 ⇒ 维持 TOO_EARLY」。**2026-10-27 ET 09:04 该事件已真实触发**：

- 生成轮于 ET 09:00 开跑（`已有 17 行但无完成标记 (UE 先写/上次中断), 继续生成` → `🚀 盘前生成开始`），09:04:52 收尾 `✅ 生成完成: 72/72 条指令入库`；
- 留痕文件 `data/shadow/instruction_arbitration.jsonl` 首次生成，**15 行**，全部 `stage=save_instruction` / `write_outcome=rejected_kept_existing` / `reason=empty_did_vs_sourced`；
- 逐行可读：`attempted`（被拒的投票：direction=SKIP，skip_reason 如 `BUY confidence 35 < 55`、`IC方向冲突: SELL_SHORT候选short侧净IC=-0.20(方向条件化)`）与 `kept`（存活的 DA 行：id / did / direction / confidence）；
- 同批 `batch_observation` 首次活体即 `complete`：selected 29 / analysed 29 / publishable 17 / missing 0 / bridge_fresh=true。

**顺带实证**（§11.5 A 批「装饰门」命题的活体证据）：生成轮对 17 个 DA 标的中 15 个判 `confidence < 55 → SKIP`，但这些 SKIP 因空 `decision_id`（优先级 0）低于 DA（优先级 3）被**写入层拒绝**，DA 行原样存活；门「判了」但「没生效」。

**遗留观察（未修，另立）**：`save_instruction` 的 UPDATE 语句对 `direction` / `confidence` 是**无条件覆写**（仅 `decision_conf` / `model_used` / `metadata_json` 有 COALESCE 保护）。生成轮对同 `decision_id` 的 DA 行会走原地 UPDATE（今日 `US.TSLA`、`US.TSM` 两行 `updated_at` 被刷新至 13:04:51）。**本次因生成轮取 bridge 值、与 DA 同源，两行数值未变**（仍 70/0.7），故未造成可见污染；但这是**潜在覆写面**，须另立一核实。

## 10. 仍未落地 / 待用户裁决

1. ~~**Prompt C-A**：按 §8 维持不推进~~ → **已于 2026-10-27 按用户终审落地（§8）**。残留项：明日起需读新批台账的 `PARTIAL_AGREE` 占比（§11.2 第 12 条）；§14/§15 的证据限制（无反向单反证、T2–T4 未实模、无生产排序/盈利证据）**未被本次落地解除**。
2. **PARTIAL_AGREE 占比 61%–96%**（12 个交易日连续）：Prompt 已改（§8），但**是否真降须等明日起新批台账**；`debate_state`/档位改动都不能治它。
3. **弃权分支 gate/adapter 口径仍不一致**：gate 按 `score/10` 抬升、adapter 保持 0.50；真弃权形态下两者仍给出不同 conf（§11.8 E17）。本批只对齐了 DISAGREE 形态，未动真弃权分支。
4. **`"AGREE" in debate_outcome` 是死代码**（所有现存文案均不含该子串），既无害又是埋雷；清理需连带核对 adapter 侧。
5. **save_instruction UPDATE 无条件覆写 `direction`/`confidence`**（§9 末）：潜在覆写面，待专项核实。
6. **挑战者 user prompt 不在 prompt 指纹内（既有观测洞，非本批引入）**：`llm_synth.py:2727` 的 `_challenger_prompt_hash = _prompt_fingerprint(challenger_system, "v3-falsify", _SAMPLING_TAG)` **只哈希 system 段**，而挑战者的**裁决规则（第三步）位于 user 段**（`:2634-2638`）。分析师侧（`:1449`）是 `SYSTEM_PROMPT + effective_user_template() + tag` **两侧都哈希** ⇒ 挑战者侧少一半。本批因 system 段（铁律）也改了，指纹**仍会移动**（`bd4f78da4ac7`→`40e9681c9661`），故**本批切分不受影响**；但**今后只改 user 段的变更将静默不可切分**。修法需把 user 段抽成模板常量（f-string 内含 3 处逐标的插值，不能直接哈希渲染结果）——属结构改动，另立一核实，**本批未动**。
7. **EXT 侧两项（与 DA 无关，单独待裁）**：
   - `EXT_RESEARCH_ARM_UNTIL=2026-10-31` ⇒ **11-01 自动失效**；10/19–10/20 的盘前成交全部来自 research 臂（band 0.5%–10.0%），严格臂 band 仅 0.5%–2.0%；10/21 起盘前连续 4 个交易日 0 单。失效后盘前供给会进一步收窄，须确认是否有意。
   - **EXT 持仓无券商侧止损**：富途 SIM 拒 STOP/STOP_LIMIT，`initial_sl`(-5%) 仅由规则引擎在 09:30 后生效；`premarket_exit_shadow` 明确「只做止盈，不做止损」。**活例**：`US.MSFT` 2026-10-20 07:31 ET 盘前开于 $535.60，已携带 5 个交易日，`sl_broker_order_id` 为空，盘前影子记 `decision_detail="underwater_no_extended_loss_stop"`。
   - 另：`PREMARKET_MAX_PER_DAY(10) == MAX_PER_DAY(10)` ⇒ 盘前子配额永远先被总配额触发，"给 afterhours 留位"实际未生效；EXT 侧 `admission.evaluate()` 仅记录不拦截（失效方向错）。

## 11. 复查清单（请第二 agent 逐条打假）

### 11.1 档位阶梯修复（§1–§7）

1. 用 §2 命令复跑，确认读数 2308 / 10 行 / 10× PASS→WARN / 0 BLOCK。
2. 用 `git show HEAD:.hermes/skills/deep-stock-analysis/scripts/signal_gate.py` 对比，确认改动**仅**在弃权分支（+26/−2 行），未触碰 `tier` 映射、`quality_score`、G1 破位硬买、方向冲突记录。
3. 复跑 `pytest tests/test_disagree_no_lift_gate.py tests/test_debate_state.py`（skill 仓）确认 17 passed；确认 `test_debate_state.py::test_abstain_priority_over_raw_disagree` **仍绿**（未改 `debate_state`）。
4. 检查 `llm_synth.py` 新弃权文案同时满足：含「弃权」、不含「合法弃权」、不含 `AGREE` 子串（含 `DISAGREE`）。
5. 在 `data/da_gate_decisions.jsonl` 查明日台账是否出现 `弃权抬升抑制` 行，并核对其 `旧口径conf=` 与 `decision_conf` 是否自洽。
6. 确认 `app/playbook/admission.py:240` 与 `app/review/daily_review.py:914` 的「弃权」子串判定仍能命中新文案（历史分层未断）。

### 11.2 Prompt C-A 修复（§8）

7. 复核 Prompt 改动**只**两处（`llm_synth.py` 的 `:2555` 铁律 + `:2634-2638` 裁决四条）：用 `git diff` 确认 `signal_gate.py` 的 tier 映射、`adapter.py`、输出契约、校验器、字段、模型、conf 映射**全部未动**。
8. 复核**方向规则未放宽**：改后 DISAGREE 仍必须「独立方向与分析师相反 + 给反方向」；AGREE 仍要求同向；只把「条数门槛」换成「严重性口径」。
9. 复核指纹：`_prompt_fingerprint(challenger_system, "v3-falsify", _SAMPLING_TAG)` 改前 = `bd4f78da4ac7`（须与今日台账逐字一致）、改后 = `40e9681c9661`；明日台账 `challenger_prompt_hash` 应变为 `40e9681c9661`，**据此切分改版前后**。
10. 复核契约兼容：确认挑战者输出契约（`:2590-2612`）无条数类约束、`skip_reason` 本就必填、`:2581` 已有「证据不足→SKIP+skip_reason」⇒ 新增第 4 条不引入 `skip_reason_missing` 违约。
11. 复核回滚可用：`git apply -R --check docs/debates/patches/DEB-20261027-da-disagree-abstain-tier-fix/prompt_ca_revert.patch`。
12. **明日读数**：新批台账 `challenger_verdict` 分布中 `PARTIAL_AGREE` 占比是否显著低于改前的 61%–96%（今日 79%）。**若无变化，不得据此判修复失败**（模型仍可能倾向判硬伤），应另议 Prompt。
13. 确认分析师侧 `PROMPT_HASH` 仍为 `c7444d67ce58`（本批只改挑战者侧）。
14. 确认本批**未**产生生产 DB 写入、**未**重启服务、**未**调用 LLM、**未**改门槛/名单/并发/仓位/退出。

---

最后更新：2026-10-27（本条）
