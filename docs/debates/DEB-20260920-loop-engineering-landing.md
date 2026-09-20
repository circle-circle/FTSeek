# DEB-20260920 · Loop Engineering 落地方案与执行记录 [RFC-091]

> **状态**: **候选 B 已落地入库** (commit `7b1d2d550` / `1329832af` / `e4d438d6a` + 本轮按需源语义批) · 独立复查完成 (异模型, verdict=确认, defects=0, 见 RFC-091 §11) · **候选 A 未启动** (闸: 本环观察窗至 2026-10-04)
> **日期**: 2026-09-20
> **作者**: Hermes
> **目标读者**: ①项目所有者 (裁决) ②执行 agent (续作候选 A) ③独立复查 agent (复查清单=§9)
> **关联**: `docs/decisions/RFC-091-loop-engineering-meta-audit-loop.md` (实现与验收判据)、`docs/CONTROL-LOOP-LEDGER.md` §2.5、`docs/ADAPTATION-CONTROL-LAW.md`、`DEB-20260919-review-decision-support-system.md` (RFC-088, 影子裁决侧消费者)
> **外部输入**: 《用 Loop Engineering 搭一套会自己进化的量化交易系统》(用户提供) — 本仓只取其**可验证的部分** (触发器/状态文件/验证器/停止条件/权限分层), **明确不采纳**其"无人值守下单"与"自动进化"两节
> **变更类型**: L1 (观测/归宿层; 零交易行为变更; 不读 DB、不连富途、不改配置)

---

## 1. 问题陈述: 要自动化的是哪一个 loop

用户口径 (2026-09-20): 交易 loop 已自动化, 但**调优 meta loop 仍是"用户 + agent + 对话"** —— 每轮从零起意、
经验不跨轮 (压缩即丢)、多 agent 并行改仓互踩。

| | 交易 loop (数据→信号→验证→执行→风控) | 优化 loop (复盘→归因→出候选→裁决→落地) |
|---|---|---|
| 现状 | **已自动化** (crontab + scheduler + 五阶段齐全) | **就是"用户 + agent + 对话"** — 文章说的"你本身就是那个循环" |
| 痛点 | 基本没有 | 每轮从零起意、靠提问驱动、经验散在会话里、多 agent 并行互踩 |

⇒ 要自动化的是**后者**, 即 RFC-088 判据清单的"谁自动去消费它并迭代"那一半。

## 2. 六部件映射 (外部框架 → 本仓实物)

| 部件 | 本仓现状 (落批前实测) | 落批后 |
|---|---|---|
| ① 触发器 | crontab + Hermes cron 已齐; `shadow_registry.get_ready_to_evaluate()` 是"条件达成"雏形 | 新增 no_agent job `fcc7301f2e47` (`20 7 * * 2-6`) |
| ② 技能手册 | Hermes 侧 ftseek-* 技能 / repo 侧 165 份 DEB + `lessons` 表 285 行 (三处分散) | 新增 `ftseek-loop-engineering` (入账清单 + 三层权限 + finding 生命周期 + 四条实测反模式) |
| ③ 状态文件 | 只有散落 push_state / risk_state.flag / dedup json; **无 loop 级 STATE.md** | `data/loops/meta_audit/{STATE.md,findings.json,findings_history.jsonl}` |
| ④ 验证器 | 有 review_orchestrator + "另一 agent 复查" (ftseek-verification-audit), 但**临时发起** | 契约钉 14 例 + **默认必经**的异模型独立复查 (本轮 glm-5.2, 与生成侧不同族) |
| ⑤ Worktree | 已在用 (worktree + hash-object 只注入自己 hunk), 但未制度化 | 沿用 (本批只提交自己 5 个文件, 与他 agent WIP 零接触) |
| ⑥ 连接器 | 终端/DB/推送/MCP 齐; 实盘下单在 `ALLOW_REAL_TRADING=false` + `allow_live=false` 下**天然是死的** | 沿用 — 该"死"正是纪律的自然闸: 本类 loop **只能出提案**, 不能下单 |

## 3. 权限三层 (与交易纪律对齐; 这是设计的主约束)

| 层 | 允许无人值守 | 依据 |
|---|---|---|
| **L1 只读** | 拉数/算指标/出判决书/出告警 — **全自动** | 零风险; 本环全部动作在此层 |
| **L2 SIM 内** | 重跑/落库/起影子 | 不动真钱 (SIM 期授权) |
| **L3 实盘/改策略** | loop **只能产提案 + 预注册判据**, 人裁 | 双锁已在 (`ALLOW_REAL_TRADING` / `allow_live`) |

**凡裁决点一律留给人**: 改参数/改门/改链/任何实盘动作/删产出。
**明确不采纳**: ①"loop 无人值守直接下单" ②"loop 自己改自己" (本环不得修改自身之外的代码 — 未经裁决的自我进化)。

## 4. 文章的坑 → 本仓的具体形态

| 文章警告 | 本仓形态 | 本批对策 |
|---|---|---|
| 没有真停止条件 → 静默坏掉 | 死门 / "agent 自报完成" (~25% 误报前科) / 声称必核 | 停止条件四判据写进 STATE.md, **任何 agent 可只读复算**; 打印时点名为假的是哪一条 |
| 验证器必须独立 | 别让同一会话既生成又验证 | 异模型复查 + 14 例契约钉 |
| loop 之间会打架 | worktree + 文件锁 + 单写者; 地面真值永远是 DB 不是文档 | 本批单写者; 引用结论必回读实物 |
| loop 会自己改自己 | 新增元风险 | L1 边界钉 (只写自己 loop 目录, 源码禁 `app*`/`futu`/`sqlite3`) |

## 5. 候选 A / B 与选择 (用户裁「按建议执行」= 采纳作者倾向)

- **候选 B (审计/守卫环, 纯 L1)**: ✅ 本批落地 — 零风险, 顺手验证"触发器/状态文件/验证器/心跳"骨架。
- **候选 A (复盘→候选→预注册优化环)**: ⏸ 未启动, 闸 = 本环观察窗 (至 2026-10-04) + 届时接 RFC-088 的 `bundle_*.json` / `pending_adjudications`。
- **不做**: 多 loop 并行、自动进化到实盘 (须先有 L1/L2 跑稳 + 裁决协议固化)。

## 6. 成本与风险 (实话说)

- **Token**: 每 loop 只带自己那份 STATE + 手册 (本环每轮读 4 个 JSON + 自己状态, 无 LLM 调用 — no_agent 载体)。
- **误报成本**: 用户注意力才是新瓶颈 ⇒ 无状态变化一律静默; 每个 finding 带 evidence 原文。
- **静默死亡**: 每轮落 `logs/meta_audit.log` (`[carrier …] rc/dur/out_lines` 头) — "没推送"≠"没跑", 心跳可外部回读。
- **最容易被高估的一点**: 迭代的产出是"更可信的判据"而不是"更多的改动"。瓶颈从来不是改动速度, 是有没有 edge — loop 要追求的是**把 edge 找出来并证伪得快**。

## 7. 首轮实测读数 (2026-09-20)

- 4/4 探测器在册 (按需源见 §8) · 未决 **10** = 4 死门 (归用户裁决, decide_by 2026-10-04) + 1 watchdog + 5 invariant · `LOOP_DONE=True`
- 关 2 项并留证据: `config_census:*` 按**按需源语义**关闭 (非"已修好")
- 首轮抓到的真问题: 台账 §2.5 D3 行声称的挂载**从未存在** ⇒ 已更正 (详见 §8)

## 8. 处置记录: 「如果无用就退役」的实测裁定

用户口径 (2026-09-20): 「如果无用就退役」。**先实测, 再裁**:

| 问题 | 实测 |
|---|---|
| 判定有用吗 | **有**: 有活消费者 = `tests/test_config_key_reverse_census.py::test_real_repo_has_no_new_invisible_switch` (每次全量回归在真仓上跑门禁) |
| 缺什么 | 缺**周期挂载**, 不是判定; 台账原声称的 cron 从未存在 |
| 该补 cron 吗 | **不补** (做减法)。要周更读数时一行开关即得: 周日正向普查行末 `&& … config_key_reverse_census.py --gate` |

**由此产生的设计修正**: 把"按需工具"当"周期传感器"是错的 —— 会给用户每天报一条"探测器停更"假阳。
引入**按需源语义** (第 5 列写消费者路径): ①产物陈旧不判失明, 陈旧读数不作 live finding; ②只有**消费者消失**才报失明;
③该源 `detector:*` finding 在消费者复活时关闭 (首版只认周期源 ⇒ 该 finding **永不收敛**, 已修)。

**建设期共抓出 6 个真缺陷** (全部靠实跑/契约钉, 非审查推测): ①周日误报 3 源 (阈值按日频直觉) ②静默契约 hash 含时间戳
③停止条件漏"逾期" ④判据标签与取值位置错位 ⑤5 元组改造后 main() 仍 4 元组解包 ⇒ 环崩溃 (被自身异常路径如实报成 rc=2)
⑥按需源 detector finding 永不收敛。

## 9. 启用后的呈现形式 (用户问: "如果启用未来是什么形式展现")

**日常形态 (已上线, 即日生效)**: 每个交易日 (CST 周二~周六) **07:20** 跑一轮, 只有**状态变化**才推一条 ≤12 行摘要到本会话:

```
[meta_audit] 2026-10-05 07:20 | 探测器 4/4 在册 | 未决 10 (新 1 / 关闭 0 / 逾期 1)
[meta_audit] LOOP_DONE=False | ①owner+decide_by 齐备=True ②无逾期未裁=False ③CLOSED 全带回读证据=True ④探测器产物全新鲜=True
[meta_audit] 🆕 gate_liveness:xxx | 死门/陈旧门: xxx | owner=用户(裁决) decide_by=2026-10-19
[meta_audit] ⏰ 逾期未裁 gate_liveness:short_squeeze | decide_by=2026-10-04 | owner=用户(裁决)
[meta_audit] ✅ 关闭 invariant:W6_except_stack_grew | 源侧回读: invariant 当日产物不再报该项 …
[meta_audit] 状态: /opt/ftseek/FTSeek-US/data/loops/meta_audit/STATE.md
```

**升级阶梯 (何时会更响)**: ①**静默** = 无变化 (不推); ②**摘要** = 新增/关闭/逾期/探测器在册状态变化;
③**到期裁决卡**: `decide_by` 到期当天, 该 finding 以 `⏰ 逾期未裁` 常驻摘要直到被裁 (不会自行消失);
④**候选 A 落地后**: 判决书卡 (提案 + 证据 + 复现命令 + 预注册判据), 仍**只提醒不落地**。

**随时可查**: `data/loops/meta_audit/STATE.md` (人读表格: 未决项/owner/decide_by/证据 + 已关闭项的回读证据 + 刻意不做的边界)。

## 10. 复查清单 (给下一个独立复查 agent)

1. 契约钉 + 台账守卫 + 对账守卫实跑 (`tests/test_meta_audit_loop.py` 等), 报真实通过数。
2. 自建临时仓证伪: 停更源+项消失→必须 `UNVERIFIABLE`; 逾期→`LOOP_DONE=False`; 连跑两轮第二次静默; 文件越界检查。
3. 台账一致性: `grep fcc7301f2e47 docs/CONTROL-LOOP-LEDGER.md`; D3 更正与 `crontab -l` / `jobs.json` 实物相符。
4. 本 DEB 与 RFC-091 里每处"实测/✅"能否指到当次命令与返回值; 逐条列出说了但没做到的。
5. 复算首轮读数: `data/loops/meta_audit/findings.json` 未决数 = 4 死门 + 1 watchdog + 5 invariant。

## 11. 裁决记录

| 日期 | 用户口径 | 落地 |
|---|---|---|
| 2026-09-20 | 「按建议执行」 | 起候选 B (纯只读审计环) 试点 + 六部件映射 + 三层权限; RFC 落 `RFC-091` (编号顺延, RFC-089 已被 ADF 周检占用) |
| 2026-09-20 | 「如果无用就退役」 | D3 反向配置普查: 实测**有活消费者** ⇒ 判定不退役, 退役"当周期传感器"的假设; 不补 cron; 环侧按需源语义接住 |
| 2026-09-20 | 「请将 Loop Engineering 的内容写入 DEB」 | 本文 (含六部件/权限/坑映射/成本/呈现形式/复查清单) |