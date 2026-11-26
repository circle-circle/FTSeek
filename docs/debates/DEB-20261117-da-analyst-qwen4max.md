# DEB-20261117 — DA 主分析模型 qwen3.7-max → qwen4-max（用户裁决执行批）

- 类型: 模型切换（信号源行为变更）；用户直接裁决，非辩论立项
- 触发: 用户 2026-11-17「亏损比较严重，交易质量源头产出太差，方向和策略不太正确，我需要更换主分析的模型，将 qwen3.7-max 切换为 qwen4-max」
- 状态: 已执行（待首夜生产实证）
- 执行: Codex (gpt-6.1-sol)
- 关联: OKR-2026-11 主 KR（DA-only 净 bps/笔 > 0）；先例 = 2026-08-27 UE 主模型切换 / 2026-09-15 UE glm-5.3 切换（用户裁决 + 实弹可达性 + 归因切分 + 一行回滚）

## 1. 范围界定（一次一变量）

- **只换 DA 分析师（主分析）主模型**。DA 链唯一定义点 =
  `/root/.hermes/skills/deep-stock-analysis/scripts/config.py` `LLM_MODEL`
  （进程 env > `.env.us` > 默认值；已核 `.env.us` 与 hermes 网关进程 env 均无
  `LLM_MODEL`/`CHALLENGER_MODEL` 覆盖，默认值即生效值）。
- **不动**: 挑战者链（deepseek-v4.1-flash / kimi-k2.6 / glm-5.2，家族约束仍成立 —
  分析师仍 qwen 家族）、UE（glm-5.3）、`unified_config.yaml` `qwen` 实体
  （qwen3.7-max，被 playbook_generator/premarket_planner 等 ~15 个 feature 引用，
  改实体 = 超授权连带升版）、事件简报链（`da_event_intelligence.py --model` 默认
  qwen3.7-max，advisory-only）、hermes cron 编排 agent 的 `model` 字段（非分析本体）。
- 仓内 `scripts/divergence_arbitration_shadow.py` `PRIMARY_MODEL="qwen3.7-max"`
  为休眠脚本（crontab/hermes cron 均无调度）的标注常量，本批不动；若将来复活须同步。

## 2. 可达性与生产同构实弹（2026-11-17，零生产写入）

1. **存在性**: DashScope `/models` 列表（182 项）**未收录** qwen4-max，但 chat
   实弹回显 `model=qwen4-max`、`finish=stop`（列表滞后，同 glm-5.3 先例）。
   `qwen4-max-preview` / `qwen4.0-max` 均 Access denied ⇒ 裸名 `qwen4-max` 为正主。
2. **参数兼容**: 小 prompt 三发（think 8192 / 关思考 / cache 数组格式）全过，
   1.7–3.6s，json_object ✓、cache_control ✓、thinking_budget ✓。
3. **生产同构全量回放**（AMD 2026-11-16 raw checkpoint + SYSTEM_PROMPT +
   MAIN_EVENT_CONTRACT，cache_system + user_tail + temp0 + json_object）:
   | 档 | 延迟 | reasoning | completion | finish | 契约 | 方向/score |
   |---|---|---|---|---|---|---|
   | budget 8192, maxtok 16192 | 278.8s | **8192（恰好撞顶）** | 14003 | stop | 23 键 ✓ | SELL_SHORT / 6 |
   | budget 16384, maxtok 24384 | 399.8s | 14547（自然收尾） | 20349 | stop | 23 键 ✓ | SELL_SHORT / 6.5 |
   对照: 生产 11-16 同标的 qwen3.7-max = SKIP / 3.0（reasoning 5015，76.6s）。
   方向分歧实例留档（非质量证明，n=1）。
4. **反证（切换空操作形态）**: 旧参数（timeout 240）下 house 探针
   `da_model_swap_probe.py --mode repeat` 两发全 TimeoutError → DEGRADED SKIP，
   728.7s/轮 ⇒ **不抬 timeout 则切换 = 全量静默降级**，必须先配套。

## 3. 变更包（四件，互为前提，各自独立回滚）

| # | 文件 | 改动 | 依据 | 回滚 |
|---|---|---|---|---|
| 1 | skill `config.py` | `LLM_MODEL` 默认 qwen3.7-max → **qwen4-max** | 用户裁决 | 改回一行；备份 `config.py.bak-llmmodel-qwen4max-20261117` |
| 2 | skill `config.py` | `DA_THINKING_BUDGET` 8192 → **16384** | 8192 撞顶 = 思考腰斩（9/14 用户裁「确保正常的思考不被中断，效果优先」）；16384 实测自然收尾 14547 | `.env.us` 加 `DA_THINKING_BUDGET=8192` |
| 3 | skill `llm_synth.py` | 分析师思考档 timeout 240 → **600** | 实测 ~400s/候选，240 必超时（§2.4 实证） | 改回 240；备份 `llm_synth.py.bak-analyst-timeout600-20261117` |
| 4 | skill `pipeline.py` | `DA_MAX_WORKERS` 默认 5 → **8** | 32 候选 × (分析师 ~400s + 挑战者 ~240s) / 5 worker ≈ 68min > carrier 0857 ET 死线（08:00 发车 57min 窗）；8 worker ≈ 43min | env `DA_MAX_WORKERS=5`；备份 `pipeline.py.bak-workers8-20261117` |

- max_tokens 联动推导 = 16384 + 8000 = **24384**，qwen4-max 实弹接受（产出 20349 tok）。
  config 旧警告「预算 ~12000+ 顶 qwen 输出上限」系 qwen3.7-max 口径，已实测排除。
- carrier（`/root/.hermes/scripts/da_main_carrier.py`）死线 0857 ET **不动** —
  它锚定 09:00 ET generate 窗（注释: 留 ≥3min finalize+bridge），不可后移。
- checkpoint 缓存按 `config.LLM_MODEL` 键控 ⇒ 换模型自动作废旧缓存，无混样。
- **零 ftseek-us 重启**: DA 经 hermes no_agent carrier（job 9694df7b6024，
  `0 8 * * 1-5` ET-pinned）每夜新进程读 config，改动下轮自然生效；
  主服务不 import skill 代码。

## 4. 归因与验收（预注册）

- 归因字段: 每决策 `_model_reported`（API 回显）+ checkpoint `_ckpt_model` +
  bridge `model_used` ⇒ qwen4-max 时代 vs qwen3.7-max 时代天然可切分。
- 验收尺子 = OKR-2026-11 主 KR: **DA-only 净 bps/笔**（A9 干净口径，日聚类 + MDE，
  宪章第 5/9 律: n<30 不下结论）。切换前基线: 前瞻窗 DA -2.25bps/笔（n=80）。
- 首夜观察项: ①分析师延迟分布（预期 300–550s）②reasoning 撞顶率（16384 档仍可能
  撞顶, 见 §6 — 撞顶时 finish=stop/契约仍 VALID, 正文预算不受挤; 若撞顶率高且
  质量指标恶化, 再裁 budget vs 死线窗权衡, 勿静默抬预算越窗）
  ③契约失败/DEGRADED 率 ④整轮完成时刻 vs 0857 ET 死线 ⑤429/配额错误（8 并发新负载）。
- 成本披露: 输出 tok ~7k→~20k/候选（~3x），按 qwen3.7-max 费率估 ~$2–3/夜；
  qwen4-max 官方定价未核（/models 不暴露价格），以账单为准。DA 直连不经
  llm_audit/llm_budget 门，成本观测靠 checkpoint `_usage` 汇总。

## 5. 风险与诚实披露

- **方向分布会变**（§2.3 实例: 同输入 SKIP→SELL_SHORT）— 这是切换的目的，也是风险；
  无离线证据证明 qwen4-max 更准，只有用户裁决 + 可达性/契约实证。质量裁决交给前瞻样本。
- 8 并发对 DashScope/Futu 配额安全性未实测（5 并发有历史安全记录）；429 有单次重试兜底。
- 挑战者延迟 ~240s 系回放残差推算（728.7 − 488），非直接测量；首夜以实测为准。
- 若首夜整轮越死线: carrier 会 `--mode finalize` 从 checkpoint 收尾（部分提名），
  处置 = env `DA_MAX_WORKERS` 再抬或 `DA_THINKING_BUDGET=8192` 降档（两旋钮独立）。

## 6. 验证记录（2026-11-17）

- **e2e house 探针**（`scripts/da_model_swap_probe.py --mode repeat --main-model
  qwen4-max --runs 2`，AMD 2026-11-16 生产同构，**改后生产代码路径、零参数覆盖、
  零生产写入**；输出 `data/shadow/event_intelligence/model_swap_qwen4max_e2e_20261117/`）:
  | run | 方向/score | 状态 | 总延迟 | 分析师 reasoning | finish | 挑战者 |
  |---|---|---|---|---|---|---|
  | 1 | SELL_SHORT / 6.0 | VALID, 0 契约错 | 368.1s | 9986（自然收尾） | stop | deepseek-v4.1-flash ✓ |
  | 2 | SELL_SHORT / 6.0 | VALID, 0 契约错 | 501.2s | **16384（撞顶）** | stop | deepseek-v4.1-flash ✓ |
  两 run `main_model_reported=qwen4-max`，方向/score 一致。
- **撞顶披露**: run2 reasoning 恰好=16384 ⇒ qwen4-max 自然思考长度方差大
  （9986 / 14547 / ≥16384），16384 档**不能保证零撞顶**。与 2048 时代病灶的区别:
  max_tokens=budget+8000 联动 ⇒ 撞顶只截思考、**正文预算恒留 8000**（run2 正文
  5470 tok 完整、finish=stop、契约 VALID）。是否进一步抬预算 = 首夜数据后的权衡
  （抬到 24576+ 会把延迟推向 600s+ / 越 0857 死线窗，属另一个裁决）。
- **夜窗核算**: ~370–500s/候选 × 32 候选 / 8 worker ≈ 25–33min < 57min 窗 ✓
  （40 候选 ≈ 42min 仍留余量）。
- **配置生效核验**: `import config` → LLM_MODEL=qwen4-max / BUDGET=16384 /
  挑战者链不变；三文件 py_compile ✓。
- **未验证**: 首夜整轮生产实证（延迟分布/撞顶率/死线余量/8 并发配额）；
  qwen4-max 官方定价；质量优劣（须前瞻样本, n<30 不下结论）。

## 7. 回滚总开关（第1轮口径）

一行: `config.py` `LLM_MODEL` 改回 `qwen3.7-max`（或 `.env.us` 写 `LLM_MODEL=qwen3.7-max`）。
配套三件可留（对 qwen3.7-max 无害: budget 16384 > 其自然思考 max 5898；timeout 600 只放宽；
workers 8 只提速）——即**单行回滚完整恢复旧模型行为**。

---

## R2. 能力限制审计与解除（2026-11-17 第2轮，用户裁「确保不要限制模型自身的能力，充分发挥信息整理和有效推理」）

> 本轮取代第1轮的两项参数: `DA_THINKING_BUDGET` 16384 → **0（无帽）**；分析师 timeout
> 600 → **config.DA_ANALYST_TIMEOUT=900**。第1轮其余结论不变。触发 = 用户要求复审
> 信息输入与推理链路，解除对模型能力的人为限制。

### R2.1 限制清单审计（逐环节）

**已解除（能力限制）**：

| # | 环节 | 原限制 | 现值 | 实弹依据 |
|---|---|---|---|---|
| 1 | 思考预算 | `thinking_budget=16384` 硬帽 | **0 = 不传帽**，模型自然收尾 | R1 e2e run2 reasoning 恰好=16384 撞顶；无帽探针 reasoning=15173 自然收尾 / 378.1s / finish=stop / 契约✓ |
| 2 | 输出总预算 | 无帽档推导 max_tokens=16000（连自然思考都装不下） | **DA_UNCAPPED_MAX_TOKENS=131072** | trivial 实弹接受 131072；全量 prompt 实弹 completion 20507–21951 无截断 |
| 3 | 分析师 timeout | 硬编码 600 | **DA_ANALYST_TIMEOUT=900**（config 化） | 无帽典型 358–500s，~1.8x 余量；timeout 只是上限不改典型延迟 |
| 4 | K线输入 | 采集 60 日**只渲染近 5 日**（模型被判支撑/阻力/趋势却看不到历史形态） | 全量 60 日 | 纯呈现层截断，零新增采集 |
| 5 | 新闻输入 | 采集 10 条只渲染 8 条标题 | 采集 **20** 全渲染 | 同一 API 调用放宽 size |
| 6 | 社区情绪 | 30 帖只渲染 **5 帖×120 字**（帖长 p50=528 / max 12274） | **30 帖×800 字** | 120 字丢掉大部分正文 |
| 7 | 内部人交易 | 采集 10 笔只渲染 5 笔 | 全量 | 呈现层 |
| 8 | 研报 | 采集/渲染 5 条 | **10** 条 | 同一 API |
| 9 | Finnhub 新闻摘要 | **6 条×200 字**（源摘要最长 1879 字） | **12 条×800 字** | 200 字截断丢事实细节 |

开关: `DA_INPUT_FULL`（config.py，默认 1；`.env.us` 写 0 → #4–#9 逐字节恢复原截断）。

**保留（治理设计，非能力限制，逐项说明）**：
- 论据溯源/反幻觉/数据充分性路由/SHORT 风险三问: 质量门，防编造，不限制推理本身。
- `score_band` 三档→数字映射（DEB-20260825 R14 P8）: 防 score 方差塌缩的校准设计，
  模型给档位的判断自由完整保留；回滚 `DA_SCORE_BAND=0` 存在。
- 挑战者链（deepseek-v4.1-flash/kimi/glm）及其数据视图截断: 独立模型的成本设计，
  本轮单一变量=主分析师，不动；挑战者思考已开（9/14 裁）。
- 契约修补路径（max_tokens=800）: 机械修 JSON，非分析调用。
- `LLM_TEMPERATURE=0`（2026-09-21 用户裁）: 可复现性设计；实测思考长度多样性正常
  （9986/13633/14547/14801/15173/≥16384），无退化迹象，不改。
- 执行层接口事实（止损钳制/飞刀门/追高闸）: 执行治理，prompt 已明示由代码执行，
  不依赖模型自我审查。
- 历史负向论据警示/条件黑名单/股性卡/成交档案注入: 软警示，**加信息**非减信息。
- synthesize 前置路径: 只有数据修正（prev_close 对账）与加性注入，无预 LLM 硬 SKIP 门。

### R2.2 验证（2026-11-17，零生产写入）

1. **无帽探针**（AMD 11-16 生产同构，enable_thinking=True 不传 budget，max_tokens=131072）:
   378.1s / reasoning 15173 自然收尾 / completion 20507 / finish=stop / 契约 23 键✓。
2. **采集放宽实测**（`collect_all("US.AMD")` 39.6s，只读）: news 20 / news_v2 12 /
   research 10 / sentiment 30 / kline 60 ✓；data_text 8843 → **19661 字符**。
   生料存 `data/shadow/event_intelligence/inputfull_raw_20261117/`（**非生产 checkpoint
   目录**，不污染今晚 resume 缓存）。
3. **改后全链 e2e**（house 探针 runs=2，今日新采集生料 + 全量呈现 + 无帽思考，
   输出 `data/shadow/event_intelligence/inputfull_uncap_e2e_20261117/`）:
   | run | 方向/score | 状态 | 总延迟 | prompt tok | reasoning | finish |
   |---|---|---|---|---|---|---|
   | 1 | SKIP / 3.0 | VALID 0 错 | 390.2s | 18984 | 14801（自然） | stop |
   | 2 | SKIP / 3.0 | VALID 0 错 | 358.7s | 18984 | 13633（自然） | stop |
   两 run 方向/score 一致，`main_model_reported=qwen4-max`，挑战者正常。
   （今日 AMD 数据给 SKIP/LOW 与 11-16 数据给 SELL_SHORT 不矛盾 — 输入是不同日数据。）
4. **夜窗核算**: ~360–500s/候选 × 32 / 8 worker ≈ **26–33min** < 57min 窗 ✓（余量充足）。
5. **成本披露（估）**: 输入 11825→18984 tok/候选（+61%），输出无帽 ~20–22k；
   按 qwen3.7-max 费率估增量 ~+$0.7/夜；qwen4-max 官方定价仍未核，以账单为准。

### R2.3 回滚旋钮（各自独立）

| 旋钮 | 回滚值 | 效果 |
|---|---|---|
| `.env.us` `DA_INPUT_FULL=0` | 0 | 输入侧 #4–#9 逐字节恢复原截断 |
| `.env.us` `DA_THINKING_BUDGET=16384`（或 8192） | >0 | 恢复有限思考预算（max_tokens 走 budget+8000 推导） |
| `.env.us` `DA_ANALYST_TIMEOUT=600` | 600 | 收紧超时上限 |
| `.env.us` `DA_UNCAPPED_MAX_TOKENS=24384` | 调小 | 收紧无帽档输出预算 |
| `config.py` `LLM_MODEL` 改回 qwen3.7-max | — | 总回滚（R1 §7；注意无帽档对 qwen3.7-max 也放行，其自然思考 max 5898 远低于预算，行为≈旧） |

备份: `llm_synth.py.bak-inputfull-20261117` / `data_collector.py.bak-inputfull-20261117` /
`config.py.bak-inputfull-20261117`（R1 备份仍在）。

### R2.4 未验证

- 首夜整轮生产实证（新输入量下的延迟分布/死线余量/8 并发配额/缓存命中率变化 —
  数据段变长不影响契约前缀缓存，但 user_tail 之后全文变长）。
- 质量优劣: 仍无离线证据，交前瞻样本（OKR 主 KR，n<30 不下结论）。
- 挑战者侧未动（其数据视图截断仍在）— 若主分析质量提升后挑战者成为短板，另立轮次。

---

## R3. P0 测量与归因执行批（2026-11-17 第3轮，用户裁「按建议优化完善执行落地」）

> 状态：**测量工具已实施并验证；零生产交易行为变更、零付费模型调用、零重启。**
> 本批不证明 qwen4-max / 全量输入 / 无帽思考具有费后 edge；首夜生产与前瞻 cohort 仍按 R2.4 验收。

### R3.1 范围与落地

| 工具 | 落地 | 关键防误读契约 |
|---|---|---|
| `scripts/da_model_ab_replay.py` | v3：模型 × `frozen/full/legacy` 输入臂 × repeats；实读 qwen4-max 无帽 `max_tokens=131072`、timeout=900；qwen3.7 历史思考臂 8192/16192 | full/legacy 只从同一 `raw.collected` 重建，不补采、不注动态历史/EventContext；记录实际 shape/摘要长度/hash。旧 raw 不满足 20/12/10 采集数量时 WARNING，不冒充当前生产全量采集。API 失败不参与稳定性；空分母=`null/n/a` |
| `scripts/da_scope_confidence_audit.py first-night` | checkpoint/bridge/DB/schedule 四层首夜健康 JSON + exit code | 模型错配、主降级、非 stop、未修复契约、缺 bridge/DB/schema、分析数/缺股/指令数不符、超时收尾、越 08:57 ET = FAIL；缺预期分析数不猜；缺 reasoning 时 `cap_check=unknown`；日志缺失/空文件=`unknown`，429/timeout 只作观测 |
| `scripts/da_chain_contract_audit.py` | 每决策串起模型/Prompt/as-of → gate → planned/effective/fill → fees/net/exit outcome | 复用严格 trade-instruction resolver；decision_id 先按 symbol 收窄；同标的歧义多组成交隔离不合并；多 fill 逐笔 RR/gap/TP；`exit_price=0` 保持 open；CLI 显示 symbol_unique/ambiguous/fill_events/归因来源 |
| `scripts/da_event_inject_report.py` | 兼容 v3 控制组：`--control-input-mode=frozen/full/legacy` | 多输入臂未指定即 ERROR；只读 `model|input_mode` 精确摘要；空臂、缺声明生产模型、零可用记录 fail-closed，禁止 full/legacy 或其他模型混算 |

未改：`app/`、`config/`、生产 Prompt、信号门、Playbook、仓位、carrier、cron、通知、EventContext canary。未触碰他人未跟踪 WIP `scripts/verdict_outcomes_ledger.py` 及其测试。

### R3.2 独立审查与反例闭环

三轮 Kimi 独立审查 + 一轮 GLM 定向实证复核，修复了以下测量假象：

1. 首夜缺 bridge/DB/schema/预期分析数/遥测时假 PASS；缺 reasoning 假 `no_hit`；主输出非 stop 仅警告。
2. 重复 decision_id 跨 symbol 合并盈亏；同 symbol 多歧义组成交被汇总；多 fill 只算首笔 TP/RR/gap；`exit_price=0` 被当已平。
3. full/legacy 控制组混算、空控制组 `pass=True`、缺生产模型时借用其他模型胜率。
4. API 全失败却 `stable_direction_rate=1.0`、SKIP-only `price_geometry_rate=0.0`、空分母被写成零。
5. qwen3.7 非生产模型 `--no-thinking` 错用 16192 而非 3000；full 标签未披露旧 raw 只有 10/6/5。

GLM 最终定向复核：五个剩余 blocker 全不复现；四文件套件 **79 passed**；独立 probe 验证空臂 raise、失败总体 `None`、跨 symbol PnL/成交分离、多 fill 全量汇总；零文件改动、零付费 API。

### R3.3 验证

- 聚焦四套件：`tests/test_da_model_ab_replay.py` + `test_da_scope_confidence_audit.py` + `test_da_chain_contract_audit.py` + `test_da_event_inject_report.py` = **79 passed**。
- 相邻回归（上述 + repeatability/citation/instruction attribution/main carrier/ledger）= **137 passed**。
- `py_compile` 四脚本 ✓；`check_undefined_names.py` rc=0；`git diff --check` rc=0。
- 交叉矩阵 dry-run（AMD 2026-11-16，frozen/full/legacy × qwen4/qwen3.7 × repeats2）：计划 12 次调用，**实调用 0、写文件 0**；生产参数=qwen4-max/无帽/131072/900；旧 raw shape=10/6/5，full 臂明确 WARNING。
- 首夜工具旧批实跑（2026-11-16，预期 qwen3.7-max/budget8192）：32 checkpoint、预期 32、bridge/DB 24/24、模型三层一致、08:31:48 ET 完成、cap no_hit；准确判 **FAIL=`unresolved_contract:1`**，空日志=`unknown`，不是健康通过。
- 决策归因旧批实跑（2026-11-16）：24 指令、13 fill、13/13 metadata 精确归属、模型 mismatch=0、歧义=0；计划 vs fill RR 漂移 12 条、TP swallowed 2；输入版本旧数据=`None`，未伪造 full 标记。

### R3.4 未验证与下一步

- **未验证**：11-17 qwen4-max 首夜生产（本批实施时仍未发车）；付费模型×输入矩阵真实 API 接受度/成本/质量；任何费后 edge、死单率改善或 live readiness。
- 今晚 DA 后执行：

```bash
PYTHONPATH=. ./venv/bin/python scripts/da_scope_confidence_audit.py first-night \
  --date 2026-11-17 --expected-model qwen4-max --thinking-budget 0
PYTHONPATH=. ./venv/bin/python scripts/da_chain_contract_audit.py \
  --date 2026-11-17 --json
```

- 付费交叉回放只在另行明确执行预算后运行；dry-run 已给出调用矩阵与输入 shape，不以离线工具落地冒充质量结论。

---

## R4. 两日生产实证结算与入场延迟复查（2026-11-19，只读审计，零运行时改动）

> 状态：**§4/R2.4 预注册首夜观察项已按 11-17/11-18 两个生产日结算；链路健康；入场延迟裁决 = 维持 0945 + 120s 不动。**
> 本批纯落档：零 `app/`/`config/`/hermes 脚本改动、零重启、零付费模型调用（futu 快照为只读行情）。

### R4.1 链路健康（§4 观察项①–⑤结算）

| 指标 | 11-17 | 11-18 |
|---|---|---|
| 分析数 / 候选数 | 35 / 40 | 31 / 35 |
| 模型三层一致（ckpt/API 回显/bridge/指令） | qwen4-max 35/35，mismatch=0 | qwen4-max 31/31，mismatch=0 |
| 错误 / finish≠stop / 缺股 | 0 / 0 / 0 | 0 / 0 / 0 |
| ①延迟 p50 / p90 / max (s) | 339 / 445 / 524 | 367 / 442 / 562 |
| ②reasoning p50 / max (tok) | 13118 / 22290 | 14692 / 24810 |
| ②reasoning ≥16000 占比 | 4/35 (11%) | **11/31 (35%)** |
| prompt p50 / max (tok) | 22989 / 29627 | 22817 / 32935 |
| 前缀缓存命中（Σcached/Σprompt） | 26.7% | 25.3% |
| ④整轮完成 (ET) / 0857 死线余量 | 08:46:26 / ~10.6min | 08:44:43 / ~12.3min |
| 门禁 PASS/WARN/BLOCK → 指令数 | 5/9/21 → 14 | 6/11/14 → 17 |

- ②撞顶：budget=0 无帽 ⇒ `cap_check=not_applicable`、0 截断。**旧 16384 帽在 11-18 会截断 35% 候选的思考** — R2 解帽的直接收益实证（思考自然收尾，正文契约全部 VALID）。
- ③契约/DEGRADED：两日 0 错误、0 非 stop、bridge fresh、state=complete、无 unreadable checkpoint。
- ⑤429/配额：carrier 日志缺失 ⇒ 审计 `log_unknown`（两日唯一 WARN）。**429 计数不可观测，不是 0**；checkpoint 侧无限流/降级形态。
- 延迟落在 §4 预期 300–550s 窗（11-18 max 562s 单候选略超，未影响死线）；BLOCK 理由抽查实质（财报前 3 日硬规/无增量 edge/证据互斥），门禁与指令数吻合，非空转。

**结论：信息输入→推理→筛选链路健康；R2 解帽未引入契约/延迟/死线回归。**

### R4.2 结果诚实呈报（非质量裁决）

- 已平 11 笔（11-17×5 + 11-18×6）**全正 +$157.79，均 +37.64 bps/笔**（切换前基线 -2.25）；出场 SL_BREAKEVEN×7 / SL_TRAIL_LOCK×3 / DEAD_SIGNAL×1，**零 SL_INITIAL**（历史最大出血族）；持仓 0.1–1.4h（ARM 5.8h 隔夜）。
- cohort 内未平浮亏（11-19 05:00 ET 快照）：ELV **-$347.22** + LLY -$83.67 ⇒ **两日 cohort 净 ≈ -$273.1**（MU +$1.62，11-18 指令为 SKIPPED_EXISTING_POSITION，归因另计）。
- **ELV 焦点案**：11-17 LOW/conf35 BUY@439.42（thesis=板块性无信息回撤低吸，证据真实但方向错）；SL 420.28 早已跌破，现价 400.84（-8.78%）。亏损被 RFC-214 出场拒单死锁放大 ~$171（DEB-20261118 R18–R20 线程口径）；V1 已于 11-19 17:15 CST 部署、ELV 归回 playbook 自管，**预期今日开盘止损出场 — 未验证**。
- 窗外存量仓 LMT(11-13)/GOOG/META(11-16) 浮亏合计 -$301.65，不计入两日 cohort；全簿浮亏 -$730.92。
- 11-18 09:51–09:53 ET 入场拒单（BA/SPCX/APH/ARM，券商通道降级+死锁类）；BA/APH/ARM 重试后 BOOKED，**SPCX 仍 PENDING 未成交**。RFC-214 已修，观察自然触发。
- **n=11 < 30，宪章第 5/9 律不下模型质量结论**。方向性证据正向（已平全正+零 SL_INITIAL），但 ELV 证明 LOW 置信全仓执行仍可放大单票亏损；质量裁决交前瞻 cohort（OKR 主 KR）。

### R4.3 入场延迟复查（裁决 = 维持 0945 + 120s 不动）

现行三层（本批全部不动）：①`entry_open_blackout_hhmm: 0945`（`config/unified_config.yaml:1332`，11-12 用户令 5→15min，热载，回滚=0935）；②RFC-141 D2' 触发后 120s 复核（holdout 20%，现役 UNPROVEN）；③主循环 180s/快循环 10s。实际入场 09:50–10:13 ET。

| 尺子 | 窗口 | 读数 | 含义 |
|---|---|---|---|
| 黑窗影子 `data/entry_blackout_shadow.jsonl` | 11-17/18 | 27 hits 全在 09:30–31；首触价→实际成交方向感知等待成本 **-59.8bps（n=13，8/13 等待获益）** | 黑窗内等待更便宜，支持维持 0945 |
| `data/shadow/entry_cost_decomposition_latest.json` | 10-20 起 n=34 | open→exec +36.3bps 不利 | 与两日读数方向相反；样本窗跨旧 regime，不可直接对比 |
| `data/shadow/entry_delay_confirm_latest.json`（DEB-20260909 §5.2 预注册） | 滚动 | 额外 2/5min 延迟 ALL 净负/不可分辨（2min ALL t=-0.94）；SL_INITIAL 族显著获益（t=6.19/9.7）但属后验标签人群 | 不支持再加延迟；T19 红线禁止后验标签前置 |
| `data/shadow/entry_confirm_forward_latest.json`（RFC-141） | 11-05 起 | verdict **INSUFFICIENT**；avoidance 38.1%、opp_cost 0.12%；holdout 分层 open_lt_15m -23.73/笔 vs later_ge_15m +10.05（小样本） | 120s 门未证明也未证伪，方向性支持延迟但不足以裁决 |

裁决理由：①变更后 5 交易日证据方向正向（零 SL_INITIAL、等待成本有利、11-16 盘中 11 笔全正）；②各尺子样本均低于裁决门（n<30 / INSUFFICIENT / 不可分辨）；③**一次一变量** — 主分析模型刚换 2 天，此时动入场延迟会污染模型归因（OKR 主 KR 测量失效）；④黑窗影子即预注册前向尺子，继续攒样，n≥30 或模型 cohort 结算后再议。

### R4.4 问题点清单与处置

| # | 问题 | 处置 | 归属 |
|---|---|---|---|
| 1 | ELV 止损出场未验证（SL 已破、死锁已解） | 今日 22:30 CST 开盘后首验：trade_records ELV 出场行 + open_positions 清空 | DEB-20261118 R20 线程 |
| 2 | SPCX 11-18 拒单仍 PENDING | RFC-214 已修，观察新拒单路径自然触发，不手工干预 | 同上 |
| 3 | GOOG/META DA 翻 SHORT 但旧 LONG 未平、新 SHORT 指令 PENDING | 设计如此（出场 SSOT 独立，DA 不管仓）；机会成本观察项，要改须另立 RFC | 观察 |
| 4 | 夜窗余量 ~11min | 候选 ≤40 不动；若越窗按 §5 旋钮（DA_MAX_WORKERS 抬 / DA_THINKING_BUDGET 降档），勿静默抬预算 | §5 既有预案 |
| 5 | LOW-band（conf<40）全仓执行（ELV conf35） | 登记为影子候选（LOW 置信仓位建议）；OKR C1 须 RFC+用户批准，本批不实施 | 待裁 |
| 6 | carrier 日志缺失 ⇒ 429 不可观测（审计 WARN=log_unknown） | 可选遥测加固，不阻塞；checkpoint 侧无限流形态 | 待裁 |

### R4.5 验证（可复跑）

```bash
PYTHONPATH=. ./venv/bin/python scripts/da_scope_confidence_audit.py first-night \
  --date 2026-11-17 --expected-model qwen4-max --thinking-budget 0   # rc=0, WARN=log_unknown only；--date 2026-11-18 同
sqlite3 -readonly data/ftseek_us.db "SELECT COUNT(*),ROUND(SUM(pnl_net),2),
  ROUND(AVG(pnl_net/NULLIF(entry_price*quantity,0)*10000),2) FROM trade_records
  WHERE trade_date IN ('2026-11-17','2026-11-18') AND exit_time IS NOT NULL AND exit_time!='';"
# → 11|157.79|37.64
```

- 等待成本复算：黑窗影子 hits（date+inst_id 去重）join `playbook_instructions.execution_price`，方向感知 ⇒ n=13、mean -59.8bps、8/13<0。
- reasoning 分布：`data/da_checkpoint/{2026-11-17,2026-11-18}/*.json` `_usage` ⇒ ≥16000：4/35、11/31；缓存 26.7%/25.3%。
- 浮亏：futu `get_market_snapshot` 六仓（11-19 05:00 ET）⇒ 全簿 -730.92、ELV -347.22。

### R4.6 未验证

- ELV 今日开盘出场（审计时点 05:00 ET 未到开盘）；SPCX 拒单新路径自然触发（归 R20 线程）。
- 模型质量/费后 edge（n=11<30，前瞻 cohort 继续）；qwen4-max 官方定价与账单。
- 429 实际计数（日志缺失，log_unknown）；挑战者链质量（未动）。
