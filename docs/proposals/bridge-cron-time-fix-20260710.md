# bridge cron 时间修正方案 (2026-07-10)

> 目标: 把 DA `bridge-generation` 从"收盘后 ET 16:00"改回"盘前 ET 09:00",
> 使 FTSeek 执行脑能拿到**当日、及时**的入场信号。
> ⚠️ 结论前置: **只改时间不够**, 必须与审计[8]的"盘中不重载"耦合问题一并解决,
> 否则为化妆式修复。

## 一、问题 (证据)

| 项 | 值 | 来源 |
|---|---|---|
| Hermes 作业 | `deep-analysis-bridge-generation` id=`13dde1f65703` | `/root/.hermes/cron/jobs.json` |
| 现状 cron | `0 4 * * 2-6`(北京 04:00 = **ET 16:00 收盘后**) | jobs.json expr |
| 意图 cron | **北京 21:00 / ET 09:00 盘前** | `scheduler_core.py:253` 注释 |
| 时区 | Hermes cron 用**北京时**(`next_run_at` 带 `+08:00`) | jobs.json |
| 实测 last_run | 07-11 04:35 北京 = 07-10 **16:35 ET**(收盘后) | jobs.json last_run_at |

**后果**: bridge 生成"当天 ET 日期"文件, 但要到 ET 16:00 才有; FTSeek 盘前查找
`opportunity_scan_{today_et}.json` 时该文件尚不存在 → **永远读不到当日 bridge**
(审计 `references/da-to-executor-consistency-audit-20260712.md` 断点[7])。

## 二、核心修正 (时间)

```
job 13dde1f65703 :  expr  "0 4 * * 2-6"  →  "0 21 * * 1-5"
```
- 北京 21:00 = ET 09:00(开盘 ET 09:30 前 30 分钟), 用最新盘前价定 entry/SL/TP。
- **DOW 必须同步 `2-6` → `1-5`**: 现在 2-6 是因 ET16:00 落在北京次日; 改盘前后
  ET 日与北京日为同一天 → 美股交易日 Mon-Fri = 北京 Mon-Fri。
- 应用方式(需在 `~/.hermes` 域, 我只读): 用 Hermes `cronjob` 工具 update
  job `13dde1f65703` 的 schedule.expr / schedule_display 为 `0 21 * * 1-5`,
  保持 `enabled=true`。

## 三、⚠️ 阻塞前置: 审计[8] 耦合 — 不解决则时间修正无效

- `_try_load_bridge()` **仅在 scheduler `__init__` 调一次**(`scheduler_core.py:259-260`),
  盘中/盘前**不重载**; dashboard 的 `_load_bridge_raw` 只用于展示, 不喂执行。
- 系统 crontab **无每日重启 FTSeek 服务**(`main.py --start` 为常驻 daemon)。
- ⇒ 即使 bridge 按 ET 09:00 正确生成, FTSeek 也不会重新加载, 除非**服务在 bridge
  生成后、开盘前重启**。

**必须二选一同时落地:**
- **A)** 加一条每日盘前重启 FTSeek 服务的 cron(bridge 生成后 ~ET 09:05、开盘 ET 09:30 前);
- **B)** FTSeek 加盘前 reload hook, 在开盘前重新调 `_try_load_bridge()` 覆盖 `voting_coordinator`。

> 推荐 B(无重启抖动、可审计), 但属 FTSeek 代码改动, 需单独 spec + sim; A 是低成本临时桥。

## 四、修正后目标时序 (北京 / ET)

```
18:00 / 06:00  DA 盘前扫描+分析      → 产 opportunity_scan / 分析
21:00 / 09:00  DA bridge-generation → 产 opportunity_scan_{today_ET}.json  ← 本次改
21:0x / 09:0x  FTSeek 加载 bridge   → voting_coordinator=BridgeVotingAdapter  (依赖③解决)
21:30 / 09:30  美股开盘
```

## 五、回滚
- 恢复 `expr` 回 `0 4 * * 2-6`、DOW 回 `2-6`、`enabled` 不变。单字段可逆, 秒级。

## 六、验证清单 (改后次一交易日)
1. `cronjob list` → job `13dde1f65703` expr=`0 21 * * 1-5`, enabled=true。
2. bridge 文件 mtime ≈ 北京 21:xx / ET 09:xx; 文件名日期 = 当日 ET。
3. FTSeek 日志出现 `[scheduler] bridge文件加载成功 (N候选)` **且在开盘前**(验证③已解决)。
4. `playbook_instructions` 当日有非 SKIP 指令, `created_at` 落在开盘前。
5. 与 DA 分析结果对齐(方向/标的一致), 无"读到隔夜旧 bridge"迹象。

## 七、应用前需确认
- **FTSeek 服务每日是否重启、几点?** 若否 → 必须先落第三节的 A 或 B, 否则本修正无效。
- **DST**: Hermes cron 固定北京时。EDT(夏)北京 21:00 = ET 09:00; EST(冬)= ET 08:00
  → 换季需微调 expr, 或接受 ±1h 漂移。
- **两个作业的 `last_delivery_error`(飞书 field validation failed)**: 属**投递摘要**失败,
  `last_status=ok` 表示作业本身跑通、bridge 应已落盘; 但建议确认落盘未受影响。

## 证据文件
- `/root/.hermes/cron/jobs.json` — job `13dde1f65703`
- `app/core/scheduler_core.py` L253 / L259-260 / L287 / L295
- `app/playbook/pipeline.py` L436 `_try_load_bridge` / L364 `opportunity_scan_`
- 审计: `~/.hermes/skills/deep-stock-analysis/references/da-to-executor-consistency-audit-20260712.md` 断点[7][8], 修复表 #5(P2)
