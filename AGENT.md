# FTSeek-US Agent Notes

> **多模型协作必读**：本文件（运维/架构规矩）→ `handoff.md`（进度状态）→ **`docs/specs/multi-agent-collaboration-charter.md`（协作宪章：角色协议/数据口径九律/落档强制/变更纪律）** → 相关 DEB。验证期（2026-07-24→08-29）已结束；8/31 终裁已于 8/23 作废（DEB-20260823 三判决，用户裁不 STOP、延续打磨模拟）。当前阶段（2026-10-04→10-31）按 `docs/OKR-2026-10.md`；**2026-11-01 起按已批准的 `docs/OKR-2026-11.md`（利润目标优先，主 KR=DA-only 净 bps/笔>0）**。10 月口径：34c7 单版本功效窗（KR1 已 BROKEN 终局走回滚线, KR2 n=200 攒样中）、KR3 已两腿证伪达成（10-15）、四锁+毕业门禁+用户签字前严格不切实盘（KR4）；新机制仍排队，行为变更一律影子先行（C4）。（指针 2026-10-15 修正：此前误指 OKR-2026-09，过期 15 天）

## 运维

- 重启服务统一使用：`systemctl restart ftseek-us`
  - 不要直接用 `python main.py --stop/--start` 或 `./start.sh` 手动重启。
  - Python 源码（`.py`）修改后必须重启才生效；热重载器仅监控 `config/unified_config.yaml` 配置值，不重载代码。
- **重启按需，不设时段限制**（2026-09-18 用户裁「重启按需 不要严格限制」，否掉「会话内重启须避开 09:30–16:00」的提案）：
  - 盘中该重启就重启 —— 待生效的改动（含纯展示件）不必压到收盘后。
  - 保护靠机制不靠时段：**durable exit ledger 跨重启续上在途出场**，交易时段 `stop_auto_loop` 由安全闸拒绝（设计）。实证：2026-09-18 09:46:47 ET 会话中重启，当时 CRM/ARM 在仓、HPE/COIN 出场在途，HPE 09:48:55 正常平掉、账实一致。
  - 重启前只核两件事：①未平仓/在途出场（`SELECT symbol FROM trade_records WHERE exit_time IS NULL`）；②`git status --porcelain -- app/ scripts/` 里的改动**确实是你自己要上线的**（不许把别的 agent 的在途 WIP 顺带上线）。
  - 重启后仍必跑启动冒烟（见军规）。

## 架构：Playbook 为唯一下单/管理管线

- **Playbook 是唯一的下单与持仓管理管线**（入场 `run_playbook_auto_execute` → 出场 `monitor_playbook_sim_exits`，均经 `DualBrokerAdapter`）。
- **主管线（signal_pipeline → order_executor）已冻结新仓**，双重锁定：
  1. `signal_pipeline.scan_loop` 硬编码 `_skip_execution_in_trading=True`，候选只生成不下单；
  2. `order_executor._submit_order` source_type 白名单只放行 `PLAYBOOK_SIM/PLAYBOOK_LIVE/ORB_ENGINE`。
- **`scan_loop` 保留运行**：它不只做信号研究/IC 数据采集，还独家驱动日/周/月复盘、持仓快照、看门狗心跳、财报日历刷新——**不要 gate 掉整个线程**，否则会误伤这些 housekeeping。
- **ORB 引擎已停用**（`config/unified_config.yaml` `orb.enabled: false`），隔离良好，保留代码即可。
- `order_executor` / `position_monitor` 的退出侧与共享设施（价格获取、退出屏障、归因、`_close_position`、券商对账）仍服务遗留/手动持仓与紧急平仓，**不可删除**。

## 军规（减法R2 裁决，2026-07-26）

- **推送默认静默 (2026-10-03 清理批, REVIEW-20261003)**: 新增/修改报表推送一律走
  「无料不推」门 (待裁/守卫告警/状态变化才推, 尺子坏了 fail-open) — 先例:
  `shadow_board._board_actionable` / `pending_adjudication_carrier` (state 变化才推) /
  D2' 入场确认门日报双面自动续观静默。影子/证伪事项到期必须**当日结算入注册表**
  (判据 FAIL ≠采集自动停: 先查 `_CONSUMER_ACK`/消费方再动 cron); 在采影子未注册 = 禁止态,
  注册表是唯一 SSOT（项数以 `app/playbook/shadow_registry.py` 实载为准——此处不写具体数,
  防计数漂移成谎言; 2026-10-15 审计实测 79 项时本文仍写 69）。
- **新增推送必须走 `app/push/` hub**（去重/聚合/健康监控），禁止直连 notifier 或自写 webhook。存量 5 个旁路不动，08-29 后按"谁先出 bug 谁先迁"处理。
- **新增熔断/regime 类机制前先查分层地图**：熔断见 `docs/circuit-breaker-map.md`（6 层正交，禁造平行层）；regime 合一待 shadow 分歧率数据（减法R2-5c）。
- **归档/删除前的零引用验证必须覆盖五个调用面**：仓内 import / crontab / systemd / **Hermes prompt 文本**（`/root/.hermes/cron/jobs.json`）/ **`.git/hooks`**（2026-09-06 实证：`check_undefined_names.py` 被 pre-commit §5.5 引用，却在减法 R2 被当零引用归档，门禁静默失效 ~40 天）—— archive/ 目录必须零 import。
- **`auto_trading_enabled` 的三处裸 regex 读磁盘是刻意设计**（绕过热重载 60s 延迟的紧急开关语义），勿"优化"成 get_dotted。
- **重启必跑启动冒烟**（2026-07-26，base.* 事故教训）：`systemctl restart ftseek-us && PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` — 七条机器断言（ConfigValidator/漂移检测/热重载/SSOT 审计/模拟盘/快循环/零 Traceback），禁止用临场 grep 代替（"服务已启动"≠健康，ConfigGov 降级运行就是这么漏的）。删 yaml 段的零引用验证须含隐性消费面：validator REQUIRED_KEYS / config_audit _AUDIT_ITEMS / CRITICAL_KEYS。
- **新增落库字段必带基数自检**（2026-07-27，"常量退化"已三例：DA conf 罚成 35 / UE conf cap=50 / UE info_source 恒 `LLM_EVENT_REASONING`）：字段落了库但取值退化为常量 → 一切下游切片零区分度且表面无恙。新字段的判分消费端（scorecard 等）必须带 distinct 计数体检，n≥10 且 distinct<2 即 WARNING。参照 `direction_hit_scorecard.py` info_channel 基数自检。
- **新增落库字段必带单位/量纲声明 + 契约测试断言**（2026-09-02，"单位混装"已四案：ATR 百分比直落绝对槽 / da_glm_shadow 异基 taint 误杀 / KR2 快照漏键假绿 / UE conf 口径）：新字段入库时单位（绝对价|百分比|计数|枚举）必须写进契约测试断言；同一字段存在新旧双格式（如 atr_source）必须逐格式有契约例；读取端注释声明量纲假设。与上一条"基数自检"配套：基数防常量退化，单位防量纲说谎。
- **prompt 字段消费方登记制**（2026-09-06，PROPOSAL-20260905 P7，用户裁"直接按建议执行"）：每要求一个 LLM 输出字段，必须登记其 live 消费方（哪个闸门/指标/报表读它）+ 落库基数自检（`scripts/shadow_field_cardinality.py`）；**无消费方的字段一律删**——write-only 是 prompt 膨胀主因（实证：DA entry_archetype/confirmation/tripwires 路由关闭时纯写入；UE self_tier 有区分度却零消费于闸门，直到 U3 tier 闸才接上）。新 prompt 字段没有登记消费方 = 不许进契约。与上两条配套：消费方登记防"写了没人用"，基数自检防"用了但塌缩"，量纲声明防"用错尺子"。基数自检已接每晚不变量自检 **W2 棘轮**（2026-09-06，用户裁「按建议执行」）：`scripts/daily_invariant_check.py::_check_field_cardinality`（cron `05 5 * * 2-6`，违反 → error_alert.py 推钉钉 + exit 1），判据基线 `data/field_cardinality_baseline.json`（6 个已知 DEGEN 逐条带 refs）——基线外新增塌缩字段**当天告警**，已知病灶 `silenced_known` 静默计数，愈合只进 `healed` **不自动改写基线**（棘轮只降不升；改基线=改判据，须先落档 ADR/DEB），缺表按旧库形态静默跳过而真故障落 `W2_detector_error`。钉子 `tests/test_w2_field_cardinality_ratchet.py`（8 例）。
