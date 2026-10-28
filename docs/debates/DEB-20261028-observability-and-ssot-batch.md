# 议题：交易/过程日志审计后的低风险优化批次（复盘 JOIN 扇出、TP SSOT、告警降噪）

## 状态

- **用户指令**：2026-10-28「按建议优化执行」——承接上一轮只读审计的建议。
- **实施状态**：已实施、已回填、已重启（PID 334442）、启动冒烟 7/7 通过。
- **性质**：数据一致性、可观测性、日志噪音与测试补齐；**零交易行为变更**。
- **未做**：`chase_cap` 日志聚合暂缓，理由见 §6；不改入场/出场/仓位/止损止盈规则、
  频率、隔夜豁免、DA prompt。

## 0. 一句话结论

本轮修掉了三个会直接污染读数的缺陷：复盘 JOIN 的事实表扇出（390 笔成交被复制成
12,282 行）、Playbook 直写 `open_positions` 时 TP 永远为空、以及空 DA stderr 每两分钟
制造一次假告警；另把重复的 TP-RR INFO 和异常栈信息收口。所有改动均只影响读取、
记录或日志级别，不改变任何交易判定。

---

## 1. 复盘 JOIN 扇出（P0 读数正确性）

### 1.1 证据

`playbook_instructions` 有 **2,978** 条 `decision_id=''` 的占位行（其中
`row_kind='supply_placeholder'` 2,704 条）。原查询：

```sql
LEFT JOIN playbook_instructions pi ON tr.decision_id = pi.decision_id
```

任一无 `decision_id` 的成交便会和全部空占位行做笛卡尔匹配。生产只读实测：

| 窗口/日期 | 原 JOIN | 新 JOIN | 去重成交数 |
|---|---:|---:|---:|
| 2026-08-01..2026-09-30 | 12,282 | **390** | **390** |
| 2026-09-16 | 5,962 | **18** | **18** |

2026-08..09 窗口的 390 笔成交中，**380 笔**有可用的
`metadata_json.playbook_instruction_id` 锚点；其余 10 笔（id 1320/1511/1513/1521/
1522/1524/1526/1536/1543/1544）的 `decision_id` 在全表并不唯一，故新查询保留成交行但
`pi.*` 为 NULL，**不丢行、不再猜测关联**。

### 1.2 修法

`app/review/review_orchestrator.py::ReviewDataCollector.collect_daily()`：

1. **权威锚**：`playbook_instructions.id = trade_records.metadata_json.playbook_instruction_id`；
2. **唯一回退**：仅当锚缺失/失效时，使用全表唯一且非空的 `decision_id`；
3. 两个分支均最多返回一行，每笔 trade_record 恒只产一行。

### 1.3 零行为与验证

- 这是复盘**只读**查询；不写业务库、不参与下单/退出。
- 新契约 `tests/test_review_join_fanout_contract.py`：临时 SQLite 覆盖
  权威锚、唯一回退、重复 decision_id（不猜）、空 decision_id（不留占位行）；
  另有生产库只读“结果行数 ≤ 成交行数且 id 全唯一”回归。
- 生产只读复算：`2026-09-16` 5,962 → 18 行；390 笔窗口 12,282 → 390 行。

---

## 2. `open_positions` TP SSOT（记录一致性）

### 2.1 病灶

`app/storage/trade_store.py::upsert_position_simple()` 是 `playbook_service` 下单后的
直写路径，但原列清单只有 `initial_sl/current_sl`，没有 `initial_tp/take_profit_json`。
因此 Playbook 新仓的 TP 只存在于 `trade_records.metadata_json`：

| 仓 | trade_records | open_positions（修前） |
|---|---|---|
| `US.CRM`（id=1729） | `take_profit=262.89` | `initial_tp=NULL`、`take_profit_json=NULL` |

### 2.2 修法

- `upsert_position_simple(..., take_profit=0)` 新增绝对价格参数（USD）；
- INSERT 同时写 `initial_tp` 与
  `take_profit_json=[{"ratio":1.0,"price":<TP>}]`；TP=0 时写 `initial_tp=0` 与 `[]`；
- `ON CONFLICT` **只补空，不覆盖已有非零 TP**，保持与 `position_sync` 的
  “已有权威值优先”语义一致；
- `playbook_service.py` 的直写调用传入
  `take_profit=float(take_profit) if take_profit else 0`。

### 2.3 一次性回填

扩展 `scripts/backfill_open_position_sl_tp.py`，新增 `missing_tp` /
`missing_tp_json`，仍只补空、默认干跑、`--apply` 才写、写前审计。

生产预演命中 **1 行**：

```
US.CRM reason=missing_tp,missing_tp_json SL 232.32→232.32 TP 0.0→262.89
```

执行后：

```text
initial_tp=262.89
take_profit_json=[{"ratio": 1.0, "price": 262.89}]
```

审计文件：`data/audits/open_position_sl_tp_20261028T042104Z.jsonl`。再跑回填返回
“无需修复 (0 行)”，幂等成立。

### 2.4 零行为论证

Playbook 出场链读的是 `trade_records.metadata_json`，不读 `open_positions.initial_tp`；
`position_monitor` 在恢复/对账时明确跳过未平 Playbook 仓。`open_positions` 的 TP
仅服务重启恢复与只读展示。故本项只让两表同源，不改变止盈判定。

---

## 3. `short_funnel_shadow` 假告警

当前 `logs/da_pipeline_stderr.log` 为 **0 字节**（carrier 尚未落盘），轮转档
`da_pipeline_stderr.log-20261028` 可解析 **29** 条评级。原逻辑对 0 字节文件仍进入
“评级 <3”分支，scheduler 每约 2 分钟调用一次，产生连续 WARNING：

```
[short_funnel_shadow] ... 日志 ... 仅解析出 0 条评级 (<3)
```

修法（`scripts/short_funnel_shadow_tag.py`）：

- 0 字节文件 → `DEBUG` 跳过，不进入 `insufficient`；
- 非空但解析 <3 条 → 保留 WARNING（输入链异常可见）；
- 同一交易日仅告警一次（进程内 set；scheduler 复用模块），后续轮询降 DEBUG。

新增 `tests/test_short_funnel_shadow_tag.py` 覆盖空文件静默、非空低解析只告警一次、
正常 3 条评级写产物。手工运行生产脚本输出 `新增 0 日 ... 输入链异常 0 日: 无`，无假告警。

---

## 4. TP-RR 门禁日志刷屏

`app/playbook/exit_engine.py:739` 原为每次轮询 INFO。`US.CRM` 的
`RR=1.449519<1.5` 在监控快循环中约每 10 秒重复一次，掩盖真正的几何变化。

修法：

- 首次吞 TP、或 TP/SL/RR/`min_tp_rr` 发生变化 → INFO；
- 同一几何重复轮询 → DEBUG；
- `meta["_tp_rr_gate"]` 的字段与写入内容完全不变（仍持久化 `rr_at_execution`、
  `min_tp_rr`、`swallowed`、`tp`、`initial_sl`）。

新增 `tests/test_tp_rr_log_noise_contract.py`：首次 INFO、相同几何第二次不新增 INFO、
TP 变化后重新 INFO。

---

## 5. 异常栈与日志卫生

- `app/data/futu_lv3_provider.py:987`：订阅失败 ERROR 补 `exc_info=True`；重连逻辑不变。
- `app/playbook/pipeline.py::_is_trading_day_today()`：节假日日历异常从 DEBUG 提升为
  `WARNING(..., exc_info=True)`；交易日 fail-open 行为不变。
- `tests/test_w456_log_hygiene_ratchet.py` **16 passed**；Futu 的 except-stack 计数由
  15 降到 13、pipeline 的 debug-swallow 由 8 降到 6（基线只降不升，不重写基线）。

---

## 6. `chase_cap` 日志聚合：本批暂缓

保留日志中 `chase_cap违例遥测` 与 `chase_cap拦截` 各约 **2,823** 条
（2026-09-15..2026-10-28，含轮转档）；单个指令可见累计第 40+ 轮。

这些行不是普通噪音：**每一行的越过价、禁追线、轮次计数都是裁决证据**，
JSONL 违例遥测必须完整保留（DEB-20260903 纪律）。若只按日志去重/吞掉，会把“何时
越过、越过多少、是否最终回落成交”的证据链打断。故本批**不改 enforce、不改
skip_reason、不改 JSONL、不改轮次计数**；若要降噪，下一批只能在“首次 / 更差值 /
每 5 分钟摘要”做纯展示聚合，并单独落档预注册。

---

## 7. 验证

| 命令/项 | 结果 |
|---|---|
| `pytest tests/test_review_join_fanout_contract.py tests/test_open_position_sl_tp_contract.py tests/test_short_funnel_shadow_tag.py tests/test_tp_rr_log_noise_contract.py tests/test_backfill_open_position_sl_tp.py -q` | **20 passed** |
| `pytest tests/playbook/test_exit_engine_params.py tests/test_daily_review.py tests/review -q` | **135 passed** |
| `pytest tests/test_w456_log_hygiene_ratchet.py -q` | **16 passed** |
| `pytest tests/test_r9_r13_contracts.py tests/playbook/test_live_arm_p0_contracts.py -q` | **35 passed / 1 skipped** |
| `pytest tests/test_pipeline_horizon.py tests/test_patient_exit.py tests/test_exit_path_replay.py -q` | **98 passed** |
| `pytest` 服务/持仓相关面（playbook_service、pipeline、position_sync、trade_store） | **123 passed** |
| `pytest tests/test_except_pass_ratchet.py tests/test_pipeline_size_ratchet.py -q` | **4 passed** |
| 生产复盘只读复算 | 390/18 行，id 全唯一 |
| 回填预演/执行/幂等 | 1 行 → 写入 → 0 行 |
| `py_compile` / `git diff --check` | 通过 |

> `tests/test_w456...` 与 ratchet 的通过是“只降不升”的结果；未修改基线文件。

---

## 8. 部署与守卫

- **重启前**：未平 `trade_records` 仅 `US.CRM`（id=1729）；`open_positions` 仅 CRM；
  `data/playbook_exits.db.exit_intents` **241/241 全 BOOKED**（non-BOOKED=0）；
  `git status --porcelain -- app scripts` 仅本批 8 个文件。
- **重启**：2026-10-28 12:25:08 CST，`systemctl restart ftseek-us`，PID **334442**。
- **启动冒烟**：`scripts/startup_smoke_check.py --wait 90` → **7/7 全过**（ConfigValidator、
  漂移检测、热重载、SSOT、双臂安全、退出快循环、零 Traceback/CRITICAL）。
- **重启后只读核对**：CRM 的 `initial_tp=262.89` 且 JSON 可解析；复盘查询仍未扇出。
- 未提交 git；文档与代码保持工作树状态，待用户裁决提交范围。

## 9. 回滚

| 项 | 回滚 |
|---|---|
| 复盘 JOIN | 还原 `collect_daily()` 的 JOIN；会恢复扇出，不建议 |
| TP 直写 | 还原 `upsert_position_simple` 列清单与调用参数；已回填行可保留（无交易影响） |
| 回填 | 按审计 JSONL 的 `old_*` 值人工恢复 |
| short_funnel | 还原空文件判断与告警 set |
| TP-RR | 还原为无条件 INFO |
| Futu/节假日 | 还原日志级别/`exc_info` |

## 10. 待观察 / 下一步

1. 下一交易时段观察 `US.CRM` 的 TP-RR INFO 是否只在首轮/几何变化出现；
2. 观察 `short_funnel_shadow` 下一次 AFTER_HOURS 轮询不再出现 0 字节 WARNING；
3. `chase_cap` 展示聚合另批预注册，不碰证据行；
4. 提交前按文件筛选，避免带上工作树中的 `obsidian_vault/`、`docs/analysis/` 等既有产物。
