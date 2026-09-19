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
- 推送: `from app.push import get_push_hub, PushMessage, Level, Category`，`category=Category.REVIEW_DAILY`、`sync=True`；**并且必须先过 caller 侧按日守卫**（读 `bundle_<date>.json:pushed_at` 或独立 state；命中则记 `skip: already pushed` 直接返回）。**不得**依赖 `dedup_key` 实现"同日只推一次" —— `sync=True` 绕过 hub 去重（§16.5 更正 B，`app/push/hub.py:124`）
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

> **§16（载体形态决定）已就上列问题给出耦合结论**：第 1 问的时刻需改（06:30 与 `alpha-backfill` 撞分钟）与归并边界（只并报表类两条）；第 2/4 问的去重机制改为 **caller 侧按日状态**（§16.5 更正 B：`sync=True` 绕过 hub 去重）；第 3 问候选 **4 → 1**（§16.5 更正 A）；第 5 问回填须只写产物不推送。

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
- [ ] §16 载体：`ops/hermes/review_bundle_carrier.py` 入库且与线上副本 sha256 一致（`pytest tests/test_ops_scripts_sync.py -q` 绿）；载体成功路径**零 stdout**（不双推）
- [ ] §16 更正 B：同日重跑**不产生第 2 条推送**（caller 侧按日守卫生效；实测：手工跑第 2 次 → 日志记 `skip: already pushed <date>`）
- [ ] 全量回归通过（`tests/review/` + 5 个既有文件）
- [ ] 第二 agent §10 清单逐条给出结论（成立/不成立 + 证据）

---

## 15. 讨论记录

- **2026-09-19 · 用户**: "目前建议系统有很多信息收集和统计，包括散落零散的测试、推送、记录等，但是缺乏一个完整并基于结果，溯源，整合的复盘分析系统，而不是每天被动询问。"
- **2026-09-19 · 用户（定调）**: "最终目的不是累积数据，而是总结溯源，合理优化，非拍脑袋决策依据，可以按周期，可以统计交易阶段等等。基于项目已有的内容和需要补充的进行完整的规划设计，写入 deb。我还需要另外的 agent 进行分析复查。"
- **2026-09-19 · Hermes（本稿）**: 按"证据优先"普查后**修正了问题陈述**：记录侧已过剩（330 脚本 / 108 cron / 108 jsonl / 285 lessons / 165 DEB），真正的缺口是**消费者层**——其中 `app/playbook/shadow_registry.py` 这一判据 SSOT **零生产消费者**（E2/E3 实测）。故方案收敛为"一层派生视图 + 四消费面"，并**删除**原设计中的"新建判据 YAML / 新建事件台账"两项（避免与既有 SSOT 双写）。
- **2026-09-19 · Hermes（自曝）**: 本会话早前给出的盘前臂读数（`pre_close −0.402% / rth_close −0.101% / intraday_sl +0.237% / 最差 −7.281%`）**已被 `RFC-087 §3` 更正为待更正数字**（回放工具 chunk 尾部静默截断）。本 DEB 不引用这些数字作为依据；§5.3 的 `integrity` 硬约束即该教训的机制化。
- **2026-09-19 · 用户（裁决）**: 问"复盘系统产出最终的样式会是 skills 还是其他什么框架合适"，并裁「需要」= 把该决定写成 DEB 新节（载体形态 + 落点清单 + 与 §13.2 的耦合）后提交、再交第二 agent 复查。落成本文 §16。
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
| C2 | 成立 | 一致：`get_ready_to_evaluate\|refresh_counts` 零非自身命中；`shadow_registry` 仅 3 处**文本子串**（.pyc 与注释不算消费者） | 原文保留（§5.6 前提成立） |
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
