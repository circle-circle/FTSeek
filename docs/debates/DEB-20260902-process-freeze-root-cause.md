# DEB-20260902: 进程全冻结 10 分钟 (9/1 GIL 事件) — 根因取证、方案迭代与预注册修复

- **立案日**: 2026-09-02（用户："请深入分析工程项问题，是否还有同类风险问题" → "是否是最优雅的解决方案？" → "请写入DEB文件，我需要另外agent分析"）
- **状态**: 分析定案；修复方案 (E0/E1/E2) **已预注册待用户裁决，未实施**
- **事故等级**: 无直接资金损失（最大实损 = LLY SL 延迟 ~2min，价格回到触发位附近；broker 侧挂单兜底在位）
- **纪律引用**: 一次一变量；预注册后实施；打标前不切实盘；证据先行于方案

---

## 〇、事件概述（供冷启动的读者）

2026-09-01 21:51–22:01 CST（= ET 盘中 09:51–10:01），ftseek-us 进程整体冻结约 10 分钟：所有依赖 futu OpenD 的线程全部静默（退出快循环、VIX 监控、持仓对账、LV3 行情 30s 健康环、订阅管理器），**进程内 watchdog 自身也沉默**，直到 OpenD 自愈后（~22:00:00）才补报 587s stale 并触发 heal 重启（`systemctl restart`，1/3 当日额度）。重启后 LLY 的 SL_INITIAL 于 22:03:21 落地。

本档价值：完整因果链取证 + SDK 层根因 + 同类风险全清单 + **一轮方案自我推翻**（第一版 P0-P3 有三处缺陷，已迭代为 E0-E3 并说明理由），供另一 agent 独立审阅裁决。

---

## 一、因果链（全部有时间戳实证）

```
21:50:13  OpenD侧: 客户端连接#A RecvFailed 断连 (futu_channel_shadow 的连接, 恰好刚完成采集)
21:51:33  我方: VIX 检查正常完成 (最后一条正常业务日志)
21:51:40  我方: playbook_exit_fast_loop 最后一次心跳, 随后卡在 monitor_playbook_sim_exits 取价调用
21:51:41  LV3 push 停止 (last_push_age=586.9s 由 22:01:27 回推)
21:51:51  OpenD侧: 连接#B SendFailed (SysErrNo=11 EAGAIN) 断连
   ↓      ~9 分钟: 我方全部 futu 依赖组件静默 (含 watchdog 线程)
21:52:44  OpenD侧: 主动踢掉空闲连接 (KeepAlive TimeOut Close) — OpenD 连接管理层活着
21:52:44→22:00:00  OpenD 收到 ZERO 请求 (含新连接请求也不处理) — OpenD API 服务层停摆
~22:00:00 OpenD 自愈, 开始处理请求 (GetGlobalState ×22 = SDK 握手潮)
22:01:27  我方解冻: LV3 假活检测→重连; watchdog 此刻才报 587s stale → heal 重启
22:01:31  systemctl restart 完成; 22:03:21 LLY SL_INITIAL 落地 (重启后对账补执行)
```

证据源：`logs/ftseek_us.log-20260902`（3854-3884 行区段）；OpenD 网关日志 `/root/.com.futunn.FutuOpenD/Log/GTWLog_0_2026_09_01_21_01_56.logs`（zip 解压后 grep，21:52-22:01 窗口内 OnRecvReq 计数=0，分钟分布仅 22:00/22:01 有请求）。

---

## 二、根因三层

### 2.1 触发源（仓外黑盒）：OpenD API 服务层部分瘫

OpenD (PID 1160, 8/26 起运行) 在窗口内：既有连接报 RecvFailed/SendFailed；新连接的请求零处理（21:52:44→22:00:00 OnRecvReq=0）；但连接管理层正常（主动踢空闲连接）。约 9 分钟后**自愈，无崩溃重启**。定性：OpenD 内部故障（具体子模块不可见，OpenD 是闭源黑盒）。这是不可控的上游单点。

### 2.2 放大器（futu SDK 默认行为 + 本仓从未收口）

futu SDK 10.09.6908 的同步查询路径（`venv/.../futu/common/open_context_base.py:296`）：连接异常期走自旋等待循环 `while status != READY: sleep(0.02)`，受 `_sync_query_connect_timeout` 控制，**默认 None = 永不超时**。全仓 grep 证实**从未调用** `set_sync_query_connect_timeout`。

- 已就绪连接的请求超时 `_query_timeout=12s` 存在，但由网络线程 `on_tick`→`_check_req_timeout` 驱动 — 窗口内是否生效未验证。
- **SDK 不会自动重连死 context**：OpenD 22:00 自愈后，还要等 LV3/订阅管理器/position_sync 自己发现并重连（22:01:27 起），恢复又拖了 ~1.5min。

→ 后果：死 OpenD 上的每个同步调用 = 无限自旋（`req_info.wait()` 无超时），把"上游瘫 9 分钟"放大成"我方冻结到自愈+检测+重连全链跑完"。

### 2.3 检测失效（架构性）：进程内 watchdog 与被检者共命运

`app/observability/watchdog.py` 是进程内线程。本次它也沉默 587s。

**watchdog 沉默的假说检定（重要：本节是被另一 agent 复核的重点）**：

- **假说A（日历缓存 miss 触发 futu 调用）— 已证伪**：watchdog `_check_all()` 每轮调 `get_market_phase()` → `is_us_holiday(now_et)` → 缓存 miss 时 `_refresh_cache` 新建 OpenQuoteContext。看似完美解释，但 ET 日期 9/1 在正午（12:00 CST）就翻页，watchdog 全天调了 ~590 次（60s 间隔），9/1 日期早已入缓存（缓存在 miss 时刷一次后命中）；且 `_refresh_cache` 的 `self._lock` 在 API 调用前已释放（`_ensure_cache` 先出锁再查询）。**此假说不成立**，但注意：边界日期首次查询若恰好撞上 OpenD 瘫，该假说路径仍真实存在（风险#2 仍成立，只是不是本次的死因）。
- **假说B（watchdog 线程自身卡在某个调用上）— 未定案**。候选：模块 import（`from app.core.scheduler_triggers import ...` 每轮执行，若撞上别线程持 import lock）— 已查 sys.modules 缓存后 import 开销极低，撞锁概率低但非零；PushHub 内部锁；其他未知。
- **结论：没有冻结瞬间的线程栈，死因无法定论。这正是 E0 把"取证"内建为第一目标的原因。**

### 2.4 systemd 单元事实（方案选型的约束）

`/etc/systemd/system/ftseek-us.service`：`Type=simple`、`Restart=on-failure`、**无 WatchdogSec**、`ExecStart=start.sh`（shell 包裹，main PID 不是 python）。TimeoutStopSec=120。

---

## 三、同类风险清单（已逐条验证）

| # | 风险 | 证据 | 触发条件 | 后果 |
|---|---|---|---|---|
| 1 | futu 同步调用无超时（全仓 14+ context 创建点；热区：退出快循环取价、持仓对账、VIX 线程、dual_broker 下单、LV3 重连） | `set_sync_query_connect_timeout` 全仓零调用 | OpenD 再瘫 | 全进程冻结，同本次 |
| 2 | watchdog 检测链含 futu 调用（日历缓存 miss 时新建 ctx） | holiday_calendar.py:142 | 冻结恰好跨日界/缓存边界且 OpenD 已瘫 | 检测器与被检者同死（本次**非**此因，见 §2.3） |
| 3 | 7 处 `requests.*` 无 timeout：vix_monitor.py:223,429、sentiment_analyst.py:145、factor_engine.py:594、eastmoney_us_source.py:112、alphavantage_provider.py:556、fundamental_provider.py:47 | grep 逐条核实 | 对应外部端点 hang | 同类冻结，换触发源 |
| 4 | 进程外无 hang 检测（Type=simple 无 WatchdogSec；进程内 watchdog 共命运） | unit 文件 | 任何全进程冻结 | 只能等自愈或人工 |
| 5 | OpenD 是无监控单点（本次实锤其"半瘫"形态：连接层活、API 层死、能自愈） | GTWLog 零请求窗口 | OpenD 内部故障 | 上游所有保护的第一依赖 |

**前科档案**（复发类，非孤例）：6/19 Juneteenth 104s（飞书 SDK/OpenD 同步调用，watchdog 注释在案）；8/28 3 分钟冻结；5/15 scan_loop 1802s 卡死告警。频率：~2.5 个月 3 次，本次最长。

**已在位缓解（本次损失有界的原因）**：
- Broker 侧挂单止损：ORCL `sl_broker_order_id=8152347` 实锤在挂 — 冻结期间价格保护不依赖进程存活；
- heal 重启机制（max 3/day）工作正常；`send_critical` 告警走独立线程（架构正确，坏的只是检测）；
- 盘后 cron 管线全部独立进程，晨间判读链不受影响。

**尾部风险（未发生但存在）**：冻结窗口内需要移动/重挂的 SL（trailing 收紧、re-price）无人执行 — 若冻结撞上急跌且恰在"撤旧挂新"间隙，存在敞口。

---

## 四、第一版方案 (P0-P3) 与三处自我推翻

第一版（本会话早先提出）：

- P0: systemd `WatchdogSec=150` + app 侧独立 sd_notify 线程
- P1: 全部 14 个 futu context 创建点补 `set_sync_query_connect_timeout(15)` + 退出快循环取价包硬墙
- P2: 7 处 HTTP 补 timeout
- P3: OpenD 外部探针 cron

用户问"是否是最优雅的解决方案？"后的复核，推翻三处：

1. **P0 缺陷 — 与被检者共命运 + PID 语义泥潭**：sd_notify 线程跑在同一进程里，GIL 级冻结（C 扩展持锁不还）下 notify 线程一样死；且 `ExecStart=start.sh` 是 shell 包裹，main PID 不是 python，`Type=notify` + `NotifyAccess` 归属语义复杂化。要外部监督就该真正在进程外。
2. **P1 缺陷 — 大爆炸 + 线程泄漏**：`future.result(timeout=20)` 只是弃单，SDK 自旋线程不会死 — OpenD 持续不应答时线程只进不出，最终池耗尽；14 处一次改全是回归面。止血应按路径分级：退出快循环坏的是**钱**，其他 13 处坏的只是**可用性**。
3. **整案缺陷 — 缺取证支柱**：连 watchdog 沉默都定不了案（§2.3），说明没有栈转储的方案对"下次为什么冻"不可解。最优方案必须让下次事故从推理题变成读题。

---

## 五、预注册方案 E0-E3（待裁决，未实施）

### E0 — 外部心跳监督 + 事故时自动取证（keystone，~30 行零热路径风险）

- 退出快循环内顺手 `Path("/run/ftseek-heartbeat").touch()`（一行，每 20s）。
- systemd timer 或 cron 每分钟检查：mtime > 300s 停滞 → **先 `venv/bin/py-spy dump --pid <ftseek主进程>` 全线程栈落 `logs/freeze_stacks/`（py-spy 0.4.2 已在 venv，零新依赖），再 `systemctl restart ftseek-us`**，并发告警。
- 为什么是它：
  - GIL 免疫、进程免疫 — 对"冻结原因不可枚举"构造性免疫；
  - 检测+取证+恢复在一次开火里同时完成 — 下次事故直接拿到冻结瞬间全线程栈，§2.3 的死因悬案即解；
  - 分层无冲突：进程内 watchdog（120s 告警/360s heal）只在"内部活着"时起作用；外部 300s 恰好只接管"内部也死了"的格子，且以心跳文件停滞为唯一触发条件 — 内部活着就永不触发；
  - 复用仓库既有 cron 词汇，无 NotifyAccess/main-PID 泥潭。
- 阈值理由：内部 heal 阈值 = 120×3 = 360s；外部若 <360s 会与内部 heal 竞速（内部 heal 优先，它有 1/3 额度节流；外部是兜底）。300s 落在内部告警(120s)之后、内部 heal(360s) 之前，语义 = "内部 watchdog 已死"才接管。**此阈值为预注册值，另一 agent 可复议。**
- 风险与注意：py-spy dump 需要 ptrace 权限（root 下 OK，需实弹验证一次）；心跳写 /run/（tmpfs，零 IO 顾虑）；市场关闭时段退出快循环休眠在 `sleep_if_market_closed` — **该函数返回前不 touch 心跳，会积累 mtime 停滞** → 心跳 touch 必须放在循环顶部 ping watchdog 的同一位置（与内部 ping 同点，市场关闭时内部 watchdog 也因 WEEKEND 检查跳过告警 — 外部监督器需同判市场相位，或接受盘后/周末的假阳性并靠监督器读市场相位跳过。**此细节是 E0 实施的第一坑，留给实施者处理**）。

### E1 — 资金路径 bounded IO（收缩版 P1）

只改退出快循环取价：共享有界 executor + `future.result(timeout=20)`，超时=跳过本轮+一次性告警，绝不允许退出监控挂起。其余 13 处 futu 调用点**不动**（E0 已兜底）。二期可选熔断（连续 N 超时 → OpenD 标 down → fail-fast + 探测恢复）。

- 已知残留：弃单后 SDK 自旋线程泄漏（见 §四.2）— 接受：E0 在 300s 级兜底重启清场，且事故频率低。
- 备选强化（不推荐现在做）：给 14 个 context 创建点统一补 `set_sync_query_connect_timeout(15)`（一行/处）— 若另一 agent 认为值得一次做全，此为机械安全改动，但回归面 14 处。

### E2 — 7 处 HTTP 补 timeout（机械，~30min）

`requests.*` 全部加 `timeout=(5, 15)`。

### E3 — OpenD 外部探针（观察项，不急）

TRADING 时段每 5 分钟探测 OpenD（`get_global_state` 带 10s 超时），失败即告警，**不自动重启 OpenD**（黑盒+登录态，重启风险大于收益）。触发条件：事故第三次复发，或 E0/E1 落地后仍出现检测盲区。

### 退役（明确不做）

- sd_notify / WatchdogSec 方案（§四.1 缺陷）。
- 教科书终态（进程域隔离 / 单一 IO actor）— **明确拒绝，理由是比例性**：2.5 个月 3 次、最大实损 = SL 延迟 2 分钟、系统两次自愈；C1 冻结期为此做架构重构 = 引入最大回归面，不成比例本身即不优雅。留给事故频率证明必要时再议。

---

## 六、明示边界（本档未做的）

1. E0-E3 **未实施** — 本档是预注册，等用户裁决 + 另一 agent 分析。
2. watchdog 沉默死因未定案（§2.3 假说B 开放）— 依赖 E0 的下次事故取证。
3. OpenD 半瘫的内部机理不可见（闭源黑盒），只能从外部行为定性。
4. 8/28 3min 冻结未做同深度取证（无对应 OpenD 日志对时），本次结论对它的适用性未验证。
5. `_query_timeout=12s` 在"网络线程活着但 OpenD 不应答"场景下是否生效，未实验验证（E0 兜底后此问题降级为学术问题）。

## 七、变更清单（实施后回填）

| 件 | 路径 | 回滚 |
|---|---|---|
| 心跳 touch | `app/core/scheduler_core.py` 退出快循环（一行） | 删行 |
| 外部监督器 | `scripts/freeze_sentinel.py`（新） + cron/timer 每分钟 | 删 cron 行 + 脚本 |
| 退出取价硬墙 | `app/playbook/pipeline.py`（有界 executor + timeout） | git revert |
| HTTP timeout ×7 | 见风险#3 清单 | git revert |
| 本档 | `docs/debates/DEB-20260902-process-freeze-root-cause.md` | — |

## 附：晨间判读旁证（同会话顺手记录）

9/2 06:05 首次影子判读报告已跑：配对 n=8/20，累计 CF 净 +237.3 vs 实际 +55.79（Δ+314），四判据攒样中不裁决。一次性 cron 行已自删；飞书推送失败（cron 行缺 `PYTHONPATH=.` → `No module named 'app'`），报告本体已落盘 `data/shadow/DEB20260901_shadow_report_2026-09-01.md`，无需补救。
