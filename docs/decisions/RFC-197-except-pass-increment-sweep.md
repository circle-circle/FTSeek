# RFC-197 夜间两棘轮清红: except:pass 增量清剿 36 处 + W4/W6 日志卫生 16 处 (2026-11-10)

状态: **已落地** (代码 / 基线 / 本文件同批; 4 个他人未跟踪脚本内的 5 处修复留在工作区)
分类: L1 缺陷修复 (只改异常留痕, **零控制流变更**; 不改任何入场/出场/SL/TP/size/门禁取值)
关联: RFC-188 / RFC-190 (VIX) / RFC-192 / RFC-193 / RFC-195, DEB-20260831 P1③ (except:pass 棘轮),
      DEB-20260910-logging (W4/W6 日志卫生棘轮)

## 1. 触发

夜间两条棘轮同时红, 全部落在 FTSeek Bot 的近期 RFC 提交面 (非数据/交易链路故障):

| 棘轮 | 修复前 | 归属 |
|---|---|---|
| `test_except_pass_not_growing` | **935** vs 基线 905 (+30 净) | RFC-190/192/193/195 的新脚本 + `economic_calendar` / `fred_series` / `vix_monitor` / `thesis` |
| `test_w456_log_hygiene_ratchet::test_real_repo_within_registered_baseline` | 16 处基线外违反 / 14 文件 | RFC-188/190/195 |

### 1.1 ① W4/W6 日志卫生 (16 处 / 13 文件)

判据两条: W4 = except 内 `logger.debug` 吞异常 (基线外新增即红), W6 = 告警行无栈。
修法只有两种, 均零控制流变更: 告警已有文本 ⇒ **只补 `exc_info=True`** (文本逐字不动);
吞异常处是 `debug` ⇒ **`debug → warning` + `exc_info=True`**。逐文件 1–4 行, 无基线 JSON 改动。

| 文件 | 处 | 形态 |
|---|---|---|
| `playbook/instruction_generator.py` | 2 | warning 补栈 (sizing 费用下限 / trade_date 解析) |
| `playbook/pipeline_parts/exit_monitor_flow.py` | 2 | debug→warning / error 补栈 (双通道平仓异常) |
| `playbook/pipeline_parts/supply_gate.py` | 3 | warning 补栈 (bridge 读取 / 组合闸开关 / 截止时点) |
| `playbook/pipeline_parts/close_flow.py` | 1 | debug→warning (holding_hours 重算) |
| `playbook/{exit_engine,exit_staging_shadow,exit_session_window_shadow,close_alert_state,position_sync,db}.py` | 各 1 | 补栈 / debug→warning |
| `web_dashboard/data_service.py` | 2 | error 补栈 / debug→warning (VIX 详情缓存) |
| `risk/vix_monitor.py` | 3 | error 补栈 ×2 / FRED 取数 debug→warning (另 2 处属 §3 的 except:pass 清剿, 同文件两类合批) |

验证: `debug_swallow` 673→668、`except_stack` 1099→1085、hits=0; 13 个 app 模块 `import` 全 OK;
`test_real_repo_within_registered_baseline` 转绿 (整套 16 passed)。

## 2. 上层归因 (先钉"哪 36 行是新增", 不靠总数差额)

以干净 worktree @`5bec3843d` (登记基线的提交) 同口径计数对撞工作区, 得**逐文件差集**:

- 新增 **36** 处 / **23** 文件 (最大簇: `non_nominated_counterfactual` 5、
  `market_env_ledger` 3、`economic_calendar` 3、`p3_market_premise_probe` 2、
  `early_tight_acceptance_replay` 2、`exit_session_window_shadow_report` 2、
  `vix_monitor` 2、`thesis` 2, 其余 15 文件各 1);
- 顺带愈合 **6** 处 (他批: `macro_calendar_shadow_collect` −3 / `signal_context_store` −2 /
  `pre_trade_check` −1);
- 核对: `905 + 36 − 6 = 935` (修复前实测), `935 − 36 = 899` (修复后实测)。

## 3. 改法 (逐类, 零控制流)

| 类 | 手法 | 处数 |
|---|---|---|
| `scripts/*` (无模块级 logger) | `except X as exc:` + `print(f"[<脚本>] <跳过什么>: {type(exc).__name__}: {exc}", file=sys.stderr)` | 29 (19 文件) |
| `app/*` (有 logger) | `except X as exc:` + `logger.warning("<什么失败>: %s", exc, exc_info=True)` | 7 (`economic_calendar` 3 / `vix_monitor` 2 / `thesis` 2) |
| 依赖补齐 | 6 个脚本补 `import sys`; `fred_series` 补 `logger = logging.getLogger(__name__)` | — |

`pass` / `continue` 的流向一律不动; 消息文本写明"哪一行、跳过了什么"; 高频循环内的坏行只打印
类型+摘要, 正常数据下零输出 (坏数据时才响)。

### 3.1 双棘轮互踢 (本批实测, 必记)

首轮给 app 那 4 处写了 `logger.debug(..., exc_info=True)` ⇒ except:pass 棘轮绿了, **W4 立刻红**
("基线外新增 3+1 处")。W4 对**新点位**只认 `warning`。结论: 清剿期 app 侧一步到位写
`warning`; `debug` 只允许用于**已在基线内**的存量点位。scripts 的 `print(..., file=sys.stderr)`
不在 W4 判据内 (W4 只管 `logger.debug` 吞异常), 两者可共存。

## 4. 基线处置 (只降不升, 不动任何 `*_baseline.json`)

`tests/test_except_pass_ratchet.py`: `BASELINE 905 → 899`。

- 36 处中 **31 处在已跟踪文件** (本提交清剿); **5 处**落在 4 个**他人未提交的新脚本**
  (`early_tight_acceptance_replay` 2 / `entry_confirm_chase_cap_evidence` 1 /
  `playbook_fee_gate_audit` 1 / `da_regime_divergence_check` 1) —— 这 4 个文件不属本批,
  **不 `git add`**, 其修复留在工作区随作者入库。
- 899 = 提交后 HEAD 与工作区的同口径实测 (见 §5.6), 取其上界保证两侧都绿。
- 基线注释里写死组成对账, 防下个 agent 把差额读成"已清零"。

## 5. 验证 (真跑)

| # | 项 | 结果 |
|---|---|---|
| 1 | 两条棘轮 | `test_except_pass_ratchet` + `test_w456_log_hygiene_ratchet` → **18 passed** |
| 2 | 计数 | 工作区 935 → **899**; 干净 worktree @登记基线 905 对撞过 |
| 3 | 语法 | 改动涉及 52 个 `.py` 全部 `py_compile` 通过 |
| 4 | 未绑定名 | `ruff --select F821` 零命中 (f-string 里的 `sym/path/field/raw/parts` 均有绑定) |
| 5 | 新增 lint | 零 (ruff 报的 20 条全是 HEAD 上既有的 E741/E731/F401/F841) |
| 6 | 提交后 HEAD 复核 | 另开 `git worktree --detach <新 HEAD>` 跑同一计数器, 与工作区同值 = 899 |
| 7 | **活体探针** | 30/36 分支被真逼走 (下表) |

### 5.1 活体探针 (不是"能编译")

做法: `importlib` 按路径加载模块 → 把模块级路径常量 (`BARS/REPLAY/CCF_BARS/SHADOW/OUT/FORWARD/JSONL/CKPT/ROOT`)
指向 tmp 里的坏 json / 坏 csv / 坏行 → 调那个函数, 断言 **stderr 那行真打印出来** 且返回值不变;
app 侧用 `unittest.mock` 造 service + 打桩 `caliber_short` 抛错走 `except` 分支。

实测输出节选:

```
[non_nominated_counterfactual] 坏 bar 跳过 US.AAPL: ValueError: could not convert string to float: 'xx'
[market_env_ledger] 坏日期行跳过 ['xx']: ValueError: time data 'xx' does not match format '%m/%d/%Y'
[vix_caliber_audit] raw 读取失败跳过 .../US.AAPL.raw.json: JSONDecodeError
[premarket_index_snapshot] 坏行情行跳过: JSONDecodeError: Expecting value: line 1 column 1 (char 0)
[playbook_fee_gate_audit] 不可解析模块跳过 .../broken.py: SyntaxError
[VIX] 锚点新鲜度检查失败, 按未过期继续: Invalid isoformat string: 'not-a-date'
[VIX] 跳变日标记读取失败(可能重复告警一天): Expecting property name ...
thesis vix 口径标签获取失败: probe-force-fail
exit_ctx vix 口径标签获取失败: probe-force-fail
```

覆盖 (30): `non_nominated_counterfactual` 4/5 · `market_env_ledger` 3 · `early_tight_acceptance_replay` 2 ·
`exit_session_window_shadow_report` 2 · `vix_caliber_audit` · `premarket_index_snapshot` · `entry_reach_audit` ·
`entry_confirm_cost_report` · `vixy_beta_calibration` · `gap_rank_attribution` · `p2b_entry_confirm_settlement` ·
`da_crosssection_replay` · `playbook_fee_gate_audit` · `wweek_peak_axis_shadow` · `supply_conf_and_confirm_scenario_report` ·
`settle_entry_confirm_forward` · `vix_monitor` 2 · `thesis` 2 · `fred_series` · `economic_calendar` (socket 路径, 返回 False)。

### 5.2 未活体 (6 处, 诚实边界)

`non_nominated_counterfactual` 的 `ctx.close` 失败、`p3_market_premise_probe` ×2 (缓存重建 / 坏日期)、
`economic_calendar` ×2 (`sock.close` / futu `ctx.close`)、`da_regime_divergence_check`(取涨跌幅) ——
都需连 Futu OpenD 才能走到。这 6 处只做了 `py_compile` + `ruff F821` 兜底 (名字绑定由 F821 证明)。

### 5.3 行为不变的证据

探针里每次都同时断言返回值: `load_bars()` → `{}`、`_rows()` → 仍返回好行、`load_pool()` → 1 行、
`_load_rows()` → 2 行、`wweek._write_jsonl()` → 原有坏行保留 + 新行追加 (3 行)、
`thesis` → `ctx["vix"]=25.64` 保留、只丢 `vix_caliber`。零控制流变更。

## 6. 无关的既有红 (定量对撞过, 非本批)

- `tests/test_vix_consumers_input_accuracy.py` 2 failed + `tests/test_vix_proxy_window_single_source.py` 6 errors:
  在 `git worktree @HEAD` 上同样 2 failed / 6 errors ⇒ 与本批的 except 块无关。
- `tests/test_thesis_direction_consistency.py` 收集期 `TypeError: Descriptors cannot be created directly`:
  futu/protobuf 版本冲突, base worktree 上同样复现。

## 7. 回滚

单文件级: `git checkout HEAD~1 -- <file>` (23 文件逐个独立, 无跨文件耦合);
整批: `git revert <this>`。基线回退 = 把 `BASELINE` 改回 905 (仅在本批被整体 revert 时)。

## 8. 遗留

- 上述 4 个他人未跟踪脚本: 我把 5 处修复写进了工作区文件, 它们入库时修复一并生效; 若作者先
  `git checkout` 覆盖, 这 5 处会退回未清剿态, 届时棘轮会红 (属其入库动作的一部分, 不是回归)。
- `docs/` 面仍排除在本棘轮之外 (DEB-20261027 证据包口径), 未变。