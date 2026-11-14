---
title: RFC-207 — EventContext、EXT 事件与 DA 指令质量只读接入
status: record_only_enabled_with_cron
date: 2026-11-13
approved_by: 用户「继续」
related: RFC-205、RFC-206、DEB-20261106 §65–66
---

# RFC-207 — 三项 record-only 接入

## 1. 授权边界

用户批准继续实施建议的三项只读接入：

1. 每日盘前 EventContext 共享简报；
2. EXT 持仓事件 `NO_CHANGE/REVIEW`；
3. 每日 DA 指令质量报告（有效卡、研究完整性、方向—计划一致性、价格几何）。

**明确不授权**：真实 direction/Gate/仓位/退出变化、生产模型替换、生产 SKIP Prompt 契约、
EventContext 自动写指令、EXT 自动 INVALIDATE/平仓、自动推送交易动作。

三项均归类 `PERMANENT_MEASURE`，不是 C1 新入场假设，不设毕业门，不用仓位/频率追样本。

## 2. 独立开关与回滚

| 影子 | 环境开关（缺省=false） | 产物 |
|---|---|---|
| EventContext | `DA_EVENT_CONTEXT_SHADOW` | `data/shadow/event_intelligence/live/latest.json` + 不可变 run 文件 |
| EXT 持仓事件 | `EXT_EVENT_RISK_SHADOW` | `data/shadow/ext_event_risk/live/latest.json` + 变化的 REVIEW 文件 |
| DA 指令质量 | `DA_INSTRUCTION_QUALITY_SHADOW` | `data/shadow/da_instruction_quality/latest.json` + 不可变日档 |

- 开关由 `scripts/shadow_runtime.py::env_flag` 每次运行实读；缺省、未知值均 false。
- `DA_EVENT_CONTEXT_SHADOW` 还必须同时有 CLI `--execute` 才允许付费调用；每日最多主/挑战者各 1 次。
- 回滚：`.env.us` 对应键改 0，下一轮即停，无需服务重启；registry/cron 只读项保留历史产物。
- 所有 latest 文件原子更新；历史证据 O_EXCL，禁止覆盖旧run。输入哈希相同则 `ALREADY_CURRENT`，不重复付费/写历史。

## 3. 数据与时间纪律

- EventContext：EventStore SQLite `mode=ro`，`first_seen_at<=as_of`；raw checkpoint 仅读；原始发布时间、实际采集时间、source tier/status、raw SHA256 均保留。 supplemental 最多48条，未审阅数必须披露。
- EXT：仅 `trade_records.exit_time IS NULL AND decision_id LIKE 'EXT-%'`；`entry_time<first_seen_at<=as_of`；输出只有 NO_CHANGE/REVIEW。底层官方反证也只保留 `official_invalidate_candidate` 供人工复核，不自动升级动作。
- 指令质量：只读 `data/da_checkpoint/<ET date>`，缺 bars 时价格/收益字段为 null，不伪造可达性或收益。
- 任何反事实 PnL 均不允许；后续经济验证另立 RFC 并预注册 bars、费用、日聚类与 MDE。

## 4. 调度与健康检查（cron 已获明确批准并安装）

三项均已完成手动 smoke，`.env.us` 开关当前为1。用户随后明确批准“安装 RFC-207 三条常驻 cron
（含每日 EventContext 付费调用）”，已按下列时间表安装，备份为
`data/crontab.backup.20261113_rfc207_pre.txt`：

- EventContext：CST 20:10 工作日（为 RFC-208 直接注入预留；原22:15已前移），`timeout 900` + `flock` 防重入。
- DA 指令质量：CST 22:20 工作日，零模型调用。
- EXT：每15分钟工作日，零模型调用、零券商连接；disabled 时立即退出。

每项注册 `PERMANENT_MEASURE`、`eval_on_demand=True`，evaluator 检查 latest 的 schema、as_of、状态、错误、计数和新鲜度。
不新增直连 notifier；无料不推。断供/错误只进入 registry/既有健康面。

## 5. 验收门（接入验收，不是收益裁决）

1. disabled：除读取 flag 外，零 DB/LLM/文件写入，退出码0。
2. enabled+execute：EventContext 每次输入哈希变化最多2次 LLM；相同哈希不重复调用。
3. EXT 仅 EXT 持仓，所有 INVALIDATE 均降级为 REVIEW；NO_CHANGE 不写逐仓历史。
4. 指令质量缺 bars 不造价格指标；SKIP 不一致与几何错误可见。
5. registry/evaluator 能区分 DISABLED/NO_DATA/OK/REVIEW/WARN/ERROR；latest 不覆盖历史证据。
6. 联合测试、完整棘轮家族、offline full-chain、undefined-name 与 diff 检查通过；生产 DB 哈希不变化。

## 6. 后续观察（不自动晋级）

- EventContext：事实覆盖、引证正确率、未审阅率、每次处理时延、validation errors；不得以方向变化声称 edge。
- EXT：积累 ≥30 position-event pairs 且 ≥10 ET 日；人工复核 REVIEW 相关性，误报与漏报并列。没有 bars 不算收益。
- 指令质量：跟踪有效率、研究完整率、方向—计划一致性、价格几何和 SKIP 不一致趋势；质量报告不改变真实指令。
- 任何进一步接 Prompt、Gate、订单或退出都必须另立行为 RFC + 用户明确裁决。

## 7. 手动 smoke 实测（2026-11-13）

- EventContext：11-12/21:00Z，1059 events、96 facts、34-symbol universe、4 transmissions、
  `validation_errors=[]`；第二次同输入为 `ALREADY_CURRENT`，没有新增 LLM 调用。
  `DA_EVENT_CONTEXT_SHADOW=1 ... --execute` 的两次实际调用均 `llm_calls=2`。
- EXT：实际未平仓 EXT=1（ASML），`NO_CHANGE`、errors=0、orders=0；latest/evaluator 均 OK。
- 指令质量：11-12 共34卡，valid=34、research_complete=34、consistency=0.7941、geometry=1.0、
  7张SKIP不一致、bars_missing=8；evaluator=WARN（仅报告）。
- 修复两项真实集成问题：①仓库 `config/` 遮蔽 Hermes config，改为显式加载并恢复模块绑定；
  ②模型误写一个事件 ID，Prompt 升 `market_event_brief_v2`，要求逐字复制/不确定留空，绝不模糊纠正。
- 联合回归 **242 passed**；棘轮+offline full-chain **59 passed**；`check_undefined_names` 与 diff 检查通过。
- `ftseek_us.db` smoke 前后哈希不变；`event_stream.db` 同时刻有既有 ingestion 写入，
  不宣称全库静止，写方代码均为只读连接。
- 为加载新 registry，已执行 `systemctl restart ftseek-us`；MainPID=2844986，启动冒烟 **7/7**。
  重启前后 BA/HON/ASML 三仓不变、非 BOOKED=0。

**cron 已安装并核实**：crontab 第422–424行分别为 EventContext、指令质量、EXT。
`flock` 防重入、`timeout` 限长、日志分流齐全；回滚仍为对应环境键置0。
首轮 EXT 自动刷新 `2026-11-13T10:45:01.766162Z`：evaluated=1、NO_CHANGE、errors=0、
new_review_artifacts=0、production_write=false。任何接真实 Prompt/Gate/订单/退出另裁。
