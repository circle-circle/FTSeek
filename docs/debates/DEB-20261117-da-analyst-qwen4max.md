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
