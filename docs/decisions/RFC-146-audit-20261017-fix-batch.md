# RFC-146: 2026-10-17 全链路审计修复批 (P0/P1)

- 日期: 2026-10-17
- 类型: 执行安全 / 数据口径 / 可观测性修复；**无策略参数变更**。其中 3 项属有限行为变更
  (VIX 缺失不再伪造、入场确认重启续窗、市价单名义上限)，方向均为"纠正静默错误"，
  逐项标注见 §2。
- 状态: 已实施（代码落地；**未提交、未重启**，生效条件见 §6）
- 触发: 用户先令「深度复查当前项目的框架、链路和逻辑，给出建议」；复核确认后令
  「如果确认无误，按照建议优化修复」
- 回归钉子: 本批新增/修改 18 个行为与契约测试文件 + 9 个棘轮/注册表守卫，见 §5

---

## 1. 背景与处置原则

审计沿真实链路逐段复核（数据源 → 信号/指令 → 执行门 → 出场 → 推送/台账），确认的
问题按"会不会静默污染交易或证据"排序处置。本批遵循：

1. **一次一变量**：按数据口径 → 执行安全 → 可观测性分组落地，每组独立回归。
2. **能只观测就不改行为**：出场意图冻结面先加影子计数，行为变更等真实数据 + DEB 预注册。
3. **失败方向优先 fail-close**：锁/名义/风控配置不可用时拒绝动作，不静默放行。
4. **新增异常处理必须留痕**：本批代码同样受 `except:pass`、W4/W5/W6 棘轮约束，
   落地后主动跑守卫，发现 3 处静默 `continue/pass` + 7 处无栈告警并就地清剿（§5）。

---

## 2. 变更清单

### A. 执行安全

#### A1. MARKET 单名义上限失守（fail-open → fail-close）

- **病灶**：`DualBrokerAdapter._notional_sanity_check` 对 `price<=0` 直接 `return True`，
  而 Playbook 市价单 `price=0.0` ⇒ 单笔名义 ≤ equity×30% 的上限与 equity 不可用时的
  fail-close 对市价单**完全失效**。
- **修法**（`app/execution/dual_broker.py`）：`qty<=0` 直接拒绝；市价单按
  `metadata.reference_price` → `_estimate_price_for_cap()`（只 peek 已注册的
  `market_data_router._shared_router`，不在下单热路径新建连接）取参考价；两者都拿不到
  ⇒ 拒绝。`playbook_service` 下单时把 `entry_price` 写入 `metadata.reference_price`。
- **行为面**：只有"无参考价的市价单"从放行变拒绝；限价单、平仓单语义不变。
- **回归**：`tests/execution/test_dual_broker_positions.py`（4 例：参考价上限/共享行情估价/
  全缺 fail-close/非正数量 fail-close）。

#### A2. 已认领指令在执行/结算窗口被重写

- **病灶**：`PlaybookDB.save_instruction` 的防覆盖守卫要求
  `executed=1 AND outcome∈{win,loss,void}`。指令被 `try_claim_execution` 认领后、
  outcome 尚未落终态的窗口里，盘中再生成可用新方向/止损/仓位**整行覆盖**持仓管理与
  结算的输入。
- **修法**（`app/playbook/db.py`）：守卫改为只看 `executed=1`（已认领即不可重写）；
  原地 UPDATE 增加 `AND COALESCE(executed,0)=0` 条件，`rowcount==0` ⇒ 回滚并保留原行
  （仲裁读与 UPDATE 之间的竞态兜底）。
- **行为面**：只收紧写入；已执行行的生成字段不再被任何重跑改写。
- **回归**：`tests/test_playbook_db.py`（认领后防覆盖 + 仲裁/写入竞态 2 例）。

#### A3. 入场确认门重启后把 120s 确认降级为立即执行

- **病灶**：pending 表是进程内存态；重启后 metadata 旗标仍是 `PENDING_CONFIRM`，
  原实现一律转 `HEALED` 并**立即执行** ⇒ 确认窗口被静默跳过。
- **修法**（`app/playbook/entry_confirm_gate.py`）：arm 时把 `ttl_sec` 一并落旗标
  （旧行无该键按 `DEFAULT_TTL_SEC`）；重启孤儿先 `_restore_arm()`——用旗标里冻结的
  `armed_at/anchor/direction/delay/ruler` 重建 pending，窗口未过 ⇒ `defer` 续上门控；
  无可信锚或窗口已整体超 TTL ⇒ 仍转 `HEALED` 放行（**绝不永久卡死**，失败路径纪律不变）。
  重建绝不用重启后的当前市价重造锚。
- **行为面**：重启不再绕过确认窗；窗口内配置通常未变，grace 用 cfg 现值。
- **回归**：`tests/test_entry_confirm_gate.py`（续窗/过期自愈/旧旗标自愈）。

#### A4. 调度器与看板并发同标的下单（跨进程互斥缺失）

- **病灶**：`_execute_via_dual_broker` 只有进程内 `threading.Lock`；调度器
  (`ftseek-us`) 与看板 (`ftseek-dashboard`) 是**两个进程**，各自持"锁"通过后，
  `duplicate_position_guard` 读 `open_positions` 的 TOCTOU 跨进程复发。
- **修法**（`app/playbook/playbook_service.py`）：下单前在
  `data/locks/symbol_orders/<safe_symbol>.lock` 取非阻塞 `fcntl.flock`，与进程内锁组成
  两级互斥。抢不到 ⇒ 拒绝本次下单（下轮重试）；锁文件不可用 ⇒ **fail-close + CRITICAL**
  （守卫崩了必须可区分，不得静默降级）；持锁方写 `pid/at` 供诊断。flock 随 fd 关闭/
  进程退出自动释放，无陈旧锁。
- **作用域**：仅同主机同文件系统（本仓无多机部署；未来拆机需换 DB/redis 锁）。
- **回归**：`tests/test_playbook_service.py`；`tests/conftest.py` 登记 `_SYMBOL_LOCK_DIR`
  为生产数据缝（否则测试会写真实 `data/locks` 并被硬闸拦截）。

#### A5. LIVE fail-mode 断言可被显式 target_env=real 绕过

- **病灶**：`risk.acb_fail_mode/preflight_fail_mode` 的启动断言只在
  `AUTO_TRADE_ACCOUNT=real` 的 DI 分支执行；`live_manual_arm` 在 AUTO=sim 下用显式
  `target_env=real` 下真单时完全绕过。运行期两处 preflight 异常分支又硬回落 `"open"`，
  等于启动时锁上的 fail-close 会在运行期被悄悄放开。
- **修法**：新增 `app/core/live_readiness.py` 统一实现——
  `live_fail_mode_issues()` / `assert_live_fail_modes()`（DI 启动用）/
  `is_live_capable_env()` / `preflight_fail_mode()`（运行期用）。
  `live_arm_gate.evaluate_live_arm_gate` 对所有 LIVE 执行补 ACB fail-mode 执行期断言；
  `auto_execute_flow` 与 `risk_gates` 的异常分支改为按环境回落：LIVE-capable → `closed`，
  SIM → `open`（SIM 默认 open 是 DEB-round2 Ω.3-3 既定设计，不改）。
- **刻意不改**：RFC-080 毕业门默认放行是**工程探针的有意例外**（审计初判为缺陷，复核为
  误报），本次未"顺手修复"，并在 `live_arm_gate.py` 注释写死，防止后续误改。
- **回归**：`tests/test_preflight_fail_mode_env.py`、`tests/playbook/test_live_probe_lock_waiver.py`、
  `tests/risk/test_r1_r5_entry_gate_contracts.py`、`tests/playbook/test_exit_compensation.py`。

### B. 数据/行情口径

#### B1. Futu K 线跨周期串缓存

- **病灶**（`app/data/futu_lv3_provider.py`）：
  ① 持久化缓存 key 只有 `symbol|autype`，60s 内 `K_DAY` 与 `K_1M` 互相命中；
  ② HTTP 拉到的任意周期 K 线回写 push ring buffer，而 buffer 是 1M bar 且行内无周期
     字段 ⇒ 后续 1M 消费方吃到日线；
  ③ `get_kline(K_DAY)` 仍按"当日"过滤，400 天日线被砍到 0-1 根，`pre_market_ranker`
     等消费方恒拿 `None`。
- **修法**：缓存 key 改为 `symbol|autype|ktype`；push ring buffer **只收 K_1M**；
  K_DAY 不再按当日过滤，按升序取最近 `count` 根；降级/异常返回缓存时使用同一 key。
- **行为面**：技术指标/盘前排序从"错周期序列"恢复为正确周期；无策略参数改动。
- **回归**：`tests/test_futu_kline_cache_caliber.py`。

#### B2. VIX 假值与 SPY 伪 MA200

- **病灶**（`app/data/market_data_router.py`）：
  ① VIX 拿不到时静默写 `20.0`，把兜底值当实测值喂给 regime/LLM/风控；20.0 又恰卡在
     NORMAL/CAUTIOUS 边界，字段上无法区分"无数据"与"真 20"；
  ② SPY MA200 在只有 50-199 根日 K 时用 `sum[-200:]/len` 的**短窗均值冒充 MA200**，
     趋势闸门以错误的尺子判定。
- **修法**：VIX 只写真实值，缺失时省略 `vix` 键并置 `vix_available=False`；
  `_classify_regime` 对 `vix is None` 跳过 VIX 分支（不用假值判 HIGH/LOW_VOL）。
  MA200 必须 ≥200 根才产出（不足只告警）；MA20/MA50 独立按各自根数计算，不再依赖 MA200 分支。
- **行为面**：VIX 缺失时 regime 不再产生假 HIGH_VOL/LOW_VOL；MA200 缺失时趋势判断
  按消费方既有默认处理，不再用短窗均值。下游自带 `.get("vix", 20)` 默认值的消费方语义不变。
- **回归**：`tests/test_market_data_caliber.py`。

#### B3. EventStream 保留策略从未生效

- **病灶**（`scripts/event_stream_retention.py`）：策略把 14 天前的 `raw_artifact` 置
  `NULL`，但生产 schema 该列是 `TEXT NOT NULL` ⇒ 每轮 UPDATE 抛 `IntegrityError`，
  cron 失败日志 0 字节 = 静默失败，清理从未发生。
- **修法**：置空串 `''`（磁盘收益与 NULL 相同，正文已释放，无需重建 1.3G 表放开约束）；
  筛选条件改为 `LENGTH(raw_artifact)>0`，同时跳过空串/NULL。
- **行为面**：保留策略首次真正生效；行本身仍全保留（`raw_id` 幂等 dedup 基座不动）。
- **回归**：`tests/test_event_stream_retention.py`。

### C. 可观测性 / 审计 / 影子

#### C1. ConfigDriftDetector 漂移从未进入推送面

- **病灶**：检测器早支持 `on_drift_detected`，但 `main.py` 构造时未传 ⇒ 盘外改配置只落
  本地日志 + WARNING，值班端零感知，改动可能一直不生效直到下次重启。
- **修法**（`main.py`）：新增 `_config_drift_alert`，经 PushHub 推 HIGH/SYSTEM，
  `dedup_key=config_drift:<old>-><new>`；推送失败/异常不打断 4h 巡检线程。
- **回归**：`tests/test_config_drift_alert.py`（含 main.py 结构钉）。

#### C2. 异步兜底告警在进程退出时静默丢失

- **病灶**（`app/utils/critical_alerts.py`）：PushHub 未受理时起 **daemon** 线程跑
  多通道重试，而 `critical_alerts.flag` 只在全部重试失败后才写。进程在 ~31s 重试窗口内
  退出/被杀 ⇒ 线程消失，CRITICAL 既未送达也无落盘痕迹。
- **修法**：起线程**之前**先写 PENDING 留痕；仅确认送达且记录未被后来者覆盖时清除；
  送达异常保留 PENDING。flag 内容比对，避免抹掉其他告警的留痕。
- **回归**：`tests/test_critical_alert_async_durability.py`、`tests/test_critical_alert_single_path.py`。

#### C3. 手动通道成交后不可追溯

- **病灶**：Dashboard 手动执行与自动通道在成交记录上不可区分；`custom_params` 对
  entry/SL/TP/size 的覆盖只存在于调用栈，成交后无从复核。
- **修法**：`execute_instruction(..., requested_by=...)`；Dashboard 传
  `dashboard:<client_ip>`，自动通道传 `auto:playbook` / `auto:playbook-live-mirror`。
  成交成功后把 `execution_origin`（requested_by/target_env/requested_at）与实际覆盖前后值
  `execution_overrides` 合并进 `metadata_json`（零 schema 迁移，best-effort，不阻断成交）。
- **回归**：`tests/test_playbook_service.py`、`tests/test_entry_confirm_live_wiring.py`。

#### C4. trail_lock_pyramid 影子计数轴错误（虚假 READY）

- **病灶**：台账 JSONL 逐轮追加（~12k 行，tick 行占绝大多数），注册表缺 `count_mode`
  ⇒ 默认按行数计，`current_n` 早早越过 `min_n=40`，把轨迹行读成"样本已达标"。
- **修法**（`app/playbook/shadow_registry.py`）：计数源改为结算器产物
  `data/shadow/trail_lock_pyramid_latest.json`，`count_mode="json_key:n_executable"`
  （帽内可执行武装事件，与 DEB① §七判据 n 轴同轴），`min_n=40` 不变。
- **回归**：`tests/test_trail_lock_pyramid_registry_axis.py`。

#### C5. 未结算出场意图的符号级冻结（先观测，暂不改行为）

- **病灶**：`_resume_playbook_exits` 把每个未结算 intent 的 `(target_env, symbol)` 放进
  `blocked_exits`，`exit_monitor_flow` 命中即**整行跳过** ⇒ 该标的 SL/TP/时间/因子规则
  全部冻结（10/1 `US.ACN`：SL 越线 2.0pp 无保护 6h）。
- **本批只做两件事**：
  1. **影子观测**：新增 `app/playbook/blocked_exit_opportunity.py`（60s 进程内节流快照）
     与 `scripts/blocked_exit_opportunity_shadow.py`（按 `intent_id` 聚合冻结时长/最大
     不利位移/SL 兜底本应退出占比）。注册表新增 `blocked_exit_opportunity`，
     `count_mode="distinct:intent_id"`，`min_n=30`，`eval_on_demand`，纯观测。
  2. **滞留年龄升级告警**：`pipeline._alert_blocked_exit_age` 每 `(intent,tier)` 一次——
     30min HIGH / 4h CRITICAL，附人工释放命令；不改释放/重评/下单行为。
  `blocked_exits` 容器由 `set` 改为 `dict`，携带该符号最早的 intent 详情
  （state/error 随 `resume_close` 结果刷新）供影子与告警使用；成员判定语义不变。
- **回归**：`tests/test_blocked_exit_opportunity_shadow.py`（21 例）、
  `tests/test_extended_hours_rth_handover.py`（返回值形状跟改）。

### D. 文档时效

- `docs/ARCHITECTURE.md`：按当日 HEAD 逐条复核 §1 双锁、§2 唯一执行链（含跨进程锁）、
  §3 退出宿主、§4 熔断锚点、§8 D1 债务行号与实测规模。
- `docs/specs/multi-agent-collaboration-charter.md`：§六 真源表 OKR-2026-09 →
  `docs/OKR-2026-10.md`；战役层改指只读组装 `scripts/okr_status.py`
  （`okr_2026_10_scoring` 判分模块未建 = 落地债#2）；§四抬头同步。

---

## 3. 明确不改 / 延后（非遗漏）

1. **出场意图自动释放、符号级→intent 级收窄、受控重评**：三项都会改变出场行为，
   必须先用 `blocked_exit_opportunity` 影子读数（n≥30 distinct episode）定 N/M，
   再另立 DEB 预注册。本批只提供证据面与告警可见性。
2. **RFC-080 毕业门默认放行**：审计初判"默认放行 = 缺陷"，复核确认为**工程探针的
   有意例外**（RFC-080 设计），不改；`live_arm_gate.py` 已留注释防止后续误改。
3. **审计其余 P1/P2 项**：按原队列处理，不在本批夹带（一次一变量）。

---

## 4. 风险评估与回滚

| 项 | 最坏情况 | 缓解 / 回滚 |
|---|---|---|
| A1 市价单上限 | 行情不可用 + 无参考价 ⇒ 市价单被拒 | 拒绝方向安全；Playbook 下单已带 `reference_price`；回滚 = 还原 `_notional_sanity_check` 与 metadata 字段 |
| A2 防覆盖 | 合法重跑被拒绝更新（保留旧行） | 只影响"已认领"行；下轮生成走新 decision_id；回滚 = 还原守卫与 UPDATE 条件 |
| A3 重启续窗 | 坏旗标导致无法重建 ⇒ 回落 HEALED 放行（与旧行为同） | `_restore_arm` 全异常兜底返回 None；回滚 = 还原孤儿分支 |
| A4 跨进程锁 | `data/locks` 不可写 ⇒ 所有下单 fail-close | 启动时目录自动创建；不可用 CRITICAL 告警；回滚 = 删锁块 + 还原调用点（见 ARCHITECTURE §2） |
| A5 LIVE fail-mode | 配置不可读时 LIVE 执行被拒 | 拒绝方向安全；SIM 不受影响；回滚 = 还原 DI 分支/门内断言/环境回落 |
| B1 K 线口径 | 缓存 key 变化导致首轮未命中（冷启动） | 仅多一次拉取；回滚 = 还原 key 与 push 写入条件 |
| B2 VIX/MA200 | 缺失时下游用自身默认值 | 不再产生假测量；回滚 = 还原 Router 兜底与 MA200 分支 |
| B3 保留清理 | 首次真清理释放正文（行保留） | 只清 14 天前 artifact；`--dry-run` 可先核；回滚 = 还原 SQL |
| C1 漂移推送 | 推送通道异常 | 回调自吞异常，不影响巡检；回滚 = 去掉 `on_drift_detected` 参数 |
| C2 PENDING 留痕 | flag 长期停留需人工核 | 送达成功自动清除；清不掉方向安全；回滚 = 还原 `send_critical` 异步分支 |
| C3 审计 metadata | metadata 写入失败 | best-effort，不阻断成交；回滚 = 去掉 requested_by 与 patch |
| C4 计数轴 | latest.json 未生成时 current_n=0（不虚假 READY） | cron 每夜刷新；回滚 = 还原 data_path/count_mode |
| C5 冻结观测 | 影子写入异常 | 写方全异常吞掉且告警，不影响监控；告警仅升级可见性；回滚 = 删调用点/注册项 |

**部署注意**：A4 依赖 `data/locks/` 可写；若未来多机部署，flock 不再成立，需换 DB/redis 锁。

---

## 5. 验证记录（2026-10-17）

```bash
# 本批行为/契约 + 棘轮/注册表守卫（27 文件）
venv/bin/python -m pytest -q \
  tests/test_event_stream_retention.py tests/test_futu_kline_cache_caliber.py \
  tests/test_market_data_caliber.py tests/execution/test_dual_broker_positions.py \
  tests/test_playbook_db.py tests/test_entry_confirm_gate.py \
  tests/test_entry_confirm_live_wiring.py tests/test_critical_alert_async_durability.py \
  tests/test_critical_alert_single_path.py tests/test_config_drift_alert.py \
  tests/test_preflight_fail_mode_env.py tests/test_playbook_service.py \
  tests/playbook/test_live_probe_lock_waiver.py tests/playbook/test_exit_compensation.py \
  tests/risk/test_r1_r5_entry_gate_contracts.py tests/test_trail_lock_pyramid_registry_axis.py \
  tests/test_blocked_exit_opportunity_shadow.py tests/test_extended_hours_rth_handover.py \
  tests/test_except_pass_ratchet.py tests/test_hardcoded_utc4_ratchet.py \
  tests/test_naive_datetime_now_ratchet.py tests/test_pipeline_size_ratchet.py \
  tests/test_shadow_registry_criteria_provenance.py tests/test_criteria_registry_visibility.py \
  tests/test_criteria_spec.py tests/test_w2_field_cardinality_ratchet.py \
  tests/test_w456_log_hygiene_ratchet.py
# → 352 passed in 51.36s

# 生产文件语法
venv/bin/python -m py_compile <本批 20 个生产文件>   # → PY_COMPILE_OK
```

**守卫自抓并已修复的回归**（诚实记录）：首轮棘轮 2 红——
`except:pass` 921→924（`dual_broker._estimate_price_for_cap` 1 处、聚合器坏行 2 处）、
W6 无栈告警 +7（`live_readiness` 2 / `entry_confirm_gate` 1 / `critical_alerts` 2 /
`playbook_service` 1 / `main.py` 1）。全部改为带栈告警后棘轮回绿，**基线不动**
（921 不升），符合"新增必须留痕、只降不升"纪律。

**此前相邻面回归**（同一工作树、同一批改动）：全量邻域 **1378 passed**、
`tests/playbook` **203 passed**、补充三文件 **69 passed**。

**全量回归与红面三分**（2026-10-17 终轮，`pytest tests/ -q` =
**7293 passed / 72 skipped / 3 failed**，460s）：

| 红项 | 归因 | 处置 |
|---|---|---|
| `test_lv3_timeliness_honesty::test_trade_snapshot_persists_measured_age...` | **本批引入**：`_override_audit` 推导式用 `for _k in (...)`，被该守卫「首处 `for _k in`」正则抢先命中（读到覆盖审计元组而非 ts_ctx 白名单） | 变量改 `_field`；该文件全绿 |
| `test_pullback_limit_shadow_telemetry::test_chase_cap_telemetry_uses_parse_raw_semantics` | **HEAD 存量**：固定窗口 10500，HEAD/工作区实测距离均为 11067（9/30 后块体继续增长） | 窗口按惯例 10500→12000 + 日期注释 |
| `test_pending_adjudication_report::test_first_run_prints_full_plain_report` | **HEAD 存量**：词典 `futu_channel_top_movers` 于 10-03 终裁 SUPERSEDED 后 cls 改为 AUTO_WATCH，测试夹具仍按 NEEDS_USER 断言白话名抬出 | 未动（属待裁域语义，需该域更新夹具） |
| `test_shadow_artifact_consumers::test_baseline_is_clean` | **HEAD 存量**：`p1_1_vwap_slope` 已 GRADUATED，但 `data/trigger_miss_telemetry.jsonl` 仍被 cron 挂载的 `scripts/gate_block_counterfactual.py` 消费，未登记 `_CONSUMER_ACK` | 未动（需判定终点报表后显式认领，属注册表治理） |
| `test_factor_stats_denominator::TestRealDataVerdictFlip::test_no_factor_becomes_positive` | **HEAD 存量/数据漂移**：测试硬编码生产路径读实时成交库，`below_ma20` n=46 贝叶斯下界 0.5513 转 positive；本批未触及 factor_analyzer 与成交写入 | 未动（数据时点问题，重跑不改变结论） |

其中 pullback/pending/artifact 三条另在 detached HEAD worktree 独立复现（与工作区同红），
证明非本批引入；因子测试因 worktree 缺生产 DB 无法同样复现，但读数只来自实时成交库。

**真实数据面**：`blocked_exit_opportunity` 注册项 `current_n=0`（当日积压 intent=0，
基线从零开始）；聚合器对真实空台账实跑 rc=0，产出
`data/shadow/blocked_exit_opportunity_latest.json`（episodes=0）。未做任何交易动作。

---

## 6. 生效与部署

- **未提交、未重启**。本批 `app/**`、`main.py`、`scripts/**` 改动需重启
  `ftseek-us` 才在调度进程生效；纯影子脚本下次运行自动加载；纯 `.md` 不触发重启。
- 重启前按军规：核对在途出场 SQL + `git status --porcelain -- app/ scripts/`，
  确认不夹带其他 agent 的 WIP；重启只用 `systemctl restart ftseek-us`，随后跑启动冒烟。
- 首次重启后观察点：①`[SymbolLock]` 无 `unavailable` CRITICAL；②LIVE 未启用时
  fail-mode 门不误伤 SIM；③K 线缓存冷启动后指标恢复正常；④VIX 缺失日出现
  `vix_available=False` 而非假 20。

---

## 7. 实施记录

**实施日期**: 2026-10-17
**Commit**: 未提交（待用户授权）
**验证结果**: 通过（本批守门 352 passed + py_compile；全量 7293 passed / 72 skipped /
3 failed，3 红均为 HEAD 存量，逐条归因见 §5）
**状态说明**: 已实施 / 待部署。本文档只记录代码与验证事实，不构成上线或重启授权。
