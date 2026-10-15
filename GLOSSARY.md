# FTSeek-US

统一语言表（ubiquitous language）。只收**本仓特有**且引发过事故或高频误用的词；
定义一律路由到权威源，本表不复制判据、不造平行尺子。与 `AGENT.md`、
`docs/ADJUDICATED-CRITERIA.md`、宪章冲突时以权威源为准。新增词条前先问：
是一般工程词（不收），还是本仓领域词（收）。

## Language

### 口径与判据

**口径**:
数据结论绑定的 source_type / 配置版本 / 窗口 / 价格基准 / 统计单位等测量前提。混口径下结论是事故主因（宪章数据口径九律）。
_Avoid_: 数据范围、筛选条件

**判据**:
预注册、期间禁改的裁决标准；生效与历史判据的 SSOT = `docs/ADJUDICATED-CRITERIA.md`（C 类红线）。
_Avoid_: 目标、KPI（OKR 叙事除外）

**cohort**:
按退出配置哈希切出的单版本统计窗（如 oct cohort = `exit-34c7153088a1`，起点 2026-09-17）；窗内换版即 BROKEN，不静默移窗。
_Avoid_: 样本池、窗口（泛称）

**MDE**:
最小可检测效应。跨信号统计结论不带按日聚类 + MDE 不予采信（宪章第 9 律）。
_Avoid_: 置信区间宽度

**净 bps**:
每笔按名义额归一的收益口径（毛 bps − 费 bps），统计一律按日聚类；名义额跨 sizing 变更不可比。
_Avoid_: $ / 笔、每笔盈亏

**hit-rate 价格基准**:
命中率必须标注 entry/open 双基准之一并成对报告；用不可成交价量的命中是基准幻觉（宪章第 8 律）。

### 治理物件

**军规**:
`AGENT.md` 的运维/架构/落库纪律总集（减法 R2 裁决起源）；GLOSSARY 与 AGENTS.md 均不得与其冲突。
_Avoid_: 规范、convention

**宪章**:
`docs/specs/multi-agent-collaboration-charter.md`：角色协议 + 数据口径九律 + 落档强制 + 变更纪律。
_Avoid_: 协作规范

**DEB**:
一事一档的辩论/裁决文档（`docs/debates/DEB-YYYYMMDD-主题.md`）；复核结论必须写回原 DEB，不留在会话里。
_Avoid_: 讨论记录

**RFC**:
决策文档（`docs/decisions/RFC-*.md`）；判据类变更须预注册。
_Avoid_: 提案（口语）

**影子**:
行为变更的先行验证模式。在采影子必须在注册表登记，注册表是唯一 SSOT；未注册在采 = 禁止态。
_Avoid_: 灰度、试运行

**落档**:
结论写入 DEB/RFC/handoff 等持久载体；不落档 = 没发生（宪章 §三）。
_Avoid_: 记录一下（口语）

**handoff**:
`handoff.md`，唯一进度队列；条目最新在上，每条含变更日志与下一步。
_Avoid_: 交接文档（泛称）

### 管线与角色

**Playbook**:
唯一下单与持仓管理管线（入场 `run_playbook_auto_execute` → 出场 `monitor_playbook_sim_exits`）。
_Avoid_: 主管线、策略管线

**主管线**:
`signal_pipeline → order_executor` 遗留链路，已冻结新仓（双重锁定）；`scan_loop` 仍独家驱动复盘/快照/心跳等 housekeeping，不可整体 gate。
_Avoid_: 信号管线（口语）

**三角色制**:
作者 → 辩论方（证伪式）→ 独立复核方（只读复现）；数据判不投票，用户终审。

### 运维机制

**启动冒烟**:
重启后必跑的七断言机器检查（`scripts/startup_smoke_check.py --wait 90`）；「服务已启动」≠ 健康。
_Avoid_: 健康检查（泛称）

**六面零引用**:
归档/删除前的消费面核查：仓内 import / crontab / systemd / Hermes prompt / `.git/hooks` / scheduler 字符串面（军规基线五面 + DEB-20260910 起第六面）。
_Avoid_: 五面零引用（口径过时时的简称）

**消费方登记**:
每个 LLM 输出字段必须登记 live 消费方 + 基数自检 + 量纲声明；无消费方的字段一律删（军规 P7）。
_Avoid_: 字段说明

**无料不推**:
推送默认静默门：待裁/守卫告警/状态变化才推；新增推送必须走 `app/push/` hub，禁直连 notifier。

## Relationships

- **判据** 由 **DEB/RFC** 预注册，红线态登记于判据字典。
- **影子** 到期必须结算回注册表与对应 **DEB**。
- **cohort** 由配置哈希定义，换版即破坏 **判据** 依赖的单版本窗。
- **Playbook** 是唯一下单路径；**主管线** 仅保留研究与 housekeeping。

## Flagged ambiguities

- 「五面/六面零引用」：军规原文为五个调用面；DEB-20260910 起扩第六面（scheduler 字符串面）。统一用**六面零引用**，引用军规原文时按原文。
- 「判据/目标」：判据是预注册裁决标准，目标是 OKR 叙事，不得混用。
