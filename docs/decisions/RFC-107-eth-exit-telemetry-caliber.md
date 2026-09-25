# RFC-107 — ETH 退出遥测口径修复批 (MFE/MAE 峰谷真值 + broker_remark 契约对齐) + B21 错失上行影子预注册

日期: 2026-09-25
裁决: 用户裁「按建议执行」(MU 9/25 盘前 BE 案复核回执, 三条建议 ①②③)
分类: L1 (功能修复 <50 行) + L5 (文档预注册)
交易行为变更: **零** (全部为观测/遥测口径; 唯一交易侧消费者 symbol_streak_ban 只会更准)

---

## 1. 背景 (MU 案两处 P2)

MU 9/25 04:04 ET 盘前 SL_BREAKEVEN 出场 (成交 1092.0×5, 净 +$15.67) 复核发现两处 P2 遥测口径瑕疵:

1. **推送 "MFE 0.36% capture 100%" 偏低**: `mfe_pct` 列取累积值 (bar/轮询口径, 冻结在
   `bar_covered_until`), ETH tick 不进 bar。真值: tick 峰 1092.63 (+0.4219%) / bar 峰 1092.8
   (+0.4375%) → 真 capture ≈ 83-86%, 非 100%。`time_to_mfe=9.7min` 是 9/23 早段小峰时点,
   与 9/25 盘前武装时刻不是一回事。
2. **`broker_remark_prefix` 记录与实际下单前缀不一致**: metadata 记 `ext_exit_SL_BREAKEVEN`,
   实际券商单 remark=`pbe_*`。根因: `extended_hours_exit_executor.py` 把前缀写在嵌套
   `metadata["extended_hours_exit"]["broker_remark_prefix"]`, 而 `exit_lifecycle.reserve_leg`
   只认 `decision.metadata` 的**顶层** `broker_remark_prefix` (且 intent session=ETH) →
   读不到 → 回退 `pbe_`。后果不只是观测字段: `reconcile_extended_hours_exits.py` 与
   `orphan_ext_order_watch.py` 均按 `remark.startswith("ext_exit_")` 过滤, ETH 单对
   对账环/孤儿单监测**失明**。

## 2. 修复 (最小变更)

### 2.1 `app/playbook/pipeline.py` — MFE/MAE 恒取峰谷真值 (~15 行)

`_close_trade_record` 内: 原「仅 `mfe_pct<=0` 才从 peak_price 反算」改为**恒取
max(累积值, peak_price 反算, peak_price_bar 反算)**; MAE 对称 (trough_price/trough_price_bar)。
与既有 `_realized_fav` 落袋兜底同型。`_mfe_accum` 保留累积基线。

**推送时点防混读**: `_px_extra` 组装处, 若最终 mfe > 累积值 (即峰值来自重算, 旧
`time_to_mfe` 时点已失真), `time_to_mfe`/`time_to_mfe_trading_min` 置 None →
推送不再显示 "(Xmin到峰)"。

**影响面分析** (mfe_pct/mae_pct 列消费者逐一核过):
- 交易侧唯一消费者 = `_symbol_streak_ban` (低MFE连败封禁, mfe<0.5 才计入) — mfe 更真
  只会**缩小**误封禁, 不放大;
- 其余 (推送 / shadow_board / edge_weekly_report / breakeven_shadow_settle) 皆观测面;
- 50% 量纲夹取 (`_MFE_MAE_SANE_MAX`) 位置不变, 仍在重算之后。

**MU 案回放验证**: 新逻辑得 mfe=0.4375% (bar峰1092.8), capture=83.2% (原 100%), t2m 抑制=True。

### 2.2 `scripts/extended_hours_exit_executor.py` — remark 契约对齐 (1 行 + 注释)

`metadata["broker_remark_prefix"] = f"ext_exit_{reason}"` 补顶层键; 嵌套键保留 (向后兼容)。

**功能验证** (真 ExitLedger 实例, 三向):
- ETH + 顶层键 (修后) → remark=`ext_exit_SL_BREAKEVEN*` ✓ (对账环/孤儿监测恢复可见)
- ETH + 仅嵌套键 (修前对照) → 仍 `pbe_*` (证明修复点精确, 无契约破坏)
- RTH + 顶层键 → 仍 `pbe_*` (reserve_leg 的 session=ETH 前置条件未动)

### 2.3 测试

`test_extended_hours_order_execution.py` 39 passed; `test_playbook_pipeline.py` +
`test_patient_exit.py` + `test_rfc080_system_order_remark.py` +
`test_extended_hours_reconciliation.py` 133 passed; `test_extended_hours_exit_book_repair.py` +
`test_external_close_book_reconciliation.py` + `test_rfc080_half_flip_interlock.py` +
`test_control_loop_ledger.py` 93 passed。py_compile 两文件 OK。
既有断言 `exit_decision["metadata"]["extended_hours_exit"] == {...}` 不受影响 (嵌套键原样)。

## 3. B21 预注册 (docs/ADJUDICATED-CRITERIA.md, 用户裁「按建议执行」第②条)

「延长时段利润保护出场后错失上行」影子: 样本=两条 ETH 退出影子的执行类 action 行,
结算复用既有 `settle_extended_hours.py` holdout (cron 已挂, 零新代码)。判据:
n≥20 ∧ d0_close_ret_pct 均值>费用线 ∧ ≥60% 为正 → 提案宽限窗另立 DEB+用户裁;
不过线 → 维持「触线即出」, 留档反证。首样本 = MU 9/25。
背景硬约束: 富途历史K线/快照**无盘前数据** (实测 request_history_kline 9/25 = 0 行),
tick 级盘前路径唯一持久源 = 影子 jsonl 逐分钟行, 事后无法补拉。

## 3B. B22 报价龄测量判据预注册 (用户 9/25 裁「要」, 同日追加)

「盘前 tick 稀疏」被自证伪 — 该前提来自 `premarket_exit_shadow.py` 头注释 (9/16), 实测**报价密度
是标的属性而非时段属性**:

| 证据源 | 读数 |
|---|---|
| 影子 jsonl 相邻分钟 bid 变化率 | MU 95-100% / AVGO 84% / ORCL 71% (高流动) vs MPC 9% / RIVN 11-19% (低流动) |
| 影子 jsonl `quote_age_seconds` p95 (按标的) | MU 8.6s / AVGO 26.4s / ORCL 35.4s vs RIVN 573s / MPC 1017s |
| 9/25 08:32 ET 活体探针 (快照 update_time 龄) | MU/AVGO/ORCL/RIVN <4s; MPC 12min; AEHL 21min (bid 8.82/ask 10.68) |
| 9/25 活体探针 (LV3 TICKER 60s 推送) | 每标的 1 tick 且 price=**昨收** ⇒ OpenD 当前订阅层拿不到 ETH 逐笔, **快照 pre_price 即盘前权威源** |

⇒ 撤回同日口头建议「用 LV3 订阅压缩 1min 决策延迟」(该路径实测不通); 真要提速只能加快快照轮询频率。

**B22 判据** (docs/ADJUDICATED-CRITERIA.md, 零新代码, 复用已采集的 `quote_age_seconds`):
执行类 action ∧ 带龄行 n≥20 时读数 — 龄>120s 执行 ≥1 笔且成交劣化较龄≤30s 组中位差 >20bps
→ 提案报价龄门 (另立 DEB+用户裁); 否则维持现状留档反证。

**登记时既有读数 (n 远不足, 仅参考)**: 执行类带龄行仅 4 笔, 全部 ≤30s (MU 2.2s / WDC 2.9s /
ACN 28.9s / WBD 21.9s); 龄>120s 的 586 行 **100% 是 `hold_no_stop`** (亏损侧按 9/17 裁决本就不执行)
⇒ 现状下「陈旧报价导致误执行」尚未发生过一次。风险面仅存在于未来低流动票进入利润保护区时。

**相邻分钟 executable 漂移 vs 龄桶 (n=2223 配对)**: ≤30s 桶中位漂移 2.3bps、>30s 各桶中位 0.0bps
(报价冻结 ⇒ 无变化, 而非"漂移小") — 说明龄桶无法用"价格漂移"直接量风险, 必须用**成交劣化**
(B22 判据口径), 这也是 B22 选 exit_slippage_bps 而非龄阈值本身做裁决轴的原因。

## 3C. except:pass 棘轮清剿 (跨工作流, 2026-09-25)

本批回归发现 `test_except_pass_ratchet` 红 (926 > BASELINE 925)。**逐 commit 二分归因**:
非本批引入 — 是昨晚他 agent 的 `527c39a2a` (DEB-20260924 F11 回放脚本
`scripts/exec_adverse_selection_replay.py`, +391 行) 带入 1 处 `except Exception: continue`
(审计 jsonl 坏行静默跳过)。按棘轮纪律就地清剿 (不抬基线): 坏行计数 `n_bad_json` 回传,
`main()` 完整性哨兵显式打印 + >0 时警示"结论建立在残缺样本上"。零控制流变更
(仍是 continue), 与该脚本自身的"完整性哨兵"设计同向。实跑验证: 坏行=0, T1/T2/T3 读数不变。
`load_events` 签名变更无外部调用方 (grep 全仓确认)。棘轮复测 2 passed。

## 3D. W4/W6 日志卫生棘轮清剿 (跨工作流, 2026-09-25)

全量回归 (`--basetemp=/tmp`) 另抓出 `test_w456_log_hygiene_ratchet` 红 — 同样**非本批引入**,
归因于近期他 agent 三个新文件的基线外新增 (逐文件核过, 均已提交, 无未提交改动纠缠):
- W4 (except 内 debug 吞异常) ×1: `app/playbook/market_regime.py` L366 F5 影子 pre_price 取价失败
  → 改 `logger.warning(..., exc_info=True)`。频率=regime 评估周期非热循环;「主判定不受影响」
  语义保留在消息文本, 控制流零变更 (影子失败仍跳过)。
- W6 (except 内告警无栈) ×7: `scripts/eth_edgar_source.py` (cache read/write ×2) +
  `scripts/eth_event_plan.py` (futu 未安装/futu 拉取失败/load_plan/_load_jsonl_records/
  reconcile ledger append ×5) → 全部补 `exc_info=True`, 消息文本逐字不动。

验证: py_compile 3 文件 ✓; W4/W6+except_pass 棘轮 18 passed ✓; market_regime + f5_shadow +
playbook_pipeline 相关 127 passed ✓。基线 JSON 未改写 (棘轮只降不升, 靠清剿归零)。

**已知环境性红 (登记不修, 归属他 agent 测试域)**: `tests/execution/test_exit_lifecycle.py`
在活仓 102 failed/110 errors, 但在 **HEAD~1 干净 worktree 同样红** (等基归因: 与本批零交集);
根因 = 该测试的写守卫用 `startswith('/tmp/')` 判"测试专用路径", 而本机 `TMPDIR` 指向
`/root/.hermes/cache/scratch` ⇒ pytest tmp_path 全被守卫误判为"非测试库"。实证:
`--basetemp=/tmp/pt_el` 下 **112 passed 全绿**。正确跑法 = 显式 basetemp 到 /tmp
(或改守卫认 TMPDIR, 属该测试作者的裁决范围, 不代改)。

## 4. 9/18 ACN/WBD 挂账补核 (建议③)

`extended_hours_exit_executions.jsonl` 中两笔 `exit_state=HELD / exit_order_evidence_pending`
(9/18 盘后 SL_TRAIL_LOCK) 实为**等证据中间态, 非失败**: 同晚已由历史对账补平 —
`playbook_exits.db` exit_intents 两笔均 `state=BOOKED`; trade_records id=1543 ACN
净 +$59.09 / id=1544 WBD 净 +$51.66 均已落账; 当前无非终态 intent。挂账关闭。

## 5. 部署

- `scripts/extended_hours_exit_executor.py`: cron 每分钟新进程, 下一 tick 自动生效。
- `app/playbook/pipeline.py`: 常驻服务需 `systemctl restart ftseek-us` (HotReloader 穿不透
  playbook.*)。重启前置已核: 未平仓仅 AVGO 16股 (sim, synced), 改动归属本批 3 文件。
- 回滚: `git revert` 本提交即可 (零配置/零 DB schema 变更)。
