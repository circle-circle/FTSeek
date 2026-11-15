# AUDIT-20261115 全仓根因批：已落地项与残留登记

> 触发：全仓审计（1349 py / 41.8 万行 / 3267 测试）+ 用户令「按优先级核查影响面做根因修复」。
> 纪律：只登记不擅改；会改变成交集合/仓位尺寸的项一律挂账等裁。

## 一、已落地

| 批次 | 提交 | 内容 |
|---|---|---|
| 第一批 | `1d2abda33` | ① `ExitPolicyGuardian` 死门：新增 `load_policy_dict()`（实例缓存）+ 4 处改 `get_guardian()` 单例；恢复路径钉 15%、`_get_atr_stop_mult` 读 SSOT(2.0)、下单股数路径钉现役 4.0。② `position_sizer` medium 单名帽 0.25→0.08 + 缺键默认 + `position_monitor` fallback 0.15→0.08。③ `close_flow` 时长 `to_et`。④ `intraday_indicators` 尾部窗口读（45/45 逐值等价，解析量 10.7%，0.30s→0.04s/标的）。⑤ 两个测试守卫根因修复（散文误报 / `config` 包同名遮蔽中断收集）。 |
| 第二批 | 见本文件同名提交 | `intraday_indicators` 两处**默认参数契约错误**：`now_et_provider` 默认返回 datetime 而函数体按 tzinfo 使用（6 处）；`module_file` 默认 `parents[2]` 只对 `app/playbook/pipeline.py` 成立，用本模块 `__file__` 会指到 `app/data/…` ⇒ 直接调用静默 None。统一走 `_resolve_tz_provider` / `_resolve_repo_root`。**生产 wrapper 一直注入正确 provider，故线上零影响。** |

## 二、残留登记（不擅改，需裁决/另立项）

### R-1 ATR 止损倍数两张皮（4.0 vs yaml 2.0）—— ✅ 已按用户「按建议执行」翻转
- 落地：`order_executor._get_order_qty` 改读 `get_guardian().load_policy_dict()`（yaml 现役 2.0），yaml 不可读时仍回落 4.0；`position_sync` 恢复路径同批已读 SSOT。
- **翻转是口径统一，不是尺寸变更（2026-11-15 实算，更正此前"名义额约翻倍"的说法）**：
  股数 = min(风险驱动, 分层总额 20%, 单名 8%, **单笔 5%**)；要令风险驱动低于 5% 需
  `ATR% × 倍数 > 60%`（倍数 4 ⇒ ATR%>15%；倍数 2 ⇒ ATR%>30%）。库内 **400 笔**有 ATR 的成交：
  ATR% 中位 **4.99%** / p99 6.29% / max **7.03%** ⇒ **0/400 受影响**（权益在不等式两边约掉，与账户大小无关）。
  静态不变量已钉成用例：`tests/test_position_sizer_default_parity.py::test_atr_multiplier_flip_is_sizing_invariant`。
- 预注册复核（20 个交易日后，任一变化即回滚为 4.0 并记录）：①被 5% 单笔帽截断的比例不变 ②成交笔数不变 ③费用率不变。
  **怎么量**：`grep -c "\[动态仓位·告警\].*单笔上限" logs/ftseek_us.log`（`order_executor` 已把 `sizing_result.warnings` 升到 WARNING 落盘），
  除以同期 `[动态仓位]` 行数 = 截断率；②用 `trade_records` 按 `source_type='MAIN_PIPELINE'` 计数；③费用率取 `fees_est/realized_pnl` 的桶均。
  三条全中 = 确认空操作并销案；任一变 = 回滚 4.0（`git revert` 或改回钉值）并写明触发项。
- 现役影响：`MAIN_PIPELINE` 自 2026-06-30 无新成交 ⇒ 今日零影响；**真正绑住仓位的是单笔 5% / 单名 8%**，要动仓位规模应从这两处立项，而非 ATR 倍数。
- 遗留观察（不在本项）：400 笔 ATR% 全挤在 2.5%–7%、中位恰 5%，疑似被某处 5% 上限规范化过；若后续要"用波动定风险"，先查 ATR 来源与钳制。

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