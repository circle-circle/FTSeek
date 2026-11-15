# AUDIT-20261115 全仓根因批：已落地项与残留登记

> 触发：全仓审计（1349 py / 41.8 万行 / 3267 测试）+ 用户令「按优先级核查影响面做根因修复」。
> 纪律：只登记不擅改；会改变成交集合/仓位尺寸的项一律挂账等裁。

## 一、已落地

| 批次 | 提交 | 内容 |
|---|---|---|
| 第一批 | `1d2abda33` | ① `ExitPolicyGuardian` 死门：新增 `load_policy_dict()`（实例缓存）+ 4 处改 `get_guardian()` 单例；恢复路径钉 15%、`_get_atr_stop_mult` 读 SSOT(2.0)、下单股数路径钉现役 4.0。② `position_sizer` medium 单名帽 0.25→0.08 + 缺键默认 + `position_monitor` fallback 0.15→0.08。③ `close_flow` 时长 `to_et`。④ `intraday_indicators` 尾部窗口读（45/45 逐值等价，解析量 10.7%，0.30s→0.04s/标的）。⑤ 两个测试守卫根因修复（散文误报 / `config` 包同名遮蔽中断收集）。 |
| 第二批 | 见本文件同名提交 | `intraday_indicators` 两处**默认参数契约错误**：`now_et_provider` 默认返回 datetime 而函数体按 tzinfo 使用（6 处）；`module_file` 默认 `parents[2]` 只对 `app/playbook/pipeline.py` 成立，用本模块 `__file__` 会指到 `app/data/…` ⇒ 直接调用静默 None。统一走 `_resolve_tz_provider` / `_resolve_repo_root`。**生产 wrapper 一直注入正确 provider，故线上零影响。** |

## 二、残留登记（不擅改，需裁决/另立项）

### R-1 ATR 止损倍数两张皮（4.0 vs yaml 2.0）
- 现状：`order_executor:1553` 钉 4.0（喂 `calc_position_size` 的 `risk_per_share`）；`position_sync` 恢复路径已读 yaml=2.0。
- 翻转代价：止损距离减半 ⇒ **同 risk_amount 下名义额约翻倍**；恢复路径 `initial_sl` 从 4×ATR 收到 2×ATR，与 exit_rule 的 2×ATR 地板叠加后会在高波动桶更早触发。
- 结论：属**交易行为变更**（尺寸/退出），须预注册（S1/S2/S3 停止条件）+ 回放；`MAIN_PIPELINE` 自 2026-06-30 无新成交，现役零影响。
- 重开条件：决定重启 legacy 入场链之前必须先行裁决。

### R-2 死代码批次（约 600+ 行 + 若干整包）
- 清单：`app/strategy/registry.py` + `app/decision/strategies/*`(4) + `app/strategy/base.py`；`app/evolution/`；`app/risk/exit_reason_registry.py`；`llm_pipeline.run_open_checker`；`fundamental_filter.py`、`risk/engine.should_exit`、5 个 `ConfigKeys` 枚举、`analytics/` 空目录；`docs/debeate` 笔误软链。
- 阻塞：当前工作区有 40+ 项并行 WIP（config/playbook/tests 均在动）⇒ 删除批必须单独立项 + worktree 回滚点 + 逐条核消费方（含包 `__init__` re-export、cron/Hermes 载体、看板读取面）。
- 重开条件：并行 WIP 落定后。

### R-3 四处"止损默认"只是各子系统的默认
- `dynamic_exit.default.stop_loss -0.02`（risk_stage / stop_loss_config）/ `risk.stop_loss.default -0.06`（仅 `order_executor._get_order_qty` 读）/ `config_container MAX_POSITION_PCT` 环境默认 / `stop_loss_config` 兜底 -0.03。
- 生产持仓止损真源 = **playbook 指令（`instruction_generator` 的 vol bucket / horizon 固定 SL）+ `exit_policy.yaml`**，四者均不在该链上。
- 结论：**只登记不清理**（清它们等于给冻结旧管线做手术，风险 > 收益）。

### R-4 测试基线归属（勿再重复怀疑）
- `tests/test_except_pass_ratchet.py`：**存量红**。干净 HEAD worktree 实测 900 > 基线 899 已红；逐文件差集：`+5 scripts/verdict_outcomes_ledger.py`（他人未跟踪脚本）、`-1 position_sync`（本批删除的死 `except: pass`）、`-1 playbook_service`（他人）。
- `tests/archive/*`：`pytest.ini` 的 `norecursedirs` 已排除，不在收集面。
- `config_key_census` / `da_prompt_injection` 等 WIP 期红灯：归属并行 WIP，需其落定后复跑，不在本批。

## 三、诚实边界
- 本批**未重启服务**：运行进程启动时刻早于全部编辑 ⇒ 代码变更要等下次重启才生效，当日成交集合零变化。
- 量能曲线的尾部窗口是**交易判定输入**（放量门分母），故以 45 个真实文件逐值对撞作为验收，不以"跑通"为准。
- 已有 874 行 aware-ET 持仓时长在本批 `to_et` 改动下逐行零变化；被纠正的仅是 ≤2026-07-06 的 naive-CST 历史行。