# RFC-106 复盘口径点名 + 退出桶标签诚实化 + 指标自净

**日期**: 2026-09-24
**状态**: 已落地 (用户令「先修复盘工程 2+3」)
**风险级别**: L1 (渲染/文案/注释; **零交易行为变更**, 不触碰任何门禁、退出、下单路径)
**证据来源**: 2026-09-23 盘后综合复盘 (cron job `7a40dd7c9e94`) 全文 + `data/ftseek_us.db` 逐笔复算

---

## 1. 背景: 一份报告里三处会把复盘带偏的地方

9/23 复盘报告的行动项 P0 写的是:

> 需查 DB 确认两笔触发的 **gap%**、标的是否符合触发条件…若确认属正常触发,则属已知风险

三处缺陷让这句话不可能被正确执行:

| # | 缺陷 | 实证 | 后果 |
|---|---|---|---|
| A | 「近7日 48笔 净-$644.34」被标成**出场日口径** | 该数字来自 `post_market_audit.attribution_exit`, SQL 是 `WHERE trade_date >= ?` = **入场日分桶**; 脚本自身注释已写「7 日窗口按入场日分桶属边界语义」 | 按出场日复算永远找不到同一组数字 (出场日最近 = 53笔/-$586.68) ⇒ 每次复盘都要重走一遍口径对账 |
| B | `OVERNIGHT_GAP_STOP` 被当成**跳空事件**读 | 两个产出点 (`exit_engine.py` 规则2 / `pipeline._sl_fallback_exit_reason`) 判据都只是「持仓过夜 且 亏损 ≥ `overnight_gap_stop_pct`」, **无任何 gap 校验**, metadata 也不落 gap 字段; 桶内 5 笔里 GAP(09:30)/UBER 是开盘出场, CRM(09:49)/RIVN(10:23) 是盘中触发 | P0 让人去查一个**不存在的字段**; 归因把隔夜仓尾部损失记成跳空事件 |
| C | `SHORT 盈亏比 4360x` 被当成**极端异常值** | `scripts/daily_direction_attribution.py` 分母回退 `0.001` (`avg_loss = ... else 0.001`) ⇒ 2 笔全胜小样本算出 4.360%/0.001% = 4360x; 报告据此写「单笔贡献占 ~100%」 | 退化读数被讲成事件; 下游 `pnl_ratio < 0.85` 告警与 OKR 判据吃到伪值 |

---

## 2. 变更

### 2.1 口径点名 (缺陷 A)

`scripts/post_market_audit.py` — 让口径随数字一起输出, 不再依赖读者推断:

- 费用净额总览: `**今日**(出场日口径, substr(exit_time,1,10))` / `**近7日**(入场日口径, trade_date 分桶)`
- 退出归因段标题: `## 🚪 退出归因 (近7日·按入场日 trade_date 分桶, 非出场日)`

复盘的 cron prompt 要求「逐字引用脚本输出」⇒ 口径标签现在**随数字传递**, 引用即自带口径。

### 2.2 退出桶标签诚实化 (缺陷 B)

- `app/core/models.py`: `ExitReason.OVERNIGHT_GAP_STOP` 注释勘误 (无 gap 校验)
- `app/playbook/exit_engine.py`: 模块 docstring + 规则2 说明勘误; `_is_overnight_position` docstring 勘误
- `app/playbook/pipeline.py`: `_sl_fallback_exit_reason` docstring 补勘误段 (附 9/23 桶内 5 笔的实盘反例)
- `app/push/message.py`: 推送显示名 `隔夜跳空止损` → `隔夜深亏止损 (非 gap 专属, 见 exit_engine 规则2)`
- `scripts/post_market_audit.py`: 退出归因段对 `OVERNIGHT_GAP_STOP` 自动追加一行口径注记 (`_EXIT_REASON_LEGEND`), 内容: 判据 = 持仓过夜 且 亏损≥阈值, 含开盘跳空打穿与盘中触发两类, **无 gap% 字段可查**

**未做全量改名**: 改名 enum 值需同时动 5 行历史 DB + ~12 处消费点 (push/dashboard/reentry/两个 replay 脚本/3 个测试) + 破坏全部历史报告与 vault 归档的可比性 ⇒ 属 L2, 非本轮"零行为变更"范围。改名与否留待裁决 (见 §5)。

### 2.3 指标自净 (缺陷 C)

`scripts/daily_direction_attribution.py`:

- `pnl_ratio` 在分母为 0 (无败笔, 或败笔 fwd 全为 0.00%) 时返回 `None`, 不再回退假分母 `0.001` / 不再回退 `99`
- 新增渲染助手 `_fmt_ratio()`: `None` → `N/A`
- 两个消费点加 None 守卫: 异常检测 `pnl_ratio < 0.85`、OKR 「LONG盈亏比 ≥1.1x」判据
- `avg_loss` 无败笔时由 `0.001` 改为 `0.0` (语义正确; `expectancy` 在 wr=1 时与之无关, 无影响)

---

## 3. 验证 (实测, 非声称)

`venv/bin/python -m pytest tests/test_daily_direction_attribution.py tests/playbook/test_sl_fallback_gap_attribution.py -q` → **16 passed** (含 2 个新增回归测试)

新增测试 (旧口径必红):

- `test_zero_loser_group_reports_na_not_fake_ratio` — 全胜组 `pnl_ratio is None`、`avg_loss == 0.0`、渲染含 `N/A` 且不含 `4360`
- `test_ratio_still_computed_when_losers_exist` — 对照组: 2.0%/1.0% 仍须算出 `2.00x` (防"修一个坏一个")

实跑 `build_report()` (只读归因, 未过 PushHub) 输出核验:

```
## 💰 费用净额总览 (毛/费/净)
  **今日**(出场日口径, substr(exit_time,1,10)): 6笔 毛$-377.68 费$32.76 → **净$-410.44** | 净胜率50%
  **近7日**(入场日口径, trade_date 分桶): 48笔 毛$+152.23 费$796.57 → **净$-644.34** | 净胜率77% | ⚠️假盈利3笔
## 🚪 退出归因 (近7日·按入场日 trade_date 分桶, 非出场日)
  🔴 OVERNIGHT_GAP_STOP: 2笔 净胜率0% PnL=$-588 avg=$-294
    ⓘ OVERNIGHT_GAP_STOP 口径: 隔夜深亏止损 (判据=持仓过夜 且 亏损≥overnight_gap_stop_pct, 默认3%); 含开盘跳空打穿与盘中触发两类 ⇒ 无 gap% 字段可查, 不要按"跳空"解读
  **合计**: 48笔 PnL=$-644
```

`python -c "from app.core.models import ExitReason"` + 6 文件 `ast.parse` 全通过; `git diff` 逐行复核, 未触及 `if/门禁` 任一条分支。

---

## 4. 生效范围

| 面 | 生效时点 | 说明 |
|---|---|---|
| 盘后复盘报告 | **立即** | `post_market_audit.py` / `daily_direction_attribution.py` 每次由 cron 新起进程, 下次跑即含新口径 |
| 服务进程内推送文案 | 下次重启后 | 改的是 `app/push/message.py`; **不为此重启** (纯文案, 按需重启纪律) |
| 交易行为 | 无变化 | 未改任何门禁判据/阈值/退出条件; `_HARD_STOP_REASONS` 集合成员未变 |

## 5. 未决

1. **是否全量改名** `OVERNIGHT_GAP_STOP` → 语义正确的新值。收益: 名称即真相; 代价: 历史 5 行 + ~12 消费点 + 历史归档可比性断裂 = L2, 需辩论/门禁。
2. **是否给该桶落 gap 证据字段**。`_check_playbook_exit_rules` 入参只有 `current_price`, **无 `prev_close`/`today_open`** ⇒ 真 gap 判定需新增取数管道 (不改判据只记事实), 属独立提案。
3. ~~归档 `ftseek_us.log-20260923.gz` 0 条告警~~ → 已查清并修复, 见 §6。

---

## 6. 追加 (2026-09-24 第二轮): 告警链一个真 bug + 复盘 cron 三处增补

### 6.1 真 bug: gz 轮转档静默 0 条 (数据丢失)

`resolve_log_sources` 会**优先返回** `*-YYYYMMDD.gz`（logrotate `compress` 产物），
但两个读取点 (`parse_log_alerts` / 覆盖诊断) 都用普通 `open()` ⇒ gz 档被当文本读，
二进制头解析不出任何时间戳行，**静默返回 0 条告警**，而诊断行只写 `xxx.gz=0`，
读者无从区分「档在窗外」与「读不了」。

**实测证据**（`ftseek_us.log-20260923.gz`，真实范围 09-22 03:29→09-23 03:26）：

| 口径 | 窗口 09-22 21:00→09-23 03:00 的告警数 |
|---|---|
| 修前 (普通 `open`) | **0** |
| 修后 (`gzip.open`) | **329** |

⇒ 任何**跨 ≥1 次轮转**的窗口（周报 / 补跑 / 晚跑）此前会丢掉整段归档期告警。
日常 21:00→06:00 窗恰好只用未压缩的最近轮转档，所以长期未暴露。
另一处同构实现 `scripts/error_alert.py` 自查**有** `import gzip` ⇒ 无此病。

**修法与钉子**：新增单一真源 `_open_log(path)`（按扩展名分派 gz/文本），两个读取点共用；
`tests/test_daily_alert_summary_log_sources.py` 原 `test_gz_rotated_variant_is_discovered`
只写空字节 `b""` 就断言"文件名被发现"——**测了发现、漏了可读**，所以 bug 一直绿。
已改为写**真实 gzip 内容**并断言条数非 0（修前必红）。

### 6.2 假告警: 覆盖诊断把「窗外档」读成「覆盖不全」

9/23 报告写「⚠️ 告警覆盖可能不全：`ftseek_us.log-20260923.gz=0`（归档文件无告警），
主档跨度 21:02→04:00 短于会话窗」。实测该档自身范围 09-22 03:29→09-23 03:26 与窗口
09-23 21:00→09-24 06:00 **根本不相交**，0 条是必然。另「告警跨度」是告警时间戳的 min/max，
**不等于档的覆盖范围**，二者混读必然误判。

**修法**：逐档补报**档自身时间范围** + `[窗外]`/`[窗内无告警]` 属性，并给出**盲区定论**
（窗内档最早起点是否 ≤ 窗起，容差 5min）；「告警跨度」改名「告警时间跨度(非覆盖)」。
实测 9/23 原窗修后输出：

```
US 日志源 3 个 (ftseek_us.log-20260923.gz=0[窗外 09-22 03:29→09-23 03:26],
  ftseek_us.log-20260924=409, ftseek_us.log=16) | 告警时间跨度(非覆盖) 21:02:11→04:34:57
  | 窗 09-23 21:00→09-24 06:00 | 窗内档 2 个, 最早档起 09-23 03:27 ≤ 窗起 ⇒ 无盲区
```

### 6.3 复盘 cron prompt 三处增补 (job `7a40dd7c9e94`, 经 `hermes cron edit`)

| 位置 | 增补 | 针对 |
|---|---|---|
| 口径纪律 0b (新) | 近N日窗口 = 入场日口径 `trade_date`，禁止标成出场日；脚本标签须逐字保留 | §2.1 口径错标 |
| 行动项纪律 7 (新) | 行动项必须出**单一定论**＋判定依据；禁止「若 A…若 B…」并列两种可能；证据不足须写「需 X 才能判定」 | 9/23 P0 让查不存在的 gap% 字段 |
| 告警覆盖行 (改) | ⚠️ 只认脚本定论（含「⚠️ 覆盖盲区」才写、含「无盲区」禁止自行推断）；跨度≠覆盖 | §6.2 假告警 |

**生效核实**（外部状态回读，非工具回执）：线上 `jobs.json` 该 job 的 prompt
sha256 `e070a99134169d6f` 与预期文件**逐字节一致**（len 9601）；
比对备份确认**只有该 job 变化**（46 个 job 无一牵连）；`enabled=True`，下次 2026-09-25 04:00 CST。
备份：`jobs.json.bak-20260924-p0-verdict`（改前）、`jobs.json.bak-20260924-alert-coverage`（第二改前）。

**验证**：`pytest tests/test_daily_alert_summary*.py tests/test_error_alert.py -q` → **91 passed**。