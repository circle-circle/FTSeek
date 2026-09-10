---
title: 日志/可观测性批 — 盘后电池 32 轮空转 + 死回滚开关 + WARNING 级别通胀 + error_alert 恒不触发
date: 2026-09-10
status: 已落地 + **已重启验证** (②④ 零行为变更 2026-09-10 上午; §9 七项推荐案经用户裁「按照建议执行优化」全部落地, 见 §7.1–§7.7 回执; 收尾批 3 处修复见 §7.8; 用户裁「请重启再观察」⇒ PID 889343→**910996** @ **13:01:47**, 冒烟 **5/7**, 2 项红经归因均为他人在途批、§3 两处降级已线上实证, 见 §7.9) + 判据待复算 (§1.5/§2.4 下一交易日起可复算; **§3.4 时钟自 2026-09-10 13:01:47 CST 起算**, 复算须扣除他人新增的 2 个 WARNING 族, 见 §7.9) + **两处声称已更正** (§4.3 的"可触发性"用 3 条探针过了, 但被守卫者真实只发 1 条 ⇒ W1~W6 违反**不会推钉钉**, 实测与修法见 §7.6 更正框 / §8.11 / §9.1④)
trigger: 用户令「请检查日志记录, 深度分析看看是否有什么值得调整优化的问题点」→ 复查后令「按照建议执行」→ 汇报 §9 七项待裁清单后再令「按照建议执行优化」(= 按各项**推荐案**执行: 1A / 2A 的第②步配置停 / 3 两处降级 + 附带单 record / 4A / 5 棘轮插队 / 6 只加棘轮不改名 / 7 补六面核对后退役)。执行方按建议分流: 零行为变更直改, 行为/判据变更落本 DEB (宪章 §四.1 新机制排队 + §三.1 一事一 DEB)。
---

# 日志/可观测性批

> 全部数字可用本文各节附的命令复算。主日志口径 = `logs/ftseek_us.log`(当前档) +
> `logs/ftseek_us.log-YYYYMMDD[.gz]`(logrotate `dateext -%Y%m%d` 档)。
> 服务器 TZ = Asia/Shanghai; 美股盘后 16:00–20:00 ET = 北京 **04:00–08:00**。
> **诚实披露**: §1 的「一晚 ~8992 次请求」是 `32 轮 × 281 票` 的算式外推, 未直接抓 OpenD
> 侧计数; 但 32 轮与 281 票均为日志实测, 且每轮 `time.sleep(1.2)` 无条件执行是源码事实。

## 0. TL;DR

1. **盘后影子电池每晚全量重跑 32 轮, 其中 31 轮零产出。** 单轮 ~7m47s 里 ~7m45s 是
   `refresh_daily_bars` 对 281 个标的各发一次 Futu 历史K线请求 + 1.2s 限流睡眠。
   28 轮的收尾行是 `完成: 刷新 0 | 已最新跳过 280 | 失败 1`。32×7.75min ≈ 4.1 小时
   ≈ **整个盘后窗口被同一批重复计算占满**。
2. **「幂等」是数据级不是执行级, 且提交信息与代码不符。** `3f2d946c`(2026-08-17) 称
   「幂等验证: 已最新110票零拉取」; 实际 `start = last`(含末日重拉) ⇒ **每轮每票必发请求**,
   「跳过」判定发生在 API 调用**之后**; 票池已从 110 涨到 281。
3. **`settle_overnight_shadow` 的回滚开关是死的, 且整条链 write-only。**
   `overnight_hold_shadow_enabled: false` + 注册表 `GRADUATED`, 但挂载点与脚本都不读该键;
   其产出字段 live 代码零消费 (只有一个测试读); 毕业后的生产判罪线 `overnight_arm_report.py`
   读的是 DB `trade_records.metadata_json.overnight_arm`, **根本不读该 jsonl**。
4. **27.8% 的 WARNING 是级别误用。** 7 日 WARNING 1721 / ERROR+CRITICAL 12 (143:1);
   其中 `app.positions.reconciler` 359 条(成功摘要横幅) + `ftseek_us.config_audit` 120 条
   (自带 `[INFO]` 标签的非阻断项) = 479 条零信息量。
5. **`error_alert.sh` 恒不触发。** THRESHOLD=20 条/15min, 实测 ERROR+CRITICAL 0–5 条/**日**
   ⇒ 近 7 日 0 次告警。另有 4 处独立缺陷 (§4)。
6. **已落地(②④, 上午批)**: 主日志格式加 `%(threadName)s`(6 个下游解析面实测通过) / 唯一未命名线程补名 /
   7 个 in-process 脚本 print→logger(实测占主日志 60.9% 的噪声源) / scheduler 23 处隔离异常收口
   (19 补 `exc_info` + 4 `debug→warning`) / logrotate 三项 / `logs/` 顶层 1107→915。
7. **已落地(§9 七项推荐案, 午间批, 用户裁「按照建议执行优化」→ §7.3–§7.7)**: `refresh_daily_bars`
   执行级日门 + 失败有限重试(只重试失败票, 3 轮封顶) / 结算器新键 `overnight_hold_settler_enabled:
   false`(**fail-closed**) + 注册表补登记**两个挂载面** / 两处 WARNING 降级为单条 INFO record
   (异常分支一律不动) / `error_alert.sh` → `error_alert.py`(六缺陷逐条修 + 相对基线阈值 `max(3,⌈3M⌉)` +
   逐指纹冷却 + push hub + 覆盖面改 glob 全 `logs/*.log`, §4.3 四判据**真推送实机验证**通过) /
   W4·W5·W6 三条日志卫生棘轮接入每晚不变量自检(存量 704·15·1135 处逐文件登记, 只降不升) /
   两个死审计组件六面核对后退役。新增钉子 **67 例**(7+7+9+28+16), 全量回归见 §7 末尾。
8. **收尾批 (同日 11:35–12:10, §7.8)**: 全量回归 `3674 passed / **2 failed**` 咬出的两处**都是本批自己引入的**
   —— ①新写的 `error_alert.py` 自带 2 处 `except: pass` 撞 8/31 的 `except:pass` 棘轮 (955→956),
   即"一个专门为让失败可见而生的脚本自己吞错", 修代码不调基线 (计数回落 954);
   ②`test_no_archive_imports` 的 `scripts/archive/` **字面量**探针咬到散文 (退役证据注释 / 运维回滚指引 /
   测试夹具路径) ⇒ 加**双向**豁免名单 (未登记命中红 + 陈旧豁免也红) 且真 import 形态永不豁免。
   另独立复核新发现 1 处**存量** (非本批引入, 五档日志实证 ≥2 周): `pangan_daily_rebuild.py::step0` 只读
   子进程 `r.stdout`, 而 print→logger 后摘要走 **stderr**、futu SDK 噪声走 stdout ⇒ `[step0]` 行长期显示
   `on_disconnect` 而非 `完成: 刷新 0 | 已最新跳过 280 | 失败 1`, 日K失败票名单从未可见 ⇒ 教训 §8.9
   (改输出面必须普查**流归属**, 不只格式)。收尾批再加钉子 **7 例** (5 新文件 + 2 守卫双向) ⇒ 本批合计 **74 例**。
9. **收尾批又更正了本批自己的一处过头声称 (§7.6 更正框 / §8.11 / §9.1④)**: W4~W6 (以及既有 W1~W3) 的违反
   **不会推钉钉** —— `daily_invariant_check.main()` 每次运行只发 **1 条** `logger.error` 汇总 (`rec()` 只累加
   Counter 不逐条打), 而 `error_alert.py` 阈值是 `max(3, ⌈3M⌉)` ⇒ `1 ≥ 3` 为假, 结构不可达。dry-run 实测
   (真 cron 时刻 05:15, 真基线 M=0.5/T=3): 注入 1 条不推、2 条不推、3 条才推。**覆盖面那一跳是真修好了**
   (`per_source` 实测到 `invariant_check.log`), 断的是"阈值 vs 单条发射器"这一跳; 旧 `.sh` 是双重不可达
   (阈值 20 + 根本不扫该文件), 故本批仍是净改善, 但 §7.6 原文写成"→ 钉钉"属声称过头, 已显式更正不静默改写。
   根因是 §4.3 的探针用了 3 条 = 恰好等于地板, 没测被守卫者的真实发射口径 ⇒ 修法四选见 §9.1④ (推荐"必推白名单",
   属告警判据变更须用户裁, **不擅自改阈值**)。

---

## 1. 议题一 — 盘后电池每晚 32 轮空转 (①, A 案已落地 → §7.3; 判据 §1.5 待复算)

### 1.1 证据链 (可复现)

```bash
cd /opt/ftseek/FTSeek-US
# (a) 32 轮的时间戳序列 — 间隔恒定 ~7m44s, 覆盖北京 04:01→07:59 = ET 16:01→19:59 盘后窗口
grep -E '^\S+ \S+ \[.*\] INFO: \[簇浓度影子\] 已重放' logs/ftseek_us.log | awk '{print $1,$2}'
# (b) 28 轮零产出的收尾行
grep -c '^完成: 刷新 0' logs/ftseek_us.log            # 改前口径; ②落地后为 "] 完成: 刷新 0"
grep -E '^完成: 刷新' logs/ftseek_us.log | sort | uniq -c
# (c) 单轮耗时归属: 簇浓度影子结束 → 下一轮开始, 中间只有 refresh_daily_bars 的输出
sed -n '143,460p' logs/ftseek_us.log | grep -E '^[0-9]{4}-|^扫描|^完成'
# (d) 配额告警的小时分布与 refresh 运行的小时分布完全重合 (04–07 时)
zcat -f logs/ftseek_us.log-20260910 logs/ftseek_us.log | grep '配额窗口满' | awk '{print substr($2,1,2)}' | sort | uniq -c
```

| 观测量 | 实测值 |
|---|---|
| 盘后电池轮数/夜 | **32** (04:01:41 → 07:59:26, 间隔 ~7m44s) |
| 单轮 `refresh_daily_bars` 耗时 | ~7m45s (04:01:41,788 → 04:09:28,483 之间无其他日志) |
| 算式核对 | 281 票 × (1.2s 睡眠 + ~0.45s 请求) ≈ 464s ≈ 7m44s ✅ |
| 零产出轮次 | 28 轮 `刷新 0 \| 已最新跳过 280 \| 失败 1` |
| 一晚 Futu 历史K线请求 | ~32 × 281 = **8992** 次, 有效 ~281 次 ⇒ **32× 浪费** |
| 盘后窗口 `K线配额窗口满` | 近两日 13 次, **100% 落在 04–07 时** |
| 持续失败标的 | `US.SKYT` 31 夜连续 `ret=-1`, 原实现丢弃错误正文 (§7 已修) |

### 1.2 根因 (两条, 相互独立)

**R1 — `refresh_daily_bars` 的「幂等」在 API 调用之后**
`scripts/refresh_daily_bars.py:60` `start = last  # 含末日重拉, 防截断` ⇒ 每轮每票都真发
`request_history_kline`; `:80` `time.sleep(1.2)` 无条件执行; `added == 0 and out[-1][0] == last`
的跳过判定在**请求返回之后**。故「已最新」不等于「零拉取」。

**R2 — AFTER_HOURS 段只有数据级幂等, 没有执行级日门**
`app/core/scheduler_core.py::_llm_strategy_loop` 每轮迭代重跑整个 phase 块。块内 12 个
in-process 钩子的配置门普查:

| 钩子 | 配置门 | 「今日已跑」门 |
|---|---|---|
| futu_channel_shadow_collect | ✅ 5 键 | ❌ (内部按日幂等) |
| stall_entry_shadow_tag / entry_sync_shadow_tag / stale_anchor_shadow_tag | ✅ | ❌ |
| ah_exit_shadow / conflict_shadow_tag / cluster_concentration_shadow | ✅ | ❌ |
| short_funnel_shadow_tag / gap_semantics_shadow_tag / band_vol_shadow_tag | ✅ | ❌ |
| refresh_daily_bars | ✅ (scheduler 侧 `:2564`) | ❌ |
| **settle_overnight_shadow** | **❌ 无** | ❌ |

对照: 同块的 IC 回填**有**执行级日门 —— 日志 `IC backfill (幂等路径): 385 条 (今日首次, 后续轮跳过)`。
即本仓已有正确范式, 只是没推广到影子电池。

### 1.3 连带损害 (为什么不只是"浪费 CPU")

`refresh_daily_bars` 自建 `OpenQuoteContext`(`:45`), **绕过 `ftseek_lv3_provider` 的
60次/30s 限速窗口**, 与同轮的 `PlaybookIC` 前瞻收益回填抢同一份 OpenD 历史K线配额 ⇒
`[FutuLV3·DailyK] K线配额窗口满 等待超时, 返回 None 触发 Router fallback` ⇒ Router 退到
TwelveData/AlphaVantage ⇒ `Read timed out` (ERROR) ⇒ `所有数据源均失败`。
实测 9/10 04:01:21 `US.GPN` 正是这条链。

**这与该脚本的立项目的直接冲突**: `4f6a2b4e` 引入它是为了防「`replay_bars_daily` 停更三周
→ A2 报告 35% 行 gap 被污染」。若配额被自己烧穿导致 fallback 全失败, 复发的是同一个病。

### 1.4 方案

| 案 | 内容 | 影响面 | 回滚 |
|---|---|---|---|
| **A (推荐)** | `refresh_daily_bars.run_check` 加当日执行门: 状态文件 `data/refresh_daily_bars_state.json` 记 `{date, ok, skip, fail}`; **仅当 `fail == 0` 才写标记**, `fail > 0` 则下轮重试 | 1 文件 | 删状态文件 + 重启 |
| B | 整个 AFTER_HOURS 影子电池加 `last_afterhours_battery_run` 门 (同 `last_afterhours_run` 的持久化形态, `_persist_ah`) | 12 钩子 | 配置键 + 重启 |
| C | A + B | — | — |

**A 的必要语义 (不可省)**: 「失败不写标记」。否则把 32 次重试变成 0 次重试 —— 一旦盘后首轮
遇 OpenD 抖动, 当天就彻底不刷新, 反而**加速**复发 7/24 那种停更。当前 `US.SKYT` 之类永久失败票
会让标记永远写不上 ⇒ 需同时定义「连续 N 轮同一票失败即视为已知病灶、写标记 + 单条 WARNING」,
N 建议 3。

**B 的取舍**: A 只解决 7m45s 的大头; `cluster_concentration_shadow` 重放 27 交易日 × 32 轮、
`settle_overnight_shadow` 重算 74 笔全表 × 32 轮仍在。B 一次解决全部, 但改动面 12 钩子,
且会让「盘后补数据 → 下一轮立刻重算」的隐性自愈消失 (例: `position_path_collector` 盘后
晚落 bar, 原设计靠后续轮次补算 —— `settle_overnight_shadow` 注释明写「缺则留待下次」)。
⇒ **B 必须给每个钩子保留"上次未成功则重试"语义**, 不能简单一日一次。

### 1.5 预注册验收判据 (改前登记, 改后同口径复算)

改后连续 **2 个交易日**, 全部满足才算通过, 任一不满足即回滚:

1. `grep -c '日K刷新] 扫描' logs/ftseek_us.log-<D>` == **1** (改前 32)
2. `grep -c '簇浓度影子] 已重放' logs/ftseek_us.log-<D>` == **1** (改前 32, 仅 B/C 案适用; A 案此项不变)
3. 北京 04–08 时 `配额窗口满` 计数 ≤ **2** (改前 13/两日)
4. `data/replay_bars_daily/*.json` 末日覆盖率 **不下降**: 改前改后各取一次
   `末日期 == 最近交易日` 的票数, 后者 ≥ 前者
5. `US.SKYT` 类永久失败票: 每日 WARNING ≤ **3** 条 (不是 0, 也不是 31)

判据 4 是**主判据** —— 本项的唯一目的是省配额, 若省配额换来缓存覆盖率下降, 即为本末倒置。

---

## 2. 议题二 — `settle_overnight_shadow` 死开关 + write-only 链 (①, A 案第②步配置停已落地 → §7.3; 判据 §2.4 待复算)

### 2.1 证据链

```bash
cd /opt/ftseek/FTSeek-US
grep -n 'overnight_hold_shadow_enabled' config/unified_config.yaml        # :1449 = false
grep -n 'status=ShadowStatus' -A0 -B8 app/playbook/shadow_registry.py | grep -A8 'overnight_hold'  # GRADUATED
rg -l 'overnight_hold_shadow_enabled' --type py    # 只有 overnight_hold_shadow.py + shadow_registry.py
rg -n 'cf_fullday_from_exit_pnl|fullday_from_exit' --glob '!docs/**' --glob '!logs/**' .
                                                     # 只有 settle 脚本自己 + tests/test_overnight_shadow.py
grep -n 'overnight_hold_shadow' scripts/overnight_arm_report.py            # 0 命中
```

| 环节 | 状态 |
|---|---|
| 写入方 `app/playbook/overnight_hold_shadow.py` (append `settled: False`) | 配置门 **false** ⇒ 当前不写 |
| 结算器 `scripts/settle_overnight_shadow.py` (读未结算行 → 写 `settled/cf_*` + `_latest.json`) | **无任何门**, 32 轮/夜重算重印 |
| `data/overnight_hold_shadow_latest.json` 消费方 | **0** (只有写入方自己 + config 里提及) |
| 结算字段 `cf_fullday_from_exit_pnl` / `fullday_from_exit` 的 live 消费方 | **0** (只有 `tests/test_overnight_shadow.py`) |
| 注册表登记的 eval_script `scripts/overnight_arm_report.py` (cron `20 6 * * 2-6`) | 读 **DB** `trade_records.metadata_json.overnight_arm`, 不读该 jsonl |
| 注册表 `config_key` | `playbook.overnight_hold_shadow_enabled` —— 与结算器挂载点**不对应** |

### 2.2 挂载核对哨兵为何漏

哨兵核的是「eval 是否挂载」+「cron 面」(注册表 `_ETC_CRONTAB`/`_ETC_CRON_D` 常量, 2026-09-01 收口)。
本例是**反向**形态: eval 挂着、数据在写、开关是 false —— 哨兵没有「`config_key=false` 但源码仍
无条件调用」这条判据。注册表 `:137` 自述「源码文本碰巧豁免挂载核对」正是这个缺口。

### 2.3 方案 (需用户裁, 因涉及退役)

| 案 | 内容 |
|---|---|
| **A (推荐)** | 按 AGENT.md 减法三步退役结算器: ①零消费已证 (§2.1) → ②`app/core/scheduler_core.py:2458` 挂载点加配置门 `playbook.overnight_hold_settler_enabled`(新键, 默认 **false**, 与写入方开关联动) → ③观察一周后代码退役 (脚本移 `scripts/archive/`, 测试同步) |
| B | 只补门不退役: 结算器补读 `overnight_hold_shadow_enabled`, 与写入方同开关联动; 注册表 `config_key` 补登记结算器挂载面 |
| C | 保留 32 轮重算, 仅把报告块降噪 (②已做: 17 行 print → 1 条原子 INFO record) |

**A 的前置核对 (执行方已做, 五面零引用)**: 仓内 import ✅(只有 scheduler 挂载点) / crontab ✅0 /
systemd ✅0 / Hermes prompt ✅0 / `.git/hooks` ✅0。`tests/test_overnight_shadow.py` 需随退役同步处理。

**B 的风险**: 写入方 false + 结算器 false ⇒ 74 笔历史反事实**冻结**, 若日后重启写入方,
历史行仍是 `settled: False` 会被一次性补算 (可接受, 结算器本就是幂等全量重算)。

### 2.4 预注册验收判据

1. 改后 `grep -c '隔夜持仓反事实' logs/ftseek_us.log-<D>` == **0** (A/B 案) 或 == **1** (C 案 + 议题一 B 案)
2. `data/overnight_hold_shadow_latest.json` 的 mtime 停止推进 (A/B 案)
3. `tests/test_overnight_shadow.py` 全绿 (B/C 案) 或随退役移除并在 DEB 记录 (A 案)
4. 注册表 `overnight_hold` 条目的 `config_key` 与实际挂载面一致 (哨兵可核)

---

## 3. 议题三 — WARNING 级别通胀 (③, 两处降级已落地 → §7.4; 判据 §3.4 待复算)

### 3.1 基线 (改前 7 日, 2026-09-04 → 09-10, 供改后同口径复算)

```bash
for f in logs/ftseek_us.log-2026090[4-9].gz logs/ftseek_us.log-20260910; do zcat -f "$f"; done \
 | grep '] WARNING:' | sed -E 's/^[0-9-]+ [0-9:,]+ \[([^]]*)\].*/\1/' | sort | uniq -c | sort -rn
```

| logger 家族 | 7日 WARNING | 占比 | 性质 |
|---|---|---|---|
| app.playbook.pipeline | 538 | 31.3% | 混合: chase_cap(DEB-20260903 **刻意不去重**) + 极端gap跳过 |
| **app.positions.reconciler** | **359** | **20.9%** | **全部是启动对账成功摘要横幅 (16 行 × 8 次重启 × ~3 日)** |
| app.data.finnhub_provider | 245 | 14.2% | 真限速 (429) |
| ftseek_us.futu_lv3 | 226 | 13.1% | 真配额窗口满 |
| **ftseek_us.config_audit** | **120** | **7.0%** | **全部自带 `[INFO]` 标签的非阻断项** |
| app.core.scheduler_core | 100 | 5.8% | config-diff 已知设计差异 |
| 其余 11 族 | 133 | 7.7% | — |
| **合计** | **1721** | | ERROR+CRITICAL 同期 **12** ⇒ **143:1** |

### 3.2 两处级别误用 (代码锚点)

**(a) `app/positions/reconciler.py:549-582`** — 对账**成功**摘要整段打 WARNING:
```python
logger.warning("=" * 70)
logger.warning("[Reconciler] 启动对账完成")
logger.warning(f"  Broker 持仓: {...} | 本地内存: {...}")
...
logger.warning("=" * 70)
```
16 行/次启动。昨日 8 次重启 ⇒ 128 条伪 WARNING。真正的异常分支 (`:147` `启动对账异常 (降级运行)`,
`:223`/`:252` 单标的补录失败) 也打 WARNING ⇒ **成功与失败同级, WARNING 失去区分度**。

**(b) `app/core/config_audit.py:125` + `:197-199`** — 自带 `[INFO]` 标签的项用 `logger.warning` 输出:
```python
f"⚠️ [INFO] YAML 缺少键 {yaml_key} "      # :125  消息自称 INFO
...
logger.warning(f"⚠️ 配置一致性审计发现 {len(non_blocking)} 个警告")   # :197
    logger.warning(f"  {w}")                                          # :199
```
且紧接着 `main.py` 打 `[ConfigAudit] ✅ SSOT 配置一致性审计通过 — 无漂移检测` (INFO) ⇒
**相邻两行自相矛盾**, 每次重启都出现, 昨日 8 次。

### 3.3 方案

| 项 | 改法 |
|---|---|
| (a) | `:549-582` 成功摘要整段 → `logger.info`; 保留 `"="*70` 横幅(或删, 见下)。异常分支 (`:147`/`:223`/`:252`) **保持 WARNING 不动** |
| (b) | `:197-199` 的 `non_blocking` 项 → `logger.info`; 只有 blocking/CRITICAL 项才 `logger.warning`。`:125` 的 `⚠️ [INFO]` 前缀与级别对齐后不再自相矛盾 |
| 附带 | `logger.warning("=" * 70)` 这类纯分隔符行: 降级后仍是噪音, 建议改多行单 record (同 §7 报告块做法), 但**不强制** |

**不动的**: `app.playbook.pipeline` 的 chase_cap 双行 WARNING —— DEB-20260903 议题一
用户已裁「warning 移出去重门」(9/2 DELL 二次更糟违例被去重门吞没的教训)。本 DEB 不重开该裁决。

### 3.4 预注册验收判据

改后连续 2 个交易日, 同 §3.1 命令复算:

1. `app.positions.reconciler` 族 WARNING == **0** (改前 359/7日)
2. `ftseek_us.config_audit` 族 WARNING == **0** (改前 120/7日)
3. **其余 15 族逐族 WARNING 计数不减少** (±5% 容差) —— 防止"降噪"实际是把真告警一起降了
4. `daily_alert_summary` 推送正文的警告组数下降幅度 ≈ 27.8% (§3.1 两族占比), 不得超过 35%
5. `intraday_heartbeat` 的 `] WARNING:` 计数同步下降, 且其静默失败检测**未**转哑
   (判据: 人为制造一次 reconciler 单标的补录失败, 必须仍出 WARNING)

判据 3 与 5 是**防"守卫只能报健康"**的主判据 (同源教训: `ops/hermes/ftseek_us_restart_20260829_hotfix.sh:64`
首版写 `'/ ERROR /'` 而日志形态是 `"] ERROR:"` ⇒ 恒报 0 错)。

---

## 4. 议题四 — `scripts/error_alert.sh` 恒不触发 + 6 处缺陷 (③, A 案已落地 → §7.5; §4.3 四判据已实机验证)

### 4.1 证据

```bash
sed -n '9p;20,24p;29,31p;52,58p' scripts/error_alert.sh
crontab -l | grep error_alert          # */15 21-23 * * 1-5  +  */15 0-5 * * 2-6, 均 2>/dev/null
for f in logs/ftseek_us.log-2026090[4-9].gz logs/ftseek_us.log-20260910; do
  echo "$f: $(zcat -f $f | grep -cE '\] (ERROR|CRITICAL):')"; done    # 5 1 2 0 0 2 2
cat data/.alert_state 2>/dev/null      # 无 error_spike_* 记录 ⇒ 从未触发
```

| # | 缺陷 | 后果 |
|---|---|---|
| 1 | `THRESHOLD=20` 条/15min, 实测 **0–5 条/日** | **恒不触发**, 近 7 日 0 告警。它是 AGENT.md 认定的两道 ERROR 覆盖之一(另一道 `intraday_heartbeat`), 实际是纸面的 |
| 2 | `$0 ~ /ERROR\|CRITICAL/` 级别盲 —— 匹配行内**任意位置**的字符串 | INFO/WARNING 正文含 "ERROR" 即计入。近两日实测误报 0 条, 但这是运气不是设计 (`daily_alert_summary.py:185` 同病: `"异常" in line` 即判 ERROR) |
| 3 | `$1 " " $2 >= cutoff` 对**无时间戳行**做字符串比较 | ②落地前主日志 60.9% 是无时间戳 print 行; 中文首字节 > ASCII 数字 ⇒ 恒判"最近"。②已消噪声源, 但判据本身仍脆 |
| 4 | 告警正文 shell 拼 JSON: `-d "{\"msgtype\":...\"content\":\"$MSG\"}}"`, `$MSG` 含 `TOP_ERRORS` 原文 | 错误消息里出现 `"` 或反斜杠 ⇒ **非法 JSON** ⇒ `curl` 失败 ⇒ `\|\| true` 静默吞掉。恰好在错误最异常时哑火 |
| 5 | 冷却键 `error_spike_$(date +%Y%m%d%H)` 是**全局小时级** | 与注释「同一类错误 1 小时内只告警一次」不符: 一小时内第二类错误不再告警 |
| 6 | 直连 DingTalk webhook (`curl` + 从 `.env.us` grep URL) | 绕开 `app/push/` hub, 是 AGENT.md 军规点名的旁路形态(存量 5 个之一), 无去重/聚合/健康监控 |

### 4.2 方案

**A (推荐)** — 重写为 Python 并接 `app/push/` hub:
- 级别判定改字面量 `"] ERROR:"` / `"] CRITICAL:"` (与 `intraday_heartbeat.py:244` 同口径, 单一真源)
- 时间窗判定改 `line[:16]` 解析 (与 `daily_alert_summary` 同口径), 无时间戳行**显式跳过并计数**
- 阈值改**相对基线**: 取近 7 日同时段 ERROR 计数的中位数 M, 触发条件 `count >= max(3, 3×M)`;
  M==0 时退化为 `count >= 3`。硬编码 20 在 0–5/日 的现实下等于关闭
- JSON 用 `json.dumps`, 不用 shell 拼接
- 冷却键改 `error_spike_<日期>_<小时>_<错误指纹>` (指纹 = 去掉数字后的消息前 60 字), 兑现注释语义
- 推送走 `app/push/` hub

**B** — 最小修: 只改阈值 (20 → 3) + JSON 用 `jq`/python 生成, 其余不动, 保留 shell 旁路形态。

**C** — 承认冗余并退役: `intraday_heartbeat` 已做静默失败检测 + 推送, `daily_alert_summary`
已做盘后告警汇总; 若三者覆盖面经核重合, 按减法三步退役 `error_alert.sh` + 其 2 条 cron。
**前置**: 须先核 `intraday_heartbeat` 是否覆盖非盘中时段 (cron 面 `*/30 21-23,0-5`, 与 error_alert 同窗)。

### 4.3 预注册验收判据

1. **可触发性证明** (主判据): 向 `logs/ftseek_us.log` 注入 3 条形如
   `<当前时间戳> [MainThread] [test.probe] ERROR: 探针` 的行, 下一个 cron 周期内必须收到推送。
   改前同法注入 20 条以下**收不到** —— 这条判据改前改后各跑一次, 形成对照。
2. **误报证明**: 注入 20 条正文含 "ERROR" 但级别为 INFO 的行 ⇒ 不得触发。
3. **JSON 健壮性**: 注入 1 条消息含 `"` 与 `\n` 的 ERROR ⇒ 推送必须到达且正文可读。
4. 探针行必须在验证后清除, 且清除动作记入本 DEB (不得留假错误污染基线)。

---

## 5. 议题五 — logger 命名 4 套并存 + 名实不符 (③, 棘轮已立 → §7.6 W5; 改名留下一批)

```bash
cat logs/ftseek_us.log logs/ftseek_us.log-20260910 \
 | grep -oE '^\S+ \S+ \[[^]]+\]' | sed -E 's/^\S+ \S+ //' \
 | sed -E 's/\[(app|scripts|ftseek_us|ftseek)\..*/[\1.*]/' | sort | uniq -c | sort -rn
```

| 命名族 | 近两日行数 | 例 |
|---|---|---|
| `app.*` | 3112 | `app.playbook.pipeline` |
| `ftseek_us.*` | 1548 | `ftseek_us.main`, `ftseek_us.futu_lv3`, `ftseek_us.config_audit` |
| `ftseek.*` | 670 | `ftseek.daily_active_pool`, `ftseek.ah_exit_shadow` |
| 裸脚本名 | 94 | `cluster_concentration_shadow`(②已并入 `ftseek.*`), `exit_policy_guardian` |
| `scripts.*` | 29 | `scripts.api_limit_monitor` |
| `config.*` | 10 | `config.config_loader` |

后果: `logging.getLogger("app").setLevel(...)` 之类按子系统设级/过滤覆盖不到 `ftseek.*`/`ftseek_us.*`;
`error_alert.sh`/`daily_alert_summary` 只能退化为全行子串匹配 (§4 缺陷 2 的成因之一)。

**名实不符**: `app/data/alphavantage_provider.py:568` 打的是 `[Finnhub] VIX (VIXY代理)`
(29 次/日) —— 模块名与消息内容指向不同数据源, 排查时会找错文件。

**方案**: 定 SSOT 约定 (建议 `ftseek.<subsystem>.<module>`), 分批迁; **先加棘轮**——
照 `scripts/daily_invariant_check.py::_check_field_cardinality` 的 W2 形态, 新增一条
「禁止新增裸名 logger」的不变量自检, 基线登记现存 94 行, 只降不升。
**前置核对**: 迁移前须普查文档/脚本/Hermes prompt 里对 logger 名的字面引用 (grep 面),
否则会打断既有排查口径 —— 这是本议题列为低优先的原因。

---

## 6. 议题六 — 两个死审计组件 (已按减法三步退役 → §7.7; 第②步在本例为空操作, 理由见该节)

| 组件 | 状态 | 证据 |
|---|---|---|
| `app/pipeline/observers.py:201` `AuditLogger` | 自称「全链路审计 — 记录每次决策→执行→结果的完整链路」, `subscribe("*")`, 但只写 `logger.debug`(`:209`) ⇒ INFO 级下**零输出**; 且 `setup_observers`(`:217`) **零调用方** ⇒ 从未注册 | `rg -n setup_observers --type py` 只有定义处 |
| `app/observability/audit_logger.py` `AuditLogger` | **零消费者**; 若被启用会**双写**(`FileHandler("logs/audit.log")` + `propagate` 未关 ⇒ 同一 record 再进 root→stdout→主日志); `retention_days=90` 的 `_cleanup_expired`(`:89`) 只过滤**内存 list**, 不删盘上文件 ⇒ 参数给出虚假的有界保留承诺 | `rg -n 'AuditLogger\|audit_logger' --type py` 只有 `observers.py` 的同名不同类; `ls logs/audit.log` 不存在 |

**方案**: 按 AGENT.md 减法三步退役 (证零消费 → 配置停 → 观察一周后代码退役)。
执行方已做五面零引用核对的**仓内面**; 退役前须补 crontab / systemd / Hermes prompt /
`.git/hooks` 四面 (军规: `check_undefined_names.py` 被 pre-commit 引用却在减法 R2 当零引用归档,
门禁静默失效 ~40 天)。

**注**: `logs/audit/` 目录与本节两个类**无关** —— 该目录由 `scripts/post_market_audit.py:48`
(FileHandler) 与 `scripts/pre_market_check.py:379` 写, ④ 已补 logrotate 覆盖。

---

## 7. 已落地回执

> §7.1–§7.2 = ②④ 零行为变更 (2026-09-10 上午, 用户裁「按照建议执行」);
> §7.3–§7.7 = §9 七项推荐案 (2026-09-10 午间, 用户裁「按照建议执行优化」)。
> 本节只登记**做了什么 + 怎么验的**, 不改 §1.5/§2.4/§3.4/§4.3 的预注册判据 —— 判据改后复算。

### 7.1 ② 可观测性 (7 文件)

| 改动 | 锚点 | 验证 |
|---|---|---|
| 主日志格式加 `%(threadName)s` | `main.py:37-52` | **6 个下游解析面逐个实测通过**: `intraday_heartbeat` 字面量 `"] ERROR:"`/`"] WARNING:"` + `line[:16]`; `daily_alert_summary` `line[:19]` strptime; `startup_smoke_check` `^asctime` re.match; `insider_signal_monitor` 正则; `error_alert.sh` awk `$1" "$2`; `pre_market_check` 子串判级。**契约写进代码注释**, 含「threadName 只能插在 asctime 与 `[name]` 之间, 否则静默失败检测器恒报 0」 |
| 唯一未命名线程补名 `PositionSync` | `app/core/position_sync.py:223` | 全仓 AST 级扫描: 42 个 `Thread(` 中 41 个已带 `name=`, 只此 1 个漏网 (先前用单行 regex 粗估为 27 个未命名, **实测纠正**) |
| print → logger (7 个 in-process 脚本) | `refresh_daily_bars` / `settle_overnight_shadow` / `cluster_concentration_shadow` / `short_funnel_shadow_tag` / `conflict_shadow_tag` / `gap_semantics_shadow_tag` / `band_vol_shadow_tag` | 改前实测 print 噪声占主日志 **60.9%** (今日 1396/2291 行)、昨日 26.0%、9/3–9/6 达 32–37%。命名统一到 in-process 既有约定 `ftseek.<name>`; 消息正文逐字不变以免打断既有 grep。**多行报告块合成单条 record** ⇒ 由 handler 锁保证原子, 不再被其他线程输出穿插。每个脚本的 `__main__` 补 `if not logging.getLogger().handlers: basicConfig(...)` 守卫 —— 否则 print→logger 后**独立 CLI 运行输出会静默变空** (7 个脚本 import + 传播 + 守卫三项逐个实测通过) |
| `refresh_daily_bars` 错误正文不再丢弃 | `scripts/refresh_daily_bars.py:70-88` | 原 `print(f"{sym}: 拉取失败 (ret={ret})")` 把 futu 在 `ret != RET_OK` 时放在 `data` 的错误正文整条丢弃 ⇒ `US.SKYT` 连续 31 夜 `ret=-1` 无从根因。现 WARNING 带正文。另补两处**原先完全静默**的失败计数 (缓存 JSON 解析失败 / 缓存为空) |
| scheduler 23 处隔离异常收口 | `app/core/scheduler_core.py` `_llm_strategy_loop` | 19 处 `logger.warning(...异常 (已隔离)...)` 补 `exc_info=True`; 4 处 `logger.debug` → `warning` + `exc_info` (`:2215`/`:2225` scan 时间持久化/载入、`:2350` 节假日检查 fail-open、`:2461` 隔夜shadow结算)。**全仓口径**: 4302 个 `except` 块只有 58 处带 `exc_info` ⇒ 98.7% 的异常路径只留 `str(e)`, 无栈无行号; 改前两日主日志 **0 条 Traceback**。`:2461` 与 `:2452` 的 2026-07-04 修复相隔 **11 行**却漏改 —— 逐案修而不立规则的代价, 见 §8 方法教训 |

回归验证: `tests/test_daily_alert_summary_log_sources.py` + `tests/test_daily_alert_equity_recon.py`
**32 passed**; `tests/observability` **15 passed**; `tests/test_overnight_shadow.py` +
`tests/coordinator/test_position_sync_p0_fixes.py` + `tests/test_watchdog_long_round_heartbeat.py` +
`tests/test_premarket_overrun_cancel.py` **43 passed**。近两日 `[LLM策略]...异常 (已隔离)` 实测
**0 条** ⇒ 补 `exc_info` 不会给 `startup_smoke_check` 的「零 Traceback」断言引入新失败面。

### 7.2 ④ logrotate + `logs/` 清理

| 改动 | 内容 | 验证 |
|---|---|---|
| `rotate 14 → 45` | 原 14 天短于 OKR-2026-09 验证期 (至 09-30), 跨期复盘缺料。日增实测 ~1.2MB (gzip 后 ~0.15MB), 45 天 ≈ 55MB | `logrotate -d` 0 error |
| 新增 `logs/audit/*.log` 段 | 原 `logs/*.log` 是**非递归** glob ⇒ `logs/audit/post_market_audit.log` 自 6/29 起无轮转无上限 | `logrotate -d` 实测 `considering log .../logs/audit/post_market_audit.log`; 零消费者核对通过 (只有写入方 `scripts/post_market_audit.py:48`) |
| **刻意不加 `maxsize`** (撤回原建议) | `dateext + dateformat -%Y%m%d` 下同一天第二次轮转**撞名** ⇒ 该档跳过、之后彻底停轮转; 且 `daily_alert_summary.resolve_log_sources` 按 `ftseek_us.log-YYYYMMDD` 日期升序拼档 (`docs/CONTROL-LOOP-LEDGER.md` §162, 2026-09-09 修), 一天多档会破坏跨档去重与会话窗拼接。日增 1.2MB 无跑飞风险 ⇒ 收益不抵风险 | 已写入 `/etc/logrotate.d/ftseek-us` 注释 |
| `logs/` 顶层 **1107 → 915** | 删 27 个「0 字节且 >7 天未更新」`.log`(`copytruncate`+`notifempty` 下永不清除); 由此成为孤儿的轮转档 + 原有 19 个孤儿共 **161** 个移入 `logs/_archive_20260910/`(**含数据未删**); 4 个 7 月 systemd 迁移期手工快照 `ftseek_us.log.pre_*` 一并移入 | 20 个涉及家族**四面零引用**(仓内 / `crontab -l` / `/root/.hermes/cron/jobs.json` / `.git/hooks`) 且零活跃 cron ⇒ 确认退役非安静; **`ftseek_us.log-YYYYMMDD` 全 14 档刻意留在顶层**(移动即给 `resolve_log_sources` 制造盲区); 归档目录带 `README.md` + 两份清单 + 一行还原命令; 清理后 `logrotate -d` 0 error, considering 116→89 |

备份: `/etc/logrotate.d/ftseek-us.bak.20260910`。
**生效条件**: ② 的 `.py` 改动须 `systemctl restart ftseek-us` 才生效 (热重载器只监控
`config/unified_config.yaml` 配置值, 不重载代码); 重启后必跑
`PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` 七条断言。
④ 的 logrotate 改动由 `/etc/cron.daily/logrotate` 下次运行自动生效, 无需重启。

### 7.3 议题一 A 案 + 议题二 A 案第②步 (§9 #1 #2)

| 改动 | 锚点 | 验证 |
|---|---|---|
| `refresh_daily_bars` 执行级日门 | `scripts/refresh_daily_bars.py` (新增 `STATE`/`ET`/`MAX_ATTEMPTS_ON_FAIL=3`; `run_check` 当日门; `do_refresh(limit, dry_run, only_syms)` 返回 `{ok,skip,fail,failed_syms}`; `main()` 加 `--ignore-gate`) | `tests/test_refresh_daily_bars_gate.py` **7 passed** |
| 失败**有限**重试语义 (§1.4「A 的必要语义」) | 同上 | 三条不可退让语义各有用例钉住: ①干净夜 31 轮只跑 1 次且首轮全量; ②失败夜**只重试失败票** (`only_syms={"US.SKYT"}`, 成本 ~1.65s/票 而非 281 票全量重扫), 连续 3 轮封顶后写 done + 单条 WARNING 带名单 (§1.5 判据 5 的 ≤3 条/日); ③跨日重新全量 + 状态文件损坏 **fail-open 向"做刷新"方向** + 状态写失败不阻断 (用例**真实**触发写失败而非替换 `_save_state` —— 替换被测函数等于把保护一起拆了, 首版此用例即犯此错) |
| 逐票失败行 WARNING → DEBUG | 同上 | 汇总行仍带失败名单 (判据 5 不丢信号), 逐票行降 DEBUG ⇒ 不把刚清掉的噪声换个形态请回来 |
| 议题二: 新键 `playbook.overnight_hold_settler_enabled: false` | `config/unified_config.yaml:1450` | yaml 可解析; `scripts/config_key_census.py` 判 **referenced** (非 orphan/dead) ⇒ 下轮普查不会把新键当死键清掉 |
| 结算器补配置门 (**fail-closed**) | `scripts/settle_overnight_shadow.py::run_check` | `tests/test_settle_overnight_settler_gate.py` **7 passed**: 默认 false → 31 轮 `_settle_all` **零调用**; 键缺失同样停 (防"删键即复活"); true → 原样返回结算笔数 (回滚 = yaml 一行 + 重启); 配置读异常 → **跳过 + WARNING** (与另 10 个钩子的 `except: pass` fail-open 刻意相反, 理由写进 docstring: 本门默认 false = 本就不该跑, fail-open 会让一次 ConfigContainer 抖动把已停作业复活; 且结算是全量幂等重算, 漏一夜下夜自动补齐); 结算异常仍隔离 + 带栈 |
| 注册表补登记**两个挂载面** | `app/playbook/shadow_registry.py` `overnight_hold.notes` | §2.4 判据 4 机器化: 用例断言 notes 含 `settle_overnight_shadow` / `overnight_hold_settler_enabled` / `_load_script_module` 三锚点, 且 `config_key` 仍是写入方那个键 (两键联动但不同名) |

**减法三步只做到第②步**: 结算器脚本与 `tests/test_overnight_shadow.py` 本批**不删** ——
第③步代码退役须先观察一周 (§2.3 A 案原文), 74 笔历史反事实 jsonl 亦保留 (§2.3 B 案风险条:
若日后重启写入方, 历史 `settled: False` 行会被一次性补算 —— 结算器本就是幂等全量重算, 可接受)。

### 7.4 议题三 两处 WARNING 降级 (§9 #3)

| 改动 | 锚点 | 验证 |
|---|---|---|
| (a) 对账**成功**摘要 16 行 WARNING → **单条 INFO record** | `app/positions/reconciler.py::_log_summary` | `tests/test_warning_level_discipline.py` **9 passed** |
| (b) `non_blocking` 项 WARNING → **单条 INFO record**; blocking 仍 `logger.critical` | `app/core/config_audit.py:196-207` | 同上 |
| 附带项 (§9 议题 3 的待裁点) **已执行** | 两处 `"=" * 70` 纯分隔符行随单 record 化消失 | 用例 `test_separator_lines_gone` 钉住 |
| **异常分支一律不动** | `reconciler.py:147` (启动对账异常) / `:223` (broker_only 补录失败) / `:252` (local_only 标记失败) | §3.4 判据 5 机器化: 三条用例分别人为制造 local_only 失败 / broker_only 失败 / 顶层异常, 断言**仍出 WARNING** 且顶层那条仍带 `exc_info` (降噪不得致盲) |
| 阻断语义不变 | `config_audit.print_config_audit(strict=True)` | 用例注入 BLOCKING 项: 仍 CRITICAL + 仍 raise `ConfigAuditError`; 观察模式 (`strict=False`) 仍不 raise 且"观察模式"提示仍在; 混合场景下非阻断项仍完整列出 (INFO) |
| 摘要字段一条不丢 | `_log_summary` | 用例逐项断言 `matched` / `broker_only` / `local_only` / `errors` 与 playbook 自管跳过、adopted、legacy 提示都在正文里 (降噪 ≠ 降信息) |

单 record 化的理由 (同 §7.1 报告块做法): handler 锁保证原子 ⇒ 启动期多线程 (scan/monitor/
出场快循环/心跳/对账) 共写一条 stdout 时横幅不被穿插; 且 `daily_alert_summary` 的 `line[:19]`
strptime 会跳过无时间戳续行 ⇒ §3.4 判据 4 的警告组数下降幅度与逐行降级**等价**, 不会因合并而少算。
级别与自带标签对齐后, `⚠️ [INFO]` / `⚠️ [DRIFT]` 不再名实矛盾, 也不再与紧邻的 `main.py:490`
`[ConfigAudit] ✅ … 审计通过` (INFO) 互相打脸。`result.errors > 0` 时摘要仍走 INFO 也不丢信号:
逐笔失败已在上游三个分支各出一条 WARNING。

### 7.5 议题四 A 案: `error_alert.sh` → `scripts/error_alert.py` (§9 #4)

六处缺陷 (§4.1) 逐条修复, 每条都有机器判据 (`tests/test_error_alert.py` **28 passed**):

| # | 缺陷 | 修复 | 钉住它的用例 |
|---|---|---|---|
| 1 | `THRESHOLD=20` 而实测 0–5 条/日 ⇒ 恒不触发 | 阈值 = `max(3, ⌈3×M⌉)`, M = 近 7 日**同一 15min 时段** (`hour`, `minute//15`) 的 ERROR+CRITICAL 计数中位数; M=0 退化为 3 | `test_three_errors_trigger` (3 条必推) / `test_two_errors_below_floor_do_not_trigger` / `test_threshold_is_relative_baseline` (造 7 日各 4 条 ⇒ M=4, T=12: 5 条不推、12 条推) / `test_threshold_floor_is_three_not_twenty` |
| 2 | `$0 ~ /ERROR\|CRITICAL/` 级别盲 | 字面量 `"] ERROR:"` / `"] CRITICAL:"` (`main.py:37-52` 契约, 与 `intraday_heartbeat.py:244` 同源单一真源) | `test_info_lines_mentioning_error_do_not_trigger` (20 条正文含 ERROR 的 INFO) / `test_warning_lines_do_not_trigger` (30 条 WARNING) / `test_critical_also_counts` |
| 3 | 对无时间戳行做字符串比较 ⇒ 中文首字节恒判"最近" | `line[:16]` 比较; 无时间戳行**显式跳过并计数** —— 且只在"前一条有时间戳行落在窗内"时计数, 否则读尾部 2MB 会把窗口外历史续行一起算进来 (首版实测虚高到 8577 行, 已修) | `test_untimestamped_error_lines_skipped_and_counted` / `test_multiline_record_continuation_counted_as_unparsed` |
| 4 | shell 拼 JSON ⇒ 消息含 `"` 即非法 ⇒ `curl` 失败被 `\|\| true` 吞掉 | 推送走 push hub, 序列化交给 `requests(json=…)`; 正文控制字符 → 空格 (每条错误占一行) | `test_message_with_quote_and_newline_stays_pushable` (含 `"` `\` 与**真实换行**的多行 record: 必推 + payload `json.dumps` round-trip + 续行不进正文) |
| 5 | 冷却键全局小时级 ⇒ 一小时内第二类错误被压制 | 键 = `error_spike_<YYYYMMDD>_<HH>_<指纹>`, 指纹 = 去数字后消息前 60 字; 一次运行只推**一条**聚合消息, 正文只列本轮尚未冷却的类 (避免 N 类 = N 条刷屏) | `test_cooldown_is_per_fingerprint_not_per_hour` / `test_same_class_within_hour_suppressed` / `test_next_hour_can_alert_again` / `test_fingerprint_ignores_numbers` |
| 6 | 直连 DingTalk webhook, 绕开 `app/push/` | `get_push_hub().push(PushMessage(level=HIGH, category=SYSTEM, dedup_key=…), **sync=True**)` —— cron 进程短命, 异步 fire-and-forget 可能在 worker 发出前就退出; `finally: hub.stop()` 防挂在非 daemon 线程上 | `test_push_failure_does_not_write_cooldown` (推送失败 → exit 1 + **冷却键不写** ⇒ 下轮重试, 不用"已告警"掩盖"没送到") |
| 新 | **告警链断裂**: 只读 `logs/ftseek_us.log`, 而 `daily_invariant_check.py` 写 `logs/invariant_check.log` | 扫描面 = `logs/*.log` **全部活跃档 + 当日轮转档**, 用 glob 而非登记表 (登记表正是断裂成因); 唯一排除自身的 `logs/error_alert.log` (防自扫正反馈环) | `test_invariant_check_log_is_covered` / `test_new_log_file_auto_covered` / `test_self_log_excluded_from_scan` / `test_rotated_archive_inside_window_is_scanned` (轮转 03:12–03:48 CST 正在 cron 窗内) |
| 新 | 守卫自己坏了不可见 | stdout 落 `logs/error_alert.log` (crontab 由 `2>/dev/null` 改 `>> … 2>&1`); 扫描异常 → ERROR + exit 1; 基线算不出 → 退地板阈值 3 + WARNING; 状态写失败 → WARNING 不阻断 | `test_scan_error_is_loud_and_nonzero_exit` / `test_baseline_error_falls_back_to_floor` / `test_state_write_failure_not_fatal` |
| 新 | 口径重复实现会漂移 | 轮转档命名本地同构实现 (不 import `daily_alert_summary` —— 那会执行 `load_dotenv` 并把 HK 根插进 `sys.path`, 污染 pytest 会话的模块解析), 改用**子进程**交叉比对 | `test_rotated_sources_matches_daily_alert_summary_convention` (改 logrotate `dateformat` 必须同步两处, 否则本用例红) |

**§4.3 四条判据的实机验证 (真日志 + 真推送, 不只单测)**:

| 判据 | 做法 | 实测结果 |
|---|---|---|
| 1 可触发性 (主判据) | 写 `logs/error_alert_probe.log` 3 条 `… [test.probe.error_alert] ERROR: 【探针/PROBE】…`, 跑**真 cron 命令** (真状态文件) | `11:00:29 [app.notifier.dingtalk] INFO: [钉钉] send_text 成功: ⚠️ [FTSeek-US] ERROR 突增 3 条/15min (阈值 ≥3)` + `已推送 (类 1, 冷却键 1 条已登记)`, exit 0。**改前对照**: 同法注入 3 条恒不触发 (阈值 20); `data/.alert_state` 停在 `error_spike_2026080522` = 近 7 日 0 告警 |
| 2 误报证明 | 同文件改写 20 条正文含 "ERROR"/"CRITICAL" 的 **INFO** 行, 再跑 | `ERROR/CRITICAL 0 条 < 阈值 3 (扫 158 文件)`, 钉钉推送 **0 次** |
| 3 JSON 健壮性 | 1 条含 `"` `\` 与**真实换行**的 ERROR (多行 record) + 2 条凑地板; 先 `--dry-run` 看正文再真推 | 正文可读 (每条错误占一行, 引号/反斜杠原样保留, 续行归入 `无时间戳跳过行: 1`); 真推 `send_text 成功`, `已推送 (类 3, 冷却键 4 条已登记)` |
| 4 探针清除 | 删 `logs/error_alert_probe.log`; 从 `data/.alert_state` 剪掉 4 个探针冷却键, 并在 `last_run.note` 记清除动作 | 已执行 (`probe log exists: False`, `剪掉探针冷却键: 4 / 剩余: []`)。主日志 `logs/ftseek_us.log` **全程未被注入** (探针走独立文件) ⇒ §3.1 WARNING 基线与 §1.5 复算口径零污染 |

上线态实测 (非探针): 窗口扫 **157 文件 / 0.45s**; 基线一次算 **468 个轮转档**, 按 `<日期>:<小时>`
缓存进状态文件 (`test_baseline_cache_reused_within_hour` 钉住一小时只算一次, 跨小时必重算);
近 8 日全库 ERROR+CRITICAL 合计 **20 条**, 当前时段 7 日样本全 0 ⇒ M=0, T=3。
旧 `data/.alert_state` 是纯文本单行 ⇒ 新版解析失败**自愈重建** + WARNING 留痕
(`test_legacy_plain_text_state_self_heals`); 冷却键/基线缓存只留今昨两日 (`test_cooldown_keys_pruned`)。

**部署面**: crontab 两条从 `/bin/bash …/error_alert.sh 2>/dev/null` 改为
`cd /opt/ftseek/FTSeek-US && PYTHONPATH=. …/venv/bin/python scripts/error_alert.py >> …/logs/error_alert.log 2>&1`
(时段/星期不变: `*/15 21-23 * * 1-5` + `*/15 0-5 * * 2-6`), 上方补 12 行改动理由注释;
改前备份 `data/crontab.backup.20260910_error_alert_py.txt` (251 行)。旧脚本 `git mv` →
`scripts/archive/error_alert.sh` (六面核对见 §7.7 表; crontab 面是**先改指向再归档**)。
两条用例 `test_crontab_points_at_python_version` / `test_legacy_shell_script_no_longer_mounted`
钉住"重写 = 真上线", 而不是"重写了但没人调" (W0 工具此前的处境)。

连带同步 5 处**现行口径**文档/注释里的旧名 (不改就是给下一个人错的口径): `main.py:37-47` 格式契约 /
`scripts/daily_invariant_check.py` 文件头 + W2 注释 / `AGENT.md:30` 军规 / `docs/OKR-2026-09.md:46` KR2 /
`docs/CONTROL-LOOP-LEDGER.md` 规则 6 —— 该条规则原话「告警链用的是 `/ERROR|CRITICAL/`」现已**反向**,
补 2026-09-10 更新段指明现行对齐目标是 `error_alert.py::LEVEL_MARKERS`。历史 DEB / handoff 归档 /
`data/crontab.backup.*` 里的旧名**刻意不改** (那是当时的真实记录, 改了就是篡改台账)。

### 7.6 议题五 + §8.2: 三条日志卫生棘轮 (§9 #5 #6)

照 W2 形态接进 `scripts/daily_invariant_check.py` (复用既有 cron `5 5 * * 2-6`; 违反 → `logger.error`
→ `logs/invariant_check.log` → **本批刚修好的** `error_alert.py` 扫描面 → 钉钉 + exit 1, 不新建推送旁路):

> **⚠️ 2026-09-10 收尾批更正 (本节原声称的最后一跳不成立, 实测见下)**: 覆盖面那一跳**确实修好了**
> (`error_alert.py` 现在真的扫 `logs/invariant_check.log`, dry-run 实测 `per_source: {"invariant_check.log": 3}`),
> 但 **"→ 钉钉" 这一跳对不变量自检结构上不可达**: `daily_invariant_check.main()` 的 `rec()` 只累加
> `Counter`、**不逐条打日志**, 全函数只在末尾出 **1 条** `logger.error(f"[不变量自检] {N} 处违反: …")`
> (无论 N 是 1 还是 50); 而 `error_alert.py` 的阈值是 `T = max(3, ⌈3M⌉)` ⇒ 单条发射器**永远够不到地板 3**。
> **实测** (临时 logs 目录 + `dry_run=True`, 零推送零写状态, `now` 设到真 cron 时刻 2026-09-11 05:15):
> 注入 1 条 → `ERROR/CRITICAL 1 条 < 阈值 3` 不推; 2 条 → 同样不推; 3 条 → 触发并出正文预览。
> 真基线同窗实测: 05:15 窗 `M=0.5 T=3` (样本 6 日)、05:30 与 21:15 窗 `M=0.0 T=3`。
> 即 **W4/W5/W6 (以及既有的 W1/W2/W3) 违反会进 `invariant_check.log`、会让 cron exit 1, 但不会推钉钉**。
> 这不是本批新造的缺陷 (旧 `.sh` 更糟: 阈值 20 + 根本不扫该文件 = 双重不可达), 但本批 §7.6 与 §8.7
> 把它写成"已通", 属**声称过头**, 按 §8.5「撤回/更正也要落档」在此显式更正, 不静默改写。
> 根因是 §4.3 的探针用了 **3 条** ERROR —— 恰好等于地板, 于是"可触发性"判据过了, 却没测
> **被守卫者的真实发射形态 (1 条)** ⇒ 教训 §8.11。处置选项见 §9.1④ (**不擅自改阈值**, 那是判据变更)。

| 维 | 判据 | 存量基线 (全仓 AST 实测, 非 grep 估算) | 基线文件 |
|---|---|---|---|
| **W4** `debug_swallow` | `except` 块内禁止 `*.debug(…)` | **704 处 / 164 文件** (top: `order_executor.py` 99、`playbook/pipeline.py` 47、`position_monitor.py` 31、`scheduler_triggers.py` 26) | `data/debug_swallow_baseline.json` |
| **W5** `bare_logger` | 禁止新增裸名 logger (`getLogger("无点号字面量")`) | **15 处 / 15 文件** | `data/bare_logger_baseline.json` |
| **W6** `except_stack` | `except` 块内 `warning/error/critical` 必须带 `exc_info` (或用 `logger.exception`) | **1135 处 / 244 文件** (全仓 **4230** 个 `except` 处理器只有 **103** 处带栈) | `data/except_stack_baseline.json` |

- 扫描面 = `main.py` + `app/ scripts/ ops/ config/` 的 `*.py` (**778 文件**, 排除 `tests/ venv/ archive/
  __pycache__/` —— 测试与墓地不是生产可见面, 纳入即染红判据), 单次 AST 遍历 **6.8s** (每日一次可接受);
  `parse_errors` 必须为 0, 解析不了的文件落 `W456_parse_error` 而不是当"零违反"。
- 判据口径 = **per-file 计数**, 不是行号: 行号随任何编辑漂移 ⇒ 行号基线会天天假告警, 假告警一多守卫
  就被无视 (比没有守卫更糟)。`refs` 只作诊断样本 (带行号 + 代码片段), **不参与判定**。已诚实登记的
  弱点: 同一文件"修一处 + 新增一处"净计数不变 ⇒ 抓不到; 跨文件不掩盖。
- 语义与 W1/W2 一字不差: 基线外新增文件 / 基线内计数增长 → 当晚告警; 基线内存量 → `silenced_known`;
  计数下降 → `healed` 提示人工收缩 (**守卫绝不自动改写基线**, 棘轮只降不升); 基线损坏 →
  `*_detector_error`; 首跑自建基线不告警。改基线 = 改判据, 须先落档 ADR/DEB 并在 refs 写裁决出处。
- **议题五 (§5 logger 命名统一) 本批只加棘轮, 不做改名** —— §9 #6 推荐案原文即"先加棘轮再分批迁",
  且改名前须普查文档/脚本/Hermes prompt 里对 logger 名的字面引用 (§5 前置核对), 属大范围改动, 留下一批。
  附带自律: 本批新写的 `scripts/error_alert.py` 自己就用点号名 `ftseek.error_alert`, 不给 W5 添新存量;
  §7.7 删掉的 `audit_logger.py` 正好带走一个裸名 (`"audit"`) ⇒ W5 存量 17 → 15 (另一处是本批改名)。
- 钉子 `tests/test_w456_log_hygiene_ratchet.py` **16 passed**: ①新增/增长告警 (三维各一条) ②存量静默
  ③愈合只报告不改写基线 ④基线损坏 → detector_error 且不牵连另两维 ⑤解析失败显性化 ⑥首跑建基线
  ⑦口径钉 (嵌套 `except` 只计一次 / `except` 块外 debug 不计 / 带 `exc_info` 与 `logger.exception` 不计 /
  点号名不算裸名 / refs 带行号与片段) ⑧扫描面钉 (tests、archive、venv、`__pycache__` 排除 + 确定性排序)
  ⑨接线钉 (`main()` 必须真调 + 结果进 `invariant_check_latest.json` + cron 面仍在) ⑩一条**全仓真实基线**
  慢测 (与每晚 cron 同口径, 红了就是有人新增了基线外违反)。

### 7.7 议题六: 两个死审计组件退役 (§9 #7)

| 对象 | 处置 | 六面零引用核对 |
|---|---|---|
| `app/observability/audit_logger.py` (96 行) | **删除** (`git rm`) | ①仓内 import: 唯一引用是 `tests/observability/test_audit_logger.py` (只断言内存 list) ②用户 crontab 0 ③`/etc/systemd` + `/etc/crontab` + `/etc/cron.d` 0 ④Hermes `/root/.hermes/cron/jobs.json` 0 ⑤`.git/hooks` 0 ⑥scheduler `_load_script_module` 字符串面 0 |
| `tests/observability/test_audit_logger.py` | **随退役删除** (§2.4 判据 3 同型: 移除并在本 DEB 记录) | 同上 |
| `app/pipeline/observers.py` `class AuditLogger` (原 `:201`) + `def setup_observers` (原 `:217`) | **删除** + 文件头事件路由表**如实重写** + 退役区留证据注释 | 六面同上 (0 命中); 二者互为唯一引用 = 闭环死代码 |
| `observers.py` 的 `EventBus` / `PipelineEvent` / `get_event_bus` | **保留** —— 是 live 面: `app/pipeline/stages/runner.py:134` 的 `event_bus` property, 由 `signal_pipeline.py:1042 build_default_pipeline` 进入 | 整文件删会打断它, 故只切死成员 (删后实测 `get_event_bus()` 与 `runner` import 均正常) |
| `observers.py` 的 `NotificationObserver` / `LessonRecorder` | **保留但标注不可达** (§9 待裁点原文要求先查它们有无他处使用 → 查得: 六面零引用, 唯一注册入口就是本批删掉的 `setup_observers`) | 本批**不删** —— 超出 §6 预注册范围 (议题六只登记了两个"死审计组件"); 且它们在删 `setup_observers` **前后同样不可达**, 本批不改变其可达性。已在文件头写明"不得据旧表推断通知/教训链在跑", 留下一批按减法三步处理 |

- **减法三步的第②步"配置停"在本例是空操作**: 两个组件都没有开关, 也从未被构造过 ⇒ "停"的状态自始
  成立且**无任何代码路径能改变它**, 观察一周不会产生新信息 ⇒ 直接进第③步代码退役。此判断与 §7.3 的
  结算器相反 (那个有活挂载点在一晚跑 32 轮, 必须先配置停 + 观察一周), 差别写进 `observers.py` 退役区
  注释, 免得下一个人以为本仓的减法三步可以随便跳步。
- 顺带纠正 §6 表格未记的两处文档不实: `observers.py` 旧文件头把 `MetricsCollector` 列为本文件的 Observer
  (实体在 `app/observability/metrics_collector.py`, 唯一调用方 `scripts/archive/run_metrics_collector.py`
  已归档), 且把从未接线的 Observer 写成已接线 —— 已随退役一并改正。
- `logs/audit/` 目录与被删的两个类**无关** (§6 注): 该目录由 `post_market_audit.py:48` 与
  `pre_market_check.py:379` 的 FileHandler 写, ④批已补 logrotate 覆盖 ⇒ 删这两个类不影响它。


### 7.8 收尾批 (同日 11:35–12:10): 全量回归咬出的 2 处 + 独立复核新发现的 1 处存量

全量回归 `3674 passed / 2 failed / 41 skipped` —— **两处失败都是本批自己引入的**, 逐条修代码而**不是调基线**:

| # | 现象 | 根因 | 处置 | 钉子 |
|---|---|---|---|---|
| A | `test_except_pass_ratchet` 红: 955→956 | 新写的 `scripts/error_alert.py` 自带 **2 处** `except: pass` (`load_state` 的 `FileNotFoundError` / `push` 的 `finally: hub.stop()`) ⇒ **一个专门为"让失败可见"而生的脚本自己吞错**, 与本批 §4 与 §8.7 的立项理由自相矛盾 | ①`load_state`: 首跑无状态文件是**正常态**不是异常 ⇒ 改显式 `return _empty_state()` (语义与 fall-through 逐字等价; 空状态收敛到单一真源, 防两处字面量各自漂移漏键), **刻意不留 WARNING** 免得每次首跑制造零信息量告警; ②`push`: `hub.stop()` 失败 ⇒ `logger.warning(..., exc_info=True)` (`sync=True` 时消息已发出, 失败主要意味聚合缓冲未 flush / 线程池未关) —— **带 `exc_info` 故不撞本批刚立的 W6 棘轮** | 实测计数回落 **954 ≤ 基线 955** (逐文件差异归零) |
| B | `test_no_archive_imports` 红: 5 处命中 | `PATTERN` 第三条 `scripts/archive/` 是**字面量**探针 (为抓 §8.6 第六面的按路径动态加载), 连散文一起咬: `observers.py` 退役证据注释 / `error_alert.py` 的**运维回滚指引** / W456 棘轮的**测试夹具路径** / 断言提示文案 —— 五条没有一条是加载 | 加 `PROSE_ALLOWLIST`, 房型照 `tests/test_dead_db_literal_gate.py`: 键 = (文件, **稳定子串**) 而非行号 (行号随任何编辑漂移 ⇒ 天天假告警, 假告警一多守卫就被无视); **双向** = 未登记命中红 + 登记项已不再命中也红 (防名单腐烂成永久盲区); 且 `IMPORT_SHAPE` **优先判定** ⇒ 真 import 形态永不豁免。**刻意不把散文改含糊**: 回滚指引必须是可复制粘贴的准确路径, 夹具路径必须真的是 `scripts/archive/dead.py` 才能验证"archive/ 被扫描面排除" | 新增 **2 例**: `test_allowlist_not_rotten` (方向2) / `test_allowlist_does_not_mask_real_import` (防假绿: 合成 `from scripts.archive import x` 即使写在豁免文件、即使行尾带豁免子串也必须红) + 一条反向臂 (豁免确实生效, 否则方向1 退化成"任何提及都红"= 守卫被无视) |
| C | **独立复核新发现 (存量, 非本批引入)**: `pangan_daily_rebuild.py::step0` 的 `[step0] 日K增量:` 行长期显示 futu SDK 的 `on_disconnect`, 真实摘要被顶掉 | 见 §8.9 (流归属) | 摘要改从**子进程 stderr** 取; 抽成纯函数 `_pick_refresh_summary(stderr, stdout)` 便于钉子直测 (真跑子进程 = 281 票 × 1.2s ≈ 7m45s 且打真 Futu 配额); 两流都无摘要 ⇒ 显式 `无摘要输出` 而非空串 (空串与"摘要为空"不可区分 = 静默失败形态); 无 `[日K刷新]` 行时退回 stdout 末行保留旧痕迹 | 新文件 `tests/test_pangan_step0_summary.py` **5 例** (合成正文取**日志实测逐字样本**而非理想化字符串; 含反向臂: stderr 里的 Traceback/RuntimeError 不得冒充摘要) |

**C 的存量证据 (非推断)**: `logs/pangan_rebuild.log-20260827.gz` / `-20260903.gz` / `-20260906.gz` /
`-20260909.gz` / `-20260910` **连续五档**的 `[step0]` 行全部形如
`OK 2026-09-10 05:56:50,244 | 856266 | … on_disconnect: Disconnected: conn=0(1) reason=CallClose`
⇒ `完成: 刷新 0 | 已最新跳过 280 | 失败 1` 这类真实读数在该日志里**从未出现过**, 日K刷新的失败票名单
长期不可见; 而行首那个 `OK` 是 returncode, 只证明子进程没崩、**不证明刷新成功** ⇒ 病灶被 "OK" 掩盖。
与 §8.7「`logger.error` 存在 ≠ 告警存在」同族, 是它的镜像形态: **"日志行存在 ≠ 日志行有用"**。
本批的 print→logger **既没加重也没修好它** (改前改后 `r.stdout` 末行同为 SDK 行), 是收尾复核时顺着
"谁在下游解析这 7 个脚本的输出"查出来的**第 7 个解析面** (§8.3 只普查了格式面, 漏了流归属面)。

**端到端实测 (非只单测)**: 以 `subprocess.run(capture_output=True)` 真跑
`refresh_daily_bars.py --dry-run --limit 2`, 旧实现打
`[step0] 日K增量: OK `(末行为空/SDK 噪声), 新实现打
`[step0] 日K增量: OK 2026-09-10 11:41:46,924 [ftseek.refresh_daily_bars] INFO: [日K刷新] 完成: 刷新 0 | 已最新跳过 0 | 失败 0`。

### 7.9 重启与观察回执 (§9.1② 登记后, 用户裁「请重启再观察」, 2026-09-10 13:01)

**重启前安全预检** (用户已下令也不跳过 —— 树里有他人在途 live 路径改动):
全部脏 `.py` 过 `py_compile`; 9 个关键模块 `importlib.import_module` **全 OK**
(`dual_broker` / `playbook.db` / `playbook_service` / `position_sync` / `observers` / `config_audit` /
`reconciler` / `shadow_registry` / `scheduler_core`) ⇒ 排除"重启进一个起不来的树";
退役面复验 `get_event_bus()` → `EventBus`、`app.pipeline.stages.runner.PipelineRunner` 在位
⇒ §7.7 只切死成员没打断 live 管线。

**重启**: `systemctl restart ftseek-us` ⇒ PID **889343 → 910996** @ **13:01:47 CST**, `NRestarts=0`, `active`。

**冒烟 `startup_smoke_check.py --wait 90` = 5/7 (exit=1)** —— 两项红**经归因均为他人在途批, 非本批**:

| 断言 | 结果 | 归因 |
|---|---|---|
| 1 ConfigValidator / 2 漂移检测 / 3 热重载 / 4 SSOT 审计 0 BLOCKING / 6 退出快循环 | ✅ 全过 | — |
| 5 **模拟盘安全确认** | ❌ | 他人**重写 `main.py` 启动模式块** (13:06 又改一次; `git diff main.py` 已从本批的 14 行涨到 **46+/40-**): `[安全确认] ✅ 当前运行模式: 模拟盘 (TRADE_ENV=1 | FUTU_TRADE_ENV=1)` → `[StartupMode] 模式一致: broker=sim db=sim ALLOW_REAL_TRADING=False` + `[StartupMode] FUTU_TRADE_ENV=1 已废弃` ⇒ 守卫针 `模拟盘 (TRADE_ENV=1` 失配。**安全实质未变且表述更严** (见下), 断的是**字面量契约** —— 正是 §8.3「改输出面必须普查下游解析面」, 他们没普查本守卫。连带 `startup_smoke_check.py:46` 的 `BOOT_MARKER_FALLBACK = "[安全确认]"` **同时失效** (若 `[PreFlight]` 哪天消失, 启动段抽取会退到 exit=2 = 守卫整体失明)。 |
| 7 **零 Traceback/CRITICAL** | ❌ | 他人新功能 `position_sync.py:169 _reconcile_playbook_execution_states` 查 `execution_state` 列, 而该列在生产库 **`open_positions`(41 列) / `trade_records`(51 列) 里都不存在** ⇒ `sqlite3.OperationalError`, 即**功能代码先于迁移落地**。已被捕获且 `WARNING + exc_info` (符合本批 W6 口径), 对账本身仍完成 (`[US Sync] Done | Futu: 1 | 主线DB: 0 | playbook自管: 1 | 一致性: ✓`)。启动段命中 2 次, 因 `[US Sync] Market closed, sync paused` 未继续; 但**开盘后按 30s 间隔会持续产生** (6.5h ≈ 780 次 WARNING+Traceback/交易日)。 |

**安全实质三项独立复核通过 (本次重启最重要的一条)**: `[StartupMode] 模式一致: broker=sim db=sim
ALLOW_REAL_TRADING=False` / `[调度器] 账户路由 AUTO_TRADE_ACCOUNT=sim | ALLOW_REAL_TRADING=False` /
`[富途] 连接成功 127.0.0.1:11111 env=模拟` ⇒ **仍在模拟盘, 零真实资金风险**。
(注: `[US Sync] ✅ REAL 持仓查询权限已解锁` 是**只读对账**用途, 原文即注明"下单环境仍由
AUTO_TRADE_ACCOUNT 决定", 与 10:05 段逐字相同 ⇒ 非本批、非本次重启引入。)

**本批 §3 的线上实证 (重启的全部目的, 改前改后同口径对照)**:
- `app/positions/reconciler.py`: 启动摘要 WARNING **12 条 (10:05 段) → 1 条 (13:01 段)**, 且剩下的那 1 条
  正是**刻意保留**的异常分支 `[Reconciler·sig] 本轮签名为空但历史非空`; 16 行横幅已合成**单条 INFO record
  + 9 条续行**, `matched / broker_only / adopted / legacy / playbook / local_only / errors` **一条不丢**,
  `"=" * 70` 纯分隔符行消失 ⇒ §3.4 判据 1/2/4 与"降噪 ≠ 降信息"当场兑现。
- `app/core/config_audit.py`: 3 个非阻断项从 3 条 WARNING → **单条 INFO record + 3 条续行**, 级别终于与条目
  **自带**的 `⚠️ [INFO]` 标签一致, 且与紧邻的 `main.py:492 [ConfigAudit] ✅ … 审计通过` (INFO) 不再互相打脸。
- §7.7 退役面: 启动段 **0 条 ImportError / ModuleNotFoundError**, 管线正常建起, 快循环与 6 个 watchdog 心跳注册正常。

**观察窗 (13:01:47 → 13:21:14, 19.5 min)**: `active`, PID 910996, `NRestarts=0`, 27 线程, 0.6% CPU, RSS 172MB;
主日志 **222 INFO / 8 WARNING / 0 ERROR / 0 CRITICAL** + 2 条 Traceback (均为上述 B1);
钉钉只 2 条推送 (13:01:40 关闭通知 / 13:02:03 启动通知), **无意外告警**;
`deploy_freshness_check.py` 的 `stale_drift` 从 10375s 降到 **279s** (因他人 13:06 又改了 `main.py`),
仍 `skip … outside_safe_window`, 未推送 critical。

**8 条 WARNING 的族分布 (§3.4 复算必须据此扣账)**: `scheduler_core` 3 (存量: bridge 文件不存在 / config-diff ×2)
+ `position_sync` 2 (**他人新族** B1, 带栈) + `main` 1 (**他人新族** StartupMode 废弃提示)
+ `earnings_calendar` 1 (存量) + `reconciler` 1 (刻意保留的异常分支)。
⇒ **§3.4 复算时须把他人这两个新族单独列出扣除**, 否则会把"别人新增了 WARNING"读成"本批降噪失败";
尤其 B1 族开盘后按 30s 增长, **单族即可淹没全仓 WARNING 口径**, 届时 §3.1 基线的"其余 15 族"对比会失真。

**回归复核 (13:19, 触及面 + 他人在途面 445 例)**: `9 failed / 436 passed`。9 条全部是 §9.1③ 的 W456 耦合族
(8 个无关不变量测试 + W456 真实基线慢测); 上一轮 (12:05) 那 9 条 exit-day-accounting 红**已被该批自行修绿**。
W456 存量违反也从 3 处收敛到 **1 处** —— `app/playbook/playbook_service.py` W6 `35→37 (+2)`,
例 `L735: logger.warning(f'[playbook] signal_snapshots 查询失败: {e}')` 缺 `exc_info`。
即 **本批棘轮当天就咬住了当天正在写的新代码并驱动作者修复** (`position_sync` W4/W6 与 `dual_broker` W6
三处已在 13:20 前被其作者修掉), 这正是 §7.6 立项要的效果, 也印证 §9.1③ 的耦合代价确实发生了。

**留待他人处置 (本批不代修 — 军规: 只修本批引入的)**:
1. `execution_state` 迁移未落 ⇒ 补迁移或先关掉 B1 查询, 否则开盘后每 30s 一条 WARNING+Traceback。
2. `startup_smoke_check.py:39` 断言 5 的针 + `:47` `BOOT_MARKER_FALLBACK` 须随 `[安全确认]→[StartupMode]`
   改名同步 —— **这是一个安全守卫当前处于红态**, 红久了会被当成"已知红"而失去作用 (§7.6 同款风险)。
3. 剩余 1 处 W6 违反 (`playbook_service.py:735` 等 2 处补 `exc_info=True`)。

**⚠️ 运维前瞻 (与本批无关但必须说)**: `deploy_freshness_check.py` 的规则是
`drift > 120s 且在安全窗 (北京 09:00–12:00) → systemctl restart + critical 告警 (冷却 6h)`,
cron `*/10 * * * *` **全天**跑 ⇒ 只要工作区还有比进程新的 `app/**/*.py` 或 `main.py`,
**明早 09:00–12:00 该守卫会自动重启服务**, 把当时磁盘上的在途代码加载上线。
他人在途批若未在明早 09:00 前落地或提交, 会由这个守卫"代为上线"。

---

## 8. 方法教训 (本批新增, 供宪章/AGENT.md 考虑收编)

1. **「幂等」必须区分数据级与执行级。** 本仓 12 个盘后钩子全部只有数据级幂等(不重复写行),
   零个有执行级日门(不重复计算)。数据级幂等在 record-only 影子上看起来"无害", 于是提交信息
   敢写「已最新110票零拉取」—— 而代码是"先拉再判"。**判据**: 凡声称幂等的挂载钩子, 必须在
   日志里留下**执行次数**证据 (本 DEB §1.5 判据 1/2 即此形态), 不能只证"数据没重复"。
   同源于宪章七律 #7「遥测实测 > 文档自述」。
2. **`debug→warning` 逐案修 4 轮仍在漏。** 已修锚点: `scheduler_core.py:2375`/`:2450`(2026-07-04)、
   `playbook_service.py:1466`/`:1486`(DEB-20260721 R4)、`pipeline.py:800`(2026-07-27)、
   DEB-20260904 #148。本批又抓出 4 处, 其中 `:2461` 与 `:2450` 的修复**相隔 11 行**。
   全仓仍有 **596** 处 `logger.debug` 记失败/异常, `LOG_LEVEL=INFO` 下全不可见。
   **建议立规则而非继续逐案**: 照 W2 棘轮形态 (`scripts/daily_invariant_check.py` +
   `data/field_cardinality_baseline.json`) 加一条不变量 —— `except` 块内禁止 `logger.debug`,
   基线登记现存 596 处逐条带 refs, 只降不升, 基线外新增当天告警。
   同理 `exc_info`: 4302 个 `except` 只有 58 处带栈 (98.7% 无栈), 可同条棘轮管。
3. **改日志格式前必须普查解析面, 且把契约写进代码。** 本批差点踩坑: `%(threadName)s` 若插在
   `[%(name)s]` 与 `%(levelname)s` 之间, `intraday_heartbeat.py:244` 的字面量 `"] ERROR:"` 即失配
   ⇒ 静默失败检测器恒报 0 错 0 警, 正是本仓反复踩的「守卫只能报健康」形态
   (`ops/hermes/ftseek_us_restart_20260829_hotfix.sh:64` 首版写 `'/ ERROR /'`;
   `tests/test_daily_alert_equity_recon.py:11` 同宗)。现已把 6 面契约写进 `main.py:37-52` 注释。
4. **降噪与致盲只有一线之隔, 判据必须双向。** §3.4 判据 3(其余族不得减少) 与判据 5(人为制造
   失败必须仍告警)、§4.3 判据 1(注入探针必须触发) + 判据 2(注入误报不得触发) 都是为此。
   单向判据("WARNING 变少了") 在本仓已被证明会放过致盲。
5. **撤回自己的建议也要落档。** 原建议 ④ 含「加 `maxsize`」, 核实后发现与 `dateext` 撞名 +
   `resolve_log_sources` 拼档冲突, 收益不抵风险 ⇒ 撤回并写明理由 (§7.2), 而非静默不做。
6. **零引用核对有第六面: 进程内按路径/字符串的动态加载。** `scheduler_core.py:2458` 经
   `_load_script_module("settle_overnight_shadow")` (importlib 按**文件路径**加载, 无 `import` 语句)
   挂载结算器 ⇒ 8/28 那次只查 crontab/Hermes 的核对得出「无排班」结论, 而实际一晚跑 32 轮。
   AGENT.md:25 的五面 (仓内 import / crontab / systemd / Hermes prompt / `.git/hooks`) 中,
   "仓内 import" 这一面**只搜 import 语句就会漏**它。
   **判据**: 核对必须用被退役对象的**裸名字面量**全仓搜 (`rg '<name>'` 不限语法位置), 覆盖
   一切按字符串派发/加载的机制 (`_load_script_module` / `getattr` 派发 / yaml 里的 script 路径 /
   entry_points)。本批 §7.7 的退役核对已按六面执行, 并把第六面写进新键注释与注册表 notes。
7. **`logger.error` 存在 ≠ 告警存在 — 告警链必须实测端到端, 且双向。** `daily_invariant_check.py`
   自 7/28 起就在文件头写「违反 → logger.error → 被 error_alert 拾取推钉钉」, 但它写
   `logs/invariant_check.log` 而 `error_alert.sh` 只读 `logs/ftseek_us.log` ⇒ 这条链**纸面了 ~6 周**。
   代码里搜得到 `logger.error` 会让核对者以为链路通。**判据**: 凡声明"违反 → 告警"的机制, 必须
   ①注入探针证明**会**触发 ②注入形似而级别不同的行证明**不会**误触发 (§4.3 判据 1+2), 两条都跑过
   才算立起; 且告警器的**日志文件覆盖面**本身要有用例钉 (本批 `test_invariant_check_log_is_covered` +
   `test_new_log_file_auto_covered`: 用 glob 而非登记表, 因为登记表正是这次断裂的成因)。
8. **棘轮基线文件没入库 = 改判据不经 review。** 本批三份新基线 (`data/{debug_swallow,bare_logger,
   except_stack}_baseline.json`) 沿用 W1/W2 形态放在 `data/`, 而 `.gitignore:46` 是 `data/*` 整体忽略
   (白名单只有 `config_freeze_v2.json`) ⇒ 现存 6 份判据基线 (含 `config_freeze_baseline` /
   `field_cardinality_baseline` / `watchdog_liveness_baseline` / `config_key_reverse_census_baseline`)
   **全部不在版本控制里**。两个后果: ①改基线 (= 改判据) 不经 diff/review, 与"改判据须落档"的纪律
   互相矛盾; ②`data/` 一旦被清理, 棘轮下次运行会**按当时的现状重建基线** ⇒ 墓地被自动重新藏起来
   (正是 CONTROL-LOOP-LEDGER 规则 5 要防的形态)。本批**不擅自改 .gitignore** (那是跨 W1/W2 的判据
   治理变更, 未预注册), 只登记为待裁项 → 见 §9 尾。
9. **改输出面必须普查「流归属」(stdout vs stderr), 不只普查格式。** §8.3 立的是"改日志**格式**前必须
   普查解析面", 本批收尾又漏掉一面: print→logger 后输出从 **stdout** 迁到 **stderr**
   (`logging.basicConfig` 与 `logging.StreamHandler()` 的默认流都是 stderr, 仓内 `main.py:49` 同款),
   而任何 `subprocess.run(..., capture_output=True)` 的下游只读 `r.stdout` 就会**静默拿到别的东西**
   (本例拿到 futu SDK 自己写 stdout 的 `on_disconnect` 行) —— 不报错、不改 returncode、只是读数变成噪声,
   正是最难发现的一类 (§7.8 C, 存量 ≥2 周, 五档日志实证)。**判据**: ①普查下游解析面时必须逐面记下
   "它读的是哪个流", 流归属与格式同为契约; ②凡把 print 改成 logger 的脚本, 落地前要 `rg` 一遍
   **谁把它当子进程调** (`subprocess` / `Popen` / `os.system` / shell 里的管道与反引号), 不能只查 `import`
   —— 本批 7 个 print→logger 脚本里只有 `refresh_daily_bars.py` 有子进程调用方
   (`pangan_daily_rebuild.py:86`), 另 6 个只经 `_load_script_module` 进程内加载 ⇒ 受害面恰好一处,
   但"恰好一处"是查出来的不是猜出来的。
10. **新写的代码也要过既有棘轮, 且"守卫/告警器自己"是最高优先级的守法对象。** 本批新写的
    `error_alert.py` 自带 2 处 `except: pass`, 被 8/31 立的 `except:pass` 棘轮当场咬住 (955→956);
    若只跑自己的 28 条新钉子就重启, 这条红会被留在树上 (§7.8 A)。**判据**: 新文件落地后必须跑一遍
    **全部棘轮/守卫类**用例 —— 本仓现有 `except:pass`、W1 `config_version` 冻结、W2 `field_cardinality`、
    W3 dead_db 字面量门、W4–W6 日志卫生、archive 零活 import —— 而不是只跑本批新增的钉子。
    棘轮的价值恰恰在于它对"新代码"生效, 而作者最容易假设自己的新代码是干净的;
    **告警器自己吞错**比别处吞错更严重 (它坏了没人知道, 见 §8.7)。
    同批的另一半教训: **字面量型守卫必然会咬到散文** (`scripts/archive/` 路径、dead_db 文件名),
    处置不是把散文改含糊 (回滚指引必须可复制粘贴, 夹具路径必须真的是那个路径), 而是登记**双向**豁免名单
    (未登记命中红 + 陈旧豁免也红) 且让"真加载形态"优先于豁免 ⇒ 豁免只覆盖散文 (§7.8 B)。
11. **端到端探针必须用「被守卫者的真实发射形态」, 不能用探针自己的理想形态。** §4.3 判据 1 用 **3 条**
    探针 ERROR 证明了 `error_alert.py` 可触发 —— 但 3 恰好**等于**阈值地板 `MIN_THRESHOLD=3`, 于是
    "可触发性"过了, 而真正要守卫的对象 `daily_invariant_check.py` **每晚只发 1 条** ERROR
    (`rec()` 只累加 Counter, 全函数末尾一条汇总) ⇒ 结构上永远够不到地板 (§7.6 更正框, 已 dry-run 实测)。
    同族形态: 用"能过"的样本证明"链路通", 而没问"线上真实样本长什么样、有几条"。
    **判据**: 凡声明"A 违反 → B 告警"的链, 探针的**条数/形态必须取自 A 的真实发射口径**
    (先数 A 在一次真实违反里到底写几行、写哪个流、写哪个文件), 再验证 B 会不会响; 且当 B 有阈值时,
    必须显式核对 `A 的单次发射量 ≥ B 的阈值` 这个不等式 —— 本例 1 ≥ 3 为假, 一算就露。
    本批已把这条不等式写进 §9.1④ 待裁, 不擅自改阈值 (改阈值 = 改告警判据)。

---

## 9. 待裁清单 (按建议执行顺序)

用户裁决 (2026-09-10 午间): **「按照建议执行优化」** = 按下表推荐案执行。落地回执见 §7.3–§7.7。

| # | 议题 | 推荐案 | 性质 | 待裁点 → 本批怎么办的 | 状态 |
|---|---|---|---|---|---|
| 1 | §1 盘后电池 32 轮空转 | A (`refresh_daily_bars` 当日门 + 失败重试语义) | 行为变更 (API 调用量 ↓32×) | 裁 A 不裁 C: **B 案不做** —— 它会消掉"盘后补数据 → 下一轮立刻重算"的隐性自愈 (`position_path_collector` 晚落 bar 靠后续轮补算), 且改 12 钩子的风险面远大于 A 的 1 文件 | ✅ §7.3 |
| 2 | §2 死开关 + write-only 链 | A (减法三步退役结算器) | 代码退役 | 只做**第②步配置停** (新键默认 false); 74 笔历史反事实与脚本/测试本批**不删** —— 第③步须观察一周 (§2.3 原文), 且写入方若重启, 历史行会被幂等全量补算 | ✅ §7.3 |
| 3 | §3 WARNING 通胀 | 两处降级 | 判级口径变更 | 附带项 (纯分隔符行改单 record) **一并执行**; 异常分支与 blocking/CRITICAL 路径一律不动 | ✅ §7.4 |
| 4 | §4 error_alert.sh | A (Python 重写 + 接 push hub + 基线阈值) | 告警判据变更 | 裁 A 不裁 B/C: C (退役) 的前置核对不成立 —— `intraday_heartbeat` 只做静默失败检测且**只读主日志**, 覆盖面与本项不重合; B (只改阈值) 留下 5 处缺陷 | ✅ §7.5 |
| 5 | §8.2 debug/exc_info 棘轮 | 立不变量自检 (照 W2 形态) | 新机制 ⇒ 按宪章 §四.1 排队 | **插队执行** (用户令"按建议执行"即含此项): W4 `debug_swallow` + W6 `except_stack` 两维, 复用既有 cron 与告警面, 不新建调度/推送 | ✅ §7.6 |
| 6 | §5 logger 命名统一 | 先加棘轮再分批迁 | 大范围改名 | **只加棘轮** (W5 `bare_logger`); 改名留下一批 —— §5 前置核对 (文档/脚本/Hermes prompt 里的字面引用普查) 未做, 大范围改名不在本批判据内 | ◐ 棘轮 ✅ / 改名待另批 |
| 7 | §6 两个死审计组件 | 减法三步退役 | 代码退役 | 补齐**六面**核对后退役 (含本批新发现的第六面, 见 §8.6); 第②步"配置停"在本例是空操作, 理由落档 | ✅ §7.7 |

**一次一变量纪律** (宪章 §四.2) 的本批执行说明:
- 原建议"议题 1 与议题 2 不同交易日落地" —— 本批**同日落地**, 理由是归因未被破坏: 两者动的是
  AFTER_HOURS 段里**两个互不相干的钩子** (不同脚本 / 不同配置键 / 不同失败模式), 且各自的预注册判据
  可从日志**独立复算** (§1.5 判据 1 = `日K刷新] 扫描` 计数应为 1; §2.4 判据 1 = `隔夜持仓反事实`
  计数应为 0) ⇒ 任一条不满足都能单独归因到单独回滚 (删状态文件 / 翻 yaml 键), 不需要靠"隔一天"来分辨。
  纪律真正要防的是"两个变更改同一个信号、判据无法拆分" —— 本例不属该形态。
- 原建议"议题 3 与议题 4 同批落地并共用 §3.1 基线复算" —— **已照办** (同批), 复算时 §3.1 的
  逐族 WARNING 计数与 §4 的 ERROR 计数须同一次取样, 否则无法区分"降噪"与"告警口径变了"。
- 本批**未触发**"一次一变量"的强约束面: 所有改动都不改信号分布、不改退出行为、不改下单/风控判据
  (①A 只改数据刷新的执行次数; ①B 只停一个 record-only 影子结算器; ③④⑤⑥⑦ 是日志/告警/守卫面)。

**改后待复算的判据 (下一个交易日起, 连续 2 日)**: §1.5 (5 条, 主判据 = 判据 4 缓存末日覆盖率不得下降)、
§2.4 (4 条)、§3.4 (5 条, 主判据 = 判据 3 其余 15 族不得减少 + 判据 5 人为失败仍告警)、
§4.3 (4 条, **已于本批实机验证**, 见 §7.5 表)。

**新登记的待裁项 (本批发现, 不擅自执行)**: 判据类基线文件是否统一入库 —— 见 §8.8。现状 6 份基线
全在 `.gitignore` 的 `data/*` 忽略面里, 改判据不经 review 且 `data/` 被清理即静默重置棘轮;
修法只需在 `.gitignore` 加 6 行 `!data/*_baseline.json` 白名单, 但那是跨 W1/W2 的判据治理变更,
须用户裁 (宪章 §三.1 一事一 DEB)。

### 9.1 收尾批新登记的待裁项 (2026-09-10 11:35–12:10, 均不擅自执行)

**① `pangan_daily_rebuild.py::step0` 仍每晚多做一次全量日K刷新 (未过日门) ⇒ 一夜全量次数 32→2, 不是 1。**
cron `50 5 * * 2-6` 以**子进程 CLI** 调 `refresh_daily_bars.py` ⇒ 走 `main()` → `do_refresh()`,
**不经 `run_check` 的执行级日门** (门只装在 scheduler 的进程内挂载面 `scheduler_core.py:2577`)。
故一夜的全量刷新是 **2 次** (北京 04:01 进程内门控 1 次 + 05:50 cron 1 次), 配额从 ~8992 次降到 ~562 次
(16× 而非 32×)。**§1.5 判据 1 仍然成立** —— 该判据数的是**主日志**里的 `[日K刷新] 扫描` 行数, 而 cron 那次
的输出被 `capture_output=True` 吞进管道、只落 `logs/pangan_rebuild.log` ⇒ 复算时看到主日志 1 行即为通过,
不要误判。**不擅自处置的理由**: 05:50 (ET 17:50) 那次可能是**有意的二次机会** —— 04:01 (ET 16:01, 收盘后
仅 1 分钟) 拉到的 D 日日K未必已定型, 首轮拿不到则 05:50 补; 删它或给它加门 = 改**数据新鲜度语义**,
属判据变更须先落档。**待裁三选**: 保留 (承认 2×) / 让 step0 也走门 (需给 CLI 加 `--respect-gate`,
现有 `--ignore-gate` 是明示语义的空开关) / 直接删 step0 (承认 04:01 单次)。

**② 重启被"他人在途 .py"挡住 ⇒ §3.4 判据的时钟尚未起算。**
收尾复核时发现工作区有**另一批**在途改动 (`app/execution/dual_broker.py` mtime 11:47 /
`app/core/position_sync.py` 11:53 / `app/playbook/db.py` / `app/playbook/playbook_service.py` /
新 `tests/playbook/test_m2_b1_execution_state.py`, 4 文件共 **361 insertions**), 全部落在 **live 交易路径**
(券商账户探测 / playbook 落库 / 指令生成 / 持仓对账)。本仓的重启前置条件 —— handoff 2026-09-10 07:40 条
原文「重启前已核 app/+scripts/ 脏文件全属本批, **无他人在途 .py**」—— 因此**不成立**, 本批**未重启**,
何时重启由用户裁 (不得用 `git stash`/checkout 清理他人在途改动来"制造"重启条件)。
**→ 本项已由用户裁决结案: 上报后用户裁「请重启再观察」⇒ 已于 13:01:47 重启, 预检/冒烟/观察/归因全部回执见 §7.9。**
**影响面已逐项核实并分级**:
- **今晚即生效, 不需重启**: 议题一 (执行级日门) 与议题二 (结算器 fail-closed 停) 都是 **AFTER_HOURS** 钩子,
  而 `_load_script_module` 用 `self._script_mod_cache` 做**进程内缓存**; 当前进程 PID **889343** 起于
  **10:05:16 CST** (北京日间), 此后**未进入过 AFTER_HOURS** (北京 04:00–08:00) ⇒ 这两个模块**尚未被缓存**,
  今晚 04:00 首次加载即从磁盘 `exec_module` 读到新代码。新键 `playbook.overnight_hold_settler_enabled`
  由 ConfigHotReloader 覆盖 (10:19:35 已实测到一次热载失效快照)。
  ⇒ **§1.5 / §2.4 判据可按原计划在下一个交易日起复算**, 不必等重启。
- **须重启才生效**: §3.4 (WARNING 降级: `app/positions/reconciler.py` + `app/core/config_audit.py`)、
  §7.7 (死审计组件退役: `app/pipeline/observers.py`)、`scheduler_core.py` 午间批新增的 `exc_info`/`debug→warning`。
  这些全是**日志级别 / 死代码**面, **零交易行为** ⇒ 不重启无功能风险; 但 §3.4 的"改后 WARNING 计数"时钟
  要等重启那一刻才起算 —— **复算必须在重启之后连续 2 个交易日取样**, 否则会量到未降级的代码而误判失败。
- **另一处红不是本批的**: `test_w456_log_hygiene_ratchet.py::test_real_repo_within_registered_baseline`
  当前**红**, 三处违反 (`position_sync.py` W4 13→15 / W6 14→15, `dual_broker.py` W6 7→8) 经归因
  **全部来自上述在途批**。证据: `logger.debug(f'[US Sync·B1] execution state 对账跳过: {exc}')` 这行在
  `HEAD` 里**不存在** (`git show HEAD:app/core/position_sync.py | grep -c` = 0, 工作区 = 1);
  且两文件是在基线建立 (**11:17:06**) 之后才被改 98 / 55 行。本批**不代修** (军规: 只修本批引入的),
  由该批按 W4/W6 的处置建议自行改代码 (推荐) 或落档后登记基线。
  这也**当场证明了本批棘轮的价值**: 它咬住的第一个对象不是历史墓地, 而是**同一天正在写的新代码**。

**③ W456 接进 `daily_invariant_check.main()` 造成 8 个无关不变量测试**非密封** ⇒ 本批设计缺陷, 待裁修法。**
现象: 全量回归 `18 failed` 里有 **9 条**是同一根因 —— `test_attribution_watch_retirement`(3) /
`test_invariant_d4_settled_fields`(2) / `test_invariant_i10_exit_time`(2) / `test_stale_bridge_guard` I15(1) /
W456 真实基线慢测(1)。前 8 条的断言形态都是"干净库 ⇒ `rc==0` 且 `result['violations']=={}`",
而它们拿到的实际值是 `{'W4_debug_swallow_grew': 1, 'W6_except_stack_grew': 2}`。
根因: W1/W2/W3 都是 **DB 或基线域**的检查 (合成库上恒净), 而 W4/W5/W6 是本仓**第一个扫活体源码树**的
检查 ⇒ 任何人任何时刻在任何文件里新增一处基线外违反, 都会让这 8 个"与日志卫生毫无关系"的测试变红,
且失败信息里只有 W4/W6 的键名、没有文件与行号 ⇒ **误归因** (改 `position_sync.py` 的人看到
`test_invariant_i10_exit_time` 红, 完全无从下手), 正是 §7.6 自己写的"假告警一多守卫就被无视"形态。
**注意: 违反本身是他人在途代码的 (见 ②), 但把 1 处仓库违反放大成 8 条无关红灯的耦合是本批的设计。**
**收尾批实测追加第二项代价 (不只是误归因, 还有运行时)**: `_check_log_hygiene` 单次 = **15.51s**
(扫 778 文件, `parse_errors=0`; §7.6 记的 6.8s 是空载值, 本次机器 load 9.45 时实测 15.51s),
而**每一个**调用 `daily_invariant_check.main()` 的测试都要付这笔钱 ⇒ 已知 8 个耦合测试
≈ **+124s** (空载约 +54s)。实测后果: 收尾批两次针对性复核 (7 文件 / 3 文件) 分别在 400s 与 240s
`timeout` 下被杀 (`exit=124`), 已跑完的 37 例与 20 例均 **0 失败**但拿不到汇总行。
即本项耦合把"每晚一次的 6.8s 扫描"变成了"每个不变量测试各付一次", 是 §7.6 设计时**没算到的账**
(当时只评估了 cron 侧"每日一次可接受")。**这条实测数字使 (甲) 从"推荐"升级为"应尽快做"** ——
密封后 8 个测试各省 15.5s, 全量回归可缩约 2 分钟。
候选修法 (各有代价, 故待裁不擅动):
- **(甲) 8 个测试各加一行密封**: 在它们的 `_run` 里 `monkeypatch.setattr(dic, "_hygiene_py_files", lambda repo=None: [])`
  —— 钩子本批已预留 (`_hygiene_py_files(repo=None)` 就是为可注入而抽的)。代价: 要动 8 个**属于别的批次**
  的测试文件, 且其中 2 个正被他人在途编辑 ⇒ 现在动必冲突。
- **(乙) W456 拆成独立入口** (`daily_invariant_check.py --hygiene-only`, crontab 加一行): `main()` 回到纯 DB 域
  ⇒ 8 个测试天然密封, 告警链不变。代价: 与 §9 #5 "复用既有 cron, 不新建调度" 的原裁决相反, 且一夜两次进程启动。
- **(丙) 结果分域**: W4/W5/W6 的违反不进 `viol` 而进 `result["log_hygiene"]["violations"]`, cron 侧单独判非空并
  `logger.error`。代价: `rc` 与 ERROR 行的关系要重新钉 (现有 16 条钉子里的"接线钉"要跟着改),
  且 8 个测试仍会在 `rc` 上看到差异 ⇒ 只解决一半。
- 推荐 **甲** (最小、不改判据、不改 cron), 但**必须等他人在途批落地后**再动, 否则是在别人正在编辑的文件上写字。

**④ `error_alert.py` 的阈值地板 3 与"单条发射器"结构不兼容 ⇒ W1~W6 违反不会推钉钉 (§7.6 更正框, 已实测)。**
不等式 `A 的单次发射量 ≥ B 的阈值` 在本例是 `1 ≥ 3` = 假。候选修法:
- **(甲) 让被守卫者按违反**逐条**发射**: `daily_invariant_check` 的 `rec()` 内即 `logger.error` 一行
  (或末尾按 key 逐条打), 则 ≥3 类违反时可推。代价: 1~2 类违反仍不可达 (**只把不可达阈值从 1 抬到 3, 没消除**);
  且 ERROR 行数从 1 变 N, 会反过来抬高该时段基线 M ⇒ 阈值自适应上升, 长期又回到不可达。
- **(乙) 给守卫类发射器开"必推"白名单** (如正文含 `[不变量自检]` / `[W4_debug_swallow` 等标记即绕过阈值,
  仍走逐指纹冷却防刷屏)。代价: 新增一条告警判据 (哪些标记算"必推"), 须预注册 + 钉子; 但这是**唯一能真正
  保证 1 条也可达**的方案, 且与 §4 的相对基线设计不冲突 (基线管的是"未知噪声突增", 白名单管的是"已知高价值单条")。
- **(丙) 降 `MIN_THRESHOLD` 到 1**: 任何单条 ERROR 都推。代价: 实测近 8 日全库 ERROR+CRITICAL 共 20 条、
  分布在多个时段 ⇒ 会把"偶发单条"全部变成夜间打扰, 与 §4 立项理由 (降噪 + 可触发) 的前一半冲突。**不推荐**。
- **(丁) 不变量自检直接走 push hub** (不经日志抓取)。代价: 就是 §9 #5 原裁决明确**不做**的"新建推送旁路",
  且会形成第二套告警口径。**不推荐**。
- 推荐 **乙**, 属告警判据变更 ⇒ 须用户裁 + 预注册判据 (含"必推标记清单"与冷却行为) 后另批落地。

---

## 10. 定稿后全量核查回执 (2026-09-10 14:40–15:35, 用户令「另外Agent已经完成不做调整了，请你核查分析」)

背景: 另一批次 (M2·A1/B1/C2, Qoder 会话) 13:41 后停手 (机器 load 由 9–14 回落 0.11), 工作区进入定稿态。
本节是对**两批合并后最终态**的独立核查, 只查证不代修 (军规: 只修本批引入的)。

### 10.1 已核实为绿
- 全量回归 `tests/`: **3739 passed / 1 failed / 41 skipped (383.6s)**; 唯一红 = §10.3-1 的日历定时炸弹, 与两批代码无关 (已用时钟对照实验证明)。
- 本批 6 个守卫/钉子文件单跑 **63 passed (11.5s)**; 对方 4 个 M2 测试文件单跑 **54 passed (3.2s)**; 脏文件 `py_compile` **35/35, 0 错**。
- W4/W5/W6 三条卫生棘轮实跑: `files_scanned=778 parse_errors=0`, **W4=703 / W5=15 / W6=1132, status 全 ok, 零违反**; 且 W4 704→703、W6 1135→1132 已低于本批 11:17 登记的基线 (对方清剿见效) ⇒ 可按"只降不升"把基线锁到 703/15/1132。
- 重启后 98min 观察窗 (13:01:47→14:40): **237 INFO / 8 WARNING / 0 ERROR / 0 CRITICAL**; 8 条逐条可归因 (scheduler_core×3 / position_sync×2 / main×1 / earnings_calendar×1 / reconciler×1)。
- 安全三面仍模拟盘: `[StartupMode] 模式一致: broker=sim db=sim ALLOW_REAL_TRADING=False` / `[交易环境] AUTO_TRADE_ACCOUNT=sim` / `[富途] env=模拟, 账户 19930823`。

### 10.2 对方定稿遗留 (核实为真, 本批不代修)
1. **`execution_state` 迁移未落生产库**: `data/ftseek_us.db::playbook_instructions` 仍 **68 列**, 无该列。迁移在 `app/playbook/db.py:126-132` 的 `ensure_tables()` 内, 而 `PlaybookDB.__init__` **不调** `ensure_tables()` ⇒ 只有 `playbook_service.py:216` / `pipeline.py:2179,2392,2592` 等构造点跑到才补列。
   **副本实证** (复制生产库后跑 `ensure_tables()`): 68→**73 列** (+`execution_state/execution_attempt/broker_order_id/broker_env/broker_order_status`), 行数 1998→1998, id 区间 (14,2459) 与 68 个共有列**逐字节相同**, `integrity_check=ok`, 表数 44→44, RFC-066 整表重建为 no-op (`UNIQUE(date,symbol)` 早已不存在), `foreign_key_check` 迁移前后同为 **9 条存量** (非本次引入) ⇒ **迁移本身无损, 可提前落**。
   回填后果实测: 状态分布 `PENDING 1498 / BOOKED 500`, 而 B1 对账过滤态 `('UNKNOWN','SUBMITTED','FILLED_UNBOOKED')` 命中 **0 行** ⇒ 不会引发对账风暴; 且 `execution_state` 全仓**只有 1 个生产消费者** (`position_sync.py:160`), 1498 条 PENDING 不会被下单链路捡起 (`try_claim_execution` 需显式 instruction_id, 由当日执行流给出)。
2. **启动冒烟守卫断言 5 永久红 — 且不是"换根针"就能修**: 对方把模式横幅 `[安全确认]`→`[StartupMode]` (`main.py:184-192`), 旧针 `模拟盘 (TRADE_ENV=1` 全仓**已无任何代码输出** (仅剩 `app/execution/engine.py:334` 注释)。更深一层: `_extract_boot_segment` 以**最后一个 `[PreFlight]`** 为界, 而本次启动 `[StartupMode] 模式一致` 在日志第 **2917** 行、`[PreFlight] OpenD 连通性检查通过` 在第 **2918** 行 ⇒ **新标记也在启动段之外**, 把针直接换成 `[StartupMode] 模式一致` 实测仍为 False。
   可行修法: ①`BOOT_MARKER` 改 `[StartupMode]` (它现在是 main.py 启动路径最早的稳定标记, 比 `[PreFlight]` 早 1ms, 且"一致/不一致/废弃告警"三个分支都会打); ②断言 5 改判**段内**的 `[交易环境]` 横幅 (`AUTO_TRADE_ACCOUNT=sim | ALLOW_REAL_TRADING=False` + `playbook.execute_mode=futu_simulate`, 实测在段内)。推荐 ①+② 并升级为**语义断言** (正则解析 broker/db/ALLOW_REAL_TRADING 三元组), 同时把"标记整体缺失"判为 exit 2 (判定无效) 而非 exit 1 (不安全), 免得下次改文案又静默解除守卫。
3. **断言 7 与 B1 缺列互相咬**: 13:02:04/13:02:09 两次 `[US Sync·B1] … no such column: execution_state` 带 Traceback 落在启动段内 ⇒ 断言 7「零 Traceback」红。**只要缺列未补, 每次重启后冒烟必红 2 项** ⇒ 重启 SOP (`systemctl restart && startup_smoke_check.py --wait 90`) 恒 exit 1, 长期后果是值守人开始忽略红字 (守卫腐化)。先落第 1 项迁移即可同时消掉这一项。
4. **`except:pass` 棘轮余量归零**: 实测 `_count_except_pass()=955` = `BASELINE=955`。归属核实: `app/core/trading_mode.py` HEAD 版 2 处 → 工作区 3 处 (**+1 来自对方 13:06 的改动**)。下一处新增即红, 建议随下一批清剿 2~3 处把 BASELINE 调低。

### 10.3 独立新发现 (存量, 与今天两批均无关)
1. **日历定时炸弹: `tests/test_stale_bridge_guard.py::TestInvariantI15Functional::test_i15_flags_stale_did_go_forward`, 自 2026-09-10 起天天红**。根因: `daily_invariant_check.main()` 的 `since = now_et().date() - 7d` (:569), I15 查询是 `WHERE date >= since AND date >= _I15_SINCE('2026-09-02')` (:672-674), 而夹具行日期**硬编码** `2026-09-02` (tests/…:321) ⇒ 今天窗口起点滑到 `2026-09-03`, 夹具行出窗, `violations={}`。
   **时钟对照实验** (同一夹具, 只改 `dic.now_et`): 真实时钟 9/10 → `window_since=2026-09-03, I15=None`; 拨回 9/9 → `2026-09-02, I15=1`; 拨回 9/5 → `2026-08-29, I15=1`。该测试 2026-09-02 立档 (commit `9790d19f`), 9/2–9/9 全绿, 9/10 起必红; `git diff` 证实今天两批**都没碰** I15 逻辑与 `since`。
   **连带损害 (假绿)**: 同类另 3 个测试 (`clean_when_did_matches` / `exempts_founding_evidence_rows` / `non_da_did_not_judgeable`) 断言的是 `"I15…" not in violations` ⇒ 出窗后**恒真**, 即使把 I15 整段删掉也照过 ⇒ I15 的测试覆盖已静默退化为"1 个正例 (现红) + 3 个空转"。修法: 夹具日期改为相对 `now_et()` 生成 (或冻结窗口), 且负例补"该行确实在扫描范围内"的作用域断言。
   对照 (均**免疫**, 不构成同类雷): `test_invariant_i10_exit_time.py` 同样硬编码 `2026-08-27`, 但 I10 走独立无窗口查询 (`WHERE exit_price>0 AND (exit_time IS NULL OR exit_time='')`, :698-701); `test_invariant_d4_settled_fields.py` 用 `dic.now_et().date()` 相对日期; `test_w1_exit_version_guard.py` 用固定 cohort 起点 `_FREEZE_COHORT_START=2026-09-03`。
2. **真钱成交通知重复推送 (今天 2/2 复现)**: 13:57:25 与 14:01:04 两笔 REAL 成交, 每笔都被 `ctx=US` 与 `ctx=HK` 两条腿各打一次 `inserted` 分支日志 ⇒ `_notify_async` 各触发两次; `.env.us:130 FTSEEK_LIVE_DEAL_PUSH_NOTIFY=1` + 飞书 webhook 在册 ⇒ **每笔真钱成交收到 2 条相同提醒**, 且 `stats["real_inserted"]` 翻倍计。
   账本本身**没有重复记账** (`live_deal_records` 每 deal_id 1 行, 今日 2 行, `UNIQUE(deal_id)` 兜住)。根因: `live_ledger.record_deal_push` (:337-343) 每次调用先 `SELECT deal_id FROM live_deal_records` 建 `existing` 快照再 upsert, 两条 SDK recv 线程相隔 ~16ms 各开连接 ⇒ 双方都在对方 commit 前读到"不存在" ⇒ `ON CONFLICT DO UPDATE` 保住了数据, 但 **inserted/updated 的判定用的是过期快照**。放大条件: 该快照是**全表扫描**, 台账越大读得越慢, 竞态窗口越宽。
   修法 (无需加锁, 跨进程亦安全): 先 `INSERT … ON CONFLICT(deal_id) DO NOTHING`, `rowcount==1` 判 inserted, 否则再跑 UPDATE 判 updated —— 让唯一约束自己仲裁, 而不是靠读快照。归属: RFC-078 批E (commit `6be1a00c`, 已提交), **非今天两批引入**; 本次是靠主日志带 `(ctx=…)` 字段才发现 (印证 §7.1 的可观测性改动值回票价)。

### 10.4 运维前瞻 (已核实到具体时刻)
- **明早 09:00 部署守卫会自动重启**: 用守卫自己的纯决策函数 `evaluate()` 实算 — `newest=app/playbook/playbook_service.py@13:38:36`, `drift=2209s`, `/run/ftseek-deploy-freshness.state` 冷却到期 **今天 15:40:19** ⇒ `9/11 09:00 → ('restart','stale_drift=2209s')`、`09:10 → restart`、`12:01 → skip(窗外)`。今天 **09:40:02 该守卫已经自动重启过一次** (日志: "部署断层自愈: 代码比进程新 7126s (最新 app/core/position_sync.py) → systemctl restart 成功") ⇒ 不是理论风险。
- **运行中进程 910996 是版本混装体**: 启动 (13:01:47) 时导入的 `main.py`(13:18 改) / `app/core/trading_mode.py`(13:06) / `app/web_dashboard/mode_switch_service.py`(13:06) 都是**旧版**, 而 `app/playbook/db.py`(12:37) / `app/core/position_sync.py`(12:49) 是**新版** ⇒ 对方 A1 (原子模式切换 + `_validate_startup_mode_or_exit`) 与 mode_switch 重写在运行进程里**并未生效**。`app/playbook/config.py` 虽 mtime 13:24 但 `git diff` 为空 (仅触碰, 内容 = HEAD, 无错配); `config/unified_config.yaml` 的唯一改动是本批的 `overnight_hold_settler_enabled: false`, 已由 13:24:44 的热重载加载。
  **侥幸不炸的一处**: 旧 `playbook_service` 有 7 处调 `release_execution_claim()` (HEAD:1525,1530,1580,1626,1917,2124), 而新 `db.py` **已删除该方法** ⇒ 若两者同时在内存, 下单失败/中止路径会 AttributeError。实测 `playbook_service` 全仓**只有函数内懒导入** (`pipeline.py:3523` + web routes), 进程 910996 至今未导入 (盘后无 auto_execute) ⇒ 今晚首次导入会拿磁盘 13:38 新版, 与新 `db.py` 匹配。**结论: 混装当前不致命, 但这是运气不是设计** —— 21:30 前任何触发 dashboard playbook 路由的访问都会把旧版服务层拉进内存与新 db 层对撞。
- **建议次序 (待用户裁, 本批不擅自执行)**: ①先落 §10.2-1 迁移 (副本已证无损) → ②修 §10.2-2 冒烟针 (①+②+语义化) → ③**受控重启** + `startup_smoke_check.py --wait 90` 求全绿 (赶在 21:30 美股开盘与明早 09:00 无人值守自愈之前) → ④锁 W4/W6 基线到 703/1132 → ⑤§10.3 两项另批处置 (I15 时钟雷 + 真钱通知重复)。

### 10.5 用户裁「需要」后的执行回执 (①②④, 2026-09-10 15:35–16:00)

用户对本节 §10.4 建议次序答「需要」⇒ 按 ①→②→④→③ 执行 (③ 受控重启放最后当验收)。

**① `execution_state` 迁移已落生产库 (先备份, WAL 安全)**
- 备份: `data/backups/ftseek_us.db.bak-20260910-b1-execution-state-migrate` (24.3 MB)。
  **不用 `cp`** — 该库是 `journal_mode=wal` 且有 86 KB 活 `-wal`, `cp` 会拿到不一致快照;
  改用 sqlite 在线备份 API (`src.backup(dst)`), 备份态 `integrity_check=ok`。
- 落地走**生产同一条代码路径** `PlaybookDB(conn=…).ensure_tables()` (`app/playbook/db.py:118`),
  不手写 ALTER, 避免与对方迁移逻辑出现第二套口径。耗时 0.05s。
- 校验 (备份 vs 迁移后逐项): 列 **68→73** (+`execution_state`/`execution_attempt`/`broker_order_id`/
  `broker_env`/`broker_order_status`); 行数 **1998→1998**, id 区间 **(14,2459)** 不变;
  **68 个共有列逐行逐字节相同 (0 行差异)**; 表数 44→44; `integrity_check=ok`; 表上**无触发器**;
  其他表行数零变化; 新索引 `idx_playbook_instructions_execution_state` 已建。
  回填结果 `PENDING 1498 / BOOKED 500`, B1 过滤态 `('UNKNOWN','SUBMITTED','FILLED_UNBOOKED')` 命中 **0 行**;
  `position_sync.py:169` 的原始查询 (逐字复制) 现返回 `OK rows=0`, 不再抛 `no such column`。
- 自查更正: 首次校验脚本曾报「共有列不同 = False」, 系**我方校验脚本自己的 bug** (拿备份的 68 列元组去比
  迁移后的 73 列元组, 长度不等) ⇒ 改用逐行逐列 id 对齐比对后为 **0 行差异**。显式记录, 不静默 (§8.5)。

**② 启动冒烟守卫断言 5 已修 (`scripts/startup_smoke_check.py`, 三处同改)**
- ①段界主标记 `BOOT_MARKER` `[PreFlight]`→**`[StartupMode]`**, 回退标记 `[安全确认]`(已死)→**`[PreFlight]`**;
  依据: `[StartupMode]` 比 `[PreFlight]` 早 1ms, 且"模式一致/不一致/废弃告警"三分支都会打 ⇒ 是最早的稳定标记。
- ②断言 5 由文案字面量 `"模拟盘 (TRADE_ENV=1" in s` 改为**正则解析语义值**, 两个独立证据源:
  `[StartupMode] 模式一致: broker=… db=… ALLOW_REAL_TRADING=…` (期望 `("sim","sim","False")`) 与
  `[交易环境] 账户路由 AUTO_TRADE_ACCOUNT=… | ALLOW_REAL_TRADING=…` (期望 `("sim","False")`);
  就位者**必须全部**判 sim (两源矛盾也判红), 下次改横幅文案不会再静默解除守卫。
- ③证据源缺席 → **exit 2「判定无效 (守卫失明 ≠ 安全)」** 而非 exit 1, 与"证据到位且确认不是模拟盘"分开报。
- 实机复核 (对 13:01:47 那段旧启动段): 断言 5 由 ✗ 转 **✓**, 失败项 2→**1** (仅剩断言 7, 因旧段内仍含
  B1 缺列 Traceback; 根因已由 ① 消除, 待 ③ 重启后新段即净)。
- 钉子 **10 例** (新文件 `tests/test_startup_smoke_check.py`, 全绿 0.33s): 健康段全绿 / **段界必须吃到排在
  `[PreFlight]` 之前的 `[StartupMode]` 行** (病根钉) / 真钱模式判红 / **两源矛盾判红** / 证据全缺→exit 2 /
  缺一个源→exit 2 / 段内 Traceback 仍判红 (断言 7 未被削弱) / 回退标记仍取到段 / 段龄超 6h→exit 2 /
  **文案字面量针不得回潮** (AST 取代码内字符串常量并排除 docstring, 禁 `TRADE_ENV=1`、`模拟盘 (TRADE_ENV`、`[安全确认]`)。
  自查: 该第 10 例初版写成"整个源文件不许出现这些串", 被本脚本**新写的历史说明 docstring** 正当命中而红 ⇒
  改为按 AST 节点身份排除 docstring (散文里讲这段历史是应该的, 被禁的是拿文案当断言针)。

**④ W4/W6 棘轮基线按「只降不升」收缩 (改判据, 故先落本档再动文件)**
- 裁决出处: 用户对本节 §10.4 建议次序答「需要」(其中 ④ = 锁基线); 纪律依据 = 基线文件自带 note
  「计数下降由人工收缩本文件, 守卫绝不自动改写」+ `tests/test_w456_log_hygiene_ratchet.py:347` 同口径。
- 实测愈合明细 (778 文件全量重扫, `parse_errors=0`, 对方定稿清剿所致):
  · W4 `debug_swallow` **704→703**: `main.py` 4→3
  · W5 `bare_logger` **15→15**: 无变化 (不动文件)
  · W6 `except_stack` **1135→1132**: `app/playbook/db.py` 5→4, `app/playbook/playbook_service.py` 35→33
- 收缩方式: `counts`/`refs` 按本次全量重扫写回 (愈合到 0 的文件从 counts 移除, 免得 `healed` 长期刷屏),
  `note`/`fix`/`deb_ref` 原文保留, 另加 `tightened_at`/`tighten_ref`/`prev_totals`/`healed_detail` 四个审计字段。
- 地板不受影响: `tests/test_w456_log_hygiene_ratchet.py:366-368` 断言 W4≥500 / W6≥900 / W5≥10, 新值 703/1132/15 仍满足。

### 10.6 ③ 受控重启回执 (2026-09-10 16:01:30 → 16:02:20, 冒烟 **7/7 全绿**)

**重启前预检 (全过才动手)**
- 脏文件 `py_compile` **37/37, 0 错**; 9 个关键模块 `importlib` 全 OK;
  `get_event_bus()`→`EventBus`、`PipelineRunner` 在位 (`app/pipeline/observers.py:77` / `app/pipeline/stages/runner.py:92`)。
- B1 状态机契约核对: `release_execution_claim` 确已移除 (预期), `try_claim_execution` 返回
  `Optional[int]`, `mark_execution_{submitted,unknown,filled_unbooked,booked}` 四件齐 ⇒ 磁盘上
  `db.py`(12:37) 与 `playbook_service.py`(13:38) 是**配套的一对**, 不再有 §10.4 的新旧对撞面。
- **模式 fail-fast 离线预跑** (对方新 `main.py:165` `_validate_startup_mode_or_exit` 会在不一致时
  `SystemExit(1)`, 故必须先离线验): `read_startup_mode_snapshot()` = `AUTO_TRADE_ACCOUNT=sim /
  TRADE_ENV=1 / ALLOW_REAL_TRADING=false / FTSEEK_DB_FILE='' / FUTU_TRADE_ENV=1` ⇒
  `validate_startup_mode_config()` → `valid=True, errors=[]`, 不会退出, 且会打出与新断言针**逐字匹配**的
  `[StartupMode] 模式一致: broker=sim db=sim ALLOW_REAL_TRADING=False`。
- 守卫组测试 37 例 (W456 棘轮 + 新冒烟钉子 + except:pass 棘轮 + WARNING 级别纪律) 全绿。
- **时机**: 排到 **16:01:30** 而非立即执行 —— 港股 16:00 收盘, `live_deal_push` 是**真钱成交被动监听**,
  重启有 ~30s 空窗; 虽有每日 `sync_deals` 兜底 (30 天回补), 但没必要在收盘前几分钟冒这个险。
  美股 21:30 开盘, 16:02 重启留有 5.5h 余量。

**重启结果**
- `systemctl restart ftseek-us` rc=0; PID **910996 → 927832**, `ExecMainStartTimestamp=16:02:00`,
  `ActiveState=active/running`, `NRestarts=0`, RSS 243 MB。
- `scripts/startup_smoke_check.py --wait 90` → **7/7 全绿, exit=0** (启动段 16:02:04)。
  这是对方把 `[安全确认]`→`[StartupMode]` 以来**第一次全绿**: 断言 5 由 ② 的语义针转绿,
  断言 7 由 ① 的迁移转绿 (段内 Traceback 0 条)。

**新启动段实测 (冒烟守卫取段 = 行 3246→3508, 263 行)**
- 分级: **243 INFO / 5 WARNING / 0 ERROR / 0 CRITICAL**; **Traceback 0 条**; `no such column` **0 次**。
  更正口径 (显式不静默, §8.5): 本节初稿写"6 WARNING / 行 3246 起"混用了两个取段偏移 —— 本次启动
  `[StartupMode]` 打了**两行** (3245 = `FUTU_TRADE_ENV=1 已废弃` WARNING, 3246 = `模式一致` INFO),
  而 `_extract_boot_segment` 用 `rfind` 取**最后一个**标记 ⇒ 段界 = 3246, 3245 那条 WARNING 在段外。
  按"本次启动全过程"口径是 **6 WARNING** (含 3245), 按"守卫判定段"口径是 **5 WARNING**; 两个数都对, 别混用。
  **已分析该 1 行盲区为良性**: 若模式不一致, `main.py:189` 打的是 `logger.critical("[StartupMode] …")`
  后 `SystemExit(1)` ⇒ 那时"最后一个 [StartupMode]"就是 critical 行本身, 段界会落在它上面, 断言 7 照样抓到;
  且 systemd 侧 `ActiveState` 也会是 failed ⇒ 双重可见, 不存在"危险分支被段界切掉"的形态。
- **① 的端到端证据**: 16:02:10,292 `[US Sync] 启动时立即执行 Broker 对账...` → 16:02:10,529
  `[US Sync] Done | Futu: 1 | 主线DB: 0 | playbook自管: 1 | 一致性: ✓` → `✅ 启动对账完成`,
  其中 B1 `_reconcile_playbook_execution_states` **零告警零栈**; 且 `US position sync started (interval=30s)`
  ⇒ 对照 13:01:47 那次同一路径抛 2 次 `no such column` (开盘后每 30s 复发, ≈780 次/日), **该噪声源已根除**。
- 6 条 WARNING 逐条可归因: `[StartupMode] FUTU_TRADE_ENV=1 已废弃` (对方刻意保留的废弃告警) /
  `bridge文件不存在 → TechnicalFallbackAdapter` (盘前 ET 09:00 才生成, 此时点属正常) /
  2× `[config-diff] 死键不一致` (min_strength=60 vs 生效键 floor=50, 以生效键为准) /
  `[财报日历] 使用静态 KEY_US_SYMBOLS fallback` / `[Reconciler·sig] 本轮签名为空但历史非空 → 跳过写入`
  (= §3 刻意保留的那个异常分支, 与 §3.4 "12→1" 口径一致)。
- 安全三面复核: `[StartupMode] 模式一致: broker=sim db=sim ALLOW_REAL_TRADING=False` /
  `[交易环境] 账户路由 AUTO_TRADE_ACCOUNT=sim | ALLOW_REAL_TRADING=False` + `playbook.execute_mode=futu_simulate` /
  `[富途] 模拟账户验证通过: 19930823` + `连接成功 127.0.0.1:11111 env=模拟`。
- **§10.4 的版本混装已消除**: A1 (`_validate_startup_mode_or_exit` + `trading_mode` 新校验) 与
  `mode_switch_service` 原子重写现已真在运行进程内; `playbook_service` 仍在首次 `auto_execute` 时懒导入,
  但磁盘与内存的 `db.py` 已同源 ⇒ 不再有旧服务层调已删方法的对撞面。

**运维前瞻复算 (守卫自身 `evaluate()` 实算)**
- `newest=app/playbook/playbook_service.py@13:38:36`, 进程启动 16:02:00 ⇒ `drift = -8603s` (代码**早于**进程)
  ⇒ `9/11 09:00 → ('ok','fresh(drift=-8603s)')`、`10:00 → ok`、`12:01 → ok`。
  **§10.4 预告的"明早 09:00 无人值守自动重启"已解除** (今天 09:40:02 那次自愈不会重演)。

**仍未处置 (登记, 不擅自执行)**
- §10.3-1 I15 日历定时炸弹 (`tests/test_stale_bridge_guard.py:321`) — 非今天两批引入, 属 9/2 立档存量, 现天天红; 连带 3 个负例空转 (假绿)。
- §10.3-2 真钱成交重复通知 (`app/web_dashboard/live_ledger.py:337`) — RFC-078 批E 存量, 今天 2/2 复现。
- `except:pass` 棘轮实测 **955 = BASELINE, 余量仍 0** (本次改动未增减该计数) ⇒ 下一处新增即红, 需一次小清剿把基线调低。
- §9.1 四项待裁 (pangan step0 残余刷新 / W456 与 8 个测试耦合 ≈+124s 的修法甲 / 告警"必推白名单" / 6 份判据基线未入库) 仍悬空。

**本批钉子累计 74 → 84** (+10 = `tests/test_startup_smoke_check.py`)。本次执行改动面:
`scripts/startup_smoke_check.py` (② 三处) + `tests/test_startup_smoke_check.py` (新) +
`data/debug_swallow_baseline.json`、`data/except_stack_baseline.json` (④ 收缩, 均被 `.gitignore:46` 忽略) +
生产库 `data/ftseek_us.db` (① 加 5 列, 备份在 `data/backups/`) + 本文档 §10.5/§10.6 + `handoff.md`。
**未提交未推送** (用户未令)。

---

### 10.7 用户令「继续」后的执行回执 (①②③ + 受控重启, 2026-09-10 16:10–17:00)

> 承接 §10.6 末尾"仍未处置"三项。次序: ② 真钱重复通知 (真钱面优先) → ① I15 日历炸弹 →
> ③ `except:pass` 小清剿 → 全量回归 → 受控重启。④ 提交**未做** (见本节末)。

**② 真钱成交重复通知 — 根因修 (`app/web_dashboard/live_ledger.py`)**
- 根因: `record_deal_push` 先 `SELECT deal_id FROM live_deal_records` 建**全表** `existing` 快照再 upsert;
  US/HK 两条 SDK recv 线程相隔 ~16ms 各建快照, 都在对方 commit 前读到"不存在" ⇒ **双方都判 `inserted`**
  ⇒ 每笔真钱成交重复推 2 条飞书 + `real_inserted` 计数翻倍。账本本身没重复记账 (`deal_id PRIMARY KEY` 兜住)。
  放大条件: 快照是全表扫描, 台账越大读得越慢 ⇒ 竞态窗越宽 (今天 2/2 复现, §10.3-2)。
- 修法: `_upsert_deal_row` 去掉 `existing` 形参 → `INSERT … ON CONFLICT(deal_id) DO NOTHING` 取
  `rowcount` 定 inserted, 撞键再补 13 列 UPDATE 定 updated (列集与旧 `DO UPDATE` **逐列一致**, 不覆写
  `deal_market/fee/counter_broker_*`) ⇒ **让唯一约束自己仲裁, 不靠读快照**; 跨线程/跨进程均安全, 无需加锁。
  - 陷阱留档: 单条 `ON CONFLICT DO UPDATE` 的 `rowcount` 在"新增"与"覆写"下**都是 1** ⇒ 区分不了
    (`sync_deals` 首版栽在这里), 故必须 DO NOTHING + 二次 UPDATE 两段式。
- 调用点同步: `record_deal_push` / `sync_deals` / `tests/test_rfc078e_live_deal_push.py:106`。
- 钉子 **+3**: barrier 双线程两腿竞态只得 1 个 inserted / 双 recv 只推 1 条 / `_upsert_deal_row` 签名
  不得再有快照形参 (结构钉, 防"顺手把快照加回来")。
- **钉子咬得住旧实现 (实测非推断)**: 临时换回旧版 (`/tmp/ll.orig.py`) ⇒ 3 枚全红 (两腿都判 inserted、
  `sent == [deal, deal]`); 换回修复版 (`/tmp/ll.fixed.py`, sha1 `c520f11c55fa040a`) ⇒ RFC-078 台账/推送
  4 文件 **95 passed**。
- 未动 (登记): `live_round_trips` 同族快照读 (`live_ledger.py:1049`) — 剪枝逻辑**真需要 pre-image**, 属合法, 不修。

**① I15 日历炸弹 — 根因修 (`tests/test_stale_bridge_guard.py`, §10.3-1 存量)**
- 病灶: 尺子窗口 `since = now_et()-7d` (`scripts/daily_invariant_check.py:569`) 是**滑动窗**, 而夹具日期是
  硬编码常量 (09-01/09-02)。9/10 起窗口滑到 09-03 ⇒ 夹具行整体出窗: 正例天天恒红 (假阳"陈旧 did"),
  3 个负例**恒真空转** (行根本没被扫到, "不告"是假绿)。
- 取证 (临时探针, 跑完即删): 不冻结时钟实跑 ⇒ `window_since=2026-09-03, checked_at=2026-09-10, I15=None`
  ⇒ 炸弹今天确实在咬, 且**四个用例同时失去判据意义** (1 红 + 3 假绿)。
- 修法: `_run_invariant(..., frozen_now=None)` → `monkeypatch.setattr(dic, "now_et", lambda: frozen_now)`;
  I15 组统一冻结 `_I15_NOW = 2026-09-05 12:00` ⇒ `window_since = 2026-08-29`, 夹具日全在窗内,
  判定只由 `_I15_SINCE` floor 与 did 日期决定 = 尺子本意。
- **防空转三件** (缺一即可能再次静默腐坏):
  1. 每例断言 `_assert_window_covers(res, 夹具日…)` = `checked_at` 以冻结日起头 (证明 patch 真生效)
     + `window_since <= 夹具日` (证明行真在窗内);
  2. 豁免例额外断言 `"2026-09-01" < _i15_floor()` ⇒ 豁免来自 `_I15_SINCE` floor 而**非**窗口滑过;
  3. 结构钉 `TestInvariantI15ClockFrozen` **+2**: 本组 `_run_invariant(` 出现次数 == `frozen_now=_I15_NOW`
     次数 (新增用例忘冻结即红) + `_assert_window_covers(` 次数 ≥ 用例数。
- 结果: 该文件 **31 passed** (原 30 passed / 1 failed)。

**①附 独立扫出的同族第二处 — I10 harness (`tests/test_invariant_i10_exit_time.py`)**
- 扫描面: 全仓 4 个 exec `daily_invariant_check` 的 harness。`test_invariant_d4_settled_fields.py` 与
  `test_attribution_watch_retirement.py` 夹具日取 `dic.now_et().date()` ⇒ **随钟滑动, 无炸弹**;
  `test_w1_exit_version_guard.py` 用固定常量窗 (`_FREEZE_WINDOW_START/_FREEZE_COHORT_START`, 查询不含
  `now_et`) ⇒ 无炸弹; **只有 I10 harness 与 I15 同款硬编码** (`trade_date = "2026-08-27"`)。
- 为何它没红: I10 自身的独立查询**不带窗口过滤** (`WHERE exit_price>0 AND (exit_time IS NULL OR …)`, 无 `since`)
  ⇒ 正例照红; 但主循环 (I1/I2/I7/I8/I9) 与 I11~I14 都带 `since` ⇒ 夹具行自 **9/4 起 (7 天)** 出窗,
  `test_clean_db_passes` 的 `rc==0` 从"物理自洽行全过"退化成**"根本没扫到"**, 该文件 docstring 自述的
  I1/I2/I7/I8 覆盖已静默失效 —— 与 I15 同族, 只是腐坏方向相反 (I15 变红, I10 变假绿)。
- 修法: 夹具日抽 `_FIXTURE_DATE` 单一真源 (`_row` 的 trade_date/entry_time 同源) + `_run()` 内冻结
  `_FROZEN_NOW = 2026-08-28 12:00`, 并把窗口覆盖断言**放进 harness `_run()` 而非逐个用例** ⇒ 新增用例
  想忘也忘不了; clean 例补 `result["sample"] == 1` 正向断言 (证明确实进了主循环, 不只是窗口对了)。
- 预实验 (临时副本 `tests/_scratch_i10_frozen.py`, 跑完即删): 冻结后 4 例 `violations`/`rc` **逐例不变**
  (`{I10:1}` / `{I10:1}` / `{}` / `{}`) ⇒ 修复只恢复判据意义, 不改任何结论; 正式文件 **4 passed**。

**③ `except:pass` 棘轮清剿 3 处 + 基线只降不升 (955 → 952)**
- 选点判据 (**不是机械删**): ①语义上真丢信息 (吞掉的是 fail-open 决策或重复告警根因) ②文件已有 logger
  (不新增 `getLogger` ⇒ 不触 W5 `bare_logger`) ③低频路径 (不制造告警风暴) ④**避开他人在途未提交批次**
  (`app/playbook/pipeline.py` 以 79 处居首, 但属高冲突面 + 热路径, 本轮不碰)。
- 三处:
  1. `app/risk/pre_trade_check.py:634` — 市场闪跌熔断检查自身异常原本静默 `pass` = 本维**放行** (fail-open),
     让"熔断未触发"与"熔断检查根本没跑成"在读数上**完全一致** ⇒ 值班人把失明读成安全态 (P0-R2 保护实际失效却零留痕)。
  2. `app/risk/pre_trade_check.py:159` — `battle_plan.market_regime` 读取失败 ⇒ DEGRADED 收紧折扣静默不应用,
     门槛留在常态档 (**弱市里反而更宽松**), 事后无从追查"今天门槛为何没收紧"。
  3. `scripts/data_freshness_watchdog.py:530` — 飞书推送成功后冷却态写失败 ⇒ 下一轮同内容**重复推**
     (本文件 2026-08-09 消除的告警风暴回归), 值班人只看得到重复告警、看不到根因。**与 ② 同族不同因**
     (② 是快照竞态, 这里是持久化失败)。
- 形式纪律: 前两处用 `logger.warning(…, exc_info=True)` (R5 模式) —— **不能用 `logger.debug`** (W4
  `debug_swallow` 棘轮把 except→debug 计为违反), 也不能省 `exc_info` (W6 `except_stack` 计违反);
  第三处随该 cron 文件既有 `print(f"[warn] …")` 惯例 (无 logger, 不为此新造命名以免触 W5)。
- **风暴复核 (实测非推断)**: 常态路径连调 3 次 `_get_effective_thresholds()` + 1 次 `_check_system_breaker()`
  ⇒ **0 条 WARNING** (只有真异常才留痕); 重启后新启动段 `fail-open` 命中 **0**。
- 基线: 清剿后实测 **952** = 新 `BASELINE` (`tests/test_except_pass_ratchet.py`, 955→952 只降不升,
  注释写明三处), `test_baseline_is_tight` 同步满足。
- 定向回归 **81 passed**: 棘轮组 + W456 卫生棘轮 + `tests/risk/test_pre_trade_check.py` +
  `tests/test_watchdog_liveness.py` + `tests/coordinator/test_p0_audit_fixes.py` +
  `tests/risk/test_earnings_exit_wiring_visible.py`。

**全量回归**
- `pytest tests/` = **3755 passed / 0 failed / 41 skipped** (403.52s)。
- 与上轮 3739/1/41 对账: **+15** = ② 的 3 枚 + ① 的 2 枚结构钉 + §10.5② 的 `tests/test_startup_smoke_check.py`
  10 枚; 且原 1 failed (I15) 转绿 ⇒ **0 failed**, 数目逐项可解释 (不是"少跑了")。

**受控重启回执 (16:55:42 → 16:56:05, 冒烟 7/7 全绿)**
- 触发理由: ② (`live_ledger.py`) 与 ③ (`pre_trade_check.py`) 都是**运行进程内的生产代码**, 不重启则修复不生效
  (正是 `deploy_freshness` 守卫要防的"部署断层"); 顺带把 drift 归负, 免明早无人值守自动重启。
- 重启前预检: 4 个改动文件 `py_compile` **0 错**; `app.risk.pre_trade_check` importlib 导入 +
  `PreTradeChecker()` 实例化 OK; 新 `exc_info=True` 站点 = 2; `_check_system_breaker()` → `(True, None)`。
- 时机: 16:55:42 CST = 港股 16:00 已收、距美股 21:30 开盘 4.5h ⇒ `live_deal_push` 真钱被动监听的 ~30s
  空窗无实盘暴露 (与 §10.6 同一条军规)。
- 结果: PID **927832 → 934161** @16:56:05, `NRestarts=0`, `active/running`, RSS **237MB**;
  `startup_smoke_check.py --wait 90` **7/7 全绿 (exit=0)**。
- 新启动段 (行 3513→3815, **303 行**): **273 INFO / 6 WARNING / 0 ERROR / 0 CRITICAL / Traceback 0 /
  `no such column` 0**; 6 条 WARNING 与 §10.6 同一可归因集 (`FUTU_TRADE_ENV=1 已废弃` / bridge 盘前才生成 /
  2× config-diff 死键 / 财报日历静态 fallback / Reconciler·sig 空签名), 且**本次段界吃到了那条废弃告警**
  ⇒ §10.6 记录的"1 行盲区"本次不存在 (段首 16:55:42 早于该告警 16:56:11)。
- 安全三面复确认: `[StartupMode] 模式一致: broker=sim db=sim ALLOW_REAL_TRADING=False` /
  `账户路由 AUTO_TRADE_ACCOUNT=sim` + `playbook.execute_mode=futu_simulate` /
  `[富途] 模拟账户验证通过: 19930823`; 且 `[实盘推送] US/HK 成交推送订阅已挂载` ⇒ **② 的修复已在运行进程内**。
- 运维前瞻复算 (守卫 `evaluate()` 实算): `newest = app/risk/pre_trade_check.py@16:46:09`, 进程启动 16:56:05
  ⇒ **drift = -596s** (代码早于进程) ⇒ `9/11 09:00 / 10:00 / 12:01` 全部 `('ok','fresh(drift=-596s)')`
  ⇒ **明早无人值守自动重启仍解除**。

**本批钉子累计 84 → 89** (+3 = ② / +2 = ① 结构钉)。本次执行改动面:
`app/web_dashboard/live_ledger.py` (②) + `tests/test_rfc078e_live_deal_push.py` (② 钉) +
`tests/test_stale_bridge_guard.py` (①) + `tests/test_invariant_i10_exit_time.py` (①附) +
`app/risk/pre_trade_check.py`、`scripts/data_freshness_watchdog.py` (③) +
`tests/test_except_pass_ratchet.py` (③ 基线) + 本文档 §10.7 + `handoff.md`。

**④ 提交 — 本轮仍未做 (需用户明确裁)**
- 工作区仍混着**他人未提交的 M2 批** (37 个脏 `.py`: `app/core/position_sync.py`、`app/playbook/*`、`main.py`、
  `app/core/trading_mode.py`、`app/web_dashboard/mode_switch_service.py` 等) ⇒ `git add -A` / 整仓提交会
  把他人代码卷进本批提交 (且有并发竞态空提交的前车之鉴 `9790d19f`)。
- 若用户裁"提交", 只能**按路径圈定**本批 8 个文件 + 2 份文档, 不得整仓; 本文档与 `handoff.md` 属本批可一并入。

**仍未处置 (登记, 不擅自执行)**
- §9.1 四项待裁照旧悬空 (pangan step0 残余刷新 / W456 与 8 个测试耦合 ≈+124s 的修法甲 / 告警"必推白名单" /
  6 份判据基线未入库)。
- `except:pass` 余量归零 (**952 = BASELINE**) ⇒ 下一处新增即红, 需持续小批清剿 (本轮已示范选点判据)。
- I15/I10 每个用例仍会真跑一次 778 文件卫生扫描 (~6s/例) = §9.1③ 已登记的测试耦合, 本轮**未顺手改**。
- `live_round_trips` 同族快照读 (`live_ledger.py:1049`) 合法 (剪枝需 pre-image), 只登记不修。
