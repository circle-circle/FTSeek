# Data Map — 决策链数据资产索引 (DEB-20260729-position-path-lake P1)

> **用途**: 每张表/文件一行 — 主键/粒度/写入方/消费方/口径坑。防"平行层"与口径拼接事故。
> **官方串联键**: `decision_id` (instruction→trade→path 全链, 近 7 日跨表 join 57/57 实测)。
> shadow/telemetry 类无 decision_id, 用 symbol+date 近似挂载 (decision_view._load_shadow_index)。
> **维护**: 新增决策相关存储必须在此登记, 否则视为平行层违规。

## 核心 DB 表 (data/ftseek_us.db)

| 表 | 主键/串联 | 粒度 | 写入方 | 主要消费方 | 口径坑 |
|---|---|---|---|---|---|
| playbook_instructions | decision_id | 指令/日 | DA bridge / UE inject / topup | auto_execute, decision_view, scorecard | date=ET; raw_instruction_json.source 切分三源; vol_bucket 盘前定桶任务补 |
| trade_records | decision_id, id | 成交笔 | playbook_service 记账 | 一切判分/回放/看板 | entry/exit_time=**naive ET**; peak/trough=**最有利/最不利**非高低价; mfe/mae≥0; entry_price_source 演进 (broker_deal_confirm=07-27 后真价); 07-28 前历史经 183 笔回填修真; **fees_est/pnl_net/fees_source** (2026-07-30 费用净额批: est=费率表估算, 三态只升不降; 明细在 metadata.fee_breakdown) |
| signal_snapshots (signal_snapshots.db) | decision_id | 信号 | signal_ic_tracker (过滤前全录) | IC 回填/analytics | 含被 SKIP 信号 (消选择偏差); fwd_ret T+N 到期才非 NULL |
| trade_attribution | trade_date+symbol | 笔 | 复盘归因 | trade.py 看板 | overall_score 五维 |

## JSONL 遥测 (data/)

| 文件 | 串联 | 写入方 | 消费方 | 口径坑 |
|---|---|---|---|---|
| trigger_miss_telemetry.jsonl | symbol+date | pipeline._emit_trigger_miss | 触发质量分析 | reason 枚举 (after_cutoff/reanchored/sl_crossed...) |
| high_atr_stall_shadow.jsonl | symbol+ts | 退出循环 shadow (record-only) | 08-07 裁决 | 每仓仅首触发点; n≥5 才提裁 |
| breakeven_shadow.jsonl | symbol+ts, variant A/B | 同上 | 08-07 裁决 | A 需离线回放补结局; B 可直接对比 |
| da_gate_decisions.jsonl | symbol+trade_date | DA skill signal_gate | gate 判分 | source=deep_analysis |
| exceptions.jsonl / guard_decisions.jsonl | - | 各守卫 | 告警汇总 | - |

## 路径湖 (data/position_paths/) — 本 DEB 新增

| 项 | 值 |
|---|---|
| 文件名 | `{decision_id sanitize [^A-Za-z0-9_-]→_}.json` |
| 内容 | meta (entry/exit/peak/config_version/exit_reason/vol_bucket) + 1min bars |
| bars 格式 | **[t, o, h, l, c] 五元组** (OHLC 俱全, t=ET naive 末标签) — §十二核查后显式写明, 防误读为二元组 |
| 窗口 | entry-30min → exit+30min (bar 天然止于 16:00); 隔夜仓跨日采两日 |
| 口径 | **autype=NONE** (原始价, 与成交价同口径); bar time_key=分钟**末标签** (DEB-bar-ruler) |
| 写入方 | position_path_collector.py (盘后 cron 04:35 CST, 幂等) |
| 消费方 | dd_param_scan --exact (G3), 未来退出假设检验 |
| 坑 | 只含已成交笔 (选择偏差原样); exit+30min 对尾盘平仓截断至 16:00 |

## 缓存/中间层

| 项 | 说明 | 坑 |
|---|---|---|
| data/replay_bars/US_{SYM}_{DATE}.json | 1min bar 缓存 (fetch_bars 写) | **list-of-lists** [t,o,h,l,c] 非 dict; NONE 口径 (07-28 后); collector 与 L1 回放共享 |
| data/sector_pulse.json | 盘中板块脉搏 30min | 凌晨为平盘噪声 (asof_et 判新鲜) |
| data/deep_sector_rotation.json | 日更盘前轮动 (DA scanner 产) | 消费方: UE 佐证 + DA 个股材料 (07-28 接线前零消费) |

## 读侧视图

- `scripts/decision_view.py` — decision_id 一行一决策 (状态×动作×结果×路径×shadow); G5 join 100%
- `scripts/dd_param_scan.py --exact` — 路径湖精确反事实
- 看板 `/api/analytics/new-mechanisms` — shadow 计数/板块脉搏/价源爬坡

## 🔒 裁决口径白名单 (强制, 2026-07-30 DEB-fee-net-pnl §九.4)

**任何对外统计/裁决矩阵/周报引用 trade_records 时, 必须使用正向白名单**:
```sql
source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
```
- **不得用 NOT IN 黑名单** — 费用案中作者 (515 混 MAIN_PIPELINE 退休引擎 +$23k) 与辩论方
  (191 漏 PLAYBOOK_SHADOW) 各中一次, 系"跨引擎拼接"口径事故第三次重演;
- 新引擎上线默认**不进**裁决口径, 须显式加入本白名单并在此登记;
- fees_est 等派生列可对全表回填 (无害), 但对外数字只取白名单子集。

## 已知历史口径事故 (引以为鉴)

1. §14.2 跨窗口拼接叙事 (v1/v2 混样本)
2. quote_fallback 假价 (183 笔回填修真, 07-27/28)
3. peak/trough 物理价 vs 最有利语义 (回填自引, 07-28 修)
4. 看板 slippage_pct 死键恒 0 (07-28 修)
5. CST×ET 日期错位家族 (九例, 全链已清)
