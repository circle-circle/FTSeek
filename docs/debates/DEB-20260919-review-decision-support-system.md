# DEB-20260919 · 复盘决策支持系统 (Review Decision Support) [RFC-088]

> **状态**: 提案 (设计稿) / 未落码 · 待用户裁决 + 第二 agent 独立复查
> **日期**: 2026-09-19
> **作者**: Hermes
> **目标读者**: ①项目所有者(裁决) ②执行 agent(落码) ③**独立复查 agent(§10 复查清单)**
> **关联**: `RFC-087`(盘前收口条件豁免预注册 — 本设计的**范例产物**)、`RFC-086`(阶段统计 SSOT)、
> `DEB-20260918-extended-hours-ssot-and-observability.md`、`app/review/review_orchestrator.py`、
> `app/playbook/shadow_registry.py`、`app/review/stage_stats.py`
> **变更类型**: L1 (观测/呈现层; **不触碰 `app/playbook/*` 执行链** ⇒ 无需重启服务)
> **工作区实现计划**(任务级、含逐条完整代码): `.hermes/plans/2026-09-19_152656-review-system.md`（`.hermes/` 被 gitignore ⇒ 本 DEB 自含必要接口，为准）

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

### 1.2 消费侧：关键 SSOT **零消费者**（本次实测，最硬的证据）

`app/playbook/shadow_registry.py` 已经是**影子/判据的单一真源**，具备完整决策要素：

- `ShadowItem` 字段: `id / name / status / description / data_path / config_key / min_n / graduation_criteria / eval_script / created / notes / count_mode`
- 状态机: `OBSERVING → READY_TO_EVALUATE → GRADUATED / KILLED / SUPERSEDED`
- API: `REGISTRY`(**30+ 项**)、`get_observing()`、`get_ready_to_evaluate()`、`refresh_counts()`、`_count_samples()`、`summary()`、`check_mounting()`(cron 挂载核对)、`check_intent_uniqueness()`
- 其 docstring 自陈要解决: *"当前有多少影子在跑 / 各自攒了多少样本、离毕业门禁还有多远 / **哪些已经够样本该裁决了但没人裁** / 哪些已废弃但 cron 还在白跑"*

**实测其消费者**：

```bash
grep -rn "shadow_registry" --include=* . | grep -v "^\\./app/playbook/shadow_registry.py" | grep -vE "\\.pyc|/logs/|\\.git/"
# → 仅 tests/test_*.py 共 7 个文件（+ scripts/okr_2026_09_scoring.py:390 的**文本子串提及**）
crontab -l | grep -i "shadow_registry\|check_mounting"     # → 空
grep -rn "get_ready_to_evaluate\|refresh_counts" app/ scripts/ | grep -v shadow_registry.py  # → 空
```

⇒ **判据成熟了没有任何程序会报**；cron 白跑没有任何程序会删；用户只能靠**主动询问**。这正是用户描述的症状，且是**可复现的客观事实**，不是感觉。

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
| G4 | **判据自动到期上报**: 复用 `shadow_registry` 状态/计数，`READY_TO_EVALUATE` 到点主动推送 | 构造一个 `min_n` 已达标的假条目 → 当日推送含该 id（fixture 测试） |
| G5 | **自审与减法**: 每周自动列"死产出/无证据指标/告警噪声/未登记影子" | `self_audit` + `coverage_<date>.json` 非空且可执行 |

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
| E2 | `shadow_registry` 无生产消费者 | `grep -rn "shadow_registry" --include=*.py app/ scripts/ \| grep -v shadow_registry.py` | **空** |
| E3 | `get_ready_to_evaluate/refresh_counts` 无调用点 | `grep -rn "get_ready_to_evaluate\|refresh_counts" app/ scripts/ \| grep -v shadow_registry.py` | **空** |
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
| 时区转换 | 一律 `app/utils/et_time`（**禁手写 −12h**） | 项目既有纪律 |
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
| 6 | 功效声明 | 写明"能判方向/尾部，还是能判 0.1pp 级"（单笔净 σ≈1.18% ⇒ 检出 +0.15% 需 **n≈481**；n=56 的 CI 半宽 ±0.31pp） |
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

**到点推送（复用 §E7 先例）**: `dedup_key=f"criteria_ready:{date}"` + `sync=True` + `Level.NORMAL` + `Category.REVIEW_WEEKLY`，**同日只推一次**，正文点名 `id` 与"到点未裁"天数。

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

---

## 6. 可观测性（本系统自身也要被观测）

- bundle 每次构建写入 `data/reviews/bundle_<date>.json`（`data/*` gitignore），并保留 `bundle_latest.json` 供快速读取。
- **幂等**: 同日重跑除 `generated_at`/`git` 外**逐字相同**（测试断言）。
- **原子写**: `tempfile + os.replace`，避免半截产物被消费。
- **本系统的成败也进自审**: `self_audit = {"dead_producers":[...], "unregistered_shadows":[...], "metrics_without_evidence":0, "error_lines_today":N, "pushes_today":M}`。
- **口径变更审计**: golden 基准 + 任何口径改动必须同批更新 golden 并写 DEB（机制上强制，见 §9 P5）。

---

## 7. 调度与周期（cron，含回滚注释）

| 时刻(CST) | 行 | 说明 |
|---|---|---|
| 每交易日 06:30 | `--date $(TZ=America/New_York date -d yesterday +\%F) --push --vault` | 日复盘（收盘后、既有 06:20 LLM 报告与 06:20+ 复盘链之后） |
| 周六 06:45 | `scripts/review_coverage_scan.py` | 每周自审（死产出/未登记影子） |
| 每月 1 日 07:00 | `--period month` | 月度双窗口对照 + 判据总账 |

⚠️ 两个必须实测的点：①crontab 内 `%` 需转义为 `\%`；②命令替换 `$()` 在部分 cron 实现下不执行 ⇒ **上线后必须 `grep review_bundle logs/review_bundle.log` 验证真实产出**（不许假设生效）。

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
- 推送: `from app.push import get_push_hub, PushMessage, Level, Category`，`dedup_key=f"review_bundle:{date}"`、`sync=True`
- 验收: `pytest tests/review/test_bundle_render.py tests/review/test_bundle_cli.py -q` → 全绿（含幂等断言）；`venv/bin/python scripts/review_bundle.py --date 2026-09-18` → 打印 headline 摘要
- 提交: `[RFC-088][review-system] P3: 渲染+CLI+单条推送`

**P4 判据到期上报（复用注册表）**
- `app/review/criteria_status.py`（§5.6，≤80 行，只读注册表）+ 到点推送
- 验收: fixture 造一个 `min_n` 已达标的假条目 → 断言当日 push body 含该 `id`；真实读取: `venv/bin/python -c "from app.review.criteria_status import pending_adjudications as p; print(len(p()))"` → ≥30
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
| C2 | `shadow_registry` **零生产消费者** | E2/E3 两条 grep | 出现任何 app/scripts 的**程序化**调用 ⇒ §5.6 前提削弱（但方案仍成立，改述"消费者过少"） |
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

---

## 14. 验收清单

- [ ] P0 覆盖扫描可跑，产出 `data/reviews/coverage_<date>.json`，含 `dead_producers`
- [ ] P1 schema 五测全绿，含"缺证据点名指标"用例
- [ ] P2 结果层与 `sqlite3` 直接查询**逐分一致**（≥1 次人工对账留痕）
- [ ] P2 切片净值之和 == headline 净额（测试断言）
- [ ] P2 `exit_date_mismatches` 存在且差异 >0 时 P0 上报
- [ ] P2 `Evidence.integrity` 4 键硬约束生效（少一键即构建失败）
- [ ] P3 幂等（同日重跑逐字相同，除 `generated_at`/`git`）
- [ ] P3 推送恰好 1 条且走 `app.push`（`dedup_key` 生效）
- [ ] P4 判据清单 ≥30 项、`READY_TO_EVALUATE` 项被单列
- [ ] P5 golden 入库且 `git check-ignore` 返回非忽略
- [ ] P5 cron 三行生效且**次日有真实产出**（`grep review_bundle logs/review_bundle.log`）
- [ ] 全量回归通过（`tests/review/` + 5 个既有文件）
- [ ] 第二 agent §10 清单逐条给出结论（成立/不成立 + 证据）

---

## 15. 讨论记录

- **2026-09-19 · 用户**: "目前建议系统有很多信息收集和统计，包括散落零散的测试、推送、记录等，但是缺乏一个完整并基于结果，溯源，整合的复盘分析系统，而不是每天被动询问。"
- **2026-09-19 · 用户（定调）**: "最终目的不是累积数据，而是总结溯源，合理优化，非拍脑袋决策依据，可以按周期，可以统计交易阶段等等。基于项目已有的内容和需要补充的进行完整的规划设计，写入 deb。我还需要另外的 agent 进行分析复查。"
- **2026-09-19 · Hermes（本稿）**: 按"证据优先"普查后**修正了问题陈述**：记录侧已过剩（330 脚本 / 108 cron / 108 jsonl / 285 lessons / 165 DEB），真正的缺口是**消费者层**——其中 `app/playbook/shadow_registry.py` 这一判据 SSOT **零生产消费者**（E2/E3 实测）。故方案收敛为"一层派生视图 + 四消费面"，并**删除**原设计中的"新建判据 YAML / 新建事件台账"两项（避免与既有 SSOT 双写）。
- **2026-09-19 · Hermes（自曝）**: 本会话早前给出的盘前臂读数（`pre_close −0.402% / rth_close −0.101% / intraday_sl +0.237% / 最差 −7.281%`）**已被 `RFC-087 §3` 更正为待更正数字**（回放工具 chunk 尾部静默截断）。本 DEB 不引用这些数字作为依据；§5.3 的 `integrity` 硬约束即该教训的机制化。
- **待补**: 第二 agent 独立复查结论（请在本节追加"第 N 轮 · <agent>"小节，逐条对应 §10）。