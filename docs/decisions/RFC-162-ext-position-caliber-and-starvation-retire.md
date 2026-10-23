# RFC-162: 延长时段研究面的「行级 / 持仓级」双轨读数 + 两条输入断供影子退役

> **状态**: 已落地 (2026-10-23)，用户裁「按合理性原则建议执行」
> **类型**: L1 测量口径修复 + 环路退役 (**零交易行为变更**：不动任何入场/出场/阈值/判决字段)
> **驱动**: 盘前/盘后研究面读数被逐分钟采样行放大 100+ 倍，行级与持仓级**符号相反**；
> 两条提案臂因上游计划层 10-15 关停而静默饿死
> **载体**: `scripts/settle_extended_hours.py` (既有 cron `30 6 * * 2-6`，不新增 cron)
> **契约测试**: `tests/test_settle_ext_position_caliber.py` (9 例)

---

## 1. 问题一：n 轴是「分钟行」，不是「决策」

`premarket_exit_shadow.jsonl` / `afterhours_exit_shadow.jsonl` 是**逐分钟**采样器。结算台账
把它当样本量 ⇒ 实测放大两个数量级：

| 面 | 行级 n | 持仓级 n（distinct 源/日/标的） | 放大 |
|---|---|---|---|
| 全部已结算 | 25,797 | **137** | 188× |
| 盘前退出 | 14,268 | 80 | 178× |
| 盘后退出 | 11,529 | 57 | 202× |
| B21 执行类 | 350 | **29** | 12.1× |

后果不是"数字难看"，是**结论会翻**：

| 口径 | 均值 | 正占比 | 读起来像 |
|---|---|---|---|
| 行级 (旧) | **+0.600%** | 31.8% | 「卖出后还有上行，该放宽持夜」 |
| 持仓级 (新) | **−2.28%** | 31% | 「卖得没问题，维持触线即出」 |

来源可定位到单笔：`premarket_exit:SL_TRAIL_LOCK` 行级 54 行 (+5.031%, 正占比 0.91)，其中
**49 行 = US.ACN 2026-10-02 同一笔的逐分钟采样**；该组持仓级 n=6、均值 **−1.551%**、
正占比 0.167 ⇒ 一笔大仓即可把均值翻转符号。

## 2. 问题二：⑤⑥ 两条提案臂被上游断供饿死（静默）

- 10-15 用户裁「RFC-104 阶段0 FAIL 关停」→ 计划生成层 8 行 cron 全注释 ⇒
  `data/loops/ext_entry_plan/` 计划文件停在 **2026-10-14**、`reconcile_ledger.jsonl` 停在 10-15。
- ⑤ `eth_relative_volume_gate` / ⑥ `eth_patient_maker` 的 `_load_plan_symbols()` 恒返回空集 →
  `continue` → **零行且零输出**（日志 0 字节，连 dormant 都不打）。
- 判据轴实读：⑤ `would_pass_relative=True` **0/30**（53 行 ratio 全 null，median history 0 日）；
  ⑥ 5 行 / 2 日、`net_econ` 全 null ⇒ 三轴一条都算不出。死线均为 2026-11-30，按 0.3 行/日不可达。
- 注册表原注记「产物缺失 = 刻意 dormant」前提已失效：`reconcile` PASS 已 **14 日 (≥7)** ⇒ 门控
  早已放行，缺产物是**断供**不是休眠。同批漏网实证：⑦ 已于 10-23 因同一断供退役，⑤⑥ 是其下游。

## 3. 本批做了什么

| # | 动作 | 落点 |
|---|---|---|
| 1 | 台账增 **持仓级并列读数**（`ledger_by_position`、`positive_share`、`b21_exec_settled_pos_n`、`b22_exec_aged_pos_n`、`position_axis_note`），**只印不判** | `scripts/settle_extended_hours.py` |
| 2 | 判据 action 集合提为模块级常量 `EXEC_B21/EXEC_B22` + `_pos_n()` 助手（语义逐字未变，便于契约测试） | 同上 |
| 3 | ⑤⑥ 退役：注册表 KILLED + `collection_retired` + verdict 留痕；cron 4 行注释留痕；台账两行标 ⛔ | `app/playbook/shadow_registry.py` / crontab / `docs/CONTROL-LOOP-LEDGER.md` |
| 4 | B21 verdict 补「双轨复算」：结论不变（维持触线即出），并写明行级均值会骗人 | 注册表 |
| 5 | B22 处置：**不结题**（原计划结题于分支②，实测 >120s 执行 = 1 ≠ 0）→ 记 STILL_NOT_DECIDABLE，待落 per-bucket 劣化 bps | 注册表 |
| 6 | 盘前失效 B 规则：标注可达性（51 日只 6 笔 executed，门 20 不可达；误杀 66.7%），处置留待用户裁 | 注册表 notes |

## 4. 明确不做（边界）

- **不改**判据阈值、判决字段、`count_mode`、注册表状态机语义（改 n 轴 = 重裁，需用户单独签）。
- **不改**采集频率（逐分钟采样是设计本意：它记录路径，只是不能当样本量）。
- **不删**任何数据文件；⑤⑥ 的 jsonl 与计划目录全部留档。
- 盘前/盘后执行链、退出链、入场闸一律未动。

## 5. 验证（实弹）

| 项 | 读数 |
|---|---|
| 契约测试 | `tests/test_settle_ext_position_caliber.py` = **9 passed**（含"单笔 49 行把均值翻正"与"0 不算正"两例） |
| 守卫回归 | `test_control_loop_ledger` / `test_shadow_board_guard` / `test_criteria_registry_visibility` / `test_shadow_intent_uniqueness` = **45 passed** |
| 真实台账复算 | `--dry --days 0`：B21 行级 350 / **持仓级 29**；B22 行级 101 / **持仓级 28**；`premarket_exit:SL_TRAIL_LOCK` 行级 54/+5.031%/0.907 → 持仓级 6/**−1.551%**/0.167 |
| 产物落盘 | `data/extended_hours_settle_latest.json` 已带新键（`newly_settled=0` 因该轮 `--days 0` 只重算台账，06:30 cron 下一轮恢复正常计数） |
| 注册表 | 94 条：OBSERVING 49 / KILLED 39 / SUPERSEDED 4 / GRADUATED 2；⑤⑥ `retired=True`；`refresh_counts()` 正常产出 |
| crontab | ⑤⑥ 4 行已注释（`grep '^[^#]*eth_(relative_volume_gate|patient_maker)'` = 0），其余行逐字未动 |

## 6. 回滚

- 代码/注册表：`git revert` 本提交（新增键对旧读者无害；退役条目恢复 OBSERVING 即可）。
- cron：`crontab /root/.hermes/cache/scratch/crontab_before_rfc162.txt` 或删 4 行行首 `# [退役 2026-10-23 RFC-162] `。
- 数据：无删除动作，无需回滚。

## 7. 待办（下一批，另裁）

1. **判据 n 轴正式改持仓级**（B21/B22 + 两条退出影子的 min_n 定义）— 需用户裁，建议先看一周并列读数。
2. settle 报告补 **per-bucket 成交劣化 bps**（B22 第二轴）与行级/持仓级**两口径同表**的健康看板段。
3. 盘前失效 B 规则的处置（改反事实口径 or 结题）。
4. 盘前执行侧 20% 失败（3× buy_not_filled + 1× `order_list_query` 10/30s 限频，与常驻 exit cron 抢额度）— 执行面单独排查。