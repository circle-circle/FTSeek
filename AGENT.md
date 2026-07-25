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
