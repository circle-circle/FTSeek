# RFC-114: 已裁结论证据锚点守卫 (ADJUDICATED note evidence anchor)

> **状态**: 已执行 (2026-09-27, 用户裁「按建议执行」; 执行记录+自曝见 §8)
> **类型**: L1 (1 个新守卫测试 + ≤12 条 note 追注; 零交易行为变更, 零 schema 变更)
> **来源**: 2026-09-27 会话「Muse 主动思考机制对照」第七节 🟢 三条的落地评估
> **前置**: RFC-092 (ADJUDICATED 登记机制) / RFC-109④ (清账批「每项都是本批实跑」纪律) /
> tests/test_criteria_registry_visibility.py (同族可见性守卫先例)

---

## 0. 提案范围更正 (普查推翻原问题陈述, 显式登记)

原会话第七节列了 🟢 三条「值得搬」: ①证据引用强制化 ②claim 生命周期 ③浮现准入门。
动笔前按 review-system-design P0 普查实测, **②③ 已基本落地, 本 RFC 唯一增量是 ①**:

| 原提议 | 实测现状 (2026-09-27) | 判定 |
|---|---|---|
| ② claim 生命周期 (evidence/confidence/status/supersedes) | `ADJUDICATED` 27 条已带 read_at / recheck_after / needs_user; `ShadowItem` 已带 status 五态 + verdict_history + cross_ref + no_adjudication_reason; RFC-092/109 两轮清账已把「19 READY 无人裁」收敛到 needs_user=True 仅 1 条 (top_movers, 闹钟 9/30) | **已落地, 不做增量**。缺的 confidence / supersedes_claim_id 属减法式否决: 判据是硬门槛非概率, confidence 无消费面; verdict_history 即取代链。登记理由防重提 |
| ③ 浮现准入门 (默认静默, meaningfully new 才上浮) | `scripts/pending_adjudication_report.py` = no_agent 静默载体: 待裁面**无变化零输出**, 变化才推; NEEDS_USER/AUTO_WATCH/WINDOW 三分类; `app/review/criteria_status.py` 逾期 14 天双门槛升级; 白话词典 SSOT (`shadow_lexicon.py`); cron 已挂 (周六 09:15, enabled=True, jobs.json 实测) | **已落地, 无增量**。Muse「值得打扰吗」门的同构物已在役 |
| ① 证据引用强制化 (每条结论强制附证据引用, 无引用不进精选表) | `bundle_schema.py` 的证据契约只覆盖 review bundle 指标, **不覆盖 ADJUDICATED**; note 是自由文本, 实测 **8/27 无任何锚点 (文档/实跑双缺), 4/27 缺文档锚** | **真缺口, 本 RFC 范围** |

⚠️ 原会话说「你的 19 条 READY 无人裁决, 根因之一是结论没有生命周期」——该前提已被
RFC-092/109 两轮清账部分证伪 (READY 面已收敛, 生命周期字段已建)。本 RFC 不引用旧说法。

---

## 1. 底牌清单 (全部实测, 命令+输出见 §1.5)

| # | 事实 | 锚点 |
|---|---|---|
| T1 | ADJUDICATED 27 条, value 键仅 {read_at, recheck_after, needs_user, note}, note 为自由文本 | probe_registry.py 实跑 |
| T2 | 8/27 note 无文档锚且无实跑锚: entry_delay_confirm / exit_disable_cf / extreme_gap_ban_shadow / gap_alignment / intraday_loss_breaker_a_prime / kr3_direction_runup_gate / futu_channel_ofi_baseline / eth_afterhours_exit_shadow | probe_anchor2.py 实跑 (正则口径见 §1.5) |
| T3 | 4/27 有实跑锚但无文档锚: futu_channel_capital_flow / exit_param_shadow / eth_premarket_exit_shadow / short_funnel_shadow | 同上 |
| T4 | T2 的 8 条全部出自 RFC-092 §5「未改动项」表 (9/21 清账批), 锚点可回填 | RFC-092 原文对照 |
| T5 | 既有同族守卫先例: test_criteria_registry_visibility.py (判据字典↔注册表可见性, 2026-09-26 立项, 起因是 B21 达标一个月无人读) | 该文件 docstring |
| T6 | test_pending_adjudication_report.py:121 已有幽灵守卫 (ADJUDICATED id ⊆ REGISTRY id), 无锚点检查 | 源码实读 |
| T7 | RFC-092 纪律原文「每项都是**本批实跑**得出的读数 (非引注册表状态/🔔 横幅)」——写在 ADJUDICATED 注释里, **无测试强制** | shadow_registry.py:238-240 |
| T8 | 现役无锚趋势会增长: 每轮清账批新增 5-10 条登记 (RFC-092 批 15 条 + RFC-109④ 批 9 条) | git log |
| T9 | shadow_registry.py 当前有他 agent 未提交 WIP (ext_entry_plan count_mode 扩展) | git status 实测 |

### 1.5 普查原始输出 (可复现)

```bash
# 锚点正则口径 (与拟落地守卫一致):
#   文档锚: RFC-\d+ | DEB-\d+ | ADR-\d+ | docs/ | 判据字典 | §
#   实跑锚: eval | \.py | 实跑 | 重跑 | report | 回放
cd /opt/ftseek/FTSeek-US && venv/bin/python - <<'EOF'
import sys, re; sys.path.insert(0, ".")
from app.playbook.shadow_registry import ADJUDICATED
doc = re.compile(r"RFC-\d+|DEB-\d+|ADR-\d+|docs/|判据字典|§")
run = re.compile(r"eval|\.py|实跑|重跑|report|回放")
for k, v in ADJUDICATED.items():
    n = v.get("note", "")
    if not doc.search(n) and not run.search(n): print("BOTH_MISSING", k)
    elif not doc.search(n): print("DOC_MISSING", k)
EOF
# 2026-09-27 实跑输出: BOTH_MISSING ×8, DOC_MISSING ×4 (名单见 T2/T3)
```

```
# 注册表面 (probe_registry.py):
status: {'GRADUATED': 2, 'KILLED': 13, 'OBSERVING': 44, 'SUPERSEDED': 2} total: 61
ADJUDICATED entries: 27 | needs_user=True now: ['futu_channel_top_movers']
ADJUDICATED value keys: {'read_at': 27, 'recheck_after': 27, 'needs_user': 27, 'note': 27}
```

---

## 2. 设计树自问自答

**Q1 病灶是什么?**
A: 「已裁结论」是本系统裁决面的精选表 (等价 Muse memory/bank), 但入表无证据门槛。
8/27 的读数只能靠 note 文本转述, 无法程序化回源复核 → 违反 MEMORY 纪律
「引用结论必 grep 原文, 找不到=自造叙事」, 且该纪律目前只约束人, 不约束登记表。

**Q2 危害是真实的还是假想的?**
A: 真实且有同型先例: T5 守卫的立项理由就是「B21 判据达标一个月从未被读过」——可见性缺口
已经实锤过一次; 本缺口是它的镜像: **读数被引用但无法回源**。RFC-113 同日自纠
(撤回「两条独立证据链一致」overclaim) 也是同族——结论强度超出证据可追范围。

**Q3 为什么不用 bundle_schema 的 Evidence dataclass 直接套?**
A: 那是 review bundle 指标的契约 (kind/ref/params/integrity 四键, 面向可复算 SQL/产物)。
ADJUDICATED note 的载荷是「判据实测读数 + 处置」, 已含数字与结论; 再套一层结构化
Evidence = 27 处迁移 + 双份真相 (note 说一套 evidence 说一套) = 违反「不新增台账」。
锚点正则校验文本即可达到「可回源」目的。→ 影响: 选方案 A (文本锚守卫), 弃方案 B。

**Q4 方案对比?**
A: 三案:
- **A. 文本锚守卫 (推荐)**: 新测试扫 ADJUDICATED, 每条 note 须命中 ≥1 文档锚;
  存量 12 条白名单豁免 (逐条列名+回填期限), 新登记即生效。零 schema/零运行时变更。
- B. 结构化 evidence 字段: 见 Q3, 否决 (过度设计)。
- C. 不做: 8/27 已失锚且每轮清账 +5~10 条 (T8), 趋势恶化; 否决。

**Q5 (反方①) 这个改动会多赚钱/多出单吗?**
A: **不会**。零交易行为变更, 收益全部在可审计性: 已裁结论可程序化回源、防自造叙事、
给未来 meta-audit 提供可 grep 的证据面。验收时不得用「改善裁决质量」以外的期望衡量。

**Q6 (反方②) 新失败模式是什么?**
A: 两个:
- **假锚点**: note 写了「RFC-092」但指向错误章节/错误文档 → 守卫只测「有锚」不测「锚对」。
  限度写进测试 docstring; 锚的正确性仍靠复核日抽查 (与 Muse「分类器不保证语义正确」同构)。
- **回填造假**: 为过守卫给 8 条失锚 note 编一个看似合理的文档引用 → 比失锚更糟
  (伪造证据)。防线: 回填批次必须逐条贴出被引文档的原文行 (grep 输出进 commit message
  或本 RFC 附录), 找不到原文的显式登记 `UNTRACEABLE_BACKFILL` 豁免而非硬凑。

**Q7 (反方③) 与已证伪/已回滚方案什么关系?**
A: 全仓无「evidence 字段建了又拆」前科 (grep 实测)。同族成功先例 = T5 可见性守卫
(已落地在役)。与 RFC-109⑤「notes 自由文本警告拦不住人 ⇒ 规则必须放字段级」的教训
表面冲突——但该教训针对的是**机器要执行的开关语义** (config_key 停采误判),
本守卫针对的是**人要回源的引用**, 消费者是测试与人, 不是运行时分支, 文本锚足够。

**Q8 守卫放哪?**
A: `tests/test_adjudicated_note_anchor.py`, 与 test_criteria_registry_visibility.py 同族
同目录。不挂 cron (CI 面即 pytest 全量回归, 已有载体)。不新增任何台账/产物/推送。

**Q9 白名单豁免的形态?**
A: 测试内 dict: `{id: (豁免类型, 理由, 回填期限)}`。豁免类型二值:
`PENDING_BACKFILL` (可回填, 带期限) / `UNTRACEABLE_BACKFILL` (原始实跑记录未入库,
永久豁免但留名)。空白名单 = 全部有锚。豁免表在测试文件里而非注册表里
(注册表是交易治理 SSOT, 不装测试基建状态)。

**Q10 为什么强制「文档锚」而不是「实跑锚 or 文档锚」?**
A: 实跑锚 (「9/26 实跑 exit_shadow_graduation」) 指向脚本, 但脚本输出不入库则仍不可回源;
文档锚 (RFC/DEB 章节) 是入库产物, git 可追。T3 的 4 条有实跑锚缺文档锚, 回填成本极低
(它们的实跑记录就在 RFC-109 里)。严格版 = 文档锚必须; 实跑锚不强制 (note 里的读数
本身就是实跑产物, 再强制重复无增量)。

**Q11 2 个无 eval_script 的活跃项 (reentry_quick_tp_gate / short_chase_ban_shadow) 归本守卫管吗?**
A: 不归。它们缺的是**执行体**, 属既有 `no_adjudication_reason=NO_EVAL_SCRIPT` 挂账轴
(RFC-092 引入)。两个轴不混: 本守卫只管「已裁 note 可回源」。→ frontier F2 留用户确认。

**Q12 与并行 agent 的冲突面?**
A: 实施批要改 shadow_registry.py (note 追注), 该文件现有他 agent WIP (T9)。
纪律: worktree + hash-object 按锚点注入自己的行, 提交前查 staged, 不碰他人 diff
(MEMORY 并行改 repo 五条)。新测试文件无冲突面。

**Q13 守卫会不会拦住合法的紧急登记流程?**
A: 可能: 清账批当天实跑了判据但 RFC 还没写完 → note 暂无文档锚 → CI 红。
出口: 豁免表加一行 `PENDING_BACKFILL` 期限 7 天 (先登记后补锚), 与「先读数后文档」的
真实工作流兼容。这不是弱化守卫, 是把宽限期显式化、留名、可追。

---

## 3. 模块线索表

| 模块 | 生产者 | 消费者 | 工件 | 幂等键 | 失败模式 | 回滚 |
|---|---|---|---|---|---|---|
| test_adjudicated_note_anchor.py (新) | 本 RFC 实施批 | pytest 全量回归 | 无 (纯断言) | — | 假锚点放行 (Q6-a, 限度已声明) | 删文件 |
| ADJUDICATED note 追注 | 回填批 (≤12 条) | pending_adjudication_report 推送面 / 未来 meta-audit | shadow_registry.py diff | note 只追加不改判据字段 | 回填造假 (Q6-b, 防线=贴原文) | git revert 单 commit |

接线点唯一: 守卫读 `from app.playbook.shadow_registry import ADJUDICATED`, 无第二消费者。

## 4. 历史坑映射

| 历史坑 | 本设计如何规避 |
|---|---|
| 「notes 自由文本警告拦不住人」(RFC-109⑤) | 本守卫消费者是 CI 不是运行时, 文本锚 + 测试红 = 结构性拦截; 机器执行的语义仍走字段 (不混轴, Q11) |
| 「引用结论必 grep 原文, 找不到=自造叙事」(MEMORY) | 回填批每条贴被引文档原文行; 找不到的进 UNTRACEABLE 豁免, 禁止硬凑 |
| 「文档声称已设 cron 必须实测载体」(2026-09-08) | 本 RFC 状态段所有「已落地」声称均附 jobs.json/源码实测锚 (§0 表) |
| 「改判据=重新裁决」(ADJUDICATED 纪律) | 反判据 (§6 阶段 1): diff 面出现任何 graduation_criteria/status/判据词变更 = 超范围回滚 |
| 共享文件并行提交 (MEMORY 五条) | Q12: worktree + hash-object 注入, 提交前查 staged |

## 5. 待裁问题 (frontier)

| # | 问题 | 推荐 | 一行理由 |
|---|---|---|---|
| F1 | 回填范围: 8 条双缺锚, 还是 12 条 (含 4 条仅缺文档锚)? | **12 条全量** | 4 条的回填源就在 RFC-109, 边际成本≈0; 半量回填会留下「同类 note 两种标准」 |
| F2 | 无 eval_script 的 2 活跃项归本守卫还是既有挂账轴? | **归既有 no_adjudication_reason 轴, 本守卫不涉** | 不混轴 (Q11); 它们尚无已裁 note, 本来就不在 ADJUDICATED 里 |
| F3 | 文档锚是否强制与日期共存 (如「RFC-092 §5, 9/21」)? | **不强制, 只推荐** | read_at 字段已带日期, note 内重复要求 = 装饰性; 减法优先 |

## 6. 分阶段行动方案 (每阶段可测判据 + 回滚)

**阶段 0 — 守卫落地 (先红后绿)**
1. 新测试: 扫 ADJUDICATED 27 条, 无文档锚且不在豁免表 → FAIL; 豁免表初始 = T2+T3 的 12 条
   (PENDING_BACKFILL, 期限 2026-10-11)。
2. 先跑一遍确认测试**对存量红** (证明它有牙), 豁免表填入后绿。
3. 反向测试: 构造一条无锚 note 的临时 dict, 断言守卫抛错 (防恒绿)。
- 判据: `pytest tests/test_adjudicated_note_anchor.py` 绿 + 反向测试红→绿留痕。
- 回滚: 删测试文件 (零运行时接触面)。触发条件: 守卫误拦真实新登记且豁免流程走不通 (Q13 出口失效)。

**阶段 1 — 存量回填 (12 条, 单独 commit)**
1. 4 条 DOC_MISSING: 追注 RFC-109 对应章节锚。
2. 8 条 BOTH_MISSING: 逐条对照 RFC-092 §5 表回填文档锚; 每条 commit message 或本 RFC
   附录贴被引原文行 (grep 输出); 原文找不到的登记 UNTRACEABLE_BACKFILL + 理由。
3. note 只**追加**「(读数出处: RFC-0XX §Y)」段, 判据/结论/数字一字不动。
- 判据 (预注册, 反造假): 回填后 `git diff` 面只含 note 追加行 + 豁免表缩减;
  出现任何 graduation_criteria/status/判据词 diff = 超范围, 整 commit revert。
  豁免表 PENDING_BACKFILL 归零 (或转 UNTRACEABLE 且逐条有理由)。
- 回滚: git revert 单 commit; 触发条件: 任一追注与被引原文不符 (抽查发现即全批回滚重做)。
- 提交纪律: shadow_registry.py 有他 agent WIP → worktree 注入法 (Q12), 提交前查 staged。

**复核日**: 2026-10-11 (豁免期限到期日; 若当日与 top_movers 9/30 结算撞期, 顺延至下一个周六周检批一起裁)。

**本文档落点**: docs/decisions/RFC-114-adjudicated-note-evidence-anchor.md (本文件)。

## 7. 明确不做 (减法清单)

- 不给 ADJUDICATED 加 confidence / supersedes 字段 (§0 ② 判定)。
- 不动浮现准入门 (§0 ③ 已在役, 无增量)。
- 不新建台账/产物/推送/cron (守卫走既有 pytest 面)。
- 不校验锚点语义正确性 (Q6-a 限度, 写进测试 docstring)。
- 不搬 Muse 的每小时校验环: 本系统裁决频率是周/月级, 每日级证据校验无消费面。
---

## 8. 执行记录 (2026-09-27, 用户裁「按建议执行」= F1 全量回填 / F2 不混轴 / F3 不强制日期)

**状态: 提案 → 已执行** (阶段 0 + 阶段 1 同批落地, commit 见 git log `[RFC-114]`)

### 8.1 落地内容

- 阶段 0: `tests/test_adjudicated_note_anchor.py` — 守卫 + 反向牙齿测试, 2 passed。
- 阶段 1: ADJUDICATED 16 条 note 回填回源锚 (RFC-092 §3/§5/§8.1/§8.4 ×9、判据字典 B19/B21 ×2、
  [RFC-109] commit 47f30ff74 ×1、可复跑脚本路径 ×4)。回填后 strict doc 锚 12/27 → **23/27**;
  余 4 条 (capital_flow / exit_param / eth_premarket / eth_afterhours) 靠 script-in-note 锚
  (`可复跑: scripts/*.py`, 文件在盘已验证)。EXEMPTIONS 豁免表 = **空** (零豁免落地)。

### 8.2 自曝: 执行中发现的三处设计修正 (与本 RFC 原案不同, 就地登记不悄悄改)

1. **守卫口径从「doc 锚必须」放宽为「三选一锚」** (doc / script-in-note / eval_script 均在盘可验)。
   动因: 严格扫描 (裸 § 不算 doc 锚) 实测 untraceable=**0/27** — 15 条无 doc 锚的 note 全部有
   注册表 `eval_script` 锚 (脚本在盘 = 读数可复跑重现, 证据强度高于文档指针)。原案「doc 锚必须」
   会迫使给这 15 条补纯装饰性文档引用 (为过守卫而写锚 = Q6-b 回填造假的温和形态)。
   三选一口径守住本质目标「可回源」, 且弱 § 锚 (vwap_gate_ab/futu_channel_iv_hv 仅 "§8.4"/"§8.1")
   已由本批补齐文档名 (RFC-092 §8.x)。
2. **回填量 12 → 16 条**: 原案 12 = 宽松正则 (裸 § 算 doc 锚) 下的 8+4; 严格口径 + 9/26 新登记批
   (eod_depth_gate / eth_exit_forgone_upside 等) 实测扩到 16。F1「全量」语义不变: 全部缺锚项回填。
3. **v1 回填脚本事故 (被预注册验证器捕获, 未入库)**: v1 的 (kind,old,delta) 转换逻辑缺陷把
   16 条 note **整体替换**成锚点串 (判据读数全丢)。阶段 1 预注册判据「diff 面只含追加」的
   语义验证器 (cmp_adj.py: 剥锚后与 HEAD 版逐字比对) 当场红 12 条 ⇒ 整批丢弃, v2 重写
   (纯插入断言 `_is_pure_insertion` + 幂等判前置) 后 16/16 applied、violations=0。
   教训: 「只追加」不能靠 diff 行数看, 必须剥锚后语义比对。

### 8.3 验证清单 (全部实跑)

- 回填纯净性: cmp_adj2.py — 27 键集不变; read_at/recheck_after/needs_user 三字段零变更;
  16 条 note 剥锚后与 HEAD 版**逐字相同** (violations=0)。
- py_compile OK (worktree + 主仓工作区双验)。
- 守卫测试: test_adjudicated_note_anchor (2) + test_pending_adjudication_report (5) +
  test_criteria_registry_visibility + test_criteria_status = 27 passed。
- 回归等基对比 (裸 worktree HEAD vs 回填后, `-k 'registry or adjudicat or shadow or criteria or
  control_loop'`): 基线 FAILED/ERROR 7 = 现在 7, **新增失败 = 0** (comm -13 空)。
  基线 7 条为环境性失败 (裸 worktree 缺 data/), 两侧同数。
- 并行纪律: shadow_registry.py 他 agent WIP (ext_entry_plan count_mode 扩展) 在工作区原样保留
  (grep 实测 `json_key:criteria.trading_days.actual` 仍在); 提交走 HEAD 基线 worktree + blob 注入,
  未 checkout/覆写工作区。
