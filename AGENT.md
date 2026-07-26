# FTSeek-US Agent Notes

> **多模型协作必读**：本文件（运维/架构规矩）→ `handoff.md`（进度状态）→ **`docs/specs/multi-agent-collaboration-charter.md`（协作宪章：角色协议/数据口径七律/落档强制/变更纪律）** → 相关 DEB。验证期（至 2026-08-29）只允许"修尺子/攒样本/做减法"三类动作，新机制排队 9 月。

## 运维

- 重启服务统一使用：`systemctl restart ftseek-us`
  - 不要直接用 `python main.py --stop/--start` 或 `./start.sh` 手动重启。
  - Python 源码（`.py`）修改后必须重启才生效；热重载器仅监控 `config/unified_config.yaml` 配置值，不重载代码。

## 架构：Playbook 为唯一下单/管理管线

- **Playbook 是唯一的下单与持仓管理管线**（入场 `run_playbook_auto_execute` → 出场 `monitor_playbook_sim_exits`，均经 `DualBrokerAdapter`）。
- **主管线（signal_pipeline → order_executor）已冻结新仓**，双重锁定：
  1. `signal_pipeline.scan_loop` 硬编码 `_skip_execution_in_trading=True`，候选只生成不下单；
  2. `order_executor._submit_order` source_type 白名单只放行 `PLAYBOOK_SIM/PLAYBOOK_LIVE/ORB_ENGINE`。
- **`scan_loop` 保留运行**：它不只做信号研究/IC 数据采集，还独家驱动日/周/月复盘、持仓快照、看门狗心跳、财报日历刷新——**不要 gate 掉整个线程**，否则会误伤这些 housekeeping。
- **ORB 引擎已停用**（`config/unified_config.yaml` `orb.enabled: false`），隔离良好，保留代码即可。
- `order_executor` / `position_monitor` 的退出侧与共享设施（价格获取、退出屏障、归因、`_close_position`、券商对账）仍服务遗留/手动持仓与紧急平仓，**不可删除**。

## 军规（减法R2 裁决，2026-07-26）

- **新增推送必须走 `app/push/` hub**（去重/聚合/健康监控），禁止直连 notifier 或自写 webhook。存量 5 个旁路不动，08-29 后按"谁先出 bug 谁先迁"处理。
- **新增熔断/regime 类机制前先查分层地图**：熔断见 `docs/circuit-breaker-map.md`（6 层正交，禁造平行层）；regime 合一待 shadow 分歧率数据（减法R2-5c）。
- **归档/删除前的零引用验证必须覆盖四个调用面**：仓内 import / crontab / systemd / **Hermes prompt 文本**（`/root/.hermes/cron/jobs.json`）—— archive/ 目录必须零 import。
- **`auto_trading_enabled` 的三处裸 regex 读磁盘是刻意设计**（绕过热重载 60s 延迟的紧急开关语义），勿"优化"成 get_dotted。
- **重启必跑启动冒烟**（2026-07-26，base.* 事故教训）：`systemctl restart ftseek-us && PYTHONPATH=. ./venv/bin/python scripts/startup_smoke_check.py --wait 90` — 七条机器断言（ConfigValidator/漂移检测/热重载/SSOT 审计/模拟盘/快循环/零 Traceback），禁止用临场 grep 代替（"服务已启动"≠健康，ConfigGov 降级运行就是这么漏的）。删 yaml 段的零引用验证须含隐性消费面：validator REQUIRED_KEYS / config_audit _AUDIT_ITEMS / CRITICAL_KEYS。
