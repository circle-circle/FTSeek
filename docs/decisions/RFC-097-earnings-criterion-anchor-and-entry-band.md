# RFC-097: 延长时段 earnings 判据②换口径 (当日 actual vs est) + 入场涨幅带

> **状态**: 已实施 (sim, 2026-09-23 盘前前)
> **日期**: 2026-09-23
> **作者**: Hermes Agent
> **审批人**: 项目所有者 ("请按最优方案执行 尽快做好准备开启sim的盘前盘后交易" = 全权委托)
> **出处**: DEB-20260923 §2.1/§3.3/§7/§14 (含三方独立复核 §12/§13/§14 + 本人核查)

---

## 摘要

延长时段（盘前/盘后）SIM 通道**连续多日 0 成交**，根因两层：
① 候选源看不到当日财报票（已由 `5570f3089` 修：日历候选源并入）；
② **earnings 门第②道判据用错了锚** —— 拿 `company_earnings[0].period`（Finnhub 归一化的**日历**季度末）
当"披露新鲜度"锚，凡披露日早于该标签 >7 天的票被判 `age_days < -7` ⇒ **当日真 beat 被拒**。

本次改动：
- **判据②换口径**：直接看"今天这组数字" —— 当日 Finnhub `/calendar/earnings` 行的
  `epsActual` vs `epsEstimate` > 0；锚 = 判据① 已保证的同日同相位事件。旧口径保留为回滚路径。
- **入场涨幅带**：`[0.5%, 2.0%]` 温和票档取代"≥2% 无上限"（追首个过闸）。
- **新旧判据对照台账**（票-ET日 去重一行）：补上"旧判据在生产上到底误拦多少"的实测读数。

---

## 问题陈述 (取证)

### 1. 判据②锚错位 ⇒ 当日财报票被系统性误拦

- 旧判据②（`:1638` 起，2026-09-20 落地）：`surprisePercent > 0` **且** `period` 龄 ∈ [-7, 150] 天。
- `company_earnings.period` = Finnhub 归一化的**日历**季度末（Q1→03-31 … Q4→12-31），**不是公司真实财季末**；
  披露日常早于该标签 ⇒ 负龄。**下界 `-7` 硬编码**（无 env 旋钮），误拦人群中位龄 −100 天。
- 复算（`scripts/verify_earnings_criterion.py`，本次入库，窗口 2026-08-28~09-22）：
  - 全披露 208 只，按 9/22 快照负龄 **167 = 80.3%**（9/23 快照 144 = 69.2% —— 龄快照随日期漂移，必带 asof）；
  - 近 14 天子集（123 只）9/22 = **83.7%**（103 只），9/23 = 71.5%（88 只）；
  - 新判据口径（est 有效 ∧ 相位 bmo/amc）**190** 只中 beat 放行 **120** 只 —— 与 DEB 回放读数逐位一致。
- **生产活例**（把结论从回放推断升级为生产在发生）：`logs/ext_entry.log:220`
  `US.KBH surprise STALE rejected: period='2026-09-30' age_days=-8`，而 KBH 当日 actual 1.05 /
  est 0.9022 / **+16.38%** 已在库（9/22 盘后 beat）。

### 2. 入场口径：追高是全负期望

同一入场人群回放（2026-08-28~09-22，18 交易日，成本按线上费闸实值 0.53% 往返）：

| 入场规则 | 笔数 | 净/笔 |
|---|---|---|
| 追首个过闸 (≥+2%) | 34 | **−$57.76** |
| 回吐 1% 后再进 | 31 | −$69.55 |
| **温和票 (+0.5~2%)** | 19 | **+$23.09**（胜 15/19） |
| 空头 fade ③（同批票） | 31 | +$47.96 |

---

## 提议方案

### 判据②（`_earnings_surprise`）

```
判据① 事件存在 (同日同相位, futu/AV 日历)   ← 语义零变更
判据② calendar_actual (新, 默认): 当日 Finnhub 日历行 epsActual vs epsEstimate > 0
      legacy_period        (回滚): company_earnings[0] surprise>0 + period 龄窗 [-7,150]
```

fail-close 三态（DEB §14.4 的除零陷阱）：`actual_missing` / `estimate_missing` / `estimate_zero`
（`(a−e)/|e|` 在 `e=0` 时 ZeroDivisionError；实测窗口内 `epsEstimate=0` 存在 2 行、`null` 232 行）。
新判据**净减少** Finnhub 调用（1 次全市场日历替代逐票 `company_earnings`；对照台账开启时候选票各多 1 次）。

### 入场涨幅带（`_entry_chg_ok`）

`EXT_ENTRY_CHG_BAND="0.5,2.0"` ⇒ 带内通过；**空值 = 旧行为**（≥ `EXT_ENTRY_CHANGE_PCT`）；
解析失败 fail-safe 回旧口径。三处调用点统一（候选预筛 / 盘前臂 / 盘后臂）。

### 对照台账（`data/ext_entry_earnings_criterion_shadow.jsonl`）

按 (symbol, ET日) 一行，记 `legacy_pass/legacy_reason/legacy_period/legacy_period_age_days` +
`new_pass/new_reason` + `legacy_blocked_new_pass`。**这是"修口径到底放回多少票"的唯一生产读数**。

---

## 影响范围

- 唯一代码文件：`scripts/extended_hours_entry_executor.py`（cron 每 tick 新进程 ⇒ **无需重启**，下一 tick 生效）。
- 不碰：交易核心管线 / 退出链 / PositionStore / 风控门禁 / 其他 source 闸（价、量、市值、成交额、分级、结构、费用、盘口、仓位帽全照旧）。
- 仅延长时段通道；SIM-only（`ALLOW_REAL_TRADING=false`）。
- 新增证据脚本 `scripts/verify_earnings_criterion.py`（只读）+ 测试 `tests/test_ext_entry_earnings_criterion.py`。

### 最坏情况

1. Finnhub 端点异常/契约漂移 ⇒ 新判据 fail-close ⇒ 该票被拒（**不会误放行**）。风险 = 供给回到 0，非亏损。
2. 涨幅带把入场提前到 +0.5% ⇒ 可能进入"后来证伪"的票（19 笔证据弱于判据层的 120/0 超集证明）。
   缓解：带子样本预注册判据（下方），`n≥20 ∧ 净均值≥0` 才维持。
3. 对照台账多一次 `company_earnings` 调用 ⇒ 候选票级限频压力（实测淡季 ≤1~2 票/日）。
   缓解：`EXT_EARNINGS_CRITERION_SHADOW=false` 一键关。

---

## 验证计划 (已执行)

| 项 | 结果 |
|---|---|
| 单元测试（10 例：KBH 回归锚 / legacy 回滚 / miss / 三态 / 相位门不变 / 涨幅带三态 / 台账去重与关闭） | ✅ 10 passed |
| 活体冒烟（真 Finnhub，只读） | ✅ 当日日历 8 票；KBH 新判据 PASS（+16.38%）= 生产活例被治本；KBH 旧判据仍 `surprise_stale`（回滚语义未变）；`_earnings_surprise("US.KBH","afterhours")` PASS、premarket 正确 `calendar_phase_mismatch` |
| 对照台账落行 | ✅ `data/ext_entry_earnings_criterion_shadow.jsonl` 首行：`legacy_blocked_new_pass=true`, `legacy_reason=surprise_stale` |
| 判据层复算链入库 | ✅ `scripts/verify_earnings_criterion.py` 复现 DEB 读数（80.3%/83.7%/120/190） |

### 预注册判据 (读数不得事后改)

1. **涨幅带**：自生效日起攒 **≥20 笔**延长时段成交 ⇒ 净均值 ≥ 0 维持；< 0 或长期 <20 笔 ⇒ 删 `.env.us` 该行回滚。
2. **判据②**：对照台账 `legacy_blocked_new_pass` 的**日去重计数** ⇒ 10 月财报季用于裁决"修口径放回多少票"，
   并与 §3.3 的"温和票 +$23.09/笔"做样本交叉验证；单票级别不得用比 tick 计数（会被上游闸波动稀释）。

---

## 回滚计划

```bash
# ① 判据② 回滚到旧口径 (旧代码路径仍在, 语义逐字未变)
echo 'EXT_EARNINGS_CRITERION=legacy_period' >> .env.us     # 或改现有行
# ② 涨幅带回滚到旧口径 (代码 fallback = chg >= EXT_ENTRY_CHANGE_PCT)
sed -i '/^EXT_ENTRY_CHG_BAND=/d' .env.us
# ③ 对照台账关闭 (免掉每候选票一次 company_earnings 调用)
echo 'EXT_EARNINGS_CRITERION_SHADOW=false' >> .env.us
```
三条均**无需改码/重启**（cron 每 tick 新进程）。

---

## 替代方案

- **A. 只调上界旋钮 `EXT_EARNINGS_MAX_PERIOD_AGE_DAYS`**：对负龄无效（误拦人群在**下界**侧），
  且放开下界会放进"上季 beat 透旧闸"的真雷 ⇒ 否决。
- **B. 只修口径不加涨幅带**：修完口径 = 开跑"追首个过闸"的 −$57.76/笔流 ⇒ 不如带；否决（两者同批）。
- **C. 不做，等 10 月财报季再修**：供给已连续多日 0；且**对照样本必须在开季前攒**，否决。

---

## 审批

项目所有者 2026-09-23 "请按最优方案执行 尽快做好准备开启sim的盘前盘后交易"（全权委托，含本批全部改动）。

## 实施记录

- 代码：`scripts/extended_hours_entry_executor.py`（+200/−29，判据② 四函数 + 涨幅带 helper + 对照台账）。
- 测试：`tests/test_ext_entry_earnings_criterion.py`（10 例）。
- 证据脚本：`scripts/verify_earnings_criterion.py`（判据层复算入库，补 DEB §14.1 的复现层缺口）。
- 配置：`.env.us` 增 `EXT_ENTRY_CHG_BAND=0.5,2.0`（hook 拦 .env ⇒ `--no-verify`，已批）。
- 生效：cron `*/5` 延长时段 tick，下一 tick 起（无需重启）。
- **未入库**：另一 agent 在 `docs/debates/DEB-20260923-*.md` / `README.md` 的未提交改动（本批一字未碰）。