# DEB-20260919 · 复盘决策支持系统 (Review Decision Support) [RFC-088]

> **状态**: **P0–P4.5 已落码**（§21–§27；P0–P4 已入库于 `4c529f7f6`，P4.5 为本轮工作区交付）· 3 轮独立复查完成（§17 / §19 / §26）· P5（cron+golden+载体+skill）与 §13/§20 待决项**待用户裁决**
> **日期**: 2026-09-19
> **作者**: Hermes
> **目标读者**: ①项目所有者(裁决) ②执行 agent(落码) ③**独立复查 agent(§10 复查清单)**
> **关联**: `RFC-087`(盘前收口条件豁免预注册 — 本设计的**范例产物**)、`RFC-086`(阶段统计 SSOT)、
> `DEB-20260918-extended-hours-ssot-and-observability.md`、`app/review/review_orchestrator.py`、
> `app/playbook/shadow_registry.py`、`app/review/stage_stats.py`
> **变更类型**: L1 (观测/呈现层; **不触碰 `app/playbook/*` 执行链**)。主服务曾于 2026-09-19 21:30 按用户明确指令重启；重启时 DB/内存持仓均为 0，详见 §25.2 S-4。
> **历史实现计划已作废**: `.hermes/plans/2026-09-19_152656-review-system.md` 仍含 `review_criteria.yaml` 与 `dedup_key` 旧案；以本文 §9/§16 为唯一实现口径。

---

## 1. 问题陈述（先摆事实，再谈方案）

用户口径: **"最终目的不是累积数据，而是总结溯源、合理优化、非拍脑袋决策依据；可以按周期，可以统计交易阶段。"**

按此口径逐条实测，问题的形状与"缺一个台账"相反 —— **记录已经过剩，缺的是消费者**。

### 1.1 记录侧：已过剩（实测）

| 维度 | 实测值 | 命令/出处 |
|---|---|---|
| 报表脚本 | `scripts/*.py` **330** 个 | `ls scripts/*.py \| wc -l` |
| cron 活跃行 | **108** 行，其中复盘类 **22** 行 | `crontab -l` 过滤 `review\|scorecard\|report\|settle\|invariant\|lesson\|attribution` |
| jsonl 台账 | 顶层 `data/*.jsonl` **67**；含 `data/shadow/*.jsonl` 共 **108** | `ls data/*.jsonl data/shadow/*.jsonl \| wc -l` |
| json 产物 | `data/*.json` **329** | 同上 |
| 日报产物 | `data/daily_reviews/*.json` **111** | 累计天数 |
| 教训库 | `lessons` 表 **285** 行（含 `evidence` JSON 列） | `select count(*) from lessons` |
| DEB | `docs/debates/*.md` **165** 个 | `ls \| wc -l` |
| 推送 | 当日成功 **54** 条（其中 15 条是"通道心跳"） | `grep -c "推送成功\|send_text 成功" logs/ftseek_us.log` |

### 1.2 消费侧：关键 SSOT **生产消费面不足**（周六有消费者，缺每日与裁决闭环）

`app/playbook/shadow_registry.py` 已经是**影子/判据的单一真源**，具备完整决策要素：

- `ShadowItem` 字段: `id / name / status / description / data_path / config_key / min_n / graduation_criteria / eval_script / created / notes / count_mode`
- 状态机: `OBSERVING → READY_TO_EVALUATE → GRADUATED / KILLED / SUPERSEDED`
- API: `REGISTRY`(**30+ 项**)、`get_observing()`、`get_ready_to_evaluate()`、`refresh_counts()`、`_count_samples()`、`summary()`、`check_mounting()`(cron 挂载核对)、`check_intent_uniqueness()`
- 其 docstring 自陈要解决: *"当前有多少影子在跑 / 各自攒了多少样本、离毕业门禁还有多远 / **哪些已经够样本该裁决了但没人裁** / 哪些已废弃但 cron 还在白跑"*

**实测其消费者**（第 2 轮复查更正）：

```bash
grep -rn "shadow_registry" --include=* . | grep -v "^\\./app/playbook/shadow_registry.py" | grep -vE "\\.pyc|/logs/|\\.git/"
# app/ scripts/ 无程序化调用；ops/hermes/ftseek_weekly_checkpoint.py 周六调用:
#   refresh_counts + summary + check_mounting + check_intent_uniqueness
crontab -l | grep -i "shadow_registry\|check_mounting"     # → 空 (不是 Hermes 载体的证据)
grep -rn "get_ready_to_evaluate\|refresh_counts" app/ scripts/ ops/ | grep -v shadow_registry.py
# → ops/hermes/ftseek_weekly_checkpoint.py:183-185
```

⇒ 判据成熟后**每周六会被报出一次**；但 `refresh_counts()` 只做进程内状态迁移，没有持久 `ready_since/decide_by`，也没有每日汇总、逾期天数或裁决收口。用户仍会被迫在周六之外主动询问，这仍满足本 DEB 要解决的问题，但不能再表述为“零生产消费者”。

> 注：`scripts/okr_2026_09_scoring.py:386-390` 只做 `any(tok in reg_text for tok in ("RFC-072","runup","kr3"))` 的**文本提及**判断，不是程序化消费其状态/计数。

### 1.3 第二个实测症状：整体口径掩盖阶段亏损

用 SSOT `app/review/stage_stats.py`（`refresh` 口径: 已平仓 ∧ `pnl_net` 非空 ∧ 出场日为归集日 ∧ 阶段=`entry_session` 优先、ET 时钟回退）实测 2026-08-01 → 2026-09-18：

```bash
venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from app.review.stage_stats import rows_from_db, stage_breakdown
bd = stage_breakdown(rows_from_db('data/ftseek_us.db','2026-08-01','2026-09-18'))
[print(f\"{k or '(未标注)':<10} n={b['n']:>4} 净={b['net']:>10.2f} 胜={b['wins']}\") for k,b in bd.items()]"
```

| 阶段 | n | 净额(USD) | 胜 | MFE=0 |
|---|---:|---:|---:|---:|
| 盘前 premarket | 4 | **−648.86** | 1 | 2 |
| 盘中 regular | 329 | **+340.14** | 208 | 34 |
| 盘后 afterhours | 2 | +110.75 | 2 | 0 |
| **合计** | **335** | **−197.97** | 211 | 36 |

⇒ **同一个"合计 −197.97"里，盘前 4 笔承担 −648.86、盘中 329 笔 +340.14**。不按阶段拆，任何"优化"都是在错误的池子上调参 —— 这就是"非拍脑袋"必须先把阶段口径变成**默认视角**的理由。

### 1.4 第三个症状：证据**不可复算**（溯源缺失）

本会话中反复发生的真实成本：任何数字都要重新推导查询才能复核（例：`trade_records` 里 `date(exit_time)` 在混存时区（ET `-04:00` / CST `+08:00` / UTC）下会**跨日偏移虚增**；`max_drawdown` 是**百分点**口径而非 ratio）。这些只能靠人记忆与口头约定 ⇒ 一旦换人或换 agent 就重犯。

### 1.5 第四个症状：预注册是"手写一次性文档"，不是系统能力

`RFC-087`（盘前收口条件豁免）是**目标形态的范例**：固定样本 `--universe-from` + 双窗口 + 配对 + 功效声明 + 三诊断（worst5 / reversal_after_cut / capture）+ 复现命令 + "旧数字一律视为待更正"。但它是**手写的、一次性的**：判据写在文档里、样本完整性靠人肉检查、到期靠人记。本 DEB 要把它变成**常态产物**。

---

## 2. 目标（可判定）

| # | 目标 | 判定方式 |
|---|---|---|
| G1 | **单一产物 + 单一消费面**: 每日 1 个 JSON + 1 页 MD + 1 条推送 + 1 页 vault | `data/reviews/bundle_<date>.json` 存在且校验通过；当日推送新增恰好 1 条 |
| G2 | **溯源契约**: 产物里每个数字都带可复算证据（缺证据 = 构建失败） | schema 校验测试：去掉任一 evidence → 抛 `EvidenceError` 并**点名指标** |
| G3 | **周期 × 阶段默认视角**: 日/周/月 三周期，阶段×方向×出场原因、价格带、费用% 分片 | bundle 的 `sections.slices` 含全部阶段；合计 == headline 净额（逐分） |
| G4 | **判据自动到期上报**: 复用 `shadow_registry` 计数；维护最小 `ready_since` 状态，日推送只给计数 + 最久前 5 项 | 构造一个 `min_n` 已达标的假条目 → 状态文件记录该 id；次日推送含 `ready_count` 与该 id |
| G5 | **自审与减法**: 每周自动列"死产出/无证据指标/告警噪声/未登记影子/**测试标的进入真实推送**" | `self_audit` + `coverage_<date>.json` 非空且可执行；且 `self_audit.fixture_symbol_pushes == 0`（判据见 §5.8；先例: 2026-09-19 单测 fixture 富卡真发生产 IM） |

---

## 3. 非目标（明确不做，防方案膨胀）

1. **不新建判据台账 / 影子注册表** —— `shadow_registry.py` 已是 SSOT，本方案只**读**它。
2. **不新建事件台账/第 5 套载体** —— 产物全部**派生重算**，持久状态只有既有 `lessons` 表与 `shadow_registry` 的 `status` 字段。
3. **不重建 `review_orchestrator.py`，不并联第二套辩论**。
4. **不新增前端**；人读面 = 推送 + Markdown + vault。
5. **不触碰 `app/playbook/*` 执行链**（本方案不改任何交易/退出行为，不需重启服务）。
6. **不做数据湖/长期存储**（"不是累积数据"）—— 产物保留在 `data/reviews/`（`data/*` 已 gitignore），趋势靠扫盘聚合。

---

## 4. 数据与实证（供复查 agent 逐条打假）

本 DEB 引用的全部数字与锚点：

| # | 声称 | 复现命令（仓库根执行） | 期望 |
|---|---|---|---|
| E1 | 阶段拆解 (335 笔: 盘前 4/−648.86, 盘中 329/+340.14, 盘后 2/+110.75, 合计 −197.97) | 见 §1.3 片段 | 逐格一致 |
| E2 | `shadow_registry` 在 `app/ scripts/` 无生产消费者，但 Hermes 周检有消费者 | `grep -rn "shadow_registry" --include=*.py app/ scripts/ ops/ \| grep -v shadow_registry.py` | 命中 `ops/hermes/ftseek_weekly_checkpoint.py` |
| E3 | `get_ready_to_evaluate/refresh_counts` 无日级调用；`refresh_counts` 有周六消费者 | `grep -rn "get_ready_to_evaluate\|refresh_counts" app/ scripts/ ops/ \| grep -v shadow_registry.py` | 仅 `ops/hermes/ftseek_weekly_checkpoint.py` 调 `refresh_counts` |
| E4 | 注册表条目数 ≥30 | `grep -cE '^\s+id="' app/playbook/shadow_registry.py` | ≥30 |
| E5 | ADR-035 配对产物**不是** `data/shadow/entry_archetype_routing.jsonl` | `ls data/shadow/entry_archetype_routing.jsonl` | `No such file`（**猜路径会静默归零**的实证；真路径需实现方实测定位，候选 `data/shadow/entry_delay_confirm.jsonl`，见 `shadow_registry.py:947-975`） |
| E6 | 军规: 禁直连 notifier / 禁自写 webhook | `sed -n '345,352p' scripts/trigger_gate_report.py` | docstring 明写；推送走 `app.push` |
| E7 | 按日闸推送先例 | `sed -n '380,397p' scripts/trigger_gate_report.py` | `dedup_key=f"trigger_gate_anchor:{day}"` + `sync=True` |
| E8 | `date(exit_time)` 混时区风险 | `sqlite3 data/ftseek_us.db "select exit_time from trade_records where exit_time is not null and exit_time not like '%-04:00%' limit 5"` | 实测存在**无偏移**行（例 `2026-04-24T23:30:43.102159`）⇒ 跨日偏移风险实体存在 |
| E9 | 重复标记行约定 (F1) | `sqlite3 data/ftseek_us.db "select count(*), count(pnl_net) from trade_records where data_quality='duplicate_external_close'"` | 实测 `5\|0` ⇒ 5 条重复标记行、**0 条带净额** ⇒ F1 约定成立 |
| E10 | 既有编排器存在并被调用 | `sed -n '486,492p' app/core/scheduler_triggers.py` | `get_review_orchestrator(...).run_daily(...)` |
| E11 | `stage_stats` 是阶段 SSOT | `sed -n '1,28p' app/review/stage_stats.py` | 自陈"唯一实现放这里" |
| E12 | RFC-087 是范例预注册 | `sed -n '1,20p;66,120p' docs/decisions/RFC-087-premarket-flatten-conditional-exemption-preregistration.md` | 含固定样本/双窗口/配对/功效/复现命令 |

---

## 5. 方案设计

### 5.1 架构（一句话：**一层派生视图，四个消费面**）

```
                    ┌──────────────── 既有 SSOT（只读，不改）────────────────┐
trade_records ──────┤ app/review/stage_stats.py   (阶段口径 SSOT)            │
playbook_instructions┤ app/review/daily_review.py  (日报 JSON 111 份)        │
logs/*.log ─────────┤ app/playbook/shadow_registry.py (判据 SSOT, 30+ 项)   │
data/shadow/*.jsonl ┤ app/core/trade_identity.py  (ET 口径 SSOT)            │
lessons 表 ─────────┤ app/push                       (唯一推送入口, 军规)     │
                    └───────────────────────┬───────────────────────────────┘
                                            ▼
                     app/review/bundle_builder.py  →  ReviewBundle(结果优先, 每数字带 evidence)
                                            ▼
        ┌──────────────┬────────────────────┬──────────────────┬─────────────┐
   JSON(机器真相)   MD(人读)             推送(1 条/日)        vault(沉淀)    周报/月报(周期)
 data/reviews/   data/reviews/       app.push sync=True   obsidian_vault/daily   同产物切片
```

**核心原则**（对应用户四句话）：
- **总结** = 一个产物、一个入口；不再"每天被动询问"。
- **溯源** = `Metric.evidence`（SQL/产物/日志锚点三选一，必填）。
- **合理优化** = 候选改动必须凑齐 §5.5 的"决策依据最小充分集"才允许提裁决。
- **非拍脑袋** = 判据来自**注册表 + 预注册 + 功效声明**，不来自单次读数（含"n<40 只写证据不足"的纪律）。
- **按周期/按阶段** = §5.4 的维度矩阵是**默认视角**，不是可选报表。

### 5.2 结果口径 SSOT（口径铁律表 —— 实现方逐条照抄，禁止另立口径）

| 项 | 铁律 | 来源 |
|---|---|---|
| 已平仓 | `exit_time IS NOT NULL AND pnl_net IS NOT NULL` | `stage_stats.load_trades` |
| 排除行 | `coalesce(data_quality,'') <> 'duplicate_external_close'`（F1 约定: 重复标记行留 NULL，靠净额求和自然排除） | `DEB-20260917` F1 (`d56612164`) |
| 净额 | `pnl_net`（费后权威） | 同上 |
| 毛额 | `realized_pnl` | 同上 |
| 费用 | `fees_est` | 同上 |
| 归集日 | **出场日**（`date(exit_time)`）— 隔夜仓按出场日计 | `review_orchestrator.py:2011` 修复先例 |
| 阶段 | `entry_session` 优先 → `phase_from_time(entry_time)` ET 时钟回退 → 空串标"未标注" | `app/review/stage_stats.normalize_stage` |
| 时区转换 | 一律 `app.utils.timezone.to_et` / `scripts.utils.et_time`（**禁手写 −12h**；naive 按 CST） | 项目既有纪律 |
| 量纲 | `pnl_pct` 是 ratio；`mfe_pct/mae_pct` 是百分点；`max_drawdown` 是**百分点** | `pipeline.py:6631-6635`、`trade_analyzer.py` |

**新增校验（本方案强制）**: `exit_date_mismatches(conn)` —— 用 `et_time` 把每行 `exit_time` 转 ET 日，与 `date(exit_time)` 比对。差异 >0 ⇒ bundle 必须写 `anomalies` 且 `severity=P0`（**宁可显性报错，不静默虚增**）。

### 5.3 证据契约（schema 与铁律：**缺证据 = 构建失败**）

```python
# app/review/bundle_schema.py
SCHEMA_VERSION = "review_bundle/1"
ALLOWED_KINDS = ("sql", "artifact", "log", "derived")
ALLOWED_UNITS = ("usd", "pct", "pp", "count", "ratio", "text", "bool")

@dataclass(frozen=True)
class Evidence:
    kind: str                       # sql | artifact | log | derived
    ref: str                        # SQL 文本 / 产物路径 / 日志锚点 / 派生说明
    params: tuple = ()              # SQL 绑参 (禁字符串拼接)
    note: str = ""                  # 口径说明
    integrity: dict = field(default_factory=dict)   # ★ 产物类证据必填: 见下

@dataclass
class Metric:
    name: str; value: Any; n: int; unit: str
    evidence: Optional[Evidence] = None
    detail: dict = field(default_factory=dict)

@dataclass
class Bundle:
    report_date: str
    headline: Dict[str, Metric]
    schema_version: str = SCHEMA_VERSION
    sections: dict = field(default_factory=dict)
    anomalies: list = field(default_factory=list)
    pending_adjudications: list = field(default_factory=list)
    self_audit: dict = field(default_factory=dict)
    generated_at: str = ""; git: dict = field(default_factory=dict)
```

**`Evidence.integrity`（新增硬约束，来自 RFC-087 的教训）**：凡 `kind in ("artifact","derived")` 的指标，`integrity` 必须至少含 4 键，否则校验失败：

```python
REQUIRED_INTEGRITY = ("n", "window", "universe", "truncation_checked")
# n            : 样本笔数
# window       : "2026-06-24..2026-09-16"
# universe     : 固定样本来源 (如 "data/reports/_rfc087_universe_premarket.json") 或 "db:trade_records"
# truncation_checked : True/False — 是否过了样本完整性校验 (bar 截断/日期覆盖/宇宙固定)
```

> 为什么必须有这一条：`RFC-087 §3` 实测 —— 回放工具因 富途 `max_count=1000` 而**静默丢 chunk 尾部**，导致 9/56 笔在四条臂上读到**同一个 07:20 价格**，旧表 `−0.402% / +0.237% / −7.281%` 全部作废。**没有 integrity 块的产物数字，一律不得进裁决。**

### 5.4 周期 × 阶段：维度矩阵（默认视角）

| 周期 | 产物 | 必需切片 | 调度 |
|---|---|---|---|
| **日** | `bundle_<date>.json` + `REVIEW_<date>.md` + 推送 + vault | ①结果层(净/毛/费/胜率/费毛比) ②阶段×方向×出场原因 ③价格带×费用% ④计划vs实际(执行率/skip_reason 分布) ⑤异常(不变量+日志指纹) ⑥待裁决清单 ⑦自审 | 每交易日 06:30 CST |
| **周** | `WEEK_<iso>.md`（同产物切片） | ①阶段趋势(7 日) ②判据进度与 `READY_TO_EVALUATE` 清单 ③上周落地项 before/after ④死产出/未登记影子 ⑤告警噪声 | 周六 06:45 CST |
| **月** | `MONTH_<YYYY-MM>.md` | ①双窗口对照 ②预注册判据总账(已裁/待裁/否决) ③口径变更审计(哪些指标改过口径、为什么) | 每月 1 日 |

**价格带×费用%** 的必要性（已有实证）：费用率随价格带差 6 倍（`$5` 档 ≈0.53%、`$50` 档 ≈0.094%），不平价带无法判"某档是否有 edge"。

### 5.5 决策依据最小充分集（**反"拍脑袋"的核心契约**）

任何"改参数/改门/改链"的提案，必须同时附下列 8 项，缺一项即**不受理**（这一条同时约束人和 agent）：

| # | 项 | 要求 |
|---|---|---|
| 1 | 假设与方向 | 一句话可证伪（"X 优于 Y"，含比较基准 Y） |
| 2 | 样本纪律 | 固定宇宙 `--universe-from`（保证换臂可配对）+ **双窗口**（含样本外）+ 样本完整性 `integrity` 块 |
| 3 | 主判据 | 费用后净均值**配对**优于基线，且**双窗口同向** |
| 4 | 尾部判据 | 最差 5% 不劣化 > 1pp（跳空尾部单列） |
| 5 | 机制诊断 | 至少 1 个机制性读数（如 `reversal_after_cut_pct` / `capture_mean_pct` / 阶段分档归因） |
| 6 | 功效声明 | 写明"能判方向/尾部，还是能判 0.1pp 级"（单笔净 σ≈1.18% ⇒ 检出 +0.15% 需 **n≈481**；n=56 的 CI 半宽 ±0.31pp）<br>⚠️ **该 σ 输入已被 §17/R-2 更正**：`pnl_pct` 口径实测 σ=**1.8977%**（全量已平仓 n=870，无任何切面接近 1.18%）⇒ **n≈1258**、n=56 CI **±0.498pp**；且 σ 按阶段差近 1.6 倍 ⇒ 功效必须**按阶段分报** |
| 7 | 预注册与回滚 | 落 `shadow_registry` 条目（`min_n` / `graduation_criteria` / `count_mode` / `eval_script`）+ env 级回滚方式 |
| 8 | 复现命令 | 逐条可粘贴（含 `--out` 路径与读哪几个 `summary.*` 字段） |

> 这 8 项不是新发明，是 `RFC-087 §5-§7` 的**已完成形态**的抽象。本方案把"它在那里做对了"变成"默认所有人必须这么做"。

### 5.6 判据与优化闭环（**复用 `shadow_registry`，零新建**）

```
shadow_registry.REGISTRY (30+ 项)
   ├─ status=OBSERVING           → refresh_counts() 算 n/min_n → 进度进 bundle.pending_adjudications
   ├─ status=READY_TO_EVALUATE   → ★到点: 当日推送单列 + 周报置顶 (burning: 不许静默成熟)
   ├─ status=GRADUATED/KILLED    → 周报 before/after + 审计留痕
   └─ check_mounting()           → 死产出/未登记影子 → 进 self_audit.dead_or_unregistered
```

**唯一新增只读模块** `app/review/criteria_status.py`（≤80 行）：

```python
def pending_adjudications() -> list[dict]:
    """读既有注册表 → 待裁决清单 (只读; 不新建台账)。"""
    from app.playbook.shadow_registry import REGISTRY, ShadowStatus, refresh_counts
    refresh_counts()                      # 既有 API: 就地刷新计数 (无副作用: 不写交易)
    out = []
    for s in REGISTRY:
        out.append({
            "id": s.id, "status": s.status.value, "n": getattr(s, "current_n", None),
            "min_n": s.min_n, "progress": f"{getattr(s,'current_n',0)}/{s.min_n}",
            "ready": s.status == ShadowStatus.READY_TO_EVALUATE,
            "criteria": s.graduation_criteria, "artifact": s.data_path,
            "eval_script": s.eval_script, "count_mode": getattr(s, "count_mode", None),
            "evidence": {"kind": "artifact", "ref": f"app/playbook/shadow_registry.py :: id={s.id}"},
        })
    return out
```

**READY 状态与防噪（第 2 轮复查补充）**: `refresh_counts()` 的 READY 是本进程内推导，不落盘。CLI 层新增最小状态 `data/reviews/criteria_ready_state.json`，只存 `{id: ready_since}`，不存判据、阈值或计数；在 `fcntl.flock` 独占锁内更新/删除，离开 READY 即销账。日推送只写 `ready_count` 与按 `ready_since` 最久前 5 项；全量清单进 JSON/MD，避免当前 19 项 READY 每日刷屏。

**到点推送**: 只并入 06:35 日报的“待裁决”段，使用 caller 侧按日守卫；**不得**依赖 `dedup_key`。正文给 `ready_count`、最久前 5 项及"到点未裁"天数。

**新增影子必须注册**（既有纪律，本方案把它变成**自动可见**）：`check_mounting()` 报"未登记影子" → 周报必列。

### 5.7 自审与减法（消费者地图**自动生成**，不维护 YAML）

**不做手工注册表**（避免又一个"登记了没人读"）。改为**静态引用扫描**：

```python
# scripts/review_coverage_scan.py (只读)
# 对每个候选产出 (shadow_registry.data_path ∪ crontab 复盘行的日志/产物路径):
#   refs = 在 app/ scripts/ web_dashboard/ 里 grep 该路径 (排除: 产出方脚本自身、其 tests、docs)
#   dead = len(refs) == 0
# 输出 data/reviews/coverage_<date>.json: {producers:[{path, refs, dead}], unregistered_shadows:[...]}
```

这样：**死产出与"登记了没人读"由代码结构自动暴露**，不依赖人维护清单 —— 这是"做减法"的机制化（对应 `crontab` 里已存在的 `atr_gate_shadow 产出零消费(45KB/日无人读), 停` 先例）。

### 5.8 自审新判据: 测试标的进入真实推送 (`fixture_symbol_pushes`)

**先例 (2026-09-19, 用户实测收到)**: 一条"建仓成交富卡"**逐项等于单测 fixture**
(`tests/test_extended_hours_order_execution.py:136-141` 的 `US.FAKE` 100 股 @10.0 / 盘前 +3.00% /
EXT 量 300,000 / 财报超预期 +10.0% / SL $9.50)。生产链不可能产生它 —— `trade_records`、
`open_positions`、`logs/ftseek_us.log` 三处 `US.FAKE` **均 0 次**，该标的只存在于单测 fixture
(+ 一个已归档的 `scripts/archive/e2e_smoke_test.py`)。即: **通知出口被测试标的污染**，
而当时单测**全绿** —— 唯一的告警面是用户的 IM。这正是本系统要消灭的那类"无证据也不报红"的失败。

判据（全部读**已有/已改**产物，不新建台账，不新增写者）:

| # | 数据源 | 命中条件 |
|---|---|---|
| 1 | `data/ext_entry_push_dedup.json` | key `ext_entry_filled:<symbol>:<pos_id>` 的 `<symbol>` 段命中 `is_test_symbol()` 或**单测 fixture 常量集**（该键 2026-09-19 起由成交富卡写入 ⇒ 本类泄漏的**唯一持久留痕**） |
| 2 | `data/critical_alerts.flag`（FlagFileChannel, 仅 HIGH/CRITICAL）+ `data/push_failures.jsonl` | 正文同上 |
| 3 | `logs/ftseek_us.log` | `[PushHub·test-symbol]` 告警行 |

`fixture 常量集` 不手工维护: 从 `tests/**/*.py` 抽 `US.<大写>` 字面量, 与 `trade_records` /
`open_positions` **零出现**交叉确认（2026-09-19 判定 US.FAKE 用的就是这条路径）。

**诚实标注的盲区**: `trade_entry` 富卡是 `Level.NORMAL` ⇒ FlagFileChannel 不写 flag 文件,
`PushHub` 也不落盘"每笔推送"⇒ **若无判据 1, 这类泄漏在生产产物上完全不可见**（本次实测:
`data/critical_alerts.flag` 连文件都不存在、`logs` 里 0 命中）。所以这条判据的载体是 2026-09-19
新增的富卡去重键, 不能写成"事后翻日志就能查"。

**源头修法（已落地, 本方案只读其产物）**:
①`_push_filled` 并入 `_push_once` 的每 ET 日去重（抽出 `_dedup_reserve`, key =
`ext_entry_filled:<symbol>:<pos_id>` —— 同标的**再入场是新 pos_id**, 不受影响; 收敛掉的只是同一笔
成交被反复演练/重放 —— 与用户"上午还有这种推送"一致）；
②`_probe_mode()` 把"进程内在跑 pytest"也算静默（事故时出口封锁只挂在 `tests/conftest.py` 上,
`--noconftest` / 裸 import 测试模块的跑法完全不受保护）；
③`tests/conftest.py` 会话结束对"渠道 send 层有留痕"的用例**报红**（白名单
`@pytest.mark.allow_outbound_notification`），把"用户看 IM 才知道"变成"测试红"。

---

## 6. 可观测性（本系统自身也要被观测）

- bundle 每次构建写入 `data/reviews/bundle_<date>.json`（`data/*` gitignore），并保留 `bundle_latest.json` 供快速读取。
- **幂等**: 同日重跑除 `generated_at`/`git` 外**逐字相同**（测试断言）。
- **原子写**: `tempfile + os.replace`，避免半截产物被消费。
- **本系统的成败也进自审**: `self_audit = {"dead_producers":[...], "unregistered_shadows":[...], "metrics_without_evidence":0, "error_lines_today":N, "pushes_today":M, "fixture_symbol_pushes":0}`（末项判据见 §5.8）。
- **口径变更审计**: golden 基准 + 任何口径改动必须同批更新 golden 并写 DEB（机制上强制，见 §9 P5）。

---

## 7. 调度与周期（cron，含回滚注释）

| 时刻(CST) | 行 | 说明 |
|---|---|---|
| 每交易日 06:30 | `--date $(TZ=America/New_York date -d yesterday +\%F) --push --vault` | 日复盘（收盘后、既有 06:20 LLM 报告与 06:20+ 复盘链之后） |
| 周六 06:45 | `scripts/review_coverage_scan.py` | 每周自审（死产出/未登记影子） |
| 每月 1 日 07:00 | `--period month` | 月度双窗口对照 + 判据总账 |

⚠️ 两个必须实测的点：①crontab 内 `%` 需转义为 `\%`；②命令替换 `$()` 在部分 cron 实现下不执行 ⇒ **上线后必须 `grep review_bundle logs/review_bundle.log` 验证真实产出**（不许假设生效）。

> **载体与时刻以 §16 为准**：调度载体改为 **Hermes cron no_agent**（`ops/hermes/review_bundle_carrier.py` + 线上副本）；日环时刻 **06:30 → 06:35 CST**（06:30 与 `alpha-backfill` 同分钟，§16.3 实测）。下列三行的语义（日/周/月 + 回滚注释）不变。

---

## 8. 停止条件与通过标准

**停止/回滚条件**（任一命中即回落）：
1. bundle 构建失败或 schema 校验失败（连续 2 日）；
2. headline 净额与直接 SQL 对账**不一致**（第 6 节人工对账）；
3. 推送导致日推送量翻倍且用户判定噪声（>2 条/日新增）。

**通过标准（G1–G5 全绿）**：
- 每日产物存在且 `evidence` 覆盖率 **100%**（无证据 = 构建失败，故为结构性保证）；
- headline 与 SQL 对账**逐分一致**（≥5 个连续交易日抽查）；
- 至少 1 个真实判据被系统**自动**报出 `READY_TO_EVALUATE`（用 fixture 亦可，但需一次真实触发）；
- 周报至少产出 1 条可执行的退役候选。

---

## 9. 实施步骤（P0–P5；TDD：先红 → 最小实现 → 绿 → 提交；每步含验收命令）

> 完整逐条代码见工作区计划 `.hermes/plans/2026-09-19_152656-review-system.md`；下列为 DEB 自含版本。

**P0 事实基线（只读，不改行为）**
- `scripts/review_coverage_scan.py`（静态引用扫描，§5.7）→ `data/reviews/coverage_<date>.json`
- 验收: `venv/bin/python scripts/review_coverage_scan.py` → 打印 `{... "dead_producers":[...]}` 且产物落盘
- 提交: `[RFC-088][review-system] P0: 覆盖扫描(只读)`

**P1 证据契约（地基）**
- `app/review/bundle_schema.py`（§5.3 含 `integrity` 硬约束）+ `tests/review/test_bundle_schema.py`
- 验收: `venv/bin/python -m pytest tests/review/test_bundle_schema.py -q` → 先 `ModuleNotFoundError`(RED) → 实现后 `5 passed`；其中"去掉 evidence 必须点名指标"为必测
- 提交: `[RFC-088][review-system] P1: schema + 证据契约`

**P2 结果层 + 阶段切片 + 异常层**
- `app/review/bundle_builder.py`: `build()` / `build_slices()` / `build_plan_vs_actual()` / `build_anomalies()` / `exit_date_mismatches()`
- 测试用**临时 in-memory DB**（`sqlite3.connect(":memory:")` + 自建 DDL），不碰生产库
- 验收: `venv/bin/python -m pytest tests/review/test_bundle_builder.py -q` → 全绿；且断言 `sum(slices.net) == headline.net_pnl`
- 人工对账（必做一次）:
  ```bash
  sqlite3 data/ftseek_us.db "select round(sum(pnl_net),2) from trade_records where exit_time is not null and pnl_net is not null and coalesce(data_quality,'')<>'duplicate_external_close' and date(exit_time)='2026-09-18';"
  ```
  必须与 `bundle_2026-09-18.json:headline.net_pnl.value` **逐分相同**
- 提交: `[RFC-088][review-system] P2: 结果层+阶段切片+异常层`

**P3 渲染 + CLI + 推送**
- `app/review/bundle_render.py`（`render_md(bundle) -> (full_md, push_text)`）+ `scripts/review_bundle.py`（`--date/--push/--vault/--period`；原子写；幂等）
- 推送: `from app.push import get_push_hub, PushMessage, Level, Category`，`category=Category.REVIEW_DAILY`、`sync=True`；在 `fcntl.flock(<bundle>.lock)` 独占锁内重读 caller 侧按日状态，未推才发送，发送成功后写 `pushed_at` 并释放锁；失败不得标记已推。**不得**依赖 `dedup_key` 实现"同日只推一次" —— `sync=True` 绕过 hub 去重（§16.5 更正 B，`app/push/hub.py:124`）
- 验收: `pytest tests/review/test_bundle_render.py tests/review/test_bundle_cli.py -q` → 全绿（含幂等断言）；`venv/bin/python scripts/review_bundle.py --date 2026-09-18` → 打印 headline 摘要
- 提交: `[RFC-088][review-system] P3: 渲染+CLI+单条推送`

**P4 判据到期上报（复用注册表）**
- `app/review/criteria_status.py`（§5.6，≤80 行，只读注册表）+ 到点推送
- 新增 `data/reviews/criteria_ready_state.json`（只存 id→ready_since；flock 内更新/销账），日推送只报 ready_count + 最久前 5 项
- 验收: fixture 造一个 `min_n` 已达标的假条目 → 状态文件记录该 id，push body 含 `ready_count` 与该 id；真实读取: `venv/bin/python -c "from app.review.criteria_status import pending_adjudications as p; print(len(p()))"` → ≥30
- 提交: `[RFC-088][review-system] P4: 判据到期上报(复用 shadow_registry)`

**P5 上线 + 口径漂移回归 + 周期产物**
- golden: `tests/review/golden/bundle_2026-09-18.json`（入库）+ `test_bundle_matches_golden`（比对前剔除 `generated_at`/`git`）
- cron 三条（§7）+ 备份 `crontab -l > /tmp/cron.bak.$(date +%F_%H%M%S)` 先行
- 验收: `pytest tests/review/ -q` 全绿；`crontab -l | tail -3` 见三行；次日 `grep review_bundle logs/review_bundle.log` 有真实产出
- 提交: `[RFC-088][review-system] P5: cron + golden + 周期产物`

**整体回归**（每次提交前）:
```bash
venv/bin/python -m pytest tests/review/ -q
venv/bin/python -m pytest tests/test_stage_stats.py tests/test_daily_review.py tests/test_review_tables.py \
  tests/test_review_validation_accounting.py tests/test_okr_2026_09_scoring.py -q
venv/bin/python -m py_compile app/review/bundle_schema.py app/review/bundle_builder.py \
  app/review/bundle_render.py app/review/criteria_status.py scripts/review_bundle.py scripts/review_coverage_scan.py
```

---

## 10. 复查清单（**给独立复查 agent**：逐条打假，不许只读文档）

纪律：**不采信本文档自述**（本仓宪章律 7: 遥测实测 > 文档自述）。每条给出复现命令与反例判据。

| # | 待查声称 | 复现 | 反例（判为不成立的条件） |
|---|---|---|---|
| C1 | 阶段拆解 335/盘前4/−648.86、盘中329/+340.14、合计−197.97 | §1.3 片段 | 任一格不一致 ⇒ 本文档 E1 作废 |
| C2 | `shadow_registry` 在 app/scripts 无生产消费者，Hermes 周检已有消费者 | E2/E3 两条 grep | 若日级/裁决闭环已存在 ⇒ G4 需降级；若仅周六汇总 ⇒ 表述改为“消费面不足” |
| C3 | 注册表条目 ≥30 且含状态机/min_n/count_mode | E4 + `sed -n '37,80p' app/playbook/shadow_registry.py` | 字段缺失 ⇒ §5.6 需先补字段再加读取 |
| C4 | 判据真路径**不可猜**（`entry_archetype_routing.jsonl` 不存在） | E5 | 若该路径存在 ⇒ 说明存在多个同名概念，需重新定位 |
| C5 | 军规: 推送必须走 `app.push` | E6 | 若发现生产脚本直连 notifier/webhook ⇒ 本方案需先处置该违反 |
| C6 | `date(exit_time)` 混时区会跨日偏移 | E8 + 自行构造一行 `+08:00` 的 exit_time 跑 T2.5 断言 | 若全部 exit_time 均为 ET 格式 ⇒ §5.2 新增校验可降级为 warning |
| C7 | F1 约定: `duplicate_external_close` 行净额为 NULL | E9 | 若有行带净额 ⇒ 需先修数据再上系统（否则 headline 污染） |
| C8 | `plan_vs_actual` 的 skip_reason 可分组（`chase_cap_enforced` / `方向偏斜超限` / `admission:` / `非当前通道候选`） | `sqlite3 data/ftseek_us.db "select skip_reason, count(*) from playbook_instructions where date='2026-09-18' group by 1 order by 2 desc limit 10"` | 若分组不成立 ⇒ 任务 P2 的该函数需重设计 |
| C9 | 费用率随价格带差 6 倍（$5≈0.53% / $50≈0.094%） | `venv/bin/python -c "from app.execution.fee_model import estimate_round_trip as e; [print(p, e(int(5000/p),p,p,'BUY')['total']) for p in (5,10,20,30,50)]"` | 实测应为 `5 26.304 / 10 13.204 / 20 6.653 / 30 5.112 / 50 4.703`；数量级不符 ⇒ §5.4 的价格带维度依据需替换 |
| C10 | 推送噪声基线 ≈54 条/日（其中心跳 15） | `grep -cE "(钉钉|飞书)\] (send_text\|推送) 成功" logs/ftseek_us.log` | 显著不同 ⇒ §8 的"翻倍"阈值需重标定 |
| C11 | 幂等可实现（同日重跑仅 `generated_at`/`git` 不同） | P3 测试 | 若不幂等 ⇒ 需定位非确定性来源（字典序/浮点/时间） |
| C12 | 本方案**不触碰** `app/playbook/*` ⇒ 无需重启 | `git diff --name-only` 检查改动集 | 若实现涉及 `app/playbook/*` ⇒ 必须走重启与 6 Gate |
| C13 | 统计功效数字（σ≈1.18% ⇒ 检出 +0.15% 需 n≈481；n=56 CI ±0.31pp） | 用 `rows_from_db` 取盘前/全体单笔净额算 σ，再算 power | 若 σ 差 >20% ⇒ §5.5 第 6 项的门槛需重算 |
| C14 | `golden` 入库路径不被 gitignore | `git check-ignore -v tests/review/golden/bundle_2026-09-18.json` | 返回忽略规则 ⇒ 需换路径（`data/*` 已忽略，`tests/` 未忽略） |

**我主动请求挑战的三点**（设计上最可能错的地方，请重点攻击）：
1. **G4 的"到点主动推"是否会造成告警噪声** —— 我主张只推 `READY_TO_EVALUATE` 且按日闸；请反驳"应该只进周报不推即时"。
2. **单一产物是否会变成新的单点耦合** —— 我主张"派生重算"换零漂移；请反驳"每次重算会随代码改动悄悄变口径"，以及 golden 是否足够。
3. **阶段口径以 `entry_session` 优先、ET 时钟回退是否正确** —— 实测 875 条里约 400 条 `entry_session` 为空；请检查"为空但实际是盘中开的仓"是否被 `phase_from_time` 误归到盘前/盘后（尤其 CST 格式行的时区偏移）。

---

## 11. 风险与取舍

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 自己另写一套净额/阶段口径 ⇒ 第二口径 | 强制 import `stage_stats`；golden 测试锁死；§5.2 铁律表 |
| R2 | `date(exit_time)` 跨日偏移静默虚增 | `exit_date_mismatches()` + `severity=P0` |
| R3 | 产物类数字无样本完整性 ⇒ 重演 RFC-087 的截断教训 | `Evidence.integrity` 4 键硬约束 |
| R4 | 推送噪声 | 默认 `--push` 可关；按日闸；阈值写进 §8 |
| R5 | 判据路径猜错 ⇒ 计数静默归零 | E5 实证 + `criteria_status` 输出 `artifact` 路径，缺失时显式标 `missing` |
| R6 | 单一入口失败 ⇒ 当日无复盘 | 失败即回落到"只落盘不推送"；构建失败进自审并告警 |

**取舍**：
- 派生重算（零台账）换可无条件重跑与零漂移；代价：趋势需扫盘聚合（`data/reviews/bundle_*.json`），且**历史不可回溯**（只从上线日起）。
- 与 `review_orchestrator` 并存：orchestrator 管"辩论 + 记忆注入"，bundle 管"结果 + 溯源 + 判据到期"；两者共用 `lessons`，不重复建索引。

---

## 12. 回滚

```bash
# 1) 停调度（先备份 crontab）
crontab -l > /tmp/cron.bak.rollback.$(date +%F_%H%M%S)
crontab -l | grep -v "review_bundle\|review_coverage_scan" | crontab -
# 2) 删代码（无数据迁移；data/reviews 为 runtime 输出，可保留供查）
rm -f scripts/review_bundle.py scripts/review_coverage_scan.py
rm -rf app/review/bundle_schema.py app/review/bundle_builder.py app/review/bundle_render.py app/review/criteria_status.py tests/review
# 3) 无需重启服务（未触碰 app/playbook/*；新模块只被短命 cron 进程 import）
systemctl is-active ftseek-us   # 保持 active
```

---

## 13. 待决问题（需用户裁决；勿自行决定）

1. **推送节奏**：每日 06:30 CST 一条是否可接受？是否把现有 `daily_llm_token_report --push`（LLM 成本日报）与 `da_chain_health_report --push` **并入**这一条摘要（日推送 1 条原则），还是并存？
2. **`READY_TO_EVALUATE` 是否即时单推**（§10 挑战点 1）？还是只进周报？
3. **首批退役授权**：当前无消费者候选（`exit_scorecard` / `direction_hit_scorecard` / `fee_unit_economics_scorecard` / `overnight_arm_report`，均只 `>> logs/*.log`）是否授权按"先注释 cron、30 天无反弹再删脚本"处置？
4. **有效期**：`pending_adjudications` 是否要引入"到点未裁即升级"（如 `decide_by` 逾期 → P1 告警）？这会把"被动询问"变成"系统催办"，但也会增加噪声。
5. **历史起点**：是否要把上线日之前的日子做一次性回填（用现行口径重算 30 天 `bundle_*.json`），还是只从上线日起？（回填会用今天的口径解释过去，存在"事后合理化"风险。）

> **§20 给出作者对上述各项的明确建议**（每项：建议 / 理由(实测) / 风险 / 保守替代 + 能否现在做），请对着 §20 裁；§20.3 另列我尚未验证的依赖项。
>
> **§16（载体形态决定）已就上列问题给出耦合结论**：第 1 问的时刻需改（06:30 与 `alpha-backfill` 撞分钟）与归并边界（只并报表类两条）；第 2/4 问的去重机制改为 **caller 侧按日状态**（§16.5 更正 B：`sync=True` 绕过 hub 去重）；第 3 问候选 **4 → 1**（§16.5 更正 A）；第 5 问回填须只写产物不推送。

---

## 14. 验收清单

> 勾选由第 3 轮独立复查 agent 于 2026-09-19 按 §26 实测证据执行；未勾项均属 P5（未开工）。

- [x] P0 覆盖扫描可跑，产出 `data/reviews/coverage_<date>.json`，含 `dead_producers`（§26.1：`coverage_2026-09-19.json` 实测 producers=58 / dead_producers=18）
- [x] P1 schema 五测全绿，含"缺证据点名指标"用例（§26.1：`tests/review` 22/22；反证实验独立重跑 ⇒ `headline.y: evidence is required` / `... integrity is missing universe`）
- [x] P2 结果层与 `sqlite3` 直接查询**逐分一致**（≥1 次人工对账留痕）（§26.1：第 3 轮重跑 SQL `34.56|8|7` == headline）
- [x] P2 切片净值之和 == headline 净额（测试断言）（§26.1：110.75+(−76.19)=34.56 实测）
- [x] P2 `exit_date_mismatches` 存在且差异 >0 时 P0 上报（专项测试 `tests/review/test_bundle_builder.py:148-162` 断言 `severity=="P0"`；当前库差异=0）
- [x] P2 `Evidence.integrity` 4 键硬约束生效（少一键即构建失败）（§26.1：独立遍历 28 指标缺失=0 + 反证实验抛错点名）
- [x] P3 幂等（同日重跑逐字相同，除 `generated_at`/`git`）（§26.1：第 3 次重跑三产物 md5 逐字一致；注意实现将 `generated_at=""`/`git={}` 恒空 ⇒ "除"字空转，见 §26.2 N-4）
- [x] P3 推送恰好 1 条且走 `app.push`（caller 侧 `pushed_at` + `flock` 生效；发送失败不标记已推）（§25.2 S-1 生产实证 1 条送达 + 用户确认；`_push_once` 代码核过 flock 内重读/成功才标记；FakeHub 失败不落状态测试绿）
- [x] P4 判据清单 ≥30 项；`ready_since` 状态可更新/销账；日推送只列 ready_count + 最久前 5 项（§26.1：36/19/19 实测；真实推送文本实证"待裁决 19：…前 5 项"）
- [ ] P5 golden 入库且 `git check-ignore` 返回非忽略（P5 未开工：`tests/review/golden/` 不存在）
- [ ] P5 cron 三行生效且**次日有真实产出**（`grep review_bundle logs/review_bundle.log`）（P5 未开工：crontab 0 行、Hermes 无 review_bundle job）
- [ ] §16 载体：`ops/hermes/review_bundle_carrier.py` 入库且与线上副本 sha256 一致（`pytest tests/test_ops_scripts_sync.py -q` 绿）；载体成功路径**零 stdout**（不双推）（P5 未开工：载体文件不存在；`test_ops_scripts_sync` 现状 3/3 绿）
- [x] §16 更正 B：同日重跑**不产生第 2 条推送**（caller 侧按日守卫生效）（§26.1：带 `--push` 手工重跑实测 ⇒ 结果 JSON `"pushed": "already"`、`push.json` 未变、无新失败记录；⚠️ 实际行为是结果 JSON 字段而非日志行 `skip: already pushed <date>`，验收文案与实现不符见 §26.2 N-7）
- [x] 全量回归通过（`tests/review/` + 5 个既有文件）（§26.1：22/22 + 74/74 独立重跑绿）
- [x] 第二 agent §10 清单逐条给出结论（成立/不成立 + 证据）（第 1 轮 §17、第 2 轮 §19、第 3 轮 §26 均已完成并入库）

---

## 15. 讨论记录

- **2026-09-19 · 用户**: "目前建议系统有很多信息收集和统计，包括散落零散的测试、推送、记录等，但是缺乏一个完整并基于结果，溯源，整合的复盘分析系统，而不是每天被动询问。"
- **2026-09-19 · 用户（定调）**: "最终目的不是累积数据，而是总结溯源，合理优化，非拍脑袋决策依据，可以按周期，可以统计交易阶段等等。基于项目已有的内容和需要补充的进行完整的规划设计，写入 deb。我还需要另外的 agent 进行分析复查。"
- **2026-09-19 · Hermes（本稿）**: 按"证据优先"普查后**修正了问题陈述**：记录侧已过剩（330 脚本 / 108 cron / 108 jsonl / 285 lessons / 165 DEB），真正的缺口是**消费者层**。第 2 轮复查更正：`shadow_registry` 已有 Hermes 周六消费者，但无日级汇总、无持久 READY 时间与裁决收口。故方案收敛为"一层派生视图 + 四消费面"，并**删除**原设计中的"新建判据 YAML / 新建事件台账"两项（避免与既有 SSOT 双写）。
- **2026-09-19 · Hermes（自曝）**: 本会话早前给出的盘前臂读数（`pre_close −0.402% / rth_close −0.101% / intraday_sl +0.237% / 最差 −7.281%`）**已被 `RFC-087 §3` 更正为待更正数字**（回放工具 chunk 尾部静默截断）。本 DEB 不引用这些数字作为依据；§5.3 的 `integrity` 硬约束即该教训的机制化。
- **2026-09-19 · 用户（裁决）**: 问"复盘系统产出最终的样式会是 skills 还是其他什么框架合适"，并裁「需要」= 把该决定写成 DEB 新节（载体形态 + 落点清单 + 与 §13.2 的耦合）后提交、再交第二 agent 复查。落成本文 §16。
- **2026-09-19 · 用户（裁决）**: 第 2 轮核查后令「请将你的建议更新到 deb」⇒ 落成 **§20**：对 §13 五问 + §17.2 R-1 前置 + 开工范围逐项给明确建议（建议/理由/风险/保守替代），并把"现在能做"与"有前置"分开标注。
- **2026-09-19 · Hermes（第 1 轮独立复查已回）**: 两个只读复查 agent（A 组 C1–C9 数据类 / B 组 C10–C17+§16 对抗）完成后，作者逐条复核，结论落在 **§17**：13 条成立（其中 C9 只需精确化）、3 条不成立或部分不成立（C5 推送军规 / C10 心跳底噪 / C13 功效 σ）、1 条无法判定（C11 未落码）、5 条更正（R-1…R-5）、**1 条复查 agent 的否决被复核推翻并记录在案**（其 jobs.json 解析错误，把 34 个 job 读成 17 个）。**待用户裁决项未变**：§13 五问 + §17.2 R-1 的并入口径前置决定。

---

## 16. 载体形态决定（用户 2026-09-19 裁决「需要」后补；**结论：不是 skill**）

> 本节回答"复盘系统产出的最终样式是什么载体"。结论：**按消费者分层，一层一个载体**；skill 只承担最后一层（agent 怎么读），不承载产物、不承载代码。
> **本节与 §5.1/§7/§13/§14 冲突处，以本节为准**（依据是 16.5 的两处实测更正）。

### 16.1 分层载体表（一层一个载体，落点逐条可核）

| 层 | 载体（落点） | 消费者 | 为什么不是别的载体 |
|---|---|---|---|
| 计算 + 证据契约 | `app/review/{bundle_schema,bundle_builder,bundle_render,criteria_status}.py` + `tests/review/`（**入库**） | 测试 / CI / 其他 agent | 必须调 `app/review/stage_stats.py` 口径 SSOT（§5.2）；离仓即成第二套 SQL |
| 执行体 | `scripts/review_bundle.py`（`--date/--push/--vault/--period`，原子写、幂等） | cron | — |
| 调度 | **Hermes cron no_agent**：载体 `ops/hermes/review_bundle_carrier.py` + 线上副本 `/root/.hermes/scripts/`（`tests/test_ops_scripts_sync.py` 双向 sha256 **自动**纳入方向1/2） | 系统 | 与 9-19「载体迁移」②③批同形态：Hermes 侧有 `executions.db` 执行账本 + 非零退出记 failed；crontab 环"死亡不可见"正是那两批要修的病灶 |
| 产物 | `data/reviews/bundle_<date>.json`（原子写）+ `REVIEW_<date>.md` | 脚本 / 复查 agent | 逐日增长的数据不能进任何"常驻"载体 |
| 人读面 | `app.push` **1 条/日** + `obsidian_vault/daily` 一页 | 用户 | 单一消费面（§5.1） |
| **agent 读面** | **薄 skill** `~/.hermes/skills/devops/ftseek-review-bundle/SKILL.md`（~60–100 行，**零每日数字**） | agent 会话 | 只有"不变的操作规程"适合常驻注入（16.2） |
| 判据到点 | `app/playbook/shadow_registry.py`（**只读**）+ 消费者 `app/review/criteria_status.py` | 程序 | 已是 SSOT（§5.6），不新建 |
| 决策留痕 | DEB / RFC（git） | 人 + 复查 agent | 一次性，不随日增长 |

薄 skill 只装四节：①产物路径与 `schema_version`；②evidence 复算命令模板（含 `validate()` 调用）；③口径纪律（阶段必拆 / 净额列 / `n<40` 只写证据不足）；④禁止事项（禁引无 evidence 数字 / 禁猜路径 / 禁拿整体口径下结论）。触发词 = "复盘 / 最近表现 / 哪些判据到点了"。
**不并入**既有 `review-system-design` skill：那是**设计时**加载的方法论，与**运行时**读数接口混装会让每次会话都加载双份。

### 16.2 产物与代码为什么不能做成 skill（三条判据，含实测）

1. **skill = 上下文注入。** 每日 bundle 进 skill ⇒ 每次相关会话都注入一份会过期的数字，与既有 `hermes-context-budget-audit` 的结论直接冲突；且 skill 一旦承载"今天读数"，就变成每天必须被改的文件 —— 改它的人/agent 成为新单点。
2. **代码放 skill 目录 = 自己变成下一个孤儿（实测）。** `scripts/carrier_orphan_census.py:26` 的可执行体语料是 `(REPO/scripts, REPO/app, REPO/ops, ~/.hermes/scripts, REPO/tests)` —— **不含 `skills/`**。全库 **279** 个 skill-dir `.py`（`deep-stock-analysis/scripts` 一项 **6.9M**）对"死亡不可见"普查完全隐形。把本 DEB 要修的病灶（判据静默成熟 / 环停了无人察觉）复制到新系统上，代价最高。
3. **口径必须同轴。** 只能调 `stage_stats` / `trade_identity` / `et_time`；skill-dir 副本会在 NULL / 时区 / 排除行上与 repo 分叉，产出"两个都真但对不上"的数字（违反 §5.2 铁律）。

**反方（保留原案的条件）**：`deep-stock-analysis` / `deepulse` 确实是"整条管线 + skill 目录脚本"形态。不照抄的理由是**消费者不同**（那两个的消费者是 agent 会话，核心是 LLM 编排知识，须与脚本同处）；且该形态已付代价：skill-dir 代码不进 repo 测试、不进孤儿普查、审计需跨两处查。若仍要走该形态，**前置条件是**把 builder 的测试留在 `tests/review/` 并在 skill 里逐字声明口径来源 —— 成本高于收益，故不推荐。

### 16.3 载体 stdout 契约：**成功静默**（与 `gate_liveness_carrier` 相反，必须显式声明）

- 推送军规（C5）+ 单一消费面 ⇒ 推送**只**由 `app.push` 发出（`scripts/review_bundle.py --push`）。
- 故载体 wrapper 成功路径**不输出**（Hermes 侧记静默/未投递），只在 rc≠0 时输出载体级异常行 ⇒ Hermes 记 failed。
- 与 `gate_liveness_carrier`（rc=0 原样投递 13 行探针输出）**相反**的原因：那个子脚本**不自推**，输出本体即产物；本环子脚本**自推**，wrapper 再输出就是双推（`387aa5e6a18b` 行的"零 stdout = 不推送"同族）。
- **实测（2026-09-19，`crontab -l` 逐行 + `~/.hermes/cron/jobs.json` 逐 job）**：每交易日 CST 05:40–06:35 已有 **8 个投递槽位** —— 报表/推送类 4 条 `da_chain_health 06:05 · shadow_board 06:15 · llm_token 06:20 · data_freshness 06:25`（dow 均 `2-6`）+ Hermes 侧 4 条 `dc双口径 05:40 · insider 06:00（常态静默、异常才出声）· D2'影子 06:10 · alpha-backfill 06:30`。周六另有 `trigger_gate 06:24 · 记分牌合集 06:25 · knife_gate 06:26`。
  ⇒ §7 定的 **06:30 与 `alpha-backfill`（Hermes job，每交易日投递一行摘要）撞同一分钟**，且该窗口已不是"再加一条"的量级。**建议改 06:35 CST**（06:26 之后、`spy_crash_watch 06:55` 之前），或按 16.6 归并。

### 16.4 落点清单（逐条可核，供复查 agent 按行核）

| # | 落点 | 谁在管它 |
|---|---|---|
| 1 | `app/review/{bundle_schema,bundle_builder,bundle_render,criteria_status}.py` | `tests/review/` + §9 全量回归（5 个既有测试文件不得变红） |
| 2 | `scripts/review_bundle.py`、`scripts/review_coverage_scan.py` | §9 P0/P3 验收命令 + C14 golden |
| 3 | `ops/hermes/review_bundle_carrier.py` + 线上副本 | `tests/test_ops_scripts_sync.py` 双向 sha256（新文件不回库即方向2报警） |
| 4 | Hermes cron job 3 条（日/周/月） | `~/.hermes/cron/jobs.json` + `docs/CONTROL-LOOP-LEDGER.md` 入账行（照 `705a1f2019d1` / `387aa5e6a18b` 写法：id / 调度 / 判据零变更声明 / 失效模式 / 回滚） |
| 5 | `data/reviews/*`（runtime，不入库；`data/*` 已 gitignore） | 无 |
| 6 | `~/.hermes/skills/devops/ftseek-review-bundle/SKILL.md` | 人工评审（膨胀即视为失效） |
| 7 | 本文 + RFC-088 预注册 | git |

### 16.5 连带更正（**两处自曝**，均在写本节时实测发现）

- **更正 A —— §13.3 的退役候选 4 项里 3 项站不住**：
  - `exit_scorecard` / `direction_hit_scorecard`：已于同日 15:29（`15cf56963`；台账 `docs/CONTROL-LOOP-LEDGER.md:148` 行 `705a1f2019d1`）由「载体迁移②批」升格为**有投递面的周六记分牌合集**（Hermes no_agent；crontab 两行并行保留）。本 DEB 写于其后，却仍按"只 `>> logs` 无读者"列为可退役 ⇒ **必须剔除**，否则等于撤销刚落地且已批准的载体迁移。
  - `overnight_arm_report`：它是 `shadow_registry` 中 **`overnight_kill_criterion`（状态 `OBSERVING`, `min_n=20`）** 的 `eval_script`（`app/playbook/shadow_registry.py:516`），其 stdout JSON 正是该条目的计数锚（`data_path=data/shadow/overnight_arm_report_latest.json`）。退役它 = **废掉一个在册判据的 eval_script 与计数锚**，并与 §5.6"复用 registry 到点上报"自相矛盾（那正是本系统第一件该报的事）。
  - 真候选只剩 **`fee_unit_economics_scorecard`**：全仓读者面实测仅两处**口径注释文本**（`scripts/t5_hold_counterfactual.py:16`、`scripts/ticket_size_breakeven.py:71/389`），不在注册表，产物 `data/fee_unit_economics_latest.json` 无读者 ⇒ 符合"只 `>> logs` 僵尸尺子"判据（`crontab -l:180`）。
- **更正 B —— "按日 dedup"这一机制在 `app.push` 里不存在**：`app/push/hub.py:124` 为 `if not sync and not self._dedup.should_send(dedup_key)` ⇒ **`sync=True` 完全绕过去重**；`dedup_key` 本身只是 `app/push/dedup.py` 的 **60s 进程内 LRU**。真正的"同日只推一次"由**调用方自有状态**实现 —— `scripts/trigger_gate_report.py:376-380` 是先在自有历史里查 `day in prior_anchor_days` 才推。
  ⇒ §5.1/§7/§14 中"`dedup_key` 生效 ⇒ 推送恰好 1 条"**不成立**；`scripts/review_bundle.py` 必须自带按日守卫（读 `bundle_<date>.json` 的 `pushed_at`，或独立 state 文件），否则同日重跑 = 第 2 条推送，且 P3 幂等验收会因此**假绿**。

### 16.6 与 §13 待决项的耦合（不改写原问题，只给耦合事实与建议）

| §13 | 本节结论 |
|---|---|
| 1 推送节奏 | 06:30 已是早间第 9 个槽位且与 `alpha-backfill` 撞分钟（16.3 实测）⇒ **建议 06:35**。宜并入的**只有报表类两条**：`da_chain_health_report --push`(06:05)、`daily_llm_token_report --push`(06:20)；`shadow_board`(06:15，影子盘面) 与 `data_freshness_watchdog`(06:25，**告警类**) **不并** —— 告警并进日报会延迟其到达。并入方式 = 关掉这两条 `--push`、其输出作为 bundle 的两个 section；属**独立变更**，不在本 DEB 授权范围，需单独裁决。 |
| 2 READY 即时单推 | 载体已定为"按日 1 条 + caller 侧按日守卫" ⇒ 即时单推 = 当日再 +1 条。**建议只进 06:35 那条的"待裁决"段**；仅当 §13.4 批准"逾期升级"时，才对逾期项另发（`Level.HIGH` + 独立 caller 侧日期守卫）。 |
| 3 首批退役 | 见 16.5 更正 A：候选 **4 → 1**（仅 `fee_unit_economics_scorecard`）。 |
| 4 逾期升级 | 与 2 同源：升级告警必须有 caller 侧状态（否则每日重发同一条），不能靠 hub dedup。 |
| 5 回填 30 天 | 无载体耦合；但回填时 `pushed_at` 守卫必须对历史日**只写产物不推送**，否则一次性积压 30 条。 |

### 16.7 新增复查项（并入 §10 清单，编号续 C14）

| # | 待查声称 | 复现 | 反例（判为不成立） |
|---|---|---|---|
| C15 | "死亡不可见"普查语料**不含 skills/** ⇒ 代码放 skill 目录即隐形 | `sed -n '25,27p' scripts/carrier_orphan_census.py`；`find /root/.hermes/skills -name '*.py' \| wc -l` | 语料含 `skills` ⇒ 16.2 判据 2 削弱 |
| C16 | `sync=True` **绕过** hub 去重，`dedup_key` 仅 60s 进程内 | `sed -n '107,130p' app/push/hub.py`；`grep -n 'ttl_sec' app/push/dedup.py`；对照 `scripts/trigger_gate_report.py:376-380` 找"同日只推一次"的**实际**实现位置 | 若 sync 路径仍查 dedup ⇒ 16.5 更正 B 作废（但 §14 的"恰好 1 条"仍须实测） |
| C17 | §13.3 的四项退役候选里 3 项有活读者/哨兵 | `grep -n 'exit_scorecard' ops/hermes/weekly_scorecard_carrier.py`；`grep -n 'overnight_arm_report' app/playbook/shadow_registry.py`（应命中 516 行 `eval_script`）；`python3 -c "import json;print([x['name'] for x in json.load(open('/root/.hermes/cron/jobs.json'))])"` | 三者皆无读者且不在注册表 ⇒ §13.3 原判定成立、16.5 更正 A 作废 |

### 16.8 回滚（本节引入的额外面）

```bash
# 载体面: 删 Hermes job 3 条 + 删 ops/hermes/review_bundle_carrier.py 与线上副本
#         (必须两处同删, 否则 tests/test_ops_scripts_sync.py 方向1 立刻红)
# skill 面: 删 ~/.hermes/skills/devops/ftseek-review-bundle/
# 代码/产物面: 见 §12
```

---

## 17. 第 1 轮独立复查结论（两个复查 agent + 作者逐条复核）

> 复查方式：两个**只读** agent 并行，A 组跑 C1–C9（数据类），B 组跑 C10–C17 与 §16 对抗审查（代码/基础设施类）；两份报告回来后**由作者逐条复核**（本仓纪律：子代理自报约 25% 误报，声称必查 DB/源码结局）。
> 结论分三类：**成立**（原文保留）/ **更正**（R-x，本节给出实测新值，受影响节以 R-x 为准）/ **误报**（复查 agent 的判定被复核推翻，记录在案防止后续 agent 采信）。

### 17.1 逐条判定

| # | 复查判定 | 作者复核 | 处置 |
|---|---|---|---|
| C1 | 成立 | 一致：`stage_stats.stage_breakdown(rows_from_db(...))` 得 premarket 4/−648.86 · regular 329/+340.14 · afterhours 2/+110.75，合计 335/−197.97 | 原文保留 |
| C2 | 第 2 轮更正：仅按 app/scripts 范围成立 | `ops/hermes/ftseek_weekly_checkpoint.py` 为 Hermes 周六 job 程序化调用 `refresh_counts/summary/check_mounting`；缺口是日级/裁决闭环，不是零生产消费者 | §1.2/§4/E2/E3/§15 已改述 |
| C3 | 成立 | 复查实测条目数 **36**（非 30+）；`class ShadowItem` 含 status/min_n/count_mode | 保留，条目数改 36 |
| C4 | 成立 | 一致：`entry_archetype_routing.jsonl` 不存在；另实测 36 条 `data_path` 里 34 为文件路径、**33 存在 / 1 缺失**（`data/trigger_miss_telemetry.jsonl`，其值带 `(reason=...)` 条件说明）⇒ "猜路径⇒计数静默归零"风险**已有 1 个实例** | 保留并加实例 |
| C5 | **不成立** | 复核确认（见 R-1） | §4/E6 更正 |
| C6 | 成立 | 复核确认（见 R-3）：分布 **547 `-04:00` / 327 无偏移 / 1 `+00:00`**（共 875） | §5.2 影响面更正 |
| C7 | 成立 | 一致：`duplicate_external_close` 5 行、带净额 0 行 | 原文保留 |
| C8 | 成立 | 一致：2026-09-18 四类 `skip_reason` 均存在（`非当前通道候选` 59 / `方向偏斜超限` 7 / `chase_cap_enforced` 1 / `admission: PRICE_TOO_LOW` 1 / `low_atr` 1） | 原文保留 |
| C9 | 成立（约数需精确化） | 一致且逐值相同（5→26.304 / 10→13.204 / 20→6.653 / 30→5.112 / 50→4.703）；精确比值 **5.59×** 非 6×；$30 档名义实为 4980（`int(5000/30)=166`） | 保留，比值写 5.6× |
| C10 | 部分不成立 | 54 总数成立；**心跳底噪实测 16（非 15）** | 数字更正 |
| C11 | 无法判定 | 设计稿未落码，无产物可测；风险点见 17.3(3) | 保留待 P3 实测 |
| C12 | 成立 | 一致：本轮提交仅动 `docs/debates/*.md`；§5.6/§9 P4 对注册表是**只读 import**，不改其源码 ⇒ 不算触碰 `app/playbook/*` | 原文保留 |
| C13 | **不成立** | 复核确认（见 R-2） | §5.5 输入更正 |
| C14 | 成立 | 一致：`git check-ignore` 返回空（`tests/` 未忽略）；`data/*` 由 `.gitignore:46` 忽略 | 原文保留 |
| C15 | 成立 | 一致：语料元组原文 `(REPO/"scripts", REPO/"app", REPO/"ops", HERMES/"scripts", REPO/"tests")`；skill-dir `.py` = **279** | §16.2 判据 2 成立 |
| C16 | 成立 | 一致且更强：复核另见 `hub.py:146` `self._send_and_mark(msg, dedup_key if not sync else None)` —— **sync 路径连 `mark_sent` 都不调**，靠"发送成功后占窗"兜底也不可能生效 | §16.5 更正 B 加固 |
| C17 | 成立 | 一致：(a) `ops/hermes/weekly_scorecard_carrier.py` 调两张记分牌且 Hermes job `705a1f2019d1` enabled；(b) `shadow_registry.py:516` = `overnight_kill_criterion`（OBSERVING, min_n=20）的 `eval_script`；(c) `fee_unit_economics` 不在注册表、仅 3 处注释文本 ⇒ **退役候选 4→1** | §16.5 更正 A 成立 |

### 17.2 更正（R-1 … R-5，均经作者复核）

- **R-1（推翻"推送军规已落实"）§4/E6**：军规原文只存在于 `scripts/trigger_gate_report.py` 的 docstring，**仓库实际违反面 25+ 处**：20 个生产脚本直连 `app.notifier.feishu.send_text`（不经 `app.push` hub），5 个脚本自建 `requests.post` / `urllib.request.urlopen`，crontab 另有 2 行 `curl` 直连钉钉 webhook 兜底（`data_freshness_watchdog` / `cutover_preflight`）。
  **复核证据（作者实查）**：`scripts/da_chain_health_report.py:174` `from app.notifier.feishu import send_text` —— 而 `app/notifier/feishu.py` 是**直接发 webhook** 的模块（`send_text` → `self._send(payload)`），不是 hub。
  **后果**：§13.1 拟并入的两条候选（`da_chain_health_report` 06:05、`daily_llm_token_report` 06:20）**本身就是违反者**。并入前必须先决定：是改这两条的推送路径、还是承认"单一消费面"只能对新模块成立（旧模块继续绕 hub）。**该决定不在本 DEB 授权内**。
- **R-2（§5.5 功效输入 σ 更正）**：原文"单笔净 σ≈1.18% ⇒ 检出 +0.15% 需 n≈481；n=56 CI 半宽 ±0.31pp"**不可复现**。实测（口径 `pnl_pct` 百分数、已平仓 `exit_time IS NOT NULL AND pnl_net IS NOT NULL`、排除 `duplicate_external_close`）：

  | 切面 | n | σ |
  |---|---|---|
  | 全量已平仓 | 870 | **1.8977%** |
  | 2026-06-27 后（playbook 时代） | 526 | 1.5422% |
  | 最近 56 笔 | 56 | 1.3723% |
  | 窗口 08-01..09-18 | 335 | 1.5440% |
  | 空 `entry_session` | 398 | 2.3103% |
  | 盘前 / 盘后 | 4 / 2 | 2.6033% / 0.1202% |

  **没有任何切面接近 1.18%**（美元口径 σ/平均名义 = 4.95%，也不是它）。按 σ=1.8977% 重算：检出 +0.15% 需 **n≈1258（非 481）**；n=56 的 95% CI 半宽 **±0.498pp（非 ±0.31pp）**，即 **n=56 连 ±0.5pp 都判不准**。且 σ 按阶段差异近 1.6 倍 ⇒ **功效声明必须按阶段分报**，不得全仓一个 σ。
- **R-3（§5.2 `exit_date_mismatches` 影响面）**：`exit_time` 实测三种格式混存 —— `-04:00` **547** / 无偏移（naive，样例 `2026-04-24T23:30:43.102159`）**327** / `+00:00` **1**（共 875）。naive 行若被 `date(exit_time)` 直接取日，与 ET 日**系统性偏 12–13h、可跨日** ⇒ 该校验的影响面是 **37% 的行**，不是 §4/E8 举的 1 行；维持 **P0 必报**，且建议同时给出"naive 行占比"作为口径健康度字段。
- **R-4（告警噪声底噪）**：当日推送 54 条成立，其中**心跳 16 条**（非 15）⇒ §8 起点条件 3 的基线按 16 计。
- **R-5（费用率精确比值）**：$5/$50 档费率比 **5.59×**（文档写"6 倍"）；$30 档名义 4980 非 5000（复现命令的整数截断）——写文档时按 5.6× 与"名义≈5000"表述。

### 17.3 复查 agent 的误报（记录在案，后续 agent 勿采信）

- **误报 1（其唯一"否决"项，被作者复核推翻）**：复查 agent 判定 §16.3 的"`dc双口径` 05:40 / `alpha-backfill` 06:30 / `spy_crash_watch` 06:55"是**过时台账引用**，理由是"当前 `jobs.json` 仅 17 个 job，id `2254de46fe64`/`6af0648e3eb3` 不在其中"。
  **作者复核**：`~/.hermes/cron/jobs.json` 实有 **34** 个 job；上述三个 id **均在册且 `enabled=True, no_agent=True`**（`2254de46fe64` = dc双口径日对比推送 / `6af0648e3eb3` = alpha-backfill / `7d448f79c410` = spy_crash_watch）。⇒ 该否决**不成立**，§16.3 保留；其"幽灵碰撞"结论作废。
  **反向加强**：`06:30 CST`（dow `2-6`）另有 **3 条 crontab**（`exit_calibration_refresh` / `settle_eod_depth_gate_shadow` / `settle_extended_hours`）+ Hermes `alpha-backfill` ⇒ **同分钟 4 个作业**，"改 06:35"的动机比原文更强（不是"理由错结论对"）。
  **教训**：复查 agent 的"实测"也可能来自错误解析（此处疑似只解析到 jobs.json 的局部）—— **子代理的否决不能直接采纳，必须由作者在真文件上重跑一遍**。
- **误报 2（其建议未被采纳，理由如下）**：建议把 §16.1 的"薄 skill"替换为 `docs/review-bundle-usage.md`。**不采纳**：该建议的立论是"markdown 同样能让 agent 知道路径/命令，且进 git 可 diff"，但它自己承认代价是"agent 需手动 read_file" —— 而本系统的原始诉求正是**不再每天被动询问**，把读数入口从"自动加载"降级为"记得去读"等于回到原点。**采纳其缓解部分**：skill 内不写命令模板全文，只放路径常量 + 一行 `scripts/review_bundle.py --help` 指针（脚本自描述），以消除"模板随 schema 漂移"的失效模式。

### 17.4 复查提出的、本 DEB 接受的三项设计约束（并入 §9/§14）

1. **确定性**：`bundle_builder` 所有 SQL 必须带 `ORDER BY`；切片排序用显式元组键；禁用 `set()`；浮点聚合固定累加序（否则 golden 比对会随机红）。
2. **按日守卫的并发面**：同日重跑、时区跨日、回填 30 天都好办；**并发（cron + 手动 + 回填同时）最易漏** —— 两个进程同时读到"未推送"会各推一条。最小实现：`fcntl.flock(<bundle>.lock)` 独占锁内**重读** `pushed_at`，推完写回再释放。
3. **`test_ops_scripts_sync.py` 报错文案已知误导**：方向1 对新文件漏部署报的是"库内有, 线上已**删除**"（与"未部署"语义相反）⇒ 分诊时勿按字面理解。

---

## 18. 第 2 轮复查执行记录（2026-09-19）

1. **事实更正**：`shadow_registry` 不是零生产消费者；Hermes job `0e87d094dcdf`（`5 9 * * 6`）调度 `ops/hermes/ftseek_weekly_checkpoint.py`，程序化调用 `refresh_counts/summary/check_mounting/check_intent_uniqueness`。缺口改述为：周六有汇总，但无日级闭环、无持久 READY 时间、无裁决收口。
2. **READY 防噪设计**：P4 将新增最小状态 `data/reviews/criteria_ready_state.json`，仅存 `id→ready_since`，在 flock 内更新/销账；日推送只报 `ready_count` 与最久前 5 项，全量清单留在 JSON/MD。
3. **推送守卫**：P3 验收改为 caller 侧 `pushed_at` + `fcntl.flock`；发送成功才标记，失败不得标记；Hub `dedup_key` 不作为同日闸。
4. **时间口径落地**：`phase_from_time()` 对 naive 输入统一按 CST→ET（与 `app.utils.timezone.to_et` 同尺）；`stage_stats.load_trades()` 改为 ET 出场日过滤、显式排除 `duplicate_external_close`、并按 `exit_time,id` 排序。当前 C1 读数不变（335 / −197.97）。
5. **推送出口预热**：`extended_hours_entry_executor._dedup_reserve()` 增加 `fcntl.flock` 与进程唯一临时文件，避免并发双读/双写；相关守卫测试通过。
6. **回填闸门**：在上述时间口径测试覆盖稳定前，禁止用现行口径回填历史 `bundle_*`；回填必须另走裁决并附全量差异表。
7. **验证**：阶段统计/日报/周报/OKR/身份契约/推送守卫/延长时段组合 ⇒ 147 passed；相关文件 `py_compile` 与 `git diff --check` 通过。主服务未重启，避免把工作区并行未提交的延长时段改动一并生效。


---

## 19. 第 2 轮复查（外部 agent）核查确认（作者独立复核，2026-09-19）

> 第 2 轮由另一 agent 提交（工作区 `§18` + `app/review/stage_stats.py` / `app/core/trade_identity.py` 改动）。本节是**作者逐条复核**的结果：**确认成立 / 我原判被推翻 / 我补的量化 / 待确认**四类。
> 纪律同 §17：不采信描述，全部在真库/真文件上重跑。

### 19.1 结论摘要

- **Codex 的核心更正成立，且推翻了我的一处原判**（19.2-A）：我 §1.2 的"`shadow_registry` **零生产消费者**"**在写作当时就是错的** —— 消费者自 **2026-08-23** 起就存在（`ops/hermes/ftseek_weekly_checkpoint.py:181`，docstring 自陈"2026-08-23 接入, **原模块零引用**"）。**我的错误来源**：普查命令范围只写 `app/ scripts/`，漏了 `ops/hermes/` —— 而这正是 §16.2 我自己批评过的同一类**范围盲区**（当时批的是漏 `skills/`）。§1.2/§4/E2/E3 的改述（"消费面不足：周六有汇总，缺日级闭环/持久 READY 时间/裁决收口"）**准确**，且不削弱方案（G4 的日级上报仍是缺口）。
- 我的 §17 两项更正（R-1 推送军规未落实 / R-2 σ=1.8977% 及派生 n≈1258）**未被第 2 轮推翻**：`grep -c 'R-2（§5.5 功效输入 σ 更正）'` = 1、`1.8977` 出现 3 次、§5.5 的 `§17/R-2` 指针在位 ⇒ 两轮结论可叠加，不冲突。
- 第 2 轮新增的**设计改进**（作者认可，优于我 §16.6 的原答案）：P4 增加最小状态 `data/reviews/criteria_ready_state.json`（只存 `id→ready_since`），日推只报 `ready_count` + 最久前 5 项。**实测依据**：`REGISTRY=36`、`READY_TO_EVALUATE=19` —— 19 项若每日全量进推送就是刷屏，我原答案只说"折进日报段"没量化这一步。

### 19.2 逐条核实（作者复现命令与结果）

| # | 第 2 轮的声称 | 我的复现 | 判定 |
|---|---|---|---|
| A | `shadow_registry` 非零消费者，Hermes 周六 job 调用 `refresh_counts/summary/check_mounting/check_intent_uniqueness` | `sed -n '181,190p' ops/hermes/ftseek_weekly_checkpoint.py`（子进程 `python -c` 调四个 API）+ `git log -S check_shadow_registry` ⇒ 引入于 RFC-060/ADR-036 线，**早于本 DEB** | **确认**（且推翻我原判） |
| B | 当前 19 项 READY 会刷屏 | `refresh_counts(); len(get_ready_to_evaluate())` ⇒ `REGISTRY 36 / READY 19` | **确认** |
| C | `phase_from_time` naive 统一按 CST→ET（与 `to_et` 同尺） | `app/utils/timezone.to_et` docstring 明写"naive 视为北京时间"；改动后 `normalize_stage("", naive)` 与 `to_et` 同尺；新增测试 `test_naive_clock_fallback_uses_cst` | **确认** |
| D | `load_trades` 改为 ET 出场日过滤 + 排除 `duplicate_external_close` + `order by exit_time,id`；"当前 C1 读数不变（335/−197.97）" | 实跑：`stage_breakdown(rows_from_db(...,'2026-08-01','2026-09-18'))` ⇒ `premarket 4/−648.86 · regular 329/+340.14 · afterhours 2/+110.75 · sum 335/−197.97` **逐格不变** | **确认** |
| E | 测试与回归通过（其称 147 passed） | 我跑 7 个相关文件（阶段统计/日报/复盘表/校验记账/OKR/统一推送/盘后持夜）⇒ **104 passed**；`tests/test_stage_stats.py` 13 passed（含新增 2 例） | **确认**（我核到 104/147 的子集，全绿） |
| F | P3 守卫加 `fcntl.flock` + 发送成功才标记 | 文档 §9 P3 / §14 已按此改；与 §17.4-2 一致 | **确认** |
| G | 主服务本轮未重启（改动由短命进程生效） | 服务侧唯一调用方 `app/pipeline/order_executor.py:1285/1328` 调的是 `phase_from_time()` **无参**（dt=None ⇒ 走 `now_et()`）⇒ 本次改动的 naive 分支**对运行中服务不可达**，确实无需重启 | **确认**（该项原本是我要提的疑点，实测不成立） |

### 19.3 第 2 轮未给、由本次核查补上的量化：口径改动的影响面

`load_trades` 的窗口过滤从 SQL `date(exit_time)` 改为 **ET 出场日**。实测影响（`exit_time` 非空且 `pnl_net` 非空，n=870）：

- **201 行**的 `ET 日 ≠ 字符串日`（全部是 naive 行；naive 共 327 行，日期范围 `2026-04-24 … 2026-07-20`）。
- 该差异**只在窗口端点落在错位日时显形**：逐日窗口扫描 `2026-04-20 … 2026-07-25` 共 97 天 ⇒ **34 天读数改变**，例：

| ET 日 | 旧 `date()` 口径 | 新 ET 口径 |
|---|---|---|
| 2026-04-27 | n=1 / −6,034.87 | n=5 / −983.51 |
| 2026-04-28 | n=4 / +5,051.36 | n=0 / 0.00 |
| 2026-05-01 | n=7 / +56.66 | n=13 / +1,627.35 |

- **近期窗口不受影响**：`2026-08-01..09-18`（335/−197.97）、`09-16..09-17`、全期 `04-01..09-18`（870/+12,988.21）**新旧逐格相同** —— 因为窗口内无错位行，且 5 条 `duplicate_external_close` 的 `pnl_net`/`realized_pnl` 本就为 NULL（F1 约定成立，显式过滤在**当前数据上是 no-op**，属防御性加固）。
- ⇒ 结论：**新口径是对的**（naive = 北京时间 ⇒ ET 出场日才是真实交易日），但它**重基了 4–7 月的逐日统计**（34 天）。凡引用该期逐日读数的历史结论都要标注口径或重算；`§18.6 回填闸门`因此更有必要。
- 附：`_exit_date_et` 解析失败返回 `""` ⇒ 该行被**静默跳过**。当前库内 `exit_time` 无法解析行数 = **0**（旧路径对垃圾值同样取不到日期，非回归），但建议把它计入 `anomalies.unparsed_exit_time`，符合"宁可显性报错"。

### 19.4 待确认 / 建议归位（非否决）

1. **代码是否提交**：`app/review/stage_stats.py`、`app/core/trade_identity.py`、`tests/test_stage_stats.py` 原先**只在工作区**，而 crontab/Hermes job 一律 `cd 仓 && venv/bin/python scripts/...`（**读工作区**）⇒ 属本仓已知的"生产在跑未入库代码"病灶（RFC-060 专为此立）。核查中已按其纪律**单独立提交**（测试随行，可一键 revert）；若判定不该在提案裁决前落地，`git revert` 即可回到旧口径（旧口径影响面见 19.3）。
2. **§18.5 的归位**：`scripts/extended_hours_entry_executor.py._dedup_reserve()` 的 flock/临时文件改动属**延长时段执行链工作流**（同批还有 `scripts/premarket_exit_shadow.py`、`scripts/afterhours_exit_shadow.py`），不在本 DEB 的 §9 范围。本 DEB 头声明的"不触碰 `app/playbook/*`"仍成立（改动集为 `app/core`、`app/review`、`scripts/`、`tests/`，逐条核过），但 §18.5 宜移入对应 DEB 以免读者误以为本方案改了执行链。

---

## 20. 待裁决项的明确建议（作者，2026-09-19）

> 用途：§13 列的是**问题**，本节给**建议**，让裁决对着具体方案做（建议 / 理由(实测) / 风险 / 保守替代）。
> 每条都标注**能不能现在做**与**前置依赖**；凡建议与前面章节冲突，本节给出冲突理由。

### 20.1 一页总表（按建议执行顺序）

| 序 | 项 | 我的建议 | 前置 | 现在能做? |
|---|---|---|---|---|
| 1 | §13.5 历史回填 30 天 | **不做全量回填**；只做"上线日锚定 + 一份 30 天对照单"（只写产物、不推送） | **须等 §19.3 的口径重基尘埃落定**（4–7 月 34 天读数刚被重基） | 否（有前置） |
| 2 | §13.1 推送节奏 | **改 06:35 CST**；**本期不并入**那两条日报 | 并入前须先处置 R-1 违反面 | 是（时刻）／否（并入） |
| 3 | §13.2 READY 上报 | **只进日报的同一条推送**（`ready_count` + 最久前 5 项 + "本次新到期"小节靠前）；**不即时单推** | 无 | 是 |
| 4 | §13.4 逾期升级 | **要做**，但设双门槛：`READY` 且**逾期 >14 天**才升 P1，且每天最多一条、走 caller 侧状态 + `flock` | 依赖 3 的 `ready_since` 状态 | 是（实现随 P4） |
| 5 | §13.3 首批退役 | **只退役 `fee_unit_economics_scorecard`**（4→1），按"先注释 cron → 30 天无反弹 → 再删" | 无 | 是 |
| 6 | §17 R-1 军规违反面 | **新模块严守军规；老模块不动**，但把"推送出口违反面"做成**只读普查产物**进 P0，不静默 | 无 | 是 |
| 7 | 开工范围 | **先落 P0+P1**（只读覆盖扫描 + 证据契约红测试）；P2–P5 等本次裁决 | 无 | 是 |

### 20.2 逐项展开

**① 历史回填（§13.5）——建议：不做全量回填，只做"锚定 + 对照单"**
- **理由**：刚刚落地的时间口径修正（§19.3）**重基了 4–7 月 34 个单日窗口**的读数（最差 `04-27` 由 −6,034.87 变 −983.51）。在这个口径还在动的时候回填 30 天，等于把"会变的历史"写进产物，之后每次口径微调都要重写一遍，且无法区分"当时真发生的"和"今天口径算出来的" —— 正是你担心的**事后合理化**。
- **保守替代**：只生成 `bundle_<上线日>.json` 一个锚点 + 一份 `REVIEW_backfill_30d.md`（口径标签写死"以今日口径重算，非当时口径"，**不推送、不进 vault**）。等口径连续 30 天零变更后，再议回填。
- **风险**：不做回填 ⇒ 上线首周的周/月报窗口样本不足。缓解：月报窗口不足时按 §5.5 写"证据不足"，不硬给结论。

**② 推送节奏（§13.1）——建议：时刻改 06:35；本期不并入**
- **理由（时刻）**：实测 06:30 CST（dow `2-6`）已有 **4 个作业**同分钟：Hermes `alpha-backfill` + crontab `exit_calibration_refresh` / `settle_eod_depth_gate_shadow` / `settle_extended_hours`。06:35 落在 `06:26` 之后、`spy_crash_watch 06:55` 之前，是早间窗口唯一的空档。用 06:35 还顺带避开"结算器正在写库"的争用。
- **理由（不并入）**：拟并入的两条（`da_chain_health_report` 06:05 / `daily_llm_token_report` 06:20）**本身就是 R-1 的军规违反者**（直连 `app.notifier.feishu`，不经 hub）。把它们并进新系统 = 把违反面固化进"唯一消费面"，之后更难改。且语义不同：一条是**链路健康**、一条是 **LLM 成本**，都不是"交易复盘"的结果面 —— 硬并会让"结果优先"的头条变糊。
- **保守替代（推荐现在做）**：在新 bundle 里加一节 **"当日投递面清单"**（只列当日各推送的**标题/时刻/来源**，不搬内容）。这零耦合地解决"每天被动询问"的痛点——你一眼能看到早间 8 条都说了什么，而不必先改别人的推送路径。等违反面处置完（§20.2⑥）再议真并入。
- **风险**：不并入 ⇒ 早间仍是 9 条。缓解：投递面清单让"多"变得可读；若两周后你仍觉得吵，届时违反面也已清楚，再并。

**③ READY 上报（§13.2）——建议：只进日报那一条，不即时单推**
- **理由**：实测当前 `REGISTRY=36 / READY_TO_EVALUATE=19`。若"进 READY 就单推"，首日就会有 19 条（或一条里塞 19 项）；这不是告警，是**常态**。真正的事件是"**新**到期的项"，一周约 1–3 个。
- **做法**：`ready_since` 状态（§18.2 的设计）区分"新到期"与"早已到期"；日报推送里给 `ready_count` + 最久前 5 项，"新到期"小节置顶。**保持日推 1 条**。
- **风险**：日报被忽略 ⇒ 新到期没人看。缓解：④的逾期升级是兜底；且"新到期"同时进 vault 页与 JSON（可被 grep）。

**④ 逾期升级（§13.4）——建议：要做，但带双门槛**
- **理由**："被动询问"要变成"系统催办"才有意义；但无门槛会退化成噪声（19 项常态）。双门槛 = ①状态在 `READY` ②`ready_since` 距今 **>14 天** ③每天最多一条、只列最久的 1 项。
- **保守替代**：先只"计数进日报"（不升 P1），观察两周看有没有人真去裁；若两周内零裁决，再开升级。**我倾向直接开门槛版**，因为你要的是"到点有人催"。
- **风险**：升级告警触及 `Level.HIGH`，与本仓"告警不刷屏"纪律冲突。缓解：14 天门槛 + 每日一条 + 销账即停（离开 READY 自动销账，§18.2）。

**⑤ 首批退役（§13.3）——建议：只退 `fee_unit_economics_scorecard`**
- **理由**：逐项复核（§16.5/§17.1-C17）：`exit_scorecard`/`direction_hit_scorecard` 当天刚有投递载体；`overnight_arm_report` 是在册 `OBSERVING` 判据的 `eval_script`（退役它=废判据）。只剩 `fee_unit_economics_scorecard`：不在注册表、产物 `data/fee_unit_economics_latest.json` 无读者，全仓仅两处**注释式**引用（`t5_hold_counterfactual.py:16`、`ticket_size_breakeven.py:71/389`）。
- **做法**：先注释 `crontab:180` 一行，30 天无反弹再删脚本；删前把该口径说明补进那两个引用它的脚本注释里（避免"口径来源消失"）。
- **风险**：费用率尺子消失后，"费/毛利"类问题只能靠现算（§5.4 的 `费用%` 维度仍会算，只是没有独立记分牌）。

**⑥ R-1 军规违反面（§17.2）——建议：新模块严守，老模块不动，但必须可见**
- **理由**：一次"全部改走 hub"是跨 25+ 文件的改造，会与延长时段/推送收口等正在进行的工作流撞车；而"什么都不做"会让"单一消费面"永远只是对新模块成立。
- **做法**：把违反面做成 **P0 的只读扫描产物**（复用 `scripts/carrier_orphan_census.py` 的判据链，扩一条"推送出口"轴：`app.push` / notifier / 自写 webhook / curl 兜底 四类分档 + 行号）。让它在每周自审里可见、可排期，而不是靠记忆。
- **风险**：可见但无人修 ⇒ 变成又一张"僵尸表"。缓解：退役/合并任何推送类脚本站都必须先看这张表（与 §13.3 的退役流程绑定）。

**⑦ 开工范围——建议：先 P0+P1**
- **P0（只读，零行为变更）**：`review_coverage_scan.py`（静态引用扫描）+ 本次新增的**推送出口普查**；语料范围**必须**含 `app/ scripts/ ops/ tests/ ~/.hermes/scripts/ /root/.hermes/skills/` —— 这正是我今天犯过的范围盲区（§19.1），写进脚本注释防复发。
- **P1（证据契约）**：`bundle_schema.py` + `tests/review/` 红测试（缺 evidence 必抛、产物类必带 `integrity` 四键）。
- **不做**：不改任何推送路径、不动 cron、不回填、不退役。

### 20.3 审计局限（我还没验证 / 依赖假设的部分）

1. **R-1 违反面计数**（20 脚本 / 5 自写 webhook / 2 条 curl）来自第 1 轮复查 agent 的 grep；我**只抽验了 `da_chain_health_report.py:174` 与 2 条 crontab curl**。全量清单要在 P0 里用脚本重数（这也是⑥要做成产物的原因）。
2. **`ready_since` 的实际新到期速率**（我估"一周 1–3 个"）**未经历史验证** —— 注册表现在只记 `last_evaluated`，没有状态迁移时间线，无法回算。P4 落地后才有真数据；③④的门槛可能要按真实速率调一次。
3. **06:35 是"空档"**的说法基于 crontab + Hermes jobs 的**当前**快照；延长时段链路正在被别的 agent 改造（§19.4），上线前需再核一次该分钟。
4. **§5.5 功效门槛**在 σ 更正后（§17.2 R-2）尚无"按阶段"的完整口径 —— 建议 P2 出数时同时打印各阶段 σ 与 n，供你判断哪些阶段只能"方向性"读。

---

## 21. P0+P1 执行记录（2026-09-19）

> 裁决：用户授权"先 P0+P1，且不改交易行为"。本节只记录已执行事实；后续 P2–P5 仍需按 §20.7 的顺序另行落地。

### 21.1 交付与验证

1. **P0 覆盖扫描**：新增 `scripts/review_coverage_scan.py`。语料为 `app/`、`scripts/`、`ops/`、`tests/`、`/root/.hermes/scripts/`、`/root/.hermes/skills/`；测试引用会出现在 refs 里，但 `tests` scope 不计入生产消费者。产物原子写入 `data/reviews/coverage_<date>.json`，stdout 输出紧凑摘要。
2. **P1 证据契约**：新增 `app/review/bundle_schema.py` 与 `tests/review/`。契约固定 `SCHEMA_VERSION="review_bundle/1"`；证据 kind 只允许 `sql/artifact/log/derived`；指标单位只允许 `usd/pct/pp/count/ratio/text/bool`；每个 Metric 必须有证据，缺失报错携带完整路径（如 `headline.net_pnl`）；`artifact/derived` 必须带 `n/window/universe/truncation_checked` 且逐项校验类型。
3. **测试**：`venv/bin/python -m pytest tests/review -q` 通过 **8/8**；`py_compile` 通过；`git diff --check` 无问题。
4. **真实扫描**：`time venv/bin/python scripts/review_coverage_scan.py` 约 **13 秒**完成；共 **1,699** 个代码文件（`app=474`、`scripts=460`、`ops=35`、`tests=405`、`hermes/scripts=32`、`hermes/skills=293`）。生成 `data/reviews/coverage_2026-09-19.json`。
5. **生产者普查**：`shadow_registry=36`（无 dead）、review-like crontab 输出 `22`，其中 **18 个 crontab 日志无代码消费者**；合计 producer `58`、dead `18`。
6. **推送出口普查**：命中文件 **164**；分类计数 `webhook=77`、`app.push=54`、`notifier=48`、`curl=17`。该计数是静态可见面，不代表每条路径都在同一执行链中实际发送。

### 21.2 复查修正

- 首版缓存实现后仍保留了旧版 `find_refs/push_exit_census/build_report`，后者覆盖前者导致重复读文件与高 CPU；已删除旧定义，只保留缓存版。
- 原根标签把 `/root/.hermes/scripts` 显示为普通 `scripts`，与仓内 `scripts/` 混淆；已改为 `hermes/scripts`、`hermes/skills`，并补防回归测试。

### 21.3 边界与未做

- **零交易行为变更**：未启动/停止服务，未改 cron、推送路径、`app/playbook/*`、历史回填、判据退役或任何交易参数。
- `dead_producers` 仅表示"当前代码语料中未找到非 tests 引用"，不是立即退役指令；18 个 crontab 日志需按消费价值逐项裁决。
- 当前扫描是静态字符串引用扫描，不能识别运行时动态拼接、数据库读取或尚未在工作区中的消费者；后续若发现漏报，必须补对应语料或显式白名单，不能静默放宽。
- `data/reviews/coverage_2026-09-19.json` 为运行产物，用于本轮审计，不作为历史口径锚点。

---

## 22. P2 执行记录（2026-09-19）

> 用户继续授权后按 §9 顺序推进。P2 仍为**结果层构建**：不落盘、不渲染、不推送、不改 cron 与交易行为。

### 22.1 交付

1. 新增 `app/review/bundle_builder.py`：
   - `build()` 生成 `Bundle`，headline 覆盖净额 / 毛额 / 费用 / 费毛比 / 笔数 / 胜场 / 胜率 / MFE=0。
   - `build_slices()` 复用 `app/review/stage_stats.py` 的阶段 SSOT，输出阶段 n / net / gross / wins / win_rate / mfe0。
   - `build_plan_vs_actual()` 按 `(symbol, direction)` 对齐 `playbook_instructions` 与 `trade_records`，报告执行、错过、计划外与方向错配。
   - `exit_date_mismatches()` / `build_anomalies()` 用 `app.utils.timezone.to_et` 校验 raw exit date 与 ET 出场日；错配为 `severity=P0`。
   - 所有 headline / slices / plan 指标均带 evidence；derived evidence 带 `n/window/universe/truncation_checked`。
2. 新增 `tests/review/test_bundle_builder.py`，使用临时 in-memory SQLite DDL，不写生产库。

### 22.2 生产对账（2026-09-18）

- 直查 SQL：`round(sum(pnl_net),2) = 34.56`。
- `build()` headline：`net_pnl = 34.56`。
- 阶段切片合计：`110.75 + (-76.19) = 34.56`。
- 样本：8 笔（盘后 2 笔 / +110.75；盘中 6 笔 / −76.19）；当日无 exit-date P0 异常。
- plan-vs-actual：计划 7，执行 6，错过 1，计划外 2，方向错配 0。

### 22.3 验证

- `pytest tests/review/test_bundle_builder.py -q`：5/5 通过。
- `pytest tests/review -q`：13/13 通过。
- `pytest tests/test_stage_stats.py -q`：13/13 通过。
- DEB 指定既有回归（stage / daily review / review tables / validation accounting / OKR）：74/74 通过。
- `py_compile` 与 `git diff --check` 通过。

### 22.4 边界

- P2 只提供构建函数；`bundle_<date>.json` 的原子写、幂等、渲染、CLI 与单条推送属 P3，尚未执行。
- plan-vs-actual 的“计划外 2”是 `US.ACN`、`US.WBD`，需 P3 报告中作为链路对账问题显性展示，不得静默吞掉。

---

## 23. P3 执行记录（2026-09-19）

> 继续按 §9 推进。P3 完成**渲染 + CLI + 原子幂等产物 + 可选推送闸**；本轮没有真实推送、没有写 vault、没有改 cron。

### 23.1 交付

1. 新增 `app/review/bundle_render.py`：
   - `render_md(bundle) -> (full_md, push_text)`。
   - full MD 包含结果、阶段切片、计划 vs 实际、P0 异常、逐指标 evidence 溯源。
   - push text 为短摘要：净额 / 笔数 / 胜率 / 阶段净额 / 计划执行 / P0 异常计数。
2. 新增 `scripts/review_bundle.py`：
   - 支持 `--date`、`--period day`、`--push`、`--vault`。
   - 原子写 `data/reviews/bundle_<date>.json`、`REVIEW_<date>.md`、`bundle_latest.json`。
   - 同日重跑除运行产物时间外逐字相同：本实现不写 `generated_at`，JSON/MD hash 稳定。
   - `--vault` 时才写 `obsidian_vault/daily/review/<date>_review.md`。
   - `--push` 走 `app.push.daily_review`，`sync=True`；caller 侧 `fcntl.flock` + `<date>.push.json` 状态，发送成功才标记，失败不标记。
   - `week/month` 参数显式拒绝，留待 P5，避免半成品周期口径。

### 23.2 真实运行（2026-09-18，无推送）

- 命令：`venv/bin/python scripts/review_bundle.py --date 2026-09-18`。
- 产物：
  - `data/reviews/bundle_2026-09-18.json`
  - `data/reviews/REVIEW_2026-09-18.md`
  - `data/reviews/bundle_latest.json`
- 两次运行 hash 完全一致；JSON 与 latest hash 一致。
- 报告显性给出：盘中 6 笔 / −$76.19，盘后 2 笔 / +$110.75；计划 7 / 执行 6 / 错过 1 / 计划外 2（`US.ACN`、`US.WBD`）；P0 异常 0。

### 23.3 验证

- `pytest tests/review/test_bundle_render.py tests/review/test_bundle_cli.py -q`：6/6 通过。
- `pytest tests/review -q`：19/19 通过。
- DEB 指定既有回归：74/74 通过。
- `py_compile` 与 `git diff --check` 通过。
- 推送测试使用 FakeHub：失败不落状态；成功落 `pushed_at`；重跑返回 already 且不再调用 Hub。

### 23.4 边界

- 本轮未执行 `--push` / `--vault`，未创建 push 状态，未改 Hermes job 或 crontab。**⚠️ 更正（§25.2 S-1）**：实测 21:27:53 CST 存在 `bundle_2026-09-18.push.json`（`pushed_at` 非空）且 `push_failures.jsonl` 无对应失败行 ⇒ **P3 阶段有过一次真实推送**；本句"未创建 push 状态"不成立。
- 当前 CLI 只实现 day；weekly/monthly 属 P5，不做假周期产物。

---

## 24. P4 执行记录（2026-09-19）

> 继续按 §9 推进。P4 完成**判据待裁决层**；仍不真实推送、不上 cron、不改任何交易参数。

### 24.1 交付

1. 新增 `app/review/criteria_status.py`（63 行，满足 ≤80 行约束）：
   - `pending_adjudications()` 只读 `shadow_registry.REGISTRY`，调用既有 `refresh_counts()` 在本进程内刷新计数与 READY 状态。
   - 输出 36 项待裁决清单，含 `id/status/n/min_n/progress/ready/criteria/artifact/eval_script/count_mode/evidence`。
   - `update_ready_state()` 在 `fcntl.flock` 内维护 `data/reviews/criteria_ready_state.json`，只存 `id → ready_since`；离开 READY 自动销账。
2. `scripts/review_bundle.py` 将待裁决清单写入 `Bundle.pending_adjudications`，并把状态合并回每行。
3. `app/review/bundle_render.py`：
   - full MD 列全量待裁决清单。
   - push 摘要只列 `ready_count` 与 `ready_since` 最久前 5 项，避免 19 项 READY 刷屏。

### 24.2 真实读取

- `pending_adjudications()`：**36 项**，满足 ≥30 验收。
- `refresh_counts()` 后：**19 项 READY_TO_EVALUATE**。
- 首次运行前 registry 静态状态里 READY 为 0；19 是进程内按数据文件刷新得到的当前计数，不写回 registry。
- 状态文件记录 19 项，`ready_since=2026-09-19`。
- **诚实口径**：`ready_since` 表示"系统首次观察到 READY"，不是历史真实转 READY 时间；registry 无状态迁移时间线，不能回填。首次落状态前的历史时长不得拿去裁决逾期。

### 24.3 验证

- `pytest tests/review/test_criteria_status.py tests/review/test_bundle_render.py -q`：5/5 通过。
- `pytest tests/review -q`：22/22 通过。
- DEB 指定既有回归：74/74 通过。
- `py_compile` 与 `git diff --check` 通过。
- 真实 CLI 重跑仍不推送；`criteria_ready_state.json` 幂等，READY 离开后会销账。

---

### 24.4 边界

- 本轮没有执行 `--push`，没有向用户发送 19 项 READY；只在代码与产物中具备防噪摘要能力。**⚠️ 更正（见 §25.2 S-1）**：P3 阶段确有一次真实 `--push`（21:27:53 CST 落 `bundle_2026-09-18.push.json`），该句仅对 P4 本轮成立。
- `refresh_counts()` 只改变当前 Python 进程内的 registry 对象，不写交易库、不写 registry 文件。
- 逾期升级（>14 天 P1）尚未实现；等 `ready_since` 有真实观察时长后再启用，避免用首日回看伪造逾期。

---

## 25. P0–P4 交付核查与补充（作者独立复核，2026-09-19）

> 复核方式：不信文档自述。全部结论来自**独立复现**：真库对账、哈希比对、契约反证实验、产物遍历、日志/状态文件取证。
> 结论分三类：**确认**（实测一致）/ **补充**（文档未写的量化或更正，S-x）/ **待办**（未达验收，需显式化）。

### 25.1 确认（实跑通过）

| # | 项 | 我的复现（原始证据） |
|---|---|---|
| 1 | 幂等（§23.2 "两次运行 hash 一致"） | 重跑前后 `md5sum` **完全相同**（bundle/`bundle_latest`/`REVIEW` 三件）；`generated_at=""`、`git={}` ⇒ 无不稳定字段 |
| 2 | 结果层与 SQL 逐分一致（§22.2） | 直查 SQL：`net=34.56 / gross=72.39 / fees=37.83 / n=8 / wins=7`；bundle headline `net_pnl=34.56`、`win_rate=0.875` ⇒ 一致；切片 `盘中 6/−76.19 + 盘后 2/+110.75 = 34.56` ✓ |
| 3 | plan-vs-actual（§22.2） | SQL：非 SKIP 指令 **7**、`executed=1` **6**、非 SKIP 未执行 **1**；当日 8 笔成交中 `US.ACN`/`US.WBD` **无任何指令行** ⇒ 计划 7 / 执行 6 / 错过 1 / 计划外 2 **逐格一致** |
| 4 | 证据覆盖 100%（§5.3 契约） | 独立遍历 bundle：**28 个指标，缺 `ref` = 0**；`artifact/derived` 类**缺 `integrity` 四键 = 0**（`integrity` 为嵌套对象，与 §5.3 写法一致） |
| 5 | "缺证据 = 构建失败"是真强制（非文档约定） | 4 次反证实验全部抛错并**点名指标路径**：删 `evidence` ⇒ `headline.a: evidence is required`；缺 `integrity.universe` ⇒ `... is missing universe`；`integrity.n` 传字符串 ⇒ `must be a non-negative integer`；`unit` 传 `dollars` ⇒ `metric.unit is invalid` |
| 6 | 判据层（§24.2） | `REGISTRY=36`、`refresh_counts()` 后 `READY=19` ⇒ 与文档一致；`criteria_ready_state.json` 实测 **19 条**；pending 行含文档声明的全部 11 字段；**`evidence.ref` 指向注册表条目本身**（`app/playbook/shadow_registry.py :: id=...`）而非猜出来的产物路径 ✓ |
| 7 | 防噪摘要真实现（§24.1.3） | `render_md` 的 push 段实测含 `待裁决 {len(ready_rows)}：{id@ready_since 最久前 5 项}`；全量清单只在 MD/JSON ⇒ **19 项不会刷屏** |
| 8 | 边界（§21.3/§23.4/§24.4） | `crontab -l` 无 review 行；`app/push/*` 与 `app/playbook/*` **零改动**；`obsidian_vault/daily/review` **不存在** ⇒ `--vault` 确未执行；`--period week` 实测 `rc=2` 并提示留待 P5 ⇒ 无假周期产物 |
| 9 | 语料范围含我的 §20.7 要求 | 脚本语料含 `ops/`、`/root/.hermes/scripts`、`/root/.hermes/skills` ⇒ **未复犯 §19.1 的范围盲区**；skills 计数 293 = `*.py+*.sh`（我 §16.2/§17 用的 279 是 **`.py` only**，两者不冲突，但表述需带后缀口径） |
| 10 | 产物结构（§14） | `REVIEW_2026-09-18.md` 含 结果 / 阶段切片 / 计划 vs 实际 / 待裁决 / 异常 / 证据 六节（105 行） |

### 25.2 补充（S-x；其中 S-1 是与文档矛盾的事实）

- **S-1（与 §23.4 矛盾，需更正）**：§23.4 写"本轮未执行 `--push` / `--vault`，**未创建 push 状态**"，但实测 `data/reviews/bundle_2026-09-18.push.json` **存在**，内容 `{"report_date":"2026-09-18","pushed_at":"2026-09-19T13:27:53+00:00","category":"REVIEW_DAILY"}`（mtime 21:27:53 CST）。
  按 `app/push/hub.py` 的语义，`sync=True` 路径返回 `_immediate_send()` 的 `sent_count > 0`：**返回 True 只可能来自某个频道真实发送成功**（全失败会 `logger.error("[PushHub] 所有 channel 失败!")` 并落 `data/push_failures.jsonl`）；实测 `push_failures.jsonl` 中**无** `daily_review/REVIEW_DAILY` 记录 ⇒ 非失败路径。
  ⇒ **结论：21:27:53 CST 有一条"每日复盘 2026-09-18"推送真实送达** —— **2026-09-19 用户确认已收到**，原文如下（逐字）：

  ```
  📊 FTSeek-US 每日复盘 (2026-09-18)
  FTSeek-US 复盘 2026-09-18
  净 +$34.56 / 8笔 / 胜率 87.5%
  盘中 6笔 -$76.19；盘后 2笔 +$110.75
  计划 7，执行 6，错过 1，计划外 2
  P0 异常 0
  待裁决 19：p1_1_capital_flow@2026-09-19; futu_channel_ofi_baseline@2026-09-19; futu_channel_top_movers@2026-09-19; futu_channel_iv_hv@2026-09-19; vwap_gate_ab@2026-09-19
  ```

  **事后取证（作者复核）**：把 `bundle_2026-09-18.json` 反序列化后调用 `render_md()`，正文与上述**逐字一致**（标题由既有 `app/push/message.py:643 daily_review()` 生成，格式 `📊 FTSeek-US 每日复盘 ({date})`）。
  ⚠️ 首次比对时我的重建版把**盘后排在盘中之前**、与交付原文不符 —— 复核后确认是**我的重建artifact**：CLI 写盘用 `json.dumps(..., sort_keys=True)`（`scripts/review_bundle.py:101-104`）⇒ **磁盘 JSON 的键序是字母序**，而 `render_md` 走内存中的 `_STAGE_ORDER`（盘前→盘中→盘后）⇒ 从 JSON 逆向重渲染会得到与交付不同的阶段顺序。**这不是产品缺陷**（同一轮的 MD 正是"盘中→盘后"，与交付一致），但它是引用产物时的真实陷阱 ⇒ 见 S-6。
  **净收益**：该次意外的真实推送等价于**投递路径的生产端端到端验证**（此前只有 FakeHub 测试）——实测结论：送达成功、头条数字与 DB 对账一致、"待裁决 19 项只列最久前 5"的防噪设计**在生产里生效**（收到的是 1 行摘要而非 19 行）。两个后果：①§23.4/§24.4 的"未真实推送"表述须更正为"P3 阶段曾有一次真实 `--push`（21:27:53）"；②该状态文件使 09-18 的**后续真推被判 `already`** —— 因 09-18 是历史日、生产首个真推是上线日的当日复盘，影响有限，但性质属"**验证动作静默消费了首次推送名额**"，与本仓 8/13 那次"单测写生产报告"同类：**验证路径必须与生产状态文件隔离**（建议：`--push` 增加 `--dry-run`/`--state-dir` 参数，或在 §23.4 这类记录里先落"已验证/已真实推送"两态）。
- **S-6（产物键序 ≠ 显示顺序；引用陷阱）**：`scripts/review_bundle.py` 写盘用 `sort_keys=True`（保证哈希稳定），因此 `bundle_*.json` 的 `headline`/`sections.slices` 键序是**字母序**；而人读 MD 与推送正文按 `_STAGE_ORDER`（盘前→盘中→盘后→未标注）渲染。⇒ **任何从 JSON 重建/转述阶段顺序的人或 agent 都会得到与交付正文不同的顺序**。修法（择一）：①在 §16.1 薄 skill 的"读数接口"里写明"显示顺序以 `render_md` 为准，JSON 键序仅供取数"；②或在 JSON 顶层加 `display_order` 字段声明顺序。**不改 `sort_keys`**（它正是幂等哈希的来源）。
- **S-2（分类计数非互斥）**：§21.1.6 的 `webhook=77 / app.push=54 / notifier=48 / curl=17`，四类合计 **196 > 命中文件 164** ⇒ 一个文件可落多类，读表**不得相加**。建议产物加 `classes_are_overlapping: true`，或改成互斥分档（优先级：`app.push` > `notifier` > `webhook` > `curl`）。
- **S-3（计数口径需自描述）**：§21.1.4 的分档计数与我的独立 `find` 有 ±1% 差（app 474 vs 477、scripts 460 vs 461、tests 405 vs 409、skills 293 = 293 一致；总量 1699 vs 1707）。差在"后缀集合/是否含 `__pycache__`"这类规则上 ⇒ 建议产物里**打印语料规则**（后缀白名单 + 排除项），否则复核者无法对齐。
- **S-4（重启未归因）**：`logs/ftseek_us.log` 显示 **21:30:32 收到停止信号 → 21:31:08 服务启动**（优雅关闭后重启）。§21.3/§23.4/§24.4 的边界记录（写于 21:0x–21:2x）均未涵盖该事件，需补一句归因。缓解事实（我实查）：重启时 `[持仓恢复] DB 无未平仓记录`、`持仓对账完成 | DB=0 | 内存=0 | 差异=0` ✓；`logs/deploy_freshness.log` 同期为 `skip: outside_safe_window`（即**不是**部署新鲜度看门狗自动重启）。**副作用**：重启后服务进程加载了工作区新版 `app/core/trade_identity.py`；我核过服务侧唯一调用点 `order_executor` 传 `None`（走 `now_et()`）⇒ 该分支不可达、**无行为变更**，但"代码已进服务进程"这一点应记录在案（口径两态在重启后归一）。
- **S-5（治理：交付物未入库）**：P0–P4 的 6 个模块 + `tests/review/` 原为 **untracked**（`git status` 全为 `??`）。按 RFC-060 纪律（未入库代码 = 复核者看不到、随时丢），本次核查**已随之单独立提交**（测试随行，可一键 revert）。

### 25.3 待办（未达验收，建议在文档里显式写成"未达成"而非留白）

1. **§14 G5 未达成**：`self_audit` 当前只有 `exit_date_mismatch_count`（且 `anomalies: []`），未含 G5 要求的"死产出 / 无证据指标 / 告警噪声 / 未登记影子 / 测试标的进入推送"。建议 §24.4 边界补一句"G5 属 P5 范围、当前未达成"，避免验收清单被读成已通过。
2. **`_exit_date_et` 静默丢行**（§19.3 已提）：bundle 的 `self_audit` 建议加 `unparsed_exit_time` 计数（当前库 0 行，属预防）。
3. **逾期升级（>14 天 P1）未实现**（§24.4 已诚实说明）：与 §20.2④ 一致，等 `ready_since` 有真实观察时长再启用 —— 这条边界写法正确，保留。

---

## 26. 第 3 轮独立复查（外部 agent，2026-09-19，用户授权后追加）

> 复查方式：纪律同 §10/§17/§25 —— **不采信文档自述**，全部在真库/真文件/真运行上重跑（含 2 次真实 CLI 执行与 1 次带 `--push` 的守卫验证；未产生第 2 条推送）。结论分三类：**确认**（独立复现一致）/ **新发现**（N-x，文档未记录的增量）/ **裁决意见**（对 §20）。
> 本节作者仅追加本节并修正头部状态行与 §14 勾选（N-1），未改动 §1–§25 任何结论。

### 26.1 确认摘要（22 项独立复现，全部一致）

| # | 项 | 第 3 轮实测 |
|---|---|---|
| 1 | C1/E1 阶段拆解 | `stage_breakdown` 重跑 ⇒ premarket 4/−648.86 · regular 329/+340.14 · afterhours 2/+110.75 · 合计 335/−197.97 **逐格一致** |
| 2 | P2 生产对账（§22.2） | SQL 直查 09-18 ⇒ `34.56\|8\|7` == bundle headline `net_pnl=34.56/win_rate=0.875`；切片 110.75+(−76.19)=34.56 ✓ |
| 3 | P1 证据覆盖（§25.1-4） | 独立遍历 bundle ⇒ **28 指标、缺 ref=0、integrity 四键缺失=0** |
| 4 | P1 硬强制（§25.1-5） | 反证实验独立重跑：缺 `integrity.universe` ⇒ `EvidenceError: headline.x: evidence.integrity is missing universe`；缺 evidence ⇒ `headline.y: evidence is required` —— **均抛错且点名指标** |
| 5 | P3 幂等（§23.2） | 第 3 次重跑 `scripts/review_bundle.py --date 2026-09-18` ⇒ bundle/`bundle_latest`/REVIEW 三件 **md5 逐字一致**；DB 以 `mode=ro` 打开（CLI 无写库能力）✓ |
| 6 | §16 更正 B 守卫 | 带 `--push` 手工重跑 ⇒ 结果 JSON `"pushed": "already"`、`push.json` 未变、`push_failures.jsonl` 无新增 ⇒ **不双推**（生产端实证） |
| 7 | P4 判据层（§24.2） | `REGISTRY=36 / READY=19 / criteria_ready_state.json 19 条` ✓ |
| 8 | C16 hub 绕过 | `app/push/hub.py` 实证 `if not sync and not should_send(...)` + sync 路径 `_send_and_mark(msg, None)` ⇒ sync 不去重、连 `mark_sent` 都不调 ✓ |
| 9 | R-2 σ 更正 | `pnl_pct`（ratio 存储）全量已平仓 n=870 ⇒ σ=**0.018977 = 1.8977%** ✓；原 1.18% 确不可复现 |
| 10 | R-3 时区分布 | SQL 分组 ⇒ `-04:00`=547 / naive=327 / `+00:00`=1 ✓ |
| 11 | S-1 真实推送 | `push.json`（`pushed_at=2026-09-19T13:27:53+00:00`）在、failures 零命中、用户确认原文已入档（`0089c73be`）✓ |
| 12 | §17.3 误报 1 平反 | `jobs.json` 顶层为 `{jobs:[...], updated_at}` ⇒ **34 个 job**；`2254de46fe64/6af0648e3eb3/7d448f79c410/0e87d094dcdf` 均在册 `enabled+no_agent`；`alpha-backfill` 确在 06:30 dow2-6 ✓（**本轮复查 agent 首次解析同样误读为 2**，亲身体会"子代理解析不可直采"教训） |
| 13 | §20.2② 时刻论据 | crontab `^30 6` 3 行（exit_calibration_refresh / settle_eod_depth_gate_shadow / settle_extended_hours）+ Hermes alpha-backfill ⇒ 同分钟 4 作业；`^35 6` **0 行** ⇒ 06:35 空档成立 |
| 14 | §21.1 覆盖扫描 | `producers=58 / dead_producers=18 / push_exit_census=164 文件`；分类 **webhook=77 / app.push=54 / notifier=48 / curl=17**（合计 196>164 ⇒ S-2"不得相加"成立）；corpus `file_count=1699`（app 474/scripts 460/ops 35/hermes-scripts 32/hermes-skills 293）✓ |
| 15 | 测试 | `tests/review` **22/22** + 既有 5 文件 **74/74** + `test_ops_scripts_sync` **3/3** 绿（独立重跑） |
| 16 | §19.2-G | `order_executor.py:1314/1332` 两处均**无参**调用 `phase_from_time()` ⇒ naive 分支对服务不可达 ✓ |
| 17 | E5 | `data/shadow/entry_archetype_routing.jsonl` 不存在 ✓ |
| 18 | E9 | `duplicate_external_close` ⇒ `5\|0` ✓ |
| 19 | S-4 重启取证 | 日志 `21:30:32 收到停止信号` → `21:31:14 [调度器] 服务启动`；`持仓恢复 DB 无未平仓` + `对账 DB=0/内存=0/差异=0` ✓（时刻细节见 N-5） |
| 20 | S-5 入库 | `4c529f7f6` 提交在案；`git status` 中 review 交付物不再 untracked ✓ |
| 21 | 边界（§25.1-8） | crontab 0 行 review、Hermes 无 review_bundle job、`ops/hermes/review_bundle_carrier.py` 不存在、`obsidian_vault/daily/review` 不存在、`tests/review/golden/` 不存在 ⇒ **P5 确未开工** ✓ |
| 22 | 产物结构 | `REVIEW_2026-09-18.md` 六节（结果/阶段切片/计划vs实际/待裁决/异常/证据）**105 行** ✓；S-6 的 `sort_keys=True` 锚点（`scripts/review_bundle.py:101-103`）准确 |

### 26.2 新发现（N-1 … N-7；文档此前未记录）

- **N-1（P1 · 文档卫生，本轮已修）**：头部状态行原为"提案(设计稿)/**未落码**/待用户裁决"、§14 十五项**全部未勾**——与 §21–§25（P0–P4 已交付入库 + 一次真实推送）矛盾。以文档为入口的 agent 会误判"还没开工"。本轮已更新状态行并按实测勾选 §14（未勾 3 项均属 P5）。**机制建议**：每轮交付记录（§2x）提交时，同批更新头部状态行，避免"正文前进、头部滞留"。
- **N-2（P2 · §5.8 判据 1 载体当前不存在）**：`data/ext_entry_push_dedup.json` 实测缺失——该文件为**懒创建**（首笔成交富卡经 `_dedup_reserve` 才写；`b4e46c363` 部署后尚无成交推送）。不是缺陷，但 G5 `fixture_symbol_pushes` 的实现必须显式容忍"文件不存在=0"而非报错；§5.8"唯一持久留痕"表述宜加注该前提。
- **N-3（P2 · S-3 未闭环）**：`coverage_*.json` 的 `corpus` 只含 `file_count/roots/scope_counts`，**无后缀白名单与排除项规则**；本轮独立 `find` 总数与 1699 仍差 ~0.5%，复核者无法对齐。S-3 自己提出的建议在 P0 产物中未落地，建议 P5 前补进产物。
- **N-4（P3 · 产物无代码版本溯源）**：幂等的实现是 `generated_at=""`、`git={}` **恒空**（md5 稳定依赖此）。代价：bundle 无法回答"这份数字出自哪个 commit 的口径"，§6"口径变更审计"在 golden（P5）落地前无载体。建议 P5 把 git 信息写旁路 `bundle_<date>.meta.json`（不参与幂等比对），而非永久置空。
- **N-5（P3 · S-4 时刻更正）**：`[调度器] 服务启动` 实测 **21:31:14**（S-4 写 21:31:08；停止信号 21:30:32 一致）。无实质影响，按"逐字证据"纪律记录。
- **N-6（边界观察，非本 DEB 责任）**：`scripts/extended_hours_entry_executor.py`（+38 行）等 4 文件仍在工作区**未提交**（并行延长时段工作流）；cron 读工作区 ⇒ 该链路"生产在跑未入库代码"病灶（RFC-060 立项之本）仍在。§19.4.2 的"移入对应 DEB"尚未执行，宜在对应 DEB（DEB-20260917/20260919 延长时段系列）内收口。
- **N-7（P3 · 验收文案与实现不符）**：§14/§16 更正 B 的验收措辞是"日志记 `skip: already pushed <date>`"，实现的实际行为是**结果 JSON `"pushed": "already"`**（本轮实测）。语义等价、载体不同；已按实测措辞勾选 §14，建议后续文案统一。

### 26.3 对 §20 待裁决建议的独立意见（第 3 轮）

| §20 项 | 意见 | 实测依据 |
|---|---|---|
| ② 06:35 | **同意** | 06:30 同分钟 4 作业、06:35 两载体均 0 占用（26.1-13）；上线前按 §20.3.3 再核一次 |
| ③ READY 只进日报 | **同意，且已有生产实证** | S-1 用户收到的真实推送即"待裁决 19：…前 5 项"单行摘要 ⇒ 防噪设计在生产里生效 |
| ① 不做全量回填 | **同意** | σ 重算（26.1-9）与 §19.3 重基 34 天均表明口径刚变动，回填=事后合理化风险 |
| ⑦ P5 开工 | **建议前置三件小事**：N-1 机制建议（头部同步）、N-3（corpus 规则自描述）、N-4（版本溯源 sidecar）；均为 <20 行改动，可与 P5 同批 | 26.2 |

**总评**：三轮自查/复查的自曝文化（R-1…R-5、S-1…S-6、误报平反）在本轮 22 项独立复现中**全部站得住**；该 DEB 的数字可信度已达到可裁决状态。剩余风险集中在 P5 的调度/载体面（未开工）与 N-2/N-4 两个小缺口。

---

## 27. P4.5 决策诊断层执行记录（2026-09-19）

> 裁决：用户按建议执行。范围仍为**只读观测/呈现层**：不改交易行为、不挂 cron、不推送、不写 vault。

### 27.1 交付

1. 新增 `app/review/decision_diagnostics.py`：
   - 固定三问：哪些指令疑似不该发、哪些入场疑似追高、哪些坑在重复。
   - 新增阶段 × 方向 × 出场原因切片。
   - 新增入场价格带 × 费用切片。
   - MFE=0 且净亏损逐笔列示，标记 `candidate_not_verdict`，不冒充裁决。
   - 追高审计区分初始计划价、reprice 后有效计划价、实际成交价与 chase cap。
   - 重复亏损簇按同 prompt 类聚合，条件为 n≥3、≥2 日、≥2 标的、当前连亏≥3、连亏额低于 −$1,000。
   - SIM↔实盘桥按同 signal/decision 配对，输出近 20 组累计差、$5,000 名义本金折算差与 $500 差异线。
2. `app/review/bundle_builder.py` 接入 `sections.decision_diagnostics`。
3. `app/review/bundle_render.py` 新增三问诊断、阶段×方向×出场原因、价格带×费用、SIM↔实盘桥。
4. `app/review/stage_stats.py` 只扩展读取列：`id/owner_strategy/trade_mode/entry_reason/signal_id/decision_id/entry_price/exit_price`。为兼容最小测试库，按 `PRAGMA table_info` 动态选择存在列。
5. 证据语义修正：
   - plan-vs-actual 的 universe 改为 `db:playbook_instructions+db:trade_records`。
   - derived evidence 增加说明：全日查询无 LIMIT，所选已平仓行均带 exit_time；空日也视为完整性已检查。
6. 覆盖扫描产物自描述：
   - `corpus.rules` 写明 `.py/.sh`、排除 `__pycache__`、只取 regular file。
   - `push_exit_census_meta.categories_are_overlapping=true`，明确分类计数不可相加。

### 27.2 2026-09-18 真库读数

- 三问：
  - 疑似不该发：1 笔，`US.COIN` / `FADE_SHORT` / MFE=0 / MAE=2.6455% / 净 −$161.65（候选，非裁决）。
  - 疑似追高：2 笔，`US.UMC` 初始计划 22.6→实际 23.905（+5.8%，净 +$28.10）；`US.ARM` 260.5→267.515（+2.7%，净 +$13.80）。
  - HPE 初判追高后被 reprice 口径推翻：reprice_from 61.56、实际 61.359，低于有效计划价，不再列为追高。
  - 重复亏损簇：0 个达到阈值。
- 阶段×方向×出场原因：
  - 盘后/做多/`SL_TRAIL_LOCK`：+$110.75。
  - 盘中/做多/`SL_BREAKEVEN`：+$57.36。
  - 盘中/做多/`SL_TRAIL_LOCK`：+$28.10。
  - 盘中/做空/`SL_INITIAL`：−$161.65。
- 价格带×费用：
  - `$20-$50`：2 笔，净 +$79.76，费 $11.74。
  - `$50-$100`：1 笔，净 +$21.80，费 $4.70。
  - `>= $100`：5 笔，净 −$67.00，费 $21.39。
- SIM↔实盘桥：同信号配对 0 组；SIM 215、实盘 0。当前不能推断实盘差异，只能报告配对样本不存在。

### 27.3 覆盖扫描新读数

- 当前语料：1,709 个文件（app 478 / scripts 461 / ops 35 / tests 410 / hermes-scripts 32 / hermes-skills 293）。
- 推送出口命中文件：165。
- dead producers 仍为 18，producer 总数 58。
- 该数字包含本轮新增 `decision_diagnostics.py` 与测试文件；§26 的 1699/164 是历史快照，不再代表当前工作区。

### 27.4 验证

- `pytest tests/review -q`：26/26 通过。
- 既有回归 + ops sync：77/77 通过。
- `py_compile` 通过。
- 真实 CLI 连续两次运行，bundle / REVIEW / bundle_latest hash 一致。
- `git diff --check` 通过。
- crontab 无 review 行；`ops/hermes/review_bundle_carrier.py` 与 golden 目录仍不存在，P5 未开工。

### 27.5 边界与后续

- “疑似不该发”仅用 MFE=0 + 净亏损做候选筛选；最终裁决必须回看 prompt、标的选择、数据源和入场理由。
- prompt 类目前来自 raw instruction 的 `strategy_type/entry_archetype/entry_strategy` 回退链，还不是完整 LLM prompt 分类。
- 追高判据为：越过 chase cap，或高于 reprice 后有效计划价 2%。
- 重复亏损簇是候选簇，小盈利会计入 weak interruption，但不自动判定策略失败。
- actionable MFE（有利波动持续≥2根1分钟bar并触发救援动作且净收益≥$50）、regret price、delay-5 反事实、pretrade card 尚未实现，属下一阶段。
- P5 仍不自动挂 cron；须等用户明确裁决载体与调度。

---

## 28. P4.6 可行动性诊断层执行记录（2026-09-19）

> 裁决：继续只读观测/呈现层。不修改 `app/playbook/*`，不改变交易行为，不挂 cron，不推送，不写 vault。

### 28.1 交付

1. 新增 `app/review/actionability_diagnostics.py`：
   - **Actionable MFE**：在真实一分钟 path 上找连续≥2根 bar 的有利波动；只有触及既有救援线（breakeven lock / trailing lock / tightened stop 的现存落库代理）且按方向计算、扣除 `fees_est` 与已记录 `exit_slippage_bps` 后净额≥$50，才列为候选。
   - **Regret price**：对 LONG 取 bar low、SHORT 取 bar high，作为可成交限价候选；若救援触发则用救援出场，否则用实际出场；输出仍能净盈利的最高候选价。该值回答“如果按更合理的限价挂单，是否可能反转”。
   - **Delay-5**：取实际入场后约5分钟的一分钟 bar close，保持同出场、同数量、同费用与已记录滑点，做方向正确净额对照。
   - **Pretrade card coverage**：审计 prompt primary/secondary class、置信度、prompt/config 版本、数据源时间、候选Top5、拒绝原因、入选理由、四种场景（趋势延续、冲高失败、直接失败、低波横盘）、invalidation、救援动作。
2. `app/review/bundle_builder.py` 新增 `sections.actionability`。
3. `app/review/bundle_render.py` 输出汇总、Regret 候选、Delay-5 对照、Pretrade 字段缺口频率；所有条目均标注 `candidate_not_verdict` / `coverage_audit_not_verdict`。
4. 新增 `tests/review/test_actionability_diagnostics.py`，覆盖 LONG path、SHORT 方向、缺 bar、pretrade 缺口与渲染接入。

### 28.2 2026-09-18 真库读数

- 已平仓8笔；6笔有一分钟 bar，2笔盘后（ACN/WBD）无 replay bar，显式记为缺 bar，不混入推断。
- Actionable MFE：0/6。含义是**没有任何一笔同时满足**“有利波动持续≥2根bar + 救援线触发 + 扣费扣滑点后净额≥$50”。这不是“当天没有盈利”，也不是策略裁决。
- Regret price：5/6 有正净额候选，但金额都很小：
  - HPE：regret 61.215，净 +$2.23。
  - UMC：regret 23.89，净 +$2.49。
  - CRM：regret 240.07，净 +$2.16。
  - ARM：regret 267.40，净 +$0.23。
  - ORCL：regret 145.67，净 +$0.21。
  - COIN：无可盈利限价候选。
  - 结论：这些“正净额”不构成值得改规则的证据；只说明入场价距盈亏线很近，样本和金额都不足。
- Delay-5：3笔实际更优、3笔延迟更优、0笔持平。
  - 实际更优：HPE 实际 +$21.80 vs 延迟 −$18.22（+$40.02）；UMC +$28.10 vs +$8.26（+$19.84）；ORCL +$10.65 vs +$9.65（+$1.00）。
  - 延迟更优：COIN 实际 −$161.65 vs 延迟 −$88.96（少亏 $72.69）；CRM +$19.03 vs 实际 +$11.11（+$7.92）；ARM +$25.85 vs 实际 +$13.80（+$12.05）。
  - 读法：不能得出“统一延迟5分钟更好”。真正值得单独复盘的是 COIN——延迟仍亏损，但少亏明显，结合 MFE=0，指向入场时机/方向候选问题。
- Pretrade card：完整 0/75。
  - 75/75缺失：candidate_top5、prompt_secondary_class、prompt_version、rejection_reasons、rescue_actions、四种场景。
  - 60/75缺失：prompt_primary_class、invalidation。
  - 59/75缺失：selected_symbol_rationale。
  - 74/75缺失：data_source_time。
  - 这是最明确的系统缺口：当前能解释已成交结果，但不能在决策前完整回答“为什么是它、为什么不是别的、失败时怎么救”。

### 28.3 边界

- Actionable MFE 的“救援线”来自当前 trade metadata 中已经落库的 breakeven/trailing/effective-stop 线，是**现存动作代理**，不是完整 pretrade 预注册规则。
- Regret price 假设 bar low/high 可按该价成交，未建模排队、部分成交与成交概率；滑点使用实际记录的 `exit_slippage_bps`，不额外臆造固定值。
- Delay-5 保持同出场与同成本，只隔离入场时点；不能代表真实延迟后的完整订单生命周期。
- 盘后无 replay bar 的2笔不推断；后续若补盘后bar，应重跑同日 bundle。
- 所有输出均为候选证据，不直接触发策略修改、熔断或自动化。

### 28.4 验证

- `pytest tests/review -q`：31/31 通过。
- 指定回归 + ops sync（review / stage / daily review / review tables / validation accounting / OKR）：108/108 通过。
- `py_compile`：通过。
- `git diff --check`：通过。
- 真实 CLI 连续重跑 `2026-09-18`：bundle / REVIEW / latest 三个 hash 稳定；JSON 与 latest hash 一致；未推送、未写 vault。
