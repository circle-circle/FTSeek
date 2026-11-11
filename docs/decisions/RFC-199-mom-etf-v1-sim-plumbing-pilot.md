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
**Commit**: 未提交
**验证结果**: R13 独立复核确认 5 项 P0 属实并修复。聚焦 63 passed；相邻 125 passed；全 `tests/playbook` 初版 1189 passed / 5 failed / 3 errors（无关 WIP）。重启后启动冒烟 7/7；readiness `data/readiness/mom_etf_v1_r13.json` sha256 `ba8a78a98aba90921647f2b51edad2f5af247fe53bde1397055f133fc6bdfbdb`，聚焦 22 passed，MOM 指令/成交/持仓 0。入口开关已热重载为 true，写入 cron 已挂回；等待锚点日触发。

## R13 复核记录

- 用户转贴 P0 复查结论成立；R11 不应判 `PLUMBING_OK`。
- 修复后重新启用入口与 cron；仍只 SIM，四锁不动。
- 明日若 QQQ ≤ MA200 或数据不完整，正确空仓/跳过，记 `PLUMBING_PENDING / CASH_GATE_OK` 或 `DATA_GATE_OK`，不判策略失败。
- 收益有效性仍须 L2 同入场反事实与 L3 基准比较；本 RFC 不承诺 alpha。
