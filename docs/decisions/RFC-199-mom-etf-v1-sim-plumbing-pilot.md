# RFC-199: MOM-ETF-V1 富途 SIM 管道试点（保留 DA，单试点垂直切片）

> **状态**: 已批准 / 已实施 / 入口已开启 / 锚点日 2026-11-13 ET 待验证
> **日期**: 2026-11-12
> **作者**: Codex
> **审批人**: 用户（2026-11-12「批准执行，明日交易日验证」）

---

## 摘要

在现有 DA/Playbook 链路上增加一个 `MOM-ETF-V1` 挂牌试点，不新建 broker/source_type/退出循环/预算器/绩效系统。首批 SIM 只判 `PLUMBING_OK / PLUMBING_BROKEN`，不判策略收益。DA 缺省路径零漂移；MOM 用独立 `decision_id`、独立子帽、独立退出 allowlist 与报表腿。

## 问题陈述

DA 存在高胜率但平均亏损大于平均盈利的负偏斜结构。直接放宽 DA 止损/止盈会混轴且已有反证。本试点用 ETF 中期动量 + 固定 21 交易日持有，验证“少截断赢家是否可能改善净期望”的工程可行性；收益有效性必须另做同入场反事实与基准比较。

## 提议方案

### V1 冻结参数

- 票池：`US.QQQ / US.XLE / US.XLRE / US.XLY`，版本 `etf_v1_20261112`，top 2，只多。
- 信号：`asof` = 前一交易日（信号日）；12-1 momentum = `close[asof-21] / close[asof-252] - 1`（截至 asof 的 `bars[-22]/bars[-253]`，至少 253 根），只用前一交易日收盘。
- 现金纪律：QQQ `close > MA200`；不满足则空仓，不为攒样本放宽。
- 调仓锚点：`2026-11-13`（美股 ET，北京时间 11/13 22:30 开盘），每 21 个交易日；缺数据/闸门拦截当次跳过，不补跑。
- 指令有效期：当日 09:50–14:00 ET；过期作废。
- 权重：等名义金额；单腿上限 $4,920，当日 MOM 上限 $9,840，最多 2 仓。
- 初始 SL：固定 3%；TP=0；隔夜亏损阈值 5%（现有语义，不宣称真实 gap 保护）；21 交易日到期退出。
- 出场 allowlist：`SL_INITIAL / OVERNIGHT_GAP_STOP / MAX_HOLD`；不套 DA BE/trailing/early-tight/dead-signal/PRE_CLOSE/EOD_FORCE_CLOSE。
- 费用：7/8 股 QQQ 只是成本测算；费用可行尺寸超预算即跳过，不为摊费抬票面。

### 代码接缝

- `app/playbook/pilot_policy.py`：单点 policy、日历、候选、定尺、预算、V1 退出、最终股数复核。
- `scripts/mom_etf_v1_generate.py`：调仓日写入方，`save_instruction + confirm_saved`，asof 必须等于上一交易日。
- `auto_execute_flow.py`：MOM 收回 DA 主循环，policy 分派 trigger/reprice/预算；共享报价年龄/chase/冷却/admission/TCA；执行前复检开关与 cutoff。
- `exit_monitor_flow.py`：MOM 行提前分支，只跑 V1 allowlist，不写 DA 退出影子。
- `overnight_cleanup.py`：MOM 行跳过 DA EOD 强平。
- `source_attribution.py`：新增 `MOM-ETF-V1` 报表腿。
- `settle_entry_confirm_forward.py`：`load_sim_pnl` 与 `load_fills` 排除 MOM，防 RFC-141 污染。
- `config/unified_config.yaml`：`playbook.mom_etf_v1_entries_enabled` 默认 false；只禁新仓，不影响存量 V1 退出。

### 影响范围

- 服务焦点：F1 模拟交易管道。
- 改动指标：首批只看管道断言；平均盈利/MFE/净 bps 仅观察。
- 预期变化：首个调仓簇最多 2 笔 SIM ETF 仓；DA 触发/退出/预算/KR 口径不变。

## 风险评估

### 最坏情况

- MOM 分支误伤 DA 或全局预算；V1 仓被 DA EOD 强平；21 交易日到期静默失败；费用可行尺寸顶穿子帽；现金过滤被绕过。

### 缓解措施

- DA 缺省 `policy_for=None`；MOM 分支前后 DA 路径不变。
- 全局 `overnight_hold_enabled=false` 不打开；MOM 单独 policy-aware。
- `max_hold_deadline` 显式扩大日历搜索窗口；日历失败 ERROR + 保守 `entry+29d` 兜底，不静默取消。
- reprice 后检查 MOM 子帽、全局日帽与全局购买力帽；所有风险乘数后按最终股数复核费用/最小名义。
- 四 ETF 任一数据不完整 ⇒ 当次 fail-close，不偷偷改票池。
- QQQ MA200 现金过滤写死；不满足则无指令。
- 重启前核在途出场与 WIP；重启后跑启动冒烟。

## 验证计划

```bash
./venv/bin/python -m pytest -q tests/playbook/test_pilot_policy.py tests/playbook/test_mom_etf_v1_flow.py
./venv/bin/python -m py_compile app/playbook/pilot_policy.py scripts/mom_etf_v1_generate.py
PYTHONPATH=. ./venv/bin/python scripts/mom_etf_v1_generate.py --force --dry-run
systemctl restart ftseek-us
PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90
PYTHONPATH=. ./venv/bin/python scripts/verify_mom_etf_v1_readiness.py --run-tests --json-out data/readiness/mom_etf_v1_r13.json
```

L1 管道判据：指令写入、09:50 定时触发、reprice 后 3% SL/TP=0、子帽/全局帽、持夜、V1 退出 allowlist、DA 零漂移、编号/报表、重启恢复、回滚。收益读数不足时一律标“功效不足/不可分辨”。

## 回滚计划

```bash
# 停新仓（热重载）；存量 V1 仍按 V1 退出，不回退 DA_DEFAULT
playbook.mom_etf_v1_entries_enabled: false
# 停写入方：删除 crontab 中 RFC-199 两行
# 紧急人工平仓：走既有 Playbook/DualBroker 流程，不与普通回滚混用
```

**回滚触发条件**: DA 零漂移失败、账实不一致、预算帽被穿透、V1 仓被 DA 规则误平、启动冒烟失败。

## 替代方案

- 手工 demo：不验证 scheduled/reprice/预算，且需人工清洗影子，不选。
- 通用 sleeves 框架：过度设计，第二个策略出现前不抽象。
- 直接改 DA 持有期：已有反证且混轴，不选。

## 审批

- [x] 用户批准执行与明日交易日验证
- [x] C1 唯一新入场假设名额用于本试点
- [x] KR4 存量未收缩下的单试点工程验证属知情批准；不新增影子采集
- [x] 测试覆盖完成（契约层）
- [x] 回滚 playbook 创建

## 实施记录

**实施日期**: 2026-11-12
**Commit**: `0d9d106b2` [RFC-199]（2026-11-12 06:54 CST，治理回滚点：把此前仅存在于磁盘的试点切片入库；config 只入库 `mom_etf_v1_entries_enabled: true` 一行，工作区他人 `breakeven_at_pct` 未卷入）
**验证结果**: R13 独立复核确认 5 项 P0 属实并修复。聚焦 63 passed；相邻 125 passed；全 `tests/playbook` 初版 1189 passed / 5 failed / 3 errors（无关 WIP）。重启后启动冒烟 7/7；readiness `data/readiness/mom_etf_v1_r13.json` sha256 `ba8a78a98aba90921647f2b51edad2f5af247fe53bde1397055f133fc6bdfbdb`，聚焦 22 passed，MOM 指令/成交/持仓 0。入口开关已热重载为 true，写入 cron 已挂回；等待锚点日触发。

## R13 复核记录

- 用户转贴 P0 复查结论成立；R11 不应判 `PLUMBING_OK`。
- 修复后重新启用入口与 cron；仍只 SIM，四锁不动。
- 明日若 QQQ ≤ MA200 或数据不完整，正确空仓/跳过，记 `PLUMBING_PENDING / CASH_GATE_OK` 或 `DATA_GATE_OK`，不判策略失败。
- 收益有效性仍须 L2 同入场反事实与 L3 基准比较；本 RFC 不承诺 alpha。

## R14 上线前治理核查（2026-11-12 06:54–07:10 CST，用户「按建议核查确认执行」）

### ① 回滚点（已落地）

- `0d9d106b2`：13 文件入库（6 新文件 + 6 改共享主链 + config 单行）。`git show --stat` 可核。
- 部分入库纪律：`config/unified_config.yaml` 用单 hunk patch（`git apply --cached`）只入 MOM 开关；
  工作区他人未提交的 `breakeven_at_pct: 0.5` 原样留在未暂存区，未卷入本提交。
- `handoff.md` 的存量暂存段（他人 11-11 条目删除）已 `git restore --staged` 退出 index，内容仍在磁盘；
  未带走他人工作。
- 入库后 `scripts/verify_mom_etf_v1_readiness.py`：`entries_enabled=true`、`writer_cron_mounted=true`、
  指令/成交/持仓 0/0/0、service pid 2667199（启动 06:07:11 CST，早于本提交 ⇒ **本提交零运行时代码变更**，
  仅把磁盘态纳入版本控制，无需重启）。artifact：`data/readiness/mom_etf_v1_r14_postcommit.json`。

### ② DA 共享路径回归（已跑，本批引入 0 条）

```
./venv/bin/python -m pytest tests/playbook/ tests/test_playbook_service.py tests/test_playbook_db.py -q
→ 1263 passed, 5 failed, 3 errors   (86.8s)
MOM 专项: tests/playbook/test_pilot_policy.py + test_mom_etf_v1_flow.py → 22 passed
```

红项归属（HEAD `6523ea776` worktree + 生产 DB 软链，同签名复现 ⇒ **既有红，非本批**）：

| 红项 | 数量 | 实测原因 |
|---|---|---|
| `test_live_probe_admission_exemption.py` 5 failed | 5 | 断言「SIM 1 股小额 → REJECT」，实跑 PASS 且 reasons 为空 —— 与 RFC-186c（2026-11-06 用户裁关闭费用门拦截）后的生产行为一致 ⇒ **过期测试**，需其所有者改断言 |
| `test_exit_compensation.py` 3 errors | 3 | 用例的网络边界封条抓到真实出站 socket（`173.223.87.182:443`）⇒ 环境依赖红，非逻辑红 |

> 结论：MOM 分支守卫（`policy_for(...) is None` 走原 DA 路径）未改变 DA 的 admission/execute/exit 判定；
> 上述红项在入库前的 HEAD 上逐字复现，**本批新增失败 0 条**。另：早前 readiness 快照里
> `focused_tests=null` 是**未加 `--run-tests` 的旧读数**，实际两个 MOM 测试文件存在且 22 passed。

### ③ V1 止盈线（TP）裁决回放 —— 结论：**不加 TP**（复查见下）

用户提问「是否要设止盈线，否则无法有目标」。用同一生产选股函数 `compute_mom_candidates` 做 10 年日线回放
（Futu QFQ 日线 2016-11→2026-11，信号=调仓日前一交易日收盘，入场=调仓日开盘，锚定 2026-11-13 每 21 交易日，
SL −3% / 隔夜跳空 −5% / MAX_HOLD 21 交易日，费用用生产 `fee_model.estimate_round_trip`，单腿名义 ≤$4,920）：

| 变体 | n | WR% | 均毛% | pay-/off | PF | 净$ | 净$/笔 | 净bps/笔 | maxDD% |
|---|---|---|---|---|---|---|---|---|---|
| 无 TP（现行） | 198 | 48.5 | +0.52 | 1.35 | 1.27 | 4,059 | 20.5 | **42.5** | −12.5 |
| TP 3% | 198 | 59.1 | +0.47 | 0.90 | 1.30 | 3,612 | 18.2 | 37.8 | −14.3 |
| TP 4% | 198 | 53.5 | +0.40 | 1.05 | 1.21 | 2,900 | 14.6 | 30.4 | −15.3 |
| TP 5% | 198 | 51.0 | +0.49 | 1.22 | 1.27 | 3,789 | 19.1 | 39.7 | −12.1 |
| TP 6% | 198 | 50.0 | +0.61 | 1.34 | 1.34 | 4,910 | 24.8 | 51.4 | −9.0 |
| TP 8% | 198 | 49.0 | +0.55 | 1.34 | 1.29 | 4,320 | 21.8 | 45.2 | −10.7 |
| TP 10/12/15/20% | 198 | 48.5 | ~+0.5 | 1.35 | ~1.27 | 3,896–4,059 | 19.7–20.5 | 40.8–42.5 | −12.5 |
| 追踪 5/8/10% | 198 | 48.5–49.5 | ~+0.5 | ~1.3 | ~1.27 | 3,875–4,223 | 19.6–21.3 | 40.6–44.2 | −11.4~−12.5 |

**统计裁定（按调仓日聚类的两两 bootstrap，10k 次，对照=无 TP）**：

- **所有 TP/追踪变体的 95% CI 都包含 0**。点估计最优 TP6% = +$4.30/笔，CI95 **−6.22 ~ +15.86**，P(>0)=0.775
  ⇒ **分辨不出**，不构成「TP6 更好」的证据。HP样本内挑最优=阈值购物，禁止据此改冻结参数。
- TP 3–4% **点估计为负**（−$2.26 / −$5.86 每笔，净 bps 从 42.5 掉到 37.8 / 30.4）：早截断右尾是纯损失。
- 截断/保护是两半开（TP6%：曾触及 41 笔，其中 19 笔回落到线下、22 笔继续走；回落平均少赚 2.89pp，
  继续走平均放弃 1.80pp）⇒ 无系统性优势，只是把分布压窄（maxDD −12.5%→−9.0% 是唯一稳定方向）。
- 分半稳定性：TP6 在前/后半段皆略优（+$2,634/+$2,276 vs 基准 +$1,970/+$2,089），但幅度 < 噪声宽度。

**「没有目标」的正确解法不是价格 TP，而是把评估目标写进预注册**：现行 21 交易日 + SL 兜底本身已是退出设计
（机制=少截断赢家），可度量的目标是**净 bps/笔（费用后）**，本回放基线 ≈ **42.5 bps/笔**（毛 52 − 费 9.1）。
建议首跑验收目标：`净bps/笔 > 0 且 ≥ 费用线 ~10bps 的 4 倍` 的读数在 n≥30 笔后再判，n<30 只记观察。

**适用边界（必须随结论一起读）**：① 票池是 2026 年才选定的 4 只 ETF，回放继承**选池前视偏差**（非 PIT 选池）；
② 现金过滤（QQQ>MA200）让 120 个调仓期只 99 期在场，样本集中在 2016-2026 牛段，缺一次完整熊段；
③ TP 按限价精确成交假设，真实有排队/滑点，且日线无法定同日 SL/TP 先后（本回放取 SL 先，保守）；
④ 198 笔/10 年，1 笔的杠杆约 0.5%总净额，任何 <$1,000 的净额差都在噪声内。

**行动**：V1 冻结参数 `TP=0` **不改**。若仍需下行波动控制，候选是 `trail_8`（点估计 +$0.83/笔、CI 跨 0、
maxDD 不变）或把 TP 作为**独立 RFC 的第二臂**（预注册阈值 + 对照臂），不在本试点开跑前改 V1。
