# PROPOSAL-20260909: 实盘切换双臂路由 (Cutover Dual-Arm Routing)

> **状态**: 提案待裁决 (设计先行, 零行为改动)
> **日期**: 2026-09-09
> **起案**: Hermes (用户 9/9 问"sim 稳定盈利后切实盘自动交易, 手动+自动都属实盘, 隔离与归属统计是否就绪")
> **前置已落地**: RFC-078 批B/E (券商真值层+实时推送), RFC-080 (AUTO 认领 remark 白名单)
> **红线**: C2 "打标前严格不切实盘" — 本提案只做准备, 切换本身仍按 RFC-062 两门一梯 + 用户签字

---

## 一、目标形态 (用户设想)

```
sim 模拟稳定盈利 ──毕业门禁──> auto 切实盘 (REAL 账户自动交易)
                                │
                                ├── manual 手动实盘 (富途 APP / 看板)   ┐
                                └── auto-live 自动实盘 (playbook)      ┴─ 都归属"实盘"
sim 研究臂继续跑 (RFC-062 门2 要求 SIM 同信号并行对照)
```

归属统计要求: 实盘周报/复盘能拆 auto-live vs manual-live 两臂。

## 二、现状体检结论 (9/9 逐行审计 + 双库实证)

**已就绪 ✅**:
| 层 | 现状 | 证据 |
|---|---|---|
| broker 路由 | `AUTO_TRADE_ACCOUNT` 独立控制 sim/real, 与 DB 路由解耦 | dual_broker `_ctx_and_acc`; playbook `target_env` 全链贯通 (:1112-1147) |
| 执行链统一 | sim/live 同一代码路径只差 `target_env`, 零行为分叉 | playbook_service:1200 "无缝命门" |
| 归属打标 | `target_env=real → source_type=PLAYBOOK_LIVE, trade_mode=live` 写码已内置 | playbook_service:1239-1243 |
| 同 symbol 防撞 (正向) | ManualHoldRegistry: auto 下单前查手动持有 (双账户维度 + SSOT 回落) | playbook_service:1250 |
| AUTO 认领凭证 | RFC-080: remark 白名单 (pb_*/orb_*/OVER_LIMIT_TRIM), APP 手动单 (remark='') 不再被误认领 | position_sync `is_system_order_remark` (9/9 实弹提交) |
| 券商真值层 | live_deal_records 秒级推送 (RFC-078 批E) + 每日 sync 兜底, 与下单路径无关 | 9/9 部署验证 |
| 实盘周报 | live_weekly_review 双源 (券商真值+回合台账), 纪律齐 (不跨币种/毛口径/缺口登记) | 9/9 推送成功 |

**未就绪 ❌ (cutover 日即爆, 按严重度)**:

### 缺口1 (P0): 毕业门禁读库随 TRADE_ENV 漂移 → cutover 日自锁
`_check_graduation_gate` → `PlaybookDB()` → `db_pool["ftseek_us"]` → **TRADE_ENV 路由**。
实证: sim 库 `playbook_instructions` **1918 行** (max_date 9/8), live 库仅 **16 行** (止于 6/22)。
翻 TRADE_ENV=0 那一刻, 门禁读 live 库 → 样本≈0 → "未毕业" → 降级 sim broker 下单,
**但记录仍写 live 库** (TRADE_ENV=0) → sim 成交污染实盘账本 + 门禁永不自愈 = 双重自锁。

### 缺口2 (P0): auto-live 记录落错库 → 归属分裂
现路由下 (TRADE_ENV=1 不翻), 翻 `AUTO_TRADE_ACCOUNT=real` 后:
playbook 记录写 `paths.get_trade_records_db()` = **sim 库** (PLAYBOOK_LIVE 行落在研究库),
而手动实盘记录在 **live 库** (RFC-078 隔离) → "实盘"账本物理分裂在两个文件,
实盘周报 (读 live 库) 看不到 auto-live 臂 → 归属统计断裂。

### 缺口3 (P1): FLAT_ALL 触发用账户级净值, cutover 后含手动仓
触发判据 `broker_drawdown` 来自 capital_safety (cutover 后喂 REAL 账户 = auto+manual 合并净值);
执行只平 managed (auto) 仓。手动仓暴亏 → 触发 auto 全平。政策问题: 这是 feature 还是 bug 需用户裁决。

### 缺口4 (P1): 反向守卫缺失
auto→manual 方向有 ManualHoldRegistry; **manual→auto 方向无守卫** —
看板/APP 手动买一只 auto-live 已持有的 symbol → broker 净额合并 → open_positions
(PK=symbol 单列) 一行两主 → 仓位孤儿化。

### 缺口5 (P2): live 库 8 行 size=0 陈旧手动仓 → cutover 日永久禁 auto 交易该 8 标的
`_ssot_manual_symbols` 不过滤 size>0 (BABA/CVNA/SNDQ/MU/AAPL/NVDA/NTES/CAT, account_type=real)。
fail-safe 方向 (只少交易), 但属静默产能损失。

### 缺口6 (P2): 实盘周报不拆 auto/manual 两臂
`WHERE trade_mode='live'` 合并报。cutover 后评估"算法本身是否稳定盈利" (晋级判据) 需要拆开。

## 三、方案对比

### 方案A: 翻 TRADE_ENV=0 (单库全实盘) — **否决**
- 杀死 sim 研究臂 (IC/OKR/exit_scorecard 数据源断供), 违反 RFC-062 门2 "SIM 同信号并行" 要求
- 缺口1 自锁必须先修 (门禁 pin + 降级路由), 修完仍是单臂
- 若要保 sim 臂 → 双进程 (两个 ftseek-us 实例) → LLM 成本×2 (~400 calls/日→800), 内存 +230MB (available 1448MB 偏紧), OpenD 连接×2, cron/影子文件互相踩 → 复杂度爆炸

### 方案B: 双臂路由 (记录跟随 trade env, TRADE_ENV 保持 1) — **推荐**
```
路由规则 (新增单一解析函数, SSOT):
  playbook 记录/open_positions:  target_env=real → live 库; target_env=sim → sim 库 (现状)
  manual/dashboard:              恒 live 库 (RFC-078 已实现)
  毕业门禁:                      恒读 sim 研究库 (pin, 与 TRADE_ENV 解耦)
结果:
  live 库 = 全部实盘 (PLAYBOOK_LIVE auto 臂 + MANUAL 手动臂) → source_type 一列拆两臂 ✅
  sim 库  = 研究臂原样续跑 (对照 + IC/OKR 不断供) ✅
  降级路径: 门禁不过 → target_env=sim → 记录回 sim 库, 零污染 ✅
```
- 改动集中在 playbook 记录读写 4 个点 (入场写/退出监控读/退出写/open_positions) + 门禁 pin
- 单进程, 无新增 LLM/连接成本
- 与 RFC-078 "看板→live 库" 同构 (解析器级路由, 先例已验证)

### 方案C: 什么都不做, cutover 时手工迁移 — **否决**
缺口1 自锁是确定性的 (1918 vs 16 行实证), 手工迁移 1900+ 行指令历史 + IC 数据易错且不可回滚。

## 四、方案B 实施分解 (影子先行, 每段独立可回滚)

| 段 | 内容 | 行为变化 (现状 TRADE_ENV=1 + AUTO=sim) | 风险 |
|---|---|---|---|
| S1 | 路由 SSOT 函数 `route_playbook_db(target_env)` + 门禁 pin sim 库 + 单测 | **零** (sim 路由结果与现状全等) | L2 |
| S2 | playbook 记录 4 点接入 S1 路由 (入场/退出读/退出写/open_positions) + 契约测试 | **零** (同上) | L2 |
| S3 | 缺口4 反向守卫 (手动下单前查 auto-live 持有) + 缺口5 size>0 过滤 | 零 (cutover 前无 auto-live 仓) | L1 |
| S4 | 缺口6 周报 auto/manual 分段 (source_type 拆列) | 报表增强 | L1 |
| S5 | 缺口3 FLAT_ALL 政策 (待用户裁决后实施) | — | 政策 |
| — | **cutover 日**: AUTO_TRADE_ACCOUNT=real + ALLOW_REAL_TRADING=true (用户签字, RFC-062 门1 判据先重裁) | 切换 | L0 级预案 |

S1-S4 全部是 **cutover 前可安全预置** 的 (现状零行为变化), 符合影子先行纪律。
切换日本身仍被 C2 红线 + 毕业门禁 + ALLOW_REAL_TRADING=false 三重锁住。

## 五、留用户裁决 (2 个政策点)

1. **FLAT_ALL (缺口3)**: cutover 后账户级回撤 (含手动仓浮亏) 触发 auto 全平 —
   a) 保留账户级触发 (保守: 账户在流血就停 auto, 但手动仓亏损会连坐平掉 auto 仓)
   b) 改 managed-only 触发 (auto 只为自己的回撤负责, 手动风险隔离)
   建议 a→b 折中: **触发用账户级, 但 action 降为停新开仓 (不平已有 auto 仓)** — 手动暴亏时 auto 停止加仓即可, 全平连坐过激。
2. **sim 研究臂在 cutover 后的去留**: RFC-062 门2 试点期 (2-4 周) 必须并行; 试点转正后 sim 臂是否继续? 建议转正后降频保留 (对照价值 vs LLM 成本)。

## 六、回滚

- S1-S4 每段独立 commit, `git revert` 即回滚; 现状 (TRADE_ENV=1 + AUTO=sim) 下全部零行为变化, 回滚无状态迁移。
- cutover 日回滚 = AUTO_TRADE_ACCOUNT=sim + 重启 (既有 kill 条件同款, RFC-062)。
