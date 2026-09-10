---
title: UE→DA 供料链时序修复 — UE 一天两跑 (早跑供 DA / 晚跑供执行)
date: 2026-09-09
status: 已落地并端到端验证 (用户令「DA和UE的cron的时间，请基于新鲜度和信息密度和先后关系，进行合适的调整」)
related_code: scripts/llm_universe_scan.py, /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py, crontab
related_okr: OKR-2026-09
tags: [时序, 新鲜度, 信息密度, UE, DA, 供料链, crontab]
---

# UE→DA 供料链时序修复（2026-09-09，19:25 CST 落地）

## §1 病灶：A 路（UE→DA）半死，且设计注释与现实相反

UE 到下游有**两条独立的路**，此前只有 B 路活着：

| 路 | 机制 | 修前状态 |
|---|---|---|
| **A** UE→DA 深分析池 | DA skill `_load_ue_short_candidates()` 读 `data/shadow/llm_universe_scan.jsonl` 取 `scans[-1]`，**只取 SHORT**，cap 3，age 守卫 30h | **半死** |
| **B** UE→直接注入指令池 | `cmd_inject` 取 `today_scans[-1]`，写 `decision_id=universe_expansion:{date}:{symbol}` | 正常 |

A 路的设计注释（`deep-stock-analysis/scripts/pipeline.py:288`）写的是：

> 时序: UE 盘前扫描 **05:10 ET** 落盘 jsonl → DA **06:00 ET** 消费今晨候选 (**age ~1h**)

**现实恰好相反**：UE scan cron = `10 21 * * 1-5` = 21:10 CST = **09:10 ET**；DA cron（Hermes
`deep-stock-analysis 每日盘前扫描+分析`）= `0 20 * * 1-5` = 20:00 CST = **08:00 ET**。
⇒ UE 在 DA **之后 1 小时 10 分**才落盘。

两个后果，都已被实证：

1. **常态消费隔夜陈旧候选**：DA 08:00 ET 读到的最新行是**前一交易日 09:10 ET** 的，
   age ≈ **20.9h**（不是注释写的 1h）。而 `30h` 守卫恰好放行 ⇒ 静默入池。
   与 UE 自身契约冲突：`evidence_checklist.catalyst_within_24h` 要求催化剂在 24h 内，
   而喂给 DA 的假设已经 21h 老。
2. **长假/周末后必然断供**：age > 30h ⇒ 全拒。

**溯源实证**（`data/opportunity_scan_*.json` 内 `origin` 字段计数）：

| 交易日 | bridge 候选 | 其中 `origin=ue_candidate` | DA 当时读到的 UE 行 age |
|---|---|---|---|
| 9/2 | 14 | **0** | 20.5h（放行，但当日无合格 SHORT） |
| 9/3 | 17 | **2** ← 近 4 日唯一生效 | 20.8h（放行） |
| 9/4 | 14 | **0** | 20.8h（放行，候选被 `_seen` 去重吃掉） |
| 9/8 | **4** | **0** | **92.8h（9/4 09:11 ET → 9/8 08:00 ET，跨周末+Labor Day）⇒ 拒** |

## §2 三轴设计依据

**信息密度（ET 盘前时间轴，全部为 9/8 实测样本）**
- 04:00 ET 盘前开市；盘前量价**在 08:30–09:30 ET 达峰**（UE 9/8 的浮现理由全部引盘前量价：
  QCOM「盘前成交近 2 亿美元且涨 7.2%」/ NVS「盘前暴跌近 13% 且放量」/ BIDU「盘前跌 7.7% 领跌中概」）
- 8-K 早晨爆发段 = **08:35–09:06 ET**（9/8 样本 15 条里 11 条落在此段）；但 06:47–07:14 ET
  也已有 ≥15 条（9/9 UE-1 早跑实测）⇒ **8-K 全天覆盖需要两跑，单跑必然漏一头**
- 美国经济数据 08:30 ET 发布 → 宏观 brief cron（21:05 CST = 09:05 ET）在其后，DA（08:00 ET）在其前
  ⇒ DA 从来看不到当日宏观（9/8 实测 `宏观:0`）

**新鲜度**
- DA 需要 UE 的事件假设**尽可能新**（最优点 = 紧贴 DA 之前）
- UE 的执行面需要盘前量价**尽可能满**（最优点 = 紧贴开盘之前）
- 两个最优点相差 ~1.5h，**单次 UE 跑无法同时满足** ⇒ 必须两跑

**先后关系（硬约束）**
- `gen_event_symbols.py` 18:30 CST 产出 `event_symbols_{ET}.json` → 必须早于 UE
- UE-1 → DA（20:00）→ `da_bridge_dingtalk_push`（20:55）→ playbook 载 bridge
- UE-2 → inject → 开盘 21:30 → 首笔入场窗 09:35 ET（21:35 CST）→ confirm（21:40）
- **DA 不能后移**：DA 实测耗时 ~19min（9/8 last run 20:18:59），若挪到 21:15 之后，
  bridge 将晚于 21:35 的首个入场窗 ⇒ 退化成 `TechnicalFallbackAdapter`（今日 14:35 已见过该告警）

**为何两跑几乎零成本**：UE scan 实测**端到端 25 秒**（19:24:25→19:24:50），`timeout 1800` 是历史
触顶事故（`f06c632c` max_tokens 8000→16000）留的宽松上限，不是真实耗时。

## §3 新时刻表

| CST | ET | 作业 | 变更 |
|---|---|---|---|
| 18:30 | 06:30 | `gen_event_symbols.py` | 不动（已在 UE 之前 ✓） |
| **19:40** | **07:40** | **`llm_universe_scan.py scan --da-feed`（UE-1）** | **新增** — 只为 A 路供料 |
| 20:00 | 08:00 | Hermes DA 深度分析（~19min） | **不动** — 读到 UE-1，age ≈ 20min |
| 20:55 | 08:55 | `da_bridge_dingtalk_push.py` | 不动 |
| 21:00 | 09:00 | `premarket_data_check.py` | 不动 |
| 21:05 | 09:05 | `premarket_peer_shadow.py`（峰群）+ 宏观披露线索 | 不动 |
| **21:10** | **09:10** | **`scan && inject && push`（UE-2）** | **不动** — 盘前量价峰值 + 8-K 早晨爆发段全到手 |
| 21:30 | 09:30 | 美股开盘 | |
| 21:35 | 09:35 | playbook 首个入场窗 | |
| 21:40 | 09:40 | `llm_universe_scan.py confirm` | 不动 |

**关键：现有行一行未动，只加一行。** 执行面（B 路）字节级不变。

## §4 落地四处 + 逐处回滚

| # | 位置 | 改动 | 回滚 |
|---|---|---|---|
| 1 | crontab（新第 70 行，插在 21:10 那行之前） | `40 19 * * 1-5 … scan --da-feed`，`timeout 900`（15min < 20min 窗口） | 删该行（备份 `/tmp/crontab.bak-20260909-ue-da-timing`，250 行） |
| 2 | `scripts/llm_universe_scan.py` | `cmd_scan(…, da_feed=False)`；`scan_window` 三态 `midday/da_feed/premarket`；`note` 自述用途；CLI `--da-feed`；dispatch 透传 | 备份 `/tmp/llm_universe_scan.py.bak-20260909` |
| 3 | 同上（两处消费面） | `cmd_score` 与效果报告 `cmd_report` 只统计 `scan_window=="premarket"` ⇒ 同日两行不会**双评/双计** | 同上 |
| 4 | `deep-stock-analysis/scripts/pipeline.py` | ①`_UE_FEED_MAX_AGE_H` **30.0 → 3.0**；②`:288` 那段与现实相反的时序注释改为实测时序 + 实证表 | 备份 `/tmp/da_pipeline.py.bak-20260909`；常量改回 30.0 |

**为何 age 守卫必须一起收（30.0 → 3.0）**：30h 恰好放行"前一交易日 09:10 ET"的行（age 20.9h），
这正是 A 路静默吃陈旧候选的直接成因。收到 3.0h 后：正常 age ≈ 0.3h ✓；**UE-1 失败或被假日守卫跳过
⇒ age 22.8h > 3h ⇒ 显式拒绝并打 `[UE-FEED] 最新 scan age X.Xh > 3h, 跳过`** —— 把"静默降级"
变成"可见拒绝"。周五→周一仍天然拒绝。

**为何 UE-1 跳过 R5 快照**（`if not midday and not da_feed:`）：`decision_id` 与 21:10 跑同键，
UPSERT 会互相覆盖。19:40 的盘前数据比 21:10 薄，若让它先占位、而 21:10 跑失败，
被污染的快照就成了当日 canonical。与 `midday` 的既有处置同理（`:1867` 原注释）。

**下游四处的行选择已逐一核验，全部不受第二行影响**：
- `cmd_inject`（`:2007`）`today_scans[-1]` → UE-2 ✓
- `cmd_push`（`:2882`）`recs[-1]  # 今日最后一轮` → UE-2 ✓
- `cmd_confirm`（`:2996`）先过滤 `scan_window=="premarket"` 再 `[-1]` → UE-2 ✓
- streak 计算（`:1726`）注释即写「今日多次 scan 不重复累加」，按 date 并集合并 → 天然容忍 ✓

## §5 端到端验证（19:24–19:27 CST 实测，非推演）

| 项 | 命令 / 方式 | 结果 |
|---|---|---|
| UE-1 真跑 | `scan --da-feed`（手跑，与 cron 行同命令） | **RC=0，25 秒**，浮现 4 标的：LONG XLE .75 / **SHORT NVCR .68 / SHORT TTAN .62 / SHORT XLV .65**；tier 分布 HARD 2 / CORR 2 / **THESIS 0**；契约违约 0 |
| 落盘行 | 读 `llm_universe_scan.jsonl` 今日行 | `scan_window=da_feed`、`scanned_at=2026-09-09T07:24:50-04:00`、`surfaced=4`、note 自述用途 ✓；今日 scan 行数=1 |
| **A 路真通**（决定性） | 直接调 DA skill `_load_ue_short_candidates()` | **返回 3 条**：US.NVCR(.68) / US.TTAN(.62) / US.XLV(.65)。**修复前同一时刻返回 0** |
| 8-K 新鲜度 | 早跑 clue brief | 命中 06:47–07:14 ET 共 ≥15 条（含 QBTS 1.01 重大协议、NVCR 5.02 CEO 离任、SIG 2.02 业绩）⇒ 早跑并非"无事件可读" |
| 不误评 | `llm_universe_scan.py score` | `[score] 无待评分 scan`，RC=0 ⇒ `da_feed` 行被正确排除（修前会被当 todo 双评） |
| 语法 | `py_compile` × 2（仓内 + 仓外 skill） | rc=0 / rc=0 |

## §6 已知代价（如实登记，不掩盖）

1. **UE-1 缺两个当日键输入**：`峰群`（`data/shadow/premarket_peer_shadow.jsonl` 9/9 记录数=**0**，
   由 21:05 cron 产）与 `宏观`（`data/ue_macro_brief_2026-09-09.json` **不存在**，同 21:05 cron 产）。
   实测**未压垮输出**（仍 4 候选 / 3 SHORT / THESIS_ONLY=0），且 `宏观` 在 9/8 的 UE-2 也是 0
   ⇒ 宏观零边际损失；**峰群是真实损失**（9/8 UE-2 有 8 组）。
   两者缺失都在 `[输入源健康]` 行**可见**，不是静默。
   **下一变量（本批刻意不做，一次一个）**：是否给 `premarket_peer_shadow.py` 加一次 ~19:15 早跑。
   风险已识别 = 该脚本按当日追加记录，两跑可能让 UE 的峰群段**重复行**，需先确认其写入是否按
   (date, group) 幂等，再决定"加早跑"还是"改成早跑单跑"。
2. **UE-1 的盘前量价比 UE-2 薄**（07:40 ET vs 09:10 ET，少 1.5h 累积）。这是设计取舍：
   A 路要的是**事件假设**（8-K/财报/内部人/因果链），B 路要的是**量价确认**；
   两跑各自拿到自己最需要的，而不是一跑折中。
3. **DA 仍看不到当日宏观**（08:30 ET 发布 vs DA 08:00 ET 起跑）。本批未动 DA 时刻（用户指定 20:00），
   登记为独立病灶。

## §7 预注册观察判据（不改任何行为，只读）

- **主判据**：`data/opportunity_scan_{date}.json` 内 `origin=="ue_candidate"` 的计数。
  基线 = 近 4 交易日 {0, 2, 0, 0}。**修后应 > 0 成为常态**；若 9/9–9/11 连续 3 日仍为 0，
  则查三处：①`universe_expansion.da_candidate_feed` 是否被置 false ②`[UE-FEED]` 日志是否报 age 超限
  ③UE-1 是否浮现了 SHORT（无 SHORT 则 A 路本就无料可供，属供给问题非时序问题）。
- **副判据**：UE-1 与 UE-2 的 `surfaced` 数与 tier 分布对比（早跑是否系统性更弱）；
  `[输入源健康]` 行的 `峰群:` 与 `宏观:` 读数（量化 §6.1 的损失）。
- **护栏**：A 路注入上限仍是 `_UE_FEED_CAP = 3`，且 DA 对 UE 候选**独立判方向**
  （`llm_synth.py:736` 的 prompt 段原文即「待验证 — 方向以你的独立分析为准」）⇒
  本修复**不可能**让 UE 单方面决定任何一笔交易，最坏情形是 DA 多分析 ≤3 只标的。
- **样本纪律**：单日不构成结论（宪章第 9 律）。本判据看的是**链路是否通**（0 vs >0 的定性翻转），
  不是绩效。

## §8 总回滚

```bash
# ① 删 cron 行（一处）
crontab /tmp/crontab.bak-20260909-ue-da-timing
# ② 仓内脚本
cp /tmp/llm_universe_scan.py.bak-20260909 scripts/llm_universe_scan.py
# ③ 仓外 DA skill
cp /tmp/da_pipeline.py.bak-20260909 /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py
# ④ 或不改代码的软回滚（A 路整体关闭，既有开关）
#    config/unified_config.yaml → universe_expansion.da_candidate_feed: false（热载）
```

三处改动**均不需重启服务**：UE 是独立 cron 进程；DA 是 Hermes cron 进程；
运行中的 `ftseek-us.service` 不 import 这两条路径的任何一行。

## §9 首日实盘验证结果（2026-09-09 晚，实测非推演）

**§7 预注册判据全部通过：**

| 判据 | 预期 | 实测 | 结果 |
|---|---|---|---|
| UE-1 19:40 触发 | scan 落主 jsonl, scan_window=da_feed | 19:40 触发, ~30s 完成, 浮现 5 (LONG XLE .78 / RKLB .65, SHORT XLV .62 / TTAN .60, SKIP CHWY .30), 0 契约违规 | ✅ |
| DA 消费 UE 候选 | 日志出现 `UE候选注入` | 20:03 启动, `[Pipeline] UE候选注入: +2 (['US.XLV','US.TTAN'])` | ✅ |
| 主判据: bridge 含 origin=ue_candidate | ≥1 | **1** — US.TTAN SHORT score 8.0/10, IC strength=80, 进入当日 5 候选 | ✅ |
| DA 在 21:00 硬deadline 前完成 | bridge 落地 | 20:17 完成 (runtime 14 min), `[Bridge] 写入 opportunity_scan_2026-09-09.json: 5 candidates, 5 instructions写入DB` | ✅ |

中间路径如实登记：XLV 被 Prefilter 以 `ATR=1.69% < 3%`（低波不做日内动量）剔除——
属既有质量闸正常工作，非链路缺陷；XLE/RKLB 未过注入阈值。UE 不能单方面决定交易
（prompt 段「待验证——方向以你的独立分析为准」），TTAN 由 DA 独立深分析后确认 SHORT。

**追加修复：盘前综合预检错峰（2026-09-09 20:18 CST 实施）**

- 病灶：Hermes job `2d69370d5c91`「FTSeek-US 盘前综合预检」原 20:00 与 DA 并发，
  Step 4（今日 Playbook 指令检查）每日必然报「指令数: 0 / 🟡 DA bridge 未生成」假警报
  （证据：`/root/.hermes/cron/output/2d69370d5c91/2026-09-08_20-04-04.md`），且与 DA 抢 OpenD。
- 修复：`hermes cron edit 2d69370d5c91 --schedule "45 20 * * 1-5"`（20:00 → 20:45）。
  仅改 schedule，prompt 零改动（`tests/test_hermes_job_prompts.py` 5 passed）。
  20:45 位于 DA 最差完成时刻（历史最长 33 min → 20:33）之后、21:00 bridge 硬deadline 之前，
  Step 4 从「必假警报」变为「真实的 bridge 落地检查」，且保留 15 min 人工干预窗口。
- 回滚：`cp /tmp/hermes_jobs.json.bak-20260909-precheck /root/.hermes/cron/jobs.json`
  或 `hermes cron edit 2d69370d5c91 --schedule "0 20 * * 1-5"`。
- 其余时刻表裁定（基于新鲜度/信息密度/先后关系三轴，均维持现状）：
  - UE-1 保持 19:40：timeout 900s → 最差 19:55 结束，DA 20:00 启动仍有 5 min 余量；
    再往后挪会吃掉挂死容差，新鲜度增益≈0（盘前信息 20 min 内无实质变化）。
  - DA 保持 20:00：不能晚于 ~20:25（33 min 最长 runtime + 21:00 硬deadline）；
    也不能早于 19:55（UE-1 最差挂死时刻），20:00 是两侧约束的唯一安全点。
  - UE-2 保持 21:10：其消费的峰群 capture 实际在 21:05（09:05 ET）、宏观 brief 21:05，
    先后关系正确；21:10 = 09:10 ET 距开盘 20 min，新鲜度最优。
  - Deepulse 保持 20:30：其产物 `theme_context.json` 的消费方是 playbook 生成链
    （21:00 `run_generate`），非 DA；20:30 → 21:00 顺序正确，无陈旧问题。
