# DEB-20261114 — 钉钉/飞书推送降噪与跨重启幂等

- 状态：第一批已部署；安全告警不降级。后续报告合并、ERROR 双载体、直连旁路和陈旧 OKR 提示词待分批处理。
- 用户裁决：2026-11-14「请合理调整优化」。
- 原则：先修高置信低信息量消息，不改交易参数、不削弱 ERROR/CRITICAL、成交失败、账实/出场异常。

## 1. 实测问题（10-31→11-14 日志窗）

- 钉钉可识别成功外发 677 条：启动/关闭 214（32%），两类成功心跳 154（23%），合计 54%。
- Hermes 近 14 天 delivered 168 次：intraday diagnose(-late) 74（44%）。
- 跨重启重复：11-14 每周复盘、Playbook 指令/盘后追踪均有重复实例；Hub dedup 仅进程内 60s。
- 盘后综合复盘单条 198 行，且仍展示 OKR-2026-08/09/10，与当前 OKR-2026-11 冲突。
- `premarket_trend_verify` 已裁 FAIL 仍每交易日完整外发；intraday late 曾以 -$3.91 触发「关注大亏」。
- 反证保护：4 条 ERROR 窗运行 568 次零外发，旧止损监控运行 132 次零外发，不能按高频运行误删。

## 2. 本批实施

1. **正常启停静默**：新增 `push.lifecycle_notifications:false`，`main.py` 启动/关闭 INFO 仅在显式 opt-in 时发送；异常启动阻断、风险告警不走此开关。回滚=true。
2. **心跳降频**：`intraday_heartbeat_interval_min=360`、`health_check_interval_sec=21600`。盘中心跳每个交易日约 1-2 次，通道静默期探测至多 4 次/日；失败计数、熔断、半开恢复和业务成功抑制不变。回滚 60/3600。
3. **周报跨重启幂等**：`data/weekly_review_push_state.json` 记录 week_end；`_push_review_text` 返回实际接受状态，成功后才标记。状态损坏/缺失 fail-open 重推。
4. **KILLED 趋势报告周频**：每日采集和落盘不变，完整 stdout 仅周六交付；显式历史回填仍打印。
5. **小额负 PnL 去伪告警**：intraday late 只有净额 ≤-$100 才形成「显著为负/关注大亏」关注点；原始 PnL 展示与语义指纹不变。
6. **陈旧 OKR 提示词**：尝试局部更新盘后/周日 Hermes prompt，但 Edit/Bash 写入被权限分类器拒绝，**未实施**。`jobs.json` 与备份 `data/crontab.backup.20261114T230500_push_noise_jobs.json` SHA256 完全一致，未造成部分写入。

## 3. 明确不做

- 不合并/延迟 ERROR、成交失败、未决入出场、exit intent、live reconcile、data freshness、资本安全告警。
- 不删除零外发的 ERROR/止损监控，不恢复 gap，不给 RFC-194 新增 cron。
- 本批不做 ERROR 双载体收敛、直连 notifier 迁移、盘前/盘后/周六大合并，避免把安全路由与大模板改造混入小步降噪。
- 不用 `push.enabled` 当总开关（未找到可靠消费方），不扩大祖父/豁免名单。

## 4. 验证与部署

- 推送/幂等/心跳/诊断契约：**181 passed / 1 skipped**。
- 完整棘轮家族：**70 passed / 0 failed**；`git diff --check`、py_compile、check_undefined_names exit 0。
- Hermes `intraday_diagnose_late` 三副本、`premarket_trend_verify` 两副本 SHA256 一致。
- 重启前：9 仓、FILLED_UNBOOKED=0、OUTBOX_PENDING=0，app/scripts/config 仅本批及已落档 WIP。
- `systemctl restart ftseek-us` → MainPID **2982930**，启动冒烟 **7/7**；重启后 9 仓不变、两类未决簿记均 0。
- 运行实读：lifecycle=False / intraday=360min / health=21600s；新启动未再发送「已启动」INFO。重启前旧进程发送的「正在关闭」属旧代码预期行为。

## 5. 未验证与后续

- 下一个周五周报验证持久 state 只推一次；下一个周六验证趋势采集不停且仅周报外发；下一自然 intraday 窗口验证小额 PnL 不再形成关注点。
- 陈旧 OKR 展示仍需在获得 Hermes `jobs.json` 修改权限后单独处理；不得宣称已清理。
- 盘前/盘后/周六报告合并、ERROR 双载体、直连 notifier 旁路需另立小批并保留现有 state/flock/sync 语义。
- 本批不改变策略收益或 live readiness。

## 6. 第二批：OKR 清理、meta/VIX 持久去重与 ERROR 条件门复核（2026-11-15 00:09 CST）

### 6.1 已实施

1. **陈旧 OKR 提示词清理完成**：用户「继续」后重试获准。盘后 `7a40dd7c9e94` 与周日 `1e9c597b0262` 仅 prompt 字段变化：禁止把 8/9/10 月 overall_score/旧 KR 当当前进度，明确 11 月判分块未建；DA 尺须带 n/日聚类/MDE。备份 JSON 对账确认其他字段/作业不变。
2. **meta_audit 内容指纹**：`ops/hermes/meta_audit_carrier.py` 只抹首行运行时间，finding/级别/owner/due/LOOP_DONE 全参与哈希。相同未决清单跨日静默；异常、子探测器失败、finding 变化和 `META_AUDIT_FORCE=1` 不受静默影响。线上副本 SHA256 一致。
3. **VIX 级别告警持久冷却**：`app/core/scheduler_core.py` 使用 `data/vix_alert_state.json`，同级 24h 最多一次，级别跳变立即推，NORMAL 后重新武装，发送失败不写冷却。`VIX 跳变`独立逻辑不变。
4. **ERROR 双载体保留**：RFC-094 §3 条件门要求撤系统 crontab 前，用户必须显式接受 gateway 死亡时盲窗从 15min 放大到最长约 24h。用户本批仅说「继续」，不构成该专项知情接受；且并行成本≈0，故不撤。后续不得把双载体当普通重复项清理。

### 6.2 验证与未验证

- meta/VIX/提示词/推送契约 **93 passed / 1 skipped**；完整棘轮 **70 passed / 0 failed**；py_compile/check_undefined_names/diff-check exit 0。
- 重启前核 9 仓、FILLED_UNBOOKED=0、OUTBOX_PENDING=0；重启 MainPID **2985056**，冒烟 **7/7**；重启后 9 仓不变、两类未决簿记均 0。
- Hermes 提示词和 meta 由自然作业加载，无需重启 Hermes。下一次盘后/周日报验证旧 overall_score 消失；下一 meta 相同清单验证跨日静默；下一 VIX 同级窗口验证跨重启不重复。
- 盘前/盘后/周六大合并、直连 notifier 迁移仍未做；ERROR 双载体未改。任何策略收益/live readiness 不在本批范围。

## 7. 第三批：心跳窗口口径收尾（2026-11-15 23:21 CST，独立复查后追加）

### 7.1 病灶（§2.2「心跳降频」的未收尾面）

第一批把 `intraday_heartbeat_interval_min` 从 60 改成 360，但卡片内三处统计窗仍写死 30min
（`_query_signal_funnel(30)` / `_query_llm_health(30)` / `_query_alert_counts(30)`），标签也硬写
「近30min」。结果：**一张卡每 6 小时发一次，却只描述其中的 30 分钟，而读者会把它当"当前状态"** ——
约 92% 的盘中时段从未被这张卡覆盖。按本仓「不伪造数据」原则，这属口径失真，不是降噪。

复查同时确认**这不是监控空洞**（故第一批的降频本身成立）：`error_alert.py` 每 15min、
`health_check.sh` 每 10min（`pgrep` + `systemctl is-failed`）、`business_heartbeat.sh` 每 30min，
三者各有独立推送路径，且后两者绕开 PushHub 直接 `curl` webhook；`data_freshness_watchdog.py`
每日 06:25 并带自身失败钉钉兜底。心跳卡是**状态播报**，检测面没有随降频而减少。

### 7.2 已实施（零交易行为，仅推送内容）

1. **窗口跟随间隔**：`_format_heartbeat(scheduler, window_min=None)` 新增窗口参数，经
   `_resolve_window_min()` 夹在 `[30, 390]`（下限 30 = 未传/非法值逐字保旧语义；上限 390 = 6.5h
   整个盘中时段，防误配 1440 把"盘中卡"变成跨日卡），传给上述三个查询，并把三处标签改为
   `近{w}min`。`push_heartbeat(..., window_min=None)` 透传；`HeartbeatLoop` 存 `self.interval_min`
   并在 `_loop` 的两处调用点传入 ⇒ 生产实际窗口 = 360min。
2. **尾扫截断不再静默**：`_query_alert_counts` 的 5MB 尾扫在窗口拉宽 12 倍后，若日志超上限会让
   ERROR/WARNING 计数假性偏低。现回传 `truncated`，卡片显式标注「[日志尾扫截断, 计数偏低]」，
   不呈现为"0 ERROR = 干净"。（实测 `logs/ftseek_us.log` 仅 143KB，当前远未触及上限；此为防退化。）
3. **契约测试**：`tests/test_push_noise_reduction.py` 新增 5 条 —— clamp 边界（None/非法/5/360/1440）、
   三查询与三标签同时跟随 360、未传时仍为 30（回滚面）、截断标注可见、`HeartbeatLoop` 两处接线
   逐字钉住（防将来只剩一处传窗口）。

### 7.3 验证

- `pytest tests/test_push_noise_reduction.py -q` → **11 passed**（原 6 + 新 5）。
- 既有直接覆盖本模块的 `tests/coordinator/test_intraday_heartbeat.py` → **13 passed**（未改一字即绿，
  证明 `window_min=None → 30` 的旧语义回滚面成立）。
- 本批受影响根目录契约合计 **158 passed**（push 降噪/治理、watchdog liveness、meta dedup、VIX dedup、
  entry booking outbox、RFC-209 settle、da_sample、verdict outcomes）。
- 复跑命令：`venv/bin/python -m pytest tests/test_push_noise_reduction.py tests/coordinator/test_intraday_heartbeat.py -q`

### 7.4 未验证

- 下一自然盘中心跳（间隔 360min）实际外发的卡片是否显示「近360min」，需等交易日观察，本批未重启服务。
- 截断标注在真实 >5MB 日志下的表现未实测（当前日志 143KB，构造测试覆盖）。
- §2.2 的 `health_check_interval_sec` 3600→21600 未改：静默期发现死 webhook 的延迟由 1h 变 6h，
  有 `business_heartbeat.sh` / watchdog 的直接 `curl` 兜底，本批接受该延迟，仅在此留痕。

### 7.5 同批复查的另一件事：S1 臂签署前置门（**已于 2026-11-16 落地代码，权威记录在 RFC-212**）

独立复查 `RFC-212`（S1 空头最小实盘探针，状态**待签**）时发现多项未登记的签署前置缺口，
2026-11-15 先只写文档登记（`RFC-212` §5 **P8–P12** + §5.1/§5.2/§5.3、`ADJUDICATED-CRITERIA.md`
§B23 行尾「实现状态留痕」）；2026-11-16 用户裁「按推荐」后 **P6/P8/P9/P10 已提前落地代码**，
P11 查明今天无法实现（缺入场追价 bps 的采集落库），P13 新登记待裁。
**权威记录 = `RFC-212` §5.4（落地清单 + 变异验证）与 §5.5（S3 阻塞原因）**，本节只留指针，
不重复正文，避免两处口径漂移。

要点留痕（细节见 RFC-212）：P8 原为 🔴 —— `s1_arm.py` 的 `_DB` 写死 sim 研究库，而真钱空单落
`ftseek_us_live.db` ⇒ 签署日若不修，$8,400 敞口帽永不绑定、用户亲自授权的 kill switch
（累计 −$300 / 前 15 笔净<0）**在代码里永不触发**；原 P6 只覆盖评估器脚本，修完 P8 依然存在。
P10 原会与 RFC-209 前瞻 cohort（同于 11-16 起跑，人群实测 71% 为多头）正面相撞，
用户 2026-11-15 裁采「S1 只约束空头、不拦多头」。四项落地**全部 inert**
（`s1_arm_enabled` 仍 false、C0–C5 一项未动、`ALLOW_REAL_TRADING=false`、waiver 仍过期
⇒ 真钱可达性零变化），与本 DEB 的降噪主题无关，仅同批复查发现。
