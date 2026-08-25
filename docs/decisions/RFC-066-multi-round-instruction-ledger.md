# RFC-066: 多轮指令台账 — 修复再入场重武装覆盖第一轮结算

> **状态**: 已实施 (2026-08-25)
> **日期**: 2026-08-25
> **作者**: Hermes
> **审批人**: 用户 (8/24 复盘后裁"需要修复数据质量问题 确保都真实有效 不隐藏问题")

---

## 摘要

移除 `playbook_instructions.UNIQUE(date, symbol)` 约束, 允许同一 (date, symbol)
多轮指令共存; 覆盖仲裁从 SQL `ON CONFLICT` UPSERT 下沉到 `save_instruction`
Python 层; 删除再入场重武装的毁灭性复位 SQL; 回填 8/7-8/24 被覆盖的 26 笔
第一轮台账。

## 问题陈述 (实锤数据)

盘中再入场 (`intraday_reentry._rearm`) 走 `save_instruction` UPSERT, 按
`ON CONFLICT(date, symbol)` **整行覆盖**第一轮指令行, 随后一段复位 SQL
(`intraday_reentry.py` L675-685, 已删) 把
`executed/triggered/outcome/pnl/exit链/MFE` 全部清零 →
**第一轮成交的台账物理消失**, 只剩 `metadata_json.original_instruction` 快照。

损害普查 (8/25 全历史扫描):
- **26 行断裂** (8/7-8/24 每个再入场日都发生; 25 reentry + 1 个 7/10 NVDA did 漂移)
- 漏计净 **+$58.97** (14胜12负) 在指令层
- 8/24 当天: 台账显示 10 笔执行/+$118.21, 真实 13 笔/+$150.86
- 下游凡按 `executed=1` / `outcome IN (win,loss)` 过滤的统计
  (盘中 cron、周报、因子分析、OKR Outcomes、exit_scorecard) 全部系统性少计

**钱没丢**: `trade_records` 权威成交表完好; 丢的是指令层归因。

连带陷阱: 结算 UPDATE `WHERE outcome NOT IN ('win','loss','void')` 对
outcome=NULL 行恒 NULL (falsy) → 新 INSERT 的轮次行永远无法结算 (8/17-8/21
15 行 out=NULL 实锤)。

## 方案

### 1. Schema 迁移 (`db.py::_migrate_drop_date_symbol_unique`)
- 重建表去 `UNIQUE(date, symbol)`, 行 id 保持 (trade_records 链接键)
- 专用连接 + `BEGIN IMMEDIATE` 单事务 (Python 3.8 池连接 legacy 隔离级别下
  DDL 会被隐式 COMMIT, 无法原子化)
- 崩溃恢复: `__rfc066` 孤儿表清理/转正; 列超集 + 行数双校验; 幂等
- 补 (date, symbol) 普通索引

### 2. `save_instruction` Python 层仲裁
- ① 同 decision_id → 原地更新 (幂等重跑); 已结算行永不被重写
- ② 空 decision_id (SKIP 票) → 不碰有源行
- ③ `intraday_reentry-` → **INSERT 新轮次行** (多轮共存)
- ④ 其他源: 旧优先级阶梯保留 (UE 不碰 DA); 目标行已结算 → 拒绝

### 3. 根因体删除
- `intraday_reentry.py` 复位 SQL 整段删除 (新轮次行 INSERT 天然干净)
- 硬规则: **已结算行 (executed=1 且 outcome 终态) 永不被重写**

### 4. NULL 陷阱修复 (`pipeline.py` L5764)
`WHERE id=? AND (outcome IS NULL OR outcome NOT IN ('win','loss','void'))`

### 5. 单行假设消费者适配
| 消费者 | 适配 |
|---|---|
| `db.get_instruction` | `ORDER BY id ASC LIMIT 1` (返回第一轮, 保旧语义) |
| `intraday_reentry._arm_instruction` 快照查询 | `ORDER BY id ASC LIMIT 1` |
| `intraday_reentry._is_reentry_instruction` | `ORDER BY id DESC LIMIT 1` (最新轮) |
| `llm_universe_scan` DA 优先防覆盖 | 扫描该 (date, symbol) 全部轮次行 |
| `premarket_peer_shadow` | direction 取第一轮; 结算取终态轮 |
| `gate_a_reconciliation` | 无影响 (按 iid 主键联接 + olddup_count 本就是普查多行) |

### 6. 数据回填 (`scripts/repair_round1_ledger_rfc066.py`, 已 --apply)
- A/B 类 25 行: 从快照 + trade_records 重建第一轮台账行 (新 id, 审计标记
  `rfc066_backfill_round1`), trade 的 `playbook_instruction_id` 改指新行
- C 类 1 行 (7/10 NVDA): decision_id 原地回指成交真值 (`rfc066_did_repair`)
- 回填后断裂普查: **0**; 8/24 口径: 21 指令/13 执行/13 win/净 +$150.86
  与 trade_records 逐笔一致

## 验证
- 全量测试 166 passed (含新增 `TestRFC066MultiRoundLedger` 6 条契约测试)
- `PRAGMA integrity_check` ok; 行数 1149 → 1174 (+25, 恰等于重建数)
- 服务重启后迁移幂等跳过; 盘中模拟诊断: 执行 13 条 (修复前 10)
- 双备份: `data/backups/ftseek_us_20260825_052943_*` + `*_pre_rfc066_apply.db`

## 复盘口径指引 (长期)
- 涉及再入场轮次的盈亏/胜率复盘一律以 `trade_records` 为准
- 引用指令层漏斗数字必须注明口径 (多行时代每轮一行)
- 8/7-8/24 的历史指令层统计已被回填修正; 之前产出的周报/复盘若引用指令层
  漏斗数字, 按本 RFC 口径重算
