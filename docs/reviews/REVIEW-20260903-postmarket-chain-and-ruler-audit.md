# REVIEW-20260903 — 9/2 美股盘后全链复盘 + 尺子审计 (含修复执行记录)

- **状态**: 已执行 (2026-09-03, 用户令"昨日美股交易结果和链路以及其他Agent的补充更新，请进行复盘"→ 复核后裁"如果确认问题存在无误，请按照建议做完整优雅合理的修复，并更新复盘落档")
- **执行者**: Codex (本会话)。核查对象含另一 Agent 当日在飞行中的穿透影子批 (提交前独立跑测后入库)。
- **范围**: ①9/2 交易结果与口径 ②入场/出场链路当日行为 ③9/3 当日 8 个提交 + 1 批未提交改动 ④测量装置 (尺子) 自身。
- **纪律**: 本批**零交易行为变更、零退出参数变更** (C1/C7 合规) — 全部是修尺子、补登记、修落档口径; 唯一执行侧改动 (议题二 marketable limit) 是上一批已裁事项, 本批只补它的判据与登记。
- **一句话结论**: 9/2 交易结果健康但统计上不可读 (按日聚类 t=0.61); 真正的病灶在测量与治理层 —
**KR2 判分式与日级冻结守卫都在读一个恒为 `v2` 的列, 属结构性假绿**, 而退出参数窗内实际已换 3 版 (今晚第 4 版)。
第二处尺子病灶在提交前查出: **测试一直在往生产判分数据里写** — 死单早期影子 90% 的行、审计留痕 100% 的行
是伪造的, 跨 23 天无人发现 (§四.8)。真库 `ftseek_us.db` 与交易台账零污染 (sha256 前后一致)。

---

## 一、9/2 结果核验 (台账实数, 与盘后文档逐条对齐)

| 口径 | 值 | 核验方式 |
|---|---|---|
| 笔数 | 12 (11 已平 + `US.BWXT` #1427 过夜) | `trade_records WHERE trade_date='2026-09-02'` |
| 毛利 | **+96.27** | `sum(realized_pnl)` — 注意该列是**毛**口径 |
| 费用 | **−52.45** (11 笔往返均 $4.77, `fees_source=est`) | 盘后文档归因段, 与 `_generate_summary` 净口径复算一致 |
| 净额 | **+43.82** | 本会话用 `DailyReviewGenerator._generate_summary` 对真库复算 = 43.82 ✓ |
| 胜率 (净) | 9/11 = **81.8%** | 同上 (`win_rate_net=0.8182`) |
| 通道 | UE+再入场 7 笔净 +66.46 / DA 晨批 4 笔净 −22.64 | 盘后文档, DELL −49.30 单笔抹平 DA 批 |
| 最大单笔持仓回撤 | **3.65 pp (峰→谷, `US.GTLB`)**, 8/11 笔有非零回撤 | 本批新增口径 (见 §四.6) — 修复前文档恒印 0.00% |

**统计可读性 (宪章第 9 律: 不带 MDE 的结论不予采信)**: 6 交易日窗口 (8/26→9/2, n=68) 日毛序列
+500.52 / +408.31 / −601.69 / −113.00 / +325.98 / +96.27 → 均值 +102.73, sd 410.9,
**按日聚类 t = 0.61**, MDE ≈ ±$335/日 (≈ ±$30/笔)。即 9/2 的 +43.82 与 81.8% 胜率**不构成 edge 证据**,
与 OKR-2026-09 前提"毛期望≈0"不矛盾。DELL 的 −49.30、BE 家族的 −19.7/案同样在噪声地板以下 —
**这些修复只能用机制口径判分, 不能用 P&L 判分** (本批据此改了 §八 判据, 见 §四.4)。

## 二、损益结构: 出血点定位 (8/26→9/2, n=68, 毛口径)

| 维度 | 数 |
|---|---|
| 赢/亏 | 56 赢 +2112.72 (均 **+37.73**) / 12 亏 −1496.33 (均 **−124.69**) → 单笔亏损是单笔赢利的 **3.3×** |
| 出场分布 | `SL_BREAKEVEN` 26 (+359.44) · `SL_TRAIL_LOCK` 20 (+969.57) · `DEAD_SIGNAL` 10 (+65.61) · `PRE_CLOSE` 2 (+222.36) · `TAKE_PROFIT` 1 (+448.71) · **`SL_INITIAL` 8 (−1069.01, 均 −133.63)** · **`OVERNIGHT_GAP` 1 (−380.29)** |
| 集中度 | `SL_INITIAL` + `OVERNIGHT_GAP` = **全部亏损的 97%** (−1449.30 / −1496.33) |
| BE 类出场侧分布 (8/17 起, `_be_close_telemetry.limit_side`) | **active 88 笔 均 +41.08 合计 +3614.77** / **passive 13 笔 均 −5.76 合计 −74.85**; 8/24→9/1 九个交易日**零 passive** |

判读: 议题二 (被动侧 marketable limit) 修的是 13/101 = **13%** 的 BE 出场, 且 9 天里 0 次触发 —
方向确定 (家族 0/5 回摆成交) 但 $ 影响小; 利润引擎在 active 侧, 一行未动是正确的回归红线。
真正的出血点是**初始止损与隔夜跳空**, 不是净保本滑点 (处置建议见 §五②, 受 C1 约束不能改退出参数)。

## 三、链路当日行为核验 (9/2 盘 + 9/3 凌晨)

- **闸口实弹全对** (盘后文档归因段, 逐条与日志/影子对齐): 8/26 LLY 教训闸 6 次拦 DELL/ASTS 盘后反手
  `SELL_SHORT`; 方向偏斜闸把 9 条多数方 BUY 降 SKIP; 低 ATR 门拦 XLY/XLI/XLU/AAPL/LLYVK 5 条结构性死指令
  (根因已由 `fb302ea9` 前移到 UE 注入源, 今晚 21:10 首跑可验)。
- **守卫与自检**: 9/3 05:05 `daily_invariant_check` 仅 `I4_mfe_recalc×1` (AVGO id=1385, 已知 9/5 自清边界行);
  **无 `I15_stale_decision_id`** → 9/2 部署的陈旧 bridge 双层守卫当夜干净 (决策溯源全当日, P0-2 变体 21:01:35 重载实证)。
- **服务日志**: 9/2 21:00 → 9/3 08:31 区间 `ERROR` 仅 1 条 (04:01 TwelveData `time_series` read timeout, 外部 API, 单次),
  `Traceback` 0 条。
- **部署状态 (本批最重要的一项运维事实)**: `systemctl show ftseek-us` → `ActiveEnterTimestamp=2026-09-02 17:00:53 CST`,
  `Restart=on-failure`, `NRestarts=1`。9/3 当日 4 个含服务侧代码的提交 (`b43ab05d` / `832fb7d9` Task#30 /
  `ffb321c6` DELL 两修 / `e6187b29` 穿透埋点) **全部未生效**; cron 独立进程侧 (`fb302ea9` UE 预检、
  `832fb7d9` invariant 段) 已生效。→ 处置见 §六 重启窗口。
- **热重载暴露**: 08:31:48 日志实证 yaml 编辑即时进运行中服务 (`PlaybookConfig`/`FeatureFlags` 快照失效)。
  本次新增两键旧码无消费者 = 良性; 但热重载使 yaml **值**变更绕过提交/重启纪律 → 飞行中改 yaml 只应加
  "旧码不消费"的新键, 或改前先提交 (已写入 handoff 提醒)。

## 四、查实的问题与处置 (8 项)

### 1. KR2 判分式与日级冻结守卫读错列 → 结构性假绿 【P0 · 已修】
- **事实**: `config_version` 在本仓有两个语义 — `trade_records.config_version` **列** = 配置档案版本
  (实测恒 `v2`, n=92); `metadata_json.config_version` = **退出参数哈希** (`exit-*`, `app/playbook/config.py:188`)。
  KR2 判的是后者, 而 OKR 判分 SQL 与 `scripts/daily_invariant_check.py::_check_config_freeze` 都查前者。
- **证据**: 官方判分 SQL 实跑 → `{v2: 92}` 唯一值数=1 (永远"达成"); metadata 口径实跑 →
  `exit-7f5263af3cfd` 58 / `exit-2623be9c8879` 21 / `exit-f837b002896d` 12 (+1 行无哈希)。
  `data/invariant_check_latest.json.config_freeze.status` 恒 `ok`。周六周检
  `ops/hermes/ftseek_weekly_checkpoint.py:67` 一直读对 → **换版当天不告警, 最多拖 6 天**,
  W1 设计意图"损失封顶当天"落空。
- **影响**: 与 AGENT.md 军规第 4 类 (单位/口径混装) 和"KR2 快照漏键假绿"同族 — 尺子恒绿比没有尺子更危险。
- **处置**: `_check_config_freeze` 改两维 (档案列维原行为不动 + 退出哈希维接
  `config_freeze_baseline.json.exit_versions` 豁免清单对账), 输出 `exit_versions` 每哈希笔数
  (= cohort 样本量, KR2 事实仪表盘) / `exit_hash_missing_rows` (无哈希行只计数不当版本, 防首跑假漂移) /
  `exit_exempt_unobserved` (幽灵豁免可见但不告警 — "先登记后上线"是合法顺序) / `status` 两维取劣。
  真库实跑: `exit_status=ok` (3 版全在册豁免), 新增 `W1_exit_version_drift` 违规码待命。
  钉死 `tests/test_w1_exit_version_guard.py` 8 例。OKR 判分 SQL 同批勘误 (判据意图未改)。

### 2. KR2/C1 事实已 4 版, 判据与事实分叉 【P0 · 待用户裁决, 本批不擅自结算】
- **事实**: 8/24 起窗内 3 个退出哈希已成交 (+1 幽灵 `exit-d1120a105711` 在册零笔), 第 4 版
  `exit-de49b1299290` 今晚起生效。每版均经 DEB + 用户裁走豁免通道, 但 KR2 原文是"唯一值数=1, 换版即 KR2 破",
  且 `config_freeze_baseline.json` 自身注记写明"改本文件=改判据, 须先改 OKR/DEB" — **OKR 变更记录此前零行**。
- **影响**: 9/30 结算会自评失真 (按字面 SQL 是"达成", 按事实是"破"); 且每换一版, KR2 承诺的
  "250 笔单版本样本"就归零一次 — 8 月正是被 7 个版本切碎才作废。
- **处置**: 本批只把事实与两个选项写进 OKR 勘误段 + 判据字典 C1 行 (状态由"冻结中"改为
  "事实已 4 版, 待项目级裁决"), **不代用户改判据**:
  - **(a)** 修订 KR2 为按 cohort 分段判分 + 明写变更预算 (如每月 ≤1 次且必须影子先行);
  - **(b) (本复盘建议)** 宣布 9/3 起硬冻结至 9/30 — 算术仍可行: 19 交易日 (9/7 劳动节休市) × 11.3 笔/日
    ≈ **215 笔单版本样本**, 接近 250 目标, 是项目史上第一个统计可用窗口; 期间新发现的缺陷一律影子先行
    (C4 已要求), 缺陷修复排到 10 月。

### 3. 穿透影子: 一处 P0 写读契约断裂 + 三处登记缺失 + 台账 crontab 盲区 【P1 · 已修/已补】
- **P0 写读契约断裂 (本复盘查出, 修复先于任何生产标签)**: `penetration_outcome_backfill.py` 原
  `st = lab.pop("status")` 把 `status` 摘掉后才 `**lab` 落盘 → labeled 结局行**没有 status 字段**,
  而三个消费端都按 `type=="outcome" and status=="labeled"` 选行: `penetration_llm_analysis.load_samples`
  (判分/毕业门 `n_labeled`)、`shadow_board` 段11 (攒样看板 0/20)、脚本自身 `v_recovery_5m_this_run` 汇总。
  后果 = **研究环与三级防遗忘看板结构性静默零**: cron 照跑、jsonl 照长、看板照渲染, 但永远 0/20 不毕业。
  修 = `pop` → `get` (status 随行落盘, outcome 行口径统一 labeled/no_bars_after) + 7 例写读配对钉。
- **三登记缺失**: 落地时 `shadow_registry.py` (SSOT, 明文"新增影子必须在此注册, 否则无人跟踪=等于没做")、
  判据字典、控制环路台账**三处全缺** — 正是 DEB-20260901 §十一 严令禁止的游离观察。已补:
  注册表 `penetration_shadow` (OBSERVING, `min_n=20`, **`count_mode=where:type=penetration`** —
  默认 `lines` 会把"1 事件 + 1 结局"双计, 20 例门变 10 例真样) + 字典 **B17** + 台账 §2.3/§2.4
  `crontab-pen-backfill` / `crontab-pen-analysis`。`check_mounting()` 实测 0 告警。
- **载体心跳**: 穿透是稀有事件 (家族 5 案/12 交易日, 8/21→9/1 零案), "文件不存在"与"当期无事件"
  对注册表产出核对长得一样 → 初生豁免 (10 天) 过后必误报黄灯 (黄灯永悬正是本日刚退役的僵尸尺子病灶)。
  回填 cron 每日 `SHADOW.touch()` → mtime 新鲜度=回填环活着 (断流 >14 天告), 行数=样本量。
- **台账守卫盲区**: `tests/test_control_loop_ledger.py` 原先只核 Hermes `jobs.json` —
  实测 **68 个被 crontab 调用的仓内脚本仅 2 个在台账**, 穿透两环 9/3 挂 cron 时零拦截即首例实证。
  已加棘轮: 存量 64 个锁 `docs/references/crontab-loop-baseline.txt` (带理由 + 反向核对陈旧豁免),
  新增环路必须入台账或显式加基线; 台账 §5 G4 / §6.1 同步注记。存量 1:1 回填属专项 (§五④)。

### 4. 议题二 §八 验收判据不可执行 → 改判三层 + 预注册回滚线 【P1 · 已落 DEB §十二 / 字典 B17】
- **核算**: passive 侧 13/101 且 8/24→9/1 九天零事件, 基率 ≈0.4 案/交易日; 要在 passive 侧检出 ~$20/案
  改善 (结局 sd 量级 $20-40) 需 n≈50+ → **≈125 交易日**。原判据"被动侧净结局 vs 家族基线"永不触发
  = 第二个"观察 2 周后定阈值"悬置 7 周的僵尸尺子。
- **改判 (预注册, 窗内禁改)**: ①**机制判据逐事件即判** (0 次"3s 死等→撤单→市价"、滑点 ≤ pad+5bps、
  `path=marketable_limit` 逐笔可查、主动侧不回退); ②**回滚线** 前 10 笔被动侧成交**或 9/30 (取先到)**:
  ≥5 笔 `cf_floor_fill_saved_bps` > 垫成本 ∧ 中位节省 >20bps → 提请裁 `pad_bps=0` 回旧行为;
  ③**毕业门 (慢环)** n≥20 ∧ matched≥8 ∧ precision≥0.7 → 自动开 DEB 草稿呈裁。③不是①②的前置。

### 5. 部署滞后 + 未提交代码已上 live crontab 【P1 · 已提交, 重启见 §六】
- 服务持 9/2 17:00 旧码; 而 live crontab 已挂两行指向**未提交**脚本
  (`penetration_outcome_backfill.py` / `penetration_llm_analysis.py`) — 任何 `git clean -fd`/回滚都会让
  明晨 06:05 cron 失败, 且重启会把未入库代码载入生产。已按"核查后入库"处置: 独立跑触及套件
  **51 passed** 后提交为 `e6187b29` (作者归属与核查过程写进提交信息)。

### 6. 盘后文档"最大回撤"常量退化 + 量纲说谎 【P2 · 已修】
- `app/core/obsidian_writer.py:909` 读 `data['max_drawdown']`, 唯一调用方取 `summary.get('max_drawdown', 0)`,
  而 `_generate_summary` **从不产该键** → 恒印 `0.00%`, 与同日"净亏 −49.30 + 水下过夜仓"并列。
- 且源字段 `trade_records.max_drawdown` 是**百分点**口径 (`_max_drawdown_from_mfe_mae` = 100*(mfe+mae)/(100+mfe)),
  同行 `pnl_pct` 是小数 (GTLB: 3.6464 vs 0.0028) → 一旦接线, `.2%` 会把 3.65pp 印成 **364.64%**。
- 修: summary 产 `max_position_drawdown_pp` / `_symbol` / `_n` (键名带量纲后缀 + 非零样本数=基数自检),
  文档行改 `**最大单笔持仓回撤**: 3.65 pp (峰→谷) · US.GTLB | 当日 8/11 笔有非零回撤`;
  全零日如实显示 `0 笔`。钉死 `tests/test_postmarket_drawdown_caliber.py` 6 例 (含真库复算 43.82/0.8182 对齐)。
- **9/2 产物处置**: `obsidian_vault/daily/postmarket/2026-09-02_us_postmarket.md:20` 的 `最大回撤: 0.00%`
  **原行保留 + 下方打可见勘误标** (房例: 证伪假设打勘误标而非静默改写) — 生成物是历史证据,
  改写它等于销毁病灶样本; 修正值 (3.65 pp / `US.GTLB` / 8-of-11) 写在勘误标内。修复自 9/3 盘后起重启生效。

### 7. 落档债 【P2 · 本批已补当日, 结构性建议见 §五】
- `handoff.md` 最后更新停在 9/2, 而 9/3 已有 8 个提交 (宪章三.3; 8/26→8/31 曾断更 5 天被迫按 git 史回溯重建) → 本批补齐。
- 9/2 `premarket`/`postmarket` vault 产物与 24 张 symbol 卡未入库 (房例是"收盘后落档批") → 本批入库。
- `bench/` 仍未跟踪: 9/2 模型评测 (qwen3.8-max/-0902, kimi-k3, qwen3.8-flash), **未测生产模型**
  (`.env.us` `BAILIAN_MODEL_MAIN=qwen-max`), 且 6 任务×1 次运行 n=1 → 不足为选型依据;
  唯一 actionable 信号是 T4 反幻觉 qwen3.8-max 0.20 vs 其他 1.00, 若考虑迁移需重跑 ≥5 次取分布。
  处置建议: 归档到 `data/analysis/` 或补跑后落 DEB, 不要留在工作区当未跟踪残留 (§五⑧)。
- 小项: `.git/hooks/pre-commit` 与 `commit-msg` 引用的 `SYSTEM_OBJECTIVE.md §5` **不在本仓**
  (实体在 `/opt/ftseek/FTSeek/SYSTEM_OBJECTIVE.md`) — 审批规则的依据文件跨仓, 本仓无从查证。
  建议本仓落一份指针或副本 (未做, 属治理文件, 需用户裁)。

### 8. 测试污染生产判分数据 (跨 23 天累积, 提交前查出) 【P1 · 已修 + 已清洗】
- **取证方式**: 给 `builtins.open` / `Path.open` / `Path.write_text` 挂探针 (写入前自动备份原文件) 跑全量
  套件 (2443 passed) → **33 次写入落在生产 `data/` 下 4 个文件**; 真库 `data/ftseek_us.db` sha256 前后一致
  (测试用 tmp 库) → 交易台账零污染。

| 载体 | 污染量 | 载体性质 |
|---|---|---|
| `data/early_dead_signal_shadow.jsonl` | **2307 / 2565 行 (90%)** `symbol=TEST`, `checkpoint` 恒 30, `mfe_pct` 恒 ≥0.3, `settled` 恒 false; 时戳跨 **8/11→9/3** | DEB-20260811 预注册裁决样本 |
| `data/audit/playbook_reconciliation.jsonl` | **363 / 363 行 (100%)** 虚构 `void_instruction` (`US.TEST`/`US.LOSS`, reason "幻影敞口清理" 该字符串全仓仅出现在 `tests/test_playbook_audit.py:106`) | 人工/自动对账留痕 |
| `data/recommendations/daily_*.json` | **40 / 41 个文件**是合成 payload (恒 AAPL/BUY/`180.00-185.00`/conf 0.85), 跨 6/20→9/3; 唯一真实产物 `daily_2026-05-28.json` | 每日推荐产物 |
| `data/vwap_gate_ab.jsonl` | **无残留** (测试自带事后擦行 fixture, 内容与备份逐字节相同) — 但与生产写者竞争同一文件 | A/B 实验台账 |

- **根因**: 三处埋点/持久化路径在函数体内**就地拼字符串**, 测试无从隔离 —
  `pipeline.py` 死单影子 `_EDP("data/early_dead_signal_shadow.jsonl")` + 两处 vwap 台账
  `_Pab(__file__)…/vwap_gate_ab.jsonl`, 以及 `audit._default_audit_path()` 无测试接管。
  对照组正是本批刚入库的正例: `penetration_shadow._PEN_PATH` **有缝** → 其测试全量 monkeypatch, 零污染。
- **影响分级 (诚实口径, 不夸大)**:
  - **机器判据未受损**: TEST 行 `mfe_pct` 恒 ≥0.3 → 30min 命中判定 (`mfe<0.2`) 计 0, 故预注册门
    "攒样≥30 命中"的分子干净 (真实 30min 命中 **25** / 15min 命中 **26**);
    `early_dead_signal_report.py:82` 的死单率分母是**命中行** (`dead_hit/n_hit`) → 未被灌水。
  - **人/LLM 判读面被系统性误导**: `shadow_board` 段10 近7天印 "30min行**1366** 命中7" (=0.5%) vs
    真实 "30min行**31** 命中7" (=22.6%) → **分母虚增 44×** (今日 220 vs 7 = 31×)。任何按行数估攒样进度、
    或按"命中/行"估死单率的读法都会得出**反向**结论。
  - **结算脚本每晚白扫** 2307 行永不可结算样本 (`symbol=TEST` 无对应 `trade_records`, `settled` 恒 false)
    → 无效 IO + "待结算"噪声常驻。
  - **审计载体丧失证据价值**: 363 行全虚构 = 该文件从未记过一件真事; 若将来发生真 phantom void,
    留痕会淹没在伪造行里 (生产 `playbook_instructions` 1694 行, `US.TEST`/`US.LOSS`/`US.AUDIT` 零匹配 →
    确认这些 void 只作用于测试的 tmp 库)。
- **处置 (缝 → 隔离+硬闸 → 清洗, 三步)**:
  1. **缝**: 三处路径提为模块常量 `_EARLY_DEAD_SHADOW_PATH` / `_VWAP_AB_LEDGER_PATH`
     (`app/playbook/pipeline.py`) — **值与解析语义逐字不变** (systemd `WorkingDirectory=/opt/ftseek/FTSeek-US`,
     相对/绝对两种既有写法各自保持), 只是把"写哪"变成可重定向的一个名字。
  2. **隔离 + 硬闸**: `tests/conftest.py` 把 `_PRODUCTION_DATA_SEAMS` 自动指向会话级 tmp 沙箱;
     未登记的 `data/` 写入 (`open` 写模式 / `Path.open` / `write_text` / `write_bytes`) **当场 RuntimeError**
     并指路三条修法; 显式豁免走 `@pytest.mark.allow_production_data_write` (`pytest.ini` 已登记 marker,
     须注明理由) — 同 `crontab-loop-baseline.txt` 的棘轮纪律: 新增埋点必须登记缝, 否则 CI 即刻红。
     `tests/test_recommendations.py` 3 处改传 `output_dir=tmp_path`;
     `tests/test_vwap_gate_ab.py` 台账路径改**跟随活跃缝** (不再直写生产 + 事后擦行)。
  3. **清洗**: 2307 TEST 行 / 363 虚构审计行 / 40 个合成推荐 JSON 移出生产, **隔离留痕**
     `data/_quarantine/20260903_test_pollution/` (README + `.orig` 完整副本 → 可整体回滚;
     审计文件保留为空载体, 读端按行解析对空文件返回 `[]`)。
- **验证**: 探针复跑全量 → **2452 passed / 37 skipped / 0 failed, 生产 `data/` 写入事件 0**, 真库 sha 未变;
  `shadow_board` 段10 复算已回真值 (近7天 31 行 / 7 命中)。钉死
  `tests/test_production_data_write_isolation.py` **9 例** (缝存在的源码契约 / 重定向生效 / 硬闸拦
  `open`+`write_text`+`write_bytes` / 读与 tmp 与沙箱放行 / 豁免阀功能可用 / 三条生产载体棘轮)。

## 五、建议 (未做 — 需裁决或另案, 按性价比排序)

1. **今天就裁 §四.2 的 (a)/(b)** — 这是唯一会让 9 月样本作废或救活的决定; 本复盘建议 **(b) 硬冻结至 9/30**,
   并把"缺陷修复排队到 10 月"写成明规则 (影子照攒, 执行不动)。
2. **注意力配置对齐出血点**: `SL_INITIAL` (8 笔 −1069, 均 −133.63) + `OVERNIGHT_GAP` (−380) = 97% 亏损。
   但 OKR 已证"退出调参不能改变毛期望", 且 C1 冻结 → **不走改参数**, 走两条已存在的轨道:
   ①入场侧 (KR3 预注册单假设 / Phase C 确认位限价, DELL 正是其教科书样本 CF $0 vs −49.30);
   ②隔夜敞口政策 (`B15` 判罪线 n=6/20、`B16` 赢家隔夜影子 9/2 起攒样) — **别再新开战线**。
3. **穿透影子历史重建 (可选高杠杆)**: 历史 13 条 passive 事件的特征已在
   `trade_records.metadata_json._be_close_telemetry` (side/path/floor_px/检测价/时戳), 1 分 bar 在
   `data/replay_bars` (1793 文件) → 可重建穿透行并跑同一回填, 把 n 从 0 推到 ~10, 省 1-3 个月。
   **必须打 `reconstructed=true` 并与实时行分段判分** (重建行无 quote_src/轮询间隙语义), 属另案需裁决。
4. **台账存量 1:1 回填专项**: 64 个 crontab 环路逐个写清层级/角色/误差信号 (写假行比不写更糟, 故未顺手做)。
   建议按层分批 (日级测量 → 周级裁决 → 采集/回填), 每批 10-15 行, 从基线文件删行即进度可见。
5. **Phase C 裁决包预备**: `pullback_limit_replay.jsonl` 现 16 行 (门槛 n≥20), 预计 1-2 交易日达标。
   建议现在就写好取数+三判据实算脚本与决策草稿, 达标当天即可裁; 判分时注意议题一修尺子后
   `n_chase_cap_violated` 口径断裂 (违例计数会上升, 因升级违例不再被吞) → **按 9/3 前后分段**, 不得读成"违例变多"。
6. **盘后文档加统计口径行** (KR1c): 固定一行"日毛 +96.27 / 6 日窗口按日聚类 t=0.61 / MDE ±$335 日 →
   本日结果不构成 edge 证据", 让日复盘不再被叙事牵着走。
7. **方案D (事件驱动退出) 列 10 月首项**: $18.40/案 (37%) 的轮询间隙击穿分量只有它能治;
   设计输入正是本影子的 `depth_bps` / `continued_bps_10m` 分布。冻结期内只攒样不实施。
8. **`bench/` 处置**: 见 §四.7 — 归档或补跑 (≥5 次/任务, 含生产 `qwen-max` 臂) 后落 DEB, 别留未跟踪残留。
9. **死单早期影子补三登记 (游离观察, §四.8 附带查出)**: `early_dead_signal_shadow` 有埋点 (`pipeline.py`)
   + 看板段10 (`shadow_board.py:331`) + 结算脚本 (`early_dead_signal_report.py`) + 预注册判据
   (写在代码注释: 攒样≥30 命中 ∧ ①双窗死单率均≥55% ②命中组实际净PnL<0 ③差集结局为负),
   但 **`shadow_registry.py` (SSOT) 与 `ADJUDICATED-CRITERIA.md` 双缺** → 与 §四.3 穿透影子同病:
   挂载哨兵看不见它 (断流无人知), 判据不在字典 (结算时无据可引)。
   本批只清了它的数据污染, **未代裁注册** — `min_n`/毕业门须逐字转录 DEB-20260811 并单独核一遍,
   建议与 §五④ 台账回填同批做。清洗后真实进度: 30min 命中 **25** / 15min 命中 **26** (门 ≥30),
   按近7天 7 命中速率约 1 周达标 → **注册宜早不宜迟** (达标当天若无字典条目, 又是无据可裁)。

## 六、执行记录 (2026-09-03, 本批)

| # | 动作 | 载体 |
|---|---|---|
| 1 | 核查并入库另一 Agent 的穿透影子三件套 (触及套件 51 passed 后提交) | `e6187b29` |
| 2 | W1 冻结守卫两维化 (退出哈希维 + 豁免对账 + cohort 可见 + 幽灵豁免可见) | `scripts/daily_invariant_check.py`, `tests/test_w1_exit_version_guard.py` 8 例 |
| 3 | 回填 `status` 写读契约 P0 修 + 载体心跳 | `scripts/penetration_outcome_backfill.py`, `tests/test_penetration_shadow.py` 第 6 组 7 例 (全文件 23) |
| 4 | 穿透影子注册表登记 (含 `count_mode` 防双计) | `app/playbook/shadow_registry.py` |
| 5 | 台账挂两行 + G4/§6.1 注记 + crontab 棘轮守卫与存量基线 | `docs/CONTROL-LOOP-LEDGER.md`, `tests/test_control_loop_ledger.py` 4 例 (全文件 10), `docs/references/crontab-loop-baseline.txt` 64 行 |
| 6 | 判据字典 B17 (三层判据 + 回滚线 + 基率披露) + C1/C7 状态如实化 | `docs/ADJUDICATED-CRITERIA.md` |
| 7 | DEB §十二 (判据可执行性核算 + 改判) / §十三 (实施核查 + 三登记 + cohort 注记) | `docs/debates/DEB-20260903-dell-dual-failure.md` |
| 8 | OKR KR2 判分 SQL 勘误 + 事实披露 + 待裁二选一 + 变更记录行 | `docs/OKR-2026-09.md` |
| 9 | 盘后"最大回撤"常量退化 + 量纲修复 (键名带 `_pp`, 带非零样本数) | `app/review/daily_review.py`, `app/core/obsidian_writer.py`, `tests/test_postmarket_drawdown_caliber.py` 6 例 |
| 10 | 复盘落档 (本文件) + handoff 补 9/3 全部条目 + 9/2 vault 产物入库 (含勘误标) | 本文件, `handoff.md`, `obsidian_vault/daily/*` |
| 11 | 生产 `data/` 写入缝 (三处埋点路径提模块常量, 值/解析语义不变) | `app/playbook/pipeline.py` |
| 12 | 测试隔离 + 硬闸 (缝自动重定向沙箱 / 未登记写入报错 / 豁免 marker) | `tests/conftest.py`, `pytest.ini`, `tests/test_recommendations.py`, `tests/test_vwap_gate_ab.py` |
| 13 | 污染清洗 + 隔离留痕 (2307 行 + 363 行 + 40 个合成 JSON, 附 `.orig` 可回滚) | `data/_quarantine/20260903_test_pollution/` (README) |
| 14 | 写入隔离钉子 (缝契约/重定向/硬闸/豁免阀/三条棘轮) | `tests/test_production_data_write_isolation.py` 9 例 |
| 15 | 重启 + 启动冒烟 (军规: 服务侧改动必重启必冒烟) | `systemctl` **10:42:29 CST** + `startup_smoke_check.py` **7/7** |

**提交**: `8399715b` (①测量/治理: W1 两维 + 台账 crontab 棘轮 + 判据/OKR 勘误) ·
`cc771bd7` (②穿透影子 P0 契约 + 三登记 + 载体心跳 + DEB §十二/§十三) ·
`b106b9da` (③测试污染: 缝 + 沙箱重定向 + 硬闸 + 清洗隔离) ·
`3c3c42f6` (④回撤口径 + 本文件落档 + 9/2 vault 产物) · handoff 条目随后一提交。
`bench/` 保持未跟踪 (§五⑧ 待裁)。

**KR2 影响**: 本批零退出参数改动 → 退出哈希仍 `exit-de49b1299290` (上一批已登记豁免), 无新版本。
**生效面**: 2/3/4/9 属服务侧或独立脚本 — 脚本类 (invariant/backfill) 下次 cron 即生效;
服务侧 (obsidian_writer/daily_review 在 `scan_loop` 驱动的日复盘里) 随重启生效。

## 七、今晚 / 明晨判读清单

- **重启窗口 — 已执行 ✅**: `systemctl restart ftseek-us` **10:42:29 CST** (早于 19:28 盘前推送窗口) →
  启动冒烟 **7/7 全绿** (启动段 10:42:35 新鲜), 重启后日志 ERROR/Traceback/CRITICAL **0** 条。
  → §三 所记 9/3 四个未生效的服务侧提交 (`b43ab05d`/`832fb7d9`/`ffb321c6`/`e6187b29`) 与本批四提交
  (`8399715b`/`cc771bd7`/`b106b9da`/`3c3c42f6`) **一并生效**。
- 19:28 钉钉**恰一条**晨报 (两条=skill 回滚, 零条=查服务推送任务)。
- 21:00 生成批 did 全当日 (`P1 陈旧bridge滚动重载成功` 或 P0-2 变体); 21:10 UE 注入列表**无** XLY/XLI/XLU/AAPL/LLYVK 且执行门 `low_atr` 拦截归零。
- 21:30 后: 追高帽**每次**违例都有 warning (议题一); 若发生 BE 被动侧出场 → 核 `_be_close_telemetry.path=marketable_limit` / `pad_bps=15` 与 `data/shadow/penetration_shadow.jsonl` 首行 (**不落盘=埋点静默死亡, 当场查**)。
- 明晨 05:05 invariant: 无 `I15`, `I4` AVGO 行 9/5 自清; `config_freeze` 段应出现 `exit_versions` 三 cohort + `exit_status`。
- 明晨 06:05 穿透回填首跑 (应见载体心跳: jsonl 存在, `events_total=0` 属正常); 06:15 shadow_board 段11 应显示"尚无穿透样本"或实际计数 (不再可能恒 0/20 而 jsonl 有行)。
- 9/5 周六 09:05 周检: 首次以新版跑 (挂载哨兵 + KR2 豁免通道 + 台账 crontab 棘轮守卫)。

## 八、诚实边界 (本批未做)

- **未改任何交易行为、未改任何退出参数** (C1/C7/C4 合规); 议题二 marketable limit 是上一批已裁事项, 本批只补判据与登记。
- **未代用户裁决 KR2** (§四.2 二选一挂待裁); 未删 §八 原文 (裁决留痕), 只追加改判段。
- **未做穿透影子历史重建** (§五③, 需裁决); 未做台账存量 1:1 回填 (§五④, 专项); 未动 `bench/` (§五⑧)。
- **未修 pre-commit/commit-msg 钩子跨仓引用** (§四.7 小项, 治理文件需用户裁)。
- 全量回归: 本文件首版落档时点 2443 passed; §四.8 修复后复跑 **2452 passed / 37 skipped / 0 failed**
  (108.56s), 且探针实证**生产 `data/` 写入事件 0**、真库 sha256 未变。
  重启 **10:42:29 CST** + 启动冒烟 **7/7** (启动段 10:42:35 新鲜), 重启后日志零 ERROR/Traceback/CRITICAL。
- **勘误 (对首版落档的自我更正)**: 首版曾记"已把测试污染的两个文件从快照还原, 生产影子零污染" —
  复核证伪: 还原发生在最后一次全量跑之前, 且当时只看见 2 个文件 (实为 4 个)、只做了还原没做根因隔离,
  故污染照旧累积 (§四.8 取证即在此复核中做出)。现改为**缝 + 硬闸 + 清洗 + 棘轮**四层, 不依赖人工还原。
- 未代裁 §五⑨ (死单影子三登记) — 注册须逐字转录 DEB-20260811 判据, 属另案;
  隔离区 `data/_quarantine/20260903_test_pollution/` 保留全部清除物与 `.orig` 副本, 用户可一键回滚。
- 统计口径: 本文所有 t/MDE 均按**日聚类**给出 (宪章第 9 律); 毛/净口径逐处标注, `realized_pnl` 列是毛。
