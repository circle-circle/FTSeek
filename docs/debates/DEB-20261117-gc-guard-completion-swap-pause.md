---
title: GC 护栏完成度诊断 + gen1/swap 长停顿复发 + 探针归因反例（不弃用，观测级校准）
status: active
module: cross
related_code: app/observability/gc_guard.py
related_okr: OKR-2026-11
tags: [observability, gc, memory, swap, forensics]
updated: 2026-11-17
---

# 议题：GC 护栏（gc_guard）功能完成度诊断 —— 弃用 / 维持 / 优化取舍 + gen1/swap 长停顿复发登记

> 关联：`DEB-20261103-open-freeze-and-perf-hotfix-review.md`（护栏上线复核，§6.4 本文结算）、
> `DEB-20261103-full-chain-perf-risk-audit.md`（1.45GB 口径待核）、
> `docs/OKR-2026-11.md`（行为变更影子先行；本文 P1/P2 为零交易语义观测件）。
>
> 数据截止：2026-11-17 16:0x CST（= 03:0x ET，休市）。取证来源：`logs/ftseek_us.log` /
> `logs/ftseek_us.log-20261117`、`/proc/3235787/{status,smaps_rollup,stat}`、`ps aux`、
> `systemctl cat ftseek-us`、`tests/observability/test_gc_guard.py` 实跑。
>
> 本文性质：**诊断 + 登记**。P0（本文件 + §6.4 结算 + handoff）已落地，纯 .md 零重启；
> P1/P2/P3/P4 为执行提案，**待用户裁决**，本批未动任何运行时码。

**状态**：待决策（诊断完成；不弃用结论成立；P0+P1 已落地，P2–P4 待裁）

## 0. 结论先行

1. **功能已完成设计使命，不弃用**：gen2 风暴结构性条件已消除（每进程 gen2 恒 1 次=启动显式 collect，零自动 gen2；11-02 式开盘冻结未复发；堆 RSS 60–285MB）。护栏是全仓唯一 GC/堆观测，阈值+freeze 是风暴抑制承载体，删模块=回默认阈值=风暴条件回归。
2. **残余长停顿不是 GC 调参问题，是宿主内存竞争**：交易进程 RSS 仅 ~60MB（宿主 1.6%），3.5GB 宿主被同驻 Hermes(~605MB)/qodercli(~412MB)/codex(~250MB) 吃掉 ~1.3GB，swap 已用 752MB ⇒ 冷页换出 ⇒ GC 扫描缺页长停顿。gen1 每进程才 4–5 次，停顿是缺页_bound 非频率_bound。
3. **两处模块自身校准缺口**（观测级，非交易语义）：① StallProbe 归因提示有已证实反例（在途 GC 不可见于 get_stats 增量）；② trim/RSS 告警门按旧 1.4GB 堆 calibrate，现 RSS≤285MB 全休眠。
4. **治理缺口**：DEB-20261103 §6.4 预注册复核至今未结算（本文 §3 结算 gen2 面 PASS）；21s 盘中 / 11s EOD 两条 gen1 长停顿此前未登记（本文 §2 登记）。

## 1. 背景与触发

用户连续三问：「读取系统日志 [GcReporter]…确认其逻辑和合理性」→「功能是否完成，是否需要弃用或者优化调整，请分析诊断」→「下一步」。本文是诊断结论的落档 + 执行提案排队。

护栏生于 2026-11-02 09:30 ET 开盘进程级冻结 ~3min（py-spy + SIGUSR1 抓到 `Garbage-collecting`），11-03 复核批修正汇总口径为「启动以来」。

## 2. 证据（本地可复算）

### 2.1 gen1 长停顿复发（两条，此前未登记）

| 时刻 (CST) | ET | 线程 | 停顿 | gen | collected | RSS | 现场 |
|---|---|---|---|---|---|---|---|
| 11-16 23:31:22,749 | 10:31 盘中 | LLMStrategyLoop | **20972ms** | 1 | 2159 | 130MB | chase_cap 重评循环中（23:30:56→23:31:22 日志空窗 26s） |
| 11-17 05:01:32,740 | 16:01 EOD | LLMStrategyLoop | **11322ms** | 1 | **0** | 145MB | factor_analyzer "Analyzing 900 instructions"（05:01:19,864）期间 |

- 载体：`logs/ftseek_us.log-20261117:3716`、`logs/ftseek_us.log:490`。
- 窗口对账自洽：11-16 23:37 汇总「本窗口停顿 1 次/最长 20972ms/gen1」= gen1 4→5 次、396→2555 回收（+2159=WARNING collected）；11-17 05:07 汇总「16 次/11322ms/gen1」= gen0 78→93(+15)+gen1 4→5(+1)，gen1 回收 396 不变（=collected 0）。
- **collected=0 却停 11s** + 停顿后 RSS 145→285MB（05:07 汇总）= GC 把换出页触回 ⇒ 时间花在缺页不是回收（与 gc_guard.py `release_free_memory` 注释假设一致）。
- 风险权重：21s 冻结在盘中，若撞止损出场窗口会延迟出场最多 21s（GIL 独占，出场快循环同冻）。非日志卫生。

### 2.2 StallProbe 归因反例（已证实）

`logs/ftseek_us.log-20261117:3715`：23:31:22,**489** 探针报「采样间隔 22.1s…gen0=+0 gen1=+0 gen2=+0…**若 gen 全为 0 则停摆非 GC 所致**」；**260ms 后**同进程 :3716 GC WARNING 实证恰是 20972ms gen1。
机制：`gc.get_stats()` 只计**已完成**回收；采样时在途的长 GC 不计入增量（回调测 start→stop 期间 GIL 可在 finalizer/weakref 阶段短暂释放让探针插队）。⇒ 提示在「在途 GC」情形说反。模块自有 `_stack` 可判别在途 GC，探针未读它。

### 2.3 宿主内存竞争（根因候选）

- 当前进程 MainPID **3235787**（09:12:35 CST 起）：VmRSS≈59MB / **VmSwap≈62MB** / VmSize≈782MB / majflt=6967。
- 宿主：Mem total 3563MB / **swap used 752MB** / swappiness=0。
- `ps aux --sort=-rss` 头部：Hermes gateway ≈605MB(16.9%) / qodercli remote-control ≈412MB(11.5%) / codex app-server ≈156MB / codex ≈93MB / FutuOpenD ≈93MB / AliYunDun ≈73MB / **ftseek-us ≈58MB(1.6%)**。
- ⇒ 交易进程是最小住户之一；~1.3GB 被同驻 dev agent 占据。GC 调参/trim 治不了物理约束。

### 2.4 完成度逐项（对照模块四目标）

| 目标 | 判定 | 证据 |
|---|---|---|
| 削风暴条件（阈值+freeze） | ✅ 完成 | 178x 复算一致；gen2 恒 1；开盘无冻结；RSS 60–285MB |
| 自我归因 | ⚠️ 基本完成 | WARNING+汇总正常；探针反例 §2.2；reporter 缺 VmSwap/majflt（swap 主线日志里看不见） |
| 归还空闲堆（trim） | ❌ 休眠 | 门 RSS≥600MB vs 峰值 285MB，零「归还空闲堆」行；2200MB 告警门同休眠；systemd MALLOC_ARENA_MAX=2 已限 arena |
| 测试/消费面 | ✅ 健康 | test_gc_guard.py 8 passed；open_health_report.py:76-95 活消费（子串 regex）；get_status() 仅测试消费 |

### 2.5 配置与消费面

- `systemctl cat ftseek-us`：`MALLOC_ARENA_MAX=2`；`.env.us`/config 无 FTSEEK_GC_* 覆盖 ⇒ 全默认（tune/monitor/trim on，门 600/2200MB，探针 5s/15s）。
- 活消费方仅 `scripts/open_health_report.py`（解析「[GC] 10min 汇总」「[GC] 停顿」「gen2 全堆扫描开始」子串）；**不解析「冻结取证」行** ⇒ P1 改探针文案不破解析器；P2 改汇总行须**追加**保持现有子串。

## 3. DEB-20261103 §6.4 结算

预注册判据：「开盘洪峰期 gen2 停顿与 RSS 不复现 11-02 的 7259ms 形态」。
- **gen2 面：PASS**。11-03 至今每进程 gen2 恒 1（启动 collect），开盘段零自动 gen2、零 gen2 长停顿；RSS 开盘段 ≤285MB，远低 1.45GB（该口径另见 full-chain-perf-risk-audit，判为虚拟地址）。
- **新面 opened（不属原判据）**：长停顿以 **gen1/swap 缺页**形态复发（§2.1），非开盘洪峰、非 gen2。原判据文字未被违反，但其「不复现长停顿」意图在新机制下未达成 ⇒ 转本文 P1/P2 + 宿主级根因登记。
- §6.1–6.3 不在本文结算范围（仍待盘中复核）。

## 4. 取舍：弃用 / 维持 / 优化

- **弃用：否决**。理由见 §0.1。任何部件（含休眠 trim）退役须先走 six-surface-zero-ref 六面审计；当前无退役理由，trim 反是最便宜可调杠杆。
- **再抬 gen1/gen2 阈值 / 禁 gen1：否决**。频率已极低（4–5 次/进程），停顿缺页_bound 非频率_bound；抬阈值反积中命对象，治不了缺页。
- **维持 + 观测级校准：采纳**（P1/P2），宿主级根因另交运维裁（P4）。

## 5. 执行提案（待裁，一次一变量）

- **P0（已落地，本批）**：本文件 + DEB-20261103 §6.4 结算 + README 索引 + handoff 条目。纯 .md，零重启，零 app/ 改动。
- **P1（已实施 2026-11-17，观测级）**：探针读 `_stack` 快照，非空即报「停摆期间 GC 进行中 gen=X 已 Yms（get_stats 只计已完成，在途不计增量）」替代「非 GC 所致」误导提示。抽 `_build_stall_message(elapsed, interval_s, d_collections, stack_snapshot)` 纯函数便于 hermetic 测试（在途/空栈/已完成增量三例）。注意 `_stack` 存 perf_counter、探针用 monotonic，在途时长用 perf_counter 算。零交易语义。**部署**：MainPID 3260934（16:33:43 CST），冒烟 7/7，gc_guard+round_timer 20 passed（含新增 3 例），重启后 0 ERROR、8 sim 仓恢复、源码 mtime 16:32:44 早于进程启动；探针文案不在 open_health_report 解析面，不破解析器。**未提交**（待裁）。
- **P2（观测级，与 P1 分批）**：reporter 行**追加** `VmSwap=…MB`（可加 majflt 增量），保持 open_health_report 现有 regex 子串不破；RSS 告警门 2200MB 重校准或改 VmSwap 门。
- **P3（纯 env 可逆，需前后观测）**：`FTSEEK_GC_TRIM_RSS_MB` 降至 ~150–200MB 让 trim 真触发。预期管理：trim 归还空闲页、缩小未来换出压力，但消不了已在 swap 的活页停顿；MALLOC_ARENA_MAX=2 下收益有限。
- **P4（运维裁，非代码）**：宿主内存竞争根因——dev agent（Hermes/qodercli/codex）迁出交易宿主 / 加内存 / EOD 重批错峰。收益最大，超出 gc_guard 范围。

## 6. 验证项

- P0：`grep -n "20972\|11322" logs/ftseek_us.log*`、`grep -n "非 GC 所致" logs/ftseek_us.log-20261117`、`awk '{print $12}' /proc/3235787/stat`、`ps aux --sort=-rss | head` 可复算本文数字。
- P1：✅ 新增 3 例 hermetic + 既有 8 例不回退（gc_guard 11 + round_timer 9 = 20 passed）；✅ 重启后冒烟 7/7（MainPID 3260934）；⏳ 盘中实证探针在途归因（需一次自然长停顿或注入，休市无自然样本，未验证）。
- P2：open_health_report.py 实跑解析不回退（子串兼容契约例）。
- P3：trim 触发后「归还空闲堆」行出现 + RSS/VmSwap/停顿前后对照。

## 7. 边界声明

- 本批（P0）**零运行时码改动、零重启、零交易语义变更**；只落档。
- P1/P2 为零交易语义观测件（同 11-03 复核批类别），但仍按一次一变量分批 + 落档 + 测试 + 重启冒烟；**未**经用户裁决前不动 app/。
- 部署 P1 须先核 `git status --porcelain -- app/ scripts/`：当前 scripts/ 有另 agent 未跟踪 WIP `verdict_outcomes_ledger.py`（混合署名树）⇒ 重启须单独裁文件切分，不许顺带上线。
- 本文数字均来自本地可复算来源；未引用未复核外部结论。

## 8. 讨论记录

- 2026-11-17：诊断方（本会话）按 AGENTS.md 必读顺序读治理文件，逐条取证（§2），结论 §0；P0 落档。P1–P4 待裁。
- 2026-11-17（P1）：用户「继续」⇒ 实施探针在途归因修正（`_build_stall_message` 纯函数 + `_stack` 快照判别），3 例契约测试，重启冒烟 7/7 部署（MainPID 3260934）。P2–P4 仍待裁。
